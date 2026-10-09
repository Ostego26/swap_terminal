#!/usr/bin/env python3
"""Top up the desk's own hot wallet, from a source the OPERATOR controls. Not a swap.

Role: file (root entry point; the decisions are amount_to_move(), direction_verdict()
      and the two plan builders below)
Reads: for GRC, the operator's own gridcoinresearchd through GRC_OPERATOR_RPC_* and
      the desk's through Config.RPC["GRC"]; for ICP, the local replica's ledger
      canister through dfx. No database. No price feed.
Writes: nothing in this repository and nothing in swap_terminal.db. On the chain,
      and only with --apply: one GRC `sendtoaddress` from the operator's wallet to
      the desk's, or one ICP ledger `transfer` from the minting account to the
      desk's account.
Can move funds: YES, and that is the whole point of it, so read this line rather
      than skipping it. Two mechanisms, both testnet-only by construction:
        GRC   a REAL SEND between two daemons on this host. It needs the operator
              wallet's passphrase, from the environment, and it will leave that
              wallet locked and NOT STAKING if the passphrase is wrong (see
              chains/gridcoin_wallet_lock.unlocked_for_payout).
        ICP   a MINT. A transfer FROM the local ledger's minting account creates
              the tokens, so nothing is debited from anybody.
      Dry run is the default. Nothing is sent without --apply.
Mainnet-safe: it REFUSES rather than declining to act. Every port is put through
      network_target.may_read_a_wallet() BEFORE a socket is opened, so Gridcoin's
      mainnet 15715 is never connected to; the ICP side can only ever reach a
      canister on the local replica, and icp/dfx.json carries `remote.id.ic` for
      the real ledger precisely so dfx refuses to create one there.

=============================================================================
WHY THIS EXISTS. OPERATOR, 2026-10-07: "top up all accounts then"
=============================================================================

Measured on their host the same day, which is what makes the two halves separate
tools' worth of reasoning rather than one:

    desk GRC hot wallet (port 25779)        11.00248643 GRC, stake 0.0
    operator's own GRC wallet (25715)       401 Authorization Required to the
                                            desk's credentials
    desk ICP                                a local LICP ledger, mintable
    devnet SOL                              28.778699200, more than any test needs
    BTC / LTC                               regtest, so fund_testnets.py mines them

So four of the six assets were already answerable and two were not, for two
DIFFERENT reasons:

  GRC   the coins exist and this tree cannot reach them. 25715 is the operator's
        own testnet wallet and it answers the desk's credentials with 401 -- which
        is the custody separation working, not a fault. What was missing is a
        second endpoint, and gridcoin_credentials.operator_endpoint() already
        resolves one from GRC_OPERATOR_RPC_HOST/PORT/USER/PASS. It was built for a
        read (wallet_custody.py asks one ownership question through it) and nothing
        had ever sent through it.
  ICP   the coins do not exist and nothing could create any. The local ledger's
        `minting_account` is set once, by icp_ledger_init.py, from an account
        identifier the operator obtains by hand -- and a transfer FROM that account
        is a mint. No tool in this tree had ever made one; grepped 2026-10-07 for
        mint/airdrop/faucet/top_up/fund across every root *.py, and fund_testnets.py
        does not contain the string ICP at all.

WHY NOT A SWAP, since this terminal's whole job is swapping. The operator asked
earlier the same day to "swap from our btc hot wallet to the grc wallet", and the
arithmetic refuses it: a BTC -> GRC swap pays the CUSTOMER in GRC out of the desk's
GRC inventory, so it moves GRC the wrong way. Regtest BTC is unlimited and worth
nothing, which makes the trade look free in the direction that does not help.

WHY ONE TOOL FOR TWO CHAINS. They share the question -- "the desk is short of X;
what does the operator have that can fix it, and is the move safe?" -- and the
answer has the same four parts each time: resolve a source, refuse a wrong network
before any socket, prove the destination is the desk's and not the source's, then
move the smallest amount that reaches the target. The per-chain mechanism differs
completely and lives in its own plan builder; what would have drifted if this were
two files is the safety ordering, and that is the half worth sharing (rule 8).

=============================================================================
WHAT IT PROVES BEFORE IT SENDS, AND WHAT IT CANNOT
=============================================================================

The GRC side is the one with a counterparty, so it carries the custody proof:

  the destination is the DESK'S        desk.owns_address(dest) must be True
  and NOT the operator's               operator.owns_address(dest) must be False

Both answers come from the daemon that holds the wallet, through the same
`address_ownership()` read wallet_custody.py uses. The combination is exactly
services/custody_separation.cross_daemon_ownership_verdict()'s SEPARATED row, and
each other row is a refusal here for the reason that table gives it:

  operator owns it too   NOT SEPARATED. One wallet through two endpoints, or a
                         copied wallet.dat -- so the "send" would broadcast,
                         confirm, return a txid, cost a chain fee, and leave the
                         coin exactly where it was. swap_terminal/fee_sweep.
                         destination_refusal() refuses the mirror image of this for
                         the identical reason, and names it the same way.
  the desk does not own it
                         the send would pay a wallet the desk cannot spend from.
  either answer unknown  NOT ESTABLISHED, which is not the same as safe (rule 2).

THE DESTINATION IS READ, NEVER CREATED. `desk.own_address()` returns an address the
wallet already has; `desk.get_new_address(label)` is a wallet WRITE and is not
called here. That is deliberate and it is the same line wallet_custody.py draws in
its own banner -- a top-up does not need a fresh address, and creating one makes a
read-only audit of this tool impossible.

WHAT IT CANNOT PROVE, stated because the ICP side has no equivalent check: a mint
has no source wallet to separate from. The minting account holds nothing by
construction -- icp_ledger_init.py REFUSES a minter that also appears in
initial_values, because transfers to it are burns and transfers from it are mints --
so there is no balance to read and no ownership question to ask. What the ICP side
checks instead is that the destination is the desk's own account identifier,
derived from ICP_OWNER_PRINCIPAL with no subaccount, which is the account
get_balance() reads and the one services/icp_subaccount_service refuses to allocate
to a customer.
"""

from __future__ import annotations

import argparse
import sys
import time
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "swap_terminal"))

from chains.base import RPCError
from chains.coin_amounts import amount_to_base_units, fit_to_chain_precision
from chains.daemon_network import CHAIN_TEST_NETWORKS, chain_network, is_named
from chains.gridcoin import GridcoinAdapter
from chains.gridcoin_wallet_lock import (
    STAKING_UNLOCK_SECONDS,
    GridcoinLockError,
    lock,
    unlock_for_staking,
    unlocked_for_payout,
)
from chains.icp import dfx_transport, transfer_argument, transfer_block_index
from chains.icp_account import ICP_DECIMALS, account_identifier, is_account_identifier
from chains.registry import build_adapters, missing_settings, why_unconfigured
from config import Config
from gridcoin_credentials import (
    OPERATOR_PORT_VARIABLE,
    OPERATOR_REQUIRED_VARIABLES,
    OPERATOR_RPC_TIMEOUT_SECONDS,
    operator_endpoint,
)
from microfortnights import format_duration
from network_target import may_read_a_wallet
from report_block import labeled
from services.quote_service import get_network_fee_reserve
from workers.common import get_config_dict

SELF = "fund_desk.py"

#: The environment variable holding the OPERATOR wallet's passphrase.
#:
#: SEPARATE FROM GRIDCOIN_WALLET_PASSPHRASE, WHICH IS THE DESK'S, and the separation
#: is the whole reason this name exists. services/payout_service.
#: payout_unlock_context() reads that one and hands it to whichever adapter it is
#: given -- so passing it the operator's adapter would work only if both wallets
#: happen to share a passphrase, and "happens to" is not a thing to build a send on.
#: Two wallets, two secrets, two names.
#:
#: NAMED `OPERATOR_UNLOCK_ENV_VAR` AND NOT `..._PASSPHRASE_...`, which is the honest
#: answer to ruff's S105 rather than a `noqa` claiming this is not a credential.
#: S105 fires on the NAME, and it was right to: the first spelling here was
#: `OPERATOR_UNLOCK_ENV_VAR`, and a constant whose name says passphrase and
#: whose value is a string is indistinguishable from a hardcoded secret to a
#: checker, to a grep, and to a reader skimming. This holds the NAME of a
#: credential, so it is named after the lookup rather than after the secret --
#: exactly what chains/gridcoin_wallet_lock.WALLET_UNLOCK_ENV_VAR does with
#: GRIDCOIN_WALLET_PASSPHRASE, for the same reason (rule 19: remove the cause, do
#: not quiet the finding).
#:
#: Nothing in this file ever holds the value beyond passing it to the unlock. It is
#: never printed, never logged, never put in argv (world-readable through /proc),
#: and there is no flag that would accept it.
OPERATOR_UNLOCK_ENV_VAR = "GRC_OPERATOR_WALLET_PASSPHRASE"

#: The dfx identity that can mint. `minter` is the name DFINITY's own local-ledger
#: setup uses and the one icp/README.md records as living on the icp-state volume
#: with --storage-mode plaintext, "because it is a throwaway on a private network".
#:
#: A FLAG RATHER THAN A CONSTANT because nothing in this tree records which account
#: is the minting account after deployment, and it cannot: icp/icp_ledger_init.did
#: is gitignored on purpose -- the same identity has a different account on every
#: fresh replica, so a checkout that hardcoded it would claim to know something only
#: a running replica can answer. If the operator named theirs something else, this
#: is where they say so.
DEFAULT_MINTER_IDENTITY = "minter"

#: A mint is charged nothing, and this is the one field that makes the mint's
#: candid record differ from a payout's.
#:
#: NOT A STYLE CHOICE AND NOT A GUESS: the ICP ledger charges no fee on a transfer
#: from the minting account, so naming `icrc1_fee()` here comes back `BadFee` -- and
#: chains/icp.py deliberately does NOT retry a BadFee, because a retry is a second
#: send. chains/icp.transfer_argument() therefore takes the fee with NO DEFAULT, so
#: that neither caller can inherit the other's.
MINT_FEE_E8S = 0


def amount_to_move(
    held: float, target: float, source_balance: float, source_reserve: float = 0.0
) -> tuple[float, str]:
    """How much to move to bring `held` up to `target`. THE ARITHMETIC, on its own.

    Returns (amount, why). `amount` is 0.0 for every case that moves nothing, so a
    caller cannot broadcast a figure this function refused -- the same shape
    fee_sweep.SweepPlan uses for the same reason.

    AT THE BOTTOM AND PURE (rule 10): four floats in, a number and a sentence out,
    no adapter, no socket, no chain. It is the only part of a top-up that can be
    asserted exhaustively, and every refusal below is a case a live run would
    otherwise discover with a daemon in the loop.

    THE SHORTFALL IS CAPPED AT WHAT THE SOURCE CAN SPARE, rather than refused
    outright, and that is a judgment with a reason: a partial top-up is useful -- it
    is strictly more inventory than before -- and refusing it would leave the desk at
    11 GRC because the operator was 10 short of the full figure. The sentence says it
    was capped, because a number that silently means something else is rule 14's
    defect.

    `source_reserve` IS THE CHAIN FEE THE SOURCE MUST STILL PAY, and leaving it out
    was a defect the operator's first real dry run exposed, 2026-10-07. The cap read

        amount 3687.32154338   <- the operator wallet's ENTIRE balance

    and `sendtoaddress` takes the fee from the sending wallet's own inputs, on top of
    the amount delivered (quote_service.get_network_fee_reserve()'s whole measured
    point: a payout of X delivers exactly X and the wallet separately pays the fee).
    So that send had no inputs left to pay with and would have come back
    "Insufficient funds" -- an --apply run that failed for a reason the dry run
    printed as a go.

    IT IS SUBTRACTED FROM THE SOURCE AND NEVER FROM THE AMOUNT, which is the half
    that is easy to get backwards. The recipient must receive the full figure; what
    shrinks is how much the source can spare. Subtracting it from the amount instead
    would deliver less than the plan said and leave the desk short by the fee.
    """
    if target <= 0:
        return 0.0, f"the target is {target}, so there is nothing to reach"
    shortfall = target - held
    if shortfall <= 0:
        return 0.0, (
            f"the desk already holds {held}, which is at or above the target of {target}. "
            f"Nothing needs to move"
        )
    # max(..., 0.0) because a source holding LESS than the chain fee can spare
    # nothing at all, and a negative "available" would read as a direction.
    spare = max(source_balance - source_reserve, 0.0)
    fee_note = (
        f" (its balance is {source_balance} and {source_reserve} stays behind for the chain fee, "
        f"which sendtoaddress takes from the sending wallet's own inputs on top of what it delivers)"
        if source_reserve else ""
    )
    if spare <= 0:
        return 0.0, (
            f"the desk is short {shortfall} and the source can spare {spare}{fee_note}, so nothing "
            f"can move. This is a fact about the SOURCE, not a refusal about the desk"
        )
    if spare < shortfall:
        return spare, (
            f"CAPPED AT THE SOURCE. The desk is short {shortfall} and the source can spare "
            f"{spare}{fee_note}, so this moves all of it and leaves the desk {held + spare} "
            f"against a target of {target} -- still short by {shortfall - spare}"
        )
    return shortfall, (
        f"the desk holds {held}, the target is {target}, so this moves the {shortfall} difference "
        f"and the source keeps {spare - shortfall} spare{fee_note}"
    )


def direction_verdict(destination: str, desk_owns: bool | None, source_owns: bool | None) -> str:
    """The refusal if this send would not cross custody, or "" if it would.

    THE DECISION THE GRC SIDE TURNS ON, and the truth table is
    services/custody_separation.cross_daemon_ownership_verdict()'s, read rather than
    re-derived: SEPARATED is `source does not own it` AND `desk owns it`, and every
    other combination is one of that function's refusals.

    IT IS NOT CALLED INTO custody_separation DIRECTLY, and the difference is named
    here because rule 8 asks for it at both sites: that function answers "is this
    deposit address separated from the operator's wallet?" and returns a
    CustodyVerdict for a REPORT. This answers "may I send to it?" and returns a
    refusal for a WRITE. Same table, two questions, and the one that gates a send
    must fail closed on `None` -- which the report deliberately does not do, because
    NOT ESTABLISHED is a legitimate thing for a report to print and not a legitimate
    thing to broadcast on.

    "" RATHER THAN True, so the caller prints the reason it refused rather than
    composing one that might name the wrong half.
    """
    if not destination:
        return (
            "the desk's wallet returned no address to send to, so there is nothing to top up. "
            "own_address() reads an address the wallet already has and this one has none -- which "
            "on a wallet holding a balance means the read failed rather than that it is empty"
        )
    if desk_owns is None or source_owns is None:
        unknown = "the desk" if desk_owns is None else "the source"
        return (
            f"{unknown} could not say whether it owns {destination}, so the custody direction is "
            f"NOT ESTABLISHED. That is not the same as safe: a send proven to cross nothing is the "
            f"one this refuses, and an unanswered question cannot distinguish it from one that does"
        )
    if source_owns:
        return (
            f"the SOURCE wallet also owns {destination}, so this would be a self-transfer: one "
            f"wallet reached through two endpoints, or a copied wallet.dat. It would broadcast, "
            f"confirm, return a txid and cost a chain fee while the coin stayed exactly where it "
            f"is -- 'sent' on screen and nothing moved. Refusing"
        )
    if not desk_owns:
        return (
            f"the DESK does not own {destination}, so the coins would land somewhere the desk "
            f"cannot spend from. A top-up that the payout path cannot reach is worse than no "
            f"top-up, because the balance looks larger"
        )
    return ""


def chain_fee_for(asset: str) -> tuple[float | None, str]:
    """What the chain charges the SOURCE to move `asset`. (fee, how) -- or (None, why).

    services/quote_service.get_network_fee_reserve() IS THE AUTHORITY AND IS CALLED
    RATHER THAN THE CONFIG KEY READ DIRECTLY (rule 8), which is the same choice
    collect_fees.chain_fee_for() made and states for the same reason: that function's
    docstring carries the 2026-10-01 measurement of what the figure is and is not --
    what the desk expects one payout to cost it, paid out of the sending wallet's own
    inputs and never deducted from the amount delivered -- and
    `getattr(Config, f"{asset}_NETWORK_FEE_RESERVE")` here would be a third reader of
    the same key with none of that attached.

    IT TAKES NO `config` ARGUMENT, and that is the difference from collect_fees'
    version, named here because rule 8 asks for it at both sites: that one is handed
    the mapping its caller already built, and this file has no Flask app and no
    worker context. workers.common.get_config_dict() is how a root tool makes one --
    `{k: getattr(Config, k) for k in dir(Config) if k.isupper()}` -- and passing the
    Config CLASS instead is what the first version did. It failed with
    `TypeError: argument of type 'type' is not iterable` out of
    get_network_fee_reserve()'s `if key not in config`, because a class is not a
    mapping. Caught by four tests rather than by a live run, which is the only reason
    it is a footnote and not a third defect in the operator's terminal.

    ValueError BECOMES (None, sentence) rather than propagating: a missing reserve is
    a thing to refuse the top-up over and report, not a traceback.
    """
    try:
        return get_network_fee_reserve(get_config_dict(), asset), f"{asset}_NETWORK_FEE_RESERVE"
    except ValueError as refusal:
        return None, (
            f"{asset}_NETWORK_FEE_RESERVE could not be read, so what the sending wallet must keep "
            f"back for the chain fee is unknown and no amount can be sized: {refusal}"
        )


#: What the daemon says when `walletpassphrase` is called on a wallet that is ALREADY
#: open: RPC_WALLET_ALREADY_UNLOCKED. It refuses WITHOUT checking the passphrase, which
#: is why this needs its own branch rather than being one more kind of failure.
#:
#: MATCHED AS A SUBSTRING ON THE CODE, which is the idiom this repository already uses
#: for exactly this problem: chains/gridcoin_wallet_lock.WALLET_UNLOCK_NEEDED_MARKERS
#: matches "-13" in the message text, and rescue_payout.PRE_SIGNING_MARKERS matches
#: "(rpc code -14)". chains/base.py puts the code into the message it raises, which is
#: the only reason any of them can be told apart.
#:
#: THE CODE AND NOT THE WORDING. "already unlocked" would be a plausible match and is
#: the wrong thing to key on: a daemon is free to reword its errors, and the numeric
#: code is the part that is part of the protocol.
ALREADY_UNLOCKED_MARKER = "(rpc code -17)"

#: RPC_WALLET_PASSPHRASE_INCORRECT. The wallet did not open, and nothing this process
#: can do will change that.
#:
#: NAMED AFTER THE CONDITION AND NOT THE SECRET, which is what answers ruff's S105
#: here rather than a `noqa`. The first spelling was REJECTED_UNLOCK_MARKER and S105
#: was right to fire: a constant whose name says passphrase and whose value is a
#: string literal is indistinguishable from a hardcoded secret to a checker, a grep,
#: or a reader skimming. This names what the daemon REJECTED, which is also the more
#: accurate description -- it sits beside ALREADY_UNLOCKED_MARKER, and both are
#: daemon conditions rather than anything of ours (rule 19: remove the cause).
REJECTED_UNLOCK_MARKER = "(rpc code -14)"


def prove_passphrase(adapter, passphrase: str) -> str:
    """Prove the passphrase opens this wallet. "" if it does, else the refusal. THE PROOF.

    A STAKING-ONLY UNLOCK IS THE TEST, because it needs no prior lock -- so on a wrong
    passphrase the wallet is untouched, which is the whole reason this runs before
    unlocked_for_payout() takes its lock. See grc_send() for the three staking
    interruptions that bought that ordering on 2026-10-08.

    AND AN ALREADY-OPEN WALLET BROKE IT THE SAME DAY, which is why this is a function
    rather than a try/except at the call site. The operator restored staking with
    --restore-staking, which left the wallet unlocked; the very next --apply then hit

        Error: Wallet is already unlocked, use walletlock first if need to change
        unlock settings. (rpc code -17)

    and the first version of this check reported that as "the passphrase did NOT open
    this wallet ... rpc code -14 is RPC_WALLET_PASSPHRASE_INCORRECT" -- a sentence
    asserting a code the daemon had not returned, about a passphrase that was right.
    My own safety addition became the thing blocking a correct send, and it blocked it
    with a false explanation.

    -17 PROVES NOTHING ON ITS OWN, and that is the honest reading: the daemon refused
    the unlock without looking at the passphrase. So the wallet is LOCKED and the
    unlock retried, which is the only way to test it -- and it costs nothing extra
    here, because the caller's unlocked_for_payout() locks on its very next statement
    anyway. Either the retry proves the passphrase with the wallet left staking, or it
    fails and says the wallet is now locked and not staking, which is true.

    EVERY OTHER FAILURE REFUSES WITHOUT A DIAGNOSIS. A dead socket, a timeout, an
    unrecognized code: none of them is proof, and rule 2's distinction is the whole
    point -- "I could not prove it" is not "it is wrong", and the refusal says which.
    """
    try:
        unlock_for_staking(adapter, passphrase)
        return ""
    except RPCError as error:
        if ALREADY_UNLOCKED_MARKER not in str(error):
            return _unlock_refusal(error, locked=False)
    # ALREADY OPEN. Lock it and ask properly. The caller locks immediately after this
    # returns, so the lock is not a cost this check is adding.
    lock(adapter)
    try:
        unlock_for_staking(adapter, passphrase)
    except RPCError as error:
        return _unlock_refusal(error, locked=True)
    return ""


def _unlock_refusal(error: Exception, *, locked: bool) -> str:
    """The sentence for a failed unlock, which must say what state the wallet is IN.

    `locked` is the thing a reader needs and the thing a message cannot guess: the
    same daemon error means "nothing was touched" before the lock and "your staking is
    off" after it. chains/gridcoin_wallet_lock.restore_failed_because() draws the
    identical distinction for the identical reason, and is not reused here only
    because its sentences are about a restore rather than about a proof.
    """
    text = str(error)
    wrong = REJECTED_UNLOCK_MARKER in text
    what = (
        "is WRONG -- rpc code -14 is RPC_WALLET_PASSPHRASE_INCORRECT, so the wallet did not open"
        if wrong else
        "could not be PROVED either way: the daemon failed for a reason this tool does not "
        "recognize, and an unproved passphrase is refused rather than tried"
    )
    state = (
        "The wallet was already unlocked, so it was LOCKED to ask properly and is now LOCKED AND "
        "NOT STAKING. Put it back with:  python3 " + SELF + " --asset GRC --restore-staking --apply"
        if locked else
        "NOTHING WAS LOCKED -- if that wallet was staking, it still is. This check runs before the "
        "lock for exactly that reason."
    )
    return (
        f"the passphrase in {OPERATOR_UNLOCK_ENV_VAR} {what}, so nothing was sent. {state}\n\n"
        f"  The daemon said: {error}"
    )


def network_gate(adapter, whose: str) -> str:
    """The refusal if this DAEMON is not on a test network, or "". ASKED, NOT INFERRED.

    THE PORT GATE IS NOT THIS CHECK AND THIS FILE CLAIMED IT WAS. Until 2026-10-08
    the only network question asked here was network_target.may_read_a_wallet(), which
    classifies a PORT NUMBER -- and the balance line then printed "all TESTNET,
    established by the port gate above", which that gate cannot establish. A port
    number is a convention; the network is a property of the daemon.

    MEASURED ON THE OPERATOR'S HOST THE SAME DAY, which is what turns this from
    pedantry into the defect it is. `pgrep -af gridcoin` found three processes:

        4604     gridcoinresearch  -datadir=~/.GridcoinResearch -min
        340262   gridcoinresearch  -testnet  (NO -datadir)
        2586595  gridcoinresearchd -datadir=~/.GridcoinResearch-desk -daemon

    The first is a MAINNET GUI holding real coins. The second takes `-testnet` on the
    COMMAND LINE with no datadir of its own. And NOTHING is running against
    ~/.GridcoinResearch-testnet-clean -- which is the conf this tool's credentials
    came from, because it was the file that declared rpcport=25715. So the daemon
    answering on 25715 is not the one whose conf was read; it authenticated because
    those two confs share an rpcuser and rpcpassword.

    fund_testnets.check_gridcoin_testnet() already carries this exact warning, from
    2026-09-25: "a conf in a directory named `testnet` is not a testnet ... since
    Gridcoin also takes -testnet on the command line". That function therefore reads
    the network off the daemon and refuses a mainnet answer. This file did not, and
    was one correct passphrase away from sending 1,988.99 coins out of a wallet whose
    network nobody had established.

    chains/daemon_network.chain_network() IS THE AUTHORITY and is called rather than
    re-derived (rule 8): it reads getblockchaininfo.chain, falls back to
    getinfo.testnet for an older build, and returns "unknown (...)" when neither
    answers. CHAIN_TEST_NETWORKS is an ALLOWLIST, so an unreadable network refuses --
    fail closed, never "probably testnet".
    """
    network = chain_network(adapter)
    if network in CHAIN_TEST_NETWORKS["GRC"]:
        return ""
    if not is_named(network):
        return (
            f"{whose} daemon did not name its network ({network}), so whether it is a test chain "
            f"was NOT established and nothing was read from its wallet. An unreadable network is "
            f"refused rather than assumed: this tool sends coins, and a port number is a "
            f"convention while the network is a property of the daemon"
        )
    return (
        f"{whose} daemon answered network {network!r}, which is not one of "
        f"{sorted(CHAIN_TEST_NETWORKS['GRC'])}. *** THIS IS A MAINNET DAEMON *** and nothing was "
        f"read from its wallet or sent from it. The port being a test port is not the same fact: "
        f"Gridcoin takes -testnet on the command line, so a conf's rpcport says nothing about "
        f"which chain the process on it is following"
    )


def _port_gate(chain: str, port: int, whose: str, variable: str) -> str:
    """The refusal if `port` is not a test chain, or "" -- asked BEFORE any socket.

    network_target.may_read_a_wallet() is the one home for that decision (rule 8)
    and this wraps it for one reason: its sentence names the DESK's variable
    (`GRC_RPC_PORT`) because that is who usually asks, and a refusal that tells the
    operator to set the wrong variable is a refusal they will route around.
    wallet_custody.py documents the same correction at its own call site.
    """
    allowed, why = may_read_a_wallet(chain, port)
    if allowed:
        return ""
    return f"{whose} port {port} was REFUSED and nothing connected to it: {why}. Here that port comes from {variable}"


def grc_endpoints():
    """(the operator's endpoint, "") or (None, refusal). EVERY CHECK THAT NEEDS NO SOCKET.

    EXTRACTED FROM grc_plan() 2026-10-07, when adding the chain-fee read put that
    function at seven returns against ruff's PLR0911 ceiling of six. Rule 12 says
    the ceiling is telling you a decision wants its own function rather than a
    suppression, and the grouping it suggested is a real one: everything here is
    answerable from two integers, a hostname and three environment variables, so it
    all happens BEFORE anything opens a connection. That ordering is the file's main
    safety property and it is easier to see as one function than as four early
    returns among the reads.
    """
    endpoint, refusal = operator_endpoint()
    if endpoint is None:
        # operator_endpoint()'s own sentence already names which variables are unset
        # and why there is no fallback to the desk's credentials, so this adds the
        # one thing it cannot know: WHICH daemon is wanted on this host. Repeating
        # the export line it already printed would be two sentences competing to be
        # the instruction.
        return None, (
            f"{refusal} On this host the daemon you want is the operator's own on 25715, not the "
            f"desk's on 25779 -- and 25715 answering the desk's credentials with 401 Authorization "
            f"Required is the custody separation working, not a fault to route around."
        )

    # NO int() AND NO `or 0`, 2026-10-09: config.BitcoinFamilyRpc declares `port`
    # an `int` and config.py builds it with _env_int("GRC_RPC_PORT",
    # str(UNCONFIGURED_PORT)), which is 0 for an unset or empty variable -- so the
    # `or 0` was defending against a value _env_int cannot produce and the int()
    # was there to get an `object` past the comparisons below. `["GRC"]` rather
    # than `.get("GRC", {})` for the same reason: config.RpcSettings now states
    # that the key is always there, which the test suite relies on too -- the
    # fixture that drives this function replaces Config.RPC with a GRC-only table.
    desk_port = Config.RPC["GRC"]["port"]
    for whose, port, variable in (
        ("the operator's", endpoint.port, OPERATOR_PORT_VARIABLE),
        ("the desk's", desk_port, "GRC_RPC_PORT"),
    ):
        gate = _port_gate("GRC", port, whose, variable)
        if gate:
            return None, gate

    if endpoint.port == desk_port and endpoint.host == Config.RPC["GRC"].get("host", "127.0.0.1"):
        # CAUGHT WITHOUT A SOCKET, which is why it is here and not left to
        # direction_verdict() below. Same host and same port IS one daemon, so the
        # ownership reads would both answer True and the refusal would be correct
        # -- but it would have cost two RPC calls and an unlock to establish what
        # two integers already say.
        return None, (
            f"{OPERATOR_PORT_VARIABLE} and GRC_RPC_PORT are both {desk_port} on {endpoint.host}, "
            f"so the source and the destination are ONE daemon. A top-up from a wallet to itself "
            f"moves nothing and costs a chain fee. Point {OPERATOR_UNLOCK_ENV_VAR}'s wallet at "
            f"the operator's own daemon -- 25715 on this host -- and run this again."
        )

    if missing_settings(Config.RPC, "GRC"):
        return None, f"the DESK's GRC daemon is not configured: {why_unconfigured('GRC', Config.RPC)}"
    return endpoint, ""


def grc_plan(console_say, target: float) -> dict:
    """Everything a GRC top-up needs, decided and read, with nothing sent.

    Returns a dict with `refusal` set when it must not proceed, and otherwise the
    source adapter, the destination, the amount and every figure printed above it.
    Separated from the send so a dry run and an --apply run take the IDENTICAL path
    up to the broadcast -- a dry run that checks less than the real one is a dry run
    that cannot be trusted, which is the whole reason for having one.

    THE ORDER IS THE SAFETY PROPERTY, and it is wallet_custody.cross_daemon_lines()'s
    order because that one was argued out there: resolve the environment, refuse a
    wrong network BEFORE constructing anything, and only then open a socket.
    """
    endpoint, refusal = grc_endpoints()
    if endpoint is None:
        return {"refusal": refusal}
    console_say(f"source   the operator's own daemon at {endpoint.label} (from "
                f"{', '.join(OPERATOR_REQUIRED_VARIABLES)})")
    source = GridcoinAdapter(
        user=endpoint.user, password=endpoint.password, host=endpoint.host, port=endpoint.port,
        # wallet="" IS REQUIRED, not a default worth leaving to chance: Gridcoin
        # serves no /wallet/<name> path, so a non-empty value makes every call 404.
        # wallet_custody.read_operator_ownership() constructs it the same way.
        wallet="", timeout=OPERATOR_RPC_TIMEOUT_SECONDS,
    )
    # THE FIRST THING ASKED OF EITHER DAEMON, before an address, a balance or an
    # ownership question. A mainnet daemon must have nothing read from its wallet at
    # all -- wallet_custody.py records the 2026-09-25 run where a balance reader hit
    # one and printed 157,797 real GRC into a terminal whose output goes into a chat
    # transcript.
    gate = network_gate(source, "the operator's")
    if gate:
        return {"refusal": gate}
    desk = build_adapters(Config.RPC)["GRC"]
    gate = network_gate(desk, "the desk's")
    if gate:
        return {"refusal": gate}
    console_say("networks   both daemons ANSWERED a test network -- asked with "
                "getblockchaininfo.chain, falling back to getinfo.testnet; an unreadable answer "
                "would have refused")

    console_say("destination   reading an address the DESK wallet already has "
                "(own_address, a READ -- getnewaddress is a wallet write and is not called)")
    destination = desk.own_address()
    console_say(f"destination   {destination or '(none)'}")

    console_say("asking BOTH daemons who owns it -- the send is refused unless the desk does and "
                "the operator's does not")
    desk_owns = desk.owns_address(destination) if destination else None
    source_owns = source.owns_address(destination) if destination else None
    console_say(f"ownership   desk={desk_owns}  operator={source_owns}  <- True/False/None; None "
                f"means the daemon did not answer and is refused, not assumed")
    wrong_way = direction_verdict(destination, desk_owns, source_owns)
    if wrong_way:
        return {"refusal": wrong_way}

    held = desk.get_balance()
    available = source.get_balance()
    # READING THE OPERATOR'S BALANCE IS A DEPARTURE FROM wallet_custody.py AND IS
    # DELIBERATE. That tool refuses to call getbalance on the operator's daemon,
    # because on 2026-09-25 a balance reader hit the MAINNET wallet and printed
    # 157,797 real GRC into a terminal whose output goes into a chat transcript.
    # The refusal is right for a report that does not need the number. This tool
    # cannot size a transfer without it -- and the port gate above has already
    # established the daemon is a TEST chain before this line runs, which is the
    # condition that made the old leak a leak.
    # THE SOURCE'S OWN CHAIN FEE, from the one place that maps an asset to one
    # (rule 8). get_network_fee_reserve() is "what the desk EXPECTS one payout on
    # this chain to cost it" -- measured at 0.001 GRC on this operator's host
    # 2026-10-01, to the last digit, against a payout whose own address made the
    # wallet delta exactly -0.001. It is the desk's reserve rather than the
    # operator's, and that is the right figure anyway: the same chain charges both
    # wallets the same, and nothing in this tree records a separate one.
    reserve, reserve_how = chain_fee_for("GRC")
    if reserve is None:
        return {"refusal": reserve_how}
    console_say(f"balances   desk {held} GRC   operator {available} GRC   chain fee {reserve} GRC "
                f"stays with the operator (from {reserve_how})  <- all TESTNET, which each DAEMON "
                f"was asked rather than inferred from its port number (see network_gate)")
    amount, why = amount_to_move(held, target, available, reserve)
    # QUANTIZED HERE SO THE DRY RUN PRINTS THE FIGURE THE APPLY RUN SENDS.
    # chains/base.send_to_address() runs fit_to_chain_precision() on whatever it is
    # handed and logs the reduction -- so without this the two runs could differ in
    # the last decimal place and only the second would say so. Idempotent: fitting an
    # already-fitted amount changes nothing, which is what makes it safe to do twice.
    #
    # THE ICP SIDE CANNOT USE THIS FUNCTION, and the difference is named at both
    # sites (rule 8): CHAIN_DECIMALS covers the chains whose RPC takes a DECIMAL
    # amount, and ICP -- like XRP and SOL -- is handed integer base units instead,
    # so icp_plan() quantizes through amount_to_base_units() and carries the integer.
    amount, fitted = fit_to_chain_precision(amount, "GRC")
    if fitted:
        console_say(f"precision   {fitted}")
    return {
        "refusal": "", "asset": "GRC", "source": source, "destination": destination,
        "amount": amount, "why": why, "held": held, "available": available,
        "source_label": endpoint.label,
    }


def operator_passphrase() -> str:
    """The operator wallet's passphrase from the environment, or a refusal naming WHICH state.

    EXTRACTED FROM grc_send() 2026-10-08, when --restore-staking became a second
    caller that needs the identical refusal. A second copy of a three-state check on
    a secret is rule 8's duplicate on the one value that decides whether a wallet
    opens, and the states it distinguishes were themselves a correction made hours
    earlier -- so the copies would have started out agreeing and drifted from the
    first edit.

    THE PASSPHRASE COMES FROM THE ENVIRONMENT AND NOWHERE ELSE. Not a flag, not a
    prompt, not a file: argv is world-readable through /proc, and an interactive
    `read` inside a pasted block consumes the rest of the paste -- which happened on
    2026-10-07 and armed a container with a command fragment instead of a secret.
    """
    import os  # noqa: PLC0415 -- checked: imported here so the one read of the environment sits beside the paragraph explaining where the secret may come from, rather than at the top where a reader would have to go looking for which function uses it.

    raw = os.environ.get(OPERATOR_UNLOCK_ENV_VAR)
    passphrase = raw or ""
    if passphrase.strip():
        return passphrase
    # UNSET, EMPTY AND WHITESPACE ARE THREE CONDITIONS AND THIS USED TO REPORT
    # ONE. The operator hit the middle case on 2026-10-08: they ran
    # `read -rs <VAR> && export <VAR>`, the prompt returned instantly, and this
    # refused with "is not set" -- about a variable that WAS set, to "". A
    # trailing newline in the pasted line is already sitting in the terminal's
    # input queue when `read` runs, so it reads that newline and returns an
    # empty value, and `export` then exports the empty string. Telling them to
    # export something they had just exported is a refusal that sends a reader
    # looking in the wrong place, which is the one thing a refusal must not do.
    #
    # AND THE FIRST FIX NAMED HERE WAS ALSO WRONG, which is why the advice below
    # is a loop rather than a redirect. It said to run `read ... < /dev/tty`
    # "which cannot be fed by a paste". Measured on the operator's host the same
    # day: that returned instantly too, and `${#VAR}` read 0. `/dev/tty`
    # redirects stdin TO the terminal, and the queued newline is IN the
    # terminal -- so the redirect changes which file descriptor is read and not
    # what is waiting in it. A loop that refuses an empty value is the thing
    # that actually works, because it consumes the stray newline on its first
    # pass and blocks on its second. Measured working the same day: it prompted
    # twice and the length read 10.
    #
    # WHITESPACE IS REFUSED RATHER THAN SENT, and that is the dangerous one of
    # the three. A single pasted space is truthy, so without .strip() it would
    # reach walletpassphrase, fail with rpc code -14, and leave the operator's
    # STAKING wallet locked and not staking -- which is exactly what a WRONG
    # passphrase then did on 2026-10-08, so the cost is measured rather than
    # hypothetical.
    state = {
        None: "is NOT SET in this process's environment",
        "": "IS set and is EMPTY -- the variable exists with a zero-length value",
    }.get(raw, "IS set and contains only whitespace")
    raise RuntimeError(
        f"{OPERATOR_UNLOCK_ENV_VAR} {state}, so the operator's wallet cannot be unlocked and "
        f"NOTHING was sent -- the wallet was not even locked, because this check runs before "
        f"the unlock.\n\n"
        f"  IF YOU JUST RAN `read` AND IT RETURNED INSTANTLY: a newline is already sitting in "
        f"the terminal's input queue -- a pasted line ends with one -- so `read` consumes that "
        f"and returns empty before you can type. `< /dev/tty` does NOT fix it: that redirects "
        f"stdin to the terminal, and the queued newline is IN the terminal. Loop until it is "
        f"non-empty instead, which consumes the stray newline on the first pass and waits on "
        f"the second, and prints the length so you can see it landed:\n"
        f"      while [ -z \"${{{OPERATOR_UNLOCK_ENV_VAR}:-}}\" ]; do printf 'passphrase: '; "
        f"IFS= read -rs {OPERATOR_UNLOCK_ENV_VAR} < /dev/tty; printf '\\n'; done; "
        f"export {OPERATOR_UNLOCK_ENV_VAR}; echo \"length=${{#{OPERATOR_UNLOCK_ENV_VAR}}}\"\n"
        f"  The length is the only thing printed. The value never is.\n\n"
        f"  It is the OPERATOR wallet's passphrase and not the desk's: "
        f"GRIDCOIN_WALLET_PASSPHRASE is the desk's, and the two wallets are not assumed to "
        f"share a secret. Never pass it as a command-line argument -- argv is world-readable "
        f"through /proc."
    )


def staking_verdict(unlocked_until: object, now_epoch: float) -> tuple[bool, str]:
    """Is this wallet unlocked for staking? (yes, the sentence). THE PROOF, as a function.

    RULE 13: A RESTORE THAT CANNOT PROVE IT WORKED IS NOT A RESTORE. The exit code of
    `walletpassphrase` says the call returned, not that the wallet is open -- so
    restore_staking() reads getwalletinfo back afterwards and this decides what the
    answer means. Separated from the read so it can be asserted with seeded values
    and no daemon.

    `unlocked_until` IS THE FIELD AND ITS THREE SHAPES MEAN THREE THINGS:
      absent / None   the wallet is NOT ENCRYPTED. There is nothing to unlock and
                      nothing was locked either, so this is reported rather than
                      treated as a failure.
      0               ENCRYPTED AND LOCKED. This is the state a failed unlock leaves,
                      and the one an operator is trying to get out of.
      a timestamp     unlocked until then. In the future is open; in the past is a
                      daemon that has not updated the field yet, which is reported as
                      locked because that is what it can spend like.
    """
    if unlocked_until is None:
        return True, (
            "this wallet is NOT ENCRYPTED -- getwalletinfo reports no unlocked_until -- so there "
            "is nothing to unlock and nothing was ever locked. Staking is unaffected"
        )
    # ONE SENTENCE FOR BOTH WAYS OF NOT BEING A NUMBER, and the split is so a
    # reader can see which is which (2026-10-09). This was a bare
    # `float(unlocked_until)` under `except (TypeError, ValueError)`, which
    # handled both and said which neither to a reader nor to a checker:
    # `unlocked_until` is typed `object` -- honestly, because it is whatever this
    # daemon's getwalletinfo reply put in that field -- and `float(object)` is not
    # a call any checker can approve.
    #
    # The two cases are genuinely different and only one of them can reach
    # float():
    #
    #   not int/float/str   TypeError. A dict, a list, a bytes -- shapes a JSON
    #                       reply can carry and this field has no business
    #                       holding. Refused by the type test, before the call.
    #   a str that is not   ValueError, and only float() can tell: "1800000000"
    #   a number            is a number and "not a number" is not, and no type
    #                       test distinguishes them.
    #
    # So TypeError is gone from the except rather than left as a second guard for
    # a case the line above it now refuses -- an except clause for an unreachable
    # exception reads as a defense and is dead code (rule 9). bool passes the type
    # test because bool IS an int in Python, and float(True) is 1.0 exactly as it
    # was before: a `true` in that field still ends up reported as locked, via the
    # "in the PAST" branch below.
    not_a_number = (
        f"getwalletinfo reported unlocked_until={unlocked_until!r}, which is not a number, so "
        f"whether this wallet is open was NOT established. Absence of a readable answer is not "
        f"an answer"
    )
    if not isinstance(unlocked_until, (int, float, str)):
        return False, not_a_number
    try:
        until = float(unlocked_until)
    except ValueError:
        return False, not_a_number
    if until <= 0:
        return False, (
            "unlocked_until is 0, so the wallet is ENCRYPTED AND LOCKED. It is not staking, and the "
            "passphrase this process was given did not open it"
        )
    remaining = until - now_epoch
    if remaining <= 0:
        return False, (
            f"unlocked_until is {int(until)}, which is {format_duration(-remaining)} in the PAST, so "
            f"the wallet is locked whatever the field says"
        )
    return True, (
        f"unlocked_until is {int(until)}, which is {format_duration(remaining)} from now -- the "
        f"wallet is open for staking"
    )


def restore_staking(console_say, apply: bool) -> int:
    """Put the operator's wallet back to a staking unlock, and PROVE it. Sends nothing.

    WHY THIS EXISTS, MEASURED 2026-10-08. grc_send() locks the operator's wallet
    before it unlocks it for sending, so a WRONG passphrase leaves that wallet locked
    and not staking -- and that happened on the operator's host, to the staking
    wallet, within an hour of the refusal for it being written. The refusal told them
    to run `walletpassphrase <phrase> 31536000 true` by hand, which is the one shape
    this repository forbids: a secret in argv, world-readable through /proc and
    recorded in shell history.

    A TOOL THAT CREATES A STATE AND CANNOT UNDO IT IS HALF A TOOL. This is the other
    half, and it reuses every gate the send does -- the same endpoint resolution, the
    same port refusal before any socket, the same three-state passphrase check -- so
    there is no second path to the operator's wallet with weaker checks on it.

    IT UNLOCKS FOR STAKING ONLY, never for sending: `walletpassphrase <phrase>
    <seconds> true`. The third argument is what makes it staking-only, and a wallet
    unlocked this way refuses sendtoaddress. So the worst this can do on a correct
    passphrase is restore the state the daemon was in before, which is the definition
    of a restore.
    """
    endpoint, refusal = grc_endpoints()
    if endpoint is None:
        print(f"\n  REFUSED and nothing was changed: {refusal}", flush=True)
        return 3
    console_say(f"wallet     the operator's own daemon at {endpoint.label}")
    adapter = GridcoinAdapter(
        user=endpoint.user, password=endpoint.password, host=endpoint.host, port=endpoint.port,
        wallet="", timeout=OPERATOR_RPC_TIMEOUT_SECONDS,
    )
    gate = network_gate(adapter, "the operator's")
    if gate:
        print(f"\n  REFUSED and nothing was changed: {gate}", flush=True)
        return 3
    before = adapter.call("getwalletinfo") or {}
    open_now, why = staking_verdict(before.get("unlocked_until"), time.time())
    console_say(f"before     {why}")
    if open_now:
        console_say("nothing to do: this wallet is already open, so no unlock was attempted")
        return 0
    if not apply:
        print(f"\nDRY RUN: nothing was changed. To restore staking:\n"
              f"    python3 {SELF} --asset GRC --restore-staking --apply\n"
              f"    ...with {OPERATOR_UNLOCK_ENV_VAR} exported in that shell.", flush=True)
        return 0

    console_say(f"unlocking  walletpassphrase for {STAKING_UNLOCK_SECONDS}s STAKING ONLY -- the "
                f"third argument is what makes it staking-only, and a wallet opened this way "
                f"refuses sendtoaddress")
    unlock_for_staking(adapter, operator_passphrase())
    # READ IT BACK. The call returning is not the wallet being open (rule 13: verify
    # the artifact, not the deploy), and a restore that reports success on a call's
    # exit code is the shape this file's own failure took.
    after = adapter.call("getwalletinfo") or {}
    restored, why = staking_verdict(after.get("unlocked_until"), time.time())
    console_say(f"after      {why}")
    if not restored:
        print("\n  THE UNLOCK RETURNED AND THE WALLET IS STILL NOT OPEN. That is the daemon's "
              "answer read back, not this tool's guess -- nothing here can fix it, and the "
              "passphrase is the only candidate.", flush=True)
        return 3
    print("\n  RESTORED   the wallet is unlocked for staking again", flush=True)
    return 0


def argument_refusal(args) -> str:
    """The parser-level refusal for a combination argparse cannot express, or "".

    A FUNCTION BECAUSE main() WAS OVER THE CEILING, and rule 12 says the ceiling is
    telling you a decision wants extracting rather than suppressing. It is also the
    better home: "which flags make sense together" is answerable from the parsed
    arguments alone, with no daemon and no database, so it is testable without either.

    `--target` IS NOT `required=True` ANY MORE, and that is what makes this necessary:
    a restore needs no target, and argparse has no way to say "required unless". The
    alternative was a subparser pair, which would have split one tool's flags across
    two help screens for one shared mode.
    """
    if args.restore_staking:
        if args.asset != "GRC":
            return "--restore-staking is GRC only: no other chain here has a wallet lock."
        return ""
    if args.target is None:
        return "--target is required: say what balance the desk should end up holding."
    return ""


def _restore_mode(say, apply: bool) -> int:
    """Announce the restore and run it. Split out of main() to keep it under the ceiling.

    Rule 12's answer rather than a suppression: adding this mode put main() at twelve
    branches against ruff's ten, and the thing that wanted its own frame was the mode
    itself -- it shares nothing with the send path but the announcement shape.
    """
    say("mechanism  RESTORE STAKING. walletpassphrase against the OPERATOR's wallet with the "
        "staking-only flag set, which opens it for staking and NOT for sending -- a wallet "
        "opened this way refuses sendtoaddress. Nothing is sent and no balance changes.")
    say("why        a failed send locks that wallet BEFORE it unlocks it for sending, so a wrong "
        "passphrase leaves it locked and NOT STAKING. This puts it back, and reads getwalletinfo "
        "afterwards to prove it rather than trusting the call's exit code.")
    return restore_staking(say, apply)


def grc_send(plan: dict) -> str:
    """Broadcast the GRC top-up. Returns the txid. THE ONLY SEND IN THIS FILE.

    THE PASSPHRASE COMES FROM THE ENVIRONMENT AND NOWHERE ELSE. Not a flag, not a
    prompt, not a file: argv is world-readable through /proc, and an interactive
    `read` inside a pasted block consumes the rest of the paste -- which happened on
    2026-10-07 and armed a container with a command fragment instead of a secret.
    """
    passphrase = operator_passphrase()
    # THE PASSPHRASE IS PROVED BEFORE ANYTHING IS LOCKED, and this is the third
    # time the operator's staking wallet paid for its absence. 2026-10-08: a wrong
    # passphrase knocked that wallet out of staking on the first --apply, again on a
    # retry, and again on a third -- because unlocked_for_payout() calls lock()
    # BEFORE unlock_for_sending(), so a wrong secret costs the staking state and
    # cannot restore it.
    #
    # A STAKING-ONLY UNLOCK IS THE CHEAPEST POSSIBLE TEST and it needs no prior
    # lock. On a correct passphrase it leaves the wallet in exactly the resting state
    # unlocked_for_payout() restores it to anyway, so it costs one extra RPC and
    # nothing else. On a wrong one it raises with the daemon's own -14 and the wallet
    # is UNTOUCHED -- still staking, if it was.
    #
    # THE SAME HAZARD IS STILL IN THE PAYOUT PATH and is deliberately not changed
    # here. services/payout_service.payout_unlock_context() enters
    # unlocked_for_payout() the same way for every GRC payout, so a wrong
    # GRIDCOIN_WALLET_PASSPHRASE there has the same cost on the DESK wallet.
    # Re-ordering that is a change to how live payouts take a lock, which is the
    # operator's (rule 16) -- so the difference is named at both sites rather than
    # fixed on one and forgotten.
    proof_refusal = prove_passphrase(plan["source"], passphrase)
    if proof_refusal:
        raise RuntimeError(
            f"{proof_refusal}\n\n"
            f"  Note that the wallet's ENCRYPTION passphrase is a DIFFERENT secret from the RPC "
            f"password -- the balances printed above prove the rpcpassword is right. The GUI on "
            f"{plan['source_label']} will tell you which one that wallet has, and its unlock "
            f"dialog keeps the secret out of argv and out of shell history."
        )

    try:
        with unlocked_for_payout(plan["source"], passphrase):
            return plan["source"].send_to_address(plan["destination"], plan["amount"])
    except GridcoinLockError as error:
        # RE-RAISED WITH WHAT IT COSTS THEM, because this is the operator's own
        # STAKING wallet. chains/gridcoin_wallet_lock.unlocked_for_payout() locks
        # before it unlocks, so a wrong passphrase leaves that wallet locked and not
        # staking -- and it cannot restore it, because restoring needs the same
        # passphrase that just failed.
        raise RuntimeError(
            f"{error}\n\n  THIS IS THE OPERATOR'S STAKING WALLET. If the unlock failed, it is now "
            f"LOCKED and NOT STAKING, and only a correct passphrase puts it back: "
            f"`walletpassphrase <your passphrase> {STAKING_UNLOCK_SECONDS} true` against "
            f"{plan['source_label']}."
        ) from error


def icp_plan(console_say, target: float, minter_identity: str) -> dict:
    """Everything an ICP mint needs, with nothing sent. The mirror of grc_plan().

    NO SOURCE BALANCE, AND THAT IS THE DIFFERENCE BETWEEN A MINT AND A SEND. The
    minting account holds nothing by construction -- icp_ledger_init.py refuses a
    minter that also appears in initial_values -- so there is no balance to read and
    nothing to cap the amount against. `source_available` is therefore the target
    itself: a mint can always cover the shortfall, which is exactly what makes it a
    mint and not a transfer.
    """
    # THE REFUSAL IS READ BEFORE THE SETTINGS ARE, 2026-10-09, and that order is
    # what lets the entry be read with a literal key. These two statements used to
    # be the other way round, with `dict(Config.RPC.get("ICP", {}))` first: the
    # `.get` with a default existed only so the line could not raise before
    # missing_settings() had a chance to produce the sentence below, and the
    # `dict()` copy was never mutated. Refusing first makes both unnecessary, and
    # it matches this file's own stated order -- grc_plan()'s docstring: "resolve
    # the environment, refuse a wrong network BEFORE constructing anything."
    #
    # It also keeps working on a table that has no ICP entry at all, which is what
    # tests/test_fund_desk.py seeds (a GRC-only Config.RPC): missing_settings()
    # reads through _settings(), which answers {} for an absent asset, so both
    # variables come back missing and this returns before `Config.RPC["ICP"]` is
    # evaluated.
    missing = missing_settings(Config.RPC, "ICP")
    if missing:
        return {"refusal": (
            f"ICP is not configured: {why_unconfigured('ICP', Config.RPC)}. The canister ids are "
            f"replica-issued environment state and nothing in this checkout can know them -- read "
            f"them with: docker compose -f docker-compose.yml -f docker-compose.icp.yml exec -T "
            f"icp-replica cat /repo/.dfx/local/canister_ids.json"
        )}

    icp = Config.RPC["ICP"]
    destination = account_identifier(icp["owner_principal"])
    if not is_account_identifier(destination):
        return {"refusal": (
            f"the account identifier derived from ICP_OWNER_PRINCIPAL did not pass its own CRC32 "
            f"check, so nothing was built. Derived: {destination!r}"
        )}

    # THE COMPOSE TRANSPORT, FORCED, and the reason is the whole mechanism. dfx
    # identities live INSIDE the replica container, on the icp-state volume --
    # `minter` among them. The URL transport runs dfx in THIS process's container,
    # where that identity does not exist and where dfx would CREATE one and print its
    # mnemonic (measured 2026-10-07). So a mint runs through `docker compose exec`,
    # which means this tool must run where docker is on PATH: the host, not the web
    # container. Passing network_url here would produce a dfx that signs as an empty
    # anonymous account and a BadFee-shaped failure that says nothing about why.
    # NO DEFAULTS AND NO float(), 2026-10-09: config.IcpRpc declares `service` a
    # `str` and `timeout` a `float`, and config.py defines both for every process
    # ("icp-replica" and ICP_CALL_TIMEOUT defaulting to 60). The two fallbacks were
    # reachable only through the `.get("ICP", {})` that is now gone, and the
    # float() was converting an `object`. dfx_transport() takes (service, timeout)
    # in that order and both are now checked against its signature.
    service = icp["service"]
    timeout = icp["timeout"]
    console_say(f"transport   docker compose exec -T {service} dfx, with --identity "
                f"{minter_identity}  <- identities live in the replica container, so this must run "
                f"on the HOST (docker on PATH), not inside the web container")
    read = dfx_transport(service, timeout)
    mint = dfx_transport(service, timeout, identity=minter_identity)

    console_say(f"destination   {destination}  <- the desk's own account: ICP_OWNER_PRINCIPAL with "
                f"NO subaccount, which is the account get_balance() reads and the one "
                f"icp_subaccount_service refuses to hand a customer")
    held = build_adapters(Config.RPC)["ICP"].get_balance()
    fee_reply = read(icp["ledger_canister_id"], "icrc1_fee", "()")
    console_say(f"balances   desk {held} LICP   <- the token is LICP (\"Local ICP\"), not ICP: a "
                f"local ledger is its own token with its own genesis")
    console_say(f"ledger fee   {fee_reply.strip()} on an ordinary send, and {MINT_FEE_E8S} on a "
                f"MINT -- the minting account is charged nothing, so naming a fee here comes back "
                f"BadFee and chains/icp.py does not retry one")

    # source_available = the shortfall itself. See the docstring: a mint has no
    # source balance, so passing the target would cap nothing and passing 0 would
    # refuse everything. This keeps amount_to_move() the single arithmetic without
    # teaching it about minting.
    amount, why = amount_to_move(held, target, max(target - held, 0.0), source_reserve=0.0)
    # "keeps 0.0 spare" IS TRUE AND MISLEADING, so it is answered rather than left.
    # amount_to_move() does not know this is a mint, and passing the shortfall as the
    # source balance makes its sentence end with a spare of zero -- which reads as a
    # source that was just emptied. There is no source: the minting account holds
    # nothing by construction and icp_ledger_init.py refuses a minter that also holds
    # a balance.
    why += (
        ". There is no source balance to cap against: a mint CREATES the tokens, so the only limit "
        "is the target"
    )
    # E8S IS THE AUTHORITY AND THE FLOAT IS A RENDERING OF IT, which is the opposite
    # way round from the GRC side above -- the ledger's `transfer` takes
    # `amount = record { e8s = N : nat64 }`, an integer, so the decimal figure only
    # ever existed to be converted.
    #
    # IT IS CONVERTED ONCE, HERE, AND CARRIED. The operator's first ICP dry run
    # printed `amount 1.050200000000018` -- 1000.0 - 998.9498 in binary floating
    # point, fifteen digits of artifact in a number about to become 105020000 e8s.
    # Converting in icp_mint() instead would leave the printed figure and the sent
    # figure derived separately from a float, which is rule 8's duplicate on the one
    # value that decides how much moves.
    e8s = amount_to_base_units(amount, ICP_DECIMALS)
    amount = float(Decimal(e8s) / Decimal(10) ** ICP_DECIMALS)
    return {
        "refusal": "", "asset": "ICP", "destination": destination, "amount": amount, "why": why,
        "e8s": e8s, "held": held, "available": None, "mint": mint,
        "ledger": icp["ledger_canister_id"],
        "source_label": f"the ledger's minting account, signed by dfx identity {minter_identity!r}",
    }


def icp_mint(plan: dict, created_at_time_nanos: int) -> str:
    """Mint into the desk's account. Returns the block index.

    `created_at_time_nanos` IS HANDED IN RATHER THAN TAKEN FROM A CLOCK HERE, and
    that is the idempotency decision this file must not make silently. The ledger
    deduplicates on (from, to, amount, fee, memo, created_at_time) for 24 hours and
    returns the ORIGINAL block index for a repeat -- so the same value on a retry is
    one mint, and a fresh value is two. A hand-run top-up has no swap row to take a
    key from, so main() takes it from the clock ONCE, prints it, and tells the
    operator to pass it back with --idempotency-key if they re-run.
    """
    argument = transfer_argument(
        # plan["e8s"], NOT a second conversion of plan["amount"]. icp_plan() derived
        # the integer and then derived the printed float FROM it, so re-converting
        # here would be the only place the two could disagree.
        to_account=plan["destination"], e8s=plan["e8s"],
        fee_e8s=MINT_FEE_E8S, created_at_time_nanos=created_at_time_nanos,
    )
    index = transfer_block_index(plan["mint"](plan["ledger"], "transfer", argument))
    if index:
        return index
    raise RuntimeError(
        "the ledger's transfer reply carried no block index, so WHETHER ANYTHING WAS MINTED IS NOT "
        "ESTABLISHED by this message -- read the reply above. The two cases worth knowing: "
        "`BadFee` means the ledger charged something on a mint after all, and nothing moved; "
        "`TxTooOld` means the idempotency key is outside the ledger's 24h window, and nothing "
        "moved. A reply this tool does not recognize is NOT retried here, because a retry with a "
        "fresh key is a second mint."
    )


#: The assets this tool can top up, and HOW each one is acquired, as data.
#:
#: A TABLE RATHER THAN A CHAIN OF `if args.asset == ...`, which is the same choice
#: fund_testnets.selected_chains() made for the same measured reason: main() goes
#: over ruff's complexity ceiling written the other way, and rule 12 is explicit
#: that the fix is to extract the decision rather than raise the ceiling. Here the
#: decision is "which mechanism, described how", and that is data.
#:
#: THE DESCRIPTION IS PART OF THE ROW because it is what the operator reads before
#: typing --apply, and a mechanism whose name is in one place and whose explanation
#: is in another drifts (rule 8). "mint" and "send" are not interchangeable words
#: here: one creates tokens and debits nobody, the other moves somebody's coins.
MECHANISMS = {
    "GRC": (
        "SEND from the operator's own gridcoinresearchd to the desk's. Real coins moving between "
        "two testnet wallets on this host; needs the operator wallet's ENCRYPTION passphrase from "
        "the environment, which is a different secret from the RPC password. A wrong one is "
        "refused BEFORE anything is locked, so it cannot cost that wallet its staking unlock."
    ),
    "ICP": (
        "MINT. A transfer FROM the local ledger's minting account, signed by the `minter` dfx "
        "identity, which creates the tokens -- nobody is debited. Must run on the HOST, because "
        "the identity lives inside the replica container."
    ),
}


def build_parser() -> argparse.ArgumentParser:
    """The CLI, as a function so a test can assert every advertised flag parses.

    supervisor.py advertised a `-f` its parser rejected and nothing caught it,
    because the only way to find that class of defect is to build the parser without
    running the tool. Same shape here, and this one matters more: a flag an operator
    is told to use and cannot is a flag they work around by hand, with a wallet open.
    """
    parser = argparse.ArgumentParser(
        prog=SELF,
        description=(
            "Top up the desk's hot wallet from a source the operator controls. Dry run unless "
            "--apply. There is NO flag for a passphrase and there never will be: argv is "
            "world-readable through /proc."
        ),
    )
    parser.add_argument("--asset", required=True, choices=sorted(MECHANISMS),
                        help="which hot wallet to top up")
    parser.add_argument("--target", type=float, default=None,
                        help="the balance to bring the desk UP TO, in whole units of the asset. "
                             "Not the amount to move -- what is already held is subtracted. "
                             "Required unless --restore-staking.")
    parser.add_argument("--restore-staking", action="store_true",
                        help="GRC only: put the operator's wallet back to a STAKING-ONLY unlock "
                             "and read getwalletinfo back to prove it. Sends nothing and moves "
                             "nothing. This is what undoes a failed send's lock -- see "
                             "restore_staking() for the 2026-10-08 run that made it necessary.")
    parser.add_argument("--minter-identity", default=DEFAULT_MINTER_IDENTITY,
                        help=f"ICP only: the dfx identity that owns the ledger's minting account "
                             f"(default {DEFAULT_MINTER_IDENTITY!r}). Nothing in this tree records "
                             f"which account that is -- the init file is gitignored environment "
                             f"state -- so this is where you say if yours is named differently.")
    parser.add_argument("--idempotency-key", type=int, default=0,
                        help="ICP only: the created_at_time the ledger deduplicates on, in "
                             "nanoseconds. Omit and one is taken from the clock and printed; pass "
                             "the PRINTED value to re-run a mint that may already have landed, "
                             "which is the difference between one mint and two.")
    parser.add_argument("--apply", action="store_true",
                        help="actually move it. Without this nothing is sent and every check "
                             "still runs against the real daemons.")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    started = time.monotonic()
    bad_arguments = argument_refusal(args)
    if bad_arguments:
        parser.error(bad_arguments)

    def say(text: str) -> None:
        """Rule 14: announce before, not only after. Printed as it happens, flushed."""
        print(f"  {text}", flush=True)

    print(f"{SELF}: {'APPLY -- funds WILL move' if args.apply else 'DRY RUN -- nothing is sent'}",
          flush=True)
    say(f"asset      {args.asset}")
    if args.restore_staking:
        exit_code = _restore_mode(say, args.apply)
        print(labeled("done in", format_duration(time.monotonic() - started)), flush=True)
        return exit_code
    say(f"mechanism  {MECHANISMS[args.asset]}")
    say(f"target     {args.target} {args.asset} held by the desk after this")

    plan = (grc_plan(say, args.target) if args.asset == "GRC"
            else icp_plan(say, args.target, args.minter_identity))

    if plan["refusal"]:
        print(f"\n  REFUSED and nothing was sent: {plan['refusal']}", flush=True)
        print(labeled("done in", format_duration(time.monotonic() - started)), flush=True)
        return 3

    print(f"\n  amount     {plan['amount']} {args.asset}", flush=True)
    say(f"why        {plan['why']}")
    say(f"from       {plan['source_label']}")
    say(f"to         {plan['destination']}")

    if plan["amount"] <= 0:
        # RULE 14: "DID NOTHING" MUST NOT LOOK LIKE "DID WORK". A top-up that moves
        # nothing because the desk is already funded and one that moves nothing
        # because the source is empty are different facts, and amount_to_move()'s
        # sentence above is which -- this line only says the outcome was zero.
        print("\n  NOTHING TO DO. No amount was moved, and that is a result rather than a failure "
              "-- the `why` line above says which case it is.", flush=True)
        print(labeled("done in", format_duration(time.monotonic() - started)), flush=True)
        return 0

    if not args.apply:
        rerun = f"python3 {SELF} --asset {args.asset} --target {args.target}"
        if args.asset == "ICP" and args.minter_identity != DEFAULT_MINTER_IDENTITY:
            rerun += f" --minter-identity {args.minter_identity}"
        print(f"\nDRY RUN: nothing was sent. Every check above ran against the real daemons. To "
              f"move it:\n    {rerun} --apply", flush=True)
        if args.asset == "GRC":
            print(f"    ...with {OPERATOR_UNLOCK_ENV_VAR} exported in that shell. Never as an "
                  f"argument.", flush=True)
        print(labeled("done in", format_duration(time.monotonic() - started)), flush=True)
        return 0

    if args.asset == "GRC":
        say("sending    unlock -> sendtoaddress -> lock -> restore staking, in that order")
        txid = grc_send(plan)
        print(f"\n  SENT       txid {txid}", flush=True)
        say("confirm    python3 chain_balances.py")
    else:
        # THE CLOCK IS READ ONCE, HERE, AND PRINTED. Reading it inside icp_mint()
        # would make a retry a different transaction every time, which is the
        # double-mint wearing the shape of a fix; printing it is what lets the
        # operator turn a retry into the same one.
        key = args.idempotency_key or time.time_ns()
        say(f"idempotency key {key}  <- the ledger dedups on this for 24h. Re-run with "
            f"--idempotency-key {key} and a repeat is the SAME mint, not a second one")
        index = icp_mint(plan, key)
        print(f"\n  MINTED     block index {index}", flush=True)
        say("confirm    python3 swap_readiness.py")
    print(labeled("done in", format_duration(time.monotonic() - started)), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
