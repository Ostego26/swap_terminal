#!/usr/bin/env python3
"""Bitcoin-style read-only RPC names mapped to their Solana equivalents.

Role: submodule (a decision, callable with no adapter and no socket -- rule 10)
Reads: nothing. Three tables and four pure functions over them.
Writes: nothing
Can move funds: no. Every method named here is a READ, and this module contains no
       transport -- it returns a method name and a POSITIONAL params list for
       chains/solana.SolanaAdapter.call() to send.
Mainnet-safe: yes. It chooses no endpoint and names no cluster.
Live-safe: yes.

WHY THIS EXISTS, and why it comes after the devnet run rather than before it.

chains/xrp_rpc_map.py was written for the same reason on the same day, at the operator's
instruction, and the SOL tab was left saying "no console here yet ... which is a thing nobody
has done, not a thing that cannot be done". This is that thing.

It was written AFTER solana_chain_check.py passed against api.devnet.solana.com on 2026-09-30,
and that order is the point. The Solana adapter's method names, parameter shapes and response
field names were originally written from documentation in an environment where every cluster
endpoint returned 403 -- so a wrong field name would have passed the whole suite and failed on
the first real call. These are the shapes that run confirmed, cluster DEVNET (genesis
EtWTRABZaYq6iMfeYKouRu166VU2xqa1wcaWoxPkrZBG), solana-core 4.3.0:

    getHealth                              -> "ok"
    getVersion                             -> {"solana-core": "4.3.0", ...}
    getGenesisHash                         -> the hash, which IDENTIFIES the cluster
    getSlot                                -> 506014088
    getEpochInfo                           -> {epoch: 1171, slotIndex: 142090, absoluteSlot: ...}
    getMinimumBalanceForRentExemption(0)   -> 650240 lamports
    getMinimumBalanceForRentExemption(165) -> 1488440 lamports

The rest -- getBalance, getAccountInfo, getSignaturesForAddress, getTransaction,
getTokenAccountBalance -- are the adapter's own documented reads and are NOT confirmed by that
run, because no address or mint was given to it. Said here rather than left to be assumed: a
console call is what confirms each of them, the same way that run confirmed the seven above.

A THIRD REQUEST SHAPE, AND THIS REPO NOW SPEAKS ALL THREE.

    bitcoind   {"method": m, "params": [positional, ...]}
    rippled    {"method": m, "params": [{named: ...}]}          <- ONE object, in a list
    solana     {"method": m, "params": [positional, {options}]} <- options object LAST

So this module returns a LIST where chains/xrp_rpc_map returns a dict, and
SolanaAdapter.call(method, *params) spreads it. Three shapes is exactly why each map is its
own module rather than one table with a shape column: the shape is not data about a method, it
is the calling convention of a whole chain, and a single table would need a branch at every
call site (rule 8 -- and the branch is the copy).

THE COMMITMENT IS ASKED FOR EXPLICITLY, and it is the same decision XRP's
`ledger_index: "validated"` is. Solana's default commitment is `finalized` for most methods,
but a default is not a statement -- and a balance read at `processed` can still change, which
for a deposit is the difference between crediting a customer and crediting a rollback.
Config.SOL_MIN_CONFIRMATIONS is 3, which solana_chain_check.py prints as "rank 3 <- a RUNG on
the commitment ladder, NOT blocks", and rung 3 IS finalized. Every read here that accepts a
commitment asks for it by name.
"""

from __future__ import annotations

from typing import NamedTuple


class SolanaEquivalent(NamedTuple):
    """One Bitcoin-style question, and how Solana answers it.

    `answers` is the path into the result that carries the figure the Bitcoin method would
    have returned, because the two rarely return the same SHAPE even when they answer the
    same question -- getblockcount is an integer and getEpochInfo is an object.

    `argument` is the one positional argument this call takes, if it takes one.
    `commitment` says whether the options object is appended; not every method accepts one,
    and sending it to a method that does not is an invalid-params error rather than a
    harmless extra.

    `note` is a warning about a gap that would otherwise mislead, and it is empty when there
    is none -- not a description of the method.
    """

    method: str
    answers: str
    argument: str = ""
    needs_address: bool = False
    commitment: bool = False
    returns_transactions: bool = False
    note: str = ""


#: 1 SOL = 1_000_000_000 lamports. Named because a balance in lamports looks like a fortune,
#: which is the same trap XRP's drops set -- and chains/solana_units.py owns the arithmetic.
LAMPORTS_PER_SOL = 1_000_000_000

#: The commitment every read here asks for by name. See the module header.
FINALIZED = "finalized"

#: The transaction version this client declares it can parse, sent with every read that
#: returns transactions.
#:
#: MEASURED THE HARD WAY, 2026-09-30. getBlock against api.devnet.solana.com without it:
#:
#:     -32015  Transaction version (0) is not supported by the requesting client. Please try
#:             the request again with the following configuration parameter:
#:             "maxSupportedTransactionVersion": 0
#:
#: A node will not hand a block to a client that has not said which transaction versions it
#: understands, and it refuses the WHOLE BLOCK rather than omitting the versioned transactions
#: in it. Versioned (v0) transactions have been ordinary on Solana since 2022, so in practice
#: every block containing any activity failed -- which is to say getBlock and getTransaction
#: were both broken, for every input, and no test in this repository would ever have shown it:
#: they exercise the map, and the map was internally consistent. The operator's first real call
#: is what found it, which is the argument this module's header already makes about writing a
#: map from documentation.
#:
#: 0 rather than a higher number because 0 is the only version that exists today; raising it
#: would be claiming to parse a format nobody has defined.
MAX_SUPPORTED_TRANSACTION_VERSION = 0

#: WHAT DOES TRANSLATE. Bitcoin-style name -> the Solana call that answers the same question.
CONGRUENT: dict[str, SolanaEquivalent] = {
    "getblockchaininfo": SolanaEquivalent(
        "getEpochInfo", "absoluteSlot / blockHeight", commitment=True,
        note="Solana splits what bitcoind puts in one object. THE CLUSTER IS NOT IN HERE: it is "
             "getGenesisHash, which a cluster cannot lie about where a hostname can -- which is "
             "why solana_chain_check.py identifies the network from that and never from the URL.",
    ),
    "getinfo": SolanaEquivalent(
        "getVersion", "solana-core",
        note="bitcoind DEPRECATED getinfo and split it. This is the build string only; liveness "
             "is getHealth and chain position is getEpochInfo.",
    ),
    "getnetworkinfo": SolanaEquivalent(
        "getClusterNodes", "length of the array",
        note="a LIST OF GOSSIP PEERS, not a version-and-connection-count object. It can be long "
             "on a public cluster. There is no `networks` array: one cluster per endpoint, named "
             "by getGenesisHash.",
    ),
    "getblockcount": SolanaEquivalent(
        "getBlockHeight", "the integer itself", commitment=True,
        note="getBlockHeight, NOT getSlot, AND THE DIFFERENCE IS REAL: a SLOT is a scheduled "
             "leader window and a slot can be SKIPPED, so the slot number runs ahead of the "
             "block height and the two are never equal on a live cluster. MEASURED on devnet "
             "2026-09-30, both figures from ONE getEpochInfo answer: absoluteSlot 506018813 "
             "against blockHeight 493266987 -- a gap of 12,751,826. Mapping this to getSlot "
             "would have been wrong by that much. "
             "solana_chain_check.py prints getSlot as 'a DIAGNOSTIC, never a confirmation "
             "count' for exactly this reason, and anything counting confirmations wants "
             "COMMITMENT rather than either number.",
    ),
    "getbestblockhash": SolanaEquivalent(
        "getLatestBlockhash", "value.blockhash", commitment=True,
        note="THIS IS A SIGNING NONCE, NOT AN IDENTIFIER OF THE TIP. A Solana transaction "
             "embeds a recent blockhash to bound its lifetime, and value.lastValidBlockHeight "
             "is when it expires. bitcoind's getbestblockhash names the tip BLOCK; to get that "
             "here, call getBlock(slot) and read its blockhash.",
    ),
    "getconnectioncount": SolanaEquivalent(
        "getClusterNodes", "length of the array",
        note="same call as getnetworkinfo; the count is the array's length rather than a field.",
    ),
    "getpeerinfo": SolanaEquivalent(
        "getClusterNodes", "the array",
        note="no admin gate on this one, unlike rippled's `peers` -- but it reports what GOSSIP "
             "knows, not who this node has a connection to.",
    ),
    "getwalletinfo": SolanaEquivalent(
        "getAccountInfo", "value", needs_address=True, commitment=True,
        note="SOLANA HAS NO WALLET AT THE NODE. There is no keystore, no unlock and no "
             "passphrase: an account is a row of state and its keys live wherever their holder "
             "put them. This is the closest analogue and it is a different kind of thing -- "
             "which is also why get_new_address() refuses on this chain and the deposit-address "
             "strategy is the operator's to choose.",
    ),
    "getbalance": SolanaEquivalent(
        "getBalance", "value", needs_address=True, commitment=True,
        note=f"LAMPORTS, NOT SOL. Divide by {LAMPORTS_PER_SOL:,}. And it is not all spendable: "
             f"an account must keep its rent-exempt minimum, MEASURED at 650240 lamports for a "
             f"0-byte system account on devnet 2026-09-30, or it is purged. Bitcoin's getbalance "
             f"has no equivalent of a rent floor.",
    ),
    "listtransactions": SolanaEquivalent(
        "getSignaturesForAddress", "the array", needs_address=True, commitment=True,
        note="SIGNATURES ONLY, newest first -- each entry is a reference, not the transaction. "
             "getTransaction fetches one. Measured 2026-09-29: the public devnet endpoint "
             "returned HTTP 429 on 11 of 20 follow-up reads, which is why the hunt in "
             "solana_chain_check.py is paced.",
    ),
    "getrawtransaction": SolanaEquivalent(
        "getTransaction", "transaction / meta", argument="signature", commitment=True,
        returns_transactions=True,
        note="the argument is a SIGNATURE, not a txid -- Solana identifies a transaction by its "
             "first signature. The answer is JSON; pass {'encoding': 'base64'} for the raw form.",
    ),
    "getblock": SolanaEquivalent(
        "getBlock", "the object", argument="slot", commitment=True, returns_transactions=True,
        note="takes a SLOT where bitcoind takes a hash, and a skipped slot has no block -- the "
             "node answers null, which is a real answer and not an error. "
             "FEEDING IT A BLOCK HEIGHT RETURNS A DIFFERENT BLOCK AND NO ERROR, which is the "
             "expensive way to get this wrong and is how it was found: on 2026-09-30 a "
             "confirmation script passed getBlockHeight's answer (493269229) into this call, "
             "and the node returned the block at SLOT 493269229 -- blockHeight 481049841, "
             "blockTime 1788566330, roughly 25 days old. A plausible object, nothing raised, "
             "and the wrong block. The two numbers differ by ~12.2 million here (see "
             "getblockcount), so a height used as a slot lands WEEKS in the past rather than "
             "slightly off.",
    ),
    "getblockhash": SolanaEquivalent(
        "getBlock", "blockhash", argument="slot", commitment=True, returns_transactions=True,
        note="same call as getblock, reading one field of it. THIS is the tip's identifier that "
             "getbestblockhash does not give you.",
    ),
}

#: WHAT DOES NOT TRANSLATE, AND WHY. The half worth reading.
#:
#: Every entry is a question Solana has no answer to -- not one nobody has got round to. Where
#: a DIFFERENT question is the one the operator probably meant, the reason names it (rule 14).
NO_EQUIVALENT: dict[str, str] = {
    "getmininginfo": (
        "Solana is proof of stake. There is no mining, no hashrate and no block subsidy: slots "
        "are assigned to leaders by a stake-weighted schedule, which is `getLeaderSchedule`, and "
        "new supply comes from inflation, which is `getInflationRate`. Neither is a difficulty."
    ),
    "getdifficulty": (
        "there is no proof of work, so there is no difficulty to report. Nothing here "
        "approximates it -- the concept does not exist on this chain. What decides whether a "
        "transaction is final is COMMITMENT, which is a rung on a ladder and not a quantity of "
        "work."
    ),
    "listunspent": (
        "Solana is an account model, not a UTXO model. An account has ONE lamport balance rather "
        "than a collection of spendable outputs, which is why chains/solana.py has no coin "
        "selection. The balance is getBalance; what an account OWNS is getTokenAccountsByOwner."
    ),
    "gettxoutsetinfo": (
        "same reason as listunspent: there is no UTXO set to summarize. The nearest figure is "
        "`getSupply`, which reports total and circulating lamports."
    ),
    "listlockunspent": (
        "no UTXOs, so nothing to lock. The nearest concept is the RENT-EXEMPT MINIMUM, which is "
        "not a lock on an output but a floor under the balance -- "
        "getMinimumBalanceForRentExemption, and 650240 lamports for a 0-byte account as measured "
        "on devnet 2026-09-30."
    ),
    "listaddressgroupings": (
        "there is no wallet at the node to group addresses by, and no change addresses to group. "
        "How this terminal would attribute SOL deposits is UNDECIDED and is the operator's "
        "choice -- a shared account with a memo, or an address per swap -- which is why "
        "get_new_address() refuses rather than guessing (README.md, 'Solana deposit addresses')."
    ),
    "decodescript": (
        "Solana has no script system. A program is compiled BPF bytecode living in an account, "
        "not a script attached to an output, and there is no RPC that renders one as operations. "
        "getAccountInfo on a program's address returns the bytecode itself."
    ),
    "decoderawtransaction": (
        "there is no server method, because there is nothing to ask a server: a transaction is "
        "decoded LOCALLY by solders/solana-py. Asking a remote node to parse a blob you already "
        "hold would be a round trip for a pure function."
    ),
    "getrawmempool": (
        "there is no mempool. A client forwards a transaction to the current leader rather than "
        "gossiping it into a shared pool, so there is no enumerable set of pending transactions "
        "and nothing to list. A transaction is either in a slot or it has expired -- and its "
        "expiry is lastValidBlockHeight from getLatestBlockhash."
    ),
    "validateaddress": (
        "this is LOCAL on Solana, not a server call: an address is 32 bytes of base58 and "
        "validating it means checking the encoding, the length, and that the point is on the "
        "curve. chains/solana_address.py is the implementation. A node will happily accept a "
        "well-formed address that has never existed, because an account that holds nothing IS "
        "nothing -- so 'valid' and 'in use' are separate questions here."
    ),
    "uptime": (
        "no node uptime is exposed over JSON-RPC. `getHealth` answers whether this node is "
        "caught up, which is the question uptime is usually a proxy for, and it answers it "
        "directly -- it returned 'ok' on devnet 2026-09-30."
    ),
    "help": (
        "Solana's JSON-RPC has no `help` method. The method list is documentation rather than "
        "something the node will recite."
    ),
}

#: READS SOLANA ANSWERS THAT BITCOIN HAS NO NAME FOR. Offered because the mapping is not the
#: point, knowing the chain is -- and six of these seven are the calls solana_chain_check.py
#: made on the run that proved this endpoint works, so they are the measured ones.
NATIVE_ONLY: dict[str, SolanaEquivalent] = {
    "getHealth": SolanaEquivalent(
        "getHealth", "the string itself",
        note="'ok' means this node is caught up. Anything else means it is behind or unwell, and "
             "a balance read from it may be stale. MEASURED 'ok' on devnet 2026-09-30.",
    ),
    "getGenesisHash": SolanaEquivalent(
        "getGenesisHash", "the hash itself",
        note="IDENTIFIES THE CLUSTER, and a cluster cannot lie about it where a hostname can. "
             "EtWTRABZaYq6iMfeYKouRu166VU2xqa1wcaWoxPkrZBG is devnet; "
             "5eykt4UsFv8P8NJdTREpY1vzqKqZKvdpKuc147dw2N9d is MAINNET-BETA and is real money. "
             "solana_chain_check.GENESIS_HASHES is the table.",
    ),
    "getSlot": SolanaEquivalent(
        "getSlot", "the integer itself", commitment=True,
        note="A DIAGNOSTIC, NEVER A CONFIRMATION COUNT. Slots are scheduled windows and can be "
             "skipped, so this runs ahead of the block height; what decides finality is "
             "commitment. MEASURED 506014088 on devnet 2026-09-30.",
    ),
    "getEpochInfo": SolanaEquivalent(
        "getEpochInfo", "epoch / slotIndex / absoluteSlot", commitment=True,
        note="context when a deposit looks stalled. MEASURED epoch 1171, slotIndex 142090 on "
             "devnet 2026-09-30.",
    ),
    "getMinimumBalanceForRentExemption": SolanaEquivalent(
        "getMinimumBalanceForRentExemption", "the integer itself", argument="space",
        note="the lamports an account of that many bytes must hold or be purged. THE CHAIN IS "
             "THE AUTHORITY and nothing sizes a transfer from the constant in "
             "chains/solana_units.py -- SIMD-0437 has three more steps to come, so a mismatch "
             "with the reference is EXPECTED rather than a defect. MEASURED 650240 at 0 bytes "
             "and 1488440 at 165 on devnet 2026-09-30.",
    ),
    "getTokenAccountBalance": SolanaEquivalent(
        "getTokenAccountBalance", "value.amount / value.decimals", needs_address=True,
        commitment=True,
        note="an SPL TOKEN account's balance -- the address is the token account, not its owner. "
             "value.amount is a STRING of base units and value.decimals is where the point goes; "
             "reading amount as a number is how a token balance comes out 10^decimals wrong. NOT "
             "confirmed by a run: no mint was configured on 2026-09-30.",
    ),
    "getSupply": SolanaEquivalent(
        "getSupply", "value.total / value.circulating", commitment=True,
        note="total and circulating lamports, which is the nearest thing to gettxoutsetinfo's "
             "summary. NOT confirmed by a run.",
    ),
}


def equivalent_of(bitcoin_method: object) -> SolanaEquivalent | None:
    """The Solana call that answers this Bitcoin-style question, or None.

    None for a name in NO_EQUIVALENT and None for a name in neither table, because the CALLER
    must not treat those the same way -- that distinction is refuse_without_equivalent()'s to
    make. This function answers only "is there a translation".
    """
    if not isinstance(bitcoin_method, str):
        return None
    return CONGRUENT.get(bitcoin_method) or NATIVE_ONLY.get(bitcoin_method)


def refuse_without_equivalent(bitcoin_method: object) -> str:
    """"" if this name translates, else why it does not. THE decision.

    THREE OUTCOMES AND THEY ARE NOT INTERCHANGEABLE, for the reason
    chains/xrp_rpc_map.refuse_without_equivalent() gives at length: "no equivalent exists"
    sends the operator away, "not mapped" sends them looking, and collapsing the two sends
    somebody hunting for a getdifficulty that cannot exist.
    """
    if not isinstance(bitcoin_method, str) or not bitcoin_method:
        return f"{bitcoin_method!r} is not a method name"
    if bitcoin_method in NO_EQUIVALENT:
        return f"{bitcoin_method} has NO Solana equivalent: {NO_EQUIVALENT[bitcoin_method]}"
    if equivalent_of(bitcoin_method) is None:
        return (
            f"{bitcoin_method} is not mapped for Solana. This table knows "
            f"{', '.join(sorted(CONGRUENT))} as Bitcoin-style names, and "
            f"{', '.join(sorted(NATIVE_ONLY))} as Solana reads with no Bitcoin name. It is not a "
            f"claim that no equivalent exists -- see NO_EQUIVALENT in this module for the names "
            f"where that IS the claim."
        )
    return ""


class MissingArgument(ValueError):
    """An entry needs an argument and none was given.

    A distinct type because the caller reports it differently from a refusal: a refusal means
    stop, and this means say what is needed and ask again.
    """


def call_for(bitcoin_method: str, argument: object = None,
                    address: str = "") -> tuple[str, list]:
    """(method, POSITIONAL params) for one translated call. Sends nothing.

    A LIST, not a dict, because Solana's params are positional with the options object LAST --
    see the module header on the three shapes this repo now speaks.
    SolanaAdapter.call(method, *params) spreads it.

    RAISES RATHER THAN GUESSING, and that is the whole reason this is a function. getBlock with
    no slot is an invalid request; getBalance with no address cannot be answered at all; and
    getMinimumBalanceForRentExemption with no size silently answers about a ZERO-byte account,
    which is a confident answer to a question nobody asked (rule 17).

    THE ADDRESS IS NOT INVENTED HERE. It comes from the caller, which gets it from the operator
    or from configuration; this module reads no environment and no config, so it cannot quietly
    answer about the hot wallet when the operator meant a customer's account.
    """
    entry = equivalent_of(bitcoin_method)
    if entry is None:
        raise ValueError(refuse_without_equivalent(bitcoin_method))

    params: list = []
    if entry.needs_address:
        if not isinstance(address, str) or not address.strip():
            raise MissingArgument(
                f"{bitcoin_method} translates to `{entry.method}`, which is ABOUT AN ACCOUNT and "
                f"needs one. Solana has no wallet at the node to default to -- see this entry's "
                f"note -- so there is nothing to fill in. Give the base58 address."
            )
        params.append(address.strip())
    if entry.argument:
        if argument is None or (isinstance(argument, str) and not argument.strip()):
            raise MissingArgument(
                f"{bitcoin_method} translates to `{entry.method}`, which needs "
                f"`{entry.argument}`. Without it the node either refuses the request or answers "
                f"about something you did not ask about."
            )
        params.append(argument.strip() if isinstance(argument, str) else argument)
    if entry.commitment:
        # ASKED FOR BY NAME, never left to the default. A default is not a statement, and a
        # balance read at a weaker commitment can still change -- which for a deposit is the
        # difference between crediting a customer and crediting a rollback. This is the same
        # decision as chains/xrp_rpc_map's ledger_index="validated".
        # WITHOUT THE VERSION THE NODE REFUSES THE WHOLE ANSWER -- see
        # MAX_SUPPORTED_TRANSACTION_VERSION for the error it returns and why every block with
        # any activity in it hit it. ONE options object rather than two appended dicts: Solana
        # takes one, and a second would be an extra positional argument where the method
        # expects none.
        #
        # SPELLED AS A DICT LITERAL rather than assigned by subscript, and that is not a style
        # choice: tests/test_address_literals_are_valid.py flags any 30-character base58-legal
        # literal in the tree unless it sits in dict-KEY position, which is a precise rule --
        # nothing in this repository pays money to a key. `maxSupportedTransactionVersion` is
        # base58-legal by coincidence and 30 characters long, so a subscript assignment tripped
        # that gate. The first fix widened the gate; this writes the call site the way the gate
        # already recognizes, which is the right direction (rule 19: fix the cause, and the
        # cause was here).
        options: dict = ({"commitment": FINALIZED, "maxSupportedTransactionVersion":
                          MAX_SUPPORTED_TRANSACTION_VERSION}
                         if entry.returns_transactions else {"commitment": FINALIZED})
        params.append(options)
    return entry.method, params
