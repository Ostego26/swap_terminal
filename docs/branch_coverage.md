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
| refund after expiry, BTC/LTC | SPENT | `step_9_refund_after_expiry`, real `refund_contract()`. BTC `7cc92abdbad07a99…` and LTC `59ed52a9f4255332…`, both 2026-09-29 |
| CLTV refuses an early refund, BTC | **ENFORCED 2026-09-29** | OK=37 FAIL=0. 8b's MEMPOOL said `mandatory-script-verify-flag-failed (Locktime requirement not satisfied)` at height 677 with BIP65 active from 1; 8c's `generateblock` refused the block. Both layers, and the mempool's own wording already said consensus |
| CLTV refuses an early refund, LTC | **ENFORCED 2026-09-29, BY A DIFFERENT PATH** | OK=38 FAIL=0. 8b's mempool said `non-mandatory-script-verify-flag (Locktime requirement not satisfied)` -- CLTV RAN and refused, but on Litecoin Core v0.21.4 it is not in the flag set the mempool calls mandatory, so that refusal alone is RELAY POLICY. 8c is what establishes consensus here: `generateblock` refused with `TestBlockValidity failed`. See the note below -- 8c is load-bearing on LTC in a way it is not on BTC |
| hashlock spend, GRC | SPENT | three live swaps, `docs/atomic_swap_runs_2026_09_27.md` |
| **refund after expiry, GRC** | **NONE** | `--chain` offers `btc`, `ltc`, `both`. **Gridcoin is not an option and never has been.** See §5 |
| CLTV executes on GRC | **ENFORCED 2026-09-29** | `grc_htlc_verify.py` OK=11 FAIL=0. Step 6 REFUSED a FINAL refund (nLockTime 3296544 = tip, so `IsFinalTx` passed) against a script locktime of 3296548; step 8 ACCEPTED the same outpoint, same script, same fee, with nLockTime 3296548. The daemon logged `ConnectInputs() : 23148431f9 VerifySignature failed` -- that txid IS step 6's, and `validation.cpp:669` is reached only when the script fails under CONSENSUS flags alone. See §5b |
| CLTV allows a refund AT the locktime, GRC | SPENT 2026-09-28 | `9495082ef304c4ca…` spends the P2SH `8468aa40f9:0` with nLockTime 3296363 and sequence 0xfffffffe, paying 0.13 GRC to the wallet. Re-established 2026-09-29 by step 8, `ee9dc216bf055b22…`, through the real `refund_contract()` |
| ~~CLTV executes on GRC~~ (the old grade) | ~~NONE~~ | the three GRC swaps took the HASHLOCK branch; `OP_CHECKLOCKTIMEVERIFY` sits in the `OP_ELSE` and never ran. What is established is that Gridcoin ACCEPTS a script CONTAINING it |

### The BTC/LTC flag-set difference, because the two runs are not interchangeable

Same script, same harness, same step, two different gradings of the same refusal:

    BTC (Core 28.1)        8b mempool: mandatory-script-verify-flag-failed (Locktime requirement
                                       not satisfied)
    LTC (Core v0.21.4)     8b mempool: non-mandatory-script-verify-flag (Locktime requirement
                                       not satisfied)

BOTH PARENTHETICALS NAME CLTV. "Locktime requirement not satisfied" is `SCRIPT_ERR_UNSATISFIED_LOCKTIME`,
which only `OP_CHECKLOCKTIMEVERIFY` produces, so on both chains the opcode ran and refused. That
is not the part that differs.

WHAT DIFFERS IS WHICH LAYER OWNS THE REFUSAL. `non-mandatory` means the spend was re-checked
under the daemon's MANDATORY flag set and PASSED there -- so on that daemon this node declined to
relay it while the chain had not refused it, and a miner not applying that policy could have
included an early refund. The harness says exactly this at the step and does not score the two
the same, which is why the wording is worth keeping rather than collapsing into "refused".

So step 8c -- asking `generateblock` to MINE the early refund -- is LOAD-BEARING ON LTC and
merely corroborating on BTC. It refused with `TestBlockValidity failed: block-validation-failed`,
which is the chain's own validity check.

RECORDED AS A LIMIT: that message is GENERIC. It says the block was invalid and does not name the
locktime, so on LTC the consensus claim rests on a refusal that does not state its reason, where
on BTC and on Gridcoin the reason was named.

**THE LOG WAS THEN CHECKED, AND IT HELD NOTHING.** 2026-09-29, same run:

    grep -a -iE "locktime|block-validation|TestBlockValidity" regtest/debug.log | tail -20
    CTransaction(hash=ef17b1d269, ver=2, vin.size=2, vout.size=2, nLockTime=1351)
    CTransaction(hash=35543aae60, ver=2, vin.size=3, vout.size=2, nLockTime=1352)
    CTransaction(hash=316cc3becd, ver=2, vin.size=3, vout.size=2, nLockTime=1354)
    CTransaction(hash=c7c6e82d04, ver=2, vin.size=3, vout.size=2, nLockTime=1355)

Four hits and every one is a FUNDING transaction carrying the wallet's own anti-fee-sniping
nLockTime -- `ef17b1d269` and `35543aae60` are contracts [A] and [B] from step 6. Not a word
about the refusal. Bitcoin-derived daemons log script-verification failures under the
`validation` and `mempoolrej` categories only, and neither is on by default, so there was
nothing to find rather than something missed.

CHECKED AND ABSENT IS A DIFFERENT GRADE FROM NOT CHECKED, and the fix belongs to the harness
rather than to the operator: `daemons.REFUSAL_LOGGING` now passes `-debug=validation
-debug=mempoolrej` on the SPAWN path, so the next LTC run can read 8c's reason out of the log
the way `23148431f9` was read out of Gridcoin's. Named categories rather than `-debug=all`,
which on a run that mines 2500 blocks writes a log nobody reads. Until that run happens, the
sentence "LTC enforces CLTV at consensus" stays one grade weaker than the BTC and GRC versions
of it.

Which is also a reason not to read Litecoin's MANDATORY set from memory: it was not read for this
note, and whether v0.21.4 omits `SCRIPT_VERIFY_CHECKLOCKTIMEVERIFY` from it -- or whether
something else in that path explains the grading -- is unestablished rather than known.

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
| shared address derivation, ON STAGENET | **CONFIRMED 2026-09-29** | OK=4 FAIL=0 against `node.monerodevs.org:38089`, nettype STAGENET read from the daemon. This repo computed `5B5ybDArLcPdBe67aRWb4wWjzhdswh5EKXL5KxMYLBW2EeB4GWxy31nhpssDAzzKKrTf5xjFDji4P4vHCNMfcZgJ91qjDkt` offline from the summed scalars and `monero-wallet-rpc generate_from_keys` derived the IDENTICAL string, with stagenet prefixes. Spend keys summed to `f43e44df97b4e8d8…`, view to `8dc18ef4ea9205f4…` |
| the SWEEP on STAGENET | **SPENT 2026-09-29** | `e0551366693139976f6fa8074e6034c331a068dff9725cbb7c10e361785edf89`. 99,938,960,000 atomic units (0.09993896 XMR) swept OUT of the 2-of-2 shared address with `s_a + s_b`, on Monero stagenet, against `node.monerodevs.org:38089`. Funded by the xmr-tw.org faucet, `c9cd65f2dae52f37…`, 99,969,500,000 in; fee 30,540,000 atomic (0.00003054 XMR) |
| a funded GRC↔XMR swap, end to end | **THE HOP EXISTS, THE RUN HAS NOT HAPPENED** | Both halves are measured on real networks and the file that joins them is written as of 2026-09-29: `adaptor_regtest_verify.py --chain grc` now emits a shares fixture carrying the `s_a` it RECOVERED from a Gridcoin scriptSig, in the format `monero_shared_key_verify.py --sweep` reads. What remains is an operator funding that stagenet lock address and running the sweep -- a swap whose XMR leg moves because a Gridcoin daemon published the scalar. Until that run, this row is NOT a pass |

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

**b. ~~The HTLC refund branch has never run on Gridcoin, and `--chain` cannot ask for it.~~
DONE 2026-09-29** -- both directions: it SPENDS at the locktime and it is REFUSED before it.
The paragraphs below are kept in the order they were written, because the two halves closed
five days apart and reading them in sequence is what shows why the first one was not enough.
Three
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

and the line `6 CLTV REFUSES A FINAL REFUND BEFORE THE LOCKTIME` reads OK.

**2026-09-29: IT READ OK. THE GAP IS CLOSED.** `OK=11 FAIL=0 SKIP=0`, on Gridcoin testnet at
tip 3296544, and the three steps that matter are these:

| step | nLockTime | tip | script locktime | result |
|---|---|---|---|---|
| 5 control | 3296548 | 3296544 | 3296548 | REFUSED -- non-final, before any script ran |
| **6 THE MEASUREMENT** | **3296544** | **3296544** | **3296548** | **REFUSED** |
| 8 control | 3296548 | 3296548 | 3296548 | ACCEPTED, `ee9dc216bf055b22…` |

All three spend the SAME outpoint, `a82a91327cde9642:0`, under the SAME 94-byte redeem script.

WHY STEP 6 ISOLATES THE OPCODE, spelled out because Gridcoin returns the same `-22 TX rejected`
for every refusal and an identical error string is exactly where an overclaim hides. `IsFinalTx`
compares nLockTime against `nBestHeight + 1`:

  - step 5: 3296548 < 3296545 is FALSE, so it falls through to the sequence check, and our
    inputs carry 0xfffffffe. NON-FINAL. The mempool refuses it before loading the script, which
    is why this step is labeled a control and proves nothing about CLTV.
  - step 6: 3296544 < 3296545 is TRUE. FINAL. The finality check PASSES, so the refusal came
    from somewhere after it.

And the remaining candidates are excluded by step 8 rather than by argument: same outpoint, same
redeem script, same 293 bytes, same fee, same signer. The ONLY field that differs between the
refused step 6 and the accepted step 8 is nLockTime -- 3296544 against 3296548 -- and the only
rule in the system that reads that field after finality is
`<3296548> OP_CHECKLOCKTIMEVERIFY`. It refused 3296544 and accepted 3296548.

So: **OP_CHECKLOCKTIMEVERIFY executes and ENFORCES on Gridcoin.** It is not a no-op, and an
HTLC this tree funds there is NOT refundable by its funder before the locktime. That was the
open question, and it is the one that decides whether a GRC leg can be trusted in a swap at all.

**AND THE DAEMON SAID SO, same day, one grep later.** The paragraph above stood on a deduction:
`-22 TX rejected` is all Gridcoin returns, so which stage refused step 6 was argued from
`IsFinalTx` plus step 8's controls rather than read. The log resolves it, because
`ConnectInputs` prints the transaction's txid and the two refusals took DIFFERENT paths:

    2026-09-29T01:06:36Z ERROR: AcceptToMemoryPool : nonstandard transaction type
    2026-09-29T01:06:37Z ERROR: ConnectInputs() : 23148431f9 VerifySignature failed

`23148431f9` is the first ten hex of step 6's txid --
`23148431f9d2be987d881fdeec98b92b2ba83d0ab7993805869a3d61d953fcfe`, double-SHA256 of the refused
bytes the run printed. Step 5's is `cbf1d0cff6…`, and the stage that refused it does not log a
txid. So, read off Gridcoin's own source rather than inferred:

  - `policy/policy.cpp:57` -- `IsStandardTx` returns false when `!IsFinalTx(tx, nBestHeight + 1)`,
    which is why a NON-FINAL transaction is reported as "nonstandard transaction type". That is
    step 5, refused in `AcceptToMemoryPool` before any script was loaded. The control behaved
    as a control, and the `nBestHeight + 1` the argument above assumed is the actual expression.
  - `validation.cpp:656-669` -- `VerifySignature failed` is reached ONLY after the spend has
    been re-verified under `consensus_flags` ALONE and failed there too; a script that fails
    only under `consensus_flags | policy_flags` returns `non-mandatory script verify failure`
    instead. That is step 6: it PASSED `IsStandardTx`, therefore passed finality, reached
    `ConnectInputs`, and its SCRIPT failed **under consensus flags**.

Which script rule failed is then the only thing left, and step 8 excludes the alternatives by
measurement rather than by argument: same outpoint, same 94-byte redeem script, same signer,
same key, accepted with nLockTime 3296548. A broken signer could not have produced that. So
`OP_CHECKLOCKTIMEVERIFY` is the rule that refused, and this is now a LOG READING of the stage
plus a measurement of the alternative, not a chain of reasoning about either.

WHAT IT STILL DOES NOT ESTABLISH. The locktime used was tip+6, a test value; the production
`contract_locktime("GRC", ROLE_INITIATOR, tip)` is tip+1920, and the run says so itself. CLTV
does the same comparison with either number -- the opcode has no knowledge of how far away the
height is -- but the sentence "the production value was exercised" would be false and is not
made here.

THE 2026-09-28 UNRECONCILED TIMELINE NOW HAS A MECHANISM, and it is this one. That entry records
`nonstandard transaction type` and `VerifySignature failed` logged in the same second, with a
refund that nevertheless exists, as unexplained. The 2026-09-29 run reproduced the identical
PAIR one second apart, and here both lines are accounted for: the first is a non-final attempt
refused at the standardness gate, the second is a final-but-early attempt refused by the script.
A run that tries both before waiting out the locktime produces exactly that pair and then
succeeds. WHETHER THE 2026-09-28 PAIR WAS THAT SEQUENCE IS STILL NOT PROVEN -- that run's
refused bytes were not captured, so `bf63246378` cannot be matched against a txid the way
`23148431f9` just was. It is now the leading explanation and a checkable one, which is a
different grade from resolved (rule 17). `--chain grc` is
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

**d. ~~The stagenet Monero sweep.~~ DONE 2026-09-29.** Regtest and stagenet derive keys
identically, so the cryptographic claim was settled and the network-specific one was not. It is
now: `e0551366693139976f6fa8074e6034c331a068dff9725cbb7c10e361785edf89` spent 0.09993896 XMR out
of the shared address on stagenet with the summed key.

WHAT THE STAGENET RUN ADDED OVER THE REGTEST ONES, because "we did it again on another network"
would undersell it. Regtest is a chain this harness mines itself, with `generateblocks` funding
the address directly and no other participant. Stagenet is a chain with real miners, real block
times, a REMOTE daemon this repository does not control, and coins that arrived from a third
party -- a faucet in Taiwan -- who knew nothing but the address. Every one of those is a place a
locally-correct derivation could have failed and did not:

  - the address was accepted by a stranger's wallet software as a payable stagenet address;
  - a remote node served the block containing the payment to a wallet restored from summed keys;
  - the 10-block lock elapsed and the output became spendable, which regtest's mined coinbase
    path exercises differently;
  - `sweep_all` built, signed and BROADCAST a spend with the summed scalar, and the network
    accepted it.

The fee is recorded because it is the one number that says a real transaction happened rather
than a simulation: 30,540,000 atomic units, 0.00003054 XMR, taken by the network between
99,969,500,000 received and 99,938,960,000 swept.

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
