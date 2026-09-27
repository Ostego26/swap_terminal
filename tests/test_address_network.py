"""A network is a version byte. Every test here exists because a first letter was trusted.

Role: test (pure; no chain, no network, no database)
Reads: modules/address_network.py
Writes: nothing
Can move funds: no
Mainnet-safe: yes
Live-safe: yes

THE MEASUREMENT THAT MADE THIS MODULE NECESSARY, so the next reader does not have to re-take
it. Encoding 200,000 random hash160s under each Gridcoin version byte:

    0x3E mainnet   S  86.92%   R  13.08%
    0x6F testnet   m  83.39%   n  16.61%

Six places in this tree decided a network by the first character, on the stated claim that "a
mainnet Gridcoin address starts with S". 13.08% of them do not. On 2026-09-27 a real
`gridcoinresearchd getnewaddress` without `-testnet` produced
RyyNX8E7tDRzACL47h8JKKDUXf9aMCz8YV in the operator's live staking wallet, and every one of
those six checks would have called it testnet.
"""

from __future__ import annotations

import collections
import os
import pathlib
import sys

import base58
import pytest

REPOSITORY_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "swap_terminal"))

from modules.address_network import (  # noqa: E402  the path shim above must run first
    MAINNET,
    P2PKH_VERSIONS,
    P2SH_VERSIONS,
    TESTNET,
    TESTNET_P2PKH_VERSION,
    TESTNET_P2SH_VERSION,
    UNKNOWN,
    address_network,
    is_testnet_address,
)
from modules.atomic_htlc_scripts import (  # noqa: E402  same
    TESTNET_P2PKH_VERSION as SCRIPTS_P2PKH,
)
from modules.atomic_htlc_scripts import (  # noqa: E402  same
    TESTNET_P2SH_VERSION as SCRIPTS_P2SH,
)
from regtest.keys import TESTNET_P2PKH_VERSION as KEYS_P2PKH  # noqa: E402  same

# The address that was actually created in the operator's MAINNET wallet on 2026-09-27. Kept
# as a literal because a regression here is not hypothetical -- this string is the incident.
THE_MAINNET_ACCIDENT = "RyyNX8E7tDRzACL47h8JKKDUXf9aMCz8YV"


def _encode(version: int, hash160: bytes) -> str:
    return base58.b58encode_check(bytes([version]) + hash160).decode()


def test_the_address_that_was_actually_created_decodes_as_mainnet():
    """The incident, pinned. A `getnewaddress` with no -testnet put this in the wallet holding
    the operator's real staking balance, and it starts with R -- so the old
    `not startswith("S")` check called it testnet, which is what it was written to prevent."""
    network, why = address_network(THE_MAINNET_ACCIDENT)
    assert network == MAINNET, why
    assert "0x3e" in why, "the reason must name the byte, because that is what settles it"
    assert not is_testnet_address(THE_MAINNET_ACCIDENT)
    # The false premise, stated as a test so it cannot be re-derived from the string's shape.
    assert not THE_MAINNET_ACCIDENT.startswith("S"), (
        "this is the whole point: a mainnet GRC address need not start with S"
    )


def test_mainnet_gridcoin_addresses_start_with_r_about_an_eighth_of_the_time():
    """THE DENOMINATOR MATTERS (rule 3), so it is asserted rather than described.

    5,000 samples here rather than the 200,000 used to get 13.08%, because a test should be
    fast; the bound is loose enough that 5,000 samples cannot flake it and tight enough that it
    fails if R ever stops being common. What it pins is the claim itself: R is not a rare
    curiosity, it is roughly one address in eight."""
    samples = 5000
    leading = collections.Counter(_encode(0x3E, os.urandom(20))[0] for _ in range(samples))
    assert set(leading) <= {"R", "S"}, f"unexpected leading characters: {sorted(leading)}"
    assert 0.08 < leading["R"] / samples < 0.19, (
        f"R was {leading['R']}/{samples}; measured at 200,000 samples it is 13.08%"
    )
    # Every single one of them decodes as mainnet regardless of which letter it drew.
    for _ in range(200):
        assert address_network(_encode(0x3E, os.urandom(20)))[0] == MAINNET


def test_no_testnet_address_ever_starts_with_s():
    """The other half of why the old check answered backwards: it REJECTED real testnet
    addresses as well as accepting mainnet ones. `startswith("S")` is False for every address
    the 0x6F byte can produce, so a stub using it as `validate_address` refused every genuine
    testnet address handed to it."""
    for _ in range(2000):
        address = _encode(0x6F, os.urandom(20))
        assert address[0] in {"m", "n"}
        assert not address.startswith("S")
        assert is_testnet_address(address)


def test_an_undecodable_address_is_unknown_and_not_testnet():
    """UNKNOWN is its own answer, and is_testnet_address() is False for it.

    "I could not tell" and "it is testnet" must never be the same value -- that is rule 2's
    "I could not find a caller is not there is no caller" applied to an address. Defaulting
    UNKNOWN to testnet is exactly the 2026-09-27 failure; defaulting it to mainnet would block
    legitimate work. So it is neither, and the reason says which case it hit."""
    for bad, expected_in_why in (
        ("", "non-empty"),
        ("   ", "non-empty"),
        (None, "non-empty"),
        ("not base58 at all!!!", "base58check"),
        (THE_MAINNET_ACCIDENT[:-1] + "X", "base58check"),          # broken checksum
        (base58.b58encode_check(b"\x99" + os.urandom(20)).decode(), "no table"),
        (base58.b58encode_check(b"\x6f" + os.urandom(10)).decode(), "bytes"),  # wrong length
    ):
        network, why = address_network(bad)
        assert network == UNKNOWN, f"{bad!r} should be UNKNOWN, got {network} ({why})"
        assert expected_in_why in why, f"{bad!r}: reason {why!r} does not name the problem"
        assert is_testnet_address(bad) is False


def test_the_testnet_byte_is_one_entry_because_three_chains_share_it():
    """BTC, LTC and GRC testnet all use 0x6F, so an address decoded from it is "some testnet".

    This is asserted because the tempting next change is to make this map to a TICKER, and the
    encoding does not carry one -- a module claiming to tell BTC testnet from GRC testnet by
    version byte would be claiming something that is not in the bytes."""
    assert P2PKH_VERSIONS[0x6F] == TESTNET
    assert P2SH_VERSIONS[0xC4] == TESTNET
    assert TESTNET_P2PKH_VERSION == b"\x6f"
    assert TESTNET_P2SH_VERSION == b"\xc4"
    assert sum(1 for value in P2PKH_VERSIONS.values() if value == TESTNET) == 1


@pytest.mark.parametrize(
    ("version", "expected"),
    [(0x00, MAINNET), (0x30, MAINNET), (0x3E, MAINNET), (0x6F, TESTNET)],
)
def test_every_declared_p2pkh_version_decodes_to_its_network(version, expected):
    """Round-trips each entry through the real encoder, so a typo in the table is caught by
    behavior rather than by reading the table back to itself."""
    assert address_network(_encode(version, os.urandom(20)))[0] == expected


def test_the_two_older_copies_of_the_testnet_byte_now_derive_from_this_one():
    """Rule 8: modules/atomic_htlc_scripts.py and regtest/keys.py each declared this byte.
    Asserted by IDENTITY rather than equality -- two independent `b"\\x6f"` literals are equal,
    so equality would pass for exactly the duplication this is checking for."""
    assert SCRIPTS_P2PKH is TESTNET_P2PKH_VERSION
    assert SCRIPTS_P2SH is TESTNET_P2SH_VERSION
    assert KEYS_P2PKH is TESTNET_P2PKH_VERSION
