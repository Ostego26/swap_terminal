"""The Monero adapter: the same five-method contract, a different transport.

Role: module (chain binding; transport plus the wallet's own vocabulary)
Reads: monero-wallet-rpc -- create_address, validate_address, get_balance,
       get_transfers, and transfer when spending is enabled
Writes: nothing to disk. THE WALLET, when get_new_address() derives a
       subaddress, and THE CHAIN, when send_to_address() is permitted and
       called.
Can move funds: CONDITIONALLY, and it is off by default. send_to_address()
       refuses unless the adapter was constructed with can_spend=True. See
       WHY VIEW-ONLY IS THE DEFAULT below -- this is the one chain here where
       watching and spending can be separated, and the default takes it.
Mainnet-safe: the read methods are. Nothing in this file selects a network; it
       talks to whatever wallet the configured port belongs to, and
       validate_address() is what asks the daemon which network that is.

THE HONEST STATUS -- READ THIS FIRST

**No part of this file has been run against a monero-wallet-rpc** -- though its
names are now confirmed against the published spec; see CONFIRMED below. Measured
2026-09-25 from the environment it was written in: `getmonero.org` returns
`403 CONNECT tunnel failed` through this container's proxy, so Monero's RPC
documentation could not be opened and no daemon could be downloaded, let alone
run.

Therefore every RPC METHOD NAME and every RESPONSE FIELD NAME below was written
from prior knowledge and NOT verified. If one of them is wrong, every test in
tests/test_monero_adapter.py still passes -- they seed the responses this file
expects -- and the adapter fails on the operator's first real call. That is the
same limitation chains/solana.py carries and states, and for the same reason.

What that means in practice, stated as rule 17 asks: the arithmetic in
chains/monero_units.py and the filtering in chains/monero_transfers.py are
MEASURED -- they are pure functions and their tests call them directly. The
wire format is a HYPOTHESIS. The two must not be read in the same voice.

Every unverified name is gathered in one block per file -- `_METHOD_*` below,
and the `FIELD_*` constants in chains/monero_transfers.py -- so that confirming
them against a real stagenet wallet is a read of two short blocks, and fixing a
wrong one is a one-line change rather than a hunt.

CONFIRMED AGAINST THE OFFICIAL SPEC ON 2026-09-25, AND THE STATUS ABOVE HAS
MOVED BECAUSE OF IT.

docs.getmonero.org/rpc-library/wallet-rpc/ was unreachable from the machine
this was written on (403 through the proxy) and IS reachable from the
operator's host. Fetched there, 380,318 bytes, and every name this code
depends on was read out of the published request/response spec:

    method            create_address, validate_address, get_balance,
                      get_transfers, transfer, get_address     ALL PRESENT
    get_transfers     takes `in` as a boolean input and returns
                      `in - array of transfers`                CONFIRMED
    transfer entry    address, amount, amounts, confirmations,
                      double_spend_seen, subaddr_index, txid,
                      type, unlock_time, locked                ALL PRESENT
    subaddr_index     "JSON object containing the major & minor
                      subaddress index"                        EXACT SHAPE
    type              'Transfer type: "in"'                    CONFIRMED
    validate_address  any_net_type "Defaults to false ... only consider an
                      address valid if it belongs to the network on which
                      the rpc-wallet's current daemon is running"
                                                               THE DEFAULT
                      THIS CODE RELIES ON, CONFIRMED
    create_address    returns `address` and `address_index`    CONFIRMED

CONFIRMED AGAINST A RUNNING WALLET ON 2026-09-27, AND THIS IS THE FIRST TIME
ANY OF IT CAME FROM A DAEMON RATHER THAN A DOCUMENT.

monero_chain_check.py was run on the operator's host against
monero-wallet-rpc --stagenet on port 38083. Point 1 below said "documentation
is not a daemon"; for two of the three methods it no longer applies:

    get_balance       ANSWERED. 5 fields in the result, and `balance` and
                      `unlocked_balance` both read -- so the two names
                      get_balance() depends on are confirmed by a wallet.
                      Reported 0.0 XMR total and 0.0 unlocked.
    validate_address  ANSWERED, and the reply carried `nettype` as this code
                      reads it. The daemon said STAGENET for the wallet's own
                      primary address, which is the mechanism validate_address()
                      exists for -- identifying the network FROM THE DAEMON
                      rather than from a port number. It also independently
                      confirms that
                      537wxk1vzCDembafqWxfTgNcZGoK6rAsbP1JHKiQkjYLLzNDtgMTUKACBguFzx2XnFf1FQVqogcjd9LXTQ52jGiVBV52C1V
                      is that wallet's primary address on stagenet, which
                      chains/monero_keys.py had only established offline from
                      its Keccak checksum.
    get_transfers     ANSWERED, and returned NO `in` key at all, because the
                      wallet has no incoming transfers. That is the documented
                      behavior and this code already handles it
                      (`result.get("in") or []`), so it is not a failure -- but
                      it CONFIRMS NOTHING about the ten transfer field names.

THE TEN TRANSFER FIELD NAMES REMAIN UNCONFIRMED, and the run said so in those
words rather than printing a clean summary: `txid`, `amount`, `address`,
`amounts`, `subaddr_index`, `confirmations`, `type`, `unlock_time`, `locked`
and `double_spend_seen` were each marked `unconfirmed (no transfers to look
at)`, the deposit scan in step 4 was skipped, and the script exited 3 --
inconclusive -- so no wrapper can read that run as a pass. Rule 14's "make
'did nothing' look different from 'did work'", working as intended.

The funding is the blocker and nothing else is. stagenet-faucet.xmr-tw.org was
rate limited on 2026-09-27 and the balance of 0.0 proves it had not already
paid out, which was the cheap thing worth checking first. See
docs/monero_stagenet_funding.md.

ALL TEN TRANSFER FIELD NAMES CONFIRMED AGAINST A REAL WALLET, 2026-09-27.

This is the sentence this file has been waiting for since it was written. A
stagenet faucet (cypherfaucet.com) sent 0.01 XMR to the wallet's primary
address, txid 63e58bb16a12bb3b51608a8b7d0367c06375778b5deb9ce82f3ab25fc93a7b4e,
and monero_chain_check.py ran on the operator's host against
monero-wallet-rpc --stagenet on port 38083. Over one real incoming transfer:

    txid  amount  address  amounts  subaddr_index  confirmations
    type  unlock_time  locked  double_spend_seen      ALL PRESENT, all 10

    exit status 0, and the script's own words: "PASSED: every method and field
    the adapter depends on was observed, over 1 real transfer(s)."

The three items this section used to list as NOT established are resolved,
partly resolved, and resolved-against-the-spec, in that order:

1. "DOCUMENTATION IS NOT A DAEMON" -- SETTLED. The names above came out of a
   wallet answering a real call, not out of a page. Nothing here rests on the
   published spec any more.
2. THE MULTI-OUTPUT AMOUNT -- STILL OPEN, and narrowed. The observed transfer
   carried a single-element `amounts`, so the relationship between `amount` and
   a MULTI-element `amounts` is still unobserved and
   _reject_amount_disagreement() in chains/monero_transfers.py still earns its
   place as a runtime check. A transfer with several outputs to the same
   subaddress is what would settle it.
3. `locked` -- RESOLVED, AND THE SPEC IS THE THING THAT IS WRONG. The published
   description reads `locked - boolean; Is the output spendable`. The observed
   transfer had **3 confirmations, unlock_time=0, and locked=True**, while
   get_balance reported total 0.01 and unlocked 0.0 -- so `locked` true means
   NOT spendable, which is what the field's NAME says and the opposite of what
   its description says. This code treats truthy `locked` as not-yet-creditable
   and that is now MEASURED to be correct rather than a cautious guess. The
   deposit scan deferred the transfer and printed why:

     deferred 63e58bb1... 0.01 XMR unlock_time=0 locked=True
       <- arrived, NOT creditable until it unlocks

SEVEN FIELDS THE WALLET SENDS THAT THIS ADAPTER IGNORES, named because a list of
what is ignored is the other half of a list of what is read:

    fee  height  note  payment_id  subaddr_indices
    suggested_confirmations_threshold  timestamp

Two of those are worth something and neither is wired in. `fee` would let a
deposit record what the sender paid, and `height` is a cheaper reorg check than
re-reading `confirmations` every poll. `suggested_confirmations_threshold` is the
wallet's own opinion about how many confirmations to wait for, which this code
currently answers with a constant. None of that is a defect today; it is named
so the next reader does not have to re-derive the gap.

One smaller measurement, recorded because it moved: get_balance returned SIX
fields on a funded wallet where the 2026-09-27 empty-wallet run saw five. The two
names this code reads, `balance` and `unlocked_balance`, are in both.


WHY THIS IS A SIBLING OF RPCAdapter AND NOT A SUBCLASS OF IT

chains/bitcoin.py, litecoin.py and gridcoin.py are four lines each: they set
`asset` and inherit everything. That works because the three are the same
daemon with different constants. Monero is not, and four things measured in
chains/base.py are wrong for it rather than merely different:

    base.py:86   auth=(user, password)          HTTP basic; Monero uses digest
    base.py:84   "params": list(params)         positional; Monero takes named
    base.py:77   http://host:port/wallet/<name> Monero serves at /json_rpc
    base.py:169  deposits found by vout         Monero publishes no vouts

Inheriting and overriding all four would leave a class whose parent contributes
nothing but confusion about which half is in force. So: same contract, own
implementation, and each file names the other (rule 8).

THE CONTRACT, MEASURED RATHER THAN ASSUMED

chains/solana.py's author walked the AST of services/, workers/, routes/ and
app.py for every attribute accessed on an adapter, and found FIVE methods:
get_new_address, validate_address, get_balance, send_to_address and
find_deposits_to_address. `get_confirmations` is NOT among them -- it is
base.py's own helper, and confirmations reach the caller inside the event
dicts. This file therefore implements five methods and does not carry a sixth
for symmetry's sake, because a method with no caller is rule 9's dead code
arriving new.

WHY VIEW-ONLY IS THE DEFAULT, AND WHY THAT IS NOT AVAILABLE TO THE OTHER CHAINS

Monero separates the view key from the spend key. A wallet opened from the view
key alone can see every incoming transfer -- it can do everything
find_deposits_to_address() needs -- and is cryptographically incapable of
building a transaction. Bitcoin, Litecoin and Gridcoin have no equivalent: in
chains/base.py, `send_to_address` is inherited by every subclass and the
package docstring in chains/__init__.py warns about exactly that.

So the deposit watcher can run against a wallet that cannot be robbed, and only
the payout worker needs one that can spend. `can_spend` defaults to FALSE to
make that the path of least resistance rather than a configuration an operator
has to discover. Turning it on is deliberate, and a wallet that cannot spend
fails at construction-time configuration rather than three layers into a
payout.
"""

from __future__ import annotations

import json

import requests
from requests.auth import HTTPDigestAuth

from .monero_transfers import MoneroTransferError, deposit_events_from_transfers
from .monero_units import (
    describe_min_confirmations,
    effective_min_confirmations,
    from_atomic,
    to_atomic,
)

# ---------------------------------------------------------------------------
# UNVERIFIED RPC METHOD NAMES. See THE HONEST STATUS above. Confirming these
# against `monero-wallet-rpc --stagenet` is the one thing this file most needs
# and the one thing it could not have.
# ---------------------------------------------------------------------------
_METHOD_CREATE_ADDRESS = "create_address"
_METHOD_VALIDATE_ADDRESS = "validate_address"
_METHOD_GET_BALANCE = "get_balance"
_METHOD_GET_TRANSFERS = "get_transfers"
_METHOD_TRANSFER = "transfer"

# monero-wallet-rpc serves JSON-RPC at this path and nothing else at the root,
# which is why this is a constant rather than an f-string with a wallet name in
# it the way base.py's url property is: one daemon serves exactly one wallet.
_JSON_RPC_PATH = "/json_rpc"


class MoneroRPCError(Exception):
    """A wallet RPC call failed, or the daemon returned an error object.

    Raised rather than swallowed, for the reason chains/base.py's RPCError
    gives and which applies with more force here: on this path a failure that
    returns a plausible value -- False from validate_address, 0 from
    get_balance, [] from find_deposits_to_address -- is worse than one that
    stops the caller, because the caller cannot tell it from a real answer. An
    empty deposit list in particular reads as "no money has arrived yet" and
    stalls a swap forever without printing anything.
    """


class MoneroSpendDisabled(MoneroRPCError):
    """send_to_address() was called on a view-only adapter.

    Its own class rather than a bare MoneroRPCError because it is not a
    failure to reach the wallet -- it is this process declining, and an
    operator reading a log needs to tell "the daemon is down" from "you did not
    turn this on".
    """


class MoneroAdapter:
    asset = "XMR"

    def __init__(  # noqa: PLR0913, PLR0917 -- checked: these ARE the wallet connection, and they arrive as **Config.RPC["XMR"], a dict built for exactly this signature. Bundling them into an object would add a type without removing a parameter, which is the same judgment base.py:68 records for its own six.
        self,
        host: str = "127.0.0.1",
        port: int = 18082,
        user: str = "",
        password: str = "",
        account_index: int = 0,
        min_confirmations: int = 10,
        can_spend: bool = False,
        timeout: float = 30.0,
    ):
        self.host = host
        self.port = int(port)
        self.user = user
        self.password = password
        # Every subaddress this terminal derives belongs to one account, so the
        # minor index alone identifies a swap. chains/monero_transfers.py uses
        # that minor index as `vout`, and that is only sound while the account
        # is fixed -- which is why it is configuration rather than a per-call
        # argument.
        self.account_index = int(account_index)
        self.can_spend = bool(can_spend)
        # The sibling of can_spend, named the same on every adapter -- see
        # chains/base.RPCAdapter.can_spend. An INSTANCE attribute here rather than a
        # class one, because unlike XRP and Solana this chain's answer is a
        # configuration value (XMR_WALLET_CAN_SPEND) rather than a structural
        # absence, and it can differ between two adapters in one process.
        self.payout_refusal = "" if self.can_spend else (
            "has an adapter but is view-only: it was constructed with can_spend=False, so "
            "send_to_address() refuses. Set XMR_WALLET_CAN_SPEND=1 in the environment this "
            "process is started with, and only for a wallet you intend to pay out from."
        )
        self.timeout = float(timeout)
        # Clamped, not trusted. See chains/monero_units.py: a setting below the
        # ten-block consensus lock does not buy a faster payout, it buys a
        # payout that fails at transfer time.
        self.min_confirmations = effective_min_confirmations(min_confirmations)
        self.configured_min_confirmations = int(min_confirmations)

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}{_JSON_RPC_PATH}"

    def describe_confirmations(self) -> str:
        """The confirmation line for a worker banner (rule 14)."""
        return describe_min_confirmations(self.configured_min_confirmations)

    def endpoint_line(self) -> str:
        """The worker banner's XMR line (rule 14).

        Prints host, port, account index, the confirmation threshold actually
        enforced, and WHETHER THIS WALLET CAN SPEND -- and deliberately never
        prints `user` or `password`, which are one key away in the same config
        dict. The spend flag is on the banner rather than left to be inferred
        because it is the difference between a watcher and a payout wallet, and
        an operator who starts the wrong one finds out either from this line or
        from a payout that refuses hours later.
        """
        spend = "CAN SPEND  <- this wallet is armed for payouts" if self.can_spend else "view-only (cannot send)"
        return (
            f"  XMR  rpc={self.host}:{self.port} account={self.account_index} "
            f"min_confirmations={self.describe_confirmations()} {spend}"
        )

    def call(self, method: str, params: dict | None = None):
        """One JSON-RPC 2.0 call, with named parameters and digest auth.

        Digest, not basic: monero-wallet-rpc authenticates with HTTP digest,
        so the `auth=(user, password)` tuple chains/base.py:86 uses would be
        sent as basic and rejected. When no user is configured the request goes
        out unauthenticated, which is what `--disable-rpc-login` expects; that
        is a deployment choice and this adapter does not second-guess it.
        """
        response = requests.post(
            self.url,
            auth=HTTPDigestAuth(self.user, self.password) if self.user else None,
            headers={"Content-Type": "application/json"},
            data=json.dumps({"jsonrpc": "2.0", "id": method, "method": method, "params": params or {}}),
            timeout=self.timeout,
        )
        response.raise_for_status()
        payload = response.json()
        if payload.get("error"):
            raise MoneroRPCError(f"{method}: {payload['error']}")
        if "result" not in payload:
            raise MoneroRPCError(
                f"{method}: the wallet returned no `result` and no `error`. NOT treated as an empty "
                f"answer -- see this class's docstring for why a plausible-looking empty value is the "
                f"more expensive failure here."
            )
        return payload["result"]

    def get_new_address(self, label: str) -> str:
        """Derive a fresh subaddress under the configured account.

        This is where Monero is easier than Solana rather than harder, and it
        is worth saying so next to the code: chains/solana.py's
        get_new_address() REFUSES, because Solana has no way to hand out a
        per-swap deposit address without either holding a key per swap or
        matching deposits by memo. A subaddress needs neither. It costs
        nothing, reveals nothing on chain, and its index is the swap.
        """
        result = self.call(_METHOD_CREATE_ADDRESS, {"account_index": self.account_index, "label": label})
        address = result.get("address")
        if not address:
            raise MoneroRPCError(
                f"{_METHOD_CREATE_ADDRESS} returned no address for label {label!r}. NOT retried here: a "
                f"second derivation would leave the first subaddress orphaned in the wallet, watched by "
                f"nothing."
            )
        return address

    def validate_address(self, address: str) -> bool:
        """Ask the wallet whether an address is valid ON ITS OWN NETWORK.

        The network half is not incidental. A mainnet Monero address is
        syntactically perfect and will validate anywhere that does not check;
        paying one from a stagenet wallet fails, and paying a stagenet address
        from a mainnet wallet is the same mistake pointed at real money. The
        daemon is asked without `any_net_type`, so its answer is scoped to the
        network it is actually on -- which is also why this returns the
        daemon's verdict rather than doing any parsing here.

        A failure to reach the wallet RAISES rather than returning False.
        Returning False would tell the caller "that address is bad", which is a
        different sentence from "I could not ask", and the customer would see
        their correct address rejected.
        """
        result = self.call(_METHOD_VALIDATE_ADDRESS, {"address": address})
        return bool(result.get("valid"))

    def get_balance(self) -> float:
        """The UNLOCKED balance, in XMR, for the configured account.

        Unlocked rather than total, and the difference is the whole point:
        `balance` includes outputs still inside their ten-block consensus lock,
        which cannot be spent no matter what this application believes.
        services/payout_service.py:267 reads this to decide whether a payout
        can be funded, so reporting the total would let it commit to a payout
        the wallet then refuses to build -- the same gate-versus-transfer
        disagreement chains/monero_units.effective_min_confirmations() exists
        to prevent, arriving through the balance instead of the gate.
        """
        result = self.call(_METHOD_GET_BALANCE, {"account_index": self.account_index})
        unlocked = result.get("unlocked_balance")
        if not isinstance(unlocked, int) or isinstance(unlocked, bool):
            raise MoneroRPCError(
                f"{_METHOD_GET_BALANCE} returned unlocked_balance={unlocked!r}, which is not an integer "
                f"of atomic units. NOT coerced: reading this at the wrong scale would be wrong by a "
                f"factor of a trillion, and it funds the payout decision."
            )
        return from_atomic(unlocked)

    def find_deposits_to_address(self, address: str) -> list[dict]:
        """Incoming transfers to one subaddress, as deposit events.

        The account-wide `get_transfers` result is filtered down to the one
        address in chains/monero_transfers.py rather than here, and the
        refusals live there too -- this method is transport. Anything held
        back is PRINTED rather than dropped, because money that has arrived
        and is not yet creditable is the exact case where a silent empty list
        makes an operator watch a swap sit at `awaiting_deposit` with nothing
        on screen to explain it (rule 14).
        """
        result = self.call(
            _METHOD_GET_TRANSFERS,
            {"in": True, "account_index": self.account_index},
        )
        try:
            scan = deposit_events_from_transfers(result.get("in") or [], address, self.min_confirmations)
        except MoneroTransferError as error:
            # Re-raised as an RPC-layer error so the worker's handler sees one
            # exception type from this adapter, with the original chained so
            # the refusal's own explanation is not lost. NOT converted to an
            # empty list -- that is the failure this whole file argues against.
            raise MoneroRPCError(f"deposit scan for {address} refused: {error}") from error
        for line in scan.deferred:
            print(f"  XMR deferred  {line}", flush=True)
        return scan.events

    def send_to_address(self, address: str, amount: float) -> str:
        """Pay `amount` XMR to `address`. Refuses unless can_spend was set.

        The refusal is checked FIRST, before the amount is converted and before
        any network call, so that a misconfigured adapter cannot get as far as
        describing a payment to a daemon that might be able to make it.

        Unlike chains/solana.py, this method is implemented rather than handed
        back, and the difference is where the key lives. The Solana path would
        require THIS process to hold a signing keypair. Here the spend key
        belongs to monero-wallet-rpc, exactly as `sendtoaddress` on the three
        Bitcoin-derived chains belongs to their wallet daemons -- so
        implementing it adds no secret to this application, and matches what
        chains/base.py already does for every other chain.
        """
        if not self.can_spend:
            raise MoneroSpendDisabled(
                f"this XMR adapter is view-only, so it will not pay {amount} XMR to {address}. NOTHING "
                f"was sent and no wallet call was made. If this wallet is meant to fund payouts, set "
                f"XMR_WALLET_CAN_SPEND=true -- and if it is not, this refusal is the deposit watcher "
                f"working as intended."
            )
        result = self.call(
            _METHOD_TRANSFER,
            {
                "destinations": [{"address": address, "amount": to_atomic(amount)}],
                "account_index": self.account_index,
            },
        )
        tx_hash = result.get("tx_hash")
        if not tx_hash:
            raise MoneroRPCError(
                f"{_METHOD_TRANSFER} returned no tx_hash for {amount} XMR to {address}. THE PAYMENT MAY "
                f"HAVE BEEN SENT: a missing field in the response is not evidence the transfer did not "
                f"happen. Check the wallet before retrying, because retrying would pay twice."
            )
        return tx_hash

    # PROVING A PAYOUT IS A KNOWN GAP, AND IT IS NOT AN OVERSIGHT.
    #
    # On the three Bitcoin-derived chains a payout proves itself: the txid goes
    # into `payouts`, and anyone can look it up and see the output paying the
    # customer's address. That is how a dispute gets settled today.
    #
    # Monero publishes no such link. Ring signatures and stealth addresses mean
    # nobody -- including us -- can point at the chain and show which output
    # paid whom. The only proof is the transaction's secret key, which
    # `transfer` will return alongside tx_hash if asked, and which the
    # recipient checks against their own address with `check_tx_key`.
    #
    # An earlier draft of this method asked for it and then threw it away,
    # which is worse than not asking: it looks like the feature is handled.
    # It is not, because storing it is the part that matters and that part is
    # the operator's:
    #
    #   - db.py's `payouts` table has no column for it, so this is a schema
    #     change on the table that records money leaving.
    #   - a tx key is a SECRET with an unusual shape. It does not let anyone
    #     spend anything, but it does reveal the amount and the destination to
    #     whoever holds it -- so storing every one of them next to the swap
    #     record creates a new class of disclosure that does not exist for any
    #     other chain here.
    #
    # Which of those costs is worth paying depends on whether payout disputes
    # are expected at all, and that is a business question with a live-money
    # answer (rule 16). Written down here rather than half-built.
