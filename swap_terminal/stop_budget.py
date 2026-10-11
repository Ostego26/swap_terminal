"""How long this container's stop is allowed to take, and in what order it spends it.

Role: submodule (decisions only -- every function here is pure, and the two
      module-level constants are environment reads with no side effect)
Reads: ST_WORKER_GRACE_SECONDS and ST_GUNICORN_GRACE_SECONDS from the environment.
       Nothing else: no file, no socket, no database.
Writes: nothing. Nothing is signaled from this file and no process is started.
Can move funds: no. There is no chain call, no RPC and no send path here.
Mainnet-safe: yes to import and yes to call.

WHY THIS FILE EXISTS, and it is two defects measured on 2026-10-11 rather than a
tidy idea. Both are the same shape: a number in one file that another file's number
has to agree with, and nothing making them agree.

THE FIRST ONE OVERRAN THE CONTAINER'S WHOLE STOP. docker-compose.web.yml's
`stop_grace_period: 45s` was chosen by a comment doing the arithmetic as
"ST_WORKER_GRACE_SECONDS (15) plus the gunicorn grace (20)". The 15 was per worker
and the stop ran them serially, so the real worst case was 3.11x that -- measured
against three processes holding SIG_IGN for SIGTERM -- or 47s for the workers alone
and ~67s with gunicorn. The runtime SIGKILLed the entrypoint mid-proof and the
container exited reporting success having proven nothing. Fixed by
supervisor.stop_workers_together(), which spends the grace ONCE for the whole set.

THE SECOND ONE IS WHY THE CONSTANTS MOVED HERE. gunicorn.conf.py set
`graceful_timeout = 30` and docker/web_workers_entrypoint.py waited
GUNICORN_GRACE_SECONDS = 20 before SIGKILLing gunicorn. NOTHING RECONCILED THEM --
measured by grepping the whole tree for `graceful_timeout`, which appears in exactly
one file. So the entrypoint killed gunicorn TEN SECONDS BEFORE gunicorn was
configured to stop waiting on its own workers, and a request in flight longer than
20s could never finish. Worse, the entrypoint's own log line for it reads

    gunicorn still alive after 20.0s; killing it

which states a defect in gunicorn for doing exactly what it was told. That is the
wrong-comment bug (rule 16) wearing a log line: a reader acting on it would go
looking at gunicorn.

THE ORDERING THIS FILE ENFORCES, and it is the whole point:

    gunicorn's own graceful_timeout   <   the entrypoint's wait for gunicorn
    the entrypoint's whole stop       <   compose's stop_grace_period

Each inner deadline must fire BEFORE the outer one, with margin. An inner deadline
equal to its outer one is a race, not a budget: the two fire together and which wins
is scheduling. So gunicorn_graceful_timeout() subtracts a named margin rather than
returning the wait itself.

WHAT THIS FILE DELIBERATELY DOES NOT OWN. `stop_grace_period` stays in
docker-compose.web.yml, because it is the RUNTIME's deadline rather than this
program's -- compose enforces it with SIGKILL and no Python can change that. The
split is: compose owns the ceiling, this file owns the spend, and
tests/test_container_stop_fits_its_grace_period.py asserts the spend fits under the
ceiling by MEASURING the spend rather than by reading either number's comment.
"""

from __future__ import annotations

import os

#: How long the workers get, TOGETHER, to exit before the supervisor escalates to
#: SIGKILL. Spent ONCE for the whole set rather than once per worker, which is what
#: supervisor.stop_workers_together() changed and what the 3.11x above measured.
WORKER_GRACE_SECONDS = float(os.getenv("ST_WORKER_GRACE_SECONDS", "15"))

#: How long docker/web_workers_entrypoint.py waits for gunicorn to exit, AFTER the
#: workers are proven gone, before it SIGKILLs gunicorn.
GUNICORN_GRACE_SECONDS = float(os.getenv("ST_GUNICORN_GRACE_SECONDS", "20"))

#: How much earlier gunicorn's own graceful shutdown must finish than the wait above
#: it. NOT tuning: it is the difference between a handoff and a race. gunicorn on
#: SIGTERM gives its workers up to its `graceful_timeout`, then kills them itself and
#: exits cleanly -- so it needs to reach that point while the entrypoint is still
#: waiting, or the entrypoint SIGKILLs it mid-shutdown and no clean exit code is ever
#: produced. Five seconds because killing two sync workers and closing a listening
#: socket is sub-second work and the margin only has to cover a loaded host.
GUNICORN_HANDOFF_MARGIN_SECONDS = 5.0


def gunicorn_graceful_timeout(
    wait_seconds: float = GUNICORN_GRACE_SECONDS,
    margin_seconds: float = GUNICORN_HANDOFF_MARGIN_SECONDS,
) -> int:
    """What gunicorn's `graceful_timeout` must be, given how long the entrypoint waits.

    AN INT because gunicorn parses this one as an integer number of seconds. Seconds
    rather than microfortnights throughout: this is an interface, not a report (rule
    6), and the µfn figure belongs in the banner a human reads.

    FLOORED AT 1 RATHER THAN ALLOWED TO GO NEGATIVE OR ZERO. A zero or negative
    graceful_timeout tells gunicorn to kill its workers immediately, which would turn
    a misconfigured margin into every in-flight request being dropped -- silently, and
    only during a stop, which is the hardest moment to notice anything. One second is
    still wrong if someone sets a one-second wait, but it is wrong in the direction
    that finishes requests rather than the one that drops them, and
    tests/test_container_stop_fits_its_grace_period.py asserts the ordering holds at
    the SHIPPED values rather than trusting this floor to be unreachable.
    """
    return max(1, int(wait_seconds - margin_seconds))


def total_stop_seconds(
    worker_multiplier: float = 1.0,
    worker_grace: float = WORKER_GRACE_SECONDS,
    gunicorn_grace: float = GUNICORN_GRACE_SECONDS,
) -> float:
    """The container's whole worst-case stop, in seconds.

    `worker_multiplier` is how many times one worker's grace the worker stop actually
    costs, and it is a PARAMETER rather than a constant because the only honest source
    for it is a measurement. It was 3.11 when the stop was serial and is 1.05 now --
    see tests/test_container_stop_fits_its_grace_period.py, which measures it against
    processes that ignore SIGTERM and passes the result in here.

    IT DEFAULTS TO 1.0, WHICH IS THE CLAIM AND NOT A SAFE GUESS: the group stop spends
    the grace once for the whole set, so one is the designed answer and anything above
    it is the defect. A default of, say, 3.0 would make this function agree with a
    serial stop and quietly stop detecting one.
    """
    return worker_multiplier * worker_grace + gunicorn_grace
