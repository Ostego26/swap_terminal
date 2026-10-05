"""A `wallet_inventory` reservation the payout rows do not justify, reported and repaired.

Role: test (seeded temp database; opens no socket, builds no adapter)
Reads: repair_inventory_reservations.py, db.py
Writes: a temp database under pytest's tmp_path ONLY
Can move funds: no
Mainnet-safe: yes

=============================================================================
THE TWO DEFECTS, MEASURED ON THE OPERATOR'S LIVE HOST 2026-10-04
=============================================================================

Read read-only out of their `wallet_inventory` and `payouts`:

    asset  hot_confirmed            hot_reserved          hot_available            updated_at
    BTC    10.0012                  0.0                   10.0012                  2026-10-04T17:19:17
    GRC    3780.08454497            9882.372957331736     -6102.288412361736       2026-10-04T17:19:17
    LTC    100.0                    0.0                   100.0                    2026-10-04T17:19:17
    SOL    28.087120892             0.0                   28.087120892             2026-10-04T17:19:17
    XRP    -59.231412662192405      0.0                   -59.231412662192405      2026-10-04T14:31:20

    GRC payouts:  broadcast 10 rows, 10343.81698888 total
                  failed     5 rows,  9993.425862093694 total

DEFECT 1 -- XRP's hot_confirmed IS THE NEGATED SUM OF ITS OWN PAYOUTS, EXACTLY:

    payouts.id=23  3.3155893288590605   (the GRC->XRP swap)
    payouts.id=24  55.91582333333334    (the BTC->XRP swap)
    sum            59.231412662192405
    hot_confirmed -59.231412662192405   <- exact negation

test_the_measured_XRP_row_is_reproduced_BY_THE_REAL_FUNCTIONS below asserts that
negation to the last floating-point digit, by calling the real
services/payout_service.reserve_inventory() and release_inventory_after_send()
in the order the payout worker calls them -- which is how the code path was
established rather than read: reserve_inventory()'s INSERT branch is the only
thing that can create an XRP row, because the other writer,
refresh_wallet_inventory(), calls chains/xrp.XRPAdapter.get_balance() first and
that method raises by design.

DEFECT 2 -- GRC's reservation exceeds its balance, giving hot_available
-6102.288412361736 on a wallet holding 3780.08454497 GRC. AND THE RESERVED
FIGURE IS NOT THE FAILED TOTAL, which is why no test here asserts that it is:

    failed GRC total    9993.425862093694
    hot_reserved        9882.372957331736
    difference            111.05290476195842   reserved is SHORT by this

So "every failure leaked" is false and is not what is seeded. 111.05290476195842
is within about 46 ulps of 2 x 55.52645238097888, twice payout id=3's amount --
the one swap settle_payout.py's own source comment records having had its four
writes applied TWICE before 2026-09-26, each pass performing a release. That is a
lead with a named mechanism and it does NOT close arithmetically: one double
release accounts for 55.526, not 111.053, and the rows that would settle it are
the operator's. The tool prints the per-row account for that reason, and
test_a_reservation_BELOW_what_the_rows_justify_is_refused_and_not_raised pins the
direction the tool takes when it meets that shape.

WHAT THE WRONG FIGURE COSTS, MEASURED THE SAME DAY AND NOT WHAT WAS EXPECTED.
services/payout_capacity.largest_fundable_payout() and
why_the_payout_cannot_be_funded() both call adapter.get_balance() and read NO
database column: seeded with hot_available=-6102.288412361736 they returned
byte-identical answers before and after hot_confirmed was zeroed, so a negative
hot_available CANNOT refuse a customer payout. The reader that does consume the
figure is swap_terminal/fee_sweep.obligation(), which sets
`floor = max(open_total, reserved)` -- seeded with the operator's row it returned
floor=9882.372957331736 against a 3780.08454497 GRC wallet with open_total=0.0,
which refuses every GRC fee sweep for as long as the figure stands.
test_the_repair_releases_the_fee_sweep_floor_that_the_leak_was_holding asserts
that, through the real obligation(), before and after.

=============================================================================
WHAT IS MEASURED AND WHAT IS SHAPED (rule 3's denominator, for fixtures)
=============================================================================

MEASURED, to the digit, and typed rather than computed: the five
`wallet_inventory` rows above, the two XRP payout amounts, the GRC failed total
and count, and the GRC broadcast total and count.

SHAPED: the individual GRC failed amounts. The operator's rows are known as a
COUNT and a TOTAL -- 5 rows, 9993.425862093694 -- not per row, so the five
amounts below are invented to sum to exactly that measured total. Any assertion
that depends on an individual GRC failed amount would be asserting on a fixture;
every assertion here depends only on the total, which is measured.

ADDRESSES COME FROM tests/valid_addresses.py AND ARE NEVER WRITTEN AS LITERALS.
tests/test_address_literals_are_valid.py is a clean gate at a ceiling with no
headroom and rule 19 forbids raising it.

=============================================================================
MUTATION-CHECKED, 2026-10-04
=============================================================================

Each test below names the production change that was made to break it and what
the failure looked like. The three the brief required are
test_only_a_CREATED_payout_justifies_a_reservation (the live-status set),
test_a_second_run_corrects_NOTHING_and_says_so (the idempotency guard) and
test_the_correction_row_holds_the_ORIGINAL_figures (the audit write).
"""

from __future__ import annotations

import sqlite3
from typing import NamedTuple

import pytest
from db import (
    INVENTORY_CORRECTIONS_SQL,
    INVENTORY_CORRECTIONS_TABLE,
    PAYOUT_LIVE_STATUSES,
    PAYOUT_RESERVED_STATUSES,
    SCHEMA,
    db_session,
    dict_factory,
)
from fee_sweep import obligation
from services.payout_capacity import largest_fundable_payout, why_the_payout_cannot_be_funded
from services.payout_service import release_inventory_after_send, reserve_inventory
from valid_addresses import GRC_PAYOUT, XRP_CUSTOMER_PAYOUT

import repair_inventory_reservations
from repair_inventory_reservations import DROP, OK, REFUSE, TRIM, apply_plan, plan_for_asset

# ---------------------------------------------------------------------------
# THE MEASURED FIGURES. Typed from the operator's host, never derived by calling
# the code under test -- a fixture computed the way the implementation computes
# passes when both are wrong, which this tree has already paid for once
# (tests/test_resolve_halted_swap.py records it).
# ---------------------------------------------------------------------------

WHEN = "2026-10-04T17:19:17"
XRP_WHEN = "2026-10-04T14:31:20"

GRC_CONFIRMED = 3780.08454497
GRC_RESERVED = 9882.372957331736
GRC_AVAILABLE = -6102.288412361736

#: The two XRP payouts, pre-quantization, as release_inventory_after_send() took them.
XRP_PAYOUT_23 = 3.3155893288590605
XRP_PAYOUT_24 = 55.91582333333334
XRP_NEGATED = -59.231412662192405

#: 5 rows, measured as a total. The per-row split is SHAPED to sum to it exactly.
GRC_FAILED_TOTAL = 9993.425862093694
GRC_FAILED_AMOUNTS = (1000.0, 2000.0, 3000.0, 2000.0, GRC_FAILED_TOTAL - 8000.0)

#: The difference the brief names, and the tool's whole reason for printing rows.
GRC_RESERVED_SHORTFALL = GRC_FAILED_TOTAL - GRC_RESERVED


class Seed(NamedTuple):
    """One payout to seed, and the swap and quote rows it needs underneath it.

    A NAMED TUPLE RATHER THAN SIX POSITIONAL ARGUMENTS, and the reason is the one
    rule 12 gives for PLR0913 read the way rule 19 asks: the helper took six
    arguments, five of which describe ONE payout, and the honest fix is to name the
    thing rather than to suppress the finding. It also removes the failure that
    shape invites in a fixture -- `("GRC", 1000.0, "failed")` and
    `("GRC", "failed", 1000.0)` are both accepted by a positional signature and
    only one of them seeds what the test says it seeds.

      swap_status  the SWAP's status, which is not the payout's. The failure path
                   sets both ('failed' / 'failed') and the live path sets 'paying'
                   / 'created', so they move together but are not one field, and a
                   fixture that conflated them could not seed the live case.
    """

    swap_id: str
    asset: str
    amount: float
    status: str
    txid: str | None = None
    swap_status: str = "failed"

    @property
    def destination(self) -> str:
        """A VALID address for the asset, from tests/valid_addresses.py, never a literal.

        tests/test_address_literals_are_valid.py is a clean gate at a ceiling with
        no headroom, and rule 19 forbids raising it. The address is never read by
        the tool under test -- it reports on `amount` and `status` -- but a seeded
        row that could not have existed is a fixture a later reader has to discount.
        """
        return XRP_CUSTOMER_PAYOUT if self.asset == "XRP" else GRC_PAYOUT


def _seed(conn, seed: Seed) -> None:
    """One quote, one swap and one payout, which is the minimum a payout row needs.

    Checked rather than assumed 2026-10-04, both foreign keys, each by seeing it
    fail first: inserting a payout row without its swap raises
    `sqlite3.IntegrityError: FOREIGN KEY constraint failed`, and so does inserting
    the swap without the quote its `quote_id` references. So a fixture that skipped
    either would not be a simpler fixture, it would be a broken one -- and the
    reason to write that down is that SQLite enforces foreign keys only when the
    pragma is on, which makes "did my seed need this?" a question about this SCHEMA
    rather than about SQLite.
    """
    conn.execute(
        "INSERT INTO quotes (id, from_asset, to_asset, input_amount, quoted_rate, fee_bps, "
        "network_fee_reserve, output_amount_estimate, expires_at, created_at) "
        "VALUES (?, 'BTC', ?, 1.0, 1.0, 150, 0.001, ?, ?, ?)",
        (f"q{seed.swap_id}", seed.asset, seed.amount, WHEN, WHEN),
    )
    conn.execute(
        "INSERT INTO swaps (id, quote_id, from_asset, to_asset, deposit_address, payout_address, "
        "expected_input_amount, actual_input_amount, quoted_rate, fee_bps, network_fee_reserve, "
        "output_amount_estimate, status, min_confirmations, created_at, updated_at, expires_at) "
        "VALUES (?, ?, 'BTC', ?, ?, ?, 1.0, 1.0, 1.0, 150, 0.001, ?, ?, 1, ?, ?, ?)",
        (seed.swap_id, f"q{seed.swap_id}", seed.asset, GRC_PAYOUT, seed.destination,
         seed.amount, seed.swap_status, WHEN, WHEN, WHEN),
    )
    conn.execute(
        "INSERT INTO payouts (swap_id, asset, destination_address, amount, txid, status, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (seed.swap_id, seed.asset, seed.destination, seed.amount, seed.txid, seed.status, WHEN),
    )


@pytest.fixture
def seeded(tmp_path):
    """The operator's five inventory rows and the payout rows behind two of them.

    ONE DATABASE PER TEST, because --apply writes and an idempotency test has to
    be able to run the tool twice against a row it knows the history of.
    """
    db_path = tmp_path / "inventory.db"
    conn = sqlite3.connect(db_path)
    conn.row_factory = dict_factory
    conn.executescript(SCHEMA)
    conn.executescript(INVENTORY_CORRECTIONS_SQL)
    for asset, confirmed, reserved, available, when in (
        ("BTC", 10.0012, 0.0, 10.0012, WHEN),
        ("GRC", GRC_CONFIRMED, GRC_RESERVED, GRC_AVAILABLE, WHEN),
        ("LTC", 100.0, 0.0, 100.0, WHEN),
        ("SOL", 28.087120892, 0.0, 28.087120892, WHEN),
        ("XRP", XRP_NEGATED, 0.0, XRP_NEGATED, XRP_WHEN),
    ):
        conn.execute(
            "INSERT INTO wallet_inventory (asset, hot_confirmed, hot_reserved, hot_available, "
            "updated_at) VALUES (?, ?, ?, ?, ?)",
            (asset, confirmed, reserved, available, when),
        )
    # GRC: five failed payouts, none of which had a release. This is the leak.
    for index, amount in enumerate(GRC_FAILED_AMOUNTS, start=1):
        _seed(conn, Seed(f"s_grc_failed_{index}", "GRC", amount, "failed"))
    # XRP: both payouts delivered, so both already had their release. Nothing
    # justifies a reservation, and the row is the negated sum of exactly these.
    for index, amount in ((23, XRP_PAYOUT_23), (24, XRP_PAYOUT_24)):
        _seed(conn, Seed(f"s_xrp_{index}", "XRP", amount, "broadcast",
                         txid=f"TX{index}", swap_status="completed"))
    conn.commit()
    conn.close()
    return db_path


def _inventory(db_path) -> dict:
    conn = sqlite3.connect(db_path)
    conn.row_factory = dict_factory
    try:
        return {row["asset"]: row for row in conn.execute("SELECT * FROM wallet_inventory").fetchall()}
    finally:
        conn.close()


def _corrections(db_path) -> list:
    conn = sqlite3.connect(db_path)
    conn.row_factory = dict_factory
    try:
        return conn.execute(
            f"SELECT * FROM {INVENTORY_CORRECTIONS_TABLE} ORDER BY id ASC"  # noqa: S608 -- a literal constant from db.py, an identifier, never input.
        ).fetchall()
    finally:
        conn.close()


def _run(db_path, *extra) -> int:
    """The REAL tool's main(), not a paraphrase of it (the behavioral-verification principle)."""
    return repair_inventory_reservations.main(["--db", str(db_path), *extra])


# ---------------------------------------------------------------------------
# DEFECT 1: the row that should not exist
# ---------------------------------------------------------------------------


def test_the_measured_XRP_row_is_reproduced_BY_THE_REAL_FUNCTIONS(tmp_path):
    """The operator's -59.231412662192405 is what the real reserve/release pair produces.

    THIS IS THE TEST THAT ESTABLISHES THE CODE PATH, and it is here rather than in
    prose because the brief's claim -- "hot_confirmed is the exact negation of the
    sum of its own payouts" -- is an arithmetic coincidence until the functions
    that would produce it are actually run. Rule 17: run the thing that would show
    it false.

    What it pins, beyond the figure:

      reserve_inventory() IS THE CREATOR. The database starts with no XRP row at
      all, and the only call made before the first release is reserve_inventory()
      -- so its INSERT branch, `(asset, 0.0, amount, -amount)`, is what brings an
      XRP row into existence. refresh_wallet_inventory() cannot: it calls
      get_balance() first and chains/xrp.XRPAdapter.get_balance() raises.

      release_inventory_after_send() IS THE DEBITER, with no floor, against a
      hot_confirmed that was never credited.

    MUTATION: change reserve_inventory()'s INSERT to seed hot_confirmed from the
    amount instead of 0.0 -- `(asset, amount, amount, 0.0)`. hot_confirmed then
    reads 0.0 rather than -59.231412662192405 and this fails on the exact-negation
    assertion. Verified 2026-10-04; restored.

    MUTATION: floor release_inventory_after_send()'s hot_confirmed write at zero,
    `max(float(row["hot_confirmed"]) - amount, 0.0)` -- which is one plausible
    reading of the proposed cause fix. hot_confirmed reads 0.0 and this fails.
    Verified 2026-10-04; restored. It is recorded because it shows the test is
    pinned to the DEFECT and will fail the moment the cause is fixed, which is the
    behavior a test of a defect should have.
    """
    db_path = tmp_path / "xrp.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(SCHEMA)
    conn.commit()
    conn.close()

    with db_session(str(db_path)) as db:
        assert db.execute("SELECT COUNT(*) AS n FROM wallet_inventory").fetchone()["n"] == 0, (
            "the row must not exist before the first reserve, or this test cannot show what created it"
        )
        for amount in (XRP_PAYOUT_23, XRP_PAYOUT_24):
            reserve_inventory(db, "XRP", amount)
            db.commit()
            release_inventory_after_send(db, "XRP", amount)
            db.commit()
        row = db.execute("SELECT * FROM wallet_inventory WHERE asset = 'XRP'").fetchone()

    # THE DEFECT NO LONGER REPRODUCES, AND THIS TEST FLIPPED TO SAY SO -- which is
    # the behavior its own docstring predicted before the cause was fixed: "it
    # shows the test is pinned to the DEFECT and will fail the moment the cause is
    # fixed". It did, so it now pins the fix instead of the fault.
    #
    # WHAT CHANGED: release_inventory_after_send() no longer writes hot_confirmed
    # at all. That column belongs to refresh_wallet_inventory(), which sets it from
    # adapter.get_balance() every 60s -- and XRP's get_balance() refuses by design,
    # so the poller skips XRP and nothing ever corrected a debit made here. Two
    # writers, one column, and only one of them could measure it.
    #
    # NOT A FLOOR, which the old MUTATION note above shows was considered and
    # rejected: clamping at max(..., 0.0) would read 0.0 for an account that holds
    # XRP nobody can count -- the "a stale balance is not a small balance" error
    # inverted -- and would destroy the `hot_confirmed < 0` discriminator
    # test_an_uncreditable_row_is_DROPPED_rather_than_zeroed depends on.
    #
    # XRP_NEGATED IS KEPT as the recorded measurement rather than deleted (rule 1:
    # the drift is the point). It is what the operator's host read on 2026-10-04
    # before the fix, and the assertion below is that these same functions can no
    # longer produce it.
    assert row["hot_confirmed"] == 0.0, (
        f"a release wrote hot_confirmed again: {row['hot_confirmed']!r}. Before 2026-10-04 these "
        f"same two calls produced {XRP_NEGATED!r} -- the exact negated sum of the two payouts, a "
        f"ledger with a debit side and no credit side -- and the fix was to stop writing the column "
        f"here rather than to floor it"
    )
    assert row["hot_confirmed"] != XRP_NEGATED, (
        "the measured defect reproduced, so the cause is back"
    )
    assert row["hot_reserved"] == 0.0, "the reservation this function DOES own must return to zero"
    assert row["hot_available"] == 0.0, (
        "hot_available is derived from the two columns above and must agree with them"
    )


def test_an_uncreditable_row_is_DROPPED_rather_than_zeroed(seeded):
    """XRP's row goes entirely, because there is no number to put in it.

    0.0 would be a CLAIM -- that the account holds nothing -- and it holds XRP;
    this process simply cannot read how much. An ABSENT row is what the tree
    already means by "not established" for this asset: chains/xrp.py's
    get_balance() docstring states in advance that "XRP gets no wallet_inventory
    row", and services/payout_service.inventory_note() reports a missing row for
    such an asset as expected by design. So the deletion restores the documented
    state and the presence of the row was the anomaly.

    THE ASSERTION IS ON THE ROWS, not on the tool's exit code or its output: after
    the run there is no XRP row, and the OTHER four assets are still there. A tool
    that deleted the table would also satisfy "no XRP row".

    MUTATION: make plan_for_asset()'s negative branch return TRIM instead of DROP.
    The XRP row survives with hot_reserved=0.0 and hot_confirmed still
    -59.231412662192405, and this fails on `"XRP" not in after`. Verified
    2026-10-04; restored.
    """
    assert _run(seeded, "--apply") == 0
    after = _inventory(seeded)
    assert "XRP" not in after, (
        f"the XRP row should be gone; it reads {after.get('XRP')!r}. Zeroing it would assert the "
        f"account is empty, which is a measurement nobody took"
    )
    assert set(after) == {"BTC", "GRC", "LTC", "SOL"}, (
        "only XRP's row may be dropped -- every other asset's balance CAN be read"
    )


def test_the_DROP_is_refused_when_the_uncreditable_row_still_owes_a_payout(seeded):
    """An uncreditable row with a live reservation is REFUSED, not dropped and not trimmed.

    THIS IS THE BRANCH THAT KEEPS THE TOOL HONEST. Dropping would discard an
    obligation for a payout that has not left; trimming would leave the negated
    payout sum sitting in hot_confirmed looking like a balance, which is the defect
    with its one visible symptom removed -- rule 19's definition of a patch.

    Nothing in the operator's measured data takes this branch. It is tested because
    the alternative to having it is a tool that guesses when something does.

    MUTATION: drop the `if justified > 0.0` guard from plan_for_asset()'s negative
    branch so every uncreditable row is DROPPED. The row disappears, the live
    reservation of 7.5 XRP goes with it, and this fails on `"XRP" in after`.
    Verified 2026-10-04; restored.
    """
    conn = sqlite3.connect(seeded)
    conn.row_factory = dict_factory
    _seed(conn, Seed("s_xrp_live", "XRP", 7.5, "created", swap_status="paying"))
    conn.execute("UPDATE wallet_inventory SET hot_reserved = 7.5 WHERE asset = 'XRP'")
    conn.commit()
    conn.close()

    assert _run(seeded, "--apply") == 0
    after = _inventory(seeded)
    assert "XRP" in after, "a row owing a live payout must not be dropped"
    assert after["XRP"]["hot_reserved"] == 7.5, "and must not be trimmed either -- nothing is written"
    assert after["XRP"]["hot_confirmed"] == XRP_NEGATED
    assert not [row for row in _corrections(seeded) if row["asset"] == "XRP"], (
        "a REFUSED row writes no correction row: there was no correction"
    )


# ---------------------------------------------------------------------------
# DEFECT 2: the reservation that exceeds the balance
# ---------------------------------------------------------------------------


def test_the_report_writes_NOTHING(seeded):
    """The default is a read. Every row, and the corrections table, are untouched.

    ASSERTED ON THE WHOLE TABLE rather than on GRC alone, because "writes nothing"
    is a claim about the database and not about the row under discussion.

    MUTATION: remove the `if args.apply` guard in _report_one_asset() so
    apply_plan() runs on every TRIM/DROP. GRC's hot_reserved moves to 0.0, XRP's
    row vanishes, two correction rows appear, and this fails on the first
    assertion. Verified 2026-10-04; restored.
    """
    before = _inventory(seeded)
    assert _run(seeded) == 0
    assert _inventory(seeded) == before, "the report must not change a single column"
    assert _corrections(seeded) == [], "and must not write a correction row either"


def test_apply_corrects_EXACTLY_the_leaked_amount(seeded):
    """GRC's hot_reserved becomes what the rows justify, and hot_available follows.

    THE SEEDED STATE IS THE OPERATOR'S: hot_reserved=9882.372957331736 against
    five `failed` payout rows and not one `created` row, so the justified figure is
    0.0 and the entire reservation is leaked. hot_available is recomputed as
    hot_confirmed - hot_reserved, which is what db.py's schema comment says the
    column holds and what refresh_wallet_inventory() computes -- leaving it stale
    would correct one number and falsify the one derived from it.

    hot_confirmed IS ASSERTED UNCHANGED, because never widening the write is a
    required property and not an incidental one: that figure comes from the chain
    and the next reconcile cycle overwrites it anyway.

    MUTATION: make apply_plan()'s UPDATE write `plan.reserved` instead of
    `plan.justified` -- a one-word change that looks like a fix. hot_reserved
    stays at 9882.372957331736 and this fails. Verified 2026-10-04; restored.

    MUTATION: drop `hot_available = ?` from the same UPDATE. hot_reserved is
    corrected to 0.0 while hot_available stays -6102.288412361736, and this fails
    on the hot_available assertion -- which is the half a reviewer would have let
    through. Verified 2026-10-04; restored.
    """
    assert _run(seeded, "--apply") == 0
    grc = _inventory(seeded)["GRC"]
    assert grc["hot_reserved"] == 0.0, (
        f"five failed payout rows justify nothing, so the whole {GRC_RESERVED!r} was leaked"
    )
    assert grc["hot_available"] == GRC_CONFIRMED, (
        "hot_available is hot_confirmed - hot_reserved; with nothing reserved it is the balance"
    )
    assert grc["hot_confirmed"] == GRC_CONFIRMED, (
        "hot_confirmed must NEVER be written: it comes from the chain and the next refresh overwrites it"
    )


def test_only_a_CREATED_payout_justifies_a_reservation(seeded):
    """The status set is the hinge of the whole correction, so it is asserted on rows.

    THE SEED IS BUILT TO MAKE EVERY WRONG ANSWER VISIBLE. GRC gets, beside its five
    `failed` rows, one `created` row of 500.0 and one `broadcast` row of 777.0:

      correct               justified = 500.0        hot_reserved -> 500.0
      if 'failed' counted   + 9993.425862093694      hot_reserved -> 10493.43...
      if 'broadcast' too    + 777.0                  hot_reserved -> 1277.0
      if PAYOUT_LIVE_STATUSES were used (created, broadcast, completed) -> 1277.0

    A `broadcast` row has ALREADY had release_inventory_after_send() subtract it,
    so counting it doubles every delivered payout into the retention floor. A
    `failed` row never had a release at all, so counting it declares the leak
    correct. Only `created` is still owed.

    MUTATION (required by the brief -- the live-status set): change
    db.PAYOUT_RESERVED_STATUSES from ("created",) to PAYOUT_LIVE_STATUSES'
    ("created", "broadcast", "completed"). hot_reserved is written as 1277.0 and
    this fails. Verified 2026-10-04; restored.

    MUTATION: change it to ("created", "failed"). hot_reserved is written as
    10493.425862093694 -- HIGHER than the figure being repaired -- and this fails.
    Verified 2026-10-04; restored.
    """
    assert PAYOUT_RESERVED_STATUSES == ("created",), (
        "this test's arithmetic is built on that set; if it moved, read the argument beside the "
        "constant in db.py and re-derive the expectations rather than editing the numbers"
    )
    assert set(PAYOUT_RESERVED_STATUSES) < set(PAYOUT_LIVE_STATUSES), (
        "a reservation-justifying status must be a live status, and strictly fewer of them -- the "
        "difference is argued beside both constants in db.py"
    )
    conn = sqlite3.connect(seeded)
    conn.row_factory = dict_factory
    _seed(conn, Seed("s_grc_live", "GRC", 500.0, "created", swap_status="paying"))
    _seed(conn, Seed("s_grc_sent", "GRC", 777.0, "broadcast", txid="TXSENT",
                     swap_status="completed"))
    conn.commit()
    conn.close()

    assert _run(seeded, "--apply") == 0
    grc = _inventory(seeded)["GRC"]
    assert grc["hot_reserved"] == 500.0, (
        f"only the one 'created' row justifies a reservation; got {grc['hot_reserved']!r}. 1277.0 "
        f"means 'broadcast' was counted (it already had its release); 10493.425862093694 means "
        f"'failed' was counted (it never had one)"
    )
    assert grc["hot_available"] == GRC_CONFIRMED - 500.0


def test_an_asset_whose_reservation_IS_justified_is_untouched(seeded):
    """LTC's row is correct, and a correct row must be left exactly as it stands.

    This is the "never widen" property from the other side: the tool must not
    normalize, round, re-timestamp or recompute a row it has no finding about.
    `updated_at` is asserted too, because a touched timestamp on an unchanged row
    makes every reader's staleness check lie.

    MUTATION: make plan_for_asset()'s final branch return TRIM instead of OK, so a
    row where leaked == 0.0 is "corrected" to the figure it already holds.
    hot_reserved is unchanged -- the write is a no-op -- but `updated_at` moves and
    a correction row appears, and this fails on both of the last two assertions.
    Verified 2026-10-04; restored. It is the mutation that shows why those two
    assertions are here: the money column alone would not have caught it.
    """
    conn = sqlite3.connect(seeded)
    conn.row_factory = dict_factory
    _seed(conn, Seed("s_ltc_live", "LTC", 1.25, "created", swap_status="paying"))
    conn.execute("UPDATE wallet_inventory SET hot_reserved = 1.25, hot_available = 98.75 "
                 "WHERE asset = 'LTC'")
    conn.commit()
    conn.close()
    before = _inventory(seeded)["LTC"]

    assert _run(seeded, "--apply") == 0
    after = _inventory(seeded)["LTC"]
    assert after == before, f"a justified row must be untouched; {before!r} became {after!r}"
    assert after["updated_at"] == WHEN, "not even the timestamp"
    assert not [row for row in _corrections(seeded) if row["asset"] == "LTC"], (
        "and no correction row, because nothing was corrected"
    )


def test_an_asset_with_NO_payout_rows_at_all_and_no_reservation_is_untouched(seeded):
    """BTC and SOL have never had a payout seeded here, and read OK rather than being skipped.

    The LEFT JOIN in JUSTIFIED_SQL is what makes this work: an asset with no
    matching payout row still appears, with justified 0.0. An INNER JOIN would
    have dropped exactly the rows being repaired -- GRC and XRP both have zero
    JUSTIFYING rows -- so this pins the join, not just the arithmetic.

    MUTATION: change JUSTIFIED_SQL's `LEFT JOIN` to `JOIN`. GRC and XRP vanish
    from the report entirely, the tool reports 2 of 2 rows all correct, and this
    test still passes -- so it is NOT the test that catches it;
    test_apply_corrects_EXACTLY_the_leaked_amount is, and it fails with GRC
    unchanged. Verified 2026-10-04; restored. Recorded as a NON-catch on purpose,
    because a mutation list that only records hits is a list of guesses about
    coverage.
    """
    before = _inventory(seeded)
    assert _run(seeded, "--apply") == 0
    after = _inventory(seeded)
    assert after["BTC"] == before["BTC"]
    assert after["SOL"] == before["SOL"], (
        "SOL's balance CAN be read -- chains/solana.get_balance() answers whenever SOL_HOT_WALLET is "
        "set, and 28.087120892 is lamport precision -- so it does not share XRP's shape"
    )


def test_a_reservation_BELOW_what_the_rows_justify_is_refused_and_not_raised(seeded):
    """The 111.05 shape: hot_reserved SHORT of what is owed. Reported, never corrected upward.

    THE OPERATOR'S GRC DISCREPANCY HAS THIS SIGN SOMEWHERE IN IT, which is why the
    tool has the branch at all:

        failed GRC total    9993.425862093694
        hot_reserved        9882.372957331736
        shortfall             111.05290476195842

    Raising a reservation TIGHTENS swap_terminal/fee_sweep.obligation()'s retention
    floor, which is a posture change on the desk's own money decided by arithmetic
    nobody has reconciled against a chain. It is also the known signature of a
    DOUBLE RELEASE -- settle_payout.py performed its writes twice until 2026-09-26,
    each pass subtracting the same reservation -- and 111.05290476195842 is within
    about 46 ulps of twice payout id=3's 55.52645238097888. So the tool refuses and
    prints the rows.

    MUTATION: delete the `if leaked < 0.0` branch from plan_for_asset() so a
    shortfall falls through to OK. The verdict becomes OK and this fails on the
    REFUSE assertion -- and the row would then be silently blessed as correct,
    which is the outcome worth a test.

    MUTATION: make that branch return TRIM. hot_reserved is RAISED to the justified
    figure and this fails on the untouched-row assertion. Verified 2026-10-04 for
    both; restored.
    """
    conn = sqlite3.connect(seeded)
    conn.row_factory = dict_factory
    _seed(conn, Seed("s_grc_short", "GRC", GRC_FAILED_TOTAL, "created", swap_status="paying"))
    conn.commit()
    conn.close()
    before = _inventory(seeded)["GRC"]
    assert before["hot_reserved"] == GRC_RESERVED

    row = dict(before, asset="GRC")
    plan = plan_for_asset(row, GRC_FAILED_TOTAL, 1)
    assert plan.verdict == REFUSE, f"a shortfall must be refused, not {plan.verdict}"
    assert plan.leaked == -GRC_RESERVED_SHORTFALL
    assert f"{GRC_RESERVED_SHORTFALL!r}" in plan.why, (
        "the sentence must carry the figure the operator has to reconcile"
    )

    assert _run(seeded, "--apply") == 0
    assert _inventory(seeded)["GRC"] == before, (
        "a refused row is written to in neither direction, not even under --apply"
    )


# ---------------------------------------------------------------------------
# The audit record, and idempotency
# ---------------------------------------------------------------------------


def test_the_correction_row_holds_the_ORIGINAL_figures(seeded):
    """One correction row per correction, in the same transaction, preserving the row as it stood.

    A SILENT OVERWRITE OF A MONEY COLUMN IS UNACCEPTABLE, and the requirement is
    correct_payout_amounts.py's: the original must be recoverable FROM THE DATABASE
    ALONE, with no git history, by somebody reconciling a figure a year from now.

    ALL THREE ORIGINALS ARE ASSERTED, not just hot_reserved. hot_available is
    nominally derived from the other two, so a record holding one figure would
    require the reader to trust that the derivation held at the time -- which is
    exactly what was broken here.

    THE DROPPED ROW'S RECORD IS ASSERTED TOO, and its `now_*` columns are NULL: a
    row that no longer exists has no "now", and writing 0.0 there would record a
    zeroing that did not happen.

    MUTATION (required by the brief -- the audit write): delete the INSERT INTO
    wallet_inventory_corrections from apply_plan(). Both corrections still land on
    `wallet_inventory` and this fails on the `len(rows) == 2` assertion. Verified
    2026-10-04; restored.

    MUTATION: write `plan.justified` into was_hot_reserved instead of
    row["hot_reserved"] -- the audit row then records 0.0 as the original, so the
    figure that was replaced is lost while the row still looks like a complete
    audit trail. This fails on was_hot_reserved. Verified 2026-10-04; restored.
    That is the mutation a reviewer is least likely to catch, because the row count
    and the correction are both still right.
    """
    assert _run(seeded, "--apply") == 0
    rows = _corrections(seeded)
    assert len(rows) == 2, f"one row per correction -- GRC trimmed and XRP dropped; got {len(rows)}"
    by_asset = {row["asset"]: row for row in rows}

    grc = by_asset["GRC"]
    assert grc["action"] == TRIM
    assert grc["was_hot_confirmed"] == GRC_CONFIRMED
    assert grc["was_hot_reserved"] == GRC_RESERVED, (
        "the figure that was replaced is the whole point of the record"
    )
    assert grc["was_hot_available"] == GRC_AVAILABLE
    assert grc["now_hot_reserved"] == 0.0
    assert grc["now_hot_available"] == GRC_CONFIRMED
    assert grc["justified_by"] == "payouts.status IN ['created']", (
        "the record must say which status set decided the figure, or a reader cannot check it"
    )
    assert f"{GRC_RESERVED!r}" in grc["message"], "the prose carries the original too"

    xrp = by_asset["XRP"]
    assert xrp["action"] == DROP
    assert xrp["was_hot_confirmed"] == XRP_NEGATED, (
        "the negated payout sum is the evidence for the deletion and must survive it"
    )
    assert xrp["now_hot_confirmed"] is None, "a deleted row has no 'now'"
    assert xrp["now_hot_reserved"] is None
    assert xrp["now_hot_available"] is None


def test_a_second_run_corrects_NOTHING_and_says_so(seeded, capsys):
    """Idempotent: the repair is re-derived, found already done, and nothing is written.

    THE GUARD IS THE PLAN AND NOT A FLAG, which is why this is asserted on rows
    rather than on a marker: the second run re-runs the same SQL, finds
    hot_reserved already equal to what the rows justify, and returns OK. There is
    no "already repaired" column to trust and nothing to go stale.

    THE DROP IS IDEMPOTENT BY ABSENCE -- the XRP row is gone, so the second run
    does not see the asset at all.

    AND IT PRINTS `(none)` RATHER THAN AN EMPTY SECTION (rule 14): a blank gap is
    ambiguous between "nothing to correct" and "the query broke".

    MUTATION (required by the brief -- the idempotency guard): change
    plan_for_asset()'s final comparison from `leaked > 0.0` to `leaked >= 0.0`, so
    a row where reserved already equals justified is TRIMMED again. The second run
    writes a third and fourth correction row and moves `updated_at`, and this fails
    on both the correction-count and the row-equality assertions. Verified
    2026-10-04; restored.

    MUTATION: make apply_plan()'s UPDATE unconditional by dropping
    `AND hot_reserved = ?`. This test still passes, because the second run never
    reaches apply_plan() -- the plan is OK. Recorded as a NON-catch: the
    compare-and-swap is a concurrency guard, not the idempotency guard, and
    conflating the two is how a tool ends up relying on the wrong one.
    """
    assert _run(seeded, "--apply") == 0
    first = _inventory(seeded)
    assert len(_corrections(seeded)) == 2
    capsys.readouterr()

    assert _run(seeded, "--apply") == 0
    assert _inventory(seeded) == first, "a second run must change nothing"
    assert len(_corrections(seeded)) == 2, (
        "and must write no further correction row -- a correction row for a correction that did not "
        "happen is a falsified audit trail"
    )
    printed = capsys.readouterr().out
    assert "(none)" in printed, (
        "an empty result must print (none); a blank gap cannot be told from a broken query"
    )
    assert "0 corrections needed" in printed


# ---------------------------------------------------------------------------
# The consequence: what the leaked figure was actually costing
# ---------------------------------------------------------------------------


def test_the_repair_releases_the_fee_sweep_floor_that_the_leak_was_holding(seeded):
    """Through the REAL fee_sweep.obligation(): floor 9882.37 before, 0.0 after.

    THIS IS THE BLAST RADIUS, MEASURED RATHER THAN DESCRIBED, and it is not the one
    the brief expected. services/payout_capacity.largest_fundable_payout() and
    why_the_payout_cannot_be_funded() both call adapter.get_balance() and read NO
    database column, so a negative hot_available cannot refuse a customer payout --
    asserted in the companion test below. swap_terminal/fee_sweep.obligation() DOES
    read hot_reserved, into `floor = max(open_total, reserved)`, and that reader was
    missing from db.py's own list of who reads this table until 2026-10-04.

    A floor of 9882.372957331736 GRC against a wallet holding 3780.08454497 with
    open_total 0.0 refuses every GRC fee sweep for as long as the figure stands --
    the desk cannot take its own fee out, and the refusal names a reservation that
    nothing owes.

    MUTATION: make apply_plan()'s UPDATE write hot_available only and leave
    hot_reserved alone. obligation() still returns floor=9882.372957331736 after
    the repair and this fails -- which is the point of asserting through the real
    consumer rather than on the column: hot_reserved is the figure with a reader.
    Verified 2026-10-04; restored.
    """
    with db_session(str(seeded)) as db:
        before = obligation(db, "GRC")
    assert before.reserved == GRC_RESERVED
    assert before.floor == GRC_RESERVED, (
        f"floor = max(open_total, reserved) = max({before.open_total!r}, {GRC_RESERVED!r})"
    )
    assert before.floor > GRC_CONFIRMED, (
        "the floor exceeds the wallet, which is what refuses every sweep"
    )

    assert _run(seeded, "--apply") == 0

    with db_session(str(seeded)) as db:
        after = obligation(db, "GRC")
    assert after.reserved == 0.0
    assert after.floor == 0.0, "nothing is owed, so nothing is retained"
    assert after.open_total == before.open_total, (
        "the repair must not touch `swaps`; open_total is computed from those rows"
    )


def test_payout_capacity_does_NOT_consult_these_columns_so_no_customer_payout_was_refused(seeded):
    """The fear the brief names, measured and FALSE. Both functions ask the adapter.

    Seeded with the operator's own negative hot_available, then with hot_confirmed
    and hot_available ZEROED, services/payout_capacity.largest_fundable_payout()
    and why_the_payout_cannot_be_funded() return identical answers -- because both
    call `adapter.get_balance()` themselves and read no row. This re-establishes
    behaviorally what db.py's schema comment records from 2026-10-03, and it is
    asserted here rather than trusted because it decides the blast radius.

    MUTATION: there is no production change that makes this fail without rewriting
    payout_capacity.py to read the table, which is the opposite of a defect -- so
    this test is a PIN rather than a guard: it fails the day somebody wires
    wallet_inventory into the funding gate, at which point the negative figure WOULD
    refuse a customer payout and this comment is the warning. Recorded plainly
    rather than claiming a mutation it does not have.
    """
    class Wallet:
        """The GRC balance the daemon actually reports, with no database in the path."""

        def get_balance(self):
            return GRC_CONFIRMED

    adapters = {"GRC": Wallet()}
    with db_session(str(seeded)) as db:
        assert db.execute(
            "SELECT hot_available FROM wallet_inventory WHERE asset = 'GRC'"
        ).fetchone()["hot_available"] == GRC_AVAILABLE
        with_leak = (largest_fundable_payout(adapters, "GRC", 0.001),
                     why_the_payout_cannot_be_funded(adapters, "GRC", 100.0, 0.001))
        db.execute("UPDATE wallet_inventory SET hot_confirmed = 0, hot_available = 0 "
                   "WHERE asset = 'GRC'")
        db.commit()
        zeroed = (largest_fundable_payout(adapters, "GRC", 0.001),
                  why_the_payout_cannot_be_funded(adapters, "GRC", 100.0, 0.001))

    assert with_leak == zeroed, (
        "both functions read the adapter, not the table, so zeroing the row changes nothing"
    )
    assert not with_leak[1].refuses, (
        f"a hot_available of {GRC_AVAILABLE!r} does NOT refuse a 100 GRC payout on a wallet holding "
        f"{GRC_CONFIRMED!r}. The blast radius is the fee sweep, not the customer"
    )


# ---------------------------------------------------------------------------
# Refusals and output, which are the operator's only interface to this tool
# ---------------------------------------------------------------------------


def test_a_MISSING_database_is_refused_rather_than_created(tmp_path, capsys):
    """Connecting would make an empty file and report a clean repair of nothing.

    Every asset would read OK against a table with no rows, which is rule 14's
    "did nothing must not look like did work" in its worst form on a tool whose
    whole output is reassurance.

    MUTATION: make refusal_before_reading() return None unconditionally. The tool
    creates the file, prints the empty-table summary and exits 0, and this fails on
    both the exit code and the "must not exist" assertion. Verified 2026-10-04;
    restored.
    """
    missing = tmp_path / "nope.db"
    assert repair_inventory_reservations.main(["--db", str(missing)]) == 2
    assert not missing.exists(), "a refusal must not leave a database behind"
    assert "REFUSED" in capsys.readouterr().err


def test_the_output_says_it_moves_no_coins_and_names_the_status_set(seeded, capsys):
    """Rule 14: the parameters that decide the answer are echoed, before the work.

    THE "MOVES NO COINS" LINE IS THE ONE THIS TOOL CANNOT DO WITHOUT. Every other
    --apply at this project root can move money -- collect_fees.py broadcasts a
    sweep, settle_payout.py and rescue_payout.py reconcile real sends -- so an
    operator has correctly learned to hesitate at the flag. The cost of that
    hesitation here is a fee sweep that stays refused.

    MUTATION: remove the `moves coins` line from _announce(). This fails. Verified
    2026-10-04; restored.
    """
    assert _run(seeded) == 0
    printed = capsys.readouterr().out
    assert "BROADCASTS NOTHING" in printed
    assert "moves no coins" in printed
    assert "payouts.status IN ['created']" in printed, (
        "the status set that decided every figure must be on screen (rule 14)"
    )
    assert str(seeded) in printed, "and the database the rows came from"
    assert "REPORT ONLY" in printed, "and which mode this was"


def test_every_payout_row_is_printed_so_the_arithmetic_is_checkable_by_eye(seeded, capsys):
    """The per-row account, which is why this tool prints rows and not just totals.

    THE OPERATOR'S 111.05290476195842 CANNOT BE SETTLED FROM A TOTAL. It is not the
    failed sum, so "every failure leaked" is false, and the rows that would explain
    it are on their host. A tool that printed one difference would hand back a
    number with no way to act on it.

    Every row is printed, not only the justifying ones, because checking a sum needs
    the denominator (rule 3): a `failed` row is a candidate leak and a `broadcast`
    row is one that was released, and both have to be visible.

    MUTATION: filter PER_ROW_SQL to `WHERE p.asset = ? AND p.status IN ('created')`.
    The five GRC failed rows disappear from the report -- the exact rows the
    operator needs in order to find the leak -- and this fails on the row count.
    Verified 2026-10-04; restored.
    """
    assert _run(seeded) == 0
    printed = capsys.readouterr().out
    for index in range(1, 6):
        assert f"s_grc_failed_{index}" in printed, (
            "a failed GRC payout is a candidate leak and must be on screen by swap id"
        )
    # THE MARKER IS COUNTED PER ROW LINE AND NOT OVER THE WHOLE BLOCK. The first
    # version of this assertion counted "JUSTIFIES" in the output and expected 0,
    # and it failed at 2 -- on the legend line, which spells the word in order to
    # explain it. Counting a marker across prose that names the marker measures the
    # prose. So the lines carrying a row id are the ones examined.
    marked = [line for line in printed.splitlines() if " id=" in line and "JUSTIFIES" in line]
    assert marked == [], (
        f"nothing in the seeded state justifies a reservation, so no ROW may be marked: {marked}"
    )
    assert "0 of 5 row(s) justify a reservation" in printed, (
        "GRC: the count and its denominator together (rule 3)"
    )
    assert "0 of 2 row(s) justify a reservation" in printed, (
        "XRP: both payouts were delivered, so both already had their release"
    )


def test_an_asset_with_no_inventory_row_prints_none_rather_than_nothing(seeded, capsys):
    """--asset for something absent is a result, and `(none)` is how a result of zero reads.

    MUTATION: delete the `if args.asset and not rows` branch from main(). The tool
    prints the announce block, a blank line and a summary about 0 rows, with nothing
    saying the asset was not found, and this fails. Verified 2026-10-04; restored.
    """
    assert _run(seeded, "--asset", "DOGE") == 0
    printed = capsys.readouterr().out
    assert "(none)" in printed
    assert "DOGE" in printed


def test_plan_for_asset_decides_from_seeded_values_with_no_database(capsys):
    """THE DECISION IS A FUNCTION AT THE BOTTOM (rule 10), so it is called directly.

    Four verdicts from four seeded rows, with no database, no file and no SQL. That
    is the property rule 10 asks for and the reason the branches are in
    plan_for_asset() rather than inlined into main(): a decision buried in
    orchestration can only be tested by running the whole thing.
    """
    def row(confirmed, reserved):
        return {"asset": "GRC", "hot_confirmed": confirmed, "hot_reserved": reserved,
                "hot_available": confirmed - reserved, "updated_at": WHEN}

    assert plan_for_asset(row(GRC_CONFIRMED, GRC_RESERVED), 0.0, 0).verdict == TRIM
    assert plan_for_asset(row(GRC_CONFIRMED, 500.0), 500.0, 1).verdict == OK
    assert plan_for_asset(row(GRC_CONFIRMED, 500.0), 900.0, 2).verdict == REFUSE
    assert plan_for_asset(row(XRP_NEGATED, 0.0), 0.0, 0).verdict == DROP
    assert plan_for_asset(row(XRP_NEGATED, 7.5), 7.5, 1).verdict == REFUSE
    assert capsys.readouterr().out == "", "the decision prints nothing; the caller does the printing"


def test_a_row_that_CHANGED_since_the_read_is_refused_and_writes_no_correction_row(seeded):
    """The compare-and-swap. A payout worker reserving mid-run must not be overwritten.

    ADDED BECAUSE A MUTATION SURVIVED, and that is the whole reason this test
    exists rather than a property somebody thought of in advance. Dropping
    `AND hot_reserved = ?` from apply_plan()'s UPDATE -- removing the
    compare-and-swap entirely -- left all 18 tests GREEN on 2026-10-04. The
    idempotency test predicted it would and said so, correctly, because a second
    run never reaches apply_plan() at all: its plan is OK. So the guard was
    completely untested while the suite read as covering it, which is exactly the
    "green for the wrong reason" this session is told to hunt.

    WHAT THE GUARD IS FOR, and it is a live host rather than a hypothetical. This
    tool prints a per-row account for every asset before it writes, and on the
    operator's host a payout worker is polling the same database: a swap claimed
    between the read and the write adds to hot_reserved, and an UPDATE with no
    condition would overwrite that reservation with a figure derived from rows as
    they stood BEFORE it existed. The result would be a payout in flight with
    nothing reserved for it -- the leak this tool repairs, created by the repair.

    BOTH HALVES ARE ASSERTED, and the second is the one that matters more: the
    correction is refused, AND no correction row is written. An audit row for a
    correction that did not happen is a falsified trail, and the two are in one
    transaction precisely so neither can happen without the other.

    MUTATION: drop `AND hot_reserved = ?` from the UPDATE and its bound value. The
    UPDATE matches the row anyway, hot_reserved is overwritten with 0.0, a
    correction row is written, and this fails on all three assertions. Verified
    2026-10-04; restored.

    MUTATION THAT WAS A NO-OP, RECORDED BECAUSE I PREDICTED IT WOULD FAIL AND IT
    DID NOT. Moving the correction INSERT above the `if not moved` check, keeping
    the compare-and-swap: all 19 tests stayed GREEN. The reason is the mechanism
    this test is about -- the refusal path calls `db.rollback()`, which discards
    the INSERT along with everything else in the transaction, so the mutation
    changes no observable behavior at all. A survivor that cannot change an outcome
    is not evidence of a coverage hole, and reporting it as one would send the next
    reader looking for a missing assertion that is not missing.

    MUTATION THAT DOES CHANGE BEHAVIOR, the same idea with the rollback removed:
    move the INSERT above the guard AND make the refusal `db.commit()` instead of
    `db.rollback()`. A correction row then lands for a correction that did not
    happen, and this fails on the correction-count assertion ALONE -- the first two
    still pass, because nothing was written to `wallet_inventory`. Verified
    2026-10-04; restored. That is the mutation the first two assertions miss, and it
    is why the third one is here.
    """
    with db_session(str(seeded)) as db:
        row = db.execute("SELECT * FROM wallet_inventory WHERE asset = 'GRC'").fetchone()
    plan = plan_for_asset(row, 0.0, 0)
    assert plan.verdict == TRIM, "the plan must be a real correction, or this proves nothing"

    # A payout worker claims a swap and reserves against GRC, AFTER the read above
    # and BEFORE the write below. reserve_inventory() is the real function, so this
    # is the real interleaving rather than a hand-edited column.
    with db_session(str(seeded)) as db:
        reserve_inventory(db, "GRC", 250.0)
        db.commit()

    with db_session(str(seeded)) as db:
        wrote, sentence = apply_plan(db, row, plan)

    assert wrote is False, "a row that moved under us must not be written"
    assert "REFUSED" in sentence and "changed between the read and the write" in sentence, (
        f"and the operator has to be told which, not just that nothing happened: {sentence!r}"
    )
    after = _inventory(seeded)["GRC"]
    assert after["hot_reserved"] == GRC_RESERVED + 250.0, (
        "the worker's reservation must survive -- overwriting it would strand a payout in flight "
        "with nothing reserved for it, which is the leak this tool repairs, created by the repair"
    )
    assert _corrections(seeded) == [], (
        "and NO correction row: an audit row for a correction that did not happen is a falsified "
        "trail, which is why both are in one transaction"
    )
