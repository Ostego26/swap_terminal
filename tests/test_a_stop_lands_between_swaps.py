"""A stop must stop STARTING payouts, never interrupt one that is already sending.

Role: submodule (tests)
Reads: the real schema, the real services/payout_service.process_pending_payouts(),
       swap_terminal/stop_budget.py and Config.RPC's timeouts
Writes: a seeded SQLite database under pytest's tmp_path. Nothing else.
Can move funds: no. The adapter is a stub that records calls and returns a fake txid;
       no chain, no socket, no credential
Mainnet-safe: yes, and network-independent

WHY IT EXISTS, measured on the operator's host 2026-10-11 and not inferred. A
`docker compose stop web` took 15.5s, and `docker compose logs --timestamps` showed:

    15:25:29.950  signal 15 received. Stopping WORKERS FIRST, then gunicorn.
    15:25:30.053  [10] [INFO] Shutting down: Master          gunicorn GONE at +0.1s
    15:25:45.004  stop deposit_watcher    stopped            the workers, +15.05s

15.05s is EXACTLY the worker grace, which means the workers did not exit -- they were
SIGKILLed. The cause is a relationship nothing enforced: the workers honor SIGTERM by
finishing the current CYCLE (workers/common.install_stop_handler), a cycle is bounded by
the chain RPC timeouts rather than by the poll interval, and every *_RPC_TIMEOUT
defaults to 30 against a grace of 15. A worker waiting on a chain could not honor
SIGTERM in time, EVER.

WHY THAT MATTERS ON THE PAYOUT WORKER SPECIFICALLY. Being SIGKILLed between
`sendtoaddress` returning a txid and _record_broadcast() committing it is the one
interruption the payout sequence is built to avoid. It is not silent loss -- the payouts
row is committed BEFORE the send, deliberately -- but rescue_payout.py "refuses every
swap it cannot prove was never broadcast", which is exactly this case. So the swap is
not auto-recoverable: it sits in `paying` and needs an operator reading the chain.

TWO CHANGES, AND NEITHER ALONE WOULD HAVE DONE IT. The loops now take a should_stop and
check it BETWEEN units of work, so a stop never begins another swap; and the grace is
now LONGEST_RPC_TIMEOUT_SECONDS + RPC_OVERRUN_MARGIN_SECONDS, so the unit already in
flight can finish. Without the first a longer grace only waits longer before killing;
without the second a swap already sending still dies mid-sequence. This file holds both.

WHAT IS STILL NOT COVERED, asserted nowhere because it is not fixed: a payout already
mid-sequence against a daemon hung on EVERY call -- unlock, send and relock can each
burn 30s. Covering that needs a two-minute grace and a `docker compose down` that waits
it out. Named in stop_budget.py rather than claimed here.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from db import SCHEMA, apply_migrations, connect_db
from services.payout_service import process_pending_payouts
from valid_addresses import GRC_PAYOUT, LTC_PARTICIPANT

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "swap_terminal"))

import stop_budget  # noqa: E402

CONFIG = {
    "BTC_MIN_CONFIRMATIONS": 2,
    "LTC_MIN_CONFIRMATIONS": 2,
    "GRC_MIN_CONFIRMATIONS": 6,
    "AMOUNT_TOLERANCE_PCT": 0.01,
}

NOW = "2026-10-11T00:00:00+00:00"


class RecordingLTC:
    """An LTC adapter that records every send and claims each one succeeded.

    asset IS SET because services/payout_service.broadcast_payout() derives the chain
    from the adapter rather than taking it as an argument, and an adapter that cannot
    name itself is refused before any send.
    """

    asset = "LTC"
    can_spend = True
    payout_refusal = ""

    def __init__(self):
        self.sent: list[tuple] = []

    def send_to_address(self, address, amount, *args, **kwargs):
        self.sent.append((address, amount))
        return f"ltc_txid_{len(self.sent)}"

    def get_balance(self) -> float:
        return 1000.0

    def validate_address(self, _address):
        return None


def seed_two_pending_payouts(path: str) -> None:
    """TWO swaps in `payout_pending`, which is the minimum that can show the behavior.

    tests/test_payout_concurrency._seed_one_pending_swap() is deliberately singular --
    "exactly one swap into payout_pending" -- and shares a fixed quote id, so it cannot
    be called twice. This seeds its own pair rather than loosening that helper, because
    one swap cannot distinguish "stopped between swaps" from "stopped before any".

    ORDER MATTERS AND IS SET BY credited_at. process_pending_payouts() reads
    `ORDER BY credited_at ASC`, so s_first is the one a single-swap cycle pays and
    s_second is the one a stop must leave alone.
    """
    conn = connect_db(path, create=True)
    conn.executescript(SCHEMA)
    conn.execute(
        """
        INSERT INTO quotes (id, from_asset, to_asset, input_amount, quoted_rate, fee_bps,
                            network_fee_reserve, output_amount_estimate, expires_at, created_at)
        VALUES ('q_pair', 'GRC', 'LTC', 100.0, 0.001, 150, 0.001, 0.0975, ?, ?)
        """,
        (NOW, NOW),
    )
    for swap_id, credited in (("s_first", "2026-10-11T00:00:01+00:00"),
                              ("s_second", "2026-10-11T00:00:02+00:00")):
        conn.execute(
            """
            INSERT INTO swaps (
                id, quote_id, from_asset, to_asset, deposit_address, payout_address,
                expected_input_amount, actual_input_amount, quoted_rate, fee_bps,
                network_fee_reserve, output_amount_estimate, status, min_confirmations,
                deposit_txid, payout_txid, created_at, updated_at, credited_at,
                completed_at, expires_at, failed_reason
            ) VALUES (?, 'q_pair', 'GRC', 'LTC', ?, ?,
                      100.0, 100.0, 0.001, 150, 0.001, 0.0975, 'payout_pending', 6,
                      'grc_txid', NULL, ?, ?, ?, NULL, ?, NULL)
            """,
            (swap_id, GRC_PAYOUT, LTC_PARTICIPANT, NOW, NOW, credited, NOW),
        )
    conn.execute(
        "INSERT INTO wallet_inventory (asset, hot_confirmed, hot_reserved, hot_available, updated_at) "
        "VALUES ('LTC', 100.0, 0.0, 100.0, ?)",
        (NOW,),
    )
    conn.commit()
    apply_migrations(conn)
    conn.close()


@pytest.fixture
def db_path(tmp_path):
    path = str(tmp_path / "between_swaps.db")
    seed_two_pending_payouts(path)
    return path


def test_a_stop_after_the_first_swap_leaves_the_second_untouched(db_path):
    """ONE send, ONE completed swap, and the second still exactly where it was.

    THIS IS THE ASSERTION THE WHOLE CHANGE IS FOR. The second swap must be `payout_pending`
    with no payout row and no txid -- not `paying`, not `failed`, not half-claimed. A
    stop that left it claimed-but-unpaid would be strictly worse than the SIGKILL it
    replaced, because `paying` is the status rescue_payout.py cannot resolve.

    MUTATION: delete the `if should_stop ... break` from process_pending_payouts() and
    this fails with two sends.
    """
    calls = {"n": 0}

    def should_stop() -> bool:
        # True on the SECOND consultation: the loop checks before each swap, so the
        # first check must pass to let one swap through and the second must stop.
        calls["n"] += 1
        return calls["n"] > 1

    adapter = RecordingLTC()
    conn = connect_db(db_path, create=False)
    try:
        completed = process_pending_payouts(conn, CONFIG, {"LTC": adapter}, should_stop)
        rows = {
            row["id"]: row
            for row in conn.execute("SELECT * FROM swaps ORDER BY credited_at").fetchall()
        }
        payouts = conn.execute("SELECT swap_id, status, txid FROM payouts").fetchall()
    finally:
        conn.close()

    assert len(adapter.sent) == 1, f"expected exactly one send, got {adapter.sent!r}"
    assert len(completed) == 1, f"expected one completed swap, got {completed!r}"

    assert rows["s_first"]["status"] == "completed", (
        f"the swap that started did not finish: {rows['s_first']['status']!r} "
        f"{rows['s_first']['failed_reason']!r}"
    )
    assert rows["s_first"]["payout_txid"] == "ltc_txid_1"

    assert rows["s_second"]["status"] == "payout_pending", (
        f"the second swap is {rows['s_second']['status']!r}. A stop must leave it exactly "
        f"where it was -- `paying` would be the status rescue_payout.py cannot resolve, "
        f"which is worse than the SIGKILL this replaced"
    )
    assert rows["s_second"]["payout_txid"] is None
    assert rows["s_second"]["failed_reason"] is None, (
        "the second swap was marked failed; a stop is not a failure and must not be "
        "recorded as one"
    )
    assert [row["swap_id"] for row in payouts] == ["s_first"], (
        f"a payouts row exists for a swap that was never started: {payouts!r}"
    )


def test_a_stop_before_any_swap_pays_nothing_and_fails_nothing(db_path):
    """should_stop True from the start: no send, no row, both swaps untouched.

    The boundary the test above cannot reach. A worker signaled while idle takes this
    path, which is the common case -- and "did nothing" must look like nothing rather
    than like a cycle that failed (rule 14).
    """
    adapter = RecordingLTC()
    conn = connect_db(db_path, create=False)
    try:
        completed = process_pending_payouts(conn, CONFIG, {"LTC": adapter}, lambda: True)
        statuses = [row["status"] for row in conn.execute(
            "SELECT status FROM swaps ORDER BY credited_at").fetchall()]
        payouts = conn.execute("SELECT COUNT(*) AS n FROM payouts").fetchone()["n"]
    finally:
        conn.close()

    assert adapter.sent == [], f"something was sent after an immediate stop: {adapter.sent!r}"
    assert completed == []
    assert statuses == ["payout_pending", "payout_pending"], statuses
    assert payouts == 0, "a payouts row was written for a cycle that sent nothing"


def test_no_should_stop_still_pays_every_pending_swap(db_path):
    """The default must be "never stop", so no existing caller changed behavior.

    should_stop defaults to None and most callers -- the tests, the rescue paths,
    repair_inventory_reservations.py -- pass nothing. A default of "stop" or a required
    parameter would have changed what they do, silently in the first case.
    """
    adapter = RecordingLTC()
    conn = connect_db(db_path, create=False)
    try:
        completed = process_pending_payouts(conn, CONFIG, {"LTC": adapter})
        statuses = sorted(row["status"] for row in conn.execute(
            "SELECT status FROM swaps").fetchall())
    finally:
        conn.close()

    assert len(adapter.sent) == 2, f"both swaps should have been paid: {adapter.sent!r}"
    assert len(completed) == 2
    assert statuses == ["completed", "completed"], statuses


def test_the_worker_grace_exceeds_the_longest_chain_rpc_timeout():
    """The grace must be able to outlast one hung RPC call, derived from Config itself.

    THE DEFECT THIS PINS, and it is a relationship nothing enforced before: the grace
    was a flat 15 while every chain's timeout defaults to 30, so a worker waiting on a
    chain could not honor SIGTERM inside the grace under any circumstances. The two
    numbers lived in different files with nothing comparing them -- the same shape as
    `graceful_timeout = 30` against a 20s wait, one layer out.

    READ FROM Config.RPC, NOT FROM stop_budget's own constant, which is the point.
    stop_budget.LONGEST_RPC_TIMEOUT_SECONDS is a declaration; this asserts it against
    the timeouts the adapters are actually constructed with, so raising any chain's
    timeout fails here rather than silently making the grace too short again.

    MUTATION: set LONGEST_RPC_TIMEOUT_SECONDS to 10 and this fails naming the chains.
    """
    from config import Config  # noqa: PLC0415 -- after conftest's path shim

    timeouts = {
        asset: settings["timeout"]
        for asset, settings in Config.RPC.items()
        if isinstance(settings, dict) and "timeout" in settings
    }
    assert timeouts, (
        "no chain in Config.RPC declares a timeout, so this test is not measuring "
        "anything -- the key moved or the shape changed"
    )
    longest = max(timeouts.values())
    assert longest <= stop_budget.LONGEST_RPC_TIMEOUT_SECONDS, (
        f"stop_budget declares the longest RPC timeout as "
        f"{stop_budget.LONGEST_RPC_TIMEOUT_SECONDS} and Config.RPC has {longest} "
        f"({timeouts}). The worker grace is derived from that declaration, so it is now "
        f"shorter than a single chain call and a worker waiting on one cannot honor "
        f"SIGTERM before the supervisor SIGKILLs it."
    )
    assert longest < stop_budget.WORKER_GRACE_SECONDS, (
        f"the worker grace is {stop_budget.WORKER_GRACE_SECONDS} and one chain call can "
        f"block for {longest}. A worker finishing its current unit of work cannot fit "
        f"inside the grace, which is what made every stop a SIGKILL."
    )
