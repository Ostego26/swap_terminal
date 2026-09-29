"""The one implementation of "fund an HTLC on a script chain", and what it never leaks.

Role: test
Reads: nothing. The client here is a recorder.
Writes: nothing.
Can send orders: no
Live-safe: yes
"""

from __future__ import annotations

import ast
import sys
from decimal import Decimal
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "swap_terminal"))

from modules.script_leg import (
    ScriptLegKeys,
    claim_the_script_leg,
    fund_the_script_leg,
    mint_leg_keys,
)

SECRET = bytes(range(32))
SECRET_HASH = "ab" * 32


class RecordingClient:
    """Records what a client was asked to do. Returns the clients' own dict shape."""

    def __init__(self):
        self.created, self.redeemed = [], []

    def create_contract(self, **kwargs):
        """KEYWORD-ONLY, because that is how the real call is made now.

        THIS STUB IS WHY THE SUITE DID NOT CATCH THE LTC DEFECT. It took the five
        arguments POSITIONALLY in the BTC/GRC order, so it accepted the positional
        call fund_the_script_leg() was making and recorded what that call meant to
        say -- while LTCClient, whose parameters are in a different order with
        secret_hash last and defaulting to None, would have received the secret
        hash as a participant address. A double that accepts a call the real thing
        rejects is a test asserting on a conversation that never happens.

        The arity check that used to live in the signature is now
        test_the_kwargs_BIND_to_every_real_clients_signature, which binds against
        the actual client classes instead of against a stub's idea of them.
        """
        amount = next(value for key, value in kwargs.items() if key.startswith("amount"))
        self.created.append((amount, kwargs["secret_hash"], kwargs["participant_address"],
                             kwargs["refund_address"], kwargs["locktime"]))
        self.create_kwargs = dict(kwargs)
        return {"txid": "cc" * 32, "vout": 1, "redeemScript": b"\x51", "p2shAddress": "2NFake"}

    def redeem_contract(self, txid, vout, redeem_script, secret, privkey, destination, blockhash=None):  # noqa: PLR0913, PLR0917 -- checked: this stub MIRRORS the real signature, which modules/atomic_btc_client.py:352 carries with its own checked suppression for the same seven. Reshaping it here (or taking *args) would make the test stop testing the interface: a call with the wrong arity would be silently accepted, which is the one thing a recorder exists to catch.
        self.redeemed.append((txid, vout, redeem_script, secret, privkey, destination))
        return "dd" * 32


# ---------------------------------------------------------------------------
# The keys. Minted here, never read out of a wallet.
# ---------------------------------------------------------------------------
def test_the_two_branches_get_DIFFERENT_keys():
    """build_htlc_redeem_script() REFUSES a script whose branches hash to one key -- a
    guard added after a contract was built that nobody could claim. Two independently
    generated keys means that state cannot be constructed here at all."""
    keys = mint_leg_keys()
    assert keys.claim_address != keys.refund_address


def test_the_keys_are_testnet_P2PKH_on_every_chain_this_drives():
    """Version byte 0x6F is testnet P2PKH for Bitcoin, Litecoin AND Gridcoin alike, which
    is what makes one generator chain-generic. m/n prefixes are what that encodes to."""
    keys = mint_leg_keys()
    for address in (keys.claim_address, keys.refund_address):
        assert address[0] in "mn", f"{address} is not a testnet P2PKH address"


def test_minting_twice_does_not_repeat():
    assert mint_leg_keys().claim_address != mint_leg_keys().claim_address


def test_THE_KEYS_OBJECT_HAS_NO_STRINGIFICATION_THAT_WOULD_MAKE_PRINTING_SAFE():
    """A DELIBERATE ABSENCE, asserted so nobody adds one as a convenience.

    CLAUDE.md's chain-safety rules say never move, copy or read back a key. A __str__ or
    __repr__ override that redacted the WIF would be a promise every future field has to
    keep, and the first field added without thinking about it breaks it silently. There is
    no override; the rule lives at the call sites, which take .claim_address.

    What this pins is that the dataclass's own repr is the plain one -- so a careless
    print is obviously a leak rather than a redacted-looking string that hides one.
    """
    assert "__str__" not in ScriptLegKeys.__dict__
    assert "__repr__" not in vars(ScriptLegKeys) or ScriptLegKeys.__repr__.__qualname__.startswith("ScriptLegKeys")


# ---------------------------------------------------------------------------
# Funding: which branch is which.
# ---------------------------------------------------------------------------
def test_the_CLAIM_key_is_the_participant_branch_and_the_REFUND_key_is_the_refund_branch():
    """GETTING THIS BACKWARDS BUILDS A CONTRACT THE WRONG PARTY CAN TAKE, and both
    arguments are addresses so nothing downstream would object. The clients take them
    positionally as (participant_address, refund_address); this asserts the order."""
    client, keys = RecordingClient(), mint_leg_keys()
    fund_the_script_leg(client, Decimal("0.5"), SECRET_HASH, keys, 900, chain="GRC")
    (_amount, _hash, participant, refund, _locktime) = client.created[0]
    assert participant == keys.claim_address
    assert refund == keys.refund_address


def test_the_amount_the_locktime_and_the_hash_reach_the_client_unchanged():
    client, keys = RecordingClient(), mint_leg_keys()
    fund_the_script_leg(client, Decimal("1.25"), SECRET_HASH, keys, 1234, chain="GRC")
    amount, secret_hash, _p, _r, locktime = client.created[0]
    assert amount == Decimal("1.25") and secret_hash == SECRET_HASH and locktime == 1234


def test_the_clients_OWN_dict_shape_comes_back_rather_than_a_reshaped_one():
    """A second vocabulary for one thing is rule 8 in miniature: a reader diffing this
    against atomic_swap.py must see the same keys."""
    contract = fund_the_script_leg(RecordingClient(), Decimal(1), SECRET_HASH, mint_leg_keys(), 1, chain="GRC")
    assert set(contract) == {"txid", "vout", "redeemScript", "p2shAddress"}


# ---------------------------------------------------------------------------
# Claiming: the preimage, and the key that signs it.
# ---------------------------------------------------------------------------
def test_the_claim_signs_with_the_CLAIM_key_and_never_the_refund_one():
    """The refund key cannot spend the hashlock branch. Signing with it produces a
    well-formed transaction the script interpreter rejects -- which surfaces as a generic
    script failure, the hardest kind to diagnose."""
    client, keys = RecordingClient(), mint_leg_keys()
    contract = fund_the_script_leg(client, Decimal(1), SECRET_HASH, keys, 1, chain="GRC")
    claim_the_script_leg(client, contract, SECRET, keys, "mDestination")
    (_txid, _vout, _script, _secret, privkey, _dest) = client.redeemed[0]
    assert privkey == keys.claim.wif
    assert privkey != keys.refund.wif


def test_THE_PREIMAGE_IS_PASSED_THROUGH_because_publishing_it_IS_the_mechanism():
    """A claim that succeeded without pushing the preimage would be a swap where one
    party takes both legs. The secret was accepted and IGNORED by redeem_contract until
    2026-09-25 (defect 1 in atomic_btc_client's header), so this is pinned rather than
    assumed."""
    client, keys = RecordingClient(), mint_leg_keys()
    contract = fund_the_script_leg(client, Decimal(1), SECRET_HASH, keys, 1, chain="GRC")
    claim_the_script_leg(client, contract, SECRET, keys, "mDestination")
    assert client.redeemed[0][3] == SECRET


def test_the_funded_outpoint_and_script_reach_the_claim_unchanged():
    client, keys = RecordingClient(), mint_leg_keys()
    contract = fund_the_script_leg(client, Decimal(1), SECRET_HASH, keys, 1, chain="GRC")
    claim_the_script_leg(client, contract, SECRET, keys, "mDestination")
    txid, vout, script, _secret, _key, destination = client.redeemed[0]
    assert (txid, vout, script) == (contract["txid"], contract["vout"], contract["redeemScript"])
    assert destination == "mDestination"


def test_a_string_vout_is_coerced_rather_than_passed_to_a_signer_as_text():
    """Some daemons answer a numeric field as a string. An int is what the spend needs."""
    client, keys = RecordingClient(), mint_leg_keys()
    contract = {"txid": "ee" * 32, "vout": "2", "redeemScript": b"\x51", "p2shAddress": "2N"}
    claim_the_script_leg(client, contract, SECRET, keys, "mDestination")
    assert client.redeemed[0][1] == 2


def test_this_module_NEVER_SPEAKS_RPC_ITSELF():
    """Rule 8, asserted structurally rather than by grepping for a method name.

    The first version of this test searched the source for "createhtlc" and FAILED -- on
    the module's own docstring, which explains at length why that RPC is not called here.
    Matching text is exactly the error this repository warns about elsewhere ("verify by
    behavior, never by SQL text"), arriving in a test written to prevent a different one.

    The precise claim is structural and has no false positive: script_leg.py composes the
    chain clients and must never reach an adapter directly, so `.call(` appears nowhere in
    its CODE. Checked by parsing rather than by reading, so a docstring can say the word
    as often as it needs to.
    """
    source = (Path(__file__).resolve().parent.parent / "swap_terminal" / "modules" / "script_leg.py")
    tree = ast.parse(source.read_text())
    attribute_calls = [
        node.func.attr for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    ]
    assert "call" not in attribute_calls, (
        "script_leg.py reaches an adapter directly; it must go through the chain clients, "
        "which is the entire reason it covers three chains instead of one"
    )
    assert {"create_contract", "redeem_contract"} <= set(attribute_calls), (
        "the client interface is what makes this chain-generic; it must still be used"
    )


@pytest.mark.parametrize("missing", ["txid", "vout", "redeemScript"])
def test_a_contract_missing_a_field_the_claim_needs_fails_LOUDLY(missing):
    """A KeyError naming the field beats a None reaching a signer, which produces a
    transaction the chain refuses for a reason that names nothing."""
    client, keys = RecordingClient(), mint_leg_keys()
    contract = {"txid": "ee" * 32, "vout": 0, "redeemScript": b"\x51", "p2shAddress": "2N"}
    del contract[missing]
    with pytest.raises(KeyError, match=missing):
        claim_the_script_leg(client, contract, SECRET, keys, "mDestination")
