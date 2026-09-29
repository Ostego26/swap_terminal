# A 2-of-2 P2SH funds and SPENDS on Gridcoin, both branches, 2026-09-28

Role: record (what one run established, and what it did not)
Reads: nothing
Writes: nothing
Can move funds: no
Mainnet-safe: yes -- every txid below is Gridcoin TESTNET, port 25715
Live-safe: yes

`adaptor_regtest_verify.py --chain grc`, against the operator's Gridcoin testnet daemon
**v5.5.1.0** at `127.0.0.1:25715`. Final line:

    OK=40  FAIL=0  XFAIL=1  SKIP=2
    exit code 0 -- ESTABLISHED on every chain

This answers the refusal that stood over this work for the whole session:

> Still not established, and I won't let this pass as done: no 2-of-2 P2SH has ever been
> spent on Gridcoin from this repo. A source reading plus a CLTV measurement is not a
> spend. That needs regtest with both branches exercised.

Both branches were exercised. Not on regtest -- Gridcoin has no regtest mode -- but on its
real testnet, against real blocks, which is stricter.

## WHAT THIS RUN DID NOT ESTABLISH, ADDED THE SAME DAY

**Every signature in every transaction below is an ORDINARY ECDSA signature.** No adaptor
signature was involved anywhere: measured by the NAME grep rule 2 asks for -- `adaptor_ecdsa`,
`pre_sign`, `complete_signature`, `recover` across `adaptor_regtest_verify.py` and
`swap_terminal/regtest/adaptor_steps.py` -- zero occurrences at the commit this run was made
from. Nothing here is softened by saying so; a 2-of-2 P2SH funding and spending on Gridcoin is
exactly what it claims and it is what was asked for. But it is NOT a rehearsal of the
adaptor-signature swap that later used it, and 40 OK / 0 FAIL should not be read as one.

The adaptor join landed after this run (`48eaaf7`) and was REMOVED on 2026-09-29 with the
chain it existed for. **This document is a record of a Gridcoin 2-of-2 funding and spend, and
nothing here depends on the protocol that was removed.** The harness it names is gone, so these
numbers cannot be reproduced by rerunning it; the transactions below are on Gridcoin testnet
and are the durable part.

`tests/test_adaptor_join.py::test_an_ordinary_2of2_spend_is_NOT_scored_as_having_published_anything`
rebuilds this exact state offline and asserts the new answer.

## The four decisive outcomes

| # | outcome | result |
|---|---------|--------|
| 1 | funded, located by scriptPubKey match | OK -- `a914fd4d680d7d31ed044d48bbacaaeff293034eec3a87` |
| 2 | spends with both signatures in key order | OK |
| 3 | REFUSED with the signatures transposed | OK |
| 4 | REFUSED without the leading OP_0 dummy | OK |

## Every transaction, in order

| what | txid | note |
|------|------|------|
| operator's funding payment | `cf461a6b85db3cb2d86644fe55b8e5682730fa97d1c479da5236bb4968fae172` | 3.50 GRC from the GUI, **vout 1** |
| split into two lock inputs | `f9605128030b04ae3f130048b373ceef3fe92cf65c716c92d5987b81d3789b90` | signed in-process, predicted == daemon |
| lock A Tx_lock | `40f652f1db9e79d277162bebf421b0a444f36560b13eab548d0902b222436121` | predicted == daemon |
| **lock A redeem -- THE 2-of-2 SPENDS** | `e75f257a8620a39b12e489fa6f4c91d133e7c01fce79e487bbaeacf5365a244d` | predicted == daemon |
| lock B Tx_lock | `4d9b7acbaf24a8024cbcef44cbc09dffd482b2008e3c5abae6f9ce4b81e69791` | predicted == daemon |
| cancel, accepted at T1 | `be0a815c8dfd79ebeb9eead62e2a83a75ec3eb1f57b86061789c239d7def1700` | predicted == daemon |
| **refund -- THE SECOND 2-of-2 SPENDS** | `91e096f5b821ea09b76d393611550da59d2834966e9d0dcdcf846deade1838ce` | predicted == daemon |

Heights: lock A T1=3295991 T2=3295997; lock B T1=3295994 T2=3296000. Tip 3295985 -> 3295997.

## The line that justifies the whole harness

    the transposed scriptSig is 307 bytes and the correct one is 307 -- identical,
    which is why only a chain can tell them apart

Both footguns produce a well-formed scriptSig of the SAME LENGTH as the correct one. No
amount of reading the code separates them; Gridcoin's script interpreter does, and both were
refused with `code=-22 TX rejected`.

## Every predicted txid matched

Seven broadcasts, seven predictions made BEFORE the bytes were sent, seven matches. That is
the property the adaptor protocol needs and the one `sendtoaddress` can never give: the
cancel/refund/punish chain is built against Tx_lock's txid, so a txid that changes at
broadcast invalidates everything signed against it.

The harness also measured, and this is FINDING 2 in `adaptor_swap_chain.py`: signing Tx_cancel
CHANGES its txid (`e4d25e9ab13d26f5..` unsigned -> `42302c7b7f169ea8..` signed), which is why
the setup needs two rounds and not one.

## WHAT THIS DOES NOT ESTABLISH

**The early cancel was refused by RELAY only.** `10b CONSENSUS refuses the early cancel` is a
SKIP, because Gridcoin has no `generateblock` and the harness will not pretend otherwise. A
relay refusal says a node will not pass the transaction on. Only a refusal to MINE it says a
miner could not have included it. On BTC and LTC that stronger claim IS measured
(`generateblock: -25 TestBlockValidity failed: bad-txns-nonfinal`); on Gridcoin it is not.

A SKIP is not a pass. The verdict block says so in its own words rather than letting a green
exit code imply otherwise.

**The wallet's unlock scope is also a SKIP**, deliberately: no Gridcoin RPC reports it, which
was verified against `src/wallet/rpcwallet.cpp` rather than assumed.

## How it was funded, and why that matters

The operator's wallet is unlocked FOR STAKING ONLY and will not create a transaction. Rather
than asking them to relock -- which discards their unlock deadline and stops their node
staking, as `CWallet::ElevateToFull`'s own docstring records -- the harness was measured
against the daemon and found a route that needs the wallet for one thing only:

    OK  GRC signrawtransaction with OUR keys (does it bypass the wallet's unlock?): got=open

`signrawtransaction` picks its keystore on argument presence BEFORE any lock check
(`src/rpc/rawtransaction.cpp:2769-2788`), and this harness already signs its own P2PKH inputs
in-process. So one GUI payment to a seed-derived address funded the entire run, the harness
found that payment itself via `listtransactions`, and the wallet was never asked to create or
sign anything. **The node kept staking throughout and the unlock deadline was never touched.**

The funding payment's vout was **1**, not 0 -- the GUI put its change first. The harness finds
the output by scriptPubKey rather than assuming an index, which is the only reason this run
did not try to spend the operator's change.
