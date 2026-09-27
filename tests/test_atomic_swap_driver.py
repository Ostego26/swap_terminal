"""The any-pair swap driver's derived vocabulary and its refusals. No daemons.

Role: test (read-only; opens no socket and constructs no client)
Reads: nothing
Writes: nothing
Can move funds: no
Mainnet-safe: yes
Live-safe: yes

WHAT IS TESTED HERE

The driver's job -- funding two legs on two chains -- needs two daemons and real balances,
so it is proven by running it, not here. What IS tested is the part a defect would make
expensive and silent: the pair vocabulary being DERIVED rather than listed, the timelock
ordering that is the swap's whole security property, and that the file refuses rather than
guesses when it cannot name a network.
"""

from __future__ import annotations

import inspect
import os
import pathlib
import re
import sys

import pytest

REPOSITORY_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "swap_terminal"))
sys.path.insert(0, str(REPOSITORY_ROOT))

from step_console import Console  # noqa: E402  same

import atomic_swap  # noqa: E402  both path shims above come first
from atomic_swap import (  # noqa: E402  same
    AMOUNT_KEYWORD,
    ASSET_PAIRS,
    ASSETS,
    CLIENTS,
    TEST_CHAIN_NAMES,
    FundedLeg,
    Leg,
    Party,
    Step,
    SwapError,
    assert_ordering,
    build_parser,
    client_for,
)


def _step() -> Step:
    return Step(console=Console(total_steps=8), number=1)


def test_the_pairs_are_derived_from_the_assets_and_not_listed():
    """Rule 11: one vocabulary, derived in one place. Three assets make six directed pairs
    because three assets exist, and a fourth would make twelve without anybody editing a
    list. A hand-written pair table agrees with CLIENTS on the day it is written and
    drifts the first time one of them changes -- rule 8's shape."""
    assert tuple(sorted(CLIENTS)) == ASSETS
    expected = {(a, b) for a in ASSETS for b in ASSETS if a != b}
    assert set(ASSET_PAIRS) == expected
    assert len(ASSET_PAIRS) == len(ASSETS) * (len(ASSETS) - 1) == 6
    assert not any(a == b for a, b in ASSET_PAIRS), "a swap needs two chains"


def test_every_asset_has_a_client_and_an_amount_keyword():
    """The three clients spell create_contract's amount differently -- amount_btc,
    amount_ltc, amount_grc -- and that divergence predates this driver. It is mapped in ONE
    place, so a new asset is a row rather than an if, and a MISSING row would be a
    TypeError at funding time rather than at import."""
    assert set(AMOUNT_KEYWORD) == set(ASSETS)
    for asset in ASSETS:
        assert AMOUNT_KEYWORD[asset] == f"amount_{asset.lower()}"
        assert hasattr(CLIENTS[asset], "create_contract")
        assert hasattr(CLIENTS[asset], "redeem_contract")
        assert hasattr(CLIENTS[asset], "refund_contract")


def test_xrp_and_xmr_are_absent_for_protocol_reasons_and_say_so():
    """"Any currency listed" is not yet true and the file must not imply it is.

    XRP has no script (EscrowCreate with a crypto-condition instead) and XMR has NO SCRIPT
    AT ALL, so there is nowhere to put a hashlock. Adding either to CLIENTS would be a
    claim no code can honor, and the module docstring is where a reader finds out which
    pairs are real.
    """
    assert "XRP" not in CLIENTS
    assert "XMR" not in CLIENTS

    # WHITESPACE-NORMALIZED, and that is the third time today a test of mine matched prose
    # and lost. The docstring wraps at 90 columns, so "NO SCRIPT AT ALL" is split across a
    # newline and a contiguous match fails on text that says exactly the right thing. The
    # structural assertions above are the load-bearing ones -- XRP and XMR being absent
    # from CLIENTS is what stops a claim no code can honor -- and these three only check
    # that a reader is TOLD where to look.
    doc = re.sub(r"\s+", " ", atomic_swap.__doc__ or "")
    assert "NO SCRIPT AT ALL" in doc, "the XMR blocker must be stated, not implied"
    assert "atomic_swap_xrp_grc.py" in doc, "the XRP driver that DOES work must be named"
    assert "section 6 stage 5" in doc, "and where the XMR work is tracked"


def test_the_participant_leg_must_expire_first_or_it_refuses():
    """THE SECURITY PROPERTY, and the reason it is a refusal rather than a warning.

    If the participant's lock outlived the initiator's, the initiator could wait out their
    OWN lock, refund their leg, and then claim the participant's with the secret they never
    spent -- taking both. The participant has no counter: the secret is theirs to learn, not
    to produce.
    """
    # Good: initiator has 100 blocks left, participant 50. Margin +50.
    assert_ordering(_step(), initiator_lock=1100, initiator_tip=1000,
                    participant_lock=2050, participant_tip=2000)

    # Bad: participant outlives the initiator.
    with pytest.raises(SwapError, match="blocks LATER than the initiator"):
        assert_ordering(_step(), initiator_lock=1050, initiator_tip=1000,
                        participant_lock=2100, participant_tip=2000)

    # Bad: equal is not "first". A zero margin is a tie, and a tie is not an ordering.
    with pytest.raises(SwapError, match=r"blocks LATER than the initiator|margin"):
        assert_ordering(_step(), initiator_lock=1100, initiator_tip=1000,
                        participant_lock=2100, participant_tip=2000)


def test_the_ordering_is_measured_in_blocks_remaining_not_raw_heights():
    """Two chains have unrelated tips, so comparing locktimes directly compares numbers
    that mean nothing to each other. Here the participant's LOCKTIME is far higher than the
    initiator's and it is still correct, because its chain's tip is higher too."""
    assert_ordering(_step(), initiator_lock=1200, initiator_tip=1000,
                    participant_lock=900_000, participant_tip=899_900)


def test_a_participant_leg_already_expired_at_funding_is_refused():
    """A lock in the past is the ABSENCE of a timelock, not a short one: its refund branch
    is spendable the moment it is funded. That is the 2026-09-24 `locktime=500000` defect,
    which was a height already mined years earlier."""
    with pytest.raises(SwapError, match="already expired at funding time"):
        assert_ordering(_step(), initiator_lock=1100, initiator_tip=1000,
                        participant_lock=1990, participant_tip=2000)


def test_client_for_refuses_an_unset_password_rather_than_guessing_one():
    """It will not invent a credential, and the refusal names the three variables."""
    saved = {k: os.environ.pop(k, None) for k in ("GRC_RPC_PASS", "LTC_RPC_PASS", "BTC_RPC_PASS")}
    try:
        for asset in ASSETS:
            with pytest.raises(SwapError, match=f"{asset}_RPC_PASS is not set"):
                client_for(asset)
    finally:
        for key, value in saved.items():
            if value is not None:
                os.environ[key] = value


def test_client_for_refuses_an_asset_it_cannot_drive_and_names_why():
    with pytest.raises(SwapError, match="no client for 'XMR'"):
        client_for("XMR")
    with pytest.raises(SwapError, match=r"different protocol|no script"):
        client_for("XRP")


def test_only_test_chain_names_are_accepted():
    """`main` is not in the set and neither is anything unrecognized. The file asks each
    daemon which chain it is on rather than inferring from a port -- config.py records what
    happened the last time a port was trusted."""
    assert "main" not in TEST_CHAIN_NAMES
    assert "mainnet" not in TEST_CHAIN_NAMES
    assert {"test", "testnet", "testnet3", "testnet4", "regtest", "signet"} <= TEST_CHAIN_NAMES


def test_the_two_addresses_on_a_leg_are_separate_fields():
    """They were ONE value in atomic_swapper.py until 2026-09-24, which made every contract
    unclaimable: both branches required the same key. Separate fields mean the confusion
    cannot be expressed here at all."""
    fields = set(Leg.__dataclass_fields__)
    assert {"participant_address", "refund_address"} <= fields
    doc = Leg.__doc__ or ""
    assert "COUNTERPARTY" in doc and "own" in doc


def test_a_funded_leg_carries_its_client_so_the_wrong_one_cannot_be_used():
    """leg, funded and client are meaningless apart -- a funded dict belongs to one leg on
    one chain reachable by one client. Passing them separately is what allows spending leg
    A's outpoint with leg B's client, which on two chains with similar RPC shapes fails
    obscurely."""
    fields = set(FundedLeg.__dataclass_fields__)
    assert fields == {"leg", "funded", "client"}
    for name in ("find_vout", "redeem_script", "script_pubkey_hex", "call"):
        assert hasattr(FundedLeg, name)


def test_the_vout_is_never_defaulted_to_zero():
    """Fixed three times in this repository -- BTC 2026-09-25, GRC 2026-09-26 -- and
    Gridcoin's createhtlc returns no vout at all while funding through SendMoney(), which
    adds change. A guess of 0 spends nothing and burns a fee."""
    source = inspect.getsource(FundedLeg.find_vout)
    assert "htlc_vout(" in source
    assert "raise SwapError" in source, "a missing vout refuses rather than defaulting"
    assert ", 0)" not in source and "or 0" not in source


def test_no_run_flag_funds_nothing():
    """Two chains get funded irreversibly, so a bare invocation must print and stop."""
    parser = build_parser()
    assert parser.parse_args([]).run is False
    assert parser.parse_args(["--from", "GRC", "--to", "LTC"]).run is False
    assert parser.parse_args(["--from", "GRC", "--to", "LTC", "--run"]).run is True


def test_the_parser_only_offers_pairs_the_file_can_drive():
    """argparse's `choices` is the guard: a typo or an unsupported asset fails at the
    command line rather than at funding time."""
    parser = build_parser()
    for asset in ASSETS:
        assert parser.parse_args(["--from", asset, "--to", "GRC" if asset != "GRC" else "LTC"])
    with pytest.raises(SystemExit):
        parser.parse_args(["--from", "XMR", "--to", "GRC"])


def test_the_party_pairs_a_key_with_its_destination():
    """A claim signed by one party's key paying another party's address is the mistake this
    shape makes hard to write."""
    party = Party(privkey="cWIF", destination="tb1qexample")
    assert party.privkey == "cWIF"
    assert set(Party.__dataclass_fields__) == {"privkey", "destination"}
