"""The root tool for `late_deposits`, exercised by running it. Read-only.

Role: test (submodule -> function)
Reads: a temporary swap_terminal.db built from db.SCHEMA, and show_late_deposits.py
Writes: only inside tmp_path
Can move funds: no
Mainnet-safe: yes

BEHAVIOR, NOT TEXT. Per the repository's behavioral-verification principle, each
test seeds real rows into the real `late_deposits` table, runs the real tool as a
subprocess, and asserts on what appears on stdout. None of them matches the SQL
or re-implements the filter.

Three of these pin defects the tool SHIPPED WITH and that only running it found:

  test_the_total_label_fits_the_column       printed `late deposits shown3`, the
                                             count welded to a 19-character label
                                             in a 16-wide field
  test_empty_run_does_not_claim_anything_
      about_resolved_rows                    said "outstanding or not" on a run
                                             that queried outstanding only
  test_the_outstanding_sentence_does_not_
      promise_the_count_will_fall            reused late_deposit_service.late_note(),
                                             whose sentence describes rows NEW THIS
                                             PASS and says the figure "returns to 0
                                             once each is recorded" -- false of a
                                             figure that only moves when a human
                                             resolves a row
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
TOOL = REPO_ROOT / "show_late_deposits.py"

sys.path.insert(0, str(REPO_ROOT / "swap_terminal"))

from db import SCHEMA, connect_db  # noqa: E402  -- after the sys.path.insert above

# WRITTEN OUT RATHER THAN BUILT FROM A COLUMN LIST. An f-string carrying the column
# names would be flagged S608, and the honest `noqa` for it reads "identifier, not
# input" -- true, but rule 19 says a suppression is not a way to make a check pass
# when the alternative is simply correct. A literal statement needs no claim.
_INSERT_QUOTE = """
INSERT INTO quotes (id, from_asset, to_asset, input_amount, quoted_rate, fee_bps,
                    network_fee_reserve, output_amount_estimate, expires_at, created_at)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""
_INSERT_SWAP = """
INSERT INTO swaps (id, quote_id, from_asset, to_asset, deposit_address, payout_address,
                   expected_input_amount, quoted_rate, fee_bps, network_fee_reserve,
                   output_amount_estimate, status, min_confirmations, created_at,
                   updated_at, expires_at)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""
_INSERT_LATE = """
INSERT INTO late_deposits (swap_id, swap_status, asset, txid, vout, address, amount,
                           confirmations, first_seen_at, last_seen_at, resolved_at,
                           resolution_note)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""


def _seed(db_path: Path, late_rows):
    """A real database with real constraints, built from db.SCHEMA.

    quotes BEFORE swaps because swaps.quote_id is a FOREIGN KEY and the schema
    turns enforcement on -- which is the point of seeding against the real schema
    rather than a hand-made table: a tool that only works where constraints are
    off is not tested.
    """
    db = connect_db(str(db_path))
    db.executescript(SCHEMA)
    rate, fee_bps, reserve, estimate = 1000.0, 150, 0.0, 98.5
    when = "2026-10-04T00:00:00+00:00"
    quotes, swaps, seen = [], [], set()
    for swap_id, status, asset, address in {
        (row[0], row[1], row[2], row[5]) for row in late_rows
    }:
        if swap_id in seen:
            continue
        seen.add(swap_id)
        quotes.append(
            (f"q_{swap_id}", asset, "GRC", 0.01, rate, fee_bps, reserve, estimate,
             "2026-10-04T01:00:00+00:00", when)
        )
        swaps.append(
            (swap_id, f"q_{swap_id}", asset, "GRC", address, "mPAYOUT", 0.01, rate,
             fee_bps, reserve, estimate, status, 2, when, when,
             "2026-10-04T01:00:00+00:00")
        )
    db.executemany(_INSERT_QUOTE, quotes)
    db.executemany(_INSERT_SWAP, swaps)
    db.executemany(_INSERT_LATE, late_rows)
    db.commit()
    db.close()


def _run(db_path: Path, *extra):
    result = subprocess.run(
        [sys.executable, str(TOOL), "--db", str(db_path), *extra],
        capture_output=True,
        text=True,
        check=False,
    )
    return result


_UNRESOLVED_BTC = (
    "s_done", "completed", "BTC", "a" * 64, 1, "bcrt1qdone", 0.0001, 3,
    "2026-10-04T16:20:00+00:00", "2026-10-04T16:25:00+00:00", None, None,
)
_UNRESOLVED_BTC_BIG = (
    "s_done", "completed", "BTC", "b" * 64, 0, "bcrt1qdone", 0.02, 5,
    "2026-10-04T17:00:00+00:00", "2026-10-04T17:05:00+00:00", None, None,
)
_UNRESOLVED_LTC_FAILED = (
    "s_fail", "failed", "LTC", "c" * 64, 1, "rltc1qfail", 0.01, 2,
    "2026-10-04T18:00:00+00:00", "2026-10-04T18:05:00+00:00", None, None,
)
_RESOLVED_BTC = (
    "s_done", "completed", "BTC", "d" * 64, 1, "bcrt1qdone", 0.5, 9,
    "2026-10-03T10:00:00+00:00", "2026-10-03T10:05:00+00:00",
    "2026-10-03T12:00:00+00:00", "refunded by hand",
)


@pytest.fixture
def seeded(tmp_path):
    db_path = tmp_path / "swap_terminal.db"
    _seed(
        db_path,
        [_UNRESOLVED_BTC, _UNRESOLVED_BTC_BIG, _UNRESOLVED_LTC_FAILED, _RESOLVED_BTC],
    )
    return db_path


def test_unresolved_rows_are_listed_and_resolved_ones_are_not(seeded):
    out = _run(seeded).stdout
    assert "a" * 64 in out
    assert "c" * 64 in out
    # The resolved row is history, and the default view is money still held.
    assert "d" * 64 not in out


def test_include_resolved_adds_the_resolved_row_and_marks_it(seeded):
    out = _run(seeded, "--include-resolved").stdout
    assert "d" * 64 in out
    assert "refunded by hand" in out
    assert "4" in out


def test_asset_filter_excludes_the_other_chain(seeded):
    out = _run(seeded, "--asset", "LTC").stdout
    assert "c" * 64 in out
    assert "a" * 64 not in out


def test_a_completed_swap_and_a_failed_one_read_differently(seeded):
    """The status is what decides the action, so it must not print as a bare token.

    completed -> the desk holds an extra send. failed -> it may still owe the
    original payout too. An operator reading one word has to go and look that up.
    """
    out = _run(seeded).stdout
    assert "its payout already went out" in out
    assert "may owe the original too" in out


def test_the_total_label_fits_the_column(seeded):
    """SHIPPED DEFECT: `late deposits shown3`, a 19-char label in a 16-wide field.

    report_block.labeled() pads rather than truncates -- correct, since truncating
    a label loses meaning -- so the caller owes it a label that fits. Asserted as
    "there is whitespace between the label and the digit", which is the thing a
    reader needs, rather than by matching the label text.
    """
    out = _run(seeded).stdout
    # next(..., None) RATHER THAN A BARE next(). Mutation-checked 2026-10-05 by
    # restoring the 19-character label: the bare form raised StopIteration, so the
    # test failed with a traceback instead of with the sentence that says what is
    # wrong. A test whose failure does not explain itself costs the reader the same
    # investigation the defect would have.
    total = next((line for line in out.splitlines() if "rows shown" in line), None)
    assert total is not None, (
        "no 'rows shown' line in the report. Either the total is gone, or its label "
        f"no longer fits report_block.LABEL_WIDTH and has absorbed the count:\n{out}"
    )
    _label, _, value = total.strip().partition("rows shown")
    assert value.startswith(" "), f"label and count are welded together: {total!r}"
    assert value.strip().startswith("3")


def test_empty_run_does_not_claim_anything_about_resolved_rows(tmp_path):
    """SHIPPED DEFECT, copied from show_unattributable.py: ", outstanding or not".

    Without --include-resolved the SELECT filters on `resolved_at IS NULL`, so it
    never looked at resolved rows. Saying "outstanding or not" asserts a fact about
    data the query excluded.
    """
    db_path = tmp_path / "swap_terminal.db"
    _seed(db_path, [_RESOLVED_BTC])
    out = _run(db_path).stdout
    assert "(none)" in out
    assert "outstanding or not" not in out
    assert "--include-resolved" in out, "must say what was not looked at"


def test_empty_run_with_include_resolved_may_speak_for_both(tmp_path):
    db_path = tmp_path / "swap_terminal.db"
    _seed(db_path, [])
    out = _run(db_path, "--include-resolved").stdout
    assert "(none)" in out
    assert "outstanding or resolved" in out


def test_the_outstanding_sentence_does_not_promise_the_count_will_fall(seeded):
    """SHIPPED DEFECT: late_deposit_service.late_note() was printed here.

    Its sentence describes the CYCLE's number -- rows new this pass -- and says the
    figure "returns to 0 once each is recorded". This tool's number is every row
    still outstanding and only moves when a human resolves one, so the borrowed
    sentence was false on an operator's screen.
    """
    out = _run(seeded).stdout
    assert "returns to 0" not in out
    assert "NEW this pass" not in out
    assert "does NOT fall on its own" in out


def test_a_missing_database_is_refused_and_nothing_is_created(tmp_path):
    """sqlite3.connect() CREATES the file, so the guard must precede it.

    A read-only tool that leaves an empty database behind has written something
    while announcing it would not, and the next reader finds tables missing with no
    explanation.
    """
    absent = tmp_path / "never" / "swap_terminal.db"
    result = _run(absent)
    assert result.returncode == 2
    assert "REFUSED" in result.stderr
    assert not absent.exists()
    assert not absent.parent.exists()


def test_the_report_echoes_the_database_it_read(seeded):
    """Rule 14: pasted output is read a day later and must say which file it is about."""
    out = _run(seeded).stdout
    assert str(seeded) in out
