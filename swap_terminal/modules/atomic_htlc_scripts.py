#!/usr/bin/env python3
"""Build HTLC redeem scripts and derive their TESTNET P2SH addresses.

Role: function level (the decisions -- script assembly and address derivation)
Reads: nothing at import any more. See "what was removed" below.
Writes: nothing
Can move funds: no directly -- it neither signs nor broadcasts. It DECIDES the
       script that locks them, which is worse: a wrong script cannot be
       undone once a contract is funded against it.
Mainnet-safe: NO, and structurally so. TESTNET_P2PKH_VERSION (0x6F) and
       TESTNET_P2SH_VERSION (0xC4) are hardcoded, so every address this module
       produces is a testnet address regardless of what is passed in --
       parse_and_reencode_as_testnet_p2pkh() will happily take a MAINNET
       address, strip its version byte and re-encode the same hash160 as
       testnet. There is no mainnet path here at all.

FIXED 2026-09-24: THE LOCKTIME WAS ENCODED AS A VARINT AND NOW IS NOT.

number_to_le_bytes() implemented Bitcoin's COMPACT SIZE (varint) encoding --
the one used for array lengths in the p2p protocol -- and its output was pushed
into the script as the operand of OP_CHECKLOCKTIMEVERIFY, which reads a
CScriptNum. The two encodings are not the same, and for every locktime a real
swap would use they did not agree. Measured by running the function before it
was deleted:

    locktime asked        bytes pushed      CLTV read        pushed now
        500,000           fe20a10700        128,000,254      20a107
        800,000           fe00350c00        204,800,254      00350c
      3,000,000           fec0c62d00        768,000,254      c0c62d
  1,735,689,600           fe80857467    444,336,537,854      80857467

The 0xfe prefix was the varint's "a 4-byte value follows" tag. CScriptNum has
no such tag, so it read the tag as the low byte of the number. Every value in
the third column is far beyond any height Bitcoin will reach, so the refund
branch of every contract this module built never became spendable: coins funded
into one were recoverable only through the redeem branch, and only by whoever
held the participant key.

encode_script_number() replaces it -- minimally-encoded little-endian, with a
zero pad byte when the top bit of the most significant byte is set. The old
function is DELETED rather than kept beside the new one (rule 2): the whole
tree was grepped for the name before removing it, and the only callers were
this module's script builder and the test file, both of which now name the new
one. Nothing else in Python, JavaScript, shell or documentation referenced it.

THIS FIX WAS NOT SHIPPED ALONE, AND MUST NOT BE READ AS IF IT WERE. Correcting
the encoder in isolation would have made things WORSE: modules/atomic_swapper.py
hardcoded `locktime=500000`, a height passed on BTC in 2017 and on LTC earlier,
so a correctly encoded 500000 is a refund that is claimable the instant the
contract is funded -- the initiator refunds their own leg and still redeems the
counterparty's. The same commit therefore added modules/htlc_timelock.py, which
derives a per-swap locktime from the chain tip with the initiator's lock
strictly longer than the participant's, and split the participant and refund
addresses that atomic_swapper.py passed as one value.

WHAT IS STILL NOT PROVEN, AND CANNOT BE PROVEN FROM HERE (rule 17). Neither
branch of a contract built with this encoder has been exercised on any chain.
The encoding is verified by round-tripping it through an independent CScriptNum
decoder and by disassembling the produced script
(tests/test_htlc_locktime_encoding.py); that is a measurement of the bytes, not
of a spend. The only honest proof is a testnet contract funded and then
REFUNDED after expiry -- the branch that runs when something has already gone
wrong -- and that needs a chain this machine does not have.

TWO MORE THINGS WORTH KNOWING BEFORE TRUSTING build_htlc_redeem_script().

parse_and_reencode_as_testnet_p2pkh() re-encodes ANY address as a P2PKH hash,
including a P2SH address and a P2WSH one. For P2WPKH that happens to be
correct, because the witness program IS hash160(pubkey). For a P2SH address the
21 decoded bytes are a SCRIPT hash, and re-encoding them as a P2PKH hash
produces a script branch that requires a signature from a key whose hash160
equals a script hash -- which nobody has. `run_swap_tests()` below passes
`2MwJ8Q735vzVkaL1SnfbGvefofrDnXgXbfY`, a testnet P2SH address, as a refund
address, so the case is not hypothetical.

And the script's OP_IF branch requires the preimage on the stack. UNTIL
2026-09-25 THIS PARAGRAPH SAID none of the three redeem_contract()
implementations "ever puts there -- see the divergence table in any of
modules/atomic_*_client.py", and BOTH HALVES OF THAT ARE NOW FALSE. All three
push it: modules/htlc_spend.hashlock_script_sig() lays out
<sig> <pubkey> <preimage> OP_1 <redeemScript> and
modules/htlc_rpc.build_hashlock_spend() signs it, one implementation for all
three clients. Verified by walking each redeem_contract()'s AST -- `secret`
appears in all three bodies and is passed by keyword in all three. And the
divergence table no longer carries that row: it reads
`redeem scriptSig  ---- modules/htlc_rpc.build_hashlock_spend, one
implementation ----`, so the sentence pointed a reader at a table that had
stopped saying it.

WHY THIS SENTENCE WAS WORTH FIXING AT ALL, given that the code was already
right. This is the authoritative file for what the script REQUIRES. An
operator holding a funded contract whose counterparty has already taken the
other leg reads it to learn whether they can redeem, is told the package
cannot, and waits out the timelock instead -- losing both legs, at the one
moment when the refund branch is not the safe default. Rule 16: a wrong
comment is a bug, and the sentence was the thing that was wrong.

WHAT WAS REMOVED FROM THIS FILE ON 2026-09-24 (rules 9 and 12).

  - `print("Before adding modules, Python search paths are:")` and
    `print(sys.path)`, twice. Debug output from someone's afternoon, executed
    by every program that imported this module, on import.
  - `sys.path.append(os.path.join(os.path.dirname(__file__), "modules"))`,
    which appended `<...>/modules/modules` -- a directory that does not exist.
    Verified by printing sys.path after the import.
  - A module-level `raise ValueError` if the SECRET_HASH environment variable
    was unset. That made `import modules.atomic_btc_client` FAIL unless an
    unrelated environment variable happened to be set, which is an import-time
    side effect (rule 12) that took the whole atomic-swap package down with
    it. The check moved into run_swap_tests(), which is the only thing that
    ever used the value.
  - Nine module-level globals read from the environment -- SERVER_LTC_REFUND_ADDR,
    SERVER_GRC_REFUND_ADDR, SERVER_BTC_REFUND_ADDR, SERVER_BTC_PARTICIPANT_ADDR,
    SERVER_BTC_HTLC_ADDR, PARTICIPANT_ADDRESS, BTC_REFUND_ADDRESS,
    LTC_REFUND_ADDRESS, GRC_REFUND_ADDRESS and LOCKTIME -- every one of them
    assigned and never read anywhere in the tree.
  - `load_dotenv()` at import, which went with them: a module that mutates
    os.environ when imported makes every later import order-dependent, and it
    no longer had anything to load for.
"""

import logging
import os
import struct
import sys

import base58
import bech32
from modules.htlc_timelock import ROLE_INITIATOR, contract_locktime, describe_locktime
from modules.utils import hash160

# No setLevel and no handler: a library module that forces DEBUG at import
# decides logging policy for every program that imports it (rule 12).
logger = logging.getLogger(__name__)

# Testnet version bytes. These are the reason this module's header says
# Mainnet-safe: NO -- there is no mainnet path, only these.
TESTNET_P2PKH_VERSION: bytes = b'\x6F'
TESTNET_P2SH_VERSION: bytes = b'\xC4'

# Base58Check payload length for a versioned hash160: 1 version byte + 20.
BASE58_VERSIONED_HASH160_LEN = 21
# A witness v0 keyhash program is a hash160, so 20 bytes.
WITNESS_V0_KEYHASH_LEN = 20

def encode_script_number(value: int) -> bytes:
    """Encode an integer the way Bitcoin script reads one (CScriptNum).

    Little-endian, minimally encoded, with a zero pad byte appended when the
    top bit of the most significant byte is set -- because script numbers are
    SIGN-AND-MAGNITUDE: that top bit is the sign, so 0x80 unpadded reads as
    -0 and 0xFFFF unpadded reads as a negative number. A locktime that reads
    negative makes OP_CHECKLOCKTIMEVERIFY fail the script outright, which on
    the refund branch means the refund is not merely late, it is impossible.
    Zero is the empty byte string, which is what an empty stack element is.

    WHY THIS REPLACED number_to_le_bytes(), WHICH IS NOW DELETED.

    That function emitted Bitcoin's COMPACT SIZE encoding -- the varint used
    for array lengths in the p2p protocol, with a leading tag byte saying how
    many bytes follow. CScriptNum has no tag, so the tag was read as the low
    byte of the number. Measured 2026-09-24 by running it:

        locktime asked   bytes it pushed   CLTV read      this function pushes
            500,000      fe20a10700        128,000,254    20a107
            800,000      fe00350c00        204,800,254    00350c
          3,000,000      fec0c62d00        768,000,254    c0c62d
      1,735,689,600      fe80857467    444,336,537,854    80857467 ... 0080857467

    Every value in the third column is a block height tens of thousands of
    years out, so the refund branch of every contract this module built was
    unspendable: the coins could only come back through the redeem branch, and
    only to whoever held the participant key.

    The last row is worth reading twice, because it is the pad byte earning its
    place: 1,735,689,600 is a unix TIMESTAMP (above LOCKTIME_THRESHOLD), its
    little-endian form is 80 85 74 67, and 0x67 has its top bit clear, so no
    pad is needed there. A value like 0x80000000 does need one, and the
    boundary cases are exercised in tests/test_htlc_locktime_encoding.py.

    Negative values raise rather than encode. Nothing in an HTLC wants one --
    CLTV rejects a negative operand -- so a negative arriving here is a caller
    bug, and encoding it would hide the bug inside a script that fails much
    later, on the branch that runs when something has already gone wrong.
    """
    if value < 0:
        raise ValueError(f"script numbers in an HTLC locktime are never negative, got {value}")
    if value == 0:
        return b""
    raw = bytearray()
    magnitude = value
    while magnitude:
        raw.append(magnitude & 0xFF)
        magnitude >>= 8
    # Sign-and-magnitude: a set top bit on the most significant byte would be
    # read as "negative", so pad with a zero byte to keep it positive.
    if raw[-1] & 0x80:
        raw.append(0x00)
    return bytes(raw)

def strip_address_prefix(address: str) -> str:
    """Remove URI scheme prefixes from an address."""
    if not address:
        raise ValueError("Address cannot be None or empty.")
    
    logger.debug(f"Stripping address prefix from: {address}")
    for prefix in ("bitcoin:", "litecoin:"):
        if address.lower().startswith(prefix):
            logger.debug("Prefix found, stripping it.")
            return address[len(prefix):]
    return address

def parse_and_reencode_as_testnet_p2pkh(address: str) -> str:
    """Parse a Bitcoin address (Base58Check or Bech32) and re-encode it as a testnet P2PKH address."""
    logger.debug(f"Re-encoding address: {address}")
    address = strip_address_prefix(address)
    raw = address.strip()

    # Attempt Base58Check decoding.
    try:
        logger.debug(f"Attempting Base58Check decode on address: {raw}")
        decoded = base58.b58decode_check(raw)
        if len(decoded) == BASE58_VERSIONED_HASH160_LEN:
            reencoded = base58.b58encode_check(TESTNET_P2PKH_VERSION + decoded[1:]).decode()
            logger.debug(f"Successfully re-encoded Base58 address: {reencoded}")
            return reencoded
    except Exception as e:  # noqa: BLE001 -- checked: this is a FORMAT PROBE, not an operation. Any failure to decode as base58check means "try bech32 next", and if that also fails the function raises ValueError rather than returning something the caller could mistake for an address.
        logger.debug(f"Base58 decode failed: {e}")
    
    # Fallback to Bech32 decoding.
    try:
        logger.debug(f"Attempting Bech32 decode on address: {raw}")
        # `hrp` is deliberately named and deliberately ignored, and that is a
        # finding rather than a style choice: the human-readable part is the
        # ONLY thing distinguishing a mainnet `bc1...`/`ltc1...` address from
        # a testnet `tb1...`/`tltc1...` one, and this function re-encodes both
        # to testnet without looking at it. Checking it is a fund-path change
        # (it would start rejecting addresses this accepts today), so it is
        # reported, not made.
        _hrp, data = bech32.bech32_decode(raw)
        if data:
            program = bech32.convertbits(data[1:], 5, 8, False)
            if program and len(program) == WITNESS_V0_KEYHASH_LEN:
                reencoded = base58.b58encode_check(TESTNET_P2PKH_VERSION + bytes(program)).decode()
                logger.debug(f"Successfully re-encoded Bech32 address to Base58: {reencoded}")
                return reencoded
    except Exception as e:  # noqa: BLE001 -- checked: same probe. Falling out of this block reaches the `raise ValueError` below, so an undecodable address is an error, never a silent pass-through.
        logger.debug(f"Bech32 decode failed: {e}")
    
    raise ValueError(f"Invalid address format: {address}")

def _extract_hash160_from_testnet_p2pkh(address: str) -> bytes:
    """Extract HASH160 from a testnet P2PKH address."""
    logger.debug(f"Extracting HASH160 from address: {address}")
    decoded = base58.b58decode_check(address)
    if len(decoded) == BASE58_VERSIONED_HASH160_LEN and decoded[0] == TESTNET_P2PKH_VERSION[0]:
        hash160_val = decoded[1:]
        logger.debug(f"Extracted hash160: {hash160_val.hex()}")
        return hash160_val
    raise ValueError("Not a valid testnet P2PKH address.")

def push_data(data: bytes) -> bytes:
    """Push `data` onto the script stack with the smallest push opcode that fits.

    LIFTED OUT OF `build_htlc_redeem_script` ON 2026-09-25, and the reason the
    old comment gave for leaving it nested no longer holds. It said extracting
    it "would be a change to the fund path made on behalf of a harness whose
    job is to measure that path, not to edit it" -- true while the only other
    caller was the harness. It is now called by modules/htlc_spend.py, which
    assembles the scriptSig that SPENDS this script on the real fund path, and
    a push encoder spelled twice inside one fund path is rule 8's failure with
    a delay on it: the scriptSig's pushes and the redeem script's pushes have
    to agree byte for byte or the P2SH hash does not match.

    THERE IS STILL A SECOND COPY, deliberately, and rule 8 requires each site
    to name the other: `regtest/txbuild.py::push_data` has identical boundaries.
    That one is NOT merged into this one, because regtest/txbuild.py is the
    harness's independent control spender -- the instrument that answers
    "is the SCRIPT sound, or is the CLIENT wrong?" If the instrument imported
    the thing it measures, a defect shared by both would make the control fail
    exactly when the client fails, and the harness would report the redeem
    SCRIPT as broken when the shared encoder was. The two are kept apart on
    purpose and held together by a test instead:
    tests/test_htlc_spend.py::test_the_two_push_encoders_agree_byte_for_byte.
    """
    length = len(data)
    if length < 0x4c:  # noqa: PLR2004 -- checked: OP_PUSHDATA1's boundary, a script opcode constant
        result = bytes([length]) + data
    elif length <= 0xff:  # noqa: PLR2004 -- checked: OP_PUSHDATA2's boundary, a script opcode constant
        result = b'\x4c' + bytes([length]) + data
    elif length <= 0xffff:  # noqa: PLR2004 -- checked: OP_PUSHDATA4's boundary, a script opcode constant
        result = b'\x4d' + struct.pack("<H", length) + data
    else:
        result = b'\x4e' + struct.pack("<I", length) + data
    # THE DATA ITSELF IS NOT LOGGED, and that changed on 2026-09-25 when this
    # function acquired its second caller.
    #
    # It used to log both `data.hex()` and `result.hex()` at DEBUG. That was
    # merely noisy while the only things it pushed were a secret HASH and two
    # hash160s -- all public by construction, all already in the script this
    # builds. The moment modules/htlc_spend.hashlock_script_sig() started using
    # it, one of the things it pushes is the PREIMAGE, and those two lines
    # printed it in full.
    #
    # That is the single most dangerous value in this tree, and CLAUDE.md's
    # chain-safety rules are absolute about it: "Never reveal a preimage. Not
    # in a log, not in a commit, not in a pasted diagnostic, not in an error
    # message. Log secret_hash, never secret." It is caught by
    # tests/test_htlc_spend.py::test_no_client_logs_the_preimage, which found
    # it -- the leak existed for the length of one test run and never reached
    # a chain.
    #
    # The LENGTH is kept, because that is what a reader diagnosing a push
    # boundary needs and it reveals nothing.
    logger.debug("pushed %d bytes as a %d-byte script element", length, len(result))
    return result


def script_to_p2sh_address(script: bytes) -> str:
    """Compute the testnet P2SH address corresponding to a redeem script."""
    script_hash = hash160(script)
    p2sh_addr = base58.b58encode_check(TESTNET_P2SH_VERSION + script_hash).decode()
    logger.debug(f"Computed P2SH address: {p2sh_addr}")
    return p2sh_addr


def p2sh_script_for(script: bytes) -> bytes:
    """The scriptPubKey that pays a P2SH: OP_HASH160 <20-byte script hash> OP_EQUAL.

    THE VERSION-INDEPENDENT NAME FOR A CONTRACT OUTPUT, and that is why it
    exists rather than the address `script_to_p2sh_address()` returns.

    Measured on regtest 2026-09-25: Bitcoin Core 28.1 returns
    `['address', 'asm', 'desc', 'hex', 'type']` in a decoded scriptPubKey and
    Litecoin Core 0.21.4 returns `['addresses', 'asm', 'hex', 'reqSigs',
    'type']`. Core deprecated `addresses` in 0.20 and removed it in 22.0, so
    code that locates its own contract output by comparing an address against
    `scriptPubKey.addresses` finds nothing on a modern node no matter how
    correctly the contract was funded -- which is exactly what
    `wait_for_tx_output()` and LTCClient.create_contract() did until
    2026-09-25. (That function lived in modules/utils.py then; it is
    modules/htlc_rpc.wait_for_tx_output() now, and matches on this.)

    `hex` is present on BOTH daemons and is the same bytes on both, and the two
    chains do not even agree on the base58 P2SH version byte (0xC4 on Bitcoin,
    printed as 0x3A by Litecoin), so the rendered address is the wrong key in
    two independent ways. Match on this.

    There is a second copy in regtest/steps.py::_p2sh_script_for, and rule 8
    requires each to name the other. That one belongs to the harness, which is
    kept independent of the code it measures on purpose -- see push_data above
    for the full reasoning. They are held together by
    tests/test_htlc_spend.py::test_the_harness_and_the_client_agree_on_the_p2sh_script.
    """
    script_hash = hash160(script)
    return b"\xa9" + bytes([len(script_hash)]) + script_hash + b"\x87"

def build_htlc_redeem_script(secret_hash: str | bytes,
                             participant_address: str,
                             refund_address: str,
                             locktime: int) -> bytes:
    """Build an HTLC redeem script.

    Args:
        secret_hash: SHA-256 of the preimage, hex or bytes. The PREIMAGE never
            appears here and must never be logged anywhere (CLAUDE.md's
            chain-safety rules); the hash is public by construction.
        participant_address: the COUNTERPARTY's address on this chain. Whoever
            controls it can take the coins by revealing the preimage.
        refund_address: YOUR address on this chain. Whoever controls it can
            take the coins back once the locktime has passed.
        locktime: an absolute block height (below 500,000,000) or a unix
            timestamp (at or above it), from modules/htlc_timelock.py. It is
            pushed as a CScriptNum, not as a varint -- see the module header
            for what happened when it was the other way round.

    Raises:
        ValueError: if locktime is not positive, or if the participant and
            refund addresses resolve to the SAME hash160.

    The two guards below are not defensive decoration; each pins a defect this
    file shipped. A locktime of 0 encodes to the empty byte string, which CLTV
    reads as the number zero -- the refund branch would then be spendable
    immediately, by anyone holding the refund key, from the moment of funding.
    And identical participant and refund hashes make both branches require the
    SAME key, which is what modules/atomic_swapper.py passed in all six of its
    swap directions until 2026-09-24: the counterparty could never claim with
    the preimage, so the contract was an expensive way to pay yourself.
    """
    logger.info("Building HTLC redeem script.")

    if locktime <= 0:
        raise ValueError(
            f"locktime must be a positive block height or unix timestamp, got {locktime!r}; "
            "zero encodes to an empty script number and makes the refund branch spendable immediately"
        )

    part = parse_and_reencode_as_testnet_p2pkh(participant_address)
    ref = parse_and_reencode_as_testnet_p2pkh(refund_address)
    logger.debug(f"Re-encoded participant address: {part}")
    logger.debug(f"Re-encoded refund address: {ref}")
    
    # Extract HASH160 values.
    p_hash = _extract_hash160_from_testnet_p2pkh(part)
    r_hash = _extract_hash160_from_testnet_p2pkh(ref)
    logger.debug(f"Participant hash160: {p_hash.hex()}")
    logger.debug(f"Refund hash160: {r_hash.hex()}")

    if p_hash == r_hash:
        raise ValueError(
            "participant and refund addresses resolve to the same hash160: both branches of the HTLC "
            "would need the same key, so the counterparty could never redeem with the preimage. "
            "The participant address is the COUNTERPARTY's; the refund address is YOURS."
        )

    if isinstance(secret_hash, str):
        logger.debug(f"Secret hash input (str): {secret_hash}")
        try:
            secret_hash = bytes.fromhex(secret_hash)
            logger.debug(f"Converted secret hash to bytes: {secret_hash.hex()}")
        except Exception as e:
            logger.error(f"Failed to convert secret hash: {e}")
            raise

    # Define opcodes.
    OP_IF = b'\x63'
    OP_ELSE = b'\x67'
    OP_ENDIF = b'\x68'
    OP_SHA256 = b'\xa8'
    OP_EQUALVERIFY = b'\x88'
    OP_DUP = b'\x76'
    OP_HASH160 = b'\xa9'
    OP_CHECKSIG = b'\xac'
    OP_CHECKLOCKTIMEVERIFY = b'\xb1'
    OP_DROP = b'\x75'

    # Build the script with two branches.
    script = (
        OP_IF +
            OP_SHA256 +
            push_data(secret_hash) +
            OP_EQUALVERIFY +
            OP_DUP + OP_HASH160 +
            push_data(p_hash) +
            OP_EQUALVERIFY +
            OP_CHECKSIG +
        OP_ELSE +
            push_data(encode_script_number(locktime)) +
            OP_CHECKLOCKTIMEVERIFY +
            OP_DROP +
            OP_DUP + OP_HASH160 +
            push_data(r_hash) +
            OP_EQUALVERIFY +
            OP_CHECKSIG +
        OP_ENDIF
    )
    
    logger.info(f"Final HTLC redeem script (hex): {script.hex()}")
    computed_p2sh = script_to_p2sh_address(script)
    logger.info(f"Computed P2SH address from redeem script: {computed_p2sh}")
    return script

def run_swap_tests(chain_tip: int):
    """Exercise build_htlc_redeem_script() over the six swap directions.

    Args:
        chain_tip: the current block height of the chain being FUNDED. It is a
            required argument and there is no default, deliberately: this
            function used to pass `locktime=500000` six times, a height BTC
            passed in December 2017, and a hardcoded height in a file people
            copy from is how that value reached the live swapper in the first
            place. Every locktime below is now derived from this tip by
            modules/htlc_timelock.contract_locktime().

    The SECRET_HASH lookup used to live at module scope and raise on import,
    which made importing this module -- and therefore the whole atomic-swap
    package -- fail unless that variable happened to be set. It is read here
    because this is the only place that ever used it. It is a HASH, never a
    preimage; do not put a preimage in that variable.

    Nothing here broadcasts, signs, or opens a socket: it builds scripts and
    logs their P2SH addresses. The asset named in each call is the chain being
    funded, which is what decides how many blocks the 48-hour initiator lock
    is worth (rule 11: one vocabulary, one place).
    """
    secret_hash = os.getenv("SECRET_HASH")
    if not secret_hash:
        logger.error("SECRET_HASH is not set; run_swap_tests() needs one to build a script.")
        raise ValueError("SECRET_HASH is required and must not be empty.")

    # (label, funded asset, participant address = the COUNTERPARTY's address on
    # the funded chain, refund address = OURS on the funded chain). The two
    # differ in every row; build_htlc_redeem_script() now refuses a row where
    # they do not.
    directions = [
        ("GRC to LTC", "GRC", "mg3G6MkQSxMp1iCUYFH5JztynohhGYHPDA", "TLTC1QZS8GM6R673QKD7DS6NPKD2QLLRRSEV48H2VFPQ"),
        ("LTC to GRC", "LTC", "QWeUHZvDSKxSm7UDyVi3VFoMTxUHc3t9jD", "mg3G6MkQSxMp1iCUYFH5JztynohhGYHPDA"),
        # The refund address in this row is a testnet P2SH address, and
        # parse_and_reencode_as_testnet_p2pkh() re-encodes its SCRIPT hash as a
        # P2PKH key hash -- a refund branch nobody can satisfy. That is the
        # defect named in this module's header, left here on purpose so the
        # example keeps demonstrating it rather than hiding it.
        ("GRC to BTC", "GRC", "mg3G6MkQSxMp1iCUYFH5JztynohhGYHPDA", "2MwJ8Q735vzVkaL1SnfbGvefofrDnXgXbfY"),
        ("BTC to GRC", "BTC", "TB1Q7K90Z9QS8UT25KTS8VQ8WWAA6H4SSY9RKJC9J3", "mg3G6MkQSxMp1iCUYFH5JztynohhGYHPDA"),
        ("BTC to LTC", "BTC", "TB1Q7K90Z9QS8UT25KTS8VQ8WWAA6H4SSY9RKJC9J3", "TLTC1QZS8GM6R673QKD7DS6NPKD2QLLRRSEV48H2VFPQ"),
        ("LTC to BTC", "LTC", "QWeUHZvDSKxSm7UDyVi3VFoMTxUHc3t9jD", "2MwJ8Q735vzVkaL1SnfbGvefofrDnXgXbfY"),
    ]

    for index, (label, asset, participant_address, refund_address) in enumerate(directions, start=1):
        locktime = contract_locktime(asset, ROLE_INITIATOR, chain_tip)
        # Progress, with a counter, so a run of six is not a blinking cursor
        # (rule 14). The locktime line says what the number means next to it.
        logger.info(f"[{index}/{len(directions)}] {label} swap, funding {asset}")
        logger.info(f"    {describe_locktime(asset, ROLE_INITIATOR, chain_tip, locktime)}")
        try:
            build_htlc_redeem_script(
                secret_hash=secret_hash,
                participant_address=participant_address,
                refund_address=refund_address,
                locktime=locktime,
            )
            logger.info(f"    {label} script built.")
        except Exception as e:  # noqa: BLE001 -- checked: this is the demo driver at the bottom of the file. It reports which direction failed and carries on to the next; nothing downstream reads a value from it, and no funds are involved because nothing here broadcasts.
            logger.error(f"    {label} FAILED to build: {e}")

if __name__ == "__main__":
    # The chain tip is required on the command line rather than defaulted: see
    # run_swap_tests()'s docstring for why there is no fallback height.
    if len(sys.argv) != 2:  # noqa: PLR2004 -- program name plus one argument
        print("usage: atomic_htlc_scripts.py <current_block_height_of_the_funded_chain>", flush=True)
        print("  builds six example HTLC redeem scripts; broadcasts nothing, signs nothing", flush=True)
        sys.exit(2)
    run_swap_tests(int(sys.argv[1]))
