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

import atomic_swap_xrp as driver
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


def test_the_refusal_still_guards_a_chain_the_flag_offers_without_a_funding_path(capsys, monkeypatch):
    """IT REFUSES NOTHING TODAY, AND THAT IS WHY IT IS TESTED WITH A CHAIN THAT DOES NOT EXIST.

    On 2026-09-29 this guard refused BTC and LTC, because both runners funded through
    Gridcoin's createhtlc. They now fund through the chain clients, so CAN_FUND_THE_HTLC
    equals SCRIPT_CHAINS and no real chain is refused.

    DELETING THE GUARD WOULD BE THE MISTAKE. What it prevents is a chain arriving in
    SCRIPT_CHAINS -- which derives from SECONDS_PER_BLOCK, so adding an interval is enough
    -- with no funding path behind it. That chain would pass every check up to step 5,
    fund the XRP escrow, and fail at step 6. DOGE stands in for it here because a guard
    exercised only by the state it already permits is a guard nobody would notice breaking.
    """
    # A REAL CHAIN WITH THE PERMISSION TAKEN AWAY, not an invented ticker. The refusal
    # reports the chain's block interval, so a chain absent from SECONDS_PER_BLOCK raises
    # KeyError instead of refusing -- which is what the first version of this test did,
    # and it would have hidden the guard rather than exercised it.
    monkeypatch.setattr(driver, "CAN_FUND_THE_HTLC", frozenset({"GRC"}))
    console = a_console(capsys)
    assert refuse_a_chain_this_driver_cannot_fund(console, "BTC") is True
    printed = capsys.readouterr().out
    assert "EVERYTHING ELSE ABOUT BTC WORKS" in printed
    assert "step 5" in printed and "step 6" in printed, (
        "the refusal must say WHERE the one-sided state would arrive, not merely that it could"
    )


def test_EVERY_CHAIN_THE_FLAG_OFFERS_CAN_ACTUALLY_FUND_ITS_HTLC():
    """THE TWO SETS AGREE AGAIN, and this is what keeps them that way.

    They diverged for one commit on 2026-09-29: --chain offered btc and ltc while only GRC
    could be funded, deliberately, because a flag that hid them would have hidden the gap
    too. Moving both runners onto the chain clients closed it. What this pins now is that
    the sets cannot drift APART again silently -- SCRIPT_CHAINS derives from
    SECONDS_PER_BLOCK, so adding a block interval for a fourth chain is enough to offer it,
    and this fails until that chain can actually fund.
    """
    assert set(SCRIPT_CHAINS) == CAN_FUND_THE_HTLC, (
        "a chain the --chain flag offers has no funding path. It would pass every check up "
        "to step 5, fund the XRP escrow, and fail at step 6 with one leg live on a real "
        "chain -- which is the whole reason refuse_a_chain_this_driver_cannot_fund() exists"
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
