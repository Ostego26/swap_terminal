#!/usr/bin/env python3
"""Retire a swap that NEVER RECEIVED A DEPOSIT, so it stops being money the desk owes.

Role: file (root entry point; the decision is expiry_verdict() below)
Reads: swap_terminal.db -- swaps, deposit_events, payouts
Writes: swap_terminal.db, and only with --apply: swaps.status -> 'expired' plus one
       swap_audit_log row, through services.swap_service.set_swap_status() so the
       write is the same compare-and-swap every worker uses. Dry run writes nothing.
Can move funds: NO. It signs nothing, broadcasts nothing, constructs no adapter and
       imports nothing that can send. The direction it moves money is DOWNWARD --
       off the obligation floor -- and it does that by refusing to count a swap
       nobody paid into, never by touching a balance or a reservation.
Mainnet-safe: it chooses no network and opens no socket.

=============================================================================
WHY THIS EXISTS. OPERATOR, 2026-10-07: "let's balance all remaining swaps too."
=============================================================================

MEASURED ON THEIR HOST THE SAME DAY, which is what makes this a defect rather than
housekeeping:

    GRC swaps open (status NOT IN completed/under_review/failed)      7
    GRC they owe between them (payout + network_fee_reserve)   2,499.69447667
    GRC the desk's hot wallet actually holds                      11.00248643
    deposit_events rows across all seven                                   0

Not one of those seven ever received a coin. They are quote windows somebody
opened and walked away from. And swap_terminal/fee_sweep.obligation() counts every
non-terminal swap as money the wallet must retain, so those 2,499 GRC sat as a
floor that no balance this desk will ever hold could clear -- which refuses every
fee sweep on the asset, permanently, and is the exact outcome fee_sweep.py's own
header says it exists to prevent.

THE CAUSE WAS THAT NOTHING COULD EVER CLOSE THEM. services/swap_view.py said so in
as many words, and said it as a measurement rather than a complaint:

    **Nothing in this tree ever sets swaps.status = 'expired'**, even though
    every swap row carries an `expires_at` copied from its quote.

So a swap opened and never funded stayed `awaiting_deposit` forever. The deposit
watcher kept scanning its address every cycle, the obligation floor kept retaining
its payout, and the only thing that would ever have changed either was a person
writing an UPDATE by hand. This is that UPDATE, with the checks that make it safe
to run and a way back from it.

=============================================================================
WHAT IS PROVEN BEFORE A SWAP IS RETIRED, AND WHAT IS NOT
=============================================================================

Five conditions, all of which must hold (expiry_verdict() below is the decision,
callable with seeded rows and no database):

  status is awaiting_deposit   The ONLY status where nothing has arrived. The next
                               two -- `deposit_seen` and `confirming` -- mean a
                               transaction is visible on chain, which is money in
                               flight, and `payout_pending` onward means it was
                               credited. Those are not candidates for anything.
  zero deposit_events rows     ANY row refuses, at ANY confirmation count,
                               credited or not. A 0-confirmation row is money on
                               its way, not an absence.
  zero payouts rows            A payout row on an awaiting_deposit swap should be
                               impossible. If one exists, the bookkeeping is wrong
                               in a way this tool must not paper over.
  actual_input_amount IS NULL  The column services/deposit_service.py writes when
                               it credits. Set, with no deposit row, means the two
                               disagree and a person reads it, not this.
  the quote window lapsed      expires_at, PLUS a grace window, must be in the
                               past. --grace-hours sets it; the default is below.

AND HERE IS THE THING THIS TOOL CANNOT PROVE (rule 17, stated as the limit it is).
Zero deposit rows is a fact about THIS DATABASE. It is not a reading of the chain,
and the two come apart in exactly one measured way: services/admin_view.py's own
warning is that a swap sits "awaiting_deposit forever, because nothing is polling
the chain". If the deposit watcher was down while a customer paid, the coins are on
chain and this database knows nothing about them.

Retiring that swap does not destroy the money, and it does not strand it either --
but it DOES change what happens to it, and the change is in the unhelpful
direction:

    while awaiting_deposit    services/deposit_service.ACTIVE_STATUSES includes it,
                              so the address is scanned every cycle and a late
                              deposit is CREDITED automatically.
    once expired              it is outside ACTIVE_STATUSES, so
                              services/late_deposit_service.py RECORDS the payment
                              against the swap and credits nothing. A person
                              resolves it by hand.

That is a real cost and it is why --apply is not the default, why the grace window
exists at all, and why --revive does. It is not a loss: late_deposit_service's
target set is the COMPLEMENT of ACTIVE_STATUSES rather than a list of finished
statuses, so `expired` is covered by that pass the moment it exists, without
anybody editing a second list. Checked, not assumed -- that module's _TARGETS_SQL
is `WHERE status NOT IN ({active})`.

THE HONEST VERSION OF THE CHECK IS THEREFORE: print the deposit target, say that
the absence is a database reading, and let the operator look at the chain if they
care to. The tool prints the address for every swap it would retire for that
reason.

=============================================================================
WHAT IT DELIBERATELY DOES NOT TOUCH
=============================================================================

wallet_inventory.               NOTHING, and it does not need to. Measured
                                2026-10-07 by grepping every writer of
                                hot_reserved: services/payout_service.
                                reserve_inventory() and its release, plus
                                repair_inventory_reservations.py. create_swap()
                                writes no inventory row at all, so an
                                awaiting_deposit swap holds no reservation to
                                release. (rescue_payout.py DOES release one,
                                because the swap it rescues got as far as a payout
                                claim. Different stage, different obligation --
                                named at both sites, rule 8.)
icp_deposit_subaccounts.        NOT FREED, and "freed" would be a fiction anyway.
                                services/icp_subaccount_service._ALLOCATE_SQL is
                                `COALESCE(MAX(subaccount_index), 0) + 1`, so an
                                index is never reused whether or not the row
                                exists. Deleting it would destroy the only mapping
                                from a subaccount to the swap that published it --
                                which is precisely what a late ICP deposit needs to
                                be attributable.
xrp swap tags.                  Same reasoning, same allocator shape
                                (services/xrp_tag_service), same answer.
quotes.                         Left alone. A quote is the record of a price that
                                was offered, and nothing reads an old one again:
                                get_quote_or_raise() refuses an expired quote
                                before a swap exists.

=============================================================================
IT IS REVERSIBLE, AND --revive IS NOT A CONVENIENCE
=============================================================================

Every other terminal status in this tree has a path back -- resolve_halted_swap.py
for `under_review`, settle_payout.py for `failed` -- and the reason this one needs
one is the limit above: "no deposit arrived" is a statement about a database, and
the one case it is wrong about is a watcher that was not running. So --revive puts
a swap back to `awaiting_deposit`, where the address is scanned again, through the
same compare-and-swap and with its own audit row. Nothing else in the swap is
touched by either direction, which is what makes the pair safe: the expiry writes
one column and the revival writes it back.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "swap_terminal"))

from config import Config
from db import db_session
from microfortnights import format_duration
from report_block import labeled
from services.helpers import parse_iso, utc_now_iso
from services.swap_service import set_swap_status

SELF = "expire_swap.py"

#: How long AFTER the quote window lapses before a swap may be retired, in seconds.
#:
#: 24 hours, and the number is a judgment rather than a measurement -- which is why
#: it is a flag (--grace-hours) and not a constant buried in the verdict. What it is
#: judging: a quote window is minutes long, so a lapse by itself says almost
#: nothing; a day of nothing on the chain says the customer is not coming. The cost
#: of being wrong is the one the header names -- an automatic credit becomes a
#: manual reconciliation -- not a lost coin, which is why a day is enough rather
#: than a week.
#:
#: The grace is measured from expires_at and NOT from updated_at, deliberately. A
#: swap in `awaiting_deposit` has had its updated_at written once, at creation, so
#: the two differ by the quote window's length -- but updated_at would also move if
#: anything ever touched the row, and a grace window that a status read can reset is
#: not a window.
DEFAULT_GRACE_SECONDS = 86400.0

#: The one status a swap may be retired FROM, and the one it is retired TO. Named
#: rather than written as literals in four places, because the pair is also what
#: --revive walks backwards.
RETIRABLE_FROM = "awaiting_deposit"
RETIRED = "expired"

#: Every awaiting_deposit swap, with the two counts the verdict needs, in ONE
#: statement.
#:
#: THE COUNTS ARE SUBQUERIES AND NOT A PYTHON LOOP (CLAUDE.md rule 20). "Does this
#: swap have any deposit row" is a join over rows already in the database, so it is
#: SQL; a loop issuing one SELECT per swap would answer the same question in a place
#: only its author can inspect, and would make the eligible set depend on the order
#: it happened to run in. As written, an operator with a sqlite3 prompt can paste
#: this and see exactly what the tool sees.
#:
#: IT SELECTS ONLY THE RETIRABLE STATUS AND STILL RETURNS REFUSALS. A swap listed
#: here can still be refused -- by a deposit row, a payout row, a credited amount or
#: a window that has not lapsed -- and that is on purpose: the operator asked what
#: can be retired, and "these four could and these three could not, because" is the
#: answer to that question. A WHERE that pre-filtered the refusals away would print
#: a shorter list and hide the reason (rule 14).
CANDIDATES_SQL = f"""
SELECT s.*,
       (SELECT COUNT(*) FROM deposit_events AS d WHERE d.swap_id = s.id) AS deposit_row_count,
       (SELECT COUNT(*) FROM payouts AS p WHERE p.swap_id = s.id)       AS payout_row_count
FROM swaps AS s
WHERE s.status = '{RETIRABLE_FROM}'
ORDER BY s.created_at ASC
"""  # noqa: S608 -- RETIRABLE_FROM is a module constant in this file, not input.


def quote_window_lapsed(
    expires_at: str | None, now_iso: str, grace_seconds: float
) -> tuple[bool, str]:
    """Has `expires_at` passed by at least `grace_seconds`? (answer, the sentence).

    AT THE BOTTOM AND ON ITS OWN because it is the only part of the verdict that
    does arithmetic on a clock, and a clock is the thing a test has to be able to
    hand in (rescue_payout.refused_before_signing() was extracted for the same
    reason). `now_iso` is passed rather than read here so a caller owns the clock --
    services/late_deposit_service.late_scan_targets() states that contract for the
    same reason.

    AN UNREADABLE TIMESTAMP IS A REFUSAL, NOT A LAPSE. A column that will not parse
    is absence of evidence about the window, and rule 2's distinction is the whole
    point: "I could not read when this expired" is not "this expired". The opposite
    default would retire every swap whose expires_at was ever written malformed.
    """
    if not expires_at:
        return False, "swaps.expires_at is empty, so there is no window to have lapsed"
    try:
        expired_at = parse_iso(expires_at)
        now = parse_iso(now_iso)
    except ValueError:
        # Narrow on purpose: datetime.fromisoformat raises ValueError and nothing
        # else for a malformed string. A TypeError from a column that is not a
        # string at all is a different defect and should surface.
        return False, f"swaps.expires_at ({expires_at!r}) is not a readable timestamp"
    elapsed = (now - expired_at).total_seconds()
    if elapsed < grace_seconds:
        remaining = grace_seconds - elapsed
        return False, (
            f"the quote window lapsed {format_duration(elapsed)} ago and the grace window is "
            f"{format_duration(grace_seconds)}, so this is retirable in another "
            f"{format_duration(remaining)}"
        )
    return True, (
        f"the quote window lapsed {format_duration(elapsed)} ago, past the "
        f"{format_duration(grace_seconds)} grace window"
    )


#: Every column on `swaps` that, if set, records that a deposit was SEEN -- each
#: with the sentence that says what it being set MEANS, because they do not mean
#: the same thing and a reader has to be told which one tripped.
#:
#: ALL THREE TOGETHER rather than actual_input_amount checked separately, which is
#: how this was first written. They are one question -- "does any column claim a
#: deposit this swap has no row for?" -- and three branches asking it three times
#: put expiry_verdict() at seven returns against ruff's PLR0911 ceiling of six.
#: The two honest fixes were to extract the decision or to suppress the finding;
#: rule 19 forbids the second and rule 12 says the ceiling is telling you a
#: decision wants its own function. It did.
DEPOSIT_WITNESS_COLUMNS = {
    "actual_input_amount": (
        "that is the credited amount services/deposit_service.py writes when a deposit clears, so the "
        "two records disagree and this tool refuses rather than choosing which one is right"
    ),
    "deposit_txid": (
        "something recorded a deposit transaction for this swap and the deposit_events rows do not show "
        "it, so a person reads it first"
    ),
    "credited_at": (
        "this swap is marked credited with nothing behind the mark, which is the one disagreement that "
        "must never be resolved by a tool"
    ),
}


def deposit_recorded_in_a_column(swap) -> str:
    """The refusal if any DEPOSIT_WITNESS_COLUMNS is set, or "" if none is.

    EXTRACTED FROM expiry_verdict() rather than left as a loop inside it, for the
    reason rule 12 gives about complexity ceilings: the loop pushed that function to
    seven returns against ruff's PLR0911 ceiling of six, and the two honest answers
    were to extract the decision or to suppress the finding. Rule 19 forbids the
    second, and this is the better half of the first anyway -- "does any column claim
    a deposit this swap has no row for" is one question, and it is now a question a
    test can ask with a dict.

    "" RATHER THAN False, the same shape rescue_payout.refused_before_signing()
    chose: the caller prints the sentence, and a boolean would make it write one of
    its own that could disagree with which column actually tripped.
    """
    for column, meaning in DEPOSIT_WITNESS_COLUMNS.items():
        value = swap[column]
        # `is not None` AND NOT TRUTHINESS, for actual_input_amount specifically: a
        # credited amount of 0.0 is falsey and is still a record that something was
        # assessed. The other two are strings where "" and NULL mean the same
        # absence, so the stricter test costs them nothing.
        if value is not None and value != "":
            return f"swaps.{column} is set ({value!r}) with no deposit row behind it: {meaning}"
    return ""


def expiry_verdict(
    swap,
    deposit_row_count: int,
    payout_row_count: int,
    now_iso: str,
    grace_seconds: float = DEFAULT_GRACE_SECONDS,
) -> tuple[bool, str]:
    """May this swap be retired? THE DECISION, as a function (CLAUDE.md rule 10).

    Returns (allowed, reason), and the reason is printed either way -- a refusal an
    operator cannot read is a refusal they will route around by hand, which is the
    one outcome worse than this tool not existing.

    ORDER IS MOST-FATAL FIRST, the same shape rescue_verdict() uses: the status,
    then any record of money, then the clock. The first refusal is the one reported,
    so an operator acts on one fact rather than four.

    THE COUNTS ARE INTS AND NOT ROW LISTS, unlike rescue_verdict()'s payout_rows,
    and the difference is deliberate: nothing here needs to look INSIDE a deposit
    row. One row at zero confirmations refuses exactly as hard as ten credited ones,
    so the only question is whether any exist -- and CANDIDATES_SQL answers it in
    SQL with two COUNT subqueries rather than hauling the rows into Python to
    measure their length.
    """
    status = swap["status"]
    if status != RETIRABLE_FROM:
        return False, (
            f"the swap is {status!r}, not {RETIRABLE_FROM!r}. Every other status is a statement that "
            f"something ARRIVED -- 'deposit_seen' and 'confirming' mean a transaction is on chain, "
            f"'payout_pending' onward means it was credited, and the terminal ones are already closed. "
            f"Nothing here retires a swap with money in it"
        )
    if deposit_row_count:
        return False, (
            f"{deposit_row_count} deposit_events row(s) exist for this swap, so money arrived. ANY row "
            f"refuses, at any confirmation count: a 0-confirmation row is a payment on its way, not an "
            f"absence. Read it with: python3 show_swap.py --swap {swap['id']}"
        )
    if payout_row_count:
        return False, (
            f"{payout_row_count} payout row(s) exist for a swap still in {RETIRABLE_FROM!r}, which "
            f"should be impossible -- a payout is claimed only after a deposit is credited. The "
            f"bookkeeping disagrees with itself and a person reads it before anything is written"
        )
    disagreement = deposit_recorded_in_a_column(swap)
    if disagreement:
        return False, disagreement
    lapsed, window = quote_window_lapsed(swap["expires_at"], now_iso, grace_seconds)
    if not lapsed:
        return False, window
    return True, (
        f"nothing arrived: 0 deposit rows, 0 payout rows, no credited amount, and {window}. This swap "
        f"is a quote window somebody opened and never paid into"
    )


def _swap_line(swap) -> str:
    """One pasteable line naming the swap, what it owes, and where it told somebody to pay.

    THE DEPOSIT TARGET IS ON IT ON PURPOSE. It is the one thing an operator needs to
    check the claim this tool cannot prove (see the header): zero deposit rows is a
    database reading, and the address is what they would look up on the chain if
    they wanted the other kind of answer.
    """
    target = swap["deposit_address"] or "(none)"
    tag = swap["deposit_tag"]
    if tag is not None:
        target = f"{target} tag={tag}"
    return (
        f"{swap['id']}  {swap['from_asset']}->{swap['to_asset']}  "
        f"owes {swap['output_amount_estimate']} + {swap['network_fee_reserve']} fee  "
        f"opened {swap['created_at']}  expires {swap['expires_at']}\n"
        f"      deposit target {target}"
    )


def judge(rows, now_iso: str, grace_seconds: float) -> tuple[list, list]:
    """Split candidate rows into (retirable, refused), each paired with its reason.

    THE LOOP, lifted out of main() so it can be called with seeded rows and no
    database and no printing. It is the only place expiry_verdict() is called, and
    it carries no decision of its own -- rule 10's "a module runs a submodule and
    holds no decision", one layer down.

    BOTH LISTS COME BACK, never just the retirable one. The refusals are the answer
    to the operator's actual question (rule 14): a tool that printed four retirable
    swaps and silently dropped three would read as "there are four", and the three
    it dropped are exactly the ones somebody needs to look at.
    """
    retirable, refused = [], []
    for row in rows:
        ok, reason = expiry_verdict(
            row, row["deposit_row_count"], row["payout_row_count"], now_iso, grace_seconds
        )
        (retirable if ok else refused).append((row, reason))
    return retirable, refused


def released_obligation(retirable) -> float:
    """What retiring these would take off the hot wallet's floor. THE NUMBER.

    payout + network_fee_reserve per swap, which is exactly what
    fee_sweep._owed_for() sums -- the wallet has to hold the payout AND the chain
    fee that sends it. Written here rather than importing fee_sweep because this
    file must not import anything that reads a balance or constructs an adapter;
    the agreement between the two is held by tests/test_expire_swap.py, which
    builds a floor with fee_sweep.obligation() before and after a retirement and
    asserts the difference IS this figure.
    """
    return sum(
        float(row["output_amount_estimate"]) + float(row["network_fee_reserve"] or 0.0)
        for row, _reason in retirable
    )


def _print_verdicts(rows, retirable, refused) -> None:
    """Both lists, with `(none)` where one is empty, and every count against its denominator."""
    print(f"\n  {len(rows)} swap(s) in {RETIRABLE_FROM!r} to judge  <- the denominator for everything "
          f"below", flush=True)
    print(f"\nRETIRABLE ({len(retirable)} of {len(rows)}):", flush=True)
    print("\n".join(f"  {_swap_line(row)}\n      {reason}" for row, reason in retirable) or "  (none)",
          flush=True)
    print(f"\nREFUSED ({len(refused)} of {len(rows)}):", flush=True)
    print("\n".join(f"  {row['id']}  {reason}" for row, reason in refused) or "  (none)", flush=True)


def _candidates(db, swap_id: str, db_path: str):
    """The rows to judge, or None having printed why there are none.

    None AND NOT AN EMPTY LIST, because the two mean different things and the
    caller's exit code differs: no rows to judge is a refusal about the swap the
    operator named, where an empty --all run is a legitimate "nothing is open".
    """
    rows = db.execute(CANDIDATES_SQL).fetchall()
    if not swap_id:
        return rows
    rows = [row for row in rows if row["id"] == swap_id]
    if rows:
        return rows
    # NOT "no such swap": it may exist in another status, and saying the wrong one
    # of those two sends an operator looking in the wrong place.
    present = db.execute("SELECT status FROM swaps WHERE id = ?", (swap_id,)).fetchone()
    if present is None:
        print(f"  REFUSED: no swap with id {swap_id} exists in {db_path}", flush=True)
    else:
        print(f"  REFUSED: swap {swap_id} is {present['status']!r}, not {RETIRABLE_FROM!r}. "
              f"Only a swap still waiting for its first deposit can be retired here.", flush=True)
    return None


def _retire(db, retirable) -> int:
    """Write the status changes. Returns how many actually moved."""
    moved = 0
    for row, reason in retirable:
        # THE COMPARE-AND-SWAP IS THE POINT OF CALLING set_swap_status() RATHER THAN
        # WRITING AN UPDATE HERE. A deposit can arrive between the SELECT and this
        # write -- the deposit watcher is polling that address the whole time -- and
        # that race is the one case where retiring is wrong. set_swap_status() writes
        # only while the row is still `awaiting_deposit`, logs the loss at INFO, and
        # returns False, so a deposit that landed mid-run keeps its swap.
        if set_swap_status(db, row["id"], RETIRED, f"{SELF}: {reason}", RETIRABLE_FROM):
            moved += 1
            print(f"  WROTE  {row['id']}  {RETIRABLE_FROM} -> {RETIRED}", flush=True)
        else:
            print(f"  DECLINED  {row['id']}  it is no longer {RETIRABLE_FROM!r} -- something moved it "
                  f"between the read and the write, which on this status means a deposit arrived. "
                  f"Nothing was written for it.", flush=True)
    db.commit()
    return moved


def _announce(args, db_path: str, grace_seconds: float) -> None:
    """What this run is about to do, BEFORE it does it (rule 14: announce, do not only report)."""
    mode = "REVIVE" if args.revive else (f"one swap: {args.swap}" if args.swap else
                                         f"ALL {RETIRABLE_FROM} swaps")
    print(f"{SELF}: {'APPLY -- rows WILL be written' if args.apply else 'DRY RUN -- nothing is written'}",
          flush=True)
    print(f"  database   {db_path}", flush=True)
    print(f"  mode       {mode}", flush=True)
    if not args.revive:
        print(f"  grace      {format_duration(grace_seconds)} after the quote window lapses "
              f"(--grace-hours {args.grace_hours})", flush=True)
    print("  this tool MOVES NO MONEY. It writes one status column and one audit row, and the only "
          "balance it changes is the obligation FLOOR in fee_sweep.obligation().", flush=True)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    started = time.monotonic()
    db_path = args.db or Config.DB_PATH
    grace_seconds = args.grace_hours * 3600.0

    if args.revive and not args.swap:
        parser.error("--revive acts on one swap and needs --swap. There is no bulk revival.")
    if not args.revive and not args.swap and not args.all:
        parser.error("say which: --swap <id>, or --all to walk every awaiting_deposit swap.")

    _announce(args, db_path, grace_seconds)
    exit_code = 0
    with db_session(str(db_path)) as db:
        if args.revive:
            return _revive(db, args, started)
        rows = _candidates(db, args.swap, str(db_path))
        if rows is None:
            exit_code = 3
        else:
            retirable, refused = judge(rows, utc_now_iso(), grace_seconds)
            _print_verdicts(rows, retirable, refused)
            if not retirable:
                print("\nNothing to do. No swap met every condition, which is a result and not a "
                      "failure (see the REFUSED list above for which condition each one missed).",
                      flush=True)
            else:
                print(f"\n  obligation this would release  {released_obligation(retirable)}  <- summed "
                      f"over the {len(retirable)} retirable swap(s), payout + network_fee_reserve each, "
                      f"which is exactly what fee_sweep.obligation() counts", flush=True)
                if args.apply:
                    moved = _retire(db, retirable)
                    print(f"\n  {moved} of {len(retirable)} retired, {len(retirable) - moved} declined "
                          f"by the compare-and-swap  <- a decline is not an error; see the line above it",
                          flush=True)
                    print("  next       the obligation floor is re-read on the next run of:\n"
                          "               python3 collect_fees.py", flush=True)
                else:
                    print(f"\nDRY RUN: nothing written. To retire them:\n"
                          f"    python3 {SELF}{' --swap ' + args.swap if args.swap else ' --all'} "
                          f"--grace-hours {args.grace_hours} --apply", flush=True)
    print(labeled("done in", format_duration(time.monotonic() - started)), flush=True)
    return exit_code


def _revive(db, args, started: float) -> int:
    """Put one retired swap back to `awaiting_deposit`, where its address is scanned again.

    SEPARATE FROM main()'s loop rather than a branch inside it, because it shares
    nothing with it: no candidate query, no verdict, no obligation total. What it
    shares is the compare-and-swap and the audit row, which is the part that had to
    be the same.
    """
    swap = db.execute("SELECT * FROM swaps WHERE id = ?", (args.swap,)).fetchone()
    if swap is None:
        print(f"  REFUSED: no swap with id {args.swap} exists", flush=True)
        return 2
    if swap["status"] != RETIRED:
        print(f"  REFUSED: swap {args.swap} is {swap['status']!r}, not {RETIRED!r}. This flag undoes a "
              f"retirement and there is nothing to undo.", flush=True)
        return 3
    print(f"\n  {_swap_line(swap)}", flush=True)
    print(f"  revival    {RETIRED} -> {RETIRABLE_FROM}, which puts the deposit address back into "
          f"services/deposit_service.ACTIVE_STATUSES so the watcher scans it again and a deposit "
          f"credits automatically", flush=True)
    if not args.apply:
        print(f"\nDRY RUN: nothing written. To revive it:\n"
              f"    python3 {SELF} --swap {args.swap} --revive --apply", flush=True)
        print(labeled("done in", format_duration(time.monotonic() - started)), flush=True)
        return 0
    moved = set_swap_status(
        db, args.swap, RETIRABLE_FROM,
        f"{SELF} --revive: retirement undone, the deposit address is scanned again", RETIRED,
    )
    db.commit()
    print(f"  {'WROTE      ' + RETIRED + ' -> ' + RETIRABLE_FROM if moved else 'DECLINED   it is no longer ' + repr(RETIRED) + ', so nothing was written'}",
          flush=True)
    print(labeled("done in", format_duration(time.monotonic() - started)), flush=True)
    return 0 if moved else 3


def build_parser() -> argparse.ArgumentParser:
    """The CLI, as a function so a test can assert every advertised flag parses.

    EXTRACTED FOR A MEASURED REASON, and not this file's: supervisor.py's help text
    advertised a `-f` its parser rejected, and the only way to catch that class of
    defect is to build the parser without running the tool. Same shape here.
    """
    parser = argparse.ArgumentParser(
        prog=SELF,
        description=(
            "Retire a swap that never received a deposit, so it stops counting against the hot "
            "wallet's obligation floor. Writes one status column and one audit row, and only with "
            "--apply."
        ),
    )
    parser.add_argument("--swap", default="", help="one swap id to judge (or to revive)")
    parser.add_argument("--all", action="store_true",
                        help=f"judge every swap in {RETIRABLE_FROM!r}")
    parser.add_argument("--db", default="", help=f"database (default: {Config.DB_PATH})")
    parser.add_argument("--grace-hours", type=float, default=DEFAULT_GRACE_SECONDS / 3600.0,
                        help="hours after the quote window lapses before a swap may be retired "
                             f"(default: {DEFAULT_GRACE_SECONDS / 3600.0:g})")
    parser.add_argument("--revive", action="store_true",
                        help="put one retired swap back to awaiting_deposit. Needs --swap.")
    parser.add_argument("--apply", action="store_true",
                        help="actually write. Without it nothing is written and the checks still run.")
    return parser


if __name__ == "__main__":
    raise SystemExit(main())
