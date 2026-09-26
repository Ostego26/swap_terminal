#!/usr/bin/env python3
"""The PREIMAGE-SHA-256 encoding, pinned against an independent implementation.

Role: tests (read-only)
Reads: nothing. No network, no daemon, no files.
Writes: nothing
Can move funds: no
Mainnet-safe: yes

WHERE THE VECTORS CAME FROM, which is the whole value of this file. They are not
copied from documentation and they are not what I believed the encoding to be.
They are the output of the `cryptoconditions` package from PyPI -- an
implementation that shares no code with this repository -- run on 2026-09-26
against the same preimages, and compared byte for byte with
chains/xrp_crypto_condition. Five lengths matched: 0, 1, 32, 32 (random) and 77.

The library is NOT a dependency here and is imported nowhere. It was used once
to check arithmetic, and these literals are what survives of that check, so the
comparison keeps working on a machine that never installs it.

WHY THE EMPTY PREIMAGE IS TESTED ALONGSIDE THE 32-BYTE ONE. Three numbers in
the encoding move between them -- the outer length 0x25 -> 0x02, the inner
length 0x20 -> 0x00, and the cost 0x20 -> 0x00 -- so a module that hardcoded
any of the three would satisfy one vector and fail the other. A single vector
here would have let a hardcoded `A0258020...810120` pass, and that is exactly
the defect worth catching: a wrong condition does not error, it creates an
escrow that no fulfillment can ever satisfy.
"""

from __future__ import annotations

import hashlib

import pytest
from chains.xrp_crypto_condition import (
    HTLC_PREIMAGE_BYTES,
    condition_matches_preimage,
    preimage_condition,
    preimage_fulfillment,
)

# The escrow harness lives at the repo root (rule 10: entry points are findable
# by looking), and tests/conftest.py puts the root on sys.path, which is how
# tests/test_xrp_send_tagged.py already imports xrp_send_tagged.
from xrp_htlc_escrow import finish_fee_drops, ripple_time

EMPTY_CONDITION = "A0258020E3B0C44298FC1C149AFBF4C8996FB92427AE41E4649B934CA495991B7852B855810100"
EMPTY_FULFILLMENT = "A0028000"
COUNTING_PREIMAGE = bytes(range(32))
COUNTING_CONDITION = "A0258020630DCD2966C4336691125448BBB25B4FF412A49C732DB2C8ABC1B8581BD710DD810120"
COUNTING_FULFILLMENT = "A0228020000102030405060708090A0B0C0D0E0F101112131415161718191A1B1C1D1E1F"


def test_the_empty_preimage_vector():
    assert preimage_condition(b"") == EMPTY_CONDITION
    assert preimage_fulfillment(b"") == EMPTY_FULFILLMENT


def test_the_thirty_two_byte_vector():
    assert preimage_condition(COUNTING_PREIMAGE) == COUNTING_CONDITION
    assert preimage_fulfillment(COUNTING_PREIMAGE) == COUNTING_FULFILLMENT


def test_the_condition_commits_to_the_same_sha256_the_btc_and_ltc_scripts_do():
    """ONE preimage must unlock both legs, or the swap is not atomic.

    modules/atomic_htlc_scripts.build_htlc_redeem_script() commits to
    sha256(preimage) through OP_SHA256, and this asserts the XRPL condition's
    fingerprint is the identical digest. If either side ever moved to HASH160 or
    SHA-512/256 the swap would still LOOK complete on each chain separately and
    the preimage revealed on one would not open the other.
    """
    preimage = bytes(range(100, 132))
    fingerprint = hashlib.sha256(preimage).hexdigest().upper()
    assert fingerprint in preimage_condition(preimage)


def test_the_fulfillment_contains_the_preimage_verbatim():
    """It IS the secret. This documents that, so nothing logs the return value."""
    preimage = bytes(range(32))
    assert preimage.hex().upper() in preimage_fulfillment(preimage)


def test_lengths_are_computed_rather_than_spelled():
    """A preimage of an unusual length still encodes with correct lengths.

    77 bytes: outer length 0x25 + 45 and cost 0x4D, none of which appears in the
    two pinned vectors. The library agreed with this on 2026-09-26.
    """
    condition = preimage_condition(bytes(77))
    assert condition.startswith("A025")          # the fingerprint half never moves
    assert condition.endswith("81014D")          # cost = 77 = 0x4D, one byte
    fulfillment = preimage_fulfillment(bytes(77))
    assert fulfillment.startswith("A04F804D")    # 0x4F = 79 = 2 + 77, 0x4D = 77


def test_a_cost_past_one_byte_takes_a_leading_zero_because_der_integers_are_signed():
    """128 encodes as 00 80, not as 80. Off-by-one-bit, and it is in the spec."""
    assert preimage_condition(bytes(128)).endswith("8102" + "0080")


def test_a_preimage_too_long_for_a_short_form_length_is_refused_not_mis_encoded():
    with pytest.raises(ValueError, match="long-form length"):
        preimage_fulfillment(bytes(200))


def test_the_local_match_check_accepts_the_right_preimage_and_rejects_a_near_miss():
    """The ledger's answer to all three failures is one opaque code.

    tecCRYPTOCONDITION_ERROR is what a wrong preimage, a malformed condition and
    a mismatched cost all produce, and it costs a transaction fee to learn.
    """
    preimage = bytes(range(32))
    assert condition_matches_preimage(preimage_condition(preimage), preimage)
    assert not condition_matches_preimage(preimage_condition(preimage), preimage[:-1] + b"\xff")
    # Case-insensitive on the way in, because a condition read back off a ledger
    # or out of a JSON file may not be uppercase.
    assert condition_matches_preimage(preimage_condition(preimage).lower(), preimage)


def test_the_htlc_preimage_size_matches_what_the_other_chains_use():
    """32 bytes, the same secret modules/atomic_htlc_scripts hashes."""
    assert HTLC_PREIMAGE_BYTES == 32


# ---------------------------------------------------------------------------
# The two arithmetic decisions in xrp_htlc_escrow.py. Both are the kind that
# fail with an opaque ledger code rather than an error, which is why they are
# functions at the bottom of that file instead of expressions inside a step.
# ---------------------------------------------------------------------------


def test_the_escrow_finish_fee_is_sized_from_the_fulfillments_bytes_not_its_hex():
    """330 drops plus 10 per 16 bytes, and the units are BYTES.

    A 32-byte preimage encodes to a 36-byte fulfillment -- the four framing bytes
    A0 22 80 20 plus the preimage, NOT the 34 that 0x22 names, which is the inner
    body's length and not the whole element's. (Written as 34 first; this
    assertion is what caught it, and the fee happened to be unaffected because
    both land in the same 16-byte chunk.) Three chunks once the partial one is
    counted: 330 + 30 = 360. Sizing from the hex
    STRING would double the chunk count -- harmless but wrong -- and sizing from
    the preimage instead of the encoded fulfillment would miss the four framing
    bytes, land on two chunks, and earn telINSUF_FEE_P from a ledger that does
    not say which number was short.

    Autofill sends the 10-drop reference fee, so this cannot be left to it.
    """
    fulfillment = preimage_fulfillment(bytes(32))
    assert len(fulfillment) // 2 == 36
    assert finish_fee_drops(fulfillment) == 360
    # The empty preimage is a 2-byte fulfillment: still one chunk, never zero.
    assert finish_fee_drops(preimage_fulfillment(b"")) == 340


def test_a_cancel_after_is_in_ripple_seconds_not_unix_seconds():
    """Off by 946,684,800, and the failure is silent and expensive.

    XRPL's epoch is 2000-01-01. Handing CancelAfter a Unix timestamp produces an
    escrow whose timelock expired around 1970 -- immediately cancellable by
    anybody, which on a real swap is the counterparty's money walking away while
    every field still looks populated.
    """
    assert ripple_time(946_684_800) == 0
    assert ripple_time(946_684_800 + 90) == 90
    # A present-day Unix timestamp must come out far SMALLER, never unchanged.
    now_unix = 1_790_000_000
    assert ripple_time(now_unix) == now_unix - 946_684_800
    assert ripple_time(now_unix) < now_unix
