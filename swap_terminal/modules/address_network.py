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

This still changes no fund-path behavior. `parse_and_reencode_as_testnet_p2pkh()` continues to
ignore the hrp, and starting to reject addresses it accepts today is a live-posture change that
belongs to the operator (rule 16). An address this module cannot decode returns UNKNOWN with
the reason, never a network.
"""

from __future__ import annotations

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
P2SH_VERSIONS: dict[int, str] = {
    0x05: MAINNET,   # BTC mainnet
    0x32: MAINNET,   # LTC mainnet (also 0x05 historically)
    0x55: MAINNET,   # GRC mainnet
    0xC4: TESTNET,   # BTC, LTC and GRC testnet/regtest
}

# Re-exported so the two older copies of this byte can derive from one place (rule 8).
TESTNET_P2PKH_VERSION: bytes = b"\x6f"
TESTNET_P2SH_VERSION: bytes = b"\xc4"

BASE58_VERSIONED_HASH160_LEN = 21

# Human-readable parts this repository can pay out to. A bech32 string whose hrp is not here
# is UNKNOWN rather than accepted on the strength of its checksum: a valid address on a chain
# we cannot reach is still an address the money never comes back from.
BECH32_HRPS = {"bc": MAINNET, "tb": TESTNET, "bcrt": TESTNET, "ltc": MAINNET, "tltc": TESTNET}


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


def _bech32_network(raw: str) -> tuple[str, str] | None:
    """The network an hrp names, or None when this string does not claim to be bech32.

    Extracted for ruff's return-statement ceiling, which rule 12 says to answer by extracting
    rather than by raising the ceiling. The hrp NAMES the network as definitively as a version
    byte does; BECH32_HRPS is that vocabulary, shared with the validity checker below so one
    module does not know it twice.
    """
    lowered = raw.lower()
    for hrp in sorted(BECH32_HRPS, key=len, reverse=True):
        if lowered.startswith(hrp + "1"):
            _decoded_hrp, data = bech32.bech32_decode(raw)
            if data is None:
                return UNKNOWN, f"claims hrp {hrp} but is not valid bech32"
            return BECH32_HRPS[hrp], f"bech32 hrp {hrp!r} is {BECH32_HRPS[hrp]}"
    return None


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

# Human-readable parts this repository can pay out to. A bech32 string whose hrp is not here
# is refused rather than accepted on the strength of its checksum: a valid bech32 address on
# a chain we cannot reach is still an address the money never comes back from.


def _bech32_result(raw: str) -> tuple[bool, str] | None:
    """The bech32 verdict, or None when this string is not claiming to be bech32.

    Extracted because decodes_as_address() crossed ruff's return-statement ceiling, and rule 12
    says a complexity finding is extracted rather than suppressed. It earns its own name
    anyway: "which hrp, and is that a chain we can reach" is one question, and the answer has
    two failure modes -- a bad checksum, and a well-formed address on a chain with no bech32 --
    that a caller must be able to tell apart.
    """
    lowered = raw.lower()
    if lowered.startswith(("grc1", "tgrc1")):
        return False, "Gridcoin has no bech32 format at all -- it is base58 only"
    for hrp in sorted(BECH32_HRPS, key=len, reverse=True):
        if lowered.startswith(hrp + "1"):
            decoded_hrp, data = bech32.bech32_decode(raw)
            if data is None:
                return False, f"starts with {hrp}1 but is not valid bech32 (checksum or charset)"
            return True, f"valid bech32, hrp={decoded_hrp} ({BECH32_HRPS[hrp]})"
    return None


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
