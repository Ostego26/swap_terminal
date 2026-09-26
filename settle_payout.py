#!/usr/bin/env python3
"""Correct a swap whose payout WAS delivered but was recorded as failed.

Role: file (root entry point; the decisions are the functions below)
Reads: swap_terminal.db (swaps, payouts), and the DESTINATION chain's wallet
       (listtransactions, gettransaction) to prove the payment exists
Writes: swap_terminal.db (payouts, swaps, swap_audit_log, wallet_inventory) --
       and only with --apply. Dry run writes nothing.
Can move funds: NO. It signs nothing, broadcasts nothing, and calls no send
       method. It corrects a RECORD of a payment that already happened, which is
       a fund-path record change and therefore the operator's to ask for
       (CLAUDE.md rule 16). It refuses every swap except one whose payout is
       missing a txid.
Mainnet-safe: it chooses no network. It reads whatever endpoint config.Config
       names, so the same GRC_RPC_PORT that points the rest of the terminal at
       real money points this there too. It asks the wallet and believes the
       wallet.

WHY THIS EXISTS, MEASURED 2026-09-26.

The first real XRP -> GRC payout was delivered and recorded as failed.
55.52645238 GRC left the operator's wallet -- txid 3e09dc9cfd7a61da..., their own
listtransactions -- and then the wallet's re-lock raised, because the staking
unlock was being sent a timeout of 0 that Gridcoin refuses. The txid was assigned
inside the `with` and stored after it, so control left the block and the txid was
discarded. The swap read `status failed`, `txid (none)`.

services/payout_service.py no longer does that. But the swap it already did it to
is still sitting there, and nothing in the tree could correct it: every status
transition on the fund path is written by the worker that performed the action, and
no worker performs "this already happened, record it".

WHY IT ASKS THE CHAIN INSTEAD OF TAKING A TXID ON TRUST.

A txid pasted into a terminal is the transcription this project has been removing
all day, and here it would mark a swap `completed` against a string. So --txid is
optional and is a DISAMBIGUATOR, not the evidence: the evidence is a transaction in
the destination wallet, to this swap's payout address, for this payout's amount.
One match is the answer, several is a refusal that lists them, none is a refusal
that says the payout was probably never broadcast.

Rule 17's shape: run the thing that would show it false. The thing that would show
"the payout was delivered" false is the wallet not having it.
"""

from __future__ import annotations

import argparse
import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "swap_terminal"))

from chains.registry import build_adapters, unconfigured_chains, why_unconfigured
from config import Config
from db import connect_db
from report_block import labeled
from services.helpers import utc_now_iso
from services.payout_service import (
    _record_broadcast,
)

# A settled payment's amount and the amount we recorded can differ in the last
# bits, because the database holds REAL and the wallet reports a decimal string.
# Compared as Decimal with a tolerance of one satoshi-equivalent rather than by
# equality, which float round-tripping does not survive.
AMOUNT_TOLERANCE = Decimal("0.00000001")


class SettleRefused(SystemExit):
    """Every refusal, so main() has one exit path and each says what to do."""


def swap_to_settle(db, swap_id: str) -> dict:
    """The one swap this tool may act on, or a refusal naming why not.

    THE CONDITIONS ARE THE SAFETY, and they are checked before anything is read from
    a chain. A swap that already has a payout_txid, or that is not `failed`, is not
    a mislabeled delivery -- it is either fine or a different problem, and
    "correcting" it would overwrite a record that something else wrote for a reason.
    """
    row = db.execute("SELECT * FROM swaps WHERE id = ?", (swap_id,)).fetchone()
    if row is None:
        raise SettleRefused(f"REFUSED: no swap {swap_id} in this database. Nothing was written.")
    if row["payout_txid"]:
        raise SettleRefused(
            f"REFUSED: swap {swap_id} already records payout_txid {row['payout_txid']}. There is nothing "
            f"to correct, and overwriting a recorded txid would destroy the only link to the payment "
            f"that was made. Nothing was written."
        )
    if row["status"] != "failed":
        raise SettleRefused(
            f"REFUSED: swap {swap_id} is '{row['status']}', not 'failed'. This tool exists for one "
            f"specific record: a payout that WAS delivered and was written down as failed. A swap in "
            f"any other state is a different problem and this is the wrong instrument for it. "
            f"Nothing was written."
        )
    return row


def payout_to_settle(db, swap_id: str) -> dict:
    """The payout row that should carry the txid, or a refusal."""
    rows = db.execute(
        "SELECT * FROM payouts WHERE swap_id = ? ORDER BY created_at", (swap_id,)
    ).fetchall()
    if not rows:
        raise SettleRefused(
            f"REFUSED: swap {swap_id} has no payout row at all, so no payout was ever claimed for it "
            f"and nothing could have been broadcast. Nothing was written."
        )
    without_txid = [row for row in rows if not row["txid"]]
    if len(without_txid) != 1:
        listing = "\n".join(
            f"      amount {row['amount']}  status {row['status']}  txid {row['txid'] or '(none)'}"
            for row in rows
        )
        raise SettleRefused(
            f"REFUSED: swap {swap_id} has {len(rows)} payout row(s), {len(without_txid)} of them "
            f"without a txid. Exactly one is required, because which row a payment belongs to is not "
            f"knowable from here otherwise:\n{listing}\nNothing was written."
        )
    return without_txid[0]


def matching_wallet_sends(transactions: list[dict], address: str, amount) -> list[dict]:
    """Wallet transactions that ARE this payout: a send, to this address, this amount.

    A pure function over the wallet's own rows, so the matching rule can be called
    with seeded input (rule 10). Three conditions, and each one matters:

      category == "send"   a `receive` row for the same txid exists whenever the
                           destination is an address the wallet owns, which is
                           exactly the operator's case -- their payout address is
                           theirs (ismine = True), so listtransactions showed BOTH
                           sides of one transaction. Matching on receive would count
                           the same payment as an incoming one.
      the address          a payout goes to the address on the swap and nowhere else.
      the amount           compared as Decimal within AMOUNT_TOLERANCE, because the
                           database holds a float and the wallet reports a string.

    The wallet reports a SEND as negative, so the sign is dropped before comparing.
    """
    target = Decimal(str(amount))
    return [
        tx
        for tx in transactions
        if tx.get("category") == "send"
        and tx.get("address") == address
        and _within_tolerance(tx.get("amount"), target)
    ]


def _within_tolerance(reported, target: Decimal) -> bool:
    """Whether a wallet-reported amount is this payout's, within one satoshi.

    A function rather than a try/except/continue inside the loop above, which ruff's
    S112 flags for the right reason: a silent `continue` inside a matcher cannot be
    told apart from "did not match", and this one wants to mean exactly that. Here the
    unparseable case has a name and one meaning -- not this payout.

    The wallet reports a send as NEGATIVE, so the sign is dropped before comparing.
    """
    try:
        sent = abs(Decimal(str(reported)))
    except (ArithmeticError, TypeError, ValueError):
        # Named exceptions, not a blind catch: Decimal raises InvalidOperation (an
        # ArithmeticError) for text, TypeError for None, ValueError for some inputs.
        # A row whose amount will not parse is not this payment, and it must never be
        # able to become a match -- that is the only failure here that would matter.
        return False
    return abs(sent - target) <= AMOUNT_TOLERANCE


def choose_transaction(candidates: list[dict], wanted_txid: str, address: str, amount) -> dict:
    """Exactly one transaction, or a refusal that lists what it saw."""
    if wanted_txid:
        named = [tx for tx in candidates if tx.get("txid") == wanted_txid]
        if not named:
            raise SettleRefused(
                f"REFUSED: --txid {wanted_txid} is not among the wallet transactions that match this "
                f"payout ({amount} to {address}). This tool will not record a txid the wallet does not "
                f"confirm sent that amount to that address. Nothing was written."
            )
        return named[0]
    if not candidates:
        raise SettleRefused(
            f"REFUSED: the destination wallet has no send of {amount} to {address}. That is evidence "
            f"the payout was never broadcast, in which case the 'failed' status is CORRECT and must "
            f"stand. If the payment is older than the window this searched, raise --count. "
            f"Nothing was written."
        )
    if len(candidates) > 1:
        listing = "\n".join(f"      {tx.get('txid')}  confirmations {tx.get('confirmations')}" for tx in candidates)
        raise SettleRefused(
            f"REFUSED: {len(candidates)} wallet transactions send {amount} to {address}, so which one "
            f"paid this swap is not knowable from here. Name it with --txid:\n{listing}\n"
            f"Nothing was written."
        )
    return candidates[0]


def record_lines(swap: dict, payout: dict) -> list[str]:
    """What the database currently says, before anything is asked of a chain."""
    return [
        labeled("pair", f"{swap['from_asset']} -> {swap['to_asset']}"),
        labeled("recorded as", f"{swap['status']}  <- what this corrects"),
        labeled("failed reason", str(swap["failed_reason"] or "(none recorded)")),
        labeled("payout amount", f"{payout['amount']} {swap['to_asset']}  <- must match a wallet send exactly"),
        labeled("payout address", f"{payout['destination_address']}  <- the send must have gone here"),
    ]


def evidence_lines(chosen: dict, details: dict) -> list[str]:
    """The wallet's own answer, which is the only evidence this tool accepts."""
    return [
        labeled("txid", f"{chosen['txid']}  <- confirmed by the wallet, not typed"),
        labeled("confirmations", f"{details.get('confirmations')}  <- a COUNT of confirmations, never a duration"),
        labeled("wallet amount", f"{chosen.get('amount')}  <- negative because a send leaves the wallet"),
    ]


def apply_correction(db, swap: dict, amount, txid: str) -> None:
    """The write. Commits. Reuses the worker's own recorder (rule 8).

    _record_broadcast() performs the four writes a successful payout performs --
    payouts.txid/status/sent_at, swaps.payout_txid/completed_at, the status
    transition, and the inventory release the failure path never did. Spelling any of
    them again here would be a second copy of the fund-path record, which is how two
    places come to disagree about what a completed swap looks like.

    old_status='failed' so the audit row records the correction truthfully instead of
    claiming a paying -> completed transition that never happened.
    """
    _record_broadcast(db, swap, amount, txid, old_status="failed")
    # THE OLD REASON IS KEPT, PREFIXED. Blanking it would leave a swap reading
    # `completed` with no trace that it spent time as `failed`, or why -- and the why
    # is the first thing a reader of this row will want.
    db.execute(
        "UPDATE swaps SET failed_reason = ?, updated_at = ? WHERE id = ?",
        (
            f"CORRECTED by settle_payout.py: the payout WAS delivered as {txid}. "
            f"Previous reason: {swap['failed_reason']}",
            utc_now_iso(),
            swap["id"],
        ),
    )
    db.commit()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Record a payout that WAS delivered on a swap that says it failed. Dry run by default.",
        epilog=(
            "It moves no funds and signs nothing. It refuses any swap that is not 'failed' with a "
            "missing payout txid, and it will not write a txid the destination wallet does not confirm."
        ),
    )
    parser.add_argument("--swap", required=True, help="the swap id to correct")
    parser.add_argument(
        "--txid", default="",
        help="which wallet transaction paid it. Only needed when more than one matches; the wallet is "
             "the evidence either way, and a txid it does not confirm is refused",
    )
    parser.add_argument("--count", type=int, default=200,
                        help="how many recent wallet transactions to search (default 200)")
    parser.add_argument("--db", default="", help=f"database (default: {Config.DB_PATH})")
    parser.add_argument("--apply", action="store_true",
                        help="actually write. Without it nothing is written and the checks still run")
    args = parser.parse_args(argv)

    db_path = args.db or Config.DB_PATH
    mode = "APPLY -- the record WILL be changed" if args.apply else "DRY RUN -- nothing is written"
    print("settle payout -- records a payment that already happened. Signs nothing, sends nothing.", flush=True)
    print(labeled("mode", mode), flush=True)
    print(labeled("database", str(db_path)), flush=True)
    print(labeled("swap", args.swap), flush=True)

    if not Path(db_path).exists():
        raise SettleRefused(f"REFUSED: no database at {db_path}. Nothing was written.")

    db = connect_db(str(db_path))
    try:
        swap = swap_to_settle(db, args.swap)
        payout = payout_to_settle(db, args.swap)
        destination_asset = swap["to_asset"]
        address = payout["destination_address"]
        amount = payout["amount"]

        for line in record_lines(swap, payout):
            print(line, flush=True)

        adapters = build_adapters(Config.RPC)
        missing = unconfigured_chains(adapters, destination_asset)
        if missing:
            raise SettleRefused(
                f"REFUSED: {why_unconfigured(destination_asset, Config.RPC)} The wallet is the only "
                f"evidence this tool accepts, so it cannot proceed without it. Nothing was written."
            )
        adapter = adapters[destination_asset]

        print(labeled("asking", f"{destination_asset} listtransactions, newest {args.count}"), flush=True)
        transactions = adapter.call("listtransactions", "*", args.count)
        candidates = matching_wallet_sends(transactions, address, amount)
        print(labeled("wallet rows", f"{len(transactions)} searched, {len(candidates) or '(none)'} match"), flush=True)

        chosen = choose_transaction(candidates, args.txid, address, amount)
        txid = chosen["txid"]
        details = adapter.call("gettransaction", txid)
        for line in evidence_lines(chosen, details):
            print(line, flush=True)

        if not args.apply:
            print("\nDRY RUN: nothing was written. The payment above is real and the record still says "
                  "failed. To correct it:", flush=True)
            extra = f" --txid {txid}" if args.txid else ""
            print(f"    python3 settle_payout.py --swap {args.swap}{extra} --apply", flush=True)
            return 0

        # Same four writes the worker performs on a successful payout, from the same
        # function (rule 8). old_status='failed' so the audit trail records the
        # correction truthfully rather than claiming a paying -> completed transition
        # that never happened.
        _record_broadcast(db, swap, amount, txid, old_status="failed")
        apply_correction(db, swap, amount, txid)
        print(f"\nCORRECTED. swap {args.swap} now reads completed with txid {txid}.", flush=True)
        print("  The previous failure reason is KEPT, prefixed with CORRECTED, so the record says what "
              "happened rather than pretending it always said this.", flush=True)
        print(f"  Read it back:  python3 show_swap.py --swap {args.swap}", flush=True)
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SettleRefused as refusal:
        print(f"\n{refusal}", file=sys.stderr, flush=True)
        sys.exit(2)
