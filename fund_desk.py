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
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "swap_terminal"))

from chains.coin_amounts import amount_to_base_units
from chains.gridcoin import GridcoinAdapter
from chains.gridcoin_wallet_lock import (
    STAKING_UNLOCK_SECONDS,
    GridcoinLockError,
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


def amount_to_move(held: float, target: float, source_available: float) -> tuple[float, str]:
    """How much to move to bring `held` up to `target`. THE ARITHMETIC, on its own.

    Returns (amount, why). `amount` is 0.0 for every case that moves nothing, so a
    caller cannot broadcast a figure this function refused -- the same shape
    fee_sweep.SweepPlan uses for the same reason.

    AT THE BOTTOM AND PURE (rule 10): three floats in, a number and a sentence out,
    no adapter, no socket, no chain. It is the only part of a top-up that can be
    asserted exhaustively, and every refusal below is a case a live run would
    otherwise discover with a daemon in the loop.

    THE SHORTFALL IS CAPPED AT WHAT THE SOURCE HAS, rather than refused outright,
    and that is a judgment with a reason: a partial top-up is useful -- it is strictly
    more inventory than before -- and refusing it would leave the desk at 11 GRC
    because the operator was 10 short of the full figure. The sentence says it was
    capped, because a number that silently means something else is rule 14's defect.
    """
    if target <= 0:
        return 0.0, f"the target is {target}, so there is nothing to reach"
    shortfall = target - held
    if shortfall <= 0:
        return 0.0, (
            f"the desk already holds {held}, which is at or above the target of {target}. "
            f"Nothing needs to move"
        )
    if source_available <= 0:
        return 0.0, (
            f"the desk is short {shortfall} and the source has {source_available} available, so "
            f"nothing can move. This is a fact about the SOURCE, not a refusal about the desk"
        )
    if source_available < shortfall:
        return source_available, (
            f"CAPPED AT THE SOURCE. The desk is short {shortfall} and the source can spare "
            f"{source_available}, so this moves all of it and leaves the desk "
            f"{held + source_available} against a target of {target} -- still short by "
            f"{shortfall - source_available}"
        )
    return shortfall, (
        f"the desk holds {held}, the target is {target}, so this moves the {shortfall} difference "
        f"and the source keeps {source_available - shortfall}"
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
    endpoint, refusal = operator_endpoint()
    if endpoint is None:
        # operator_endpoint()'s own sentence already names which variables are unset
        # and why there is no fallback to the desk's credentials, so this adds the
        # one thing it cannot know: WHICH daemon is wanted on this host. Repeating
        # the export line it already printed would be two sentences competing to be
        # the instruction.
        return {"refusal": (
            f"{refusal} On this host the daemon you want is the operator's own on 25715, not the "
            f"desk's on 25779 -- and 25715 answering the desk's credentials with 401 Authorization "
            f"Required is the custody separation working, not a fault to route around."
        )}

    desk_port = int(Config.RPC.get("GRC", {}).get("port") or 0)
    for whose, port, variable in (
        ("the operator's", endpoint.port, OPERATOR_PORT_VARIABLE),
        ("the desk's", desk_port, "GRC_RPC_PORT"),
    ):
        gate = _port_gate("GRC", port, whose, variable)
        if gate:
            return {"refusal": gate}

    if endpoint.port == desk_port and endpoint.host == Config.RPC["GRC"].get("host", "127.0.0.1"):
        # CAUGHT WITHOUT A SOCKET, which is why it is here and not left to
        # direction_verdict() below. Same host and same port IS one daemon, so the
        # ownership reads would both answer True and the refusal would be correct
        # -- but it would have cost two RPC calls and an unlock to establish what
        # two integers already say.
        return {"refusal": (
            f"{OPERATOR_PORT_VARIABLE} and GRC_RPC_PORT are both {desk_port} on {endpoint.host}, "
            f"so the source and the destination are ONE daemon. A top-up from a wallet to itself "
            f"moves nothing and costs a chain fee. Point {OPERATOR_UNLOCK_ENV_VAR}'s wallet at "
            f"the operator's own daemon -- 25715 on this host -- and run this again."
        )}

    missing = missing_settings(Config.RPC, "GRC")
    if missing:
        return {"refusal": f"the DESK's GRC daemon is not configured: {why_unconfigured('GRC', Config.RPC)}"}

    console_say(f"source   the operator's own daemon at {endpoint.label} (from "
                f"{', '.join(OPERATOR_REQUIRED_VARIABLES)})")
    source = GridcoinAdapter(
        user=endpoint.user, password=endpoint.password, host=endpoint.host, port=endpoint.port,
        # wallet="" IS REQUIRED, not a default worth leaving to chance: Gridcoin
        # serves no /wallet/<name> path, so a non-empty value makes every call 404.
        # wallet_custody.read_operator_ownership() constructs it the same way.
        wallet="", timeout=OPERATOR_RPC_TIMEOUT_SECONDS,
    )
    desk = build_adapters(Config.RPC)["GRC"]

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
    console_say(f"balances   desk {held} GRC   operator {available} GRC  <- both TESTNET, "
                f"established by the port gate above before either socket opened")
    amount, why = amount_to_move(held, target, available)
    return {
        "refusal": "", "asset": "GRC", "source": source, "destination": destination,
        "amount": amount, "why": why, "held": held, "available": available,
        "source_label": endpoint.label,
    }


def grc_send(plan: dict) -> str:
    """Broadcast the GRC top-up. Returns the txid. THE ONLY SEND IN THIS FILE.

    THE PASSPHRASE COMES FROM THE ENVIRONMENT AND NOWHERE ELSE. Not a flag, not a
    prompt, not a file: argv is world-readable through /proc, and an interactive
    `read` inside a pasted block consumes the rest of the paste -- which happened on
    2026-10-07 and armed a container with a command fragment instead of a secret.
    """
    import os  # noqa: PLC0415 -- checked: imported here so the one read of the environment sits beside the paragraph explaining where the secret may come from, rather than at the top where a reader would have to go looking for which function uses it.

    passphrase = os.environ.get(OPERATOR_UNLOCK_ENV_VAR, "")
    if not passphrase:
        raise RuntimeError(
            f"{OPERATOR_UNLOCK_ENV_VAR} is not set in this process's environment, so the "
            f"operator's wallet cannot be unlocked and NOTHING was sent. It is the OPERATOR "
            f"wallet's passphrase and not the desk's -- GRIDCOIN_WALLET_PASSPHRASE is the desk's, "
            f"and the two wallets are not assumed to share a secret. Export it into the shell that "
            f"runs this (never as a command-line argument: argv is world-readable through /proc)."
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
    icp = dict(Config.RPC.get("ICP", {}))
    missing = missing_settings(Config.RPC, "ICP")
    if missing:
        return {"refusal": (
            f"ICP is not configured: {why_unconfigured('ICP', Config.RPC)}. The canister ids are "
            f"replica-issued environment state and nothing in this checkout can know them -- read "
            f"them with: docker compose -f docker-compose.yml -f docker-compose.icp.yml exec -T "
            f"icp-replica cat /repo/.dfx/local/canister_ids.json"
        )}

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
    service = icp.get("service", "icp-replica")
    timeout = float(icp.get("timeout", 60.0))
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
    amount, why = amount_to_move(held, target, max(target - held, 0.0))
    return {
        "refusal": "", "asset": "ICP", "destination": destination, "amount": amount, "why": why,
        "held": held, "available": None, "mint": mint, "ledger": icp["ledger_canister_id"],
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
        to_account=plan["destination"], e8s=amount_to_base_units(plan["amount"], ICP_DECIMALS),
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
        "two testnet wallets on this host; needs the operator wallet's passphrase from the "
        "environment, and a wrong one leaves that wallet locked and NOT STAKING."
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
    parser.add_argument("--target", type=float, required=True,
                        help="the balance to bring the desk UP TO, in whole units of the asset. "
                             "Not the amount to move -- what is already held is subtracted.")
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

    def say(text: str) -> None:
        """Rule 14: announce before, not only after. Printed as it happens, flushed."""
        print(f"  {text}", flush=True)

    print(f"{SELF}: {'APPLY -- funds WILL move' if args.apply else 'DRY RUN -- nothing is sent'}",
          flush=True)
    say(f"asset      {args.asset}")
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
