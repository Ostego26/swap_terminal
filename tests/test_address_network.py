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

import bech32  # noqa: E402  the path shim above must run first
import valid_addresses  # noqa: E402  same
from bip350_vectors import (  # noqa: E402  same
    BIP350_INVALID,
    BIP350_VALID,
    WITNESS_VERSION_OPCODE,
)
from modules.address_network import (  # noqa: E402  same
    BECH32_CHECKSUM_CONSTANT,
    BECH32M_CHECKSUM_CONSTANT,
    MAINNET,
    P2PKH_VERSIONS,
    P2SH_VERSIONS,
    TESTNET,
    TESTNET_P2PKH_VERSION,
    TESTNET_P2SH_VERSION,
    UNKNOWN,
    XRP_BASE58_ALPHABET,
    address_network,
    decode_segwit_address,
    decodes_as_address,
    is_testnet_address,
    is_valid_address,
    witness_program_refusal,
)
from modules.atomic_htlc_scripts import (  # noqa: E402  same
    TESTNET_P2PKH_VERSION as SCRIPTS_P2PKH,
)
from modules.atomic_htlc_scripts import (  # noqa: E402  same
    TESTNET_P2SH_VERSION as SCRIPTS_P2SH,
)
from regtest.keys import TESTNET_P2PKH_VERSION as KEYS_P2PKH  # noqa: E402  same
from valid_addresses import (  # noqa: E402  conftest puts tests/ on sys.path
    BTC_FUTURE_WITNESS_V5,
    BTC_TAPROOT_MAINNET,
    BTC_TAPROOT_PAYOUT,
    INVALID_GRC_BECH32,
    INVALID_PLACEHOLDERS,
    LTC_P2SH_SCRIPT_ADDRESS2,
    LTC_REGTEST_DEPOSIT,
    LTC_REGTEST_TAPROOT,
)

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


# ---------------------------------------------------------------------------------------
# VALIDITY, as distinct from network. Added after a mutation check found that four ways of
# loosening decodes_as_address() killed no test -- the gate in
# tests/test_address_literals_are_valid.py exercised it only against a tree that was already
# clean, which proves the tree and not the function.
# ---------------------------------------------------------------------------------------


def test_gridcoin_bech32_is_refused_because_no_such_format_exists():
    """Gridcoin is base58 ONLY. The tree contained a `tgrc1q...` address in
    atomic_grc_client.py's usage example, and a reader who copied it would have written an
    address no Gridcoin daemon can parse -- a fee paid there is burned, which is the same loss
    the PLATFORM_FEE_TESTNET_DEFAULT burn was.

    INVALID_GRC_BECH32 carries a VALID bech32 checksum (it is a real tb1 address with its hrp
    swapped), so the refusal cannot be passing for the wrong reason. The checksum is not the
    problem; the format does not exist on that chain."""
    decodable, why = decodes_as_address(INVALID_GRC_BECH32)
    assert not decodable, f"{INVALID_GRC_BECH32} was accepted: {why}"
    assert "no bech32" in why, f"the reason must say why, not merely refuse: {why}"
    # And the same string with a real hrp IS valid, which is what pins "the hrp is the reason".
    assert is_valid_address("tb1" + INVALID_GRC_BECH32.split("1", 1)[1])


def test_a_payload_that_is_not_twenty_one_bytes_is_refused():
    """A version byte plus a hash160 is 21 bytes. Anything else is a truncation or a different
    object entirely -- a WIF, a 32-byte key -- and accepting it means paying an address that
    cannot receive. The length is checked rather than assumed from the string's length, because
    base58 is not fixed-width."""
    for payload, label in ((b"\x6f" + b"\x00" * 10, "half a hash160"),
                           (b"\x6f" + b"\x00" * 31, "a 32-byte key"),
                           (b"\x6f", "a bare version byte")):
        address = base58.b58encode_check(payload).decode()
        decodable, why = decodes_as_address(address)
        assert not decodable, f"{label} was accepted as an address: {why}"
        assert "bytes" in why, f"the reason must name the length problem: {why}"


def test_bech32_hrps_map_to_the_network_they_actually_name():
    """bc and ltc are MAINNET; tb, bcrt and tltc are testnet. Asserted through the real encoder
    rather than by reading BECH32_HRPS back to itself, and it matters because every bech32
    address in this repository decoded as UNKNOWN until address_network learned hrps -- so a
    fixture test asserting "nothing is mainnet" passed for `bc1...` strings."""
    program = bytes(range(20))
    expected = {"bc": MAINNET, "ltc": MAINNET, "tb": TESTNET, "bcrt": TESTNET, "tltc": TESTNET}
    for hrp, network in expected.items():
        address = bech32.encode(hrp, 0, program)
        assert address is not None, f"could not encode an {hrp} address"
        got, why = address_network(address)
        assert got == network, f"{address} -> {got}, expected {network} ({why})"
        assert decodes_as_address(address)[0]


def test_an_hrp_this_repository_cannot_pay_is_unknown_rather_than_accepted():
    """A valid bech32 address on a chain we cannot reach is still an address the money never
    comes back from, so an unknown hrp is UNKNOWN -- never accepted on the strength of its
    checksum alone."""
    address = bech32.encode("doge", 0, bytes(range(20)))
    assert address is not None
    network, why = address_network(address)
    assert network == UNKNOWN, f"{address} -> {network} ({why})"


def test_an_xrp_address_is_not_a_bitcoin_address_and_the_reverse():
    """The two alphabets are the same 58 characters in a different order, so the checksum is over
    different bytes. Decoded under the wrong alphabet an address either fails its checksum or
    silently becomes a different key -- which is why the decoder TRIES both and reports which
    one held, rather than assuming from the leading character."""
    payload = b"\x00" + bytes(range(20))
    xrp = base58.b58encode_check(payload, alphabet=XRP_BASE58_ALPHABET).decode()
    btc = base58.b58encode_check(payload).decode()
    assert xrp != btc, "the same bytes encode differently under the two alphabets"
    assert decodes_as_address(xrp)[0] and "XRP" in decodes_as_address(xrp)[1]
    assert decodes_as_address(btc)[0] and "XRP" not in decodes_as_address(btc)[1]

    # THE TRY-ORDER DOES NOT MATTER, AND AN EARLIER VERSION OF THIS TEST IMPLIED IT DID.
    #
    # It asserted the BTC address is not reported as XRP and commented that this would break
    # "if the order of the two attempts stopped mattering". A mutation check swapped the order
    # and killed no test, so the claim was checked: encoding 40,000 payloads under one alphabet
    # and decoding under the other, ZERO also passed the wrong alphabet's checksum. Expected by
    # chance is 40000/2**32 = 0.0000093, because the checksum is four bytes.
    #
    # So the order is arbitrary by construction, not by luck, and that is a stronger property
    # than the one the old assertion pretended to hold: the decoder does not have to guess which
    # alphabet a string used, because at most one of them can validate it. The assertions above
    # pin the OUTCOME -- each address is reported under its own encoding -- which is true in
    # either order and is what a caller depends on.
    for candidate in (xrp, btc):
        decodable, why = decodes_as_address(candidate)
        assert decodable, why
    assert base58.b58decode_check(xrp, alphabet=XRP_BASE58_ALPHABET) == payload
    assert base58.b58decode_check(btc) == payload


@pytest.mark.parametrize("label", sorted(INVALID_PLACEHOLDERS))
def test_is_valid_address_is_false_for_each_derived_failure_mode(label):
    """One test per failure mode, so a failure names WHICH kind stopped being caught.

    The six are a bech32 checksum, Gridcoin bech32 (a format that cannot exist), a base58
    checksum, a base58 charset violation, a truncation, and XRP's account zero with the typo
    this repository deliberately keeps -- every shape the 2026-09-27 sweep actually found,
    derived from valid addresses so none of them appears in the source as a literal."""
    value = INVALID_PLACEHOLDERS[label]
    assert not is_valid_address(value), f"{label}: {value} is still accepted"


# ---------------------------------------------------------------------------------------
# THE 2026-09-27 REGTEST SWAP. Three addresses off two real daemons, two of them refused.
# ---------------------------------------------------------------------------------------

# LITERALS ON PURPOSE, AND THE ONLY ONES IN THIS BLOCK. Everywhere else in this repository an
# address is derived from a phrase, because a mistyped checksum in a literal goes unnoticed
# until something tries to pay it. These three are the exception for the same reason
# GRC_MAINNET_ACCIDENT above is: they are not examples of a format, they are the RECORD of a
# specific run, and deriving them would lose exactly what makes them evidence.
#
# The operator ran a BTC->LTC atomic swap on regtest against bitcoind 28.1 and litecoind
# 0.21.4 on 2026-09-27. These came off those daemons. Measured through
# modules/address_network.address_network() at the time, before the fix:
#
#     rltc1q7u6dnat...   ('unknown', 'not decodable as base58check')   <- hrp `rltc` absent
#     QYqEyFb1v76Q...    ('unknown', 'version byte 0x3a is in no table this module knows')
#     2MxYyFprP15u...    ('testnet', ok)                               <- the one that worked
#
# Two of three. Both were Litecoin formats this module CLAIMED to support, and an UNKNOWN
# verdict on the payout path is how a valid customer payout gets refused -- an outage, and
# worse than the burn the validity checker exists to prevent.
LIVE_LTC_REGTEST_BECH32 = "rltc1q7u6dnatxpsds4wvq2svx3h64v8s03cf69xg52q"
LIVE_LTC_REGTEST_P2SH_SCRIPT_ADDRESS2 = "QYqEyFb1v76QraQ3uo5wVkGtJrC4Rf5vU3"
LIVE_LTC_REGTEST_P2SH_BITCOIN_COMPATIBLE = "2MxYyFprP15ub2bQwPwCRFa8LqDifxG2gee"


@pytest.mark.parametrize(
    "address",
    [
        LIVE_LTC_REGTEST_BECH32,
        LIVE_LTC_REGTEST_P2SH_SCRIPT_ADDRESS2,
        LIVE_LTC_REGTEST_P2SH_BITCOIN_COMPATIBLE,
    ],
)
def test_every_address_from_the_live_regtest_swap_decodes_as_testnet(address):
    """THE REGRESSION. Each of these was produced by a running daemon, so each must decode.

    TESTNET rather than merely "not UNKNOWN": regtest is a test network, and a payout guard
    that reads UNKNOWN cannot tell a regtest address from a mainnet one -- which is the whole
    question the receive-path network check asks.

    MUTATION: remove `rltc` from BECH32_HRPS_BY_ASSET["LTC"] and the first case fails; remove
    0x3A from P2SH_VERSIONS and the second fails.
    """
    network, why = address_network(address)

    assert network == TESTNET, f"{address} reads as {network}: {why}"


def test_litecoin_has_two_live_p2sh_version_bytes_per_network():
    """NOT a migration: SCRIPT_ADDRESS and SCRIPT_ADDRESS2 are both declared and both decode.

    Read off litecoin-project/litecoin src/chainparams.cpp on master, fetched 2026-09-27
    rather than recalled -- mainnet declares SCRIPT_ADDRESS 5 and SCRIPT_ADDRESS2 50,
    testnet and regtest declare 196 and 58. litecoind ENCODES with the second and DECODES
    both, so a table holding one of each pair rejects half of every Litecoin P2SH address in
    existence.

    Asserted as a property of the table rather than of one address, because the failure was a
    missing ENTRY and the next one will be too.
    """
    assert P2SH_VERSIONS[0xC4] == TESTNET, "196, SCRIPT_ADDRESS -- shared with BTC and GRC"
    assert P2SH_VERSIONS[0x3A] == TESTNET, "58, Litecoin's SCRIPT_ADDRESS2 -- `Q...` addresses"
    assert P2SH_VERSIONS[0x05] == MAINNET, "5, SCRIPT_ADDRESS -- Bitcoin AND Litecoin declare it"
    assert P2SH_VERSIONS[0x32] == MAINNET, "50, Litecoin's mainnet SCRIPT_ADDRESS2 -- `M...`"


def test_litecoin_regtest_has_its_own_bech32_hrp():
    """`rltc`, not `tltc`. Regtest is its own network with its own hrp, as `bcrt` is for BTC.

    Both derived fixtures are asserted, so the clean gate and this test cannot disagree about
    which formats are covered.
    """
    # THE NETWORK AND THE hrp, NOT THE WHOLE SENTENCE. This asserted the exact reason string
    # and went red when the reason gained the witness version and program length -- a test
    # pinning a SPELLING rather than the fact it exists for. The fact is that `rltc` resolves to
    # TESTNET and the reason says so; the diagnostic detail beside it is free to improve.
    network, why = address_network(LTC_REGTEST_DEPOSIT)
    assert network == TESTNET
    assert "rltc" in why and "testnet" in why, why
    assert is_valid_address(LTC_REGTEST_DEPOSIT)
    assert is_testnet_address(LTC_P2SH_SCRIPT_ADDRESS2)


# =======================================================================================
# BIP-350 / bech32m. THE GAP THAT CAUSED AN OUTAGE, AND WHY NO TEST CAUGHT IT.
#
# Before 2026-09-28 no fixture in this suite and no test in it used a bech32m address, and
# `bech32.bech32_decode()` -- the installed BIP-173 reference decoder -- was called at FIVE
# sites across three modules. It verifies one checksum constant, 1; BIP-350 uses 0x2BC830A3
# for witness version 1 and above. So every Taproot address in existence was refused as
# INVALID with the sentence "the checksum or the character set is wrong", which is a
# confident, false statement about a perfectly spendable address.
#
# What that cost, measured on the real modules before the fix:
#   - services/swap_service.py raised ValueError, so NO swap paying out to bc1p... could be
#     created at all. Taproot is the default receive type in Sparrow, Muun, Phoenix and
#     `bitcoin-cli getnewaddress "" bech32m`.
#   - services/payout_service.py set an ALREADY CREDITED swap to status='failed' before
#     reserving inventory and before the payouts INSERT. Nothing sent, and 'failed' is
#     terminal by design, so it is never retried -- the customer's coin sits in our wallet
#     and their swap is permanently dead.
#   - modules/htlc_fee.py silently dropped the platform fee output, so 1.5% of every redeem
#     stayed with the redeemer.
#
# The vectors below are the PUBLISHED BIP-350 tables, generated by neither this repository's
# decoder nor tests/valid_addresses.py's encoder, and the valid ones are checked against the
# spec's own expected scriptPubKeys rather than merely "it decoded".
# =======================================================================================

@pytest.mark.parametrize(("address", "expected_script"), BIP350_VALID)
def test_every_published_bip350_valid_vector_decodes_to_the_spec_s_own_script(address, expected_script):
    """NOT "it decoded" -- the PROGRAM BYTES, against the scriptPubKey BIP-350 publishes for
    each vector. An address that is accepted with the wrong program burns money just as
    completely as one that is refused strands it, and only one of those two failures is
    visible from a boolean.

    The P2TR vector's program is secp256k1's generator x-coordinate
    (79be667e...16f81798), which is checkable by eye.
    """
    decoded = decode_segwit_address(address)
    assert decoded.ok, f"BIP-350 says this is valid: {decoded.why}"
    # BOUND AND REFUSED SEPARATELY RATHER THAN READ OFF `ok`, because `ok` does not say
    # this. SegwitAddress.ok is literally `self.program is not None`; it carries no claim
    # at all about `witness_version`, and this test indexes WITNESS_VERSION_OPCODE with
    # that field one line later. decode_segwit_address() has ONE return that populates
    # both and THREE that return a witness version with program=None, so "ok implies a
    # version" holds today only because of which returns exist -- nothing asserts it. Two
    # lines make it a pinned invariant, and make a decode that ever carried a program
    # without a version fail with that sentence instead of with a TypeError raised from
    # inside bytes() four frames away from the cause (rule 14).
    witness_version, program = decoded.witness_version, decoded.program
    assert witness_version is not None and program is not None, (
        f"a published BIP-350 VALID vector decoded ok but without BOTH a witness version "
        f"and a program (version={witness_version!r}, program={program!r}): {decoded.why}"
    )
    script = bytes([WITNESS_VERSION_OPCODE[witness_version], len(program)]) + program
    assert script.hex() == expected_script, (
        f"decoded to {script.hex()} but BIP-350's table says {expected_script}"
    )


@pytest.mark.parametrize(("address", "why"), BIP350_INVALID)
def test_every_published_bip350_invalid_vector_is_refused(address, why):
    """THE HALF THAT MAKES THE FIX SAFE RATHER THAN MERELY PERMISSIVE. Adding bech32m by
    accepting anything whose checksum matches EITHER constant would accept a v0 address with a
    bech32m checksum and a v1 address with a bech32 one -- corruptions, not alternative
    spellings. Two of these eight vectors are exactly those cases."""
    decoded = decode_segwit_address(address)
    assert not decoded.ok, f"BIP-350 says this is invalid ({why}) but it was accepted: {decoded.why}"
    assert decoded.why, "rule 14: a refusal with no reason sends the reader nowhere"


def test_the_encoding_is_decided_by_the_checksum_constant_not_by_trying_both():
    """segwit_encoding_of() asserted directly, which is the one decision BIP-350 is about."""
    v0 = decode_segwit_address("BC1QW508D6QEJXTDG4Y5R3ZARVARY0C5XW7KV8F3T4")
    v1 = decode_segwit_address("bc1p0xlxvlhemja6c4dqv22uapctqupfhlxm9h8z3k2e72q4k9hcz7vqzk5jj0")
    assert v0.encoding == "bech32"
    assert v1.encoding == "bech32m"
    assert BECH32_CHECKSUM_CONSTANT == 1
    assert BECH32M_CHECKSUM_CONSTANT == 0x2BC830A3, "BIP-350's constant, not a recalled one"


def test_the_version_to_encoding_binding_is_enforced_in_both_directions():
    """witness_program_refusal() directly, so the rule is asserted rather than inferred from
    an address. MUTATION: drop either branch and a corrupted address becomes valid."""
    assert witness_program_refusal(0, b"\x00" * 20, "bech32") is None
    assert witness_program_refusal(1, b"\x00" * 32, "bech32m") is None
    assert "bech32m checksum" in (witness_program_refusal(0, b"\x00" * 20, "bech32m") or "")
    assert "bech32 checksum" in (witness_program_refusal(1, b"\x00" * 32, "bech32") or "")


def test_a_witness_version_nobody_has_defined_is_ACCEPTED_when_well_formed():
    """REFUSE ONLY WHAT IS CONTRADICTED. A well-formed v5 address is not evidence against
    itself -- it is a version this repository has not heard of, which is the same category as
    the unknown hrp `rltc` was until 2026-09-27. Refusing it would be the exact failure
    bech32m's absence caused, arriving by the next door along."""
    decoded = decode_segwit_address(BTC_FUTURE_WITNESS_V5)
    assert decoded.ok, decoded.why
    assert decoded.witness_version == 5
    assert decoded.encoding == "bech32m"


def test_witness_version_above_sixteen_is_refused_because_no_opcode_expresses_it():
    """The boundary of the permissiveness above, and it is a real one rather than caution: the
    scriptPubKey's first byte is OP_0..OP_16, so v17 cannot be written down."""
    assert witness_program_refusal(16, b"\x00" * 32, "bech32m") is None
    assert "above 16" in (witness_program_refusal(17, b"\x00" * 32, "bech32m") or "")


@pytest.mark.parametrize("fixture_name", [
    "BTC_TAPROOT_PAYOUT", "BTC_TAPROOT_MAINNET", "BTC_REGTEST_TAPROOT",
    "LTC_TAPROOT_PAYOUT", "LTC_TAPROOT_PLATFORM_FEE", "LTC_REGTEST_TAPROOT",
])
def test_the_taproot_fixtures_decode_under_the_production_decoder(fixture_name):
    """TWO INDEPENDENT IMPLEMENTATIONS AGREEING. tests/valid_addresses.py writes out BIP-350's
    constant, charset and generator itself rather than importing them, precisely so that a sign
    error on either side shows up here instead of cancelling out -- a fixture built by the
    function that validates it proves only that the function agrees with itself."""
    address = getattr(valid_addresses, fixture_name)
    decoded = decode_segwit_address(address)
    assert decoded.ok, f"{fixture_name}: {decoded.why}"
    assert decoded.encoding == "bech32m"
    assert decoded.witness_version == 1
    # Same reason as the BIP-350 vector test above: `ok` is `program is not None`, so the
    # program is bound and refused here rather than measured through an attribute pyright
    # -- and a reader -- can only see as optional. A None here would otherwise surface as
    # "object of type None has no len()", which names neither the fixture nor the decoder.
    program = decoded.program
    assert program is not None, f"{fixture_name} decoded ok but carries no program: {decoded.why}"
    assert len(program) == 32, "P2TR carries a 32-byte x-only output key"


def test_taproot_addresses_resolve_to_the_network_their_hrp_names():
    """The network question, which is separate from validity and is the one that decides whether
    a deposit address may be handed to a customer."""
    assert address_network(BTC_TAPROOT_MAINNET)[0] == MAINNET
    assert address_network(BTC_TAPROOT_PAYOUT)[0] == TESTNET
    assert address_network(LTC_REGTEST_TAPROOT)[0] == TESTNET
    assert is_valid_address(BTC_TAPROOT_PAYOUT)
