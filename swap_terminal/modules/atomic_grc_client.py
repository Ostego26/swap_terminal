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
Mainnet-safe: NO, for two independent reasons. The script derivation is
       testnet-only, exactly as on the other two clients. AND THE DEFAULT
       PLATFORM FEE ADDRESS IS A TESTNET ADDRESS: redeem_contract() falls back
       to the literal `mnTh582mZM12fQry6rtZV7XehNVtZRVdDw` when
       PLATFORM_FEE_GRC_ADDRESS is unset, and that is base58 with the 0x6F
       testnet P2PKH version byte -- a mainnet Gridcoin address begins with S.
       So on mainnet, unset, 0.25% of every redeemed contract goes to an
       address on the wrong network: unspendable by anyone, and gone. The LTC
       client's header has always said this about its own `tltc1...` default
       and this one did not, which is why it is here now rather than only at
       the call site (rule 16: a wrong -- or missing -- comment is a bug).

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
     call sites in atomic_swap_gui.py (deleted 2026-09-26, see
     modules/atomic_btc_client.py) all passed an operator's own validated
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
from modules.htlc_fee import platform_fee_coin, usable_platform_fee_address
from modules.htlc_rpc import (
    assert_output_pays_the_contract,
    broadcast_refund,
    build_hashlock_spend,
    describe_rpc_payload,
    lookup_contract_output,
    rpc_result,
    wait_for_tx_output,
)
from modules.htlc_timelock import ROLE_INITIATOR, contract_locktime
from modules.rpc_method_support import rpc_failure_report

# No setLevel and no handler. A library module that forces DEBUG on its own
# logger and attaches a StreamHandler AT IMPORT decides logging policy for
# every program that imports it, and there is no way for the application to
# turn it back off short of reaching into the logger object. That is an
# import-time side effect (rule 12), and on this branch it was the delivery
# mechanism for a preimage leak: see describe_rpc_payload() in
# modules/htlc_rpc.py for the measurement. modules/utils.py had exactly this
# removed on 2026-09-24 for exactly this reason. The application owns logging
# policy -- regtest_htlc_verify.py calls logging.basicConfig(), and so did
# atomic_swap_gui.py until it was deleted on 2026-09-26. One application-level
# caller is still one more than this module may assume, which is why the
# reasoning is about the rule and not about the count.
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
            return rpc_result(response, "GRC RPC Error")
        except requests.exceptions.RequestException as ex:
            logger.exception(f"RPC request error: {ex}")
            raise
        except ValueError as ex:
            logger.error(f"Invalid JSON response: {ex}")
            raise
        except Exception as ex:
            # RULE 14, BACKWARDS, FIXED 2026-09-28. This was
            #     logger.exception("... RPC call failed: ...")
            # which is ERROR *plus* a stack for every exception reaching here -- including a
            # `-32601` on `gettxout`, which GRIDCOIN DOES NOT HAVE AT ALL. That miss is route 1
            # of modules/htlc_rpc.lookup_contract_output()'s four, it happens on EVERY GRC
            # spend, and route 4 covers it -- so ~40 lines of traceback printed in front of a
            # spend that SUCCEEDED. An operator cannot read that as anything but a failure.
            #
            # The decision about WHICH misses are routine is not made here: it is
            # modules/rpc_method_support.rpc_failure_report(), one table for both clients that
            # have this handler, and it quiets a method-not-found ONLY for a method some caller
            # already falls back from. A 401, a refused connection, a rejected transaction and a
            # -32601 on a method this repository needs are all still ERROR with a stack.
            level, note, with_traceback = rpc_failure_report("GRC", method, ex)
            logger.log(level, "%s", note, exc_info=with_traceback)
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

    # No suppression: removing the dead `fee` argument took this signature
    # back under PLR0913's ceiling (rule 19 -- a suppression that reaches zero
    # gets deleted).
    def create_contract(self,
                        amount_grc: Decimal,
                        secret_hash: str,
                        participant_address: str,
                        refund_address: str,
                        locktime: int) -> dict:
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

        THE `fee` PARAMETER IS GONE. Its own docstring line said "currently not
        used in this method", which was true and had been for as long as the
        method existed -- the same shape as the `secret` parameter
        redeem_contract() accepted and never referenced (defect 1 in the module
        header). Grepped by name across every .py, .sh and .js in the tree: no
        caller on any of the three clients ever passed it. The funding fee is
        the wallet's own `sendtoaddress` choice; the REDEEM fee is
        modules/htlc_fee.py's.
            
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

    # NO refund_contract() ON THIS CLIENT, and that is a deliberate gap named
    # here so the next reader does not take it for an oversight (rule 8: a
    # genuine difference belongs in a comment at BOTH sites). BTCClient and
    # LTCClient gained one on 2026-09-26 and both were verified against a real
    # regtest daemon -- refused before expiry, spent after it. Gridcoin has no
    # node in this tree's harness and none can be started in the session that
    # wrote this, so a GRC refund would be untested fund-path code, which rule
    # 16 makes a proposal rather than a fix.
    #
    # Adding it is two lines once a node exists, because everything it needs is
    # already shared: modules/htlc_rpc.broadcast_refund() takes `asset="GRC"`
    # and the fee rule in modules/htlc_fee.py already carries GRC. What is NOT
    # established is the thing that decides whether it can work at all: whether
    # Gridcoin's script interpreter enforces OP_CHECKLOCKTIMEVERIFY. Nothing in
    # this tree has measured that, and the locktime in the redeem script this
    # client builds is meaningless if it does not.
    def redeem_contract(self,  # noqa: PLR0913, PLR0917 -- checked: the seven are the spend's own inputs, and `secret` is the PREIMAGE, which is now pushed onto the stack rather than accepted and ignored (defect 1). They stay POSITIONAL because the two callers in this tree pass them positionally: swap_terminal/regtest/steps.py::_attempt_real_redeem and tests/test_htlc_spend.py::_drive_redeem. UNTIL 2026-09-25 THIS COMMENT NAMED modules/atomic_swapper.py AS A CALLER AND IT IS NOT ONE -- atomic_swapper has no redeem path at all, only start_swap(), which is the same file whose header says the counterparty's leg is redeemed by hand. Grepped by name across every .py, .sh and .js in the tree. Reordering a fund-path signature to satisfy a lint ceiling is the trade rule 12 refuses either way, but the reason has to be true. GRC has an extra reason to be careful: nothing in this tree calls GRCClient.redeem_contract() at all -- not even the harness, which has no Gridcoin node -- so this signature has no caller to break and no run to prove it.
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

        # 0.25% platform fee, from modules/htlc_fee.PLATFORM_FEE_RATE. THE
        # COMMENT THAT USED TO SIT HERE SAID "2.5% of the total amount" beside
        # an expression that computes 0.25%, and a reader in a hurry trusts the
        # sentence. The CODE was right -- it agreed with the LTC client, which
        # spelled the same rate as `Decimal("0.25") / Decimal(100)` -- so the
        # sentence was the defect. THE TWO SPELLINGS ARE NOW ONE (rule 8): that
        # a wrong comment could sit beside a right expression for as long as it
        # did is what two copies of one rule buy you.
        platform_fee = platform_fee_coin("GRC", found.value)
        # THE TESTNET DEFAULT IS GONE, and the comment that used to sit here
        # described the bug correctly without fixing it. It said: `mnTh...` is
        # base58 with the 0x6F testnet P2PKH version byte, a MAINNET Gridcoin
        # address starts with S (WHICH WAS FALSE -- 13.08% of mainnet GRC addresses
        # start with R, measured 2026-09-27; modules/address_network.py decodes the
        # version byte instead), so on mainnet with PLATFORM_FEE_GRC_ADDRESS unset
        # the fee is "paid to an address on the wrong network: unspendable by
        # anyone, and gone" -- and then it said "setting the variable is the fix and
        # it is the operator's".
        #
        # That was the wrong division of labor. Naming a burn is not fixing it, and
        # the 2026-09-27 rate change to 1.5% made it six times more expensive to
        # leave named. platform_fee_address() returns None when the variable is
        # unset, and None means charge no fee -- the redeem still goes through,
        # because the hashlock branch has to be spent before the counterparty's
        # timelock expires and no client here implements a refund. The operator
        # still has to set the variable to COLLECT the fee; they no longer have to
        # set it to avoid destroying it.
        # RESOLVED AND VALIDATED IN ONE CALL since 2026-09-27. This was
        #     fee_address = platform_fee_address("GRC")
        # which returns whatever the variable holds, non-empty, unexamined -- so a
        # truncated paste or an address from another chain went straight into a
        # transaction output and the fee was burned on every redeem, silently. That is
        # the same failure the testnet-literal default had; only the source of the bad
        # string changed.
        #
        # usable_platform_fee_address() returns None for an unusable address exactly as it
        # does for an unset one, and that direction is fixed: an invalid FEE address must
        # NEVER block the redeem. The hashlock branch has to be spent before the
        # counterparty's timelock expires and no client in this package implements a
        # refund, so refusing here would trade our 1.5% for the customer's whole leg.
        # Skip the fee output, warn loudly, let the redeem proceed.
        #
        # The RETURNED VALUE carries WHY, because after this change the old warning text
        # ("is unset") would be false three times out of four (rule 14, rule 16's wrong
        # comment). It also carries whether the address was actually CHECKED -- see the log
        # line below and modules/htlc_fee.PlatformFeeOutput.
        fee = usable_platform_fee_address("GRC")
        extra_outputs = {fee.address: platform_fee} if fee.address else {}

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
            extra_outputs=extra_outputs,
        )
        # Rule 14 and rule 8, rewritten 2026-09-28. This was a three-line branch --
        #     if fee_address: logger.info(...)  else: logger.warning(...)
        # -- spelled identically in all three clients, and it was WRONG in one case that
        # the review found and a live run had already produced: an UNDETERMINED address
        # (a valid address this repository's tables cannot place, which Litecoin's `rltc`
        # and 0x3A both were on 2026-09-27) passes through WITH the address, so the
        # truthiness test took the INFO branch and printed the sentence a VERIFIED
        # address gets. Nothing had been checked and the log could not say so.
        #
        # Both the level and the sentence now come from modules/htlc_fee.py, which is the
        # one place that knows which of the four outcomes happened. A fourth state added
        # there cannot leave one of three clients behind -- which is exactly what had
        # just happened when a fourth state was added.
        logger.log(fee.log_level, "%s; %s", spend.describe("GRC"), fee.outcome("GRC", platform_fee))

        # The preimage is on the stack of what is about to be broadcast and is
        # never logged.
        txid = self.rpc_call("sendrawtransaction", [spend.raw_hex])
        logger.info(f"Redeemed contract with TXID: {txid}")
        return txid


    def refund_contract(  # noqa: PLR0913 -- checked: the six after `self` ARE the refund -- the outpoint, the script, its locktime, the refund key and where the coins go. None can be defaulted and none is derivable from another. This is the same claim, for the same six values, that LTCClient.refund_contract() carries; the two are deliberately identical so a reader can diff them, and grouping them into a Contract object is the better shape on BOTH or neither (see LTC's note).
        self,
        *,
        contract_txid: str,
        contract_vout: int,
        redeem_script: bytes,
        locktime: int,
        refund_privkey: str,
        refund_address: str,
        contract_blockhash: str | None = None,
    ) -> str:
        """Spend the contract's TIMELOCK branch, returning the GRC to the refund key.

        ADDED 2026-09-27, and it closes the last hole in this package's atomicity.
        LTCClient has had a refund since 2026-09-26 and BTCClient's was added with it;
        GRC could FUND a contract and REDEEM one and had no way to get its own coins
        back. A contract that can be funded and cannot be recovered is the worst of the
        three states -- worse than one that cannot be funded -- and on the GRC leg that
        was the state.

        It matters more here than on the other two chains because of which leg GRC tends
        to be. In atomic_swap_xrp_grc.py's GRC-first direction the Gridcoin leg carries
        the INITIATOR's longer timelock, so it is the leg that is still locked when the
        counterparty walks away: exactly the case a refund exists for, on the one chain
        that could not perform one.

        KEYWORD-ONLY, matching LTCClient.refund_contract() rather than the positional
        redeem_contract() beside it, and for the same reason that one gives:
        `refund_privkey` versus the participant key is precisely the confusion positional
        arguments create on a fund path, and there are no existing callers here forcing
        the older convention.

        THE WALLET UNLOCK IS THE ONE GRC-SPECIFIC LINE. broadcast_refund() is shared
        across all three chains because a refund has none of the per-chain divergence a
        redeem has -- no platform fee, one output, one branch. What GRC adds is that its
        wallet is encrypted and `signrawtransaction` needs it open, which is why
        ensure_fully_unlocked() is called here exactly as redeem_contract() calls it. It
        is called BEFORE the output lookup rather than just before signing, matching the
        redeem, so that a locked wallet fails at the same point on both paths instead of
        one of them discovering it late.
        """
        logger.info(f"Refunding GRC HTLC contract with TXID {contract_txid}.")
        self.ensure_fully_unlocked()
        return broadcast_refund(
            asset="GRC",
            rpc_call=self.rpc_call,
            contract_txid=contract_txid,
            contract_vout=contract_vout,
            redeem_script=redeem_script,
            locktime=locktime,
            refund_privkey=refund_privkey,
            refund_address=refund_address,
            contract_blockhash=contract_blockhash,
        )


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
            participant_address="mjKMm7NbZ42D1JLsHG6fW7eETZ2VX6UzGX",
            refund_address="mvbAng7R399T9vz81KiAVRobBNBfKnFrA2",
            # Derived from the daemon's own tip, never a literal -- see the
            # BTC client's demo block for why a hardcoded 500000 is now
            # dangerous rather than merely wrong.
            locktime=contract_locktime("GRC", ROLE_INITIATOR, int(client.rpc_call("getblockcount")))
        )
        print("GRC Contract created:", example_contract)
    except Exception as e:  # noqa: BLE001 -- checked: the demo driver at the bottom of the file.
        print("Error during testing:", e)
