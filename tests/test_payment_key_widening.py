"""The migration that lets two ledgers' block index 2 be two different payments.

Role: test (pure; a temporary sqlite file built with the PREVIOUS schema, then
      migrated by the real apply_migrations(). No socket, no chain, no key.)
Reads: swap_terminal/db.py
Writes: a throwaway database under pytest's tmp_path
Can move funds: no
Mainnet-safe: yes

=============================================================================
WHAT THIS MIGRATES, AND WHY IT IS A TABLE REBUILD
=============================================================================

`deposit_events` and `late_deposits` were keyed UNIQUE(asset, txid, vout), which
encodes an assumption: that a txid identifies a payment. It does, on every chain
whose txid is a hash. It does not on ICP, where chains/icp.py uses the LEDGER BLOCK
INDEX as the txid -- deliberately, for the reason its docstring gives -- and a block
index is unique only within ONE ledger.

`docker compose down` destroys the local replica's ledger and the rebuilt one
restarts at block 0. Measured on the operator's host 2026-10-10, from their own
admin page:

    s_f5cf62e0b7a9342a  ICP  txid 2  vout 0  0.05000000  conf 1   2026-10-07
    s_968a69b37c3da5c9  ICP  txid 2  vout 0  2.44081155           2026-10-10

On the old key those are one row. services/deposit_service.upsert_deposit_event()
found the 2026-10-07 row, bumped its confirmations and returned, so the live swap
got no row and sat at awaiting_deposit while its 2.44081155 ICP was in its
subaccount. Nothing raised.

SQLite can add and drop a column and CANNOT drop a table-level constraint, so this
is the documented create-copy-drop-rename -- more machinery than any other migration
in db.py, and the measurement above is what justifies it.

=============================================================================
THESE RUN THE REAL MIGRATION AGAINST THE REAL PREVIOUS SCHEMA
=============================================================================

The old DDL is not pasted. It is derived by reversing the one substitution the
change made, so this file cannot drift from db.SCHEMA the way a copied CREATE
statement would -- a column added to SCHEMA tomorrow appears in the "old" database
here too, which is exactly the case the rebuild must not drop.

Verified by outcome at every step: the key is read back from PRAGMA, the rows are
counted out of the real table, and the colliding INSERT is attempted for real.
"""

from __future__ import annotations

import re
import sqlite3

import db as db_module
import pytest
from db import (
    PAYMENT_KEYED_TABLES,
    PAYMENT_UNIQUE_KEY,
    PAYMENT_UNIQUE_KEY_BEFORE,
    SCHEMA,
    WIDEN_ALREADY,
    WIDEN_PROCEED,
    WIDEN_UNRECOGNIZED,
    WIDEN_VERDICTS,
    apply_migrations,
    connect_db,
    foreign_keys_enforced,
    rebuilt_create_sql,
    unique_keys,
    widen_payment_unique_key,
    widening_verdict,
)

NEW_CLAUSE = f"UNIQUE({', '.join(PAYMENT_UNIQUE_KEY)})"
OLD_CLAUSE = f"UNIQUE({', '.join(PAYMENT_UNIQUE_KEY_BEFORE)})"

#: Two ICP subaccounts. Hex account identifiers, so they are 64 characters and are
#: not a chain address any validator in this tree would be asked to parse.
DEAD_LEDGER_SUBACCOUNT = "a" * 64
LIVE_SUBACCOUNT = "b" * 64


def old_schema() -> str:
    """db.SCHEMA as it stood before the widening, derived rather than copied."""
    assert SCHEMA.count(NEW_CLAUSE) == len(PAYMENT_KEYED_TABLES), (
        f"SCHEMA carries {NEW_CLAUSE} {SCHEMA.count(NEW_CLAUSE)} time(s), not once per table in "
        f"PAYMENT_KEYED_TABLES. Either a third table grew the key or the clause was reworded, and "
        f"this file's reconstruction of the previous schema is no longer the previous schema."
    )
    return SCHEMA.replace(NEW_CLAUSE, OLD_CLAUSE)


@pytest.fixture
def legacy(tmp_path):
    """A database carrying the PREVIOUS payment key, with the 2026-10-07 row in it."""
    conn = connect_db(str(tmp_path / "legacy.db"))
    conn.executescript(old_schema())
    conn.execute(
        "INSERT INTO quotes (id, from_asset, to_asset, input_amount, quoted_rate, fee_bps,"
        " network_fee_reserve, output_amount_estimate, expires_at, created_at)"
        " VALUES ('q','ICP','GRC',0.05,327.21,150,0.001,16.0,"
        "'2999-01-01T00:00:00+00:00','2026-10-07T22:14:34+00:00')"
    )
    conn.execute(
        "INSERT INTO swaps (id, quote_id, from_asset, to_asset, deposit_address, payout_address,"
        " expected_input_amount, quoted_rate, fee_bps, network_fee_reserve,"
        " output_amount_estimate, status, min_confirmations, expires_at, created_at, updated_at)"
        " VALUES ('s_f5cf62e0b7a9342a','q','ICP','GRC',?,'mg3gJAmhADxf2ScRuXu7HXM2oixxiQG2Ap',"
        "0.05,327.21,150,0.001,16.0,'completed',1,'2999-01-01T00:00:00+00:00',"
        "'2026-10-07T22:14:34+00:00','2026-10-07T22:14:34+00:00')",
        (DEAD_LEDGER_SUBACCOUNT,),
    )
    conn.execute(
        "INSERT INTO deposit_events (swap_id, asset, txid, vout, address, amount, confirmations,"
        " first_seen_at, last_seen_at)"
        " VALUES ('s_f5cf62e0b7a9342a','ICP','2',0,?,0.05,1,'2026-10-07T22:14:34+00:00',"
        "'2026-10-07T22:14:34+00:00')",
        (DEAD_LEDGER_SUBACCOUNT,),
    )
    conn.commit()
    return conn


def seed_live_swap(conn):
    """The 2026-10-10 swap itself. ITS ROW HAS TO EXIST FOR THE DEPOSIT TO REFERENCE IT.

    db.py's SCHEMA opens with `PRAGMA foreign_keys=ON;`, so this connection enforces
    deposit_events' FOREIGN KEY to swaps(id) -- which is also why the migration has to turn
    enforcement off for the rebuild and put it back. Without this row the INSERT below fails
    on the foreign key and the test would be reporting on the wrong constraint.
    """
    conn.execute(
        "INSERT INTO swaps (id, quote_id, from_asset, to_asset, deposit_address, payout_address,"
        " expected_input_amount, quoted_rate, fee_bps, network_fee_reserve,"
        " output_amount_estimate, status, min_confirmations, expires_at, created_at, updated_at)"
        " VALUES ('s_968a69b37c3da5c9','q','ICP','GRC',?,'mg3gJAmhADxf2ScRuXu7HXM2oixxiQG2Ap',"
        "2.44081155,327.21,150,0.001,786.0,'awaiting_deposit',1,'2999-01-01T00:00:00+00:00',"
        "'2026-10-10T18:46:41+00:00','2026-10-10T18:46:41+00:00')",
        (LIVE_SUBACCOUNT,),
    )


def insert_live_row(conn):
    """The 2026-10-10 deposit, as upsert_deposit_event() would insert it."""
    conn.execute(
        "INSERT INTO deposit_events (swap_id, asset, txid, vout, address, amount, confirmations,"
        " first_seen_at, last_seen_at)"
        " VALUES ('s_968a69b37c3da5c9','ICP','2',0,?,2.44081155,1,'2026-10-10T18:46:41+00:00',"
        "'2026-10-10T18:46:41+00:00')",
        (LIVE_SUBACCOUNT,),
    )


# --- the fixture really is the old schema -------------------------------------

def test_the_legacy_fixture_carries_the_OLD_key_or_this_file_proves_nothing(legacy):
    """A fixture already carrying the new key would make every test below vacuous."""
    for table in PAYMENT_KEYED_TABLES:
        keys = [tuple(key) for key in unique_keys(legacy, table)]
        assert tuple(PAYMENT_UNIQUE_KEY_BEFORE) in keys, f"{table}: {keys}"
        assert tuple(PAYMENT_UNIQUE_KEY) not in keys, f"{table}: {keys}"


def test_the_collision_is_REAL_on_the_old_key(legacy):
    """The defect, reproduced as a database error rather than described.

    upsert_deposit_event() never reached this error, which is the part that made the
    bug silent: its SELECT matched first and it UPDATEd the 2026-10-07 row instead.
    Asserting the constraint here is what makes "these are one row to SQLite" a
    measurement.
    """
    seed_live_swap(legacy)
    with pytest.raises(sqlite3.IntegrityError, match=re.escape("deposit_events.vout")):
        insert_live_row(legacy)


# --- the migration ------------------------------------------------------------

def test_the_migration_widens_both_tables_and_says_which(legacy):
    result = apply_migrations(legacy)
    assert sorted(result["payment_keys_widened"]) == sorted(PAYMENT_KEYED_TABLES)
    for table in PAYMENT_KEYED_TABLES:
        keys = [tuple(key) for key in unique_keys(legacy, table)]
        assert tuple(PAYMENT_UNIQUE_KEY) in keys, (
            f"{table} was reported widened and its keys read back as {keys}"
        )


def test_the_2026_10_07_ROW_SURVIVES_THE_REBUILD(legacy):
    """Rule 7: never delete the record of what the system did with money.

    A rebuild that got the constraint right and lost the row would be the worse
    outcome of the two, and it is the one a create-copy-drop-rename can produce
    quietly.
    """
    apply_migrations(legacy)
    rows = legacy.execute(
        "SELECT swap_id, asset, txid, vout, address, amount, confirmations, first_seen_at"
        " FROM deposit_events"
    ).fetchall()
    assert [dict(row) for row in rows] == [{
        "swap_id": "s_f5cf62e0b7a9342a", "asset": "ICP", "txid": "2", "vout": 0,
        "address": DEAD_LEDGER_SUBACCOUNT, "amount": 0.05, "confirmations": 1,
        "first_seen_at": "2026-10-07T22:14:34+00:00",
    }]


def test_both_ledgers_block_index_2_can_now_coexist(legacy):
    """THE OUTCOME THE WHOLE CHANGE IS FOR."""
    apply_migrations(legacy)
    seed_live_swap(legacy)
    insert_live_row(legacy)
    legacy.commit()

    rows = legacy.execute(
        "SELECT swap_id, address, amount FROM deposit_events WHERE asset = 'ICP' AND txid = '2'"
        " ORDER BY id ASC"
    ).fetchall()
    assert [dict(row) for row in rows] == [
        {"swap_id": "s_f5cf62e0b7a9342a", "address": DEAD_LEDGER_SUBACCOUNT, "amount": 0.05},
        {"swap_id": "s_968a69b37c3da5c9", "address": LIVE_SUBACCOUNT, "amount": 2.44081155},
    ]


def test_the_SAME_address_at_the_same_index_is_STILL_one_row(legacy):
    """The guard the key exists for is not given up.

    The point of a unique key here is that one payment cannot be counted twice --
    refresh_swap_from_chain() SUMS every row for a swap, so a duplicate is a double
    credit. Widening must not weaken that for the case it was protecting.
    """
    apply_migrations(legacy)
    with pytest.raises(sqlite3.IntegrityError):
        legacy.execute(
            "INSERT INTO deposit_events (swap_id, asset, txid, vout, address, amount,"
            " confirmations, first_seen_at, last_seen_at)"
            " VALUES ('s_other','ICP','2',0,?,0.05,1,'2026-10-10T00:00:00+00:00',"
            "'2026-10-10T00:00:00+00:00')",
            (DEAD_LEDGER_SUBACCOUNT,),
        )


def test_the_non_unique_index_is_recreated_rather_than_lost_with_the_table(legacy):
    """DROP TABLE takes its indexes with it, and that is easy to not notice.

    `idx_deposit_events_swap_id` backs the read refresh_swap_from_chain() does on
    every cycle. Losing it costs a table scan per swap per 15s and nothing fails, so
    nothing would report it.
    """
    before = {
        row["name"] for row in legacy.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = 'deposit_events'"
            " AND sql IS NOT NULL"
        ).fetchall()
    }
    assert "idx_deposit_events_swap_id" in before, "the fixture has no named index to lose"
    apply_migrations(legacy)
    after = {
        row["name"] for row in legacy.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = 'deposit_events'"
            " AND sql IS NOT NULL"
        ).fetchall()
    }
    assert before <= after, f"indexes lost in the rebuild: {sorted(before - after)}"


# --- idempotence and the refusals ---------------------------------------------

def test_a_second_run_rebuilds_nothing(legacy):
    """Every worker calls apply_migrations() at start, so this runs three times a boot."""
    apply_migrations(legacy)
    again = apply_migrations(legacy)
    assert again["payment_keys_widened"] == []


def test_a_FRESH_database_is_never_rebuilt(tmp_path):
    """A database created from today's SCHEMA already has the key."""
    conn = connect_db(str(tmp_path / "fresh.db"))
    conn.executescript(SCHEMA)
    conn.commit()
    assert apply_migrations(conn)["payment_keys_widened"] == []


def test_a_table_with_neither_key_is_LEFT_ALONE_rather_than_guessed_at(tmp_path, caplog):
    """Rule 2's distinction: not recognizing a shape is not knowing what it is.

    A rebuild driven by a guess about unfamiliar DDL would be the one operation in
    this module that can destroy rows.
    """
    conn = sqlite3.connect(str(tmp_path / "odd.db"))
    conn.row_factory = db_module.dict_factory
    conn.execute(
        "CREATE TABLE deposit_events (id INTEGER PRIMARY KEY, asset TEXT, txid TEXT,"
        " vout INTEGER, address TEXT, UNIQUE(txid))"
    )
    conn.commit()
    with caplog.at_level("WARNING"):
        assert widen_payment_unique_key(conn, "deposit_events") is False
    assert "LEFT ALONE" in caplog.text
    assert [tuple(key) for key in unique_keys(conn, "deposit_events")] == [("txid",)]


# --- foreign key enforcement, which the real path has ON -----------------------

def test_the_fixture_connection_really_does_ENFORCE_foreign_keys(legacy):
    """Otherwise every test below runs on a connection the real app does not have.

    `db.py`'s SCHEMA begins with `PRAGMA foreign_keys=ON;` and the startup path runs
    executescript(SCHEMA) before apply_migrations() on the same connection. An earlier
    version of widen_payment_unique_key() REFUSED whenever enforcement was on -- its
    docstring claimed sqlite3 leaves it off and connect_db() does not set it, both true and
    both beside the point -- so the migration would have declined on every real database
    and logged a reason nobody reads on a worker that started fine. This assertion is what
    makes the rest of this file a test of the real configuration.
    """
    assert foreign_keys_enforced(legacy) == 1


def test_enforcement_is_still_ON_after_the_rebuild(legacy):
    """The rebuild turns it off, which is step 1 of SQLite's documented procedure.

    Leaving it off would be the quieter and worse outcome: every later INSERT on that
    connection would skip its foreign key check, and nothing would say so.
    """
    apply_migrations(legacy)
    assert foreign_keys_enforced(legacy) == 1


def test_a_connection_that_runs_WITHOUT_enforcement_keeps_it_off(legacy):
    """It RESTORES rather than setting ON, because both states are legitimate here.

    services/xrp_tag_service.py's own operator message says `PRAGMA foreign_keys=ON` is
    "which db.py's SCHEMA sets and a bare connect_db() does not", so which one a connection
    runs with is not this migration's to decide.
    """
    legacy.execute("PRAGMA foreign_keys = OFF")
    assert foreign_keys_enforced(legacy) == 0
    assert widen_payment_unique_key(legacy, "deposit_events") is True
    assert foreign_keys_enforced(legacy) == 0


def test_it_REFUSES_when_the_pragma_cannot_TAKE(legacy, caplog):
    """`PRAGMA foreign_keys` is a no-op inside a transaction, so OFF has to be read back.

    This is the one case the old refusal was right about: with a transaction already open
    the rebuild would run with enforcement live, and the DROP is the step that can orphan
    rows. So the pragma is set and then MEASURED, and a value that did not change refuses.
    """
    legacy.execute("BEGIN IMMEDIATE")
    with caplog.at_level("ERROR"):
        assert widen_payment_unique_key(legacy, "deposit_events") is False
    assert "did not take" in caplog.text
    legacy.rollback()
    keys = [tuple(key) for key in unique_keys(legacy, "deposit_events")]
    assert tuple(PAYMENT_UNIQUE_KEY_BEFORE) in keys, "nothing was changed by the refusal"
    assert legacy.execute("SELECT COUNT(*) AS n FROM deposit_events").fetchone()["n"] == 1


# --- the substitution, which has to see code and not prose ---------------------

def test_the_clause_is_matched_as_CODE_and_not_inside_a_comment():
    """SCHEMA's own comment quotes the old clause, and that made the rebuild refuse.

    MUTATION: drop replace_outside_comments() for a plain str.replace and this fails --
    the count comes back 2 and nothing is rebuilt. Found by this file's `legacy` fixture,
    which reconstructs the previous schema by reversing the substitution and therefore puts
    the clause in both places.
    """
    create = (
        "CREATE TABLE IF NOT EXISTS deposit_events (\n"
        "    id INTEGER PRIMARY KEY,\n"
        f"    -- This was {OLD_CLAUSE} and that encodes an assumption\n"
        f"    {OLD_CLAUSE}\n"
        ");"
    )
    rebuilt = rebuilt_create_sql(create, "deposit_events", "deposit_events_widening")
    assert " deposit_events_widening (" in rebuilt
    assert f"-- This was {OLD_CLAUSE} and that encodes" in rebuilt, (
        "the comment is kept verbatim -- dropping it would throw away the documentation "
        "sqlite_master carries about the constraint"
    )
    code = "\n".join(line.split("--", 1)[0] for line in rebuilt.splitlines())
    assert code.count(NEW_CLAUSE) == 1
    assert code.count(OLD_CLAUSE) == 0


def test_a_DDL_with_no_such_clause_raises_rather_than_being_rebuilt_from_a_guess():
    with pytest.raises(ValueError, match="as CODE 0 time"):
        rebuilt_create_sql(
            "CREATE TABLE IF NOT EXISTS deposit_events (id INTEGER, UNIQUE(txid));",
            "deposit_events", "deposit_events_widening",
        )


@pytest.mark.parametrize(
    ("keys", "expected"),
    [
        ([PAYMENT_UNIQUE_KEY], WIDEN_ALREADY),
        ([PAYMENT_UNIQUE_KEY_BEFORE], WIDEN_PROCEED),
        ([PAYMENT_UNIQUE_KEY_BEFORE, PAYMENT_UNIQUE_KEY], WIDEN_ALREADY),
        ([("txid",)], WIDEN_UNRECOGNIZED),
        ([], WIDEN_UNRECOGNIZED),
    ],
)
def test_the_verdict_is_pure_and_total(keys, expected):
    """The decision, called with seeded inputs (rule 10).

    It was three `if` branches inside the rebuild, which is where ruff's C901 was pointing.
    """
    assert widening_verdict(keys) == expected
    assert widening_verdict(keys) in WIDEN_VERDICTS
