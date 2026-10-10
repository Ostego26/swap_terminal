"""Shared scaffolding for the three polling workers.

Role: submodule (adapter construction, config snapshot, and the one
      implementation of the workers' announce/progress/shutdown behavior)
Reads: config.Config -- RPC endpoints, database path, confirmation thresholds
Writes: stdout only
Can move funds: no -- it CONSTRUCTS the adapters that can, but calls nothing on
      them. build_adapters() opens no socket; RPCAdapter.__init__ only stores
      credentials and computes a URL.
Mainnet-safe: yes

WHY THE REPORTING LIVES HERE AND NOT IN EACH WORKER.

Rule 8: two copies of one rule is a bug with a delay on it. Three workers
printing their own status lines would agree on the day they were written and
drift from then on, and the drift is invisible -- each file looks right on its
own. There is one announce_start() and one cycle_line(), so a change to what
the operator sees is a change in one place.

Rule 14 is what they implement, and the four clauses that matter here are:

  announce before, not only after   -- the banner names the database, every
      chain endpoint and the poll interval BEFORE the first cycle, because a
      line that only appears on completion is invisible during the wait, which
      is exactly when it is needed.
  make "did nothing" look different from "did work"   -- cycle_line() prints
      IDLE or WORKED as a distinct token. A poll that found nothing and a poll
      that paid someone must not share a success line.
  state what the number means, next to the number   -- the counts carry their
      own interpretation, because the operator reads the screen, not the
      source.
  never print a secret   -- nothing here ever formats Config.RPC's `user` or
      `password`, and nothing prints an HTLC preimage. Endpoints and wallet
      names only.

SHUTDOWN, AND WHY IT IS NOT THE DEFAULT ONE.

install_stop_handler() makes SIGTERM and SIGINT request a stop at the TOP of
the next cycle rather than killing the process where it stands. The default
disposition would let a SIGTERM land between `sendtoaddress` returning a txid
and the UPDATE that records it -- money on chain, no row in the database, which
is the specific failure rule 14's opening paragraph names. Finishing the
current cycle costs at most one poll interval and removes that window.

supervisor.py is the reaper for all three workers (rule 13). It sends SIGTERM,
waits, escalates to SIGKILL, and then proves the process is gone by asking the
operating system rather than by reading the kill's exit code. The escalation is
the backstop for a cycle that wedges; it does not replace the handler.
"""

import os
import signal
import sqlite3
import time
from collections.abc import Callable
from pathlib import Path

from chains.registry import build_adapters, missing_settings
from chains.solana import SolanaAdapter
from chains.xrp import XRPAdapter
from config import (
    ENV_SET,
    ENV_SET_BUT_EMPTY,
    ENV_UNSET,
    Config,
    bitcoin_family_rpc,
    env_variable_state,
)
from db import db_session
from log_setup import configure_logging
from microfortnights import format_duration
from network_target import CHAIN_PORTS, classify, configuring_variable
from services.custody_separation import wallet_label
from services.deposit_service import ACTIVE_STATUSES
from supervisor import CODE_STALE, worker_code_freshness


def build_adapters_from_config() -> dict:
    """The workers' entry into chains/registry.build_adapters().

    A one-line wrapper rather than a second implementation. Until 2026-09-25
    this function WAS the second implementation -- six lines here and six more
    in app.py, differing only in where the RPC mapping came from -- which is
    CLAUDE.md rule 8's "bug with a delay on it", and a fourth chain is exactly
    when that delay expires.

    Named differently from the shared one it calls so that `from
    chains.registry import build_adapters` and this can coexist in one module
    without either shadowing the other.
    """
    return build_adapters(Config.RPC)


def get_config_dict() -> dict:
    return {k: getattr(Config, k) for k in dir(Config) if k.isupper()}


def endpoint_lines() -> list[str]:
    """One line per chain: where it is and how many blocks it must wait.

    Deliberately prints host, port, wallet name and confirmation count, and
    deliberately never prints `user` or `password` -- those are in the same
    dict, one key away, and a status line that helpfully echoed them would put
    wallet credentials into every log this worker writes.

    The confirmation figure is a COUNT OF BLOCKS and is never rendered in
    microfortnights (rule 6). Six confirmations is six confirmations; a block
    is not 1.2096 seconds long.
    """
    lines = []
    for asset in ("BTC", "LTC", "GRC"):
        # bitcoin_family_rpc() RATHER THAN Config.RPC[asset], 2026-10-09. The
        # subscript works today only because this tuple is a literal a checker can
        # read, and that is the fragile half: the tuple is INLINE here -- one of the
        # five places ("BTC", "LTC", "GRC") is spelled, see config.RpcSettings for
        # the list -- so a chain added to it is one edit away from `rpc["host"]` and
        # `rpc["port"]` on an entry that has neither. SOL, XRP and ICP all lack both,
        # and the two lines they get instead are appended separately below for
        # exactly that reason. The accessor refuses by name and says which shapes
        # exist, rather than raising KeyError('host') out of a startup banner.
        rpc = bitcoin_family_rpc(asset)
        # wallet_label() RATHER THAN `or "(default wallet)"`, 2026-10-03. This line
        # and services/admin_view._endpoint_text() spelled that same phrase for the
        # same question (rule 8's two copies), and both said something true that
        # told the operator nothing: an empty wallet value means this endpoint has
        # no /wallet/<name> path, so the daemon routes to whichever wallet it
        # serves by default -- the one a bare CLI call reaches. On the operator's
        # host that is why a 500 GRC customer deposit on 2026-10-03 moved only the
        # 0.001 fee: the desk's deposit address and the operator's own coins were
        # in one wallet, and the banner line that could have said so said
        # "(default wallet)".
        # NO str() AND NO `or ""`, 2026-10-09: config.BitcoinFamilyRpc declares
        # `wallet` a `str` and config.py builds it with _env("<ASSET>_RPC_WALLET",
        # ""), which returns "" for unset, empty or whitespace-only. The wrapper was
        # getting an `object` past wallet_label()'s `str` parameter and not guarding
        # anything -- unlike the two `.get("wallet") or ""` reads in swap_readiness
        # and wallet_custody, which are reached with test-seeded partial entries and
        # keep theirs. This one reads the table config.py built.
        wallet = wallet_label(asset, rpc["wallet"])
        confirmations = getattr(Config, f"{asset}_MIN_CONFIRMATIONS")
        # AN UNCONFIGURED CHAIN SAYS SO, matching what SOL and XRP say below.
        #
        # This printed `rpc=127.0.0.1:0` until 2026-09-26, and it was a regression
        # from the same day's change that made these three default to
        # UNCONFIGURED_PORT instead of a mainnet port. Before that a port always
        # had a real value, so interpolating it was safe; afterwards, `:0` rendered
        # an unconfigured chain as a configured one -- a reader would see three
        # chains with an endpoint and three marked "not configured", when in fact
        # all six were unconfigured.
        #
        # Worse than cosmetic, because chains/registry.py SKIPS a port-0 chain, so
        # the banner was naming adapters that do not exist. Rule 14's "make 'did
        # nothing' look different from 'did work'", and rule 16's "a wrong comment
        # is a bug" applied to a line of output.
        # EVERY SETTING, not just the port, since 2026-09-26. The paragraph above is
        # about a banner naming adapters that do not exist, and testing the port
        # alone reintroduced it one setting over: chains/registry.build_adapters()
        # now also requires the RPC user and password (an empty pair is a guaranteed
        # 401 -- chains/base.py authenticates with auth=(user, password) and has no
        # cookie path), so a chain with a port and no password has no adapter while
        # this line would have printed `rpc=127.0.0.1:25715` for it.
        #
        # missing_settings() is the same function the page and create_swap() use, so
        # the three cannot disagree about what "configured" means.
        missing = missing_settings(Config.RPC, asset)
        if missing:
            lines.append(
                f"  {asset}  not configured ({', '.join(missing)} unset)  <- no {asset} "
                f"adapter is constructed; test chain is {CHAIN_PORTS[asset].test_hint}"
            )
            continue
        lines.append(
            f"  {asset}  rpc={rpc['host']}:{rpc['port']} wallet={wallet} min_confirmations={confirmations} blocks"
            f"  <- {chain_verdict(asset, rpc['port'])}"
        )
    # SOL IS APPENDED SEPARATELY AND SAYS A DIFFERENT THING, because it IS a
    # different thing. The loop above prints `min_confirmations=N blocks`; a
    # Solana deposit has no block count and its threshold is a rung on a
    # commitment ladder (chains/solana_units.py). Printing it through the same
    # f-string would produce "min_confirmations=3 blocks", which is a sentence
    # that is not true and that an operator would read all morning without
    # noticing -- CLAUDE.md rule 6's unit laundering, arriving as a status line
    # rather than as a number.
    #
    # It appears ONLY when configured, for the same reason chains/registry.py
    # only constructs it then: a banner line for a chain nobody set up is
    # noise, and "(none)" is reserved for a result rather than an absence of
    # configuration.
    if Config.RPC.get("SOL", {}).get("url"):
        lines.append(SolanaAdapter(**Config.RPC["SOL"]).endpoint_line())
    else:
        lines.append(
            f"  SOL  not configured ({configuring_variable('SOL')} unset)  <- no Solana adapter "
            f"is constructed; no SOL pair is allowed"
        )

    # XRP, and its threshold is neither blocks nor a commitment rank -- it is
    # a validated-ledger boolean. Its own line so the unit is stated (rule 6).
    if Config.RPC.get("XRP", {}).get("url"):
        lines.append(XRPAdapter(**Config.RPC["XRP"]).endpoint_line())
    else:
        lines.append(
            f"  XRP  not configured ({configuring_variable('XRP')} unset)  <- no XRP adapter "
            f"is constructed; no XRP pair is allowed"
        )

    return lines


def chain_verdict(asset: str, port: int) -> str:
    """Which network a configured port belongs to, for the startup banner.

    A bare `rpc=127.0.0.1:15715` requires the reader to know Gridcoin's port
    table. Rule 14 asks output to say what a number MEANS next to the number, and
    on a worker that is about to watch for real deposits, "which chain" is the
    number that matters most. network_target.classify() is the one place that
    decides it (rule 11), so this does not re-derive it.
    """
    verdict = classify(asset, port)
    if verdict == "MAINNET":
        return "*** MAINNET, REAL MONEY ***"
    if verdict == "TEST":
        return f"test chain (mainnet is {CHAIN_PORTS[asset].mainnet_port})"
    return f"UNRECOGNIZED port, so which chain this is was NOT established; mainnet is {CHAIN_PORTS[asset].mainnet_port}"


def database_census(db_path: str) -> str:
    """What is actually IN the database this worker just opened. One line.

    WHY THIS LINE EXISTS, AND IT COST AN HOUR OF THE OPERATOR'S EVENING ON
    2026-10-01.

    Three workers were started from a shell whose SWAP_DB_PATH pointed at the
    wrong file -- repo_root/runtime/swap_terminal.db instead of
    repo_root/swap_terminal/swap_terminal.db, a path I put in a block I handed
    them. They then ran for an hour printing

        deposit_watcher cycle=57 IDLE  active_swaps=0 refreshed=0
        now_payout_pending=0 HALTED_for_review=0

    while THREE swaps sat in awaiting_deposit in the database every root tool
    reads. Nothing was wrong with any worker. Nothing failed. The banner even
    printed the path it was using, correctly, which is what finally identified it
    -- and that was not enough, because a path is only wrong RELATIVE to what you
    expected, and the cycle line beside it said `active_swaps=0 is expected only
    when no swap is open`, which was true of the file it was reading and false of
    the system.

    So this is rule 13's defect in its exact stated form -- "treat 'skipped' plus
    'success' in the same output as a defect in the output" -- and rule 14's "make
    did-nothing look different from did-work", at the level of a whole database: a
    terminal pointed at an empty file is INDISTINGUISHABLE from a quiet one, and
    the second is the normal state, so the first reads as normal.

    AND connect_db() CREATES WHAT IT CANNOT FIND. db.py:561 is a bare
    sqlite3.connect(), which makes a missing file rather than refusing, and no
    worker applies SCHEMA. So a typo in a path does not fail: it manufactures an
    empty database and polls it forever. The file at the wrong path even had a
    schema, because the desktop launcher had passed the same bad value to
    create_app(), which calls init_db().

    WHAT IT REPORTS, and why a count rather than a verdict. There is no way from
    inside one worker to know which database is the RIGHT one -- that is the
    operator's intent, not a fact in the tree (rule 17). What a worker can say is
    what it found, so the reader can compare it against what they expect:

      no such file, created now   the strongest signal, and the one that would
                                  have caught this in a second
      a file with no `swaps`      created but never initialized. NOT the same
      table                       answer as an empty table, which is the
                                  distinction show_swap.py and open_swap.py both
                                  make for the same reason
      a tally per status          so three awaiting_deposit in the file the
                                  operator means and zero here is visible on
                                  one line
    """
    path = Path(db_path)
    # exists() BEFORE connecting, because connect() would create it and then this
    # line could never report the one state most worth reporting.
    if not path.exists():
        return (
            "DOES NOT EXIST YET. sqlite3.connect() CREATES a missing file and no worker applies the "
            "schema, so this worker is about to poll a database it manufactured. If you expected swaps "
            "here, SWAP_DB_PATH is pointing somewhere you did not mean"
        )
    try:
        with db_session(db_path) as db:
            rows = db.execute(
                "SELECT status, COUNT(*) AS swaps FROM swaps GROUP BY status ORDER BY status"
            ).fetchall()
    except sqlite3.OperationalError as error:
        # NAMED, not broad. This is what a file created but never initialized
        # raises ("no such table: swaps"), and it must never render the same way as
        # a healthy empty table -- that confusion is the whole failure above.
        return (
            f"EXISTS BUT HAS NO SWAPS TABLE ({error}). It was created and never initialized; the web app "
            f"applies the schema on first run. Every cycle below will fail until then"
        )
    if not rows:
        return (
            "exists, schema present, and holds NO SWAPS AT ALL  <- every cycle will report IDLE and that "
            "will be correct for THIS file. If you expected swaps, compare this path against the one your "
            "other tools print"
        )
    tally = ", ".join(f"{row['status']} {row['swaps']}" for row in rows)
    return f"{tally}  <- what is in THIS file. active_swaps counts only {', '.join(ACTIVE_STATUSES)}"


def announce_start(worker_name: str, poll_seconds: float, pid: int) -> None:
    """Print what this worker is about to do, before it does any of it."""
    # FIRST, before the banner, so that anything the banner itself logs is captured.
    # Here rather than in each worker's main() deliberately: all three already call
    # announce_start() as their first statement, and three call sites for one
    # startup step is three chances for a new worker to omit it -- which would
    # reintroduce exactly the silence this fixes, in the one worker nobody checked.
    configure_logging()
    print(f"{worker_name}: starting", flush=True)
    print(f"  pid             {pid}  <- supervisor.py stop reads this from runtime/{worker_name}.pid", flush=True)
    print(f"  database        {Config.DB_PATH}  <- {db_path_source()}", flush=True)
    print(f"  it holds        {database_census(str(Config.DB_PATH))}", flush=True)
    print(f"  poll interval   {format_duration(poll_seconds)}", flush=True)
    print("  chains:", flush=True)
    for line in endpoint_lines():
        print(line, flush=True)
    print(
        "  network         NOT VERIFIED from configuration -- a port is the default for a network, not proof "
        "of one. Ask the daemon before trusting it.",
        flush=True,
    )
    print("  shutdown        SIGTERM/SIGINT finish the current cycle, then exit", flush=True)


# Counts that report a STANDING CONDITION rather than work the cycle did.
#
# MEASURED 2026-09-26, the day after deposit_watcher gained HALTED_for_review.
# That count is a total rather than a delta on purpose -- a halted swap has to
# stay on screen for as long as it is halted, because a delta shows the
# transition once and then reads as zero forever. But cycle_line() treated every
# count as evidence of work, so one halted swap made EVERY cycle print:
#
#     deposit_watcher cycle=4 WORKED active_swaps=0 refreshed=0
#     now_payout_pending=0 HALTED_for_review=1
#
# WORKED, with every count that describes work at zero, once every fifteen
# seconds, for as long as the swap sat there. That is "skipped plus success in
# the same output", which this project's rules call a defect in the OUTPUT
# rather than a cosmetic complaint -- and it was happening on exactly the cycles
# somebody was reading because something was wrong.
#
# Keyed by the COUNT's name rather than by the worker's, because the property
# belongs to the count: a standing total is standing whoever prints it, so a
# second worker reporting the same field gets the same treatment without anybody
# remembering to ask for it. The one spelling here and the one in
# workers/deposit_watcher.py's counts dict are pinned to each other by
# tests/test_show_swap.py.
#
# `failed_total` JOINED 2026-10-01, AND THE PARAGRAPH ABOVE PREDICTED IT WRONGLY.
# "A second worker reporting the same FIELD gets the same treatment without
# anybody remembering to ask for it" is true and was not enough: payout_worker
# reports a different field with the identical property, and nobody remembered.
#
# Measured from the operator's own log while they were between steps of a devnet
# SOL -> testnet GRC rehearsal. Three swaps had failed earlier in the week, so:
#
#     payout_worker cycle=14 WORKED pending_at_start=0 broadcast=0 failed_total=3
#     in 0.0µfn (0.0s)  <- ... failed_total is cumulative, not this cycle
#
# WORKED, with both counts that describe work at zero, every ten seconds,
# forever -- because `failed_total` is a cumulative total of every payout that
# has ever failed and cycle_line() read any non-zero count as evidence of work.
# The line's own note says "failed_total is cumulative, not this cycle", so the
# worker was explaining in prose why the marker beside it was wrong.
#
# It is strictly worse than the HALTED_for_review case it mirrors, because
# `failed_total` NEVER returns to zero. A halted swap gets resolved and the
# deposit watcher goes back to printing IDLE; a failed payout is permanent, so
# this one latched the first time any payout failed and could never unlatch. On
# this host that was 2026-09-26, five days before anybody noticed.
#
# `inventory_rows` AND `refreshed_swaps` JOINED 2026-10-02, and they are the third and
# fourth fields with the identical property -- which finally settles that this is a
# PROPERTY OF THE FIELD and not a list of two exceptions. Both are reconcile_worker's,
# and between them they meant that worker could NEVER print IDLE:
#
#     reconcile_worker cycle=N WORKED refreshed_swaps=3 inventory_rows=2 in ...
#
# `inventory_rows` is how many wallet_inventory rows exist after the refresh, so on a
# host where any adapter answers it is non-zero on every cycle forever. `refreshed_swaps`
# is how many swaps are OPEN, not how many this cycle moved -- and reconcile_worker is
# the SECOND loop to refresh them (deposit_watcher does the same work at 15s, see that
# file's header), so on a healthy host it re-reads swaps the watcher has already
# advanced and reports WORKED for doing nothing. That is the case rule 13 names
# directly: "treat 'skipped' plus 'success' in the same output as a defect in the
# output", and it is worse here than in the two cases above, because this is the worker
# an operator would look at to find out whether the BACKSTOP is carrying the load.
#
# What reconcile_worker now reports as work is `transitions_written` -- the audit rows
# its own pass wrote -- and that figure is zero exactly when deposit_watcher is healthy.
#
# `late_unresolved` JOINED 2026-10-04 and it is the `failed_total` shape exactly, which
# is why it was put here in the same commit that added the field rather than after
# somebody watched it latch. It counts late_deposits rows a PERSON has not resolved yet
# (services/late_deposit_service.py), so it stays non-zero from the moment the first late
# deposit is recorded until the operator writes a resolution -- days, plausibly. Without
# this line reconcile_worker would print WORKED on every cycle of that period for doing
# nothing at all, and the `late_deposits` field beside it -- which counts rows NEW this
# pass and returns to 0 -- is the one that genuinely means this cycle found something.
# Both are on the line because an operator needs "one arrived just now" and "four are
# still sitting there" to be different numbers; only the first may claim work.
STANDING_COUNTS = frozenset({
    "HALTED_for_review", "failed_total", "inventory_rows", "late_unresolved",
    "refreshed_swaps",
})


class CycleFailures:
    """One worker's run of consecutive failed cycles, and the line each one prints.

    WHY THIS EXISTS, MEASURED 2026-10-01 ON THE OPERATOR'S HOST. A devnet DNS
    lookup failed for a moment:

        SolanaRPCError: getSignaturesForAddress could not reach
        https://api.devnet.solana.com: Failed to resolve
        'api.devnet.solana.com' ([Errno -2] Name or service not known)

    and the deposit watcher DIED. Counted at the time: zero `try` and zero
    `except` in any of the three workers, so any exception from any chain
    terminated the process. The operator found it with
    `supervisor.py status` -- "pid file is stale; process is gone" -- half an
    hour and one uncredited deposit later.

    THE DAMAGE IS RULE 13'S, INVERTED. An orphan holds a lock and everything
    downstream reports success. Here the process was GONE and everything
    downstream reported success: payout_worker went on printing
    `IDLE pending_at_start=0` every ten seconds, which is exactly what it prints
    when there is genuinely nothing to pay. Nothing anywhere said "deposits are
    no longer being credited". A customer's money would arrive, confirm, and sit.

    AND IT TOOK EVERY CHAIN DOWN, NOT ONE. The Solana endpoint was unreachable;
    BTC, LTC, GRC and XRP deposits stopped being credited too, because they
    share the loop that died.

    SO A FAILED CYCLE IS REPORTED AND THE LOOP CONTINUES. A worker that keeps
    running and says FAILED every cycle is strictly better than one that is gone:
    the operator can see it, `supervisor.py status` still finds it, and a chain
    that comes back is picked up on the next poll with no intervention.

    THE CONSECUTIVE COUNT IS THE POINT OF THE CLASS. One failed cycle is a blip
    and the next poll fixes it; two hundred is an outage or a bug, and the two
    must not read the same. Rule 14's "state what the number means, next to the
    number" -- a bare FAILED repeated forever is the cried-wolf shape that gets
    ignored, and `failed_cycles_in_a_row=203` is not.
    """

    #: Characters of the exception text kept on the cycle line. The full text
    #: goes to the log via logger.exception(); this is the one-line summary an
    #: operator skims, and a DNS error's useful part is at the front.
    REASON_ON_LINE = 300

    def __init__(self, worker_name: str) -> None:
        self.worker_name = worker_name
        self.consecutive = 0

    def clear(self) -> None:
        """A cycle succeeded. Called on the success path, so the count means what it says."""
        self.consecutive = 0

    def record(self, cycle: int, seconds: float, error: BaseException) -> str:
        """Count a failed cycle and render its line. Does NOT print or log.

        Returning the string rather than printing it keeps this testable without
        capturing stdout, and leaves the caller owning its own output stream --
        the same split cycle_line() already uses.
        """
        self.consecutive += 1
        reason = str(error)[: self.REASON_ON_LINE] or error.__class__.__name__
        return (
            f"{self.worker_name} cycle={cycle} FAILED {error.__class__.__name__}: {reason} "
            f"in {format_duration(seconds)}  <- THIS CYCLE DID NO WORK and credited nothing. "
            f"failed_cycles_in_a_row={self.consecutive}. The worker is still running and will try "
            f"again at the next poll; one failure is usually a chain being briefly unreachable, and "
            f"a count that keeps climbing is an outage or a bug. Nothing was lost -- a deposit "
            f"already on chain is credited whenever a cycle next succeeds."
        )


def stale_code_note(pid: int | None = None) -> str:
    """"This worker is running code older than the tree", on the line the operator reads.

    EMPTY STRING WHEN THERE IS NOTHING TO SAY, so a healthy cycle line is exactly
    what it was before this function existed.

    WHY IT IS HERE AND NOT ONLY IN `supervisor.py status`. supervisor.py has
    detected this since 2026-10-03 and says so loudly -- `*** STALE CODE ***`,
    naming the newest file and the gap. Measured on the operator's host
    2026-10-05: all three workers had been running code 46029.9µfn (55677.8s)
    older than the tree, about FIFTEEN AND A HALF HOURS, including the window in
    which supervisor.py's own double-spawn defect was fixed. The detection worked
    perfectly and nobody saw it, because `status` is a command somebody has to
    decide to run and the worker log is what an operator actually tails.

    So this is rule 14's "say what you are doing while you do it" applied to a
    fact that only becomes true AFTER the startup banner has scrolled away: a
    worker cannot know at exec that a file will be edited at noon, and
    announce_start() has already printed by then.

    IT DECIDES NOTHING AND SIGNALS NOTHING, which is the policy supervisor.py's
    own header sets out and this does not get to change: restarting a payout
    worker is a live-posture action (rule 16), and a worker that exited on its own
    reading of a file mtime would be deciding when money stops moving on the
    strength of a timestamp. It prints a sentence.

    COST, MEASURED 2026-10-05 before wiring it in: newest_code_file() walks 125
    .py files in 1.2ms mean over ten runs (min 1.1, max 1.4). Against the
    deposit watcher's 15s poll that is 0.008% of a cycle, so it runs every cycle
    with no throttle and no cache -- a cache would need invalidating, which is
    more machinery than the thing it saves.
    """
    freshness = worker_code_freshness(os.getpid() if pid is None else pid)
    if freshness["verdict"] != CODE_STALE:
        return ""
    return (
        "*** STALE CODE *** this worker predates the tree: "
        f"{freshness['reason']}. newest={freshness['newest_path']}. "
        "`supervisor.py restart` reloads it; `start` alone will NOT"
    )


def cycle_line(worker_name: str, cycle: int, seconds: float, counts: dict[str, int], notes: str = "") -> str:
    """Render one cycle's result so that idle and productive cycles differ.

    Rule 14: "a poll that found nothing and a poll that paid someone must not
    share a success line." The IDLE/WORKED token is that difference, and it is
    the first thing on the line so it survives being skimmed.

    A count named in STANDING_COUNTS still prints and still reads non-zero; it
    just does not let the cycle claim it worked. See that constant for the
    measurement behind it.

    THE STALENESS NOTE IS APPENDED HERE, which is the one place all three workers
    render this line -- so they all gain it without three edits to three files on
    the order path (rule 8). It appends nothing at all when the code is current,
    so an existing healthy line is byte-identical to what it was.
    """
    did_work = any(value for key, value in counts.items() if key not in STANDING_COUNTS)
    marker = "WORKED" if did_work else "IDLE  "
    rendered = " ".join(f"{key}={value}" for key, value in counts.items()) or "(none)"
    line = f"{worker_name} cycle={cycle} {marker} {rendered} in {format_duration(seconds)}"
    if notes:
        line = f"{line}  <- {notes}"
    stale = stale_code_note()
    return f"{line}\n  {stale}" if stale else line


# The repository root, derived from this file's own location rather than from a
# working directory. swap_terminal/workers/common.py -> workers -> swap_terminal
# -> the root, which is where the operator's entry points live (rule 10).
REPO_ROOT = Path(__file__).resolve().parents[2]

# What a pasted command calls the interpreter.
#
# "python3" rather than sys.executable, and that is a decision rather than a
# shortcut. sys.executable is the interpreter THIS PROCESS was started with --
# under supervisor.py that is whatever launched the supervisor, which may be a
# virtualenv path that is not on the operator's PATH and is not what README.md
# tells them to type. The operator pastes this into their own shell, so it says
# what their shell needs.
PASTEABLE_INTERPRETER = "python3"


def root_tool_command(script: str, *arguments: str) -> str:
    """`python3 <repo root>/<script> <args>` -- a command that pastes from anywhere.

    THE PATH IS ABSOLUTE ON PURPOSE, AND IT WAS MEASURED (rule 17). supervisor.py
    starts every worker with `cwd=supervisor.BASE_DIR`, which is `swap_terminal/`
    -- the package directory, NOT the repository root where the entry points are.
    So a worker printing `python3 show_swap.py` would be naming a file that does
    not exist relative to the directory that process is in, and the operator's own
    shell may be somewhere else again. An absolute path resolves from anywhere,
    which is the only property that matters for a line whose whole job is to be
    copied.

    Rule 14's "the operator reads the screen, not the source": a hint that cannot
    be pasted is silence one step removed, which is exactly the defect this
    function was added to fix -- workers/deposit_watcher.py used to end its halt
    note with `query swaps WHERE status='under_review'`, a SQL fragment handed to
    somebody sitting in a shell with nothing to run it in.

    No existence check here. A command naming a script that is not on disk is a
    deploy defect and it belongs to a test rather than to a runtime branch --
    tests/test_show_swap.py asserts that the file the deposit watcher's note names
    is really there, which catches a rename that no import graph would see
    (rule 2).
    """
    return " ".join([PASTEABLE_INTERPRETER, str(REPO_ROOT / script), *arguments])


def install_stop_handler() -> Callable[[], bool]:
    """Ask for a clean stop on SIGTERM/SIGINT; return a should_stop() predicate.

    The flag is checked at the TOP of the loop, so a signal arriving mid-cycle
    lets the cycle finish. See this module's docstring for why that window
    matters on the payout path.
    """
    state = {"stop": False}

    def _request_stop(signum, _frame):
        state["stop"] = True
        print(f"  signal {signal.Signals(signum).name} received: finishing this cycle, then exiting", flush=True)

    signal.signal(signal.SIGTERM, _request_stop)
    signal.signal(signal.SIGINT, _request_stop)
    return lambda: state["stop"]


def sleep_until_next_cycle(poll_seconds: float, should_stop: Callable[[], bool]) -> None:
    """Sleep in short slices so a stop request is honored promptly.

    A single time.sleep(60) would make `supervisor.py stop` wait out the whole
    interval before the worker noticed, and the supervisor would escalate to
    SIGKILL for a worker that was perfectly willing to exit. Seconds here, not
    microfortnights: time.sleep() is an interface, not a report (rule 6).
    """
    deadline = time.monotonic() + poll_seconds
    while time.monotonic() < deadline:
        if should_stop():
            return
        time.sleep(min(0.25, max(0.0, deadline - time.monotonic())))


#: The environment variable every database path in this project comes from, when
#: it comes from the environment at all. Named once here because three root tools
#: print a sentence about it and config.py reads it, and a variable NAME spelled
#: four times is rule 8's shape with a typo waiting in it.
DB_PATH_VARIABLE = "SWAP_DB_PATH"


def db_path_source(explicit_db: str = "") -> str:
    """Where a reported database path actually came from. FOUR answers, not two.

    WHY THIS IS A SHARED FUNCTION AND WHY IT HAS A THIRD CASE, both measured on
    the operator's host 2026-10-01.

    show_fees.py printed, in a shell where SWAP_DB_PATH was not set at all:

        database   .../swap_terminal/swap_terminal.db  <- SWAP_DB_PATH

    The path was right. The provenance was a fabrication: that value came from
    `config.DB_PATH`'s built-in default, and the line asserted it came from an
    environment variable the shell did not have. An operator checking that claim
    against their own `env` finds it disagreeing and has no way to tell which half
    is wrong -- which is the precise failure this project has paid for repeatedly,
    a statement in the register of a measurement (rule 17), here about the one
    parameter that decides every other number in the report.

    It was a two-case function inferring the answer from `db_path != Config.DB_PATH`,
    so "not the default" was read as "--db was passed" and "equal to the default"
    as "SWAP_DB_PATH". Both inferences are wrong in a reachable case: the default
    IS what you get with nothing set, and `--db` pointed at the default value
    reports as the environment.

    THREE COPIES EXISTED, which is why the fix is a move and not an edit:

        show_swap.py::_db_source      the two-case version
        show_fees.py::_db_source      copied from it on 2026-10-01, bug included
        open_swap.py                  labels the path "the SAME file the workers
                                      read (SWAP_DB_PATH)" unconditionally -- the
                                      original defect, never corrected, and the
                                      one show_swap's docstring records being
                                      flagged in September

    `explicit_db` is the flag's own value rather than a comparison, so the first
    case is OBSERVED instead of inferred. It reads os.environ, which is why this
    lives here next to get_config_dict() rather than in report_block.py, whose
    header says it reads nothing and would have been made false by this.

    THE FOURTH ANSWER, 2026-10-10, AND IT IS THIS FUNCTION'S OWN FOUNDING DEFECT
    ONE CASE FURTHER IN. Measured by running it against all four shell states:

        SWAP_DB_PATH in the shell       path reported   this said
        unset entirely                  the default     "IS NOT SET in this shell"
        set to /data/real.db            /data/real.db   "SWAP_DB_PATH"
        SET BUT EMPTY (SWAP_DB_PATH=)   the default     "IS NOT SET in this shell"
        set to whitespace               the default     "IS NOT SET in this shell"

    The PATH is right in all four -- config._env falls back to the default for an
    empty or whitespace value, which is correct and is why no entry point dies. The
    PROVENANCE is a fabrication in two of them: the variable IS set, and the
    sentence says it is not. That is word for word the failure the paragraph at the
    top of this docstring opens with -- "The path was right. The provenance was a
    fabrication... an operator checking that claim against their own `env` finds it
    disagreeing and has no way to tell which half is wrong." A two-case function
    became three and still collapsed two states into one sentence.

    An `export SWAP_DB_PATH=` is not an exotic input. config.py's own comment block
    above _env() lists where empty values come from -- a shell script, a CI template
    with a blank field, a .env line with nothing after the `=` -- and that block
    exists because a single empty variable used to take down every entry point in
    this project.

    THE STATE TEST IS config.env_variable_state() AND NOT AN `if` HERE, because
    four places in this tree already spell "set-but-empty is not unset" their own
    way and one of them (gunicorn.conf.py) spells it wrongly. That function names
    all four. Rule 19: the fourth copy is the defect, not a backlog item.

    `db_path` WAS THE FIRST PARAMETER AND WAS NEVER READ. Removed in the same pass,
    nine call sites updated. It was the vestige of the two-case version this
    docstring describes, which DID compare the path (`db_path != Config.DB_PATH`) --
    and that comparison is the inference recorded above as the bug. Leaving the
    parameter in the signature invited exactly that reading back: a reader sees
    `db_path_source(db_path, explicit_db)` and concludes the path participates in
    the answer. It did not. Worse, nothing stopped a caller passing a path this
    function never looks at and getting a confident provenance claim about it.

    PROVEN UNREAD RATHER THAN ASSUMED (rule 17): an ast.walk over this function
    reported `loaded: ['explicit_db']`, `NEVER READ: ['db_path']`. Ruff cannot catch
    it -- ARG is not in pyproject's selected set, which is rule 12's "two things the
    linter cannot check" arriving by a third route. And no caller could have been
    relying on a comparison: all nine derive the path as `args.db or Config.DB_PATH`
    within two lines of the call, so the value passed was always either
    `explicit_db` itself or the config default.
    """
    if explicit_db:
        return "--db"
    state = env_variable_state(DB_PATH_VARIABLE)
    if state == ENV_SET:
        return DB_PATH_VARIABLE
    # BOTH REMAINING STATES GET THE DEFAULT PATH and they must not get the same
    # sentence: one tells the operator to export the variable, the other tells them
    # the export they already wrote did nothing. `env | grep SWAP_DB_PATH` answers
    # differently for the two, and the report has to agree with it.
    why = {
        ENV_UNSET: f"{DB_PATH_VARIABLE} IS NOT SET in this shell",
        ENV_SET_BUT_EMPTY: (
            f"{DB_PATH_VARIABLE} IS SET BUT EMPTY in this shell -- `export {DB_PATH_VARIABLE}=` is "
            f"not the same as not exporting it, and config._env falls back to the built-in default "
            f"for a value that is empty or whitespace"
        ),
    }[state]
    return (
        f"the built-in default, because {why}. The workers read "
        f"whatever {DB_PATH_VARIABLE} named in the shell that STARTED them, which may be a different file"
    )
