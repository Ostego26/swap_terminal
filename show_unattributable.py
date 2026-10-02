#!/usr/bin/env python3
"""Deposits that reached the account but matched no swap. Money nobody can claim. Read-only.

Role: file (entry point, at the repository root per CLAUDE.md rule 10)
Reads: DB_PATH, through workers/common.get_config_dict(), and swap_terminal.db -- the
       `unattributable_deposits` table only, through
       services/unattributable_deposit_service.outstanding(). It opens no
       socket: no chain, no daemon, no RPC.
Writes: NOTHING. Not a row, not a view, not the database file -- the path is
       checked with Path.exists() before connect(), because connect() CREATES a
       missing file and a read-only tool that leaves an empty database behind
       has written something while announcing that it would not. Same guard, for
       the same reason, as show_fees.py and show_swap.read_report().
Can move funds: no. It has no --resolve, no --refund and no --credit. Deciding
       that one of these deposits belongs to somebody, and sending them coins,
       is fund movement and the operator's call (rule 16). This tool exists to
       put the evidence for that call on a screen, and resolution goes through
       services/unattributable_deposit_service.resolve_credited() when a scan
       establishes it, never through a report.
Mainnet-safe: yes. It chooses no network and reaches none.

WHY THIS FILE EXISTS.

Measured 2026-10-02: NOTHING in the tree could show these rows. Four files
reference `unattributable_deposits` outside tests -- the recorder
(services/unattributable_deposit_service.py), the adapter that drops into it
(chains/solana.py), the schema (db.py), and pay_test_deposit.py -- and not one
of them lists it. Every argument parser at the repository root was read; none
reports an unclaimed deposit.

So the operator's host had carried two unclaimed SOL deposits since 2026-10-01
with no way to look at them. I handed them a hand-written SELECT to count them
and got the column name wrong -- `reason`, where the schema says `why` -- and
the query died on the one table in this database whose rows are somebody's
money. That is the argument for a tool rather than a one-off query: a SELECT
typed fresh each time can be wrong each time, and being wrong here means an
operator concludes there is nothing to look at.

The count itself was right, and it is the denominator for the 2026-10-02 skip
set fix (rule 3): two SOL signatures, each previously costing one getTransaction
per cycle against an endpoint answering HTTP 429.

WHAT IT REIMPLEMENTS: NOTHING.

    the query             unattributable_deposit_service.OUTSTANDING_SQL -- one
                          SELECT, and the only place it exists (rules 5 and 20)
    outstanding-ness      derived in that SQL as a column, not recomputed here
    the database path     workers/common.db_path_source(), so this tool names
                          the same file the workers poll and says WHERE the path
                          came from. Pointing three workers at a database I
                          invented is a mistake already made in this repo once,
                          and it cost an hour of IDLE cycles.
    the label alignment   report_block.labeled()
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "swap_terminal"))

from db import connect_db
from report_block import CONTINUATION, labeled
from services.unattributable_deposit_service import discriminator_name, outstanding
from workers.common import db_path_source, get_config_dict, root_tool_command


class Refused(Exception):
    """This tool will not run, and the message says what the operator can do about it."""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "List deposits that arrived but matched no swap. Read-only: it cannot "
            "credit, refund or resolve anything."
        )
    )
    parser.add_argument(
        "--asset",
        default=None,
        help="Only this asset (SOL, XRP, GRC). Default: every asset.",
    )
    parser.add_argument(
        "--include-resolved",
        action="store_true",
        help=(
            "Also show deposits a human has already dealt with. Default is "
            "outstanding only, because a resolved row is history and an "
            "outstanding one is somebody still owed an answer."
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
    that decide the answer. Pasted output is read a day later and has to say
    which database and which filter produced it -- an empty report from a
    --asset=SOL run and an empty report from the whole table are different
    facts and would otherwise render identically.
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
    """One unclaimed deposit, as the operator has to read it to act on it.

    EVERY FIELD A HUMAN MATCHING THIS BY HAND NEEDS, which is the whole reason
    the table carries them rather than pointing at the chain: what arrived, where
    it landed, in how many pieces, what reference it carried if any, why it was
    refused in the adapter's own words, and how long it has been sitting.
    """
    asset = str(row["asset"])
    discriminator = row["discriminator"]
    # NULL AND A VALUE ARE TWO DIFFERENT SUPPORT CONVERSATIONS, which is why the
    # schema makes this column nullable rather than defaulting it to 0. NULL is
    # "you sent without a reference"; a value is "your reference matches no
    # order". An operator who cannot tell them apart has to open the chain.
    if discriminator is None:
        reference = f"(none) <- arrived with no {discriminator_name(asset)} at all"
    else:
        reference = f"{discriminator} <- carried this, and it matched no open swap"
    lines = [
        "",
        labeled("txid", str(row["txid"])),
        labeled("asset", f"{asset}  {row['amount']} in {row['credits']} credit(s)"),
        labeled("landed in", str(row["address"])),
        labeled("reference", reference),
        labeled("why refused", str(row["why"])),
        labeled("confirmations", str(row["confirmations"])),
        labeled("first seen", str(row["first_seen_at"])),
        labeled("last seen", str(row["last_seen_at"])),
    ]
    if row["resolved_at"]:
        note = row["resolution_note"] or "(no note)"
        lines.append(labeled("RESOLVED", f"{row['resolved_at']}  {note}"))
    else:
        lines.append(
            labeled("status", "OUTSTANDING <- nobody has been given these coins")
        )
    return lines


def total_lines(rows, args) -> list[str]:
    """The count, with what it was counted out of (rule 3) and what it means (rule 14).

    `(none)` RATHER THAN A BLANK, because a blank gap is ambiguous between zero
    rows and a query that broke -- and this tool exists precisely because a
    broken query about this table read as "nothing to see".
    """
    outstanding_count = sum(1 for row in rows if row["outstanding"])
    if not rows:
        scope = f"for {args.asset}" if args.asset else "in any asset"
        shown = "" if args.include_resolved else ", outstanding or not"
        return [
            "",
            f"(none)  <- no unattributable deposits {scope}{shown}. Every deposit "
            "this terminal has seen reached a swap.",
        ]
    per_asset: dict[str, int] = {}
    for row in rows:
        per_asset[str(row["asset"])] = per_asset.get(str(row["asset"]), 0) + 1
    breakdown = ", ".join(f"{asset} {n}" for asset, n in sorted(per_asset.items()))
    return [
        "",
        labeled("deposits shown", f"{len(rows)}   ({breakdown})"),
        labeled(
            "outstanding",
            f"{outstanding_count} of {len(rows)}   <- rows where somebody is still "
            "owed an answer",
        ),
        CONTINUATION
        + "<- each outstanding row is also one getTransaction the deposit scan now",
        CONTINUATION
        + "   skips per cycle rather than re-reads; grep the worker log for",
        CONTINUATION + '   "did not re-read" to see the live count',
    ]


def run(args) -> int:
    config = get_config_dict()
    db_path = Path(args.db) if args.db else Path(config["DB_PATH"])
    source = db_path_source(db_path, explicit_db=args.db)
    # Path.exists() BEFORE connect(), because sqlite3.connect() creates the file.
    # A read-only tool that leaves a new empty database on disk has written
    # something, and the next reader finds a database with no tables and no
    # explanation.
    if not db_path.exists():
        raise Refused(
            f"no database at {db_path} ({source}). Nothing was created. If the "
            f"workers have never run, there is no deposit history to read yet."
        )
    print("\n".join(header_lines(db_path, source, args)))
    db = connect_db(str(db_path))
    try:
        rows = outstanding(
            db, args.asset, include_resolved=args.include_resolved
        )
    except sqlite3.OperationalError as error:
        raise Refused(
            f"could not read unattributable_deposits from {db_path}: {error}. A "
            f"missing table means this database predates the deposit attribution "
            f"schema, not that there are no unclaimed deposits."
        ) from error
    for row in rows:
        print("\n".join(deposit_lines(row)))
    print("\n".join(total_lines(rows, args)))
    print()
    print(labeled("this report", root_tool_command("show_unattributable.py")))
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
