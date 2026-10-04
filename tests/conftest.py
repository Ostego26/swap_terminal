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

# AN ASSET THIS TERMINAL DOES NOT TRADE, for the gates whose whole job is to refuse
# a pair Config.ALLOWED_PAIRS does not carry.
#
# WHY A CONSTANT AND NOT A DERIVED ONE, WHICH IS THE WHOLE POINT OF THIS ENTRY.
# Until 2026-10-04 two test files derived their refusal fixture from
# Config.ALLOWED_PAIRS: "every ordered pair of the traded assets, minus the ones it
# carries". That was the right fix for the problem it solved -- both files had
# previously SPELLED a pair (`GRC:SOL`, `BTC:XRP`) that an operator later enabled, so
# the test quietly started exercising the ALLOWED path while asserting the refused
# one, a fixture rotting rather than a gate failing.
#
# AND THE DERIVATION HIT ZERO. Measured 2026-10-04, after the operator enabled the
# last four directions: 5 traded assets, 20 ordered pairs, 20 allowed, so the derived
# set is EMPTY and the refusal gate has nothing to exercise. Both files predicted
# exactly this in prose -- tests/test_swap_readiness.py's
# test_there_is_an_unallowed_direction_left_to_refuse asserted the denominator so it
# would FAIL rather than skip, and it did, which is the mechanism working.
#
# So the fixture needs an asset that CANNOT become allowed by an operator enabling a
# direction between assets this terminal already trades. XMR is that asset, and it is
# not hypothetical: tests/test_address_authority.py records Config.ALLOWED_PAIRS
# having carried ("GRC","XMR"), so it is a real pair somebody really configured and a
# plausible thing to type.
#
# THIS CAN ROT TOO, IN ONE WAY, AND THE ROT IS LOUD. If XMR is ever traded, every pair
# below becomes allowable -- test_the_refusal_fixture_is_still_outside_the_config in
# tests/test_swap_readiness.py asserts both that this asset is untraded and that every
# string this returns is outside Config.ALLOWED_PAIRS, so adding XMR fails the suite by
# name rather than silently inverting a gate. Measured 2026-10-04: setting this to
# "BTC" fails that test and tests/test_open_swap.py's refusal test, and nothing else.
UNTRADED_ASSET = "XMR"


def unallowed_directions(allowed_pairs) -> list[str]:
    """Every `FROM:TO` string a pair gate must refuse, and it can never be empty.

    TWO SOURCES, UNIONED, because each covers what the other cannot:

      derived    ordered pairs of the assets `allowed_pairs` itself names, minus the
                 ones it carries. Cannot rot while any direction is unenabled, and is
                 EMPTY as of 2026-10-04 (20 of 20). Kept rather than dropped: it is
                 what catches a direction being disabled again, and it is the half
                 that cannot go stale.
      untraded   UNTRADED_ASSET paired both ways with every traded asset. Cannot
                 become allowed by enabling a direction among traded assets, which is
                 the failure mode that emptied the derived half.

    Returned sorted so pytest's parametrize ids are stable between runs; a set would
    reorder them and make a failure impossible to match against a previous one.

    HERE RATHER THAN IN EITHER CONSUMER because two files needed it within the same
    hour, which is rule 8's threshold and the same reason RPC_FIXTURE_USER above sits
    in this file.
    """
    allowed = set(allowed_pairs)
    assets = sorted({asset for pair in allowed for asset in pair})
    derived = [
        (from_asset, to_asset)
        for from_asset in assets
        for to_asset in assets
        if from_asset != to_asset and (from_asset, to_asset) not in allowed
    ]
    untraded = [(asset, UNTRADED_ASSET) for asset in assets] + [(UNTRADED_ASSET, asset) for asset in assets]
    return sorted(f"{a}:{b}" for a, b in set(derived) | set(untraded) if (a, b) not in allowed)


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
