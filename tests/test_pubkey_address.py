"""A chain address derived from a public key, checked against a daemon AND the chain.

Role: test (pure functions; opens no socket, touches no database, signs nothing)
Reads: swap_terminal/modules/pubkey_address.py
Writes: nothing
Can move funds: no
Mainnet-safe: yes

NO ADDRESS LITERAL APPEARS IN THIS FILE, DELIBERATELY. The obvious test is
`assert derived == "mg3gJAmh..."`, and tests/test_address_literals_are_valid.py
caps address literals for good reasons -- it caught four of mine earlier the same
day and turned the branch red. Writing one here would be the third time.

So the assertion pins something stronger instead: the HASH160, in hex, which is
not an address and which came off the CHAIN rather than out of this repository.
"This address pays the script the chain recorded" is a better statement than
"this string equals that string", and it needs no literal to say it.
"""

from __future__ import annotations

import sys
from pathlib import Path

import base58
import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "swap_terminal"))

from modules.address_network import MAINNET, TESTNET  # noqa: E402
from modules.pubkey_address import (  # noqa: E402
    P2PKH_VERSION_FOR,
    PublicKeyRefused,
    address_from_public_key,
    network_of_version,
)

# MEASURED 2026-10-05 ON THE OPERATOR'S HOST, and both halves came from outside
# this repository, which is the only property that makes them worth asserting.
#
#   PUBKEY           `gridcoinresearchd -testnet validateaddress` reported it
#                    beside the address it belongs to.
#   ONCHAIN_HASH160  the OP_HASH160 in the scriptPubKey of all four GRC payouts
#                    the terminal broadcast that day -- txids e090712c, b278a236,
#                    35f565e2 and c82f9e37, each paying this same script.
#
# The wallet and the ledger are two different authorities and neither is this
# code, so agreeing with both is a real check rather than a tautology.
PUBKEY = bytes.fromhex("037b3a84fc93ee1518ac7c0bb4485c4410b01df632d5fb480be5bef30aa6d1fdc9")
ONCHAIN_HASH160 = "05cf8a542bd038401e9d692712bb1d8082c62f56"


def test_the_derived_address_pays_the_script_the_chain_recorded():
    """MUTATION: hash the key once (SHA256 only) instead of RIPEMD160(SHA256).

    That mutation produces a perfectly well-formed base58check address of the
    right length and the right prefix. Nothing about its SHAPE is wrong -- which
    is exactly why the assertion is on the hash160 the chain recorded and not on
    the address's form. A payout to the mutated address would be unspendable.
    """
    derived = address_from_public_key(PUBKEY, "GRC", TESTNET)
    payload = base58.b58decode_check(derived)

    assert payload[0] == 0x6F, "GRC testnet P2PKH version byte"
    assert payload[1:].hex() == ONCHAIN_HASH160, (
        "the derived address must pay the same hash160 the GRC chain recorded in "
        "every payout this terminal broadcast on 2026-10-05"
    )


def test_one_key_yields_a_different_address_on_every_chain():
    """The property the whole threshold-custody idea rests on.

    One secp256k1 key controls funds on BTC, LTC and GRC alike; only the version
    byte differs. If two of these collided, an address derived for one chain would
    be accepted as the other's and a payout could be sent to the wrong network.
    """
    addresses = {
        (asset, network): address_from_public_key(PUBKEY, asset, network)
        for asset, network in P2PKH_VERSION_FOR
    }
    assert len(set(addresses.values())) == len(set(P2PKH_VERSION_FOR.values())), (
        "two (asset, network) pairs sharing a version byte MUST share an address -- "
        "0x6F is BTC, LTC and GRC testnet alike -- and distinct bytes must not collide"
    )
    # Every address decodes to the same hash160: it is one key.
    for address in addresses.values():
        assert base58.b58decode_check(address)[1:].hex() == ONCHAIN_HASH160


def test_the_forward_table_agrees_with_the_reverse_one():
    """The two version tables are held consistent by this, not by a comment.

    modules/address_network.P2PKH_VERSIONS maps version -> network and is
    one-to-many in the direction encoding needs, so pubkey_address carries its own
    (asset, network) -> version table. Rule 8 tolerates the second table because
    it genuinely answers a different question; it does not tolerate the two
    drifting, and this is what stops that.
    """
    for (asset, network), version in P2PKH_VERSION_FOR.items():
        assert network_of_version(version) == network, (
            f"{asset}/{network} claims version 0x{version:02x}, which address_network "
            f"says is {network_of_version(version)}"
        )


def test_an_uncompressed_key_is_REFUSED_rather_than_quietly_derived_from():
    """The failure that would be invisible: a valid-looking address nobody holds.

    A 65-byte uncompressed key hashes to a DIFFERENT hash160 than its own 33-byte
    compressed form, so the same key yields two addresses and the funds are at one
    of them. Deriving from the wrong one produces an address that passes every
    checksum and every `validateaddress` -- and is unspendable. That is the
    82.65 tGRC failure this repository already paid for once, reached by a
    different road.
    """
    uncompressed = bytes([0x04]) + bytes(64)
    with pytest.raises(PublicKeyRefused, match="compressed secp256k1"):
        address_from_public_key(uncompressed, "BTC", MAINNET)

    with pytest.raises(PublicKeyRefused):
        address_from_public_key(PUBKEY[:-1], "BTC", MAINNET)

    with pytest.raises(PublicKeyRefused, match="must be bytes"):
        address_from_public_key(PUBKEY.hex(), "BTC", MAINNET)


def test_an_unknown_chain_or_network_refuses_and_names_what_it_knows():
    """Rule 14: the refusal says what IS available, so the reader can act on it."""
    with pytest.raises(PublicKeyRefused, match="no P2PKH version byte"):
        address_from_public_key(PUBKEY, "SOL", MAINNET)
    with pytest.raises(PublicKeyRefused, match="BTC/mainnet"):
        address_from_public_key(PUBKEY, "GRC", "regtest-but-spelled-wrong")
