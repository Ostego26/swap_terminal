"""ed25519 group arithmetic, against RFC 8032's published vectors, Go's edwards25519, and libsodium.

Role: test (pure functions; no chain, no socket, no database, no wallet)
Reads: swap_terminal/modules/ed25519_group.py, the five RFC 8032 section 7.1
      vectors embedded below, and tests/vectors/dleq_cross_curve_go.json
Writes: nothing
Can move funds: no
Mainnet-safe: yes -- nothing here can reach a chain, and the module under test
      cannot either. The module itself is NOT mainnet-safe, for reasons that
      have nothing to do with sockets; see its docstring.

WHY THESE PARTICULAR TESTS, AND WHAT EACH WOULD COST IF IT WERE MISSING.

modules/ed25519_group.py hand-writes cryptography, and cryptography that merely
looks right is worth nothing -- the argument tests/test_solana_address.py and
tests/test_adaptor_ecdsa.py both make. THREE of the four kinds of evidence here
are independent of the implementation, which is the only reason this file is
worth more than a round trip:

  INDEPENDENT, PUBLISHED   RFC 8032 section 7.1's five known-answer vectors, in
                           `RFC_8032_SECTION_7_1` below. These are the only real
                           KATs available for any of this
                           (docs/dleq_cross_curve_design.md section 6 stage 1
                           says so and is why this file exists). Source:
                           https://www.rfc-editor.org/rfc/rfc8032.txt section
                           7.1 "Test Vectors for Ed25519". THAT URL IS BLOCKED
                           FROM THIS CONTAINER (measured 2026-09-27: 403 CONNECT
                           tunnel failed, as are datatracker.ietf.org and
                           www.ietf.org), so the text was taken from two
                           unrelated GitHub mirrors of the RFC --
                           smuellerDD/leancrypto curve25519/doc/rfc8032.txt and
                           hacspec/hacspec-python
                           archive/formal-models/rfc-8032-eddsa/rfc8032.txt --
                           whose bytes are identical: 103,210 bytes each,
                           sha256 ed63657ff389301282b169b0abde9b5dd2c7e4d524fdfa5da6ff3094fc93c4c3.
                           Two unrelated repositories agreeing byte for byte is
                           not the canonical source and is the best available
                           from here; it is labeled rather than glossed.
                           The vectors were then EXTRACTED BY SCRIPT from that
                           text (page footers stripped, whitespace removed)
                           rather than retyped, because a hand-copied 1023-byte
                           message is a transcription bug waiting to be
                           diagnosed as a crypto bug.
  INDEPENDENT, IN-REPO     tests/vectors/dleq_cross_curve_go.json, produced by
                           Go's filippo.io/edwards25519. Its `one` vector's
                           ed25519 point is the compressed basepoint, and all
                           three are reproduced from their little-endian
                           witnesses.
  INDEPENDENT, RUNNABLE    libsodium, through `pynacl` 1.4.0's
                           `nacl.bindings.crypto_core_ed25519_*` and
                           `crypto_scalarmult_ed25519_*_noclamp`. This is the
                           oracle with the most inputs: point addition,
                           subtraction, fixed- and variable-base multiplication,
                           64-byte scalar reduction, scalar inversion, scalar
                           multiplication, and point validity. NOT A DEPENDENCY
                           OF THE MODULE -- the same pattern this repo already
                           accepted for `solders` in test_solana_address.py and
                           `cryptoconditions` in test_xrp_crypto_condition.py --
                           and every test using it skips when it is absent, so
                           this file still passes on a host that has only
                           `base58` and `ecdsa`.
  SELF-CONSISTENT          the property tests. These cannot catch a systematic
                           error (a wrong d would satisfy every one of them on a
                           different curve), which is why they are last in this
                           list and why the KATs are first.

WHAT IS NOT TESTED HERE, SAID PLAINLY.

  - That nobody knows log_B(H) for Monero's RingCT generator. That is the
    security property the Pedersen commitment rests on; no test can establish
    it, and no test below pretends to.
  - Constant-time behavior. The module declares it is not constant time. A
    timing test belongs with the DLEQ prover, where the secret-dependent branch
    lives (docs/dleq_cross_curve_design.md section 5.4 specifies that test); a
    timing assertion over this module would measure the container's noise, which
    was measured at +-0.00025µfn (+-0.3ms) on a 0.0017µfn (2ms) operation
    -- rule 6 governs a timing written in prose too -- and would make the test flaky
    rather than informative.
  - The DLEQ proof itself, which does not exist. This file is stage 1.

NO `random` ANYWHERE. Random-looking test data comes from SHA-512 of a label and
a counter, so a failure is reproducible from the test name and index alone with
no seed to remember -- the convention tests/test_adaptor_ecdsa.py already set.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "swap_terminal"))

# Imported after the sys.path insert above, which is CLAUDE.md rule 10's layout
# gap rather than a hazard. No `noqa: E402` here: tests/conftest.py already makes
# this import resolvable and ruff's E402 does not fire on it (measured
# 2026-09-27; test_adaptor_ecdsa.py records the same, including that RUF100
# reports a suppression here as unnecessary).
from chains.solana_address import FIELD_PRIME
from modules.ed25519_group import (
    BASEPOINT,
    COFACTOR,
    GROUP_ORDER,
    IDENTITY,
    POINT_BYTES,
    RINGCT_H,
    RINGCT_H_ENCODING,
    Ed25519Error,
    Point,
    has_small_order,
    is_canonical,
    is_canonical_scalar_le,
    is_torsion_free,
    point_scalar_mul,
    scalar_add,
    scalar_base_mul,
    scalar_from_bytes_le,
    scalar_inverse,
    scalar_mul,
    scalar_reduce,
    scalar_sub,
    scalar_to_bytes_le,
)

# libsodium is the runnable independent oracle. Absent on a host with only the
# two declared runtime dependencies, which is the deployment case, so every test
# that uses it skips rather than fails -- a test that fails because an ORACLE is
# missing tells the operator nothing about the code.
try:
    import nacl.bindings as _libsodium
except ImportError:  # pragma: no cover -- exercised only on a host without pynacl
    _libsodium = None

libsodium_required = pytest.mark.skipif(
    _libsodium is None,
    reason="pynacl is not installed; it is an oracle for these tests and deliberately NOT a dependency",
)

VECTORS_DIR = Path(__file__).resolve().parent / "vectors"


# --- RFC 8032 section 7.1, the only published known-answer vectors ------------
#
# Extracted by script from the RFC text named in the module docstring, not
# retyped. TEST 1024's message is 1023 bytes and is wrapped across lines as
# adjacent string literals; the wrapping is at 96 hex characters and carries no
# meaning.

RFC_8032_SECTION_7_1 = (
    {
        "name": "TEST 1",
        "secret_key": "9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60",
        "public_key": "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a",
        "message": "",
        "signature": (
            "e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e065224901555fb8821590a33bacc61e39701cf9b46b"
            "d25bf5f0595bbe24655141438e7a100b"
        ),
    },
    {
        "name": "TEST 2",
        "secret_key": "4ccd089b28ff96da9db6c346ec114e0f5b8a319f35aba624da8cf6ed4fb8a6fb",
        "public_key": "3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c",
        "message": "72",
        "signature": (
            "92a009a9f0d4cab8720e820b5f642540a2b27b5416503f8fb3762223ebdb69da085ac1e43e15996e458f3613d0f11d8c"
            "387b2eaeb4302aeeb00d291612bb0c00"
        ),
    },
    {
        "name": "TEST 3",
        "secret_key": "c5aa8df43f9f837bedb7442f31dcb7b166d38535076f094b85ce3a2e0b4458f7",
        "public_key": "fc51cd8e6218a1a38da47ed00230f0580816ed13ba3303ac5deb911548908025",
        "message": "af82",
        "signature": (
            "6291d657deec24024827e69c3abe01a30ce548a284743a445e3680d7db5ac3ac18ff9b538d16f290ae67f760984dc659"
            "4a7c15e9716ed28dc027beceea1ec40a"
        ),
    },
    {
        "name": "TEST 1024",
        "secret_key": "f5e5767cf153319517630f226876b86c8160cc583bc013744c6bf255f5cc0ee5",
        "public_key": "278117fc144c72340f67d0f2316e8386ceffbf2b2428c9c51fef7c597f1d426e",
        "message": (
            "08b8b2b733424243760fe426a4b54908632110a66c2f6591eabd3345e3e4eb98fa6e264bf09efe12ee50f8f54e9f77b1"
            "e355f6c50544e23fb1433ddf73be84d879de7c0046dc4996d9e773f4bc9efe5738829adb26c81b37c93a1b270b20329d"
            "658675fc6ea534e0810a4432826bf58c941efb65d57a338bbd2e26640f89ffbc1a858efcb8550ee3a5e1998bd177e93a"
            "7363c344fe6b199ee5d02e82d522c4feba15452f80288a821a579116ec6dad2b3b310da903401aa62100ab5d1a36553e"
            "06203b33890cc9b832f79ef80560ccb9a39ce767967ed628c6ad573cb116dbefefd75499da96bd68a8a97b928a8bbc10"
            "3b6621fcde2beca1231d206be6cd9ec7aff6f6c94fcd7204ed3455c68c83f4a41da4af2b74ef5c53f1d8ac70bdcb7ed1"
            "85ce81bd84359d44254d95629e9855a94a7c1958d1f8ada5d0532ed8a5aa3fb2d17ba70eb6248e594e1a2297acbbb39d"
            "502f1a8c6eb6f1ce22b3de1a1f40cc24554119a831a9aad6079cad88425de6bde1a9187ebb6092cf67bf2b13fd65f270"
            "88d78b7e883c8759d2c4f5c65adb7553878ad575f9fad878e80a0c9ba63bcbcc2732e69485bbc9c90bfbd62481d9089b"
            "eccf80cfe2df16a2cf65bd92dd597b0707e0917af48bbb75fed413d238f5555a7a569d80c3414a8d0859dc65a46128ba"
            "b27af87a71314f318c782b23ebfe808b82b0ce26401d2e22f04d83d1255dc51addd3b75a2b1ae0784504df543af8969b"
            "e3ea7082ff7fc9888c144da2af58429ec96031dbcad3dad9af0dcbaaaf268cb8fcffead94f3c7ca495e056a9b47acdb7"
            "51fb73e666c6c655ade8297297d07ad1ba5e43f1bca32301651339e22904cc8c42f58c30c04aafdb038dda0847dd988d"
            "cda6f3bfd15c4b4c4525004aa06eeff8ca61783aacec57fb3d1f92b0fe2fd1a85f6724517b65e614ad6808d6f6ee34df"
            "f7310fdc82aebfd904b01e1dc54b2927094b2db68d6f903b68401adebf5a7e08d78ff4ef5d63653a65040cf9bfd4aca7"
            "984a74d37145986780fc0b16ac451649de6188a7dbdf191f64b5fc5e2ab47b57f7f7276cd419c17a3ca8e1b939ae49e4"
            "88acba6b965610b5480109c8b17b80e1b7b750dfc7598d5d5011fd2dcc5600a32ef5b52a1ecc820e308aa342721aac09"
            "43bf6686b64b2579376504ccc493d97e6aed3fb0f9cd71a43dd497f01f17c0e2cb3797aa2a2f256656168e6c496afc5f"
            "b93246f6b1116398a346f1a641f3b041e989f7914f90cc2c7fff357876e506b50d334ba77c225bc307ba537152f3f161"
            "0e4eafe595f6d9d90d11faa933a15ef1369546868a7f3a45a96768d40fd9d03412c091c6315cf4fde7cb68606937380d"
            "b2eaaa707b4c4185c32eddcdd306705e4dc1ffc872eeee475a64dfac86aba41c0618983f8741c5ef68d3a101e8a3b8ca"
            "c60c905c15fc910840b94c00a0b9d0"
        ),
        "signature": (
            "0aab4c900501b3e24d7cdf4663326a3a87df5e4843b2cbdb67cbf6e460fec350aa5371b1508f9f4528ecea23c436d94b"
            "5e8fcd4f681e30a6ac00a9704a188a03"
        ),
    },
    {
        "name": "TEST SHA(abc)",
        "secret_key": "833fe62409237b9d62ec77587520911e9a759cec1d19755b7da901b96dca3d42",
        "public_key": "ec172b93ad5e563bf4932c70e1245034c35467ef2efd4d64ebf819683467e2bf",
        "message": (
            "ddaf35a193617abacc417349ae20413112e6fa4e89a97ea20a9eeee64b55d39a2192992a274fc1a836ba3c23a3feebbd"
            "454d4423643ce80e2a9ac94fa54ca49f"
        ),
        "signature": (
            "dc2a4459e7369633a52b1bf277839a00201009a3efbf3ecb69bea2186c26b58909351fc9ac90b3ecfdfbc7c66431e030"
            "3dca179c138ac17ad9bef1177331a704"
        ),
    },
)


# --- A test-only Ed25519, built from nothing but the module under test --------
#
# docs/dleq_cross_curve_design.md section 6 stage 1 specifies exactly this: "RFC
# 8032 section 7.1 signature vectors, consumed by a test-only verifier". The
# point is that signing and verifying together exercise every primitive the DLEQ
# needs -- decompression with x-recovery, the group law, fixed- and
# variable-base multiplication, and reduction of a 64-byte hash -- against
# numbers somebody else published.
#
# These two functions live HERE and not in the module, deliberately. The module
# is group arithmetic; an Ed25519 signer in it would be a second, unaudited
# signature implementation in a tree that has no caller for one, which rule 2
# would then ask to justify. They are test scaffolding and they say so.


def _ed25519_secret_scalar(seed: bytes) -> int:
    """RFC 8032 section 5.1.5's key derivation: SHA-512 the seed, clamp, read little-endian.

    The clamping (clear the low three bits, clear the top bit, set bit 254) is
    what makes the scalar a multiple of the cofactor and fixes its bit length.
    Written out here rather than hidden, because the low-three-bits clear is the
    step that makes every honest ed25519 public key torsion-free -- which is why
    `is_torsion_free` is never needed for a key that came from a real wallet, and
    always needed for one that came from a counterparty.
    """
    digest = hashlib.sha512(seed).digest()
    clamped = bytearray(digest[:32])
    clamped[0] &= 248
    clamped[31] &= 127
    clamped[31] |= 64
    return int.from_bytes(clamped, "little")


def _ed25519_verify(public_key: bytes, message: bytes, signature: bytes) -> bool:
    """RFC 8032 section 5.1.7 verification, in the strict (non-cofactored) form.

    Checks [S]B == R + [k]A with k = SHA512(R || A || M) reduced mod l, and
    refuses an S that is not below l. That last refusal is RFC 8032's own ("if
    the signature is not 64 bytes, or if any of the 32 bytes encoding ... is not
    of the form ... reject") and it is the reason `is_canonical_scalar_le` exists
    in the module: a verifier that reduced S instead would accept a second,
    malleated encoding of every signature.

    NON-COFACTORED on purpose. The cofactored form -- [8S]B == [8]R + [8k]A -- is
    what libsodium's batch verification and ZIP-215 use, and it accepts
    signatures this rejects. RFC 8032's five vectors pass under both, so this
    choice is invisible to them; it is written the strict way because the DLEQ
    verifier this module exists for CANNOT use the cofactored trick (the module's
    `is_torsion_free` docstring says why), and a test-only verifier that used the
    looser rule would exercise the looser arithmetic.
    """
    if len(signature) != 2 * POINT_BYTES:
        return False
    r_encoding, s_encoding = signature[:POINT_BYTES], signature[POINT_BYTES:]
    if not is_canonical_scalar_le(s_encoding):
        return False
    try:
        a_point = Point.decompress(public_key)
        r_point = Point.decompress(r_encoding)
    except Ed25519Error:
        return False
    s_scalar = int.from_bytes(s_encoding, "little")
    challenge = scalar_from_bytes_le(hashlib.sha512(r_encoding + public_key + message).digest())
    return scalar_base_mul(s_scalar) == r_point.add(point_scalar_mul(challenge, a_point))


# --- Deterministic test data --------------------------------------------------


def _derive_scalar(label: str, index: int) -> int:
    """A reproducible non-zero scalar in [1, l) from a label and an index.

    SHA-512 rather than `random.randrange`, for tests/test_adaptor_ecdsa.py's
    stated reason: a failing case is then identified completely by its label and
    index, so it can be re-run with no seed to remember. The rejection loop
    avoids returning zero without biasing anything that matters to a test.
    """
    counter = 0
    while True:
        digest = hashlib.sha512(f"{label}/{index}/{counter}".encode()).digest()
        value = int.from_bytes(digest, "little") % GROUP_ORDER
        if value != 0:
            return value
        counter += 1


def _derive_point(label: str, index: int) -> Point:
    """A reproducible point in the prime-order subgroup: k*B for a derived k.

    Every point produced this way is torsion-free, which is the right default
    for property tests about the group law and the wrong one for the torsion
    tests -- those build their inputs by adding a derived 8-torsion point
    instead, and say so.
    """
    return scalar_base_mul(_derive_scalar(label, index))


def _eight_torsion_generator() -> Point:
    """A point of order exactly 8, DERIVED rather than pasted from a published list.

    The derivation, which needs nothing but the module under test: the full curve
    has order 8*l, so for ANY point P on the curve, l*P has order dividing 8.
    Scan small y values for one that decompresses, multiply by l, and keep the
    first result whose order is not 1, 2 or 4.

    Measured 2026-09-27: y = 3 is the first y that works, and the resulting point
    compresses to c7176a703d4dd84fba3c0b760d10670f2a2053fa2c39ccc64ec7fd7792ac037a.
    Its sign-flipped sibling ...03fa is published independently as a small-order
    point in ziglang/zig's lib/std/crypto/25519/ed25519.zig test suite (found
    2026-09-27 by GitHub code search), and `test_derived_small_order_set_matches_published_value`
    asserts the derived set contains it.

    THE PUBLISHED SOURCE FOR THE FULL LIST IS CITED BUT WAS NOT READ: Chalkias,
    Garillot and Nikolaenko, "Taming the Many EdDSAs" (IACR ePrint 2020/1244),
    Table 1, which is where the eight canonical small-order encodings are
    tabulated. eprint.iacr.org is blocked from this container (measured
    2026-09-27, the same block docs/dleq_cross_curve_design.md records), so that
    citation is a pointer for a reader and NOT evidence behind this test.
    The evidence is the derivation plus libsodium's agreement.
    """
    for y in range(2, 64):
        try:
            candidate = Point.decompress(y.to_bytes(POINT_BYTES, "little"))
        except Ed25519Error:
            continue
        torsion = point_scalar_mul_unreduced(GROUP_ORDER, candidate)
        if not torsion.double().double().is_identity():
            return torsion
    raise AssertionError("no order-8 point found for y < 64; the curve order or the group law is wrong")


def point_scalar_mul_unreduced(scalar: int, point: Point) -> Point:
    """k*P WITHOUT reducing k mod l, by repeated doubling and addition.

    The module's `point_scalar_mul` reduces, and its docstring explains why that
    is right for every caller it has and wrong for exactly one caller: this one.
    l mod l is 0, so asking the module for l*P would return the identity for
    every input and the derivation above would find nothing.

    Written here as a plain double-and-add rather than by reaching into the
    module's private `_scalar_mul_with_table`: a test that pokes at a private
    name is a test that breaks when the private name changes, and this is six
    lines. It is also a SECOND, independent implementation of scalar
    multiplication, which is the one place in this file where duplicating logic
    is correct -- and per CLAUDE.md rule 8 that is said at both sites, so the
    module's `_scalar_mul_with_table` docstring is where the fast one lives.
    """
    accumulator = IDENTITY
    for bit in reversed(range(scalar.bit_length())):
        accumulator = accumulator.double()
        if (scalar >> bit) & 1:
            accumulator = accumulator.add(point)
    return accumulator


# --- 1. RFC 8032 section 7.1 known-answer tests ------------------------------


@pytest.mark.parametrize("vector", RFC_8032_SECTION_7_1, ids=[v["name"] for v in RFC_8032_SECTION_7_1])
def test_rfc8032_public_key_is_rederived_from_the_secret_key(vector: dict[str, str]) -> None:
    """scalar_base_mul + compress reproduce RFC 8032's published public key.

    This is the narrowest possible KAT on fixed-base multiplication and on the
    encoding: one wrong bit in the window loop, the basepoint, or the sign bit
    and the 32 bytes differ. If this passes, `scalar_base_mul` and
    `Point.compress` agree with the RFC on five independent inputs.
    """
    seed = bytes.fromhex(vector["secret_key"])
    expected = bytes.fromhex(vector["public_key"])
    derived = scalar_base_mul(_ed25519_secret_scalar(seed)).compress()
    assert derived == expected, f"{vector['name']}: derived {derived.hex()} != published {expected.hex()}"


@pytest.mark.parametrize("vector", RFC_8032_SECTION_7_1, ids=[v["name"] for v in RFC_8032_SECTION_7_1])
def test_rfc8032_signature_verifies(vector: dict[str, str]) -> None:
    """The published signature verifies through decompression, the group law and both multiplications.

    The broadest KAT available: it exercises `Point.decompress` (x-recovery, on
    both A and R), `scalar_from_bytes_le` over a 64-byte SHA-512 digest,
    `point_scalar_mul`, `scalar_base_mul`, `Point.add` and projective `__eq__`,
    and it does so against numbers produced by implementations that predate this
    one by nine years.
    """
    assert _ed25519_verify(
        bytes.fromhex(vector["public_key"]),
        bytes.fromhex(vector["message"]),
        bytes.fromhex(vector["signature"]),
    ), f"{vector['name']}: published signature did not verify"


@pytest.mark.parametrize("vector", RFC_8032_SECTION_7_1, ids=[v["name"] for v in RFC_8032_SECTION_7_1])
def test_rfc8032_signature_fails_with_one_bit_flipped_in_the_message(vector: dict[str, str]) -> None:
    """A mutated message must NOT verify -- the KAT above is worthless without this.

    CLAUDE.md's behavioral-verification principle in miniature: a verifier that
    returns True unconditionally passes every vector in the previous test. This
    is the assertion that makes those five mean something, and it flips the LAST
    bit of the message specifically because a verifier that hashed only a prefix
    would still pass a first-bit flip.
    """
    message = bytearray(bytes.fromhex(vector["message"]) or b"\x00")
    message[-1] ^= 0x01
    assert not _ed25519_verify(
        bytes.fromhex(vector["public_key"]),
        bytes(message),
        bytes.fromhex(vector["signature"]),
    ), f"{vector['name']}: a signature verified over a mutated message"


# --- 2. Cross-checks against independent implementations ---------------------


def test_go_edwards25519_vectors_are_reproduced_byte_for_byte() -> None:
    """Every ed25519 point in tests/vectors/dleq_cross_curve_go.json is reproduced from its witness.

    Produced by Go's filippo.io/edwards25519 (the file is already in this repo
    for the cross-curve DLEQ work). The `one` vector's point is the compressed
    basepoint, which is the check the task for this module named specifically:
    `scalar_base_mul(1)` must equal it byte for byte.

    The witnesses are stored little-endian, per the field name
    `witness_hex_le32`, and reading them big-endian would produce three valid
    points that match nothing -- which is the failure
    `scalar_from_bytes_le`'s name exists to prevent.
    """
    vectors = json.loads((VECTORS_DIR / "dleq_cross_curve_go.json").read_text())
    assert vectors, "the vector file is empty; this test would otherwise pass vacuously"
    for vector in vectors:
        witness = scalar_from_bytes_le(bytes.fromhex(vector["witness_hex_le32"]))
        produced = scalar_base_mul(witness).compress().hex()
        assert produced == vector["ed25519_point"], (
            f"{vector['label']}: produced {produced} != Go's {vector['ed25519_point']}"
        )


def test_the_one_vector_is_the_basepoint_and_scalar_base_mul_of_one_reproduces_it() -> None:
    """The `one` vector, the derived basepoint and RFC 8032's decimal literals are all the same point.

    Three independent statements of one constant, asserted together because the
    module DERIVES the basepoint (y = 4/5, x even) rather than copying it, and a
    derivation needs a check against something published or it is just an
    opinion.
    """
    vectors = json.loads((VECTORS_DIR / "dleq_cross_curve_go.json").read_text())
    one = next(v for v in vectors if v["label"] == "one")
    assert BASEPOINT.compress().hex() == one["ed25519_point"]
    assert scalar_base_mul(1) == BASEPOINT
    assert scalar_base_mul(1).compress() == BASEPOINT.compress()
    # RFC 8032 section 5.1, verbatim decimal literals. These appear NOWHERE in
    # the module -- that is the point of deriving the basepoint -- so this is the
    # only place in the tree they can be diffed against the RFC by eye.
    assert BASEPOINT.to_affine() == (
        15112221349535400772501151409588531511454012693041857206046113283949847762202,
        46316835694926478169428394003475163141307993866256225615783033603165251855960,
    )


@libsodium_required
def test_point_addition_and_subtraction_agree_with_libsodium() -> None:
    """512 additions and 512 subtractions against libsodium's crypto_core_ed25519_add/sub.

    The group law is where a transcription error in the HWCD formulas would live,
    and it is the part no published KAT covers directly -- RFC 8032's vectors
    exercise it, but only along the one path a signature verification takes. 512
    random pairs is the cheapest way to cover the rest.
    """
    for index in range(512):
        left = _derive_point("libsodium-add-left", index)
        right = _derive_point("libsodium-add-right", index)
        assert left.add(right).compress() == _libsodium.crypto_core_ed25519_add(
            left.compress(), right.compress()
        ), f"addition disagreed with libsodium at index {index}"
        assert left.subtract(right).compress() == _libsodium.crypto_core_ed25519_sub(
            left.compress(), right.compress()
        ), f"subtraction disagreed with libsodium at index {index}"


@libsodium_required
def test_scalar_multiplication_agrees_with_libsodium() -> None:
    """256 fixed-base and 256 variable-base multiplications against libsodium.

    `_noclamp` is the required variant: libsodium's clamping form would clear the
    low three bits of the scalar, which is RFC 8032's key-derivation step and NOT
    what a group operation does. Using the clamped function here would make every
    comparison fail and would look like a bug in the window loop.
    """
    for index in range(256):
        scalar = _derive_scalar("libsodium-mul", index)
        scalar_le = scalar_to_bytes_le(scalar)
        assert scalar_base_mul(scalar).compress() == _libsodium.crypto_scalarmult_ed25519_base_noclamp(
            scalar_le
        ), f"fixed-base multiplication disagreed with libsodium at index {index}"
        point = _derive_point("libsodium-mul-point", index)
        assert point_scalar_mul(scalar, point).compress() == _libsodium.crypto_scalarmult_ed25519_noclamp(
            scalar_le, point.compress()
        ), f"variable-base multiplication disagreed with libsodium at index {index}"


@libsodium_required
def test_scalar_arithmetic_agrees_with_libsodium() -> None:
    """512 reductions of 64-byte values, 256 inversions and 256 multiplications, against libsodium.

    The 64-byte reduction is the one that matters most: it is RFC 8032's
    challenge step, it is the only place a scalar arrives wider than the group
    order, and a reduction that used the wrong modulus would still produce a
    plausible-looking scalar.
    """
    for index in range(256):
        wide = hashlib.sha512(f"libsodium-reduce/{index}".encode()).digest()
        assert scalar_to_bytes_le(scalar_from_bytes_le(wide)) == _libsodium.crypto_core_ed25519_scalar_reduce(
            wide
        ), f"64-byte reduction disagreed with libsodium at index {index}"
        wide_alt = hashlib.sha512(f"libsodium-reduce-alt/{index}".encode()).digest()
        assert scalar_to_bytes_le(
            scalar_from_bytes_le(wide_alt)
        ) == _libsodium.crypto_core_ed25519_scalar_reduce(wide_alt)
        left = _derive_scalar("libsodium-scalar-left", index)
        right = _derive_scalar("libsodium-scalar-right", index)
        assert scalar_to_bytes_le(scalar_inverse(left)) == _libsodium.crypto_core_ed25519_scalar_invert(
            scalar_to_bytes_le(left)
        ), f"inversion disagreed with libsodium at index {index}"
        assert scalar_to_bytes_le(scalar_mul(left, right)) == _libsodium.crypto_core_ed25519_scalar_mul(
            scalar_to_bytes_le(left), scalar_to_bytes_le(right)
        ), f"scalar multiplication disagreed with libsodium at index {index}"


def _libsodium_multiply_by_l(encoding: bytes) -> bytes:
    """l*P computed entirely inside libsodium, as (l//2)*P + (l - l//2)*P.

    libsodium has no "multiply by the group order" entry point, and it cannot
    have one: `crypto_scalarmult_ed25519_noclamp` refuses a scalar that is zero
    mod l, and l IS zero mod l. Splitting l into two halves that are each below l
    gets the same product out of the same arithmetic, which is what makes this an
    INDEPENDENT computation of the quantity `is_torsion_free` decides on -- and
    it is the evidence behind the test below.
    """
    half = GROUP_ORDER // 2
    return _libsodium.crypto_core_ed25519_add(
        _libsodium.crypto_scalarmult_ed25519_noclamp(scalar_to_bytes_le(half), encoding),
        _libsodium.crypto_scalarmult_ed25519_noclamp(scalar_to_bytes_le(GROUP_ORDER - half), encoding),
    )


@libsodium_required
def test_point_validity_agrees_with_libsodium_except_where_this_libsodium_is_weaker() -> None:
    """1,024 pseudorandom 32-byte strings, and the 64 disagreements are a MEASURED defect in the ORACLE.

    This test found something, so it is written as the finding rather than as an
    assertion of equality. MEASURED 2026-09-27 over the 1,024 strings below:

        canonical, on-curve points                  489 of 1024
        of those, torsion-free by this module        60  (12.3%, and 1/8 is 12.5%)
        libsodium's crypto_core_ed25519_is_valid_point says valid   124
        disagreements, all one-directional          64

    Every one of the 64 is a point this module rejects and this libsodium
    accepts, and all 64 have the SAME shape: l*P is the order-2 point
    ecffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff7f rather than
    the identity. 124/489 is 25%, which is 2/8 -- exactly the points whose
    torsion component has order 1 or 2 -- where torsion-free is 1/8.

    WHICH ONE IS WRONG, ESTABLISHED RATHER THAN ASSUMED (rule 17). libsodium's
    OWN arithmetic agrees with this module: `_libsodium_multiply_by_l` computes
    l*P using nothing but libsodium calls and returns the same order-2 point for
    all 64, asserted below. So the two implementations agree on the ARITHMETIC
    and disagree on the PREDICATE, which locates the defect in the predicate.

    AND THE PREDICATE IS A KNOWN, FIXED libsodium BUG. Read 2026-09-27:
    libsodium 1.0.16's `ge25519_is_on_main_subgroup` is
    `return fe25519_iszero(pl.X);` -- X == 0 alone, which is true for the
    identity AND for the order-2 point (0, -1), hence 2/8 instead of 1/8. From
    1.0.18 it is `fe25519_iszero(pl.X) & fe25519_iszero(pl.Y - pl.Z)`, the strict
    test. (Sources: the 1.0.16 and master copies of
    src/libsodium/crypto_core/ed25519/ref10/ed25519_ref10.c on
    raw.githubusercontent.com.) The pynacl 1.4.0 this was first measured against
    behaves like the pre-1.0.18 version; its exact version string is NOT readable
    from Python (`sodium_version_string` is not exposed through pynacl's cffi
    bindings, measured), so "that build behaves like 1.0.16" is a statement about
    behavior and not about a version number, and it is written that way.

    CONFIRMED FROM THE OTHER SIDE, and this is why the assertion at the end is a
    set and not a number. Re-run 2026-09-27 against **pynacl 1.6.2** -- the
    current release, installed fresh, on the same 1,024 strings:

        libsodium build   canonical   torsion-free (this module)   disagreements
        pynacl 1.4.0            489                           60              64
        pynacl 1.6.2            489                           60               0

    The two left columns are identical, which is what makes the comparison mean
    anything: the sample and this module's verdict did not move, only the
    oracle's did. So the 64 are a defect in one libsodium generation and not a
    property of the check, and a reader who sees 0 here is looking at a fixed
    oracle rather than at a weakened test.

    NOTE ON WHEN THIS TEST RUNS AT ALL. pynacl is deliberately not a dependency
    of this repo, so in a default checkout this test and the three other
    libsodium cross-checks SKIP -- the suite reports `4 skipped`, which is honest
    but does not say that the loss is the only non-self-referential evidence for
    `is_torsion_free`. `python3 -m pip install pynacl` turns all four on, and the
    numbers above are what they produce.

    WHY THIS IS THE MOST IMPORTANT TEST IN THE FILE. It is the only evidence for
    `is_torsion_free` from anything other than this module -- no published KAT
    exists for a torsion check -- and it says the module is STRICTER than the
    oracle on exactly the property a cross-curve DLEQ depends on. A verifier
    built on this container's libsodium would accept a claimed key whose torsion
    component has order 2. If this test ever starts failing with zero
    disagreements, that is libsodium being upgraded, not a regression: the
    assertion below allows it explicitly.
    """
    # The order-2 point, (0, -1), spelled as its canonical encoding. Derived
    # rather than trusted: it is l*P for every one of the 64 disagreements, and
    # the assertion below is what pins that.
    order_two_encoding = bytes.fromhex("ecffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff7f")
    canonical_count = 0
    torsion_free_count = 0
    disagreements = []
    for index in range(1024):
        candidate = hashlib.sha512(f"validity/{index}".encode()).digest()[:POINT_BYTES]
        theirs = bool(_libsodium.crypto_core_ed25519_is_valid_point(candidate))
        if not is_canonical(candidate):
            assert not theirs, f"libsodium accepted a string this module calls non-canonical: {candidate.hex()}"
            continue
        canonical_count += 1
        point = Point.decompress(candidate)
        ours = is_torsion_free(point) and not point.is_identity()
        if ours:
            torsion_free_count += 1
            assert theirs, f"libsodium rejected a torsion-free point at index {index}: {candidate.hex()}"
        elif theirs:
            disagreements.append((index, candidate, point))
    assert canonical_count == 489
    assert torsion_free_count == 60

    for index, candidate, point in disagreements:
        ours_l = point_scalar_mul_unreduced(GROUP_ORDER, point).compress()
        assert ours_l == order_two_encoding, (
            f"index {index}: expected l*P to be the order-2 point, got {ours_l.hex()}"
        )
        assert _libsodium_multiply_by_l(candidate) == ours_l, (
            f"index {index}: libsodium's own l*P disagrees with this module's, "
            "which would move the defect from libsodium's predicate to one of the two arithmetics"
        )
    # 64 on the build measured 2026-09-27; 0 on a libsodium from 1.0.18 onward.
    # Any other number means the disagreement is no longer the single explained
    # class above, and the loop over `disagreements` is what would say so.
    assert len(disagreements) in (0, 64), (
        f"{len(disagreements)} disagreements is neither the measured 64 (pre-1.0.18 libsodium) nor 0 (fixed)"
    )


# --- 3. Property tests -------------------------------------------------------


@pytest.mark.parametrize("index", range(32))
def test_compress_decompress_round_trip(index: int) -> None:
    """decompress(compress(P)) == P, and the encoding is canonical and 32 bytes.

    Over 32 derived points rather than one, because the sign bit is what a round
    trip is actually testing and a single point exercises one value of it.
    """
    point = _derive_point("round-trip", index)
    encoding = point.compress()
    assert len(encoding) == POINT_BYTES
    assert is_canonical(encoding)
    assert Point.decompress(encoding) == point
    assert Point.decompress(encoding).compress() == encoding


@pytest.mark.parametrize("index", range(16))
def test_point_scalar_mul_against_the_basepoint_equals_scalar_base_mul(index: int) -> None:
    """point_scalar_mul(k, B) == scalar_base_mul(k): the fixed-base path is not a different function.

    The persistent basepoint window table is the one piece of shared mutable-ish
    state in the module (a module-level constant built at import), and this is
    the assertion that it holds a table for the basepoint and not for something
    else.
    """
    scalar = _derive_scalar("fixed-vs-variable", index)
    assert point_scalar_mul(scalar, BASEPOINT) == scalar_base_mul(scalar)


@pytest.mark.parametrize("index", range(16))
def test_addition_is_associative_and_commutative(index: int) -> None:
    """(P + Q) + R == P + (Q + R), and P + Q == Q + P.

    Associativity is the property that catches a sign error in the HWCD formulas
    which happens to be self-consistent for a doubling: a wrong _D2 can still
    satisfy P + P == 2P while breaking three-term addition.
    """
    p_point = _derive_point("assoc-p", index)
    q_point = _derive_point("assoc-q", index)
    r_point = _derive_point("assoc-r", index)
    assert p_point.add(q_point).add(r_point) == p_point.add(q_point.add(r_point))
    assert p_point.add(q_point) == q_point.add(p_point)


@pytest.mark.parametrize("index", range(16))
def test_negate_then_add_gives_the_identity(index: int) -> None:
    """P + (-P) == identity, with no special case in `add`.

    On a twisted Edwards curve this works because the addition formulas are
    complete -- see `Point.add`. It is the case a short Weierstrass
    implementation has to branch for, so it is the case worth asserting here.
    """
    point = _derive_point("negate", index)
    assert point.add(point.negate()).is_identity()
    assert point.add(point.negate()) == IDENTITY
    assert point.subtract(point).is_identity()
    assert point.negate().negate() == point


@pytest.mark.parametrize("index", range(16))
def test_scalar_multiplication_is_a_homomorphism(index: int) -> None:
    """(a + b)*P == a*P + b*P and (a*b)*P == a*(b*P), tying scalar arithmetic to point arithmetic.

    This is the only place the two halves of the module are checked against each
    other. `scalar_add` reducing mod the wrong modulus, or `point_scalar_mul`
    dropping a window, breaks exactly here and nowhere else in this file.
    """
    a_scalar = _derive_scalar("hom-a", index)
    b_scalar = _derive_scalar("hom-b", index)
    point = _derive_point("hom-p", index)
    assert point_scalar_mul(scalar_add(a_scalar, b_scalar), point) == point_scalar_mul(
        a_scalar, point
    ).add(point_scalar_mul(b_scalar, point))
    assert point_scalar_mul(scalar_mul(a_scalar, b_scalar), point) == point_scalar_mul(
        a_scalar, point_scalar_mul(b_scalar, point)
    )
    assert point_scalar_mul(scalar_sub(a_scalar, a_scalar), point).is_identity()


def test_projective_equality_holds_for_the_same_point_reached_two_ways() -> None:
    """A point computed by two different routes compares equal although its coordinates differ.

    This is the assertion that pins `eq=False` on the dataclass. 5*B reached as
    B+B+B+B+B and as 5*B have different (X:Y:Z:T) representatives -- checked
    explicitly below, so the test cannot pass vacuously if some future change
    normalizes them -- and the dataclass-generated field-by-field `__eq__` would
    call them unequal. The module docstring explains why that failure would be
    intermittent and therefore worse than a consistent one.
    """
    stepwise = BASEPOINT
    for _ in range(4):
        stepwise = stepwise.add(BASEPOINT)
    direct = scalar_base_mul(5)
    assert (stepwise.X, stepwise.Y, stepwise.Z, stepwise.T) != (direct.X, direct.Y, direct.Z, direct.T)
    assert stepwise == direct
    assert hash(stepwise) == hash(direct)
    assert stepwise.compress() == direct.compress()


def test_identity_behaves_as_the_neutral_element() -> None:
    """P + 0 == P, 0 + 0 == 0, 0*P == 0, and the identity encodes as 0100..00 and decodes back."""
    point = _derive_point("identity", 0)
    assert point.add(IDENTITY) == point
    assert IDENTITY.add(IDENTITY) == IDENTITY
    assert IDENTITY.double().is_identity()
    assert point_scalar_mul(0, point).is_identity()
    assert scalar_base_mul(0).is_identity()
    encoding = IDENTITY.compress()
    assert encoding == bytes([1]) + bytes(31)
    assert Point.decompress(encoding).is_identity()
    # l*P == identity for any torsion-free P: the defining property of the
    # subgroup, and the thing `is_torsion_free` checks.
    assert point_scalar_mul_unreduced(GROUP_ORDER, point).is_identity()


# --- Encoding refusals -------------------------------------------------------


def test_non_canonical_y_above_the_field_prime_is_rejected() -> None:
    """An encoding with y >= p is refused by both is_canonical and decompress.

    Built by taking a real point's y and adding p, which produces a string that
    denotes the same point and is not its encoding. That is the malleability the
    module's `is_canonical` docstring is about, and the one
    chains/solana_address.py::_raw_is_on_curve already refuses for Solana
    addresses.
    """
    # y + p must still fit in the 255 bits the encoding carries, so y < 19. That
    # is the whole reason only 19 y-values are affected, and it is why this
    # fixture SEARCHES the small y instead of taking a derived point's y: a
    # random y is about 255 bits and y + p would overflow into the sign bit,
    # producing a different point rather than a second encoding of one.
    for small_y in range(2**255 - FIELD_PRIME):
        canonical = small_y.to_bytes(POINT_BYTES, "little")
        if not is_canonical(canonical):
            continue
        shifted = small_y + FIELD_PRIME
        assert shifted < 1 << 255
        encoding = shifted.to_bytes(POINT_BYTES, "little")
        assert not is_canonical(encoding), f"{encoding.hex()} has y >= p and must be non-canonical"
        with pytest.raises(Ed25519Error, match="non-canonical"):
            Point.decompress(encoding)
        # The canonical spelling of the same point IS accepted, and the two
        # strings differ only by p -- so this test is about canonicality and not
        # about the point being unusable.
        assert is_canonical(canonical)
        assert Point.decompress(canonical).compress() == canonical
        return
    raise AssertionError("no y below 2^255 - p was on the curve, which contradicts the enumeration test")


def test_negative_zero_encoding_is_rejected() -> None:
    """x == 0 with the sign bit set is a second encoding of the identity and is refused.

    The identity is 0100..00. Setting the top bit means "the negative of x = 0",
    which is x = 0, so 0100..80 denotes the same point. The same holds for the
    order-2 point at y = p - 1. Both are refused.
    """
    for canonical, negative_zero in (
        (bytes([1]) + bytes(31), bytes([1]) + bytes(30) + bytes([0x80])),
        (
            (FIELD_PRIME - 1).to_bytes(POINT_BYTES, "little"),
            ((FIELD_PRIME - 1) | (1 << 255)).to_bytes(POINT_BYTES, "little"),
        ),
    ):
        assert is_canonical(canonical), f"{canonical.hex()} should be the canonical form"
        assert not is_canonical(negative_zero), f"{negative_zero.hex()} should be non-canonical"
        with pytest.raises(Ed25519Error):
            Point.decompress(negative_zero)


def test_the_non_canonical_encoding_count_is_what_the_module_claims() -> None:
    """Re-run the enumeration the module's is_canonical docstring reports, and pin its numbers.

    MEASURED 2026-09-27 and written into that docstring: of the 38 (y, sign)
    pairs with y in [p, 2^255), 23 denote a valid curve point, over 12 distinct
    y; plus the 2 negative-zero strings, so 25 of the 2^256 possible 32-byte
    strings are non-canonical encodings of a valid point.

    This test exists for one specific failure: a `y >= FIELD_PRIME` check that is
    silently never true -- because the mask was wrong, or because the comparison
    was written against 2^255 instead of p -- would make `is_canonical` a
    function that rejects nothing, and every other test in this file would still
    pass. Rule 3: the denominator is 38, not 19, and stating it is the point.
    """
    decoding_pairs = []
    for y_value in range(FIELD_PRIME, 1 << 255):
        for sign in (0, 1):
            encoding = (y_value | (sign << 255)).to_bytes(POINT_BYTES, "little")
            # is_canonical must say False for every one of these, whether or not
            # the reduced y denotes a real point.
            assert not is_canonical(encoding)
            reduced = (y_value % FIELD_PRIME) | (sign << 255)
            if is_canonical(reduced.to_bytes(POINT_BYTES, "little")):
                decoding_pairs.append((y_value, sign))
    assert len(range(FIELD_PRIME, 1 << 255)) * 2 == 38, "19 y-values above p, two sign bits each"
    assert len(decoding_pairs) == 23
    assert len({y for y, _ in decoding_pairs}) == 12


@pytest.mark.parametrize(
    ("bad", "reason"),
    [
        (b"", "wrong length"),
        (bytes(31), "one byte short"),
        (bytes(33), "one byte long"),
        (b"\x00" * 32, "y = 0 with the sign bit clear is a valid order-4 point, so this one must NOT raise"),
    ],
)
def test_decompress_refuses_wrong_lengths(bad: bytes, reason: str) -> None:
    """A wrong length raises rather than being padded or truncated.

    The fourth case is the control: 32 zero bytes IS a valid point (one of the
    order-4 points), so it must decode. Without it, a `decompress` that raised on
    everything would pass the other three.
    """
    if len(bad) == POINT_BYTES:
        assert Point.decompress(bad).compress() == bad, reason
        return
    with pytest.raises(Ed25519Error):
        Point.decompress(bad)


def test_an_off_curve_encoding_is_rejected() -> None:
    """A y whose implied x^2 is a non-residue is refused, and such a y is found by searching.

    Searched rather than hard-coded: roughly half of all y values are off the
    curve, so the first few small integers include one, and finding it here means
    the fixture cannot rot if the curve constant changes.
    """
    for y_value in range(2, 64):
        encoding = y_value.to_bytes(POINT_BYTES, "little")
        if not is_canonical(encoding):
            with pytest.raises(Ed25519Error, match="not a usable point"):
                Point.decompress(encoding)
            return
    raise AssertionError("no off-curve y below 64 was found, which contradicts the curve's structure")


# --- Small order and torsion --------------------------------------------------


def test_the_eight_torsion_points_are_derived_and_all_have_small_order() -> None:
    """All eight points of the 8-torsion subgroup are detected by has_small_order, and seven are not torsion-free.

    Derived, not pasted -- `_eight_torsion_generator` explains how and why. The
    count is the assertion: exactly 8 distinct encodings, because the torsion
    subgroup of ed25519 has order 8, and a derivation that produced 4 or 16 would
    mean the cofactor or the group order is wrong.

    SEVEN of the eight are not torsion-free, not eight: the identity is in the
    prime-order subgroup as well as the torsion subgroup, and a test asserting
    "no small-order point is torsion-free" would fail on it. That is exactly the
    sort of off-by-one an implementation gets right and a test gets wrong.
    """
    generator = _eight_torsion_generator()
    encodings = {point_scalar_mul_unreduced(k, generator).compress() for k in range(8)}
    assert len(encodings) == COFACTOR
    torsion_free_count = 0
    for encoding in encodings:
        point = Point.decompress(encoding)
        assert has_small_order(point), f"{encoding.hex()} is in the torsion subgroup but was not detected"
        if is_torsion_free(point):
            torsion_free_count += 1
    assert torsion_free_count == 1, "only the identity is both small-order and torsion-free"
    assert IDENTITY.compress() in encodings


def test_derived_small_order_set_matches_published_value() -> None:
    """The derived eight contain a small-order encoding published by an unrelated project.

    c7176a703d4dd84fba3c0b760d10670f2a2053fa2c39ccc64ec7fd7792ac03fa appears as a
    small-order R value in ziglang/zig's lib/std/crypto/25519/ed25519.zig test
    suite (found 2026-09-27 by GitHub code search). It is one string rather than
    the full published table -- the table lives in IACR ePrint 2020/1244, which
    is blocked from this container -- and one independently published member is
    still a check the derivation cannot fake.
    """
    published = bytes.fromhex("c7176a703d4dd84fba3c0b760d10670f2a2053fa2c39ccc64ec7fd7792ac03fa")
    generator = _eight_torsion_generator()
    encodings = {point_scalar_mul_unreduced(k, generator).compress() for k in range(8)}
    assert published in encodings
    assert has_small_order(Point.decompress(published))
    assert not is_torsion_free(Point.decompress(published))


def test_a_point_that_is_neither_small_order_nor_torsion_free_is_the_dangerous_case() -> None:
    """B + T passes a small-order check and fails a torsion check: the two are not complements.

    This is the attack input the module's `has_small_order` docstring names. A
    verifier that blacklists small-order points and nothing else accepts this,
    and on a cross-curve DLEQ that is a claimed key whose discrete log is not
    what the proof is about.
    """
    torsion = _eight_torsion_generator()
    mixed = BASEPOINT.add(torsion)
    assert not has_small_order(mixed)
    assert not is_torsion_free(mixed)
    # And it is a perfectly well-formed, canonically encoded curve point, which
    # is the whole problem.
    assert is_canonical(mixed.compress())
    assert Point.decompress(mixed.compress()) == mixed


def test_honest_keys_and_both_generators_are_torsion_free() -> None:
    """The basepoint, Monero's RingCT H, and 32 derived subgroup points are all torsion-free.

    H matters most here. The module takes it as a published literal because
    re-deriving it needs Keccak, so the only property of it that CAN be checked
    from here is the one that would break the DLEQ if it failed: that it is a
    canonical, on-curve, prime-order point.
    """
    assert is_torsion_free(BASEPOINT)
    assert not has_small_order(BASEPOINT)
    assert is_canonical(RINGCT_H_ENCODING)
    assert RINGCT_H.compress() == RINGCT_H_ENCODING
    assert is_torsion_free(RINGCT_H)
    assert not has_small_order(RINGCT_H)
    assert RINGCT_H != BASEPOINT
    for index in range(32):
        assert is_torsion_free(_derive_point("torsion-free", index))


# --- 4. Scalars at and above the group order ---------------------------------


def test_scalars_at_and_above_the_group_order_reduce() -> None:
    """l reduces to 0, l+1 to 1, and a negative scalar lands in [0, l).

    The boundary cases a random scalar will never hit, pinned deliberately --
    docs/dleq_cross_curve_design.md section 3.2 makes exactly this point about
    its own soundness suite ("a suite that uses small fixtures will not [hit both
    cases]").
    """
    assert scalar_reduce(GROUP_ORDER) == 0
    assert scalar_reduce(GROUP_ORDER + 1) == 1
    assert scalar_reduce(GROUP_ORDER - 1) == GROUP_ORDER - 1
    assert scalar_reduce(-1) == GROUP_ORDER - 1
    assert scalar_reduce(2 * GROUP_ORDER + 5) == 5
    assert scalar_add(GROUP_ORDER - 1, 1) == 0
    assert scalar_sub(0, 1) == GROUP_ORDER - 1
    assert scalar_mul(GROUP_ORDER - 1, GROUP_ORDER - 1) == 1
    # l is prime, so l*P is the identity for a subgroup point, and a scalar of l
    # must therefore act as zero.
    assert scalar_base_mul(GROUP_ORDER).is_identity()
    assert scalar_base_mul(GROUP_ORDER + 1) == BASEPOINT


def test_scalar_from_bytes_le_of_a_value_above_the_order_does_not_produce_the_unreduced_point() -> None:
    """A 32-byte scalar above l reduces, and the module gives a caller a way to refuse it instead.

    THIS IS THE TEST FOR THE SILENT CASE docs/dleq_cross_curve_design.md SECTION 3
    IS ABOUT, and it has two halves because the module's position has two halves:

      1. `scalar_from_bytes_le` REDUCES, which RFC 8032 requires, so the point it
         produces for a value v >= l is (v mod l)*B and NOT some 253-bit-integer
         point -- there is no such point, and a reader who assumes the unreduced
         integer "went in" has the wrong model. Asserted directly: the two
         agree, and the unreduced integer's own multiple of B is the same point,
         because l*B is the identity.
      2. `is_canonical_scalar_le` says False for that same 32 bytes, which is how
         a DLEQ verifier refuses it instead of accepting two encodings of one
         scalar. A wire scalar that reduces is a malleability bug, and the module
         puts the refusal in the caller's hands rather than reducing silently.

    The fixture is l itself and l+1, spelled as 32-byte little-endian, plus the
    all-ones string -- the largest 32-byte value, which is about 2^255/l = 7.6
    times the order.
    """
    for offset in (0, 1, 2):
        above = (GROUP_ORDER + offset).to_bytes(32, "little")
        assert not is_canonical_scalar_le(above), "a 32-byte scalar >= l must be refusable"
        assert scalar_from_bytes_le(above) == offset
        assert scalar_base_mul(scalar_from_bytes_le(above)) == scalar_base_mul(offset)
    all_ones = b"\xff" * 32
    unreduced = int.from_bytes(all_ones, "little")
    assert unreduced > GROUP_ORDER
    assert not is_canonical_scalar_le(all_ones)
    assert scalar_from_bytes_le(all_ones) == unreduced % GROUP_ORDER
    # The unreduced integer and its reduction give the SAME point, which is why
    # reduction here is silent and why the refusal has to be a separate call.
    assert point_scalar_mul_unreduced(unreduced, BASEPOINT) == scalar_base_mul(scalar_from_bytes_le(all_ones))
    # And a canonical scalar is accepted, so the predicate is not simply False.
    assert is_canonical_scalar_le(scalar_to_bytes_le(GROUP_ORDER - 1))


def test_scalar_from_bytes_le_is_little_endian_and_says_so() -> None:
    """The endianness in the name is the endianness in the code.

    0x01 in the FIRST byte is the scalar 1, not 2^248. Getting this backwards
    produces valid points and valid proofs about the wrong secret, with no error
    anywhere -- the failure the function's name exists to prevent -- so it is
    asserted rather than assumed, and it is asserted against the Go vector file
    too (`test_go_edwards25519_vectors_are_reproduced_byte_for_byte`), whose
    field is named `witness_hex_le32` by its producer.
    """
    assert scalar_from_bytes_le(bytes([1]) + bytes(31)) == 1
    assert scalar_from_bytes_le(bytes(31) + bytes([1])) == 1 << 248
    assert scalar_to_bytes_le(1) == bytes([1]) + bytes(31)
    assert scalar_from_bytes_le(scalar_to_bytes_le(12345)) == 12345
    # A 64-byte input is accepted (RFC 8032's challenge) and reduced.
    wide = bytes([1]) + bytes(63)
    assert scalar_from_bytes_le(wide) == 1


@pytest.mark.parametrize("length", [0, 1, 31, 33, 63, 65, 128])
def test_scalar_from_bytes_le_refuses_other_lengths(length: int) -> None:
    """Only 32 and 64 bytes are accepted; nothing is padded or truncated."""
    with pytest.raises(Ed25519Error, match="32 or 64 bytes"):
        scalar_from_bytes_le(bytes(length))


def test_scalar_inverse_round_trips_and_refuses_zero() -> None:
    """k * k^-1 == 1 for 32 derived scalars, and zero raises rather than returning zero."""
    for index in range(32):
        scalar = _derive_scalar("inverse", index)
        assert scalar_mul(scalar, scalar_inverse(scalar)) == 1
    assert scalar_inverse(1) == 1
    assert scalar_inverse(GROUP_ORDER - 1) == GROUP_ORDER - 1
    with pytest.raises(Ed25519Error, match="no inverse"):
        scalar_inverse(0)
    with pytest.raises(Ed25519Error, match="no inverse"):
        scalar_inverse(GROUP_ORDER)


@pytest.mark.parametrize("bad", [None, 1.5, "1", b"\x01", True])
def test_scalar_reduce_refuses_non_integers(bad: object) -> None:
    """A float, a string, bytes or a bool is not a scalar.

    `True` is in the list deliberately: `isinstance(True, int)` is True in
    Python, so a bool reaches every arithmetic path unless it is refused
    explicitly, and `scalar_base_mul(True)` silently meaning `scalar_base_mul(1)`
    is the kind of coincidence that hides a caller passing a flag where a key
    belongs.
    """
    with pytest.raises(Ed25519Error):
        scalar_reduce(bad)  # type: ignore[arg-type]


def test_repr_is_the_compressed_encoding() -> None:
    """repr(P) prints the compressed point, not four 77-digit integers.

    CLAUDE.md rule 14: pasted output has to be self-describing a day later. A
    repr that prints (X:Y:Z:T) cannot be compared against RFC 8032, against
    tests/vectors/dleq_cross_curve_go.json, or against another implementation.
    """
    assert repr(BASEPOINT) == f"Point({BASEPOINT.compress().hex()})"
    assert "5866666666666666666666666666666666666666666666666666666666666666" in repr(BASEPOINT)
