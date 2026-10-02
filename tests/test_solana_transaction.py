"""Solana's legacy transfer format, checked byte for byte against an independent implementation.

Role: test (pure byte layout; no socket, no cluster, no key but the throwaway
      ones this file derives in-process)
Reads: swap_terminal/chains/solana_transaction.py
Writes: nothing
Can move funds: no. NOTHING HERE IS BROADCAST and nothing can be: there is no
      transport in the module under test. Every keypair below is derived from a
      fixed seed written in this file, so it is published by construction and
      holds nothing on any cluster.
Mainnet-safe: yes

WHY THIS FILE IS MOSTLY DATA, AND WHERE THE DATA CAME FROM.

A hand-written serializer cannot be verified by reading it. Both halves of a
wrong byte layout look equally plausible on the page, and the failure mode is a
transaction that moves the right amount to the wrong account, or one the
runtime rejects after a fee has been claimed.

So the vectors below were produced by `@solana/web3.js` 1.x -- Solana's own
JavaScript client, and THE SAME LIBRARY the Node bridge in
swap_terminal/grc-sol-swap uses to sign and broadcast real SOL transfers
(server.js's sendSolPayout). It was installed into a throwaway directory
outside this checkout on 2026-10-02, handed fixed seeds and a fixed blockhash,
and asked for Transaction.serializeMessage() and Transaction.serialize(). The
hex strings in REFERENCE_VECTORS are its output, unedited.

WHAT THE FULL CROSS-CHECK MEASURED, beyond the five vectors pinned here:

    60 random vectors    random payer seed, random destination, random
                         blockhash, lamports across 1 / 127 / 128 / 16383 /
                         16384 / 5000 / 650240 / 1e9.
                         **0 message mismatches. 0 wire mismatches.**
    the reverse          web3.js's Transaction.from() parsed all 60 of the
                         PYTHON-produced transactions and verifySignatures()
                         returned true for every one. Destination, fee payer,
                         blockhash, instruction discriminant and lamports were
                         read back out of its parse and compared. 60/60.

That run is not re-runnable from inside this suite -- it needs Node and a
package that is deliberately not a dependency here -- which is exactly why its
vectors are pinned. chains/solana_address.py established the same pattern with
solders: the reference produces the vectors once, the test holds them from then
on.

WHAT NONE OF IT ESTABLISHES: no transaction built here has been broadcast.
api.devnet.solana.com answers 403 at this container's proxy (re-measured
2026-10-02), so agreement with web3.js is agreement about a FORMAT and says
nothing about whether a cluster accepts the result. chains/solana.py's payout
path is labeled a PROPOSAL for that reason.
"""

from __future__ import annotations

import base58
import pytest
from chains.solana_transaction import (
    BLOCKHASH_BYTES,
    SIGNATURE_BYTES,
    SYSTEM_PROGRAM_ID,
    SYSTEM_TRANSFER_INSTRUCTION,
    TRANSFER_MESSAGE_BYTES,
    TRANSFER_WIRE_BYTES,
    SolanaTransactionError,
    compile_transfer_message,
    decode_compact_u16,
    encode_compact_u16,
    parse_transfer_transaction,
    transfer_instruction_data,
    wire_transaction,
)

# PyNaCl is an optional dependency (chains/solana_signing._public_key_bytes
# records why), so the half of this file that needs a signature skips rather
# than failing collection -- the mechanism tests/test_xrp_adapter.py uses for
# xrpl-py, and the skip reason says what goes unchecked.
nacl_signing = pytest.importorskip(
    "nacl.signing", reason="PyNaCl absent, so the signature and the signed wire form are UNCHECKED"
)

# THE SEEDS THE REFERENCE WAS GIVEN. Thirty-two bytes of 0x07 and of 0x09, and a
# blockhash of thirty-two 0x03 -- chosen so that anybody can regenerate the
# vectors with three lines of JavaScript and no shared state.
PAYER_SEED = bytes([7]) * 32
DESTINATION_SEED = bytes([9]) * 32
BLOCKHASH = base58.b58encode(bytes([3]) * BLOCKHASH_BYTES).decode("ascii")

#: What @solana/web3.js's Keypair.fromSeed() derived from those two seeds. NOT
#: hand-written addresses: the test below derives them with PyNaCl and asserts
#: the two libraries agree, which is a check on the key derivation as well as a
#: fixture.
REFERENCE_PAYER = "GmaDrppBC7P5ARKV8g3djiwP89vz1jLK23V2GBjuAEGB"
REFERENCE_DESTINATION = "J2xccRtuG43drESLYznHhLhQkLTdfepcKYbiQ9BsJVaf"

#: One per interesting lamport value: the smallest sendable amount, the
#: signature fee, the rent-exempt minimum the operator's devnet run measured for
#: a 0-byte account, one whole SOL, and the top of the u64 range. Each entry is
#: web3.js's serializeMessage() and serialize() output, verbatim.
REFERENCE_VECTORS = (
    (
        1,
        "01000103ea4a6c63e29c520abef5507b132ec5f9954776aebebe7b92421eea691446d22cfd1724385aa0c75b64fb78cd602fa1d991fdebf76b13c58ed702eac835e9f6180000000000000000000000000000000000000000000000000000000000000000030303030303030303030303030303030303030303030303030303030303030301020200010c020000000100000000000000",
        "01ead4a47f3a5649d5f973a94ff3dadabd943006806b2cd98a7ae290bf6deaac9a26ab7d8c6b0bb5a9ea04565eb41425928c6b5124c808c6b1a78848d4848b640401000103ea4a6c63e29c520abef5507b132ec5f9954776aebebe7b92421eea691446d22cfd1724385aa0c75b64fb78cd602fa1d991fdebf76b13c58ed702eac835e9f6180000000000000000000000000000000000000000000000000000000000000000030303030303030303030303030303030303030303030303030303030303030301020200010c020000000100000000000000",
    ),
    (
        5_000,
        "01000103ea4a6c63e29c520abef5507b132ec5f9954776aebebe7b92421eea691446d22cfd1724385aa0c75b64fb78cd602fa1d991fdebf76b13c58ed702eac835e9f6180000000000000000000000000000000000000000000000000000000000000000030303030303030303030303030303030303030303030303030303030303030301020200010c020000008813000000000000",
        "01aff3e0adc3d3b2e874dfa3ed98c6c3135c49a03a9f0ac8dc343cbae944886fe4ae020808e23b7c2123575f18c736928ef10ab9564e400fa4bfb55fed5a82b70401000103ea4a6c63e29c520abef5507b132ec5f9954776aebebe7b92421eea691446d22cfd1724385aa0c75b64fb78cd602fa1d991fdebf76b13c58ed702eac835e9f6180000000000000000000000000000000000000000000000000000000000000000030303030303030303030303030303030303030303030303030303030303030301020200010c020000008813000000000000",
    ),
    (
        650_240,
        "01000103ea4a6c63e29c520abef5507b132ec5f9954776aebebe7b92421eea691446d22cfd1724385aa0c75b64fb78cd602fa1d991fdebf76b13c58ed702eac835e9f6180000000000000000000000000000000000000000000000000000000000000000030303030303030303030303030303030303030303030303030303030303030301020200010c0200000000ec090000000000",
        "0177ad707e7d45e67baee7890303155126e2affffbf498e95cc582dc5e81d6c271feb3e66d8c3ff73f4801c0bea260fed7ff7887b3185657e679d61b377afadd0701000103ea4a6c63e29c520abef5507b132ec5f9954776aebebe7b92421eea691446d22cfd1724385aa0c75b64fb78cd602fa1d991fdebf76b13c58ed702eac835e9f6180000000000000000000000000000000000000000000000000000000000000000030303030303030303030303030303030303030303030303030303030303030301020200010c0200000000ec090000000000",
    ),
    (
        1_000_000_000,
        "01000103ea4a6c63e29c520abef5507b132ec5f9954776aebebe7b92421eea691446d22cfd1724385aa0c75b64fb78cd602fa1d991fdebf76b13c58ed702eac835e9f6180000000000000000000000000000000000000000000000000000000000000000030303030303030303030303030303030303030303030303030303030303030301020200010c0200000000ca9a3b00000000",
        "0113633a5706af9e83635bdb0846ea8355a90e0c6ae591513cb5c2ed2db8ec00a5cfb22c0be29524ccc87ef0b0f4079ee10cb95484bade7c84c0e256df955b860e01000103ea4a6c63e29c520abef5507b132ec5f9954776aebebe7b92421eea691446d22cfd1724385aa0c75b64fb78cd602fa1d991fdebf76b13c58ed702eac835e9f6180000000000000000000000000000000000000000000000000000000000000000030303030303030303030303030303030303030303030303030303030303030301020200010c0200000000ca9a3b00000000",
    ),
    (
        0xFFFFFFFFFFFFFFFF,
        "01000103ea4a6c63e29c520abef5507b132ec5f9954776aebebe7b92421eea691446d22cfd1724385aa0c75b64fb78cd602fa1d991fdebf76b13c58ed702eac835e9f6180000000000000000000000000000000000000000000000000000000000000000030303030303030303030303030303030303030303030303030303030303030301020200010c02000000ffffffffffffffff",
        "0156485d60a11bb4c48b9159c118a4e46aca0138bc3e033230bd762ddae90814da053a38359553dbcc03ffedcb1f392f2963239efa88f3e4fed4a13f04bdfdc00601000103ea4a6c63e29c520abef5507b132ec5f9954776aebebe7b92421eea691446d22cfd1724385aa0c75b64fb78cd602fa1d991fdebf76b13c58ed702eac835e9f6180000000000000000000000000000000000000000000000000000000000000000030303030303030303030303030303030303030303030303030303030303030301020200010c02000000ffffffffffffffff",
    ),
)

#: THE ONLY MULTI-BYTE SHORTVEC A REAL TRANSACTION CAN CARRY, and where this
#: pair came from. 130 transfer instructions would need 2,344 bytes and web3.js
#: refuses to serialize past Solana's 1,232-byte packet limit (measured --
#: ERR_OUT_OF_RANGE at offset 1232), so an array COUNT can never need two
#: bytes. An instruction's DATA length can: a reference transaction carrying 200
#: bytes of instruction data had `c801` at the data-length offset, with the
#: first 0xab data byte immediately after it.
TWO_BYTE_LENGTH_VALUE = 200
TWO_BYTE_LENGTH_BYTES = bytes.fromhex("c801")

#: Offsets into a signed native transfer, derived from the layout rather than
#: counted by hand, so a mutation test names the field it is corrupting.
SIGNATURE_AT = 1
MESSAGE_AT = SIGNATURE_AT + SIGNATURE_BYTES
HEADER_AT = MESSAGE_AT
KEY_COUNT_AT = HEADER_AT + 3
KEYS_AT = KEY_COUNT_AT + 1
BLOCKHASH_AT = KEYS_AT + 3 * 32
INSTRUCTION_COUNT_AT = BLOCKHASH_AT + BLOCKHASH_BYTES
PROGRAM_INDEX_AT = INSTRUCTION_COUNT_AT + 1
ACCOUNT_COUNT_AT = PROGRAM_INDEX_AT + 1
ACCOUNT_INDEXES_AT = ACCOUNT_COUNT_AT + 1
DATA_LENGTH_AT = ACCOUNT_INDEXES_AT + 2
DATA_AT = DATA_LENGTH_AT + 1


def payer_key():
    return nacl_signing.SigningKey(PAYER_SEED)


def address_of(key) -> str:
    return base58.b58encode(bytes(key.verify_key)).decode("ascii")


def signed(lamports: int) -> bytes:
    """A real signed transfer for the pinned seeds and blockhash."""
    key = payer_key()
    message = compile_transfer_message(address_of(key), REFERENCE_DESTINATION, lamports, BLOCKHASH)
    return wire_transaction(key.sign(message).signature, message)


def corrupted(lamports: int, offset: int, replacement: bytes) -> bytes:
    """A signed transfer with `replacement` written over `offset`.

    Used to point the parser at a transaction it must refuse. The signature is
    left as it was on purpose: the parser's job is to notice that the STRUCTURE
    is not the one this code builds, and it must do that without needing a
    cryptographic check first.
    """
    raw = bytearray(signed(lamports))
    raw[offset : offset + len(replacement)] = replacement
    return bytes(raw)


# --- the keys, which are a fixture AND a cross-check -------------------------


def test_pynacl_derives_the_same_public_keys_the_reference_library_did():
    """One seed, two ed25519 implementations, the same address.

    If this fails, nothing else in this file means anything: every reference
    vector was signed by a key derived from these seeds, so a derivation that
    disagreed would make every byte comparison below a comparison of two
    different transactions.
    """
    assert address_of(nacl_signing.SigningKey(PAYER_SEED)) == REFERENCE_PAYER
    assert address_of(nacl_signing.SigningKey(DESTINATION_SEED)) == REFERENCE_DESTINATION


def test_the_system_program_address_is_thirty_two_zero_bytes():
    """Asserted rather than commented, because the constant is a literal.

    `11111111111111111111111111111111` is what an explorer shows and what every
    other Solana client ships; it has to be 32 zero bytes or the instruction
    names a program that does not exist.
    """
    assert base58.b58decode(SYSTEM_PROGRAM_ID) == bytes(32)


# --- the byte layout, against the reference implementation -------------------


@pytest.mark.parametrize(("lamports", "reference_message", "reference_wire"), REFERENCE_VECTORS)
def test_the_message_is_byte_identical_to_what_solana_web3js_serialized(lamports, reference_message, reference_wire):
    """The message -- header, key order, blockhash, instruction -- byte for byte.

    This is the test that would fail for every mutation of the layout: a wrong
    header byte, the payer and destination swapped, a u8 instruction
    discriminant instead of a u32, big-endian lamports, a missing shortvec. Each
    of those produces a transaction that is accepted by nothing, or worse,
    accepted and read as something else.
    """
    message = compile_transfer_message(REFERENCE_PAYER, REFERENCE_DESTINATION, lamports, BLOCKHASH)
    assert message.hex() == reference_message
    assert len(message) == TRANSFER_MESSAGE_BYTES == 150


@pytest.mark.parametrize(("lamports", "reference_message", "reference_wire"), REFERENCE_VECTORS)
def test_the_signed_wire_transaction_is_byte_identical_too(lamports, reference_message, reference_wire):
    """Including the SIGNATURE, which is a second fact worth having.

    ed25519 is deterministic: one key over one message produces one signature.
    So PyNaCl and tweetnacl agreeing on 64 bytes means the message bytes are
    identical AND the key derivation is, and a signature in the wrong position
    in the array would show up here too.
    """
    assert signed(lamports).hex() == reference_wire
    assert len(signed(lamports)) == TRANSFER_WIRE_BYTES == 215


def test_the_signature_verifies_against_the_payer_over_the_parsed_message():
    """The check that does not need a reference library at all.

    The parser hands back the message bytes the signature covers, so this
    verifies the artifact against the public key the transaction names as its
    fee payer -- which is the property a cluster checks.
    """
    wire = signed(5_000)
    parsed = parse_transfer_transaction(wire)
    verify = nacl_signing.VerifyKey(base58.b58decode(parsed["payer"]))
    verify.verify(parsed["message"], base58.b58decode(parsed["signature"]))


def test_the_parse_returns_the_fields_the_payout_path_compares_against_its_plan():
    wire = signed(650_240)
    parsed = parse_transfer_transaction(wire)
    assert parsed["payer"] == REFERENCE_PAYER
    assert parsed["destination"] == REFERENCE_DESTINATION
    assert parsed["lamports"] == 650_240
    assert parsed["recent_blockhash"] == BLOCKHASH
    assert parsed["program"] == SYSTEM_PROGRAM_ID


# --- compact-u16 -------------------------------------------------------------


def test_every_length_a_native_transfer_writes_is_one_byte():
    """Measured, not assumed, and it is why the two-byte branch needs a reference.

    Three keys, one instruction, two account indexes, twelve bytes of data: the
    production path never produces a multi-byte shortvec, so its own output
    cannot test one.
    """
    for value in (1, 2, 3, 12, 127):
        assert len(encode_compact_u16(value)) == 1
        assert encode_compact_u16(value) == bytes([value])


def test_the_two_byte_case_matches_the_reference_transaction_both_ways():
    """200 encodes to `c801`, and `c801` decodes to 200.

    The bytes are web3.js's, taken from a transaction carrying 200 bytes of
    instruction data -- the only multi-byte array length Solana's packet limit
    leaves reachable. Both directions are asserted because the decoder is what
    reads a real transaction and the encoder is what writes one.
    """
    assert encode_compact_u16(TWO_BYTE_LENGTH_VALUE) == TWO_BYTE_LENGTH_BYTES
    assert decode_compact_u16(TWO_BYTE_LENGTH_BYTES, 0) == (TWO_BYTE_LENGTH_VALUE, 2)


def test_the_boundary_is_at_128_not_at_256():
    """Seven bits per byte, so the second byte starts one short of a byte boundary."""
    assert encode_compact_u16(127) == bytes([0x7F])
    assert len(encode_compact_u16(128)) == 2
    assert encode_compact_u16(16_383) == bytes([0xFF, 0x7F])
    assert len(encode_compact_u16(16_384)) == 3


def test_encode_and_decode_round_trip_across_the_whole_u16_range():
    for value in (0, 1, 127, 128, 255, 256, 16_383, 16_384, 65_535):
        assert decode_compact_u16(encode_compact_u16(value), 0) == (value, len(encode_compact_u16(value)))


def test_a_length_outside_the_u16_range_is_refused_rather_than_truncated():
    for wrong in (-1, 65_536, 1 << 20):
        with pytest.raises(SolanaTransactionError, match="compact-u16"):
            encode_compact_u16(wrong)


def test_a_truncated_length_prefix_refuses_instead_of_returning_a_partial_number():
    with pytest.raises(SolanaTransactionError, match="runs past the end"):
        decode_compact_u16(bytes([0x80]), 0)


def test_a_length_prefix_that_never_terminates_is_refused():
    with pytest.raises(SolanaTransactionError, match="did not terminate"):
        decode_compact_u16(bytes([0x80, 0x80, 0x80, 0x80]), 0)


# --- the instruction data ----------------------------------------------------


def test_the_instruction_data_is_a_u32_discriminant_then_a_u64_amount_little_endian():
    data = transfer_instruction_data(1)
    assert len(data) == 12
    assert data[:4] == bytes([2, 0, 0, 0]), "the discriminant is a u32, and 2 is Transfer"
    assert data[4:] == bytes([1, 0, 0, 0, 0, 0, 0, 0]), "one lamport, little-endian"
    assert int.from_bytes(data[:4], "little") == SYSTEM_TRANSFER_INSTRUCTION


def test_a_zero_or_negative_amount_is_refused_before_anything_is_laid_out():
    """A zero-lamport transfer is a valid transaction that pays a fee and delivers nothing."""
    for wrong in (0, -1):
        with pytest.raises(SolanaTransactionError, match="nothing to send"):
            transfer_instruction_data(wrong)


def test_an_amount_past_a_u64_is_refused_rather_than_wrapped():
    with pytest.raises(SolanaTransactionError, match="exceeds a u64"):
        transfer_instruction_data(1 << 64)


# --- the message's own refusals ----------------------------------------------


def test_a_self_transfer_is_refused_because_the_key_list_would_de_duplicate():
    with pytest.raises(SolanaTransactionError, match="same account"):
        compile_transfer_message(REFERENCE_PAYER, REFERENCE_PAYER, 1, BLOCKHASH)


def test_a_blockhash_of_the_wrong_length_is_refused():
    short = base58.b58encode(bytes(31)).decode("ascii")
    with pytest.raises(SolanaTransactionError, match="recent blockhash is 32 bytes"):
        compile_transfer_message(REFERENCE_PAYER, REFERENCE_DESTINATION, 1, short)


def test_a_signature_of_the_wrong_length_never_reaches_a_wire_transaction():
    message = compile_transfer_message(REFERENCE_PAYER, REFERENCE_DESTINATION, 1, BLOCKHASH)
    with pytest.raises(SolanaTransactionError, match="ed25519 signature is 64 bytes"):
        wire_transaction(bytes(63), message)


# --- the parser's refusals, by corrupting a real transaction ------------------


def test_a_changed_header_is_refused_and_the_message_names_the_right_one():
    with pytest.raises(SolanaTransactionError, match=r"\(1, 0, 1\)"):
        parse_transfer_transaction(corrupted(1, HEADER_AT, bytes([1, 0, 0])))


def test_a_program_index_pointing_at_the_payer_is_refused():
    """Index 0 is the payer, not the system program. The bytes still parse."""
    with pytest.raises(SolanaTransactionError, match="not the system program"):
        parse_transfer_transaction(corrupted(1, PROGRAM_INDEX_AT, bytes([0])))


def test_reversed_account_indexes_are_refused_because_that_debits_the_recipient():
    with pytest.raises(SolanaTransactionError, match=r"touches accounts \[1, 0\]"):
        parse_transfer_transaction(corrupted(1, ACCOUNT_INDEXES_AT, bytes([1, 0])))


def test_a_different_system_instruction_over_the_same_accounts_is_refused():
    """Discriminant 1 is Assign, which changes an account's owner rather than paying it."""
    with pytest.raises(SolanaTransactionError, match="discriminant is 1"):
        parse_transfer_transaction(corrupted(1, DATA_AT, bytes([1, 0, 0, 0])))


def test_a_second_instruction_is_refused_rather_than_ignored():
    """Every instruction in a transaction executes, so an extra one moves funds nothing described."""
    with pytest.raises(SolanaTransactionError, match="carries 2 instructions"):
        parse_transfer_transaction(corrupted(1, INSTRUCTION_COUNT_AT, bytes([2])))


def test_trailing_bytes_are_refused():
    with pytest.raises(SolanaTransactionError, match="trailing bytes"):
        parse_transfer_transaction(signed(1) + b"\x00")


def test_a_truncated_transaction_is_refused_at_whichever_field_runs_out():
    wire = signed(1)
    for cut, where in ((40, "signature"), (70, "account keys"), (180, "recent blockhash"), (210, "instruction data")):
        with pytest.raises(SolanaTransactionError, match=where):
            parse_transfer_transaction(wire[:cut])


def test_two_signatures_are_refused():
    raw = bytearray(signed(1))
    raw[0:1] = bytes([2])
    with pytest.raises(SolanaTransactionError, match="carries 2 signatures"):
        parse_transfer_transaction(bytes(raw))


def test_something_that_is_not_bytes_at_all_is_refused_by_type():
    with pytest.raises(SolanaTransactionError, match="wire transaction is bytes"):
        parse_transfer_transaction("01000103")
