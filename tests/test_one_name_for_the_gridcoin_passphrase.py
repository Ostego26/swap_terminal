"""Both suites must ask for Gridcoin's passphrase under the name the arming surface sets.

Role: submodule (tests)
Reads: chains/wallet_lock.passphrase_env_var, chains/gridcoin_wallet_lock's constant,
       .env.example, every docker-compose*.yml, and the two HTLC call sites
Writes: nothing
Can move funds: no. NO PASSPHRASE IS READ, SET OR PRINTED anywhere in this file. Every
       assertion is about a variable NAME; the one place a value appears it is the
       literal string "sentinel-not-a-passphrase", seeded into a monkeypatched
       environment and compared by identity
Mainnet-safe: yes. No adapter is constructed against a daemon, no socket is opened

WHY IT EXISTS. Measured 2026-10-11: Gridcoin's wallet passphrase had TWO variable names
and only one of them was configurable.

    GRIDCOIN_WALLET_PASSPHRASE   28 .py files, 4 .env.example lines, 4 compose files.
                                 What `configure_env.py --arm grc` and
                                 docker-compose.web.armed-grc.yml write.
    GRC_WALLET_PASSPHRASE        built by atomic_swap.py as
                                 `f"{asset}_WALLET_PASSPHRASE"`. Zero compose files,
                                 zero .env.example lines.

So the HTLC driver refused with "GRC_WALLET_PASSPHRASE is not set" at an operator who
HAD armed GRC, naming a variable nothing in the tree had ever mentioned -- and
modules/atomic_grc_client.py read the same absent name in an IMPORT-TIME default
argument, so its value was always "" and the unlock was silently skipped.

WHERE IT CAME FROM, in the operator's words: "you got confused and were trying to
integrate my gui wallet for grc testnet instead of doing what i said which you finally
did and was build it's own daemon from the code." The daemon built from source is the
`grc-desk` service. The second name was debris from an integration that was not asked
for, which is why it is being removed rather than reconciled.

THE GENERIC PATTERN SURVIVES FOR BTC AND LTC and that is asserted below rather than
left implicit. Their wallets can be encrypted and nothing configures a passphrase for
either, so `<CHAIN>_WALLET_PASSPHRASE` is the honest answer for them; only GRC had a
specific name already in use by 36 sites.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "swap_terminal"))

from chains import gridcoin_wallet_lock  # noqa: E402
from chains.wallet_lock import passphrase_env_var  # noqa: E402

#: Not a passphrase. Named so a secret scanner reading this file finds a constant whose
#: name says what it is, and so the assertion below can compare by identity.
SENTINEL = "sentinel-not-a-passphrase"


def test_grc_resolves_to_the_name_the_arming_surface_actually_sets():
    """passphrase_env_var("GRC") must be the constant gridcoin_wallet_lock owns.

    MUTATION: return f"{chain}_WALLET_PASSPHRASE" for GRC too and this fails -- which is
    the state the tree was in until 2026-10-11.
    """
    assert passphrase_env_var("GRC") == gridcoin_wallet_lock.WALLET_UNLOCK_ENV_VAR
    assert passphrase_env_var("GRC") == "GRIDCOIN_WALLET_PASSPHRASE"


@pytest.mark.parametrize("chain", ["BTC", "LTC"])
def test_the_other_chains_keep_the_generic_convention(chain):
    """BTC and LTC must still be <CHAIN>_WALLET_PASSPHRASE.

    Asserted so that "fix the GRC collision" cannot quietly become "give every chain a
    bespoke name". Nothing configures a passphrase for either, so the obvious convention
    is the most useful thing a refusal can name.
    """
    assert passphrase_env_var(chain) == f"{chain}_WALLET_PASSPHRASE"


def test_the_resolved_grc_name_is_one_a_deployment_can_actually_set():
    """It must appear in .env.example AND in at least one compose file.

    THIS IS THE ASSERTION THE WHOLE DEFECT NEEDED. Both halves of the old split were
    internally consistent -- the driver asked for a name, the error message printed that
    name -- and what was missing was any check that the name could be supplied. A
    variable no file in the configuration surface mentions cannot be set by an operator
    following the tree's own instructions.
    """
    name = passphrase_env_var("GRC")
    pattern = re.compile(rf"\b{re.escape(name)}\b")

    env_example = REPOSITORY_ROOT / ".env.example"
    assert pattern.search(env_example.read_text()), (
        f"{name} is what the code asks for and .env.example never mentions it, so an "
        f"operator reading the template cannot learn to set it"
    )
    composes = [
        path.name for path in sorted(REPOSITORY_ROOT.glob("docker-compose*.yml"))
        if pattern.search(path.read_text())
    ]
    assert composes, (
        f"{name} appears in no compose file, so no deployment passes it to a container"
    )


def test_the_htlc_driver_no_longer_builds_the_name_itself():
    """atomic_swap.py must call the resolver, not spell an f-string.

    Asserted on the source because the alternative is running the driver, which needs a
    daemon. The narrow thing checked is the one that regressed: an f-string that builds
    a passphrase variable name from an asset.
    """
    source = (REPOSITORY_ROOT / "atomic_swap.py").read_text()
    code = [line for line in source.splitlines() if not line.strip().startswith("#")]
    offenders = [line.strip() for line in code if '_WALLET_PASSPHRASE"' in line and "f\"" in line]
    assert offenders == [], (
        f"atomic_swap.py builds a passphrase variable name itself: {offenders}. It must go "
        f"through chains/wallet_lock.passphrase_env_var() so this file and "
        f"modules/atomic_grc_client.py cannot disagree."
    )
    assert "passphrase_env_var(asset)" in source, (
        "atomic_swap.py does not call passphrase_env_var(asset)"
    )


def test_the_grc_client_reads_the_environment_at_construction_not_at_import(monkeypatch):
    """Setting the variable AFTER the module is imported must still reach the client.

    THIS IS THE IMPORT-TIME DEFECT, MEASURED RATHER THAN READ. The old signature was
    `wallet_passphrase: str = os.environ.get("GRC_WALLET_PASSPHRASE", "")`, evaluated
    once when the class body executed -- so a process that loaded its .env after
    importing this module got "" and skipped the unlock silently. This test imports the
    module first and sets the variable second, which is the order that used to fail.

    MUTATION: put the value back in the default argument and this fails, because by the
    time monkeypatch runs the default is already bound.

    NO REAL PASSPHRASE IS INVOLVED. SENTINEL is a fixture string, and the assertion is
    that the client holds the value it was given -- not that any particular credential
    is correct.
    """
    from modules.atomic_grc_client import (  # noqa: PLC0415 -- imported INSIDE the test: the import must precede monkeypatch.setenv for this to measure the import-time read at all
        GRCClient,
    )

    monkeypatch.setenv(passphrase_env_var("GRC"), SENTINEL)
    client = GRCClient("http://127.0.0.1:0", "u", "p")
    assert client.wallet_passphrase == SENTINEL, (
        "the client did not pick up the variable set after import, so it is still being "
        "read in a default argument at import time"
    )


def test_an_explicit_empty_passphrase_is_distinguishable_from_an_unset_one(monkeypatch):
    """"" must mean "no passphrase", and None must mean "read the environment".

    The old signature could not express both: a single empty-string default made "a
    caller stating there is no passphrase" and "a process that had not loaded its .env"
    the same input, which is exactly why the import-time read never surfaced as a
    mismatch.
    """
    from modules.atomic_grc_client import GRCClient  # noqa: PLC0415 -- same reason as above

    monkeypatch.setenv(passphrase_env_var("GRC"), SENTINEL)
    explicit = GRCClient("http://127.0.0.1:0", "u", "p", wallet_passphrase="")
    assert explicit.wallet_passphrase == "", (
        "an explicitly empty passphrase was overridden by the environment, so a caller "
        "cannot say 'this wallet has none'"
    )
    implicit = GRCClient("http://127.0.0.1:0", "u", "p")
    assert implicit.wallet_passphrase == SENTINEL, "None did not read the environment"
