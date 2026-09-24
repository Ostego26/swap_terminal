#!/usr/bin/env python3
"""
File: atomic_grc_client.py

Description:
  This module implements the GRCClient class for interacting with a Gridcoin node via JSON-RPC.
  It provides methods to:
    - Make JSON-RPC calls.
    - Retrieve the balance of a given address.
    - Ensure the wallet is unlocked (using a wallet passphrase).
    - Create an HTLC contract for atomic swaps.
    - Redeem an HTLC contract.
    
  Note: This implementation assumes that the Gridcoin node’s RPC commands (such as 
  getreceivedbyaddress, listunspent, decodescript, sendtoaddress, getrawtransaction, 
  createrawtransaction, signrawtransaction, and sendrawtransaction) behave similarly to Bitcoin’s.
"""

import os
import requests
import logging
import time
from decimal import Decimal
from modules.atomic_htlc_scripts import build_htlc_redeem_script
from modules.utils import wait_for_tx_output

# Configure logger
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


class GRCClient:
    def __init__(self, rpc_url: str, rpc_user: str, rpc_pass: str, wallet_passphrase: str = os.environ.get("GRC_WALLET_PASSPHRASE", "")):
        """
        Initializes the Gridcoin client.
        
        Args:
            rpc_url (str): The RPC URL of the Gridcoin node.
            rpc_user (str): RPC username.
            rpc_pass (str): RPC password.
            wallet_passphrase (str): The wallet passphrase (if the wallet is encrypted).
        """
        if not rpc_url or not rpc_user or not rpc_pass:
            raise ValueError("Missing GRC RPC credentials.")
        self.rpc_url = rpc_url.strip()
        self.rpc_user = rpc_user
        self.rpc_pass = rpc_pass
        self.wallet_passphrase = wallet_passphrase
        logger.debug(f"GRCClient initialized at {self.rpc_url}")

    def rpc_call(self, method: str, params=None):
        """
        Makes a JSON-RPC call to the Gridcoin node.
        
        Args:
            method (str): The RPC method name.
            params (list, optional): The list of parameters for the RPC call.
            
        Returns:
            The 'result' field from the RPC response.
            
        Raises:
            Exception: If the RPC request fails or returns an error.
        """
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
            response = requests.post(self.rpc_url, json=payload, auth=(self.rpc_user, self.rpc_pass))
            logger.debug(f"RPC response: {response.status_code} - {response.text}")
            response.raise_for_status()  # Check for HTTP errors
            rj = response.json()
            if rj.get("error"):
                logger.error(f"GRC RPC Error: {rj['error']}")
                raise Exception(f"GRC RPC Error: {rj['error']}")
            logger.debug(f"RPC response result: {rj['result']}")
            return rj.get("result")
        except requests.exceptions.RequestException as ex:
            logger.exception(f"RPC request error: {ex}")
            raise
        except ValueError as ex:
            logger.error(f"Invalid JSON response: {ex}")
            raise
        except Exception as ex:
            logger.exception(f"GRC RPC call failed: {ex}")
            raise

    def get_address_balance(self, address: str) -> Decimal:
        """
        Retrieves the balance for the specified address.
        First attempts to use 'getreceivedbyaddress'; if that fails, it sums UTXO amounts.
        
        Args:
            address (str): The Gridcoin address.
            
        Returns:
            Decimal: The balance of the address.
        """
        logger.info(f"Fetching balance for address {address}.")
        try:
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

    def ensure_fully_unlocked(self, timeout=120, delay=3):
        """
        Unlocks the Gridcoin wallet if a passphrase is provided.
        It first locks the wallet (if needed) and then unlocks it for a specified timeout.
        
        Args:
            timeout (int): The duration (in seconds) for which the wallet is unlocked.
            delay (int): Delay in seconds after unlocking.
            
        Raises:
            Exception: If unlocking the wallet fails.
        """
        if not self.wallet_passphrase:
            logger.info("Wallet passphrase is not provided. Skipping wallet unlock.")
            return
        try:
            logger.info("Locking the wallet first (if needed).")
            # Lock the wallet first (ignore errors if already locked)
            self.rpc_call("walletlock")
        except Exception as e:
            logger.warning(f"Error locking wallet: {e}")
        
        logger.info(f"Unlocking the wallet for {timeout} seconds.")
        try:
            self.rpc_call("walletpassphrase", [self.wallet_passphrase, timeout])
            logger.info("Wallet unlocked successfully.")
        except Exception as e:
            logger.error(f"Failed to unlock GRC wallet: {e}")
            raise
        time.sleep(delay)

    def create_contract(self,
                        amount_grc: Decimal,
                        secret_hash: str,
                        participant_address: str,
                        refund_address: str,
                        locktime: int,
                        fee: Decimal = Decimal('0.01')) -> dict:
        """
        Creates a Gridcoin HTLC contract by:
          1. Building the HTLC redeem script.
          2. Decoding the script to obtain the P2SH address.
          3. Sending funds to that P2SH address.
          4. Waiting (up to a specified timeout) for the contract output to appear.
        
        Args:
            amount_grc (Decimal): The amount of GRC to lock.
            secret_hash (str): The SHA256 hash of the secret.
            participant_address (str): The Gridcoin address of the counterparty.
            refund_address (str): The refund address in case the contract times out.
            locktime (int): The locktime for the contract.
            fee (Decimal, optional): The fee to be considered (currently not used in this method).
            
        Returns:
            dict: A dictionary containing the contract's TXID, output index, redeem script, P2SH address, and secret hash.
            
        Raises:
            Exception: If the contract creation fails.
        """
        logger.info(f"Creating GRC HTLC contract for {amount_grc} GRC.")
        self.ensure_fully_unlocked()
        # Build the HTLC redeem script.
        redeem_script = build_htlc_redeem_script(secret_hash, participant_address, refund_address, locktime)
        redeem_hex = redeem_script.hex()
        logger.debug(f"Redeem script built: {redeem_hex}")
        # Decode the script to obtain the P2SH address.
        dec = self.rpc_call("decodescript", [redeem_hex])
        p2sh_addr = dec.get("p2sh")
        if not p2sh_addr:
            logger.error("Failed to decode GRC redeem script to P2SH.")
            raise Exception("Failed to decode GRC redeem script to P2SH.")
        
        # Send funds to the P2SH address.
        logger.info(f"Sending {amount_grc} GRC to P2SH address {p2sh_addr}.")
        txid = self.rpc_call("sendtoaddress", [p2sh_addr, float(amount_grc)])
        logger.info(f"Transaction sent with TXID: {txid}")
        
        # Wait for the output to appear.
        vout_index, _ = wait_for_tx_output(self, txid, p2sh_addr)
        logger.debug(f"Transaction output found at index {vout_index}.")
        
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
        Redeems a Gridcoin HTLC contract by:
          1. Retrieving the raw transaction details.
          2. Creating a raw transaction that spends the HTLC output.
          3. Signing the raw transaction.
          4. Broadcasting the signed transaction.
        
        Args:
            contract_txid (str): The TXID of the contract.
            contract_vout (int): The output index of the contract.
            redeem_script (bytes): The redeem script used in the contract.
            secret (bytes): The secret (preimage) to unlock the funds.
            participant_privkey (str): The private key (in WIF) to sign the transaction.
            destination_address (str): The address to which the funds should be sent.
            
        Returns:
            str: The TXID of the redeemed transaction.
            
        Raises:
            Exception: If signing or broadcasting the transaction fails.
        """
        logger.info(f"Redeeming GRC HTLC contract with TXID {contract_txid}.")
        self.ensure_fully_unlocked()
        raw_tx = self.rpc_call("getrawtransaction", [contract_txid, True])
        total_amount = Decimal(str(raw_tx["vout"][contract_vout]["value"]))
        miner_fee = Decimal("0.01")
        # Calculate platform fee (2.5% of the total amount).
        platform_fee = (Decimal("0.0025") * total_amount).quantize(Decimal("0.00000001"))
        net_to_user = total_amount - miner_fee - platform_fee
        logger.debug(f"Net amount after fee: {net_to_user}")
        
        if net_to_user <= 0:
            logger.error("Not enough GRC to cover fees after deductions.")
            raise Exception("Not enough GRC after fees.")
        
        inputs = [{"txid": contract_txid, "vout": contract_vout}]
        fee_address = os.environ.get("PLATFORM_FEE_GRC_ADDRESS", "mnTh582mZM12fQry6rtZV7XehNVtZRVdDw")
        outputs = {
            destination_address: float(net_to_user),
            fee_address: float(platform_fee)
        }
        # Create a raw transaction.
        rawtx = self.rpc_call("createrawtransaction", [inputs, outputs])
        prevtx = {
            "txid": contract_txid,
            "vout": contract_vout,
            "scriptPubKey": raw_tx["vout"][contract_vout]["scriptPubKey"]["hex"],
            "redeemScript": redeem_script.hex(),
            "amount": float(total_amount)
        }
        # Sign the raw transaction.
        sign_result = self.rpc_call("signrawtransaction", [rawtx, [prevtx], [participant_privkey], "ALL"])
        
        if not sign_result.get("complete"):
            logger.error(f"GRC signing incomplete: {sign_result}")
            raise Exception(f"GRC signing incomplete: {sign_result}")
        
        final_hex = sign_result["hex"]
        # Broadcast the transaction.
        txid = self.rpc_call("sendrawtransaction", [final_hex])
        logger.info(f"Redeemed contract with TXID: {txid}")
        return txid


# For testing purposes:
if __name__ == "__main__":
    try:
        # Ensure that the following environment variables are set:
        # GRC_RPC_URL, GRC_RPC_USER, GRC_RPC_PASS, and GRC_WALLET_PASSPHRASE.
        client = GRCClient(
            os.environ.get("GRC_RPC_URL"),
            os.environ.get("GRC_RPC_USER"),
            os.environ.get("GRC_RPC_PASS")
        )
        example_contract = client.create_contract(
            amount_grc=Decimal("1.0"),
            secret_hash="ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff",
            participant_address="tgrc1qexampleparticipantaddress0000000000000000000000",
            refund_address="tgrc1qexamplerefundaddress000000000000000000000000",
            locktime=500000
        )
        print("GRC Contract created:", example_contract)
    except Exception as e:
        print("Error during testing:", e)
