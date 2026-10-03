#!/usr/bin/env python3
"""The payout follows the deposit that arrived, and an exact deposit pays the quote.

Role: tests (read-only)
Reads: services/payout_service.payout_amount() and fee_ledger's identity. No
      network, no database, no chain.
Writes: nothing
Can move funds: no
Mainnet-safe: yes

OPERATOR INSTRUCTION 2026-10-03, on being told the payout amount was not fully
locked to the deposit: "goddamit. we need to fix this."

WHAT WAS WRONG. process_pending_payouts() paid `output_amount_estimate` verbatim,
which create_quote() computed from the EXPECTED deposit, while the fee is charged
against what actually arrived. fee_ledger.py's own derivation -- with G the realized
gross, E the quoted gross, f the fee fraction:

    drift = (1 - f) * (G - E)

so any gap between deposit and quote was kept or given away silently at (1-f) of
it. Inside AMOUNT_TOLERANCE_PCT, which is 1%, nothing flagged it.

AND THE FIRST FIX WAS WRONG IN A WAY A FIXTURE CAUGHT. Re-deriving the payout as
`actual * quoted_rate * (1 - f)` is identical ONLY while the stored estimate follows
today's formula. tests/test_address_authority.py seeds rate=0.001, fee_bps=150,
expected=actual=100.0, output_amount_estimate=0.0975 -- and 100*0.001*0.985 =
0.0985. That stored figure came from the SUPERSEDED formula that also deducted the
network fee reserve (0.0985 - 0.001 = 0.0975), dropped on 2026-10-01. So re-deriving
would have paid 0.0985 where the customer was quoted 0.0975: a changed amount on a
deposit that matched the quote exactly, which is the opposite of the ask.

SCALING CANNOT DO THAT, which is why the shipped fix is

    paid = output_amount_estimate * (actual_input / expected_input)

and why the first test below is the one that matters most: with actual == expected
the ratio is 1 and the quoted figure is paid unchanged, BY CONSTRUCTION, whatever
formula produced it.
"""

from __future__ import annotations

import pytest
from services.payout_service import payout_amount


def swap_row(*, estimate=0.0975, expected=100.0, actual=100.0):
    """The four fields payout_amount() reads. A dict, because it only subscripts.

    DELIBERATELY SEEDED INCONSISTENT by default: estimate=0.0975 against
    expected=100.0 is NOT 100*0.001*0.985, and that is the whole point -- it is
    tests/test_address_authority.py's real fixture, carrying a figure from the
    superseded reserve-deducting formula. A test built on a self-consistent row
    could not tell scaling from re-deriving, which is the distinction that cost a
    wrong first fix.
    """
    return {"id": "s_test", "output_amount_estimate": estimate,
            "expected_input_amount": expected, "actual_input_amount": actual}


def test_an_EXACT_deposit_pays_THE_QUOTED_FIGURE_whatever_formula_produced_it():
    """The guard against the first fix. Re-deriving fails this; scaling cannot.

    MUTATION: replace the body with `actual * quoted_rate * (1 - fee_bps/10000)`.
    This row's rate and fee would give 0.0985 against a quoted 0.0975, and this
    fails -- which is exactly how tests/test_address_authority.py caught it.
    """
    amount, why = payout_amount(swap_row(estimate=0.0975, expected=100.0, actual=100.0))
    assert amount == 0.0975, (
        f"a deposit that matched the quote exactly was paid {amount} instead of the quoted 0.0975. The "
        f"payout must not change on an exact deposit, however the quote was computed"
    )
    assert "matched the quote exactly" in why, why


def test_an_UNDER_deposit_is_paid_LESS_in_proportion():
    """0.9% short pays 0.9% less. Before this, it was paid in full."""
    amount, why = payout_amount(swap_row(estimate=0.0975, expected=100.0, actual=99.1))
    assert amount == pytest.approx(0.0975 * 0.991), amount
    assert amount < 0.0975, "an under-deposit was paid the full quoted figure, which is the old defect"
    assert "SCALED" in why and "99.1" in why, why


def test_an_OVER_deposit_is_paid_MORE_in_proportion():
    """The other direction, and it must move too.

    A fix that only reduced an under-deposit would keep the overage -- the same
    silent retention, half the time. This is the half a desk has no incentive to
    notice, which is why it is asserted separately.
    """
    amount, why = payout_amount(swap_row(estimate=0.0975, expected=100.0, actual=100.9))
    assert amount == pytest.approx(0.0975 * 1.009), amount
    assert amount > 0.0975, "an over-deposit was paid the quoted figure, so the overage was kept"
    assert "SCALED" in why, why


def test_the_DRIFT_fee_ledger_MEASURES_goes_to_ZERO():
    """The property the whole change is for, asserted as fee_ledger derives it.

    retained = G - paid;  drift = retained - G*f.  With paid = E_est * G/E and
    E_est = E*rate*(1-f), drift is zero for every deposit size -- so the realized
    fee equals the 150bps schedule rather than the schedule plus a term nobody
    quoted.

    MUTATION: pay the estimate verbatim. drift becomes (1-f)*(G-E), which for the
    0.9%-short case below is a non-zero figure this asserts against.
    """
    rate, fee_bps, expected = 0.001, 150, 100.0
    fee = fee_bps / 10000.0
    estimate = expected * rate * (1 - fee)
    for actual in (99.1, 100.0, 100.9):
        paid, _why = payout_amount(swap_row(estimate=estimate, expected=expected, actual=actual))
        realized_gross = actual * rate
        drift = (realized_gross - paid) - realized_gross * fee
        assert drift == pytest.approx(0.0, abs=1e-12), (
            f"deposit {actual} leaves drift {drift}, so the realized fee is not the scheduled {fee_bps}bps"
        )


def test_a_NULL_actual_pays_the_quote_and_SAYS_SO_rather_than_defaulting_silently():
    """Unreachable in the live path, and recorded rather than assumed.

    A payout_pending swap always has actual_input_amount -- deposit_service writes
    it alongside credited_at. If it is ever NULL the quoted figure is the only
    sensible thing to pay, and the reason has to say the payout was priced on the
    expected deposit so somebody can investigate the row.
    """
    amount, why = payout_amount(swap_row(actual=None))
    assert amount == 0.0975
    assert "NULL" in why and "investigate" in why.lower(), why


def test_a_NON_POSITIVE_expected_does_not_divide_by_zero():
    """The ratio is undefined, so the quote is paid and the row is flagged.

    Dividing anyway would turn an impossible row into a ZeroDivisionError inside the
    payout loop -- which, on the order path, is a crash between claiming a swap and
    sending it.
    """
    for expected in (0.0, None):
        amount, why = payout_amount(swap_row(expected=expected))
        assert amount == 0.0975, (expected, amount)
        assert "undefined" in why, why
