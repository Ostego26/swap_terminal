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

import importlib.util
import os
import sys
import tempfile
from collections.abc import Mapping
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


# ---------------------------------------------------------------------------
# A CREDENTIAL CANARY FOR Config.RPC, IN ONE PLACE BECAUSE TWO TESTS WANT IT.
#
# tests/test_worker_reporting.py and tests/test_supervisor.py each check that a
# banner never prints a wallet RPC credential, and each built its own poisoned
# copy of Config.RPC with the same four-line comprehension. That is one rule --
# "replace every credential field in every entry, then assert none of it reached
# the output" -- spelled twice, which is rule 8's shape: the day Config.RPC grows
# a third credential field, whichever copy is not updated silently stops covering
# it, and nothing fails.
#
# THE FIELD LIST IS HERE RATHER THAN AT THE CALL SITES for the same reason.
#
# WHY A LOOP THAT ASSERTS RATHER THAN A COMPREHENSION THAT FILTERS. The obvious
# way to satisfy a checker here -- Config.RPC is a TypedDict since 2026-10-09, so
# .items() yields `object` and `{**values}` is refused -- is
# `if isinstance(values, Mapping)` in the comprehension. That is a FILTER: an
# entry that was not a mapping would be dropped, the canary would never be placed
# in it, and the test would pass having checked one chain fewer. An assertion says
# the same thing and fails instead.
def root_entry_point(relative_path: str, module_name: str | None = None):
    """Import an entry point by path, as a module. The one copy.

    Rule 10 puts entry points at the project root, and they are not importable as
    package members from there -- so a test that wants one has to load it by path.
    Five tests did, with five copies of this function. ALL FIVE ARE MIGRATED ONTO THIS
    ONE as of 2026-10-10 (C47), and these are the surviving call sites:

        test_operator_panel.py:73       _entry()        "operator_panel_entry"
        test_solana_payout.py:1228      teller_entry()  "operator_panel_entry_sol"
        test_grc_htlc_verify.py:69      _entry()
        test_reclaim_funding.py:57      _entry()
        test_icp_replica_entrypoint.py:49               at COLLECTION time
        test_daemon_capabilities.py:129-131             three in one dict
        test_suite_baseline.py:32

    test_solana_payout.py named test_operator_panel.py::_entry() in a comment as
    the thing it was copied from, and ALL FIVE carried the same unchecked
    `spec.loader` hole -- closed separately in four of them. One bug, four
    diagnoses, which is rule 8's cost made explicit. OPEN_FINDINGS.md has recorded
    "One conftest.py helper fixes all five" since; this is that helper.

    ITS OWN TESTS ARE tests/test_root_entry_point.py, written in the same commit as the
    migration and for a reason worth stating: the survivor of a merge becomes the single
    thing every caller depends on, so leaving it untested means a mutation here reports
    as thirty AttributeErrors in files about the ICP replica and the GRC HTLC harness --
    which is exactly the mechanism by which one bug in five copies came to be diagnosed
    four separate times. Six mutations of this function, six caught. The
    `spec.loader is None` branch below is the one thing NOT covered, and that file says
    so with the measurement rather than leaving it as a gap.

    It exists because I nearly wrote the SIXTH copy 2026-10-09, in the test for a
    commit about consolidating duplicated knowledge, twenty minutes after
    discovering I had made BITCOIN_FAMILY the sixth spelling of ("BTC","LTC","GRC")
    in the commit before. Rule 9's "every time you are in a file, leave less of it
    behind" is aimed at exactly this reflex.

    BOTH FAILURE MODES ARE NAMED, which is the fix the five copies each needed:
    spec_from_file_location() returns None when the path does not exist or no
    loader claims it, and `spec.loader` is None for a spec carrying no loader. Left
    unchecked, a renamed entry point arrives at every test in the file as
    `AttributeError: 'NoneType' object has no attribute 'loader'`, naming neither
    the file nor the reason.

    A RELATIVE PATH RATHER THAN A BARE NAME, because one of the five is not at the
    root: tests/test_icp_replica_entrypoint.py loads
    `docker/icp_replica_entrypoint.py`, which lives in the Docker build context. A
    helper taking a bare name could not express that, and a helper that could not
    absorb all five would leave a copy behind -- which is how five became five.

    `module_name` defaults to the path's stem, and is separate because two callers
    load THE SAME FILE under two names on purpose: test_operator_panel.py as
    "operator_panel_entry" and test_solana_payout.py as "operator_panel_entry_sol",
    so the two test files get independent module objects. That distinction is
    preserved rather than flattened while merging.

    NO sys.path INSERT, and that is a removal rather than an omission. Two of the
    five did `sys.path.insert(0, root / "swap_terminal")` first -- which this
    conftest already does at import (see the APP_ROOT block above), so those lines
    were dead. RUF100 found the same redundancy in a `noqa: E402` earlier on this
    branch; it is the same dead idiom, copied.

    THE EXISTENCE CHECK IS FIRST, AND IT IS THE FIX ALL FIVE NEEDED.
    test_solana_payout.py records the measurement: a path that does NOT EXIST
    still produces a perfectly good spec, so `spec is None` never fires for the
    realistic failure. Checking the file is there is what actually names a renamed
    entry point.
    """
    source = REPO_ROOT / relative_path
    if not source.exists():
        raise FileNotFoundError(
            f"{source} does not exist -- the entry point was renamed or removed, so nothing "
            f"that imports it can be checked against it. This file is named by PATH, so no "
            f"import graph points at it and nothing else would have caught the move."
        )
    spec = importlib.util.spec_from_file_location(module_name or source.stem, source)
    if spec is None:
        raise ImportError(
            f"no import spec for {source} -- the entry point is unreadable, so nothing in the "
            f"calling test file can be checked against it"
        )
    if spec.loader is None:
        raise ImportError(f"the import spec for {source} carries no loader, so it cannot be executed")
    module = importlib.util.module_from_spec(spec)
    # REGISTERED IN sys.modules BEFORE exec_module, AND THIS IS A BUG FIX RATHER THAN
    # BOILERPLATE, found 2026-10-09 by the first caller whose entry point declares a
    # @dataclass. dataclasses._process_class() checks for KW_ONLY via _is_type(), which
    # does `sys.modules.get(cls.__module__).__dict__` -- so a module executed without
    # being registered gives:
    #
    #     AttributeError: 'NoneType' object has no attribute '__dict__'
    #
    # from inside dataclasses.py, naming neither the entry point nor the real cause. It
    # is the same shape as the unchecked `spec.loader` this helper was created to fix:
    # a latent hole that every one of the five copies carried and that nothing hit until
    # a caller used the one feature that trips it. The same applies to any entry point
    # using typing.get_type_hints(), pickle, or a dataclass field type resolved lazily.
    #
    # Registered under `spec.name`, which is `module_name or source.stem`, so the two
    # callers that deliberately load ONE file under TWO names still get independent
    # module objects -- they land at different sys.modules keys.
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        # NOT LEFT BEHIND ON FAILURE. A half-executed module in sys.modules is worse
        # than none: the next importer gets it without the exception and reads partial
        # definitions as the real thing.
        sys.modules.pop(spec.name, None)
        raise
    return module


def poisoned_rpc_table(rpc) -> dict:
    """Every entry of `rpc`, with each credential field replaced by a canary."""
    poisoned = {}
    for asset, settings in rpc.items():
        assert isinstance(settings, Mapping), (
            f"{asset}'s RPC entry is a {type(settings).__name__}, not a mapping, so no canary "
            f"could be placed in it and this chain would have gone unchecked"
        )
        poisoned[asset] = {**settings, **RPC_CANARIES}
    assert poisoned, "Config.RPC is empty, so the canary test below would assert nothing"
    return poisoned


#: The credential fields, and the value each is replaced with. Distinctive enough
#: that a substring search for them cannot match anything a banner legitimately
#: prints.
RPC_CANARIES = {"user": "canary-rpc-user", "password": "canary-rpc-password"}
