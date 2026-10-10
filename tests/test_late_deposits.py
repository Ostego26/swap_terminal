"""A payment to a FINISHED swap's deposit address is recorded, and nothing else happens.

Role: test (real database on the real schema, the real reconcile pass, stub adapters)
Reads: services/late_deposit_service.py, workers/reconcile_worker.py,
       services/deposit_service.ACTIVE_STATUSES, services/swap_service.TAG_ATTRIBUTED_ASSETS
Writes: a temp database under pytest's tmp_path
Can move funds: no
Mainnet-safe: yes -- no test here opens a socket to any chain.

=============================================================================
THE DEFECT, MEASURED END TO END ON THE OPERATOR'S HOST 2026-10-04
=============================================================================

A payment arriving at a swap's deposit address AFTER that swap completed was
recorded NOWHERE. Not credited, not refused, not marked unattributable.

    swap s_10b5946333612e06   BTC->GRC   status=completed
    expected_input_amount     0.0001
    credited_at               2026-10-04T16:13:09.315366+00:00
    deposit row id=24         vout=1  amount=0.0001  confs=2  txid 7ea61ac9c7038f58...
    payout                    986.89613973 GRC broadcast

The operator then sent a SECOND 0.0001 BTC to the same address, txid
1abce90c0e7fdfe5ebd3155bdd96036a6d119ccd85d1a9aa8b6fc88e9c6b87ff, confirmed on
chain -- their `desk_hot` balance went 10.00110000 -> 10.00120000. Queried
read-only afterwards: `(none)` rows in deposit_events for that txid and `(none)`
rows in unattributable_deposits. 0.0001 BTC in the hot wallet, nothing in the
database aware of it.

Those exact figures are the fixture below, so a reader of a failure sees the
case rather than an abstraction.

THE CAUSE WAS ESTABLISHED BY RUNNING IT, NOT BY READING THE CODE, and the
measurement is in services/late_deposit_service.py's header as a per-status
table: of the eight statuses services/swap_view.py enumerates, a BTC swap's
deposit address is scanned for the three in deposit_service.ACTIVE_STATUSES and
scanned ZERO times for payout_pending, paying, completed, under_review and
failed. test_the_five_finished_statuses_are_all_covered below asserts the
complement from ACTIVE_STATUSES rather than from a list, so a new status is
covered by construction.

=============================================================================
WHAT IS ASSERTED, AND WHY NONE OF IT IS A LOG ASSERTION
=============================================================================

"Verify by behavior, never by reading the code": every test here seeds rows into
the real tables, runs the REAL pass -- reconcile_worker.run_cycle() or
late_deposit_service.reconcile_late_deposits(), never a paraphrase -- and asserts
on the rows actually present. The cycle line is asserted only where the line
ITSELF is the deliverable (rule 14: a count an operator cannot see is not a
record), and even there the row assertions stand beside it.

MUTATION-CHECKED 2026-10-04. Each test below names the production change that
breaks it, and every one was run and confirmed to fail before being written down.
The three the brief required are the window (test_a_payment_outside_the_window_
is_not_recorded and test_the_window_exceeds_the_measured_floor), the idempotency
check (test_a_second_pass_records_nothing) and the guard that stops the settled
swap being modified (test_the_completed_swap_is_not_modified).

THREE OF THE 26 TESTS HERE EXIST BECAUSE A MUTATION SURVIVED, and they are named
because a survivor is the only evidence a suite is green for the wrong reason:

  test_a_second_output_of_the_ALREADY_CREDITED_transaction_is_still_recorded
      weakening late_rows()' accounted-key comparison from (txid, vout) to txid
      alone passed all 23 tests in the first version of this file, including the
      one whose docstring claimed to catch it.
  test_a_zero_value_output_is_not_a_payment
      deleting the `amount <= 0` guard passed all 25 then present; no test seeded
      a zero-value output.
  test_a_deposit_credited_IN_THIS_CYCLE_is_not_called_late
      written after reconcile_worker.run_cycle()'s comment was found to claim the
      ordering of the two passes was load-bearing and pinned by a test. Moving the
      late pass above process_active_swaps() leaves every test here passing, so
      the claim was false twice over; that comment now records why the order
      cannot matter, and this test pins the invariant that is real -- a deposit
      credited during the same cycle is not called late.

AND ONE MUTATION WAS A NO-OP AND IS NOT COUNTED AS A SURVIVOR: appending a
comment to reconcile_late_deposits()' return statement changes no behavior, so
its passing says nothing about this suite. It is named so the figure above is
three rather than four.
"""

from __future__ import annotations

import sys
import time
from datetime import timedelta
from pathlib import Path

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT))
sys.path.insert(0, str(REPOSITORY_ROOT / "swap_terminal"))

from db import PAYMENT_UNIQUE_KEY, SCHEMA, apply_migrations, connect_db  # noqa: E402
from valid_addresses import BTC_REGTEST_DEPOSIT, BTC_REGTEST_SOMEBODY_ELSE, GRC_PAYOUT  # noqa: E402

from swap_terminal.services import late_deposit_service  # noqa: E402
from swap_terminal.services.deposit_service import ACTIVE_STATUSES  # noqa: E402
from swap_terminal.services.helpers import parse_iso, utc_now_iso  # noqa: E402
from swap_terminal.services.swap_service import TAG_ATTRIBUTED_ASSETS  # noqa: E402
from swap_terminal.services.swap_view import STATUS_MEANINGS  # noqa: E402
from swap_terminal.workers import reconcile_worker  # noqa: E402

# The operator's own swap, deposit and late payment, to the digit.
SWAP_ID = "s_10b5946333612e06"
CREDITED_TXID = "7ea61ac9c7038f58"
CREDITED_VOUT = 1
LATE_TXID = "1abce90c0e7fdfe5ebd3155bdd96036a6d119ccd85d1a9aa8b6fc88e9c6b87ff"
AMOUNT = 0.0001
ADDRESS = BTC_REGTEST_DEPOSIT

CONFIG = {
    "AMOUNT_TOLERANCE_PCT": 0.01,
    "QUOTE_TTL_SECONDS": 600,
    "BTC_MIN_CONFIRMATIONS": 2,
    "LTC_MIN_CONFIRMATIONS": 2,
    "GRC_MIN_CONFIRMATIONS": 6,
}


class BtcAdapter:
    """Answers a balance for the inventory refresh and a fixed event list for any scan.

    NOT A MOCK OF late_deposit_service -- it is a stand-in for the CHAIN, at exactly
    the one method services/late_deposit_service.py calls on it. The module under test
    runs unmodified; what is seeded is what the daemon would have said.
    """

    can_spend = True
    payout_refusal = ""

    def __init__(self, events=(), balance=10.0012):
        self.events = [dict(event) for event in events]
        self._balance = balance
        self.scanned: list[str] = []

    def get_balance(self):
        return self._balance

    def validate_address(self, address):
        return True

    def find_deposits_to_address(self, address, tx_limit=500, skip_txids=frozenset()):
        self.scanned.append(address)
        return [dict(event) for event in self.events if event["address"] == address]

    def owns_address(self, address):
        """Not the desk's -- these fixtures all supply a CUSTOMER payout address.

        Added 2026-10-04 with the ownership gate in services/swap_service.
        refuse_unusable_payout_address(). A destination stub without it raises
        AttributeError, so the gate could not be exercised and every create_swap
        test failed on the harness instead of on its own subject. False is the
        honest default here; a test that wants the refusal returns True.
        """
        return False


def event(txid, vout, *, amount=AMOUNT, confirmations=2, address=ADDRESS):
    return {"txid": txid, "vout": vout, "address": address, "amount": amount,
            "confirmations": confirmations}


CREDITED_EVENT = event(CREDITED_TXID, CREDITED_VOUT, confirmations=20)
LATE_EVENT = event(LATE_TXID, 0)


@pytest.fixture
def db(tmp_path):
    conn = connect_db(str(tmp_path / "late.db"), create=True)
    conn.executescript(SCHEMA)
    apply_migrations(conn)
    conn.execute(
        "INSERT INTO quotes (id, from_asset, to_asset, input_amount, quoted_rate, fee_bps,"
        " network_fee_reserve, output_amount_estimate, expires_at, created_at)"
        " VALUES ('q','BTC','GRC',0.0001,9868961.0,150,0.01,986.89613973,"
        "'2999-01-01T00:00:00+00:00','2026-10-04T00:00:00+00:00')"
    )
    conn.commit()
    return conn


def seed_finished_swap(db, *, status="completed", updated_at=None, address=ADDRESS,
                       with_deposit_row=True):
    """The operator's swap, as it stood after its payout: finished, with its deposit credited.

    `updated_at` DEFAULTS TO NOW because the window is measured from it, and a fixture
    with a hardcoded timestamp would start passing or failing on its own as the clock
    moved past the window -- the fixture-rot failure tests/conftest.py's UNTRADED_ASSET
    comment records in a different form.
    """
    updated = updated_at or utc_now_iso()
    db.execute(
        "INSERT INTO swaps (id, quote_id, from_asset, to_asset, deposit_address,"
        " payout_address, expected_input_amount, actual_input_amount, quoted_rate, fee_bps,"
        " network_fee_reserve, output_amount_estimate, status, min_confirmations,"
        " deposit_txid, payout_txid, expires_at, created_at, updated_at, credited_at,"
        " completed_at) VALUES (?,'q','BTC','GRC',?,?,?,?,9868961.0,150,0.01,986.89613973,"
        "?,2,?,'grc_payout_txid','2999-01-01T00:00:00+00:00','2026-10-04T16:12:00+00:00',"
        "?,'2026-10-04T16:13:09.315366+00:00','2026-10-04T16:14:00+00:00')",
        (SWAP_ID, address, GRC_PAYOUT, AMOUNT, AMOUNT, status, CREDITED_TXID, updated),
    )
    if with_deposit_row:
        db.execute(
            "INSERT INTO deposit_events (swap_id, asset, txid, vout, address, amount,"
            " confirmations, first_seen_at, last_seen_at, credited_at)"
            " VALUES (?,'BTC',?,?,?,?,2,'2026-10-04T16:13:00+00:00',"
            "'2026-10-04T16:13:09+00:00','2026-10-04T16:13:09.315366+00:00')",
            (SWAP_ID, CREDITED_TXID, CREDITED_VOUT, address, AMOUNT),
        )
    db.commit()


def run_pass(db, adapter, *, cycle=1):
    """The REAL worker cycle, returning its line. Not a paraphrase of it."""
    return reconcile_worker.run_cycle(db, CONFIG, {"BTC": adapter}, cycle, time.monotonic())


def late_rows_in(db):
    return db.execute("SELECT * FROM late_deposits ORDER BY id").fetchall()


# --- the hole, and that it is closed -----------------------------------------


def test_the_operators_late_payment_is_recorded(db):
    """0.0001 BTC to a completed swap's address becomes one late_deposits row.

    THE MEASUREMENT THIS INVERTS. Before 2026-10-04 the same seeded state produced
    `(none)` rows in deposit_events and `(none)` in unattributable_deposits for txid
    1abce90c0e7fdfe5... -- verified by running the real process_active_swaps() with a
    counting adapter, which was never called at all because the swap is not in
    deposit_service.ACTIVE_STATUSES.

    MUTATION -- THE SCAN WINDOW, which is the first of the three the brief required.
    Set late_deposit_service.LATE_DEPOSIT_WINDOW_SECONDS to 0 and this FAILS with 0
    rows: the cutoff becomes `now`, the swap's updated_at is strictly before it, and
    late_scan_targets() returns nothing, so the payment is missed exactly as it was
    before this module existed. Confirmed failing 2026-10-04, then restored.

    MUTATION -- THE STATUS PREDICATE. Change _TARGETS_SQL's `status NOT IN` to
    `status IN` and this FAILS with 0 rows while the three ACTIVE statuses would start
    being scanned instead -- the inversion that would look like a working feature on a
    host with an open swap. Confirmed failing, then restored.
    """
    seed_finished_swap(db)
    adapter = BtcAdapter([CREDITED_EVENT, LATE_EVENT])

    run_pass(db, adapter)

    rows = late_rows_in(db)
    assert len(rows) == 1, f"expected exactly one late_deposits row, got {len(rows)}"
    row = rows[0]
    assert row["txid"] == LATE_TXID
    assert row["vout"] == 0
    assert row["swap_id"] == SWAP_ID
    # THE STATUS AT RECORDING TIME, which db.py's column comment is about: a row read a
    # week later has to say whether the money arrived after a COMPLETED payout or a
    # FAILED one.
    assert row["swap_status"] == "completed"
    assert row["asset"] == "BTC"
    assert row["address"] == ADDRESS
    assert row["amount"] == pytest.approx(AMOUNT)
    assert row["confirmations"] == 2
    # UNRESOLVED ON ARRIVAL. Nothing in this module may close a row; only a person can.
    assert row["resolved_at"] is None
    assert row["resolution_note"] is None


def test_the_swaps_own_credited_deposit_is_not_recorded_as_late(db):
    """The payment already in deposit_events is accounted for and must not be re-recorded.

    This is the case that would have made the feature useless rather than merely absent:
    a pass that recorded every output it found would write a late_deposits row for the
    swap's own 0.0001 BTC on the first cycle after every single completed swap, and the
    table an operator is supposed to act on would be entirely noise.

    MUTATION -- THE ACCOUNTED SET. Make accounted_keys() return frozenset() and this
    FAILS with 2 rows instead of 1: the credited txid 7ea61ac9c7038f58 at vout=1 is
    recorded as late alongside the genuine one. Confirmed failing, then restored.

    MUTATION -- THE KEY'S SECOND HALF. Compare on txid alone in late_rows() and this test
    still passes. SO DOES EVERY OTHER TEST THAT WAS IN THIS FILE WHEN IT WAS FIRST
    WRITTEN -- that mutation SURVIVED, measured 2026-10-04, and the claim this paragraph
    used to make (that test_two_outputs_in_one_transaction_are_two_rows catches it) was
    false: both of that test's outputs share a txid that is in no deposit_events row, so
    the weakened comparison gives the same answer. The test that catches it is
    test_a_second_output_of_the_ALREADY_CREDITED_transaction_is_still_recorded, written
    for that reason.
    """
    seed_finished_swap(db)
    adapter = BtcAdapter([CREDITED_EVENT, LATE_EVENT])

    run_pass(db, adapter)

    rows = late_rows_in(db)
    assert len(rows) == 1
    assert rows[0]["txid"] == LATE_TXID
    assert {row["txid"] for row in rows} == {LATE_TXID}, "the credited deposit was re-recorded"


def test_the_completed_swap_is_not_modified(db):
    """Recording a late deposit changes NOTHING about the settled swap. Row-for-row.

    THE INVARIANT THAT MATTERS MOST HERE, and it is the reason this records into its own
    table rather than into deposit_events: that table is what
    deposit_service.refresh_swap_from_chain() SUMS to decide whether a payout may be
    released, so a row there against a completed swap would either halt a finished swap
    or re-arm a payout against money nobody decided to accept. Rewriting a settled swap
    is what migrate_deposit_vouts.py refuses to do, for the same reason.

    ASSERTED AS A WHOLE-ROW COMPARISON rather than as a status check, because `status`
    is only the most visible of fourteen columns a careless write could touch --
    actual_input_amount, credited_at, completed_at and payout_txid are all things a
    "helpful" re-credit would move.

    MUTATION -- THE GUARD, which is the third of the three the brief required. There is
    no `if` to invert here: the guarantee is structural, so the mutation has to be the
    write that the structure forbids. Adding
    `db.execute("UPDATE swaps SET status='payout_pending' WHERE id=?", (row.swap_id,))`
    to late_deposit_service.record()'s loop -- the single most plausible "fix" somebody
    would write for a late deposit -- makes this test FAIL on the status column and on
    nothing else, and leaves every other test in this file passing. Confirmed failing,
    then restored. The second form, adding an upsert_deposit_event() call beside it,
    fails this test on deposit_events and fails
    test_the_operators_late_payment_is_recorded on the next pass as well.
    """
    seed_finished_swap(db)
    before_swap = dict(db.execute("SELECT * FROM swaps WHERE id = ?", (SWAP_ID,)).fetchone())
    before_events = [dict(row) for row in db.execute(
        "SELECT * FROM deposit_events ORDER BY id").fetchall()]
    before_audit = [dict(row) for row in db.execute(
        "SELECT * FROM swap_audit_log ORDER BY id").fetchall()]
    before_payouts = [dict(row) for row in db.execute(
        "SELECT * FROM payouts ORDER BY id").fetchall()]

    run_pass(db, BtcAdapter([CREDITED_EVENT, LATE_EVENT]))

    assert len(late_rows_in(db)) == 1, "the late deposit was not recorded, so this proves nothing"
    after_swap = dict(db.execute("SELECT * FROM swaps WHERE id = ?", (SWAP_ID,)).fetchone())
    assert after_swap == before_swap, "the settled swap row was modified"
    assert [dict(row) for row in db.execute(
        "SELECT * FROM deposit_events ORDER BY id").fetchall()] == before_events
    assert [dict(row) for row in db.execute(
        "SELECT * FROM swap_audit_log ORDER BY id").fetchall()] == before_audit
    assert [dict(row) for row in db.execute(
        "SELECT * FROM payouts ORDER BY id").fetchall()] == before_payouts
    # And the unattributable table is untouched: this is a DIFFERENT case and must not
    # quietly land in the table whose own schema comment says it holds no swap_id.
    assert db.execute("SELECT COUNT(*) AS n FROM unattributable_deposits").fetchone()["n"] == 0


def test_a_second_pass_records_nothing(db):
    """Idempotence: the same payment seen twice is one row, and the second count is 0.

    THE POLL IS WHY THIS MATTERS. reconcile_worker runs every 60s against a window of
    86400s, so a late deposit is seen up to 1440 times and a pass that accumulated a row
    per sighting would turn one 0.0001 BTC payment into a day's worth of rows.

    MUTATION -- THE IDEMPOTENCY CHECK, the second of the three the brief required.
    Replace record()'s first-sighting test with `inserted += 1` unconditionally and this
    FAILS on the second pass's returned count (1, not 0) and on the cycle line's
    `late_deposits=1`, while the ROW count stays 1 because the UNIQUE(asset, txid, vout)
    constraint and the ON CONFLICT clause still hold. Confirmed failing, then restored.

    MUTATION -- THE CONSTRAINT'S OTHER HALF. Change the ON CONFLICT target to
    `(asset, txid)` and this test fails too, on an sqlite3 IntegrityError rather than on a
    count, because the UNIQUE index it names does not exist;
    test_two_outputs_in_one_transaction_are_two_rows is what catches the version where
    both are changed together. Confirmed 2026-10-04.

    last_seen_at ADVANCING IS ASSERTED, not just the absence of a new row, because a
    frozen last_seen_at is how an operator would be unable to tell "still there" from
    "the scan stopped running" -- the same reading
    unattributable_deposit_service.record() gives for the same column.
    """
    seed_finished_swap(db)
    adapter = BtcAdapter([CREDITED_EVENT, LATE_EVENT])

    first = late_deposit_service.reconcile_late_deposits(
        db, CONFIG, {"BTC": adapter}, now="2026-10-04T17:00:00+00:00")
    second = late_deposit_service.reconcile_late_deposits(
        db, CONFIG, {"BTC": adapter}, now="2026-10-04T17:01:00+00:00")

    assert first.recorded == 1
    assert second.recorded == 0, "the second pass recorded the same payment again"
    rows = late_rows_in(db)
    assert len(rows) == 1, f"expected one row after two passes, got {len(rows)}"
    assert rows[0]["first_seen_at"] == "2026-10-04T17:00:00+00:00"
    assert rows[0]["last_seen_at"] == "2026-10-04T17:01:00+00:00"


def test_a_resolved_row_is_not_reopened_by_a_later_pass(db):
    """A person's resolution survives the poll. The scan may touch the row, never reopen it.

    Same rule and same reason as unattributable_deposit_service.record(): a later
    sighting of the same output is not new information about who sent it, and clearing
    resolved_at would make the scan undo a human's decision every sixty seconds.

    MUTATION. Add `resolved_at = NULL, resolution_note = NULL` to record()'s ON CONFLICT
    DO UPDATE and this FAILS on both columns. Confirmed failing, then restored.
    """
    seed_finished_swap(db)
    adapter = BtcAdapter([CREDITED_EVENT, LATE_EVENT])
    late_deposit_service.reconcile_late_deposits(
        db, CONFIG, {"BTC": adapter}, now="2026-10-04T17:00:00+00:00")
    db.execute(
        "UPDATE late_deposits SET resolved_at = ?, resolution_note = ? WHERE txid = ?",
        ("2026-10-04T18:00:00+00:00", "refunded by hand, see grc txid ...", LATE_TXID),
    )
    db.commit()

    late_deposit_service.reconcile_late_deposits(
        db, CONFIG, {"BTC": adapter}, now="2026-10-04T19:00:00+00:00")

    row = late_rows_in(db)[0]
    assert row["resolved_at"] == "2026-10-04T18:00:00+00:00"
    assert row["resolution_note"] == "refunded by hand, see grc txid ..."
    # And the swap_status it was first seen under is not rewritten either -- that column
    # exists to hold the status AT FIRST SIGHTING (db.py's comment on it).
    assert row["swap_status"] == "completed"


# --- the window ---------------------------------------------------------------


def test_a_payment_outside_the_window_is_not_recorded(db):
    """A swap that finished longer ago than the window is not re-read. The window IS a window.

    ASSERTED SO THE WINDOW IS NOT VACUOUSLY WIDE. Every other test here would pass if
    the cutoff were the beginning of time, which is the mutation that looks like working
    harder rather than like a bug -- and a window of everything is a scan cost that grows
    without bound as the swaps table does (LATE_DEPOSIT_WINDOW_SECONDS' own comment
    reasons about exactly that denominator).

    MUTATION -- THE CUTOFF'S DIRECTION. Change _cutoff_iso() to ADD the window instead of
    subtracting it and this test PASSES while
    test_the_operators_late_payment_is_recorded FAILS: adding puts the cutoff an hour in
    the future so nothing is ever in range. Change _TARGETS_SQL's `updated_at >= ?` to
    `updated_at <= ?` and THIS test fails (the stale swap is recorded) while that one
    also fails -- the inversion is caught from both sides, which is why both tests
    exist. Both confirmed failing in the stated direction, then restored.
    """
    stale = (parse_iso(utc_now_iso())
             - timedelta(seconds=late_deposit_service.LATE_DEPOSIT_WINDOW_SECONDS + 60)).isoformat()
    seed_finished_swap(db, updated_at=stale)
    adapter = BtcAdapter([CREDITED_EVENT, LATE_EVENT])

    result = late_deposit_service.reconcile_late_deposits(
        db, CONFIG, {"BTC": adapter}, now=utc_now_iso())

    assert result.targets == 0, "a swap older than the window was still a scan target"
    assert adapter.scanned == [], "the chain was scanned for a swap outside the window"
    assert late_rows_in(db) == []


def test_the_window_exceeds_the_measured_floor():
    """The chosen window covers an in-flight payment on every address-attributed asset.

    THE FLOOR, DERIVED FROM THE TREE AND NOT FROM A FEELING:

        Config.QUOTE_TTL_SECONDS + min_confirmations * SECONDS_PER_BLOCK[asset]

        asset  TTL   min_conf  block target   floor
        BTC    600       2         600s        1800s   <- the binding one
        LTC    600       2         150s         900s
        GRC    600       6          90s        1140s

    The first term is how long a customer is still acting on a LIVE deposit instruction
    (QUOTE_TTL_SECONDS defaults to 600). The second is the longest a payment sent at the
    instant a swap stopped being watched can take to become discoverable, from that
    asset's own *_MIN_CONFIRMATIONS and modules/htlc_timelock.SECONDS_PER_BLOCK. A window
    below 1800s provably misses a BTC payment that was already in flight, which is
    precisely "their first send was slow so they resent".

    86400s is 48x the binding floor, and the multiple is NOT derived from anything -- the
    case this module exists for is a human one and there is no measurement of human
    latency in this tree (rule 3: say so rather than let an estimate harden into a fact).
    What this test pins is the half that IS derived.

    IT IS A GATE RATHER THAN A RATCHET (rule 19). It fails if somebody raises
    BTC_MIN_CONFIRMATIONS past 142 without widening the window, or corrects a block
    interval upward -- both of which would silently narrow the coverage.

    MUTATION. Set LATE_DEPOSIT_WINDOW_SECONDS to 1700 and this FAILS naming BTC; set it
    to 1800 and it fails too, since the floor is the minimum a window must EXCEED rather
    than meet. Confirmed failing, then restored.
    """
    assets = late_deposit_service.address_attributed_assets(
        {"BTC": object(), "LTC": object(), "GRC": object()})
    floors = late_deposit_service.discovery_floor_seconds(CONFIG, assets)

    assert sorted(floors) == sorted(assets), (
        f"an address-attributed asset has no known block interval: "
        f"{sorted(set(assets) - set(floors))} -- modules/htlc_timelock.SECONDS_PER_BLOCK "
        f"needs it before a window can be justified for it"
    )
    assert floors == {"BTC": 1800.0, "GRC": 1140.0, "LTC": 900.0}
    for asset, floor in floors.items():
        assert floor < late_deposit_service.LATE_DEPOSIT_WINDOW_SECONDS, (
            f"the window is {late_deposit_service.LATE_DEPOSIT_WINDOW_SECONDS}s, which does not "
            f"exceed {asset}'s discovery floor of {floor}s"
        )


# --- what gets scanned, and what does not -------------------------------------


@pytest.mark.parametrize("status", sorted(
    set(STATUS_MEANINGS) - set(ACTIVE_STATUSES)))
def test_the_five_finished_statuses_are_all_covered(db, status):
    """Every status that is NOT active gets its deposit address re-read. Derived, not listed.

    THE COMPLEMENT OF deposit_service.ACTIVE_STATUSES, taken from
    services/swap_view.STATUS_MEANINGS so the parametrization cannot go stale: a status
    added to the system arrives here as a new case automatically, which is the property a
    hand-written list of five would lose (rule 8's bug with a delay on it -- and the
    delay would be a status whose late deposits are invisible exactly as `completed`'s
    were).

    Measured 2026-10-04 as the five this covers: failed, paying, payout_pending,
    under_review and completed. `under_review` is the one worth naming -- that is the
    HALT path, reached precisely when a deposit did not match its quote, which is the
    state a customer is most likely to answer by sending again.

    MUTATION. Spell the five statuses as a literal tuple in _TARGETS_SQL instead of
    `NOT IN (ACTIVE_STATUSES)` and this passes -- it would, they agree today -- but
    remove any ONE of them from that literal and the corresponding parametrized case
    FAILS by name. Confirmed with `completed` removed, then restored.
    """
    seed_finished_swap(db, status=status)
    adapter = BtcAdapter([CREDITED_EVENT, LATE_EVENT])

    result = late_deposit_service.reconcile_late_deposits(
        db, CONFIG, {"BTC": adapter}, now=utc_now_iso())

    assert result.targets == 1, f"status {status!r} was not a scan target"
    assert result.recorded == 1, f"the late payment was not recorded for status {status!r}"
    assert late_rows_in(db)[0]["swap_status"] == status


@pytest.mark.parametrize("status", sorted(ACTIVE_STATUSES))
def test_an_active_swap_is_left_entirely_to_the_crediting_path(db, status):
    """A swap still in ACTIVE_STATUSES is not touched here. Its own refresh owns it.

    THIS IS THE DUPLICATE-WRITER REFUSAL, and it is the same one
    deposit_service.reconcile_shared_accounts() makes: "events matching an ACTIVE swap
    are left entirely alone, even though this function can see them". A second writer
    deciding about the same payment is rule 8's duplicate on the path where the two
    copies disagreeing means somebody is paid twice -- or, here, that an arriving deposit
    is simultaneously being credited and filed as unaccounted-for money.

    MUTATION. Drop the `status NOT IN` clause from _TARGETS_SQL entirely and this FAILS
    for all three statuses: the deposit that is mid-confirmation is recorded as a late
    deposit while the crediting path is still working on it. Confirmed failing, then
    restored.
    """
    seed_finished_swap(db, status=status, with_deposit_row=False)
    adapter = BtcAdapter([LATE_EVENT])

    result = late_deposit_service.reconcile_late_deposits(
        db, CONFIG, {"BTC": adapter}, now=utc_now_iso())

    assert result.targets == 0, f"an active swap ({status!r}) was a late-scan target"
    assert adapter.scanned == []
    assert late_rows_in(db) == []


def test_a_tag_attributed_asset_is_not_scanned_here(db):
    """XRP and SOL are the shared-account path's job, and that path already covers them.

    ESTABLISHED BY RUNNING IT, 2026-10-04, not assumed from the code: a payment carrying
    a FINISHED swap's DestinationTag IS recorded today. Seeding one XRP swap per status
    with a late payment carrying its tag, the real process_active_swaps() wrote one
    unattributable_deposits row for each of payout_pending, paying, completed,
    under_review and failed, credited nothing, and changed no swap -- because
    reconcile_shared_accounts() scans the shared account once per cycle whether or not
    anything is open and its `claimed` map is EVERY swap on the asset rather than the
    active ones. So there is no hole there to close, and a second pass over the same
    account would be rule 8's duplicate.

    DERIVED BY SUBTRACTION FROM swap_service.TAG_ATTRIBUTED_ASSETS rather than by listing
    BTC/LTC/GRC, so a chain moved into or out of that set follows automatically.

    MUTATION. Make address_attributed_assets() return `sorted(adapters)` -- dropping the
    subtraction -- and this FAILS: XRP becomes a scan target and the payment is recorded
    in BOTH tables, two rows asserting different things about one payment. Confirmed
    failing, then restored.
    """
    assert TAG_ATTRIBUTED_ASSETS, "the subtraction below would be vacuous"
    assets = late_deposit_service.address_attributed_assets(
        {asset: object() for asset in ({"BTC", "LTC", "GRC"} | set(TAG_ATTRIBUTED_ASSETS))})

    assert set(assets).isdisjoint(TAG_ATTRIBUTED_ASSETS)
    assert set(assets) == {"BTC", "GRC", "LTC"}


def test_a_payment_to_somebody_elses_address_is_not_this_swaps_late_deposit(db):
    """The address is re-checked against the swap's own, even though the adapter was asked for it.

    chains/base.find_deposits_to_address() is a whole-wallet `listtransactions` filtered
    in Python, so what comes back is whatever that filter let through -- and a row written
    against the wrong address here would attribute a stranger's payment to a customer's
    finished swap, which is the one error on this table that costs a conversation rather
    than a query.

    MUTATION. Delete the address comparison from late_rows() and this FAILS with a row
    whose `address` is BTC_REGTEST_SOMEBODY_ELSE against swap s_10b5946333612e06.
    Confirmed failing, then restored.
    """
    seed_finished_swap(db)
    stranger = event("stranger_txid", 0, address=BTC_REGTEST_SOMEBODY_ELSE)

    class MisfilteringAdapter(BtcAdapter):
        """Asked for ADDRESS, answers with a row for a DIFFERENT address.

        That is the shape a mis-filtered whole-wallet `listtransactions` produces, so the
        stub reproduces the failure rather than the happy path -- BtcAdapter's own filter
        would never emit it, which is exactly why it cannot be the one used here.
        """

        def find_deposits_to_address(self, address, tx_limit=500, skip_txids=frozenset()):
            self.scanned.append(address)
            return [dict(CREDITED_EVENT), dict(stranger)]

    adapter = MisfilteringAdapter()

    result = late_deposit_service.reconcile_late_deposits(
        db, CONFIG, {"BTC": adapter}, now=utc_now_iso())

    assert result.recorded == 0
    assert late_rows_in(db) == []


def test_an_unconfirmed_payment_waits_for_a_confirmation(db):
    """A zero-confirmation sighting is not yet money, so it is not yet a record.

    MIN_RECORDED_CONFIRMATIONS carries the argument: a transaction with no confirmations
    can still be replaced or dropped, and a permanent row asserting the desk holds money
    it may never receive is worse than a row that appears a block later -- the whole point
    of the row is that a person acts on it. Nothing is lost by waiting: the loop runs every
    60s against an 86400s window, so a payment that confirms is recorded with 1439 passes
    to spare.

    MUTATION. Set MIN_RECORDED_CONFIRMATIONS to 0 and this FAILS with a row at
    confirmations=0. Confirmed failing, then restored.
    """
    seed_finished_swap(db)
    adapter = BtcAdapter([CREDITED_EVENT, event(LATE_TXID, 0, confirmations=0)])

    first = late_deposit_service.reconcile_late_deposits(
        db, CONFIG, {"BTC": adapter}, now="2026-10-04T17:00:00+00:00")
    assert first.recorded == 0
    assert late_rows_in(db) == []

    # And the SAME payment, one confirmation later, IS recorded -- so this is a wait and
    # not a refusal.
    adapter.events = [dict(CREDITED_EVENT), event(LATE_TXID, 0, confirmations=1)]
    second = late_deposit_service.reconcile_late_deposits(
        db, CONFIG, {"BTC": adapter}, now="2026-10-04T17:01:00+00:00")
    assert second.recorded == 1
    assert late_rows_in(db)[0]["confirmations"] == 1


def test_a_zero_value_output_is_not_a_payment(db):
    """An output of 0 is not money, so it is not a record of money.

    ADDED BECAUSE A MUTATION SURVIVED. Deleting late_rows()' `amount <= 0` guard left all
    25 tests then in this file passing, because none of them seeded a zero-value output --
    a real gap rather than a no-op mutation, since the guard does change what is written
    for such an event. Recorded here rather than silently patched.

    IT IS NOT DEAD BELT-AND-BRACES EITHER, and the distinction matters for rule 9. The
    Bitcoin-family adapter filters `amount <= 0` on the whole-wallet `listtransactions`
    row before it ever calls _extract_matching_vouts(), so one guard is upstream -- but
    what comes back from that extraction is a list of individual OUTPUTS, and a
    transaction with a positive total can carry an output of zero to the same address. So
    the two guards are about different quantities: a transaction's net receive, and one
    output's value. db.py's `late_deposits` is a record of money the desk holds, and a row
    saying 0.00000000 BTC arrived would send a person to look at nothing.

    MUTATION. Replace the `amount <= 0` check with `if False` and this FAILS with a row
    at amount 0.0. Confirmed failing 2026-10-04, then restored.
    """
    seed_finished_swap(db)
    adapter = BtcAdapter([CREDITED_EVENT, event(LATE_TXID, 0, amount=0.0, confirmations=6)])

    result = late_deposit_service.reconcile_late_deposits(
        db, CONFIG, {"BTC": adapter}, now=utc_now_iso())

    assert result.recorded == 0
    assert late_rows_in(db) == []


def test_two_outputs_in_one_transaction_are_two_rows(db):
    """One transaction paying the address twice is two payments, so two rows.

    WHY THE KEY IS (asset, txid, vout) AND NOT (asset, txid), which is the other way round
    from unattributable_deposits and db.py's comment on both tables says why: there `vout`
    carries the integer discriminator and the row exists BECAUSE no discriminator was
    found, so it cannot be part of the key; here it is a real output index on a UTXO chain.

    MUTATION. Change record()'s ON CONFLICT target and the UNIQUE constraint to
    (asset, txid) and this FAILS with 1 row instead of 2 -- the second output silently
    UPSERTs over the first and 0.0002 BTC of the desk's holdings disappears from the
    record. Confirmed failing, then restored.

    IT DOES *NOT* CATCH dropping `vout` from late_rows()' accounted-key tuple, which an
    earlier version of this docstring claimed it did. See
    test_a_second_output_of_the_ALREADY_CREDITED_transaction_is_still_recorded.
    """
    seed_finished_swap(db)
    adapter = BtcAdapter([
        CREDITED_EVENT,
        event(LATE_TXID, 0),
        event(LATE_TXID, 3, amount=0.0002),
    ])

    result = late_deposit_service.reconcile_late_deposits(
        db, CONFIG, {"BTC": adapter}, now=utc_now_iso())

    assert result.recorded == 2
    rows = late_rows_in(db)
    assert sorted((row["vout"], row["amount"]) for row in rows) == [
        (0, pytest.approx(AMOUNT)), (3, pytest.approx(0.0002)),
    ]


def test_a_second_output_of_the_ALREADY_CREDITED_transaction_is_still_recorded(db):
    """One transaction, one output credited and another not: the uncredited one is late.

    THIS TEST EXISTS BECAUSE A MUTATION SURVIVED, and that is recorded here rather than
    quietly fixed. late_rows() skips an output whose (txid, vout) is already in
    deposit_events. Weakening that to compare on `txid` ALONE -- the obvious-looking
    simplification -- was mutated in on 2026-10-04 and every one of the 23 tests then in
    this file still passed, including the one whose docstring claimed to catch it
    (test_two_outputs_in_one_transaction_are_two_rows). It could not: both of its outputs
    share a txid that appears in no deposit_events row, so comparing on txid alone gives
    the same answer.

    WHAT THE WEAKENED FORM LOSES IS MONEY FROM THE RECORD. The operator's own deposit is
    txid 7ea61ac9c7038f58 at vout=1. A transaction that pays the deposit address at TWO
    outputs -- one credited, one not -- is not hypothetical in this tree: the whole reason
    deposit_events is keyed on (asset, txid, vout) and the reason
    deposit_vout_artifact.multi_vout_groups() exists is that one transaction really does
    contribute several rows here. Under the txid-only comparison the uncredited output is
    skipped forever, which is the exact defect this module was written to close, reached
    by a different road.

    MUTATION. Change late_rows()' check to `if txid in {t for t, _v in accounted}` and
    this FAILS with 0 rows. Confirmed failing 2026-10-04, then restored -- and confirmed
    SURVIVING against the file as it stood before this test was added.
    """
    seed_finished_swap(db)
    second_output = event(CREDITED_TXID, 5, amount=0.0003, confirmations=20)
    adapter = BtcAdapter([CREDITED_EVENT, second_output])

    result = late_deposit_service.reconcile_late_deposits(
        db, CONFIG, {"BTC": adapter}, now=utc_now_iso())

    assert result.recorded == 1, (
        "the uncredited second output of an already-credited transaction was skipped"
    )
    row = late_rows_in(db)[0]
    assert (row["txid"], row["vout"]) == (CREDITED_TXID, 5)
    assert row["amount"] == pytest.approx(0.0003)
    # The credited output at vout=1 is still not re-recorded, so the narrower comparison
    # did not become a wider one.
    assert len(late_rows_in(db)) == 1


def test_one_scan_per_address_not_one_per_swap(db):
    """Two swaps sharing a deposit address cost one chain read, not two.

    Should not happen on an address-attributed chain -- swap_service derives a fresh
    address per swap -- but "should not happen" is not a reason to make the cost
    quadratic if it does, which is the same per-cycle scan cache
    deposit_service.scan_shared_accounts() keeps for the tag-attributed chains.

    MUTATION. Remove the `if key not in scanned` guard in reconcile_late_deposits() and
    this FAILS with 2 scans. Confirmed failing, then restored. The ROW count stays 1
    either way, which is why this asserts the scan list rather than the table.
    """
    seed_finished_swap(db)
    db.execute(
        "INSERT INTO swaps (id, quote_id, from_asset, to_asset, deposit_address,"
        " payout_address, expected_input_amount, quoted_rate, fee_bps, network_fee_reserve,"
        " output_amount_estimate, status, min_confirmations, expires_at, created_at,"
        " updated_at) VALUES ('s_second','q','BTC','GRC',?,?,0.0001,9868961.0,150,0.01,"
        "986.89613973,'failed',2,'2999-01-01T00:00:00+00:00','2026-10-04T00:00:00+00:00',?)",
        (ADDRESS, GRC_PAYOUT, utc_now_iso()),
    )
    db.commit()
    adapter = BtcAdapter([CREDITED_EVENT, LATE_EVENT])

    result = late_deposit_service.reconcile_late_deposits(
        db, CONFIG, {"BTC": adapter}, now=utc_now_iso())

    assert result.targets == 2
    assert result.addresses == 1
    assert adapter.scanned == [ADDRESS], f"scanned {len(adapter.scanned)} times for one address"
    assert len(late_rows_in(db)) == 1


def test_an_adapter_that_raises_does_not_lose_the_pass(db):
    """One chain being unreachable must not stop the others being recorded.

    This pass is the ONLY thing that records a late deposit, so a cycle that died on GRC
    being briefly unreachable would silently stop recording BTC's -- which is the shape
    workers/common.CycleFailures was written for after a devnet DNS lookup killed a
    worker on 2026-10-01.

    THE BROAD CATCH IS LEGITIMATE BY RULE 12'S OWN TEST and this is what checks it: the
    caller CAN tell the failure from a real answer, because the count returned is rows
    WRITTEN rather than a zero standing in for "nothing arrived", and the exception is
    logged with the asset and the address.

    MUTATION. Change the handler to `return LatePass(0, 0, 0)` and this FAILS: the GRC
    failure erases the BTC row's count. Confirmed failing, then restored.
    """
    seed_finished_swap(db)
    db.execute("INSERT INTO quotes (id, from_asset, to_asset, input_amount, quoted_rate,"
               " fee_bps, network_fee_reserve, output_amount_estimate, expires_at, created_at)"
               " VALUES ('q2','GRC','BTC',100.0,0.0001,150,0.0001,0.01,"
               "'2999-01-01T00:00:00+00:00','2026-10-04T00:00:00+00:00')")
    db.execute(
        "INSERT INTO swaps (id, quote_id, from_asset, to_asset, deposit_address,"
        " payout_address, expected_input_amount, quoted_rate, fee_bps, network_fee_reserve,"
        " output_amount_estimate, status, min_confirmations, expires_at, created_at,"
        " updated_at) VALUES ('s_grc','q2','GRC','BTC',?,?,100.0,0.0001,150,0.0001,0.01,"
        "'completed',6,'2999-01-01T00:00:00+00:00','2026-10-04T00:00:00+00:00',?)",
        (GRC_PAYOUT, ADDRESS, utc_now_iso()),
    )
    db.commit()

    class Broken:
        def find_deposits_to_address(self, address, tx_limit=500, skip_txids=frozenset()):
            raise RuntimeError("gridcoin daemon unreachable")

    result = late_deposit_service.reconcile_late_deposits(
        db, CONFIG, {"BTC": BtcAdapter([CREDITED_EVENT, LATE_EVENT]), "GRC": Broken()},
        now=utc_now_iso())

    assert result.recorded == 1, "the BTC row was lost to the GRC failure"
    assert [row["asset"] for row in late_rows_in(db)] == ["BTC"]


def test_a_deposit_credited_IN_THIS_CYCLE_is_not_called_late(db):
    """A swap that leaves ACTIVE_STATUSES during the same cycle keeps its own deposit.

    THE DEFECT SHAPE THIS RULES OUT is the one
    deposit_service.reconcile_shared_accounts() records from 2026-10-01: the crediting loop
    advances a swap out of the refreshed set, and the reconciler that runs afterwards sees
    `payout_pending` and calls the payment it has just credited stranded. A late-deposit
    pass in the same worker is the same hazard one table over -- and the row it would write
    says the desk holds unaccounted money that is in fact a customer's correctly credited
    deposit.

    SEEDED SO THE SWAP REALLY DOES MOVE DURING THE PASS: it starts in `awaiting_deposit`
    with no deposit_events row, the adapter answers with a deposit that is OUT of the
    tolerance band, so the real process_active_swaps() writes the deposit_events row and
    moves the swap to `under_review` -- a status the late pass DOES scan. The assertion is
    that the late table is still empty afterwards.

    MUTATION -- THE ACCOUNTED SET. Make accounted_keys() return frozenset() and this FAILS
    with the freshly credited deposit recorded as late. Confirmed failing 2026-10-04, then
    restored.

    THE ORDER OF THE TWO CALLS IS *NOT* WHAT THIS PINS, and that is said plainly because an
    earlier comment in reconcile_worker.run_cycle() claimed it was. Moving the late pass
    above process_active_swaps() leaves this test and all the others passing -- measured,
    not assumed -- because an active swap is excluded by the status filter in that order and
    by this accounted set in the other. reconcile_worker.run_cycle() now carries the
    correction.
    """
    db.execute(
        "INSERT INTO swaps (id, quote_id, from_asset, to_asset, deposit_address,"
        " payout_address, expected_input_amount, quoted_rate, fee_bps, network_fee_reserve,"
        " output_amount_estimate, status, min_confirmations, expires_at, created_at,"
        " updated_at) VALUES ('s_mid','q','BTC','GRC',?,?,0.0001,9868961.0,150,0.01,"
        "986.89613973,'awaiting_deposit',2,'2999-01-01T00:00:00+00:00',"
        "'2026-10-04T16:12:00+00:00',?)",
        (ADDRESS, GRC_PAYOUT, utc_now_iso()),
    )
    db.commit()
    # Out of the 1% tolerance band, so the swap halts into `under_review` -- which the late
    # pass scans -- rather than into payout_pending, which it also scans. Either works; this
    # one is the sharper case because it is the status a customer is most likely to resend
    # against.
    arriving = event("arriving_txid", 2, amount=AMOUNT * 3, confirmations=6)
    adapter = BtcAdapter([arriving])

    line = run_pass(db, adapter)

    assert db.execute("SELECT status FROM swaps WHERE id='s_mid'").fetchone()["status"] == (
        "under_review"), "the swap did not move during this cycle, so this proves nothing"
    assert db.execute(
        "SELECT COUNT(*) AS n FROM deposit_events WHERE txid='arriving_txid'"
    ).fetchone()["n"] == 1
    assert late_rows_in(db) == [], "a deposit credited in this very cycle was called late"
    assert "late_deposits=0" in line


# --- the cycle line (rule 14) -------------------------------------------------


def test_the_cycle_line_says_non_zero_then_zero(db):
    """The count an operator reads: WORKED with late_deposits=1, then IDLE with 0.

    THE LINE IS THE DELIVERABLE HERE and not a proxy for one, which is why this is the
    one test in the file that asserts on rendered text. A late deposit is money the desk
    holds and does not account for; a row nobody is told about is the same silence in a
    different place (rule 14: "make 'did nothing' look different from 'did work'").

    `late_unresolved` STAYS NON-ZERO AND MUST NOT CLAIM WORK, which is why it was added
    to workers/common.STANDING_COUNTS in the same commit as the field. It is the
    `failed_total` shape exactly: a cumulative figure that never returns to zero on its
    own, and the one that latched a WORKED marker on the operator's host for five days
    before anybody noticed.

    MUTATION -- THE MARKER. Remove `late_unresolved` from STANDING_COUNTS and the second
    assertion FAILS: cycle 2 prints WORKED for a pass that did nothing, with
    late_deposits=0 right there on the line. Confirmed failing, then restored.

    MUTATION -- THE NOTE. Make late_note() return the same string for zero and non-zero
    and the `(none)` assertion FAILS. Confirmed failing, then restored.
    """
    seed_finished_swap(db)
    adapter = BtcAdapter([CREDITED_EVENT, LATE_EVENT])

    first = run_pass(db, adapter, cycle=1)
    second = run_pass(db, adapter, cycle=2)

    assert "late_deposits=1" in first
    assert "WORKED" in first
    assert "ALREADY FINISHED" in first, "the line does not say what the count means"
    assert "late_deposits=0" in second
    assert "IDLE" in second, f"a pass that recorded nothing claimed work: {second}"
    # Rule 14: an empty result is a result, and it says so rather than leaving a gap.
    assert "(none)" in second
    # The standing figure is still reported, so "one arrived just now" and "one is still
    # sitting there" are different numbers on the same line.
    assert "late_unresolved=1" in first
    assert "late_unresolved=1" in second
    # And the window that decides the answer is echoed, in microfortnights with the
    # seconds in parentheses (rule 6).
    assert "µfn (86400.0s)" in second


def test_the_cycle_line_distinguishes_nothing_to_check_from_nothing_found(db):
    """late_deposits=0 with no finished swap says so, rather than reading as all-clear.

    A bare 0 is ambiguous between "nothing arrived late" and "nothing finished recently
    enough to be looked at", and only the second is compatible with a late deposit having
    arrived two days ago. The `targets` count is what separates them, which is why
    late_note() takes it.

    MUTATION. Drop the `if not targets` branch from late_note() and this FAILS: the line
    claims the addresses "were re-read" when none existed to read. Confirmed failing,
    then restored.
    """
    line = run_pass(db, BtcAdapter([]))

    assert "late_deposits=0" in line
    assert "no finished swap inside the" in line
    assert "(none) to check" in line
    assert "IDLE" in line


# =============================================================================
# THE ACCOUNTED SET MUST BE KEYED THE WAY deposit_events IS, OR IT SILENCES A REAL
# LATE DEPOSIT BELONGING TO A DIFFERENT SWAP
# =============================================================================
#
# accounted_keys() asked for (txid, vout) asset-wide and justified it in its own
# docstring: "deposit_events is UNIQUE(asset, txid, vout), so an output credited to a
# DIFFERENT swap cannot also be this swap's." That was true when it was written and
# stopped being true in 4e71b37 on 2026-10-10, when the constraint was widened to
# (asset, txid, vout, address) -- because on ICP the txid IS the ledger block index
# and every swap gets its own subaccount, so two swaps legitimately share a
# (txid, vout) and are told apart only by the address.
#
# The widening touched the two places that WRITE (deposit_events, late_deposits) and
# left this one, which READS deposit_events to decide. Rule 8's exact damage model:
# two copies of one rule, agreeing on the day they were written.
#
# THE ADDRESS CHECK ALREADY IN late_rows() DOES NOT COVER THIS, and that is the part
# worth being precise about. It compares the event against THIS swap's address; the
# accounted set is asset-wide and holds OTHER swaps' addresses. So an event that IS
# for this swap's address was being dropped because a different swap happened to have
# a row at the same block index.

#: A second subaccount on the same asset, which is the shape ICP produces and which
#: the Bitcoin-family fixtures above never generate -- a BTC txid is a hash, so two
#: swaps sharing one is not a case that arises.
OTHER_SWAP_ADDRESS = "subaccount_of_a_different_swap"


def test_a_late_payment_is_not_silenced_by_another_swaps_row_at_the_same_txid(db):
    """THE REGRESSION TEST. MUTATION: drop `address` from accounted_key()'s tuple.

    Seeds the operator's own ICP case in Bitcoin-family clothing, because the defect
    is in asset-independent code: an ALREADY-CREDITED row at (txid, vout) for swap A's
    address, and a real, confirmed payment at the SAME (txid, vout) into swap B's
    address. The second is a different payment -- the schema says so, since 4e71b37 --
    and it must be recorded.

    THE MEASUREMENT THIS REPRODUCES used 2.44081155 ICP at ledger block index 2, which
    is the operator's own figure from 2026-10-10: an old settled row from the ledger a
    `docker compose down` destroyed made ('2', 0) accounted for the whole asset, and a
    real payment at block index 2 of the REBUILT ledger into a different subaccount was
    filtered out with a bare `continue` -- no late_deposits row, nothing in the pass's
    count, nothing in show_late_deposits.py. This pass is, by its own header, the only
    thing that records money arriving for a swap that has already finished.
    """
    seed_finished_swap(db)
    db.execute(
        "INSERT INTO swaps (id, quote_id, from_asset, to_asset, deposit_address,"
        " payout_address, expected_input_amount, quoted_rate, fee_bps, network_fee_reserve,"
        " output_amount_estimate, status, min_confirmations, expires_at, created_at,"
        " updated_at) VALUES ('s_other','q','BTC','GRC',?,?,?,9868961.0,150,0.01,"
        "986.89613973,'completed',2,'2999-01-01T00:00:00+00:00',"
        "'2026-10-04T16:12:00+00:00',?)",
        (OTHER_SWAP_ADDRESS, GRC_PAYOUT, AMOUNT, utc_now_iso()),
    )
    db.commit()

    # The SAME txid and vout the seeded swap's credited row carries, paid into the
    # OTHER swap's address. Before the fix, accounted_keys() held ('<txid>', <vout>)
    # and this was dropped.
    collision = event(CREDITED_TXID, CREDITED_VOUT, confirmations=20, address=OTHER_SWAP_ADDRESS)
    other = dict(db.execute("SELECT * FROM swaps WHERE id = 's_other'").fetchone())

    recorded = late_deposit_service.late_rows(
        [collision], other, late_deposit_service.accounted_keys(db, "BTC")
    )

    assert recorded, (
        "the live payment into a DIFFERENT swap's address was dropped because another "
        "swap had a deposit_events row at the same (txid, vout)"
    )
    assert [(row.txid, row.vout, row.address) for row in recorded] == [
        (CREDITED_TXID, CREDITED_VOUT, OTHER_SWAP_ADDRESS)
    ]


def test_the_swaps_own_credited_payment_is_still_suppressed(db):
    """The idempotency half, which the widening must not break.

    Widening a key loosens it, so the risk runs the other way too: a payment ALREADY
    in deposit_events for THIS address must still not be recorded as late. Without
    this, the fix above would trade a dropped deposit for a double-counted one -- the
    opposite failure and equally quiet, since nothing downstream distinguishes a late
    row from a second late row.
    """
    seed_finished_swap(db)
    swap = dict(db.execute("SELECT * FROM swaps WHERE id = ?", (SWAP_ID,)).fetchone())

    recorded = late_deposit_service.late_rows(
        [CREDITED_EVENT], swap, late_deposit_service.accounted_keys(db, "BTC")
    )
    assert recorded == [], "the swap's own credited deposit was recorded as a late one"


def test_the_accounted_key_is_derived_from_the_schemas_own_unique_key(db):
    """MUTATION: change db.PAYMENT_UNIQUE_KEY and this test, not production, is what breaks.

    The defect was not that the key was wrong. It was that the key was SPELLED TWICE --
    once in db.py's constraint and once in a SELECT here -- so widening one left the
    other behind for a commit. accounted_keys() now derives its columns from
    db.PAYMENT_UNIQUE_KEY, which is why there is nothing left to forget.

    ASSERTED ON THE TUPLE'S SHAPE, not on the SQL text, so the test does not pin the
    implementation it is protecting.
    """
    seed_finished_swap(db)
    keys = late_deposit_service.accounted_keys(db, "BTC")

    assert len(keys) == 1
    key = next(iter(keys))
    # asset is the WHERE clause rather than part of the tuple, so the arity is the
    # key's length minus one.
    assert len(key) == len(PAYMENT_UNIQUE_KEY) - 1, (
        f"the accounted key has {len(key)} elements and db.PAYMENT_UNIQUE_KEY has "
        f"{len(PAYMENT_UNIQUE_KEY)}; one of them was widened without the other"
    )
    assert key == (CREDITED_TXID, CREDITED_VOUT, ADDRESS)


def test_both_sides_of_the_membership_test_are_built_by_one_function(db):
    """A string vout from a chain scan must match an integer vout from the database.

    THE OTHER WAY THIS KEY FAILS, and it is quieter than the drop: a tuple that
    differs only by type is a tuple that is never found, so the payment is recorded
    AGAIN rather than suppressed. accounted_key() normalizes both sides with the same
    code, which is the only reason that cannot happen.
    """
    seed_finished_swap(db)
    accounted = late_deposit_service.accounted_keys(db, "BTC")
    swap = dict(db.execute("SELECT * FROM swaps WHERE id = ?", (SWAP_ID,)).fetchone())

    # vout as a STRING, address with whitespace -- what a loosely-typed RPC response
    # or a hand-built dict supplies.
    sloppy = {"txid": CREDITED_TXID, "vout": str(CREDITED_VOUT),
              "address": f"  {ADDRESS}  ", "amount": AMOUNT, "confirmations": 20}
    # late_rows() strips the address before comparing it to the swap's, so the
    # whitespace case is exercised through accounted_key() directly as well.
    assert late_deposit_service.accounted_key(sloppy) in accounted, (
        "a string vout or a padded address produced a key the database's own row did not match"
    )
    assert late_deposit_service.late_rows([sloppy], swap, accounted) == []
