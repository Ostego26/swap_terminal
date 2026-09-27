#!/usr/bin/env python3
"""PREIMAGE-SHA-256 crypto-conditions, which are how an HTLC works on the XRP Ledger.

Role: function (the decisions are the two encoders; nothing here talks to a
      network, reads a file, or holds state)
Reads: nothing
Writes: nothing
Can move funds: no. It produces the two hex strings an EscrowCreate and an
      EscrowFinish carry. Whoever submits those moves the coins.
Mainnet-safe: yes -- it has no endpoint and cannot submit anything.

WHY THIS EXISTS.

The XRP Ledger has no Bitcoin script, so the HTLC this repository builds for
BTC and LTC -- OP_SHA256 over a preimage, OP_CHECKLOCKTIMEVERIFY over a height
-- cannot be expressed on it. What XRPL has instead is an Escrow object with
two optional fields that are exactly a hashlock and a timelock:

    Condition     a PREIMAGE-SHA-256 crypto-condition. EscrowFinish is
                  rejected unless it carries a Fulfillment whose SHA-256
                  matches. This is the hashlock.
    CancelAfter   a time after which anyone may EscrowCancel and the XRP
                  returns to the sender. This is the timelock.

THE TWO LEGS INTERLOCK, and that is the point rather than a coincidence: the
condition commits to sha256(preimage) and so does the `OP_SHA256 <hash>
OP_EQUALVERIFY` in modules/atomic_htlc_scripts.build_htlc_redeem_script(). ONE
preimage therefore unlocks both sides of an XRP<->BTC or XRP<->LTC swap, and
revealing it on either chain reveals it on the other -- which is what makes the
swap atomic rather than merely two-sided. A different hash on either leg (RIPEMD,
HASH160, SHA-512/256) would break that and is the single thing most worth
checking if a cross-chain swap ever half-completes.

THE ENCODING, AND WHY IT IS NOT WRITTEN FROM MEMORY.

A crypto-condition is DER. The two shapes this module produces are:

    condition    A0 <len> 80 20 <32-byte sha256> 81 <len> <cost>
    fulfillment  A0 <len> 80 <len> <preimage>

where the outer A0 is the PREIMAGE-SHA-256 type tag, 80 is the fingerprint (the
hash) in a condition and the preimage in a fulfillment, and 81 is the cost --
which for this condition type is the preimage's LENGTH in bytes.

Every length here is COMPUTED, never spelled. A hardcoded `A0258020...810120`
is correct for a 32-byte preimage and silently wrong for any other, and a wrong
condition does not fail loudly: it produces an escrow that no fulfillment can
ever satisfy, whose XRP is recoverable only by waiting out CancelAfter.

CROSS-CHECKED 2026-09-26 against an INDEPENDENT implementation rather than
against my own reading of the spec -- the `cryptoconditions` package from PyPI,
which shares no code with this file. Three vectors, its output and this
module's, byte for byte:

    preimage                     condition
    b""                          A0258020E3B0C44298FC1C149AFBF4C8996FB92427
                                 AE41E4649B934CA495991B7852B855810100
    bytes(range(32))             A0258020630DCD2966C4336691125448BBB25B4FF4
                                 12A49C732DB2C8ABC1B8581BD710DD810120

    preimage                     fulfillment
    b""                          A0028000
    bytes(range(32))             A0228020000102030405060708090A0B0C0D0E0F10
                                 1112131415161718191A1B1C1D1E1F

Note what the empty-preimage vector proves that the 32-byte one cannot: the
outer length moves (0x25 -> 0x02), the inner length moves (0x20 -> 0x00), and
the cost moves (0x20 -> 0x00). A module that hardcoded any of the three would
match one vector and fail the other, which is why both are in the tests.

Those vectors are recorded in tests/test_xrp_crypto_condition.py as literals.
The library is NOT a dependency of this repo and is not imported anywhere -- it
was used once, to check the arithmetic, and the check is preserved as the
numbers it produced.

HEX CASE. XRPL returns these fields UPPERCASE and compares them as bytes after
decoding, so case does not affect validity. They are produced uppercase anyway
so a Condition read back off a ledger can be compared to one built here with
`==` rather than with a case fold that somebody will forget.
"""

from __future__ import annotations

import hashlib

# The DER tags, named rather than inlined. Each appears once below, and a
# transposed 0x80/0x81 is the kind of defect that produces a permanently
# unsatisfiable escrow instead of an error.
TYPE_PREIMAGE_SHA_256 = 0xA0
FIELD_FINGERPRINT_OR_PREIMAGE = 0x80
FIELD_COST = 0x81

# DER short-form lengths only. Everything past this needs the long form (0x81
# <len>, 0x82 <len> <len>) and this module refuses rather than emitting a length
# it has not been checked against. XRPL's own limit on a Fulfillment is far
# larger, but nothing in this repository uses a preimage that is not 32 bytes,
# and an untested encoding on a fund path is worth less than a refusal.
MAX_SHORT_FORM_LENGTH = 0x7F

# What an HTLC preimage is here: 32 bytes, the same secret the BTC and LTC
# redeem scripts commit to. Not enforced by this module -- XRPL allows other
# lengths and the empty-preimage vector above is a real condition -- but it is
# what the callers use, and a caller passing something else should know it is
# leaving the shape the other chains' scripts expect.
HTLC_PREIMAGE_BYTES = 32

# A sha256 digest, in bytes. preimage_from_fulfillment() refuses a hash that is
# not this length rather than never matching one.
SHA256_DIGEST_BYTES = 32

# The shortest possible fulfillment: A0 02 80 00, the empty preimage. Anything
# shorter cannot carry two DER elements and is rejected before indexing into it.
_MINIMUM_FULFILLMENT_BYTES = 4


def _der(tag: int, body: bytes) -> bytes:
    """One DER element. The length is computed from the body, always."""
    if len(body) > MAX_SHORT_FORM_LENGTH:
        raise ValueError(
            f"a {len(body)}-byte body needs a DER long-form length, which this module has not been checked "
            f"against (the limit is {MAX_SHORT_FORM_LENGTH}). Nothing was encoded. See the module header: an "
            "unverified encoding here produces an escrow no fulfillment can satisfy, recoverable only by "
            "waiting out CancelAfter."
        )
    return bytes([tag, len(body)]) + body


def _der_unsigned_integer(value: int) -> bytes:
    """A non-negative integer as a minimal DER INTEGER body.

    ASN.1 INTEGER is SIGNED, so a value whose top bit is set takes a leading
    zero byte -- 128 encodes as 00 80, not as 80. Zero encodes as a single 00
    rather than as nothing, which is what the empty-preimage vector in the
    module header pins.
    """
    if value < 0:
        raise ValueError(f"a crypto-condition cost cannot be negative; got {value}. Nothing was encoded.")
    if value == 0:
        return b"\x00"
    body = value.to_bytes((value.bit_length() + 7) // 8, "big")
    if body[0] & 0x80:
        body = b"\x00" + body
    return body


def preimage_condition(preimage: bytes) -> str:
    """The CONDITION for an EscrowCreate: uppercase hex, safe to publish.

    A condition reveals only sha256(preimage), so it goes on a public ledger and
    into logs freely -- it is the same public identifier the BTC and LTC redeem
    scripts carry as `secret_hash`. THE PREIMAGE IS NOT, and this function is
    the boundary: callers log what it returns, never what they passed in.
    """
    fingerprint = hashlib.sha256(preimage).digest()
    body = _der(FIELD_FINGERPRINT_OR_PREIMAGE, fingerprint) + _der(
        FIELD_COST, _der_unsigned_integer(len(preimage))
    )
    return _der(TYPE_PREIMAGE_SHA_256, body).hex().upper()


def preimage_fulfillment(preimage: bytes) -> str:
    """The FULFILLMENT for an EscrowFinish: uppercase hex, and it IS the secret.

    NEVER LOG THIS. It contains the preimage verbatim -- that is what a
    fulfillment is -- and submitting it publishes it on the ledger, which is
    exactly how the other leg of an atomic swap becomes claimable. Publishing it
    on purpose, in a transaction, is the mechanism; printing it into a log
    before the counterparty's leg is funded and confirmed gives the swap away.

    The same rule the BTC and LTC clients follow for the scriptSig they build:
    modules/htlc_spend.hashlock_script_sig() says it there.
    """
    return _der(TYPE_PREIMAGE_SHA_256, _der(FIELD_FINGERPRINT_OR_PREIMAGE, preimage)).hex().upper()


def condition_matches_preimage(condition: str, preimage: bytes) -> bool:
    """Whether this preimage is the one that condition commits to.

    A local check before submitting an EscrowFinish, in the spirit of
    modules/htlc_spend.spend_key_matches_script(): the ledger's own answer is
    `tecCRYPTOCONDITION_ERROR`, which is byte for byte what a malformed
    condition, a wrong preimage and a mismatched cost all produce, and it costs
    a transaction fee to learn. Comparing here names the problem for free.
    """
    return condition.upper() == preimage_condition(preimage)


def preimage_from_fulfillment(fulfillment: str, secret_hash: bytes) -> bytes | None:
    """The preimage inside a PREIMAGE-SHA-256 fulfillment, or None if it is not there.

    THE XRPL HALF OF AN ATOMIC SWAP'S SECRET READ, and the mirror of
    modules/htlc_spend.preimage_from_scriptsig(). When the XRP leg is the one
    CLAIMED -- which is the case whenever the XRP holder is the participant
    rather than the initiator -- the secret becomes public in an EscrowFinish's
    `Fulfillment` field, not in a scriptSig, and the counterparty reads it from
    there to claim the other chain.

    Without this, a swap can only run in the direction where the Bitcoin-style
    chain is claimed second. The two readers together are what make the pairing
    work in EITHER direction.

    IT VERIFIES THE HASH rather than trusting the framing, for the same reason
    the scriptSig reader hashes every push. A fulfillment is
    `A0 <len> 80 <len> <preimage>` and this could simply slice the last bytes --
    but the value is read off a public ledger, where anyone may submit a
    transaction carrying a well-formed fulfillment for a DIFFERENT secret. That
    is not even adversarial: on a shared account two unrelated swaps produce two
    valid fulfillments, and claiming with the wrong one burns a fee and leaves
    the real preimage unused while a timelock runs down. So the bytes are
    extracted and then hashed, and a mismatch returns None.

    Returns None rather than raising for anything malformed, absent or
    non-matching, because a participant polling a ledger needs to say "not this
    transaction" per attempt without an exception each time.
    """
    if len(secret_hash) != SHA256_DIGEST_BYTES:
        raise ValueError(
            f"a secret hash is {SHA256_DIGEST_BYTES} bytes and this one is {len(secret_hash)}. A hex string is "
            "64 characters and would never match a digest, so the caller would read `the counterparty has not "
            "revealed the preimage` off a transaction that carries it."
        )
    try:
        raw = bytes.fromhex(fulfillment)
    except ValueError:
        return None
    # Walk the two DER elements rather than slicing at fixed offsets: the outer
    # and inner lengths both move with the preimage's size (the empty-preimage
    # fulfillment is A0028000), so fixed offsets are correct for a 32-byte secret
    # and silently wrong for anything else.
    if len(raw) < _MINIMUM_FULFILLMENT_BYTES or raw[0] != TYPE_PREIMAGE_SHA_256:
        return None
    outer_length = raw[1]
    body = raw[2:2 + outer_length]
    if len(body) != outer_length or not body or body[0] != FIELD_FINGERPRINT_OR_PREIMAGE:
        return None
    inner_length = body[1]
    preimage = body[2:2 + inner_length]
    if len(preimage) != inner_length:
        return None
    if hashlib.sha256(preimage).digest() != secret_hash:
        return None
    return preimage


def preimage_from_escrow_finish(transaction: dict, secret_hash: bytes) -> bytes | None:
    """The preimage out of an EscrowFinish transaction as a ledger returns it.

    Reads the `Fulfillment` field and verifies it against the commitment. A
    transaction with no Fulfillment -- an EscrowCancel, an ordinary Payment, or
    an EscrowFinish for a conditionless escrow -- yields None, which is the
    ordinary case while polling and not an error.

    Takes the transaction DICT rather than a client, deliberately: how the
    caller obtained it (rippled's `tx`, a `transaction_entry`, an
    `account_tx` row, or xrpl-py's response) is not this function's business,
    and keeping it out means the decision can be tested without a ledger.
    """
    fulfillment = transaction.get("Fulfillment") or (transaction.get("tx_json") or {}).get("Fulfillment")
    if not fulfillment:
        return None
    return preimage_from_fulfillment(str(fulfillment), secret_hash)
