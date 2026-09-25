"""The XRP Ledger adapter: the same five-method contract, a third transport.

Role: module (chain binding; transport plus the ledger's own vocabulary)
Reads: a rippled server -- account_info, account_tx, server_info
Writes: nothing to disk, nothing to the ledger. send_to_address() REFUSES;
        see WHY PAYOUTS ARE NOT IMPLEMENTED below.
Can move funds: NO. This module holds no key, reads no key path, and imports
        nothing that can sign. The refusal is an absence, not a flag.
Mainnet-safe: yes to import and to every method it implements. It identifies
        the network from the server rather than from the URL.

THE HONEST STATUS

xrpl.org and s1.ripple.com were unreachable from the environment this was
written in (no response through the proxy), so every RPC METHOD NAME and every
RESPONSE FIELD NAME here came from prior knowledge and was NOT read from the
published API reference. If one is wrong, the tests still pass -- they seed the
shapes this file expects -- and it fails on the operator's first real call.
That is the same limitation chains/monero.py carried, and it was resolved there
by fetching the docs from the operator's host, which is the first thing to do
here too.

What IS measured: chains/xrp_address.py, verified against the ledger's own
ACCOUNT_ZERO and ACCOUNT_ONE constants, 5,000 round trips and 33 single
character mutations, all rejected by checksum. And chains/xrp_payments.py's
filtering, tested directly. The wire format is the hypothesis; the arithmetic
and the rules are not.

TWO THINGS ABOUT rippled's JSON-RPC THAT ARE EASY TO GET WRONG

1. `params` IS AN ARRAY CONTAINING ONE OBJECT. Not an object, as Monero's
   JSON-RPC takes, and not a positional list as bitcoind takes. The shape is
   {"method": "...", "params": [{...}]} and sending either of the other two
   forms produces an error about parameter parsing, several layers from the
   cause.

2. AN ERROR ARRIVES AS HTTP 200. rippled reports failures inside the response
   body -- result.status == "error" with result.error naming it -- while the
   HTTP status stays 200. A client that only checks raise_for_status() treats
   every failure as a success and reads whatever fields happen to be absent as
   empty. That is the shape this whole codebase keeps being bitten by, so it is
   checked explicitly below.

WHY PAYOUTS ARE NOT IMPLEMENTED, AND WHY THAT IS NOT LAZINESS

rippled removed transaction signing from its public API. There is no `sign`
method to call on a remote server, deliberately: signing there would mean
sending a secret key over the wire. So paying out XRP means THIS process holds
a signing key and signs locally.

That is the same custody question chains/solana.py handed back, and it is
answered the same way: not here, and not without the operator deciding. So
send_to_address() refuses, and the refusal is structural -- this module imports
no signing library and reads no key path, so it could not sign if it tried.

get_new_address() also refuses, but for a happier reason. XRP does not need
one: a DESTINATION TAG is a per-swap identifier on a single account, costs
nothing, creates no key, and is what every exchange on this ledger uses. The
attribution problem chains/solana.py had to hand back does not exist here --
what it needs instead is a tag allocator wired into swap creation, which is a
services/ change rather than an adapter one.
"""

from __future__ import annotations

import json

import requests

from .xrp_address import describe_address, is_valid_classic_address, looks_like_x_address
from .xrp_payments import XRPPaymentError, deposit_events_from_transactions
from .xrp_units import (
    REFERENCE_BASE_RESERVE_XRP,
    describe_min_confirmations,
    from_drops,
    validate_min_confirmations,
)

# UNVERIFIED METHOD NAMES -- see THE HONEST STATUS above.
_METHOD_ACCOUNT_INFO = "account_info"
_METHOD_ACCOUNT_TX = "account_tx"
_METHOD_SERVER_INFO = "server_info"


class XRPRPCError(Exception):
    """A rippled call failed, or the server returned an error in a 200 body.

    Raised rather than swallowed: on this path a failure that returns a
    plausible value -- 0 from get_balance, [] from find_deposits_to_address --
    is worse than one that stops the caller, because an empty deposit list
    reads as "no money has arrived yet" and stalls a swap silently forever.
    """


class XRPPayoutDisabled(XRPRPCError):
    """send_to_address() was called. This module cannot sign and will not try."""


class XRPAdapter:
    asset = "XRP"

    def __init__(
        self,
        url: str = "",
        min_confirmations: int = 1,
        timeout: float = 30.0,
    ):
        if not url:
            raise XRPRPCError("XRPAdapter needs a rippled JSON-RPC url; there is no sensible default")
        self.url = url
        self.timeout = float(timeout)
        # Validated at CONSTRUCTION, not at poll time. A threshold no XRP
        # payment can reach would leave every deposit below it forever, with
        # nothing in any log saying why (see chains/xrp_units.py).
        self.min_confirmations = validate_min_confirmations(min_confirmations)

    def endpoint_line(self) -> str:
        """The worker banner's XRP line (rule 14).

        Says "validated ledger" rather than a bare number so the figure cannot
        be read as a block depth the way the BTC/LTC/GRC lines are, and states
        that payouts are off -- an operator who expects this chain to pay needs
        to learn that here rather than from a refusal hours later.
        """
        return (
            f"  XRP  rpc={self.url} min_confirmations={describe_min_confirmations(self.min_confirmations)} "
            f"payouts=REFUSED (holds no signing key)"
        )

    def call(self, method: str, params: dict | None = None):
        """One rippled JSON-RPC call. Note the params shape and the 200-with-error.

        See the two numbered points in this module's docstring: `params` is a
        LIST containing one object, and a failure comes back with HTTP 200 and
        result.status == "error". Both are checked here rather than trusted.
        """
        response = requests.post(
            self.url,
            headers={"Content-Type": "application/json"},
            data=json.dumps({"method": method, "params": [params or {}]}),
            timeout=self.timeout,
        )
        response.raise_for_status()
        payload = response.json()
        result = payload.get("result")
        if not isinstance(result, dict):
            raise XRPRPCError(
                f"{method}: response has no `result` object. NOT treated as an empty answer -- see "
                f"this class's docstring for why a plausible empty value is the more expensive failure."
            )
        if result.get("status") == "error":
            raise XRPRPCError(
                f"{method}: {result.get('error')} -- {result.get('error_message', 'no message')}. "
                f"NOTE rippled returns errors with HTTP 200, so this was NOT caught by the status code."
            )
        return result

    def get_new_address(self, label: str) -> str:
        """Refuses. XRP attributes deposits by destination tag, not by address."""
        raise XRPRPCError(
            f"this adapter does not derive per-swap XRP addresses, and for {label!r} it should not. "
            f"The XRP Ledger attributes deposits with a DESTINATION TAG on a single account: an "
            f"integer per swap, no new key, no funding reserve, and it is what every exchange on this "
            f"ledger uses. What is needed is a tag allocator in services/swap_service.py -- a change "
            f"to how a swap is created, not to this adapter. Deriving a fresh account instead would "
            f"cost a base reserve per swap AND put a signing key per swap on this host."
        )

    def validate_address(self, address: str) -> bool:
        """Checksum-verified locally, with no network call.

        Unlike every other adapter here this asks no daemon, because it does
        not need to: an XRPL address carries a double-SHA256 checksum and
        chains/xrp_address.py verifies it against the ledger's own constants.
        Asking the server would answer a DIFFERENT question -- whether the
        account exists -- and an unfunded but perfectly valid address is
        exactly what a first payment creates.

        An X-address is accepted as valid but flagged by describe_address():
        it carries its own destination tag, which the payout path would have to
        decode rather than take a tag separately.
        """
        if looks_like_x_address(address):
            return True
        return is_valid_classic_address(address)

    def describe_address(self, address: str) -> str:
        """One operator-readable line about an address (rule 14)."""
        return describe_address(address)

    def get_balance(self) -> float:
        raise XRPRPCError(
            "get_balance() needs the hot-wallet account this terminal pays out from, and this adapter "
            "has none -- payouts are refused (see the class docstring). Wire an account here only "
            "alongside the signing decision, not before it."
        )

    def account_balance(self, address: str) -> float:
        """The spendable balance of one account, in XRP.

        NOT called get_balance() on purpose: that name is the adapter contract
        services/payout_service.py uses to decide whether a payout can be
        funded, and answering it while this adapter cannot pay would be a
        method that lies about what it enables.

        The reserve is SUBTRACTED, because it is not spendable. Every funded
        XRPL account must retain a base reserve, and reporting the raw balance
        would let a caller commit to a payment the ledger then refuses -- the
        gate-versus-send disagreement in a third form.
        """
        result = self.call(_METHOD_ACCOUNT_INFO, {"account": address, "ledger_index": "validated"})
        data = result.get("account_data") or {}
        raw = data.get("Balance")
        if not isinstance(raw, str):
            raise XRPRPCError(
                f"{_METHOD_ACCOUNT_INFO} returned Balance={raw!r}; the XRP Ledger sends drop counts as "
                f"JSON STRINGS, specifically so a client cannot round them through a double. A "
                f"non-string here means the response shape is not what this adapter expects."
            )
        return from_drops(raw) - float(self.reserve_xrp())

    def reserve_xrp(self) -> float:
        """The base reserve, ASKED OF THE SERVER rather than hardcoded.

        It has changed more than once -- 20 XRP, then 10, then 1 -- so a
        compiled-in constant would eventually make this terminal believe it has
        spendable balance it does not. chains/xrp_units.py carries a reference
        figure for a banner and explicitly not for this.
        """
        result = self.call(_METHOD_SERVER_INFO)
        ledger = (result.get("info") or {}).get("validated_ledger") or {}
        reserve = ledger.get("reserve_base_xrp")
        if reserve is None:
            raise XRPRPCError(
                f"{_METHOD_SERVER_INFO} did not report validated_ledger.reserve_base_xrp. NOT falling "
                f"back to the reference value {REFERENCE_BASE_RESERVE_XRP} XRP: the reserve is a "
                f"network parameter that has changed before, and guessing it wrong overstates "
                f"spendable balance."
            )
        return float(reserve)

    def network(self) -> str:
        """Which network the SERVER says it is on, not which the URL implies.

        A hostname can lie or be reused; the server's own network id cannot be
        mistaken by the client. solana_chain_check.py makes the same
        distinction with getGenesisHash and for the same reason.
        """
        result = self.call(_METHOD_SERVER_INFO)
        info = result.get("info") or {}
        return str(info.get("network_id", info.get("build_version", "unknown")))

    def find_deposits_to_address(self, address: str) -> list[dict]:
        """Validated payments to one account, as deposit events.

        The filtering and every refusal live in chains/xrp_payments.py -- most
        importantly that the credited figure comes from meta.delivered_amount
        and never from Amount, which is the partial payment exploit. This
        method is transport.
        """
        result = self.call(
            _METHOD_ACCOUNT_TX,
            {"account": address, "ledger_index_min": -1, "ledger_index_max": -1, "binary": False},
        )
        try:
            scan = deposit_events_from_transactions(
                result.get("transactions") or [], address, self.min_confirmations
            )
        except XRPPaymentError as error:
            raise XRPRPCError(f"deposit scan for {address} refused: {error}") from error
        for line in scan.deferred:
            print(f"  XRP deferred  {line}", flush=True)
        return scan.events

    def send_to_address(self, address: str, amount: float) -> str:
        """Refuses, structurally. This module cannot sign.

        rippled removed signing from its public API, so paying XRP means
        holding a key in THIS process. That is the operator's decision (rule
        16), and until it is made the refusal is an absence rather than a flag:
        no signing library is imported here and no key path is read, so this
        could not sign if the check were deleted.
        """
        raise XRPPayoutDisabled(
            f"XRP payouts are not implemented, so {amount} XRP was NOT sent to {address} and no "
            f"network call was made. rippled has no remote `sign` method -- deliberately, since that "
            f"would mean sending a secret key over the wire -- so paying out requires a signing key in "
            f"this process. That is a custody decision for the operator, the same one chains/solana.py "
            f"hands back."
        )
