# Cross-curve DLEQ for a GRC<->XMR swap: design, parameters, and the case against

    Role: design document (research; no code)
    Reads: nothing at runtime. Written from sources cited inline plus
           measurements taken in this container.
    Writes: nothing
    Can move funds: no. It DESCRIBES a component that would sit on the order
           path of a GRC<->XMR swap, which is why every claim below is labeled
           measured, read-from-source, or unverified.
    Mainnet-safe: not applicable -- this is prose. The thing it describes is
           not mainnet-safe and section 7 argues it may never be.

STATUS. **No implementation exists and none is proposed by this document.** This
is the review artifact asked for before code: a plausible-but-wrong
cryptographic implementation that passes its own tests is the worst outcome
available here, and a design is cheaper to reject than a library.

Two conventions from CLAUDE.md govern the numbers below. Rule 3: every count
carries its denominator, and anything not measured says so. Rule 6: every
reported timing is in microfortnights, `1µfn = 1.2096s`, written with the seconds
in parentheses. Rule 17: a reason to believe something is not the same as having
checked it, and the two are never written in the same voice.

WHERE THIS SITS IN THE TREE. `swap_terminal/modules/adaptor_ecdsa.py` already
exists (read 2026-09-27; 779 lines) and implements the secp256k1 half: ECDSA
pre-signature, pre-verification, `adapt()`, and `recover_adaptor_secret()`,
against the DLC specification, with eleven of that specification's own published
vectors passing. Its own docstring names what is missing, and this document is
about that missing piece:

> It needs a Gridcoin testnet, a Monero stagenet, and the other two components
> (the cross-curve discrete-log-equality proof, and the Monero 2-of-2 spend
> path) that do not exist yet.

One thing that file makes clear and that reframes the Monero side: the XMR is
locked to an ordinary address whose private spend key is `s = s_a + s_b`. There
is no Monero multisig, no CLSAG work, and no ring-signature code in Python.
Whichever party learns both shares reconstructs `s` and imports it into
`monero-wallet-rpc` (`generate_from_keys`). The hard cryptography is entirely on
this document's subject and on the adaptor signature -- not on the Monero
transaction.

---

## 1. THE CONSTRUCTION

### 1.1 The primary source, and how to read it from this container

The construction is **MRL-0010, "Discrete logarithm equality across groups",
Sarang Noether, Monero Research Lab, December 4, 2018** (3 pages).

    canonical    https://www.getmonero.org/resources/research-lab/pubs/MRL-0010.pdf
    reachable    https://raw.githubusercontent.com/monero-project/monero-site/master/resources/research-lab/pubs/MRL-0010.pdf

Measured 2026-09-27: `www.getmonero.org` answers `403 CONNECT tunnel failed`
through this container's egress proxy -- the same block
`swap_terminal/chains/monero.py` records for the wallet-RPC documentation -- and
so do `eprint.iacr.org`, `cic.iacr.org`, `arxiv.org` and `web.archive.org`. The
`raw.githubusercontent.com` copy inside the Monero website's own repository is
byte-served (HTTP 200, 202,372 bytes) and is what the text below was extracted
from. **`monero-project/research-lab` does not contain MRL-0010**: its
`publications/bulletins/` holds MRL-0001 through MRL-0006 plus two in-progress
notes (listed 2026-09-27). Anyone re-checking this should use the monero-site
path or ask the operator to fetch the canonical URL.

### 1.2 What the note actually specifies

The summary in the task -- bit-by-bit, Pedersen commitments on both curves, a
small ring signature per bit proving 0-or-1, and a sum argument -- **is correct
in outline.** Quoting the abstract verbatim:

> The scheme expresses the common value as a scalar representation of bits, and
> uses a set of ring signatures to prove each bit is a valid value that is the
> same (up to an equivalence) across both scalar groups.

and section 1:

> Since there is no meaningful map assumed between the two groups, our approach
> is to decompose x into bits, treating each bit as a scalar in both Zp and Zq
> using our equivalence, and generate commitments to each bit in both groups.
> For each bit, we will construct a Schnorr-type ring signature showing that the
> bit commitment is valid and the same value in each group.
>
> This method was originally proposed publicly by Andrew Poelstra.

So Poelstra is the origin, per the note itself, and MRL-0010 is the written
specification. Notation, verbatim from section 1: groups `G`, `H` with generator
pairs `G, G'` and `H, H'`, orders `|G| = p` and `|H| = q`, hash functions
`H_G : {0,1}* -> Z_p` and `H_H : {0,1}* -> Z_q`, and **"Without loss of
generality, assume p <= q. Choose x in Z such that 0 <= x < p."** That last
sentence is section 3 of this document.

The prover, from section 2.1, with `x = sum b_i 2^i`:

1. For `i` in `[0, n-2]` sample blinders `r_i in Z_p` and `s_i in Z_q`.
2. **The last blinder is not random.** Set
   `r_{n-1} = (2^{n-1})^{-1} sum_{i=0}^{n-2} r_i 2^i` and likewise `s_{n-1}`,
   "to ensure that `sum r_i 2^i = sum s_i 2^i = 0`".
3. Two Pedersen commitments per bit: `C^G_i = b_i G' + r_i G` and
   `C^H_i = b_i H' + s_i H`.
4. Because the blinders cancel, the weighted sums are exactly the claimed keys:
   `sum 2^i C^G_i = x G'` and `sum 2^i C^H_i = x H'`.
5. Per bit, a two-branch Schnorr ring signature computed **in both scalar fields
   from the same preimage** -- `e^G` via `H_G` and `e^H` via `H_H` over the same
   tuple `(C^G_i, C^H_i, ...)`. The branch chosen is shared between the curves,
   which is the mechanism that ties bit `b_i` on one curve to bit `b_i` on the
   other.

The proof tuple, verbatim:

> `(xG', xH', {C^G_i}, {C^H_i}, {e^G_{0,i}}, {e^H_{0,i}}, {a_{0,i}}, {a_{1,i}}, {b_{0,i}}, {b_{1,i}})`

The verifier (section 2.2) checks the two weighted sums, recomputes
`e_1` then `e_0` per bit, and compares the recomputed `e_0` against the tuple's.
It closes with a requirement that is easy to skip and is a money bug when
skipped:

> The verifier is assumed to have also checked each proof tuple element to
> ensure it belongs to the expected group, to account for a malicious prover.

### 1.3 Four corrections to the outline, each of which is a place to get it wrong

**(a) There is no separate "the two sets sum to the same scalar" proof.** Each
set is checked against *its own* claimed public key, `sum 2^i C^G_i = xG'` and
`sum 2^i C^H_i = xH'`. Cross-curve equality comes from the per-bit ring
signatures sharing one branch decision, not from a sum comparison. An
implementation that adds a "sum equality" check is checking something that
cannot be expressed (the two sums live in different groups); an implementation
that *relies* on one has misunderstood where the binding comes from.

**(b) The forced last blinder is the load-bearing trick, and its sign is a
trap.** The weighted sum equals `xG'` only because `sum r_i 2^i = 0`, which
requires `r_{n-1} = -(2^{n-1})^{-1} sum_{i<n-1} r_i 2^i`. The text extracted
from the PDF by `pypdf` renders that line **without a leading minus**;
`AthanorLabs/go-dleq` computes `blinders[i] = sum.Negate().Mul(currPowerOfTwoInv)`
(read at `prove.go`, `generateCommitments`) and then asserts the running sum is
zero. The negation is required by the arithmetic, so this is a transcription
question and not an open one -- but it is exactly the class of detail that
produces a proof which verifies against the wrong point. Whether the published
PDF carries the minus and the extractor dropped it is **unverified**; the
arithmetic settles what the code must do either way.

**(c) MRL-0010 as published is not sufficient, and both real implementations
augment it.** This is the most important correction. `serai-dex/serai`'s
`crypto/dleq/README.md` (read 2026-09-27):

> The present cross-group DLEq is based off MRL-0010, which isn't
> computationally correct as while it proves both keys have the same discrete
> logarithm for their `G'`/`H'` component, it doesn't prove a lack of a `G`/`H`
> component. Accordingly, it was augmented with a pair of Schnorr Proof of
> Knowledges, proving a known `G'`/`H'` component, guaranteeing a lack of a
> `G`/`H` component (assuming an unknown relation between `G`/`H` and
> `G'`/`H'`).

`go-dleq`'s README states the same augmentation in different words: "In addition
to what's specified in the paper, it contains an additional proof of knowledge
of the witness ie. a signature on both curves." Its `Proof` struct carries
`signatureA, signatureB` and `Verify()` checks both (read `prove.go`,
`verify.go`). **A port of the note alone would be a proof of the wrong
statement.** Any implementation here must carry the two proofs of knowledge, and
the reason must be written at the site, because the note does not say it.

**(d) A second, inequivalent shape is what the production Monero swap actually
uses.** `comit-network/xmr-btc-swap` imports
`sigma_fun::ext::dl_secp256k1_ed25519_eq::CrossCurveDLEQ` (read
`swap/src/protocol.rs` line 12 and `swap/Cargo.toml`: `sigma_fun = { version =
"0.7", features = ["ed25519", "secp256k1", ...] }`). That module's own docstring
(read `sigma_fun/src/ext/dl_secp256k1_ed25519_eq.rs`, 340 lines) says "This was
partially inspired by [MRL-0010] but it re-imagines it as a Sigma protocol," and
it differs in three ways that matter to anyone writing test vectors:

- the commitment is to `b_i * 2^i * H` rather than to `b_i * H'` with the weight
  applied at sum time, so the verifier adds commitments with weight 1;
- the **sum of the Pedersen blindings is published** (`sum_blindings`), and a
  pair of same-curve DLEQ proofs then tie `sum C_i - sigma*G` to the claimed key;
- the OR proofs are composed generically (`And<All<Or<And<DLG,DLG>,And<DLG,DLG>>,
  U252>, And<Eq<DLG,DL>, Eq<DLG,DL>>>`) with one merged Fiat-Shamir challenge of
  31 bytes (`U31`).

Per rule 8, if this repo picks one shape, the other belongs in a comment at the
site naming it -- the two are not wire-compatible and a vector from one will
never verify under the other.

**(e) The later academic treatment is unread from here.** `noot/dleq-rs` states
it implements the protocol of `https://eprint.iacr.org/2022/1593.pdf` ("Proofs
of discrete logarithm equality across groups") plus "the extension in section 5
which allows for values larger than `BITLEN_WITNESS` to be proven", and
describes itself as "not production-ready ... I wrote this for learning purposes
only." **The paper itself could not be fetched** (`eprint.iacr.org` and
`cic.iacr.org` both blocked from this container), so its construction, its
relation to MRL-0010, and whether it is cheaper are **unverified**. If this work
proceeds, that paper is the first thing the operator should fetch, because a
logarithmic-size proof would change every number in section 2. `serai`'s README
notes the same hope: "All proofs are suffixed 'Linear' in the hope a logarithmic
proof makes itself available."

### 1.4 The generators, and why they are not a free choice

Both curves need a second generator with no known discrete-log relation to the
first. `go-dleq` hardcodes both, and both turn out to be the standard
nothing-up-my-sleeve constants:

    secp256k1  0250929b74c1a04954b78b4b6035e97a5e078a5a0f28ec96d547bfee9ace803ac0
    ed25519    8b655970153799af2aeadc9ff1add0ea6c7251d54154cfa92c173a0dd39c1f94

**Measured here, not recalled:** `sha256(04 || Gx || Gy)` -- the *uncompressed*
SEC1 serialization of the secp256k1 generator -- is exactly
`50929b74c1a04954b78b4b6035e97a5e078a5a0f28ec96d547bfee9ace803ac0`, the
x-coordinate above. The compressed forms do not match; the derivation is
specifically over the uncompressed encoding, and an implementation that hashes
the 33-byte form gets a different, unrelated point. The ed25519 constant is
Monero's RingCT `H`: it appears verbatim in the Monero Research Lab's own
`source-code/MiniNero/RingCT.py:10` and `RingCT2.py:44` as `getHForCT()`, whose
dead code beneath the `return` shows the derivation it replaced
(`hashToPoint_ct(publicFromInt(1))`).

Using those two published constants avoids implementing hash-to-point in Python
entirely, which matters for section 4.

---

## 2. CONCRETE PARAMETERS

### 2.1 Bit count

    ed25519  l = 2^252 + 27742317777372353535851937790883648493
               = 0x1000000000000000000000000000000014def9dea2f79cd65812631a5cf5d3ed
               253 bits
    secp256k1 n = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
               256 bits, and n / l ~ 16.0

Computed here. `l < n`, so ed25519 is MRL-0010's `p` and the scalar must be
below it. Both implementations reach the same number by the same route:

- `go-dleq`: `bits := min(curveA.BitSize(), curveB.BitSize())` with
  `ed25519.BitSize() == 252` and `secp256k1.BitSize() == 255`, so **252**, and
  `checkWitnessSize` refuses anything larger (read `prove.go`, `ed25519/curve.go`,
  `secp256k1/curve.go`).
- `sigma_fun`: `const COMMITMENT_BITS: usize = 252;` and
  `assert!(secret.as_bytes()[31] & 0b00010000 == 0)` in `prove()`, with a test
  named `high_scalar_should_panic`.

**252 bits, and the constraint is not negotiable downward or upward.**

### 2.2 Proof size

Computed from `go-dleq`'s `serde.go` plus the two curves' `CompressedPointSize()`
(33 for secp256k1, 32 for ed25519) and its `const scalarLen = 32`:

    per bit   C_A 33 + C_B 32 + 6 scalars x 32 = 257 bytes
    252 bits                                     64,764
    claimed keys X_A 33 + X_B 32                     65
    bit count byte                                    1
    len(signatureA) byte + DER ECDSA signature   71 to 73
    len(signatureB) byte + ed25519 signature           65
    TOTAL                              64,966 to 64,968 bytes = 63.4 KiB

**THE COMPUTED 64,960 IN THE LINE ABOVE WAS WRONG AND IS KEPT NOWHERE -- this
table is the corrected one.** It read "two 64-byte signatures with length
prefixes, 130", and both halves of that are false. `signatureA` is a
**DER-encoded** ECDSA signature over secp256k1, so it is 70, 71 or 72 bytes
depending on whether r and s each need a leading 0x00 to stay positive; only
`signatureB` (ed25519) is a fixed 64. **The total is therefore not a constant.**

Measured, not deduced: the three proofs captured from the Go implementation on
2026-09-27 (tests/vectors/dleq_cross_curve_go.json) are 64,967, 64,968 and
64,966 bytes, with `signatureA` lengths of 71, 72 and 70 -- each starting 0x30,
DER's SEQUENCE tag. `swap_terminal/modules/dleq_proof_format.py` parses all
three to the last byte with zero trailing, and
`tests/test_dleq_proof_format.py::test_the_secp256k1_signature_is_der_and_its_length_varies`
is the assertion that would have caught this: a format with two fixed 64-byte
signatures cannot produce three different totals. A verifier written to 64,960
would have rejected every real proof, and the one-byte length prefixes the
format spends on each signature are themselves the tell that at least one of
them is variable.

For comparison, `serai`'s README publishes **measured** sizes and verification
times for its four variants on an Intel i7-118567 with `k256`/`curve25519_dalek`
-- a compiled Rust stack, so the times are a floor, not a prediction for Python:

    ClassicLinear      56,829 bytes    157 ms
    ConciseLinear      44,607 bytes    156 ms   (their reference)
    EfficientLinear    65,145 bytes    122 ms
    CompromiseLinear   48,765 bytes    137 ms

`sigma_fun`'s commitments alone are `252 * (33 + 32) = 16,380` bytes plus a
64-byte blinding sum plus a `CompactProof` whose serialized length was **not
computed here** (it is a generic-array type; reading its size needs the crate
built, which was not done).

Order of magnitude: **a 45-65 KiB message per proof, two proofs per swap.** That
is fine over this repo's HTTP transport and fine to store; per rule 5 it belongs
in SQL as a blob or hex column on the swap row, with the verification VERDICT
stored as a column so no decision requires re-parsing the blob.

### 2.3 Cost in pure Python

Two things were measured and one was computed. **Nothing end-to-end was
measured, because nothing is implemented.**

MEASURED in this container (Python 3.11.15, `ecdsa` 0.19.2, single core, 200
operations per figure except where stated):

| operation | measured |
|---|---|
| ed25519 variable-base scalarmult, extended coords, double-and-add | 0.00140µfn (1.69ms) |
| ed25519 same, 4-bit window with the table built per call | 0.00129µfn (1.56ms) |
| ed25519 fixed-base with a persistent 4-bit table | 0.00117µfn (1.42ms) |
| secp256k1 `generator * k` via `ecdsa` (library precompute) | 0.000405µfn (0.49ms) |
| secp256k1 arbitrary point `* k` via `ecdsa` (50 ops) | 0.00131µfn (1.58ms) |
| secp256k1 fixed-base `H` with `PointJacobi(..., generator=True)` | 0.000446µfn (0.54ms) |
| ed25519 `sum 2^i C_i` over 252 points, Horner (1 run) | 0.00187µfn (2.26ms) |
| ed25519 same sum done naively as 252 scalarmults (1 run) | 0.192µfn (232ms) |
| secp256k1 `sum 2^i C_i` over 252 points, Horner (1 run) | 0.00306µfn (3.70ms) |

The benchmark was written to a scratchpad outside this repository and is not part
of it; it hand-rolls the ed25519 extended-coordinate group law (the same
formulas any implementation would need) and uses `ecdsa` unchanged for
secp256k1.

COUNTED by reading `go-dleq`'s `verify.go` and `prove.go` rather than estimated:

    verifier, per bit, per curve   2 fixed-base (a1*H, a0*H)
                                   2 variable-base (e*C, e1*(C - G'))
    prover,   per bit, per curve   3 fixed-base (r*H, j*H, a0*H)
                                   1 variable-base (e*C)
    both                           one weighted sum per curve

PROJECTED from the two together -- this is arithmetic on measured per-operation
costs, not a measurement of a proof:

    verify   252 * (2*1.42 + 2*1.69 + 2*0.54 + 2*1.58) ms + 5.96 ms Horner
             = 2.18µfn (2.64s)
    prove    252 * (3*1.42 + 1.69 + 3*0.54 + 1.58) ms + 5.96 ms Horner
             = 1.91µfn (2.31s)
    per party, one proof generated and one verified   4.10µfn (4.95s)

Two consequences worth stating next to the numbers, in rule 14's sense:

- **Feasibility is not the problem.** Five seconds of CPU once per swap setup is
  affordable. Section 7's case against this is not a performance case.
- **The weighted sum must not be written the way `go-dleq` writes it.**
  `verifyCommitmentsSum` multiplies each commitment by `2^i`, which measured
  0.192µfn (232ms) on ed25519 alone; Horner (double, add, repeat) measured
  0.00187µfn (2.26ms) for the identical result, a factor of ~103. That is a
  deliberate divergence from the reference implementation and per rule 8 it needs
  a comment at the site naming what `go-dleq` does, because a reader
  cross-checking the two files will otherwise read it as a bug.

---

## 3. WHY THE ORDER MISMATCH MATTERS

This is the section most likely to decide whether an implementation is silently
broken, so it is written as four separate failure modes rather than one.

### 3.1 The statement is only well-formed below the smaller order

MRL-0010 requires `0 <= x < p` with `p = min(|G|, |H|)`, and says why: the
natural projections `Z -> Z_p` and `Z -> Z_q` are injective on that range, so
"there is a bijection between elements of Z_p and the restriction of Z_q." Above
it, there is no bijection and **there is no claim left to prove**: `x` and
`x mod l` are different integers with different bit decompositions, and "the two
public keys have the same discrete logarithm" is simply false as an equality of
integers even though both keys were derived from the same 256-bit number.

What an implementation does in that case is the question. `go-dleq` and
`sigma_fun` both refuse (`checkWitnessSize`, and an `assert!` respectively). An
implementation that *reduces* instead -- the natural thing to write, because both
libraries' scalar types reduce on construction -- produces two commitment sets
over different bit strings, and the per-bit ring signatures then fail. **That
failure is loud**, which is the good case, and it is why "sample 256 bits and
hope" is not the danger.

### 3.2 The dangerous case is the sum, and it is silent

The Monero spend key is `s = s_a + s_b mod l`. Each share is a 252-bit value, so
`s_a + s_b < 2^253`, and `2^253 < n` -- therefore **on secp256k1 the sum never
reduces**, while **on ed25519 it reduces whenever the sum reaches `l ~ 2^252`,
which happens for roughly half of all share pairs** (computed: the sum is
distributed on `[0, 2^253)` and `l/2^253 ~ 0.5`).

So `S_a + S_b` on secp256k1 and `S_a + S_b` on ed25519 are commitments to
*different integers* about half the time, with no error anywhere. The protocol
consequence:

- Each share gets its **own** DLEQ proof and its **own** adaptor. Never one
  proof about the sum.
- Nothing may compare a secp256k1 sum point against the Monero spend-key point,
  or derive one from the other. The addition happens on the ed25519 side only,
  after both shares are known as integers.
- A test that seeds two shares whose sum is below `l` passes, and the same test
  with two large shares fails. **A soundness test suite that samples shares
  uniformly will hit both cases and a suite that uses small fixtures will not**,
  which is exactly the kind of thing to pin deliberately rather than leave to
  the random seed.

### 3.3 The standard mitigation, and what it costs

Sample the witness uniformly from `[0, 2^252)`. `go-dleq` does this in
`GenerateSecretForCurves` -> `generateRandomBits(min(bitsize))`, clearing the top
bits of the little-endian buffer; `sigma_fun` asserts bit 252 is clear.

Entropy cost, computed: `2^252 / l = 1 - 3.8e-39`, i.e. **restricting to 252 bits
gives up essentially nothing relative to the ed25519 key space** (`l` is only
barely above `2^252`), and gives up 4 bits relative to secp256k1. Generic
discrete-log security is `sqrt(2^252) = 2^126` either way. There is no security
argument against the restriction, and there is no reason to be clever about it.

### 3.4 The checks MRL-0010 delegates to the verifier, spelled out

The note's one-line requirement that every tuple element "belongs to the
expected group" is, on ed25519 specifically, three separate checks that a Python
implementation must do by hand:

- **Canonical encoding.** `y >= p` must be rejected.
  `swap_terminal/chains/solana_address.py::_raw_is_on_curve` already does exactly
  this and says why ("curve25519-dalek rejects these, so this matches what the
  chain itself would do rather than what the arithmetic would tolerate").
- **On the curve.** Same file, Euler's criterion.
- **Torsion-free / prime-order subgroup.** ed25519 has cofactor 8; a point with a
  torsion component is on the curve, encodes canonically, and is not in the group
  the claim is about. `sigma_fun`'s `verify()` does this first:
  `if proof.commitments.len() != COMMITMENT_BITS || !claim.1.is_torsion_free()
  { return false; }`. This repo has **no** torsion check today -- measured: the
  only ed25519 arithmetic in the tree is `solana_address.py`, which needs
  membership and not subgroup membership.
- **Non-zero witness.** Both implementations refuse zero.

---

## 4. WHAT PYTHON WOULD NEED, AND WHETHER IT ALREADY HAS IT

### 4.1 secp256k1: entirely covered, with no new dependency

`ecdsa` is declared in `swap_terminal/requirements.txt` at `>=0.19,<1.0` and
measured working at 0.19.2. `modules/adaptor_ecdsa.py` already demonstrates
everything the DLEQ needs from it -- `SECP256k1`, `PointJacobi`, `INFINITY`,
compressed SEC1 encode/decode, scalar arithmetic mod the curve order -- and its
docstring already argues the dependency question the same way this document
would: `coincurve` and `secp256k1` are libsecp256k1 bindings and "would put a
compiled extension on a host that holds BTC_RPC_PASS, LTC_RPC_PASS, GRC_RPC_PASS
and wallet keys".

One measured optimization for this specific proof: nearly all secp256k1
multiplications are against the **fixed** alternate generator `H`, and wrapping
`H` in `PointJacobi(..., generator=True)` builds `ecdsa`'s precompute table,
measured 0.000446µfn (0.54ms) against 0.00131µfn (1.58ms) for the same point
without it -- a factor of 3.5 for one line.

### 4.2 ed25519: possible, not present, and the gap is bigger than it looks

What the tree has: `swap_terminal/chains/solana_address.py` hand-writes the field
prime `2^255 - 19`, computes `d = -121665/121666` rather than spelling it, and
decides curve membership by Euler's criterion. Read in full. It is real evidence
that this kind of arithmetic is acceptable here, and its own docstring makes the
argument: "Adding a compiled Rust extension to a host that holds wallet
credentials, to compute two hashes and a Legendre symbol, is a worse trade than
40 lines that are tested."

What it does **not** have, and each item is new code:

- the group law (point addition and doubling; extended coordinates are the sane
  choice and are what the benchmark above used);
- `x` recovery from a compressed point -- `_raw_is_on_curve` deliberately stops
  short of it: "It does not recover x, because nothing here needs x";
- scalar arithmetic mod `l`, including inversion for the forced last blinder;
- a torsion / prime-order-subgroup check;
- hash-to-scalar into `Z_l` and into `Z_n`.

Measured feasibility: a pure-Python ed25519 variable-base scalarmult is
0.00140µfn (1.69ms), and section 2.3 projects the whole proof at single-digit
seconds. **Yes, this can be done in pure Python at acceptable cost.** No
compiled dependency is required for the proof itself.

### 4.3 One primitive that is genuinely missing, and one that must not be used

**Keccak-256 is not in the standard library.** Measured:
`hashlib.algorithms_available` contains no name matching `keccak`, and
`hashlib.sha3_256(b"")` is `a7ffc6f8...` where Keccak-256 of the empty string is
`c5d24601...` -- different padding, different function, and reaching for
`sha3_256` because the name looks right is a silent break. Keccak would be needed
for Monero address construction and for hash-to-point. **Both are avoidable**:
use the two published generator constants from section 1.4 rather than deriving
`H`, and let `monero-wallet-rpc` build the address from the reconstructed keys.
If Keccak becomes unavoidable, it is ~100 lines of pure Python with published
KAT vectors, which is a better trade than a dependency -- but it should be
avoided rather than written.

**`pycryptodome` 3.23.0 is importable in this container and must not be built
on.** It is a compiled extension, it is **not** in
`swap_terminal/requirements.txt` (measured: the file declares `base58` and
`ecdsa`, and the dev file declares `pytest` and `ruff`), so its presence here is
an artifact of the container and not of the deployment. `pynacl` and `monero` are
absent (measured: `ModuleNotFoundError` for both).

### 4.4 The constant-time problem, restated because it is worse here

`adaptor_ecdsa.py` already states the general form: "pure-Python group arithmetic
is not constant time, so this code is NOT side-channel resistant ... it is not
fixable within the no-compiled-extension constraint -- it is a genuine trade, not
an oversight."

The DLEQ makes it sharper in one specific way: **the prover branches on the
individual bits of the secret.** `go-dleq` has a literal `switch x { case 0: ...
case 1: ... }` inside `generateRingSignature`, with different point arithmetic in
each arm. `sigma_fun` goes to visible effort to avoid exactly this --
`PointP::conditional_select(...)` with a `subtle::Choice`, plus a `black_box`
wrapper and `zeroize` on the bool, in a function whose comment reads "Make sure
to do a constant time choice here."

In Python, `conditional_select` is not available and cannot be faked reliably
(integers are variable-width, `int.__mul__` is not constant time, and the
interpreter reorders nothing predictably). So the design must choose, explicitly
and in writing:

- compute **both** arms unconditionally and select with arithmetic rather than a
  branch, which doubles the prover's cost to a projected ~3.8µfn (4.6s) and still
  leaks through data-dependent integer widths; or
- accept the leak and state that the prover must not be exposed to an attacker
  who can time it -- which on a networked swap endpoint is a claim about the
  transport, not about this file.

Neither is free, and a design that does not say which one it chose has chosen the
first-draft default, which is the leaky branch.

---

## 5. TEST VECTORS AND OFFLINE VERIFICATION

This is where the honest answer is least comfortable.

### 5.1 There are no published known-answer vectors. Measured.

`adaptor_ecdsa.py` had an unusual advantage: the DLC specification ships
`test/ecdsa_adaptor.json`, produced by Blockstream's C implementation, and eleven
vectors are embedded in this repo's tests. **Nothing equivalent exists for
cross-curve DLEQ**, established by reading the tests of all three
implementations found:

- `go-dleq/dleq_test.go` (78 lines, read in full): four tests --
  `TestWitnessSize`, `TestGenerateCommitments`, `TestGenerateRingSignature`,
  `TestProveAndVerify`. Every one generates a fresh random secret and checks
  round-trip or internal consistency. **No fixed vectors, and no negative test
  that a forged proof is rejected.**
- `sigma_fun`'s `dl_secp256k1_ed25519_eq.rs` test module (read): one
  `should_panic` test for a 253-bit scalar and two `proptest`s with three cases
  each -- prove-then-verify, and serialization round-trip. **No fixed vectors, no
  soundness test.** And see 5.2.
- `serai`: no vectors; the README calls the cross-group proof `experimental` with
  "no formal proofs available", and the March 2023 Cypher Stack audit explicitly
  **excludes** the `experimental` feature.

So a Python implementation cannot be checked against a published number. It can
only be checked against another implementation, which is the repo's established
pattern (`solders` for the Solana derivations, the `cryptoconditions` PyPI
package for the XRP crypto-condition encoder, `xrpl-py` for serialization) and
the plan in section 6 follows it.

### 5.2 An observation about the production configuration, offered as a review
item and not as a finding

Read, and therefore verified as source facts:

- `sigma_fun`'s commitment is `commit(b) = r*G + b*H` where `G` is each curve's
  standard basepoint and `H` is supplied by the caller (its own comment says so,
  and `prove()` computes `zero_commit_p = g!(rP * GP)` then adds `H2P`).
- `xmr-btc-swap` constructs the proof system with
  `CrossCurveDLEQ::new((*ecdsa_fun::fun::G).normalize(),
  curve25519_dalek::constants::ED25519_BASEPOINT_POINT)` -- that is, **`H` is the
  standard basepoint on both curves, so `H == G`.**
- `sigma_fun`'s own proptests instead draw `HP in any::<PointP>()` and
  `HQ in ed25519_point()`, i.e. **random** generators. The configuration shipped
  in production is not the configuration the tests exercise.

**Unverified, and it is a hypothesis in rule 17's sense:** with `H == G` the
per-bit OR proof looks like it loses its binding, because both branches
(`C = r*G` and `C - 2^i*H = r'*G`) then have a witness the prover can compute for
any `C` it constructed, so the branch choice would no longer testify to the bit.
If that reading is right, the cross-curve tie would be vacuous and a malicious
counterparty could commit to different scalars on the two curves -- a fund-loss
bug in a tool with a long production history, which is itself strong evidence
that **the reading is more likely wrong than right.** I could not test it:
running the Go and Rust implementations in this session was refused by the
sandbox, so there is no experiment behind this paragraph.

It is written down anyway for two reasons. First, if this repo ever adopts
`sigma_fun`'s shape, this question must be settled before a line is written.
Second, it is the section-7 argument in miniature: a parameter choice inside a
widely used implementation that cannot be eyeballed as safe, and whose own test
suite does not cover the production setting.

### 5.3 What can be tested offline, with no chain and no network

Everything in this subsection needs nothing but Python and pytest.

**Completeness.** A correct proof verifies. Over random witnesses in
`[1, 2^252)`, and over the boundary fixtures that random sampling will not
reliably produce: `x = 1`, `x = 2^252 - 1`, `x` with alternating bits, `x` with a
single high bit, `x` with a single low bit.

**Soundness, as mutation tests -- one per invariant, each asserting rejection.**
The list is the design, because each entry corresponds to a check that could be
omitted and would otherwise never be noticed:

1. Two different scalars, one per curve (`x` on secp256k1, `x + 1` on ed25519) --
   the headline case, and it must fail.
2. `x` on secp256k1 against `x mod l` on ed25519, for an `x` in `[l, 2^253)` --
   section 3.2's silent case made explicit.
3. Flip one bit of one commitment set, at index 0, at index 251, and at a middle
   index. Index 251 is the forced-blinder index and is the one most likely to be
   special-cased wrongly.
4. Swap `C_i` and `C_j` between two indices; the weighted sums change, so this
   must fail on the sum check specifically.
5. Replace one commitment with the group identity.
6. Tamper each of the six per-bit scalars in turn.
7. Truncate the proof by one byte, and extend it by one byte.
8. Offer a claimed ed25519 key with a torsion component (add a point of order 8);
   must be refused before any other work.
9. Offer a non-canonical ed25519 encoding (`y >= p`).
10. Offer an off-curve 32-byte string as a commitment.
11. Witness `0`, and witness `>= 2^252`: the prover must refuse, not produce
    something.
12. Omit the proofs of knowledge from section 1.3(c) and assert the verifier
    rejects -- i.e. pin the augmentation, not just the note.

**Each of those tests must itself be mutation-checked**, in the repo's sense:
delete the corresponding check from the implementation and confirm the test goes
red. A soundness test that passes for the wrong reason is worse than no test,
because it is a green check beside a missing guard.

**Structural invariants, computed a second way in the test.** Assert
`sum 2^i C_i == X` using a naive loop in the test while the implementation uses
Horner (section 2.3). Two independent computations of the same quantity is the
cheapest defense against a fast-path bug, and it is the only place in this design
where duplicating logic is correct -- which per rule 8 needs saying at both sites.

**The join with the adaptor.** End-to-end, offline, no chain: generate a share
`x`, prove it across the curves, run `adaptor_ecdsa.pre_sign` under `Y = x*G`,
`adapt()`, then `recover_adaptor_secret()`, and assert the recovered integer is
the same `x` whose ed25519 commitment the DLEQ verified -- **and** that
`x * ed25519_basepoint` equals the point the Monero side would use. This is the
test that catches the encoding disagreements (big-endian versus little-endian
scalars, compressed point forms) that are otherwise found on a stagenet.

### 5.4 Zero-knowledge is not testable, and here is what stands in for it

Stated plainly: no test establishes zero-knowledge. It is a statement about the
existence of a simulator, and the honest position is that this property rests on
MRL-0010's argument and on review, not on the suite. What can be tested is the
set of ways an implementation *breaks* ZK, and those are worth having:

- **Fresh randomness per proof.** Two proofs of the same witness must differ in
  every blinder-derived field. Blinder reuse across two proofs of the same `x`
  leaks the witness outright.
- **Shape independent of the secret.** The serialized length and the element
  count must be identical for `x = 0b000...`, `x = 0b111...` and random `x`. A
  length that moves with the bits is a direct leak.
- **Timing independent of the secret, measured and reported in µfn.** Time
  `prove()` for an all-zero-bits witness and an all-one-bits witness over many
  runs and assert the difference is under a stated threshold. This is a real test
  for section 4.4's branch-on-secret-bit defect, it needs no chain, and it is the
  only mechanical check on the one property Python is worst at. It will be noisy;
  the threshold and the run count go in the test with their denominator, and a
  failure means "read the branch", not "re-run it".
- **No secret-derived value in the transcript.** This one is review, not test,
  and should be an explicit reviewer checklist item rather than a pretended
  assertion.

### 5.5 The cross-check that would actually be worth something

`go-dleq` is the best available oracle, for a reason specific to it: it has an
explicit, stable wire format (`Serialize` / `Deserialize` in `serde.go`, read),
so proofs cross the language boundary in both directions.

    Go proves  -> bytes -> Python verifies      catches verifier bugs
    Python proves -> bytes -> Go verifies       catches prover bugs

The second direction is the valuable one and is the direction the `solders` and
`cryptoconditions` cross-checks could not offer. Captured vectors then get
embedded as literals in `tests/`, exactly as
`tests/test_xrp_crypto_condition.py` holds the `cryptoconditions` output, and
`go-dleq` never becomes a dependency of this repository.

Three honest caveats:

- **It was not done in this session.** Running `go test` was refused by the
  sandbox ("Permission for this action was denied ... [Code from External]"),
  although Go 1.24.7 is installed at `/usr/local/go/bin/go`. So the wire format
  described in section 2.2 is read from source and has **not** been confirmed
  against a byte stream. This needs the operator's approval to run third-party
  code, and it is the single most valuable thing they could authorize.
- **Scalar endianness is unverified.** `go-dleq` encodes secp256k1 scalars
  through `ModNScalar.Bytes()` (big-endian) and ed25519 scalars through
  `edwards25519.Scalar.Bytes()` (little-endian), so one proof mixes both
  conventions. That is read from the libraries' conventions, not from a captured
  byte stream, and it is exactly the sort of thing to establish by experiment
  before writing a parser.
- **Agreement with `go-dleq` is not security.** A bit-for-bit match inherits
  every bug `go-dleq` has, and `go-dleq` has no soundness tests of its own
  (5.1). The cross-check proves the encoding and the arithmetic agree with one
  other unaudited implementation. It proves nothing about the construction.

---

## 6. A STAGED PLAN

The cost column is deliberately not in hours. What can be stated honestly is the
shape of each stage, what it needs, and what evidence it produces; a session's
duration is not something measured here, and rule 3 prefers saying so to
inventing a number that then hardens into a commitment.

| # | stage | needs a chain? | needs network? | evidence it produces | scale |
|---|---|---|---|---|---|
| 0 | this document, reviewed and either accepted or rejected | no | no | a decision | done |
| 1 | ed25519 group arithmetic: extended-coordinate add/double, decompress with `x` recovery, scalars mod `l`, torsion check | no | no | **RFC 8032 section 7.1 signature vectors**, consumed by a test-only verifier -- these are public known-answer vectors that exercise decompression, the group law and scalar reduction together, and they are the one KAT available anywhere in this design | ~200 lines plus tests |
| 2 | wire format: encode/decode `go-dleq`'s serialization, and capture cross-check vectors | no | Go module download only | captured byte vectors, embedded as literals | small, but needs operator approval to run third-party code (5.5) |
| 3 | verifier only, against stage 2's vectors | no | no | Go-produced proofs verifying in Python | ~250 lines |
| 4 | prover, then the full property and mutation suite of 5.3 and 5.4 | no | no | Python proofs verifying in Go; twelve mutation tests, each mutation-checked | ~300 lines plus a large test file |
| 5 | protocol design: which party proves what, when, share separation per 3.2, the join with `adaptor_ecdsa`, storage in SQL per rule 5 | no | no | a second design document, reviewed before code | prose |
| 6 | integration against Gridcoin testnet and Monero stagenet | **yes** | yes | the only evidence that the leaked scalar really opens the XMR | operator's host |
| 7 | independent cryptographic review before any mainnet posture change | n/a | n/a | the thing none of this produces | not ours |

**Stages 1, 3, 4 and 5 need no chain and no network at all.** Stage 2 needs a Go
toolchain and module fetch and nothing else. That is most of the work, which is
the one genuinely encouraging fact in this document: the expensive-to-arrange
part (stage 6) comes last and is small, and the part that decides whether the
crypto is right can be done entirely offline.

Stage ordering is not negotiable in one respect: **stage 3 before stage 4.** A
verifier written against an independent implementation's proofs is checked by
something other than itself. A prover written first is checked by the verifier
its own author wrote, and a matched pair of wrong implementations agrees
perfectly.

---

## 7. REASONS NOT TO BUILD THIS

The strongest version, as asked. I find this case stronger than the case for
building, and the deciding argument is not performance -- section 2.3 measured
that performance is fine.

**1. There is no audited implementation of this construction anywhere, in any
language.** Not "none in Python" -- none at all. Every implementation found says
so in its own words. `serai`: "neither the original postulation (which had flaws)
nor any construction here has been proven nor audited. Accordingly, they are
solely experimental, and none are recommended," and its Cypher Stack audit covers
everything **except** this feature. `sigma_fun`: "Highly experimental ... There
could easily be implementation mistakes especially in the more complicated proofs
in the `ext` module" -- and the cross-curve DLEQ is in `ext`. `noot/dleq-rs`:
"not production-ready, I wrote this for learning purposes only." Writing a fourth
implementation from a three-page note is not porting reviewed cryptography; it is
producing new unaudited cryptography, and section 1.3(c) shows the note alone
proves the wrong statement, so the port is not even mechanical.

**2. The repo's best verification technique is unavailable in the form that
matters.** `adaptor_ecdsa.py` is defensible largely because eleven vectors from
an independent C implementation pass. There is no such artifact here (5.1).
Cross-checking against `go-dleq` establishes that two unaudited implementations
agree -- valuable for encoding bugs, worth nothing against a construction error,
and `go-dleq` itself ships no soundness test. The verification story is
structurally weaker than the one this repo already accepted for the adaptor, and
it is weaker in the dimension that loses money.

**3. Section 5.2 is what the audit burden looks like.** A production tool with
years of swaps behind it passes its own basepoint as the commitment generator,
its tests use random generators instead, and one session of reading cannot settle
whether that is fine. If a careful read of an existing implementation cannot
resolve a question like that, a careful read of a new one will not either -- and
review is the only control this design has.

**4. Failure is total, delayed, and looks like an operational fault.** A broken
DLEQ does not error. It lets a counterparty commit to different scalars on the
two curves, take the Gridcoin, and leave XMR locked to a key nobody holds. The
symptom arrives after the money moved, and on first sight it reads as a stuck
swap. Every other unverified thing in this tree fails by refusing to work.

**5. The side channel is real and cannot be closed here.** The prover branches on
the bits of a Monero spend-key share, 252 times, in an interpreter that cannot be
made constant time (4.4). The mitigation that exists in the Rust implementation
-- `subtle::conditional_select`, `black_box`, `zeroize` -- has no Python
equivalent, and the Python version of "constant time" is a comment claiming it.

**6. The DLEQ is not the critical path, and building it first is the wrong
order.** Measured from its own docstring: **no part of
`swap_terminal/chains/monero.py` has ever been run against a
`monero-wallet-rpc`.** Its method names were confirmed against the published
spec on 2026-09-25 and its wire behavior remains, in its own words, "a
HYPOTHESIS". The Monero settlement path -- watching for the lock, unlock_time,
reorg handling, importing a reconstructed key, sweeping -- does not exist. A
correct DLEQ delivered onto an unproven Monero transport buys nothing, and the
work it displaces is work that could be verified.

**7. Two alternatives dominate on the evidence.**

- **Call out to a maintained implementation as a separate process.** This is the
  strongest anti-build option and it is not the thing the repo has already
  refused. `solana_address.py` and `adaptor_ecdsa.py` refuse a **compiled Python
  extension in the wallet-holding process**; a small `go-dleq` helper binary,
  invoked over stdin/stdout with no wallet credentials in its environment, is a
  different trade entirely -- no in-process ABI, no C in the interpreter that
  holds `GRC_RPC_PASS`, and the crypto lives in code other people are running.
  Its real costs are a Go toolchain in deployment and a serialization boundary,
  and that boundary is exactly what stage 2's vectors test. Note that the code
  called out to is still unaudited; what is bought is shared exposure and other
  people's bug reports, not a proof.
- **Do not offer GRC<->XMR trustlessly, and say so.** Keep the brokered
  hot-wallet path this repo already runs, and disclose that XMR is custodial. The
  comparison is uncomfortable but it is the right one: a custodial swap's failure
  mode is "the operator can take the funds", which is **known, disclosed, and
  priced by the user**; a hand-rolled DLEQ's failure mode is "the cryptography is
  wrong", which is **unknown, undisclosed, and also loses the funds**. Presenting
  the second as trustless because it has no custodian is the more dishonest of
  the two. Unless the operator can state an expected volume for this pair that
  justifies commissioning a review, the custodial path is the honest answer -- and
  if the volume cannot be stated, that is itself the answer.
- Third, weaker: **wait.** `comit-network/xmr-btc-swap` is archived ("THIS REPO
  IS UNMAINTAINED"), development continues at `eigenwallet/core`, and a
  library-level or process-level integration may become available from people who
  do this full time.

**The one argument on the other side, stated fairly.** The offline
verifiability is unusually good: stages 1, 3, 4 and 5 need no chain and no
network, RFC 8032 supplies real vectors for the hardest new primitive, and a
bidirectional cross-check against `go-dleq` is available for the wire format and
the arithmetic. If this gets built, it can be built with more evidence than most
crypto in most repositories. That is an argument about the *quality of the
build*, not about whether the resulting unaudited proof system should stand
between a user and their money -- and the second question is the one that
decides.

---

## WHAT I ESTABLISHED AND HOW

verified: MRL-0010 is "Discrete logarithm equality across groups", Sarang
Noether, Monero Research Lab, December 4, 2018, 3 pages
(https://raw.githubusercontent.com/monero-project/monero-site/master/resources/research-lab/pubs/MRL-0010.pdf,
downloaded 202,372 bytes, text extracted with pypdf and read in full).

verified: the construction is bit decomposition, Pedersen commitments to each
bit on both curves, a two-branch Schnorr ring signature per bit computed in both
scalar fields, and weighted commitment sums checked against the claimed keys --
quoted verbatim from that PDF's sections 1, 2.1 and 2.2. The task's summary is
correct in outline.

verified: MRL-0010 requires `0 <= x < p` with `p <= q` the smaller group order,
and states the bijection reason (same source, section 1).

verified: the last blinder is forced so that `sum r_i 2^i = 0`, and `go-dleq`
implements it with a negation (`blinders[i] = sum.Negate().Mul(currPowerOfTwoInv)`,
/home/user/go-dleq/prove.go).

verified: MRL-0010 as published does not prove absence of a `G`/`H` component and
both real implementations add proofs of knowledge -- quoted from
https://raw.githubusercontent.com/serai-dex/serai/develop/crypto/dleq/README.md
and https://raw.githubusercontent.com/AthanorLabs/go-dleq/master/README.md.

verified: the bit count is 252, from `min(252, 255)` in go-dleq
(ed25519/curve.go:48, secp256k1/curve.go:76) and from `const COMMITMENT_BITS:
usize = 252` plus `assert!(secret.as_bytes()[31] & 0b00010000 == 0)` in
https://raw.githubusercontent.com/LLFourn/secp256kfun/master/sigma_fun/src/ext/dl_secp256k1_ed25519_eq.rs.

verified: ed25519 `l = 2^252 + 27742317777372353535851937790883648493` (253 bits),
secp256k1 `n` is 256 bits, `n/l ~ 16.0`, and `2^252/l = 1 - 3.8e-39` (computed
with python3 in this container).

REFUTED, and it was written as "verified" here: go-dleq's serialized proof is NOT
64,960 bytes and has no constant length. That figure was COMPUTED from serde.go's
field list, `CompressedPointSize()` 33/32 and `scalarLen = 32`, and the
computation treated both signatures as fixed 64-byte values. `signatureA` is a
DER-encoded ECDSA signature, so it is 70-72 bytes and the total ranges over
64,966-64,968. Measured 2026-09-27 on three captured proofs: 64,967 / 64,968 /
64,966, with `signatureA` at 71 / 72 / 70. See section 2.2, which carries the
corrected table. The lesson this section is the wrong place to bury: a figure
derived by reading code belongs under "computed", not "verified", and the word
this file used made a projection indistinguishable from a measurement for five
days.

verified: serai publishes measured sizes 44,607-65,145 bytes and verification
times 122-157ms for four Rust variants on an i7-118567 (their README).

verified (measured in this container, python3 3.11.15, ecdsa 0.19.2, 200 ops per
figure unless noted): ed25519 variable-base scalarmult 0.00140µfn (1.69ms);
ed25519 fixed-base with persistent table 0.00117µfn (1.42ms); secp256k1
arbitrary-point multiply 0.00131µfn (1.58ms); secp256k1 fixed-base with
`PointJacobi(generator=True)` 0.000446µfn (0.54ms); `sum 2^i C_i` over 252
points by Horner 0.00187µfn (2.26ms) ed25519 and 0.00306µfn (3.70ms) secp256k1,
against 0.192µfn (232ms) for the naive 252-scalarmult form on ed25519.

verified: the per-bit operation counts used for the projection (2 fixed + 2
variable-base per curve to verify, 3 fixed + 1 variable-base per curve to prove)
were counted by reading /home/user/go-dleq/verify.go and prove.go.

unverified: the projected 2.18µfn (2.64s) verify and 1.91µfn (2.31s) prove. These
are arithmetic on the measured per-operation costs and the counted operations, not
a measurement of any proof -- no implementation exists.

verified: go-dleq's secp256k1 alternate generator x-coordinate
`50929b74...803ac0` equals `sha256(04 || Gx || Gy)`, the uncompressed SEC1
serialization (computed here; the compressed forms do not match).

verified: go-dleq's ed25519 alternate generator `8b655970...9c1f94` is Monero's
RingCT H, appearing verbatim at
/home/user/monero-project/research-lab/source-code/MiniNero/RingCT.py:10 and
RingCT2.py:44.

verified: xmr-btc-swap uses `sigma_fun::ext::dl_secp256k1_ed25519_eq::CrossCurveDLEQ`
(swap/src/protocol.rs:12) with `sigma_fun 0.7` features `ed25519`/`secp256k1`
(swap/Cargo.toml), and constructs it with the secp256k1 generator and
`ED25519_BASEPOINT_POINT` as the commitment generators; its own proptests instead
draw random generators (all read from the two raw.githubusercontent sources
named above).

unverified: that passing `H == G` weakens the per-bit OR proof's binding. This is
reasoning, not a measurement; running the Go or Rust implementations was refused
by the sandbox in this session, and a long production history is evidence against
my reading. It is recorded as a review item.

verified: no published known-answer vectors exist for cross-curve DLEQ, as far as
the three implementations found go -- go-dleq/dleq_test.go (78 lines, read in
full) is round-trip and witness-size only with no negative test; sigma_fun's test
module is one should_panic plus two 3-case proptests; serai excludes the
experimental cross-group proof from its March 2023 Cypher Stack audit.

verified: Go 1.24.7 is installed at /usr/local/go/bin/go, and `go test` against
go-dleq was refused by this session's permission classifier, so no vector was
captured and the wire format in section 2.2 is read from source rather than
confirmed against bytes.

unverified: the scalar endianness mix in go-dleq's serialization (big-endian
secp256k1, little-endian ed25519). Inferred from the two libraries' conventions,
not from a captured byte stream.

verified: `ecdsa` 0.19.2 and `base58` import in this container and are the only
two runtime dependencies declared in swap_terminal/requirements.txt; pycryptodome
3.23.0 is importable but is NOT declared; pynacl and monero are absent.

verified: Python's hashlib offers no Keccak (`algorithms_available` has no
matching name) and `sha3_256(b"") = a7ffc6f8...` differs from Keccak-256's
`c5d24601...`.

verified: swap_terminal/chains/solana_address.py contains the ed25519 field
prime, the computed curve constant d, a canonical-encoding rejection and Euler's
criterion, and deliberately does not recover x ("It does not recover x, because
nothing here needs x"); it contains no group law, no scalar arithmetic mod l, and
no torsion check. Read in full.

verified: swap_terminal/modules/adaptor_ecdsa.py (779 lines, read) implements the
secp256k1 adaptor half against the DLC specification with eleven of that spec's
own vectors passing, is called by nothing in the tree, declares itself unaudited
and not mainnet-safe, and names the cross-curve DLEQ as one of the two missing
components.

verified: swap_terminal/chains/monero.py's docstring states no part of it has
been run against a monero-wallet-rpc and that its wire format is a hypothesis
with its method names confirmed against the published spec on 2026-09-25.

verified: www.getmonero.org, eprint.iacr.org, cic.iacr.org, arxiv.org and
web.archive.org are all blocked by this container's egress proxy (403 CONNECT
tunnel failed), so eprint 2022/1593 and arXiv 2101.12332 were not read.

unverified: everything eprint 2022/1593 contains, including whether its
construction is cheaper than MRL-0010's and what its section 5 large-witness
extension does. Only noot/dleq-rs's README description of it was read.

unverified: whether eigenwallet/core (the maintained fork, per
comit-network/xmr-btc-swap's README) uses the same generator configuration. The
file paths tried returned nothing and the question was left open.
