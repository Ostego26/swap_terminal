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
import sys
from pathlib import Path

import requests

# The application imports its own modules rootlessly, so a file two
# directories down needs the package root on the path to reach a leaf beside
# microfortnights.py. This is rule 10's layout gap, the same one
# workers/deposit_watcher.py names; fixing it properly means moving entry
# points to the root, which is a large diff with no behavioral benefit on a
# key-holding system.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from script_pub_key import pays_address


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


class RPCAdapter:
    asset = ""

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
        return self.call("sendtoaddress", address, float(amount))

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

    def find_deposits_to_address(self, address: str, tx_limit: int = 500):
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
