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

import swap_readiness
from swap_readiness import FAIL, PASS, SKIP, describe_wallet_lock, gridcoin_precheck

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


# --- the Gridcoin lock state, which is a precondition no other chain has ------

def test_a_staking_only_unlock_is_reported_as_unable_to_send():
    """The operator's own operational fact, 2026-09-26.

    A Gridcoin wallet that stakes is normally left unlocked FOR STAKING ONLY, and
    a staking-only unlock cannot send. Paying out needs a full unlock, and the
    wallet is meant to be re-locked and re-unlocked for staking afterwards --
    leaving it fully unlocked is a security regression on a live wallet.

    So a GRC payout has a precondition nothing else here has, and an adapter
    cannot satisfy it: a full unlock needs the passphrase, which this terminal
    deliberately does not hold. What it can do is say so BEFORE a swap is
    created, instead of letting sendtoaddress fail opaquely mid-payout with a
    customer's deposit already taken.
    """
    state, detail = describe_wallet_lock({"unlocked_until": 1790000000, "staking_only": True})

    assert state == FAIL
    assert "STAKING ONLY" in detail
    assert "full unlock" in detail
    assert "re-unlock for staking" in detail, "the line must say how to put it back"


def test_a_locked_wallet_is_reported_as_locked():
    state, detail = describe_wallet_lock({"unlocked_until": 0})

    assert state == FAIL
    assert "LOCKED" in detail


def test_a_fully_unlocked_wallet_can_send_and_is_told_to_relock():
    state, detail = describe_wallet_lock({"unlocked_until": 1790000000, "staking_only": False})

    assert state == PASS
    assert "re-lock for staking" in detail


def test_an_unrecognized_response_is_NOT_read_as_unlocked():
    """Rule 17, and the reason this returns three answers rather than two.

    These field names are NOT confirmed against a live Gridcoin daemon -- none is
    reachable from the environment this was written in. Reporting an unrecognized
    response as "unlocked" would be a guess in the voice of a measurement, and the
    cost of being wrong is a swap created against a wallet that cannot pay it.

    It also prints the keys the daemon DID return, which is how the real field
    names get confirmed: the same way the Monero and XRP field names were, from
    the operator's own run rather than from memory.
    """
    state, detail = describe_wallet_lock({"balance": 1.0, "walletversion": 130000})

    assert state == SKIP, "unknown must not be PASS"
    assert state != PASS
    assert "NOT ESTABLISHED" in detail
    assert "walletversion" in detail, "it must echo the keys it saw so the names can be confirmed"


def test_an_empty_response_says_none_rather_than_printing_nothing():
    """Rule 14: (none) is a result; a blank is ambiguous between zero and broken."""
    _state, detail = describe_wallet_lock({})

    assert "(none)" in detail


# --- a preflight that raises has failed at the one thing it exists to do ------

def test_a_crashing_check_is_reported_and_does_not_kill_the_run(monkeypatch, capsys):
    """Measured on the operator's host 2026-09-26, and it was my defect, not theirs.

    check_xrp() read parameters["reserve_base_drops"] -- a key that does not
    exist. server_parameters() returns base_reserve_xrp and owner_reserve_xrp, in
    XRP rather than drops, so both the NAME and the UNIT were invented instead of
    read. The KeyError killed the run four checks in, so the operator learned
    nothing about GRC, pricing, or anything after it.

    A crash in a reporting tool masks the report. Every check is now wrapped, and
    the wrapper is not a swallow: it records a FAIL naming the check, the
    exception type and the message, so the exit code is non-zero and the line
    says the bug is in swap_readiness.py rather than in what it inspected.

    Asserted by making a check raise and requiring that the LATER checks still
    ran -- the failure mode was never "no error shown", it was "the rest of the
    report never happened".
    """
    def explode():
        raise KeyError("reserve_base_drops")

    monkeypatch.setattr(swap_readiness, "check_schema", explode)
    monkeypatch.setattr(swap_readiness, "check_gridcoin", lambda: swap_readiness.record(PASS, "GRC", "reached"))
    monkeypatch.setattr(swap_readiness, "check_pricing", lambda: swap_readiness.record(PASS, "pricing", "reached"))
    monkeypatch.setattr(swap_readiness, "check_xrp", lambda account: None)
    monkeypatch.setattr(swap_readiness, "check_deposit_account", lambda: "")
    swap_readiness._results.clear()

    exit_code = swap_readiness.main()
    out = capsys.readouterr().out

    assert exit_code == 1, "a crashed check must not produce a READY verdict"
    assert "check crashed" in out
    assert "KeyError" in out
    assert "swap_readiness.py" in out, "the line must say the bug is in the preflight, not the subject"
    assert out.count("reached") == 2, "the checks AFTER the crash must still run -- that was the real cost"
