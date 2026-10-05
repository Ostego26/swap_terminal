#!/usr/bin/env python3
"""Money that arrived at a finished swap's deposit address. The desk is holding it. Read-only.

Role: file (entry point, at the repository root per CLAUDE.md rule 10)
Reads: DB_PATH, through workers/common.get_config_dict(), and swap_terminal.db --
       the `late_deposits` table only, through
       services/late_deposit_service.outstanding(). It opens no socket: no chain,
       no daemon, no RPC.
Writes: NOTHING. Not a row, not a view, not the database file -- the path is
       checked with Path.exists() before connect(), because connect() CREATES a
       missing file and a read-only tool that leaves an empty database behind has
       written something while announcing that it would not. Same guard, for the
       same reason, as show_unattributable.py, show_fees.py and
       show_swap.read_report().
Can move funds: no. There is no --resolve, no --refund and no --credit. Deciding
       that one of these payments should be returned or credited, and sending
       coins, is fund movement and the operator's call (rule 16). This tool puts
       the evidence for that call on a screen.
Mainnet-safe: yes. It chooses no network and reaches none.

WHY THIS FILE EXISTS.

services/late_deposit_service.py has recorded these rows since 2026-10-04 and
late_note() announces the count on every cycle line -- in those words: "the desk
is holding those coins and no swap accounts for them... read the late_deposits
table and decide."

NOTHING COULD READ THAT TABLE. Measured 2026-10-05 across the repository root:
every argparse tool was listed, and the files referencing `late_deposits` outside
tests were the recorder, the schema (db.py) and the cycle line that names it.
Not one of them lists a row. So the cycle told the operator to read a table, and
the only way to do it was a hand-typed SELECT.

That is the same gap show_unattributable.py was written to close three days
earlier, and that one has the cautionary measurement attached: a SELECT typed
fresh each time can be wrong each time, and the hand-written one got a column
name wrong (`reason`, where the schema says `why`) against the single table in
this database whose rows are somebody's money. A query that dies reads, to an
operator, exactly like a table with nothing in it.

Live on the operator's host as this was written: THREE unresolved rows, every
one `resolved_at: None` -- 0.01 LTC, 0.01 BTC and 0.0001 BTC. Real money, held
by the desk, with no tool able to show it.

WHAT IT REIMPLEMENTS: NOTHING.

    the query             late_deposit_service.OUTSTANDING_SQL -- one SELECT, and
                          the only place it exists (rules 5 and 20)
    outstanding-ness      derived in that SQL as a column, not recomputed here.
                          A root tool spelling `resolved_at IS NULL` a second
                          time is rule 8's bug with a delay on it.
    the database path      workers/common.db_path_source(), so this tool names the
                          same file the workers poll and says WHERE the path came
                          from
    the label alignment    report_block.labeled()

AND ONE THING IT DELIBERATELY DOES *NOT* REUSE: late_deposit_service.late_note().
The first draft printed it, for symmetry with the cycle line. Running the tool
against seeded rows showed the sentence is FALSE here, which is why it is worth a
paragraph rather than a quiet deletion.

late_note() describes the CYCLE's number: rows NEW THIS PASS, against a count of
finished swaps that were inside the scan window. Its own words are "It counts rows
NEW this pass, so it returns to 0 once each is recorded". This tool's number is
every row still OUTSTANDING, at any age, and it has no window and no target count
at all -- so the borrowed sentence told the reader the figure would return to zero
on its own, about a figure that only moves when a human resolves something. Two
numbers that happen to be integers are not the same number, and reuse that makes
printed output false is worse than the duplication it avoids (rule 16: a wrong
comment is a bug, and a wrong explanation printed on an operator's screen is the
same bug with an audience).

WHY IT DOES NOT JOIN TO `swaps`, which is the one thing a reader will reach for.
db.py:271-276 carries `swap_status` on the row on purpose: an operator reading
this a week later needs the status AT THE MOMENT THE MONEY ARRIVED, because that
is what decides whether the desk is holding a customer's extra send (the payout
had already completed) or may still owe the original payout too (it failed).
Joining would give today's status, which is not the one that made this a late
deposit. The omission is the feature.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "swap_terminal"))

from db import connect_db
from report_block import CONTINUATION, labeled
from services.late_deposit_service import outstanding
from workers.common import db_path_source, get_config_dict, root_tool_command


class Refused(Exception):
    """This tool will not run, and the message says what the operator can do about it."""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "List payments that arrived at the deposit address of a swap that had "
            "already finished. Read-only: it cannot credit, refund or resolve anything."
        )
    )
    parser.add_argument(
        "--asset",
        default=None,
        help="Only this asset (BTC, LTC, GRC, SOL, XRP). Default: every asset.",
    )
    parser.add_argument(
        "--include-resolved",
        action="store_true",
        help=(
            "Also show rows a human has already dealt with. Default is outstanding "
            "only, because a resolved row is history and an outstanding one is "
            "money the desk is still sitting on."
        ),
    )
    parser.add_argument(
        "--db",
        default="",
        help="Database path, overriding config. Default: the path the workers use.",
    )
    return parser


def header_lines(db_path: Path, source: str, args) -> list[str]:
    """What is being read and with what filters, BEFORE any row is printed.

    Rule 14: announce the target and the scale up front, and echo the parameters
    that decide the answer. Pasted output is read a day later and has to say which
    database and which filter produced it -- an empty report from `--asset BTC` and
    an empty report over the whole table are different facts that would otherwise
    render identically.
    """
    scope = args.asset or "every asset"
    shown = "outstanding and resolved" if args.include_resolved else "outstanding only"
    return [
        labeled("database", f"{db_path}"),
        CONTINUATION + f"<- {source}",
        labeled("asset", scope),
        labeled("showing", shown),
    ]


def deposit_lines(row) -> list[str]:
    """One late payment, as the operator has to read it to act on it.

    THE TWO FIELDS THAT DECIDE THE ACTION ARE `swap_status` AND `amount`, and the
    status gets a sentence rather than a bare token. "completed" and "failed" lead
    to opposite conversations with the same customer -- in one the desk owes a
    refund of an extra send, in the other it may still owe the original payout too
    -- and an operator reading a bare word has to go and look that up. Rule 14:
    state what the value means, next to the value.
    """
    status = str(row["swap_status"])
    if status == "completed":
        meaning = f"{status} <- its payout already went out; this is money ON TOP of the swap"
    elif status in ("failed", "refunded", "expired"):
        meaning = f"{status} <- its payout did NOT complete; the desk may owe the original too"
    else:
        meaning = f"{status} <- the status when the money landed, carried not looked up"
    lines = [
        "",
        labeled("txid", f"{row['txid']}:{row['vout']}"),
        labeled("asset", f"{row['asset']}  {row['amount']}"),
        labeled("landed in", str(row["address"])),
        labeled("for swap", str(row["swap_id"])),
        labeled("swap was", meaning),
        labeled("confirmations", str(row["confirmations"])),
        labeled("first seen", str(row["first_seen_at"])),
        labeled("last read", f"{row['last_seen_at']}  <- when the scan last READ this on-chain"),
    ]
    if row["resolved_at"]:
        note = row["resolution_note"] or "(no note)"
        lines.append(labeled("RESOLVED", f"{row['resolved_at']}  {note}"))
    else:
        lines.append(
            labeled("status", "OUTSTANDING <- nobody has been given these coins back")
        )
    return lines


def total_lines(rows, args) -> list[str]:
    """The count, with what it was counted out of (rule 3) and what it means (rule 14).

    `(none)` RATHER THAN A BLANK, because a blank gap is ambiguous between zero rows
    and a query that broke -- and the sibling tool for the sibling table exists
    precisely because a broken query about it read as "nothing to see".

    THE EMPTY MESSAGE MUST NOT OVERCLAIM, and the first draft did, copied straight
    from show_unattributable.py: it said "no late deposits for XRP, outstanding or
    not" on a run that had queried OUTSTANDING ONLY. The query cannot see resolved
    rows unless --include-resolved is passed, so "or not" asserted something the
    SELECT never looked at. Same defect fixed in show_unattributable.py in the same
    commit (rule 19: when you find N instances, fix them, not one).

    THE PER-ASSET TOTAL IS A SUM, not just a count, and that is the difference from
    show_unattributable.py. Here every row is the same kind of thing -- an overpay
    into one address -- so "BTC 0.0201 in 2" is the figure an operator acts on. A
    bare "BTC 2" would make them add it up themselves off the rows above.
    """
    outstanding_count = sum(1 for row in rows if row["outstanding"])
    if not rows:
        scope = f"for {args.asset}" if args.asset else "in any asset"
        if args.include_resolved:
            qualifier = "outstanding or resolved"
            consequence = (
                "Every payment this terminal has seen arrived while its swap was "
                "still open."
            )
        else:
            qualifier = "OUTSTANDING"
            consequence = (
                "The desk is holding nothing unaccounted for. Resolved rows were "
                "not queried -- pass --include-resolved to see those."
            )
        return [
            "",
            f"(none)  <- no {qualifier} late deposits {scope}. {consequence}",
        ]
    per_asset: dict[str, list] = {}
    for row in rows:
        entry = per_asset.setdefault(str(row["asset"]), [0, 0.0])
        entry[0] += 1
        entry[1] += float(row["amount"])
    breakdown = ", ".join(
        f"{asset} {total:.8f} in {n}" for asset, (n, total) in sorted(per_asset.items())
    )
    # LABELS FIT IN report_block.LABEL_WIDTH (16) OR THEY EAT THE SEPARATING SPACE.
    # The first draft used "late deposits shown", 19 characters, and printed
    # `late deposits shown3` -- the count welded to its own label. labeled() pads to
    # a width rather than truncating, which is correct (truncating a label loses
    # meaning), so the caller owes it a label that fits.
    lines = [
        "",
        labeled("rows shown", f"{len(rows)}   ({breakdown})"),
        labeled(
            "outstanding",
            f"{outstanding_count} of {len(rows)}   <- rows where the desk still holds coins",
        ),
    ]
    if outstanding_count:
        lines += [
            "",
            f"{outstanding_count} payment(s) arrived at the deposit address of a swap",
            "that had ALREADY FINISHED. The desk is holding those coins and no swap",
            "accounts for them. NOTHING here was credited, refunded or changed by this",
            "report, and this count does NOT fall on its own -- it moves only when a",
            "human resolves a row.",
        ]
    return lines


def run(args) -> int:
    config = get_config_dict()
    db_path = Path(args.db) if args.db else Path(config["DB_PATH"])
    source = db_path_source(db_path, explicit_db=args.db)
    # Path.exists() BEFORE connect(), because sqlite3.connect() creates the file. A
    # read-only tool that leaves a new empty database on disk has written something,
    # and the next reader finds a database with no tables and no explanation.
    if not db_path.exists():
        raise Refused(
            f"no database at {db_path} ({source}). Nothing was created. If the workers "
            f"have never run, there is no deposit history to read yet."
        )
    print("\n".join(header_lines(db_path, source, args)))
    db = connect_db(str(db_path))
    try:
        rows = outstanding(db, args.asset, include_resolved=args.include_resolved)
    except sqlite3.OperationalError as error:
        raise Refused(
            f"could not read late_deposits from {db_path}: {error}. A missing table "
            f"means this database predates the late-deposit schema, not that no "
            f"payment has ever arrived late."
        ) from error
    for row in rows:
        print("\n".join(deposit_lines(row)))
    print("\n".join(total_lines(rows, args)))
    print()
    print(labeled("this report", root_tool_command("show_late_deposits.py")))
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return run(args)
    except Refused as refusal:
        print(f"\nREFUSED: {refusal}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
