"""Behavioral tests for the one microfortnight conversion (CLAUDE.md rule 6).

Role: test (read-only)
Reads: swap_terminal/microfortnights.py
Writes: nothing
Can move funds: no
Mainnet-safe: yes

These assert on the STRING a reader would see, not on the arithmetic alone,
because every part of rule 6 that has ever drifted is in the rendering: an
ASCII "u" instead of µ, a space before the unit, seconds printed as "2.8 s".
Asserting `seconds_to_microfortnights(2.4192) == 2.0` would pass on output that
violates every one of those.
"""

import pytest
from microfortnights import (
    UFN_SECONDS,
    UFN_SYMBOL,
    format_duration,
    format_microfortnights,
    microfortnights_to_seconds,
    seconds_to_microfortnights,
)


def test_one_microfortnight_is_exactly_1_2096_seconds():
    assert UFN_SECONDS == 1.2096
    assert seconds_to_microfortnights(1.2096) == 1.0


def test_conversion_scales_linearly():
    # A fortnight has a million microfortnights, by construction.
    fortnight_seconds = 14 * 24 * 60 * 60
    assert round(seconds_to_microfortnights(fortnight_seconds)) == 1_000_000


def test_round_trip_returns_the_original_duration():
    # approx, not ==: this is a divide followed by a multiply in binary
    # floating point, and 2.8 comes back as 2.7999999999999994. Asserting
    # exact equality here would be asserting something about IEEE 754 rather
    # than about the conversion, and it fails on the second value tried.
    for seconds in (0.0, 0.5, 2.8, 902.3, 3600.0):
        assert microfortnights_to_seconds(seconds_to_microfortnights(seconds)) == pytest.approx(seconds)


def test_negative_duration_is_reported_negative_not_clamped():
    # A negative duration means the caller's clock arithmetic is backwards.
    # Clamping to 0.0 would turn a bug into a plausible-looking measurement.
    assert seconds_to_microfortnights(-1.2096) == -1.0


def test_unit_uses_the_micro_sign_and_never_an_ascii_u():
    rendered = format_microfortnights(2.8)
    assert "µfn" in rendered
    assert "ufn" not in rendered
    # U+03BC GREEK SMALL LETTER MU renders identically in many fonts and is a
    # different codepoint. The symbol is U+00B5.
    assert "μ" not in rendered
    assert UFN_SYMBOL == "µfn"


def test_no_space_between_the_number_and_the_unit():
    assert format_microfortnights(2.8) == "2.3µfn"
    assert " µfn" not in format_microfortnights(2.8)


def test_format_duration_is_ufn_then_seconds_in_parentheses():
    # The exact shape rule 6 names: `done in 2.3µfn (2.8s)`.
    assert format_duration(2.8) == "2.3µfn (2.8s)"
    # And the seconds follow the same no-space rule.
    assert " s)" not in format_duration(2.8)


def test_format_duration_decimals_are_honored_on_both_halves():
    assert format_duration(2.8, decimals=3) == "2.315µfn (2.800s)"


def test_zero_duration_still_prints_a_value_rather_than_nothing():
    # Rule 14: never let an empty result print nothing. A zero-length phase is
    # a result, and it has to look different from a missing one.
    assert format_duration(0.0) == "0.0µfn (0.0s)"
