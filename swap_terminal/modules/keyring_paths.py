"""The one place that says which curve an asset uses and which path derives its key.

Role: module (pure functions and one table; no network, no wallet, no key material)
Reads: modules.address_network's MAINNET/TESTNET names, and nothing else
Writes: nothing
Can move funds: no. It holds no secret, signs nothing, and returns byte strings.
      But it DECIDES which key a given asset's funds live under, and a path that
      changes after an address has been funded is an address nobody can spend
      from -- see `SCHEME_VERSION` below, which exists to make that impossible
      to do by accident.
Mainnet-safe: yes to import and to call. Every function here is total and offline.

=============================================================================
WHY THIS FILE EXISTS: ONE VOCABULARY, DERIVED IN ONE PLACE (CLAUDE.md RULE 11)
=============================================================================

icp/threshold_custody holds threshold keys and reports their public halves. To
ask it for a key you must name two things: a CURVE and a DERIVATION PATH. Until
this file, neither had an owner -- `icp_custody_addresses.py` passed the literal
`(vec {})`, the canister's root key, which is the one path that cannot express
"this asset's key" at all.

Rule 11's test is "did every consumer follow automatically?" and the answer has
to be yes here for a harder reason than usual. A derivation path is not a
configuration detail that can be corrected later: the path IS the key, the key is
the address, and the address is where funds are. Getting it wrong once and fixing
it afterwards does not move the funds, it abandons them.

=============================================================================
WHY PYTHON OWNS THE PATH AND THE CANISTER DOES NOT
=============================================================================

The canister takes the path as an ARGUMENT and never constructs one. That is
deliberate, and it is the same split icp/threshold_custody/src/lib.rs already
argues for addresses: the canister answers the question only it can answer (what
is the public key for this curve and path), and the Python side owns the question
it can verify offline (which path, and what address does the result imply).

The alternative -- a table of asset paths inside the canister -- would be rule
8's "two copies of one rule is a bug with a delay on it" in its worst form. The
copies would agree the day they were written; the day they drifted, Python would
derive an address from the key at path A while the canister signed with the key
at path B, and the only symptom would be a signature that does not validate
against the address the funds are at. Keeping the table in one language means
there is nothing to drift.

So: nothing in this file has a counterpart in the Rust. The only thing that must
agree across the two languages is the CURVE NAME, and
tests/test_keyring_curve_vocabulary.py reads both sources and asserts the sets
are equal.

=============================================================================
THE PATH SCHEME, AND WHAT EACH ELEMENT BUYS
=============================================================================

    [ b"swap_terminal", SCHEME_VERSION, network, asset, index ]

ICP derivation paths are `Vec<Vec<u8>>` -- a list of arbitrary byte strings, not
BIP32 integers -- so the elements are chosen for what they separate rather than
to match anybody's standard.

  b"swap_terminal"  Domain separation from any other canister that might ever be
                    installed under the same master key. The master key is named
                    per ICP network (`dfx_test_key`, `test_key_1`, `key_1`), not
                    per canister, so without a project prefix two unrelated
                    projects asking for the same path get the same key.

  SCHEME_VERSION    So a future change to this scheme cannot MOVE an existing
                    address. Bump it and every asset gets new keys at new
                    addresses, leaving the old ones derivable forever. Editing
                    any element below without bumping this is the one change in
                    this file that silently orphans funds, which is why
                    tests/test_keyring_paths.py pins the exact bytes of every
                    path for every asset: changing one fails the suite.

  network           mainnet and testnet get DIFFERENT keys. Without this element
                    they would share one, because the ICP master key does not
                    know what a Bitcoin testnet is: the same path would give one
                    public key, one hash160, and the testnet and mainnet
                    addresses would be that same hash under two version bytes
                    (which is exactly what modules/pubkey_address.py observes).
                    That is not a key disclosure, but it does mean every
                    rehearsal signature on testnet is made by the key holding
                    mainnet funds, and there is no reason to accept that when
                    one path element avoids it.

  asset             BTC, LTC and GRC are all secp256k1 and would otherwise share
                    a key, giving one hash160 and therefore three addresses that
                    are publicly linkable and that fall together. Per-asset
                    paths cost nothing and isolate them.

  index             Four bytes, big-endian, so per-swap deposit addresses can
                    arrive later WITHOUT moving any address that exists now.
                    Reserving the element up front is the whole point: adding it
                    later would be a scheme change, and a scheme change is a
                    SCHEME_VERSION bump, and a bump moves everything.

=============================================================================
REGTEST SHARES TESTNET'S PATH, AND THAT IS A DECISION RATHER THAN AN OMISSION
=============================================================================

`network` accepts only MAINNET and TESTNET, the two names
modules/address_network.py defines. There is no "regtest" and no "unknown".

modules/address_network.py gives version byte 0x6F ONE entry for BTC, LTC and GRC
testnet and regtest alike, with the reasoning that an address decoded from it is
"some testnet", which is the question every caller is actually asking. Inventing
a third network name here would be a second vocabulary in a second place, which
is the rule this file exists to enforce.

The consequence, stated rather than left to be discovered: a regtest key and a
testnet key under this scheme ARE THE SAME KEY. That is harmless because regtest
coins have no value and regtest chains are disposable, but a reader who assumes
otherwise would be wrong, so it is written here.

UNKNOWN is refused rather than mapped. A path built from an unknown network is a
path nobody can reproduce deliberately, and the funds at its address would be
reachable only by guessing which network the caller meant.
"""

from __future__ import annotations

from dataclasses import dataclass

from .address_network import MAINNET, TESTNET

#: The scheme's version, as the second path element. See the header: bumping this
#: moves EVERY address this scheme derives, and editing any other element without
#: bumping it orphans the funds at the old ones.
#:
#: Bytes rather than an integer because every element of an ICP derivation path is
#: a byte string, and spelling it `b"v1"` at the one site that uses it is clearer
#: than an integer that gets encoded somewhere out of sight.
SCHEME_VERSION = b"v1"

#: The project prefix. Domain separation from any other canister installed under
#: the same ICP master key.
SCHEME_PREFIX = b"swap_terminal"

# =============================================================================
# THE CURVES
# =============================================================================
#
# THREE NAMES, TWO MANAGEMENT-CANISTER CALLS, AND THE SPLIT IS NOT WHERE A READER
# EXPECTS IT. `ecdsa_public_key` serves secp256k1 ECDSA. `schnorr_public_key`
# serves BOTH ed25519 AND BIP-340 secp256k1, selected by its `algorithm` field.
# So SECP256K1_ECDSA and BIP340 are the same curve under different signature
# schemes, reached through different calls, and they produce DIFFERENT KEYS AND
# DIFFERENT ADDRESSES. Treating them as one value because the curve name matches
# would be the quietest possible way to derive an address nobody holds.
#
# The strings are the vocabulary the Rust side maps to its own enum, and
# tests/test_keyring_curve_vocabulary.py asserts the two sets are equal.

#: secp256k1 with ECDSA. The Bitcoin-derived family's signature scheme.
SECP256K1_ECDSA = "secp256k1_ecdsa"

#: ed25519 with Schnorr. Solana's and XRP's modern key type.
ED25519 = "ed25519"

#: secp256k1 with BIP-340 Schnorr. Bitcoin taproot. NOT USED BY ANY ASSET BELOW
#: and present anyway, because the canister supports it and a reader comparing
#: the two vocabularies must find the same three names in both places. An asset
#: that wants taproot adds a row; it does not add a curve.
BIP340 = "bip340secp256k1"

#: Every curve this keyring can ask for. The canister refuses anything else.
CURVES = frozenset({SECP256K1_ECDSA, ED25519, BIP340})

#: The expected PUBLIC KEY LENGTH per curve, in bytes, and what the leading byte
#: may be. Checked by the canister one hop earlier than here, and repeated here
#: because this is the table a Python caller validates against before deriving an
#: address -- the same belt-and-braces modules/pubkey_address.py already applies
#: to compressed secp256k1 keys, for the same measured reason (an uncompressed key
#: hashes to a different hash160, so it yields a well-formed address nobody holds).
#:
#: An empty prefix tuple means "any leading byte is valid", which is true of
#: ed25519 and BIP-340 x-only keys: both are 32 bytes of coordinate with no
#: parity or format prefix at all.
PUBLIC_KEY_SHAPE: dict[str, tuple[int, tuple[int, ...]]] = {
    SECP256K1_ECDSA: (33, (0x02, 0x03)),
    ED25519: (32, ()),
    BIP340: (32, ()),
}

# =============================================================================
# THE ASSET TABLE
# =============================================================================
#
# Rule 11's first test: "Is the new asset in the single shared table?" This is
# that table for the keyring. An asset absent from it has no keyring path, which
# is a REFUSAL rather than a default -- see `curve_for`.
#
# ICP IS DELIBERATELY ABSENT, and it is the asset a reader will look for first.
# A canister's ICP identity is its own principal; there is no threshold key to
# derive and no address to compute, so a row here would describe a derivation
# that does not exist. chains/icp.py holds that path and says `can_spend = False`
# because it calls with `--identity anonymous`.
#
# XMR IS ALSO ABSENT, and for a different reason worth writing down: Monero keys
# are ed25519, so the CURVE is available, but a Monero address is a pair of keys
# (view and spend) with its own encoding, and the subaddress scheme is not a
# derivation path this table can express. Monero custody through a threshold key
# is not a row, it is an increment.

#: asset -> the curve its keys live on.
CURVE_FOR_ASSET: dict[str, str] = {
    # The Bitcoin-derived three. One curve, one signature scheme, three separate
    # paths -- see the header on why they do not share a key.
    "BTC": SECP256K1_ECDSA,
    "LTC": SECP256K1_ECDSA,
    "GRC": SECP256K1_ECDSA,
    # Solana: the 32-byte ed25519 public key IS the account address, base58 of the
    # raw bytes with no hashing and no version byte.
    "SOL": ED25519,
    # XRP SUPPORTS BOTH secp256k1 AND ed25519, and this picks ed25519 rather than
    # inheriting it. An XRP ed25519 public key is published as 33 bytes with a
    # 0xED prefix in front of the 32 raw bytes, so whatever derives an XRP address
    # from this key must ADD that prefix -- the canister returns the bare 32, as
    # PUBLIC_KEY_SHAPE says. A reader who forgets the prefix gets a well-formed
    # address for a different account, which is this repository's most expensive
    # recurring failure shape.
    "XRP": ED25519,
}

#: The networks a path may name. Exactly the two modules/address_network.py
#: defines; UNKNOWN and regtest are refused, for the reasons in the header.
NETWORKS = frozenset({MAINNET, TESTNET})

#: The index element's width. Four bytes big-endian, so the first 4,294,967,296
#: addresses per (network, asset) are expressible without a scheme change.
INDEX_BYTES = 4


class KeyringRefused(ValueError):
    """No path or curve was produced, and the caller must not substitute one.

    ITS OWN TYPE for the same reason modules/pubkey_address.PublicKeyRefused has
    one: the remedy is never "retry with a default". Every refusal here means the
    caller named an asset, network or index this scheme cannot express, and the
    cost of guessing on its behalf is funds at an address derived from a path
    nobody chose.
    """


def curve_for(asset: str) -> str:
    """The curve `asset`'s keyring keys live on.

    REFUSES an unknown asset rather than defaulting to secp256k1. Three of the
    five assets in the table are secp256k1, so a default would be right often
    enough to look correct and would silently give SOL or XRP a secp256k1 key --
    a key of the wrong type, whose public half is the wrong length, from which
    any address derived is an address nobody holds.
    """
    curve = CURVE_FOR_ASSET.get(asset.upper())
    if curve is None:
        known = ", ".join(sorted(CURVE_FOR_ASSET))
        raise KeyringRefused(
            f"{asset!r} has no keyring curve. Known assets: {known}. ICP and XMR are "
            f"deliberately absent -- see the table's comment. Nothing was derived."
        )
    return curve


def derivation_path(asset: str, network: str, index: int = 0) -> list[bytes]:
    """The derivation path for `asset` on `network` at `index`.

    THE RETURN IS THE KEY, so every argument is validated and none is coerced.
    An `index` quietly clamped, an asset quietly upper-cased from something that
    was not a ticker, or a network quietly defaulted, each produces a path that
    is well-formed and is not the one the caller meant -- and the funds end up at
    its address either way.

    `asset` IS upper-cased, deliberately and narrowly: the table's keys are
    upper-case tickers and `curve_for` already accepts either case, so refusing
    "btc" here while accepting it there would be two answers to one question.
    The path element is always the upper-case form, which is what makes the path
    reproducible from a lower-case caller.
    """
    curve_for(asset)  # Refuses an unknown asset before anything else is computed.

    if network not in NETWORKS:
        known = ", ".join(sorted(NETWORKS))
        raise KeyringRefused(
            f"{network!r} is not a network this scheme names. Known: {known}. regtest "
            f"shares testnet's path on purpose and 'unknown' is refused -- see the "
            f"module header. Nothing was derived."
        )

    # A bool is an int in Python, and `derivation_path("BTC", MAINNET, True)` would
    # otherwise silently mean index 1. Refused by type rather than by value,
    # because the caller who passed a flag meant something this cannot express.
    if isinstance(index, bool) or not isinstance(index, int):
        raise KeyringRefused(
            f"index must be an int, got {type(index).__name__}. Nothing was derived."
        )
    limit = 1 << (INDEX_BYTES * 8)
    if not 0 <= index < limit:
        raise KeyringRefused(
            f"index {index} is outside 0..{limit - 1}, which is what {INDEX_BYTES} "
            f"big-endian bytes can express. A wider index is a scheme change and a "
            f"SCHEME_VERSION bump, not a wider integer. Nothing was derived."
        )

    return [
        SCHEME_PREFIX,
        SCHEME_VERSION,
        network.encode("ascii"),
        asset.upper().encode("ascii"),
        index.to_bytes(INDEX_BYTES, "big"),
    ]


def public_key_shape(curve: str) -> tuple[int, tuple[int, ...]]:
    """The (length, allowed leading bytes) a public key on `curve` must have.

    Refuses an unknown curve for the same reason `curve_for` refuses an unknown
    asset: a shape guess is a validation that passes the wrong key.
    """
    shape = PUBLIC_KEY_SHAPE.get(curve)
    if shape is None:
        known = ", ".join(sorted(PUBLIC_KEY_SHAPE))
        raise KeyringRefused(f"{curve!r} is not a known curve. Known: {known}.")
    return shape


def refuse_wrong_shape(public_key: object, curve: str) -> bytes:
    """Return `public_key` as bytes if it has `curve`'s shape, else refuse.

    THE CHECK THE CANISTER ALREADY MAKES, MADE AGAIN ON THIS SIDE, and the
    duplication is deliberate rather than rule 8's defect: these are two
    different trust boundaries. The canister checks what the management canister
    handed IT; this checks what arrived over a dfx transport, through a text
    candid reply, through a regex. Either hop can produce a well-formed-looking
    key of the wrong length, and the address derived from one is unspendable.
    The canister's copy carries a comment naming this one.
    """
    length, prefixes = public_key_shape(curve)
    if not isinstance(public_key, (bytes, bytearray)):
        raise KeyringRefused(
            f"public_key must be bytes, got {type(public_key).__name__}. Nothing was derived."
        )
    raw = bytes(public_key)
    if len(raw) != length:
        raise KeyringRefused(
            f"a {curve} public key is {length} bytes; got {len(raw)}. Nothing was derived."
        )
    if prefixes and raw[0] not in prefixes:
        allowed = " or ".join(f"0x{p:02x}" for p in prefixes)
        raise KeyringRefused(
            f"a {curve} public key starts {allowed}; got 0x{raw[0]:02x}. Nothing was derived."
        )
    return raw


#: The printable-ASCII window `describe_path` renders as itself. Named rather than
#: spelled inline because 0x20 and 0x7F as bare literals in a comparison read as
#: arbitrary, and the thing they decide -- whether an operator sees `testnet` or
#: `0x746573746e6574` -- is worth a name.
PRINTABLE_LOW = 0x20
PRINTABLE_HIGH = 0x7F


@dataclass(frozen=True)
class KeyRequest:
    """One (curve, derivation_path) pair: everything the canister needs to answer.

    A TYPE RATHER THAN TWO PARAMETERS, and it mirrors the canister's own
    `PublicKeyRequest` record deliberately. Both halves decide which key comes
    back, so neither is meaningful alone -- and passing them separately down a
    call chain is how one gets updated and the other does not.

    FROZEN because a request that mutates after it has been rendered to candid is
    a request whose text no longer describes what was asked for.

    The curve is validated at CONSTRUCTION, so an invalid one cannot be carried
    around and discovered at the call. `derivation_path` is a tuple for the same
    reason the class is frozen: a list would let a caller append to a request
    another caller is holding.
    """

    curve: str
    derivation_path: tuple[bytes, ...] = ()

    def __post_init__(self) -> None:
        if self.curve not in CURVES:
            known = ", ".join(sorted(CURVES))
            raise KeyringRefused(
                f"{self.curve!r} is not a curve this keyring names. Known: {known}. "
                f"Nothing was requested."
            )
        for element in self.derivation_path:
            if not isinstance(element, (bytes, bytearray)):
                raise KeyringRefused(
                    f"every derivation path element must be bytes; got "
                    f"{type(element).__name__}. Nothing was requested."
                )

    def describe(self) -> str:
        """One readable line for a log or an operator-facing table (rule 14)."""
        return f"{self.curve} {describe_path(list(self.derivation_path))}"


#: The canister's ROOT key on secp256k1 ECDSA -- an empty derivation path.
#:
#: NAMED because it is the default `icp_custody_addresses.py` sends, and it is the
#: same key the operator read by hand before the per-asset scheme existed. A
#: literal `KeyRequest(SECP256K1_ECDSA, ())` at each call site would be three
#: chances to write `()` as something else.
ROOT_SECP256K1 = KeyRequest(SECP256K1_ECDSA, ())


def request_for(asset: str, network: str, index: int = 0) -> KeyRequest:
    """The [`KeyRequest`] for `asset` on `network` at `index`.

    THE ONE FUNCTION A CALLER SHOULD USE. `curve_for` and `derivation_path` are
    the two halves and are public because the tests assert on them separately,
    but a caller that composes them by hand is a caller that can pair one asset's
    curve with another asset's path -- which asks for a key that exists, is the
    wrong one, and yields an address nobody holds.
    """
    return KeyRequest(curve_for(asset), tuple(derivation_path(asset, network, index)))


def describe_path(path: list[bytes]) -> str:
    """A path as one readable line, for a log or an operator-facing table.

    Rule 14: a derivation path printed as raw bytes is a value an operator cannot
    check against what they asked for. Printed elements are ASCII where they are
    ASCII and hex where they are not, so the index reads as hex and the rest
    reads as itself.
    """
    parts = []
    for element in path:
        if element and all(PRINTABLE_LOW <= b < PRINTABLE_HIGH for b in element):
            parts.append(element.decode("ascii"))
        else:
            parts.append("0x" + element.hex())
    return "/".join(parts)
