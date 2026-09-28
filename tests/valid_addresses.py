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

from chains import monero_keys
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

# LITECOIN'S REGTEST BECH32, AND ITS SECOND P2SH VERSION BYTE. Both added 2026-09-27, both
# because a REAL RUN produced one and this repository refused it.
#
# The operator ran a BTC->LTC atomic swap on regtest against bitcoind 28.1 and litecoind
# 0.21.4. Two of the three addresses that came off those daemons decoded as UNKNOWN here:
#
#     rltc1q7u6dnatxpsds4wvq2svx3h64v8s03cf69xg52q   `litecoin-cli getnewaddress`
#     QYqEyFb1v76QraQ3uo5wVkGtJrC4Rf5vU3             the funded HTLC contract address
#
# `rltc` is Litecoin's REGTEST hrp -- its own, exactly as Bitcoin's regtest is `bcrt` and not
# `tb` -- and 0x3A is Litecoin's SCRIPT_ADDRESS2 on testnet and regtest, which is live at the
# same time as 0xC4 rather than replacing it. Neither was in modules/address_network.py.
#
# These two fixtures exist so the clean gate covers both formats from now on. The live strings
# themselves are pinned as regression cases in tests/test_address_network.py, because they are
# the incident and not a constructed example.
LTC_REGTEST_DEPOSIT = bech32_address("rltc", "ltc regtest deposit")
LTC_REGTEST_PLATFORM_FEE = bech32_address("rltc", "ltc regtest platform fee")

# Litecoin's OTHER testnet/regtest P2SH form -- version 58, the one whose addresses start `Q`.
# Read off litecoin/src/chainparams.cpp:259 (SCRIPT_ADDRESS2), not recalled.
LTC_P2SH_SCRIPT_ADDRESS2 = base58.b58encode_check(
    bytes([0x3A]) + _hash160("ltc testnet p2sh contract, second version byte")
).decode()

XRP_HOT_ACCOUNT = xrp_address("swap_terminal hot account")

# =======================================================================================
# BECH32M / TAPROOT FIXTURES, AND THE ENCODER IS DELIBERATELY NOT THE PRODUCTION ONE.
# =======================================================================================
#
# Added 2026-09-28. Before that date NO fixture in this file and no test in this suite used a
# bech32m address, and `modules/address_authority.check_address()` refused every one of them as
# INVALID -- so a Taproot payout address made a swap uncreatable, and an ALREADY CREDITED swap
# went to status='failed' terminally with the customer's coin in our wallet. Six independent
# review dimensions found that same root cause, and the reason no test caught it is this gap.
#
# THE CHECKSUM IS COMPUTED HERE RATHER THAN CALLED FROM modules/address_network. That is not
# duplication for its own sake, and rule 8 is the reason to state it: a fixture built by the
# same function that validates it proves only that the function agrees with itself. `bech32m`'s
# constant, charset and generator are written out below from BIP-350, so a sign error in the
# production decoder shows up as a test failure instead of cancelling out.
#
# Cross-checked the other way too: tests/test_address_network.py asserts these fixtures decode
# to the programs they were built from, AND asserts the published BIP-350 vectors -- which were
# generated by neither implementation -- decode to the spec's own expected scriptPubKeys.
BECH32M_CONSTANT = 0x2BC830A3
_BECH32_CHARSET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"
_BECH32_GENERATOR = (0x3B6A57B2, 0x26508E6D, 0x1EA119FA, 0x3D4233DD, 0x2A1462B3)


def _polymod(values: list[int]) -> int:
    """BIP-173's checksum polynomial, written out rather than imported (see above)."""
    chk = 1
    for value in values:
        top = chk >> 25
        chk = (chk & 0x1FFFFFF) << 5 ^ value
        for index in range(5):
            chk ^= _BECH32_GENERATOR[index] if ((top >> index) & 1) else 0
    return chk


def _hrp_expand(hrp: str) -> list[int]:
    return [ord(char) >> 5 for char in hrp] + [0] + [ord(char) & 31 for char in hrp]


def _to_five_bit(data: bytes) -> list[int]:
    """8-bit bytes -> 5-bit groups, padding the tail with zero bits."""
    acc, bits, out = 0, 0, []
    for byte in data:
        acc = (acc << 8) | byte
        bits += 8
        while bits >= 5:
            bits -= 5
            out.append((acc >> bits) & 31)
    if bits:
        out.append((acc << (5 - bits)) & 31)
    return out


def segwit_address(hrp: str, witness_version: int, program: bytes) -> str:
    """A segwit address, bech32 for v0 and bech32m for v1+, per BIP-350.

    The CONSTANT IS CHOSEN BY THE WITNESS VERSION, which is the whole of BIP-350 and the thing
    the production decoder has to get right: v0 uses 1, v1 and above use 0x2BC830A3. A fixture
    builder that used one constant for both would generate addresses no wallet accepts.
    """
    constant = 1 if witness_version == 0 else BECH32M_CONSTANT
    data = [witness_version, *_to_five_bit(program)]
    checksum_input = _hrp_expand(hrp) + data + [0, 0, 0, 0, 0, 0]
    polymod = _polymod(checksum_input) ^ constant
    checksum = [(polymod >> 5 * (5 - index)) & 31 for index in range(6)]
    return hrp + "1" + "".join(_BECH32_CHARSET[value] for value in data + checksum)


def taproot_address(hrp: str, phrase: str) -> str:
    """A P2TR address under `hrp`: witness v1, a 32-byte program, nobody holds the key.

    SHA256 of the phrase for the 32 bytes rather than _hash160, because a Taproot output key is
    32 bytes of x-only public key and not a HASH160 -- the distinction that makes
    `parse_and_reencode_as_testnet_p2pkh()` refuse these, correctly.
    """
    return segwit_address(hrp, 1, hashlib.sha256(phrase.encode()).digest())


# One per role and per network, so no test reuses a single Taproot address for two purposes.
BTC_TAPROOT_PAYOUT = taproot_address("tb", "btc testnet taproot payout")
BTC_TAPROOT_MAINNET = taproot_address("bc", "btc mainnet taproot -- must be refused on testnet")
BTC_REGTEST_TAPROOT = taproot_address("bcrt", "btc regtest taproot")
LTC_TAPROOT_PAYOUT = taproot_address("tltc", "ltc testnet taproot payout")
LTC_TAPROOT_PLATFORM_FEE = taproot_address("tltc", "ltc testnet taproot platform fee")
LTC_REGTEST_TAPROOT = taproot_address("rltc", "ltc regtest taproot")

# A witness version NOBODY HAS DEFINED, well-formed. It must be ACCEPTED: refusing a
# well-formed unknown version is refusing what is merely UNRECOGNIZED rather than what is
# CONTRADICTED, which is the principle modules/address_authority.py is built on -- and
# bech32m's absence was that exact failure.
BTC_FUTURE_WITNESS_V5 = segwit_address("tb", 5, hashlib.sha256(b"a witness version from 2030").digest())



def monero_testnet_address(spend_share: int, view_share: int) -> str:
    """A Monero TESTNET primary address, derived -- not a literal, and not a hash160.

    ADDED 2026-09-27 for modules/address_authority.py, which is the first thing in this
    repository that has to answer "is this a valid XMR address" on the fund path. There was
    no Monero fixture here before, and the reason that matters is the trap the authority
    exists to avoid: `address_network.is_valid_address()` understands three encodings and
    Monero is none of them, so a blanket guard at the payout site would have refused every
    valid XMR payout. A test asserting that needs a valid XMR address to refuse-or-not, and
    writing one as a literal is what tests/test_address_literals_are_valid.py forbids.

    _hash160() is NOT reusable here. A Monero address carries two ED25519 PUBLIC KEYS, not a
    key hash, and monero_keys.encode_address() refuses anything that is not a canonical
    point on the curve -- measured while writing this: twenty-two ordinary bytes raise
    MoneroKeyError("not a canonical ed25519 encoding"). So the keys come from
    public_key_for_share(), which multiplies the base point by a scalar and therefore cannot
    produce an off-curve value by construction. Nobody holds the scalars in any wallet.

    TESTNET on purpose, like every other fixture in this file. Monero's regtest/fakechain
    reuses the MAINNET prefixes (cryptonote_config.h:361, recorded in monero_keys.py), so
    "regtest" is not available as a safe third option the way `bcrt` is for Bitcoin.
    """
    return monero_keys.encode_address(
        "testnet",
        monero_keys.public_key_for_share(spend_share),
        monero_keys.public_key_for_share(view_share),
    )


def solana_address_for(phrase: str) -> str:
    """A Solana address: base58 of 32 bytes, with NO CHECKSUM, because that is the format.

    Worth a sentence, because it is the one fixture here that a typo cannot invalidate.
    Every other address in this file is protected by a checksum -- change a character and it
    stops decoding. A Solana address is a bare 32-byte ed25519 public key, so ANY 44-ish
    base58 string of the right length is "valid" and the only malformation available is a
    wrong LENGTH. tests/test_address_authority.py asserts exactly that limit rather than
    pretending the check is stronger than it is.

    SHA256 of the phrase is used directly: it is 32 bytes, deterministic, and says what the
    address is for. It is not required to be on the curve -- an off-curve key is a Program
    Derived Address, which is a legitimate destination (an Associated Token Account is one).
    """
    return base58.b58encode(hashlib.sha256(phrase.encode()).digest()).decode()


def unplaceable_base58(phrase: str) -> str:
    """A base58check address whose checksum HOLDS under a version byte no table here knows.

    THE UNDETERMINED FIXTURE, and it is a FUNCTION rather than an upper-case constant on
    purpose: ALL_VALID below sweeps every upper-case string in this module and
    tests/test_address_literals_are_valid.fixture_asset() then demands the name begin with a
    chain that has a validator. An address belonging to no chain cannot satisfy that, and a
    constant that had to be special-cased out of the sweep would be a hole in the sweep.

    WHY IT IS NEEDED AT ALL, measured 2026-09-27 on the operator's regtest run. Litecoin's
    `Q...` P2SH form -- version 0x3A, its SCRIPT_ADDRESS2 on testnet and regtest -- was
    missing from modules/address_network.P2SH_VERSIONS, so a real HTLC contract address that
    litecoind itself produced read as INVALID. The entry is there now; this stands in for the
    NEXT missing entry, which is the one that matters, because the state exists so that gap
    is a warning and not a refused customer payout.

    0x7B is used because it is unassigned in every table in modules/address_network.py --
    asserted by the caller rather than trusted here, since the whole point is that the set of
    known bytes changes.
    """
    return base58.b58encode_check(bytes([0x7B]) + _hash160(phrase)).decode()


def unplaceable_bech32(phrase: str) -> str:
    """A bech32 address whose checksum HOLDS under an hrp no table here knows.

    The bech32 half of unplaceable_base58(). `rltc` was exactly this until 2026-09-27 -- a
    real Litecoin regtest address off `litecoin-cli getnewaddress` that this repository read
    as "decodes as neither bech32 nor base58check". `doge` stands in for whatever the next
    one is; nothing in this tree pays Dogecoin, which is what makes it safe as a stand-in.
    """
    return bech32_address("doge", phrase)


XMR_PAYOUT = monero_testnet_address(7, 11)
XMR_SECOND_PAYOUT = monero_testnet_address(13, 17)

SOL_PAYOUT = solana_address_for("swap_terminal test fixture SOL payout")

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

# FIXTURES THAT ARE MAINNET ON PURPOSE, each with the reason it has to be.
#
# tests/test_address_literals_are_valid.test_no_shared_fixture_is_a_mainnet_address() refuses a
# mainnet fixture, and it is right to: a testnet suite reaching for a mainnet address by
# accident is the 2026-09-27 accident in miniature. But the network CHECK cannot be tested
# without one -- a receive-path test that cannot produce a mainnet address would pass just as
# well with the check deleted, which is how a guard becomes decoration.
#
# The repository's existing answer was to keep such an address out of this file entirely
# (GRC_MAINNET_NOBODY_HOLDS lives in test_address_authority.py for exactly this reason). That
# stops working the moment TWO test files need the same one: BTC_TAPROOT_MAINNET is needed by
# test_address_network.py for the network question and by test_address_authority.py for the
# receive-path refusal, and two copies of one fixture is rule 8's bug with a delay on it.
#
# So it is named here instead, and the naming is a CLAIM THAT GETS CHECKED rather than a way
# out of the gate: test_the_deliberately_mainnet_fixtures_really_are_mainnet() asserts each one
# IS mainnet and IS valid. A fixture listed here that turned out to be testnet, or invalid,
# fails -- so this cannot be used to smuggle a broken address past the sweep, only to say "this
# one is mainnet and here is why that is required".
DELIBERATELY_MAINNET = {
    "BTC_TAPROOT_MAINNET":
        "the receive-path network check has to be provable for bech32m too. Taproot was refused "
        "outright until 2026-09-28, so no test could reach the network question for it at all, "
        "and accepting bech32m without proving the mainnet refusal still fires would trade one "
        "live-money defect for another. Nobody holds its key: the program is SHA256 of a phrase.",
}

ALL_VALID = {
    name: value
    for name, value in sorted(globals().items())
    # `not name.startswith("_")`: Python's own convention for "not part of the interface", and
    # it was a real hole rather than tidiness. `"_BECH32_CHARSET".isupper()` is True -- an
    # underscore is not a cased character -- so the bech32m encoder's private charset constant
    # was swept in as a fixture and the gate tried to decode "qpzry9x8gf2tvdw0s3jn54khce6mua7l"
    # as an address, then demanded its name begin with a chain that has a validator. Any future
    # private upper-case helper string would have done the same.
    if name.isupper() and isinstance(value, str) and not name.startswith("_")
    and not name.startswith("INVALID")
    and not name.endswith("_WITH_TYPO")
    and not name.endswith("ALPHABET") and not name.endswith("VERSION")
    and name not in DELIBERATELY_MAINNET
}
