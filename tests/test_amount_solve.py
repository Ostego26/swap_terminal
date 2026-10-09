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

from decimal import ROUND_FLOOR, Decimal
from pathlib import Path

import pytest
from chains.amount_solve import (
    CAPACITY_NOT_ESTABLISHED,
    CHAIN_PRECISION,
    UNSOLVABLE_DEPOSIT,
    UNSOLVABLE_FEE,
    UNSOLVABLE_OUTPUT,
    UNSOLVABLE_PRECISION,
    UNSOLVABLE_RATE,
    deposit_for_desired_payout,
    max_deposit_for_capacity,
    payout_for_deposit,
    payout_multiplier,
    quantize_down,
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


# ===========================================================================
# THE FORWARD DIRECTION, added 2026-10-09 with chains/amount_solve.
# payout_for_deposit().
#
# The file above exists because an inverse that disagrees with the forward is a
# silent defect. The forward now has a function of its own -- so that the ATM's
# address and review screens can state what a deposit buys without a database --
# and it inherits the identical hazard: a third expression for one relationship.
#
# `forward_payout` at the top of this file is quote_service.py's two statements
# copied verbatim, and test_the_forward_copy_matches_quote_service_source reads
# the real source to keep the copy honest. So these tests round-trip the new
# function through THAT, which makes the chain complete: quote_service's source
# -> the copy -> payout_for_deposit(). A fee change that lands on one of the
# three fails here.
# ===========================================================================


@pytest.mark.parametrize("asset", sorted(CHAIN_PRECISION))
def test_the_forward_solver_is_the_forward_expression_quantized(asset):
    """payout_for_deposit() must be forward_payout(), cut to the chain's precision.

    Swept over every chain this system knows and over the live fee alongside two
    extremes, because the one thing this function adds to the expression is a
    rounding step and a rounding step is only wrong at the boundary.
    """
    for rate in (0.0005, 1.0, 321.7, 62000.0):
        for fee_bps in (0, int(Config.DEFAULT_FEE_BPS), 9999):
            for deposit in (0.001, 1.0, 7.77777777, 1234.5):
                got, refusal = payout_for_deposit(deposit, rate, fee_bps, asset)
                assert refusal == ""
                exact = Decimal(repr(forward_payout(deposit, rate, fee_bps)))
                step = Decimal(1).scaleb(-CHAIN_PRECISION[asset])
                assert Decimal(repr(got)) == exact.quantize(step, rounding=ROUND_FLOOR), (
                    f"{asset} at rate {rate} fee {fee_bps} on {deposit}: the forward solver and "
                    f"quote_service's own expression disagree"
                )


@pytest.mark.parametrize("asset", sorted(CHAIN_PRECISION))
def test_a_displayed_payout_is_never_more_than_the_price_actually_gives(asset):
    """The rounding direction, which is the only decision in the function.

    DOWN, and the module docstring's table says why: this figure is PRINTED
    before a customer decides, so rounding it up is a terminal promising a
    fraction more than the quote will deliver. The other two solvers in that file
    round the other way for reasons about money that has already moved; swapping
    this one would look identical in every other test.

    Swept rather than spot-checked, over values chosen to land on and off the
    chain's own precision boundary -- a ceiling and a floor agree everywhere
    except there, which is exactly where a swapped mode would hide.
    """
    step = 10.0 ** -CHAIN_PRECISION[asset]
    for deposit in (1.0, 1.0 + step, 3.0 - step / 2, 999.999999):
        got, refusal = payout_for_deposit(deposit, 321.7, int(Config.DEFAULT_FEE_BPS), asset)
        assert refusal == ""
        assert got <= forward_payout(deposit, 321.7, int(Config.DEFAULT_FEE_BPS)) + 1e-12, (
            f"{asset}: the printed payout {got} is ABOVE what the price gives for {deposit}, so the "
            "screen is promising more than the quote will deliver"
        )


def test_the_two_directions_round_trip_without_ever_shorting_the_customer():
    """Solve a deposit for a payout, price that deposit, and the payout is not less.

    THE PROPERTY THAT TIES THE FILE TOGETHER, and it is what the ATM's review
    screen now relies on: services/wizard.both_sides() shows the solved deposit
    AND what that deposit buys, so if these two functions disagreed about
    direction the screen would quote a customer less than they asked for.

    Both roundings have to be right for this to hold -- the deposit UP and the
    payout DOWN -- so a swap of either mode breaks it.
    """
    for asset_pair in (("ICP", "GRC"), ("BTC", "GRC"), ("GRC", "BTC"), ("XRP", "SOL")):
        source, destination = asset_pair
        for wanted in (0.5, 300.0, 1234.56):
            deposit, refusal = deposit_for_desired_payout(wanted, 321.7, 150, source)
            assert refusal == ""
            delivered, refusal = payout_for_deposit(deposit, 321.7, 150, destination)
            assert refusal == ""
            assert delivered >= wanted, (
                f"{source}->{destination}: asked for {wanted}, the solved deposit {deposit} prices to "
                f"{delivered} -- a customer would be shown less than they typed"
            )


def test_the_forward_solver_refuses_the_same_unsolvable_inputs():
    """Its refusals must line up with the inverse's, or one of them is wrong.

    A ZERO DEPOSIT HAS ITS OWN SENTENCE and does not share UNSOLVABLE_OUTPUT.
    The two name different boxes on the screen -- one is what you send and the
    other is what you want back -- and a customer sent to re-read the wrong field
    is rule 14's failure on the one screen that takes a number.
    """
    assert payout_for_deposit(0.0, 321.7, 150, "GRC") == (0.0, UNSOLVABLE_DEPOSIT)
    assert payout_for_deposit(-1.0, 321.7, 150, "GRC") == (0.0, UNSOLVABLE_DEPOSIT)
    assert UNSOLVABLE_DEPOSIT != UNSOLVABLE_OUTPUT, (
        "the two directions' 'not a positive amount' sentences have become one, so a customer who "
        "typed a bad SEND figure is now told about the RECEIVE box"
    )
    assert payout_for_deposit(1.0, 0.0, 150, "GRC") == (0.0, UNSOLVABLE_RATE)
    assert payout_for_deposit(1.0, 321.7, 10000, "GRC") == (0.0, UNSOLVABLE_FEE)
    assert payout_for_deposit(1.0, 321.7, 150, "DOGE") == (0.0, UNSOLVABLE_PRECISION)


def test_a_desk_figure_is_cut_to_the_chains_precision_before_it_is_printed():
    """The 407.50974481000003 case, which is the measurement that added this.

    MEASURED on the real amount screen 2026-10-09 with a GRC desk balance of
    407.51074481 and a 0.001 reserve: services/payout_capacity.
    largest_fundable_payout() subtracts them in float and the ceiling line
    rendered

        Most you can send: 0.00020685 BTC -- because that is all this desk can pay
        out on the other side (407.50974481000003 GRC).

    Seventeen significant digits, five of which describe nothing that exists on
    any chain. The value is not wrong; its PRINTED form says this terminal cannot
    count.
    """
    assert quantize_down(407.51074481 - 0.001, "GRC") == (407.50974481, "")

    # DOWN, like payout_for_deposit() and for the same reason: this is the desk's
    # own capacity, and a displayed maximum rounded UP is one the desk cannot pay.
    cut, refusal = quantize_down(1.123456789, "GRC")
    assert (cut, refusal) == (1.12345678, "")
    assert cut < 1.123456789

    # An exact value is unchanged, so this cannot be a function that quietly
    # perturbs every figure it is handed.
    assert quantize_down(407.5, "GRC") == (407.5, "")
    assert quantize_down(0.0, "GRC") == (0.0, "")


def test_an_unknown_chain_is_reported_rather_than_silently_unquantized():
    """A passthrough that says nothing is indistinguishable from nothing to do.

    This returns the figure AS GIVEN plus the reason, so a caller that started
    printing raw floats for a new chain can find out why. Rule 13: did-nothing
    must not look like did-work.
    """
    value, refusal = quantize_down(1.23456789012, "DOGE")
    assert value == 1.23456789012, "an unknown chain's figure must not be altered"
    assert refusal == UNSOLVABLE_PRECISION, "and the caller must be told it was not quantized"
