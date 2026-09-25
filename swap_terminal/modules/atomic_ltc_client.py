#!/usr/bin/env python3
"""Litecoin HTLC client: build, fund and redeem a contract over JSON-RPC.

Role: submodule (one chain's HTLC operations)
Reads: a Litecoin wallet daemon -- decodescript, getrawtransaction, listunspent,
       getreceivedbyaddress; and the environment variable
       PLATFORM_FEE_LTC_ADDRESS
Writes: nothing to disk. THE CHAIN AND THE WALLET: importaddress mutates the
       wallet; sendtoaddress and sendrawtransaction broadcast.
Can move funds: YES. create_contract() sends coins to a P2SH address, and
       redeem_contract() signs and broadcasts a spend that pays TWO outputs --
       the user, and a platform fee address.
Mainnet-safe: NO. Same testnet-only script derivation as the BTC client, and
       the default platform fee address in redeem_contract() is a testnet
       address (a `tltc1...` literal), so on mainnet the fee output would be
       unspendable or rejected.

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

MEASURED ON REGTEST 2026-09-25, and it changes what the `addresses` row above
means. `regtest_htlc_verify.py` ran both chains against real daemons and
printed each one's scriptPubKey fields:

  BTC, Bitcoin Core 28.1.0    ['address', 'asm', 'desc', 'hex', 'type']
  LTC, Litecoin Core 0.21.4   ['addresses', 'asm', 'hex', 'reqSigs', 'type']

So the reliance on `scriptPubKey.addresses` in modules/utils.wait_for_tx_output()
and LTCClient.create_contract() is VERSION-DEPENDENT, not universal: Bitcoin
Core deprecated the field in 0.20 and removed it in 22.0, and Litecoin 0.21.4
still returns it. The real LTCClient.create_contract() therefore SUCCEEDED on
regtest (txid=232594016d..., vout=0) while BTCClient.create_contract() failed --
and BTC's failure was a DIFFERENT cause, `importaddress` refusing on a
descriptor wallet, which Core 28.1 creates by default.

Two consequences worth stating, because the first is easy to read backwards:
LTC's create_contract works today only because its daemon is four years behind,
and it will break the moment that daemon is upgraded past the removal. And the
BTC failure is not fixed by restoring `addresses`; it is a legacy-wallet RPC on
a wallet type that no longer supports one.

A THIRD DEFECT, MEASURED THE SAME DAY AND SHARED BY ALL THREE CLIENTS.
redeem_contract()'s first statement is `getrawtransaction(contract_txid, True)`,
which searches only the mempool. On any node without -txindex -- the default --
it cannot see a contract that has been CONFIRMED, so the redeem path fails
before it reaches signing on exactly the contracts a real swap would redeem.
`gettransaction`, or `getrawtransaction` with the contract's block hash, both
work without an index.

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

THE HARDCODED CREDENTIAL IN `__main__` IS NOT A SECRET WORTH ROTATING, BUT IT
IS A HABIT WORTH STOPPING. The block at the bottom defaults LTC_RPC_PASS to a
literal. It is the shape that put a live GRIDCOIN_RPC_PASSWORD into this
repository's history (rule 2).
"""

import logging
import os
from decimal import Decimal
from typing import Any

import requests

# Import the HTLC script builder from atomic_htlc_scripts.
from modules.atomic_htlc_scripts import build_htlc_redeem_script
from modules.htlc_timelock import ROLE_INITIATOR, contract_locktime

# Import the wait_for_tx_output helper from utils.

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

    def rpc_call(self, method: str, params: list[Any] | None = None) -> Any:
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
        payload: dict[str, Any] = {
            "jsonrpc": "1.0",
            "id": "atomic-swap",
            "method": method,
            "params": params
        }
        logger.debug(f"LTC RPC Call Payload: {payload}")
        try:
            # See the GRC client for the full note: this call has NO timeout
            # while the BTC client's has timeout=30, and adding one is a
            # fund-path change because the same call carries sendtoaddress and
            # sendrawtransaction. Reported, not made (rule 16).
            response = requests.post(  # noqa: S113
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
            raise Exception(f"LTC RPC request failed: {e}") from e

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
        except Exception as e:  # noqa: BLE001 -- checked: any failure means "try the UTXO sum". UNLIKE the BTC and GRC clients, the fallback here is NOT wrapped, so a failure of listunspent propagates -- which is the right direction, and the divergence is noted in the module header.
            logger.warning(f"getreceivedbyaddress failed for {address}: {e}. Using fallback.")
            fallback = Decimal("0.0")
            utxos = self.rpc_call("listunspent", [])
            for utxo in utxos:
                if utxo.get("address") == address:
                    fallback += Decimal(str(utxo.get("amount", "0")))
            logger.debug(f"Fallback balance for {address}: {fallback}")
            return fallback

    def sign_fallback(self, rawtx_hex: str, private_keys: list[str], prevtxs: list[dict[str, Any]]) -> dict[str, Any]:
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
            logger.debug("Attempting to sign transaction using 'signrawtransactionwithkey' method.")
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

    def create_contract(self,  # noqa: PLR0913, PLR0917 -- checked: the six are the HTLC's own parameters. Note this signature's ORDER differs from the other two clients (see the divergence table in the module header); reordering it is a fund-path change.
                        amount_ltc: Decimal,
                        participant_address: str,
                        refund_address: str,
                        locktime: int,
                        secret_hash: str | None = None,
                        fee: Decimal = Decimal('0.0001')) -> dict[str, Any]:
        """
        Create an LTC HTLC contract by:
          1. Building the HTLC redeem script.
          2. Importing the redeem script as an address.
          3. Decoding the redeem script to obtain the P2SH address.
          4. Sending funds to that P2SH address.
          5. Waiting for the contract output to appear (up to 300 seconds).
        
        Args:
            amount_ltc (Decimal): The LTC amount to send.
            participant_address (str): The participant's Litecoin address.
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
        vout_index: int | None = None
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

    def redeem_contract(self,  # noqa: PLR0913, PLR0917 -- checked: same. `secret` is one of the six and is never used.
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
        platform_fee = (Decimal("0.25") / Decimal(100)) * total_amount
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
            # Derived from the daemon's own tip, never a literal -- see the
            # BTC client's demo block for why a hardcoded 500000 is now
            # dangerous rather than merely wrong.
            locktime=contract_locktime("LTC", ROLE_INITIATOR, int(client.rpc_call("getblockcount"))),
            # Not a credential: a placeholder SHA-256 digest for the demo block.
            secret_hash="ff" * 32,
        )
        print("Contract created:", contract)
    except Exception as e:  # noqa: BLE001 -- checked: the demo driver at the bottom of the file.
        print("Error during LTC contract creation:", e)
