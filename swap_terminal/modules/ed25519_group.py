"""ed25519 group and scalar arithmetic: the group law, x-recovery, scalars mod l, and the torsion check.

Role: function level (the bottom of CLAUDE.md rule 10's stack -- every name in
      this file is a pure function of its arguments, or a method on an
      immutable point)
Reads: nothing. No socket, no file, no environment, no clock, no database, and
      no entropy: there is not a single call to `secrets` or `random` here.
      Every output is a deterministic function of the inputs.
Writes: nothing
Can move funds: no. Nothing here signs, encodes a transaction, derives an
      address, or opens a socket. It is nonetheless FUND-ADJACENT in exactly
      the sense CLAUDE.md rule 16 means, and more sharply than most files in
      this tree: these primitives exist to carry a cross-curve DLEQ proof about
      a Monero spend-key share (docs/dleq_cross_curve_design.md), and a defect
      in `Point.decompress` or `is_torsion_free` is how a counterparty commits
      to one scalar on secp256k1 and a different one here, takes the Gridcoin,
      and leaves the XMR locked to a key nobody holds. That failure is silent
      and arrives after the money moved.
Mainnet-safe: NO. Not because it can reach a chain -- it cannot, even if asked
      -- but because it is UNAUDITED cryptography, and "safe to run" is not the
      question a reader of this field is asking on a key-holding host.

UNAUDITED. NOT WIRED IN. NOT FOR MAINNET.

Three statements, each measured rather than hedged, on 2026-09-27:

  1. This is unaudited cryptography written by a single author (an LLM session)
     from RFC 8032 and from the curve's defining equation. It has had no review
     by a cryptographer and no third-party audit. What it does have is the
     strongest offline evidence available for this particular primitive, named
     in "HOW THIS WAS VERIFIED" below: RFC 8032 section 7.1's five published
     known-answer vectors, three vectors from Go's filippo.io/edwards25519
     already in this repo, and agreement with libsodium over thousands of
     random inputs across seven operations. Passing tests is not security; it
     is self-consistency plus agreement with three independent implementations.
  2. NOTHING IN THIS TREE CALLS IT. Established 2026-09-27 by grepping every
     .py, .js, .sh, .md, .json, .toml and .txt file outside node_modules and
     .git for `ed25519_group`, `scalar_base_mul`, `point_scalar_mul`,
     `is_torsion_free`, `has_small_order` and `RINGCT_H` -- the NAME grep
     CLAUDE.md rule 2 asks for, not an import-graph walk. The hits are this
     file, tests/test_ed25519_group.py, and one line of
     docs/dleq_cross_curve_design.md section 3.4 that QUOTES `sigma_fun`'s Rust
     source (`!claim.1.is_torsion_free()`) and is not a reference to this
     module. There is no swap path, no route, no worker and no CLI entry point
     wired to it.
  3. It is therefore stage 1 of docs/dleq_cross_curve_design.md's section 6
     plan, and stage 1 alone. The DLEQ proof itself is NOT here, on purpose:
     that document's section 7 argues the case against building it at all, and
     nothing below prejudges that decision. What is below is the arithmetic
     that every answer to that question needs -- including "call out to a
     maintained implementation", which still needs a torsion check on this side
     of the process boundary.

WHY THIS FILE EXISTS RATHER THAN A DEPENDENCY

`swap_terminal/requirements.txt` declares exactly two runtime dependencies,
`base58` and `ecdsa` (read 2026-09-27). NO DEPENDENCY WAS ADDED for this file
and none may be: `pynacl` and `cryptography` are both bindings around compiled
libraries, and `chains/solana_address.py` and `modules/adaptor_ecdsa.py` both
already refused a compiled extension on a host that holds BTC_RPC_PASS,
LTC_RPC_PASS, GRC_RPC_PASS and wallet keys. This file follows that refusal.

`pynacl` 1.4.0 IS importable in this container (measured 2026-09-27) and
tests/test_ed25519_group.py uses it as an ORACLE -- the same pattern this repo
already accepted for `solders` in test_solana_address.py and for
`cryptoconditions` in test_xrp_crypto_condition.py. An oracle in a test is not a
dependency of the code under test, and that test skips when it is absent.

THE ONE HONEST COST, STATED THE SAME WAY adaptor_ecdsa.py STATES IT

Pure-Python group arithmetic is not constant time, so this code is NOT
side-channel resistant. `point_scalar_mul` walks 4-bit windows of the scalar and
indexes a table; Python's integers are variable-width, `int.__mul__` is not
constant time, and the interpreter reorders nothing predictably. On a host where
an attacker can measure timing, a secret scalar passed through here is at risk.
That is not fixable within the no-compiled-extension constraint -- it is a
genuine trade, not an oversight -- and it is a second reason this file is not
mainnet-safe. docs/dleq_cross_curve_design.md section 4.4 is the longer version
of this paragraph, and the situation is worse for a DLEQ prover than for
anything here, because the prover branches on the individual bits of the secret.

WHAT THE CURVE IS, SPELLED OUT SO THE CODE CAN BE DIFFED AGAINST RFC 8032

edwards25519 is the twisted Edwards curve

    -x^2 + y^2 = 1 + d*x^2*y^2        over F_p, p = 2^255 - 19, d = -121665/121666

Its group has order 8*l, where l = 2^252 + 27742317777372353535851937790883648493
(RFC 8032 section 5.1; the same constant, written the same way, is in
docs/dleq_cross_curve_design.md section 2.1). COFACTOR 8 IS THE WHOLE REASON
`is_torsion_free` EXISTS: a point can satisfy the curve equation, encode
canonically, and still not lie in the prime-order subgroup that a discrete-log
claim is about. See that function.

A point is encoded in 32 bytes: the low 255 bits are y little-endian, the top
bit is the low bit of x. Decoding therefore has to RECOVER x from y, which is
the step `chains/solana_address.py` deliberately does not take.

WHAT IS SHARED WITH chains/solana_address.py, AND WHY IT IS AN IMPORT

CLAUDE.md rule 8: when you touch a rule, grep for it, and let one survivor own
the concept. The field prime and the curve constant d are defined in
`chains/solana_address.py` -- which computes d rather than spelling a 77-digit
literal, for the reason its own comment gives -- and this module IMPORTS them
rather than re-deriving them. Two copies of `d` would be rule 8's bug with a
delay on it: they agree on the day they are written, and a reader who found one
would never be told the other existed.

What is NOT shared, with the reason, because rule 8 asks for the difference to
be named at the site:

  `_raw_is_on_curve`   answers "is this y on the curve" with Euler's criterion,
                       one modular exponentiation, and returns False for BOTH a
                       non-canonical encoding and an off-curve point. That
                       fused answer is exactly right for its caller -- Solana
                       asks one question, "could a private key exist" -- and
                       unusable here, because `Point.decompress` must
                       distinguish the two failures and must produce x, not a
                       boolean. Recovering x with `pow(x2, (p+3)//8, p)`
                       establishes membership as a by-product (the candidate
                       root is squared and compared), so calling
                       `_raw_is_on_curve` first would be a second 255-bit
                       exponentiation to learn what the first one already
                       settles. No logic is duplicated: that file decides
                       membership, this one decides membership-and-x.
  `is_canonical`       is a separate public function here precisely because
                       `_raw_is_on_curve` cannot answer it separately, and
                       because a DLEQ verifier has to reject a non-canonical
                       encoding BEFORE it does any arithmetic
                       (docs/dleq_cross_curve_design.md section 3.4).

Whether those two field constants should move to a module both files import is
a real question and it is NOT answered here: moving them would edit a file on
the live Solana payout path in order to serve a file nothing calls, which is
the wrong direction for that trade. It is named in this session's report
instead of done quietly.

HOW THIS WAS VERIFIED (CLAUDE.md rule 17: run the thing that would show it
false). Every item is a test in tests/test_ed25519_group.py; every number was
measured on 2026-09-27:

  RFC 8032 section 7.1   all five published vectors (TEST 1, 2, 3, 1024 and
                         SHA(abc)) pass in both directions: the public key is
                         re-derived from the secret key through
                         `scalar_base_mul` and `Point.compress`, and the
                         signature is verified through `Point.decompress`
                         (x-recovery), `scalar_from_bytes_le` over a 64-byte
                         SHA-512 digest, the group law and `point_scalar_mul`.
                         The RFC text was fetched from two unrelated GitHub
                         mirrors whose sha256 agreed byte for byte
                         (ed63657f...c4c3, 103,210 bytes); the canonical
                         www.rfc-editor.org and datatracker.ietf.org are both
                         blocked by this container's egress proxy, which is the
                         same block docs/dleq_cross_curve_design.md records for
                         getmonero.org and eprint.iacr.org.
  go-dleq vectors        tests/vectors/dleq_cross_curve_go.json, produced by
                         Go's filippo.io/edwards25519: all three ed25519 points
                         reproduced byte for byte from their little-endian
                         witnesses.
  libsodium (pynacl)     0 mismatches on 512 random point additions, 512
                         subtractions, 256 variable-base multiplications, 256
                         fixed-base multiplications, 512 reductions of 64-byte
                         scalars, and 256 scalar inversions and 256 scalar
                         multiplications.
                         POINT VALIDITY IS THE ONE PLACE THE TWO DISAGREE, AND
                         THIS MODULE IS THE STRICTER ONE. Over 1,024 random
                         32-byte strings, 489 are canonical on-curve points and
                         60 of those (12.3%, against 1/8 = 12.5%) are
                         torsion-free by `is_torsion_free`; the libsodium
                         bundled with this container's pynacl 1.4.0 calls 124 of
                         them valid, which is 2/8, and the 64 extra are exactly
                         the points whose l*P is the ORDER-2 point rather than
                         the identity. libsodium's own arithmetic agrees with
                         this module on every one of those l*P values -- the
                         test computes them through libsodium calls only -- so
                         the disagreement is in its PREDICATE, and that
                         predicate is a bug fixed upstream in 1.0.18:
                         `ge25519_is_on_main_subgroup` tested `X == 0` alone
                         (1.0.16, read 2026-09-27), which is true of the
                         identity AND of (0, -1). The test states this as the
                         finding and allows 0 disagreements so it does not break
                         when libsodium is upgraded. A DLEQ verifier that
                         delegated its torsion check to THIS libsodium would
                         accept a claimed key with an order-2 torsion
                         component.
  the basepoint          DERIVED here (y = 4/5, x even) and then checked against
                         RFC 8032 section 5.1's two decimal literals. Not
                         copied from anywhere; there is no 78-digit literal in
                         this file to mistype.
  the eight small-order  DERIVED here (l*P has order dividing 8 for any curve
  encodings              point P) and cross-checked against libsodium's
                         independent algebraic test.

MEASURED SPEED, AND WHETHER THE DESIGN DOCUMENT'S PROJECTION HELD. Rule 6: all
timings in microfortnights, 1µfn = 1.2096s, seconds in parentheses. Measured
2026-09-27 in this container, Python 3.11.15, single core. THREE repetitions of
n=300 (n=3000 for the sub-millisecond rows) are reported as a RANGE rather than
a mean, because the spread between repetitions turned out to be larger than
several of the differences this file would otherwise have claimed -- see the
note under the table, which is the actual finding here:

    operation                                 n     3 reps, min -- max
    point_scalar_mul, table built per call   300   0.00158--0.00187µfn (1.91--2.26ms)
    scalar_base_mul, persistent table        300   0.00148--0.00175µfn (1.79--2.12ms)
    is_torsion_free, one full l*P            300   0.00149--0.00163µfn (1.80--1.97ms)
    Point.decompress, with x-recovery       3000   0.000146--0.000162µfn (0.177--0.196ms)
    Point.compress, one inversion           3000   0.0000182--0.0000213µfn (0.022--0.026ms)
    has_small_order, three doublings        3000   0.0000129--0.0000183µfn (0.016--0.022ms)
    _window_table, 15 additions             3000   0.0000703--0.0000866µfn (0.085--0.105ms)

THE DESIGN DOCUMENT'S PROJECTION HELD IN DIRECTION AND MISSED IN MAGNITUDE BY
ABOUT A THIRD, AND SAYING SO IS THE POINT OF MEASURING.
docs/dleq_cross_curve_design.md section 2.3 measured 0.00129µfn (1.56ms) for a
4-bit window with the table built per call and 0.00117µfn (1.42ms) for a
persistent fixed-base table, on a hand-rolled benchmark outside this repository.
This file is SLOWER than both -- roughly 1.3x on the variable-base figure and
1.3x on the fixed-base one. Substituting these measurements into that section's
own arithmetic, with its secp256k1 and Horner terms unchanged:

    verify   252 * (2*1.90 + 2*2.10 + 2*0.54 + 2*1.58) ms + 5.96 ms
             = 2.55µfn (3.09s), against that document's 2.18µfn (2.64s)
    prove    252 * (3*1.90 + 2.10 + 3*0.54 + 1.58) ms + 5.96 ms
             = 2.30µfn (2.78s), against that document's 1.91µfn (2.31s)

So the conclusion that section drew -- single-digit seconds per swap setup,
"feasibility is not the problem" -- survives unchanged, and its numbers do not.
Both of those remain PROJECTIONS and not measurements: no proof exists to time,
which is what that document already says about its own figure.

WHY SLOWER IS UNVERIFIED. The likely reason is that this file allocates a frozen
`Point` object per group operation (about 320 per scalar multiplication) where a
throwaway benchmark would use tuples, so the gap would be interpreter overhead
rather than arithmetic. That was NOT tested: the benchmark in question was
written outside this repository and is gone, so there is nothing left to profile
against. It is a hypothesis in rule 17's sense and is labeled as one.

WHAT THE NOISE REFUTED, which is the useful part. The 15 additions of a window
table cost 0.085--0.105ms, and `scalar_base_mul` saves exactly that over
`point_scalar_mul` by holding the basepoint's table as a module constant. That
saving is REAL but is smaller than the +-0.3ms spread between repetitions, so on
this host, at this n, **`scalar_base_mul` cannot be shown to be faster than
`point_scalar_mul` by measurement** -- the ranges overlap. The optimization is
kept because it removes work rather than because a measurement supports it
(rule 3 prefers removing work to doing it faster, and rule 3 also forbids
claiming an improvement that was not measured). The 4-bit window width was
likewise checked against 3, 5 and 6 bits over n=300: every width landed within
2% of every other on the variable-base figure, which is inside the same noise,
so 4 stays and nobody should re-tune it on the strength of one run.

One cost worth naming because the obvious call pattern is much worse:
`is_torsion_free` is a full 253-bit scalar multiplication, ~1.9ms, and a DLEQ
verifier that called it on all 252 commitments per curve would spend 0.396µfn
(479ms) doing nothing else. The check belongs on the claimed keys and the sums,
not on every commitment. This file provides the correct check and states its
cost; where to call it is a protocol decision and belongs to code that has a
protocol.
"""

from __future__ import annotations

from dataclasses import dataclass

# CLAUDE.md rule 8: the field prime and the curve constant d have ONE owner in
# this tree and it is chains/solana_address.py, which computes d rather than
# spelling a 77-digit literal. Importing them is the alternative to a second
# copy that agrees today and drifts later. That module is pure -- it imports
# hashlib and base58 and touches no environment, file or socket at import time
# (read in full 2026-09-27) -- so this import has no side effect, which is the
# thing CLAUDE.md rule 12 asks about and that a linter cannot check.
#
# The rootless import path is this repository's convention, not a choice made
# here: the application imports its own modules as `from chains.x import y`
# with swap_terminal/ on sys.path (tests/conftest.py puts it there and explains
# why), and `modules/atomic_swapper.py` does the same. That is rule 10's layout
# gap; a different spelling in this one file would not close it and would fail
# to import.
from chains.solana_address import CURVE_D, FIELD_PRIME

# --- Group constants ---------------------------------------------------------

# The order of the prime-order subgroup, l. RFC 8032 section 5.1 writes it as
# 2^252 + 27742317777372353535851937790883648493, and it is written the same way
# here rather than as a hex literal so it can be diffed against the RFC and
# against docs/dleq_cross_curve_design.md section 2.1 by eye.
GROUP_ORDER = 2**252 + 27742317777372353535851937790883648493

# The full curve has 8*l points. Every ed25519 pitfall that is not an encoding
# pitfall comes from this number not being 1.
COFACTOR = 8

# A compressed point and a canonical scalar are both 32 bytes, and they are
# named separately because they are different things that happen to share a
# size -- `scalar_from_bytes_le` also accepts 64 -- and conflating them is how a
# 64-byte hash gets read as a point.
POINT_BYTES = 32
SCALAR_BYTES = 32
# RFC 8032's SHA-512 challenge is reduced from 64 bytes. Named rather than
# spelled at the two sites that check it (ruff PLR2004).
WIDE_SCALAR_BYTES = 64

# The number of bits in y that a 32-byte encoding carries; the 256th is the sign
# of x. Named because `(1 << 255) - 1` appears in three places and a typo in one
# of them would produce a mask that still looks plausible.
_Y_BITS = 255
_Y_MASK = (1 << _Y_BITS) - 1

# sqrt(-1) mod p, needed by x-recovery. p = 2^255 - 19 is 5 mod 8, so
# 2^((p-1)/4) is a square root of -1; computed at import rather than written as
# a literal, for chains/solana_address.py's stated reason (a literal is a thing
# that can be mistyped and a thing no reader can check).
_SQRT_MINUS_ONE = pow(2, (FIELD_PRIME - 1) // 4, FIELD_PRIME)

# 2*d, the constant the extended-coordinate addition formula actually wants.
# Precomputed because it appears in every single point addition.
_D2 = (2 * CURVE_D) % FIELD_PRIME

# Window width for scalar multiplication. 4 bits means a 16-entry table and 64
# window steps for a 253-bit scalar; docs/dleq_cross_curve_design.md section 2.3
# measured this shape and it is what the timings in this module's docstring were
# taken against. Not a tuning knob to turn without measuring: 5 bits halves the
# additions and doubles the table-building cost, and which wins depends on
# whether the table is reused.
_WINDOW_BITS = 4
_WINDOW_SIZE = 1 << _WINDOW_BITS
_WINDOW_MASK = _WINDOW_SIZE - 1
# Whole windows needed to cover any scalar below l. Rounded UP, so the top
# window is not silently dropped for a scalar near l.
_WINDOW_COUNT = (GROUP_ORDER.bit_length() + _WINDOW_BITS - 1) // _WINDOW_BITS


class Ed25519Error(ValueError):
    """A malformed input: a bad length, a non-canonical encoding, an off-curve point, a scalar that is not an int.

    Deliberately a ValueError subclass, and deliberately DISTINCT from the
    "this is a well-formed point and the answer is no" results, which are the
    `False` returns from `is_canonical`, `is_canonical_scalar_le`,
    `is_torsion_free` and `has_small_order`.

    That split is CLAUDE.md rule 12's BLE001 argument applied to a return type
    rather than to an except clause, and it is the same split
    `modules/adaptor_ecdsa.py::AdaptorError` makes for the same reason. A caller
    must be able to tell "you handed me 31 bytes" from "that is a real point and
    it has a torsion component", because on a swap path those demand opposite
    actions: the first is a bug or a truncated message, the second is a
    counterparty sending something a verifier must refuse and log. One `False`
    for both makes a malicious input indistinguishable from a corrupt one.
    """


# --- Scalars mod l -----------------------------------------------------------
#
# Every function in this section takes and returns a plain Python int in
# [0, l). There is no Scalar class, on purpose: a wrapper would add a type to
# check at every boundary, and the one thing it WOULD buy -- making it
# impossible to hand an unreduced integer to a point multiplication -- is
# provided instead by `point_scalar_mul` reducing its own argument. The trap
# that remains is the wire format, and it is `scalar_from_bytes_le`'s docstring.


def scalar_reduce(value: int) -> int:
    """`value` mod l, for any Python int including a negative one.

    Python's `%` already returns a non-negative result for a positive modulus,
    so this is one operation; it exists as a named function anyway because
    "reduce mod the GROUP order l" and "reduce mod the FIELD prime p" are two
    different reductions on this curve, and writing `% GROUP_ORDER` inline at
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

    Present although only add/mul/inverse were asked for, because MRL-0010's
    forced last blinder is a NEGATED sum -- docs/dleq_cross_curve_design.md
    section 1.3(b) records that the PDF's extracted text drops the minus sign
    while go-dleq's code has it, and calls that "exactly the class of detail
    that produces a proof which verifies against the wrong point". A caller
    writing `scalar_add(a, GROUP_ORDER - b)` by hand is a caller who will one
    day write `a - b` and pass a negative int somewhere that does not reduce.
    """
    return (scalar_reduce(left) - scalar_reduce(right)) % GROUP_ORDER


def scalar_mul(left: int, right: int) -> int:
    """(left * right) mod l.

    NOTE THE NAME BOUNDARY, because it is the one place this module's vocabulary
    could be misread: `scalar_mul` multiplies TWO SCALARS in Z_l, and
    `point_scalar_mul` multiplies a POINT by a scalar. Several libraries spell
    the second one `scalar_mul`; this module cannot, because it needs both, and
    rule 8 forbids two spellings of one concept exactly as firmly as it forbids
    one spelling of two.
    """
    return (scalar_reduce(left) * scalar_reduce(right)) % GROUP_ORDER


def scalar_inverse(value: int) -> int:
    """The multiplicative inverse of `value` mod l.

    Raises for zero rather than returning zero. l is prime, so every non-zero
    residue has an inverse and zero has none; `pow(0, -1, l)` raises in Python
    3.8+ but with a message about a modular inverse rather than about a scalar,
    and MRL-0010's `(2^{n-1})^{-1}` term is the caller that would have to read
    it.
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
    big-endian. A cross-curve DLEQ carries BOTH conventions in one message --
    docs/dleq_cross_curve_design.md section 5.5 flags precisely this as
    unverified in go-dleq's wire format -- and a byte-reversed scalar is still a
    perfectly good scalar: it produces a valid point, a valid proof about the
    wrong secret, and no error anywhere. The only defense is a name that cannot
    be called by accident, which is why this is not `scalar_from_bytes`.

    THIS REDUCES, AND THAT IS RIGHT FOR THE TWO CALLERS THAT EXIST, both of
    which are RFC 8032's:

      - the 64-byte SHA-512 challenge SHA512(R || A || M), which the RFC
        reduces mod l ("interpret ... as an integer in little-endian, reduce
        modulo L");
      - the 32-byte clamped secret scalar, which is below 2^254 and above l, so
        RFC 8032's own key derivation depends on reduction happening.

    IT IS WRONG FOR A WIRE-FORMAT SCALAR, AND THAT IS THE TRAP
    docs/dleq_cross_curve_design.md SECTION 3 IS ABOUT. A 32-byte field in a
    received proof whose value is >= l must be REJECTED, not reduced: reducing
    accepts two distinct encodings of one scalar, which is malleability, and on
    the DLEQ path it is worse -- the construction is only well-formed for
    0 <= x < l, so a verifier that reduces is answering a question nobody asked,
    and section 3.2's silent case (two shares whose sum reduces on ed25519 but
    not on secp256k1) is exactly this mistake wearing a protocol. Call
    `is_canonical_scalar_le` first and refuse. That function exists for this and
    has no other reason to exist.

    Anything other than 32 or 64 bytes raises rather than being padded or
    truncated, for `adaptor_ecdsa._require_message_hash`'s stated reason: a
    caller handed a short buffer who gets a padded scalar has signed a digest of
    something other than what they think.
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
    """A scalar as 32 LITTLE-ENDIAN bytes, the inverse of `scalar_from_bytes_le`.

    Reduces first, so the output is always canonical (below l) and the round trip
    `scalar_from_bytes_le(scalar_to_bytes_le(k)) == k mod l` holds for any int. A
    32-byte output cannot represent an unreduced value anyway, so refusing one
    instead of reducing would gain a raise and nothing else.
    """
    return scalar_reduce(value).to_bytes(SCALAR_BYTES, "little")


def is_canonical_scalar_le(data: bytes) -> bool:
    """True if `data` is 32 bytes encoding a little-endian integer strictly below l.

    The check `scalar_from_bytes_le` deliberately does not do; its docstring says
    why that matters and who has to call this -- every 32-byte scalar arriving
    from a counterparty, before it is used for anything.

    Returns False rather than raising for a wrong length, because the caller's
    question is "may I use this field" and the answer for 31 bytes is no.
    """
    if not isinstance(data, (bytes, bytearray)) or len(data) != SCALAR_BYTES:
        return False
    return int.from_bytes(data, "little") < GROUP_ORDER


# --- Points ------------------------------------------------------------------


@dataclass(frozen=True, eq=False)
class Point:
    """A point on edwards25519 in extended coordinates (X : Y : Z : T), with x = X/Z, y = Y/Z, T = XY/Z.

    WHY EXTENDED COORDINATES AND NOT AFFINE (x, y). Affine addition on a twisted
    Edwards curve needs a modular inverse per addition -- one 255-bit
    exponentiation, measured here at 0.022--0.026ms as part of `compress`.
    A scalar multiplication performs about 320 group operations, so paying an
    inversion at each one would add roughly 7ms to a figure measured at
    1.9--2.3ms: a factor of about 4, computed from those two measurements rather
    than measured end to end, because no affine implementation was written to
    time. Extended coordinates defer every inversion to the single one
    `compress` does at the end. The formulas used below are
    Hisil-Wong-Carter-Dawson 2008 ("Twisted Edwards curves revisited"), which is
    what every ed25519 implementation uses, including the one
    docs/dleq_cross_curve_design.md section 2.3 benchmarked.

    FROZEN, because a mutable point is a shared-reference bug waiting for a
    caller who reuses one. `add`, `double` and `negate` return new points;
    nothing here modifies anything.

    `eq=False` IS LOAD-BEARING AND IS NOT A STYLE CHOICE. Projective coordinates
    are not unique: (X:Y:Z:T) and (2X:2Y:2Z:2T) are the same point with
    different field elements in them. The dataclass-generated field-by-field
    `__eq__` would report two equal points as unequal, and it would do it
    INTERMITTENTLY -- whether two computations of one point land on the same
    representative depends on the path taken to each -- which on the DLEQ path
    is a verifier that rejects honest proofs on some inputs and not others. With
    `eq=False` the dataclass installs neither `__eq__` nor a `__hash__` over the
    raw fields, and the two written below are the ones that survive.
    tests/test_ed25519_group.py pins this with a point built two different ways.
    """

    __slots__ = ("T", "X", "Y", "Z")

    X: int
    Y: int
    Z: int
    T: int

    def __eq__(self, other: object) -> bool:
        """Projective equality: X1*Z2 == X2*Z1 and Y1*Z2 == Y2*Z1.

        Four multiplications and no inversion. Comparing affine forms instead
        would cost two inversions -- about 0.046ms, from the measured
        0.022--0.026ms of `compress`, which is one inversion plus a shift -- to
        answer the identical question, and a DLEQ verifier calls this at least
        once per bit per curve.
        """
        if not isinstance(other, Point):
            return NotImplemented
        return (self.X * other.Z - other.X * self.Z) % FIELD_PRIME == 0 and (
            self.Y * other.Z - other.Y * self.Z
        ) % FIELD_PRIME == 0

    def __hash__(self) -> int:
        """Hash the affine form, so that equal points hash equally.

        Hashing the projective fields would break the invariant that a == b
        implies hash(a) == hash(b), which is a bug that surfaces weeks later as a
        set lookup that misses. This pays one modular inversion; nothing on the
        DLEQ path needs points in a set, so the cost is irrelevant and the
        correctness is not.
        """
        return hash(self.to_affine())

    def __repr__(self) -> str:
        """The compressed encoding in hex, not the four field elements.

        The dataclass default would print four 77-digit integers that no reader
        can compare against anything. A compressed point is what every other
        implementation, every test vector and every wire format spells, so a
        pasted repr can be diffed against RFC 8032 or against
        tests/vectors/dleq_cross_curve_go.json directly -- CLAUDE.md rule 14's
        "pasted output has to be self-describing a day later".
        """
        return f"Point({self.compress().hex()})"

    # --- construction ---

    @classmethod
    def identity(cls) -> Point:
        """The neutral element, affine (0, 1).

        On a twisted Edwards curve the identity is an ordinary point with
        ordinary coordinates: there is no separate point at infinity and no
        special case anywhere in the group law below. That is the structural
        advantage of this curve shape over secp256k1's short Weierstrass form,
        where `modules/adaptor_ecdsa.py` has to check for INFINITY at every
        encode and raise.
        """
        return cls(0, 1, 1, 0)

    @classmethod
    def from_affine(cls, x: int, y: int) -> Point:
        """A point from affine coordinates, WITHOUT checking that it is on the curve.

        Public in name but internal in contract: the callers are `decompress`
        (which has already established membership by recovering x) and the
        derivation of the basepoint below. A caller holding an (x, y) pair from
        anywhere else wants `decompress`, which checks. Named in the module
        docstring's verification list is the reason this is not made private:
        the test derives the eight torsion points through it.
        """
        x %= FIELD_PRIME
        y %= FIELD_PRIME
        return cls(x, y, 1, (x * y) % FIELD_PRIME)

    def to_affine(self) -> tuple[int, int]:
        """(x, y), with the projective Z divided out. Costs one modular inversion.

        Raises for Z == 0, which cannot happen for any point this module
        produces: the HWCD formulas never yield Z == 0 for inputs on the curve,
        because d is a non-square mod p and that is exactly the condition making
        them complete. The check is here because "cannot happen" and "is
        checked" are different claims (rule 17), and this is a fund-adjacent
        path.
        """
        if self.Z == 0:
            raise Ed25519Error("a point with Z == 0 has no affine form; this should be unreachable")
        z_inverse = pow(self.Z, -1, FIELD_PRIME)
        return (self.X * z_inverse % FIELD_PRIME, self.Y * z_inverse % FIELD_PRIME)

    def is_identity(self) -> bool:
        """True for the neutral element, by projective comparison against (0 : 1 : 1 : 0).

        Written as X == 0 and Y == Z rather than `self == Point.identity()` so it
        is two reductions instead of four multiplications; `is_torsion_free`
        calls it, and a DLEQ verifier calls that per received point.
        """
        return self.X % FIELD_PRIME == 0 and (self.Y - self.Z) % FIELD_PRIME == 0

    # --- the group law ---

    def add(self, other: Point) -> Point:
        """P + Q, by HWCD 2008's "add-2008-hwcd-3" formulas specialized to a = -1.

        COMPLETE, WITH NO SPECIAL CASES. This one method handles P + Q for
        distinct points, P + P, P + (-P) = identity, and P + identity, and it
        does so because d is a non-square mod p: the denominators in the Edwards
        addition law cannot vanish for points on the curve. That is why there is
        no `if self == other: return self.double()` branch here and why there
        must not be one -- such a branch is both unnecessary and a timing oracle
        on whether two secret points coincide.

        `double()` exists anyway and is NOT a duplicate of this (rule 8): it is a
        genuinely different and cheaper formula, 4 squarings against 9
        multiplications, and the difference is measurable across the 252
        doublings of a scalar multiplication.
        """
        p = FIELD_PRIME
        a = (self.Y - self.X) * (other.Y - other.X) % p
        b = (self.Y + self.X) * (other.Y + other.X) % p
        c = self.T * _D2 % p * other.T % p
        d = self.Z * 2 * other.Z % p
        e = b - a
        f = d - c
        g = d + c
        h = b + a
        return Point(e * f % p, g * h % p, f * g % p, e * h % p)

    def double(self) -> Point:
        """2P, by HWCD 2008's "dbl-2008-hwcd" formulas specialized to a = -1.

        Cheaper than `self.add(self)` because it trades multiplications for
        squarings and never reads T -- which also means it is correct for a point
        whose T is stale, although nothing here produces one. A 253-bit scalar
        multiplication does 252 doublings and at most 64 additions, so this
        formula is where most of the time in this module is spent.
        """
        p = FIELD_PRIME
        a = self.X * self.X % p
        b = self.Y * self.Y % p
        c = 2 * self.Z * self.Z % p
        h = a + b
        e = h - (self.X + self.Y) * (self.X + self.Y) % p
        g = a - b
        f = c + g
        return Point(e * f % p, g * h % p, f * g % p, e * h % p)

    def negate(self) -> Point:
        """-P, which on a twisted Edwards curve is (-x, y): negate X and T, leave Y and Z.

        No field inversion, no conditional, and `P.add(P.negate())` is the
        identity by the completeness argument in `add`. That identity is one of
        the property tests in tests/test_ed25519_group.py, and it is worth
        testing rather than assuming precisely because it is the case a short
        Weierstrass implementation has to special-case.
        """
        p = FIELD_PRIME
        return Point((-self.X) % p, self.Y, self.Z, (-self.T) % p)

    def subtract(self, other: Point) -> Point:
        """P - Q. Two calls, `self.add(other.negate())`, named because a verifier writes it constantly.

        MRL-0010's verifier recomputes `e1 * (C - G')` per bit per curve, which
        docs/dleq_cross_curve_design.md section 2.3 counts as one of the two
        hottest operations in the whole construction. This is not a second
        implementation of anything.
        """
        return self.add(other.negate())

    # --- encoding ---

    def compress(self) -> bytes:
        """The 32-byte RFC 8032 encoding: y little-endian in the low 255 bits, the low bit of x on top.

        Costs the one modular inversion that extended coordinates deferred:
        measured 0.0000182--0.0000213µfn (0.022--0.026ms) over 3 repetitions of
        n=3000.

        There is no "point at infinity" case to refuse, the way
        `modules/adaptor_ecdsa.py::point_to_bytes` has to: the identity is (0, 1)
        and encodes as 0100...00, a perfectly valid 32-byte point that
        decompresses back to the identity. Whether the identity is acceptable in
        a given field of a protocol message is a protocol question, not an
        encoding one -- and docs/dleq_cross_curve_design.md section 5.3 lists
        "replace one commitment with the group identity" as its own soundness
        mutation test, which is where that refusal belongs.
        """
        x, y = self.to_affine()
        return (y | ((x & 1) << _Y_BITS)).to_bytes(POINT_BYTES, "little")

    @classmethod
    def decompress(cls, encoding: bytes) -> Point:
        """Decode 32 bytes into a point, RECOVERING x from y and the sign bit, or raise Ed25519Error.

        THIS IS THE STEP chains/solana_address.py DELIBERATELY DOES NOT TAKE
        ("It does not recover x, because nothing here needs x"). Deciding
        membership needs only a Legendre symbol; getting a usable point needs
        the square root itself.

        The arithmetic, so it can be diffed against RFC 8032 section 5.1.3:

            y    = the low 255 bits, little-endian;   sign = bit 255
            x^2  = (y^2 - 1) / (d*y^2 + 1)
            x    = (x^2)^((p+3)/8), corrected by sqrt(-1) if that is the wrong root
            if the low bit of x disagrees with `sign`, x = p - x

        The (p+3)/8 exponent is a square root only because p = 5 mod 8: it
        returns a root of v or a root of -v, and one correction by sqrt(-1)
        covers the second case. If neither candidate squares back to x^2, no
        point with this y exists and this raises. That comparison IS the
        membership test, so no separate Euler's-criterion call is made -- see the
        module docstring on what is and is not shared with
        chains/solana_address.py.

        THREE SEPARATE REFUSALS, separate because
        docs/dleq_cross_curve_design.md section 3.4 lists them as three distinct
        checks a Python implementation has to do by hand:

          wrong length    31 or 33 bytes is not a short point, it is a framing
                          bug in whatever produced the message.
          non-canonical   y >= p, or x == 0 with the sign bit set. `is_canonical`
                          is the same test exposed as a predicate, for a caller
                          that must refuse before it decodes.
          off the curve   x^2 is not a square mod p.

        WHAT THIS DOES NOT CHECK is subgroup membership: the returned point may
        have a torsion component and still be a legitimate decode of a valid
        curve point. That is `is_torsion_free`'s question, it is deliberately a
        separate call, and the reason is in that function.
        """
        if not isinstance(encoding, (bytes, bytearray)):
            raise Ed25519Error(f"a compressed point must be bytes, got {type(encoding).__name__}")
        if len(encoding) != POINT_BYTES:
            raise Ed25519Error(f"a compressed point is {POINT_BYTES} bytes, got {len(encoding)}")
        raw = int.from_bytes(encoding, "little")
        y = raw & _Y_MASK
        sign = raw >> _Y_BITS
        if y >= FIELD_PRIME:
            raise Ed25519Error(
                "non-canonical point encoding: y is not reduced mod p. "
                "Two encodings of one point is a malleability bug, and libsodium, curve25519-dalek "
                "and this repo's chains/solana_address.py all reject these."
            )
        x = _recover_x(y, sign)
        if x is None:
            raise Ed25519Error(
                "not a usable point: either the implied x^2 has no square root mod p (off the curve), "
                "or x == 0 with the sign bit set, which is a non-canonical encoding of the same point"
            )
        return cls.from_affine(x, y)


def _recover_x(y: int, sign: int) -> int | None:
    """The x matching this y and sign bit, or None if no point on the curve has this y.

    Split out of `Point.decompress` for one reason and it is not style: this is
    the DECISION (CLAUDE.md rule 10 -- the smallest, most testable piece at the
    bottom), and a test can call it with a y it constructed instead of having to
    build a 32-byte encoding to reach it. `decompress` is the framing around it,
    and `is_canonical` is a second caller that needs the same answer with a
    different return type.

    Returns None rather than raising, because "this y has no x" is an ANSWER at
    this level; the caller turns it into a message with the context it has.
    """
    p = FIELD_PRIME
    y_squared = y * y % p
    numerator = (y_squared - 1) % p
    denominator = (CURVE_D * y_squared + 1) % p
    if denominator == 0:
        # No point on the curve has this y: the defining equation gives x^2 no
        # finite value. Unreachable for ed25519 in practice -- it needs
        # y^2 == -1/d, and -1/d is a non-square mod p -- but "unreachable" is a
        # claim, not a check (rule 17), and the alternative on a fund-adjacent
        # path is a ZeroDivisionError from pow() with no explanation.
        return None
    x_squared = numerator * pow(denominator, -1, p) % p
    if x_squared == 0:
        # y = +-1: the identity and the point of order 2. x = 0 is a genuine root
        # here, and it has exactly ONE encoding, with the sign bit clear. A sign
        # bit of 1 would mean "the negative of zero", which is zero, so that
        # string is a second encoding of the same point and is refused rather
        # than silently read as it. libsodium and curve25519-dalek both refuse
        # it; a verifier that accepted it would accept two distinct 32-byte
        # strings for one commitment.
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
    """True if `encoding` is the ONE 32-byte string RFC 8032 assigns to the point it denotes.

    Rejects, and these are the only two ways a point can have a second encoding
    on this curve:

      1. y >= p. The encoding carries 255 bits of y but the field has only
         2^255 - 19 elements, so 19 y-values are representable twice. This is
         the family libsodium, curve25519-dalek and ZIP-215 all discuss, and the
         one chains/solana_address.py::_raw_is_on_curve already refuses (its
         comment: "curve25519-dalek rejects these, so this matches what the
         chain itself would do rather than what the arithmetic would tolerate").
      2. x == 0 with the sign bit set. Negative zero is zero, so 0100..00 and
         0100..80 denote the same point, as do the two encodings for y = p - 1.
         Rarely stated, and one comparison to check.

    MEASURED 2026-09-27 by enumerating all 19 y-values in [p, 2^255) against
    both sign bits, and both sign bits for the two y with x == 0: of those 38
    (y, sign) pairs, 23 decode to a valid curve point, spread over 12 distinct y
    -- plus the 2 negative-zero strings. SO: **25 of the 2^256 possible 32-byte
    strings are non-canonical encodings of a valid point**, and the denominator
    of the 23 is 38, not 19 (rule 3: a count without what it was counted out of
    has caused real errors). tests/test_ed25519_group.py pins both numbers by
    re-running that enumeration, because the interesting failure is a
    `y >= FIELD_PRIME` check that is silently never true.

    Returns False rather than raising for a wrong length or an off-curve point,
    because the caller's question is "may I use this 32-byte field" and the
    answer for garbage is no. A caller that needs to know WHY calls
    `Point.decompress` and reads the message.

    WHY THIS IS A DLEQ REQUIREMENT AND NOT HYGIENE. MRL-0010 closes with one
    line -- "the verifier is assumed to have also checked each proof tuple
    element to ensure it belongs to the expected group, to account for a
    malicious prover" -- and docs/dleq_cross_curve_design.md section 3.4 expands
    that line into the three checks this module provides. A proof whose
    commitments are re-encodable is a proof whose Fiat-Shamir transcript can be
    re-spelled, which is a second distinct proof of one statement and, depending
    on the protocol wrapped around it, a replay.
    """
    if not isinstance(encoding, (bytes, bytearray)) or len(encoding) != POINT_BYTES:
        return False
    raw = int.from_bytes(encoding, "little")
    y = raw & _Y_MASK
    sign = raw >> _Y_BITS
    if y >= FIELD_PRIME:
        return False
    # _recover_x returns None both for an off-curve y and for the negative-zero
    # case, and both answers here are False, so they do not need separating.
    # `Point.decompress` separates them in its message, for a caller that is
    # diagnosing rather than filtering.
    return _recover_x(y, sign) is not None


# --- The two published generators --------------------------------------------
#
# CLAUDE.md rule 17 and the task both ask where a constant came from, so each of
# these says. One is DERIVED and then checked against a published literal; the
# other IS a published literal and cannot be derived here without Keccak. The
# difference is the point of the two comments.

# B, the ed25519 basepoint. DERIVED, NOT COPIED. RFC 8032 section 5.1 defines it
# as the point with y = 4/5 and the "unique" x that is positive -- positive
# meaning the sign bit is 0, i.e. x even. So y = 4 * 5^-1 mod p and x is the
# even square root of (y^2 - 1)/(d*y^2 + 1), which is what the two lines below
# compute.
#
# MEASURED 2026-09-27: the result equals RFC 8032 section 5.1's own two decimal
# literals,
#   x = 15112221349535400772501151409588531511454012693041857206046113283949847762202
#   y = 46316835694926478169428394003475163141307993866256225615783033603165251855960
# and compresses to
#   5866666666666666666666666666666666666666666666666666666666666666
# which is byte for byte the `one` vector's ed25519 point in
# tests/vectors/dleq_cross_curve_go.json -- produced by Go's
# filippo.io/edwards25519, a different implementation in a different language.
# The test pins all three comparisons, so a wrong basepoint cannot survive here,
# and deriving it means this file contains no 78-digit literal for anybody to
# mistype.
_BASEPOINT_Y = 4 * pow(5, -1, FIELD_PRIME) % FIELD_PRIME
_BASEPOINT_X = _recover_x(_BASEPOINT_Y, 0)
if _BASEPOINT_X is None:  # pragma: no cover -- the curve arithmetic forbids it
    raise Ed25519Error("the basepoint's y has no recoverable x, which would mean p, d or _recover_x is wrong")
BASEPOINT = Point.from_affine(_BASEPOINT_X, _BASEPOINT_Y)

# H, Monero's RingCT alternate generator. A PUBLISHED CONSTANT, NOT DERIVED, and
# the difference from B is the reason this comment is long.
#
# H was produced by Monero's hash-to-point applied to the encoding of the scalar
# 1. Reproducing that derivation needs Keccak-256, which is NOT in Python's
# standard library -- measured: `hashlib.algorithms_available` contains no name
# matching keccak, and `sha3_256(b"")` is a7ffc6f8... where Keccak-256 of the
# empty string is c5d24601..., different padding, so reaching for sha3_256
# because the name looks right is a silent break.
# docs/dleq_cross_curve_design.md section 4.3 argues that avoiding Keccak
# entirely is the better trade and that using the published constant is how.
# This is the one place in this module where a literal is the honest choice,
# because the alternative is ~100 lines of unaudited Keccak to re-derive a value
# that two independent published sources already agree on.
#
# The two sources, both fetched and read 2026-09-27:
#   monero-project/research-lab, source-code/MiniNero/RingCT.py line 10:
#     `def getHForCT(): return "8b655970...9c1f94"`, with dead code beneath the
#     return showing the hashToPoint_ct(publicFromInt(1)) derivation it replaced.
#     That is the Monero Research Lab's own reference code.
#   AthanorLabs/go-dleq, ed25519/curve.go line 32:
#     `const str = "8b655970...9c1f94"` inside altBasePoint() -- an unrelated
#     implementation of the very construction this module is being built for.
# They agree byte for byte, which is the only check available without Keccak.
# What the test adds is that this module decompresses it to a canonical,
# on-curve, TORSION-FREE point -- that last property is the one that would
# actually break the DLEQ if it failed, and it is checked rather than assumed.
#
# WHAT IS UNVERIFIED AND STAYS UNVERIFIED HERE: that nobody knows a discrete log
# of H with respect to B. That is the security property the whole Pedersen
# commitment rests on, it cannot be established by any test, and it rests on the
# nothing-up-my-sleeve derivation rather than on anything in this file.
RINGCT_H_ENCODING = bytes.fromhex("8b655970153799af2aeadc9ff1add0ea6c7251d54154cfa92c173a0dd39c1f94")
RINGCT_H = Point.decompress(RINGCT_H_ENCODING)

# The neutral element as a module constant, so callers comparing against it do
# not each spell `Point.identity()`. Safe to share because Point is frozen.
IDENTITY = Point.identity()


# --- Scalar multiplication ---------------------------------------------------


def _window_table(point: Point) -> list[Point]:
    """[0*P, 1*P, ..., 15*P], the lookup table for 4-bit windowed multiplication.

    Fifteen additions, built by repeated addition rather than by a doubling
    chain, because at this size the difference is one addition and the shorter
    code is worth more than it.
    """
    table = [IDENTITY, point]
    for _ in range(2, _WINDOW_SIZE):
        table.append(table[-1].add(point))
    return table


def _scalar_mul_with_table(scalar: int, table: list[Point]) -> Point:
    """k*P given a precomputed window table for P. The only multiplication loop in this module.

    Most-significant window first, so the accumulator is doubled four times per
    window and one table entry is added: 64 windows for a 253-bit scalar, 256
    doublings and 64 additions.

    `scalar` must ALREADY be reduced; this takes it as given, and the two public
    entry points reduce before calling. A separate reduction here would be a
    second copy of the same decision (rule 8) and would hide which of the two
    callers was relying on it.

    NOT CONSTANT TIME, and the leak is specific enough to name. The loop body
    always runs and always adds -- including a `table[0]` identity addition for a
    zero window, which is deliberate: skipping it would leak the zero windows
    through timing far more loudly than the table index does. What remains is
    that `table[window]` is a data-dependent memory access, and that Python's
    big-integer multiplication time depends on the operand values. See the
    module docstring; this is not closable here.
    """
    accumulator = IDENTITY
    for index in range(_WINDOW_COUNT - 1, -1, -1):
        for _ in range(_WINDOW_BITS):
            accumulator = accumulator.double()
        accumulator = accumulator.add(table[(scalar >> (index * _WINDOW_BITS)) & _WINDOW_MASK])
    return accumulator


def point_scalar_mul(scalar: int, point: Point) -> Point:
    """k*P for an arbitrary point. Measured 0.00158--0.00187µfn (1.91--2.26ms), table built per call.

    The scalar is REDUCED mod l first, which is a real choice and not a
    formality: the group has order 8*l, so for a point WITH a torsion component
    k*P and (k mod l)*P genuinely differ. For a torsion-free point they are
    identical, and for a point that is not torsion-free the claim being computed
    was already meaningless (docs/dleq_cross_curve_design.md section 3.4). A
    caller that needs the honest 8*l-group answer must not come through here --
    `is_torsion_free` is the only such caller in this module and it does not.

    Named `point_scalar_mul` and not `scalar_mul` because `scalar_mul` is the
    mod-l multiply of two scalars and they cannot share a name; that function's
    docstring has the rest.
    """
    return _scalar_mul_with_table(scalar_reduce(scalar), _window_table(point))


# The basepoint's window table, built once at import: 15 point additions,
# measured 0.0000703--0.0000866µfn (0.085--0.105ms) over 3 repetitions of
# n=3000, which is about a twentieth of one scalar multiplication and is paid
# whether or not the importer multiplies anything.
#
# BUILT AT IMPORT RATHER THAN MEMOIZED ON FIRST USE, deliberately. The lazy
# version needs a `global` statement and therefore a `noqa: PLW0603`, and
# CLAUDE.md rule 19 is explicit that a suppression is a claim you checked rather
# than a way past a finding -- a 0.044ms import cost is not worth a suppression,
# and a module-level constant that is computed once is what this actually is.
# It is pure: no file, no socket, no environment, which is the import-time side
# effect rule 12 warns about and this is not one.
_BASEPOINT_WINDOW = _window_table(BASEPOINT)


def scalar_base_mul(scalar: int) -> Point:
    """k*B against the ed25519 basepoint. Measured 0.00148--0.00175µfn (1.79--2.12ms).

    It does strictly less work than `point_scalar_mul(k, BASEPOINT)` -- the 15
    additions of the window table, measured 0.085--0.105ms, which the basepoint
    only ever needs built once. THAT SAVING IS SMALLER THAN THE RUN-TO-RUN NOISE
    ON THIS HOST, so the two functions' measured ranges overlap and no speed
    claim is made here; the module docstring has the numbers and the argument.
    Rule 3 prefers removing work to doing it faster, and that is the whole
    justification for this function existing.
    docs/dleq_cross_curve_design.md section 4.1 records the same shape of win,
    there a measured factor of 3.5, for `ecdsa`'s fixed-base precompute on
    secp256k1 -- where the table is much larger and the win is outside the
    noise.

    `scalar_base_mul(1) == BASEPOINT` is one of the tests, and it is not
    tautological: it is the one assertion that catches a window loop whose top
    window is dropped or whose table is built off by one.
    """
    return _scalar_mul_with_table(scalar_reduce(scalar), _BASEPOINT_WINDOW)


# --- Subgroup membership -----------------------------------------------------


def is_torsion_free(point: Point) -> bool:
    """True if `point` lies in the prime-order subgroup: l*P == identity.

    THE TREE HAD NO SUCH CHECK BEFORE THIS FILE. Measured 2026-09-27 by grepping
    every .py in the repository for `torsion`, `cofactor` and the digits of l:
    the only ed25519 arithmetic was chains/solana_address.py, which needs curve
    membership rather than subgroup membership and does not contain l at all.
    docs/dleq_cross_curve_design.md section 3.4 records the same measurement and
    names what depends on it -- `sigma_fun`'s cross-curve verifier calls
    `is_torsion_free()` on the claimed key as the very first thing it does,
    before even the bit-count check, and MRL-0010 delegates it in the one line
    quoted in `is_canonical`.

    WHY A CURVE CHECK IS NOT ENOUGH. ed25519 has cofactor 8. A point with a
    torsion component satisfies the curve equation, encodes canonically, and is
    NOT in the group any discrete-log claim is about: if A = x*B + T with T of
    order 8, then A is a fine-looking public key for which "the discrete log of
    A base B is x" is false, and eight different scalars become
    indistinguishable to a verifier that does not check. On a cross-curve DLEQ
    that is the entire failure the proof exists to prevent.

    THE COST, because it is why this is a separate call rather than something
    `decompress` does for free: one full 253-bit scalar multiplication plus its
    table, measured 0.00149--0.00163µfn (1.80--1.97ms). Multiplying by l is the CORRECT check
    and this function does it. Two cheaper things exist and both answer a
    DIFFERENT question, so neither is used here:

      - `has_small_order` below is 8*P == identity, and it is the complement of
        nothing: a point can be neither small-order nor torsion-free. B + T is
        the example and the test pins it.
      - Multiplying the whole verification equation by 8 -- "cofactored"
        verification, what ZIP-215 and libsodium's batch verification do -- makes
        the torsion component irrelevant to the outcome instead of checking for
        it. That is the right engineering answer for signature verification and
        it is NOT available to a DLEQ verifier, because the claim there is about
        one specific scalar rather than about the existence of some scalar.

    So a caller verifying 252 commitments per curve must not call this 252 times
    (0.396µfn / 479ms of pure overhead): it checks what the protocol requires --
    the claimed keys and the sums -- and that choice belongs in code that has a
    protocol, which is not this file.
    """
    return _scalar_mul_with_table(GROUP_ORDER, _window_table(point)).is_identity()


def has_small_order(point: Point) -> bool:
    """True if `point` is one of the eight points the cofactor kills: 8*P == identity.

    Three doublings, measured 0.0000129--0.0000183µfn (0.016--0.022ms) -- about
    100 times cheaper than `is_torsion_free`, which is why both exist rather
    than one.

    NOT THE COMPLEMENT OF `is_torsion_free`, AND READING IT AS ONE IS THE BUG
    THIS DOCSTRING EXISTS TO PREVENT. All three cases are real, and the test
    asserts all three:

        B              torsion-free, not small order    the honest case
        T of order 8   small order, not torsion-free    the obvious attack input
        B + T          NEITHER                          the one that gets through

    A verifier that refuses small-order points and accepts everything else
    accepts B + T, which is precisely what a malicious prover would send: it
    looks like a key, it is not in the prime-order subgroup, and no amount of
    small-order blacklisting catches it. libsodium's
    `crypto_core_ed25519_is_valid_point` checks BOTH -- read 2026-09-27 at
    src/libsodium/crypto_core/ed25519/ref10/ed25519_ref10.c, which does
    canonical, on-curve, `ge25519_has_small_order` (an algebraic test: X == 0,
    Y == 0, Z == 0, or Y*sqrt(-1) == +-X) and `ge25519_is_on_main_subgroup` --
    and that is the shape a caller here should copy.

    The eight points this detects are the 8-torsion subgroup, and
    tests/test_ed25519_group.py DERIVES all eight canonical encodings from this
    module's own arithmetic rather than pasting a published list: for any curve
    point P, l*P has order dividing 8. It then cross-checks the derived set
    against libsodium and against one encoding published independently in
    ziglang/zig's ed25519 test suite.
    """
    return point.double().double().double().is_identity()
