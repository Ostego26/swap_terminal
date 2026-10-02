"""The report that shows money nobody can claim.

Behavioral, against the real SCHEMA and the real entry point: seed rows, drive main()
with argv, assert on what reaches stdout (BEHAVIORAL_VERIFICATION_PRINCIPLE).

main(argv) IS DRIVEN RATHER THAN run() OR THE FORMATTERS, because three mutations in
this session survived a correct function whose CALL SITE discarded the result --
describe_wallet_lock's can_unlock hardcoded at its call site, and sender_refusal's
call deleted from main() outright. A formatter asserted on directly cannot catch
either.

WHY THIS FILE EXISTS AT ALL, which is a mistake rather than a feature: on 2026-10-02
nothing in the tree could list unattributable_deposits, so I wrote the operator a
SELECT by hand and spelled `why` as `reason`. It raised OperationalError against the
one table in this schema whose rows are somebody's money. The tool replaces the
hand-written query; these tests are what make the tool trustworthy enough to replace
it.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "swap_terminal"))

from db import SCHEMA, connect_db  # noqa: E402

import show_unattributable  # noqa: E402
from swap_terminal.services.unattributable_deposit_service import (  # noqa: E402
    StrandedDeposit,
    outstanding,
    record,
    resolve_credited,
    unattributable_txids,
)

#: The operator's two real stranded devnet signatures, the ones that were burning a
#: getTransaction per cycle against an endpoint answering 429. Real signatures rather
#: than generated ones: two earlier seeds in this suite were invalid base58 because
#: they were built with f"{n:02d}" and `0` is not in the alphabet.
STRANDED_A = (
    "5rHDrJYpVRBWPjGvmTCMkqF8KaKMscFVBF7YHxNqfmVHvJeDeZqHkfBHQbyRAgQSJQJ4"
    "6YMtPbPQGBCNRQ1GQRAV"
)
STRANDED_B = (
    "61otPXfyjvVHYxFJwPPcFhqkVPpjQKvmrfpdFqN2rCQsNDLYcQi8ePVRGPvB3xrYEzBJ"
    "QvYEfHcV4HrjzpnYPDWF"
)
ACCOUNT = "CUBnQ5QBfYkL71TCqSdecAQ9xjfGmAdu6Hs3fjQeLorp"
NOW = "2026-10-01T04:00:00Z"
LATER = "2026-10-02T04:00:00Z"


@pytest.fixture
def db_file(tmp_path):
    """A real database on the real SCHEMA, at a real path the tool can be pointed at."""
    path = tmp_path / "swap_terminal_test.db"
    conn = connect_db(str(path))
    conn.executescript(SCHEMA)
    conn.commit()
    conn.close()
    return path


def seed(db_file, rows, *, now=NOW):
    conn = connect_db(str(db_file))
    record(conn, rows, now=now)
    conn.commit()
    conn.close()


def no_reference(txid, *, amount=0.25):
    return StrandedDeposit(
        asset="SOL",
        txid=txid,
        address=ACCOUNT,
        amount=amount,
        credits=1,
        why="no memo instruction, so nothing identifies the sender",
        confirmations=32,
        discriminator=None,
    )


def wrong_reference(txid, *, discriminator=4242):
    return StrandedDeposit(
        asset="SOL",
        txid=txid,
        address=ACCOUNT,
        amount=1.5,
        credits=2,
        why="memo discriminator matched no open swap",
        confirmations=15,
        discriminator=discriminator,
    )


# --- the query ----------------------------------------------------------------


def test_the_column_is_why_and_a_wrong_name_RAISES_rather_than_reading_as_empty(db_file):
    """The exact mistake that produced this tool, pinned so the tool cannot repeat it.

    `reason` is not a column; `why` is. The hand-written query raised
    OperationalError, which was the GOOD outcome -- it was loud. The bad outcome
    available in that situation is a query that returns nothing and reads as "no
    unclaimed deposits", because that is indistinguishable from the true empty case
    and the operator stops looking.

    So this asserts both halves: the real SQL names `why` and returns the row, and a
    query against the name I guessed raises instead of answering emptily.
    """
    seed(db_file, [no_reference(STRANDED_A)])
    conn = connect_db(str(db_file))
    rows = outstanding(conn, "SOL")
    assert len(rows) == 1
    assert rows[0]["why"] == "no memo instruction, so nothing identifies the sender"
    with pytest.raises(sqlite3.OperationalError, match="reason"):
        conn.execute("SELECT reason FROM unattributable_deposits").fetchall()


def test_outstanding_is_the_default_and_resolved_rows_are_hidden(db_file):
    seed(db_file, [no_reference(STRANDED_A), wrong_reference(STRANDED_B)])
    conn = connect_db(str(db_file))
    resolve_credited(conn, "SOL", [STRANDED_A], now=LATER)
    conn.commit()
    assert [row["txid"] for row in outstanding(conn, "SOL")] == [STRANDED_B]


def test_include_resolved_shows_BOTH_and_marks_which(db_file):
    seed(db_file, [no_reference(STRANDED_A), wrong_reference(STRANDED_B)])
    conn = connect_db(str(db_file))
    resolve_credited(conn, "SOL", [STRANDED_A], now=LATER)
    conn.commit()
    rows = outstanding(conn, "SOL", include_resolved=True)
    flags = {row["txid"]: row["outstanding"] for row in rows}
    assert flags == {STRANDED_A: 0, STRANDED_B: 1}


def test_the_default_is_the_OPPOSITE_of_the_scanners_skip_set(db_file):
    """Same table, two questions, deliberately different defaults -- stated at both sites.

    outstanding() hides resolved rows: a dealt-with deposit is history. 
    unattributable_txids() INCLUDES them: there the question is "may the scanner skip
    this", and a human having dealt with it is a stronger yes than an open row. A
    reader who found one of these and assumed the other would either re-open the 429
    leak or hide outstanding money.
    """
    seed(db_file, [no_reference(STRANDED_A), wrong_reference(STRANDED_B)])
    conn = connect_db(str(db_file))
    resolve_credited(conn, "SOL", [STRANDED_A], now=LATER)
    conn.commit()
    assert [row["txid"] for row in outstanding(conn, "SOL")] == [STRANDED_B]
    assert unattributable_txids(conn, "SOL") == frozenset({STRANDED_A, STRANDED_B})


def test_the_asset_filter_is_per_asset(db_file):
    seed(db_file, [no_reference(STRANDED_A)])
    conn = connect_db(str(db_file))
    assert outstanding(conn, "XRP") == []
    assert len(outstanding(conn, None)) == 1, "and no filter means every asset"


def test_the_oldest_deposit_is_FIRST(db_file):
    """Somebody has been waiting longest on the top row, which is why the index exists."""
    seed(db_file, [wrong_reference(STRANDED_B)], now=LATER)
    seed(db_file, [no_reference(STRANDED_A)], now=NOW)
    conn = connect_db(str(db_file))
    assert [row["txid"] for row in outstanding(conn, "SOL")] == [STRANDED_A, STRANDED_B]


# --- the report ---------------------------------------------------------------


def test_the_report_names_the_database_and_where_the_path_came_from(db_file, capsys):
    seed(db_file, [no_reference(STRANDED_A)])
    assert show_unattributable.main(["--db", str(db_file)]) == 0
    out = capsys.readouterr().out
    assert str(db_file) in out
    assert "<-" in out, "and says WHERE the path came from, not just what it is"


def test_a_deposit_with_NO_reference_reads_differently_from_one_with_a_wrong_reference(
    db_file, capsys
):
    """Two different support conversations, and the schema makes the column nullable
    for exactly this reason. An operator who cannot tell them apart has to open the
    chain to find out which one they are having."""
    seed(db_file, [no_reference(STRANDED_A)])
    assert show_unattributable.main(["--db", str(db_file)]) == 0
    without = capsys.readouterr().out
    assert "(none)" in without
    assert "no memo" in without

    seed(db_file, [wrong_reference(STRANDED_B)])
    assert show_unattributable.main(["--db", str(db_file), "--asset", "SOL"]) == 0
    with_wrong = capsys.readouterr().out
    assert "4242" in with_wrong
    assert "matched no open swap" in with_wrong


def test_the_report_shows_the_amount_the_account_and_the_credit_count(db_file, capsys):
    """Everything a human matching this by hand needs, which is why the row carries it."""
    seed(db_file, [wrong_reference(STRANDED_B)])
    assert show_unattributable.main(["--db", str(db_file)]) == 0
    out = capsys.readouterr().out
    assert "1.5" in out
    assert "2 credit(s)" in out
    assert ACCOUNT in out
    assert STRANDED_B in out, "the full signature, so it can be pasted into an explorer"


def test_an_outstanding_row_SAYS_nobody_has_been_given_the_coins(db_file, capsys):
    seed(db_file, [no_reference(STRANDED_A)])
    assert show_unattributable.main(["--db", str(db_file)]) == 0
    out = capsys.readouterr().out
    assert "OUTSTANDING" in out
    assert "nobody has been given these coins" in out


def test_an_EMPTY_report_prints_none_and_not_a_blank_gap(db_file, capsys):
    """Rule 14. A blank gap is ambiguous between zero rows and a query that broke --
    and a broken query about this table is the thing that happened."""
    assert show_unattributable.main(["--db", str(db_file)]) == 0
    out = capsys.readouterr().out
    assert "(none)" in out
    assert "reached a swap" in out, "and says what zero MEANS, next to the zero"


def test_the_empty_report_echoes_the_filter_that_produced_it(db_file, capsys):
    """An empty --asset=XRP run and an empty whole-table run are different facts."""
    seed(db_file, [no_reference(STRANDED_A)])
    assert show_unattributable.main(["--db", str(db_file), "--asset", "XRP"]) == 0
    out = capsys.readouterr().out
    assert "(none)" in out
    assert "XRP" in out, "so the reader knows SOL rows were filtered out, not absent"


def test_the_count_carries_its_denominator_and_the_per_asset_breakdown(db_file, capsys):
    seed(db_file, [no_reference(STRANDED_A), wrong_reference(STRANDED_B)])
    assert show_unattributable.main(["--db", str(db_file)]) == 0
    out = capsys.readouterr().out
    assert "SOL 2" in out
    assert "2 of 2" in out, "rule 3: a count without what it was counted out of"


def test_the_totals_point_at_the_LIVE_counter_that_proves_the_skip_set_works(db_file, capsys):
    """The report and the worker log have to be connectable, because the whole point of
    the 2026-10-02 fix is that these rows stop costing a getTransaction per cycle. An
    operator reading `outstanding 2` needs to know the log should say `did not re-read 2`.
    """
    seed(db_file, [no_reference(STRANDED_A), wrong_reference(STRANDED_B)])
    assert show_unattributable.main(["--db", str(db_file)]) == 0
    out = capsys.readouterr().out
    assert "did not re-read" in out


def test_a_missing_database_is_REFUSED_and_nothing_is_created(db_file, capsys):
    """connect() CREATES the file, so a read-only tool must check first. A new empty
    database left on disk is a write, and the next reader finds tables missing with no
    explanation."""
    absent = db_file.parent / "not_there.db"
    assert show_unattributable.main(["--db", str(absent)]) == 2
    assert not absent.exists(), "the refusal must not have created what it refused to read"
    assert "REFUSED" in capsys.readouterr().err


def test_a_database_without_the_table_is_REFUSED_and_not_reported_as_empty(tmp_path, capsys):
    """An older database is "I cannot tell you", never "there are none". This is the
    same failure the wrong column name would have had if it had returned empty instead
    of raising."""
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE unrelated (id INTEGER)")
    conn.commit()
    conn.close()
    assert show_unattributable.main(["--db", str(path)]) == 2
    err = capsys.readouterr().err
    assert "REFUSED" in err
    assert "not that there are no unclaimed deposits" in err


def test_resolved_rows_show_their_note_under_include_resolved(db_file, capsys):
    seed(db_file, [no_reference(STRANDED_A)])
    conn = connect_db(str(db_file))
    resolve_credited(conn, "SOL", [STRANDED_A], now=LATER)
    conn.commit()
    conn.close()
    assert show_unattributable.main(["--db", str(db_file), "--include-resolved"]) == 0
    out = capsys.readouterr().out
    assert "RESOLVED" in out
    assert LATER in out
