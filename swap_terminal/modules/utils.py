#!/usr/bin/env python3
"""Secret generation, hashing, and waiting for a contract output to appear.

Role: function level (the bottom of rule 10's stack)
Reads: os.urandom; and, in wait_for_tx_output(), a chain daemon through the
       caller's rpc_call (getrawtransaction)
Writes: nothing to disk
Can move funds: no. It GENERATES the HTLC preimage, which is the single most
       dangerous value in this tree -- revealing it before the counterparty's
       leg is funded and confirmed hands them both legs -- but it neither
       signs nor broadcasts.
Mainnet-safe: yes

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
import time
from typing import Any

from microfortnights import format_duration

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


def wait_for_tx_output(
    rpc_client: Any,
    txid: str,
    expected_address: str,
    max_wait: int = 60,
    interval: int = 5,
) -> tuple[int, dict]:
    """Poll the node until a transaction output paying `expected_address` appears.

    Args:
        rpc_client: anything with an `rpc_call` method.
        txid: the transaction to inspect.
        expected_address: the address to look for among the outputs.
        max_wait: SECONDS to keep polling. Seconds, not microfortnights: this
            is compared against time.monotonic() arithmetic and passed to
            time.sleep(), which is an interface, not a report (rule 6). The
            µfn figure appears in the log lines, where a human reads it.
        interval: SECONDS between checks, for the same reason.

    Returns:
        (vout index, the verbose raw transaction).

    Raises:
        TimeoutError: if the output does not appear in time. It carries the
            count of RPC errors seen while waiting, because "the chain has not
            included it yet" and "the daemon has been refusing us for a minute"
            produce the same silence and must not produce the same message
            (rules 12 and 14).

    PROGRESS IS REPORTED, which it was not before 2026-09-24. This loop can sit
    for a full minute by design, and a poll against a quiet chain and a poll
    against a daemon that stopped answering rendered identically -- the RPC
    error was swallowed at DEBUG and nothing was printed at all. An operator
    watching that cannot tell working from hung, and the way that resolves is
    Ctrl-C, which on this path can land between broadcasting a funding
    transaction and recording it.
    """
    started = time.monotonic()
    deadline = started + max_wait
    attempts = 0
    rpc_errors = 0
    last_error = ""
    logger.info(
        "waiting for an output of %s paying %s; giving up after %s",
        txid,
        expected_address,
        format_duration(max_wait),
    )

    while time.monotonic() < deadline:
        attempts += 1
        elapsed = time.monotonic() - started
        try:
            raw_tx = rpc_client.rpc_call("getrawtransaction", [txid, True])
            for idx, v in enumerate(raw_tx.get("vout", [])):
                addresses = v.get("scriptPubKey", {}).get("addresses", [])
                if expected_address in addresses:
                    logger.info(
                        "found the output at index %d after %s (%d poll(s), %d rpc error(s))",
                        idx,
                        format_duration(time.monotonic() - started),
                        attempts,
                        rpc_errors,
                    )
                    return idx, raw_tx
            logger.info(
                "poll %d: not in the transaction yet, %s elapsed of %s, rpc_errors=%d",
                attempts,
                format_duration(elapsed),
                format_duration(max_wait),
                rpc_errors,
            )
        except Exception as exc:  # noqa: BLE001 -- checked: a transient RPC failure must not abort a wait that is otherwise going fine, but it is NOT swallowed: it is counted, logged at WARNING on every occurrence, and carried into the TimeoutError so the caller can tell a quiet chain from a broken daemon.
            rpc_errors += 1
            last_error = str(exc)
            logger.warning(
                "poll %d: rpc error after %s (%d of %d polls have failed): %s",
                attempts,
                format_duration(elapsed),
                rpc_errors,
                attempts,
                exc,
            )

        time.sleep(min(interval, max(0.0, deadline - time.monotonic())))

    waited = time.monotonic() - started
    detail = f"; {rpc_errors} of {attempts} polls raised, last: {last_error}" if rpc_errors else "; no rpc errors"
    logger.error("gave up on %s after %s%s", txid, format_duration(waited), detail)
    raise TimeoutError(
        f"output for txid {txid} paying {expected_address} did not appear within "
        f"{format_duration(waited)}{detail}"
    )
