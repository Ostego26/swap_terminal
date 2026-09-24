#!/usr/bin/env python3
"""
File: atomic_htlc_scripts.py

Description:
  This module contains helper functions for building HTLC redeem scripts and for
  re-encoding Bitcoin (and Litecoin) addresses to testnet P2PKH format.
  It also provides a function to compute the testnet P2SH address from a redeem script.
"""

import logging
import struct
import sys
import os
from typing import Union

import base58
import bech32
from dotenv import load_dotenv

# Debug: Print the current Python search paths
print("Before adding modules, Python search paths are:")
print(sys.path)

# Ensure 'modules' is added to the Python path, without repeating the path
module_path = os.path.abspath(os.path.join(os.path.dirname(__file__), 'modules'))
if module_path not in sys.path:
    sys.path.append(module_path)

# Debug: Print the Python search paths again after adding 'modules'
print("After adding modules, Python search paths are:")
print(sys.path)

# Now proceed with the import
from modules.utils import hash160

# Load environment variables from .env file
load_dotenv()


# Configure module logger for debugging and verbose output
logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)
if not logger.handlers:
    ch = logging.StreamHandler()
    ch.setLevel(logging.DEBUG)
    formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    ch.setFormatter(formatter)
    logger.addHandler(ch)

# Testnet version bytes.
TESTNET_P2PKH_VERSION: bytes = b'\x6F'
TESTNET_P2SH_VERSION: bytes = b'\xC4'

# Fetch refund addresses and other config from environment variables
SERVER_LTC_REFUND_ADDR = os.getenv('SERVER_LTC_REFUND_ADDR')
SERVER_GRC_REFUND_ADDR = os.getenv('SERVER_GRC_REFUND_ADDR')
SERVER_BTC_REFUND_ADDR = os.getenv('SERVER_BTC_REFUND_ADDR')
SERVER_BTC_PARTICIPANT_ADDR = os.getenv('SERVER_BTC_PARTICIPANT_ADDR')
SERVER_BTC_HTLC_ADDR = os.getenv('SERVER_BTC_HTLC_ADDR')

# Fetch secret hash, participant, and refund addresses from environment variables
SECRET_HASH = os.getenv('SECRET_HASH')
PARTICIPANT_ADDRESS = os.getenv('PARTICIPANT_ADDRESS', '')
BTC_REFUND_ADDRESS = os.getenv('SERVER_BTC_REFUND_ADDR')
LTC_REFUND_ADDRESS = os.getenv('SERVER_LTC_REFUND_ADDR')
GRC_REFUND_ADDRESS = os.getenv('SERVER_GRC_REFUND_ADDR')
LOCKTIME = int(os.getenv('LOCKTIME', 500000))

# Ensure SECRET_HASH is set in .env
if not SECRET_HASH:
    logger.error("SECRET_HASH environment variable is missing or empty.")
    raise ValueError("SECRET_HASH is required and must not be empty.")

def number_to_le_bytes(num: int) -> bytes:
    """Convert integer to little-endian variable-length byte representation."""
    logger.debug(f"Converting number {num} to little-endian bytes.")
    if num < 253:
        return bytes([num])
    elif num <= 0xFFFF:
        return b'\xfd' + struct.pack("<H", num)
    elif num <= 0xFFFFFFFF:
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
            logger.debug(f"Prefix found, stripping it.")
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
        if len(decoded) == 21:
            reencoded = base58.b58encode_check(TESTNET_P2PKH_VERSION + decoded[1:]).decode()
            logger.debug(f"Successfully re-encoded Base58 address: {reencoded}")
            return reencoded
    except Exception as e:
        logger.debug(f"Base58 decode failed: {e}")
    
    # Fallback to Bech32 decoding.
    try:
        logger.debug(f"Attempting Bech32 decode on address: {raw}")
        hrp, data = bech32.bech32_decode(raw)
        if data:
            program = bech32.convertbits(data[1:], 5, 8, False)
            if program and len(program) == 20:
                reencoded = base58.b58encode_check(TESTNET_P2PKH_VERSION + bytes(program)).decode()
                logger.debug(f"Successfully re-encoded Bech32 address to Base58: {reencoded}")
                return reencoded
    except Exception as e:
        logger.debug(f"Bech32 decode failed: {e}")
    
    raise ValueError(f"Invalid address format: {address}")

def _extract_hash160_from_testnet_p2pkh(address: str) -> bytes:
    """Extract HASH160 from a testnet P2PKH address."""
    logger.debug(f"Extracting HASH160 from address: {address}")
    decoded = base58.b58decode_check(address)
    if len(decoded) == 21 and decoded[0] == TESTNET_P2PKH_VERSION[0]:
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

def build_htlc_redeem_script(secret_hash: Union[str, bytes],
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
    OP_EQUAL = b'\x87'
    OP_CHECKSIG = b'\xac'
    OP_CHECKLOCKTIMEVERIFY = b'\xb1'
    OP_DROP = b'\x75'

    def push_data(data: bytes) -> bytes:
        """Pushes data onto the script stack using the appropriate opcode."""
        length = len(data)
        logger.debug(f"Pushing data (length {length}): {data.hex()}")
        if length < 0x4c:
            result = bytes([length]) + data
        elif length <= 0xff:
            result = b'\x4c' + bytes([length]) + data
        elif length <= 0xffff:
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
    try:
        # Test GRC to LTC Swap
        logger.info("Testing GRC to LTC Swap...")
        redeem_script = build_htlc_redeem_script(
            secret_hash=SECRET_HASH,
            participant_address="mg3G6MkQSxMp1iCUYFH5JztynohhGYHPDA",
            refund_address="TLTC1QZS8GM6R673QKD7DS6NPKD2QLLRRSEV48H2VFPQ",
            locktime=500000
        )
        logger.info("GRC to LTC Swap completed successfully.")
        
        # Test LTC to GRC Swap
        logger.info("Testing LTC to GRC Swap...")
        redeem_script = build_htlc_redeem_script(
            secret_hash=SECRET_HASH,
            participant_address="QWeUHZvDSKxSm7UDyVi3VFoMTxUHc3t9jD",
            refund_address="mg3G6MkQSxMp1iCUYFH5JztynohhGYHPDA",
            locktime=500000
        )
        logger.info("LTC to GRC Swap completed successfully.")
        
        # Test GRC to BTC Swap (example)
        logger.info("Testing GRC to BTC Swap...")
        redeem_script = build_htlc_redeem_script(
            secret_hash=SECRET_HASH,
            participant_address="mg3G6MkQSxMp1iCUYFH5JztynohhGYHPDA",
            refund_address="2MwJ8Q735vzVkaL1SnfbGvefofrDnXgXbfY",
            locktime=500000
        )
        logger.info("GRC to BTC Swap completed successfully.")
        
        # Test BTC to GRC Swap (example)
        logger.info("Testing BTC to GRC Swap...")
        redeem_script = build_htlc_redeem_script(
            secret_hash=SECRET_HASH,
            participant_address="TB1Q7K90Z9QS8UT25KTS8VQ8WWAA6H4SSY9RKJC9J3",
            refund_address="mg3G6MkQSxMp1iCUYFH5JztynohhGYHPDA",
            locktime=500000
        )
        logger.info("BTC to GRC Swap completed successfully.")
        
        # Test BTC to LTC Swap (example)
        logger.info("Testing BTC to LTC Swap...")
        redeem_script = build_htlc_redeem_script(
            secret_hash=SECRET_HASH,
            participant_address="TB1Q7K90Z9QS8UT25KTS8VQ8WWAA6H4SSY9RKJC9J3",
            refund_address="TLTC1QZS8GM6R673QKD7DS6NPKD2QLLRRSEV48H2VFPQ",
            locktime=500000
        )
        logger.info("BTC to LTC Swap completed successfully.")
        
        # Test LTC to BTC Swap (example)
        logger.info("Testing LTC to BTC Swap...")
        redeem_script = build_htlc_redeem_script(
            secret_hash=SECRET_HASH,
            participant_address="QWeUHZvDSKxSm7UDyVi3VFoMTxUHc3t9jD",
            refund_address="2MwJ8Q735vzVkaL1SnfbGvefofrDnXgXbfY",
            locktime=500000
        )
        logger.info("LTC to BTC Swap completed successfully.")

    except Exception as e:
        logger.error(f"Swap test error: {e}")

if __name__ == "__main__":
    # Call the function to run the swap tests.
    run_swap_tests()
