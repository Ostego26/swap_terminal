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
import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "swap_terminal"))

from network_target import UNCONFIGURED_PORT

import swap_readiness
from swap_readiness import (
    FAIL,
    PASS,
    SKIP,
    describe_wallet_lock,
    explain_grc_failure,
    gridcoin_precheck,
)

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

def test_an_unlocked_wallet_is_NOT_reported_as_able_to_send():
    """The measured limitation, 2026-09-26, and the reason this returns SKIP not PASS.

    Established from the wallet's own `help wallet` and a live getwalletinfo:
    `walletpassphrase <passphrase> <timeout> [stakingonly]` means the staking-only
    STATE exists, but NO wallet-category RPC reports it back. getwalletinfo returns
    exactly one lock field, `unlocked_until`.

    So if a staking-only unlock also sets a timestamp, this state covers both a
    wallet that can pay and one that cannot, and nothing over RPC separates them.
    Reporting PASS would be a guess in the voice of a measurement about the single
    condition that decides whether a payout works -- so it reports SKIP and says
    what it cannot rule out.
    """
    state, detail = describe_wallet_lock({"unlocked_until": 1790000000})

    assert state != PASS, "an unlock that might be staking-only must not read as PASS"
    assert state == SKIP
    assert "stakingonly" in detail, "the line must name the actual RPC parameter"
    assert "CANNOT send" in detail
    assert "re-unlock for staking" in detail, "the line must say how to put the wallet back"


def test_the_deleted_field_names_are_gone_rather_than_kept_as_guesses():
    """Three invented names were removed: they describe a field Gridcoin never returns.

    unlocked_for_staking_only, staking_only and walletunlockstakingonly were my
    guesses at a name for something getwalletinfo does not carry at all -- measured
    against the real wallet, whose entire response was {'unlocked_until': 0}, and
    against `help wallet`, which lists no RPC reporting staking state.

    Rule 2: delete a dead guess rather than leave it looking authoritative. A reader
    finding that tuple would reasonably take it for a list of names somebody had
    seen. Pinned as a test because the tempting "fix" for the ambiguity above is to
    reintroduce a field name and branch on it.
    """
    source = Path(swap_readiness.__file__).read_text()
    for invented in ("unlocked_for_staking_only", "staking_only", "walletunlockstakingonly"):
        assert f'"{invented}"' not in source, f"{invented} is not a real Gridcoin field"
    assert not hasattr(swap_readiness, "_STAKING_ONLY_FIELDS"), "the guessed tuple must stay deleted"


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


# --- a failure must not assert the opposite of what it proves -----------------

def test_a_401_says_the_wallet_is_running_because_that_is_what_a_401_proves():
    """The operator's run, 2026-09-26, and the hint contradicted the evidence.

    Every GRC failure used to get the same appended hint -- "is the testnet daemon
    running?" -- and the run produced a 401. A 401 PROVES the daemon is running:
    something accepted the connection, parsed the request, and rejected the
    credentials. The hint asserted the opposite of what the response established.

    The remedy the wrong hint implies is "restart the staking wallet", which is
    not free, and this session has already caused one needless restart by
    misreading a different signal. So the line now says "do not restart it".
    """
    error = requests.HTTPError("401 Client Error: Authorization Required for url: http://127.0.0.1:25715/")

    detail = explain_grc_failure(error, 25715)

    assert "IS listening" in detail
    assert "do not restart" in detail
    assert "running?" not in detail, "a 401 must never ask whether the daemon is running"


def test_a_refused_connection_says_nothing_is_listening():
    """The other case, which is where the old hint was actually right.

    Fixing the 401 by deleting the hint entirely would have lost this: a refused
    connection genuinely does mean the wallet is not up, or is up without
    server=1, or is reading a different conf. Both branches are asserted so a
    future edit cannot collapse them back into one message.
    """
    error = requests.ConnectionError("HTTPConnectionPool(host='127.0.0.1', port=25779): Max retries exceeded")

    detail = explain_grc_failure(error, 25779)

    assert "nothing is listening" in detail
    assert "server=1" in detail, "the line should name the switch that produces this exact symptom"


def test_an_unrecognized_failure_invents_no_hint_for_itself():
    """Rule 17: no interpretation is better than a guessed one.

    The whole defect above was a hint attached to a failure it did not fit, so the
    default for an unfamiliar failure is to report it and say plainly that it has
    not been interpreted.
    """
    detail = explain_grc_failure(ValueError("something nobody has seen before"), 25715)

    assert "no known interpretation" in detail
    assert "listening" not in detail


def test_the_three_failure_kinds_never_render_the_same_way():
    """Rule 14's shape, pinned as its own assertion.

    The original defect was ONE message for several situations, so a future edit
    that merges any two of these would pass the tests above while restoring it.
    """
    details = {
        explain_grc_failure(requests.HTTPError("401 Client Error: Authorization Required"), 25715),
        explain_grc_failure(requests.ConnectionError("Max retries exceeded"), 25715),
        explain_grc_failure(ValueError("unknown"), 25715),
    }

    assert len(details) == 3, "each failure kind must read differently"
