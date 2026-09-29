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
from decimal import Decimal

import pytest

REPOSITORY_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "swap_terminal"))
sys.path.insert(0, str(REPOSITORY_ROOT))

from modules.atomic_htlc_scripts import p2sh_script_for  # noqa: E402  path shims above
from modules.htlc_timelock import (  # noqa: E402  same
    ROLE_INITIATOR,
    ROLE_PARTICIPANT,
    contract_locktime,
)
from step_console import Console  # noqa: E402  same
from valid_addresses import BTC_PARTICIPANT  # noqa: E402  conftest puts tests/ on sys.path

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
    PlannedLeg,
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


def test_xrp_is_absent_for_a_protocol_reason_and_says_so():
    """"Any currency listed" is not yet true and the file must not imply it is.

    XRP has no script -- EscrowCreate with a crypto-condition instead -- so there is nowhere
    to put a P2SH hashlock. Adding it to CLIENTS would be a claim no code can honor, and the
    module docstring is where a reader finds out which pairs are real.

    NARROWED 2026-09-29 FROM A TEST THAT ALSO PINNED XMR. Monero was removed from this tree,
    so the three prose assertions naming its blocker were pinning sentences that no longer
    describe anything. The XRP half is unchanged and is the half that still has a subject
    (rule 2: a test dies with the thing it pinned, or changes to pin the stronger invariant).

    WHITESPACE-NORMALIZED, and that is worth keeping: the docstring wraps at 90 columns, so
    a phrase can be split across a newline and a contiguous match fails on text that says
    exactly the right thing. The structural assertion is the load-bearing one -- XRP being
    absent from CLIENTS is what stops a claim no code can honor -- and the prose one only
    checks that a reader is TOLD where the working driver is.
    """
    assert "XRP" not in CLIENTS

    doc = re.sub(r"\s+", " ", atomic_swap.__doc__ or "")
    assert "atomic_swap_xrp_grc.py" in doc, "the XRP driver that DOES work must be named"


def _planned(asset: str, role: str, tip: int, locktime: int) -> PlannedLeg:
    """A PlannedLeg with only the fields the ordering check reads."""
    return PlannedLeg(
        leg=Leg(asset=asset, role=role, amount=Decimal(1),
                participant_address="participant", refund_address="refund"),
        tip=tip, locktime=locktime,
    )


def test_the_real_btc_ltc_swap_that_the_old_check_refused_is_accepted():
    """THE REGRESSION, with the operator's own numbers from 2026-09-27.

    First real run, BTC regtest tip 1358 and LTC regtest tip 3657. contract_locktime() derived
    exactly what its policy says -- 48h for the initiator, 24h for the participant -- and the
    ordering check REFUSED it:

        initiator   BTC  288 blocks x 600s = 172800s = 48h
        participant LTC  576 blocks x 150s =  86400s = 24h
        old check: 288 - 576 = -288 blocks  -> REFUSED a correct swap
        new check: 172800 - 86400 = +86400s -> accepted, with 24h of margin

    Litecoin needs FOUR TIMES the blocks for HALF the time, so block counts across two chains
    were never comparable. This is the live case, not a constructed one."""
    initiator = _planned("BTC", ROLE_INITIATOR, tip=1358, locktime=1646)
    participant = _planned("LTC", ROLE_PARTICIPANT, tip=3657, locktime=4233)

    assert initiator.blocks_remaining == 288 and participant.blocks_remaining == 576
    assert initiator.blocks_remaining < participant.blocks_remaining, (
        "fewer BLOCKS, which is what the old check refused on"
    )
    assert initiator.seconds_remaining == 172800.0
    assert participant.seconds_remaining == 86400.0
    assert_ordering(_step(), initiator=initiator, participant=participant)


def test_the_locktimes_the_real_authority_derives_always_order_correctly():
    """Not just the one measured pair: EVERY directed pair of the three assets, with the
    locktimes contract_locktime() actually derives. The bug was a disagreement between the
    authority and the checker, so the checker is fed the authority's own output rather than
    numbers chosen to pass."""
    for from_asset, to_asset in ASSET_PAIRS:
        initiator = _planned(from_asset, ROLE_INITIATOR, tip=1000,
                             locktime=contract_locktime(from_asset, ROLE_INITIATOR, 1000))
        participant = _planned(to_asset, ROLE_PARTICIPANT, tip=5000,
                               locktime=contract_locktime(to_asset, ROLE_PARTICIPANT, 5000))
        assert_ordering(_step(), initiator=initiator, participant=participant)


def test_the_participant_leg_must_expire_first_or_it_refuses():
    """The security property. If the participant's lock outlives the initiator's, the
    initiator waits out their own lock, refunds their leg, and THEN claims the participant's
    with the secret -- taking both."""
    with pytest.raises(SwapError, match=r"expires .* LATER"):
        assert_ordering(
            _step(),
            initiator=_planned("BTC", ROLE_INITIATOR, tip=1000, locktime=1100),
            participant=_planned("BTC", ROLE_PARTICIPANT, tip=1000, locktime=1200),
        )


def test_the_ordering_is_compared_in_time_and_not_in_blocks():
    """THE TEST THIS REPLACED ASSERTED THE BUG, which is why it is called out here.

    It was named `..._is_measured_in_blocks_remaining_not_raw_heights` and it pinned exactly
    the comparison that refused a correct swap. Blocks remaining IS the comparable unit within
    one chain; across two chains with different target intervals it is a category error.

    Constructed so the two answers DISAGREE: the initiator has FEWER blocks and MORE time.
    A check counting blocks refuses this; a check measuring time accepts it."""
    initiator = _planned("BTC", ROLE_INITIATOR, tip=0, locktime=100)      # 100 x 600s = 60000s
    participant = _planned("LTC", ROLE_PARTICIPANT, tip=0, locktime=200)  # 200 x 150s = 30000s
    assert initiator.blocks_remaining < participant.blocks_remaining
    assert initiator.seconds_remaining > participant.seconds_remaining
    assert_ordering(_step(), initiator=initiator, participant=participant)

    # And the reverse: MORE blocks but LESS time must still be refused.
    with pytest.raises(SwapError, match=r"expires .* LATER"):
        assert_ordering(
            _step(),
            initiator=_planned("LTC", ROLE_INITIATOR, tip=0, locktime=200),
            participant=_planned("BTC", ROLE_PARTICIPANT, tip=0, locktime=100),
        )


def test_a_zero_margin_is_refused_because_simultaneous_expiry_is_a_race():
    """STRICTLY first, and a mutation check is why this test exists.

    Relaxing `margin <= 0` to `margin < 0` -- accepting two legs that expire at the same
    instant -- killed no test. It is not a harmless boundary: at equal expiry both refund
    branches open together, so whether the initiator refunds their own leg before the
    participant refunds theirs is decided by block timing and relay, not by the protocol.
    The initiator is the one holding the secret, so they are the only party who can profit
    from winning that race.

    Constructed ACROSS two chains, so it is the time margin being asserted and not a block
    count that happens to be equal: 600 LTC blocks x 150s = 90000s = 150 BTC blocks x 600s."""
    initiator = _planned("BTC", ROLE_INITIATOR, tip=0, locktime=150)
    participant = _planned("LTC", ROLE_PARTICIPANT, tip=0, locktime=600)
    assert initiator.seconds_remaining == participant.seconds_remaining == 90000.0
    assert initiator.blocks_remaining != participant.blocks_remaining
    with pytest.raises(SwapError, match=r"expires .* LATER"):
        assert_ordering(_step(), initiator=initiator, participant=participant)


def test_a_participant_leg_already_expired_at_funding_is_refused():
    """A lock in the past is the ABSENCE of a timelock, not a short one: its refund branch is
    spendable the moment it is funded. That is the 2026-09-24 `locktime=500000` defect, a
    height already mined years earlier."""
    with pytest.raises(SwapError, match="already expired at funding time"):
        assert_ordering(
            _step(),
            initiator=_planned("BTC", ROLE_INITIATOR, tip=1000, locktime=1100),
            participant=_planned("BTC", ROLE_PARTICIPANT, tip=2000, locktime=1990),
        )


def test_every_refusal_says_nothing_was_funded_and_means_it():
    """The message used to be FALSE. On 2026-09-27 the check ran after both legs were funded
    and printed "Nothing was funded" while two contracts existed on two chains.

    Planning is now separate from funding, so the claim is structural: assert_ordering takes
    PlannedLegs, which carry a tip and a locktime and no txid, because there is no funding to
    carry. A leg that has been funded is a FundedLeg and this function cannot accept one."""
    with pytest.raises(SwapError, match="Nothing was funded"):
        assert_ordering(
            _step(),
            initiator=_planned("BTC", ROLE_INITIATOR, tip=1000, locktime=1100),
            participant=_planned("BTC", ROLE_PARTICIPANT, tip=1000, locktime=1200),
        )
    assert set(PlannedLeg.__dataclass_fields__) == {"leg", "tip", "locktime"}, (
        "a PlannedLeg carries no txid, because nothing has been funded when it is judged"
    )


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
    party = Party(privkey="cWIF", destination=BTC_PARTICIPANT)
    assert party.privkey == "cWIF"
    assert set(Party.__dataclass_fields__) == {"privkey", "destination"}


# ---------------------------------------------------------------------------------------
# THE RUN PATH, WITH SEEDED CHAINS AND NO DAEMONS
#
# Everything above this line tests vocabulary and refusals. What follows tests the three
# functions main() delegates its decisions to -- mint_parties, build_legs and
# claim_both_legs -- because each of them can be WRONG IN A WAY THAT SUCCEEDS.
#
# The stub below is why these are behavioral rather than source-matching. Three tests
# written earlier in this session asserted on inspect.getsource() output and PASSED while
# the code under them was mutated: one matched a docstring sentence, one matched
# "broadcast_refund(" inside prose, one matched a phrase that a line wrap had split. A test
# that reads source proves the source says something. Only a test that runs the function
# and asserts on what it DID proves the function does something, so each test below was
# checked by breaking the thing it names and confirming it fails.
# ---------------------------------------------------------------------------------------


class _StubChain:
    """One chain, answering only the RPCs the claim path actually calls.

    It records every redeem_contract call in `claims`, and the two stubs share one
    `order` list so a test can assert WHICH LEG WAS CLAIMED FIRST -- the security-relevant
    ordering that a swap of two arguments would silently reverse.

    `published` is the preimage this chain will hand back out of the claim's scriptSig. It
    is a separate field from the secret passed in precisely so a test can make them
    disagree, which is the one case where both legs are funded and the run must stop.
    """

    def __init__(self, asset: str, redeem_script: bytes, *, published: bytes,
                 order: list, vout: int = 1) -> None:
        self.asset = asset
        self.redeem_script = redeem_script
        self.published = published
        self.order = order
        self.vout = vout
        self.claims: list[dict] = []
        self.addresses_issued: list[str] = []
        self.funding_txid = f"{asset.lower()}-funding-txid"
        self.claim_txid = f"{asset.lower()}-claim-txid"

    def _funding_outputs(self) -> list[dict]:
        # The contract deliberately sits at index `vout` and NOT at 0, so a test fails if
        # anything ever reintroduces the defaulted index this repository has fixed three
        # times. Index 0 is a decoy paying something else.
        outputs = [{"n": index, "scriptPubKey": {"hex": "00" * 22}} for index in range(self.vout)]
        outputs.append({"n": self.vout,
                        "scriptPubKey": {"hex": p2sh_script_for(self.redeem_script).hex()}})
        return outputs

    def rpc_call(self, method: str, args: list):
        if method == "getnewaddress":
            address = f"{self.asset}-wallet-{len(self.addresses_issued)}"
            self.addresses_issued.append(args[0] if args else "")
            return address
        if method == "getrawtransaction":
            txid = args[0]
            if txid == self.funding_txid:
                return {"vout": self._funding_outputs()}
            if txid == self.claim_txid:
                # A 32-byte push (0x20) of the preimage, which is the shape
                # preimage_from_scriptsig scans for.
                return {"vin": [{"scriptSig": {"hex": "20" + self.published.hex()}}]}
            raise AssertionError(f"{self.asset}: unexpected txid {txid}")
        raise AssertionError(f"{self.asset}: unexpected rpc {method}")

    def redeem_contract(self, txid, vout, redeem_script,  # noqa: PLR0913, PLR0917 -- checked: this signature is NOT mine to choose. It mirrors modules/atomic_{btc,ltc,grc}_client.redeem_contract(), which take the same six positionally and carry the same noqa with the same reason. A stub that grouped them into a dataclass to satisfy the ceiling would accept calls the real clients reject, which is the one thing a stub must never do -- it would make this file pass while the run path was broken.
                        secret_hex, privkey, destination):
        # THE TYPES ARE RECORDED, not just the values. All three clients declare
        # `secret: bytes` and push it with push_data(); a hex STRING reaches
        # `bytes([length]) + data` and dies with "can't concat str to bytes". That is exactly
        # where the operator's 2026-09-27 run stopped, with both legs funded -- and the old
        # stub accepted a str happily, because a stub that does not enforce its own signature
        # cannot catch a caller that violates it.
        if not isinstance(secret_hex, bytes):
            raise TypeError(
                f"redeem_contract's `secret` is declared bytes on all three clients; got "
                f"{type(secret_hex).__name__}. push_data() concatenates it to bytes directly"
            )
        if not isinstance(redeem_script, bytes):
            raise TypeError(f"`redeem_script` is declared bytes; got {type(redeem_script).__name__}")
        self.claims.append({"txid": txid, "vout": vout, "secret_hex": secret_hex.hex(),
                            "secret_bytes": secret_hex,
                            "privkey": privkey, "destination": destination})
        self.order.append(self.asset)
        return self.claim_txid


@pytest.fixture(autouse=True)
def _no_polling_sleep(monkeypatch):
    """read_secret_off_chain() polls with a 3-second sleep for up to 20 attempts, which is
    right on a chain and wrong in a suite.

    MEASURED, because the first mutation check of these tests is what found it: with the
    real sleep in place, every mutation that makes the off-chain read look for the wrong
    transaction spends 60 seconds per test in the poll loop, and five of the tests below
    reach that path -- 300 seconds for ONE mutant, and a seven-mutant check that does not
    finish. The polling BEHAVIOR is still exercised (the attempts still run, the reasons
    are still collected); only the wall clock is removed, which is why this patches
    time.sleep rather than shrinking `attempts`.
    """
    monkeypatch.setattr(atomic_swap.time, "sleep", lambda _seconds: None)


def _seeded_swap(*, published: bytes | None = None):
    """A funded two-leg swap over two stub chains: GRC out, LTC in.

    Returns everything a test needs to assert on -- the console, the parties, the two
    FundedLegs, the shared claim-order list and the secret.
    """
    secret = bytes(range(32))
    console = Console(total_steps=8)
    parties = atomic_swap.mint_parties(Step(console, 2), ("GRC", "LTC"))
    leg_a, leg_b = atomic_swap.build_legs("GRC", "LTC", 1000, Decimal("0.05"), parties)

    order: list[str] = []
    scripts = {"GRC": bytes([0x51] * 40), "LTC": bytes([0x52] * 44)}
    chains = {
        asset: _StubChain(asset, scripts[asset],
                          published=(published if published is not None else secret),
                          order=order)
        for asset in ("GRC", "LTC")
    }
    funded = {}
    for leg in (leg_a, leg_b):
        chain = chains[leg.asset]
        funded[leg.asset] = FundedLeg(
            leg,
            {"txid": chain.funding_txid, "p2sh_address": f"2{leg.asset}", "tip": 100,
             "redeem_script": chain.redeem_script.hex(), "locktime": 120},
            chain,
        )
    return {"console": console, "parties": parties, "secret": secret, "order": order,
            "chains": chains, "funded_a": funded["GRC"], "funded_b": funded["LTC"]}


class _StubContractClient:
    """A client whose create_contract answers the way BTCClient's actually does.

    The point of the stub is the ABSENCE: BTCClient returns no `p2sh_address` key at all, and
    `contract.get("p2sh_address") or ""` therefore produced the empty string, which the driver
    printed as if it were an address. `reports_address` flips that so both shapes are covered.
    """

    def __init__(self, redeem_script: bytes, *, reports_address: str | None = None) -> None:
        self.redeem_script = redeem_script
        self.reports_address = reports_address
        self.calls: list[dict] = []

    def create_contract(self, **kwargs):
        self.calls.append(kwargs)
        # THE REAL SHAPE, deliberately: camelCase keys and the script as BYTES, exactly as
        # atomic_btc_client.py:330 and its two siblings return it. A stub answering in hex
        # under snake_case is how the driver came to assume a shape no client produces --
        # it passed its tests and died on the operator's first real run.
        answer = {"txid": "a" * 64, "redeemScript": self.redeem_script}
        if self.reports_address is not None:
            answer["p2shAddress"] = self.reports_address
        return answer


def test_the_three_real_clients_answer_in_the_shape_read_contract_expects():
    """READ FROM THE CLIENTS, NOT ASSUMED, and this is the test that would have caught it.

    All three create_contract() implementations return

        {"txid": str, "vout": int, "redeemScript": BYTES, "p2shAddress": str}

    -- camelCase, script as bytes. The driver looked for "redeem_script" and "p2sh_address",
    found neither, and fell through to `or ""`. That printed a bare "BTC contract at" with
    nothing after it on the operator's 2026-09-27 run, and then died on
    bytes.fromhex("b'\\x63\\xa8...") once the address was derived instead: str() of bytes is
    its repr, so position 1 is the quote.

    Asserted against the real classes' source keys via the constants, so adding a fourth client
    that answers differently fails here rather than at funding time."""
    assert "redeemScript" in atomic_swap.CONTRACT_SCRIPT_KEYS
    assert "p2shAddress" in atomic_swap.CONTRACT_ADDRESS_KEYS
    assert "vout" in atomic_swap.CONTRACT_VOUT_KEYS
    # Both spellings, because accepting one and guessing is what produced the blank line.
    assert "redeem_script" in atomic_swap.CONTRACT_SCRIPT_KEYS
    assert "p2sh_address" in atomic_swap.CONTRACT_ADDRESS_KEYS


def test_a_redeem_script_is_accepted_as_bytes_or_hex_and_refused_otherwise():
    """bytes is the REAL case; hex is accepted because a future client may hand one over.

    `str(b"\x63")` is "b'c'" rather than an error, which is why this conversion lives in one
    function: a wrong conversion that raises is a good day, and this one produced a plausible
    string that failed five lines later talking about hexadecimal."""
    script = bytes([0x63, 0xA8, 0x20])
    assert atomic_swap.script_hex_from(script) == "63a820"
    assert atomic_swap.script_hex_from(bytearray(script)) == "63a820"
    assert atomic_swap.script_hex_from("63a820") == "63a820"
    assert atomic_swap.script_hex_from("  63a820  ") == "63a820"
    # The exact failure from the live run: a bytes repr that reached fromhex as a string.
    with pytest.raises(SwapError, match="neither bytes nor hex"):
        atomic_swap.script_hex_from(str(script))
    with pytest.raises(SwapError, match="neither bytes nor hex"):
        atomic_swap.script_hex_from("not a script")


def test_a_contract_missing_its_script_under_every_spelling_is_refused():
    """A key absent under EVERY spelling is a refusal naming the keys that did arrive -- never
    a default. Defaulting is what turned a missing key into an empty address that got printed
    as though it were an answer."""
    step = _step()
    with pytest.raises(SwapError, match="Keys present"):
        atomic_swap.read_contract(step, "BTC", {"txid": "a" * 64})
    with pytest.raises(SwapError, match="Keys present"):
        atomic_swap.read_contract(step, "BTC", {"redeemScript": b"\x51"})
    # And the reason names the spellings it looked for, so an operator can see the mismatch.
    try:
        atomic_swap.read_contract(step, "BTC", {"txid": "a" * 64, "script": b"\x51"})
    except SwapError as error:
        assert "redeemScript" in str(error) and "script" in str(error)
    else:
        raise AssertionError("a contract with no recognized script key must refuse")


def test_the_reported_vout_is_cross_checked_against_the_chain_and_a_mismatch_refuses():
    """The clients DO return a vout and this driver ignored it. Matching the scriptPubKey on
    chain is the stronger method -- but ignoring a second opinion throws away a free check.

    The client derived its index from its own view of the funding transaction, so a
    disagreement means the transaction on chain is not the one it thinks it funded. Neither
    number is then usable, so it refuses: spending the wrong index spends nothing and burns a
    fee, which is the defect this repository has fixed three times."""
    seeded = _seeded_swap()
    funded = seeded["funded_b"]          # its stub puts the contract at vout 1
    agreeing = FundedLeg(funded.leg, {**funded.funded, "reported_vout": 1}, funded.client)
    assert agreeing.find_vout(_step()) == 1

    disagreeing = FundedLeg(funded.leg, {**funded.funded, "reported_vout": 0}, funded.client)
    with pytest.raises(SwapError, match="cannot both be right"):
        disagreeing.find_vout(_step())

    # A client that reports nothing is not a disagreement -- the chain match stands alone.
    silent = FundedLeg(funded.leg, {**funded.funded, "reported_vout": None}, funded.client)
    assert silent.find_vout(_step()) == 1


def test_a_client_that_reports_no_address_still_prints_the_contract_it_built(capsys):
    """THE BLANK LINE FROM THE OPERATOR'S FIRST RUN, 2026-09-27:

        BTC contract at

    with nothing after it. BTCClient returns no `p2sh_address`, so the driver printed an empty
    string as if it were an answer. Rule 14: a blank gap cannot be told from a value that
    broke, and this is the line an operator would copy to look the contract up.

    It is DERIVED from the redeem script the daemon itself returned -- the same
    p2sh_script_for() that find_vout() uses to locate the output on chain -- rather than
    reported as missing, because the script determines the scriptPubKey completely."""
    script = bytes([0x51] * 40)
    client = _StubContractClient(script)
    console = Console(total_steps=8)
    leg = Leg(asset="BTC", role=ROLE_INITIATOR, amount=Decimal("0.01"),
              participant_address="participant", refund_address="refund")
    planned = atomic_swap.PlannedLeg(leg=leg, tip=1358, locktime=1646)

    funded = atomic_swap.fund_leg(Step(console, 6), planned, "ab" * 32, client)

    printed = capsys.readouterr().out
    expected_hex = p2sh_script_for(script).hex()
    assert expected_hex in printed, "the derived scriptPubKey must be printed"
    assert "contract scriptPubKey \n" not in printed and "contract at \n" not in printed
    assert "(none reported; derived below)" in printed, (
        "rule 14: say the address is ABSENT rather than printing a gap where one belongs"
    )
    assert funded["redeem_script"] == script.hex()
    assert funded["locktime"] == 1646 and funded["tip"] == 1358
    # The locktime that was PLANNED is the one sent to the daemon -- not one re-derived from a
    # tip read again inside create_contract, which is what made the old ordering check late.
    assert client.calls[0]["locktime"] == 1646


def test_a_client_that_does_report_an_address_has_it_shown_beside_the_derivation(capsys):
    """The other branch, so the function cannot pass by always printing the same thing. GRC's
    createhtlc does return an address; it is shown, and the derivation is shown too, because a
    disagreement between them means the daemon funded a contract other than the one it
    described."""
    script = bytes([0x52] * 44)
    client = _StubContractClient(script, reports_address="2MxFF952zuNzXonkRW4uUWcCFvntkaTxGQ6")
    console = Console(total_steps=8)
    leg = Leg(asset="GRC", role=ROLE_PARTICIPANT, amount=Decimal(1000),
              participant_address="participant", refund_address="refund")
    atomic_swap.fund_leg(Step(console, 6), atomic_swap.PlannedLeg(leg=leg, tip=10, locktime=99),
                         "cd" * 32, client)
    printed = capsys.readouterr().out
    assert p2sh_script_for(script).hex() in printed
    assert "2MxFF952zuNzXonkRW4uUWcCFvntkaTxGQ6" in printed
    assert "(none reported" not in printed


def test_mint_parties_makes_four_distinct_keys_and_prints_no_private_key(capsys):
    """Four keys and not two, because each leg's two branches must hash to DIFFERENT keys
    -- build_htlc_redeem_script() refuses a script whose branches resolve to one hash160,
    and four distinct keys means that state cannot be constructed here at all.

    The second assertion is the one that matters more than it looks: the WIFs exist in this
    process and nothing may print them. swap_terminal's own rules forbid reading a key back
    at all, and a debug line that echoes a party would put a spending key into the terminal
    scrollback an operator pastes around."""
    console = Console(total_steps=8)
    parties = atomic_swap.mint_parties(Step(console, 2), ("GRC", "LTC"))

    assert set(parties) == {("GRC", ROLE_INITIATOR), ("GRC", ROLE_PARTICIPANT),
                            ("LTC", ROLE_INITIATOR), ("LTC", ROLE_PARTICIPANT)}
    assert len({key.address for key in parties.values()}) == 4

    printed = capsys.readouterr().out
    assert parties[("GRC", ROLE_INITIATOR)].address in printed, (
        "the addresses ARE printed, so this test is reading the right stream"
    )
    for key in parties.values():
        assert key.wif not in printed


def test_build_legs_inverts_the_roles_between_the_two_legs():
    """THE INVERSION, asserted on the addresses rather than on the source.

    Leg A (the --from asset, the initiator's) has its hashlock branch naming the
    PARTICIPANT's key and its refund branch the initiator's own. Leg B inverts both,
    because the initiator is the party who claims leg B with the secret.

    Getting this backwards builds two contracts each claimable only by whoever funded it:
    two self-payments that both report success. build_htlc_redeem_script() cannot catch it,
    because a consistently swapped script is perfectly well formed -- so this assertion is
    the only thing standing between that bug and a run."""
    console = Console(total_steps=8)
    parties = atomic_swap.mint_parties(Step(console, 2), ("GRC", "LTC"))
    leg_a, leg_b = atomic_swap.build_legs("GRC", "LTC", 1000, Decimal("0.05"), parties)

    assert leg_a.asset == "GRC" and leg_a.role == ROLE_INITIATOR
    assert leg_a.participant_address == parties[("GRC", ROLE_PARTICIPANT)].address
    assert leg_a.refund_address == parties[("GRC", ROLE_INITIATOR)].address

    assert leg_b.asset == "LTC" and leg_b.role == ROLE_PARTICIPANT
    assert leg_b.participant_address == parties[("LTC", ROLE_INITIATOR)].address
    assert leg_b.refund_address == parties[("LTC", ROLE_PARTICIPANT)].address


def test_build_legs_refuses_a_non_positive_amount_on_either_side():
    """A zero or negative amount reaches create_contract as a transfer, and the flag named
    in the message is the one the operator has to fix -- naming the wrong side of a
    two-chain command costs a round trip."""
    console = Console(total_steps=8)
    parties = atomic_swap.mint_parties(Step(console, 2), ("GRC", "LTC"))
    with pytest.raises(SwapError, match="--from-amount"):
        atomic_swap.build_legs("GRC", "LTC", 0, Decimal("0.05"), parties)
    with pytest.raises(SwapError, match="--to-amount"):
        atomic_swap.build_legs("GRC", "LTC", 1000, Decimal(-1), parties)


def test_wallet_destination_asks_the_wallet_and_never_returns_a_minted_address():
    """The subtlety this function exists for: the contract's BRANCHES name the in-process
    keys, but the claim's DESTINATION must be a wallet address. Paying a claim to a minted
    address sends the swapped coins to a key discarded when the process exits -- a
    successful swap whose proceeds nobody can spend, which reads as success and is a
    total loss of that leg."""
    seeded = _seeded_swap()
    chain = seeded["chains"]["LTC"]
    address = atomic_swap.wallet_destination(
        Step(seeded["console"], 7), "LTC", chain, "atomic-swap initiator payout")

    assert address == "LTC-wallet-0"
    assert chain.addresses_issued == ["atomic-swap initiator payout"]
    minted = {key.address for key in seeded["parties"].values()}
    assert address not in minted


def test_claim_both_legs_claims_the_participant_leg_first():
    """WHICH LEG MOVES FIRST is the security property, and it is not symmetric.

    The initiator holds the secret, so they are the only party who can move, and the leg
    they move against is the PARTICIPANT's -- funded_b, the one with the shorter timelock.
    Claiming funded_a first would publish the preimage on the initiator's own chain while
    the participant's leg is still locked: the participant learns the secret, claims the
    leg they were receiving anyway, and the initiator has given up their only leverage."""
    seeded = _seeded_swap()
    claim_b, claim_a = atomic_swap.claim_both_legs(
        seeded["console"], seeded["funded_a"], seeded["funded_b"],
        seeded["parties"], seeded["secret"])

    assert seeded["order"] == ["LTC", "GRC"], "the participant's leg is claimed first"
    assert claim_b == "ltc-claim-txid"
    assert claim_a == "grc-claim-txid"


def test_claim_both_legs_signs_each_claim_with_the_counterparty_key_on_that_chain():
    """The inversion build_legs() encodes, one level on: leg B's claim is signed by the
    INITIATOR's key on leg B's chain, leg A's by the PARTICIPANT's key on leg A's.

    Swapping these produces no recognizable error -- it produces a signature that fails
    script verification, surfacing as a rejected transaction on a chain where the other
    leg is already funded and the secret may already be public."""
    seeded = _seeded_swap()
    atomic_swap.claim_both_legs(seeded["console"], seeded["funded_a"], seeded["funded_b"],
                               seeded["parties"], seeded["secret"])
    parties = seeded["parties"]

    ltc_claim = seeded["chains"]["LTC"].claims[0]
    grc_claim = seeded["chains"]["GRC"].claims[0]
    assert ltc_claim["privkey"] == parties[("LTC", ROLE_INITIATOR)].wif
    assert grc_claim["privkey"] == parties[("GRC", ROLE_PARTICIPANT)].wif
    # And each pays OUT to that chain's wallet, not to the other chain's and not to a
    # minted address.
    assert ltc_claim["destination"] == "LTC-wallet-0"
    assert grc_claim["destination"] == "GRC-wallet-0"


def test_claim_both_legs_spends_the_vout_found_on_chain_and_not_index_zero():
    """The stubs deliberately put the contract at index 1 behind a decoy at 0. The defect
    fixed three times in this repository -- BTC 2026-09-25, GRC 2026-09-26 -- is a guess of
    0, which spends a decoy and burns a fee while reporting a txid."""
    seeded = _seeded_swap()
    atomic_swap.claim_both_legs(seeded["console"], seeded["funded_a"], seeded["funded_b"],
                               seeded["parties"], seeded["secret"])
    assert seeded["chains"]["LTC"].claims[0]["vout"] == 1
    assert seeded["chains"]["GRC"].claims[0]["vout"] == 1


def test_leg_a_is_claimed_by_a_function_that_cannot_see_the_generated_secret():
    """WHY THIS IS A SIGNATURE TEST AND NOT A BEHAVIORAL ONE, which is the interesting part.

    The guarantee wanted here is "leg A is claimed with the bytes read off leg B's chain,
    never with the copy in memory" -- and no behavioral test can check it. In a correct read
    the two values are EQUAL by construction: read_secret_off_chain() returns the push whose
    SHA-256 matches the committed hash, and the committed hash is sha256(secret). A mutation
    on 2026-09-27 that swapped `recovered` for `secret` killed no test, and it never could.

    So the guarantee was made structural. claim_initiator_leg() takes the preimage and has
    no `secret` parameter, so the mutation is not a mistake to avoid -- it is a name that
    does not exist. THAT is checkable, and this is the check. An earlier version of this
    same function also ended with `if recovered != secret: raise`, which looked like the
    safety net and was unreachable short of a SHA-256 collision; deleting it killed no test
    either, which is how it was found."""
    names = list(inspect.signature(atomic_swap.claim_initiator_leg).parameters)
    assert names == ["console", "funded_a", "parties", "preimage"]
    assert "secret" not in names
    source = inspect.getsource(atomic_swap.claim_initiator_leg)
    assert "secret" not in source.split('"""')[-1], (
        "the body must not name a secret it was not given"
    )


def test_claim_initiator_leg_puts_the_bytes_it_was_given_on_the_chain():
    """Behavioral half: whatever preimage reaches this function is what gets pushed. Seeded
    with bytes that are NOT the swap's secret, so the assertion distinguishes the argument
    from any value the function could reach for."""
    seeded = _seeded_swap()
    foreign = bytes([0xAB]) * 32
    assert foreign != seeded["secret"]
    atomic_swap.claim_initiator_leg(seeded["console"], seeded["funded_a"],
                                    seeded["parties"], foreign)
    assert seeded["chains"]["GRC"].claims[0]["secret_hex"] == foreign.hex()


def test_a_published_preimage_that_is_not_ours_stops_before_claiming_leg_a():
    """Both legs are funded at this point, so a scriptSig carrying no push that matches the
    committed hash must stop -- and must NOT refund. The chain here publishes a well-formed
    32-byte push whose SHA-256 is not the committed hash, which is what reading a DIFFERENT
    transaction looks like.

    The refusal comes from read_secret_off_chain(), which is where the hash is actually
    compared. claim_both_legs() used to repeat the comparison afterwards and that copy was
    unreachable; this test is what remains of it, and it is the reachable one."""
    seeded = _seeded_swap(published=bytes([0xAB]) * 32)
    with pytest.raises(SwapError, match="carries NO push"):
        atomic_swap.claim_both_legs(seeded["console"], seeded["funded_a"], seeded["funded_b"],
                                    seeded["parties"], seeded["secret"])
    assert seeded["order"] == ["LTC"], "leg A must not be claimed without a matching preimage"
    assert seeded["chains"]["GRC"].claims == []


def test_claim_both_legs_derives_the_hash_rather_than_taking_it_as_an_argument():
    """It began as a sixth parameter and the linter refused it, which was right for a
    reason beyond the count: a caller passing both a secret and its hash can pass two that
    do not correspond, and this function's whole job is checking the secret against the
    hash the chain published. Taking both would be comparing one argument to another."""
    names = list(inspect.signature(atomic_swap.claim_both_legs).parameters)
    assert names == ["console", "funded_a", "funded_b", "parties", "secret"]


# ---------------------------------------------------------------------------------------
# NAMING THE NETWORK, AND THE GRIDCOIN ROUTE THAT HAD NEVER RUN.
#
# BTC and LTC answer getblockchaininfo with a `chain`. Gridcoin carries a `testnet` boolean on
# getinfo instead. That fallback was written from reading Gridcoin's RPC surface and had never
# been exercised by anything when the operator asked to test GRC pairs end to end (2026-09-27),
# which is rule 17's distinction between a reason to believe and a check.
#
# THIS BLOCK USED TO SAY GRIDCOIN HAS "NO getblockchaininfo at all", AND THE STUB BELOW WAS
# BUILT TO MATCH -- `fail_blockchaininfo=True` raising method-not-found. Measured 2026-09-28
# against the operator's testnet daemon, that is the wrong shape: the method ANSWERS and simply
# has no `chain` key (adaptor_regtest_verify step 2 printed `got=(none)` with no exception
# text, which is the successful-call-missing-key rendering, and Gridcoin master registers the
# command). So the one route GRC actually takes had never been exercised either -- the test
# passed through a branch the daemon does not use. Both shapes are pinned below now: the
# measured one FIRST, and the method-not-found one kept because a build older than the
# operator's may well produce it and the fallback must survive both.
# ---------------------------------------------------------------------------------------


class _StubNetworkClient:
    """A daemon that answers only the two network RPCs, in one of the three real shapes."""

    def __init__(self, *, chain: str | None = None, testnet: bool | None = None,
                 fail_blockchaininfo: bool = False) -> None:
        self.chain = chain
        self.testnet = testnet
        self.fail_blockchaininfo = fail_blockchaininfo
        self.asked: list[str] = []

    def rpc_call(self, method: str, params=None):
        self.asked.append(method)
        if method == "getblockchaininfo":
            if self.fail_blockchaininfo:
                raise RuntimeError("Method not found")
            return {} if self.chain is None else {"chain": self.chain}
        if method == "getinfo":
            return {} if self.testnet is None else {"testnet": self.testnet}
        raise AssertionError(f"unexpected rpc {method}")


def test_bitcoin_and_litecoin_are_named_from_getblockchaininfo():
    """The route those two daemons answer on, and the values they really return."""
    for chain in ("regtest", "test", "testnet3", "signet"):
        client = _StubNetworkClient(chain=chain)
        assert atomic_swap.chain_name("BTC", client) == chain
        assert chain in TEST_CHAIN_NAMES


def test_gridcoin_is_named_from_getinfos_testnet_boolean():
    """A BUILD WHOSE getblockchaininfo IS ABSENT -- method-not-found is the SIGNAL to try
    getinfo rather than a failure, and getinfo carries `testnet`. Kept alongside the measured
    shape above rather than replaced by it: the operator's build answers False for
    signrawtransactionwithkey which master has, so builds in this family differ, and the
    fallback has to survive either answer.

    Both values, because a route that only ever returns "testnet" would pass this test while
    being unable to refuse a mainnet daemon -- and the operator has a MAINNET Gridcoin wallet
    running on the same machine, holding their real staking balance, one port number away."""
    testnet = _StubNetworkClient(testnet=True, fail_blockchaininfo=True)
    assert atomic_swap.chain_name("GRC", testnet) == "testnet"
    assert "getblockchaininfo" in testnet.asked and "getinfo" in testnet.asked
    assert atomic_swap.chain_name("GRC", testnet).lower() in TEST_CHAIN_NAMES

    mainnet = _StubNetworkClient(testnet=False, fail_blockchaininfo=True)
    assert atomic_swap.chain_name("GRC", mainnet) == "main"
    assert "main" not in TEST_CHAIN_NAMES, "the mainnet answer must not pass the gate"


def test_gridcoins_measured_shape_is_getblockchaininfo_answering_without_a_chain_key():
    """THE SHAPE THE OPERATOR'S DAEMON ACTUALLY PRODUCES, which nothing exercised until now.

    `chain=None` makes the stub return `{}` from getblockchaininfo -- the method answering with
    no `chain` key, which is what was measured on 2026-09-28 -- and the naming must fall
    through to getinfo exactly as it does for a method-not-found. Without this test the GRC
    route was only ever driven through an exception branch that daemon never takes, so a
    refactor that handled the raise and dropped the missing-key case would have gone green and
    then refused every real Gridcoin daemon at step 1.
    """
    testnet = _StubNetworkClient(chain=None, testnet=True)
    assert atomic_swap.chain_name("GRC", testnet) == "testnet"
    assert testnet.asked == ["getblockchaininfo", "getinfo"], (
        "getblockchaininfo is asked and ANSWERS; the fall-through is on the key, not on a raise"
    )
    mainnet = _StubNetworkClient(chain=None, testnet=False)
    assert atomic_swap.chain_name("GRC", mainnet) == "main", (
        "and the mainnet answer still comes back as mainnet through the same route"
    )


def test_a_failed_probe_carries_the_daemons_own_words_into_the_refusal():
    """The MESSAGE, not just the exception type, so a -32601 can be told from a 401.

    `getblockchaininfo: RPCError` was all the refusal used to carry. That is the same
    ambiguity that sent the operator to check credentials which were fine when the real cause
    was a method the family does not have -- so the text of the error is now part of the
    reason, and this pins it.
    """
    class _Raises:
        def rpc_call(self, method, params=None):
            raise RuntimeError(f"{method}: code=-32601 message=Method not found")

    with pytest.raises(SwapError, match="code=-32601") as raised:
        atomic_swap.chain_name("GRC", _Raises())
    assert "RuntimeError" in str(raised.value), "the type is kept as well as the message"


def test_a_daemon_that_names_no_network_is_refused_rather_than_assumed():
    """Neither route answering is a REFUSAL, never a default. This file will not fund a contract
    on a chain it cannot name -- which is the same shape as UNKNOWN in address_network: "I could
    not tell" and "it is testnet" must never be one value."""
    with pytest.raises(SwapError, match="could not determine the network"):
        atomic_swap.chain_name("GRC", _StubNetworkClient(fail_blockchaininfo=True))
    with pytest.raises(SwapError, match="could not determine the network"):
        atomic_swap.chain_name("BTC", _StubNetworkClient())


def test_open_test_clients_refuses_a_mainnet_daemon_and_names_it(monkeypatch):
    """The gate, driven through the real function with a seeded client.

    A MAINNET daemon must stop the run at step 1 -- before a key is minted, before a secret
    exists, and long before anything is funded. On 2026-09-27 a `gridcoinresearchd getnewaddress`
    without -testnet put an address in the operator's live staking wallet, so this is the exact
    confusion the check exists for and it is one port number wide."""
    monkeypatch.setenv("GRC_RPC_PASS", "not-a-real-credential")
    monkeypatch.setitem(atomic_swap.CLIENTS, "GRC",
                        lambda url, user, password: _StubNetworkClient(testnet=False,
                                                                      fail_blockchaininfo=True))
    with pytest.raises(SwapError, match="REFUSING"):
        atomic_swap.open_test_clients(_step(), ("GRC",))

    monkeypatch.setitem(atomic_swap.CLIENTS, "GRC",
                        lambda url, user, password: _StubNetworkClient(testnet=True,
                                                                      fail_blockchaininfo=True))
    clients = atomic_swap.open_test_clients(_step(), ("GRC",))
    assert set(clients) == {"GRC"}


def test_a_dry_run_says_what_it_proved_and_what_it_did_not(capsys):
    """A DRY RUN THAT CANNOT FAIL TEACHES NOTHING, which is what the old one was: it printed
    three lines derived from its own arguments and never opened a socket. The operator's GRC dry
    run printed a clean plan against a daemon nobody had contacted, and two runs earlier the
    same clean plan preceded a connection-refused traceback.

    The distinction between PROVEN and NOT PROVEN is the whole value, so it is asserted -- a
    report claiming only the good half is how a dry run becomes a false reassurance."""
    console = Console(total_steps=8)
    assert atomic_swap.report_dry_run(console) == 0
    printed = capsys.readouterr().out
    assert "nothing was funded" in printed
    assert "PROVEN: both daemons answered" in printed
    assert "NOT PROVEN" in printed
    for absent in ("spendable balance", "unlock", "create_contract"):
        assert absent in printed, f"the report must name {absent!r} as unproven"


class _RefusesToFund:
    """A daemon that answers every READ the dry run makes and EXPLODES on any write.

    The assertion is the absence: create_contract, sendtoaddress and sendrawtransaction raise,
    so a dry run that funds anything fails loudly rather than being caught by a later check.
    Rule 13's shape -- "a stop that cannot prove it worked is not a stop" -- applied to funding:
    the proof is that the irreversible call was never reachable, not that a flag was read.
    """

    def __init__(self, *, chain: str, tip: int) -> None:
        self.chain = chain
        self.tip = tip
        self.reads: list[str] = []

    def rpc_call(self, method: str, params=None):
        self.reads.append(method)
        if method == "getblockchaininfo":
            return {"chain": self.chain}
        if method == "getblockcount":
            return self.tip
        if method == "getnewaddress":
            return "a-wallet-address"
        if method in ("sendtoaddress", "sendrawtransaction", "walletpassphrase", "walletlock"):
            raise AssertionError(f"a dry run must not call {method}")
        raise AssertionError(f"unexpected rpc {method}")

    def create_contract(self, **kwargs):
        raise AssertionError(f"a dry run must not create a contract: {sorted(kwargs)}")

    def redeem_contract(self, *args, **kwargs):
        raise AssertionError("a dry run must not redeem")


def test_a_dry_run_reaches_the_ordering_check_and_funds_nothing(monkeypatch, capsys):
    """THE PROPERTY THAT MATTERS, and a mutation check is why it exists: deleting the
    `if not args.run: return` so a dry run funds both legs anyway killed no test.

    Driven through main() with a real argv, because the defect class here is plumbing -- the
    thing that has broken on the operator's first run three times today. The stubs raise on
    every write, so this asserts the ABSENCE of funding rather than the presence of a flag."""
    tips = {"BTC": 1647, "LTC": 3657}
    made: dict[str, _RefusesToFund] = {}

    def factory(asset):
        def build(url, user, password):
            made[asset] = _RefusesToFund(chain="regtest", tip=tips[asset])
            return made[asset]
        return build

    for asset in ("BTC", "LTC"):
        monkeypatch.setenv(f"{asset}_RPC_PASS", "not-a-real-credential")
        monkeypatch.setitem(atomic_swap.CLIENTS, asset, factory(asset))
    monkeypatch.setattr(sys, "argv", [
        "atomic_swap.py", "--from", "BTC", "--to", "LTC",
        "--from-amount", "0.01", "--to-amount", "0.5",
    ])

    assert atomic_swap.main() == 0
    printed = capsys.readouterr().out

    # It got far enough to be worth something: both networks named, both tips read, the
    # ordering judged.
    assert "BTC network" in printed and "LTC network" in printed
    assert "participant expires FIRST" in printed
    assert "DRY RUN COMPLETE" in printed
    for asset in ("BTC", "LTC"):
        assert "getblockcount" in made[asset].reads, f"{asset}'s tip was never read"
    # And nothing was funded -- proven by the stubs never being asked to.
    assert all("sendtoaddress" not in client.reads for client in made.values())


def test_a_dry_run_still_refuses_a_mainnet_daemon_before_reading_a_tip(monkeypatch):
    """A dry run is read-only, so it is tempting to let it look at mainnet. It must not: the
    locktimes it reports would be derived from a mainnet tip and read as a rehearsal of
    something safe. It stops at step 1, before a tip is read."""
    client = _RefusesToFund(chain="main", tip=900_000)
    monkeypatch.setenv("BTC_RPC_PASS", "not-a-real-credential")
    monkeypatch.setenv("LTC_RPC_PASS", "not-a-real-credential")
    monkeypatch.setitem(atomic_swap.CLIENTS, "BTC", lambda url, user, password: client)
    monkeypatch.setattr(sys, "argv", [
        "atomic_swap.py", "--from", "BTC", "--to", "LTC",
        "--from-amount", "0.01", "--to-amount", "0.5",
    ])
    assert atomic_swap.main() == 1, "a mainnet daemon must make the run fail, not merely warn"
    assert "getblockcount" not in client.reads, "it must refuse before deriving a locktime"
