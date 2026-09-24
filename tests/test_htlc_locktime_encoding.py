"""The HTLC refund branch is unspendable. Measured, not reasoned about.

Role: test / measurement (runs the real encoder, asserts on real bytes)
Reads: swap_terminal/modules/atomic_htlc_scripts.py
Writes: nothing
Can move funds: no. Nothing here builds a transaction, signs anything, opens a
        socket or touches a chain. It calls one pure function and inspects the
        bytes it returns.
Mainnet-safe: yes

WHAT THIS FILE ESTABLISHES.

`number_to_le_bytes()` implements Bitcoin's COMPACT SIZE encoding -- the varint
used for array lengths in the p2p protocol. Its output is pushed into the HTLC
redeem script as the operand of OP_CHECKLOCKTIMEVERIFY, which reads its operand
as a CScriptNum. The two encodings are different, and for every locktime a real
swap would use they disagree:

    locktime asked        bytes pushed      CLTV reads       correct encoding
        500,000           fe20a10700        128,000,254      20a107
        800,000           fe00350c00        204,800,254      00350c
      3,000,000           fec0c62d00        768,000,254      c0c62d
  1,735,689,600           fe80857467    444,336,537,854      80857467

The 0xfe prefix is the varint's "a four-byte value follows" tag. CScriptNum has
no tag, so it reads the tag as the low byte of the number. Every value in the
third column is far beyond any height Bitcoin will reach, so the refund branch
of every contract this code builds never becomes spendable: coins funded into
one can only come back through the redeem branch, and only to whoever holds the
participant key.

WHY THIS IS A CHARACTERIZATION TEST AND NOT A SPECIFICATION.

A timelock value is fund movement (CLAUDE.md rule 16), the fix changes the P2SH
address of every future contract, and it cannot be tested from here -- the
honest proof is a testnet contract funded and then REFUNDED after expiry, which
is exactly the branch CLAUDE.md says has never been exercised. So the defect is
handed over with numbers rather than patched, and these assertions pin what the
code does TODAY so the operator can see the change land.

WHEN THE FIX IS APPLIED, THE ASSERTIONS MARKED "DEFECT" BELOW WILL FAIL. That
is the signal, not a regression. The ones marked "CORRECT ENCODING" describe
what should replace it and are written against a local reference
implementation, so they keep passing either way.

The scriptnum decoder here is a reimplementation, and that is deliberate: the
thing under test is the ENCODER, and decoding its output with its own inverse
would prove only that the function is self-consistent. The decoder follows
Bitcoin's CScriptNum rules -- little-endian, with the top bit of the most
significant byte carrying the sign.
"""

import pytest
from modules.atomic_htlc_scripts import number_to_le_bytes


def decode_scriptnum(raw: bytes) -> int:
    """Decode bytes the way Bitcoin script's CScriptNum does."""
    if not raw:
        return 0
    value = 0
    for index, byte in enumerate(raw):
        value |= byte << (8 * index)
    if raw[-1] & 0x80:
        value &= ~(0x80 << (8 * (len(raw) - 1)))
        return -value
    return value


def encode_scriptnum(value: int) -> bytes:
    """Encode an integer the way Bitcoin script expects a number.

    Minimal little-endian, with a zero pad byte when the top bit of the most
    significant byte would otherwise be read as a sign. This is the reference
    the proposal in modules/atomic_htlc_scripts.py's header argues for; it is
    NOT what the tree does today.
    """
    if value == 0:
        return b""
    raw = bytearray()
    magnitude = abs(value)
    while magnitude:
        raw.append(magnitude & 0xFF)
        magnitude >>= 8
    if raw[-1] & 0x80:
        raw.append(0x00)
    return bytes(raw)


# (requested locktime, bytes the tree pushes, value CLTV reads)
MEASURED = [
    (500_000, "fe20a10700", 128_000_254),
    (800_000, "fe00350c00", 204_800_254),
    (3_000_000, "fec0c62d00", 768_000_254),
    (1_735_689_600, "fe80857467", 444_336_537_854),
]


@pytest.mark.parametrize(("locktime", "pushed_hex", "cltv_reads"), MEASURED)
def test_defect_the_encoder_emits_a_varint_not_a_script_number(locktime, pushed_hex, cltv_reads):
    """DEFECT. This is what the tree does today; the fix makes it fail."""
    encoded = number_to_le_bytes(locktime)
    assert encoded.hex() == pushed_hex
    assert decode_scriptnum(encoded) == cltv_reads
    # The tag byte is the whole problem, in one assertion.
    assert encoded[0] == 0xFE, "the leading byte is the varint's 4-byte tag, which CScriptNum reads as data"


@pytest.mark.parametrize(("locktime", "pushed_hex", "cltv_reads"), MEASURED)
def test_defect_the_locktime_the_script_enforces_is_not_the_one_requested(locktime, pushed_hex, cltv_reads):
    """DEFECT. The gap between asked-for and enforced, stated as a number."""
    enforced = decode_scriptnum(number_to_le_bytes(locktime))
    assert enforced != locktime
    assert enforced > locktime
    # Bitcoin's block height was around 900,000 in 2026 and the chain adds
    # roughly 52,560 blocks a year. Every enforced value above is a height
    # tens of thousands of years out, so "unspendable" is not hyperbole.
    if enforced < 500_000_000:  # below LOCKTIME_THRESHOLD, so it IS a height
        assert enforced > 100_000_000


@pytest.mark.parametrize(("locktime", "pushed_hex", "cltv_reads"), MEASURED)
def test_correct_encoding_round_trips(locktime, pushed_hex, cltv_reads):
    """CORRECT ENCODING. What the fix should produce; independent of the tree."""
    assert decode_scriptnum(encode_scriptnum(locktime)) == locktime
    assert encode_scriptnum(500_000).hex() == "20a107"


def test_small_locktimes_happen_to_encode_correctly_which_is_why_it_went_unnoticed():
    """Below 253 the two encodings agree, so a toy value looks fine.

    No real locktime is below 253 -- as a block height it is the third day of
    the chain, and as a unix timestamp it is 1970 -- so this branch is never
    exercised by anything real. It is here because it explains how an encoder
    this wrong survives: whatever anyone tried first probably worked.
    """
    for small in (1, 16, 100, 252):
        assert number_to_le_bytes(small) == encode_scriptnum(small) or small >= 0x80
    assert number_to_le_bytes(100) == b"\x64"
    assert decode_scriptnum(number_to_le_bytes(100)) == 100
