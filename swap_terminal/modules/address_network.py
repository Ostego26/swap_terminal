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

WHAT THIS DELIBERATELY DOES NOT DO. It does not judge bech32 (`tb1`/`bc1`/`tltc1`) addresses,
because those carry their network in a human-readable prefix rather than a version byte and
`parse_and_reencode_as_testnet_p2pkh()` already notes that it ignores that prefix -- changing
that is a fund-path behavior change and belongs to the operator (rule 16). An address this
module cannot decode returns UNKNOWN with the reason, never a network.
"""

from __future__ import annotations

import base58

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
    try:
        decoded = base58.b58decode_check(address.strip())
    except Exception as error:  # noqa: BLE001 -- checked: this is a FORMAT PROBE and the caller can tell the outcomes apart, which is the condition rule 12 puts on a broad catch. base58 raises several unrelated types for a bad checksum, a bad alphabet and a short payload, and every one of them means the same thing here: this string is not a base58check address. The reason is returned in `why` rather than discarded, so nothing silently becomes UNKNOWN without saying why.
        return UNKNOWN, f"not decodable as base58check ({type(error).__name__})"
    if len(decoded) != BASE58_VERSIONED_HASH160_LEN:
        return UNKNOWN, f"decoded to {len(decoded)} bytes, not {BASE58_VERSIONED_HASH160_LEN}"
    version = decoded[0]
    for table, kind in ((P2PKH_VERSIONS, "P2PKH"), (P2SH_VERSIONS, "P2SH")):
        if version in table:
            return table[version], f"version byte {version:#04x} is a {table[version]} {kind} version"
    return UNKNOWN, f"version byte {version:#04x} is in no table this module knows"


def is_testnet_address(address: str) -> bool:
    """True only when the version byte SAYS testnet. UNKNOWN is not testnet.

    The boolean every one of the six replaced checks wanted. It is False for an undecodable
    address on purpose: "I could not tell" and "it is testnet" must not be the same value,
    which is the general form of the thing that broke here (rule 2's "I could not find a
    caller is not there is no caller").
    """
    return address_network(address)[0] == TESTNET
