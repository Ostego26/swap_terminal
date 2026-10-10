#!/usr/bin/env python3
"""What a Bitcoin-family daemon's WALLET says about itself, as Core's GUI asks it.

Role: submodule (the four read panes of a Core wallet, plus the read-only allowlist
      that bounds every one of them -- each is a decision callable with a stub node)
Reads: one Bitcoin-style JSON-RPC handle, passed in. It opens no socket of its own
      and knows no host, port, credential or wallet name.
Writes: nothing
Can move funds: no, and that is enforced rather than asserted: every method any
      function here calls is in READ_ONLY_RPCS below, and
      tests/test_operator_panel.py::test_no_pane_rpc_is_outside_the_READ_ONLY_allowlist
      plus tests/test_chain_panel.py assert it by COLLECTING the calls a real pane
      made against a recording node, not by reading this sentence.
Mainnet-safe: yes to import and to call. Nothing here signs, submits, unlocks or
      derives a key, so pointing it at a mainnet daemon reads a mainnet balance and
      changes nothing. Whether a caller SHOULD is chains/daemon_network's question.
Live-safe: yes. It starts and stops no daemon and never touches a wallet lock.

=============================================================================
EXTRACTED FROM regtest/operator_panel.py ON 2026-10-10, WHEN A SECOND CALLER
APPEARED THAT HAS NO `regtest.funding_steps.Run`.
=============================================================================

These panes were written for the regtest harness's own panel and they are not
regtest knowledge: every one of them asks a daemon what a Core GUI asks it, and
the operator asked for the same four panes on /admin -- a surface that reaches its
daemons through `chains/registry.build_adapters()` and has no Run, no ChainConfig
and no console to print to. Copying four functions into services/ is rule 8's
defect with a delay on it; the worked precedent one module over is
`regtest/daemons.ensure_wallet_on()`, extracted the same day for the same reason
and for the same second caller.

WHAT TRAVELS AND WHAT DID NOT. The pane functions take the `run`-shaped
`HasAChainNode` protocol they always took -- one member, `node(wallet: bool = ...)`,
returning something with `.call(method, *params)` -- so `regtest.funding_steps.Run`
satisfies it unchanged and so does a four-line adapter wrapper. Nothing about the
regtest caller changed except where it imports from, which is what makes this
extraction safe to do on a live-money tree: there is no second copy to drift, and
no behavior moved with the text.

WHY THE ALLOWLIST CAME WITH THEM rather than staying behind. READ_ONLY_RPCS is a
list of BITCOIN METHOD NAMES and which of them read -- chain knowledge, not panel
knowledge -- and the second caller needs exactly the same list for exactly the same
reason. Leaving it in regtest/ would have meant either a web surface importing a
regtest module (upwards, through `regtest.daemons`, on a Flask request path) or a
second copy of the list. `call_read_only()` did NOT come: it takes a Run and builds
the panel's console answer, which is that panel's business.

THE THREE REFUSALS THIS MODULE OWES, restated here because this is where a reader
will look for them:

  send        money movement is CLAUDE.md rule 16's "live posture" -- the
              operator's call once measured, never something added while building
              a viewer. There is no sendtoaddress in READ_ONLY_RPCS and no
              function here composes a transaction.
  passphrase  a passphrase must never appear in a command this repository emits,
              and no surface built on this module may have a field for one.
              `walletpassphrase` takes it as an ARGUMENT, so a form collecting it
              would put it in a POST body, a browser's autofill and a request log.
              encryption_note() below REPORTS whether a wallet is encrypted and
              offers nothing that would unlock it.
  keys        no dumpprivkey, no dumpwallet, no backupwallet, no WIF, no
              wallet.dat. None is in the allowlist and none is added here.

`getnewaddress` IS DELIBERATELY ABSENT TOO, and it is the one that looks safe.
wallet_custody.py's header already draws the line: it "calls no getnewaddress --
which is a WALLET WRITE, not a read." A Receive pane is therefore NOT built here;
services/chain_panel.absent_capabilities() names its absence and names the tool
that does derive an address, which is what rule 14 asks for instead of a gap.
"""

from __future__ import annotations

from collections.abc import Mapping

# THE ENCRYPTION FIELD AND THE PREDICATE OVER IT, IMPORTED RATHER THAN SPELLED
# AGAIN (rule 8). chains/wallet_lock.py has owned "an encrypted wallet is one whose
# getwalletinfo reports `unlocked_until`, and PRESENCE rather than value is the
# test" since 2026-09-29, with the measurement that forced it in its own header.
# A second `"unlocked_until" in info` here would be two copies of one rule.
#
# IMPORTING THAT MODULE IS SAFE FROM A READ-ONLY SURFACE AND THE REASON IS WORTH
# STATING, because its own header says "It CALLS walletlock/walletpassphrase on an
# encrypted wallet": that is `unlocked_for_payout()`, which nothing here calls and
# which takes a passphrase this module never has. What is imported is the pure
# predicate and the field name -- neither touches a daemon.
from chains.wallet_lock import ENCRYPTION_FIELD, wallet_is_encrypted

#: THE ONLY RPC METHODS ANY SURFACE BUILT ON THIS MODULE WILL CALL. An allowlist, not a
#: denylist, and the difference is the whole safety argument: a denylist is a list of the ways
#: to lose money that somebody thought of, and it is wrong the first time a daemon adds a
#: method.
#:
#: IT GOVERNS TWO SURFACES SINCE THE EXTRACTION THIS MODULE'S HEADER RECORDS, through two
#: different mechanisms and one list:
#:
#:   regtest/operator_panel.py  the browser NAMES a method out of this list and the server
#:                              refuses anything else through refuse_unless_read_only() below.
#:   services/chain_panel.py    the browser names nothing. The panes below call a FIXED set,
#:                              and tests/test_chain_panel.py collects what a real pane asked
#:                              a recording node and asserts every name is permitted here.
#:
#: One list, because "which bitcoin RPCs read" is a fact about the daemons rather than about
#: either surface -- and a second copy would agree on the day it was written (rule 8).
#:
#: EVERY ONE OF THESE READS. None creates a transaction, none signs, none touches a lock, none
#: writes to a wallet. `getnewaddress` is NOT here even though it looks harmless -- it writes a
#: key into wallet.dat, which a staking-only wallet may refuse and which changes a file the
#: operator backs up.
#:
#: WHAT IS DELIBERATELY ABSENT AND WHY, because the omissions are the design:
#:
#:   sendtoaddress, sendrawtransaction, signrawtransaction
#:       these move or authorize money. The regtest panel already has buttons that run
#:       HARNESSES, which spend -- but those are named, reviewed entry points from its own
#:       RUNNABLE table, not an arbitrary transaction an operator can compose in a browser with
#:       no confirmation step. /admin has no such table and no write route at all.
#:   walletpassphrase, walletlock, encryptwallet
#:       walletpassphrase takes the passphrase as an ARGUMENT. Serving a form that collects it
#:       would put it in a POST body, the browser's autofill, and this process's memory --
#:       against swap_terminal/CLAUDE.md's "never move, copy, or read back a key", and against
#:       the standing rule that a passphrase never appears in anything this repository emits.
#:       No surface built on this module may be able to ask for one. encryption_note() below
#:       REPORTS the state of the lock and offers nothing that would change it.
#:   stop
#:       the operator's Gridcoin daemon is staking their live wallet. Rule 13 and the
#:       live-safety rules: nothing here starts or stops a daemon.
#:   dumpprivkey, dumpwallet, backupwallet, importprivkey
#:       a key read back out is a key that has left the wallet. `backupwallet` joined this
#:       line on 2026-10-10: it writes wallet.dat somewhere a reader can then fetch, which is
#:       the same exposure by a longer route.
#:
#: WHERE IT IS ENFORCED is the server, in both surfaces. The regtest page offers these as a
#: dropdown and that dropdown is only a convenience -- a request naming anything else is
#: refused whatever the page sends -- and /admin's panel sends no method name at all.
READ_ONLY_RPCS = (
    "getblockchaininfo", "getinfo", "getmininginfo", "getnetworkinfo", "getwalletinfo",
    "getblockcount", "getbestblockhash", "getdifficulty", "getconnectioncount", "getpeerinfo",
    "getbalance", "listunspent", "listtransactions", "listaddressgroupings", "listlockunspent",
    "getrawtransaction", "decoderawtransaction", "decodescript", "validateaddress",
    "getblock", "getblockhash", "getrawmempool", "gettxoutsetinfo", "uptime", "help",
    # ADDED 2026-10-10 FOR THE WALLET PANE, and both are reads by the definition the
    # refusal sentence below gives: neither creates a transaction, signs, touches a
    # wallet lock, nor writes a key. Neither carries a balance either, which is the
    # stricter bar network_target.may_read_a_wallet() is about.
    #
    #   listwallets    which wallets are LOADED. The one call that can answer "this
    #                  node has no wallet" positively, rather than by reading a
    #                  JSON-RPC error code off getwalletinfo's failure and treating
    #                  every other error as the same thing. See wallet_state().
    #   listwalletdir  which wallet NAMES exist on disk. Names only -- no balance, no
    #                  descriptor, no key -- and it is the actionable half of "no
    #                  wallet is loaded": the operator needs to know whether one is
    #                  sitting there to load or whether none was ever created.
    #
    # They were NOT here when the panes were written, and tests/test_operator_panel.py
    # ::test_no_pane_rpc_is_outside_the_READ_ONLY_allowlist caught it on its first run
    # -- which is the whole argument for that test: a pane that quietly widens what
    # the panel may ask a daemon is how a viewer stops being a viewer.
    "listwallets", "listwalletdir",
)


def refuse_unless_read_only(method: object) -> str:
    """"" if this method may be called, else the reason it may not. THE decision, on its own.

    A FUNCTION RATHER THAN AN `in` CHECK AT THE ROUTE, because this is the single decision that
    decides whether a browser can make this process move money, and a decision reachable only by
    making an HTTP request is a decision nobody tests (rule 10). This one is called with a
    string, including hostile ones.

    THE REFUSAL NAMES THE RULE rather than just saying no. An operator who is refused
    `sendtoaddress` needs to know it is a deliberate boundary and where the capability lives
    instead, or they will go looking for a flag that does not exist.
    """
    if not isinstance(method, str) or not method:
        return f"{method!r} is not a method name"
    if method in READ_ONLY_RPCS:
        return ""
    return (
        f"{method!r} is not on the read-only allowlist in chains/daemon_wallet.py. Every method "
        f"a surface built on that module will call READS -- nothing there creates a transaction, "
        f"signs, touches a wallet lock, or writes a key. Spending goes through a named entry "
        f"point at the repository root, or through your own shell; a passphrase never goes "
        f"through a page in this application at all."
    )


# =============================================================================
# THE CORE-WALLET PANES, 2026-10-10, at the operator's instruction: "create
# another tab that is daemon control that is bassically a literally ripoff of the
# entire btc, ltc, or grc core gui wallet but in the whatever format would work
# best in a web browser for the operator to control the swap terminal."
#
# THE TAB ALREADY EXISTED -- they asked for it on 2026-09-30 and the HTML still
# carries the sentence ("create a tab for daemon controls and then under there put
# subtabs for each"). What it did NOT have is a wallet: it offered a dropdown of
# 25 RPC names and printed raw JSON, so every question a Core wallet answers at a
# glance -- what is my balance, did that transaction arrive, am I connected to
# anybody, is this node even synced -- was a name to pick and a blob to read.
# These functions assemble the Core GUI's four read panes out of RPCs
# READ_ONLY_RPCS ALREADY ALLOWS, so nothing here widens what either surface may
# call.
#
# THE SEND, PASSPHRASE AND KEY REFUSALS ARE IN THIS MODULE'S HEADER and are NOT
# restated here. They were, in a near-identical block, until the extraction on
# 2026-10-10 put a module header above them that said the same three things -- and
# two copies of one refusal twenty lines apart is the defect OPEN_FINDINGS records
# four daemons shipping ("One of those files said both things twenty lines apart").
# Rule 9: the cull runs alongside the merge.
#
# WHAT A RECEIVE PANE WOULD BE, and this sentence was wrong until today. It read
# "A Receive pane therefore shows the addresses the wallet ALREADY has, via
# listaddressgroupings, and does not mint one" -- present tense, about a pane that
# does not exist in this module and never did. `listaddressgroupings` IS on the
# allowlist and would be the read-only half, so the sentence was describing
# something buildable as though it were built (rule 16: a wrong comment is a bug).
# services/chain_panel.absent_capabilities() is where the absence is now NAMED to
# the operator, with the tool that does derive an address.
# =============================================================================

#: The three things that can be true of a wallet, and the middle one is the whole
#: reason this is a classifier rather than a balance lookup.
#:
#: MEASURED ON THE OPERATOR'S HOST 2026-10-10, and it cost a wrong answer on screen.
#: Their regtest bitcoind answered `getblockcount` with 812 and answered BOTH
#: `getbalance` and `getaddressinfo` with nothing at all -- because no wallet was
#: loaded. A diagnostic block that wrapped those calls in `2>/dev/null` printed
#:
#:     balance       BTC   (need 0.00010123)
#:
#: which reads as a value and was a swallowed error. That is CLAUDE.md rule 12's
#: BLE001 shape in a shell pipeline: the caller cannot tell the failure from a real
#: answer. A pane that renders "0.00000000" for a node with no wallet is the same
#: defect with better typography, and it is worse here because an operator would
#: conclude their coins were gone.
WALLET_LOADED = "loaded"
WALLET_NONE_LOADED = "none_loaded"
WALLET_NOT_ESTABLISHED = "not_established"

#: Every state a wallet pane can render, so the renderer can be TESTED for covering
#: them all rather than discovering a gap on an operator's screen -- the same guard
#: stack_authority._PREFIX_NOTES carries, added there the same day after a renderer
#: branched on a bare None and invented a sentence about XRP.
WALLET_STATES = (WALLET_LOADED, WALLET_NONE_LOADED, WALLET_NOT_ESTABLISHED)


def _read(node, method: str, *params):
    """One read, returning (value, error_text). NEVER raises, never returns a bare None.

    A TUPLE BECAUSE A PANE HAS TO TELL "the daemon said no" FROM "we did not ask"
    (rule 17). Every caller below renders the error text when it is non-empty, so a
    missing RPC costs one row's worth of explanation instead of a blank pane or an
    exception out of a report.

    The broad catch is the legitimate kind rule 12 describes -- a diagnostic that
    must not die on one bad row -- and it is legitimate ONLY because the failure is
    IN the return value: `(None, "reason")` cannot be mistaken for an answer by any
    caller that unpacks it.
    """
    try:
        return node.call(method, *params), ""
    except Exception as error:  # noqa: BLE001 -- checked: the reason is RETURNED, not discarded, and every caller renders it. A pre-0.17 daemon simply lacks several of these methods and that is an ordinary, reportable fact rather than an outage.
        return None, f"{type(error).__name__}: {error}"


def wallet_state(run) -> dict:
    """Is a wallet loaded, and what does it hold? The Overview pane's whole content.

    THREE OUTCOMES AND THEY RENDER DIFFERENTLY (see WALLET_STATES above):

      loaded           a wallet answered. `balances` carries Core's own three
                       numbers -- available, pending, immature -- under the names
                       the GUI shows them under.
      none_loaded      the NODE answered and told us it has no wallet. A positive
                       finding, not a failure, and the only correct rendering is to
                       say so: a balance of zero would be a lie about a wallet that
                       does not exist.
      not_established  nobody answered. Says which, and never reads as either of
                       the above.

    `listwallets` IS ASKED FIRST AND IS WHY THIS CAN BE POSITIVE. Guessing from
    getwalletinfo's failure would mean reading a JSON-RPC error code (-18) and
    treating every other error as the same thing; asking the node for its list of
    loaded wallets answers the question directly and distinguishes "none" from
    "could not ask". Older daemons have no `listwallets` at all -- Gridcoin among
    them -- so its absence falls through to the balance read rather than being
    reported as "no wallet", which would be a confident wrong answer on the one
    chain that is working.

    `getwalletinfo` IS NOW READ ONCE, AND IT WAS READ TWICE UNTIL 2026-10-10.
    wallet_balances() asked for it and then this function asked for it AGAIN, three
    lines later, purely for `txcount` -- two identical round trips to the same daemon
    in one pane, on a surface that refreshes on a timer. Counted off a recording node:
    wallet_pane() made 8 calls on a loaded wallet and makes 7 now, with the duplicate
    being the one removed. _balances_and_info() returns the response alongside the
    balances so the second caller reads the FIRST answer, which also closes a subtler
    gap: the two calls could land either side of a wallet being unloaded, and the pane
    would then report a balance and a txcount from different wallets with nothing
    saying so (rule 3's "measure before claiming an improvement" -- the measurement is
    the call count, and the correctness is the free half).

    `encryption` RIDES ON THE SAME RESPONSE for the same reason, so reporting the lock
    costs no call at all. See encryption_note().
    """
    node = run.node(wallet=False)
    loaded, listed_error = _read(node, "listwallets")
    on_disk, _ = _read(node, "listwalletdir")
    names = [str(name) for name in loaded] if isinstance(loaded, list) else None
    if names is not None and not names:
        return {
            "state": WALLET_NONE_LOADED,
            "why": (
                "the node answered and holds NO loaded wallet. This is not a balance of zero -- "
                "there is no wallet to have a balance. Bitcoin Core since 0.21 creates none on "
                "its own, so one has to be loaded or created before this node can hold or send "
                "a coin."
            ),
            "loaded": [],
            "on_disk": _wallet_dir_names(on_disk),
            "balances": None,
            "txcount": None,
            "encryption": encryption_note(None),
        }
    balances, why, info = _balances_and_info(run)
    if balances is None:
        return {
            "state": WALLET_NOT_ESTABLISHED,
            "why": (
                f"no balance could be read, so whether this wallet holds anything is NOT "
                f"established -- it is not zero. {why}"
                + (f" (listwallets: {listed_error})" if listed_error else "")
            ),
            "loaded": names or [],
            "on_disk": _wallet_dir_names(on_disk),
            "balances": None,
            "txcount": None,
            "encryption": encryption_note(info),
        }
    txcount = info.get("txcount") if isinstance(info, Mapping) else None
    return {
        "state": WALLET_LOADED,
        "why": "",
        "loaded": names if names is not None else ["(this daemon has no listwallets; one wallet)"],
        "on_disk": _wallet_dir_names(on_disk),
        "balances": balances,
        "txcount": txcount,
        "encryption": encryption_note(info),
    }


#: What encryption_note() can say about the lock, so a renderer with one line per
#: case can be tested for covering them all rather than discovering a gap on a
#: screen -- the same guard WALLET_STATES above carries.
LOCK_ENCRYPTED = "encrypted"
LOCK_NOT_ENCRYPTED = "not_encrypted"
LOCK_NOT_ESTABLISHED = "not_established"
LOCK_STATES = (LOCK_ENCRYPTED, LOCK_NOT_ENCRYPTED, LOCK_NOT_ESTABLISHED)


def encryption_note(wallet_info: object) -> dict:
    """Is this wallet encrypted, and what does the surface say about it? PURE.

    Takes the `getwalletinfo` RESPONSE rather than an adapter, so it costs no call
    and can be asserted with a seeded dict (rule 10). wallet_state() above hands it
    the one response it already read.

    WHY A READ-ONLY SURFACE REPORTS THIS AT ALL. Measured on the operator's host
    2026-10-10: `chain_balances.py` printed `wallet ENCRYPTED -- a payout needs a
    passphrase (getwalletinfo reports unlocked_until)` for GRC. That is the single
    fact that decides whether a GRC payout can be made at all, it is invisible in
    every balance figure on every other screen, and an operator reading a healthy
    balance beside a locked wallet has no way to tell that nothing can leave it.

    AND WHY IT OFFERS NOTHING. There is no unlock here, no form and no field -- see
    this module's header. The three states are a REPORT; `fund_desk.staking_verdict()`
    is what interprets the VALUE of `unlocked_until` for a payout (locked, unlocked
    until a moment, unlocked in the past), and that reading belongs with the tool
    that is about to spend rather than with a dashboard. Rule 8 asks for the pointer
    rather than the copy, so this names it and stops at presence.

    THE THIRD STATE IS NOT "NOT ENCRYPTED", which is the whole reason this is a
    classifier. chains/wallet_lock.encryption_state() assumes ENCRYPTED when it
    cannot read getwalletinfo, because it is about to unlock and that is the safe
    direction for a spender. A viewer has a third option a spender does not: say it
    could not tell. Reporting "not encrypted" for a daemon that never answered would
    be a measurement nobody took (rule 17), and reporting "encrypted" would send an
    operator looking for a passphrase that may not exist.
    """
    if not isinstance(wallet_info, Mapping):
        return {
            "state": LOCK_NOT_ESTABLISHED,
            "why": (
                "no getwalletinfo response to read, so whether this wallet has a passphrase is "
                "NOT established -- it is not a claim that it has none. chains/wallet_lock.py "
                "assumes encrypted in this case because it is about to unlock; this page is "
                "only reading, so it says it does not know."
            ),
            "unlocked_until": None,
        }
    if not wallet_is_encrypted(wallet_info):
        return {
            "state": LOCK_NOT_ENCRYPTED,
            "why": (
                f"getwalletinfo reports no `{ENCRYPTION_FIELD}`, and a Bitcoin-derived daemon "
                f"reports that field ONLY for an encrypted wallet -- so this wallet has no "
                f"passphrase and nothing has to be unlocked before it can sign."
            ),
            "unlocked_until": None,
        }
    return {
        "state": LOCK_ENCRYPTED,
        "why": (
            f"getwalletinfo reports `{ENCRYPTION_FIELD}`, so this wallet IS encrypted and a "
            f"payout from it needs a passphrase. This page will not ask for one and has no "
            f"field for one; `fund_desk.py` is the tool that unlocks, and its "
            f"staking_verdict() is what reads the value beside this line."
        ),
        "unlocked_until": wallet_info.get(ENCRYPTION_FIELD),
    }


def _wallet_dir_names(on_disk: object) -> list[str]:
    """Wallet names from listwalletdir, or []. Shape-checked because it is nested.

    `{"wallets": [{"name": "..."}]}` is the documented shape and a daemon that
    lacks the method returns None here, so every level is checked rather than
    indexed -- a report that raises on an older daemon tells the operator nothing.
    """
    if not isinstance(on_disk, Mapping):
        return []
    entries = on_disk.get("wallets")
    if not isinstance(entries, list):
        return []
    return [str(entry.get("name", "")) for entry in entries if isinstance(entry, Mapping)]


#: Core's Overview pane shows three numbers under these exact words, so the panel
#: uses the same words. An operator who knows the wallet should not have to learn a
#: second vocabulary for the same quantities (rule 11, applied to a label).
#:
#: getwalletinfo's key -> what Core's GUI calls it, and why it is separate.
_BALANCE_FIELDS = (
    ("balance", "available", "spendable now; this is the number a send can draw on"),
    ("unconfirmed_balance", "pending", "in the mempool or below your confirmation target"),
    ("immature_balance", "immature", "coinbase, locked for 100 blocks after it was mined"),
)


def wallet_balances(run) -> tuple[dict | None, str]:
    """Core's three Overview numbers, or (None, why). Two routes, because the chains differ.

    THIS DOCSTRING SAID "`getwalletinfo` IS THE MODERN ROUTE AND GRIDCOIN DOES NOT
    HAVE IT" AND THAT IS WRONG. Established from the tree on 2026-10-10, not from a
    daemon -- I cannot reach the operator's Gridcoin node from here, so this is the
    weaker evidence and says so (rule 17). THREE call sites read
    `getwalletinfo` against the GRC adapter and depend on its answer:
    `fund_desk.py:877` (`before.get("unlocked_until")`, the staking unlock),
    `regtest/funding_steps.py:1076` (the same field, in the GRC funding walk), and
    `chains/wallet_lock.encryption_state()`, which is reached for GRC through
    `wallet_lock.STAKING_CHAINS`. And the operator's own `chain_balances.py` run that
    day printed, for GRC, `wallet ENCRYPTED -- a payout needs a passphrase
    (getwalletinfo reports unlocked_until)`. A daemon that answers that field answers
    that method. `getwalletinfo` arrived in Bitcoin Core 0.9.2, well before the 0.17
    line Gridcoin's RPC surface predates, so the "pre-0.17" reasoning never applied
    to it -- the method that GRC does lack is the PLURAL `getbalances` (Core 0.19),
    which nothing in this module calls.

    BOTH FACTS ARE NOW ROWS IN chains/daemon_capabilities.py rather than only this
    paragraph, which is the point of that file: the old docstring CITED it as the
    authority for a claim it had no row for, so a reader who went to check found
    nothing and a reader who did not went on believing it. `getwalletinfo` is recorded
    present on all three with this correction as its provenance, and `getbalances` is
    recorded absent on GRC with the operator's own -32601 as its evidence -- so GRC's
    chain panel now prints that difference without this module knowing it exists.

    SO WHY THE getinfo FALLBACK STAYS, which is the honest half. It is not dead code
    and it is not for Gridcoin: `getwalletinfo` fails on any daemon with NO WALLET
    LOADED (-18) and on a `--disable-wallet` build, and `getinfo` still answers a
    bare `balance` on an older build that has it. The fallback returns the one number
    it HAS and says the other two are unavailable rather than rendering them as zero,
    which would claim a measurement nobody took. What changed is the claim about
    which chain needs it: none of the three is known to, and the route is kept for
    the wallet STATE rather than for a chain vintage.

    THE TWO-ROUTE SHAPE IS STILL chains/daemon_network.chain_network()'s and rule 8
    asks for the pointer: that one answers "which network", this one answers "what is
    the balance", and neither can be derived from the other.

    A TWO-TUPLE STILL, AND _balances_and_info() BELOW IS THE THREE-TUPLE FORM. Every
    caller that only wants the numbers keeps this signature; wallet_state() wants the
    getwalletinfo response as well (for `txcount` and for the lock) and used to get it
    by asking the daemon a second time. See wallet_state()'s docstring for the count.
    """
    balances, why, _info = _balances_and_info(run)
    return balances, why


def _balances_and_info(run) -> tuple[dict | None, str, object]:
    """wallet_balances(), plus the getwalletinfo response it read. One call, two readers.

    THE THIRD ELEMENT IS THE WHOLE POINT and it is `object` rather than `dict | None`
    deliberately: on the getinfo route there IS no getwalletinfo response, and on a
    daemon that answered something unexpected it is whatever arrived. Every reader
    `isinstance`-checks it, which is what keeps "the daemon did not say" out of the
    same shape as "the daemon said".
    """
    info, why = _read(run.node(), "getwalletinfo")
    if isinstance(info, Mapping):
        found = {}
        for key, label, note in _BALANCE_FIELDS:
            value = info.get(key)
            found[label] = {
                "value": None if value is None else float(value),
                "note": note,
                # NAMED, NOT OMITTED. A key this daemon does not carry has to read as
                # "this build does not report it", because an absent `immature` and an
                # immature of 0.0 are different facts and only one of them is a number.
                "reported": value is not None,
            }
        return found, "", info
    legacy, legacy_why = _read(run.node(), "getinfo")
    if isinstance(legacy, Mapping) and legacy.get("balance") is not None:
        return {
            "available": {
                "value": float(legacy["balance"]),
                "note": "spendable now; read from getinfo, which is all this daemon vintage has",
                "reported": True,
            },
            "pending": {"value": None, "note": "not reported by getinfo", "reported": False},
            "immature": {"value": None, "note": "not reported by getinfo", "reported": False},
        }, "", info
    return None, f"getwalletinfo: {why}; getinfo: {legacy_why}", info


#: How many recent transactions the Transactions pane asks for. Bounded because the
#: panel refreshes on a timer and `listtransactions` with no count returns 10 by
#: default and the whole wallet with a large one -- a pane that gets slower the
#: longer the desk runs is a pane an operator stops opening (rule 14's Ctrl-C).
TRANSACTION_ROWS = 25


def recent_transactions(run, count: int = TRANSACTION_ROWS) -> tuple[list[dict], str]:
    """Core's Transactions pane: the newest `count` wallet entries, newest first.

    FIELDS CHOSEN TO MATCH WHAT THE GUI's COLUMNS SHOW, so the pane answers the
    question an operator actually opens it for -- "did the coin arrive, and is it
    confirmed yet" -- rather than printing whatever the RPC happened to return.

    `category` IS KEPT VERBATIM from the daemon (send / receive / generate /
    immature / orphan) instead of being collapsed to a direction. `generate` and
    `immature` are the ones that matter on a desk that mines its own regtest coins,
    and both would read as `receive` if this normalized them.
    """
    rows, why = _read(run.node(), "listtransactions", "*", int(count))
    if not isinstance(rows, list):
        return [], why or "listtransactions did not return a list"
    found = []
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        found.append({
            "txid": str(row.get("txid", "")),
            "category": str(row.get("category", "")),
            "amount": None if row.get("amount") is None else float(row["amount"]),
            "fee": None if row.get("fee") is None else float(row["fee"]),
            "confirmations": row.get("confirmations"),
            "address": str(row.get("address", "")),
            "label": str(row.get("label", "")),
            "time": row.get("time"),
        })
    # NEWEST FIRST, because listtransactions returns OLDEST first and the GUI shows
    # newest at the top. An operator checking whether a send just went out looks at
    # the first row, and on a wallet with 25 entries the default order puts the one
    # they want last.
    found.reverse()
    return found, ""


def peer_rows(run) -> tuple[list[dict], str]:
    """Core's Peers pane. ZERO PEERS IS THE FINDING THIS EXISTS FOR.

    Measured on the operator's host 2026-10-10: both regtest daemons had
    `getconnectioncount` of 0. On regtest that is the default and it is invisible
    until it matters -- a transaction broadcast from a node with no peers is valid,
    confirms on that node's own chain, and is never seen by any other wallet. The
    operator spent the evening trying to get coins into an external wallet; this
    pane would have said in one row that nothing was connected to send them to.

    So an empty list is NOT an empty pane. The caller renders "(none) -- this node
    talks to nobody" because rule 14 is explicit that a blank gap is ambiguous
    between zero rows and a query that broke.

    TRIMMED TO SEVEN FIELDS out of getpeerinfo's thirty-odd. The pane refreshes on a
    timer and the rest is bytes-per-message-type histograms nobody reads in a
    browser; `subver` is kept because it answers "is that other node the same
    build", which is the question when two nodes will not talk.
    """
    peers, why = _read(run.node(wallet=False), "getpeerinfo")
    if not isinstance(peers, list):
        return [], why or "getpeerinfo did not return a list"
    found = []
    for peer in peers:
        if not isinstance(peer, Mapping):
            continue
        found.append({
            "addr": str(peer.get("addr", "")),
            "subver": str(peer.get("subver", "")),
            "inbound": bool(peer.get("inbound", False)),
            "pingtime": peer.get("pingtime"),
            "synced_blocks": peer.get("synced_blocks"),
            "banscore": peer.get("banscore"),
            "conntime": peer.get("conntime"),
        })
    return found, ""


def node_summary(run) -> dict:
    """Core's Information pane: chain, sync progress, disk, version, connections.

    EVERY FIELD IS OPTIONAL AND SAYS SO. This has to render against three daemons a
    decade apart in vintage, so each value carries its own presence rather than
    defaulting -- a `pruned: false` invented for a daemon that never said is a claim
    about disk retention, and on a chain where an HTLC needs `getrawtransaction` for
    an arbitrary txid that claim decides whether a spend path can work at all.

    `verificationprogress` IS RENDERED AS A FRACTION, not multiplied into a
    percentage here. The pane does that; this returns what the daemon said, because
    0.9999 and 100% are different things to an operator deciding whether a sync has
    finished and rounding is how the second one gets printed for the first.
    """
    chain, chain_why = _read(run.node(wallet=False), "getblockchaininfo")
    net, net_why = _read(run.node(wallet=False), "getnetworkinfo")
    summary = {"errors": [why for why in (chain_why, net_why) if why]}
    for source, keys in ((chain, ("chain", "blocks", "headers", "verificationprogress",
                                  "pruned", "size_on_disk", "initialblockdownload")),
                         (net, ("version", "subversion", "protocolversion", "connections"))):
        for key in keys:
            value = source.get(key) if isinstance(source, Mapping) else None
            summary[key] = {"value": value, "reported": value is not None}
    return summary


class _CountedNode:
    """One node, counting the calls made through it. Nothing else.

    `*params` is forwarded unexamined and no method name is inspected here: this
    counts, it does not police. The allowlist is refuse_unless_read_only() and the
    panes' fixed call sites, and a second gate hidden inside a counter would be a
    place for the two to disagree.
    """

    def __init__(self, tally: _Counted, node) -> None:
        self._tally = tally
        self._node = node

    def call(self, method: str, *params):
        self._tally.calls += 1
        return self._node.call(method, *params)


class _Counted:
    """A `run`-shaped wrapper whose `.calls` is how many reads the panes actually made.

    WHY THIS EXISTS AND NOT A LITERAL. `rpc_calls` was `6 if no wallet else 8`, two
    hand-written numbers, and BOTH were wrong when counted on 2026-10-10 against a
    recording node:

        no wallet loaded      claimed 6, makes 5   (listwallets, listwalletdir,
                                                    getpeerinfo, getblockchaininfo,
                                                    getnetworkinfo)
        wallet loaded         claimed 8, makes 7   (+ getwalletinfo, listtransactions)
        balance unreadable    claimed 8, makes 8   (+ the getinfo fallback)

    The 8 was right for one of three states by coincidence, and the duplicate
    getwalletinfo wallet_state() used to make meant the loaded path was really 8 for a
    different reason than the literal claimed. This is the figure an operator reads to
    account for traffic in their own daemon's log, so a number that drifts the first
    time a pane gains a read is a number that quietly stops meaning anything -- rule
    14's "state what the number means, next to the number", and rule 3's "measure
    before claiming". It is measured now, so it cannot drift at all.
    """

    def __init__(self, run) -> None:
        self._run = run
        self.calls = 0

    def node(self, wallet: bool = True):
        return _CountedNode(self, self._run.node(wallet))


def wallet_pane(run) -> dict:
    """Everything the four read panes need, in one payload. The tab's wallet half.

    ONE FUNCTION SO ONE REFRESH MAKES ONE PASS. Each pane's reads are independent
    and each tolerates its own failure, so a daemon missing `getpeerinfo` still
    renders a balance -- but they are gathered here rather than behind four routes,
    because four fetches per tab per refresh across four tabs is sixteen round trips
    for one screen.

    COUNTED, AND THE COUNT IS PART OF THE PAYLOAD. `rpc_calls` says how many reads
    this cost, because the panel polls and an operator watching a daemon's own log
    scroll should be able to account for the traffic this page generates rather than
    wondering what is hammering it. It is a tally of the calls that were made, not a
    literal -- see _Counted above for the two wrong literals it replaced.
    """
    run = _Counted(run)
    wallet = wallet_state(run)
    transactions, tx_why = ([], "no wallet is loaded, so there are no transactions to list") \
        if wallet["state"] == WALLET_NONE_LOADED else recent_transactions(run)
    peers, peer_why = peer_rows(run)
    return {
        "wallet": wallet,
        "transactions": {"rows": transactions, "error": tx_why, "asked_for": TRANSACTION_ROWS},
        "peers": {
            "rows": peers,
            "error": peer_why,
            # SAID HERE RATHER THAN LEFT TO THE TEMPLATE, because it is a finding and
            # not a layout choice. See peer_rows() for the 2026-10-10 measurement.
            "note": (
                "a node with NO peers broadcasts into nothing: the transaction is valid, it "
                "confirms on this node's own chain, and no other wallet ever sees it"
                if not peers and not peer_why else ""
            ),
        },
        "node": node_summary(run),
        "rpc_calls": run.calls,
    }


class OneNode:
    """A single `.call`-able handle, shaped as the `run` the panes above take.

    THE FOUR LINES THAT MADE THE EXTRACTION WORTH DOING. Every pane in this module
    reaches exactly `run.node(wallet).call(method, *params)` -- the `HasAChainNode`
    protocol in regtest/funding_steps.py, one member -- and the Flask application has
    no Run: it has a `chains/base.RPCAdapter` out of `chains/registry.build_adapters()`.
    This is the whole adaptation, and it is here rather than in services/ because the
    protocol it satisfies is this module's.

    ONE NODE FOR BOTH `wallet=True` AND `wallet=False`, WHICH IS A REAL LIMITATION AND
    IS NAMED RATHER THAN HIDDEN. regtest's Run answers those with two different
    adapters: a bare URL and a `/wallet/<name>` one. An application adapter is built
    once from Config.RPC with its wallet already baked into `.url`, so there is one
    URL here and `wallet=False` gets it too.

    WHAT THAT COSTS: on BTC or LTC with `<ASSET>_RPC_WALLET` set to a wallet the
    daemon does not have, Core answers EVERY call at that path with "Requested wallet
    does not exist" -- including `listwallets`, which is the one call that could have
    said which wallets it does have. So the pane reports "not established" where a
    bare-URL node would have listed them.

    WHY THAT IS THE RIGHT TRADE ANYWAY, and the alternative is worse: building a
    second, bare-URL adapter means reading `.user` and `.password` off this one and
    passing them to a constructor. services/admin_view._endpoint_text() records why
    that boundary is held -- "Nothing in this function can reach a password even if
    one is set" -- and spending it to improve one error message on a misconfigured
    wallet name is not a trade a read-only surface should make. The configuration that
    produces it is reported INSTEAD, by chains/daemon_capabilities.wallet_path_warning(),
    which the panel renders beside the wallet pane.
    """

    def __init__(self, node) -> None:
        self._node = node

    def node(self, wallet: bool = True):
        # `wallet` IS ACCEPTED AND IGNORED, which the protocol permits -- funding_steps
        # .HasAChainNode declares `wallet: bool = ...` precisely because "the default's
        # VALUE is the implementation's business". Ignored, not asserted on: a pane
        # asking for the bare node is not making a mistake, it is asking for something
        # this handle cannot give it. See the class docstring for what that costs.
        return self._node
