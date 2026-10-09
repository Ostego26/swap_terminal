"""Resolving a HALTED swap the way the operator decided, on their real figures.

Role: test (seeded database; opens no socket)
Reads: resolve_halted_swap.py
Writes: a temp database
Can move funds: no
Mainnet-safe: yes

THE CASE, measured on the operator's host 2026-10-03. s_612fac62489f2122,
XRP -> GRC, halted since 2026-09-26T15:02:25Z:

    expected   5.0 XRP
    seen       1.0 XRP   (20%, outside AMOUNT_TOLERANCE_PCT=0.01)
    payout     277.2412844507888 GRC was the estimate for the full 5.0
    broadcast  nothing

Three resolutions were priced and the operator chose to SCALE THE LOCKED QUOTE:
277.2412844507888 * (1.0 / 5.0) = 55.44825689015776 GRC. They were shown that 1
XRP bought 56.29 GRC on 2026-09-26 and 160.55 GRC on 2026-10-03, so the locked
rate pays 102.69 GRC less than the deposit is worth, and chose it anyway. Every
figure below is theirs.
"""

from __future__ import annotations

import sqlite3

import pytest
from db import SCHEMA, dict_factory

from resolve_halted_swap import SCALE_THE_LOCKED_QUOTE, main, resolve_verdict, scaled_payout_preview

HALTED_AT = "2026-09-26T15:02:25.878829+00:00"
EXPECTED, SEEN = 5.0, 1.0
LOCKED_PAYOUT = 277.2412844507888
#: TYPED, NOT DERIVED FROM THE CODE UNDER TEST. A parametrized case that computes
#: its expectation the way the implementation does passes when both are wrong --
#: an agent hit exactly that in this tree on 2026-10-03, where
#: `(MIN_FABRICATED_CONFIRMATIONS, 1)` moved with the constant it was pinning.
SCALED_PAYOUT = 55.44825689015776
REASON = f"Confirmed amount {SEEN} outside tolerance for expected {EXPECTED}"


def swap_row(status: str = "under_review", actual: float | None = SEEN):
    """A swaps row as resolve_verdict() reads it.

    `actual: float | None` because "nothing credited" is one of the eight
    refusals parametrized below and db.py has `actual_input_amount REAL`,
    nullable -- a NULL there is a row that really exists, which is why the tool
    must refuse it rather than divide by it. Inferring the type from SEEN made
    that case a type error (pyright reportArgumentType, 2026-10-09).
    """
    return {"status": status, "actual_input_amount": actual, "from_asset": "XRP", "to_asset": "GRC",
            "expected_input_amount": EXPECTED, "output_amount_estimate": LOCKED_PAYOUT,
            "failed_reason": REASON}


def payout_row(status="failed", txid=None):
    return {"status": status, "txid": txid, "amount": LOCKED_PAYOUT}


def test_the_operators_real_halted_swap_IS_allowed():
    allowed, reason = resolve_verdict(swap_row(), [])

    assert allowed
    assert "no payout row live and no txid anywhere" in reason


@pytest.mark.parametrize(("label", "swap", "rows"), [
    ("already paying out", swap_row(status="payout_pending"), []),
    ("already completed", swap_row(status="completed"), []),
    ("still waiting for its deposit", swap_row(status="awaiting_deposit"), []),
    ("a live payout row", swap_row(), [payout_row(status="broadcast")]),
    ("a claimed payout row", swap_row(), [payout_row(status="created")]),
    ("a txid on a failed row", swap_row(), [payout_row(txid="ABC123")]),
    ("nothing credited", swap_row(actual=None), []),
    ("zero credited", swap_row(actual=0.0), []),
])
def test_everything_that_is_not_a_held_deposit_is_REFUSED(label, swap, rows):
    """Each is a different way the swap is not what this tool assumes.

    The txid case is the hardest: it is EVIDENCE money left, where rule 2's "I
    could not find a broadcast is not there was no broadcast" cuts the other way.
    """
    allowed, reason = resolve_verdict(swap, rows)

    assert not allowed, f"{label} was allowed"
    assert reason, f"{label} refused without saying why"


def test_the_preview_asks_payout_amount_rather_than_repeating_its_arithmetic():
    """The screen and the broadcast must be the same number for the same reason.

    A preview computing `estimate * actual / expected` itself would agree today and
    drift the first time that scaling changes -- rule 8's shape on the line that
    decides what a customer receives. So the preview CALLS payout_amount(), and
    this asserts the operator's real figure comes back.
    """
    amount, how = scaled_payout_preview(swap_row())

    assert amount == pytest.approx(SCALED_PAYOUT)
    assert how, "payout_amount() explains which figure it chose; the preview must carry that"


def seeded(tmp_path, *, status="under_review", reserved=0.0):
    """The operator's row as it actually stands: halted, no payout row at all."""
    db_path = tmp_path / "halted.db"
    conn = sqlite3.connect(db_path)
    conn.row_factory = dict_factory
    conn.executescript(SCHEMA)
    conn.execute("INSERT INTO quotes (id, from_asset, to_asset, input_amount, quoted_rate, fee_bps, "
                 "network_fee_reserve, output_amount_estimate, expires_at, created_at) "
                 "VALUES ('q1','XRP','GRC',?,56.29,150,0.001,?,?,?)",
                 (EXPECTED, LOCKED_PAYOUT, HALTED_AT, HALTED_AT))
    conn.execute("INSERT INTO swaps (id, quote_id, from_asset, to_asset, deposit_address, payout_address, "
                 "expected_input_amount, actual_input_amount, quoted_rate, fee_bps, network_fee_reserve, "
                 # credited_at IS NULL, AND THE FIRST VERSION OF THIS FIXTURE SET IT --
                 # which made two mutations survive and is recorded here because that is
                 # the defect, not the typo. A swap halted on a tolerance mismatch never
                 # reaches deposit_service._credit_confirmed_deposit(), so credited_at is
                 # NULL on the real row; seeding it meant the stamp test asserted the
                 # FIXTURE rather than the tool, and the "UPDATE ... WHERE credited_at IS
                 # NULL" mutation was a no-op for the same reason. Both mutations are
                 # caught with it NULL.
                 "output_amount_estimate, status, failed_reason, min_confirmations, created_at, "
                 "updated_at, expires_at) VALUES ('s1','q1','XRP','GRC','rDEP','mGRC',?,?,"
                 "56.29,150,0.001,?,?,?,1,?,?,?)",
                 (EXPECTED, SEEN, LOCKED_PAYOUT, status, REASON, HALTED_AT, HALTED_AT, HALTED_AT))
    conn.execute("INSERT INTO wallet_inventory (asset, hot_confirmed, hot_reserved, hot_available, "
                 "updated_at) VALUES ('GRC', 3780.08854497, ?, ?, ?)",
                 (reserved, 3780.08854497 - reserved, HALTED_AT))
    conn.commit()
    conn.close()
    return db_path


def read(db_path, sql):
    conn = sqlite3.connect(db_path)
    conn.row_factory = dict_factory
    try:
        return conn.execute(sql).fetchone()
    finally:
        conn.close()


def test_a_DRY_RUN_writes_nothing(tmp_path):
    db_path = seeded(tmp_path)

    code = main(["--swap", "s1", "--resolution", SCALE_THE_LOCKED_QUOTE, "--db", str(db_path)])

    assert code == 0
    assert read(db_path, "SELECT status FROM swaps WHERE id='s1'")["status"] == "under_review"
    assert read(db_path, "SELECT COUNT(*) AS n FROM swap_audit_log")["n"] == 0


def test_APPLY_moves_it_to_payout_pending_and_writes_one_audit_row(tmp_path):
    """And the worker computes the payout, which is why no payouts row is written here."""
    db_path = seeded(tmp_path)

    code = main(["--swap", "s1", "--resolution", SCALE_THE_LOCKED_QUOTE, "--db", str(db_path), "--apply"])

    assert code == 0
    swap = read(db_path, "SELECT * FROM swaps WHERE id='s1'")
    assert swap["status"] == "payout_pending"
    assert "resolved by resolve_halted_swap.py" in swap["failed_reason"]
    assert "chose the locked one" in swap["failed_reason"], (
        "the row has to record that a person was shown the alternative and picked this"
    )
    audit = read(db_path, "SELECT * FROM swap_audit_log")
    assert audit["old_status"] == "under_review" and audit["new_status"] == "payout_pending"
    assert str(SCALED_PAYOUT) in audit["message"]
    assert read(db_path, "SELECT COUNT(*) AS n FROM payouts")["n"] == 0, (
        "this tool writes no payouts row: payout_worker claims it and computes the amount itself"
    )


def test_APPLY_does_NOT_touch_the_reservation(tmp_path):
    """The difference from rescue_payout.py, checked rather than assumed.

    That tool releases a reservation because a FAILED payout attempt reserved the
    amount before the send and nothing released it. A halted swap has no payout row
    at all -- the verdict refuses if one is live -- so nothing was ever reserved for
    it. Releasing anyway would subtract a reservation belonging to some OTHER swap
    on the same asset, which is why this is a test and not a comment.
    """
    db_path = seeded(tmp_path, reserved=12.5)

    main(["--swap", "s1", "--resolution", SCALE_THE_LOCKED_QUOTE, "--db", str(db_path), "--apply"])

    inventory = read(db_path, "SELECT * FROM wallet_inventory WHERE asset='GRC'")
    assert inventory["hot_reserved"] == 12.5, "another swap's reservation was reduced"
    assert inventory["hot_available"] == pytest.approx(3780.08854497 - 12.5)


def test_a_swap_that_is_not_halted_is_REFUSED_by_the_CLI_too(tmp_path):
    """The call site, not just the verdict function."""
    db_path = seeded(tmp_path, status="completed")

    code = main(["--swap", "s1", "--resolution", SCALE_THE_LOCKED_QUOTE, "--db", str(db_path), "--apply"])

    assert code == 3
    assert read(db_path, "SELECT status FROM swaps WHERE id='s1'")["status"] == "completed"
    assert read(db_path, "SELECT COUNT(*) AS n FROM swap_audit_log")["n"] == 0


def test_an_unknown_swap_id_is_REFUSED_without_writing(tmp_path):
    db_path = seeded(tmp_path)

    code = main(["--swap", "s_nope", "--resolution", SCALE_THE_LOCKED_QUOTE, "--db", str(db_path),
                 "--apply"])

    assert code == 2
    assert read(db_path, "SELECT status FROM swaps WHERE id='s1'")["status"] == "under_review"


def test_the_resolution_flag_has_no_default(tmp_path):
    """A tool that moves money must not have a default for WHICH way it moves it.

    The other resolutions priced for the operator -- re-pricing at tonight's rate,
    refunding the deposit -- are deliberately unimplemented, and argparse refusing
    a missing --resolution is what keeps a future one from being reached by
    accident.
    """
    db_path = seeded(tmp_path)

    with pytest.raises(SystemExit) as exit_info:
        main(["--swap", "s1", "--db", str(db_path)])

    assert exit_info.value.code == 2
    with pytest.raises(SystemExit):
        main(["--swap", "s1", "--resolution", "refund-the-deposit", "--db", str(db_path)])


def test_APPLY_stamps_credited_at_because_resolving_IS_accepting_the_deposit(tmp_path):
    """A PAID swap said its deposit was never accepted. Measured 2026-10-03.

    Minutes after this tool first ran, s_612fac62489f2122 went under_review ->
    payout_pending -> completed, broadcast 55.44825689 GRC, and show_swap.py
    reported

        credited  (none) -- the deposit was never accepted
        1.0 XRP  1 confirmation(s)  COUNTED by the gate  credited_at (not credited)

    on a swap that had just paid out. deposit_service._credit_confirmed_deposit()
    is the only thing that stamps those columns, and moving a swap straight to
    payout_pending skips it.

    RESOLVING A HALTED SWAP THIS WAY *IS* ACCEPTING THE DEPOSIT, which is what
    makes the stamp correct rather than cosmetic: a person looked at a deposit
    outside tolerance and decided to pay for it. The row should say it was
    accepted, because it was -- by a person rather than by the gate.

    THE STRANDING QUESTION WAS CHECKED RATHER THAN ASSUMED, and the answer is why
    this is display-only: unattributable_deposit_service.unclaimed_events() takes
    `credited` as the set of txids that HAVE a deposit_events row, not those with
    credited_at set, so the missing stamp could not have made this deposit read as
    stranded money.
    """
    db_path = seeded(tmp_path)
    conn = sqlite3.connect(db_path)
    conn.execute("INSERT INTO deposit_events (swap_id, asset, txid, vout, address, amount, "
                 "confirmations, first_seen_at, last_seen_at) "
                 "VALUES ('s1','XRP','5FD710C2',1,'rDEP',?,1,?,?)", (SEEN, HALTED_AT, HALTED_AT))
    conn.commit()
    conn.close()

    main(["--swap", "s1", "--resolution", SCALE_THE_LOCKED_QUOTE, "--db", str(db_path), "--apply"])

    assert read(db_path, "SELECT credited_at FROM swaps WHERE id='s1'")["credited_at"], (
        "a swap this tool handed to the payout worker must not report that its deposit was never "
        "accepted"
    )
    assert read(db_path, "SELECT credited_at FROM deposit_events WHERE swap_id='s1'")["credited_at"]


def test_APPLY_does_NOT_rewrite_the_payouts_own_basis(tmp_path):
    """actual_input_amount is what payout_amount() scaled by, and it stays untouched.

    _credit_confirmed_deposit() sets it from confirmed_total. Here it already holds
    the counted figure the preview was computed from, and rewriting the payout's
    own basis during a resolution is the one thing this tool must not do -- the
    figure on the screen the operator authorized would stop matching what the
    worker sends.
    """
    db_path = seeded(tmp_path)

    main(["--swap", "s1", "--resolution", SCALE_THE_LOCKED_QUOTE, "--db", str(db_path), "--apply"])

    swap = read(db_path, "SELECT * FROM swaps WHERE id='s1'")
    assert swap["actual_input_amount"] == SEEN
    assert swap["expected_input_amount"] == EXPECTED
    assert swap["output_amount_estimate"] == LOCKED_PAYOUT, "the quote figure is history, not a draft"
