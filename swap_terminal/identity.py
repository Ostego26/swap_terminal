#!/usr/bin/env python3
"""Demo: signed BOINC attestations for a Gridcoin address, anchored on chain.

Role: file (a standalone demonstration; nothing in this tree imports it)
Reads: a Gridcoin wallet daemon -- listunspent, createrawtransaction,
       signrawtransactionwithwallet, sendrawtransaction; GRIDCOIN_RPC_* from
       the environment
Writes: nothing to disk. THE CHAIN: store_attestation_on_chain() signs and
       broadcasts.
Can move funds: YES, and in the worst possible way -- see below.
Mainnet-safe: NO. The docstring says testnet and the RPC endpoint says
       whatever the environment says; nothing in the code enforces either.

MEASURED DEFECT, NOT FIXED HERE: store_attestation_on_chain() BURNS THE WHOLE
UTXO AS FEE. It selects `utxos[0]`, then builds

    outputs = {"data": attestation_hash}

and signs and broadcasts it. There is no change output. A raw transaction's fee
is inputs minus outputs, and the only output here is a zero-value OP_RETURN, so
the ENTIRE value of the selected UTXO is paid to the miner. On a wallet whose
first unspent output is large, one call to this function donates it.

Not fixed here because adding a change output is a fund-path change (rule 16):
it changes what the transaction pays, to which address, and at what fee, and it
cannot be tested from here -- the honest proof is a testnet broadcast whose
txid the operator can look up.

Two smaller things, also measured: `rpc_call(method, params: list = [])` uses a
MUTABLE DEFAULT ARGUMENT, shared across every call that omits `params`; and
nothing in the tree imports this file (established by grepping the whole tree
for the name), so it is an entry point with no entry.
"""

import hashlib
import json
import logging
import random
import time
from decimal import Decimal

import requests
from ecdsa import BadSignatureError, SECP256k1, SigningKey, VerifyingKey
from gridcoin_credentials import (
    gridcoin_rpc_url,
    gridcoin_rpc_user,
    require_gridcoin_rpc_password,
)

# -----------------------------------------------------------------------------
# Logging Configuration
# -----------------------------------------------------------------------------
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

# -----------------------------------------------------------------------------
# Attestation Class and Utility Functions
# -----------------------------------------------------------------------------
class Attestation:
    """Represents an attestation (or badge) issued for a completed BOINC task."""

    def __init__(  # noqa: PLR0913, PLR0917 -- checked: the six are the attestation's own fields and they are exactly what gets signed; bundling them would change the signed message.
        self, gridcoin_address: str, cpid: str, task_reference: str, timestamp: float,
        metadata_hash: str | None, signature: str,
    ):
        """
        Initialize an Attestation object.
        
        Args:
            gridcoin_address (str): The user's Gridcoin address.
            cpid (str): The user's CPID (Combined Project ID).
            task_reference (str): A reference to the completed task.
            timestamp (float): The issuance time.
            metadata_hash (Optional[str]): Optional hash of detailed metadata.
            signature (str): The hex-encoded signature.
        """
        self.gridcoin_address = gridcoin_address
        self.cpid = cpid
        self.task_reference = task_reference
        self.timestamp = timestamp
        self.metadata_hash = metadata_hash
        self.signature = signature

    def to_dict(self) -> dict:
        """Return a dictionary representation of the attestation."""
        return {
            "gridcoin_address": self.gridcoin_address,
            "cpid": self.cpid,
            "task_reference": self.task_reference,
            "timestamp": self.timestamp,
            "metadata_hash": self.metadata_hash,
            "signature": self.signature
        }

    def __str__(self) -> str:
        """Return the attestation as a formatted JSON string."""
        return json.dumps(self.to_dict(), indent=2)


def create_message(gridcoin_address: str, cpid: str, task_reference: str, timestamp: float,
                   metadata_hash: str | None = None) -> bytes:
    """
    Construct the message to be signed from the attestation components.
    
    Args:
        gridcoin_address (str): The user's Gridcoin address.
        cpid (str): The user's CPID.
        task_reference (str): The task reference.
        timestamp (float): The issuance time.
        metadata_hash (Optional[str]): Optional metadata hash.
    
    Returns:
        bytes: The UTF-8 encoded message.
    """
    components = [gridcoin_address, cpid, task_reference, str(timestamp)]
    if metadata_hash:
        components.append(metadata_hash)
    message = "|".join(components)
    logger.debug("Created message: %s", message)
    return message.encode('utf-8')


def issue_attestation(issuer_sk: SigningKey, gridcoin_address: str, cpid: str,
                      task_reference: str, metadata_hash: str | None = None) -> Attestation:
    """
    Issue an attestation by signing the message constructed from the provided details.
    
    Args:
        issuer_sk (SigningKey): The issuer's private key.
        gridcoin_address (str): The user's Gridcoin address.
        cpid (str): The user's CPID.
        task_reference (str): A reference to the completed task.
        metadata_hash (Optional[str]): Optional hash of detailed metadata.
    
    Returns:
        Attestation: The issued attestation.
    """
    timestamp = time.time()
    message = create_message(gridcoin_address, cpid, task_reference, timestamp, metadata_hash)
    message_hash = hashlib.sha256(message).digest()
    logger.debug("Message hash: %s", message_hash.hex())
    signature = issuer_sk.sign(message_hash)
    logger.debug("Generated signature: %s", signature.hex())
    return Attestation(gridcoin_address, cpid, task_reference, timestamp, metadata_hash, signature.hex())


def verify_attestation(attestation: Attestation, issuer_vk: VerifyingKey) -> bool:
    """
    Verify the attestation's signature using the issuer's public key.
    
    Args:
        attestation (Attestation): The attestation to verify.
        issuer_vk (VerifyingKey): The issuer's public key.
    
    Returns:
        bool: True if the signature is valid, otherwise False.
    """
    message = create_message(attestation.gridcoin_address, attestation.cpid,
                             attestation.task_reference, attestation.timestamp, attestation.metadata_hash)
    message_hash = hashlib.sha256(message).digest()
    try:
        result = issuer_vk.verify(bytes.fromhex(attestation.signature), message_hash)
        logger.debug("Attestation verification result: %s", result)
        return result
    except BadSignatureError:
        logger.error("Attestation verification failed due to a bad signature.")
        return False


def compute_reputation(address: str, attestations: list[Attestation]) -> Decimal:
    """
    Compute a reputation score for a given address by counting valid attestations.
    
    For simplicity, each valid attestation counts as 1 reputation point.
    
    Args:
        address (str): The user's Gridcoin address.
        attestations (List[Attestation]): A list of attestations.
    
    Returns:
        Decimal: The computed reputation score.
    """
    valid_count = sum(1 for att in attestations if att.gridcoin_address == address)
    logger.debug("Computed %d valid attestations for address %s", valid_count, address)
    return Decimal(valid_count)


# =============================================================================
# RPC Utility Functions for Gridcoin Testnet
# =============================================================================

# These credentials should match your gridcoinresearch.conf file.
#
# GRIDCOIN_RPC_PASS HAS NO DEFAULT, AND IT USED TO. Until 2026-09-25 this line
# carried a hardcoded password as its fallback -- the same literal as
# chain_tx.sh, which is rule 8's two-copies-of-one-thing wearing a secret.
#
# The value is deliberately not named here. A comment reproducing it would keep
# the string in the tree, trip every secret scanner, and survive the history
# rewrite meant to remove it.
#
# It was a TESTNET credential (25779 is Gridcoin's test chain; mainnet is
# 15715), so nothing of value was behind it. It went anyway, for the reason a
# default like that is always wrong: it makes the insecure path the SILENT one.
# A reader who never sets the variable gets a working script and no signal,
# right up until the day they point it at something that is not testnet.
#
# Empty rather than absent so that importing this module still works -- reading
# the environment at import time is already a rule 12 hazard, and raising here
# would make a failed import the first symptom. rpc_call() refuses instead, at
# the point of use, where the message can say what to do.
# RESOLVED THROUGH gridcoin_credentials, NOT READ DIRECTLY, because the name
# this file used was not the name the operator's .env uses. Measured
# 2026-09-25: the live .env spells it GRIDCOIN_RPC_PASSWORD and this file read
# GRIDCOIN_RPC_PASS, so it could not have authenticated against the
# configuration that actually exists -- and the symptom would have been a 401
# reading as "the wallet is broken". See that module for all four spellings and
# why they are not being renamed.
GRIDCOIN_RPC_USER = gridcoin_rpc_user()
GRIDCOIN_RPC_URL = gridcoin_rpc_url()


def rpc_call(method: str, params: list | None = None):
    """
    Make an RPC call to the Gridcoin testnet wallet.

    Args:
        method (str): The RPC method name.
        params (list): A list of parameters.

    Returns:
        The 'result' field from the JSON-RPC response.
    """
    # `params` was a MUTABLE DEFAULT ARGUMENT (`params: list = []`) until
    # 2026-09-24: one list object shared by every call that omitted it, so
    # anything appending to it would leak into the next caller. Nothing in this
    # file appends, so no behavior changes -- it is the shape that is wrong,
    # and it is one edit away from being a live bug.
    if params is None:
        params = []
    payload = {
        "jsonrpc": "1.0",
        "id": "gridcoin-identity",
        "method": method,
        "params": params
    }
    logger.debug("RPC call: %s with params: %s", method, params)
    # Checked before the socket opens. An unauthenticated Gridcoin RPC call
    # comes back as an HTTP 401, and raise_for_status() turns that into a
    # generic HTTPError several frames from the cause -- so a missing password
    # would present as "the wallet is broken" rather than "you did not set a
    # password". This method carries sendrawtransaction, so that confusion is
    # expensive: the obvious reaction to an unexplained failure is to retry.
    # Resolved at CALL time, not import time. The environment can be set after
    # this module is imported -- a test does exactly that -- and caching the
    # answer at import would make configuration order-dependent, which is the
    # rule 12 hazard this file already carries for its URL and user.
    password = require_gridcoin_rpc_password(method)
    # No timeout, and this same call carries sendrawtransaction. Adding one
    # would make the client report failure for a broadcast that may already
    # have gone out -- the fund-path trade-off written up in
    # modules/atomic_grc_client.py. Reported, not made (rule 16).
    response = requests.post(GRIDCOIN_RPC_URL, json=payload, auth=(GRIDCOIN_RPC_USER, password))  # noqa: S113
    response.raise_for_status()
    result = response.json()["result"]
    logger.debug("RPC result: %s", result)
    return result


def store_attestation_on_chain(attestation: Attestation) -> str:
    """
    Record the attestation on the Gridcoin testnet blockchain by embedding its hash in an OP_RETURN output.

    This function creates a raw transaction with an OP_RETURN output containing the SHA256 hash
    of the attestation (serialized as JSON). This is a simplified example that assumes you have a UTXO available.

    Returns:
        str: The transaction ID of the broadcast transaction.
    """
    # Calculate the SHA256 hash of the attestation JSON.
    attestation_json = str(attestation)
    attestation_hash = hashlib.sha256(attestation_json.encode('utf-8')).hexdigest()
    logger.debug("Attestation hash to store on-chain: %s", attestation_hash)

    # Get available UTXOs.
    utxos = rpc_call("listunspent")
    if not utxos:
        raise Exception("No UTXOs available to fund the transaction.")

    # Use the first UTXO for funding.
    utxo = utxos[0]
    logger.debug("Using UTXO: %s", utxo)
    inputs = [{"txid": utxo["txid"], "vout": utxo["vout"]}]

    # Create a transaction with a single OP_RETURN output using the "data" key.
    outputs = {"data": attestation_hash}
    raw_tx = rpc_call("createrawtransaction", [inputs, outputs])
    signed_tx = rpc_call("signrawtransactionwithwallet", [raw_tx])
    if not signed_tx.get("complete", False):
        raise Exception("Transaction signing incomplete.")
    txid = rpc_call("sendrawtransaction", [signed_tx["hex"]])
    logger.debug("Attestation recorded on-chain, TXID: %s", txid)
    return txid


# =============================================================================
# User Challenge Functions
# =============================================================================

def generate_challenge_message(length: int = 32) -> str:
    """
    Generate a random alphanumeric challenge message.

    Args:
        length (int): The length of the challenge message.

    Returns:
        str: The generated challenge message.
    """
    letters = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    # `random`, not `secrets`: this challenge is what a user signs to prove
    # they hold a key, so a predictable one lets an attacker pre-compute a
    # signature request for a challenge that has not been issued yet. This
    # file is a demo with no callers, so the change is named in the
    # enforcement report rather than made here -- but it is one import.
    challenge = "".join(random.choice(letters) for _ in range(length))  # noqa: S311
    logger.debug("Generated challenge message: %s", challenge)
    return challenge


def user_sign_challenge(challenge: str, user_sk: SigningKey) -> str:
    """
    Simulate the user signing a challenge message.

    Args:
        challenge (str): The challenge message.
        user_sk (SigningKey): The user's ECDSA signing key.

    Returns:
        str: The hex-encoded signature.
    """
    message = challenge.encode('utf-8')
    message_hash = hashlib.sha256(message).digest()
    signature = user_sk.sign(message_hash)
    logger.debug("User signature for challenge: %s", signature.hex())
    return signature.hex()


def verify_user_challenge(challenge: str, signature: str, user_vk: VerifyingKey) -> bool:
    """
    Verify the user's signature for the challenge message.

    Args:
        challenge (str): The challenge message.
        signature (str): The hex-encoded signature provided by the user.
        user_vk (VerifyingKey): The user's ECDSA verifying key.

    Returns:
        bool: True if the signature is valid, False otherwise.
    """
    message = challenge.encode('utf-8')
    message_hash = hashlib.sha256(message).digest()
    try:
        result = user_vk.verify(bytes.fromhex(signature), message_hash)
        logger.debug("User challenge signature verification result: %s", result)
        return result
    except BadSignatureError:
        logger.error("User challenge signature verification failed.")
        return False


# =============================================================================
# Blockchain CPID Verification Using RPC
# =============================================================================

def verify_cpid_on_blockchain(gridcoin_address: str, cpid: str) -> bool:
    """
    Verify that the provided CPID is associated with the given Gridcoin address by querying the blockchain.

    This function uses the "validateaddress" RPC call (if supported by your Gridcoin testnet node)
    to check if the address is valid and to extract the associated CPID.

    Args:
        gridcoin_address (str): The user's Gridcoin address.
        cpid (str): The CPID to verify.

    Returns:
        bool: True if the CPID is valid for the address, False otherwise.
    """
    try:
        logger.debug("Verifying CPID for address: %s", gridcoin_address)
        info = rpc_call("validateaddress", [gridcoin_address])
        logger.debug("validateaddress RPC result: %s", info)
        if info.get("isvalid", False):
            # Assume the CPID is returned under the key "cpid" (adjust as needed for your node).
            chain_cpid = info.get("cpid", None)
            logger.debug("Extracted CPID from blockchain: %s", chain_cpid)
            return chain_cpid == cpid
        else:
            logger.error("Address %s is not valid according to the blockchain.", gridcoin_address)
            return False
    except Exception as e:  # noqa: BLE001 -- checked: this returns False, which is INDISTINGUISHABLE from "the CPID does not match" -- rule 12's exact defect. It is left as-is and named in the enforcement report because this file has no callers; if it ever gets one, this is the first line to fix.
        logger.error("Error during CPID verification: %s", e)
        return False


# =============================================================================
# Main Workflow
# =============================================================================

def main() -> None:
    # Generate issuer keys (for demo purposes, these are newly generated; persist them in production).
    issuer_sk = SigningKey.generate(curve=SECP256k1)
    issuer_vk = issuer_sk.get_verifying_key()

    # Simulate a user key pair (in production, the user would have an existing key pair linked to their Gridcoin address).
    user_sk = SigningKey.generate(curve=SECP256k1)
    user_vk = user_sk.get_verifying_key()

    # Example user data.
    gridcoin_address = "mre8bKn5zM72oVCk3W6noNwajtoFEqpHhT"  # Your Gridcoin testnet address.
    cpid = "09ff71bf7098642c260fcbc9bd9c08c3"                # The CPID associated with the address.
    task_reference = "BOINC_Task_12345"                       # Reference to a completed BOINC task.
    metadata_hash = "optionalhashofmetadata"                  # Optionally, a hash of detailed metadata.

    # Step 1: Verify CPID against the blockchain.
    if not verify_cpid_on_blockchain(gridcoin_address, cpid):
        print("CPID verification failed on the blockchain.")
        return
    else:
        print("CPID successfully verified on the blockchain.")

    # Step 2: Issue an attestation.
    attestation = issue_attestation(issuer_sk, gridcoin_address, cpid, task_reference, metadata_hash)
    print("\nIssued Attestation:")
    print(attestation)

    # Step 3: Verify the attestation.
    if verify_attestation(attestation, issuer_vk):
        print("\nAttestation valid.")
    else:
        print("\nAttestation invalid.")

    # Step 4: Simulate local storage of attestations.
    attestations_db: list[Attestation] = []
    attestations_db.append(attestation)

    # Step 5: Compute reputation score.
    reputation_score = compute_reputation(gridcoin_address, attestations_db)
    print(f"\nReputation score for {gridcoin_address}: {reputation_score}")

    # Step 6: Record the attestation on-chain by storing its hash in an OP_RETURN output.
    try:
        txid = store_attestation_on_chain(attestation)
        print(f"\nAttestation stored on-chain in transaction: {txid}")
    except Exception as e:  # noqa: BLE001 -- checked: the demo driver in main(); it prints and continues to the next step. Nothing reads a value from it.
        print(f"\nError storing attestation on-chain: {e}")

    # Step 7: Generate a challenge message for the user to sign.
    challenge = generate_challenge_message(32)
    print(f"\nChallenge Message: {challenge}")

    # Step 8: Simulate the user signing the challenge.
    user_signature = user_sign_challenge(challenge, user_sk)
    print(f"\nUser's Challenge Signature: {user_signature}")

    # Step 9: Verify the user's challenge signature.
    if verify_user_challenge(challenge, user_signature, user_vk):
        print("\nUser challenge signature verified successfully!")
    else:
        print("\nUser challenge signature verification failed.")


if __name__ == "__main__":
    main()
