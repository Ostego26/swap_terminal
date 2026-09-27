"""The DLEQ wire parser, against three proofs a DIFFERENT implementation produced.

Role: test (read-only)
Reads: tests/vectors/dleq_cross_curve_go.json
Writes: nothing
Can move funds: no
Mainnet-safe: yes
Live-safe: yes

WHY THE VECTORS MATTER MORE THAN THE ASSERTIONS

A parser tested only against bytes the same session invented is a test that the
author's two readings of a format agree, which they will. These three proofs came
out of `athanorlabs/go-dleq` at d6fd7c03, built and run on 2026-09-27, and their
lengths (64,967 / 64,968 / 64,966) are the measurement that refuted the design
doc's computed 64,960. So the load-bearing test here is `test_captured_vectors`:
every byte consumed, nothing trailing, on all three.
"""

from __future__ import annotations

import json
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "swap_terminal"))

from modules.dleq_proof_format import (
    BIT_PROOF_BYTES,
    ED25519_SIGNATURE_BYTES,
    FIXED_PREFIX_BYTES,
    WITNESS_BITS,
    DleqFormatError,
    parse_proof,
)

VECTOR_PATH = pathlib.Path(__file__).resolve().parents[1] / "tests" / "vectors" / "dleq_cross_curve_go.json"


def _vectors() -> list[dict]:
    with VECTOR_PATH.open() as handle:
        return json.load(handle)


def _first_proof_bytes() -> bytes:
    return bytes.fromhex(_vectors()[0]["proof_hex"])


def test_layout_constants_are_derived_not_transcribed():
    """257 per bit and 64,830 fixed. If either drifts, every offset below is wrong."""
    assert BIT_PROOF_BYTES == 33 + 32 + 6 * 32 == 257
    assert FIXED_PREFIX_BYTES == 33 + 32 + 1 + 252 * 257 == 64830


def test_the_vector_file_has_the_three_captured_proofs():
    """Rule 14: an empty fixture must not read as a passing test."""
    vectors = _vectors()
    assert len(vectors) == 3, f"expected 3 captured proofs, found {len(vectors)}"
    assert {v["label"] for v in vectors} == {"one", "counting_1_to_31", "just_under_2_252"}


@pytest.mark.parametrize("vector", _vectors(), ids=lambda v: v["label"])
def test_captured_vectors(vector):
    """Every byte of a real go-dleq proof is accounted for, and the commitments in the
    header are the claimed keys the capture recorded separately."""
    blob = bytes.fromhex(vector["proof_hex"])
    assert len(blob) == vector["proof_len_bytes"]

    proof = parse_proof(blob)

    assert proof.commitment_a.hex() == vector["secp256k1_point"]
    assert proof.commitment_b.hex() == vector["ed25519_point"]
    assert len(proof.bit_proofs) == WITNESS_BITS
    assert proof.serialized_length() == len(blob)
    assert len(proof.signature_b) == ED25519_SIGNATURE_BYTES


def test_the_secp256k1_signature_is_der_and_its_length_varies():
    """This is the assertion the design doc's 64,960 would have failed.

    The three totals differ only because signatureA is DER: 71, 72 and 70 bytes,
    each starting 0x30 with an inner length two less than the whole. A format
    with two fixed 64-byte signatures could not produce three different totals.
    """
    lengths = {}
    for vector in _vectors():
        proof = parse_proof(bytes.fromhex(vector["proof_hex"]))
        assert proof.signature_a[0] == 0x30, "DER SEQUENCE tag"
        assert proof.signature_a[1] == len(proof.signature_a) - 2, "DER inner length"
        lengths[vector["label"]] = len(proof.signature_a)

    assert lengths == {"one": 71, "counting_1_to_31": 72, "just_under_2_252": 70}
    assert len({v["proof_len_bytes"] for v in _vectors()}) == 3, (
        "three distinct totals is the evidence that the length is not a constant"
    )


def test_one_appended_byte_is_refused():
    """Design doc tamper case 7, the direction go-dleq's own Deserialize accepts."""
    with pytest.raises(DleqFormatError, match="trailing byte"):
        parse_proof(_first_proof_bytes() + b"\x00")


def test_one_truncated_byte_is_refused_and_names_the_field():
    """The other direction of case 7. The message has to name signatureB, not just
    fail: a short read at offset 64,903 is unreadable without the field name."""
    with pytest.raises(DleqFormatError, match="signatureB"):
        parse_proof(_first_proof_bytes()[:-1])


def test_empty_input_is_refused():
    with pytest.raises(DleqFormatError, match="CommitmentA"):
        parse_proof(b"")


def test_a_bit_count_other_than_252_is_refused():
    """251 would frame cleanly and leave 257 trailing bytes; 253 would run off the
    end. Both are refused for the same stated reason -- the count itself -- so the
    error does not depend on which side of 252 the corruption fell."""
    blob = bytearray(_first_proof_bytes())
    for wrong in (0, 251, 253, 255):
        blob[65] = wrong
        with pytest.raises(DleqFormatError, match="bit count is"):
            parse_proof(bytes(blob))


def test_a_corrupted_signature_length_is_named_rather_than_read_short():
    """A single wrong length byte must not surface as a mystery short read."""
    blob = bytearray(_first_proof_bytes())
    blob[FIXED_PREFIX_BYTES] = 200
    with pytest.raises(DleqFormatError, match=r"len\(signatureA\) is 200"):
        parse_proof(bytes(blob))


def test_a_65_byte_ed25519_signature_length_is_refused():
    """ed25519 is fixed width, so 65 is not a wide DER cousin -- it is corruption."""
    blob = bytearray(_first_proof_bytes())
    sig_a_length = blob[FIXED_PREFIX_BYTES]
    offset = FIXED_PREFIX_BYTES + 1 + sig_a_length
    assert blob[offset] == ED25519_SIGNATURE_BYTES, "located len(signatureB)"
    blob[offset] = 65
    with pytest.raises(DleqFormatError, match=r"len\(signatureB\) is 65"):
        parse_proof(bytes(blob))


def test_bytearray_and_memoryview_callers_get_a_named_refusal_or_work():
    """A bytearray must parse (transports hand one over); a str must not."""
    assert parse_proof(bytearray(_first_proof_bytes())).serialized_length() == 64967
    with pytest.raises(DleqFormatError, match="must be bytes, got str"):
        parse_proof(_vectors()[0]["proof_hex"])


def test_parsing_is_field_exact_not_just_length_exact():
    """Reassembling the parsed fields in encode() order must reproduce the input byte
    for byte. A parser with two offsets transposed passes every length assertion
    above and fails this one."""
    blob = _first_proof_bytes()
    proof = parse_proof(blob)

    rebuilt = bytearray(proof.commitment_a + proof.commitment_b + bytes([WITNESS_BITS]))
    for bit in proof.bit_proofs:
        rebuilt += (
            bit.commitment_a
            + bit.commitment_b
            + bit.e_curve_a
            + bit.e_curve_b
            + bit.a0
            + bit.a1
            + bit.b0
            + bit.b1
        )
    rebuilt += bytes([len(proof.signature_a)]) + proof.signature_a
    rebuilt += bytes([len(proof.signature_b)]) + proof.signature_b

    assert bytes(rebuilt) == blob
