"""Both sides of one trade at one rate: what to send for a payout, what a deposit buys.

The INVERSE is what this file was built for (deposit_for_desired_payout); the
FORWARD arrived 2026-10-09 (payout_for_deposit) when the ATM's amount screen had
to show a customer what they would get before any quote row exists. The header
below still explains the inverse at length because it is the one with the
interesting derivation; the forward is one multiplication and its only decision
is the rounding mode, which the table further down names.

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

FOUR ROUNDING DECISIONS LIVE IN THIS FILE AND THEY MUST NOT BE SWAPPED. This
paragraph named TWO until 2026-10-09 and the list is kept current rather than
left to be rediscovered, because its whole purpose is that a reader checks which
function they are in before changing a rounding mode.

    deposit_for_desired_payout   UP    what the CUSTOMER SENDS
    max_deposit_for_capacity     DOWN  the limit the DESK can honor
    payout_for_deposit           DOWN  what a screen PROMISES (display only)
    quantize_down                DOWN  any desk figure on its way to a screen

Only the first rounds up, and it is the only one of the four where the figure is
a number the customer puts into a wallet.

max_deposit_for_capacity() rounds DOWN, for the mirror-image reason:
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
#: The forward direction's mirror of UNSOLVABLE_OUTPUT. Its own sentence rather
#: than a shared "that is not a positive amount", because the two name different
#: fields on the screen and a customer who typed one must not be sent to read the
#: other (rule 14: say what the number means, next to the number).
UNSOLVABLE_DEPOSIT = "the amount you asked to send is not a positive amount"
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


def payout_for_deposit(
    deposit: float, rate: float, fee_bps: int, to_asset: str
) -> tuple[float, str]:
    """How much `to_asset` a deposit of `deposit` buys. The FORWARD of the function above.

    Returns (amount, "") or (0.0, reason).

    WHY IT IS HERE AND NOT IN quote_service.py, WHICH ALREADY DOES THIS SUM.
    create_quote() computes the same product as two statements inside a function
    that needs a database handle, an adapters dict and a live price feed -- so the
    ONE screen that wants to show a customer what they would get, before anything
    is written, could not call it. The alternative was a third spelling of
    `input * rate * (1 - fee/10000)` in a route or a template, which is rule 8's
    subject: this product already existed twice (quote_service.py:582+615 and
    payout_multiplier() above) and a third copy in a Jinja file would be the one
    nothing type-checks.

    IT EVALUATES quote_service's TWO STATEMENTS IN quote_service's ORDER, AND THE
    FIRST VERSION USED payout_multiplier() AND WAS MEASURABLY DIFFERENT.

    `(deposit * rate) * (1 - fee)` and `deposit * (rate * (1 - fee))` are the same
    number in arithmetic and not in binary floating point. Caught by
    test_the_forward_solver_is_the_forward_expression_quantized on its first run,
    at rate 321.7, 150bps, 1234.5 XRP:

        quote_service's order   391181.57024999993  ->  391181.570249
        payout_multiplier()     391181.57025        ->  391181.570250

    -- one unit in the sixth decimal, which is nothing as money and is everything
    for what this function is for. The figure a customer reads on the review has
    to be the figure `output_amount_estimate` carries on the swap row created one
    click later, or the two disagree in the last digit and the only person who can
    reconcile them is whoever wrote both. payout_multiplier() stays the authority
    for the FACTOR -- the inverse divides by it and the sign test below uses it --
    and the forward reproduces the product the way the quote computes it.

    THAT MAKES THIS A COPY, AND THE COPY IS GUARDED RATHER THAN TRUSTED (rule 8).
    tests/test_amount_solve.py holds `forward_payout`, which is quote_service.py's
    two statements copied verbatim, AND
    test_the_forward_copy_matches_quote_service_source, which reads the real
    source and asserts the expression is still spelled that way. Every assertion
    about this function round-trips through that copy, so the chain is
    quote_service's source -> the checked copy -> here, and a fee change that
    lands on one of the three fails the suite. The alternative -- having
    create_quote() call this -- is a change to the function that prices live
    swaps, which is rule 16's line and the operator's call, not mine.

    AN ESTIMATE, AND THE CALLER MUST SAY SO. `rate` here is whatever the caller
    read from the price feed a moment ago; the rate a swap is actually priced at
    is fixed by create_quote() at confirm time, from its own read. Showing this
    figure without the word "estimate" beside it would be a price this terminal
    has not committed to -- see templates/_atm_costs.html, which prints it with
    the quote window next to it for exactly that reason.

    ROUNDING GOES DOWN, WHICH IS THE THIRD DIRECTION IN THIS FILE AND THE THIRD
    JUSTIFICATION. The two above round away from whoever would otherwise find out
    after the money moved; this one is not a figure anyone sends, it is a figure
    the customer is shown BEFORE they decide. So it rounds away from the desk's
    mouth: a displayed payout that rounds UP is a terminal promising a fraction
    more than the quote will deliver, and the customer who notices has been given
    a reason to distrust every other number on the page. Down is the direction in
    which being wrong costs nothing.

    This is also the direction chains/payout_quantization.quantize_for_chain()
    takes for what the desk SENDS, and the agreement is not a coincidence -- both
    are the desk's own side of a figure. The two still do not share an
    implementation, because that one quantizes an amount about to be broadcast
    and is measured over 60,015 amounts per chain; this one rounds a display.

    A ZERO IS A REAL ANSWER and returns (0.0, ""): a deposit so small that the
    destination chain cannot represent any of it after the fee really does buy
    nothing, and the screen must be able to say that rather than report it as a
    failure to measure (rule 13).
    """
    if deposit <= 0:
        return 0.0, UNSOLVABLE_DEPOSIT
    if rate <= 0:
        return 0.0, UNSOLVABLE_RATE
    decimals = CHAIN_PRECISION.get(to_asset)
    if decimals is None:
        return 0.0, UNSOLVABLE_PRECISION
    if payout_multiplier(rate, fee_bps) <= 0:
        # A fee at or above 100% pays out nothing at any size. Reported rather
        # than rendered as 0.0, because "you receive 0" and "the configured fee
        # leaves nothing to pay out" are different facts and only the second one
        # tells an operator what to change.
        #
        # THE MULTIPLIER IS USED HERE AS A SIGN TEST AND NOT AS A FACTOR. The
        # product below is quote_service's two statements in quote_service's
        # order, for the floating-point reason the docstring measures; whether
        # the fee leaves anything at all is a question about the fee, and
        # payout_multiplier() is the one place that expression lives.
        return 0.0, UNSOLVABLE_FEE
    # quote_service.py:582 and :615, in that order and with its clamp. Not
    # `deposit * payout_multiplier(...)`: see the docstring for the measurement.
    gross_output = deposit * rate
    exact = max(gross_output * (1 - fee_bps / 10000.0), 0.0)
    step = Decimal(1).scaleb(-decimals)
    rounded = Decimal(repr(exact)).quantize(step, rounding=ROUND_FLOOR)
    return float(rounded), ""


def quantize_down(amount: float, asset: str) -> tuple[float, str]:
    """`amount` cut to what `asset` can represent, never upward. For DISPLAY.

    WHY A SCREEN NEEDS THIS AT ALL, measured 2026-10-09 on the real amount step
    with the stub desk balances: the step-3 ceiling line rendered

        (407.50974481000003 GRC)

    -- seventeen significant digits of binary floating point, out of
    `407.51074481 - 0.001` in services/payout_capacity.largest_fundable_payout().
    GRC has eight decimals, so five of those digits describe nothing that exists
    on the chain. A customer reading it learns that this terminal does not know
    how much it holds, which is the opposite of what the line is for (rule 14:
    state what the number means, next to the number).

    DOWN, for payout_for_deposit()'s reason: this is the desk's own figure, and a
    displayed capacity rounded UP is a maximum the desk cannot actually pay.

    NOT A SUBSTITUTE FOR chains/payout_quantization.quantize_for_chain(). That one
    governs what is BROADCAST and carries the measurement to prove it; this one
    governs what is PRINTED. Named at both sites (rule 8) so neither is reached
    for by mistake: an amount on its way to a chain goes through that module, and
    nothing on the order path may call this.
    """
    decimals = CHAIN_PRECISION.get(asset)
    if decimals is None:
        # NOT a silent passthrough. An unknown asset's figure is printed as it
        # came, and the caller is told -- a screen that quietly stopped
        # quantizing would look exactly like one that had nothing to quantize.
        return float(amount), UNSOLVABLE_PRECISION
    step = Decimal(1).scaleb(-decimals)
    return float(Decimal(repr(float(amount))).quantize(step, rounding=ROUND_FLOOR)), ""


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
