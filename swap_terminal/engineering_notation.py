"""The one place a number too small to write plainly becomes readable.

Role: shared leaf function (number formatting for human-readable output)
Reads: nothing
Writes: nothing
Can move funds: no
Mainnet-safe: yes -- pure arithmetic through Decimal, no I/O of any kind

WHY THIS MODULE EXISTS AT ALL.

Operator instruction, 2026-10-09, on seeing the ATM review screen one click
before "Create the swap":

    man, do all "scientific notation" in engineering notation where all powers
    are multiples of 3 since we have a number with a unit which is btc.

What they were looking at was

    You send    8.061e-05 BTC

on the screen where a customer commits to sending that amount. Nobody reads
`8.061e-05` as a quantity of money. The exponent -5 is not a multiple of three,
so it does not even line up with the magnitude words a person has -- milli,
micro, nano -- which is the operator's own reason for the rule: the figure
carries a UNIT, and engineering exponents are the ones units are named at.
`80.61e-6 BTC` is 80.61 micro-BTC, which is a magnitude you can hold in your
head; `8.061e-05 BTC` is a string you have to decode.

WHERE THE DEFECT CAME FROM, measured rather than guessed. Python's float `str()`
switches to scientific notation below 1e-4:

      0.00015229  ->  0.00015229      plain
       0.0001     ->  0.0001          plain, and this is the boundary
       0.00009999 ->  9.999e-05       scientific
       8.061e-05  ->  8.061e-05       scientific  <- what the operator saw
       0.000008   ->  8e-06           scientific

So EVERY BTC amount below 0.0001 BTC rendered as scientific notation, and the
templates print `{{ estimate.send }}` -- a float straight into Jinja, which
calls str(). At the BTC price this desk quotes against, 0.0001 BTC is roughly
ten dollars, so this was not an edge case: it was most small swaps.

WHY NOT Decimal.to_eng_string(), WHICH IS THE DOMAIN API (rule 12). Because it
answers a different question. Its threshold for using an exponent at all is an
adjusted exponent below -6, not Python's -4, so it renders the operator's own
number as plain `0.00008061` and never reaches engineering form:

    Decimal("8.061e-05").to_eng_string()  ->  '0.00008061'
    Decimal("1e-8").to_eng_string()       ->  '10E-9'

That first line would satisfy "no scientific notation" and NOT satisfy the
instruction, which asks for the powers to be multiples of three. It also emits a
capital `E`. Decimal still does the arithmetic here -- the exponent and mantissa
come from `adjusted()` and `scaleb()`, not from log10 on a float -- but the
rendering decision is this module's.

THE FORMATTING RULES, stated absolutely because this is rule 6's lesson applied
to a second notation: a convention that is not absolute drifts at every call
site and nothing fails when it does.

  lowercase `e`, never `E`.  `80.61e-6`, the form engineering notation is
  written in and the form Python floats already print. Decimal's capital E is
  not followed.

  the exponent is ALWAYS a multiple of three.  That is the whole instruction. An
  exponent of -5 or -7 is a defect here the same as a wrong digit would be.

  the mantissa is in [1, 1000).  That follows from the exponent rule and is what
  makes the figure line up with milli/micro/nano.

  no space inside the quantity.  `80.61e-6`, not `80.61 e-6` -- one token, the
  same argument rule 6 makes for `2.3µfn`.

  a space before the UNIT, which is the one difference from rule 6 and is not an
  inconsistency. `80.61e-6 BTC` separates two things a reader parses
  separately: a number written in a notation, and the asset it counts. `2.3µfn`
  is one word because µfn is a suffix on a bare count; BTC is a noun.

WHAT IS NEVER CONVERTED.

  A number that already writes plainly.  0.00015229 stays 0.00015229. The
  instruction is about scientific notation, and restyling figures that were
  always legible would change every screen in the application to fix a defect on
  one of them.

  Counts.  Confirmations, block heights, ledger indexes and satoshis are
  integers, and `6e0` is not an improvement on `6`. Same category error rule 6
  refuses for durations, and there is deliberately no helper here that would let
  you do it to a count by accident.

  Zero.  `0e0` is absurd; zero is "0".
"""

from __future__ import annotations

from decimal import Decimal

#: Below this magnitude, Python's float str() switches to scientific notation and
#: this module takes over. MEASURED, not chosen: see the table in the docstring.
#:
#: IT IS THE SAME BOUNDARY rather than one of my own, so the only figures whose
#: appearance changes are the ones that were already unreadable. A lower
#: threshold would leave some scientific notation in place; a higher one would
#: restyle amounts nobody complained about, which is the large-diff-no-benefit
#: trade rule 12 refuses for a tree-wide lint sweep.
PLAIN_BELOW = Decimal("0.0001")

#: And above this, for the same reason in the other direction: float str() goes
#: scientific at 1e16. No amount in this application is near it -- the largest
#: thing priced is a GRC payout -- but a formatter whose contract is "never
#: scientific notation" must not have a hole at the top.
PLAIN_AT_OR_ABOVE = Decimal("1e16")

#: The exponent step. Three is the instruction, and it is a constant rather than
#: a literal in the arithmetic because the word "engineering" means exactly this
#: number and a reader should be able to find it.
EXPONENT_STEP = 3


def needs_engineering(value: object) -> bool:
    """Would this print as scientific notation if handed straight to str()?

    ASKED OF Decimal, NOT OF THE RENDERED STRING. Checking `"e" in str(value)`
    would be the same mistake serving_verdict() was fixed for on this branch
    today: reading a rendered form to recover a fact the value already carries.
    It would also be wrong for a Decimal, whose str() uses an exponent at a
    different threshold than float's.
    """
    number = _decimal_of(value)
    if number is None or number == 0:
        return False
    magnitude = abs(number)
    return magnitude < PLAIN_BELOW or magnitude >= PLAIN_AT_OR_ABOVE


def engineering_notation(value: object) -> str:
    """`80.61e-6` -- the mantissa in [1, 1000) and the exponent a multiple of 3.

    Returns the PLAIN decimal form for anything that writes plainly, so this is
    safe to call on every amount rather than only on small ones. A caller that
    had to decide first would be a second copy of needs_engineering(), which is
    rule 8's two-copies-of-one-rule.

    Anything that is not a number comes back as str(value) untouched. A
    formatter is not a validator: the one place that would matter is a template
    rendering a refusal sentence where a figure was expected, and replacing that
    sentence with "0" would hide it (rule 14).
    """
    number = _decimal_of(value)
    if number is None:
        return str(value)
    if not needs_engineering(number):
        # str() on a Decimal built from a float's repr gives the plain form for
        # everything in this range, and does NOT reintroduce an exponent.
        return _plain(number)
    # adjusted() is floor(log10(|n|)) without touching a float. Flooring the
    # division to a multiple of three is what Python's `//` already does for
    # negatives -- -5 // 3 == -2, so -5 becomes -6 and not -3. Rounding toward
    # zero here would give an exponent of -3 and a mantissa of 0.08061, which is
    # outside [1, 1000) and is the bug this line is written carefully to avoid.
    exponent = (number.adjusted() // EXPONENT_STEP) * EXPONENT_STEP
    mantissa = number.scaleb(-exponent)
    return f"{_plain(mantissa)}e{exponent}"


def coin_amount_text(value: object, asset: str = "") -> str:
    """An amount with its unit: `80.61e-6 BTC`. The form a screen prints.

    Its own function so that templates render ONE expression and the space-before
    -the-unit rule lives here rather than in each `.html`. Three templates print
    these amounts today and a fourth that spelled `{{ x }} {{ asset }}` by hand
    would be the drift this module exists to prevent.

    An empty `asset` gives the bare number, for the callers that print the unit
    in a column header instead of beside the figure.
    """
    number = _decimal_of(value)
    if number is None:
        # NO UNIT ON A NON-NUMBER. `coin_amount_text(None, "BTC")` returning
        # "None BTC" puts a unit on a word, and "priced when you confirm BTC"
        # reads as a figure that failed to render rather than as the sentence it
        # is. The value comes back untouched so a caller's refusal sentence
        # survives (rule 14: never replace a reason with a zero), and anything
        # genuinely broken still LOOKS broken instead of looking like an amount.
        return str(value)
    return f"{engineering_notation(number)} {asset}" if asset else engineering_notation(number)


def _decimal_of(value: object) -> Decimal | None:
    """`value` as a Decimal, or None if it is not a number.

    Decimal(str(value)) for a float, NEVER Decimal(value), and the reason is the
    one coin_amounts.amount_to_base_units() already states: the second converts
    the float's exact binary value, so Decimal(0.1) is
    0.1000000000000000055511151231257827, and formatting THAT prints seventeen
    digits of noise under a figure a customer is checking.

    bool is refused ahead of int because `True` is an int in Python and
    `1e0 True` is not a thing anybody wants on a screen.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, Decimal):
        return value
    if isinstance(value, (int, float)):
        try:
            return Decimal(str(value))
        except (ValueError, ArithmeticError):
            # nan and inf parse as Decimal and are not amounts. A payout screen
            # printing "NaN BTC" would be worse than printing the raw repr,
            # which at least looks broken rather than looking like a number.
            return None
    return None


def _plain(number: Decimal) -> str:
    """A Decimal as digits only -- no exponent, no trailing zeros, no "-0".

    `f"{number:f}"` is the fixed-point format, which is the part that guarantees
    no exponent whatever the Decimal's internal exponent is. The trailing-zero
    strip is cosmetic and bounded: it only runs when a decimal point is present,
    so "1000" does not become "1".
    """
    text = f"{number:f}"
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"
