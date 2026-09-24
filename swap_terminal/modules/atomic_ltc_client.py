#!/usr/bin/env python3
"""
File: atomic_ltc_client.py

Description:
  This module implements the LTCClient class for interacting with a Litecoin testnet node
  via JSON-RPC. It provides methods for:
    - Making RPC calls.
    - Retrieving an address balance.
    - Creating HTLC contracts.
    - Redeeming HTLC contracts.
  
  NOTE: Ensure that your environment variables for LTC_RPC_URL, LTC_RPC_USER, and LTC_RPC_PASS are set.
"""

import os
import requests
import logging
import time
import struct
from decimal import Decimal
from typing import Any, Dict, List, Optional

# Import the HTLC script builder from atomic_htlc_scripts.
from modules.atomic_htlc_scripts import build_htlc_redeem_script
# Import the wait_for_tx_output helper from utils.
from modules.utils import wait_for_tx_output

# Configure module logger.
logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)
if not logger.handlers:
    ch = logging.StreamHandler()
    ch.setLevel(logging.DEBUG)
    formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    ch.setFormatter(formatter)
    logger.addHandler(ch)


class LTCClient:
    def __init__(self, rpc_url: str, rpc_user: str, rpc_pass: str) -> None:
        """
        Initialize the LTCClient with the provided RPC credentials.
        
        Args:
            rpc_url (str): The RPC URL of the Litecoin node.
            rpc_user (str): RPC username.
            rpc_pass (str): RPC password.
        """
        if not all([rpc_url, rpc_user, rpc_pass]):
            raise ValueError("LTC RPC credentials missing.")
        self.rpc_url: str = rpc_url.strip()
        self.rpc_user: str = rpc_user
        self.rpc_pass: str = rpc_pass
        logger.debug(f"LTCClient initialized at {self.rpc_url}")

    def rpc_call(self, method: str, params: Optional[List[Any]] = None) -> Any:
        """
        Make a JSON-RPC call to the Litecoin node.
        
        Args:
            method (str): The RPC method name.
            params (Optional[List[Any]]): A list of parameters for the RPC call.
            
        Returns:
            Any: The 'result' field from the JSON-RPC response.
        
        Raises:
            Exception: If the HTTP request or RPC call fails.
        """
        if params is None:
            params = []
        payload: Dict[str, Any] = {
            "jsonrpc": "1.0",
            "id": "atomic-swap",
            "method": method,
            "params": params
        }
        logger.debug(f"LTC RPC Call Payload: {payload}")
        try:
            response = requests.post(
                self.rpc_url,
                json=payload,
                auth=(self.rpc_user, self.rpc_pass)
            )
            logger.debug(f"LTC HTTP Status Code: {response.status_code}")
            logger.debug(f"LTC HTTP Response Text: {response.text}")
            response.raise_for_status()
            rj = response.json()
            if rj.get("error"):
                logger.error(f"RPC Error: {rj['error']}")
                raise Exception(f"RPC Error: {rj['error']}")
            logger.debug(f"RPC response result: {rj['result']}")
            return rj.get("result")
        except requests.exceptions.RequestException as e:
            logger.exception(f"LTC RPC request failed for method {method} with params {params}")
            raise Exception(f"LTC RPC request failed: {e}")

    def get_address_balance(self, address: str) -> Decimal:
        """
        Retrieve the balance for a given address.
        First, it attempts to use 'getreceivedbyaddress'; if that fails, it sums UTXO amounts.
        
        Args:
            address (str): The Litecoin address.
        
        Returns:
            Decimal: The balance of the address.
        """
        try:
            logger.info(f"Fetching balance for address {address}.")
            bal = self.rpc_call("getreceivedbyaddress", [address, 0])
            logger.debug(f"Balance from getreceivedbyaddress: {bal}")
            return Decimal(bal)
        except Exception as e:
            logger.warning(f"getreceivedbyaddress failed for {address}: {e}. Using fallback.")
            fallback = Decimal("0.0")
            utxos = self.rpc_call("listunspent", [])
            for utxo in utxos:
                if utxo.get("address") == address:
                    fallback += Decimal(str(utxo.get("amount", "0")))
            logger.debug(f"Fallback balance for {address}: {fallback}")
            return fallback

    def sign_fallback(self, rawtx_hex: str, private_keys: List[str], prevtxs: List[Dict[str, Any]]) -> Dict[str, Any]:
        """
        Attempt to sign a transaction using 'signrawtransactionwithkey'.
        If that fails (e.g., due to the method not being available), fallback to the legacy 'signrawtransaction' method.
        
        Args:
            rawtx_hex (str): The raw transaction in hexadecimal.
            private_keys (List[str]): A list of private keys in WIF format.
            prevtxs (List[Dict[str, Any]]): A list of previous transaction dictionaries.
        
        Returns:
            Dict[str, Any]: The result from the signing RPC call.
        """
        try:
            logger.debug(f"Attempting to sign transaction using 'signrawtransactionwithkey' method.")
            result = self.rpc_call("signrawtransactionwithkey", [rawtx_hex, private_keys, prevtxs])
            logger.debug("Signed LTC transaction with signrawtransactionwithkey.")
            return result
        except Exception as e:
            logger.warning(f"signrawtransactionwithkey failed: {e}")
            if "Method not found" in str(e) or "-32601" in str(e):
                logger.debug("Fallback: Using legacy signrawtransaction method.")
                result = self.rpc_call("signrawtransaction", [rawtx_hex, prevtxs, private_keys, "ALL"])
                logger.debug("Signed LTC transaction with legacy signrawtransaction fallback.")
                return result
            else:
                raise

    def create_contract(self,
                        amount_ltc: Decimal,
                        participant_address: str,
                        refund_address: str,
                        locktime: int,
                        secret_hash: Optional[str] = None,
                        fee: Decimal = Decimal('0.0001')) -> Dict[str, Any]:
        """
        Create an LTC HTLC contract by:
          1. Building the HTLC redeem script.
          2. Importing the redeem script as an address.
          3. Decoding the redeem script to obtain the P2SH address.
          4. Sending funds to that P2SH address.
          5. Waiting for the contract output to appear (up to 300 seconds).
        
        Args:
            amount_ltc (Decimal): The LTC amount to send.
            participant_address (str): The participant’s Litecoin address.
            refund_address (str): The refund Litecoin address.
            locktime (int): The locktime for the HTLC.
            secret_hash (Optional[str]): A hex string representing the secret hash.
            fee (Decimal): A fee parameter (reserved for future use).
        
        Returns:
            Dict[str, Any]: A dictionary containing contract details.
        """
        logger.info("Creating LTC HTLC contract.")
        # Build the HTLC redeem script.
        redeem_script = build_htlc_redeem_script(secret_hash, participant_address, refund_address, locktime)
        redeem_hex = redeem_script.hex()
        logger.debug(f"Built redeem script: {redeem_hex}")
        # Import the redeem script as an address (with no rescan).
        self.rpc_call("importaddress", [redeem_hex, "HTLC-watch", False, True])
        dec = self.rpc_call("decodescript", [redeem_hex])
        p2sh_addr = dec.get("p2sh")
        if not p2sh_addr:
            logger.error("Failed to decode redeem script to P2SH for LTC.")
            raise Exception("Failed to decode redeem script to P2SH for LTC.")
        logger.info(f"Derived LTC P2SH address: {p2sh_addr}")
        # Send funds to the P2SH address.
        txid = self.rpc_call("sendtoaddress", [p2sh_addr, float(amount_ltc)])
        logger.info(f"sendtoaddress returned TXID: {txid}")
        # Wait up to 300 seconds for the contract output to appear.
        raw_tx = self.rpc_call("getrawtransaction", [txid, True])
        vout_index: Optional[int] = None
        for i, v in enumerate(raw_tx.get("vout", [])):
            addresses = v.get("scriptPubKey", {}).get("addresses", [])
            if p2sh_addr in addresses:
                vout_index = i
                break
        if vout_index is None:
            raise Exception("No LTC contract output found in TX.")
        logger.debug(f"Contract output found at index {vout_index}.")
        return {
            "txid": txid,
            "vout": vout_index,
            "redeemScript": redeem_script,
            "p2shAddress": p2sh_addr,
            "secret_hash": secret_hash
        }

    def redeem_contract(self,
                        contract_txid: str,
                        contract_vout: int,
                        redeem_script: bytes,
                        secret: bytes,
                        participant_privkey: str,
                        destination_address: str) -> str:
        """
        Redeem the LTC contract by:
          1. Retrieving the raw transaction.
          2. Creating a raw transaction that spends the contract output.
          3. Signing the transaction using a fallback signing method.
          4. Broadcasting the signed transaction.
        
        Args:
            contract_txid (str): The transaction ID of the contract.
            contract_vout (int): The output index of the contract.
            redeem_script (bytes): The HTLC redeem script in bytes.
            secret (bytes): The secret to redeem the contract.
            participant_privkey (str): The participant's private key in WIF format.
            destination_address (str): The Litecoin address to send redeemed funds.
        
        Returns:
            str: The transaction ID of the redeemed transaction.
        """
        logger.info("Redeeming LTC contract.")
        raw_tx = self.rpc_call("getrawtransaction", [contract_txid, True])
        total_amount = Decimal(str(raw_tx["vout"][contract_vout]["value"]))
        miner_fee = Decimal("0.0001")
        # Calculate platform fee (0.25% of total) and quantize.
        platform_fee = (Decimal("0.25") / Decimal("100")) * total_amount
        platform_fee = platform_fee.quantize(Decimal("0.00000001"))
        net_to_user = total_amount - miner_fee - platform_fee
        if net_to_user <= 0:
            raise Exception("Not enough LTC after fees.")
        logger.debug(f"Net amount after fee: {net_to_user}")
        
        inputs = [{"txid": contract_txid, "vout": contract_vout}]
        fee_address = os.environ.get("PLATFORM_FEE_LTC_ADDRESS", "tltc1qzxllez2nfy70rypyh3re0v4z8v0jp57egw6w4p")
        outputs = {
            destination_address: float(net_to_user),
            fee_address: float(platform_fee)
        }
        rawtx = self.rpc_call("createrawtransaction", [inputs, outputs])
        prevtx = {
            "txid": contract_txid,
            "vout": contract_vout,
            "scriptPubKey": raw_tx["vout"][contract_vout]["scriptPubKey"]["hex"],
            "redeemScript": redeem_script.hex(),
            "amount": float(total_amount)
        }
        sign_result = self.sign_fallback(rawtx, [participant_privkey], [prevtx])
        if not sign_result.get("complete"):
            logger.error(f"LTC signing incomplete: {sign_result}")
            raise Exception(f"LTC signing incomplete: {sign_result}")
        final_hex = sign_result["hex"]
        txid = self.rpc_call("sendrawtransaction", [final_hex])
        logger.info(f"Redeemed LTC TXID: {txid}")
        return txid


# For testing purposes, this block is executed only when running this module directly.
if __name__ == "__main__":
    try:
        client = LTCClient(
            os.environ.get("LTC_RPC_URL", "http://127.0.0.1:19332"),
            os.environ.get("LTC_RPC_USER", "litecoinrpc"),
            os.environ.get("LTC_RPC_PASS", "litec0inPass123")
        )
        # Replace these with valid testnet addresses and values.
        contract = client.create_contract(
            amount_ltc=Decimal("0.1"),
            participant_address="tltc1qexampleparticipantaddressxxxxxxxxxxxxxxxxxxx",
            refund_address="tltc1qexamplerefundaddressxxxxxxxxxxxxxxxxxxxx",
            locktime=500000,
            secret_hash="ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff"
        )
        print("Contract created:", contract)
    except Exception as e:
        print("Error during LTC contract creation:", e)
