"""Solve the deposit amount a customer must send to RECEIVE a chosen payout.

Role: submodule (decisions only -- pure arithmetic, no socket, no database)
Reads: the per-chain precision constants, imported from the modules that own them
Writes: nothing
Can move funds: no. It returns a number; nothing here signs or sends.
Mainnet-safe: yes to import and yes to call.

WHY THIS EXISTS. The ATM wizard lets a customer type EITHER side -- "I am sending
1 ICP" or "I want 300 GRC" -- and the second needs the inverse of the pricing
services/quote_service.py performs forward.

THE INVERSE IS EXACT AND CLOSED-FORM, which is worth stating because it was not
obviously going to be. The forward calculation is, in full:

    gross_output = input_amount * rate                       quote_service.py:582
    output       = max(gross_output * (1 - fee_bps / 10000), 0)          :615

so

    input = output / (rate * (1 - fee_bps / 10000))

and there is NO circularity, because `network_fee_reserve` is not subtracted from
the payout. That changed on 2026-10-03 and quote_service.py:577 says so in as many
words -- "the payout is a function of the rate and the fee alone and can be
computed first". The reserve is a cost the desk carries out of its own fee, and
show_fees.py reports it that way. Had the reserve still been deducted, this
inverse would have needed the reserve, the reserve would have needed the payout
size, and the payout size is what we are solving for -- a fixed point requiring
iteration, with a rounding decision on every step. It does not, so this is one
division.

ROUNDING GOES UP, AND THAT IS THE ONE DECISION IN THIS FILE.

Config.AMOUNT_TOLERANCE_PCT is 0.01, and services/deposit_service.py sends a
deposit outside +/-1% of `expected_input_amount` to `under_review` rather than
crediting it. So the direction of a sub-unit rounding is not cosmetic: round the
solved input DOWN and the customer sends very slightly less than the swap row
expects, which on a thin tolerance is a deposit that stops and waits for an
operator instead of paying out. Round UP and they send a hair more, the deposit
credits, and the surplus is the desk's by the same arithmetic that already governs
an over-payment inside tolerance.

TWO DIRECTIONS LIVE IN THIS FILE AND THEY MUST NOT BE SWAPPED.
max_deposit_for_capacity() at the bottom rounds DOWN, for the mirror-image reason:
it turns the desk's payout ceiling into a maximum deposit, and rounding that UP
would show a customer a limit the desk cannot actually pay -- they send it and the
payout is refused at broadcast, which is the 2026-10-03 failure
services/payout_capacity.py is named after. Each function rounds AWAY FROM the
party who would otherwise discover the shortfall after the money moved: the
customer here, the desk there.

That is the opposite of chains/payout_quantization.quantize_for_chain(), which is
documented as NEVER GREATER than its input and is measured so over 60,015 amounts
per chain. The difference is the direction of the risk, and it belongs in a
comment at both sites (rule 8): that function quantizes what the DESK SENDS, where
rounding up would spend money the desk did not quote; this solves what the
CUSTOMER SENDS, where rounding down strands their deposit in review. Neither may
borrow the other's direction.

NO FIFTH PRECISION TABLE. Four modules already own these numbers and a hand-typed
copy here would be rule 8's failure with a delay on it -- the copies agree on the
day they are written. CHAIN_PRECISION below is DERIVED by importing each owner, so
a chain whose precision changes changes here automatically and a chain nobody
listed is absent rather than guessed.
"""

from __future__ import annotations

from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal

from .coin_amounts import CHAIN_DECIMALS
from .icp_account import ICP_DECIMALS
from .solana_units import SOL_DECIMALS
from .xrp_units import XRP_DECIMALS

#: Decimal places each chain can actually represent, derived from the modules that
#: own the figure. Nothing here is typed as a literal -- see the module docstring.
#:
#: BTC/LTC/GRC come in as a mapping rather than three lines for the same reason:
#: coin_amounts.CHAIN_DECIMALS is the table, and spreading it means a fourth
#: bitcoin-family chain added there arrives here with no edit.
CHAIN_PRECISION: dict[str, int] = {
    **CHAIN_DECIMALS,
    "XRP": XRP_DECIMALS,
    "SOL": SOL_DECIMALS,
    "ICP": ICP_DECIMALS,
}

#: What a solve could not answer, as the reason to show the customer. Each is a
#: SENTENCE rather than a code, because it is rendered (rule 14: state what the
#: number means, next to the number).
UNSOLVABLE_RATE = "no usable rate is available for this pair right now"
UNSOLVABLE_FEE = "the configured fee leaves nothing to pay out, so no deposit size can produce this payout"
UNSOLVABLE_OUTPUT = "the payout you asked for is not a positive amount"
UNSOLVABLE_PRECISION = "this chain's precision is not recorded, so the deposit amount cannot be rounded safely"


def payout_multiplier(rate: float, fee_bps: int) -> float:
    """`rate * (1 - fee_bps/10000)` -- the single factor the forward price applies.

    Its own function because BOTH directions need it and spelling it twice is how
    a fee change lands on one side only. quote_service.py applies it forward as
    two statements; this is the same product, and the test asserts they agree
    rather than trusting that they do.
    """
    return rate * (1.0 - fee_bps / 10000.0)


def deposit_for_desired_payout(
    desired_output: float, rate: float, fee_bps: int, from_asset: str
) -> tuple[float, str]:
    """How much `from_asset` to send to receive AT LEAST `desired_output`.

    Returns (amount, "") on success, or (0.0, reason) when no amount can.

    ROUNDED UP to `from_asset`'s own precision, for the AMOUNT_TOLERANCE_PCT
    reason in the module docstring. The caller must present the result as a
    MINIMUM -- "send 1.00000001 ICP to receive at least 300 GRC" -- because the
    rounding means the realised payout is a hair above what was asked, never
    below.

    Decimal, not float, for the rounding step alone. The division stays float
    because `rate` is a float from the price feed and converting it to Decimal
    would imply a precision the feed does not have.

    WHY THE CEILING IS Decimal, STATED AS WHAT WAS MEASURED RATHER THAN AS THE
    STRONGER CLAIM I FIRST WROTE HERE. `math.ceil(x * 10**8) / 10**8` is wrong at
    a decimal boundary: 1.1 * 10**8 is 110000000.00000001 in binary floating
    point, so it ceils to 1.10000001 -- a FULL UNIT too high. 2.2 does the same.
    That much is real and reproducible.

    What is NOT true is that it would have cost anything at live parameters, and
    the first version of this comment implied it would. Measured 2026-10-07 over
    200,000 payouts (0.01 to 2000.00 in 0.01 steps) at rate 321.7 and 150bps:
    the Decimal ceiling and the float ceiling differ on ZERO of them. The reason
    is structural -- dividing by `payout_multiplier()` produces a value whose
    float form essentially never lands on an eight-decimal boundary, and the bug
    needs it to land there. A multiplier of exactly 1.0 (rate 1.0, fee 0) does
    land, which is how tests/test_amount_solve.py reaches the case at all.

    So Decimal here is correct-by-construction rather than a fix for an observed
    overcharge, and that distinction is the difference between a measurement and a
    hypothesis (rule 17). It stays Decimal because the cost is nothing and the
    failure it rules out is a systematic overcharge at whatever future rate and
    fee DO divide evenly -- a fee of 0bps and a 1:1 pair is not an absurd future
    configuration.
    """
    if desired_output <= 0:
        return 0.0, UNSOLVABLE_OUTPUT
    if rate <= 0:
        return 0.0, UNSOLVABLE_RATE
    decimals = CHAIN_PRECISION.get(from_asset)
    if decimals is None:
        return 0.0, UNSOLVABLE_PRECISION
    multiplier = payout_multiplier(rate, fee_bps)
    if multiplier <= 0:
        # A fee at or above 100% clamps the forward payout to 0 for every input,
        # so there is no deposit that produces a positive payout. Reported rather
        # than divided by, because dividing gives inf or a negative and both
        # render as a number a customer might try to send.
        return 0.0, UNSOLVABLE_FEE
    exact = desired_output / multiplier
    step = Decimal(1).scaleb(-decimals)
    rounded = Decimal(repr(exact)).quantize(step, rounding=ROUND_CEILING)
    return float(rounded), ""


#: Why no maximum deposit could be stated. Distinguished from a maximum of ZERO,
#: for the reason services/payout_capacity.largest_fundable_payout() gives about
#: its own -1.0 sentinel: an empty wallet is a legitimate answer and "we could not
#: read the wallet" is not, and the two must not render the same way (rule 13).
CAPACITY_NOT_ESTABLISHED = "the desk's payout balance could not be read, so no maximum can be stated"


def max_deposit_for_capacity(
    payout_ceiling: float, rate: float, fee_bps: int, from_asset: str
) -> tuple[float, str]:
    """The most `from_asset` worth sending, given the biggest payout the desk can fund.

    Returns (amount, "") or (0.0, reason). A ceiling of 0.0 is a real answer and
    returns (0.0, "") -- the desk can fund nothing, which the caller must say out
    loud rather than treating as a failure to measure.

    WHY THIS EXISTS, and it is the operator's own framing of the business made
    into a number. 2026-10-07: "basically we are the one picking up the other end
    of an htlc unless we find a buyer out there on the chain." Measured the same
    day: all 30 pairs in Config.ALLOWED_PAIRS settle CUSTODIALLY in the web
    terminal -- every settlement headline ends "this terminal settles it
    CUSTODIALLY, with no hashlock", including the three whose atomic path has run
    green. So the desk is the counterparty on every pair, and the binding
    constraint on any swap is the desk's own inventory of the DESTINATION asset.

    THE DEAD END THIS REMOVES. services/swap_service.create_swap() refuses an
    unfundable payout through payout_capacity.why_the_payout_cannot_be_funded(),
    and services/quote_service.py has no capacity check at all -- measured by
    grepping both for capacity/get_balance/fundable. So a customer could be quoted
    any size, answer every question, and be refused at the confirm screen. An ATM
    states its limit before you type, which is the whole reason the operator asked
    for this shape.

    ROUNDING GOES DOWN HERE, WHICH IS THE OPPOSITE OF
    deposit_for_desired_payout() TWENTY LINES UP, AND THE DIRECTIONS MUST NOT BE
    SWAPPED (rule 8: the difference is the point, and it is named at both sites).

        deposit_for_desired_payout   UP    the customer must not send LESS than
                                           the swap expects, or AMOUNT_TOLERANCE_PCT
                                           sends their deposit to under_review
        max_deposit_for_capacity     DOWN  the desk must not be shown a maximum it
                                           cannot actually pay, or the customer
                                           sends it and the payout is refused at
                                           broadcast -- which is the 2026-10-03
                                           failure payout_capacity is named after

    One rounds toward the customer's safety and one toward the desk's solvency,
    and in both cases the direction is away from the party who would otherwise
    discover the shortfall after the money moved.
    """
    if payout_ceiling < 0:
        # largest_fundable_payout()'s -1.0 sentinel: NOT ESTABLISHED, not zero.
        return 0.0, CAPACITY_NOT_ESTABLISHED
    if rate <= 0:
        return 0.0, UNSOLVABLE_RATE
    decimals = CHAIN_PRECISION.get(from_asset)
    if decimals is None:
        return 0.0, UNSOLVABLE_PRECISION
    multiplier = payout_multiplier(rate, fee_bps)
    if multiplier <= 0:
        return 0.0, UNSOLVABLE_FEE
    if payout_ceiling == 0:
        # A REAL ANSWER AND NOT A REFUSAL. The desk holds nothing of the
        # destination asset, so the maximum deposit is zero -- and the caller has
        # to render that as "we cannot pay out any of this right now" rather than
        # as a missing figure.
        return 0.0, ""
    exact = payout_ceiling / multiplier
    step = Decimal(1).scaleb(-decimals)
    return float(Decimal(repr(exact)).quantize(step, rounding=ROUND_FLOOR)), ""
