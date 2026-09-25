"""The WSGI entry point gunicorn imports. Serves HTTP; starts no workers.

Role: entry point (the production server's application object)
Reads: swap_terminal/config.py (the environment, at import), and through
       create_app() the database file named by SWAP_DB_PATH
Writes: swap_terminal.db's schema, via create_app()'s init_db() -- idempotent,
       CREATE TABLE IF NOT EXISTS, and it is what app.py already did at import
Can move funds: no. This process never calls sendtoaddress and never signs. It
       derives deposit addresses and records intent, exactly as app.py does;
       workers/payout_worker.py is the only thing in this tree that broadcasts.
       Note that it HOLDS wallet RPC credentials, which is why the app's
       debugger defaults to off and why nothing here re-enables it.
Mainnet-safe: yes to import. Whether the deployment is mainnet-safe is decided
       by the RPC endpoints in the environment, not by this file.

IT LIVES AT THE REPOSITORY ROOT ON PURPOSE (CLAUDE.md rule 10).

"The entry point lives at the ROOT. If an operator, a cron entry or a launcher
names it, it belongs where it can be found without knowing the layout." A
gunicorn command line names this file, so it is at the root beside
regtest_htlc_verify.py, migrate_deposit_vouts.py and gunicorn.conf.py, rather
than buried next to the module it imports.

WHAT THIS FILE IS FOR, GIVEN THAT app.py ALREADY DEFINES `app`.

gunicorn could import `app:app` directly. It does not, and the difference is
the guard below. `gunicorn app:app` would work on the day it was typed and
would say nothing at all on the day somebody adds a poll loop to create_app().
Importing through here means the import is MEASURED -- see
swap_terminal/import_spawn_guard.py for the full reasoning, and the short form
is this:

    gunicorn forks N workers, each of which imports this module. A background
    thread started at import is therefore not started once. It is started N
    times, in N processes, and nothing anywhere says so.

That is CLAUDE.md rule 13 arriving through a door the repository has not used
before. The rule's damage model -- "an orphan does not crash anything" -- is
exactly this shape: the extra copies work perfectly, and what comes out is two
payout broadcasts for one swap, which this repository has already measured once
(2 sends, 1 swap_id, 2 'broadcast' rows).

THE DIVISION OF LABOR, WHICH NO SETTING CHANGES:

    this process (gunicorn)   serves HTTP. Starts no workers, ever.
    supervisor.py             owns the three polling loops and is their reaper.

`preload_app`, `workers`, `threads` and the worker class are all tuning for the
first line and none of them affects the second. In particular `preload_app`
does NOT make a background thread safe: a thread started before the fork does
not survive into the children (only the forking thread does), so preloading
turns "N copies running" into "zero copies running, in a parent that then forks
away from them" -- a different wrong answer, not a right one.

RUNNING IT:

    gunicorn -c gunicorn.conf.py wsgi:app

app.py's `__main__` block is still there and is still the local-development
path (`python3 swap_terminal/app.py`). Under gunicorn it is never reached,
which is strictly better than relying on its defaults: see README.md for which
of the two an operator is actually running and what each one binds.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
APP_ROOT = REPO_ROOT / "swap_terminal"

# The application imports its own modules rootlessly (`from config import
# Config`), so swap_terminal/ has to be importable before `import app` can
# work. tests/conftest.py does the identical thing for the same reason, and
# chains/base.py does it one directory further down. That is CLAUDE.md rule
# 10's layout gap; adapting to it here is a two-line entry point, and closing
# it properly means rewriting every import in the tree.
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

from import_spawn_guard import (  # noqa: E402 -- the sys.path line above is what makes this importable at all; moving it up would break the import it enables.
    assert_nothing_spawned,
    child_pid_snapshot,
    thread_snapshot,
)

# Snapshot BEFORE the application is imported. Everything that `import app`
# transitively imports -- config, db, chains, routes, services -- is inside the
# window these two pairs of snapshots bracket.
_THREADS_BEFORE = thread_snapshot()
_CHILDREN_BEFORE = child_pid_snapshot()

# Deliberately after the snapshot: measuring this import is the entire point of
# the module, so it cannot be hoisted to the top with the others (E402), and it
# is the module-level `app = create_app()` inside it that is being measured.
import app as _application_module  # noqa: E402

app = _application_module.app

assert_nothing_spawned(
    _THREADS_BEFORE,
    thread_snapshot(),
    _CHILDREN_BEFORE,
    child_pid_snapshot(),
    context="importing the swap_terminal WSGI application",
)

# `gunicorn wsgi:app` resolves against this name. It is assigned above rather
# than imported directly so that the assertion runs BETWEEN the import and the
# name being usable -- a gunicorn worker that trips the guard raises before it
# can serve a single request.
__all__ = ["app"]
