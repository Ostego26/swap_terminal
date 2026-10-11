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
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import requests

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "swap_terminal"))

from chains.icp import ICPCallFailed  # noqa: E402
from chains.solana import SolanaRPCError  # noqa: E402
from chains.xrp import XRPRPCError  # noqa: E402
from db import SCHEMA, connect_db  # noqa: E402
from prose import code_without_prose  # noqa: E402
from services import pair_view, payout_service  # noqa: E402
from services.chain_reachability import (  # noqa: E402
    STALE_AFTER_SECONDS,
    is_transport_failure,
    reachability_refusal,
    reachability_rows,
    record_reachability,
    row_age_seconds,
)
from services.helpers import utc_now_iso  # noqa: E402
from services.pair_view import pair_serviceability  # noqa: E402
from services.swap_service import create_swap  # noqa: E402
from valid_addresses import LTC_PARTICIPANT  # noqa: E402

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


# --- the gate next to the write --------------------------------------------------
#
# THE PAGE NOT OFFERING A PAIR IS NOT THE SAME AS THE WRITE REFUSING IT, and the gap
# between those two is where the operator's swap died. A POST to /api/swaps does not
# come from the page, and a customer who loaded the page before a daemon died is
# holding a quote for a pair that was serviceable when it priced.


class RefusesToBeAsked:
    """An adapter that FAILS THE TEST if any RPC is spent on it.

    This is how "the gate fires before any RPC" is measured rather than asserted: the
    thing that must not happen raises. A stub that returned a plausible answer instead
    would let the gate be removed with every test still passing, because create_swap
    would simply do the work and succeed.
    """

    can_spend = True

    def __init__(self, asset):
        self.asset = asset

    def get_balance(self):
        raise ConnectionError(TIMED_OUT)

    def address_report(self, address, want="both"):
        raise AssertionError(
            "address_report() was called on a chain already known not to answer. On the "
            "operator's host that was two 30s connect timeouts; the gate exists to not "
            "spend them."
        )

    def validate_address(self, address):
        raise AssertionError("validate_address() was called on a chain known not to answer")

    def deposit_address(self, index):
        raise AssertionError("a deposit address was derived for a chain known not to answer")


def seed_quote(db, from_asset="GRC", to_asset="LTC"):
    """A real `quotes` row, because create_swap() reads the pair off the quote.

    The first version of this proof passed a made-up quote id and got "Quote not
    found" -- a refusal, in 0.0001s, that proved nothing about the gate. The quote
    lookup is correctly FIRST, since the assets come from it.
    """
    now = datetime.now(UTC)
    db.execute(
        "INSERT INTO quotes (id, from_asset, to_asset, input_amount, quoted_rate, fee_bps,"
        " network_fee_reserve, output_amount_estimate, expires_at, created_at)"
        " VALUES ('q_1', ?, ?, 500.0, 0.0001, 50, 0.0001, 0.0495, ?, ?)",
        (from_asset, to_asset, (now + timedelta(minutes=10)).isoformat(), now.isoformat()),
    )
    db.commit()
    return "q_1"


#: A REAL LTC TESTNET ADDRESS, derived rather than typed. The first draft of these
#: tests used a hand-made string, and modules/address_authority refused it correctly --
#: "decodes as neither bech32 nor base58check under Bitcoin's alphabet" -- which made
#: four tests fail for a reason that had nothing to do with reachability. The local
#: decode working is what made the fixture wrong.
#:
#: tests/valid_addresses.py exists so no test invents an address, and
#: tests/test_address_literals_are_valid.py counts every address-shaped literal in the
#: tree for exactly this reason.
LTC_PAYOUT = LTC_PARTICIPANT


def swap_config():
    return {"ALLOWED_PAIRS": [("GRC", "LTC")], "RPC": {}, "MIN_CONFIRMATIONS": {}}


def test_create_swap_refuses_an_unreachable_destination_BEFORE_any_rpc(db):
    """The whole point, through the real create_swap(). 0.0004s where it was 120s.

    RefusesToBeAsked raises on every RPC, so this test cannot pass unless the gate
    fires first.
    """
    quote = seed_quote(db)
    adapters = {"GRC": Answers("GRC"), "LTC": RefusesToBeAsked("LTC")}
    payout_service._REPORTED_INVENTORY_FAILURES.clear()
    payout_service.refresh_wallet_inventory(db, adapters)

    with pytest.raises(ValueError) as refused:
        create_swap(db, swap_config(), adapters, quote, LTC_PAYOUT)
    message = str(refused.value)
    assert "LTC" in message
    assert "did not answer" in message
    assert "Nothing was written" in message
    assert db.execute("SELECT COUNT(*) AS n FROM swaps").fetchone()["n"] == 0


def test_the_refusal_does_not_claim_the_chains_are_reachable(db):
    """Two messages in create_swap() SAID "both chains are reachable" and it never asked.

    Rule 16, in text a customer reads. The gate makes the weaker claim true -- "both
    chains answered when they were last asked" -- and the strong one is gone.
    """
    # code_without_prose() AND NOT read_text(), because the raw scan failed on a
    # DOCSTRING describing another module's pair states -- the ninth occurrence of that
    # defect class in this tree, and the reason tests/prose.py exists. The refusal
    # messages are f-strings in `raise` statements, so they survive the strip; only the
    # explanatory text goes.
    source = code_without_prose(REPO_ROOT / "swap_terminal" / "services" / "swap_service.py")
    assert "both chains are reachable" not in source, (
        "a refusal still claims the chains are reachable. create_swap() checks that a "
        "chain is CONFIGURED and, since 2026-10-11, that it ANSWERED when last asked; "
        "neither is 'is reachable right now', and a message that says so will be trusted."
    )


def test_create_swap_refuses_an_unreachable_SOURCE_before_minting_an_address(db):
    """The source is the leg that mints the deposit address, so it is checked first.

    "it never told me where to send 500grc" -- the operator, on the step this prevents
    being reached at all.
    """
    quote = seed_quote(db, from_asset="GRC", to_asset="LTC")
    adapters = {"GRC": RefusesToBeAsked("GRC"), "LTC": Answers("LTC")}
    payout_service._REPORTED_INVENTORY_FAILURES.clear()
    payout_service.refresh_wallet_inventory(db, adapters)

    with pytest.raises(ValueError) as refused:
        create_swap(db, swap_config(), adapters, quote, LTC_PAYOUT)
    assert "GRC" in str(refused.value)


def test_a_stale_reading_does_NOT_refuse_a_swap(db):
    """The gate must not outlive its evidence. A stale row means the WORKERS are down.

    Proven by the adapter being reached: RefusesToBeAsked raises from address_report(),
    so an AssertionError here is the gate correctly standing aside and letting the swap
    proceed to the step that actually touches the chain.
    """
    quote = seed_quote(db)
    adapters = {"GRC": Answers("GRC"), "LTC": RefusesToBeAsked("LTC")}
    record_reachability(db, "LTC", "get_balance", reachable=False, detail=TIMED_OUT)
    db.execute(
        "UPDATE chain_reachability SET checked_at = ? WHERE asset = 'LTC'",
        ("2020-01-01T00:00:00+00:00",),
    )
    db.commit()

    with pytest.raises(AssertionError, match="already known not to answer"):
        create_swap(db, swap_config(), adapters, quote, LTC_PAYOUT)


def test_a_MISSING_table_does_not_refuse_a_swap(db):
    """THE GATE'S OWN FAILURE MUST NOT REFUSE. It exists to stop a swap choking.

    An older database file that has not had SCHEMA applied has no such table, and that
    must never be the thing that turns a customer away. Same proof as above: the
    adapter being reached is the gate standing aside.
    """
    quote = seed_quote(db)
    adapters = {"GRC": Answers("GRC"), "LTC": RefusesToBeAsked("LTC")}
    db.execute("DROP TABLE chain_reachability")
    db.commit()

    with pytest.raises(AssertionError, match="already known not to answer"):
        create_swap(db, swap_config(), adapters, quote, LTC_PAYOUT)


def test_a_reachable_pair_is_not_stopped_by_the_gate(db):
    """The gate must be invisible when both chains answered. Nothing new refuses.

    Reaching the adapter is the assertion: the swap gets past the gate and on to the
    checks that were always there.
    """
    quote = seed_quote(db)
    adapters = {"GRC": Answers("GRC"), "LTC": RefusesToBeAsked("LTC")}
    record_reachability(db, "GRC", "get_balance", reachable=True)
    record_reachability(db, "LTC", "get_balance", reachable=True)
    db.commit()

    with pytest.raises(AssertionError, match="already known not to answer"):
        create_swap(db, swap_config(), adapters, quote, LTC_PAYOUT)


# --- a by-design refusal is NOT an outage ----------------------------------------
#
# THE BUG THIS SECTION EXISTS FOR, measured on the operator's host 2026-10-11, in the
# first run of the gate itself:
#
#     XRP did NOT answer  via get_balance
#         get_balance() answers 'what is MY balance' and this adapter has no account
#         of its own, deliberately
#
# XRPAdapter.get_balance() refuses BY DESIGN: the XRP Ledger deposit and payout account
# is XRP_DEPOSIT_ACCOUNT, supplied at the call site, and the adapter deliberately holds
# none so it is never one configuration value away from being a payout path. Recording
# that as unreachable made the gate refuse EVERY XRP PAIR for a chain that was fine --
# a working chain blocked by a check added to stop chains choking.
#
# chain_reachability.py's header already said this distinction mattered and nothing
# implemented it. Rule 17: a comment is a reason to believe, not a check.


class ByDesignRefusal:
    """An adapter whose get_balance refuses deliberately, as XRPAdapter's does.

    A BARE `raise` WITH NO `from`, which is the distinguishing fact and is why this
    stub is written this way rather than with a chained cause: XRP's refusal is not
    raised from inside an `except`, so it has neither __cause__ nor __context__, and
    that absence is what is_transport_failure() reads.
    """

    can_spend = True

    def __init__(self, asset):
        self.asset = asset

    def get_balance(self):
        raise XRPRPCError(
            "get_balance() answers 'what is MY balance' and this adapter has no account "
            "of its own, deliberately"
        )


def chained(outer: BaseException, cause: BaseException) -> BaseException:
    """`outer` with `cause` as its __cause__, exactly as `raise outer from cause` sets it.

    BUILT RATHER THAN RAISED, and the first draft of these tests raised and caught with
    `except Exception`, which ruff refused (BLE001) and was right to: a test that catches
    everything cannot say which class it meant. Constructing the structure directly says
    it, needs no try block, and is what is_transport_failure() actually walks.
    """
    outer.__cause__ = cause
    return outer


def test_a_propagated_transport_failure_IS_one():
    """BTC, LTC and GRC: chains/base.RPCAdapter lets requests' exception through."""
    assert is_transport_failure(requests.exceptions.ConnectTimeout("dropped SYN")) is True
    assert is_transport_failure(ConnectionError("refused")) is True


def test_a_WRAPPED_transport_failure_IS_one():
    """SOL and ICP re-raise with `from exc`, so the cause chain has to be walked.

    chains/solana.py: `except requests.RequestException as exc: raise SolanaRPCError(...)
    from exc`. The outer class is not an OSError; the cause is. Checking only the
    exception would leave a genuinely unreachable Solana cluster ungated.
    """
    wrapped = chained(
        SolanaRPCError("could not reach the cluster"),
        requests.exceptions.ConnectionError("dropped"),
    )
    assert is_transport_failure(wrapped) is True


def test_a_dfx_TIMEOUT_is_one_even_though_it_is_not_an_OSError():
    """chains/icp.py runs `dfx` as a subprocess. TimeoutExpired inherits SubprocessError.

    Listed explicitly in _TRANSPORT_FAILURES because an OSError-only test would leave an
    unreachable ICP replica permanently ungated.
    """
    wrapped = chained(ICPCallFailed("dfx did not answer"), subprocess.TimeoutExpired("dfx", 60))
    assert is_transport_failure(wrapped) is True
    # And the bare TimeoutExpired, since it is listed in its own right.
    assert is_transport_failure(subprocess.TimeoutExpired("dfx", 60)) is True


def test_a_BY_DESIGN_refusal_is_NOT_a_transport_failure():
    """THE ASSERTION THE OPERATOR'S HOST WOULD HAVE FAILED."""
    refusal = XRPRPCError("this adapter has no account of its own, deliberately")
    assert refusal.__cause__ is None, "the absence of a cause IS the distinguishing fact"
    assert is_transport_failure(refusal) is False, (
        "a deliberate refusal reads as an outage, so the gate refuses every pair on a chain "
        "that is working. This is the XRP row from 2026-10-11."
    )


def test_an_RPC_ERROR_REPLY_is_NOT_a_transport_failure():
    """A daemon that answered "insufficient funds" was REACHED. It is not an outage."""
    assert is_transport_failure(SolanaRPCError("insufficient funds for rent exemption")) is False


def test_an_unknown_failure_shape_defaults_to_NOT_a_transport_failure():
    """FALSE IS THE SAFE DEFAULT: silence, not a refusal of a chain that may be fine.

    An adapter inventing a new failure class and chaining nothing must not cause its
    chain to be gated. The opposite default would refuse a working chain on an
    exception nobody anticipated -- the defect this function exists to undo.
    """
    assert is_transport_failure(RuntimeError("something nobody anticipated")) is False


def test_a_by_design_refusal_writes_NO_row(db):
    """Through the real refresh_wallet_inventory(). No claim, so nothing to gate on."""
    payout_service._REPORTED_INVENTORY_FAILURES.clear()
    payout_service.refresh_wallet_inventory(db, {"XRP": ByDesignRefusal("XRP")})
    assert "XRP" not in reachability_rows(db), (
        "a by-design refusal left a reachability claim standing, which refuses every pair "
        "on that chain"
    )


def test_an_EXISTING_wrong_row_is_WITHDRAWN_on_the_next_cycle(db):
    """SELF-HEALS, rather than waiting out STALE_AFTER_SECONDS.

    The operator's database already held `XRP did NOT answer`, written before
    is_transport_failure() existed. Merely skipping future writes would leave that claim
    gating every XRP pair for ten minutes after the fix deployed. forget_reachability()
    deletes it on the first cycle -- the difference between a fix and a fix that arrives
    in ten minutes.
    """
    record_reachability(db, "XRP", "get_balance", reachable=False, detail="wrongly recorded")
    db.commit()
    assert reachability_rows(db)["XRP"]["reachable"] is False

    payout_service._REPORTED_INVENTORY_FAILURES.clear()
    payout_service.refresh_wallet_inventory(db, {"XRP": ByDesignRefusal("XRP")})
    assert "XRP" not in reachability_rows(db)


def test_a_by_design_refusal_does_not_stop_a_swap(db):
    """The end of the chain: an XRP pair must not be refused by the gate.

    Reaching the adapter is the assertion -- RefusesToBeAsked raises from
    address_report(), so an AssertionError means the gate correctly stood aside.
    """
    # THE DESTINATION MUST ANSWER, and the first draft of this test used
    # RefusesToBeAsked for it -- whose get_balance raises a REAL ConnectionError. So LTC
    # was correctly marked unreachable and the gate correctly refused, and the test never
    # exercised the by-design refusal it was named for. The gate working is what made the
    # test wrong.
    class AnswersButNotForAddresses:
        can_spend = True
        asset = "LTC"

        def get_balance(self):
            return 100.0

        def address_report(self, address, want="both"):
            raise AssertionError("the gate stood aside, which is what this test asserts")

    quote = seed_quote(db, from_asset="GRC", to_asset="LTC")
    adapters = {"GRC": ByDesignRefusal("GRC"), "LTC": AnswersButNotForAddresses()}
    payout_service._REPORTED_INVENTORY_FAILURES.clear()
    payout_service.refresh_wallet_inventory(db, adapters)
    assert "GRC" not in reachability_rows(db), "a by-design refusal left a claim standing"
    with pytest.raises(AssertionError, match="the gate stood aside"):
        create_swap(db, swap_config(), adapters, quote, LTC_PAYOUT)


def test_a_transport_failure_still_writes_its_row(db):
    """The fix must not have turned the gate off. A real outage is still recorded."""

    class RealOutage:
        can_spend = True
        asset = "LTC"

        def get_balance(self):
            raise requests.exceptions.ConnectTimeout(
                "Connection to host.docker.internal timed out. (connect timeout=5.0)"
            )

    payout_service._REPORTED_INVENTORY_FAILURES.clear()
    payout_service.refresh_wallet_inventory(db, {"LTC": RealOutage()})
    assert reachability_rows(db)["LTC"]["reachable"] is False
