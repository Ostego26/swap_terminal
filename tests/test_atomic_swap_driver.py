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
from modules.htlc_timelock import ROLE_INITIATOR, ROLE_PARTICIPANT  # noqa: E402  same
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
        self.claims.append({"txid": txid, "vout": vout, "secret_hex": secret_hex,
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
