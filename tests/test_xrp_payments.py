"""What the XRP deposit scan credits, holds back, and refuses.

Role: test (pure function; no socket, no daemon, no database)
Reads: chains/xrp_payments.py
Writes: nothing
Can move funds: no -- but what it pins releases payouts, which is why the
      refusals outnumber the happy path here by three to one.
Mainnet-safe: yes

THE FIRST FOUR TESTS ARE THE PARTIAL PAYMENT EXPLOIT.

An XRP Payment's `Amount` is what the sender ASKED to deliver. With
tfPartialPayment set, the ledger may deliver less -- and the transaction still
succeeds, still reports tesSUCCESS, and still shows the original larger Amount.
What arrived is in meta.delivered_amount and nowhere else.

An exchange that credits Amount can be drained: claim a million XRP, deliver
one drop, be credited a million. This is the best-known integration mistake on
this ledger and it has taken real money off real exchanges. These tests exist
so that a future simplification of this module cannot reintroduce it quietly.

The seeded shapes here are UNVERIFIED against a real rippled (see THE HONEST
STATUS in chains/xrp.py), so these prove the rules, not the wire format.
"""

import pytest
from chains.xrp_payments import XRPPaymentError, deposit_events_from_transactions

ADDRESS = "rSwapTerminalHotAccountAddressXXXXXXX"
TXID = "A" * 64


def entry(**overrides):
    """A creditable validated payment, which each test then spoils one field of."""
    tx = {
        "hash": TXID,
        "TransactionType": "Payment",
        "Destination": ADDRESS,
        "DestinationTag": 4242,
        "Amount": "25000000",
    }
    meta = {"TransactionResult": "tesSUCCESS", "delivered_amount": "25000000"}
    base = {"tx": tx, "meta": meta, "validated": True}
    for key, value in overrides.items():
        if key in ("tx", "meta"):
            base[key] = {**base[key], **value}
        else:
            base[key] = value
    return base


# --- the partial payment exploit -------------------------------------------

def test_the_credited_amount_comes_from_delivered_not_from_amount():
    """THE EXPLOIT, in one assertion.

    Amount claims 1,000,000 XRP. delivered_amount says one drop. Crediting the
    first is how exchanges have been drained; this must credit 0.000001.
    """
    scan = deposit_events_from_transactions(
        [entry(tx={"Amount": "1000000000000"}, meta={"delivered_amount": "1"})], ADDRESS, 1
    )
    assert scan.events[0]["amount"] == 0.000001


def test_a_payment_with_no_delivered_amount_refuses_and_does_not_fall_back():
    """The fallback IS the exploit, so there must not be one.

    A missing delivered_amount is the one case where reading Amount would look
    like a reasonable default, which is exactly why it raises instead.
    """
    broken = entry()
    del broken["meta"]["delivered_amount"]
    with pytest.raises(XRPPaymentError, match="NOT falling back to the Amount field"):
        deposit_events_from_transactions([broken], ADDRESS, 1)


def test_an_issued_currency_is_never_credited_as_xrp():
    """An IOU is not XRP, and anybody can mint one.

    delivered_amount is a STRING of drops for XRP and a JSON OBJECT for an
    issued currency. Crediting the object's value as XRP would pay out real XRP
    for a token the depositor issued themselves.
    """
    fake = entry(meta={"delivered_amount": {"currency": "USD", "issuer": "rScammer", "value": "1000000"}})
    with pytest.raises(XRPPaymentError, match="ISSUED CURRENCY"):
        deposit_events_from_transactions([fake], ADDRESS, 1)


def test_a_non_string_delivered_amount_refuses():
    """Drop counts travel as strings so clients cannot round them through a double."""
    for bad in (25000000, 25.0, None, True):
        with pytest.raises(XRPPaymentError):
            deposit_events_from_transactions([entry(meta={"delivered_amount": bad})], ADDRESS, 1)


# --- what is and is not a deposit ------------------------------------------

def test_a_plain_validated_payment_is_credited_at_its_tag():
    scan = deposit_events_from_transactions([entry()], ADDRESS, 1)
    assert scan.events == [{
        "txid": TXID, "vout": 4242, "address": ADDRESS, "amount": 25.0, "confirmations": 1,
    }]
    assert scan.deferred == []


def test_a_failed_transaction_is_reported_not_credited():
    """A tec* code is INCLUDED in a ledger and claims a fee while delivering nothing.

    So it is neither success nor absence, and rendering it as either would be
    wrong -- it is money the customer spent that did not arrive.
    """
    scan = deposit_events_from_transactions(
        [entry(meta={"TransactionResult": "tecUNFUNDED_PAYMENT"})], ADDRESS, 1
    )
    assert scan.events == []
    assert "tecUNFUNDED_PAYMENT" in scan.deferred[0]


def test_a_payment_with_no_destination_tag_is_held_back_and_reported():
    """Money that arrived and cannot be attributed to any swap.

    Not credited, not guessed at, and NOT silent -- this is a customer whose
    deposit is sitting in the account needing a human.
    """
    anonymous = entry()
    del anonymous["tx"]["DestinationTag"]
    scan = deposit_events_from_transactions([anonymous], ADDRESS, 1)
    assert scan.events == []
    assert "CANNOT be attributed" in scan.deferred[0]


def test_payments_out_of_the_account_are_not_deposits():
    """account_tx returns everything touching the account, both directions."""
    scan = deposit_events_from_transactions([entry(tx={"Destination": "rSomebodyElse"})], ADDRESS, 1)
    assert scan.events == []


def test_non_payment_transactions_are_ignored():
    for kind in ("OfferCreate", "TrustSet", "AccountSet", "EscrowCreate"):
        scan = deposit_events_from_transactions([entry(tx={"TransactionType": kind})], ADDRESS, 1)
        assert scan.events == [], kind


def test_an_unvalidated_payment_is_credited_at_rank_zero_and_reported():
    """Rank 0 keeps it below any threshold; the deferred line makes the wait visible."""
    scan = deposit_events_from_transactions([entry(validated=False)], ADDRESS, 1)
    assert scan.events[0]["confirmations"] == 0
    assert "NOT yet validated" in scan.deferred[0]


# --- shape and identity -----------------------------------------------------

def test_both_rippled_api_shapes_are_accepted():
    """v1 nests the transaction under `tx`, v2 under `tx_json`.

    Guessing one would make every deposit invisible on a server using the
    other -- an empty list that reads as "no deposits yet".
    """
    v1 = entry()
    v2 = {"tx_json": v1["tx"], "metaData": v1["meta"], "validated": True}
    assert deposit_events_from_transactions([v1], ADDRESS, 1).events \
        == deposit_events_from_transactions([v2], ADDRESS, 1).events


def test_a_non_integer_destination_tag_refuses():
    """The tag becomes `vout`, which db.py:110 keys on."""
    for bad in ("4242", 42.5, True, {"tag": 1}):
        with pytest.raises(XRPPaymentError, match="non-integer"):
            deposit_events_from_transactions([entry(tx={"DestinationTag": bad})], ADDRESS, 1)


def test_a_transaction_with_no_hash_refuses():
    """Without an id it cannot be deduplicated, so every rescan re-credits it."""
    nameless = entry()
    del nameless["tx"]["hash"]
    with pytest.raises(XRPPaymentError, match="no hash"):
        deposit_events_from_transactions([nameless], ADDRESS, 1)


def test_two_payments_claiming_one_tag_stop_the_scan():
    """Cannot happen against a real ledger -- a Payment carries exactly one tag.

    If it ever does, the response shape is not what this module assumes, and
    every way of resolving it silently loses or invents money.
    """
    with pytest.raises(XRPPaymentError, match="cannot happen"):
        deposit_events_from_transactions([entry(), entry()], ADDRESS, 1)


def test_different_tags_in_one_transaction_list_are_fine():
    scan = deposit_events_from_transactions(
        [entry(tx={"DestinationTag": 1}), entry(tx={"hash": "B" * 64, "DestinationTag": 2})], ADDRESS, 1
    )
    assert sorted(e["vout"] for e in scan.events) == [1, 2]


def test_an_empty_ledger_answer_is_an_empty_scan_not_an_error():
    for empty in ([], None):
        scan = deposit_events_from_transactions(empty, ADDRESS, 1)
        assert scan.events == [] and scan.deferred == []
