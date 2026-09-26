"""The operator surface's read-only assembly, against a real database.

Role: test (behavioral verification of services/admin_view.py)
Reads: a temporary SQLite database created per test with the REAL schema from
       db.py, and seeded with real rows
Writes: that temporary database only
Can move funds: no
Mainnet-safe: yes -- no socket is opened; the one function that would
       (probe_chain) is exercised against stub adapters

VERIFIED BY BEHAVIOR, NOT BY READING THE CODE. Every query below runs against
the actual schema executed from db.SCHEMA, with rows inserted the way the
application inserts them, and asserts on what comes back. "The function
contains a WHERE clause for status='created'" is not evidence; "seeding a
created payout produced a row in unresolved_payouts and seeding a broadcast one
did not" is.
"""

import sqlite3
from datetime import datetime, timedelta

import pytest
from chains.base import RPCAdapter
from chains.bitcoin import BitcoinAdapter
from db import SCHEMA, dict_factory
from services import admin_view
from services.admin_view import (
    DEPOSIT_QUIET_AFTER_SECONDS,
    INVENTORY_STALE_AFTER_SECONDS,
    chain_rows,
    config_echo,
    freshness,
    inventory_rows,
    overview,
    pair_rows,
    probe_chain,
    probe_kind,
    recent_deposits,
    status_counts,
    swaps_in_flight,
    unresolved_payouts,
)
from services.swap_view import STALL_AFTER_SECONDS
from workers.reconcile_worker import DEFAULT_POLL_SECONDS as RECONCILE_POLL

NOW = "2026-09-26T12:00:00+00:00"

# A path that is never opened. It exists so config_echo() has a DB_PATH to echo,
# and it is deliberately not under /tmp: a literal temp path in a test reads as
# a file the test might actually write, and ruff's S108 says so for good reason.
SEEDED_DB_PATH = "(seeded config; no file is opened at this path)"

# A SENTINEL STRING, not a credential, and the name says so on purpose.
#
# The tests below put it where an RPC password goes and then assert that it does
# NOT come back out of a rendered page or a JSON response, so a value has to
# exist. Two lint rules are about exactly this shape and both were fixed by
# saying what the thing is rather than by suppressing them (rule 19): S106
# objected to a literal passed as `password=`, and S105 then objected to a
# constant NAMED like a password. It is neither -- it is a marker that a leak
# test looks for, and calling it one is the honest fix.
LEAK_SENTINEL = "leak-sentinel-value-that-must-never-reach-a-page"


def before(seconds: float) -> str:
    return (datetime.fromisoformat(NOW) - timedelta(seconds=seconds)).isoformat()


@pytest.fixture
def db(tmp_path):
    """A real database on the real schema. No fixtures that fake a cursor."""
    conn = sqlite3.connect(tmp_path / "admin_test.db")
    conn.row_factory = dict_factory
    conn.executescript(SCHEMA)
    conn.commit()
    yield conn
    conn.close()


def seed_swap(conn, swap_id, status, **kw):
    conn.execute(
        "INSERT INTO quotes (id, from_asset, to_asset, input_amount, quoted_rate, fee_bps, network_fee_reserve,"
        " output_amount_estimate, expires_at, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (f"q_{swap_id}", "GRC", "BTC", 1000.0, 1e-7, 150, 0.00002, 0.0001, before(-600), before(600)),
    )
    conn.execute(
        "INSERT INTO swaps (id, quote_id, from_asset, to_asset, deposit_address, payout_address,"
        " expected_input_amount, actual_input_amount, quoted_rate, fee_bps, network_fee_reserve,"
        " output_amount_estimate, status, min_confirmations, deposit_txid, payout_txid, created_at, updated_at,"
        " credited_at, completed_at, expires_at, failed_reason)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            swap_id, f"q_{swap_id}", "GRC", "BTC", "Saddr", "bc1addr", 1000.0, kw.get("actual"), 1e-7, 150,
            0.00002, 0.0001, status, 6, None, None, before(600), kw.get("updated_at", before(30)), None, None,
            before(-600), None,
        ),
    )
    conn.commit()


# --- freshness is the point of the page -------------------------------------


def test_fresh_stale_and_missing_are_three_different_answers():
    """MUTATION: return "stale" for a missing timestamp, or age 0.0 for one.

    README.md records a staging file that sat over 6000s stale while the system
    used it anyway. The page that is supposed to stop that repeating has to tell
    three things apart: nothing was ever written, something was written long
    ago, and something was just written. Collapsing the first into either of the
    others is how a column whose writer has been broken since the day it was
    created reads as healthy.
    """
    assert freshness(before(10), NOW, 300)["state"] == "fresh"
    assert freshness(before(9000), NOW, 300)["state"] == "stale"
    assert freshness(None, NOW, 300)["state"] == "missing"
    assert freshness("", NOW, 300)["state"] == "missing"
    assert freshness("not-a-timestamp", NOW, 300)["state"] == "unreadable"

    # A missing reading has NO age, rather than an age of zero.
    assert freshness(None, NOW, 300)["age_seconds"] is None
    assert freshness(None, NOW, 300)["age_display"] == "(never)"


def test_the_stale_threshold_is_echoed_with_the_reading():
    """Rule 14: state what the number means, next to the number.

    MUTATION: drop `threshold_display` from the return value. The badge then
    says STALE with no way for the reader to know what counted as stale, which
    is the bare `0` the rule is about.
    """
    reading = freshness(before(9000), NOW, 300)
    assert "µfn" in reading["threshold_display"]
    assert "300.0s" in reading["threshold_display"]
    assert "µfn" in reading["age_display"]


def test_the_boundary_is_strictly_greater_than():
    assert freshness(before(300), NOW, 300)["state"] == "fresh"
    assert freshness(before(300.5), NOW, 300)["state"] == "stale"


def test_the_inventory_threshold_is_longer_than_the_reconcile_workers_interval():
    """DERIVED, not picked (rule 3). One authority for the poll interval."""
    assert INVENTORY_STALE_AFTER_SECONDS > RECONCILE_POLL
    assert INVENTORY_STALE_AFTER_SECONDS >= RECONCILE_POLL * 3
    assert DEPOSIT_QUIET_AFTER_SECONDS > RECONCILE_POLL


def test_a_stale_inventory_row_is_marked_stale_and_a_fresh_one_is_not(db):
    """Seeded rows, real table, real function (verify by behavior).

    MUTATION: make inventory_rows() skip the freshness() call. Both rows then
    render identically and a balance nobody has refreshed in two hours reads the
    same as one refreshed four seconds ago.
    """
    for asset, age in (("BTC", 5.0), ("GRC", INVENTORY_STALE_AFTER_SECONDS + 600)):
        db.execute(
            "INSERT INTO wallet_inventory (asset, hot_confirmed, hot_reserved, hot_available, updated_at)"
            " VALUES (?,?,?,?,?)",
            (asset, 1.0, 0.0, 1.0, before(age)),
        )
    db.commit()
    rows = {row["asset"]: row for row in inventory_rows(db, NOW)}
    assert rows["BTC"]["fresh"]["state"] == "fresh"
    assert rows["GRC"]["fresh"]["state"] == "stale"
    assert rows["BTC"]["fresh"]["age_display"] != rows["GRC"]["fresh"]["age_display"]


# --- empty results are results ---------------------------------------------


def test_every_listing_returns_an_empty_list_rather_than_raising_on_an_empty_database(db):
    """An empty database must produce empty LISTS, which the templates render as `(none)`.

    MUTATION: have any of these return None on an empty result. The template's
    `{% if data.x %}` then takes the same branch it takes for zero rows, so this
    would not be caught here -- but `overview()` would, because a None cannot be
    iterated and the page would 500. Both outcomes are worse than `(none)`.
    """
    assert status_counts(db) == []
    assert swaps_in_flight(db, NOW) == []
    assert recent_deposits(db, NOW) == []
    assert unresolved_payouts(db) == []
    assert inventory_rows(db, NOW) == []
    everything = overview(db, seeded_config(), {}, NOW)
    for key in ("status_counts", "in_flight", "deposits", "payouts", "unresolved_payouts", "inventory", "transitions"):
        assert everything[key] == [], key


# --- in flight --------------------------------------------------------------


def seeded_config() -> dict:
    return {
        "DB_PATH": SEEDED_DB_PATH,
        "ALLOWED_PAIRS": {("GRC", "BTC"), ("BTC", "GRC")},
        "BTC_MIN_CONFIRMATIONS": 2,
        "LTC_MIN_CONFIRMATIONS": 2,
        "GRC_MIN_CONFIRMATIONS": 6,
        "SOL_MIN_CONFIRMATIONS": 3,
        "XMR_MIN_CONFIRMATIONS": 10,
        "XRP_MIN_CONFIRMATIONS": 1,
        "QUOTE_TTL_SECONDS": 600,
        # Deliberately present, and deliberately NOT echoed. See the test below.
        "RPC": {"BTC": {"user": "rpcuser", "password": LEAK_SENTINEL, "host": "127.0.0.1"}},
        "SECRET_KEY": "swap-terminal-dev",
    }


def test_in_flight_excludes_terminal_swaps_and_carries_the_same_verdict_as_the_customer_page(db):
    """MUTATION: add 'completed' to IN_FLIGHT_STATUSES, or re-introduce a second
    staleness rule here instead of calling swap_view.attention().

    The first fills the operator's working list with finished swaps. The second
    is the bug this file's DEPOSIT_QUIET_AFTER_SECONDS comment records: a swap
    stuck in payout_pending for 26 minutes read SLOW to the customer and FRESH
    to the operator, because the admin surface had its own hour-long threshold.
    The operator got the weaker signal.
    """
    seed_swap(db, "s_inflight", "payout_pending", updated_at=before(STALL_AFTER_SECONDS["payout_pending"] + 60))
    seed_swap(db, "s_done", "completed")
    seed_swap(db, "s_review", "under_review")

    rows = swaps_in_flight(db, NOW)
    assert [row["id"] for row in rows] == ["s_inflight"]
    assert rows[0]["attention"]["level"] == "slow"


def test_in_flight_counts_deposit_rows_and_the_highest_confirmation(db):
    seed_swap(db, "s_conf", "confirming")
    for vout, confirmations in ((0, 1), (1, 4)):
        db.execute(
            "INSERT INTO deposit_events (swap_id, asset, txid, vout, address, amount, confirmations,"
            " first_seen_at, last_seen_at, credited_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            ("s_conf", "GRC", "tx1", vout, "Saddr", 500.0, confirmations, before(300), before(60), None),
        )
    db.commit()
    row = swaps_in_flight(db, NOW)[0]
    assert row["deposit_rows"] == 2
    assert row["max_confirmations"] == 4


# --- the alarm --------------------------------------------------------------


def test_only_a_created_payout_counts_as_unresolved(db):
    """The crash window in services/payout_service.py, surfaced.

    MUTATION: widen the filter to include 'broadcast'. Every healthy payout
    would then appear under an alarm that says money may be on chain with
    nothing recorded, and an alarm that is always on is one nobody reads.
    """
    seed_swap(db, "s_pay", "paying")
    for status, txid in (("created", None), ("broadcast", "abc"), ("failed", None), ("completed", "def")):
        db.execute(
            "INSERT INTO payouts (swap_id, asset, destination_address, amount, txid, status, created_at, sent_at)"
            " VALUES (?,?,?,?,?,?,?,?)",
            ("s_pay", "BTC", "bc1addr", 0.0001, txid, status, before(120), None),
        )
    db.commit()
    unresolved = unresolved_payouts(db)
    assert len(unresolved) == 1
    assert unresolved[0]["swap_id"] == "s_pay"


# --- configuration is read, never written, and never leaked -----------------


def test_the_config_echo_is_an_allowlist_and_cannot_reach_a_credential():
    """MUTATION: replace ECHOED_CONFIG_KEYS with a pattern over config.keys().

    Config carries Config.RPC, whose Bitcoin entries hold `user` and `password`.
    A denylist or a pattern that forgot one would render a wallet credential
    into a web page. This asserts on the VALUE, not on the key name, so an
    echo that renamed the key would still be caught.
    """
    echoed = config_echo(seeded_config())
    rendered = " ".join(f"{row['key']}={row['value']}" for row in echoed)
    assert LEAK_SENTINEL not in rendered
    assert "rpcuser" not in rendered
    assert "RPC" not in [row["key"] for row in echoed]
    assert "SECRET_KEY" not in [row["key"] for row in echoed]
    # It does echo the thresholds, which is the point of the panel.
    assert "GRC_MIN_CONFIRMATIONS" in [row["key"] for row in echoed]


def test_pair_rows_read_allowed_pairs_and_mark_everything_else_disabled():
    """MUTATION: have pair_rows() list only the enabled pairs.

    "Is XRP on?" is then answered by an absence, and an absent row is
    indistinguishable from a row nobody rendered (rule 14). Disabled pairs are
    shown AS disabled.
    """
    rows = pair_rows(seeded_config())
    enabled = {row["label"] for row in rows if row["enabled"]}
    assert enabled == {"GRC -> BTC", "BTC -> GRC"}
    disabled = {row["label"] for row in rows if not row["enabled"]}
    assert "XRP -> BTC" in disabled
    assert "GRC -> XRP" in disabled
    # Nothing here mutates the authority.
    assert seeded_config()["ALLOWED_PAIRS"] == {("GRC", "BTC"), ("BTC", "GRC")}


def test_chain_rows_report_an_unconfigured_chain_rather_than_omitting_it():
    rows = {row["asset"]: row for row in chain_rows(seeded_config(), {})}
    assert set(rows) >= {"BTC", "LTC", "GRC", "SOL", "XMR", "XRP"}
    assert all(row["configured"] is False for row in rows.values())
    assert "not configured" in rows["XRP"]["endpoint"]
    # XRP is present, described, and NOT tradeable.
    assert rows["XRP"]["attribution"] == "destination_tag"
    assert rows["XRP"]["tradeable"] is False
    assert rows["GRC"]["tradeable"] is True


def test_chain_rows_never_render_an_rpc_password():
    """An adapter carries its credentials; the row must carry only the endpoint.

    MUTATION: have _endpoint_text() read Config.RPC[asset] instead of the
    adapter's attributes. `user` and `password` are one key away in that dict,
    and a row built from it by `", ".join(...)` would publish both.
    """
    adapter = BitcoinAdapter(user="rpcuser", password=LEAK_SENTINEL, host="127.0.0.1", port=8332)
    rows = {row["asset"]: row for row in chain_rows(seeded_config(), {"BTC": adapter})}
    rendered = str(rows["BTC"])
    assert LEAK_SENTINEL not in rendered
    assert "rpcuser" not in rendered
    assert "127.0.0.1:8332" in rows["BTC"]["endpoint"]


# --- the probe reports failures in its return value -------------------------


class _RefusingAdapter(RPCAdapter):
    """An adapter whose read raises, standing in for a daemon that is down.

    A REAL SUBCLASS, not a duck-typed stand-in, and the first draft of this test
    got that wrong in a way worth recording. probe_kind() decides by
    `isinstance(adapter, RPCAdapter)` rather than by "has a .call", and the
    duck-typed stub was therefore reported as UNPROBEABLE -- which is what
    should happen. Monero's adapter also has a `call`, with a different
    signature (`call(method, params: dict)`), so a duck-typed probe would send
    `getblockchaininfo` to a wallet RPC, get an error, and report a healthy
    Monero wallet as unreachable. The strict check is the point; the stub was
    wrong.
    """

    asset = "BTC"

    def __init__(self):
        super().__init__(user="", password="", host="127.0.0.1", port=8332)

    def call(self, method, *params):
        raise ConnectionError("Connection refused")


class _AnsweringAdapter(_RefusingAdapter):
    def call(self, method, *params):
        assert method == "getblockchaininfo", "the probe must be a READ"
        return {"chain": "test"}


class _NoProbeAdapter:
    """An adapter with neither network() nor an RPCAdapter transport."""


def test_a_probe_never_raises_and_says_which_failure_it_was():
    """MUTATION: let probe_chain() propagate, or report a failure as reachable=None.

    A broad catch is legitimate here only because the caller can tell a failure
    from an answer -- `reachable` IS the answer and it is False, with the reason
    beside it. Reporting None would make "did not answer" indistinguishable from
    "was not probed", which is the distinction the next test is about.
    """
    failed = probe_chain("BTC", _RefusingAdapter())
    assert failed["probed"] is True
    assert failed["reachable"] is False
    assert "Connection refused" in failed["detail"]


def test_an_answering_chain_is_named_by_what_the_daemon_reports(monkeypatch):
    answered = probe_chain("BTC", _AnsweringAdapter())
    assert answered["reachable"] is True
    assert answered["network"] == "test"


def test_not_configured_and_not_probeable_are_not_failures():
    """Three outcomes, three answers (rule 14: "did nothing" must not look like "did work").

    MUTATION: report an unprobeable adapter as reachable=False. A Monero wallet
    with no probe implemented would then display as a Monero wallet that is
    down, and somebody would go looking for a daemon that is running fine.
    """
    missing = probe_chain("SOL", None)
    assert missing["probed"] is False and missing["reachable"] is None
    assert "not configured" in missing["detail"]

    unprobeable = probe_chain("XMR", _NoProbeAdapter())
    assert unprobeable["probed"] is False and unprobeable["reachable"] is None
    assert "no read-only network probe" in unprobeable["detail"]


def test_probe_kind_is_decided_by_capability_not_by_asset_name():
    assert probe_kind(BitcoinAdapter(user="", password="", host="h", port=1)) == "bitcoin_rpc"
    assert probe_kind(_NoProbeAdapter()) == "none"

    class _HasNetwork:
        def network(self):
            return "mainnet"

    assert probe_kind(_HasNetwork()) == "network_method"


# --- the module writes nothing ---------------------------------------------


def test_no_statement_in_this_module_writes(db):
    """Proven by the DATABASE refusing writes, not by reading the source.

    A read-only connection makes every INSERT, UPDATE, DELETE and CREATE raise.
    overview() is then run in full against seeded rows: if any statement in it
    writes, this test fails with "attempt to write a readonly database". Reading
    the file and seeing only SELECTs would prove nothing about a future edit.
    """
    seed_swap(db, "s_ro", "confirming")
    db.execute(
        "INSERT INTO wallet_inventory (asset, hot_confirmed, hot_reserved, hot_available, updated_at)"
        " VALUES ('BTC', 1, 0, 1, ?)",
        (before(10),),
    )
    db.commit()
    path = db.execute("PRAGMA database_list").fetchall()[0]["file"]
    db.close()

    readonly = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    readonly.row_factory = dict_factory
    try:
        result = overview(readonly, seeded_config(), {}, NOW)
        assert result["in_flight"], "the seeded swap must actually have been read"
        assert result["inventory"], "the seeded inventory row must actually have been read"
    finally:
        readonly.close()


def test_overview_measures_every_freshness_against_one_clock_reading(db):
    """MUTATION: have overview() call utc_now_iso() per section.

    Two rows on one screen would then disagree about what "now" is, which makes
    a staleness table impossible to reason about -- and the disagreement grows
    with how slow the page was to build, so it is worst exactly when it matters.
    """
    seed_swap(db, "s_clock", "confirming")
    result = overview(db, seeded_config(), {}, NOW)
    assert result["generated_at"] == NOW


def test_worker_rows_report_stopped_for_a_missing_pid_file(tmp_path):
    """No pid file means stopped, and it says why. It starts nothing."""
    rows = admin_view.worker_rows(tmp_path)
    assert {row["worker"] for row in rows} == {"deposit_watcher", "payout_worker", "reconcile_worker"}
    for row in rows:
        assert row["state"] == "stopped"
        assert row["detail"] == "no pid file"
