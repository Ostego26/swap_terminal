"""The cross-curve DLEQ proof wire format: parse it, reject it, and say its exact length.

Role: function level (CLAUDE.md rule 10's bottom -- every name here is a pure
      function of its arguments; the only types are bytes and ints)
Reads: nothing. No socket, no file, no environment, no clock, no database.
Writes: nothing
Can move funds: no. Nothing here signs, derives an address, or reaches a chain.
      It is FUND-ADJACENT in rule 16's sense only by omission: a parser that
      accepts a proof it should have rejected hands the group arithmetic above
      it a structure that is not what the prover sent. It is the layer that
      makes "the bytes are well formed" a separate, testable question from
      "the proof is valid", which is the whole reason it is its own file.
Mainnet-safe: yes -- it cannot reach a network and it holds no key. That is a
      weaker statement than adaptor_ecdsa.py's, and deliberately so: this file
      makes no cryptographic claim at all. It does not verify anything. See
      "WHAT THIS DOES NOT DO".
Live-safe: yes (same reason).

WHAT THIS DOES NOT DO

It does NOT verify the proof. It does not touch a curve, does not decompress a
point, does not reduce a scalar, and cannot tell a valid proof from a forgery
whose fields happen to be the right length. `parse_proof` returning a
`DleqProofBytes` means exactly one thing: the byte string has the shape
`go-dleq`'s `Proof.Serialize()` produces, consumed to the last byte with nothing
left over. Verification is the caller's, and it needs the ed25519 and secp256k1
group law that does not exist in this tree yet.

Saying that plainly is the point. The design doc (docs/dleq_cross_curve_design.md)
lists nine tamper cases, and cases 7 (truncate by one byte, extend by one byte)
and 8/9 (non-canonical encodings) fail at two DIFFERENT layers. 7 is this file
and costs a length comparison. 8 and 9 are the group layer and cost 252 point
decompressions. Conflating them is how a verifier ends up doing 64 KiB of
arithmetic to discover a short read.

THE LAYOUT, READ OUT OF THE ENCODER AND RECONCILED AGAINST THREE CAPTURED PROOFS

`go-dleq`'s serde.go at d6fd7c03 (`Proof.Serialize` and `bitProof.encode`):

    offset            field                                       bytes
    0                 CommitmentA   compressed secp256k1 point        33
    33                CommitmentB   compressed ed25519 point          32
    65                bit count     one byte, 252 in every real proof  1
    66                252 x bitProof, each:                       64,764
                        commitmentA  compressed secp256k1 point      33
                        commitmentB  compressed ed25519 point        32
                        eCurveA, eCurveB, a0, a1, b0, b1
                                     six 32-byte scalars           192
    64,830            len(signatureA)  one byte                       1
    64,831            signatureA                               70 to 72
    ...               len(signatureB)  one byte                       1
    ...               signatureB                                     64
                                                       TOTAL  64,966-64,968

**THE TOTAL IS NOT A CONSTANT, AND THE DESIGN DOC SAID IT WAS.** That section
computed 64,960 from "two 64-byte signatures with length prefixes = 130", which
is wrong on both halves: `signatureA` is a **DER-encoded** ECDSA signature over
secp256k1, so it is 70, 71 or 72 bytes depending on whether r and s each need a
leading 0x00 to stay positive, and only `signatureB` (ed25519) is a fixed 64.
The one-byte length prefixes in the format exist for exactly that reason -- an
encoder writing two fixed-width signatures would not need them.

This was not deduced. The three vectors in tests/vectors/dleq_cross_curve_go.json
were captured from the Go implementation on 2026-09-27 and measured 64,967,
64,968 and 64,966 bytes. Parsed by this file they consume every byte with zero
trailing, and their `signatureA` lengths are 71, 72 and 70 -- each one starting
0x30, DER's SEQUENCE tag, with an inner length field two less than the total.
The spread in the three totals IS the DER spread; nothing else in the format
varies. A parser written to the doc's 64,960 would have rejected all three.

WHY A 252 BIT COUNT IS A HARD CHECK AND NOT A HINT

`Deserialize` reads the count from the wire and trusts it, which is correct for
a decoder that reports its own errors, and wrong for this tree. 252 is
`min(ed25519.BitSize(), secp256k1.BitSize())` and it is the number of bits the
witness is sampled over; a proof carrying any other count is either from a
different implementation or is a proof about a witness this protocol never
generates. `sigma_fun` hard-codes the same constant
(`COMMITMENT_BITS: usize = 252`) and checks it in `verify`. So do we, and the
check lives here rather than in the verifier because it decides how many bytes
to read -- a wrong count is a framing error before it is a cryptographic one.

WHAT IS STILL MISSING, STATED AS RULE 16 AND RULE 17 REQUIRE

NOTHING IN THIS TREE CALLS THIS FILE. Established by grepping every .py, .js,
.sh, .md, .toml and .txt file outside node_modules for `dleq_proof_format`,
`parse_proof` and `DleqProofBytes` -- the NAME grep rule 2 asks for, not an
import-graph walk -- and the only hits are this file and its test. It is a
PROPOSAL in rule 16's sense: the property that matters for a GRC<->XMR swap,
that a proof generated by our prover is accepted by go-dleq's verifier and vice
versa, cannot be tested until the prover exists. What IS tested here is the
narrower and fully checkable claim: three proofs a different implementation
produced parse to the structure its own encoder documents, byte for byte.
"""

from __future__ import annotations

from dataclasses import dataclass

# go-dleq's curveA is secp256k1 and curveB is ed25519, and the asymmetry in the
# two point sizes is the whole reason the offsets above are not multiples of 32.
# 33 is SEC1 compressed (a 0x02/0x03 parity prefix plus a 32-byte x); 32 is
# ed25519's y with the x parity folded into its top bit and so no prefix at all.
SECP256K1_POINT_BYTES = 33
ED25519_POINT_BYTES = 32

# `const scalarLen = 32` in serde.go, applied to BOTH curves. That is a
# coincidence of these two curves rather than a property of the format -- the
# encoder's own comment says "WARN: this assumes the groups have an encoded
# scalar length of 32!" -- so it is named once here and not spelled per field.
SCALAR_BYTES = 32

# Six scalars per bit proof: the two ring-signature challenges (one per curve)
# and the four responses (a0, a1 on curveA; b0, b1 on curveB).
SCALARS_PER_BIT_PROOF = 6

# min(ed25519.BitSize()=252, secp256k1.BitSize()=255). See the module docstring:
# this is a hard requirement, not a hint read off the wire.
WITNESS_BITS = 252

BIT_PROOF_BYTES = (
    SECP256K1_POINT_BYTES
    + ED25519_POINT_BYTES
    + SCALARS_PER_BIT_PROOF * SCALAR_BYTES
)

# Everything up to and including the first signature's length prefix is fixed
# width; the two signatures are not. Kept as a named constant because the tests
# assert against it directly and because it is the number the design doc got
# wrong by treating the signatures as fixed too.
FIXED_PREFIX_BYTES = (
    SECP256K1_POINT_BYTES + ED25519_POINT_BYTES + 1 + WITNESS_BITS * BIT_PROOF_BYTES
)

# The ed25519 signature is 64 bytes always (R || s, both 32). The secp256k1 one
# is DER and is 70, 71 or 72 in practice -- 0x30, a length byte, then two
# INTEGERs of 32 or 33 bytes each with two bytes of tag and length apiece. The
# bounds here are deliberately wider than 70-72: a DER integer may also be
# SHORTER than 32 bytes when its leading bytes are zero, which happens with
# probability about 2^-8 per component and so will be seen eventually rather
# than never. Rejecting a legitimate 69-byte signature because three captured
# proofs did not contain one would be exactly the "fixtures narrower than
# reality" failure this repo keeps finding.
ED25519_SIGNATURE_BYTES = 64
SECP256K1_DER_SIGNATURE_MIN_BYTES = 8
SECP256K1_DER_SIGNATURE_MAX_BYTES = 72


@dataclass(frozen=True)
class BitProofBytes:
    """One bit's commitments and ring signature, still as bytes. Nothing is decoded."""

    commitment_a: bytes
    commitment_b: bytes
    e_curve_a: bytes
    e_curve_b: bytes
    a0: bytes
    a1: bytes
    b0: bytes
    b1: bytes


@dataclass(frozen=True)
class DleqProofBytes:
    """A parsed proof. Every field is raw bytes; none of them has been validated as a
    point or a scalar, and `parse_proof`'s docstring says so again because the
    distinction is the one thing a caller can get wrong here."""

    commitment_a: bytes
    commitment_b: bytes
    bit_proofs: tuple[BitProofBytes, ...]
    signature_a: bytes
    signature_b: bytes

    def serialized_length(self) -> int:
        """The length the proof this came from had. Not a constant -- see the module
        docstring on why the design doc's 64,960 was wrong."""
        return FIXED_PREFIX_BYTES + 1 + len(self.signature_a) + 1 + len(self.signature_b)


class DleqFormatError(ValueError):
    """A framing failure: wrong length, wrong bit count, trailing bytes.

    Deliberately NOT a subclass of anything the group layer raises. A caller
    that sees this knows the bytes never reached the arithmetic, which is a
    different thing to report to a counterparty than "your proof is invalid" --
    and telling them apart is the reason parse and verify are separate files.
    """


def _take(blob: bytes, offset: int, count: int, field: str) -> tuple[bytes, int]:
    """Read `count` bytes, or refuse and say which field ran out and by how much.

    `bytes.Buffer.Next` in Go returns a SHORT slice at the end of the buffer
    rather than failing, which is why `Deserialize` has to pre-check its lengths
    in three places and still cannot pre-check the per-bit reads. Refusing here
    means every read in this file is total: it either returns `count` bytes or
    raises, and no caller below has to length-check its own slice.
    """
    end = offset + count
    if end > len(blob):
        raise DleqFormatError(
            f"{field}: needs {count} bytes at offset {offset}, but the proof is "
            f"{len(blob)} bytes -- {end - len(blob)} short"
        )
    return blob[offset:end], end


def _parse_bit_proof(blob: bytes, offset: int, index: int) -> tuple[BitProofBytes, int]:
    """One bit proof, in `bitProof.encode`'s order. The order is not alphabetical and
    not grouped by curve: commitments first, then eCurveA, eCurveB, a0, a1, b0, b1."""
    commitment_a, offset = _take(blob, offset, SECP256K1_POINT_BYTES, f"bit[{index}].commitmentA")
    commitment_b, offset = _take(blob, offset, ED25519_POINT_BYTES, f"bit[{index}].commitmentB")
    e_curve_a, offset = _take(blob, offset, SCALAR_BYTES, f"bit[{index}].eCurveA")
    e_curve_b, offset = _take(blob, offset, SCALAR_BYTES, f"bit[{index}].eCurveB")
    a0, offset = _take(blob, offset, SCALAR_BYTES, f"bit[{index}].a0")
    a1, offset = _take(blob, offset, SCALAR_BYTES, f"bit[{index}].a1")
    b0, offset = _take(blob, offset, SCALAR_BYTES, f"bit[{index}].b0")
    b1, offset = _take(blob, offset, SCALAR_BYTES, f"bit[{index}].b1")
    return (
        BitProofBytes(
            commitment_a=commitment_a,
            commitment_b=commitment_b,
            e_curve_a=e_curve_a,
            e_curve_b=e_curve_b,
            a0=a0,
            a1=a1,
            b0=b0,
            b1=b1,
        ),
        offset,
    )


def parse_proof(blob: bytes) -> DleqProofBytes:
    """Frame a serialized cross-curve DLEQ proof. This does NOT verify it.

    Raises DleqFormatError and never anything else for malformed input: a short
    read, a bit count that is not 252, a signature length outside its encoding's
    possible range, or a single trailing byte. Returning normally means the byte
    string is framed exactly as go-dleq's encoder frames one, and says nothing
    whatever about whether the points are on their curves, whether the scalars
    are reduced, or whether the proof is true.

    The trailing-byte check is not pedantry. `Deserialize` stops at the last
    field it needs and ignores anything after it, so a proof with a byte appended
    round-trips through Go unchanged and through a length-comparing peer as a
    mismatch -- which is the design doc's tamper case 7 in its "extend by one
    byte" direction, and the direction that a decoder without this check passes.
    """
    if not isinstance(blob, bytes | bytearray):
        raise DleqFormatError(f"proof must be bytes, got {type(blob).__name__}")
    blob = bytes(blob)

    offset = 0
    commitment_a, offset = _take(blob, offset, SECP256K1_POINT_BYTES, "CommitmentA")
    commitment_b, offset = _take(blob, offset, ED25519_POINT_BYTES, "CommitmentB")

    count_byte, offset = _take(blob, offset, 1, "bit count")
    bit_count = count_byte[0]
    if bit_count != WITNESS_BITS:
        raise DleqFormatError(
            f"bit count is {bit_count}, and this protocol only generates and only "
            f"accepts {WITNESS_BITS} -- min(ed25519 252, secp256k1 255). A proof "
            f"with another count is about a witness sampled over a different range"
        )

    bit_proofs = []
    for index in range(bit_count):
        bit_proof, offset = _parse_bit_proof(blob, offset, index)
        bit_proofs.append(bit_proof)

    signature_a, offset = _take_signature(
        blob,
        offset,
        "signatureA",
        SECP256K1_DER_SIGNATURE_MIN_BYTES,
        SECP256K1_DER_SIGNATURE_MAX_BYTES,
    )
    signature_b, offset = _take_signature(
        blob, offset, "signatureB", ED25519_SIGNATURE_BYTES, ED25519_SIGNATURE_BYTES
    )

    if offset != len(blob):
        raise DleqFormatError(
            f"{len(blob) - offset} trailing byte(s) after signatureB: the proof is "
            f"{len(blob)} bytes and its fields account for {offset}"
        )

    return DleqProofBytes(
        commitment_a=commitment_a,
        commitment_b=commitment_b,
        bit_proofs=tuple(bit_proofs),
        signature_a=signature_a,
        signature_b=signature_b,
    )


def _take_signature(
    blob: bytes, offset: int, field: str, minimum: int, maximum: int
) -> tuple[bytes, int]:
    """A one-byte length prefix and that many bytes, with the length range checked.

    The range check is what turns a corrupted length byte into a named refusal
    instead of a short read forty thousand bytes earlier in the message. Both
    bounds are inclusive and for ed25519 they are the same number, which is the
    honest way to say "fixed width" in a format that still spends a byte saying
    it.
    """
    length_byte, offset = _take(blob, offset, 1, f"len({field})")
    length = length_byte[0]
    if not minimum <= length <= maximum:
        expected = f"{minimum}" if minimum == maximum else f"{minimum} to {maximum}"
        raise DleqFormatError(
            f"len({field}) is {length}, outside the {expected} bytes that encoding "
            f"can produce"
        )
    return _take(blob, offset, length, field)
