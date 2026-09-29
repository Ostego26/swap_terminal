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

**THAT PARAGRAPH SAID "AND IT IS STILL NOT A SWAP" UNTIL 2026-09-29, AND IT WAS WRONG BY TWO
DAYS.** It is kept named rather than quietly replaced, because a reader who believed it would
rebuild a driver that exists and has completed both directions on real testnets -- which is more
expensive than the original error (rule 1: the drift is the point, and this document is the one
place a stale claim about what is proven does the most damage).

What was true of `xrp_htlc_escrow.py` and is STILL true of it: its `CancelAfter` comes from
`--cancel-after` on the command line, not from `lock_hours_for_role()`. That harness exercises
BRANCHES and was never a swap driver.

What is false is the conclusion drawn from it. `atomic_swap_xrp.py` derives BOTH legs from
`lock_hours_for_role()` in `swap_timelocks()`, follows the ROLE rather than the chain so the
reverse direction cannot invert the ordering, converts the participant's hours to a Gridcoin
HEIGHT from the tip, and asserts the ordering in `assert_timelock_ordering()`. And it has RUN:

| direction | result | evidence |
|---|---|---|
| XRP -> GRC | OK=16 FAIL=0 | escrow `C5563F1C9FECEFB8…`, GRC HTLC `7cf4b61200b913e4…`, GRC claim `d3134b2cfa8a208b…`, XRP finish `A4F8123482E24386…`. B's balance 115999980 -> 116999980 drops, asserted |
| GRC -> XRP | OK=16 FAIL=0 | GRC HTLC `9283c4c5d0df8c5b…`, escrow `98E03E67336167A6…`, XRP claim `B5E22EA1F4AACBDA…` (the Fulfillment carries the preimage), GRC claim `b6f7f57dc789bfb5…`. A's balance +999640 drops = 1000000 escrowed - 360 finish fee |

Both 2026-09-27, both testnet x testnet, recorded in `docs/atomic_swap_runs_2026_09_27.md`.

**SO THE GRC<->XRP SWAP IS THE ONE THAT IS FINISHED**, and since 2026-09-29 it is the only
cross-chain atomic swap this tree implements at all.

And the finding that pair produced, which is the reason it matters beyond XRP: the two
directions reveal the secret by DIFFERENT MECHANISMS -- `xrp-first` publishes the preimage in a
scriptSig on Gridcoin, `grc-first` publishes it in the `Fulfillment` field of an `EscrowFinish`
on a ledger with no scripting language at all. Atomicity does not require a script. It requires
only that TAKING your leg forces you to publish something the counterparty can read -- which is
why a chain with no script can still take a leg, and is the reason this finding outlasted the
adaptor-signature work that was removed.

Incidental, and worth keeping: the public testnet server answered `notSupported` to
server-side signing, and the harness switched to LOCAL signing with xrpl-py for that and every
later transaction. That is a fact about the server, not a failure, and the seed never left the
machine.

## 3b. Solana -- `solana_chain_check.py`

**THIS SECTION DID NOT EXIST UNTIL 2026-09-29, AND THE ABSENCE WAS THE DEFECT.** Every other
leg here carries rows saying what is proven and what is not. Solana carried none, and a missing
row is the one state this inventory cannot distinguish from an oversight -- so a reader could
conclude the chain was out of scope, or that somebody forgot, and the truth is neither.

| Branch | Evidence | note |
|---|---|---|
| the READ half of the adapter contract | **PASSED 2026-09-29** | `solana_chain_check.py` against `api.devnet.solana.com`, every step answered. Cluster DEVNET by genesis `EtWTRABZaYq6iMfeYKouRu166VU2xqa1wcaWoxPkrZBG`, solana-core 4.3.0, slot 505503638, epoch 1170 |
| rent exemption, both account sizes | PASSED | 650240 lamports for 0 bytes and 1488440 for 165, both matching the 5080 lamports/byte reference |
| a real address's balance and signatures | **NOT RUN** | no `--address` and no `SOL_HOT_WALLET`. The check says so as a RESULT rather than skipping quietly |
| an SPL mint | **NOT RUN** | same -- no `--mint`, no `SOL_SPL_MINT`, and the check prints "(none configured -- native SOL. This is a RESULT, not a skipped step.)" |
| `get_new_address()` | **REFUSES BY DESIGN** | not a gap. See below |
| `send_to_address()` | **REFUSES BY DESIGN** | not a gap. See below |
| any swap leg, either direction | **NONE** | there is no Solana swap driver in this tree, and there cannot be one until the refusal below is resolved |

**THE TWO REFUSALS ARE NOT UNFINISHED WORK, and this section exists partly to stop them being
read as such.** BTC, LTC and GRC answer `get_new_address` with `getnewaddress`: the DAEMON
derives a key, stores it in wallet.dat, and the application never holds a secret. Solana has no
equivalent -- no wallet daemon, no keystore, nothing on the far end of an RPC that can mint an
address and remember how to spend from it. So the application must hold something, and WHICH
something is a custody decision rather than an implementation detail:

    fresh keypair per swap   a stored secret for EVERY open swap. Largest secret surface.
    one account + memo       no new secrets; a misattributed deposit pays the wrong person.
    derivation from a seed   one secret, many addresses; the seed leaking is total loss.

README.md carries the three and recommends the memo strategy. Rule 20's "do not ask which" does
not reach this: that rule's own exception is fund movement, and address derivation and signing
are fund movement. Until an operator chooses, SOL is a PAYOUT-SIDE asset only and the refusal is
the guard rather than the gap.

**WHAT THE ADAPTER CONTRACT ACTUALLY IS, because it is the reason a non-UTXO chain fits at all.**
Measured 2026-09-25 by walking the AST of every module in `services/`, `workers/`, `routes/` and
`app.py` and collecting every attribute accessed on an adapter object: five methods, one call
site each. `chains/base.RPCAdapter`'s wide UTXO surface is NOT the contract, so `chains/solana.py`
does not subclass it -- and says so in its own header rather than leaving a reader to wonder
whether that was an oversight.

## 5. THE GAPS, IN THE ORDER THEY COST SOMETHING

**RUN 2026-09-28, and these rows moved:** commands 0, 3 and 4 of the operator's list all
passed -- the offline suite (2154), the BTC and LTC HTLC with both branches through the real
client (`regtest_htlc_verify.py --chain both --wipe`, OK=76 FAIL=0, CLTV refused by CONSENSUS on
both chains via `generateblock`), and the XRP escrow (OK=11 FAIL=0, both branches on the live
testnet). **All of the operator's commands that survive the 2026-09-29 removal have passed.**

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

## What was removed on 2026-09-29, and where its evidence went

Monero and the adaptor-signature swap were removed from this tree at the
operator's instruction. Four sections of this document went with them: the
adaptor swap chain, the Monero shared-key work, gaps (d) and (e), and the
GRC<->XMR swap that closed them.

THE EVIDENCE WAS NOT WRONG AND IS NOT BEING DISOWNED. Those runs happened and
the transactions are on public testnets -- `6dd649aedd6e344c` on Gridcoin
testnet published a Monero spend share out of an adaptor pre-signature, and
`1c390795d621daf6cc92aa8c56b5f823f96f9e2f714e37574d6f809ff590effb` on Monero
stagenet swept the lock that share opened. What changed is that this document
describes what the system CAN DO, present tense, and it can no longer do that.
A section recording a branch no code can take is the same defect as a comment
describing a function that was deleted.

The full text is in git history at the commit that removed the code. That is
the archive (rule 2), and it is a better one than a section here that a reader
has to date-check before trusting.

---

## What this document is NOT

It is hand-maintained, which is the thing CLAUDE.md rule 19 warns about: a list that is correct
the day it is written and wrong by the next commit, and the thing it would be wrong about is
which parts of a money system are proven. Two things hold it down and neither is a ratchet:

  - **every SPENT row names a txid or a harness**, so a claim here is checkable against a chain
    or re-runnable, rather than trusted;
  - **a row with no harness is a gap by construction.** The NONE rows are the work list, and
    the document stops being useful the moment somebody writes SPENT in one without a txid.
