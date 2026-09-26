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

from chains.monero import MoneroAdapter
from chains.registry import build_adapters
from chains.solana import SolanaAdapter
from chains.xrp import XRPAdapter
from config import Config
from microfortnights import format_duration
from network_target import CHAIN_PORTS, UNCONFIGURED_PORT, classify, configuring_variable


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
        # AN UNCONFIGURED CHAIN SAYS SO, matching what SOL, XRP and XMR say below.
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
        if rpc["port"] == UNCONFIGURED_PORT:
            lines.append(
                f"  {asset}  not configured ({CHAIN_PORTS[asset].port_variable} unset)  <- no {asset} "
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

    # XMR, appended separately for a DIFFERENT reason than SOL's. Its threshold
    # genuinely is a count of blocks, so it is not the unit mismatch above --
    # it is that the number printed may not be the number configured. Monero's
    # ten-block consensus spend lock is a floor under XMR_MIN_CONFIRMATIONS
    # (chains/monero_units.py), so a banner echoing the raw setting could tell
    # an operator their wallet waits 2 blocks when it waits 10. The adapter
    # renders its own line and says so when it raised the figure.
    # XRP, and its threshold is neither blocks nor a commitment rank -- it is
    # a validated-ledger boolean. Its own line so the unit is stated (rule 6).
    if Config.RPC.get("XRP", {}).get("url"):
        lines.append(XRPAdapter(**Config.RPC["XRP"]).endpoint_line())
    else:
        lines.append(
            f"  XRP  not configured ({configuring_variable('XRP')} unset)  <- no XRP adapter "
            f"is constructed; no XRP pair is allowed"
        )

    if Config.RPC.get("XMR", {}).get("port"):
        lines.append(MoneroAdapter(**Config.RPC["XMR"]).endpoint_line())
    else:
        lines.append(
            f"  XMR  not configured ({configuring_variable('XMR')} unset)  <- no Monero adapter "
            f"is constructed; no XMR pair is allowed"
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


def cycle_line(worker_name: str, cycle: int, seconds: float, counts: dict[str, int], notes: str = "") -> str:
    """Render one cycle's result so that idle and productive cycles differ.

    Rule 14: "a poll that found nothing and a poll that paid someone must not
    share a success line." The IDLE/WORKED token is that difference, and it is
    the first thing on the line so it survives being skimmed.
    """
    did_work = any(value for value in counts.values())
    marker = "WORKED" if did_work else "IDLE  "
    rendered = " ".join(f"{key}={value}" for key, value in counts.items()) or "(none)"
    line = f"{worker_name} cycle={cycle} {marker} {rendered} in {format_duration(seconds)}"
    return f"{line}  <- {notes}" if notes else line


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
