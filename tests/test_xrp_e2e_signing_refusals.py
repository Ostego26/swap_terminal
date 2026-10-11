#!/usr/bin/env python3
"""The three XRP refusals that were unnamed, unreadable, or not asked at all.

Role: tests (seeded rippled transport; no socket, nothing signed, nothing submitted)
Reads: chains/xrp_signing.py, chains/xrp_testnet.py, chains/xrp.py,
      services/payout_service.py, services/payout_rescue.py, db.py's real SCHEMA
Writes: one throwaway sqlite file per test, under pytest's tmp_path
Can move funds: no. requests.post and xrpl.transaction.submit_and_wait are both
      replaced, so the real adapter and the real payout service run with nothing
      reachable. Every address here is derived in tests/valid_addresses.py or by
      xrpl.wallet.Wallet.create(), and no account named here has ever been funded.
Mainnet-safe: yes -- no test in this file opens a socket.

Each test below fails when ONE named mechanism is removed, which is the shape
tests/test_xrp_adapter.py's header asks for: a guard with no test that fails when
it is DISABLED is not a guard.

=============================================================================
WHAT WAS MEASURED, 2026-10-11, AND WHAT EACH FIX CHANGED
=============================================================================

1. A SEED THAT DECODED AT CONSTRUCTION AND NOT AT SEND TIME LEFT A CREDITED
   SWAP SAYING "Invalid character 'O'".

   Driven end to end through services/payout_service.process_pending_payouts()
   against the real schema: a GRC -> XRP swap in `payout_pending` with
   actual_input_amount already set, an XRPAdapter built while
   XRP_PAYOUT_SECRET_SEED decoded (so can_spend was True and the swap was
   payable), and the variable then re-exported with a nine-character placeholder
   WITHOUT restarting the process -- the state chains/xrp.endpoint_line()'s own
   ARMED branch already warns about in so many words.

       submits reaching the ledger                             0
       swaps.status                                            failed
       swaps.failed_reason                       "Invalid character 'O'"
       payouts row                               status failed, txid NULL
       payout_rescue.refused_before_signing()                   ''

   Nothing was signed -- xrpl.wallet.Wallet.from_seed() raises before any Payment
   object exists -- and the recorded reason said none of that. It named no chain,
   no variable, no account and no operation; it carried one character of the
   rejected key into a database column and a log file; and it matched no entry in
   services/payout_rescue.PRE_SIGNING_MARKERS, so the ONE XRP failure that is
   provably pre-signing could not be handed back to the payout worker by
   rescue_payout.py. Deposit credited, payout refused, no tool able to move it.

   chains/xrp_signing.derive_and_check() now raises XRPSigningRefused, which is
   what that module's exception docstring promises of every guard in it.

2. chains/xrp_testnet.refuse_mainnet() PASSED A MISSING network_id.

   It tested `network_id in MAINNET_NETWORK_IDS` -- a membership check on a
   frozenset of one integer -- so `None in frozenset({0})` was False and an
   answer that did not name the network at all was reported as a safe one, in the
   file whose own header calls this "the check every XRP script runs before
   submitting". A STRING "0" passed the same way. chains/xrp_signing.
   require_non_mainnet() has refused all three since it was written, with the
   argument in its docstring: "not reading the network is not the same as reading
   a safe one". Two copies of one rule, agreeing about which id is mainnet and
   disagreeing about every answer that is not an id (rule 8). It derives from the
   survivor now.

3. XRP's unattributable rows recorded confirmations 0 FOR A VALIDATED PAYMENT.

   Measured through the real services against the real schema: an untagged
   validated Payment of 2.5 XRP wrote

       amount 2.5   confirmations 0   discriminator NULL

   while the row one case over -- a tagged payment no swap owns, written by
   unclaimed_rows() rather than stranded_rows() -- wrote confirmations 1 off the
   same ledger. Zero is services/unattributable_deposit_service.stranded_rows()'
   default and is correct for Solana, whose adapter genuinely does not carry the
   rank. On XRP it is known by construction, and the adapter now publishes it.

   THE CONSUMING ARGUMENT IS NOT IN THIS TREE YET. services/deposit_service.py is
   out of scope for the change that added the attribute, so the test below asserts
   what this file OWNS -- that the value is published, that it is the validated
   rank taken from chains/xrp_units.py, and that every element of the list it
   describes really is from a validated ledger. The one-argument patch at
   record_what_nobody_can_claim()'s call site is in the report; applied in memory
   it moved the row from 0 to 1 against this same seeded payment.
"""

from __future__ import annotations

import hashlib
import json

import pytest
from chains import xrp as xrp_module
from chains import xrp_testnet as xrp_testnet_module
from chains.xrp import XRPAdapter, XRPRPCError
from chains.xrp_signing import (
    XRPMainnetRefused,
    XRPSigningRefused,
    derive_and_check,
)
from chains.xrp_units import LEDGER_VALIDATED, MAX_LEDGER_RANK
from db import SCHEMA, apply_migrations, connect_db
from services.payout_rescue import refused_before_signing
from services.payout_service import process_pending_payouts
from valid_addresses import GRC_PAYOUT, XRP_CUSTOMER_PAYOUT, XRP_HOT_ACCOUNT
from xrp_seeded_transport import Recorder, account_info, server_info

# xrpl-py is an OPTIONAL dependency and these tests need the real decoder: the
# subject of test 1 is what xrpl.wallet.Wallet.from_seed() does with a value that
# is not a seed, which a stub cannot stand in for. importorskip rather than a
# try/except at module scope, for the reason tests/test_xrp_payout_wiring.py
# records: a bare import runs before any skip could take effect and breaks
# collection on a host without the library.
wallet_module = pytest.importorskip(
    "xrpl.wallet", reason="xrpl-py absent, so the real seed decoder is UNCHECKED"
)
transaction_module = pytest.importorskip(
    "xrpl.transaction", reason="xrpl-py absent, so the signed-and-submitted path is UNCHECKED"
)

SIGNING_SEED_ENV_VAR = "XRP_PAYOUT_SECRET_SEED"
SEEDED_URL = "https://seeded.invalid:51234/"

# A NINE-CHARACTER PLACEHOLDER, WHICH IS THE LENGTH MEASURED ON THE OPERATOR'S HOST
# ON 2026-10-03 (chains/xrp_payout_seed.signing_seed_decodes() carries that
# measurement). It is a literal rather than a derived value because the subject is
# a value that does NOT decode, and the whole point is that nothing can sign with
# it: Wallet.from_seed() raises ValueError on it, which this file asserts directly
# rather than assuming.
NOT_A_SEED = "sPLACEHOL"

# A SECOND NON-SEED, SEED-SHAPED AND LONG, so the refusal is not resting on the
# first one's length. Derived from a hash of a phrase written in this file, the same
# construction tests/valid_addresses.py uses, so anybody reading this line can
# reproduce it -- which is the definition of not secret.
SEED_SHAPED_BUT_NOT_A_SEED = "sNotASeed" + hashlib.sha256(
    b"swap_terminal xrp e2e: a seed-shaped string that is not a seed"
).hexdigest()[:22]


@pytest.fixture(autouse=True)
def _restore_the_real_transport():
    """Put chains.xrp.requests.post back, whatever a test replaced it with.

    THE HELPERS BELOW ASSIGN IT DIRECTLY RATHER THAN THROUGH monkeypatch, because
    several of them are called from tests that build more than one adapter and
    each needs its own seeded entry list -- monkeypatch.setattr would need the
    fixture threaded through every signature for no gain. What a direct assignment
    does NOT do is revert, and a stand-in left installed is a stand-in the NEXT
    file in the session inherits: tests/test_xrp_adapter.py and
    tests/test_xrp_unattributable_deposits.py both patch the same name, and a
    responder answering their requests with THIS file's entries would be a
    cross-file failure whose cause is in a file the failure does not name.
    `-p no:randomly` is not in this suite's addopts, so file order is not even
    stable enough to make that reproducible.

    So the revert is a fixture rather than a discipline. It runs for every test
    here whether or not that test replaced anything, which is why it reads the
    original once and writes it back unconditionally.
    """
    original = xrp_module.requests.post
    try:
        yield
    finally:
        xrp_module.requests.post = original


@pytest.fixture(autouse=True)
def _no_ambient_seed(monkeypatch):
    """Every test starts UNARMED, whatever shell ran pytest.

    tests/conftest.py pops the variable at import, which covers the process; this
    covers the test, and monkeypatch reverts so no ordering can leak either way.
    The same fixture, for the same reason, as tests/test_xrp_payout_wiring.py.
    """
    monkeypatch.delenv(SIGNING_SEED_ENV_VAR, raising=False)


# --- 1. a seed that does not decode is a NAMED refusal ----------------------


def test_a_value_that_is_not_a_seed_is_refused_by_name_and_not_by_ValueError():
    """The decode failure arrives as the module's own guard type.

    FAILS WITHOUT THE FIX: derive_and_check() called Wallet.from_seed() bare, so
    the raise was xrpl-py's ValueError. XRPSigningRefused is a RuntimeError and
    ValueError is not, so `pytest.raises(XRPSigningRefused)` does not catch it and
    this test errors with the ValueError instead.

    BOTH NON-SEEDS, because a refusal that only recognizes a short string would
    pass with a length check in place of a decode.
    """
    for value in (NOT_A_SEED, SEED_SHAPED_BUT_NOT_A_SEED, ""):
        with pytest.raises(XRPSigningRefused) as refusal:
            derive_and_check(value, XRP_HOT_ACCOUNT)
        message = str(refusal.value)
        assert "does NOT decode" in message, (
            f"the refusal for {value!r} does not say the value failed to decode, so an operator "
            f"cannot tell it from the mismatch refusal one branch down: {message!r}"
        )


def test_the_refusal_says_nothing_was_signed_and_names_the_variable():
    """Rule 14: echo the parameter that decides the answer, and say what happened.

    FAILS WITHOUT THE FIX: the recorded reason was "Invalid character 'O'", which
    contains neither phrase.

    THE PHRASE "nothing was submitted and no fee was claimed" IS LOAD-BEARING and
    not decoration. services/payout_rescue.PRE_SIGNING_MARKERS is matched against
    swaps.failed_reason as a case-insensitive substring, and it is the sentence
    services/payout_service.PayoutSigningUnavailable already uses for the same
    fact -- so the one-line marker addition proposed alongside this change makes
    rescue_payout.py able to re-drive this swap, and a reworded message here would
    silently take that away again.
    """
    with pytest.raises(XRPSigningRefused) as refusal:
        derive_and_check(NOT_A_SEED, XRP_HOT_ACCOUNT)
    message = str(refusal.value)
    assert SIGNING_SEED_ENV_VAR in message
    assert "nothing was submitted and no fee was claimed" in message
    assert XRP_HOT_ACCOUNT in message, "the announced account is public and is what the operator needs"


def test_no_part_of_the_rejected_value_reaches_the_refusal():
    """A key never appears in a message, not even one character of it.

    FAILS WITHOUT THE FIX: xrpl-py's own ValueError is `Invalid character 'O'` --
    measured on xrpl-py 5.2.0 against four different non-seeds -- and
    services/payout_service.py writes str(exc) into swaps.failed_reason and logs
    it. One character of a 29-character seed is not a meaningful disclosure and it
    is still a character of a key in a database column, which
    chains/xrp_payout_seed.derived_payout_account() already excludes str(error)
    to avoid and says so.

    ASSERTED PER CHARACTER rather than on the whole string, because the whole
    string was never the leak: the test that looked for it would have passed
    before the fix.
    """
    with pytest.raises(XRPSigningRefused) as refusal:
        derive_and_check(NOT_A_SEED, XRP_HOT_ACCOUNT)
    message = str(refusal.value)
    assert NOT_A_SEED not in message
    # The characters unique to the placeholder, so the assertion is about the VALUE
    # and not about letters that any English sentence contains. "O" and "L" are
    # upper-case and appear nowhere else in the refusal's prose; `set` so the
    # failure names which one got through.
    leaked = {character for character in "OL" if f"'{character}'" in message or f'"{character}"' in message}
    assert not leaked, (
        f"the refusal quotes {sorted(leaked)} from the rejected value, which is how xrpl-py's own "
        f"message leaked a character of the key into swaps.failed_reason"
    )


def test_a_seed_for_another_account_is_still_the_OTHER_refusal():
    """The decode guard must not swallow the mismatch guard it sits above.

    Two distinct states, two distinct sentences -- which is the whole argument in
    XRPSigningRefused's docstring for having subclasses at all. A seed that
    DECODES and derives the wrong account still gets the mismatch message, and
    this is what fails if the new try block were widened to wrap the comparison
    too.
    """
    paying = wallet_module.Wallet.create()
    with pytest.raises(XRPSigningRefused) as refusal:
        derive_and_check(paying.seed, XRP_CUSTOMER_PAYOUT)
    message = str(refusal.value)
    assert "does NOT decode" not in message, "a seed that decodes must not be reported as one that did not"
    assert paying.classic_address in message
    assert XRP_CUSTOMER_PAYOUT in message
    assert paying.seed not in message


def test_a_seed_that_decodes_and_matches_still_returns_its_wallet():
    """The happy path, so the guard cannot be passing by refusing everything."""
    paying = wallet_module.Wallet.create()
    wallet = derive_and_check(paying.seed, paying.classic_address)
    assert wallet.classic_address == paying.classic_address


def test_a_credited_swap_records_a_reason_an_operator_can_act_on(tmp_path, monkeypatch):
    """The whole defect, end to end, on rows rather than on an exception.

    The adapter is built while the seed DECODES -- which is what makes can_spend
    True and the swap payable -- and the environment is then changed without a
    restart. The assertion is what services/payout_service.py left in the
    database for a swap whose customer has already paid.

    FAILS WITHOUT THE FIX: swaps.failed_reason is "Invalid character 'O'", so
    every `in` assertion below is False.

    AND IT ASSERTS THE ZERO SUBMITS, because "the reason improved" would be worth
    nothing if the payment had gone out. Wallet.from_seed() raises before any
    Payment object exists, and the empty list is how that is established rather
    than argued.
    """
    paying = wallet_module.Wallet.create()
    monkeypatch.setenv(SIGNING_SEED_ENV_VAR, paying.seed)

    recorder = Recorder(server_info=server_info(), account_info=account_info(drops="1000000000"))
    monkeypatch.setattr(xrp_module.requests, "post", recorder)
    submitted: list = []
    monkeypatch.setattr(
        transaction_module,
        "submit_and_wait",
        lambda transaction, client, wallet, **kwargs: submitted.append(transaction),
    )
    adapter = XRPAdapter(url=SEEDED_URL, min_confirmations=1)
    assert adapter.can_spend, "the premise is an ARMED adapter; without it this measures the wrong gate"

    db_path = str(tmp_path / "xrp_seed_changed.db")
    _seed_one_pending_xrp_swap(db_path)

    # The operator re-exports the variable with a placeholder. The process is not
    # restarted, so the adapter keeps offering XRP -- which is exactly what
    # chains/xrp.endpoint_line()'s ARMED branch warns will happen.
    monkeypatch.setenv(SIGNING_SEED_ENV_VAR, NOT_A_SEED)
    conn = connect_db(db_path, create=True)
    try:
        process_pending_payouts(
            conn,
            {"XRP_MIN_CONFIRMATIONS": 1, "GRC_MIN_CONFIRMATIONS": 6, "AMOUNT_TOLERANCE_PCT": 0.01,
             "XRP_DEPOSIT_ACCOUNT": paying.classic_address},
            {"XRP": adapter},
        )
        swap = conn.execute("SELECT * FROM swaps WHERE id = 's_xrp_e2e'").fetchone()
        payouts = conn.execute("SELECT * FROM payouts WHERE swap_id = 's_xrp_e2e'").fetchall()
    finally:
        conn.close()

    assert submitted == [], "nothing may be signed or submitted when the seed does not decode"
    assert swap["status"] == "failed"
    assert swap["payout_txid"] is None
    assert [row["status"] for row in payouts] == ["failed"]
    assert all(row["txid"] is None for row in payouts)

    reason = swap["failed_reason"] or ""
    assert SIGNING_SEED_ENV_VAR in reason, (
        f"the swap has stopped with a credited deposit and the recorded reason does not name the "
        f"variable that caused it: {reason!r}"
    )
    assert "nothing was submitted and no fee was claimed" in reason
    assert NOT_A_SEED not in reason, "the rejected value must not reach a database column"


def test_the_rescue_gate_would_recognize_this_reason_once_the_marker_is_added():
    """The half that is NOT in this tree yet, measured rather than claimed (rule 17).

    services/payout_rescue.PRE_SIGNING_MARKERS is a SHARED file and out of scope
    for this change, so today refused_before_signing() returns "" for the refusal
    above and rescue_payout.py still declines to re-drive the swap -- which is the
    correct direction for a gate that cannot recognize a reason (rule 2: "I could
    not find a broadcast" is not "there was no broadcast").

    What this test pins is that the sentence this file produces carries the exact
    phrase the proposed one-line marker matches on, so the two halves fit when the
    patch lands. It asserts the CURRENT state as well, so it fails loudly on the
    day the marker is added rather than quietly describing a tree that has moved.
    """
    with pytest.raises(XRPSigningRefused) as refusal:
        derive_and_check(NOT_A_SEED, XRP_HOT_ACCOUNT)
    reason = str(refusal.value)
    marker = "nothing was submitted and no fee was claimed"
    assert marker in reason.lower(), "the proposed marker would not match this refusal"
    matched = refused_before_signing(reason)
    assert matched in ("", marker), (
        f"refused_before_signing() returned {matched!r}. Either PRE_SIGNING_MARKERS has gained the "
        f"proposed marker -- in which case delete this assertion and keep the one above -- or it has "
        f"gained a DIFFERENT marker that matches a post-signing reason too, which is the hazard that "
        f"table exists to avoid"
    )


# --- 2. refuse_mainnet() asks the question it claims to ask -----------------


def test_a_server_that_does_not_report_its_network_is_REFUSED():
    """Not reading the network is not reading a safe one.

    FAILS WITHOUT THE FIX: `None in frozenset({0})` is False, so the old
    membership test returned the string "network_id None, build 3.4.1" and every
    caller printed it as a pass.
    """
    _answer_server_info({"build_version": "3.4.1"})
    with pytest.raises(XRPMainnetRefused) as refusal:
        xrp_testnet_module.refuse_mainnet()
    assert "did not report a network_id" in str(refusal.value)


def test_a_network_id_that_is_not_a_number_is_REFUSED():
    """An unreadable field means this is not the response the path was written against.

    FAILS WITHOUT THE FIX: `"testnet" in frozenset({0})` is False and passed.
    """
    _answer_server_info({"network_id": "testnet", "build_version": "3.4.1"})
    with pytest.raises(XRPMainnetRefused) as refusal:
        xrp_testnet_module.refuse_mainnet()
    assert "not an integer" in str(refusal.value)


def test_a_STRINGIFIED_mainnet_id_is_REFUSED():
    """The one that is mainnet and did not look like it.

    FAILS WITHOUT THE FIX: `"0" in frozenset({0})` is False, so a server
    reporting its network id as the STRING "0" was reported as a test network by
    the function this module's header calls the thing that makes its callers
    mainnet-safe.
    """
    _answer_server_info({"network_id": "0", "build_version": "3.4.1"})
    with pytest.raises(XRPMainnetRefused) as refusal:
        xrp_testnet_module.refuse_mainnet()
    assert "MAINNET" in str(refusal.value)


def test_an_integer_mainnet_id_is_still_refused_and_a_testnet_one_still_passes():
    """The two answers the old comparison got right, so the fix is a narrowing.

    The returned sentence is what xrp_send_tagged.py prints and what
    atomic_swap_xrp.py and xrp_balances.py put through a console check, so it has
    to keep naming both the id and the build (rule 14).
    """
    _answer_server_info({"network_id": 0, "build_version": "3.4.1"})
    with pytest.raises(XRPMainnetRefused):
        xrp_testnet_module.refuse_mainnet()

    _answer_server_info({"network_id": 1, "build_version": "3.4.1"})
    verdict = xrp_testnet_module.refuse_mainnet()
    assert "network_id 1" in verdict
    assert "NOT mainnet" in verdict
    assert "build 3.4.1" in verdict


def test_the_refusal_is_still_a_RuntimeError_for_every_existing_caller():
    """Callers catch RuntimeError, so the new type must remain one.

    xrp_send_tagged.py prints this function's result and xrp_balances.py and
    atomic_swap_xrp.py wrap it in `except Exception`. XRPMainnetRefused
    subclasses XRPSigningRefused which subclasses RuntimeError -- the type the
    old code raised -- and this is the assertion that keeps a future narrowing of
    that hierarchy from silently escaping every existing handler.
    """
    assert issubclass(XRPMainnetRefused, RuntimeError)
    _answer_server_info({"build_version": "3.4.1"})
    with pytest.raises(RuntimeError):
        xrp_testnet_module.refuse_mainnet()


def _answer_server_info(info: dict) -> None:
    """Point chains/xrp_testnet.rpc() at a seeded server_info body.

    The REAL rpc() runs -- its params shape, its 200-with-error check and its
    `result` extraction are all exercised -- and only requests.post is replaced,
    which is the narrowest substitution available and the one every other XRP
    test file uses. Not a monkeypatch fixture because this helper is called
    several times inside one test and each call must replace the last.
    """
    class _Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"result": {"status": "success", "info": info}}

    xrp_testnet_module.requests.post = lambda url, **kwargs: _Response()


# --- 3. the adapter publishes the rank its stranded rows were read at -------


def test_the_adapter_publishes_the_confirmation_rank_of_its_unattributable_drops():
    """The value services/deposit_service.py needs, by the name it reads.

    FAILS WITHOUT THE FIX: the attribute does not exist, and getattr with no
    default raises AttributeError.

    THE RANK IS ASSERTED AGAINST chains/xrp_units.py rather than against 1, so a
    change to what a validated ledger counts as moves both together (rule 11).
    """
    adapter = _adapter_over([])
    assert adapter.unattributable_confirmations == LEDGER_VALIDATED
    assert adapter.unattributable_confirmations == MAX_LEDGER_RANK


def test_every_recorded_untagged_payment_really_IS_from_a_validated_ledger():
    """The claim the constant makes, established by running the scan both ways.

    This is the test that makes the constant honest rather than convenient: an
    UNVALIDATED untagged payment must produce NO element, because the rank
    published above would then describe it wrongly. chains/xrp_payments.
    delivered_drops_to() is what enforces it -- it refuses unless `validated is
    True` -- and untagged_payments() turns that refusal into a console line
    instead of a row.

    FAILS IF THE VALIDATION REQUIREMENT IS EVER RELAXED, which is the only way
    the constant could start lying.
    """
    validated = _adapter_over([_payment("A" * 64, validated=True)])
    validated.find_deposits_to_address(XRP_HOT_ACCOUNT)
    assert len(validated.unattributable_drops) == 1

    unvalidated = _adapter_over([_payment("B" * 64, validated=False)])
    unvalidated.find_deposits_to_address(XRP_HOT_ACCOUNT)
    assert unvalidated.unattributable_drops == [], (
        "an unvalidated untagged payment produced a stranded row, so the rank this adapter "
        "publishes for those rows no longer describes all of them"
    )


def test_the_attribute_survives_a_scan_that_raises():
    """A failed scan must not leave the rank describing a list from a previous one.

    The rank is a constant per adapter, so this is cheap -- and it is asserted
    rather than assumed because the DROPS list is deliberately cleared at the top
    of every scan (tests/test_xrp_unattributable_deposits.py pins that), and a
    future change that moved the rank onto the scan would have to keep these two
    in step.
    """
    adapter = _adapter_over([_payment("C" * 64, validated=True)])
    adapter.find_deposits_to_address(XRP_HOT_ACCOUNT)

    def explode(*_args, **_kwargs):
        raise XRPRPCError("the endpoint went away mid-cycle")

    adapter.call = explode
    with pytest.raises(XRPRPCError):
        adapter.find_deposits_to_address(XRP_HOT_ACCOUNT)
    assert adapter.unattributable_drops == []
    assert adapter.unattributable_confirmations == LEDGER_VALIDATED


# --- 4. a truncated account_tx scan says so ---------------------------------


def test_a_truncated_history_is_ANNOUNCED_rather_than_looking_complete(capsys):
    """A `marker` in the response means the server held more back.

    FAILS WITHOUT THE FIX: nothing read `marker`, so a scan that saw part of the
    desk account's history printed exactly what a complete one printed.

    THE ACCOUNT AND THE COUNT ARE BOTH IN THE LINE, because "there is more" with
    no "more than what" is rule 3's error -- a count with no denominator -- and
    rule 14 asks for a pasted line to be self-describing a day later.
    """
    adapter = _adapter_over([_payment("D" * 64, tag=1)], marker={"ledger": 100, "seq": 3})
    events = adapter.find_deposits_to_address(XRP_HOT_ACCOUNT)
    printed = capsys.readouterr().out

    assert "TRUNCATED SCAN" in printed, (
        f"a truncated account_tx response printed no warning, so it is indistinguishable from a "
        f"complete one. What it did print: {printed!r}"
    )
    assert XRP_HOT_ACCOUNT in printed
    assert "1 entries" in printed
    assert events, "the warning must not replace the scan -- the entries it DID see are still credited"


def test_a_complete_scan_gains_no_warning_line(capsys):
    """A healthy cycle stays quiet, so the line means something when it appears.

    Rule 13's "treat skipped plus success in the same output as a defect in the
    output", applied one step earlier: a warning that fires every cycle is a
    warning an operator learns to skim past, and then misses the real one.
    """
    adapter = _adapter_over([_payment("E" * 64, tag=1)])
    adapter.find_deposits_to_address(XRP_HOT_ACCOUNT)
    assert "TRUNCATED SCAN" not in capsys.readouterr().out


# --- the seeded transport these tests share ---------------------------------


def _payment(tx_hash: str, *, tag=None, validated: bool = True, drops: str = "2500000") -> dict:
    """One account_tx entry in the `tx`-nested shape account_tx actually returns.

    `tx`-nested and not flat, because chains/xrp.py calls ONLY account_tx --
    chains/xrp_payments._unwrap()'s docstring records which nesting each rippled
    method uses, measured on 3.4.1 from the operator's host. 2,500,000 drops is
    the figure tests/xrp_seeded_transport.py records as MEASURED off the live
    server, as the STRING rippled sends.
    """
    body = {"hash": tx_hash, "TransactionType": "Payment", "Destination": XRP_HOT_ACCOUNT}
    if tag is not None:
        body["DestinationTag"] = tag
    return {
        "tx": body,
        "meta": {"TransactionResult": "tesSUCCESS", "delivered_amount": drops},
        "validated": validated,
    }


def _adapter_over(entries: list[dict], *, marker=None) -> XRPAdapter:
    """A REAL XRPAdapter whose socket is a responder returning `entries`.

    Its __init__, its call(), its find_deposits_to_address() and all of
    chains/xrp_payments.py run; only requests.post is replaced. Assigned on the
    module rather than through monkeypatch because several adapters are built in
    one test and each needs its own entry list.
    """
    class _Response:
        def __init__(self, payload):
            self._payload = payload

        def raise_for_status(self):
            return None

        def json(self):
            return self._payload

    def post(url, **kwargs):
        json.loads(kwargs["data"])  # the real body is built and parsed, as the server would
        # `marker` IS WHAT rippled SENDS FOR A TRUNCATED RESULT and is omitted entirely
        # when the page is complete, which is the distinction the warning rests on -- a
        # fixture that always carried the key, even as None, could not exercise both
        # halves.
        body: dict = {"status": "success", "transactions": entries}
        if marker is not None:
            body["marker"] = marker
        return _Response({"result": body})

    xrp_module.requests.post = post
    return XRPAdapter(url=SEEDED_URL, min_confirmations=1)


def _seed_one_pending_xrp_swap(db_path: str) -> None:
    """One GRC -> XRP swap in `payout_pending`, the state the payout worker acts on.

    apply_migrations() is run because workers/payout_worker.py runs it once at
    startup and idx_payouts_one_live_per_swap is what makes a second live payout
    row impossible -- a test that skipped it would measure a database the worker
    never sees. Same reasoning, same shape, as
    tests/test_xrp_payout_wiring._seed_one_pending_xrp_swap().
    """
    conn = connect_db(db_path, create=True)
    conn.executescript(SCHEMA)
    now = "2026-10-11T00:00:00+00:00"
    conn.execute(
        "INSERT INTO quotes (id, from_asset, to_asset, input_amount, quoted_rate, fee_bps, "
        "network_fee_reserve, output_amount_estimate, expires_at, created_at) "
        "VALUES ('q_xrp_e2e', 'GRC', 'XRP', 100.0, 0.02, 150, 0.0, 1.97, ?, ?)",
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
        ) VALUES ('s_xrp_e2e', 'q_xrp_e2e', 'GRC', 'XRP', ?, ?,
                  100.0, 100.0, 0.02, 150, 0.0, 1.97, 'payout_pending', 6,
                  'grc_txid', NULL, ?, ?, ?, NULL, ?, NULL)
        """,
        (GRC_PAYOUT, XRP_CUSTOMER_PAYOUT, now, now, now, now),
    )
    conn.commit()
    apply_migrations(conn)
    conn.close()
