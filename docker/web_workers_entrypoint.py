#!/usr/bin/env python3
"""gunicorn AND the three supervised workers, in one container, with a PROVEN stop.

Role: file (container entry point for the `web` service)
Reads: the process environment; swap_terminal/supervisor.py's own worker table
Writes: nothing of its own. The workers write pid files and logs under
        swap_terminal/runtime/, which is a volume.
Can move funds: YES, indirectly and unavoidably -- payout_worker is one of the
        three processes it starts, and gunicorn serves the route that creates
        swaps. This file does not decide anything about either; it starts them
        and it stops them.
Live-safe: it starts whatever the environment arms. Arming is the operator's
        decision (CLAUDE.md rule 16) and nothing here supplies a credential.

WHY THIS FILE EXISTS AT ALL, AND WHY IT IS NOT `CMD ["gunicorn", ...]`.

Three processes have to live in ONE container, and the design doc's §6.1 says why
in one sentence: splitting the workers out of the Flask container breaks
services/kill_switch.py in the direction where a stop reports success and the
payout worker keeps running. supervisor.pid_is_still_ours() reads
/proc/<pid>/cmdline and unaccounted_workers() walks /proc, so the proof of
absence requires one PID namespace. A topology that makes the payout stop
unprovable is not a topology.

That leaves the question this file answers: if gunicorn is pid 1, `docker compose
stop` signals gunicorn and NOTHING signals the workers. They are then SIGKILLed
when the container dies, which is the exact failure rule 13 names -- a stop that
cannot prove it worked. The container would report a clean exit while three
processes were killed without ever being asked to stop, one of them mid-send.

So the order is explicit and the proof is the existing one:

    SIGTERM -> supervisor.stop_worker() for each worker, which reads the pid file,
               signals, and POLLS FOR ABSENCE -> then gunicorn is asked to stop ->
               then this process exits with what the stop actually established.

The reaper is named here, in the same file as the spawn, which is rule 13's first
bullet: a spawn and its reap are one change.

WHAT IS DELIBERATELY NOT DONE HERE.

No supervisord, no s6, no tini-with-a-shell-script. Those replace a stop this
repository can already prove with one it cannot: supervisor.py's stop is the thing
tests/test_supervisor.py exercises, and a process manager that signals the workers
on its own schedule would make that proof describe something that no longer runs.

No retry loop around a worker that dies. supervisor.confirm_spawned() already
reports a DIED result and cleans the pid file; reviving a worker that is failing on
every start would turn a loud failure into a quiet one.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR / "swap_terminal"))

import supervisor  # noqa: E402  -- after the sys.path.insert above, same as every root tool

#: Where the workers' pid files and logs go. MUST be the volume, not the image's
#: writable layer: a pid file that dies with the container is a pid file that can
#: never be used to prove a stop after a restart, which is the whole point of
#: having one.
RUN_DIR = Path(os.getenv("ST_WORKER_RUN_DIR", str(BASE_DIR / "swap_terminal" / "runtime")))

#: How long each worker gets to exit before supervisor.stop_worker() escalates.
#: Must be comfortably INSIDE the compose `stop_grace_period`, or the runtime
#: SIGKILLs this process mid-stop and the proof is lost -- which would be a stop
#: that cannot prove it worked, arrived at by a timeout rather than a bug.
WORKER_GRACE_SECONDS = float(os.getenv("ST_WORKER_GRACE_SECONDS", "15"))

#: Seconds to wait for gunicorn after the workers are gone.
GUNICORN_GRACE_SECONDS = float(os.getenv("ST_GUNICORN_GRACE_SECONDS", "20"))

_stopping = False


def announce(lines: list[str]) -> None:
    """Print and flush. Rule 14: output nobody sees during the wait is not output.

    flush=True because this is pid 1 behind a pipe, where Python's block buffering
    holds a startup banner until the buffer fills -- so an operator watching
    `docker compose logs -f` sees nothing at the moment they most need to.
    """
    for line in lines:
        print(line, flush=True)


def start_workers() -> list[dict]:
    """supervisor.start_worker() for each of the three, announcing the outcome of each.

    NOT a bare loop that ignores results. start_worker() returns an outcome of
    "started" or "already-running", and rule 13 says those must not render the same
    way: "already-running" inside a FRESH container means a pid file on the volume
    survived from a previous run and still names a live process, which cannot
    happen if the stop worked and is worth seeing.
    """
    results = []
    for name, argv in supervisor.worker_commands().items():
        result = supervisor.start_worker(name, argv, RUN_DIR)
        results.append(result)
        announce([f"  worker {name:18} {result['outcome']:16} pid={result['pid']}"])
    confirmed = supervisor.confirm_spawned(results)
    died = [r for r in confirmed if r.get("outcome") == "died"]
    for result in died:
        announce([
            f"  ! worker {result['worker']} DIED immediately after spawn; its log is "
            f"{result.get('log', '(unknown)')}"
        ])
    return confirmed


def stop_workers() -> bool:
    """Stop all three and return whether every one was PROVEN gone.

    The return value is the absence, not the exit code of a kill -- rule 13's
    "make the absence the assertion". supervisor.stop_worker() polls /proc; this
    only reports what it established.
    """
    all_gone = True
    for name in supervisor.worker_commands():
        result = supervisor.stop_worker(name, RUN_DIR, grace_seconds=WORKER_GRACE_SECONDS)
        outcome = result.get("outcome", "unknown")
        announce([f"  stop {name:18} {outcome}"])
        if outcome not in ("stopped", "not-running", "stale-pidfile"):
            all_gone = False
    # THE ORPHAN SCAN, AND ITS OUTPUT COMES FROM supervisor.unaccounted_lines().
    # The first draft of this file called unaccounted_workers() with NO arguments
    # and formatted the result here. It takes (names, run_dir) and returns a dict,
    # so the call was simply wrong -- and the formatting was a second place that
    # "no orphans" gets worded, which is rule 8 in prose: unaccounted_lines()
    # already handles the no-/proc case as NOT ESTABLISHED rather than as a pass,
    # and a hand-rolled copy here would have printed "(none)" for it.
    names = list(supervisor.worker_commands())
    for line in supervisor.unaccounted_lines(names, RUN_DIR):
        announce([line])
    if supervisor.unaccounted_workers(names, RUN_DIR):
        all_gone = False
    return all_gone


def main() -> int:
    # NO `global _stopping` HERE. main() only READS the flag; on_term() is the only
    # writer and declares it. ruff's PLW0602 said so -- a `global` for a name you
    # never assign is a claim about this function that is not true of it.

    announce([
        "swap_terminal web+workers container",
        f"  base dir        {BASE_DIR}",
        f"  run dir         {RUN_DIR}  <- pid files and worker logs; must be a volume",
        f"  SWAP_DB_PATH    {os.getenv('SWAP_DB_PATH', '(unset)')}  <- (unset) means config.py's"
        " BASE_DIR default, which inside a container is the IMAGE and dies with it",
        f"  worker grace    {WORKER_GRACE_SECONDS}s  <- must be inside compose stop_grace_period",
        "  starting three workers, then gunicorn. Stop order is the reverse, and the",
        "  workers' absence is PROVEN by /proc before gunicorn is asked to go.",
    ])
    RUN_DIR.mkdir(parents=True, exist_ok=True)

    start_workers()

    gunicorn_argv = ["gunicorn", "-c", str(BASE_DIR / "gunicorn.conf.py"), "wsgi:app"]
    announce([f"  gunicorn        {' '.join(gunicorn_argv)}"])
    # No shell, fixed argv built from this file's own constants.
    gunicorn = subprocess.Popen(gunicorn_argv, cwd=str(BASE_DIR))  # noqa: S603

    def on_term(signum, _frame):
        global _stopping  # noqa: PLW0603 -- see main()
        if _stopping:
            announce([f"  second signal {signum} while already stopping; ignoring it"])
            return
        _stopping = True
        announce([f"  signal {signum} received. Stopping WORKERS FIRST, then gunicorn."])
        proven = stop_workers()
        announce([
            "  workers proven gone" if proven
            else "  ! workers NOT proven gone -- see the lines above; this is the defect,"
                 " not the container stop"
        ])
        gunicorn.terminate()

    signal.signal(signal.SIGTERM, on_term)
    signal.signal(signal.SIGINT, on_term)

    try:
        code = gunicorn.wait()
    except KeyboardInterrupt:
        code = 130
    if not _stopping:
        # gunicorn died on its own. The workers are still running and nothing has
        # asked them to stop, so ask -- otherwise the container exits and they are
        # SIGKILLed, which proves nothing.
        announce([f"  gunicorn exited on its own with {code}; stopping the workers it outlived"])
        stop_workers()
    else:
        deadline = time.monotonic() + GUNICORN_GRACE_SECONDS
        while gunicorn.poll() is None and time.monotonic() < deadline:
            time.sleep(0.1)
        if gunicorn.poll() is None:
            announce([f"  gunicorn still alive after {GUNICORN_GRACE_SECONDS}s; killing it"])
            gunicorn.kill()
            code = gunicorn.wait()
    announce([f"  exit {code}"])
    return code


if __name__ == "__main__":
    sys.exit(main())
