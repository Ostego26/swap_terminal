"""What `inventory_rows` means, with the denominator it was actually counted out of.

THE LINE THIS TESTS REPLACED TOLD THE OPERATOR A HEALTHY SYSTEM WAS BROKEN, which is
why the note is a tested function rather than a string literal in a print. Measured on
their host 2026-10-02, the reconcile worker printed:

    inventory_rows=1 ... <- inventory_rows should be 3 (BTC/LTC/GRC); fewer means a
    getbalance call is failing and refresh_wallet_inventory swallowed it

1 was correct. Only GRC and SOL had adapters (BTC and LTC were unconfigured, so no
adapter existed and no row could ever appear), and SOL's get_balance() refuses by
design when SOL_HOT_WALLET is unset. The annotation accused the code of swallowing an
exception it in fact logs a WARNING for.

An annotation that makes a working system read as broken is worse than none: it spends
the operator's attention and teaches them to discount the next one.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "swap_terminal"))

from db import SCHEMA, connect_db  # noqa: E402

from swap_terminal.services.payout_service import (  # noqa: E402
    inventory_assets,
    inventory_note,
    refresh_wallet_inventory,
)
from swap_terminal.workers import reconcile_worker  # noqa: E402


class Adapter:
    """Minimal adapter: either answers a balance or raises, which is the only axis here."""

    def __init__(self, balance=None, error=None):
        self._balance = balance
        self._error = error

    def get_balance(self):
        if self._error is not None:
            raise self._error
        return self._balance


# --- the note -----------------------------------------------------------------


def test_the_denominator_is_the_CONSTRUCTED_adapters_and_never_a_fixed_number():
    """Rule 3: a count without what it was counted out of. The old string hardcoded 3."""
    note = inventory_note({"GRC": Adapter(), "SOL": Adapter()}, {"GRC"})
    assert "expected 2" in note
    assert "3" not in note, "a hardcoded denominator is the defect being fixed"
    assert "not a fixed number" in note


def test_the_note_NAMES_the_missing_asset_rather_than_only_counting():
    """`Missing: SOL` is actionable; `fewer than expected` sends the reader looking."""
    note = inventory_note({"GRC": Adapter(), "SOL": Adapter()}, {"GRC"})
    assert "Missing: SOL" in note
    assert "GRC, SOL" in note, "and names what WAS expected, so the set is readable"


def test_the_note_NEVER_claims_the_failure_was_swallowed():
    """The clause that did the damage. refresh_wallet_inventory logs a WARNING naming
    the asset and the reason; the annotation accused it of the defect it was written to
    avoid, and sent a reader hunting a swallowed exception that was not there."""
    note = inventory_note({"GRC": Adapter(), "SOL": Adapter()}, {"GRC"})
    assert "swallowed" in note, "the word appears only to say it is NOT what happened"
    assert "NOT swallowed" in note
    assert "logs a WARNING" in note, "and says where to look instead"


def test_a_COMPLETE_inventory_says_so_rather_than_naming_a_threshold():
    """`did nothing` must not look like `did work`, and the inverse: a complete poll
    must not print a line that reads like a warning."""
    note = inventory_note({"GRC": Adapter(), "SOL": Adapter()}, {"GRC", "SOL"})
    assert "nothing is missing" in note
    assert "Missing" not in note
    assert "failing" not in note


def test_ZERO_adapters_is_CORRECT_and_the_note_says_so():
    """The old string made 0 look like three failures. With no adapter constructed
    there is nothing to poll, which is a configuration fact and not an error."""
    note = inventory_note({}, set())
    assert "is CORRECT" in note
    assert "nothing to poll" in note
    assert "Missing" not in note


def test_an_asset_with_a_row_but_no_adapter_is_not_reported_missing():
    """A stale row from an asset that is no longer configured is not a missing row.
    The note's subject is the adapters that exist now."""
    note = inventory_note({"GRC": Adapter()}, {"GRC", "BTC"})
    assert "nothing is missing" in note


# --- against the real table ---------------------------------------------------


def test_inventory_assets_reads_the_real_rows_the_real_refresh_wrote(tmp_path):
    """Behavioral: run the real refresh with one working and one raising adapter, then
    read the table. The note's `present` argument has to come from the rows that
    actually exist, not from which adapters were asked.
    """
    db = connect_db(str(tmp_path / "t.db"), create=True)
    db.executescript(SCHEMA)
    adapters = {
        "GRC": Adapter(balance=3780.09),
        "SOL": Adapter(error=RuntimeError("SOL_HOT_WALLET is not set, so there is no wallet to read")),
    }
    refresh_wallet_inventory(db, adapters)
    present = inventory_assets(db)
    assert present == {"GRC"}, "the raising adapter wrote no row, which is the whole point"
    note = inventory_note(adapters, present)
    assert "Missing: SOL" in note
    assert "unset refuses by design and is expected here" in note, (
        "the operator's exact case, named so they do not go hunting"
    )


# --- the WIRING, because the call site is what the mutation survived ----------


def test_the_reconcile_cycle_LINE_carries_the_derived_note(tmp_path):
    """The mutation that survived: reverting the call site to the old literal.

    Every test above asserts on inventory_note() and every one of them passed with
    main() printing `inventory_rows should be 3 (BTC/LTC/GRC)` again. So the real
    cycle is driven here, with a real database and a real refresh, and the assertion
    is on the line the worker actually prints.
    """
    db = connect_db(str(tmp_path / "t.db"), create=True)
    db.executescript(SCHEMA)
    adapters = {
        "GRC": Adapter(balance=3780.09),
        "SOL": Adapter(error=RuntimeError("SOL_HOT_WALLET is not set")),
    }
    line = reconcile_worker.run_cycle(db, {}, adapters, 1, 0.0)
    assert "inventory_rows=1" in line, "GRC wrote a row, SOL raised, so one row"
    assert "expected 2" in line, "and the line says 2, derived from the adapters"
    assert "Missing: SOL" in line
    assert "BTC/LTC/GRC" not in line, "the hardcoded trio is the string being replaced"
    assert "NOT swallowed" in line


def test_a_cycle_where_every_adapter_answers_does_not_print_a_warning_shape(tmp_path):
    """`did work` and `something is wrong` must not render alike."""
    db = connect_db(str(tmp_path / "t.db"), create=True)
    db.executescript(SCHEMA)
    adapters = {"GRC": Adapter(balance=1.0), "SOL": Adapter(balance=2.0)}
    line = reconcile_worker.run_cycle(db, {}, adapters, 4, 0.0)
    assert "inventory_rows=2" in line
    assert "nothing is missing" in line
    assert "Missing" not in line
