# Every branch this system can take, and what actually establishes it

    Role: inventory (what is proven, by what, and to what strength)
    Reads: nothing at runtime
    Writes: nothing
    Can move funds: no
    Mainnet-safe: yes -- prose
    Live-safe: yes

Operator, 2026-09-28: *"well are we going to systematically test all the branches we've got
working now?"*

Yes, and this is the list. It exists because the answer was not knowable from anywhere: the
evidence was spread across three run records, six harnesses and a dozen module headers, and
"which branches are proven" had to be reassembled by hand every time it was asked.

## How to read the Evidence column, because the grades are not interchangeable

    SPENT     a transaction taking this branch was accepted by a real chain. A txid is named.
              This is the only grade that settles anything (Verify by behavior, never by
              reading the code).
    REFUSED   a transaction taking this branch was REFUSED by a real chain, deliberately. That
              proves a gate HOLDS and says nothing about the branch working -- Tx_punish had
              this grade for its whole life until 2026-09-28, and four of five transactions had
              moved a coin while it had moved none.
    OFFLINE   a test in tests/ pins it with no chain involved. Catches encoding and logic and
              cannot catch a consensus rule.
    READ      somebody read the source of the chain daemon. Labeled as a reading everywhere it
              appears and never written in the same voice as a measurement (rule 17).
    NONE      nothing establishes it.

**A row's harness is what re-establishes it.** Every SPENT row names the command that produced
it, so the row is a thing that can be re-run rather than a claim that has to be trusted. Rows
with no harness are the work.

---

## 1. The ADAPTOR swap chain -- `adaptor_regtest_verify.py --chain grc`

The five transactions of `docs/monero_swap_protocol.md` section 2, on Gridcoin TESTNET
v5.5.1.0. Full record: `docs/gridcoin_adaptor_join_2026_09_28.md`.

| Branch | Evidence | txid / note |
|---|---|---|
| Tx_lock funds a 2-of-2 P2SH | SPENT | `7451773d99518f37…`, `04eb71d817fbaa37…` |
| Tx_redeem, ADAPTOR under Y_a | SPENT | `ccacc0614e34af7a…` and the scalar recovered from it |
| Tx_redeem, signatures TRANSPOSED | REFUSED | the footgun, and it must stay refused |
| Tx_redeem, OP_0 dummy MISSING | REFUSED | the 2010 off-by-one |
| Tx_cancel before T1 | REFUSED | relay only -- see the consensus row |
| Tx_cancel at T1 | SPENT | `843b5fa64e1b91aa…`, SAME BYTES as the refusal |
| Tx_refund, ADAPTOR under Y_b | SPENT | `8fd143103ed91b36…` and the scalar recovered |
| Tx_punish before T2 | REFUSED | the control |
| Tx_punish at T2 | SPENT | `db8c2e9456456fb6…` on its own lock C, 2026-09-28. The SAME 308 bytes refused at height 3296126 and accepted at 3296131, and its scriptSig publishes nothing |
| the cancel publishes nothing | SPENT | asserted over 218 published bytes |
| nLockTime is CONSENSUS, not relay | READ | `src/validation.cpp:1777`. Gridcoin has no `generateblock`, so this cannot be measured from here |

## 2. The HTLC swap -- `regtest_htlc_verify.py`

| Branch | Evidence | note |
|---|---|---|
| hashlock spend, BTC regtest | SPENT | `--chain btc` |
| hashlock spend, LTC regtest | SPENT | `--chain ltc` |
| refund after expiry, BTC/LTC | SPENT | `step_9_refund_after_expiry`, real `refund_contract()` |
| hashlock spend, GRC | SPENT | three live swaps, `docs/atomic_swap_runs_2026_09_27.md` |
| **refund after expiry, GRC** | **NONE** | `--chain` offers `btc`, `ltc`, `both`. **Gridcoin is not an option and never has been.** See §5 |
| CLTV executes on GRC | **SPENT 2026-09-28** | `9495082ef304c4ca…` spends the P2SH `8468aa40f9:0` with nLockTime 3296363 and sequence 0xfffffffe, paying 0.13 GRC to the wallet. The REFUSAL side (steps 5 and 6) is still untested |
| ~~CLTV executes on GRC~~ (the old grade) | ~~NONE~~ | the three GRC swaps took the HASHLOCK branch; `OP_CHECKLOCKTIMEVERIFY` sits in the `OP_ELSE` and never ran. What is established is that Gridcoin ACCEPTS a script CONTAINING it |

## 3. XRP escrow -- `xrp_htlc_escrow.py --run`

| Branch | Evidence | note |
|---|---|---|
| EscrowCreate [A] | SPENT | XRP testnet, `1216DF875A438B07…` (2026-09-28) |
| EscrowFinish, WRONG fulfillment | REFUSED | `tecCRYPTOCONDITION_ERROR` -- the hashlock holding |
| EscrowFinish, RIGHT fulfillment | SPENT | `DDC13937449FE365…`, destination +1000000 drops exactly |
| EscrowCreate [B], for the refund path | SPENT | `BB23A43883B9E6B5…`, a dedicated escrow so an early cancel cannot destroy the one step 6 needs |
| EscrowCancel before CancelAfter | REFUSED | `tecNO_PERMISSION` |
| EscrowCancel after CancelAfter | SPENT | `5D4EC8C88DCB868B…`, sender's balance restored |

Both branches, twice over (2026-09-26 and 2026-09-28, independent runs). This is the most
completely exercised leg in the tree.

**And it is still not a swap, which the harness says itself**: nothing wires escrow into the
deposit path, and the XRP leg's `CancelAfter` is not derived from
`modules/htlc_timelock.lock_hours_for_role()` -- so the two legs' timelocks are not related by
the rule that makes a swap safe. Branches proven, protocol not assembled.

Incidental, and worth keeping: the public testnet server answered `notSupported` to
server-side signing, and the harness switched to LOCAL signing with xrpl-py for that and every
later transaction. That is a fact about the server, not a failure, and the seed never left the
machine.

## 4. Monero

| Branch | Evidence | note |
|---|---|---|
| shared address derivation | SPENT | checked against `monero-wallet-rpc generate_from_keys`, byte-identical |
| sweep with `s_a + s_b` | SPENT | Monero REGTEST, `bb85f7fa10759077…` (2026-09-28) and `f584606948f430bf…` (2026-09-27), 738,723,841,921,372 atomic units each. Two runs, two independent share sets, the same answer -- `python3 monero_shared_key_verify.py --run --allow-open-wallet --mine 80` then `--sweep <address>` |
| the same on STAGENET | **NONE** | `docs/monero_stagenet_funding.md` says in its own words the regtest result "does NOT settle anything about stagenet specifically" |
| a funded GRC↔XMR swap, end to end | **NONE** | the script half and the Monero half have never been run as one. The cross-curve close is a KEY MATCH against an address, not a spend of coins at it |

---

## 5. THE GAPS, IN THE ORDER THEY COST SOMETHING

**RUN 2026-09-28, and these rows moved:** commands 0, 3 and 4 of the operator's list all
passed -- the offline suite (2154), the BTC and LTC HTLC with both branches through the real
client (`regtest_htlc_verify.py --chain both --wipe`, OK=76 FAIL=0, CLTV refused by CONSENSUS on
both chains via `generateblock`), the Monero shared-key sweep, and the XRP escrow (OK=11 FAIL=0,
both branches on the live testnet). and the GRC adaptor chain with the punish branch (OK=63 FAIL=0). **All five of the
operator's commands have now passed.**

**a. ~~Tx_punish at T2 has never spent.~~ DONE 2026-09-28**, `db8c2e9456456fb6…`. All five
transactions the protocol specifies have now moved a coin on Gridcoin. Kept struck through
rather than deleted, because what it says about the other four gaps is that they are the same
kind of work and equally closable.

**b. The HTLC refund branch has never run on Gridcoin, and `--chain` cannot ask for it.** Three
GRC swaps completed, all through the hashlock. The refund branch of a GRC HTLC -- the one an
operator needs when a counterparty vanishes -- has never been executed, and the harness that
would do it does not offer the chain. `OP_CHECKLOCKTIMEVERIFY` has never executed on Gridcoin
from this tree at all.

**2026-09-28, LATE: THE REFUND BRANCH SPENT.** `9495082ef304c4cab39d83dcabd28512c2800f8f60acaf6ed1e00ca01d79163b`,
51 confirmations, spends `8468aa40f94e6c81:0` -- a P2SH output -- carrying nLockTime 3296363
and nSequence 0xfffffffe, and pays 0.13 GRC to `mjhEmpAUU5p7NU2PZTyV4ayTr8U15fczPt`. That
address's hash160 is `2dd26b1d0fdceeeca05b499bbde58519a7684e8f`, which is the one this harness's
`--recover` chose, so the transaction is ours rather than a coincidence on the same outpoint.

WHAT THIS ESTABLISHES, exactly and no more: a coin came OUT of a Gridcoin HTLC through the
timelock branch. The script ran, `OP_CHECKLOCKTIMEVERIFY` did not refuse it at a height past the
locktime, and the two fields it needs -- a non-final sequence and an nLockTime at least the
script's -- were carried correctly by `htlc_spend.with_locktime`.

WHAT IT DOES NOT ESTABLISH, and the distinction is the whole reason grc_htlc_verify.py has a
step 5 and a step 6: that CLTV REFUSES an early refund. An opcode that never refuses anything
is indistinguishable from a no-op, and a no-op here means every HTLC this repository funds on
Gridcoin can be refunded by its funder at any time. Steps 5 and 6 have still never run.

THE RUN WE WATCHED REPORTED `-22 TX rejected` AND THIS TRANSACTION EXISTS, and those two facts
have not been reconciled. The daemon logged two errors in the same second -- `nonstandard
transaction type` and `VerifySignature failed` -- and there is no evidence yet saying which, if
either, was this transaction. It is recorded as unexplained rather than resolved (rule 17): a
success that nobody can account for is not the same as a success.

**2026-09-28: `grc_htlc_verify.py` now EXISTS and is unit-tested and mutation-checked, and IT
HAS NOT YET RUN ON THE CHAIN.** Those are two different facts and this line keeps them apart
deliberately (rule 17): the harness is written, its ten tests pass offline, and a mutant that
made its measurement step build a NON-FINAL refund -- which would have degraded the CLTV proof
into a duplicate of the non-finality control while still printing a pass -- was killed. None of
that is evidence about Gridcoin. This gap closes when the operator runs

    export ST_ADAPTOR_FUNDING_SEED='...'
    python3 grc_htlc_verify.py

and the line `6 CLTV REFUSES A FINAL REFUND BEFORE THE LOCKTIME` reads OK. `--chain grc` is
still not offered by `regtest_htlc_verify.py`, and deliberately so: that harness MINES to the
locktime, and `contract_locktime("GRC", ROLE_INITIATOR, tip)` is tip+1920, which is 48 hours at
Gridcoin's 90s target on a chain with no `generateblock`. The separate file is the answer to
that, not an oversight.

**c-bis. IT HAPPENED AGAIN, ON 2026-09-28, IN THE HARNESS WRITTEN THAT DAY.** Recorded here
rather than only in the commit, because the interesting part is not the loss -- 1.50 GRC
testnet, `e1f8ae8f961d8591:0` -- but that gap (c) was written down, read, and reproduced within
hours by somebody who had just read it. `grc_htlc_verify.py` minted its contract's refund key
with `generate_key()`, funded it, died at step 4 on an unrelated defect, and exited. The key
existed only in that process. Nobody can spend that output now, and nothing about the HTLC
under test was at fault.

Fixed in the harness the same day: the refund key is derived from
`ST_ADAPTOR_FUNDING_SEED` under its own role, so its address is stable and a failed run leaves
its coins where the next run can spend them. The participant key stays random on purpose --
nothing is ever paid to it, and a key that never RECEIVES cannot strand anything. That is the
general rule the two incidents share, and it is cheaper to state than to rediscover: **only
keys that receive have to be recoverable.**

`atomic_swap.py` is NOT fixed by any of that, and the fix there is still live posture and still
the operator's (below).

**c. A CRASH BETWEEN FUNDING AND CLAIMING DESTROYS THE LEG, AND THE FIX IS LIVE POSTURE.**
Already found, already handed over, still open since 2026-09-27. `atomic_swap.py` mints four
keypairs IN PROCESS and never persists them; BOTH branches of each contract name those keys, so
when the process exits neither can ever be signed again. A timelock does not help when nobody
holds the refund key. It has already cost **310.72 GRC testnet** and stranded regtest legs on
BTC and LTC.

The fix named in that record -- point the REFUND branch at a WALLET address the daemon can
always spend, and let the hashlock branch keep its in-process key -- changes what the fund path
builds. That is live posture and the operator's call (rule 16). `docs/atomic_swap_runs_2026_09_27.md`
says it "should be made before this driver touches a chain where the coins matter", and that
sentence is unchanged.

**d. The stagenet Monero sweep.** Regtest and stagenet derive keys identically, so the
cryptographic claim is settled and the network-specific one is not.

**e. A full GRC↔XMR swap.** Everything above is halves.

---

## What this document is NOT

It is hand-maintained, which is the thing CLAUDE.md rule 19 warns about: a list that is correct
the day it is written and wrong by the next commit, and the thing it would be wrong about is
which parts of a money system are proven. Two things hold it down and neither is a ratchet:

  - **every SPENT row names a txid or a harness**, so a claim here is checkable against a chain
    or re-runnable, rather than trusted;
  - **a row with no harness is a gap by construction.** The NONE rows are the work list, and
    the document stops being useful the moment somebody writes SPENT in one without a txid.
