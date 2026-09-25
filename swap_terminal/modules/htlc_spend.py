#!/usr/bin/env python3
"""Assemble and sign the scriptSig that spends an HTLC's hashlock branch.

Role: function level (the decisions -- the push order, the sighash, the WIF
      decode, the size the fee is computed from). Nothing here touches a chain.
Reads: nothing. Every input is an argument -- no RPC, no file, no environment.
Writes: nothing. It returns bytes; broadcasting belongs to the caller.
Can move funds: no directly. It PRODUCES a signed transaction, and whoever
      broadcasts what it returns moves the contract's coins. Treat a change
      here as fund movement.
Mainnet-safe: yes in the sense that it opens nothing. What it returns would be
      perfectly valid on mainnet if it were fed mainnet inputs, so read it as a
      signing library, not as a safe one.

WHY THIS FILE EXISTS: redeem_contract() NEVER PUSHED THE PREIMAGE.

Measured 2026-09-25 against real Bitcoin and Litecoin regtest daemons. All
three clients' `redeem_contract()` accepted a `secret: bytes` parameter and
never referenced it -- established first by walking each function's AST, then
confirmed on chain. They built the spend with `createrawtransaction` and handed
it to a `signrawtransaction*` call, which constructs a scriptSig for a P2SH
input by RECOGNIZING A SCRIPT PATTERN: P2PKH, multisig, P2SH wrapping a
recognized form, and the segwit variants. An HTLC redeem script is
OP_IF/OP_ELSE/OP_ENDIF and matches none of them, and there is no argument
either RPC takes that says "take the OP_IF branch, and here is the preimage".
The daemons said so in their own words:

    BTC   'error': 'Unable to sign input, invalid stack size (possibly missing key)'
    LTC   'error': 'Invalid OP_IF construction'

So a funded contract was recoverable only by refund -- which is to say, only by
waiting out the timelock and giving up the swap. That is the defect this module
removes, and the removal is to stop asking the wallet to do something it cannot
do and assemble the scriptSig here instead.

THE PUSH ORDER, AND WHY IT IS WHAT IT IS.

P2SH spending evaluates the scriptSig, then pops its LAST push as the redeem
script and executes that against what remains on the stack. So the pushes
before the redeem script are, bottom to top, exactly what the redeem script
expects to find:

    <signature> <pubkey> <preimage> OP_1 <redeemScript>

OP_IF pops the OP_1 and takes the true branch. OP_SHA256 then consumes the
preimage, OP_EQUALVERIFY compares it against the secret hash the script
carries, and OP_DUP OP_HASH160 <participant hash160> OP_EQUALVERIFY OP_CHECKSIG
consumes the pubkey and the signature.

Get that order wrong and a node answers `mandatory-script-verify-flag-failed`,
which is the SAME thing it says when the redeem script itself is wrong. That is
why the order is asserted in a stack machine in tests/test_htlc_spend.py
against the real builder's script, rather than trusted to review.

THERE IS A SECOND IMPLEMENTATION OF THIS, DELIBERATELY. Rule 8 requires each
site to name the other. `regtest/txbuild.py::build_branch_spend` builds the
same hashlock scriptSig (and the refund one, which no client implements). It is
NOT merged into this module and this module does not import it, because
txbuild is the regtest harness's independent CONTROL spender: its whole job is
to answer "is the redeem SCRIPT sound, or is the CLIENT wrong?" by spending the
same output a different way. An instrument that imported the thing it measures
would fail in lockstep with it, and the harness would report the script as
broken when a shared encoder was. The two are kept apart on purpose and held
together mechanically instead, by
tests/test_htlc_spend.py::test_the_client_and_the_harness_build_the_same_scriptsig,
which asserts they produce BYTE-IDENTICAL output for the same inputs. If either
changes, that test says so on the next run.

WHY THE TRANSACTION IS PARSED RATHER THAN BUILT FROM SCRATCH.

The node builds the unsigned transaction with `createrawtransaction` and this
module only replaces the input's scriptSig inside the node's own bytes. That
looks like the long way round and it is load-bearing twice over:

  ADDRESSES. Converting a destination address to a scriptPubKey means
  implementing base58 P2PKH, base58 P2SH, bech32 and bech32m, on three chains
  with different human-readable parts and different version bytes. The daemon
  already knows its own address formats and cannot disagree with itself.

  GRIDCOIN'S TRANSACTION LAYOUT. Gridcoin descends from the Peercoin line of
  proof-of-stake forks, whose CTransaction carries a 4-byte `nTime` between the
  version and the input count -- a field Bitcoin and Litecoin do not have. A
  serializer written to Bitcoin's layout would produce an invalid Gridcoin
  transaction, and a sighash computed over it would sign the wrong bytes.
  Reusing the node's serialization verbatim means this module never has to know
  which layout it is looking at. NOT VERIFIED ON A GRIDCOIN NODE: there is no
  GRC regtest in this setup, so the nTime field is reasoned from Gridcoin's
  lineage and not measured (rule 17). `parse_transaction()` is written so that
  being wrong about it is impossible to do QUIETLY -- it tries each candidate
  layout, re-serializes, and accepts only the one that reproduces the daemon's
  bytes exactly and whose first input is the outpoint the caller asked to spend.
"""

from __future__ import annotations

import hashlib
import struct
from dataclasses import dataclass
from decimal import Decimal

import base58
from ecdsa import SECP256k1, SigningKey
from ecdsa.util import sigencode_der_canonize
from modules.atomic_htlc_scripts import push_data
from modules.utils import hash160

SIGHASH_ALL = 0x01

# OP_1 pushes the number 1, which is the TRUE that sends OP_IF down the
# hashlock branch. OP_0 is the refund branch's selector and is named here only
# so a reader of this file can see that the two differ by one byte -- no client
# in this repository implements a refund.
OP_1 = b"\x51"

# The largest a DER signature plus its one-byte sighash type can be. r and s
# are each at most 32 bytes plus a possible leading zero pad, and the DER
# envelope adds six: 0x30 <len> 0x02 <rlen> r 0x02 <slen> s. Low-S canonical
# signing keeps s below the curve order's halfway point, so s never needs the
# pad and the real maximum is 72; 73 is used as the upper bound because
# overestimating by one byte overpays a fee by half a satoshi and
# UNDERestimating would size a fee from a transaction smaller than the one
# actually broadcast.
MAX_DER_SIGNATURE_WITH_HASHTYPE = 73

# Base58Check WIF payload: 1 version byte + 32 key bytes, plus a trailing 0x01
# when the key is for a COMPRESSED public key. The version byte differs per
# chain (0x80 Bitcoin mainnet, 0xEF testnet and regtest, 0xB0 Litecoin mainnet)
# and is deliberately NOT checked: this module signs for three chains and a
# version allowlist would be a fourth place that has to learn about a new one.
# What matters for correctness is the length and the compression flag, because
# the compression flag decides which public key is pushed, and the public key
# has to hash to the hash160 the redeem script commits to.
WIF_PAYLOAD_LEN = 33
WIF_PAYLOAD_LEN_COMPRESSED = 34
WIF_COMPRESSED_SUFFIX = 0x01

# Compact-size boundaries. A varint encodes COUNTS and SCRIPT LENGTHS, and is
# not the same encoding as a script number: this repository confused the two
# once already and every HTLC refund branch was unspendable for it (see
# modules/atomic_htlc_scripts.encode_script_number).
VARINT_1BYTE_MAX = 0xFC
VARINT_2BYTE_MAX = 0xFFFF
VARINT_4BYTE_MAX = 0xFFFFFFFF
# The marker byte a multi-byte compact size starts with, and how many bytes
# follow it. Named rather than spelled inline so the three thresholds above and
# the three widths here read as one encoding rather than as six literals.
VARINT_MARKER_WIDTHS = {0xFD: 2, 0xFE: 4, 0xFF: 8}

# The transaction layouts this module knows how to take apart, as the number of
# bytes before the input count. See the module docstring.
#   4  Bitcoin and Litecoin: [version]
#   8  Gridcoin and the Peercoin line: [version][nTime]
CANDIDATE_PREFIX_LENGTHS = (4, 8)

# Sanity bounds used while deciding which layout a blob is in. They are
# deliberately loose -- the decisive checks are the exact re-serialization and
# the expected outpoint -- and exist so that a wrong guess fails fast instead of
# trying to allocate a list of four billion inputs.
MAX_PLAUSIBLE_IO_COUNT = 10_000
MAX_PLAUSIBLE_SCRIPT_LEN = 100_000

OUTPOINT_LEN = 36
SEQUENCE_LEN = 4
VALUE_LEN = 8
LOCKTIME_LEN = 4


class TransactionLayoutError(ValueError):
    """A raw transaction could not be taken apart in any layout this module knows.

    Its own error class because the caller's response is specific: it must NOT
    fall back to signing something, and the message has to name every layout
    that was tried so an operator on an unfamiliar chain can see what the
    daemon handed back.
    """


# Eight decimal places on BTC, LTC and GRC alike. A transaction carries an
# integer number of satoshis; a wallet RPC talks in coin. There is a second
# copy of this conversion in regtest/txbuild.py::coins_to_satoshis, and rule 8
# requires each to name the other -- that one belongs to the harness's
# independent control spender and is deliberately not shared (see push_data in
# modules/atomic_htlc_scripts.py for the full reasoning).
SATOSHIS_PER_COIN = 100_000_000


def coins_to_satoshis(amount: str | Decimal) -> int:
    """Whole satoshis from a coin amount, via Decimal-safe string arithmetic.

    Takes a string or a Decimal and never a float, because
    `int(0.1 * 100_000_000)` is 10000000 for some values and 9999999 for
    others, and an output one satoshi short of what was intended is a defect
    nobody reads as a float problem.
    """
    return int((Decimal(str(amount)) * SATOSHIS_PER_COIN).to_integral_value())


def satoshis_to_coins(satoshis: int) -> Decimal:
    """A coin amount from whole satoshis, exactly, as a Decimal."""
    return (Decimal(satoshis) / SATOSHIS_PER_COIN).quantize(Decimal("0.00000001"))


def varint(value: int) -> bytes:
    """Bitcoin's compact size encoding, for counts and script lengths."""
    if value < 0:
        raise ValueError(f"a compact size is never negative, got {value!r}")
    if value <= VARINT_1BYTE_MAX:
        return bytes([value])
    if value <= VARINT_2BYTE_MAX:
        return b"\xfd" + struct.pack("<H", value)
    if value <= VARINT_4BYTE_MAX:
        return b"\xfe" + struct.pack("<I", value)
    return b"\xff" + struct.pack("<Q", value)


def varint_length(value: int) -> int:
    """How many bytes `varint(value)` will occupy, without building it."""
    return len(varint(value))


def _read_varint(raw: bytes, offset: int) -> tuple[int, int]:
    """(value, new offset). Raises IndexError-shaped ValueError past the end."""
    if offset >= len(raw):
        raise TransactionLayoutError(f"ran off the end of the transaction reading a compact size at byte {offset}")
    first = raw[offset]
    offset += 1
    if first <= VARINT_1BYTE_MAX:
        return first, offset
    width = VARINT_MARKER_WIDTHS[first]
    if offset + width > len(raw):
        raise TransactionLayoutError(
            f"a {width}-byte compact size at byte {offset} runs past the end of a {len(raw)}-byte transaction"
        )
    return int.from_bytes(raw[offset : offset + width], "little"), offset + width


def double_sha256(data: bytes) -> bytes:
    """SHA256(SHA256(data)) -- what a legacy sighash and a txid are both made of."""
    return hashlib.sha256(hashlib.sha256(data).digest()).digest()


def decode_wif(wif: str) -> tuple[bytes, bool]:
    """(32-byte private key, whether its public key is compressed).

    The compression flag is not cosmetic. The redeem script commits to
    HASH160(pubkey), and the compressed and uncompressed encodings of one key
    hash to two different values. Pushing the wrong one fails at
    OP_EQUALVERIFY, which reads on screen as `mandatory-script-verify-flag-failed`
    -- indistinguishable from the preimage being wrong, or the script being
    wrong, or the branch selector being wrong.

    Raises:
        ValueError: if the string is not valid Base58Check, or its payload is
            not one of the two lengths a WIF has. It never returns a key it is
            unsure about: a private key this function got wrong would sign a
            transaction that cannot be spent and cannot be taken back.
    """
    try:
        payload = base58.b58decode_check(wif)
    except Exception as exc:
        # The WIF ITSELF IS NEVER IN THIS MESSAGE, and that is the point of
        # catching and re-raising rather than letting base58's own error out:
        # base58 puts the offending string in its exception text, and this one
        # is a signing key. Exposure is total loss of whatever it controls.
        raise ValueError(
            "the private key is not valid Base58Check (the key itself is deliberately not shown). "
            "A WIF is what `dumpprivkey` prints and what BTC_HTLC_PRIVKEY holds."
        ) from exc
    if len(payload) == WIF_PAYLOAD_LEN:
        return payload[1:], False
    if len(payload) == WIF_PAYLOAD_LEN_COMPRESSED and payload[-1] == WIF_COMPRESSED_SUFFIX:
        return payload[1:-1], True
    raise ValueError(
        f"the private key decoded to a {len(payload)}-byte payload; a WIF is {WIF_PAYLOAD_LEN} bytes "
        f"(uncompressed) or {WIF_PAYLOAD_LEN_COMPRESSED} ending in 0x01 (compressed). The key is not shown."
    )


def public_key_for(private_key: bytes, compressed: bool) -> bytes:
    """The public key to push in the scriptSig, in the encoding the WIF asked for."""
    verifying_key = SigningKey.from_string(private_key, curve=SECP256k1).get_verifying_key()
    return verifying_key.to_string("compressed" if compressed else "uncompressed")


def sign_digest(private_key: bytes, digest: bytes) -> bytes:
    """A low-S DER signature over `digest`, with the SIGHASH_ALL byte appended.

    Low-S (`sigencode_der_canonize`) is not tidiness: Bitcoin Core has enforced
    LOW_S as a standardness rule since 0.11, so a high-S signature is refused
    from the mempool with `Non-canonical signature: S value is unnecessarily
    high` -- which, on a spend of a conditional script, reads exactly like the
    script having failed.

    Deterministic (RFC 6979) rather than randomized, so that the same spend
    built twice produces the same bytes. A redeem that has to be rebuilt and
    rebroadcast then has the same txid rather than becoming a second, competing
    transaction.
    """
    signing_key = SigningKey.from_string(private_key, curve=SECP256k1)
    der = signing_key.sign_digest_deterministic(
        digest,
        hashfunc=hashlib.sha256,
        sigencode=sigencode_der_canonize,
    )
    return der + bytes([SIGHASH_ALL])


def hashlock_script_sig(signature: bytes, public_key: bytes, secret: bytes, redeem_script: bytes) -> bytes:
    """<sig> <pubkey> <preimage> OP_1 <redeemScript> -- the hashlock branch.

    `secret` is the preimage, and it becomes public the instant this
    transaction is relayed. That is not a leak, it is how an atomic swap works:
    revealing it on THIS chain is what lets the counterparty take their leg on
    the other. What must never happen is revealing it before the counterparty's
    leg is funded and confirmed, which is a sequencing decision belonging to
    whoever calls this -- and it must never be LOGGED (chain-safety rules), so
    nothing in this module prints it.
    """
    return push_data(signature) + push_data(public_key) + push_data(secret) + OP_1 + push_data(redeem_script)


@dataclass(frozen=True)
class ParsedTransaction:
    """A raw transaction taken apart just far enough to replace one scriptSig.

    `prefix` and `suffix` are carried VERBATIM -- whatever the daemon put
    before the input count and after the last output, including Gridcoin's
    nTime and every chain's nLockTime. Nothing in this class interprets them,
    which is what lets one implementation serve three chains that do not agree
    on the layout.
    """

    prefix: bytes
    inputs: tuple[tuple[bytes, bytes, bytes], ...]
    outputs: tuple[tuple[int, bytes], ...]
    suffix: bytes

    def serialize(self, script_sigs: dict[int, bytes] | None = None) -> bytes:
        """Re-emit the transaction, optionally replacing some inputs' scriptSigs.

        With no replacements this returns the exact bytes it was parsed from,
        which is how `parse_transaction()` proves it read the layout correctly.
        """
        overrides = script_sigs or {}
        body = self.prefix + varint(len(self.inputs))
        for index, (outpoint, script_sig, sequence) in enumerate(self.inputs):
            chosen = overrides.get(index, script_sig)
            body += outpoint + varint(len(chosen)) + chosen + sequence
        body += varint(len(self.outputs))
        for value, script_pubkey in self.outputs:
            body += struct.pack("<q", value) + varint(len(script_pubkey)) + script_pubkey
        return body + self.suffix

    def size_with_script_sig(self, index: int, script_sig_length: int) -> int:
        """Serialized size if input `index` carried a scriptSig of that length.

        Computed rather than measured because the fee has to be known BEFORE
        the signature exists -- the output amounts depend on the fee, the
        sighash depends on the output amounts, and the signature depends on the
        sighash. Breaking that circle is the only reason this method exists.
        """
        current = self.inputs[index][1]
        base = len(self.serialize())
        return (
            base
            - varint_length(len(current))
            - len(current)
            + varint_length(script_sig_length)
            + script_sig_length
        )

    @property
    def output_total(self) -> int:
        """The sum of every output, in satoshis -- what the transaction pays out.

        The fee is the input value minus this, and reading it off the parsed
        transaction is how the caller checks the fee it MEANT to pay against the
        fee the bytes actually encode, rather than trusting its own arithmetic
        to have survived a float trip through the daemon's JSON.
        """
        return sum(value for value, _ in self.outputs)


def parse_transaction(raw: bytes, expected_txid: str | None = None, expected_vout: int | None = None) -> ParsedTransaction:
    """Take a raw transaction apart, deciding its layout by proof rather than by guess.

    Each candidate layout in CANDIDATE_PREFIX_LENGTHS is tried in turn and a
    parse is accepted only if BOTH of these hold:

      1. re-serializing reproduces the input bytes exactly, and
      2. if the caller named an outpoint, input 0 is that outpoint.

    The second check is what makes the decision unambiguous rather than
    probabilistic. Gridcoin's nTime is a unix timestamp, so read as a Bitcoin
    transaction its bytes would be an input count and a txid prefix -- a parse
    that happens to round-trip is conceivable, and one that also happens to
    land the caller's own 32-byte txid at the right offset is not.

    Raises:
        TransactionLayoutError: naming every layout tried and why each failed.
            It never returns a best guess. A transaction parsed into the wrong
            shape would be signed over the wrong bytes and broadcast, and there
            is no version of that failure that can be taken back.
    """
    failures: list[str] = []
    for prefix_length in CANDIDATE_PREFIX_LENGTHS:
        try:
            parsed = _parse_with_prefix(raw, prefix_length)
        except TransactionLayoutError as exc:
            failures.append(f"{prefix_length}-byte prefix: {exc}")
            continue
        if parsed.serialize() != raw:
            failures.append(f"{prefix_length}-byte prefix: parsed, but re-serializing did not reproduce the bytes")
            continue
        if expected_txid is not None:
            outpoint = parsed.inputs[0][0]
            got_txid = outpoint[:32][::-1].hex()
            got_vout = int.from_bytes(outpoint[32:36], "little")
            if got_txid != expected_txid or (expected_vout is not None and got_vout != expected_vout):
                failures.append(
                    f"{prefix_length}-byte prefix: round-tripped, but input 0 is {got_txid}:{got_vout} "
                    f"and the caller asked to spend {expected_txid}:{expected_vout}"
                )
                continue
        return parsed
    raise TransactionLayoutError(
        f"could not take apart a {len(raw)}-byte transaction in any known layout. Tried: "
        + "; ".join(failures)
        + ". The layouts are Bitcoin/Litecoin (4-byte version) and the Peercoin line including Gridcoin "
        "(4-byte version plus a 4-byte nTime). A segwit-serialized transaction would also land here, and this "
        "module does not sign one: an HTLC P2SH input has no witness."
    )


def _parse_with_prefix(raw: bytes, prefix_length: int) -> ParsedTransaction:
    """One attempt at one layout. Structural failures raise; the caller tries the next."""
    if len(raw) <= prefix_length:
        raise TransactionLayoutError(f"only {len(raw)} bytes, which is not even a {prefix_length}-byte prefix")
    inputs, offset = _parse_inputs(raw, prefix_length)
    outputs, offset = _parse_outputs(raw, offset)
    if len(raw) - offset != LOCKTIME_LEN:
        raise TransactionLayoutError(
            f"{len(raw) - offset} bytes left after the last output, and a transaction ends with a "
            f"{LOCKTIME_LEN}-byte nLockTime"
        )
    return ParsedTransaction(
        prefix=raw[:prefix_length],
        inputs=tuple(inputs),
        outputs=tuple(outputs),
        suffix=raw[offset:],
    )


def _parse_inputs(raw: bytes, offset: int) -> tuple[list[tuple[bytes, bytes, bytes]], int]:
    """Every input as (outpoint, scriptSig, sequence), and where the outputs start."""
    input_count, offset = _read_varint(raw, offset)
    if not 1 <= input_count <= MAX_PLAUSIBLE_IO_COUNT:
        raise TransactionLayoutError(f"input count {input_count} is not plausible")
    inputs: list[tuple[bytes, bytes, bytes]] = []
    for _ in range(input_count):
        if offset + OUTPOINT_LEN > len(raw):
            raise TransactionLayoutError("ran off the end reading an outpoint")
        outpoint = raw[offset : offset + OUTPOINT_LEN]
        offset += OUTPOINT_LEN
        script_len, offset = _read_varint(raw, offset)
        if script_len > MAX_PLAUSIBLE_SCRIPT_LEN or offset + script_len + SEQUENCE_LEN > len(raw):
            raise TransactionLayoutError(f"a {script_len}-byte scriptSig does not fit")
        script_sig = raw[offset : offset + script_len]
        offset += script_len
        sequence = raw[offset : offset + SEQUENCE_LEN]
        offset += SEQUENCE_LEN
        inputs.append((outpoint, script_sig, sequence))
    return inputs, offset


def _parse_outputs(raw: bytes, offset: int) -> tuple[list[tuple[int, bytes]], int]:
    """Every output as (satoshis, scriptPubKey), and where the nLockTime starts."""
    output_count, offset = _read_varint(raw, offset)
    if not 1 <= output_count <= MAX_PLAUSIBLE_IO_COUNT:
        raise TransactionLayoutError(f"output count {output_count} is not plausible")
    outputs: list[tuple[int, bytes]] = []
    for _ in range(output_count):
        if offset + VALUE_LEN > len(raw):
            raise TransactionLayoutError("ran off the end reading an output value")
        value = struct.unpack("<q", raw[offset : offset + VALUE_LEN])[0]
        offset += VALUE_LEN
        script_len, offset = _read_varint(raw, offset)
        if script_len > MAX_PLAUSIBLE_SCRIPT_LEN or offset + script_len > len(raw):
            raise TransactionLayoutError(f"a {script_len}-byte scriptPubKey does not fit")
        outputs.append((value, raw[offset : offset + script_len]))
        offset += script_len
    return outputs, offset


def legacy_sighash(parsed: ParsedTransaction, input_index: int, script_code: bytes) -> bytes:
    """The SIGHASH_ALL digest for one input, under pre-segwit rules.

    Every OTHER input's scriptSig is emptied and the signed input's is filled
    with the SCRIPT CODE -- for a P2SH input that is the redeem script itself,
    not the P2SH scriptPubKey. Signing over the scriptPubKey instead produces a
    signature that verifies against nothing, and the node reports it as a bare
    script failure with no hint about which of the two scripts was wrong.
    """
    substitutions = {index: (script_code if index == input_index else b"") for index in range(len(parsed.inputs))}
    return double_sha256(parsed.serialize(substitutions) + struct.pack("<I", SIGHASH_ALL))


def estimated_script_sig_length(public_key: bytes, secret: bytes, redeem_script: bytes) -> int:
    """An exact UPPER BOUND on the hashlock scriptSig, before the signature exists.

    Built by assembling the real scriptSig around a dummy signature of the
    maximum length, with the real push encoder, so the bound cannot drift from
    what is actually produced: everything except the signature is known
    exactly, and the signature's only freedom is to be one or two bytes
    shorter. An upper bound is the safe direction -- the fee is computed from
    it, so the transaction that is broadcast is never larger than the one the
    fee was sized for.
    """
    dummy_signature = b"\x00" * MAX_DER_SIGNATURE_WITH_HASHTYPE
    return len(hashlock_script_sig(dummy_signature, public_key, secret, redeem_script))


def participant_key_matches_script(public_key: bytes, redeem_script: bytes) -> bool:
    """Whether this key's hash160 is one of the two the redeem script commits to.

    A cheap local check that turns the single most confusing on-chain failure
    into a refusal with a name. Signing with the wrong key produces
    `mandatory-script-verify-flag-failed` at OP_EQUALVERIFY, which is byte for
    byte the message a wrong preimage, a wrong branch selector or a wrong
    script produces -- so an operator reading it has four candidates and no way
    to choose between them.

    It searches for the hash160 anywhere in the script rather than parsing the
    branch, deliberately: this is a guard against an obviously wrong key, not a
    validator, and a parser here would be a second place that has to know the
    script's shape (rule 8). The script's own OP_EQUALVERIFY remains the
    authority on which branch the key is for.
    """
    return hash160(public_key) in redeem_script
