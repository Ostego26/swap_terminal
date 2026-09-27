"""Valid addresses for tests to use, DERIVED -- so no test ever invents an address again.

Role: test support (pure; no chain, no network, no database)
Reads: nothing
Writes: nothing
Can move funds: no
Mainnet-safe: yes -- every address here is on a TEST network by construction.
Live-safe: yes

Operator, 2026-09-27: "make sure any an all addresses used for any transaction are valid
addresses that pass all tests."

WHY A MODULE AND NOT A LITERAL PER TEST FILE. Scanned that day, 25 address-shaped literals in
this tree decoded as nothing at all -- not a bad network, not a wrong chain: unparseable.

    tb1qexampleparticipantaddress0000000000000000000000   the BTC client's usage example
    tgrc1qexamplerefundaddress000000000000000000000000    the GRC client's, doubly wrong:
                                                          GRIDCOIN HAS NO BECH32 FORMAT
    bcrt1qdepositaddressexample00000000000000000          two deposit-matching test files
    S8kKq2VrZ4mQvYtN6dWxJ3hLpB7cFgTnEu                    test_swap_view's payout fixture
    SdzHNW1234567890abcdefghijklmnopq                     test_open_swap's, with characters
                                                          outside base58's alphabet entirely
    "S" + "x" * 30                                        test_xrp_swap_attribution's

Each looked plausible in its own line, and each sat where a reader would take it for a working
example. Two of them were WORSE than decoration: the stubs beside them modeled a daemon's
validate_address by a leading letter, so a fake address and a fake check agreed with each other
and the pair passed 17 tests while matching no daemon either could stand in for. That is rule
8's failure -- two copies of one idea, drifting together instead of apart.

DERIVED FROM FIXED PHRASES, not written down. A literal can be mistyped in its checksum and
nothing notices until something tries to pay it; a derivation cannot. The phrase also says what
the address is FOR, which a 34-character string never does.

EVERY ONE IS ON A TEST NETWORK, deliberately, and tests/test_address_literals_are_valid.py
asserts it. A mainnet address in a fixture is one copy-paste away from a real payout -- which
is not hypothetical here: a `getnewaddress` without `-testnet` on 2026-09-27 put
RyyNX8E7tDRzACL47h8JKKDUXf9aMCz8YV in the operator's live staking wallet.
"""

from __future__ import annotations

import hashlib
import pathlib
import sys

import base58
import bech32

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "swap_terminal"))

from modules.address_network import (
    TESTNET_P2PKH_VERSION,
    TESTNET_P2SH_VERSION,
    XRP_BASE58_ALPHABET,
)


def _hash160(phrase: str) -> bytes:
    """A deterministic 20-byte key hash from a phrase.

    RIPEMD160(SHA256(phrase)), which is the same construction a real address uses over a public
    key -- so these are addresses that could exist, not strings shaped like one. Nobody holds
    the key, which is the point: a fixture must be unspendable by construction and valid by
    construction at the same time.
    """
    return hashlib.new("ripemd160", hashlib.sha256(phrase.encode()).digest()).digest()


def base58_testnet(phrase: str) -> str:
    """A BTC/LTC/GRC testnet P2PKH address. The version byte comes from address_network."""
    return base58.b58encode_check(TESTNET_P2PKH_VERSION + _hash160(phrase)).decode()


def bech32_address(hrp: str, phrase: str) -> str:
    """A v0 witness-program address under `hrp` (tb, bcrt, tltc)."""
    return bech32.encode(hrp, 0, _hash160(phrase))


def xrp_address(phrase: str) -> str:
    """A classic XRP address. Same base58check construction, XRP's alphabet, version 0.

    XRP has no separate testnet address format -- the network is a property of the server you
    submit to, not of the address -- so "test network" cannot be asserted for these the way it
    can for the others. Said here rather than left as a gap in the validity test.
    """
    return base58.b58encode_check(b"\x00" + _hash160(phrase), alphabet=XRP_BASE58_ALPHABET).decode()


# The named fixtures. One per role, because a test that reuses one address for two roles is
# testing a case build_htlc_redeem_script() refuses (both branches hashing to one key).
GRC_PAYOUT = base58_testnet("swap_terminal test fixture GRC payout")
GRC_PARTICIPANT = base58_testnet("grc testnet participant")
GRC_REFUND = base58_testnet("grc testnet refund")
GRC_SECOND_PAYOUT = base58_testnet("swap_terminal test fixture GRC second payout")

BTC_PARTICIPANT = bech32_address("tb", "btc testnet participant")
BTC_REFUND = bech32_address("tb", "btc testnet refund")
BTC_REGTEST_DEPOSIT = bech32_address("bcrt", "btc regtest deposit")
BTC_REGTEST_SOMEBODY_ELSE = bech32_address("bcrt", "btc regtest somebody else")

LTC_PARTICIPANT = bech32_address("tltc", "ltc testnet participant")
LTC_REFUND = bech32_address("tltc", "ltc testnet refund")
LTC_PLATFORM_FEE = bech32_address("tltc", "ltc testnet platform fee")

# A P2SH testnet address, for the `{"addresses": [...]}` shape Core 0.19 and Litecoin 0.21.4
# report. It replaced "2NexampleLTC", which is the right LENGTH for nothing and decodes as
# nothing -- and a contract output is exactly where a P2SH address appears, so a fixture that
# cannot be decoded is a fixture that could not have come off a chain.
LTC_P2SH_TESTNET = base58.b58encode_check(
    TESTNET_P2SH_VERSION + _hash160("ltc testnet p2sh contract")
).decode()

XRP_HOT_ACCOUNT = xrp_address("swap_terminal hot account")

# XRP's ACCOUNT_ZERO, DERIVED rather than recalled -- and it is worth saying why. Asked for it
# from memory on 2026-09-27 I wrote `...hoLvTq`; encoding twenty-one zero bytes under XRP's
# alphabet gives `...hoLvTp`. One character, and the wrong one is exactly what
# tests/test_xrp_destination_tags.py uses on purpose to prove a typo'd account is REFUSED.
# So the valid one is derived here and the invalid one is derived FROM it, below, rather than
# both being spelled and one of them quietly being the other.
XRP_ACCOUNT_ZERO = base58.b58encode_check(b"\x00" * 21, alphabet=XRP_BASE58_ALPHABET).decode()
XRP_ACCOUNT_ZERO_WITH_TYPO = XRP_ACCOUNT_ZERO[:-1] + ("q" if XRP_ACCOUNT_ZERO[-1] != "q" else "p")

# ---------------------------------------------------------------------------------------
# DELIBERATELY INVALID, AND DERIVED RATHER THAN SPELLED.
#
# Tests that prove a refusal need a string that cannot be paid. Writing one as a literal puts
# an invalid address in the source, which tests/test_address_literals_are_valid.py then
# (correctly) fails on -- and it caught exactly that, in the very tests written to check the
# refusals. Suppressing the gate for those files would be a baseline, which rule 19 forbids.
#
# So each one is BUILT from a valid address, and the construction is the documentation: a
# reader sees "this is GRC_PAYOUT with its last character changed" instead of a 34-character
# string they have to decode to understand. The gate sees no literal, because there is none.
#
# Each is named for the FAILURE MODE it represents, because the failure mode is what a test
# using it is asserting about.
# ---------------------------------------------------------------------------------------

def _flip_last(value: str) -> str:
    """The same address with its final character changed, which breaks the checksum.

    Both alphabets exclude 0, O, I and l, so appending a character from outside them would test
    the CHARSET rather than the checksum. Swapping within the alphabet keeps the string
    well-formed and makes only the checksum wrong, which is the typo an operator actually makes.
    """
    alphabet = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
    return value[:-1] + ("A" if value[-1] != "A" else "B") if value[-1] in alphabet else value + "A"


# A bech32 checksum that does not hold: the hrp and charset are right, the checksum is not.
INVALID_BECH32_CHECKSUM = BTC_PARTICIPANT[:-1] + ("q" if BTC_PARTICIPANT[-1] != "q" else "p")

# Gridcoin bech32, which CANNOT EXIST -- and with a valid checksum, so the refusal cannot be
# passing for the wrong reason. This is the shape atomic_grc_client.py's usage example had.
INVALID_GRC_BECH32 = "tgrc1" + BTC_PARTICIPANT.split("1", 1)[1]

# A base58 address with one character changed: the exact operator typo.
INVALID_BASE58_CHECKSUM = _flip_last(GRC_PAYOUT)

# A base58 string containing a character the alphabet excludes. "0" is not in base58 precisely
# because it is confusable with O, which is the class of typo this represents.
INVALID_BASE58_CHARSET = GRC_PAYOUT[:-1] + "0"

# Truncation: what a copy-paste that clipped the end produces. Still address-SHAPED.
INVALID_TRUNCATED = GRC_PAYOUT[:26]

INVALID_PLACEHOLDERS = {
    "bech32 checksum": INVALID_BECH32_CHECKSUM,
    "Gridcoin bech32, a format that cannot exist": INVALID_GRC_BECH32,
    "base58 checksum": INVALID_BASE58_CHECKSUM,
    "base58 charset": INVALID_BASE58_CHARSET,
    "truncated": INVALID_TRUNCATED,
    "XRP account zero with a typo": XRP_ACCOUNT_ZERO_WITH_TYPO,
}

ALL_VALID = {
    name: value
    for name, value in sorted(globals().items())
    if name.isupper() and isinstance(value, str) and not name.startswith("INVALID")
    and not name.endswith("_WITH_TYPO")
    and not name.endswith("ALPHABET") and not name.endswith("VERSION")
}
