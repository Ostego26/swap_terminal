"""The harness must refuse an unknown chain and a mainnet datadir, by name, before acting.

Role: test (behavioral, no chain)
Reads: regtest_htlc_verify.py, swap_terminal/regtest/daemons.py, chains/daemon_launch.py
Writes: nothing outside tmp_path
Can move funds: no
Mainnet-safe: yes -- no socket is opened and no real datadir is touched
Live-safe: yes

=========================================================================
THREE REFUSALS, ADDED 2026-11-22 WHILE SCOPING A GRIDCOIN LEG
=========================================================================

None of these was reachable from the CLI on the day it was written: `--chain` offers
("btc","ltc","both"), so no asset but BTC and LTC has ever flowed through any of the three
sites. That is precisely when a guard is cheap, and precisely when the next person adds the
third choice and finds out what was underneath. Each refusal below replaces a path that did
something worse than fail.

    build_real_client()         returned an LTCClient for EVERY asset that is not BTC.
                                There was no `elif asset == "LTC"` and no else-raise, so
                                `--chain grc` would have driven a Gridcoin daemon with a
                                Litecoin client and set PLATFORM_FEE_LTC_ADDRESS to a
                                Litecoin regtest address. A platform fee is a real output
                                to a real address: wrong-chain fund movement, not a crash.

    resolve_chain_config()      raised a bare `KeyError: 'GRC'` from the middle of step 1,
                                naming neither the table, nor the two chains it holds, nor
                                why Gridcoin cannot be one of them.

    wipe_datadir()              had ONE guard -- a liveness probe -- and no asset or
                                network gate. The words "GRC", "Gridcoin" and "mainnet"
                                appeared nowhere in it. Meanwhile
                                funding_steps.resolve_config("GRC") hands out a ChainConfig
                                whose datadir is the hardcoded literal `~/.GridcoinResearch`,
                                the operator's mainnet GUI staking wallet.

THE WIPE ONE IS THE EXPENSIVE ONE AND IT IS A TRAP RATHER THAN A BUG. Measured that day:
`resolve_config("GRC")` gives datadir `~/.GridcoinResearch`, so `regtest_dir` is
`~/.GridcoinResearch/regtest` -- a directory no Gridcoin wallet creates. So the rmtree was a
silent no-op and `--wipe` appeared harmless. The next person finds --wipe doing nothing for
GRC, "fixes" regtest_dir to Gridcoin's real net subdirectory, and the fix deletes
`~/.GridcoinResearch/testnet`, which on the Bitcoin-derived layout is where that network's
wallet.dat lives. The guard and the target were also resolved from independent sources: the
probe polls the env-supplied port (25715), the target comes from the literal, so the mainnet
daemon being up on 15715 would not have blocked a delete under its own datadir.

NO TEST HERE DELETES ANYTHING REAL. The allowed-wipe cases are pointed at tmp_path, and the
refused cases never reach the rmtree -- which is itself asserted, by checking the directory
is still there afterwards rather than by trusting that an exception was raised early enough.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "swap_terminal"))


from chains.daemon_launch import MAINNET_DATADIR_SEGMENTS, mainnet_datadir_named_by
from regtest import daemons
from regtest.daemons import RegtestSetupError

#: Stand-in for an RPC credential in a ChainConfig that never reaches a daemon.
#:
#: A NAMED CONSTANT RATHER THAN A `noqa: S106` ON EACH CALL. Ruff is right that a string
#: literal assigned to `rpc_password=` is worth flagging, and rule 19 says a suppression is
#: a claim somebody has to re-check. Not writing the literal is cheaper than defending it,
#: and the name says what the value is for -- which a `noqa` would not.
NOT_A_CREDENTIAL = "placeholder"

#: A path that cannot exist, for the configs whose filesystem is never touched.
#:
#: NOT UNDER /tmp, which ruff flags (S108) and is right to: a world-writable fixed path in a
#: test is a real hazard pattern even when this particular test never opens it. Tests that
#: DO touch the filesystem take pytest's tmp_path instead, and every one of them below does.
UNUSED_DATADIR = Path("/nonexistent/not-used-by-this-test")


class _Console:
    def __init__(self):
        self.lines: list[str] = []

    def say(self, *parts: object) -> None:
        self.lines.append(" ".join(str(part) for part in parts))


def _config(asset: str, datadir: Path, port: int = 25715):
    """A ChainConfig with the one field these refusals turn on, and no live daemon."""
    return daemons.ChainConfig(
        asset=asset,
        daemon_path="/nonexistent/daemon",
        cli_path="/nonexistent/cli",
        datadir=datadir,
        host="127.0.0.1",
        # A port nothing is listening on, so rpc_answers() is False and cannot be what
        # produces the refusal. The mainnet-datadir guard has to stand on its own.
        port=port,
        rpc_user=NOT_A_CREDENTIAL,
        rpc_password=NOT_A_CREDENTIAL,
        conf_name="gridcoinresearch.conf",
        pid_name="gridcoinresearchd.pid",
    )


# ---------------------------------------------------------------------------
# build_real_client(): no client for a chain this harness does not drive
# ---------------------------------------------------------------------------

def _entry():
    """regtest_htlc_verify.py as a module, loaded the way the other entry-point tests do."""
    path = Path(__file__).resolve().parent.parent / "regtest_htlc_verify.py"
    spec = importlib.util.spec_from_file_location("regtest_htlc_verify_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_build_real_client_REFUSES_an_asset_it_has_no_client_for():
    """THE DEFECT: this returned an LTCClient for GRC rather than refusing."""
    entry = _entry()
    config = _config("GRC", UNUSED_DATADIR)

    with pytest.raises(RegtestSetupError) as caught:
        entry.build_real_client("GRC", config, "a_wallet")

    message = str(caught.value)
    assert "no client for GRC" in message, message
    assert "LTCClient" in message, "the refusal must say what it USED to return"
    assert "GRCClient" in message, "and where the right client lives"


def test_build_real_client_REFUSES_before_setting_a_litecoin_fee_address(monkeypatch):
    """The refusal must come BEFORE the environment is mutated, or it leaks a wrong default.

    PLATFORM_FEE_LTC_ADDRESS is set with setdefault() one line below the old fall-through.
    A guard placed after it would refuse the client and still have planted a Litecoin
    payout address in the process environment for whatever ran next.
    """
    entry = _entry()
    monkeypatch.delenv("PLATFORM_FEE_LTC_ADDRESS", raising=False)

    with pytest.raises(RegtestSetupError):
        entry.build_real_client("GRC", _config("GRC", UNUSED_DATADIR), "w")

    assert "PLATFORM_FEE_LTC_ADDRESS" not in os.environ, (
        "the refusal happened after the environment was mutated")


def test_build_real_client_still_builds_BTC_and_LTC(monkeypatch):
    """The guard must not have narrowed the two chains this harness actually drives."""
    entry = _entry()
    monkeypatch.setenv("PLATFORM_FEE_LTC_ADDRESS", "placeholder-not-used-without-a-broadcast")

    btc = entry.build_real_client("BTC", _config("BTC", UNUSED_DATADIR, port=18443), "w")
    ltc = entry.build_real_client("LTC", _config("LTC", UNUSED_DATADIR, port=19443), "w")

    assert type(btc).__name__ == "BTCClient", type(btc).__name__
    assert type(ltc).__name__ == "LTCClient", type(ltc).__name__


# ---------------------------------------------------------------------------
# resolve_chain_config(): a named refusal, not a bare KeyError
# ---------------------------------------------------------------------------

def test_resolve_chain_config_REFUSES_an_asset_the_table_does_not_carry():
    """THE DEFECT: `KeyError: 'GRC'` from the middle of step 1, with no remedy."""
    with pytest.raises(RegtestSetupError) as caught:
        daemons.resolve_chain_config("GRC")

    message = str(caught.value)
    assert "CHAIN_DEFAULTS has no entry for 'GRC'" in message, message
    assert "['BTC', 'LTC']" in message, "the refusal must state what the table DOES hold"
    assert "funding_steps" in message, "and send the reader to the GRC-shaped path"


def test_resolve_chain_config_does_not_raise_KeyError_any_more():
    """Pinned separately: a caller catching RegtestSetupError must not also need KeyError."""
    try:
        daemons.resolve_chain_config("NOPE")
    except RegtestSetupError:
        pass
    except KeyError as exc:  # pragma: no cover -- this is the regression
        pytest.fail(f"still a bare KeyError: {exc}")


def test_the_two_chains_the_table_carries_still_resolve():
    for asset in ("BTC", "LTC"):
        config = daemons.resolve_chain_config(asset)
        assert config.asset == asset, config


# ---------------------------------------------------------------------------
# wipe_datadir(): never a datadir that names the operator's staking wallet
# ---------------------------------------------------------------------------

def test_wipe_REFUSES_a_datadir_that_names_the_mainnet_wallet(tmp_path):
    """THE EXPENSIVE ONE. The only guard was a liveness probe on a different port."""
    segment = MAINNET_DATADIR_SEGMENTS[0]
    datadir = tmp_path / segment
    (datadir / "regtest").mkdir(parents=True)

    with pytest.raises(RegtestSetupError) as caught:
        daemons.wipe_datadir(_Console(), _config("GRC", datadir))

    message = str(caught.value)
    assert "REFUSED" in message, message
    assert segment in message, "the refusal must name the datadir segment it matched"
    assert "MAINNET" in message, message
    # AND THE DIRECTORY IS STILL THERE. Asserting on the exception alone would not
    # distinguish "refused before the delete" from "deleted, then refused".
    assert (datadir / "regtest").exists(), "the wipe target was deleted before the refusal"


def test_wipe_refuses_from_the_config_alone_with_no_daemon_asked(tmp_path, monkeypatch):
    """The refusal must not depend on an RPC answer, because both callers wipe first.

    regtest_htlc_verify.py runs --wipe immediately before step_2_daemon, and
    fund_testnets.py before start_daemon. A guard that needed the network asserted would
    arrive after the rmtree.
    """
    asked: list[str] = []
    monkeypatch.setattr(
        daemons, "rpc_answers", lambda config: asked.append(config.base_url) or False)

    datadir = tmp_path / MAINNET_DATADIR_SEGMENTS[0]
    (datadir / "regtest").mkdir(parents=True)

    with pytest.raises(RegtestSetupError):
        daemons.wipe_datadir(_Console(), _config("GRC", datadir))

    assert asked == [], f"the mainnet guard asked a daemon before refusing: {asked}"


def test_wipe_still_works_for_a_datadir_that_is_the_harness_own(tmp_path, monkeypatch):
    """The guard must not have broken --wipe for the chains it exists to serve."""
    monkeypatch.setattr(daemons, "rpc_answers", lambda config: False)
    datadir = tmp_path / "regtest-btc"
    target = datadir / "regtest"
    target.mkdir(parents=True)
    (target / "blocks").mkdir()

    console = _Console()
    daemons.wipe_datadir(console, _config("BTC", datadir, port=18443))

    assert not target.exists(), "the wipe did not happen"
    assert any("wiped" in line for line in console.lines), console.lines


def test_a_desk_datadir_beside_the_mainnet_one_is_NOT_refused(tmp_path, monkeypatch):
    """`-desk` starts with the mainnet name and must still be allowed (segment, not prefix).

    This is the case chains/daemon_launch's matcher was written for, and the one a
    `startswith` or a bare `in` gets wrong. If this test ever fails, the guard has become
    a prefix match and has taken the operator's own desk datadir with it.
    """
    monkeypatch.setattr(daemons, "rpc_answers", lambda config: False)
    datadir = tmp_path / f"{MAINNET_DATADIR_SEGMENTS[0]}-desk"
    target = datadir / "regtest"
    target.mkdir(parents=True)

    daemons.wipe_datadir(_Console(), _config("GRC", datadir))

    assert not target.exists(), "a desk datadir was refused, so the guard is matching a prefix"


def test_the_matcher_answers_about_the_datadir_and_not_its_subdirectories():
    """The contract mainnet_datadir_named_by() documents, pinned.

    It answers "does this path NAME the mainnet datadir", not "is it INSIDE one" -- so a
    caller that passes the subdirectory it is about to delete gets a cheerful "". That is
    why wipe_datadir applies it to config.datadir and never to config.regtest_dir, and this
    test is what stops someone "simplifying" that.
    """
    segment = MAINNET_DATADIR_SEGMENTS[0]
    assert mainnet_datadir_named_by(f"/home/someone/{segment}") == segment
    assert mainnet_datadir_named_by(f"/home/someone/{segment}-desk") == ""
    assert mainnet_datadir_named_by(f"/home/someone/{segment}/regtest") == ""
    assert mainnet_datadir_named_by(f"/home/someone/{segment}/testnet") == ""
    assert mainnet_datadir_named_by("/home/someone/regtest/btc") == ""


def test_the_matcher_takes_a_Path_as_well_as_a_string():
    """wipe_datadir hands it a Path, so the str() conversion is part of the contract."""
    segment = MAINNET_DATADIR_SEGMENTS[0]
    assert mainnet_datadir_named_by(Path("/home/someone") / segment) == segment
    assert mainnet_datadir_named_by(Path("/home/someone") / f"{segment}-desk") == ""
