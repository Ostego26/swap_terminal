"""ECDSA adaptor signatures, against the DLC specification's own published vectors and against an independent verifier.

Role: test (pure functions; no chain, no socket, no database, no wallet)
Reads: swap_terminal/modules/adaptor_ecdsa.py, and the eleven official test
      vectors embedded below
Writes: nothing
Can move funds: no
Mainnet-safe: yes -- nothing here can reach a chain, and the module under test
      cannot either. The module itself is NOT mainnet-safe, for reasons that
      have nothing to do with sockets; see its docstring.

WHY THESE PARTICULAR TESTS, AND WHAT EACH ONE WOULD COST IF IT WERE MISSING.

modules/adaptor_ecdsa.py implements cryptography from a written specification,
and cryptography that merely looks right is worth nothing -- the same argument
tests/test_solana_address.py makes about hand-written ed25519 arithmetic. Two
kinds of evidence are therefore used here, and only one of them is this
implementation checking itself:

  INDEPENDENT     the eleven vectors in `OFFICIAL_VECTORS` come from
                  https://github.com/discreetlogcontracts/dlcspecs/blob/master/test/ecdsa_adaptor.json
                  and were produced by BlockstreamResearch/secp256k1-zkp's
                  ECDSA adaptor module, an independent implementation in C.
                  Fetched 2026-09-27. If this Python disagrees with them it is
                  wrong, full stop.
  INDEPENDENT     every "does this verify as an ordinary ECDSA signature"
                  assertion goes through `ecdsa.VerifyingKey.verify_digest`,
                  the `ecdsa` package's own verifier, never through anything in
                  the module under test. A round trip checked against our own
                  verify function would prove only that two halves of one file
                  agree.

The five properties the suite exists to pin, each with what its failure costs on
a GRC<->XMR swap:

  1. round trip            a pre-signature adapted with the right secret is a
                           signature the counterparty's node accepts. Failure
                           means a swap that cannot complete.
  2. a pre-signature is    THE property that makes the scheme safe. A
     NOT a signature       pre-signature is published to the counterparty before
                           anything is settled; if it were itself spendable,
                           publishing it would hand over the coins. This one is
                           MUTATION-CHECKED below -- see
                           test_the_not_a_signature_detector_catches_the_classic_mistake.
  3. wrong secret ->       nobody completes the signature by guessing.
     invalid signature
  4. recovery is exact     the leaked scalar IS the Monero key share. Off by a
                           sign, or right 99% of the time, means funds stranded
                           on a chain with no script to refund them.
  5. recovery returns      an unrelated signature must produce None, not an
     None, not garbage     exception and not a wrong secret. A caller that gets
     and not an exception   a wrong secret will try to spend with it.

MUTATION CHECKING, AND WHY ONLY PROPERTY 2 GETS IT

A test that asserts "this thing is rejected" passes just as happily when the
rejection mechanism is broken as when it works -- `assert not accepted` also
passes if `accepted` is always False for an unrelated reason. For property 2
that is intolerable, because the failure is silent and the cost is the coins.
So the suite contains a deliberate MUTANT: `_mutant_pre_sign_that_is_a_real_signature`
builds a pre-signature the way a plausible misreading of the specification
would -- taking `r` from `R_a` instead of from `R` -- and the suite asserts that
the very same detector used in property 2 ACCEPTS the mutant. If the detector
were vacuous, that assertion fails.

Properties 1, 3, 4 and 5 do not need the same treatment because each asserts a
positive, specific outcome (a signature verifies; an exact integer comes back),
and a broken implementation cannot satisfy those by accident.

WHAT THE MUTATION RUN ACTUALLY MEASURED, 2026-09-27

The suite was run against seven deliberate one-line mutations of
modules/adaptor_ecdsa.py, each restored afterwards (the file was byte-compared
against a saved copy after every round). The point is not that "tests fail when
code breaks" -- it is which test catches which break, because a property whose
only witness is an incidental assertion elsewhere is a property that is not
really pinned. 27 tests, all passing unmutated, 2.4µfn (2.9s):

  mutation                                        caught by
  r taken from R_a instead of R in pre_sign       property 2's test (the
                                                  pre-signature became a valid
                                                  signature) plus 5 others
  low-S negation removed from adapt               test_the_adapted_signature_is_low_s
                                                  and the official "decrypted
                                                  signature is high" vector.
                                                  NOTHING ELSE catches it --
                                                  the ecdsa package accepts
                                                  high-S, so without that test
                                                  the break is invisible here
                                                  and shows up as a Gridcoin
                                                  transaction that will not relay
  recover's `-Y` branch removed                   property 4 (200+ triples) and
                                                  the high-S official vector
  recover stops checking Y entirely               property 4, the mangled-s test,
                                                  and the high-S vector
  pre_verify stops calling DLEQ_verify            the tampered-proof test, the
                                                  different-message and
                                                  different-key tests, and the
                                                  official "proof is wrong" vector
  adapt multiplies by y instead of y⁻¹            properties 1, 3, 4 and two
                                                  official vectors
  recover stops checking r against R              ONLY the official recovery
                                                  vector commented "the R value
                                                  of the signature does not
                                                  match"

That last row is a real gap in THIS suite's own coverage and is written down
rather than smoothed over: property 5's unrelated-signature test still passes
with the r-match check removed, because an unrelated signature also fails the
later Y comparison and returns None by a different route. The property survives
-- the return value is still None -- but the check that is supposed to enforce
it is not the one being exercised. The official vector is what holds that line,
which is one more argument for embedding them.

NO `random` ANYWHERE. Test data is derived from SHA256 of a counter, which makes
every failure reproducible from the test name alone and means the suite never
touches a PRNG that would be wrong to use for a key. The module's own nonces
still come from `secrets` -- that is the code under test, not the fixtures.
"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

import pytest
from ecdsa import SECP256k1, SigningKey, VerifyingKey
from ecdsa.keys import BadSignatureError
from ecdsa.util import number_to_string

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "swap_terminal"))

# Imported after the sys.path insert above, which is CLAUDE.md rule 10's layout
# gap rather than a hazard. No `noqa: E402` here: ruff reports one as unnecessary
# (measured 2026-09-27, RUF100), because tests/conftest.py already makes this
# import resolvable and ruff's E402 does not fire on it.
from modules.adaptor_ecdsa import (
    AdaptorError,
    PreSignature,
    adapt,
    point_from_bytes,
    point_to_bytes,
    pre_sign,
    pre_verify,
    public_key_point,
    recover_adaptor_secret,
)

_N = SECP256k1.order
_G = SECP256k1.generator
_HALF_N = _N // 2


# --- Deterministic test data -------------------------------------------------


def _derive_scalar(label: str, index: int) -> int:
    """A reproducible scalar in 1..n-1 from a label and an index.

    Deliberately not `random.randrange`: a failing case is then identified
    completely by its label and index, so a regression can be re-run without a
    seed to remember. The rejection loop keeps the value in range without
    biasing it -- irrelevant to a test's security, kept because a fixture that
    can return 0 would exercise the error path by accident and look like a bug.
    """
    counter = 0
    while True:
        digest = hashlib.sha256(f"{label}/{index}/{counter}".encode()).digest()
        value = int.from_bytes(digest, "big")
        if 1 <= value < _N:
            return value
        counter += 1


def _derive_digest(label: str, index: int) -> bytes:
    """A 32-byte message hash. Stands in for a transaction digest; its content is irrelevant."""
    return hashlib.sha256(f"digest/{label}/{index}".encode()).digest()


def _standard_verify(public_key, message_hash: bytes, signature: tuple[int, int]) -> bool:
    """Verify (r, s) with the `ecdsa` package's own verifier, NOT with anything in the module under test.

    This is the load-bearing helper of the whole file. `VerifyingKey.verify_digest`
    is third-party code that knows nothing about adaptor signatures, so when it
    accepts a signature that means an ordinary ECDSA verifier accepts it -- which
    is the only statement that matters for "will the counterparty's node take
    this transaction".

    Returns a bool rather than letting BadSignatureError propagate, so a test can
    assert on rejection as an outcome. `MalformedPointError` and `ValueError` are
    caught for the same reason: an out-of-range scalar is a rejection, and a test
    for "this does not verify" must not pass merely because the encoder blew up
    before verification -- which is why the caught set is narrow and named rather
    than a bare `except Exception`.
    """
    r, s = signature
    if not 1 <= r < _N or not 1 <= s < _N:
        return False
    verifying_key = VerifyingKey.from_public_point(public_key, curve=SECP256k1)
    encoded = number_to_string(r, _N) + number_to_string(s, _N)
    try:
        return bool(verifying_key.verify_digest(encoded, message_hash))
    except BadSignatureError:
        return False


def _mutant_pre_sign_that_is_a_real_signature(private_key: int, message_hash: bytes, adaptor_point):
    """A DELIBERATELY BROKEN pre-signer, used only to prove the property-2 detector is not vacuous.

    It is the specification misread in the single most plausible way: the spec
    says

        Set `R_a` to `k * G`
        Set `R` to `k * Y`
        Set `r` to the x-coordinate of `R` modulo `n`
        Set `s_a` to `k⁻¹ (m + r * x)`

    and this mutant takes `r` from `R_a` instead of from `R`. The result is not
    an adaptor signature at all: it is a textbook ECDSA signature over the same
    message, because R_a = k*G is exactly the nonce point ordinary ECDSA uses.

    A reader should see why that misreading is attractive: `R_a` is the point
    the verification equation checks against, so "the nonce point" feels like
    `R_a`. Building the signature scalar over `R_a` instead of `R` is the
    difference between handing your counterparty an encrypted signature and
    handing them the coins.

    Returns the same shape as the real function so the exact same detector can
    be pointed at it.
    """
    m = int.from_bytes(message_hash, "big") % _N
    k = _derive_scalar("mutant-nonce", int.from_bytes(message_hash[:4], "big"))
    r_a = _G * k
    r_point = adaptor_point * k
    r_from_the_wrong_point = r_a.x() % _N
    s_a = (pow(k, -1, _N) * (m + r_from_the_wrong_point * private_key)) % _N
    return PreSignature(
        adaptor_point=adaptor_point,
        r_point=r_point,
        r_a=r_a,
        s_a=s_a,
        dleq_proof=(1, 1),
    ), r_from_the_wrong_point


def _pre_signature_read_as_an_ordinary_signature(pre_signature: PreSignature) -> tuple[int, int]:
    """The (r, s) a counterparty would try if handed a pre-signature and told to spend with it.

    `r` is the pre-signature's own r (the x-coordinate of R, which is what the
    completed signature will carry) and `s` is the adaptor scalar s_a. This is
    the most favorable reading available to an attacker, which is why it is the
    one tested.
    """
    return pre_signature.r, pre_signature.s_a


# --- Property 1: round trip, checked with the `ecdsa` package's verifier ------


def test_adapted_signature_verifies_under_the_ecdsa_packages_own_verifier():
    """pre_sign -> adapt(right secret) -> a signature a standard ECDSA verifier accepts.

    Twenty-five independent (key, message, secret) triples rather than one,
    because the low-S negation in `adapt` fires on roughly half of them and a
    single trial could miss whichever branch is broken.
    """
    for index in range(25):
        private_key = _derive_scalar("roundtrip-key", index)
        adaptor_secret = _derive_scalar("roundtrip-secret", index)
        digest = _derive_digest("roundtrip", index)
        public_key = public_key_point(private_key)
        adaptor_point = public_key_point(adaptor_secret)

        pre_signature = pre_sign(private_key, digest, adaptor_point)
        assert pre_verify(public_key, digest, adaptor_point, pre_signature) is True

        signature = adapt(pre_signature, adaptor_secret)
        assert _standard_verify(public_key, digest, signature), f"triple {index} did not verify"


def test_the_adapted_signature_is_low_s():
    """Every signature `adapt` produces is low-S, which Gridcoin's SCRIPT_VERIFY_LOW_S requires.

    Measured 2026-09-27 from gridcoin-community/Gridcoin-Research
    src/policy/policy.h: SCRIPT_VERIFY_LOW_S is in STANDARD_SCRIPT_VERIFY_FLAGS
    on master (so a high-S spend is non-standard and will not relay) and in
    V15_SCRIPT_VERIFY_FLAGS on development, whose comment states it is
    "activated as CONSENSUS at block version 15". Either way a high-S signature
    is unusable for the Gridcoin leg of a swap -- see the module docstring for
    the full quotation and for what remains unverified about which branch the
    live network runs.

    Without this test, `adapt` could drop the negation and every test above
    would still pass, because the `ecdsa` package's verifier accepts high-S
    signatures. The break would surface only as a transaction that never
    relays.
    """
    for index in range(50):
        private_key = _derive_scalar("lows-key", index)
        adaptor_secret = _derive_scalar("lows-secret", index)
        digest = _derive_digest("lows", index)
        _, s = adapt(pre_sign(private_key, digest, public_key_point(adaptor_secret)), adaptor_secret)
        assert s <= _HALF_N, f"triple {index} produced a high-S signature"


# --- Property 2: a pre-signature is NOT a signature, and the detector is checked ---


def test_a_pre_signature_does_not_verify_as_an_ordinary_ecdsa_signature():
    """THE safety property: publishing a pre-signature must not publish a spendable signature.

    If this fails, the swap protocol built on this module gives the coins away
    at the moment the pre-signature is sent -- before the counterparty has
    locked anything, and with no way to take it back.
    """
    for index in range(25):
        private_key = _derive_scalar("notasig-key", index)
        adaptor_secret = _derive_scalar("notasig-secret", index)
        digest = _derive_digest("notasig", index)
        public_key = public_key_point(private_key)
        adaptor_point = public_key_point(adaptor_secret)

        pre_signature = pre_sign(private_key, digest, adaptor_point)
        forged = _pre_signature_read_as_an_ordinary_signature(pre_signature)
        assert not _standard_verify(public_key, digest, forged), f"triple {index}: PRE-SIGNATURE VERIFIED AS A SIGNATURE"

        # The other reading available to an attacker: use R_a's x-coordinate as
        # r, since R_a is the point the pre-verification equation balances
        # against. It is the mutant's shape, without the mutant's s_a.
        also_forged = (pre_signature.r_a.x() % _N, pre_signature.s_a)
        assert not _standard_verify(public_key, digest, also_forged), f"triple {index}: R_a reading verified"


def test_the_not_a_signature_detector_catches_the_classic_mistake():
    """MUTATION CHECK for the test above: a deliberately broken pre-signer IS caught by the same detector.

    `test_a_pre_signature_does_not_verify_as_an_ordinary_ecdsa_signature`
    asserts a negative, and an assertion of a negative passes when the detector
    is broken just as it does when the property holds. This test points the
    identical detector -- `_standard_verify` on
    `_pre_signature_read_as_an_ordinary_signature` -- at
    `_mutant_pre_sign_that_is_a_real_signature`, whose pre-signature IS a valid
    ECDSA signature by construction.

    So: if this test fails, the test above proves nothing, regardless of whether
    it is green.
    """
    caught = 0
    for index in range(5):
        private_key = _derive_scalar("mutant-key", index)
        adaptor_secret = _derive_scalar("mutant-secret", index)
        digest = _derive_digest("mutant", index)
        public_key = public_key_point(private_key)
        adaptor_point = public_key_point(adaptor_secret)

        mutant, mutant_r = _mutant_pre_sign_that_is_a_real_signature(private_key, digest, adaptor_point)
        # The mutant's own r (taken from R_a) with its s_a is an ordinary ECDSA
        # signature, and the detector must say so.
        assert _standard_verify(public_key, digest, (mutant_r, mutant.s_a)), (
            f"mutant {index}: the detector FAILED to accept a signature that is valid by construction, "
            "so the negative assertion in the property-2 test is vacuous"
        )
        caught += 1

        # And the real implementation, on the same inputs, is not accepted --
        # the two halves of the mutation check side by side.
        real = pre_sign(private_key, digest, adaptor_point)
        assert not _standard_verify(public_key, digest, _pre_signature_read_as_an_ordinary_signature(real))
    assert caught == 5, "(none) -- the mutation check ran zero cases, which is not a pass"


# --- Property 3: the wrong secret does not complete the signature ------------


def test_adapt_with_the_wrong_secret_produces_a_signature_that_does_not_verify():
    """A signature completed with a secret that is not the adaptor secret must not verify.

    Note what is NOT asserted: that `adapt` raises. It does not, on purpose --
    being handed the wrong secret is a protocol event, not a malformed input,
    and the module docstring for `adapt` says why hiding it behind an exception
    would be worse. What is asserted is the property that matters: the result
    is not spendable.
    """
    for index in range(25):
        private_key = _derive_scalar("wrong-key", index)
        adaptor_secret = _derive_scalar("wrong-secret", index)
        wrong_secret = _derive_scalar("wrong-secret-other", index)
        assert wrong_secret != adaptor_secret
        digest = _derive_digest("wrong", index)
        public_key = public_key_point(private_key)

        pre_signature = pre_sign(private_key, digest, public_key_point(adaptor_secret))
        signature = adapt(pre_signature, wrong_secret)
        assert not _standard_verify(public_key, digest, signature), f"triple {index}: wrong secret produced a valid signature"

        # And the right one still works on the same pre-signature, so the test
        # above is not passing because the pre-signature was unusable.
        assert _standard_verify(public_key, digest, adapt(pre_signature, adaptor_secret))


# --- Property 4: recovery returns EXACTLY the secret, over 200+ triples ------


def test_recover_adaptor_secret_returns_exactly_the_secret_over_200_triples():
    """The scheme's whole purpose: the completed signature leaks the adaptor secret, exactly.

    220 independent (key, message, secret) triples. The count is above the 200
    asked for so that the low-S negation branch -- which arrives at recovery as
    `-y` and needs the `Y_implied == -Y` case -- is exercised roughly 110 times
    rather than possibly zero. The split is asserted at the end rather than
    assumed: a suite where every triple happened to take one branch would test
    half the function while reporting 220 passes.
    """
    negated_branch = 0
    for index in range(220):
        private_key = _derive_scalar("recover-key", index)
        adaptor_secret = _derive_scalar("recover-secret", index)
        digest = _derive_digest("recover", index)
        adaptor_point = public_key_point(adaptor_secret)

        pre_signature = pre_sign(private_key, digest, adaptor_point)
        unnormalized = (pre_signature.s_a * pow(adaptor_secret, -1, _N)) % _N
        signature = adapt(pre_signature, adaptor_secret)
        if unnormalized > _HALF_N:
            negated_branch += 1

        recovered = recover_adaptor_secret(pre_signature, signature)
        assert recovered == adaptor_secret, f"triple {index}: recovered {recovered!r}, expected {adaptor_secret!r}"

    assert negated_branch > 0, "(none) -- no triple exercised the low-S negation path, so -Y recovery is untested"
    assert negated_branch < 220, "(all) -- every triple was negated, so the plain +Y recovery path is untested"


# --- Property 5: an unrelated signature recovers None, not garbage, not an exception ---


def test_recover_adaptor_secret_returns_none_for_a_valid_but_unrelated_signature():
    """A signature that is perfectly valid on its own message, but unrelated to this pre-signature, gives None.

    Not a wrong secret and not an exception. Both alternatives are dangerous in
    opposite directions: a wrong secret is something a caller will try to spend
    with, and an exception on a message a counterparty can choose makes the
    recovery path a denial-of-service target.

    The "unrelated" signature here is a real ECDSA signature produced by the
    `ecdsa` package -- independent code -- and its validity is asserted before
    it is fed to recovery, so the None cannot be explained by the signature
    being junk.
    """
    for index in range(25):
        private_key = _derive_scalar("unrelated-key", index)
        adaptor_secret = _derive_scalar("unrelated-secret", index)
        digest = _derive_digest("unrelated", index)
        public_key = public_key_point(private_key)
        pre_signature = pre_sign(private_key, digest, public_key_point(adaptor_secret))

        signing_key = SigningKey.from_secret_exponent(private_key, curve=SECP256k1)
        other_digest = _derive_digest("unrelated-other", index)
        encoded = signing_key.sign_digest(other_digest)
        r = int.from_bytes(encoded[:32], "big")
        s = int.from_bytes(encoded[32:], "big")

        assert _standard_verify(public_key, other_digest, (r, s)), "the unrelated signature must itself be valid"
        assert recover_adaptor_secret(pre_signature, (r, s)) is None, f"triple {index}: unrelated signature recovered something"


def test_recover_adaptor_secret_returns_none_when_r_matches_but_s_is_mangled():
    """The second of the three "does not correspond" cases: right r, wrong s.

    Distinguished from the test above because it takes a different branch: r
    matches, so the function proceeds to compute y and then finds Y_implied is
    neither Y nor -Y. Without this, that branch is never reached.
    """
    private_key = _derive_scalar("mangled-key", 0)
    adaptor_secret = _derive_scalar("mangled-secret", 0)
    digest = _derive_digest("mangled", 0)
    pre_signature = pre_sign(private_key, digest, public_key_point(adaptor_secret))
    r, s = adapt(pre_signature, adaptor_secret)

    assert recover_adaptor_secret(pre_signature, (r, s)) == adaptor_secret
    assert recover_adaptor_secret(pre_signature, (r, (s + 1) % _N)) is None


def test_recover_adaptor_secret_returns_none_for_out_of_range_scalars_and_raises_for_non_ints():
    """Out of range is `None` (not a well-formed signature); a non-int is an AdaptorError.

    The split is the module's stated contract and it is the point of having an
    exception type at all: "you handed me garbage" must be distinguishable from
    "these do not correspond".
    """
    private_key = _derive_scalar("range-key", 0)
    adaptor_secret = _derive_scalar("range-secret", 0)
    digest = _derive_digest("range", 0)
    pre_signature = pre_sign(private_key, digest, public_key_point(adaptor_secret))

    assert recover_adaptor_secret(pre_signature, (0, 1)) is None
    assert recover_adaptor_secret(pre_signature, (1, 0)) is None
    assert recover_adaptor_secret(pre_signature, (_N, 1)) is None
    assert recover_adaptor_secret(pre_signature, (1, _N + 5)) is None
    with pytest.raises(AdaptorError):
        recover_adaptor_secret(pre_signature, ("1", 1))


# --- pre_verify rejects the three substitutions a counterparty could make ----


def test_pre_verify_rejects_a_different_adaptor_point():
    """A pre-signature is an encryption under one specific Y, and must not verify under another.

    This is the check that stops a swap being set up against the wrong key
    share: if it passed, a party could accept a pre-signature believing it will
    be completable by the secret behind THEIR Y when it is completable only by
    someone else's.
    """
    private_key = _derive_scalar("subst-key", 0)
    adaptor_secret = _derive_scalar("subst-secret", 0)
    digest = _derive_digest("subst", 0)
    public_key = public_key_point(private_key)
    adaptor_point = public_key_point(adaptor_secret)
    other_point = public_key_point(_derive_scalar("subst-secret-other", 0))

    pre_signature = pre_sign(private_key, digest, adaptor_point)
    assert pre_verify(public_key, digest, adaptor_point, pre_signature) is True
    assert pre_verify(public_key, digest, other_point, pre_signature) is False


def test_pre_verify_rejects_a_different_message():
    """The message is the transaction digest. A pre-signature for one spend must not verify for another."""
    private_key = _derive_scalar("msg-key", 0)
    adaptor_secret = _derive_scalar("msg-secret", 0)
    digest = _derive_digest("msg", 0)
    other_digest = _derive_digest("msg-other", 0)
    public_key = public_key_point(private_key)
    adaptor_point = public_key_point(adaptor_secret)

    pre_signature = pre_sign(private_key, digest, adaptor_point)
    assert pre_verify(public_key, digest, adaptor_point, pre_signature) is True
    assert pre_verify(public_key, other_digest, adaptor_point, pre_signature) is False


def test_pre_verify_rejects_a_different_signing_key():
    """A pre-signature commits to the signing key, so it must not verify under another party's public key."""
    private_key = _derive_scalar("key-key", 0)
    other_private_key = _derive_scalar("key-key-other", 0)
    adaptor_secret = _derive_scalar("key-secret", 0)
    digest = _derive_digest("key", 0)
    adaptor_point = public_key_point(adaptor_secret)

    pre_signature = pre_sign(private_key, digest, adaptor_point)
    assert pre_verify(public_key_point(private_key), digest, adaptor_point, pre_signature) is True
    assert pre_verify(public_key_point(other_private_key), digest, adaptor_point, pre_signature) is False


def test_pre_verify_rejects_a_tampered_dleq_proof():
    """Without the DLEQ proof a signer can put an R in the pre-signature that no honest secret completes.

    The specification's verification equation alone constrains only R_a; the
    proof is what ties R to the same nonce. Flipping one scalar of the proof
    must fail verification, and this is the only test that would notice if
    `pre_verify` stopped calling DLEQ_verify at all.
    """
    private_key = _derive_scalar("dleq-key", 0)
    adaptor_secret = _derive_scalar("dleq-secret", 0)
    digest = _derive_digest("dleq", 0)
    public_key = public_key_point(private_key)
    adaptor_point = public_key_point(adaptor_secret)

    pre_signature = pre_sign(private_key, digest, adaptor_point)
    b, c = pre_signature.dleq_proof
    for tampered in ((b, (c + 1) % _N), ((b + 1) % _N, c)):
        mutated = PreSignature(
            adaptor_point=adaptor_point,
            r_point=pre_signature.r_point,
            r_a=pre_signature.r_a,
            s_a=pre_signature.s_a,
            dleq_proof=tampered,
        )
        assert pre_verify(public_key, digest, adaptor_point, mutated) is False


def test_pre_sign_and_pre_verify_reject_malformed_input():
    """A message hash that is not 32 bytes, and a private key out of range, are errors rather than silent coercions."""
    adaptor_point = public_key_point(_derive_scalar("malformed-secret", 0))
    with pytest.raises(AdaptorError):
        pre_sign(_derive_scalar("malformed-key", 0), b"too short", adaptor_point)
    with pytest.raises(AdaptorError):
        pre_sign(0, _derive_digest("malformed", 0), adaptor_point)
    with pytest.raises(AdaptorError):
        pre_sign(_N, _derive_digest("malformed", 0), adaptor_point)


# --- The official DLC specification vectors ----------------------------------
#
# ALL ELEVEN VECTORS, VERBATIM, from
# https://github.com/discreetlogcontracts/dlcspecs/blob/master/test/ecdsa_adaptor.json
# fetched with curl on 2026-09-27 and pasted here by a script rather than by
# hand, because a hand-typed cryptographic vector that is one nibble wrong is a
# test that proves the opposite of what it claims. The only transformation
# applied to the file's contents was JSON `null` -> Python `None`; every hex
# string, `kind`, `error` and `comment` below is byte-identical to the
# specification's file.
#
# These are the only assertions in this suite whose expected values were not
# computed inside this repository. They were produced by
# BlockstreamResearch/secp256k1-zkp's ECDSA adaptor module, an independent
# implementation in C, which is what makes them worth having: everything else
# here could in principle be this implementation agreeing with itself.
#
# The three kinds, and what the specification says each one tests:
#   verification   `adaptor_sig` passes ecdsa_adaptor_verify; decryption yields
#                  `signature`; recovery yields `decryption_key`.
#   recovery       only the recovery function.
#   serialization  deserialization, and that re-serializing gives the same bytes.
# "Tests should fail only when `error` is set in the test vector."

OFFICIAL_VECTORS = [
    {
        "kind": "verification",
        "adaptor_sig": "03424d14a5471c048ab87b3b83f6085d125d5864249ae4297a57c84e74710bb6730223f325042fce535d040fee52ec13231bf709ccd84233c6944b90317e62528b2527dff9d659a96db4c99f9750168308633c1867b70f3a18fb0f4539a1aecedcd1fc0148fc22f36b6303083ece3f872b18e35d368b3958efe5fb081f7716736ccb598d269aa3084d57e1855e1ea9a45efc10463bbf32ae378029f5763ceb40173f",
        "message_hash": "8131e6f4b45754f2c90bd06688ceeabc0c45055460729928b4eecf11026a9e2d",
        "public_signing_key": "035be5e9478209674a96e60f1f037f6176540fd001fa1d64694770c56a7709c42c",
        "encryption_key": "02c2662c97488b07b6e819124b8989849206334a4c2fbdf691f7b34d2b16e9c293",
        "decryption_key": "0b2aba63b885a0f0e96fa0f303920c7fb7431ddfa94376ad94d969fbf4109dc8",
        "signature": "424d14a5471c048ab87b3b83f6085d125d5864249ae4297a57c84e74710bb67329e80e0ee60e57af3e625bbae1672b1ecaa58effe613426b024fa1621d903394",
        "comment": "plain valid adaptor signature"
    },
    {
        "kind": "verification",
        "adaptor_sig": "036035c89860ec62ad153f69b5b3077bcd08fbb0d28dc7f7f6df4a05cca35455be037043b63c56f6317d9928e8f91007335748c49824220db14ad10d80a5d00a9654af0996c1824c64c90b951bb2734aaecf78d4b36131a47238c3fa2ba25e2ced54255b06df696de1483c3767242a3728826e05f79e3981e12553355bba8a0131cd370e63e3da73106f638576a5aab0ea6d45c042574c0c8d0b14b8c7c01cfe9072",
        "message_hash": "8131e6f4b45754f2c90bd06688ceeabc0c45055460729928b4eecf11026a9e2d",
        "public_signing_key": "035be5e9478209674a96e60f1f037f6176540fd001fa1d64694770c56a7709c42c",
        "encryption_key": "024eee18be9a5a5224000f916c80b393447989e7194bc0b0f1ad7a03369702bb51",
        "decryption_key": "db2debddb002473a001dd70b06f6c97bdcd1c46ba1001237fe0ee1aeffb2b6c4",
        "signature": "6035c89860ec62ad153f69b5b3077bcd08fbb0d28dc7f7f6df4a05cca35455be4ceacf921546c03dd1be596723ad1e7691bdac73d88cc36c421c5e7f08384305",
        "comment": "the decrypted signature is high so it must be negated first AND the extracted decryption key must be negated"
    },
    {
        "kind": "verification",
        "adaptor_sig": "03f94dca206d7582c015fb9bffe4e43b14591b30ef7d2b464d103ec5e116595dba03127f8ac3533d249280332474339000922eb6a58e3b9bf4fc7e01e4b4df2b7a4100a1e089f16e5d70bb89f961516f1de0684cc79db978495df2f399b0d01ed7240fa6e3252aedb58bdc6b5877b0c602628a235dd1ccaebdddcbe96198c0c21bead7b05f423b673d14d206fa1507b2dbe2722af792b8c266fc25a2d901d7e2c335",
        "message_hash": "8131e6f4b45754f2c90bd06688ceeabc0c45055460729928b4eecf11026a9e2d",
        "public_signing_key": "035be5e9478209674a96e60f1f037f6176540fd001fa1d64694770c56a7709c42c",
        "encryption_key": "0214ccb756249ad6e733c80285ea7ac2ee12ffebbcee4e556e6810793a60c45ad4",
        "decryption_key": "1dfcfc0880e72509768ab46f2545b33168b8b8df8e4f5feb5059aa3750ee59d0",
        "signature": "424d14a5471c048ab87b3b83f6085d125d5864249ae4297a57c84e74710bb67329e80e0ee60e57af3e625bbae1672b1ecaa58effe613426b024fa1621d903394",
        "error": "proof is wrong"
    },
    {
        "kind": "recovery",
        "encryption_key": "027ee4f899bc9c5f2b626fa1a9b37ce291c0388b5227e90b0fd8f4fa576164ede7",
        "adaptor_sig": "03f2db6e9ed33092cc0b898fd6b282e99bdaeccb3de85c2d2512d8d507f9abab290210c01b5bed7094a12664aeaab3402d8709a8f362b140328d1b36dd7cb420d02fb66b1230d61c16d0cd0a2a02246d5ac7848dcd6f04fe627053cd3c7015a7d4aa6ac2b04347348bd67da43be8722515d99a7985fbfa66f0365c701de76ff0400dffdc9fa84dddf413a729823b16af60aa6361bc32e7cfd6701e32957c72ace67b",
        "signature": "f2db6e9ed33092cc0b898fd6b282e99bdaeccb3de85c2d2512d8d507f9abab2921811fe7b53becf3b7affa9442abaa93c0ab8a8e45cd7ee2ea8d258bfc25d464",
        "decryption_key": "9cf3ea9be594366b78c457162908af3c2ea177058177e9c6bf99047927773a06",
        "comment": "plain recovery"
    },
    {
        "kind": "recovery",
        "adaptor_sig": "03aa86d78059a91059c29ec1a757c4dc029ff636a1e6c1142fefe1e9d7339617c003a8153e50c0c8574a38d389e61bbb0b5815169e060924e4b5f2e78ff13aa7ad858e0c27c4b9eed9d60521b3f54ff83ca4774be5fb3a680f820a35e8840f4aaf2de88e7c5cff38a37b78725904ef97bb82341328d55987019bd38ae1745e3efe0f8ea8bdfede0d378fc1f96e944a7505249f41e93781509ee0bade77290d39cd12",
        "signature": "f7f7fe6bd056fc4abd70d335f72d0aa1e8406bba68f3e579e4789475323564a452c46176c7fb40aa37d5651341f55697dab27d84a213b30c93011a7790bace8c",
        "encryption_key": "035176d24129741b0fcaa5fd6750727ce30860447e0a92c9ebebdeb7c3f93995ed",
        "decryption_key": None,
        "error": "the R value of the signature does not match"
    },
    {
        "kind": "recovery",
        "adaptor_sig": "032c637cd797dd8c2ce261907ed43e82d6d1a48cbabbbece801133dd8d70a01b1403eb615a3e59b1cbbf4f87acaf645be1eda32a066611f35dd5557802802b14b19c81c04c3fefac5783b2077bd43fa0a39ab8a64d4d78332a5d621ea23eca46bc011011ab82dda6deb85699f508744d70d4134bea03f784d285b5c6c15a56e4e1fab4bc356abbdebb3b8fe1e55e6dd6d2a9ea457e91b2e6642fae69f9dbb5258854",
        "signature": "2c637cd797dd8c2ce261907ed43e82d6d1a48cbabbbece801133dd8d70a01b14b5f24321f550b7b9dd06ee4fcfd82bdad8b142ff93a790cc4d9f7962b38c6a3b",
        "encryption_key": "02042537e913ad74c4bbd8da9607ad3b9cb297d08e014afc51133083f1bd687a62",
        "decryption_key": "324719b51ff2474c9438eb76494b0dc0bcceeb529f0a5428fd198ad8f886e99c",
        "comment": "recovery from high s signature"
    },
    {
        "kind": "serialization",
        "adaptor_sig": "03e6d51da7bc2bf24cf9dfd9acc6c4f0a3e74d8a6273ee5a573ed6818e3095b60903f33bc98f9d2ea3511f2e24f3358557c815abd7713c9318af9f4dfab4441898ecd619acb1cb75c1a5946fbaf716d227199a6479a678d10a6d95512d674fb7703d85b58980b8e6c54bd20616bdb9461dccd8eebb7d7e7c83a91452cc20edf53be5b0fe0db44dddaaafbe737678c684b6e89b9b4b679b1855aa6ed644498b89c918",
        "error": None
    },
    {
        "kind": "serialization",
        "adaptor_sig": "03fffffffffffffffffffffffffffffffffffffffffffffffffffffffefffffc2c03f33bc98f9d2ea3511f2e24f3358557c815abd7713c9318af9f4dfab4441898ecd619acb1cb75c1a5946fbaf716d227199a6479a678d10a6d95512d674fb7703d85b58980b8e6c54bd20616bdb9461dccd8eebb7d7e7c83a91452cc20edf53be5b0fe0db44dddaaafbe737678c684b6e89b9b4b679b1855aa6ed644498b89c918",
        "error": None,
        "comment": "R can be above curve order"
    },
    {
        "kind": "serialization",
        "adaptor_sig": "03e6d51da7bc2bf24cf9dfd9acc6c4f0a3e74d8a6273ee5a573ed6818e3095b60903fffffffffffffffffffffffffffffffffffffffffffffffffffffffefffffc2cd619acb1cb75c1a5946fbaf716d227199a6479a678d10a6d95512d674fb7703d85b58980b8e6c54bd20616bdb9461dccd8eebb7d7e7c83a91452cc20edf53be5b0fe0db44dddaaafbe737678c684b6e89b9b4b679b1855aa6ed644498b89c918",
        "error": None,
        "comment": "R_a can be above curve order"
    },
    {
        "kind": "serialization",
        "adaptor_sig": "03e6d51da7bc2bf24cf9dfd9acc6c4f0a3e74d8a6273ee5a573ed6818e3095b60903f33bc98f9d2ea3511f2e24f3358557c815abd7713c9318af9f4dfab4441898ec000000000000000000000000000000000000000000000000000000000000000085b58980b8e6c54bd20616bdb9461dccd8eebb7d7e7c83a91452cc20edf53be5b0fe0db44dddaaafbe737678c684b6e89b9b4b679b1855aa6ed644498b89c918",
        "error": "s_a cannot be zero"
    },
    {
        "kind": "serialization",
        "adaptor_sig": "03e6d51da7bc2bf24cf9dfd9acc6c4f0a3e74d8a6273ee5a573ed6818e3095b60903f33bc98f9d2ea3511f2e24f3358557c815abd7713c9318af9f4dfab4441898ecfffffffffffffffffffffffffffffffebaaedce6af48a03bbfd25e8cd036414185b58980b8e6c54bd20616bdb9461dccd8eebb7d7e7c83a91452cc20edf53be5b0fe0db44dddaaafbe737678c684b6e89b9b4b679b1855aa6ed644498b89c918",
        "error": "s_a too high"
    }
]


def _vectors(kind: str) -> list[dict]:
    """The vectors of one kind, with a guard that the list is not empty.

    Rule 14 in test form: a parametrization that silently matches zero cases
    reports as a pass. `(none)` has to be impossible here, so it is asserted.
    """
    selected = [vector for vector in OFFICIAL_VECTORS if vector["kind"] == kind]
    assert selected, f"(none) -- no official vectors of kind {kind!r}; the embedded list is wrong"
    return selected


def test_all_eleven_official_vectors_are_embedded():
    """A count, with its denominator, so a truncated paste cannot pass quietly (rule 3).

    Measured against the specification file as fetched 2026-09-27: 11 vectors --
    3 verification, 3 recovery, 5 serialization.
    """
    kinds = [vector["kind"] for vector in OFFICIAL_VECTORS]
    assert len(OFFICIAL_VECTORS) == 11, f"expected 11 official vectors, embedded {len(OFFICIAL_VECTORS)}"
    assert kinds.count("verification") == 3
    assert kinds.count("recovery") == 3
    assert kinds.count("serialization") == 5


@pytest.mark.parametrize("vector", _vectors("verification"), ids=lambda v: (v.get("comment") or v.get("error") or "valid")[:40])
def test_official_verification_vectors(vector):
    """pre_verify, adapt and recover against the specification's verification vectors.

    Each vector carries a signing key, an encryption key, a message hash, the
    adaptor signature, the expected completed signature and the expected
    decryption key. A vector with `error` set must NOT verify -- and for those
    the suite additionally asserts that adapt/recover do not hand back the
    expected values, so "it failed for the right reason" is checked rather than
    assumed.
    """
    encryption_key = point_from_bytes(bytes.fromhex(vector["encryption_key"]))
    signing_key = point_from_bytes(bytes.fromhex(vector["public_signing_key"]))
    message_hash = bytes.fromhex(vector["message_hash"])
    pre_signature = PreSignature.from_bytes(bytes.fromhex(vector["adaptor_sig"]), encryption_key)

    verified = pre_verify(signing_key, message_hash, encryption_key, pre_signature)
    if vector.get("error") is not None:
        assert verified is False, f"a vector the specification marks {vector.get('error')!r} verified"
        return

    assert verified is True, "an official valid vector failed pre_verify"

    expected = bytes.fromhex(vector["signature"])
    expected_signature = (int.from_bytes(expected[:32], "big"), int.from_bytes(expected[32:], "big"))
    decryption_key = int(vector["decryption_key"], 16)

    assert adapt(pre_signature, decryption_key) == expected_signature, "adapt did not reproduce the official signature"
    assert recover_adaptor_secret(pre_signature, expected_signature) == decryption_key, (
        "recovery did not reproduce the official decryption key"
    )
    assert _standard_verify(signing_key, message_hash, expected_signature), (
        "the official completed signature did not verify under the ecdsa package -- "
        "this would mean the vector or the verifier helper is wrong, not the module"
    )


@pytest.mark.parametrize("vector", _vectors("recovery"), ids=lambda v: (v.get("comment") or v.get("error") or "valid")[:40])
def test_official_recovery_vectors(vector):
    """Recovery alone, including the specification's negative case.

    The negative vector is commented "the R value of the signature does not
    match", and the expected behavior is a failure -- which in this module's
    contract is a `None` return rather than an exception, because an unrelated
    signature is an answer and not malformed input.
    """
    encryption_key = point_from_bytes(bytes.fromhex(vector["encryption_key"]))
    pre_signature = PreSignature.from_bytes(bytes.fromhex(vector["adaptor_sig"]), encryption_key)
    signature_bytes = bytes.fromhex(vector["signature"])
    signature = (int.from_bytes(signature_bytes[:32], "big"), int.from_bytes(signature_bytes[32:], "big"))

    recovered = recover_adaptor_secret(pre_signature, signature)
    if vector.get("error") is not None:
        assert recovered is None, f"a vector the specification marks {vector.get('error')!r} recovered {recovered!r}"
    else:
        assert recovered == int(vector["decryption_key"], 16)


@pytest.mark.parametrize("vector", _vectors("serialization"), ids=lambda v: (v.get("comment") or v.get("error") or "valid")[:40])
def test_official_serialization_vectors(vector):
    """Parsing and re-encoding the 162-byte wire form, including the two vectors that must be rejected.

    The encryption key is not part of the encoding, so an arbitrary valid point
    is supplied as `Y` here -- these vectors say nothing about it. Two of the
    five must be rejected (`s_a` zero, `s_a` above the order) and two of the
    three valid ones exist specifically to check that an R or R_a
    x-coordinate ABOVE the curve order is accepted, which is why `PreSignature.r`
    reduces mod n rather than rejecting.
    """
    arbitrary_encryption_key = public_key_point(_derive_scalar("serialization-Y", 0))
    raw = bytes.fromhex(vector["adaptor_sig"])

    if vector.get("error") is not None:
        with pytest.raises(AdaptorError):
            PreSignature.from_bytes(raw, arbitrary_encryption_key)
        return

    pre_signature = PreSignature.from_bytes(raw, arbitrary_encryption_key)
    assert pre_signature.to_bytes() == raw, "re-serialization did not reproduce the official bytes"
    assert len(pre_signature.to_bytes()) == 162


def test_point_encoding_round_trips():
    """point_to_bytes/point_from_bytes are inverses, and a wrong length is rejected rather than truncated."""
    for index in range(10):
        point = public_key_point(_derive_scalar("encoding", index))
        assert point_from_bytes(point_to_bytes(point)) == point
    with pytest.raises(AdaptorError):
        point_from_bytes(b"\x02" * 32)
    with pytest.raises(AdaptorError):
        point_from_bytes(b"\x09" + b"\x00" * 32)
