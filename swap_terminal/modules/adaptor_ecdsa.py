"""ECDSA adaptor signatures on secp256k1: pre-sign, pre-verify, adapt, and recover the adaptor secret.

Role: function level (the bottom of CLAUDE.md rule 10's stack -- every name in
      this file is a pure function of its arguments)
Reads: nothing. No socket, no file, no environment, no clock, no database.
       The only external entropy is `secrets.token_bytes(32)` inside
       `_sample_nonce`, and that is documented at the call site.
Writes: nothing
Can move funds: no -- and this is the field that needs the longest answer, so
      it is answered under "WHAT THIS IS NOT ALLOWED TO BE USED FOR" below.
      Nothing here signs a transaction, selects a fee, derives an address, or
      touches a wallet. It is nonetheless FUND-ADJACENT in exactly the sense
      CLAUDE.md rule 16 means: a defect in `pre_verify` hands a counterparty a
      signature they should not have been able to complete, and a defect in
      `recover_adaptor_secret` loses the Monero key share that is the only
      thing making the swap atomic. Wrong output here is unrecoverable in the
      same way a broadcast transaction is.
Mainnet-safe: NO. Not because it can reach a chain -- it cannot, even if asked
      -- but because it is UNAUDITED cryptography, and "safe to run" is not the
      question a reader of this field is asking on a key-holding host. See the
      next section, which is the most important paragraph in the file.

UNAUDITED. NOT WIRED IN. NOT FOR MAINNET.

Three statements, each measured rather than hedged, on 2026-09-27:

  1. This is unaudited cryptography written by a single author (an LLM session)
     from a written specification, checked against that specification's own
     published test vectors and against the `ecdsa` package's verifier. It has
     had no review by a cryptographer and no third-party audit. Passing its own
     tests is not evidence of security; it is evidence of self-consistency plus
     agreement with eleven official vectors.
  2. NOTHING IN THIS TREE CALLS IT. Established by grepping every .py, .js,
     .sh, .md, .toml and .txt file outside node_modules for the strings
     `adaptor_ecdsa`, `pre_sign`, `pre_verify`, `recover_adaptor_secret` and
     `adapt(` -- the NAME grep CLAUDE.md rule 2 asks for, not an import-graph
     walk. The only hits are this file and tests/test_adaptor_ecdsa.py. There
     is no swap path, no route, no worker and no CLI entry point wired to it.
  3. It is therefore a PROPOSAL and not a fix, in rule 16's sense: the property
     that actually matters for a GRC<->XMR swap -- that a Gridcoin transaction
     carrying an adapted signature relays and confirms, and that the leaked
     scalar really is the counterparty's Monero key share -- cannot be tested
     from here. It needs a Gridcoin testnet, a Monero stagenet, and the other
     two components (the cross-curve discrete-log-equality proof, and the
     Monero 2-of-2 spend path) that do not exist yet.

WHAT THIS IS NOT ALLOWED TO BE USED FOR, AND WHY THE SPECIFICATION ITSELF SAYS SO

The DLC specification carries a warning in capitals, and it is not boilerplate:

    "WARNING: This scheme is applicable to DLCs only and should not be applied
    in another context without careful analysis. This is because each adaptor
    signature leaks the Diffie-Hellman key for the signing key `X` and the
    encryption key `Y`."

That leak is structural, not a bug: the pre-signature publishes `R = k*Y`
alongside `R_a = k*G` and a proof that the two share the same `k`, which is
exactly a Diffie-Hellman tuple. In the DLC application the warning is discharged
because `Y` is an oracle's anticipated signature point -- a value the oracle
demonstrably knows the discrete logarithm of, and one that is used once.

FOR A GRC<->XMR SWAP THAT DISCHARGE HAS TO BE RE-ARGUED AND IT IS NOT ARGUED
HERE. `Y` would be the counterparty's Monero key share commitment on secp256k1,
which is (a) not an oracle signature, (b) reused across the two legs of the swap
if the protocol is written carelessly, and (c) the thing whose discrete log the
counterparty must prove knowledge of -- the proof of knowledge the specification
says it omits *because* the DLC setting makes it free. In this setting it is not
free. Whether a single `Y` may safely receive more than one pre-signature under
the same signing key `x` is UNVERIFIED here and is the first question a reviewer
should be pointed at; what would settle it is the analysis in [Aum] (eprint
2020/476), which is the paper whose security proof the specification borrows and
which is where the proof-of-knowledge requirement comes from.

WHAT WAS IMPLEMENTED, AND FROM WHERE

    https://github.com/discreetlogcontracts/dlcspecs/blob/master/ECDSA-adaptor.md

That file -- the DLC specification's ECDSA adaptor signature document -- is the
source, and it was implemented clause by clause. It was fetched on 2026-09-27
and its algorithms are reproduced in the comments at each function so a reader
can diff code against spec without a browser. The specification's own references
for the construction:

    [Fou] Lloyd Fournier, "One-Time Verifiably Encrypted Signatures A.K.A.
          Adaptor Signatures"      https://github.com/LLFourn/one-time-VES/blob/master/main.pdf
    [Aum] Aumayr et al., "Generalized Bitcoin-Compatible Channels" (the
          security proof the spec adopts, and the source of the
          proof-of-knowledge requirement)   https://eprint.iacr.org/2020/476.pdf
    [Mor] Original mailing-list posting of the scheme
    [BIP62] low-S    https://github.com/bitcoin/bips/blob/master/bip-0062.mediawiki#low-s-values-in-signatures
    [BIP340] tagged hashes, and the nonce-generation advice this file follows
    [SEC1] point encoding

The specification's published test vectors --

    https://github.com/discreetlogcontracts/dlcspecs/blob/master/test/ecdsa_adaptor.json

-- are embedded in tests/test_adaptor_ecdsa.py and all eleven of them pass
against this code. That is the one piece of evidence here that is not this
implementation checking itself: those vectors were produced by
BlockstreamResearch/secp256k1-zkp's ECDSA adaptor module, an independent
implementation in C.

WHERE THIS DEVIATES FROM THE SPECIFICATION. Three places, all of them named:

  1. `sample_nonce` is instantiated, because the specification deliberately
     does not instantiate it ("We abandon some of the responsibility for this
     in this document for brevity and flexibility"). See `_sample_nonce`.
  2. `PreSignature` carries the encryption key `Y` as a field, so that
     `recover_adaptor_secret(pre_sig, sig)` needs only two arguments where the
     spec's `ecdsa_adaptor_recover(Y, a, sig)` takes three. `Y` is NOT part of
     the 162-byte wire encoding -- `to_bytes`/`from_bytes` match the spec
     exactly, and `from_bytes` therefore requires `Y` to be supplied. The
     recovery algorithm genuinely needs `Y`: without it the sign ambiguity
     introduced by low-S negation cannot be resolved, and the function would
     have to return a secret that is right up to sign, which is a footgun.
  3. `adapt()` applies low-S normalization unconditionally, which is what the
     spec's `ecdsa_adaptor_decrypt` does. See "LOW-S" below for why that is
     load-bearing for Gridcoin specifically and not merely conventional.

LOW-S, AND WHETHER GRIDCOIN MAKES IT MANDATORY

Measured 2026-09-27 by reading the Gridcoin source rather than recalling it,
`src/policy/policy.h` on two branches of gridcoin-community/Gridcoin-Research:

  master        SCRIPT_VERIFY_LOW_S is in STANDARD_SCRIPT_VERIFY_FLAGS and is
                NOT in MANDATORY_SCRIPT_VERIFY_FLAGS (which is P2SH | DERSIG |
                NULLDUMMY). The comment above the standard set says nodes do
                not ban peers for forwarding transactions that violate the
                non-mandatory rules. So on master a high-S signature is
                NON-STANDARD: it is a relay-policy failure, not a consensus
                failure.
  development   additionally defines V15_SCRIPT_VERIFY_FLAGS, which includes
                SCRIPT_VERIFY_LOW_S, and its comment states it is "activated as
                CONSENSUS at block version 15 by GetBlockScriptFlags()". Its
                own text records why nothing enforced it before: "pubkey.cpp
                normalizes high-S on verification and documents that
                enforcement belongs to SCRIPT_VERIFY_LOW_S, which nothing was
                setting."

So: low-S normalization is MANDATORY for any signature this code produces that
is meant to be broadcast on Gridcoin. On master it is mandatory because a
high-S transaction will not relay -- which for a swap is indistinguishable from
a failed leg, and worse, it is malleable, so a third party can flip S and get a
different txid for the same spend. Under the development branch's v15 rules it
is mandatory in the stronger consensus sense.

UNVERIFIED, and named as such (rule 17): which of those two branches the live
Gridcoin mainnet actually runs, and whether block version 15 is active on it.
That was not established from here -- it needs `getblockchaininfo` /
`getblock` against a live node, or the release notes of the deployed version.
The conclusion "normalize low-S" is the same either way, which is why the
question did not have to be settled to write the code.

WHY THE `ecdsa` PACKAGE AND NOTHING ELSE

`ecdsa` (pure Python, already declared in swap_terminal/requirements.txt at
`>=0.19,<1.0`, observed working at 0.19.2) provides the curve, the group
arithmetic, and the point encoding. NO DEPENDENCY WAS ADDED for this file.
`coincurve` and `secp256k1` are both bindings around libsecp256k1 and would put
a compiled extension on a host that holds BTC_RPC_PASS, LTC_RPC_PASS,
GRC_RPC_PASS and wallet keys; chains/solana_address.py already refused that
trade for the same reason and this file follows it.

The cost is honest and should be stated: pure-Python group arithmetic is not
constant time, so this code is NOT side-channel resistant. On a host where an
attacker can measure timing, the signing key is at risk. That is a second
reason this file is not mainnet-safe, and it is not fixable within the
no-compiled-extension constraint -- it is a genuine trade, not an oversight.
"""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass

from ecdsa.curves import SECP256k1
from ecdsa.ellipticcurve import INFINITY, PointJacobi

# --- Curve constants ---------------------------------------------------------
#
# Spelled once, here, rather than at each call site (CLAUDE.md rule 11's shape:
# one vocabulary, derived in one place). `_N` is the group order from the curve
# object rather than the literal
# 0xFFFFFFFF...BAAEDCE6AF48A03BBFD25E8CD0364141 that the specification quotes,
# so there is no second copy of it to drift.
CURVE = SECP256k1
_G = CURVE.generator
_N = CURVE.order
_CURVE_FP = CURVE.curve

# Half the order, for the BIP62 low-S test. `s` is "high" when s > n/2.
_HALF_N = _N // 2

# The DLEQ domain tag, built exactly as the specification says: "we set `tag` to
# SHA256("DLEQ") || SHA256("DLEQ")". This is BIP340's tagged-hash construction,
# and its purpose is that a hash computed for this proof system can never be
# reinterpreted as a hash from another one.
_DLEQ_TAG = hashlib.sha256(b"DLEQ").digest() * 2

# A separate tag for nonce derivation, which the specification does NOT define
# because it does not define `sample_nonce` at all. It is domain-separated from
# _DLEQ_TAG on purpose: the nonce input and the challenge input are both hashes
# over the same points, and if they shared a tag a challenge could collide with
# a nonce. This tag is an implementation choice and is listed among the
# deviations in the module docstring.
_NONCE_TAG = hashlib.sha256(b"swap_terminal/adaptor_ecdsa/nonce").digest() * 2

# Wire sizes, from the specification's encoding rules: points are the 33-byte
# compressed SEC1 form, scalars are 32-byte big-endian.
_POINT_BYTES = 33
_SCALAR_BYTES = 32
# The width of the auxiliary randomness mixed into a nonce. 32 bytes because
# that is the security level of the curve: fewer would cap the entropy of the
# nonce below the strength of the key it protects. Named rather than spelled as
# a literal at the two places it is used (ruff PLR2004).
_AUX_RAND_BYTES = 32
# a = R || R_a || s_a || (b || c)
PRE_SIGNATURE_BYTES = 2 * _POINT_BYTES + 3 * _SCALAR_BYTES


class AdaptorError(ValueError):
    """A malformed input: a scalar out of range, a point not on the curve, a bad length.

    Deliberately a ValueError subclass, and deliberately DISTINCT from the
    "these two things do not correspond" answer, which is a `None` return from
    `recover_adaptor_secret` and a `False` return from `pre_verify`.

    That split is the whole of CLAUDE.md rule 12's BLE001 argument applied to a
    return type rather than to an except clause. A caller must be able to tell
    "you handed me garbage" from "this pre-signature and this signature are
    honestly unrelated", because on a swap path they demand opposite actions:
    the first is a bug or a corrupt message and the second is a counterparty
    who published a signature that does not complete our pre-signature, which
    is a real protocol event. A single `return None` for both would make them
    indistinguishable, which is exactly the failure that rule quotes
    (`except Exception: return None` inside resolve_underlying_family()).
    """


# --- Encoding helpers --------------------------------------------------------


def point_to_bytes(point: PointJacobi) -> bytes:
    """Encode a non-infinite secp256k1 point as 33 compressed SEC1 bytes.

    The specification says implementations "should fail if they try and encode
    the point at infinity", and this does: the point at infinity has no
    coordinates to compress, and silently emitting 33 zero bytes would produce
    a wire message that decodes to something else entirely.
    """
    if point == INFINITY:
        raise AdaptorError("cannot encode the point at infinity")
    return point.to_bytes("compressed")


def point_from_bytes(data: bytes) -> PointJacobi:
    """Decode 33 compressed SEC1 bytes into a point, or raise AdaptorError.

    `PointJacobi.from_bytes` already rejects a point that is not on the curve
    and an out-of-field x-coordinate; this wrapper pins the length (so a
    64-byte uncompressed key cannot sneak in and be read as something else)
    and converts the package's exception type into this module's one, so a
    caller has a single class to catch.
    """
    if len(data) != _POINT_BYTES:
        raise AdaptorError(f"expected a {_POINT_BYTES}-byte compressed point, got {len(data)} bytes")
    try:
        point = PointJacobi.from_bytes(_CURVE_FP, bytes(data))
    except Exception as exc:
        # A broad catch, and CLAUDE.md rule 12's BLE001 argument is answered by
        # the re-raise rather than by a `noqa` -- ruff agrees, and reports the
        # suppression as unnecessary if one is written here (measured
        # 2026-09-27: RUF100 "Unused `noqa` directive (unused: BLE001)").
        # `from_bytes` raises
        # several unrelated types for a bad encoding -- MalformedPointError for
        # a bad prefix, and ValueError/AssertionError from the field arithmetic
        # for an x-coordinate with no square root -- and this handler does NOT
        # swallow the distinction the way rule 12 warns about: it re-raises, so
        # the caller cannot mistake a decode failure for a valid answer, and
        # `from exc` keeps the original type and message in the traceback.
        raise AdaptorError(f"not a valid compressed secp256k1 point: {exc}") from exc
    if point == INFINITY:
        raise AdaptorError("the point at infinity is not a valid key or nonce point")
    return point


def public_key_point(private_key: int) -> PointJacobi:
    """X = x*G. A convenience so callers do not each spell the multiplication."""
    return _G * _require_scalar(private_key, "private_key")


def _scalar_to_bytes(value: int) -> bytes:
    """32-byte big-endian, per the specification's scalar encoding."""
    return int(value).to_bytes(_SCALAR_BYTES, "big")


def _require_scalar(value: int, name: str) -> int:
    """Accept only a scalar in 1..n-1, which is what every scalar in this scheme must be.

    Zero is rejected everywhere, not as pedantry: a zero nonce makes `k⁻¹`
    undefined, a zero private key is not a key, and a zero decryption key would
    make `adapt()` divide by zero. The specification marks its DLEQ inputs
    **non-zero** in bold for the same reason.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise AdaptorError(f"{name} must be an int, got {type(value).__name__}")
    if not 1 <= value < _N:
        raise AdaptorError(f"{name} must be a scalar in 1..n-1")
    return value


def _require_message_hash(message_hash: bytes) -> bytes:
    """Accept only 32 bytes, because the caller must have hashed already.

    The specification: message_hash "**must** be the hash of the message (in
    our case a transaction digest)". Accepting a short string and padding it,
    or accepting a long one and truncating, is how a caller ends up signing a
    digest of something other than what they think -- and on this path the
    thing being signed is a transaction that moves coins.
    """
    if not isinstance(message_hash, (bytes, bytearray)):
        raise AdaptorError(f"message_hash must be bytes, got {type(message_hash).__name__}")
    if len(message_hash) != _SCALAR_BYTES:
        raise AdaptorError(f"message_hash must be exactly {_SCALAR_BYTES} bytes, got {len(message_hash)}")
    return bytes(message_hash)


def _message_scalar(message_hash: bytes) -> int:
    """m = scalar(message_hash): the digest read as a big-endian integer, reduced mod n.

    Reduction mod n is standard ECDSA and is not the same as rejecting a digest
    above the order: SHA256 output can exceed n (n is just below 2^256) and
    every ECDSA implementation reduces rather than refusing to sign. The
    specification's `scalar()` is this conversion.
    """
    return int.from_bytes(message_hash, "big") % _N


def _negate(point: PointJacobi) -> PointJacobi:
    """-P. Spelled as a helper only so `recover_adaptor_secret` reads like the spec."""
    return -point


# --- Nonce generation --------------------------------------------------------


def _sample_nonce(context: bytes, aux_rand: bytes | None = None) -> int:
    """The specification's `sample_nonce(byte_string)`, instantiated here because the spec does not instantiate it.

    THIS IS A RANDOMIZED NONCE, NOT RFC6979, AND THAT IS DELIBERATE.

    The specification says: "If `sample_nonce` is instantiated with a secure
    hash function then the resulting scheme is secure if we model it as a random
    oracle as shown by [Bel]. However, we recommend adding system randomness
    into the process as well." So a purely deterministic instantiation is
    permitted and a hash-plus-randomness one is recommended; this follows the
    recommendation.

    Why the recommendation is worth following here rather than taking the
    deterministic option for testability:

      - The deterministic input the spec names for signing is `Y || m || x`,
        i.e. the nonce is bound to the encryption key as well as the message.
        That binding is what stops the same `k` being reused across two
        different adaptor points, and re-deriving `k` deterministically is only
        as safe as the caller's discipline about passing the right `Y`. Mixing
        32 bytes of system randomness in means a mistake in that input cannot
        by itself produce nonce reuse across different messages, which is the
        failure that leaks the signing key outright.
      - RFC6979 is a deterministic ECDSA nonce over (private key, message). It
        does not know about `Y` at all. Using it unmodified here would be
        exactly the reuse hazard above. A "RFC6979 over (x, Y, m)" variant is
        no longer RFC6979, so calling it that would be a false citation.

    The randomness source is `secrets.token_bytes`, never `random`:
    `random.Random` is a Mersenne Twister, is seeded from a small state, and
    its output is fully predictable to anyone who sees 624 outputs. It is the
    wrong tool for a value that leaks a private key when guessed.

    `aux_rand` exists so that a test can pin an exact nonce and reproduce a
    known vector; production callers leave it None and get system randomness.
    It is NOT a way to supply a nonce directly -- the result is still hashed
    together with the context -- but a caller who passes a constant here gets a
    deterministic nonce and takes on the reuse risk themselves.

    The rejection loop handles the (astronomically unlikely, but not
    impossible) case of a digest that is zero or >= n. Returning a reduced
    value instead would bias the nonce; rejection does not.
    """
    if aux_rand is None:
        aux_rand = secrets.token_bytes(_AUX_RAND_BYTES)
    elif len(aux_rand) != _AUX_RAND_BYTES:
        raise AdaptorError(f"aux_rand must be exactly 32 bytes when supplied, got {len(aux_rand)}")

    for counter in range(256):
        digest = hashlib.sha256(_NONCE_TAG + context + aux_rand + counter.to_bytes(1, "big")).digest()
        candidate = int.from_bytes(digest, "big")
        if 1 <= candidate < _N:
            return candidate
    # Unreachable in practice: each iteration fails with probability under
    # 2^-127. Raising rather than looping forever means a broken hash shows up
    # as an error instead of a hang (rule 14: silence is a defect).
    raise AdaptorError("could not sample a nonce in 256 attempts; the hash function is broken")


# --- Proof of discrete logarithm equality ------------------------------------
#
# The specification's DLEQ proves that the discrete log of X to base G equals
# the discrete log of Z to base Y. In this scheme it is instantiated with
# (X, Y, Z) = (R_a, Y, R) and witness k, so it proves R = k*Y for the same k
# that R_a = k*G was built from.
#
# WHY THE PROOF IS THE PART THAT MAKES THE SCHEME WORK, since it is easy to read
# as ceremony. Without it, a pre-signature verifier can check that R_a is
# consistent with the signature equation but has no way to know that R -- the
# point whose x-coordinate becomes the final signature's `r` -- is k*Y for that
# same k. A cheating signer could publish an R that is k*Y' for some other Y'
# they control, and the verifier would accept a pre-signature that the honest
# adaptor secret can never complete. On a swap that is a funded leg the
# counterparty can never claim.


def _dleq_challenge(x_pt: PointJacobi, y_pt: PointJacobi, z_pt: PointJacobi, a_g: PointJacobi, a_y: PointJacobi) -> int:
    """b = H(X || Y || Z || A_G || A_Y), where H(x) = scalar(SHA256(tag || x)).

    Note the reduction mod n rather than rejection: the specification's
    `scalar()` conversion is used for the challenge, and a challenge is not a
    nonce -- a negligible bias in it does not leak the witness.
    """
    preimage = _DLEQ_TAG + b"".join(point_to_bytes(p) for p in (x_pt, y_pt, z_pt, a_g, a_y))
    return int.from_bytes(hashlib.sha256(preimage).digest(), "big") % _N


def _dleq_prove(
    witness: int,
    x_pt: PointJacobi,
    y_pt: PointJacobi,
    z_pt: PointJacobi,
    aux_rand: bytes | None = None,
) -> tuple[int, int]:
    """DLEQ_prove(x, (X, Y, Z)) -> (b, c), transcribed from the specification.

        Set `a` to `sample_nonce(tag || X || Y || Z || x)`
        Set `A_G` to `a * G`
        Set `A_Y` to `a * Y`
        Set `b` to  `H(X || Y || Z || A_G || A_Y)`
        Set `c` to `a + b * x`
        Set `proof` to `b || c`
    """
    _require_scalar(witness, "dleq witness")
    nonce_context = _DLEQ_TAG + b"".join(point_to_bytes(p) for p in (x_pt, y_pt, z_pt)) + _scalar_to_bytes(witness)
    a = _sample_nonce(nonce_context, aux_rand)
    a_g = _G * a
    a_y = y_pt * a
    b = _dleq_challenge(x_pt, y_pt, z_pt, a_g, a_y)
    c = (a + b * witness) % _N
    return b, c


def _dleq_verify(x_pt: PointJacobi, y_pt: PointJacobi, z_pt: PointJacobi, proof: tuple[int, int]) -> bool:
    """DLEQ_verify((X, Y, Z), proof), transcribed from the specification.

        Parse `proof` as two scalars `(b,c)`
        Set `A_G` to `c * G - b * X`
        Set `A_Y` to `c * Y - b * Z`
        Set `implied_b` to `H(X || Y || Z || A_G || A_Y)`
        Check `implied_b == b`

    Returns False rather than raising on a bad proof: a proof that does not
    verify is an answer, not a malformed input.
    """
    b, c = proof
    if not 0 <= b < _N or not 0 <= c < _N:
        return False
    # Written as `+ (-P)` rather than `-`: ecdsa's PointJacobi implements point
    # negation but NOT point subtraction, so the specification's `c * G - b * X`
    # has to be spelled as an addition of the negation. Measured 2026-09-27:
    # `PointJacobi.__sub__` does not exist and the spec-literal spelling raises
    # `TypeError: unsupported operand type(s) for -`.
    a_g = _G * c + (-(x_pt * b))
    a_y = y_pt * c + (-(z_pt * b))
    if INFINITY in (a_g, a_y):
        # A_G or A_Y at infinity has no compressed encoding, so the challenge
        # cannot be recomputed. That is a rejection, not a crash.
        return False
    return _dleq_challenge(x_pt, y_pt, z_pt, a_g, a_y) == b


# --- The adaptor signature ---------------------------------------------------


@dataclass(frozen=True)
class PreSignature:
    """An ECDSA adaptor signature: verifiable, completable by whoever knows the adaptor secret, and NOT an ECDSA signature.

    Fields, named as the specification names them:

      adaptor_point   `Y`, the encryption public key. NOT part of the wire
                      encoding (see the module docstring's deviation 2) -- it is
                      carried here so `recover_adaptor_secret` can resolve the
                      sign ambiguity that low-S normalization introduces.
      r_point         `R = k*Y`. Its x-coordinate mod n becomes the completed
                      signature's `r`.
      r_a             `R_a = k*G`. The point the signature equation is checked
                      against.
      s_a             `s_a = k⁻¹(m + r*x)`, the adaptor scalar. Note the `r`
                      inside it comes from `R`, not from `R_a` -- that
                      asymmetry is the whole trick, and getting it backwards
                      produces a scheme whose pre-signature IS a valid
                      signature.
      dleq_proof      `(b, c)`, proving R and R_a share the same k.

    Frozen because a PreSignature is a message that was sent or received. A
    mutable one invites a caller to "fix" a field after verification, at which
    point the verification result describes a different object than the one
    being used.

    WHY THIS IS NOT A SIGNATURE, stated here because it is the property whose
    failure loses money. `(r, s_a)` is not a valid ECDSA signature on the
    message: standard verification computes u1 = s_a⁻¹m, u2 = s_a⁻¹r and checks
    that the x-coordinate of u1*G + u2*X equals r -- but by construction
    u1*G + u2*X is `R_a`, and `r` is the x-coordinate of `R = k*Y`. Those agree
    only if Y = G, i.e. only if the adaptor secret is 1. So publishing a
    pre-signature does not publish a spendable signature; that is exactly what
    makes it safe to hand to a counterparty, and tests/test_adaptor_ecdsa.py
    pins it against the `ecdsa` package's own verifier rather than against this
    reasoning.
    """

    adaptor_point: PointJacobi
    r_point: PointJacobi
    r_a: PointJacobi
    s_a: int
    dleq_proof: tuple[int, int]

    @property
    def r(self) -> int:
        """The `r` of the completed signature: x-coordinate of R, reduced mod n.

        Reduced rather than rejected when above the order: the specification's
        serialization test vectors include one commented "R can be above curve
        order", so an x-coordinate in [n, p) is legal and must be reduced.
        """
        return self.r_point.x() % _N

    def to_bytes(self) -> bytes:
        """The specification's 162-byte encoding: R || R_a || s_a || b || c."""
        return b"".join(
            (
                point_to_bytes(self.r_point),
                point_to_bytes(self.r_a),
                _scalar_to_bytes(self.s_a),
                _scalar_to_bytes(self.dleq_proof[0]),
                _scalar_to_bytes(self.dleq_proof[1]),
            )
        )

    @classmethod
    def from_bytes(cls, data: bytes, adaptor_point: PointJacobi) -> PreSignature:
        """Parse the 162-byte encoding. `adaptor_point` is supplied separately because the encoding does not carry it.

        Strictness matches the specification's serialization vectors: `s_a`
        must be a scalar in 1..n-1 (there are vectors asserting failure for
        s_a = 0 and for s_a >= n), while the x-coordinates of R and R_a may
        exceed the curve order and are handled by field decoding.
        """
        if len(data) != PRE_SIGNATURE_BYTES:
            raise AdaptorError(f"a pre-signature is {PRE_SIGNATURE_BYTES} bytes, got {len(data)}")
        offset = 0

        def take(width: int) -> bytes:
            nonlocal offset
            chunk = data[offset : offset + width]
            offset += width
            return chunk

        r_point = point_from_bytes(take(_POINT_BYTES))
        r_a = point_from_bytes(take(_POINT_BYTES))
        s_a = _require_scalar(int.from_bytes(take(_SCALAR_BYTES), "big"), "s_a")
        b = int.from_bytes(take(_SCALAR_BYTES), "big")
        c = int.from_bytes(take(_SCALAR_BYTES), "big")
        if b >= _N or c >= _N:
            raise AdaptorError("DLEQ proof scalars must be below the curve order")
        return cls(adaptor_point=adaptor_point, r_point=r_point, r_a=r_a, s_a=s_a, dleq_proof=(b, c))


def pre_sign(
    private_key: int,
    message_hash: bytes,
    adaptor_point: PointJacobi,
    aux_rand: bytes | None = None,
) -> PreSignature:
    """ecdsa_adaptor_encrypt(x, Y, message_hash), transcribed from the specification.

        Set `k` to `sample_nonce(Y || message_hash || x)`
        Set `m` to `scalar(message_hash)`
        Set `R_a` to `k * G`
        Set `R` to `k * Y`
        Set `proof` to `DLEQ_prove(k, (R_a, Y, R))`
        Set `r` to the x-coordinate of `R` modulo `n`
        Set `s_a` to `k⁻¹ (m + r * x)`
        Set `a` to `R || R_a || s_a || proof`

    `aux_rand`, when supplied, is mixed into both nonces (the signing nonce k
    and the DLEQ nonce a) so a test can reproduce an exact pre-signature. The
    two nonces remain different values because their contexts differ.

    Raises AdaptorError on a degenerate result -- `r == 0` or `s_a == 0` --
    rather than returning it. Both are negligible-probability events that
    indicate either a broken nonce or a maliciously chosen Y, and an s_a of
    zero has no inverse, so `adapt` on it would divide by zero later, further
    from the cause.
    """
    x = _require_scalar(private_key, "private_key")
    digest = _require_message_hash(message_hash)
    if adaptor_point == INFINITY:
        raise AdaptorError("adaptor_point must not be the point at infinity")

    m = _message_scalar(digest)
    nonce_context = point_to_bytes(adaptor_point) + digest + _scalar_to_bytes(x)
    k = _sample_nonce(nonce_context, aux_rand)

    r_a = _G * k
    r_point = adaptor_point * k
    if r_point == INFINITY:
        raise AdaptorError("R = k*Y is the point at infinity; adaptor_point is not a valid group element")

    proof = _dleq_prove(k, r_a, adaptor_point, r_point, aux_rand)

    r = r_point.x() % _N
    if r == 0:
        raise AdaptorError("r is zero; retry with a different nonce")
    s_a = (pow(k, -1, _N) * (m + r * x)) % _N
    if s_a == 0:
        raise AdaptorError("s_a is zero; retry with a different nonce")

    return PreSignature(adaptor_point=adaptor_point, r_point=r_point, r_a=r_a, s_a=s_a, dleq_proof=proof)


def pre_verify(
    public_key: PointJacobi,
    message_hash: bytes,
    adaptor_point: PointJacobi,
    pre_signature: PreSignature,
) -> bool:
    """ecdsa_adaptor_verify(X, Y, message_hash, a), transcribed from the specification.

        Parse `a` as `(R, R_a, s_a, proof)`
        Check `DLEQ_verify((R_a, Y, R), proof)` or fail
        Set `m` to `scalar(message_hash)`
        Set `u_1` to `s_a⁻¹ * m`
        Set `u_2` to `s_a⁻¹ * r`
        Check `u_1 * G + u2 * X == R_a`

    Returns False for anything that does not verify, including a pre-signature
    made for a different message, a different signing key, or a different
    adaptor point. It raises only on a structurally malformed input (a
    message_hash that is not 32 bytes, an s_a out of range) -- see
    AdaptorError's docstring for why those two answers are kept apart.

    `adaptor_point` is taken as a parameter rather than read off the
    PreSignature on purpose. The verifier's question is "is this a valid
    encryption under the Y *I* expect", and reading Y out of the object being
    verified would make that check vacuous -- the pre-signature would always
    verify under its own claimed key. The two are compared below.
    """
    digest = _require_message_hash(message_hash)
    if INFINITY in (public_key, adaptor_point):
        return False
    if pre_signature.adaptor_point != adaptor_point:
        # A pre-signature built for a different Y. Checked explicitly so that
        # `adapt`/`recover_adaptor_secret`, which use the carried Y, cannot
        # operate on an object that passed verification against a different one.
        return False
    if not 1 <= pre_signature.s_a < _N:
        raise AdaptorError("s_a must be a scalar in 1..n-1")

    if not _dleq_verify(pre_signature.r_a, adaptor_point, pre_signature.r_point, pre_signature.dleq_proof):
        return False

    m = _message_scalar(digest)
    r = pre_signature.r
    if r == 0:
        return False
    s_a_inv = pow(pre_signature.s_a, -1, _N)
    u_1 = (s_a_inv * m) % _N
    u_2 = (s_a_inv * r) % _N
    return _G * u_1 + public_key * u_2 == pre_signature.r_a


def adapt(pre_signature: PreSignature, adaptor_secret: int) -> tuple[int, int]:
    """ecdsa_adaptor_decrypt(a, y) -> (r, s), transcribed from the specification.

        Set `s` to `s_a * y⁻¹`
        Negate `s` if it is high as specified in [BIP62]
        Set `r` to the x-coordinate of `R` modulo `n`
        Return `r || s`

    THIS DOES NOT CHECK THAT `adaptor_secret` IS THE DISCRETE LOG OF THE
    PRE-SIGNATURE'S `adaptor_point`, and that is deliberate rather than an
    omission. The specification's decrypt does not check it either, and there
    are two reasons not to add the check here:

      - It would hide a real protocol outcome behind an exception. Being handed
        a wrong secret is a thing a counterparty can do, and the caller's
        correct response is to notice that the resulting signature does not
        verify -- not to catch an exception.
      - A caller who wants the check has it for free: `adapt` then verify with
        a standard ECDSA verifier, which is what the counterparty's node will
        do anyway. Checking `y*G == Y` here would test a weaker property than
        the one that actually matters.

    So the contract is: garbage in (a well-formed scalar that is the wrong
    secret), garbage out (a well-formed signature that does not verify). A
    secret that is not a scalar in 1..n-1 still raises, because that is
    malformed rather than wrong.

    The low-S negation is NOT optional for Gridcoin -- see "LOW-S" in the module
    docstring for the measurement. It also has a consequence the caller must
    know about: after negation, `s⁻¹ * s_a` is `-y` rather than `y`, which is
    why `recover_adaptor_secret` has to test both signs.
    """
    y = _require_scalar(adaptor_secret, "adaptor_secret")
    s = (pre_signature.s_a * pow(y, -1, _N)) % _N
    if s == 0:
        raise AdaptorError("s is zero; this pre-signature and secret do not produce a signature")
    if s > _HALF_N:
        s = _N - s
    return pre_signature.r, s


def recover_adaptor_secret(pre_signature: PreSignature, signature: tuple[int, int]) -> int | None:
    """ecdsa_adaptor_recover(Y, a, sig) -> y, transcribed from the specification. `Y` comes from the PreSignature.

        Parse `sig` as `(r,s)`
        Set `r_implied` to the x-coordinate of `R` modulo `n`
        Check `r == r_implied` or fail
        Set `y` to `s⁻¹ * s_a`
        Set `Y_implied` to `y * G`
        If `Y_implied == Y` then return `y`
        Otherwise, if `Y_implied == -Y` then return `-y`
        Otherwise fail

    THIS IS THE FUNCTION THE WHOLE SWAP RESTS ON. In a GRC<->XMR swap the
    Gridcoin-side signature is broadcast to claim coins, and the act of
    broadcasting it leaks the scalar that lets the other party spend the Monero
    side. If this returns None where it should return a secret, a party who
    performed their half of the swap cannot claim the other half; Monero has no
    script, so there is no timelock refund to fall back on at the protocol
    level.

    Returns None -- never raises -- for a signature that simply does not
    correspond to this pre-signature, which is the case test 5 of the suite
    pins. "Does not correspond" covers three distinct situations and all three
    are one answer on purpose, because the caller's action is the same in each
    (treat the counterparty's signature as unrelated and do not proceed):

      - `r` does not match the pre-signature's R at all (an unrelated
        signature, possibly perfectly valid on its own message);
      - `r` matches but the implied Y is neither Y nor -Y (a mangled `s`);
      - `s` or `r` is outside 1..n-1 (not a well-formed signature).

    The `-Y` branch is not a curiosity: `adapt()` negates `s` for BIP62 low-S
    roughly half the time, and every one of those cases arrives here as -y.
    Dropping that branch would make recovery fail on about half of all real
    swaps -- a coin-flip for money, which is why the test suite runs 200+
    random triples rather than one.
    """
    r, s = signature
    if not isinstance(r, int) or not isinstance(s, int):
        raise AdaptorError("signature must be a pair of ints (r, s)")
    if not 1 <= r < _N or not 1 <= s < _N:
        return None
    if r != pre_signature.r:
        return None

    y = (pow(s, -1, _N) * pre_signature.s_a) % _N
    if y == 0:
        return None
    y_implied = _G * y
    if y_implied == pre_signature.adaptor_point:
        return y
    if y_implied == _negate(pre_signature.adaptor_point):
        return (-y) % _N
    return None
