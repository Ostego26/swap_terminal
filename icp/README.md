# ICP canisters

Rust, not Motoko, and the reason is specific rather than taste: `ic-cdk-timers`
is Rust-only, HTLC construction means building P2SH scripts and signing sighashes
(the `bitcoin` crate does that; hand-rolling script serialization inside a
canister is the last place anyone wants to debug a sighash), and DFINITY's
threshold-ECDSA and Bitcoin-integration examples are Rust-first.

## What is here

`threshold_custody` -- increment 1. Holds one threshold key and reports its
PUBLIC half. It cannot sign: `sign_with_ecdsa` appears nowhere in the source, and
`tests/test_icp_canister_cannot_sign.py` pins that.

THE WASM HALF OF THAT SENTENCE IS NOT PINNED BY A NORMAL RUN, and this paragraph
claimed it was until 2026-10-11. The test that inspects the built artifact,
`test_the_built_wasm_exports_no_signing_call`, is `@pytest.mark.skipif`'d on
`ST_CHECK_WASM` not being set, because a release build costs ~34s against a suite
of 5,000-odd tests. So `python3 -m pytest` reports 1 skipped there and the only
check that actually ran read the SOURCE -- which CLAUDE.md is explicit about:
"Never accept 'the code contains a check for X' as evidence X is enforced."

To get the real one:

    ST_CHECK_WASM=1 python3 -m pytest tests/test_icp_canister_cannot_sign.py

It builds the wasm, asserts `ecdsa_public_key` and `schnorr_public_key` ARE in the
bytes, asserts none of the forbidden calls is, and asserts exactly one
`canister_update` and one `canister_query` export. Run it before trusting a
deployment, because it is the one check that would catch a signing call arriving
through a macro, a re-export or a dependency rather than through a line of lib.rs.

`operator_admin` -- the configuration and audit canister: posture, fee bps, pairs,
per-asset confirmations, armed flags, an audit log and an operator console served
from its own `http_request` query method. It holds no key material. Its inability
to sign was UNPINNED until 2026-10-11 -- four files in `tests/` mentioned it and
none checked -- and it is now covered by the same table as `threshold_custody`,
with a per-canister forbidden list, because `http_request` is a legitimate method
here and an outcall there.

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

## Key names, checked against primary sources 2026-10-11

`threshold_custody_init.did` ships `(record { key_name = "dfx_test_key" })`, and
`src/lib.rs` uses that one configured name for `EcdsaKeyId.name` AND
`SchnorrKeyId.name`. A review agent reported this as a defect -- that no Ed25519
`dfx_test_key` exists, so the SOL and XRP legs of the keyring could not be exercised
locally. **That is wrong, and the key name is correct for all three curves.**

DFINITY's threshold-Schnorr page lists the supported key ids for the feature as:

    dfx_test_key   only available on the local development environment started by dfx
    test_key_1     test key available on the ICP mainnet
    key_1          production key available on the ICP mainnet

and separately gives an Ed25519 `dfx_test_key` public key. Fetched from
`raw.githubusercontent.com/dfinity/portal/master/docs/building-apps/network-features/signatures/t-schnorr.mdx`,
HTTP 200, 12691 bytes.

**The same page settles the apparent contradiction with the ephemeral-key measurement
below.** It publishes FIXED public keys for `dfx_test_key` on both curves, which reads
as a deterministic key -- while this project measured the local key CHANGING across one
ordinary container restart. Both hold: the published values are labeled the **PocketIC**
test keys, and PocketIC is a different harness from `dfx start`. A deterministic
published value exists and does not describe the replica this project runs, so a custody
address derived against `icp-replica` is still valid only for that container's lifetime.

**Method note, because it cost a round.** Plain `curl` through this environment's proxy
reaches `raw.githubusercontent.com` with HTTP 200 and returns whole files; a summarizing
fetch tool returns a paraphrase and will not quote a table. For a question that turns on
the exact contents of a list, fetch the file.

## The local ICP ledger, and what running it settled (2026-10-06)

`dfx deploy icp_ledger_canister` puts the REAL released ICP ledger
(`ledger-suite-icp-2025-08-29`, the code mainnet runs) on the local replica. Not
a hand-rolled ICRC-1: an adapter tested against a ledger written by whoever wrote
the adapter proves the two agree, not that the adapter is right.

ICP HAS NO TESTNET. DFINITY's own documentation says so verbatim -- "Since the
execution of canisters on ICP is fairly cheap and canisters can be upgraded once
deployed, there is no testnet for ICP" -- and the two "testnet-like" options it
offers (the playground, ICP Ninja) are both ON MAINNET, with canisters deleted
after 20 minutes. So this replica is the testnet in the only sense ICP has one,
which is also what the chain's maintainers recommend.

What the first deployed ledger answered:

    icrc1_symbol        "LICP"              not ICP -- a local ledger is its own
                                            token on its own network
    icrc1_decimals      8                   so chains/coin_amounts.CHAIN_DECIMALS
                                            answers for ICP; no second table
    icrc1_fee           10_000 e8s          matches DFINITY's documented example.
                                            Still not a constant in the codebase:
                                            an adapter reads icrc1_fee()
    icrc1_total_supply  100_000_000_000     the 1000 LICP the init file granted

And the measurement the deposit design depends on. The ledger was funded by a
64-hex ACCOUNT IDENTIFIER that chains/icp_account.py derived, and then queried by
PRINCIPAL with no subaccount:

    icrc1_balance_of(record { owner = principal "ybr6p-...-cqe" })
      -> 100_000_000_000 : nat

Same account, both ways. A per-swap ICP deposit address is a subaccount of the
desk's principal, published to the customer in 64-hex form and watched through
icrc1_balance_of -- if those two encodings disagreed, the terminal would publish
an address, the customer would pay it, and the watcher would poll an account that
stays at zero forever with no error anywhere.

### Replica state and identities live in different places, on purpose

`DFX_CONFIG_ROOT=/state` is a named volume; `./icp:/repo` is your working tree.
That split is not tidiness, and it was tested by accident: recreating the
container to fix the mount discarded the `.dfx` holding replica state, so the
replica handed out first-canister ids again and `threshold_custody` had to be
redeployed. The IDENTITIES survived, because they are under /state -- `dfx ledger
account-id` still returned the account the init file had funded, which is the
only reason that deploy was valid rather than 1000 LICP credited to an account
nobody controls.

Identities are key material, which is also why they stay OUT of the repository
tree: `minter` is stored with `--storage-mode plaintext` because it is a
throwaway on a private network, and a read-write project mount with identities
inside it would put a key under a path somebody could `git add -f`.

### What is NOT built yet

There is no `ICPAdapter`. Swapping ICP needs one, plus `_REQUIRED_SETTINGS`,
pricing, the deposit watcher, the payout path, and a subaccount allocation table
with a uniqueness constraint in SQL (the same shape as `xrp_destination_tags` --
nothing in `icp_account.py` allocates, deliberately). Measured cost of an asset
in this tree: 24 non-test files spell `"XRP"`, `config.py` sixteen times.
