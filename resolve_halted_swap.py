#!/usr/bin/env python3
"""Resolve a HALTED swap the way the operator decided. Dry run by default.

Role: file (operator entry point at the project root per CLAUDE.md rule 10; the
      decisions are the functions below and are callable with seeded inputs)
Reads: swap_terminal.db (swaps, payouts, deposit_events, wallet_inventory) and,
      when a destination adapter is available, that chain's balance
Writes: with --apply ONLY -- swaps.status, swaps.failed_reason, swaps.credited_at,
      deposit_events.credited_at for this swap, and one swap_audit_log row.
      Nothing else, and all of it in one transaction.
Can send orders: NO. It signs nothing and broadcasts nothing. It hands the swap
      to payout_worker, which does.
Live-safe: the dry run is read-only. --apply moves a swap into payout_pending,
      which a worker then pays out -- so --apply is the operator's decision being
      executed, not a diagnostic.

WHY THIS IS NOT PART OF rescue_payout.py, AND THAT MATTERS.

rescue_payout.py re-drives a FAILED swap, and its whole premise is a PROVABLE
fact: the recorded reason matches a refusal raised before anything was signed, so
nothing can be on chain. It refuses every reason it cannot prove, and
PRE_SIGNING_MARKERS exists to keep that gate narrow.

A HALTED swap has nothing to prove. It is holding a deposit that did not match
what the swap expected, and every way out of it moves money in a direction only a
person can choose. Folding this into rescue_payout.py would mean its verdict
function sometimes answers "a person said so", which is exactly the weakening its
marker list is built to prevent. So this is a separate tool with a separate
premise: the decision has already been made, and this records and executes it.

THE CASE IT WAS BUILT FOR, 2026-10-03.

s_612fac62489f2122, XRP -> GRC, halted since 2026-09-26T15:02:25Z:

    expected   5.0 XRP
    seen       1.0 XRP   (20%, outside AMOUNT_TOLERANCE_PCT=0.01)
    payout     277.2412844507888 GRC was the estimate for the full 5.0
    broadcast  nothing

Three resolutions were priced for the operator and they chose to SCALE THE LOCKED
QUOTE: pay what the deposit is worth at the rate agreed on 2026-09-26, which is
277.2412844507888 * (1.0 / 5.0) = 55.4483 GRC.

WHAT THEY WERE TOLD AND CHOSE ANYWAY, recorded because a decision is only
informed if the cost was on the screen: 1 XRP bought 56.29 GRC on 2026-09-26 and
160.55 GRC on 2026-10-03, so paying the locked rate is 102.69 GRC less than the
deposit is worth tonight, and the week of drift accrued during the DESK's delay
rather than the customer's. The two other options -- re-pricing at tonight's rate
and refunding the deposit -- are not implemented here, deliberately, because
neither was chosen. Refunding is also not merely unimplemented: deposit_events
records `address`, which for XRP is OUR deposit account, so the sender is not in
the database at all and would have to be read off the ledger by txid first.

WHY NO NEW PAYOUT ARITHMETIC. services/payout_service.payout_amount() already
scales the quote by actual_input_amount / expected_input_amount, and its own
docstring records the 2026-10-03 measurement that put it there. So the scaled
figure is not computed here and sent -- the swap is moved to payout_pending and
the EXISTING worker computes it. A second copy of that arithmetic in this file is
rule 8's duplicate on the one line that decides what a customer receives.

NO AMOUNT FIELD IS REWRITTEN, and that is the whole reason this resolution is
the cheap one. payout_amount() scales the quote by actual/expected, so leaving
expected_input_amount at 5.0 and actual_input_amount at 1.0 produces
277.2412844507888 * (1.0 / 5.0) = 55.4483 GRC on its own. The chosen answer is
what the existing arithmetic already computes from the rows as they stand, so
--apply touches swaps.status, swaps.failed_reason and one audit row. Nothing
else.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "swap_terminal"))

from chains.registry import build_adapters
from config import Config
from db import PAYOUT_LIVE_STATUSES, db_session
from microfortnights import format_duration
from report_block import labeled
from services.helpers import utc_now_iso
from services.payout_capacity import as_amount, why_the_payout_cannot_be_funded
from services.payout_service import payout_amount
from services.swap_service import payout_source_account
from services.swap_view import HALTED_STATUSES
from workers.common import get_config_dict

SELF = "resolve_halted_swap.py"

#: The only resolution this tool implements. Named rather than implied so a second
#: one cannot be added by editing a branch: each needs its own verdict, its own
#: arithmetic and its own audit sentence.
SCALE_THE_LOCKED_QUOTE = "scale-locked-quote"


def resolve_verdict(swap, payout_rows) -> tuple[bool, str]:
    """May this halted swap be handed to payout_worker. (allowed, the sentence).

    THE DECISION, at the bottom where it can be called with seeded rows (rule 10).

    THREE REFUSALS, and each is a different way the swap is not what this tool
    assumes:

      not halted        a swap in any other status is already being acted on by a
                        worker, or has been paid. Moving it would race the worker
                        or double-pay.
      a live payout row PAYOUT_LIVE_STATUSES means something is already claimed,
                        broadcast or completed for this swap. Handing it back
                        would be a second payout for one deposit.
      nothing credited  actual_input_amount is the figure payout_amount() scales
                        by, and a NULL or zero makes the scaled payout zero or
                        undefined. A swap halted before any deposit was counted is
                        not the case this resolves.

    A txid on ANY row is the hardest refusal: it is evidence money left, and rule
    2's distinction applies -- "I could not find a broadcast" is not "there was no
    broadcast", but a txid IS one.
    """
    if swap["status"] not in HALTED_STATUSES:
        return False, (f"this swap is {swap['status']!r}, not halted. HALTED_STATUSES is "
                       f"{', '.join(HALTED_STATUSES)}; anything else is either being acted on by a "
                       f"worker right now or has already been paid, and moving it would race the "
                       f"worker or pay twice")
    live = [row for row in payout_rows if row["status"] in PAYOUT_LIVE_STATUSES]
    if live:
        return False, (f"{len(live)} payout row(s) are already live ("
                       f"{', '.join(sorted({row['status'] for row in live}))}), so something has "
                       f"already been claimed or broadcast for this swap. Handing it back would be a "
                       f"second payout for one deposit")
    with_txid = [row for row in payout_rows if (row["txid"] or "").strip()]
    if with_txid:
        return False, (f"a payout row carries txid {with_txid[0]['txid']}, which is EVIDENCE money left "
                       f"this desk for this swap. Nothing here can un-send it")
    actual = swap["actual_input_amount"]
    if actual is None or float(actual) <= 0:
        return False, ("actual_input_amount is NULL or zero, so no deposit has been counted for this "
                       "swap. services/payout_service.payout_amount() scales the quote by "
                       "actual/expected, which is undefined here -- this tool resolves a swap holding "
                       "a deposit, not one waiting for it")
    return True, (f"halted at {swap['status']!r} with {actual} {swap['from_asset']} counted, no payout "
                  f"row live and no txid anywhere. payout_worker will compute the scaled payout itself")


def scaled_payout_preview(swap) -> tuple[float, str]:
    """What payout_worker WILL send, asked of the function that will decide it.

    CALLS payout_amount() RATHER THAN REPEATING ITS ARITHMETIC. The figure on this
    screen and the figure broadcast five seconds later must be the same number for
    the same reason, and the only way to guarantee that is to ask the same
    function. A preview that computes `estimate * actual / expected` itself would
    agree today and drift the first time that scaling changes -- rule 8's shape on
    the line that decides what a customer receives.
    """
    return payout_amount(swap)


def funding_line(adapters, config, swap, amount: float) -> tuple[bool, str]:
    """Can the destination wallet actually fund the scaled payout. (ok, sentence).

    ASKED HERE BECAUSE MOVING A SWAP TO payout_pending BYPASSES THE GATE THAT
    NORMALLY ASKS. services/swap_service.create_swap() refuses a swap whose payout
    the destination wallet cannot fund -- added 2026-10-03 after a 0.001 BTC
    deposit was taken against a 9049.69 GRC payout and a 3780.09 GRC wallet -- and
    that gate runs at CREATION. This swap was created a week ago and is being
    handed to the worker now, so the balance it was checked against is a week old.

    A SHORTFALL DOES NOT REFUSE, it warns. The deposit is already held and already
    irreversible; refusing to resolve a swap because the wallet is short does not
    give the customer their money, it just leaves the swap halted. So this reports
    and the operator decides, which is the same place rule 16 puts it.
    """
    verdict = why_the_payout_cannot_be_funded(
        adapters, swap["to_asset"], amount, float(swap["network_fee_reserve"]),
        source_account=payout_source_account(config, swap["to_asset"]),
    )
    if verdict.refuses:
        return False, f"*** {verdict.why} ***"
    if verdict.unchecked:
        return True, f"NOT CHECKED -- {verdict.why}"
    return True, f"the {swap['to_asset']} wallet can fund {as_amount(amount)} {swap['to_asset']}"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Hand a HALTED swap to payout_worker at the scaled locked quote. Dry run by default.",
        epilog=("--resolution has one value and is REQUIRED anyway: a tool that moves money must not "
                "have a default for WHICH way it moves it. The other resolutions priced for the "
                "operator -- re-pricing at today's rate, refunding the deposit -- are deliberately not "
                "implemented here."),
    )
    parser.add_argument("--swap", required=True, help="the halted swap id to resolve")
    parser.add_argument("--resolution", required=True, choices=[SCALE_THE_LOCKED_QUOTE],
                        help="how to resolve it. Only the scaled locked quote is implemented.")
    parser.add_argument("--db", default="", help=f"database (default: {Config.DB_PATH})")
    parser.add_argument("--apply", action="store_true",
                        help="actually write. Without it nothing is written and every check still runs.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    started = time.monotonic()
    db_path = args.db or str(Config.DB_PATH)
    print(f"{SELF}: {'APPLY -- rows WILL be written' if args.apply else 'DRY RUN -- nothing is written'}",
          flush=True)
    print(labeled("database", db_path), flush=True)
    print(labeled("swap", args.swap), flush=True)
    print(labeled("resolution", f"{args.resolution}  <- pay what the deposit is worth at the rate the "
                                f"quote locked, scaled by actual/expected"), flush=True)
    print("  this tool SIGNS NOTHING and BROADCASTS NOTHING. It hands the swap to payout_worker, "
          "which does.", flush=True)

    with db_session(db_path) as db:
        swap = db.execute("SELECT * FROM swaps WHERE id = ?", (args.swap,)).fetchone()
        if swap is None:
            print(f"  REFUSED: no swap with id {args.swap} exists in {db_path}", flush=True)
            return 2
        rows = db.execute("SELECT * FROM payouts WHERE swap_id = ? ORDER BY id", (args.swap,)).fetchall()
        print(labeled("status", f"{swap['status']}  <- halted since {swap['updated_at']}"), flush=True)
        print(labeled("deposit", f"{swap['actual_input_amount']} {swap['from_asset']} counted against "
                                 f"{swap['expected_input_amount']} expected"), flush=True)
        print(labeled("payout rows", f"{len(rows)}"
                      + ("".join(f"\n                  {r['amount']} {r['asset']} status={r['status']} "
                                 f"txid={r['txid'] or '(none)'}" for r in rows) or "  (none)")), flush=True)

        allowed, reason = resolve_verdict(swap, rows)
        print(labeled("verdict", f"{'ALLOWED' if allowed else 'REFUSED'} -- {reason}"), flush=True)
        if not allowed:
            print(labeled("done in", format_duration(time.monotonic() - started)), flush=True)
            return 3

        amount, how = scaled_payout_preview(swap)
        print(labeled("will pay", f"{as_amount(amount)} {swap['to_asset']}  <- {how}"), flush=True)
        print(labeled("to", f"{swap['payout_address']}  <- FINAL, set when the swap was created"),
              flush=True)
        adapters = build_adapters(Config.RPC)
        # get_config_dict(), NOT Config.RPC. payout_source_account() reads
        # TAG_ATTRIBUTION[asset][0] off the whole config, the way create_swap()
        # passes it; Config.RPC is only the per-chain connection map and handing
        # that over would have made every lookup miss.
        _fundable, funding = funding_line(adapters, get_config_dict(), swap, amount)
        print(labeled("funding", funding), flush=True)

        if not args.apply:
            print(f"\nDRY RUN: nothing written. To resolve it:\n"
                  f"    python3 {SELF} --swap {args.swap} --resolution {args.resolution} --apply",
                  flush=True)
            print(labeled("done in", format_duration(time.monotonic() - started)), flush=True)
            return 0

        now = utc_now_iso()
        # NO RESERVATION IS RELEASED HERE, and the difference from rescue_payout.py
        # is checked rather than assumed. That tool releases one because a FAILED
        # payout attempt reserved the amount before the send and nothing released it.
        # This swap has no payout row at all -- the verdict above refuses if one is
        # live -- so nothing was ever reserved for it and there is nothing to
        # release. Releasing anyway would subtract a reservation belonging to some
        # other swap on the same asset.
        #
        # COMPARE-AND-SWAP ON THE STATUS, so a worker that moved this swap between
        # the read above and this write loses nothing: the UPDATE matches zero rows
        # and the audit row is not written either, because both are in one
        # transaction.
        moved = db.execute(
            "UPDATE swaps SET status = 'payout_pending', failed_reason = ?, updated_at = ? "
            "WHERE id = ? AND status = ?",
            (f"resolved by {SELF} on {now} as {args.resolution}; the operator was shown the locked rate "
             f"against tonight's and chose the locked one. Previous: {swap['failed_reason']}",
             now, args.swap, swap["status"]),
        ).rowcount
        if not moved:
            print(f"  REFUSED: the swap left {swap['status']!r} between the read and the write, so "
                  f"nothing was changed. Run the dry run again and look at its status.", flush=True)
            return 4
        # credited_at IS STAMPED HERE, AND LEAVING IT OUT PRODUCED A PAID SWAP THAT
        # SAID ITS DEPOSIT WAS NEVER ACCEPTED. Measured on the operator's host
        # 2026-10-03, minutes after this tool first ran: s_612fac62489f2122 went
        # under_review -> payout_pending -> completed, broadcast 55.44825689 GRC, and
        # show_swap.py reported
        #
        #     credited  (none) -- the deposit was never accepted
        #     1.0 XRP  1 confirmation(s)  COUNTED by the gate  credited_at (not credited)
        #
        # on a swap that had just paid out. services/deposit_service.
        # _credit_confirmed_deposit() is the only thing that stamps those columns, and
        # moving a swap straight to payout_pending skips it.
        #
        # RESOLVING A HALTED SWAP THIS WAY *IS* ACCEPTING THE DEPOSIT, which is what
        # makes stamping it correct rather than cosmetic: the operator looked at a
        # deposit outside tolerance and decided to pay for it. The row should say the
        # deposit was accepted, because it was -- by a person rather than by the gate.
        #
        # actual_input_amount IS DELIBERATELY NOT TOUCHED. _credit_confirmed_deposit()
        # sets it from confirmed_total; here it already holds the counted figure that
        # payout_amount() scaled by, and rewriting the payout's own basis during a
        # resolution is the one thing this tool must not do.
        #
        # THE STRANDING QUESTION WAS CHECKED, not assumed: unattributable_deposit_service.
        # unclaimed_events() takes `credited` as the set of txids that HAVE a
        # deposit_events row, not those with credited_at set, so the missing stamp
        # could not have made this deposit read as stranded money. The defect was
        # display only -- and a paid swap reporting "the deposit was never accepted" is
        # rule 13's "'skipped' plus 'success' in one output" on the two lines an
        # operator reads to decide whether a customer was served.
        db.execute(
            "UPDATE swaps SET credited_at = ? WHERE id = ? AND credited_at IS NULL",
            (now, args.swap),
        )
        db.execute(
            "UPDATE deposit_events SET credited_at = ? WHERE swap_id = ? AND credited_at IS NULL",
            (now, args.swap),
        )
        db.execute(
            # Column names read off db.py's schema, not recalled: the table is
            # (old_status, new_status, message), and a guess at (from_status,
            # to_status, reason) cost a failed query in this tree once already.
            "INSERT INTO swap_audit_log (swap_id, old_status, new_status, message, created_at) "
            "VALUES (?, ?, 'payout_pending', ?, ?)",
            (args.swap, swap["status"],
             f"{SELF}: {args.resolution}. {reason}. Will pay {amount} {swap['to_asset']}.", now),
        )
        db.commit()
        print(labeled("WROTE", f"status {swap['status']} -> payout_pending, credited_at stamped on the "
                               f"swap and its deposit row(s), audit row written. No reservation existed "
                               f"to release"), flush=True)
        print(f"  next       payout_worker picks it up on its next cycle. Watch it:\n"
              f"               python3 show_swap.py --swap {args.swap}", flush=True)
    print(labeled("done in", format_duration(time.monotonic() - started)), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
