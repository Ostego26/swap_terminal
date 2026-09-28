# The GRC<->XMR swap protocol: who moves first, and what each party can steal

    Role: design document (stage 5 of docs/dleq_cross_curve_design.md section 6)
    Reads: nothing at runtime. Written from that document, from the four
           components' own source, from /home/user/go-dleq, and from
           measurements taken in this container on 2026-09-27 and labeled
           inline.
    Writes: nothing
    Can move funds: no. It SPECIFIES something that moves funds on two chains,
           which is why every claim below says measured, read-from-source, or
           unverified.
    Mainnet-safe: not applicable -- this is prose. What it describes is not
           mainnet-safe, and section 7 of the DLEQ document argues it may never
           be. Nothing here contradicts that.

STATUS, AND WHAT STAGE THIS IS. `docs/dleq_cross_curve_design.md` section 6
stage 5 is: "protocol design: which party proves what, when, share separation
per 3.2, the join with `adaptor_ecdsa`, storage in SQL per rule 5", and its
evidence column says **"a second design document, reviewed before code"**. This
is that document. The stage ordering in that table is not decoration -- it puts
prose before code on this specific stage -- so the analysis below is the
deliverable and `swap_terminal/modules/monero_swap_protocol.py` is downstream of
it, carrying the decisions this document names and nothing else.

WHAT IS NEW HERE AND IS NOT IN THE OTHER DOCUMENT. Two things, and the second
is the reason this document is longer than the plan expected:

  1. The asymmetry analysis the DLEQ document does not attempt: an XMR swap has
     no preimage published in a scriptSig and no unilateral refund on the Monero
     side, so "who moves first and what can each party take" has a different
     answer than it has for an HTLC pair, and it has to be derived rather than
     carried over.
  2. **A FIFTH COMPONENT IS MISSING AND WAS NOT ON ANYBODY'S LIST.** The four
     tested components are not sufficient. See "THE FIFTH COMPONENT" below; it
     is measured, not suspected, and it is the honest headline of this document.

---

## 0. NAMES, AND WHY THE ROLES ARE NOT INTERCHANGEABLE

Two chains, and they are named by what they can express rather than by ticker,
because the protocol is generic over the script side exactly the way
`atomic_swap.py` is:

    S-chain   the SCRIPT chain: GRC, BTC or LTC. secp256k1 ECDSA, a scripting
              language with OP_CHECKMULTISIG and OP_CHECKLOCKTIMEVERIFY.
    X-chain   Monero. ed25519, NO SCRIPT AT ALL -- no hashlock, no timelock, no
              multisig opcode, and therefore no unilateral refund.

Two parties, and **the naming is a consequence of the cryptography rather than a
convention**, which is the first thing this document establishes:

    ALICE   holds XMR, wants S-coin. She funds the X-chain leg and RECEIVES on
            the S-chain.
    BOB     holds S-coin, wants XMR. He funds the S-chain leg and RECEIVES on
            the X-chain.

WHY ALICE -- THE XMR HOLDER -- MUST BE THE ONE WHO RECEIVES ON THE SCRIPT CHAIN.
This is the single structural fact the whole protocol hangs off, and getting it
backwards produces something that looks symmetric and loses money.

An adaptor signature leaks a scalar when it is COMPLETED and PUBLISHED. On the
X-chain there is nothing to publish -- Monero has no script, so no Monero
transaction can be made to reveal anything. The only publishable act in the
entire swap is an S-chain transaction. So the leak can only be arranged in one
direction: **somebody taking S-coin must be forced to publish a completed
adaptor signature, and the scalar it leaks must be the X-chain spend share the
other party needs.**

The party taking S-coin is the party who wanted S-coin, which is the XMR holder.
So the XMR holder's own Monero spend share is the scalar under which the
S-chain redeem is pre-signed. Reverse the roles -- pre-sign the redeem under the
S-coin holder's share -- and the party who takes the S-coin leaks a share that
is worthless to the counterparty, because the counterparty is not waiting on
Monero.

That is derived, not recalled, and the mirror-image protocol was written out and
rejected in the course of writing this document: with Bob receiving on the
S-chain, Bob holds his own share, so he can complete the redeem pre-signature
the moment he has it and take the S-coin without ever funding the X-chain leg --
and the scalar Alice recovers unlocks an address with nothing in it.

---

## 1. KEYS, AND WHAT IS EXCHANGED IN THE CLEAR

Per party, once per swap:

    S-chain key pair            A_sk / A_pk for Alice, B_sk / B_pk for Bob.
                                Ordinary secp256k1, ordinary WIF.
    Monero SPEND share          s_a, s_b. Sampled uniformly from [1, 2^252).
    Monero VIEW share           v_a, v_b. Same range.
    the adaptor point           Y_a = s_a*G on secp256k1, Y_b = s_b*G.
    the spend-share public key  S_a = s_a*B on ed25519, S_b = s_b*B.
    a cross-curve DLEQ proof    binding Y_a to S_a, and Y_b to S_b.

The X-chain lock address is an ORDINARY Monero address -- there is no Monero
multisig anywhere in this protocol, which is the fact that makes it implementable
at all:

    public spend key    S = S_a + S_b        private   s = s_a + s_b mod l
    public view  key    V = V_a + V_b        private   v = v_a + v_b mod l

**THE VIEW SHARES ARE EXCHANGED IN THE CLEAR AND THE SPEND SHARES ARE NOT.** Both
parties therefore hold the whole private view key `v` from setup onward, and
neither holds the whole spend key until the swap resolves. That asymmetry is load
bearing twice over: a party who could not watch the lock address could not tell a
funded swap from an unfunded one, and a party who could watch it with their own
view share alone would be able to see funds the other could not -- which is why
`chains/monero_keys.shared_address()` takes FOUR arguments rather than two and
says so at the site.

The range `[1, 2^252)` is not a style choice and is not negotiable in either
direction. `2^252 < l = 2^252 + 27742317777372353535851937790883648493`, so a
252-bit share is always a valid ed25519 scalar; and 252 is the cross-curve
DLEQ's own bound (`min(ed25519 252, secp256k1 255)`, measured from
`go-dleq`'s `BitSize()` and echoed by the running helper's `version` response as
`bit_count: 252`). A share at or above `2^252` cannot be proven across the
curves at all, and a prover that reduced it instead of refusing would commit to
two different integers on the two curves.

### 1.1 The sum is never proven, and this is the silent failure the other document warned about

`docs/dleq_cross_curve_design.md` section 3.2 is titled "The dangerous case is
the sum, and it is silent". **Measured here, 2026-09-27, 2000 random share
pairs from `[1, 2^252)`: 961 of them, 48.0%, sum to at least `l`.** The doc
projected "roughly half"; that is the measurement behind it.

For one such pair (`s_a = 2^252 - 5`, `s_b = 2^252 - 9`), measured in this
container:

    s_a + s_b as an integer      14474011154664524427946373126085988481658748083205070504932198000989141204978
    (s_a + s_b) mod l             7237005577332262213973186563042994240801631723825162898930247062703686953989
    they differ by exactly l     True
    ed25519:  S_a + S_b == ((s_a+s_b) mod l) * B      True
    secp256k1: Y_a + Y_b == (s_a+s_b) * G             True
    secp256k1: Y_a + Y_b == ((s_a+s_b) mod l) * G     FALSE   <- the silent case

So `Y_a + Y_b` and `S_a + S_b` are commitments to DIFFERENT INTEGERS about half
the time, with no error anywhere. The protocol rules that follow are absolute:

- **Each share gets its own DLEQ proof and its own adaptor point.** There is
  never a proof about the sum, and there is never an adaptor signature under
  `Y_a + Y_b`.
- **Nothing compares a secp256k1 sum against the Monero spend key, or derives
  one from the other.** The addition happens on the ed25519 side only, after the
  missing share is known as an integer.
- The S-chain side of this protocol never adds two adaptor points together at
  all. If a future change needs to, that is the review trigger.

---

## 2. THE FIFTH COMPONENT, WHICH DOES NOT EXIST

The four components named in `chains/monero_keys.py`'s header and in
`atomic_swap.py`'s -- `modules/adaptor_ecdsa.py`, `modules/ed25519_group.py`,
`modules/dleq_helper.py`, `chains/monero_keys.py` -- are each tested, and they
are **not sufficient**. Composing them needs one more thing that is not in the
tree and was not on the list.

**MEASURED, by reading `modules/atomic_htlc_scripts.build_htlc_redeem_script()`
and `modules/htlc_spend.hashlock_script_sig()`: this repo's HTLC has two
branches and BOTH END IN A SINGLE-KEY `OP_CHECKSIG`.** The hashlock branch is

    OP_IF OP_SHA256 <hash> OP_EQUALVERIFY OP_DUP OP_HASH160 <p_hash>
    OP_EQUALVERIFY OP_CHECKSIG

and the claimer satisfies it with a signature under their OWN key, produced
freely by `htlc_spend.sign_digest`. **Nothing constrains which signature they
use, so nothing can be arranged to leak when they spend.** An adaptor signature
has no purchase on a single-key output: the whole mechanism requires that the
spender be UNABLE to sign alone, so that the only signature available to them is
the one they must complete with the scalar.

Therefore an adaptor-based XMR swap cannot reuse the HTLC at all. It needs, on
the S-chain:

    Tx_lock     an output paying a 2-of-2 { A_pk, B_pk }. No hashlock. The
                hashlock is what the adaptor REPLACES, not something it joins.
    Tx_redeem   spends the lock to ALICE. Needs both signatures; Bob supplies
                his as an ADAPTOR pre-signature under Y_a.
    Tx_cancel   spends the lock to a second 2-of-2 { A_pk, B_pk }, nLockTime T1.
                Both parties hold both plain signatures from setup, so either can
                publish it once T1 has passed.
    Tx_refund   spends the cancel output to BOB. Needs both signatures; Alice
                supplies hers as an ADAPTOR pre-signature under Y_b.
    Tx_punish   spends the cancel output to ALICE, nLockTime T2 > T1. Both plain
                signatures exchanged at setup.

**ALL FIVE NOW EXIST, AND FOUR OF THEM HAVE BEEN SPENT ON A CHAIN.** This
paragraph said "None of those five exist" and listed the 2-of-2 script builder,
the four-way pre-signed chain and the fee arithmetic as missing. That was written
against the tree as of `5b5109a` and was already false: `988bd6c` added
`modules/adaptor_swap_scripts.py` (the 2-of-2 redeem script,
`two_of_two_p2sh_script`, the OP_0-dummy `two_of_two_script_sig`, and
`two_of_two_sighash`), `modules/adaptor_swap_chain.py` builds `build_redeem`,
`build_cancel`, `build_refund` and `build_punish` plus `fee_satoshis_for` across
them, and `16a4644` funded and spent the lock on Litecoin Core 0.21.4 regtest --
40 checks OK, 0 FAIL, 0 SKIP, both `OP_CHECKMULTISIG` footguns refused by the
daemon, nLockTime refused at CONSENSUS (`generateblock: -25 TestBlockValidity
failed: bad-txns-nonfinal`) and not merely at relay, the cancel txid predicted
before broadcast and matched, and the second 2-of-2 spent by the refund.

What was reused rather than rebuilt, as this paragraph correctly anticipated:
`htlc_spend.legacy_sighash` (the digest an adaptor signature has to be over),
`parse_transaction`, `varint`, `push_data`, `encode_script_number`, `decode_wif`,
`public_key_for`, `coins_to_satoshis`.

**WHAT IS STILL MISSING IS NARROWER AND HARDER: THE JOIN.** Measured 2026-09-28
by grepping `adaptor_ecdsa`, `pre_sign`, `complete_signature` and `recover` across
`adaptor_regtest_verify.py` and `swap_terminal/regtest/adaptor_steps.py` -- ZERO
occurrences. That chain spend used two ORDINARY 2-of-2 signatures, so the single
property an adaptor signature exists for (the spender cannot sign alone, and
completing the signature is what publishes the scalar) has never been exercised
against a consensus rule. Nothing hands a `ChainTransaction.digest` to
`adaptor_ecdsa.pre_sign`; `modules/monero_swap_protocol.py` has exactly one
importer and it is `monero_swap.py`. And the chain was LITECOIN: nothing above is
established on Gridcoin, where `adaptor_regtest_verify.py --chain grc` has not
completed.

**This is named work, not a baseline (rule 19), and it is the next stage.** It is
also the stage that cannot be honestly written blind: a consensus script whose
only proof is that its author read it is the thing "Verify by behavior, never by
reading the code" exists to refuse. It wants a regtest chain and both branches
exercised, which is the same standard the HTLC refund branch is still measured
against.

**THE JOIN IS BUILT AS OF 2026-09-28, AND WHAT THAT DOES AND DOES NOT SETTLE.**
The two paragraphs above are kept because the measurement in them -- zero
occurrences of `adaptor_ecdsa` in the harness, a chain spend made with two
ORDINARY signatures -- is what the work was aimed at, and a measurement in prose
ages (rule 1).

WHAT EXISTS NOW. `swap_terminal/regtest/adaptor_join.py` holds the decisions:
pre-sign a `ChainTransaction.digest` under an adaptor point, DER-encode the
completed `(r, s)` with the SIGHASH byte, walk a scriptSig's pushes back into
signatures, and recover the scalar from bytes fetched back off a chain.
`regtest/adaptor_steps.py` uses it in both places section 2 specifies:

    Tx_redeem   Bob's signature is a pre-signature under Y_a, completed by
                Alice with s_a. Bob's key NEVER signs that digest.
    Tx_refund   Alice's is a pre-signature under Y_b, completed by Bob with s_b.
    Tx_cancel   plain both sides -- and `nothing_leaks()` asserts against the
                PUBLISHED cancel that it tells neither party anything, rather
                than arguing it from the absence of an `adapt` call (rule 17).

and the run now closes the cross-curve loop: the scalar recovered from the
redeem is added to the other share on ed25519 by
`monero_swap_protocol.reconstruct_spend_key`, and the result's public key is
compared against the public spend key DECODED BACK OUT OF THE LOCK ADDRESS. Two
of `ChainOutcome`'s six decisive outcomes are those two facts, so a run that
spends a 2-of-2 with ordinary signatures and publishes nothing now scores FAIL
and says what it would have cost -- which is precisely what the 2026-09-28
Gridcoin run was, scored 40 OK / 0 FAIL.
`tests/test_adaptor_join.py::test_an_ordinary_2of2_spend_is_NOT_scored_as_having_published_anything`
rebuilds that exact state and asserts the new answer.

WHAT IS STILL NOT SETTLED, AND IT IS THE HALF THAT NEEDS A DAEMON. Everything
above is measured OFFLINE -- 14 tests, no network. No run of
`adaptor_regtest_verify.py --chain grc` has yet driven the adaptor version
against the operator's Gridcoin testnet daemon, so "a Gridcoin transaction
carrying an ADAPTED signature relays and confirms" is still a proposal in rule
16's sense and not a fix. The `e75f257a` redeem and the `91e096f5` refund
recorded above and in `docs/gridcoin_2of2_spend_2026_09_28.md` are the ORDINARY
version of those transactions. What changed is that the harness can now tell the
two apart, which it could not before.

**THAT RUN HAPPENED THE SAME DAY AND THE PARAGRAPH ABOVE IS NOW OUT OF DATE.**
It is kept because it states exactly what was missing, and because the gap
between "there is now something to run" and "it ran" was about four hours --
which is the timescale at which a measurement in prose ages (rule 1).

    adaptor_regtest_verify.py --chain grc, Gridcoin testnet v5.5.1.0, 2026-09-28
    OK=48  FAIL=0  XFAIL=1  SKIP=2  --  exit code 0, ESTABLISHED

    Tx_redeem  ccacc0614e34af7acd69a7941092b906d687fbc6bf6f7061492f868de16607ec
    Tx_refund  8fd143103ed91b36ff8d64aafb403cb3c5369800003128fe5367bb9dc6d21e0d

Both carry an adaptor pre-signature completed with a Monero spend share, both
were relayed and confirmed, and for both the scalar came back out of the
scriptSig FETCHED FROM THE DAEMON -- with the ordinary signature beside it
yielding nothing, and the recovered scalar's ed25519 public key equal to the
share captured at setup. `s_a + s_b` then reconstructed a private spend key
whose public key is the one decoded out of the lock address. The plain-signature
`Tx_cancel` published nothing, asserted over its 218 published bytes.

**AND THE SWAP STILL HAS NOT HAPPENED.** No Monero moved. The cross-curve close
is a KEY MATCH against an address, not a spend of coins at it; the spend is
established separately on a Monero regtest chain and the two halves have never
been run as one. The consensus half of the timelock is still a reading, because
Gridcoin has no `generateblock`. And `modules/adaptor_ecdsa.py` is still
unaudited -- a chain accepting its output says the encoding is right and says
nothing about whether the scheme is secure.

`docs/gridcoin_adaptor_join_2026_09_28.md` is the full record.

UPDATE 2026-09-28: THE FIVE TRANSACTIONS NOW EXIST AND FOUR OF THEM HAVE BEEN
SPENT ON A CHAIN. `swap_terminal/modules/adaptor_swap_chain.py` builds Tx_lock,
Tx_redeem, Tx_cancel, Tx_refund and Tx_punish, and
`adaptor_regtest_verify.py` drove them against Litecoin Core 0.21.4 regtest:
40 checks OK, 0 FAIL. The 2-of-2 P2SH funded, spent with both signatures in key
order, and was REFUSED both with the signatures transposed
(`mandatory-script-verify-flag-failed (Signature must be zero for failed
CHECK(MULTI)SIG operation)`) and with the OP_0 dummy missing (`Operation not
valid with the current stack size`). The cancel's nLockTime was refused before
T1 by relay AND by consensus -- `generateblock` answered `TestBlockValidity
failed: bad-txns-nonfinal` -- and the same transaction was accepted at T1. The
second 2-of-2, the cancel output, also spent both ways.

Two paragraphs below and one in `adaptor_swap_scripts.py` carried a claim about
GRIDCOIN AND CLTV that is weaker than it reads, and both were mine.

**CORRECTION.** The sentence that used to be here said "CLTV is established on
GRC -- three swaps completed through `atomic_swap.py` on 2026-09-27 using
`OP_CHECKLOCKTIMEVERIFY`". Those three swaps took the HASHLOCK branch:
`docs/atomic_swap_runs_2026_09_27.md` records a GRC *claim* txid for each and no
GRC refund anywhere. `OP_CHECKLOCKTIMEVERIFY` sits in the `OP_ELSE` branch,
which a hashlock spend never executes. So what they establish is that Gridcoin
ACCEPTS a script CONTAINING that opcode in a branch that does not run -- a real
fact, and not the fact that was claimed. The `generateblock: TestBlockValidity
failed` measurement that was also cited for GRC is `regtest_htlc_verify.py`'s
step 8c, whose `--chain` flag offers `btc`, `ltc` and `both` only; no
`generateblock` call has ever been made against a Gridcoin daemon from this
tree.

None of that touches the chain in section 2, which uses NO
`OP_CHECKLOCKTIMEVERIFY` at all: T1 and T2 are plain `nLockTime` fields on
transactions that cannot move without both parties, and nLockTime finality has
no deployment to activate.

The question that still belongs to the operator (rule 16, fund movement) is
therefore narrower than it was, and it is still open: **does Gridcoin's
consensus accept `OP_CHECKMULTISIG` inside a P2SH redeem script, and does it
enforce `nLockTime` on a transaction spending one?**
`adaptor_regtest_verify.py --chain grc` is written to answer exactly that
against a Gridcoin TESTNET daemon, and it has NOT been run -- no
`gridcoinresearchd` exists in this container. Bitcoin is unmeasured too, for a
duller reason: bitcoincore.org is blocked by the egress proxy here and the
`bitcoin/bitcoin` GitHub release carries no binaries.

---

## 3. THE PROTOCOL, STEP BY STEP

Timelocks are two absolute points on the S-chain, written as block heights
through `OP_CHECKLOCKTIMEVERIFY`, and they are compared in **TIME**, never in
blocks -- see section 5.

    T1   cancel becomes available
    T2   punish becomes available,  T2 > T1

### Step 0 -- SETUP. Nothing is funded and nothing is at risk.

Both parties sample their shares, then exchange, in one round:

    Alice -> Bob     A_pk, S_a, V_a, v_a, Y_a, DLEQ_a
    Bob   -> Alice   B_pk, S_b, V_b, v_b, Y_b, DLEQ_b

Each party then, before anything else:

  a. **Verifies the counterparty's DLEQ against the keys it claims to be about.**
     `dleq_helper.verify(proof, expect_secp256k1=Y_other, expect_ed25519=S_other)`
     -- with the `expect_` arguments, never `verify_any`. A proof that verifies
     is a proof about SOME pair of keys; using it as evidence about the pair you
     hold is only sound if you checked that it is the same pair. That check is
     `commitments_match()` and it is the one whose absence is easiest to mistake
     for a working verifier.
  b. **Refuses a counterparty spend share that is non-canonical, off-curve, the
     identity, or has a torsion component.** `monero_keys._checked_public_share`
     does all four. The identity case is a protocol attack rather than a typo: a
     share of `0` makes `s` equal to the other party's share alone, so whoever
     submitted the identity has handed the whole spend key to the counterparty.
     A torsion component means several distinct encodings agree with the share,
     so the share does not pin one point.
  c. **Builds the lock address from the bytes it just verified** -- the same `S_b`
     the DLEQ was checked against, not a second copy from anywhere else -- and
     **compares the resulting address, byte for byte, against the counterparty's
     independently computed one.** Point addition commutes, so the two must be
     identical; if they are not, one side is working from different inputs and
     the swap stops before any funding.

Then the S-chain transactions are built and the signatures exchanged:

    Alice -> Bob     plain signature on Tx_cancel
                     ADAPTOR pre-signature on Tx_refund, under Y_b
    Bob   -> Alice   plain signature on Tx_cancel
                     plain signature on Tx_punish
                     -- and NOT the redeem pre-signature. See step 3.

Each `pre_verify`s the pre-signature it received, passing the adaptor point it
expects as a separate argument. `adaptor_ecdsa.pre_verify` takes `adaptor_point`
as a parameter rather than reading it off the object being verified, and its
docstring says why: reading `Y` out of the pre-signature would make the check
vacuous, since the object would then always verify under its own claim.

**Why it is safe for Alice to hand over the Tx_refund pre-signature this early,
when Bob knows `s_b` and can complete it immediately.** Because Tx_refund spends
the output that Tx_cancel CREATES, and Tx_cancel cannot confirm before T1. Bob
completing it early produces a transaction with no input to spend. And the money
it moves is Bob's own S-coin returning to Bob. The scalar it leaks, `s_b`, is
worthless to Alice at this point: she has locked no XMR, so the address it helps
open is empty.

### Step 1 -- BOB FUNDS THE S-CHAIN LOCK. Bob broadcasts Tx_lock and waits for confirmations.

### Step 2 -- ALICE FUNDS THE X-CHAIN LOCK. Alice sends the XMR to the shared address, and waits for it to be both confirmed AND SPENDABLE.

Those are two different conditions on Monero and the published documentation has
the second one backwards. **Measured on the operator's host, 2026-09-27: a
transfer with 3 confirmations and `unlock_time=0` still reported `locked=True`
and contributed nothing to `unlocked_balance`.** `locked=True` means NOT
spendable. An ordinary Monero output locks for 10 blocks; a coinbase output
locks for 60. So the wait here is on `locked=False` and on
`unlocked_balance`, not on a confirmation count, and `chains/monero_transfers.py`
carries `FIELD_LOCKED` and `FIELD_UNLOCK_TIME` for exactly this.

### Step 3 -- BOB RELEASES THE REDEEM PRE-SIGNATURE, and only now.

Bob sends Alice the ADAPTOR pre-signature on Tx_redeem under `Y_a`, **after he has
seen the X-chain lock confirmed and unlocked, and not before.**

**THIS WITHHOLDING IS A SECURITY PROPERTY AND NOT AN OPTIMIZATION.** Alice holds
`s_a`, so the moment she has that pre-signature she can complete it and take the
S-coin. Released at setup, she can take Bob's S-coin without ever sending any
XMR: Bob recovers `s_a` from the broadcast, computes `s = s_a + s_b`, opens the
lock address, and finds it empty. Bob's entire protection in step 1 through step
3 is that this one message has not been sent yet.

Stated as a rule for the implementation: **the redeem pre-signature is the last
thing to leave Bob's process, it is gated on an observation of the X-chain rather
than on a message from Alice, and nothing may compute it earlier "to have it
ready".**

### Step 4 -- ALICE REDEEMS, WHICH IS WHAT PUBLISHES HER SHARE.

Alice `adapt()`s Bob's pre-signature with `s_a`, adds her own signature, and
broadcasts Tx_redeem. She receives the S-coin. The broadcast puts a complete
ECDSA signature on a public chain.

**She must do this so that it CONFIRMS strictly before T1, with a margin.** Past
T1, Bob can publish Tx_cancel, which spends the same output and invalidates
Tx_redeem -- see hazard 3.

### Step 5 -- BOB RECOVERS THE SHARE AND SWEEPS.

Bob reads Tx_redeem off the chain, extracts `(r, s)`, and calls
`recover_adaptor_secret(pre_signature, signature)`. Then, before using it:

- **He checks `s_a * B == S_a`**, against the `S_a` the DLEQ was verified against
  and the address was built from. `recover_adaptor_secret` guarantees only that
  the returned scalar multiplies `G` to `Y_a` on secp256k1; the ed25519 half of
  that claim comes from the DLEQ, and re-checking it at the point of use is a
  cheap independent confirmation that costs one scalar multiplication and catches
  the case where the DLEQ step was skipped, or verified against a different key,
  or where the wrong pre-signature object was passed in.
- Then `s = s_a + s_b mod l`, reconstruct the wallet with
  `generate_from_keys`, and sweep.

Two measured facts about that recovery, both from
`adaptor_ecdsa.recover_adaptor_secret`'s own behavior rather than from its prose.
**Measured here over 12 random trials, 2026-09-27: the recovered integer equals
the witness exactly in 12 of 12, and its ed25519 public key equals the
DLEQ-proven commitment in 12 of 12.** And the `-Y` branch of that function --
the one that exists because `adapt()` negates `s` for BIP62 low-S -- **fired in 6
of those 12 trials.** It is not a curiosity; deleting it would break recovery on
about half of all real swaps, which is a coin flip for money.

### The CANCEL path, when step 2, 3 or 4 does not happen.

After T1, either party publishes Tx_cancel (both hold both signatures). Then:

- **Bob publishes Tx_refund**, completing Alice's pre-signature with `s_b`, and
  gets his S-coin back. That broadcast leaks `s_b` to Alice, who checks
  `s_b * B == S_b`, computes `s`, and sweeps her XMR back. This is the clean
  unwind and both parties end where they started, minus fees.
- **If Bob does not refund, then after T2 Alice publishes Tx_punish** and takes
  Bob's S-coin. Her XMR is then locked forever, because Bob has no remaining
  incentive to leak `s_b` -- so this is not a restoration, it is compensation.

---

## 4. THE ANALYSIS: PER PARTY, PER STEP, WHAT CAN BE TAKEN AND WHAT IS LOST BY WALKING AWAY

The question at every row is the one that matters: can one side end up holding
both legs. `S` is Bob's S-coin, `X` is Alice's XMR.

| after step | Alice holds | Bob holds | Alice can take | Bob can take | Alice walks away | Bob walks away |
|---|---|---|---|---|---|---|
| 0 setup | `s_a`, `v`, `S_b`, `Y_b`, Bob's Tx_cancel sig, Bob's Tx_punish sig | `s_b`, `v`, `S_a`, `Y_a`, Alice's Tx_cancel sig, Alice's Tx_refund pre-sig | nothing -- no funds exist | nothing -- no funds exist | nothing lost | nothing lost |
| 1 S-chain locked | same | same, plus `S` committed to the 2-of-2 | nothing before T1. After T1: cancel, then punish after T2 -> she takes `S` **if Bob is offline** (hazard 1) | nothing -- he cannot spend the 2-of-2 alone, and he holds no redeem pre-sig because he is the one who withholds it | nothing lost | `S` at risk until he refunds or punish fires |
| 2 X-chain locked | same, `X` committed to the shared address | same | as above | nothing -- he cannot spend the shared address without `s_a`, and he has sent no redeem pre-sig | `X` at risk: recoverable only if Bob refunds | `S` at risk as above |
| 3 redeem pre-sig released | plus Bob's pre-sig on Tx_redeem | same | **`S`, by completing the pre-sig with `s_a` -- which is the intended step and publishes `s_a`** | nothing new | she forfeits `S`; her `X` returns only if Bob cancels and refunds | unchanged |
| 4 Tx_redeem confirmed | `S` | plus `s_a` off the chain | nothing further -- she cannot open the shared address without `s_b` | **`X`, which is the intended step** | -- | he forfeits `X`; Alice already has `S` |
| 5 swept | `S` | `X` | -- | -- | -- | -- |
| cancel, Bob refunds | `X` back | `S` back | nothing further | nothing further | -- | -- |
| cancel, T2 passes | `S` by punish; `X` burned | nothing | -- | -- | -- | total loss of `S` |

**Read across the two "can take" columns: there is no row in which either party
can take both `S` and `X`.** Alice's route to `S` is the redeem, and it publishes
the scalar that gives Bob `X`. Bob's route to `X` requires that publication.
Bob's route to `S` is the refund, and it publishes the scalar that gives Alice
`X` back. Every path that moves one leg moves the other.

That holds for the protocol as specified. **It does not hold unconditionally, and
three hazards are named below rather than buried.** One of them -- hazard 3 -- IS
a both-legs outcome, it is a race rather than a protocol flaw, and it is stated
plainly as the brief asked.

### HAZARD 1 -- Bob must be online in the window [T1, T2] or he loses `S` outright.

From step 1 onward, Alice holds Bob's plain signatures on Tx_cancel and
Tx_punish. She can publish Tx_cancel at T1 and Tx_punish at T2 and take Bob's
S-coin **without ever having funded any XMR.** Bob's only defense is to publish
Tx_refund in between, which he can always do -- he holds Alice's pre-signature and
knows `s_b` -- but it requires him to be watching.

This is not fixable, it is the shape of the protocol: the punish path exists
precisely so that Alice is not left with locked XMR and no recourse when Bob
vanishes, and a punish path that Bob can disarm would not be one. What follows
from it is a requirement on `T2 - T1` and on Bob's deployment: that window is the
maximum time Bob may be unresponsive, and it must be generous relative to
S-chain confirmation times. **It also means the FIRST FUNDER carries the liveness
risk**, which is the honest answer to "who moves first": Bob does, and he pays
for it in an uptime obligation rather than in exposure to theft by an attentive
counterparty.

### HAZARD 2 -- Alice must not fund the X-chain leg without enough time left.

Alice funds in step 2 but cannot redeem until step 3, and step 3 is Bob's to
send. If T1 arrives first, the cancel path opens and she is dependent on Bob
refunding to get her XMR back -- with punish as compensation if he does not. She
is never left with nothing, but she can be left with the wrong asset.

The requirement is arithmetic and belongs in the implementation as a refusal:
before funding the X-chain leg, the time remaining until T1 must exceed the
X-chain confirm-and-unlock wait plus the S-chain redeem confirmation plus a
margin. **The X-chain unlock is a real term and it is easy to omit: 10 Monero
blocks at a 120s target is 1200s, about 992µfn, before the lock is spendable at
all** -- and Bob is waiting on that same condition in step 3.

### HAZARD 3 -- THE RE-ORG RACE AT STEP 4, WHICH IS THE ONE BOTH-LEGS OUTCOME.

Stated plainly, as asked, because it is the one place this protocol lets a single
party hold both legs:

> The instant Alice broadcasts Tx_redeem, `s_a` is public. If that transaction is
> then re-orged out and Tx_cancel confirms in its place, Bob holds `s_a` AND
> `s_b` AND his own S-coin. He sweeps the XMR and keeps the S-coin. **He has both
> legs and Alice has nothing.**

Three things are true about it and all three have to be said together. It is not
a flaw in the cryptography -- every signature involved is exactly what it claims
to be. It exists in the published protocol this one is derived from, for the same
reason, and is not something this design introduced. And it is bounded entirely
by one quantity: **the number of S-chain confirmations between Alice's redeem and
T1.** A redeem that confirms deeply before T1 cannot be replaced by a
transaction that is not valid until T1.

So the mitigation is a refusal, not a warning, and it has to live in the
implementation: Alice does not broadcast Tx_redeem unless the time remaining
until T1 exceeds a stated confirmation depth by a stated margin, and if that
margin has already been lost she goes to the cancel path instead -- taking the
refund-or-punish outcome, which costs her the trade but not the asset. **A
protocol that broadcasts the redeem "because we finally got the pre-signature"
with an hour left before T1 is the version of this that loses money**, and it is
the version anybody writes first.

### What is NOT a hazard, checked rather than assumed

- **A pre-signature under a substituted adaptor point.** Bob pre-signing under
  some `Y'` whose discrete log he knows would give Alice a pre-signature she
  cannot complete with `s_a`. `pre_verify(public_key, message_hash,
  adaptor_point=Y_a, pre_signature)` refuses it, because the expected point is a
  separate argument and is compared against the object's own.
- **A valid DLEQ proof about different keys.** `verify()`'s `expect_*` arguments
  are required, not optional, and `commitments_match` refuses on mismatch.
- **A share with a torsion component, or the identity, or a non-canonical
  encoding.** `_checked_public_share` refuses all three.
- **A share at or above `2^252`.** The DLEQ prover refuses it and so does
  `witness_from_int`, which quotes the BIT LENGTH rather than the value because
  the value is a spend-key share.
- **A wrong secret handed to `adapt()`.** It is not checked there, deliberately,
  and the caller's correct response is that the resulting signature does not
  verify. That is the documented contract and it is the right one: the
  counterparty's node performs that check anyway.

---

## 5. TIMELOCKS ARE COMPARED IN TIME, ACROSS THREE DIFFERENT BLOCK TARGETS

`assert_ordering()` in `atomic_swap.py` carries the incident this rule comes
from, measured on the operator's first real run on 2026-09-27: a correctly built
BTC/LTC swap was REFUSED because the check subtracted one chain's block count
from the other's. Litecoin needs four times the blocks for half the time.

This protocol is worse, because there are **three** block targets in play and one
of them belongs to a chain with no timelock at all:

    GRC   90s   S-chain, and T1/T2 are heights on it
    BTC   600s  same role when the S-chain is Bitcoin
    LTC   150s  same
    XMR   120s  NOT a timelock. It converts the 10-block output lock into time.

`htlc_timelock.SECONDS_PER_BLOCK` holds the first three and **deliberately does
not hold XMR**, which is a rule 8 divergence and so is named at both sites: if
`"XMR"` were a row there, `timelock_blocks("XMR", role)` would return a block
count for a chain that cannot express a timelock, and `contract_locktime` would
hand back a height for a script that does not exist. The Monero block target
lives in `modules/monero_swap_protocol.py` with that sentence beside it.

The ordering the implementation must refuse to violate, all in seconds:

    T1 - now   >   X-chain confirm wait + X-chain unlock (10 blocks)
                 + S-chain redeem confirmations + margin       (hazard 2, 3)
    T2 - T1    >   S-chain cancel confirmations
                 + Bob's maximum unresponsive window + margin  (hazard 1)

Both are refusals with a positive margin, following `assert_ordering`'s shape,
and both report the margin in µfn with the seconds in parentheses. A warning on
either is a warning nobody reads until a swap has been taken.

---

## 6. WHERE THE STATE LIVES

Rule 5 and rule 20: `runtime`-adjacent files are mirrors, and the authority is
`swap_terminal.db`. For this protocol specifically:

- The DLEQ proof is a 64,966-64,968 byte blob (measured; it is not a constant --
  the secp256k1 signature inside it is DER and varies from 70 to 72 bytes). It
  belongs in SQL as a blob or hex column on the swap row, **with the verification
  VERDICT stored as its own column**, so that no later decision requires
  re-parsing 64 KiB or re-spawning the helper. The verdict is the decision; the
  blob is the evidence for it.
- The two public shares, the two adaptor points, the lock address, the five
  transaction ids and the two timelock heights are columns.
- **No private share is ever written anywhere.** Not to the database, not to a
  log, not to a file, not into an exception message, and not onto a command line
  -- argv is world-readable through `/proc` and `ps`, which is why
  `dleq_helper` speaks a line protocol over stdin rather than taking the witness
  as an argument. A share that has to survive a process restart is a design
  problem to solve some other way, not a row to add.

---

## 7. WHAT THIS DOCUMENT DOES NOT DO, AND WHAT IS STILL TRUE FROM SECTION 7

`docs/dleq_cross_curve_design.md` section 7 is "REASONS NOT TO BUILD THIS" and
**nothing here answers it.** Its arguments are untouched by this document and by
the code downstream of it:

- There is no audited implementation of the cross-curve DLEQ construction in any
  language. `go-dleq`, which the helper calls, ships no soundness test.
- A broken DLEQ does not error. It lets a counterparty commit to different
  scalars on the two curves, take the S-coin, and leave the XMR locked to a key
  nobody holds. The symptom arrives after the money moved and reads as a stuck
  swap.
- The Monero settlement path is thin. Reorg handling and the sweep are not built.
- The custodial alternative's failure mode is "the operator can take the funds",
  which is known, disclosed and priced. This one's is "the cryptography is
  wrong", which is none of those.

What this document adds to that ledger is a fifth reason, and it is specific:
**the S-chain side needs a 2-of-2 plus a four-transaction pre-signed chain that
does not exist, so the swap is one component further away than the four-tested-
components sentence implies.** That is a correction to the tree's own summary in
`atomic_swap.py` and in `chains/monero_keys.py`, both of which say the four
components are individually tested and nothing composes them -- true, and
incomplete, because composing them is not the only thing left.

---

## WHAT I ESTABLISHED AND HOW

measured (this container, 2026-09-27, `tools/dleq_helper/dleq_helper` against
`go-dleq v0.0.0-20230113214619-d6fd7c03e213`, which the running binary reports
itself): the DLEQ's two claimed keys are `x * G` on secp256k1 and `x * B` on
ed25519 -- the STANDARD basepoints, not the alternate generators -- so they are
directly the adaptor point and the Monero spend-share public key with no
conversion. `commitment_secp256k1` equals
`adaptor_ecdsa.point_to_bytes(public_key_point(x))` and `commitment_ed25519`
equals both `ed25519_group.scalar_base_mul(x).compress()` and
`monero_keys.public_key_for_share(x)`, byte for byte.

read from source, and it is why the above is true: `go-dleq`'s `prove.go` sets
`XA = curveA.ScalarBaseMul(xA)` and commits each bit as
`b_i*ScalarBaseMul + r_i*AltBasePoint()`. So the VALUE generator is the
basepoint and the BLINDER generator is the alternate one -- the opposite
assignment to the one `docs/dleq_cross_curve_design.md` section 1.2's notation
suggests, where `G'` carries the value. The arithmetic is the same either way;
the consequence is that the claimed keys are usable as-is.

measured: the round trip DLEQ -> `pre_sign` -> `adapt` -> `recover_adaptor_secret`
returns the witness EXACTLY over 12 of 12 random trials with random signing keys
and messages, and the recovered scalar's ed25519 public key equals the
DLEQ-proven commitment in 12 of 12. The low-S negation branch of
`recover_adaptor_secret` fired in 6 of those 12.

measured: 961 of 2000 random share pairs from `[1, 2^252)` sum to at least `l`
(48.0%), and for one such pair the secp256k1 sum point and the ed25519 sum point
commit to different integers with no error raised anywhere.

measured: the helper proves in 0.396s and verifies in 0.318s for one 64,967-byte
proof, i.e. **0.327µfn (0.396s) and 0.263µfn (0.318s)**. `docs/dleq_cross_curve_design.md`
section 2.3 PROJECTED 1.91µfn (2.31s) and 2.18µfn (2.64s) for a pure-Python
implementation; these are the Go helper, so they are not a refutation of that
projection, they are the cost of the path the design document chose instead.

measured, by reading `modules/atomic_htlc_scripts.build_htlc_redeem_script()`:
both branches of this repo's HTLC end in a single-key `OP_CHECKSIG`, and
`htlc_spend.hashlock_script_sig()` takes a signature the claimer produced with
their own key. So the existing HTLC cannot host an adaptor signature, and the
five-transaction 2-of-2 chain in section 2 is a genuinely missing fifth
component.

measured on LITECOIN, 2026-09-28: section 2's five transactions were built by
`swap_terminal/modules/adaptor_swap_chain.py` and driven against Litecoin Core
0.21.4 regtest by `adaptor_regtest_verify.py`. 40 checks OK, 0 FAIL. The 2-of-2
P2SH funds, spends with both signatures in key order, and is refused both
transposed and without the OP_0 dummy; the cancel's `nLockTime` is refused
before T1 by relay and by consensus and accepted at T1; the cancel output's own
2-of-2 spends both ways. The txid predicted before broadcast equaled the
daemon's for every transaction in the chain, which is the property step 0
requires and the one `sendtoaddress` cannot provide.

unverified, and it belongs to the operator: the same on GRIDCOIN. Whether its
consensus accepts `OP_CHECKMULTISIG` inside a P2SH redeem script and enforces
`nLockTime` on a spend of one is a SOURCE READING of `src/script.cpp` and
nothing more -- see the CORRECTION in section 2, which withdraws the claim that
three completed swaps had settled CLTV there. `adaptor_regtest_verify.py --chain
grc` is written for it and has not been run: there is no `gridcoinresearchd` in
this container. Bitcoin is unverified as well, because Bitcoin Core could not be
downloaded here.

established 2026-09-28 and NOT in section 3, which is a correction to this
document rather than to the code: **a legacy txid depends on the signatures, so
step 0 cannot be one round.** Tx_cancel's txid is not known until Tx_cancel is
fully signed, and Tx_refund and Tx_punish spend its output -- so they cannot be
built, let alone signed, in the same round. The order is: sign and exchange the
cancel signatures, assemble the cancel, agree on its txid, and only then build
and sign the refund and the punish. Measured on LTC regtest the same day: the
unsigned cancel's txid was `5f238d99390bbf7a..` and the signed one was
`2ada30110e598fac..`. The residual hazard is named at
`adaptor_swap_chain.py`'s FINDING 2: either party can assemble a second, equally
valid cancel under a different ECDSA nonce, which orphans BOTH the refund and
the punish. Nobody gains a coin by it -- the defector loses their own path too --
so it is mutual destruction rather than theft, and it is structural to legacy
script.

unverified: everything about a live X-chain leg. There is no `monerod` and no
`monero-wallet-rpc` in this container (`command -v` finds neither), so no part of
step 2, step 3's gating observation, or step 5's sweep has been run. The
`locked=True` and 10-block-unlock facts in step 2 are the operator's
measurements from 2026-09-27, carried here, not re-measured.

CORRECTION TO A CLAIM MADE WHILE THIS WORK WAS BRIEFED: the shared 2-of-2 key
was swept on **REGTEST**, txid `f584606948f430bf...`, 738,723,841,921,372 atomic
units, which is what `chains/monero_keys.py`'s header records and what
`docs/monero_stagenet_funding.md` records. A stagenet sweep with txid
`6d8beca05bd15e36...` and 4,969,540,000 atomic units was described to me as
having also happened; **that txid and that figure appear zero times anywhere in
this tree**, and `docs/monero_stagenet_funding.md` says in its own words that
the regtest result "does NOT settle anything about stagenet specifically". So
the evidence for the shared key is regtest, it is strong, and it is not
stagenet.
