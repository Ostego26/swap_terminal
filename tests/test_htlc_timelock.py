"""Which locktime goes into a contract, and which side gets the longer one.

Role: test / measurement (runs the real decision function and the real
      Swapper.start_swap with stub clients)
Reads: swap_terminal/modules/htlc_timelock.py,
      swap_terminal/modules/atomic_swapper.py
Writes: nothing
Can move funds: no. The clients here are stubs that record the arguments they
        were called with. No socket is opened, nothing is signed, nothing is
        broadcast, and no price is fetched over the network -- the two
        CoinGecko calls are replaced by fixed values.
Mainnet-safe: yes

WHAT THIS FILE ESTABLISHES, AND WHAT IT CANNOT.

Until 2026-09-24 `Swapper.start_swap()` passed `locktime=500000` in all six
swap directions and passed the initiator's OWN address as both the participant
and the refund address. Block 500,000 is in the past on BTC (December 2017) and
on LTC, so once the encoder defect was corrected -- and it was corrected in the
same commit -- that literal would have produced a contract refundable by the
initiator the instant it was funded, while they could still redeem the
counterparty's leg with the preimage. Both legs, one party.

So these tests assert on the three things that replaced it:

  1. the locktime is derived from the FUNDED chain's own tip, per swap;
  2. the initiator's timelock is strictly longer than the participant's, on
     every chain, which is what stops the refund-and-redeem attack above;
  3. the participant and refund addresses reach the client as two different
     values, and start_swap refuses to run if they are the same.

What it does NOT establish is anything about a chain. No contract built from
these values has been funded, redeemed or refunded anywhere -- this machine has
no chain access -- and the refund branch in particular has never been
exercised. That is the proof CLAUDE.md's verification section asks for and it
is not in this file (rule 17).
"""

from decimal import Decimal

import pytest
from modules import atomic_swapper
from modules.atomic_swapper import Swapper
from modules.htlc_timelock import (
    INITIATOR_LOCK_HOURS,
    LOCKTIME_THRESHOLD,
    PARTICIPANT_LOCK_HOURS,
    ROLE_INITIATOR,
    ROLE_PARTICIPANT,
    SECONDS_PER_BLOCK,
    contract_locktime,
    describe_locktime,
    timelock_blocks,
)

ASSETS = sorted(SECONDS_PER_BLOCK)

# Two distinct testnet addresses. Their format does not matter here: the stub
# client records them without parsing, and the real script builder's address
# handling is tested in tests/test_htlc_locktime_encoding.py.
COUNTERPARTY = "mg3G6MkQSxMp1iCUYFH5JztynohhGYHPDA"
OURS = "mkHS9ne12qx9pS9VojpwU5xtRd4T7X7ZUt"


class StubClient:
    """Records create_contract's arguments instead of funding anything."""

    def __init__(self, tip: int):
        self.tip = tip
        self.calls: list[dict] = []
        self.rpc_calls: list[str] = []

    def rpc_call(self, method: str, params=None):
        self.rpc_calls.append(method)
        if method == "getblockcount":
            return self.tip
        raise AssertionError(f"the stub was asked for {method!r}, which this path should not need")

    def create_contract(self, **kwargs):
        self.calls.append(kwargs)
        return {"txid": "stub-txid", "vout": 0, "p2shAddress": "2-stub-address"}


@pytest.fixture
def swapper_with_stubs(monkeypatch):
    """A Swapper whose three clients are stubs and whose prices are fixed.

    The price functions are replaced rather than allowed to reach CoinGecko:
    a test that needs the network is a test that fails for reasons that have
    nothing to do with what it asserts.
    """
    monkeypatch.setattr(atomic_swapper, "fetch_btc_ltc_prices", lambda **_: {"BTC": "100000", "LTC": "100"})
    monkeypatch.setattr(atomic_swapper, "fetch_grc_price", lambda **_: "0.01")
    clients = {"BTC": StubClient(900_000), "LTC": StubClient(2_800_000), "GRC": StubClient(3_900_000)}
    return Swapper(clients["BTC"], clients["LTC"], clients["GRC"]), clients


@pytest.mark.parametrize("asset", ASSETS)
def test_the_initiators_timelock_is_strictly_longer_than_the_participants(asset):
    """The asymmetry, on every chain. This is the invariant, not a preference.

    If the initiator's lock expired first they could refund their own leg while
    still able to redeem the counterparty's. The ratio is 2:1 in hours; what is
    asserted here is the direction, because that is what makes the swap atomic.
    """
    assert timelock_blocks(asset, ROLE_INITIATOR) > timelock_blocks(asset, ROLE_PARTICIPANT)
    tip = 900_000
    assert contract_locktime(asset, ROLE_INITIATOR, tip) > contract_locktime(asset, ROLE_PARTICIPANT, tip)
    assert INITIATOR_LOCK_HOURS > PARTICIPANT_LOCK_HOURS


@pytest.mark.parametrize(
    ("asset", "role", "expected_blocks"),
    [
        # hours * 3600 / target block interval, per chain. Written out rather
        # than recomputed from the constants, so that an edit to a constant
        # fails here instead of agreeing with itself.
        ("BTC", ROLE_INITIATOR, 288),  # 48h at 600s
        ("BTC", ROLE_PARTICIPANT, 144),  # 24h at 600s
        ("LTC", ROLE_INITIATOR, 1152),  # 48h at 150s
        ("LTC", ROLE_PARTICIPANT, 576),
        ("GRC", ROLE_INITIATOR, 1920),  # 48h at 90s
        ("GRC", ROLE_PARTICIPANT, 960),
    ],
)
def test_the_block_counts_are_per_chain_and_not_one_number_for_all_three(asset, role, expected_blocks):
    """48 hours is 288 blocks on BTC and 1920 on GRC. Blocks are never µfn (rule 6)."""
    assert timelock_blocks(asset, role) == expected_blocks


def test_the_locktime_is_the_tip_plus_the_blocks_and_is_a_height():
    """Derived per swap from the chain tip, and always below the threshold.

    500,000,000 is LOCKTIME_THRESHOLD: below it a value is a block height, at
    or above it a unix timestamp, and CHECKLOCKTIMEVERIFY refuses to compare
    across it -- which would be discovered at refund time.
    """
    assert contract_locktime("BTC", ROLE_INITIATOR, 900_000) == 900_288
    assert contract_locktime("BTC", ROLE_INITIATOR, 900_000) < LOCKTIME_THRESHOLD


def test_a_tip_that_is_actually_a_timestamp_is_refused():
    """A unix timestamp passed as a height crosses the threshold, so it raises."""
    with pytest.raises(ValueError, match="LOCKTIME_THRESHOLD"):
        contract_locktime("BTC", ROLE_INITIATOR, 1_735_689_600)
    with pytest.raises(ValueError, match="positive block height"):
        contract_locktime("BTC", ROLE_INITIATOR, 0)


def test_unknown_assets_and_roles_raise_rather_than_defaulting():
    """A typo must not silently select the shorter lock for the initiator's leg."""
    with pytest.raises(ValueError, match="unknown asset"):
        timelock_blocks("DOGE", ROLE_INITIATOR)
    with pytest.raises(ValueError, match="unknown HTLC role"):
        timelock_blocks("BTC", "iniator")


def test_describe_locktime_says_blocks_and_never_microfortnights():
    """Rule 6: blocks and confirmations are not times and are never converted."""
    line = describe_locktime("BTC", ROLE_INITIATOR, 900_000, 900_288)
    assert "900288" in line
    assert "+288 blocks" in line
    assert "µfn" not in line
    assert "ufn" not in line


@pytest.mark.parametrize(
    ("direction", "funded"),
    [
        ("BTC2LTC", "BTC"),
        ("LTC2BTC", "LTC"),
        ("BTC2GRC", "BTC"),
        ("GRC2BTC", "GRC"),
        ("LTC2GRC", "LTC"),
        ("GRC2LTC", "GRC"),
    ],
)
def test_start_swap_funds_the_right_chain_with_a_derived_locktime(swapper_with_stubs, direction, funded):
    """All six directions, through the real start_swap, against stub clients.

    Asserts what reaches create_contract: the amount under that client's own
    keyword, a locktime that is the FUNDED chain's tip plus the initiator's
    blocks -- not 500000, and not another chain's tip -- and the two addresses
    as two different values.
    """
    swapper, clients = swapper_with_stubs
    swapper.start_swap(
        swap_direction=direction,
        participant_address=COUNTERPARTY,
        refund_address=OURS,
        swap_amount=Decimal("0.001"),
    )

    client = clients[funded]
    assert len(client.calls) == 1
    call = client.calls[0]
    assert "getblockcount" in client.rpc_calls

    expected = client.tip + timelock_blocks(funded, ROLE_INITIATOR)
    assert call["locktime"] == expected
    assert call["locktime"] != 500_000, "the hardcoded height this change exists to remove"
    assert call["participant_address"] == COUNTERPARTY
    assert call["refund_address"] == OURS
    assert call["participant_address"] != call["refund_address"]
    assert call[f"amount_{funded.lower()}"] == Decimal("0.001")

    # And no other chain was funded.
    for asset, other in clients.items():
        if asset != funded:
            assert other.calls == []


def test_start_swap_refuses_when_both_addresses_are_the_same(swapper_with_stubs):
    """The third defect, refused before anything is fetched or broadcast."""
    swapper, clients = swapper_with_stubs
    with pytest.raises(ValueError, match="same value"):
        swapper.start_swap(
            swap_direction="BTC2LTC",
            participant_address=OURS,
            refund_address=OURS,
            swap_amount=Decimal("0.001"),
        )
    assert clients["BTC"].calls == []
    assert clients["BTC"].rpc_calls == [], "it refused before asking the daemon anything"


def test_start_swap_never_logs_the_preimage(swapper_with_stubs, caplog):
    """The secret hash is logged; the preimage is not. It cannot be un-revealed.

    The summary string DOES carry the preimage -- the initiator needs it to
    redeem the counterparty's leg and there is no other channel -- so the
    assertion is specifically about the log stream.
    """
    swapper, _ = swapper_with_stubs
    with caplog.at_level("DEBUG"):
        summary = swapper.start_swap(
            swap_direction="BTC2LTC",
            participant_address=COUNTERPARTY,
            refund_address=OURS,
            swap_amount=Decimal("0.001"),
        )
    secret_line = [line for line in summary.splitlines() if line.startswith("Secret: ")]
    assert len(secret_line) == 1
    preimage = secret_line[0].removeprefix("Secret: ")
    assert preimage
    assert preimage not in caplog.text
    assert "Secret hash: " in caplog.text


def test_an_unsupported_direction_raises_before_any_rpc(swapper_with_stubs):
    """A malformed direction must not reach a daemon or a contract."""
    swapper, clients = swapper_with_stubs
    for bad in ("BTC2BTC", "BTC2DOGE", "nonsense"):
        with pytest.raises(NotImplementedError):
            swapper.start_swap(
                swap_direction=bad,
                participant_address=COUNTERPARTY,
                refund_address=OURS,
                swap_amount=Decimal("0.001"),
            )
    assert all(client.calls == [] for client in clients.values())
