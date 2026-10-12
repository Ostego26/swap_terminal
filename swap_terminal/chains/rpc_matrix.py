"""One cross-reference: for a Bitcoin-style RPC name, what EACH of the six chains does.

Role: submodule (a join over the per-chain authorities, plus the one table those
      authorities do not have -- Bitcoin Core's own arrival and removal versions. Holds
      no decision about funds, addresses or amounts.)
Reads: chains/gridcoin_rpc_commands.py, chains/xrp_rpc_map.py, chains/solana_rpc_map.py
      and its own version table. Nothing at runtime; no socket, no adapter, no daemon.
Writes: nothing
Can move funds: no
Mainnet-safe: yes

=========================================================================
WHY THIS EXISTS, AND IT IS A COMBINE RATHER THAN A FOURTH TABLE
=========================================================================

Operator, 2026-10-12, in three steps: "why do you keep fucking choking on gridcoin? ...
it's similar but DIFFERENT. stop assuming" / "just map all grc commands from the
website" / "map all commands for any chain we have and create a cross-reference matrix
of equivalence too" / "combine and extend them".

THREE MAPS ALREADY EXISTED and each was written at the operator's instruction for one
chain: chains/xrp_rpc_map.py (2026-09-30), chains/solana_rpc_map.py (2026-09-30), and
chains/gridcoin_rpc_commands.py (today). Every one keys on the BITCOIN-STYLE METHOD
NAME, which is this tree's lingua franca -- so the join below needs no new facts about
those three chains and deliberately states none. It reads them.

WHAT WAS MISSING was the BTC and LTC columns, which nothing held because they were the
assumption everything else was measured against. That is the hole the operator kept
falling into: "Bitcoin-derived" was the default, the default was never written down, and
so it could not be wrong.

=========================================================================
THE ASSUMPTION THAT WAS BACKWARDS, AND IT IS WHY THE MATRIX EARNS ITS KEEP
=========================================================================

The intuition is that Gridcoin is a 2014 fork, so its RPC set is an OLDER, SMALLER
Bitcoin Core. Measured 2026-10-12, it is not a subset in either direction:

    fundrawtransaction        Core 0.10+     GRC: ABSENT
    getaddressesbylabel       Core 0.17+     GRC: ABSENT (has getaddressesbyaccount)
    createwallet/loadwallet   Core 0.17+     GRC: ABSENT (one wallet per datadir)
    signrawtransaction        REMOVED in     GRC: PRESENT
                              Core 0.18
    createhtlc/claimhtlc/     never in Core  GRC: PRESENT -- native HTLC RPCs
    refundhtlc
    advertisebeacon, magnitude, superblocks, rainbymagnitude, ...
                              never in Core  GRC: PRESENT -- 19 staking RPCs

So `signrawtransaction` works on Gridcoin and does NOT exist on the operator's Bitcoin
Core 28.1, which is the opposite of the subset story; and Gridcoin has whole command
CATEGORIES Core has never had. "Similar but DIFFERENT" is the correct description and
the matrix is the only way to hold it.

=========================================================================
WHAT IT IS NOT
=========================================================================

NOT A CLAIM ABOUT ANY BINARY. Every cell is sourced from a release tag's own code or
from Core's release history, and the evidence kind is on the cell. daemon_capabilities.py
states the same limit for its own rows: the operator's daemon "could have been built
from anywhere in history".

NOT A COMPLETE DUMP OF CORE'S API. The rows are the methods THIS TREE CALLS -- that is
the denominator, it is derived by a test rather than typed here, and a complete Core
index would be hundreds of rows nobody reads, which rules 2 and 9 are about. The GRC
table IS complete, because GRC is the chain whose absences are silent.

NOT A TRANSPORT. xrp_rpc_map.call_for() and solana_rpc_map.call_for() build actual
requests; this answers "would it work, and what is it called there" and sends nothing.
"""

from __future__ import annotations

from typing import NamedTuple

from . import gridcoin_rpc_commands, solana_rpc_map, xrp_rpc_map
from .daemon_capabilities import MEASURED, RELEASE_HISTORY

#: THE CORE VERSION EACH CHAIN'S DAEMON IS, as (major, minor), so one table answers both
#: the BTC and the LTC column. That is the "combine": Litecoin 0.21.4 is a fork of
#: Bitcoin Core 0.21, so a method that arrived in Core 22.0 is absent from it by
#: provenance -- no second table of Litecoin methods is needed or honest.
#:
#: THE TUPLE ORDERING IS WHY THIS WORKS AT ALL. Core went 0.9 ... 0.21 and then jumped to
#: 22.0, so (0, 21) < (22, 0) < (28, 1) compares correctly as tuples while the strings
#: "0.21" and "22.0" do not. A string comparison here would call every Core 22+ method
#: present on Litecoin.
CORE_BASE: dict[str, tuple[int, int]] = {
    "BTC": (28, 1),
    "LTC": (0, 21),
}

#: The Core release each method arrived in. Only methods this tree calls, and only where
#: the version MATTERS -- a method present since before 0.9 is in ANCIENT below rather
#: than given a number nobody needs.
ARRIVED_IN: dict[str, tuple[int, int]] = {
    "fundrawtransaction": (0, 10),
    "getaddressesbylabel": (0, 17),
    "createwallet": (0, 17),
    "loadwallet": (0, 17),
    "listwallets": (0, 17),
    "testmempoolaccept": (0, 17),
    "signrawtransactionwithwallet": (0, 17),
    "listwalletdir": (0, 18),
    "getaddressinfo": (0, 18),
    "generatetoaddress": (0, 16),
    "generateblock": (22, 0),
    "getdeploymentinfo": (25, 0),
    "getblockchaininfo": (0, 9),
    "getnetworkinfo": (0, 9),
    "getbestblockhash": (0, 9),
    "getwalletinfo": (0, 9),
    "decodescript": (0, 9),
}

#: The Core release each method was REMOVED in, and the direction nobody expects.
#:
#: `signrawtransaction` was deprecated in Core 0.17 and REMOVED in 0.18 -- confirmed
#: 2026-10-12 against v28.1's src/rpc/client.cpp, where only signrawtransactionwithkey
#: and signrawtransactionwithwallet appear, and against the 0.18 release notes and
#: bitcoin#14645. GRIDCOIN STILL HAS IT. So this is the cell where the subset story
#: inverts: the method works on the 2014 coin and not on the modern one.
REMOVED_IN: dict[str, tuple[int, int]] = {
    "signrawtransaction": (0, 18),
}

#: Methods present in Core since before 0.9 and never removed. Listed so that a method
#: with no entry anywhere is UNKNOWN rather than silently assumed ancient -- which is
#: the assumption this whole module exists to stop making.
ANCIENT: frozenset[str] = frozenset({
    "createrawtransaction", "decoderawtransaction", "dumpprivkey", "getbalance",
    "getblock", "getblockcount", "getblockhash", "getnewaddress", "getrawtransaction",
    "gettransaction", "gettxout", "help", "importprivkey", "listreceivedbyaddress",
    "listtransactions", "listunspent", "sendrawtransaction", "sendtoaddress",
    "settxfee", "signmessage", "stop", "validateaddress", "verifymessage",
    "walletlock", "walletpassphrase", "walletpassphrasechange", "encryptwallet",
    "backupwallet", "keypoolrefill", "getconnectioncount", "getdifficulty", "getinfo",
    "getpeerinfo", "getrawmempool", "addnode", "ping",
})

#: Methods that are GRIDCOIN'S OWN and have never existed in Bitcoin Core. Derived from
#: the GRC table's categories rather than listed: anything in `staking`, `voting` or
#: `developer` is Gridcoin-specific by construction, and the three wallet-category HTLC
#: RPCs are named because their category does not give them away.
GRIDCOIN_NATIVE_HTLC: frozenset[str] = frozenset({"createhtlc", "claimhtlc", "refundhtlc"})

#: The states a cell can hold. Four, and collapsing any two loses the thing a reader
#: needs: ABSENT and NO_EQUIVALENT differ by whether there is somewhere else to go, and
#: UNKNOWN must never render as either (rule 17).
PRESENT, RENAMED, ABSENT, NOT_RPC, UNKNOWN = "PRESENT", "RENAMED", "ABSENT", "NOT_RPC", "UNKNOWN"


class Cell(NamedTuple):
    """What one chain does with one Bitcoin-style method name.

    `equivalent` is the name to call THERE: the same name when PRESENT, a different one
    when RENAMED, and "" when there is nothing to call. `evidence` says how strongly it
    is known, reusing daemon_capabilities' own vocabulary so a reader meets one scheme.
    """

    asset: str
    method: str
    state: str
    equivalent: str
    evidence: str
    why: str


def _core_cell(asset: str, method: str) -> Cell:
    """BTC and LTC, from one version table. See CORE_BASE for why that is sound."""
    base = CORE_BASE[asset]
    removed = REMOVED_IN.get(method)
    if removed is not None and base >= removed:
        return Cell(asset, method, ABSENT, "", RELEASE_HISTORY, (
            f"removed in Bitcoin Core {removed[0]}.{removed[1]}, and this chain's daemon is "
            f"Core {base[0]}.{base[1]} -- so it is GONE here while Gridcoin still has it"
        ))
    arrived = ARRIVED_IN.get(method)
    if arrived is not None:
        if base < arrived:
            return Cell(asset, method, ABSENT, "", RELEASE_HISTORY, (
                f"arrived in Bitcoin Core {arrived[0]}.{arrived[1]} and this chain's daemon is "
                f"Core {base[0]}.{base[1]}, which predates it"
            ))
        return Cell(asset, method, PRESENT, method, RELEASE_HISTORY, (
            f"arrived in Core {arrived[0]}.{arrived[1]}; this daemon is Core "
            f"{base[0]}.{base[1]}"
        ))
    if method in ANCIENT:
        return Cell(asset, method, PRESENT, method, RELEASE_HISTORY,
                    "present in Bitcoin Core since before 0.9 and never removed")
    if method in GRIDCOIN_NATIVE_HTLC or method in gridcoin_rpc_commands.COMMANDS:
        return Cell(asset, method, ABSENT, "", RELEASE_HISTORY,
                    "a Gridcoin-only RPC; Bitcoin Core has never had it")
    return Cell(asset, method, UNKNOWN, "", "", (
        "not in this module's version tables, so whether this daemon has it is NOT "
        "ESTABLISHED. Add it to ARRIVED_IN, ANCIENT or REMOVED_IN."
    ))


def _gridcoin_cell(method: str) -> Cell:
    """GRC, read off its own dispatch table. No facts restated here."""
    if gridcoin_rpc_commands.has(method):
        return Cell("GRC", method, PRESENT, method, MEASURED, (
            f"in Gridcoin {gridcoin_rpc_commands.SOURCE_TAG}'s own command table, category "
            f"{gridcoin_rpc_commands.COMMANDS[method]}"
        ))
    why = gridcoin_rpc_commands.absent_from_gridcoin(method)
    if not why:
        return Cell("GRC", method, UNKNOWN, "", "", (
            f"absent from Gridcoin {gridcoin_rpc_commands.SOURCE_TAG}'s command table and not "
            f"classified in CORE_ONLY, so no equivalent has been established"
        ))
    equivalent = gridcoin_rpc_commands.CORE_ONLY.get(method, "")
    return Cell("GRC", method, RENAMED if equivalent else ABSENT, equivalent, MEASURED, why)


def _mapped_cell(asset: str, method: str, module) -> Cell:
    """SOL and XRP, from the map each already has. Identical shape, so one function.

    THE TWO MAPS HAVE THE SAME THREE TABLES AND THE SAME TWO LOOKUPS -- CONGRUENT,
    NO_EQUIVALENT, NATIVE_ONLY and equivalent_of()/refuse_without_equivalent() -- which
    is what makes this a join rather than two branches. They were written on the same
    day for the same reason and their shapes agreeing is the thing being used.
    """
    found = module.equivalent_of(method)
    if found is not None:
        return Cell(asset, method, RENAMED, found.method, MEASURED,
                    f"congruent: call `{found.method}` there")
    refusal = module.refuse_without_equivalent(method)
    if refusal:
        return Cell(asset, method, ABSENT, "", MEASURED, refusal)
    return Cell(asset, method, UNKNOWN, "", "", (
        f"neither congruent nor recorded as having no equivalent in "
        f"{module.__name__.rsplit('.', 1)[-1]}, so it is NOT ESTABLISHED"
    ))


#: Why ICP has no row-by-row mapping, said once rather than per method.
ICP_WHY = (
    "ICP is not a JSON-RPC chain at all: chains/icp.py reaches the ledger by running "
    "`dfx canister call` against a Candid interface, so there is no method NAME to map. "
    "The ledger's operations are icrc1_balance_of, icrc1_fee, transfer and the block "
    "queries -- see chains/icp.py and icp/dfx.json. A Bitcoin-style name has no "
    "equivalent here in the sense this matrix means."
)


def cell(asset: str, method: str) -> Cell:
    """What `asset` does with this Bitcoin-style method name. The one lookup.

    DISPATCHED BY CHAIN FAMILY AND NOT BY A CHAIN OF ifs PER ASSET, so adding a seventh
    chain is a table entry. An unknown asset is UNKNOWN rather than an exception: this
    is a reporting join, and a KeyError on a report is the failure mode
    daemon_capabilities._capability() deliberately accepts for a different reason (a
    typo'd capability NAME is a bug, where a chain nobody has mapped is a gap).
    """
    if asset in CORE_BASE:
        return _core_cell(asset, method)
    if asset == "GRC":
        return _gridcoin_cell(method)
    if asset == "SOL":
        return _mapped_cell("SOL", method, solana_rpc_map)
    if asset == "XRP":
        return _mapped_cell("XRP", method, xrp_rpc_map)
    if asset == "ICP":
        return Cell("ICP", method, NOT_RPC, "", MEASURED, ICP_WHY)
    return Cell(asset, method, UNKNOWN, "", "", f"{asset} has no entry in chains/rpc_matrix.py")


#: The chains, in the order a matrix prints them: the three Bitcoin-style ones together
#: so their disagreements line up, then the two mapped ones, then ICP.
ASSETS: tuple[str, ...] = ("BTC", "LTC", "GRC", "SOL", "XRP", "ICP")


def row(method: str) -> dict[str, Cell]:
    """One method across every chain, keyed by asset."""
    return {asset: cell(asset, method) for asset in ASSETS}


def disagreements(method: str) -> str:
    """The one-line summary of a row: which chains differ, or "" when all agree.

    THE POINT OF THE WHOLE MODULE IN ONE FUNCTION. A row where every Bitcoin-style chain
    says PRESENT needs no attention; a row where they disagree is a method somebody will
    call on the wrong chain. Only BTC, LTC and GRC are compared -- SOL and XRP differ
    from them by construction and saying so on every row would bury the signal.
    """
    states = {asset: cell(asset, method).state for asset in ("BTC", "LTC", "GRC")}
    if len(set(states.values())) == 1:
        return ""
    return ", ".join(f"{asset}={state}" for asset, state in states.items())
