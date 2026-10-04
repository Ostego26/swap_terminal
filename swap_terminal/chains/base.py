"""JSON-RPC adapter for the three Bitcoin-derived chains.

Role: submodule (one class; the three chains differ only by an `asset` string)
Reads: a wallet daemon over JSON-RPC -- validateaddress, getaddressinfo,
       getbalance, gettransaction, getrawtransaction, decoderawtransaction,
       listtransactions, estimatesmartfee
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

# SUSPECT_VOUT is the vout a fabricated deposit event is stamped with, and it
# is imported rather than written as a `0` literal here because the two have to
# agree for the migration to be able to FIND the rows this file writes:
# deposit_vout_artifact.multi_vout_groups() and migrate_deposit_vouts.py both
# select on it as the artifact's signature. Two copies of one constant is rule
# 8's bug with a delay on it, and the delay here would be measured in rows
# nobody could clean up. The module is safe to import from this money path and
# that was checked rather than assumed: deposit_vout_artifact.py has NO imports
# at all, opens no database, reads no environment and defines only constants
# and pure functions, so there is no import-time side effect of the kind rule 12
# names.
from deposit_vout_artifact import SUSPECT_VOUT
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

# A fabricated event is emitted only at or above this many confirmations. One,
# not the swap's min_confirmations: this module does not know which swap the
# transaction belongs to and must not learn, because min_confirmations is
# copied onto each swap row at creation and services/deposit_service.py reads
# it from there (the comparison is `int(row["confirmations"]) >=
# int(swap["min_confirmations"])`). A second copy of that threshold down here
# would be rule 8's duplicate on the one line that releases a payout. What this
# constant encodes is weaker and chain-wide: a transaction with ZERO
# confirmations cannot satisfy any positive min_confirmations, whatever it is,
# so a zero-confirmation event can never contribute to confirmed_total.
MIN_FABRICATED_CONFIRMATIONS = 1


def fabricated_deposit_events(  # noqa: PLR0913 -- checked: these six are the deposit_events row (asset, txid, address, amount, confirmations) plus the reason the row is synthetic, and the call is KEYWORD-ONLY, which is the transposition hazard PLR0913 exists to flag. Bundling them into an object would add a type without removing a parameter -- the same reading, for the same reason, as RPCAdapter.__init__'s six below.
    *, asset: str, txid: str, address: str, amount: float, confirmations: int, why: str
) -> list[dict]:
    """The fabricated deposit event, or NOTHING when the transaction has no confirmations.

    Keyword-only on purpose: `txid`, `address` and `amount` are three values a
    positional call can transpose without the type system noticing, and a
    transposed address on this path writes a deposit row against the wrong
    string.

    `why` IS REQUIRED AND HAS NO DEFAULT, because the warning this function
    prints used to say "raw outputs could not be matched" for every caller and
    that sentence was wrong for most of them. One string covered a decode that
    RAISED, a decode that succeeded and matched nothing, and -- once the wallet
    route below existed -- a wallet record that carried neither a serialization
    nor a block hash. Three different things an operator would do three
    different things about, rendered identically, which is rule 14's defect
    (and it misled a reader of the 2026-10-04 log before it was fixed). A
    caller that cannot say which failure it saw has not established which
    failure it saw.

    =========================================================================
    WHAT FABRICATION MEANS HERE
    =========================================================================

    _extract_matching_vouts() calls this at ONE site -- it used to be two,
    the decode raising and the decode succeeding while matching no output, and
    they are now one `if` at the bottom of that method with `why` carrying
    which happened. Either way no real output has been read, so the event is
    built from what the caller already believed: SUSPECT_VOUT (0) for the
    output index, the amount `listtransactions` summarized, and a confirmation
    count from the wallet rather than from an output. services/deposit_service.py
    cannot tell that apart from a real event, which is rule 12's BLE001 in its
    expensive form and is written up at length in deposit_vout_artifact.py.

    =========================================================================
    THE DEFECT THAT PUT THE GUARD HERE -- MEASURED TWICE, 2026-10-03
    =========================================================================

    One BTC deposit of ONE transaction produced TWO rows in deposit_events.
    Reproduced on two separate swaps, `s_6cd1a920cbe5739e` and
    `s_02623852c1ea42cc`. For txid

        fd898cfb8b5b8fa026f21ce30afc7f234126fe965a27c1330c8d6969a228eaf2

    the operator's screen showed both of these rows, for the one payment:

        0.001 BTC  0 confirmation(s)  NOT counted -- below min_confirmations=2  vout 1
        0.001 BTC  2 confirmation(s)  COUNTED by the gate                       vout 0

    `bitcoin-cli gettransaction <txid> true` settled what the transaction
    actually contains: ONE payment, of 0.001, whose real output is at **vout
    1**. So the row the credit arithmetic actually used records a vout the
    transaction does not have.

    upsert_deposit_event() keys on (asset, txid, vout), so a vout=0 row and a
    vout=1 row are two different keys and both persist. refresh_swap_from_chain()
    then sums every row for the swap, which is the double count
    deposit_vout_artifact.py describes.

    WHICH BRANCH WROTE WHICH ROW WAS NOT ESTABLISHABLE WHEN THAT WAS WRITTEN,
    AND IT IS NOW. The sentence that stood here said the real matching branch
    reads `int(vout.get("n", 0))` and so can legitimately emit 0 too, which is
    true of the CODE and is settled by the measurement beside it: the
    operator's `gettransaction <txid> true` showed ONE payment, of 0.001, at
    vout 1. For the vout=0 row to have come from the matching branch, output 0
    would have to pay the deposit address 0.001 -- and it does not; it is
    change to another address. So the matching branch cannot have written the
    vout=0 row, and the fabricated branch cannot have written the vout=1 row
    (it writes SUSPECT_VOUT and nothing else). One row from each branch, and
    which is which follows from the transaction rather than from a guess.

    =========================================================================
    AND THE CAUSE IS NOW ESTABLISHED TOO -- MEASURED 2026-10-04, ON THE
    OPERATOR'S OWN BITCOIND
    =========================================================================

    `_raw_tx_for_vouts()` was one line: `getrawtransaction(txid, True)`, with
    no block hash. Against the real deposit
    b2892636451355197be246e9f5dc56fcac309261fc17bd8d36b2a2b1108dd4f8 that
    daemon answered

        error code: -5
        No such mempool transaction. Use -txindex or provide a block hash to
        enable blockchain transaction queries. Use gettransaction for wallet
        transactions.

    and `grep -c '^txindex=1' ~/regtest/btc/bitcoin.conf` answered 0. Both
    readings are the operator's. So on that node the call could read a
    transaction only while it sat in the MEMPOOL, and every deposit that
    reached a confirmation before the next poll took the fabricated branch.

    That accounts for both 2026-10-03 rows, counts included, with one
    mechanism:

        poll while unconfirmed   in the mempool, so the decode SUCCEEDS
                                 -> the real output, vout 1, confirmations 0
        poll after confirming    gone from the mempool, so -5
                                 -> fabricated, vout 0, confirmations 2

    WHAT IS STILL NOT ESTABLISHED, stated because the narrower claim is the
    true one (rule 17). The -5 and the missing `txindex=1` were measured on
    2026-10-04 against a DIFFERENT txid on the same host; nobody measured that
    daemon's configuration on 2026-10-03. And no log from that day can settle
    it directly, because the warning text was identical for the raising branch
    and the no-match branch -- the very defect `why` above now removes. What IS
    established is that every other route to those two rows is excluded by
    measurements already recorded: the matching branch ran (it produced the
    vout=1 row, which the pre-2026-09-25 `scriptPubKey.addresses` code could
    not have), and the no-match branch is excluded because the same outputs
    decoded and matched at zero confirmations and outputs do not change when a
    transaction is mined.

    =========================================================================
    WHY REFUSING AT ZERO CONFIRMATIONS COSTS NOTHING
    =========================================================================

    The credit sums only rows at or above the swap's own min_confirmations, and
    min_confirmations is positive on every chain this terminal serves. A
    zero-confirmation row therefore cannot contribute to confirmed_total and
    cannot release a payout -- it can only sit in the table, under a key the
    real output's row will never reuse. Emitting nothing while the decode is
    failing forfeits no credit at all.

    WHAT THE ZERO-CONFIRMATION REFUSAL DOES AND DOES NOT CLOSE, stated
    precisely because the narrower claim is the true one. A fabricated event
    can reach the table at any confirmation count, and the refusal removes
    exactly one route: the insert made while the transaction is unconfirmed. It
    was NOT the route the 2026-10-03 rows took -- those were a real row at zero
    confirmations and a fabricated row at two, which is the opposite way round
    -- and this is the correction to the paragraph that used to stand here and
    could not say so.

    WHAT CLOSED THE ACTUAL ROUTE is the wallet route in
    RPCAdapter._raw_tx_for_vouts(): `gettransaction` for the serialization and
    `decoderawtransaction` for the outputs, so a confirmed wallet transaction
    on a node with no `-txindex` is DECODED instead of fabricated from. The
    refusal below stays because it is cheap and because it holds for the
    failures that remain (a wallet record carrying neither `hex` nor
    `blockhash`, and a recovery call the daemon refuses), neither of which this
    session can reproduce against a real daemon.

    A negative count is suppressed by the same comparison and that is
    deliberate: Bitcoin Core reports `confirmations: -1` for a transaction
    conflicted by a block, which is further from creditable than zero is.

    WHAT IS DELIBERATELY NOT CHANGED. Where this fires for a transaction that
    DOES have confirmations, the fabricated event is still returned, exactly as
    before. Refusing there would stall deposits that credit today, which
    _extract_matching_vouts()'s own comment has said since 2026-09-25 is a fund
    decision and the operator's (rule 16). This function narrows the fabricated
    branch to the cases where it can still do damage; it does not remove it.

    WHAT CHANGES ON SCREEN, since it is not nothing. A deposit whose decode
    fails while it is still unconfirmed no longer produces a deposit_events
    row, so the swap stays in `awaiting_deposit` instead of moving to
    `deposit_seen` -- advance_deposit_status() turns on `sums.has_rows`. That is
    a visibility difference on an uncreditable deposit, and it resolves on the
    next poll that either decodes the transaction or sees a confirmation. The
    suppression logs rather than passing silently (rule 14), because the
    original defect went unnoticed for precisely as long as nothing said
    anything.
    """
    if int(confirmations) < MIN_FABRICATED_CONFIRMATIONS:
        logger.warning(
            "%s deposit %s to %s: %s, and the transaction has %s confirmation(s), so NO "
            "deposit event was emitted for %s %s  <- a zero-confirmation event cannot reach "
            "any min_confirmations, and emitting one at vout=%s would collide with the real "
            "output once the outputs can be read (measured 2026-10-03, swaps "
            "s_6cd1a920cbe5739e and s_02623852c1ea42cc)",
            asset or "?", txid, address, why, int(confirmations), amount, asset or "?",
            SUSPECT_VOUT,
        )
        return []
    logger.warning(
        "%s deposit %s to %s: %s, so this event is FABRICATED from the wallet summary -- "
        "amount=%s vout=%s confirmations=%s  <- vout=%s is invented, not read from the "
        "transaction; the amount is listtransactions' figure rather than an output's value",
        asset or "?", txid, address, why, amount, SUSPECT_VOUT, int(confirmations), SUSPECT_VOUT,
    )
    return [{
        "txid": txid,
        "vout": SUSPECT_VOUT,
        "address": address,
        "amount": float(amount),
        "confirmations": int(confirmations),
    }]


class VoutReadRoute(NamedTuple):
    """Which RPC can take a wallet transaction apart, and why that one.

    `method` is empty when no route is available, and `why` is then the reason
    -- never an empty string, because a blank gap in a log is ambiguous between
    "no route" and "the lookup broke" (rule 14).
    """

    method: str
    params: tuple
    why: str


def vout_read_route(txid: str, wallet_tx) -> VoutReadRoute:
    """THE DECISION (rule 10): how to read a wallet transaction's outputs with no -txindex.

    Called with a `gettransaction` result and nothing else, so it is testable
    with seeded dictionaries rather than only by running a deposit poll.

    =========================================================================
    WHY THE ORDER IS hex FIRST AND blockhash SECOND
    =========================================================================

    Both routes answer the daemon's own -5 complaint, and either would work on
    Bitcoin Core 28.1 and Litecoin Core 0.21.4. They do NOT both work on the
    third chain, and that is what decides the order. Measured on the
    operator's Gridcoin testnet daemon 2026-09-27 by asking it, and recorded
    in docs/atomic_swap_runs_2026_09_27.md:

        getrawtransaction            getrawtransaction <txid> [verbose=bool]
        gettransaction               gettransaction "txid" ( includeWatchonly )

    TWO parameters. The block-hash argument arrived in Bitcoin Core 0.16 and
    Gridcoin is on the pre-0.17 surface (it has neither `getaddressinfo` nor
    `gettxout`, both measured on that daemon), so a route built on a third
    argument is a route that cannot exist on one of the three chains this
    adapter serves. `decoderawtransaction` predates all of them.

    The hex route also needs no chain context at all, so it answers for a
    transaction in the mempool and for one mined forty blocks ago with the same
    two calls -- where the block-hash route has nothing to pass while the
    transaction is unconfirmed.

    It is kept as the SECOND route rather than dropped because a wallet record
    with no `hex` is the one case the first cannot serve, and a daemon vintage
    that omits it is not something this session can rule out: no GRC
    `gettransaction` output has been read in this tree, so whether Gridcoin
    reports `hex` is NOT established here (and GRC does not reach this function
    today -- see _raw_tx_for_vouts).

    =========================================================================
    THE SAME LADDER EXISTS ONCE MORE IN THIS TREE (rule 8)
    =========================================================================

    modules/htlc_rpc.lookup_contract_output() tries four routes -- `gettxout`,
    `getrawtransaction` with a block hash, `gettransaction` plus
    `decoderawtransaction`, then bare `getrawtransaction` -- for the same
    reason, and its docstring carries the establishment for each. A reader who
    finds one must be told the other exists, so: that is the other one, and
    these genuinely differ rather than being a copy to merge.

      - It reads ONE output by index for an HTLC the counterparty may have
        funded; this searches every output for one that pays an address, for a
        wallet transaction `listtransactions` just reported, which is why
        `gettxout` (unspent only) is not a route here at all -- a deposit that
        has been swept is still a deposit.
      - It cannot be imported from here. modules/htlc_rpc imports
        modules/htlc_spend, which imports `ecdsa`, `base58` and `bech32`, and
        swap_terminal/requirements.txt states as a deployment property that the
        Flask app does not drag the atomic-swap dependencies in. The same
        measurement is why script_pub_key.py exists at the package root.
    """
    if not isinstance(wallet_tx, dict):
        return VoutReadRoute("", (), f"gettransaction answered {type(wallet_tx).__name__}, not an object")
    raw_hex = wallet_tx.get("hex")
    if isinstance(raw_hex, str) and raw_hex:
        return VoutReadRoute(
            "decoderawtransaction",
            (raw_hex,),
            "gettransaction + decoderawtransaction (the wallet's own serialization, which needs no block hash)",
        )
    block_hash = wallet_tx.get("blockhash")
    if isinstance(block_hash, str) and block_hash:
        return VoutReadRoute(
            "getrawtransaction",
            (txid, True, block_hash),
            f"getrawtransaction with the wallet's block hash {block_hash}",
        )
    return VoutReadRoute(
        "", (),
        "gettransaction reported neither `hex` nor `blockhash`, so there is no way to ask for the outputs",
    )


class DecodedOutputs(NamedTuple):
    """A transaction's outputs as some daemon decoded them, plus how that was done.

    `read` is the field that matters and it is NOT `bool(outputs)`: "the
    daemon decoded this and these are its outputs" and "nothing could decode
    it" are different answers, and collapsing them is the shape of defect this
    whole module keeps paying for. `how` is a sentence either way -- the route
    that worked, or every route that did not -- and it is what reaches the
    operator's log through fabricated_deposit_events(`why=`).
    """

    outputs: list
    confirmations: int
    read: bool
    how: str


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

        AND THE DIRECTION OF THAT ROUNDING WAS ESTABLISHED LATER THE SAME DAY: it
        is HALF UP, so 2701.34958032 is one satoshi MORE than the 2701.34958031
        fit_to_chain_precision() computes from the same input. Measured over the
        operator's 9 GRC payout rows -- 9 of 9 fit round-half-up, 6 of 9 fit
        truncation, and the 6 are exactly the rows where the two cannot differ --
        and traced to Gridcoin 5.5.1.0's AmountFromValue(), which is the pre-0.17
        `roundint64(dAmount * COIN)` form rather than ParseFixedPoint. The full
        table of what each daemon does with an over-precise amount, with the
        source references, is in chains/payout_quantization.py's header; it is
        written once and pointed at (rule 8).

        NOTHING ON THIS PATH REACHES THAT BEHAVIOR ANY MORE. Since 54892d5 the
        payout service quantizes before the send, and this method quantizes again
        below, so the figure Gridcoin is handed already has eight decimals and
        every rounding agrees on it. The masking is history, not a live tolerance
        being relied on.

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

    def own_address(self) -> str:
        """An address this WALLET already owns, read not derived. "" when none can be read.

        FOR MEASURING A FEE AGAINST, and it exists because the first source tried
        was wrong for the case that matters. services/quote_service.
        own_address_on_chain() read swaps.deposit_address, which holds addresses for
        chains used as a SOURCE -- and the reserve is needed for the DESTINATION
        chain. Measured on the operator's host within the hour: a BTC -> LTC quote
        fell back to the flat constant because no swap had ever taken an LTC
        DEPOSIT, LTC having only ever been paid out to. A payout chain that is only
        ever a destination is the normal case, so the one source I picked had no
        row precisely when it was needed.

        READS ONLY, AND THAT IS THE WHOLE CONSTRAINT. getnewaddress would answer in
        one call and DERIVES a key, so a priced quote would leave one in the hot
        wallet for every page refresh. listreceivedbyaddress with include_empty
        lists addresses the wallet already has; getaddressesbylabel is the fallback
        for a daemon that answers one and not the other. Neither creates anything.

        ANY ADDRESS OF THE RIGHT TYPE WILL DO, which is why this does not care
        which one comes back: a fee is the inputs selected plus the output's SIZE,
        and a p2wpkh output is 31 bytes whoever owns it. The assumption and its
        residual error are recorded at measured_or_configured_reserve().
        """
        try:
            received = self.call("listreceivedbyaddress", 0, True) or []
        except Exception:  # noqa: BLE001 -- checked: returns "" through the fallback below, and the caller renders a missing address as "the constant was used, because no address could be read". No failure here can produce a wrong fee, only an unmeasured one.
            received = []
        for entry in received:
            address = (entry or {}).get("address") or ""
            if address:
                return str(address)
        try:
            labeled_addresses = self.call("getaddressesbylabel", "") or {}
        except Exception:  # noqa: BLE001 -- checked: as above. Two reads are tried because daemons differ on which they expose, and neither existing is a legitimate answer.
            return ""
        for address in labeled_addresses:
            if address:
                return str(address)
        return ""

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
        # caller rather than becoming an empty dict a caller would read as zero
        # confirmations.
        #
        # THE FALLBACK HERE CANNOT READ A CONFIRMED TRANSACTION ON A NODE WITH
        # NO -txindex, which is the 2026-10-04 measurement written up at
        # _raw_tx_for_vouts() below, and it is NOT fixed the same way: it is
        # reached only when `gettransaction` has already failed, so there is no
        # wallet record to take a block hash or a serialization from. Both
        # callers (chains/payout_on_chain.py and show_payout_fees.py) read
        # transactions THIS wallet sent, where the first call answers. A reader
        # who comes here from that measurement must be told why one site took
        # the wallet route and this one cannot (rule 8).
        try:
            return self.call("gettransaction", txid)
        except Exception:  # noqa: BLE001 -- checked: see the comment above; the fallback's own failure propagates, so a caller never receives a plausible-looking empty answer.
            return self.call("getrawtransaction", txid, True)

    # get_confirmations() USED TO BE HERE AND IS DELETED, 2026-10-04 (rules 2
    # and 9). It was `int(self.get_transaction(txid).get("confirmations", 0))`
    # and its only caller in the tree was the fabricated branch of
    # _extract_matching_vouts(), which now takes its count from the wallet
    # record the new route already read -- so keeping it would leave a helper
    # whose only caller had just been deleted. Established by grepping the NAME
    # across every file in the repository, not just the import graph (rule 2):
    # the remaining hits are this comment, the historical quote in
    # deposit_vout_artifact.py's docstring and chains/solana.py's note that it
    # was never part of the adapter contract. What that establishes is "no
    # caller in this tree"; it cannot establish "no caller anywhere", and
    # nothing outside this repository is visible from here.

    def _raw_tx_for_vouts(self, txid: str) -> DecodedOutputs:
        """This transaction's outputs, read WITHOUT requiring -txindex on the node.

        =====================================================================
        THE DEFECT THIS REPLACES -- MEASURED ON THE OPERATOR'S BITCOIND 2026-10-04
        =====================================================================

        This method was one line:

            return self.call("getrawtransaction", txid, True)

        No block hash. Against the real deposit
        b2892636451355197be246e9f5dc56fcac309261fc17bd8d36b2a2b1108dd4f8 the
        operator's own daemon answered

            error code: -5
            No such mempool transaction. Use -txindex or provide a block hash
            to enable blockchain transaction queries. Use gettransaction for
            wallet transactions.

        and `grep -c '^txindex=1' ~/regtest/btc/bitcoin.conf` answered 0.
        Both readings are the operator's, not this session's.

        So the one call could see a transaction only while it was in the
        MEMPOOL. Every BTC or LTC deposit that reached a confirmation before
        the next 15s poll therefore raised, and _extract_matching_vouts()
        fabricated an event at vout=0 with the amount from
        `listtransactions` -- which is the two-rows-for-one-payment artifact
        measured 2026-10-03 and is written up in full at
        fabricated_deposit_events().

        It was never a decode failure. The wallet knew the transaction the
        whole time; the adapter simply did not ask it.

        =====================================================================
        WHAT IT COSTS, PER DEPOSIT READ
        =====================================================================

            node that can answer directly   1 RPC   unchanged
              (the mempool; a node with -txindex; Gridcoin, whose pre-0.8
               lineage indexes every transaction -- measured 2026-09-27, route
               4 of modules/htlc_rpc.lookup_contract_output() answered for a
               CONFIRMED GRC contract on a daemon with no txindex setting)
            confirmed, no -txindex          3 RPCs  was 2, and fabricated
              (getrawtransaction, then gettransaction, then the route below)
            no route available              2 RPCs  was 2, still fabricates

        The direct call is tried FIRST rather than going straight to the
        wallet, and that ordering is the reason GRC cannot regress: a daemon
        that answers today answers identically and never reaches a line of
        the new code. It also keeps the mempool case at one call, which is
        most polls -- the watcher sees a deposit unconfirmed before it sees it
        confirmed.

        =====================================================================
        WHAT IS CAUGHT, AND WHY IT IS NOT `except Exception`
        =====================================================================

        `RPCError` is the daemon saying no (code -5 here), and
        `requests.RequestException` is the socket saying no -- those two are
        every failure self.call() is built to produce, and
        requests.exceptions.JSONDecodeError is a RequestException, so a 200
        carrying junk is included (checked against requests 2.33.1's MRO
        rather than assumed).

        Anything else -- a TypeError, an AttributeError -- is a defect in this
        file and now REACHES THE WORKER instead of becoming a fabricated
        deposit event. That is a deliberate narrowing of the old
        `except Exception`: the worker logs a FAILED cycle and credits
        nothing, where before a bug here was laundered into a credit built
        from a wallet summary. It halts rather than releasing, which is the
        safe direction on this path.
        """
        try:
            raw = self.call("getrawtransaction", txid, True)
        except (RPCError, requests.RequestException) as direct_failure:
            return self._vouts_through_the_wallet(txid, direct_failure)
        raw = raw if isinstance(raw, dict) else {}
        return DecodedOutputs(
            outputs=list(raw.get("vout") or []),
            confirmations=int(raw.get("confirmations") or 0),
            read=True,
            how="getrawtransaction verbose, answered directly (the mempool, -txindex, or a daemon that indexes every transaction)",
        )

    def _vouts_through_the_wallet(self, txid: str, direct_failure: Exception) -> DecodedOutputs:
        """The outputs by way of the WALLET, for a transaction the chain query cannot reach.

        Reached only when the direct `getrawtransaction` failed. Every txid
        that gets here came out of `listtransactions` (see
        find_deposits_to_address), so it IS a wallet transaction and
        `gettransaction` is the right question -- which is exactly what the
        daemon's own -5 message says to ask.

        `gettransaction` is NOT wrapped, and that is unchanged from the code
        this replaces: if the wallet cannot answer for a transaction it just
        listed, the exception reaches the worker rather than becoming a zero
        confirmation count that reads as a real unconfirmed deposit.

        The SECOND call is wrapped, because its failure must not be worse than
        the behavior it replaces: before this method existed, a daemon that
        refused here fabricated an event, and a route that turned that into a
        dead worker cycle would be a regression on a chain nobody here can
        test. The reason is carried out in `how` rather than swallowed, so the
        log names both what the chain query said and what the wallet route
        then said (rule 12: a broad-ish catch has to say which failure it saw).

        The confirmation count comes from the wallet record, once, and is the
        only count available on this path -- `decoderawtransaction` is handed
        bytes, not a position in a chain, so it reports none.
        modules/htlc_rpc.lookup_contract_output() splices the same field in for
        the same reason.
        """
        direct = f"getrawtransaction answered {type(direct_failure).__name__}: {direct_failure}"
        wallet_tx = self.call("gettransaction", txid)
        # A non-dict is not an answer, and 0 confirmations is the right floor
        # for one: it cannot reach any swap's min_confirmations, so it cannot
        # release anything. vout_read_route() reports the same shape as "no
        # route" and says so in its reason.
        confirmations = int(wallet_tx.get("confirmations") or 0) if isinstance(wallet_tx, dict) else 0
        route = vout_read_route(txid, wallet_tx)
        if not route.method:
            return DecodedOutputs([], confirmations, False, f"{direct}, and then {route.why}")
        try:
            decoded = self.call(route.method, *route.params)
        except (RPCError, requests.RequestException) as recovery_failure:
            return DecodedOutputs(
                [], confirmations, False,
                f"{direct}, and then {route.why} answered "
                f"{type(recovery_failure).__name__}: {recovery_failure}",
            )
        decoded = decoded if isinstance(decoded, dict) else {}
        return DecodedOutputs(list(decoded.get("vout") or []), confirmations, True, route.why)

    def _extract_matching_vouts(self, txid: str, address: str, amount: float):
        # THE PROPOSAL MARKER THAT USED TO HEAD THIS METHOD IS NARROWED AGAIN,
        # AND THE CAUSE IT DESCRIBED IS GONE. It said that both fallbacks here
        # fabricate a deposit event -- vout SUSPECT_VOUT, the amount the caller
        # already believed, a confirmation count from a second RPC -- and that
        # services/deposit_service.py cannot tell that from a real event. All
        # of that is still true of the fallback. What changed on 2026-10-04 is
        # WHY it was being reached.
        #
        # It was not a decode failure. `_raw_tx_for_vouts()` asked
        # `getrawtransaction(txid, True)` with no block hash, which on a node
        # without -txindex cannot see a transaction once it leaves the mempool
        # -- measured on the operator's own bitcoind, error code -5, with
        # txindex unset. So every BTC and LTC deposit that confirmed between
        # two polls took the fabricated branch. That method now reads the
        # outputs through the WALLET instead, and both its measurement and the
        # RPC cost are written down there.
        #
        # WHAT REACHES THE FALLBACK NOW: a wallet record carrying neither `hex`
        # nor `blockhash`, a recovery call the daemon refuses, and a decode
        # that succeeded while matching no output (every output pays somebody
        # else, names no address at all, or carries a value further than one
        # satoshi from the amount `listtransactions` reported). Below one
        # confirmation none of them emits anything; at or above it, the event
        # is still fabricated and still credited from the wallet's summary,
        # which is unchanged on purpose -- making it raise would stall swaps
        # that credit today, and which failure mode the operator wants is a
        # fund decision (rule 16).
        #
        # ONE CALL SITE, not two. The two fabrication sites twenty lines apart
        # are now one `if` at the bottom, and `why` carries which of the
        # failures above happened -- they used to print the same sentence,
        # "raw outputs could not be matched", for all of them (rule 14).
        decoded = self._raw_tx_for_vouts(txid)
        matches = []
        confirmations = decoded.confirmations
        for vout in decoded.outputs:
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
        # NO REAL OUTPUT WAS READ, by either road, and the two roads are told
        # apart in the log rather than in the code path. `decoded.read` is the
        # distinction: True means a daemon handed over this transaction's
        # outputs and none of them pays `address` for `amount` -- which is what
        # the removed `scriptPubKey.addresses` field produced for EVERY output
        # on a Core 22+ node until 2026-09-25, silently, without an exception
        # and without a `noqa` to mark it. False means nothing could read the
        # outputs at all, and `decoded.how` then names every route that was
        # tried and what each said.
        #
        # The confirmation count is whichever one _raw_tx_for_vouts() already
        # has -- the decoded transaction's, or the wallet record's from the
        # route it took. No extra round trip, and no second answer that could
        # differ from the first.
        if decoded.read:
            why = (f"the outputs were read ({decoded.how}) and none of them pays this address "
                   f"for this amount")
        else:
            why = f"the outputs could not be read: {decoded.how}"
        return fabricated_deposit_events(
            asset=self.asset,
            txid=txid,
            address=address,
            amount=amount,
            confirmations=confirmations,
            why=why,
        )

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
