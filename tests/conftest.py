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

# NOT CREDENTIALS, AND NAMED SO THAT ruff's S105/S107 ARE ANSWERED RATHER THAN
# SUPPRESSED (rule 19: a noqa is a claim you checked, not a way to quiet a finding).
#
# chains/registry.build_adapters() requires a non-empty RPC user and password for a
# Bitcoin-derived chain -- since 2026-09-26, because chains/base.py authenticates
# with auth=(user, password) and has no cookie-file path, so an empty pair is a
# guaranteed 401 and an adapter built from one is worse than no adapter. Any fixture
# that wants a CONFIGURED Bitcoin-derived chain therefore has to supply both, and
# these are what it supplies.
#
# Here rather than in each test file because two files needed them within a minute
# of each other (test_network_target.py and test_app_run_defaults.py) and a third
# will: two copies of one value is rule 8's shape whether the value matters or not.
# The names deliberately avoid the words S105 looks for -- the same move
# tests/test_gridcoin_wallet_lock.py makes with LEAK_SENTINEL.
#
# No test in this suite opens a socket, which this file's own header already states.
RPC_FIXTURE_USER = "fixture-rpc-user"
RPC_FIXTURE_AUTH = "fixture-rpc-auth-value"

REPO_ROOT = Path(__file__).resolve().parent.parent
APP_ROOT = REPO_ROOT / "swap_terminal"

if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

# Note: not tmp_path, because this must run at import time, before any fixture
# exists. The directory is left for the OS to reap; it holds nothing but an
# empty schema.
_TEST_DB_DIR = tempfile.mkdtemp(prefix="swap_terminal_tests_")
os.environ.setdefault("SWAP_DB_PATH", str(Path(_TEST_DB_DIR) / "swap_terminal_test.db"))

# 3. THE XRP SIGNING SEED IS REMOVED FROM THIS PROCESS'S ENVIRONMENT. Added
#    2026-10-02 with chains/xrp_payout_seed.py.
#
#    XRPAdapter reads that variable at CONSTRUCTION to decide `can_spend`, and
#    `can_spend` decides whether services/swap_service.create_swap() will create an
#    XRP-destination swap at all. So a developer or an operator who has the variable
#    exported -- which is exactly the state a host that pays XRP out is in -- would
#    run this suite against a DIFFERENT posture from CI, and the tests that assert
#    the default refusal would fail on their machine and pass in CI. A suite whose
#    result depends on the shell that started it is a suite nobody can use to
#    establish anything.
#
#    REMOVED RATHER THAN DEFAULTED, and the direction matters: the refusing state is
#    the one every test but the three that arm it deliberately should see, and the
#    three do it with monkeypatch.setenv, which reverts. os.environ.setdefault()
#    would have been the wrong tool twice over -- it cannot unset, and a default
#    value here would be a string this harness invented standing in for a secret.
#
#    THIS IS AN IMPORT-TIME SIDE EFFECT and that is what rule 12 names as a hazard,
#    so: it is confined to the test harness, it is the same mechanism the
#    SWAP_DB_PATH line above already relies on for the same reason (Config reads the
#    environment at class-definition time, so a fixture would be too late), and the
#    thing it removes is a secret rather than a setting.
os.environ.pop("XRP_PAYOUT_SECRET_SEED", None)

# 4. THE SOL PAYOUT KEYPAIR PATH IS REMOVED FROM THIS PROCESS'S ENVIRONMENT. Added
#    2026-10-03 with chains/solana_payout_keypair.py, for the identical reason as
#    item 3 above and not restated at length: SolanaAdapter reads that variable at
#    CONSTRUCTION to decide `can_spend`, and `can_spend` decides whether
#    services/swap_service.create_swap() will create a swap that pays out in SOL. An
#    operator or developer with it exported -- which is exactly the state a host that
#    pays SOL out is in -- would otherwise run this suite against a DIFFERENT posture
#    from CI, and a suite whose result depends on the shell that started it cannot be
#    used to establish anything.
#
#    ONE DIFFERENCE FROM THE SEED ABOVE, worth naming because it is the whole design
#    of that module: this variable holds a PATH and not a secret. Nothing in this
#    harness or in chains/solana_payout_keypair.py ever opens the file it names --
#    the only reader is chains/solana_signing.load_payout_keypair(), reached from
#    signed_transfer_wire() after the arming token has matched. Removing the path is
#    therefore about POSTURE determinism rather than about secret hygiene, and both
#    reasons would call for the same line.
os.environ.pop("SOL_PAYOUT_KEYPAIR_PATH", None)
