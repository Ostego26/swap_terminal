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
| **Tx_punish at T2** | **NONE** | **lock C is built and has never run.** See §5 |
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
| CLTV executes on GRC | **NONE** | the three GRC swaps took the HASHLOCK branch; `OP_CHECKLOCKTIMEVERIFY` sits in the `OP_ELSE` and never ran. What is established is that Gridcoin ACCEPTS a script CONTAINING it |

## 3. XRP escrow -- `xrp_htlc_escrow.py --run`

| Branch | Evidence | note |
|---|---|---|
| EscrowCreate | SPENT | XRP testnet, `9D3A81C4946F26E2…` |
| EscrowFinish, WRONG fulfillment | REFUSED | the hashlock holding |
| EscrowFinish, RIGHT fulfillment | SPENT | |
| EscrowCancel before CancelAfter | REFUSED | |
| EscrowCancel after CancelAfter | SPENT | second escrow, `DDD2CE2A37A830AE…` |

Both branches. This is the most completely exercised leg in the tree.

## 4. Monero

| Branch | Evidence | note |
|---|---|---|
| shared address derivation | SPENT | checked against `monero-wallet-rpc generate_from_keys`, byte-identical |
| sweep with `s_a + s_b` | SPENT | Monero REGTEST, `f584606948f430bf…`, 738,723,841,921,372 atomic units |
| the same on STAGENET | **NONE** | `docs/monero_stagenet_funding.md` says in its own words the regtest result "does NOT settle anything about stagenet specifically" |
| a funded GRC↔XMR swap, end to end | **NONE** | the script half and the Monero half have never been run as one. The cross-curve close is a KEY MATCH against an address, not a spend of coins at it |

---

## 5. THE GAPS, IN THE ORDER THEY COST SOMETHING

**a. Tx_punish at T2 has never spent.** Lock C and step 11 are built, tested offline and
pushed; the run costs one GRC payment of 4.6 and about 40 minutes of block waiting. This is
purely owed work with nothing blocking it.

**b. The HTLC refund branch has never run on Gridcoin, and `--chain` cannot ask for it.** Three
GRC swaps completed, all through the hashlock. The refund branch of a GRC HTLC -- the one an
operator needs when a counterparty vanishes -- has never been executed, and the harness that
would do it does not offer the chain. `OP_CHECKLOCKTIMEVERIFY` has never executed on Gridcoin
from this tree at all.

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
