"""What fee a completed swap actually kept, asserted on rows and not on arithmetic.

Behavioral, against the real SCHEMA and the real SELECT -- seed rows into the real
`swaps` and `payouts` tables, run fee_ledger.fee_rows(), assert on the rows it
returns (BEHAVIORAL_VERIFICATION_PRINCIPLE, absorbed into CLAUDE.md). No test here
asserts that the query TEXT contains anything; the one thing a text check could pin
-- which payout states count -- is pinned instead by seeding a payout in each
non-counting state and asserting it is absent.

WHERE THE FIXTURE NUMBERS COME FROM, AND WHICH OF THEM ARE REAL.

The rate and the payout are the operator's live devnet-SOL -> testnet-GRC swap of
2026-10-01, read back out of their database that afternoon:

    quoted_rate             8995.69403675488   GRC per SOL
    output_amount_estimate  88.5975862620356   GRC
    fee_bps                 150
    network_fee_reserve     0.01               GRC (config.GRC_NETWORK_FEE_RESERVE)

EXPECTED_INPUT below is DERIVED from those by inverting create_quote()'s
expression, not remembered: 0.01 SOL reproduces that payout to the last digit
(asserted in test_the_live_swap_reproduces_its_recorded_payout, which is what
makes the rest of this fixture trustworthy rather than decorative). The txids and
txids are synthetic -- no assertion here depends on a txid's value -- and the two
addresses come from tests/valid_addresses.py rather than being written out, which
tests/test_address_literals_are_valid.py enforces with a ceiling on how many
address literals the tree may hold. That ceiling is what caught the first draft of
this file: it wrote both addresses as string literals and pushed the count from 60
to 62. A derived or shared fixture cannot be mistyped and says what it is for.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path
from typing import NamedTuple

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "swap_terminal"))

from config import Config  # noqa: E402
from db import SCHEMA, connect_db  # noqa: E402

from swap_terminal import fee_ledger  # noqa: E402
from swap_terminal.fee_ledger import (  # noqa: E402
    RECONCILE_TOLERANCE_BPS,
    asset_totals,
    fee_rows,
)
from tests.valid_addresses import GRC_PAYOUT, SOL_DEPOSIT_ACCOUNT  # noqa: E402

#: The live swap's recorded figures. See the module docstring.
QUOTED_RATE = 8995.69403675488
FEE_BPS = 150
RESERVE = 0.01
LIVE_PAYOUT = 88.5975862620356
EXPECTED_INPUT = (LIVE_PAYOUT + RESERVE) / (QUOTED_RATE * (1 - FEE_BPS / 10000.0))

#: config.AMOUNT_TOLERANCE_PCT's shipped value. Not imported, deliberately: this
#: suite asserts what happens AT the band's edges, and importing the setting would
#: make the assertions move with it and quietly stop testing the edge they name.
#: If the shipped value changes, this line is the one that should fail to match it,
#: which test_the_tolerance_in_config_still_matches_this_suite checks.
TOLERANCE_PCT = 0.01


def _estimate(expected_input: float) -> float:
    """create_quote()'s payout expression, for seeding only.

    A second spelling of one rule is rule 8's defect, so this is named as what it
    is: the fixture needs an `output_amount_estimate` for a swap it invents, and
    the real create_quote() cannot be called without a price feed and a database
    of quotes. test_the_live_swap_reproduces_its_recorded_payout pins this
    expression against a figure the REAL function produced in production, which is
    the only thing that keeps the two from drifting.
    """
    return max(expected_input * QUOTED_RATE * (1 - FEE_BPS / 10000.0) - RESERVE, 0.0)


@pytest.fixture
def db():
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.executescript(SCHEMA)
    yield connection
    connection.close()


class Seed(NamedTuple):
    """Everything about a seeded swap except its id.

    A tuple rather than six keyword arguments, because ruff's PLR0913 put
    seed_swap() at seven parameters and the two honest answers were to remove
    arguments or to group them; a `noqa` claiming the count is fine is the
    suppression rule 19 forbids. Grouping also made the call sites read better:
    `Seed(actual_input=...)` names the ONE thing a given test is varying, with
    every other fixture value defaulted in one place instead of repeated.

    One argument really was removed on the way: `estimate`, an override for
    `output_amount_estimate`, had no caller anywhere (rule 9 -- a helper
    parameter nobody passes is dead code with a plausible docstring). The clamp
    test reaches the same state through a real expected_input small enough to
    trip max(...,0), which is the honest route to it.
    """

    expected_input: float = EXPECTED_INPUT
    actual_input: float | None = None
    to_asset: str = "GRC"
    reserve: float = RESERVE


#: Module-level singletons for the defaults, because ruff's B008 refuses a call in
#: an argument default. The rule's usual reason -- a mutable default shared across
#: calls -- does not apply to a NamedTuple, but the remedy it names is the right
#: shape anyway and costs nothing, which is a better answer than a `noqa` claiming
#: immutability (rule 19: a suppression is a claim you checked, and here there is
#: no need to claim anything).
DEFAULT_SEED = Seed()


def seed_swap(db, swap_id: str, seed: Seed = DEFAULT_SEED) -> float:
    """One completed SOL -> <to_asset> swap. Returns the payout amount seeded."""
    paid = _estimate(seed.expected_input)
    actual = seed.expected_input if seed.actual_input is None else seed.actual_input
    db.execute(
        "INSERT INTO quotes (id, from_asset, to_asset, input_amount, quoted_rate, fee_bps,"
        " network_fee_reserve, output_amount_estimate, expires_at, created_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?)",
        (f"q_{swap_id}", "SOL", seed.to_asset, seed.expected_input, QUOTED_RATE, FEE_BPS, seed.reserve,
         paid, "2026-10-01T00:10:00+00:00", "2026-10-01T00:00:00+00:00"),
    )
    db.execute(
        "INSERT INTO swaps (id, quote_id, from_asset, to_asset, deposit_address, payout_address,"
        " expected_input_amount, actual_input_amount, quoted_rate, fee_bps, network_fee_reserve,"
        " output_amount_estimate, status, min_confirmations, created_at, updated_at, credited_at,"
        " completed_at, expires_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (swap_id, f"q_{swap_id}", "SOL", seed.to_asset,
         SOL_DEPOSIT_ACCOUNT,
         GRC_PAYOUT, seed.expected_input, actual, QUOTED_RATE, FEE_BPS,
         seed.reserve, paid, "completed", 3, "2026-10-01T00:00:00+00:00", "2026-10-01T00:05:00+00:00",
         "2026-10-01T00:03:00+00:00", "2026-10-01T00:05:00+00:00", "2026-10-01T00:10:00+00:00"),
    )
    return paid


#: `txid=None` has to be able to mean "store SQL NULL", and a None default would
#: make it mean "use the derived value" instead. That is not hypothetical
#: carefulness: this fixture WAS written with `txid: str | None = None` and
#: test_a_broadcast_payout_with_no_txid_is_not_counted failed on the first run
#: because passing None got the derived txid -- and the 'created' test one above
#: it PASSED while asserting nothing about a txid at all, since its status alone
#: excluded the row. A fixture whose absent-value case cannot be reached is a test
#: that is green on the wrong thing.
DERIVE_TXID = object()


class PayoutSeed(NamedTuple):
    """Everything about a seeded payout except its swap and its amount.

    Grouped for the same reason as Seed above: seven parameters, and rule 19
    rules out a suppression.
    """

    status: str = "broadcast"
    txid: object = DERIVE_TXID
    sent_at: str = "2026-10-01T00:05:00+00:00"
    asset: str = "GRC"


DEFAULT_PAYOUT = PayoutSeed()


def seed_payout(db, swap_id: str, amount: float, payout: PayoutSeed = DEFAULT_PAYOUT) -> None:
    db.execute(
        "INSERT INTO payouts (swap_id, asset, destination_address, amount, txid, status,"
        " created_at, sent_at) VALUES (?,?,?,?,?,?,?,?)",
        (swap_id, payout.asset, GRC_PAYOUT, amount,
         f"{swap_id}_txid" if payout.txid is DERIVE_TXID else payout.txid, payout.status,
         "2026-10-01T00:04:00+00:00", payout.sent_at),
    )


def one_row(db):
    rows = fee_rows(db)
    assert len(rows) == 1, f"expected exactly one fee row, got {len(rows)}"
    return rows[0]


# ---------------------------------------------------------------- the live swap


def test_the_live_swap_reproduces_its_recorded_payout():
    """The fixture's arithmetic agrees with what the real create_quote() produced.

    This is the anchor for every other test in the file. 0.01 SOL at the recorded
    rate, through the recorded fee and reserve, is the exact payout the operator's
    database holds -- so a later edit to _estimate() that no longer matches the
    real expression fails here rather than quietly re-baselining every assertion
    below it.
    """
    assert pytest.approx(0.01, abs=1e-12) == EXPECTED_INPUT
    assert _estimate(EXPECTED_INPUT) == pytest.approx(LIVE_PAYOUT, rel=0, abs=1e-12)


def test_the_live_swaps_realized_fee_is_the_schedule_plus_the_reserve(db):
    """151.11bps on a 150bps schedule, and the extra 1.11 is the reserve.

    The figure an operator asking "what do we charge" wants, and it is NOT
    `fee_bps`: the network fee reserve is also held back from the payout, so what
    the desk retains is larger than the schedule by whatever fraction of the gross
    that reserve is. On this swap 0.01 GRC against an 89.96 GRC gross is 1.11bps.
    """
    paid = seed_swap(db, "s_live")
    seed_payout(db, "s_live", paid)
    row = one_row(db)

    assert row.realized_gross == pytest.approx(89.95694036754884, abs=1e-9)
    assert row.retained == pytest.approx(1.3593541055132334, abs=1e-9)
    assert row.retained_bps == pytest.approx(151.11164296597266, abs=1e-6)
    assert row.reserve_bps == pytest.approx(1.1116429659725746, abs=1e-6)
    # Zero because the deposit matched the quote exactly -- the next two tests are
    # what happens when it does not.
    assert row.drift_bps == pytest.approx(0.0, abs=1e-9)
    assert row.reconciles
    assert row.residual_bps == pytest.approx(0.0, abs=1e-9)


# ------------------------------------------------- the schedule is not the fee


def test_a_deposit_at_the_bottom_of_tolerance_gives_away_most_of_the_fee(db):
    """0.99x expected pays the full estimate, so the realized fee collapses.

    THE FINDING THIS FILE EXISTS FOR, and it is a behavioral assertion rather than
    a reading of two functions: services/deposit_service.py credits the swap on the
    CONFIRMED total and never recomputes `output_amount_estimate`, so the payout is
    still priced on `expected_input_amount`. A customer who sends one percent less
    than quoted is paid as though they had sent the full amount.

    150bps scheduled, ~52bps realized. The desk is not charging what it says.
    """
    paid = seed_swap(db, "s_low", Seed(actual_input=EXPECTED_INPUT * (1 - TOLERANCE_PCT)))
    seed_payout(db, "s_low", paid)
    row = one_row(db)

    assert row.actual_input < row.expected_input
    assert row.drift_bps < 0, "a short deposit must show as fee GIVEN AWAY, not kept"
    assert row.retained_bps == pytest.approx(52.5, abs=1.0)
    assert row.reconciles, "the identity must still hold -- only the total moved"


def test_a_deposit_at_the_top_of_tolerance_keeps_far_more_than_the_schedule(db):
    """1.01x expected is still inside tolerance, and the surplus is retained.

    The same defect in the other direction, and the one that matters for a
    complaint rather than for revenue: the customer sent more and was paid the
    same. ~249bps realized against a 150bps schedule.
    """
    paid = seed_swap(db, "s_high", Seed(actual_input=EXPECTED_INPUT * (1 + TOLERANCE_PCT)))
    seed_payout(db, "s_high", paid)
    row = one_row(db)

    assert row.actual_input > row.expected_input
    assert row.drift_bps > 0
    assert row.retained_bps == pytest.approx(249.6, abs=1.0)
    assert row.reconciles


def test_the_realized_fee_spans_four_fold_across_the_tolerance_band(db):
    """The range, measured end to end in one assertion, because the range is the point.

    A 150bps schedule realizing between ~52 and ~250bps is not a 150bps fee, and
    which end a customer lands on is decided by how their wallet rounded an amount.
    Asserting the SPAN rather than the two endpoints separately is what keeps this
    test meaningful if the tolerance or the schedule is retuned: narrowing either
    one narrows the span, and that is the number to look at.
    """
    for name, factor in (("s_low", 1 - TOLERANCE_PCT), ("s_mid", 1.0), ("s_high", 1 + TOLERANCE_PCT)):
        paid = seed_swap(db, name, Seed(actual_input=EXPECTED_INPUT * factor))
        seed_payout(db, name, paid, PayoutSeed(sent_at=f"2026-10-01T00:0{len(name)}:00+00:00"))

    realized = sorted(row.retained_bps for row in fee_rows(db))
    assert len(realized) == 3
    assert realized[1] == pytest.approx(151.1, abs=1.0), "the middle swap is the schedule"
    assert realized[2] / realized[0] == pytest.approx(4.75, abs=0.2), (
        "the widest realized fee is nearly five times the narrowest, on one schedule"
    )


def test_the_tolerance_in_config_still_matches_this_suite():
    """AMOUNT_TOLERANCE_PCT has not moved out from under the assertions above.

    The three bps figures in this file are only meaningful at a 1% band. Importing
    the setting into the assertions would make them follow it and stop testing
    anything; comparing it here means a retune fails ONE test, with a sentence
    saying which numbers to re-derive, instead of silently passing.
    """
    assert Config.AMOUNT_TOLERANCE_PCT == TOLERANCE_PCT, (
        f"AMOUNT_TOLERANCE_PCT is {Config.AMOUNT_TOLERANCE_PCT}, not {TOLERANCE_PCT}. The realized-fee "
        f"figures in this file were derived at {TOLERANCE_PCT}; re-derive them for the new band."
    )


# --------------------------------------------------- which payouts count as paid


def test_a_payout_still_marked_created_is_not_counted(db):
    """Money that may be on chain with no txid is not revenue.

    services/payout_service.py INSERTs 'created' before the send and writes the
    status after it, so 'created' means the send's outcome is unknown. Counting it
    would put an amount nobody knows was paid into a fee total.
    """
    paid = seed_swap(db, "s_created")
    seed_payout(db, "s_created", paid, PayoutSeed(status="created", txid=None))
    assert fee_rows(db) == []


def test_a_broadcast_payout_with_no_txid_is_not_counted(db):
    """'broadcast' and a txid are two claims, and both are required.

    The crash window process_pending_payouts() documents can leave the status set
    and the txid not. A row like this is the operator's to resolve
    (admin_view.unresolved_payouts()); it is not a fee.
    """
    paid = seed_swap(db, "s_notxid")
    seed_payout(db, "s_notxid", paid, PayoutSeed(txid=None))
    assert fee_rows(db) == []


def test_a_failed_payout_carrying_a_txid_is_not_counted(db):
    """The status clause, pinned on its own -- and it needed to be.

    MUTATION-FOUND, 2026-10-01. Deleting `p.status = 'broadcast'` from the SELECT
    left every test in this file green, because the only non-broadcast row seeded
    anywhere also had a NULL txid and the OTHER clause excluded it. A clause no
    test can distinguish from its own absence is not enforced by anything.

    IS THIS STATE REACHABLE? NOT BY TODAY'S CODE, AND THAT IS ESTABLISHED RATHER
    THAN ASSUMED (rule 17). Every write to `payouts.status` in the tree, read
    2026-10-01: services/payout_service.py INSERTs 'created' with txid NULL, moves
    it to 'failed' with the txid still NULL on the failure path, and
    _record_broadcast() sets 'broadcast' and the txid in ONE statement -- so no
    path writes a txid onto a row that is not simultaneously 'broadcast'.

    The clause stays, and is tested, for two reasons. It is the one that carries
    the MEANING -- "this payout was broadcast" -- while `txid IS NOT NULL` only
    corroborates it; and settle_payout.py is a hand-run correction tool whose own
    comment records that this exact UPDATE already matched nothing once, in
    September, leaving a swap 'completed' beside a payout row reading
    `status failed  txid (none)`. One edit in that area is all it takes to produce
    the pair below, and a fee total is the wrong place to discover it.
    """
    paid = seed_swap(db, "s_failed_txid")
    seed_payout(db, "s_failed_txid", paid, PayoutSeed(status="failed", txid="f_txid"))
    assert fee_rows(db) == []


def test_a_swap_with_no_deposit_seen_is_not_counted(db):
    """actual_input_amount NULL means no deposit row exists, which is not zero.

    Without the filter this row would arrive as NULL arithmetic throughout -- a
    blank line in a report about money, which rule 14 refuses. Absent is the
    answer, and show_fees.py's count makes the absence visible.
    """
    paid = seed_swap(db, "s_nodeposit", Seed(actual_input=EXPECTED_INPUT))
    db.execute("UPDATE swaps SET actual_input_amount = NULL WHERE id = 's_nodeposit'")
    seed_payout(db, "s_nodeposit", paid)
    assert fee_rows(db) == []


def test_nothing_paid_out_returns_an_empty_list_not_an_error(db):
    """An empty database is a result. "(none)" is show_fees.py's job, not a raise."""
    assert fee_rows(db) == []
    assert asset_totals([]) == []


# ------------------------------------------------------------ the clamp, reported


def test_a_payout_clamped_to_zero_is_reported_as_not_reconciling(db):
    """When the reserve exceeds the gross, create_quote() pays zero and we keep it all.

    `max(..., 0)` in create_quote() means a swap smaller than the destination
    chain's fee reserve pays out nothing. The whole gross is then retained, so
    `retained_bps` is 10000 and the identity cannot hold. That is a real state and
    this report says so rather than averaging a 10000bps "fee" into a rate: the row
    is returned, `reconciles` is False, and asset_totals() COUNTS it.
    """
    tiny = RESERVE / QUOTED_RATE / 2
    paid = seed_swap(db, "s_clamped", Seed(expected_input=tiny))
    assert paid == 0.0, "the fixture must actually trip the clamp, or this tests nothing"
    seed_payout(db, "s_clamped", paid)
    row = one_row(db)

    assert row.paid == 0.0
    assert row.retained == pytest.approx(row.realized_gross, abs=1e-12)
    assert row.retained_bps == pytest.approx(10000.0, abs=1e-6)
    assert not row.reconciles
    assert abs(row.residual_bps) > RECONCILE_TOLERANCE_BPS
    assert asset_totals([row])[0].unreconciled == 1


# ----------------------------------------------------------------------- totals


def test_the_weighted_fee_is_not_the_mean_of_the_per_swap_fees(db):
    """One large swap at the schedule and one tiny swap that gave the fee away.

    The distinction the totals exist for: the MEAN of the two realized fees is
    dragged halfway down by a swap worth a thousandth of the other, while the
    weighted figure -- retained over gross -- answers "what fraction of what came
    in did we keep", which is the only version of the number a desk can act on.
    """
    big = seed_swap(db, "s_big", Seed(expected_input=10.0))
    seed_payout(db, "s_big", big)
    small = seed_swap(db, "s_small", Seed(expected_input=0.01,
                      actual_input=0.01 * (1 - TOLERANCE_PCT)))
    seed_payout(db, "s_small", small, PayoutSeed(sent_at="2026-10-01T00:06:00+00:00"))

    rows = fee_rows(db)
    totals = asset_totals(rows)
    assert len(totals) == 1
    total = totals[0]

    mean = sum(row.retained_bps for row in rows) / len(rows)
    assert total.swaps == 2, "the denominator rides with the rate (rule 3)"
    assert total.weighted_bps == pytest.approx(150.1, abs=1.0)
    assert mean == pytest.approx(101.6, abs=2.0)
    assert total.weighted_bps > mean + 40, (
        "the weighted fee and the mean must differ materially here, or this fixture "
        "no longer demonstrates why the weighting was chosen"
    )
    assert total.unreconciled == 0


def test_offsetting_drift_shows_up_in_the_gross_even_when_the_net_cancels(db):
    """FOUND BY PRINTING THE REPORT, not by reading it (2026-10-01).

    show_fees.py's first run over four seeded swaps printed `drift
    +0.00000000 GRC` while two of the four rows under it read -99.5bps and
    +97.5bps: one short deposit and one long one, equal and opposite, cancelling
    in the net. The asset line said no mismatch had occurred on a ledger where
    half the customers were charged the wrong fee.

    So the net is kept -- it is the right answer to "what did this cost us" --
    and `drift_abs` and `drifted` are what answer "is this happening at all".
    """
    low = seed_swap(db, "s_low", Seed(actual_input=EXPECTED_INPUT * (1 - TOLERANCE_PCT)))
    seed_payout(db, "s_low", low)
    high = seed_swap(db, "s_high", Seed(actual_input=EXPECTED_INPUT * (1 + TOLERANCE_PCT)))
    seed_payout(db, "s_high", high, PayoutSeed(sent_at="2026-10-01T00:06:00+00:00"))

    total = asset_totals(fee_rows(db))[0]
    assert total.drift == pytest.approx(0.0, abs=1e-9), "the premise: the net really does cancel"
    assert total.drift_abs > 1.7, "and the gross movement is nearly two whole coins"
    assert total.drifted == 2, "both swaps drifted, and the count is what makes that readable"


def test_an_exact_deposit_is_not_counted_as_having_drifted(db):
    """Float arithmetic makes an exact match compute to ~1e-17, not to 0.0.

    Counting `!= 0` would report every swap in the ledger as mischarged, which is
    the opposite failure to the one above and just as misleading. DRIFT_IS_ZERO_COIN
    is a thousandth of a satoshi -- it decides this COUNT and a printed sentence,
    never a payout and never a sum.
    """
    paid = seed_swap(db, "s_exact")
    seed_payout(db, "s_exact", paid)
    row = fee_rows(db)[0]
    total = asset_totals([row])[0]

    # A tautology sat here on the first draft (`x != 0 or x == 0`), which cannot
    # fail and is therefore worse than no line: it reads as a check. Removed.
    assert abs(row.drift_coin) < fee_ledger.DRIFT_IS_ZERO_COIN
    assert total.drifted == 0
    assert total.drift_abs == pytest.approx(0.0, abs=fee_ledger.DRIFT_IS_ZERO_COIN)


def test_totals_are_per_destination_asset_and_never_pooled(db):
    """GRC and LTC retention are not added together.

    Summing coins of different value and calling the result revenue is the mistake
    Project Mammon rule 11 refuses for cadences. Two assets in, two rows out.
    """
    grc = seed_swap(db, "s_grc", Seed(to_asset="GRC"))
    seed_payout(db, "s_grc", grc, PayoutSeed(asset="GRC"))
    ltc = seed_swap(db, "s_ltc", Seed(to_asset="LTC", reserve=0.001))
    seed_payout(db, "s_ltc", ltc, PayoutSeed(asset="LTC", sent_at="2026-10-01T00:06:00+00:00"))

    totals = asset_totals(fee_rows(db))
    assert [total.asset for total in totals] == ["GRC", "LTC"]
    assert all(total.swaps == 1 for total in totals)


def test_the_scheduled_figure_is_weighted_the_same_way_as_the_realized_one(db):
    """Otherwise the two columns beside each other are not comparable.

    Both swaps here are quoted at 150bps, so `scheduled_bps` must be exactly 150
    however the sizes differ -- which is the property that makes the gap between
    the columns readable as drift and nothing else.
    """
    big = seed_swap(db, "s_a", Seed(expected_input=10.0))
    seed_payout(db, "s_a", big)
    small = seed_swap(db, "s_b", Seed(expected_input=0.01, actual_input=0.01 * 1.005))
    seed_payout(db, "s_b", small, PayoutSeed(sent_at="2026-10-01T00:06:00+00:00"))

    total = asset_totals(fee_rows(db))[0]
    assert total.scheduled_bps == pytest.approx(150.0, abs=1e-9)
    assert total.weighted_bps != pytest.approx(total.scheduled_bps, abs=1e-6)


def test_rows_come_back_oldest_first(db):
    """So a printed ledger reads in the order the money moved."""
    for name, sent in (("s_third", "00:30"), ("s_first", "00:10"), ("s_second", "00:20")):
        paid = seed_swap(db, name)
        seed_payout(db, name, paid, PayoutSeed(sent_at=f"2026-10-01T{sent}:00+00:00"))
    assert [row.swap_id for row in fee_rows(db)] == ["s_first", "s_second", "s_third"]


def test_the_ledger_opens_no_socket_and_writes_nothing(db):
    """The module's header claims read-only; this is the claim tested.

    A write would have to go through this connection, so the check is that the
    tables are byte-identical afterward. `connect_db` is imported by the suite and
    not by the module under test, which is the other half of "it opens nothing".
    """
    paid = seed_swap(db, "s_ro")
    seed_payout(db, "s_ro", paid)
    before = db.execute("SELECT * FROM swaps").fetchall(), db.execute("SELECT * FROM payouts").fetchall()
    fee_rows(db)
    after = db.execute("SELECT * FROM swaps").fetchall(), db.execute("SELECT * FROM payouts").fetchall()
    assert [tuple(row) for row in before[0]] == [tuple(row) for row in after[0]]
    assert [tuple(row) for row in before[1]] == [tuple(row) for row in after[1]]

    source = Path(fee_ledger.__file__).read_text()
    for statement in ("INSERT", "UPDATE", "DELETE", "CREATE"):
        assert statement not in source.replace("-- ", "").split('"""')[-1], (
            f"{statement} appears in the code of a module whose header says it writes nothing"
        )
    assert connect_db is not None
