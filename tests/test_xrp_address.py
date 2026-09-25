"""XRP address decoding, against the ledger's own constants.

Role: test (pure functions; no socket, no daemon)
Reads: chains/xrp_address.py
Writes: nothing
Can move funds: no. What it pins decides whether a payout address is accepted,
      and an XRP payment cannot be recalled.
Mainnet-safe: yes

THIS IS THE MEASURED HALF OF THE XRP WORK. chains/xrp.py's RPC field names are
a hypothesis; a base58 checksum is not. ACCOUNT_ZERO and ACCOUNT_ONE are
protocol constants, so matching them proves the alphabet AND the checksum
rather than proving the decoder agrees with itself.
"""

import hashlib
import random

import pytest
from chains.xrp_address import (
    XRPL_ALPHABET,
    XRPAddressError,
    decode_classic_address,
    describe_address,
    is_valid_classic_address,
    looks_like_x_address,
)

ACCOUNT_ZERO = "rrrrrrrrrrrrrrrrrrrrrhoLvTp"
ACCOUNT_ONE = "rrrrrrrrrrrrrrrrrrrrBZbvji"


def encode(account_id: bytes) -> str:
    """The inverse, written here rather than imported.

    Deliberately NOT added to chains/xrp_address.py: nothing in the application
    needs to encode an address -- it only ever validates ones customers supply
    -- and an unused public function is rule 9's dead code arriving new. It
    lives here because round-tripping is how the decoder is checked.
    """
    payload = b"\x00" + account_id
    payload += hashlib.sha256(hashlib.sha256(payload).digest()).digest()[:4]
    number = int.from_bytes(payload, "big")
    out = ""
    while number:
        number, remainder = divmod(number, 58)
        out = XRPL_ALPHABET[remainder] + out
    return XRPL_ALPHABET[0] * (len(payload) - len(payload.lstrip(b"\x00"))) + out


def test_account_zero_is_twenty_zero_bytes():
    """A protocol constant. If this fails, the alphabet or the checksum is wrong."""
    assert decode_classic_address(ACCOUNT_ZERO) == bytes(20)


def test_account_one_is_nineteen_zeros_then_one():
    assert decode_classic_address(ACCOUNT_ONE) == bytes(19) + b"\x01"


def test_the_alphabet_is_ripples_and_not_bitcoins():
    """Same 58 characters, different order. Getting this wrong fails every checksum."""
    assert XRPL_ALPHABET.startswith("rpshnaf39wBUDNEGHJKLM4PQRST7VWXYZ")
    assert XRPL_ALPHABET != "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
    assert sorted(XRPL_ALPHABET) == sorted("123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz")


def test_round_trip_over_many_random_account_ids():
    # S311 is suppressed as a checked claim: this generates TEST VECTORS,
    # not key material. A seeded Mersenne Twister is exactly right here --
    # it makes the 2000 account ids reproducible, so a failure can be
    # rerun. Nothing derived from it is ever a secret or an address a
    # customer pays.
    rng = random.Random(20260925)  # noqa: S311
    for _ in range(2000):
        account_id = bytes(rng.getrandbits(8) for _ in range(20))
        assert decode_classic_address(encode(account_id)) == account_id


def test_every_single_character_change_is_caught_by_the_checksum():
    """THE POINT OF THE CHECKSUM: a typo must not reach a payment.

    An XRP payment cannot be recalled, so a transposed character that decoded
    successfully would send money to an address nobody holds the key to.
    """
    address = encode(bytes(range(20)))
    for index in range(len(address)):
        replacement = next(c for c in XRPL_ALPHABET if c != address[index])
        mutated = address[:index] + replacement + address[index + 1:]
        assert not is_valid_classic_address(mutated), f"accepted a mutation at index {index}"


@pytest.mark.parametrize("bitcoin_address", [
    "1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNa",
    "3J98t1WpEZ73CNmQviecrnyiWrnqRhWNLy",
    "bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4",
])
def test_a_bitcoin_address_is_rejected(bitcoin_address):
    """A BTC address pasted into an XRP field is a different mistake, caught here.

    This tree already holds BTC, LTC and GRC addresses, so the paste is
    plausible; the two alphabets differing is what makes it loud.
    """
    assert not is_valid_classic_address(bitcoin_address)


def test_an_empty_or_truncated_address_is_rejected():
    assert not is_valid_classic_address("")
    full = encode(bytes(range(20)))
    assert not is_valid_classic_address(full[:-1])
    assert not is_valid_classic_address(full + XRPL_ALPHABET[1])


def test_the_failure_says_which_kind_of_wrong_it_is():
    """Three causes want three responses from a human, so the message separates them."""
    with pytest.raises(XRPAddressError, match="not in the XRP Ledger base58 alphabet"):
        decode_classic_address("rInvalid0Character")
    with pytest.raises(XRPAddressError, match="checksum does not match"):
        address = encode(bytes(range(20)))
        decode_classic_address(address[:-1] + next(c for c in XRPL_ALPHABET if c != address[-1]))


def test_x_addresses_are_recognized_and_not_decoded_as_classic():
    """An X-address carries its own destination tag -- a customer doing it right."""
    assert looks_like_x_address("XVLhHMPHU98es4dbozjVtdWzVrDjtV18pX8yuPT7y4xaEHi")
    assert looks_like_x_address("TVsBZmcewpEHgajPi1jApLeYnHPJw82v9JNYf7dkGmWphmh")
    assert not looks_like_x_address(ACCOUNT_ZERO)


def test_describe_address_never_returns_a_bare_verdict():
    """Rule 14: the operator reads the screen, not the source."""
    assert "valid classic address" in describe_address(ACCOUNT_ZERO)
    assert "NOT a valid classic address" in describe_address("rNope0")
    assert "destination tag" in describe_address("XVLhHMPHU98es4dbozjVtdWzVrDjtV18pX8yuPT7y4xaEHi")
