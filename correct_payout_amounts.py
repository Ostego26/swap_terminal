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


def plan_for_payout(row, chain: ChainAmount) -> Correction:
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
      chain disagrees   REFUSED, with both numbers. The quantizer being wrong
                        about a chain is a bigger finding than this row -- see
                        this module's docstring.
      chain agrees      corrected to the CHAIN's figure, not to the quantized
                        one. They are equal by the branch above; writing the
                        chain's own number is what makes the row's provenance
                        the observation rather than the arithmetic.

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
            f"no txid, status={row['status']!r} -- this row records a PROPOSAL, not a send. Quantizing "
            f"it would make an amount this desk never sent look like one it did, which is a worse "
            f"record than the overstatement being corrected"
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
        return Correction(REFUSE, recorded, None, "", (
            f"THE CHAIN AND THE QUANTIZER DISAGREE. The chain says {chain.amount!r} {asset} "
            f"({chain.how}); chains/payout_quantization.quantize_for_chain({recorded!r}, {asset!r}) "
            f"says {quantized!r}. NOTHING was written for this row: that disagreement means the "
            f"quantizer is wrong about {asset}, which decides what every FUTURE payout records, "
            f"reserves and sends -- a bigger finding than this row, and correcting the row would "
            f"bury it"
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
    marker = "" if plan.authority == FROM_CHAIN else "*** NOT VERIFIED ON CHAIN ***  "
    return [
        f"  {plan.verdict:<8} {head}",
        f"           recorded    {plan.recorded!r}  <- what the quote computed, which the chain cannot send",
        f"           correct to  {plan.corrected!r}  <- {marker}{plan.authority}",
        # :f rather than the Decimal's own repr, which is exponent notation for a
        # figure this small -- 9.7888E-10 is the same number as 0.00000000097888 and
        # only one of them can be counted against a satoshi by eye (rule 14).
        f"           overstated by {difference(plan.recorded, plan.corrected):f} {row['asset']}"
        f"   txid={row['txid']}",
        f"           {plan.why}",
    ]


def plans_for(rows, adapters, *, ask_chain: bool, say) -> list[tuple[dict, Correction]]:
    """Every row paired with its verdict, asking the chain ONLY where a write would happen.

    TWO CALLS OF THE SAME PURE FUNCTION, which is the cheap way to get both
    properties the tool needs. The first asks "would this row be corrected at
    all", with NOT_ASKED standing in for the chain; only a row that answers
    CORRECT is worth a network round trip, so an already-corrected database costs
    zero RPC calls and a second run is both idempotent and silent on the wire. The
    second call re-decides with the chain's real answer, which is where a
    disagreement becomes a refusal.

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
            plan = plan_for_payout(row, chain)
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
                "resolved either way: that disagreement is a defect in the quantizer, which decides "
                "what every future payout sends."),
    ))


def _parser_with_arguments(parser: argparse.ArgumentParser) -> argparse.ArgumentParser:
    parser.add_argument("--db", default="", help=f"database to correct (default: {Config.DB_PATH})")
    parser.add_argument("--apply", action="store_true",
                        help="actually write. Without it nothing is written and every check still runs.")
    parser.add_argument("--no-chain", action="store_true",
                        help="ask no chain, so it is safe with every daemon down. Every correction is "
                             "then ARITHMETIC ONLY and says so per row and in its audit row.")
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


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    started = time.monotonic()
    db_path = args.db or str(Config.DB_PATH)
    _announce(args, db_path)

    # REFUSED BEFORE sqlite3.connect(), WHICH CREATES A MISSING FILE. show_swap.py
    # guards the same way and its header says why: a tool that connects to a
    # mistyped path leaves an empty database behind and then reports "(none)",
    # which is indistinguishable from a database whose payouts are all correct.
    # Here it would also mean an --apply run that wrote nothing and said so
    # cheerfully.
    if not Path(db_path).exists():
        print(f"  REFUSED: {db_path} does not exist. NOT created -- connecting would make an empty "
              f"database and every count below would read 0 for a file nothing uses. Check "
              f"SWAP_DB_PATH, or pass --db with the path the workers read.", file=sys.stderr)
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

        planned = plans_for(rows, adapters, ask_chain=not args.no_chain, say=lambda line: print(line, flush=True))
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
    print(labeled("summary", f"{corrections} correctable of {len(rows)} payout row(s) in {db_path}; "
                             f"{verified} of those {corrections} verified against the chain itself, "
                             f"{corrections - verified} from arithmetic alone"), flush=True)
    if not args.apply:
        print(f"\nDRY RUN: nothing written. To correct the {corrections} row(s) above:\n"
              f"    python3 {SELF} --apply" + (" --no-chain" if args.no_chain else "")
              + (f" --db {db_path}" if args.db else ""), flush=True)
    print(labeled("done in", format_duration(time.monotonic() - started)), flush=True)
    # A refusal is not a failure of this run and it is not a success either: the
    # rows that could be corrected were, and something was found that a person has
    # to look at. Rule 13 -- "did nothing" and "did work" must not share an exit
    # code any more than they share a line.
    return 5 if refusals else 0


if __name__ == "__main__":
    raise SystemExit(main())
