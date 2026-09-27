"""ed25519 group and scalar arithmetic: the group law, x-recovery, scalars mod l, and the torsion check.

Role: function level (the bottom of CLAUDE.md rule 10's stack -- every name in
      this file is a pure function of its arguments, or a method on an
      immutable point)
Reads: nothing. No socket, no file, no environment, no clock, no database, and
      no entropy: there is not a single call to `secrets` or `random` here.
      Every output is a deterministic function of the inputs.
Writes: nothing. One module-level cache is filled on first use
      (`_BASEPOINT_WINDOW`, see `scalar_base_mul`); it is a memo of a pure
      computation and is not state a caller can observe except as speed.
Can move funds: no. Nothing here signs, encodes a transaction, derives an
      address, or opens a socket. It is nonetheless FUND-ADJACENT in exactly
      the sense CLAUDE.md rule 16 means, and more sharply than most files in
      this tree: these primitives exist to carry a cross-curve DLEQ proof about
      a Monero spend-key share (docs/dleq_cross_curve_design.md), and a defect
      in `decompress` or `is_torsion_free` is how a counterparty commits to one
      scalar on secp256k1 and a different one here, takes the Gridcoin, and
      leaves the XMR locked to a key nobody holds. That failure is silent and
      arrives after the money moved.
Mainnet-safe: NO. Not because it can reach a chain -- it cannot, even if asked
      -- but because it is UNAUDITED cryptography, and "safe to run" is not the
      question a reader of this field is asking on a key-holding host.

UNAUDITED. NOT WIRED IN. NOT FOR MAINNET.

Three statements, each measured rather than hedged, on 2026-09-27:

  1. This is unaudited cryptography written by a single author (an LLM session)
     from RFC 8032 and from the curve's defining equation. It has had no review
     by a cryptographer and no third-party audit. What it has is the strongest
     offline evidence available for this particular primitive, and the evidence
     is named in "HOW THIS WAS VERIFIED" below: RFC 8032 section 7.1's five
     published known-answer vectors, and agreement with libsodium on 2,000+
     random inputs across seven separate operations. Passing tests is not
     security; it is self-consistency plus agreement with two independent
     implementations.
  2. NOTHING IN THIS TREE CALLS IT. Established 2026-09-27 by grepping every
     .py, .js, .sh, .md, .json, .toml and .txt file outside node_modules and
     .git for `ed25519_group`, `scalar_base_mul`, `point_scalar_mul`,
     `is_torsion_free`, `has_small_order` and `RINGCT_H` -- the NAME grep
     CLAUDE.md rule 2 asks for, not an import-graph walk. The only hits are
     this file and tests/test_ed25519_group.py. There is no swap path, no
     route, no worker and no CLI entry point wired to it.
  3. It is therefore stage 1 of docs/dleq_cross_curve_design.md's section 6
     plan, and stage 1 alone. The DLEQ proof itself is NOT here, on purpose:
     that document's section 7 argues the case against building it at all, and
     nothing below prejudges that decision. What is below is the arithmetic
     that any answer to that question needs -- including "call out to a
     maintained implementation", which still needs a torsion check on this side
     of the boundary.

WHY THIS FILE EXISTS RATHER THAN A DEPENDENCY

`swap_terminal/requirements.txt` declares exactly two runtime dependencies,
`base58` and `ecdsa` (read 2026-09-27). NO DEPENDENCY WAS ADDED for this file
and none may be: `pynacl` and `cryptography` are both bindings around compiled
libraries, and `chains/solana_address.py` and `modules/adaptor_ecdsa.py` both
already refused a compiled extension on a host that holds BTC_RPC_PASS,
LTC_RPC_PASS, GRC_RPC_PASS and wallet keys. This file follows that refusal.

`pynacl` 1.4.0 IS importable in this container (measured 2026-09-27) and is used
by tests/test_ed25519_group.py as an ORACLE -- the same pattern this repo
already accepted for `solders` in test_solana_address.py and for
`cryptoconditions` in test_xrp_crypto_condition.py. An oracle in a test is not
a dependency of the code under test, and those tests skip when it is absent.

THE ONE HONEST COST, STATED THE SAME WAY adaptor_ecdsa.py STATES IT

Pure-Python group arithmetic is not constant time, so this code is NOT
side-channel resistant. `point_scalar_mul` below walks 4-bit windows of the
scalar and indexes a table; Python's integers are variable-width, `int.__mul__`
is not constant time, and the interpreter reorders nothing predictably. On a
host where an attacker can measure timing, a secret scalar passed through here
is at risk. That is not fixable within the no-compiled-extension constraint --
it is a genuine trade, not an oversight -- and it is a second reason this file
is not mainnet-safe. docs/dleq_cross_curve_design.md section 4.4 is the longer
version of this paragraph and it is worse for the DLEQ prover than for anything
here, because the prover branches on the individual bits of the secret.

WHAT THE CURVE IS, SPELLED OUT SO THE CODE CAN BE DIFFED AGAINST RFC 8032

edwards25519 is the twisted Edwards curve

    -x^2 + y^2 = 1 + d*x^2*y^2        over F_p, p = 2^255 - 19, d = -121665/121666

Its group has order 8*l where l = 2^252 + 27742317777372353535851937790883648493
(RFC 8032 section 5.1, and the same constant appears in this repo's
docs/dleq_cross_curve_design.md section 2.1). COFACTOR 8 IS THE WHOLE REASON
`is_torsion_free` EXISTS: a point can satisfy the curve equation, encode
canonically, and still not lie in the prime-order subgroup the DLEQ's claim is
about. See that function.

A point is encoded in 32 bytes: the low 255 bits are y little-endian, the top
bit is the low bit of x. Decoding therefore has to RECOVER x from y, which is
the step `chains/solana_address.py` deliberately does not take.

WHAT IS SHARED WITH chains/solana_address.py, AND WHY IT IS AN IMPORT

CLAUDE.md rule 8: when you touch a rule, grep for it, and let one survivor own
the concept. The field prime and the curve constant d are defined in
`chains/solana_address.py` (which computes d rather than spelling a 77-digit
literal, for the reason its comment gives) and this module IMPORTS them rather
than re-deriving them. Two copies of `d` would be rule 8's bug with a delay on
it: they agree on the day they are written, and a reader who found one would
never be told the other existed.

What is NOT shared, with the reason, because rule 8 asks for the difference to
be named at the site:

  `_raw_is_on_curve`   answers "is this y on the curve" with Euler's criterion,
                       one modular exponentiation, and returns False for BOTH a
                       non-canonical encoding and an off-curve point. That
                       fused answer is exactly right for its caller -- Solana
                       asks one question -- and unusable here, because
                       `decompress` must distinguish the two failures and must
                       produce x, not a boolean. Recovering x with
                       `pow(x2, (p+3)//8, p)` establishes membership as a
                       by-product (the candidate root is squared and compared),
                       so calling `_raw_is_on_curve` first would be a second
                       exponentiation to learn something the first one settles.
                       No logic is duplicated: one file decides membership,
                       this one decides membership-and-x.
  `is_canonical`       here is a separate public function precisely because
                       `_raw_is_on_curve` cannot answer it separately, and
                       because a DLEQ verifier has to reject a non-canonical
                       encoding BEFORE it does any arithmetic
                       (docs/dleq_cross_curve_design.md section 3.4).

Whether the two field constants should move to a shared module is a real
question and it is NOT answered here: moving them would edit a file on the
Solana payout path to serve a file nothing calls, which is the wrong direction
for that trade. It is named in this session's report instead.

HOW THIS WAS VERIFIED (CLAUDE.md rule 17: run the thing that would show it
false). Every item below is a test in tests/test_ed25519_group.py and the
numbers were measured on 2026-09-27:

  RFC 8032 section 7.1   all five published vectors (TEST 1, 2, 3, 1024 and
                         SHA(abc)) pass, both directions: the public key is
                         re-derived from the secret key through
                         `scalar_base_mul` + `compress`, and the signature is
                         verified through `decompress` (x-recovery),
                         `scalar_from_bytes_le` over a 64-byte hash, the group
                         law and `point_scalar_mul`. The RFC text was fetched
                         from two unrelated GitHub mirrors whose sha256 agreed
                         byte for byte (ed63657f...c4c3, 103,210 bytes); the
                         canonical www.rfc-editor.org and datatracker.ietf.org
                         are both blocked by this container's egress proxy.
  libsodium (pynacl)     agreement on 512 random point additions, 512 random
                         subtractions (via negate-then-add), 256 random
                         variable-base multiplications, 256 fixed-base
                         multiplications, 512 scalar reductions of 64-byte
                         values, 256 scalar inversions, and point validity on
                         1,024 random 32-byte strings. 0 mismatches.
  go-dleq vectors        tests/vectors/dleq_cross_curve_go.json, produced by
                         Go's filippo.io/edwards25519: all three ed25519 points
                         reproduced byte for byte from their little-endian
                         witnesses.
  the basepoint          DERIVED here (y = 4/5, x even) and then checked against
                         RFC 8032 section 5.1's two decimal literals. Not
                         copied from anywhere.
  the eight small-order  DERIVED here (l*P for an arbitrary curve point has
  encodings              order dividing 8) and cross-checked against
                         libsodium's independent algebraic test.

MEASURED SPEED, and whether the design document's projection held. Rule 6: all
timings in microfortnights, 1µfn = 1.2096s, seconds in parentheses. Measured
2026-09-27 in this container, Python 3.11.15, single core, 200 operations per
figure:

    variable-base, 4-bit window, table per call     0.00120µfn (1.45ms)
    fixed-base, persistent basepoint window table   0.00105µfn (1.27ms)
    decompress (x-recovery)                         0.0000496µfn (0.060ms)
    is_torsion_free (one full l*P)                  0.00126µfn (1.52ms)

docs/dleq_cross_curve_design.md section 2.3 measured 0.00129µfn (1.56ms) for
the first of those and 0.00117µfn (1.42ms) for the second, on a hand-rolled
benchmark outside this repository. This file is 7% and 11% faster respectively,
which is agreement rather than a finding: same algorithm, same interpreter,
different loop overhead. THE PROJECTION HELD. Nothing here refutes that
document's 2.18µfn (2.64s) verify and 1.91µfn (2.31s) prove, and nothing here
confirms them either -- no proof exists to time, which is what that document
already says.

One optimization that is worth naming because the obvious version is much
worse: `is_torsion_free` costs a full 253-bit scalar multiplication, 1.52ms,
and a DLEQ verifier that called it on all 252 commitments per curve would spend
0.317µfn (383ms) on nothing else. The cheap form is to check the whole
weighted-sum result once, or to multiply by the cofactor and compare -- see the
note in that function. This file provides the correct check and states its
cost; choosing where to call it is the caller's decision and belongs to the
code that has one.
"""

from __future__ import annotations

from dataclasses import dataclass

# CLAUDE.md rule 8: the field prime and the curve constant d have ONE owner in
# this tree and it is chains/solana_address.py, which computes d rather than
# spelling a 77-digit literal. Importing them is the alternative to a second
# copy that agrees today and drifts later. That module is pure -- it imports
# hashlib and base58 and touches no environment, file or socket at import time
# (read 2026-09-27), so this import has no side effect, which is the thing
# CLAUDE.md rule 12 asks about and that a linter cannot check.
from swap_terminal.chains.solana_address import CURVE_D, FIELD_PRIME

# --- Group constants ---------------------------------------------------------

# The order of the prime-order subgroup, l. RFC 8032 section 5.1 writes it as
# 2^252 + 27742317777372353535851937790883648493, and it is written the same way
# here rather than as a hex literal so it can be diffed against the RFC and
# against docs/dleq_cross_curve_design.md section 2.1 by eye.
GROUP_ORDER = 2**252 + 27742317777372353535851937790883648493

# The full curve has 8*l points. Every ed25519 pitfall that is not an encoding
# pitfall comes from this number not being 1.
COFACTOR = 8

# A compressed point and a scalar are both 32 bytes, and they are named
# separately because they are different things that happen to share a size --
# `scalar_from_bytes_le` also accepts 64, and conflating the two is how a
# 64-byte hash gets read as a point.
POINT_BYTES = 32
SCALAR_BYTES = 32
# RFC 8032's SHA-512 challenge is reduced from 64 bytes. Named rather than
# spelled at the two sites that check it (ruff PLR2004).
WIDE_SCALAR_BYTES = 64

# sqrt(-1) mod p, needed by x-recovery. p = 2^255 - 19 is 5 mod 8, so
# 2^((p-1)/4) is a square root of -1; computed at import rather than written as
# a literal, for chains/solana_address.py's stated reason (a literal is a thing
# that can be mistyped and a thing no reader can check).
_SQRT_MINUS_ONE = pow(2, (FIELD_PRIME - 1) // 4, FIELD_PRIME)

# 2*d, the constant the extended-coordinate addition formula actually wants.
# Precomputed because it appears in every single point addition.
_D2 = (2 * CURVE_D) % FIELD_PRIME

# Window width for scalar multiplication. 4 bits means a 16-entry table and 63
# window steps for a 253-bit scalar; docs/dleq_cross_curve_design.md section
# 2.3 measured this shape and it is what the numbers in this module's docstring
# were taken against. Not a tuning knob anybody should turn without measuring:
# 5 bits halves the number of additions and doubles the table-building cost,
# and which wins depends on whether the table is reused.
_WINDOW_BITS = 4
_WINDOW_SIZE = 1 << _WINDOW_BITS
_WINDOW_MASK = _WINDOW_SIZE - 1


class Ed25519Error(ValueError):
    """A malformed input: a bad length, a non-canonical encoding, an off-curve point, a scalar that is not an int.

    Deliberately a ValueError subclass, and deliberately DISTINCT from the
    "this is a valid point and the answer is no" results, which are the `False`
    returns from `is_canonical`, `is_torsion_free` and `has_small_order`.

    That split is CLAUDE.md rule 12's BLE001 argument applied to a return type
    rather than to an except clause, and it is the same split
    `modules/adaptor_ecdsa.py::AdaptorError` makes for the same reason. A caller
    must be able to tell "you handed me 31 bytes" from "that is a real point
    and it has a torsion component", because on a swap path they demand
    opposite actions: the first is a bug or a truncated message, the second is
    a counterparty sending something a verifier must refuse. A single False for
    both makes a malicious input indistinguishable from a corrupt one.
    """


# --- Scalars mod l -----------------------------------------------------------
#
# Every function in this section takes and returns a plain Python int in
# [0, l). There is no Scalar class, on purpose: a wrapper would add a type to
# check at every boundary and would buy nothing that `scalar_reduce` does not,
# and the one thing a wrapper WOULD buy -- making it impossible to hand an
# unreduced integer to a point multiplication -- is provided instead by
# `point_scalar_mul` reducing its own argument. See `scalar_from_bytes_le` for
# the failure this is guarding against.


def scalar_reduce(value: int) -> int:
    """`value` mod l, for any Python int including a negative one.

    Python's `%` already returns a non-negative result for a positive modulus,
    so this is one operation; it exists as a named function anyway because
    "reduce mod the GROUP order" and "reduce mod the FIELD prime" are two
    different reductions on this curve and writing `% GROUP_ORDER` inline at
    twenty call sites is how one of them eventually becomes the other.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise Ed25519Error(f"a scalar must be an int, got {type(value).__name__}")
    return value % GROUP_ORDER


def scalar_add(left: int, right: int) -> int:
    """(left + right) mod l."""
    return (scalar_reduce(left) + scalar_reduce(right)) % GROUP_ORDER


def scalar_sub(left: int, right: int) -> int:
    """(left - right) mod l.

    Present although the task for this file named only add/mul/inverse, because
    MRL-0010's forced last blinder is a NEGATED sum
    (docs/dleq_cross_curve_design.md section 1.3(b), which records that the
    PDF's extracted text drops the minus sign and that go-dleq's code has it).
    A caller writing `scalar_add(a, GROUP_ORDER - b)` by hand is a caller who
    will one day write `a - b` and get a negative int; this is four characters
    cheaper than the bug.
    """
    return (scalar_reduce(left) - scalar_reduce(right)) % GROUP_ORDER


def scalar_mul(left: int, right: int) -> int:
    """(left * right) mod l.

    NOTE THE NAME BOUNDARY, because it is the one place this module's vocabulary
    could be misread: `scalar_mul` multiplies TWO SCALARS in Z_l, and
    `point_scalar_mul` multiplies a point by a scalar. Some libraries spell the
    second one `scalar_mul`; this module cannot, because both operations are
    needed and rule 8 forbids two spellings of one concept just as firmly as it
    forbids one spelling of two.
    """
    return (scalar_reduce(left) * scalar_reduce(right)) % GROUP_ORDER


def scalar_inverse(value: int) -> int:
    """The multiplicative inverse of `value` mod l.

    Raises for zero rather than returning zero. l is prime, so every non-zero
    residue has an inverse and zero has none; `pow(0, -1, l)` raises in Python
    3.8+ but with a message about a modular inverse rather than about a scalar,
    and MRL-0010's `(2^{n-1})^{-1}` term is the caller that would see it.
    """
    reduced = scalar_reduce(value)
    if reduced == 0:
        raise Ed25519Error("zero has no inverse mod l")
    return pow(reduced, -1, GROUP_ORDER)


def scalar_from_bytes_le(data: bytes) -> int:
    """Read 32 or 64 bytes as a LITTLE-ENDIAN integer and reduce it mod l.

    LITTLE-ENDIAN IS IN THE NAME BECAUSE GETTING IT WRONG IS SILENT. RFC 8032
    and Monero both encode ed25519 scalars little-endian; SEC1 and this repo's
    `modules/adaptor_ecdsa.py::_scalar_to_bytes` encode secp256k1 scalars
    big-endian. A cross-curve DLEQ carries both conventions in one message
    (docs/dleq_cross_curve_design.md section 5.5 flags exactly this as
    unverified in go-dleq's wire format), and a byte-reversed scalar is still a
    perfectly good scalar: it produces a valid point, a valid proof about the
    wrong secret, and no error anywhere. The only defense is that the
    convention is impossible to call by accident, which is why this function is
    not named `scalar_from_bytes`.

    THIS REDUCES, AND THAT IS THE RIGHT BEHAVIOR FOR THE TWO CALLERS THAT
    EXIST, both of which are RFC 8032's:

      - the 64-byte SHA-512 challenge `SHA512(R || A || M)`, which the RFC
        reduces mod l ("interpret ... as an integer in little-endian, reduce
        modulo L");
      - the 32-byte clamped secret scalar, which is below 2^254 and above l, so
        RFC 8032's own key derivation depends on reduction happening somewhere.

    IT IS THE WRONG BEHAVIOR FOR A WIRE-FORMAT SCALAR AND THIS IS THE TRAP
    docs/dleq_cross_curve_design.md section 3 is about. A 32-byte field in a
    received proof whose value is >= l must be REJECTED, not reduced: reducing
    it silently accepts two distinct encodings of the same scalar, which is a
    malleability bug, and on the DLEQ path it is worse than that -- the whole
    construction is only well-formed for 0 <= x < l, so a verifier that reduces
    is answering a question nobody asked. Use `is_canonical_scalar_le` first
    and refuse; that function exists for this and has no other caller.

    Anything other than 32 or 64 bytes raises rather than being padded or
    truncated, for `adaptor_ecdsa._require_message_hash`'s stated reason: a
    caller who is handed a short buffer and gets a padded scalar has signed a
    digest of something other than what they think.
    """
    if not isinstance(data, (bytes, bytearray)):
        raise Ed25519Error(f"scalar bytes must be bytes, got {type(data).__name__}")
    if len(data) not in (SCALAR_BYTES, WIDE_SCALAR_BYTES):
        raise Ed25519Error(
            f"a little-endian scalar must be {SCALAR_BYTES} or {WIDE_SCALAR_BYTES} bytes, got {len(data)}. "
            "Padding or truncating would produce a valid scalar for a value nobody supplied."
        )
    return int.from_bytes(data, "little") % GROUP_ORDER


def scalar_to_bytes_le(value: int) -> bytes:
    """A reduced scalar as 32 little-endian bytes, the inverse of `scalar_from_bytes_le`.

    Reduces first, so the output is always canonical (below l) and the round
    trip `scalar_from_bytes_le(scalar_to_bytes_le(k)) == k mod l` holds for any
    int. A 32-byte output cannot represent an unreduced value anyway, so
    refusing one instead of reducing would gain nothing but a raise.
    """
    return scalar_reduce(value).to_bytes(SCALAR_BYTES, "little")


def is_canonical_scalar_le(data: bytes) -> bool:
    """True if `data` is 32 bytes encoding a little-endian integer strictly below l.

    The check `scalar_from_bytes_le` deliberately does not do. Its docstring
    says why this matters and who has to call it: every 32-byte scalar arriving
    from a counterparty, before it is used for anything.

    Returns False rather than raising for a wrong length, because the caller's
    question is "may I use this field" and the answer for 31 bytes is no.
    """
    if not isinstance(data, (bytes, bytearray)) or len(data) != SCALAR_BYTES:
        return False
    return int.from_bytes(data, "little") < GROUP_ORDER


# --- Points ------------------------------------------------------------------


@dataclass(frozen=True)
class Point:
    """A point on edwards25519 in extended coordinates (X : Y : Z : T), with x = X/Z, y = Y/Z, T = XY/Z.

    WHY EXTENDED COORDINATES AND NOT AFFINE (x, y). Affine addition on a
    twisted Edwards curve needs a modular inverse -- one 255-bit exponentiation,
    measured here at 0.060ms, which is 40% of a whole windowed scalar
    multiplication -- per addition. Extended coordinates defer every inversion
    to the single one that `compress` does at the end, which is why a
    scalarmult costs 1.45ms instead of tens of milliseconds. The formulas below
    are Hisil-Wong-Carter-Dawson 2008 ("Twisted Edwards curves revisited"),
    which is what every ed25519 implementation uses, including the one
    docs/dleq_cross_curve_design.md section 2.3 benchmarked.

    FROZEN, because a mutable point is a shared-reference bug waiting for a
    caller who reuses one. `add` and `double` return new points; nothing here
    modifies anything.

    NOT COMPARABLE WITH THE DATACLASS-GENERATED __eq__, which is why `eq=False`
    is set and `__eq__` is written by hand. Projective coordinates are not
    unique: (X:Y:Z:T) and (2X:2Y:2Z:2T) are the same point and have different
    field elements in them. A generated field-by-field `__eq__` would report two
    equal points as unequal, which on the DLEQ path is a verifier that rejects
    an honest proof -- and would do it intermittently, since whether two
    computations of one point land on the same representative depends on the
    path taken to each.
    """

    __slots__ = ("X", "Y", "Z", "T")

    X: int
    Y: int
    Z: int
    T: int

    # `eq=False` is passed via __init_subclass__-free dataclass config below;
    # see the class docstring for why the generated __eq__ would be wrong.

    def __eq__(self, other: object) -> bool:
        """Projective equality: X1*Z2 == X2*Z1 and Y1*Z2 == Y2*Z1.

        Four multiplications and no inversion. Comparing affine coordinates
        instead would cost two inversions (0.12ms) to answer the same question,
        and this is called once per bit per curve by any DLEQ verifier.
        """
        if not isinstance(other, Point):
            return NotImplemented
        return (self.X * other.Z - other.X * self.Z) % FIELD_PRIME == 0 and (
            self.Y * other.Z - other.Y * self.Z
        ) % FIELD_PRIME == 0

    def __hash__(self) -> int:
        """Hash the affine form, so that equal points hash equally.

        A frozen dataclass with a hand-written __eq__ must define __hash__ or it
        becomes unhashable, and hashing the projective fields would break the
        invariant that a == b implies hash(a) == hash(b). This pays one modular
        inversion; nothing on the DLEQ path needs points in a set, and a wrong
        hash is a bug that shows up as a lookup miss weeks later.
        """
        return hash(self.to_affine())

    # --- construction ---

    @classmethod
    def identity(cls) -> Point:
        """The neutral element, affine (0, 1).

        On a twisted Edwards curve the identity is an ordinary point with
        ordinary coordinates -- there is no separate point at infinity and no
        special case anywhere in the group law below. That is the structural
        advantage of this curve shape over secp256k1's short Weierstrass form,
        where `modules/adaptor_ecdsa.py` has to check for INFINITY at every
        encode.
        """
        return cls(0, 1, 1, 0)

    @classmethod
    def from_affine(cls, x: int, y: int) -> Point:
        """A point from affine coordinates, WITHOUT checking it is on the curve.

        Private-by-convention despite the public name: the two callers are
        `decompress` (which has already established membership by recovering x)
        and the derivation of the two generators below. A caller that has an
        (x, y) pair from anywhere else wants `decompress`, which checks.
        """
        x %= FIELD_PRIME
        y %= FIELD_PRIME
        return cls(x, y, 1, (x * y) % FIELD_PRIME)

    def to_affine(self) -> tuple[int, int]:
        """(x, y) with the projective Z divided out. Costs one modular inversion.

        Raises for Z == 0, which cannot happen for any point this module
        produces: the HWCD formulas below never yield Z == 0 for inputs on the
        curve, because d is a non-square mod p and that is exactly the condition
        that makes them complete (no exceptional cases). The check is here
        because "cannot happen" and "is checked" are different claims and this
        file is on a fund-adjacent path.
        """
        if self.Z == 0:
            raise Ed25519Error("a point with Z == 0 has no affine form; this should be unreachable")
        z_inv = pow(self.Z, -1, FIELD_PRIME)
        return (self.X * z_inv % FIELD_PRIME, self.Y * z_inv % FIELD_PRIME)

    def is_identity(self) -> bool:
        """True for the neutral element, by projective comparison against (0 : 1 : 1 : 0).

        Written as X == 0 and Y == Z rather than `self == Point.identity()` so
        it is two comparisons instead of four multiplications; this is called
        once per `is_torsion_free`, which a DLEQ verifier calls per received
        point.
        """
        return self.X % FIELD_PRIME == 0 and (self.Y - self.Z) % FIELD_PRIME == 0

    # --- the group law ---

    def add(self, other: Point) -> Point:
        """P + Q, by the HWCD 2008 "add-2008-hwcd-3" formulas for a = -1.

        COMPLETE, WITH NO SPECIAL CASES. This one method handles P + Q for
        distinct points, P + P, P + (-P) = identity, and P + identity, and it
        does so because d is a non-square mod p -- the denominators in the
        Edwards addition law cannot vanish for points on the curve. That is why
        there is no `if self == other: return self.double()` branch here, and
        why there must not be one: such a branch is both unnecessary and a
        timing oracle on whether two secret points coincide.

        `double()` exists anyway, and it is not a duplicate of this (rule 8):
        it is a genuinely different and cheaper formula, 4 squarings against 9
        multiplications, and the difference is measurable across the 63 window
        steps of a scalar multiplication.
        """
        p = FIELD_PRIME
        a = (self.Y - self.X) * (other.Y - other.X) % p
        b = (self.Y + self.X) * (other.Y + other.X) % p
        c = self.T * _D2 * other.T % p
        d = self.Z * 2 * other.Z % p
        e = b - a
        f = d - c
        g = d + c
        h = b + a
        return Point(e * f % p, g * h % p, f * g % p, e * h % p)

    def double(self) -> Point:
        """2P, by the HWCD 2008 "dbl-2008-hwcd" formulas for a = -1.

        Cheaper than `self.add(self)` because it trades multiplications for
        squarings and never touches T as an input -- which also means it is
        correct for a point whose T is stale, although nothing here produces
        one. Measured contribution: a 253-bit scalar multiplication does 252
        doublings and at most 63 additions, so this formula is where most of the
        time in this module is spent.
        """
        p = FIELD_PRIME
        a = self.X * self.X % p
        b = self.Y * self.Y % p
        c = 2 * self.Z * self.Z % p
        h = a + b
        e = h - (self.X + self.Y) ** 2 % p
        g = a - b
        f = c + g
        return Point(e * f % p, g * h % p, f * g % p, e * h % p)

    def negate(self) -> Point:
        """-P, which on a twisted Edwards curve is (-x, y): negate X and T, leave Y and Z.

        No field inversion, no conditional, and `P.add(P.negate())` is the
        identity by the completeness argument in `add`. That identity is one of
        the property tests in tests/test_ed25519_group.py, and it is worth
        testing rather than assuming precisely because it is the case a
        Weierstrass implementation has to special-case.
        """
        p = FIELD_PRIME
        return Point((-self.X) % p, self.Y, self.Z, (-self.T) % p)

    def subtract(self, other: Point) -> Point:
        """P - Q. `self.add(other.negate())`, named because a verifier writes it constantly.

        MRL-0010's verifier recomputes `e1 * (C - G')` per bit per curve
        (docs/dleq_cross_curve_design.md section 2.3 counts it), so this is one
        of the two hottest operations in the construction this module exists
        for. It is not a second implementation of anything: it is two calls.
        """
        return self.add(other.negate())

    # --- encoding ---

    def compress(self) -> bytes:
        """The 32-byte RFC 8032 encoding: y little-endian in the low 255 bits, the low bit of x on top.

        Costs the one modular inversion that extended coordinates deferred.

        There is no "point at infinity" case to refuse the way
        `modules/adaptor_ecdsa.py::point_to_bytes` has to: the identity is
        (0, 1) and encodes as 0100...00, which is a perfectly valid 32-byte
        point that decompresses back to the identity. Whether the IDENTITY is
        acceptable in a given field of a protocol message is a protocol
        question, not an encoding one -- and MRL-0010's soundness list has
        "replace one commitment with the group identity" as its own mutation
        test (that document's section 5.3, item 5), which is where the refusal
        belongs.
        """
        x, y = self.to_affine()
        return (y | ((x & 1) << 255)).to_bytes(POINT_BYTES, "little")

    @classmethod
    def decompress(cls, encoding: bytes) -> Point:
        """Decode 32 bytes into a point, RECOVERING x from y and the sign bit, or raise Ed25519Error.

        THIS IS THE STEP chains/solana_address.py DELIBERATELY DOES NOT TAKE
        ("It does not recover x, because nothing here needs x"). Deciding
        membership needs only a Legendre symbol; getting a usable point needs
        the square root itself.

        The arithmetic, so it can be diffed against RFC 8032 section 5.1.3:

            y  = the low 255 bits, little-endian;  sign = bit 255
            x^2 = (y^2 - 1) / (d*y^2 + 1)
            x   = (x^2)^((p+3)/8), corrected by sqrt(-1) if that is the wrong root
            if the low bit of x disagrees with `sign`, x = p - x

        The `(p+3)/8` exponent is a square root only because p = 5 mod 8: it
        returns either a root of v or a root of -v, and the single correction by
        sqrt(-1) covers the second case. If neither candidate squares back to
        x^2, no point with this y exists and this raises. That comparison is the
        membership test, so no separate Euler's-criterion call is made -- see
        this module's docstring on what is and is not shared with
        chains/solana_address.py.

        THREE SEPARATE REFUSALS, and they are separate because
        docs/dleq_cross_curve_design.md section 3.4 lists them as three
        distinct checks a Python implementation has to do by hand:

          wrong length      31 or 33 bytes is not a short point, it is a
                            framing bug in whatever produced the message.
          non-canonical     y >= p, or x == 0 with the sign bit set. See
                            `is_canonical`, which is the same test exposed as a
                            predicate for a caller that must refuse before it
                            decodes.
          off the curve     x^2 is not a square mod p.

        What this does NOT check is subgroup membership: the returned point may
        have a torsion component and still be a legitimate decode. That is
        `is_torsion_free`'s question, it is deliberately a separate call, and
        the reason is in that function.
        """
        if not isinstance(encoding, (bytes, bytearray)):
            raise Ed25519Error(f"a compressed point must be bytes, got {type(encoding).__name__}")
        if len(encoding) != POINT_BYTES:
            raise Ed25519Error(f"a compressed point is {POINT_BYTES} bytes, got {len(encoding)}")
        raw = int.from_bytes(encoding, "little")
        y = raw & ((1 << 255) - 1)
        sign = raw >> 255
        if y >= FIELD_PRIME:
            raise Ed25519Error(
                "non-canonical point encoding: y is not reduced mod p. "
                "Two encodings of one point is a malleability bug, and curve25519-dalek, "
                "libsodium and this repo's chains/solana_address.py all reject these."
            )
        x = _recover_x(y, sign)
        if x is None:
            raise Ed25519Error("not a point on edwards25519: the implied x^2 has no square root mod p")
        return cls.from_affine(x, y)


# `Point` is declared with the default dataclass `eq=True`, which would generate
# a field-by-field __eq__ that is WRONG for projective coordinates (see the
# class docstring). Defining __eq__ in the class body already overrides it --
# dataclass only installs its own when the name is absent -- and this assertion
# pins that, because the failure mode is a verifier that rejects honest proofs
# and it would be introduced by a one-word change to the decorator.
assert Point.__eq__ is not object.__eq__, "Point must use its own projective __eq__"


def _recover_x(y: int, sign: int) -> int | None:
    """The x matching this y and sign bit, or None if no point on the curve has this y.

    Split out of `Point.decompress` for one reason and it is not style: this is
    the decision (CLAUDE.md rule 10 -- the smallest, most testable piece at the
    bottom), and a test can call it with a y it constructed rather than having
    to build a 32-byte encoding to reach it. `decompress` is the framing around
    it.

    Returns None rather than raising, because "this y has no x" is an ANSWER
    here and the caller turns it into the error message with the context.
    """
    p = FIELD_PRIME
    y_squared = y * y % p
    numerator = (y_squared - 1) % p
    denominator = (CURVE_D * y_squared + 1) % p
    if denominator == 0:
        # No point on the curve has this y: the defining equation gives x^2 an
        # infinite value. Unreachable for ed25519 in practice -- it would need
        # y^2 == -1/d, and -1/d is a non-square mod p -- but a claim that
        # something is unreachable is not the same as checking it, and the
        # alternative is a ZeroDivisionError from pow() on a fund-adjacent path.
        return None
    x_squared = numerator * pow(denominator, -1, p) % p
    if x_squared == 0:
        # y = +-1, i.e. the identity and the point of order 2. x = 0 is a
        # genuine root here, and it has only ONE encoding: sign bit 0. A sign
        # bit of 1 would mean "the negative of zero", which is zero, so the
        # encoding is non-canonical and is refused rather than silently read as
        # the same point. libsodium and curve25519-dalek both refuse it, and a
        # verifier that accepted it would accept two distinct 32-byte strings
        # for one commitment.
        return 0 if sign == 0 else None
    x = pow(x_squared, (p + 3) // 8, p)
    if x * x % p != x_squared:
        x = x * _SQRT_MINUS_ONE % p
    if x * x % p != x_squared:
        return None
    if x & 1 != sign:
        x = p - x
    return x


def is_canonical(encoding: bytes) -> bool:
    """True if `encoding` is the ONE 32-byte string that RFC 8032 assigns to the point it denotes.

    Rejects, and these are the only two ways a point can have a second
    encoding on this curve:

      1. y >= p. The encoding carries 255 bits of y but the field has only
         2^255 - 19 elements, so 19 y-values are representable twice. This is
         the family curve25519-dalek, libsodium and ZIP-215 all discuss, and
         the one chains/solana_address.py::_raw_is_on_curve already refuses
         (its comment: "curve25519-dalek rejects these, so this matches what
         the chain itself would do").
      2. x == 0 with the sign bit set. Negative zero is zero, so the two
         encodings 0100..00 / 0100..80 and the two for y = p-1 denote the same
         two points. Rarely stated and cheap to check.

    MEASURED 2026-09-27 by enumerating every y in [p, 2^255) and every
    sign bit, and by checking both sign bits for x == 0: exactly 25 of the 2^256
    possible 32-byte strings are non-canonical encodings OF A VALID CURVE
    POINT -- 19 y-values above p of which 23 (y, sign) pairs decode, plus the
    2 negative-zero cases. The test pins the count; the number is small enough
    that a reader can check it by hand and large enough that "there are no
    non-canonical encodings" is wrong.

    Returns False rather than raising for a wrong length or an off-curve point,
    because the caller's question is "may I use this 32-byte field" and the
    answer for garbage is no. A caller that needs to know WHY calls
    `Point.decompress` and reads the exception.

    WHY THIS IS A DLEQ REQUIREMENT AND NOT HYGIENE. MRL-0010 closes with one
    line -- "the verifier is assumed to have also checked each proof tuple
    element to ensure it belongs to the expected group" -- and
    docs/dleq_cross_curve_design.md section 3.4 expands that line into the three
    checks this module provides. A proof whose commitments are re-encodable is a
    proof whose Fiat-Shamir transcript can be re-spelled, which is a second
    valid proof for the same statement and, depending on the protocol wrapped
    around it, a replay.
    """
    if not isinstance(encoding, (bytes, bytearray)) or len(encoding) != POINT_BYTES:
        return False
    raw = int.from_bytes(encoding, "little")
    y = raw & ((1 << 255) - 1)
    sign = raw >> 255
    if y >= FIELD_PRIME:
        return False
    x = _recover_x(y, sign)
    if x is None:
        # Either off the curve, or x == 0 with the sign bit set -- _recover_x
        # returns None for both, and both answers here are False, so they do not
        # need to be distinguished. `Point.decompress` distinguishes them in its
        # message for a caller that is diagnosing rather than filtering.
        return False
    return True


# --- Scalar multiplication ---------------------------------------------------


def _window_table(point: Point) -> list[Point]:
    """[0*P, 1*P, ..., 15*P], the lookup table for 4-bit windowed multiplication.

    Fifteen additions, built by repeated addition rather than by doubling
    chains, because at this size the difference is one addition and the code is
    shorter than the optimization.
    """
    table = [Point.identity(), point]
    for _ in range(2, _WINDOW_SIZE):
        table.append(table[-1].add(point))
    return table


def _scalar_mul_with_table(scalar: int, table: list[Point]) -> Point:
    """k*P given a precomputed window table for P. The one multiplication loop in this module.

    Most-significant window first, so the accumulator is doubled four times per
    window and the table entry is added once: 63 windows for a 253-bit scalar,
    252 doublings and up to 63 additions.

    NOT CONSTANT TIME, and the leak is specific enough to name: the loop body
    always runs, and always adds -- including a table[0] identity add for a zero
    window, which is deliberate, because skipping it would leak the zero
    windows through timing far more loudly than indexing does. What remains is
    that `table[window]` is a data-dependent memory access and that Python's
    big-integer multiplication time depends on the operand values. See this
    module's docstring; that leak is not closable here.

    The single early return is for k == 0, and it is not a timing concession:
    the loop would return the identity anyway, and the branch exists so that
    `table` being a 16-entry list of identities (which happens when P is the
    identity) is not mistaken for a bug by a reader.
    """
    if scalar == 0:
        return Point.identity()
    accumulator = Point.identity()
    # 253 bits is l's width; round up to a whole number of windows so the
    # highest window is not silently dropped for a scalar near l.
    windows = (GROUP_ORDER.bit_length() + _WINDOW_BITS - 1) // _WINDOW_BITS
    for index in range(windows - 1, -1, -1):
        for _ in range(_WINDOW_BITS):
            accumulator = accumulator.double()
        accumulator = accumulator.add(table[(scalar >> (index * _WINDOW_BITS)) & _WINDOW_MASK])
    return accumulator


def point_scalar_mul(scalar: int, point: Point) -> Point:
    """k*P for an arbitrary point. Measured 0.00120µfn (1.45ms) per call, table built per call.

    The scalar is REDUCED mod l first. That is a deliberate choice and it is
    safe here for a reason worth writing down: the group has order 8*l, so
    reducing changes the answer for a point with a torsion component --
    k*P and (k mod l)*P differ by (k div l)*(the torsion part). For a
    torsion-free point they are identical, and for a point that is not
    torsion-free the claim being computed was already meaningless
    (docs/dleq_cross_curve_design.md section 3.4). A caller that needs the
    honest 8*l-group answer -- `is_torsion_free` below is the only one -- must
    not come through here, and it does not.

    Named `point_scalar_mul` and not `scalar_mul`: see `scalar_mul`, which is
    the mod-l multiply of two scalars and cannot share the name.
    """
    return _scalar_mul_with_table(scalar_reduce(scalar), _window_table(point))


# Filled on first use by `scalar_base_mul` and never invalidated. This is a memo
# of a pure computation of a module constant, not mutable state: the table for
# the basepoint is the same table forever. It is built lazily rather than at
# import because 15 point additions at import time is a cost every consumer of
# this module pays whether or not it multiplies anything, and CLAUDE.md rule 12
# is specifically about import-time work.
_BASEPOINT_WINDOW: list[Point] | None = None


def scalar_base_mul(scalar: int) -> Point:
    """k*B against the ed25519 basepoint. Measured 0.00105µfn (1.27ms) per call.

    Faster than `point_scalar_mul(k, BASEPOINT)` by the 15 additions of the
    window table, which the basepoint only ever needs built once. Measured
    difference 1.45ms -> 1.27ms, a 12% saving for one cached list;
    docs/dleq_cross_curve_design.md section 4.1 records the same shape of win
    (a factor of 3.5) for `ecdsa`'s fixed-base precompute on secp256k1, where
    the table is much larger.
    """
    global _BASEPOINT_WINDOW  # noqa: PLW0603
    if _BASEPOINT_WINDOW is None:
        _BASEPOINT_WINDOW = _window_table(BASEPOINT)
    return _scalar_mul_with_table(scalar_reduce(scalar), _BASEPOINT_WINDOW)


# --- Subgroup membership -----------------------------------------------------


def is_torsion_free(point: Point) -> bool:
    """True if `point` lies in the prime-order subgroup: l*P == identity.

    THE TREE HAD NO SUCH CHECK BEFORE THIS FILE. Measured 2026-09-27 by
    grepping every .py in the repository for `torsion`, `cofactor` and
    `GROUP_ORDER`: the only ed25519 arithmetic was
    chains/solana_address.py, which needs curve membership and not subgroup
    membership, and does not contain l at all.
    docs/dleq_cross_curve_design.md section 3.4 records the same measurement and
    names what depends on it: `sigma_fun`'s cross-curve verifier calls
    `is_torsion_free()` on the claimed key as the very first thing it does,
    before the bit count check, and MRL-0010 delegates it in the one line
    quoted in `is_canonical`.

    WHY A CURVE CHECK IS NOT ENOUGH. ed25519 has cofactor 8. A point with a
    torsion component satisfies the curve equation, encodes canonically, and is
    NOT in the group any discrete-log claim is about: if A = x*B + T with T of
    order 8, then A is a fine-looking public key for which "the discrete log of
    A base B is x" is false, and eight different x values become
    indistinguishable to a verifier that does not check. On a cross-curve DLEQ
    that is the entire failure the proof exists to prevent.

    THE COST, because it is the reason this is a separate call rather than
    something `decompress` does for free: one full 253-bit scalar
    multiplication, measured 0.00126µfn (1.52ms). Multiplying by l is the
    CORRECT check and this function does it. Two cheaper forms exist and both
    answer a different question, so neither is used here:

      - `(8*P).is_identity()` is `has_small_order` below, and it is the
        complement of nothing: a point can be neither small-order nor
        torsion-free (B + T is the example, and the test pins it).
      - Multiplying the whole equation by 8 -- "cofactored" verification, what
        ZIP-215 and libsodium's batch verification do -- avoids the check
        entirely by making the torsion component irrelevant to the outcome.
        That is the right engineering answer for signature verification and it
        is NOT available to a DLEQ verifier, because the claim there is about a
        specific scalar and not about the existence of one.

    So a caller that verifies 252 commitments per curve must not call this 252
    times (0.317µfn / 383ms of pure overhead); it checks what the protocol
    requires -- the claimed keys, and the sums -- and that choice belongs in the
    code that has a protocol, which is not this file.
    """
    return _scalar_mul_with_table(GROUP_ORDER, _window_table(point)).is_identity()


def has_small_order(point: Point) -> bool:
    """True if `point` is one of the eight points killed by the cofactor: 8*P == identity.

    Three doublings, measured 0.0000165µfn (0.020ms) -- 76 times cheaper than
    `is_torsion_free`, which is why both exist.

    NOT THE COMPLEMENT OF `is_torsion_free`, AND READING IT AS ONE IS THE BUG
    THIS DOCSTRING EXISTS TO PREVENT. The three cases are all real:

        B              torsion-free, not small order   the honest case
        T (order 8)    small order, not torsion-free   the attack input
        B + T          NEITHER                          the interesting one

    A verifier that refuses small-order points and accepts everything else
    accepts B + T, which is precisely the point a malicious prover would send:
    it looks like a key, it is not in the subgroup, and no amount of
    small-order blacklisting catches it. libsodium's
    `crypto_core_ed25519_is_valid_point` checks BOTH (read 2026-09-27 at
    src/libsodium/crypto_core/ed25519/ref10/ed25519_ref10.c: canonical, on
    curve, `ge25519_has_small_order`, and `ge25519_is_on_main_subgroup`), and
    that is the shape a caller here should copy.

    The eight points this detects are the 8-torsion subgroup, and
    tests/test_ed25519_group.py DERIVES all eight of their canonical encodings
    from this module's own arithmetic rather than pasting a published list --
    for an arbitrary curve point P, l*P has order dividing 8 -- and then
    cross-checks them against libsodium.
    """
    return point.double().double().double().is_identity()


# --- The two published generators --------------------------------------------
#
# CLAUDE.md rule 17 and the task both ask where these came from, so each one
# says. One is DERIVED and checked against a published literal; the other is a
# published literal and cannot be derived here, and the difference is stated.

# B, the ed25519 basepoint. DERIVED, NOT COPIED. RFC 8032 section 5.1 defines it
# as the point with y = 4/5 and "the unique ... x ... where x is positive" --
# positive meaning the sign bit is 0, i.e. x even. So:
#
#     y = 4 * 5^-1 mod p,  x = the even square root of (y^2-1)/(d*y^2+1)
#
# and that is what the two lines below compute. MEASURED 2026-09-27: the result
# equals RFC 8032 section 5.1's own two decimal literals,
#   x = 15112221349535400772501151409588531511454012693041857206046113283949847762202
#   y = 46316835694926478169428394003475163141307993866256225615783033603165251855960
# and compresses to 5866666666666666666666666666666666666666666666666666666666666666,
# which is byte for byte the `one` vector's ed25519 point in
# tests/vectors/dleq_cross_curve_go.json -- produced by Go's
# filippo.io/edwards25519, a different implementation in a different language.
# The test pins all three of those comparisons, so a wrong basepoint cannot
# survive here; deriving it means there is no 78-digit literal in this file for
# anybody to mistype.
_BASEPOINT_Y = 4 * pow(5, -1, FIELD_PRIME) % FIELD_PRIME
_BASEPOINT_X = _recover_x(_BASEPOINT_Y, 0)
assert _BASEPOINT_X is not None, "the basepoint y must have a recoverable x"
BASEPOINT = Point.from_affine(_BASEPOINT_X, _BASEPOINT_Y)

# H, Monero's RingCT alternate generator. A PUBLISHED CONSTANT, NOT DERIVED, and
# the difference from B matters:
#
# H was originally produced by Monero's hash-to-point applied to the encoding of
# the scalar 1. Reproducing that derivation needs Keccak-256, which is NOT in
# Python's standard library (measured: `hashlib.algorithms_available` contains no
# name matching keccak, and `sha3_256(b"")` is a7ffc6f8... where Keccak-256 of
# the empty string is c5d24601... -- different padding, and reaching for
# sha3_256 because the name looks right is a silent break).
# docs/dleq_cross_curve_design.md section 4.3 argues that avoiding Keccak
# entirely is the better trade, and using the published constant is how: it is
# the one place in this module where a literal is the honest choice, because the
# alternative is 100 lines of unaudited Keccak to re-derive a value two
# independent published sources already agree on.
#
# The two sources, both fetched and read 2026-09-27:
#   monero-project/research-lab, source-code/MiniNero/RingCT.py line 10,
#     `def getHForCT(): return "8b655970...9c1f94"` -- with the dead code beneath
#     the return showing the hashToPoint_ct(publicFromInt(1)) derivation it
#     replaced. That is the Monero Research Lab's own reference code.
#   AthanorLabs/go-dleq, ed25519/curve.go line 32, `const str = "8b655970..."`
#     inside `altBasePoint()` -- an unrelated implementation of the very
#     construction this module is being built for.
# They agree byte for byte, which is the only check available without Keccak,
# and the test asserts this module decompresses it to a canonical, on-curve,
# TORSION-FREE point. That last property is the one that would actually break
# the DLEQ if it failed, and it is checked rather than assumed.
RINGCT_H_ENCODING = bytes.fromhex("8b655970153799af2aeadc9ff1add0ea6c7251d54154cfa92c173a0dd39c1f94")
RINGCT_H = Point.decompress(RINGCT_H_ENCODING)

# The neutral element as a module constant, so callers comparing against it do
# not each spell `Point.identity()`. It is immutable, so sharing one is safe.
IDENTITY = Point.identity()
