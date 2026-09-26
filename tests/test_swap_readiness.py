"""The preflight's one decision with a cost: whether to open a socket to a Gridcoin wallet.

Role: test (pure function; opens no socket)
Reads: swap_readiness.py
Writes: nothing
Can move funds: no
Mainnet-safe: yes
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "swap_terminal"))

from network_target import UNCONFIGURED_PORT

from swap_readiness import FAIL, PASS, gridcoin_precheck

MAINNET_PORT = 15715
TESTNET_PORT = 25779


def test_a_mainnet_port_refuses_to_connect_at_all():
    """THE one that matters. Looking is the hazard, not acting.

    A get_balance() against 15715 prints the operator's real staking balance into
    whatever terminal, transcript or pasted block the output lands in. That
    happened on 2026-09-25 -- 157,797 GRC into a chat log -- and the lesson was
    that labeling a balance "mainnet" AFTER fetching and printing it is the wrong
    altitude. Classify first, then decide whether to open the socket.

    Asserted as connect=False specifically, not merely as a FAIL verdict: a
    version that connected, read the balance and then reported FAIL would satisfy
    a verdict-only assertion while doing the exact thing this prevents.
    """
    connect, state, detail = gridcoin_precheck(MAINNET_PORT)

    assert connect is False, "a mainnet port must not be connected to at all"
    assert state == FAIL
    assert "did NOT connect" in detail


def test_an_unrecognized_port_also_refuses_rather_than_assuming_it_is_safe():
    """Rule 17: an unknown port means the chain was not established.

    Any daemon can run on any -rpcport, so an unrecognized port may well be a
    mainnet wallet. Treating "not a known mainnet port" as "safe to read" is a
    guess in the voice of a measurement, and the cost of being wrong is the same
    leaked balance.
    """
    connect, state, _detail = gridcoin_precheck(34567)

    assert connect is False
    assert state == FAIL


def test_an_unconfigured_port_names_the_variable_and_the_test_port():
    """"Connection refused" sends an operator to restart a daemon that was fine."""
    connect, state, detail = gridcoin_precheck(UNCONFIGURED_PORT)

    assert connect is False
    assert state == FAIL
    assert "GRC_RPC_PORT" in detail
    assert str(TESTNET_PORT) in detail


def test_a_test_port_is_the_only_case_that_connects():
    """The positive case, or every test above passes against a function that always refuses."""
    connect, state, detail = gridcoin_precheck(TESTNET_PORT)

    assert connect is True
    assert state == PASS
    assert str(MAINNET_PORT) in detail, "the line should say what mainnet is, so the reader can tell them apart"


@pytest.mark.parametrize("port", [MAINNET_PORT, 34567, UNCONFIGURED_PORT])
def test_no_refusing_case_ever_returns_connect_true(port):
    """One assertion over every refusing input, because the failure is silent.

    A regression here does not raise or print anything unusual -- it just quietly
    reads a wallet it should not have. Enumerating the cases means adding a new
    refusal reason without adding it here shows up as a gap rather than passing.
    """
    assert gridcoin_precheck(port)[0] is False
