#!/usr/bin/env python3
"""Move every swap out of an orphaned database into the authority, then say what moved.

Role: file (entry point at the repository root, per CLAUDE.md rule 10)
Reads: the SOURCE database read-only (mode=ro, so it cannot be altered even by
       accident) and the DESTINATION's schema
Writes: the DESTINATION database, and only with --apply: one row per swap and per
       swap-referencing row that is not already there. It writes NOTHING to the
       source and it DELETES NOTHING anywhere.
Can move funds: no. It signs nothing, broadcasts nothing and opens no socket. It
       can make a swap VISIBLE to the workers that pay out, which is why every
       insert is a row the destination did not have and never an overwrite.
Mainnet-safe: it chooses no network and reaches none.

=============================================================================
WHY THIS EXISTS, AND THE SECOND DATABASE WAS MY FAULT
=============================================================================

The operator's host had two, found 2026-10-10:

    327,680 bytes   52 swaps   newest 2026-10-10T21:57   <- the authority
    118,784 bytes    1 swap    newest 2026-10-01T22:34   <- an orphan, 9 days dead

CLAUDE.md rule 15 is one sentence: "One database is the authority." Two is the
defect, and the cause is recorded in workers/common.database_census() because it
already cost an hour of their evening on 2026-10-01:

    Three workers were started from a shell whose SWAP_DB_PATH pointed at the
    wrong file -- repo_root/runtime/swap_terminal.db instead of
    repo_root/swap_terminal/swap_terminal.db, A PATH I PUT IN A BLOCK I HANDED
    THEM.

And the reason a typo became a database rather than an error:

    connect_db() CREATES WHAT IT CANNOT FIND. db.py is a bare sqlite3.connect(),
    which makes a missing file rather than refusing, and no worker applies SCHEMA.
    So a typo in a path does not fail: it manufactures an empty database and polls
    it forever.

The operator's instruction, 2026-10-10: "you better merge then cull that shit".

=============================================================================
WHY THIS MERGES RATHER THAN LETTING THE ORPHAN BE DELETED
=============================================================================

The orphan holds one swap and it is `failed`, so nothing is owed on it. Deleting
the file would be the shorter route and CLAUDE.md rule 7 forbids it outright:

    Never delete that record to save space. If disk is the problem, the answer is
    a churn table, not the record of what the system did with money.

A failed swap is the MORE valuable half of that record -- rule 7 says so in the
same paragraph -- because it says what went wrong. So the rows move first, the
operator confirms them in the authority, and only then is the file redundant.

=============================================================================
WHAT IT REFUSES, AND IT NEVER OVERWRITES
=============================================================================

  an id already present    REFUSED for that row, reported, and the rest still
                           move. Two databases that both hold `s_abc` do not hold
                           the same swap -- ids are generated per database -- so
                           an overwrite would replace a real swap in the authority
                           with an unrelated one. There is no --force.
  a schema mismatch        REFUSED outright. A table in the source that the
                           destination does not have, or a column the destination
                           lacks, and nothing is written: a partial move leaves
                           rows referencing a swap that did not arrive.
  the same file twice      REFUSED. Absorbing a database into itself would insert
                           every row over itself and report success.

THE SOURCE IS OPENED mode=ro, which is stronger than intent: sqlite refuses a
write on that connection, so a bug in this file cannot damage the evidence it is
reading.

=============================================================================
THE ORDER IS FOREIGN-KEY ORDER AND THAT IS NOT COSMETIC
=============================================================================

quotes before swaps, swaps before everything that references one. db.py's SCHEMA
turns `PRAGMA foreign_keys=ON`, so inserting a swap before its quote raises --
and an insert order that happens to work today would break the day a table gains
a reference. TABLE_ORDER below is derived from the schema's own FOREIGN KEY
clauses rather than typed from memory.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "swap_terminal"))


SELF = Path(__file__).name

#: Insert order: a table may only be written after everything it references.
#: quotes first because swaps.quote_id points at it; swaps next; then every table
#: carrying a swap_id. Derived from SCHEMA's FOREIGN KEY clauses, not recalled.
TABLE_ORDER = (
    "quotes",
    "swaps",
    "address_proof_challenges",
    "deposit_events",
    "icp_deposit_subaccounts",
    "late_deposits",
    "payouts",
    "swap_audit_log",
    "xrp_destination_tags",
)

#: The column that ties a row to a swap, per table. `swaps` is keyed by its own id
#: and `quotes` is reached through swaps.quote_id, so neither appears here.
SWAP_COLUMN = "swap_id"


def columns_of(conn: sqlite3.Connection, table: str) -> list[str]:
    """This table's column names in declaration order, read by NAME from PRAGMA.

    BY NAME, NOT POSITION, for the reason db.add_column_if_missing() records: a
    positional read of a PRAGMA is a guess about the caller's row factory, and
    `row[1]` raises KeyError on the dict factory this repository installs -- a
    standalone check passed while the app's own connection broke collection of the
    whole suite on 2026-09-26.
    """
    return [row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()]


def refuse_before_touching_anything(source: Path, destination: Path, src, dst) -> list[str]:
    """Every reason not to proceed. Empty means the move may run. Writes nothing.

    CHECKED AS A SET BEFORE THE FIRST INSERT, not discovered during it. A merge
    that fails halfway leaves rows referencing a swap that did not arrive, and
    sqlite's foreign keys would then refuse the NEXT run too -- so the second
    attempt reports a problem the first one created.
    """
    reasons = []
    if source.resolve() == destination.resolve():
        reasons.append(
            f"the source and the destination are the same file ({source.resolve()}). Absorbing "
            f"a database into itself would insert every row over itself and report success."
        )
    for table in TABLE_ORDER:
        src_cols = columns_of(src, table)
        if not src_cols:
            continue
        dst_cols = columns_of(dst, table)
        if not dst_cols:
            reasons.append(
                f"the source has a {table!r} table and the destination does not, so its rows "
                f"have nowhere to go. Nothing was written."
            )
            continue
        absent = [name for name in src_cols if name not in dst_cols]
        if absent:
            reasons.append(
                f"{table}: the source has column(s) {absent} the destination lacks, so those "
                f"values would be dropped silently. Nothing was written."
            )
    return reasons


def plan(src, dst) -> dict:
    """What would move, per table, and what is already there. PURE READS, both sides.

    RETURNS THE ROWS, not just counts, so --apply inserts exactly what the dry run
    printed. A plan that recomputed at write time could print one thing and do
    another -- which is the whole hazard a dry run exists to remove.
    """
    swap_rows = [dict(row) for row in src.execute("SELECT * FROM swaps ORDER BY created_at").fetchall()]
    present = {row["id"] for row in dst.execute("SELECT id FROM swaps").fetchall()}
    moving = [row for row in swap_rows if row["id"] not in present]
    colliding = [row["id"] for row in swap_rows if row["id"] in present]

    quote_ids = {row["quote_id"] for row in moving if row.get("quote_id")}
    have_quotes = {row["id"] for row in dst.execute("SELECT id FROM quotes").fetchall()}
    quotes = [
        dict(row)
        for qid in sorted(quote_ids - have_quotes)
        for row in src.execute("SELECT * FROM quotes WHERE id = ?", (qid,)).fetchall()
    ]

    moving_ids = [row["id"] for row in moving]
    children: dict[str, list[dict]] = {}
    for table in TABLE_ORDER:
        if table in ("quotes", "swaps") or not columns_of(src, table):
            continue
        if SWAP_COLUMN not in columns_of(src, table):
            continue
        rows = []
        for swap_id in moving_ids:
            rows += [
                dict(row) for row in src.execute(
                    f"SELECT * FROM {table} WHERE {SWAP_COLUMN} = ?", (swap_id,)  # noqa: S608 -- `table` is a literal from TABLE_ORDER in this module; the value is bound
                ).fetchall()
            ]
        if rows:
            children[table] = rows
    return {"quotes": quotes, "swaps": moving, "children": children, "colliding": colliding}


def insert_rows(dst, table: str, rows: list[dict]) -> int:
    """Insert these rows, dropping each row's own primary key where it is a rowid.

    THE `id` COLUMN IS DROPPED FOR AUTOINCREMENT TABLES, which is the one
    transformation this tool makes. deposit_events, payouts and swap_audit_log key
    on `INTEGER PRIMARY KEY AUTOINCREMENT`, and those numbers are per-database --
    the orphan's payout 1 and the authority's payout 1 are different payments. So
    the destination assigns fresh ones and the rows keep their swap_id, which is
    what actually identifies them.
    """
    if not rows:
        return 0
    autoincrement = "id" in columns_of(dst, table) and table not in ("swaps", "quotes")
    written = 0
    for row in rows:
        payload = {k: v for k, v in row.items() if not (autoincrement and k == "id")}
        names = ", ".join(payload)
        marks = ", ".join("?" for _ in payload)
        dst.execute(
            f"INSERT INTO {table} ({names}) VALUES ({marks})",  # noqa: S608 -- identifiers come from PRAGMA table_info on this very table; every value is bound
            tuple(payload.values()),
        )
        written += 1
    return written


def open_both(source: Path, destination: Path):
    """Both connections, or an exit code. Extracted so main() holds the sequence only.

    RETURNS AN INT ON REFUSAL rather than raising, because this is the one refusal that
    happens before anything is open and the caller's answer is an exit status. Splitting
    it out is rule 12's answer to ruff's complexity ceiling on main(): the ceiling was
    pointing at a function that had swallowed the path checks, not at a line count.

    THE SOURCE IS mode=ro AND THAT IS THE GUARD, not a convention. sqlite refuses a write
    on that handle, so no bug in this file can alter the evidence it is reading -- which
    matters because the source is the only copy of whatever it holds.

    BOTH MUST EXIST. The destination is NOT created: db.connect_db() creating a missing
    file is what produced the orphan this tool exists to absorb (workers/common.
    database_census() records the hour it cost), and a merge tool that manufactured its
    own destination would make a third database out of a typo in the same way.
    """
    for path, what in ((source, "source"), (destination, "destination")):
        if not path.is_file():
            print(f"  REFUSED: the {what} {path} does not exist. Nothing was read or written.", flush=True)
            return 2
    src = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    src.row_factory = sqlite3.Row
    dst = sqlite3.connect(str(destination))
    dst.row_factory = sqlite3.Row
    return src, dst


def report(found: dict) -> list[str]:
    """What moved or would move, per table, with (none) where nothing did.

    RULE 14: `(none)` IS A RESULT. A table that contributed nothing prints a zero
    rather than being omitted, because an absent line is ambiguous between "no rows"
    and "this tool does not look at that table" -- and on a merge the second is the
    one that loses data.
    """
    lines = [f"  swaps        {len(found['swaps'])}"]
    lines.extend(
        f"                 {row['id']}  {row.get('from_asset')} -> {row.get('to_asset')}  "
        f"{row.get('status')}  created {row.get('created_at')}"
        for row in found["swaps"]
    )
    if not found["swaps"]:
        lines.append("                 (none)  <- nothing to move; the source holds no swap the destination lacks")
    lines.append(f"  quotes       {len(found['quotes'])}" + ("" if found["quotes"] else "  <- (none)"))
    for table in TABLE_ORDER:
        if table in ("quotes", "swaps"):
            continue
        rows = found["children"].get(table, [])
        lines.append(f"  {table:28} {len(rows)}" + ("" if rows else "  <- (none)"))
    if found["colliding"]:
        lines.append(
            f"  COLLIDING    {len(found['colliding'])} swap id(s) already exist in the destination and "
            f"are NOT moved: {', '.join(found['colliding'])}"
        )
        lines.append(
            "                 Ids are generated per database, so the same id in two databases is "
            "two different swaps. Overwriting would replace a real one; there is no --force."
        )
    return lines


def main(argv: list[str] | None = None) -> int:
    """Read the source, plan the move, print it, and write only with --apply."""
    parser = argparse.ArgumentParser(
        prog=SELF,
        description="Move swaps out of an orphaned swap_terminal.db into the authority.",
    )
    parser.add_argument("--source", required=True, help="the orphaned database. Opened READ-ONLY.")
    parser.add_argument("--destination", required=True, help="the authority. Written only with --apply.")
    parser.add_argument("--apply", action="store_true",
                        help="actually insert. Without it nothing is written and the checks still run.")
    args = parser.parse_args(argv)

    source, destination = Path(args.source), Path(args.destination)
    print(f"{SELF}: {'APPLY -- rows WILL be inserted' if args.apply else 'DRY RUN -- nothing is written'}", flush=True)
    print(f"  source       {source}  (read-only)", flush=True)
    print(f"  destination  {destination}", flush=True)
    print("  this tool DELETES NOTHING, anywhere, and never overwrites a row.", flush=True)

    opened = open_both(source, destination)
    if isinstance(opened, int):
        return opened
    src, dst = opened

    reasons = refuse_before_touching_anything(source, destination, src, dst)
    if reasons:
        print(f"  REFUSED      {len(reasons)} reason(s), and nothing was written:", flush=True)
        for reason in reasons:
            print(f"                 - {reason}", flush=True)
        return 1

    found = plan(src, dst)
    for line in report(found):
        print(line, flush=True)

    if not found["swaps"] and not found["quotes"] and not found["children"]:
        print("\n  NOTHING TO DO: the destination already has every swap the source holds.", flush=True)
        print("  The source file is redundant. Deleting it is YOURS to do -- this tool never does.", flush=True)
        return 0

    if not args.apply:
        print(f"\nDRY RUN: nothing written. To move exactly what is listed above:\n"
              f"    python3 {SELF} --source {source} --destination {destination} --apply", flush=True)
        return 0

    # ONE TRANSACTION. A merge that commits halfway leaves rows pointing at a swap that
    # did not arrive, and db.py's SCHEMA has foreign keys ON -- so the NEXT run would
    # report a problem this run created.
    dst.execute("BEGIN IMMEDIATE")
    try:
        written = {"quotes": insert_rows(dst, "quotes", found["quotes"]),
                   "swaps": insert_rows(dst, "swaps", found["swaps"])}
        for table in TABLE_ORDER:
            if table in ("quotes", "swaps"):
                continue
            written[table] = insert_rows(dst, table, found["children"].get(table, []))
    except Exception:
        dst.rollback()
        print("  ROLLED BACK: nothing was written and the source is untouched.", flush=True)
        raise
    dst.commit()

    total = sum(written.values())
    print(f"  WROTE        {total} row(s): "
          + ", ".join(f"{t}={n}" for t, n in written.items() if n), flush=True)
    print(f"  verify       python3 show_swap.py --swap {found['swaps'][0]['id']} --db {destination}"
          if found["swaps"] else "  verify       nothing moved", flush=True)
    print("  then         the source is redundant and deleting it is YOURS (rule 7: the record "
          "moved, it was not destroyed).", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
