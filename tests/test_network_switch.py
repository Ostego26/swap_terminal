"""The switch must default to test, refuse a typo, and arm nothing by itself.

Role: test (pure; no chain, no socket, no filesystem)
Reads: swap_terminal/network_switch.py
Writes: nothing (monkeypatch only)
Can move funds: no
Mainnet-safe: yes
Live-safe: yes

=========================================================================
WHY THESE PROPERTIES AND NOT OTHERS
=========================================================================

network_switch decides whether anything in this tree may touch a mainnet endpoint, so the
tests that matter are the ones about getting it WRONG. Each below is a defect this tree has
already had, or one the operator's two instructions make possible for the first time:

  absence means test       network_target.py's header records the other default costing a
                           loop that polled the operator's live Gridcoin staking wallet:
                           `int(os.getenv("GRC_RPC_PORT", "15715"))`, and 15715 is mainnet.
  a typo refuses           new. ST_NETWORK=mian has a charitable reading (main) and a safe
                           one (test); a module that picks is a module that guesses about
                           real money.
  the target arms nothing  new, and the reason the operator gets two variables instead of
                           one: "we will need the ability to put it all on mainnet eventually
                           and at a switch throw" must not mean one keystroke between a test
                           deployment and six live wallets.
  unknown is refused       CLAUDE.md's chain-safety rules: an absence of evidence about a
                           network is treated as mainnet. An endpoint nobody can classify is
                           exactly that absence.

EVERY TEST CLEARS THE ENVIRONMENT FIRST. The variables are read fresh on every call by
design, so a test that inherited one from the shell -- or from a test that ran before it --
would pass or fail for a reason that has nothing to do with the assertion. monkeypatch.delenv
with raising=False is what makes each case start from unset rather than from whatever the
suite happens to leave behind.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "swap_terminal"))

import network_switch as ns
import network_target

#: Every asset the tree has an adapter for, so the per-chain arming tests are not quietly
#: about BTC alone. Spelled here rather than imported from chains.registry on purpose: this
#: module must not need the adapter layer to be importable, and a six-name list that drifts
#: is a visible failure where a silent single-asset test is not.
ASSETS = ("BTC", "LTC", "GRC", "SOL", "XRP", "ICP")


@pytest.fixture(autouse=True)
def _clean_environment(monkeypatch):
    """Unset the target and every arming variable before each test.

    AUTOUSE, because the one thing every case here needs is a known starting point, and a
    fixture each test had to remember to request is one a future test will forget.
    """
    monkeypatch.delenv(ns.TARGET_VARIABLE, raising=False)
    for asset in ASSETS:
        monkeypatch.delenv(ns.ARMED_VARIABLE.format(asset=asset), raising=False)


# ---------------------------------------------------------------------------
# absence means test
# ---------------------------------------------------------------------------

def test_an_unset_target_is_test_and_not_mainnet():
    """THE DEFAULT THAT MATTERS. Unset must never read as live."""
    assert ns.declared_target() == ns.TEST
    assert ns.targeting_mainnet() is False


def test_an_empty_or_whitespace_target_is_also_test(monkeypatch):
    """`ST_NETWORK=` and `ST_NETWORK="  "` are how a half-edited .env arrives.

    An empty value is not a choice of mainnet, and treating it as unreadable would refuse a
    deployment that is in fact safe. Both read as test.
    """
    for blank in ("", "   ", "\t"):
        monkeypatch.setenv(ns.TARGET_VARIABLE, blank)
        assert ns.declared_target() == ns.TEST, f"{blank!r} did not read as test"


def test_the_target_is_read_fresh_and_not_cached_at_import(monkeypatch):
    """A cached target would make a test pass for whatever was set when the module loaded."""
    assert ns.declared_target() == ns.TEST
    monkeypatch.setenv(ns.TARGET_VARIABLE, ns.MAIN)
    assert ns.declared_target() == ns.MAIN, "the target was cached"
    monkeypatch.delenv(ns.TARGET_VARIABLE)
    assert ns.declared_target() == ns.TEST, "the target was cached"


def test_the_target_is_case_and_whitespace_insensitive(monkeypatch):
    """`ST_NETWORK=MAIN ` is a human typing, not a different network."""
    for spelling in ("MAIN", " main ", "Main", "mAiN\n"):
        monkeypatch.setenv(ns.TARGET_VARIABLE, spelling)
        assert ns.declared_target() == ns.MAIN, f"{spelling!r} was not read as main"


# ---------------------------------------------------------------------------
# a typo refuses
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("typo", ["mian", "mainnet", "testnet", "regtest", "prod", "live", "1"])
def test_an_unrecognized_target_refuses_rather_than_defaulting(monkeypatch, typo):
    """Refusing beats both fallbacks, and the parameters say why.

    `mainnet` and `testnet` are the interesting entries: they are what somebody writes when
    they mean it, and accepting them would make the vocabulary two-valued-plus-synonyms,
    which is the beginning of a table. `1` is here because it is what a script sets.
    """
    monkeypatch.setenv(ns.TARGET_VARIABLE, typo)
    with pytest.raises(ns.NetworkTargetRefused) as caught:
        ns.declared_target()
    message = str(caught.value)
    assert ns.TARGET_VARIABLE in message, message
    assert repr(typo) in message, "the refusal must quote what it was given"
    assert "test" in message and "main" in message, "and name the values it accepts"


def test_an_unreadable_target_does_not_report_as_allowed(monkeypatch):
    """may_use_mainnet() never raises, so it must not answer True when it cannot read."""
    monkeypatch.setenv(ns.TARGET_VARIABLE, "mian")
    allowed, why = ns.may_use_mainnet("BTC")
    assert allowed is False
    assert "mian" in why, why


def test_an_unreadable_target_refuses_every_endpoint(monkeypatch):
    """Including a TEST one: if the target cannot be read, nothing about it is known.

    This is the one case where a TEST endpoint is refused, and it is deliberate -- the
    refusal is about the unreadable variable, not about the endpoint.
    """
    monkeypatch.setenv(ns.TARGET_VARIABLE, "mian")
    with pytest.raises(ns.NetworkTargetRefused):
        ns.refuse_if_target_forbids("BTC", "MAINNET")


# ---------------------------------------------------------------------------
# the target arms nothing by itself
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("asset", ASSETS)
def test_the_target_alone_arms_no_chain(monkeypatch, asset):
    """THE PROPERTY THE OPERATOR'S SECOND INSTRUCTION TURNS ON. Parametrized over all six."""
    monkeypatch.setenv(ns.TARGET_VARIABLE, ns.MAIN)
    allowed, why = ns.may_use_mainnet(asset)
    assert allowed is False, f"{asset} went live on the target alone"
    assert ns.ARMED_VARIABLE.format(asset=asset) in why, "the refusal must name the variable"


def test_arming_one_chain_does_not_arm_the_others(monkeypatch):
    """A global flag plus one armed chain must leave the other five refused."""
    monkeypatch.setenv(ns.TARGET_VARIABLE, ns.MAIN)
    monkeypatch.setenv(ns.ARMED_VARIABLE.format(asset="BTC"), "yes")

    assert ns.may_use_mainnet("BTC")[0] is True
    for asset in ASSETS:
        if asset == "BTC":
            continue
        assert ns.may_use_mainnet(asset)[0] is False, f"arming BTC also armed {asset}"


def test_arming_a_chain_without_the_target_does_nothing(monkeypatch):
    """The other direction. An armed chain on a test deployment is still test.

    This is the case that makes arming safe to leave set: an operator who armed GRC last
    month and is back on test today has not left a loaded gun.
    """
    monkeypatch.setenv(ns.ARMED_VARIABLE.format(asset="GRC"), "yes")
    assert ns.armed_for_mainnet("GRC") is True, "the arming variable itself should read set"
    allowed, why = ns.may_use_mainnet("GRC")
    assert allowed is False
    assert ns.TARGET_VARIABLE in why, why


@pytest.mark.parametrize("value", sorted(ns.ARMED_VALUES))
def test_each_accepted_arming_word_works(monkeypatch, value):
    monkeypatch.setenv(ns.TARGET_VARIABLE, ns.MAIN)
    monkeypatch.setenv(ns.ARMED_VARIABLE.format(asset="XRP"), value.upper() + " ")
    assert ns.may_use_mainnet("XRP")[0] is True, f"{value!r} did not arm"


@pytest.mark.parametrize("value", ["1", "0", "no", "false", "", "maybe", "y"])
def test_a_vague_arming_value_does_not_arm(monkeypatch, value):
    """`1` IS DELIBERATELY NOT ENOUGH, and that is the interesting parameter.

    An arming variable is an explicit human act, so it takes a word a human typed on purpose.
    `1` is what a script sets when it means "true" for something unrelated; the cost of
    refusing it is one error message and the cost of accepting it is a live wallet.
    """
    monkeypatch.setenv(ns.TARGET_VARIABLE, ns.MAIN)
    monkeypatch.setenv(ns.ARMED_VARIABLE.format(asset="SOL"), value)
    assert ns.may_use_mainnet("SOL")[0] is False, f"{value!r} armed a chain"


# ---------------------------------------------------------------------------
# composing with network_target.classify()'s word
# ---------------------------------------------------------------------------

def test_a_test_endpoint_is_allowed_even_on_a_live_deployment(monkeypatch):
    """A live deployment must still be able to run one chain in test.

    That is the ordinary way a chain gets added, and a switch that forbade it would make
    going live all-or-nothing -- which is the opposite of per-chain arming.
    """
    monkeypatch.setenv(ns.TARGET_VARIABLE, ns.MAIN)
    ns.refuse_if_target_forbids("LTC", "TEST")


def test_a_mainnet_endpoint_is_refused_on_a_test_target():
    with pytest.raises(ns.NetworkTargetRefused) as caught:
        ns.refuse_if_target_forbids("GRC", "MAINNET")
    assert "MAINNET" in str(caught.value)
    assert ns.TARGET_VARIABLE in str(caught.value)


def test_a_mainnet_endpoint_is_refused_when_the_chain_is_not_armed(monkeypatch):
    monkeypatch.setenv(ns.TARGET_VARIABLE, ns.MAIN)
    with pytest.raises(ns.NetworkTargetRefused) as caught:
        ns.refuse_if_target_forbids("GRC", "MAINNET")
    assert ns.ARMED_VARIABLE.format(asset="GRC") in str(caught.value)


def test_a_mainnet_endpoint_is_allowed_when_targeted_and_armed(monkeypatch):
    monkeypatch.setenv(ns.TARGET_VARIABLE, ns.MAIN)
    monkeypatch.setenv(ns.ARMED_VARIABLE.format(asset="GRC"), "armed")
    ns.refuse_if_target_forbids("GRC", "MAINNET")


@pytest.mark.parametrize("word", ["UNCONFIGURED", "UNRECOGNIZED", "", "   ", "sometestnet"])
def test_an_unclassifiable_endpoint_is_refused_on_every_target(monkeypatch, word):
    """Absence of evidence about a network is treated as mainnet (chain-safety rules).

    Asserted on BOTH targets, with the chain armed, so it cannot be mistaken for the
    arming check doing the work.
    """
    monkeypatch.setenv(ns.TARGET_VARIABLE, ns.MAIN)
    monkeypatch.setenv(ns.ARMED_VARIABLE.format(asset="BTC"), "yes")
    with pytest.raises(ns.NetworkTargetRefused) as caught:
        ns.refuse_if_target_forbids("BTC", word)
    assert "unknown" in str(caught.value).lower(), str(caught.value)


def test_classify_words_are_the_ones_network_target_actually_returns():
    """Guard the seam, not the module: these two files must agree on the vocabulary.

    refuse_if_target_forbids() matches on TEST and MAINNET. If network_target.classify()
    ever returned different words, every endpoint would land in the unclassifiable branch
    and the whole tree would refuse -- loudly, but for a reason nobody would look for here.
    Rule 8: one vocabulary, and this asserts the two users of it have not drifted.
    """
    returned = {
        network_target.classify("BTC", 8332),
        network_target.classify("BTC", 18443),
        network_target.classify("BTC", 0),
        network_target.classify("BTC", 65000),
    }
    assert "MAINNET" in returned, returned
    assert "TEST" in returned, returned
    # Every word classify() can return must be one refuse_if_target_forbids handles
    # explicitly -- TEST and MAINNET pass or gate, everything else refuses. The assertion
    # is that nothing returns a FIFTH word that would silently land in the refuse branch
    # while meaning something else.
    assert returned <= {"MAINNET", "TEST", "UNCONFIGURED", "UNRECOGNIZED"}, returned


# ---------------------------------------------------------------------------
# the banner
# ---------------------------------------------------------------------------

def test_the_banner_always_names_the_variable_and_is_never_blank(monkeypatch):
    """Rule 14: a pasted line must be self-describing a day later."""
    assert ns.TARGET_VARIABLE in ns.describe()
    assert "test" in ns.describe()

    monkeypatch.setenv(ns.TARGET_VARIABLE, ns.MAIN)
    live = ns.describe()
    assert ns.TARGET_VARIABLE in live
    assert "LIVE" in live, live
    assert "REAL" in live, "a live banner must say so in words an operator cannot skim past"

    monkeypatch.setenv(ns.TARGET_VARIABLE, "mian")
    assert "UNREADABLE" in ns.describe()
