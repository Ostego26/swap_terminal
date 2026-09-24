#!/usr/bin/env python3
"""Bitcoin HTLC client: build, fund and redeem a contract over JSON-RPC.

Role: submodule (one chain's HTLC operations)
Reads: a Bitcoin wallet daemon -- decodescript, getrawtransaction, listunspent,
       getreceivedbyaddress; and the environment variables BTC_RPC_WALLET and
       BTC_HTLC_PRIVKEY
Writes: nothing to disk. THE CHAIN AND THE WALLET: importaddress and
       importprivkey mutate the wallet; sendtoaddress and sendrawtransaction
       broadcast.
Can move funds: YES. create_contract() sends coins to a P2SH address and
       redeem_contract() signs and broadcasts a spend of it.
Mainnet-safe: NO. It also cannot build a mainnet contract correctly --
       modules/atomic_htlc_scripts.py hardcodes TESTNET version bytes (0x6F
       P2PKH, 0xC4 P2SH), so every address it derives is a testnet address.

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

THE REFUND BRANCH OF EVERY CONTRACT BUILT HERE WAS UNSPENDABLE UNTIL
2026-09-24. The locktime encoding in modules/atomic_htlc_scripts emitted a
varint, not a script number: a requested block height of 500000 was read by
CHECKLOCKTIMEVERIFY as 128,000,254. encode_script_number() replaced it, the
locktime is now derived per swap from the chain tip by
modules/htlc_timelock.py, and the participant and refund addresses are two
values rather than one. See atomic_htlc_scripts.py's header for the
measurement -- and note that no contract built with the corrected script has
been funded, redeemed or refunded on any chain, so the refund branch remains
untested where it counts (rule 17).
"""

import logging
import os
from decimal import Decimal

import requests
from modules.atomic_htlc_scripts import build_htlc_redeem_script, script_to_p2sh_address
from modules.htlc_timelock import ROLE_INITIATOR, contract_locktime
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

    def create_contract(self,  # noqa: PLR0913, PLR0917 -- checked: the six are the HTLC's own parameters (amount, secret hash, participant, refund, locktime, fee). Bundling them into a dataclass changes every call site in the fund path for no behavioral gain.
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

    def redeem_contract(self,  # noqa: PLR0913, PLR0917 -- checked: same. Note `secret` is one of the six and is never used; see the divergence table in the module header.
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
        
        # The WIF PRIVATE KEY used to be logged here, in full, at DEBUG -- and
        # this module sets its own logger to DEBUG with a StreamHandler at
        # import time, so it was printed by default. Exposure of a signing key
        # is total loss of whatever it controls; there is no partial version of
        # that failure. Nothing about this line needed the key itself: what a
        # reader wants to know is WHICH signing route was taken, which the two
        # log lines below now say.
        logger.debug("Signing raw transaction (wallet first, then key fallback).")
        try:
            sign_result = self.rpc_call("signrawtransactionwithwallet", [raw_hex])
        except Exception as e:  # noqa: BLE001 -- checked: any failure of the wallet route is a reason to try the key route, and the key route's own failure is NOT caught, so a genuine signing failure still propagates.
            logger.warning(f"signrawtransactionwithwallet failed: {e}. Falling back to signing with the supplied key.")
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
        except Exception as e:  # noqa: BLE001 -- checked: getreceivedbyaddress is absent on some wallet builds, so any failure means "try the UTXO sum instead". The fallback below is NOT itself swallowed -- it raises -- so a caller never receives a zero balance that actually means "the call failed".
            logger.warning(f"getreceivedbyaddress failed for {address}: {e}. Falling back to UTXO sum.")
            fallback = Decimal("0.0")
            try:
                utxos = self.rpc_call("listunspent", [])
                for utxo in utxos:
                    if utxo.get("address") == address:
                        fallback += Decimal(str(utxo.get("amount", "0")))
            # Checked: this one RE-RAISES (with `from utxo_error`, so the cause
            # survives). Both routes to a balance have failed, and the caller
            # gets an exception rather than a number it cannot distinguish
            # from a real empty address.
            except Exception as utxo_error:
                logger.error(f"Failed to list UTXOs: {utxo_error}")
                raise Exception("Could not retrieve address balance.") from utxo_error
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
            # Not a credential: a placeholder SHA-256 DIGEST for the demo
            # block. The preimage that hashes to it does not exist.
            secret_hash="ff" * 32,
            participant_address="tb1qexampleparticipantaddress0000000000000000000000",
            refund_address="tb1qexamplerefundaddress000000000000000000000000",
            # Derived from the daemon's own tip, never a literal. This block
            # BROADCASTS, and a hardcoded 500000 -- a height BTC passed in
            # 2017 -- would now build a contract refundable the instant it is
            # funded, because the encoder that used to mangle that number into
            # an unreachable height was fixed on 2026-09-24.
            locktime=contract_locktime("BTC", ROLE_INITIATOR, int(client.rpc_call("getblockcount")))
        )
        print("Contract created:", example_contract)
    except Exception as e:  # noqa: BLE001 -- checked: the demo driver at the bottom of the file; it prints and exits, and nothing reads a value from it.
        print("Error during testing:", e)
