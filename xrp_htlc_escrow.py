#!/usr/bin/env python3
"""Prove -- or refute -- an HTLC on the XRP Ledger, using Escrow and a crypto-condition.

Role: file (the entry point; the decisions live in
      swap_terminal/chains/xrp_crypto_condition.py and the RPC in
      chains/xrp_testnet.py)
Reads: the XRPL TESTNET JSON-RPC endpoint in chains/xrp_testnet.py, and the
       faucet accounts in ~/.config/swap_terminal/keys/xrp-testnet-*.json
Writes: nothing on disk. It SUBMITS transactions -- EscrowCreate, EscrowFinish,
       EscrowCancel -- on the XRP testnet, and only with --run.
Can move funds: YES, testnet XRP between two faucet accounts the operator
       already holds. Never mainnet: refuse_mainnet() asks the server for its
       network_id and aborts on a mainnet one, before anything is submitted.
Mainnet-safe: NO, AND IT REFUSES TO BE ASKED, the same unconditional shape
       regtest_htlc_verify.py uses. The endpoint is not a flag.

WHAT THE FIRST RUN ESTABLISHED, AND WHAT IT REFUTED.

Written in a container that cannot reach the XRP testnet -- both
s.altnet.rippletest.net:51234 and faucet.altnet.rippletest.net are refused by
its egress proxy with 403 on CONNECT, measured 2026-09-26 -- so every claim here
about what XRPL would DO started as a reading of the protocol rather than a
measurement (rule 17).

The operator ran it the same day, and steps 1 to 3 passed: network_id 1 on build
3.4.1, two faucet accounts found, the condition built and self-checked, and the
360-drop EscrowFinish fee computed. STEP 4 THEN FAILED, and not on anything to
do with escrow:

    submitting EscrowCreate: Fee=(autofilled) drops
    EscrowCreate -> notSupported: Signing is not supported by this server.

This file's own header had said rippled's legacy server-side `submit` "may" be
allowed on a public testnet server. It is not, on that server. The fallback is
to sign locally with xrpl-py -- which is what xrp_send_tagged.py already did for
the real XRP payment that went out earlier the same day, so the capability was
in the tree and this file could not reach it. Both now go through
chains/xrp_submit.Submitter, which probes server-side signing once, announces
the refusal, and signs locally from then on.

The run also exposed a reporting defect worth more than the fix: the failed
check printed `got=(none)`, because it read `engine_result`, which is ABSENT when
the transaction never reaches the ledger. The actual diagnosis sat one line
above in the submit log. `(none)` for an empty result is right (rule 14) and it
hid the answer anyway. Every assertion now goes through describe_result(), which
falls back to `error` and `error_message`.

ALL NINE STEPS PASSED ON 2026-09-26, SECOND RUN. The hashlock and the timelock
on the XRP Ledger are MEASURED now, not read from the protocol. Both branches of
a funded escrow were exercised against testnet build 3.4.1, OK=11 FAIL=0:

    step 4  EscrowCreate [A]   tesSUCCESS, validated 9D3A81C4946F26E2...
                               OfferSequence 21051271
    step 5  wrong fulfillment  tecCRYPTOCONDITION_ERROR: "Malformed, invalid,
                               or mismatched conditional or fulfillment."
                               <- THE HASHLOCK ACTUALLY LOCKS
    step 6  right fulfillment  tesSUCCESS, validated E7AC258695A58136...,
                               destination 115000000 -> 116000000 drops,
                               +1000000 EXACTLY
    step 7  EscrowCreate [B]   tesSUCCESS, validated DDD2CE2A37A830AE...
    step 8  early cancel       tecNO_PERMISSION: "No permission to perform
                               requested operation."
                               <- THE TIMELOCK ACTUALLY LOCKS
    step 9  cancel after it    tesSUCCESS, validated AE73C8727D87609A...,
                               sender 82999200 -> 83999180 drops, +999980 =
                               the escrowed 1000000 less 20 drops of fees

Step 5 is the one that makes the rest mean anything. An escrow that accepted any
fulfillment would have produced an identical green step 6, and the refusal code
names the mechanism rather than a generic failure. Step 8's tecNO_PERMISSION is
its counterpart for the timelock: the escrow existed, was cancellable by
construction, and the ledger refused because the time had not come.

Step 6 asserts the BALANCE, not the engine result. tesSUCCESS says a transaction
applied; +1000000 drops at the destination says the escrow paid out, which is the
same distinction regtest_htlc_verify.py draws between a broadcast redeem and a
spend read back off the chain.

WHAT THIS DOES AND DOES NOT ESTABLISH ABOUT A CROSS-CHAIN SWAP.

It establishes that both HTLC primitives exist and work on both sides: BTC and
LTC have a script whose hashlock and timelock branches both spend through real
code (regtest_htlc_verify.py, OK=38 on each chain), and XRPL has an escrow whose
condition and CancelAfter both hold. They commit to the SAME sha256, so one
preimage opens both.

It does NOT establish a swap, and three things stand between here and one:

  1. NOTHING WIRES ESCROW INTO THE SWAP FLOW. chains/xrp.py still does
     Payment-with-a-DestinationTag, which is the custodial path that moved real
     money on 2026-09-26. Making escrow the deposit mechanism changes what gets
     traded and how, which is live posture and the operator's call (rule 16).
  2. THE TIMELOCKS MUST BE ORDERED, and the XRP leg's clock is a different KIND.
     modules/htlc_timelock.py already expresses the policy -- INITIATOR_LOCK_HOURS
     48 against PARTICIPANT_LOCK_HOURS 24, a 2:1 ratio -- but it converts hours
     into BLOCKS for BTC and LTC, and XRPL's CancelAfter is a wall-clock instant.
     A swap whose participant leg expires after its initiator leg lets the
     initiator take one side and refund the other, so whichever code derives the
     XRP CancelAfter must read lock_hours_for_role() rather than the 90-second
     demonstration value this harness uses. That derivation does not exist yet
     and is deliberately not invented here.
  3. WHO REVEALS FIRST is a sequencing decision, not an encoding one. The
     preimage becomes public the instant either leg is claimed -- step 6's own
     output says so -- and revealing before the counterparty's leg is funded and
     confirmed gives the swap away for nothing.

WHY XRPL CAN DO THIS AT ALL, WHICH IS NOT OBVIOUS.

There is no Bitcoin script on the XRP Ledger, so the redeem script
modules/atomic_htlc_scripts.build_htlc_redeem_script() builds cannot be
expressed. XRPL's Escrow object carries the two fields separately instead:

    Condition     a PREIMAGE-SHA-256 crypto-condition. An EscrowFinish is
                  rejected unless it carries a Fulfillment whose SHA-256
                  matches it. THE HASHLOCK.
    CancelAfter   a time after which anybody may EscrowCancel and the XRP
                  returns to the sender. THE TIMELOCK.

And the two legs interlock, which is the whole reason a cross-chain swap is
possible: the condition commits to sha256(preimage), and so does `OP_SHA256
<hash> OP_EQUALVERIFY` in the BTC and LTC redeem script. ONE preimage opens both
sides. tests/test_xrp_crypto_condition.py asserts that the digest in the
condition is the identical sha256 the scripts hash, because if either leg ever
moved to HASH160 the swap would look complete on each chain separately and the
preimage revealed on one would not open the other.

WHAT NINE STEPS ARE FOR. Same shape as regtest_htlc_verify.py, deliberately
(rule 11: one vocabulary), because the questions are the same ones:

    1  the endpoint is a test network, and it says which
    2  two funded faucet accounts exist on this machine
    3  a 32-byte secret and its condition (the condition is printed; the
       secret never is)
    4  EscrowCreate [A] with Condition and CancelAfter
    5  EscrowFinish with a WRONG fulfillment must be REFUSED -- the hashlock
       actually locking, rather than the escrow merely existing
    6  EscrowFinish with the RIGHT fulfillment must SUCCEED, and the
       destination's balance must rise
    7  EscrowCreate [B], a second escrow for the refund path -- never the one
       step 6 already finished
    8  EscrowCancel BEFORE CancelAfter must be REFUSED
    9  EscrowCancel AFTER CancelAfter must SUCCEED, and the sender's balance
       must come back

Step 5 is the one that makes this more than a demo. An escrow that pays out to
any fulfillment is not a hashlock, and an escrow that pays out with no
fulfillment at all is not even an escrow -- both would produce a green step 6.

THE FEE ON AN EscrowFinish IS NOT THE DEFAULT, and this is the detail most
likely to produce a confusing failure. XRPL charges a finish carrying a
fulfillment 330 drops plus 10 drops per 16 bytes of it, so rippled's autofill --
which fills the ordinary 10-drop reference fee -- produces telINSUF_FEE_P. The
fee is therefore set explicitly here and the arithmetic is shown on screen.

HOW TO RUN IT

    cd <repo>
    source .venv/bin/activate
    python3 xrp_htlc_escrow.py            # describes every step, submits nothing
    python3 xrp_htlc_escrow.py --run      # submits, on testnet

It needs TWO faucet accounts in ~/.config/swap_terminal/keys/. fund_testnets.py
makes them. Step 9 waits for CancelAfter to pass, so a run takes at least
--cancel-after seconds of wall clock; the default keeps that under two minutes
and the step says how long it is waiting and why.

THE SECRET IS NEVER PRINTED. Not the preimage, not the fulfillment that contains
it, not at any verbosity. The condition and its sha256 are printed, because both
are public by construction -- the condition goes on a public ledger in step 4.
The same rule modules/htlc_spend.py follows for a scriptSig.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
APP_ROOT = REPO_ROOT / "swap_terminal"
if str(APP_ROOT) not in sys.path:
    # Same rootless-import gap regtest_htlc_verify.py documents: the application
    # imports its own modules as `from chains... import`, so swap_terminal/ has
    # to be importable. CLAUDE.md rule 10's layout debt, not a new one.
    sys.path.insert(0, str(APP_ROOT))

from chains.xrp_crypto_condition import (  # noqa: E402 -- the sys.path line above must run first
    HTLC_PREIMAGE_BYTES,
    condition_matches_preimage,
    preimage_condition,
    preimage_fulfillment,
)
from chains.xrp_escrow import escrow_cancel_tx  # noqa: E402 -- same. MOVED 2026-09-30
from chains.xrp_submit import LocalSigningUnavailable, Submitter  # noqa: E402 -- same
from chains.xrp_testnet import TESTNET_URL, refuse_mainnet, rpc, saved_faucet_accounts  # noqa: E402 -- same
from chains.xrp_units import (  # noqa: E402 -- same. Re-exported from here: see the note below and rule 8.
    RIPPLE_EPOCH_OFFSET_SECONDS,
    ripple_time,
)
from microfortnights import format_duration  # noqa: E402 -- same

# The Console moved to swap_terminal/step_console.py on 2026-09-26, when
# grc_htlc_verify.py became the third verifier wanting the same output
# conventions. Two copies of rule 6's unit and rule 14's `(none)` would drift
# and each would look right in its own file (rule 8).
#
# BOTH NAMES, and which one a function declares is decided by reading its body.
# Console is what main() builds: it owns the step count, the banner and the summary,
# and wait_validated() reads its stopwatch. StepNarrator is the one-method protocol
# submit() declares, because saying two lines is all submit() does with a console --
# naming the concrete class there promises six members to a reader and refuses every
# test recorder that implements the one. 19 pyright errors on 2026-10-09 were that
# shape. step_console.StepNarrator's docstring carries the reasoning and the measured
# reason its parameters are positional-only.
from step_console import Console, StepNarrator, StepSession  # noqa: E402 -- same

# THE EPOCH OFFSET AND ITS TWO CONVERSIONS NOW LIVE IN chains/xrp_units.py,
# imported above beside the drop conversions. They moved there 2026-09-29 so
# xrp_balances.py -- read-only, and forbidden by its own tests from calling
# anything that submits -- could convert a CancelAfter without importing this
# file, which submits escrows. This module re-exports both names because
# atomic_swap_xrp.py and two test files import them from here; that is one
# definition imported twice, not two definitions (rule 8).

# What each escrow locks. Small on purpose: two faucet accounts hold 100 XRP
# each and XRPL's base reserve must stay free, so this is a demonstration
# amount rather than a stress test.
ESCROW_DROPS = 1_000_000  # 1 XRP

# Two accounts, and the reason each is needed: one to send the escrow and one to
# be its destination. Named rather than inline so the assertion in step 2 and the
# message that explains it cannot disagree about the number.
ACCOUNTS_NEEDED = 2

# EscrowFinish's fee floor, from the protocol: 330 drops plus 10 per 16 bytes of
# fulfillment. Computed rather than spelled, and printed, because autofill gets
# this wrong and telINSUF_FEE_P does not say which of the two numbers is short.
FINISH_BASE_FEE_DROPS = 330
FINISH_FEE_DROPS_PER_16_BYTES = 10

# How long to wait for a submitted transaction to appear in a validated ledger.
# Testnet ledgers close in roughly 4 seconds, so this is many ledgers' worth --
# a timeout here means the transaction did not make it, not that it was slow.
VALIDATION_TIMEOUT_SECONDS = 60.0
VALIDATION_POLL_SECONDS = 2.0


def finish_fee_drops(fulfillment_hex: str) -> int:
    """The minimum fee for an EscrowFinish carrying this fulfillment.

    The fulfillment is counted in BYTES, so the hex string's length is halved
    first -- a fee sized from the hex length would be double, which is harmless,
    and sized from the preimage length instead of the encoded fulfillment would
    be short by the four framing bytes, which is telINSUF_FEE_P.
    """
    size_bytes = len(fulfillment_hex) // 2
    chunks = -(-size_bytes // 16)  # ceiling division; a partial 16 bytes still costs
    return FINISH_BASE_FEE_DROPS + FINISH_FEE_DROPS_PER_16_BYTES * chunks


def escrow_create_tx(sender: str, receiver: str, drops: int, condition: str, cancel_after: int) -> dict:
    """The EscrowCreate payload. EXTRACTED so it can be checked without a ledger.

    These three builders were inline dicts inside main() until 2026-09-26, which
    meant the only way to find out whether a payload was well formed was to
    submit it -- and the operator's first run spent its step 4 learning something
    else entirely. A payload is a decision (rule 10's bottom layer), so it is a
    function, and tests/test_xrp_escrow_payloads.py now parses and signs all
    three through xrpl-py's own model classes offline. A missing required field
    or a mistyped one fails there, for free.

    Amount is a STRING of drops. xrpl-py's model accepts an int and rippled does
    not; passing the int works locally and is rejected by the server, which is a
    failure that only appears where it is expensive.
    """
    return {
        "TransactionType": "EscrowCreate",
        "Account": sender,
        "Destination": receiver,
        "Amount": str(drops),
        "Condition": condition,
        "CancelAfter": cancel_after,
    }


def escrow_finish_tx(  # noqa: PLR0913 -- checked: six, and each one is a field of the transaction. `owner` is separate from `sender` on purpose rather than defaulted to it: an EscrowFinish may be submitted by ANYONE, and Owner is the account that CREATED the escrow. Collapsing them would bake in "the creator finishes it", which is the opposite of how a swap's counterparty claims their leg.
    sender: str,
    owner: str,
    offer_sequence: int,
    *,
    condition: str,
    fulfillment: str,
    fee: int,
) -> dict:
    """The EscrowFinish payload. Fee is explicit -- autofill's 10 drops is too low.

    THE FULFILLMENT IS THE SECRET. Nothing may print this dict.
    """
    return {
        "TransactionType": "EscrowFinish",
        "Account": sender,
        "Owner": owner,
        "OfferSequence": offer_sequence,
        "Condition": condition,
        "Fulfillment": fulfillment,
        "Fee": str(fee),
    }


# escrow_cancel_tx() STOOD HERE AND NOW LIVES IN chains/xrp_escrow.py, imported above.
# It was defined in this entry point, which meant anything else wanting to build a cancel had
# to either import from a nine-step testnet verifier or write a second copy -- and a second
# copy of a payload that moves money is rule 8's bug with a delay on it. It belongs beside
# cancel_verdict(), which decides WHETHER to cancel, and offer_sequence_from(), which supplies
# the one field an account_objects entry does not carry (measured 2026-09-30: absent from all
# four entries the operator's run returned). Rule 10: the decision is the smallest testable
# piece, and a file at the root owns the ORDER of steps rather than their contents.


def submit(console: StepNarrator, submitter, secret: str, tx_json: dict) -> dict:
    """Submit one transaction, through whichever signer this server allows.

    CORRECTED 2026-09-26 after the first run against a real ledger. This used to
    call rippled's legacy server-side `submit` only, on the reasoning that a
    public testnet server "may" permit it. s.altnet.rippletest.net build 3.4.1
    answers `notSupported: Signing is not supported by this server.` and step 4
    of nine died there. chains/xrp_submit.Submitter falls back to signing locally
    with xrpl-py, which is the path xrp_send_tagged.py already used for the real
    payment that went out today.

    It announces the transaction type and the fee BEFORE submitting (rule 14),
    and it NEVER prints tx_json verbatim -- an EscrowFinish's tx_json contains
    the Fulfillment, which IS the secret.
    """
    kind = tx_json.get("TransactionType", "(none)")
    console.say(f"submitting {kind}: Fee={tx_json.get('Fee', '(autofilled)')} drops "
                f"(Fulfillment is never printed)")
    result = submitter(tx_json, secret)
    console.say(f"{kind} -> {describe_result(result)}")
    return result


def describe_result(result: dict) -> str:
    """The engine result and its message, or the error when there is no engine result.

    WHY THIS IS A FUNCTION. The first run printed `got=(none)` for a failed
    EscrowCreate while the real answer -- `notSupported: Signing is not supported
    by this server.` -- sat one line above in the submit log. The check read
    `result.get("engine_result")`, which is absent when the transaction never
    reached the ledger at all, and `(none)` is what rule 14 asks an empty result
    to print. Correct in isolation, and it hid the diagnosis in the one line an
    operator reads. Every assertion below now goes through this instead.
    """
    status = result.get("engine_result") or result.get("error") or "(none)"
    message = (
        result.get("engine_result_message")
        or result.get("error_message")
        or result.get("error_exception")
        or "(no message)"
    )
    return f"{status}: {message}"


def engine_result(result: dict) -> str:
    """Just the code, for comparing against tesSUCCESS or a tec/tem prefix."""
    return str(result.get("engine_result") or result.get("error") or "(none)")


def submitted_sequence(result: dict) -> tuple[int | None, str]:
    """The Sequence the ledger assigned the EscrowCreate just submitted. Returns (sequence, why).

    WHY THIS IS A FUNCTION AND WHY IT REFUSES RATHER THAN DEFAULTING. `OfferSequence` plus
    `Owner` is how the XRP Ledger IDENTIFIES an escrow -- there is no hash to name it by -- so
    every one of steps 5, 6, 8 and 9 is built around this number. main() read it inline at BOTH
    create sites -- `tx_json = created.get("tx_json") or {}` then `tx_json.get("Sequence")` --
    and passed the result straight into escrow_finish_tx() (steps 5 and 6, from escrow [A]) and
    escrow_cancel_tx() (steps 8 and 9, from escrow [B]), whose `offer_sequence` is declared
    `int` in both. A response that carried no Sequence therefore put `"OfferSequence": None`
    into a transaction dict and SUBMITTED it: a malformed field, rejected by the ledger a step
    after the create whose response was the real cause, with nothing on screen joining the two
    -- the "confusing failure later" shape this tree keeps paying for. pyright 1.1.414 flagged
    all four call sites on 2026-10-09.

    THE ONE THING IT CHECKS is that the Sequence is an integer, and `bool` is excluded because
    it is a subclass of `int` and `True` would otherwise sail through as sequence 1 -- naming a
    DIFFERENT escrow of the same owner, which is the mis-cancel chains/xrp_escrow.py's
    offer_sequence_from() carries its own long warning about.

    AND WHAT IT DELIBERATELY DOES NOT CHECK, because that is the difference from
    chains/xrp_escrow.offer_sequence_from() and rule 8 says to name it at both sites. That one
    reads a `tx` LOOKUP of a transaction reached through `PreviousTxnID`, which points at
    whatever LAST modified a ledger entry, so it must verify `TransactionType == "EscrowCreate"`
    before trusting the Sequence. Here the response is the submit reply to a transaction THIS
    process built two lines earlier, so its identity is not in question -- and whether rippled's
    submit reply echoes `TransactionType` back is not established anywhere in this tree, so
    checking it would be a guess that can only fail on the operator's machine (rule 17). The
    note at the other site could not be added from this session: it lives in
    swap_terminal/chains/xrp_escrow.py, which is outside the files this change may touch.
    """
    tx_json = result.get("tx_json") or {}
    sequence = tx_json.get("Sequence")
    if isinstance(sequence, bool) or not isinstance(sequence, int):
        return None, f"no integer Sequence on the submit response (got {sequence!r})"
    return sequence, ""


def create_escrow(console: Console, submitter, secret: str, tx_json: dict, label: str) -> int | None:
    """Submit one EscrowCreate and come back with the number that NAMES the escrow, or None.

    EXTRACTED 2026-10-09, and the lint code that asked for it is the one rule 12 writes about at
    length: adding the Sequence refusal below to each of main()'s two create blocks took main()
    to C901 12 and PLR0911 8, which rule 12 says to answer by extracting the decision rather
    than by raising the ceiling. The two blocks were already near-identical -- submit, check
    accepted, read the sequence, wait for validation -- so this is rule 8's merge as well: one
    copy of "create an escrow and come back with its identity", called twice, where two copies
    would have drifted from the day they were written. main() keeps the ORDER of the nine steps,
    which is what rule 10 says a file at the root is for.

    A None IS A REFUSAL THAT HAS ALREADY BEEN REPORTED. There are two of them and the caller
    does not need to tell them apart -- both mean no further step can be built -- but an
    operator does, so each says on screen which happened and what it costs.

    [A] AND [B] BOTH ANNOUNCE THEIR SEQUENCE NOW. Only [A] did before, and [B] is the escrow
    with drops still locked in it when the run ends: steps 8 and 9 name it by this number, and a
    number that decides two steps belongs on the screen (rule 14).
    """
    created = submit(console, submitter, secret, tx_json)
    if not console.check(f"EscrowCreate {label} accepted", describe_result(created), "tesSUCCESS",
                         engine_result(created) == "tesSUCCESS"):
        return None

    # READ AFTER THE ACCEPTED CHECK, not before it. A create that was refused carries no
    # Sequence either, and reporting the missing field first puts the consequence above the
    # cause -- the same ordering defect this file's header records for `got=(none)`.
    sequence, why = submitted_sequence(created)
    if sequence is None:
        console.check(f"the EscrowCreate {label} Sequence came back", why, "an integer Sequence",
                      ok=False)
        console.say("EscrowFinish and EscrowCancel name an escrow by Owner plus OfferSequence -- there "
                    "is no hash for it -- so none of the remaining steps can be built. STOPPING HERE "
                    "RATHER THAN SUBMITTING: a transaction carrying OfferSequence=None is a malformed "
                    "field, and the rejection would arrive several steps away from its cause.")
        console.say("THE ESCROW ABOVE IS ON THE LEDGER and its drops are locked. Nothing here can "
                    "cancel it without that sequence; its CancelAfter still applies, and "
                    "xrp_balances.py reads the sequence back off the EscrowCreate for this situation.")
        return None

    console.say(f"OfferSequence for the finish/cancel below = {sequence} (the CREATE's Sequence; "
                f"EscrowFinish names the escrow by it, not by a hash)")
    wait_validated(console, (created.get("tx_json") or {}).get("hash", ""))
    return sequence


def wait_validated(console: StepSession, tx_hash: str) -> dict:
    """OVER-PROMISES BY THREE MEMBERS ON PURPOSE, and the alternative was worse.

    This uses `say` and `elapsed`. A protocol of exactly those two would be a FOURTH
    rung beside StepNarrator, StepReporter and StepSession -- and step_console's own
    rule for when to add one is "split a Protocol when a caller needs less AND
    something can supply less". Nothing supplies less here: both production call
    sites pass a full runner console, and no stub in tests/ implements say+elapsed
    and nothing else. A fourth name nothing can use is surface for its own sake.

    Poll until the transaction is in a VALIDATED ledger, or give up saying so.

    engine_result is a PROVISIONAL answer from one server about what it expects
    to happen. tesSUCCESS from `submit` is not the ledger agreeing, and reporting
    it as a completed escrow is the same overclaim regtest_htlc_verify.py's step
    8 exists to avoid on the other chains: it is the mempool's opinion, not the
    chain's.
    """
    deadline = time.monotonic() + VALIDATION_TIMEOUT_SECONDS
    polls = 0
    while time.monotonic() < deadline:
        polls += 1
        found = rpc("tx", {"transaction": tx_hash})
        if found.get("validated"):
            console.say(f"{tx_hash[:16]}... validated after {polls} polls, {console.elapsed()}")
            return found
        console.say(f"waiting for validation: poll {polls}, not yet in a validated ledger")
        time.sleep(VALIDATION_POLL_SECONDS)
    console.say(f"gave up waiting for {tx_hash[:16]}... after "
                f"{format_duration(VALIDATION_TIMEOUT_SECONDS)}")
    return {}


def balance_drops(address: str) -> int:
    """One account's XRP balance in drops, or -1 if the account is not funded."""
    info = rpc("account_info", {"account": address, "ledger_index": "validated"})
    account = info.get("account_data") or {}
    return int(account.get("Balance", -1))


def main() -> int:  # noqa: PLR0915 -- checked: this is the nine-step SEQUENCE, and the decisions it makes are all extracted already (the encoding in chains/xrp_crypto_condition.py, the fee in finish_fee_drops, the clock in ripple_time, the RPC in chains/xrp_testnet.py). What remains is order and reporting, which is what rule 10 says a file at the root is FOR. regtest_htlc_verify.py's run_chain carries the same shape for the same reason. Splitting it would put the steps' order somewhere other than the file named after the thing being verified.
    parser = argparse.ArgumentParser(
        description="Prove an HTLC on the XRP Ledger with Escrow plus a PREIMAGE-SHA-256 condition. "
                    "Testnet only, structurally.",
    )
    parser.add_argument("--run", action="store_true",
                        help="actually submit. Without it, every step is described and nothing is sent")
    parser.add_argument("--cancel-after", type=int, default=90,
                        help="seconds until the escrow's timelock expires (default 90). Step 9 waits it out")
    args = parser.parse_args()

    console = Console()
    console.banner("XRP LEDGER HTLC verification -- Escrow with a PREIMAGE-SHA-256 condition")
    console.say(f"endpoint={TESTNET_URL}")
    console.say(f"mode={'--run: transactions WILL be submitted on testnet' if args.run else 'DRY RUN: nothing is submitted'}")
    console.say("ALL NINE STEPS PASSED against a real ledger on 2026-09-26 (testnet build 3.4.1): the wrong")
    console.say("fulfillment was refused tecCRYPTOCONDITION_ERROR, the right one paid +1000000 drops exactly,")
    console.say("the early cancel was refused tecNO_PERMISSION, and the late one returned the escrow. So the")
    console.say("expectations below are MEASURED, and a failure here is a regression rather than a discovery.")
    console.say("What is still unbuilt is the swap around them -- see this file's header for the three gaps.")

    console.step(1, "the endpoint is a TEST network, and it says which")
    try:
        console.check("network", refuse_mainnet(), "a non-mainnet network_id", True)
    except Exception as error:  # noqa: BLE001 -- checked: there are two failures here and BOTH have to arrive as a labeled FAIL rather than as a traceback. refuse_mainnet() raises RuntimeError when the server says mainnet; requests raises ConnectionError, HTTPError or a timeout when the endpoint cannot be reached at all -- which is what happens in a container whose egress proxy refuses this host, measured 2026-09-26. A bare traceback at step 1 of nine is exactly the round trip this harness exists to avoid (rule 14), and nothing here treats the failure as a pass: it returns non-zero through the summary.
        console.check("network", f"{type(error).__name__}: {error}", "a non-mainnet network_id", False)
        console.say(f"nothing was submitted. If this is a connection failure rather than a mainnet refusal, "
                    f"{TESTNET_URL} is unreachable from this machine -- check egress before reading anything "
                    f"into it. This script cannot run where the ledger cannot be reached -- which is why "
                    f"steps 4 to 9 are still unmeasured; see this file's header.")
        return console.summary()

    console.step(2, "two funded faucet accounts on this machine")
    accounts = saved_faucet_accounts()
    if not console.check("faucet accounts found", len(accounts), f">= {ACCOUNTS_NEEDED}", len(accounts) >= ACCOUNTS_NEEDED):
        console.say(f"fund_testnets.py creates them. {ACCOUNTS_NEEDED} are needed: one sends the escrow, one receives it.")
        return console.summary()
    (_, sender, sender_secret), (_, receiver, _receiver_secret) = accounts[0], accounts[1]
    console.say(f"sender={sender}  receiver={receiver}")
    console.say("NOTE the escrow's DESTINATION is the receiver, but an EscrowFinish may be submitted by "
                "anyone -- it is sent here by the sender, which is why only one secret is needed.")

    console.step(3, f"a {HTLC_PREIMAGE_BYTES}-byte secret and its condition")
    preimage = os.urandom(HTLC_PREIMAGE_BYTES)
    condition = preimage_condition(preimage)
    fulfillment = preimage_fulfillment(preimage)
    console.say(f"condition={condition}")
    console.say("the preimage and the fulfillment are NOT printed, here or anywhere below. The condition is,")
    console.say("because step 4 puts it on a public ledger.")
    console.check("the condition commits to this preimage",
                  condition_matches_preimage(condition, preimage), "True", 
                  condition_matches_preimage(condition, preimage))
    fee = finish_fee_drops(fulfillment)
    console.say(f"EscrowFinish fee = {FINISH_BASE_FEE_DROPS} + {FINISH_FEE_DROPS_PER_16_BYTES} per 16 bytes of a "
                f"{len(fulfillment) // 2}-byte fulfillment = {fee} drops (autofill would send the 10-drop "
                f"reference fee and earn telINSUF_FEE_P)")

    cancel_after = ripple_time(time.time() + args.cancel_after)
    console.say(f"CancelAfter={cancel_after} (RIPPLE seconds, {args.cancel_after}s from now; a Unix timestamp "
                f"here would expire the escrow 30 years ago)")

    if not args.run:
        console.banner("DRY RUN -- nothing was submitted")
        console.say(f"steps 4 to 9 would submit: EscrowCreate {ESCROW_DROPS} drops from {sender} to {receiver} "
                    f"with the condition above; an EscrowFinish with a WRONG fulfillment (must be refused); one "
                    f"with the right one (must succeed); a second EscrowCreate; an EscrowCancel before "
                    f"CancelAfter (must be refused); and one after it (must succeed).")
        console.say("re-run with --run to submit them.")
        return console.summary()

    # ONE submitter for the whole run: it probes server-side signing on the first
    # transaction and switches to local signing permanently on a refusal, saying
    # so once. Probing before each of the six would print the same refusal six
    # times, which is rule 14's other failure -- output that says nothing new.
    submitter = Submitter(console.say)

    # A missing xrpl-py is now a FATAL condition rather than a fallback, since
    # this server will not sign. It is caught around every submit below through
    # this one wrapper, so the operator gets the install line instead of an
    # ImportError traceback at step 4 of nine (rule 14).
    def guarded(tx_json: dict, secret: str) -> dict:
        try:
            return submitter.submit(tx_json, secret)
        except LocalSigningUnavailable as error:
            return {"error": "localSigningUnavailable", "error_message": str(error)}

    submitter_submit = guarded

    before = balance_drops(receiver)
    console.step(4, f"EscrowCreate [A]: {ESCROW_DROPS} drops with the condition and the timelock")
    escrow_sequence = create_escrow(
        console, submitter_submit, sender_secret,
        escrow_create_tx(sender, receiver, ESCROW_DROPS, condition, cancel_after), "[A]")
    if escrow_sequence is None:
        return console.summary()

    console.step(5, "EscrowFinish with a WRONG fulfillment must be REFUSED -- this is the hashlock")
    wrong = preimage_fulfillment(bytes(HTLC_PREIMAGE_BYTES))  # a fulfillment for all-zero bytes
    console.say("submitting a fulfillment for a DIFFERENT preimage. An escrow that accepts this is not a "
                "hashlock, and step 6 would be green either way.")
    refused = submit(console, submitter_submit, sender_secret,
                     escrow_finish_tx(sender, sender, escrow_sequence, condition=condition, fulfillment=wrong, fee=fee))
    console.check("a wrong fulfillment is refused", describe_result(refused),
                  "tecCRYPTOCONDITION_ERROR (any tec/tem is a refusal)",
                  engine_result(refused).startswith(("tec", "tem")))

    console.step(6, "EscrowFinish with the RIGHT fulfillment must SUCCEED")
    finished = submit(console, submitter_submit, sender_secret,
                      escrow_finish_tx(sender, sender, escrow_sequence, condition=condition, fulfillment=fulfillment, fee=fee))
    ok_finish = console.check("the right fulfillment finishes the escrow", describe_result(finished),
                              "tesSUCCESS", engine_result(finished) == "tesSUCCESS")
    if ok_finish:
        wait_validated(console, (finished.get("tx_json") or {}).get("hash", ""))
        after = balance_drops(receiver)
        # THE BALANCE, not the engine result. A txid says a transaction was
        # accepted; only the destination's balance says the escrow paid out --
        # the same distinction regtest_htlc_verify.py draws between a broadcast
        # redeem and a spend read back off the chain.
        console.check("the destination's balance rose by the escrowed amount",
                      f"{before} -> {after} drops (+{after - before})", f"+{ESCROW_DROPS}",
                      after - before == ESCROW_DROPS)
        console.say("the fulfillment is now PUBLIC on the ledger, which is how the other leg of a swap becomes "
                    "claimable. That is the mechanism, not a leak.")

    console.step(7, "EscrowCreate [B]: a second escrow, for the refund path")
    console.say("a dedicated escrow, never the one step 6 finished -- if an early cancel were wrongly accepted, "
                "the object it destroyed must not be the one the last step depends on.")
    refund_cancel_after = ripple_time(time.time() + args.cancel_after)
    sequence_b = create_escrow(
        console, submitter_submit, sender_secret,
        escrow_create_tx(sender, receiver, ESCROW_DROPS, condition, refund_cancel_after), "[B]")
    if sequence_b is None:
        return console.summary()
    sender_before_cancel = balance_drops(sender)

    console.step(8, "EscrowCancel BEFORE CancelAfter must be REFUSED -- this is the timelock")
    early = submit(console, submitter_submit, sender_secret, escrow_cancel_tx(sender, sender, sequence_b))
    console.check("an early cancel is refused", describe_result(early),
                  "tecNO_PERMISSION (any tec/tem is a refusal)",
                  engine_result(early).startswith(("tec", "tem")))

    console.step(9, "EscrowCancel AFTER CancelAfter must SUCCEED")
    # The wait is announced with its reason and its length, because a silent
    # pause here is indistinguishable from a hang and the operator's answer to
    # that is Ctrl-C (rule 14).
    remaining = max(0.0, (refund_cancel_after + RIPPLE_EPOCH_OFFSET_SECONDS) - time.time()) + 5
    console.say(f"waiting {format_duration(remaining)} for the timelock to pass. Nothing is wrong; XRPL compares "
                f"CancelAfter against the LAST CLOSED LEDGER's time, so a few seconds of slack is added.")
    time.sleep(remaining)
    late = submit(console, submitter_submit, sender_secret, escrow_cancel_tx(sender, sender, sequence_b))
    if console.check("a cancel after the timelock succeeds", describe_result(late), "tesSUCCESS",
                     engine_result(late) == "tesSUCCESS"):
        wait_validated(console, (late.get("tx_json") or {}).get("hash", ""))
        sender_after = balance_drops(sender)
        console.check("the sender got the escrowed amount back",
                      f"{sender_before_cancel} -> {sender_after} drops", f"about +{ESCROW_DROPS} less fees",
                      sender_after > sender_before_cancel)

    console.banner("WHICH BRANCH OF A FUNDED ESCROW ACTUALLY PAYS")
    console.say("the hashlock: an escrow finished only by the fulfillment that matches its condition, with a "
                "wrong one refused (steps 5 and 6).")
    console.say("the timelock: an escrow cancelled only after CancelAfter, with an early cancel refused "
                "(steps 8 and 9).")
    # The comparison an operator actually wants, and the honest limit of it. The
    # primitives match; the swap around them does not exist yet (rule 17 -- this
    # says what was established, not what it implies).
    console.say("this matches what regtest_htlc_verify.py establishes for BTC and LTC, on the SAME sha256: one "
                "preimage opens either side. It does NOT mean a swap exists -- nothing wires escrow into the "
                "deposit path, and the XRP leg's CancelAfter is not yet derived from "
                "modules/htlc_timelock.lock_hours_for_role(). See this file's header.")
    return console.summary()


if __name__ == "__main__":
    sys.exit(main())
