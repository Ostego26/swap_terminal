"""Monero's arithmetic and confirmation floor, tested directly.

Role: test (pure functions; no daemon, no socket, no database)
Reads: chains/monero_units.py
Writes: nothing
Can move funds: no
Mainnet-safe: yes

These are the parts of the Monero path that ARE measured. chains/monero.py's
header is explicit that its wire format is a hypothesis -- the RPC field names
could not be checked against a running wallet from the environment this was
written in. Nothing in this file has that excuse: every assertion below calls
the real function with a seeded value and checks the number that comes back.
"""

import pytest
from chains.monero_units import (
    ATOMIC_UNITS_PER_XMR,
    CONSENSUS_SPEND_LOCK_BLOCKS,
    MAX_EXACT_XMR,
    MoneroUnitError,
    describe_min_confirmations,
    effective_min_confirmations,
    from_atomic,
    from_atomic_decimal,
    to_atomic,
)


def test_one_xmr_is_a_trillion_atomic_units_not_a_hundred_million():
    """The whole reason this chain is not a fourth one-line subclass.

    chains/base.py hardcodes SATOSHI = 1e-8 and says "the chains here all use
    8 decimal places". Monero uses 12. If this ever equals 10**8, an amount
    has been read at Bitcoin's scale and every figure on the XMR path is wrong
    by a factor of ten thousand.
    """
    assert ATOMIC_UNITS_PER_XMR == 10**12
    assert to_atomic(1) == 1_000_000_000_000


def test_to_atomic_does_not_multiply_the_float():
    """0.1 * 10**12 is 100000000000.00001 in binary floating point.

    int() of that is 100000000000 only because the error happens to fall the
    right way; other amounts round the other way and lose a piconero. Going
    through Decimal(str(amount)) removes the question entirely, and this test
    is here so that an "optimization" back to float multiplication fails.
    """
    assert to_atomic(0.1) == 100_000_000_000
    assert to_atomic(0.3) == 300_000_000_000
    assert to_atomic(2.675) == 2_675_000_000_000

    # THE THREE ABOVE DO NOT ACTUALLY DISCRIMINATE, and that is worth saying
    # rather than quietly fixing. A first version of this test asserted only
    # those, and a mutation replacing the Decimal path with
    # `int(float(amount) * 10**12)` PASSED it -- every one of them happens to
    # land on the right side of the binary rounding error.
    #
    # These do not. Found 2026-09-25 by searching 400,000 random amounts for a
    # disagreement between the two implementations; each of these loses exactly
    # one piconero to truncation under float multiplication, and 2.11 is the
    # point of the exercise -- an amount somebody would type, not a contrived
    # one.
    assert to_atomic(2.11) == 2_110_000_000_000
    assert to_atomic(4.187218) == 4_187_218_000_000
    assert to_atomic(8.24818869) == 8_248_188_690_000
    assert to_atomic(32.825506366807) == 32_825_506_366_807


def test_to_atomic_accepts_a_string_and_agrees_with_the_float():
    assert to_atomic("0.1") == to_atomic(0.1)
    assert to_atomic("1.234567890123") == 1_234_567_890_123


def test_to_atomic_refuses_what_is_not_an_amount():
    """Each of these would otherwise express itself as a rejected `transfer`.

    Several layers from the mistake, in a log nobody reads until a payout is
    late. MoneroUnitError names the value instead.
    """
    for bad in ("banana", None, object(), float("nan"), float("inf")):
        with pytest.raises(MoneroUnitError):
            to_atomic(bad)


def test_to_atomic_refuses_a_negative_amount():
    """Neither a deposit credit nor a payout has a meaning below zero."""
    with pytest.raises(MoneroUnitError):
        to_atomic(-1)
    with pytest.raises(MoneroUnitError):
        to_atomic("-0.000000000001")


def test_from_atomic_round_trips_every_amount_this_terminal_would_see():
    for amount in ("0.000000000001", "0.1", "1", "12.345678901234", "1000"):
        assert to_atomic(from_atomic(to_atomic(amount))) == to_atomic(amount)


def test_from_atomic_decimal_is_exact_where_the_float_is_not():
    """The float and the Decimal agree on value and disagree on exactness.

    from_atomic() returns a float to match db.py's REAL columns and every
    other adapter; from_atomic_decimal() is for anything an operator reads or
    reconciles. This pins that the exact form really is exact, so the two are
    not quietly the same function.
    """
    atomic = 1_234_567_890_123
    assert str(from_atomic_decimal(atomic)) == "1.234567890123"
    assert float(from_atomic_decimal(atomic)) == from_atomic(atomic)


def test_the_float_precision_ceiling_is_where_the_docstring_says_it_is():
    """2**53 piconero, and the module says 9007.199254740992 XMR.

    A measured claim in a comment ages (CLAUDE.md rule 3 and the whole of
    Mammon's rule 1). This is the claim, asserted, so it cannot age silently.
    """
    assert MAX_EXACT_XMR == 2**53 / 10**12
    assert round(MAX_EXACT_XMR, 6) == 9007.199255


def test_min_confirmations_is_floored_at_the_consensus_spend_lock():
    """Configuring 2 does not buy a faster payout; it buys a later failure."""
    assert CONSENSUS_SPEND_LOCK_BLOCKS == 10
    assert effective_min_confirmations(0) == 10
    assert effective_min_confirmations(2) == 10
    assert effective_min_confirmations(10) == 10


def test_min_confirmations_above_the_floor_is_left_alone():
    """The floor is a floor, not an override. An operator who wants more gets more."""
    assert effective_min_confirmations(20) == 20


def test_the_banner_says_so_when_it_raised_the_operators_number():
    """Rule 14: state what the number means, next to the number.

    A banner printing `min_confirmations=2` while the code enforces 10 tells
    the operator something false about their own configuration, and they read
    the screen rather than this file.
    """
    raised = describe_min_confirmations(2)
    assert "10 blocks" in raised
    assert "raised from the configured 2" in raised

    unchanged = describe_min_confirmations(12)
    assert unchanged == "12 blocks"
    assert "raised" not in unchanged
