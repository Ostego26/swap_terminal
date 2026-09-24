"""Build and sign the two spends of an HTLC P2SH output, by hand.

Role: function level (the decisions -- serialization, sighash, scriptSig shape)
Reads: nothing. Every input is an argument; no RPC, no file, no environment.
Writes: nothing. It returns hex; broadcasting is the caller's.
Can move funds: no directly. It PRODUCES signed transactions, which the
       caller broadcasts, and on this harness's path those spend regtest
       coins only.
Mainnet-safe: yes in the sense that it opens nothing -- it cannot reach a
       chain. What it returns would be perfectly valid on mainnet if somebody
       fed it mainnet inputs, so treat it as a signing library, not as a safe
       one.

WHY THE HARNESS BUILDS THESE ITSELF, WHICH IS THE OPPOSITE OF WHAT THE
VERIFICATION RULE NORMALLY ASKS FOR.

CLAUDE.md is explicit: run the real script, not a paraphrase of its logic. For
the redeem branch this harness obeys that literally -- step 7 calls the real
`BTCClient.redeem_contract` / `LTCClient.redeem_contract`. For the REFUND
branch there is nothing to call. Grepped across the whole tree on 2026-09-24:

    $ grep -rn "def .*refund" --include=*.py
    (no match in any client)

No `refund_contract` exists on BTCClient, LTCClient or GRCClient, and
modules/atomic_swapper.py never refunds. The refund branch of every contract
this repository builds has therefore never been exercised by any code in this
repository, because no code in this repository can exercise it. That is the
finding, and the only way to test the SCRIPT the real builder produced is to
bring a spender.

So what is under test here is `modules/atomic_htlc_scripts.build_htlc_redeem_script`'s
output -- the redeem script, the locktime encoding, the two branches -- and
this module is the instrument, not the subject. Every place the harness uses
it, the screen says so.

WHY THE WALLET CANNOT SIGN THESE, WHICH IS ALSO WHY STEP 7 IS EXPECTED TO FAIL.

`signrawtransactionwithwallet` and `signrawtransactionwithkey` solve a
scriptPubKey by pattern: they recognize P2PKH, P2SH wrapping a recognized
form, multisig, and the segwit variants. An HTLC redeem script is
`OP_IF ... OP_ELSE ... OP_ENDIF`, which matches none of them, so the solver
returns non-standard and the signing call reports `complete: false` with no
scriptSig it could construct. There is no argument either RPC takes that says
"take the OP_IF branch and here is the preimage". A conditional script has to
be satisfied by an assembled scriptSig, which is what `redeem_script_sig()`
and `refund_script_sig()` below produce.

THE TWO SCRIPTSIGS, AND WHY THEIR ORDER IS WHAT IT IS.

P2SH spending evaluates the scriptSig, then pops its LAST push as the redeem
script and executes that against what remains on the stack. So the pushes
before the redeem script are, bottom to top, exactly what the redeem script
expects to find.

  redeem branch:  <sig> <pubkey> <preimage> OP_1 <redeemScript>
     OP_IF pops the OP_1 and takes the true branch. OP_SHA256 then consumes
     the preimage, OP_EQUALVERIFY compares it against the pushed secret_hash,
     and OP_DUP OP_HASH160 ... OP_CHECKSIG consumes the pubkey and the sig.

  refund branch:  <sig> <pubkey> OP_0 <redeemScript>
     OP_IF pops the OP_0 -- an empty byte string, which is false -- and takes
     the OP_ELSE branch, where the locktime is pushed, CHECKLOCKTIMEVERIFY
     compares it against the transaction's nLockTime, OP_DROP clears it, and
     the same DUP/HASH160/CHECKSIG pair runs against the refund key.

TWO TRANSACTION FIELDS THE REFUND DEPENDS ON, AND BOTH ARE EASY TO GET WRONG.

  nSequence must not be 0xFFFFFFFF. CHECKLOCKTIMEVERIFY fails immediately if
  the spending input's sequence is final, regardless of heights -- that is how
  BIP65 makes the opcode meaningful. SEQUENCE_NON_FINAL below is 0xFFFFFFFE,
  the conventional value: non-final for locktime purposes, and above the
  BIP125 replaceability threshold so the transaction is not signalling RBF.

  nLockTime must be at least the script's locktime, AND the transaction must
  be final for the block it would go into. Those are two different checks in
  two different places, and the harness exercises them separately in step 8:
  a transaction with nLockTime equal to the script's locktime is rejected by
  the MEMPOOL as non-final while the tip is short of it, and a transaction
  with nLockTime set to the current tip is final enough to be evaluated and is
  then rejected by CHECKLOCKTIMEVERIFY itself. Only the second proves the
  opcode is doing anything.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from decimal import Decimal

from regtest.keys import RegtestKey, double_sha256

# Non-final sequence. See the module docstring: CLTV fails outright on a final
# input, so a refund built with 0xFFFFFFFF fails for a reason that has nothing
# to do with the locktime, and reads on screen as if the timelock were wrong.
SEQUENCE_NON_FINAL = 0xFFFFFFFE
SIGHASH_ALL = 0x01
TX_VERSION = 2

OP_0 = b"\x00"
OP_1 = b"\x51"

# Opcode bytes used when disassembling a scriptSig for the operator, so the
# harness can print what it actually built rather than a hex blob alone.
_OPCODE_NAMES = {0x00: "OP_0", 0x51: "OP_1", 0x4C: "OP_PUSHDATA1", 0x4D: "OP_PUSHDATA2", 0x4E: "OP_PUSHDATA4"}

SATOSHIS_PER_COIN = 100_000_000
PUSHDATA1_BOUNDARY = 0x4C
PUSHDATA2_BOUNDARY = 0xFF
PUSHDATA4_BOUNDARY = 0xFFFF
# Compact-size boundaries, named rather than spelled inline so that the three
# comparisons in varint() read as the protocol's own thresholds.
VARINT_1BYTE_MAX = 0xFC
VARINT_2BYTE_MAX = 0xFFFF
VARINT_4BYTE_MAX = 0xFFFFFFFF


def push_data(data: bytes) -> bytes:
    """Push `data` onto the script stack with the smallest push opcode.

    THERE IS A SECOND COPY OF THIS, and rule 8 requires each to name the other:
    `modules/atomic_htlc_scripts.build_htlc_redeem_script` has a nested
    `push_data` with identical boundaries. They are not merged because that one
    is inside the function that DECIDES the redeem script -- fund-path code
    (rule 16) -- and extracting it would be a change to the fund path made by a
    harness that is supposed to be measuring it. If either changes, change both.
    """
    length = len(data)
    if length < PUSHDATA1_BOUNDARY:
        return bytes([length]) + data
    if length <= PUSHDATA2_BOUNDARY:
        return b"\x4c" + bytes([length]) + data
    if length <= PUSHDATA4_BOUNDARY:
        return b"\x4d" + struct.pack("<H", length) + data
    return b"\x4e" + struct.pack("<I", length) + data


def varint(value: int) -> bytes:
    """Bitcoin's compact size encoding, used for counts and script lengths.

    NOT a script number. The two were confused in this repository once already
    -- the locktime was pushed into the redeem script with a compact-size
    encoder, so a requested height of 500,000 was enforced as 128,000,254 and
    every refund branch was unspendable. See
    modules/atomic_htlc_scripts.encode_script_number for the measurement. This
    one is correct HERE because a transaction's field lengths genuinely are
    compact size.
    """
    if value <= VARINT_1BYTE_MAX:
        return bytes([value])
    if value <= VARINT_2BYTE_MAX:
        return b"\xfd" + struct.pack("<H", value)
    if value <= VARINT_4BYTE_MAX:
        return b"\xfe" + struct.pack("<I", value)
    return b"\xff" + struct.pack("<Q", value)


def coins_to_satoshis(amount: str | float) -> int:
    """Whole satoshis from a coin amount, via Decimal-safe string arithmetic.

    Takes the amount as a string wherever the caller has one, because
    `int(0.1 * 100_000_000)` is 10000000 on some values and 9999999 on others,
    and an output that is one satoshi short of what the harness asserts is a
    failure nobody will read as a float problem.
    """
    return int((Decimal(str(amount)) * SATOSHIS_PER_COIN).to_integral_value())


@dataclass(frozen=True)
class Outpoint:
    """The one input every spend in this harness has: the contract output."""

    txid: str
    vout: int
    value_satoshis: int


def _serialize_input(outpoint: Outpoint, script_sig: bytes) -> bytes:
    # txids are printed big-endian and serialized little-endian. Getting this
    # backwards produces a transaction that references an outpoint nobody has,
    # and the node says "bad-txns-inputs-missingorspent" -- which reads like
    # the contract was already spent.
    return (
        bytes.fromhex(outpoint.txid)[::-1]
        + struct.pack("<I", outpoint.vout)
        + varint(len(script_sig))
        + script_sig
        + struct.pack("<I", SEQUENCE_NON_FINAL)
    )


def serialize_transaction(
    outpoint: Outpoint,
    script_sig: bytes,
    outputs: list[tuple[int, bytes]],
    locktime: int,
) -> bytes:
    """One-input, N-output legacy transaction. No witness, because P2SH-HTLC has none."""
    body = struct.pack("<i", TX_VERSION)
    body += varint(1) + _serialize_input(outpoint, script_sig)
    body += varint(len(outputs))
    for value_satoshis, script_pubkey in outputs:
        body += struct.pack("<q", value_satoshis) + varint(len(script_pubkey)) + script_pubkey
    body += struct.pack("<I", locktime)
    return body


def legacy_sighash(
    outpoint: Outpoint,
    script_code: bytes,
    outputs: list[tuple[int, bytes]],
    locktime: int,
) -> bytes:
    """The SIGHASH_ALL digest for the single input, pre-segwit rules.

    The scriptSig position is filled with the SCRIPT CODE -- for a P2SH input
    that is the redeem script itself, not the P2SH scriptPubKey. Signing over
    the scriptPubKey instead produces a signature that verifies against nothing
    and fails as `Signature must be zero for failed CHECK(MULTI)SIG` or a bare
    script failure, with no hint about which of the two scripts was wrong.
    """
    preimage = serialize_transaction(outpoint, script_code, outputs, locktime)
    preimage += struct.pack("<I", SIGHASH_ALL)
    return double_sha256(preimage)


def _signature_for(key: RegtestKey, digest: bytes) -> bytes:
    """DER signature plus the one-byte sighash type, which is what goes on the stack."""
    return key.sign_digest(digest) + bytes([SIGHASH_ALL])


def redeem_script_sig(signature: bytes, key: RegtestKey, secret: bytes, redeem_script: bytes) -> bytes:
    """<sig> <pubkey> <preimage> OP_1 <redeemScript> -- takes the hashlock branch.

    `secret` is the preimage. It is on the stack because that is the entire
    point of the branch, and it becomes public the instant this transaction is
    relayed -- which is why the chain-safety rules forbid revealing one before
    the counterparty's leg is funded. This harness only ever builds one against
    a regtest contract it funded itself, and never prints it: see
    regtest.console.redact.
    """
    return (
        push_data(signature)
        + push_data(key.public_key)
        + push_data(secret)
        + OP_1
        + push_data(redeem_script)
    )


def refund_script_sig(signature: bytes, key: RegtestKey, redeem_script: bytes) -> bytes:
    """<sig> <pubkey> OP_0 <redeemScript> -- takes the timelock branch."""
    return push_data(signature) + push_data(key.public_key) + OP_0 + push_data(redeem_script)


def build_branch_spend(  # noqa: PLR0913, PLR0917 -- checked: these seven ARE the transaction. Six of them appear once each in the body and none can be defaulted: the outpoint, the script being satisfied, the key, the destination, the fee and the nLockTime are independent inputs to one signature, and `secret` is what selects the branch. Bundling them into a dataclass would add a type without removing an argument, and would put the branch selection somewhere other than the call site that makes it.
    outpoint: Outpoint,
    redeem_script: bytes,
    key: RegtestKey,
    destination_script: bytes,
    fee_satoshis: int,
    locktime: int,
    secret: bytes | None,
) -> tuple[str, bytes]:
    """Sign a spend of the contract output down one of its two branches.

    Args:
        outpoint: the funded contract output, with its value in satoshis.
        redeem_script: exactly the bytes the REAL builder returned. This is the
            thing under test; nothing here reconstructs or validates it.
        key: the participant key for the redeem branch, the refund key for the
            refund branch. Passing the wrong one produces a script failure at
            OP_EQUALVERIFY on the hash160, which the harness reports verbatim.
        destination_script: where the coins go -- a P2PKH scriptPubKey.
        fee_satoshis: subtracted from the input value. Regtest has no fee
            market, so this exists to keep the transaction above the minimum
            relay feerate, not to compete for space.
        locktime: the transaction's nLockTime. For a refund it must be at least
            the script's locktime AND no greater than the current tip; for a
            redeem it is irrelevant to the script and is set to 0.
        secret: the preimage for the redeem branch, None for the refund branch.
            Which branch is taken is decided by whether this is None, so there
            is exactly one place that decision lives.

    Returns:
        (raw transaction hex, the scriptSig bytes) -- the scriptSig is returned
        separately so the harness can disassemble and print it beside the
        redeem script when a spend is refused (rule 14: state what the number
        means, next to the number).
    """
    value_out = outpoint.value_satoshis - fee_satoshis
    if value_out <= 0:
        raise ValueError(
            f"fee {fee_satoshis} satoshis is at or above the contract value {outpoint.value_satoshis}; "
            "nothing would be left to send"
        )
    outputs = [(value_out, destination_script)]
    digest = legacy_sighash(outpoint, redeem_script, outputs, locktime)
    signature = _signature_for(key, digest)
    if secret is None:
        script_sig = refund_script_sig(signature, key, redeem_script)
    else:
        script_sig = redeem_script_sig(signature, key, secret, redeem_script)
    raw = serialize_transaction(outpoint, script_sig, outputs, locktime)
    return raw.hex(), script_sig


def describe_script_sig(script_sig: bytes) -> str:
    """Disassemble a scriptSig into pushes and opcodes, for the operator.

    Deliberately shallow: it names each element and its length rather than
    trying to interpret it, because the question this answers on screen is
    "how many things were pushed, and was the preimage one of them" -- which is
    exactly the question step 7 exists to settle. Data pushes longer than 8
    bytes are abbreviated to their first and last four bytes so that a redeem
    script does not fill a terminal, and because a preimage must never be
    printed in full (chain-safety rules) even though this harness's own
    scriptSig is the one place it legitimately appears.
    """
    parts: list[str] = []
    index = 0
    while index < len(script_sig):
        opcode = script_sig[index]
        index += 1
        if opcode == 0x00 or opcode >= PUSHDATA1_BOUNDARY:
            name = _OPCODE_NAMES.get(opcode, f"OP_{opcode:02X}")
            if opcode in (0x4C, 0x4D, 0x4E):
                width = {0x4C: 1, 0x4D: 2, 0x4E: 4}[opcode]
                length = int.from_bytes(script_sig[index : index + width], "little")
                index += width
                parts.append(f"{name}[{length}]")
                index += length
            else:
                parts.append(name)
            continue
        data = script_sig[index : index + opcode]
        index += opcode
        rendered = data.hex() if len(data) <= 8 else f"{data[:4].hex()}..{data[-4:].hex()}"  # noqa: PLR2004 -- checked: 8 is the width at which a hex blob stops being readable in a terminal column, not a protocol constant
        parts.append(f"push[{len(data)}]={rendered}")
    return " ".join(parts) if parts else "(none: empty scriptSig)"
