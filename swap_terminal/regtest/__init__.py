"""Regtest verification harness for the HTLC atomic-swap path.

Role: module (the stages the root entry point `regtest_htlc_verify.py` runs)
Reads: local bitcoind / litecoind daemons over JSON-RPC, and the environment
       variables named in regtest.config
Writes: nothing to this repository. THE REGTEST CHAINS AND WALLETS: it starts
       daemons, creates wallets, mines blocks, and broadcasts transactions.
Can move funds: YES on a regtest chain, and only there. Every module in this
       package refuses to act against a daemon whose `getblockchaininfo`
       reports anything but `regtest` -- see regtest.daemons.assert_regtest,
       which is the first call made after every connection.
Mainnet-safe: NO, and it is structurally prevented from reaching mainnet. The
       refusal above is unconditional and has no flag to disable it. It is
       also incapable of building a mainnet contract: the script builder this
       package drives (modules/atomic_htlc_scripts.py) hardcodes testnet
       version bytes.

WHY A PACKAGE AND NOT ONE FILE (CLAUDE.md rule 10).

The entry point is `regtest_htlc_verify.py` at the repository root, where an
operator can find it by looking. It runs the stages in `regtest.steps`, which
run the decisions in `regtest.txbuild` and `regtest.keys`. The decisions --
"what scriptSig satisfies this branch", "what sighash does this input commit
to" -- are leaf functions that can be called with seeded inputs, which is the
only reason any of this can be reviewed without a chain.

WHAT THIS PACKAGE IS NOT.

It is not a reimplementation of the swap path. Steps 5 through 7 call the real
`modules.atomic_htlc_scripts.build_htlc_redeem_script`, the real
`modules.htlc_timelock.contract_locktime`, and the real
`BTCClient.create_contract` / `redeem_contract` and
`LTCClient.create_contract` / `redeem_contract`. Where this package builds a
transaction itself -- `regtest.txbuild` -- it is because NO refund
implementation exists anywhere in the tree to drive (grepped 2026-09-24:
`def .*refund` matches nothing in any client), so the refund branch can only
be exercised by a spender written for the purpose. That spender is the
harness's control, not the code under test, and every place it is used says
so on the screen.
"""
