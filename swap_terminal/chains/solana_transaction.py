"""Solana's legacy transaction wire format, written out byte by byte.

Role: function level (the bottom of CLAUDE.md rule 10's stack -- every name
      here is a pure function of its arguments)
Reads: nothing. No socket, no file, no environment, no clock, NO KEY. The
      signature arrives as 64 bytes from whoever already had one.
Writes: nothing
Can move funds: NOT BY ITSELF, AND IT IS STILL ON THE MONEY PATH -- read the
      rest of this field rather than the first two words. The split from
      chains/solana_signing.py is where "cannot sign" stops being a promise and
      becomes a property: this module cannot sign,
      because it never sees a secret. It LAYS OUT the bytes a signature is
      computed over, which makes it fund-ADJACENT in the way rule 16 means --
      a wrong byte here sends the right lamports to the wrong account, or
      produces a transaction the cluster rejects after a fee was claimed.
Mainnet-safe: yes. It cannot reach a cluster even if asked to, and it names no
      endpoint.

=============================================================================
WHY THIS IS HAND-WRITTEN AT ALL
=============================================================================

`solders` and `solana-py` are NOT installed on this host and are not
dependencies of this repository -- measured 2026-10-02, both
`import solders` and `import solana` raise ModuleNotFoundError. The two
libraries that ARE present are PyNaCl 1.6.2 (ed25519) and base58 2.1.1, so
ed25519 signing is available and the transaction FORMAT is the part that had to
be written here.

That is the same trade chains/solana_address.py made for its two derivations,
and it records the argument: adding a compiled Rust extension to a host that
holds wallet credentials, to lay out 150 bytes, is a worse trade than code that
is tested. The difference is that an address derivation is checkable by
comparing one string; a transaction is checkable only against an independent
implementation of the same format. So one was obtained.

=============================================================================
HOW THE BYTE LAYOUT WAS VERIFIED (CLAUDE.md rule 17: run the thing that would
show it false) -- AND WHAT THAT STILL DOES NOT ESTABLISH
=============================================================================

MEASURED 2026-10-02, against `@solana/web3.js` 1.x installed into a throwaway
directory outside this checkout. That reference was not chosen for convenience:
it is THE SAME LIBRARY the Node bridge in swap_terminal/grc-sol-swap uses to
sign and broadcast real SOL transfers (server.js's sendSolPayout calls
SystemProgram.transfer and sendAndConfirmTransaction), so agreement with it is
agreement with the implementation that has actually moved money on this
project's behalf.

  60 random vectors        random payer seed, random destination, random
                           blockhash, lamports across 1 / 127 / 128 / 16383 /
                           16384 / 5000 / 650240 / 1e9. For each one, THIS
                           module's compile_transfer_message() and
                           wire_transaction() were compared byte for byte
                           against web3.js's Transaction.serializeMessage() and
                           Transaction.serialize().
                           **0 mismatches on the message. 0 on the signed wire
                           transaction.** The signatures agree too, which is a
                           second fact: ed25519 is deterministic, so PyNaCl and
                           tweetnacl producing the same 64 bytes over the same
                           message is a check on the key derivation as well.

  the reverse direction    web3.js's `Transaction.from()` parsed all 60 of the
                           PYTHON-produced wire transactions, and
                           `verifySignatures()` returned true for every one --
                           the reference implementation accepting our bytes as
                           a validly signed transaction, not merely producing
                           similar ones. Destination, fee payer, blockhash,
                           instruction index and lamports were read back out of
                           its parse and compared against the inputs. 60/60.

  the lengths              a one-signature native transfer is exactly 215 bytes
                           on the wire and its message is 150. Both fall out of
                           the field sizes below and both were confirmed on
                           every reference vector.

  a two-byte compact-u16   the only multi-byte array length REACHABLE inside
                           Solana's 1232-byte packet limit is an instruction's
                           data length: 130 transfer instructions would need
                           2344 bytes and web3.js refuses to serialize them
                           (measured -- ERR_OUT_OF_RANGE at offset 1232). So
                           the two-byte case was obtained from a reference
                           transaction carrying 200 bytes of instruction data,
                           where web3.js wrote `c801`, and
                           tests/test_solana_transaction.py pins both
                           directions against it.

tests/test_solana_transaction.py carries DETERMINISTIC reference vectors
(fixed seeds, fixed blockhash) taken from that run, so the cross-check is
re-runnable by anybody with the reference library and does not depend on this
file being re-read. That is the pattern chains/solana_address.py established
with solders: the reference establishes the vectors, the test pins them.

WHAT NONE OF THAT ESTABLISHES, said plainly because it is the expensive gap:
**no transaction built here has ever been broadcast.** api.devnet.solana.com
answers 403 at this container's proxy -- re-measured 2026-10-02, `CONNECT
tunnel failed, response 403` -- so there is no run in which a cluster accepted
one of these. Agreement with web3.js is agreement about a FORMAT; whether the
runtime accepts the transaction (fees, rent, a live blockhash) is a different
question and only a broadcast answers it. chains/solana.py's payout path is
labeled a PROPOSAL for exactly that reason (rule 16: a fix you cannot test
here is a proposal and not a fix).

=============================================================================
THE FORMAT, AND THE FOUR PLACES IT IS EASY TO GET WRONG
=============================================================================

A legacy transaction is:

    compact-u16 count, then that many 64-byte signatures
    the MESSAGE:
        3 bytes of header -- required signatures, readonly SIGNED accounts,
                             readonly UNSIGNED accounts
        compact-u16 count, then that many 32-byte account keys
        32 bytes of recent blockhash
        compact-u16 count, then that many instructions, each:
            1 byte  program id INDEX into the account keys
            compact-u16 count, then that many 1-byte account INDEXES
            compact-u16 count, then that many bytes of instruction data

  compact-u16 (shortvec)   seven bits per byte, little end first, high bit set
                           to mean "another byte follows". 0..127 is one byte,
                           which is every length this module writes in
                           production (three keys, one instruction, two account
                           indexes, twelve bytes of data). The multi-byte branch
                           of the DECODER is therefore never exercised by our
                           own artifacts, which is why it is pinned against a
                           reference vector instead of against our own output.

  the account ORDER        is not cosmetic and is not alphabetical. Keys are
                           ordered writable-signers, readonly-signers,
                           writable-non-signers, readonly-non-signers, and the
                           FEE PAYER IS FIRST. The header counts the groups
                           rather than naming them, so the order and the three
                           header bytes have to agree -- get it wrong and the
                           runtime reads a different account as the payer, or
                           refuses the transaction after a fee was claimed. For
                           a native transfer the answer is exactly
                           [payer, destination, system program] with a header
                           of (1, 0, 1): one signature required, no readonly
                           signed accounts, and the system program is the one
                           readonly unsigned account.

  the instruction data     a u32 little-endian SystemInstruction discriminant
                           (2 = Transfer) followed by a u64 little-endian
                           lamport amount. Twelve bytes. Little-endian in both
                           halves, which is the byte order a big-endian habit
                           silently inverts -- 1 lamport written big-endian is
                           72,057,594,037,927,936 lamports read little-endian.

  the signature ORDER      signatures go in the same order as the required
                           signers, which for a one-signer transfer means the
                           single signature is the fee payer's. A transaction
                           with the right signature in the wrong slot verifies
                           against nothing.
"""

from __future__ import annotations

import base58

from .solana_address import PUBKEY_BYTES, decode_address

# The System Program: 32 zero bytes, which base58-encodes to thirty-two '1's.
# Spelled as the address rather than as `bytes(32)` because the address is what
# an operator reads in an explorer and what every other Solana client prints,
# and the test asserts the two are the same thing rather than trusting this
# comment.
SYSTEM_PROGRAM_ID = "11111111111111111111111111111111"

# SystemInstruction::Transfer. The enum is serialized as a u32, not a u8:
# 0 = CreateAccount, 1 = Assign, 2 = Transfer. A u8 discriminant would write
# one byte where the runtime reads four and the instruction would decode as
# something else entirely.
SYSTEM_TRANSFER_INSTRUCTION = 2

#: Bytes in an ed25519 signature, and in a blockhash. Named separately from
#: PUBKEY_BYTES even though a blockhash is also 32, because they are different
#: things that happen to be the same size -- and a reader checking an offset
#: needs to see which quantity each 32 is.
SIGNATURE_BYTES = 64
BLOCKHASH_BYTES = 32

#: The message header is three bytes and a native transfer's is exactly this:
#: one required signature, no readonly SIGNED accounts, one readonly unsigned
#: account (the system program). Named as a tuple so the serializer and the
#: parser read the SAME three numbers -- two spellings of a header is rule 8's
#: bug with a delay on it, and the delay here ends at a cluster.
MESSAGE_HEADER_BYTES = 3
TRANSFER_MESSAGE_HEADER = (1, 0, 1)

#: Three keys (payer, destination, system program) and twelve bytes of
#: instruction data (u32 discriminant, u64 lamports).
TRANSFER_ACCOUNT_KEYS = 3
TRANSFER_DATA_BYTES = 12

#: The exact sizes of a one-signature native transfer, derived from the layout
#: above rather than typed: 1 + 64 signatures, 3 header, 1 + 3*32 keys, 32
#: blockhash, 1 + (1 + 1 + 2 + 1 + 12) instructions.
TRANSFER_MESSAGE_BYTES = (
    MESSAGE_HEADER_BYTES + 1 + TRANSFER_ACCOUNT_KEYS * PUBKEY_BYTES + BLOCKHASH_BYTES
    + 1 + (1 + 1 + 2 + 1 + TRANSFER_DATA_BYTES)
)
TRANSFER_WIRE_BYTES = 1 + SIGNATURE_BYTES + TRANSFER_MESSAGE_BYTES

#: The largest value a compact-u16 may carry. Every array length in a Solana
#: transaction is a u16, so a caller handing this a larger number has a bug
#: rather than a long array, and it is refused instead of truncated.
COMPACT_U16_MAX = 0xFFFF

#: A lamport amount is a u64. Refused above it rather than wrapped: an amount
#: that silently wraps is an amount nobody wrote.
U64_MAX = 0xFFFFFFFFFFFFFFFF

#: Solana's packet limit. A transaction larger than this is dropped by the
#: network, not merely rejected by a node. Nothing this module builds comes
#: close (215 bytes) and the check is here so that a future caller adding
#: instructions finds out here rather than from a silent drop.
PACKET_LIMIT_BYTES = 1232


class SolanaTransactionError(ValueError):
    """A transaction could not be laid out, or did not parse back as expected.

    A ValueError subclass for the reason SolanaAddressError gives: nothing in
    this module talks to a network, so every failure is a statement about the
    ARGUMENTS and never about reachability. A caller can never mistake one of
    these for "the cluster said no".
    """


def encode_compact_u16(value: int) -> bytes:
    """Solana's shortvec length prefix: seven bits per byte, little end first.

    The high bit of each byte means "another byte follows", so 0..127 is one
    byte, 128..16383 is two, and the rest of the u16 range is three. Written
    out rather than imported because there is nothing to import it from on this
    host (see the module docstring), and pinned against a reference vector for
    the two-byte case because our own transactions never produce one.
    """
    if not isinstance(value, int) or isinstance(value, bool):
        raise SolanaTransactionError(f"a compact-u16 length must be an integer, got {value!r}")
    if value < 0 or value > COMPACT_U16_MAX:
        raise SolanaTransactionError(
            f"{value} is not a compact-u16 length: every array length in a Solana transaction is a u16, "
            f"so the range is 0..{COMPACT_U16_MAX}. Refused rather than truncated."
        )
    out = bytearray()
    remaining = value
    while True:
        chunk = remaining & 0x7F
        remaining >>= 7
        if remaining == 0:
            out.append(chunk)
            return bytes(out)
        out.append(chunk | 0x80)


def decode_compact_u16(raw: bytes, offset: int = 0) -> tuple[int, int]:
    """Read one shortvec length. Returns (value, offset just past it).

    Returns the new offset rather than the number of bytes consumed, because
    every caller is walking a buffer and would otherwise add it themselves at
    four separate sites -- which is where an off-by-one in a parser lives.
    """
    value = 0
    shift = 0
    cursor = offset
    while True:
        if cursor >= len(raw):
            raise SolanaTransactionError(
                f"a compact-u16 length runs past the end of the buffer at offset {offset} "
                f"(length {len(raw)}). The transaction is truncated."
            )
        byte = raw[cursor]
        cursor += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, cursor
        shift += 7
        if shift > 14:  # noqa: PLR2004 -- checked: 14 is two full 7-bit groups; a u16 cannot need a fourth byte.
            raise SolanaTransactionError(
                f"a compact-u16 at offset {offset} did not terminate within three bytes, so it is not a "
                f"u16 length. The buffer is not a Solana transaction."
            )


def transfer_instruction_data(lamports: int) -> bytes:
    """The twelve bytes of a SystemProgram.transfer instruction.

    u32 little-endian discriminant (2) then u64 little-endian lamports. Both
    little-endian; see the module docstring for what a big-endian habit does to
    the amount.

    REFUSES ZERO. A zero-lamport transfer is a valid Solana transaction that
    costs a fee and delivers nothing, and it would be written into `payouts` as
    a broadcast payout -- the same refusal chains/xrp.py makes about a
    zero-drop Payment, for the same reason.
    """
    if not isinstance(lamports, int) or isinstance(lamports, bool):
        raise SolanaTransactionError(f"lamports must be an integer, got {lamports!r}")
    if lamports <= 0:
        raise SolanaTransactionError(
            f"{lamports} lamports is nothing to send. A zero- or negative-value transfer is not a "
            "payout; it is a fee paid to deliver nothing."
        )
    if lamports > U64_MAX:
        raise SolanaTransactionError(f"{lamports} lamports exceeds a u64, so it cannot be encoded")
    return SYSTEM_TRANSFER_INSTRUCTION.to_bytes(4, "little") + lamports.to_bytes(8, "little")


def compile_transfer_message(payer: str, destination: str, lamports: int, recent_blockhash: str) -> bytes:
    """The MESSAGE bytes of a native SOL transfer -- what a signature is over.

    Exactly one instruction and exactly three account keys, in the one order
    the runtime reads as (fee payer, recipient, system program). The header is
    (1, 0, 1) and the module docstring says at length why those three numbers
    and that order are one decision rather than two.

    REFUSES A SELF-TRANSFER, which is the case the ORDER cannot survive. If the
    destination equals the payer, a real Solana client de-duplicates the key
    list down to two entries and the account indexes shift -- so this layout
    would be wrong rather than merely pointless. It is also never a payout:
    paying the hot wallet from the hot wallet moves nothing and costs a fee.
    Refused here rather than handled, because handling it would mean carrying a
    second layout for a case that must not happen.
    """
    payer_raw = decode_address(payer)
    destination_raw = decode_address(destination)
    if payer_raw == destination_raw:
        raise SolanaTransactionError(
            f"the payer and the destination are the same account ({payer}), so there is nothing to "
            "transfer. Refused: a self-transfer de-duplicates the account key list, which shifts every "
            "instruction index, and it moves no money while still paying a fee."
        )
    blockhash_raw = base58.b58decode(recent_blockhash.strip() if isinstance(recent_blockhash, str) else b"")
    if len(blockhash_raw) != BLOCKHASH_BYTES:
        raise SolanaTransactionError(
            f"a recent blockhash is {BLOCKHASH_BYTES} bytes; {recent_blockhash!r} decodes to "
            f"{len(blockhash_raw)}. A transaction signed over a malformed blockhash cannot be accepted "
            "by any cluster."
        )
    data = transfer_instruction_data(lamports)
    keys = [payer_raw, destination_raw, decode_address(SYSTEM_PROGRAM_ID)]
    message = bytearray()
    # The header. One required signature (the payer's), no readonly SIGNED
    # accounts, one readonly unsigned account (the system program).
    message += bytes(TRANSFER_MESSAGE_HEADER)
    message += encode_compact_u16(len(keys))
    for key in keys:
        message += key
    message += blockhash_raw
    message += encode_compact_u16(1)
    # Program id index 2 -- the system program, the third key. The instruction
    # touches keys 0 and 1: the payer (writable, signer) and the destination
    # (writable). The program itself is NOT in its own account list.
    message += bytes([2])
    message += encode_compact_u16(2) + bytes([0, 1])
    message += encode_compact_u16(len(data)) + data
    out = bytes(message)
    if len(out) != TRANSFER_MESSAGE_BYTES:
        raise SolanaTransactionError(
            f"the compiled message is {len(out)} bytes and a native transfer message is "
            f"{TRANSFER_MESSAGE_BYTES}. Refusing to hand this to a signer: a length this code cannot "
            "explain means the layout above and the constant disagree."
        )
    return out


def wire_transaction(signature: bytes, message: bytes) -> bytes:
    """The broadcastable transaction: one signature, then the message it signs.

    One signature rather than a list, because a native transfer requires
    exactly one signer and a list would invite a caller to pass them in an
    order the header does not describe. The module docstring states the rule
    that makes this a safety property: signatures appear in the same order as
    the required signers, so the one signature here IS the fee payer's.
    """
    if len(signature) != SIGNATURE_BYTES:
        raise SolanaTransactionError(
            f"an ed25519 signature is {SIGNATURE_BYTES} bytes, got {len(signature)}. Nothing was "
            "assembled."
        )
    out = bytes(encode_compact_u16(1)) + bytes(signature) + bytes(message)
    if len(out) > PACKET_LIMIT_BYTES:
        raise SolanaTransactionError(
            f"the transaction is {len(out)} bytes, over Solana's {PACKET_LIMIT_BYTES}-byte packet limit, "
            "so the network would DROP it rather than reject it. Nothing was broadcast."
        )
    return out


def _parse_prologue(raw: bytes) -> tuple[list[str], bytes, int, int]:
    """The signature, header, account keys and blockhash. Returns (keys, blockhash, message offset, cursor).

    Split out of parse_transfer_transaction() because that function was past
    ruff's complexity ceiling, and rule 12 is explicit that the fix is to
    extract the decision rather than raise the ceiling: each of these three
    helpers is now callable with a seeded buffer on its own.
    """
    signature_count, cursor = decode_compact_u16(raw, 0)
    if signature_count != 1:
        raise SolanaTransactionError(
            f"this transaction carries {signature_count} signatures and a native transfer carries one. "
            "Refusing to read it as one."
        )
    if len(raw[cursor : cursor + SIGNATURE_BYTES]) != SIGNATURE_BYTES:
        raise SolanaTransactionError("the transaction is truncated inside its signature")
    cursor += SIGNATURE_BYTES
    message_offset = cursor
    header = raw[cursor : cursor + MESSAGE_HEADER_BYTES]
    if len(header) != MESSAGE_HEADER_BYTES:
        raise SolanaTransactionError("the transaction is truncated inside its message header")
    cursor += MESSAGE_HEADER_BYTES
    if tuple(header) != TRANSFER_MESSAGE_HEADER:
        raise SolanaTransactionError(
            f"the message header is {tuple(header)} and a native transfer's is {TRANSFER_MESSAGE_HEADER} "
            "-- one required signature, no readonly signed accounts, one readonly unsigned account. A "
            "different header describes a different set of signers."
        )
    key_count, cursor = decode_compact_u16(raw, cursor)
    if key_count != TRANSFER_ACCOUNT_KEYS:
        raise SolanaTransactionError(
            f"the message names {key_count} accounts and a native transfer names "
            f"{TRANSFER_ACCOUNT_KEYS} (payer, destination, system program)."
        )
    keys = []
    for _index in range(key_count):
        key = raw[cursor : cursor + PUBKEY_BYTES]
        if len(key) != PUBKEY_BYTES:
            raise SolanaTransactionError("the transaction is truncated inside its account keys")
        keys.append(base58.b58encode(key).decode("ascii"))
        cursor += PUBKEY_BYTES
    blockhash_raw = raw[cursor : cursor + BLOCKHASH_BYTES]
    if len(blockhash_raw) != BLOCKHASH_BYTES:
        raise SolanaTransactionError("the transaction is truncated inside its recent blockhash")
    cursor += BLOCKHASH_BYTES
    return keys, blockhash_raw, message_offset, cursor


def _parse_only_instruction(raw: bytes, cursor: int) -> tuple[int, list[int], bytes]:
    """The one instruction, refusing anything that carries a second one.

    A SECOND INSTRUCTION IS THE DANGEROUS CASE, which is why it is refused
    rather than ignored: a transaction may carry any number of instructions and
    they all execute, so an extra one moves funds the plan the operator read
    never described.
    """
    instruction_count, cursor = decode_compact_u16(raw, cursor)
    if instruction_count != 1:
        raise SolanaTransactionError(
            f"the message carries {instruction_count} instructions and a native transfer carries one. "
            "Refusing to read it as one: every instruction in a transaction executes, so an extra one "
            "can move funds this plan never described."
        )
    if cursor >= len(raw):
        raise SolanaTransactionError("the transaction is truncated before its instruction")
    program_index = raw[cursor]
    cursor += 1
    account_count, cursor = decode_compact_u16(raw, cursor)
    account_indexes = list(raw[cursor : cursor + account_count])
    if len(account_indexes) != account_count:
        raise SolanaTransactionError("the transaction is truncated inside its instruction account list")
    cursor += account_count
    data_length, cursor = decode_compact_u16(raw, cursor)
    data = raw[cursor : cursor + data_length]
    if len(data) != data_length:
        raise SolanaTransactionError("the transaction is truncated inside its instruction data")
    cursor += data_length
    if cursor != len(raw):
        raise SolanaTransactionError(
            f"{len(raw) - cursor} trailing bytes follow the instruction. Refused: a transaction with "
            "bytes nothing accounts for is not the transaction this code built."
        )
    return program_index, account_indexes, data


def _interpret_transfer(keys: list[str], program_index: int, account_indexes: list[int], data: bytes) -> int:
    """The lamports this instruction moves, or a refusal naming what it is instead.

    Every refusal here is a case where the bytes parse perfectly and mean
    something OTHER than "pay the destination": the wrong program, the accounts
    reversed, or a different system instruction against the same two accounts.
    """
    if program_index >= len(keys) or keys[program_index] != SYSTEM_PROGRAM_ID:
        named = keys[program_index] if program_index < len(keys) else "an index past the key list"
        raise SolanaTransactionError(
            f"the instruction's program is key {program_index} ({named}), not the system program "
            f"{SYSTEM_PROGRAM_ID}. A native SOL transfer is a system program instruction and nothing else."
        )
    if account_indexes != [0, 1]:
        raise SolanaTransactionError(
            f"the instruction touches accounts {account_indexes} and a native transfer touches [0, 1] -- "
            "the payer and the destination, in that order. Reversed, it debits the recipient."
        )
    if len(data) != TRANSFER_DATA_BYTES:
        raise SolanaTransactionError(
            f"the instruction carries {len(data)} bytes of data and a transfer carries "
            f"{TRANSFER_DATA_BYTES} (u32 discriminant, u64 lamports)."
        )
    discriminant = int.from_bytes(data[:4], "little")
    if discriminant != SYSTEM_TRANSFER_INSTRUCTION:
        raise SolanaTransactionError(
            f"the instruction's discriminant is {discriminant} and Transfer is "
            f"{SYSTEM_TRANSFER_INSTRUCTION}. A different system instruction does a different thing to "
            "the same accounts -- 0 is CreateAccount and 1 is Assign."
        )
    return int.from_bytes(data[4:TRANSFER_DATA_BYTES], "little")


def parse_transfer_transaction(wire: bytes) -> dict:
    """Read a signed native transfer back out of its own bytes.

    THIS IS A CHECK ON OUR OWN ARTIFACT, NOT A GENERAL DECODER, and it is
    deliberately strict about the shape it accepts: exactly one signature,
    exactly three keys, exactly one instruction, the system program, the
    Transfer discriminant, twelve bytes of data. Anything else means the thing
    about to be broadcast is not the thing this code intended to build, which
    is the only question it exists to answer.

    WHY IT EXISTS AT ALL rather than trusting the serializer that just ran.
    CLAUDE.md's verification principle -- never accept "the code contains a
    check for X" as evidence X holds -- and rule 13's "verify the artifact, not
    the deploy". chains/solana_signing.py calls this on the bytes it is about
    to hand to sendTransaction and compares the result against the plan the
    operator read in the preview. A serializer defect that put the right
    lamports against the wrong key would otherwise be invisible until an
    explorer showed it.

    Returns payer, destination, lamports, blockhash and the signature, all as
    the strings and integers the plan carries, so the comparison is between
    like things rather than between bytes and a dict.
    """
    if not isinstance(wire, (bytes, bytearray)):
        raise SolanaTransactionError(f"a wire transaction is bytes, got {type(wire).__name__}")
    raw = bytes(wire)
    keys, blockhash_raw, message_offset, cursor = _parse_prologue(raw)
    program_index, account_indexes, data = _parse_only_instruction(raw, cursor)
    lamports = _interpret_transfer(keys, program_index, account_indexes, data)
    return {
        "signature": base58.b58encode(raw[1 : 1 + SIGNATURE_BYTES]).decode("ascii"),
        "payer": keys[0],
        "destination": keys[1],
        "program": keys[program_index],
        "lamports": lamports,
        "recent_blockhash": base58.b58encode(blockhash_raw).decode("ascii"),
        # The message bytes the signature is over, so a caller can verify the
        # signature against the parsed form rather than against the buffer it
        # happens to be holding.
        "message": raw[message_offset:],
    }
