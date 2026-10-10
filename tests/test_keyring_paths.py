"""The per-asset derivation path scheme, pinned byte for byte.

Role: test (pure; opens no socket, runs no canister, needs no replica)
Reads: swap_terminal/modules/keyring_paths.py, icp_custody_addresses.py
Writes: nothing
Can move funds: no
Mainnet-safe: yes

WHY THE PATHS ARE PINNED AS LITERAL BYTES RATHER THAN RECOMPUTED.

A test that asserts `derivation_path("BTC", TESTNET) == derivation_path("BTC",
TESTNET)` passes under every possible scheme, including a broken one. These
assertions spell the expected bytes out, so editing any element of the scheme
without bumping SCHEME_VERSION fails here -- which is the whole guarantee the
scheme offers.

The guarantee matters because of what a path IS. The path decides the key, the
key decides the address, and the address is where funds are. A scheme change
after an address has been funded does not move the funds; it abandons them. So
the suite is the thing that makes such a change deliberate, and it has to be
spelled in literals to do that job.
"""

from __future__ import annotations

import dataclasses

import pytest
from modules.address_network import MAINNET, TESTNET
from modules.keyring_paths import (
    BIP340,
    CURVE_FOR_ASSET,
    CURVES,
    ED25519,
    INDEX_BYTES,
    NETWORKS,
    PUBLIC_KEY_SHAPE,
    ROOT_SECP256K1,
    SCHEME_PREFIX,
    SCHEME_VERSION,
    SECP256K1_ECDSA,
    KeyRequest,
    KeyringRefused,
    curve_for,
    derivation_path,
    describe_path,
    public_key_shape,
    refuse_wrong_shape,
    request_for,
)

import icp_custody_addresses as custody

#: The whole scheme, written out. If any of these changes, a funded address moves.
EXPECTED_PATHS = {
    ("BTC", TESTNET): [b"swap_terminal", b"v1", b"testnet", b"BTC", b"\x00\x00\x00\x00"],
    ("BTC", MAINNET): [b"swap_terminal", b"v1", b"mainnet", b"BTC", b"\x00\x00\x00\x00"],
    ("LTC", TESTNET): [b"swap_terminal", b"v1", b"testnet", b"LTC", b"\x00\x00\x00\x00"],
    ("GRC", TESTNET): [b"swap_terminal", b"v1", b"testnet", b"GRC", b"\x00\x00\x00\x00"],
    ("SOL", TESTNET): [b"swap_terminal", b"v1", b"testnet", b"SOL", b"\x00\x00\x00\x00"],
    ("XRP", TESTNET): [b"swap_terminal", b"v1", b"testnet", b"XRP", b"\x00\x00\x00\x00"],
}


@pytest.mark.parametrize(("key", "expected"), sorted(EXPECTED_PATHS.items()))
def test_the_path_is_exactly_these_bytes(key, expected):
    """MUTATION: change any element of the scheme, or reorder two."""
    asset, network = key
    assert derivation_path(asset, network) == expected


def test_every_asset_in_the_table_has_a_path_on_both_networks():
    """Rule 11: 'did every consumer follow automatically?'

    An asset added to CURVE_FOR_ASSET and nowhere else must still derive, because
    the table IS the single place. If adding a row required a second edit
    somewhere, that second place would be the bug.
    """
    for asset in CURVE_FOR_ASSET:
        for network in NETWORKS:
            path = derivation_path(asset, network)
            assert len(path) == 5
            assert path[0] == SCHEME_PREFIX
            assert path[1] == SCHEME_VERSION
            assert path[3] == asset.encode("ascii")


def test_no_two_assets_share_a_path():
    """BTC, LTC and GRC are all secp256k1 and must NOT share a key.

    Sharing would give one hash160 and therefore three addresses that are
    publicly linkable and that fall together. Per-asset paths are what separate
    them, and nothing else in the system would.
    """
    paths = {
        (asset, network): tuple(derivation_path(asset, network))
        for asset in CURVE_FOR_ASSET
        for network in NETWORKS
    }
    assert len(set(paths.values())) == len(paths), "two (asset, network) pairs share a path"


def test_mainnet_and_testnet_never_share_a_key():
    """The element that stops a rehearsal signature being made by the mainnet key."""
    for asset in CURVE_FOR_ASSET:
        assert derivation_path(asset, MAINNET) != derivation_path(asset, TESTNET)


def test_the_index_widens_the_path_without_changing_anything_else():
    """Reserved so per-swap deposit addresses can arrive without moving an address."""
    base = derivation_path("BTC", TESTNET, 0)
    first = derivation_path("BTC", TESTNET, 1)
    assert base[:4] == first[:4]
    assert base[4] == b"\x00\x00\x00\x00"
    assert first[4] == b"\x00\x00\x00\x01"
    assert len(first[4]) == INDEX_BYTES


def test_index_0_is_what_the_default_produces():
    """So a caller that omits the index and one that passes 0 get the SAME key."""
    assert derivation_path("GRC", TESTNET) == derivation_path("GRC", TESTNET, 0)


def test_a_lowercase_asset_gives_the_same_path_as_uppercase():
    """`curve_for` accepts either case, so refusing one here would be two answers."""
    assert derivation_path("btc", TESTNET) == derivation_path("BTC", TESTNET)
    assert derivation_path("BTC", TESTNET)[3] == b"BTC"


# ---------------------------------------------------------------------------
# REFUSALS. Each one is a path that must not be guessed on the caller's behalf.


def test_an_unknown_asset_is_refused_rather_than_defaulted():
    """MUTATION: make curve_for fall back to secp256k1.

    Three of five assets ARE secp256k1, so a default would be right often enough
    to look correct and would hand SOL or XRP a key of the wrong type.
    """
    with pytest.raises(KeyringRefused, match="no keyring curve"):
        derivation_path("DOGE", TESTNET)


@pytest.mark.parametrize("asset", ["ICP", "XMR"])
def test_the_deliberately_absent_assets_are_refused_and_say_so(asset):
    """ICP has no threshold key to derive; XMR's addresses are not a path."""
    with pytest.raises(KeyringRefused, match="deliberately absent"):
        curve_for(asset)


@pytest.mark.parametrize("network", ["regtest", "unknown", "", "TESTNET", "main"])
def test_an_unnamed_network_is_refused(network):
    """regtest shares testnet's path on purpose; 'unknown' cannot be reproduced."""
    with pytest.raises(KeyringRefused, match="not a network"):
        derivation_path("BTC", network)


def test_a_bool_index_is_refused_because_true_would_mean_one():
    """MUTATION: drop the isinstance(index, bool) check.

    A bool IS an int in Python, so `derivation_path("BTC", TESTNET, True)` would
    silently mean index 1 -- a different key than the caller's flag implied.
    """
    with pytest.raises(KeyringRefused, match="must be an int"):
        derivation_path("BTC", TESTNET, True)


@pytest.mark.parametrize("index", [-1, 1 << (INDEX_BYTES * 8), 1 << 40])
def test_an_index_outside_the_four_byte_range_is_refused(index):
    """A wider index is a scheme change, not a wider integer."""
    with pytest.raises(KeyringRefused, match="outside"):
        derivation_path("BTC", TESTNET, index)


# ---------------------------------------------------------------------------
# CURVES AND KEY SHAPES


def test_the_three_curve_names_are_the_set_the_canister_lists():
    """The Rust side's own copy is compared in test_keyring_curve_vocabulary.py."""
    assert {SECP256K1_ECDSA, ED25519, BIP340} == CURVES


def test_the_two_secp256k1_curves_are_distinct_values():
    """Same curve, different signature scheme, DIFFERENT key and address.

    Collapsing them because the curve name matches is the quietest way to derive
    an address nobody holds the key for.
    """
    assert SECP256K1_ECDSA != BIP340
    assert "secp256k1" in SECP256K1_ECDSA
    assert "secp256k1" in BIP340


def test_every_curve_has_a_shape_and_every_shape_names_a_curve():
    assert set(PUBLIC_KEY_SHAPE) == CURVES


def test_the_bitcoin_family_is_secp256k1_ecdsa_and_the_rest_is_ed25519():
    """Pinned because it decides which management call the canister makes."""
    assert curve_for("BTC") == SECP256K1_ECDSA
    assert curve_for("LTC") == SECP256K1_ECDSA
    assert curve_for("GRC") == SECP256K1_ECDSA
    assert curve_for("SOL") == ED25519
    assert curve_for("XRP") == ED25519


def test_a_compressed_secp256k1_key_is_accepted_and_an_uncompressed_one_is_not():
    assert refuse_wrong_shape(b"\x02" + b"\xab" * 32, SECP256K1_ECDSA) == b"\x02" + b"\xab" * 32
    with pytest.raises(KeyringRefused, match="33 bytes"):
        refuse_wrong_shape(b"\x04" + b"\xab" * 64, SECP256K1_ECDSA)


def test_an_ed25519_key_accepts_any_leading_byte():
    """It carries no parity byte and no format tag; 32 bytes is the whole check."""
    for prefix in (0x00, 0x02, 0x04, 0xED, 0xFF):
        raw = bytes([prefix]) + b"\xab" * 31
        assert refuse_wrong_shape(raw, ED25519) == raw


def test_an_ed25519_key_with_xrps_0xed_prefix_is_refused_as_33_bytes():
    """XRP publishes ed25519 as 0xED || 32 bytes. The canister returns the bare 32.

    A 33-byte value arriving here means the prefix was added on the wrong side of
    the boundary, and the address derived from it is one nobody holds.
    """
    with pytest.raises(KeyringRefused, match="32 bytes"):
        refuse_wrong_shape(b"\xed" + b"\xab" * 32, ED25519)


def test_a_non_bytes_key_is_refused_by_name():
    with pytest.raises(KeyringRefused, match="must be bytes"):
        refuse_wrong_shape("02ab", SECP256K1_ECDSA)


def test_an_unknown_curve_has_no_shape_to_guess():
    with pytest.raises(KeyringRefused, match="not a known curve"):
        public_key_shape("ed25519_but_misspelled")


# ---------------------------------------------------------------------------
# THE OPERATOR-FACING RENDERING (rule 14)


def test_describe_path_reads_as_itself_for_ascii_and_hex_for_the_index():
    """A path printed as raw bytes is a value an operator cannot check."""
    text = describe_path(derivation_path("BTC", TESTNET))
    assert text == "swap_terminal/v1/testnet/BTC/0x00000000"


def test_describe_path_hexes_an_element_that_is_not_printable():
    assert describe_path([b"\x00\xff"]) == "0x00ff"


def test_describe_path_does_not_claim_an_empty_element_is_ascii():
    """An empty element must not render as an empty string that reads as nothing."""
    assert describe_path([b""]) == "0x"


# ---------------------------------------------------------------------------
# THE CANDID ENCODING, which is where a correct path becomes a correct call.


def test_every_byte_of_a_blob_literal_is_hex_escaped():
    """Uniform escaping has one rule; a mixed encoding has one per element."""
    assert custody.candid_blob(b"v1") == 'blob "\\76\\31"'
    assert custody.candid_blob(b"\x00\x00\x00\x01") == 'blob "\\00\\00\\00\\01"'
    assert custody.candid_blob(b"") == 'blob ""'


def test_the_argument_for_the_root_key_is_an_empty_vec():
    """The default call, and the one the operator ran by hand before increment 1b."""
    assert custody.candid_public_key_argument(ROOT_SECP256K1) == (
        "(record { curve = variant { secp256k1_ecdsa }; derivation_path = vec {} })"
    )


def test_the_argument_carries_the_curve_and_every_path_element():
    argument = custody.candid_public_key_argument(request_for("SOL", TESTNET))
    assert "variant { ed25519 }" in argument
    # b"SOL" is 0x53 0x4f 0x4c, and the index is four zero bytes.
    assert 'blob "\\53\\4f\\4c"' in argument
    assert 'blob "\\00\\00\\00\\00"' in argument
    assert argument.count("blob ") == 5


def test_a_curve_the_canister_does_not_list_is_refused_at_construction():
    """Refused when the request is BUILT, not when it is sent.

    Otherwise an invalid curve is carried through the call chain and surfaces as
    an opaque candid decode failure from dfx, at the one moment an operator is
    trying to read a key.
    """
    with pytest.raises(KeyringRefused, match="not a curve this keyring names"):
        KeyRequest("secp256k1", ())


def test_a_request_refuses_a_path_element_that_is_not_bytes():
    """A str element would render as its repr and silently be a different path."""
    with pytest.raises(KeyringRefused, match="must be bytes"):
        KeyRequest(SECP256K1_ECDSA, ("swap_terminal",))


def test_request_for_pairs_each_asset_with_its_own_curve():
    """THE REASON request_for EXISTS: composing curve_for and derivation_path by
    hand lets one asset's curve be paired with another's path, which asks for a
    key that exists, is the wrong one, and yields an address nobody holds."""
    assert request_for("SOL", TESTNET).curve == ED25519
    assert request_for("BTC", TESTNET).curve == SECP256K1_ECDSA
    assert request_for("BTC", TESTNET).derivation_path == tuple(
        derivation_path("BTC", TESTNET)
    )


def test_a_request_is_frozen_so_its_candid_text_keeps_describing_it():
    with pytest.raises(dataclasses.FrozenInstanceError):
        request_for("BTC", TESTNET).curve = ED25519


def test_the_root_request_is_the_secp256k1_empty_path():
    assert ROOT_SECP256K1.curve == SECP256K1_ECDSA
    assert ROOT_SECP256K1.derivation_path == ()


def test_the_keyring_plan_carries_a_refusal_as_a_row_rather_than_dropping_it():
    """Rule 14 per row: a chain missing from the table must not look like a fine one."""
    rows = custody.keyring_plan({"BTC": TESTNET, "ICP": TESTNET}, ["BTC", "ICP"])
    by_asset = {row["asset"]: row for row in rows}
    assert "refused" not in by_asset["BTC"]
    assert by_asset["BTC"]["curve"] == SECP256K1_ECDSA
    assert by_asset["BTC"]["request"] == request_for("BTC", TESTNET)
    assert "refused" in by_asset["ICP"]
    assert "deliberately absent" in by_asset["ICP"]["refused"]


def test_the_keyring_plan_opens_nothing():
    """It is the decision, made before anything is asked (rule 10).

    Seeded with a network that cannot resolve, it still returns rows -- which it
    could not do if it were reaching a replica to find out.
    """
    rows = custody.keyring_plan({}, sorted(CURVE_FOR_ASSET))
    assert len(rows) == len(CURVE_FOR_ASSET)
    assert all("refused" in row for row in rows), "an unknown network must refuse, not guess"
