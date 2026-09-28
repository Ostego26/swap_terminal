"""Which network an address belongs to, decoded from its version byte -- never guessed from
its first character.

Role: submodule (one decision, no I/O)
Reads: nothing. Pure function of the string handed to it.
Writes: nothing
Can move funds: no. It is the check that stops something else from moving them on the wrong
       chain, which is the opposite.
Mainnet-safe: yes -- it never contacts a chain. Its whole purpose is telling one from another.
Live-safe: yes

WHY THIS EXISTS: A LEADING CHARACTER IS NOT A NETWORK, AND THE GAP BIT ON 2026-09-27.

Six places in this tree decided a Gridcoin address's network by its first letter, and the
claim they all rested on -- "a mainnet Gridcoin address starts with S" -- is FALSE. Measured
here by encoding 200,000 random hash160s under each version byte:

    version        leading characters observed (200,000 samples each)
    0x3E mainnet   S  86.92%   R  13.08%
    0x6F testnet   m  83.39%   n  16.61%

Exact boundaries, computed rather than sampled: a mainnet address runs from Rwx... (hash160
all zero bytes) to SMJ... (all 0xFF), and a testnet one from mfW... to n4r...

So `not address.startswith("S")` calls a mainnet address TESTNET for 13.08% of all possible
keys. That is not a theoretical share. The same day this was written I handed the operator a
`gridcoinresearchd getnewaddress` with no `-testnet`, which created

    RyyNX8E7tDRzACL47h8JKKDUXf9aMCz8YV

in their MAINNET wallet -- the wallet that holds their real staking balance. It starts with R.
Every one of those six checks would have waved it through as a testnet address, which is the
precise failure they were written to prevent. The accident landed inside the 13%, and it
landed there because 13% of addresses do.

WHY A VERSION BYTE IS THE ONLY HONEST ANSWER. base58check encodes a 21-byte payload as a
number, and the leading character is a property of that NUMBER, not of the version byte -- it
shifts as soon as the hash160's high bits move. The version byte is the field that names the
network, by construction, and reading it is one decode. There was never a cheaper correct
check to trade against.

ONE VOCABULARY, per rule 11, and it was already two. TESTNET_P2PKH_VERSION was spelled in
modules/atomic_htlc_scripts.py AND regtest/keys.py, agreeing on the day they were written.
Both now import it from here, so a third caller cannot disagree with either.

BECH32 IS ANSWERED TOO, and this paragraph used to say it was not. The original version
refused bech32 on the grounds that its network lives in a human-readable prefix rather than a
version byte, which is true and was the wrong conclusion: the hrp NAMES the network as
definitively as a version byte does, and BECH32_HRPS below is that mapping. What made the old
refusal untenable was adding the validity checker further down -- it had to know the hrps
anyway, so refusing to answer here would have been two modules knowing one vocabulary and only
one of them admitting it (rule 8). Measured consequence of the old behavior: every bech32
fixture in this repository decoded as UNKNOWN, so a test asserting "no fixture is mainnet"
passed for `bc1...` addresses.

THAT SENTENCE USED TO READ "this still changes no fund-path behavior", AND IT IS NO LONGER
TRUE -- corrected 2026-09-27 rather than left to be trusted (rule 16: a wrong comment is a
bug). The operator's instruction was "obviously spin up an agent and make this burn proof",
and the tables and decoders below are now READ ON THE FUND PATH, through
modules/address_authority.py: by services/payout_service.py before a send, by
services/swap_service.py before a swap row is written, by services/swap_view.py before a
deposit address is shown, and by modules/htlc_fee.py before a platform fee output is added.
A missing entry in any table here is therefore a live defect and not a reporting gap -- which
it promptly was: `rltc` and 0x3A were both absent, and both refused a real Litecoin address.

`parse_and_reencode_as_testnet_p2pkh()` still ignores the hrp, and that has not changed. An
address this module cannot decode returns UNKNOWN with the reason, never a network -- and
modules/address_authority.py is deliberately careful never to turn UNKNOWN into a refusal, for
the reason written at length in its own header.
"""

from __future__ import annotations

from typing import NamedTuple

import base58
import bech32

# THE VOCABULARY. Version byte -> (chain, network). Gridcoin's 0x3E and 0x6F are from its own
# src/chainparams.cpp; Bitcoin and Litecoin testnet share 0x6F, which is WHY a version byte
# names a network and not a coin, and why this maps to a network rather than to a ticker.
#
# 0x6F is deliberately ONE entry and not three. BTC testnet, LTC testnet and GRC testnet all
# use it, so an address decoded from it is "some testnet" -- which is exactly the question
# every caller here is asking. A module that claimed to tell BTC testnet from GRC testnet by
# version byte would be claiming something the encoding does not carry.
MAINNET = "mainnet"
TESTNET = "testnet"
UNKNOWN = "unknown"

P2PKH_VERSIONS: dict[int, str] = {
    0x00: MAINNET,   # BTC mainnet
    0x30: MAINNET,   # LTC mainnet
    0x3E: MAINNET,   # GRC mainnet -- the one that starts with R 13.08% of the time
    0x6F: TESTNET,   # BTC, LTC and GRC testnet/regtest alike
}
#
# LITECOIN HAS TWO P2SH VERSION BYTES PER NETWORK AND BOTH ARE LIVE AT ONCE. This is not a
# migration where one superseded the other: Litecoin's chainparams.cpp declares
# SCRIPT_ADDRESS *and* SCRIPT_ADDRESS2 for every network, litecoind DECODES both, and it
# ENCODES new addresses with the second. So a Litecoin P2SH address may begin `3`/`M` on
# mainnet or `2`/`Q` on testnet, and a table with one of each pair rejects half of them.
#
# 0x3A WAS MISSING UNTIL 2026-09-27, AND IT COST A REAL REFUSAL. The operator ran a
# BTC->LTC atomic swap on regtest against litecoind 0.21.4, and the HTLC contract address
# litecoind produced for the funded participant leg was
#
#     QYqEyFb1v76QraQ3uo5wVkGtJrC4Rf5vU3        21 bytes, version 0x3a
#
# which this table read as UNKNOWN -- so the address the money was actually sent to
# decoded as belonging to no network. That is the false-refusal failure mode, which is
# WORSE than the burn the validity checker exists to prevent: a burn costs one payout, a
# false refusal breaks a working chain for every customer on it.
#
# MEASURED, NOT RECALLED, from each project's own chainparams.cpp on master, fetched
# 2026-09-27 -- because the same day the operator and I were each wrong about a version
# byte once already:
#
#   litecoin-project/litecoin  src/chainparams.cpp:145-152  mainnet  PUBKEY 48  SCRIPT 5   SCRIPT2 50  hrp ltc
#                                                :257-264  testnet  PUBKEY 111 SCRIPT 196 SCRIPT2 58  hrp tltc
#                                                :371-378  regtest  PUBKEY 111 SCRIPT 196 SCRIPT2 58  hrp rltc
#   bitcoin/bitcoin  src/kernel/chainparams.cpp:176-182  mainnet  PUBKEY 0   SCRIPT 5    hrp bc
#                                              :297-303  testnet  PUBKEY 111 SCRIPT 196  hrp tb
#                                              :672-678  regtest  PUBKEY 111 SCRIPT 196  hrp bcrt
#   gridcoin-community/Gridcoin-Research  src/chainparams.cpp:111-113  mainnet PUBKEY 62 SCRIPT 85
#                                                              :230-232  testnet PUBKEY 111 SCRIPT 196
#
# Two things fall out of that survey and both are recorded rather than assumed. Bitcoin and
# Gridcoin have NO SCRIPT_ADDRESS2, so 0x3A and 0x32 belong to Litecoin alone -- which is why
# BASE58_VERSION_ASSETS below can name LTC alone for them even on a TEST network, where every other
# byte is shared. And Gridcoin's chainparams.cpp sets no `bech32_hrp` AT ALL, which is the
# measurement behind "Gridcoin has no bech32"; that claim used to rest on nobody having seen
# one.
P2SH_VERSIONS: dict[int, str] = {
    0x05: MAINNET,   # BTC mainnet SCRIPT_ADDRESS -- and LTC's too (both declare 5)
    0x32: MAINNET,   # LTC mainnet SCRIPT_ADDRESS2 (50): the `M...` form litecoind encodes
    0x55: MAINNET,   # GRC mainnet
    0xC4: TESTNET,   # BTC, LTC and GRC testnet/regtest SCRIPT_ADDRESS (196)
    0x3A: TESTNET,   # LTC testnet AND regtest SCRIPT_ADDRESS2 (58): the `Q...` form
}

# Re-exported so the two older copies of this byte can derive from one place (rule 8).
TESTNET_P2PKH_VERSION: bytes = b"\x6f"
TESTNET_P2SH_VERSION: bytes = b"\xc4"

BASE58_VERSIONED_HASH160_LEN = 21

# Human-readable parts this repository can pay out to. A bech32 string whose hrp is not here
# is UNKNOWN rather than accepted on the strength of its checksum: a valid address on a chain
# we cannot reach is still an address the money never comes back from.
#
# KEYED BY ASSET SINCE 2026-09-27, and BECH32_HRPS below is DERIVED from it rather than
# spelled a second time (rule 8). The flat hrp -> network map was all this module needed
# while it only answered "which network"; modules/address_authority.py has to answer a
# second question -- "is this hrp one THIS chain uses" -- and a `ltc1...` string handed in
# as a BTC payout address is a real burn that the flat map cannot see, because it is a
# perfectly valid mainnet address. It is just not Bitcoin's.
#
# GRC is present with an EMPTY table on purpose. "Gridcoin has no bech32" is a fact the
# authority has to be able to read off this vocabulary, and an absent key would mean
# "nobody has said", which is the distinction this module refuses to collapse everywhere
# else. _bech32_result() below already hard-codes the same refusal for grc1/tgrc1; that
# branch stays because it also catches the string BEFORE any asset is known.
BECH32_HRPS_BY_ASSET: dict[str, dict[str, str]] = {
    "BTC": {"bc": MAINNET, "tb": TESTNET, "bcrt": TESTNET},
    # rltc IS LITECOIN'S REGTEST HRP AND IT WAS MISSING UNTIL 2026-09-27. Regtest is its own
    # human-readable part, exactly as Bitcoin's is `bcrt` and not `tb` -- read off
    # litecoin/src/chainparams.cpp:378, not recalled. Without it,
    #
    #     rltc1q7u6dnatxpsds4wvq2svx3h64v8s03cf69xg52q
    #
    # -- a real address off `litecoin-cli getnewaddress` during the operator's BTC->LTC
    # regtest swap -- read as "not decodable as base58check", because an unknown hrp fell
    # through the bech32 branch entirely and was then tried as base58. Every Litecoin regtest
    # payout address would have been refused.
    "LTC": {"ltc": MAINNET, "tltc": TESTNET, "rltc": TESTNET},
    "GRC": {},
}

BECH32_HRPS = {hrp: network for table in BECH32_HRPS_BY_ASSET.values() for hrp, network in table.items()}

# WHICH CHAINS a base58 version byte can belong to. A SET PER BYTE, never one-or-None.
#
# THIS WAS A dict[int, str | None] UNTIL A MUTATION EXPOSED THE HOLE, 2026-09-27, and the
# SHAPE is the fix rather than any one entry. None meant "shared, cannot narrow", which left a
# caller two options: accept the address for every chain, or refuse it for every chain. Both
# are wrong for 0x05. Bitcoin and Litecoin both declare SCRIPT_ADDRESS = 5 and Gridcoin
# declares 85, so a `3...` address is ambiguous between TWO chains and definitively not the
# third -- and the old shape could not say that, so modules/address_authority.py accepted a
# Bitcoin P2SH address as a GRIDCOIN payout destination. A set says exactly what is known and
# exactly what is not.
#
# Found by MUTATING the byte, not by reading the code: flipping 0x05 from None to "BTC" killed
# no test, which is what sent somebody to look at it at all.
#
# EVERY ENTRY MEASURED from the three projects' own chainparams.cpp on master, fetched
# 2026-09-27; the line numbers are in the P2SH_VERSIONS comment above. Two facts shape the
# whole table: the test-network bytes 111 and 196 are declared by all three chains, and only
# Litecoin declares a SCRIPT_ADDRESS2 at all.
#
# A byte whose set has MORE THAN ONE member cannot be narrowed by decoding, and a caller must
# not pretend otherwise: refusing a 0x6F address as "not Bitcoin" would refuse every valid
# Bitcoin testnet address in this repository, which is the false-refusal outage that is worse
# than the burn this whole area exists to prevent.
#
# tests/test_address_authority.py asserts these keys are EXACTLY the keys of
# P2PKH_VERSIONS | P2SH_VERSIONS, so the two tables cannot drift apart the way rule 8 says two
# copies of one vocabulary always do.
BASE58_VERSION_ASSETS: dict[int, frozenset[str]] = {
    0x00: frozenset({"BTC"}),                # BTC mainnet PUBKEY_ADDRESS (0)
    0x05: frozenset({"BTC", "LTC"}),         # SCRIPT_ADDRESS (5) -- BOTH chains declare it
    0x30: frozenset({"LTC"}),                # LTC mainnet PUBKEY_ADDRESS (48)
    0x32: frozenset({"LTC"}),                # LTC mainnet SCRIPT_ADDRESS2 (50) -- LTC alone
    0x3A: frozenset({"LTC"}),                # LTC test/regtest SCRIPT_ADDRESS2 (58) -- LTC alone
    0x3E: frozenset({"GRC"}),                # GRC mainnet (62) -- starts with R 13.08% of the time
    0x55: frozenset({"GRC"}),                # GRC mainnet SCRIPT_ADDRESS (85)
    0x6F: frozenset({"BTC", "LTC", "GRC"}),  # PUBKEY_ADDRESS (111) on every test network
    0xC4: frozenset({"BTC", "LTC", "GRC"}),  # SCRIPT_ADDRESS (196) on every test network
}


def _claimed_hrp(raw: str) -> str | None:
    """The bech32 hrp this string claims, longest first, or None. THE one copy of that scan.

    MERGED 2026-09-28, and the merge is rule 8 rather than tidiness. This loop --

        for hrp in sorted(BECH32_HRPS, key=len, reverse=True):
            if lowered.startswith(hrp + "1"):

    -- was spelled THREE times in this file: in _bech32_network(), in _bech32_result() and in
    bech32_hrp(). All three agreed on the day they were written, which is exactly the state
    rule 8 describes as a bug with a delay on it, and this file had already paid for it once:
    `rltc` was added to BECH32_HRPS_BY_ASSET and every one of the three picked it up only
    because the table is shared. A fourth caller that reached for `startswith("tltc1")`
    directly, or a change to the ordering in one of the three, would not have been visible in
    the other two.

    LONGEST FIRST IS KEPT AND IS NOT CURRENTLY LOAD-BEARING, and saying which is the point --
    the three copies each carried it as though it were the reason they were correct, and it is
    not. MEASURED 2026-09-28 over the real table rather than reasoned about (rule 17):

        hrps                             bc, bcrt, ltc, rltc, tb, tltc
        pairs where one `hrp + "1"`      (none)
          prefixes another
        hrps containing a "1"            (none)
        same answer under longest-first  yes, for every hrp in the table
          and shortest-first

    The SEPARATOR is what makes it unambiguous, and that is the thing worth writing down: the
    scan matches `hrp + "1"`, not `hrp`, so `bcrt1...` never matches `bc1` and `tltc1...` never
    matches `ltc1` -- no ordering can confuse them. It would take an hrp containing the
    separator character to make the sort matter, which bech32 permits (the separator is the
    LAST "1") and no chain here uses.

    So the sort is cheap insurance against that hrp arriving, kept rather than removed because
    removing it would be a behavior-identical edit to a fund-path decoder. What it must NOT be
    is cited as the reason `bcrt` resolves correctly: tests/test_address_network.py::
    test_bech32_hrps_map_to_the_network_they_actually_name proves `bcrt1...` is TESTNET, and it
    would still pass with the sort reversed. A mainnet/regtest confusion IS the 2026-09-27
    accident's shape; the separator is what prevents it here, and the version-byte tables are
    what prevent it on the base58 side.
    """
    if not isinstance(raw, str):
        return None
    lowered = raw.strip().lower()
    for hrp in sorted(BECH32_HRPS, key=len, reverse=True):
        if lowered.startswith(hrp + "1"):
            return hrp
    return None


def address_network(address: str) -> tuple[str, str]:
    """(network, why) for a base58check address, from its version byte.

    Returns UNKNOWN with an explanation rather than raising, and rather than defaulting to
    either network. That is the same shape htlc_vout() uses for a missing output and for the
    same reason: a caller that cannot tell must be told it cannot tell, never handed the
    safer-sounding answer. Defaulting to MAINNET would block legitimate testnet work and
    defaulting to TESTNET would do what the 2026-09-27 accident did.

    `why` is written to be printed. The operator reads the screen, not this source (rule 14),
    and "0x3e is GRC mainnet" is the sentence that settles an argument about an address.
    """
    if not isinstance(address, str) or not address.strip():
        return UNKNOWN, f"not a non-empty string: {address!r}"
    raw = address.strip()
    bech32_answer = _bech32_network(raw)
    if bech32_answer is not None:
        return bech32_answer
    decoded = _decode_or_none(raw, BITCOIN_BASE58_ALPHABET)
    if decoded is None:
        return UNKNOWN, "not decodable as base58check"
    if len(decoded) != BASE58_VERSIONED_HASH160_LEN:
        return UNKNOWN, f"decoded to {len(decoded)} bytes, not {BASE58_VERSIONED_HASH160_LEN}"
    version = decoded[0]
    for table, kind in ((P2PKH_VERSIONS, "P2PKH"), (P2SH_VERSIONS, "P2SH")):
        if version in table:
            return table[version], f"version byte {version:#04x} is a {table[version]} {kind} version"
    return UNKNOWN, f"version byte {version:#04x} is in no table this module knows"


# =======================================================================================
# BIP-350 / bech32m, AND THE OUTAGE ITS ABSENCE WAS CAUSING.
# =======================================================================================
#
# `bech32.bech32_decode()` -- the installed BIP-173 reference decoder -- verifies ONE checksum
# constant, 1. Read from the installed package:
#
#     def bech32_verify_checksum(hrp, data):
#         return bech32_polymod(bech32_hrp_expand(hrp) + list(data)) == 1
#
# BIP-350 changed the constant to 0x2BC830A3 for witness version 1 and above, which is every
# Taproot address. So that decoder returns (None, None) for EVERY `bc1p...`, `tb1p...`,
# `bcrt1p...`, `ltc1p...`, `tltc1p...` and `rltc1p...` address ever issued, and every caller
# in this tree read that as "the checksum or the character set is wrong".
#
# MEASURED 2026-09-28, against the published BIP-350 test vectors, before this was written:
#
#     INVALID   BTC  bc1p0xlxvlhemja6c4dqv22uapctqupfhlxm9h8z3k2e72q4k9hcz7vqzk5jj0
#     INVALID   BTC  BC1SW50QGDZ25J
#     INVALID   BTC  bc1zw508d6qejxtdg4y5r3zarvaryvaxxpcs
#     INVALID   BTC  tb1pqqqqp399et2xygdj5xreqhjjvcmzhxw4aywxecjdzew6hylgvsesrxh6hy
#     VALID     LTC  ltc1qw508d6qejxtdg4y5r3zarvary0c5xw7kgmn4n9      <- v0 control
#
# WHAT THAT COST, and it is the exact outage modules/address_authority.py's own header says is
# WORSE than the burn it was written to prevent. A confident INVALID on a perfectly spendable
# address, in five places, because `bech32_decode` was called five times across three modules
# instead of once:
#
#   services/swap_service.py     a swap paying out to a taproot address CANNOT BE CREATED --
#                                ValueError, no row written. Taproot is the default receive
#                                type in Sparrow, Muun, Phoenix and `bitcoin-cli getnewaddress
#                                "" bech32m`, so this is not an edge case.
#   services/payout_service.py   worse, and this is the blocking half: a swap that ALREADY
#                                EXISTS and whose deposit has ALREADY BEEN CREDITED is set to
#                                status='failed' with a failed_reason, before reserve_inventory
#                                and before the payouts INSERT. Nothing is sent. 'failed' is
#                                terminal by design -- correctly, for a genuinely bad address --
#                                so it is never retried. The customer's coin is in our wallet
#                                and their swap is permanently dead.
#   modules/htlc_fee.py          a valid taproot PLATFORM_FEE_<ASSET>_ADDRESS silently drops the
#                                fee output. 1.5% of every redeem stays with the redeemer.
#   modules/atomic_htlc_scripts  refuses to build a script to a taproot destination at all.
#
# Six independent review dimensions found this same root cause separately, which is what a
# single decision copied to five call sites produces (rule 8).
#
# ONE DECODER, HERE, BECAUSE THIS MODULE ALREADY OWNS THE VOCABULARY. Rule 8's survivor owns
# the concept: BECH32_HRPS_BY_ASSET is here, so the segwit decoding rule is here too, and the
# other four sites call it. Built on the library's OWN primitives -- bech32_polymod,
# bech32_hrp_expand, CHARSET, convertbits -- rather than on a second copy of the charset and
# the generator polynomial, which is the mistake this whole comment is about.
BECH32_CHECKSUM_CONSTANT = 1
BECH32M_CHECKSUM_CONSTANT = 0x2BC830A3

# BIP-141/BIP-350's program-length rules. v0 is EXACTLY 20 (P2WPKH) or 32 (P2WSH); v1+ is
# 2..40 inclusive, which is what lets a future witness version be forwarded to without this
# file being changed -- the "refuse only what is CONTRADICTED" principle applied to a version
# nobody has defined yet.
WITNESS_V0_PROGRAM_LENGTHS = (20, 32)
WITNESS_PROGRAM_MIN_LEN = 2
WITNESS_PROGRAM_MAX_LEN = 40
MAX_BECH32_LENGTH = 90


class SegwitAddress(NamedTuple):
    """A decoded segwit address, or the reason it is not one.

    `hrp` is None exactly when this is not a segwit address at all. `why` is ALWAYS populated,
    because every caller here reports a reason to an operator or a customer (rule 14), and a
    bare False has sent somebody to check a wallet that was fine.
    """

    hrp: str | None
    witness_version: int | None
    program: bytes | None
    encoding: str | None          # "bech32" | "bech32m" | None
    why: str

    @property
    def ok(self) -> bool:
        return self.program is not None


# BIP-173's own bounds on the string itself, named so no comparison below is a bare number.
PRINTABLE_ASCII_LOW = 33
PRINTABLE_ASCII_HIGH = 126
MAX_WITNESS_VERSION = 16
TAPROOT_PROGRAM_LEN = 32
TAPROOT_WITNESS_VERSION = 1


def bech32_string_refusal(raw: str) -> str | None:
    """Why this string cannot be bech32 AS TEXT, before any hrp or checksum is considered.

    A SEPARATE FUNCTION because these are facts about the characters, not about the address:
    emptiness, mixed case, length and the printable-ASCII range. Extracting them is rule 12's
    answer to a complexity finding -- pull the decision out where it can be called with seeded
    inputs, never raise the ceiling and never add a noqa (rule 19 forbids both).
    """
    if not raw:
        return "empty string is not an address"
    # Mixed case is invalid per BIP-173: the checksum is computed over ONE case, so a mixed
    # string is ambiguous rather than merely ugly.
    if raw.lower() != raw and raw.upper() != raw:
        return "mixed case, which BIP-173 forbids"
    if len(raw) > MAX_BECH32_LENGTH:
        return f"{len(raw)} characters, over BIP-173's {MAX_BECH32_LENGTH} limit"
    if any(not PRINTABLE_ASCII_LOW <= ord(char) <= PRINTABLE_ASCII_HIGH for char in raw):
        return "contains a character outside printable ASCII"
    return None


def _segwit_split(raw: str) -> tuple[str, list[int]] | SegwitAddress:
    """The hrp and the 5-bit values, or the SegwitAddress explaining why this is not one.

    "Is this bech32-shaped" only. Which checksum it satisfies is segwit_encoding_of(), and
    whether its witness program is legal is witness_program_refusal(); three questions, three
    functions, each assertable on its own.
    """
    text_refusal = bech32_string_refusal(raw)
    if text_refusal is not None:
        return SegwitAddress(None, None, None, None, text_refusal)
    lowered = raw.lower()
    separator = lowered.rfind("1")
    if separator < 1 or separator + 7 > len(lowered):
        return SegwitAddress(
            None, None, None, None,
            "no bech32 separator with an hrp before it and a checksum after it",
        )
    hrp = lowered[:separator]
    body = lowered[separator + 1:]
    if not all(char in bech32.CHARSET for char in body):
        return SegwitAddress(
            hrp, None, None, None,
            f"claims hrp {hrp!r} but the data part uses characters outside bech32's charset",
        )
    return hrp, [bech32.CHARSET.find(char) for char in body]


def segwit_encoding_of(hrp: str, values: list[int]) -> str | None:
    """"bech32", "bech32m", or None when the string satisfies NEITHER constant.

    THE ONE DECISION BIP-350 IS ABOUT, as a function, so it can be asserted directly against
    the published vectors rather than only through a whole address. The polymod is computed
    once over hrp-expansion plus every value INCLUDING the six checksum characters -- that is
    what makes the result a constant rather than a comparison.
    """
    polymod = bech32.bech32_polymod(bech32.bech32_hrp_expand(hrp) + values)
    if polymod == BECH32_CHECKSUM_CONSTANT:
        return "bech32"
    if polymod == BECH32M_CHECKSUM_CONSTANT:
        return "bech32m"
    return None


def witness_program_refusal(witness_version: int, program: bytes, encoding: str) -> str | None:
    """Why this witness version / program / encoding triple is illegal, or None if it is legal.

    THE VERSION-TO-ENCODING BINDING IS THE POINT, and it is why this cannot be "try bech32,
    then try bech32m". BIP-350 ties v0 to bech32 and v1+ to bech32m, so a v0 address carrying a
    bech32m checksum is a CORRUPTION, not an alternative spelling -- accepting it would accept
    exactly the class of malformed address the constant change exists to separate.

    v1+ accepts any 2..40 byte program, so a witness version nobody has defined yet is
    forwarded to without editing this file. That is "refuse only what is CONTRADICTED" applied
    to the future, and it is the principle modules/address_authority.py's header is built on.
    """
    if witness_version == 0:
        return _witness_v0_refusal(program, encoding)
    return _witness_v1_plus_refusal(witness_version, program, encoding)


def _witness_v0_refusal(program: bytes, encoding: str) -> str | None:
    """v0's rules: bech32 exactly, and a program of exactly 20 or 32 bytes."""
    if encoding != "bech32":
        return (
            "witness version 0 with a bech32m checksum. BIP-350 ties v0 to bech32, so this "
            "is a corrupted address rather than another spelling of a valid one"
        )
    if len(program) not in WITNESS_V0_PROGRAM_LENGTHS:
        return (
            f"witness version 0 with a {len(program)}-byte program, which is neither 20 "
            f"(P2WPKH) nor 32 (P2WSH)"
        )
    return None


def _witness_v1_plus_refusal(witness_version: int, program: bytes, encoding: str) -> str | None:
    """v1 and above: bech32m exactly, version at most 16, program 2..40 bytes.

    DELIBERATELY PERMISSIVE ON THE VERSION. Any 2..40 byte program at any version up to 16 is
    accepted, so a witness version nobody has defined yet is forwarded to without editing this
    file. Refusing an unrecognized-but-well-formed version would be refusing what is merely
    UNRECOGNIZED rather than what is CONTRADICTED, which is the principle
    modules/address_authority.py's header is built on -- and the failure that principle exists
    to prevent is exactly the one bech32m's absence was causing.
    """
    if encoding != "bech32m":
        return (
            f"witness version {witness_version} with a bech32 checksum. BIP-350 ties v1 and "
            f"above to bech32m, so this is a corrupted address"
        )
    if witness_version > MAX_WITNESS_VERSION:
        return (
            f"witness version {witness_version} is above {MAX_WITNESS_VERSION}, which no "
            f"opcode can express"
        )
    if not WITNESS_PROGRAM_MIN_LEN <= len(program) <= WITNESS_PROGRAM_MAX_LEN:
        return (
            f"witness version {witness_version} with a {len(program)}-byte program, outside "
            f"BIP-141's {WITNESS_PROGRAM_MIN_LEN}..{WITNESS_PROGRAM_MAX_LEN} range"
        )
    return None


def decode_segwit_address(address: str) -> SegwitAddress:
    """Decode a bech32 OR bech32m address per BIP-173 and BIP-350. THE one copy.

    Three decisions, each its own function above so each is callable with seeded inputs: is this
    bech32-shaped (_segwit_split), which constant does it satisfy (segwit_encoding_of), and is
    the version/program/encoding triple legal (witness_program_refusal). This function only
    sequences them, which is rule 10's shape -- the thing that decides is the smallest piece.

    Returns the program as BYTES so a caller can build a scriptPubKey without a second
    convertbits, and names the encoding so a diagnostic can say which rule the address met.
    """
    split = _segwit_split(address.strip())
    if isinstance(split, SegwitAddress):
        return split
    hrp, values = split

    encoding = segwit_encoding_of(hrp, values)
    if encoding is None:
        return SegwitAddress(
            hrp, None, None, None,
            f"claims hrp {hrp!r} but matches NEITHER bech32's checksum constant nor bech32m's. "
            f"This is the shape a typo or a truncated copy-paste makes",
        )

    data = values[:-6]
    if not data:
        return SegwitAddress(hrp, None, None, encoding, f"valid {encoding} but carries no witness version")
    witness_version = data[0]
    program_list = bech32.convertbits(data[1:], 5, 8, False)
    if program_list is None:
        return SegwitAddress(
            hrp, witness_version, None, encoding,
            f"valid {encoding} but the witness program has leftover bits -- a padding error, "
            f"which BIP-173 rejects rather than truncating",
        )
    program = bytes(program_list)

    refusal = witness_program_refusal(witness_version, program, encoding)
    if refusal is not None:
        return SegwitAddress(hrp, witness_version, None, encoding, refusal)

    kind = (
        "P2TR"
        if witness_version == TAPROOT_WITNESS_VERSION and len(program) == TAPROOT_PROGRAM_LEN
        else f"witness v{witness_version}"
    )
    return SegwitAddress(
        hrp, witness_version, program, encoding,
        f"valid {encoding}, hrp={hrp!r}, {kind}, {len(program)}-byte program",
    )


def _bech32_network(raw: str) -> tuple[str, str] | None:
    """The network an hrp names, or None when this string does not claim to be bech32.

    Extracted for ruff's return-statement ceiling, which rule 12 says to answer by extracting
    rather than by raising the ceiling. The hrp NAMES the network as definitively as a version
    byte does; BECH32_HRPS is that vocabulary, shared with the validity checker below so one
    module does not know it twice.
    """
    hrp = _claimed_hrp(raw)
    if hrp is None:
        return None
    # decode_segwit_address(), NOT bech32.bech32_decode(): the library's decoder knows only
    # BIP-173's checksum constant, so it answered None for every Taproot address and this
    # function reported a perfectly good bc1p... as UNKNOWN. See the BIP-350 block above.
    decoded = decode_segwit_address(raw)
    if not decoded.ok:
        return UNKNOWN, f"claims hrp {hrp} but {decoded.why}"
    return BECH32_HRPS[hrp], f"{decoded.encoding} hrp {hrp!r} is {BECH32_HRPS[hrp]} ({decoded.why})"


def is_testnet_address(address: str) -> bool:
    """True only when the version byte SAYS testnet. UNKNOWN is not testnet.

    The boolean every one of the six replaced checks wanted. It is False for an undecodable
    address on purpose: "I could not tell" and "it is testnet" must not be the same value,
    which is the general form of the thing that broke here (rule 2's "I could not find a
    caller is not there is no caller").
    """
    return address_network(address)[0] == TESTNET


# ---------------------------------------------------------------------------------------
# VALIDITY, which is a different question from NETWORK and is asked in more places.
#
# Operator, 2026-09-27: "make sure any an all addresses used for any transaction are valid
# addresses that pass all tests." The network decoder above answers "which chain"; this
# answers "could this string ever receive money at all", and the second is the one that was
# being answered by eye across this tree.
#
# THREE ENCODINGS, because this repository pays out on chains that use three:
#
#   base58check      BTC/LTC/GRC P2PKH and P2SH. 21-byte payload, Bitcoin's alphabet.
#   bech32           BTC and LTC segwit (bc1/tb1/bcrt1/ltc1/tltc1). NOT Gridcoin -- see below.
#   XRP base58check  the same construction with a DIFFERENT alphabet, which is why an XRP
#                    address decoded with Bitcoin's alphabet fails and vice versa.
#
# GRIDCOIN HAS NO BECH32 AT ALL, and that is worth a line of its own because the tree
# contained `tgrc1qexampleparticipantaddress...` in atomic_grc_client.py's usage example.
# There is no such format: Gridcoin is base58 only. A reader copying that example would
# write an address no Gridcoin daemon can parse, and the fee paid to it would be burned --
# the same class of loss the PLATFORM_FEE_TESTNET_DEFAULT burn was.
# ---------------------------------------------------------------------------------------

# Bitcoin's base58 alphabet and XRP's are the same 58 characters in a DIFFERENT ORDER. That
# is the whole difference, and it is why a valid XRP address is not a valid Bitcoin one: the
# checksum is over the decoded bytes, so a different alphabet decodes to different bytes.
BITCOIN_BASE58_ALPHABET = b"123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
XRP_BASE58_ALPHABET = b"rpshnaf39wBUDNEGHJKLM4PQRST7VWXYZ2bcdeCg65jkm8oFqi1tuvAxyz"

def _bech32_result(raw: str) -> tuple[bool, str] | None:
    """The bech32 verdict, or None when this string is not claiming to be bech32.

    Extracted because decodes_as_address() crossed ruff's return-statement ceiling, and rule 12
    says a complexity finding is extracted rather than suppressed. It earns its own name
    anyway: "which hrp, and is that a chain we can reach" is one question, and the answer has
    two failure modes -- a bad checksum, and a well-formed address on a chain with no bech32 --
    that a caller must be able to tell apart.
    """
    if raw.lower().startswith(("grc1", "tgrc1")):
        return False, "Gridcoin has no bech32 format at all -- it is base58 only"
    hrp = _claimed_hrp(raw)
    if hrp is None:
        return None
    # Same substitution, same reason. This is the site services/swap_service.py reaches
    # through is_valid_address(), so before this a taproot deposit or payout address made a
    # swap uncreatable.
    decoded = decode_segwit_address(raw)
    if not decoded.ok:
        return False, f"starts with {hrp}1 but {decoded.why}"
    return True, f"{decoded.why}, network {BECH32_HRPS[hrp]}"


def _base58_result(raw: str) -> tuple[bool, str] | None:
    """The base58check verdict under either alphabet, or None when neither decodes it.

    The two alphabets are TRIED rather than chosen, because nothing in the string says which
    one it used -- Bitcoin's and XRP's are the same 58 characters in a different order, so the
    only way to know is to decode and see whether the checksum holds.

    The failure of one alphabet is NOT logged or discarded ambiguously (ruff's S112): a miss
    means "not this encoding", the loop moves on, and a miss on BOTH returns None so the caller
    reports "decodes as nothing" with its own words. That is the condition rule 12 puts on a
    broad catch -- the caller can tell a failure from an answer.
    """
    for alphabet, name in ((BITCOIN_BASE58_ALPHABET, "base58check"),
                           (XRP_BASE58_ALPHABET, "XRP base58check")):
        payload = _decode_or_none(raw, alphabet)
        if payload is None:
            continue
        if len(payload) != BASE58_VERSIONED_HASH160_LEN:
            return False, f"{name} decoded to {len(payload)} bytes, not {BASE58_VERSIONED_HASH160_LEN}"
        return True, f"valid {name}, version byte {payload[0]:#04x}"
    return None


def _decode_or_none(raw: str, alphabet: bytes) -> bytes | None:
    """base58check under one alphabet, or None. One line, and it exists so the probe's broad
    catch sits in a function whose whole contract is "None means it is not this encoding"."""
    try:
        return base58.b58decode_check(raw, alphabet=alphabet)
    except Exception:  # noqa: BLE001 -- checked: this function's ONLY job is to answer "does this decode under this alphabet", and None says no. base58 raises several unrelated types for a bad checksum, a character outside the alphabet and a payload too short to hold one, and all three mean the same thing to every caller. Nothing here can mistake the failure for an answer, because the return type has no other value that means success.
        return None


def decodes_as_address(address: str) -> tuple[bool, str]:
    """(is_decodable, why) for any address on any chain this repository pays out to.

    Returns the REASON in both cases, because the reason is the whole value of the check. "not
    valid base58check" and "valid bech32 but on a chain with no bech32" send an operator to two
    different fixes, and a bare False sends them nowhere.

    It NEVER falls back to "well, it looks like an address". The 2026-09-27 sweep found 25
    address-shaped literals in this tree that decode as nothing at all --
    `tb1qexampleparticipantaddress000...`, `bcrt1qsomebodyelse000...`,
    `S8kKq2VrZ4mQvYtN6dWxJ3hLpB7cFgTnEu` -- every one sitting where a reader would take it for
    a working example.
    """
    if not isinstance(address, str) or not address.strip():
        return False, f"not a non-empty string: {address!r}"
    raw = address.strip()
    for result in (_bech32_result(raw), _base58_result(raw)):
        if result is not None:
            return result
    return False, "decodes as neither bech32, base58check nor XRP base58check"


def is_valid_address(address: str) -> bool:
    """True only when the string decodes under one of the three encodings.

    The boolean for a caller that is about to pay out. False on anything undecodable, which
    includes every placeholder -- an address that cannot be decoded cannot be paid, so this is
    the check that turns a silent burn into a refusal.
    """
    return decodes_as_address(address)[0]


def bech32_hrp(address: str) -> str | None:
    """The bech32 hrp this string CLAIMS, or None when it claims none.

    "Claims" rather than "has": the separator is what makes a string a bech32 candidate, so
    `tb1...` claims `tb` whether or not its checksum holds. That distinction is the point --
    a caller must be able to tell "this is a Bitcoin address with a broken checksum" from
    "this is not a Bitcoin address at all", and those are two different messages to an
    operator staring at a failed payout.

    MERGED INTO _claimed_hrp() ON 2026-09-28, which is where the scan now lives -- this used
    to say the sort "lives in one expression here that both could eventually share", and
    "eventually" is how three copies of one loop came to sit in this file. _bech32_result()
    and _bech32_network() call the same function this does, so there is one scan and not
    three. See that function for what the ordering does and does not buy, measured.

    Added 2026-09-27 for modules/address_authority.py, which needs the hrp ITSELF rather
    than the network it maps to: `ltc1...` and `bc1...` are both mainnet, and handing the
    first one to a Bitcoin payout burns the money exactly as an undecodable string would.
    """
    return _claimed_hrp(address)


def base58check_payload(address: str) -> bytes | None:
    """The versioned payload under BITCOIN's alphabet, or None when it does not decode.

    A thin public name over _decode_or_none(), which exists so the authority module can read
    a VERSION BYTE without owning a second copy of the try/except that decides whether a
    string is base58check at all (rule 8). Deliberately Bitcoin's alphabet only: XRP's is a
    different encoding answered by chains/xrp_address.py, and a function that silently tried
    both would hand back a payload whose version byte means nothing to the caller.

    No length check here. The caller wants the bytes and each one has its own opinion about
    how many there should be -- address_network() above wants 21 and says so in its own
    words, which is what lets it report "decoded to 23 bytes" instead of a bare refusal.
    """
    if not isinstance(address, str):
        return None
    return _decode_or_none(address.strip(), BITCOIN_BASE58_ALPHABET)
