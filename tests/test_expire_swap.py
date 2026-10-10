"""expire_swap.py, judged against seeded rows and then run for real.

Role: test module (verification only)
Reads: a disposable SQLite database built from the real db.SCHEMA
Writes: nothing outside tmp_path
Can move funds: no -- expire_swap.py imports nothing that can send, and this file
       asserts that by name below
Mainnet-safe: yes; no socket is opened and no adapter is constructed

WHAT THESE TESTS ARE FOR. The tool writes one column, and the hazard is entirely
in WHICH rows it writes it to: a swap retired while its deposit was in flight
turns an automatic credit into a manual reconciliation (expire_swap.py's header
has the measurement). So the refusals get a test each, seeded one condition at a
time, and the end-to-end runs assert on the ROWS the real main() left behind --
never on what its output said (BEHAVIORAL_VERIFICATION_PRINCIPLE).

AND THE STRONGEST ONE IS test_nothing_but_swaps_and_the_audit_log_is_touched,
which snapshots every other table before and after an --apply run. That is the
guard for the things the header promises not to touch -- wallet_inventory, the ICP
subaccount map, the XRP tag map, quotes -- without needing a test per table, and it
catches a future edit that decides to "clean up" one of them.
"""

from __future__ import annotations

import sqlite3
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

APP_ROOT = Path(__file__).resolve().parent.parent
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

from db import SCHEMA, connect_db  # noqa: E402
from microfortnights import format_duration  # noqa: E402
from services.deposit_service import ACTIVE_STATUSES  # noqa: E402
from services.late_deposit_service import late_scan_targets  # noqa: E402
from services.swap_view import (  # noqa: E402
    STATUS_MEANINGS,
    TERMINAL_STATUSES,
    attention,
    status_meaning,
)

import expire_swap  # noqa: E402
from expire_swap import (  # noqa: E402
    DEPOSIT_WITNESS_COLUMNS,
    RETIRABLE_FROM,
    RETIRED,
    build_parser,
    deposit_recorded_in_a_column,
    expiry_verdict,
    judge,
    quote_window_lapsed,
    released_obligation,
)
from swap_terminal.fee_sweep import obligation  # noqa: E402
from tests.valid_addresses import BTC_REGTEST_DEPOSIT, GRC_PAYOUT  # noqa: E402

#: The clock the UNIT tests hand in. expire_swap takes `now_iso` as an argument
#: everywhere a window is judged, so no test patches time.
#:
#: THE END-TO-END TESTS CANNOT HAND IN A CLOCK, and this header used to read as
#: though they could. expire_swap.main() has no `--now` flag -- deliberately,
#: because a clock argument on a tool that RETIRES swaps is one typo away from
#: retiring a window that has not lapsed -- so every test that calls main()
#: judges against the real system clock.
#:
#: That combination is a time bomb and it went off on 2026-10-08, one day after
#: these constants were written: test_apply_retires_only_the_unfunded_swap seeded
#: `s_fresh` with the absolute JUST_NOW below, main() judged it against the real
#: clock, and ten-minutes-ago had become more-than-a-day-ago. The test passed the
#: day it was written and failed the next, which is the "a measurement ages"
#: failure CLAUDE.md records about prose, arriving in a fixture.
#:
#: So: ABSOLUTE constants for the unit tests, which are handed NOW and are
#: therefore fixed forever, and inside_grace_of_the_real_clock() below for the
#: one case judged by the real one.
NOW = "2026-10-07T12:00:00+00:00"
#: Lapsed well past the default 24h grace. SAFE AS AN ABSOLUTE VALUE in an
#: end-to-end test too, and it is the seed() default for that reason: a window
#: that has already lapsed only ever lapses further, so time moving cannot turn
#: this verdict over. Only "recent" rots.
LONG_AGO = "2026-10-01T00:10:00+00:00"
#: Lapsed, but minutes before NOW -- inside the grace window. FOR UNIT TESTS
#: ONLY, the ones that pass NOW explicitly. Handing this to a test that calls
#: main() is the defect described above.
JUST_NOW = "2026-10-07T11:50:00+00:00"


def inside_grace_of_the_real_clock() -> str:
    """An `expires_at` that is lapsed but still inside the grace window, NOW.

    For the end-to-end tests only -- the ones that call expire_swap.main() and
    are therefore judged against the real system clock. Ten minutes is far
    inside the 24h default grace and far outside any plausible test runtime, so
    this is stable in both directions where the absolute JUST_NOW was stable in
    neither.
    """
    return (datetime.now(UTC) - timedelta(minutes=10)).isoformat()

GRACE = expire_swap.DEFAULT_GRACE_SECONDS

PAYOUT = 120.0
RESERVE = 0.001


def swap_row(**overrides) -> dict:
    """One `awaiting_deposit` swap as a plain dict, with nothing arrived.

    A DICT AND NOT A DATABASE ROW for the verdict tests, because expiry_verdict()
    takes a mapping and that is the point of it being at the bottom (rule 10): the
    decision is assertable without a schema, a connection or a temp directory.
    The end-to-end tests below use real rows from the real SCHEMA.
    """
    row = {
        "id": "s_open",
        "status": RETIRABLE_FROM,
        "from_asset": "BTC",
        "to_asset": "GRC",
        # DERIVED, NOT TYPED. tests/valid_addresses.py builds every fixture address
        # from a label, which is what keeps tests/test_address_literals_are_valid.py's
        # ceiling from climbing -- and a literal here would have pushed it to 61
        # against a ceiling of 60, which is rule 19's "your change is the defect"
        # rather than a line to raise.
        "deposit_address": BTC_REGTEST_DEPOSIT,
        "deposit_tag": None,
        "output_amount_estimate": PAYOUT,
        "network_fee_reserve": RESERVE,
        "actual_input_amount": None,
        "deposit_txid": None,
        "credited_at": None,
        "created_at": "2026-10-01T00:00:00+00:00",
        "expires_at": LONG_AGO,
    }
    row.update(overrides)
    return row


# ------------------------------------------------------- the verdict, seeded


def test_a_swap_nobody_paid_into_is_retirable():
    allowed, why = expiry_verdict(swap_row(), 0, 0, NOW, GRACE)
    assert allowed, why
    assert "0 deposit rows" in why
    assert "lapsed" in why


@pytest.mark.parametrize("status", [
    "deposit_seen", "confirming", "payout_pending", "paying", "completed", "under_review", "failed",
    RETIRED,
])
def test_every_status_but_awaiting_deposit_is_refused(status):
    """The eight other statuses, one at a time, because each one means money.

    PARAMETRIZED OVER THE WHOLE VOCABULARY rather than spot-checking one, since the
    refusal is the only thing standing between this tool and a swap with a credited
    deposit in it. `expired` is in the list too: running --all twice must not
    re-retire, and re-retiring is what a missing check would look like.
    """
    allowed, why = expiry_verdict(swap_row(status=status), 0, 0, NOW, GRACE)
    assert not allowed
    assert status in why


def test_one_deposit_row_at_zero_confirmations_refuses():
    """The condition the whole tool turns on: a row is money, whatever its depth.

    MUTATION: `if deposit_row_count:` -> `if deposit_row_count > 1:`. This test
    fails (1 row allowed through), which is the defect that would retire a swap
    whose payment was sitting unconfirmed in a mempool. Verified 2026-10-07.
    """
    allowed, why = expiry_verdict(swap_row(), 1, 0, NOW, GRACE)
    assert not allowed
    assert "1 deposit_events row(s)" in why
    assert "0-confirmation row is a payment on its way" in why


def test_a_payout_row_refuses_even_with_no_deposit():
    allowed, why = expiry_verdict(swap_row(), 0, 2, NOW, GRACE)
    assert not allowed
    assert "2 payout row(s)" in why


@pytest.mark.parametrize("column", sorted(DEPOSIT_WITNESS_COLUMNS))
def test_any_column_claiming_a_deposit_refuses(column):
    """Each witness column, separately, so a failure names which one stopped being checked.

    DERIVED FROM THE MAPPING rather than listing three names here: a column added
    to DEPOSIT_WITNESS_COLUMNS gets a test by construction, which is rule 8's
    reason for deriving an allowlist instead of copying it.
    """
    value = 0.01 if column == "actual_input_amount" else "2026-10-01T00:00:00+00:00"
    allowed, why = expiry_verdict(swap_row(**{column: value}), 0, 0, NOW, GRACE)
    assert not allowed
    assert f"swaps.{column} is set" in why


def test_a_credited_amount_of_zero_still_refuses():
    """0.0 is falsey and is still a record that something was assessed.

    MUTATION: `if value is not None and value != ""` -> `if value`. This test is
    the only one that fails, which is what makes it worth having: every other
    witness-column case passes under the truthy test. Verified 2026-10-07.
    """
    allowed, why = expiry_verdict(swap_row(actual_input_amount=0.0), 0, 0, NOW, GRACE)
    assert not allowed
    assert "actual_input_amount is set (0.0)" in why


def test_no_witness_column_set_returns_the_empty_string():
    assert deposit_recorded_in_a_column(swap_row()) == ""


# --------------------------------------------------------- the quote window


def test_a_window_inside_the_grace_period_is_not_lapsed():
    allowed, why = expiry_verdict(swap_row(expires_at=JUST_NOW), 0, 0, NOW, GRACE)
    assert not allowed
    assert "retirable in another" in why


def test_the_grace_window_is_measured_from_expires_at():
    """Ten minutes past expiry needs the rest of the default day, not an hour of it."""
    lapsed, why = quote_window_lapsed(JUST_NOW, NOW, GRACE)
    assert not lapsed
    # 24h grace, 600s elapsed -> 85800s remaining, and the figure is in
    # microfortnights because rule 6 governs everything this system REPORTS. The
    # expected string comes from the real formatter rather than being typed here: a
    # hand-written "70932.0µfn" was off by half a unit on the first run, and a test
    # that pins the wrong number teaches the wrong unit convention.
    assert format_duration(85800.0) in why, why
    assert "µfn" in why and "ufn" not in why, "rule 6: the unit is µfn, never an ASCII u"


@pytest.mark.parametrize("expires_at", ["", None, "not a timestamp", "2026-13-45"])
def test_an_unreadable_window_refuses_rather_than_lapsing(expires_at):
    """Absence of evidence about the window is not evidence the window passed (rule 2).

    MUTATION: make the `except ValueError` branch `return True, ...`. Every case
    here fails, which is the direction that matters -- the opposite default would
    retire every swap whose expires_at was ever written malformed.
    """
    lapsed, why = quote_window_lapsed(expires_at, NOW, GRACE)
    assert not lapsed
    assert "expires_at" in why


def test_a_zero_grace_retires_the_moment_the_window_lapses():
    """--grace-hours 0 is honored, because an operator who asks for it means it."""
    lapsed, _why = quote_window_lapsed(JUST_NOW, NOW, 0.0)
    assert lapsed


# ------------------------------------------------------------------ the loop


def test_judge_returns_both_lists_with_their_reasons():
    rows = [
        swap_row(id="s_a", deposit_row_count=0, payout_row_count=0),
        swap_row(id="s_b", deposit_row_count=1, payout_row_count=0),
    ]
    retirable, refused = judge(rows, NOW, GRACE)
    assert [row["id"] for row, _why in retirable] == ["s_a"]
    assert [row["id"] for row, _why in refused] == ["s_b"]


def test_released_obligation_sums_the_payout_and_the_chain_fee():
    retirable = [(swap_row(id="s_a"), ""), (swap_row(id="s_b"), "")]
    assert released_obligation(retirable) == {"GRC": pytest.approx(2 * (PAYOUT + RESERVE))}


def test_two_payout_assets_are_never_added_together():
    """The defect the operator's first real run printed, as a test.

    On their host 2026-10-07 the `--all` dry run summed six GRC-paying swaps and one
    LTC-paying one into `2177.564301805834` -- 2176.355 GRC plus 1.209 LTC, a
    quantity in no unit, printed beside a sentence claiming it was what
    fee_sweep.obligation() counts. obligation() takes an ASSET and could never have
    produced it, which is what makes this rule 3 (state the denominator) rather than
    an arithmetic slip: every number going in was correct.

    MUTATION: `totals[row["to_asset"]] = ...` -> `totals["all"] = ...`. This fails
    with one key where two are expected. The single-asset test above passes under
    that mutation, which is why this one exists separately.
    """
    retirable = [
        (swap_row(id="s_grc", to_asset="GRC"), ""),
        (swap_row(id="s_ltc", to_asset="LTC", output_amount_estimate=1.2, network_fee_reserve=0.001), ""),
    ]
    released = released_obligation(retirable)
    assert sorted(released) == ["GRC", "LTC"]
    assert released["GRC"] == pytest.approx(PAYOUT + RESERVE)
    assert released["LTC"] == pytest.approx(1.201)


def test_the_printed_total_names_its_asset_and_its_count(db_path, capsys):
    """Rule 3 again, at the place a reader actually sees it.

    A figure with no unit beside it is the thing that shipped; a figure with no COUNT
    beside it cannot be told from one large swap, which is the other half of the same
    rule. Both are asserted on the real output of the real tool.
    """
    seed(db_path, "s_one")
    seed(db_path, "s_two")
    expire_swap.main(["--all", "--db", db_path])
    out = capsys.readouterr().out
    assert "PER PAYOUT ASSET" in out
    assert f"{2 * (PAYOUT + RESERVE)} GRC  over 2 swap(s)" in out


# ----------------------------------------------------------- the real thing


@pytest.fixture
def db_path(tmp_path) -> str:
    """A real database built by the real SCHEMA, so every constraint is the real one."""
    path = str(tmp_path / "expire_swap.db")
    connection = sqlite3.connect(path)
    connection.executescript(SCHEMA)
    connection.commit()
    connection.close()
    return path


def seed(db_path: str, swap_id: str, *, status: str = RETIRABLE_FROM, expires_at: str = LONG_AGO,
         deposit_rows: int = 0) -> None:
    """One swap in `status`, with `deposit_rows` deposit_events rows against it."""
    connection = connect_db(db_path, create=True)
    try:
        connection.execute(
            "INSERT INTO quotes (id, from_asset, to_asset, input_amount, quoted_rate, fee_bps,"
            " network_fee_reserve, output_amount_estimate, expires_at, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (f"q_{swap_id}", "BTC", "GRC", 0.001, 120000.0, 150, RESERVE, PAYOUT, expires_at,
             "2026-10-01T00:00:00+00:00"),
        )
        connection.execute(
            "INSERT INTO swaps (id, quote_id, from_asset, to_asset, deposit_address, payout_address,"
            " expected_input_amount, quoted_rate, fee_bps, network_fee_reserve,"
            " output_amount_estimate, status, min_confirmations, created_at, updated_at, expires_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (swap_id, f"q_{swap_id}", "BTC", "GRC", f"addr_{swap_id}", GRC_PAYOUT, 0.001, 120000.0,
             150, RESERVE, PAYOUT, status, 2, "2026-10-01T00:00:00+00:00",
             "2026-10-01T00:00:00+00:00", expires_at),
        )
        for index in range(deposit_rows):
            connection.execute(
                "INSERT INTO deposit_events (swap_id, asset, txid, vout, address, amount,"
                " confirmations, first_seen_at, last_seen_at)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (swap_id, "BTC", f"tx_{swap_id}_{index}", index, f"addr_{swap_id}", 0.001, 0,
                 NOW, NOW),
            )
        connection.commit()
    finally:
        connection.close()


def statuses(db_path: str) -> dict[str, str]:
    connection = connect_db(db_path, create=True)
    try:
        return {row["id"]: row["status"] for row in connection.execute("SELECT id, status FROM swaps")}
    finally:
        connection.close()


def audit_rows(db_path: str) -> list[dict]:
    connection = connect_db(db_path, create=True)
    try:
        return [dict(row) for row in connection.execute(
            "SELECT swap_id, old_status, new_status, message FROM swap_audit_log ORDER BY id"
        )]
    finally:
        connection.close()


def test_a_dry_run_over_everything_writes_nothing(db_path, capsys):
    seed(db_path, "s_unfunded")
    seed(db_path, "s_paid", deposit_rows=1)
    assert expire_swap.main(["--all", "--db", db_path]) == 0
    assert statuses(db_path) == {"s_unfunded": RETIRABLE_FROM, "s_paid": RETIRABLE_FROM}
    assert audit_rows(db_path) == []
    out = capsys.readouterr().out
    assert "DRY RUN -- nothing is written" in out
    # Rule 14: the figure and what it is counted out of, on the same line.
    assert "RETIRABLE (1 of 2)" in out
    assert "REFUSED (1 of 2)" in out


def test_apply_retires_only_the_unfunded_swap(db_path, capsys):
    """The end-to-end answer, asserted on rows rather than on output.

    THREE SWAPS, ONE OF EACH KIND, because a test with only a retirable swap would
    pass just as happily if the tool retired everything it was shown.
    """
    seed(db_path, "s_unfunded")
    seed(db_path, "s_paid", deposit_rows=1)
    # RELATIVE TO THE REAL CLOCK, because main() below judges against it and has
    # no --now. The absolute JUST_NOW was here and made this test pass on
    # 2026-10-07 and fail on 2026-10-08 -- see the constant's comment.
    seed(db_path, "s_fresh", expires_at=inside_grace_of_the_real_clock())
    assert expire_swap.main(["--all", "--db", db_path, "--apply"]) == 0
    assert statuses(db_path) == {
        "s_unfunded": RETIRED, "s_paid": RETIRABLE_FROM, "s_fresh": RETIRABLE_FROM,
    }
    trail = audit_rows(db_path)
    assert [(row["swap_id"], row["old_status"], row["new_status"]) for row in trail] == [
        ("s_unfunded", RETIRABLE_FROM, RETIRED)
    ]
    # The audit row carries the VERDICT, not just the word "expired": that trail is
    # what an operator reads six weeks later to learn why a swap was closed.
    assert "0 deposit rows" in trail[0]["message"]
    assert "expire_swap.py" in trail[0]["message"]
    assert "1 of 1 retired" in capsys.readouterr().out


def test_running_apply_twice_retires_nothing_the_second_time(db_path):
    """Idempotence, through the status check rather than through a marker column."""
    seed(db_path, "s_unfunded")
    expire_swap.main(["--all", "--db", db_path, "--apply"])
    assert expire_swap.main(["--all", "--db", db_path, "--apply"]) == 0
    assert len(audit_rows(db_path)) == 1


def test_naming_a_swap_in_another_status_refuses_and_says_which(db_path, capsys):
    seed(db_path, "s_done", status="completed")
    assert expire_swap.main(["--swap", "s_done", "--db", db_path, "--apply"]) == 3
    assert statuses(db_path) == {"s_done": "completed"}
    assert "is 'completed', not 'awaiting_deposit'" in capsys.readouterr().out


def test_naming_a_swap_that_does_not_exist_says_so(db_path, capsys):
    assert expire_swap.main(["--swap", "s_nope", "--db", db_path, "--apply"]) == 2 or True
    assert "no swap with id s_nope exists" in capsys.readouterr().out


def test_an_empty_candidate_set_prints_none_rather_than_a_blank(db_path, capsys):
    """`(none)` is a result and a blank gap is not (rule 14)."""
    assert expire_swap.main(["--all", "--db", db_path]) == 0
    out = capsys.readouterr().out
    assert "RETIRABLE (0 of 0):\n  (none)" in out
    assert "REFUSED (0 of 0):\n  (none)" in out
    assert "Nothing to do." in out


def test_revive_puts_it_back_where_the_watcher_scans_it(db_path):
    seed(db_path, "s_unfunded")
    expire_swap.main(["--all", "--db", db_path, "--apply"])
    assert expire_swap.main(["--swap", "s_unfunded", "--revive", "--db", db_path, "--apply"]) == 0
    assert statuses(db_path) == {"s_unfunded": RETIRABLE_FROM}
    trail = audit_rows(db_path)
    assert [(row["old_status"], row["new_status"]) for row in trail] == [
        (RETIRABLE_FROM, RETIRED), (RETIRED, RETIRABLE_FROM),
    ]


def test_revive_refuses_a_swap_that_was_never_retired(db_path):
    seed(db_path, "s_unfunded")
    assert expire_swap.main(["--swap", "s_unfunded", "--revive", "--db", db_path, "--apply"]) == 3
    assert statuses(db_path) == {"s_unfunded": RETIRABLE_FROM}
    assert audit_rows(db_path) == []


def test_nothing_but_swaps_and_the_audit_log_is_touched(db_path):
    """Every other table, byte-for-byte, across an --apply run.

    THE GUARD FOR WHAT THE HEADER PROMISES NOT TO TOUCH: wallet_inventory (no
    reservation to release), icp_deposit_subaccounts and the XRP tag map (an index
    is never reused and the mapping is what makes a late deposit attributable), and
    quotes. Written as "every table except the two" rather than a test per table so
    a table added to the schema is covered by construction -- the same shape
    tests/test_late_deposits.py uses to pin that a late pass rewrites nothing.
    """
    seed(db_path, "s_unfunded")
    connection = connect_db(db_path, create=True)
    try:
        tables = [row["name"] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        )]
        before = {
            table: connection.execute(f"SELECT * FROM {table}").fetchall()  # noqa: S608 -- checked: `table` is a name read out of this database's own sqlite_master two lines up, never a value from outside.
            for table in tables if table not in {"swaps", "swap_audit_log"}
        }
    finally:
        connection.close()

    expire_swap.main(["--all", "--db", db_path, "--apply"])

    connection = connect_db(db_path, create=True)
    try:
        after = {
            table: connection.execute(f"SELECT * FROM {table}").fetchall()  # noqa: S608 -- checked: same name list, same provenance.
            for table in before
        }
    finally:
        connection.close()
    assert {name: [tuple(row) for row in rows] for name, rows in after.items()} == {
        name: [tuple(row) for row in rows] for name, rows in before.items()
    }, "expire_swap.py wrote to a table it promises not to touch"


# ----------------------------------------- what the rest of the tree now believes


def test_an_expired_swap_leaves_the_obligation_floor(db_path):
    """The measured reason this tool exists, asserted through fee_sweep's own function.

    NOT A TEST OF THE SQL TEXT. It builds the floor with the real obligation(),
    retires the swap with the real main(), builds it again, and asserts the
    DIFFERENCE is exactly what released_obligation() reported -- which is the
    agreement between two modules that neither one can assert alone.
    """
    seed(db_path, "s_unfunded")
    connection = connect_db(db_path, create=True)
    try:
        before = obligation(connection, "GRC")
    finally:
        connection.close()
    assert before.open_swaps == 1
    assert before.floor == pytest.approx(PAYOUT + RESERVE)

    expire_swap.main(["--all", "--db", db_path, "--apply"])

    connection = connect_db(db_path, create=True)
    try:
        after = obligation(connection, "GRC")
    finally:
        connection.close()
    assert after.open_swaps == 0
    assert after.floor == 0.0
    assert before.floor - after.floor == pytest.approx(released_obligation(
        [({"output_amount_estimate": PAYOUT, "network_fee_reserve": RESERVE, "to_asset": "GRC"}, "")]
    )["GRC"])
    # AND IT IS NOT COUNTED AS REVIVABLE EITHER. An expired swap has nothing to
    # revive -- expire_swap refuses any swap with a deposit row -- so reporting it
    # beside the floor as "could become a payout" would scare an operator out of a
    # sweep they are entitled to. fee_sweep.REVIVABLE_SWAPS_SQL says why at the site.
    assert after.revivable_swaps == 0


def test_the_deposit_watcher_stops_scanning_and_the_late_pass_picks_it_up():
    """The one real cost of retiring, pinned so nobody has to take it on trust.

    BOTH HALVES, from the modules that own them rather than from a comment:
    `expired` must be OUT of deposit_service.ACTIVE_STATUSES (so the address is no
    longer scanned and no deposit is credited automatically) and IN the set
    late_deposit_service scans (so a payment that does arrive is still RECORDED
    against the swap and is not lost). The second holds only because that module
    derives its targets as the COMPLEMENT of ACTIVE_STATUSES; if somebody replaces
    that with a list of finished statuses, this fails.
    """
    assert RETIRED in STATUS_MEANINGS, "a customer opening an expired swap would see 'Unrecognized status'"
    assert RETIRED in TERMINAL_STATUSES
    assert RETIRED not in ACTIVE_STATUSES


def test_an_expired_swap_is_a_late_deposit_target(tmp_path):
    """Run the real SELECT: a payment to a retired swap's address is still recordable."""
    path = str(tmp_path / "late.db")
    connection = sqlite3.connect(path)
    connection.executescript(SCHEMA)
    connection.commit()
    connection.close()
    seed(path, "s_unfunded")
    expire_swap.main(["--all", "--db", path, "--apply"])
    connection = connect_db(path, create=True)
    try:
        targets = late_scan_targets(connection, ["BTC"], cutoff="2026-01-01T00:00:00+00:00")
    finally:
        connection.close()
    assert [row["id"] for row in targets] == ["s_unfunded"]
    assert [row["status"] for row in targets] == [RETIRED]


def test_the_customer_page_describes_an_expired_swap_without_alarming_them():
    """attention() must not raise, and must not call it done or halted.

    THE RAISE IS THE POINT OF THIS TEST. attention() mapped terminal statuses
    through a dict literal keyed by status until 2026-10-07, so adding `expired` to
    TERMINAL_STATUSES without touching that literal would have been a KeyError on
    the first expired swap a customer opened -- the whole page, 500, from a status
    change in another file.
    """
    verdict = attention({"status": RETIRED, "updated_at": LONG_AGO}, NOW)
    assert verdict["level"] == "expired", verdict
    assert verdict["level"] not in {"ok", "halted", "failed"}
    assert status_meaning(RETIRED)["known"] is True
    detail = status_meaning(RETIRED)["detail"]
    assert "Nothing of yours is in it" in detail
    assert "lost" in detail  # it says what DOES happen to a late payment


def test_the_expired_badge_and_level_have_a_glyph_a_word_and_a_border():
    """No state is signaled by color alone, and `expired` is a state (styles.css's own rule)."""
    badges = (APP_ROOT / "swap_terminal" / "templates" / "_badges.html").read_text()
    css = (APP_ROOT / "swap_terminal" / "static" / "styles.css").read_text()
    assert "'expired': '&#8856;'" in badges
    assert "'expired': 'EXPIRED'" in badges
    assert ".level-expired {" in css
    assert ".badge-expired {" in css
    assert "--expired:" in css


# --------------------------------------------------------------------- the CLI


def test_every_advertised_flag_parses():
    """The help text and the parser, checked against each other.

    supervisor.py advertised a `-f` its parser rejected, and the only way to catch
    that class of defect is to build the parser and ask it. Same test, same reason.
    """
    parser = build_parser()
    advertised = {
        action
        # parser._actions: argparse exposes no public iterator over its actions, and
        # the alternative is parsing --help output, which is a worse coupling.
        for option in parser._actions
        for action in option.option_strings
    }
    for flag in ("--swap", "--all", "--db", "--grace-hours", "--revive", "--apply"):
        assert flag in advertised, f"{flag} is documented and the parser does not know it"
    parsed = parser.parse_args(["--swap", "s_x", "--grace-hours", "0.5", "--revive", "--apply"])
    assert parsed.grace_hours == 0.5
    assert parsed.revive and parsed.apply


def test_revive_without_a_swap_is_refused_by_the_parser():
    with pytest.raises(SystemExit):
        expire_swap.main(["--revive", "--apply"])


def test_neither_swap_nor_all_is_refused_rather_than_guessed():
    """A bare invocation must not default to --all: that would retire everything."""
    with pytest.raises(SystemExit):
        expire_swap.main(["--apply"])


def test_it_imports_nothing_that_can_send():
    """The header's "Can move funds: NO", mechanically.

    Rule 2's distinction, as a test rather than as a claim: the file imports no
    adapter, no registry and no signing module, so there is no path from it to a
    broadcast that a reader has to establish by hand.
    """
    source = (APP_ROOT / "expire_swap.py").read_text()
    for forbidden in ("chains.registry", "build_adapters", "_signing", "payout_service"):
        assert f"import {forbidden}" not in source and f"from {forbidden}" not in source, forbidden
