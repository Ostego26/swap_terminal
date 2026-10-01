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

import signal
import time
from collections.abc import Callable
from pathlib import Path

from chains.registry import build_adapters, missing_settings
from chains.solana import SolanaAdapter
from chains.xrp import XRPAdapter
from config import Config
from log_setup import configure_logging
from microfortnights import format_duration
from network_target import CHAIN_PORTS, classify, configuring_variable


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
        rpc = Config.RPC[asset]
        wallet = rpc["wallet"] or "(default wallet)"
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
    print(f"  database        {Config.DB_PATH}", flush=True)
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
STANDING_COUNTS = frozenset({"HALTED_for_review", "failed_total"})


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


def cycle_line(worker_name: str, cycle: int, seconds: float, counts: dict[str, int], notes: str = "") -> str:
    """Render one cycle's result so that idle and productive cycles differ.

    Rule 14: "a poll that found nothing and a poll that paid someone must not
    share a success line." The IDLE/WORKED token is that difference, and it is
    the first thing on the line so it survives being skimmed.

    A count named in STANDING_COUNTS still prints and still reads non-zero; it
    just does not let the cycle claim it worked. See that constant for the
    measurement behind it.
    """
    did_work = any(value for key, value in counts.items() if key not in STANDING_COUNTS)
    marker = "WORKED" if did_work else "IDLE  "
    rendered = " ".join(f"{key}={value}" for key, value in counts.items()) or "(none)"
    line = f"{worker_name} cycle={cycle} {marker} {rendered} in {format_duration(seconds)}"
    return f"{line}  <- {notes}" if notes else line


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
