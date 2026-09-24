#!/usr/bin/env python3
"""
File: atomic_btc_client.py

Description:
  This module implements the BTCClient class which communicates with a Bitcoin node via JSON-RPC.
  It provides methods to:
    - Make JSON-RPC calls.
    - Import HTLC redeem scripts and private keys.
    - Create HTLC contracts.
    - Redeem HTLC contracts.
    - Retrieve the balance for a given address.
"""

import os
import requests
import logging
from decimal import Decimal
from modules.atomic_htlc_scripts import build_htlc_redeem_script, script_to_p2sh_address
from modules.utils import wait_for_tx_output

# Configure logger.
logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)
if not logger.handlers:
    ch = logging.StreamHandler()
    ch.setLevel(logging.DEBUG)
    formatter = logging.Formatter(
        '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )
    ch.setFormatter(formatter)
    logger.addHandler(ch)


class BTCClient:
    def __init__(self, rpc_url: str, rpc_user: str, rpc_pass: str):
        if not rpc_url or not rpc_user or not rpc_pass:
            raise ValueError("Missing BTC RPC credentials.")
        self.rpc_url = rpc_url.strip()
        # Append wallet name if not already included.
        if "/wallet/" not in self.rpc_url:
            wallet = os.environ.get("BTC_RPC_WALLET", "LegacyWallet")
            self.rpc_url = f"{self.rpc_url}/wallet/{wallet}"
        self.rpc_user = rpc_user
        self.rpc_pass = rpc_pass
        logger.debug(f"BTCClient initialized at {self.rpc_url}")

    def rpc_call(self, method: str, params=None):
        """Makes a JSON-RPC call to the Bitcoin node."""
        if params is None:
            params = []
        payload = {
            "jsonrpc": "1.0",
            "id": "atomic-swap",
            "method": method,
            "params": params
        }
        logger.debug(f"RPC Call Payload: {payload}")
        try:
            response = requests.post(
                self.rpc_url,
                json=payload,
                auth=(self.rpc_user, self.rpc_pass),
                timeout=30
            )
            logger.debug(f"RPC response: {response.status_code} - {response.text}")
            response.raise_for_status()  # Check for HTTP errors
            js = response.json()
            if js.get("error"):
                logger.error(f"RPC Error: {js['error']}")
                raise Exception(f"RPC Error: {js['error']}")
            logger.debug(f"RPC response result: {js['result']}")
            return js["result"]
        except requests.exceptions.RequestException as ex:
            logger.exception(f"RPC request error: {ex}")
            raise
        except ValueError as ex:
            logger.error(f"Invalid JSON response: {ex}")
            raise
        except Exception as ex:
            logger.exception(f"RPC call failed: {ex}")
            raise

    def import_redeem_script_and_key(self, redeem_hex: str):
        """
        Imports the redeem script (via its P2SH address) and the HTLC private key into the wallet.
        """
        p2sh_addr = script_to_p2sh_address(bytes.fromhex(redeem_hex))
        logger.info(f"Importing redeem script with address: {p2sh_addr}")
        try:
            self.rpc_call("importaddress", [p2sh_addr, "HTLC-watch", False, False])
            logger.info(f"Imported redeem script address {p2sh_addr} using importaddress.")
        except Exception as e:
            logger.error(f"Failed to import redeem script {p2sh_addr}: {e}")
            raise

        wif_key = os.environ.get("BTC_HTLC_PRIVKEY", "")
        if not wif_key:
            raise Exception("BTC_HTLC_PRIVKEY is missing from environment variables.")
        
        logger.info("Importing HTLC private key.")
        try:
            self.rpc_call("importprivkey", [wif_key, "HTLC-key", False])
            logger.info("Imported HTLC private key successfully.")
        except Exception as e:
            if "already exists" in str(e).lower() or "duplicate" in str(e).lower():
                logger.info("HTLC private key already imported.")
            else:
                logger.error(f"Failed to import HTLC private key: {e}")
                raise

    def create_contract(self,
                        amount_btc: Decimal,
                        secret_hash: str,
                        participant_address: str,
                        refund_address: str,
                        locktime: int,
                        fee: Decimal = Decimal('0.0001')) -> dict:
        """
        Creates a Bitcoin HTLC contract by:
          1. Building the HTLC redeem script.
          2. Importing the redeem script and HTLC key.
          3. Decoding the script to extract the P2SH address.
          4. Sending funds to the P2SH address.
          5. Waiting (up to 300 seconds) for the output to appear.
        """
        logger.info(f"Creating BTC HTLC contract for {amount_btc} BTC.")
        redeem_script = build_htlc_redeem_script(secret_hash, participant_address, refund_address, locktime)
        redeem_hex = redeem_script.hex()
        logger.debug(f"Built redeem script: {redeem_hex}")
        
        self.import_redeem_script_and_key(redeem_hex)
        
        dec = self.rpc_call("decodescript", [redeem_hex])
        p2sh_addr = dec.get("p2sh")
        if not p2sh_addr:
            logger.error("Failed to derive P2SH from redeem script.")
            raise Exception("Failed to derive P2SH from redeem script.")
        
        logger.info(f"Sending {amount_btc} BTC to P2SH address {p2sh_addr}.")
        txid = self.rpc_call("sendtoaddress", [p2sh_addr, float(amount_btc)])
        logger.info(f"Transaction sent with TXID: {txid}")
        
        vout_index, _ = wait_for_tx_output(self, txid, p2sh_addr, max_wait=300)
        logger.debug(f"Transaction output found at index {vout_index}.")
        
        return {
            "txid": txid,
            "vout": vout_index,
            "redeemScript": redeem_script,
            "p2shAddress": p2sh_addr
        }

    def redeem_contract(self,
                        contract_txid: str,
                        contract_vout: int,
                        redeem_script: bytes,
                        secret: bytes,
                        participant_privkey: str,
                        destination_address: str) -> str:
        """
        Redeems the BTC HTLC contract by:
          1. Retrieving the raw transaction.
          2. Creating a raw transaction that spends the contract output.
          3. Signing the transaction (using either the wallet or key fallback).
          4. Broadcasting the signed transaction.
        """
        logger.info(f"Redeeming BTC HTLC contract with TXID {contract_txid}.")
        raw_tx = self.rpc_call("getrawtransaction", [contract_txid, True])
        total_amount = Decimal(str(raw_tx["vout"][contract_vout]["value"]))
        miner_fee = Decimal("0.0001")
        net_amount = total_amount - miner_fee
        logger.debug(f"Net amount after fee: {net_amount}")
        if net_amount <= 0:
            logger.error("Not enough BTC to cover miner fee.")
            raise Exception("Not enough BTC to cover fee.")
        
        outputs = {destination_address: float(net_amount)}
        inputs = [{"txid": contract_txid, "vout": contract_vout}]
        raw_hex = self.rpc_call("createrawtransaction", [inputs, outputs])
        
        prevtx = {
            "txid": contract_txid,
            "vout": contract_vout,
            "scriptPubKey": raw_tx["vout"][contract_vout]["scriptPubKey"]["hex"],
            "redeemScript": redeem_script.hex(),
            "amount": float(total_amount)
        }
        
        logger.debug(f"Signing raw transaction with key {participant_privkey}.")
        try:
            sign_result = self.rpc_call("signrawtransactionwithwallet", [raw_hex])
        except Exception as e:
            logger.warning(f"signrawtransactionwithwallet failed: {e}. Falling back to signing with key.")
            sign_result = self.rpc_call("signrawtransactionwithkey", [raw_hex, [participant_privkey], [prevtx]])
        
        if not sign_result.get("complete"):
            logger.error(f"Signing incomplete: {sign_result}")
            raise Exception(f"BTC signing incomplete: {sign_result}")
        
        final_hex = sign_result["hex"]
        txid = self.rpc_call("sendrawtransaction", [final_hex])
        logger.info(f"Redeemed contract with TXID: {txid}")
        return txid

    def get_address_balance(self, address: str) -> Decimal:
        """
        Retrieves the balance for the specified address using 'getreceivedbyaddress'.
        Falls back to summing unspent outputs (UTXOs) if needed.
        """
        try:
            logger.info(f"Fetching balance for address {address}.")
            bal = self.rpc_call("getreceivedbyaddress", [address, 0])
            logger.debug(f"Balance from getreceivedbyaddress: {bal}")
            return Decimal(bal)
        except Exception as e:
            logger.warning(f"getreceivedbyaddress failed for {address}: {e}. Falling back to UTXO sum.")
            fallback = Decimal("0.0")
            try:
                utxos = self.rpc_call("listunspent", [])
                for utxo in utxos:
                    if utxo.get("address") == address:
                        fallback += Decimal(str(utxo.get("amount", "0")))
            except Exception as utxo_error:
                logger.error(f"Failed to list UTXOs: {utxo_error}")
                raise Exception("Could not retrieve address balance.")
            return fallback


# For testing purposes:
if __name__ == "__main__":
    try:
        # Ensure that BTC_RPC_URL, BTC_RPC_USER, BTC_RPC_PASS, and BTC_HTLC_PRIVKEY are set in the environment.
        client = BTCClient(
            os.environ.get("BTC_RPC_URL"),
            os.environ.get("BTC_RPC_USER"),
            os.environ.get("BTC_RPC_PASS")
        )
        example_contract = client.create_contract(
            amount_btc=Decimal("0.0001"),
            secret_hash="ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff",
            participant_address="tb1qexampleparticipantaddress0000000000000000000000",
            refund_address="tb1qexamplerefundaddress000000000000000000000000",
            locktime=500000
        )
        print("Contract created:", example_contract)
    except Exception as e:
        print("Error during testing:", e)
