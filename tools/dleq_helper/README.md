# The cross-curve DLEQ helper

A small Go binary that proves and verifies secp256k1 <-> ed25519
discrete-logarithm-equality using `github.com/athanorlabs/go-dleq`, spoken to
over stdin and stdout. `swap_terminal/modules/dleq_helper.py` is the Python side.

## Build it

    cd tools/dleq_helper
    go build -o dleq_helper .

That is the whole deployment cost, and it is the cost
`docs/dleq_cross_curve_design.md` section 7 named when it ranked this approach
first: a Go toolchain, plus a serialization boundary. The binary is not committed
(`.gitignore` excludes it) because a checked-in binary is a thing nobody can
review and everybody trusts.

`go.sum` IS committed, so the module versions are pinned and a build either
reproduces them or fails. The pinned implementation is

    github.com/athanorlabs/go-dleq v0.0.0-20230113214619-d6fd7c03e213

which is the same commit that produced `tests/vectors/dleq_cross_curve_go.json`,
and `tests/test_dleq_helper.py` asserts the running binary reports it.

## Why a helper process and not Python

Because `docs/dleq_cross_curve_design.md` section 7 argued against a Python port
and ranked this option first, in its own words:

> Call out to a maintained implementation as a separate process. This is the
> strongest anti-build option and it is not the thing the repo has already
> refused. `solana_address.py` and `adaptor_ecdsa.py` refuse a **compiled Python
> extension in the wallet-holding process**; a small `go-dleq` helper binary,
> invoked over stdin/stdout with no wallet credentials in its environment, is a
> different trade entirely -- no in-process ABI, no C in the interpreter that
> holds `GRC_RPC_PASS`, and the crypto lives in code other people are running.

**What that buys and what it does not.** It buys shared exposure and other
people's bug reports. It does not buy a proof: `go-dleq` is unaudited, ships no
soundness test, and its own README says MRL-0010 as published "isn't
computationally correct" -- so it adds proofs the note omits, and whether those
additions are correct has never been reviewed. Section 7's other five arguments,
including the one against offering GRC<->XMR trustlessly at all before somebody
commissions a review, are untouched by this directory and still stand.

## Why the witness goes over stdin

`argv` is world-readable through `/proc` and `ps`. The witness on the `prove`
path is, in a GRC<->XMR swap, one share of a Monero spend key; passed as a
command-line argument it is readable by every user on the host for the duration
of the call. stdin is the only channel to a child here that is not exposed that
way, and that is the entire reason this is a line protocol rather than one
invocation per proof -- process startup is negligible next to ~150ms of curve
arithmetic, so performance was never the argument.

The matching rule on the Go side: **no code path formats the witness into an
error or writes it to stderr.** `errWitnessLength` is a constant rather than a
`%q` of the input for exactly that reason, and the hex-decode failure on the
prove branch deliberately does not wrap `hex.InvalidByteError`, which would
include a byte of the witness.

## Protocol

One JSON object per line in, one per line out, in order. Every response carries
`ok`; a false `ok` carries `error`. An unparseable line gets an error response
and the process stays up -- a helper that exits on one bad line turns a caller's
encoding bug into a lost swap leg.

    {"op":"version"}
      -> {"ok":true,"go_dleq":"v0.0.0-...","bit_count":252}

    {"op":"prove","witness":"<64 hex chars, 32 bytes little-endian, < 2^252>"}
      -> {"ok":true,"proof":"<hex>","secp256k1_point":"<hex33>","ed25519_point":"<hex32>"}

    {"op":"verify","proof":"<hex>"}
      -> {"ok":true,"secp256k1_point":"<hex33>","ed25519_point":"<hex32>"}
      -> {"ok":false,"error":"verify: ..."}

The two points come back on `verify` from the DESERIALIZED proof, and they are
not decoration. **A DLEQ proof says the two points it commits to share a discrete
log; it says nothing about WHICH points.** Verifying a proof and then treating it
as evidence about a key you hold is sound only if you checked that the proof is
about that key, so the Python side's `verify()` REQUIRES the expected keys as
keyword arguments and returns `verified=False` when they differ, while
`verify_any()` is the separate, deliberately awkward name for the case where the
caller does not know them yet.

A verification failure is a normal `ok:false` response, not a protocol error: a
counterparty sending a bad proof is what this exists to detect.

## Two things worth knowing about the boundary

**`Deserialize` ignores trailing bytes.** It stops at the last field it needs, so
a proof with a byte appended round-trips through this binary as valid. The Python
side therefore frames every proof with `modules/dleq_proof_format.parse_proof`
BEFORE handing the bytes over, and raises `DleqFormatError` -- a different
outcome from a false verdict, so a caller can tell an encoding bug from a
cheating counterparty.

**A proof is ~64,967 bytes, and the length is not a constant.** `signatureA` is
DER-encoded ECDSA, so the total ranges over 64,966-64,968. The scanner's line
limit is 4 MiB for that reason, since `bufio.Scanner`'s default 64 KiB would
truncate every single verify request.
