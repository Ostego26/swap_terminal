"""The fee report puts the right facts on a terminal screen, and writes nothing.

Behavioral: seed real rows into the real schema, run the real main() with a real
--db, and assert on the captured stdout and on the database afterward. Nothing
here asserts on the shape of a format string.

WHAT THESE TESTS ARE FOR, beyond "it runs".

Two defects in this report were found by PRINTING IT over a seeded ledger rather
than by reading it, on 2026-10-01, and both are pinned below:

  `drift +0.00000000 GRC`   printed on a ledger where two of four customers were
                            charged the wrong fee, by -99.5bps and +97.5bps. The
                            two cancelled. A net with no magnitude beside it is
                            unreadable as evidence that anything happened.
  `10000.0bps = 150 quoted  three correct terms and a sum wrong by a factor of
   + 20000.0 reserve        two, on the row where max(...,0) clamped the payout.
   +0.0 drift`              A later line admitted it, which is not enough: a
                            column of sums must not contain one that does not add.

A format-string test would have passed on both.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "swap_terminal"))

from db import SCHEMA  # noqa: E402

import show_fees  # noqa: E402
from tests.test_fee_ledger import (  # noqa: E402
    EXPECTED_INPUT,
    QUOTED_RATE,
    RESERVE,
    TOLERANCE_PCT,
    PayoutSeed,
    Seed,
    seed_payout,
    seed_swap,
)


@pytest.fixture
def db_path(tmp_path):
    """A real file, because the tool's first act is Path.exists() on one."""
    path = tmp_path / "swap_terminal.db"
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.executescript(SCHEMA)
    connection.commit()
    connection.close()
    return path


def seed(path, swaps):
    """swaps is (name, actual-input-factor-or-None, expected_input)."""
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    for index, (name, factor, expected) in enumerate(swaps):
        paid = seed_swap(connection, name, Seed(
            expected_input=expected,
            actual_input=None if factor is None else expected * factor,
        ))
        seed_payout(connection, name, paid, PayoutSeed(sent_at=f"2026-10-01T18:{index:02d}:00+00:00"))
    connection.commit()
    connection.close()


def run(path, capsys) -> str:
    assert show_fees.main(["--db", str(path)]) == 0
    return capsys.readouterr().out


# ------------------------------------------------------------------- the empty case


def test_an_empty_database_says_none_and_says_what_none_means(db_path, capsys):
    """Rule 14: "(none)" is a result, and a blank gap is not.

    It also has to distinguish itself from a zero fee, which is the mistake a
    bare "0.0bps" would invite: nothing has been delivered to charge a fee ON.
    """
    out = run(db_path, capsys)
    assert "fees earned: (none)" in out
    assert "NOT the same as a zero fee" in out
    # No totals block and no per-swap block when there is nothing -- and no
    # closing advice either, because there is nothing to advise about.
    assert "fees earned, per destination asset" not in out
    assert "WHAT THESE NUMBERS SAY" not in out


def test_a_missing_database_refuses_rather_than_reporting_no_fees(tmp_path, capsys):
    """And it must not create the file on the way past (the header claims it does not)."""
    missing = tmp_path / "never_created.db"
    assert show_fees.main(["--db", str(missing)]) == 2
    assert not missing.exists(), "a read-only tool created a database file"
    captured = capsys.readouterr()
    assert "REFUSED" in captured.err
    assert "That is not 'no fees were earned'" in captured.err


def test_a_database_with_no_tables_refuses_rather_than_reporting_no_fees(tmp_path, capsys):
    """An uninitialized file must not give the same answer a healthy empty one does."""
    path = tmp_path / "blank.db"
    sqlite3.connect(path).close()
    assert show_fees.main(["--db", str(path)]) == 2
    captured = capsys.readouterr()
    assert "could not be queried" in captured.err
    assert "cannot tell an empty database from a missing table" in captured.err


# --------------------------------------------------------- the two found defects


def test_offsetting_drift_does_not_print_as_no_drift(db_path, capsys):
    """THE FIRST FOUND DEFECT. A net near zero over two wrong fees is not "no drift".

    One short deposit and one long one, equal and opposite. The net cancels; the
    gross line and the count are what tell the operator two customers were charged
    something other than what they were quoted.
    """
    seed(db_path, [
        ("s_low", 1 - TOLERANCE_PCT, EXPECTED_INPUT),
        ("s_high", 1 + TOLERANCE_PCT, EXPECTED_INPUT),
    ])
    out = run(db_path, capsys)

    assert "drift, net      +0.00000000" in out, "the net really does cancel here -- that is the premise"
    assert "drift, gross" in out
    assert "2 of 2 payout(s) were NOT charged the fee they were quoted" in out
    # The guard against the defect coming back in a different wording: a net of
    # zero must never be the ONLY drift figure on the screen.
    assert "READ THIS BESIDE THE NET, NOT INSTEAD OF IT" in out


def test_a_clamped_row_prints_no_decomposition_that_does_not_add_up(db_path, capsys):
    """THE SECOND FOUND DEFECT. No sum on the screen may fail to add.

    A swap smaller than the network fee reserve pays out zero, so the whole gross
    is retained and `retained_bps` is 10000 while the three terms beside it total
    20150. The row now shows the figure alone and says it is not a rate.
    """
    seed(db_path, [("s_clamped", None, RESERVE / QUOTED_RATE / 2)])
    out = run(db_path, capsys)

    assert "10000.0bps  <- NOT a fee rate" in out
    assert "quoted + " not in out, "a clamped row must not print the decomposition at all"
    assert "DOES NOT RECONCILE" in out
    assert "max(...,0) clamped it" in out


# ------------------------------------------------------- the numbers on the screen


def test_the_realized_fee_and_the_scheduled_fee_are_both_printed(db_path, capsys):
    """The whole point of the report: the two are not the same number.

    Asserted as the printed pair rather than as a computation, because the failure
    this guards is a report that shows only the schedule -- which is what every
    other surface in the tree showed before this file existed.
    """
    seed(db_path, [("s_mid", None, EXPECTED_INPUT), ("s_high", 1 + TOLERANCE_PCT, EXPECTED_INPUT)])
    out = run(db_path, capsys)

    assert "realized fee " in out
    assert "scheduled fee   150.0bps" in out
    assert "248.6bps" in out, "the per-swap realized fee for a 1.01x deposit"
    assert "deposit was 1.0100x the quoted amount" in out


def test_the_parameters_that_decide_every_number_are_echoed_first(db_path, capsys):
    """Rule 14: announce before, not only after; a pasted block is read a day later.

    The database, the current schedule and the tolerance. The tolerance especially:
    without it no `drift` figure below can be interpreted.
    """
    out = run(db_path, capsys)
    first = out.split("\n\n")[0]
    assert str(db_path) in first
    assert "DEFAULT_FEE_BPS=150bps" in first
    assert "AMOUNT_TOLERANCE_PCT=0.01" in first
    assert "READ-ONLY" in first


def test_an_excluded_payout_is_explained_rather_than_silently_dropped(db_path, capsys):
    """The `counted` line says which rows are in, so an absence is readable."""
    out = run(db_path, capsys)
    assert "status='broadcast' AND a txid" in out
    assert "delivery is NOT established and is excluded" in out


def test_timings_are_in_microfortnights_with_the_seconds_beside_them(db_path, capsys):
    """Rule 6: µ and no space, and the seconds in parentheses for the same line."""
    out = run(db_path, capsys)
    assert "µfn" in out
    assert "ufn" not in out, "an ASCII u in displayed output is a defect, not a fallback"
    assert " µfn" not in out, "no space between the number and the unit"


# ------------------------------------------------------------ it changes nothing


def test_the_report_writes_nothing_to_the_database(db_path, capsys):
    """Every table byte-identical afterward, including swap_audit_log."""
    seed(db_path, [("s_one", None, EXPECTED_INPUT)])
    connection = sqlite3.connect(db_path)
    tables = [row[0] for row in connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall()]
    before = {name: connection.execute(f"SELECT * FROM {name}").fetchall() for name in tables}  # noqa: S608
    connection.close()

    run(db_path, capsys)

    connection = sqlite3.connect(db_path)
    after = {name: connection.execute(f"SELECT * FROM {name}").fetchall() for name in tables}  # noqa: S608
    schema_after = [row[0] for row in connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall()]
    connection.close()

    assert before == after
    assert tables == schema_after, "no table or view was created by a read-only report"


def test_it_names_no_command_that_writes(db_path, capsys):
    """Rule 16. The fee rate, the payout arithmetic and fee collection are the
    operator's, and a ready-made setting to paste would be pasted at exactly the
    moment somebody is comparing their fee to a larger exchange's.
    """
    seed(db_path, [("s_one", None, EXPECTED_INPUT)])
    out = run(db_path, capsys)
    for forbidden in ("UPDATE ", "INSERT ", "export DEFAULT_FEE_BPS", "--set", "--collect", "--sweep"):
        assert forbidden not in out.replace("There is no --set, no --collect and no --sweep", ""), (
            f"the report printed {forbidden!r}, which is a way to change money from a read-only tool"
        )
    assert "belong to the operator" in out or "are the operator's" in out


def test_the_reprint_command_carries_the_database_it_was_run_against(db_path, capsys):
    """Otherwise a copied line silently reports on Config.DB_PATH instead.

    show_swap.py and open_swap.py both shipped this defect and both were fixed on
    2026-09-26; this is the same property, pinned before it can be shipped a third
    time.
    """
    seed(db_path, [("s_one", None, EXPECTED_INPUT)])
    out = run(db_path, capsys)
    assert f"show_fees.py --db {db_path}" in out
    assert "<" not in out.split("show_fees.py --db")[1].split("\n")[0], (
        "a placeholder reached a printed command"
    )


def test_the_header_never_claims_an_unset_variable_as_the_source(db_path, capsys, monkeypatch):
    """THE DEFECT THAT REACHED THE OPERATOR, 2026-10-01, from this exact function.

    In a shell with SWAP_DB_PATH unset, this report printed

        database   .../swap_terminal/swap_terminal.db  <- SWAP_DB_PATH

    The path was correct. The provenance was a fabrication: the value came from
    config.DB_PATH's built-in default, and the line asserted it came from an
    environment variable the shell did not have -- about the one parameter that
    decides every other number in the report. The two-case function could not have
    said otherwise; it inferred the source from `db_path != Config.DB_PATH`, so
    "equals the default" read as "came from the environment".

    Now through workers.common.db_path_source(), shared with show_swap.py and
    open_swap.py, which each held their own copy of the same two-case bug.
    """
    monkeypatch.delenv("SWAP_DB_PATH", raising=False)
    assert show_fees.main(["--db", str(db_path)]) == 0
    flagged = capsys.readouterr().out
    assert "<- --db." in flagged
    assert "<- SWAP_DB_PATH." not in flagged

    monkeypatch.delenv("SWAP_DB_PATH", raising=False)
    config = show_fees.get_config_dict()
    from_default = show_fees.header_lines(str(show_fees.Config.DB_PATH), config)
    assert any("IS NOT SET in this shell" in line for line in from_default)
    assert not any("<- SWAP_DB_PATH." in line for line in from_default)

    monkeypatch.setenv("SWAP_DB_PATH", str(show_fees.Config.DB_PATH))
    from_environment = show_fees.header_lines(str(show_fees.Config.DB_PATH), config)
    assert any("<- SWAP_DB_PATH." in line for line in from_environment)


def test_a_row_priced_without_the_reserve_shows_no_reserve_term(db_path, capsys):
    """MUTATION-FOUND. Always printing the term survived the suite.

    "+0.0 reserve" on a row priced without one implies the reserve is still part of
    the fee and merely rounded away, which is the opposite of what changed on
    2026-10-02. On a large swap it rounds to 0.0 and looks harmless; on a 56 GRC
    swap it would print "+1.8 reserve" for an amount the customer was never
    charged, and the decomposition would not add up.
    """
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    paid = seed_swap(connection, "s_new", Seed(expected_input=EXPECTED_INPUT))
    # Priced WITHOUT the reserve: gross * (1 - f), no subtraction.
    without = EXPECTED_INPUT * QUOTED_RATE * (1 - 150 / 10000.0)
    connection.execute("UPDATE swaps SET output_amount_estimate = ? WHERE id = 's_new'", (without,))
    seed_payout(connection, "s_new", without, PayoutSeed(sent_at="2026-10-02T02:00:00+00:00"))
    connection.commit()
    connection.close()

    out = run(db_path, capsys)

    assert "150.0bps  = 150 quoted " in out
    assert "reserve" not in out.split("every delivered payout")[1].split("WHAT THESE NUMBERS SAY")[0], (
        "a row priced without the reserve must not carry a reserve term at all"
    )
    assert "withheld from 0 of 1 payout(s)" in out
    assert paid != without, "the fixture must differ from the old pricing, or this tests nothing"


def test_a_charged_reserve_too_small_to_round_is_never_shown_as_zero(db_path, capsys):
    """MEASURED ON THE OPERATOR'S 2026-10-02 RUN. s_e820c23626002c37 read

        realized  150.0bps = 150 quoted + 0.0 reserve +0.0 drift

    directly above a row with NO reserve term at all. Its reserve was 0.01 GRC on a
    3133 GRC gross -- 0.032bps, rounding away at one decimal. The two states are
    "charged a hundredth of a basis point" and "not charged anything", and
    `+ 0.0 reserve` beside `+0.0 drift` makes them look identical. The PRESENCE of
    the term is the whole signal, and a zero undermines it.
    """
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    # 0.25 SOL at the operator's rate: a gross large enough that 0.01 GRC is
    # 0.032bps. The reserve IS charged, which is what makes a zero wrong.
    big = 3133.59547107 / QUOTED_RATE
    paid = seed_swap(connection, "s_tiny_reserve", Seed(expected_input=big))
    seed_payout(connection, "s_tiny_reserve", paid)
    connection.commit()
    connection.close()

    out = run(db_path, capsys)
    row = out.split("every delivered payout")[1]

    assert "+ <0.1 reserve" in row, "a charged reserve below the display floor must say so, not round to 0"
    assert "+ 0.0 reserve" not in row
    assert "withheld from 1 of 1 payout(s)" in out, "and the asset line must still count it as charged"


def test_the_retained_line_does_not_repeat_the_retracted_justification(db_path, capsys):
    """It said the reserve was "spent on the chain's own fee". It was not.

    From the operator's 2026-10-02 run, AFTER the pricing change shipped: I had
    updated the per-row display and added the `reserve charged` line, and left this
    sentence asserting the very thing both of those exist to correct. The reserve
    was withheld from the customer; the wallet paid the chain separately; the
    remainder was margin. Measured from their own reconciliation to a difference of
    zero.
    """
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    amount = seed_swap(connection, "s_one")
    seed_payout(connection, "s_one", amount)
    connection.commit()
    connection.close()

    out = run(db_path, capsys)
    retained = next(line for line in out.splitlines() if line.strip().startswith("retained")
                    and "received minus paid out" in line)

    assert "spent on the chain's own fee" not in retained, (
        "the retracted claim must be gone from the one line that totals the money"
    )
    assert "never spent on the chain's fee" in retained
    assert "the wallet pays that separately" in retained
    assert "predate 2026-10-02" in retained, (
        "and it must say the reserve is in this total for only SOME rows"
    )
