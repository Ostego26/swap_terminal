"""JSON-RPC adapter for the three Bitcoin-derived chains.

Role: submodule (one class; the three chains differ only by an `asset` string)
Reads: a wallet daemon over JSON-RPC -- validateaddress, getaddressinfo,
       getbalance, gettransaction, getrawtransaction, listtransactions,
       estimatesmartfee
Writes: nothing to disk. On the chain it can write: see below.
Can move funds: YES. send_to_address() calls `sendtoaddress`, which broadcasts.
       get_new_address() also mutates the wallet (it derives and stores a new
       key). Everything else here is read-only.
Mainnet-safe: the read methods are. send_to_address() is the payout path and
       is only ever called by services/payout_service.py.

THIS IS THE GOOD SHAPE, AND THERE IS A SECOND ONE (rule 8).

Measured 2026-09-24: this family is 140 lines -- one base class plus three
four-line subclasses -- and `modules/rpc_clients.py` plus the three
`modules/atomic_*_client.py` files are 1001 lines doing the same JSON-RPC
against the same three daemons. They share no code. A reader who finds one
must be told the other exists, so: the other one is modules/, and the
divergences between its three near-copies are written up in the enforcement
report rather than silently merged, because merging them touches signing and
broadcasting.

WHO USES THAT SECOND FAMILY, RE-MEASURED 2026-09-26. This sentence used to say
"it is used by atomic_swap_gui.py", and that file has been deleted (it was
tkinter, nothing in the tree imported it, and the operator asked for web
surfaces instead). Measured by grepping every IMPORT of the three client
classes across the tree, rather than by reasoning about who probably calls
them -- the first attempt at this paragraph named the wrong files, from a
grep for the class names that also matched prose:

    regtest_htlc_verify.py:178-179   BTCClient, LTCClient -- the regtest
                                     harness entry point at the repository root
    tests/test_htlc_spend.py:72-79   BTCClient, GRCClient, LTCClient

and nothing else imports them. So after the deletion the second RPC family is
reached from ONE entry point -- a regtest harness -- plus one test file, and
the Flask application reaches it from nowhere at all.

That does NOT make it dead: the harness is a real caller, and rule 2's line
holds anyway ("no caller in this tree" is not "no caller"). It is also not a
licence to merge the families here, because merging them touches signing and
broadcasting. It is worth knowing before anybody prices that work again,
because the blast radius just got smaller.

WHAT THE BROAD EXCEPTS HERE DO AND DO NOT HIDE. Each is annotated at its site
with what was checked. The rule (12) is that a broad catch is never legitimate
when the caller cannot tell the failure from a real answer -- and on this money
path `except Exception: return 0` around a confirmation count reads as "zero
confirmations", which is indistinguishable from a real unconfirmed deposit.
"""

import json
import logging
import sys
from pathlib import Path
from typing import NamedTuple

import requests

# The application imports its own modules rootlessly, so a file two
# directories down needs the package root on the path to reach a leaf beside
# microfortnights.py. This is rule 10's layout gap, the same one
# workers/deposit_watcher.py names; fixing it properly means moving entry
# points to the root, which is a large diff with no behavioral benefit on a
# key-holding system.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from chains.coin_amounts import fit_to_chain_precision
from script_pub_key import pays_address

logger = logging.getLogger(__name__)


class RPCError(Exception):
    """A JSON-RPC call failed, or the daemon returned an error object.

    Raised rather than swallowed on purpose: on this path, a failure that
    returns a plausible value (False, 0, an empty list) is worse than one that
    stops the caller, because the caller cannot tell it from a real answer.
    """


# One satoshi, as a float, used as the tolerance when matching an output's
# value against an expected amount. The chains here all use 8 decimal places,
# so anything smaller than this is float noise rather than a difference in
# money.
SATOSHI = 1e-8


def rpc_error_from_body(response) -> str | None:
    """The daemon's own error message, or None if this response is not a JSON-RPC error.

    Returns a STRING rather than the raw error object, built from the code and the
    message, because that string is what lands in swaps.failed_reason and is read
    by a person deciding what to do about an unpaid customer. The code alone
    ("-13") is not that; the message alone loses which class of failure it was.

    None means "not a JSON-RPC error", which covers three cases that must all fall
    through to raise_for_status():
      - a 2xx success, where there is no error to report
      - a body that is not JSON at all, which is what a 401 returns
      - JSON without an `error` object

    Never raises. A diagnostic helper that can throw while explaining a throw makes
    the original failure unreachable, which is the shape this whole fix is about.
    """
    try:
        body = response.json()
    except ValueError:
        return None
    if not isinstance(body, dict):
        return None
    error = body.get("error")
    if not error:
        return None
    if isinstance(error, dict):
        message = error.get("message") or "(no message)"
        code = error.get("code")
        return f"{message} (rpc code {code})" if code is not None else str(message)
    return str(error)


class AddressOwnership(NamedTuple):
    """Whether this wallet holds the key for an address, AND why that is the answer.

    `verdict` keeps owns_address()'s three-valued contract -- True, False, or
    None for "not established" -- and `why` is what bare None could never carry.
    See RPCAdapter.address_ownership() for the 2026-10-01 measurement that made
    the reason a return value instead of a DEBUG log line.
    """

    verdict: bool | None
    why: str


class RPCAdapter:
    asset = ""

    # CAN THIS CHAIN BE A PAYOUT DESTINATION? One name across every adapter,
    # extended rather than replaced whenever a chain arrives, because a second
    # vocabulary for one concept is rule 8's bug with a delay on it.
    #
    # True here: a Bitcoin-derived daemon's own wallet signs, which is what
    # send_to_address() below calls. False on XRPAdapter and SolanaAdapter.
    #
    # Read through chains/registry.why_cannot_pay_out(), which uses
    # getattr(adapter, "can_spend", False) -- FAIL-CLOSED, so an adapter that
    # forgets to declare it is treated as unable to pay rather than assumed able.
    can_spend = True
    # Empty when can_spend is True. Otherwise a sentence that goes straight onto
    # the swap page beside a DISABLED pair. It lives on the adapter because the
    # adapter is what knows.
    payout_refusal = ""

    def __init__(self, user: str, password: str, host: str, port: int, wallet: str = "", timeout: float = 30.0):  # noqa: PLR0913, PLR0917 -- checked: these six ARE the RPC connection. They arrive as **Config.RPC[asset], a dict built for exactly this signature, so bundling them into an object would add a type without removing a parameter.
        self.user = user
        self.password = password
        self.host = host
        self.port = port
        self.wallet = wallet.strip("/")
        self.timeout = timeout

    @property
    def url(self) -> str:
        base = f"http://{self.host}:{self.port}"
        if self.wallet:
            return f"{base}/wallet/{self.wallet}"
        return base

    def call(self, method: str, *params):
        payload = {"jsonrpc": "2.0", "id": method, "method": method, "params": list(params)}
        response = requests.post(
            self.url,
            auth=(self.user, self.password),
            headers={"Content-Type": "application/json"},
            data=json.dumps(payload),
            timeout=self.timeout,
        )
        # THE BODY IS READ BEFORE THE STATUS, and the order is the whole fix.
        #
        # Bitcoin-derived daemons -- bitcoind, litecoind, gridcoinresearch -- return
        # HTTP 500 for an ORDINARY JSON-RPC error, with the real reason in the body's
        # `error` object. raise_for_status() therefore fired first and the
        # `data.get("error")` branch below was UNREACHABLE for every RPC error on
        # all three chains. The reason was parsed, then discarded, then replaced
        # with the status line.
        #
        # Measured on the operator's host 2026-09-26. A payout failed and
        # swaps.failed_reason recorded:
        #
        #     500 Server Error: Internal Server Error for url: http://127.0.0.1:25715/
        #
        # which says nothing. The daemon had sent the actual cause and this method
        # threw it away -- so a locked wallet, an insufficient balance and a bad
        # address were one indistinguishable line, on the fund path, in the field an
        # operator reads to decide what to do about a customer who was not paid.
        #
        # rpc_error_from_body() decides; raise_for_status() is the fallback for a
        # response that is NOT a JSON-RPC error (a 401 returns no JSON at all, and
        # "401 Client Error: Unauthorized" is genuinely the most informative thing
        # available for it).
        error = rpc_error_from_body(response)
        if error is not None:
            raise RPCError(error)
        response.raise_for_status()
        return response.json().get("result")

    def get_new_address(self, label: str) -> str:
        return self.call("getnewaddress", label)

    def owns_address(self, address: str) -> bool | None:
        """Whether THIS wallet holds the key for `address`. None when unanswerable.

        WHY A TERMINAL NEEDS THIS, measured on the operator 2026-09-26. Their first
        end-to-end XRP -> GRC swap paid 55.52645238 GRC to an address in their own
        wallet, so listtransactions reported a send AND a matching receive and the
        net movement was the 0.001 GRC fee. Everything worked. What they saw was
        "there's still no goddamn grc from xrp testnets", because a payout into the
        wallet it came out of looks exactly like nothing happening.

        The tools said "payout address ... VALID <- the GRC daemon accepts it", which
        is true and answers a different question: validateaddress says WELL-FORMED,
        never YOURS. For a customer's swap those are the right semantics -- a payout
        address should NOT be in the terminal's wallet -- so this is not a refusal,
        it is a fact worth putting on screen next to the address.

        THREE ANSWERS, and None is not a failure. `ismine` is a WALLET field, and
        Bitcoin Core moved wallet fields out of validateaddress into getaddressinfo
        in 0.18 -- so a daemon may answer the validity question and not this one.
        None means "not established", which is different from False, and a caller
        that prints "not yours" for None would be inventing the reassuring answer.

        Read-only: it calls the same two methods validate_address() already calls and
        touches no key.
        """
        return self.address_ownership(address).verdict

    def address_ownership(self, address: str) -> AddressOwnership:
        """owns_address() plus the REASON, which bare None could not carry.

        MEASURED 2026-10-01, and the thing it misled was me. A diagnostic asked
        this about a Gridcoin payout address from a shell with no GRC_RPC_*
        exported, so both calls hit 127.0.0.1:80 and died on
        ECONNREFUSED. owns_address() returned None, the harness printed
        "NOT-ESTABLISHED", and I read that as "this daemon has no `ismine` field"
        and said so to the operator as a fact. It was a transport failure. The
        daemon answers `ismine` perfectly well -- measured minutes later from the
        right shell:

            getaddressinfo  -> Method not found (rpc code -32601)
            validateaddress -> {"address": ..., "ismine": false, "isvalid": true}

        which is exactly the pre-0.17 surface owns_address()'s docstring already
        describes. The capability was never missing.

        TWO FAILURES WERE COLLAPSED INTO ONE None, AND THEY NEED OPPOSITE
        RESPONSES:

          the daemon could not be reached   -- fix the endpoint and ask again.
                The answer is unknown and knowable.
          the daemon answered without       -- this chain cannot answer. Asking
          `ismine`                             again changes nothing.

        The reasons were already being collected into `unanswered` and then
        logged at DEBUG, which is invisible by default, and dropped. That is
        precisely the blind-catch failure CLAUDE.md rule 12 names: a broad catch
        "is never legitimate when the caller cannot tell the failure from a real
        answer". The handler said so on the way out, at a level nobody sees.

        So the reason is now a RETURN VALUE. owns_address() keeps its bool|None
        contract for every existing caller, and anything that needs to tell an
        outage from a capability gap -- a diagnostic, an operator at a prompt --
        asks this instead. The log moved from DEBUG to WARNING for the same
        reason: a payout-address ownership check that cannot answer, on a chain
        whose daemon normally can, is a condition an operator needs to see. XRP
        does not reach this code (chains/xrp.py overrides owns_address to return
        None by design, because the XRP Ledger has no such question), so a
        warning here always means something is actually wrong.
        """
        unanswered = []
        for method in ("getaddressinfo", "validateaddress"):
            try:
                result = self.call(method, address)
            except Exception as exc:  # noqa: BLE001 -- checked: one method failing is not an answer, it is a reason to try the other, and `getaddressinfo` is ABSENT on Gridcoin by design (measured: rpc code -32601). The reason is collected and RETURNED in `why`, so the caller can tell a transport failure from a capability gap -- which is what rule 12 asks of a broad catch and what this function previously only whispered at DEBUG.
                unanswered.append(f"{method}: {exc}")
                continue
            if isinstance(result, dict) and "ismine" in result:
                return AddressOwnership(bool(result["ismine"]), f"{method} answered ismine")
            unanswered.append(f"{method}: answered, with no `ismine` field")
        why = "; ".join(unanswered)
        logger.warning(
            "address_ownership(%s) on %s: NOT ESTABLISHED -- %s. This is NOT 'not yours'; "
            "it is 'nobody answered'. If the reasons above are connection errors, the daemon "
            "endpoint is wrong or down and the question is still answerable.",
            address,
            self.asset or "this chain",
            why,
        )
        return AddressOwnership(None, why)

    def validate_address(self, address: str) -> bool:
        """Ask the daemon whether an address is valid.

        Returns True or False for an ANSWER, and RAISES if the daemon could not
        be asked. That distinction is the whole point, and it is what this
        method used to get wrong (rule 12's BLE001, fixed 2026-09-24).

        It previously ended with `except Exception: return False`, so a
        Litecoin daemon that was down, wedged, or refusing authentication
        produced the SAME answer as a genuinely malformed address: False. The
        caller -- services/swap_service.create_swap() -- turns that into
        `ValueError("Invalid LTC payout address")`, so the operator's response
        to an outage was to go and check the customer's address.

        NOTE FOR THE FUND-SAFETY REVIEW: this change cannot make a swap proceed
        that would not have proceeded before. Both the old behavior and the new
        one REFUSE; only the reason given changes, from a wrong one to a true
        one. tests/test_address_validation.py asserts exactly that, including
        that create_swap() still writes no swap row in either case.

        Two RPC names are tried because they belong to different daemon
        vintages: `validateaddress` carried `isvalid` on older builds, and
        Bitcoin Core moved wallet-related fields to `getaddressinfo` in 0.18.
        """
        errors = []
        for method in ("validateaddress", "getaddressinfo"):
            try:
                result = self.call(method, address)
            except Exception as exc:  # noqa: BLE001 -- checked: a failure of ONE method is not an answer, it is a reason to try the other. It is collected, not discarded, and if both fail the RPCError below carries them. Nothing here can return False because of a transport failure.
                errors.append(f"{method}: {exc}")
                continue
            if isinstance(result, dict) and "isvalid" in result:
                return bool(result["isvalid"])
            # A dict without `isvalid` is a daemon that answered about the
            # address at all, which older Gridcoin builds do for addresses they
            # recognize. Treat the answer as affirmative rather than inventing
            # a refusal.
            if isinstance(result, dict):
                return True
        raise RPCError(
            f"could not validate address with {self.asset or 'this'} daemon at {self.host}:{self.port}; "
            f"this is NOT a statement about the address: {'; '.join(errors) or 'no method returned a usable answer'}"
        )

    def get_balance(self) -> float:
        return float(self.call("getbalance"))

    def send_to_address(self, address: str, amount: float) -> str:
        """Pay `amount` to `address`. The amount is FITTED to the chain's precision first.

        IT USED TO SEND THE RAW FLOAT, AND BTC AND LTC COULD NOT HAVE PAID OUT AT
        ALL. Measured 2026-10-03 on the operator's regtest node. A quoted payout is
        a float with as many decimals as the arithmetic produced, and this is what
        went on the wire:

            2701.3495803173805   ->  "2701.3495803173805"    13 decimals
            0.00041198765432109  ->  "0.00041198765432109"   17 decimals

        Bitcoin Core parses an amount with ParseFixedPoint(value, 8), which REJECTS
        more than eight decimals rather than rounding. Proven with
        createrawtransaction, which runs the same parser and broadcasts nothing:

            0.00041198765432109  ->  error code -3, "Invalid amount"
            4.1e-07              ->  ACCEPTED, an output of 0x29 = 41 satoshis

        GRC->BTC and GRC->LTC are in ALLOWED_PAIRS and pass every gate, so the send
        would have failed with "Invalid amount" AFTER the customer's deposit was
        confirmed and irreversible -- the same ordering as the insufficient-funds
        failure the funding gate was added for that morning.

        GRIDCOIN MASKED IT, which is why five swaps settled without finding it: its
        RPC accepted 2701.3495803173805 and rounded on chain to 2701.34958032,
        exactly as the payout transaction the operator pasted shows.

        AND ONE EXPECTATION WAS REFUTED (rule 17): I expected exponent notation to
        fail too, since small payouts serialize as "4.1e-07". Core accepted it. So
        the defect is the decimal COUNT alone and no string formatting is needed --
        a fitted float serializes either within eight decimals or in an exponent
        form the parser handles.

        DOWN, NEVER NEAREST. chains/coin_amounts.fit_to_chain_precision() inherits
        that from amount_to_base_units(), whose own reason is that rounding up
        sends a fraction more than was quoted out of the hot wallet every time. The
        customer is short by at most one satoshi; the desk is never over.

        THE REDUCTION IS LOGGED, not silent. A payout the operator reconciles
        against a chain explorer differs from the quote in its last digit, and a
        number that changed without saying so is the defect rule 14 is about.
        """
        fitted, changed = fit_to_chain_precision(float(amount), self.asset)
        # A POSITIVE PAYOUT THAT FITS TO ZERO IS REFUSED HERE, NOT SENT. Found by
        # measuring the fit rather than by reasoning about it: 1e-09 BTC is a tenth
        # of a satoshi, so it quantizes to 0.0, and `sendtoaddress(address, 0.0)`
        # is a daemon error ("amount must be positive") dressed up as our own
        # arithmetic. The refusal names the cause instead, because "Invalid amount"
        # arriving from a daemon for a payout WE reduced to zero is the hardest
        # kind of message to trace back.
        #
        # THE RIGHT PLACE FOR THIS IS create_swap(), AND THAT IS THE OPERATOR'S
        # (rule 16). A payout refused here has already taken the customer's
        # deposit; refusing at creation costs a retry, which is the whole argument
        # services/payout_capacity.py was built on the same day. What stops it being
        # done here is that the threshold is not zero -- Bitcoin Core also rejects
        # an output below its DUST limit, which is a few hundred satoshis and
        # depends on the output type, and no part of this tree measures it yet.
        # Refusing exactly-zero is the half that needs no threshold.
        if fitted <= 0 < float(amount):
            raise RPCError(
                f"a {self.asset or 'this chain'} payout of {amount!r} fits to {fitted!r} at "
                f"{self.asset or 'this chain'}'s precision, which is nothing: the amount is smaller than "
                f"the chain's smallest unit. NOTHING was sent. {changed}"
            )
        if changed:
            logger.info("%s payout amount %s  <- %s", self.asset or "?", fitted, changed)
        return self.call("sendtoaddress", address, fitted)

    def measure_send_fee(self, address: str, amount: float) -> tuple[float | None, str]:
        """What THIS wallet would actually pay in fees to send `amount` now. (fee, how).

        MEASURED, NOT ESTIMATED, and that distinction is the whole reason this
        exists. The operator, 2026-10-03: "we need to match the scaling here."

        WHAT PROMPTED IT. Two payout fees were read off their own node the same
        day and the configured reserves were wrong in OPPOSITE directions:

            BTC   configured 0.00002   measured 0.00002820   41% too low
            LTC   configured 0.001     measured 0.00021483   4.7x too high

        and the reason they cannot both be a constant is in the transactions: the
        BTC send spent ONE input, the LTC send spent FIFTEEN, because that wallet
        is full of small mining outputs. A Bitcoin-style fee is bytes x rate and
        bytes scale with input count, so a flat reserve is wrong for every wallet
        except the one it was measured on, on the day it was measured.

        SO IT ASKS THE DAEMON INSTEAD OF MODELLING IT. createrawtransaction builds
        the one output, fundrawtransaction selects real inputs from the real UTXO
        set and REPORTS THE FEE it would pay. Nothing is signed, nothing is
        broadcast, and no UTXO is locked -- lockUnspents defaults to false, which
        is why this is safe to call from a read-only report while a payout worker
        is running. It is the same shape rule 5 asks for everywhere else: ask the
        authority rather than keep a second copy of its answer.

        THE AMOUNT IS FITTED FIRST, because createrawtransaction runs the SAME
        ParseFixedPoint(value, 8) that rejected a seventeen-decimal payout with
        "Invalid amount" -- so measuring an unfitted amount would fail on the
        measurement rather than on the send, which is the confusing direction.

        (None, why) WHEN IT CANNOT BE ASKED, never a fabricated number. An older
        daemon without fundrawtransaction, a wallet with nothing spendable, or a
        transport failure all return None with the reason, because a reserve
        chosen from a made-up figure is the defect this replaces.
        """
        fitted, _changed = fit_to_chain_precision(float(amount), self.asset)
        if fitted <= 0:
            return None, (f"{fitted!r} {self.asset} is not a sendable amount, so no fee could be "
                          f"measured for it")
        try:
            raw = self.call("createrawtransaction", [], {address: fitted})
        except Exception as error:  # noqa: BLE001 -- checked: returns None with the type and message, so no caller can mistake a failed measurement for a cheap fee. Every failure mode here (no such method, bad address, parser refusal) means the same thing to the caller: not established.
            return None, (f"createrawtransaction refused, so no fee was measured "
                          f"({type(error).__name__}: {str(error)[:120]})")
        try:
            funded = self.call("fundrawtransaction", raw)
        except Exception as error:  # noqa: BLE001 -- checked: as above. The commonest real case is a wallet with insufficient spendable balance, whose message says so and is passed through.
            return None, (f"fundrawtransaction refused, so no fee was measured "
                          f"({type(error).__name__}: {str(error)[:120]})")
        fee = (funded or {}).get("fee")
        if fee is None:
            return None, ("fundrawtransaction answered without a `fee` field, so the figure was NOT "
                          "established")
        # Reported as a POSITIVE cost. Core returns it positive here, unlike
        # gettransaction's `fee`, which is negative because it is a balance delta.
        return abs(float(fee)), (f"fundrawtransaction selected real inputs for a {fitted} {self.asset} "
                                 f"send and reported this fee; nothing was signed, broadcast or locked")

    def get_transaction(self, txid: str) -> dict:
        # Checked: `gettransaction` only knows wallet transactions, so its
        # failure means "ask about it as a raw transaction instead". The second
        # call is NOT wrapped -- if that fails too, the exception reaches the
        # caller rather than becoming an empty dict that get_confirmations()
        # would read as zero confirmations.
        try:
            return self.call("gettransaction", txid)
        except Exception:  # noqa: BLE001 -- checked: see the comment above; the fallback's own failure propagates.
            return self.call("getrawtransaction", txid, True)

    def get_confirmations(self, txid: str) -> int:
        tx = self.get_transaction(txid)
        return int(tx.get("confirmations", 0))

    def _raw_tx_for_vouts(self, txid: str) -> dict:
        return self.call("getrawtransaction", txid, True)

    def _extract_matching_vouts(self, txid: str, address: str, amount: float):
        # PROPOSAL MARKER, NOT AN ENDORSEMENT. The handler below FABRICATES a
        # deposit event -- vout 0, the amount the caller already believed, and
        # a confirmation count from a second RPC -- and returns it in the same
        # shape as a real one. services/deposit_service.py cannot tell the two
        # apart, so a transaction this adapter failed to decode is credited
        # from the wallet's summary rather than from its outputs.
        #
        # That is rule 12's BLE001 in its expensive form and it is NOT fixed
        # here, because this value feeds the confirmation comparison that
        # releases a payout: making it raise would stall swaps that credit
        # today. Which of the two failure modes the operator wants is a fund
        # decision (rule 16).
        try:
            raw = self._raw_tx_for_vouts(txid)
        except Exception:  # noqa: BLE001 -- checked: see the proposal marker above. The caller CANNOT distinguish this synthetic event from a real one, which is exactly why it is being handed over rather than patched.
            return [{"txid": txid, "vout": 0, "address": address, "amount": float(amount), "confirmations": self.get_confirmations(txid)}]
        matches = []
        confirmations = int(raw.get("confirmations", 0))
        for vout in raw.get("vout", []):
            # MATCHED ON EITHER DAEMON'S FIELD SHAPE, and this was the FOURTH
            # copy of one defect. This line read
            #
            #     addresses = script_pub_key.get("addresses") or []
            #     if address in addresses and ...
            #
            # and `addresses` (plural) was deprecated in Bitcoin Core 0.20 and
            # REMOVED in 22.0. On a Core 28.1 node it is simply absent, so the
            # list was empty for every output, nothing ever matched, and this
            # function fell through to the fabricated event below -- crediting
            # a deposit at vout 0 with the amount the WALLET SUMMARY reported
            # instead of the amount its outputs actually carry.
            #
            # The same defect was found and fixed in modules/utils.
            # wait_for_tx_output() and in LTCClient.create_contract() on
            # 2026-09-25 and was never carried across to here, because nothing
            # pointed from one copy to the others. swap_terminal/
            # script_pub_key.py is now the one place that knows, and it is at
            # the package root with no third-party imports precisely so this
            # file can reach it without importing the atomic-swap path.
            #
            # A MIGRATION NOTE, because this changes which rows appear rather
            # than only whether they are right. On a Core 28.1 node every
            # existing deposit_events row was written by the fabricated branch
            # and carries vout=0. upsert_deposit_event() keys on
            # (asset, txid, vout), so a deposit whose real output is at vout=N
            # now inserts a SECOND row, and refresh_swap_from_chain() sums
            # every row for the swap -- which double-counts and sends the swap
            # to `under_review` rather than to a payout. That direction is a
            # halt, not a release, but it is still armed state for any swap
            # open across the deploy and it belongs to the operator (rule 16):
            # the fix for those rows is to delete the fabricated vout=0 row for
            # any swap still open, and this comment is where that is written
            # down rather than discovered.
            script_pub_key = vout.get("scriptPubKey", {})
            value = float(vout.get("value", 0))
            if pays_address(script_pub_key, address) and abs(value - float(amount)) < SATOSHI:
                matches.append({
                    "txid": txid,
                    "vout": int(vout.get("n", 0)),
                    "address": address,
                    "amount": value,
                    "confirmations": confirmations,
                })
        if matches:
            return matches
        return [{"txid": txid, "vout": 0, "address": address, "amount": float(amount), "confirmations": confirmations}]

    def find_deposits_to_address(self, address: str, tx_limit: int = 500, skip_txids=frozenset()):
        """Credits to one address, as deposit events. ONE `listtransactions` call.

        A docstring at all is new on 2026-10-01: this method had only inline
        comments, on the one function every Bitcoin-family deposit passes through.

        SETTLED_TXIDS IS ACCEPTED AND IGNORED, and that is the point of it being
        in the signature. Bitcoin-family discovery is a single listtransactions
        call for the whole wallet, so there is no per-transaction cost to avoid
        and nothing to skip. chains/solana.py is the one implementation that uses
        it, because Solana discovery costs one getTransaction PER TRANSACTION and
        re-reading settled history rate-limited a real deposit out of being
        credited -- the measurement is in that method's comment.

        It is a parameter here rather than a branch in the caller so that
        services/deposit_service.py calls every adapter the same way (rule 8). A
        `if isinstance(adapter, SolanaAdapter)` in the deposit path would be the
        chain-specific knowledge in the wrong layer that rule 10 is about.
        """
        results = []
        # Checked: the 5-argument form (with include_watchonly) is not
        # accepted by every daemon vintage, so its failure means "retry with
        # the short form". The retry is NOT wrapped: if that fails too, the
        # exception reaches the worker rather than becoming an empty deposit
        # list, which would read as "no deposit has arrived" and stall the swap
        # silently.
        try:
            txs = self.call("listtransactions", "*", tx_limit, 0, True)
        except Exception:  # noqa: BLE001 -- checked: see above; the retry's failure propagates.
            txs = self.call("listtransactions", "*", tx_limit)
        for tx in txs or []:
            if tx.get("category") != "receive":
                continue
            if tx.get("address") != address:
                continue
            amount = abs(float(tx.get("amount", 0)))
            txid = tx.get("txid")
            if not txid or amount <= 0:
                continue
            results.extend(self._extract_matching_vouts(txid, address, amount))
        deduped = {}
        for item in results:
            deduped[(item["txid"], item["vout"])] = item
        return list(deduped.values())
