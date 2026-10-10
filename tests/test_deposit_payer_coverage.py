"""Every leg a swap can start from has a decision about how its deposit gets paid.

Role: test (reads a table and the filesystem; no network, no wallet, nothing sent)
Reads: swap_terminal/deposit_payers.py, config.Config.ALLOWED_PAIRS, and the
       repository root to confirm each named tool exists
Writes: nothing
Can move funds: no
Mainnet-safe: yes

WHY THIS GATE EXISTS, in the operator's own words on 2026-10-10: "make sure this
doesn't happen when it's any other chain/coin."

WHAT HAPPENED. A swap was created whose deposit leg was ICP, the page showed an
address and an amount, and nothing in the tree could pay it. Establishing THAT
took several attempts, because a tool that does not exist looks exactly like a
tool that is hard to find. Measured the same day across the six assets
ALLOWED_PAIRS can start a swap from: SOL payable, XRP partly, and ICP, BTC, LTC
and GRC not at all. The payer was being written per chain, ad hoc, whenever
somebody hit the wall -- so the wall was going to be hit three more times.

WHAT THIS FILE IS FOR, AND WHAT IT IS NOT. It is NOT a list of today's gaps; the
registry itself carries those, named, with what each one is missing. It is the
assertion that **an asset cannot reach ALLOWED_PAIRS without somebody deciding
how its deposit leg gets paid.** The decision may be "nothing pays this yet" --
that is an allowed and honest answer, recorded in the note. What is not allowed
is silence, which is how ICP arrived.

NO PINNED COUNT, DELIBERATELY (CLAUDE.md rule 19). A test asserting "exactly
three legs are unpayable" would be a baseline: green while the backlog sits,
and needing an edit to go DOWN as well as up. These assert properties instead --
every asset has an entry, every entry is complete, every named tool exists -- so
implementing a payer needs no change here, and adding a chain without one fails.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from config import Config
from deposit_payers import PAYERS, DepositPayer, UnknownAsset, coverage_lines, payer_for, unpayable_assets

ROOT = Path(__file__).resolve().parent.parent

#: Every asset a swap can start FROM. The deposit leg is the from side, which is
#: the side that needs paying -- the `to` side is the payout path's problem.
FROM_ASSETS = sorted({from_asset for from_asset, _ in Config.ALLOWED_PAIRS})


def test_there_is_something_to_check():
    """A gate over an empty list passes while checking nothing."""
    assert FROM_ASSETS, "ALLOWED_PAIRS yielded no from-assets, so this gate is inert"
    assert len(FROM_ASSETS) >= 2


@pytest.mark.parametrize("asset", FROM_ASSETS)
def test_every_leg_a_swap_can_start_from_has_a_registered_payer(asset):
    """THE GATE. MUTATION: add a seventh asset to ALLOWED_PAIRS and nothing else.

    This is the one assertion the file exists for. A swap whose deposit nothing
    can pay, and that nobody noticed could not be paid, is the 2026-10-10 defect;
    the entry is what forces the question to be asked while the pair is being
    added rather than when a customer is waiting.
    """
    payer = payer_for(asset)
    assert isinstance(payer, DepositPayer)


def test_no_registry_entry_is_stale():
    """An entry for an asset no longer in ALLOWED_PAIRS is a tool nobody can reach.

    The reverse direction of the gate. Rule 9: dead code gets read, greped past
    and copied from -- and a registry row for a removed chain would send somebody
    looking for a swap they cannot create.
    """
    orphans = sorted(set(PAYERS) - set(FROM_ASSETS))
    assert not orphans, (
        f"{orphans} have deposit-payer entries but cannot be a swap's from_asset. Either "
        f"ALLOWED_PAIRS lost them or the entry outlived the chain."
    )


@pytest.mark.parametrize("asset", sorted(PAYERS))
def test_an_unpayable_leg_says_what_is_missing(asset):
    """MUTATION: set a tool to "" and leave the note empty.

    "Nothing pays this yet" is an honest answer and an allowed one. "Nothing pays
    this and nobody wrote down why" is the silence this gate exists to prevent --
    it is indistinguishable from an entry somebody forgot to finish.
    """
    payer = PAYERS[asset]
    if payer.payable:
        return
    assert payer.note.strip(), f"{asset} has no payer and no note saying what is missing"
    assert len(payer.note) > 60, (
        f"{asset}'s note is {len(payer.note)} characters, which cannot say what is missing "
        f"and why. The registry's existing notes name the specific call that is absent."
    )


@pytest.mark.parametrize("asset", sorted(PAYERS))
def test_every_named_tool_actually_exists(asset):
    """MUTATION: rename pay_icp_deposit.py without updating the registry.

    A registry that names a tool which is not there is worse than an empty entry:
    it reports the leg as payable and sends the operator to a file that does not
    exist, which reads as their mistake rather than the tree's.
    """
    payer = PAYERS[asset]
    if not payer.payable:
        return
    named = [word for word in payer.tool.split() if word.endswith(".py")]
    assert named, f"{asset}'s tool {payer.tool!r} names no .py file, so nothing can be checked"
    for script in named:
        assert (ROOT / script).is_file(), (
            f"{asset}'s registered payer names {script}, which is not in the repository root. "
            f"Either the tool moved or the entry is wrong -- and the entry is what an operator "
            f"reads."
        )


@pytest.mark.parametrize("asset", sorted(PAYERS))
def test_a_tool_that_moves_funds_is_not_offered_without_its_arming_named(asset):
    """Every payable leg states what the operator must supply explicitly.

    chains/xrp.py's own noqa is the argument: four keyword-only arguments exist
    so "a forgotten opt-in cannot become a send", after an incident where
    can_spend read True and the desk took a deposit it could not pay. A registry
    that named the tool and not the arming would be describing a send as simpler
    than it is.

    BTC and LTC are the legitimate "" case -- base.send_to_address needs only an
    unlocked wallet -- but they are also not payable yet, so the assertion below
    only binds once somebody writes their wrapper, which is when the question
    becomes real.
    """
    payer = PAYERS[asset]
    if not payer.payable:
        return
    assert payer.arming.strip(), (
        f"{asset} is registered as payable with no arming named. If its send genuinely needs "
        f"nothing but an unlocked wallet, say that in `arming` rather than leaving it empty -- "
        f"an empty string is indistinguishable from nobody having checked."
    )


def test_payer_for_refuses_an_unregistered_asset_rather_than_guessing():
    """A default would be wrong in both directions.

    "Payable" sends an operator after a tool that does not exist. "Unpayable"
    would have described ICP correctly by accident on the one day somebody proved
    otherwise -- and told nobody the question had never been asked.
    """
    with pytest.raises(UnknownAsset, match="no deposit payer is registered"):
        payer_for("DOGE")


def test_payer_for_accepts_either_case():
    assert payer_for("icp") == payer_for("ICP")


def test_unpayable_assets_is_sorted_so_pasted_output_does_not_reorder():
    """Rule 14: output read a day later must not have moved."""
    assert unpayable_assets() == sorted(unpayable_assets())
    assert set(unpayable_assets()) <= set(PAYERS)


def test_the_coverage_report_never_prints_nothing():
    """`(none)` is a result; a blank gap is ambiguous between zero and broken."""
    lines = coverage_lines()
    assert lines
    joined = "\n".join(lines)
    if unpayable_assets():
        assert "CANNOT BE PAID" in joined
        for asset in unpayable_assets():
            assert asset in joined
    else:
        assert "no gaps" in joined


def test_the_coverage_report_names_every_payable_tool():
    """So the report is the answer, not a pointer to one."""
    joined = "\n".join(coverage_lines())
    for asset, payer in PAYERS.items():
        if payer.payable:
            assert asset in joined
            assert payer.tool.split()[1] in joined, f"{asset}'s tool is not in the report"
