#!/usr/bin/env python3
"""Bitcoin HTLC client: build, fund and redeem a contract over JSON-RPC.

Role: submodule (one chain's HTLC operations; every decision it makes is a call
      into modules/htlc_rpc.py, modules/htlc_spend.py or modules/htlc_fee.py)
Reads: a Bitcoin wallet daemon -- decodescript, getwalletinfo, getdescriptorinfo,
       gettxout, gettransaction, getrawtransaction, createrawtransaction,
       listunspent, getreceivedbyaddress; and the environment variable
       BTC_RPC_WALLET
Writes: nothing to disk. THE CHAIN AND THE WALLET: the best-effort watch-only
       import mutates the wallet; sendtoaddress and sendrawtransaction
       broadcast.
Can move funds: YES. create_contract() sends coins to a P2SH address and
       redeem_contract() signs and broadcasts a spend of it.
Mainnet-safe: NO. It also cannot build a mainnet contract correctly --
       modules/atomic_htlc_scripts.py hardcodes TESTNET version bytes (0x6F
       P2PKH, 0xC4 P2SH), so every address it derives is a testnet address.

BTC_HTLC_PRIVKEY IS NO LONGER READ, and that is a deliberate reduction rather
than an oversight. It existed to feed `importprivkey`, so that
`signrawtransactionwithwallet` could sign the redeem -- which never worked on a
conditional script (see defect 1 below). redeem_contract() signs with the
`participant_privkey` it is already passed, in this process, so there is one
fewer signing key sitting in an environment variable.

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
        # REDACTED, never the payload verbatim. `sendrawtransaction`'s
        # parameter is the signed spend, whose scriptSig carries the
        # PREIMAGE -- and this line runs BEFORE requests.post, so a
        # broadcast that never leaves the building still printed it. See
        # describe_rpc_payload() in modules/htlc_rpc.py for the
        # measurement and for the one table that decides what is safe to
        # print. The same call is made from all three clients (rule 8).
        logger.debug("RPC call: %s", describe_rpc_payload(method, params))
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

    # NO SUPPRESSION HERE ANY MORE. This line carried a PLR0913 and PLR0917
    # suppression whose reason read "the six are the HTLC's own parameters
    # (amount, secret hash, participant, refund, locktime, fee)", and removing
    # the dead `fee` argument took the signature back under the ceiling on all
    # three clients. Rule 19: a suppression that reaches zero gets deleted
    # rather than kept -- and the thing this one was quieting turned out to be
    # a parameter nobody read, which is exactly what an argument-count warning
    # is for. (Spelled out in words rather than quoted, because a linter reads
    # the quotation as a live directive.)
    def create_contract(self,
                        amount_btc: Decimal,
                        secret_hash: str,
                        participant_address: str,
                        refund_address: str,
                        locktime: int) -> dict:
        """Build the HTLC redeem script, fund its P2SH, and wait for the output.

          1. Build the redeem script.
          2. Ask the node for its P2SH address (decodescript).
          3. Best-effort watch-only import -- NEVER fatal. See defect 3 in the
             module header: this used to be `importaddress`, which raised
             `code=-4, Only legacy wallets are supported by this command` on
             every Bitcoin Core 28.1 wallet, so no contract could be created at
             all. It is not a precondition of anything; a failure here costs
             wallet visibility and nothing else.
          4. Send the funds.
          5. Wait (up to 300 SECONDS -- an interface, not a report, rule 6) for
             the output to appear, matched on the scriptPubKey HEX rather than
             on `scriptPubKey.addresses`, which Core removed in 22.0 (defect 4).

        THE `fee` PARAMETER IS GONE, on all three clients, and it went for the
        same reason the `secret` defect was worth fixing: it was accepted and
        never read. `fee: Decimal = Decimal('0.0001')` here, the same on LTC,
        `Decimal('0.01')` on GRC, and no method body referenced it -- the GRC
        docstring even said "currently not used in this method". A number an
        operator could pass, believing it set the funding fee, that nothing
        anywhere consumed. Grepped by name across every .py, .sh and .js in the
        tree before removing it: no caller on any of the three ever passed it,
        and it was the last positional parameter with a default, so no
        positional caller could break either. The funding fee is the wallet's
        own `sendtoaddress` choice; the REDEEM fee is modules/htlc_fee.py's.
        """
        logger.info(f"Creating BTC HTLC contract for {amount_btc} BTC.")
        redeem_script = build_htlc_redeem_script(secret_hash, participant_address, refund_address, locktime)
        redeem_hex = redeem_script.hex()
        logger.debug(f"Built redeem script: {redeem_hex}")

        dec = self.rpc_call("decodescript", [redeem_hex])
        p2sh_addr = dec.get("p2sh")
        if not p2sh_addr:
            logger.error("Failed to derive P2SH from redeem script.")
            raise Exception("Failed to derive P2SH from redeem script.")

        # The level is chosen INSIDE ensure_watch_only_import(), which is the
        # only place that knows whether this succeeded, was skipped or failed.
        # This line used to be `logger.info(ensure_watch_only_import(...))` in
        # both clients, so a failure and a success printed at the same level on
        # the same shape of sentence (rule 14).
        ensure_watch_only_import(self.rpc_call, p2sh_addr)

        logger.info(f"Sending {amount_btc} BTC to P2SH address {p2sh_addr}.")
        txid = self.rpc_call("sendtoaddress", [p2sh_addr, float(amount_btc)])
        logger.info(f"Transaction sent with TXID: {txid}")

        contract_script_hex = p2sh_script_for(redeem_script).hex()
        vout_index, _outputs = wait_for_tx_output(
            self, txid, contract_script_hex, max_wait=300, expected_address=p2sh_addr
        )
        logger.debug(f"Transaction output found at index {vout_index}.")

        return {
            "txid": txid,
            "vout": vout_index,
            "redeemScript": redeem_script,
            "p2shAddress": p2sh_addr
        }

    def redeem_contract(self,  # noqa: PLR0913, PLR0917 -- checked: the seven are the spend's own inputs. `secret` is the PREIMAGE and is now used -- it is pushed onto the stack, which is the whole point of the hashlock branch; it was accepted and ignored until 2026-09-25 (defect 1 in the module header). They stay POSITIONAL because the two callers in this tree pass them positionally: swap_terminal/regtest/steps.py::_attempt_real_redeem and tests/test_htlc_spend.py::_drive_redeem. UNTIL 2026-09-25 THIS COMMENT NAMED modules/atomic_swapper.py AS A CALLER AND IT IS NOT ONE -- atomic_swapper has no redeem path at all, only start_swap(), which is the same file whose header says the counterparty's leg is redeemed by hand. Grepped by name across every .py, .sh and .js in the tree. Reordering a fund-path signature to satisfy a lint ceiling is the trade rule 12 refuses either way, but the reason has to be true.
                        contract_txid: str,
                        contract_vout: int,
                        redeem_script: bytes,
                        secret: bytes,
                        participant_privkey: str,
                        destination_address: str,
                        contract_blockhash: str | None = None) -> str:
        """Spend the contract's HASHLOCK branch by revealing the preimage.

          1. Read the contract output back -- four routes, none needing
             -txindex (defect 2).
          2. Check the output on chain really is this contract's.
          3. Build, size the fee for, and SIGN the spend in this process. The
             wallet is not asked to sign: `signrawtransactionwithwallet`
             answered `Unable to sign input, invalid stack size (possibly
             missing key)` on this exact script, because it constructs a
             scriptSig by recognizing a pattern and an HTLC matches none.
          4. Broadcast it, WITHOUT disabling the node's fee ceiling. Leaving
             maxfeerate at its default is what makes the run prove the fee rule
             in modules/htlc_fee.py holds on a real transaction rather than
             only in arithmetic.

             THIS SENTENCE USED TO END "the old flat 0.0001 was three times
             over it and would be refused here", which is the 1000x error this
             file's own header refutes forty lines up -- 0.0001 over a 250-byte
             redeem is 0.0004 coin/kvB, 250 times UNDER the 0.10 ceiling, not
             four times over it. A reader who got this far without reading the
             header would have carried the wrong figure away from the line that
             looked most authoritative, which is the whole hazard of leaving a
             refuted number anywhere it still reads as a statement.

        Args:
            contract_blockhash: optional, and only a speed-up. The lookup finds
                a confirmed contract without it; supplying it skips straight to
                the block. It is the ONE addition to this signature, defaulted
                so no existing caller changes.

        Returns:
            The broadcast txid.
        """
        logger.info(f"Redeeming BTC HTLC contract with TXID {contract_txid}.")
        found = lookup_contract_output(self.rpc_call, contract_txid, contract_vout, contract_blockhash)
        logger.info(
            "contract output read via %s: value=%s confirmations=%s (a count, never a duration)",
            found.route,
            found.value,
            found.confirmations,
        )
        assert_output_pays_the_contract(found, redeem_script, "BTC redeem")

        # NO PLATFORM FEE ON BTC. The LTC and GRC clients pay 0.25% to a fee
        # address here and this one pays nothing -- see the divergence table in
        # the module header. That difference is fund movement and is the
        # operator's to settle (rule 16); it is not resolved by this merge.
        spend = build_hashlock_spend(
            asset="BTC",
            rpc_call=self.rpc_call,
            contract_txid=contract_txid,
            contract_vout=contract_vout,
            contract_value=found.value,
            redeem_script=redeem_script,
            secret=secret,
            wif=participant_privkey,
            destination_address=destination_address,
        )
        logger.info(spend.describe("BTC"))

        # The PREIMAGE is on the stack of what is about to be broadcast, and it
        # becomes public the moment this relays -- that is how an atomic swap
        # works. It must never be LOGGED, so nothing here prints the spend's
        # scriptSig or the secret; the secret_hash in the redeem script is the
        # public identifier for this swap.
        txid = self.rpc_call("sendrawtransaction", [spend.raw_hex])
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
        # Ensure that BTC_RPC_URL, BTC_RPC_USER and BTC_RPC_PASS are set in the
        # environment. BTC_HTLC_PRIVKEY is no longer read by anything -- see the
        # module header.
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
