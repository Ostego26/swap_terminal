"""Solana's vocabulary: amounts, the commitment ladder, rent and fees.

Role: test (pure functions; no chain, no socket)
Reads: swap_terminal/chains/solana_units.py
Writes: nothing
Can move funds: no
Mainnet-safe: yes

THE TWO THINGS HERE THAT ARE NOT ORDINARY UNIT TESTS.

test_a_bitcoin_style_threshold_is_refused_rather_than_obeyed is the important
one in this file. An operator who sets SOL_MIN_CONFIRMATIONS=6 -- copying
Gridcoin's value, which is an entirely reasonable thing to type -- has asked
for a rung that does not exist on Solana's commitment ladder. Without the
refusal, every Solana deposit would sit at rank 3 forever, confirmed_total
would stay 0, the payout would never fire, and NOTHING WOULD BE LOGGED AS
WRONG. That is CLAUDE.md rule 14's worst shape: "did nothing" wearing the same
face as "did work". The only symptom is a customer asking where their coins
are.

test_an_unknown_commitment_level_stalls_rather_than_credits pins the direction
of the other failure. A level this code has never heard of maps to rank 0, so a
future Solana release adding a fourth level stalls a swap rather than releasing
one. Stalling is visible and recoverable; releasing is on-chain and final.
"""

from __future__ import annotations

import pytest
from chains.solana_units import (
    COMMITMENT_RANKS,
    FINALIZED_RANK,
    FLOAT_EXACT_INTEGER_LIMIT,
    LAMPORTS_PER_SOL,
    SOL_DECIMALS,
    amount_to_base_units,
    base_units_to_amount,
    commitment_rank,
    describe_commitment,
    float_is_exact_for,
    rank_name,
    transfer_fee_lamports,
    validate_min_commitment_rank,
)

# --- amounts -----------------------------------------------------------------


def test_one_sol_is_a_billion_lamports_in_both_directions():
    assert base_units_to_amount(LAMPORTS_PER_SOL, SOL_DECIMALS) == 1.0
    assert amount_to_base_units(1.0, SOL_DECIMALS) == LAMPORTS_PER_SOL


def test_the_conversion_is_exact_where_float_division_is_not():
    """123456789 lamports is 0.123456789 SOL exactly. The naive float division
    is wrong in the last place, and the result is compared against a quoted
    amount inside a tolerance band."""
    assert base_units_to_amount(123_456_789, SOL_DECIMALS) == 0.123456789


def test_an_spl_token_uses_its_own_decimals_and_not_sols_nine():
    """Rule 11: a value crossing a boundary at the wrong precision is silently
    wrong by orders of magnitude. 1000000 base units is 1.0 at six decimals and
    0.001 at nine."""
    assert base_units_to_amount(1_000_000, 6) == 1.0
    assert base_units_to_amount(1_000_000, SOL_DECIMALS) == 0.001


def test_amounts_truncate_rather_than_round_up():
    """The direction is a money decision: rounding up would send a fraction of a
    unit more than was quoted, every time, out of the hot wallet."""
    assert amount_to_base_units(1.9999999999, SOL_DECIMALS) == 1_999_999_999
    assert amount_to_base_units(0.0000000009, SOL_DECIMALS) == 0


def test_amount_to_base_units_reads_the_typed_decimal_not_the_binary_float():
    """Decimal(str(0.1)) is 0.1; Decimal(0.1) is
    0.1000000000000000055511151231257827..., and truncating those gives
    different answers at nine decimals."""
    assert amount_to_base_units(0.1, SOL_DECIMALS) == 100_000_000


@pytest.mark.parametrize("decimals", [-1, -9])
def test_negative_decimals_are_refused(decimals):
    with pytest.raises(ValueError, match="negative"):
        base_units_to_amount(1, decimals)
    with pytest.raises(ValueError, match="negative"):
        amount_to_base_units(1.0, decimals)


def test_the_float_precision_boundary_is_reported_rather_than_rounded_past():
    assert float_is_exact_for(FLOAT_EXACT_INTEGER_LIMIT)
    assert not float_is_exact_for(FLOAT_EXACT_INTEGER_LIMIT + 1)


# --- the commitment ladder ---------------------------------------------------


@pytest.mark.parametrize(
    ("level", "expected"),
    [("processed", 1), ("confirmed", 2), ("finalized", 3), ("FINALIZED", 3), ("  confirmed  ", 2)],
)
def test_each_commitment_level_maps_to_its_rung(level, expected):
    assert commitment_rank(level) == expected


def test_an_unknown_commitment_level_stalls_rather_than_credits():
    """Rank 0 is not creditable. A level this code has never heard of therefore
    stops a swap instead of releasing a payout against it."""
    assert commitment_rank(None) == 0
    assert commitment_rank("") == 0
    assert commitment_rank("some_future_level") == 0


def test_rank_zero_matches_an_unmined_bitcoin_deposit():
    """services/deposit_service.py branches on `max_confirmations <= 0` to mean
    'seen but not confirming'. Rank 0 keeps that branch working for Solana
    without a per-chain special case."""
    assert COMMITMENT_RANKS["unknown"] == 0


def test_finalized_is_the_top_of_the_ladder():
    assert max(COMMITMENT_RANKS.values()) == FINALIZED_RANK
    assert rank_name(FINALIZED_RANK) == "finalized"


# --- the threshold refusal ---------------------------------------------------


@pytest.mark.parametrize("rung", [0, 1, 2, 3])
def test_every_rung_on_the_ladder_is_accepted(rung):
    assert validate_min_commitment_rank(rung) == rung


@pytest.mark.parametrize("bitcoin_style", [4, 6, 2 * 3, 100])
def test_a_bitcoin_style_threshold_is_refused_rather_than_obeyed(bitcoin_style):
    """THE ONE THAT MATTERS. See this module's docstring: obeying this value
    would stall every SOL swap forever with nothing logged as wrong."""
    with pytest.raises(ValueError) as exc:
        validate_min_commitment_rank(bitcoin_style)
    message = str(exc.value)
    # The message has to explain the category difference, not just refuse --
    # the operator who typed 6 believes it is a count of blocks.
    assert "commitment" in message
    assert "not a count of blocks" in message
    # And it has to print the ladder, so the fix is on the screen (rule 14).
    assert "3=finalized" in message


def test_a_non_integer_threshold_is_refused():
    with pytest.raises(ValueError, match="integer"):
        validate_min_commitment_rank("finalized")


def test_describe_commitment_says_the_verdict_and_that_it_is_not_blocks():
    creditable = describe_commitment(3, 3)
    assert "CREDITABLE" in creditable
    assert "NOT a count of blocks" in creditable
    pending = describe_commitment(1, 3)
    assert "not yet creditable" in pending
    assert "processed" in pending


def test_slot_depth_is_labeled_a_diagnostic_wherever_it_is_printed():
    """Slot depth reads exactly like a confirmation count and carries no safety
    claim, which is why it may be reported and may never be the gate."""
    line = describe_commitment(1, 3, slot_depth=500)
    assert "500" in line
    assert "diagnostic only" in line
    assert "NOT the gate" in line


# --- fees --------------------------------------------------------------------


def test_the_fee_is_per_signature_and_does_not_vary_with_size():
    """htlc_fee.py's rule is rate x size. Solana's is 5,000 lamports per
    signature, whether the transfer moves one lamport or a million SOL."""
    assert transfer_fee_lamports(1) == 5_000
    assert transfer_fee_lamports(2) == 10_000


def test_a_transaction_with_no_signature_is_refused():
    with pytest.raises(ValueError, match="at least one signature"):
        transfer_fee_lamports(0)
