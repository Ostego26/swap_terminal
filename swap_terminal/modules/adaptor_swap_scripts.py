"""The 2-of-2 scripts an adaptor-signature swap needs, which the HTLC cannot provide.

Role: submodule (script construction and signature assembly; the decisions are the
      functions here, each callable with seeded inputs)
Reads: nothing. Pure functions of keys, scripts and transactions.
Writes: nothing. It builds bytes; broadcasting them is somebody else's job.
Can move funds: no, and that distinction matters here. Every byte this module produces
      is unsigned or partially signed until a caller adds a signature, and it never
      opens a socket. What it produces CAN move funds once broadcast.
Mainnet-safe: yes in the sense that applies -- it contacts no chain and knows no network.
      The scripts it builds are network-independent; the P2SH version byte that wraps
      them is chosen by the caller (modules/address_network owns that vocabulary).
Live-safe: yes

WHY THIS FILE EXISTS, AND WHY THE HTLC COULD NOT BE REUSED.

Measured 2026-09-27 by reading modules/atomic_htlc_scripts.build_htlc_redeem_script() and
modules/htlc_spend.hashlock_script_sig(), and confirmed against the redeem script from a
real Gridcoin swap that completed the same evening: BOTH BRANCHES OF THIS REPO'S HTLC END
IN A SINGLE-KEY OP_CHECKSIG. The claimer satisfies it with a signature under their own key,
produced freely by sign_digest().

AN ADAPTOR SIGNATURE HAS NO PURCHASE ON AN OUTPUT THE SPENDER CAN ALREADY SIGN. The whole
mechanism requires the spender to be UNABLE to sign alone, so that the only signature
available to them is one they must COMPLETE with a scalar -- and completing it is what
publishes the scalar. Against a single-key output there is nothing to withhold and
therefore nothing to leak, so the swap has no way to link the two chains.

Monero has no script at all, so no Monero transaction can be made to reveal anything. The
only publishable act in the whole swap is on the script chain, which is why the scalar must
leak there and why this module is the thing standing between four tested components and a
working XMR swap.

docs/monero_swap_protocol.md section 2 specifies the five transactions; this file builds
the scripts and signature assembly they are made of.

GRIDCOIN ACCEPTS THESE SCRIPTS, and that was the gating question. Read from
Gridcoin-Research src/script.cpp on 2026-09-28: OP_CHECKMULTISIG is fully implemented in
EvalScript (not disabled or reserved), TX_MULTISIG is a Solver template, the P2SH path
recurses into the subscript so a redeem script may contain multisig, and no P2SH-specific
sigop cap appears. OP_CHECKLOCKTIMEVERIFY is implemented under
SCRIPT_VERIFY_CHECKLOCKTIMEVERIFY -- and that flag is not merely present in the source:
three swaps on 2026-09-27 proved CLTV is CONSENSUS-enforced on Gridcoin, because the daemon
refused to MINE an early refund into a block (generateblock answered
"TestBlockValidity failed"), which is the chain refusing rather than a relay policy.

That is a source reading plus a measurement, and they answer different halves. What is NOT
yet established is a 2-of-2 P2SH actually spending on Gridcoin; that needs a chain and is
named as such in the test file rather than assumed here.
"""

from __future__ import annotations

from modules.atomic_htlc_scripts import p2sh_script_for, push_data
from modules.htlc_spend import ParsedTransaction, legacy_sighash

# Opcodes. Spelled here rather than imported from atomic_htlc_scripts because that module
# declares only the four the HTLC needs and none of these; two of the names would collide
# with different values if both files grew (rule 8's failure with a delay on it).
OP_0 = b"\x00"
OP_2 = b"\x52"
OP_CHECKMULTISIG = b"\xae"

# A compressed secp256k1 public key. UNCOMPRESSED KEYS ARE REFUSED rather than supported:
# a 2-of-2 whose two keys are encoded differently still works, but the redeem script's
# hash160 then depends on an encoding choice nobody records, and the counterparty who
# rebuilds the script from the same two keys gets a different address. Refusing one
# encoding makes the script a function of the keys alone.
COMPRESSED_PUBKEY_BYTES = 33

# The number of signatures a 2-of-2 needs and the number of keys it names. Both are 2 and
# they are separate constants on purpose: a reader checking "is this 2-of-2 or 1-of-2" should
# find the answer twice, because OP_CHECKMULTISIG takes them at opposite ends of the script
# and transposing them silently produces a 2-of-2 that a single party can spend.
MULTISIG_REQUIRED = 2
MULTISIG_TOTAL = 2

# A DER sequence's leading tag byte. Checked because a raw 64-byte (r, s) pair -- what most
# signing libraries hand back by default -- reaches the node as a malformed push and is
# reported as a bare script failure, with nothing to say the encoding was the problem.
DER_SEQUENCE_TAG = 0x30

# The shortest thing that could be a DER signature plus a SIGHASH byte: 0x30, a length, two
# 3-byte INTEGERs at minimum, and the trailing SIGHASH. Anything shorter is not a short
# signature, it is a different object.
MINIMUM_DER_SIGNATURE_BYTES = 9


class AdaptorScriptError(ValueError):
    """A script or signature that must not be built, rather than one that failed to build."""


def two_of_two_redeem_script(first_pubkey: bytes, second_pubkey: bytes) -> bytes:
    """OP_2 <first> <second> OP_2 OP_CHECKMULTISIG, with the key ORDER preserved.

    THE ORDER IS PART OF THE SCRIPT AND PART OF THE PROTOCOL. OP_CHECKMULTISIG walks the
    signatures and the keys in lockstep, so a spend must supply its signatures in the same
    order the keys appear here -- see two_of_two_script_sig(), which is where that is
    enforced rather than hoped for.

    This function therefore does NOT sort the keys. A sorted variant would be friendlier and
    is refused: both parties derive this script independently from the same two public keys,
    and if one sorts and the other does not they compute different addresses and the swap
    dies at funding with no indication which side is wrong. The caller decides the order once
    (docs/monero_swap_protocol.md fixes it at setup) and both sides use it.

    Refuses anything that is not a 33-byte compressed key, and refuses two IDENTICAL keys --
    a 2-of-2 naming one key twice is spendable by whoever holds that key alone, which is the
    degenerate case build_htlc_redeem_script() already refuses for the same reason.
    """
    for label, key in (("first", first_pubkey), ("second", second_pubkey)):
        if not isinstance(key, bytes | bytearray):
            raise AdaptorScriptError(f"the {label} public key is {type(key).__name__}, not bytes")
        if len(key) != COMPRESSED_PUBKEY_BYTES:
            raise AdaptorScriptError(
                f"the {label} public key is {len(key)} bytes; a compressed secp256k1 key is "
                f"{COMPRESSED_PUBKEY_BYTES}. Uncompressed keys are refused because the script's "
                f"hash160 would then depend on an encoding choice nobody records"
            )
        if key[0] not in (0x02, 0x03):
            raise AdaptorScriptError(
                f"the {label} public key starts with {key[0]:#04x}; a compressed key starts "
                f"with 0x02 or 0x03"
            )
    if bytes(first_pubkey) == bytes(second_pubkey):
        raise AdaptorScriptError(
            "both keys of the 2-of-2 are the same key, which makes the output spendable by "
            "whoever holds it ALONE. That is not a 2-of-2 and it is the same degenerate case "
            "build_htlc_redeem_script() refuses when both branches hash to one key"
        )
    return (
        OP_2
        + push_data(bytes(first_pubkey))
        + push_data(bytes(second_pubkey))
        + OP_2
        + OP_CHECKMULTISIG
    )


def two_of_two_p2sh_script(redeem_script: bytes) -> bytes:
    """The P2SH scriptPubKey that pays this redeem script.

    One line, and it delegates to the same p2sh_script_for() the HTLC path uses, so a
    contract output built here is located on chain by the same htlc_vout() scriptPubKey match
    that has found every contract this repository has funded (rule 8).
    """
    return p2sh_script_for(redeem_script)


def two_of_two_script_sig(first_signature: bytes, second_signature: bytes,
                          redeem_script: bytes) -> bytes:
    """OP_0 <sig_first> <sig_second> <redeemScript>, in the key order of the script.

    TWO THINGS HERE ARE EASY TO GET WRONG AND BOTH FAIL THE SAME WAY -- a bare script
    failure with no hint which of them it was.

    THE LEADING OP_0 IS NOT PADDING. OP_CHECKMULTISIG pops one item more than it uses, a
    consensus bug from 2010 that can never be fixed because fixing it would invalidate old
    transactions. The extra pop must be present and must be an empty push; a missing OP_0
    consumes the first signature as the dummy and the script fails having looked correct.

    THE SIGNATURE ORDER MUST MATCH THE KEY ORDER in the redeem script. OP_CHECKMULTISIG walks
    both lists in lockstep and never backtracks, so signatures for keys [A, B] supplied as
    [sig_B, sig_A] verify NEITHER -- it tries sig_B against A, fails, discards A, tries sig_B
    against B, succeeds, then has sig_A left with no keys remaining. The caller's argument
    names are `first` and `second` to match two_of_two_redeem_script()'s, so the pairing is
    visible at both sites rather than documented at one.

    Each signature must already carry its SIGHASH byte -- sign_digest() appends it, and a
    signature without one is refused here rather than by the node.
    """
    for label, signature in (("first", first_signature), ("second", second_signature)):
        if not isinstance(signature, bytes | bytearray) or not signature:
            raise AdaptorScriptError(f"the {label} signature is empty or not bytes")
        if signature[0] != DER_SEQUENCE_TAG:
            raise AdaptorScriptError(
                f"the {label} signature does not start with {DER_SEQUENCE_TAG:#04x}, so it is "
                f"not DER. A raw "
                f"(r, s) pair reaches the node as a malformed push"
            )
        if len(signature) < MINIMUM_DER_SIGNATURE_BYTES:
            raise AdaptorScriptError(
                f"the {label} signature is {len(signature)} bytes, too short to "
                f"be DER plus a SIGHASH byte (minimum {MINIMUM_DER_SIGNATURE_BYTES}). "
                f"sign_digest() appends the SIGHASH byte; a signature without "
                f"one verifies against nothing"
            )
    return (
        OP_0
        + push_data(bytes(first_signature))
        + push_data(bytes(second_signature))
        + push_data(bytes(redeem_script))
    )


def two_of_two_sighash(parsed: ParsedTransaction, input_index: int,
                       redeem_script: bytes) -> bytes:
    """The digest both parties sign, and the digest an adaptor pre-signature is made over.

    Delegates to htlc_spend.legacy_sighash() with the REDEEM SCRIPT as the script code, which
    is what a P2SH input signs over -- not the P2SH scriptPubKey. That distinction has its own
    paragraph in legacy_sighash()'s docstring because signing the scriptPubKey produces a
    signature that verifies against nothing and the node reports only a bare script failure.

    This exists as a named function rather than a call at each site because the adaptor path
    has FOUR transactions signed over this digest shape (redeem, cancel, refund, punish) and
    one of them -- the pre-signature -- is computed by a party who is not signing in the
    ordinary way. A single definition is what lets the pre-signature and the completed
    signature be provably over the same bytes.
    """
    return legacy_sighash(parsed, input_index, redeem_script)
