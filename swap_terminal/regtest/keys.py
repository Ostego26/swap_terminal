"""Throwaway secp256k1 keys for a regtest run, generated in this process.

Role: function level (key generation and address encoding -- the decisions)
Reads: os.urandom
Writes: nothing. No key produced here is ever written to disk, exported to a
        wallet file, or printed.
Can move funds: no directly. It produces the keys that regtest.txbuild signs
        with, and those spends move regtest coins.
Mainnet-safe: yes in the sense that it touches nothing -- but every address it
        produces carries the TESTNET version byte 0x6F, deliberately, because
        modules/atomic_htlc_scripts.py re-encodes every address it is given to
        that same version. An address from this module and an address from
        that module round-trip to the identical hash160, which is what makes
        the harness able to sign for the branch it built.

WHY THE HARNESS GENERATES KEYS INSTEAD OF ASKING THE WALLET FOR ONE.

The obvious route is `getnewaddress` followed by `dumpprivkey`. CLAUDE.md's
chain-safety rules forbid it outright: "Never move, copy, or read back a key.
Not .env, not a keypair JSON, not a WIF, not wallet.dat." A regtest wallet is
not worth anything, but the habit is what leaked a live GRIDCOIN_RPC_PASSWORD
into this repository's history (rule 2), and a harness that teaches the operator
to type `dumpprivkey` is teaching the wrong reflex.

It also happens to be the only route that works on both daemons. `dumpprivkey`
is a legacy-wallet RPC; Bitcoin Core 28.1 creates descriptor wallets by
default and refuses it there. Generating in-process sidesteps a version
divergence entirely rather than branching on a guess about it.

THE PRIVATE KEY NEVER LEAVES THIS PROCESS EXCEPT AS A WIF HANDED TO THE REAL
CLIENT, and as of 2026-09-25 it does not go through the environment to get
there.

It used to. `BTCClient.import_redeem_script_and_key()` read `BTC_HTLC_PRIVKEY`
and called `importprivkey`, so that the WALLET could sign the redeem -- which
never worked, because `signrawtransactionwithwallet` cannot build a scriptSig
for an OP_IF script. That whole path is gone: the clients sign with the
`participant_privkey` argument they were already being passed, in-process, and
nothing in the tree reads `BTC_HTLC_PRIVKEY` any more. The harness no longer
sets it.

What remains is the same guarantee, one step shorter. The key handed to
`redeem_contract()` is generated in this process seconds earlier, exists
nowhere else, controls nothing but regtest coins the harness itself mined, and
is never printed, never logged and never written to a file.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass

import base58
from ecdsa import SECP256k1, SigningKey
from ecdsa.util import sigencode_der_canonize
from modules import address_network as _address_network

# The same testnet version bytes modules/atomic_htlc_scripts.py hardcodes.
# They are spelled again here rather than imported because importing them
# would make this module's correctness depend on that module not changing
# them -- and if they ever diverge, the round-trip assertion in
# regtest.steps (address in == address out) is what catches it, loudly,
# instead of a silent mismatch between a key and the hash160 in a script.
# DERIVED from modules/address_network.py, which owns the version-byte vocabulary (rule 8).
# This file and modules/atomic_htlc_scripts.py both declared this byte independently; a third
# caller could have disagreed with either and nothing would have failed until an address went
# out on the wrong chain.
TESTNET_P2PKH_VERSION = _address_network.TESTNET_P2PKH_VERSION
# WIF version byte for testnet and regtest on both Bitcoin and Litecoin
# (SECRET_KEY = 239). The trailing 0x01 marks a COMPRESSED public key, which
# must match the pubkey actually pushed in the scriptSig -- a WIF that says
# uncompressed and a scriptSig that pushes 33 bytes hash to two different
# addresses, and the mismatch surfaces only as a failed script.
TESTNET_WIF_VERSION = b"\xef"
WIF_COMPRESSED_SUFFIX = b"\x01"

SECP256K1_ORDER = SECP256k1.order
PRIVKEY_BYTES = 32


def hash160(data: bytes) -> bytes:
    """RIPEMD160(SHA256(data)).

    modules/utils.py has a `hash160` too, and this is the other copy rule 8
    asks to be told about: that one logs at DEBUG through the application's
    logger and is used by the script builder; this one is a pure function used
    while assembling a scriptPubKey. They are byte-for-byte equivalent, and the
    reason this harness does not simply import that one is that a debug log
    line per hash would bury the progress output this harness exists to print.
    If either is ever changed, change both.
    """
    return hashlib.new("ripemd160", hashlib.sha256(data).digest()).digest()


def double_sha256(data: bytes) -> bytes:
    """SHA256(SHA256(data)) -- the hash a legacy sighash and a txid both use."""
    return hashlib.sha256(hashlib.sha256(data).digest()).digest()


@dataclass(frozen=True)
class RegtestKey:
    """One throwaway keypair and the four forms of it the harness needs.

    `wif` is included because every client's `redeem_contract()` takes the
    participant's key as a WIF argument and signs with it directly. (It used to
    be needed for a second reason -- the BTC client read one out of
    BTC_HTLC_PRIVKEY to `importprivkey` into the wallet -- and that path was
    deleted on 2026-09-25 along with the wallet-signing it existed for.)

    It must never be printed; there is no __str__ override that would make that
    safe, so the rule is at the call sites.
    """

    private_key: bytes
    public_key: bytes
    address: str

    @property
    def hash160(self) -> bytes:
        return hash160(self.public_key)

    @property
    def wif(self) -> str:
        payload = TESTNET_WIF_VERSION + self.private_key + WIF_COMPRESSED_SUFFIX
        return base58.b58encode_check(payload).decode()

    @property
    def p2pkh_script(self) -> bytes:
        """OP_DUP OP_HASH160 <20> OP_EQUALVERIFY OP_CHECKSIG.

        Built from the hash160 rather than from the address string, so it is
        independent of which base58 version byte a given chain prints. That
        matters: Litecoin and Bitcoin agree on 0x6F for testnet P2PKH but
        disagree on the P2SH byte, and an assertion written against a rendered
        address would fail on one chain for a reason that has nothing to do
        with the script.
        """
        return b"\x76\xa9" + bytes([len(self.hash160)]) + self.hash160 + b"\x88\xac"

    def sign_digest(self, digest: bytes) -> bytes:
        """DER signature over `digest`, low-S, with no sighash byte appended.

        `sigencode_der_canonize` is what makes it low-S. That is not cosmetic:
        Bitcoin Core has enforced LOW_S as a standardness rule since 0.11, so a
        high-S signature is rejected from the mempool with
        `non-mandatory-script-verify-flag (Non-canonical signature: S value is
        unnecessarily high)` -- which would look exactly like the script being
        wrong, on a harness whose whole job is to say whether the script is
        wrong.

        Deterministic (RFC 6979) rather than randomized, so that a failed run
        and a rerun produce the same bytes and the operator can compare two
        pastes line by line.
        """
        return self.sign_digest_with(digest)

    def sign_digest_with(self, digest: bytes) -> bytes:
        signing_key = SigningKey.from_string(self.private_key, curve=SECP256k1)
        return signing_key.sign_digest_deterministic(
            digest,
            hashfunc=hashlib.sha256,
            sigencode=sigencode_der_canonize,
        )


# A value still wrapped in the angle brackets of the instruction it was copied out of.
# Exactly the two characters, at the two ends, after stripping -- not a guess at what a weak
# seed looks like. Nobody types a secret that begins with "<" and ends with ">"; what produces
# one is a copy-paste of somebody else's example.
PLACEHOLDER_OPEN = "<"
PLACEHOLDER_CLOSE = ">"


def looks_like_an_unsubstituted_placeholder(seed: str) -> bool:
    """Is this the instruction rather than the answer to it?

    The decision is its own function so it can be called with seeded inputs (rule 10) and so the
    rule is stated once. It is deliberately NARROW: `<...>` and nothing else. A refusal that
    fired on a legitimate seed would lock an operator out of their own funding address, which is
    strictly worse than the failure it prevents, so this does not guess at "weak" or "obviously
    fake" -- those are speculation, and rule 17 asks for the thing that was measured.
    """
    stripped = seed.strip()
    return (
        len(stripped) > len(PLACEHOLDER_OPEN) + len(PLACEHOLDER_CLOSE)
        and stripped.startswith(PLACEHOLDER_OPEN)
        and stripped.endswith(PLACEHOLDER_CLOSE)
    )


def refuse_unsubstituted_placeholder(seed: str) -> None:
    """Refuse a seed that is somebody's placeholder, because it derives a REAL and WRONG address.

    MEASURED ON THE OPERATOR'S HOST, 2026-09-28, AND IT IS THE SECOND TIME. The run before this
    one was `--funding-txid <that txid>`, where bash read the `<` as a redirect and the operator
    answered "i don't know the fucking tx id" -- correct, and the response was to remove the need
    for a txid entirely. Then the same placeholder shape came back through a different door:

        export ST_ADAPTOR_FUNDING_SEED='<the same seed as yesterday>'

    Single-quoted, so bash passed it through verbatim, so it is a perfectly good non-empty seed
    and `key_from_seed` had no complaint. It derived `msuGYPvo3Fyv64Fvzeg35f6jwN9gThSuwi`, and
    the previous day's real seed had derived `msxA9RajhxTvJ4EgPwuiza1VJEYJqdsNqw`. The harness
    then looked for a payment to an address the wallet had never paid, found none, and printed
    "SO FUND THIS ADDRESS ONCE" -- correct behavior, and a complete dead end, because funding it
    would have funded the placeholder.

    WHAT MADE IT UNDIAGNOSABLE FROM THE SCREEN is that every line was right. There is no wrong
    number to notice. The only visible symptom is that the address differs from last run's, and
    an operator has no reason to be comparing addresses between runs -- so the failure presents
    as "the funded route stopped working" and sends somebody looking at the funding code.

    The empty-seed check above already establishes that this function is the right place for the
    rule: an empty seed is refused because it derives a key anybody could derive, and a
    placeholder is refused because it derives a key that is real, is yours, and is not the one
    you funded. Both are "the seed is not what you think it is", one function apart.
    """
    if looks_like_an_unsubstituted_placeholder(seed):
        raise ValueError(
            "the funding seed is still a PLACEHOLDER -- it begins with '<' and ends with '>', so "
            "it is the instruction rather than your answer to it. It would work: a placeholder is "
            "a valid seed and derives a real address. It would just be the WRONG address, and "
            "nothing on screen would say so, because every line would be correct.\n"
            "  Set ST_ADAPTOR_FUNDING_SEED to the actual value, with no angle brackets. If you do "
            "not remember the one you used before, any new value is fine -- a completed run spends "
            "its funding, so there is nothing at the old address to lose. Keep the new one set and "
            "it is the same address every run.\n"
            "  THE ADDRESS IS THE SEED'S FINGERPRINT: if the address this harness prints is not "
            "the one you funded, the seed is not the one you funded it from."
        )


def _key_from_scalar(candidate: bytes) -> RegtestKey:
    """A RegtestKey from 32 bytes already checked to be a valid secp256k1 scalar.

    ONE OWNER FOR THE DERIVATION. These four lines -- signing key, compressed public key,
    base58check P2PKH address, RegtestKey -- were written out twice, once in key_from_seed()
    and once in generate_key(), differing only in where the scalar came from. Rule 8: two
    copies of one rule agree on the day they are written. The drift this particular pair
    invites is the worst kind available here, because the compressed/uncompressed choice and
    the version byte both decide WHICH ADDRESS a key controls -- so an edit to one copy would
    produce a harness that funds an address it cannot spend from, and the failure would surface
    as a script error rather than as a key error.

    THE CALLER STILL OWNS THE REJECTION SAMPLING, deliberately: generate_key() samples from
    os.urandom and key_from_seed() walks a counter over a seeded hash, which are different
    decisions about where entropy comes from. Only the encoding is shared.

    The scalar is never logged, never printed and never returned as text; see the module header
    and RegtestKey's own docstring for where that rule is kept.

    THE PUBLIC-KEY DERIVATION IS SPELLED IN TWO PLACES AND STAYS THAT WAY, which rule 8
    requires be said at BOTH sites rather than at neither. The other one is
    modules/htlc_spend.public_key_for(), which takes the encoding as an
    argument because a WIF can ask for either, and it is the same three operations:
    SigningKey.from_string(key, curve=SECP256k1).get_verifying_key().to_string("compressed").

    They are NOT merged, for the reason modules/atomic_htlc_scripts.push_data() gives about
    regtest/txbuild.push_data(): regtest/ is the harness that MEASURES the fund path, and an
    instrument that imports the thing it measures cannot tell "the script is wrong" from "the
    shared encoder is wrong". A compression or version-byte defect reached through one import
    would put the identical defect in the key the harness signs with AND in the pubkey the
    client pushes, so the two would agree and the harness would report the script sound. If
    either is ever changed, change both.

    BOTH ALSO CARRY THE SAME PYRIGHT FINDING, and it is a packaging gap in ecdsa 0.19.2 rather
    than a defect here: that release ships no py.typed and typeshed has no stub for it, so
    pyright infers `SigningKey.get_verifying_key()` from its body, `return self.verifying_key`.
    `SigningKey.__init__` sets that attribute to None (ecdsa/keys.py:765) and every classmethod
    that fills it in -- from_string, from_secret_exponent, from_pem -- assigns through a LOCAL
    named `self` (`self = cls(_error__please_use_generate=True)`, ecdsa/keys.py:795), which
    pyright does not count as a declaration of the class attribute. The inferred type is
    therefore exactly `None`, not `VerifyingKey | None`, and the library's own docstring says
    `:rtype: VerifyingKey`.

    NOTHING IN THIS FILE CAN FIX THAT HONESTLY. An `if vk is None: raise` narrows `None` to
    `Never`, which makes the two lines after it UNCHECKED rather than checked -- it would
    remove the report by removing the analysis, and it guards nothing at runtime that is not
    already a crash. The real fix is a stub for the library (a `typings/ecdsa/keys.pyi`
    declaring `verifying_key: VerifyingKey | None`, which pyright reads from `./typings` with
    no config change) and it is a tree-wide change to how every ecdsa import is resolved, so
    it is named here and left to the operator.
    """
    signing_key = SigningKey.from_string(candidate, curve=SECP256k1)
    public_key = signing_key.get_verifying_key().to_string("compressed")
    address = base58.b58encode_check(TESTNET_P2PKH_VERSION + hash160(public_key)).decode()
    return RegtestKey(private_key=candidate, public_key=public_key, address=address)


def key_from_seed(seed: str, role: str) -> RegtestKey:
    """A keypair derived DETERMINISTICALLY from an operator-supplied seed, so its address is
    stable across runs.

    WHY A STABLE ADDRESS IS THE WHOLE POINT. generate_key() above makes a fresh key per run and
    never prints it, which is right for every key this harness uses to SPEND. But it makes one
    thing impossible: the operator cannot fund the harness in advance, because the address is
    different every time.

    Measured on the operator's Gridcoin testnet daemon 2026-09-28, and this is what makes the
    stable address worth having: their wallet is unlocked FOR STAKING ONLY, so it will not
    create a transaction -- and every RPC route around that is closed. But the harness needs
    the wallet for exactly ONE thing: coins sitting at an address it holds the key for.
    Everything after that is already in-process -- _sign_p2pkh() and _p2pkh_sighash() in
    funding_steps.py sign Tx_lock's P2PKH input here, not in the daemon -- and broadcasting is
    sendrawtransaction, which consults no lock.

    So with a stable address the operator makes ONE payment from their GUI (which elevates in
    place and hands the elevation straight back, walletmodel.cpp:615/:704 -- staking never
    stops, the unlock deadline is never discarded) and the harness never asks the wallet for
    anything again.

    THE KEY NEVER LEAVES THIS PROCESS AND THE SEED IS NEVER PRINTED. What is printed is the
    ADDRESS, which is not a secret -- it is what the operator has to be told in order to pay it.
    `role` separates the keys derived from one seed so a single seed can back more than one
    purpose without them sharing a scalar.

    SHA-256 over a domain-separated string, then rejection sampling on the scalar, matching
    generate_key()'s reasoning exactly: reducing modulo the order would bias the distribution,
    and the biased version must not exist in a repository that also signs real transactions.
    Not a slow KDF, and that is deliberate rather than an oversight -- this key guards TESTNET
    coins the operator chose to put at a throwaway address, and a seed weak enough for the KDF
    to matter is a seed that should not be reused anywhere that does matter. The docstring says
    so where an operator will read it.
    """
    if not seed or not seed.strip():
        raise ValueError(
            "an empty funding seed derives one fixed key that anybody reading this source could "
            "also derive. Set ST_ADAPTOR_FUNDING_SEED to something only you know."
        )
    refuse_unsubstituted_placeholder(seed)
    counter = 0
    while True:
        material = f"swap_terminal/adaptor-funding/v1/{role}/{counter}/{seed}".encode()
        candidate = hashlib.sha256(material).digest()
        scalar = int.from_bytes(candidate, "big")
        if 0 < scalar < SECP256K1_ORDER:
            break
        counter += 1
    return _key_from_scalar(candidate)


def generate_key() -> RegtestKey:
    """A fresh keypair with a testnet P2PKH address.

    Rejection sampling on the scalar rather than reduction modulo the order:
    reducing would bias the distribution, and while nothing here needs
    cryptographic quality against an adversary, writing the biased version in a
    repository that also signs real transactions is how the biased version gets
    copied somewhere it matters.
    """
    while True:
        candidate = os.urandom(PRIVKEY_BYTES)
        scalar = int.from_bytes(candidate, "big")
        if 0 < scalar < SECP256K1_ORDER:
            break
    return _key_from_scalar(candidate)
