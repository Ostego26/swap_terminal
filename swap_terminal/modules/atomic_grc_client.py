#!/usr/bin/env python3
"""Gridcoin HTLC client: build, fund and redeem a contract over JSON-RPC.

Role: submodule (one chain's HTLC operations; every decision it makes is a call
      into modules/htlc_rpc.py, modules/htlc_spend.py or modules/htlc_fee.py)
Reads: a Gridcoin wallet daemon -- decodescript, gettxout, gettransaction,
       getrawtransaction, createrawtransaction, listunspent,
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

NOTHING BELOW HAS BEEN RUN AGAINST A GRIDCOIN NODE. There is none in this
setup. Everything here is the BTC/LTC fix applied to this file by reasoning,
and the "GRIDCOIN IS NOT VERIFIED" paragraph further down says exactly what
that leaves unknown (rule 17).

THE WALLET PASSPHRASE IS READ AT IMPORT TIME, NOT AT CALL TIME. The default
argument `wallet_passphrase: str = os.environ.get("GRC_WALLET_PASSPHRASE", "")`
is evaluated once, when the class body is executed -- so a process that loads
its .env after importing this module gets an empty passphrase and silently
skips the unlock, and a process that changes the variable later never sees the
change. That is an import-time side effect (rule 12) hiding in a default
argument.

FOUR DEFECTS WERE FIXED ON 2026-09-25. ALL FOUR WERE IN ALL THREE CLIENTS.

This block is IDENTICAL IN ALL THREE FILES, on purpose. Rule 8: "if they
genuinely differ, the difference is the point and belongs in a comment at BOTH
sites, naming the other one. A reader who finds one must be told the other
exists." Each defect was confirmed against real Bitcoin Core 28.1.0 and
Litecoin Core 0.21.4 regtest daemons by regtest_htlc_verify.py, except where a
line says otherwise.

  1. redeem_contract() NEVER PUSHED THE PREIMAGE. It accepted `secret: bytes`
     and never referenced it -- proven first by walking each function's AST,
     then on chain. The spend was built with createrawtransaction and handed to
     a signrawtransaction* call, which builds a scriptSig for a P2SH input by
     RECOGNIZING A SCRIPT PATTERN. An HTLC is OP_IF/OP_ELSE/OP_ENDIF and
     matches none, and no argument either RPC takes says "take the OP_IF branch,
     and here is the preimage". The daemons' own words:

         BTC   'error': 'Unable to sign input, invalid stack size (possibly missing key)'
         LTC   'error': 'Invalid OP_IF construction'

     So a funded contract was recoverable only by REFUND -- that is, only by
     abandoning the swap and waiting out the timelock. The spend is now
     assembled and signed in this process:
     modules/htlc_spend.hashlock_script_sig() lays out
     <sig> <pubkey> <preimage> OP_1 <redeemScript>, and
     modules/htlc_rpc.build_hashlock_spend() signs it. ONE implementation, all
     three clients.

  2. redeem_contract() COULD NOT READ BACK A CONFIRMED CONTRACT. Its first
     statement was `getrawtransaction(contract_txid, True)`, which searches only
     the mempool on a node without -txindex. Measured on both chains:

         code=-5, No such mempool transaction. Use -txindex or provide a block
         hash to enable blockchain transaction queries.

     Every contract a real swap redeems is CONFIRMED -- that is what the
     counterparty waited for -- so the redeem failed on its own first line,
     always, before reaching anything to do with HTLCs.
     modules/htlc_rpc.lookup_contract_output() replaces it with four routes
     that each work on a default node: gettxout, getrawtransaction with a block
     hash, gettransaction, and the original call last. It does NOT use
     -txindex, because enabling that on an existing datadir forces a reindex.

  3. create_contract() CALLED importaddress. Measured on Core 28.1, which
     creates DESCRIPTOR wallets by default:

         code=-4, Only legacy wallets are supported by this command

     and the BTC client RAISED on it, so no contract could be created at all.
     Establishing whether the import was needed came first: the harness funds
     the identical P2SH with a plain sendtoaddress and then spends it, and
     `getreceivedbyaddress` -- the only thing in this tree that needs an address
     to be in the wallet -- is called only from get_address_balance(), whose six
     call sites in atomic_swap_gui.py all pass an operator's own validated
     address and never a contract P2SH. So the import is not a precondition of
     anything here. It is KEPT, because "no caller in this tree" is not "no
     caller" (rule 2) and an operator may watch the address on their own node,
     but it is now best-effort: modules/htlc_rpc.ensure_watch_only_import()
     reads getwalletinfo.descriptors AT RUNTIME (never a version string), calls
     importdescriptors on a descriptor wallet and importaddress on a legacy one,
     and cannot stop a swap. The companion `importprivkey` was DELETED: its only
     purpose was to let signrawtransactionwithwallet sign the redeem, which
     never worked and cannot.

  4. wait_for_tx_output() READ scriptPubKey.addresses. Measured:

         BTC, Bitcoin Core 28.1.0    ['address', 'asm', 'desc', 'hex', 'type']
         LTC, Litecoin Core 0.21.4   ['addresses', 'asm', 'hex', 'reqSigs', 'type']

     Core deprecated `addresses` in 0.20 and removed it in 22.0. The match is
     now on the scriptPubKey HEX, which is present and identical on both:
     modules/htlc_rpc.find_output_by_script(). LTC's create_contract() had its
     own inline copy of the same search and it worked ONLY because its daemon is
     four years behind; it would have broken on the day that daemon was upgraded.

  AND A FIFTH THING, WHICH TURNED OUT NOT TO BE A DEFECT AT ALL. Every client
  hardcoded a flat miner fee -- 0.0001 on BTC and LTC, 0.01 on GRC -- and paid
  it whatever the transaction weighed. The brief for this work predicted that
  fixing defect 1 would simply move the failure to `sendrawtransaction`
  refusing the spend as `absurdly-high-fee`, on the grounds that 0.0001 over a
  ~250-byte redeem is "roughly 0.4 coin/kvB, about four times" the 0.10
  default maxfeerate. IT IS 0.0004 coin/kvB -- a thousandth of that, and 250
  times UNDER the ceiling rather than four times over. The same wrong figure
  was already written into this repository's regtest harness and is corrected
  there in the same commit. The fee is now sized from the actual transaction
  anyway, because a constant is the right fee at exactly one size and drifts
  under the minimum RELAY fee as a transaction grows -- but the floor for each
  chain is that chain's old flat fee, so an ordinary redeem pays what it always
  paid. modules/htlc_fee.py carries the rule, the arithmetic, and what it costs.

WHAT IS SHARED NOW, AND WHAT STILL DIVERGES. Measured by reading the three
files side by side after the fix.

  aspect                     BTC                 LTC                 GRC                 odd one out
  redeem scriptSig           ---- modules/htlc_rpc.build_hashlock_spend, one implementation ----
  redeem miner fee           ---- modules/htlc_fee.redeem_miner_fee, one rule, per-chain constants ----
  contract read-back         ---- modules/htlc_rpc.lookup_contract_output, one implementation ----
  finding an output          ---- modules/htlc_rpc.find_output_by_script, on the scriptPubKey hex ----
  waits for the output       wait_for_tx_output  wait_for_tx_output  wait_for_tx_output  (LTC used to not wait)
  ...with max_wait           300s                300s                60s (the default)   GRC (and it always did)
  watch-only import          best-effort         best-effort         does not import     GRC (unchanged; it never did)
  rpc HTTP timeout           timeout=30          NONE                NONE                BTC (only one with a timeout)
  platform fee on redeem     none                0.25%               0.25%               BTC (charges nothing)
  ...fee amount vs comment   --                  matches             comment said 2.5%   (fixed 2026-09-25)
  create_contract arg order  amount, secret_hash,   amount, participant,  amount, secret_hash,   LTC
                             participant, refund,   refund, locktime,     participant, refund,
                             locktime               secret_hash=None      locktime
  secret_hash required?      yes                 NO, defaults None   yes                 LTC
  unlocks the wallet         no                  no                  YES                 GRC
  returns secret_hash        no                  yes                 yes                 BTC
  rpc_call catches           Request/Value/all   RequestException    Request/Value/all   LTC
  balance fallback guarded   yes                 NO try around it    yes                 LTC
  default creds in __main__  no                  YES, a literal      no                  LTC

The rows that still diverge are the ones a merge cannot settle without deciding
something: whether BTC should charge a platform fee, whether LTC's parameter
order should change under its callers, whether GRC should stop unlocking the
wallet. Each is fund movement or armed state, and belongs to the operator
(rule 16). The rows that are merged are the ones where all three were wrong in
the same way, which is the merge rule 8 actually asks for.

GRIDCOIN IS NOT VERIFIED AND CANNOT BE. There is no Gridcoin regtest in this
setup and no GRC node the fixes could be run against, so every GRC line above
is REASONED FROM THE BTC AND LTC RESULT AND NOT MEASURED (rule 17). Two
specifics a reader should carry: Gridcoin descends from the Peercoin line of
proof-of-stake forks, whose transactions carry a 4-byte nTime field Bitcoin and
Litecoin do not have -- modules/htlc_spend.parse_transaction() handles that by
re-serializing the daemon's own bytes and refusing anything it cannot reproduce
exactly, rather than by assuming a layout -- and Gridcoin's sendrawtransaction
takes no maxfeerate argument, so the fee ceiling is checked in
modules/htlc_fee.assert_within_broadcast_ceiling() before broadcasting rather
than being enforced by the node.

THE REFUND BRANCH OF EVERY CONTRACT BUILT HERE WAS UNSPENDABLE UNTIL
2026-09-24. The locktime was encoded as a varint, not a script number: a
requested block height of 500,000 was read by CHECKLOCKTIMEVERIFY as
128,000,254. encode_script_number() replaced it, the locktime is derived per
swap from the chain tip by modules/htlc_timelock.py, and the participant and
refund addresses are two values rather than one. NO CLIENT HERE IMPLEMENTS A
REFUND AT ALL -- there is no refund_contract() on any of the three -- so the
refund branch is exercised only by the regtest harness's own spender, and the
one a real swap would have to use does not exist yet.
"""

import logging
import os
import time
from decimal import Decimal

import requests
from modules.atomic_htlc_scripts import build_htlc_redeem_script, p2sh_script_for
from modules.htlc_rpc import (
    assert_output_pays_the_contract,
    build_hashlock_spend,
    describe_rpc_payload,
    lookup_contract_output,
    wait_for_tx_output,
)
from modules.htlc_timelock import ROLE_INITIATOR, contract_locktime

# No setLevel and no handler. A library module that forces DEBUG on its own
# logger and attaches a StreamHandler AT IMPORT decides logging policy for
# every program that imports it, and there is no way for the application to
# turn it back off short of reaching into the logger object. That is an
# import-time side effect (rule 12), and on this branch it was the delivery
# mechanism for a preimage leak: see describe_rpc_payload() in
# modules/htlc_rpc.py for the measurement. modules/utils.py had exactly this
# removed on 2026-09-24 for exactly this reason. The application owns logging
# policy -- regtest_htlc_verify.py and atomic_swap_gui.py both call
# logging.basicConfig().
logger = logging.getLogger(__name__)


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
        # REDACTED -- see the BTC client and describe_rpc_payload() in
        # modules/htlc_rpc.py. On THIS client the line carried two
        # secrets, not one: ensure_fully_unlocked() calls
        # `walletpassphrase` with the operator's wallet passphrase as
        # parameter 0, on every create_contract() and every
        # redeem_contract().
        logger.debug("RPC call: %s", describe_rpc_payload(method, params))
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
        
        # Wait for the output to appear, matched on the scriptPubKey HEX rather
        # than on `scriptPubKey.addresses` (defect 4). max_wait is left at the
        # helper's 60-SECOND default, which is the one row of the divergence
        # table this merge did NOT change: BTC and LTC wait 300s and this waits
        # 60s, and lengthening a wait on the chain that cannot be tested here
        # would be a change made on a guess about Gridcoin's block timing.
        #
        # NO WATCH-ONLY IMPORT HERE, and that is deliberate rather than an
        # omission. This client never imported the contract address -- the
        # other two did -- and the import is not a precondition of anything
        # (see defect 3 in the module header). Adding an RPC round trip to the
        # fund path of the one chain nobody can run would be a change with no
        # way to measure it, on a client whose rpc_call has no HTTP timeout.
        contract_script_hex = p2sh_script_for(redeem_script).hex()
        vout_index, _outputs = wait_for_tx_output(self, txid, contract_script_hex, expected_address=p2sh_addr)
        logger.debug(f"Transaction output found at index {vout_index}.")
        
        return {
            "txid": txid,
            "vout": vout_index,
            "redeemScript": redeem_script,
            "p2shAddress": p2sh_addr,
            "secret_hash": secret_hash
        }

    def redeem_contract(self,  # noqa: PLR0913, PLR0917 -- checked: the seven are the spend's own inputs, and `secret` is the PREIMAGE, which is now pushed onto the stack rather than accepted and ignored (defect 1). They stay POSITIONAL because modules/atomic_swapper.py calls this positionally.
                        contract_txid: str,
                        contract_vout: int,
                        redeem_script: bytes,
                        secret: bytes,
                        participant_privkey: str,
                        destination_address: str,
                        contract_blockhash: str | None = None) -> str:
        """Spend the contract's HASHLOCK branch by revealing the preimage.

        NOT RUN AGAINST A GRIDCOIN NODE, because there is none in this setup.
        This is the BTC and LTC fix applied here by reasoning (rule 17). Two
        Gridcoin-specific things it depends on, both reasoned and neither
        measured:

          THE TRANSACTION LAYOUT. Gridcoin descends from the Peercoin line of
          proof-of-stake forks, whose CTransaction carries a 4-byte nTime
          between the version and the input count. modules/htlc_spend.py does
          not assume either layout: the node builds the unsigned transaction,
          the parser tries each known layout, and it accepts only one that
          re-serializes to the daemon's exact bytes AND whose first input is
          the outpoint asked for. Being wrong about nTime therefore fails
          loudly with TransactionLayoutError before anything is signed, rather
          than producing a signature over the wrong bytes.

          THE BROADCAST CEILING. Gridcoin's sendrawtransaction takes no
          maxfeerate argument, so no node-side refusal protects a wrong fee
          here. modules/htlc_fee.assert_within_broadcast_ceiling() checks it in
          this process, before broadcasting, for that reason.

        The old `signrawtransaction` call is gone. It was the third of three
        different signing routes across three clients for one job, and like the
        other two it cannot satisfy an OP_IF script whatever arguments it is
        given.

        Args:
            contract_blockhash: optional, and only a speed-up; see the BTC
                client. Defaulted, so no existing caller changes.

        Returns:
            The broadcast txid.
        """
        logger.info(f"Redeeming GRC HTLC contract with TXID {contract_txid}.")
        self.ensure_fully_unlocked()
        found = lookup_contract_output(self.rpc_call, contract_txid, contract_vout, contract_blockhash)
        logger.info(
            "contract output read via %s: value=%s confirmations=%s (a count, never a duration)",
            found.route,
            found.value,
            found.confirmations,
        )
        assert_output_pays_the_contract(found, redeem_script, "GRC redeem")

        # 0.25% platform fee, the same rate the LTC client charges. THE COMMENT
        # THAT USED TO SIT HERE SAID "2.5% of the total amount" beside an
        # expression that computes 0.25%, and a reader in a hurry trusts the
        # sentence. The CODE was right -- it agrees with the LTC client, which
        # spells the same rate as `Decimal("0.25") / Decimal(100)` -- so the
        # sentence was the defect and the sentence is what changed. Nothing
        # about what this pays has moved (rule 16: a wrong comment is a bug, and
        # say which of the two was wrong).
        platform_fee = (Decimal("0.0025") * found.value).quantize(Decimal("0.00000001"))
        fee_address = os.environ.get("PLATFORM_FEE_GRC_ADDRESS", "mnTh582mZM12fQry6rtZV7XehNVtZRVdDw")

        spend = build_hashlock_spend(
            asset="GRC",
            rpc_call=self.rpc_call,
            contract_txid=contract_txid,
            contract_vout=contract_vout,
            contract_value=found.value,
            redeem_script=redeem_script,
            secret=secret,
            wif=participant_privkey,
            destination_address=destination_address,
            extra_outputs={fee_address: platform_fee},
        )
        logger.info("%s; platform fee %s to %s", spend.describe("GRC"), platform_fee, fee_address)

        # The preimage is on the stack of what is about to be broadcast and is
        # never logged.
        txid = self.rpc_call("sendrawtransaction", [spend.raw_hex])
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
