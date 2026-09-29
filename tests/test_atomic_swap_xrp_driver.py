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
from decimal import Decimal
from pathlib import Path

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


def test_a_chain_that_CAN_be_funded_is_not_refused(capsys):
    for chain in sorted(CAN_FUND_THE_HTLC):
        assert refuse_a_chain_this_driver_cannot_fund(a_console(capsys), chain) is False


def test_the_refusal_still_guards_a_chain_the_flag_offers_without_a_funding_path(capsys, monkeypatch):
    """IT REFUSES NOTHING TODAY, AND THAT IS WHY THE PERMISSION IS TAKEN AWAY HERE.

    THIS REPLACED A PARAMETRIZED TEST OVER `set(SCRIPT_CHAINS) - CAN_FUND_THE_HTLC`, which
    became an EMPTY parameter set the moment the two sets converged -- pytest reported it
    as one skip among ten passes, which is how a guard loses its only test without anything
    turning red. A test whose subject can vanish should not be parametrized over the thing
    that makes it vanish.

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


# ---------------------------------------------------------------------------
# The rate, which is easy to invert and expensive to get wrong.
# ---------------------------------------------------------------------------
def test_THE_RATE_SENTENCE_STATES_BOTH_DIRECTIONS(capsys):
    """A CORRECT LABEL IS ONLY HALF OF RULE 14, measured 2026-09-29.

    `--rate` is XRP PER UNIT of the script chain and the output always said so. The number
    an operator carries in their head is the other one, because it is what the recorded
    runs print: "1 XRP for 66.10250498 GRC". Passing 66.1 to a flag wanting 0.0151 is
    accepted, arithmetically fine, and off by a factor of about 4,300.

    That is exactly what happened: --rate 66.1 was suggested, passed, and produced a leg of
    0.01512859 GRC against one XRP. The label was right and nobody read it, because reading
    it required doing the division. Printing the inverse does the division.
    """
    amount, why = driver._rated_chain_amount(a_console(capsys), "66.1", "GRC")
    assert "1 XRP buys 0.01512859 GRC" in why
    assert "1 GRC costs 66.1 XRP" in why
    assert "you have passed its inverse" in why, (
        "the sentence must name the mistake, not merely make it visible"
    )
    assert amount == Decimal("0.01512859")


def test_the_rate_that_reproduces_the_recorded_run_is_the_INVERSE_of_the_recorded_figure():
    """docs/atomic_swap_runs_2026_09_27.md records 1 XRP for 66.10250498 GRC. The --rate
    that produces that is ~0.0151, not 66.1 -- which is the trap, stated as a number."""
    amount, _ = driver._rated_chain_amount(_QuietConsole(), "0.01512859", "GRC")
    assert Decimal(66) < amount < Decimal(67), (
        f"--rate 0.01512859 should buy about 66 GRC per XRP, got {amount}"
    )


class _QuietConsole:
    """A console that answers check() and says nothing. The rate path only prints on error."""

    def check(self, *_args, **_kwargs):
        return True

    def say(self, *_args, **_kwargs):
        return None


# ---------------------------------------------------------------------------
# The dry run, which is prose and which no test read until it was wrong.
# ---------------------------------------------------------------------------
def test_THE_DRY_RUN_DESCRIBES_THE_PATH_THAT_ACTUALLY_RUNS(capsys):
    """IT DESCRIBED A DEAD MECHANISM, and a dry run is where that costs the most.

    Until 2026-09-29 this printed "step 6 would fund the GRC leg: createhtlc
    receiver=<wallet address> sender=<wallet address>". Both halves were wrong the moment
    the runners moved onto the chain clients: there is no createhtlc call, and those
    addresses are where the claim and refund LAND rather than the HTLC's branches.

    Caught by an operator running it, not by the suite -- the dry run is prose and nothing
    read it. A stale comment is bad; a dry run describing the wrong mechanism is a stale
    comment at the exact moment it is load-bearing, because a dry run exists to be read
    BEFORE committing money.
    """
    console = a_console(capsys)
    leg = driver.ScriptLeg(chain="BTC", tip_height=100, timeout_height=244)
    driver.describe_the_dry_run(console, argparse.Namespace(direction=XRP_FIRST), "BTC", leg,
                                Decimal("0.5"), 844199449, "rA", "rB", "mClaim")
    printed = capsys.readouterr().out
    assert "createhtlc" not in printed, "the dry run names an RPC this driver no longer calls"
    assert "P2SH HTLC" in printed and "script_leg.py" in printed
    assert "minted" in printed, "the branches pay minted keys and the reader must know that"
    assert "shut before the step that publishes the secret" in printed


def test_the_dry_run_numbers_the_steps_BY_DIRECTION(capsys):
    """In xrp-first the XRP leg is step 6 and the script leg is 7; chain-first reverses it.

    Both printed "step 6" until 2026-09-29 -- which reads as two things happening at once,
    in a sequence whose ORDER is the security property. The whole reason the initiator's
    leg is funded first and takes the longer lock is that the order decides who is exposed.

    THE FIRST VERSION OF THIS TEST WAS WORTHLESS and is recorded rather than quietly
    replaced: it ended `assert ... or True`, which passes whatever the code does. A test
    that cannot fail is worse than no test, because it occupies the place where a real one
    would go and reports green while doing so.
    """
    leg = driver.ScriptLeg(chain="GRC", tip_height=1, timeout_height=2)

    driver.describe_the_dry_run(a_console(capsys), argparse.Namespace(direction=XRP_FIRST),
                                "GRC", leg, Decimal(1), 1, "rA", "rB", "mClaim")
    forward = capsys.readouterr().out
    assert "step 6 would fund the XRP leg" in forward
    assert "step 7 would fund the GRC leg" in forward

    driver.describe_the_dry_run(a_console(capsys), argparse.Namespace(direction=CHAIN_FIRST),
                                "GRC", leg, Decimal(1), 1, "rA", "rB", "mClaim")
    reverse = capsys.readouterr().out
    assert "step 7 would fund the XRP leg" in reverse
    assert "step 6 would fund the GRC leg" in reverse, (
        "chain-first funds the script leg FIRST; numbering it 7 would describe the other "
        "direction's exposure to an operator about to commit to this one"
    )
