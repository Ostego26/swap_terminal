"""Every RPC command Gridcoin 5.5.1.0 ACTUALLY HAS, read off its own dispatch table.

Role: submodule (a table of facts plus the two questions callers ask of it; holds no
      decision about funds, addresses or amounts)
Reads: nothing at runtime. The table is data.
Writes: nothing
Can move funds: no
Mainnet-safe: yes

=========================================================================
WHY THIS FILE EXISTS, IN THE OPERATOR'S WORDS
=========================================================================

2026-10-12: "why do you keep fucking choking on gridcoin? it's a 2014 coin. it's not
like ltc or btc. it's similar but DIFFERENT. stop assuming" -- and then "just map all
grc commands from the website."

They are right and it is a pattern, not a slip. Every one of these was found by
assuming Bitcoin Core behavior and then discovering Gridcoin's:

    -rpcbind does not exist in Gridcoin AT ALL      found after assuming Core's bind
    split config files, not [test] sections         cost five rounds
    rpcallowip does not read CIDR                   cost separate rounds
    rpcallowip being SET widens the bind            the opposite of Core, where
                                                    rpcbind controls the bind

The structural reason is that this tree's default is "Bitcoin-derived", and the default
is usually right: GRC is in daemon_capabilities.BITCOIN_FAMILY, shares
chains/base.RPCAdapter with BTC and LTC, and 8-decimal precision and most of the wallet
API really are the same. So a reader -- or a function -- inherits Core's API for free
and only finds the holes by falling into one.

THIS IS THE HOLE MAP. The table below is the `vRPCCommands` dispatch table from
src/rpc/server.cpp at tag 5.5.1.0, which is the tag daemon_capabilities.py's rpcallowip
row was settled against and the line the operator runs. A method not in that table does
not exist: the server answers "Method not found" and nothing in this tree can make it
work.

WHAT IT IMMEDIATELY FOUND, measured 2026-10-12 by diffing this table against every
`self.call("...")` reachable by a GridcoinAdapter:

    fundrawtransaction     ABSENT (Core 0.10+). chains/base.measure_send_fee() calls it,
                           so GRC fee measurement can NEVER succeed -- every GRC send
                           falls back to the configured GRC_NETWORK_FEE_RESERVE. config.py
                           already half-knew this; its comment says "FALLBACK: the
                           configured GRC_NETWORK_FEE_RESERVE" without saying why.
    getaddressesbylabel    ABSENT (Core 0.17 renamed accounts to labels). chains/base.
                           _any_wallet_address() calls it inside a broad except, so on GRC
                           it silently returns nothing and the caller reports "no address
                           could be read" -- true, and for a reason that has nothing to do
                           with the wallet. Gridcoin has getaddressesbyaccount, the
                           pre-0.17 name, and nothing was asking for it.

Both were reached through the SHARED adapter, both were swallowed by a broad `except`,
and both reported a reason naming the METHOD rather than the CHAIN -- which is why
neither had ever been noticed. Rule 12 is explicit about that shape: "It is never
legitimate when the caller cannot tell the failure from a real answer."

THE TABLE IS THE DENOMINATOR (rule 3) AND ITS SIZE IS NOT WRITTEN HERE. The first draft
of this header said "171 commands" and len(COMMANDS) is 166 -- a number I typed rather
than read, which is the exact mistake OPEN_FINDINGS 24 is about, made again three hours
later in a different file. The count is len(COMMANDS); the tag is stated beside them -- not "Gridcoin's RPC API", which is a moving thing.

HOW IT WAS READ, so the next person can redo it rather than trust it: fetched
https://raw.githubusercontent.com/gridcoin-community/Gridcoin-Research/5.5.1.0/src/rpc/server.cpp
and transcribed the dispatch table's name column in table order, then re-queried the same
file for each absence this tree depends on. The categories are that table's own third
column, unchanged.

WHAT THIS FILE IS NOT. It is not a claim about the operator's BINARY, which could have
been built from anywhere in history -- the same limit daemon_capabilities.py states for
its own rpcallowip row. It is a claim about tag 5.5.1.0's source. Nothing here was
executed against a daemon.
"""

from __future__ import annotations

#: The tag the table was read at. Stated, because "Gridcoin's RPC API" is not a fact and
#: this is: daemon_capabilities.py settled its rpcallowip row against the same tag, and
#: CIDR support arrived on `development` AFTER it (commit 924f36eb, 2026-08-23), which is
#: exactly the kind of difference a version-less table would erase.
SOURCE_TAG = "5.5.1.0"
SOURCE_FILE = "src/rpc/server.cpp"
SOURCE_URL = (
    "https://raw.githubusercontent.com/gridcoin-community/Gridcoin-Research/"
    f"{SOURCE_TAG}/{SOURCE_FILE}"
)

#: name -> the dispatch table's own category column. In table order, which is neither
#: alphabetical nor meaningful -- it is kept because a diff against a re-read of the
#: source should be empty rather than a reordering.
COMMANDS: dict[str, str] = {
    "help": "null",
    # -- cat_wallet ---------------------------------------------------------------
    "addmultisigaddress": "wallet", "addredeemscript": "wallet", "backupwallet": "wallet",
    "burn": "wallet", "checkwallet": "wallet", "claimhtlc": "wallet", "createhtlc": "wallet",
    "createrawtransaction": "wallet", "consolidatemsunspent": "wallet",
    "decoderawtransaction": "wallet", "decodescript": "wallet", "dumpprivkey": "wallet",
    "dumpwallet": "wallet", "encryptwallet": "wallet", "getaccount": "wallet",
    "getaccountaddress": "wallet", "getaddressesbyaccount": "wallet", "getbalance": "wallet",
    "getbalancedetail": "wallet", "getnewaddress": "wallet", "getnewpubkey": "wallet",
    "getrawtransaction": "wallet", "getrawwallettransaction": "wallet",
    "getreceivedbyaccount": "wallet", "getreceivedbyaddress": "wallet",
    "gettransaction": "wallet", "getunconfirmedbalance": "wallet", "getwalletinfo": "wallet",
    "importprivkey": "wallet", "importwallet": "wallet", "keypoolrefill": "wallet",
    "listaccounts": "wallet", "listaddressgroupings": "wallet",
    "listreceivedbyaccount": "wallet", "listreceivedbyaddress": "wallet",
    "listsinceblock": "wallet", "liststakes": "wallet", "listtransactions": "wallet",
    "listunspent": "wallet", "consolidateunspent": "wallet", "makekeypair": "wallet",
    "maintainbackups": "wallet", "move": "wallet", "rainbymagnitude": "wallet",
    "refundhtlc": "wallet", "repairwallet": "wallet", "resendtx": "wallet",
    "reservebalance": "wallet", "scanforunspent": "wallet", "sendfrom": "wallet",
    "sendmany": "wallet", "sendrawtransaction": "wallet", "sendtoaddress": "wallet",
    "setaccount": "wallet", "sethdseed": "wallet", "settxfee": "wallet",
    "signmessage": "wallet", "signrawtransaction": "wallet", "upgradewallet": "wallet",
    "validateaddress": "wallet", "validatepubkey": "wallet", "verifymessage": "wallet",
    "walletlock": "wallet", "walletpassphrase": "wallet",
    "walletpassphrasechange": "wallet", "walletdiagnose": "wallet",
    # -- cat_staking --------------------------------------------------------------
    "advertisebeacon": "staking", "advertisebeaconv3": "staking", "beaconauth": "staking",
    "beaconconvergence": "staking", "beaconreport": "staking", "beaconstatus": "staking",
    "createmrcrequest": "staking", "explainmagnitude": "staking", "getlaststake": "staking",
    "getmrcinfo": "staking", "getstakinginfo": "staking", "getmininginfo": "staking",
    "lifetime": "staking", "magnitude": "staking", "pendingbeaconreport": "staking",
    "resetcpids": "staking", "revokebeacon": "staking", "superblockage": "staking",
    "superblocks": "staking",
    # -- cat_developer ------------------------------------------------------------
    "auditsnapshotaccrual": "developer", "auditsnapshotaccruals": "developer",
    "addkey": "developer", "beaconaudit": "developer", "changesettings": "developer",
    "currentcontractaverage": "developer", "debug": "developer", "dumpcontracts": "developer",
    "exportstats1": "developer", "getblockstats": "developer", "getrecentblocks": "developer",
    "inspectaccrualsnapshot": "developer", "listalerts": "developer",
    "listprojects": "developer", "getautogreylist": "developer",
    "listprotocolentries": "developer", "listresearcheraccounts": "developer",
    "listscrapers": "developer", "listsidestakes": "developer",
    "listmandatorysidestakes": "developer", "listsettings": "developer",
    "logging": "developer", "network": "developer",
    "parseaccrualsnapshotfile": "developer", "parselegacysb": "developer",
    "projects": "developer", "readdata": "developer", "reorganize": "developer",
    "sendalert": "developer", "sendalert2": "developer", "sendblock": "developer",
    "superblockaverage": "developer", "versionreport": "developer", "writedata": "developer",
    "listmanifests": "developer", "getmpart": "developer",
    "sendscraperfilemanifest": "developer", "savescraperfilemanifest": "developer",
    "deletecscrapermanifest": "developer", "archivelog": "developer",
    "testnewsb": "developer", "convergencereport": "developer", "scraperreport": "developer",
    # -- cat_network --------------------------------------------------------------
    "addnode": "network", "askforoutstandingblocks": "network",
    "getblockchaininfo": "network", "getnetworkinfo": "network", "clearbanned": "network",
    "currenttime": "network", "getaddednodeinfo": "network", "getnodeaddresses": "network",
    "getbestblockhash": "network", "getblock": "network", "getblockbynumber": "network",
    "getblockbymintime": "network", "getblocksbatch": "network", "getblockcount": "network",
    "getblockhash": "network", "getburnreport": "network", "getcheckpoint": "network",
    "getconnectioncount": "network", "getdifficulty": "network", "getinfo": "network",
    "getnettotals": "network", "getpeerinfo": "network", "getrawmempool": "network",
    "listbanned": "network", "networktime": "network", "ping": "network",
    "setban": "network", "showblock": "network", "stop": "network",
    # -- cat_voting ---------------------------------------------------------------
    "addpoll": "voting", "getpollresults": "voting", "getvotingclaim": "voting",
    "listpolls": "voting", "testpollnotification": "voting", "vote": "voting",
    "votebyid": "voting", "votedetails": "voting",
}


#: Bitcoin Core methods this tree calls that Gridcoin does NOT have, with the Gridcoin
#: equivalent where there is one and "" where there is none.
#:
#: WHY THE MAPPING IS HERE AND NOT A rename() FUNCTION. Two of these have no equivalent
#: at all, so a renamer would have to return something for them and every caller would
#: have to check whether it got one -- which is the same branch, moved. A caller that
#: needs to know asks absent_from_gridcoin() and gets a sentence it can print.
#:
#: VERIFIED BY A SECOND, TARGETED READ of the same file rather than by the absence of a
#: key above, because "I did not find it" and "it is not there" are different claims
#: (rule 2, and rule 17's general form). Each of these was queried by name against
#: SOURCE_URL on 2026-10-12 and reported absent, with getaddressesbyaccount,
#: signrawtransaction, validateaddress, settxfee, listunspent, getblockchaininfo and stop
#: queried in the same pass and reported PRESENT -- so the read distinguishes.
CORE_ONLY: dict[str, str] = {
    # Core 0.10. No Gridcoin equivalent: the wallet has no coin-selection RPC that
    # reports a fee without sending. listunspent + createrawtransaction + a manual
    # change calculation is the nearest, which is building a transaction rather than
    # asking what one would cost.
    "fundrawtransaction": "",
    # Core 0.17 renamed accounts to labels. Gridcoin kept the pre-0.17 vocabulary whole:
    # getaccount, getaccountaddress, setaccount, listaccounts, getreceivedbyaccount,
    # listreceivedbyaccount and move are all present.
    "getaddressesbylabel": "getaddressesbyaccount",
    "listlabels": "listaccounts",
    "getaddressinfo": "validateaddress",
    "signrawtransactionwithwallet": "signrawtransaction",
    # Core 0.15's fee estimator. Gridcoin has settxfee -- a fee you SET, not one it
    # estimates -- which is the whole reason GRC_NETWORK_FEE_RESERVE is configured
    # rather than measured.
    "estimatesmartfee": "settxfee",
    # Core 0.17 multi-wallet. Gridcoin has ONE wallet per datadir and no /wallet/<name>
    # endpoint, which is why three Gridcoin wallets on this host means three DATADIRS.
    "createwallet": "",
    "loadwallet": "",
    "listwallets": "",
    # Core 0.18, and part of the same multi-wallet feature: there is no wallet DIRECTORY
    # to list when a datadir holds exactly one wallet. chains/wallet_hint.py calls this
    # on the live path and its fallback sentence then tells the reader to run
    # `createwallet`, which Gridcoin does not have either -- wrong advice in a line an
    # operator pastes. Classified here so rpc_matrix reports ABSENT with a reason
    # instead of UNKNOWN.
    "listwalletdir": "",
    # Core 22.0. Gridcoin is proof-of-stake and its command table has no block-generation
    # RPC of any kind -- no generate, no generatetoaddress, no generateblock. Only
    # regtest/steps.py calls it, against daemons the harness starts itself.
    "generateblock": "",
    # Core 25.0. Gridcoin has getblockchaininfo for chain state but nothing reporting
    # soft-fork deployment status; its consensus changes do not arrive that way.
    "getdeploymentinfo": "",
}


def has(command: str) -> bool:
    """Does Gridcoin 5.5.1.0 dispatch this RPC method at all?"""
    return command in COMMANDS


def absent_from_gridcoin(command: str) -> str:
    """Why this method cannot work on Gridcoin, or "" when it exists.

    THE SENTENCE NAMES THE CHAIN AND THE TAG, which is the correction this whole module
    is. The two call sites that found this reported "fundrawtransaction refused" and "no
    address could be read" -- both true, both reading as a wallet or a daemon problem,
    and neither saying that the method does not exist on this chain and never will.

    Returns "" for a command that exists AND for one nobody has classified, and those are
    different: the second means this table does not know the method, which is reported by
    the gate in tests/test_gridcoin_rpc_commands.py rather than guessed at here. A
    caller printing this sentence is printing a fact; a caller treating "" as "it works"
    is relying on the gate, which is the intended division.
    """
    if has(command):
        return ""
    equivalent = CORE_ONLY.get(command)
    if equivalent is None:
        return ""
    instead = (
        f" Gridcoin's equivalent is `{equivalent}`."
        if equivalent
        else " Gridcoin has no equivalent, so this cannot be measured on this chain at all."
    )
    return (
        f"`{command}` is a Bitcoin Core method that Gridcoin {SOURCE_TAG} does not dispatch "
        f"-- it is absent from {SOURCE_FILE}'s command table, so the daemon answers "
        f"\"Method not found\".{instead}"
    )
