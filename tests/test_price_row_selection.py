"""The price-row decision, called with seeded inputs (rules 10 and 12).

Role: test (read-only)
Reads: swap_terminal/transactions.py
Writes: nothing
Can move funds: no. transactions.py issues exactly one RPC method,
        `listtransactions`, and nothing here reaches even that: the two
        functions under test are pure.
Mainnet-safe: yes

WHY THESE FUNCTIONS EXIST SEPARATELY AT ALL.

`fetch_grc_price_from_cryptocompare()` was over the C901 complexity ceiling,
and CLAUDE.md rule 12 says what that means: "a function past the ceiling is
orchestration that has swallowed decisions ... the fix is to extract the
decision so it can be called with seeded inputs, not to raise the ceiling."
It was building request parameters, making an HTTP call, running a circuit
breaker, parsing a payload, CHOOSING A ROW and CHOOSING A PRICE, all in one
body -- so the only way to exercise the last two was to call CryptoCompare.

select_cryptocompare_row() and usable_close_or_open() are those two decisions,
extracted, and this file is the thing that became possible afterwards.

HOW THIS FILE IMPORTS A TKINTER MODULE WITHOUT TKINTER.

transactions.py is a Tk application and `import tkinter as tk` is at its top,
but tkinter is a system package rather than a pip one and is absent here. The
conftest-style stub below puts a placeholder in sys.modules before the import.
That is not a reimplementation of anything under test: the functions being
called are the real ones from the real file, and neither of them references tk.
"""

import sys
import types
from datetime import UTC, date, datetime

import pytest

# transactions.py imports pandas and yfinance at module scope and neither is in
# requirements.txt, which lists exactly Flask and requests. Skip rather than
# error on a machine that has the declared requirements and nothing else -- an
# undeclared dependency is a finding for the report, not a reason for the suite
# to go red on a correct install.
pytest.importorskip("pandas", reason="transactions.py imports pandas; it is not in requirements.txt")
pytest.importorskip("yfinance", reason="transactions.py imports yfinance; it is not in requirements.txt")

# Stub tkinter before importing transactions.py, and the stub is EMPTY on
# purpose.
#
# IT USED TO CARRY TWELVE NAMES -- Tk, Label, Listbox, Button, StringVar, X,
# BOTH, END, LEFT, DISABLED, NORMAL and a `tkinter.messagebox` submodule -- and
# every one of them was dead. MEASURED 2026-10-09 rather than reasoned about: a
# fresh interpreter with `sys.modules["tkinter"] = types.ModuleType("tkinter")`
# and nothing else imports transactions.py and runs both functions under test.
# The reason is readable off the file: transactions.py has `import tkinter as
# tk` and no `from tkinter import ...`, and every `tk.<name>` reference sits
# inside TxViewerApp's methods or main(), so not one is evaluated at import.
# There is no messagebox import in that file at all.
#
# The names mattered because they were WRONG to carry, not merely surplus. A
# stub that declares a surface nobody touches is read as a description of what
# the module under test needs (rule 9: dead code gets read, greped past and
# copied from), and the next person adding a GUI test would have copied eleven
# placeholders that have never once been looked up.
#
# tests/test_transactions_announces_its_chain.py stubs the same module the same
# way, for the same file, and already did it with bare modules; named here so a
# reader who finds one knows the other exists (rule 8).
#
# THE `if` GUARD RATHER THAN sys.modules.setdefault(), which is the same
# operation in one line and is what that sibling file uses. Ruff's E402 treats
# an `if` block as allowed preamble before an import -- the conditional-import
# idiom -- and a bare call statement as not, so the one-liner costs an E402 on
# the `from transactions import ...` below and a `noqa` to answer it. Measured
# 2026-10-09 on both forms. A suppression to buy a shorter line is the trade
# rule 19 refuses, so the block stays a block.
if "tkinter" not in sys.modules:
    sys.modules["tkinter"] = types.ModuleType("tkinter")

from transactions import (
    select_cryptocompare_row,
    usable_close_or_open,
)

TARGET = date(2026, 3, 15)


def _row(day: date, **fields):
    """A CryptoCompare histoday row, as the API returns them."""
    stamp = int(datetime(day.year, day.month, day.day, 12, 0, tzinfo=UTC).timestamp())
    return {"time": stamp, **fields}


# --- select_cryptocompare_row ------------------------------------------------


def test_it_picks_the_latest_row_at_or_before_the_target():
    rows = [
        _row(date(2026, 3, 13), close=1.0),
        _row(date(2026, 3, 14), close=2.0),
        _row(date(2026, 3, 16), close=3.0),  # after the target: must not win
    ]
    picked = select_cryptocompare_row(rows, TARGET)
    assert picked is not None, "the 3-14 row is at or before the target and had to be selected"
    assert picked["close"] == 2.0


def test_a_row_exactly_on_the_target_date_is_eligible():
    rows = [_row(date(2026, 3, 14), close=1.0), _row(TARGET, close=9.0)]
    picked = select_cryptocompare_row(rows, TARGET)
    assert picked is not None, "a row ON the target date is eligible and had to be selected"
    assert picked["close"] == 9.0


def test_rows_entirely_after_the_target_select_nothing():
    """None, not the nearest row. A price from the future is not a price for
    the date being priced, and silently using one would date-shift a whole
    transaction history by a day."""
    rows = [_row(date(2026, 3, 16), close=1.0), _row(date(2026, 3, 17), close=2.0)]
    assert select_cryptocompare_row(rows, TARGET) is None


def test_rows_with_a_non_positive_timestamp_are_skipped_not_trusted():
    rows = [_row(date(2026, 3, 14), close=5.0), {"time": 0, "close": 99.0}]
    picked = select_cryptocompare_row(rows, TARGET)
    assert picked is not None, "skipping the time=0 row must not cost the good row beside it"
    assert picked["close"] == 5.0
    rows = [{"time": -1, "close": 99.0}]
    assert select_cryptocompare_row(rows, TARGET) is None


@pytest.mark.parametrize("junk", [None, {}, "rows", 7, []])
def test_anything_that_is_not_a_list_of_rows_selects_nothing(junk):
    assert select_cryptocompare_row(junk, TARGET) is None


def test_non_dict_entries_inside_the_list_are_skipped():
    rows = ["nonsense", None, _row(date(2026, 3, 14), close=4.0)]
    picked = select_cryptocompare_row(rows, TARGET)
    assert picked is not None, "skipping the non-dict entries must not cost the real row beside them"
    assert picked["close"] == 4.0


# --- usable_close_or_open ----------------------------------------------------


def test_the_close_wins_when_it_is_positive():
    assert usable_close_or_open({"close": 0.0042, "open": 0.0039}) == 0.0042


def test_a_zero_close_falls_back_to_the_open():
    assert usable_close_or_open({"close": 0, "open": 0.0039}) == 0.0039


def test_a_missing_close_falls_back_to_the_open():
    assert usable_close_or_open({"open": 0.0039}) == 0.0039


def test_no_usable_price_is_none_and_never_zero():
    """The point of the whole function.

    On a price path a zero is not a cheap asset, it is a missing measurement.
    Returning 0.0 would be multiplied by an amount and recorded as a USD value
    of nothing, which is indistinguishable from a real valuation -- rule 12's
    "the caller cannot tell the failure from a real answer".
    """
    for row in ({"close": 0, "open": 0}, {}, {"close": None, "open": None}, {"close": -1, "open": -2}):
        assert usable_close_or_open(row) is None
