"""Shared pytest setup for the swap_terminal suite.

Role: test harness support (path and environment setup for the real modules)
Reads: nothing at import beyond its own location
Writes: SWAP_DB_PATH is pointed at a per-session temp directory so that
        importing the application never creates or touches a real
        swap_terminal.db
Can move funds: no
Mainnet-safe: yes -- no test in this suite opens a socket to any chain. Every
        adapter used in a test is a stub that records calls.

Two things have to happen before any test imports application code, and both
are here rather than in the tests because import order is the whole point.

1. swap_terminal/ goes on sys.path. The application imports its own modules
   rootlessly (`from config import Config`, `from db import db_session`) rather
   than as a package, so it only imports when its own directory is importable.
   That is CLAUDE.md rule 10's layout gap, and papering over it by rewriting
   every import in the tree would be a large diff with no behavioral benefit --
   so the harness adapts to the tree instead.

2. SWAP_DB_PATH is set to a temp path. `config.Config` reads the environment at
   CLASS DEFINITION time, which is import time, so setting it inside a test
   would be too late: the value is already baked in. Importing `app` also runs
   `create_app()` at module scope, which calls `init_db()` and therefore
   CREATES the database file. Pointing it somewhere disposable first is what
   keeps `python3 -m pytest` from writing a stray swap_terminal.db into the
   checkout.
"""

import os
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
APP_ROOT = REPO_ROOT / "swap_terminal"

if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

# Note: not tmp_path, because this must run at import time, before any fixture
# exists. The directory is left for the OS to reap; it holds nothing but an
# empty schema.
_TEST_DB_DIR = tempfile.mkdtemp(prefix="swap_terminal_tests_")
os.environ.setdefault("SWAP_DB_PATH", str(Path(_TEST_DB_DIR) / "swap_terminal_test.db"))
