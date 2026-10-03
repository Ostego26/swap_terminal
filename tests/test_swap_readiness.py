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
from regtest.daemons import GRC_CREDENTIALS_ARE_PER_NETWORK, why_nothing_answered
from services.payout_service import WALLET_UNLOCK_ENV_VAR

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
    names get confirmed: the same way the XRP field names were, from
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
    # `pair=None`, because check_pricing() gained that parameter when --pair landed.
    # A zero-argument stub raises TypeError, the outer wrapper catches it and records
    # "pricing (check crashed)", and this test then failed on its OWN stub rather
    # than on the thing it tests -- a stub narrower than the real signature, which
    # is the fixture-narrower-than-reality pattern this suite keeps hitting.
    monkeypatch.setattr(swap_readiness, "check_pricing",
                        lambda pair=None: swap_readiness.record(PASS, "pricing", "reached"))
    monkeypatch.setattr(swap_readiness, "check_xrp", lambda account: None)
    monkeypatch.setattr(swap_readiness, "check_deposit_account", lambda: "")
    swap_readiness._results.clear()

    exit_code = swap_readiness.main([])
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


# --- the preflight must answer about the pair being RUN -----------------------
#
# Everything below was added 2026-10-01, when the operator asked to run the whole
# SOL -> GRC rehearsal again and this file's subject turned out to be XRP: the
# title said "XRP <-> GRC", check_pair_is_allowed() filtered ALLOWED_PAIRS to
# pairs containing XRP, check_pricing() hardwired XRP_USD and GRC_USD, and there
# was no SOL check of any kind. A verdict about a leg nobody is running is worse
# than no verdict, because it is a verdict.


class FakeSolana:
    """The three calls check_solana() makes, and nothing else.

    A stub rather than a mock of the whole adapter: these three are the entire
    contract that function depends on, and a stub that only answers them fails
    loudly if a fourth call is ever added, where a permissive mock would silently
    answer it.
    """

    def __init__(self, genesis: str, lamports: int = 1_000_000, valid: bool = True):
        self._genesis = genesis
        self._lamports = lamports
        self._valid = valid
        self.calls: list[str] = []

    def call(self, method, *params):
        self.calls.append(method)
        if method == "getGenesisHash":
            return self._genesis
        if method == "getBalance":
            return {"value": self._lamports}
        raise AssertionError(f"check_solana() made an unexpected call: {method}")

    def validate_address(self, address: str) -> bool:
        return self._valid


DEVNET_GENESIS = "EtWTRABZaYq6iMfeYKouRu166VU2xqa1wcaWoxPkrZBG"
MAINNET_GENESIS = "5eykt4UsFv8P8NJdTREpY1vzqKqZKvdpKuc147dw2N9d"


def run_solana(monkeypatch, adapter, account="CUBnQ5QBfYkL71TCqSdecAQ9xjfGmAdu6Hs3fjQeLorp", url="https://x"):
    """check_solana() against a stub, returning the recorded (state, name, detail) rows."""
    monkeypatch.setattr(swap_readiness.Config, "RPC", {"SOL": {"url": url}}, raising=False)
    monkeypatch.setattr(swap_readiness.Config, "SOL_DEPOSIT_ACCOUNT", account, raising=False)
    swap_readiness._results.clear()
    swap_readiness.check_solana({"SOL": adapter} if adapter is not None else {})
    return list(swap_readiness._results)


def test_a_mainnet_solana_cluster_is_a_failure_and_not_a_note(monkeypatch):
    """Same judgment check_gridcoin() makes about port 15715: looking is the hazard.

    Every Solana address and keypair in this project is a devnet one. The only
    reason to be pointed at mainnet-beta during a rehearsal is a mistake, and a
    PASS beside the word MAINNET is how that mistake survives to a transfer.
    """
    rows = run_solana(monkeypatch, FakeSolana(MAINNET_GENESIS))
    cluster = [row for row in rows if row[1] == "SOL cluster"]
    assert len(cluster) == 1
    assert cluster[0][0] == FAIL
    assert "MAINNET" in cluster[0][2]
    assert "REAL MONEY" in cluster[0][2]


def test_the_cluster_is_identified_by_genesis_and_says_so(monkeypatch):
    """Because the hostname is a label anybody can point anywhere.

    An operator pointing a "devnet" alias at mainnet would otherwise read the word
    devnet all the way to a real transfer -- so the line names the genesis hash and
    states that the URL was NOT what decided it.
    """
    rows = run_solana(monkeypatch, FakeSolana(DEVNET_GENESIS), url="https://api.devnet.solana.com")
    cluster = next(row for row in rows if row[1] == "SOL cluster")
    assert cluster[0] == PASS
    assert "DEVNET" in cluster[2]
    assert DEVNET_GENESIS in cluster[2]
    assert "NOT by the hostname" in cluster[2]


def test_an_unset_rpc_url_says_the_watcher_will_credit_nothing_forever(monkeypatch):
    """The silence is the defect, and the line has to name it.

    An unconfigured chain does not crash. chains/registry builds no adapter, the
    deposit watcher logs "SOL not configured" once a cycle, and every SOL swap sits
    in awaiting_deposit while the deposit is on chain. Rule 13's shape: nothing
    fails, nothing works.
    """
    rows = run_solana(monkeypatch, None, url="")
    assert rows[0][0] == FAIL
    assert rows[0][1] == "SOL_RPC_URL"
    assert "credits nothing, forever" in rows[0][2]


def test_an_unset_deposit_account_distinguishes_allowed_from_creatable(monkeypatch):
    """A pair being in ALLOWED_PAIRS and a swap being creatable are two gates.

    The `pair allowed` line says SOL->GRC is allowed. create_swap() still refuses
    every SOL swap while SOL_DEPOSIT_ACCOUNT is empty, and only this line says so.
    """
    rows = run_solana(monkeypatch, FakeSolana(DEVNET_GENESIS), account="")
    account = next(row for row in rows if row[1] == "SOL_DEPOSIT_ACCOUNT")
    assert account[0] == FAIL
    assert "does not make a SOL swap creatable" in account[2]


def test_a_zero_balance_deposit_account_is_a_PASS_unlike_the_payout_chains(monkeypatch):
    """The opposite verdict to XRP and GRC, and the asymmetry is the point.

    Nothing is ever SENT from the deposit account -- it only receives -- so a zero
    balance is not a defect there, where for a payout wallet it means nothing can
    be paid. Reporting them the same way would be rule 14's "state what the number
    means, next to the number" failed in the direction that stops a good run.
    """
    rows = run_solana(monkeypatch, FakeSolana(DEVNET_GENESIS, lamports=0))
    account = next(row for row in rows if row[1] == "SOL deposit account")
    assert account[0] == PASS
    assert "A zero balance is fine" in account[2]
    assert "nothing is ever sent FROM here" in account[2]


def test_an_off_curve_deposit_account_is_refused_before_anything_is_told_to_send(monkeypatch):
    """About half of all 32-byte base58 strings are off-curve and are not accounts."""
    adapter = FakeSolana(DEVNET_GENESIS, valid=False)
    rows = run_solana(monkeypatch, adapter)
    account = next(row for row in rows if row[1] == "SOL_DEPOSIT_ACCOUNT")
    assert account[0] == FAIL
    assert "off-curve" in account[2]
    assert "getBalance" not in adapter.calls, "a refused address must not be looked up"


def test_an_endpoint_that_does_not_answer_fails_without_a_traceback(monkeypatch):
    """A 429 is the specific failure that killed a live deposit watcher on 2026-10-01."""
    class Throttled(FakeSolana):
        def call(self, method, *params):
            raise RuntimeError("getSignaturesForAddress returned HTTP 429")

    rows = run_solana(monkeypatch, Throttled(DEVNET_GENESIS))
    endpoint = next(row for row in rows if row[1] == "SOL endpoint")
    assert endpoint[0] == FAIL
    assert "RuntimeError" in endpoint[2]
    assert "429" in endpoint[2]


# --- the check that would have saved three rehearsals ------------------------


def unlock_row():
    """The `payout unlock` row, SELECTED BY NAME.

    Three tests here indexed swap_readiness._results[0] and all three broke when
    check_payout_unlock() gained a `payout chain` line in front of the unlock one
    (2026-10-01) -- a position is not an identity, and the failure said
    "assert 'FAIL' == 'SKIP'" about a row the test was not asking about.
    """
    return next(row for row in swap_readiness._results if row[1] == "payout unlock")


def test_a_missing_gridcoin_passphrase_is_a_FAIL_before_any_swap_exists(monkeypatch):
    """THE PRECONDITION THAT BROKE THREE LIVE RUNS, 2026-10-01.

    Each one reached the payout -- memo attributed, deposit credited, swap advanced,
    payout claimed -- and died on GRIDCOIN_WALLET_PASSPHRASE being unset in the
    supervisor's environment, landing the swap in 'failed', which nothing retries.
    Three swaps and three deposits for one unexported variable, and no preflight
    checked it.
    """
    monkeypatch.delenv("GRIDCOIN_WALLET_PASSPHRASE", raising=False)
    swap_readiness._results.clear()
    swap_readiness.check_payout_unlock({"GRC": CanSign()})
    row = unlock_row()

    assert row[0] == FAIL
    assert "IS NOT SET" in row[2]
    assert "nothing retries" in row[2]


def test_a_present_passphrase_claims_presence_and_never_correctness(monkeypatch):
    """"Set" and "works" are different claims, and this makes the weaker one (rule 17).

    The passphrase itself must never reach a line, and neither must its length.
    """
    # Named `fixture_value`, not `secret`: ruff's S105 flags a string assigned to a
    # password-shaped NAME, and the honest fix is the name -- this is a test
    # fixture, not a credential, and a `noqa` claiming so would be the suppression
    # rule 19 forbids. It is never a real passphrase and never read from anywhere.
    fixture_value = "not-the-real-one-and-never-printed"
    monkeypatch.setenv("GRIDCOIN_WALLET_PASSPHRASE", fixture_value)
    swap_readiness._results.clear()
    swap_readiness.check_payout_unlock({"GRC": CanSign()})
    detail = unlock_row()[2]

    assert unlock_row()[0] == PASS
    assert "NOT a claim that it is the right passphrase" in detail
    assert fixture_value not in detail, "the preflight printed the passphrase"
    assert str(len(fixture_value)) not in detail, "the preflight printed the passphrase's length"


def test_a_chain_that_needs_no_unlock_gets_no_warning(monkeypatch):
    """Cried-wolf noise for a chain nobody set up is what this file fixed once already.

    BTC, not SOL, and the change is the point: a process with only a SOL adapter
    could pay NOTHING, which is its own FAIL (see
    test_nothing_payable_is_a_FAIL_not_a_SKIP). BTC is the destination of two
    allowed pairs and needs no wallet unlock, which is the case this test is
    actually about -- the old fixture was exercising the no-payable path and
    scoring it as "no warning needed".

    THE REASON SOL COULD PAY NOTHING CHANGED ON 2026-10-03 and the choice of BTC
    here did not. It used to be "SOL is never a TO asset", which stopped being true
    when ("GRC","SOL"), ("BTC","SOL") and ("LTC","SOL") were enabled; it is now "SOL
    holds no signing key on an unarmed host". Either way a SOL-only adapter table is
    the no-payable case and the wrong fixture for a test about unlock noise, so this
    sentence is corrected rather than the fixture.
    """
    monkeypatch.delenv("GRIDCOIN_WALLET_PASSPHRASE", raising=False)
    swap_readiness._results.clear()
    swap_readiness.check_payout_unlock({"BTC": CanSign()})
    row = unlock_row()

    assert row[0] == SKIP
    assert "GRC is the only one that does" in row[2]


def test_nothing_payable_is_a_FAIL_not_a_SKIP(monkeypatch):
    """MEASURED ON THE OPERATOR'S HOST 2026-10-01, and it rendered as a SKIP.

    With GRC_RPC_PASS unset the only adapter was SOL. SOL was deliberately never a
    TO asset -- config.ALLOWED_PAIRS carried ("SOL","GRC") and not the reverse,
    because chains/solana.py could not sign -- so NO swap this terminal allowed
    could ever have been paid. The line printed was

        SKIP  payout unlock   no configured chain needs a wallet unlock to pay out

    which is true, and reads as nothing-to-worry-about. The supervisor's spawn
    banner had the identical defect in the identical case and said "a payout worker
    CAN broadcast" one screen later.

    THE FIXTURE CHANGED 2026-10-03 AND THE INVARIANT DID NOT. The paragraph above
    is kept as written because it is the measurement this test exists for, and
    every clause of it about SOL has stopped being true: ("GRC","SOL"),
    ("BTC","SOL") and ("LTC","SOL") are now in ALLOWED_PAIRS, so SOL IS a
    destination, and `{"SOL": CanSign()}` no longer expresses "nothing can be paid"
    -- it expresses the opposite. The test was handing payable_assets() a payable
    adapter and asserting the unpayable verdict.

    IT NOW REACHES THE SAME STATE THROUGH THE CONDITION THAT ACTUALLY HOLDS ON
    EVERY CHECKOUT, which is a stronger premise rather than a looser one.
    SOL_PAYOUT_KEYPAIR_PATH is unset in every test run, so SolanaAdapter.can_spend
    is False, so payable_assets() drops SOL for want of a SIGNING KEY rather than
    for want of a pair -- and CannotSign() is exactly that adapter. The old fixture
    asserted the FAIL through a config fact an operator changes with one line; this
    one asserts it through the arming switch, which is still false on the next
    unarmed host whatever ALLOWED_PAIRS grows to.
    """
    monkeypatch.delenv("GRIDCOIN_WALLET_PASSPHRASE", raising=False)
    swap_readiness._results.clear()
    swap_readiness.check_payout_unlock({"SOL": CannotSign()})
    rows = list(swap_readiness._results)

    assert len(rows) == 1, "it must stop at the payout-chain line rather than also asking about unlocks"
    assert rows[0][1] == "payout chain"
    assert rows[0][0] == FAIL
    assert "NOTHING CAN BE PAID OUT" in rows[0][2]
    assert "nothing retries" in rows[0][2]


class CanSign:
    """An adapter that can broadcast. `CanSign()` USED TO STAND IN FOR THIS and stopped
    being adequate on 2026-10-02.

    These fixtures passed `CanSign()` because only the dict KEYS mattered:
    services/payout_service.payable_assets() took asset NAMES and asked nothing of the
    adapter. It now takes the adapters and reads chains/registry.why_cannot_pay_out(),
    because it was reporting XRP as payable on the operator's host while XRP holds no
    signing key -- so the VALUE is now the question, and a bare object answers it wrong.

    It answers wrong in the SAFE direction, which is why this is a fixture change rather
    than a softened check: chains/base.py reads `getattr(adapter, "can_spend", False)`,
    fail-closed on purpose, so an adapter that does not say is treated as unable to move
    money. An `CanSign()` is precisely that adapter, and these tests were asserting a
    PASS for one.
    """

    can_spend = True
    payout_refusal = ""


class CannotSign:
    """An adapter that is REACHABLE and still cannot pay out, which is a different
    thing from being absent and has to be expressible as a fixture.

    ADDED 2026-10-03, because the fixture that used to express "nothing can be paid
    out" was `{"SOL": CanSign()}` and stopped expressing it the moment SOL became a
    destination of three allowed pairs. The unpayable state did not go away with it
    -- it moved from ALLOWED_PAIRS to the arming switch -- and this is that state:
    chains/registry.why_cannot_pay_out() returns `payout_refusal` for an adapter
    whose can_spend is False, which is precisely what SolanaAdapter answers on a
    host with no SOL_PAYOUT_KEYPAIR_PATH exported, i.e. every test run and every
    fresh checkout.

    The refusal sentence is non-empty on purpose. why_cannot_pay_out() falls back to
    "cannot pay out, and its adapter does not say why" for an adapter that declines
    without a reason, and a fixture that took that path would be testing the
    fallback rather than the case.
    """

    can_spend = False
    payout_refusal = "cannot pay out in this test: no payout key is armed"


def test_a_payable_chain_is_named_so_a_missing_one_is_visible(monkeypatch):
    """The PASS half, because a version that always failed would pass the test above.

    THE FIXTURE CHANGED 2026-10-03, FOR THE REASON RECORDED ON THE TEST ABOVE, and
    the assertion it carries is now the stronger of the two available. It used to
    read

        assert "SOL" not in row[2], "SOL has an adapter but is no pair's destination"

    which pinned the DESTINATION half of payable_assets() -- true until ("GRC","SOL")
    landed, and a config edit away from being untrue again. The half that is worth
    pinning is the one that was missing from payable_assets() until 2026-10-02 and
    had it reporting XRP as payable on the operator's host while XRP held no signing
    key: an adapter that EXISTS, is REACHABLE, and cannot sign must not be named on
    a line that promises a payout can be broadcast. So SOL is still the asset
    asserted absent, and it is absent for the reason that costs money rather than
    the reason that is a config setting.
    """
    monkeypatch.setenv("GRIDCOIN_WALLET_PASSPHRASE", "present-for-this-test-only")
    swap_readiness._results.clear()
    swap_readiness.check_payout_unlock({"GRC": CanSign(), "SOL": CannotSign()})
    row = next(r for r in swap_readiness._results if r[1] == "payout chain")

    assert row[0] == PASS
    assert "GRC" in row[2]
    assert "SOL" not in row[2], (
        "SOL has an adapter and is the destination of three allowed pairs, and it holds no signing "
        "key here -- a line that names it claims a payout that would raise and strand a deposit"
    )


# --- the pair line and the pricing line must follow ALLOWED_PAIRS ------------


def test_the_pair_line_lists_every_allowed_pair_not_one_leg(monkeypatch):
    """It filtered to XRP and called the result `pair allowed`.

    On the operator's host that rendered as a PASS listing four XRP pairs, with
    SOL->GRC -- the pair being rehearsed that afternoon -- absent from a line whose
    name promises to list what is allowed.
    """
    monkeypatch.setattr(swap_readiness.Config, "ALLOWED_PAIRS",
                        {("SOL", "GRC"), ("XRP", "GRC"), ("BTC", "LTC")}, raising=False)
    swap_readiness._results.clear()
    swap_readiness.check_pair_is_allowed()
    allowed = next(row for row in swap_readiness._results if row[1] == "pair allowed")
    checked = next(row for row in swap_readiness._results if row[1] == "pairs checked here")

    assert "SOL->GRC" in allowed[2]
    assert "BTC->LTC" in allowed[2], "a pair this file cannot check is still ALLOWED and must be listed"
    assert "all 3" in allowed[2]
    assert "SOL->GRC" in checked[2]
    assert "BTC->LTC" not in checked[2]
    assert "2 of 3" in checked[2], "the gap between allowed and checked is stated, not implied"


def test_pricing_follows_the_allowed_pairs_rather_than_two_hardwired_assets(monkeypatch):
    """A missing SOL_USD must FAIL, where the old two-asset check would have passed.

    create_quote() raises on a missing price and routes/quotes.py renders str(exc),
    so the consequence of this check being wrong is an exception repr in a
    customer's browser -- the same shape get_network_fee_reserve() was rewritten
    for.
    """
    monkeypatch.setattr(swap_readiness.Config, "ALLOWED_PAIRS", {("SOL", "GRC")}, raising=False)
    monkeypatch.setattr(swap_readiness, "fetch_usd_prices",
                        lambda *a, **k: {"XRP_USD": 2.5, "GRC_USD": 0.0125})
    swap_readiness._results.clear()
    swap_readiness.check_pricing()
    row = swap_readiness._results[0]

    assert row[0] == FAIL
    assert "SOL" in row[2]
    assert "every checked pair needs BOTH legs priced" in row[2]


def test_pricing_rates_every_checked_pair_and_not_just_one(monkeypatch):
    """"1 XRP = N GRC" says nothing about whether SOL can be quoted."""
    monkeypatch.setattr(swap_readiness.Config, "ALLOWED_PAIRS",
                        {("SOL", "GRC"), ("XRP", "GRC")}, raising=False)
    monkeypatch.setattr(swap_readiness, "fetch_usd_prices",
                        lambda *a, **k: {"XRP_USD": 2.5, "GRC_USD": 0.0125, "SOL_USD": 118.5})
    swap_readiness._results.clear()
    swap_readiness.check_pricing()
    row = swap_readiness._results[0]

    assert row[0] == PASS
    assert "1 SOL = 9480.0000 GRC" in row[2]
    assert "1 XRP = 200.0000 GRC" in row[2]


def test_zero_adapters_is_a_FAIL_and_not_a_green_line(monkeypatch, capsys):
    """It printed `PASS  adapters built  (none)` on its first run.

    A green verdict on a terminal that cannot reach a single chain: rule 13's
    "'skipped' plus 'success' in the same output is a defect in the OUTPUT", printed
    by the tool whose entire job is to not do that.
    """
    monkeypatch.setattr(swap_readiness, "build_adapters", lambda rpc: {})
    for name in ("check_schema", "check_gridcoin", "check_pricing"):
        monkeypatch.setattr(swap_readiness, name, lambda: None)
    monkeypatch.setattr(swap_readiness, "check_xrp", lambda account: None)
    monkeypatch.setattr(swap_readiness, "check_deposit_account", lambda: "")
    monkeypatch.setattr(swap_readiness, "check_solana", lambda adapters: None)
    monkeypatch.setattr(swap_readiness, "check_payout_unlock", lambda adapters: None)
    monkeypatch.setattr(swap_readiness, "check_pair_is_allowed", lambda: None)
    swap_readiness._results.clear()

    exit_code = swap_readiness.main([])
    out = capsys.readouterr().out

    assert exit_code == 1, "no chain reachable must not produce a READY verdict"
    assert "FAIL  adapters built" in out
    assert "NOTHING is reachable" in out


# --- a gate that can never open is not a gate ----------------------------------
#
# MEASURED 2026-10-01. I handed the operator `swap_readiness.py && supervisor.py
# start` so a broken config could not spawn workers. That gate is UNSATISFIABLE on
# their host: XRP is not configured and is not going to be -- they run SOL -> GRC
# -- so XRP_RPC_URL and XRP_DEPOSIT_ACCOUNT fail forever and the verdict is NOT
# READY forever. A gate nobody can satisfy is one people learn to bypass, which is
# worse than no gate, because the next REAL failure gets bypassed with it.


def test_a_scoped_run_skips_the_legs_the_pair_does_not_name(capsys):
    """SKIPPED, not dropped. A check that vanishes cannot be told from one that
    did not run (rule 14), and the tally at the bottom counts what it printed."""
    swap_readiness._results.clear()
    swap_readiness.main(["--pair", "SOL:GRC"])
    out = capsys.readouterr().out

    assert "SOL -> GRC ONLY" in out
    assert "not checked: --pair SOL:GRC has no XRP leg" in out
    assert "XRP_DEPOSIT_ACCOUNT" not in out, "an XRP check ran for a pair with no XRP leg"


def test_an_unconfigured_chain_outside_the_pair_cannot_fail_the_verdict(monkeypatch, capsys):
    """THE WHOLE POINT. XRP unset must not make SOL->GRC report NOT READY.

    Every SOL and GRC precondition is stubbed to pass here, so the only thing that
    could fail the run is a leg the pair does not name. Before --pair existed, this
    configuration returned 1 forever.
    """
    monkeypatch.setattr(swap_readiness, "build_adapters", lambda rpc: {"GRC": CanSign(), "SOL": CanSign()})
    monkeypatch.setattr(swap_readiness, "check_solana", lambda adapters: swap_readiness.record(
        PASS, "SOL", "stubbed"))
    monkeypatch.setattr(swap_readiness, "check_gridcoin", lambda: swap_readiness.record(
        PASS, "GRC wallet", "stubbed"))
    monkeypatch.setattr(swap_readiness, "check_pricing", lambda pair=None: swap_readiness.record(
        PASS, "pricing", "stubbed"))
    monkeypatch.setenv("GRIDCOIN_WALLET_PASSPHRASE", "present-for-this-test-only")
    monkeypatch.delenv("XRP_RPC_URL", raising=False)
    monkeypatch.delenv("XRP_DEPOSIT_ACCOUNT", raising=False)
    swap_readiness._results.clear()

    code = swap_readiness.main(["--pair", "SOL:GRC"])
    out = capsys.readouterr().out

    assert code == 0, f"a scoped run failed on a leg outside the pair:\n{out}"
    assert "READY" in out
    assert "SOL -> GRC can be created and paid" in out


def test_the_unscoped_verdict_still_covers_the_whole_terminal(monkeypatch, capsys):
    """The default must not become the narrow question.

    "is everything I own working" is a real question and the right default; what
    was missing is "can THIS pair be created and paid". Adding the second must not
    quietly replace the first.
    """
    monkeypatch.setattr(swap_readiness, "build_adapters", lambda rpc: {})
    swap_readiness._results.clear()
    code = swap_readiness.main([])
    out = capsys.readouterr().out

    assert code == 1
    assert "every pair this terminal allows" in out
    assert "XRP_RPC_URL" in out, "the unscoped run must still check every leg it knows"


def _ordered_pairs_outside_ALLOWED_PAIRS() -> list[str]:
    """Every FROM:TO over the tradeable assets that Config does NOT allow.

    DERIVED, NOT SPELLED, AND THE SPELLING IS WHY. This test said `GRC:SOL` -- the
    reverse of ("SOL","GRC") and therefore outside ALLOWED_PAIRS when it was
    written, and INSIDE it from 2026-10-03, when the three *->SOL directions were
    enabled. The test then exercised the allowed path and asserted the refused one,
    which is a fixture rotting rather than a gate failing, and it is the same
    hand-written-copy-of-the-config failure the pair line above already records.

    So the argument comes off Config.ALLOWED_PAIRS itself: every ordered pair of
    the assets that appear in it, minus the ones it carries. That cannot rot while
    any direction remains unenabled -- and if one day none does, the parametrize
    list is empty, which pytest reports rather than passing silently, and the
    assertion below says what to do about it.

    Measured 2026-10-03 against this tree: 5 tradeable assets, 20 ordered pairs, 16
    allowed, so 4 are refused -- BTC:XRP, LTC:XRP, SOL:XRP, XRP:SOL. Each is a real
    asset pair and a plausible typo, which is the case this gate is for; a nonsense
    string is the test below.
    """
    allowed = set(swap_readiness.Config.ALLOWED_PAIRS)
    assets = sorted({asset for pair in allowed for asset in pair})
    return [
        f"{from_asset}:{to_asset}"
        for from_asset in assets
        for to_asset in assets
        if from_asset != to_asset and (from_asset, to_asset) not in allowed
    ]


def test_there_is_an_unallowed_direction_left_to_refuse():
    """An empty parametrize set below would report as a SKIP, which reads as
    nothing-to-worry-about -- the exact shape test_nothing_payable_is_a_FAIL_not_a_SKIP
    is about, one layer up in the test suite itself. So the denominator is asserted
    here rather than left to pytest's collection message (rule 3, rule 14)."""
    assert _ordered_pairs_outside_ALLOWED_PAIRS(), (
        "every ordered pair of every tradeable asset is now in Config.ALLOWED_PAIRS, so the refusal "
        "path below has nothing to exercise. That is a real finding about config.py and not a setup "
        "problem: parse_pair()'s gate is then unreachable and needs a fixture that is not derived."
    )


@pytest.mark.parametrize("text", _ordered_pairs_outside_ALLOWED_PAIRS())
def test_a_pair_outside_ALLOWED_PAIRS_is_refused_rather_than_widening_the_gate(capsys, text):
    """A typo in a gate's argument must not check everything instead.

    The dangerous reading of an unrecognized --pair is "scope to nothing, so
    nothing fails". Exit 2, distinct from both 0 and the 1 that means NOT READY.

    EVERY unallowed direction, not one of them (2026-10-03). The single argument
    this used to pass became an ALLOWED pair, so parametrizing over the derived set
    both fixes that and widens the coverage: a refusal that worked for one spelling
    and not another would now be visible.
    """
    swap_readiness._results.clear()
    code = swap_readiness.main(["--pair", text])
    captured = capsys.readouterr()

    assert code == 2
    assert "not in Config.ALLOWED_PAIRS" in captured.err
    assert "Nothing was read." in captured.err
    assert "READY" not in captured.out


def test_a_malformed_pair_is_refused_and_says_the_shape(capsys):
    swap_readiness._results.clear()
    code = swap_readiness.main(["--pair", "nonsense"])
    captured = capsys.readouterr()

    assert code == 2
    assert "is not a pair" in captured.err
    assert "FROM:TO" in captured.err


@pytest.mark.parametrize("text", ["sol:grc", "SOL->GRC", "SOL/GRC", " SOL : GRC "])
def test_the_pair_spellings_an_operator_will_actually_type_are_accepted(text):
    """Lowercase, an arrow, a slash, stray spaces. Refusing these teaches nothing
    and costs a round trip, and every one of them is unambiguous."""
    assert swap_readiness.parse_pair(text) == ("SOL", "GRC")


def test_no_pair_means_every_checked_leg():
    """parse_pair("") is None, and None means the whole terminal -- not an empty
    scope, which would silently check nothing and report READY."""
    assert swap_readiness.parse_pair("") is None
    assert swap_readiness.legs_to_check(None) == swap_readiness.CHECKED_LEGS
    assert swap_readiness.legs_to_check(("SOL", "GRC")) == ("SOL", "GRC")


def test_the_401_names_the_testnet_conf_and_not_just_some_conf():
    """MEASURED TWICE, A WEEK APART, AND THE SECOND TIME THROUGH THIS PATH.

    2026-09-29: the operator's GUI wallet was serving RPC and answering HTTP 401
    because the credentials had been read out of the MAINNET conf. Two files named
    gridcoinresearch.conf sit one directory apart under ~/.GridcoinResearch and
    carry different credentials, and nothing about a 401 says which you used.
    regtest/daemons.why_nothing_answered() has said so ever since.

    2026-10-02: the identical 401 arrived through swap_readiness.py, whose own
    message said only "the conf that wallet actually reads". The operator read it
    and concluded "probably fat fingered the password" -- a plausible reading, and
    not the cause this project had already measured. Rule 8's drift arriving as a
    worse DIAGNOSIS rather than a wrong number: both copies looked right in their
    own file and only one had the finding in it.

    One sentence now, in regtest/daemons.GRC_CREDENTIALS_ARE_PER_NETWORK, imported
    by both.
    """
    detail = swap_readiness.explain_grc_failure(RuntimeError("401 Client Error: Unauthorized"), 25715)

    assert "25715" in detail
    assert "do not restart it" in detail, "a 401 PROVES the daemon is up"
    assert "TESTNET gridcoinresearch.conf" in detail
    assert "`testnet` subdirectory" in detail
    assert "DIFFERENT credentials" in detail
    assert "check WHICH FILE before retyping anything" in detail


def test_the_two_401_messages_carry_the_same_sentence():
    """The property that makes the merge real rather than a copy.

    A reader who hits this through the regtest harness and a reader who hits it
    through the preflight must be told the same thing -- which is only guaranteed
    while both read one constant.
    """
    preflight = swap_readiness.explain_grc_failure(RuntimeError("401 Unauthorized"), 25715)
    harness = why_nothing_answered(["HTTP 401 Unauthorized"])

    assert GRC_CREDENTIALS_ARE_PER_NETWORK in preflight
    assert GRC_CREDENTIALS_ARE_PER_NETWORK in harness


def test_the_401_guidance_names_a_path_and_never_a_value():
    """It tells you WHERE to look. Nothing here reads that file or echoes a secret.

    chains/daemon_conf.CONF_FALLBACK_NETWORK excludes GRC deliberately -- its conf
    is shared by a live staking wallet -- so naming the path is the most this may
    do, and it must not drift into reading it.
    """
    assert "rpcpassword=" not in GRC_CREDENTIALS_ARE_PER_NETWORK
    assert "rpcuser=" not in GRC_CREDENTIALS_ARE_PER_NETWORK
    # The variable NAMES are fine to print; a value or an = assignment is not.
    assert "GRC_RPC_PASS" in GRC_CREDENTIALS_ARE_PER_NETWORK


# --- a locked wallet is the CORRECT resting state, not a blocker ----------------


def test_a_locked_wallet_with_a_passphrase_available_is_a_PASS():
    """MEASURED ON THE OPERATOR'S HOST 2026-10-02, and this one line held the run.

    Every other precondition for SOL -> GRC passed. This check returned FAIL for
    `unlocked_until: 0` regardless of anything else, so the whole run reported NOT
    READY and the `&&` gate refused to start the workers -- for the CORRECT resting
    state of a staking wallet.

    The verdict was contradicted by the last clause of its own sentence:

        wallet is LOCKED ... payout_worker performs the full unlock itself when
        GRIDCOIN_WALLET_PASSPHRASE is set; this line is about the resting state

    It knew the unlock would happen and failed anyway. Locked is what a GRC wallet
    SHOULD be at rest -- chains/gridcoin_wallet_lock.unlock_for_sending() opens it
    for one send and locks it again, and it unlocks from locked, which is what
    `walletpassphrase` is for. Leaving a staking wallet fully unlocked is the state
    to avoid, and that is the opposite of this one.
    """
    state, detail = describe_wallet_lock({"unlocked_until": 0}, can_unlock=True)

    assert state == PASS
    assert "CORRECT resting state" in detail
    assert "unlock_for_sending" in detail, "the line must name what performs the unlock"
    assert "{'unlocked_until': 0}" in detail, "it still echoes what it read"


def test_a_locked_wallet_with_no_passphrase_is_still_a_FAIL():
    """The other half. A version that always passed would satisfy the test above.

    With nothing able to unlock it, a locked wallet means the payout refuses and
    the swap lands in 'failed', which nothing retries -- the failure that cost
    three rehearsals on 2026-10-01.
    """
    state, detail = describe_wallet_lock({"unlocked_until": 0}, can_unlock=False)

    assert state == FAIL
    assert "NOTHING CAN UNLOCK IT" in detail
    assert "GRIDCOIN_WALLET_PASSPHRASE is unset" in detail
    assert "CORRECT resting state" not in detail


def test_the_unlock_capability_defaults_to_absent():
    """So a caller that forgets to pass it FAILS rather than passing silently.

    The safe direction for a default on a money path: an omitted argument must not
    manufacture a capability the process may not have.
    """
    state, _ = describe_wallet_lock({"unlocked_until": 0})
    assert state == FAIL


def test_the_lock_line_and_the_unlock_line_read_the_same_variable(monkeypatch):
    """They must not disagree about whether an unlock is possible (rule 8).

    `payout unlock` and `GRC wallet lock` are two lines in one block, four apart,
    and this session has already shipped one contradiction between two sentences in
    that same block (supervisor.spawn_warning). Both read
    payout_service.WALLET_UNLOCK_ENV_VAR.
    """
    monkeypatch.setenv(WALLET_UNLOCK_ENV_VAR, "present-for-this-test-only")
    swap_readiness._results.clear()
    swap_readiness.check_payout_unlock({"GRC": CanSign()})
    unlock = unlock_row()
    locked_with = describe_wallet_lock({"unlocked_until": 0}, can_unlock=True)

    assert unlock[0] == PASS
    assert locked_with[0] == PASS, "one line says an unlock can be attempted; the other must agree"

    monkeypatch.delenv(WALLET_UNLOCK_ENV_VAR, raising=False)
    swap_readiness._results.clear()
    swap_readiness.check_payout_unlock({"GRC": CanSign()})

    assert unlock_row()[0] == FAIL
    assert describe_wallet_lock({"unlocked_until": 0}, can_unlock=False)[0] == FAIL


class FakeGridcoin:
    """A Gridcoin adapter that answers the two calls check_gridcoin() makes.

    CARRIES can_spend/payout_refusal as of 2026-10-02: payable_assets() now asks the
    adapter whether it can sign, and GRC is the one chain in these tests that genuinely
    can. See CanSign above for why the question moved from the key to the value.

    A stub rather than a mock of the whole adapter: get_balance() and
    call("getwalletinfo") are the entire contract that function depends on, and a
    stub answering only those fails loudly if a third call is added.
    """

    can_spend = True
    payout_refusal = ""

    def __init__(self, balance: float = 3780.09254497, unlocked_until: int = 0):
        self._balance = balance
        self._unlocked_until = unlocked_until

    def get_balance(self) -> float:
        return self._balance

    def call(self, method, *params):
        if method == "getwalletinfo":
            return {"unlocked_until": self._unlocked_until}
        raise AssertionError(f"check_gridcoin() made an unexpected call: {method}")


def test_check_gridcoin_reads_the_passphrase_from_the_environment(monkeypatch):
    """MUTATION-FOUND. Hardcoding can_unlock=True at the call site survived.

    describe_wallet_lock() takes the capability as an argument so it cannot
    disagree with the `payout unlock` line, and the CALL SITE is what supplies it.
    Every test for the new branches called describe_wallet_lock() directly, so the
    one line that reads os.environ was exercised by nothing -- an argument threaded
    correctly into a function nobody checked was threaded.

    The operator's real numbers: 3780.09254497 GRC on a locked wallet.
    """
    monkeypatch.setattr(swap_readiness, "build_adapters", lambda rpc: {"GRC": FakeGridcoin()})
    monkeypatch.setitem(swap_readiness.Config.RPC["GRC"], "port", 25715)

    monkeypatch.setenv(WALLET_UNLOCK_ENV_VAR, "present-for-this-test-only")
    swap_readiness._results.clear()
    swap_readiness.check_gridcoin()
    with_passphrase = next(r for r in swap_readiness._results if r[1] == "GRC wallet lock")

    monkeypatch.delenv(WALLET_UNLOCK_ENV_VAR, raising=False)
    swap_readiness._results.clear()
    swap_readiness.check_gridcoin()
    without = next(r for r in swap_readiness._results if r[1] == "GRC wallet lock")

    assert with_passphrase[0] == PASS, "a locked wallet plus a passphrase is the correct resting state"
    assert "CORRECT resting state" in with_passphrase[2]
    assert without[0] == FAIL, "the SAME wallet, with nothing able to unlock it"
    assert "NOTHING CAN UNLOCK IT" in without[2]


def test_the_whole_run_is_READY_on_the_operators_actual_state(monkeypatch, capsys):
    """END TO END, on the configuration that reported NOT READY for one wrong reason.

    Their 2026-10-02 run: every SOL and GRC precondition passing, 3780.09 GRC in a
    LOCKED wallet, GRIDCOIN_WALLET_PASSPHRASE set. The verdict was NOT READY and the
    `&&` gate refused to start the workers. It is READY, and this asserts the exit
    code rather than a sentence, because the exit code is what the gate reads.
    """
    monkeypatch.setattr(swap_readiness, "build_adapters",
                        lambda rpc: {"GRC": FakeGridcoin(), "SOL": CanSign()})
    monkeypatch.setitem(swap_readiness.Config.RPC["GRC"], "port", 25715)
    monkeypatch.setattr(swap_readiness, "check_solana",
                        lambda adapters: swap_readiness.record(PASS, "SOL", "stubbed"))
    monkeypatch.setattr(swap_readiness, "check_pricing",
                        lambda pair=None: swap_readiness.record(PASS, "pricing", "stubbed"))
    monkeypatch.setenv(WALLET_UNLOCK_ENV_VAR, "present-for-this-test-only")
    swap_readiness._results.clear()

    code = swap_readiness.main(["--pair", "SOL:GRC"])
    out = capsys.readouterr().out

    assert code == 0, f"the gate must open on this configuration:\n{out}"
    assert "READY" in out
    assert "SOL -> GRC can be created and paid" in out
