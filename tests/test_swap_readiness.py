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

from conftest import UNTRADED_ASSET, unallowed_directions
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
    rate_text,
)

MAINNET_PORT = 15715
TESTNET_PORT = 25779

# THE MARKER FOR "THIS RUN REPORTED A SEND PRECONDITION ON THIS CHAIN", in one
# place because four tests use it and they must not disagree about what they are
# looking for (rule 8 at its smallest).
#
# IT USED TO BE THE LITERAL "must be > 0 to pay a GRC leg", four times. That
# sentence was the whole of this file's balance test until 2026-10-03, when it
# PASSED on the morning a real 0.001 BTC deposit was taken against a 9049.69 GRC
# payout that a 3780.09 GRC wallet could not fund:
#
#     PASS  GRC wallet   3780.08854497 GRC  <- must be > 0 to pay a GRC leg
#
# True, and useless -- the number an operator needs is the largest payout the
# wallet can fund, which is now the ceiling on every swap this terminal accepts.
# So the line says that instead, and these tests track the stronger statement
# rather than being loosened to keep matching the weaker one (rule 2: a test
# changes to pin the stronger invariant, or it dies with the behavior).
class CanReadBalance:
    """A destination adapter that answers get_balance(). Nothing else is asked of it."""

    def __init__(self, balance):
        self._balance = balance

    def get_balance(self):
        return self._balance


class CannotReadBalance:
    """A destination adapter whose get_balance() raises, as a down daemon's does."""

    def get_balance(self):
        raise RuntimeError("connection refused")


PAYOUT_CAPACITY_MARKER = "payout this wallet can fund"


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


def test_an_unencrypted_grc_wallet_FAILS_rather_than_reporting_unknown():
    """MUTATION: return SKIP here instead of FAIL, which is what it used to do.

    MEASURED ON THE DESK DAEMON 2026-10-05, and the measurement is why this moved
    from SKIP to FAIL. getwalletinfo returned nine keys and no `unlocked_until`:

        balance, keypoololdest, keypoolsize, masterkeyid, mining-error,
        newmint, stake, staking, walletversion

    and `walletpassphrase <a deliberately wrong string> 1` answered

        -15  Error: running with an unencrypted wallet, but walletpassphrase was called.

    So the absence is not an unknown, it is a diagnosis -- the one
    chains/wallet_lock.ENCRYPTION_FIELD already documented, now confirmed behaviorally.

    THE COST OF THE OLD VERDICT, on that same run: swap_readiness printed
    "READY: all 41 checks passed" for a terminal that could not pay a single GRC
    swap. Both GRC payout paths fail on an unencrypted wallet -- with the passphrase
    set, walletlock raises -15 after the deposit is irreversible; without it,
    PayoutUnlockUnavailable refuses before the send. A green run over that is rule
    13's defect: "did nothing" rendering identically to "did work".
    """
    keys = {"balance": 500.0, "staking": True, "walletversion": 130000,
            "masterkeyid": "abc", "keypoolsize": 100}

    for can_unlock in (True, False):
        state, detail = describe_wallet_lock(keys, can_unlock=can_unlock)
        assert state == FAIL, (
            f"can_unlock={can_unlock}: both GRC payout paths fail on an unencrypted "
            f"wallet, so neither may report anything softer than FAIL"
        )
        assert "UNENCRYPTED" in detail
        assert "AFTER the deposit is confirmed and irreversible" in detail
        assert "walletversion" in detail, "it must still echo the keys it saw"
        assert "NOT ESTABLISHED" not in detail, (
            "this is a diagnosis now, not an unknown -- measured 2026-10-05"
        )

    # AND AN ENCRYPTED WALLET IS UNTOUCHED: the field present means the lock cycle
    # works, and that case must not be dragged into this one.
    state, _ = describe_wallet_lock({"unlocked_until": 0}, can_unlock=True)
    assert state == PASS


# test_an_unrecognized_response_is_NOT_read_as_unlocked() WAS HERE AND IS DELETED,
# not moved and not weakened. It seeded exactly the response above -- getwalletinfo
# with no lock field -- and asserted SKIP, i.e. "unknown". That reading is refuted:
# measured 2026-10-05, the absence means UNENCRYPTED and both GRC payout paths fail.
# The test it pinned is the behavior this file now treats as the defect, so keeping
# it would be two tests asserting opposite verdicts about one seeded input (rule 2:
# its test dies with it or changes to pin the stronger invariant).
#
# What it uniquely guarded -- "a response lacking the lock field must never read as
# PASS" -- is strictly implied by the FAIL assertions above, for both values of
# can_unlock. Nothing it covered is now uncovered.


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
    # **_ BECAUSE A STUB MUST ACCEPT THE SIGNATURE IT STANDS IN FOR. check_gridcoin()
    # gained `pays_out_grc` on 2026-10-03 so it could tell a GRC payout from a GRC
    # deposit, and a zero-arity lambda then raised TypeError INSIDE the runner --
    # which this test read as "the check crashed", failing for the stub's arity
    # rather than for anything about the behavior it covers.
    monkeypatch.setattr(swap_readiness, "check_gridcoin",
                        lambda **_: swap_readiness.record(PASS, "GRC", "reached"))
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

    def owns_address(self, address):
        """Not the desk's -- these fixtures all supply a CUSTOMER payout address.

        Added 2026-10-04 with the ownership gate in services/swap_service.
        refuse_unusable_payout_address(). A destination stub without it raises
        AttributeError, so the gate could not be exercised and every create_swap
        test failed on the harness instead of on its own subject. False is the
        honest default here; a test that wants the refusal returns True.
        """
        return False


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

    THE FIXTURE'S PREMISE CHANGED 2026-10-03. It used to assert that BTC->LTC was
    in `pair allowed` and ABSENT from `pairs checked here`, with "2 of 3" as the
    gap -- correct while CHECKED_LEGS was ("XRP", "SOL", "GRC") and a BTC leg was
    unverified here. The operator then said "add the btc/ltc legs now",
    check_bitcoin_like() landed, and BTC and LTC joined CHECKED_LEGS. Loosening
    the assertion would have been the wrong move (rule 2: a test changes to pin
    the stronger invariant, or it dies): the stronger statement is that a pair
    whose BOTH legs are checked is now REPORTED as checked, so the gap closes and
    the line says so. The gap half is pinned by the test below, on a leg this file
    genuinely has no check for.
    """
    monkeypatch.setattr(swap_readiness.Config, "ALLOWED_PAIRS",
                        {("SOL", "GRC"), ("XRP", "GRC"), ("BTC", "LTC")}, raising=False)
    swap_readiness._results.clear()
    swap_readiness.check_pair_is_allowed()
    allowed = next(row for row in swap_readiness._results if row[1] == "pair allowed")
    checked = next(row for row in swap_readiness._results if row[1] == "pairs checked here")

    assert "SOL->GRC" in allowed[2]
    assert "BTC->LTC" in allowed[2]
    assert "all 3" in allowed[2]
    assert "SOL->GRC" in checked[2]
    assert "BTC->LTC" in checked[2], (
        "both legs of BTC->LTC are in CHECKED_LEGS since the btc/ltc legs landed, so a line that "
        "omits it tells the operator a verified pair is unverified"
    )
    assert "3 of 3" in checked[2], "the gap between allowed and checked is stated, not implied"
    assert "(none)" in checked[2], (
        "rule 14: with no unchecked leg left, the absence is a RESULT and has to be printed as one -- "
        "a sentence that simply stops saying anything reads as a line that forgot to"
    )


def test_an_allowed_leg_with_no_check_here_is_named_rather_than_implied(monkeypatch):
    """The gap half, on a leg this file genuinely cannot check.

    This used to be carried by BTC, and BTC stopped being an example when
    check_bitcoin_like() landed. The property has nothing to do with BTC: it is
    that `pairs checked here` derives what it CANNOT answer from CHECKED_LEGS
    rather than naming chains in a string literal. So the fixture supplies a pair
    on a chain CHECKED_LEGS does not contain -- which is what BTC was on
    2026-10-01 -- and asserts the leg is named and the arithmetic reports the
    shortfall.

    Without this, the derived sentence could be reduced to a constant "(none)" and
    the test above would still pass.
    """
    monkeypatch.setattr(swap_readiness.Config, "ALLOWED_PAIRS",
                        {("SOL", "GRC"), ("DOGE", "GRC")}, raising=False)
    swap_readiness._results.clear()
    swap_readiness.check_pair_is_allowed()
    allowed = next(row for row in swap_readiness._results if row[1] == "pair allowed")
    checked = next(row for row in swap_readiness._results if row[1] == "pairs checked here")

    assert "DOGE->GRC" in allowed[2], "a pair this file cannot check is still ALLOWED and must be listed"
    assert "DOGE->GRC" not in checked[2]
    assert "1 of 2" in checked[2]
    assert "DOGE" in checked[2] and "NOT verified" in checked[2], (
        "the operator reads the screen, not CHECKED_LEGS: the leg that has no check has to be named "
        "on the line that stops short of it"
    )


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
    monkeypatch.setattr(swap_readiness, "check_gridcoin", lambda **_: swap_readiness.record(
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


def _pairs_the_gate_must_refuse() -> list[str]:
    """Every FROM:TO this terminal must refuse. From conftest, not spelled here.

    DERIVED, NOT SPELLED, AND THE SPELLING IS WHY. This test said `GRC:SOL` -- the
    reverse of ("SOL","GRC") and therefore outside ALLOWED_PAIRS when it was
    written, and INSIDE it from 2026-10-03, when the three *->SOL directions were
    enabled. The test then exercised the allowed path and asserted the refused one,
    which is a fixture rotting rather than a gate failing.

    SO IT WAS DERIVED FROM Config.ALLOWED_PAIRS -- "every ordered pair of the assets
    it names, minus the ones it carries" -- AND THAT DERIVATION HIT ZERO ON
    2026-10-04. Measured against this tree:

        2026-10-03   5 tradeable assets, 20 ordered pairs, 16 allowed, 4 refused
                     (BTC:XRP, LTC:XRP, SOL:XRP, XRP:SOL)
        2026-10-04   5 tradeable assets, 20 ordered pairs, 20 allowed, 0 refused

    The operator enabled exactly those four ("yeah let's figure out why and enable
    them"), so the derived list emptied and this gate had nothing left to exercise.
    test_there_is_an_unallowed_direction_left_to_refuse FAILED rather than the
    parametrize silently reporting a skip, which is what it was written to do.

    The denominator moved, not the gate, so the fixture gains a second source rather
    than the gate losing coverage: conftest.unallowed_directions() unions the derived
    half with UNTRADED_ASSET paired both ways. An asset this terminal does not price
    cannot become allowed by enabling a direction between assets it does, which is
    the rot that emptied the first half. Both halves live in conftest because
    tests/test_open_swap.py needed the same fixture in the same hour (rule 8).
    """
    return unallowed_directions(swap_readiness.Config.ALLOWED_PAIRS)


def test_there_is_an_unallowed_direction_left_to_refuse():
    """An empty parametrize set below would report as a SKIP, which reads as
    nothing-to-worry-about -- the exact shape test_nothing_payable_is_a_FAIL_not_a_SKIP
    is about, one layer up in the test suite itself. So the denominator is asserted
    here rather than left to pytest's collection message (rule 3, rule 14).

    THIS ASSERTION FIRED FOR REAL ON 2026-10-04 and the failure was the news, exactly
    as written: enabling the last four directions took Config.ALLOWED_PAIRS to 20 of
    20 and emptied the derived fixture. The fix was a second source for the fixture,
    not a relaxation here -- see _pairs_the_gate_must_refuse().
    """
    assert _pairs_the_gate_must_refuse(), (
        "there is no pair left for parse_pair()'s ALLOWED_PAIRS gate to refuse, so the path below "
        "is unreachable and the parametrize list is empty. conftest.unallowed_directions() unions "
        "an untraded asset into the fixture precisely so this cannot happen by enabling a pair -- "
        "if it is empty, UNTRADED_ASSET has become tradeable and the fixture needs a new one"
    )


def test_the_refusal_fixture_is_still_outside_the_config():
    """The fixture's own premise, asserted rather than trusted.

    conftest.UNTRADED_ASSET is only useful while this terminal does not trade it. If
    XMR is ever added to services/pricing.IDS and to Config.ALLOWED_PAIRS, every
    string the gate test feeds in becomes an ALLOWED pair and the gate test inverts --
    it would assert a refusal for a pair the terminal accepts, pass for the wrong
    reason, and report nothing.

    That is the same silent inversion the derived fixture suffered twice (GRC:SOL in
    2026-10-03, BTC:XRP in 2026-10-04), and the lesson both times was that the
    fixture's premise has to be a measurement and not a memory (rule 17).
    """
    allowed = set(swap_readiness.Config.ALLOWED_PAIRS)
    traded = {asset for pair in allowed for asset in pair}
    assert UNTRADED_ASSET not in traded, (
        f"{UNTRADED_ASSET} is now traded, so conftest.unallowed_directions() is feeding the pair "
        f"gate pairs it ACCEPTS and every assertion about a refusal below is passing for the wrong "
        f"reason. Pick an asset this terminal does not trade"
    )
    for text in _pairs_the_gate_must_refuse():
        from_asset, to_asset = text.split(":")
        assert (from_asset, to_asset) not in allowed, (
            f"{text} is in Config.ALLOWED_PAIRS and is being fed to a gate that must refuse it"
        )


@pytest.mark.parametrize("text", _pairs_the_gate_must_refuse())
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

    MEASURED AGAIN 2026-10-05, AND THE VERDICT SURVIVED WHILE THE SENTENCE DID NOT.
    This used to assert the line said "CORRECT resting state", i.e. that 0 means
    locked. It does not. The operator unlocked that wallet from the GUI padlock and
    500 tGRC was sent out of it with no walletpassphrase call, while getwalletinfo
    returned exactly {'unlocked_until': 0} -- because Gridcoin prints
    GetUnlockDeadline().value_or(0) and a GUI unlock arms no deadline
    (src/test/wallet_tests.cpp:1095-1097).

    PASS is still correct and for the same reason: unlock_for_sending() opens the
    wallet for one send from EITHER state. So this now pins the stronger property --
    PASS, and a line that does not claim to know a state this field cannot show.
    """
    state, detail = describe_wallet_lock({"unlocked_until": 0}, can_unlock=True)

    assert state == PASS
    assert "unlock_for_sending" in detail, "the line must name what performs the unlock"
    assert "{'unlocked_until': 0}" in detail, "it still echoes what it read"
    assert "NOT ESTABLISHED" in detail, "0 cannot distinguish locked from unlocked-no-deadline"
    assert "CORRECT resting state" not in detail, (
        "refuted 2026-10-05: a wallet reading exactly this paid out 500 tGRC"
    )
    assert "wallet is LOCKED" not in detail, "it must not assert a state it cannot see"


def test_a_locked_wallet_with_no_passphrase_is_still_a_FAIL():
    """The other half. A version that always passed would satisfy the test above.

    With nothing able to unlock it, a locked wallet means the payout refuses and
    the swap lands in 'failed', which nothing retries -- the failure that cost
    three rehearsals on 2026-10-01.

    FAIL IS KEPT DELIBERATELY ON AN AMBIGUOUS READING, 2026-10-05. Since 0 covers
    both locked and unlocked-with-no-deadline, this verdict is now a choice between
    two unequal errors rather than a deduction: a false FAIL costs the operator an
    investigation that finds nothing, while a false SKIP reports READY and strands a
    confirmed, irreversible deposit when the payout cannot send.
    """
    state, detail = describe_wallet_lock({"unlocked_until": 0}, can_unlock=False)

    assert state == FAIL
    assert "NOTHING IN THIS PROCESS CAN UNLOCK" in detail
    assert "GRIDCOIN_WALLET_PASSPHRASE is unset" in detail
    assert "CORRECT resting state" not in detail
    assert "UNAMBIGUOUS" not in detail, "refuted 2026-10-05; the field cannot carry that claim"
    assert "probe_wallet_unlock_scope" in detail, (
        "it must point at the function that CAN settle this behaviorally"
    )


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

    assert with_passphrase[0] == PASS, "unlock_for_sending() opens the wallet from either state"
    assert "NOT ESTABLISHED" in with_passphrase[2], "0 cannot separate locked from unlocked"
    assert without[0] == FAIL, "the SAME wallet, with nothing able to unlock it"
    assert "NOTHING IN THIS PROCESS CAN UNLOCK" in without[2]


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


def test_the_unpayable_FAIL_distinguishes_a_refused_NEW_swap_from_a_stranded_OPEN_one():
    """One line used to attach the stranded-deposit hazard to the wrong case.

    It said, for every unpayable destination: "A deposit would still be watched and
    CREDITED, and the payout would then refuse and land the swap in 'failed', which
    nothing retries."

    MEASURED 2026-10-03, after chains/solana.py's can_spend became derived: for a
    NEW swap that does not happen. services/swap_service.create_swap() reads
    chains/registry.why_cannot_pay_out() and refuses with nothing written --
    tests/test_solana_adapter.py::
    test_an_UNARMED_host_REFUSES_a_SOL_payout_swap_so_no_deposit_is_ever_taken
    proves zero swap rows over seeded rows through the real services. No swap row,
    no deposit target, nothing credited.

    AND THE SENTENCE IS STILL TRUE OF A SWAP ALREADY OPEN, which is why it stays: a
    swap created while the payout was ARMED, on a process since restarted without
    the variable, is exactly that stranded case. An operator produces it by
    unexporting one variable.

    So the line must say BOTH and say which is which. A hazard attached to the wrong
    case either scares an operator off a safe action or hides the unsafe one, and
    this check exists to send them looking in the right place.

    MUTATION: drop either clause. Whichever goes, one assertion below fails.
    """
    swap_readiness._results.clear()
    swap_readiness.check_payout_unlock({"SOL": CannotSign()})
    payout = [row for row in swap_readiness._results if row[1] == "payout chain"]
    assert len(payout) == 1, f"expected one payout-chain row, got {[r[1] for r in swap_readiness._results]}"
    verdict, _label, detail = payout[0]
    assert verdict == FAIL, f"an unpayable destination must FAIL, got {verdict}"

    # The NEW-swap half: a closed door, and it must say nothing is written.
    assert "REFUSED by create_swap()" in detail, detail
    assert "no deposit is watched or credited" in detail, detail
    # The ALREADY-OPEN half: the real trap, and it must still be named.
    assert "ALREADY OPEN" in detail, detail
    assert "nothing retries" in detail, (
        "the stranded-deposit hazard left the line entirely. It is real for an in-flight swap and an "
        "operator who ever armed this host needs to go and look"
    )
    # And the two must be distinguishable, not run together as one claim.
    assert detail.index("REFUSED by create_swap()") < detail.index("ALREADY OPEN"), (
        "the trap is described before the closed door, so a reader meets the hazard first and attaches "
        "it to the action they were about to take"
    )


def test_the_payout_FAIL_names_the_VARIABLE_to_export_per_destination():
    """The footer promises every line names the value to change. This one did not.

    MEASURED 2026-10-03 on the operator's own screen. They had just armed nothing,
    ran the check, and read:

        FAIL  payout chain  NOTHING CAN BE PAID OUT. Adapters built: GRC, SOL, XRP;
                            destination(s) needed: SOL. ...

    Not one variable named -- under a footer that says "Each line above names the
    value to change". They then had to be told SOL_PAYOUT_KEYPAIR_PATH and
    SOL_HOT_WALLET in conversation, which is the exact round trip rule 14 exists to
    remove: an operator reads the screen, not the source, and not a transcript.

    THE SENTENCE IS NOT WRITTEN IN swap_readiness.py AND MUST NOT BE.
    chains/registry.why_cannot_pay_out() already has it per asset, and it is the
    same sentence the customer page, /admin and the worker's spawn banner render. A
    second spelling here is how four implementations of the pay-out verdict came to
    disagree with three of them wrong (services/pair_view.py's header has that
    measurement), so this asserts the AUTHORITY's text appears rather than asserting
    a copy of it.

    MUTATION: have _why_each_destination_refuses() return "". Both assertions below
    fail, and the screen goes back to naming nothing.
    """
    swap_readiness._results.clear()
    swap_readiness.check_payout_unlock({"SOL": CannotSign()})
    detail = next(row[2] for row in swap_readiness._results if row[1] == "payout chain")

    assert "WHAT TO CHANGE, per destination:" in detail, detail
    assert "SOL:" in detail, "the destination that refused is not named, so a multi-chain run is unreadable"
    assert "cannot pay out in this test" in detail, (
        "the per-destination line does not carry the ADAPTER's own refusal. If swap_readiness started "
        "writing its own sentence instead, this page and every other surface would be free to word one "
        "refusal two ways -- which is the defect this assertion exists to prevent"
    )


def test_a_destination_with_NO_adapter_is_not_reported_as_a_refusing_one():
    """Two different problems, and conflating them sends the operator to the wrong fix.

    No adapter means a chain this process cannot reach at all -- the `adapters
    built` line above already says which. An adapter that REFUSES is reachable and
    unarmed. Printing the registry's reachability sentence again here would read as
    a second finding about the same chain.
    """
    # THE FIXTURE WAS THE DEFECT ON THE FIRST ATTEMPT AND IT IS WORTH RECORDING.
    # `{"GRC": CanSign()}` was handed in to mean "SOL is absent", and GRC is itself
    # the destination of an allowed pair and CAN sign -- so the check PASSED, there
    # was a `payout chain` row, and the test read the PASS row's text looking for a
    # refusal. It failed on "has an adapter AND is the destination of an allowed
    # pair", which is the correct sentence for the state the fixture actually built.
    #
    # The premise needs a payable set that is EMPTY while a destination is absent:
    # GRC present but unable to sign, SOL absent entirely. Then nothing is payable,
    # the check FAILS, and SOL's line is the absent-chain case this test is about.
    swap_readiness._results.clear()
    swap_readiness.check_payout_unlock({"GRC": CannotSign()})
    payout = [row for row in swap_readiness._results if row[1] == "payout chain"]
    assert payout and payout[0][0] == FAIL, (
        f"the fixture did not produce a payout-chain FAIL, so there is no per-destination list to "
        f"inspect: {[(r[0], r[1]) for r in swap_readiness._results]}"
    )
    detail = payout[0][2]
    assert "no adapter in this process" in detail, detail
    assert "see `adapters built` above" in detail, (
        "an absent chain's line does not point at the line that already lists it, so the reader gets "
        "the same fact twice with no indication they are the same fact"
    )


def test_a_GRC_SOURCE_run_does_not_report_GRC_SEND_preconditions(monkeypatch):
    """GRC -> SOL sends no GRC, so a balance and a send-capable unlock are not its preconditions.

    THE DEFECT, MEASURED ON THE OPERATOR'S SCREEN 2026-10-03. `--pair GRC:SOL`
    reported:

        PASS  payout unlock  GRC payout unlock  GRIDCOIN_WALLET_PASSPHRASE IS set
        PASS  GRC wallet     3780.09054497 GRC  <- must be > 0 to pay a GRC leg
        SKIP  GRC wallet lock  ... a staking-only unlock CANNOT send ...

    Every one of those is true of the PROCESS and irrelevant to the RUN. GRC is the
    SOURCE of that pair: the customer deposits GRC and the desk pays SOL, and
    services/payout_service.payout_unlock_context() is keyed on the DESTINATION
    asset, so the Gridcoin unlock path is a no-op that never executes.

    AND IT MISLED A READER THE SAME HOUR. The operator was told a GRC-side failure
    on this swap would point at the wallet lock. There is no GRC-side send to fail.
    A PASS beside an irrelevant precondition is worse than no line at all: it reads
    as a precondition that was checked and met, which is exactly the inference that
    was drawn.

    WHAT THE SOURCE WALLET ACTUALLY DOES is derive a deposit address --
    `getnewaddress`, a write and not a spend -- and the lines say so instead,
    because naming the real operation is the point of the page.

    MUTATION: pass pays_out_grc=True. The balance line comes back, naming the "
    pay a GRC leg" and the lock line to the send prose, and both assertions fail.
    """
    monkeypatch.setattr(swap_readiness, "gridcoin_precheck",
                        lambda _port: (True, PASS, "port 25715 is a test chain"))
    monkeypatch.setattr(swap_readiness, "build_adapters", lambda _rpc: {"GRC": FakeGridcoin()})

    swap_readiness._results.clear()
    swap_readiness.check_gridcoin(pays_out_grc=False)
    rows = {row[1]: row for row in swap_readiness._results}

    wallet = rows.get("GRC wallet")
    assert wallet, f"no GRC wallet line at all: {sorted(rows)}"
    assert PAYOUT_CAPACITY_MARKER not in wallet[2], (
        f"a send precondition is asserted for a direction that sends no GRC: {wallet[2]}"
    )
    assert "SOURCE in this run" in wallet[2], wallet[2]
    assert "NOT a precondition" in wallet[2], wallet[2]

    lock = rows.get("GRC wallet lock")
    assert lock, f"the lock line vanished rather than being skipped with a reason: {sorted(rows)}"
    assert lock[0] == SKIP, f"the lock was CHECKED for a direction that sends no GRC: {lock}"
    assert "nothing sends GRC" in lock[2], lock[2]
    assert "CANNOT send" not in lock[2], (
        f"the send-capability prose survived into a run with no GRC send: {lock[2]}"
    )
    # And it must still name the real operation, or the reader learns nothing from
    # the skip (rule 14: `(none)` is a result, a blank is not).
    assert "getnewaddress" in lock[2], lock[2]


def test_a_GRC_DESTINATION_run_still_reports_every_send_precondition(monkeypatch):
    """So the scoping cannot pass by silencing the checks everywhere.

    A version of check_gridcoin() that skipped the balance and the lock
    unconditionally would satisfy the test above and remove a real guard from
    SOL -> GRC, where the desk DOES send GRC and a staking-only unlock blocks it.
    """
    monkeypatch.setattr(swap_readiness, "gridcoin_precheck",
                        lambda _port: (True, PASS, "port 25715 is a test chain"))
    monkeypatch.setattr(swap_readiness, "build_adapters", lambda _rpc: {"GRC": FakeGridcoin()})

    swap_readiness._results.clear()
    swap_readiness.check_gridcoin(pays_out_grc=True)
    rows = {row[1]: row for row in swap_readiness._results}

    assert PAYOUT_CAPACITY_MARKER in rows["GRC wallet"][2], rows["GRC wallet"][2]
    lock = rows.get("GRC wallet lock")
    assert lock, "the lock check did not run for a direction that pays out GRC"
    assert "nothing sends GRC" not in lock[2], (
        "the source-chain skip leaked into a destination run, so the send guard is gone where it matters"
    )


def test_main_PASSES_THE_SCOPE_to_check_gridcoin_for_both_directions(monkeypatch, capsys):
    """The CALL SITE, not the branch. The branch was right and nothing wired it.

    THE TWO TESTS ABOVE PASSED WITH THE WIRING BROKEN, which is the finding. They
    call check_gridcoin(pays_out_grc=...) directly, so replacing main()'s
    `pays_out_grc=(pair is None or pair[1] == "GRC")` with a hardcoded True left
    them green while every scoped run went back to reporting GRC send preconditions
    it does not have. Verified by mutation 2026-10-03: the suite stayed at exit 0.

    That is this repository's recurring defect shape -- a correct function whose
    call site discards the distinction -- and it has been caught three times in one
    day: services/payout_service.payable_assets(), show_payout_fees.report_asset(),
    and now this. A unit test on a branch is not a test of whether the branch is
    reached.

    So this drives main() for BOTH directions over the same stubbed wallet and
    asserts the rendered page differs. Nothing is patched except the daemon.
    """
    monkeypatch.setattr(swap_readiness, "gridcoin_precheck",
                        lambda _port: (True, PASS, "port 25715 is a test chain"))
    monkeypatch.setattr(swap_readiness, "build_adapters", lambda _rpc: {"GRC": FakeGridcoin()})

    swap_readiness._results.clear()
    swap_readiness.main(["--pair", "GRC:SOL"])
    source_run = capsys.readouterr().out

    swap_readiness._results.clear()
    swap_readiness.main(["--pair", "SOL:GRC"])
    destination_run = capsys.readouterr().out

    # GRC as the SOURCE: no send precondition anywhere on the page.
    assert "GRC is the SOURCE in this run" in source_run, (
        "main() did not pass the scope to check_gridcoin(), so a GRC -> SOL run still reports GRC send "
        "preconditions. The branch exists and nothing reaches it"
    )
    assert PAYOUT_CAPACITY_MARKER not in source_run, (
        "a scoped GRC -> SOL run asserts a balance precondition for a direction that sends no GRC"
    )

    # GRC as the DESTINATION: the send preconditions are back, so the scoping
    # cannot pass by silencing them everywhere.
    assert PAYOUT_CAPACITY_MARKER in destination_run, (
        "a SOL -> GRC run lost the balance precondition, so the guard is gone where the desk does send GRC"
    )
    assert "GRC is the SOURCE in this run" not in destination_run, destination_run[-400:]


def test_main_PASSES_THE_DIRECTION_to_the_XRP_deposit_check(monkeypatch, capsys):
    """GRC -> XRP pays OUT in XRP, so XRP_DEPOSIT_ACCOUNT is not its precondition.

    THE SYMMETRIC DEFECT TO check_gridcoin()'s, found an hour after fixing that one
    by reading the operator's unscoped run -- and I had not thought to look for it.

    XRP_DEPOSIT_ACCOUNT is XRP's DEPOSIT TARGET: services/swap_service.TAG_ATTRIBUTION
    names it as the shared account every XRP deposit is attributed against by
    DestinationTag. A swap that PAYS OUT in XRP receives nothing there.

    MEASURED, and the proof was already on the operator's host: GRC -> XRP reads
    AVAILABLE on the customer page with XRP_DEPOSIT_ACCOUNT unset, because
    services/pair_view.pair_serviceability() asks why_cannot_take_deposits() about the
    SOURCE only. This page said NOT READY for that same pair. Two surfaces, one pair,
    opposite verdicts -- which is exactly the 2026-10-02 defect that started this
    whole thread, in a third place.

    The leg filter cannot catch it: it only asks whether XRP appears in the pair AT
    ALL, not whether it appears as the deposit leg or the payout leg.

    AND THIS DRIVES main(), NOT THE BRANCH. The branch-only version of this test
    passes with the call site hardcoded, which is how the GRC fix shipped broken an
    hour ago. Verified by mutation 2026-10-03.
    """
    monkeypatch.delenv("XRP_DEPOSIT_ACCOUNT", raising=False)
    monkeypatch.setattr(swap_readiness.Config, "XRP_DEPOSIT_ACCOUNT", "", raising=False)

    swap_readiness._results.clear()
    swap_readiness.main(["--pair", "GRC:XRP"])
    paying_out = capsys.readouterr().out

    swap_readiness._results.clear()
    swap_readiness.main(["--pair", "XRP:GRC"])
    taking_deposits = capsys.readouterr().out

    # XRP as DESTINATION: the account is not demanded, and the line says why.
    assert "XRP is the DESTINATION in this run" in paying_out, (
        "main() did not pass the direction to check_deposit_account(), so GRC -> XRP still demands a "
        "deposit account it never uses -- and reports NOT READY for a pair the customer page offers"
    )
    assert "refuses every XRP-SOURCE swap" not in paying_out, (
        "the FAIL message for a missing deposit account appeared on a pair that takes no XRP deposit"
    )

    # XRP as SOURCE: it IS demanded, so the scoping cannot pass by never asking.
    assert "refuses every XRP-SOURCE swap" in taking_deposits, (
        "XRP:GRC stopped requiring the deposit account, so the custody gate is gone where it matters -- "
        "create_swap() would refuse and nothing here would have said so"
    )
    assert "XRP is the DESTINATION in this run" not in taking_deposits, taking_deposits[-300:]


# --- the BTC and LTC legs ----------------------------------------------------
#
# ADDED 2026-10-03 with check_bitcoin_like(), on the operator's "add the btc/ltc
# legs now". Every assertion below is shaped by something that already happened on
# their host rather than by the function's surface: the -18 case is the one
# chain_balances.py found while swap_readiness said READY, and the no-socket case
# is the 2026-09-25 balance leak.


class FakeBitcoinLike:
    """A Bitcoin-derived adapter that RECORDS what was asked of it.

    The recording is the point in two of the tests below, and it is why these do
    not use a bare lambda. "Did it refuse mainnet" cannot be asserted from a FAIL
    verdict -- a version that connected, read the balance, printed it and THEN
    failed would satisfy a verdict-only assertion while doing the exact thing the
    refusal exists to prevent. So `calls` is the assertion, and the verdict is the
    corroboration.
    """

    def __init__(self, *, walletinfo=None, error=None, balance=0.0, wallets=("regtest_htlc_harness",)):
        self._walletinfo = walletinfo if walletinfo is not None else {"walletname": "regtest_htlc_harness"}
        self._error = error
        self._balance = balance
        self._wallets = wallets
        self.calls: list[str] = []

    def call(self, method, *_args, **_kwargs):
        self.calls.append(method)
        if method == "listwalletdir":
            return {"wallets": [{"name": name} for name in self._wallets]}
        if method == "getwalletinfo":
            if self._error is not None:
                raise self._error
            return self._walletinfo
        raise AssertionError(f"check_bitcoin_like asked for {method!r}, which this fixture does not stub")

    def get_balance(self):
        self.calls.append("get_balance")
        return self._balance


def _rows_by_name():
    return {row[1]: row for row in swap_readiness._results}


@pytest.mark.parametrize(("asset", "mainnet_port"), [("BTC", 8332), ("LTC", 9332)])
def test_a_mainnet_btc_or_ltc_port_opens_no_socket_at_all(monkeypatch, asset, mainnet_port):
    """The 2026-09-25 leak, one chain over.

    chain_precheck() is generalized from gridcoin_precheck() precisely so this
    property is one implementation rather than three, and this is the test that
    the GENERALIZATION carried the property rather than only the signature.
    """
    adapter = FakeBitcoinLike()
    monkeypatch.setitem(swap_readiness.Config.RPC, asset, {"port": mainnet_port})
    swap_readiness._results.clear()
    swap_readiness.check_bitcoin_like(asset, {asset: adapter}, pays_out=True)
    rows = _rows_by_name()

    assert adapter.calls == [], (
        "a mainnet port must not be read. Reading means printing a real balance into whatever "
        "transcript this output lands in, which is what happened on 2026-09-25"
    )
    assert rows[f"{asset} network"][0] == FAIL
    assert "did NOT connect" in rows[f"{asset} network"][2]
    assert rows[f"{asset} wallet"][0] == SKIP, "the wallet line must say it was skipped, not go missing"


@pytest.mark.parametrize(("asset", "port"), [("BTC", 18443), ("LTC", 19443)])
def test_an_unloaded_wallet_fails_the_leg_and_names_the_wallet_on_disk(monkeypatch, asset, port):
    """The exact failure the operator hit, and the reason this function exists.

    Measured on their host 2026-10-03: swap_readiness reported `adapters built
    BTC, GRC, LTC, SOL, XRP` and a READY banner, while chain_balances.py -- a
    different tool -- reported

        FAIL  BTC balance: RPCError: No wallet is loaded. (rpc code -18)
              this daemon has 1 wallet(s) on disk: regtest_htlc_harness

    An adapter that CONNECTS is not a wallet that can act: getnewaddress returns
    -18 the same way getbalance does, so the swap would have died deriving the
    deposit address, after the quote and in front of the customer.

    The hint is asserted to come from which_wallets_are_on_disk() -- the wallet
    name reaches the line -- rather than being a second spelling of it here
    (rule 8).
    """
    adapter = FakeBitcoinLike(error=RuntimeError("No wallet is loaded. (rpc code -18)"))
    monkeypatch.setitem(swap_readiness.Config.RPC, asset, {"port": port})
    swap_readiness._results.clear()
    swap_readiness.check_bitcoin_like(asset, {asset: adapter}, pays_out=True)
    rows = _rows_by_name()

    assert rows[f"{asset} network"][0] == PASS, "a regtest port is the case that connects"
    assert rows[f"{asset} wallet"][0] == FAIL
    assert "-18" in rows[f"{asset} wallet"][2]
    assert "regtest_htlc_harness" in rows[f"{asset} wallet"][2], (
        "the operator reads the screen: the line has to name the wallet that could be loaded"
    )
    assert "get_balance" not in adapter.calls, (
        "a balance read after a -18 adds a second failure line for one cause, and the leg is already "
        "established as unable to act"
    )
    assert f"{asset} balance" not in rows, "no balance line at all, rather than a second FAIL for one cause"


@pytest.mark.parametrize("asset", ["BTC", "LTC"])
def test_a_source_leg_does_not_assert_a_balance_it_does_not_need(monkeypatch, asset):
    """BTC->GRC sends no BTC, so a zero BTC balance is not a defect.

    This is the third instance of one defect in this file -- check_gridcoin() and
    check_deposit_account() both carried `pays_out` after the same correction --
    and it is the direction that FAILS WRONG: a fresh regtest chain holds nothing,
    so asserting a balance on the source would report NOT READY for a run that is
    entirely fine.

    get_balance is asserted unmade, not merely unreported: a version that read it
    and then declined to record the row would still have printed the operator's
    balance through whatever the adapter logs.
    """
    adapter = FakeBitcoinLike(balance=0.0)
    monkeypatch.setitem(swap_readiness.Config.RPC, asset, {"port": 18443 if asset == "BTC" else 19443})
    swap_readiness._results.clear()
    swap_readiness.check_bitcoin_like(asset, {asset: adapter}, pays_out=False)
    rows = _rows_by_name()

    assert rows[f"{asset} wallet"][0] == PASS
    assert rows[f"{asset} balance"][0] == SKIP
    assert "SOURCE" in rows[f"{asset} balance"][2]
    assert "get_balance" not in adapter.calls


@pytest.mark.parametrize(("balance", "verdict"), [(0.0, FAIL), (1.25, PASS)])
def test_a_destination_leg_needs_coins_and_zero_is_the_failing_case(monkeypatch, balance, verdict):
    """Both halves, because a check that always passed would satisfy either alone.

    GRC->BTC pays out BTC. A wallet that is loaded and empty can derive a payout
    address and cannot fund the payout, which is a deposit taken against a payout
    that will fail -- the same shape the SOL keypair gate was added for.
    """
    adapter = FakeBitcoinLike(balance=balance)
    monkeypatch.setitem(swap_readiness.Config.RPC, "BTC", {"port": 18443})
    swap_readiness._results.clear()
    swap_readiness.check_bitcoin_like("BTC", {"BTC": adapter}, pays_out=True)
    rows = _rows_by_name()

    assert rows["BTC balance"][0] == verdict
    assert PAYOUT_CAPACITY_MARKER in rows["BTC balance"][2], (
        "state what the number means, next to the number: the ceiling is what decides whether a "
        "swap can be created, where a bare '> 0' passed on a wallet holding 41.8% of its payout"
    )


def test_a_missing_adapter_names_the_variable_rather_than_the_absence(monkeypatch):
    """"no BTC adapter" tells the operator nothing they can act on.

    why_unconfigured() is the authority for which variable is unset, and it is
    reached rather than re-derived here for the reason rule 8 gives: a second
    sentence about BTC credentials would agree on the day it was written.
    """
    swap_readiness._results.clear()
    swap_readiness.check_bitcoin_like("BTC", {}, pays_out=True)
    rows = _rows_by_name()

    assert rows["BTC"][0] == FAIL
    assert "BTC_RPC" in rows["BTC"][2], "the line has to name a variable the operator can export"


def test_the_wallet_line_says_when_it_cut_the_daemons_message(monkeypatch):
    """The call site, because the function being right is not the thing that failed.

    Four times this session a correct function's CALL SITE discarded or bypassed
    its result -- payable_assets(), show_payout_fees.report_asset(),
    check_gridcoin() and check_deposit_account() -- so clipped() being tested in
    tests/test_report_block.py is necessary and is not sufficient. This asserts
    the marker reaches the row the operator reads.

    The message is bitcoind's real -18 text at its real length, which is what
    reached their screen as "... (Note: A default wallet is no".
    """
    long_error = (
        "No wallet is loaded. Load a wallet using loadwallet or create a new one with createwallet. "
        "(Note: A default wallet is no longer automatically created)"
    )
    adapter = FakeBitcoinLike(error=RuntimeError(long_error))
    monkeypatch.setitem(swap_readiness.Config.RPC, "BTC", {"port": 18443})
    swap_readiness._results.clear()
    swap_readiness.check_bitcoin_like("BTC", {"BTC": adapter}, pays_out=True)
    line = _rows_by_name()["BTC wallet"][2]

    assert "cut by this tool" in line, (
        "a sentence that stops mid-word with no marker is ambiguous between a truncated daemon "
        "message, a dropped terminal line, and a tool clipping it -- and only the last needs no action"
    )
    assert "regtest_htlc_harness" in line, "the clip must not swallow the hint that follows it"


@pytest.mark.parametrize(
    ("adapters", "fragment"),
    [
        ({}, "NOT established"),
        ({"GRC": CannotReadBalance()}, "NOT established"),
    ],
)
def test_a_ceiling_that_cannot_be_computed_says_so_instead_of_printing_a_number(adapters, fragment):
    """A readiness page that dies on a missing variable cannot report it.

    get_network_fee_reserve() RAISES for an asset with no reserve configured, which
    is right for a quote and wrong here -- so payout_ceiling_note() asks through a
    wrapper that returns None. Either way the line must print the absence rather
    than a figure: a 0.0 ceiling would send an operator to fund a wallet that may
    be full, which is rule 13's "did nothing must not look like did work" applied
    to a number instead of a verdict.
    """
    note = swap_readiness.payout_ceiling_note(adapters, "GRC")

    assert fragment in note
    assert "can fund is" not in note, "a sentinel must never render as a ceiling"


def test_the_ceiling_note_names_the_consequence_and_not_just_the_number():
    """The number alone is trivia; what it DECIDES is the content.

    Since 2026-10-03 this figure is the ceiling on every swap the terminal will
    create -- services/swap_service.refuse_unless_the_payout_can_be_funded()
    refuses above it, before any row exists. An operator reading a bare balance
    cannot know that, and the operator reads the screen, not the source (rule 14).
    """
    note = swap_readiness.payout_ceiling_note({"GRC": CanReadBalance(3780.08854497)}, "GRC")

    assert "3780.08854497" in note
    assert "REFUSED at creation" in note
    assert "before any deposit is taken" in note


# --- a rate that cannot show its own value ------------------------------------


@pytest.mark.parametrize(
    ("rate", "shown"),
    [
        (9142021.62107, "9142021.6211"),  # BTC->GRC, the readable majority: fixed format stays
        (0.0062, "0.0062"),               # GRC->XRP
        (0.0001, "0.0001"),               # the exact threshold four decimals can still carry
        (1.09e-07, "1.09e-07"),           # GRC->BTC, which printed 0.0000
        (1.76e-05, "1.76e-05"),           # XRP->BTC, which printed 0.0000
        (0.0, "0"),                       # a result, not a formatting failure
        (-1.0, "-1"),                     # a broken feed must stay visible
    ],
)
def test_a_small_rate_keeps_its_digits_instead_of_printing_as_zero(rate, shown):
    """1 GRC = 0.0000 BTC, on the first all-green run of the whole terminal.

    MEASURED ON THE OPERATOR'S SCREEN 2026-10-03. The pricing line formatted every
    rate with `:.4f`, so GRC->BTC (about 1.09e-07) and XRP->BTC (about 1.76e-05)
    both rendered as exactly 0.0000 -- for pairs this terminal WILL quote. A rate
    of zero reads as a broken price feed and is indistinguishable from one.

    FOUR DECIMALS STAYS WHERE IT WORKS, which is why the first two rows are here:
    switching everything to %g would print 9142021.6211 as 9.14202e+06, trading a
    readable majority for an unreadable minority.

    THE LAST ROW IS A CORRECTION TO MY OWN FIRST FIX. It read `if rate <= 0:
    return "0"`, which rendered -1.0 as "0" -- the same defect pointed the other
    way. A negative rate cannot come from two positive USD prices, so if one
    appears it is a broken feed and must be visible rather than flattened into a
    plausible zero.
    """
    assert rate_text(rate) == shown


def test_the_pricing_line_uses_the_formatter_rather_than_its_own_format(monkeypatch):
    """The call site, because the formatter being right is not what failed.

    `:.4f` was inline in the f-string that builds the line, so the only way to be
    sure the fix reaches the screen is to read the screen's line.
    """
    monkeypatch.setattr(swap_readiness.Config, "ALLOWED_PAIRS", {("GRC", "BTC")}, raising=False)
    monkeypatch.setattr(swap_readiness, "fetch_usd_prices",
                        lambda *a, **k: {"GRC_USD": 0.00928077, "BTC_USD": 84845.0})
    swap_readiness._results.clear()
    swap_readiness.check_pricing()
    row = swap_readiness._results[0]

    assert row[0] == PASS
    assert "0.0000 BTC" not in row[2], "the defect: a quotable pair's rate rendered as zero"
    assert "1 GRC = 1.09e-07 BTC" in row[2]
