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
    # The Popen object is carried out, and it is NOT printable state -- it exists
    # so confirm_spawned() can call process.poll(). See that function for why
    # process_alive() is the wrong question to ask about a child this process
    # just forked.
    return {
        "worker": name,
        "outcome": "started",
        "pid": process.pid,
        "argv": argv,
        "log": str(log_path),
        "process": process,
    }


# How long a freshly spawned worker is given to prove it is still there.
#
# Paid ONCE for the whole start rather than once per worker -- every worker is
# spawned first, then the settle is slept, then all of them are polled -- so
# starting three costs this much in total and not three times this much.
#
# 1.0s because the failure being caught is an IMMEDIATE death: an ImportError,
# a config error at class-definition time, a database path that cannot be
# opened. Measured on this machine, `import supervisor` alone is 0.022s and
# `import supervisor, workers.common` 0.143-0.214s, so a worker that is going
# to die on import has died well inside a second. This is deliberately NOT a
# readiness check -- it does not wait for the banner, does not open a socket,
# and makes no claim that the worker can reach any chain. It answers exactly
# one question: is the process we just forked still a process?
IMMEDIATE_DEATH_SETTLE_SECONDS = 1.0


def confirm_spawned(results: list[dict], settle_seconds: float = IMMEDIATE_DEATH_SETTLE_SECONDS) -> list[dict]:
    """Ask again whether each freshly spawned worker is still running.

    THE DEFECT THIS EXISTS FOR, found 2026-10-01 while the operator was
    restarting the workers to pick up SOL_RPC_URL. start_worker() returned
    `"started"` because subprocess.Popen() RETURNED, and nothing ever asked a
    second time. A worker that exits immediately -- an ImportError, a
    SWAP_DB_PATH that cannot be opened, a config error raised at
    class-definition time -- produced

        started           deposit_watcher pid=3998319 log=.../deposit_watcher.log

    and a pid file naming a dead process. Three green lines, and nothing
    polling for deposits. That is rule 13's own sentence turned around: "when a
    deploy depends on new code actually running, verify the artifact, not the
    deploy", and rule 14's "make 'did nothing' look different from 'did work'"
    on the one command whose entire job is to say what it just did.

    stop_worker() has always made the ABSENCE of the process the assertion
    rather than the exit code of the kill. This is the same assertion pointed
    the other way: the PRESENCE of the process, asked of the operating system
    after the fact, rather than the return of the call that created it.

    WHY process.poll() AND NOT process_alive(). Not because process_alive()
    would get the wrong answer -- it would get the right one. The spawned
    workers are our own children, a child that has exited stays in the process
    table as a ZOMBIE until its parent reaps it, and bare `os.kill(pid, 0)`
    succeeds on a zombie; but process_alive() already calls _is_zombie() for
    exactly that reason and tests/test_supervisor.py::test_a_zombie_is_not_alive
    pins it. (That paragraph is here because the first draft of this comment
    asserted the opposite, was wrong, and is the rule 17 failure of writing a
    plausible reading in the register of a measurement.)

    poll() is used for two things process_alive() cannot give:

      the EXIT CODE.  "exited 1" and "killed by SIGKILL" are different
          problems with different fixes, and a boolean cannot say which. The
          code is the most useful token on the line.
      the REAP.  This process is the parent. Leaving three dead children
          unreaped would leave three zombies owned by a supervisor that is
          about to exit -- harmless, since they reparent to init, but it means
          the supervisor's own report of a death is the one thing that does not
          clean up after it.

    Returns the same list, with any dead worker's outcome changed to "DIED" and
    its exit code and last log line attached. The pid file is REMOVED for it,
    because a file claiming a running process that is not running is the stale
    record rule 13 warns about -- a later `stop` would read it, find nothing,
    and have to report an absence it did not cause.
    """
    spawned = [result for result in results if result["outcome"] == "started"]
    if not spawned:
        return results
    time.sleep(settle_seconds)
    for result in spawned:
        process = result.get("process")
        if process is None:
            # A caller that built the result dict by hand rather than through
            # start_worker(). Nothing to poll, so nothing is claimed either way.
            continue
        code = process.poll()
        if code is None:
            continue
        result["outcome"] = "DIED"
        result["exit_code"] = code
        result["last_log_line"] = _last_log_line(result.get("log", ""))
    return results


def _last_log_line(log_path: str) -> str:
    """The last non-empty line of a worker's log, for a worker that just died.

    On a traceback that is the exception, which is the single most useful line
    on the screen and the one an operator would otherwise have to go and find.
    Rule 14: say what happened where the operator is already looking.

    Never raises. This runs inside the report of a failure that has already
    happened, and a report that dies reading a log file has turned one problem
    into two.
    """
    if not log_path:
        return ""
    try:
        lines = [line.strip() for line in Path(log_path).read_text(errors="replace").splitlines()]
    except OSError as exc:
        # NOT a blind except: OSError only, and the failure is REPORTED in the
        # return value rather than becoming an empty string that reads like an
        # empty log (rule 12's BLE001 note -- the caller must be able to tell a
        # failure from a real answer).
        return f"(could not read {log_path}: {exc})"
    populated = [line for line in lines if line]
    return populated[-1] if populated else "(the log is empty -- the worker wrote nothing at all)"


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

    ONE LINE OF ITS OWN -- the database -- AND THEN workers/common.endpoint_lines().
    This function used to render the chains itself, with its own `default_ports`
    dict and its own `for asset in ("BTC", "LTC", "GRC"):` loop, and that was
    CLAUDE.md rule 8's "two copies of one rule is a bug with a delay on it"
    sitting on a SAFETY banner.

    MEASURED 2026-10-01, on the live host, which is what forced this. The
    operator ran `python3 swap_terminal/supervisor.py start` while rehearsing a
    devnet-SOL -> testnet-GRC swap and got:

        BTC rpc           127.0.0.1:0  wallet=(default wallet)  <- not a default port for this chain
        LTC rpc           127.0.0.1:0  wallet=(default wallet)  <- not a default port for this chain
        GRC rpc           127.0.0.1:25715  wallet=(default wallet)  <- not a default port for this chain
        network           NOT VERIFIED here -- ...
        about to spawn    a payout worker CAN broadcast. Stop now if this database is pointed at a funded mainnet wallet.

    Three chains, and NOT ONE WORD ABOUT SOL -- not the endpoint, not the
    commitment threshold, not whether a Solana adapter was constructed at all --
    on the banner printed immediately above the line that warns the spawn can
    move money. The deposit_watcher it was about to start is the process that
    would or would not see that devnet deposit. The banner could not say which,
    and it read EXACTLY THE SAME either way. XRP was missing for the same
    reason: this loop was written when three chains was all there was, and a
    fourth and fifth arrived somewhere else.

    Two further defects came with the duplication, and both are visible in the
    paste above:

      `127.0.0.1:0`   An unconfigured chain rendered as a configured one.
          chains/registry.py SKIPS a port-0 chain, so the banner was naming two
          adapters that do not exist. endpoint_lines() fixed this on 2026-09-26
          and the fix never reached here, because here was a second copy.
      `not a default port for this chain`   Printed against GRC's 25715, which
          IS the Gridcoin test port. network_target.CHAIN_PORTS knows that;
          this function's hand-maintained `default_ports` dict had 25779 in it.
          A hand-written copy of a vocabulary that is derived elsewhere
          (rule 11), disagreeing with the derivation, and telling the operator
          their test chain was unrecognized.

    So the chain rendering is now imported rather than reimplemented. There is
    one answer to "where is each chain and what does it wait for", it lives in
    the module the workers themselves print from, and a sixth chain cannot
    arrive in one banner and not the other.

    WHAT IS STILL SAID HERE AND NOT THERE. The database path, because
    endpoint_lines() is called by three workers that each print their own
    database line already; and the network disclaimer, because nothing in this
    process opens a socket, so nothing in this process can honestly claim to
    know which network a daemon is on. A port number is a reason to believe,
    not a check (rule 17).
    """
    # DEFERRED, NOT MODULE-SCOPE, and the noqa below is a claim about two
    # things that were both checked rather than assumed (rule 19).
    #
    # Structural: workers.common imports chains.registry, chains.solana and
    # chains.xrp at module scope. `supervisor.py stop` needs none of that to
    # send a signal to a pid, and at module scope an ImportError anywhere in
    # the chain code would propagate out of `import supervisor` and take the
    # kill path down with it -- the reaper defeated by the thing it reaps
    # (rule 13). The stop path stays independent of the chain code.
    #
    # Measured 2026-10-01, three runs each, warm page cache, on this machine:
    #
    #     import supervisor                       0.022 / 0.021 / 0.024 s
    #     import supervisor, workers.common       0.143 / 0.174 / 0.214 s
    #
    # so pulling the chain modules in costs roughly 0.15s of the 0.02s this
    # module takes on its own -- about eight times the import, paid by every
    # `start`, `stop` and `status` invocation whether or not a banner is
    # printed. (A first, cold-cache measurement read 0.95s for workers.common
    # alone; it is kept here because it is what an operator on a cold host
    # actually waits, and because a single warm number quoted as "the" cost
    # would be the measurement-in-prose this repo keeps re-learning.) Not a
    # large number, but `stop` prints no chain line at all -- `start` (line
    # ~482) and `status` (line ~547) are the only two callers -- so on the one
    # subcommand an operator runs when something is already wrong it buys
    # nothing.
    from workers.common import endpoint_lines  # noqa: PLC0415 -- measured above

    return [
        f"  database          {Config.DB_PATH}",
        *endpoint_lines(),
        "  network           NOT VERIFIED here -- the port above is only the default for a network, "
        "not proof of one. Ask the daemon (`getblockchaininfo`) before trusting it.",
    ]


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
    # Every worker is spawned BEFORE the settle is slept, so the wait is paid
    # once for the whole command rather than once per worker.
    results = confirm_spawned(results)

    lines = []
    for result in results:
        if result["outcome"] == "started":
            lines.append(
                f"  started           {result['worker']} pid={result['pid']} log={result.get('log', '?')}"
                f"  <- still running {format_duration(IMMEDIATE_DEATH_SETTLE_SECONDS)} after the spawn, asked of the OS"
            )
        elif result["outcome"] == "DIED":
            pid_file(run_dir, result["worker"]).unlink(missing_ok=True)
            lines.append(
                f"  *** DIED ***      {result['worker']} pid={result['pid']} exited {result['exit_code']} within "
                f"{format_duration(IMMEDIATE_DEATH_SETTLE_SECONDS)} of being spawned, so it is NOT polling and its pid "
                f"file has been removed. Log: {result.get('log', '?')}"
            )
            last = result.get("last_log_line", "")
            if last:
                lines.append(f"                    last log line: {last}")
        else:
            lines.append(
                f"  ALREADY RUNNING   {result['worker']} pid={result['pid']}  <- nothing was spawned for this one"
            )
    _print_block("  outcome", lines)

    started = sum(1 for r in results if r["outcome"] == "started")
    died = sum(1 for r in results if r["outcome"] == "DIED")
    skipped = len(results) - started - died
    # "did nothing" must not look like "did work" (rule 13/14), so all three
    # counts are always printed even when two of them are zero. died= leads the
    # interpretation because it is the one that means the command failed at the
    # thing it was for.
    print(
        f"  summary           spawned={started}  already-running={skipped}  died={died}  "
        f"in {format_duration(time.monotonic() - started_at)}"
        + ("  <- died>0 means a worker is NOT polling; nothing below this line will happen" if died else "")
    )
    # Non-zero, so a script or a .desktop launcher that chains off this command
    # does not carry on as though the workers were up.
    return 1 if died else 0


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
