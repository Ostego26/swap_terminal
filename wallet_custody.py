#!/usr/bin/env python3
"""Is the desk's hot wallet the operator's own wallet? Read-only, per chain.

Role: file (operator entry point at the repository root per CLAUDE.md rule 10;
      every decision it prints is a function in
      services/custody_separation.py, callable with seeded inputs)
Reads: config.Config for the endpoints and the custody variables; the process
      ENVIRONMENT for the operator's own GRC endpoint (GRC_OPERATOR_RPC_*, read
      through gridcoin_credentials.operator_endpoint() and from nowhere else --
      never a .conf, never a .env);
      swap_terminal.db (the `swaps` table only, and only with --swap);
      and over the network, per Bitcoin-derived chain whose port classifies as a
      TEST chain: getwalletinfo, listwallets, and -- only with --swap --
      validateaddress/getaddressinfo. Against the operator's own GRC daemon, when
      it is named and its port classifies as a test chain: ONE read,
      validateaddress/getaddressinfo for the desk's deposit address, and nothing
      that carries a balance. XRP: no network call at all. SOL: no network call
      at all.
Writes: NOTHING. Not a row, not a file, not a wallet. The database path is
      checked with Path.exists() before connect(), because sqlite3.connect()
      CREATES a missing file and a read-only tool that leaves an empty database
      behind has written something while announcing that it would not (the same
      guard, for the same reason, as show_unattributable.py and show_fees.py).
Can move funds: no. It calls no getnewaddress -- which is a WALLET WRITE, not a
      read -- no sendtoaddress, no dumpprivkey, no walletpassphrase. It opens no
      keypair file, reads no WIF, and never touches wallet.dat. The one secret
      this question touches, XRP_PAYOUT_SECRET_SEED, is read ONLY inside
      chains/xrp_payout_seed.derived_payout_account(), which hands back a public
      classic address and never the value.
Mainnet-safe: it REFUSES a mainnet or unrecognized port rather than reading it,
      through network_target.may_read_a_wallet(), BEFORE any socket opens --
      because getwalletinfo carries a balance and reading one means printing it.
      157,797 GRC of the operator's real staking balance reached a chat log that
      way on 2026-09-25.

WHY THIS EXISTS, MEASURED 2026-10-03

The operator's instruction, in their words that day: "these two hot wallets are
used by the swap terminal machine NOT THE USER." This is a CUSTODIAL desk --
the customer deposits to an address the desk owns, and the desk pays out of its
own inventory -- so a GRC->XRP swap has to move 500 GRC OUT of the customer's
wallet and INTO the terminal's, and 3+ XRP OUT of the terminal's XRP account and
INTO the customer's.

THAT IS NOT WHAT HAPPENED. Three things were measured on the operator's host
that day, and the first two are theirs rather than mine -- I have no network
path to their daemons from here and could not re-run them (rule 17: say which
you have):

  1. swap s_539d922e9ef0a5d8 completed. Its 500 GRC deposit went to
     moaSBv8gcwXRnmQhxJJAjUvXMd542jsNNz, derived by getnewaddress on the
     operator's OWN gridcoinresearchd, and their wallet balance moved
     3780.08654497 -> 3780.08554497: the 0.001 fee and nothing else. The
     transaction carries `category: send` AND `category: receive` for one
     address. A self-transfer. No custody moved.
  2. the XRP payout went rnjG8n16JinjqkzZj5Jmw6NDMBMzhhNbVv ->
     rBfM7je6e9Ca2cMvuRn7cr9xExFgDa5NGx, Amount 3315589 drops, Fee 10,
     tesSUCCESS, validated. Both are the operator's own testnet faucet accounts.
  3. MEASURED HERE, in this tree, and this is the mechanism behind 1:

         BTC_RPC_WALLET -> ''     config.py:447
         LTC_RPC_WALLET -> ''     config.py:455
         GRC_RPC_WALLET -> ''     config.py:520

     chains/base.RPCAdapter.url appends `/wallet/<name>` only when that value is
     non-empty, so an empty one addresses the daemon with NO wallet path and the
     daemon routes to the wallet it serves by default. Verified by construction:
     RPCAdapter(wallet="") -> http://127.0.0.1:25715, and
     RPCAdapter(wallet="desk_hot") -> http://127.0.0.1:25715/wallet/desk_hot.
     The default wallet is the same wallet `gridcoinresearchd getnewaddress`
     reaches, so the desk's deposit addresses and the operator's own coins are in
     one wallet -- which is exactly what makes a deposit into one a self-transfer.

WHAT THIS TOOL WILL NOT CLAIM, AND WHY THAT IS THE DESIGN RATHER THAN A GAP

Nothing on any of these five chains says whose money a coin is. There is no
`isdesks` field beside `ismine`. So a check that announced "the desk's wallet is
separate from the user's" would be a hypothesis in the register of a measurement
(rule 17) -- the exact failure that costs an operator the ability to tell which
they are holding. What this establishes is narrower and true:

  BTC/LTC/GRC   WHICH WALLET this process's endpoint actually serves, from the
                daemon's own getwalletinfo().walletname -- never from the config
                text, because "the code contains a check for X" is not evidence
                of X. Plus, from listwallets, whether a bare CLI call with no
                -rpcwallet would reach that same wallet, which is the difference
                between a named wallet and an ENFORCED separation.
  per swap      whether the payout wallet reports ismine for that swap's own
                deposit address. True is CORRECT for a custodial desk and is not
                the finding; the finding is that line read beside the wallet line.
  GRC           and ONLY GRC: whether the OPERATOR's OWN daemon reports ismine for
                the desk's deposit address. Measured 2026-10-03 on their Gridcoin
                v5.5.1.0 testnet daemon, `help` lists none of createwallet,
                loadwallet, listwallets or unloadwallet -- one wallet per datadir,
                no -rpcwallet, no /wallet/<name>, no `walletname` field -- so the
                BTC/LTC route CANNOT work here and no configuration can make it.
                Asking the other daemon is the only separation evidence available,
                and it is stronger than a config read: an `ismine: false` from the
                daemon holding the operator's coins is an observation about a KEY.
                `ismine: true` from both is proof of ONE wallet -- or of a
                wallet.dat that was copied, which no other check in this tree
                could detect. The desk address comes from the swap row, never from
                getnewaddress, because that is a wallet WRITE.
  XRP           whether XRP_DEPOSIT_ACCOUNT is the account the payout seed
                controls. ONE desk account serving both directions is the
                custodial design here, not a defect -- and whether a payout LEFT
                that account is the separate question, asked per swap.
  SOL           whether SOL_DEPOSIT_ACCOUNT and SOL_HOT_WALLET are set and are
                two different accounts.

AND WHAT IT CANNOT: that the coins in a named wallet were never the operator's,
and that an address the desk does not control belongs to the customer. Both
limits are printed at the top of every run, before any verdict, because a limit
in a footnote has already been misread by the time it is reached.

WHY A ROOT TOOL AND NOT A CHECK IN swap_readiness.py, DECIDED RATHER THAN SPLIT

swap_readiness.py answers "would a swap work, and if not which line do I
change". A shared wallet does not stop a swap working -- swap s_539d922e9ef0a5d8
completed -- so a FAIL there would make that gate unsatisfiable on the operator's
host today, which is the failure swap_readiness.legs_to_check()'s own docstring
names: "a gate that can never open is not a gate; it is a thing people learn to
bypass, which is worse than no gate because the next real failure gets bypassed
with it." A PASS or SKIP line would be noise in a report that already has
fourteen. So the verdicts live here, and what swap_readiness gained instead is
honesty in the line it ALREADY printed: its `{asset} wallet  loaded (name)` now
says what an unnamed wallet means and names this tool.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "swap_terminal"))

from chains.base import AddressOwnership
from chains.gridcoin import GridcoinAdapter
from chains.registry import build_adapters, why_unconfigured
from chains.xrp_payout_seed import SIGNING_SEED_ENV_VAR, derived_payout_account
from config import Config
from db import connect_db
from gridcoin_credentials import (
    OPERATOR_PORT_VARIABLE,
    OPERATOR_REQUIRED_VARIABLES,
    OPERATOR_RPC_TIMEOUT_SECONDS,
    OperatorEndpoint,
    operator_endpoint,
)
from microfortnights import format_duration
from network_target import may_read_a_wallet
from report_block import CONTINUATION, labeled, wrapped
from services.custody_separation import (
    BY_DESIGN,
    DESK_OWNERSHIP_FROM_STATE,
    DESK_OWNS,
    NOT_ESTABLISHED,
    SEPARATED,
    STATES,
    cross_daemon_ownership_verdict,
    deposit_address_verdict,
    script_chain_verdict,
    solana_account_verdict,
    what_this_cannot_establish,
    xrp_desk_account_verdict,
    xrp_payout_destination_verdict,
)
from workers.common import db_path_source, get_config_dict, root_tool_command

#: The chains whose custody separation is a WALLET on a Bitcoin-derived daemon.
#: Named here rather than written into three loops, and deliberately in the same
#: order chains/registry and swap_readiness use so two reports scan alike.
SCRIPT_CHAINS = ("BTC", "LTC", "GRC")

#: The one chain whose separation is established by asking a SECOND daemon, and
#: the only chain where that is the only route available.
#:
#: MEASURED ON THE OPERATOR'S GRIDCOIN v5.5.1.0 TESTNET DAEMON, 2026-10-03:
#:
#:     gridcoinresearchd -testnet help | grep -iE '^(createwallet|loadwallet|listwallets|unloadwallet)'
#:       -> (none of the multiwallet RPCs exist on this build)
#:
#: So Gridcoin has ONE wallet per datadir -- no -rpcwallet, no /wallet/<name>
#: endpoint, no `walletname` field -- and the `GRC wallet` line above answers
#: NOT ESTABLISHED for a field that does not exist rather than for a field nobody
#: configured. BTC and LTC do carry those RPCs (docs/hot_wallet_separation_runbook.md
#: has the createwallet steps that worked for both), so they need no second daemon
#: and deliberately do not get this line: a check that asked a second endpoint on a
#: chain where the first endpoint can answer would be two mechanisms for one
#: question (rule 8).
CROSS_DAEMON_CHAIN = "GRC"

#: The check name the cross-daemon line reads the DESK's half of its comparison
#: from, and it is spelled once here because script_chain_lines() produces it as
#: f"{asset} deposit addr". The two have to agree or the cross-daemon verdict
#: silently loses the desk's answer and reports "not read" forever --
#: tests/test_custody_separation.py pins them against each other for that reason.
DESK_DEPOSIT_CHECK = f"{CROSS_DAEMON_CHAIN} deposit addr"

#: The states that mean the question was answered AND the answer is the one a
#: custodial desk wants. Everything else -- NOT SEPARATED, NOT THE DESK'S,
#: MISCONFIGURED, NOT ESTABLISHED -- makes the exit code non-zero, which is what
#: "never a green verdict by default" has to mean for a tool somebody may put in
#: a script.
#:
#: DESK_OWNS IS IN HERE AND THAT IS THE POINT OF ITS EXISTING. A payout wallet
#: that holds the key for its own swap's deposit address is a custodial desk
#: working correctly; counting it as a failure -- which the first draft did, by
#: rendering it as NOT SEPARATED -- made this tool disagree with the design it
#: inspects, and would have had an operator "fixing" the one line that was right.
GOOD_STATES = (SEPARATED, BY_DESIGN, DESK_OWNS)


class Refused(Exception):
    """This tool will not run, and the message says what the operator can do about it."""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Per chain: is the desk's hot wallet distinct from the daemon's default wallet and from "
            "the operator's own accounts? Read-only -- it derives no address, signs nothing, and "
            "reads no key file."
        ),
        # DERIVED FROM GOOD_STATES RATHER THAN RESPELLED, and it had already drifted:
        # this sentence said "SEPARATED or ONE DESK ACCOUNT" after that state was
        # renamed to BY DESIGN (because the longer spelling was cut mid-word in the
        # tally column), and it never named DESK OWNS IT at all. A hand-maintained
        # copy of a vocabulary is rule 8's bug with a delay on it, and the delay on
        # this one expired the same day it was written.
        epilog=(
            f"Exit 0 only when every line answered one of: {', '.join(GOOD_STATES)}. NOT ESTABLISHED is "
            "never a pass: a daemon that did not answer exits non-zero with the reason printed, "
            "because a green verdict by default is the defect this tool exists to remove."
        ),
    )
    parser.add_argument(
        "--swap", default="", metavar="SWAP_ID",
        help=(
            "Also answer the per-swap questions for this swap: is its deposit address inside the "
            "payout wallet, (GRC) does the OPERATOR's own daemon hold the key for it, and (XRP) did "
            "its payout leave the desk's account. Without it those lines say they were not asked, "
            "rather than being silently absent. The GRC cross-daemon question NEEDS a swap: its "
            "address is the swap's own deposit_address, because the alternative -- getnewaddress -- "
            "is a wallet WRITE."
        ),
    )
    parser.add_argument(
        "--db", default="",
        help="Database path, overriding config. Only read with --swap. Default: the path the workers use.",
    )
    return parser


def swap_row(args) -> tuple[dict | None, str]:
    """The swap --swap names, or (None, why not). Opens nothing without --swap.

    (row, note) RATHER THAN RAISING for a missing row, because a swap id typed
    wrong and a database with no such swap are both "the per-swap lines cannot be
    answered", and the chain-level verdicts below are still worth printing. A
    REFUSAL is reserved for a database path that does not exist, where continuing
    would mean sqlite3.connect() creating one.
    """
    if not args.swap:
        return None, (
            "no --swap was given, so the two per-swap questions were NOT asked: whether a swap's "
            "deposit address is inside the payout wallet, and whether its payout left the desk's "
            "account. Pass --swap <id> to ask them"
        )
    config = get_config_dict()
    db_path = Path(args.db) if args.db else Path(config["DB_PATH"])
    source = db_path_source(str(db_path), explicit_db=args.db)
    if not db_path.exists():
        raise Refused(
            f"no database at {db_path} ({source}). Nothing was created and no chain was contacted. "
            f"Run without --swap for the chain-level verdicts, which need no database."
        )
    db = connect_db(str(db_path))
    row = db.execute(
        "SELECT id, from_asset, to_asset, deposit_address, deposit_tag, payout_address, status, "
        "payout_txid FROM swaps WHERE id = ?",
        (args.swap,),
    ).fetchone()
    if row is None:
        return None, (
            f"no swap {args.swap!r} in {db_path} ({source}), so the two per-swap questions were NOT "
            f"answered. The chain-level verdicts below do not depend on it"
        )
    return dict(row), f"swap {row['id']}  {row['from_asset']} -> {row['to_asset']}  status={row['status']}"


def operator_daemon_note() -> str:
    """host:port of the operator's own GRC daemon for the header, or what is unset.

    NEVER A CREDENTIAL. OperatorEndpoint.label is host:port by construction and
    this function returns nothing else; gridcoin_credentials.operator_endpoint()'s
    refusal names only variable NAMES. A header line that echoed what was SET
    would echo GRC_OPERATOR_RPC_PASS on the one run somebody pastes.

    A SENTENCE AND NOT A BLANK WHEN UNSET, because rule 14's `(none)` is a result:
    an absent second endpoint means the GRC separation question goes unanswered,
    and a header that said nothing about it would leave the reader to discover
    that forty lines down.
    """
    endpoint, _refusal = operator_endpoint()
    if endpoint is None:
        return (
            f"(not named) -- {', '.join(OPERATOR_REQUIRED_VARIABLES)} unset, so the "
            f"{CROSS_DAEMON_CHAIN} cross-daemon ownership question is NOT asked"
        )
    # THE PORT IS CLASSIFIED FOR THE HEADER TOO, and it was not at first: with
    # GRC_OPERATOR_RPC_PORT=15715 this line read "127.0.0.1:15715 <- asked ONE
    # read", promising a read that cross_daemon_lines() then refuses to make. A
    # header that announces work the tool will not do is rule 14's defect in the one
    # place rule 14 is about -- and it announces it about a MAINNET daemon, which is
    # the one holding real coins. Same may_read_a_wallet() as the refusal itself, so
    # the two cannot disagree.
    connect, why_port = may_read_a_wallet(CROSS_DAEMON_CHAIN, endpoint.port)
    if not connect:
        # AND THE VARIABLE IT NAMES IS CORRECTED, because may_read_a_wallet()'s
        # sentence is the chain-level one and says "Set GRC_RPC_PORT" -- the DESK's
        # variable. Following it here would repoint the desk's own endpoint and
        # leave this one untouched. The shared sentence is not edited (one mainnet
        # refusal in this tree, rule 8); the caller that borrowed it says which
        # variable is its own.
        return (
            f"{endpoint.label}  <- WILL NOT BE READ: {why_port}. The variable to change for THIS "
            f"endpoint is {OPERATOR_PORT_VARIABLE}, not the {CROSS_DAEMON_CHAIN}_RPC_PORT that "
            f"sentence names -- that one is the desk's"
        )
    return (
        f"{endpoint.label}  <- {why_port}; asked ONE read with --swap: ismine for the desk's "
        f"{CROSS_DAEMON_CHAIN} deposit address"
    )


def header_lines(args, row_note: str) -> list[str]:
    """What is about to be read, and with what, BEFORE a socket opens.

    Rule 14 start to finish: announce the target and the scale up front, echo the
    parameters that decide the answer, and always print the NETWORK -- a line that
    does not say mainnet or testnet is a line that will eventually be read as the
    wrong one. Here the network per chain is what may_read_a_wallet() decides, so
    it appears on each chain's own first line rather than being claimed once here.
    """
    lines = [
        "wallet custody -- is the desk's hot wallet the operator's own?  READ-ONLY.",
        "  no getnewaddress (that is a wallet WRITE), no sendtoaddress, no key file, no wallet.dat.",
        "",
        labeled("chains asked", f"{', '.join(SCRIPT_CHAINS)} by wallet; XRP and SOL by account"),
        labeled("swap scope", row_note),
        # THE SECOND ENDPOINT, ECHOED BEFORE IT IS USED (rule 14), and as host:port
        # only: OperatorEndpoint.label carries no credential and the refusal names
        # variable NAMES rather than values, so this line is safe to paste. On GRC
        # this is the ONLY separation evidence available -- the daemon family has no
        # multiwallet RPCs and so no `walletname` to compare -- which is why an
        # unset endpoint is announced up here rather than discovered at the verdict.
        labeled("operator daemon", operator_daemon_note()),
        labeled("database", "(not opened) -- no --swap, so nothing reads swap_terminal.db"
                if not args.swap else str(Path(args.db) if args.db else Path(get_config_dict()["DB_PATH"]))),
        "",
        "WHAT THIS CANNOT ESTABLISH, said before any verdict rather than in a footnote:",
    ]
    for limit in what_this_cannot_establish():
        lines.extend(wrapped("  --", limit))
    lines.append("")
    return lines


def read_script_chain(asset: str, adapter) -> tuple[object, str, tuple[str, ...] | None]:
    """(walletinfo, read_error, loaded_wallets). TWO READS, no write, never raises.

    getwalletinfo IS THE BEHAVIORAL QUESTION and `walletname` is its answer: what
    this endpoint is SERVING, as opposed to what the process asked for. listwallets
    is the second read and it is what turns a name into a measurement -- Bitcoin
    Core refuses a bare wallet RPC with rpc code -19 when more than one wallet is
    loaded, so "another wallet is also loaded" is the daemon enforcing the
    separation rather than configuration describing it.

    LISTWALLETS FAILING COSTS ONLY THAT SECOND CLAUSE, which is why its failure
    returns None rather than becoming the chain's error: it is absent on a daemon
    built without wallet support and on older builds (Gridcoin among them), and
    services/custody_separation.script_chain_verdict() renders None as "whether a
    bare CLI call also reaches this wallet was not established" instead of as a
    stronger or weaker verdict than it has.
    """
    try:
        info = adapter.call("getwalletinfo")
    except Exception as error:  # noqa: BLE001 -- checked: the type and message are RETURNED as `read_error` and rendered as NOT ESTABLISHED with the reason, so a down daemon, a refused login and an unloaded wallet cannot be mistaken for an answer about custody. Nothing here can produce a green verdict.
        return None, f"{type(error).__name__}: {error}", None
    try:
        loaded = adapter.call("listwallets") or []
    except Exception:  # noqa: BLE001 -- checked: listwallets is absent on older daemons and its absence costs ONE clause, which the verdict then reports as not established rather than as an answer. The chain's verdict comes from getwalletinfo above, which already succeeded.
        return info, "", None
    return info, "", tuple(str(name) for name in loaded)


def script_chain_lines(asset: str, adapters: dict, row: dict | None) -> list[tuple[str, str, str]]:
    """Every (check, state, why) for one Bitcoin-derived chain. Reads, never writes.

    THE ORDER IS THE ORDER THAT COSTS LEAST, exactly as swap_readiness.
    check_bitcoin_like() orders its three: no adapter is answered from config with
    no socket, the PORT is classified before a socket opens, and only then is the
    wallet asked. A tool that read the wallet first and classified afterwards has
    already printed the balance it was refusing to read.
    """
    adapter = adapters.get(asset)
    if adapter is None:
        return [(f"{asset} wallet", NOT_ESTABLISHED, (
            f"{asset}: no adapter in this process, so no daemon was asked and nothing about this "
            f"chain's custody was established -- {why_unconfigured(asset, Config.RPC)}"
        ))]
    port = Config.RPC[asset]["port"]
    connect, why_port = may_read_a_wallet(asset, port)
    if not connect:
        return [(f"{asset} wallet", NOT_ESTABLISHED, (
            f"{asset}: {why_port}. NO SOCKET WAS OPENED, so nothing about this chain's custody was "
            f"established -- and that is the safe direction: getwalletinfo carries a balance, and "
            f"reading one means printing it"
        ))]
    started = time.monotonic()
    info, read_error, loaded = read_script_chain(asset, adapter)
    # .get() AND NOT [..]: an absent key and an empty value both mean "no named
    # wallet was configured for this chain", which is what the verdict reads, and
    # a KeyError here would kill a report over a missing dict key.
    configured = str(Config.RPC[asset].get("wallet") or "")
    verdict = script_chain_verdict(asset, configured, info, read_error, loaded)
    elapsed = format_duration(time.monotonic() - started)
    lines = [(f"{asset} wallet", verdict.state, f"{verdict.why}  [{why_port}; read in {elapsed}]")]
    if row is None or str(row["from_asset"]) != asset:
        return lines
    # address_ownership() AND NOT owns_address(), because the three-valued bool
    # cannot carry WHY it is None -- and the two Nones need opposite responses. A
    # transport failure is unknown and knowable; a daemon with no `ismine` field
    # cannot be asked again to any effect. chains/base.py records the 2026-10-01
    # measurement where exactly that distinction was collapsed and the wrong one
    # was reported to the operator as a fact.
    deposit_address = str(row["deposit_address"])
    ownership = adapter.address_ownership(deposit_address)
    owned = deposit_address_verdict(asset, str(row["id"]), deposit_address, ownership.verdict, ownership.why)
    lines.append((f"{asset} deposit addr", owned.state, owned.why))
    return lines


def read_operator_ownership(endpoint: OperatorEndpoint, address: str) -> AddressOwnership:
    """ONE read against the operator's own daemon: `ismine` for the desk's address.

    validateaddress / getaddressinfo AND NOTHING ELSE. chains/base.
    RPCAdapter.address_ownership() tries both and returns the three-valued answer
    plus the reason, so this function adds a connection and no behavior. It calls
    no getwalletinfo and no getbalance against the operator's daemon ON PURPOSE:
    those carry a balance, and 157,797 GRC of the operator's real staking balance
    reached a chat log on 2026-09-25 because something read one. `ismine` is a
    boolean about one address and carries no amount.

    NO WALLET PATH, AND THAT IS NOT AN OMISSION. `wallet=""` addresses the daemon
    as `http://host:port` with no /wallet/<name> segment, which is the wallet a
    bare `gridcoinresearchd` CLI call reaches -- and on this daemon family it is
    the ONLY wallet there is (measured 2026-10-03: no multiwallet RPCs exist). The
    operator's coins are in it, which is exactly why its answer is the evidence.

    IT CANNOT RAISE, which is why there is no try/except here and no `noqa` to
    justify. address_ownership() catches per method, collects the reasons and
    returns AddressOwnership(None, why) -- the shape that let the 2026-10-01
    transport failure be told apart from a capability gap instead of collapsing
    into one None.
    """
    adapter = GridcoinAdapter(
        user=endpoint.user,
        password=endpoint.password,
        host=endpoint.host,
        port=endpoint.port,
        wallet="",
        timeout=OPERATOR_RPC_TIMEOUT_SECONDS,
    )
    return adapter.address_ownership(address)


def cross_daemon_lines(row: dict | None, results: list[tuple[str, str, str]]) -> list[tuple[str, str, str]]:
    """GRC only: ask the OPERATOR's daemon whether the DESK's deposit address is theirs.

    THE ORDER IS THE ORDER THAT COSTS LEAST, the same order script_chain_lines()
    uses and for the same reason. No GRC deposit leg is answered from the database
    row with no socket; the second endpoint's variables are resolved from the
    ENVIRONMENT next, and an unset one refuses before anything is built; the PORT
    is classified by network_target.may_read_a_wallet() after that and before any
    connection, so a mainnet or unrecognized port is refused with no socket opened;
    and only then is the operator's daemon asked one read.

    THE DESK'S HALF IS NOT RE-READ. It is translated back out of the state this
    run already recorded for DESK_DEPOSIT_CHECK, through
    services/custody_separation.DESK_OWNERSHIP_FROM_STATE. A second socket to the
    desk's endpoint asking the question it was already asked is two chances for one
    report to disagree with itself (rule 8), and the answer is already on the
    screen two lines up.

    NOTHING HERE PRINTS A CREDENTIAL. The only rendering of the second endpoint is
    OperatorEndpoint.label, which is host:port; the password is read by
    requests' basic auth inside RPCAdapter.call() and reaches no string this
    function builds. gridcoin_credentials.operator_endpoint()'s refusals name
    variable NAMES and never values, for the same reason.
    """
    name = f"{CROSS_DAEMON_CHAIN} operator daemon"
    swap_id = str(row["id"]) if row is not None else ""
    # EMPTY ADDRESS IS THE ONE INPUT THAT ROUTES STRAIGHT TO THE VERDICT, because
    # "no --swap", "a swap whose deposit leg is another chain" and "a row with no
    # deposit address" are one answer -- this check cannot run -- and
    # cross_daemon_ownership_verdict()'s first branch already says it in the words
    # an operator acts on. A second sentence here would be a second spelling of it.
    if row is None or str(row["from_asset"]) != CROSS_DAEMON_CHAIN:
        verdict = cross_daemon_ownership_verdict(CROSS_DAEMON_CHAIN, swap_id, "", "", None)
        return [(name, verdict.state, verdict.why)]
    deposit_address = str(row["deposit_address"] or "")
    if not deposit_address:
        verdict = cross_daemon_ownership_verdict(CROSS_DAEMON_CHAIN, swap_id, "", "", None)
        return [(name, verdict.state, verdict.why)]
    endpoint, refusal = operator_endpoint()
    if endpoint is None:
        return [(name, NOT_ESTABLISHED, (
            f"{CROSS_DAEMON_CHAIN}: {refusal}. Without it the GRC separation question is UNANSWERED "
            f"rather than answered either way -- this daemon family has no `walletname` to compare, "
            f"so the second daemon is the only route there is"
        ))]
    connect, why_port = may_read_a_wallet(CROSS_DAEMON_CHAIN, endpoint.port)
    if not connect:
        return [(name, NOT_ESTABLISHED, (
            f"{CROSS_DAEMON_CHAIN}: the operator's endpoint is {endpoint.label} and {why_port}. NO "
            f"SECOND SOCKET WAS OPENED, so nothing about GRC custody was established -- and that is "
            f"the safe direction: a mainnet daemon is the one holding real coins, and this check "
            f"would be reading it. Set {OPERATOR_PORT_VARIABLE} to the operator's TESTNET rpcport "
            f"-- and note that the sentence above names {CROSS_DAEMON_CHAIN}_RPC_PORT, which is the "
            f"DESK's variable rather than this one"
        ))]
    started = time.monotonic()
    ownership = read_operator_ownership(endpoint, deposit_address)
    elapsed = format_duration(time.monotonic() - started)
    desk_state = next((state for check, state, _why in results if check == DESK_DEPOSIT_CHECK), "")
    verdict = cross_daemon_ownership_verdict(
        CROSS_DAEMON_CHAIN,
        swap_id,
        deposit_address,
        endpoint.label,
        ownership.verdict,
        ownership.why,
        DESK_OWNERSHIP_FROM_STATE.get(desk_state),
    )
    return [(name, verdict.state, f"{verdict.why}  [{why_port}; one read in {elapsed}]")]


def xrp_lines(row: dict | None) -> list[tuple[str, str, str]]:
    """The XRP account questions. NO NETWORK CALL AT ALL, and that is deliberate.

    Both answers are local: XRP_DEPOSIT_ACCOUNT is config, and the account the
    signing seed controls is base58check plus a key derivation inside
    chains/xrp_payout_seed.derived_payout_account(). So this reports on a host
    with no rippled reachable, which is the state a custody question is most
    likely to be asked from.

    THE SEED IS NEVER SEEN HERE. derived_payout_account() returns a public classic
    address or a refusal; this function never holds the value, so it cannot print
    it, log it, or put it in a pasted report.
    """
    desk, refusal = derived_payout_account()
    account_verdict = xrp_desk_account_verdict(Config.XRP_DEPOSIT_ACCOUNT, desk, refusal)
    lines = [("XRP desk account", account_verdict.state,
              f"{account_verdict.why}  [{SIGNING_SEED_ENV_VAR} is read only inside "
              f"chains/xrp_payout_seed.py and is never printed]")]
    if row is None or str(row["to_asset"]) != "XRP":
        lines.append(("XRP payout left", NOT_ESTABLISHED, (
            "XRP: no --swap with an XRP payout leg was given, so whether a payout actually LEFT the "
            "desk's account was not asked. That is the custody question on this chain -- a payout to "
            "the desk's own account is validated, pays a fee, and moves nothing"
        )))
        return lines
    # The desk account used for the comparison is the CONFIGURED one, not the
    # derived one: it is what services/swap_service.payout_source_account() reads
    # and therefore what the payout was actually debited from. Using the derived
    # account here would answer about an account the payout may not have used,
    # which is the same wrong-number-under-a-right-label failure the register in
    # tests/test_daemon_conf.py is about.
    paid = xrp_payout_destination_verdict(Config.XRP_DEPOSIT_ACCOUNT, str(row["id"]), str(row["payout_address"]))
    lines.append(("XRP payout left", paid.state, paid.why))
    return lines


def solana_lines() -> list[tuple[str, str, str]]:
    """The two Solana account variables, compared as strings. No cluster is contacted.

    WHY NO NETWORK READ. Whether either account exists on a cluster, and what it
    holds, is sol_payout_preview.py's question. A custody report that needed a
    reachable cluster would report nothing on an unreachable one, and the thing
    being asked here -- are these two variables two different accounts -- is
    answerable with no endpoint at all.
    """
    verdict = solana_account_verdict(Config.SOL_DEPOSIT_ACCOUNT, str(Config.RPC["SOL"]["hot_wallet"] or ""))
    return [("SOL accounts", verdict.state, verdict.why)]


def tally_lines(results: list[tuple[str, str, str]]) -> list[str]:
    """The count per state WITH its denominator, and `(none)` where a state is empty.

    Rule 3: a count without what it was counted out of has caused real errors. So
    every state in STATES gets a row even at zero -- a state silently absent is
    indistinguishable from a state nobody checked -- and the total is the number of
    lines this run actually printed rather than a literal, because the per-swap
    lines only exist with --swap.
    """
    lines = ["", "=" * 70, labeled("checks printed", f"{len(results)}  <- the denominator for every count below")]
    for state in STATES:
        hits = [name for name, got, _why in results if got == state]
        lines.append(labeled(state, f"{len(hits)} of {len(results)}  {', '.join(hits) or '(none)'}"))
    return lines


def verdict_lines(results: list[tuple[str, str, str]]) -> tuple[list[str], int]:
    """The closing verdict and the exit code. (lines, code).

    NON-ZERO FOR NOT ESTABLISHED, which is the half somebody will want to loosen.
    A tool whose exit code reads 0 when a daemon did not answer is a tool that
    reports custody separation it never measured, and that is the one failure mode
    this whole file is shaped to avoid.
    """
    bad = [(name, state, why) for name, state, why in results if state not in GOOD_STATES]
    if not bad:
        return ([
            "",
            f"SEPARATED: all {len(results)} checks answered, and every one answered the way a "
            f"custodial desk needs -- a wallet or account distinct from the operator's default, the "
            f"one shared desk account where that is the design, or the desk owning its own deposit "
            f"address.",
            "  This does NOT establish that the coins in those wallets were never the operator's own.",
            "  No chain says whose money a coin is; see the limits printed at the top of this run.",
        ], 0)
    # THE NAMES ONLY, NOT THE SENTENCES AGAIN. Each sentence is three to five
    # wrapped lines and every one of them was printed in full twenty lines above;
    # repeating them doubled the length of a pasted block without adding a word.
    # What a closing list is for is scanning WHICH checks did not answer, so that
    # is what it holds, and it says where the reasons are.
    #
    # THE HEADING NAMES NO STATES, and it used to name two ("NOT SEPARATED or NOT
    # ESTABLISHED"). Four states can land in this list and a heading that names
    # two of them is wrong the first time a third appears -- which on this host is
    # one MISCONFIGURED away. The states are in the column beside each name.
    lines = ["", f"{len(bad)} of {len(results)} checks did NOT answer the way a custodial desk needs, in order --",
             "  the full sentence for each is in the block above, under the same state:"]
    for name, state, _why in bad:
        lines.append(labeled(state, name))
    lines.append("")
    lines.append("  Each line names the variable to change. Nothing was written and no wallet was touched.")
    lines.append("  docs/hot_wallet_separation_runbook.md has the per-chain steps, and they are the")
    lines.append("  operator's to run: this tool creates no wallet, funds nothing and edits no .env.")
    return lines, 1


def run(args) -> int:
    row, row_note = swap_row(args)
    print("\n".join(header_lines(args, row_note)), flush=True)

    # BUILT ONCE AND PASSED DOWN, for the reason swap_readiness.main() records: a
    # second build_adapters() call is a second chance to disagree about what is
    # configured, and two panels of one report disagreeing is worse than either
    # answer alone. Wrapped, because build_adapters() reads config for every chain
    # and a raise here would kill the report before its first verdict.
    try:
        adapters = build_adapters(Config.RPC)
    except Exception as error:  # noqa: BLE001 -- checked: this is a reporting tool and the alternative is a traceback instead of a report. Every chain line below then reports NOT ESTABLISHED for lack of an adapter, which is the honest rendering, and the exit code is non-zero.
        print(labeled("adapters", f"BUILD CRASHED -- {type(error).__name__}: {error}  <- a bug in "
                                  f"chains/registry.py or in config, not in any one chain"), flush=True)
        adapters = {}
    else:
        built = ", ".join(sorted(adapters)) or "(none) -- NOTHING is reachable, so every line below is NOT ESTABLISHED"
        print(labeled("adapters", f"{built}  <- the chains this process can reach at all"), flush=True)
    print(flush=True)

    results: list[tuple[str, str, str]] = []
    # A COUNTER AND AN ELAPSED FIGURE PER CHAIN (rule 14). Each script chain is up
    # to three JSON-RPC round trips against a daemon that may be slow or wedged,
    # and a blinking cursor is what makes an operator Ctrl-C a healthy read.
    for index, asset in enumerate(SCRIPT_CHAINS, start=1):
        print(f"  asking {index}/{len(SCRIPT_CHAINS)} {asset} ...", flush=True)
        results.extend(script_chain_lines(asset, adapters, row))
    # AFTER the loop and not inside it: this line reads the DESK's recorded answer
    # for the GRC deposit address out of `results`, so the chain lines have to exist
    # before it runs. It is announced separately because it is a SECOND endpoint --
    # an operator watching the counter above would otherwise see one more round trip
    # than the "3/3" they were promised (rule 14).
    print(f"  asking the operator's own {CROSS_DAEMON_CHAIN} daemon (second endpoint) ...", flush=True)
    results.extend(cross_daemon_lines(row, results))
    results.extend(xrp_lines(row))
    results.extend(solana_lines())

    print(flush=True)
    for name, state, why in results:
        print("\n".join(wrapped(state, f"{name}: {why}")), flush=True)
    print("\n".join(tally_lines(results)), flush=True)
    lines, code = verdict_lines(results)
    print("\n".join(lines), flush=True)
    print(labeled("this report", root_tool_command("wallet_custody.py", *(["--swap", args.swap] if args.swap else []))),
          flush=True)
    # THIS SENTENCE USED TO SAY "two RPC reads per chain", AND THAT STOPPED BEING
    # TRUE when the cross-daemon GRC check landed on 2026-10-03: with --swap there
    # is a third read on the deposit chain (validateaddress) and a FOURTH against a
    # SECOND daemon, the operator's own. A closing line that undercounts the reads
    # is the wrong-comment bug (rule 16) on the one line whose job is to tell an
    # operator what this is about to do to their host.
    print(CONTINUATION + "<- safe against a live cycle: at most four RPC READS (getwalletinfo and "
                         "listwallets per chain, plus validateaddress on the deposit chain and one "
                         "against the operator's own GRC daemon) and no write anywhere", flush=True)
    return code


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return run(args)
    except Refused as refusal:
        print(f"\nREFUSED: {refusal}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
