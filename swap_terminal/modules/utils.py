#!/usr/bin/env python3
"""Secret generation and hashing. Nothing here opens a socket.

Role: function level (the bottom of rule 10's stack)
Reads: os.urandom, and nothing else
Writes: nothing to disk
Can move funds: no. It GENERATES the HTLC preimage, which is the single most
       dangerous value in this tree -- revealing it before the counterparty's
       leg is funded and confirmed hands them both legs -- but it neither
       signs nor broadcasts.
Mainnet-safe: yes

WHAT LEFT THIS FILE ON 2026-09-25, AND WHY IT COULD NOT BE FIXED IN PLACE.

`wait_for_tx_output()` used to live here, and it is now
`modules/htlc_rpc.wait_for_tx_output()`. Two reasons, and the second is the one
that forced it:

  IT DID NOT BELONG. This file is the bottom of rule 10's stack -- pure
  functions over bytes, no socket, no daemon. A polling loop that holds an RPC
  conversation for up to five minutes is a submodule, not a function, and its
  presence here is what made this module's header have to say "Reads: ... a
  chain daemon through the caller's rpc_call".

  IT COULD NOT BE FIXED HERE. It matched a contract output by comparing an
  address against `scriptPubKey.addresses`, a field Bitcoin Core removed in
  22.0, so on Core 28.1 it found nothing and polled to its deadline against a
  perfectly funded contract. The fix is to match on the scriptPubKey hex and
  to fall back to the wallet's own record once the funding is confirmed -- and
  that needs a transaction parser, which lives in modules/htlc_spend.py, which
  imports THIS file for hash160(). Fixing it in place would have been an import
  cycle.

THE PREIMAGE USED TO BE LOGGED, AND THAT WAS THE WORST LINE IN THIS FILE.

Before 2026-09-24, generate_secret() ended with

    logger.debug(f"Generated secret: {secret.hex()}")

and this module configured its own logger with `setLevel(logging.DEBUG)` and a
StreamHandler on stdout at import time. So every secret this function ever
produced was printed, in full, to standard output, by default, with no
application opt-in -- into terminal scrollback, into any log file the process
was redirected to, and into anything that captured its output.

The chain-safety rules say it plainly: "Never reveal a preimage. Not in a log,
not in a commit, not in a pasted diagnostic, not in an error message. Log
secret_hash, never secret." The replacement logs the LENGTH and the SHA-256
hash -- the hash is the value that goes into the redeem script and is public by
construction, so it identifies which swap the line is about without being the
thing that spends it.

The handler configuration went with it. A library module that forces DEBUG on
the root of its own logger and attaches a handler at import decides logging
policy for every program that imports it, and there is no way for the
application to turn it back off short of reaching into the logger object. That
is an import-time side effect (rule 12) that happened to be the delivery
mechanism for the leak above.
"""

import hashlib
import logging
import os

# No setLevel and no handler: this is a library module, and the application
# owns logging policy. See the module docstring -- the handler that used to be
# here is what made the preimage leak reach stdout by default.
logger = logging.getLogger(__name__)


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
    # NEVER log `secret` itself. The SHA-256 hash is what goes into the redeem
    # script and is public the moment the contract is funded, so it identifies
    # the swap without being the value that spends it.
    logger.debug("Generated a %d-byte secret; sha256=%s", length, hashlib.sha256(secret).hexdigest())
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
    # Safe to log: this is the value that goes into the redeem script. Note it
    # is the hash OF the preimage, so printing it reveals nothing -- but do not
    # add a line here that logs `data`, which IS the preimage when this is
    # called from the swap path.
    logger.debug("SHA-256 hash: %s", digest.hex())
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
    logger.debug("HASH160 hash: %s", digest.hex())
    return digest
