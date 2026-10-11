#!/usr/bin/env python3
"""The XRP swap path walked in BOTH directions, on rows rather than on returns.

Role: tests (seeded rippled transport; no socket, nothing signed, nothing submitted)
Reads: chains/xrp.py, chains/xrp_payments.py, chains/xrp_address.py,
      services/deposit_service.py, services/unattributable_deposit_service.py,
      services/payout_service.py, services/pair_view.py, services/quote_service.py,
      config.Config, db.py's real SCHEMA
Writes: one throwaway sqlite file per test, under pytest's tmp_path
Can move funds: no. requests.post and xrpl.transaction.submit_and_wait are both
      replaced, so the real adapter, the real deposit service and the real payout
      service run with nothing reachable. Every address is derived in
      tests/valid_addresses.py or by xrpl.wallet.Wallet.create().
Mainnet-safe: yes -- no test in this file opens a socket.

WHY A SECOND XRP FILE WHEN THERE ARE NINETEEN, said first because that is the
question rule 8 asks of anything new. The nineteen test one mechanism each --
tests/test_xrp_payments.py the classifier, tests/test_xrp_destination_tags.py the
allocator, tests/test_xrp_unattributable_deposits.py the stranded row,
tests/test_xrp_payout_wiring.py the arming -- and each is the right shape for its
subject. What none of them holds is the SET of outcomes a whole direction can
have, and two of the gaps below were found only by enumerating them:

    as a DESTINATION   tests/test_xrp_payout_wiring.py covers the happy submit.
                       The other four outcomes a submit can have -- a tec* result,
                       a tesSUCCESS that is not validated, a response with no
                       hash, and a response that never came back at all -- had no
                       test at all, and "a refused submit cannot be recorded as
                       sent" is a statement about all five together.
    as a SOURCE        each tag case has a test. What the five of them MEAN
                       together -- which of them reach a table and which reach
                       none -- is the question an operator asks when money is in
                       the shared account, and it was answered in five files.

=============================================================================
MEASURED 2026-10-11, AND THESE ARE THE NUMBERS THE TESTS BELOW PIN
=============================================================================

ONE SHARED ACCOUNT, FIVE WAYS A TAG CAN BE WRONG, run end to end through
services/deposit_service.process_active_swaps() against the real schema:

    what arrived                       deposit_events  unattributable  swap
    ---------------------------------  --------------  --------------  ----------
    tag matching an open swap                       1               0  payout_pending
    NO tag at all                                   0               1  unchanged
    a tag no swap on the account owns               0               1  unchanged
    TWO payments carrying one tag                   2               0  under_review
    a tag whose swap is already completed           0               1  unchanged

The fourth row is the one worth reading twice. Two payments with one tag are both
attributed to that swap, refresh_swap_from_chain() sums every row, 2.5 + 2.5 = 5.0
against an expected 2.5, and the swap HALTS at `under_review` rather than paying
out twice the quote. A halt, not a release, which is the safe direction -- and it
is a swap that has stopped moving and needs a person.

FIVE SUBMIT OUTCOMES, each driven twice through
services/payout_service.process_pending_payouts() so that "a second cycle does not
retry" is measured rather than reasoned about:

    what the ledger said               submits  payouts row       swap
    ---------------------------------  -------  ----------------  ---------
    tesSUCCESS, validated, with a hash       1  broadcast + hash  completed
    tecUNFUNDED_PAYMENT, validated           1  failed, txid NULL failed
    tesSUCCESS, validated=False              1  failed, txid NULL failed
    tesSUCCESS, validated, NO hash           1  failed, txid NULL failed
    the response never came back             1  failed, txid NULL failed

ONE submit in every row, over two cycles. WHAT PROTECTS THAT is three database
facts and no ledger mechanism, which is worth stating plainly because XRP has no
created_at_time idempotency key the way the ICP ledger does:

  the claim         services/payout_service.claim_swap_for_payout() moves the swap
                    payout_pending -> paying with a conditional UPDATE and COMMITS
                    before any network call, so two workers cannot both send.
  the terminal      process_pending_payouts() selects `status = 'payout_pending'`
  status            and nothing else. A swap left in `failed` is never re-claimed,
                    which is what makes the second cycle a no-op.
  the partial index  db.idx_payouts_one_live_per_swap blocks a second payout row
                    while one is created/broadcast/completed -- and a `failed` row
                    is OUTSIDE that index, so it does NOT protect an operator-driven
                    retry. rescue_payout.py is the gate that does, and it refuses
                    every one of the four failure reasons above: measured, all four
                    match no entry in services/payout_rescue.PRE_SIGNING_MARKERS.

AND THE ONE THING THE LEDGER WOULD HAVE DONE IS REMOVED BY THE RETRY ITSELF,
stated because it is the obvious place to look for safety and it is not there. An
XRPL account Sequence makes a resubmission of the SAME signed blob invalid
(tefPAST_SEQ), but xrpl-py's submit_and_wait() autofills a FRESH Sequence from a
live account_info read, so a retry is a different transaction that the ledger will
happily apply. The sequence protects a replay; it does not protect a retry.
"""

from __future__ import annotations

import json
import sqlite3
from time import time

import pytest
from chains import xrp as xrp_module
from chains.xrp import XRPAdapter
from chains.xrp_address import is_valid_classic_address
from config import Config
from db import SCHEMA, apply_migrations, connect_db, db_session, dict_factory
from services import pricing
from services.deposit_service import process_active_swaps
from services.pair_view import customer_availability, pair_serviceability
from services.payout_service import process_pending_payouts
from services.pricing import IDS
from services.quote_service import create_quote
from test_web_surfaces import DEPOSIT_ACCOUNTS
from valid_addresses import (
    GRC_PAYOUT,
    XRP_ACCOUNT_ZERO,
    XRP_ACCOUNT_ZERO_WITH_TYPO,
    XRP_CUSTOMER_PAYOUT,
    XRP_HOT_ACCOUNT,
    xrp_family_seed,
)
from workers.common import get_config_dict
from xrp_seeded_transport import Recorder, account_info, server_info

wallet_module = pytest.importorskip(
    "xrpl.wallet", reason="xrpl-py absent, so the real signing path is UNCHECKED"
)
transaction_module = pytest.importorskip(
    "xrpl.transaction", reason="xrpl-py absent, so the signed-and-submitted path is UNCHECKED"
)
# httpx ARRIVES WITH xrpl-py and is what its client raises on a transport failure,
# which is the only way to stand in for a response that never came back.
# chains/xrp._sign_and_submit() catches (XRPLException, httpx.HTTPError) by name, so
# a stub raising anything else would exercise the wrong handler.
httpx = pytest.importorskip("httpx", reason="xrpl-py absent, so the lost-response path is UNCHECKED")

SIGNING_SEED_ENV_VAR = "XRP_PAYOUT_SECRET_SEED"
SEEDED_URL = "https://seeded.invalid:51234/"
ACCOUNT = XRP_HOT_ACCOUNT

#: 2,500,000 drops is 2.5 XRP, and it is the figure
#: tests/xrp_seeded_transport.py records as MEASURED off the live testnet server --
#: `meta.delivered_amount` came back as the STRING "2500000" from rippled 3.4.1 --
#: so the shape these tests feed the parser is the shape the ledger really sends.
DELIVERED_DROPS = "2500000"
DELIVERED_XRP = 2.5

#: A seed that really DECODES, derived rather than typed, for the reason
#: tests/valid_addresses.xrp_family_seed() records at length: XRPAdapter.can_spend
#: comes from DECODING the seed since 2026-10-03, so a hand-typed one with a bad
#: checksum would silently turn every "armed" test here into an unarmed one. The
#: account it derives has never been funded on any network.
SEED_THAT_DECODES = xrp_family_seed("swap_terminal xrp e2e both directions: decodes, owns nothing")


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
    """Every test starts UNARMED, whatever shell ran pytest."""
    monkeypatch.delenv(SIGNING_SEED_ENV_VAR, raising=False)


# =========================================================================
# AS A SOURCE: one shared account, five ways a DestinationTag can be wrong
# =========================================================================


def test_a_tagged_deposit_for_an_open_swap_is_credited(tmp_path):
    """The happy path, so every refusal below is measured against a working one."""
    db, adapters = _cycle_over(tmp_path, [_payment("A" * 64, tag=1)])
    _seed_swap(db, "s_open", ACCOUNT, tag=1, status="awaiting_deposit")
    process_active_swaps(db, _deposit_config(), adapters)

    assert _counts(db) == {"deposit_events": 1, "unattributable": 0, "late": 0}
    swap = db.execute("SELECT * FROM swaps WHERE id = 's_open'").fetchone()
    assert swap["status"] == "payout_pending"
    assert swap["actual_input_amount"] == DELIVERED_XRP
    assert swap["deposit_txid"] == "A" * 64


def test_a_payment_with_NO_tag_reaches_the_stranded_table_and_credits_nothing(tmp_path):
    """No tag at all: discriminator NULL, and no swap moves.

    NULL rather than an integer is the distinction db.py's own column comment
    draws -- "sent with no reference at all" against "sent with a reference that
    matches no open order" -- and they are two different support conversations.
    """
    db, adapters = _cycle_over(tmp_path, [_payment("B" * 64)])
    _seed_swap(db, "s_open", ACCOUNT, tag=1, status="awaiting_deposit")
    process_active_swaps(db, _deposit_config(), adapters)

    assert _counts(db) == {"deposit_events": 0, "unattributable": 1, "late": 0}
    row = db.execute("SELECT * FROM unattributable_deposits").fetchone()
    assert row["txid"] == "B" * 64
    assert row["amount"] == DELIVERED_XRP
    assert row["discriminator"] is None
    assert row["address"] == ACCOUNT
    assert db.execute("SELECT status FROM swaps WHERE id = 's_open'").fetchone()["status"] == (
        "awaiting_deposit"
    ), "a payment nobody can claim must not advance somebody else's swap"


def test_a_tag_no_swap_owns_reaches_the_stranded_table_with_the_tag_recorded(tmp_path):
    """A tag matching no swap: the integer is kept, so a person can match it.

    AND THE CONFIRMATION COUNT IS REAL ON THIS PATH, which is the measurement that
    found the gap the sibling file fixes. This row is written by
    unattributable_deposit_service.unclaimed_rows(), which reads the event's own
    confirmations; the NO-TAG row above is written by stranded_rows(), which until
    2026-10-11 could only record 0. Same table, same cycle, same ledger.
    """
    db, adapters = _cycle_over(tmp_path, [_payment("C" * 64, tag=9)])
    _seed_swap(db, "s_open", ACCOUNT, tag=1, status="awaiting_deposit")
    process_active_swaps(db, _deposit_config(), adapters)

    assert _counts(db) == {"deposit_events": 0, "unattributable": 1, "late": 0}
    row = db.execute("SELECT * FROM unattributable_deposits").fetchone()
    assert row["discriminator"] == 9
    assert row["confirmations"] == 1
    assert "no swap on this asset has that discriminator" in row["why"]
    assert "DestinationTag" in row["why"], (
        "the reason must name THIS chain's discriminator -- a SOL refusal that said DestinationTag is "
        "the defect services/unattributable_deposit_service.discriminator_name() exists to prevent"
    )


def test_TWO_payments_with_one_tag_are_summed_and_the_swap_HALTS(tmp_path):
    """Both are credited, the sum exceeds the quote, and the swap stops.

    THE SAFE DIRECTION, AND STILL A STOPPED SWAP. 2.5 + 2.5 = 5.0 against an
    expected 2.5 is outside AMOUNT_TOLERANCE_PCT, so advance_deposit_status()
    sends it to `under_review` instead of paying out twice the quote. Nothing is
    released and a person has to decide.

    chains/xrp_payments._reject_duplicate_tags() deliberately does NOT fire here
    and that is correct: it keys on (txid, tag), so two DIFFERENT transactions
    carrying one tag are two facts rather than a response shape it does not
    understand. Asserted so a future widening of that guard -- which would turn a
    double payment into a refused scan for the whole account, and therefore for
    every other XRP swap on it -- fails here first.
    """
    db, adapters = _cycle_over(tmp_path, [_payment("D" * 64, tag=1), _payment("E" * 64, tag=1)])
    _seed_swap(db, "s_open", ACCOUNT, tag=1, status="awaiting_deposit")
    process_active_swaps(db, _deposit_config(), adapters)

    assert _counts(db) == {"deposit_events": 2, "unattributable": 0, "late": 0}
    swap = db.execute("SELECT * FROM swaps WHERE id = 's_open'").fetchone()
    assert swap["status"] == "under_review"
    assert swap["actual_input_amount"] == DELIVERED_XRP * 2


def test_a_late_payment_carrying_a_COMPLETED_swaps_tag_is_recorded_not_credited(tmp_path):
    """A completed swap still owns its tag, and a later payment to it is stranded.

    The reason names the swap AND its status, because "no swap has this tag" and
    "a finished swap has this tag" send an operator to two different places --
    which is why unclaimed_events() returns a reason per event rather than a list.
    """
    db, adapters = _cycle_over(tmp_path, [_payment("F" * 64, tag=1)])
    _seed_swap(db, "s_done", ACCOUNT, tag=1, status="completed")
    process_active_swaps(db, _deposit_config(), adapters)

    assert _counts(db) == {"deposit_events": 0, "unattributable": 1, "late": 0}
    row = db.execute("SELECT * FROM unattributable_deposits").fetchone()
    assert row["discriminator"] == 1
    assert "s_done" in row["why"]
    assert "completed" in row["why"]
    assert db.execute("SELECT status FROM swaps WHERE id = 's_done'").fetchone()["status"] == "completed"


# =========================================================================
# AS A DESTINATION: the five things a submit can answer
# =========================================================================


def test_a_validated_success_is_recorded_as_sent_with_its_hash(tmp_path, monkeypatch):
    """The only outcome that may become a `broadcast` payouts row."""
    outcome = _drive_payout(tmp_path, monkeypatch, _responds({
        "meta": {"TransactionResult": "tesSUCCESS"}, "hash": "1" * 64, "validated": True,
    }))
    assert outcome["submits"] == 1
    assert outcome["swap"]["status"] == "completed"
    assert outcome["swap"]["payout_txid"] == "1" * 64
    assert [(row["status"], row["txid"]) for row in outcome["payouts"]] == [("broadcast", "1" * 64)]


@pytest.mark.parametrize(
    ("label", "result", "marker"),
    [
        # A tec* code REACHED the ledger and claimed a fee while delivering
        # nothing. The worst possible recording would be `broadcast` with this
        # hash, because the hash is real and the delivery is not.
        ("a tec* result", {"meta": {"TransactionResult": "tecUNFUNDED_PAYMENT"},
                           "hash": "2" * 64, "validated": True}, "NOT tesSUCCESS"),
        # Succeeded but not final. chains/xrp_units.py: this ledger's finality is
        # BINARY, so this is not a weaker yes.
        ("not validated", {"meta": {"TransactionResult": "tesSUCCESS"},
                           "hash": "3" * 64, "validated": False}, "validated=False"),
        # Validated and successful with no hash to record. The payment was almost
        # certainly made, and there is nothing to write into payout_txid.
        ("no hash in the response", {"meta": {"TransactionResult": "tesSUCCESS"},
                                     "validated": True}, "carried no hash"),
    ],
)
def test_a_submit_the_ledger_did_not_complete_is_NEVER_recorded_as_sent(
    tmp_path, monkeypatch, label, result, marker
):
    """Three non-deliveries, none of which may leave a `broadcast` row.

    THE ASSERTION IS THE ROW AND NOT THE EXCEPTION, because an exception that
    services/payout_service.py caught and then recorded as a send would satisfy
    `pytest.raises` and lose a customer's payout. The hash is checked against the
    whole database rather than against payout_txid alone: a hash the ledger
    produced for a payment it did not complete must not be written anywhere a
    later reader could take for a delivery.
    """
    outcome = _drive_payout(tmp_path, monkeypatch, _responds(result))
    assert outcome["submits"] == 1, f"{label}: a second cycle retried a payout it must not retry"
    assert outcome["swap"]["status"] == "failed"
    assert outcome["swap"]["payout_txid"] is None
    assert [row["status"] for row in outcome["payouts"]] == ["failed"]
    assert all(row["txid"] is None for row in outcome["payouts"])
    assert marker in (outcome["swap"]["failed_reason"] or ""), (
        f"{label}: the recorded reason does not say which of the three outcomes this was, so an "
        f"operator cannot tell a fee-claiming tec* from a payment that may yet validate"
    )
    submitted_hash = result.get("hash")
    if submitted_hash:
        # NOT "the hash is nowhere". swaps.failed_reason NAMES it on purpose and
        # must keep doing so -- it is the one value an operator needs to look the
        # transaction up, and rule 14 asks for exactly that. What must not carry it
        # is either column a later reader takes for a DELIVERY, and the first
        # version of this assertion searched the whole database and failed on the
        # message that is doing its job.
        assert outcome["swap"]["payout_txid"] is None
        assert all(row["txid"] is None for row in outcome["payouts"])
        assert submitted_hash in (outcome["swap"]["failed_reason"] or ""), (
            f"{label}: the recorded reason does not name the hash, so an operator has nothing to look "
            f"up for a transaction that may have reached the ledger"
        )
        delivery_columns = _delivery_columns(outcome["db_path"])
        assert submitted_hash not in delivery_columns, (
            f"{label}: the ledger's hash for a payment that delivered nothing is in a column that "
            f"records deliveries: {delivery_columns!r}"
        )


def test_a_LOST_response_fails_the_swap_and_a_second_cycle_does_not_send_again(tmp_path, monkeypatch):
    """The case where nothing can be known, and the one that could pay twice.

    A transport failure AFTER the blob was relayed is indistinguishable from a
    refusal before it, so the recorded reason must say exactly that rather than
    implying the payment did not happen.

    TWO CYCLES, ONE SUBMIT, and the second cycle is the whole point: a swap left
    in `failed` is never selected by process_pending_payouts() again, which is the
    mechanism that stands between a lost response and a double payment on a ledger
    with no idempotency key for this path.
    """
    def lost(transaction, client, wallet, **kwargs):
        raise httpx.ReadTimeout("the response never came back")

    outcome = _drive_payout(tmp_path, monkeypatch, lost)
    assert outcome["submits"] == 1
    assert outcome["swap"]["status"] == "failed"
    assert outcome["swap"]["payout_txid"] is None
    assert [row["status"] for row in outcome["payouts"]] == ["failed"]
    reason = outcome["swap"]["failed_reason"] or ""
    assert "THIS IS NOT PROOF NOTHING WAS SENT" in reason
    assert "pays twice" in reason


def test_a_swap_left_failed_is_not_selected_for_payout_again(tmp_path, monkeypatch):
    """The mechanism named directly, with seeded rows rather than through a failure.

    Written separately from the test above so a regression says WHICH half stopped
    holding: that one measures the outcome of a lost response, this one measures
    the selection rule that makes the outcome safe. A `failed` swap moved back to
    `payout_pending` by hand IS paid -- which is rescue_payout.py's whole subject
    and the reason that tool refuses every reason it does not recognize.
    """
    paying, adapters, submitted, monkeypatched_config = _armed_adapter(monkeypatch)
    transaction_module_patch = _responds({
        "meta": {"TransactionResult": "tesSUCCESS"}, "hash": "9" * 64, "validated": True,
    })
    monkeypatch.setattr(transaction_module, "submit_and_wait", _recording(submitted, transaction_module_patch))

    db_path = str(tmp_path / "xrp_failed_not_retried.db")
    _seed_pending_payout(db_path)
    conn = connect_db(db_path, create=True)
    try:
        conn.execute("UPDATE swaps SET status = 'failed' WHERE id = 's_xrp_e2e'")
        conn.commit()
        process_pending_payouts(conn, monkeypatched_config(paying), adapters)
        assert submitted == [], "a failed swap was paid"

        conn.execute("UPDATE swaps SET status = 'payout_pending' WHERE id = 's_xrp_e2e'")
        conn.commit()
        process_pending_payouts(conn, monkeypatched_config(paying), adapters)
        assert len(submitted) == 1, (
            "moving the swap back to payout_pending did NOT make it payable, so the protection above "
            "is something other than the status and this file names the wrong mechanism"
        )
    finally:
        conn.close()


# =========================================================================
# THE PAIRS: ten directions, and every available one prices
# =========================================================================


def test_config_carries_exactly_the_ten_xrp_directions():
    """Five in, five out, over the five other assets this terminal trades.

    DERIVED FROM Config rather than spelled, so enabling a sixth asset grows this
    assertion instead of breaking it -- which is the fixture-rot
    tests/conftest.py's UNTRADED_ASSET comment records happening twice.
    """
    pairs = {pair for pair in Config.ALLOWED_PAIRS if "XRP" in pair}
    sources = {pair[0] for pair in pairs if pair[1] == "XRP"}
    destinations = {pair[1] for pair in pairs if pair[0] == "XRP"}
    assert sources == destinations, (
        f"XRP is not symmetric: it can be swapped FROM {sorted(destinations)} and TO {sorted(sources)}, "
        f"and a one-way asset is the posture tests/test_allowed_pairs_are_serviceable.py stopped "
        f"exempting on 2026-10-04"
    )
    assert len(pairs) == 2 * len(sources)
    assert len(pairs) == 10, (
        f"ten directions were measured on 2026-10-11 and Config now carries {len(pairs)}: "
        f"{sorted(pairs)}. If an asset was added or removed this count is what has to move"
    )


def test_every_available_xrp_direction_prices_a_real_quote(tmp_path, monkeypatch):
    """The page's promise, with the REAL XRP adapter rather than a permissive stub.

    tests/test_an_available_pair_can_actually_be_quoted.py asks this of all thirty
    pairs through a stub that answers yes to everything, which is the right shape
    for the question it asks. This asks it of the XRP directions with an armed
    XRPAdapter: a real can_spend derived from a real decoded seed, a real
    validate_address() on a real account, and the real deposit-account gate.

    BOTH SIDES OF THE PROMISE. An AVAILABLE direction must price; one that is not
    available must carry a reason, because a pair refused with nothing said is
    rule 14's empty gap and is how the fourth serviceability condition came to be
    missing in the first place.
    """
    monkeypatch.setenv(SIGNING_SEED_ENV_VAR, SEED_THAT_DECODES)
    recorder = _seeded_server(monkeypatch)
    assert recorder is not None
    adapter = XRPAdapter(url=SEEDED_URL, min_confirmations=1)
    assert adapter.can_spend, "the premise is an ARMED XRP adapter"

    adapters = {asset: _Reachable() for asset in IDS}
    adapters["XRP"] = adapter
    config = _priced_config(tmp_path)

    priced, refused = [], []
    for pair in sorted(pair for pair in Config.ALLOWED_PAIRS if "XRP" in pair):
        from_asset, to_asset = pair
        row = {
            **pair_serviceability(config, adapters, from_asset, to_asset),
            "label": f"{from_asset} -> {to_asset}",
        }
        row["enabled"] = row["serviceable"]
        state = customer_availability(row)
        with db_session(config["DB_PATH"]) as db:
            try:
                quote = create_quote(db, config, from_asset, to_asset, 10)
            except ValueError as exc:
                refused.append((row["label"], str(exc)))
                quoted = False
            else:
                quoted = quote["output_amount_estimate"] > 0
                priced.append(row["label"])
        if state["available"]:
            assert quoted, (
                f"{row['label']} is badged {state['word']} -- {state['note']} -- and a real quote for "
                f"it did not price. What the quote said: {dict(refused).get(row['label'], 'it priced to zero')}"
            )
        else:
            assert row["reason"].strip(), f"{row['label']} is not offered and carries no reason"

    # Rule 14: the counts, with what they were counted out of, and what they mean.
    print(f"\nof 10 XRP directions: {len(priced)} priced a real quote, {len(refused)} refused")
    for label, message in refused:
        print(f"  {label}: {message.split('.')[0]}")
    assert priced, "no XRP direction priced at all, so this test measured nothing"


def test_every_direction_INTO_xrp_prices_once_the_reserve_exists(tmp_path, monkeypatch):
    """The 2026-10-02 defect in its own direction, pinned where it happened.

    GRC -> XRP was badged AVAILABLE and then refused to quote for want of
    XRP_NETWORK_FEE_RESERVE, and services/pair_view.py's header records it as the
    FOURTH instance of one defect shape. Five directions pay out in XRP now, so
    the assertion is about all five rather than the one that was reported.
    """
    monkeypatch.setenv(SIGNING_SEED_ENV_VAR, SEED_THAT_DECODES)
    _seeded_server(monkeypatch)
    adapters = {asset: _Reachable() for asset in IDS}
    adapters["XRP"] = XRPAdapter(url=SEEDED_URL, min_confirmations=1)
    config = _priced_config(tmp_path)

    into_xrp = sorted(pair for pair in Config.ALLOWED_PAIRS if pair[1] == "XRP")
    assert into_xrp, "no direction pays out in XRP, so this test measured nothing"
    for from_asset, to_asset in into_xrp:
        verdict = pair_serviceability(config, adapters, from_asset, to_asset)
        assert not verdict["cannot_quote"], (
            f"{from_asset} -> {to_asset}: {verdict['cannot_quote']}"
        )
        with db_session(config["DB_PATH"]) as db:
            quote = create_quote(db, config, from_asset, to_asset, 10)
        assert quote["output_amount_estimate"] > 0
        assert quote["network_fee_reserve"] == pytest.approx(Config.XRP_NETWORK_FEE_RESERVE), (
            "the quote booked a different XRP payout cost than Config carries, which is the "
            "quote-prices-then-cannot-pay sequence one layer in"
        )


# =========================================================================
# THE ADDRESSES: every form the ledger produces, and the near-miss
# =========================================================================


def test_the_ledgers_own_constants_are_accepted_and_a_one_character_typo_is_not(monkeypatch):
    """ACCOUNT_ZERO and a single changed character, from the derived fixtures.

    DERIVED, NEVER INVENTED. tests/valid_addresses.py builds XRP_ACCOUNT_ZERO by
    encoding twenty-one zero bytes under XRP's own alphabet rather than from
    memory, and records that recalling it produced the wrong string once. The typo
    fixture is that same address with its last character changed, which is the
    cheapest real test of a checksum there is.

    NO NETWORK, asserted by a responder that raises: an XRPL address carries its
    own checksum and chains/xrp.py's validate_address() promises to decide this
    locally. A daemon round trip here would be answering a different question
    (whether the account exists), and an unfunded but valid address is exactly
    what a first payment creates.
    """
    def refuse(*_args, **_kwargs):
        raise AssertionError("validate_address() must not touch the network")

    monkeypatch.setattr(xrp_module.requests, "post", refuse)
    adapter = XRPAdapter(url=SEEDED_URL, min_confirmations=1)

    for accepted in (XRP_ACCOUNT_ZERO, XRP_HOT_ACCOUNT, XRP_CUSTOMER_PAYOUT):
        assert adapter.validate_address(accepted), f"{accepted} is a real classic address"
        assert is_valid_classic_address(accepted)
    assert not adapter.validate_address(XRP_ACCOUNT_ZERO_WITH_TYPO), (
        "a one-character change to ACCOUNT_ZERO passed the checksum, which is the whole thing "
        "standing between a customer's typo and a payment that cannot be recalled"
    )
    assert not is_valid_classic_address(XRP_ACCOUNT_ZERO_WITH_TYPO)


def test_the_payout_path_refuses_both_the_typo_and_the_tag_bearing_form(monkeypatch):
    """preview_payout() reads nothing from the server for either refusal.

    An X-address is refused because it carries its own tag, which would make two
    tags on one payment with one of them silently losing. A typo is refused by
    checksum. Both before any read, which the empty call list is the assertion for
    -- "the exception was raised" cannot tell a guard that refused before reading
    the account from one that refused after.
    """
    recorder = _seeded_server(monkeypatch)
    adapter = XRPAdapter(url=SEEDED_URL, min_confirmations=1)
    for refused in (XRP_ACCOUNT_ZERO_WITH_TYPO, "T" + XRP_HOT_ACCOUNT[1:]):
        with pytest.raises(Exception) as caught:
            adapter.preview_payout(refused, 1, XRP_HOT_ACCOUNT)
        assert "nothing was sent" in str(caught.value).lower()
    assert recorder.calls == [], (
        f"a refused payout address reached the network: {[call['method'] for call in recorder.calls]}"
    )


# =========================================================================
# the seeded transport and the row seeding these tests share
# =========================================================================


class _Reachable:
    """A counterparty chain that is reachable, can pay out, and validates anything.

    DELIBERATELY THE MOST PERMISSIVE STUB AVAILABLE, copied in spirit from
    tests/test_an_available_pair_can_actually_be_quoted._Reachable and for the
    same reason: the question here is about XRP, so a counterparty that refused
    something would make a pair unserviceable for a reason that has nothing to do
    with what is measured.
    """

    can_spend = True
    payout_refusal = ""

    def validate_address(self, _address):
        return True

    def why_cannot_pay_out(self):
        return ""

    def owns_address(self, _address):
        return False


def _payment(tx_hash: str, *, tag=None, validated: bool = True) -> dict:
    """One account_tx entry, `tx`-nested as account_tx actually returns it."""
    body = {"hash": tx_hash, "TransactionType": "Payment", "Destination": ACCOUNT}
    if tag is not None:
        body["DestinationTag"] = tag
    return {
        "tx": body,
        "meta": {"TransactionResult": "tesSUCCESS", "delivered_amount": DELIVERED_DROPS},
        "validated": validated,
    }


def _cycle_over(tmp_path, entries: list[dict]):
    """A real database on the real SCHEMA, and a real XRPAdapter over `entries`."""
    db_path = str(tmp_path / "xrp_source.db")
    conn = connect_db(db_path, create=True)
    conn.executescript(SCHEMA)
    apply_migrations(conn)

    class _Response:
        def __init__(self, payload):
            self._payload = payload

        def raise_for_status(self):
            return None

        def json(self):
            return self._payload

    def post(url, **kwargs):
        json.loads(kwargs["data"])
        return _Response({"result": {"status": "success", "transactions": entries}})

    xrp_module.requests.post = post
    return conn, {"XRP": XRPAdapter(url=SEEDED_URL, min_confirmations=1)}


def _deposit_config() -> dict:
    return {
        "XRP_DEPOSIT_ACCOUNT": ACCOUNT,
        "XRP_MIN_CONFIRMATIONS": 1,
        "AMOUNT_TOLERANCE_PCT": 0.01,
    }


def _counts(db) -> dict:
    """The three tables a deposit can land in, always all three (rule 14).

    All three on every assertion rather than only the one a test is about: "it
    reached the right table" and "it reached only that table" are different
    claims, and the 2026-10-11 measurement that started this work was a payment
    that reached NONE of them.
    """
    return {
        "deposit_events": db.execute("SELECT COUNT(*) AS c FROM deposit_events").fetchone()["c"],
        "unattributable": db.execute(
            "SELECT COUNT(*) AS c FROM unattributable_deposits"
        ).fetchone()["c"],
        "late": db.execute("SELECT COUNT(*) AS c FROM late_deposits").fetchone()["c"],
    }


def _seed_swap(db, swap_id: str, account: str, *, tag: int, status: str) -> None:
    """One XRP -> GRC swap on `account` holding `tag`, expecting DELIVERED_XRP.

    The expected amount is the module constant rather than a parameter: every swap in
    this file expects the one delivered figure, and the sixth parameter was an
    argument no caller passed (ruff PLR0913, and rule 9 one level down).
    """
    expected = DELIVERED_XRP
    now = "2026-10-11T00:00:00+00:00"
    db.execute(
        "INSERT INTO quotes (id, from_asset, to_asset, input_amount, quoted_rate, fee_bps,"
        " network_fee_reserve, output_amount_estimate, expires_at, created_at)"
        " VALUES (?, 'XRP', 'GRC', ?, 1.0, 150, 0.001, 1.0, ?, ?)",
        (f"q_{swap_id}", expected, "2026-12-31T00:00:00+00:00", now),
    )
    db.execute(
        "INSERT INTO swaps (id, quote_id, from_asset, to_asset, deposit_address, deposit_tag,"
        " payout_address, expected_input_amount, quoted_rate, fee_bps, network_fee_reserve,"
        " output_amount_estimate, status, min_confirmations, created_at, updated_at, expires_at)"
        " VALUES (?, ?, 'XRP', 'GRC', ?, ?, ?, ?, 1.0, 150, 0.001, 1.0, ?, 1, ?, ?, ?)",
        (swap_id, f"q_{swap_id}", account, tag, GRC_PAYOUT, expected, status, now, now,
         "2026-12-31T00:00:00+00:00"),
    )
    db.commit()


def _seeded_server(monkeypatch):
    """server_info and account_info in the shapes measured off rippled 3.4.1."""
    recorder = Recorder(server_info=server_info(), account_info=account_info(drops="1000000000"))
    monkeypatch.setattr(xrp_module.requests, "post", recorder)
    return recorder


def _responds(result: dict):
    """A submit_and_wait stand-in returning one fixed ledger answer."""
    class _StubResponse:
        def __init__(self, payload):
            self.result = payload

    def stub(transaction, client, wallet, **kwargs):
        return _StubResponse(result)

    return stub


def _recording(submitted: list, inner):
    """Wrap a submit stand-in so every Payment that reached it is kept.

    The list is the assertion for "exactly one submit over two cycles": an empty
    list is how "nothing was signed" becomes a measurement rather than the absence
    of an error.
    """
    def stub(transaction, client, wallet, **kwargs):
        submitted.append(transaction)
        return inner(transaction, client, wallet, **kwargs)

    return stub


def _armed_adapter(monkeypatch):
    """A funded-looking, armed XRPAdapter plus the config its payouts need."""
    paying = wallet_module.Wallet.create()
    monkeypatch.setenv(SIGNING_SEED_ENV_VAR, paying.seed)
    _seeded_server(monkeypatch)
    adapter = XRPAdapter(url=SEEDED_URL, min_confirmations=1)
    assert adapter.can_spend, "the premise of every payout test here is an armed adapter"

    def config_for(wallet):
        return {
            "XRP_MIN_CONFIRMATIONS": 1,
            "GRC_MIN_CONFIRMATIONS": 6,
            "AMOUNT_TOLERANCE_PCT": 0.01,
            "XRP_DEPOSIT_ACCOUNT": wallet.classic_address,
        }

    return paying, {"XRP": adapter}, [], config_for


def _drive_payout(tmp_path, monkeypatch, submit_stub) -> dict:
    """Run TWO payout cycles over one pending swap and return what they left behind.

    TWO cycles, always, because "a second cycle does not retry" is half of every
    assertion in this section and a single cycle cannot measure it.
    """
    paying, adapters, submitted, config_for = _armed_adapter(monkeypatch)
    monkeypatch.setattr(transaction_module, "submit_and_wait", _recording(submitted, submit_stub))

    db_path = str(tmp_path / "xrp_destination.db")
    _seed_pending_payout(db_path)
    conn = connect_db(db_path, create=True)
    try:
        for _ in range(2):
            process_pending_payouts(conn, config_for(paying), adapters)
        swap = conn.execute("SELECT * FROM swaps WHERE id = 's_xrp_e2e'").fetchone()
        payouts = conn.execute("SELECT * FROM payouts WHERE swap_id = 's_xrp_e2e' ORDER BY id").fetchall()
    finally:
        conn.close()
    return {"submits": len(submitted), "swap": swap, "payouts": payouts, "db_path": db_path}


def _seed_pending_payout(db_path: str) -> None:
    """One GRC -> XRP swap in `payout_pending`, with migrations applied.

    apply_migrations() because workers/payout_worker.py runs it at startup and
    idx_payouts_one_live_per_swap is one of the three mechanisms this file names:
    a test that skipped it would measure a database the worker never sees.
    """
    conn = connect_db(db_path, create=True)
    conn.executescript(SCHEMA)
    now = "2026-10-11T00:00:00+00:00"
    conn.execute(
        "INSERT INTO quotes (id, from_asset, to_asset, input_amount, quoted_rate, fee_bps,"
        " network_fee_reserve, output_amount_estimate, expires_at, created_at)"
        " VALUES ('q_xrp_e2e', 'GRC', 'XRP', 100.0, 0.02, 150, 0.0, 1.97, ?, ?)",
        (now, now),
    )
    conn.execute(
        "INSERT INTO swaps (id, quote_id, from_asset, to_asset, deposit_address, payout_address,"
        " expected_input_amount, actual_input_amount, quoted_rate, fee_bps, network_fee_reserve,"
        " output_amount_estimate, status, min_confirmations, deposit_txid, created_at, updated_at,"
        " credited_at, expires_at)"
        " VALUES ('s_xrp_e2e', 'q_xrp_e2e', 'GRC', 'XRP', ?, ?, 100.0, 100.0, 0.02, 150, 0.0,"
        " 1.97, 'payout_pending', 6, 'grc_txid', ?, ?, ?, ?)",
        (GRC_PAYOUT, XRP_CUSTOMER_PAYOUT, now, now, now, now),
    )
    conn.commit()
    apply_migrations(conn)
    conn.close()


def _delivery_columns(path: str) -> str:
    """Every value in the two columns that record a DELIVERY, as one string.

    swaps.payout_txid and payouts.txid, and deliberately not every column in every
    table the way tests/test_xrp_payout_wiring._whole_database() does. That helper
    is searching for a SECRET, where "nowhere at all" is the right claim; here the
    claim is narrower and the difference is the point -- swaps.failed_reason
    carries the hash on purpose, because it is what an operator looks up for a
    transaction that may have reached the ledger.
    """
    conn = sqlite3.connect(path)
    conn.row_factory = dict_factory
    try:
        parts = [
            str(row["payout_txid"])
            for row in conn.execute("SELECT payout_txid FROM swaps").fetchall()
            if row["payout_txid"] is not None
        ]
        parts.extend(
            str(row["txid"])
            for row in conn.execute("SELECT txid FROM payouts").fetchall()
            if row["txid"] is not None
        )
        return " ".join(parts) or "(both columns are NULL on every row)"
    finally:
        conn.close()


def _priced_config(tmp_path) -> dict:
    """A config with every deposit account set and the price cache warm.

    The deposit accounts come from tests/test_web_surfaces.py's table rather than
    being invented here, because services/swap_service.deposit_account() runs a
    REAL format decode: a made-up string would refuse for its shape and these
    tests would pass for the wrong reason.
    """
    db_path = tmp_path / "xrp_pairs.db"
    conn = connect_db(str(db_path), create=True)
    conn.executescript(SCHEMA)
    conn.commit()
    conn.close()

    now = time()
    pricing._cache.update({
        "raw": {
            identifier: {
                "usd": 100.0, "usd_market_cap": 5_000_000_000.0, "usd_24h_vol": 200_000_000.0,
                "usd_24h_change": 1.0, "last_updated_at": 1790717713,
            }
            for identifier in IDS.values()
        },
        "prices": None, "context": None, "source": "seeded",
        "fetched_at": now, "expires_at": now + 999,
    })

    config = dict(get_config_dict())
    config.update(DEPOSIT_ACCOUNTS)
    config["DB_PATH"] = str(db_path)
    config["XRP_DEPOSIT_ACCOUNT"] = ACCOUNT
    return config
