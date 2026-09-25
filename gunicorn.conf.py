"""Gunicorn configuration for the swap_terminal API. Read by `-c`, not imported.

Role: entry point support (the production server's settings; holds no decision
      about swaps, amounts, addresses or fees)
Reads: the process environment -- SWAP_TERMINAL_HOST, SWAP_TERMINAL_PORT,
       GUNICORN_WORKERS, GUNICORN_TIMEOUT_SECONDS, GUNICORN_LOG_LEVEL
Writes: stdout/stderr (access and error logs)
Can move funds: no. Nothing here signs, broadcasts, or names a wallet. The
      process it configures holds wallet RPC credentials, which is why the
      bind default below is loopback and why the hooks refuse to start a
      worker that spawned something during import.
Mainnet-safe: yes to read. Whether the deployment is mainnet-safe is decided by
      the RPC endpoints in the environment, not by this file.

    gunicorn -c gunicorn.conf.py wsgi:app

=============================================================================
THE ONE THING THIS FILE CANNOT CHANGE, AND THE SETTINGS PEOPLE EXPECT TO
=============================================================================

**THE WSGI PROCESS SERVES HTTP AND STARTS NO WORKERS.**
**swap_terminal/supervisor.py OWNS THE WORKERS AND IS THEIR REAPER.**

`workers`, `threads`, the worker class and `preload_app` are tuning for the
first line. None of them affects the second, and none of them is a way to run
the deposit watcher, the payout worker or the reconcile worker.

The reason is CLAUDE.md rule 13 arriving through a door this repository has not
used before. Gunicorn forks N worker processes and EACH ONE IMPORTS THE
APPLICATION. So a background thread or poll loop started at import time, or
inside create_app(), is not started once -- it is started N times, in N
processes, and nothing crashes and nothing logs an error. Rule 13's damage
model is exactly that: "an orphan does not crash anything."

What comes out of it has already been measured in this tree, from CLAUDE.md's
closing section: two payout workers paid the same swap twice -- 2 sends, 1
swap_id, 2 `broadcast` rows, on-chain and final. Those were two processes
started by hand. `workers = 4` would make four the DEFAULT, silently, on every
restart.

`preload_app` IS NOT A FIX FOR THAT, and it is worth saying plainly because it
looks like one. Preloading imports the application once in the master and forks
after, so it feels like "only one copy of the background thread". What actually
happens is that only the forking thread survives `fork()`: a thread started
before the fork does not exist in the children at all. Preloading turns "N
copies running" into "zero copies running, in a parent that has forked away
from them" -- a different wrong answer, not a right one. Either way the thread
does not belong here.

`preload_app` is left at its default (False) below for an unrelated and
ordinary reason, stated at the setting.

=============================================================================
HOW THAT IS ENFORCED RATHER THAN MERELY WRITTEN DOWN
=============================================================================

Two mechanisms, and neither of them is this comment:

  wsgi.py                       snapshots this process's threads and child pids
                                around `import app` and RAISES if either grew.
                                It runs in every worker, because every worker
                                imports wsgi. A worker that spawned something
                                during import never serves a request.
  post_worker_init below        re-runs the same measurement across the fork
                                boundary, which is the half wsgi.py cannot see
                                on its own: work started by gunicorn's own
                                worker bootstrap, after the import, before the
                                first request. It WARNS rather than raises --
                                see the hook for why the two differ.

tests/test_wsgi_spawn_guard.py holds the invariant in the suite, so the check
is not only present at deploy time.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# gunicorn exec's this file rather than importing it as a package member, so the
# swap_terminal/ directory is not on sys.path yet -- the same rootless-import
# gap wsgi.py and tests/conftest.py both work around (CLAUDE.md rule 10).
_REPO_ROOT = Path(__file__).resolve().parent
_APP_ROOT = _REPO_ROOT / "swap_terminal"
if str(_APP_ROOT) not in sys.path:
    sys.path.insert(0, str(_APP_ROOT))

# All three are reachable only because of the sys.path line above; hoisting them
# to the top of the file would break the import it enables, which is what the
# E402 suppressions claim and what a reader can check from these lines. `config`
# is read-only at import: its class body reads the environment and touches
# nothing else.
from config import Config  # noqa: E402
from import_spawn_guard import (  # noqa: E402
    child_pid_snapshot,
    children_are_observable,
    describe_spawn_delta,
    thread_snapshot,
)
from microfortnights import format_duration  # noqa: E402

# --- Where it listens --------------------------------------------------------
#
# The same two environment variables app.py's development server reads, and the
# same loopback default, so that moving from `python3 swap_terminal/app.py` to
# gunicorn does not silently change what the API is reachable from. Nothing in
# this application authenticates a caller; binding it off-box is a decision an
# operator makes on purpose and sees echoed in the banner below.
bind = f"{os.getenv('SWAP_TERMINAL_HOST', '127.0.0.1')}:{os.getenv('SWAP_TERMINAL_PORT', '5000')}"

# --- How many, and of what kind ----------------------------------------------
#
# Sync workers, because every route here is a short request that talks to SQLite
# and to a chain daemon over a blocking `requests` call. Two is a deliberate
# default rather than the usual `2 * cpus + 1`: the ceiling on this application
# is not CPU, it is SQLite's single writer lock, and more workers contending for
# it produces `database is locked` rather than throughput.
#
# CHANGING THIS NUMBER DOES NOT START OR STOP ANY WORKER LOOP. See the header.
workers = int(os.getenv("GUNICORN_WORKERS", "2"))
worker_class = "sync"

# Left at gunicorn's default (False). With preloading off, a worker that fails
# its spawn guard dies on its own rather than taking the master down before it
# ever binds -- which means the operator sees the guard's message in a worker
# log next to a server that is otherwise up, instead of a process that vanished.
# It is NOT off for any reason to do with background threads; see the header,
# where preloading is explicitly not a fix for those.
preload_app = False

# --- Timeouts ----------------------------------------------------------------
#
# Seconds, because this is an interface and not a report (CLAUDE.md rule 6):
# gunicorn parses this integer. The µfn figure appears in the banner, which is
# the thing a human reads.
#
# 60 rather than gunicorn's 30 because a route can make a wallet RPC call whose
# own timeout is 30 (config.Config's *_RPC_TIMEOUT default), and a worker
# timeout at or below the call it is waiting on kills the worker instead of
# surfacing the chain's own failure.
timeout = int(os.getenv("GUNICORN_TIMEOUT_SECONDS", "60"))
graceful_timeout = 30
keepalive = 5

# --- Logging -----------------------------------------------------------------
#
# To stdout/stderr so that whatever supervises gunicorn owns the log file. The
# access log deliberately does NOT include the query string beyond gunicorn's
# default request line; nothing in this API takes a secret in a URL today, and
# the default is the conservative side of that.
accesslog = "-"
errorlog = "-"
loglevel = os.getenv("GUNICORN_LOG_LEVEL", "info")

# Total seconds gunicorn may spend booting before this file's banner is stale.
# Only used to render the banner's own elapsed figure.
_CONFIG_LOADED_AT = os.times().elapsed


def on_starting(server):
    """Announce the deployment before anything binds (CLAUDE.md rule 14).

    "Announce before, not only after. Print the target and the scale up front."
    A gunicorn master that is binding and one that is wedged on a DNS lookup
    look identical from outside, and the resolution for the second is Ctrl-C.

    Every parameter that decides the answer is echoed -- bind, worker count,
    timeout, database -- so the pasted block is self-describing a day later.
    Nothing here prints an RPC username or password: those are one key away in
    Config.RPC and a helpful status line is how they reach a log file.
    """
    host = bind.rsplit(":", 1)[0]
    exposed = host not in {"127.0.0.1", "localhost", "::1", ""}
    lines = [
        "swap_terminal API starting under gunicorn",
        f"  bind            {bind}  <- 127.0.0.1 is the default; anything else is reachable off-box",
        f"  workers         {workers} x {worker_class}  <- HTTP workers ONLY. This starts no deposit/payout/reconcile loop.",
        f"  worker timeout  {format_duration(timeout)}",
        f"  database        {Config.DB_PATH}",
        "  debugger        off  <- gunicorn never runs the Werkzeug console; app.py's __main__ block is not reached",
        "  swap workers    NOT STARTED HERE. `python3 swap_terminal/supervisor.py status` is where they live.",
        f"  child pids      {'observable' if children_are_observable() else 'NOT observable on this platform -- the fork half of the spawn guard cannot report'}",
    ]
    if exposed:
        lines.append(
            f"  WARNINGS:\n    ! binding {host} exposes this API beyond the local machine; nothing in this app authenticates a caller."
        )
    else:
        lines.append("  warnings        (none)")
    for line in lines:
        print(line, flush=True)


def post_worker_init(worker):
    """Re-measure across the fork, and say something either way (rule 14).

    wsgi.py's guard brackets `import app` INSIDE this worker, so it already
    covers the import. What it cannot see is the window gunicorn owns: between
    the fork and the first request, where gunicorn's own bootstrap and any
    future `when_ready`/`post_fork` hook run. This closes that window.

    WHY THIS WARNS WHERE wsgi.py RAISES, and the difference is deliberate. At
    import time the only plausible cause of a new thread is our own code, and
    failing closed costs an HTTP worker. Here, a new thread may legitimately
    belong to gunicorn -- the `gthread` worker class starts a pool by design,
    and so does a future switch to it. Refusing to boot on gunicorn's own
    threads would be a guard that cries wolf, and CLAUDE.md rule 19's test for
    a patch cuts the other way too: a check somebody disables is worse than a
    check that reports.

    So it prints, with the same wording as the raising version, and the line
    says what a reader should do about it. An operator who sees this has a
    background thread running in N processes and needs to know which.

    "Never let an empty result print nothing" (rule 14): the clean case says so
    explicitly rather than staying silent, because silence here is ambiguous
    between "nothing spawned" and "the hook never ran".
    """
    findings = describe_spawn_delta(
        worker.__dict__.get("_st_threads_before", set()),
        thread_snapshot(),
        worker.__dict__.get("_st_children_before", set()),
        child_pid_snapshot(),
    )
    if findings:
        print(f"  worker pid={worker.pid} SPAWN GUARD: background work is running in this HTTP worker:", flush=True)
        for line in findings:
            print(f"    ! {line}", flush=True)
        print(
            "    Under gunicorn this runs once PER WORKER. The WSGI process serves HTTP and starts no workers;\n"
            "    swap_terminal/supervisor.py owns the three polling loops and is their reaper (CLAUDE.md rule 13).",
            flush=True,
        )
    else:
        print(
            f"  worker pid={worker.pid} ready  spawn guard: 0 threads, 0 child processes started  <- expected 0/0; "
            "anything else means N copies of a background loop",
            flush=True,
        )


def post_fork(server, worker):
    """Snapshot immediately after the fork, for post_worker_init to compare to.

    Split from the hook that reports because gunicorn calls post_fork first and
    post_worker_init second, and the delta between them is the window described
    there. Stashed on the worker object rather than in a module global because
    the master process runs this file too, and a global would be shared state
    across a fork boundary -- which is the class of bug this whole file is about.
    """
    worker.__dict__["_st_threads_before"] = thread_snapshot()
    worker.__dict__["_st_children_before"] = child_pid_snapshot()
