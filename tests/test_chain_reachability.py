"""A chain that did not answer is not offered, and the fact comes from a row nobody paid for.

Role: test
Reads: services/chain_reachability.py, services/payout_service.py,
       services/pair_view.py, and the real app for the customer page
Writes: a temporary SQLite database under pytest's tmp_path
Can move funds: no
Mainnet-safe: yes -- NO CHAIN AND NO NETWORK. Every adapter here is defined in
       this file and either returns a number or raises.

=============================================================================
WHAT WENT WRONG. Operator, 2026-10-11:
=============================================================================

    "how bout ya stop guessing at fucking timeouts and assuming shit and have a
     robust and graceful way or verifying each step of the process was completed
     so nothing chokes"

From their payout_worker.log, cycles 69 to 80 -- twelve consecutive cycles:

    wallet inventory for LTC NOT refreshed ... ConnectTimeoutError(... port=19332
      ... 'Connection to host.docker.internal timed out. (connect timeout=30.0)')
    wallet inventory for GRC NOT refreshed ... port=25715 ... the same
    payout_worker cycle=71 IDLE ... in 50.1microfortnights (60.6s)

THE TERMINAL KNEW, ONCE A MINUTE, THAT BOTH CHAINS WERE DOWN. It wrote that to a
log file and dropped it -- and then offered GRC -> LTC on the customer page and
spent four 30s connect timeouts failing to create the swap, past gunicorn's 60s
worker timeout, so the customer got a 500 with no reason and no deposit address.

Rule 7: "A measurement that only exists in a log is not learning. If it decides
something, it belongs in SQL where the next run can read it."

=============================================================================
THE FOUR CLAIMS, AND WHY EACH NEEDS ITS OWN TEST
=============================================================================

  THE POLL RECORDS        refresh_wallet_inventory() already made the call. The row
                          must be written for BOTH outcomes -- a failure-only writer
                          would leave a recovered chain marked down until the row
                          went stale, which is a gate outliving its cause (rule 19).
  STALENESS STOPS IT      a row older than the window means THE WORKERS ARE NOT
                          RUNNING, not that the chain is down. Refusing on it would
                          send somebody to restart a healthy daemon.
  THE PAIR REFUSES        and it is the first reason reported, because
                          pair_serviceability()'s own rule is "the first condition
                          that refuses is the one reported".
  NOTHING NEW IS PAID     the verdict costs one SELECT over at most six rows. If a
                          socket is ever opened on this path, the page is back to
                          costing the sum of six RPC timeouts, which is what
                          admin_view.probe_chain() is kept off a page to avoid.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "swap_terminal"))

from db import SCHEMA, connect_db  # noqa: E402
from services import pair_view, payout_service  # noqa: E402
from services.chain_reachability import (  # noqa: E402
    STALE_AFTER_SECONDS,
    reachability_refusal,
    reachability_rows,
    record_reachability,
    row_age_seconds,
)
from services.helpers import utc_now_iso  # noqa: E402
from services.pair_view import pair_serviceability  # noqa: E402

TIMED_OUT = "Connection to host.docker.internal timed out. (connect timeout=30.0)"


class Answers:
    """An adapter whose get_balance returns. can_spend so it may be a destination."""

    can_spend = True

    def __init__(self, asset, balance=100.0):
        self.asset = asset
        self._balance = balance

    def get_balance(self):
        return self._balance


class DoesNotAnswer:
    """An adapter whose get_balance raises what the operator's LTC daemon raised."""

    can_spend = True

    def __init__(self, asset):
        self.asset = asset

    def get_balance(self):
        raise ConnectionError(TIMED_OUT)


@pytest.fixture
def db(tmp_path):
    conn = connect_db(str(tmp_path / "reach.db"), create=True)
    conn.executescript(SCHEMA)
    return conn


# --- the poll records, both ways --------------------------------------------------


def test_a_failing_poll_writes_the_row(db):
    """Through the REAL refresh_wallet_inventory(), not a paraphrase of it."""
    payout_service._REPORTED_INVENTORY_FAILURES.clear()
    payout_service.refresh_wallet_inventory(db, {"LTC": DoesNotAnswer("LTC")})
    rows = reachability_rows(db)
    assert rows["LTC"]["reachable"] is False
    assert rows["LTC"]["asked"] == "get_balance", (
        "the METHOD is recorded because 'LTC is unreachable' is a claim about a method on "
        "an endpoint -- SOL's get_balance refuses by design and is not an outage"
    )
    assert TIMED_OUT in rows["LTC"]["detail"]


def test_a_succeeding_poll_writes_the_row_too(db):
    payout_service.refresh_wallet_inventory(db, {"GRC": Answers("GRC")})
    rows = reachability_rows(db)
    assert rows["GRC"]["reachable"] is True
    assert rows["GRC"]["detail"] == ""


def test_a_recovered_CHAIN_FLIPS_the_row_rather_than_lingering(db):
    """A gate that outlives its cause is rule 19's patch. The row must follow the chain.

    Without the success-side write, a daemon that came back would stay marked down
    until the row went stale -- up to STALE_AFTER_SECONDS of refusing a pair that works.
    """
    payout_service._REPORTED_INVENTORY_FAILURES.clear()
    payout_service.refresh_wallet_inventory(db, {"LTC": DoesNotAnswer("LTC")})
    assert reachability_rows(db)["LTC"]["reachable"] is False
    payout_service.refresh_wallet_inventory(db, {"LTC": Answers("LTC")})
    row = reachability_rows(db)["LTC"]
    assert row["reachable"] is True
    assert row["detail"] == "", "the stale failure reason survived a successful poll"


def test_the_row_is_upserted_and_never_accumulates(db):
    """One row per asset. Ten cycles must not be ten rows.

    This is a table the workers write every ~60s forever. A per-cycle INSERT would grow
    without bound on the one database the whole terminal shares a writer lock on.
    """
    for _ in range(10):
        payout_service.refresh_wallet_inventory(db, {"GRC": Answers("GRC")})
    count = db.execute("SELECT COUNT(*) AS n FROM chain_reachability").fetchone()["n"]
    assert count == 1, f"{count} rows after ten cycles of one chain"


def test_recording_does_not_commit_on_its_own(db, tmp_path):
    """The caller commits once per cycle. Three workers share one writer lock (rule 15).

    Asserted by reading the row back on a SEPARATE connection before any commit: if
    record_reachability() committed, the other connection would see it.
    """
    record_reachability(db, "BTC", "get_balance", reachable=False, detail="x")
    other = sqlite3.connect(str(tmp_path / "reach.db"))
    other.row_factory = sqlite3.Row
    visible = other.execute("SELECT COUNT(*) FROM chain_reachability").fetchone()[0]
    other.close()
    assert visible == 0, (
        "record_reachability() committed. It is called once per chain inside a loop that "
        "commits at the end, so committing here is one transaction per chain against the "
        "single writer lock instead of one per cycle."
    )


# --- the refusal decision ---------------------------------------------------------


def seeded(reachable, checked_at=None, asset="LTC"):
    """One stored reading, FRESH BY DEFAULT.

    checked_at=None MEANS NOW, and the first draft used a fixed string --
    "2026-10-11T21:20:00+00:00" -- which was fresh for about ten minutes and then
    silently stopped refusing anything, because reachability_refusal() computes the
    real age when none is seeded and a row past STALE_AFTER_SECONDS does not gate.
    Two pair tests failed for that reason and the staleness logic was right: a fixture
    that decays into a pass is worse than one that fails, because it fails LATER and
    somewhere else.

    The tests that are ABOUT parsing pass an explicit string; everything else wants a
    reading that is current, which is also what a running deployment always has.
    """
    return {asset: {"reachable": reachable, "asked": "get_balance",
                    "detail": TIMED_OUT,
                    "checked_at": utc_now_iso() if checked_at is None else checked_at}}


def test_a_fresh_down_row_refuses_and_names_the_age():
    """"GRC is not answering" and "GRC was not answering nine minutes ago" differ."""
    refusal = reachability_refusal(seeded(False), "LTC", age_seconds=42.0)
    assert "LTC" in refusal
    assert "did not answer" in refusal
    assert "get_balance" in refusal
    assert "ago" in refusal, "the age must be in the sentence; only the dated claim is true"
    assert "µfn" in refusal, "rule 6: a reported duration is in microfortnights"


def test_a_fresh_up_row_is_silent():
    assert reachability_refusal(seeded(True), "LTC", age_seconds=42.0) == ""


def test_a_STALE_down_row_does_NOT_refuse():
    """A row this old means the WORKERS are not running, not that the chain is down.

    Refusing on it would report a worker outage as a chain outage and send somebody to
    restart a daemon that is fine. Refusing to refuse is also the safe direction: the
    attempt proceeds and fails at the step that touches the chain, with its own reason.
    """
    assert reachability_refusal(seeded(False), "LTC", age_seconds=STALE_AFTER_SECONDS + 1) == ""


def test_a_row_exactly_at_the_limit_still_refuses():
    """The boundary is pinned, because `>` and `>=` here are a silent one-cycle difference."""
    assert reachability_refusal(seeded(False), "LTC", age_seconds=STALE_AFTER_SECONDS) != ""


def test_a_MISSING_row_does_not_refuse():
    """A fresh database has none. Refusing would empty the customer page on a cold start."""
    assert reachability_refusal({}, "LTC", age_seconds=1.0) == ""


def test_an_unreadable_timestamp_reads_as_unknown_age_and_does_not_refuse():
    """None rather than 0.0, because 0.0 would make a corrupt row the freshest possible."""
    assert row_age_seconds("not-a-timestamp") is None
    assert reachability_refusal(seeded(False, checked_at="not-a-timestamp"), "LTC") == ""


def test_a_NAIVE_timestamp_reads_as_unknown_rather_than_being_guessed():
    """Guessing a zone is how an age comes out hours wrong, in either direction."""
    assert row_age_seconds("2026-10-11T21:20:00") is None


def test_the_stale_window_is_larger_than_a_worker_cycle():
    """DERIVED, not chosen: the window must hold several of the cycles that write the row.

    A cycle measured 60.5s on the operator's host with two chains dropping. If the
    window were ever set below that, every row would be stale on arrival and the gate
    would never fire -- green, and inert, which is the shape of the 15s-grace defect.
    """
    measured_worst_cycle_seconds = 60.8
    assert measured_worst_cycle_seconds * 2 < STALE_AFTER_SECONDS, (
        f"STALE_AFTER_SECONDS is {STALE_AFTER_SECONDS} and a measured worker cycle is "
        f"{measured_worst_cycle_seconds}s. A window that does not hold at least two cycles "
        "makes every row stale on arrival and the gate silently inert."
    )


# --- the sixth condition ----------------------------------------------------------


def servicing_config():
    return {"ALLOWED_PAIRS": [("GRC", "LTC"), ("LTC", "GRC")], "RPC": {}}


def both_adapters():
    return {"GRC": Answers("GRC"), "LTC": Answers("LTC")}


def test_an_unreachable_DESTINATION_makes_the_pair_unserviceable():
    verdict = pair_serviceability(
        servicing_config(), both_adapters(), "GRC", "LTC", seeded(False, asset="LTC")
    )
    assert verdict["serviceable"] is False
    assert verdict["unreachable"]
    assert verdict["reason"] == verdict["unreachable"], (
        "pair_serviceability()'s own rule is that the first condition to refuse is the one "
        "reported; two reasons for one pair leaves the operator to work out which to act on"
    )


def test_an_unreachable_SOURCE_makes_the_pair_unserviceable():
    """The source is asked FIRST, because it is the leg that mints the deposit address.

    That is the step the operator's attempt never reached, and the one whose absence they
    actually saw: "it never told me where to send 500grc".
    """
    verdict = pair_serviceability(
        servicing_config(), both_adapters(), "LTC", "GRC", seeded(False, asset="LTC")
    )
    assert verdict["serviceable"] is False
    assert "LTC" in verdict["unreachable"]


def test_reachability_None_changes_NOTHING(monkeypatch):
    """Every caller that predates this keeps its exact behavior.

    None means DO NOT CONSIDER IT -- not "unreachable". A caller with no database, a
    test, or a surface running before the workers have written anything must not be
    made to refuse every pair.
    """
    with_none = pair_serviceability(servicing_config(), both_adapters(), "GRC", "LTC")
    assert with_none["unreachable"] == ""
    with_empty = pair_serviceability(servicing_config(), both_adapters(), "GRC", "LTC", {})
    assert with_empty["unreachable"] == ""
    assert with_none["serviceable"] == with_empty["serviceable"]


def test_an_unreachable_chain_SHORT_CIRCUITS_the_one_network_read():
    """cannot_establish is the only condition here that touches a socket. It must be skipped.

    A chain already known not to answer must not be asked again by the page that is
    refusing it -- otherwise the gate costs the very RPC timeout it exists to avoid.
    """
    asked = []

    class Floor:
        can_spend = True
        asset = "LTC"

        def get_balance(self):
            return 1.0

    def recording_floor(adapters, to_asset):
        asked.append(to_asset)
        return ""

    original = pair_view.why_cannot_establish_payout_floor
    pair_view.why_cannot_establish_payout_floor = recording_floor
    try:
        pair_serviceability(
            servicing_config(), {"GRC": Answers("GRC"), "LTC": Floor()},
            "GRC", "LTC", seeded(False, asset="LTC"),
        )
    finally:
        pair_view.why_cannot_establish_payout_floor = original
    assert asked == [], (
        "why_cannot_establish_payout_floor() was called for a chain already known to be "
        "unreachable. That is the one socket on this path, and paying it defeats the gate."
    )


def test_the_verdict_costs_exactly_one_query(db):
    """One SELECT over at most six rows, not one per asset.

    The pair list asks about both legs of up to 30 pairs. Sixty single-row queries
    against the one writer lock to render one page is the shape rule 15 warns about.
    """
    payout_service.refresh_wallet_inventory(db, {"GRC": Answers("GRC"), "LTC": Answers("LTC")})
    # set_trace_callback() AND NOT A MONKEYPATCH on Connection.execute, which is
    # read-only on sqlite3.Connection. This is SQLite's own hook for exactly this
    # question, so it counts what the DRIVER ran rather than what the code called --
    # a stronger measurement, since a helper issuing a second statement internally
    # would be invisible to a patched execute.
    queries = []
    db.set_trace_callback(queries.append)
    try:
        rows = reachability_rows(db)
    finally:
        db.set_trace_callback(None)
    assert len(queries) == 1, f"reachability_rows() issued {len(queries)} statements: {queries}"
    assert set(rows) == {"GRC", "LTC"}
