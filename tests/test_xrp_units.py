"""XRP's drops, finality ladder and threshold guard, called directly.

Role: test (pure functions; no daemon, no socket)
Reads: chains/xrp_units.py
Writes: nothing
Can move funds: no
Mainnet-safe: yes
"""

import pytest
from chains.xrp_units import (
    DROPS_PER_XRP,
    LEDGER_UNVALIDATED,
    LEDGER_VALIDATED,
    MAX_EXACT_XRP,
    XRPThresholdError,
    XRPUnitError,
    describe_min_confirmations,
    from_drops,
    from_drops_decimal,
    ledger_rank,
    to_drops,
    validate_min_confirmations,
)


def test_one_xrp_is_a_million_drops():
    assert DROPS_PER_XRP == 1_000_000
    assert to_drops(1) == 1_000_000


def test_to_drops_does_not_multiply_the_float():
    """Searched for amounts where int(float(x) * 1e6) disagrees with the exact value.

    Fewer exist at 6 decimals than at Monero's 12 -- which is the point: the
    hazard shrinks with the exponent but does not vanish, so the Decimal path
    is not an optimization to remove later.
    """
    for amount, expected in [("0.1", 100_000), ("2.11", 2_110_000), ("8.7", 8_700_000),
                             ("0.000001", 1), ("1234.567891", 1_234_567_891)]:
        assert to_drops(amount) == expected
        assert to_drops(float(amount)) == expected


def test_to_drops_refuses_what_is_not_an_amount():
    for bad in ("banana", None, object(), float("nan"), float("inf"), -1, "-0.000001"):
        with pytest.raises(XRPUnitError):
            to_drops(bad)


def test_from_drops_accepts_the_string_the_ledger_actually_sends():
    """Drop counts arrive as JSON STRINGS, so a client cannot round them.

    Accepting the string and converting once is what keeps that protection --
    parsing it as a JSON number first would defeat the reason it is a string.
    """
    assert from_drops("25000000") == 25.0
    assert from_drops(25_000_000) == 25.0
    assert str(from_drops_decimal("1234567891")) == "1234.567891"


def test_xrp_has_more_float_headroom_than_any_other_chain_here():
    """Six decimals, so the REAL columns are roomier than for the 8-decimal chains."""
    assert MAX_EXACT_XRP == 2**53 / 10**6
    assert MAX_EXACT_XRP > 9_000_000_000


def test_the_finality_ladder_has_exactly_two_rungs():
    """A validated ledger is final. There is no third state to invent."""
    assert ledger_rank(True) == LEDGER_VALIDATED == 1
    assert ledger_rank(False) == LEDGER_UNVALIDATED == 0
    assert ledger_rank(None) == LEDGER_UNVALIDATED


def test_anything_that_is_not_exactly_true_is_unvalidated():
    """Fail closed. A truthy string or a 1 is not the ledger saying "validated"."""
    for ambiguous in ("true", 1, "validated", [], {}, "yes"):
        assert ledger_rank(ambiguous) == LEDGER_UNVALIDATED


@pytest.mark.parametrize("bad", [0, 2, 3, 6, 12])
def test_a_threshold_no_payment_can_reach_is_refused(bad):
    """THE SILENT-STALL GUARD.

    A 6 copied from a Bitcoin-shaped config would leave every XRP deposit
    below the threshold forever, with nothing in any log saying why -- because
    nothing fails. Refused at construction instead.
    """
    with pytest.raises(XRPThresholdError, match="cannot be satisfied"):
        validate_min_confirmations(bad)


def test_the_only_valid_threshold_is_one():
    assert validate_min_confirmations(1) == 1


def test_the_refusal_explains_the_ledger_rather_than_just_the_range():
    """An operator needs to know WHY 6 is meaningless here, not just that it is."""
    with pytest.raises(XRPThresholdError) as caught:
        validate_min_confirmations(6)
    message = str(caught.value)
    assert "does not reorganize" in message
    assert "no depth to accumulate" in message


def test_the_banner_never_lets_the_number_read_as_blocks():
    """Rule 6: the unit is stated next to the figure, every time."""
    line = describe_min_confirmations(1)
    assert "validated ledger" in line
    assert "NOT a block depth" in line
