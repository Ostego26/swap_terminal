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

    SIGTERM -> supervisor.stop_workers_together() for all three, which reads each pid file,
               signals, and POLLS FOR ABSENCE -> then gunicorn is asked to stop ->
               then this process exits with what the stop actually established.

THAT ORDER IS WHAT THIS FILE REQUESTS AND NOT WHAT THE CONTAINER DOES, measured on
the operator's host 2026-10-11 from `docker compose logs --timestamps`:

    15:25:29.950  [10] [INFO] Handling signal: term          gunicorn's master
    15:25:29.950  signal 15 received. Stopping WORKERS FIRST  this file
    15:25:30.053  [10] [INFO] Shutting down: Master           gunicorn GONE, +0.1s
    15:25:45.004  stop deposit_watcher    stopped             the workers, +15.05s

gunicorn received SIGTERM at the SAME MILLISECOND as this process, fifteen seconds
before the line below would have asked it to. The cause is two lines neither of which
is in this file: docker/web.Dockerfile's `ENTRYPOINT ["dumb-init", "--"]` passes no
--single-child, and the gunicorn Popen below passes no start_new_session -- so
gunicorn shares this process's group and dumb-init's default signals the whole group.

THE REAL BEHAVIOR IS THE SAFER OF THE TWO AND IS DELIBERATELY LEFT, which is why this
is a corrected comment rather than a code change. gunicorn dying first means no swap
can be CREATED while the workers wind down; the order this file requests would leave
the page accepting swaps whose payout worker was already gone. What the order below
still buys is the case where gunicorn does NOT take the group signal -- it is
terminate()d and waited for explicitly, so a gunicorn that ignored SIGTERM is still
reaped here rather than SIGKILLed by the runtime.

AND THE TWO LEGS ARE THEREFORE CONCURRENT, NOT SEQUENTIAL. The container's stop is
max(worker grace, gunicorn's own graceful_timeout) rather than their sum -- see
swap_terminal/stop_budget.total_stop_seconds(), which sums them on purpose and says
why.

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

# ROOTLESS, after the sys.path.insert above, same as every root tool in this tree.
#
# WHY db AND config ARE HERE AT ALL, added 2026-10-10 with connect_db()'s refusal.
# db.connect_db() no longer creates a missing file (it manufactured a second database
# out of a mistyped SWAP_DB_PATH on 2026-10-01 and three workers polled it for an
# hour). That refusal is right for every caller EXCEPT the first one on a fresh
# volume -- and on this container the first one is a WORKER, because main() calls
# start_workers() before it execs gunicorn, and gunicorn is what imports
# wsgi -> app -> create_app() -> init_db(). So the database has to exist BEFORE
# start_workers() returns, and this file is the only thing that runs earlier.
#
# prepare_database() calls db.init_db(path) rather than repeating
# executescript(SCHEMA): one copy of "create and migrate" (rule 8). Nothing here
# derives the path -- config.Config does, the same way every other process in the
# container reads it.
import db  # noqa: E402
import stop_budget  # noqa: E402
import supervisor  # noqa: E402
from config import Config  # noqa: E402

#: Where the workers' pid files and logs go. MUST be the volume, not the image's
#: writable layer: a pid file that dies with the container is a pid file that can
#: never be used to prove a stop after a restart, which is the whole point of
#: having one.
RUN_DIR = Path(os.getenv("ST_WORKER_RUN_DIR", str(BASE_DIR / "swap_terminal" / "runtime")))

#: THE TWO GRACES ARE IMPORTED, NOT DEFINED HERE, SINCE 2026-10-11. They used to be
#: two module-level os.getenv() reads in this file, and gunicorn.conf.py separately
#: hardcoded `graceful_timeout = 30` -- a number that had to be smaller than the wait
#: below with nothing making it so, and was not. See swap_terminal/stop_budget.py for
#: the measurement and for the ordering the two must satisfy; the point of the move is
#: that one file now owns the spend and both readers derive from it (rule 8).
#:
#: Re-exported under the same names on purpose: this module's own tests and
#: tests/test_container_stop_fits_its_grace_period.py read entrypoint.WORKER_GRACE_SECONDS
#: and entrypoint.GUNICORN_GRACE_SECONDS, and an operator reading the startup banner
#: sees the same two names the environment variables have.
WORKER_GRACE_SECONDS = stop_budget.WORKER_GRACE_SECONDS
GUNICORN_GRACE_SECONDS = stop_budget.GUNICORN_GRACE_SECONDS

_stopping = False


def announce(lines: list[str]) -> None:
    """Print and flush. Rule 14: output nobody sees during the wait is not output.

    flush=True because this is pid 1 behind a pipe, where Python's block buffering
    holds a startup banner until the buffer fills -- so an operator watching
    `docker compose logs -f` sees nothing at the moment they most need to.
    """
    for line in lines:
        print(line, flush=True)


def prepare_database() -> None:
    """Create and migrate the swap database before a single worker polls it.

    RUNS BEFORE start_workers() AND THAT ORDER IS THE WHOLE POINT. See the `import db`
    comment at the top of this file: the workers reach the database before gunicorn
    does, so if this did not run, a fresh volume would give three workers a
    DatabaseNotFound at startup for a path that was perfectly correct.

    IT ANNOUNCES WHICH OF THE TWO THINGS HAPPENED, because they want different reactions
    from whoever is reading the container log (rule 14 -- "make did-nothing look
    different from did work"):

      CREATED     a database did not exist at this path and now does. On a first
                  deployment that is expected. On a restart it means the volume is not
                  the volume you think it is, and the swaps from yesterday are in
                  another file -- which is precisely the 2026-10-01 failure, now visible
                  on line one instead of invisible for an hour.
      already     the normal restart. The schema is reapplied anyway; it is
      present     CREATE TABLE IF NOT EXISTS plus db.apply_migrations(), both idempotent.

    NO try/except. A database that cannot be created or migrated is not a condition this
    container can serve through, and the traceback names the path. Starting the workers
    anyway would hand them the failure one layer further from its cause.
    """
    path = Path(str(Config.DB_PATH))
    existed = path.is_file()
    db.init_db(path)
    announce([
        f"  database        {path}  <- {'already present' if existed else 'CREATED, it did not exist at this path'}",
        "                  schema applied and migrations run before any worker polls it"
        if existed
        else "                  if you expected existing swaps here, SWAP_DB_PATH or the /data mount is not what you meant",
    ])


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
    "make the absence the assertion". supervisor.stop_workers_together() polls /proc;
    this only reports what it established.

    ONE CALL FOR ALL THREE, AND THAT IS THE WHOLE OF A FIX measured on 2026-10-11.
    This was a `for name in worker_commands(): stop_worker(name)` loop, and
    stop_worker() spends up to WORKER_GRACE_SECONDS proving absence after SIGTERM and
    again after SIGKILL -- so three workers that do not exit promptly cost three
    times the grace. Measured against three processes holding SIG_IGN for SIGTERM:
    6.22s at grace=2s, 3.11x one worker's grace, WHICH AT THE SHIPPED 15s IS 47s FOR
    THE WORKERS ALONE. compose's stop_grace_period is 45s and gunicorn's own 20s
    comes after, so `docker compose down` was SIGKILLed mid-proof and this container
    exited reporting success having proven nothing. The group call spends the grace
    once: 15s worst case, and in the ordinary case where the workers honor SIGTERM,
    well under a second.
    """
    all_gone = True
    results = supervisor.stop_workers_together(
        list(supervisor.worker_commands()), RUN_DIR, grace_seconds=WORKER_GRACE_SECONDS,
    )
    for result in results:
        outcome = result.get("outcome", "unknown")
        announce([f"  stop {result['worker']:18} {outcome}"])
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
        f"  worker grace    {WORKER_GRACE_SECONDS}s  <- one hung chain call ({stop_budget.LONGEST_RPC_TIMEOUT_SECONDS}s, "
        f"ICP_CALL_TIMEOUT) plus {stop_budget.RPC_OVERRUN_MARGIN_SECONDS}s. Must be inside compose stop_grace_period",
        "  starting three workers, then gunicorn.",
        "  ON STOP: all four get SIGTERM TOGETHER, because pid 1 is dumb-init with no",
        "  --single-child and gunicorn shares this process's group. gunicorn exits first",
        "  (~0.1s); the workers stop STARTING new work and finish the unit in hand, and",
        "  their absence is PROVEN by /proc -- by exit if the unit fits in the grace, by",
        "  SIGKILL if it does not. One unit can exceed it: see stop_budget.py.",
    ])
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    prepare_database()

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
