"""What the XRP driver refuses, and what it normalizes, before it contacts anything.

Role: test
Reads: nothing. No adapter, no endpoint, no daemon.
Writes: nothing.
Can send orders: no
Live-safe: yes
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "swap_terminal"))

from step_console import Console

from atomic_swap_xrp import (
    CAN_FUND_THE_HTLC,
    CHAIN_FIRST,
    LEGACY_CHAIN_FIRST,
    SCRIPT_CHAINS,
    XRP_FIRST,
    normalize_arguments,
    refuse_a_chain_this_driver_cannot_fund,
)


def a_console(capsys):
    capsys.readouterr()
    return Console(total_steps=10)


# ---------------------------------------------------------------------------
# The refusal. It exists because the alternative is a funded XRP escrow.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("chain", sorted(set(SCRIPT_CHAINS) - CAN_FUND_THE_HTLC))
def test_a_chain_whose_HTLC_CANNOT_BE_FUNDED_is_refused_before_any_network_call(chain, capsys):
    """THE ONE-SIDED STATE THIS PREVENTS IS THE WHOLE POINT.

    Both runners fund the script leg with `adapter.call("createhtlc", ...)`, and
    createhtlc is a GRIDCOIN RPC -- bitcoind and litecoind answer "Method not found".
    Measured 2026-09-29 by running `--chain btc`, which passed the adapter check, the
    regtest network check, the bech32 addresses and the commitment before anything
    suggested a problem.

    In xrp-first THE XRP ESCROW IS FUNDED AT STEP 5 and createhtlc runs at step 6. A
    --run that discovered the missing method there would have one leg funded on a live
    chain and no way to fund the other -- exactly the state every timelock in this file
    exists to prevent, arriving through the driver rather than through a counterparty.

    So the refusal is at the top of main(), before the first network call, and this test
    asserts it returns True for every chain that cannot be funded.
    """
    assert refuse_a_chain_this_driver_cannot_fund(a_console(capsys), chain) is True


def test_a_chain_that_CAN_be_funded_is_not_refused(capsys):
    for chain in sorted(CAN_FUND_THE_HTLC):
        assert refuse_a_chain_this_driver_cannot_fund(a_console(capsys), chain) is False


def test_the_refusal_names_the_METHOD_and_the_INTERFACE_that_would_replace_it(capsys):
    """Rule 14: a refusal an operator cannot act on is one they route around.

    It has to say three things -- which method is missing, that everything else about the
    chain works, and where the replacement already lives -- because the fix is NOT to add
    a method. modules/atomic_btc_client.py already exposes create_contract() and builds
    the P2SH itself; atomic_swap.py funds HTLCs on all three chains through it. This
    driver simply has a second implementation that covers one chain.
    """
    console = a_console(capsys)
    refuse_a_chain_this_driver_cannot_fund(console, "BTC")
    printed = capsys.readouterr().out
    assert "createhtlc" in printed
    assert "atomic_btc_client" in printed and "create_contract" in printed
    assert "EVERYTHING ELSE ABOUT BTC WORKS" in printed
    assert "step 5" in printed and "step 6" in printed, (
        "the refusal must say WHERE the one-sided state would arrive, not merely that it could"
    )


def test_CAN_FUND_THE_HTLC_is_a_SUBSET_of_the_chains_the_flag_offers():
    """The flag deliberately offers more chains than can complete a swap, because the
    driver genuinely handles them everywhere except step 6 -- and a flag that hid them
    would hide the gap too. This pins that the two sets cannot drift into disagreement in
    the other direction: a chain that can fund but is not offered would be unreachable.
    """
    assert set(SCRIPT_CHAINS) >= CAN_FUND_THE_HTLC
    assert {"GRC"} == CAN_FUND_THE_HTLC, (
        "if a chain gained a funding path, this set moved -- and PROVEN_LIVE, the banner "
        "and docs/branch_coverage.md all describe the old state until they are updated too"
    )


# ---------------------------------------------------------------------------
# Normalization: what was typed, versus what the tables are keyed by.
# ---------------------------------------------------------------------------
def test_a_lowercase_chain_becomes_the_key_every_table_uses():
    args = argparse.Namespace(chain="btc", direction=XRP_FIRST)
    normalize_arguments(args)
    assert args.chain == "BTC"


def test_the_LEGACY_direction_spelling_resolves_rather_than_becoming_a_second_name():
    """grc-first is accepted because it is in every recorded run of this driver and in
    the operator's history. It resolves HERE so nothing downstream ever sees two names
    for one direction -- which is what stops a later `== CHAIN_FIRST` from silently
    missing the legacy runs."""
    args = argparse.Namespace(chain="grc", direction=LEGACY_CHAIN_FIRST)
    normalize_arguments(args)
    assert args.direction == CHAIN_FIRST
    assert args.chain == "GRC"


def test_the_modern_direction_is_left_alone():
    args = argparse.Namespace(chain="grc", direction=XRP_FIRST)
    normalize_arguments(args)
    assert args.direction == XRP_FIRST
