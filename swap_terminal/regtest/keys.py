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

# The same testnet version bytes modules/atomic_htlc_scripts.py hardcodes.
# They are spelled again here rather than imported because importing them
# would make this module's correctness depend on that module not changing
# them -- and if they ever diverge, the round-trip assertion in
# regtest.steps (address in == address out) is what catches it, loudly,
# instead of a silent mismatch between a key and the hash160 in a script.
TESTNET_P2PKH_VERSION = b"\x6f"
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
    signing_key = SigningKey.from_string(candidate, curve=SECP256k1)
    public_key = signing_key.get_verifying_key().to_string("compressed")
    address = base58.b58encode_check(TESTNET_P2PKH_VERSION + hash160(public_key)).decode()
    return RegtestKey(private_key=candidate, public_key=public_key, address=address)
