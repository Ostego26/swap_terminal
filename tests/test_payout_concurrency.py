"""Can two payout workers pay the same swap twice? Measured, not reasoned about.

Role: test / measurement (seeds real rows, runs the real function)
Reads: swap_terminal/services/payout_service.py, swap_terminal/db.py SCHEMA
Writes: a throwaway SQLite database under pytest's tmp_path
Can move funds: no -- the adapter is a stub that appends to a list. No socket
        is opened, no RPC is issued, nothing is signed and nothing is
        broadcast. `send_to_address` here returns a fake txid string.
Mainnet-safe: yes

WHAT THIS FILE IS FOR.

CLAUDE.md rule 13 states the hazard as a consequence, not as a measurement:

    "A payout worker started by hand in a second terminal is invisible to the
    first. Two payout workers polling the same SQLite database will both read
    the same pending swap. Without a database-level state transition guard,
    that is a double payout, and it is on-chain and final."

Rule 17 says not to leave it there: "before a claim about what the live system
is doing, run the thing that would show it false." This file runs it. It seeds
ONE swap in `payout_pending`, runs the REAL `process_pending_payouts()` from
two connections to the SAME database with a controlled overlap, and counts the
sends that actually reached the adapter.

THE RESULT, measured 2026-09-24 on this tree:

    sends observed by the adapter        2
    distinct swap_ids in those sends     1
    rows in payouts for that swap        2   (both status='broadcast')

So yes. Twice, for one deposit.

AND THE FIRST RUN OF THIS TEST SAID OTHERWISE, WHICH IS WORTH RECORDING.
Seeded without a `wallet_inventory` row, worker B does not send: it dies inside
reserve_inventory() with `IntegrityError: UNIQUE constraint failed:
wallet_inventory.asset`, because both workers read no inventory row (A's INSERT
being uncommitted) and both took the INSERT branch. That looks like a guard and
is not one. It only fires on the very first payout for an asset, it protects
nothing afterwards, and what it does instead of paying twice is crash the
worker -- rule 12's "the caller cannot tell the failure from a real answer",
one layer up. test_missing_inventory_row_masks_the_race_by_crashing_the_worker
pins that behavior so nobody mistakes it for the fix.

The realistic state is the one with the row, because payout_worker.py calls
refresh_wallet_inventory() immediately before process_pending_payouts() on
every single cycle, and that function creates the row the first time it runs.
Measuring the cold-start state and stopping there would have produced a
confident, wrong "no, it cannot happen" (rule 17).

WHY SQLITE'S WRITER LOCK DOES NOT SAVE IT -- this is the part that is easy to
get wrong by reasoning. SQLite has one writer lock, so the second worker's
INSERT really does block behind the first one. But the guard that is supposed
to prevent the second payout is a READ:

    SELECT * FROM payouts WHERE swap_id = ? AND status IN ('broadcast','completed')

and that read happens BEFORE the write lock is ever contended. Worker B reads
it while worker A is still inside `send_to_address` with its transaction open
and uncommitted, so B sees no payout row, decides to pay, and only THEN blocks
on the write lock. When A commits, B's write proceeds -- and B is still acting
on a decision it made from a snapshot that is now stale. The lock serialized
the writes and did nothing at all about the decision.

That is check-then-act across two transactions, and it is why rule 13's last
bullet says a database constraint beats a lock: "it survives the case where
the lock was wrong."

THE PROPOSAL THIS MEASUREMENT ARGUES FOR IS NOT APPLIED HERE.

Changing the payout path is fund movement (rule 16), so the two changes below
are for the operator to apply, not for this pass. Both are demonstrated
against a real database in the last two tests of this file, so the operator can
see the evidence rather than take the claim:

  1. Claim the swap with a conditional UPDATE, and proceed only if it won:

         UPDATE swaps SET status = 'paying', updated_at = ?
          WHERE id = ? AND status = 'payout_pending'

     then check `cursor.rowcount == 1`. The loser updates zero rows and must
     `continue`. This turns the guard from a read into a write, so the writer
     lock that already exists is the thing that serializes the DECISION
     instead of only the insert.

  2. Back it with a constraint, so it holds even when the code is wrong:

         CREATE UNIQUE INDEX IF NOT EXISTS idx_payouts_one_live_per_swap
             ON payouts(swap_id)
          WHERE status IN ('created', 'broadcast', 'completed');

     A partial unique index, so that a payout which genuinely FAILED can still
     be retried, while a second live payout for the same swap is impossible to
     insert. test_partial_unique_index_would_have_stopped_it below shows the
     second INSERT raising IntegrityError on a real database.

Both are needed. (1) alone still leaves the window open if a future caller
forgets the rowcount check; (2) alone converts a double payout into an
unhandled IntegrityError after the first send, which is safe but ugly.

WHEN THE OPERATOR APPLIES THE FIX, test_two_workers_can_pay_the_same_swap_twice
BELOW WILL FAIL. That is correct and intended: it is a characterization test of
a defect, it is named and commented as one, and its failure is the signal that
the defect is gone. Invert it then -- assert 1 send, not 2.
"""

import sqlite3
import threading
import time

import pytest
from db import SCHEMA, connect_db
from services.payout_service import process_pending_payouts


class RecordingAdapter:
    """A stand-in for a chain adapter that records sends instead of making them.

    `hold` is an optional threading.Event: when set on the instance, the first
    send blocks until it is set, which is how the overlap between the two
    workers is made deterministic rather than timing-dependent.
    """

    def __init__(self, asset: str):
        self.asset = asset
        self.sends: list[tuple[str, float]] = []
        self.release = None
        self._lock = threading.Lock()

    def send_to_address(self, address: str, amount: float) -> str:
        with self._lock:
            index = len(self.sends)
            self.sends.append((address, float(amount)))
        if self.release is not None and index == 0:
            # Only the FIRST send waits. This is worker A sitting inside its
            # RPC call, which is exactly the window a second worker walks into.
            self.release.wait(timeout=30)
        return f"stub-txid-{index}"

    def get_balance(self) -> float:
        return 1000.0


CONFIG = {
    "BTC_MIN_CONFIRMATIONS": 2,
    "LTC_MIN_CONFIRMATIONS": 2,
    "GRC_MIN_CONFIRMATIONS": 6,
    "AMOUNT_TOLERANCE_PCT": 0.01,
}


def _seed_one_pending_swap(db_path: str, swap_id: str = "s_double", with_inventory_row: bool = True) -> None:
    """Put exactly one swap into `payout_pending`, the state the worker acts on.

    `with_inventory_row` seeds `wallet_inventory` for LTC, and the default is
    True because that is what a running system looks like: payout_worker.py
    calls refresh_wallet_inventory() immediately before process_pending_payouts()
    on EVERY cycle, and that function inserts a row for each asset the first
    time it sees one. A test that omits the row is testing the first fifteen
    seconds of a worker's life and nothing after it -- and, as
    test_missing_inventory_row_masks_the_race_by_crashing_the_worker below
    shows, it measures something completely different.
    """
    conn = connect_db(db_path)
    conn.executescript(SCHEMA)
    now = "2026-09-24T00:00:00+00:00"
    conn.execute(
        """
        INSERT INTO quotes (id, from_asset, to_asset, input_amount, quoted_rate, fee_bps,
                            network_fee_reserve, output_amount_estimate, expires_at, created_at)
        VALUES ('q_double', 'GRC', 'LTC', 100.0, 0.001, 150, 0.001, 0.0975, ?, ?)
        """,
        (now, now),
    )
    conn.execute(
        """
        INSERT INTO swaps (
            id, quote_id, from_asset, to_asset, deposit_address, payout_address,
            expected_input_amount, actual_input_amount, quoted_rate, fee_bps,
            network_fee_reserve, output_amount_estimate, status, min_confirmations,
            deposit_txid, payout_txid, created_at, updated_at, credited_at,
            completed_at, expires_at, failed_reason
        ) VALUES (?, 'q_double', 'GRC', 'LTC', 'grc_deposit_addr', 'ltc_payout_addr',
                  100.0, 100.0, 0.001, 150, 0.001, 0.0975, 'payout_pending', 6,
                  'grc_txid', NULL, ?, ?, ?, NULL, ?, NULL)
        """,
        (swap_id, now, now, now, now),
    )
    if with_inventory_row:
        conn.execute(
            "INSERT INTO wallet_inventory (asset, hot_confirmed, hot_reserved, hot_available, updated_at) "
            "VALUES ('LTC', 10.0, 0.0, 10.0, ?)",
            (now,),
        )
    conn.commit()
    conn.close()


@pytest.fixture
def db_path(tmp_path):
    path = str(tmp_path / "swap_terminal_concurrency.db")
    _seed_one_pending_swap(path)
    return path


def test_one_worker_alone_pays_exactly_once(db_path):
    """The control. Without overlap the guard works, which is why this is subtle."""
    adapter = RecordingAdapter("LTC")
    conn = connect_db(db_path)
    process_pending_payouts(conn, CONFIG, {"LTC": adapter, "GRC": RecordingAdapter("GRC")})
    conn.close()

    assert len(adapter.sends) == 1

    # And a second pass over the same database sends nothing more, because the
    # swap is no longer payout_pending.
    conn = connect_db(db_path)
    process_pending_payouts(conn, CONFIG, {"LTC": adapter, "GRC": RecordingAdapter("GRC")})
    conn.close()
    assert len(adapter.sends) == 1


def test_two_workers_can_pay_the_same_swap_twice(db_path):
    """MEASUREMENT, not a specification. See this file's header.

    When the operator applies the claim-by-UPDATE and the partial unique index,
    this test must be INVERTED to assert exactly one send. Its failure is the
    signal that the double payout is fixed, not a regression.
    """
    adapter = RecordingAdapter("LTC")
    adapter.release = threading.Event()
    adapters = {"LTC": adapter, "GRC": RecordingAdapter("GRC")}

    def worker():
        # Each thread opens its own connection, because sqlite3 objects are
        # bound to the thread that created them -- and because that is the
        # faithful shape anyway: two worker PROCESSES each hold their own.
        conn = connect_db(db_path)
        try:
            process_pending_payouts(conn, CONFIG, adapters)
        finally:
            conn.close()

    thread_a = threading.Thread(target=worker, name="payout-worker-A")
    thread_a.start()

    # Wait for A to be inside send_to_address -- its transaction is open and
    # uncommitted at this point, which is the window a second worker enters.
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and not adapter.sends:
        time.sleep(0.01)
    assert adapter.sends, "worker A never reached send_to_address; test setup failed"

    thread_b = threading.Thread(target=worker, name="payout-worker-B")
    thread_b.start()

    # Give B time to complete BOTH of its reads -- the swaps SELECT and the
    # payouts guard SELECT -- and then block on the write lock. Half a second
    # is roughly five orders of magnitude more than two indexed SELECTs on a
    # local temp file need, and B's busy timeout is SQLite's default 5s, so
    # the ordering does not depend on how fast the machine is.
    time.sleep(0.5)
    adapter.release.set()

    thread_a.join(timeout=30)
    thread_b.join(timeout=30)

    # The measurement.
    assert len(adapter.sends) == 2, (
        f"expected the double payout this file documents, saw {len(adapter.sends)} send(s). "
        "If this now says 1, the guard was fixed -- invert this test and delete the proposal."
    )
    assert adapter.sends[0] == adapter.sends[1], "both sends went to the same address for the same amount"

    conn = connect_db(db_path)
    payouts = conn.execute("SELECT status, txid FROM payouts WHERE swap_id = 's_double'").fetchall()
    conn.close()
    assert len(payouts) == 2
    assert [row["status"] for row in payouts] == ["broadcast", "broadcast"]


def test_missing_inventory_row_masks_the_race_by_crashing_the_worker(tmp_path):
    """The cold-start case, pinned so it is not mistaken for a guard.

    With no `wallet_inventory` row for the payout asset, both workers take
    reserve_inventory()'s INSERT branch and the second one hits
    UNIQUE(wallet_inventory.asset). B therefore does not send -- but it does
    not decline either: it raises out of process_pending_payouts(), the
    db_session rolls back, and the worker loop dies. This protects exactly the
    first payout ever made for an asset and nothing after it.
    """
    path = str(tmp_path / "cold_start.db")
    _seed_one_pending_swap(path, with_inventory_row=False)

    adapter = RecordingAdapter("LTC")
    adapter.release = threading.Event()
    adapters = {"LTC": adapter, "GRC": RecordingAdapter("GRC")}
    failures: list[BaseException] = []

    def worker():
        conn = connect_db(path)
        try:
            process_pending_payouts(conn, CONFIG, adapters)
        except BaseException as exc:  # noqa: BLE001 -- the test's whole subject is which exception escapes; it is recorded, not swallowed
            failures.append(exc)
        finally:
            conn.close()

    thread_a = threading.Thread(target=worker, name="cold-A")
    thread_a.start()
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and not adapter.sends:
        time.sleep(0.01)
    thread_b = threading.Thread(target=worker, name="cold-B")
    thread_b.start()
    time.sleep(0.5)
    adapter.release.set()
    thread_a.join(timeout=30)
    thread_b.join(timeout=30)

    assert len(adapter.sends) == 1  # B never got to send
    assert len(failures) == 1
    assert isinstance(failures[0], sqlite3.IntegrityError)
    assert "wallet_inventory" in str(failures[0])


def test_the_guard_read_is_what_goes_stale(db_path):
    """Isolate the mechanism, so the fix is aimed at the right thing.

    Worker A inserts its payout row and holds the transaction open. From a
    SECOND connection, the exact guard query in payout_service returns nothing
    -- not because the guard is wrong about what it asks, but because the row
    it would match is not committed yet.
    """
    conn_a = connect_db(db_path)
    conn_a.execute(
        "INSERT INTO payouts (swap_id, asset, destination_address, amount, txid, status, created_at, sent_at) "
        "VALUES ('s_double', 'LTC', 'ltc_payout_addr', 0.0975, NULL, 'created', '2026-09-24T00:00:00+00:00', NULL)"
    )
    # Deliberately NOT committed: this is A mid-send.

    conn_b = connect_db(db_path)
    still_pending = conn_b.execute("SELECT status FROM swaps WHERE id = 's_double'").fetchone()
    guard = conn_b.execute(
        "SELECT * FROM payouts WHERE swap_id = 's_double' AND status IN ('broadcast','completed')"
    ).fetchone()

    assert still_pending["status"] == "payout_pending"  # B sees work to do
    assert guard is None  # and B's guard says nobody is doing it

    conn_a.rollback()
    conn_a.close()
    conn_b.close()


def test_claim_by_conditional_update_would_have_stopped_it(db_path):
    """Proposal 1, demonstrated on a real database. NOT applied to the tree.

    Exactly one of two concurrent claimants can win a conditional UPDATE,
    because the UPDATE takes the writer lock and re-reads the row under it.
    rowcount is the whole mechanism: the loser updates zero rows.
    """
    conn_a = connect_db(db_path)
    claim_a = conn_a.execute(
        "UPDATE swaps SET status = 'paying', updated_at = ? WHERE id = 's_double' AND status = 'payout_pending'",
        ("2026-09-24T00:00:01+00:00",),
    )
    assert claim_a.rowcount == 1  # A won and may send
    conn_a.commit()

    conn_b = connect_db(db_path)
    claim_b = conn_b.execute(
        "UPDATE swaps SET status = 'paying', updated_at = ? WHERE id = 's_double' AND status = 'payout_pending'",
        ("2026-09-24T00:00:02+00:00",),
    )
    assert claim_b.rowcount == 0  # B lost and must `continue`, sending nothing
    conn_b.commit()

    conn_a.close()
    conn_b.close()


def test_partial_unique_index_would_have_stopped_it(db_path):
    """Proposal 2, demonstrated on a real database. NOT applied to the tree.

    A partial unique index makes a second LIVE payout row impossible while
    still allowing a genuinely failed payout to be retried -- which a plain
    UNIQUE(swap_id) would forbid, turning one incident into a stuck swap.
    """
    conn = connect_db(db_path)
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_payouts_one_live_per_swap "
        "ON payouts(swap_id) WHERE status IN ('created', 'broadcast', 'completed')"
    )
    insert = (
        "INSERT INTO payouts (swap_id, asset, destination_address, amount, txid, status, created_at, sent_at) "
        "VALUES ('s_double', 'LTC', 'ltc_payout_addr', 0.0975, ?, ?, '2026-09-24T00:00:00+00:00', NULL)"
    )
    conn.execute(insert, ("txid-a", "broadcast"))
    conn.commit()

    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(insert, ("txid-b", "created"))
    conn.rollback()

    # A failed payout does not block a retry: that is why the index is partial.
    conn.execute("UPDATE payouts SET status = 'failed' WHERE swap_id = 's_double'")
    conn.execute(insert, ("txid-retry", "created"))
    conn.commit()
    live = conn.execute(
        "SELECT COUNT(*) AS n FROM payouts WHERE swap_id = 's_double' AND status IN ('created','broadcast','completed')"
    ).fetchone()
    assert live["n"] == 1
    conn.close()
