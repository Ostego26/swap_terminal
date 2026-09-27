# Cross-curve DLEQ test vectors

`tests/vectors/dleq_cross_curve_go.json` holds three known-answer vectors for a
secp256k1 <-> ed25519 discrete-logarithm-equality proof, generated on 2026-09-27
by `main.go` in this directory against the Go reference implementation:

    github.com/athanorlabs/go-dleq  at d6fd7c03e213683cbb286d4d5bbba1c7e7e1cfe1
    (2023-01-13, "add ed25519.NewPoint")

## Why these exist

`docs/dleq_cross_curve_design.md` names the absence of published vectors as the
deciding argument AGAINST building a Python port: every technique that made
`chains/xrp_crypto_condition.py` and `modules/adaptor_ecdsa.py` trustworthy rests
on byte-identical agreement with an independent implementation's published
vectors, and for cross-curve DLEQ no such vectors are published in any language.
All three implementations' test files are round-trip only, and serai's 2023
Cypher Stack audit explicitly excludes the cross-group proof.

These are not published vectors -- they are CAPTURED ones, which is weaker in one
way and stronger in another. Weaker: nobody has reviewed them, and they inherit
whatever go-dleq gets wrong. Stronger: the oracle is BIDIRECTIONAL, because
go-dleq exposes `Serialize`/`Deserialize`, so a Python implementation can be
checked in both directions -- can it verify a Go proof, and can Go verify a Python
proof. The `solders` and `cryptoconditions` cross-checks this repo already relies
on only went one way.

## What is independently confirmed, and what is not

CONFIRMED, without trusting go-dleq: the `one` vector's witness is the scalar 1,
so its two committed points must be the curves' own generators, and they are --
`0279be667ef9dcbbac55a06295ce870b07029bfcdb2dce28d959f2815b16f81798` is the
compressed secp256k1 G and `5866...66` is the compressed ed25519 basepoint. That
is a check against published curve constants, not against the library that
produced the file, and it establishes the vectors are correctly oriented rather
than transposed.

CONFIRMED: `go test ./...` passes on that commit, and all three proofs report
`verifies: true` from the library's own verifier.

NOT CONFIRMED: that go-dleq's construction is sound. Its README says MRL-0010 as
published "isn't computationally correct", and go-dleq adds proofs the note
omits; whether its additions are correct is unaudited. A Python port agreeing
with this file byte for byte proves the port is a faithful reimplementation, NOT
that either one is secure.

NOT CONFIRMED: the scalar endianness in the serialized form, read from serde.go
rather than derived from these bytes.

## Regenerating

    git clone https://github.com/athanorlabs/go-dleq
    cp main.go go-dleq/cmd/vectors/main.go
    cd go-dleq && go run ./cmd/vectors > dleq_cross_curve_go.json

The witnesses are deterministic -- 1, the bytes 1..31, and one just under 2^252 --
so the committed points and proof lengths are reproducible. The PROOF BYTES are
not: go-dleq samples blinders randomly, so a regenerated file differs from this
one in the proof field while agreeing on every point. Compare points, and verify
proofs; do not diff the proof hex.

Each proof is ~64,967 bytes, and the three differ: 64,967, 64,968 and 64,966.
That REFUTES the design document's projection of 64,960 rather than confirming it
to within eight bytes, which is what this line used to say. The projection treated
both signatures as fixed 64-byte values; `signatureA` is DER-encoded ECDSA, so it
is 70-72 bytes and the total is not a constant at all. Three distinct lengths from
three proofs is the evidence -- a format with two fixed-width signatures cannot
produce them. `swap_terminal/modules/dleq_proof_format.py` is the parser that
establishes the layout byte for byte, and a verifier written to 64,960 would have
rejected all three of these files.
