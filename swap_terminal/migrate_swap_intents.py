"""Move the Express bridge's swap intents out of a JSON file and into swap_terminal.db.

Role: file (entry point -- `python3 swap_terminal/migrate_swap_intents.py`)
Reads: grc-sol-swap/abstergo_exchange/swap_intents.json (or --intents),
       swap_terminal.db (or --db), the environment variable SWAP_DB_PATH
Writes: NOTHING unless --apply is given. With --apply: the swap_intents,
       swap_intent_deposits and swap_intent_payouts tables in swap_terminal.db.
       It never writes to, moves, or truncates the JSON file, in either mode.
Can move funds: no. It opens no socket, reads no keypair, signs nothing and
       broadcasts nothing. It cannot cause a payout -- but see the warning
       below about what it does to armed state.
Mainnet-safe: yes to run. It is read-only by default and prints what it would
       do. --apply against a live database while the bridge is running is a
       different question and is answered under "WHEN TO RUN THIS" below.

================================================================================
DRY RUN IS THE DEFAULT. --apply IS THE ONLY WAY TO WRITE.
================================================================================

That is not politeness. swap_intents.json holds ARMED STATE -- measured
2026-09-24: 3 intents, one of them status=paid with a recorded payout of
5,560,821 lamports and a Solana signature. CLAUDE.md rule 16 puts armed state
with the operator, and the chain-safety rules put anything irreversible there
too. So this script's job is to show the operator exactly what it would move,
in a block they can read off the screen (rule 14), and then stop.

WHY THE MIGRATION IS WANTED AT ALL.

The Express server reads swap_intents.json to decide whether a payout is
authorized. Rule 5: "If a reader has to open a file to learn whether a payout
is authorized, the authority is in the wrong place." Rule 15: swap_terminal.db
is the only place a decision may be made from, and the Express server has no
connection to it, "so there are currently two systems of record for swaps, and
nothing reconciles them."

Moving the rows is half the fix. The other half is the claim: once the intents
are rows, the payout guard becomes a conditional UPDATE checked on rowcount,
backed by a partial unique index that makes a second live payout per intent
impossible to insert. Both are in swap_intents_schema.py with the reasoning.
The JSON store now has an in-process mutex and a lock file doing the same job
(intent_store.js), and that is strictly weaker: an advisory lock serializes
processes that agree to use it, where a constraint survives the case where the
code was wrong. Rule 13's last bullet, exactly.

WHAT THIS SCRIPT DOES NOT DO, ON PURPOSE.

  - It does not repoint the server. server.js still reads the JSON file after
    this runs with --apply, and both copies then exist. That is the operator's
    switch, and it should be thrown only after they have seen this dry run --
    which is why wiring the server to SQLite was deliberately left out of the
    same change rather than shipped as one step.
  - It does not delete or rewrite swap_intents.json. Rule 2 says git history is
    the archive, but a live store holding a paid intent is not dead code, and
    a migration that removes its own source before anyone has verified the
    destination has nowhere to fall back to.
  - It does not resolve conflicts. An intent_id already present in the database
    is REPORTED and skipped, never overwritten. If the database's copy and the
    file's copy disagree about a status, that disagreement is the finding, and
    silently picking a winner would destroy it.

WHEN TO RUN --apply. With the bridge stopped, or at least with no payout in
flight. The script takes no lock on swap_intents.json: adding one would mean
this script and the Node lock format have to agree forever, which is rule 8's
duplication in a place where a disagreement is silent. Instead it reads the
file once, reports the mtime it read, and re-reads it before writing to check
it has not changed -- and refuses if it has. That detects a concurrent write
rather than preventing one, which is the honest guarantee and is stated rather
than implied.
"""

import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path

# sys.path.insert is the idiom every entry point in this tree needs, because the
# application imports its own modules rootlessly (`from config import Config`).
# CLAUDE.md rule 10 names that as the largest gap between the rule and the tree,
# and rewriting every import to close it would be a large diff with no
# behavioral benefit.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from config import Config
from microfortnights import format_duration
from swap_intents_schema import SCHEMA

DEFAULT_INTENTS_PATH = Path(__file__).resolve().parent / "grc-sol-swap" / "abstergo_exchange" / "swap_intents.json"

# The statuses the JSON store can hold, mapped to the ones the schema's CHECK
# constraint allows. They are identical today and the map exists anyway,
# because a migration that assumes two vocabularies agree is a migration that
# writes a row the constraint rejects halfway through -- and finds out after it
# has already written the earlier ones.
#
# 'payout_failed' has no rows in the live file (measured: 3 intents, one paid,
# two awaiting_deposit) and is listed because intent_store.js can now produce
# it. An UNKNOWN status is a hard stop, not a default: quietly mapping it to
# 'awaiting_deposit' would take a paid intent and make it payable again.
STATUS_MAP = {
    "awaiting_deposit": "awaiting_deposit",
    "verified": "verified",
    "paying": "paying",
    "paid": "paid",
    "payout_failed": "payout_failed",
    "expired": "expired",
}


class MigrationRefused(Exception):
    """A condition that must stop the migration rather than be worked around."""


def load_intents(intents_path: Path) -> tuple[list[dict], float]:
    """Read the JSON store, returning its intents and the mtime they were read at.

    No broad catch. A store that will not parse is the single most important
    thing this script could discover, and reporting it as "no intents to
    migrate" -- which is what `except Exception: return []` would do, and what
    the Express server's loadIntentStore did until 2026-09-24 -- would let an
    operator conclude the file was empty and delete it.
    """
    mtime = intents_path.stat().st_mtime
    parsed = json.loads(intents_path.read_text(encoding="utf-8"))
    if not isinstance(parsed, dict) or not isinstance(parsed.get("intents"), list):
        raise MigrationRefused(
            f"{intents_path} parsed but has no 'intents' array (top level is {type(parsed).__name__}). "
            f"Refusing to treat this as an empty store."
        )
    return parsed["intents"], mtime


def intent_rows(intent: dict) -> tuple[dict, dict | None, dict | None]:
    """Flatten one JSON intent into the three rows it becomes.

    This is the decision layer of this script (rule 10: the function at the
    bottom is the thing that decides, and it is callable with seeded inputs --
    tests/test_swap_intent_migration.py does exactly that). Everything above it
    is orchestration.

    Returns (intent_row, deposit_row_or_None, payout_row_or_None).
    """
    intent_id = intent.get("intentId")
    if not intent_id:
        raise MigrationRefused(f"an intent has no intentId: {sorted(intent)}")

    raw_status = intent.get("status")
    if raw_status not in STATUS_MAP:
        raise MigrationRefused(
            f"intent {intent_id} has status {raw_status!r}, which is not one this schema knows. "
            f"Refusing to guess -- mapping an unknown status to a payable one would re-arm a payout."
        )

    quote = intent.get("expectedQuote") or {}
    lamports = quote.get("lamports")
    if not isinstance(lamports, int) or lamports <= 0:
        raise MigrationRefused(
            f"intent {intent_id} has quoted lamports {lamports!r}; the schema requires a positive integer, "
            f"because that figure is what a payout would send."
        )

    intent_row = {
        "intent_id": intent_id,
        "status": STATUS_MAP[raw_status],
        "created_at": intent.get("createdAt"),
        "expires_at": intent.get("expiresAt"),
        "updated_at": intent.get("createdAt"),
        "gridcoin_deposit_address": intent.get("gridcoinDepositAddress"),
        "destination_solana_address": intent.get("destinationSolanaAddress"),
        "expected_grc_amount": float(intent.get("expectedGrcAmount", 0.0)),
        "quoted_lamports": lamports,
        "quoted_sol_amount": float(quote.get("solAmount", 0.0)),
        "quoted_grc_price_usd": quote.get("grcPriceUsd"),
        "quoted_sol_price_usd": quote.get("solPriceUsd"),
    }

    deposit = intent.get("verifiedDeposit")
    deposit_row = None
    if deposit:
        deposit_row = {
            "intent_id": intent_id,
            "gridcoin_txid": deposit.get("gridcoinTxid"),
            "confirmations": int(deposit.get("confirmations", 0)),
            "received_grc_amount": float(deposit.get("receivedGrcAmount", 0.0)),
            "source_gridcoin_address": deposit.get("sourceGridcoinAddress"),
            "verification_source": deposit.get("verificationSource"),
            "verified_at": deposit.get("verifiedAt"),
        }

    payout = intent.get("payout")
    payout_row = None
    if payout and payout.get("signature"):
        # Only a payout with a SIGNATURE becomes a row. A payout dict holding
        # just a startedAt is an attempt that never reported an outcome, and
        # inserting it as 'claimed' would trip the partial unique index and
        # block the operator from ever retrying that intent. An attempt with no
        # outcome is reported in the dry run instead, where a human can look at
        # the chain.
        payout_row = {
            "intent_id": intent_id,
            "claim_token": payout.get("claimToken") or f"imported_{intent_id}",
            "status": "completed",
            "destination_solana_address": intent.get("destinationSolanaAddress"),
            "lamports": int(payout.get("lamports") or lamports),
            "signature": payout.get("signature"),
            "error": payout.get("error"),
            "claimed_at": payout.get("startedAt") or payout.get("paidAt"),
            "paid_at": payout.get("paidAt"),
            "failed_at": payout.get("failedAt"),
        }

    return intent_row, deposit_row, payout_row


def existing_intent_ids(conn: sqlite3.Connection) -> set[str]:
    """Which intent_ids the database already holds.

    Tolerates the table being absent, and ONLY that. A dry run must not create
    the schema -- the first version of this script did, via executescript, and
    that made "writes nothing" false in the exact mode whose whole purpose is
    to write nothing. A missing table in a read-only connection is the normal
    state before the first --apply, and it means zero existing intents.

    The catch is narrow on both the exception type and the message. Any other
    OperationalError -- a locked database, a corrupt page, a permissions
    problem -- is re-raised, because reporting one of those as "no rows yet"
    would let an operator read a failure as a clean slate (rule 12: the caller
    must be able to tell the failure from a real answer).
    """
    try:
        cursor = conn.execute("SELECT intent_id FROM swap_intents")
    except sqlite3.OperationalError as error:
        if "no such table" in str(error):
            return set()
        raise
    return {row[0] for row in cursor.fetchall()}


def open_database(db_path: Path, apply: bool) -> sqlite3.Connection:
    """Open the database read-only for a dry run, read-write for --apply.

    The read-only URI is not decoration. SQLite's connect() CREATES the file if
    it is absent and executescript() would then create the tables, so a dry run
    against a fresh path used to leave a database behind -- a write, performed
    by the mode that promises not to write. mode=ro makes the promise
    enforceable by the library rather than by this function remembering.
    """
    if apply:
        return sqlite3.connect(db_path)
    if not db_path.exists():
        # A dry run against a database that does not exist yet is a legitimate
        # and common case: it is what the operator runs before anything has
        # ever been migrated. An in-memory connection gives the rest of the
        # function a real cursor to work with and cannot touch the disk.
        return sqlite3.connect(":memory:")
    return sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)


# First six and last four characters is enough to match a dry-run row against
# the file by eye, and short enough that the line does not wrap.
_REDACT_KEEP_HEAD = 6
_REDACT_KEEP_TAIL = 4


def _redact(address: str | None) -> str:
    """Render an address so a pasted dry run identifies the row without republishing it.

    A Solana or Gridcoin address is public by construction, so this is not a
    secret rule -- it is a pasting rule. Dry-run output routinely ends up in a
    chat window or an issue, and a full destination address there is one
    copy-paste away from being mistaken for an instruction. First six and last
    four is enough to match a row against the file by eye.
    """
    if not address:
        return "(none)"
    if len(address) <= _REDACT_KEEP_HEAD + _REDACT_KEEP_TAIL + 2:
        # Short enough that abbreviating it would remove nothing and make the
        # row harder to match against the file.
        return address
    return f"{address[:_REDACT_KEEP_HEAD]}...{address[-_REDACT_KEEP_TAIL:]}"


def describe(rows: list[tuple[dict, dict | None, dict | None]], skipped: list[str]) -> list[str]:
    """Render the plan. Rule 14: state what each number means, next to the number."""
    lines = []
    lines.append(f"intents to insert   {len(rows)}")
    lines.append(f"intents skipped     {len(skipped)}  <- already present in the database; never overwritten")
    if not rows and not skipped:
        # Rule 14 again: an empty result is a result. A blank gap here is
        # ambiguous between "nothing to migrate" and "the reader broke".
        lines.append("(none)  <- the store parsed and contained zero intents")
        return lines

    lines.append("")
    lines.append(f"  {'intent_id':<28} {'status':<17} {'lamports':>12}  {'destination':<14} deposit payout")
    for intent_row, deposit_row, payout_row in rows:
        lines.append(
            f"  {intent_row['intent_id']:<28} {intent_row['status']:<17} {intent_row['quoted_lamports']:>12}"
            f"  {_redact(intent_row['destination_solana_address']):<14}"
            f" {'yes' if deposit_row else 'no ':<7}"
            f" {'yes' if payout_row else 'no'}"
        )
    lines.extend(f"  {intent_id:<28} SKIPPED -- intent_id already in swap_intents" for intent_id in skipped)
    return lines


def build_plan(intents: list[dict], already: set[str]) -> tuple[list[tuple], list[str], list[str]]:
    """Decide what would be migrated. No I/O, so it is callable with seeded inputs.

    Returns (planned, skipped, attempts_without_outcome). This is the decision
    layer (rule 10); everything that prints or writes is orchestration around
    it. Extracting it is also what took migrate() back under the complexity
    ceiling -- rule 12: "a main() past the ceiling is orchestration that has
    swallowed decisions, and the fix is to extract the decision, not to raise
    the ceiling."
    """
    planned: list[tuple] = []
    skipped: list[str] = []
    attempts_without_outcome: list[str] = []

    for intent in intents:
        rows = intent_rows(intent)
        intent_id = rows[0]["intent_id"]
        payout = intent.get("payout")
        if payout and not payout.get("signature"):
            attempts_without_outcome.append(intent_id)
        if intent_id in already:
            skipped.append(intent_id)
            continue
        planned.append(rows)

    return planned, skipped, attempts_without_outcome


def insert_plan(conn: sqlite3.Connection, planned: list[tuple], intents_path: Path, imported_at: str) -> None:
    """Write the planned rows. One transaction for the whole plan.

    `with conn:` is the transaction: either every intent, deposit and payout
    row lands or none of them do. A per-intent commit would leave a half-moved
    store on any failure, which is rule 5's "a stage that writes its rows as it
    goes" hazard inverted -- here the partial result is the dangerous one,
    because an operator would not know which half to trust.
    """
    with conn:
        for intent_row, deposit_row, payout_row in planned:
            conn.execute(
                """
                INSERT INTO swap_intents (
                    intent_id, status, created_at, expires_at, updated_at,
                    gridcoin_deposit_address, destination_solana_address, expected_grc_amount,
                    quoted_lamports, quoted_sol_amount, quoted_grc_price_usd, quoted_sol_price_usd,
                    imported_from, imported_at
                ) VALUES (
                    :intent_id, :status, :created_at, :expires_at, :updated_at,
                    :gridcoin_deposit_address, :destination_solana_address, :expected_grc_amount,
                    :quoted_lamports, :quoted_sol_amount, :quoted_grc_price_usd, :quoted_sol_price_usd,
                    :imported_from, :imported_at
                )
                """,
                {**intent_row, "imported_from": str(intents_path), "imported_at": imported_at},
            )
            if deposit_row:
                conn.execute(
                    """
                    INSERT INTO swap_intent_deposits (
                        intent_id, gridcoin_txid, confirmations, received_grc_amount,
                        source_gridcoin_address, verification_source, verified_at
                    ) VALUES (
                        :intent_id, :gridcoin_txid, :confirmations, :received_grc_amount,
                        :source_gridcoin_address, :verification_source, :verified_at
                    )
                    """,
                    deposit_row,
                )
            if payout_row:
                conn.execute(
                    """
                    INSERT INTO swap_intent_payouts (
                        intent_id, claim_token, status, destination_solana_address, lamports,
                        signature, error, claimed_at, paid_at, failed_at
                    ) VALUES (
                        :intent_id, :claim_token, :status, :destination_solana_address, :lamports,
                        :signature, :error, :claimed_at, :paid_at, :failed_at
                    )
                    """,
                    payout_row,
                )


def report_attempts_without_outcome(attempts: list[str]) -> None:
    """Name the intents whose payout attempt has no recorded outcome.

    These are the rows a human has to settle against the chain, and they are
    reported separately rather than mixed into the table because they are the
    one thing in a dry run that needs an action taken outside this script.
    """
    if not attempts:
        return
    print()
    print(
        f"payout attempts with no signature   {len(attempts)}"
        f"  <- NOT migrated as payout rows; an attempt with no recorded outcome has to be"
    )
    print("      settled against the chain by a human before anything claims it can be retried:")
    for intent_id in attempts:
        print(f"      {intent_id}")


def migrate(intents_path: Path, db_path: Path, apply: bool) -> int:
    started = time.monotonic()

    # Rule 14: announce the target and the scale BEFORE doing anything. A line
    # that only appears on completion is invisible during the wait, which is
    # exactly when it is needed -- and a pasted block has to say which database
    # and which file it was about, because it is usually read a day later.
    print("swap intent migration")
    print(f"  mode       {'APPLY -- will write' if apply else 'DRY RUN -- writes nothing'}")
    print(f"  source     {intents_path}")
    print(f"  database   {db_path}")
    print()

    if not intents_path.exists():
        print(f"source does not exist: {intents_path}  <- nothing to migrate, and nothing was written")
        return 1

    intents, mtime_at_read = load_intents(intents_path)
    read_at = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(mtime_at_read))
    print(f"read {len(intents)} intents from the store (mtime {read_at})")

    conn = open_database(db_path, apply)
    try:
        if apply:
            conn.executescript(SCHEMA)
            conn.commit()

        planned, skipped, attempts = build_plan(intents, existing_intent_ids(conn))

        if not apply:
            has_schema = bool(
                conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'swap_intents'").fetchone()
            )
            created = "already created by an earlier run" if has_schema else "they would be created by --apply; nothing was created now"
            print(f"target tables present  {'yes' if has_schema else 'no'}  <- {created}")

        for line in describe(planned, skipped):
            print(line)
        report_attempts_without_outcome(attempts)

        if not apply:
            print()
            print(f"DRY RUN -- nothing was written. {len(planned)} intents would be inserted.")
            print("Re-run with --apply to write them, with the bridge stopped.")
            print(f"done in {format_duration(time.monotonic() - started)}")
            return 0

        # Re-read and compare the mtime. This DETECTS a concurrent write; it
        # does not prevent one. Stated as it is rather than dressed up as a
        # lock -- see the module docstring on why no lock is taken.
        if intents_path.stat().st_mtime != mtime_at_read:
            raise MigrationRefused(
                f"{intents_path} changed while the plan was being built. Something is writing to the store -- "
                f"stop the bridge and re-run. Nothing was written."
            )

        insert_plan(conn, planned, intents_path, time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))

        # Verify by reading the rows back out, in a fresh statement rather than
        # trusting what the INSERTs returned -- "verify by behavior" is the
        # principle, and a count taken from the writing statement is not
        # independent evidence that the rows are there.
        written = conn.execute("SELECT COUNT(*) FROM swap_intents").fetchone()[0]
        print()
        print(f"APPLIED. {len(planned)} intents inserted; swap_intents now holds {written} rows in total.")
        print(f"{intents_path} was NOT modified and is still what server.js reads.")
        print(f"done in {format_duration(time.monotonic() - started)}")
        return 0
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Preview (and, with --apply, perform) the migration of swap intents from JSON into swap_terminal.db.",
    )
    parser.add_argument("--intents", type=Path, default=DEFAULT_INTENTS_PATH, help="path to swap_intents.json")
    parser.add_argument("--db", type=Path, default=Path(Config.DB_PATH), help="path to swap_terminal.db")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="actually write the rows. Without this flag nothing is written, which is the default on purpose: "
        "swap_intents.json holds armed state and moving it is the operator's call (CLAUDE.md rule 16).",
    )
    args = parser.parse_args(argv)

    try:
        return migrate(args.intents, args.db, args.apply)
    except MigrationRefused as refusal:
        # Refusals are printed as refusals, not as tracebacks. An operator
        # reading this needs the sentence, and a traceback buries it.
        print(f"REFUSED: {refusal}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
