"""A status write only lands when the swap is still in the status the writer thinks it is.

Role: test (real database on the real schema; stub adapters, no socket)
Reads: services/swap_service.set_swap_status, services/deposit_service.py
Writes: a temp database under pytest's tmp_path
Can move funds: no
Mainnet-safe: yes
Live-safe: yes

WHY THIS FILE EXISTS, MEASURED ON THE OPERATOR'S HOST FROM THEIR OWN AUDIT TRAIL.
`set_swap_status()` took an `old_status` argument and used it for ONE thing -- filling
in the audit row -- while the write was `UPDATE swaps SET status = ? WHERE id = ?`.
So a worker holding a stale read still wrote, and still wrote an audit row describing
a transition out of a status the swap had already left. The identical two transitions,
recorded twice, 0.83s apart, because deposit_watcher (15s) and reconcile_worker (60s)
both ran process_active_swaps() over that swap:

    21:37:46.943103  s_95a807c181644190  confirming -> payout_pending  Deposit fully confirmed
    21:37:46.942952  s_95a807c181644190  awaiting_deposit -> confirming  Deposit detected
    21:37:46.113918  s_95a807c181644190  confirming -> payout_pending  Deposit fully confirmed
    21:37:46.113783  s_95a807c181644190  awaiting_deposit -> confirming  Deposit detected

Exactly one payout row existed for that swap, so the payout claim guard held and nobody
was paid twice. The damage was a false audit trail -- the record an operator reads to
understand what happened to somebody's money.

TWO CONNECTIONS AND TWO ADAPTER INSTANCES, like tests/test_deposit_rate_limit.py's
two-process tests, because a race between two processes cannot be asserted from one
view of the database. The shape is: let one connection advance the swap, then hand the
OTHER connection the row as it looked before that -- which is exactly what
process_active_swaps() hands refresh_swap_from_chain(), a row read at the top of the
cycle.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT))
sys.path.insert(0, str(REPOSITORY_ROOT / "swap_terminal"))

from db import SCHEMA, apply_migrations, connect_db  # noqa: E402
from valid_addresses import GRC_PAYOUT, SOL_DEPOSIT_ACCOUNT  # noqa: E402

from swap_terminal.services import deposit_service  # noqa: E402
from swap_terminal.services.payout_service import (  # noqa: E402
    _record_broadcast,
    refuse_payout_before_sending,
)
from swap_terminal.services.swap_service import set_swap_status  # noqa: E402

#: DERIVED, NEVER TYPED. tests/test_address_literals_are_valid.py asks for this by name,
#: and three commits in one day were caught hand-writing a chain address into a fixture.
ACCOUNT = SOL_DEPOSIT_ACCOUNT
CONFIG = {
    "AMOUNT_TOLERANCE_PCT": 0.01,
    "SOL_DEPOSIT_ACCOUNT": ACCOUNT,
    "SOL_MIN_CONFIRMATIONS": 3,
}


class StubAdapter:
    """Returns a fixed event list, like the real adapter's shape and nothing else."""

    can_spend = True
    payout_refusal = ""

    def __init__(self, events):
        self.events = events

    def find_deposits_to_address(self, address, tx_limit=None, skip_txids=frozenset()):
        return [dict(e) for e in self.events if e["txid"] not in skip_txids]

    def validate_address(self, address):
        return True


class AdapterThatRacesMidCycle(StubAdapter):
    """Advances a swap on ANOTHER connection during the scan, before the loop reads it.

    THIS IS HOW THE INTERLEAVE IS REPRODUCED IN ONE THREAD, and it is faithful to where
    the window actually is. process_active_swaps() SELECTs the active swaps, then calls
    scan_shared_accounts(), then refreshes each swap from the row it read at the top.
    A second process writing anywhere in that span leaves the loop holding a stale
    status -- so the write is injected during the scan, which is inside the span and is
    the one hook a stub adapter legitimately has.
    """

    def __init__(self, events, *, write):
        super().__init__(events)
        self._write = write
        self.raced = False

    def find_deposits_to_address(self, address, tx_limit=None, skip_txids=frozenset()):
        if not self.raced:
            self.raced = True
            self._write()
        return super().find_deposits_to_address(address, tx_limit=tx_limit, skip_txids=skip_txids)


@pytest.fixture
def db_path(tmp_path):
    return str(tmp_path / "t.db")


@pytest.fixture
def db(db_path):
    conn = connect_db(db_path)
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


def seed_swap(db, swap_id, tag, *, status="awaiting_deposit", min_conf=3):
    db.execute(
        "INSERT INTO swaps (id, quote_id, from_asset, to_asset, deposit_address, deposit_tag,"
        " payout_address, expected_input_amount, quoted_rate, fee_bps, network_fee_reserve,"
        " output_amount_estimate, status, min_confirmations, expires_at, created_at, updated_at)"
        " VALUES (?,'q','SOL','GRC',?,?,?,0.01,8700.0,150,0.01,86.0,?,?,"
        "'2999-01-01T00:00:00+00:00','2026-10-01T00:00:00+00:00','2026-10-01T00:00:00+00:00')",
        (swap_id, ACCOUNT, tag, GRC_PAYOUT, status, min_conf),
    )
    db.commit()


def event(txid, tag, amount=0.01, confirmations=3):
    return {"txid": txid, "vout": tag, "address": ACCOUNT, "amount": amount,
            "confirmations": confirmations}


def audit(db, swap_id):
    return [
        (row["old_status"], row["new_status"])
        for row in db.execute(
            "SELECT old_status, new_status FROM swap_audit_log WHERE swap_id = ? ORDER BY id ASC",
            (swap_id,),
        ).fetchall()
    ]


def status_of(db, swap_id):
    return db.execute("SELECT status FROM swaps WHERE id = ?", (swap_id,)).fetchone()["status"]


# --- the compare-and-swap itself ----------------------------------------------


def test_a_transition_out_of_the_WRONG_status_writes_nothing_at_all(db):
    """The whole defect in four lines: the swap has moved on, so the write declines.

    MUTATION: drop `AND status IS ?` from the UPDATE and this fails -- the swap is
    dragged back to deposit_seen and an audit row claims it came from awaiting_deposit,
    which is the false trail the operator's audit table showed.
    """
    seed_swap(db, "s_moved", 1, status="payout_pending")

    moved = set_swap_status(
        db, "s_moved", "deposit_seen", "Deposit detected", old_status="awaiting_deposit"
    )
    db.commit()

    assert moved is False, "a write against a status the swap has left must decline"
    assert status_of(db, "s_moved") == "payout_pending", "the swap was dragged backwards"
    assert audit(db, "s_moved") == [], "an audit row was written for a transition that did not happen"


def test_a_transition_out_of_the_RIGHT_status_still_writes_both(db):
    """The property the CAS must not cost: the ordinary transition is unchanged."""
    seed_swap(db, "s_ok", 2, status="confirming")

    moved = set_swap_status(
        db, "s_ok", "payout_pending", "Deposit fully confirmed", old_status="confirming"
    )
    db.commit()

    assert moved is True
    assert status_of(db, "s_ok") == "payout_pending"
    assert audit(db, "s_ok") == [("confirming", "payout_pending")]


def test_old_status_None_reads_the_CURRENT_status_and_still_compares(db):
    """`old_status=None` means "whatever it is now", not "skip the check".

    Every non-test caller passes old_status explicitly, so this path is the one a future
    caller takes. It must still be a conditional write: an argument that switches the
    guarantee off is two behaviors behind one name.
    """
    seed_swap(db, "s_read", 3, status="confirming")

    assert set_swap_status(db, "s_read", "payout_pending", "x") is True
    db.commit()
    assert audit(db, "s_read") == [("confirming", "payout_pending")], (
        "the audit row must record the status it actually read, not None"
    )


def test_a_swap_that_does_not_exist_gets_no_audit_row(db):
    """The second thing the bare UPDATE did wrong, and it needed no concurrency at all.

    `UPDATE ... WHERE id = ?` matched nothing for an unknown id, and the audit INSERT ran
    anyway -- so swap_audit_log grew a row, with old_status NULL, for a swap that has
    never existed. swap_audit_log has no foreign key into swaps, so nothing stopped it.
    """
    assert set_swap_status(db, "s_nonexistent", "failed", "nothing to fail") is False
    db.commit()
    assert db.execute("SELECT COUNT(*) AS n FROM swap_audit_log").fetchone()["n"] == 0


# --- the refresh that loses the race ------------------------------------------


def test_a_stale_refresh_neither_advances_the_swap_nor_writes_an_audit_row(db, db_path):
    """TWO CONNECTIONS, ONE SWAP, and the loser must write nothing.

    The winner moves the swap to `confirming` from its own cycle. The loser is then handed
    the row as it looked BEFORE that -- which is what a 60s worker holds while a 15s
    worker runs -- and its first transition is refused.

    MUTATION -- AND THIS IS THE CALL-SITE ONE. Make _advance_to_detected() ignore
    set_swap_status()'s result (`set_swap_status(...)` in place of `if not
    set_swap_status(...): return ...`) and this fails: the loser carries on with a status
    it assigned itself rather than one it wrote, reaches the credit branch, finds the
    database agreeing with its invented `confirming`, and writes a SECOND
    confirming -> payout_pending audit row. That is the duplicated trail, reproduced.
    """
    seed_swap(db, "s_race", 11)
    stale = dict(db.execute("SELECT * FROM swaps WHERE id = 's_race'").fetchone())

    winner = connect_db(db_path)
    deposit_service.process_active_swaps(
        winner, CONFIG, {"SOL": StubAdapter([event("tx_race", 11, confirmations=1)])})
    assert status_of(db, "s_race") == "confirming", "the winner's own cycle"
    assert audit(db, "s_race") == [("awaiting_deposit", "confirming")]

    refreshed = deposit_service.refresh_swap_from_chain(
        db, CONFIG, {"SOL": StubAdapter([event("tx_race", 11, confirmations=3)])}, stale)

    assert status_of(db, "s_race") == "confirming", (
        "the loser advanced a swap from a status it had already left"
    )
    assert audit(db, "s_race") == [("awaiting_deposit", "confirming")], (
        f"the loser wrote an audit row; trail={audit(db, 's_race')}"
    )
    assert refreshed["status"] == "confirming", (
        "the row handed back must be the swap as it IS, not as the loser believed"
    )


def test_the_deferred_credit_still_happens_on_the_NEXT_cycle(db, db_path):
    """THE COST OF DEFERRING, asserted rather than asserted-away.

    Stopping the loser's refresh delays that swap by at most one cycle -- 14.5µfn (17.6s)
    on deposit_watcher -- and a delay that never ended would be a stall, which is worse
    than the duplicate trail it replaces. So the same connection runs a second, ordinary
    cycle and the deposit is credited.
    """
    seed_swap(db, "s_race", 11)
    stale = dict(db.execute("SELECT * FROM swaps WHERE id = 's_race'").fetchone())
    winner = connect_db(db_path)
    deposit_service.process_active_swaps(
        winner, CONFIG, {"SOL": StubAdapter([event("tx_race", 11, confirmations=1)])})
    deposit_service.refresh_swap_from_chain(
        db, CONFIG, {"SOL": StubAdapter([event("tx_race", 11, confirmations=3)])}, stale)

    deposit_service.process_active_swaps(
        db, CONFIG, {"SOL": StubAdapter([event("tx_race", 11, confirmations=3)])})

    assert status_of(db, "s_race") == "payout_pending", "a deferred credit must not be a lost one"
    assert audit(db, "s_race") == [
        ("awaiting_deposit", "confirming"), ("confirming", "payout_pending")
    ], f"one transition each, in order; trail={audit(db, 's_race')}"


def test_the_deposit_events_row_the_loser_upserted_is_KEPT(db, db_path):
    """A refused status write must not discard a correct credit.

    _abandon_on_lost_status_race() COMMITS rather than rolling back, because the upsert and
    the amount figures the loser wrote are the same ones the winner wrote -- absolute
    assignments, not increments. Rolling back would throw away a correct deposit row
    because a status write was declined.
    """
    seed_swap(db, "s_race", 11)
    stale = dict(db.execute("SELECT * FROM swaps WHERE id = 's_race'").fetchone())
    winner = connect_db(db_path)
    deposit_service.process_active_swaps(
        winner, CONFIG, {"SOL": StubAdapter([event("tx_race", 11, confirmations=1)])})

    deposit_service.refresh_swap_from_chain(
        db, CONFIG, {"SOL": StubAdapter([event("tx_race", 11, confirmations=3)])}, stale)

    rows = db.execute(
        "SELECT txid, confirmations FROM deposit_events WHERE swap_id = 's_race'").fetchall()
    assert [dict(r) for r in rows] == [{"txid": "tx_race", "confirmations": 3}], (
        f"the loser's upsert was discarded; rows={[dict(r) for r in rows]}"
    )


def test_a_lost_race_does_not_stop_the_OTHER_swaps_in_the_SAME_cycle(db, db_path):
    """THE REASON A LOST RACE MUST NOT RAISE, driven through the real comprehension.

    refresh_swap_from_chain() is called inside a list comprehension in
    process_active_swaps(). One exception there discards every other swap's processing in
    the cycle, and the worker's `except Exception` prints a FAILED cycle on which nothing
    is credited on ANY chain -- so a routine race between two workers would become an
    outage for every customer.

    The racing write is injected during the scan, which is inside the window between the
    cycle's SELECT and its refreshes. s_race then holds a stale status and loses; s_other
    must still be credited by the same call.
    """
    seed_swap(db, "s_race", 11)
    seed_swap(db, "s_other", 12)
    other = connect_db(db_path)

    def race():
        other.execute(
            "UPDATE swaps SET status = 'confirming' WHERE id = 's_race'")
        other.commit()

    adapter = AdapterThatRacesMidCycle(
        [event("tx_race", 11, confirmations=3), event("tx_other", 12, confirmations=3)],
        write=race,
    )
    processed = deposit_service.process_active_swaps(db, CONFIG, {"SOL": adapter})

    assert adapter.raced, "the injected concurrent write never ran, so nothing was raced"
    assert len(processed) == 2, "a lost race dropped a swap out of the cycle's result"
    assert status_of(db, "s_other") == "payout_pending", (
        "the swap that did NOT lose the race was not credited, which is the outage this "
        "test exists to refuse"
    )
    assert audit(db, "s_race") == [], (
        f"the loser wrote a transition; trail={audit(db, 's_race')}"
    )


# --- the payout path's three call sites ---------------------------------------
#
# These three cannot stop and defer the way the deposit refresh does, because by the time
# they run the swap is already CLAIMED -- claim_swap_for_payout() moved it into 'paying'
# and committed -- and on one of them the money is already on the chain. So the fix there
# is the other half of rule 14: say so, loudly, and do not raise. A mutation that reverts
# any of those call sites to ignoring the return value has no effect on any row, which is
# exactly why the assertion has to be on the output.


def test_a_refused_payout_that_loses_the_status_race_SAYS_SO(db, caplog):
    """The address-refusal path, whose only remaining signal is the line it prints.

    MUTATION: drop the `if not` from refuse_payout_before_sending()'s set_swap_status call
    and this fails. Nothing on any row changes -- the status write declines either way --
    so a test asserting on rows would score the mutation as harmless. What is lost is the
    operator's only notice that a committed payout claim was moved out from under them.
    """
    # NOT 'paying': something moved the swap out of the claim this caller holds.
    seed_swap(db, "s_refused", 20, status="completed")
    swap = dict(db.execute("SELECT * FROM swaps WHERE id = 's_refused'").fetchone())

    with caplog.at_level("ERROR"):
        refuse_payout_before_sending(
            db, swap, SimpleNamespace(why="not a valid GRC address"), "GRC", 86.0)

    assert status_of(db, "s_refused") == "completed", "it must not drag the swap to failed"
    assert audit(db, "s_refused") == [], "no audit row for a transition that did not happen"
    said = "\n".join(record.getMessage() for record in caplog.records)
    assert "could not be marked failed" in said and "s_refused" in said, (
        f"the operator got no notice that the claim had been moved; log was:\n{said}"
    )


def test_a_BROADCAST_payout_that_loses_the_status_race_SAYS_SO(db, caplog):
    """The loudest of the three: the money is on the chain and the status did not move.

    This is also settle_payout.py's state B, where the decline is CORRECT -- it refuses a
    duplicate `failed -> completed` audit row on a swap a previous run already corrected.
    The payouts row and swaps.payout_txid are written before the status, so the correction
    still lands; what must not happen is silence.

    MUTATION: drop the `if not` from _record_broadcast()'s set_swap_status call and this
    fails, for the same reason as the test above -- no row moves either way.
    """
    seed_swap(db, "s_done", 21, status="completed")
    db.execute(
        "INSERT INTO payouts (swap_id, asset, destination_address, amount, txid, status,"
        " created_at, sent_at) VALUES ('s_done','GRC',?,86.0,NULL,'failed',"
        "'2026-10-01T00:00:00+00:00',NULL)",
        (GRC_PAYOUT,),
    )
    db.commit()
    swap = dict(db.execute("SELECT * FROM swaps WHERE id = 's_done'").fetchone())

    with caplog.at_level("ERROR"):
        _record_broadcast(db, swap, 86.0, "tx_broadcast", old_status="failed")

    row = db.execute("SELECT txid, status FROM payouts WHERE swap_id = 's_done'").fetchone()
    assert dict(row) == {"txid": "tx_broadcast", "status": "broadcast"}, (
        "the writes BEFORE the status transition must still land, or state B is not corrected"
    )
    assert audit(db, "s_done") == [], "the duplicate audit row is what the CAS refuses"
    said = "\n".join(record.getMessage() for record in caplog.records)
    assert "was NOT moved to 'completed'" in said and "s_done" in said, (
        f"a broadcast payout whose status did not move said nothing; log was:\n{said}"
    )
