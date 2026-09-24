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

MEASURED DEFECT, NOT FIXED HERE: THE LOCKTIME IS ENCODED AS A VARINT.

number_to_le_bytes() implements Bitcoin's COMPACT SIZE (varint) encoding -- the
one used for array lengths in the p2p protocol. The value it produces is then
pushed into the script as the argument to OP_CHECKLOCKTIMEVERIFY, which reads
its operand as a CScriptNum. The two encodings are not the same, and for every
locktime a real swap would use they do not agree. Measured by running the
function:

    locktime asked        bytes pushed      CLTV reads       correct encoding
        500,000           fe20a10700        128,000,254      20a107
        800,000           fe00350c00        204,800,254      00350c
      3,000,000           fec0c62d00        768,000,254      c0c62d
  1,735,689,600           fe80857467    444,336,537,854      80857467

The 0xfe prefix is the varint's "a 4-byte value follows" tag. CScriptNum has no
such tag, so it reads the tag as the low byte of the number. Every value above
is far beyond any height Bitcoin will reach, so the refund branch of every
contract this module builds never becomes spendable: coins funded into one are
recoverable only through the redeem branch, and only by whoever holds the
participant key.

The correct encoding is a minimally-encoded little-endian CScriptNum -- three
bytes for a block height in the hundreds of thousands, with a zero padding byte
if the top bit of the most significant byte is set.

THIS IS NOT FIXED HERE. A timelock value is fund movement (rule 16), the change
alters the P2SH address of every future contract, and it cannot be tested from
here -- the honest proof is a testnet contract funded and then REFUNDED after
expiry, which is exactly the branch CLAUDE.md says has never been exercised.
Handed over with the numbers rather than patched.

TWO MORE THINGS WORTH KNOWING BEFORE TRUSTING build_htlc_redeem_script().

parse_and_reencode_as_testnet_p2pkh() re-encodes ANY address as a P2PKH hash,
including a P2SH address and a P2WSH one. For P2WPKH that happens to be
correct, because the witness program IS hash160(pubkey). For a P2SH address the
21 decoded bytes are a SCRIPT hash, and re-encoding them as a P2PKH hash
produces a script branch that requires a signature from a key whose hash160
equals a script hash -- which nobody has. `run_swap_tests()` below passes
`2MwJ8Q735vzVkaL1SnfbGvefofrDnXgXbfY`, a testnet P2SH address, as a refund
address, so the case is not hypothetical.

And the script's OP_IF branch requires the preimage on the stack, which none of
the three redeem_contract() implementations in this package ever puts there --
see the divergence table in any of modules/atomic_*_client.py.

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

import base58
import bech32
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

def number_to_le_bytes(num: int) -> bytes:
    """Convert integer to little-endian variable-length byte representation."""
    logger.debug(f"Converting number {num} to little-endian bytes.")
    if num < 253:  # noqa: PLR2004 -- checked: these are compact-size varint boundaries, and this function is the SUBJECT of an unresolved fund-path proposal (see the module header). It is left byte-identical so the operator reviews the defect, not a rename.
        return bytes([num])
    elif num <= 0xFFFF:  # noqa: PLR2004 -- same: varint boundary, function under proposal
        return b'\xfd' + struct.pack("<H", num)
    elif num <= 0xFFFFFFFF:  # noqa: PLR2004 -- same: varint boundary, function under proposal
        return b'\xfe' + struct.pack("<I", num)
    else:
        return b'\xff' + struct.pack("<Q", num)

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

def script_to_p2sh_address(script: bytes) -> str:
    """Compute the testnet P2SH address corresponding to a redeem script."""
    script_hash = hash160(script)
    p2sh_addr = base58.b58encode_check(TESTNET_P2SH_VERSION + script_hash).decode()
    logger.debug(f"Computed P2SH address: {p2sh_addr}")
    return p2sh_addr

def build_htlc_redeem_script(secret_hash: str | bytes,
                             participant_address: str,
                             refund_address: str,
                             locktime: int) -> bytes:
    """Build an HTLC redeem script."""
    logger.info("Building HTLC redeem script.")
    
    part = parse_and_reencode_as_testnet_p2pkh(participant_address)
    ref = parse_and_reencode_as_testnet_p2pkh(refund_address)
    logger.debug(f"Re-encoded participant address: {part}")
    logger.debug(f"Re-encoded refund address: {ref}")
    
    # Extract HASH160 values.
    p_hash = _extract_hash160_from_testnet_p2pkh(part)
    r_hash = _extract_hash160_from_testnet_p2pkh(ref)
    logger.debug(f"Participant hash160: {p_hash.hex()}")
    logger.debug(f"Refund hash160: {r_hash.hex()}")

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

    def push_data(data: bytes) -> bytes:
        """Pushes data onto the script stack using the appropriate opcode."""
        length = len(data)
        logger.debug(f"Pushing data (length {length}): {data.hex()}")
        if length < 0x4c:  # noqa: PLR2004 -- checked: OP_PUSHDATA1's boundary, a script opcode constant
            result = bytes([length]) + data
        elif length <= 0xff:  # noqa: PLR2004 -- checked: OP_PUSHDATA2's boundary, a script opcode constant
            result = b'\x4c' + bytes([length]) + data
        elif length <= 0xffff:  # noqa: PLR2004 -- checked: OP_PUSHDATA4's boundary, a script opcode constant
            result = b'\x4d' + struct.pack("<H", length) + data
        else:
            result = b'\x4e' + struct.pack("<I", length) + data
        logger.debug(f"push_data result: {result.hex()}")
        return result

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
            push_data(number_to_le_bytes(locktime)) +
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

def run_swap_tests():
    """Exercise build_htlc_redeem_script() over the six swap directions.

    The SECRET_HASH lookup used to live at module scope and raise on import,
    which made importing this module -- and therefore the whole atomic-swap
    package -- fail unless that variable happened to be set. It is read here
    because this is the only place that ever used it.
    """
    secret_hash = os.getenv("SECRET_HASH")
    if not secret_hash:
        logger.error("SECRET_HASH is not set; run_swap_tests() needs one to build a script.")
        raise ValueError("SECRET_HASH is required and must not be empty.")
    try:
        # Test GRC to LTC Swap
        logger.info("Testing GRC to LTC Swap...")
        build_htlc_redeem_script(
            secret_hash=secret_hash,
            participant_address="mg3G6MkQSxMp1iCUYFH5JztynohhGYHPDA",
            refund_address="TLTC1QZS8GM6R673QKD7DS6NPKD2QLLRRSEV48H2VFPQ",
            locktime=500000
        )
        logger.info("GRC to LTC Swap completed successfully.")
        
        # Test LTC to GRC Swap
        logger.info("Testing LTC to GRC Swap...")
        build_htlc_redeem_script(
            secret_hash=secret_hash,
            participant_address="QWeUHZvDSKxSm7UDyVi3VFoMTxUHc3t9jD",
            refund_address="mg3G6MkQSxMp1iCUYFH5JztynohhGYHPDA",
            locktime=500000
        )
        logger.info("LTC to GRC Swap completed successfully.")
        
        # Test GRC to BTC Swap (example)
        logger.info("Testing GRC to BTC Swap...")
        build_htlc_redeem_script(
            secret_hash=secret_hash,
            participant_address="mg3G6MkQSxMp1iCUYFH5JztynohhGYHPDA",
            refund_address="2MwJ8Q735vzVkaL1SnfbGvefofrDnXgXbfY",
            locktime=500000
        )
        logger.info("GRC to BTC Swap completed successfully.")
        
        # Test BTC to GRC Swap (example)
        logger.info("Testing BTC to GRC Swap...")
        build_htlc_redeem_script(
            secret_hash=secret_hash,
            participant_address="TB1Q7K90Z9QS8UT25KTS8VQ8WWAA6H4SSY9RKJC9J3",
            refund_address="mg3G6MkQSxMp1iCUYFH5JztynohhGYHPDA",
            locktime=500000
        )
        logger.info("BTC to GRC Swap completed successfully.")
        
        # Test BTC to LTC Swap (example)
        logger.info("Testing BTC to LTC Swap...")
        build_htlc_redeem_script(
            secret_hash=secret_hash,
            participant_address="TB1Q7K90Z9QS8UT25KTS8VQ8WWAA6H4SSY9RKJC9J3",
            refund_address="TLTC1QZS8GM6R673QKD7DS6NPKD2QLLRRSEV48H2VFPQ",
            locktime=500000
        )
        logger.info("BTC to LTC Swap completed successfully.")
        
        # Test LTC to BTC Swap (example)
        logger.info("Testing LTC to BTC Swap...")
        build_htlc_redeem_script(
            secret_hash=secret_hash,
            participant_address="QWeUHZvDSKxSm7UDyVi3VFoMTxUHc3t9jD",
            refund_address="2MwJ8Q735vzVkaL1SnfbGvefofrDnXgXbfY",
            locktime=500000
        )
        logger.info("LTC to BTC Swap completed successfully.")

    except Exception as e:  # noqa: BLE001 -- checked: this is the demo driver at the bottom of the file. It reports and returns; nothing downstream reads a value from it.
        logger.error(f"Swap test error: {e}")

if __name__ == "__main__":
    # Call the function to run the swap tests.
    run_swap_tests()
