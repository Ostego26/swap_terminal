"""Broadcast the payout leg, and track the hot wallet's BALANCE.

THE FIRST LINE SAID "track hot-wallet inventory" UNTIL 2026-10-03, AND
"inventory" WAS THE WRONG WORD FOR WHAT THIS FILE WRITES. refresh_wallet_inventory()
stores `adapter.get_balance()` -- the WHOLE wallet the endpoint serves -- into
`wallet_inventory.hot_confirmed`. On a host where `*_RPC_WALLET` is unset, that is
the operator's own wallet, personal coins included, and none of it was committed to
the desk. Measured on the operator's host 2026-10-03: 3780.08654497 GRC recorded as
desk stock, of which the amount actually committed to the desk was never established
because nothing in the tree could express the distinction (wallet_custody.py now
reports it; docs/hot_wallet_separation_runbook.md says how to separate them).

The column name is left alone deliberately: renaming a table column is a migration
on a live database for a wording change, which is the large-diff-no-benefit trade
rule 10 refuses for layout. The DDL comment in db.py and every surface that PRINTS
the figure now say what it measures, which is where a reader meets it.

Role: submodule -> function (process_pending_payouts is the decision)
Reads: swap_terminal.db (swaps, payouts, wallet_inventory), the destination
       adapter (getbalance), and THE PROCESS ENVIRONMENT for two secrets it
       never stores and never logs -- GRIDCOIN_WALLET_PASSPHRASE (see
       WALLET_UNLOCK_ENV_VAR) and, since 2026-10-02, XRP_PAYOUT_SECRET_SEED
       (see chains/xrp_payout_seed.py, which owns the name)
Writes: swap_terminal.db (payouts, wallet_inventory, swaps, swap_audit_log)
       AND THE CHAIN
Can move funds: YES. `broadcast_payout(...)` on the line inside
       process_pending_payouts' try block is the only broadcast in the Flask
       suite. It is final the instant it is relayed.

       THIS FIELD NAMED `adapters[destination_asset].send_to_address(...)` until
       2026-10-02, which was the call site itself rather than a function. It is
       broadcast_payout() now -- one call site, one function, and the only place
       that decides which keywords a chain's send needs. Corrected rather than
       left: a header naming a line that is no longer there sends a reader
       looking for the broadcast in the wrong place, and this is the one file
       where that matters most.

       SOL JOINED THE CHAINS THIS FUNCTION CAN MOVE ON 2026-10-03 (operator:
       "whoa we have to be able to swap TO SOL too"). It is armed by
       SOL_PAYOUT_KEYPAIR_PATH, which this module never reads and never names
       as a value -- chains/solana_signing.py reads it, inside the one call
       that signs -- and it is refused on devnet's genesis hash alone.

       THIS FIELD SAID "no pair in Config.ALLOWED_PAIRS pays out in SOL as of
       that date (measured), so this branch is reachable only once the operator
       enables one", AND IT IS NOW WRONG. Re-measured 2026-10-03 later the same
       day: ALLOWED_PAIRS carries ("GRC","SOL"), ("BTC","SOL") and
       ("LTC","SOL"), enabled by 79c4808 on the operator's instruction "whoa we
       have to be able to swap TO SOL too". Five destination assets, not four.
       Corrected rather than left, because a header that says a branch is
       unreachable is the sentence a reader trusts before deciding how carefully
       to read it (rule 16: a wrong comment is a bug).
Mainnet-safe: NO -- running this against a funded mainnet wallet is operating
       the payout path, not inspecting it.

TWO WORKERS RUNNING THIS AT ONCE PAID THE SAME SWAP TWICE. FIXED 2026-09-24.

Measured that day in tests/test_payout_concurrency.py with this exact function
against a real database: 2 sends, 1 swap_id, 2 rows in `payouts` both
status='broadcast'. The guard on the `payout_exists` SELECT was correct about
what it asked and was still not enough, because it was a READ that happened
before the writer lock was contended -- the second worker ran it while the
first was inside `sendtoaddress` with its transaction open.

Both halves of the fix are here, and neither is sufficient alone:

  claim_swap_for_payout()   turns the decision into a conditional UPDATE, so
        the writer lock that already existed serializes the DECISION and not
        merely the insert. Exactly one claimant sees rowcount 1.
  idx_payouts_one_live_per_swap   a PARTIAL unique index (db.py), applied by
        apply_migrations(), so a second live payout row is impossible even if
        a future caller forgets to check rowcount -- while a genuinely failed
        payout stays retryable.

The first alone reopens the hole the moment somebody writes a new caller; the
second alone converts a double payout into an unhandled IntegrityError AFTER
the first send has gone out. The same test file now measures 1 send where it
measured 2, and it keeps the demonstrations of each mechanism in isolation.

process_pending_payouts()'s docstring states the commit ordering around the
send and what it costs in the crash case. Read it before reordering anything
there: a broadcast and a commit are two systems and cannot be made atomic.

refresh_wallet_inventory()'s `except Exception: continue` is annotated at its
site: a chain being unreachable must not stop the other two from being
refreshed, and the consequence of swallowing it is stated there.
"""

import logging
import os
import sqlite3
from contextlib import nullcontext

from chains.gridcoin_wallet_lock import (
    WALLET_UNLOCK_ASSETS,
    WALLET_UNLOCK_ENV_VAR,
    GridcoinLockError,
    unlocked_for_payout,
)

# QUANTIZATION BEFORE THE RECORD, NOT AFTER THE SEND. See
# amount_decided_and_logged() for the ledger reading that measured the defect, and
# chains/payout_quantization.py's header for why that module is not
# chains/coin_amounts.py (a measured import cycle, reproduced in both directions).
from chains.payout_quantization import quantize_for_chain
from chains.registry import why_cannot_pay_out, why_unconfigured

# THE ARMING TOKEN ONLY, AND DELIBERATELY NOT chains/solana_payout_keypair.
#
# This module needs no capability check for SOL: broadcast_payout() passes the
# token unconditionally and the keypair path is what arms the send, refused
# inside chains/solana_signing.require_send_confirmation() with the preview
# attached. Importing the presence check here as well would put the same
# question in two places on one path (rule 8), and the copy here would be the
# one that answered without the preview.
from chains.solana_signing import CONFIRM_SOL_SEND
from chains.xrp_payout_seed import SIGNING_SEED_ENV_VAR, signing_seed, signing_seed_is_present
from chains.xrp_signing import CONFIRM_XRP_SEND
from modules.address_authority import check_address

from .helpers import utc_now_iso
from .swap_service import SHARED_ACCOUNT_PAYOUT_ASSETS, payout_source_account, set_swap_status

logger = logging.getLogger(__name__)


def amount_decided_and_logged(swap) -> float:
    """payout_amount(), QUANTIZED TO WHAT THE CHAIN WILL SEND, plus the log lines for both.

    EXTRACTED BECAUSE THE BRANCH PUT process_pending_payouts() PAST C901, and rule
    12 says a function past that ceiling is orchestration that has swallowed a
    decision -- the fix is to extract it, not to raise the ceiling or write a noqa.
    The decision here is "is this payout worth telling the operator about".

    THE REASON IS LOGGED, NOT DISCARDED. ruff caught `amount_reason` unused on the
    first attempt, which is this tree's recurring defect in miniature: a function
    that explains itself and a caller that throws the explanation away.

    LOGGED ONLY WHEN THE FIGURE DIFFERS from the quoted estimate. A line on every
    payout saying "the deposit matched" is noise that trains an operator to skim
    past the one that says it did not, and a difference is the only case where
    anybody later asks why the payout was not the quoted number.

    ==================================================================
    THE QUANTIZATION, ADDED 2026-10-03: THE ROW RECORDED A NUMBER THE
    CHAIN HAD NOT SENT
    ==================================================================

    Measured that day on the completed swap s_539d922e9ef0a5d8 (GRC -> XRP), by
    reading the XRP testnet ledger for the transaction the `payouts` row names:

        payouts.amount    3.3155893288590605 XRP
        the ledger        Amount 3315589 drops = 3.315589 XRP, tesSUCCESS,
                          validated true

    0.00000032885906 XRP of overstatement -- less than one drop, so the ledger
    cannot express the number the database claimed was paid. The same defect on
    the other chains the same day: an LTC payout booked 1.1996736819422498 and
    broadcast 1.19967368, and a GRC payout booked 2701.3495803173805 and
    broadcast 2701.34958031.

    THE CAUSE WAS WHERE THE QUANTIZATION HAPPENED, not whether it happened. Every
    adapter already reduced the figure to its chain's precision -- 8 decimals in
    chains/base.RPCAdapter.send_to_address(), drops in
    chains/xrp.XRPAdapter.preview_payout(), lamports in
    chains/solana.SolanaAdapter.build_transfer_plan() -- and every one of them
    returns only a txid, so this service could not learn what had been sent even
    in principle. The figure it reserved, INSERTed and released was the booked
    one at every step.

    SO IT IS QUANTIZED HERE, ONCE, BEFORE ANYTHING IS WRITTEN. process_pending_
    payouts() uses one local for reserve_inventory(), the payouts INSERT,
    broadcast_payout() and release_inventory_after_send(), so quantizing the
    value this function returns makes all four the chain's own figure. The record
    matches the chain BY CONSTRUCTION rather than by coincidence.

    AND THE BROADCAST AMOUNT DOES NOT MOVE, which is the hard constraint on this
    change. The adapter quantizes AGAIN after this function has, so the only way
    the sent figure could change is if quantizing twice differed from quantizing
    once. It does not, on any of the five chains, measured over 60,016 amounts
    per chain in tests/test_payout_quantization.py rather than argued:
    chains/payout_quantization.quantize_for_chain() returns a FIXED POINT of the
    adapter's own conversion.

    A POSITIVE PAYOUT THAT QUANTIZES TO NOTHING KEEPS THE REQUESTED FIGURE, and
    that branch is the one place this function does not simply take the quantized
    number. 1e-09 BTC is a tenth of a satoshi: it quantizes to 0.0, and passing
    0.0 to the adapter reaches `sendtoaddress(address, 0.0)` -- a daemon error
    about a positive amount, for a figure WE reduced to zero, which is the
    hardest kind of message to trace back. chains/base.py already refuses that
    case before asking the daemon ("fits to 0.0 ... which is nothing"), with the
    chain's precision in the message, and that refusal is reached only by a
    caller that has NOT pre-quantized. So the requested figure goes through and
    the existing refusal fires exactly as it did before this change -- same
    exception, same `payouts` row, same 'failed' status, nothing sent.
    tests/test_coin_amounts.py pins the adapter end of that; a payout of zero is
    deliberately NOT turned into a precision complaint there, and widening the
    adapter's guard to catch it would have broken the decision that test records.
    """
    amount, reason = payout_amount(swap)
    if amount != float(swap["output_amount_estimate"]):
        logger.info("swap %s: payout amount %s differs from the quote -- %s", swap["id"], amount, reason)
    destination_asset = swap["to_asset"]
    quantized, quantization = quantize_for_chain(amount, destination_asset)
    if quantization:
        # Rule 14: two correct numbers that disagree in their last digits is exactly
        # where "state what the number means, next to the number" earns its keep. An
        # operator reconciling this row against a block explorer needs to be told
        # which figure is on the chain, and that the other one is the quote.
        logger.info(
            "swap %s: payout %s -> %s, the figure %s can actually send and the figure that will be "
            "RECORDED -- %s",
            swap["id"], amount, quantized, destination_asset, quantization,
        )
    if quantized <= 0 < amount:
        return amount
    return quantized


def payout_amount(swap) -> tuple[float, str]:
    """What to pay: the QUOTED figure, scaled by how much actually arrived. The decision.

    OPERATOR INSTRUCTION 2026-10-03, on being told the amount was not fully locked:
    "goddamit. we need to fix this."

    WHAT WAS WRONG. The payout was `swap["output_amount_estimate"]` verbatim, which
    services/quote_service.create_quote() computed from the EXPECTED deposit, while
    the fee is charged against what actually arrived. fee_ledger.py derives the
    consequence exactly -- with G the realized gross, E the quoted gross:

        retained = G - paid = G - E*(1-f)
        drift    = retained - G*f = (1-f)*(G - E)

    So any difference between the deposit and the quote was kept (deposit over) or
    given away (deposit under), silently, at (1-f) of it. A customer who sent 0.9%
    less than quoted was paid as though they had sent the full amount.

    THE FIX SCALES THE QUOTE. IT DOES NOT RE-DERIVE IT, and that distinction is the
    whole of this docstring:

        paid = output_amount_estimate * (actual_input / expected_input)

    MY FIRST ATTEMPT RE-DERIVED IT as `actual * quoted_rate * (1 - f)`, which is
    algebraically identical ONLY WHILE the stored estimate follows today's formula.
    tests/test_address_authority.py's fixture caught it: it seeds rate=0.001,
    fee_bps=150, expected=actual=100.0 and output_amount_estimate=0.0975, while
    100 * 0.001 * 0.985 = 0.0985. The stored figure was computed by the SUPERSEDED
    formula that also deducted the network fee reserve (0.0985 - 0.001 = 0.0975),
    which this tree stopped doing on 2026-10-01.

    So re-deriving would have paid 0.0985 on a swap whose customer was quoted
    0.0975 -- changing the amount on a deposit that matched the quote exactly, which
    is the opposite of what was asked for. Scaling cannot do that: with
    actual == expected the ratio is 1 and the quoted figure is paid unchanged, by
    construction, whatever formula produced it and however often that formula
    changes afterwards.

    THE DRIFT STILL GOES TO ZERO. Substituting paid = E_est * G/E into
    fee_ledger.py's identity gives drift = 0 for any E_est, so the realized fee
    equals the schedule at every size -- which is the property the fix is for.

    BOUNDED BY ±AMOUNT_TOLERANCE_PCT, AND THAT IS WHY THIS IS SAFE RATHER THAN
    OPEN-ENDED. A deposit outside the band never reaches 'payout_pending':
    services/deposit_service.py halts it to 'under_review' (measured 2026-10-03 at
    deposit_service.py:695). So the ratio is within 1% of 1.0 today, in either
    direction, and reserve_inventory() reserves against THIS figure because the
    caller uses one local for both.

    RETURNS THE REASON ALONGSIDE, because a payout that differs from the number a
    customer was quoted has to be explainable from the row a year later, and "it was
    recomputed" is not an explanation anybody can audit.

    TWO GUARDS ON THE RATIO, neither of them hypothetical:

      actual_input_amount IS NULL   pays the quoted figure and SAYS SO. A
                                    'payout_pending' swap always has it --
                                    deposit_service writes it with credited_at --
                                    so this is unreachable in the live path and is
                                    recorded rather than defaulted silently.
      expected_input_amount <= 0    pays the quoted figure and says so, because the
                                    ratio is undefined. A swap cannot be created
                                    with a non-positive input, so this is the same
                                    kind of unreachable, and dividing anyway would
                                    turn an impossible row into a ZeroDivisionError
                                    inside the payout loop.
    """
    estimate = float(swap["output_amount_estimate"])
    actual = swap["actual_input_amount"]
    expected = swap["expected_input_amount"]
    if actual is None:
        return estimate, (
            "paid the QUOTED figure because actual_input_amount is NULL, which should be impossible "
            "for a payout_pending swap -- deposit_service writes it with credited_at. Investigate: "
            "this payout was priced on the expected deposit, not the real one"
        )
    actual = float(actual)
    expected = float(expected or 0.0)
    if expected <= 0:
        return estimate, (
            f"paid the QUOTED figure because expected_input_amount is {expected}, so the ratio to the "
            f"real deposit is undefined. A swap cannot be created with a non-positive input; "
            f"investigate this row"
        )
    if actual == expected:
        return estimate, f"the deposit matched the quote exactly ({actual}), so the quoted figure is paid"
    scaled = max(estimate * (actual / expected), 0.0)
    return scaled, (
        f"SCALED from the deposit that arrived: expected {expected}, actual {actual}, ratio "
        f"{actual / expected}. Quoted {estimate} -> paid {scaled}. The quoted RATE and the quoted "
        f"FIGURE are both honored; only the quantity is adjusted. Before 2026-10-03 the quoted figure "
        f"was paid verbatim and this difference was kept or given away"
    )


def reserve_inventory(db, asset: str, amount: float):
    row = db.execute("SELECT * FROM wallet_inventory WHERE asset = ?", (asset,)).fetchone()
    now = utc_now_iso()
    if row is None:
        db.execute(
            "INSERT INTO wallet_inventory (asset, hot_confirmed, hot_reserved, hot_available, updated_at) VALUES (?, ?, ?, ?, ?)",
            (asset, 0.0, amount, -amount, now),
        )
        return
    db.execute(
        "UPDATE wallet_inventory SET hot_reserved = ?, hot_available = ?, updated_at = ? WHERE asset = ?",
        (float(row["hot_reserved"]) + amount, float(row["hot_available"]) - amount, now, asset),
    )


def release_inventory_after_send(db, asset: str, amount: float):
    """Release the reservation a sent payout was holding. Does NOT touch hot_confirmed.

    IT USED TO DEBIT hot_confirmed AND THAT DROVE XRP NEGATIVE, TWICE, MEASURED ON
    THE OPERATOR'S HOST. The line was:

        float(row["hot_confirmed"]) - amount,     # beside a FLOORED hot_reserved

    and the asymmetry was the tell: hot_reserved was clamped with max(..., 0.0)
    and hot_confirmed was not.

    WHY ONLY XRP, AND IT IS NOT A COINCIDENCE -- it is the whole diagnosis.
    refresh_wallet_inventory() below OWNS hot_confirmed: it writes
    `hot_confirmed = adapter.get_balance()` for every chain every 60s. So on
    BTC/LTC/GRC/SOL this function's debit was overwritten by a measured balance
    within a minute and was invisible. XRP's get_balance() REFUSES BY DESIGN --
    chains/xrp.py holds no account of its own -- so refresh_wallet_inventory()
    hits its `except` and, in its own words, "the row for that asset keeps its
    previous values". Nothing ever corrected XRP, so every payout drove the column
    further down with no floor and no corrector.

    THE EVIDENCE, both readings exact rather than approximate:

        2026-10-04 morning   hot_confirmed = -59.231412662192405, which is exactly
                             the negated SUM of XRP's two payouts' pre-quantization
                             amounts. repair_inventory_reservations.py DELETED the
                             row rather than zeroing it, discriminating on < 0.
        2026-10-04 20:52     hot_confirmed = -5.63518500 again, exactly the negated
                             amount of payouts.id=30 -- the single XRP payout made
                             after that repair. One payout, one re-break.

    SO THE FIX IS ONE AUTHORITY PER COLUMN (rules 8 and 15), not a floor. Clamping
    at zero would have stopped the negative number and kept the second writer: the
    column would then read 0 for an asset whose balance nobody measured, which is
    the "a stale balance is not a small balance -- it is a number nobody has
    checked" distinction the admin page makes, inverted. hot_reserved is THIS
    function's to release, because reserve_inventory() is what took it. The
    measured balance belongs to the poller alone.

    hot_available IS RECOMPUTED rather than carried, because it is derived:
    refresh_wallet_inventory() defines it as `balance - reserved`, and this
    function changes `reserved`. Writing the old value back -- which it did, as
    `float(row["hot_available"])` -- left it describing a reservation that no
    longer existed, so the three columns disagreed until the next poll. On XRP,
    where there is no next poll, they disagreed permanently: -61.37835700 against
    a hot_confirmed of -5.63518500 and a reserved of 55.74317200.

    A ROW THAT DOES NOT EXIST IS STILL NOT CREATED HERE. The early return is
    unchanged: an inventory row is the poller's to create, and inventing one from
    a send would be this function guessing a balance again by another route.
    """
    row = db.execute("SELECT * FROM wallet_inventory WHERE asset = ?", (asset,)).fetchone()
    if not row:
        return
    reserved = max(float(row["hot_reserved"]) - amount, 0.0)
    db.execute(
        "UPDATE wallet_inventory SET hot_reserved = ?, hot_available = ?, updated_at = ? WHERE asset = ?",
        (reserved, float(row["hot_confirmed"]) - reserved, utc_now_iso(), asset),
    )


def claim_swap_for_payout(db, swap_id: str) -> bool:
    """Take exclusive ownership of one swap's payout, or decline.

    This is THE decision, and it is a WRITE rather than a read on purpose.

    The guard it replaced was

        SELECT * FROM payouts WHERE swap_id = ? AND status IN ('broadcast','completed')

    which asks the right question and is still not enough. Measured 2026-09-24
    in tests/test_payout_concurrency.py against a real database: two workers,
    2 sends, 1 swap_id, 2 rows in `payouts` both status='broadcast'. SQLite has
    one writer lock, so the second worker's INSERT really did block behind the
    first -- but the SELECT completed long before the lock was ever contended.
    Worker B ran it while A was inside `sendtoaddress` with its transaction
    open, saw no payout row, DECIDED to pay, and only then blocked. The lock
    serialized the writes and did nothing about the decision.

    A conditional UPDATE moves the decision into the write, where the lock
    already applies: the row is re-read under the writer lock, and exactly one
    of two claimants can see `status = 'payout_pending'`. The loser updates
    zero rows and must decline -- which is what `rowcount == 1` means here and
    why the caller checks it rather than assuming success.

    The claim is COMMITTED before the caller sends anything. That is
    deliberate and it is not merely about durability: if the claim stayed in an
    open transaction for the duration of the RPC call, a second worker's
    competing UPDATE would block on the writer lock for as long as the send
    took, and sqlite3's default 5-second busy timeout would turn a slow but
    perfectly healthy `sendtoaddress` into "database is locked" in the other
    worker. Committing first makes the loser's UPDATE return rowcount 0
    immediately instead of waiting.

    THE SAME SHAPE AS swap_service.set_swap_status() SINCE 2026-10-02, and the difference is
    stated at both sites (rule 8). That function is now also a conditional UPDATE whose audit
    row is written only when the UPDATE applied, and this docstring's argument is the one it
    cites. Two things keep them separate rather than merged: this one COMMITS, because a claim
    left in an open transaction blocks the other worker's competing UPDATE for the length of
    an RPC call, and this return value is a claim of OWNERSHIP the caller must act on, where
    that one reports whether a status moved.

    Returns:
        True if this caller owns the payout for `swap_id` and may send.
    """
    claim = db.execute(
        "UPDATE swaps SET status = 'paying', updated_at = ? WHERE id = ? AND status = 'payout_pending'",
        (utc_now_iso(), swap_id),
    )
    if claim.rowcount != 1:
        db.commit()
        return False
    db.execute(
        "INSERT INTO swap_audit_log (swap_id, old_status, new_status, message, created_at) VALUES (?, ?, ?, ?, ?)",
        (swap_id, "payout_pending", "paying", "Claimed for payout", utc_now_iso()),
    )
    db.commit()
    return True


def refuse_payout_before_sending(db, swap: dict, verdict, destination_asset: str, amount) -> None:
    """Write and announce the refusal of a payout address. Sends nothing, reserves nothing.

    EXTRACTED 2026-10-02 WHEN THE STATUS WRITE BECAME A COMPARE-AND-SWAP, and the reason is
    rule 12's C901 note rather than tidiness: checking the three set_swap_status() return
    values put process_pending_payouts() past the complexity and statement ceilings, and the
    fix for orchestration that has swallowed a decision is to extract the decision -- "what
    happens to a swap whose payout address we refuse" -- not to raise the ceiling or write a
    noqa (rule 19). It is also the shape rule 10 asks for: the loop above is orchestration,
    this is the outcome, and it can now be called with a seeded verdict.

    THE ORDER IS LOAD-BEARING AND IS UNCHANGED. failed_reason first, then the status, then the
    commit, then the line the operator reads. The reason has to be on the row before the status
    says 'failed', or a reader who catches the swap between the two sees a failure with no
    explanation.
    """
    db.execute(
        "UPDATE swaps SET failed_reason = ?, updated_at = ? WHERE id = ?",
        (f"payout address refused before send: {verdict.why}", utc_now_iso(), swap["id"]),
    )
    if not set_swap_status(
        db, swap["id"], "failed",
        f"NOTHING WAS SENT. Payout address refused: {verdict.why}", old_status="paying",
    ):
        # THE CAS DECLINED, WHICH HERE IS AN ANOMALY RATHER THAN A RACE. This caller holds an
        # exclusive claim on the swap -- claim_swap_for_payout() moved it into 'paying' and
        # committed -- and since 2026-10-02 nothing else can overwrite 'paying', because every
        # other writer of swaps.status goes through the same compare-and-swap. So reaching
        # this line means something moved the swap out from under a committed claim, and the
        # operator needs the sentence rather than a silent skip (rule 14).
        #
        # IT DOES NOT RAISE. Nothing was sent, no inventory was reserved and no payouts row
        # was written, so there is nothing to undo; raising would turn one anomalous swap into
        # a FAILED cycle that stops every OTHER swap being paid.
        logger.error(
            "swap %s: the payout address was REFUSED and NOTHING WAS SENT, but the swap could "
            "not be marked failed -- it is no longer 'paying', so another process moved it out "
            "of a committed payout claim. The refusal reason IS recorded in "
            "swaps.failed_reason; the status and the audit row are NOT. Read it back with "
            "show_swap.py --swap %s before retrying anything.",
            swap["id"], swap["id"],
        )
    db.commit()
    # Rule 14: the refusal names the address AND the reason, on the screen the operator is
    # actually looking at. `swaps.failed_reason` reached nothing they were watching on
    # 2026-09-26, which is the measurement recorded at the send-failure logger.error() inside
    # process_pending_payouts().
    logger.error(
        "payout REFUSED BEFORE SENDING for swap %s (%s -> %s, %s %s): address %r is not a valid "
        "%s address -- %s  <- NOTHING was sent, no inventory was reserved, no payouts row was "
        "written, and the swap is now 'failed' and will NOT be retried. Money sent to this string "
        "would be unspendable by anybody.",
        swap["id"], swap["from_asset"], swap["to_asset"], amount, destination_asset,
        swap["payout_address"], destination_asset, verdict.why,
    )


class PayoutSigningUnavailable(RuntimeError):
    """This process cannot sign for that chain, so no send was attempted.

    Its own type for the reason PayoutUnlockUnavailable below has one: it is
    categorically different from a send that FAILED. No transaction was created,
    nothing reached any server, no fee was claimed, and the remedy is an
    environment variable rather than an investigation.
    """


def broadcast_payout(adapter, asset: str, config, address: str, amount: float) -> str:
    """Send one payout on `asset`, with whatever that chain's send actually needs. Returns the txid.

    THE DECISION THIS HOLDS is "which keywords does this chain's send require", and
    it is extracted into a function rather than written inline in
    process_pending_payouts() for rule 10's reason: it is the thing that decides
    whether money moves, so it has to be callable with seeded inputs and asserted on
    directly. Inline, the only way to test it would be to run the loop against a
    database.

    WHAT IT REPLACED, AND WHAT THAT COST. Until 2026-10-02 the call site was

        txid = adapter.send_to_address(swap["payout_address"], amount)

    two positional arguments for every chain. For BTC, LTC and GRC that is the whole
    of the send: the daemon holds the wallet, picks the inputs and signs. For XRP it
    is a REFUSAL -- chains/xrp.py previews by default and requires an arming token at
    the call site -- so every XRP payout raised XRPSendNotArmed and the swap landed
    in `failed` WITH THE CUSTOMER'S DEPOSIT ALREADY CREDITED. The customer page said
    so, in those words, and the operator asked for the opposite: "we should be able
    to swap any coin for another of any combination."

    TWO BLOCKERS WERE CLAIMED AND ONLY ONE WAS THE REAL ONE. The page's sentence read
    "it holds no signing key, AND services/payout_service.py calls send_to_address()
    without the arming token". Both were true; they were different problems. Arming
    this call site alone would have changed XRPSendNotArmed into XRPSendNotArmed's
    second branch ("armed but no signing seed was supplied"), because there was no
    environment variable, no Config field and no path of any kind by which a seed
    could reach the adapter. chains/xrp_payout_seed.py is what was actually missing.

    THE REFUSALS HERE COME BEFORE ANY NETWORK CALL, which is the ordering rule 14
    asks for: a host missing a variable learns so immediately rather than after two
    round trips to a rippled server. Both are named, both say which variable, and
    neither creates a transaction. Everything after them is the adapter's own guard
    sequence, which runs in this order and is documented at
    chains/xrp.py::preview_payout():

        local    the destination is not an X-address; its checksum decodes; the
                 source decodes; the amount is more than zero drops
        network  server_info, and xrp_signing.require_non_mainnet() refuses network
                 id 0, a MISSING id and an unreadable id -- decided from what the
                 SERVER reports and never from the url
        network  account_info on the SOURCE, then the reserve arithmetic in integer
                 drops
        armed    the exact token from this function, and a non-empty seed
        key      derive_and_check(): the seed's own account must equal the source
                 this run announced, or nothing is signed
        tx       refuse_partial_payment() on the SERIALIZED transaction
        ledger   submit_and_wait(), then tesSUCCESS AND validated AND a hash

    THE SEED IS NEVER BOUND TO A NAME IN THIS FUNCTION. signing_seed() is called in
    the argument list and its value is consumed by send_to_address() immediately: it
    is never a local, never a dict value, never part of this function's return, and
    never interpolated into any message raised from here. The pre-flight check uses
    signing_seed_is_present(), which returns a bool. tests/test_xrp_payout_wiring.py
    captures every log record at DEBUG -- including repr(record.args), where a value
    survives whether or not a handler formats the message -- and asserts the seed is
    in none of them, in no exception string, and in no return value.

    SOL WAS ADDED 2026-10-03 AND IT IS A THIRD SHAPE, NOT A SECOND COPY OF XRP'S.
    Operator: "whoa we have to be able to swap TO SOL too". Until that instruction
    SOL fell through to the two-positional branch below, so every SOL payout raised
    SolanaSendNotArmed no matter what the host had exported -- the same stranded-swap
    shape XRP had, one chain over. What SOL's send needs is the arming token and
    NOTHING ELSE: no source account (the payer is SOL_HOT_WALLET, which the adapter
    already holds and the preview announces) and no key (chains/solana.py holds
    none; chains/solana_signing.signed_transfer_wire() reads the keypair file from
    SOL_PAYOUT_KEYPAIR_PATH for the duration of one call).

    AND IT DIVERGES FROM XRP ON ONE POINT, WHICH IS THE PRE-FLIGHT CHECK. XRP refuses
    HERE, before any network call, when its seed variable is unset. SOL deliberately
    does not: it passes the token unconditionally and lets
    chains/solana_signing.require_send_confirmation() refuse on the empty keypair
    path, because that refusal arrives with the whole PREVIEW inside it -- cluster,
    payer, destination, lamports, fee, rent verdict, headroom -- and an operator who
    then exports the variable is arming something they have read. A pre-flight
    refusal here would be a second copy of the same rule (rule 8) that said less.
    What it costs is read-only: the preview's genesis, balance, account and rent
    reads happen before the refusal on an unarmed host. Nothing is signed and
    nothing is broadcast, which tests/test_solana_payout.py asserts on the
    recorded call list rather than on which exception came back.

    SO THE ARMING IS THE KEYPAIR PATH, AND NOT A BOOLEAN ANYWHERE. There is no
    config flag, no Config field and no `if enabled:` on this path. The only way a
    SOL payout signs is for SOL_PAYOUT_KEYPAIR_PATH to name a keypair file that
    derives to the announced payer, on a cluster whose own genesis hash is devnet's.
    chains/solana_payout_keypair.payout_keypair_is_present() is the same question
    read one layer up, where it sets the adapter's can_spend and therefore whether
    services/swap_service.create_swap() will create such a swap at all.

    EVERY OTHER CHAIN IS UNCHANGED, BYTE FOR BYTE. An asset that is neither XRP nor
    SOL gets `send_to_address(address, amount)`, the same two positional arguments it
    always got. That is deliberate rather than incidental: adding keywords to a BTC
    send would be a change to the one function in this suite that moves money, for no
    behavioral gain.
    """
    if asset == "SOL":
        # THE TOKEN IS SPELLED BY IMPORTING THE CONSTANT, never by copying its text,
        # for the reason the XRP branch below records: `grep -rn CONFIRM_SOL_SEND`
        # is meant to enumerate every site in this tree that can send SOL, and a
        # local literal would hide from it -- while a literal that drifted by one
        # character would refuse every payout with a message about the token.
        return adapter.send_to_address(address, amount, confirm_send=CONFIRM_SOL_SEND)

    if asset not in SHARED_ACCOUNT_PAYOUT_ASSETS:
        return adapter.send_to_address(address, amount)

    # Raises ValueError, named and with the variable in it, when XRP_DEPOSIT_ACCOUNT
    # is unset or is not a payable account. services/swap_service.create_swap()
    # already refuses to CREATE a swap in that state, so reaching this means the
    # variable was removed between creation and payout -- which is exactly when a
    # loud refusal before any network call is what is wanted.
    source = payout_source_account(config, asset)
    # PRESENCE, NOT DECODING, AND THAT IS DELIBERATE SINCE 2026-10-03 (rule 8 asks
    # that a real difference be named at both sites; the other is
    # chains/xrp_payout_seed.payout_capability(), which decodes and is what
    # XRPAdapter.can_spend reads).
    #
    # WHAT THIS CHECK IS FOR is the narrow case its message describes: the swap was
    # CREATED while the terminal was armed -- create_swap() refuses otherwise -- so
    # reaching here with nothing exported means the variable was removed between
    # creation and payout, and the right answer is a loud refusal before any network
    # call. An undecodable value is not that case: it cannot have passed create_swap()
    # since the narrowing, and if it somehow arrives it is refused by
    # chains/xrp_signing.derive_and_check()'s own Wallet.from_seed() with nothing
    # signed, nothing submitted and no fee claimed -- the same outcome, one guard
    # later. Decoding it here as well would be a second copy of that question on the
    # money path, and the copy would have to invent a third sentence about it.
    if not signing_seed_is_present():
        raise PayoutSigningUnavailable(
            f"{asset} payouts are signed in this process and {SIGNING_SEED_ENV_VAR} is not set in its "
            f"environment, so NOTHING was signed, nothing was submitted and no fee was claimed. The "
            f"swap was created while it WAS set -- chains/xrp.py reads it at adapter construction and "
            f"services/swap_service.create_swap() refuses without it -- so it has been removed since. "
            f"Export {SIGNING_SEED_ENV_VAR} in the shell that starts this worker; a value set in a "
            f"file, or in another shell, does not reach here. The account that pays is {source}."
        )
    return adapter.send_to_address(
        address,
        amount,
        source=source,
        # CALLED IN THE ARGUMENT LIST ON PURPOSE. Assigning it to a local first is
        # the obvious spelling and it is the one that leaks: a local survives into
        # a traceback's frame locals, which anything that formats an exception with
        # a rich traceback renderer will print. Consumed here, it exists for the
        # duration of the call and is named nowhere.
        seed=signing_seed(),
        # THE ARMING TOKEN, SPELLED BY IMPORTING THE CONSTANT rather than by copying
        # its text. chains/xrp_signing.py compares for EXACT equality, so a copied
        # literal that drifted by one character would refuse every payout with a
        # message about the token -- and `grep -rn CONFIRM_XRP_SEND` is meant to
        # enumerate every site in this tree that can send XRP, which a local copy
        # of the string would hide from.
        confirm_send=CONFIRM_XRP_SEND,
        # NO DESTINATION TAG, and the absence is a decision rather than an omission.
        # `swaps` has a deposit_tag column and no payout_tag column: a tag on the
        # way OUT is the customer's exchange's identifier, this terminal never
        # collects one, and passing a wrong one would misroute a payment inside
        # their exchange with no way to recover it. A customer whose destination
        # needs a tag therefore cannot be paid by this path, which is a known
        # limitation and the honest one: preview_payout() refuses an X-address
        # outright for the same reason rather than silently dropping its tag.
    )


def warn_if_address_unchecked(swap, verdict, destination_asset: str) -> bool:
    """Say out loud that a payout address was never verified. True if it warned.

    EXTRACTED FROM process_pending_payouts() ON 2026-10-10, UNCHANGED IN BEHAVIOR. The
    loop was at ruff's complexity ceiling, and adding defer_for_missing_adapter()'s
    branch pushed it over. Rule 12 says the fix is to extract a decision rather than
    raise the ceiling -- and this block is a decision ("does the operator have to be
    told nothing was checked") that had been written as a logging call inside
    orchestration, which is rule 10's defect in its mildest form. It is now callable
    with a seeded verdict and asserted on directly.

    `.unchecked` RATHER THAN `state == NO_VALIDATOR`, since 2026-09-27: there are TWO
    ways to pass without being verified and the second is the one that actually
    happened. NO_VALIDATOR is "no validator for this chain"; UNDETERMINED is "a
    validator ran and could not place the address", which is what Litecoin's regtest hrp
    `rltc` and its second P2SH byte 0x3A both produced on the operator's live regtest
    swap. Both proceed, for the same reason -- a gap in our tables is not evidence
    against a customer's address -- and both must say so.

    IT DOES NOT REFUSE, and that is the decision rather than an omission: refusing an
    address we cannot place would break a working chain, which is worse than the burn
    this guard prevents. verdict.refuses is the arm that stops a send, and it is
    checked by the caller immediately above this call.
    """
    if not verdict.unchecked:
        return False
    logger.warning(
        "payout for swap %s is going to a %s address that was NOT CHECKED (%s): %s  <- the send is "
        "proceeding, because refusing an address we cannot place would break a working chain, which "
        "is worse than the burn this guard prevents.",
        swap["id"], destination_asset, verdict.state, verdict.why,
    )
    return True


def defer_for_missing_adapter(db, swap, destination_asset: str, adapters) -> bool:
    """True if this worker cannot pay `destination_asset`; the swap is deferred, not failed.

    =========================================================================
    A CREDITED SWAP WAS KILLED WITH failed_reason "'GRC'"
    =========================================================================

    `adapter = adapters[destination_asset]` used to sit INSIDE the try in
    process_pending_payouts(), whose handler writes str(exc) into swaps.failed_reason and
    sets the swap to 'failed'. str(KeyError('GRC')) is "'GRC'" and nothing else.

    MEASURED: one payout_pending BTC->GRC swap seeded into a throwaway database, the real
    process_pending_payouts() called with adapters={'BTC': ...} and no GRC entry:

        status        = failed
        failed_reason = "'GRC'"
        payouts       = asset=GRC amount=1000.0 status=failed txid=None
        audit         = paying -> failed : Payout failed: 'GRC'

    after the customer's deposit was already irreversible. And 'failed' invites a retry
    this worker will never make -- nothing re-queues a failed swap.

    =========================================================================
    WHY DEFER RATHER THAN IMPROVE THE MESSAGE
    =========================================================================

    A missing adapter is a configuration fault in THIS PROCESS, not a fact about the
    swap. workers/payout_worker.py builds adapters once from its own environment
    (payout_worker.py:97), and the web process that created the swap is a different
    process with a different environment -- on the host, a different shell. So the next
    worker start, with the variable set, pays this swap correctly. Marking it failed
    throws that away for a reason that will not be true in five minutes.

    THE TREE ALREADY FIXED THIS ONCE, IN THE OTHER DIRECTION. On 2026-09-26 the
    operator's browser produced "No swap was created: GRC" because the SERVER lacked
    GRC_RPC_PORT while the workers had it, and services/swap_service.py gained
    unconfigured_chains + why_unconfigured for it. create_swap got a gate; the payout
    worker did not. Same defect, one process over (rule 8) -- which is why this uses
    chains/registry.why_unconfigured() rather than composing its own sentence.

    =========================================================================
    WHAT IT WRITES, AND WHAT IT DELIBERATELY DOES NOT
    =========================================================================

      status -> payout_pending   AND NOT left in 'paying'. claim_swap_for_payout() moved
                                 it to 'paying' to claim it against the other worker;
                                 leaving it there would strand it exactly as 'failed'
                                 did, because nothing re-queues a 'paying' swap either.
      an audit row               so the deferral is in the swap's own history and on
                                 /admin, not only in a worker log the operator is not
                                 reading at the time.
      failed_reason             UNTOUCHED. The swap has not failed, and a reason on a
                                 pending swap is read by show_swap.py and the admin page
                                 as a post-mortem.
      no payouts row            it returns before the INSERT, so there is nothing for the
                                 next cycle's partial unique index to collide with.
      no inventory reservation   it returns before reserve_inventory(), so there is
                                 nothing to unwind.

    RETURNS A BOOL AND THE CALLER CONTINUES, rather than raising: a raise would land in
    the same broad handler this exists to keep the swap out of.
    """
    if destination_asset in adapters:
        return False

    why = why_unconfigured(destination_asset)
    logger.error(
        "swap %s is credited and payable but this worker has NO %s ADAPTER, so nothing was sent and "
        "the swap is LEFT IN payout_pending rather than failed: %s  <- a configuration fault in THIS "
        "process, not a dead swap. The web process that created the swap and this worker have "
        "separate environments; a worker restarted with the variable set will pay it. Nothing was "
        "reserved and no payout row was written.",
        swap["id"], destination_asset, why,
    )
    db.execute(
        "INSERT INTO swap_audit_log (swap_id, old_status, new_status, message, created_at)"
        " VALUES (?, ?, ?, ?, ?)",
        (swap["id"], swap["status"], "payout_pending",
         f"Payout deferred: this worker has no {destination_asset} adapter. {why}", utc_now_iso()),
    )
    db.execute(
        "UPDATE swaps SET status = 'payout_pending', updated_at = ? WHERE id = ?",
        (utc_now_iso(), swap["id"]),
    )
    db.commit()
    return True


def address_check_stops_payout(db, swap, verdict, destination_asset: str, amount) -> bool:
    """True if the payout address refuses the send. False proceeds, having warned if unchecked.

    ONE FUNCTION FOR BOTH ARMS, because they are two answers to one question and were two
    branches in the loop. The behavior of each is unchanged and lives in the function it
    already lived in -- refuse_payout_before_sending() and warn_if_address_unchecked() --
    so this is a junction and holds no decision of its own beyond the order.

    THE ORDER IS LOAD-BEARING AND IS THE ONLY THING THIS ADDS. `.refuses` is checked
    first: an address that cannot be spent from must stop the send before anything warns
    about it being unchecked, and a verdict can carry both states' wording without
    carrying both meanings.

    WHY A REFUSAL IS TERMINAL AND A DEFERRAL IS NOT, which this loop now has both of.
    refuse_payout_before_sending() marks the swap failed, and that is correct here:
    'SgrcPayoutAddress' is not a GRC address in any form, and money sent to it would be
    unspendable by anybody -- no restart, no variable and no retry changes that. The
    deferral arms below are the opposite case: a missing adapter or an unset passphrase
    is a fact about THIS PROCESS, true for as long as it takes to export a variable.
    Marking those failed is what cost a credited swap, and conflating the two categories
    is the mistake this comment exists to prevent.
    """
    if verdict.refuses:
        refuse_payout_before_sending(db, swap, verdict, destination_asset, amount)
        return True
    warn_if_address_unchecked(swap, verdict, destination_asset)
    return False


def halt_on_duplicate_payout_row(db, swap, exc) -> None:
    """A live payouts row already exists for this swap. Nothing is sent and nobody retries.

    idx_payouts_one_live_per_swap refused a second LIVE row. Reaching this means
    claim_swap_for_payout()'s UPDATE and the index disagree, which is a state a human has
    to look at -- so nothing is sent, nothing is retried, and the reason is written where
    the operator reads it rather than only raised.

    IT IS A HALT AND NOT A DEFERRAL, which is the distinction worth being precise about
    now that this loop has two deferral arms beside it. defer_for_missing_adapter() and
    defer_for_locked_wallet() both return the swap to payout_pending, because in both
    cases NOTHING HAPPENED and the fix is an environment variable. Here something may
    very well have happened: a live payout row with a txid is a payout that was
    broadcast, and this swap is one of the two the 2026-09 double-payout measurement
    produced. So it stays in 'paying', which is the status nothing picks up, and the
    operator reconciles the existing payout before releasing it.

    EXTRACTED 2026-10-10 FROM AN INLINE except ARM, behavior unchanged -- see the call
    site's comment for why (ruff's C901, and rule 12's reading of it).
    """
    db.rollback()
    logger.error(
        "swap %s: a live payout row already exists (%s). NOTHING SENT. The swap is left in 'paying' and "
        "will not be retried automatically; reconcile the existing payout before releasing it.",
        swap["id"],
        exc,
    )
    db.execute(
        "INSERT INTO swap_audit_log (swap_id, old_status, new_status, message, created_at) VALUES (?, ?, ?, ?, ?)",
        (swap["id"], "paying", "paying", f"Payout suppressed by unique index: {exc}", utc_now_iso()),
    )
    db.commit()


def defer_for_locked_wallet(db, swap, destination_asset: str, amount, exc) -> None:
    """Put a swap back in payout_pending because this worker has no wallet passphrase.

    =========================================================================
    PayoutUnlockUnavailable WAS A TYPE CREATED FOR A DISTINCTION NOBODY MADE
    =========================================================================

    Its own docstring says why it exists: "categorically different from a send that
    FAILED: no transaction was created, nothing reached any daemon, and the fix is an
    environment variable rather than an investigation." payout_unlock_context()'s says
    "the reason is named and nothing is claimed."

    Both were true of the MESSAGE and false of the OUTCOME. Nothing caught the type, so
    the raise fell through to `except Exception`, which wrote that sentence into
    swaps.failed_reason and set the swap to 'failed'. The swap died of an unset
    environment variable -- the exact thing the raise was written to prevent -- and
    nothing in this tree re-queues a failed swap, which is why
    services/payout_rescue.py had to exist at all.

    FOUND BY RUNNING THE CODE, NOT BY READING IT (rule 17, and the ordinary way this
    goes). A test seeding a BTC->GRC payout with a real GRC adapter failed on
    `assert sent, "a configured chain's payout was deferred"`, and the captured log
    read `payout FAILED ... GRIDCOIN_WALLET_PASSPHRASE is not set ... <- the swap is
    now 'failed' and this worker will NOT retry it`. The test was wrong about its own
    fixture; the code was wrong about the swap.

    =========================================================================
    WHY DEFERRING IS SAFE HERE AND NOT IN THE BROAD HANDLER
    =========================================================================

    NOTHING WAS SENT AND THAT IS CERTAIN. payout_unlock_context() raises BEFORE
    entering the `with`, so there was no unlock, no signature and no broadcast. That is
    what separates this from the case the broad handler has to live with -- a timeout
    that may or may not have been relayed -- and conflating the two is what cost the
    swap. A deferral is only ever correct when "nothing happened" is a fact rather than
    a hope.

    IT WITHDRAWS THE PAYOUTS ROW, which is not optional. The row is INSERTed and
    committed BEFORE the send (see process_pending_payouts()' docstring on ordering),
    so leaving it at 'created' would block the very retry this deferral exists for:
    idx_payouts_one_live_per_swap refuses a second live row.

    AND IT RELEASES THE RESERVATION, through release_inventory_after_send() despite
    nothing having been sent -- the name is the only thing wrong with that. It releases
    the reservation and deliberately does not touch hot_confirmed, which is what drove
    XRP's column to -59.231412662192405 twice on the operator's host. A reservation
    taken for a send that did not happen has to come back, or the hot wallet is
    permanently short on paper.

    BACK TO payout_pending AND NOT LEFT IN 'paying'. claim_swap_for_payout() moved it to
    'paying'; nothing re-queues a 'paying' swap either, so leaving it there strands it
    exactly as 'failed' did.
    """
    # The payouts row was INSERTed and committed before the send (see this
    # function's docstring on ordering), so it has to be withdrawn rather than
    # left at 'created' -- a live row would block the retry this deferral is
    # for, via idx_payouts_one_live_per_swap.
    db.execute(
        "DELETE FROM payouts WHERE swap_id = ? AND status = 'created' AND txid IS NULL",
        (swap["id"],),
    )
    # release_inventory_after_send() DESPITE NOTHING HAVING BEEN SENT, and the
    # name is the only thing wrong with that. It releases the RESERVATION and
    # deliberately does not touch hot_confirmed (see its docstring: debiting
    # hot_confirmed here is what drove XRP's column to -59.231412662192405
    # twice on the operator's host). A reservation taken for a send that did not
    # happen has to come back or the hot wallet is permanently short on paper,
    # which is the same correction services/payout_rescue.py makes by hand.
    release_inventory_after_send(db, destination_asset, amount)
    db.execute(
        "INSERT INTO swap_audit_log (swap_id, old_status, new_status, message, created_at)"
        " VALUES (?, ?, ?, ?, ?)",
        (swap["id"], "paying", "payout_pending", f"Payout deferred, NOTHING SENT: {exc}",
         utc_now_iso()),
    )
    db.execute(
        "UPDATE swaps SET status = 'payout_pending', updated_at = ? WHERE id = ?",
        (utc_now_iso(), swap["id"]),
    )
    db.commit()
    logger.error(
        "payout DEFERRED for swap %s (%s -> %s, %s %s): %s  <- NOTHING was sent, nothing was "
        "signed, no inventory is held and the swap is back in payout_pending. This is a "
        "configuration fault in THIS worker process, not a failed swap: set the variable, "
        "restart the worker, and it pays. Before 2026-10-10 this marked the swap 'failed' and "
        "nothing re-queued it.",
        swap["id"], swap["from_asset"], swap["to_asset"], amount, destination_asset, exc,
    )


def pay_one_swap(db, config, adapters: dict, swap):
    """Pay ONE credited swap, or return None having recorded why it was not paid.

    =========================================================================
    EXTRACTED FROM process_pending_payouts()' LOOP BODY ON 2026-10-10
    =========================================================================

    A PURE MOVE: every line below was already running, in this order, inside
    `for swap in swaps:`. What changed is that `continue` became `return None` and the
    single `completed.append(...)` became the return value.

    WHY IT MOVED, AND IT IS NOT HOUSEKEEPING. Two guard arms were added that day -- a
    missing adapter and a locked wallet, each of which had been killing a CREDITED swap
    -- and ruff's C901 took the loop function to 11. Rule 12 is explicit about what that
    code means: "a main() past the ceiling is orchestration that has swallowed decisions,
    which is rule 10's defect wearing a lint code. The fix is to extract the decision so
    it can be called with seeded inputs, not to raise the ceiling." Three successive
    body-only extractions did not move the number, because the BRANCHES are the
    complexity and the branches all belong to ONE SWAP'S PAYOUT. That is the unit, so
    that is the function.

    AND IT IS WHY TODAY'S TWO DEFECTS WERE HARD TO SEE. Both -- `adapters[asset]` inside
    a try whose handler marks the swap failed, and a PayoutUnlockUnavailable that nothing
    caught -- were reachable only by seeding a whole payout cycle. One swap's payout is
    now callable with seeded inputs, which is the whole of rule 10's argument: "when the
    decision is a function at the bottom, it can be called with seeded inputs and
    asserted on directly. When it is buried three levels up inside orchestration, the
    only way to test it is to run the whole thing against a real chain, and the only way
    to find it is to already know it is there."

    RETURNS the swap row when a payout was broadcast and recorded, and None otherwise.
    None covers every non-send outcome and they are NOT equivalent to one another -- a
    refusal, a halt and a deferral leave the swap in three different statuses, each
    written and logged where it happens. The caller counts sends; the swap's own status
    and audit trail are where the other outcomes live.

    IT DOES NOT COMMIT AT THE END, deliberately: process_pending_payouts() commits once
    after the loop, exactly as it did when this was the loop body. The commits INSIDE
    here are the ones that were always here, and each is load-bearing -- the intent to
    pay is durable before any money can move (see the caller's docstring on ordering).

    CAN MOVE FUNDS: YES. This is the function that calls send_to_address().
    """
    destination_asset = swap["to_asset"]
    # FROM THE DEPOSIT THAT ARRIVED, not the one that was quoted. See
    # payout_amount() for the arithmetic and why the difference was silent.
    amount = amount_decided_and_logged(swap)

    if not claim_swap_for_payout(db, swap["id"]):
        # Another worker owns this payout. Not an error and not a failure:
        # the swap is being paid by somebody else, right now.
        logger.info(
            "swap %s: payout claimed by another worker, skipping (0 sent by this worker for this swap)",
            swap["id"],
        )
        return None

    # THE BURN GUARD. Added 2026-09-27 at the operator's instruction ("make this burn
    # proof"), and it is the LAST place a bad address can be stopped: the next fund-path
    # statement in this function is adapter.send_to_address(), after which the money is
    # on a chain and nobody -- not us, not the customer, not the miner -- can spend it.
    # Not stolen. Not recoverable. Gone.
    #
    # The defect being closed: modules/address_network.is_valid_address() landed in
    # f805efa and NOTHING ON THE FUND PATH CALLED IT. An undecodable payout address went
    # straight through to the daemon.
    #
    # WHY NOT THAT FUNCTION, AND WHY THIS IS A TABLE LOOKUP INSTEAD. is_valid_address()
    # understands bech32, Bitcoin-alphabet base58check and XRP-alphabet base58check.
    # A Solana address is plain base58 of an ed25519 key with no checksum at all --
    # measured 2026-09-27, every valid SOL fixture in tests/valid_addresses.py returns
    # False from it. So the one-line version of this guard would refuse every Solana
    # payout the day a SOL pair is enabled, on a swap whose deposit is ALREADY OURS and
    # already credited. That is a worse outcome than
    # the burn: the burn costs one payout, the false refusal costs every customer of
    # that chain while their money sits in our wallet. modules/address_authority.py is
    # the per-asset table that avoids it.
    #
    # NO_VALIDATOR PASSES THROUGH, LOUDLY, for the same reason: refusing a chain we
    # cannot check IS that outage, arriving by the door a future chain comes in by.
    # tests/test_address_authority.py closes the hole at the other end by asserting
    # every asset this terminal can reach HAS a validator, so the gap fails the suite
    # instead of either burning money or stranding a payout.
    #
    # PLACED AFTER THE CLAIM AND BEFORE reserve_inventory(), which is not arbitrary.
    # Claiming first means exactly one worker owns this swap, so the refusal is written
    # once. Refusing before the reserve and before the INSERT means a refused payout
    # leaves NO reserved-inventory row and NO payouts row in 'created' -- the two
    # dangling states this function's own docstring is about. The swap lands in 'failed'
    # by the same route a failed send does (set_swap_status + failed_reason + audit),
    # because 'payout_pending' would be re-read and re-refused on every cycle forever,
    # which is rule 14's "did nothing must not look like did work" turned into a loop.
    verdict = check_address(destination_asset, swap["payout_address"])
    # ONE CALL FOR BOTH ARMS OF ONE QUESTION, since 2026-10-10. The question is
    # "what does the address check say about this payout", and it has exactly two
    # answers that matter here: stop, or proceed having said that nothing was
    # checked. They were two separate `if`s in this loop, which is two branches for
    # one decision -- rule 10's shape, and what took ruff's C901 over the ceiling
    # when the two deferral guards were added below.
    if address_check_stops_payout(db, swap, verdict, destination_asset, amount):
        return None

    # EXTRACTED, NOT INLINED, AND ruff's C901 IS WHY -- rule 12: "a main() past the
    # ceiling is orchestration that has swallowed decisions. The fix is to extract the
    # decision so it can be called with seeded inputs, not to raise the ceiling." This
    # loop went to complexity 11 the moment the check was written inline, which is the
    # linter pointing at the layering rather than at the line count.
    if defer_for_missing_adapter(db, swap, destination_asset, adapters):
        return None

    reserve_inventory(db, destination_asset, amount)
    try:
        db.execute(
            "INSERT INTO payouts (swap_id, asset, destination_address, amount, txid, status, created_at, sent_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (swap["id"], destination_asset, swap["payout_address"], amount, None, "created", utc_now_iso(), None),
        )
    except sqlite3.IntegrityError as exc:
        # EXTRACTED for ruff's C901 (rule 12: extract the decision, do not raise the
        # ceiling). The loop grew two guard arms on 2026-10-10 -- a missing adapter
        # and a locked wallet -- and this third arm's body is what took it over.
        halt_on_duplicate_payout_row(db, swap, exc)
        return None

    # Durable BEFORE the send. See this function's docstring.
    db.commit()

    try:
        # LOCK -> UNLOCK past staking -> send -> LOCK -> back to staking, for any
        # chain in WALLET_UNLOCK_ASSETS; a no-op context for the rest. The
        # re-lock is in the context manager's `finally`, so it runs even when
        # the send raises -- a wallet left fully unlocked because a payout failed
        # is the outcome that must not happen.
        # GUARANTEED PRESENT by the refusal above, which returns before this try is
        # entered. Kept as a lookup rather than threaded in, because the membership
        # check and this line are twenty lines apart and a reader of either needs the
        # other: a KeyError here would mean `adapters` was mutated mid-loop.
        adapter = adapters[destination_asset]
        # ONE NAME CARRYING BOTH FACTS, 2026-10-09, AND NO BEHAVIOR CHANGE.
        #
        # This was `recorded = False` plus a `txid` assigned INSIDE the `with` below, and
        # the two could only be read together by trusting that they were set on adjacent
        # lines. A checker cannot: it reports `txid` as possibly unbound at both reads in
        # the `except GridcoinLockError` handler, because the only thing that makes them
        # safe is `if not recorded: raise` -- a different variable.
        #
        # `recorded_txid is None` IS `not recorded`. Both were set at the identical point,
        # immediately after _record_broadcast() returns, so the branch takes the same arm
        # for every input. `is None` rather than falsiness, deliberately: a daemon that
        # ever returned an empty txid must still count as RECORDED here, because the
        # payouts row and the swap status were already committed and treating that as "not
        # recorded" would re-raise and mark a delivered payment failed -- which is the
        # 2026-09-26 defect this whole block exists to prevent, arrived at from the other
        # direction.
        #
        # tests/test_payout_concurrency.py's
        # test_a_failed_send_whose_restore_also_fails_is_a_payout_failure() is the one that
        # pins the re-raise arm, and it asserts on the recorded reason specifically so that
        # a NameError from THIS handler cannot pass as the daemon's own failure.
        recorded_txid: str | None = None
        try:
            with payout_unlock_context(destination_asset, adapter):
                # broadcast_payout() rather than adapter.send_to_address() since
                # 2026-10-02: XRP's send needs a source account, a signing seed and
                # an arming token, and every other chain's needs exactly the two
                # positional arguments this line used to pass. The dispatch is a
                # function so it can be tested with a stub adapter and a seeded
                # environment; see its docstring for the full refusal order.
                txid = broadcast_payout(adapter, destination_asset, config, swap["payout_address"], amount)
                # RECORDED INSIDE THE CONTEXT, BEFORE THE RE-LOCK CAN RAISE.
                #
                # This block sat AFTER the `with` until 2026-09-26, and the first
                # real payout this code ever made is what found it. The send
                # succeeded -- 55.52645238 GRC left the wallet, txid
                # 3e09dc9cfd7a61da..., confirmed afterwards in the operator's own
                # listtransactions -- and then the context's restore raised,
                # because the staking unlock was being sent a timeout of 0 that
                # Gridcoin refuses. Control jumped from the `with` straight to the
                # except clause below, `txid` was discarded, and the swap was
                # marked `failed` with `txid (none)`.
                #
                # Money out, no record: the single worst outcome available on this
                # path, and it was caused by a wallet-housekeeping call that has
                # nothing to do with whether the payment was delivered.
                #
                # The commit is what makes it durable, and `recorded_txid` is set
                # only after it returns -- so a database failure here is still a
                # payout failure, while a LOCK failure after it is not.
                _record_broadcast(db, swap, amount, txid)
                recorded_txid = txid
        except GridcoinLockError:
            # THE PAYOUT IS ALREADY DURABLE. The wallet's lock state is a separate
            # problem with its own loud message (chains/gridcoin_wallet_lock.py
            # distinguishes "locked, not staking" from "may still be unlocked"),
            # and treating it as a payout failure is what mislabeled a delivered
            # payment. Re-raised when the send never got as far as being recorded,
            # because then it IS the payout's failure -- `recorded_txid is None` is that
            # test, and it holds the txid the message below needs for the same reason.
            if recorded_txid is None:
                raise
            logger.exception(
                "payout for swap %s WAS BROADCAST as %s and is recorded as completed. The wallet's "
                "lock state could not be restored afterwards -- read the message above and act on "
                "the wallet, NOT on the swap.",
                swap["id"],
                recorded_txid,
            )
            db.execute(
                "INSERT INTO swap_audit_log (swap_id, old_status, new_status, message, created_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (
                    swap["id"],
                    "completed",
                    "completed",
                    f"payout broadcast {recorded_txid}; wallet lock restore FAILED afterwards",
                    utc_now_iso(),
                ),
            )
            db.commit()
        # THE RETURN VALUE, which was `completed.append(...)` while this was a
        # loop body. The caller collects it; this function knows one swap.
        return db.execute("SELECT * FROM swaps WHERE id = ?", (swap["id"],)).fetchone()
    # Checked, and this broad catch is the right one: `send_to_address`
    # can fail for transport reasons, daemon reasons, insufficient funds,
    # or a rejected transaction, and EVERY one of them must land the swap
    # in `failed` with the reason recorded rather than aborting the loop
    # and leaving the remaining swaps unprocessed. The caller can tell the
    # failure from success because the swap's status and failed_reason both
    # say so -- rule 12's test is met in the return value, not by narrowing.
    #
    # What it does NOT establish is whether the transaction was broadcast.
    # A timeout after the daemon accepted it looks identical to a refusal,
    # and this marks both 'failed'. That is a real gap on the fund path and
    # it is named in the enforcement report rather than changed here. Note
    # what the partial unique index does about it: 'failed' is not a live
    # status, so a retry CAN insert a new payout row -- which is the right
    # behavior for a refusal and the wrong one for a timeout that was
    # actually relayed. Same gap, now with a name.
    # =====================================================================
    # A MISSING PASSPHRASE IS A CONFIGURATION FAULT, NOT A DEAD SWAP
    # =====================================================================
    #
    # CAUGHT BEFORE THE BROAD HANDLER BELOW, and before 2026-10-10 it was not --
    # which made PayoutUnlockUnavailable a type created for a distinction nobody
    # ever made. Its own docstring says why it exists: "categorically different
    # from a send that FAILED: no transaction was created, nothing reached any
    # daemon, and the fix is an environment variable rather than an
    # investigation." And payout_unlock_context()'s says "the reason is named and
    # nothing is claimed". Both were true of the MESSAGE and false of the OUTCOME:
    # the raise fell through to `except Exception`, which wrote the sentence into
    # swaps.failed_reason and set the swap to 'failed'. The swap died of an unset
    # environment variable, which is the exact thing the raise was written to
    # prevent.
    #
    # FOUND BY RUNNING IT, not by reading it. A test seeding a BTC->GRC payout with
    # a real GRC adapter failed with `assert sent, "a configured chain's payout was
    # deferred"` and the captured log showed `payout FAILED ... GRIDCOIN_WALLET_
    # PASSPHRASE is not set ... <- the swap is now 'failed' and this worker will NOT
    # retry it`. The test was wrong about its own fixture; the code was wrong about
    # the swap.
    #
    # SAME REMEDY AS defer_for_missing_adapter(), FOR THE SAME REASON. Both are
    # facts about THIS PROCESS's environment rather than about the swap or the
    # customer: set the variable, restart the worker, and the swap pays. Marking it
    # failed throws that away, and nothing in this tree re-queues a failed swap --
    # which is why services/payout_rescue.py had to be written at all.
    #
    # NOTHING WAS SENT AND THAT IS CERTAIN HERE, which is what makes deferring safe:
    # payout_unlock_context() raises BEFORE entering the `with`, so no unlock, no
    # signature and no broadcast occurred. This is not the ambiguous case the broad
    # handler below has to live with (a timeout that may or may not have been
    # relayed) -- it is the unambiguous one, and conflating them is what cost the
    # swap.
    except PayoutUnlockUnavailable as exc:
        # EXTRACTED for ruff's C901, which went to 11 the moment this arm was
        # written inline -- rule 12: extract the decision, do not raise the ceiling.
        defer_for_locked_wallet(db, swap, destination_asset, amount, exc)
        return None
    except Exception as exc:  # noqa: BLE001
        db.execute(
            "UPDATE payouts SET status = ? WHERE swap_id = ? AND status = 'created'",
            ("failed", swap["id"]),
        )
        db.execute(
            "UPDATE swaps SET failed_reason = ?, updated_at = ? WHERE id = ?",
            (str(exc), utc_now_iso(), swap["id"]),
        )
        if not set_swap_status(db, swap["id"], "failed", f"Payout failed: {exc}", old_status="paying"):
            # Same anomaly and same reasoning as the address-refusal path above: an
            # exclusive claim was committed, so 'paying' should still be there. The send
            # may or may not have reached the chain -- that is what this except clause is
            # about -- and raising from here would replace a recorded failure with an
            # unrecorded one.
            logger.error(
                "swap %s: the payout FAILED (%s) and the reason is recorded in "
                "swaps.failed_reason and on the payouts row, but the swap could not be "
                "marked failed -- it is no longer 'paying', so another process moved it "
                "out of a committed payout claim. Read it back with show_swap.py --swap %s.",
                swap["id"], exc, swap["id"],
            )
        db.commit()
        # LOGGED, not only recorded. The reason reached swaps.failed_reason and
        # the audit log; it reached NOTHING the operator was looking at. Their
        # run 2026-09-26 printed
        #
        #     payout_worker cycle=1 WORKED pending_at_start=1 broadcast=0 failed_total=1
        #
        # and nothing else -- so a locked wallet, an insufficient balance, a
        # rejected address and an unreachable daemon all look identical from the
        # terminal, and the one that is a five-second fix is indistinguishable
        # from the one that needs an investigation.
        #
        # At ERROR because a failed payout on a CREDITED swap is the most
        # serious routine outcome this worker has: the customer's deposit is
        # already ours and they have not been paid.
        logger.error(
            "payout FAILED for swap %s (%s -> %s, %s %s to %s): %s  <- the swap is now 'failed' "
            "and this worker will NOT retry it",
            swap["id"],
            swap["from_asset"],
            swap["to_asset"],
            # THE AMOUNT ACTUALLY ATTEMPTED, not the quoted estimate. These were the
            # same figure until 2026-10-03 and are not any more, and a failure log
            # naming a number that was never sent is how an investigation starts
            # from the wrong premise.
            amount,
            swap["to_asset"],
            swap["payout_address"],
            exc,
        )


def process_pending_payouts(db, config, adapters: dict) -> list[dict]:
    """Broadcast the payout for every swap that is waiting for one.

    THE ORDERING AROUND THE SEND, AND WHAT IT COSTS.

    Rule 5 says write SQL first and mirror second, and rule 13 notes that a
    signal can land between `sendtoaddress` returning a txid and the UPDATE
    that records it. Those two together decide the order here:

      1. claim the swap (UPDATE ... WHERE status='payout_pending'), reserve
         inventory, INSERT the payouts row with status='created' -- and COMMIT
         all of it BEFORE the send;
      2. send;
      3. record the txid, mark the payout 'broadcast' and the swap 'completed',
         and commit again.

    So the intent to pay is durable before any money can move. The cost is
    stated rather than hidden: a crash or a SIGKILL between (2) and (3) leaves
    a payouts row with status='created' and NO txid, and a swap stuck in
    'paying' -- money possibly on chain with no txid recorded. That cannot be
    made atomic from here; a broadcast and a commit are two systems. What it
    buys is that the failure is VISIBLE and is not automatically repeated: no
    worker acts on a swap in 'paying', and the partial unique index counts
    'created' as live, so nothing can insert a second payout for that swap
    until a human resolves the first. The opposite order -- send, then write --
    would lose the record entirely and leave the swap in 'payout_pending' for
    the next cycle to pay AGAIN.

    The IntegrityError path is deliberate for the same reason. If the index
    fires, a live payout row for this swap already exists; the swap is left in
    'paying' with an audit row saying so, and nothing is sent. It is not marked
    'failed', because 'failed' invites a retry and a payout that may already be
    on chain is exactly what must not be retried.
    """
    swaps = db.execute(
        "SELECT * FROM swaps WHERE status = 'payout_pending' ORDER BY credited_at ASC"
    ).fetchall()
    completed = []
    for swap in swaps:
        # ONE SWAP, ONE FUNCTION (rule 10). This loop is orchestration and holds no
        # decision of its own: pay_one_swap() returns the swap row when it broadcast,
        # and None for every outcome that did not -- each of which it has already
        # recorded and logged where it happened.
        paid = pay_one_swap(db, config, adapters, swap)
        if paid is not None:
            completed.append(paid)
    db.commit()
    return completed


# Assets whose get_balance() failure has already been reported this process. A
# DESIGNED refusal must not warn every cycle.
#
# Measured on the operator's host 2026-09-26: ten payout_worker cycles printed ten
# copies of "wallet inventory for XRP NOT refreshed", because the XRP adapter
# refuses get_balance() BY DESIGN -- it holds no hot-wallet account, which is the
# custody decision it is waiting on. So the warning described a fault that does not
# exist, once every ten seconds, forever.
#
# chains/registry.py's own header names this exact hazard as the reason SOL is left
# unconstructed rather than built and left to warn: "a log that cries wolf is a log
# nobody reads the day something real happens". This is that, arriving through the
# other door -- a configured adapter whose refusal is permanent.
#
# Said ONCE per process, and a restart says it again: an operator starting a worker
# is exactly the person who needs to know an asset's balance is not being polled.
# Keyed on the asset AND the message, so a DIFFERENT failure for the same asset --
# a daemon that was up and is now down -- still reports.
_REPORTED_INVENTORY_FAILURES: set[tuple[str, str]] = set()


# Chains whose wallet must be FULLY UNLOCKED to send, and which are left unlocked
# for staking the rest of the time. Gridcoin is the only one here.
#
# Operator, 2026-09-26: "the system is supposed to unlock the wallet FULLY on it's
# own not the user for now", and the order: "LOCK---UNLOCK past staking---LOCK---
# return to unlocked for staking".
#
# WHAT THIS MEANS, SAID ONCE AND PLAINLY, because it is the security consequence of
# what was asked for and it should not be discovered later: this process can now
# spend the Gridcoin wallet. Before this change it could not -- a payout failed with
# rpc code -4 and the wallet stayed shut. After it, anything that can run code in
# this worker can move those coins, and the passphrase is in its environment.
#
# The narrowing that is available, and all of it is applied:
#   - the passphrase is read from the ENVIRONMENT at use time, never stored in this
#     repo, never written to a file by this code, never placed in argv, never logged
#   - the full unlock lasts DEFAULT_UNLOCK_SECONDS (60) and not until shutdown
#   - the wallet is RE-LOCKED and returned to staking in a `finally`, so it happens
#     on success, on a failed send, and on Ctrl-C
#   - a missing passphrase REFUSES the payout rather than attempting a send that
#     would fail anyway, and says which variable to set
# BOTH CONSTANTS NOW LIVE IN chains/gridcoin_wallet_lock.py AND ARE RE-EXPORTED HERE.
# services/swap_service.py needs them for the same lock cycle around getnewaddress,
# and this module already imports swap_service -- so defining them here and importing
# back would be a cycle. They are imported at the top of this file; these names stay
# bound so every existing caller and test keeps working, with ONE definition rather
# than two spellings (rule 8).
_WALLET_UNLOCK_CONSTANTS_LIVE_IN = "chains/gridcoin_wallet_lock.py"


# What the payouts row reads while the swap reads the key. The two move together in
# both paths; see _record_broadcast() for the run that proved a literal wrong here.
_PAYOUT_STATUS_BEFORE = {"paying": "created", "failed": "failed"}


def _record_broadcast(db, swap, amount, txid: str, *, old_status: str = "paying") -> None:
    """Make a delivered payout durable. Commits. Called INSIDE the unlock context.

    A function rather than inline, so the ordering that matters can be asserted
    directly (rule 10): everything here must be committed before the wallet's
    re-lock is attempted, because the re-lock can raise and a raised re-lock used to
    discard the txid of a payment that had already left the wallet.

    THE DESTINATION ASSET IS DERIVED HERE, NOT PASSED. It was a parameter until ruff
    put the count at six (PLR0913), and the honest fix was to remove an argument
    rather than to suppress the finding (rule 19). It is always swap["to_asset"] --
    payout_service.py:181 is the only place it was ever computed -- so passing it
    added a way for a caller to name one asset while handing over another swap's row,
    on the function that releases inventory against that asset. One fewer argument
    and one fewer disagreement.

    `old_status` IS KEYWORD-ONLY AND A PARAMETER BECAUSE THERE IS A SECOND CALLER,
    which is the one
    that made this function worth having. settle_payout.py corrects a swap whose
    payout was delivered but recorded as failed -- the exact record this function's
    original bug produced -- and for it the previous status is `failed`, not
    `paying`. Writing an audit row that claims `paying -> completed` for a swap that
    has been sitting in `failed` would falsify the one trail that explains the
    correction. Everything else it does is identical, including the inventory
    release, which the failure path never performed: a payout that failed left its
    reservation standing, so a correction that skipped release_inventory_after_send()
    would leave the hot wallet permanently short on paper.
    """
    destination_asset = swap["to_asset"]
    # THE PAYOUT ROW'S PRIOR STATUS IS DERIVED FROM THE SWAP'S, not filtered on a
    # literal. This line read `AND status = 'created'` and that was a bug with a
    # measurement behind it: on 2026-09-26 settle_payout.py corrected a swap whose
    # payout row the failure path had already moved to `failed`, so this UPDATE
    # matched NOTHING. The swap read `completed` with its txid while its payout row
    # still read `status failed  txid (none)` -- an internally inconsistent record,
    # which is worse than the one it was correcting.
    #
    # The two statuses move together in both paths and always have: the live path has
    # swap `paying` / payout `created`, and the failure path sets swap `failed` AND
    # payout `failed` (this file, the except clause above). So one mapping, in one
    # place, rather than a second parameter -- which also keeps the argument count
    # under PLR0913 without a suppression.
    #
    # It stays a FILTER rather than becoming an unconditional UPDATE, because a swap
    # can hold an old `failed` payout row beside a new `created` one after a retry,
    # and `WHERE txid IS NULL` alone would write the same txid onto both.
    payout_status_before = _PAYOUT_STATUS_BEFORE[old_status]
    db.execute(
        "UPDATE payouts SET txid = ?, status = ?, sent_at = ? WHERE swap_id = ? AND status = ? AND txid IS NULL",
        (txid, "broadcast", utc_now_iso(), swap["id"], payout_status_before),
    )
    db.execute(
        "UPDATE swaps SET payout_txid = ?, completed_at = ?, updated_at = ? WHERE id = ?",
        (txid, utc_now_iso(), utc_now_iso(), swap["id"]),
    )
    if not set_swap_status(db, swap["id"], "completed", "Payout broadcast", old_status=old_status):
        # THE MONEY IS ALREADY ON THE CHAIN when this fires from the worker, so this is the
        # one of the three that must be loudest and still must not raise: the payouts row,
        # swaps.payout_txid and swaps.completed_at above are already written, and an exception
        # here would leave a broadcast payout looking like a failed cycle.
        #
        # THE EXPECTED WAY TO SEE THIS IS settle_payout.py'S STATE B, and it is not an error
        # there. That tool corrects a swap whose payout was delivered but recorded as failed,
        # and passes old_status="failed" in BOTH of its states -- in state B the swap half of
        # the correction already ran, so the swap reads 'completed' and the CAS declines. What
        # it declines to write is a DUPLICATE audit row claiming failed -> completed a second
        # time, which is exactly the false trail the compare-and-swap exists to stop. The
        # payouts row this state B exists to fix is updated above, before this line.
        logger.error(
            "swap %s: the payout txid, completed_at and the payouts row ARE recorded, but the "
            "status was NOT moved to 'completed' and no audit row was written -- the swap is "
            "no longer %r. If this came from settle_payout.py correcting an already-corrected "
            "swap, that is expected and the duplicate audit row was refused on purpose; from "
            "payout_worker it means something moved the swap out of a committed payout claim. "
            "Read it back with show_swap.py --swap %s.",
            swap["id"], old_status, swap["id"],
        )
    release_inventory_after_send(db, destination_asset, amount)
    db.commit()


def payout_unlock_context(asset: str, adapter):
    """A context manager that holds the wallet open for ONE send, or explains why not.

    Returns nullcontext() for every chain that does not need it, so the call site
    reads the same for all of them and no chain grows a special case at the send.

    Raises PayoutUnlockUnavailable when the chain needs a passphrase and none is
    set. Raising BEFORE the send is deliberate: attempting it would fail with rpc
    code -4 and mark the swap terminally failed, so the swap would die of a missing
    environment variable. This way the reason is named and nothing is claimed.
    """
    if asset not in WALLET_UNLOCK_ASSETS:
        return nullcontext()

    passphrase = os.environ.get(WALLET_UNLOCK_ENV_VAR, "")
    if not passphrase:
        raise PayoutUnlockUnavailable(
            f"{asset} payouts need the wallet fully unlocked, and {WALLET_UNLOCK_ENV_VAR} is "
            f"not set in this process's environment. A {asset} wallet left unlocked for staking "
            f"CANNOT send -- the daemon answers rpc code -4 -- so this refuses before attempting a "
            f"send that would fail and mark the swap terminally failed. Set "
            f"{WALLET_UNLOCK_ENV_VAR} for the worker process only."
        )
    return unlocked_for_payout(adapter, passphrase)


class PayoutUnlockUnavailable(RuntimeError):
    """The wallet cannot be unlocked, so no send was attempted.

    Its own type because it is categorically different from a send that FAILED: no
    transaction was created, nothing reached any daemon, and the fix is an
    environment variable rather than an investigation.
    """


def inventory_note(adapters: dict, present: set) -> str:
    """What `inventory_rows` means, with its REAL denominator. THE decision, so it is testable.

    THE LINE THIS REPLACES TOLD THE OPERATOR A HEALTHY SYSTEM WAS BROKEN. It read:

        inventory_rows should be 3 (BTC/LTC/GRC); fewer means a getbalance call is
        failing and refresh_wallet_inventory swallowed it

    Measured on the operator's host 2026-10-02, where it printed beside
    `inventory_rows=1`. Wrong three ways at once:

      THE DENOMINATOR IS NOT 3. refresh_wallet_inventory() iterates
      `adapters.items()`, so it is however many adapters were CONSTRUCTED. BTC and
      LTC were unconfigured, so no adapter existed for either and no row could ever
      appear. A hardcoded count is rule 3's missing denominator in its most direct
      form: a number compared against something it was not counted out of.

      THE NAMES WERE STALE. BTC/LTC/GRC predates SOL and XRP joining the adapter
      table. On that host the constructed adapters were GRC and SOL.

      "SWALLOWED" WAS FALSE, and it is the clause that does the damage. SOL's
      get_balance() refuses by design when SOL_HOT_WALLET is unset -- that is a
      configuration fact the start banner already prints, not a lost error -- and the
      except branch is not silent either: it logs a WARNING naming the asset and the
      reason, once per process. So the annotation accused the code of the exact defect
      the code had been written to avoid, and sent a reader looking for a swallowed
      exception that was not there.

    An annotation that makes a working system read as broken is worse than no
    annotation: it spends the operator's attention and teaches them to discount the
    next one. Rule 14 asks for what the number MEANS next to the number, and this is
    the half of that rule that is easy to satisfy wrongly.

    PURE, and takes `present` rather than reading the table, so a test can assert
    every combination without a database.
    """
    configured = sorted(adapters)
    if not configured:
        return (
            "inventory_rows=0 is CORRECT: no adapter is constructed at all, so there is "
            "nothing to poll. Check the start banner for which assets are unconfigured"
        )
    missing = [asset for asset in configured if asset not in present]
    if not missing:
        return (
            f"inventory_rows={len(configured)} is every constructed adapter "
            f"({', '.join(configured)}), so nothing is missing"
        )
    return (
        f"expected {len(configured)} -- one per CONSTRUCTED adapter "
        f"({', '.join(configured)}), not a fixed number. Missing: {', '.join(missing)}. "
        f"A missing row means that asset's get_balance() raised; it is NOT swallowed -- "
        f"refresh_wallet_inventory logs a WARNING naming the asset and the reason, once "
        f"per process, so look for it earlier in this log. An asset whose hot wallet is "
        f"unset refuses by design and is expected here"
    )


def inventory_assets(db) -> set:
    """Which assets actually have a wallet_inventory row. The `present` half of the note."""
    rows = db.execute("SELECT asset FROM wallet_inventory").fetchall()
    return {str(row["asset"]) for row in rows}


def refresh_wallet_inventory(db, adapters: dict):
    now = utc_now_iso()
    for asset, adapter in adapters.items():
        try:
            balance = float(adapter.get_balance())
        except Exception as exc:  # noqa: BLE001 -- checked: one chain being unreachable must not stop the other two from being refreshed, so this continues rather than raising. It is NOT silent: the row for that asset keeps its previous values and the WARNING below says which asset and why, so a stale inventory figure can be traced to the poll that failed instead of looking like a balance that did not move.
            signature = (asset, str(exc)[:200])
            if signature not in _REPORTED_INVENTORY_FAILURES:
                _REPORTED_INVENTORY_FAILURES.add(signature)
                logger.warning(
                    "wallet inventory for %s NOT refreshed (previous values kept), and this is said "
                    "ONCE per process rather than every cycle: %s", asset, exc
                )
            continue
        row = db.execute("SELECT * FROM wallet_inventory WHERE asset = ?", (asset,)).fetchone()
        reserved = float(row["hot_reserved"]) if row else 0.0
        available = balance - reserved
        if row:
            db.execute(
                "UPDATE wallet_inventory SET hot_confirmed = ?, hot_available = ?, updated_at = ? WHERE asset = ?",
                (balance, available, now, asset),
            )
        else:
            db.execute(
                "INSERT INTO wallet_inventory (asset, hot_confirmed, hot_reserved, hot_available, updated_at) VALUES (?, ?, ?, ?, ?)",
                (asset, balance, reserved, available, now),
            )
    db.commit()


def payable_assets(adapters, allowed_pairs) -> set[str]:
    """Which assets a payout could actually be BROADCAST on, right now.

    THREE things, each necessary and none sufficient: the asset must be the TO leg
    of an allowed pair, this process must have an adapter for it, and that adapter
    must be able to SIGN.

    THE THIRD ONE WAS MISSING UNTIL 2026-10-02 AND THIS FUNCTION REPEATED ITS OWN
    DEFECT. Measured on the operator's host the moment they exported XRP_RPC_URL:
    supervisor.py's spawn_warning() printed, immediately above spawning three
    workers,

        about to spawn    a payout worker CAN broadcast on GRC, XRP. Stop now if
                          this database is pointed at a funded mainnet wallet.

    while the same process's own customer page said of GRC -> XRP:

        XRP cannot pay out: it holds no signing key, and services/payout_service.py
        calls send_to_address() without the arming token, so an XRP payout raises
        and the swap lands in `failed` with the deposit already credited.

    Both sentences, in one process, about one asset. XRP holds no signing key, so
    nothing could be broadcast on it and the banner named it anyway.

    That is the identical failure the paragraph below records -- the banner claiming
    a payout capability the process does not have -- which is why the signature
    changed from `configured_assets` (asset NAMES) to `adapters`: the old parameter
    made the question unaskable. A set of strings cannot be asked whether it can
    sign, so the check could not have been written without changing the shape, and
    passing names rather than adapters is what let the defect be reintroduced by a
    function written to fix it.

    WHY THIS EXISTS, MEASURED ON THE OPERATOR'S HOST 2026-10-01. supervisor.py's
    spawn_warning() printed, immediately above spawning three workers:

        about to spawn    a payout worker CAN broadcast. Stop now if this
                          database is pointed at a funded mainnet wallet.

    GRC_RPC_PASS was unset, so chains/registry had built exactly one adapter --
    SOL -- and on 2026-10-01 SOL was never a TO asset, so NOTHING could be paid
    out at all. (THAT PARENTHETICAL HAS BEEN WRONG TWICE AND IS CORRECTED RATHER
    THAN EXTENDED A THIRD TIME. It first said "because chains/solana.py cannot
    sign", which stopped being true on 2026-10-02 when the payout path was built.
    It then said the operator had not enabled an output pair, which stopped being
    true later on 2026-10-03: 79c4808 added ("GRC","SOL"), ("BTC","SOL") and
    ("LTC","SOL") to ALLOWED_PAIRS. What remains true is the only part this
    function is about -- with no GRC adapter built, a GRC -> SOL swap has no
    source chain either.) The banner said the opposite, in the
    direction that costs rounds: the operator had just been told NOT READY by
    swap_readiness.py one screen earlier.

    THE SAME DEFECT I HAD ALREADY FIXED, ONE CASE OVER. spawn_warning() derives
    its sentence from unlock_readiness_lines(), which reports a MISSING PASSPHRASE
    for a configured payout chain. With no payout chain configured at all that
    function correctly returns nothing -- it is asked about unlock state, not
    about existence -- so "no blockers" and "nothing to block" rendered the same
    way. Rule 14's "make did-nothing look different from did-work", at the level
    of a capability.

    `allowed_pairs` is PASSED rather than read from Config here, for the reason
    unlock_readiness_lines() takes `configured_assets`: the caller decides what it
    is describing, and this cannot disagree with the pair list printed beside it
    (rule 8).
    """
    destinations = {to_asset for _, to_asset in allowed_pairs}
    reachable = destinations & set(adapters)
    # AND THE ADAPTER MUST BE ABLE TO SIGN, which this function did not ask until
    # 2026-10-02 and which is the whole difference between "an adapter exists" and
    # "a payout could be broadcast".
    #
    # why_cannot_pay_out() IS THE AUTHORITY AND IS NOT RE-DERIVED HERE (rule 8).
    # services/pair_view.py:77 and services/swap_service.py:354 already read it;
    # this was the third spelling of the same question and the only one that
    # answered differently, so it now reads the same function rather than a fourth
    # copy of the rule.
    return {asset for asset in reachable if not why_cannot_pay_out(adapters, asset)}


def unlock_readiness_lines(configured_assets) -> list[str]:
    """Whether each payout-unlock chain CAN be unlocked, for a startup banner. PURE-ish.

    Reads os.environ and nothing else. Never returns the passphrase, never
    returns its length, and never claims it is CORRECT -- only that one is
    present. "Set" and "works" are different claims and this makes the weaker
    one (rule 17); a wrong passphrase still fails at the send, and saying
    otherwise here would be the reassuring answer rather than the measured one.

    WHY THIS EXISTS, MEASURED THREE TIMES ON 2026-10-01. The operator ran a
    devnet SOL -> testnet GRC rehearsal. All three times the whole pipeline
    worked -- memo attributed, deposit credited, swap advanced, payout worker
    claimed it -- and all three times the GRC leg died on

        GRIDCOIN_WALLET_PASSPHRASE is not set in this process's environment

    because the supervisor had been started from a shell without it. And all
    three times supervisor.py's start banner had said, immediately above the
    spawn:

        about to spawn    a payout worker CAN broadcast. Stop now if this
                          database is pointed at a funded mainnet wallet.

    Which was false in the direction that cost three rounds: it could not
    broadcast at all. The banner warned about the danger of succeeding while
    saying nothing about a guaranteed failure, and the environment is exactly
    the kind of parameter rule 14 says to echo -- "echo the parameters that
    decide the answer", because a worker inherits the shell that spawned it and
    nothing downstream can see which shell that was.

    `configured_assets` is PASSED IN rather than read from Config here, so the
    caller decides what "configured" means and this function cannot disagree
    with the chain lines printed beside it (rule 8). A chain that has no adapter
    gets no line: a passphrase warning for a chain nobody set up is the
    cried-wolf noise this file already fixed once for XRP's get_balance().
    """
    lines = []
    for asset in sorted(WALLET_UNLOCK_ASSETS & set(configured_assets)):
        if os.environ.get(WALLET_UNLOCK_ENV_VAR, ""):
            lines.append(
                f"  {asset} payout unlock  {WALLET_UNLOCK_ENV_VAR} IS set in this process, so a payout can "
                f"attempt the unlock. NOT a claim that it is the right passphrase -- a wrong one still "
                f"fails at the send."
            )
        else:
            lines.append(
                f"  {asset} payout unlock  *** {WALLET_UNLOCK_ENV_VAR} IS NOT SET *** so every {asset} payout "
                f"WILL refuse before sending and the swap will land in 'failed', which nothing retries. A "
                f"{asset} wallet unlocked for staking cannot send (rpc code -4). Export it in the shell that "
                f"starts this process; a value set in a file, or in another shell, does not reach here."
            )
    return lines
