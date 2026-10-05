# ICP canisters

Rust, not Motoko, and the reason is specific rather than taste: `ic-cdk-timers`
is Rust-only, HTLC construction means building P2SH scripts and signing sighashes
(the `bitcoin` crate does that; hand-rolling script serialization inside a
canister is the last place anyone wants to debug a sighash), and DFINITY's
threshold-ECDSA and Bitcoin-integration examples are Rust-first.

## What is here

`threshold_custody` -- increment 1. Holds one threshold key and reports its
PUBLIC half. It cannot sign: `sign_with_ecdsa` appears nowhere in the source and
nowhere in the built wasm, which `tests/test_icp_canister_cannot_sign.py` pins.

Build and inspect without dfx:

    cd icp/threshold_custody
    cargo build --release --target wasm32-unknown-unknown
    cargo clippy --release --target wasm32-unknown-unknown -- -D warnings

Deploying needs `dfx`, which this repository does not vendor.

## What the research established, so nobody re-litigates it

Measured against `dfinity/portal` at commit ed742236 (2026-08-24), because the
rendered docs site was unreachable from the session that did the reading:

  custody        WORKS for all five chains. Threshold ECDSA (secp256k1) covers
                 BTC, LTC and GRC; threshold Schnorr (Ed25519) covers SOL and
                 XRP. No private key exists in reconstructed form anywhere.
  chain state    BITCOIN ONLY is consensus-backed. Dogecoin is the one other
                 direct integration and is beta. Solana is the NNS-governed SOL
                 RPC canister -- an HTTPS-outcall wrapper over third-party
                 providers, with `getLatestBlockhash` unsupported, which is an
                 unsolved design question for signing Solana transactions.
                 XRP and LTC are raw outcalls you write yourself.
  Gridcoin       NOT REACHABLE, and this is the finding that shapes the whole
                 track. GRC stopped accepting proof-of-work blocks at height
                 2050 (src/consensus/consensus.h:6, LAST_POW_BLOCK); the chain is
                 above 4.1 million. ICP's Bitcoin adapter validates PROOF OF
                 WORK into replicated state, and there is none here to validate.
                 A PoS chain needs the stake set, which needs the chain state,
                 which is the circle a light client exists to break.

The blunt consequence: an atomic swap is only as trustless as its weakest leg,
and four of the five legs would be RPC-provider-trust legs. The custody story is
a genuine win over Python holding a seed. The trustlessness story is Bitcoin and
nothing else today. Do not ship it described otherwise.
