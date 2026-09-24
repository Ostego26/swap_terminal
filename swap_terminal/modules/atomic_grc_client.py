#!/usr/bin/env python3
"""Gridcoin HTLC client: build, fund and redeem a contract over JSON-RPC.

Role: submodule (one chain's HTLC operations)
Reads: a Gridcoin wallet daemon -- decodescript, getrawtransaction, listunspent,
       getreceivedbyaddress; and the environment variables
       GRC_WALLET_PASSPHRASE and PLATFORM_FEE_GRC_ADDRESS
Writes: nothing to disk. THE CHAIN AND THE WALLET: walletlock/walletpassphrase
       change the wallet's lock state; sendtoaddress and sendrawtransaction
       broadcast.
Can move funds: YES, and this is the only one of the three that UNLOCKS THE
       WALLET to do it. ensure_fully_unlocked() calls `walletpassphrase` with
       the configured passphrase and leaves the wallet unlocked for 120
       seconds by default -- during which anything else with RPC access can
       spend from it.
Mainnet-safe: NO. Same testnet-only script derivation as the other two clients.

THE WALLET PASSPHRASE IS READ AT IMPORT TIME, NOT AT CALL TIME. The default
argument `wallet_passphrase: str = os.environ.get("GRC_WALLET_PASSPHRASE", "")`
is evaluated once, when the class body is executed -- so a process that loads
its .env after importing this module gets an empty passphrase and silently
skips the unlock, and a process that changes the variable later never sees the
change. That is an import-time side effect (rule 12) hiding in a default
argument.

THE PLATFORM FEE COMMENT CONTRADICTS THE CODE. The comment above the
calculation says "2.5% of the total amount"; the expression is
`Decimal("0.0025") * total_amount`, which is 0.25%. One of the two is wrong and
a reader in a hurry will trust the sentence. Which one is INTENDED is a fund
question and belongs to the operator (rule 16) -- the mismatch is reported, not
resolved. Note the LTC client charges 0.25% and spells it
`Decimal("0.25") / Decimal("100")`, so the code agrees across the two chains
and only this comment disagrees with both.

THE THREE CLIENTS DISAGREE. THE TABLE IS IN ALL THREE FILES, ON PURPOSE.

Rule 8: "if they genuinely differ, the difference is the point and belongs in a
comment at BOTH sites, naming the other one. A reader who finds one must be
told the other exists." Measured 2026-09-24 by reading
modules/atomic_btc_client.py, modules/atomic_ltc_client.py and
modules/atomic_grc_client.py side by side. The last column names which file is
the odd one out; where none is named, no two agree.

  aspect                     BTC                 LTC                 GRC                 odd one out
  rpc HTTP timeout           timeout=30          NONE                NONE                BTC (only one with a timeout)
  platform fee on redeem     none                0.25%               0.25%               BTC (charges nothing)
  ...fee amount vs comment   --                  matches             comment says 2.5%   GRC (comment contradicts code)
  miner fee                  0.0001 BTC          0.0001 LTC          0.01 GRC            (scales differ by chain; fine)
  create_contract arg order  amount, secret_hash,   amount, participant,  amount, secret_hash,   LTC
                             participant, refund,   refund, locktime,     participant, refund,
                             locktime               secret_hash=None      locktime
  secret_hash required?      yes                 NO, defaults None   yes                 LTC
  imports redeem script      importaddress(p2sh) importaddress(hex)  does not import     GRC
  imports HTLC private key   YES (importprivkey) no                  no                  BTC
  signing route              wallet, then key    key, then legacy    legacy only         (all three differ)
  unlocks the wallet         no                  no                  YES                 GRC
  waits for the output       wait_for_tx_output  ONE getrawtransaction  wait_for_tx_output   LTC (does not wait)
  ...with max_wait           300s                n/a                 60s (the default)   (BTC and GRC disagree)
  returns secret_hash        no                  yes                 yes                 BTC
  rpc_call catches           Request/Value/all   RequestException    Request/Value/all   LTC
  balance fallback guarded   yes                 NO try around it    yes                 LTC
  default creds in __main__  no                  YES, a literal      no                  LTC

Two of those are worth reading twice. LTC's `create_contract` takes
`secret_hash` FIFTH and OPTIONAL while the other two take it SECOND and
required, so a caller that passes positionally in the BTC/GRC order builds an
LTC contract whose participant address is the secret hash -- and a caller that
omits it builds one with `secret_hash=None`. And LTC and GRC are the only two
issuing HTTP requests with NO timeout, so a wallet daemon that accepts the
connection and never answers hangs the swap forever with nothing printed.

NONE OF THE THREE USES THE PREIMAGE WHEN REDEEMING. Proven mechanically, by
walking each function's AST for names referenced in its body: in all three,
`redeem_contract`'s `secret: bytes` parameter is accepted and never referenced.
The transaction is built with `createrawtransaction` and handed to a
`signrawtransaction*` call, which for a P2SH input constructs the scriptSig
from the redeem script -- it has no way to know that this particular script
needs the preimage and a TRUE flag pushed to take its OP_IF branch. That the
parameter is unused is measured; that the resulting scriptSig therefore cannot
satisfy the hashlock branch follows from how P2SH spending works and has NOT
been confirmed against a chain here. Say which you have (rule 17): this is the
second.

MERGING THESE THREE IS A PROPOSAL, NOT A CHANGE. They sign and broadcast, which
is fund movement (rule 16). chains/base.py's RPCAdapter is the survivor for
connection handling and the HTLC methods belong on top of it -- but the merge
has to resolve every row above, and each resolution changes what a live swap
does.
"""

import logging
import os
import time
from decimal import Decimal

import requests
from modules.atomic_htlc_scripts import build_htlc_redeem_script
from modules.htlc_timelock import ROLE_INITIATOR, contract_locktime
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
            # The suppression on the requests.post line is a PROPOSAL MARKER,
            # not a dismissal. This
            # client issues HTTP with no timeout, so a daemon that accepts the
            # connection and never answers hangs the swap forever with nothing
            # printed -- the BTC client passes timeout=30 and this one does not
            # (see the divergence table in the module header). Adding one is a
            # FUND-PATH change: the same call carries `sendtoaddress` and
            # `sendrawtransaction`, and a timeout would make the client report
            # failure for a broadcast that may already have gone out. That
            # trade-off is the operator's (rule 16), so it is reported here
            # rather than made quietly.
            response = requests.post(self.rpc_url, json=payload, auth=(self.rpc_user, self.rpc_pass))  # noqa: S113
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
        except Exception as e:  # noqa: BLE001 -- checked: any failure of getreceivedbyaddress means "try the UTXO sum". The fallback re-raises on its own failure, so no caller ever gets a zero it cannot distinguish from a real empty address.
            logger.warning(f"getreceivedbyaddress failed for {address}: {e}. Falling back to UTXO sum.")
            fallback = Decimal("0.0")
            try:
                utxos = self.rpc_call("listunspent", [])
                for utxo in utxos:
                    if utxo.get("address") == address:
                        fallback += Decimal(str(utxo.get("amount", "0")))
            # Checked: this one RE-RAISES, so both routes failing is an error
            # rather than a number the caller could read as a real balance.
            except Exception as utxo_error:
                logger.error(f"Failed to list UTXOs: {utxo_error}")
                raise Exception("Could not retrieve address balance.") from utxo_error
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
        except Exception as e:  # noqa: BLE001 -- checked: `walletlock` on an already-locked wallet is an error we do not care about, and the UNLOCK that follows is not caught -- if that fails, it raises and no contract is created.
            logger.warning(f"Error locking wallet: {e}")
        
        logger.info(f"Unlocking the wallet for {timeout} seconds.")
        try:
            self.rpc_call("walletpassphrase", [self.wallet_passphrase, timeout])
            logger.info("Wallet unlocked successfully.")
        except Exception as e:
            logger.error(f"Failed to unlock GRC wallet: {e}")
            raise
        time.sleep(delay)

    def create_contract(self,  # noqa: PLR0913, PLR0917 -- checked: the six are the HTLC's own parameters; bundling them changes every fund-path call site for no behavioral gain.
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

    def redeem_contract(self,  # noqa: PLR0913, PLR0917 -- checked: same. `secret` is one of the six and is never used; see the divergence table in the module header.
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
            # Not a credential: a placeholder SHA-256 digest for the demo block.
            secret_hash="ff" * 32,
            participant_address="tgrc1qexampleparticipantaddress0000000000000000000000",
            refund_address="tgrc1qexamplerefundaddress000000000000000000000000",
            # Derived from the daemon's own tip, never a literal -- see the
            # BTC client's demo block for why a hardcoded 500000 is now
            # dangerous rather than merely wrong.
            locktime=contract_locktime("GRC", ROLE_INITIATOR, int(client.rpc_call("getblockcount")))
        )
        print("GRC Contract created:", example_contract)
    except Exception as e:  # noqa: BLE001 -- checked: the demo driver at the bottom of the file.
        print("Error during testing:", e)
