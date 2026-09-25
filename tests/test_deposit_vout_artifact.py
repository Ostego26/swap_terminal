"""The vout=0 double count: reproduce it, report it, and delete only what was named.

Role: test / measurement (seeds real rows into a real SQLite database, runs the
        real functions and the real entry point, and asserts on the rows that
        are actually there afterwards)
Reads: swap_terminal/deposit_vout_artifact.py,
        swap_terminal/services/deposit_service.py, swap_terminal/chains/base.py,
        swap_terminal/db.py, migrate_deposit_vouts.py
Writes: throwaway databases under pytest's tmp_path. Never a real
        swap_terminal.db -- every test passes --db explicitly, and two tests
        assert that the dry run left the database byte-identical.
Can move funds: no. Nothing here opens a socket, reads a key, signs or
        broadcasts. Every adapter is a stub whose `call` is a seeded script.
Mainnet-safe: yes

WHAT THIS FILE IS FOR, AND IT IS FOUR SEPARATE CLAIMS THAT FAIL DIFFERENTLY.

1. THE DOUBLE COUNT IS REAL. Asserted by running the REAL
   refresh_swap_from_chain() against a real database holding a real fabricated
   row, and reading the swap's status back out. Not by reading that the code
   sums every row -- CLAUDE.md's verification section forbids accepting "the
   code contains a check for X" as evidence of anything, and only "condition X
   produced outcome Y" counts.

2. THE DRY RUN IS A DRY RUN. migrate_swap_intents.py's first version called
   executescript() before checking its flag and created a database during a
   "dry run". That is the failure this suite exists to keep caught, so it is
   asserted three ways: the database is byte-identical (md5 AND mtime, not just
   a row count, because a row count cannot see a rewritten page), a dry run
   against a nonexistent path leaves no file at all, and the connection is
   opened mode=ro so SQLite refuses a write rather than this file's control
   flow remembering to.

3. --apply DELETES EXACTLY WHAT WAS NAMED. Counted before and after, with the
   surviving ids listed, because "one row was deleted" and "the right row was
   deleted" are different claims.

4. A LEGITIMATE TWO-OUTPUT DEPOSIT SURVIVES. This is the one that would cost a
   customer money. A transaction that pays the deposit address twice produces
   exactly the artifact's shape, and a tool that deleted one of its rows would
   under-credit somebody who sent the full amount.

NOT TESTED HERE, because it cannot be from a test: that --apply against the
operator's real database does the right thing. It has never been run against a
real one, deliberately. What is tested is --apply against seeded databases with
the same shapes.
"""

import hashlib
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
from chains.base import RPCAdapter
from db import SCHEMA, connect_db
from deposit_vout_artifact import (
    AFFECTED_SWAP_ROWS_SQL,
    EFFECT_CORRECTS,
    EFFECT_NO_CHANGE,
    EFFECT_STILL_OUTSIDE,
    EFFECT_WOULD_BREAK,
    assess_swap,
    group_rows_by_swap,
    multi_vout_groups,
    suspect_effect,
    suspect_rows,
)
from services.deposit_service import refresh_swap_from_chain, warn_on_multi_vout_rows

from migrate_deposit_vouts import (
    MigrationRefused,
    backup_path_for,
    deletion_plan,
    main,
    open_database,
    refusal_for,
)

SCRIPT = Path(__file__).resolve().parent.parent / "migrate_deposit_vouts.py"

DEPOSIT_ADDRESS = "bcrt1qdepositaddressexample00000000000000000"
OTHER_ADDRESS = "bcrt1qsomebodyelse0000000000000000000000000"
TXID = "ab" * 32
OTHER_TXID = "cd" * 32
SEEDED_AT = "2026-09-20T08:00:00+00:00"
CONFIG = {"AMOUNT_TOLERANCE_PCT": 0.01}


# --------------------------------------------------------------------------
# Seeding. Real tables, real schema, real foreign keys -- db.SCHEMA itself, not
# a hand-copied subset of it, so a column added there breaks these tests rather
# than being quietly untested.
# --------------------------------------------------------------------------


def make_db(path: Path) -> sqlite3.Connection:
    conn = connect_db(str(path))
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


def seed_swap(conn, swap_id, status="awaiting_deposit", expected=1.5, min_confirmations=2):
    """Insert one swap and the quote its foreign key needs.

    The quote id is derived from the swap id rather than passed: no test needs
    two swaps to share a quote, and a parameter nothing varies is a parameter
    the next reader has to check the call sites for.
    """
    quote_id = f"q_{swap_id}"
    conn.execute(
        "INSERT INTO quotes (id, from_asset, to_asset, input_amount, quoted_rate, fee_bps, network_fee_reserve,"
        " output_amount_estimate, expires_at, created_at) VALUES (?, 'BTC', 'GRC', ?, 1000, 150, 0.00002, 1400, ?, ?)",
        (quote_id, expected, SEEDED_AT, SEEDED_AT),
    )
    conn.execute(
        "INSERT INTO swaps (id, quote_id, from_asset, to_asset, deposit_address, payout_address,"
        " expected_input_amount, quoted_rate, fee_bps, network_fee_reserve, output_amount_estimate, status,"
        " min_confirmations, created_at, updated_at, expires_at)"
        " VALUES (?, ?, 'BTC', 'GRC', ?, 'Gpayout', ?, 1000, 150, 0.00002, 1400, ?, ?, ?, ?, ?)",
        (swap_id, quote_id, DEPOSIT_ADDRESS, expected, status, min_confirmations, SEEDED_AT, SEEDED_AT, SEEDED_AT),
    )
    conn.commit()


def seed_event(conn, swap_id, **event):
    """Insert one deposit_events row. Everything but swap_id arrives as an event dict.

    Shaped like services.deposit_service.upsert_deposit_event(db, swap_id,
    asset, event) on purpose -- the real writer takes the row as a mapping too,
    so a reader moving between them is reading the same thing. It is also what
    keeps this helper under the argument ceiling without a suppression: the
    fields are data, not a signature that grows every time a column is needed.
    """
    row = {
        "asset": "BTC",
        "txid": TXID,
        "address": DEPOSIT_ADDRESS,
        "confirmations": 4,
        "first_seen_at": SEEDED_AT,
        **event,
    }
    conn.execute(
        "INSERT INTO deposit_events (swap_id, asset, txid, vout, address, amount, confirmations,"
        " first_seen_at, last_seen_at, credited_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)",
        (
            swap_id, row["asset"], row["txid"], row["vout"], row["address"],
            row["amount"], row["confirmations"], row["first_seen_at"], SEEDED_AT,
        ),
    )
    conn.commit()


class StubAdapter(RPCAdapter):
    """An RPCAdapter whose `call` is a seeded script instead of a socket.

    Subclasses the REAL adapter, so find_deposits_to_address() and
    _extract_matching_vouts() are the real ones. Named and shaped like the
    stubs in tests/test_deposit_vout_matching.py and
    tests/test_address_validation.py on purpose: a reader who finds one must
    not be surprised by the others.
    """

    asset = "BTC"

    def __init__(self, deposit_vout=2, amount=1.5, confirmations=4):
        # Not a credential: this stub never opens a socket, so the values are
        # here only because RPCAdapter.__init__ requires them.
        super().__init__(user="u", password="p", host="127.0.0.1", port=18443)  # noqa: S106
        self._vout = deposit_vout
        self._amount = amount
        self._confirmations = confirmations

    def call(self, method, *params):
        if method == "listtransactions":
            return [{"category": "receive", "address": DEPOSIT_ADDRESS, "amount": self._amount, "txid": TXID}]
        if method == "getrawtransaction":
            outputs = [
                {"n": index, "value": 0.1, "scriptPubKey": {"address": OTHER_ADDRESS}}
                for index in range(self._vout)
            ]
            outputs.append(
                {"n": self._vout, "value": self._amount, "scriptPubKey": {"address": DEPOSIT_ADDRESS}}
            )
            return {"confirmations": self._confirmations, "vout": outputs}
        raise AssertionError(f"stub had no scripted answer for {method}")


def fingerprint(path: Path) -> tuple[str, int, int]:
    """md5, size and mtime_ns of the database file.

    A row count is not enough on its own: a DELETE and a re-INSERT of the same
    values leaves the count identical, and "the dry run wrote nothing" is the
    claim this file's fifth section exists to hold.

    WHY THE MAIN FILE ALONE IS ENOUGH, MEASURED 2026-09-25 RATHER THAN ASSUMED.
    db.py sets `PRAGMA journal_mode=WAL`, so a committed write lands in the
    `-wal` sidecar first and the main file can lag behind it. That would make
    this function blind to a write -- except that SQLite checkpoints the WAL
    back into the main file when the LAST connection closes, and every run
    these tests fingerprint is a subprocess that exits. Measured directly on a
    seeded database:

        after a real DELETE and close   md5 5e96d65be072 -> fad07074ff15,
                                        mtime_ns changed, -wal gone
        with the connection still open  main file unchanged, -wal present

    So the fingerprint is taken only across a process boundary, never while a
    connection this test holds is open. The nuance is written down because it
    already produced one misleading measurement: a mutation that made the dry
    run run `UPDATE deposit_events SET last_seen_at = last_seen_at` did not
    trip this check, and the reason was not WAL -- SQLite optimizes a no-op
    assignment away and wrote nothing at all. A mutation that wrote a real
    value tripped it, and seven other tests with it.
    """
    return (
        hashlib.md5(path.read_bytes()).hexdigest(),  # noqa: S324 -- checked: this is a change detector for a test fixture, not a security primitive. Nothing here authenticates anything.
        path.stat().st_size,
        path.stat().st_mtime_ns,
    )


# --------------------------------------------------------------------------
# 1. The double count, through the real function
# --------------------------------------------------------------------------


def test_the_double_count_reproduces_through_the_real_refresh(tmp_path):
    """THE ARTIFACT. A fabricated vout=0 row plus a real vout=2 row halts the swap.

    This is the measurement the migration exists for, and it is taken by
    running services.deposit_service.refresh_swap_from_chain() -- the real
    function, against a real database -- not by reading that it sums every row.
    """
    conn = make_db(tmp_path / "swap_terminal.db")
    seed_swap(conn, "s_open", status="awaiting_deposit", expected=1.5)
    # The pre-deploy row: vout=0, the amount from the wallet's listtransactions
    # summary, confirmations from a second RPC. Exactly what the fabricated
    # branch of _extract_matching_vouts() writes.
    seed_event(conn, "s_open", vout=0, amount=1.5, confirmations=4)

    before = conn.execute("SELECT COUNT(*) AS n FROM deposit_events").fetchone()["n"]
    assert before == 1

    swap = conn.execute("SELECT * FROM swaps WHERE id = 's_open'").fetchone()
    refreshed = refresh_swap_from_chain(conn, CONFIG, {"BTC": StubAdapter(deposit_vout=2)}, swap)

    rows = conn.execute("SELECT vout, amount FROM deposit_events WHERE swap_id = 's_open' ORDER BY vout").fetchall()
    assert [int(row["vout"]) for row in rows] == [0, 2], "the real output did not insert a second row"
    assert sum(float(row["amount"]) for row in rows) == 3.0, "the sum did not double"
    assert refreshed["status"] == "under_review"
    assert refreshed["actual_input_amount"] == 3.0
    assert "outside tolerance" in refreshed["failed_reason"]


def test_deleting_the_row_fixes_the_sum_but_does_not_unstick_the_swap(tmp_path):
    """The measurement behind the script's second refusal, and it is the operator's.

    Removing the fabricated row corrects actual_input_amount. It does NOT move
    the swap out of `under_review`: refresh_swap_from_chain()'s recovery branch
    is `elif current_status in {"confirming", "deposit_seen", "awaiting_deposit"}`
    and `under_review` is not in it, and ACTIVE_STATUSES does not contain it
    either, so process_active_swaps() never selects the swap again.

    If this assertion ever inverts -- if a later change makes the swap recover
    by itself -- the migration's output is telling the operator to do something
    unnecessary, and this test is where that gets caught.
    """
    conn = make_db(tmp_path / "swap_terminal.db")
    seed_swap(conn, "s_open", status="under_review", expected=1.5)
    conn.execute("UPDATE swaps SET actual_input_amount = 3.0, failed_reason = 'stale' WHERE id = 's_open'")
    seed_event(conn, "s_open", vout=0, amount=1.5)
    seed_event(conn, "s_open", vout=2, amount=1.5)
    conn.commit()

    conn.execute("DELETE FROM deposit_events WHERE swap_id = 's_open' AND vout = 0")
    conn.commit()

    swap = conn.execute("SELECT * FROM swaps WHERE id = 's_open'").fetchone()
    refreshed = refresh_swap_from_chain(conn, CONFIG, {"BTC": StubAdapter(deposit_vout=2)}, swap)

    assert refreshed["actual_input_amount"] == 1.5, "the sum was not corrected by the deletion"
    assert refreshed["status"] == "under_review", "the swap recovered by itself; the migration's notice is now wrong"
    assert refreshed["failed_reason"] == "stale", "the stale reason was cleared; the notice is now wrong"


# --------------------------------------------------------------------------
# 2. The decisions, called with seeded inputs (rule 10)
# --------------------------------------------------------------------------


def rows_for(vouts_and_amounts, confirmations=4, txid=TXID, asset="BTC"):
    """Build rows in the shape AFFECTED_SWAP_ROWS_SQL returns, for a pure-function test."""
    return [
        {
            "event_id": index + 1,
            "swap_id": "s",
            "asset": asset,
            "txid": txid,
            "vout": vout,
            "amount": amount,
            "confirmations": confirmations,
            "first_seen_at": SEEDED_AT,
            "last_seen_at": SEEDED_AT,
            "credited_at": None,
            "swap_status": "under_review",
            "expected_input_amount": 1.5,
            "actual_input_amount": 3.0,
            "min_confirmations": 2,
            "failed_reason": None,
        }
        for index, (vout, amount) in enumerate(vouts_and_amounts)
    ]


def test_multi_vout_groups_only_returns_groups_that_disagree_about_vout():
    """One row is not a finding; two rows at the same vout cannot exist; two vouts are."""
    assert multi_vout_groups(rows_for([(0, 1.5)])) == {}
    groups = multi_vout_groups(rows_for([(0, 1.5), (2, 1.5)]))
    assert list(groups) == [("BTC", TXID)]
    assert [int(row["vout"]) for row in groups[("BTC", TXID)]] == [0, 2]


def test_a_vout_zero_row_alone_in_its_transaction_is_not_a_suspect():
    """Deleting it would delete the swap's only record that the deposit arrived."""
    assert suspect_rows(rows_for([(0, 1.5)])) == []
    assert [int(row["event_id"]) for row in suspect_rows(rows_for([(0, 1.5), (2, 1.5)]))] == [1]


def test_two_transactions_are_two_groups_and_only_the_split_one_is_a_finding():
    """A swap may legitimately be funded by several transactions; that is not the artifact."""
    rows = rows_for([(0, 1.5), (2, 1.5)]) + rows_for([(0, 0.5)], txid=OTHER_TXID)
    groups = multi_vout_groups(rows)
    assert list(groups) == [("BTC", TXID)]


def test_assess_swap_reproduces_the_gates_own_figures():
    """confirmed_total is what deposit_service.py:67 sums, and the window is :76-77's."""
    assessment = assess_swap(rows_for([(0, 1.5), (2, 1.5)]), 0.01)
    assert assessment["confirmed_total"] == 3.0
    assert assessment["confirmed_total_without_suspects"] == 1.5
    assert assessment["tolerance_low"] == pytest.approx(1.485)
    assert assessment["tolerance_high"] == pytest.approx(1.515)
    assert assessment["inside_with_suspects"] is False
    assert assessment["inside_without_suspects"] is True


def test_an_unconfirmed_row_is_in_seen_total_and_not_in_confirmed_total():
    """min_confirmations is a COUNT OF BLOCKS and the two totals differ because of it (rule 6)."""
    rows = rows_for([(0, 1.5)]) + rows_for([(2, 1.5)], confirmations=1)
    rows[1]["event_id"] = 2
    assessment = assess_swap(rows, 0.01)
    assert assessment["seen_total"] == 3.0
    assert assessment["confirmed_total"] == 1.5, "a row below min_confirmations was counted as confirmed"


def test_suspect_effect_offers_only_the_deletion_that_corrects():
    """THE DISCRIMINATOR. Outside -> inside is offered; inside -> outside never is."""
    corrects = assess_swap(rows_for([(0, 1.5), (2, 1.5)]), 0.01)
    assert corrects["effect"]["effect"] == EFFECT_CORRECTS
    assert corrects["effect"]["offered"] is True


def test_a_legitimate_two_output_deposit_is_not_offered_for_deletion():
    """THE ONE THAT WOULD COST A CUSTOMER MONEY.

    One transaction paying the deposit address twice, 0.6 + 0.9 = 1.5, which is
    exactly the expected amount. The rows have the artifact's shape and the
    total is already correct, so deleting the vout=0 row would take a correct
    swap OUTSIDE tolerance and under-credit somebody who sent the full amount.
    """
    rows = rows_for([(0, 0.6), (3, 0.9)])
    assessment = assess_swap(rows, 0.01)
    assert assessment["inside_with_suspects"] is True
    assert assessment["inside_without_suspects"] is False
    assert assessment["effect"]["effect"] == EFFECT_WOULD_BREAK
    assert assessment["effect"]["offered"] is False
    assert "under-crediting" in assessment["effect"]["note"]


def test_a_deletion_that_leaves_the_swap_halted_is_offered_with_that_said():
    """A fabricated row over a genuinely short deposit: still fabricated, still halted."""
    rows = rows_for([(0, 1.5), (2, 0.9)])
    assessment = assess_swap(rows, 0.01)
    assert assessment["effect"]["effect"] == EFFECT_STILL_OUTSIDE
    assert assessment["effect"]["offered"] is True
    assert "does NOT bring the swap back inside tolerance" in assessment["effect"]["note"]


def test_a_settled_swap_is_never_offered_even_when_it_carries_the_shape():
    """A completed swap can show the `corrects` shape, and must still not be offered.

    Found by running the script against a seeded database: the settled swap's
    row was appearing in the copy-pasteable --apply command while its own block
    said its rows were refused. Two lines of one screen disagreeing is how an
    operator stops trusting the screen.
    """
    rows = rows_for([(0, 1.5), (2, 1.5)])
    for row in rows:
        row["swap_status"] = "completed"
    assessment = assess_swap(rows, 0.01)
    assert assessment["settled"] is True
    assert assessment["effect"]["offered"] is False
    assert assessment["effect"]["effect"] == EFFECT_NO_CHANGE
    assert "REFUSED" in assessment["effect"]["note"]


def test_an_orphaned_event_row_reports_unknown_rather_than_zero():
    """No swap row means no expected amount; printing 0.0 would be a number with nothing behind it."""
    rows = rows_for([(0, 1.5), (2, 1.5)])
    for row in rows:
        row["swap_status"] = None
        row["expected_input_amount"] = None
        row["min_confirmations"] = None
    assessment = assess_swap(rows, 0.01)
    assert assessment["orphaned"] is True
    assert assessment["expected"] is None
    assert assessment["confirmed_total"] is None
    assert assessment["effect"]["offered"] is False


def test_suspect_effect_is_a_no_change_when_the_suspect_is_unconfirmed():
    """Removing a row the gate never counted cannot change what the gate decides."""
    rows = rows_for([(0, 1.5)], confirmations=0) + rows_for([(2, 1.5)])
    rows[1]["event_id"] = 2
    assessment = assess_swap(rows, 0.01)
    assert assessment["confirmed_total"] == 1.5
    assert assessment["confirmed_total_without_suspects"] == 1.5
    assert suspect_effect(assessment)["offered"] is False


# --------------------------------------------------------------------------
# 3. The SQL and the in-memory grouping must agree (rule 8)
# --------------------------------------------------------------------------


def test_the_sql_and_the_in_memory_grouping_agree(tmp_path):
    """Two implementations of one rule, asserted equal against seeded rows.

    AFFECTED_SWAP_ROWS_SQL scans the table; multi_vout_groups() walks rows the
    deposit path already has. They exist separately for a measured reason (the
    warning must not cost a round trip on every poll) and rule 8 says the
    difference has to be held by something. This is that something.
    """
    conn = make_db(tmp_path / "swap_terminal.db")
    seed_swap(conn, "s_split")
    seed_event(conn, "s_split", vout=0, amount=1.5)
    seed_event(conn, "s_split", vout=2, amount=1.5)
    seed_swap(conn, "s_single")
    seed_event(conn, "s_single", vout=0, amount=1.5, txid=OTHER_TXID)
    seed_swap(conn, "s_two_txids")
    seed_event(conn, "s_two_txids", vout=1, amount=0.7, txid="11" * 32)
    seed_event(conn, "s_two_txids", vout=1, amount=0.8, txid="22" * 32)

    from_sql = {row["swap_id"] for row in conn.execute(AFFECTED_SWAP_ROWS_SQL).fetchall()}

    all_rows = conn.execute("SELECT *, id AS event_id FROM deposit_events").fetchall()
    from_python = {
        swap_id
        for swap_id, rows in group_rows_by_swap(all_rows).items()
        if multi_vout_groups(rows)
    }

    assert from_sql == from_python == {"s_split"}


# --------------------------------------------------------------------------
# 4. The warning in the deposit path
# --------------------------------------------------------------------------


def test_the_deposit_path_warns_when_two_rows_for_one_txid_are_summed(tmp_path, caplog):
    """The one code change: a WARNING naming the swap and the vouts.

    Asserted through the REAL refresh_swap_from_chain(), so it measures that
    the warning is reached on the real path and not merely that the helper
    works when called directly.
    """
    conn = make_db(tmp_path / "swap_terminal.db")
    seed_swap(conn, "s_open", status="awaiting_deposit")
    seed_event(conn, "s_open", vout=0, amount=1.5)

    swap = conn.execute("SELECT * FROM swaps WHERE id = 's_open'").fetchone()
    with caplog.at_level("WARNING"):
        refresh_swap_from_chain(conn, CONFIG, {"BTC": StubAdapter(deposit_vout=2)}, swap)

    warnings = [record.getMessage() for record in caplog.records if record.levelname == "WARNING"]
    assert len(warnings) == 1, f"expected exactly one warning, got {warnings}"
    assert "s_open" in warnings[0], "the warning does not name the swap"
    assert TXID in warnings[0], "the warning does not name the transaction"
    assert "vout 0, 2" in warnings[0], "the warning does not name the vouts"
    assert "DOUBLE COUNT" in warnings[0]


def test_the_deposit_path_is_silent_when_one_transaction_makes_one_row(tmp_path, caplog):
    """A warning that fires on every ordinary deposit is a warning nobody reads."""
    conn = make_db(tmp_path / "swap_terminal.db")
    seed_swap(conn, "s_open", status="awaiting_deposit")

    swap = conn.execute("SELECT * FROM swaps WHERE id = 's_open'").fetchone()
    with caplog.at_level("WARNING"):
        refresh_swap_from_chain(conn, CONFIG, {"BTC": StubAdapter(deposit_vout=2)}, swap)

    assert [r.getMessage() for r in caplog.records if r.levelname == "WARNING"] == []


def test_the_warning_changes_nothing_about_what_is_credited(tmp_path):
    """It is a diagnostic. The credit path must compute and decide exactly as before.

    A swap whose single real deposit confirms must still reach payout_pending
    with the warning code in place -- the guard against a "diagnostic" that
    quietly became a branch.
    """
    conn = make_db(tmp_path / "swap_terminal.db")
    seed_swap(conn, "s_ok", status="awaiting_deposit", expected=1.5)

    swap = conn.execute("SELECT * FROM swaps WHERE id = 's_ok'").fetchone()
    refreshed = refresh_swap_from_chain(conn, CONFIG, {"BTC": StubAdapter(deposit_vout=2)}, swap)

    assert refreshed["status"] == "payout_pending"
    assert refreshed["actual_input_amount"] == 1.5
    assert refreshed["credited_at"] is not None


def test_warn_on_multi_vout_rows_returns_nothing_and_touches_nothing():
    """Called directly with seeded rows: it is a print, not a filter."""
    rows = rows_for([(0, 1.5), (2, 1.5)])
    assert warn_on_multi_vout_rows("s", rows) is None
    assert len(rows) == 2, "the diagnostic mutated its input"


# --------------------------------------------------------------------------
# 5. The dry run writes nothing
# --------------------------------------------------------------------------


def affected_db(tmp_path, name="swap_terminal.db"):
    """One halted swap with the artifact, one legitimate two-output deposit, one settled swap, one clean swap."""
    path = tmp_path / name
    conn = make_db(path)
    seed_swap(conn, "s_halted", status="under_review", expected=1.5)
    conn.execute("UPDATE swaps SET actual_input_amount = 3.0, failed_reason = 'stale' WHERE id = 's_halted'")
    seed_event(conn, "s_halted", vout=0, amount=1.5, first_seen_at="2026-09-20T08:00:00+00:00")
    seed_event(conn, "s_halted", vout=2, amount=1.5, first_seen_at="2026-09-25T14:10:00+00:00")

    seed_swap(conn, "s_legit", status="confirming", expected=1.5)
    seed_event(conn, "s_legit", vout=0, amount=0.6, txid=OTHER_TXID)
    seed_event(conn, "s_legit", vout=3, amount=0.9, txid=OTHER_TXID)

    seed_swap(conn, "s_done", status="completed", expected=1.5)
    seed_event(conn, "s_done", vout=0, amount=1.5, txid="33" * 32)
    seed_event(conn, "s_done", vout=1, amount=1.5, txid="33" * 32)

    seed_swap(conn, "s_clean", status="confirming", expected=1.5)
    seed_event(conn, "s_clean", vout=1, amount=1.5, txid="44" * 32)
    conn.commit()
    conn.close()
    return path


def run_script(args):
    """Run the real entry point as a subprocess, the way an operator does."""
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True,
        text=True,
        check=False,
    )


def test_a_dry_run_leaves_the_database_byte_identical(tmp_path):
    """md5, size and mtime, not a row count. An UPDATE writing the same values back is invisible to a count."""
    path = affected_db(tmp_path)
    before = fingerprint(path)
    result = run_script(["--db", str(path), "--run-dir", str(tmp_path / "run")])
    assert result.returncode == 0, result.stderr
    assert fingerprint(path) == before, "the dry run modified the database"


def test_a_dry_run_leaves_the_row_count_unchanged(tmp_path):
    """The count as well as the bytes, because they fail for different reasons."""
    path = affected_db(tmp_path)
    conn = sqlite3.connect(path)
    before = conn.execute("SELECT COUNT(*) FROM deposit_events").fetchone()[0]
    conn.close()

    run_script(["--db", str(path), "--run-dir", str(tmp_path / "run")])

    conn = sqlite3.connect(path)
    assert conn.execute("SELECT COUNT(*) FROM deposit_events").fetchone()[0] == before
    conn.close()


def test_a_dry_run_against_a_nonexistent_path_creates_no_file(tmp_path):
    """migrate_swap_intents.py's first version created a database during a dry run.

    sqlite3.connect() creates a missing file, so this is not a hypothetical
    hazard -- it is the one that already happened in this repository once.
    """
    missing = tmp_path / "does_not_exist.db"
    result = run_script(["--db", str(missing), "--run-dir", str(tmp_path / "run")])
    assert result.returncode == 0, result.stderr
    assert not missing.exists(), "the dry run created a database"
    assert list(tmp_path.glob("*.db")) == [], f"the dry run left files behind: {list(tmp_path.iterdir())}"


def test_a_dry_run_connection_refuses_a_write_at_the_sqlite_level(tmp_path):
    """mode=ro is the guard, not this script's control flow remembering to check a flag."""
    path = affected_db(tmp_path)
    conn = open_database(path, apply=False)
    try:
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            conn.execute("DELETE FROM deposit_events WHERE id = 1")
    finally:
        conn.close()


def test_a_clean_database_says_none_rather_than_printing_a_gap(tmp_path):
    """The most likely outcome, and the operator has to be able to read it off the screen."""
    path = tmp_path / "swap_terminal.db"
    conn = make_db(path)
    seed_swap(conn, "s_clean", status="confirming")
    seed_event(conn, "s_clean", vout=1, amount=1.5)
    conn.close()

    result = run_script(["--db", str(path), "--run-dir", str(tmp_path / "run")])
    assert result.returncode == 0, result.stderr
    assert "affected swaps  0" in result.stdout
    assert "(none)" in result.stdout
    assert "No swap is double-counted" in result.stdout


def test_an_empty_database_says_none_too(tmp_path):
    """Zero rows and a broken query must not render the same way."""
    path = tmp_path / "swap_terminal.db"
    make_db(path).close()
    result = run_script(["--db", str(path), "--run-dir", str(tmp_path / "run")])
    assert result.returncode == 0, result.stderr
    assert "(none)" in result.stdout
    assert "deposit_events is empty" in result.stdout


def test_the_dry_run_announces_the_database_the_tolerance_and_the_workers(tmp_path):
    """Rule 14: echo the parameters that decide the answer, before any finding."""
    path = affected_db(tmp_path)
    result = run_script(["--db", str(path), "--run-dir", str(tmp_path / "run")])
    assert str(path) in result.stdout
    assert "AMOUNT_TOLERANCE_PCT=" in result.stdout
    assert "DRY RUN" in result.stdout
    assert "deposit_watcher" in result.stdout
    assert "PARTIAL CHECK" in result.stdout, "the limit of the worker check is not stated"
    # Rule 6: the duration is microfortnights with the seconds in parentheses,
    # the micro sign is U+00B5, and there is no space before either unit.
    assert "µfn (" in result.stdout
    assert "ufn" not in result.stdout.replace("µfn", "")
    assert " µfn" not in result.stdout


def test_the_dry_run_prints_every_row_and_both_totals(tmp_path):
    """The operator has to see at a glance whether removing the row lands inside tolerance."""
    path = affected_db(tmp_path)
    result = run_script(["--db", str(path), "--run-dir", str(tmp_path / "run")])
    assert "confirmed total, every row" in result.stdout
    assert "confirmed total without vout=0" in result.stdout
    assert "first_seen_at" in result.stdout
    assert "credited_at" in result.stdout
    assert "expected_input_amount" in result.stdout
    assert "2026-09-20T08:00:00+00:00" in result.stdout, "first_seen_at was not printed for the suspect row"


def test_the_dry_run_names_the_settled_swap_and_leaves_it_alone(tmp_path):
    """Rewriting settled history is worse than leaving an artifact in it."""
    path = affected_db(tmp_path)
    result = run_script(["--db", str(path), "--run-dir", str(tmp_path / "run")])
    assert "s_done" in result.stdout
    assert "SETTLED" in result.stdout
    assert "--delete-event-id 5" not in result.stdout, "a settled swap's row was offered for deletion"


def test_the_dry_run_withholds_the_legitimate_two_output_deposit(tmp_path):
    """It must be REPORTED and NOT offered -- the customer-money case, end to end."""
    path = affected_db(tmp_path)
    result = run_script(["--db", str(path), "--run-dir", str(tmp_path / "run")])
    assert "s_legit" in result.stdout, "the legitimate deposit was not reported at all"
    assert "NOT OFFERED" in result.stdout
    assert "under-crediting" in result.stdout
    offer = [line for line in result.stdout.splitlines() if line.strip().startswith("OFFERED:")]
    assert len(offer) == 1, f"expected exactly one offered swap, got {offer}"
    assert "--delete-event-id 1" in offer[0]


# --------------------------------------------------------------------------
# 6. --apply
# --------------------------------------------------------------------------


def test_apply_without_an_id_refuses_and_writes_nothing(tmp_path):
    """There is no flag that picks a row. --apply alone is a refusal, not a default."""
    path = affected_db(tmp_path)
    before = fingerprint(path)
    result = run_script(["--db", str(path), "--run-dir", str(tmp_path / "run"), "--apply"])
    assert result.returncode == 2
    assert "never chooses a row for you" in result.stderr
    assert fingerprint(path) == before
    assert list(tmp_path.glob("*pre-vout-migration*")) == [], "a backup was taken for a refused run"


def test_apply_deletes_exactly_the_named_row_and_nothing_else(tmp_path):
    """Counted before and after, with the surviving ids listed.

    "One row was deleted" and "the right row was deleted" are different claims,
    so both are asserted.
    """
    path = affected_db(tmp_path)
    conn = sqlite3.connect(path)
    before = [row[0] for row in conn.execute("SELECT id FROM deposit_events ORDER BY id")]
    conn.close()
    assert before == [1, 2, 3, 4, 5, 6, 7]

    result = run_script(["--db", str(path), "--run-dir", str(tmp_path / "run"), "--apply", "--delete-event-id", "1"])
    assert result.returncode == 0, result.stderr
    assert "APPLIED. 1 row(s) deleted" in result.stdout

    conn = sqlite3.connect(path)
    after = [row[0] for row in conn.execute("SELECT id FROM deposit_events ORDER BY id")]
    swaps = dict(conn.execute("SELECT id, status FROM swaps"))
    actual = conn.execute("SELECT actual_input_amount FROM swaps WHERE id = 's_halted'").fetchone()[0]
    conn.close()

    assert after == [2, 3, 4, 5, 6, 7], "the wrong rows are present after the delete"
    assert swaps["s_halted"] == "under_review", "the migration moved a swap's status"
    assert actual == 3.0, "the migration wrote to the swaps table"


def test_apply_takes_a_backup_that_opens_as_a_database_and_holds_the_deleted_row(tmp_path):
    """Connection.backup(), not shutil.copy2 -- db.py sets journal_mode=WAL (rule 12)."""
    path = affected_db(tmp_path)
    result = run_script(["--db", str(path), "--run-dir", str(tmp_path / "run"), "--apply", "--delete-event-id", "1"])
    assert result.returncode == 0, result.stderr

    backups = list(tmp_path.glob("*pre-vout-migration*.db"))
    assert len(backups) == 1, f"expected exactly one backup, got {backups}"
    backup = backups[0]
    assert str(backup) in result.stdout, "the backup path was not printed"

    conn = sqlite3.connect(f"file:{backup}?mode=ro", uri=True)
    try:
        assert conn.execute("SELECT COUNT(*) FROM deposit_events").fetchone()[0] == 7
        assert conn.execute("SELECT 1 FROM deposit_events WHERE id = 1").fetchone() is not None
    finally:
        conn.close()


def test_apply_refuses_to_overwrite_an_existing_backup(tmp_path):
    """A path that already exists is a second run in one second or the wrong file. Both stop."""
    path = affected_db(tmp_path)
    destination = tmp_path / "already-here.db"
    destination.write_bytes(b"not a database")
    before = fingerprint(path)

    result = run_script(
        ["--db", str(path), "--run-dir", str(tmp_path / "run"), "--apply", "--delete-event-id", "1",
         "--backup", str(destination)]
    )
    assert result.returncode == 2
    assert "refusing to overwrite" in result.stderr
    assert fingerprint(path) == before, "rows were deleted despite the backup refusal"
    assert destination.read_bytes() == b"not a database"


def test_apply_refuses_a_row_that_is_not_at_vout_zero(tmp_path):
    """The fabricated branch could only ever write vout=0."""
    path = affected_db(tmp_path)
    before = fingerprint(path)
    result = run_script(["--db", str(path), "--run-dir", str(tmp_path / "run"), "--apply", "--delete-event-id", "2"])
    assert result.returncode == 2
    assert "vout=2, not 0" in result.stderr
    assert fingerprint(path) == before


def test_apply_refuses_a_settled_swaps_row(tmp_path):
    """s_done is completed. Its rows are refused, not merely unoffered."""
    path = affected_db(tmp_path)
    before = fingerprint(path)
    result = run_script(["--db", str(path), "--run-dir", str(tmp_path / "run"), "--apply", "--delete-event-id", "5"])
    assert result.returncode == 2
    assert "is completed" in result.stderr
    assert fingerprint(path) == before


def test_apply_refuses_an_id_that_was_never_reported(tmp_path):
    """Only a row this run found and printed may be deleted."""
    path = affected_db(tmp_path)
    before = fingerprint(path)
    result = run_script(["--db", str(path), "--run-dir", str(tmp_path / "run"), "--apply", "--delete-event-id", "7"])
    assert result.returncode == 2
    assert "not one of the rows reported above" in result.stderr
    assert fingerprint(path) == before


def test_apply_reports_every_refused_id_at_once(tmp_path):
    """An operator fixing one typo at a time runs --apply four times against a live database."""
    path = affected_db(tmp_path)
    result = run_script(
        ["--db", str(path), "--run-dir", str(tmp_path / "run"), "--apply",
         "--delete-event-id", "2", "--delete-event-id", "5", "--delete-event-id", "99"]
    )
    assert result.returncode == 2
    assert "vout=2, not 0" in result.stderr
    assert "is completed" in result.stderr
    assert "99: not one of the rows reported above" in result.stderr


def test_one_vout_zero_row_per_transaction_is_a_database_constraint(tmp_path):
    """The invariant deletion_plan()'s missing fourth guard rests on.

    Asserted by INSERTING a second vout=0 row and catching the error, not by
    reading `UNIQUE(asset, txid, vout)` out of db.SCHEMA. The behavioral
    verification principle is explicit that "the code contains a constraint" is
    not evidence a constraint is enforced.

    If this ever stops raising, deletion_plan()'s reasoning is void and the
    guard it removed has to come back -- which is why the argument is pinned
    here rather than written only in a comment.
    """
    path = tmp_path / "swap_terminal.db"
    conn = make_db(path)
    seed_swap(conn, "s_one")
    seed_event(conn, "s_one", vout=0, amount=1.5)
    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE constraint failed"):
        seed_event(conn, "s_one", vout=0, amount=0.9)
    conn.close()


def test_a_plan_always_leaves_a_row_for_every_affected_transaction(tmp_path):
    """The invariant itself: never every row of one transaction, whatever is named.

    Rule 2 -- a guard that was removed as unreachable has its test changed to
    pin the stronger invariant rather than deleted with it. Every suspect id
    this script would ever offer is named at once, which is the worst case a
    correct-looking command line can produce.
    """
    path = affected_db(tmp_path)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    rows = [dict(row) for row in conn.execute(AFFECTED_SWAP_ROWS_SQL).fetchall()]
    conn.close()

    assessments = [assess_swap(swap_rows, 0.01) for swap_rows in group_rows_by_swap(rows).values()]
    every_suspect = [i for a in assessments if not a["settled"] for i in a["suspect_ids"]]
    assert every_suspect, "the fixture carries no suspects; this test would assert nothing"

    planned_ids = {int(row["event_id"]) for row in deletion_plan(assessments, every_suspect)}
    for assessment in assessments:
        for (asset, txid), group in assessment["groups"].items():
            survivors = [row for row in group if int(row["event_id"]) not in planned_ids]
            assert survivors, f"every row of {asset} {txid} would be deleted"


def test_apply_deletes_an_unoffered_row_only_with_a_warning_printed(tmp_path):
    """A withheld row named explicitly is the operator's call -- but never silent.

    `would-break` is not refused outright: a fabricated row over a genuinely
    short real deposit is a real case, and an operator with the chain in front
    of them may know something the rows do not carry. What must not happen is
    that it goes through with nothing said.
    """
    path = affected_db(tmp_path)
    result = run_script(["--db", str(path), "--run-dir", str(tmp_path / "run"), "--apply", "--delete-event-id", "3"])
    assert result.returncode == 0, result.stderr
    assert "WARNING: event_id 3 was NAMED but was NOT offered" in result.stdout
    assert "under-crediting" in result.stdout
    assert "your call and it is being carried out" in result.stdout

    conn = sqlite3.connect(path)
    assert conn.execute("SELECT 1 FROM deposit_events WHERE id = 3").fetchone() is None
    conn.close()


def test_apply_against_a_nonexistent_database_refuses_rather_than_creating_one(tmp_path):
    missing = tmp_path / "nope.db"
    result = run_script(["--db", str(missing), "--run-dir", str(tmp_path / "run"), "--apply", "--delete-event-id", "1"])
    assert result.returncode == 2
    assert "will not create a database" in result.stderr
    assert not missing.exists()


def test_apply_refuses_while_a_supervised_worker_is_running(tmp_path):
    """A deposit_watcher poll re-INSERTS the row, so the delete would look like it worked.

    The pid file is written with THIS process's pid and command line, because
    supervisor.pid_is_still_ours() checks /proc/<pid>/cmdline against the
    recorded command -- a made-up pid would be reported as stale, which is the
    correct behavior and would test nothing.
    """
    path = affected_db(tmp_path)
    before = fingerprint(path)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    command = Path("/proc/self/cmdline").read_bytes().replace(b"\x00", b" ").decode().strip()
    (run_dir / "deposit_watcher.pid").write_text(f"{os.getpid()}\n{command}\n", encoding="utf-8")

    result = run_script(["--db", str(path), "--run-dir", str(run_dir), "--apply", "--delete-event-id", "1"])
    assert result.returncode == 2
    assert "deposit_watcher" in result.stderr
    assert "re-INSERT" in result.stderr
    assert fingerprint(path) == before


def test_the_backup_name_ends_in_db_so_gitignore_blocks_it():
    """Rule 2: this repository's backup habit is what leaked a live RPC password."""
    assert backup_path_for(Path("/x/swap_terminal.db"), "20260925T120000Z").name.endswith(".db")
    assert "pre-vout-migration" in backup_path_for(Path("/x/swap_terminal.db"), "20260925T120000Z").name


# --------------------------------------------------------------------------
# 7. The refusal decision, called directly
# --------------------------------------------------------------------------


def test_refusal_for_accepts_only_a_reported_vout_zero_row_on_a_live_swap():
    rows = rows_for([(0, 1.5), (2, 1.5)])
    assessment = assess_swap(rows, 0.01)
    assert refusal_for(1, (rows[0], assessment)) is None
    assert "not one of the rows reported" in refusal_for(99, None)
    assert "vout=2, not 0" in refusal_for(2, (rows[1], assessment))

    settled_rows = rows_for([(0, 1.5), (2, 1.5)])
    for row in settled_rows:
        row["swap_status"] = "failed"
    settled = assess_swap(settled_rows, 0.01)
    assert "is failed" in refusal_for(1, (settled_rows[0], settled))


def test_deletion_plan_raises_rather_than_returning_a_partial_plan():
    """A plan with the bad ids quietly dropped is how the wrong rows get deleted."""
    rows = rows_for([(0, 1.5), (2, 1.5)])
    assessment = assess_swap(rows, 0.01)
    with pytest.raises(MigrationRefused, match="NOTHING was written"):
        deletion_plan([assessment], [1, 2])


def test_main_returns_two_on_a_refusal_rather_than_raising(tmp_path, capsys):
    """A refusal is a sentence the operator reads, not a traceback they cannot act on.

    --run-dir is passed explicitly so this cannot accidentally read the real
    swap_terminal/runtime/ and refuse for a different reason than the one it
    means to assert.
    """
    missing = tmp_path / "x.db"
    assert main(["--db", str(missing), "--run-dir", str(tmp_path / "run"), "--apply", "--delete-event-id", "1"]) == 2
    captured = capsys.readouterr()
    assert "REFUSED" in captured.err
    assert "will not create a database" in captured.err
