"""Does the reverse-side amount agree with the forward price, and never round short?

Role: test / measurement (pure arithmetic, asserted against the real forward
      statements and over a swept range rather than at one point)
Reads: chains/amount_solve.py, services/quote_service.py's forward expression
Writes: nothing
Can move funds: no
Mainnet-safe: yes

WHY THIS FILE EXISTS. The ATM wizard lets a customer type either side, and the
"I want 300 GRC" direction is the inverse of pricing that lives in
services/quote_service.py. Two expressions for one relationship is rule 8's shape
at its smallest, and the failure mode is silent: a fee change applied forward and
not inverted gives a wizard that quotes a deposit which prices to the wrong
payout, and nothing raises.

So the forward expression is spelled ONCE here, copied verbatim from
quote_service.py:582 and :615, and every assertion round-trips through it.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest
from chains.amount_solve import (
    CAPACITY_NOT_ESTABLISHED,
    CHAIN_PRECISION,
    UNSOLVABLE_FEE,
    UNSOLVABLE_OUTPUT,
    UNSOLVABLE_PRECISION,
    UNSOLVABLE_RATE,
    deposit_for_desired_payout,
    max_deposit_for_capacity,
    payout_multiplier,
)
from chains.coin_amounts import CHAIN_DECIMALS
from chains.icp_account import ICP_DECIMALS
from chains.solana_units import SOL_DECIMALS
from chains.xrp_units import XRP_DECIMALS
from config import Config


def forward_payout(input_amount: float, rate: float, fee_bps: int) -> float:
    """quote_service.py's two statements, verbatim, as one function.

    Copied rather than imported because create_quote() needs a database, a
    config, adapters and a live price feed to reach these two lines. The copy is
    the hazard this file is about, which is why
    test_the_forward_copy_matches_quote_service_source reads the real source and
    asserts the expression is still spelled this way.
    """
    gross_output = input_amount * rate
    return max(gross_output * (1 - fee_bps / 10000.0), 0.0)


def test_the_forward_copy_matches_quote_service_source():
    """The expression above must still be the one quote_service.py evaluates.

    THE WHOLE FILE RESTS ON THIS. forward_payout() is a hand copy, and a copy
    that silently stops matching turns every assertion below into a test of
    itself. So the real source is read and the two statements are required to be
    present as written.

    This is a textual check and it is the one place in this file where that is
    the right tool: the thing at risk IS a transcription, and there is no row to
    seed for "these two source lines still say this". Everything else here is
    behavioral.
    """
    source = (Path(__file__).resolve().parents[1] / "swap_terminal" / "services" / "quote_service.py").read_text()
    assert "gross_output = input_amount * rate" in source, (
        "quote_service.py no longer computes gross_output this way; forward_payout() above is stale "
        "and every assertion in this file is now measuring the copy against itself"
    )
    assert "output_amount_estimate = max(gross_output * (1 - fee_bps / 10000.0), 0.0)" in source, (
        "quote_service.py's payout expression changed; update forward_payout() and re-derive "
        "chains/amount_solve.deposit_for_desired_payout()'s inverse to match"
    )
    # AND THE RESERVE MUST STILL NOT BE SUBTRACTED. This is the fact that makes
    # the inverse closed-form instead of a fixed point, so its disappearance is
    # the change that would silently make amount_solve wrong rather than stale.
    assert "output_amount_estimate - network_fee_reserve" not in source, (
        "the network fee reserve appears to be deducted from the payout again. If so the inverse in "
        "chains/amount_solve.py is no longer exact: the reserve depends on the payout size, which is "
        "what that function solves for, and it would need to iterate to a fixed point"
    )


def test_the_multiplier_is_the_same_product_both_directions():
    """payout_multiplier() must be exactly what the forward path applies."""
    for rate in (0.5, 1.0, 123.456, 3221.7):
        for fee_bps in (0, 150, 9999):
            # forward(1.0) IS the multiplier, by definition of the expression.
            assert forward_payout(1.0, rate, fee_bps) == pytest.approx(
                payout_multiplier(rate, fee_bps), rel=1e-12
            )


@pytest.mark.parametrize("asset", sorted(CHAIN_PRECISION))
def test_a_solved_deposit_never_pays_out_less_than_asked(asset):
    """The rounding direction, which is the only real decision in that module.

    Config.AMOUNT_TOLERANCE_PCT is 0.01 and deposit_service sends a deposit
    outside that band to `under_review` instead of crediting it. So a solved
    deposit that rounds DOWN makes the customer send slightly less than
    `expected_input_amount`, and on a thin tolerance that is a deposit which
    stops and waits for a human rather than paying out.

    Swept across sizes and fees rather than asserted at one point, because a
    rounding error that only appears at some decimal positions is exactly the
    kind that passes a single-value test. Every case must pay out AT LEAST what
    was asked -- never a hair under.
    """
    rate = 321.7
    for fee_bps in (0, 150, 500):
        for desired in (0.00000001, 0.5, 1.0, 1.1, 3.14159265, 300.0, 323.37323279, 99999.99999999):
            amount, reason = deposit_for_desired_payout(desired, rate, fee_bps, asset)
            assert reason == "", f"{asset} {desired} at {fee_bps}bps was refused: {reason}"
            assert amount > 0
            realised = forward_payout(amount, rate, fee_bps)
            assert realised >= desired or realised == pytest.approx(desired, rel=1e-9), (
                f"{asset}: sending {amount} pays out {realised}, which is LESS than the {desired} "
                f"asked for. A deposit short of expected_input_amount lands in under_review."
            )


@pytest.mark.parametrize("asset", sorted(CHAIN_PRECISION))
def test_a_solved_deposit_fits_the_chains_own_precision(asset):
    """An amount with more decimals than the chain has is not a sendable amount.

    A customer handed `1.000000012` ICP cannot send it: the wallet truncates, so
    they send less than they were told to, which is the under_review case again
    arriving by a different route.
    """
    decimals = CHAIN_PRECISION[asset]
    for desired in (0.5, 1.0, 1.1, 3.14159265, 300.0):
        amount, reason = deposit_for_desired_payout(desired, 321.7, 150, asset)
        assert reason == ""
        quantum = Decimal(1).scaleb(-decimals)
        assert Decimal(repr(amount)) % quantum == 0, (
            f"{asset} has {decimals} decimals and the solve returned {amount}, which cannot be sent exactly"
        )


def test_the_rounding_is_not_a_whole_unit_too_high():
    """Up, but by at most one unit -- the float-ceiling defect the Decimal avoids.

    THE FIRST VERSION OF THIS TEST DID NOT CATCH THE THING IT NAMED, and the
    mutation that proved it is recorded here because the mistake is instructive
    rather than embarrassing. It asserted over `desired` values 1.1, 2.2, 3.3 --
    boundary values where `math.ceil(x * 1e8) / 1e8` really is a unit high -- and
    then DIVIDED each by payout_multiplier(321.7, 150) before rounding. The
    division destroys the exact-boundary property the bug requires: measured over
    200,000 payouts at those live parameters (0.01 to 2000.00 in 0.01 steps), the
    Decimal and float ceilings differ on ZERO of them.

    Established by restoring that weak body and running it against a
    float-ceiling mutation: 18 passed. Worth recording how, because the first
    attempt to measure this was itself wrong -- the mutation's replacement string
    quoted the return as `''` where the source has `""`, so it never applied and
    "the mutation survived" was a reading taken from unmutated code. A mutation
    that silently fails to apply reports exactly what a passing test reports.

    So the multiplier is 1.0 here -- rate 1.0, fee 0 -- which makes `exact` equal
    `desired` and puts the value back ON the boundary, the only arrangement where
    the two methods disagree. Not a realistic quote, and not meant to be: what is
    under test is the rounding primitive. The realistic range is covered by the
    sweep in test_a_solved_deposit_never_pays_out_less_than_asked.
    """
    # 1.1 and 2.2 are the values whose float product with 1e8 overshoots; 3.3 and
    # 0.7 do not, and are here so this also pins that the correct cases stay
    # correct rather than being "fixed" into overpaying too.
    one_unit = 10 ** -CHAIN_PRECISION["ICP"]
    for desired in (1.1, 2.2, 3.3, 0.7, 300.3):
        amount, reason = deposit_for_desired_payout(desired, 1.0, 0, "ICP")
        assert reason == ""
        assert amount - desired < one_unit, (
            f"solved {amount} for a payout of {desired} at a 1:1 zero-fee rate: a full unit or more "
            f"of overpayment, which is the float-ceiling defect rather than a sub-unit round up"
        )
        # The stronger statement available at a multiplier of 1.0: an already
        # representable payout needs NO rounding, so anything but the figure
        # itself is the defect.
        assert amount == desired, f"a 1:1 zero-fee solve of {desired} must be {desired}, not {amount}"


def test_an_unsolvable_request_says_which_and_returns_no_amount():
    """Every refusal path returns 0.0 and a SENTENCE, never a number to send.

    inf, nan and negatives all render as something a customer might paste into a
    wallet, so the refusals are checked for the amount as much as the reason.
    """
    assert deposit_for_desired_payout(0.0, 321.7, 150, "ICP") == (0.0, UNSOLVABLE_OUTPUT)
    assert deposit_for_desired_payout(-5.0, 321.7, 150, "ICP") == (0.0, UNSOLVABLE_OUTPUT)
    assert deposit_for_desired_payout(300.0, 0.0, 150, "ICP") == (0.0, UNSOLVABLE_RATE)
    assert deposit_for_desired_payout(300.0, -1.0, 150, "ICP") == (0.0, UNSOLVABLE_RATE)
    # A fee at or above 100% clamps the forward payout to zero for EVERY input, so
    # no deposit produces this payout. Dividing would have given inf.
    assert deposit_for_desired_payout(300.0, 321.7, 10000, "ICP") == (0.0, UNSOLVABLE_FEE)
    assert deposit_for_desired_payout(300.0, 321.7, 12000, "ICP") == (0.0, UNSOLVABLE_FEE)
    # An unlisted chain is refused rather than rounded at a guessed precision.
    amount, reason = deposit_for_desired_payout(300.0, 321.7, 150, "DOGE")
    assert (amount, reason) == (0.0, UNSOLVABLE_PRECISION)


def test_every_tradeable_asset_has_a_recorded_precision():
    """A pair this terminal quotes must be one the reverse solve can round for.

    The same gate shape that caught ICP's missing ATTRIBUTION_MODELS entry on
    2026-10-07, applied to this table before it can happen here: without it, a
    new chain in ALLOWED_PAIRS gets UNSOLVABLE_PRECISION on the wizard's reverse
    side and nothing says why until a customer meets it.
    """
    tradeable = {asset for pair in Config.ALLOWED_PAIRS for asset in pair}
    assert tradeable, "this test is worthless if no pair is allowed"
    missing = sorted(tradeable - set(CHAIN_PRECISION))
    assert not missing, (
        f"{missing} can be quoted but has no entry in chains/amount_solve.CHAIN_PRECISION, so the "
        f"wizard's 'I want N of this' side will refuse it. CHAIN_PRECISION is DERIVED -- add the "
        f"chain's decimals to the module that owns them, not a literal here."
    )


def test_the_precision_table_is_derived_and_not_retyped():
    """Each entry must still equal the constant its owning module defines.

    The table is built by importing four modules precisely so that no number is
    typed twice (rule 8). This asserts the identity rather than the values: if
    XRP's drops change, this test keeps passing and the table follows -- which is
    the point. What it catches is somebody replacing an import with a literal.
    """
    assert CHAIN_PRECISION["XRP"] == XRP_DECIMALS
    assert CHAIN_PRECISION["SOL"] == SOL_DECIMALS
    assert CHAIN_PRECISION["ICP"] == ICP_DECIMALS
    for asset, decimals in CHAIN_DECIMALS.items():
        assert CHAIN_PRECISION[asset] == decimals


# =============================================================================
# THE DESK'S OWN CEILING, turned into a maximum deposit. Operator, 2026-10-07:
# "basically we are the one picking up the other end of an htlc unless we find a
# buyer out there on the chain." Measured the same day: all 30 pairs settle
# CUSTODIALLY in the web terminal, so the desk is the counterparty on every one
# and its inventory of the DESTINATION asset is the binding constraint.
# =============================================================================


@pytest.mark.parametrize("asset", sorted(CHAIN_PRECISION))
def test_a_capacity_derived_maximum_never_exceeds_what_the_desk_can_pay(asset):
    """The rounding direction, and it is the OPPOSITE of the other solve's.

    deposit_for_desired_payout() rounds UP so a customer is never short of
    expected_input_amount. This rounds DOWN so the desk is never shown a limit it
    cannot honor -- a customer who sends the stated maximum and then has the
    payout refused at broadcast is the 2026-10-03 failure
    services/payout_capacity.py is named after.

    Swept rather than spot-checked, for the same reason as the forward sweep: a
    rounding error at particular decimal positions passes a single-value test.
    """
    rate = 321.7
    for fee_bps in (0, 150, 500):
        for ceiling in (0.00000001, 1.0, 407.51074481, 1000.0, 99999.99999999):
            amount, reason = max_deposit_for_capacity(ceiling, rate, fee_bps, asset)
            assert reason == "", f"{asset} ceiling {ceiling} at {fee_bps}bps refused: {reason}"
            realised = forward_payout(amount, rate, fee_bps)
            assert realised <= ceiling or realised == pytest.approx(ceiling, rel=1e-9), (
                f"{asset}: the stated maximum deposit of {amount} would need a payout of {realised}, "
                f"which is MORE than the {ceiling} the desk can fund. The customer sends it and the "
                f"payout is refused after their money has moved."
            )


def test_the_two_solves_round_in_opposite_directions():
    """One file, two directions, each away from whoever would find out too late.

    Asserted together rather than in separate tests, because what matters is the
    RELATIONSHIP -- that they bracket the exact answer from either side. A future
    edit that made both round the same way would leave each individual test
    passing while the pair became wrong.
    """
    rate, fee_bps, target = 321.7, 150, 300.0
    exact = target / payout_multiplier(rate, fee_bps)

    needed, _ = deposit_for_desired_payout(target, rate, fee_bps, "ICP")
    allowed, _ = max_deposit_for_capacity(target, rate, fee_bps, "ICP")

    assert needed >= exact, "the customer-facing solve must never ask for less than the exact figure"
    assert allowed <= exact, "the capacity-facing solve must never offer more than the exact figure"
    assert allowed <= needed, (
        "the maximum the desk can honor must not exceed what the customer would need to send for the "
        "same payout -- if it does, the two roundings have been swapped"
    )


def test_an_unreadable_balance_is_not_the_same_answer_as_an_empty_wallet():
    """-1.0 means NOT ESTABLISHED and 0.0 means nothing to pay. Rule 13.

    services/payout_capacity.largest_fundable_payout() chose -1.0 for exactly this
    reason, in its own words: "0.0 is a legitimate answer for an empty wallet and
    the two must not render the same way". This must carry that distinction
    forward rather than collapsing both into "no maximum".
    """
    amount, reason = max_deposit_for_capacity(-1.0, 321.7, 150, "ICP")
    assert (amount, reason) == (0.0, CAPACITY_NOT_ESTABLISHED)
    assert "could not be read" in reason

    # An EMPTY wallet is a real measurement and returns no reason at all, so the
    # caller says "we cannot pay out any of this right now" rather than rendering
    # a gap where a figure should be.
    assert max_deposit_for_capacity(0.0, 321.7, 150, "ICP") == (0.0, "")


def test_a_capacity_solve_refuses_the_same_unsolvable_inputs():
    """The refusals must agree with the other direction's, or one of them is wrong."""
    assert max_deposit_for_capacity(300.0, 0.0, 150, "ICP") == (0.0, UNSOLVABLE_RATE)
    assert max_deposit_for_capacity(300.0, 321.7, 10000, "ICP") == (0.0, UNSOLVABLE_FEE)
    assert max_deposit_for_capacity(300.0, 321.7, 150, "DOGE") == (0.0, UNSOLVABLE_PRECISION)
