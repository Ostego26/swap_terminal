"""XRP Ledger address decoding and validation, locally and without a daemon.

Role: function layer (rule 10 -- every decision here is a pure function over
      a string, callable with seeded inputs)
Reads: nothing. Arguments only.
Writes: nothing
Can move funds: no. It DECIDES whether an address is payable, which gates a
      payout -- so a wrong answer sends money to a place it cannot be
      recovered from, or refuses a customer's correct address.
Mainnet-safe: yes

THIS IS THE HALF OF THE XRP WORK THAT IS MEASURED.

chains/xrp.py's header says its RPC field names could not be verified from the
environment this was written in (xrpl.org returns no response through the
proxy). Nothing in THIS file has that excuse: an XRPL classic address is a
base58-encoded payload with a double-SHA256 checksum, and either the checksum
verifies or it does not. That is arithmetic, and the tests call it directly.

WHY LOCAL VALIDATION AT ALL, WHEN THE DAEMON HAS account_info

Because they answer different questions. `account_info` says whether an
account EXISTS and is funded. This says whether a string is a well-formed
address at all. A customer who pastes a truncated address, or one with a
transposed character, produces a string the daemon will also reject -- but
only after a network round trip, and only if the daemon is reachable. Worse,
an UNFUNDED but perfectly valid address returns actAsNotFound from
account_info, which is not the same as invalid: paying it is exactly how a new
XRPL account gets created.

So: shape is checked here, existence is asked of the daemon, and the two are
never conflated.

THE BASE58 ALPHABET IS NOT BITCOIN'S, AND THAT IS THE FIRST THING TO GET WRONG.

Ripple chose a different ordering for the same 58 characters. Decoding an XRPL
address with Bitcoin's alphabet produces a different 25 bytes, which then fails
the checksum -- so the failure is loud rather than silent, which is the one
mercy here. The reverse is worse: a Bitcoin address decoded with THIS alphabet
would also fail, so the two cannot be confused into each other.
"""

from __future__ import annotations

import hashlib

# Ripple's base58 ordering. NOT Bitcoin's, which starts "123456789ABCDEF...".
# The difference is deliberate on Ripple's part and is the reason a Bitcoin
# library cannot decode these addresses by accident.
XRPL_ALPHABET = "rpshnaf39wBUDNEGHJKLM4PQRST7VWXYZ2bcdeCg65jkm8oFqi1tuvAxyz"

# A classic address decodes to 25 bytes: a 1-byte type prefix, a 20-byte
# account id, and a 4-byte checksum.
CLASSIC_ADDRESS_BYTES = 25
ACCOUNT_ID_BYTES = 20
CHECKSUM_BYTES = 4
CLASSIC_ADDRESS_PREFIX = 0x00

# An X-address packs a classic address AND a destination tag into one string,
# so a customer cannot paste the address while forgetting the tag. Its prefixes
# differ by network, which is also what makes a mainnet/testnet mix-up visible.
X_ADDRESS_PREFIX_MAINNET = bytes([0x05, 0x44])
X_ADDRESS_PREFIX_TESTNET = bytes([0x04, 0x93])


class XRPAddressError(ValueError):
    """A string is not a usable XRP Ledger address, with the reason attached.

    Carries WHY rather than just failing, because the three causes want three
    different responses from a human: a typo is retyped, a Bitcoin address
    pasted into an XRP field is a different mistake entirely, and an X-address
    on the wrong network means the customer is on testnet.
    """


def _base58_decode(text: str) -> bytes:
    """XRPL base58 -> bytes, preserving leading-zero semantics.

    Leading 'r' characters encode leading zero bytes (r is index 0 in this
    alphabet, the way '1' is in Bitcoin's). Dropping them would shorten the
    payload and fail the length check for a perfectly good address, so they are
    counted and re-added.
    """
    if not text:
        raise XRPAddressError("empty string is not an address")
    number = 0
    for character in text:
        index = XRPL_ALPHABET.find(character)
        if index < 0:
            raise XRPAddressError(
                f"{character!r} is not in the XRP Ledger base58 alphabet. Note this alphabet is NOT "
                f"Bitcoin's -- a BTC/LTC/GRC address pasted into an XRP field fails here, which is the "
                f"intended outcome rather than a bug."
            )
        number = number * 58 + index
    body = number.to_bytes((number.bit_length() + 7) // 8, "big") if number else b""
    leading_zeros = len(text) - len(text.lstrip(XRPL_ALPHABET[0]))
    return b"\x00" * leading_zeros + body


def _checksum(payload: bytes) -> bytes:
    """XRPL uses Bitcoin's double-SHA256 checksum, first four bytes."""
    return hashlib.sha256(hashlib.sha256(payload).digest()).digest()[:CHECKSUM_BYTES]


def decode_classic_address(address: str) -> bytes:
    """A classic r-address -> its 20-byte account id, or a refusal.

    The checksum is the whole point. A single transposed character changes the
    decoded bytes, the recomputed checksum no longer matches, and this raises --
    which is what stands between a customer's typo and an irrecoverable
    payment.
    """
    decoded = _base58_decode(address)
    if len(decoded) != CLASSIC_ADDRESS_BYTES:
        raise XRPAddressError(
            f"decodes to {len(decoded)} bytes, not {CLASSIC_ADDRESS_BYTES}. A classic XRP address is "
            f"a 1-byte prefix, a {ACCOUNT_ID_BYTES}-byte account id and a {CHECKSUM_BYTES}-byte "
            f"checksum; this is the wrong length, so it is truncated, padded or not an address."
        )
    if decoded[0] != CLASSIC_ADDRESS_PREFIX:
        raise XRPAddressError(
            f"type prefix is 0x{decoded[0]:02x}, not 0x{CLASSIC_ADDRESS_PREFIX:02x}. This decodes as "
            f"base58 but is not a classic account address."
        )
    body, given = decoded[:-CHECKSUM_BYTES], decoded[-CHECKSUM_BYTES:]
    if _checksum(body) != given:
        raise XRPAddressError(
            "checksum does not match. One or more characters are wrong -- this is what catches a "
            "transposed or dropped character before a payment leaves, and an XRP payment cannot be "
            "recalled."
        )
    return body[1:]


def is_valid_classic_address(address: str) -> bool:
    """True/False wrapper for callers that only need the verdict.

    Deliberately does NOT swallow anything else: only XRPAddressError becomes
    False. A bug in this module must not read as "the customer's address is
    bad" -- that is chains/base.py's validate_address lesson, where returning
    False for "I could not check" shows a customer their correct address
    rejected.
    """
    try:
        decode_classic_address(address)
    except XRPAddressError:
        return False
    return True


def looks_like_x_address(address: str) -> bool:
    """Is this the tag-bearing X-address form rather than a classic r-address?

    Worth distinguishing rather than just rejecting: an X-address is a customer
    doing the RIGHT thing -- it carries the destination tag inside the string,
    so they cannot paste an address and forget the tag, which is the single most
    common way an XRP deposit goes missing.
    """
    return address.startswith(("X", "T"))


def describe_address(address: str) -> str:
    """One line an operator can read, naming the form and the verdict (rule 14)."""
    if looks_like_x_address(address):
        network = "mainnet" if address.startswith("X") else "testnet"
        return f"X-address ({network}); carries its own destination tag -- not decoded here, see chains/xrp.py"
    try:
        account_id = decode_classic_address(address)
    except XRPAddressError as error:
        return f"NOT a valid classic address: {error}"
    return f"valid classic address, account id {account_id.hex()}  <- shape only; existence is a daemon question"
