"""Can two payout workers pay the same swap twice? Measured, not reasoned about.

Role: test / measurement (seeds real rows, runs the real function)
Reads: swap_terminal/services/payout_service.py, swap_terminal/db.py SCHEMA
       and apply_migrations()
Writes: a throwaway SQLite database under pytest's tmp_path
Can move funds: no -- the adapter is a stub that appends to a list. No socket
        is opened, no RPC is issued, nothing is signed and nothing is
        broadcast. `send_to_address` here returns a fake txid string.
Mainnet-safe: yes

WHAT THIS FILE MEASURED, AND WHAT IT MEASURES NOW.

On 2026-09-24 it ran ONE swap in `payout_pending` through the REAL
`process_pending_payouts()` from two connections to the SAME database with a
controlled overlap, and counted the sends that reached the adapter:

    sends observed by the adapter        2
    distinct swap_ids in those sends     1
    rows in payouts for that swap        2   (both status='broadcast')

Twice, for one deposit. SQLite's writer lock did not prevent it, and the
reason is the part that is easy to get wrong by reasoning: the guard was a
READ,

    SELECT * FROM payouts WHERE swap_id = ? AND status IN ('broadcast','completed')

and it completed before the write lock was ever contended. Worker B ran it
while worker A was still inside `send_to_address` with its transaction open
and uncommitted, so B saw no payout row, decided to pay, and only THEN blocked
on the write lock. When A committed, B's write proceeded -- acting on a
decision made from a snapshot that was already stale. Check-then-act across
two transactions. The lock serialized the writes and did nothing about the
decision.

THE FIX LANDED THE SAME DAY, AND THIS FILE IS INVERTED ACCORDINGLY (rule 2).

    test_two_workers_can_pay_the_same_swap_twice   ->
    test_two_workers_cannot_pay_the_same_swap_twice, asserting ONE send.

Both halves are applied and both are exercised below, because neither is
sufficient alone:

  1. claim_swap_for_payout() runs

         UPDATE swaps SET status = 'paying', updated_at = ?
          WHERE id = ? AND status = 'payout_pending'

     and only a caller whose `rowcount` is 1 may send. This turns the guard
     from a read into a write, so the writer lock that already existed
     serializes the DECISION. Alone it reopens the hole the moment a future
     caller forgets the rowcount check.

  2. idx_payouts_one_live_per_swap, a PARTIAL unique index on payouts(swap_id)
     WHERE status IN ('created','broadcast','completed'), created by
     db.apply_migrations(). A second LIVE payout row is impossible to insert;
     a genuinely FAILED payout is still retryable, which a plain
     UNIQUE(swap_id) would have forbidden -- turning one incident into a stuck
     swap. Alone it converts a double payout into an IntegrityError AFTER the
     first send has already gone out.

AND THE FIRST RUN OF THE ORIGINAL TEST SAID OTHERWISE, WHICH IS STILL WORTH
RECORDING. Seeded without a `wallet_inventory` row, worker B did not send: it
died inside reserve_inventory() with `IntegrityError: UNIQUE constraint failed:
wallet_inventory.asset`, because both workers read no inventory row (A's INSERT
being uncommitted) and both took the INSERT branch. That looked like a guard
and was not one -- it only ever fired on the very first payout for an asset,
and what it did instead of paying twice was crash the worker.

The realistic state is the one WITH the row, because payout_worker.py calls
refresh_wallet_inventory() immediately before process_pending_payouts() on
every cycle, and that function creates the row the first time it runs. Seeding
without it measures the first fifteen seconds of a worker's life and nothing
after it. test_the_cold_start_case_no_longer_crashes_the_worker keeps that case
around, inverted: B now declines at the claim, before it ever reaches
inventory, so there is no crash to mistake for a guard.

WHAT THIS FILE DOES NOT PROVE. Nothing here touches a chain. `send_to_address`
returns a string. The concurrency it demonstrates is two threads against one
local SQLite file, which is the faithful shape for two worker processes on one
host; it says nothing about two hosts against a database on a network file
system, where SQLite's locking assumptions are different and were not tested
(rule 17).
"""

import logging
import sqlite3
import threading
import time

import pytest
from db import SCHEMA, add_column_if_missing, apply_migrations, connect_db
from services.helpers import utc_now_iso
from services.payout_service import claim_swap_for_payout, process_pending_payouts


class RecordingAdapter:
    """A stand-in for a chain adapter that records sends instead of making them.

    `release` is an optional threading.Event: when set on the instance, the
    first send blocks until it is set, which is how the overlap between the two
    workers is made deterministic rather than timing-dependent.

    `on_send` is an optional callback invoked while the first send is "in
    flight", so a test can observe what the database looks like at the one
    moment that matters: after the intent to pay is committed and before the
    txid exists.
    """

    def __init__(self, asset: str):
        self.asset = asset
        self.sends: list[tuple[str, float]] = []
        self.release = None
        self.on_send = None
        self._lock = threading.Lock()

    def send_to_address(self, address: str, amount: float) -> str:
        with self._lock:
            index = len(self.sends)
            self.sends.append((address, float(amount)))
        if index == 0 and self.on_send is not None:
            self.on_send()
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


def _seed_one_pending_swap(
    db_path: str,
    swap_id: str = "s_double",
    with_inventory_row: bool = True,
    with_index: bool = True,
) -> None:
    """Put exactly one swap into `payout_pending`, the state the worker acts on.

    `with_inventory_row` defaults True because that is what a running system
    looks like: payout_worker.py calls refresh_wallet_inventory() immediately
    before process_pending_payouts() on EVERY cycle.

    `with_index` runs db.apply_migrations(), which is what payout_worker.py
    does once at startup. It is a parameter rather than unconditional so that
    the claim-by-UPDATE can be measured on its own, without the index standing
    behind it -- otherwise a regression in the claim would be masked by the
    constraint and nobody would learn which one was holding.
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
    if with_index:
        apply_migrations(conn)
    conn.close()


@pytest.fixture
def db_path(tmp_path):
    path = str(tmp_path / "swap_terminal_concurrency.db")
    _seed_one_pending_swap(path)
    return path


def _run_two_overlapping_workers(path: str, adapters: dict, adapter: RecordingAdapter) -> list:
    """Start A, wait until it is inside send_to_address, then start B.

    Returns whatever escaped either worker, so a test can assert on the
    absence of an exception rather than on a green run that swallowed one.
    """
    failures: list[BaseException] = []

    def worker():
        # Each thread opens its own connection, because sqlite3 objects are
        # bound to the thread that created them -- and because that is the
        # faithful shape anyway: two worker PROCESSES each hold their own.
        conn = connect_db(path)
        try:
            process_pending_payouts(conn, CONFIG, adapters)
        except BaseException as exc:  # noqa: BLE001 -- recorded, not swallowed: what escapes is the subject
            failures.append(exc)
        finally:
            conn.close()

    thread_a = threading.Thread(target=worker, name="payout-worker-A")
    thread_a.start()

    # Wait for A to be inside send_to_address -- its payout row is committed
    # and its txid does not exist yet, which is the window a second worker
    # enters.
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and not adapter.sends:
        time.sleep(0.01)
    assert adapter.sends, "worker A never reached send_to_address; test setup failed"

    thread_b = threading.Thread(target=worker, name="payout-worker-B")
    thread_b.start()

    # Give B time to complete its claim attempt and everything after it. Half a
    # second is roughly five orders of magnitude more than an indexed UPDATE on
    # a local temp file needs.
    time.sleep(0.5)
    adapter.release.set()

    thread_a.join(timeout=30)
    thread_b.join(timeout=30)
    return failures


def test_one_worker_alone_pays_exactly_once(db_path):
    """The control. Unchanged by the fix, and it must stay that way."""
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


def test_two_workers_cannot_pay_the_same_swap_twice(db_path):
    """INVERTED from test_two_workers_can_pay_the_same_swap_twice.

    That test asserted 2 sends and said, in its own docstring, that its failure
    would be the signal that the defect was gone. The fix landed on 2026-09-24
    and this is that same overlap, asserting the number that matters: ONE.
    """
    adapter = RecordingAdapter("LTC")
    adapter.release = threading.Event()
    adapters = {"LTC": adapter, "GRC": RecordingAdapter("GRC")}

    failures = _run_two_overlapping_workers(db_path, adapters, adapter)

    assert failures == [], f"a worker died instead of declining: {failures}"
    assert len(adapter.sends) == 1, (
        f"expected exactly one send for one swap, saw {len(adapter.sends)}. "
        "Two means the claim-by-UPDATE stopped holding."
    )

    conn = connect_db(db_path)
    payouts = conn.execute("SELECT status, txid FROM payouts WHERE swap_id = 's_double'").fetchall()
    swap = conn.execute("SELECT status, payout_txid FROM swaps WHERE id = 's_double'").fetchone()
    audit = conn.execute(
        "SELECT new_status FROM swap_audit_log WHERE swap_id = 's_double' ORDER BY id"
    ).fetchall()
    conn.close()

    assert len(payouts) == 1
    assert payouts[0]["status"] == "broadcast"
    assert swap["status"] == "completed"
    assert swap["payout_txid"] == payouts[0]["txid"]
    # The claim is recorded as a transition, so an operator reading the audit
    # log can see which worker took the swap and when.
    assert [row["new_status"] for row in audit] == ["paying", "completed"]


def test_the_claim_alone_holds_without_the_index(tmp_path):
    """One send even with NO unique index, so it is clear which half is holding.

    If this passes and the index test also passes, the two mechanisms are
    independent. If only the index were doing the work, a future caller that
    forgets the rowcount check would silently reopen the hole and nothing here
    would say so.
    """
    path = str(tmp_path / "claim_only.db")
    _seed_one_pending_swap(path, with_index=False)

    conn = connect_db(path)
    has_index = conn.execute(
        "SELECT COUNT(*) AS n FROM sqlite_master WHERE type = 'index' AND name = 'idx_payouts_one_live_per_swap'"
    ).fetchone()["n"]
    conn.close()
    assert has_index == 0, "this test is only meaningful without the index"

    adapter = RecordingAdapter("LTC")
    adapter.release = threading.Event()
    failures = _run_two_overlapping_workers(path, {"LTC": adapter, "GRC": RecordingAdapter("GRC")}, adapter)

    assert failures == []
    assert len(adapter.sends) == 1


def test_the_intent_to_pay_is_committed_before_the_send(db_path):
    """The ordering rule 5 asks for, observed from a SECOND connection mid-send.

    While worker A is inside send_to_address, another connection must already
    be able to SEE a payouts row for this swap -- with no txid yet -- and the
    swap already out of `payout_pending`. That is what makes a crash during the
    send a visible, non-repeating failure instead of a lost payment that the
    next cycle pays again.
    """
    observed = {}
    adapter = RecordingAdapter("LTC")

    def observe():
        other = connect_db(db_path)
        observed["payouts"] = other.execute(
            "SELECT status, txid FROM payouts WHERE swap_id = 's_double'"
        ).fetchall()
        observed["swap_status"] = other.execute(
            "SELECT status FROM swaps WHERE id = 's_double'"
        ).fetchone()["status"]
        other.close()

    adapter.on_send = observe
    conn = connect_db(db_path)
    process_pending_payouts(conn, CONFIG, {"LTC": adapter, "GRC": RecordingAdapter("GRC")})
    conn.close()

    assert len(observed["payouts"]) == 1
    assert observed["payouts"][0]["status"] == "created"
    assert observed["payouts"][0]["txid"] is None, "the txid does not exist yet -- that is the point of the window"
    assert observed["swap_status"] == "paying"


def test_the_cold_start_case_no_longer_crashes_the_worker(tmp_path):
    """INVERTED from test_missing_inventory_row_masks_the_race_by_crashing_the_worker.

    With no `wallet_inventory` row, both workers used to take
    reserve_inventory()'s INSERT branch and the second one hit
    UNIQUE(wallet_inventory.asset): B did not send, but it did not decline
    either -- it raised out of process_pending_payouts() and the worker loop
    died. That protected exactly the first payout ever made for an asset and
    nothing after it.

    B now loses the claim before it reaches inventory at all, so there is no
    crash and nothing to mistake for a guard.
    """
    path = str(tmp_path / "cold_start.db")
    _seed_one_pending_swap(path, with_inventory_row=False)

    adapter = RecordingAdapter("LTC")
    adapter.release = threading.Event()
    failures = _run_two_overlapping_workers(path, {"LTC": adapter, "GRC": RecordingAdapter("GRC")}, adapter)

    assert len(adapter.sends) == 1
    assert failures == [], f"no worker should raise any more, saw {failures}"


def test_the_guard_read_that_used_to_go_stale_still_goes_stale(db_path):
    """The mechanism, isolated, so nobody re-introduces the old shape.

    Worker A inserts its payout row and holds the transaction open. From a
    SECOND connection, the OLD guard query returns nothing -- not because it
    asks the wrong question, but because the row it would match is not
    committed yet. This is why the decision had to become a write; the read is
    reproduced here rather than described.
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
    assert guard is None  # and the old guard says nobody is doing it

    conn_a.rollback()
    conn_a.close()
    conn_b.close()


def test_claim_by_conditional_update_lets_exactly_one_claimant_through(db_path):
    """Mechanism 1, on a real database. Now applied, not proposed.

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
    assert claim_b.rowcount == 0  # B lost and must decline, sending nothing
    conn_b.commit()

    conn_a.close()
    conn_b.close()


def test_a_second_claim_on_the_same_swap_is_refused_by_the_real_function(db_path):
    """claim_swap_for_payout() itself, from two connections. The rowcount check.

    This is deliberately separate from the two-worker test above, and the
    reason is worth writing down because it changes what each test proves.

    In the two-worker test, B starts while A is already inside send_to_address
    -- and by then A's claim is COMMITTED, so B's opening
    `SELECT ... WHERE status = 'payout_pending'` returns nothing at all and B
    has no swap to act on. That is a real and sufficient defense, but it means
    that test does not exercise the rowcount branch: it is the commit-before-
    send ordering doing the work.

    The window the rowcount branch is for is the narrower one where BOTH
    workers read the pending list before EITHER claims. That is what these two
    calls reproduce, at the function that decides, with seeded state rather
    than with sleeps.
    """
    conn_a = connect_db(db_path)
    conn_b = connect_db(db_path)
    try:
        assert claim_swap_for_payout(conn_a, "s_double") is True
        assert claim_swap_for_payout(conn_b, "s_double") is False, (
            "the second claimant must decline; if this returns True the rowcount check is gone "
            "and only the unique index stands between one payout and two"
        )
        # And a swap that is not payable at all is refused too.
        assert claim_swap_for_payout(conn_a, "no_such_swap") is False
    finally:
        conn_a.close()
        conn_b.close()


def test_the_partial_unique_index_is_in_place_and_is_partial(db_path):
    """Mechanism 2, on a real database, created by the real apply_migrations().

    A second LIVE payout row is impossible while a genuinely failed payout can
    still be retried -- which a plain UNIQUE(swap_id) would forbid, turning one
    incident into a stuck swap.
    """
    conn = connect_db(db_path)
    assert (
        conn.execute(
            "SELECT COUNT(*) AS n FROM sqlite_master WHERE type='index' AND name='idx_payouts_one_live_per_swap'"
        ).fetchone()["n"]
        == 1
    ), "apply_migrations() did not create the index"

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


def test_apply_migrations_refuses_to_destroy_evidence_of_a_past_double_payout(tmp_path):
    """A database that ALREADY contains a double payout keeps both rows.

    SQLite cannot build a unique index over data that violates it, and the
    honest outcome is to say which swaps are affected and leave the rows alone.
    Deleting one to get the index built would be deleting the record of a
    payment that is on chain -- and the index is not the thing that matters at
    that point; the reconciliation is.
    """
    path = str(tmp_path / "already_doubled.db")
    _seed_one_pending_swap(path, with_index=False)
    conn = connect_db(path)
    insert = (
        "INSERT INTO payouts (swap_id, asset, destination_address, amount, txid, status, created_at, sent_at) "
        "VALUES ('s_double', 'LTC', 'ltc_payout_addr', 0.0975, ?, 'broadcast', '2026-09-24T00:00:00+00:00', NULL)"
    )
    conn.execute(insert, ("txid-a",))
    conn.execute(insert, ("txid-b",))
    conn.commit()

    result = apply_migrations(conn)

    assert result["index_created"] is False
    assert [row["swap_id"] for row in result["duplicates"]] == ["s_double"]
    assert result["duplicates"][0]["live_rows"] == 2
    assert (
        conn.execute("SELECT COUNT(*) AS n FROM payouts WHERE swap_id = 's_double'").fetchone()["n"] == 2
    ), "both rows must still be there -- they are the record of what was sent"
    assert (
        conn.execute(
            "SELECT COUNT(*) AS n FROM sqlite_master WHERE type='index' AND name='idx_payouts_one_live_per_swap'"
        ).fetchone()["n"]
        == 0
    )
    conn.close()


def test_apply_migrations_is_idempotent(db_path):
    """Running it twice is a no-op, because the worker runs it on every start.

    `deposit_tag_added` is False on BOTH runs here and that is the interesting
    part, not an omission: this fixture builds the database from the current
    SCHEMA, which already declares swaps.deposit_tag, so the ALTER has nothing to
    do. It returns True only for a database created before the column existed --
    pinned separately in test_deposit_tag_migration_adds_the_column_to_an_old_db.
    Asserting the whole dict rather than individual keys is deliberate: it is what
    caught the added key when deposit_tag landed on 2026-09-26.
    """
    conn = connect_db(db_path)
    first = apply_migrations(conn)
    second = apply_migrations(conn)
    conn.close()
    assert first == {"index_created": True, "duplicates": [], "deposit_tag_added": False}
    assert second == {"index_created": True, "duplicates": [], "deposit_tag_added": False}


def test_deposit_tag_migration_adds_the_column_to_an_old_db(tmp_path):
    """The case the fixture above cannot reach: a database predating the column.

    Built by hand from a cut-down `swaps` table rather than from SCHEMA, because
    SCHEMA now HAS the column -- so the only way to exercise the migration is to
    construct the old shape. Asserts the existing row survives with NULL rather
    than merely that the ALTER ran: a migration that added the column and lost a
    swap would pass a column-presence check.
    """
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript("CREATE TABLE swaps (id TEXT PRIMARY KEY, deposit_address TEXT NOT NULL);")
    conn.execute("INSERT INTO swaps VALUES ('s-old', 'rPreExisting')")
    conn.commit()

    assert add_column_if_missing(conn, "swaps", "deposit_tag", "INTEGER") is True
    assert add_column_if_missing(conn, "swaps", "deposit_tag", "INTEGER") is False

    row = conn.execute("SELECT id, deposit_address, deposit_tag FROM swaps").fetchone()
    conn.close()
    assert row == ("s-old", "rPreExisting", None), "the pre-existing swap must survive the ALTER"


def seed_payout_pending_swap(conn, swap_id):
    """One swap sitting at payout_pending, with the quote row its FOREIGN KEY needs."""
    now = utc_now_iso()
    conn.execute(
        "INSERT OR IGNORE INTO quotes (id, from_asset, to_asset, input_amount, quoted_rate, fee_bps, "
        "network_fee_reserve, output_amount_estimate, created_at, expires_at) "
        "VALUES ('q_seed','XRP','GRC',1.0,56.0,150,0.01,55.43,?,'2099-01-01T00:00:00+00:00')",
        (now,),
    )
    conn.execute(
        "INSERT INTO swaps (id, quote_id, from_asset, to_asset, deposit_address, payout_address, "
        "expected_input_amount, quoted_rate, fee_bps, network_fee_reserve, output_amount_estimate, "
        "status, min_confirmations, created_at, updated_at, expires_at) "
        "VALUES (?, 'q_seed','XRP','GRC','rAcct','mPayTo',1.0,56.0,150,0.01,55.43,"
        "'payout_pending',1,?,?,'2099-01-01T00:00:00+00:00')",
        (swap_id, now, now),
    )
    conn.commit()


def test_a_failed_payout_names_the_reason_in_the_log(db_path, caplog):
    """The reason reached the database and nothing the operator was watching.

    Their run 2026-09-26 printed:

        payout_worker cycle=1 WORKED pending_at_start=1 broadcast=0 failed_total=1

    and nothing else. swaps.failed_reason and the audit log both had the reason —
    but from the terminal a locked wallet, an insufficient balance, a rejected
    address and an unreachable daemon all look identical, and the one that is a
    five-second fix is indistinguishable from the one that needs an investigation.

    At ERROR because a failed payout on a CREDITED swap is the most serious routine
    outcome this worker has: the deposit is already ours and the customer has not
    been paid.
    """
    caplog.set_level(logging.ERROR)
    conn = connect_db(db_path)
    conn.executescript(SCHEMA)
    seed_payout_pending_swap(conn, "s_locked")

    class LockedWallet:
        def send_to_address(self, address, amount):
            raise RuntimeError("Error: Please enter the wallet passphrase with walletpassphrase first.")

        def get_balance(self):
            return 4190.0

    process_pending_payouts(conn, {}, {"GRC": LockedWallet()})
    conn.close()

    assert "payout FAILED" in caplog.text
    assert "walletpassphrase" in caplog.text, "the log must carry the DAEMON's reason, not a summary"
    assert "s_locked" in caplog.text, "it must name which swap"
    assert "will NOT retry" in caplog.text, "it must say what happens next, not just what failed"


def test_a_locked_wallet_leaves_the_swap_terminally_failed(db_path):
    """MEASURED, and reported to the operator rather than changed here.

    A locked Gridcoin wallet is a TRANSIENT, operator-fixable condition — the
    wallet is normally unlocked for staking only, which cannot send, and a payout
    needs a full unlock. But process_pending_payouts() marks every send failure
    'failed', which is terminal: nothing retries it.

    So a swap whose deposit was already credited dies because the operator had not
    unlocked a wallet. Their XRP is in the account and the swap is dead.

    This test PINS the current behavior rather than asserting the desired one,
    because changing whether a failed payout retries changes what gets sent and
    when — live posture, and the operator's call (rule 16). If they choose to make
    transient failures retryable, this test is the one to change, and it names why.
    """
    conn = connect_db(db_path)
    conn.executescript(SCHEMA)
    seed_payout_pending_swap(conn, "s_locked")

    class LockedWallet:
        def send_to_address(self, address, amount):
            raise RuntimeError("Error: Please enter the wallet passphrase with walletpassphrase first.")

        def get_balance(self):
            return 4190.0

    process_pending_payouts(conn, {}, {"GRC": LockedWallet()})
    row = conn.execute("SELECT status FROM swaps WHERE id = 's_locked'").fetchone()

    # A second pass finds nothing, which is the whole point: 'failed' is not
    # 'payout_pending', so the swap is never looked at again.
    again = process_pending_payouts(conn, {}, {"GRC": LockedWallet()})
    conn.close()

    assert row["status"] == "failed"
    assert again == [], "a terminally failed swap is never retried, even once the wallet is unlocked"
