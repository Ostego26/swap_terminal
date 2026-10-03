"""The XRP payout, wired to the worker: which blocker was real, and every refusal in order.

Role: test (seeded transport, stubbed submit, real database, no socket)
Reads: chains/xrp_payout_seed.py, chains/xrp.py, services/payout_service.py,
      services/swap_service.py
Writes: a temp SQLite database per test. Nothing to any chain.
Can move funds: no. requests.post is replaced by tests/xrp_seeded_transport.py's
      Recorder and xrpl.transaction.submit_and_wait by a stub that appends the
      Payment to a list, so the armed path runs end to end and reaches no
      ledger. Every account is either derived in-process by Wallet.create() or
      taken from tests/valid_addresses.py.
Mainnet-safe: yes

WHAT THESE TESTS ESTABLISH, said first because a green suite is the thing most
likely to be mistaken for evidence that a payout path works.

THE CLAIM THAT WAS ON THE CUSTOMER PAGE, and it named TWO blockers:

    XRP cannot pay out: it holds no signing key, and services/payout_service.py
    calls send_to_address() without the arming token, so an XRP payout raises
    and the swap lands in `failed` with the deposit already credited.

Both clauses were true. They were different problems, and only the FIRST was the
real blocker -- which these tests establish rather than assert:

  test_arming_the_old_call_site_alone_would_not_have_sent_anything
        passes the arming token with no seed, exactly as a change that wired only
        the call site would have, and the send still refuses. So the second
        clause was the cheaper half: fixing it alone turns XRPSendNotArmed's
        first branch into its second.
  test_the_adapter_cannot_reach_a_seed_even_when_the_variable_is_set
        sets XRP_PAYOUT_SECRET_SEED and calls send_to_address() with no seed
        argument. It still refuses, which is what "the adapter holds no key"
        means now that a variable exists -- and it is the assertion that fails
        if chains/xrp.py ever imports signing_seed().

THEY ESTABLISH, against seeded inputs and a real database:

  - the DEFAULT is a refusal. With the variable unset, can_spend is False,
    create_swap() refuses an XRP-destination swap, and nothing is taken
  - the two environment refusals happen BEFORE any network call, asserted on an
    empty recorder rather than on the absence of an exception
  - a mainnet network_id refuses before account_info and before any signature,
    through the service path and not only through the adapter
  - a seed that derives a different account refuses with nothing submitted
  - a payment that would breach the reserve refuses with nothing submitted
  - the Payment that reaches submit_and_wait carries no tfPartialPayment bit,
    asserted on the SERIALIZED transaction
  - an armed payout marks the payouts row `broadcast` and the swap `completed`
    with the txid recorded
  - the SEED is in no log record at DEBUG (including repr(record.args)), in no
    exception string, in no database column, on no stream, and in no return value
  - every other chain still gets exactly `send_to_address(address, amount)`

THEY ESTABLISH NOTHING ABOUT A REAL LEDGER. Nothing here was submitted to any
network. s.altnet.rippletest.net:51234 is unreachable from the container these
were written in, so the actual broadcast through this wiring is a PROPOSAL and
not a fix (rule 16). xrp_payout_verify.py --via-service is the one command that
changes that, and it has to be run by the operator.
"""

from __future__ import annotations

import hashlib
import logging
import sqlite3

import pytest
import requests
from chains.xrp import XRPAdapter, XRPRPCError
from chains.xrp_payout_seed import (
    SIGNING_SEED_ENV_VAR,
    missing_seed_refusal,
    signing_seed,
    signing_seed_is_present,
)
from chains.xrp_signing import (
    CONFIRM_XRP_SEND,
    TF_PARTIAL_PAYMENT,
    XRPMainnetRefused,
    XRPReserveRefused,
    XRPSendNotArmed,
    XRPSigningRefused,
)
from db import SCHEMA, apply_migrations, connect_db, dict_factory
from services.payout_service import (
    PayoutSigningUnavailable,
    broadcast_payout,
    process_pending_payouts,
)
from services.swap_service import create_swap
from valid_addresses import GRC_PAYOUT, XRP_CUSTOMER_PAYOUT, XRP_HOT_ACCOUNT
from xrp_seeded_transport import TESTNET_URL, Recorder, account_info, server_info

# xrpl-py is an OPTIONAL dependency and importorskip is the mechanism for both of
# these, for the reason tests/test_xrp_adapter.py records: a plain top-of-file
# `import xrpl.transaction` runs BEFORE any skip could take effect and would break
# collection on a host without the library. No suppression is needed (rule 19).
wallet_module = pytest.importorskip(
    "xrpl.wallet", reason="xrpl-py absent, so real seed derivation and the signed Payment are UNCHECKED"
)
transaction_module = pytest.importorskip(
    "xrpl.transaction", reason="xrpl-py absent, so the signed-and-submitted path is UNCHECKED"
)

# A SEED-SHAPED STRING THAT IS NOT A SEED, for the tests that only need the
# variable to be PRESENT. Derived from a published hash of a phrase written in this
# file, so anybody who can read this test can reproduce it -- which is the
# definition of not secret -- and the same construction tests/valid_addresses.py and
# tests/test_grc_address_proof.py already use rather than a literal somebody typed.
#
# It is never decoded. chains/xrp_payout_seed.signing_seed_is_present() reads
# bool() of the variable, so every test that uses this one stops before xrpl-py sees
# it. The tests that need a seed that actually derives an account use
# Wallet.create().
NOT_A_SEED = "sNotASeed" + hashlib.sha256(b"swap_terminal xrp wiring fixture").hexdigest()[:22]

CONFIG_BASE = {
    "GRC_MIN_CONFIRMATIONS": 6,
    "XRP_MIN_CONFIRMATIONS": 1,
    "AMOUNT_TOLERANCE_PCT": 0.01,
}


class StubResponse:
    def __init__(self, result):
        self.result = result


def validated_success(tx_hash="B" * 64):
    return {"meta": {"TransactionResult": "tesSUCCESS"}, "hash": tx_hash, "validated": True}


class RecordingAdapter:
    """Records how it was CALLED, which is the whole subject of the dispatch tests.

    Both halves of the call are kept -- positional args and keywords, separately --
    because "XRP gets three keywords and GRC gets none" is a statement about the
    SHAPE of the call and not about its result. A stub that merged them into one
    bag would pass whether or not a BTC send had grown a `seed=` keyword.
    """

    asset = "STUB"
    can_spend = True
    payout_refusal = ""

    def __init__(self):
        self.calls: list[tuple[tuple, dict]] = []

    def send_to_address(self, *args, **kwargs) -> str:
        self.calls.append((args, kwargs))
        return "stub-txid"

    def get_balance(self) -> float:
        return 1000.0


@pytest.fixture(autouse=True)
def _no_ambient_seed(monkeypatch):
    """Every test starts UNARMED, whatever the shell that ran pytest looks like.

    tests/conftest.py pops the variable at import, which covers the process. This
    covers the test: a test that asserts the default refusal must not depend on
    another file having arranged it, and a test that arms the path must leave the
    next one unarmed. monkeypatch reverts, so the ordering cannot leak either way.
    """
    monkeypatch.delenv(SIGNING_SEED_ENV_VAR, raising=False)


class SeededLedger:
    """The rippled server AND the submit call, stood in for together.

    ONE fixture rather than two, and not only because six fixtures put this file over
    ruff's PLR0913 -- a suppression there would have been the lazy half of rule 19.
    They are one object: a ledger answers reads and accepts transactions, and every
    test here needs both halves because every assertion is a pair ("it refused" AND
    "nothing was submitted"). Two fixtures let a test take the first and forget the
    second, which is the shape where a guard looks tested and is not.

    `.answer(**by_method)` installs the seeded responses and returns the Recorder, so
    the call list is still available for the assertions that are about calls which
    must NOT have happened. `.submitted` is the list of Payments that reached
    submit_and_wait; an empty list is the assertion that nothing was signed.
    """

    def __init__(self, monkeypatch):
        self._monkeypatch = monkeypatch
        self.submitted: list = []
        self.recorder: Recorder | None = None

        def stub(transaction, client, wallet, **kwargs):
            self.submitted.append(transaction)
            return StubResponse(validated_success())

        monkeypatch.setattr(transaction_module, "submit_and_wait", stub)

    def answer(self, **by_method) -> Recorder:
        self.recorder = Recorder(**by_method)
        self._monkeypatch.setattr(requests, "post", self.recorder)
        return self.recorder

    @property
    def calls(self):
        """Every rippled call made, or [] when `answer` was never reached at all."""
        return self.recorder.calls if self.recorder is not None else []

    @property
    def methods(self):
        return self.recorder.methods if self.recorder is not None else []


@pytest.fixture
def ledger(monkeypatch):
    return SeededLedger(monkeypatch)


def armed_config(account: str) -> dict:
    return {**CONFIG_BASE, "XRP_DEPOSIT_ACCOUNT": account}


def all_log_text(caplog) -> str:
    """Every rendered record PLUS every raw argument, as one string.

    Both halves, and the second is the one that catches a leak: `logger.debug("x=%s",
    value)` leaves `value` in record.args whether or not the message is ever
    formatted, so a test that only read record.getMessage() would miss a value a
    handler would print. Same helper, same reasoning, as
    tests/test_secrets_are_not_logged.py and tests/test_grc_address_proof.py.
    """
    parts = []
    for record in caplog.records:
        parts.append(record.getMessage())
        parts.append(repr(record.args))
    return " ".join(parts)


# --- which of the two claimed blockers was real ------------------------------


def test_the_default_is_no_seed_and_therefore_no_payout_capability():
    """Unset is the default, and every surface derives its answer from that one fact.

    MUTATION: make signing_seed_is_present() return True unconditionally. This
    fails, and so does create_swap()'s refusal -- which is the point of deriving
    can_spend from it rather than writing False in two files.
    """
    assert signing_seed_is_present() is False
    assert signing_seed() == "", "an unset variable must read as empty, never as a plausible value"
    adapter = XRPAdapter(url=TESTNET_URL, min_confirmations=1)
    assert adapter.can_spend is False
    assert adapter.payout_refusal == missing_seed_refusal()
    # Rule 14: the refusal names BOTH variables, because naming one sends the reader
    # to do half the work.
    assert SIGNING_SEED_ENV_VAR in adapter.payout_refusal
    assert "XRP_DEPOSIT_ACCOUNT" in adapter.payout_refusal


def test_arming_the_old_call_site_alone_would_not_have_sent_anything(ledger):
    """THE FIRST OF THE TWO CLAIMED BLOCKERS WAS THE REAL ONE, established here.

    The customer page named two: "it holds no signing key, AND
    services/payout_service.py calls send_to_address() without the arming token".
    This test is the second clause fixed and the first left alone -- the exact state
    a change that wired only the call site would have produced. The token is passed,
    spelled correctly, and the send still refuses, because there was no environment
    variable, no Config field and no path of any kind by which a seed could arrive.

    So arming the call site alone would have converted XRPSendNotArmed's first
    branch ("not armed") into its second ("armed but no signing seed was supplied").
    A different message, the same stranded swap. chains/xrp_payout_seed.py is what
    was actually missing.

    ASSERTED ON AN EMPTY SUBMIT LIST, not on the exception: "it raised" does not
    establish that nothing was signed.
    """
    adapter = XRPAdapter(url=TESTNET_URL, min_confirmations=1)
    ledger.answer(server_info=server_info(), account_info=account_info())

    with pytest.raises(XRPSendNotArmed, match="no signing seed"):
        adapter.send_to_address(
            XRP_CUSTOMER_PAYOUT, 1, source=XRP_HOT_ACCOUNT, confirm_send=CONFIRM_XRP_SEND
        )

    assert ledger.submitted == [], "a seedless armed call must reach no signing library at all"


def test_the_adapter_cannot_reach_a_seed_even_when_the_variable_is_set(ledger, monkeypatch):
    """SETTING THE VARIABLE DOES NOT ARM THE ADAPTER. The structural property, behaviorally.

    chains/xrp.py imports signing_seed_is_present() and missing_seed_refusal() from
    chains/xrp_payout_seed.py and NOT signing_seed(), so the module can learn that a
    seed exists and cannot learn what it is. That is what keeps "this module holds no
    key" true in a tree where an environment variable now carries one.

    MUTATION, and it is the one this test exists for: add
    `from .xrp_payout_seed import signing_seed` to chains/xrp.py and fall back to it
    when the `seed` argument is empty. Every other test in this file still passes --
    the payout works, the guards fire, the suite is green -- and this one fails. A
    source-text check could not tell the difference, which is the behavioral
    verification principle: never accept "the code does not contain X" as evidence
    about what the code does.

    The seed here is never decoded: the refusal arrives at
    require_send_confirmation(), which compares an empty string, long before xrpl-py
    would see it.
    """
    monkeypatch.setenv(SIGNING_SEED_ENV_VAR, NOT_A_SEED)
    adapter = XRPAdapter(url=TESTNET_URL, min_confirmations=1)
    assert adapter.can_spend is True, "the variable IS set, so the capability must read as present"
    ledger.answer(server_info=server_info(), account_info=account_info())

    with pytest.raises(XRPSendNotArmed, match="no signing seed"):
        adapter.send_to_address(
            XRP_CUSTOMER_PAYOUT, 1, source=XRP_HOT_ACCOUNT, confirm_send=CONFIRM_XRP_SEND
        )

    assert ledger.submitted == [], "the adapter must not be able to find a seed for itself"


# --- the dispatch: which keywords each chain's send actually needs ------------


def test_every_other_chain_still_gets_exactly_two_positional_arguments():
    """BTC, LTC and GRC are unchanged, byte for byte, and that is deliberate.

    Their adapter calls `sendtoaddress` and the DAEMON picks the inputs and signs,
    so there is no source account to name and no seed to pass. Adding a keyword to
    the one function in this suite that moves money, for a chain that does not need
    it, would be a change with no behavioral gain.

    MUTATION: make broadcast_payout() pass source= unconditionally. This fails,
    naming the keyword that arrived.
    """
    adapter = RecordingAdapter()
    txid = broadcast_payout(adapter, "GRC", {}, GRC_PAYOUT, 55.5)

    assert txid == "stub-txid"
    assert adapter.calls == [((GRC_PAYOUT, 55.5), {})], (
        "a non-XRP send grew a keyword argument; the daemon chooses its own inputs"
    )


def test_an_xrp_send_gets_the_source_the_seed_and_the_arming_token(monkeypatch):
    """The three things XRP's send needs that no other chain's does.

    THE TOKEN IS COMPARED AGAINST THE IMPORTED CONSTANT rather than against a copy of
    its text: chains/xrp_signing.py matches for EXACT equality, so a literal that
    drifted by one character would refuse every payout, and `grep -rn
    CONFIRM_XRP_SEND` is meant to enumerate every site that can send XRP.

    NO DESTINATION TAG, asserted as an absence. `swaps` has deposit_tag and no
    payout_tag column; a tag on the way OUT belongs to the customer's exchange and
    this terminal never collects one. Passing a guessed one would misroute a payment
    inside their exchange with no way to recover it.

    MUTATION: drop confirm_send= from the call. This fails here and
    test_an_armed_xrp_payout_completes_the_swap_and_records_the_txid fails too -- one
    on the shape of the call and one on the outcome, which is the pair rule 13 asks
    for ("the assertion is the outcome, not the absence of an exception").
    """
    monkeypatch.setenv(SIGNING_SEED_ENV_VAR, NOT_A_SEED)
    adapter = RecordingAdapter()

    broadcast_payout(adapter, "XRP", armed_config(XRP_HOT_ACCOUNT), XRP_CUSTOMER_PAYOUT, 2.5)

    (args, kwargs) = adapter.calls[0]
    assert args == (XRP_CUSTOMER_PAYOUT, 2.5)
    assert kwargs["source"] == XRP_HOT_ACCOUNT, "the account the Payment DEBITS must be the configured one"
    assert kwargs["seed"] == NOT_A_SEED, "the seed must arrive from the environment, not from a literal"
    assert kwargs["confirm_send"] == CONFIRM_XRP_SEND
    assert "destination_tag" not in kwargs, (
        "a payout destination tag was passed; nothing in this terminal collects one, so any value "
        "here would be a guess at a field that routes money inside somebody else's exchange"
    )


def test_the_return_value_is_the_txid_and_nothing_else(monkeypatch):
    """The seed must not come back out. A return value is read, logged and stored.

    broadcast_payout() returns straight into _record_broadcast(), which writes it to
    `payouts.txid` and `swaps.payout_txid`. Anything else in that value would be
    written to the database by the next statement.
    """
    monkeypatch.setenv(SIGNING_SEED_ENV_VAR, NOT_A_SEED)
    returned = broadcast_payout(RecordingAdapter(), "XRP", armed_config(XRP_HOT_ACCOUNT), XRP_CUSTOMER_PAYOUT, 1)

    assert returned == "stub-txid"
    assert NOT_A_SEED not in returned


# --- the refusal order, cheapest and most fatal first ------------------------


def test_an_unset_payout_account_refuses_before_any_network_call(ledger, monkeypatch):
    """Two variables, and the second one is the one `can_spend` cannot see.

    XRPAdapter sets can_spend from the SEED alone -- it holds no Config and cannot be
    asked about XRP_DEPOSIT_ACCOUNT -- so this is the state a host reaches by
    exporting one of the two. create_swap() refuses it before a swap exists; this is
    the backstop for a variable removed between creation and payout.

    ASSERTED ON AN EMPTY RECORDER. A refusal that happened after two round trips to a
    rippled server would still raise, so the exception alone establishes nothing
    about the order. Rule 14: a host missing a variable should learn so immediately.
    """
    monkeypatch.setenv(SIGNING_SEED_ENV_VAR, NOT_A_SEED)
    recorder = ledger.answer(server_info=server_info(), account_info=account_info())
    adapter = XRPAdapter(url=TESTNET_URL, min_confirmations=1)

    with pytest.raises(ValueError, match="XRP_DEPOSIT_ACCOUNT"):
        broadcast_payout(adapter, "XRP", dict(CONFIG_BASE), XRP_CUSTOMER_PAYOUT, 1)

    assert recorder.calls == [], "the account check must precede every network call"
    assert ledger.submitted == []


def test_an_unset_seed_refuses_before_any_network_call(ledger):
    """The other half, and it is its own exception type for a reason.

    PayoutSigningUnavailable rather than a generic failure, matching
    PayoutUnlockUnavailable: no transaction was created, nothing reached any server,
    no fee was claimed, and the remedy is an environment variable rather than an
    investigation. process_pending_payouts() marks the swap `failed` either way, but
    an operator reading the reason needs to be able to tell them apart.
    """
    recorder = ledger.answer(server_info=server_info(), account_info=account_info())
    adapter = XRPAdapter(url=TESTNET_URL, min_confirmations=1)

    with pytest.raises(PayoutSigningUnavailable, match=SIGNING_SEED_ENV_VAR):
        broadcast_payout(adapter, "XRP", armed_config(XRP_HOT_ACCOUNT), XRP_CUSTOMER_PAYOUT, 1)

    assert recorder.calls == [], "the seed check must precede every network call"
    assert ledger.submitted == []


def test_a_mainnet_server_refuses_before_account_info_and_before_signing(ledger, monkeypatch):
    """The first network call is the mainnet question, and the URL is not consulted.

    Through the SERVICE path rather than only through the adapter, because that is
    the path a worker takes. The url here SAYS testnet and the server reports
    network_id 0, which is the combination a hostname check would pass: an
    /etc/hosts entry, a split-horizon resolver or a copied config can point
    "altnet.rippletest.net" at a mainnet validator while every log line still prints
    the testnet name.

    ASSERTED ON THE CALL LIST. `methods == ["server_info"]` establishes that the
    source account was never even read, so mainnet cannot be previewed against, let
    alone paid from.

    MUTATION: delete the require_non_mainnet() call in server_parameters(). This
    fails on the exception, and nothing else in this file does.
    """
    monkeypatch.setenv(SIGNING_SEED_ENV_VAR, NOT_A_SEED)
    recorder = ledger.answer(server_info=server_info(network_id=0), account_info=account_info())
    adapter = XRPAdapter(url=TESTNET_URL, min_confirmations=1)

    with pytest.raises(XRPMainnetRefused, match="MAINNET"):
        broadcast_payout(adapter, "XRP", armed_config(XRP_HOT_ACCOUNT), XRP_CUSTOMER_PAYOUT, 1)

    assert recorder.methods == ["server_info"], "account_info must not be read on a mainnet endpoint"
    assert ledger.submitted == [], "nothing may be signed for a mainnet ledger, by any route"


def test_a_seed_for_another_account_refuses_through_the_service_path(ledger, monkeypatch):
    """The derivation guard, reached the way the worker reaches it.

    Server-side `submit` sends the secret and `Account` separately so the server
    rejects a mismatch. Signing locally, WE choose the account the transaction
    claims -- so a seed paired with the wrong address signs a Payment debiting an
    account nobody announced. Here the two arrive from two DIFFERENT environment
    variables, which is the weakest coupling of all: the operator exports a seed and
    an account and nothing but this guard checks that they are the same wallet.

    AND THE MESSAGE NAMES ADDRESSES, NEVER THE SEED. A key-mismatch error is exactly
    where a secret leaks, because the debugging impulse on a mismatch is to print the
    key. Checked here for the seed AND for every seven-character run of it, since a
    truncated "sEd7...(redacted)" is still a leak a logger keeps forever.
    """
    paying = wallet_module.Wallet.create()
    announced = wallet_module.Wallet.create()
    monkeypatch.setenv(SIGNING_SEED_ENV_VAR, paying.seed)
    ledger.answer(server_info=server_info(), account_info=account_info(drops="1000000000"))
    adapter = XRPAdapter(url=TESTNET_URL, min_confirmations=1)

    with pytest.raises(XRPSigningRefused, match="REFUSING to sign") as caught:
        broadcast_payout(adapter, "XRP", armed_config(announced.classic_address), XRP_CUSTOMER_PAYOUT, 1)

    assert ledger.submitted == [], "a mismatched seed must not reach submit_and_wait"
    message = str(caught.value)
    assert paying.classic_address in message, "the operator needs to know which account the seed IS for"
    assert announced.classic_address in message
    assert paying.seed not in message
    for start in range(len(paying.seed) - 6):
        assert paying.seed[start : start + 7] not in message, "not even a fragment"


def test_a_payment_that_would_breach_the_reserve_refuses_before_signing(ledger, monkeypatch):
    """Refused here rather than by the ledger, which would have claimed a fee first.

    A tecUNFUNDED_PAYMENT or tecINSUFFICIENT_RESERVE arrives AFTER the transaction
    reached the ledger and paid for the privilege, and reads to an operator as an
    opaque four-letter code. The shortfall is named in drops instead.

    Seeded at 1,500,000 drops against a 1 XRP base reserve and a 1 XRP payment: the
    payment plus the fee allowance leaves less than the reserve.
    """
    paying = wallet_module.Wallet.create()
    monkeypatch.setenv(SIGNING_SEED_ENV_VAR, paying.seed)
    ledger.answer(server_info=server_info(), account_info=account_info(drops="1500000"))
    adapter = XRPAdapter(url=TESTNET_URL, min_confirmations=1)

    with pytest.raises(XRPReserveRefused, match="short by"):
        broadcast_payout(adapter, "XRP", armed_config(paying.classic_address), XRP_CUSTOMER_PAYOUT, 1)

    assert ledger.submitted == []


def test_the_payment_that_reaches_the_ledger_carries_no_partial_payment_bit(ledger, monkeypatch):
    """Asserted on the SERIALIZED transaction, through the service path.

    With tfPartialPayment set the ledger may deliver LESS than Amount and still
    return tesSUCCESS -- a payout that under-pays the customer while recording a real
    hash. "The code does not pass flags" is a claim about today's call site; this is a
    claim about the transaction that was signed.
    """
    paying = wallet_module.Wallet.create()
    monkeypatch.setenv(SIGNING_SEED_ENV_VAR, paying.seed)
    ledger.answer(server_info=server_info(), account_info=account_info(drops="1000000000"))
    adapter = XRPAdapter(url=TESTNET_URL, min_confirmations=1)

    txid = broadcast_payout(adapter, "XRP", armed_config(paying.classic_address), XRP_CUSTOMER_PAYOUT, 1.5)

    assert txid == "B" * 64
    assert len(ledger.submitted) == 1
    serialized = ledger.submitted[0].to_xrpl()
    assert serialized["Account"] == paying.classic_address, "the account DEBITED is the one configured"
    assert serialized["Destination"] == XRP_CUSTOMER_PAYOUT
    assert serialized["Amount"] == "1500000", "drops, as a string, exactly as the ledger expects"
    assert not int(serialized.get("Flags") or 0) & TF_PARTIAL_PAYMENT
    assert "DestinationTag" not in serialized


# --- the seed reaches no log, no exception, no column, no stream --------------


def _seed_one_pending_xrp_swap(db_path: str, payout_address: str, swap_id: str = "s_xrp") -> None:
    """One GRC -> XRP swap in `payout_pending`, the state the worker acts on.

    `apply_migrations` is run because payout_worker.py runs it once at startup, and
    idx_payouts_one_live_per_swap is what makes a second live payout row impossible.
    A test that skipped it would be measuring a database the worker never sees.
    """
    conn = connect_db(db_path)
    conn.executescript(SCHEMA)
    now = "2026-10-02T00:00:00+00:00"
    conn.execute(
        "INSERT INTO quotes (id, from_asset, to_asset, input_amount, quoted_rate, fee_bps, "
        "network_fee_reserve, output_amount_estimate, expires_at, created_at) "
        "VALUES ('q_xrp', 'GRC', 'XRP', 100.0, 0.02, 150, 0.0, 1.97, ?, ?)",
        (now, now),
    )
    conn.execute(
        """
        INSERT INTO swaps (
            id, quote_id, from_asset, to_asset, deposit_address, payout_address,
            expected_input_amount, actual_input_amount, quoted_rate, fee_bps,
            network_fee_reserve, output_amount_estimate, status, min_confirmations,
            deposit_txid, payout_txid, created_at, updated_at, credited_at,
            completed_at, expires_at, failed_reason
        ) VALUES (?, 'q_xrp', 'GRC', 'XRP', ?, ?,
                  100.0, 100.0, 0.02, 150, 0.0, 1.97, 'payout_pending', 6,
                  'grc_txid', NULL, ?, ?, ?, NULL, ?, NULL)
        """,
        (swap_id, GRC_PAYOUT, payout_address, now, now, now, now),
    )
    conn.commit()
    apply_migrations(conn)
    conn.close()


def _whole_database(path: str) -> str:
    """Every value in every row of every table, as one string.

    Every table rather than the columns a reader expects, because the claim is that
    NO column anywhere holds the seed -- which is the same reason
    tests/test_grc_address_proof.py searches all of address_proof_challenges rather
    than the two fields it would guess.
    """
    conn = sqlite3.connect(path)
    conn.row_factory = dict_factory
    tables = [row["name"] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'"
    ).fetchall()]
    values = []
    for table in tables:
        for row in conn.execute(f"SELECT * FROM {table}").fetchall():  # noqa: S608 -- checked: `table` is a name read from THIS temp database's own sqlite_master, not input. Values would be parameters; an identifier cannot be.
            values.extend(str(value) for value in row.values())
    conn.close()
    return " ".join(values)


def test_the_seed_reaches_no_log_record_no_database_column_and_no_stream(tmp_path, ledger, monkeypatch, caplog, capsys):
    """THE WHOLE ARMED PAYOUT RUNS and the seed is nowhere a human or a log can see it.

    FOUR PLACES, and every one of them has leaked a secret in some codebase:

      log records     captured at DEBUG, the most verbose level, and searched in BOTH
                      record.getMessage() and repr(record.args) -- a value passed as
                      a lazy format argument survives in `args` whether or not any
                      handler ever formats the message, which is the half a naive
                      test misses
      the database    every value of every row of every table in the real schema,
                      because the claim is that no column anywhere holds it
      stdout/stderr   chains/xrp.py prints a seven-line preview and three progress
                      lines (rule 14), and the preview is exactly the kind of block
                      an operator pastes back into a chat
      the return      checked by test_the_return_value_is_the_txid_and_nothing_else

    A TRUNCATION AND A HASH ARE ALSO CHECKED, in the database and in the log. An XRPL
    family seed is 128 bits of entropy with known structure, so its hash is a target
    and a prefix narrows the search space -- storing either would be storing the seed
    with an extra step. Same argument as
    tests/test_grc_address_proof.py::test_a_pasted_private_key_is_in_neither_the_database_nor_the_log.

    THE PAYOUT SUCCEEDS HERE, deliberately. A refusal path prints less, so a test
    that only exercised one would be the weaker measurement; this one goes all the
    way through signing and recording.
    """
    paying = wallet_module.Wallet.create()
    seed = paying.seed
    monkeypatch.setenv(SIGNING_SEED_ENV_VAR, seed)
    ledger.answer(server_info=server_info(), account_info=account_info(drops="1000000000"))

    db_path = str(tmp_path / "xrp_payout_leak.db")
    _seed_one_pending_xrp_swap(db_path, XRP_CUSTOMER_PAYOUT)
    adapters = {"XRP": XRPAdapter(url=TESTNET_URL, min_confirmations=1)}

    with caplog.at_level(logging.DEBUG):
        conn = connect_db(db_path)
        try:
            completed = process_pending_payouts(conn, armed_config(paying.classic_address), adapters)
        finally:
            conn.close()

    assert len(completed) == 1, "the payout must actually have happened, or this measures a quiet path"
    assert len(ledger.submitted) == 1

    for where, text in (
        ("a log record", all_log_text(caplog)),
        ("the database", _whole_database(db_path)),
        ("stdout", capsys.readouterr().out),
    ):
        assert seed not in text, f"the signing seed appeared in {where}"
        assert seed[:16] not in text, f"a 16-character prefix of the seed appeared in {where}"
        assert hashlib.sha256(seed.encode()).hexdigest() not in text, (
            f"a hash of the seed appeared in {where}; hashing 128 bits of known structure is not redaction"
        )


def test_no_refusal_message_on_this_path_carries_the_seed(ledger, monkeypatch):
    """Every exception the armed path can raise, checked for the seed.

    An error message is where a secret leaks, because the debugging impulse on a
    failure is to print the inputs. Each refusal below is produced with a REAL seed in
    the environment and the message is searched, so this is not an argument about
    which messages happen to interpolate what -- it is the set of strings the path
    can actually produce.
    """
    paying = wallet_module.Wallet.create()
    other = wallet_module.Wallet.create()
    monkeypatch.setenv(SIGNING_SEED_ENV_VAR, paying.seed)
    adapter = XRPAdapter(url=TESTNET_URL, min_confirmations=1)
    messages = []

    # mainnet
    ledger.answer(server_info=server_info(network_id=0), account_info=account_info())
    with pytest.raises(XRPMainnetRefused) as caught:
        broadcast_payout(adapter, "XRP", armed_config(paying.classic_address), XRP_CUSTOMER_PAYOUT, 1)
    messages.append(str(caught.value))

    # the reserve
    ledger.answer(server_info=server_info(), account_info=account_info(drops="1500000"))
    with pytest.raises(XRPReserveRefused) as caught:
        broadcast_payout(adapter, "XRP", armed_config(paying.classic_address), XRP_CUSTOMER_PAYOUT, 1)
    messages.append(str(caught.value))

    # a seed for another account
    ledger.answer(server_info=server_info(), account_info=account_info(drops="1000000000"))
    with pytest.raises(XRPSigningRefused) as caught:
        broadcast_payout(adapter, "XRP", armed_config(other.classic_address), XRP_CUSTOMER_PAYOUT, 1)
    messages.append(str(caught.value))

    # a bad destination, refused locally before anything is read
    with pytest.raises(XRPRPCError) as caught:
        broadcast_payout(adapter, "XRP", armed_config(paying.classic_address), "not-an-address", 1)
    messages.append(str(caught.value))

    # an unset account, with the seed present
    with pytest.raises(ValueError) as caught:
        broadcast_payout(adapter, "XRP", dict(CONFIG_BASE), XRP_CUSTOMER_PAYOUT, 1)
    messages.append(str(caught.value))

    assert len(messages) == 5, "every refusal on this path must be exercised, or the set is not the set"
    for message in messages:
        assert paying.seed not in message
        assert paying.seed[:16] not in message
        assert hashlib.sha256(paying.seed.encode()).hexdigest() not in message
    assert ledger.submitted == [], "not one of these refusals may have signed anything"


# --- end to end through the worker, and the call-site mutation ---------------


def test_an_armed_xrp_payout_completes_the_swap_and_records_the_txid(tmp_path, ledger, monkeypatch):
    """The outcome, asserted on rows rather than on a return value (rule 13).

    A payout that "did not raise" is not a payout. The assertion is the state the
    worker left behind: the payouts row is `broadcast` with the hash, the swap is
    `completed` with the same hash in payout_txid, and exactly one Payment reached
    submit_and_wait.
    """
    paying = wallet_module.Wallet.create()
    monkeypatch.setenv(SIGNING_SEED_ENV_VAR, paying.seed)
    ledger.answer(server_info=server_info(), account_info=account_info(drops="1000000000"))

    db_path = str(tmp_path / "xrp_payout_ok.db")
    _seed_one_pending_xrp_swap(db_path, XRP_CUSTOMER_PAYOUT)
    adapters = {"XRP": XRPAdapter(url=TESTNET_URL, min_confirmations=1)}

    conn = connect_db(db_path)
    try:
        process_pending_payouts(conn, armed_config(paying.classic_address), adapters)
        swap = conn.execute("SELECT * FROM swaps WHERE id = 's_xrp'").fetchone()
        payout = conn.execute("SELECT * FROM payouts WHERE swap_id = 's_xrp'").fetchone()
    finally:
        conn.close()

    assert swap["status"] == "completed"
    assert swap["payout_txid"] == "B" * 64
    assert payout["status"] == "broadcast"
    assert payout["txid"] == "B" * 64
    assert payout["destination_address"] == XRP_CUSTOMER_PAYOUT
    assert len(ledger.submitted) == 1


def test_reverting_the_call_site_to_two_positional_arguments_fails_the_payout(tmp_path, ledger, monkeypatch):
    """MUTATION: put the old call site back, and the swap strands exactly as it used to.

    This is the mutation check for the wiring itself, and it is the one that matters
    most: a correct broadcast_payout() whose caller ignored it would look exactly like
    a working change. So the mutation is applied to the thing
    process_pending_payouts() actually calls -- the name it resolves at call time --
    and the assertion is the stranded swap, not an exception.

    WHAT IT REPRODUCES is the failure the customer page described: the deposit is
    already credited, the payout refuses permanently, and the swap lands in `failed`
    needing a person. Everything else in this file is armed and green while this
    mutation is in place, which is what makes the real call site load-bearing rather
    than decorative.
    """
    paying = wallet_module.Wallet.create()
    monkeypatch.setenv(SIGNING_SEED_ENV_VAR, paying.seed)
    ledger.answer(server_info=server_info(), account_info=account_info(drops="1000000000"))

    db_path = str(tmp_path / "xrp_payout_reverted.db")
    _seed_one_pending_xrp_swap(db_path, XRP_CUSTOMER_PAYOUT)
    adapters = {"XRP": XRPAdapter(url=TESTNET_URL, min_confirmations=1)}

    import services.payout_service as payout_module  # noqa: PLC0415 -- checked: imported here, not at the top, because the point is to rebind ONE name on the module object for the duration of this test and monkeypatch reverts it. A top-level import would leave the module in scope for every other test in this file.

    monkeypatch.setattr(
        payout_module,
        "broadcast_payout",
        lambda adapter, asset, config, address, amount: adapter.send_to_address(address, amount),
    )

    conn = connect_db(db_path)
    try:
        completed = process_pending_payouts(conn, armed_config(paying.classic_address), adapters)
        swap = conn.execute("SELECT * FROM swaps WHERE id = 's_xrp'").fetchone()
        payout = conn.execute("SELECT * FROM payouts WHERE swap_id = 's_xrp'").fetchone()
    finally:
        conn.close()

    assert completed == [], "the reverted call site must complete nothing"
    assert swap["status"] == "failed"
    assert swap["payout_txid"] is None
    # WHICH refusal it dies on, and it is NOT the arming token -- which this assertion
    # first expected and which the run corrected. The two-positional call passes no
    # source, and preview_payout() checks the source LOCALLY, before it reads the
    # server and long before require_send_confirmation(). So the old call site fails
    # one guard EARLIER than the customer page's sentence described: it never gets as
    # far as being unarmed.
    #
    # Recorded rather than smoothed over, because the distinction is the refusal ORDER
    # this file is about, and because an assertion written from the expected message
    # instead of the measured one is how a test comes to pin a sentence nobody
    # produced.
    assert "needs the SOURCE account" in (swap["failed_reason"] or ""), (
        f"the reverted call site should die on the missing source; it was {swap['failed_reason']!r}"
    )
    assert payout["status"] == "failed"
    assert ledger.submitted == [], "nothing may have been signed"


# --- the hole the wiring would have opened, closed before the swap exists -----


class PayableGRC:
    """A GRC adapter that can do everything create_swap() asks of a SOURCE chain.

    A stub rather than a real adapter because create_swap() calls get_new_address()
    on the deposit side, which on a real one is an RPC. Nothing here is the subject
    of this test: the subject is the XRP leg, and a source chain that refused would
    make the test pass for the wrong reason.
    """

    asset, can_spend = "GRC", True

    def get_new_address(self, _label):
        return GRC_PAYOUT

    def validate_address(self, _address):
        return True

    def describe_address(self, _address):
        return "a stub GRC address"


def _quote_row(db_path: str, quote_id: str = "q_gate") -> None:
    conn = connect_db(db_path)
    conn.executescript(SCHEMA)
    now = "2026-10-02T00:00:00+00:00"
    later = "2126-10-02T00:00:00+00:00"
    conn.execute(
        "INSERT INTO quotes (id, from_asset, to_asset, input_amount, quoted_rate, fee_bps, "
        "network_fee_reserve, output_amount_estimate, expires_at, created_at) "
        "VALUES (?, 'GRC', 'XRP', 100.0, 0.02, 150, 0.0, 1.97, ?, ?)",
        (quote_id, later, now),
    )
    conn.commit()
    apply_migrations(conn)
    conn.close()


def test_a_swap_is_refused_when_the_seed_is_set_but_the_payout_account_is_not(tmp_path, monkeypatch):
    """THE HOLE THE WIRING WOULD HAVE OPENED, and it is closed before anything is taken.

    TWO VARIABLES ARM AN XRP PAYOUT and `can_spend` can only see one of them.
    XRPAdapter holds no Config -- deliberately, because an adapter that held a
    hot-wallet account would be one configuration value away from being a payout path
    -- so it answers "is XRP_PAYOUT_SECRET_SEED set" and cannot answer "is
    XRP_DEPOSIT_ACCOUNT set".

    SO THIS IS THE REACHABLE HALF-CONFIGURED STATE: seed exported, account not. Before
    2026-10-02 it did not exist, because no seed could arrive at all. With the wiring
    and without this gate, chains/registry.why_cannot_pay_out() would return "" and
    the swap would be CREATED -- then the customer's GRC would be deposited, credited,
    and the payout would refuse at broadcast_payout() with the deposit already taken.
    That is the precise failure the whole GRC -> XRP paragraph in chains/xrp.py is
    about, arriving through the one door the adapter cannot see.

    MUTATION: delete the `payout_source_account()` try/except from create_swap(). This
    test fails and NOTHING ELSE IN THE SUITE DOES -- the payout tests all configure
    both variables, so the hole is invisible to every one of them. That is why this
    test exists at the creation stage rather than at the send.

    ASSERTED ON THE ROWS, not only on the exception: "nothing was written" is the
    property that makes a refusal here cost a retry instead of a customer's deposit.
    """
    monkeypatch.setenv(SIGNING_SEED_ENV_VAR, NOT_A_SEED)
    db_path = str(tmp_path / "xrp_create_gate.db")
    _quote_row(db_path)
    adapters = {"GRC": PayableGRC(), "XRP": XRPAdapter(url=TESTNET_URL, min_confirmations=1)}

    conn = connect_db(db_path)
    try:
        # The seed alone is NOT enough, which is the whole assertion.
        with pytest.raises(ValueError, match="XRP_DEPOSIT_ACCOUNT"):
            create_swap(conn, dict(CONFIG_BASE), adapters, "q_gate", XRP_CUSTOMER_PAYOUT)
        swaps = conn.execute("SELECT * FROM swaps").fetchall()
    finally:
        conn.close()

    assert swaps == [], "a refused swap must leave no row; a row is a deposit instruction"


def test_a_swap_is_created_once_both_variables_are_set(tmp_path, monkeypatch):
    """The other direction, because a gate that refuses in both states is not a gate.

    This is the half that would regress silently. If `payout_source_account()` ever
    raised for a correctly configured host -- a stricter address check, a renamed
    variable -- every XRP swap would refuse at creation and the only symptom would be
    customers being told a working pair is unavailable. The refusal test above would
    still pass.

    MUTATION: make payout_source_account() raise unconditionally. This fails; the
    test above does not.
    """
    monkeypatch.setenv(SIGNING_SEED_ENV_VAR, NOT_A_SEED)
    db_path = str(tmp_path / "xrp_create_ok.db")
    _quote_row(db_path)
    adapters = {"GRC": PayableGRC(), "XRP": XRPAdapter(url=TESTNET_URL, min_confirmations=1)}

    conn = connect_db(db_path)
    try:
        swap = create_swap(conn, armed_config(XRP_HOT_ACCOUNT), adapters, "q_gate", XRP_CUSTOMER_PAYOUT)
        stored = conn.execute("SELECT * FROM swaps WHERE id = ?", (swap["id"],)).fetchone()
    finally:
        conn.close()

    assert stored["to_asset"] == "XRP"
    assert stored["payout_address"] == XRP_CUSTOMER_PAYOUT
    assert stored["status"] == "awaiting_deposit"


# --- presence is not validity, and the banner had no way to say so ------------


def test_the_banner_says_the_seed_DOES_NOT_DECODE_when_it_does_not(monkeypatch):
    """ARMED printed on a nine-character placeholder, measured 2026-10-03.

    XRP_PAYOUT_SECRET_SEED was SET, so signing_seed_is_present() returned True, so
    can_spend was True, so chains/registry.why_cannot_pay_out() returned "" and
    this terminal considered XRP a payout destination it could serve. The value was
    nine characters and did not start with 's'. chains/xrp_signing.py:397 derives
    the payout wallet with the same Wallet.from_seed(), so every XRP payout would
    have failed at signing -- after the deposit was confirmed and irreversible.

    The old line said "NOT a claim the seed is correct", which was true and left
    the operator unable to tell this state from a working one. Decoding is offline
    and costs nothing, so the banner states it.
    """
    monkeypatch.setenv("XRP_PAYOUT_SECRET_SEED", "placeholdr")
    adapter = XRPAdapter(url="https://s.altnet.rippletest.net:51234")

    line = adapter.endpoint_line()

    assert "ARMED" in line, "can_spend is unchanged: presence still arms it, and the line still says so"
    assert "DOES NOT DECODE" in line
    assert "still OFFERED" in line, (
        "the consequence is the content: a pair this terminal will sell and cannot pay. A line that "
        "reported the bad seed without saying it is still on the menu understates it"
    )
    assert "placeholdr" not in line, "a seed is a key and must never reach a banner"


def test_the_banner_says_the_seed_DECODES_when_it_does(monkeypatch):
    """The other half, or a version that always warned would pass the test above.

    The seed funds nothing and controls an account that has never existed.
    """
    monkeypatch.setenv("XRP_PAYOUT_SECRET_SEED", "sEdTM1uX8pu2do5XvTnutH6HsouMaM2")
    adapter = XRPAdapter(url="https://s.altnet.rippletest.net:51234")

    line = adapter.endpoint_line()

    assert "The seed DECODES" in line
    assert "DOES NOT DECODE" not in line
    assert "RIGHT account" in line, (
        "decoding is not ownership: derive_and_check() still refuses a seed paired with an account it "
        "does not control, and the line must not overclaim"
    )
    assert "sEdTM1uX8pu2do5XvTnutH6HsouMaM2" not in line
