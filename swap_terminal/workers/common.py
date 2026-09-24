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

from chains.bitcoin import BitcoinAdapter
from chains.gridcoin import GridcoinAdapter
from chains.litecoin import LitecoinAdapter
from config import Config
from microfortnights import format_duration


def build_adapters() -> dict:
    rpc = Config.RPC
    return {
        "BTC": BitcoinAdapter(**rpc["BTC"]),
        "LTC": LitecoinAdapter(**rpc["LTC"]),
        "GRC": GridcoinAdapter(**rpc["GRC"]),
    }


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
        lines.append(
            f"  {asset}  rpc={rpc['host']}:{rpc['port']} wallet={wallet} min_confirmations={confirmations} blocks"
        )
    return lines


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
