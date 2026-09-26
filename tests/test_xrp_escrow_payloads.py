#!/usr/bin/env python3
"""Every escrow payload parses and SIGNS, offline, before any ledger sees it.

Role: tests (read-only)
Reads: nothing. No network. xrpl-py is used purely as a parser and a signer.
Writes: nothing
Can move funds: no. Nothing here submits; the signed blobs are discarded.
Mainnet-safe: yes

WHY THIS FILE EXISTS. xrp_htlc_escrow.py's three transaction payloads were
inline dicts inside main() until 2026-09-26, so the only way to learn whether one
was well formed was to submit it -- and the operator's first run against a real
ledger spent its step 4 discovering something else entirely (that the server
would not sign). A malformed EscrowCreate would have cost another round trip
after that one.

So the payloads are functions now, and these tests push each through xrpl-py's
OWN model classes -- the same `Transaction.from_xrpl()` the submit path uses --
and then sign it. Parsing catches a missing or mistyped required field; signing
catches anything the serializer rejects. Both happen here, for free, on a machine
with no ledger.

WHAT IT STILL CANNOT SAY: whether the LEDGER accepts the semantics. A perfectly
formed EscrowFinish with a wrong fulfillment parses and signs exactly as well as
one with the right fulfillment -- the difference is decided by rippled, and
steps 5 and 6 of the harness are the only things that can observe it. This file
rules out the boring failures so that a run's failures are interesting.

xrpl-py is an OPTIONAL dependency here (it is not in
swap_terminal/requirements.txt; the clients reach it through lazy imports), so
this skips when it is absent, the way tests/test_price_row_selection.py skips on
pandas.
"""

from __future__ import annotations

import pytest

pytest.importorskip("xrpl", reason="xrpl-py is an optional dependency; chains/xrp_submit imports it lazily")

from chains.xrp_crypto_condition import preimage_condition, preimage_fulfillment
from xrpl.models.transactions.transaction import Transaction
from xrpl.transaction import sign
from xrpl.wallet import Wallet

from xrp_htlc_escrow import (
    escrow_cancel_tx,
    escrow_create_tx,
    escrow_finish_tx,
    finish_fee_drops,
)

# A destination that is not the signer. Any valid classic address does; this one
# is a faucet account from the operator's own run, which is public information --
# an address, never a seed.
RECEIVER = "rBfM7je6e9Ca2cMvuRn7cr9xExFgDa5NGx"

PREIMAGE = bytes(range(32))
CONDITION = preimage_condition(PREIMAGE)
FULFILLMENT = preimage_fulfillment(PREIMAGE)

# Fields autofill would normally supply. Set explicitly so sign() needs no
# network: autofill is the only part of the submit path that talks to a server,
# and skipping it is what makes these tests offline.
FIXED = {"Fee": "10", "Sequence": 1, "LastLedgerSequence": 999_999}


def _signable(payload: dict) -> Transaction:
    """Parse through xrpl-py's own dispatcher and sign. Raises if either refuses."""
    wallet = Wallet.create()
    merged = {**FIXED, **payload, "Account": wallet.classic_address}
    if payload.get("Owner"):
        merged["Owner"] = wallet.classic_address
    transaction = Transaction.from_xrpl(merged)
    sign(transaction, wallet)          # raises on anything the serializer rejects
    return transaction


def test_the_escrow_create_payload_parses_and_signs():
    transaction = _signable(escrow_create_tx("rSENDER", RECEIVER, 1_000_000, CONDITION, 843_778_920))
    assert transaction.condition == CONDITION
    assert transaction.cancel_after == 843_778_920
    # Amount is a STRING of drops. xrpl-py's model accepts an int and rippled
    # does not, so an int here works locally and is rejected by the server --
    # a failure that only appears where it costs a round trip.
    assert transaction.amount == "1000000"
    assert isinstance(escrow_create_tx("rS", RECEIVER, 1_000_000, CONDITION, 1)["Amount"], str)


def test_the_escrow_finish_payload_parses_and_signs_and_keeps_its_explicit_fee():
    fee = finish_fee_drops(FULFILLMENT)
    transaction = _signable(
        escrow_finish_tx("rSENDER", "rOWNER", 7, condition=CONDITION, fulfillment=FULFILLMENT, fee=fee)
    )
    # The FEE is the point of asserting here. FIXED supplies "10" and the
    # payload's own Fee must win, because autofill's reference fee earns
    # telINSUF_FEE_P on a finish carrying a fulfillment.
    assert transaction.fee == str(fee) == "360"
    assert transaction.offer_sequence == 7
    assert transaction.fulfillment == FULFILLMENT
    assert transaction.condition == CONDITION


def test_the_escrow_cancel_payload_carries_no_condition_and_no_fulfillment():
    """The timelock branch names neither. A cancel carrying a condition is a finish."""
    payload = escrow_cancel_tx("rSENDER", "rOWNER", 7)
    assert "Condition" not in payload
    assert "Fulfillment" not in payload
    transaction = _signable(payload)
    assert transaction.offer_sequence == 7


def test_the_finish_payload_lets_owner_differ_from_the_submitter():
    """An EscrowFinish may be submitted by ANYONE; Owner is who created the escrow.

    This is not a detail -- it is how a swap's counterparty claims their leg. A
    builder that defaulted Owner to the sender would bake in "the creator
    finishes it", which is the opposite case.
    """
    payload = escrow_finish_tx("rSUBMITTER", "rCREATOR", 3, condition=CONDITION, fulfillment=FULFILLMENT, fee=360)
    assert payload["Account"] == "rSUBMITTER"
    assert payload["Owner"] == "rCREATOR"
