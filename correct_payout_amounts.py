#!/usr/bin/env python3
"""Correct `payouts` rows that record an amount their chain cannot express. Dry run by default.

Role: file (operator entry point at the project root per CLAUDE.md rule 10; the
      decision is plan_for_payout() below and is callable with seeded rows)
Reads: swap_terminal.db (payouts joined to swaps) and, unless --no-chain is
      given, ONE read-only transaction lookup per row that needs correcting --
      `gettransaction` on BTC/LTC/GRC, the XRP Ledger's `tx`, Solana's
      `getTransaction`. No .env, no key material, no keypair file, no price feed.
Writes: with --apply ONLY -- `payouts.amount` on rows that carry a txid, and one
      `swap_audit_log` row per correction, in the same transaction as the
      correction. Nothing else: no status, no swap column, no deposit row, no
      inventory, no quote.
Can move funds: NO. It signs nothing, broadcasts nothing, constructs no send
      call and passes no arming token. Every chain call it makes is a read, and
      the transactions it reads are already final and cannot be unsent.
Mainnet-safe: the dry run is read-only in both directions -- it writes no
      database row and asks only for transactions by id. --apply rewrites a
      LEDGER ROW, which is why every correction carries an audit row preserving
      the original figure: see WHY A SILENT OVERWRITE IS UNACCEPTABLE below.

=============================================================================
THE DEFECT, MEASURED ON THE OPERATOR'S HOST 2026-10-03
=============================================================================

chains/payout_quantization.quantize_for_chain() landed in 54892d5 because the
`payouts` row recorded the amount the QUOTE computed, not the amount the CHAIN
sent. It fixed the path forward. Run against the operator's existing rows, it
says all 23 of 23 record an amount their chain cannot express. 15 carry a txid
-- the money left -- and 8 are `status=failed` with `txid=(none)`.

The 15, as measured, with what the chain could actually send:

    id=3  s_5ca7e29438479d29 GRC 55.52645238097888     -> 55.52645238
    id=6  s_aaa81fa6e8538163 GRC 82.65089987734812     -> 82.65089987
    id=7  s_c7dbce4be8b0efc2 GRC 87.97839509528555     -> 87.97839509
    id=8  s_e397e7d6b66830b7 GRC 87.86977058217977     -> 87.86977058
    id=9  s_95a807c181644190 GRC 88.59758626203558     -> 88.59758626
    id=10 s_e820c23626002c37 GRC 3086.581539002641     -> 3086.581539
    id=11 s_c992993c19d28bef GRC 3110.9183687421837    -> 3110.91836874
    id=14 s_1a44ddb70c118d13 SOL 0.0007616540576794097 -> 0.000761654
    id=15 s_45175de92a535066 SOL 0.0007616540576794097 -> 0.000761654
    id=17 s_6cd1a920cbe5739e GRC 2701.3495803173805    -> 2701.34958031
    id=19 s_2daaceb70dadc1d4 LTC 1.1996736819422498    -> 1.19967368
    id=20 s_02623852c1ea42cc LTC 1.2077457737321198    -> 1.20774577
    id=21 s_68f2cdef60e9252e BTC 0.004017802860711172  -> 0.0040178
    id=22 s_612fac62489f2122 GRC 55.44825689015776     -> 55.44825689
    id=23 s_539d922e9ef0a5d8 XRP 3.3155893288590605    -> 3.315589

ONE OF THE FIFTEEN IS INDEPENDENTLY CONFIRMED and fourteen are not. id=23's
transaction 799F8DED7CFB657411C5B1B9BE500C79CD62F07CF5634D2817804E89BDED7935
reads `Amount "3315589"` drops, `Fee "10"`, tesSUCCESS, validated true on the
XRP testnet -- 3.315589 XRP, exactly what the arithmetic above computes. The
operator's instruction was "let's correct the 15 with a txid in a batch as
well," and the fourteen unconfirmed rows are why this tool asks the chain
rather than trusting the one that was checked (rule 17: run the thing that
would show it false).

NOTE several payout rows can exist for ONE swap. ids 12/13 are failed rows for
the same swap as 14, and 18 for the same swap as 19, so this iterates PAYOUT
ROWS keyed by `payouts.id` and never swaps. A tool that assumed one row per
swap would correct one of each pair and silently leave the other.

=============================================================================
WHAT IT REFUSES TO TOUCH, AND WHY THE SKIP IS PRINTED RATHER THAN COUNTED
=============================================================================

A `failed` row with `txid=(none)` records a PROPOSAL -- an amount this desk
intended to send and did not. Quantizing it would make the record look like a
send that never happened, which is worse than the overstatement being
corrected, because the overstatement is at least about a real payment. So
those 8 rows are refused, named individually, with that reason beside each.

They are printed and not merely tallied because rule 13's "'skipped' plus
'success' in one output is a defect in the output" is exactly the failure this
tool could produce: "15 corrected, 8 skipped, exit 0" reads as a clean run
whether the 8 were the right 8 or not.

=============================================================================
WHY A SILENT OVERWRITE IS UNACCEPTABLE, AND WHAT THE AUDIT ROW HOLDS
=============================================================================

`payouts.amount` is the desk's record of what a customer was paid. Rewriting it
in place with no trail would mean the only surviving evidence of what the row
used to say is this repository's git history -- which is not in the database, is
not on the operator's screen, and is not available to anyone reconciling a row
against a chain explorer a year from now.

So every correction writes one `swap_audit_log` row IN THE SAME TRANSACTION,
carrying the original figure, the corrected figure, the exact difference, the
txid, and WHICH AUTHORITY the new figure came from. The original is recoverable
from the database alone. The column names are read off db.py's schema rather
than recalled -- (swap_id, old_status, new_status, message, created_at) -- the
same way resolve_halted_swap.py does it, because a guess at that shape cost a
failed query in this tree once already.

THE AUDIT ROW IS NOT A STATUS TRANSITION and does not pretend to be one. Both
status columns carry the swap's CURRENT status, unchanged, and the message says
so: this tool corrects an amount and moves nothing.

=============================================================================
CHAIN-VERIFIED AND ARITHMETIC-ONLY ARE DIFFERENT CLAIMS
=============================================================================

The claim being written is "this is what the chain sent." The strongest form
asks the chain:

    CHAIN-VERIFIED    chains/payout_on_chain.delivered_to_destination() read the
                      transaction and the figure is the chain's own. The printed
                      line names the method and the field, and the audit row
                      carries that sentence.
    ARITHMETIC ONLY   the chain could not be asked -- no adapter, a daemon that
                      refused, a pruned history -- so the figure is
                      quantize_for_chain()'s derivation of what the adapter
                      would have sent. The line says *** NOT VERIFIED ON CHAIN
                      ***, with the reason the chain could not answer.

Both are honest corrections and they are NOT interchangeable, so they never
print the same way and the audit rows never read the same way.

A DISAGREEMENT REFUSES THE ROW, LOUDLY, AND CORRECTS NOTHING. If a chain
answers with a figure that is not what quantize_for_chain() computes, then the
quantizer is wrong about that chain -- and that function decides what every
future payout records, reserves and sends. That finding is bigger than any one
row, so this tool prints both numbers and writes neither. Picking one would
bury it.

=============================================================================
THE DISAGREEMENT HAPPENED, IT IS EXPLAINED, AND THERE IS NOW A FLAG FOR IT
=============================================================================

Run with --apply against the operator's host on 2026-10-03 it corrected 12 rows
and REFUSED 3, all GRC, for exactly that disagreement. The refusal was right and
the explanation came afterwards, which is the order this tool was built for.

WHAT THE THREE ROWS ARE. Gridcoin's daemon ROUNDS an over-precise amount, HALF
UP, where chains/coin_amounts.fit_to_chain_precision() TRUNCATES. Measured over
all 9 of the operator's GRC payout rows: 9 of 9 fit round-half-up and only 6 of
9 fit truncation, and the 6 are exactly the rows whose remainder beyond the
eighth decimal is below 0.5 -- where the two roundings cannot differ. The three
that discriminate are the three that were refused:

    payouts.id=6   recorded 82.65089987734812   chain 82.65089988   quantizer 82.65089987
    payouts.id=7   recorded 87.97839509528555   chain 87.9783951    quantizer 87.97839509
    payouts.id=17  recorded 2701.3495803173805  chain 2701.34958032 quantizer 2701.34958031

One satoshi of GRC each. The cause, verified in Gridcoin 5.5.1.0's own source
rather than inferred, and the full four-behavior table of what each daemon does
with an over-precise amount, are in chains/payout_quantization.py's header. THE
THREE CHAIN FIGURES ABOVE ARE THE OPERATOR'S MEASUREMENT, read off their own
daemon; nothing in this container can reach it and they were not re-run here
(rule 17).

SO THE QUANTIZER IS NOT WRONG ABOUT GRC'S PRECISION -- it is right that eight
decimals is what the chain can express, and the payout service now quantizes
BEFORE the send (54892d5) so no future GRC payout can reach the daemon's
rounding at all. These three were sent before that fix existed. The operator's
2026-10-03 decision to make every chain truncate did NOT resolve them, and must
not be read a month from now as having done so: the quantizer still truncates,
the chain still rounded, and the disagreement on these three rows is permanent.

WHICH IS WHY THE FLAG IS NAMED FOR WHAT IT TRUSTS AND NOT FOR WHAT IT
OVERRIDES. --trust-the-chain-over-the-quantizer corrects such a row to THE
CHAIN'S OWN FIGURE. It is not --force and it is not general: it reaches the
chain/quantizer disagreement and nothing else. Without it, nothing about this
tool's behavior changes -- the row is still refused and the run still exits 5.

=============================================================================
WHAT THE FLAG DOES NOT REACH, AND WHY THAT IS STRUCTURAL RATHER THAN A CHECK
=============================================================================

chains/payout_on_chain.py refuses to READ a transaction in nine distinct
situations -- a partial XRP payment (delivered_amount below the intended
Amount), a Solana balance delta of zero or less, two outputs paying the
destination different values, no output paying the destination at all, a
response with neither a `details` nor a `vout` list, an unvalidated or failed
transaction, a pruned history, an unreadable cluster answer, and a row with no
txid. EVERY ONE of them returns ChainAmount(None, why).

A None amount cannot reach the disagreement branch, because that branch sits
below `if chain.amount is None` and only ever compares two real numbers. So
those nine are not refusals this flag could widen to even by accident: they
remain what they are today, ARITHMETIC ONLY corrections that carry the chain's
reason for not answering into the audit row. The flag's reach is bounded by the
shape of the data rather than by a list of exclusions that could fall out of
date, and there is a test asserting it per refusal kind.

THE COMPARE-AND-SWAP IS ALSO UNTOUCHED. A row that changed between the read and
the write is refused by apply_correction() regardless of this flag, which is a
third kind of refusal and the one that protects against a concurrent writer.

=============================================================================
ONE TRANSACTION PER ROW, AND WHY NOT ONE FOR ALL FIFTEEN
=============================================================================

Each row is an independent claim about a different transaction on a different
chain, and establishing it costs a network round trip that can hang, time out
or be interrupted. Rule 5's reasoning applies directly: "a stage that writes
its rows to SQL as it goes and emits the file at the end degrades into a
partial result when it is interrupted. A stage that accumulates in memory and
writes both at the end loses everything."

So the commit boundary is the ROW. Interrupted after the seventh, seven rows
are corrected, each with its audit row, and the remaining eight are untouched
and will be corrected by the next run -- which is safe precisely because the
tool is idempotent (a row whose amount already equals the chain's figure is
skipped, so a second run corrects nothing).

WHAT MUST BE ATOMIC IS THE PAIR, not the batch: a corrected amount with no
audit row would destroy the original figure, so the UPDATE and the INSERT are
in one transaction and commit together. A batch-wide transaction would buy
nothing -- there is no invariant that spans two payout rows -- and would cost
every completed correction on an interruption.

THE UPDATE IS A COMPARE-AND-SWAP on the amount AND the txid, so a row that
changed between the read and the write matches zero rows, writes no audit row
either, and is reported as refused rather than silently reapplied.

=============================================================================
THE DATABASE PATH IS ECHOED LOUDLY, AND THAT IS NOT DECORATION
=============================================================================

show_swap.py's header already names the confusion: `SWAP_DB_PATH` is often
unset, so a tool run in one shell and a worker started in another can read
different files, and the symptom is a count that disagrees with what the
operator is looking at. This tool WRITES, so the same confusion would mean
correcting rows in a database nothing reads. The resolved path is printed on
the second line, before anything is read, and again beside the final count.
"""

from __future__ import annotations

import argparse
import sys
import time
from decimal import Decimal
from pathlib import Path
from typing import NamedTuple

sys.path.insert(0, str(Path(__file__).resolve().parent / "swap_terminal"))

from chains.payout_on_chain import ChainAmount, delivered_to_destination
from chains.payout_quantization import quantize_for_chain
from chains.registry import build_adapters
from config import Config
from db import db_session
from microfortnights import format_duration
from report_block import labeled
from services.helpers import utc_now_iso

SELF = "correct_payout_amounts.py"

#: Every payout row, with the swap status the audit row has to record. ORDERED BY
#: p.id so a run's output is comparable against the previous run's line by line,
#: which is how this repository's own baseline-diffing instruction asks that
#: output be read.
#:
#: EVERY ROW IS SELECTED, including the ones with no txid, because a tool that
#: filtered them in SQL could not NAME them as skipped -- and naming them is the
#: point (see WHAT IT REFUSES TO TOUCH in this module's docstring).
PAYOUT_SQL = """
SELECT p.id                 AS id,
       p.swap_id            AS swap_id,
       p.asset              AS asset,
       p.destination_address AS destination_address,
       p.amount             AS amount,
       p.txid               AS txid,
       p.status             AS status,
       s.status             AS swap_status,
       s.to_asset           AS swap_to_asset
FROM payouts p
JOIN swaps s ON s.id = p.swap_id
ORDER BY p.id
"""

CORRECT = "CORRECT"
SKIP = "SKIP"
REFUSE = "REFUSE"

#: Where a corrected figure came from. Printed per row and written into the audit
#: row, because a row corrected from the chain and a row corrected from
#: arithmetic are different claims (see this module's docstring).
FROM_CHAIN = "CHAIN-VERIFIED"
FROM_ARITHMETIC = "ARITHMETIC ONLY"

#: The third authority, reachable ONLY with --trust-the-chain-over-the-quantizer.
#: It is a separate string rather than FROM_CHAIN because the two are different
#: claims: FROM_CHAIN means the chain and the quantizer AGREED and the chain's
#: figure was taken, and this one means they did NOT agree and the chain's figure
#: was taken anyway, on an operator's explicit instruction. A reader a year from
#: now has to be able to tell those apart in the audit row, which is the whole
#: reason the audit row names its authority at all.
FROM_CHAIN_OVER_QUANTIZER = "CHAIN OVER QUANTIZER (they disagreed)"

#: The flag, spelled once so the parser, the refusals and the footer cannot drift
#: apart (rule 8). NAMED FOR WHAT IT TRUSTS, not for what it overrides: `--force`
#: would be a claim about this tool's own checks, where this is a claim about
#: which of two authorities is right about one chain, and only that.
TRUST_CHAIN_FLAG = "--trust-the-chain-over-the-quantizer"

#: What each authority prints beside the figure it produced. A DICT RATHER THAN AN
#: `if`, so adding a fourth authority without deciding how it prints raises a
#: KeyError here instead of silently inheriting the blank marker that means "the
#: chain confirmed this" (rule 14: make the states look different).
AUTHORITY_MARKERS = {
    FROM_CHAIN: "",
    FROM_ARITHMETIC: "*** NOT VERIFIED ON CHAIN ***  ",
    FROM_CHAIN_OVER_QUANTIZER: "*** CHAIN AND QUANTIZER DISAGREED -- CHAIN TAKEN AS AUTHORITATIVE ***  ",
}

#: The ChainAmount handed to plan_for_payout() before any chain has been asked.
#: A sentence rather than an empty string so that a row which turns out not to
#: need correcting still has a readable provenance in the dry run.
NOT_ASKED = ChainAmount(None, "the chain was not asked (--no-chain, or the row needs no correction)")


class Correction(NamedTuple):
    """What this tool would do to one payout row, and why. The whole verdict.

    `corrected` is None for every verdict that writes nothing, so a caller cannot
    accidentally write a figure for a skipped or refused row.
    """

    verdict: str
    recorded: float
    corrected: float | None
    authority: str
    why: str


def plan_for_payout(row, chain: ChainAmount, *, trust_chain_over_quantizer: bool = False) -> Correction:
    """What to do with one payout row, given what the chain said. THE DECISION.

    At the bottom where it can be called with seeded rows and a seeded chain
    answer (rule 10). It performs no I/O: the caller does the reading, which is
    what makes every branch below testable without a daemon.

    THE ORDER OF THE FIVE BRANCHES IS THE DESIGN, and each one is a different
    fact about the row:

      no txid           the row records a PROPOSAL. Correcting it would make an
                        intention look like a send. Refused first, before any
                        arithmetic, because the amount is not a claim about a
                        chain at all.
      already agrees    quantize_for_chain() returns the recorded figure
                        unchanged, so the row already records what the chain can
                        express. This is what makes a second run correct nothing,
                        and it is checked BEFORE the chain comparison so an
                        already-corrected row costs no round trip.
      chain not asked   a correction from arithmetic alone, SAYING SO. The
                        quantizer is this terminal's own derivation of what the
                        adapter sent, which is weaker evidence than the chain's
                        own answer and is not nothing.
      chain disagrees   REFUSED, with both numbers -- unless
                        trust_chain_over_quantizer, which corrects to the
                        CHAIN's figure under its own authority. The quantizer
                        being wrong about a chain is a bigger finding than this
                        row, which is why the default refuses; see this module's
                        docstring for the disagreement that actually occurred
                        and for why an explicit flag rather than a default.
      chain agrees      corrected to the CHAIN's figure, not to the quantized
                        one. They are equal by the branch above; writing the
                        chain's own number is what makes the row's provenance
                        the observation rather than the arithmetic.

    `trust_chain_over_quantizer` DEFAULTS TO FALSE, AND THAT DEFAULT IS THE
    SAFETY PROPERTY, not a convenience. A caller that does not pass it gets
    2026-10-03's behavior exactly: the row is refused, nothing is written, and
    the run exits 5. It is keyword-only so no positional call can set it by
    accident, and there is a test that fails if the default flips.

    IT CANNOT WIDEN TO ANY OTHER REFUSAL, by the shape of the code rather than
    by a check. It is read in ONE branch, below `if chain.amount is None`, so
    every situation in which chains/payout_on_chain.py declines to read a
    transaction -- a partial XRP payment, a non-positive Solana delta, two
    outputs paying the destination different values, and the six others --
    arrives here as `chain.amount is None` and takes the ARITHMETIC ONLY branch
    above, flag or no flag.

    THE AGREEMENT TEST QUANTIZES THE CHAIN'S FIGURE TOO, rather than comparing
    raw floats. A chain's amount is already at its own precision, so quantizing
    it is a no-op -- that is the fixed-point property 54892d5 measured over
    60,016 amounts per chain -- and running both sides through the same function
    means the comparison is exact instead of depending on how a daemon's JSON
    happened to serialize. It also needs no second precision table (rule 11: if a
    chain had to be added to a second place by hand, that second place is the bug).
    """
    recorded = float(row["amount"])
    asset = row["asset"]
    txid = (row["txid"] or "").strip()
    if not txid:
        return Correction(SKIP, recorded, None, "", (
            # "MISSTATEMENT" RATHER THAN "OVERSTATEMENT" SINCE 2026-10-03. Every
            # correction this tool made used to reduce the figure, so the word was
            # exact; --trust-the-chain-over-the-quantizer writes a LARGER figure on
            # a row the chain rounded up, and that one is an understatement. The
            # argument is unchanged either way -- a proposal recorded as a send is
            # the worse record -- so only the noun had to stop claiming a direction.
            f"no txid, status={row['status']!r} -- this row records a PROPOSAL, not a send. Quantizing "
            f"it would make an amount this desk never sent look like one it did, which is a worse "
            f"record than the misstatement being corrected"
        ))
    quantized, note = quantize_for_chain(recorded, asset)
    if quantized == recorded:
        return Correction(SKIP, recorded, None, "", (
            f"{recorded!r} is already what {asset}'s chain can express, so there is nothing to correct "
            f"-- this is the row state a second run of this tool sees"
        ))
    if chain.amount is None:
        return Correction(CORRECT, recorded, quantized, FROM_ARITHMETIC, (
            f"{note}. THE CHAIN WAS NOT ASKED OR COULD NOT ANSWER: {chain.how}"
        ))
    if quantize_for_chain(chain.amount, asset)[0] != quantized:
        # BOTH FIGURES ARE IN THIS SENTENCE AND IT IS SHARED BY BOTH OUTCOMES, so
        # the refusal an operator reads and the audit row a reader finds a year
        # later cannot disagree about what the disagreement WAS (rule 8). The
        # correction below embeds it verbatim rather than restating it.
        disagreement = (
            f"THE CHAIN AND THE QUANTIZER DISAGREE. The chain says {chain.amount!r} {asset} "
            f"({chain.how}); chains/payout_quantization.quantize_for_chain({recorded!r}, {asset!r}) "
            f"says {quantized!r}"
        )
        if not trust_chain_over_quantizer:
            return Correction(REFUSE, recorded, None, "", (
                f"{disagreement}. NOTHING was written for this row: that disagreement means the "
                f"quantizer is wrong about {asset}, which decides what every FUTURE payout records, "
                f"reserves and sends -- a bigger finding than this row, and correcting the row would "
                f"bury it. To correct it to the CHAIN's figure anyway, re-run with "
                f"{TRUST_CHAIN_FLAG}"
            ))
        return Correction(CORRECT, recorded, chain.amount, FROM_CHAIN_OVER_QUANTIZER, (
            f"{disagreement}. THE CHAIN WAS TAKEN AS AUTHORITATIVE and {chain.amount!r} {asset} was "
            f"written, because {TRUST_CHAIN_FLAG} was passed. The quantizer's {quantized!r} is NOT "
            f"retracted by this: it is what this terminal would send today, and the chain's figure is "
            f"what the chain did send on a transaction that is already final and cannot be unsent. "
            f"The two differ for {asset} on a historical row because the daemon rounded an "
            f"over-precise amount that this terminal no longer hands it"
        ))
    return Correction(CORRECT, recorded, chain.amount, FROM_CHAIN, (
        f"the chain's own figure: {chain.how}. It matches quantize_for_chain({recorded!r}, {asset!r}) "
        f"= {quantized!r}, so the record and the ledger agree on the figure being written"
    ))


def difference(recorded: float, corrected: float) -> Decimal:
    """How much the record overstated (positive) or understated (negative) the send.

    Decimal(str(x)) rather than a float subtraction, for the reason
    chains/coin_amounts.amount_to_base_units() gives: a float subtraction of two
    near-equal figures prints a tail that is an artifact of binary floating point,
    and this difference is the number an operator reconciles against a chain
    explorer. Measured on the operator's own row id=23: the float subtraction of
    3.3155893288590605 - 3.315589 is 3.2885906046454236e-07, where the decimal
    answer is 0.0000003288590605 -- the same number, and only one of them can be
    compared against a drop count by eye.
    """
    return Decimal(str(recorded)) - Decimal(str(corrected))


def audit_message(row, plan: Correction) -> str:
    """The sentence the `swap_audit_log` row carries. It must survive without git.

    EVERYTHING NEEDED TO UNDO THIS BY HAND IS IN IT: the payout row's id, the
    original figure as it was stored, the figure written, the exact difference,
    the txid the claim is about, and which authority the new figure came from.
    Rule 1's reasoning applied to a database row rather than to a comment -- the
    only place a future reader will look is next to the thing that changed.
    """
    return (
        f"{SELF}: payouts.id={row['id']} amount CORRECTED from {plan.recorded!r} to "
        f"{plan.corrected!r} {row['asset']} (difference {difference(plan.recorded, plan.corrected):f} "
        f"{row['asset']}). txid={row['txid']}. Authority: {plan.authority} -- {plan.why}. The "
        f"recorded figure was the one the QUOTE computed; the chain cannot express it. No status, "
        f"amount basis or other column was changed by this correction and the swap stays "
        f"{row['swap_status']!r}."
    )


def apply_correction(db, row, plan: Correction) -> tuple[bool, str]:
    """Write one correction and its audit row in ONE transaction. (wrote, sentence).

    THE COMPARE-AND-SWAP IS ON THE AMOUNT AND THE TXID. A row whose amount changed
    between the read and this write matches zero rows, and because the audit
    INSERT is in the same transaction and is skipped with it, there is no way to
    produce an audit row for a correction that did not happen -- or a correction
    with no audit row, which is the one outcome this tool must never leave behind.

    Commits per row. See ONE TRANSACTION PER ROW in this module's docstring for
    why the boundary is here and not around the batch.
    """
    moved = db.execute(
        "UPDATE payouts SET amount = ? WHERE id = ? AND amount = ? AND txid = ?",
        (plan.corrected, row["id"], plan.recorded, row["txid"]),
    ).rowcount
    if not moved:
        db.rollback()
        return False, (
            f"REFUSED: payouts.id={row['id']} no longer holds {plan.recorded!r} with txid "
            f"{row['txid']}, so it changed between the read and the write. Nothing was written and no "
            f"audit row either -- both are in one transaction. Run the dry run again and look at the row"
        )
    db.execute(
        "INSERT INTO swap_audit_log (swap_id, old_status, new_status, message, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (row["swap_id"], row["swap_status"], row["swap_status"], audit_message(row, plan), utc_now_iso()),
    )
    db.commit()
    return True, (
        f"WROTE: payouts.id={row['id']} {plan.recorded!r} -> {plan.corrected!r} {row['asset']}, "
        f"audit row written in the same transaction, swap left {row['swap_status']!r}"
    )


def misstatement(recorded: float, corrected: float) -> str:
    """"overstated by X" or "UNDERSTATED by X", with the word chosen from the sign.

    A FIXED "overstated by" LABEL PRINTED A NEGATIVE NUMBER, and that was a
    defect this session INTRODUCED and then read in the output (2026-10-03):

        recorded    2701.3495803173805
        correct to  2701.34958032
        overstated by -0.0000000026195 GRC

    Every correction before --trust-the-chain-over-the-quantizer existed reduced
    the figure, because all five quantizers truncate -- so the label was right
    for every case that could occur and a constant was the honest way to write
    it. The flag creates the other case for the first time: Gridcoin ROUNDED UP,
    so the chain's figure is LARGER than the record, and the record was an
    UNDERSTATEMENT. A minus sign in front of a word that says the opposite is
    rule 14's "state what the number means, next to the number" inverted -- the
    reader has to notice the sign and then distrust the label.

    difference() was already honest about this: its docstring says "overstated
    (positive) or understated (negative)". It was only the printed label that
    assumed one direction, which is why the sign lives here and not there.

    UNDERSTATED IS SHOUTED AND OVERSTATED IS NOT, deliberately. An overstatement
    means the desk's record claimed more than it paid. An UNDERSTATEMENT means
    the chain moved MORE MONEY than the record admits, which is the direction an
    operator reconciling a wallet balance needs to see without looking for a
    minus sign.
    """
    amount = difference(recorded, corrected)
    # :f rather than the Decimal's own repr, which is exponent notation for a
    # figure this small -- 9.7888E-10 is the same number as 0.00000000097888 and
    # only one of them can be counted against a satoshi by eye (rule 14).
    if amount < 0:
        return f"UNDERSTATED by {-amount:f}"
    return f"overstated by {amount:f}"


def describe(row, plan: Correction) -> list[str]:
    """The lines one row prints. A chain-verified row and an arithmetic one differ here.

    Rule 14: the figures are printed with repr() and not reformatted, because the
    whole subject of this tool is the last digits -- services/payout_capacity.
    as_amount() truncates at eight decimals, which would hide the ninth decimal
    that IS the correction on the two SOL rows (0.000761654).
    """
    head = (f"{row['asset']:<4} payouts.id={row['id']:<4} swap={row['swap_id']}  "
            f"status={row['status']}")
    if plan.verdict != CORRECT:
        return [f"  {plan.verdict:<8} {head}", f"           {plan.why}"]
    marker = AUTHORITY_MARKERS[plan.authority]
    # "WHICH THE CHAIN CANNOT SEND" IS TRUE OF EVERY RECORDED FIGURE HERE -- each
    # one carries more decimals than its chain can express, which is what made it
    # correctable -- but it is not the whole story on a row the chain ROUNDED UP,
    # where the chain sent a figure the record understates. plan.why carries that
    # distinction in full and the line below now agrees with it rather than
    # asserting one direction.
    return [
        f"  {plan.verdict:<8} {head}",
        f"           recorded    {plan.recorded!r}  <- what the quote computed, which the chain cannot "
        f"express at its own precision",
        f"           correct to  {plan.corrected!r}  <- {marker}{plan.authority}",
        f"           {misstatement(plan.recorded, plan.corrected)} {row['asset']}"
        f"   txid={row['txid']}",
        f"           {plan.why}",
    ]


def plans_for(rows, adapters, *, ask_chain: bool, say,
              trust_chain_over_quantizer: bool = False) -> list[tuple[dict, Correction]]:
    """Every row paired with its verdict, asking the chain ONLY where a write would happen.

    TWO CALLS OF THE SAME PURE FUNCTION, which is the cheap way to get both
    properties the tool needs. The first asks "would this row be corrected at
    all", with NOT_ASKED standing in for the chain; only a row that answers
    CORRECT is worth a network round trip, so an already-corrected database costs
    zero RPC calls and a second run is both idempotent and silent on the wire. The
    second call re-decides with the chain's real answer, which is where a
    disagreement becomes a refusal.

    THAT SAVING IS MEASURED AND PINNED, not assumed to survive a refactor (rule
    3). On the operator's host on 2026-10-03, after --apply had corrected 12 of
    23 rows, the second dry run asked the chain 3 times rather than 23 -- once
    per row still needing a correction, which by then was only the three refused
    ones. tests/test_correct_payout_amounts.py counts the calls, because an
    innocuous-looking move of the `if` would turn a silent no-op run into 23
    network round trips with nothing on screen to say so.

    THE FLAG IS PASSED ONLY TO THE SECOND CALL'S DECISION, and the first call
    cannot be affected by it: NOT_ASKED carries `amount=None`, so the
    disagreement branch the flag governs is unreachable there.

    `say` is the printer, injected so a test can collect the progress lines rather
    than have them go to a terminal. The lines are part of what this function is
    for (rule 14): a chain read can hang for a daemon's whole timeout, and an
    operator watching a blinking cursor cannot tell that from a hung tool.
    """
    planned: list[tuple[dict, Correction]] = []
    for index, row in enumerate(rows, start=1):
        plan = plan_for_payout(row, NOT_ASKED)
        if plan.verdict == CORRECT and ask_chain:
            # THE WHOLE TXID, NOT A PREFIX. report_block.clipped() exists because a
            # value cut without saying so is three readings and no way to choose
            # (its docstring has the measurement), and a txid is the one string on
            # this line an operator will paste into an explorer.
            say(f"  asking the {row['asset']} chain  {index}/{len(rows)}  "
                f"payouts.id={row['id']}  txid={row['txid']}")
            chain = delivered_to_destination(
                adapters.get(row["asset"]), row["asset"], row["txid"], row["destination_address"],
            )
            plan = plan_for_payout(row, chain,
                                   trust_chain_over_quantizer=trust_chain_over_quantizer)
        planned.append((row, plan))
    return planned


def build_parser() -> argparse.ArgumentParser:
    return _parser_with_arguments(argparse.ArgumentParser(
        prog=SELF,
        description=("Correct `payouts` rows whose recorded amount the chain cannot express. Only rows "
                     "WITH a txid, verified against the chain where the chain can be asked. Dry run by "
                     "default."),
        epilog=("A row with no txid records a proposal rather than a send and is never corrected. A row "
                "whose chain answers with a figure the quantizer disagrees with is refused rather than "
                f"resolved either way, unless {TRUST_CHAIN_FLAG} says which of the two to believe. "
                "Every other kind of refusal is out of that flag's reach by construction."),
    ))


def _parser_with_arguments(parser: argparse.ArgumentParser) -> argparse.ArgumentParser:
    parser.add_argument("--db", default="", help=f"database to correct (default: {Config.DB_PATH})")
    parser.add_argument("--apply", action="store_true",
                        help="actually write. Without it nothing is written and every check still runs.")
    parser.add_argument("--no-chain", action="store_true",
                        help="ask no chain, so it is safe with every daemon down. Every correction is "
                             "then ARITHMETIC ONLY and says so per row and in its audit row.")
    # `dest` IS SPELLED OUT because argparse would derive
    # `trust_the_chain_over_the_quantizer` from the flag, and the keyword this
    # reaches on plan_for_payout() is `trust_chain_over_quantizer`. Two spellings
    # of one concept is rule 8's defect; naming the destination here means the
    # flag's wording can be read as English without the function's keyword having
    # to match it word for word.
    parser.add_argument(TRUST_CHAIN_FLAG, dest="trust_chain_over_quantizer", action="store_true",
                        help="when a chain's own figure disagrees with the quantizer, correct the row to "
                             "THE CHAIN's figure instead of refusing it. Still needs --apply to write. "
                             "This is NOT a general override: it reaches that one disagreement and no "
                             "other refusal. The audit row records both figures and says the chain was "
                             "taken as authoritative.")
    return parser


def _announce(args, db_path: str) -> None:
    """Everything that decides the answer, before anything is read (rule 14)."""
    print(f"{SELF}: {'APPLY -- rows WILL be written' if args.apply else 'DRY RUN -- nothing is written'}",
          flush=True)
    print(labeled("database", f"{db_path}  <- the rows corrected are THESE. SWAP_DB_PATH is often unset, "
                              f"so compare this against the path the workers were started with"),
          flush=True)
    print(labeled("chain", "NOT ASKED (--no-chain): every correction is arithmetic only"
                           if args.no_chain else
                           "asked, once per row that needs correcting, read-only "
                           "(gettransaction / tx / getTransaction)"), flush=True)
    print(labeled("writes", "payouts.amount on rows WITH a txid, plus one swap_audit_log row per "
                            "correction in the same transaction. Nothing else, and no status moves"),
          flush=True)
    print(labeled("disagreements", f"CORRECTED TO THE CHAIN'S FIGURE ({TRUST_CHAIN_FLAG}) -- a row whose "
                                   f"chain contradicts the quantizer is written from the chain, with "
                                   f"both figures in its audit row"
                                   if args.trust_chain_over_quantizer else
                                   f"REFUSED (the default) -- a row whose chain contradicts the quantizer "
                                   f"is left alone and this run exits 5. {TRUST_CHAIN_FLAG} is what "
                                   f"corrects one"), flush=True)
    print("  this tool SIGNS NOTHING and BROADCASTS NOTHING. The transactions it reads are final.",
          flush=True)


def _report(planned: list[tuple[dict, Correction]], *, applied: list[str]) -> None:
    """Print every row under its verdict, and never let a section print blank."""
    for verdict, meaning in (
        (CORRECT, "the recorded amount is not what the chain could send, and the row carries a txid"),
        (REFUSE, "something is wrong that is bigger than the row -- NOTHING was written for these"),
        (SKIP, "left exactly as they are, each for the reason beside it"),
    ):
        chosen = [(row, plan) for row, plan in planned if plan.verdict == verdict]
        print(f"\n{verdict}  {len(chosen)} row(s)  <- {meaning}", flush=True)
        if not chosen:
            print("  (none)", flush=True)
            continue
        for row, plan in chosen:
            for line in describe(row, plan):
                print(line, flush=True)
    print(flush=True)
    for line in applied:
        print(f"  {line}", flush=True)


def _rerun_command(args, db_path: str, *extra: str) -> str:
    """The command an operator pastes to repeat this run with `extra` added.

    IT ECHOES BACK THE FLAGS THAT DECIDED THIS ANSWER rather than printing a bare
    invocation (rule 14: "echo the parameters that decide the answer"). --db is
    included only when it was given, because the default is already printed on
    the `database` line above and a reader who did not pass it did not choose it.

    TRUST_CHAIN_FLAG IS ECHOED TOO, AND LEAVING IT OUT WAS A DEFECT I SHIPPED
    AND THEN READ (2026-10-03). With the flag on and three rows disagreeing, the
    footer counted 15 correctable and then offered:

        python3 correct_payout_amounts.py --apply --db ...

    which is a DIFFERENT run: without the flag it corrects 12 and refuses 3. The
    count and the command disagreed by three rows, which is the same defect as
    the "0 row(s)" footer this function was written to remove -- a sentence true
    about the run the reader just did and false about the one it tells them to
    do. Found by reading the output, which is the only place it was visible, and
    there is now a test.

    IT CANNOT BE ECHOED TWICE: the one caller that passes it as `extra` is in the
    branch reached only when `args.trust_chain_over_quantizer` is False.
    """
    flags = [f"--{word}" for word in ("no-chain",) if getattr(args, word.replace("-", "_"))]
    if args.trust_chain_over_quantizer:
        flags.append(TRUST_CHAIN_FLAG)
    if args.db:
        flags.append(f"--db {db_path}")
    return " ".join(["python3", SELF, *extra, *flags])


def _summary_line(db_path: str, *, rows: int, correctable: int, verified: int,
                  over_quantizer: int) -> str:
    """The one-line count. `(none)` when there is nothing to break down (rule 14).

    "0 OF THOSE 0" IS WHY THIS IS A FUNCTION. The line read `0 correctable of 23
    payout row(s); 0 of those 0 verified against the chain itself, 0 from
    arithmetic alone` on the operator's second run, which is arithmetically
    correct and reads like a defect -- three zeros and a division of an empty set
    into two empty halves. Rule 14 asks that an empty result print `(none)` rather
    than nothing; the companion is that a BREAKDOWN of an empty set is not a
    result either, and printing it makes a reader check whether the tool broke.

    So the breakdown appears only when there is something to break down. The
    count itself always appears, with its denominator, because `0 correctable of
    23` is the answer and is not an empty result.
    """
    if not correctable:
        return (f"0 correctable of {rows} payout row(s) in {db_path}; (none) to verify, so there is no "
                f"chain/arithmetic split to report")
    provenance = [f"{verified} of those {correctable} verified against the chain itself"]
    if over_quantizer:
        provenance.append(f"{over_quantizer} corrected to the chain's figure OVER a quantizer "
                          f"disagreement ({TRUST_CHAIN_FLAG})")
    provenance.append(f"{correctable - verified - over_quantizer} from arithmetic alone")
    return f"{correctable} correctable of {rows} payout row(s) in {db_path}; " + ", ".join(provenance)


def _footer(args, db_path: str, *, correctable: int, refusals: int) -> list[str]:
    """What to do next, which is a DIFFERENT sentence in each of four states.

    THE DEFECT THIS REPLACES, measured on the operator's host 2026-10-03 on the
    second dry run after --apply had corrected 12 rows. The run reported
    `CORRECT 0 row(s) / (none)` and `REFUSE 3 row(s)` correctly, and then ended:

        DRY RUN: nothing written. To correct the 0 row(s) above:
            python3 correct_payout_amounts.py --apply

    Three things wrong with that in the state the reader was actually in. "the 0
    row(s) above" is a sentence about an empty set. The command offered would
    have written nothing. And the footer's SHAPE was identical whether there were
    twelve rows to correct or none, so a reader skimming saw a call to action
    that was not one -- which is rule 14's "make 'did nothing' look different
    from 'did work'", failing in the one place a reader looks last.

    It is the same class of defect as the `credited (none) -- the deposit was
    never accepted` line fixed earlier the same day: a sentence that was true
    when it was written and false in the state the reader reaches it in.

    THE FOUR STATES, and the counts that distinguish them are already computed by
    the caller rather than recounted here:

      correctable, no refusals      the original footer: the count and the
                                    command.
      nothing correctable, none     say plainly that every row already agrees
      refused                       with its chain, and offer NO command --
                                    there is nothing for --apply to write.
      nothing correctable, rows     do not offer a bare --apply, which cannot
      refused                       resolve them. Name the one flag that can.
      correctable AND refused       both, corrections first, so the refusals are
                                    not buried under a command that skips them.

    RETURNED RATHER THAN PRINTED, so the four states can be asserted on directly
    with seeded counts (rule 10). An applied run gets the refusal half only: it
    has already written what it could, and a command telling it to write again
    would be wrong, but the refusals still need naming.
    """
    lines: list[str] = []
    if args.apply:
        if refusals and not args.trust_chain_over_quantizer:
            lines.append(f"\n{refusals} row(s) were REFUSED and nothing was written for them. The only "
                         f"thing that writes a row whose chain contradicts the quantizer is:\n"
                         f"    {_rerun_command(args, db_path, '--apply', TRUST_CHAIN_FLAG)}")
        return lines
    if correctable:
        lines.append(f"\nDRY RUN: nothing written. To correct the {correctable} row(s) above:\n"
                     f"    {_rerun_command(args, db_path, '--apply')}")
    elif not refusals:
        lines.append("\nDRY RUN, AND THERE IS NOTHING TO DO: every payout row already records a figure "
                     "its chain can express, so no row would be written even with --apply. No command "
                     "is offered because there is no work to run one on.")
    if refusals and not args.trust_chain_over_quantizer:
        lines.append(f"\n{refusals} row(s) were REFUSED, and --apply alone would write NOTHING for them "
                     f"-- it skips every refusal. The only thing that writes a row whose chain "
                     f"contradicts the quantizer is the flag that says which of the two to believe:\n"
                     f"    {_rerun_command(args, db_path, '--apply', TRUST_CHAIN_FLAG)}\n"
                     f"  Read the REFUSE section above first: it prints both figures for every one of "
                     f"them, and that flag writes the CHAIN's.")
    return lines


def refusal_before_reading(args, db_path: str) -> str | None:
    """Why this run must not start at all, or None. Nothing has been read yet.

    BOTH REFUSALS ARE THE SAME DECISION -- "this invocation cannot do what it
    says" -- so they live together at the bottom where they can be called with
    seeded args and no database (rule 10). Keeping them inline grew main() past
    ruff's C901 ceiling, which rule 12 says to answer by extracting the decision
    rather than by raising the ceiling.

    THE TWO FLAGS CONTRADICT EACH OTHER IN WORDS. --no-chain asks no chain, so
    there is no chain figure to prefer over the quantizer and the branch
    TRUST_CHAIN_FLAG governs is unreachable: the flag would be silently inert.
    Refused rather than warned-and-continued, because an operator who passed
    both holds a belief about this run that is wrong, and rule 14's complaint is
    precisely about a run that looks like it did something it did not.

    A MISSING DATABASE IS REFUSED BEFORE sqlite3.connect(), WHICH CREATES ONE.
    show_swap.py guards the same way and its header says why: a tool that
    connects to a mistyped path leaves an empty database behind and then reports
    "(none)", which is indistinguishable from a database whose payouts are all
    correct. Here it would also mean an --apply run that wrote nothing and said
    so cheerfully.
    """
    if args.trust_chain_over_quantizer and args.no_chain:
        return (f"{TRUST_CHAIN_FLAG} and --no-chain cannot both be given. The first says to believe the "
                f"chain's own figure over the quantizer's; the second says not to ask any chain. With no "
                f"chain answer there is no disagreement to resolve and the flag would change nothing. "
                f"Drop one of them.")
    if not Path(db_path).exists():
        return (f"{db_path} does not exist. NOT created -- connecting would make an empty database and "
                f"every count below would read 0 for a file nothing uses. Check SWAP_DB_PATH, or pass "
                f"--db with the path the workers read.")
    return None


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    started = time.monotonic()
    db_path = args.db or str(Config.DB_PATH)
    _announce(args, db_path)

    refusal = refusal_before_reading(args, db_path)
    if refusal:
        print(f"  REFUSED: {refusal}", file=sys.stderr)
        return 2

    adapters = {} if args.no_chain else build_adapters(Config.RPC)
    if not args.no_chain:
        print(labeled("adapters", f"{', '.join(sorted(adapters)) or '(none)'}  <- the chains that CAN be "
                                  f"asked in this process; a row on any other chain is corrected from "
                                  f"arithmetic and says so"), flush=True)

    with db_session(db_path) as db:
        rows = [dict(row) for row in db.execute(PAYOUT_SQL).fetchall()]
        print(labeled("payout rows", f"{len(rows)}  <- every row in `payouts`, not a filtered set: a row "
                                     f"this tool refuses is named rather than counted"), flush=True)
        if not rows:
            print("  (none)  <- no payout row exists in this database. If the operator's host has 23, "
                  "this is the wrong file -- compare the path above.", flush=True)
            print(labeled("done in", format_duration(time.monotonic() - started)), flush=True)
            return 0
        mismatched = [row for row in rows if row["asset"] != row["swap_to_asset"]]
        if mismatched:
            print(labeled("WARNING", f"{len(mismatched)} row(s) name an asset their swap does not pay out "
                                     f"in: {', '.join(str(row['id']) for row in mismatched)}. The chain "
                                     f"read uses payouts.asset, which is what the send used"), flush=True)

        planned = plans_for(rows, adapters, ask_chain=not args.no_chain,
                            say=lambda line: print(line, flush=True),
                            trust_chain_over_quantizer=args.trust_chain_over_quantizer)
        applied: list[str] = []
        if args.apply:
            for row, plan in planned:
                if plan.verdict != CORRECT:
                    continue
                _wrote, sentence = apply_correction(db, row, plan)
                applied.append(sentence)
        _report(planned, applied=applied)

    corrections = sum(1 for _row, plan in planned if plan.verdict == CORRECT)
    refusals = sum(1 for _row, plan in planned if plan.verdict == REFUSE)
    verified = sum(1 for _row, plan in planned
                   if plan.verdict == CORRECT and plan.authority == FROM_CHAIN)
    over_quantizer = sum(1 for _row, plan in planned
                         if plan.verdict == CORRECT and plan.authority == FROM_CHAIN_OVER_QUANTIZER)
    print(labeled("summary", _summary_line(db_path, rows=len(rows), correctable=corrections,
                                           verified=verified, over_quantizer=over_quantizer)), flush=True)
    if over_quantizer:
        # THE DISAGREEMENT DOES NOT STOP BEING A FINDING BECAUSE A ROW WAS
        # CORRECTED. The flag resolves the ROW; it says nothing about the chain,
        # and a reader who sees only "corrected" would conclude otherwise.
        print(labeled("WARNING", f"{over_quantizer} row(s) were written from the chain DESPITE the "
                                 f"quantizer disagreeing. That disagreement is still real: on the "
                                 f"operator's host it is Gridcoin rounding an over-precise amount half "
                                 f"up where the quantizer truncates, on sends made before the payout "
                                 f"service began quantizing first. Each audit row holds BOTH figures"),
              flush=True)
    for line in _footer(args, db_path, correctable=corrections, refusals=refusals):
        print(line, flush=True)
    print(labeled("done in", format_duration(time.monotonic() - started)), flush=True)
    # A refusal is not a failure of this run and it is not a success either: the
    # rows that could be corrected were, and something was found that a person has
    # to look at. Rule 13 -- "did nothing" and "did work" must not share an exit
    # code any more than they share a line.
    #
    # WITH TRUST_CHAIN_FLAG A DISAGREEMENT IS NO LONGER A REFUSAL, so a run that
    # resolves all three of the operator's rows exits 0 -- which is correct and is
    # not the flag hiding anything: the WARNING above fires on exactly those rows
    # and every audit row carries both figures. What the exit code reports is
    # "something is still unresolved", and after an explicit instruction to
    # resolve it, nothing is.
    return 5 if refusals else 0


if __name__ == "__main__":
    raise SystemExit(main())
