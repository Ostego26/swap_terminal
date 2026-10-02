"""reconcile_worker is deposit_watcher's backstop, and its cycle line has to say which.

Role: test (real database on the real schema; stub adapters, no socket)
Reads: workers/reconcile_worker.py, workers/common.STANDING_COUNTS
Writes: a temp database under pytest's tmp_path
Can move funds: no
Mainnet-safe: yes
Live-safe: yes

THE DEFECT, measured 2026-10-02. services/deposit_service.process_active_swaps() is called
by workers/deposit_watcher.py every 15s AND by workers/reconcile_worker.py every 60s, in
two processes against one database -- the duplication reconcile_worker's own header has
documented since 2026-09-24. It produced the duplicated audit rows the compare-and-swap in
services/swap_service.set_swap_status() now refuses, and it left a second thing behind that
the compare-and-swap does nothing about: nothing on reconcile_worker's cycle line said
whether that pass had done any crediting or had simply re-read swaps deposit_watcher had
already advanced. `refreshed_swaps` counts how many swaps are OPEN, and `inventory_rows`
counts rows that exist on any healthy host, so cycle_line() read both as evidence of work
and this worker printed WORKED on every cycle of its life.

THE CALL STAYS, and reconcile_worker.py's header carries the established reasoning:
supervisor.py has no respawn, so this 60s loop is the only other path that credits a
deposit if deposit_watcher dies. What these tests pin is that it still credits when it is
the only loop running, and that its line distinguishes the two cases.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT))
sys.path.insert(0, str(REPOSITORY_ROOT / "swap_terminal"))

from db import SCHEMA, apply_migrations, connect_db  # noqa: E402
from valid_addresses import GRC_PAYOUT, SOL_DEPOSIT_ACCOUNT  # noqa: E402

from swap_terminal.workers import reconcile_worker  # noqa: E402
from swap_terminal.workers.common import STANDING_COUNTS, cycle_line  # noqa: E402

ACCOUNT = SOL_DEPOSIT_ACCOUNT
CONFIG = {
    "AMOUNT_TOLERANCE_PCT": 0.01,
    "SOL_DEPOSIT_ACCOUNT": ACCOUNT,
    "SOL_MIN_CONFIRMATIONS": 3,
}


class DepositAdapter:
    """Answers a balance for the inventory refresh and a fixed event list for the scan."""

    can_spend = True
    payout_refusal = ""

    def __init__(self, events=(), balance=1.0):
        self.events = list(events)
        self._balance = balance

    def get_balance(self):
        return self._balance

    def find_deposits_to_address(self, address, tx_limit=None, skip_txids=frozenset()):
        return [dict(e) for e in self.events if e["txid"] not in skip_txids]

    def validate_address(self, address):
        return True


@pytest.fixture
def db(tmp_path):
    conn = connect_db(str(tmp_path / "t.db"))
    conn.executescript(SCHEMA)
    apply_migrations(conn)
    conn.execute(
        "INSERT INTO quotes (id, from_asset, to_asset, input_amount, quoted_rate, fee_bps,"
        " network_fee_reserve, output_amount_estimate, expires_at, created_at)"
        " VALUES ('q','SOL','GRC',0.01,8700.0,150,0.01,86.0,"
        "'2999-01-01T00:00:00+00:00','2026-10-01T00:00:00+00:00')"
    )
    conn.commit()
    return conn


def seed_swap(db, swap_id, tag, *, status="awaiting_deposit"):
    db.execute(
        "INSERT INTO swaps (id, quote_id, from_asset, to_asset, deposit_address, deposit_tag,"
        " payout_address, expected_input_amount, quoted_rate, fee_bps, network_fee_reserve,"
        " output_amount_estimate, status, min_confirmations, expires_at, created_at, updated_at)"
        " VALUES (?,'q','SOL','GRC',?,?,?,0.01,8700.0,150,0.01,86.0,?,3,"
        "'2999-01-01T00:00:00+00:00','2026-10-01T00:00:00+00:00','2026-10-01T00:00:00+00:00')",
        (swap_id, ACCOUNT, tag, GRC_PAYOUT, status),
    )
    db.commit()


def event(txid, tag, amount=0.01, confirmations=3):
    return {"txid": txid, "vout": tag, "address": ACCOUNT, "amount": amount,
            "confirmations": confirmations}


# --- the backstop does its job ------------------------------------------------


def test_the_backstop_credits_a_deposit_when_it_is_the_ONLY_loop_running(db):
    """WHY THE CALL STAYS, as a credit produced rather than as an argument in a comment.

    supervisor.py has start, stop and status and no respawn, so a dead deposit_watcher
    means this 60s loop is the only thing crediting deposits until a person notices.

    MUTATION -- AND THIS IS THE CALL-SITE ONE. Delete the process_active_swaps() call
    from run_cycle() and this fails: the swap stays in awaiting_deposit, nothing is
    credited, and the only visible change on the line is a count that reads 0 instead of
    1. Removing a safety net is the easiest change in this file to make look harmless.
    """
    seed_swap(db, "s_only", 11)

    line = reconcile_worker.run_cycle(
        db, CONFIG, {"SOL": DepositAdapter([event("tx_one", 11)])}, 1, 0.0)

    row = db.execute("SELECT status FROM swaps WHERE id = 's_only'").fetchone()
    assert row["status"] == "payout_pending", (
        "the backstop did not credit a fully confirmed deposit, so nothing would have "
        "credited it with deposit_watcher dead"
    )
    assert "WORKED" in line, f"a cycle that credited a deposit must not read IDLE: {line}"
    assert "transitions_written=2" in line, (
        f"awaiting_deposit -> confirming and confirming -> payout_pending; line was: {line}"
    )


# --- the line says which of the two happened ----------------------------------


def test_a_cycle_that_moved_NOTHING_reads_IDLE_even_with_swaps_open(db):
    """The healthy shape with both workers up: this pass found nothing left to move.

    MUTATION: remove "refreshed_swaps" from workers/common.STANDING_COUNTS and this
    fails -- the cycle claims WORKED for re-reading a swap it did not touch, which is
    what every reconcile cycle on the operator's host has printed since 2026-09-24.
    """
    seed_swap(db, "s_quiet", 11)

    line = reconcile_worker.run_cycle(db, CONFIG, {"SOL": DepositAdapter()}, 2, 0.0)

    assert "refreshed_swaps=1" in line, f"the open swap is still counted on screen: {line}"
    assert "transitions_written=0" in line
    assert "IDLE" in line, (
        f"re-reading a swap another loop already advanced is not work: {line}"
    )


def test_inventory_rows_alone_cannot_make_the_cycle_claim_it_worked(db):
    """The other latched count, and it latched on every host where any adapter answers.

    refresh_wallet_inventory() writes a row per answering adapter, so `inventory_rows` is
    non-zero forever. MUTATION: remove "inventory_rows" from STANDING_COUNTS and this
    fails.
    """
    line = reconcile_worker.run_cycle(db, CONFIG, {"SOL": DepositAdapter(balance=7.0)}, 3, 0.0)

    assert "inventory_rows=1" in line, f"the standing figure still prints: {line}"
    assert "IDLE" in line, f"a cycle with no swaps and no transitions did nothing: {line}"


def test_both_reconcile_counts_are_named_as_STANDING(db):
    """The constant, asserted by NAME, because that is how cycle_line() reads it.

    Driven through cycle_line() with this worker's exact count keys rather than by
    asserting set membership, so a rename of either key fails here too.
    """
    assert {"refreshed_swaps", "inventory_rows"} <= STANDING_COUNTS
    assert "IDLE" in cycle_line(
        "reconcile_worker", 9, 0.04,
        {"refreshed_swaps": 3, "transitions_written": 0, "inventory_rows": 2},
    )
    assert "WORKED" in cycle_line(
        "reconcile_worker", 10, 0.04,
        {"refreshed_swaps": 3, "transitions_written": 1, "inventory_rows": 2},
    )


# --- what the count is counted out of -----------------------------------------


def test_transitions_written_counts_only_the_swaps_THIS_cycle_refreshed(db):
    """Rule 3: the denominator. Another swap's audit row in the same window is not ours.

    A time window alone would let payout_worker's concurrent claim on an unrelated swap
    read as this loop's work.
    """
    seed_swap(db, "s_mine", 11)
    seed_swap(db, "s_theirs", 12, status="payout_pending")
    db.execute(
        "INSERT INTO swap_audit_log (swap_id, old_status, new_status, message, created_at)"
        " VALUES ('s_theirs','payout_pending','paying','Claimed for payout','2999-01-01T00:00:00+00:00')"
    )
    db.commit()

    line = reconcile_worker.run_cycle(
        db, CONFIG, {"SOL": DepositAdapter([event("tx_mine", 11)])}, 4, 0.0)

    assert "transitions_written=2" in line, (
        f"s_theirs is payout_pending, so it is not refreshed and its row is not ours: {line}"
    )


def test_no_refreshed_swaps_means_zero_without_a_query(db):
    """`IN ()` is not valid SQLite, and zero refreshed swaps really did write zero rows.

    Rule 14: an empty result is a result. Asserted through the real function with an
    empty id list, because the guard is what keeps an idle host from raising every 60s.
    """
    assert reconcile_worker.transitions_written(db, [], since="2026-10-01T00:00:00+00:00") == 0
    assert reconcile_worker.transitions_written(db, None, since="2026-10-01T00:00:00+00:00") == 0
