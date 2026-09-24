#!/usr/bin/env python3
"""
File: utils.py

Description:
  This module contains utility functions for:
    - Generating cryptographically secure random secrets.
    - Calculating SHA-256 and HASH160 (RIPEMD160(SHA256)) hashes.
    - Polling a blockchain node for a specific transaction output.
"""

import os
import time
import struct
import hashlib
import logging
from typing import Tuple, Any

logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)
if not logger.handlers:
    from sys import stdout
    ch = logging.StreamHandler(stdout)
    ch.setLevel(logging.DEBUG)
    formatter = logging.Formatter(
        '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )
    ch.setFormatter(formatter)
    logger.addHandler(ch)


def generate_secret(length: int = 32) -> bytes:
    """
    Generate a cryptographically secure random secret.

    Args:
        length (int): The number of random bytes to generate (default is 32).

    Returns:
        bytes: The generated secret.

    Raises:
        ValueError: If the specified length is not positive.
    """
    if length <= 0:
        logger.error(f"Invalid length: {length}. Length must be positive.")
        raise ValueError("Length must be positive")
    secret = os.urandom(length)
    logger.debug(f"Generated secret: {secret.hex()}")
    return secret


def sha256_hash(data: bytes) -> bytes:
    """
    Compute the SHA-256 hash of the given data.

    Args:
        data (bytes): The data to hash.

    Returns:
        bytes: The SHA-256 digest of the data.

    Raises:
        TypeError: If the input data is not of type bytes.
    """
    if not isinstance(data, bytes):
        logger.error(f"Invalid data type: {type(data)}. Data must be bytes.")
        raise TypeError("Data must be bytes")
    digest = hashlib.sha256(data).digest()
    logger.debug(f"SHA-256 hash: {digest.hex()}")
    return digest


def hash160(data: bytes) -> bytes:
    """
    Compute the HASH160 (RIPEMD160(SHA256)) of the given data.

    Args:
        data (bytes): The data to hash.

    Returns:
        bytes: The HASH160 digest.

    Raises:
        TypeError: If the input data is not of type bytes.
    """
    if not isinstance(data, bytes):
        logger.error(f"Invalid data type: {type(data)}. Data must be bytes.")
        raise TypeError("Data must be bytes")
    sha_digest = hashlib.sha256(data).digest()
    ripemd160 = hashlib.new('ripemd160', sha_digest)
    digest = ripemd160.digest()
    logger.debug(f"HASH160 hash: {digest.hex()}")
    return digest


def wait_for_tx_output(
    rpc_client: Any,
    txid: str,
    expected_address: str,
    max_wait: int = 60,
    interval: int = 5
) -> Tuple[int, dict]:
    """
    Polls the blockchain node for a specific transaction output that sends funds to the expected address.
    
    This function repeatedly calls the RPC method 'getrawtransaction' (with verbose output)
    and checks each output (vout) for the expected address. If the output is found within the
    max_wait period, it returns the index of that output along with the full raw transaction.
    
    Args:
        rpc_client (Any): An object that has an `rpc_call` method to query the blockchain.
        txid (str): The transaction ID to inspect.
        expected_address (str): The address for which to search in the transaction outputs.
        max_wait (int): The maximum number of seconds to wait (default is 60).
        interval (int): The interval (in seconds) between consecutive checks (default is 5).
    
    Returns:
        Tuple[int, dict]: A tuple containing the index of the output (vout) and the raw transaction data.
    
    Raises:
        Exception: If the expected output is not found within the max_wait period.
    """
    elapsed = 0
    logger.info(f"Waiting for transaction output from TXID: {txid} for address: {expected_address}. Max wait time: {max_wait}s.")

    while elapsed < max_wait:
        try:
            logger.debug(f"Checking transaction {txid}, elapsed time: {elapsed}s.")
            raw_tx = rpc_client.rpc_call("getrawtransaction", [txid, True])
            logger.debug(f"Raw transaction data: {raw_tx}")
            
            for idx, v in enumerate(raw_tx.get("vout", [])):
                addresses = v.get("scriptPubKey", {}).get("addresses", [])
                logger.debug(f"Checking output index {idx}, addresses found: {addresses}")
                if expected_address in addresses:
                    logger.info(f"Found output at index {idx} for address {expected_address}.")
                    return idx, raw_tx
        except Exception as e:
            logger.debug(f"Error while fetching transaction output for {txid}: {e}")
        
        time.sleep(interval)
        elapsed += interval
    
    logger.error(f"Expected output for TXID {txid} not found within {max_wait} seconds.")
    raise Exception(f"Expected output for txid {txid} not found within {max_wait} seconds.")
