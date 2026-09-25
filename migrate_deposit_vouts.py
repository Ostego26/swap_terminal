#!/usr/bin/env python3
"""Report -- and, when the operator names rows, delete -- the fabricated vout=0 deposit rows.

Role: file (the entry point; the decisions live in
      swap_terminal/deposit_vout_artifact.py and this file holds none of its
      own beyond the refusals below)
Reads: swap_terminal.db (deposit_events, swaps) -- opened `mode=ro` unless
      --apply is given -- config.Config for the default database path and
      AMOUNT_TOLERANCE_PCT, and supervisor.py's pid files under
      swap_terminal/runtime/ to report whether the workers are running
Writes: NOTHING unless --apply and --delete-event-id are BOTH given. With them:
      a backup of the database taken through sqlite3.Connection.backup(), and
      then one DELETE per named deposit_events row. It never touches the
      `swaps` table, never writes swap_audit_log, and never creates a database.
Can move funds: no. It opens no socket, reads no key, signs nothing and
      broadcasts nothing -- but the rows it can delete are the rows whose sum
      decides whether a payout is released, so a deletion here changes what a
      later poll will authorize. That is armed state and it is why --apply
      exists, why it needs row ids, and why the dry run is the default.
Mainnet-safe: yes to RUN. The default mode opens the database read-only and
      SQLite, not this file's control flow, enforces that. --apply against a
      live database is a different question and is answered under "STOP THE
      WORKERS FIRST" below.

================================================================================
DRY RUN IS THE DEFAULT, AND --apply ALONE STILL DELETES NOTHING
================================================================================

Two flags are needed, not one. `--apply` says "write", and
`--delete-event-id N` says WHICH row -- and the ids only exist in the dry run's
output, so a deletion is structurally downstream of somebody having read the
report. There is no `--all`, no `--auto`, and no heuristic that picks a row,
because the thing this tool cannot do is the thing such a flag would claim to
do. See "WHAT THIS REFUSES TO DECIDE".

Following swap_terminal/migrate_swap_intents.py, and learning from what that
script got wrong on its first version: it called executescript() before
checking the flag, so a "dry run" against a fresh path CREATED a database and
its tables. The promise is made enforceable here rather than remembered --
open_database() returns a `file:...?mode=ro` connection in dry-run mode, so a
stray write raises `attempt to write a readonly database` instead of
succeeding, and a dry run against a path that does not exist gets an in-memory
connection and leaves no file behind.

ONE HONEST QUALIFICATION, because it looks like a write and is not. Opening an
EXISTING WAL-mode database -- which db.py makes every swap_terminal.db --
creates its `-shm` and `-wal` sidecars if they are absent, even read-only:
SQLite needs the shared-memory index to read a WAL database at all. MEASURED
2026-09-25: after a dry run the `-wal` is 0 bytes, and swap_terminal.db itself
is byte-identical with an unchanged mtime (tests/test_deposit_vout_artifact.py
::test_a_dry_run_leaves_the_database_byte_identical asserts the md5 and the
mtime, not just the row count). Claiming "creates no file at all" would be the
kind of slightly-too-strong sentence rule 17 is about; the claim is that the
DATABASE is not modified, and that is the one that is tested.

================================================================================
WHAT THIS REFUSES TO DECIDE, AND WHY IT IS THE OPERATOR'S
================================================================================

The artifact and a legitimate deposit are the same shape. One transaction may
pay the same address twice, and then two rows sharing (swap_id, asset, txid)
with different vouts are two real outputs whose sum is the real amount
received. Deleting one of those would take a correctly credited swap and
under-credit it -- and unlike the artifact, THAT failure direction pays a
customer less than they sent.

What separates the two is when the row was written, not what it contains. A
fabricated row predates the deploy that fixed
chains/base._extract_matching_vouts(); a real one was written afterwards. This
script cannot know when that deploy landed on the operator's host, so it prints
`first_seen_at` and `credited_at` for every row and compares them to nothing.

It also does not move a swap's status. MEASURED 2026-09-25 against a real
seeded database (tests/test_deposit_vout_artifact.py::
test_deleting_the_row_fixes_the_sum_but_does_not_unstick_the_swap): deleting
the fabricated row corrects actual_input_amount from 3.0 back to 1.5, and the
swap STAYS in `under_review` with its stale failed_reason, because
refresh_swap_from_chain()'s recovery branch is
`elif current_status in {"confirming", "deposit_seen", "awaiting_deposit"}` and
`under_review` is not in that set, and ACTIVE_STATUSES does not contain it
either so the watcher never looks at the swap again. Returning a swap to a
payable status is arming a payout. That is fund movement (rule 16) and this
script will not do it as a side effect of tidying rows; it says so, per swap,
in the output.

Where a swap is already `completed` or `failed`, the sum decides nothing for it
any more and the script refuses to delete its rows at all. Rewriting settled
history is worse than leaving an artifact in it.

================================================================================
STOP THE WORKERS FIRST
================================================================================

This script takes no lock. A deposit_watcher polling mid-run will re-INSERT a
row this script has just deleted, because upsert_deposit_event() writes
whatever the adapter reports and the adapter reports real vouts now -- so the
delete would appear to have worked and the row would come back looking new.

It reads supervisor.py's pid files and refuses --apply while any supervised
worker is running. That detection is REAL but PARTIAL, and the limit is stated
in the output rather than implied: a worker started by hand
(`python3 workers/deposit_watcher.py` in another terminal) writes no pid file
and is invisible to it. Rule 13's own words -- "a pid file is a convention, not
a constraint". Stop the workers with `python3 swap_terminal/supervisor.py stop`
and check `status` before running --apply.

================================================================================
THE BACKUP
================================================================================

Taken before the first DELETE, with sqlite3.Connection.backup(), and --apply
refuses if it cannot be taken. Not shutil.copy2: CLAUDE.md rule 12 names that
call by name because db.py sets `PRAGMA journal_mode=WAL`, and copying a
WAL-mode database at the file level takes the main file without the write-ahead
log -- silently dropping whatever has not been checkpointed. Connection.backup()
goes through SQLite's own backup API and is correct by construction.

The backup is named `<stem>-pre-vout-migration-<UTC timestamp>.db`. The `.db`
suffix is deliberate: .gitignore already blocks `*.db`, and this repository's
rule 2 records that its backup habit is what leaked a live RPC password to
GitHub. A backup that cannot be committed by accident is the point. The path is
printed, and an existing file at that path is a refusal rather than an
overwrite.
"""

import argparse
import sqlite3
import sys
import time
from pathlib import Path

# sys.path.insert is the idiom every entry point in this tree needs, because
# the application imports its own modules rootlessly (`from config import
# Config`). CLAUDE.md rule 10 names that as the largest gap between the rule
# and the tree, and rewriting every import to close it would be a large diff
# with no behavioral benefit on a key-holding system.
sys.path.insert(0, str(Path(__file__).resolve().parent / "swap_terminal"))

from config import Config
from deposit_vout_artifact import (
    AFFECTED_SWAP_ROWS_SQL,
    EFFECT_WOULD_BREAK,
    SCAN_COUNTS_SQL,
    SETTLED_STATUSES,
    SUSPECT_VOUT,
    assess_swap,
    group_rows_by_swap,
)
from microfortnights import format_duration
from supervisor import DEFAULT_RUN_DIR, worker_commands, worker_status

# Amounts on all three chains here carry 8 decimal places. Printing fewer would
# hide a dust-sized difference between two rows, which is exactly the kind of
# difference an operator is looking at this output to judge.
AMOUNT_DECIMALS = 8


class MigrationRefused(Exception):
    """A condition that must stop the migration rather than be worked around.

    Named and raised rather than printed-and-continued so that every refusal
    leaves through one place and none of them can be mistaken for a result.
    """


def backup_path_for(db_path: Path, now: str) -> Path:
    """Where the pre-migration backup goes, unless --backup says otherwise.

    `.db` on the end on purpose -- see the module docstring on .gitignore and
    rule 2's leaked credential.
    """
    return db_path.with_name(f"{db_path.stem}-pre-vout-migration-{now}.db")


def take_backup(conn: sqlite3.Connection, destination: Path) -> int:
    """Copy the open database to `destination` through SQLite's backup API.

    Returns the size of the backup in bytes, read back off the disk rather than
    computed, so the figure printed is evidence the file is there.

    Refuses to overwrite. A backup path that already exists is either a second
    run in the same second or an operator pointing at the wrong file, and both
    are better stopped than silently resolved.
    """
    if destination.exists():
        raise MigrationRefused(
            f"backup destination already exists: {destination}  <- refusing to overwrite it. "
            f"Move it aside, or pass --backup with a different path. Nothing was written."
        )
    destination.parent.mkdir(parents=True, exist_ok=True)
    target = sqlite3.connect(destination)
    try:
        conn.backup(target)
    finally:
        target.close()
    if not destination.exists():
        raise MigrationRefused(f"backup reported success but {destination} is not there. Nothing was written.")
    return destination.stat().st_size


def backup_is_readable(destination: Path) -> int:
    """Open the backup and count its deposit_events rows.

    Verification by behavior: a file of the right size is not evidence of a
    database. Reading a row count out of it with a fresh connection is.
    """
    conn = sqlite3.connect(f"file:{destination}?mode=ro", uri=True)
    try:
        return int(conn.execute("SELECT COUNT(*) FROM deposit_events").fetchone()[0])
    finally:
        conn.close()


def open_database(db_path: Path, apply: bool) -> sqlite3.Connection:
    """Read-write for --apply, read-only otherwise, enforced by SQLite.

    sqlite3.connect() CREATES a missing file, so a dry run against a path that
    does not exist would leave a database behind -- a write performed by the
    mode whose whole purpose is not to write. The in-memory branch is what
    makes "a dry run against a nonexistent path creates no file" true by
    construction rather than by this function remembering.
    """
    if apply:
        if not db_path.exists():
            raise MigrationRefused(
                f"--apply was given but {db_path} does not exist  <- there is nothing to migrate, and this "
                f"script will not create a database. Nothing was written."
            )
        conn = sqlite3.connect(db_path)
    elif db_path.exists():
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    else:
        conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    return conn


def scan_counts(conn: sqlite3.Connection) -> dict:
    """Table sizes for the announce block, tolerating a database with no schema.

    The narrow catch is on the message as well as the type. A missing table is
    the normal state of a fresh or wrong path and means zero rows; any other
    OperationalError -- locked, corrupt, unreadable -- is re-raised, because
    reporting one of those as "0 rows" would let an operator read a failure as
    a clean database (rule 12: the caller must be able to tell the failure from
    a real answer).
    """
    try:
        row = conn.execute(SCAN_COUNTS_SQL).fetchone()
    except sqlite3.OperationalError as error:
        if "no such table" in str(error):
            return {"deposit_events": 0, "swaps": 0, "affected_groups": 0, "schema_present": False}
        raise
    return {
        "deposit_events": int(row["deposit_events"]),
        "swaps": int(row["swaps"]),
        "affected_groups": int(row["affected_groups"]),
        "schema_present": True,
    }


def affected_assessments(conn: sqlite3.Connection, tolerance_pct: float) -> list[dict]:
    """Run the detection SQL and assess each affected swap. Ordered by swap id."""
    try:
        rows = [dict(row) for row in conn.execute(AFFECTED_SWAP_ROWS_SQL).fetchall()]
    except sqlite3.OperationalError as error:
        if "no such table" in str(error):
            return []
        raise
    by_swap = group_rows_by_swap(rows)
    return [assess_swap(by_swap[swap_id], tolerance_pct) for swap_id in sorted(by_swap)]


def worker_lines(run_dir: Path) -> tuple[list[str], list[str]]:
    """Report each supervised worker's state; return (lines, names_running).

    The limit is printed with the answer rather than left to the reader to
    remember: this sees only workers supervisor.py started.
    """
    lines = []
    running = []
    for name in sorted(worker_commands()):
        state = worker_status(name, run_dir)
        detail = f"  ({state['detail']})" if state["detail"] else ""
        lines.append(f"      {name:<18} {state['state']}{detail}")
        if state["state"] != "stopped":
            running.append(name)
    return lines, running


def _amount(value) -> str:
    return "(none)" if value is None else f"{float(value):.{AMOUNT_DECIMALS}f}"


def _text(value) -> str:
    """Rule 14: `(none)` is a result; a blank gap is ambiguous between zero and broken."""
    return "(none)" if value in (None, "") else str(value)


def swap_header(assessment: dict) -> str:
    """One line naming the swap and, where it matters, why its rows are off limits."""
    header = f"  swap {assessment['swap_id']}   status={_text(assessment['status'])}"
    if assessment["orphaned"]:
        return header + "   <- NO ROW IN `swaps` for this swap_id; expected amount and threshold are unknowable"
    if assessment["settled"]:
        return header + "   <- SETTLED. The sum decides nothing for it now, and this script will not delete its rows"
    if assessment["past_the_gate"]:
        return header + "   <- payout already authorized; the deposit sum is no longer what gates it"
    return header


def describe_rows(assessment: dict) -> list[str]:
    """The rows, grouped by the transaction they came from.

    Grouped rather than listed flat because (asset, txid) is the unit the
    artifact is defined in: two rows are only suspicious when they share one
    transaction. A flat list puts the txid in a 64-character column on every
    line and leaves the reader to spot which ones match.

    A swap's rows from OTHER transactions are shown too, unmarked. They are not
    findings, but they are part of the sum the gate compares, so leaving them
    out would print a total the reader cannot reconcile with the rows above it.
    """
    lines = []
    by_group: dict[tuple[str, str], list[dict]] = {}
    for row in assessment["rows"]:
        by_group.setdefault((row["asset"], row["txid"]), []).append(row)
    affected = set(assessment["groups"])

    for (asset, txid), group in by_group.items():
        vouts = ", ".join(str(int(row["vout"])) for row in group)
        marker = "  <- AFFECTED: one transaction, more than one row" if (asset, txid) in affected else ""
        lines.append(f"      {asset} txid {txid}{marker}")
        lines.append(f"          {len(group)} row(s) at vout {vouts}")
        lines.append(
            f"          {'event_id':>8} {'vout':>5} {'amount':>18} {'confs':>6}  "
            f"{'first_seen_at':<34} credited_at"
        )
        for row in group:
            suspect = "  <- vout=0: COULD be fabricated, or a real first output" if int(row["event_id"]) in assessment["suspect_ids"] else ""
            lines.append(
                f"          {int(row['event_id']):>8} {int(row['vout']):>5} {_amount(row['amount']):>18} "
                f"{int(row['confirmations']):>6}  {_text(row['first_seen_at']):<34} "
                f"{_text(row['credited_at'])}{suspect}"
            )
        lines.append("")
    return lines


def describe_totals(assessment: dict) -> list[str]:
    """Both totals, what each one means, and whether a deletion is offered.

    The verdict beside each total says what the GATE would do with it, and for
    a swap the gate no longer looks at, it says that instead of pretending a
    comparison still happens.
    """
    if assessment["orphaned"]:
        return [
            f"      seen total (every row)          {_amount(assessment['seen_total'])}",
            "      confirmed total                 (none)  <- no swap row, so no min_confirmations to apply",
        ]

    live = not (assessment["settled"] or assessment["past_the_gate"])

    def verdict(inside: bool) -> str:
        if not live:
            return "the gate no longer runs for this swap; shown for comparison only"
        return "INSIDE tolerance" if inside else "OUTSIDE tolerance -> under_review"

    lines = [
        f"      confirmed total, every row      {_amount(assessment['confirmed_total'])}"
        f"   <- {verdict(assessment['inside_with_suspects'])}",
        f"      confirmed total without vout=0  {_amount(assessment['confirmed_total_without_suspects'])}"
        f"   <- {verdict(assessment['inside_without_suspects'])}",
        f"      seen total, every row           {_amount(assessment['seen_total'])}"
        f"   /  without vout=0  {_amount(assessment['seen_total_without_suspects'])}",
    ]

    # One source for what happens to this swap's rows: suspect_effect(). A
    # second `if assessment["settled"]` branch here would be a copy of a rule
    # that already exists one module over, and the two would drift (rule 8).
    effect = assessment["effect"]
    if effect["offered"]:
        ids = " ".join(f"--delete-event-id {event_id}" for event_id in assessment["suspect_ids"])
        lines.append(f"      OFFERED: --apply {ids}")
        lines.append(f"          {effect['note']}")
    elif effect["note"]:
        lines.extend(f"      {note_line}" for note_line in effect["note"].splitlines())
    return lines


def describe_swap(assessment: dict) -> list[str]:
    """Render one affected swap: every row, both totals, and what is undecidable.

    Every figure the tolerance gate uses is printed beside what it means,
    because the operator reads the screen and not deposit_service.py.
    """
    lines = [swap_header(assessment)]
    if not assessment["orphaned"]:
        lines.append(
            f"      expected_input_amount  {_amount(assessment['expected'])}"
            f"   min_confirmations={assessment['min_confirmations']} blocks"
        )
        lines.append(
            f"      tolerance window       [{_amount(assessment['tolerance_low'])}, "
            f"{_amount(assessment['tolerance_high'])}]   <- a CONFIRMED total outside this sets under_review"
        )
        lines.append(f"      actual_input_amount    {_amount(assessment['actual_input_amount'])}   (as recorded on the swap)")
        lines.append(f"      failed_reason          {_text(assessment['failed_reason'])}")
    lines.append("")
    lines.extend(describe_rows(assessment))
    lines.extend(describe_totals(assessment))
    return lines


def report(assessments: list[dict], counts: dict) -> list[str]:
    """The findings block. An empty one says so rather than printing a gap."""
    lines = []
    lines.append(f"affected swaps  {len(assessments)}  <- swaps with 2+ deposit_events rows sharing one (asset, txid)")
    if not assessments:
        if not counts["schema_present"]:
            lines.append("(none)  <- the database has no deposit_events table; nothing has ever been written to it")
        elif counts["deposit_events"] == 0:
            lines.append("(none)  <- deposit_events is empty; there are no rows of any kind to be affected")
        else:
            lines.append(
                f"(none)  <- all {counts['deposit_events']} deposit_events rows are the only row for their "
                f"(swap_id, asset, txid). No swap is double-counted. Nothing to do."
            )
        return lines
    lines.append("")
    for assessment in assessments:
        lines.extend(describe_swap(assessment))
        lines.append("")
    return lines


def offered_assessments(assessments: list[dict]) -> list[dict]:
    """The swaps whose vout=0 row this script is willing to suggest deleting.

    One place, so the count in the closing line, the copy-pasteable command and
    the per-swap OFFERED lines cannot disagree about what was offered -- which
    is rule 8's failure in miniature and would be read as the script having
    changed its mind between two lines of one screen.
    """
    return [a for a in assessments if a["effect"]["offered"]]


def undecidable_notice(assessments: list[dict]) -> list[str]:
    """What the operator has to settle, stated as a refusal rather than omitted."""
    if not assessments:
        return []
    actionable = offered_assessments(assessments)
    withheld = [a for a in assessments if a["effect"]["effect"] == EFFECT_WOULD_BREAK]
    lines = ["WHAT THIS SCRIPT WILL NOT DECIDE, AND WHY IT IS YOURS:"]
    lines.append(
        "  1. Whether a vout=0 row is FABRICATED or a real first output. The two are the same shape. What tells"
    )
    lines.append(
        "     them apart is WHEN the row was written -- a fabricated one predates the deploy that fixed"
    )
    lines.append(
        "     chains/base._extract_matching_vouts(), a real one does not -- and this script cannot know that date."
    )
    lines.append("     `first_seen_at` is printed above and is compared to nothing here.")
    lines.append(
        "  2. Whether to return a halted swap to a payable status. Deleting the row corrects the sum; it does NOT"
    )
    lines.append(
        "     move the swap out of `under_review`, and the deposit watcher never looks at an under_review swap"
    )
    lines.append(
        "     again (ACTIVE_STATUSES). Re-arming a payout is fund movement and is a separate decision, taken"
    )
    lines.append("     with the chain in front of you.")
    lines.append(
        f"  3. Anything at all about a {' or '.join(SETTLED_STATUSES)} swap. Those rows are refused, not offered."
    )
    if withheld:
        lines.append("")
        lines.append(
            f"  {len(withheld)} swap(s) carry the shape and are NOT offered, because removing the vout=0 row would"
        )
        lines.append(
            "  take a total that is currently INSIDE tolerance OUTSIDE it -- the shape of one transaction paying"
        )
        lines.append(
            f"  the address twice, not of the artifact: {', '.join(a['swap_id'] for a in withheld)}."
        )
    if actionable:
        ids = " ".join(f"--delete-event-id {i}" for a in actionable for i in a["suspect_ids"])
        lines.append("")
        lines.append("  If -- and only if -- you have established those rows are fabricated:")
        lines.append(f"      python3 migrate_deposit_vouts.py --apply {ids}")
    return lines


def refusal_for(event_id: int, found: tuple[dict, dict] | None) -> str | None:
    """Why one requested event id may NOT be deleted, or None if it may be.

    The decision, with no I/O and no accumulator, so it is callable with seeded
    inputs (rule 10) -- and extracting it is also what took deletion_plan()
    back under the complexity ceiling, which is the fix rule 12 asks for rather
    than raising the ceiling.

    Three refusals, and each exists because the alternative destroys something:

      not in the affected set   the id names a row this run did not report, so
                                nobody has seen it. It may well be a typo for a
                                row that IS a swap's only deposit record.
      vout is not 0             the fabricated branch could only ever write
                                vout=0, so a row at any other index was written
                                by code that read a real output.
      the swap is settled       rewriting the history of a completed or failed
                                swap changes the record of what happened, and
                                the sum no longer decides anything for it.

    There is no fourth refusal for "these ids together would empty a whole
    transaction's group". That case cannot arise, and deletion_plan()'s
    docstring carries the three invariants that establish it rather than a
    branch that would never run.
    """
    if found is None:
        return (
            f"  {event_id}: not one of the rows reported above  <- only a row this run found and printed "
            f"may be deleted"
        )
    row, assessment = found
    if int(row["vout"]) != SUSPECT_VOUT:
        return (
            f"  {event_id}: vout={int(row['vout'])}, not {SUSPECT_VOUT}  <- the fabricated branch could only "
            f"ever write vout={SUSPECT_VOUT}; this row was written by code that read a real output"
        )
    if assessment["settled"]:
        return (
            f"  {event_id}: swap {assessment['swap_id']} is {assessment['status']}  <- settled; the sum no "
            f"longer decides anything for it and its history is not being rewritten"
        )
    return None


def deletion_plan(assessments: list[dict], requested_ids: list[int]) -> list[dict]:
    """Match the requested event ids against the rows found, refusing everything else.

    Returns the rows to delete. Raises MigrationRefused naming every id that
    fails, all of them at once -- an operator fixing one typo at a time is an
    operator running --apply four times against a live database.

    THE ONE REFUSAL THAT IS NOT HERE, AND WHY IT IS NOT NEEDED.

    The obvious fourth guard is "refuse a set of ids that would delete EVERY
    row of one transaction", because that erases the swap's only record that
    the deposit arrived. It was written, and then removed as unreachable rather
    than kept as reassurance -- a guard that cannot fire is not protection, it
    is a reader's false confidence (rule 9). Three invariants make it so, and
    they were established rather than assumed:

      1. db.py's deposit_events carries UNIQUE(asset, txid, vout), present
         since the initial import (43661f4) and verified BEHAVIORALLY rather
         than by reading the schema -- a second vout=0 row for one (asset,
         txid) raises IntegrityError. tests/test_deposit_vout_artifact.py::
         test_one_vout_zero_row_per_transaction_is_a_database_constraint
         asserts that, because this argument rests on it.
      2. multi_vout_groups() only returns groups with TWO OR MORE distinct
         vouts, so every group here has at least two rows.
      3. refusal_for() accepts only rows at vout=0.

    Together: at most one row per group is ever accepted, out of at least two,
    so at least one always survives. The invariant is pinned by
    test_a_plan_always_leaves_a_row_for_every_affected_transaction rather than
    by a branch, which is rule 2's "its test changes to pin the stronger
    invariant" instead of the code being kept for reference.
    """
    by_id = {int(row["event_id"]): (row, assessment) for assessment in assessments for row in assessment["rows"]}
    refusals = []
    planned = []
    for event_id in requested_ids:
        refusal = refusal_for(event_id, by_id.get(event_id))
        if refusal is None:
            planned.append(by_id[event_id][0])
        else:
            refusals.append(refusal)
    if refusals:
        raise MigrationRefused("these ids were refused and NOTHING was written:\n" + "\n".join(sorted(set(refusals))))
    return planned


def unoffered_warnings(assessments: list[dict], planned: list[dict]) -> list[str]:
    """Say, at apply time, when a named row is one this script did not offer.

    The task this whole script exists for has one way to go badly wrong: a
    legitimate two-output deposit gets deleted and a customer is under-credited.
    suspect_effect() keeps it from being SUGGESTED; this keeps it from being
    done SILENTLY. An operator who has reasons the rows do not carry may still
    name it -- but the run that does so prints why it was withheld, in the same
    pasted block as the deletion, where it is read afterwards as well as before.
    """
    planned_ids = {int(row["event_id"]) for row in planned}
    lines = []
    for assessment in assessments:
        if assessment["effect"]["offered"]:
            continue
        named = sorted(planned_ids.intersection(assessment["suspect_ids"]))
        if not named:
            continue
        lines.append("")
        lines.append(f"WARNING: event_id {', '.join(str(i) for i in named)} was NAMED but was NOT offered.")
        lines.append(f"  swap {assessment['swap_id']} (status={_text(assessment['status'])})")
        lines.extend(f"  {note_line}" for note_line in assessment["effect"]["note"].splitlines())
        if assessment["effect"]["effect"] == EFFECT_WOULD_BREAK:
            lines.append(
                "  Deleting it anyway is your call and it is being carried out. The backup below is how it is undone."
            )
    return lines


def delete_rows(conn: sqlite3.Connection, planned: list[dict]) -> int:
    """Delete exactly the planned rows, by primary key, in one transaction.

    `with conn:` is the transaction: every named row goes or none do. A
    per-row commit would leave a half-resolved swap on any failure, and half a
    correction is a total the operator has no reason to trust.

    Deleting by `id` and not by (swap_id, asset, txid, vout) is the point: the
    primary key names one row and cannot widen. A predicate could match a row
    that arrived between the report and the delete.
    """
    with conn:
        for row in planned:
            conn.execute("DELETE FROM deposit_events WHERE id = ?", (int(row["event_id"]),))
    return len(planned)


def announce(db_path: Path, apply: bool, tolerance_pct: float, run_dir: Path) -> tuple[list[str], list[str]]:
    """Everything that decides the answer, printed before any finding (rule 14).

    Returns (lines_to_print, names_of_running_workers). The second half is the
    refusal --apply checks; it is returned rather than re-derived so that what
    the operator READ and what the script ACTED ON are the same reading.
    """
    lines = []
    lines.append("deposit vout artifact -- report, and migration behind --apply")
    lines.append(f"  mode        {'APPLY -- will delete the named rows' if apply else 'DRY RUN -- writes nothing'}")
    lines.append(f"  database    {db_path}  {'(read-write)' if apply else '(opened mode=ro; SQLite enforces it)'}")
    lines.append(
        f"  tolerance   AMOUNT_TOLERANCE_PCT={tolerance_pct}  <- a confirmed total further than this "
        f"fraction from expected_input_amount sets under_review"
    )
    lines.append("  workers:")
    worker_state_lines, running = worker_lines(run_dir)
    lines.extend(worker_state_lines)
    lines.append(f"      read from {run_dir}")
    lines.append(
        "      PARTIAL CHECK: a worker started by hand writes no pid file and is invisible here. Stop them with"
    )
    lines.append("      `python3 swap_terminal/supervisor.py stop` before --apply, and confirm with `status`.")
    return lines, running


def migrate(db_path: Path, apply: bool, delete_ids: list[int], backup: Path | None, run_dir: Path) -> int:
    started = time.monotonic()
    tolerance_pct = float(Config.AMOUNT_TOLERANCE_PCT)

    lines, running = announce(db_path, apply, tolerance_pct, run_dir)
    for line in lines:
        print(line)
    print()

    if apply and running:
        raise MigrationRefused(
            f"these supervised workers are running: {', '.join(running)}  <- a deposit_watcher poll will "
            f"re-INSERT a row this deletes, so the delete would look like it worked and the row would come "
            f"back. Stop them first. Nothing was written."
        )

    # A path that is not there is refused, in BOTH modes.
    #
    # --apply already refused it, inside open_database(). A DRY RUN did not: it
    # fell back to an in-memory connection -- correct in that it created no
    # file -- and then printed the ordinary findings block, ending in
    #
    #     scanned     deposit_events 0 rows across swaps 0 rows  (schema present: no)
    #     affected swaps  0
    #     (none)  <- the database has no deposit_events table
    #     DRY RUN -- nothing was written ...          exit code 0
    #
    # Measured 2026-09-25: the operator ran this against the literal
    # placeholder `/path/to/swap_terminal.db` out of a pasted command, and got
    # exactly that -- a clean bill of health for a file that does not exist.
    # Nothing in the output said the path was wrong, and the exit code agreed
    # with it.
    #
    # That is CLAUDE.md rule 14's own instruction, failed by the tool written
    # to serve it: "Make 'did nothing' look different from 'did work.'" A scan
    # that examined nothing must not read like a scan that found nothing,
    # because the two lead an operator to opposite conclusions about whether
    # their database carries the artifact. The in-memory fallback in
    # open_database() stays -- it is what makes "a dry run creates no file"
    # true by construction rather than by remembering -- but it is no longer
    # reachable with a report attached to it.
    if not db_path.exists():
        raise MigrationRefused(
            f"{db_path} does not exist  <- nothing was scanned, and this is NOT a clean result. "
            f"No report was printed, because a scan of nothing reads exactly like a scan that found "
            f"nothing, and this script will not create a database to scan. Pass the real path with "
            f"--db: it is SWAP_DB_PATH, or swap_terminal/swap_terminal.db when that is unset "
            f"(config.py:31)."
        )

    conn = open_database(db_path, apply)
    try:
        counts = scan_counts(conn)
        print(
            f"scanned     deposit_events {counts['deposit_events']} rows across swaps {counts['swaps']} rows"
            f"   (schema present: {'yes' if counts['schema_present'] else 'no'})"
        )
        print(
            f"            (asset, txid) groups with more than one vout: {counts['affected_groups']}"
            f"  <- expected 0 on a database that never ran against a Core 22+ node"
        )
        print()

        assessments = affected_assessments(conn, tolerance_pct)
        for line in report(assessments, counts):
            print(line)

        if not apply:
            for line in undecidable_notice(assessments):
                print(line)
            print()
            offered = sum(len(a["suspect_ids"]) for a in offered_assessments(assessments))
            print(
                f"DRY RUN -- nothing was written, and no file was created. "
                f"{offered} row(s) offered for deletion out of {len(assessments)} affected swap(s)."
            )
            print(f"done in {format_duration(time.monotonic() - started)}")
            return 0

        if not delete_ids:
            raise MigrationRefused(
                "--apply was given with no --delete-event-id  <- this script never chooses a row for you. "
                "Read the table above and name the rows. Nothing was written."
            )

        planned = deletion_plan(assessments, delete_ids)
        for line in unoffered_warnings(assessments, planned):
            print(line)

        destination = backup or backup_path_for(db_path, time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()))
        size = take_backup(conn, destination)
        readable_rows = backup_is_readable(destination)
        print(f"backup      {destination}")
        print(f"            {size} bytes, opens as a database, holds {readable_rows} deposit_events rows")
        print("            taken with sqlite3.Connection.backup(), NOT a file copy: db.py sets journal_mode=WAL")
        print()

        deleted = delete_rows(conn, planned)

        # Verified by reading the rows back out in fresh statements. A count
        # taken from the DELETE is not independent evidence of anything.
        remaining = int(conn.execute("SELECT COUNT(*) FROM deposit_events").fetchone()[0])
        still_there = [
            int(row["event_id"]) for row in planned
            if conn.execute("SELECT 1 FROM deposit_events WHERE id = ?", (int(row["event_id"]),)).fetchone()
        ]
        if still_there:
            raise MigrationRefused(f"DELETE reported success but these ids are still present: {still_there}")
        print(f"APPLIED. {deleted} row(s) deleted; deposit_events now holds {remaining} rows in total.")
        print(f"            deleted event_ids: {', '.join(str(int(row['event_id'])) for row in planned)}")
        print("The `swaps` table was NOT touched. A swap sitting in `under_review` is STILL in `under_review`,")
        print("and the deposit watcher does not poll that status -- moving it is a separate, fund-moving decision.")
        print(f"done in {format_duration(time.monotonic() - started)}")
        return 0
    finally:
        conn.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Report the fabricated vout=0 deposit_events rows, and delete the ones you name.",
    )
    parser.add_argument("--db", type=Path, default=Path(Config.DB_PATH), help="path to swap_terminal.db")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="actually delete. Requires at least one --delete-event-id; there is no flag that picks rows for you.",
    )
    parser.add_argument(
        "--delete-event-id",
        type=int,
        action="append",
        default=[],
        dest="delete_ids",
        metavar="ID",
        help="a deposit_events.id from the table above. Repeatable. Only a vout=0 row in a reported group, "
        "belonging to a swap that is not completed or failed, is accepted.",
    )
    parser.add_argument(
        "--backup",
        type=Path,
        default=None,
        help="where to put the pre-migration backup. Defaults to <db stem>-pre-vout-migration-<UTC>.db beside "
        "the database. Never overwritten.",
    )
    parser.add_argument(
        "--run-dir",
        type=Path,
        default=DEFAULT_RUN_DIR,
        help="supervisor.py's pid file directory, read to check whether the workers are running.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return migrate(args.db, args.apply, args.delete_ids, args.backup, args.run_dir)
    except MigrationRefused as refusal:
        # Refusals print as refusals, not as tracebacks. The operator needs the
        # sentence; a traceback buries it under a stack they cannot act on.
        print(f"REFUSED: {refusal}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
