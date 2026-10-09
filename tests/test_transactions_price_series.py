"""The price-series decisions in transactions.py, called with seeded inputs.

Role: test (read-only)
Reads: swap_terminal/transactions.py
Writes: nothing
Can move funds: no. transactions.py issues exactly one RPC method,
        `listtransactions`, and nothing here reaches even that: every function
        under test is pure, and the one that is not
        (fetch_grc_price_from_yahoo) has its only network call -- the yfinance
        download behind load_full_grc_data() -- replaced by a seeded frame.
Mainnet-safe: yes

WHY THIS FILE EXISTS.

A pyright 1.1.414 run on 2026-10-09 reported 65 findings in transactions.py,
and 60 of them were ONE gap reported twice: `extract_close_series()` contained
the same three lines, written out once per input shape --

    series = pd.to_numeric(series, errors="coerce").dropna()
    series.index = pd.to_datetime(series.index).normalize()
    return series

-- and `pandas.to_numeric` carries no return annotation in pandas 3.0.6, so
each copy lost the Series type and every statement after it was checked
against the twenty-member union inferred from that function's body.

Collapsing the two copies into `numeric_series_on_dates()` is a rule 8 merge,
and rule 8 is specific about the risk it is removing: two copies of one rule
agree on the day they are written and drift from then on, invisibly, because
each looks correct in its own file. The invariant that catches that drift is
not "each branch works" -- it is "both branches answer the same way for the
same data", and that is what
test_a_series_input_and_a_frame_input_agree_exactly() pins. It would have
failed for a drifted pair and passed for either copy alone.

The second thing here is a real defect rather than a type, and it is the only
part worth reading quickly: `as_timestamp()` was annotated `-> pd.Timestamp`
and returned NaT. See test_as_timestamp_refuses_a_date_that_is_not_one.

HOW THIS IMPORTS A TKINTER MODULE WITHOUT TKINTER.

transactions.py is a Tk application and `import tkinter as tk` is line 49, but
tkinter is a system package rather than a pip one and is absent on this host
(checked: `python3 -c "import tkinter"` is ModuleNotFoundError). Nothing below
constructs the GUI class and nothing in transactions.py touches `tk` at module
scope, so an empty placeholder module is enough.

That placeholder is the THIRD copy of this scaffolding in tests/ --
tests/test_transactions_announces_its_chain.py has the same two lines and
tests/test_price_row_selection.py has a longer version that fills in the
specific widget names. Three copies of one stub is rule 8's shape, and the
merge it wants is a shared `tests/tk_stub.py` that all three import. It is NOT
done here because the other two files are being edited by other agents in this
same checkout right now; it is named in the report instead of being left
unsaid.
"""

import importlib
import sys
import types
from datetime import UTC, datetime, timedelta

import pytest

# pandas and yfinance are imported at transactions.py module scope and neither
# is in requirements.txt, which lists exactly Flask and requests. Skip rather
# than error on a machine that has the declared requirements and nothing else:
# an undeclared dependency is a finding for the report, not a reason for the
# suite to go red on a correct install.
pd = pytest.importorskip("pandas", reason="transactions.py imports pandas; it is not in requirements.txt")
pytest.importorskip("yfinance", reason="transactions.py imports yfinance; it is not in requirements.txt")

for _name in ("tkinter", "tkinter.messagebox", "tkinter.ttk"):
    sys.modules.setdefault(_name, types.ModuleType(_name))

# importlib RATHER THAN AN `import` STATEMENT, AND NOT BECAUSE OF THE LINTER.
#
# The placeholders above have to be in sys.modules before transactions.py is loaded, so the
# load cannot be at the top of the file -- and an `import` statement here is E402, which rule
# 19 forbids answering with a `noqa` ("never add a suppression to make a check pass"). A
# module loaded by a function call is not an import statement, so there is nothing to
# suppress and the ordering requirement is still met and still visible.
#
# tests/test_transactions_announces_its_chain.py already does exactly this, for the same
# module and the same reason, so this is the idiom this suite already has rather than a new
# one (rule 8).
transactions = importlib.import_module("transactions")

MAX_YAHOO_STALENESS_DAYS = transactions.MAX_YAHOO_STALENESS_DAYS
as_series = transactions.as_series
as_timestamp = transactions.as_timestamp
extract_close_series = transactions.extract_close_series
fetch_grc_price_from_yahoo = transactions.fetch_grc_price_from_yahoo
normalized_dates = transactions.normalized_dates
numeric_series_on_dates = transactions.numeric_series_on_dates

# Three closes on three consecutive days, each stamped at a different hour so
# that a test which failed to normalize the index would miss a midnight lookup
# rather than quietly pass.
SEEDED_INDEX = ["2026-03-13 14:31:00", "2026-03-14 09:05:00", "2026-03-15 23:59:00"]
SEEDED_CLOSES = [0.0041, 0.0042, 0.0043]


def _frame(index=SEEDED_INDEX, closes=SEEDED_CLOSES, column="Close"):
    """A yfinance-shaped daily frame: a Close column on a DatetimeIndex."""
    return pd.DataFrame({column: closes}, index=pd.to_datetime(index))


def _multiindex_frame():
    """The shape yfinance actually returns for a single ticker.

    yf.download() hands back a MultiIndex column axis -- ('Close', 'GRC-USD')
    and friends -- which is why extract_close_series() has a MultiIndex branch
    at all.
    """
    columns = pd.MultiIndex.from_tuples([("Close", "GRC-USD"), ("Volume", "GRC-USD")])
    return pd.DataFrame([[c, 10] for c in SEEDED_CLOSES], index=pd.to_datetime(SEEDED_INDEX), columns=columns)


# --- as_timestamp: the real defect -------------------------------------------


@pytest.mark.parametrize(
    ("value", "label"),
    [
        (None, "None"),
        (float("nan"), "a float nan"),
        ("", "an empty string"),
    ],
)
def test_as_timestamp_refuses_a_date_that_is_not_one(value, label):
    """THE CLASS-1 DEFECT IN THIS FILE, and it is a wrong annotation with teeth.

    `as_timestamp()` was one line -- `return pd.Timestamp(value)` -- under the
    annotation `-> pd.Timestamp`. Measured at the interpreter on 2026-10-09:
    pd.Timestamp(None), pd.Timestamp(pd.NaT) and pd.Timestamp(float("nan"))
    all return NaT, so the annotation was a claim the function did not perform.

    NaT can genuinely arrive. `pd.to_datetime` turns a missing index label into
    NaT without raising, `.dropna()` drops missing VALUES and not missing index
    labels, and get_latest_grc_price_record() reads `close_series.index[-1]`
    with no mask in front of it. What happened then, also measured:
    `NaT.strftime("%Y-%m-%d")` raises `ValueError: NaTType does not support
    strftime`, three frames away from the missing date, and the caller's broad
    `except Exception` logs THAT and returns None.

    So the right outcome was reached by accident and reported under the wrong
    cause. The fix makes the refusal the annotation already claimed real, at
    the point where the value is known to be missing.
    """
    with pytest.raises(ValueError, match="not a date"):
        as_timestamp(value)
    assert label  # the parametrize label is in the failure output, not an assertion


def test_as_timestamp_passes_a_real_date_through_unchanged():
    """The refusal must not cost the function its job."""
    assert as_timestamp("2026-03-15") == pd.Timestamp("2026-03-15")
    assert as_timestamp(pd.Timestamp("2026-03-15 12:00")) == pd.Timestamp("2026-03-15 12:00")


def test_a_nat_index_label_is_named_rather_than_blamed_on_strftime():
    """End to end: the series whose last label is missing.

    This is the shape get_latest_grc_price_record() reads, and the assertion is
    on WHICH error comes out. Before the fix it was a pandas formatting error
    from inside strftime; now it names the value.
    """
    series = pd.Series([1.0, 2.0], index=pd.to_datetime(["2026-03-14", None]))
    with pytest.raises(ValueError, match="not a date"):
        as_timestamp(series.index[-1])


# --- as_series: what the helper claims about itself --------------------------


def test_as_series_returns_the_identical_object_for_a_series():
    """The claim in the comment above as_series(): no copy, no reindex.

    If this were `pd.Series(obj)` unconditionally it would still pass an
    `.equals()` check while allocating a new object on every call, on a path
    that runs once per priced transaction. `is` is the assertion because
    identity is the claim.
    """
    series = pd.Series(SEEDED_CLOSES, index=pd.to_datetime(SEEDED_INDEX))
    assert as_series(series) is series


def test_as_series_constructs_one_for_what_to_numeric_can_also_return():
    """Totality, which the code it replaced did not have.

    `pd.to_numeric` returns a SCALAR for a scalar argument, and the old
    `pd.to_numeric(x, errors="coerce").dropna()` would have raised
    AttributeError on it. No caller in this file passes a scalar, so this is
    not a bug that fired -- it is the reason the helper is an isinstance and
    not an assertion.
    """
    built = as_series(0.0042)
    assert isinstance(built, pd.Series)
    assert built.tolist() == [0.0042]


# --- numeric_series_on_dates and the merge it came from ----------------------


def test_the_index_is_moved_to_midnight():
    """Times of day are stripped, which is what every lookup in the file keys on."""
    out = numeric_series_on_dates(pd.Series(SEEDED_CLOSES, index=pd.to_datetime(SEEDED_INDEX)))
    assert out.index.tolist() == pd.to_datetime(["2026-03-13", "2026-03-14", "2026-03-15"]).tolist()
    assert pd.Timestamp("2026-03-15") in out.index


def test_values_that_are_not_numbers_are_dropped_not_zeroed():
    """A zero on a price path is a missing measurement wearing a valuation.

    Same reason test_price_row_selection.py's
    test_no_usable_price_is_none_and_never_zero() exists: a 0.0 gets multiplied
    by an amount and recorded as a USD value of nothing, which is
    indistinguishable from a real one.
    """
    out = numeric_series_on_dates(pd.Series([0.004, "n/a", None], index=pd.to_datetime(SEEDED_INDEX)))
    assert out.tolist() == [0.004]
    assert out.index.tolist() == [pd.Timestamp("2026-03-13")]


def test_normalized_dates_returns_a_datetime_index_at_midnight():
    out = normalized_dates(pd.Index(SEEDED_INDEX))
    assert isinstance(out, pd.DatetimeIndex)
    assert out.tolist() == pd.to_datetime(["2026-03-13", "2026-03-14", "2026-03-15"]).tolist()


def test_a_series_input_and_a_frame_input_agree_exactly():
    """THE INVARIANT THE MERGE EXISTS FOR (rule 2: its test pins the stronger one).

    extract_close_series() had two arms and each wrote the same three lines
    itself. Nothing checked that they agreed, and nothing would have failed on
    the day one of them was edited -- which is rule 8's "a bug with a delay on
    it", precisely. Both arms now call one function, and this is the assertion
    that says so from the outside rather than by reading the source.
    """
    from_series = extract_close_series(pd.Series(SEEDED_CLOSES, index=pd.to_datetime(SEEDED_INDEX)))
    from_frame = extract_close_series(_frame())
    assert from_series.index.tolist() == from_frame.index.tolist()
    assert from_series.tolist() == from_frame.tolist()


def test_the_multiindex_column_shape_yfinance_really_returns_is_read():
    out = extract_close_series(_multiindex_frame())
    assert out.tolist() == SEEDED_CLOSES
    assert out.index.tolist() == pd.to_datetime(["2026-03-13", "2026-03-14", "2026-03-15"]).tolist()


@pytest.mark.parametrize(
    ("value", "why"),
    [
        (None, "no frame was downloaded"),
        (pd.DataFrame(), "the frame has no columns at all"),
        (pd.DataFrame({"Volume": [1, 2, 3]}, index=pd.to_datetime(SEEDED_INDEX)), "there is no Close column"),
    ],
)
def test_nothing_usable_is_an_empty_series_and_never_a_raise(value, why):
    """Every caller tests `.empty`, so the no-data answer has to be a Series."""
    out = extract_close_series(value)
    assert isinstance(out, pd.Series), why
    assert out.empty, why


# --- the boolean-masked slice in the Yahoo fallback --------------------------
#
# Lines 637-652 of the pre-fix file read `close_series[close_series.index <=
# target_date]` and then `.empty`, `.index[-1]` and `.iloc[-1]` off it. Those
# three attribute reads were three of the 65 findings, for the same reason as
# the other sixty: `Series.__getitem__` has no return annotation in pandas
# 3.0.6 either, so the inferred union includes an ndarray that has none of
# them. The three tests below are what says the wrap did not change which row
# gets picked or when the staleness guard fires.


def _seed_frame(monkeypatch, frame):
    monkeypatch.setattr(transactions, "load_full_grc_data", lambda: frame)
    monkeypatch.setattr(transactions, "grc_data_cache", frame, raising=False)


def test_an_exact_date_match_is_taken_from_the_series(monkeypatch):
    _seed_frame(monkeypatch, _frame())
    record = fetch_grc_price_from_yahoo("15-03-2026")
    assert record is not None
    assert record["price"] == 0.0043
    assert record["source"] == "Yahoo"
    assert record["source_date"] == "2026-03-15"


def test_the_closest_prior_row_wins_inside_the_staleness_window(monkeypatch):
    """One day past the last close: a fallback, and it says it is one."""
    _seed_frame(monkeypatch, _frame())
    record = fetch_grc_price_from_yahoo("16-03-2026")
    assert record is not None
    assert record["price"] == 0.0043
    assert record["source_date"] == "2026-03-15"
    assert "fallback" in record["source"]


def test_a_gap_past_the_ceiling_is_refused_rather_than_date_shifted(monkeypatch):
    """The guard this slice exists to feed.

    MAX_YAHOO_STALENESS_DAYS is 3, so a target one day past the ceiling must
    produce None. A price from the wrong week is not a price for the date being
    priced, and using one silently date-shifts a cost-basis history.
    """
    _seed_frame(monkeypatch, _frame())
    last = datetime(2026, 3, 15, tzinfo=UTC).date()
    too_far = last + timedelta(days=MAX_YAHOO_STALENESS_DAYS + 1)
    assert fetch_grc_price_from_yahoo(too_far.strftime("%d-%m-%Y")) is None
    just_inside = last + timedelta(days=MAX_YAHOO_STALENESS_DAYS)
    assert fetch_grc_price_from_yahoo(just_inside.strftime("%d-%m-%Y")) is not None


def test_a_target_before_every_row_selects_nothing(monkeypatch):
    """The `prior_series.empty` arm: an empty mask result, not an exception."""
    _seed_frame(monkeypatch, _frame())
    assert fetch_grc_price_from_yahoo("01-01-2020") is None
