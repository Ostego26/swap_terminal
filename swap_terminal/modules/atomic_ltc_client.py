#!/usr/bin/env python3
"""Litecoin HTLC client: build, fund and redeem a contract over JSON-RPC.

Role: submodule (one chain's HTLC operations; every decision it makes is a call
      into modules/htlc_rpc.py, modules/htlc_spend.py or modules/htlc_fee.py)
Reads: a Litecoin wallet daemon -- decodescript, getwalletinfo, gettxout,
       gettransaction, getrawtransaction, createrawtransaction, listunspent,
       getreceivedbyaddress; and the environment variable
       PLATFORM_FEE_LTC_ADDRESS
Writes: nothing to disk. THE CHAIN AND THE WALLET: the best-effort watch-only
       import mutates the wallet; sendtoaddress and sendrawtransaction
       broadcast.
Can move funds: YES. create_contract() sends coins to a P2SH address, and
       redeem_contract() signs and broadcasts a spend that pays TWO outputs --
       the user, and a platform fee address.
Mainnet-safe: NO. Same testnet-only script derivation as the BTC client, and
       the default platform fee address in redeem_contract() is a testnet
       address (a `tltc1...` literal), so on mainnet the fee output would be
       unspendable or rejected.

THE HARDCODED CREDENTIAL IN `__main__` IS NOT A SECRET WORTH ROTATING, BUT IT
IS A HABIT WORTH STOPPING. The block at the bottom defaults LTC_RPC_PASS to a
literal. It is the shape that put a live GRIDCOIN_RPC_PASSWORD into this
repository's history (rule 2).

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
from decimal import Decimal
from typing import Any

import requests
from modules.atomic_htlc_scripts import build_htlc_redeem_script, p2sh_script_for
from modules.htlc_rpc import (
    assert_output_pays_the_contract,
    build_hashlock_spend,
    describe_rpc_payload,
    ensure_watch_only_import,
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
        # REDACTED -- see the BTC client and describe_rpc_payload() in
        # modules/htlc_rpc.py. One table, three callers (rule 8).
        logger.debug("LTC RPC call: %s", describe_rpc_payload(method, params))
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
            # This used to be `... with params {params}` and so was a
            # SECOND copy of the payload leak -- on the failure path,
            # which is precisely when an operator pastes the output to
            # ask why a redeem did not go out. logger.exception also
            # emits at ERROR, which no application has to opt into.
            logger.exception("LTC RPC request failed: %s", describe_rpc_payload(method, params))
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
        dec = self.rpc_call("decodescript", [redeem_hex])
        p2sh_addr = dec.get("p2sh")
        if not p2sh_addr:
            logger.error("Failed to decode redeem script to P2SH for LTC.")
            raise Exception("Failed to decode redeem script to P2SH for LTC.")
        logger.info(f"Derived LTC P2SH address: {p2sh_addr}")

        # Best-effort watch-only import, and NEVER fatal (defect 3). This used
        # to be `importaddress(redeem_hex, ...)` -- note it passed the SCRIPT
        # HEX where the BTC client passed the P2SH ADDRESS, which is a third
        # spelling of one call and exactly the drift rule 8 is about. It now
        # goes through the shared helper, which asks getwalletinfo which kind
        # of wallet this is instead of assuming.
        logger.info(ensure_watch_only_import(self.rpc_call, p2sh_addr))

        # Send funds to the P2SH address.
        txid = self.rpc_call("sendtoaddress", [p2sh_addr, float(amount_ltc)])
        logger.info(f"sendtoaddress returned TXID: {txid}")

        # WAIT for the output, which this client did not do: it made ONE
        # getrawtransaction call and raised if the output was not there yet,
        # where BTC and GRC polled. It also matched on `scriptPubKey.addresses`
        # and worked only because Litecoin Core 0.21.4 still returns that field
        # (defect 4) -- the match is now on the scriptPubKey hex, which is the
        # same bytes on every daemon.
        contract_script_hex = p2sh_script_for(redeem_script).hex()
        vout_index, _outputs = wait_for_tx_output(
            self, txid, contract_script_hex, max_wait=300, expected_address=p2sh_addr
        )
        logger.debug(f"Contract output found at index {vout_index}.")
        return {
            "txid": txid,
            "vout": vout_index,
            "redeemScript": redeem_script,
            "p2shAddress": p2sh_addr,
            "secret_hash": secret_hash
        }

    def redeem_contract(self,  # noqa: PLR0913, PLR0917 -- checked: the seven are the spend's own inputs, and `secret` is the PREIMAGE, which is now pushed onto the stack rather than accepted and ignored (defect 1). They stay POSITIONAL because modules/atomic_swapper.py and the regtest harness both call this positionally.
                        contract_txid: str,
                        contract_vout: int,
                        redeem_script: bytes,
                        secret: bytes,
                        participant_privkey: str,
                        destination_address: str,
                        contract_blockhash: str | None = None) -> str:
        """Spend the contract's HASHLOCK branch by revealing the preimage.

        The same four steps as the BTC client, plus the platform fee this chain
        charges and BTC does not. See the divergence table in the module
        header: whether BTC should charge one too is fund movement and is the
        operator's (rule 16), so the merge left that row alone.

        The old `sign_fallback()` helper went with this rewrite. It tried
        `signrawtransactionwithkey` and fell back to the legacy
        `signrawtransaction` -- two routes to a signer that cannot satisfy a
        conditional script at all, which Litecoin said in as many words:
        `'error': 'Invalid OP_IF construction'`. Nothing else called it
        (grepped by name across the tree, not by import graph), so it is
        deleted rather than left looking authoritative (rule 9).

        Args:
            contract_blockhash: optional, and only a speed-up; see the BTC
                client. Defaulted, so no existing caller changes.

        Returns:
            The broadcast txid.
        """
        logger.info("Redeeming LTC contract.")
        found = lookup_contract_output(self.rpc_call, contract_txid, contract_vout, contract_blockhash)
        logger.info(
            "contract output read via %s: value=%s confirmations=%s (a count, never a duration)",
            found.route,
            found.value,
            found.confirmations,
        )
        assert_output_pays_the_contract(found, redeem_script, "LTC redeem")

        # 0.25% platform fee, quantized to the satoshi. The GRC client charges
        # the same rate; the BTC client charges nothing. It is taken off the
        # TOTAL, so it does not move when the miner fee does.
        platform_fee = ((Decimal("0.25") / Decimal(100)) * found.value).quantize(Decimal("0.00000001"))
        fee_address = os.environ.get("PLATFORM_FEE_LTC_ADDRESS", "tltc1qzxllez2nfy70rypyh3re0v4z8v0jp57egw6w4p")

        spend = build_hashlock_spend(
            asset="LTC",
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
        logger.info("%s; platform fee %s to %s", spend.describe("LTC"), platform_fee, fee_address)

        # The preimage is on the stack of what is about to be broadcast and is
        # never logged; the secret hash in the redeem script is the public
        # identifier for this swap.
        txid = self.rpc_call("sendrawtransaction", [spend.raw_hex])
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
