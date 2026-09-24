"""The HTLC locktime encoding, and what the redeem script actually says.

Role: test / measurement (runs the real encoder and the real script builder,
      asserts on real bytes)
Reads: swap_terminal/modules/atomic_htlc_scripts.py,
      swap_terminal/modules/htlc_timelock.py
Writes: nothing
Can move funds: no. Nothing here builds a transaction, signs anything, opens a
        socket or touches a chain. It calls pure functions and inspects the
        bytes they return.
Mainnet-safe: yes

WHAT THIS FILE USED TO ESTABLISH, AND WHAT IT PINS NOW.

Until 2026-09-24 `number_to_le_bytes()` implemented Bitcoin's COMPACT SIZE
encoding -- the varint used for array lengths in the p2p protocol -- and its
output was pushed into the HTLC redeem script as the operand of
OP_CHECKLOCKTIMEVERIFY, which reads a CScriptNum. The two encodings are
different, and for every locktime a real swap would use they disagreed:

    locktime asked        bytes pushed      CLTV read        pushed now
        500,000           fe20a10700        128,000,254      20a107
        800,000           fe00350c00        204,800,254      00350c
      3,000,000           fec0c62d00        768,000,254      c0c62d
  1,735,689,600           fe80857467    444,336,537,854      80857467

The 0xfe prefix was the varint's "a four-byte value follows" tag. CScriptNum
has no tag, so it read the tag as the low byte of the number. Every value in
the third column is far beyond any height Bitcoin will reach, so the refund
branch of every contract that code built never became spendable.

The four assertions that pinned that behavior were marked DEFECT and said, in
their own docstrings, that the fix would make them fail. The fix landed, so
they are INVERTED here rather than deleted: same four locktimes, same table,
now asserting that what the encoder pushes is what CLTV will read (CLAUDE.md
rule 2 -- a test pinning replaced behavior changes to pin the stronger
invariant).

WHAT THE PROOF IN THIS FILE IS, AND EXACTLY WHERE IT STOPS.

It is a measurement of BYTES. Three independent things are checked:

  1. encode_script_number()'s output decodes, through a decoder written here
     from Bitcoin's CScriptNum rules rather than from the encoder's inverse,
     back to the number that was asked for -- across the 0x7f/0x80,
     0x7fff/0x8000 and four-byte boundaries where the sign-pad byte decides
     whether a value reads positive or negative.
  2. The redeem script that build_htlc_redeem_script() returns disassembles to
     the expected opcode sequence, including OP_DROP immediately after
     OP_CHECKLOCKTIMEVERIFY -- CLTV leaves its operand on the stack, and a
     script missing that DROP fails on the refund branch with an unclean
     stack, which is again only discovered when a refund is attempted.
  3. The P2SH address derived from the script round-trips: base58check decodes
     to the testnet P2SH version byte plus hash160 of the exact script bytes.

WHAT IT IS NOT. NEITHER BRANCH OF THIS CONTRACT HAS BEEN EXERCISED ON A CHAIN.
Not on mainnet, not on testnet, not in regtest -- this machine has no chain
access at all. Nothing here proves that a node accepts the script, that the
redeem branch spends with a preimage, or -- the one that matters most -- that
the refund branch becomes spendable when the height passes. CLAUDE.md's
verification section is explicit that the only honest proof is a contract
funded and then REFUNDED after expiry on a test chain, and that the refund
branch is the one that runs when something has already gone wrong. What
follows is bytes matching a specification, which is a reason to believe the
spend will work and is not the same as having checked it (rule 17).

The scriptnum decoder here is a reimplementation, and that is deliberate: the
thing under test is the ENCODER, and decoding its output with its own inverse
would prove only that the function is self-consistent.
"""

import base58
import pytest
from modules.atomic_htlc_scripts import (
    TESTNET_P2SH_VERSION,
    build_htlc_redeem_script,
    encode_script_number,
    script_to_p2sh_address,
)
from modules.utils import hash160

# Two testnet P2PKH addresses that decode cleanly and are NOT the same key --
# build_htlc_redeem_script() refuses a script whose two branches resolve to one
# hash160, which is its own test below.
PARTICIPANT = "mg3G6MkQSxMp1iCUYFH5JztynohhGYHPDA"
REFUND = "mkHS9ne12qx9pS9VojpwU5xtRd4T7X7ZUt"
SECRET_HASH = "ff" * 32  # not a credential: a placeholder DIGEST, no preimage exists for it

OP_IF = 0x63
OP_ELSE = 0x67
OP_ENDIF = 0x68
OP_SHA256 = 0xA8
OP_EQUALVERIFY = 0x88
OP_DUP = 0x76
OP_HASH160 = 0xA9
OP_CHECKSIG = 0xAC
OP_CHECKLOCKTIMEVERIFY = 0xB1
OP_DROP = 0x75


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


def disassemble(script: bytes) -> list:
    """Split a script into a list of opcodes (ints) and pushed data (bytes).

    Handles the plain 1..75-byte pushes and OP_PUSHDATA1, which is everything
    build_htlc_redeem_script() can emit for a hash160, a 32-byte digest or a
    locktime. Anything longer would be a different bug and is left to raise.
    """
    items: list = []
    index = 0
    while index < len(script):
        opcode = script[index]
        index += 1
        if 1 <= opcode <= 75:  # the direct-push opcode range: the opcode IS the byte count
            items.append(script[index : index + opcode])
            index += opcode
        elif opcode == 0x4C:  # OP_PUSHDATA1
            length = script[index]
            index += 1
            items.append(script[index : index + length])
            index += length
        else:
            items.append(opcode)
    return items


# (requested locktime, the bytes the encoder pushes, what CLTV reads)
#
# The third column USED to differ from the first -- that difference was the
# defect. It is now equal by construction, and it is still written out per row
# rather than asserted as `== locktime` alone, because the point of the table
# is that a reader can check the middle column by hand.
MEASURED = [
    (500_000, "20a107", 500_000),
    (800_000, "00350c", 800_000),
    (3_000_000, "c0c62d", 3_000_000),
    (1_735_689_600, "80857467", 1_735_689_600),
]


@pytest.mark.parametrize(("locktime", "pushed_hex", "cltv_reads"), MEASURED)
def test_the_encoder_emits_a_script_number_not_a_varint(locktime, pushed_hex, cltv_reads):
    """INVERTED from the DEFECT case this file used to carry.

    The old assertion was `encoded[0] == 0xFE` -- the varint's four-byte tag,
    which CScriptNum read as data. There is no tag byte in a script number, so
    the inverse assertion is that the first byte is the low byte of the value.
    """
    encoded = encode_script_number(locktime)
    assert encoded.hex() == pushed_hex
    assert decode_scriptnum(encoded) == cltv_reads
    assert encoded[0] == locktime & 0xFF, "the first byte of a script number is the value's low byte, not a length tag"
    assert len(encoded) <= 5, "a locktime never needs more than five bytes, pad included"


@pytest.mark.parametrize(("locktime", "pushed_hex", "cltv_reads"), MEASURED)
def test_the_locktime_the_script_enforces_is_the_one_requested(locktime, pushed_hex, cltv_reads):
    """INVERTED. The gap between asked-for and enforced was the defect; it is zero.

    This is the assertion the whole change exists for: whatever the caller
    asks for is what OP_CHECKLOCKTIMEVERIFY compares against. It reads the
    operand out of the REAL redeem script rather than out of the encoder, so a
    future change that encodes correctly and then pushes the bytes wrongly
    still fails here.
    """
    script = build_htlc_redeem_script(SECRET_HASH, PARTICIPANT, REFUND, locktime)
    items = disassemble(script)
    operand = items[items.index(OP_CHECKLOCKTIMEVERIFY) - 1]
    assert isinstance(operand, bytes)
    assert decode_scriptnum(operand) == locktime


@pytest.mark.parametrize(
    "value",
    [
        1,
        16,
        0x7E,
        0x7F,  # top bit clear: one byte, no pad
        0x80,  # top bit set: needs the pad, or it reads as -0
        0x81,
        0xFF,
        0x7FFF,  # two bytes, no pad
        0x8000,  # two bytes plus pad
        0xFFFF,
        0x7FFFFF,
        0x800000,
        0x7FFFFFFF,  # four bytes, no pad
        0x80000000,  # four bytes plus pad -- five on the wire
        500_000,
        1_735_689_600,
    ],
)
def test_encode_decode_round_trips_across_every_sign_bit_boundary(value):
    """The boundaries are where a missing pad byte turns a locktime negative.

    A negative operand makes OP_CHECKLOCKTIMEVERIFY fail the script outright,
    so on the refund branch the difference between a pad byte and no pad byte
    is the difference between a refund and no refund.
    """
    encoded = encode_script_number(value)
    assert decode_scriptnum(encoded) == value
    if encoded[-1] == 0x00:
        # A trailing zero byte is only ever a sign pad. Proof that it is
        # earning its place: drop it and the same bytes read as a NON-POSITIVE
        # number instead of the value asked for. Non-positive rather than
        # negative because of one case: 0x80 with the pad removed is
        # sign-and-magnitude NEGATIVE ZERO, which decodes to 0 and would make a
        # `< 0` assertion fail for the smallest value that needs a pad at all.
        assert encoded[-2] & 0x80
        unpadded = decode_scriptnum(encoded[:-1])
        assert unpadded != value
        assert unpadded <= 0
    else:
        assert not encoded[-1] & 0x80, "no pad byte, so the top bit must already be clear"


def test_zero_is_the_empty_push_and_negatives_are_refused():
    """Zero has no bytes in CScriptNum, and a negative locktime is a caller bug."""
    assert encode_script_number(0) == b""
    assert decode_scriptnum(b"") == 0
    with pytest.raises(ValueError, match="never negative"):
        encode_script_number(-1)


def test_a_zero_locktime_is_refused_by_the_script_builder():
    """Zero encodes to an empty push, which CLTV reads as 0 -- refundable at once.

    This is the guard that makes the encoder fix safe on its own terms: an
    empty operand is not a malformed script, it is a VALID script whose refund
    branch is spendable from the moment the contract is funded.
    """
    with pytest.raises(ValueError, match="positive block height"):
        build_htlc_redeem_script(SECRET_HASH, PARTICIPANT, REFUND, 0)


def test_identical_participant_and_refund_addresses_are_refused():
    """Both branches needing one key is the third defect, refused at the decision.

    modules/atomic_swapper.py passed the initiator's own address as both values
    in all six swap directions until 2026-09-24. The counterparty could then
    never redeem with the preimage, so the contract was an expensive way to pay
    yourself.
    """
    with pytest.raises(ValueError, match="same hash160"):
        build_htlc_redeem_script(SECRET_HASH, PARTICIPANT, PARTICIPANT, 800_000)


def test_the_redeem_script_disassembles_to_the_expected_opcode_sequence():
    """The whole script, opcode by opcode, with OP_DROP where it must be.

    CLTV does not consume its operand: it verifies and leaves the number on the
    stack. Without the OP_DROP immediately after it, the refund branch ends
    with an extra stack item and fails cleanup -- and, as with everything else
    on this branch, that is discovered at refund time.
    """
    locktime = 800_000
    script = build_htlc_redeem_script(SECRET_HASH, PARTICIPANT, REFUND, locktime)
    items = disassemble(script)

    participant_hash = base58.b58decode_check(PARTICIPANT)[1:]
    refund_hash = base58.b58decode_check(REFUND)[1:]

    assert items == [
        OP_IF,
        OP_SHA256,
        bytes.fromhex(SECRET_HASH),
        OP_EQUALVERIFY,
        OP_DUP,
        OP_HASH160,
        participant_hash,
        OP_EQUALVERIFY,
        OP_CHECKSIG,
        OP_ELSE,
        encode_script_number(locktime),
        OP_CHECKLOCKTIMEVERIFY,
        OP_DROP,
        OP_DUP,
        OP_HASH160,
        refund_hash,
        OP_EQUALVERIFY,
        OP_CHECKSIG,
        OP_ENDIF,
    ]
    # Stated separately from the list comparison so a failure says WHICH
    # invariant broke rather than dumping nineteen items.
    cltv_at = items.index(OP_CHECKLOCKTIMEVERIFY)
    assert items[cltv_at + 1] == OP_DROP
    assert items[cltv_at - 1] == encode_script_number(locktime)


def test_the_p2sh_address_still_round_trips():
    """The address is base58check(testnet P2SH version || hash160(script)).

    Worth pinning because the encoder change alters the script BYTES, and
    therefore the address of every future contract. What must not change is the
    derivation: an address that no longer matches its own script is funds sent
    somewhere nobody holds the script for.
    """
    script = build_htlc_redeem_script(SECRET_HASH, PARTICIPANT, REFUND, 500_000)
    address = script_to_p2sh_address(script)
    decoded = base58.b58decode_check(address)
    assert decoded[:1] == TESTNET_P2SH_VERSION
    assert decoded[1:] == hash160(script)
    assert address.startswith("2"), "testnet P2SH addresses start with 2"


def test_changing_only_the_locktime_changes_the_contract_address():
    """Two locktimes, two scripts, two addresses -- and never the same one.

    This is the operational consequence of the fix, stated as a test: a swap
    started today and a swap started tomorrow fund DIFFERENT addresses, because
    the locktime is now derived from the chain tip instead of being the same
    500000 for every contract ever built.
    """
    a = script_to_p2sh_address(build_htlc_redeem_script(SECRET_HASH, PARTICIPANT, REFUND, 900_000))
    b = script_to_p2sh_address(build_htlc_redeem_script(SECRET_HASH, PARTICIPANT, REFUND, 900_144))
    assert a != b
