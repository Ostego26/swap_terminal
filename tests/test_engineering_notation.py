"""Engineering notation: exponents in multiples of three, on a figure with a unit.

Operator instruction, 2026-10-09, looking at the ATM review screen one click
before "Create the swap":

    man, do all "scientific notation" in engineering notation where all powers
    are multiples of 3 since we have a number with a unit which is btc.

The screen read `You send  8.061e-05 BTC`. These pin the convention and the one
place it is implemented, the way tests/test_microfortnights.py pins rule 6's.
"""

from __future__ import annotations

import re
from decimal import Decimal

import pytest

# tests/conftest.py already puts swap_terminal/ on sys.path, so there is no
# insert here and no `noqa: E402` either -- RUF100 reported that marker as
# unused, which is how the redundant insert was found (rule 19: a noqa for a
# finding that never fires is a claim nobody checked).
from engineering_notation import (
    EXPONENT_STEP,
    PLAIN_AT_OR_ABOVE,
    PLAIN_BELOW,
    coin_amount_text,
    engineering_notation,
    needs_engineering,
)

#: The figure off the operator's own screen, and what it must become.
_THE_DEFECT = 8.061e-05
_THE_FIX = "80.61e-6"

#: A spread of magnitudes either side of the boundary. Built rather than listed
#: so the sweep below cannot quietly stop covering the exponents it was written
#: for -- a hand-written list is the thing that rots (rule 3's denominator).
_SWEEP = [
    float(f"{mantissa}e-{exponent}")
    for mantissa in (1, 3, 8, 99, 123, 8061, 999999)
    for exponent in range(0, 16)
]


def _exponent_of(shown: str) -> int | None:
    """The exponent in a rendered figure, or None if it has none."""
    match = re.search(r"e(-?\d+)$", shown)
    return int(match.group(1)) if match else None


def test_the_operators_own_figure():
    """The one that prompted the rule, asserted on its own."""
    assert engineering_notation(_THE_DEFECT) == _THE_FIX
    assert coin_amount_text(_THE_DEFECT, "BTC") == f"{_THE_FIX} BTC"
    assert "e-05" not in coin_amount_text(_THE_DEFECT, "BTC")


def test_every_exponent_is_a_multiple_of_three():
    """THE INSTRUCTION, over a sweep rather than over examples I chose.

    MUTATION: `number.adjusted()` without the floor-to-step, which gives the
    plain scientific exponent -- and -5 is what the operator objected to.
    """
    for value in _SWEEP:
        shown = engineering_notation(value)
        exponent = _exponent_of(shown)
        if exponent is not None:
            assert exponent % EXPONENT_STEP == 0, (
                f"{value!r} rendered as {shown}, whose exponent {exponent} is not a multiple "
                f"of {EXPONENT_STEP}"
            )


def test_the_mantissa_stays_in_one_to_a_thousand():
    """What makes the figure line up with milli/micro/nano.

    MUTATION: round the exponent toward zero instead of flooring it. `-5 // 3`
    is -2 in Python, so -5 becomes -6 and the mantissa is 80.61; truncating to
    -3 would give a mantissa of 0.08061, which is outside the range and reads as
    a smaller number than it is.
    """
    for value in _SWEEP:
        shown = engineering_notation(value)
        if _exponent_of(shown) is None:
            continue
        mantissa = abs(float(shown.split("e")[0]))
        assert 1 <= mantissa < 1000, f"{value!r} rendered as {shown}, mantissa {mantissa}"


def test_no_rendered_amount_is_ever_in_scientific_notation():
    """The contract, stated as the absence it is.

    str() is what the templates call, and it is what produced the defect. If any
    of these comes back with a non-engineering exponent or a capital E, the
    formatter has a hole in it.
    """
    for value in _SWEEP:
        shown = engineering_notation(value)
        assert "E" not in shown, f"capital E in {shown} -- Decimal's form, not this module's"
        exponent = _exponent_of(shown)
        assert exponent is None or exponent % EXPONENT_STEP == 0, shown
        # And nothing may survive with Python's own exponent spelling in it.
        assert not re.search(r"e[-+]0\d", shown), (
            f"{shown} carries a zero-padded exponent, which is float str()'s form"
        )


def test_a_figure_that_already_writes_plainly_is_left_alone():
    """Restyling legible figures would change every screen to fix one.

    0.00015229 is the deposit for a 300 GRC payout, printed plainly today. The
    instruction is about scientific notation; this is not it.
    """
    for value in (0.00015229, 0.0001, 0.5, 1, 1000, 1000.09376816, 1234567.0):
        shown = engineering_notation(value)
        assert _exponent_of(shown) is None, f"{value!r} did not need an exponent and got {shown}"
        assert needs_engineering(value) is False, value


def test_the_boundary_is_where_pythons_own_str_gives_up():
    """MEASURED, not chosen -- and this test is what pins it to the measurement.

    float str() switches below 1e-4. Picking a different threshold would either
    leave scientific notation on screen (lower) or restyle figures nobody
    complained about (higher).
    """
    assert Decimal("0.0001") == PLAIN_BELOW
    just_under = 0.00009999
    assert "e-" in str(just_under), (
        "this test's premise is that Python goes scientific here; it no longer does"
    )
    assert needs_engineering(just_under) is True
    assert needs_engineering(float(PLAIN_BELOW)) is False, "the boundary itself writes plainly"
    assert needs_engineering(float(PLAIN_AT_OR_ABOVE)) is True, "and the top end is not a hole"


def test_zero_is_zero_and_not_an_exponent():
    """`0e0` is absurd. Rule 14 still wants it printed, so it must not be blank."""
    for zero in (0, 0.0, Decimal(0), Decimal("0.00000000")):
        assert engineering_notation(zero) == "0", zero
        assert needs_engineering(zero) is False, zero
    assert coin_amount_text(0.0, "BTC") == "0 BTC"


def test_a_float_is_read_through_its_repr_not_its_binary_value():
    """Decimal(0.1) is 0.1000000000000000055511151231257827.

    Formatting that would print seventeen digits of binary noise under a figure a
    customer is checking. coin_amounts.amount_to_base_units() already states this
    reason for the same conversion; this pins it for the display side.

    MUTATION: `Decimal(value)` instead of `Decimal(str(value))`.
    """
    assert engineering_notation(0.1) == "0.1"
    assert engineering_notation(1e-7) == "100e-9"
    assert "0000000000" not in engineering_notation(2.675)


def test_a_sentence_is_not_given_a_unit_and_is_not_turned_into_a_number():
    """The confirm screen's other two branches are a typed string and a sentence.

    Replacing either with "0" would hide a refusal, which rule 14 forbids
    explicitly -- and appending " BTC" to a sentence reads as a figure that
    failed to render rather than as the sentence it is.
    """
    sentence = "priced when you confirm"
    assert coin_amount_text(sentence) == sentence
    assert coin_amount_text(sentence, "BTC") == sentence, "a sentence must not acquire a unit"
    assert coin_amount_text(None, "BTC") == "None", "and nor must None"
    # A bool is an int in Python and is not an amount.
    assert coin_amount_text(True, "BTC") == "True"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (8.061e-05, "80.61e-6"),
        (9.999e-05, "99.99e-6"),
        (8e-06, "8e-6"),
        (1e-08, "10e-9"),
        (1e-07, "100e-9"),
        (0.00015229, "0.00015229"),
        (1000.09376816, "1000.09376816"),
    ],
)
def test_the_exact_renderings(value, expected):
    """Spelled out, because a property test can pass while every figure is ugly."""
    assert engineering_notation(value) == expected
