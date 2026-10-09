"""A chain address from a secp256k1 PUBLIC KEY, for every Bitcoin-derived chain here.

Role: module (pure functions; no network, no wallet, no key material)
Reads: modules.utils.hash160, modules.address_network's version bytes
Writes: nothing
Can move funds: no. It holds no secret and signs nothing. It turns a PUBLIC key
      into the address that key controls, which is public information.
Live-safe: yes.

WHY THIS EXISTS, AND IT IS NOT (YET) ABOUT ICP.

The pieces were already here and nothing composed them: modules/utils.hash160()
does RIPEMD160(SHA256), modules/address_network.py holds the version bytes, and
base58 is a dependency. What was missing is the one line that says a P2PKH
address IS base58check(version || hash160(pubkey)) -- so every caller that wanted
it either did not exist or would have written its own.

The immediate caller is a threshold-signature custody design: ICP's
`ecdsa_public_key` hands back a 33-byte compressed secp256k1 key and no address,
and whoever holds that key controls funds on BTC, LTC and GRC alike -- same key,
same hash160, four different version bytes. But nothing in this file knows about
ICP, and it is useful without it: it is also how you check that an address a
daemon handed you is the one its pubkey implies.

MEASURED AGAINST A LIVE DAEMON AND AGAINST THE CHAIN, 2026-10-05.

`gridcoinresearchd -testnet validateaddress` on the operator's host reported both
halves of one pair: pubkey 037b3a84..d1fdc9 and address mg3gJAmh..QG2Ap. This
file's derivation reproduces that address exactly. Independently, the hash160 it
computes --

    05cf8a542bd038401e9d692712bb1d8082c62f56

-- is byte-for-byte the OP_HASH160 in the scriptPubKey of all four GRC payouts
the terminal broadcast that day (txids e090712c, b278a236, 35f565e2, c82f9e37).
So the derivation agrees with the wallet AND with what the chain recorded, which
are two different authorities and neither is this code.

THE FORWARD TABLE IS NOT DERIVABLE FROM THE REVERSE ONE, which is why it exists
separately and why that is not rule 8's duplication. address_network.P2PKH_VERSIONS
maps version -> NETWORK NAME and is one-to-many in the direction this file needs:
0x6F is testnet for BTC, LTC and GRC alike, so it cannot answer "which byte does
GRC testnet use". Encoding needs (asset, network) -> version. The two tables are
held consistent by a test rather than by a comment: every entry here is looked up
in P2PKH_VERSIONS and must report the network it claims.
"""

from __future__ import annotations

import base58
from modules.address_network import MAINNET, P2PKH_VERSIONS, TESTNET
from modules.utils import hash160

#: (asset, network) -> P2PKH version byte. See the module docstring for why this
#: is not generated from address_network.P2PKH_VERSIONS: that table is keyed the
#: other way and is one-to-many in this direction.
#:
#: GRC MAINNET IS 0x3E AND IT IS THE ONE WORTH CHECKING TWICE. Read from
#: Gridcoin's own chainparams.cpp 2026-10-05 (src/chainparams.cpp:230,
#: base58Prefixes[PUBKEY_ADDRESS] = 62), not recalled -- 62 decimal is 0x3E, and
#: it is the only version byte here that differs from Bitcoin's family.
P2PKH_VERSION_FOR: dict[tuple[str, str], int] = {
    ("BTC", MAINNET): 0x00,
    ("BTC", TESTNET): 0x6F,
    ("LTC", MAINNET): 0x30,
    ("LTC", TESTNET): 0x6F,
    ("GRC", MAINNET): 0x3E,
    ("GRC", TESTNET): 0x6F,
}

#: A compressed secp256k1 point: 33 bytes, leading 0x02 or 0x03.
COMPRESSED_LENGTH = 33
COMPRESSED_PREFIXES = (0x02, 0x03)


class PublicKeyRefused(ValueError):
    """The key is not a compressed secp256k1 point, so no address is derived.

    ITS OWN TYPE because the caller's remedy differs from every other failure
    here: an uncompressed or truncated key is a bug in whatever produced it, and
    deriving an address from it anyway would produce a VALID-LOOKING address that
    nobody holds the key for. That is the 82.65 tGRC failure mode this repository
    already paid for once, reached by a different road.
    """


def address_from_public_key(public_key: object, asset: str, network: str) -> str:
    """The P2PKH address `public_key` controls on `asset`/`network`.

    UNCOMPRESSED KEYS ARE REFUSED RATHER THAN ACCEPTED, and the refusal is the
    point. A 65-byte uncompressed key hashes to a DIFFERENT hash160 than its own
    33-byte compressed form, so the same key yields two addresses and only one of
    them is where the funds are. Accepting both would make this function answer a
    question the caller did not ask. ICP's ecdsa_public_key returns the compressed
    form, and so does every `validateaddress` reply this tree has read.
    """
    # `public_key: object` AND NOT `public_key: bytes`: this function's first act is to refuse a wrong
    # type by name, so the narrow annotation was a claim the guard below contradicts.
    # The reasoning, and what the trade costs, is written once at
    # chains/xrp_rpc_map.call_for() -- which carries the precedent in its own signature.
    if not isinstance(public_key, (bytes, bytearray)):
        raise PublicKeyRefused(
            f"public_key must be bytes, got {type(public_key).__name__}. Nothing was derived."
        )
    if len(public_key) != COMPRESSED_LENGTH or public_key[0] not in COMPRESSED_PREFIXES:
        raise PublicKeyRefused(
            f"public_key is {len(public_key)} bytes starting 0x{public_key[0]:02x} if non-empty; a "
            f"compressed secp256k1 point is {COMPRESSED_LENGTH} bytes starting 0x02 or 0x03. An "
            f"uncompressed key hashes to a different hash160, so deriving from it would produce a "
            f"well-formed address nobody holds the key for. Nothing was derived."
        )
    version = P2PKH_VERSION_FOR.get((asset.upper(), network))
    if version is None:
        known = ", ".join(f"{a}/{n}" for a, n in sorted(P2PKH_VERSION_FOR))
        raise PublicKeyRefused(
            f"no P2PKH version byte for {asset}/{network}. Known: {known}. Nothing was derived."
        )
    return base58.b58encode_check(bytes([version]) + hash160(bytes(public_key))).decode()


def network_of_version(version: int) -> str | None:
    """What address_network says `version` means, or None. The reverse direction.

    Here so that a reader comparing the two tables finds them in one file, and so
    the consistency test has one import rather than two.
    """
    return P2PKH_VERSIONS.get(version)
