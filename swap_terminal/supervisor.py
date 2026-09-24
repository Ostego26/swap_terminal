#!/usr/bin/env python3
"""Start, stop and inspect the swap_terminal workers -- the reaper (rule 13).

Role: entry point (process supervisor for the three polling workers)
Reads: swap_terminal.db path and RPC endpoints from config.Config (to echo
       them, not to query them), pid files under the run directory, and
       /proc/<pid>/cmdline to confirm a pid is still the process it was
Writes: pid files under the run directory (default swap_terminal/runtime/),
       and signals to the processes named in them
Can move funds: no, not directly -- but it STARTS workers/payout_worker.py,
       which broadcasts payouts. Starting a payout worker against a funded
       mainnet wallet moves funds; that is why `start` prints the database, the
       RPC endpoints and every worker it is about to spawn before spawning
       anything.
Mainnet-safe: yes to run, and `status` and `stop` are safe at any time. `start`
       is exactly as safe as the workers it starts, which is not very.

WHY THIS FILE EXISTS.

Measured 2026-09-24, before this commit: workers/deposit_watcher.py:11,
workers/payout_worker.py:11 and workers/reconcile_worker.py:12 were each a bare
`while True:` with a `time.sleep()` at the bottom, app.py started none of them,
and nothing anywhere in the tree stopped any of them. No pid file, no
supervisor, no kill list. swap_terminal/README.md told the operator to run each
one "in separate terminals", by hand.

Rule 13's damage model is why that matters more here than it looks. An orphan
does not crash anything. A payout worker started by hand in a second terminal
is invisible to the first, and two payout workers polling the same SQLite
database both read the same pending swap. What comes out of that is not an
error message, it is a second transaction on a chain.

THE FOUR THINGS RULE 13 ASKS FOR, AND WHERE EACH ONE IS.

  Name the reaper at the spawn site.   Each worker's module header now names
    this file. A spawn and its reap are one change.
  Prefer a pid file to a pgrep pattern.   `pgrep -f workers/payout_worker.py`
    matches what a command line happens to look like today; renaming the script
    silently orphans it. The pid files here are the record. To survive pid
    REUSE -- the failure a pid file has and a pattern does not -- each file
    stores the pid AND the command line, and _pid_is_still_ours() refuses to
    signal a pid whose /proc entry no longer matches. Killing a recycled pid
    would be worse than failing to kill anything.
  A stop that cannot prove it worked is not a stop.   stop_worker() does not
    report the exit status of the kill. It polls until the process is GONE and
    returns the absence as the result; if the process is still there after
    SIGTERM, the grace period and SIGKILL, it returns "failed" and the CLI
    exits non-zero. The absence is the assertion.
  "Skipped" must not look like "did work".   start on an already-running worker
    returns "already-running", prints it in its own marked line, and the
    summary counts it separately from "started". A run that spawned nothing and
    a run that spawned three processes never share a success line.

WHAT THIS DELIBERATELY DOES NOT DO.

It does not guard the WORK, only the processes. Rule 13's last bullet is the
harder half: "a database constraint that makes a second payout impossible is
better [than a lock], because it survives the case where the lock was wrong."
A single-instance pid file does not survive an operator running
`python3 workers/payout_worker.py` directly, and it cannot -- the pid file is a
convention, not a constraint. The measurement of what happens when two payout
workers do overlap is in tests/test_payout_concurrency.py, and the database
change it argues for is a PROPOSAL for the operator (rule 16: it changes the
payout path, which is fund movement), not something this file works around.
"""

import argparse
import contextlib
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from config import Config
from microfortnights import format_duration

BASE_DIR = Path(__file__).resolve().parent

# Where pid files live. Overridable so a test can point it at a temp directory,
# and so an operator can put it on a tmpfs. Kept out of git by .gitignore.
DEFAULT_RUN_DIR = BASE_DIR / "runtime"

# How long a worker gets to exit on SIGTERM before SIGKILL. Seconds, because
# signal.alarm-style waits and time.monotonic() arithmetic are an interface,
# not a report (rule 6): the µfn figure is printed, the arithmetic is in
# seconds.
DEFAULT_GRACE_SECONDS = 10.0

# Poll interval while waiting for a process to disappear.
REAP_POLL_SECONDS = 0.05


def worker_commands(python_executable: str = sys.executable) -> dict[str, list[str]]:
    """Return the argv for each supervised worker, keyed by name.

    A function rather than a module constant so that a test can substitute a
    harmless command (`python3 -c "time.sleep(30)"`) and exercise the real
    start/stop/status paths without running anything that talks to a chain.
    """
    return {
        "deposit_watcher": [python_executable, str(BASE_DIR / "workers" / "deposit_watcher.py")],
        "payout_worker": [python_executable, str(BASE_DIR / "workers" / "payout_worker.py")],
        "reconcile_worker": [python_executable, str(BASE_DIR / "workers" / "reconcile_worker.py")],
    }


def pid_file(run_dir: Path, name: str) -> Path:
    return run_dir / f"{name}.pid"


def read_pid_record(path: Path) -> tuple[int, str] | None:
    """Return (pid, command) from a pid file, or None if it is missing or junk.

    Junk includes: an empty file, a non-numeric first line, and a file written
    by a version of this supervisor that did not record the command. All of
    them mean "this file tells us nothing", which is different from "the
    process is gone" -- the caller distinguishes them.
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    lines = raw.splitlines()
    if not lines:
        return None
    try:
        pid = int(lines[0].strip())
    except ValueError:
        return None
    command = lines[1].strip() if len(lines) > 1 else ""
    return pid, command


def write_pid_record(path: Path, pid: int, command: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{pid}\n{command}\n", encoding="utf-8")


def _is_zombie(pid: int) -> bool:
    """True if this pid is a zombie -- exited, but not yet reaped by its parent.

    Found by tests/test_supervisor.py on 2026-09-24, and it is not a test
    artifact. `os.kill(pid, 0)` SUCCEEDS for a zombie, because a zombie still
    occupies a process table entry. A supervisor that starts a worker and then
    stops it in the same process therefore watched a dead process forever, sent
    SIGKILL to something already dead, waited out the full grace period twice
    and then reported

        FAILED  sleeper pid=1902 is STILL ALIVE after SIGTERM+SIGKILL

    about a process that had exited immediately. Reporting a successful reap as
    a failure is the same class of defect as the reverse, and on the payout
    worker it would send an operator hunting for a process that is not there.

    A zombie runs no code, holds no database lock and cannot broadcast a
    transaction, so for every question this file asks -- may I start another
    one, is it gone yet -- a zombie is NOT alive.

    /proc/<pid>/stat's third field is the state, but the second field is the
    executable name in parentheses and may itself contain spaces and
    parentheses, so the state is read after the LAST ')' rather than by
    splitting on whitespace.
    """
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8", errors="replace")
    except (FileNotFoundError, PermissionError, OSError):
        # No /proc, or not readable: we cannot tell, so do not claim it is dead.
        return False
    _, _, remainder = stat.rpartition(")")
    fields = remainder.split()
    return bool(fields) and fields[0] == "Z"


def process_alive(pid: int) -> bool:
    """True if a process with this pid exists, is not a zombie, and is signalable.

    signal 0 performs the permission and existence checks without delivering
    anything. A PermissionError means the process exists but belongs to someone
    else, which is still "alive" for the purpose of refusing to double-start.
    """
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return not _is_zombie(pid)


def _proc_cmdline(pid: int) -> str:
    """Return /proc/<pid>/cmdline as a space-joined string, or "" if unreadable.

    Empty string means "cannot tell" -- on a platform without /proc, or for a
    process we may not read. The caller treats "cannot tell" as "do not refuse
    to signal", because the pid file is still the best evidence available and
    refusing would leave an orphan running, which is the failure this whole
    file exists to prevent.
    """
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except (FileNotFoundError, PermissionError, OSError):
        return ""
    return " ".join(part for part in raw.decode("utf-8", "replace").split("\0") if part)


def pid_is_still_ours(pid: int, recorded_command: str) -> bool:
    """Guard against pid reuse before signalling.

    A pid file names a number, and numbers get recycled. Between the worker
    exiting and the operator running `stop`, that pid can belong to anything --
    including the shell the operator is typing in. Signalling it would be
    strictly worse than failing to stop a process that is already gone.

    Returns True when the recorded command still appears in the live process's
    command line, and also when we have no way to check (no /proc, or no
    command recorded), because in that case the pid file remains the best
    evidence we have.
    """
    if not recorded_command:
        return True
    live = _proc_cmdline(pid)
    if not live:
        return True
    # The recorded command is the argv joined with spaces, so a prefix match on
    # the script path is the stable part: a worker that re-execs itself keeps
    # the path and may lose the interpreter's absolute form.
    marker = recorded_command.rsplit(maxsplit=1)[-1]
    return marker in live


def start_worker(name: str, argv: list[str], run_dir: Path) -> dict:
    """Start one worker unless it is already running.

    Returns a result dict with `outcome` in {"started", "already-running"},
    the pid, and the argv actually used. "already-running" is a distinct
    outcome and not a quiet success: rule 13 treats "skipped" printed beside
    "ok" as a defect in the output.
    """
    path = pid_file(run_dir, name)
    record = read_pid_record(path)
    if record is not None:
        pid, command = record
        if process_alive(pid) and pid_is_still_ours(pid, command):
            return {"worker": name, "outcome": "already-running", "pid": pid, "argv": argv}

    run_dir.mkdir(parents=True, exist_ok=True)
    log_path = run_dir / f"{name}.log"
    # The child's stdout and stderr go to a file rather than being inherited:
    # an operator who closes the starting terminal must not take the workers
    # with it, and a worker whose output goes nowhere is rule 14's silence.
    log_handle = log_path.open("ab")
    try:
        process = subprocess.Popen(  # noqa: S603 -- argv is worker_commands()' own table plus sys.executable; no shell, no user string
            argv,
            cwd=str(BASE_DIR),
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
    finally:
        log_handle.close()
    write_pid_record(path, process.pid, " ".join(argv))
    return {"worker": name, "outcome": "started", "pid": process.pid, "argv": argv, "log": str(log_path)}


def stop_worker(name: str, run_dir: Path, grace_seconds: float = DEFAULT_GRACE_SECONDS) -> dict:
    """Stop one worker and PROVE it is gone.

    The return value's `outcome` is one of:

      not-running     no pid file, or a pid file naming nothing that exists.
      stale-pidfile   the pid exists but is no longer the process we started
                      (pid reuse). Nothing is signalled and the file is
                      removed. This is the case where killing would be the
                      damage.
      stopped         the process was signalled and is now ABSENT. The absence
                      was polled for and confirmed; this is not the exit status
                      of the kill.
      failed          still alive after SIGTERM, the grace period and SIGKILL.
                      The caller must treat this as an error, because the thing
                      the operator asked for did not happen.
    """
    path = pid_file(run_dir, name)
    record = read_pid_record(path)
    if record is None:
        return {"worker": name, "outcome": "not-running", "pid": None, "signals": []}

    pid, command = record
    if not process_alive(pid):
        path.unlink(missing_ok=True)
        return {"worker": name, "outcome": "not-running", "pid": pid, "signals": [], "note": "pid file was stale"}

    if not pid_is_still_ours(pid, command):
        path.unlink(missing_ok=True)
        return {"worker": name, "outcome": "stale-pidfile", "pid": pid, "signals": [], "note": "pid belongs to another process now"}

    signals_sent = []
    started = time.monotonic()

    os.kill(pid, signal.SIGTERM)
    signals_sent.append("SIGTERM")
    if _wait_for_absence(pid, grace_seconds):
        path.unlink(missing_ok=True)
        return {
            "worker": name,
            "outcome": "stopped",
            "pid": pid,
            "signals": signals_sent,
            "seconds": time.monotonic() - started,
        }

    os.kill(pid, signal.SIGKILL)
    signals_sent.append("SIGKILL")
    # SIGKILL cannot be caught, so a process that survives this window is
    # either a zombie we cannot reap (not our child any more) or uninterruptible
    # in the kernel. Either way we must not claim it stopped.
    if _wait_for_absence(pid, grace_seconds):
        path.unlink(missing_ok=True)
        return {
            "worker": name,
            "outcome": "stopped",
            "pid": pid,
            "signals": signals_sent,
            "seconds": time.monotonic() - started,
        }

    return {
        "worker": name,
        "outcome": "failed",
        "pid": pid,
        "signals": signals_sent,
        "seconds": time.monotonic() - started,
    }


def _reap_if_ours(pid: int) -> None:
    """Clear the zombie if this pid is a child of ours, otherwise do nothing.

    When `start` and `stop` run in the same process -- which is what a test
    does, and what any future combined command would do -- the stopped worker
    is our own child and stays a zombie until someone waits on it. When they
    run as separate invocations, the worker is re-parented to init on the
    starting process's exit and init reaps it, so ChildProcessError here is the
    normal case and is not an error.
    """
    with contextlib.suppress(ChildProcessError, OSError):
        os.waitpid(pid, os.WNOHANG)


def _wait_for_absence(pid: int, timeout_seconds: float) -> bool:
    """Poll until the pid is gone. True means gone, False means still there."""
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        _reap_if_ours(pid)
        if not process_alive(pid):
            return True
        time.sleep(REAP_POLL_SECONDS)
    _reap_if_ours(pid)
    return not process_alive(pid)


def worker_status(name: str, run_dir: Path) -> dict:
    """Report one worker's state without changing anything."""
    path = pid_file(run_dir, name)
    record = read_pid_record(path)
    if record is None:
        return {"worker": name, "state": "stopped", "pid": None, "detail": "no pid file"}
    pid, command = record
    if not process_alive(pid):
        return {"worker": name, "state": "stopped", "pid": pid, "detail": "pid file is stale; process is gone"}
    if not pid_is_still_ours(pid, command):
        return {"worker": name, "state": "unknown", "pid": pid, "detail": "pid now belongs to a different process"}
    return {"worker": name, "state": "running", "pid": pid, "detail": ""}


def endpoint_summary() -> list[str]:
    """Lines describing what the workers will talk to (rule 14).

    Every parameter that decides the answer is echoed: the database, each
    chain's RPC endpoint, and the confirmation threshold that governs when a
    deposit is credited. The default-port note is labeled as what it is -- the
    port a daemon uses by default -- and NOT presented as a measurement of
    which network the daemon is actually on. Nothing here connects to anything,
    so nothing here can honestly claim to know the network; a port number is a
    reason to believe, not a check (rule 17).
    """
    default_ports = {
        8332: "BTC mainnet default port",
        18332: "BTC testnet default port",
        9332: "LTC mainnet default port",
        19332: "LTC testnet default port",
        15715: "GRC mainnet default port",
        25779: "GRC testnet default port",
    }
    lines = [f"  database          {Config.DB_PATH}"]
    for asset in ("BTC", "LTC", "GRC"):
        rpc = Config.RPC[asset]
        port = int(rpc["port"])
        hint = default_ports.get(port, "not a default port for this chain")
        confirmations = getattr(Config, f"{asset}_MIN_CONFIRMATIONS")
        wallet = rpc["wallet"] or "(default wallet)"
        lines.append(
            f"  {asset} rpc           {rpc['host']}:{port}  wallet={wallet}  <- {hint}"
        )
        # Confirmations are a COUNT OF BLOCKS and are never rendered in µfn
        # (rule 6): six confirmations is six confirmations.
        lines.append(f"  {asset} confirmations {confirmations} blocks before a deposit is credited")
    lines.append(
        "  network           NOT VERIFIED here -- the port above is only the default for a network, "
        "not proof of one. Ask the daemon (`getblockchaininfo`) before trusting it."
    )
    return lines


def _print_block(title: str, lines: list[str]) -> None:
    print(title)
    if lines:
        for line in lines:
            print(line)
    else:
        # Rule 14: never let an empty result print nothing. A blank gap is
        # ambiguous between zero rows and a query that broke.
        print("  (none)")


def command_start(names: list[str], run_dir: Path, commands: dict[str, list[str]]) -> int:
    started_at = time.monotonic()
    print("swap_terminal supervisor: START")
    _print_block("  targets", [f"  workers           {', '.join(names)}", *endpoint_summary()])
    print(f"  run directory     {run_dir}")
    print("  about to spawn    a payout worker CAN broadcast. Stop now if this database is pointed at a funded mainnet wallet.")

    results = [start_worker(name, commands[name], run_dir) for name in names]
    lines = []
    for result in results:
        if result["outcome"] == "started":
            lines.append(f"  started           {result['worker']} pid={result['pid']} log={result.get('log', '?')}")
        else:
            lines.append(
                f"  ALREADY RUNNING   {result['worker']} pid={result['pid']}  <- nothing was spawned for this one"
            )
    _print_block("  outcome", lines)

    started = sum(1 for r in results if r["outcome"] == "started")
    skipped = len(results) - started
    # "did nothing" must not look like "did work" (rule 13/14), so the two
    # counts are always both printed even when one of them is zero.
    print(f"  summary           spawned={started}  already-running={skipped}  in {format_duration(time.monotonic() - started_at)}")
    return 0


def command_stop(names: list[str], run_dir: Path, grace_seconds: float) -> int:
    started_at = time.monotonic()
    print("swap_terminal supervisor: STOP")
    print(f"  workers           {', '.join(names)}")
    print(f"  run directory     {run_dir}")
    print(f"  grace             {format_duration(grace_seconds)} on SIGTERM before SIGKILL")

    results = [stop_worker(name, run_dir, grace_seconds) for name in names]
    lines = []
    for result in results:
        signals = "+".join(result["signals"]) or "none"
        if result["outcome"] == "stopped":
            lines.append(
                f"  stopped           {result['worker']} pid={result['pid']} signals={signals} "
                f"in {format_duration(result['seconds'])}  <- absence confirmed by polling, not by the kill's exit code"
            )
        elif result["outcome"] == "not-running":
            lines.append(f"  not running       {result['worker']}  <- nothing to stop; {result.get('note', 'no pid file')}")
        elif result["outcome"] == "stale-pidfile":
            lines.append(
                f"  STALE PID FILE    {result['worker']} pid={result['pid']} was NOT signalled: "
                f"{result.get('note', '')}. pid file removed."
            )
        else:
            lines.append(
                f"  FAILED            {result['worker']} pid={result['pid']} is STILL ALIVE after {signals}. "
                "Do not assume it stopped."
            )
    _print_block("  outcome", lines)

    stopped = sum(1 for r in results if r["outcome"] == "stopped")
    failed = sum(1 for r in results if r["outcome"] == "failed")
    print(
        f"  summary           stopped={stopped}  failed={failed}  "
        f"untouched={len(results) - stopped - failed}  in {format_duration(time.monotonic() - started_at)}"
    )
    return 1 if failed else 0


def command_status(names: list[str], run_dir: Path) -> int:
    print("swap_terminal supervisor: STATUS")
    print(f"  run directory     {run_dir}")
    for line in endpoint_summary():
        print(line)
    lines = []
    for name in names:
        state = worker_status(name, run_dir)
        detail = f"  {state['detail']}" if state["detail"] else ""
        lines.append(f"  {state['state']:<9} {state['worker']} pid={state['pid']}{detail}")
    _print_block("  workers", lines)
    running = sum(1 for name in names if worker_status(name, run_dir)["state"] == "running")
    print(
        f"  summary           running={running}/{len(names)}  <- expected {len(names)} while swaps are open; "
        "0 means no worker is polling and deposits will not be credited"
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="supervisor.py",
        description="Start, stop and inspect the swap_terminal workers. Every spawn here has a reaper here.",
    )
    parser.add_argument("action", choices=("start", "stop", "status"))
    parser.add_argument(
        "workers",
        nargs="*",
        help="worker names; default is all of them",
    )
    parser.add_argument(
        "--run-dir",
        default=str(DEFAULT_RUN_DIR),
        help=f"where pid files live (default: {DEFAULT_RUN_DIR})",
    )
    parser.add_argument(
        "--grace-seconds",
        type=float,
        default=DEFAULT_GRACE_SECONDS,
        help="seconds to wait after SIGTERM before SIGKILL (name says _SECONDS, so it stays seconds -- rule 6)",
    )
    return parser


def main(argv: list[str] | None = None, commands: dict[str, list[str]] | None = None) -> int:
    args = build_parser().parse_args(argv)
    table = worker_commands() if commands is None else commands
    names = args.workers or list(table)
    unknown = [name for name in names if name not in table]
    if unknown:
        print(f"unknown worker(s): {', '.join(unknown)}; known: {', '.join(table)}")
        return 2

    run_dir = Path(args.run_dir)
    if args.action == "start":
        return command_start(names, run_dir, table)
    if args.action == "stop":
        return command_stop(names, run_dir, args.grace_seconds)
    return command_status(names, run_dir)


if __name__ == "__main__":
    sys.exit(main())
