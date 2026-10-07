"""The administrator's read-only picture of the system. Reads only, always.

Role: submodule -> function (the read-only assembly behind the admin surface)
Reads: swap_terminal.db (swaps, deposit_events, payouts, wallet_inventory,
       swap_audit_log), config.Config through the dict the caller passes,
       supervisor.py's pid files, services/pricing.py's in-process price CACHE
       (read, never fetched), and -- only from probe_chains() and probe_peg(),
       only when explicitly asked -- one read-only RPC call per configured chain
       and one price lookup per stablecoin
Writes: NOTHING. No INSERT, no UPDATE, no DELETE, no file. Every SQL statement
       in this module is a SELECT and that is checked by a test
       (tests/test_admin_view.py::test_no_statement_in_this_module_writes).
Can move funds: no. This module imports nothing that can sign, calls no
       send_to_address, and has no code path that changes a swap's status.
Mainnet-safe: yes. probe_chains() makes network calls, and they are the same
       read-only calls workers already make every cycle (getblockchaininfo /
       server_info); it never calls getnewaddress, sendtoaddress or any signing
       method. probe_peg() makes two HTTPS GETs to a public price API, which
       carry no key and touch no chain.

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

The thinness verdict on the pricing panel is market_context.turnover_finding()
called, and the peg findings are wallet_leveling.peg_findings() called -- the
same two functions the quote path and `chain_balances.py --level` use. This
module owns no copy of either threshold, which is why THIN_TURNOVER and
PEG_TOLERANCE appear nowhere in it.

workers/common.endpoint_lines() and supervisor.endpoint_summary() already render
the per-chain endpoint and threshold as TEXT for a terminal banner. chain_rows()
below is the structured form of the same facts for a table, and the difference
is the rendering only: all three read config.Config and the adapters, and none
of them carries its own copy of a threshold, a pair list or a confirmation
count. If a fourth spelling appears, merge them (rule 8) -- a comment in
workers/common.py names this one.
"""

from __future__ import annotations

from time import time

from chains.base import RPCAdapter
from chains.daemon_network import chain_network, is_named
from chains.registry import why_cannot_pay_out, why_unconfigured
from microfortnights import format_duration
from modules.htlc_assets import settlement_verdict
from supervisor import DEFAULT_RUN_DIR, worker_commands, worker_status

# THE PAIR VERDICT COMES FROM pair_view AND IS NOT COMPUTED IN THIS FILE.
# pair_rows()'s docstring records what the second copy cost: /admin said ENABLED
# for a pair / said DISABLED about, in one process at one moment, because this
# module knew one of the three conditions pair_view has had since 2026-10-01.
from .custody_separation import wallet_label
from .helpers import parse_iso, utc_now_iso
from .pair_view import ASSET_ROLLUP_STATES, asset_rollups, pair_serviceability
from .swap_service import TAG_ATTRIBUTION, why_cannot_take_deposits
from .swap_view import (
    ADDRESS_DERIVATIONS,
    ATTRIBUTION_MODELS,
    HALTED_STATUSES,
    STAGE_ORDER,
    TERMINAL_STATUSES,
    attention,
    threshold_note,
)

#: THE PAGE'S SECTIONS, IN ORDER, FOR THE TAB STRIP.
#:
#: Operator instruction 2026-10-02: "also, our operator page should be tabs."
#:
#: TABS AS NAVIGATION, NOT AS HIDING, AND THE NUMBER IS WHY. A browser copy does
#: not include `display: none` content, and this operator reads these pages by
#: pasting them back -- which is how the eleven-times-repeated tile note and the
#: cell-whitespace bug were both caught today. Measured on the real rendered page
#: before anything was built: marking every panel but one hidden, the way any tab
#: mechanism does, a paste returns 923 of 12,502 characters -- 7.4% -- and 0 of the
#: 14 panel headings, with nothing in it saying the rest exists. That is rule 14's
#: "did nothing must not look like did work" aimed at the operator's own way of
#: reading the page, so the panels stay in the document and the strip jumps to them.
#:
#: ONE LIST AND A BIDIRECTIONAL CHECK. The strip is rendered from this tuple and
#: the headings live in the template, which is two places naming one set of
#: sections -- rule 8's shape. It is held by
#: tests/test_customer_page_layout.py, which asserts over the RENDERED page that
#: every anchor here exists as a heading id, that every heading id on the page has
#: an entry here, and that each label's words all appear in the heading it points
#: at. A label may therefore be shorter than its heading and cannot say something
#: different, and a section added to the template without a tab fails.
PAGE_SECTIONS = (
    ("stuck-heading", "Payouts never sent"),
    ("workers-heading", "Workers"),
    ("flight-heading", "Swaps in flight"),
    ("counts-heading", "Every swap status"),
    # "balance" AND NOT "inventory", 2026-10-03. The word was wrong about the
    # number: wallet_inventory.hot_confirmed is whatever the destination adapter's
    # get_balance() returned, which on a Bitcoin-derived chain is `getbalance`
    # with no arguments -- the WHOLE wallet. Measured behaviorally the same day by
    # calling services/payout_service.refresh_wallet_inventory() with a stub
    # adapter: one RPC call, `getbalance`, and hot_confirmed came back exactly
    # equal to it. On a host where GRC_RPC_WALLET is empty that wallet is the
    # daemon's default wallet, so the figure includes every coin the operator put
    # there by hand. "Inventory" names committed desk stock; this is a balance.
    ("inventory-heading", "Hot-wallet balance"),
    ("chains-heading", "Chains"),
    ("pricing-heading", "Pricing"),
    ("reachability-heading", "Reachability"),
    ("peg-heading", "The dollar"),
    ("pairs-heading", "Trading pairs"),
    ("deposits-heading", "Deposits seen"),
    ("payouts-heading", "Payouts"),
    ("audit-heading", "Status changes"),
    ("config-heading", "Settings"),
)


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
    "XRP_MIN_CONFIRMATIONS",
    "BTC_NETWORK_FEE_RESERVE",
    "LTC_NETWORK_FEE_RESERVE",
    "GRC_NETWORK_FEE_RESERVE",
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
# One of the five cannot, and the page says so rather than reporting it as
# unreachable -- those are different facts, and collapsing them would report a
# healthy wallet as down.
#
#   BTC / LTC / GRC   chains/base.RPCAdapter.call("getblockchaininfo") -- a read
#                     that returns a `chain` field naming the network.
#   XRP               chains/xrp.XRPAdapter.network(), which asks the server for
#                     its network_id.
#   SOL               NO PROBE. chains/solana.py has no method that names the
#                     cluster; solana_chain_check.py does it with getGenesisHash,
#                     which the adapter does not expose. Adding one is an adapter
#                     change and is not this surface's to make.
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


def swaps_with_status(db, statuses: tuple[str, ...], now_iso: str, limit: int = 100) -> list[dict]:
    """Swaps in any of `statuses`, oldest first, each carrying its attention verdict.

    ONE query, two status sets. It was extracted from swaps_in_flight() on
    2026-09-26 when a second caller needed the identical columns for a different
    set -- show_swap.py at the repository root, listing the swaps that have
    HALTED and are waiting on a person. The alternative was a second SELECT with
    the same eleven columns and the same two correlated subqueries, which is
    rule 8's shape exactly: the copies agree on the day they are written, and
    the day someone adds a column to one of them the other quietly reports less
    than it used to while still looking correct.

    The statuses are the only thing that varies, so they are the parameter. The
    status filter is BOUND; only the run of `?` is built from the LENGTH of the
    tuple, which is structure rather than input -- the same construction
    services/deposit_service.process_active_swaps() uses, and the same thing its
    S608 suppression claims.

    An empty `statuses` is deliberately NOT special-cased. `IN ()` is a syntax
    error and SQLite raises, where returning [] would be a quiet "no such swaps"
    for a caller whose status vocabulary had broken -- which is the failure this
    whole surface exists to make impossible. The two callers pass module-level
    constants and tests pin both as non-empty.
    """
    placeholders = ",".join("?" for _ in statuses)
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
        """,  # noqa: S608 -- checked: `placeholders` is a run of '?' generated from len(statuses). No value is interpolated; the statuses and the limit are both bound below.
        (*statuses, int(limit)),
    ).fetchall()
    for row in rows:
        # The SAME verdict the customer's page shows, from the same function.
        # See DEPOSIT_QUIET_AFTER_SECONDS above for why this is not a second
        # staleness rule of the admin surface's own.
        row["attention"] = attention(row, now_iso)
    return rows


def swaps_in_flight(db, now_iso: str, limit: int = 100) -> list[dict]:
    """The swaps still moving, oldest first, each with how quiet it has been."""
    return swaps_with_status(db, IN_FLIGHT_STATUSES, now_iso, limit)


def halted_swaps(db, now_iso: str, limit: int = 100) -> list[dict]:
    """The swaps that have HALTED and are waiting on a person. Oldest first.

    The rows behind the `HALTED_for_review` count on workers/deposit_watcher.py's
    cycle line, and the reason show_swap.py at the repository root exists: that
    counter reports a number and the operator had no way to see WHICH swap it
    was about or WHY it stopped. Measured 2026-09-26 by running overview()
    against a seeded `under_review` swap -- status_counts() reported
    `under_review: 1`, in_flight was EMPTY (the halt is not in flight, correctly),
    and `swaps.failed_reason` -- the sentence services/deposit_service.py wrote
    saying what the amounts were -- appeared nowhere in the result at all.

    Oldest first because the swap that has been waiting longest is the one whose
    customer has been waiting longest, and that is the order a queue of work is
    read in.

    NOT YET ON THE ADMIN PAGE, and that is named here rather than left for
    somebody to rediscover. overview() does not call this function, so /admin
    still shows a halted swap only as a number in the status chips -- the same
    gap on the web surface that the deposit watcher's counter had in the
    terminal. Closing it is one entry in overview()'s dict and one panel in
    templates/admin.html, both read-only. It was left out of the change that
    added this function because that change was scoped to the terminal path and
    a web panel carries its own layout and its own tests; it is owed work, not a
    decision that the page should stay as it is.
    """
    return swaps_with_status(db, HALTED_STATUSES, now_iso, limit)


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
    """The hot-wallet BALANCE rows, each carrying its own freshness verdict.

    WHAT hot_confirmed ACTUALLY HOLDS, because the table's name says otherwise and
    the page used to repeat it. services/payout_service.refresh_wallet_inventory()
    sets it to `float(adapter.get_balance())`, and chains/base.RPCAdapter.
    get_balance() is `getbalance` with NO arguments -- the whole wallet the
    endpoint serves, every address in it, change included. Measured behaviorally
    2026-10-03 by running that function against a stub adapter: exactly one RPC
    call, `getbalance`, and the stored hot_confirmed equal to what it returned.

    SO IT IS NOT DESK STOCK. With BTC_RPC_WALLET / LTC_RPC_WALLET / GRC_RPC_WALLET
    empty -- which is config.py's default and was the state on the operator's host
    that day -- the endpoint has no /wallet/<name> path and the daemon routes to
    its DEFAULT wallet, the one an operator's own CLI reaches. Every coin the
    operator happens to hold there is in this number. wallet_custody.py is the
    tool that answers which wallet an endpoint serves; this function only reports
    the figure, and the panel beside it says what the figure measures.

    NOTHING WAS CHANGED ABOUT HOW IT IS COMPUTED OR USED. Rule 16: how inventory
    is computed and what gates read it is live posture and the operator's call.
    The table, the column, the refresh and every reader are untouched -- only the
    words around the number moved, from a claim about desk stock to what was
    measured.
    """
    rows = db.execute(
        "SELECT asset, hot_confirmed, hot_reserved, hot_available, updated_at FROM wallet_inventory ORDER BY asset ASC"
    ).fetchall()
    for row in rows:
        row["fresh"] = freshness(row["updated_at"], now_iso, INVENTORY_STALE_AFTER_SECONDS)
    return rows


def retained_fee_rows(db) -> list[dict]:
    """The desk's own fee, per asset: earned, already collected, still collectable.

    WHY THIS IS ON THIS PAGE AND IN THIS PANEL. Operator, 2026-10-04: "i NEED to
    collect a fee to be profitable." The hot-wallet panel beside this reads
    `getbalance` with no arguments -- the WHOLE wallet -- so one number was
    carrying three different things at once: customer deposits awaiting payout, the
    desk's own float, and the retained fee. The panel was corrected on 2026-10-03 to
    stop calling that figure inventory, which was the right fix for the word and
    left the operator still unable to tell PROFIT from FLOAT by looking.

    This is the missing column. It does not change the balance figure or how it is
    computed (rule 16: how inventory is computed and what reads it is live posture);
    it puts the retention beside it so the two can be read against each other.

    REIMPLEMENTS NOTHING (rule 8). fee_sweep.retention_by_asset() is the one
    derivation -- fee_ledger.py's `retained` joined to what `fee_sweeps` records as
    gone -- and collect_fees.py and show_fees.py read the same function. This
    converts its tuples to the dicts this page's template consumes and adds nothing.

    IT OPENS NO SOCKET, which is what keeps it safe to call on a page render: the
    accrual is a SELECT over `swaps` and `payouts` and the swept figure is one
    GROUP BY. The wallet-can-afford-it question needs a balance and is deliberately
    NOT asked here -- that is collect_fees.py's, where the operator is deciding
    whether to send rather than reading a page.
    """
    from fee_sweep import (  # noqa: PLC0415 -- checked: local for the reason pricing_rows() and probe_peg() are local in this module -- it keeps this page's import surface to what the database rows need, and fee_sweep pulls in fee_ledger behind it.
        retention_by_asset,
    )

    return [
        {
            "asset": row.asset,
            "accrued": row.accrued,
            "swept": row.swept,
            "sweepable": row.sweepable,
            "swaps": row.swaps,
            "sweeps": row.sweeps,
        }
        for row in retention_by_asset(db)
    ]


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

      ENABLED        in ALLOWED_PAIRS, both chains reachable, the source can take
                     a deposit and the destination can pay out.
      UNREACHABLE    in ALLOWED_PAIRS, but a chain has no adapter in THIS process.
                     A quote will price and create_swap() will refuse.
      CANNOT COMPLETE
                     in ALLOWED_PAIRS, both chains reachable, and the pair still
                     cannot complete. THREE causes, not the two this line listed
                     until 2026-10-03: the destination holds no signing key, the
                     source has no deposit account, or nobody has recorded what a
                     payout on the destination costs this desk. The third refuses
                     at the QUOTE while the other two price a quote and refuse the
                     swap, so `detail` says which stage. ADDED 2026-10-02; see the
                     next paragraph for the page this state was missing from.
      DISABLED       not in ALLOWED_PAIRS. Refused before anything else happens.

    THREE STATES WERE NOT ENOUGH EITHER, AND THE FOURTH WAS MISSING FROM A LIVE
    PAGE. Measured 2026-10-02 from the operator's own rendered surfaces, in one
    process at one moment:

        /admin   XRP -> GRC   ENABLED    "both chains have an adapter here"
        /        XRP -> GRC   DISABLED   "XRP cannot take deposits:
                                          XRP_DEPOSIT_ACCOUNT is unset"
        /admin   GRC -> XRP   ENABLED    "both chains have an adapter here"
        /        GRC -> XRP   DISABLED   "XRP cannot pay out: it holds no signing key"

    The customer page was right. This one was wrong in the direction that costs
    money: it told the operator a pair was fine when a deposit on it would be
    CREDITED and the payout would then raise, leaving the swap `failed` with the
    customer's coins already taken. And it contradicted its own page -- chain_rows()
    prints "payouts=PREVIEW-ONLY unless armed at the call site" for XRP three panels
    above a pairs entry that read ENABLED for a pair ending in XRP.

    THE CAUSE WAS THAT THIS FUNCTION COMPUTED THE VERDICT ITSELF, from
    unconfigured_chains() alone -- one of the three conditions services/pair_view.py
    has had since 2026-10-01. It no longer computes anything: it calls
    pair_view.pair_serviceability(), which is the single evaluation of all three, and
    this function's remaining job is the one thing that genuinely differs, which is
    the N x N shape and the DISABLED state the customer's list has no use for.

    Four implementations of that rule existed and three were wrong. The swap page's
    was fixed 2026-09-26, payout_service.payable_assets() 2026-10-02 (02c5d56, which
    had made supervisor.py's spawn banner print "a payout worker CAN broadcast on
    GRC, XRP"), and this one is the fourth. Each was found separately, by somebody
    looking. That is the argument for the survivor owning the concept.

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

    TWO DETAIL STRINGS, AND THE DIFFERENCE IS THE POINT (rule 8's second clause:
    "if they genuinely differ, the difference belongs in a comment at BOTH sites,
    naming the other one"). The two readers of these rows are not in the same
    situation:

      `detail`        the LONG, self-contained reason, including the full
                      why_unconfigured() sentence naming the variables to export.
                      Read by operator_panel.py's teller pane (line ~764,
                      `esc(r.state) + " -- " + esc(r.detail)`), which is a
                      standalone desktop pane with NO chains table on it. It has
                      nowhere to point, so the sentence has to travel with the row.
      `short_detail`  names only WHICH SIDE is unreachable. Read by
                      templates/admin.html, where the Chains panel is three
                      panels up on the same page and already says what to set --
                      once per asset rather than once per pair.

    WHY THAT SPLIT EXISTS AT ALL, measured on the rendered page 2026-10-02: this
    function's `detail` put the whole why_unconfigured() paragraph on every
    affected pair, and `/admin` printed the .env/export sentence TWENTY-TWO times
    (twice on a pair with both sides unconfigured). The Trading pairs panel was
    1767 of the page's 3156 visible words -- 56.0% of the entire operator page --
    and nearly all of it was one sentence, re-rendered. One source, so the copies
    could not disagree; and unreadable anyway, because a reader scrolling past
    twenty-two of them cannot tell that it IS one source.
    """
    allowed = set(config["ALLOWED_PAIRS"])
    # `| {"SOL"}` STOOD HERE UNTIL 2026-09-30 and is gone because it is now redundant, proven
    # rather than assumed: ATTRIBUTION_MODELS' keys are {BTC, GRC, LTC, SOL, XRP}, and SOL is a
    # member because the table derives its tag entries from TAG_ATTRIBUTED_ASSETS. The literal
    # existed precisely BECAUSE the table did not know about SOL -- a hardcoded patch over the
    # drift the derivation removes (rule 9: consolidation creates dead code, and this is it).
    assets = sorted({asset for pair in allowed for asset in pair} | set(ATTRIBUTION_MODELS))
    rows = []
    for from_asset in assets:
        for to_asset in assets:
            if from_asset == to_asset:
                continue
            enabled = (from_asset, to_asset) in allowed
            # COMPUTED ONCE PER CELL AND NOT PER KEY. The first draft called
            # settlement_verdict() twice in the row literal below -- once for the dict
            # and once for the headline -- which is two evaluations of one decision in
            # one row, the shape this whole file's docstring is about.
            #
            # NOT SKIPPED FOR A DISABLED PAIR, unlike pair_serviceability() above. The
            # reason that one is skipped is that naming a missing adapter for a pair the
            # operator has not enabled sends them to configure a chain that would change
            # nothing. Settlement is the opposite case: "this pair is not enabled AND an
            # atomic driver covers it, run green" is exactly what an operator deciding
            # whether to enable it needs, and it costs no socket to say.
            settlement = settlement_verdict(from_asset, to_asset)
            # ONE CALL, AND IT IS THE ONLY PLACE THE VERDICT COMES FROM. Skipped for
            # a pair that is not in ALLOWED_PAIRS, because the reason it is refused
            # is the pair list: naming a missing adapter instead would send the
            # operator to configure a chain that would change nothing.
            verdict = (
                pair_serviceability(config, adapters, from_asset, to_asset)
                if enabled
                else {"missing": [], "cannot_pay": "", "cannot_take": "", "cannot_quote": "",
                      "cannot_establish": "", "serviceable": False, "reason": ""}
            )
            missing = verdict["missing"]
            if not enabled:
                state, detail = "disabled", (
                    "NOT in Config.ALLOWED_PAIRS -- a quote for this pair is refused before anything else happens"
                )
                short_detail = "not in ALLOWED_PAIRS"
            elif missing:
                state, detail = "unreachable", (
                    "in Config.ALLOWED_PAIRS, but not reachable from this process: "
                    + verdict["reason"]
                    + " A quote WILL price; create_swap() refuses."
                )
                # NAMES THE SIDE AND STOPS. Which variable to export is in the
                # Chains panel on the same page, once per asset; what the state
                # costs ("a quote WILL price; create_swap() refuses") is in that
                # panel's own note, once per page. `missing` is already on the
                # row, so this is a rendering of it and not a second derivation.
                short_detail = f"{_and_list(missing)} unreachable here"
            elif not verdict["serviceable"]:
                # THE STATE THAT WAS MISSING. Both chains reachable and the pair
                # still cannot complete, which is the case that read ENABLED on this
                # page while the customer page refused it. The reason is the
                # authority's own sentence -- why_cannot_pay_out() or
                # why_cannot_take_deposits() -- carried verbatim, so the two surfaces
                # cannot word one refusal two ways.
                # WHERE THE REFUSAL LANDS DEPENDS ON WHICH CONDITION REFUSED, and this
                # sentence said "A quote WILL price" unconditionally until 2026-10-02.
                # For the fourth condition that is backwards: a pair with no fee
                # reserve refuses AT THE QUOTE and never reaches create_swap(). An
                # operator reading "a quote WILL price" about GRC -> XRP would go
                # looking for the bug in the swap path, where it is not.
                stage = (
                    "A quote REFUSES; nothing reaches create_swap()."
                    if verdict["cannot_quote"] or verdict["cannot_establish"]
                    else "A quote WILL price; create_swap() refuses."
                )
                state, detail = "cannot_complete", (
                    "in Config.ALLOWED_PAIRS and both chains have an adapter here, but this pair cannot "
                    "complete: " + verdict["reason"] + " " + stage
                )
                # WHICH CONDITION REFUSED, named for the pill. The sentence itself is
                # in this panel's state legend, once per state, and the asset's own
                # row in the Chains table carries its payout and deposit posture.
                #
                # THE THIRD BRANCH IS NOT COSMETIC. This was a two-way expression, so
                # a pair refused by the fee reserve -- neither cannot_pay nor
                # cannot_take -- fell through to the else and the pill read
                # "GRC cannot take deposits" about the chain this desk pays out of
                # every day. A pill that names the wrong end is worse than no pill:
                # it is a measurement-shaped statement that is false.
                if verdict["cannot_pay"]:
                    short_detail = f"{to_asset} cannot pay out"
                elif verdict["cannot_take"]:
                    short_detail = f"{from_asset} cannot take deposits"
                elif verdict["cannot_quote"]:
                    short_detail = f"no {to_asset} fee reserve, so no quote"
                else:
                    # THE FOURTH CAUSE, 2026-10-03. The chain imposes a payout minimum
                    # and could not be asked for it, which is a NOW problem rather than
                    # a configuration one -- so the pill says the chain could not be
                    # reached rather than naming a setting the operator would go and
                    # check for nothing.
                    short_detail = f"{to_asset}'s payout minimum could not be read from the chain"
            else:
                state, detail = "enabled", verdict["reason"]
                short_detail = "reachable, can take a deposit, can pay out"
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
                    # CARRIED THROUGH FROM THE AUTHORITY, 2026-10-02, so a reader of
                    # these rows can see WHICH of the three conditions refused
                    # without re-asking the registry. templates/admin.html uses them
                    # to cross-check its own Chains table, and
                    # tests/test_customer_page_layout.py asserts the two surfaces
                    # report every pair the same way.
                    "cannot_pay": verdict["cannot_pay"],
                    "cannot_take": verdict["cannot_take"],
                    "cannot_quote": verdict["cannot_quote"],
                    "cannot_establish": verdict["cannot_establish"],
                    "serviceable": enabled and verdict["serviceable"],
                    "detail": detail,
                    "short_detail": short_detail,
                    # HOW IT WOULD SETTLE, from the one place that decides it
                    # (modules/htlc_assets.settlement_verdict()), the same dict
                    # services/pair_view.allowed_pair_rows() carries. Added 2026-10-03.
                    #
                    # IT IS NOT A STATE AND DOES NOT TOUCH `state`, which is deliberate:
                    # every pair on this page settles CUSTODIALLY, so a settlement badge
                    # beside the verdict column would be the same value on all twenty
                    # cells and tell a reader nothing. What varies, and what an operator
                    # wants, is WHICH atomic driver covers the pair and whether it has
                    # ever been RUN -- so it rides on the cell's label rather than on its
                    # pill. `settlement_headline` is the short form, spelled by that
                    # module rather than assembled here.
                    "settlement": settlement,
                    "settlement_headline": settlement["headline"],
                }
            )
    return rows


def pair_matrix(rows: list[dict]) -> dict[str, dict[str, dict]]:
    """pair_rows()'s rows, indexed [source][destination]. DERIVED, never recomputed.

    TAKES THE ROWS AND NOT (config, adapters), which is the whole point. A version
    that took the configuration would evaluate the verdict a second time, and a
    second evaluation of that verdict is the defect this module was repairing
    yesterday: services/admin_view.pair_rows() had one of the three conditions
    services/pair_view.py has, and /admin said ENABLED about a pair / said DISABLED
    about, in one process. The same shape one layer up would be the same bug.

    So this is an INDEX over rows that already exist. Every cell IS a row -- the
    same dict object -- so a cell cannot disagree with the list it came from, and
    `label`, `state` and `short_detail` are whatever pair_rows() put there.

    The diagonal is absent rather than filled with a placeholder: an asset to itself
    is not an ordered pair, pair_rows() does not emit one, and inventing a row for
    it here would be this function deciding something. The template renders the
    diagonal in words instead (rule 14: a blank cell is ambiguous between "not a
    pair" and "a verdict that failed to render").
    """
    matrix: dict[str, dict[str, dict]] = {}
    for row in rows:
        matrix.setdefault(row["from_asset"], {})[row["to_asset"]] = row
    return matrix


def pair_assets(rows: list[dict]) -> list[str]:
    """Every asset that appears on either side of any pair row, sorted.

    READ OFF THE ROWS, for the same reason pair_matrix() is: pair_rows() already
    unions ALLOWED_PAIRS' assets with ATTRIBUTION_MODELS' keys and its comment
    records that a hardcoded second copy of that set was deleted on 2026-09-30 as "a
    hardcoded patch over the drift the derivation removes". Rebuilding the set here
    would restore exactly that.
    """
    return sorted({row["from_asset"] for row in rows} | {row["to_asset"] for row in rows})


def _and_list(names) -> str:
    """"BTC", or "BTC and LTC", or "BTC, GRC and LTC".

    chains/registry.why_unconfigured() joins a list of variable names exactly
    this way. That is two copies of one rule and they are NOT merged, for the
    reason rule 8 allows: this one is in a template-facing view module and that
    one is inside the sentence it builds, and importing a private join helper
    across that boundary to save four lines would couple the admin page's
    wording to the registry's. The difference is named here and the other site is
    named with it, which is what rule 8 asks for when two copies genuinely stay.
    """
    names = list(names)
    if len(names) <= 1:
        return names[0] if names else ""
    return ", ".join(names[:-1]) + f" and {names[-1]}"


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
    # THE SAME CULL, and this union was the louder one: all four literals are keys of
    # ATTRIBUTION_MODELS (checked 2026-09-30 -- {BTC, GRC, LTC, SOL, XRP}), so it restated the
    # table's own contents beside the table. What it was FOR is worth keeping in words: every
    # chain this application knows gets a row whether or not a pair enables it, because "XRP is
    # off" is the answer to a question an operator would otherwise ask by reading source. That
    # intent is now served by the table alone, and a chain absent from the table has no
    # attribution model to report anyway.
    for asset in sorted(ATTRIBUTION_MODELS):
        adapter = adapters.get(asset)
        threshold = config.get(f"{asset}_MIN_CONFIRMATIONS")
        model = ATTRIBUTION_MODELS.get(asset, "unknown")
        rows.append(
            {
                "asset": asset,
                "configured": adapter is not None,
                "endpoint": _endpoint_text(asset, adapter),
                # WHY IT IS UNCONFIGURED, AND THIS ROW IS NOW THE ONLY PLACE ON
                # THE PAGE THAT SAYS IT. Added 2026-10-02. It was previously
                # reachable ONLY through pair_rows()'s `detail`, which meant the
                # sentence naming the variables to export rendered once per
                # affected PAIR -- twenty-two times on the operator's page -- and
                # zero times in the table of chains, whose `endpoint` column said
                # "(not configured -- no adapter was constructed)" and left the
                # reader to find the remedy in the pairs panel four panels down.
                # The reason is per ASSET, so it belongs on the asset's row.
                # "" for a configured chain, so the template renders nothing
                # rather than an empty-looking sentence.
                "why_unconfigured": "" if adapter is not None else why_unconfigured(asset, config.get("RPC")),
                "attribution": model,
                # SPLIT IN TWO, 2026-10-02, because the first half was the same
                # words on every chain in the model and the second half is the
                # only part that varies. The page renders the model halves once
                # each in a legend and the asset halves once per row; composed,
                # they are the sentence that used to be on every row. Three
                # address chains shared the "a fresh address per swap, and the
                # address IS the attribution" opener and two tag chains shared
                # the whole tag sentence bar two words.
                # THE TWO ENDS, PER ASSET, ADDED 2026-10-02. Both were reachable
                # only through a PAIR before this -- pair_view's `reason` on the
                # customer page and pair_rows()'s short_detail in the matrix cell --
                # and both are facts about one chain, not about a pair. An asset
                # that cannot pay out cannot pay out in any pair, and the operator
                # asking "why is XRP off everywhere" was reading twelve pair rows
                # for one answer.
                #
                # THE SAME TWO AUTHORITIES THE VERDICT USES, called directly rather
                # than re-derived: chains/registry.why_cannot_pay_out() and
                # services/swap_service.why_cannot_take_deposits(). "" means the
                # chain can, which is what both functions return for no refusal.
                "cannot_pay": why_cannot_pay_out(adapters, asset),
                "cannot_take": why_cannot_take_deposits(config, adapters, asset),
                "attribution_model_note": attribution_model_note(model),
                "attribution_detail": attribution_asset_detail(asset),
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
    # wallet_label() RATHER THAN `or "(default wallet)"`, 2026-10-03. This file
    # and workers/common.endpoint_lines() spelled that same fallback phrase for
    # the same question, which is rule 8's two-copies shape -- and both of them
    # said something true that told the operator nothing. An empty wallet value
    # means this endpoint has no /wallet/<name> path, so the daemon routes to
    # whichever wallet it serves by default: the one a bare CLI call reaches. That
    # is the custody question, visible on this row and nowhere else on the page,
    # and it rendered as a parenthetical that reads like a default being fine.
    wallet = wallet_label(asset, getattr(adapter, "wallet", "") or "")
    return f"{asset}  rpc={host}:{port} wallet={wallet}"


def attribution_model_note(model: str) -> str:
    """How an attribution MODEL works. One sentence per model, never per asset.

    SPLIT OUT OF _attribution_note() 2026-10-02, AND THE NUMBER IS WHY. That
    function returned one sentence per ASSET, and the leading clause of it was
    identical across every asset in a model -- so the Chains table printed

        "a fresh address per swap, and the address IS the attribution."

    three times (BTC, GRC, LTC) and

        "one shared <chain> account, one integer per swap carried as its
         <field>; get_new_address() refuses by design"

    twice (SOL, XRP), differing in two words. Measured on the rendered page the
    same day: the address opener appeared 3 times and the Chains panel was 439 of
    the page's 3156 visible words, 13.9%, second only to Trading pairs.

    The model note goes in a legend above the table, once per model that any
    chain uses; attribution_asset_detail() below carries the only part that
    varies. Composed, the two halves are the sentence that used to be on every
    row -- which is what lets the collapse be proven to lose nothing rather than
    asserted to.

    An unknown model gets a sentence that says the application has not decided,
    rather than borrowing the nearest model's -- the same refusal
    worker_stopped_consequence() makes for an unknown worker, and for the same
    reason: a page that guesses is worse than a page that says it does not know.
    """
    if model == "address":
        return "a fresh address per swap, and the address IS the attribution."
    if model == "tag":
        # PHRASED SO IT READS CORRECTLY FOR BOTH CHAINS IN THE MODEL, which the
        # first attempt at the per-asset version did not: "one integer Memo
        # instruction per swap" is garbled, because on Solana the integer is
        # CARRIED BY the memo rather than being the memo. The integer is what
        # both chains have in common and the field is where each one puts it, so
        # the sentence says that -- and the field's name per chain is in the
        # asset detail, where the thing that varies belongs.
        return ("one shared account per chain, one integer per swap carried in that chain's own field; "
                "get_new_address() refuses by design.")
    return "not decided in this application -- get_new_address() refuses and the custody choice is the operator's."


def attribution_asset_detail(asset: str) -> str:
    """The part of the attribution answer that is true of THIS asset and no other.

    NAMES THE DERIVATION PER ASSET, and that is the repair this function is. Both
    tables live in services/swap_view.py and neither is restated here -- the
    customer's page and this page answer "how is a deposit attributed" from one
    place (rule 8), the same arrangement threshold_note() already has.

    THE DEFAULT BRANCH HAS NOW RENDERED FOR TWO CHAINS IT WAS FALSE ABOUT, which is why the
    table it reads is DERIVED rather than merely corrected. Measured 2026-09-27 by calling this
    function directly, it returned

        "not decided in this application -- get_new_address() refuses and the
         custody choice is the operator's"

    for a chain whose adapter DID return a real per-swap address and whose custody question was
    not open. It reached the default only because swap_view.ATTRIBUTION_MODELS did not list it
    while chain_rows() below forces every reachable chain into the table. So the false sentence
    rendered on the operator's chain page beside an `attribution` column reading "unknown". The
    chain WAS decided; only the mapping had not heard.

    THEN IT HAPPENED AGAIN, TO SOL, AND THIS DOCSTRING ASSERTED IT COULD NOT. The paragraph
    above used to continue "It is true of SOL -- chains/solana.py get_new_address() raises
    NotImplementedError and its message names the three custody options README.md leaves with
    the operator". That was true when it was written and false by the next day. The operator
    chose the one-account-plus-memo strategy on 2026-09-29 (commit d22b2a1), SOL joined
    services/swap_service.TAG_ATTRIBUTED_ASSETS and TAG_ATTRIBUTION, chains/solana_memo.py was
    written and chains/solana.py._attributable() credits by that memo tag -- and measured
    2026-09-30, this function still told the operator SOL's attribution was undecided.
    get_new_address() does still refuse, which is the sentence's own trap: the half a reader can
    verify stayed true while the half that mattered went stale.

    Same table, same failure, twelve days apart, so ATTRIBUTION_MODELS now derives its tag
    entries from TAG_ATTRIBUTED_ASSETS and a third tag chain cannot reach this default at all.

    The address-model clause was the other half. It said "derived by the daemon's
    getnewaddress" for every chain in that model, which is right for the three
    Bitcoin-derived daemons and wrong for anything else that joins -- so admitting
    a chain to the model without splitting that clause per asset replaces one
    false sentence with another. ADDRESS_DERIVATIONS is that split.

    An asset in the address model with no derivation recorded says so rather than
    borrowing the nearest chain's, which is the same refusal
    worker_stopped_consequence() makes further down this file, and for the same reason:
    a page that guesses is worse than a page that says it does not know.

    RETURNS "" FOR THE UNKNOWN MODEL, deliberately. There is nothing asset-specific
    to say about a chain the application has not decided about, and
    attribution_model_note() already says the application has not decided. A
    placeholder here would be a second sentence saying the same nothing -- and the
    template renders `(nothing specific to this chain)` for the empty case, so the
    cell is never a blank gap (rule 14).
    """
    model = ATTRIBUTION_MODELS.get(asset, "unknown")
    if model == "address":
        derivation = ADDRESS_DERIVATIONS.get(asset)
        if derivation is None:
            return (
                f"HOW {asset} derives that address is not recorded here -- add it to ADDRESS_DERIVATIONS "
                f"in services/swap_view.py"
            )
        return f"Derived by {derivation}"
    if model == "tag":
        # NAMED PER ASSET, from the one table that knows. XRP's discriminator is a
        # DestinationTag on the XRP Ledger; SOL's is a Memo instruction on Solana. This line
        # said "one integer destination tag per swap" for every tag chain until 2026-09-30,
        # which was true of the only member it had and would have been false about Solana the
        # moment SOL joined -- the same shape as the address clause two branches up, which said
        # "derived by the daemon's getnewaddress" for every address chain and is now split per
        # asset for exactly this reason. Rule 8: the copies agree on the day they are written.
        _, discriminator, network = TAG_ATTRIBUTION[asset]
        return f"{network}; the integer is carried as its {discriminator}"
    return ""


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
        "the hot-wallet balance rows stop being refreshed, so the balances on this page go stale. "
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


# The two coins the dollar on this page is checked against, and the one place
# the admin surface names them.
#
# THEY ARE DELIBERATELY NOT IN services/pricing.IDS. Adding them there would put
# them inside _require_every_asset(), so a CoinPaprika outage on USDC -- a coin
# no pair on this terminal trades -- would refuse every quote. The peg is a
# yardstick check on the reporting, not an input to a price, and a yardstick
# that can veto a swap is the wrong shape. The cost of keeping them out is that
# the cached reading below cannot carry them, which is why the peg is a separate
# explicit probe rather than a row in the pricing panel.
_PEG_NOT_ON_RENDER = (
    "not checked on page load -- USDC and USDT are not in services/pricing.IDS (see "
    "_PEG_NOT_ON_RENDER in services/admin_view.py for why), so checking the peg is a "
    "fetch and this page fetches nothing. Ask for it."
)


def pricing_rows(snapshots) -> list[dict]:
    """One row per priced asset, with the thinness verdict beside the numbers.

    THE VERDICT IS market_context.turnover_finding(), CALLED, NOT RESTATED. That
    function was `_turnover_finding` until 2026-09-30 and became public for this
    caller rather than being copied into it -- a second `volume / cap <
    THIN_TURNOVER` in this file is rule 8's exact shape, and the drift would be
    invisible: each site would look right in its own file and the page would
    disagree with the quote a customer was refused.

    A None finding is rendered as "(not measured)" and not as OK. turnover_finding()
    returns None when the cap or the volume is unusable, which for GRC is the
    ORDINARY case on a feed that reports market_cap 0 -- and a missing thinness
    reading must not read as a passed one (rule 14).
    """
    from .market_context import (  # noqa: PLC0415 -- checked: imported inside the function, not at module scope, because market_context imports services.pricing which imports `requests`; a host without it must still be able to import admin_view for the database-only rows, which is what every other reading on this page is.
        turnover_finding,
    )

    rows = []
    for snapshot in snapshots:
        finding = turnover_finding(snapshot)
        rows.append(
            {
                "asset": snapshot.asset,
                "price_usd": snapshot.price_usd,
                "market_cap_usd": snapshot.market_cap_usd,
                "volume_24h_usd": snapshot.volume_24h_usd,
                "change_24h_pct": snapshot.change_24h_pct,
                "turnover_verdict": "(not measured)" if finding is None else finding.verdict,
                "turnover_note": (
                    "the feed gave no usable market cap or 24h volume, so no thinness bound exists "
                    "at all -- this is NOT a passed check"
                    if finding is None
                    else finding.message
                ),
            }
        )
    return rows


def pricing_panel(now_unix: float | None = None) -> dict:
    """What the quote path last fetched, read out of the cache. FETCHES NOTHING.

    The panel answers three questions an operator has had to read source to
    answer: which feed is actually answering from this host, how old the number
    is, and whether any asset is thin enough that its spot price is not a price.

    WHY IT READS THE CACHE RATHER THAN ASKING. routes/admin.py's header sets out
    the rule this follows: /admin renders from the database and the
    configuration, and every network call is a separate deliberate route,
    because a page whose load time is the sum of remote timeouts is the blinking
    cursor rule 14 opens with. A pricing panel that fetched would also make the
    whole page -- swaps in flight, stuck payouts, worker state -- fail on the day
    a price API went down, which is exactly the wrong dependency for the screen
    an operator opens when something is wrong.

    It is also the more honest number. The cache is what create_quote() priced
    from, so this is the figure a customer was quoted against, not a fresh one
    that nothing used.

    A COLD CACHE IS A RESULT AND SAYS SO. `cached` False with `assets` empty
    means nothing has been priced since this process started -- which on a
    freshly restarted app is the ordinary case, and is a different fact from
    "every asset came back unpriced" (rule 14). The template renders the
    difference.
    """
    from .pricing import (  # noqa: PLC0415 -- checked: same reason as pricing_rows() -- services.pricing imports `requests` at module scope, and the database-only readings on this page must not need it.
        cache_state,
        cached_market_context,
    )

    now = time() if now_unix is None else now_unix
    state = cache_state()
    snapshots = cached_market_context()
    fetched_at = state["fetched_at"]
    expires_at = state["expires_at"]
    return {
        "cached": state["cached"],
        "source": state["source"] or None,
        "fetched_at": fetched_at,
        "age": None if fetched_at is None else format_duration(max(0.0, now - fetched_at)),
        # The TTL that actually applied, recovered from the two stamps, because
        # the cache does not store it -- it is whatever the last caller passed.
        "ttl": None if fetched_at is None or expires_at is None else format_duration(expires_at - fetched_at),
        "expired": None if expires_at is None else now >= expires_at,
        "assets": pricing_rows(snapshots),
        "peg": _PEG_NOT_ON_RENDER,
    }


def probe_peg() -> dict:
    """Price USDC and USDT and report whether this page's dollar is a dollar.

    THE SECOND AND LAST FUNCTION IN THIS MODULE THAT OPENS A SOCKET, and like
    probe_chain() it is called only from its own explicit route and never on a
    page render. Two reads, no amount, no address, no key.

    The findings come from services/wallet_leveling.peg_findings(), which is the
    same function `chain_balances.py --level` prints -- so the web surface and
    the CLI cannot disagree about whether the peg holds (rule 8). What is NOT
    shared is the price fetch: --level prices every wallet asset, and this needs
    the two stablecoins only.

    Never raises. A feed that will not answer is reported in the return value as
    suspect=True with the reason in the findings, which is what peg_findings()
    already does for an absent price -- "unchecked" and "fine" must not render
    the same way.
    """
    from decimal import (  # noqa: PLC0415 -- checked: local to keep this module's import surface to what the database rows need, matching pricing_panel() above.
        Decimal,
    )

    from .coinpaprika import (  # noqa: PLC0415 -- checked: imports `requests` transitively; see pricing_rows().
        fetch_quote,
    )
    from .wallet_leveling import PEG_ASSETS, peg_findings  # noqa: PLC0415 -- checked: same.

    prices, failures = {}, []
    for asset in PEG_ASSETS:
        try:
            prices[asset] = Decimal(str(fetch_quote(asset).price_usd))
        except Exception as exc:  # noqa: BLE001 -- checked: one stablecoin failing must not lose the other, and the caller CAN tell the failure from an answer because the asset is absent from `prices` and peg_findings() reports it as NOT PRICED rather than as on peg. The reason is carried in `failures` so it is not swallowed.
            failures.append(f"{asset}: {type(exc).__name__}: {exc}")
    suspect, findings = peg_findings(prices)
    return {
        "probed_at": utc_now_iso(),
        # Echo what decided the answer (rule 14): asking for two and pricing
        # zero, and asking for two and pricing two that disagree, are different
        # failures and a bare findings list does not distinguish them.
        "assets_asked": list(PEG_ASSETS),
        "assets_priced": sorted(prices),
        "suspect": suspect,
        "findings": findings,
        "fetch_failures": failures,
    }


def overview(db, config, adapters: dict, now_iso: str | None = None, run_dir=None) -> dict:
    """Everything the admin page shows, in one read-only pass.

    Takes `now_iso` so every freshness verdict on one render is measured against
    ONE clock reading. Computing the time per row would let two rows on the same
    screen disagree about what "now" is, which is a small thing that makes a
    staleness table impossible to reason about.

    STILL CONTACTS NOTHING. pricing_panel() reads the price cache and does not
    fetch; probe_chains() and probe_peg() are the only network calls in this
    module and neither is reachable from here.
    """
    now = utc_now_iso() if now_iso is None else now_iso
    _pairs = pair_rows(config, adapters)
    return {
        "generated_at": now,
        # The tab strip's entries, from the one ordered list above. Passed rather
        # than imported in the template so the template holds no name of its own.
        "sections": PAGE_SECTIONS,
        "database": config.get("DB_PATH"),
        "config": config_echo(config),
        "status_counts": status_counts(db),
        "in_flight": swaps_in_flight(db, now),
        "deposits": recent_deposits(db, now),
        "payouts": payout_rows(db),
        "unresolved_payouts": unresolved_payouts(db),
        "inventory": inventory_rows(db, now),
        # The desk's own fee, beside the wallet balance it is sitting inside. See
        # retained_fee_rows() for why this belongs on this page and nowhere else on
        # it.
        "retained_fees": retained_fee_rows(db),
        "transitions": recent_transitions(db),
        # ONE CALL, THREE SHAPES. The matrix and the asset list are INDEXES over
        # these rows, not second derivations of them -- see pair_matrix(). `pairs`
        # itself is kept because /admin's own note counts it, operator_panel.py's
        # teller pane reads it as a flat list, and a matrix is the wrong shape for
        # both.
        "pairs": _pairs,
        "pair_matrix": pair_matrix(_pairs),
        "pair_assets": pair_assets(_pairs),
        # A FOURTH SHAPE OVER THE SAME ROWS, and the same argument as the other two:
        # services/pair_view.asset_rollups() is an INDEX over `_pairs`, not a second
        # derivation of serviceability. This module's header records what a second
        # derivation cost -- pair_rows() once carried the first of three conditions and
        # told the operator a pair was ENABLED that the customer page was refusing in
        # the same process -- so the lamp a this page shows and the lamp the customer
        # page shows are the same function over the same rows.
        #
        # THE CUSTOMER PAGE AND THIS ONE DIFFER IN WHAT THEY SHOW, NOT IN WHAT THEY
        # KNOW, which is the distinction /admin's own header draws about audience. The
        # colour and the out/in counts are identical; this page keeps the matrix beneath
        # them, because an operator needs the per-cell reason and a customer does not.
        "asset_lamps": asset_rollups(_pairs),
        "asset_lamp_states": ASSET_ROLLUP_STATES,
        "chains": chain_rows(config, adapters),
        "workers": worker_rows(run_dir),
        "pricing": pricing_panel(),
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
    # NAMED AND UNNAMED ARE DIFFERENT ANSWERS AND RENDER DIFFERENTLY (rule 14).
    # `network` carries a name or None -- never a sentence about why there is no
    # name, which is the shape that put "it reports its network as answered, but
    # reported no chain name" on the operator's screen.
    named = is_named(network)
    return {
        "asset": asset,
        "probed": True,
        "reachable": True,
        "network": network if named else None,
        "detail": (
            f"answered; it reports its network as {network}"
            if named
            else f"answered, but would not name its network -- {network}. Reachable is not the "
            f"question this probe exists to answer: an alias pointing at mainnet reads the same "
            f"as one pointing at testnet until a daemon says which"
        ),
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
    """Name this daemon's network. RAISES when it cannot be reached at all.

    THIS USED TO BE A SECOND IMPLEMENTATION OF chains/daemon_network.chain_network()
    and it was the weaker one, which showed up on the operator's screen on
    2026-09-30. It asked `getblockchaininfo` only, and Gridcoin's answer HAS NO
    `chain` KEY -- so on the one chain that host had configured, this returned
    the sentence "answered, but reported no chain name" and probe_chain()
    interpolated it into "it reports its network as answered, but reported no
    chain name". A sentence rendered as a name.

    That is not a cosmetic defect. probe_chain()'s own docstring says an
    operator whose "testnet" alias points at mainnet must READ THE WORD MAINNET
    here, and for GRC this could never print either word. chain_network() falls
    back to `getinfo.testnet` -- a boolean on the older build Gridcoin is -- and
    names it. atomic_swap.py:435 already carried the same finding in a comment;
    this is the third place that knowledge lived and the second that acted on it.

    TWO CALLS NOW, NOT ONE, AND THE PROBE SAYS SO. The reachability read is kept
    separate and first, because chain_network() never raises: folding the two
    together would turn "this daemon did not answer" into "it answered and its
    network is unknown", which are different facts and only one of them means
    a daemon is down.
    """
    if probe_kind(adapter) == "network_method":
        return str(adapter.network())
    # Reachability, and it is this call raising that reports a chain as down.
    adapter.call(_PROBE_METHOD)
    return chain_network(adapter)


def probe_chains(adapters: dict, assets: tuple[str, ...] | None = None) -> list[dict]:
    """Probe every configured chain, read-only. Never raises.

    One call for an adapter with its own network(); up to two for a
    Bitcoin-style daemon -- the reachability read, then the naming read that
    older builds need. See _ask_network().
    """
    names = tuple(sorted(adapters)) if assets is None else assets
    return [probe_chain(asset, adapters.get(asset)) for asset in names]
