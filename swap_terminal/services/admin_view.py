"""The administrator's read-only picture of the system. Reads only, always.

Role: submodule -> function (the read-only assembly behind the admin surface)
Reads: swap_terminal.db (swaps, deposit_events, payouts, wallet_inventory,
       swap_audit_log), config.Config through the dict the caller passes,
       supervisor.py's pid files, and -- only from probe_chains(), only when
       explicitly asked -- one read-only RPC call per configured chain
Writes: NOTHING. No INSERT, no UPDATE, no DELETE, no file. Every SQL statement
       in this module is a SELECT and that is checked by a test
       (tests/test_admin_view.py::test_no_statement_in_this_module_writes).
Can move funds: no. This module imports nothing that can sign, calls no
       send_to_address, and has no code path that changes a swap's status.
Mainnet-safe: yes. probe_chains() makes network calls, and they are the same
       read-only calls workers already make every cycle (getblockchaininfo /
       server_info); it never calls getnewaddress, sendtoaddress or any signing
       method.

WHY THIS SURFACE IS READ-ONLY, WRITTEN DOWN SO IT IS NOT READ AS AN OVERSIGHT.

Every obvious admin button -- retry this payout, enable this pair, cancel this
swap, restart the watcher -- is fund movement or armed state, which CLAUDE.md
rule 16 puts with the operator rather than with a UI. A read-only admin page
cannot lose money. A page with a "retry payout" button can lose it the first
time somebody clicks it on a swap whose earlier payout was actually relayed and
recorded as failed -- which is the exact gap
services/payout_service.process_pending_payouts() names in its own comment
(a timeout after the daemon accepted a transaction is marked 'failed' and is
indistinguishable from a refusal). So the answer here is a screen that tells the
operator what is true, and a shell for the two or three things that change it.

STALE VERSUS FRESH IS THE WHOLE POINT OF THE PAGE.

README.md records a staging file that sat over 6000s stale while the system used
it anyway and nothing said so louder than a warning. A dashboard that renders a
four-hour-old balance in the same type as a four-second-old one repeats that
failure with better typography. So every time-bearing reading on this page goes
through freshness(), which returns an explicit state -- fresh, stale, or missing
-- alongside the age in microfortnights, and the thresholds are echoed on screen
next to the readings they govern (rule 14).

WHAT IS NOT DUPLICATED HERE.

workers/common.endpoint_lines() and supervisor.endpoint_summary() already render
the per-chain endpoint and threshold as TEXT for a terminal banner. chain_rows()
below is the structured form of the same facts for a table, and the difference
is the rendering only: all three read config.Config and the adapters, and none
of them carries its own copy of a threshold, a pair list or a confirmation
count. If a fourth spelling appears, merge them (rule 8) -- a comment in
workers/common.py names this one.
"""

from __future__ import annotations

from chains.base import RPCAdapter
from chains.registry import unconfigured_chains, why_unconfigured
from microfortnights import format_duration
from supervisor import DEFAULT_RUN_DIR, worker_commands, worker_status

from .helpers import parse_iso, utc_now_iso
from .swap_view import ATTRIBUTION_MODELS, STAGE_ORDER, TERMINAL_STATUSES, attention, threshold_note

# How old a wallet_inventory row may be before the page calls it stale.
#
# DERIVED, not picked: workers/reconcile_worker.py refreshes inventory every
# DEFAULT_POLL_SECONDS = 60, so a row older than five of those cycles means the
# refresh is not happening rather than that it happened recently. The multiple is
# stated on screen beside the reading. tests/test_admin_view.py pins this as
# strictly greater than the reconcile worker's own interval, imported from that
# module rather than restated here, so slowing the worker fails a test instead of
# turning the page permanently red.
INVENTORY_STALE_AFTER_SECONDS = 300.0

# How old a deposit row's `last_seen_at` may be before the page flags it.
#
# THIS DOES NOT GOVERN SWAPS, and it used to. The first version of this file
# carried a single `SWAP_QUIET_AFTER_SECONDS` and applied it to the in-flight
# table -- which was a SECOND rule about when a swap has been quiet too long,
# beside the per-status one services/swap_view.attention() already owns. They
# agreed on the day they were written and would have drifted from then on
# (rule 8), and worse, they disagreed immediately: a swap sitting in
# payout_pending for 26 minutes reads SLOW to the customer, because the payout
# worker polls every 10s, and read FRESH to the operator, because an hour had
# not passed. The operator got the weaker signal. swaps_in_flight() now calls
# attention() -- the same function, the same verdict, one place -- and this
# threshold governs deposit rows only, where no other rule applies.
DEPOSIT_QUIET_AFTER_SECONDS = 3600.0

# The statuses that mean a swap is still moving. Derived from swap_view's rail
# rather than restated, minus the terminal ones, so adding a status to the state
# machine cannot leave this list behind (rule 8).
IN_FLIGHT_STATUSES = tuple(status for status in STAGE_ORDER if status not in TERMINAL_STATUSES)

# Config keys this page is allowed to echo. An allowlist rather than a
# denylist, and that direction is deliberate: Config also holds Config.RPC,
# whose BTC/LTC/GRC entries carry `user` and `password`. A denylist that forgot
# one would render a wallet credential into a web page, which is the single
# worst thing this file could do. Adding a key here is a deliberate act; nothing
# is echoed because it happened not to match a pattern.
ECHOED_CONFIG_KEYS = (
    "DB_PATH",
    "QUOTE_TTL_SECONDS",
    "RATE_CACHE_SECONDS",
    "DEFAULT_FEE_BPS",
    "AMOUNT_TOLERANCE_PCT",
    "SMALL_SWAP_MANUAL_REVIEW_USD",
    "BTC_MIN_CONFIRMATIONS",
    "LTC_MIN_CONFIRMATIONS",
    "GRC_MIN_CONFIRMATIONS",
    "SOL_MIN_CONFIRMATIONS",
    "XMR_MIN_CONFIRMATIONS",
    "XRP_MIN_CONFIRMATIONS",
    "BTC_NETWORK_FEE_RESERVE",
    "LTC_NETWORK_FEE_RESERVE",
    "GRC_NETWORK_FEE_RESERVE",
    "XMR_NETWORK_FEE_RESERVE",
)

# The read-only RPC method a Bitcoin-derived daemon answers a reachability probe
# with. It also names the network -- and naming the network from the DAEMON
# rather than from the port is the same reason xrp_chain_check.py asks for
# network_id instead of parsing the URL: a hostname or a port number can lie
# about which chain it is, and a daemon cannot. It writes nothing, signs nothing
# and takes no address or amount.
_PROBE_METHOD = "getblockchaininfo"

# WHICH ADAPTERS CAN BE PROBED, MEASURED BY READING THEM (rule 17).
#
# Two of the six cannot, and the page says so rather than reporting them as
# unreachable -- those are different facts and collapsing them would report a
# healthy Monero wallet as down.
#
#   BTC / LTC / GRC   chains/base.RPCAdapter.call("getblockchaininfo") -- a read
#                     that returns a `chain` field naming the network.
#   XRP               chains/xrp.XRPAdapter.network(), which asks the server for
#                     its network_id.
#   SOL               NO PROBE. chains/solana.py has no method that names the
#                     cluster; solana_chain_check.py does it with getGenesisHash,
#                     which the adapter does not expose. Adding one is an adapter
#                     change and is not this surface's to make.
#   XMR               NO PROBE. chains/monero.py exposes no version or network
#                     read either; monero_chain_check.py is the script that does.
#
# Named here as a capability rather than tested by asset string, so a probe is
# attempted only where one is actually implemented.
_NO_PROBE_REASON = (
    "no read-only network probe is implemented for this adapter in this application -- "
    "run its chain-check script from the repository root instead"
)


def freshness(timestamp: str | None, now_iso: str, stale_after_seconds: float) -> dict:
    """Fresh, stale or missing -- as a state, not as a number to be judged later.

    Returns `state` in (fresh, stale, missing, unreadable) plus the age both in
    seconds (for a test to assert on) and as a µfn display string (for the
    screen, rule 6). The threshold is echoed back so whatever renders this can
    print it beside the reading rather than leaving the reader to wonder what
    counted as stale.

    A missing timestamp is `missing`, never `stale` and never age 0. Those three
    are different facts: nothing was ever written, something was written long
    ago, and something was just written. Collapsing the first into either of the
    others is how a column whose writer has been broken since the day it was
    created reads as healthy.
    """
    if not timestamp:
        return {
            "state": "missing",
            "age_seconds": None,
            "age_display": "(never)",
            "threshold_display": format_duration(stale_after_seconds),
            "note": "No timestamp has ever been written here.",
        }
    try:
        age = (parse_iso(now_iso) - parse_iso(timestamp)).total_seconds()
    except ValueError:
        # Narrow on purpose: fromisoformat raises ValueError for a malformed
        # string. Reporting "unreadable" rather than "stale" keeps a corrupt
        # column distinguishable from an old one.
        return {
            "state": "unreadable",
            "age_seconds": None,
            "age_display": "(unreadable)",
            "threshold_display": format_duration(stale_after_seconds),
            "note": f"The stored timestamp {timestamp!r} is not a readable ISO timestamp.",
        }
    stale = age > stale_after_seconds
    return {
        "state": "stale" if stale else "fresh",
        "age_seconds": age,
        "age_display": format_duration(age),
        "threshold_display": format_duration(stale_after_seconds),
        "note": (
            f"Older than the {format_duration(stale_after_seconds)} threshold."
            if stale
            else f"Within the {format_duration(stale_after_seconds)} threshold."
        ),
    }


def status_counts(db) -> list[dict]:
    """Every swap status present, with its count. SQL, because it is a grouping.

    Rule 20: a count per status over rows already in the database is a GROUP BY,
    not a Python loop. Returns [] for an empty table, and the template renders
    that as `(none)` rather than as an empty region (rule 14).
    """
    return db.execute(
        "SELECT status, COUNT(*) AS swaps FROM swaps GROUP BY status ORDER BY swaps DESC, status ASC"
    ).fetchall()


def swaps_in_flight(db, now_iso: str, limit: int = 100) -> list[dict]:
    """The swaps still moving, oldest first, each with how quiet it has been.

    The status filter is bound as parameters; only the run of `?` is built from
    the LENGTH of IN_FLIGHT_STATUSES, which is structure rather than input --
    the same construction services/deposit_service.process_active_swaps() uses,
    and the same thing its S608 suppression claims.
    """
    placeholders = ",".join("?" for _ in IN_FLIGHT_STATUSES)
    rows = db.execute(
        f"""
        SELECT s.id, s.status, s.from_asset, s.to_asset, s.expected_input_amount, s.actual_input_amount,
               s.output_amount_estimate, s.min_confirmations, s.created_at, s.updated_at, s.deposit_txid,
               s.failed_reason,
               (SELECT COUNT(*) FROM deposit_events d WHERE d.swap_id = s.id) AS deposit_rows,
               (SELECT MAX(d.confirmations) FROM deposit_events d WHERE d.swap_id = s.id) AS max_confirmations
        FROM swaps s
        WHERE s.status IN ({placeholders})
        ORDER BY s.created_at ASC
        LIMIT ?
        """,  # noqa: S608 -- checked: `placeholders` is a run of '?' generated from len(IN_FLIGHT_STATUSES). No value is interpolated; the statuses and the limit are both bound below.
        (*IN_FLIGHT_STATUSES, int(limit)),
    ).fetchall()
    for row in rows:
        # The SAME verdict the customer's page shows, from the same function.
        # See DEPOSIT_QUIET_AFTER_SECONDS above for why this is not a second
        # staleness rule of the admin surface's own.
        row["attention"] = attention(row, now_iso)
    return rows


def recent_deposits(db, now_iso: str, limit: int = 25) -> list[dict]:
    """The most recently seen deposit rows, with how long since each was seen.

    Deposit rows are the evidence a chain is being watched at all: a watcher
    that stopped polling produces no new `last_seen_at`, and the age column is
    where that becomes visible. Confirmations here are COUNTS and are never
    rendered in microfortnights (rule 6); the ages beside them are durations and
    are.
    """
    rows = db.execute(
        """
        SELECT id, swap_id, asset, txid, vout, amount, confirmations, first_seen_at, last_seen_at, credited_at
        FROM deposit_events
        ORDER BY last_seen_at DESC, id DESC
        LIMIT ?
        """,
        (int(limit),),
    ).fetchall()
    for row in rows:
        row["seen"] = freshness(row["last_seen_at"], now_iso, DEPOSIT_QUIET_AFTER_SECONDS)
    return rows


def payout_rows(db, limit: int = 25) -> list[dict]:
    """Recent payout rows, most recent first. The armed-state column of the page.

    `status='created'` with no txid is the documented crash window in
    services/payout_service.py -- money possibly on chain with nothing recorded
    -- so it is surfaced rather than folded in with the rest, and the template
    labels it as the thing a human has to resolve.
    """
    return db.execute(
        """
        SELECT id, swap_id, asset, destination_address, amount, txid, status, created_at, sent_at
        FROM payouts
        ORDER BY id DESC
        LIMIT ?
        """,
        (int(limit),),
    ).fetchall()


def unresolved_payouts(db) -> list[dict]:
    """Payout rows claimed but never reported sent. The one alarm on the page.

    A row with status='created' means process_pending_payouts() committed its
    intent to pay and then either has not returned yet or died between the
    broadcast and the UPDATE that records the txid. The second case is money
    possibly on chain with no txid, and it is deliberately not retried by
    anything, so the only thing that resolves it is a human looking -- which
    means this list existing at all is the point of the admin surface.
    """
    return db.execute(
        "SELECT id, swap_id, asset, amount, created_at FROM payouts WHERE status = 'created' ORDER BY id ASC"
    ).fetchall()


def inventory_rows(db, now_iso: str) -> list[dict]:
    """Hot-wallet inventory, each row carrying its own freshness verdict."""
    rows = db.execute(
        "SELECT asset, hot_confirmed, hot_reserved, hot_available, updated_at FROM wallet_inventory ORDER BY asset ASC"
    ).fetchall()
    for row in rows:
        row["fresh"] = freshness(row["updated_at"], now_iso, INVENTORY_STALE_AFTER_SECONDS)
    return rows


def recent_transitions(db, limit: int = 25) -> list[dict]:
    """The audit log tail: what actually changed status, and when.

    The one place on the page that shows movement rather than state. An empty
    tail on a database with swaps in it means no swap has changed status since
    the rows were written, which is a fact worth being able to read.
    """
    return db.execute(
        """
        SELECT id, swap_id, old_status, new_status, message, created_at
        FROM swap_audit_log
        ORDER BY id DESC
        LIMIT ?
        """,
        (int(limit),),
    ).fetchall()


def pair_rows(config, adapters: dict) -> list[dict]:
    """Every pair this application could name, and whether it may be swapped.

    THREE STATES, NOT TWO, SINCE 2026-09-26. "In Config.ALLOWED_PAIRS" was the
    only test here, and it produced the one thing a diagnostic page must never do:
    it agreed with the defect. The operator's server had built exactly one adapter
    (no BTC_RPC_PORT, no LTC_RPC_PORT, no GRC_RPC_PORT in its process
    environment), the swap page offered six pairs badged ENABLED, Create swap
    answered `No swap was created: 'GRC'`, and this page -- the page they would
    open next to find out why -- would have said XRP -> GRC was ENABLED as well.
    chain_rows() below already reported GRC as unconfigured, so the admin surface
    contradicted itself two panels apart.

      ENABLED      in ALLOWED_PAIRS, and both chains have an adapter here
      UNREACHABLE  in ALLOWED_PAIRS, but a chain has no adapter in THIS process.
                   A quote will price and create_swap() will refuse.
      DISABLED     not in ALLOWED_PAIRS. Refused before anything else happens.

    ALLOWED_PAIRS remains the authority for what this terminal is WILLING to swap,
    and this still does not re-derive it; the adapters dict is the authority for
    what it can REACH, exactly as chain_rows() already treats it. Two authorities,
    both read, neither copied.

    WHY THIS IS NOT routes/ui.allowed_pair_rows(), which is the sibling question.
    That one builds the CUSTOMER's offer list: only the allowed pairs, and the
    template offers just the reachable ones. This builds the OPERATOR's full
    matrix: every ordered pair of every asset the tree knows, so a chain that was
    wired and never enabled is visible rather than absent. Same two authorities,
    different question, and each docstring names the other (rule 8) so a reader who
    finds one knows the other exists.

    The disabled rows are shown rather than hidden, because "XRP is off" is the
    answer to a question an operator will otherwise ask by reading source.
    """
    allowed = set(config["ALLOWED_PAIRS"])
    assets = sorted({asset for pair in allowed for asset in pair} | set(ATTRIBUTION_MODELS) | {"SOL", "XMR"})
    rows = []
    for from_asset in assets:
        for to_asset in assets:
            if from_asset == to_asset:
                continue
            enabled = (from_asset, to_asset) in allowed
            missing = unconfigured_chains(adapters, from_asset, to_asset) if enabled else []
            if not enabled:
                state, detail = "disabled", (
                    "NOT in Config.ALLOWED_PAIRS -- a quote for this pair is refused before anything else happens"
                )
            elif missing:
                state, detail = "unreachable", (
                    "in Config.ALLOWED_PAIRS, but not reachable from this process: "
                    + " Also: ".join(why_unconfigured(asset) for asset in missing)
                    + " A quote WILL price; create_swap() refuses."
                )
            else:
                state, detail = "enabled", "in Config.ALLOWED_PAIRS, and both chains have an adapter here"
            rows.append(
                {
                    "from_asset": from_asset,
                    "to_asset": to_asset,
                    "label": f"{from_asset} -> {to_asset}",
                    # `enabled` still means exactly "in ALLOWED_PAIRS" so nothing
                    # reading it changed meaning underneath. `state` is the field
                    # to branch on; `reachable` is there for a caller that wants
                    # the second half alone.
                    "enabled": enabled,
                    "reachable": enabled and not missing,
                    "state": state,
                    "missing": missing,
                    "detail": detail,
                }
            )
    return rows


def chain_rows(config, adapters: dict) -> list[dict]:
    """One row per chain: configured or not, how deposits are attributed, threshold.

    Reads the adapters dict chains/registry.build_adapters() produced, so
    "configured" here means exactly what it means everywhere else in the tree:
    an adapter was constructed. A chain with no adapter is reported as not
    configured rather than omitted -- an absent row is indistinguishable from a
    row nobody rendered (rule 14).

    `endpoint` comes from each adapter's own endpoint_line() where it has one,
    which is the same string the worker banners print, so there is one spelling
    of "where does this chain point" rather than a second one built here. The
    three Bitcoin-derived adapters have no endpoint_line(), so their host:port
    is read off the adapter's own attributes -- never from Config.RPC, which
    carries `user` and `password` in the same dict.
    """
    rows = []
    for asset in sorted(set(ATTRIBUTION_MODELS) | {"BTC", "LTC", "GRC", "SOL", "XMR"}):
        adapter = adapters.get(asset)
        threshold = config.get(f"{asset}_MIN_CONFIRMATIONS")
        rows.append(
            {
                "asset": asset,
                "configured": adapter is not None,
                "endpoint": _endpoint_text(asset, adapter),
                "attribution": ATTRIBUTION_MODELS.get(asset, "unknown"),
                "attribution_note": _attribution_note(asset),
                "threshold": threshold,
                # services/swap_view.threshold_note() -- the SAME sentence the
                # customer's confirmation panel prints. This module carried its
                # own near-identical copy until 2026-09-26, which is rule 8's
                # shape: two wordings of one fact, drifting, with the drift
                # invisible because both render.
                "threshold_note": threshold_note(asset, threshold),
                "tradeable": any(asset in pair for pair in config["ALLOWED_PAIRS"]),
            }
        )
    return rows


def _endpoint_text(asset: str, adapter) -> str:
    """Where a chain points, without ever formatting a credential.

    Config.RPC's Bitcoin entries hold `user` and `password` one key away from
    `host`, so this reads the ADAPTER's attributes and names only host, port and
    wallet. Nothing in this function can reach a password even if one is set.
    """
    if adapter is None:
        return "(not configured -- no adapter was constructed)"
    line = getattr(adapter, "endpoint_line", None)
    if callable(line):
        return line().strip()
    host = getattr(adapter, "host", "?")
    port = getattr(adapter, "port", "?")
    wallet = getattr(adapter, "wallet", "") or "(default wallet)"
    return f"{asset}  rpc={host}:{port} wallet={wallet}"


def _attribution_note(asset: str) -> str:
    """One sentence on how a deposit is matched to a swap on this chain."""
    model = ATTRIBUTION_MODELS.get(asset, "unknown")
    if model == "address":
        return "a fresh address per swap, derived by the daemon's getnewaddress"
    if model == "destination_tag":
        return "one shared account, one integer destination tag per swap; get_new_address() refuses by design"
    return "not decided in this application -- get_new_address() refuses and the custody choice is the operator's"


# What a STOPPED worker costs, per worker. Written out because the three
# consequences are genuinely different, and the page said otherwise.
#
# Measured on the operator's own admin page 2026-09-26: all three worker cards
# rendered the SAME sentence -- "a stopped payout worker leaves credited swaps in
# payout_pending indefinitely" -- because templates/admin.html hardcoded one
# consequence for every row. So two of the three cards told the operator something
# false about what stopping that worker does, on the page they would consult to
# decide whether it mattered.
#
# Rule 16 counts a wrong comment as a bug, and this is a wrong comment rendered as
# a fact about live state. It also belonged in a function rather than a template
# (rule 10): "what does stopping this cost" is a decision, and a template is where
# a decision cannot be tested.
WORKER_STOPPED_CONSEQUENCES = {
    "deposit_watcher": (
        "no deposit is ever SEEN. A customer can pay in full and their swap stays in "
        "awaiting_deposit forever, because nothing is polling the chain -- and every HTTP "
        "response still says 200."
    ),
    "payout_worker": (
        "credited swaps sit in payout_pending indefinitely. The deposit is already ours and "
        "the customer is not paid -- and every HTTP response still says 200."
    ),
    "reconcile_worker": (
        "the hot-wallet inventory stops being refreshed, so the balances on this page go stale. "
        "A stale balance is not a small balance -- it is a number nobody has checked."
    ),
}


def worker_stopped_consequence(name: str) -> str:
    """What it costs that THIS worker is not running. Never another worker's answer.

    An unknown worker gets a sentence that says it is unknown rather than borrowing
    the nearest one -- which is the defect this function replaces, one step smaller.
    """
    return WORKER_STOPPED_CONSEQUENCES.get(
        name,
        f"what a stopped {name} costs is not recorded here. Do NOT assume it is harmless: add it to "
        f"WORKER_STOPPED_CONSEQUENCES in services/admin_view.py.",
    )


def worker_rows(run_dir=None) -> list[dict]:
    """Each supervised worker's state, read from its pid file. Starts nothing.

    supervisor.worker_status() is the one implementation of "is this worker
    running", and it is READ-ONLY: it reads the pid file, asks the operating
    system whether that pid is alive, and compares /proc's cmdline against the
    recorded command so a recycled pid reports `unknown` rather than `running`.
    This function calls it and nothing else from that module -- no start, no
    stop, no signal. Importing supervisor.py spawns nothing at import time,
    which matters because wsgi.py's spawn guard raises if importing the app
    grows a thread or a child process.

    A worker reported `stopped` is the reading the page most needs to make
    obvious, and WHAT it costs differs per worker -- so each row carries its own
    consequence rather than the template stating one for all three.
    """
    directory = DEFAULT_RUN_DIR if run_dir is None else run_dir
    rows = []
    for name in sorted(worker_commands()):
        row = dict(worker_status(name, directory))
        row["stopped_consequence"] = worker_stopped_consequence(name)
        rows.append(row)
    return rows


def config_echo(config) -> list[dict]:
    """The settings that decide the answers on this page, and only those.

    An ALLOWLIST (see ECHOED_CONFIG_KEYS). Config also holds Config.RPC with
    wallet credentials in it, and a page that echoed config by pattern rather
    than by name would eventually render one.
    """
    return [{"key": key, "value": config.get(key)} for key in ECHOED_CONFIG_KEYS if key in config]


def overview(db, config, adapters: dict, now_iso: str | None = None, run_dir=None) -> dict:
    """Everything the admin page shows, in one read-only pass.

    Takes `now_iso` so every freshness verdict on one render is measured against
    ONE clock reading. Computing the time per row would let two rows on the same
    screen disagree about what "now" is, which is a small thing that makes a
    staleness table impossible to reason about.
    """
    now = utc_now_iso() if now_iso is None else now_iso
    return {
        "generated_at": now,
        "database": config.get("DB_PATH"),
        "config": config_echo(config),
        "status_counts": status_counts(db),
        "in_flight": swaps_in_flight(db, now),
        "deposits": recent_deposits(db, now),
        "payouts": payout_rows(db),
        "unresolved_payouts": unresolved_payouts(db),
        "inventory": inventory_rows(db, now),
        "transitions": recent_transitions(db),
        "pairs": pair_rows(config, adapters),
        "chains": chain_rows(config, adapters),
        "workers": worker_rows(run_dir),
        "thresholds": {
            "inventory_stale_after": format_duration(INVENTORY_STALE_AFTER_SECONDS),
            "deposit_quiet_after": format_duration(DEPOSIT_QUIET_AFTER_SECONDS),
        },
    }


def probe_chain(asset: str, adapter) -> dict:
    """Ask ONE chain whether it is reachable, read-only, and name the network.

    The only function in this module that opens a socket, and it is called only
    from probe_chains() which is called only from the explicit
    GET /api/admin/chains -- never on a page render, because a page whose load
    time is the sum of six RPC timeouts is a page an operator interrupts, which
    is rule 14's opening failure.

    WHAT IT CALLS, AND WHY EACH IS SAFE. `getblockchaininfo` on a Bitcoin-style
    daemon and `network()` on the XRP adapter are reads: they take no amount, no
    address and no key, and neither creates a wallet key the way getnewaddress
    does. Nothing here calls send_to_address, and this module imports no signing
    library.

    THE NETWORK IS TAKEN FROM THE DAEMON, NOT FROM THE PORT. A port number is a
    reason to believe and not a check (rule 17), and supervisor.endpoint_summary()
    says the same thing about the same numbers. An operator whose "testnet" alias
    points at mainnet must read the word mainnet here.

    Never raises. A failure is reported IN THE RETURN VALUE -- reachable=False
    with the reason -- which is what rule 12 requires of a broad catch: the
    caller can tell a failure from an answer, because "reachable" is the answer
    and it is False.
    """
    if adapter is None:
        return {
            "asset": asset,
            "probed": False,
            "reachable": None,
            "network": None,
            "detail": "not configured -- no adapter exists, so there is nothing to probe",
        }
    if probe_kind(adapter) == "none":
        return {
            "asset": asset,
            "probed": False,
            "reachable": None,
            "network": None,
            "detail": _NO_PROBE_REASON,
        }
    try:
        network = _ask_network(adapter)
    except Exception as exc:  # noqa: BLE001 -- checked: a probe must report every failure kind the same way, because the point of the probe is the reachable=False answer. Transport errors, auth failures, a daemon answering an error object and an adapter that refuses the call are all "this chain did not answer", they are all named in `detail`, and NOTHING downstream reads a decision from this -- it renders one table row. Narrowing would mean listing requests' exception tree plus RPCError plus XRPRPCError and still falling through on the next transport library.
        return {
            "asset": asset,
            "probed": True,
            "reachable": False,
            "network": None,
            "detail": f"did not answer: {exc}",
        }
    return {
        "asset": asset,
        "probed": True,
        "reachable": True,
        "network": network,
        "detail": f"answered; it reports its network as {network}",
    }


def probe_kind(adapter) -> str:
    """Which read-only probe this adapter supports: network_method, bitcoin_rpc, none.

    Decided by what the adapter HAS rather than by its asset string, so a new
    chain does not have to be added to a second list here (rule 8). An adapter
    with its own network() knows how to name its network; an RPCAdapter answers
    getblockchaininfo. Anything else is honestly `none` -- see _NO_PROBE_REASON.
    """
    if callable(getattr(adapter, "network", None)):
        return "network_method"
    if isinstance(adapter, RPCAdapter):
        return "bitcoin_rpc"
    return "none"


def _ask_network(adapter) -> str:
    """The one read that both names the network and proves reachability."""
    if probe_kind(adapter) == "network_method":
        return str(adapter.network())
    info = adapter.call(_PROBE_METHOD)
    if isinstance(info, dict) and info.get("chain"):
        return str(info["chain"])
    return "answered, but reported no chain name"


def probe_chains(adapters: dict, assets: tuple[str, ...] | None = None) -> list[dict]:
    """Probe every configured chain, one read-only call each. Never raises."""
    names = tuple(sorted(adapters)) if assets is None else assets
    return [probe_chain(asset, adapters.get(asset)) for asset in names]
