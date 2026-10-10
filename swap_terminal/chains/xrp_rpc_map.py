#!/usr/bin/env python3
"""Bitcoin-style read-only RPC names mapped to their XRP Ledger equivalents.

Role: submodule (a decision, callable with no adapter and no socket -- rule 10)
Reads: nothing. Two tables and four pure functions over them.
Writes: nothing
Can move funds: no. Every method named here is a READ, and the module contains no
       transport at all -- it returns a method name and a params dict for
       chains/xrp.XRPAdapter.call() to send.
Mainnet-safe: yes. It chooses no endpoint and names no network.
Live-safe: yes.

WHY THIS EXISTS. Operator instruction, 2026-09-30: "please map xrp control commands
that are congruent to btc/ltc/grc rpc commands since xrp is just different."

The operator panel's RPC console speaks one protocol -- Bitcoin-style
{"method": ..., "params": [...]} against a daemon that answers that shape -- and it
refused the XRP tab outright, saying so. That refusal was honest and it was also a
dead end: the XRP Ledger answers the same QUESTIONS, under different names, in a
different request shape, and with a handful of questions that genuinely have no
answer there at all.

THE THREE DIFFERENCES THAT MAKE THIS A TABLE RATHER THAN A RENAME.

  1. THE REQUEST SHAPE. rippled takes `params` as a LIST CONTAINING ONE OBJECT of
     NAMED arguments -- {"method": "account_info", "params": [{"account": "r..."}]} --
     where bitcoind takes a positional array. chains/xrp.XRPAdapter.call() already
     owns that shape and the 200-with-error behavior, so this module returns
     (method, params dict) and sends nothing.

  2. NAMED ARGUMENTS, NOT POSITIONS. `getblock <hash>` has no positional analogue;
     `ledger` wants {"ledger_index": N}. So each entry says which named argument the
     operator's input becomes, and an entry that needs one REFUSES without it rather
     than sending a call that asks for the wrong ledger.

  3. SOME QUESTIONS DO NOT EXIST THERE. There is no proof of work, so no difficulty.
     No UTXO set, so no listunspent and no gettxoutsetinfo. No script system, so no
     decodescript. No mempool, so no getrawmempool. NO_EQUIVALENT below is the half
     of this map that matters most: a console that quietly returned something
     plausible for `getdifficulty` would be worse than one that refuses, and the
     reason is rule 14's -- an answer nobody can tell is meaningless is worse than a
     refusal that says why.

WHAT IS DELIBERATELY NOT HERE. No write, no sign, no `submit`, no `wallet_propose`,
no key of any kind. The panel's own allowlist (chains/daemon_wallet.READ_ONLY_RPCS)
is still the gate on what may be asked for; this only says what the asking TRANSLATES
to. Both have to agree for a call to happen, and NO_EQUIVALENT can only ever shrink
what the console offers, never widen it -- asserted by a test, because "a second
allowlist" is the shape that lets one of them drift open.
"""

from __future__ import annotations

from typing import NamedTuple


class XRPEquivalent(NamedTuple):
    """One Bitcoin-style question, and how the XRP Ledger answers it.

    `answers` is the path into rippled's result that carries the figure the Bitcoin
    method would have returned, because the two rarely return the same SHAPE even
    when they answer the same question -- `getblockcount` is an integer and
    `ledger_closed` is an object with the index inside it. The page prints the whole
    result and names this path beside it, so the operator is not left comparing a
    number to a document.

    `note` is for a difference that would otherwise mislead, and it is empty when
    there is none. Not a description of the method -- a warning about the gap.
    """

    method: str
    answers: str
    argument: str = ""
    needs_account: bool = False
    admin_only: bool = False
    note: str = ""


#: The one place a drops figure is named, because 1 XRP = 1,000,000 drops is the
#: conversion that makes `getbalance` LOOK like it returned a fortune. chains/xrp.py
#: owns the arithmetic; this exists so the note below cannot drift from it.
DROPS_PER_XRP = 1_000_000

#: WHAT DOES TRANSLATE. Bitcoin-style name -> the XRP Ledger call that answers the
#: same question.
#:
#: Several map to `server_info`, and that is not laziness: bitcoind split one
#: `getinfo` into getblockchaininfo / getnetworkinfo / getwalletinfo / getmininginfo
#: and deprecated the original, while rippled never split its equivalent. One method
#: answering four questions is the honest shape of that history, and each entry's
#: `answers` path says which part of the response is the part being asked for.
CONGRUENT: dict[str, XRPEquivalent] = {
    "getblockchaininfo": XRPEquivalent(
        "server_info", "info.validated_ledger",
        note="rippled never split its getinfo the way bitcoind did, so this one call also "
             "answers getnetworkinfo, getwalletinfo's server half and uptime.",
    ),
    "getinfo": XRPEquivalent(
        "server_info", "info",
        note="bitcoind DEPRECATED getinfo and split it; rippled did not. This is the closest "
             "thing to the original on either chain.",
    ),
    "getnetworkinfo": XRPEquivalent(
        "server_info", "info.build_version / info.peers",
        note="no `networks` array: the XRP Ledger has one network per endpoint, named by "
             "info.network_id, and that field is what require_non_mainnet() reads.",
    ),
    "getblockcount": XRPEquivalent(
        "ledger_closed", "ledger_index",
        note="THIS IS THE LAST CLOSED LEDGER, WHICH IS NOT NECESSARILY VALIDATED. The "
             "validated index is server_info.validated_ledger.seq, and THAT is the one a "
             "payout's depth must be measured against -- a closed ledger can still change, a "
             "validated one cannot. Bitcoin's height is never final either, which is why it "
             "needs confirmations; an XRPL validated ledger is, which is why "
             "XRP_MIN_CONFIRMATIONS is 1.",
    ),
    "getbestblockhash": XRPEquivalent(
        "ledger_closed", "ledger_hash",
        note="same call as getblockcount -- one rippled method carries both the index and the "
             "hash, where bitcoind needs two.",
    ),
    "getconnectioncount": XRPEquivalent(
        "server_info", "info.peers",
        note="a COUNT only. The per-peer detail is `peers`, which is admin-only -- see "
             "getpeerinfo.",
    ),
    "getpeerinfo": XRPEquivalent(
        "peers", "peers", admin_only=True,
        note="ADMIN-ONLY ON rippled. A public JSON-RPC endpoint refuses it, and that refusal "
             "comes from the server rather than from here -- the count alone is in "
             "server_info.peers, which every endpoint answers.",
    ),
    "getwalletinfo": XRPEquivalent(
        "account_info", "account_data", needs_account=True,
        note="RIPPLED HAS NO WALLET. There is no keystore, no unlock and no passphrase: an "
             "'account' is a row on the ledger and the keys live wherever their holder put "
             "them. This is the closest analogue and it is a different kind of thing -- which "
             "is also why the XRP tab has no wallet-lock switch to offer.",
    ),
    "getbalance": XRPEquivalent(
        "account_info", "account_data.Balance", needs_account=True,
        note=f"DROPS, NOT XRP. Divide by {DROPS_PER_XRP:,} -- a balance that looks like a "
             f"fortune is drops. And it is not all spendable: the base reserve plus the owner "
             f"reserve is locked, which chains/xrp.reserve_xrp() computes from server_info. "
             f"Bitcoin's getbalance has no equivalent of a reserve.",
    ),
    "listtransactions": XRPEquivalent(
        "account_tx", "transactions", needs_account=True,
        note="one account's history, newest first. It takes ledger_index_min / _max rather "
             "than a count and a skip, and it returns full transaction objects rather than "
             "wallet-relative entries -- there is no wallet to be relative to.",
    ),
    "getrawtransaction": XRPEquivalent(
        "tx", "tx_json", argument="transaction",
        note="the argument is a transaction HASH, and the answer is JSON rather than a hex "
             "blob -- XRPL transactions are JSON natively. Pass binary:true for the blob.",
    ),
    "getblock": XRPEquivalent(
        "ledger", "ledger", argument="ledger_index",
        note="takes an INDEX where bitcoind's getblock takes a hash. Add transactions:true to "
             "get the transaction list; `ledger` omits it by default.",
    ),
    "getblockhash": XRPEquivalent(
        "ledger", "ledger_hash", argument="ledger_index",
        note="same call as getblock, reading one field of it.",
    ),
    "uptime": XRPEquivalent(
        "server_info", "info.uptime",
        note="SECONDS, as the server reports them. Rule 6 governs how this repo PRINTS a "
             "duration, not what an external API returns -- convert at the display, not here.",
    ),
}

#: WHAT DOES NOT TRANSLATE, AND WHY. This is the half of the map worth reading.
#:
#: Every entry is a question that has no answer on the XRP Ledger -- not one this
#: module has not got round to. A console that returned something plausible for
#: `getdifficulty` would be worse than one that refuses, because the reader cannot
#: tell a meaningless answer from a meaningful one (rule 14). Where a DIFFERENT
#: question is the one the operator probably meant, the reason names it.
NO_EQUIVALENT: dict[str, str] = {
    # ADDED 2026-10-10 WITH THE WALLET PANE, and the partition test is what demanded
    # it: `listwallets` and `listwalletdir` went onto the panel's read-only allowlist
    # for chains/daemon_wallet.wallet_state(), and
    # test_the_allowlist_is_an_EXACT_PARTITION_of_the_panels_own went red with the
    # right sentence -- "'not mapped' is a different claim from 'no equivalent
    # exists' -- decide which and record it". This is that decision, recorded.
    "listwallets": (
        "rippled HOLDS NO WALLET. There is no server-side wallet to load, unload or list: an "
        "XRPL account is an entry in the ledger, its secret lives wherever the submitter keeps "
        "it, and this repository's own XRP secret is read only inside "
        "chains/xrp_payout_seed.derived_payout_account(), which hands back a public classic "
        "address and never the value. So there is nothing this could translate to -- not an "
        "empty list, which would be an answer, but no such question."
    ),
    "listwalletdir": (
        "same reason as listwallets, one layer further out: there is no wallet FILE either. "
        "rippled stores no per-account key material, so there is no directory to enumerate. A "
        "console that returned an empty list here would be stating that this node has no "
        "wallets on disk, which is true of every rippled that ever ran and says nothing about "
        "this one."
    ),
    "getmininginfo": (
        "the XRP Ledger has no mining. Ledgers close by validator consensus roughly every 4 "
        "seconds, there is no hashrate and no block subsidy, and the transaction fee is BURNED "
        "rather than paid to anybody. `server_info` is where the closing cadence shows up, and "
        "`fee` is where the current cost does."
    ),
    "getdifficulty": (
        "there is no proof of work at all, so there is no difficulty to report. Nothing here is "
        "an approximation of it -- the concept does not exist on this chain."
    ),
    "listunspent": (
        "there is no UTXO set. An XRPL account has ONE balance, not a collection of spendable "
        "outputs, which is why chains/xrp.py has no coin selection and why an XRP payout cannot "
        "be built from chosen inputs. The balance is account_info.account_data.Balance."
    ),
    "gettxoutsetinfo": (
        "same reason as listunspent: no UTXO set to summarize. The nearest figure is the ledger "
        "header's total coin supply, in `ledger`'s result."
    ),
    "listlockunspent": (
        "no UTXOs, so nothing to lock. The nearest concept is the account RESERVE, which is not "
        "a lock on an output but a minimum balance -- chains/xrp.reserve_xrp()."
    ),
    "listaddressgroupings": (
        "there is no wallet here to group addresses by, and no change addresses to group. One "
        "account is one address; this terminal attributes XRP deposits by DESTINATION TAG on a "
        "single shared account, which is the whole reason get_new_address() refuses on this "
        "chain."
    ),
    "decodescript": (
        "the XRP Ledger has no script system. There is nothing to decode: transaction types are "
        "a fixed set of named objects, not programs, which is also why an XRPL escrow is a "
        "different mechanism from an HTLC output script."
    ),
    "decoderawtransaction": (
        "there is no server method for it, because there is nothing to ask a server: XRPL "
        "transactions are JSON, and the binary form is decoded LOCALLY by "
        "xrpl.core.binarycodec. Asking a remote server to parse a blob you already hold would "
        "be a round trip for a pure function."
    ),
    "getrawmempool": (
        "there is no mempool. A transaction is either in a closed ledger or in the load-based "
        "QUEUE, and the queue is not enumerable -- `fee` reports current_queue_size and "
        "max_queue_size, which is a depth rather than a list of hashes."
    ),
    "validateaddress": (
        "this is LOCAL on the XRP Ledger, not a server call: an address carries its own "
        "checksum. chains/xrp.XRPAdapter.validate_address() is the implementation -- and a "
        "review on 2026-09-26 found it accepts any X-address WITHOUT checking that checksum, "
        "which is recorded here because this is where a reader comes looking for it."
    ),
    "help": (
        "rippled has no `help` method over JSON-RPC. The method list is documentation rather "
        "than something the server will recite."
    ),
}

#: READS THE XRP LEDGER ANSWERS THAT BITCOIN HAS NO NAME FOR. Offered because the
#: mapping is not the point -- knowing the chain is -- and three of these are the
#: things an operator actually needs here: what a transaction will cost, what the
#: reserve is holding back, and whether this endpoint is caught up.
#:
#: `fee` and `server_state` take no argument. The three account_* reads take one.
NATIVE_ONLY: dict[str, XRPEquivalent] = {
    "fee": XRPEquivalent(
        "fee", "drops.open_ledger_fee",
        note="the CURRENT cost of a transaction, in drops, which rises with load. There is no "
             "bitcoind equivalent that is a fee ORACLE rather than an estimate: this is what "
             "the network is charging now, not a prediction.",
    ),
    "server_state": XRPEquivalent(
        "server_state", "state.server_state",
        note="`full` or `proposing` means this server is caught up and its answers are current. "
             "`syncing` or `connected` means it is not, and a balance read from it may be "
             "stale -- which server_info does not make as obvious.",
    ),
    "account_lines": XRPEquivalent(
        "account_lines", "lines", needs_account=True,
        note="TRUST LINES: balances in issued currencies, which are NOT XRP and are somebody "
             "else's liability. Empty is the normal reading for an account that only holds XRP.",
    ),
    "account_objects": XRPEquivalent(
        "account_objects", "account_objects", needs_account=True,
        note="what this account OWNS on the ledger -- escrows, offers, trust lines, tickets. "
             "Each one raises the owner reserve, so this is the answer to 'why is my spendable "
             "balance lower than my balance'. The expired escrow this terminal is holding shows "
             "up here.",
    ),
    "account_offers": XRPEquivalent(
        "account_offers", "offers", needs_account=True,
        note="open orders on the XRPL's own decentralized exchange. This terminal places none; "
             "a non-empty answer on the hot account is worth knowing about.",
    ),
}


def equivalent_of(bitcoin_method: object) -> XRPEquivalent | None:
    """The XRP call that answers this Bitcoin-style question, or None.

    None for a name in NO_EQUIVALENT and None for a name in neither table, because
    the CALLER must not treat those the same way and the distinction is
    refuse_without_equivalent()'s to make, not this one's. This function answers only
    "is there a translation".
    """
    if not isinstance(bitcoin_method, str):
        return None
    return CONGRUENT.get(bitcoin_method) or NATIVE_ONLY.get(bitcoin_method)


def refuse_without_equivalent(bitcoin_method: object) -> str:
    """"" if this name translates, else why it does not. THE decision.

    THREE OUTCOMES, AND THEY ARE NOT INTERCHANGEABLE, which is why each gets its own
    sentence rather than a shared "unsupported":

      it translates        "" -- the caller proceeds to call_for().
      no equivalent        the chain has no such concept, and the reason says what it
                           has instead. The operator should stop looking.
      not a name we know   a typo, or a method nobody has mapped. The operator should
                           keep looking, and the message says where.

    Collapsing the middle two would send somebody hunting for a `getdifficulty`
    equivalent that cannot exist, which is the more expensive of the two mistakes.
    """
    if not isinstance(bitcoin_method, str) or not bitcoin_method:
        return f"{bitcoin_method!r} is not a method name"
    if bitcoin_method in NO_EQUIVALENT:
        return f"{bitcoin_method} has NO XRP Ledger equivalent: {NO_EQUIVALENT[bitcoin_method]}"
    if equivalent_of(bitcoin_method) is None:
        return (
            f"{bitcoin_method} is not mapped for the XRP Ledger. This table knows "
            f"{', '.join(sorted(CONGRUENT))} as Bitcoin-style names, and "
            f"{', '.join(sorted(NATIVE_ONLY))} as XRPL reads with no Bitcoin name. It is not a "
            f"claim that no equivalent exists -- see NO_EQUIVALENT in this module for the names "
            f"where that IS the claim."
        )
    return ""


class MissingArgument(ValueError):
    """An entry needs a named argument and none was given.

    A distinct type because the caller reports it differently from a refusal: a
    refusal means stop, and this means say what is needed and ask again.
    """


def call_for(bitcoin_method: str, argument: object = None, account: object = "") -> tuple[str, dict]:
    """(rippled method, params) for one translated call. Sends nothing.

    RAISES RATHER THAN GUESSING, and that is the whole reason this is a function.
    `ledger` without a ledger_index answers about a DIFFERENT ledger than the
    operator asked about -- rippled defaults to `current` -- and `account_info`
    without an account is an error the server reports in a way that reads like the
    account is missing. Defaulting either would produce a confident answer to a
    question nobody asked (rule 17).

    THE ACCOUNT IS NOT INVENTED HERE EITHER. It comes from the caller, which gets it
    from the operator or from configuration; this module reads no environment and no
    config, so it cannot silently answer about the hot wallet when the operator meant
    a customer's account.
    """
    # `account: object`, NOT `account: str`, AND THE PRECEDENT IS IN THIS SIGNATURE.
    #
    # Look at the three parameters. `argument` has been `object` since this function
    # was written, for exactly the reason below. `account` was `str` and is validated
    # by the identical `isinstance(...) or not ....strip()` line four rows down. One
    # function, two parameters, one rule, two spellings of it -- rule 8's shape at its
    # smallest, and the kind nothing ever fails on.
    #
    # THE RULE, WRITTEN ONCE HERE AND POINTED AT FROM THE OTHER SIX SITES: when a
    # function's own first act is to REFUSE a wrong type BY NAME, a narrow annotation
    # is a claim the body contradicts one line later. It makes the guard read as dead
    # code to anyone -- human or checker -- who trusts the signature, and it makes the
    # test that pins the refusal impossible to write without a suppression. The honest
    # type of a validating boundary is `object`, because `object` is what it genuinely
    # accepts: it takes anything and answers with a named refusal, which is HANDLING
    # the input rather than failing on it.
    #
    # WHAT THIS COSTS, because it is not free and pretending otherwise is the wrong
    # comment: an honest caller passing the wrong type is no longer caught at
    # type-check time. That trade is right HERE and at the five sites below because
    # every one of them reads a value from OUTSIDE the program -- an operator's
    # environment variable, a customer-typed address, a CLI argument -- where the
    # runtime refusal is the real guard and the annotation was never going to be.
    #
    # IT IS NOT RIGHT EVERYWHERE, and chains/solana_transaction.parse_transfer_transaction()
    # is the counter-example this pass deliberately left alone: its input is an artifact
    # this program just produced, handed to it by chains/solana_signing.py, so widening
    # it to `object` would remove real checking on a signing path to satisfy one test.
    # That one went `bytes` -> `bytes | bytearray` instead, which is simply the truth
    # (the body accepts both), and the test finding is named rather than silenced.
    #
    # The six sites, 2026-10-09: this one; modules/address_network.address_network and
    # .is_testnet_address; modules/pubkey_address.address_from_public_key;
    # chains/solana_units.validate_min_commitment_rank; chains/solana_rpc_map.call_for.
    entry = equivalent_of(bitcoin_method)
    if entry is None:
        raise ValueError(refuse_without_equivalent(bitcoin_method))

    params: dict = {}
    if entry.needs_account:
        if not isinstance(account, str) or not account.strip():
            raise MissingArgument(
                f"{bitcoin_method} translates to `{entry.method}`, which is ABOUT AN ACCOUNT and "
                f"needs one. rippled has no wallet to default to -- see this entry's note -- so "
                f"there is nothing to fill in. Give the r-address."
            )
        params["account"] = account.strip()
        # `validated` asks for the answer as of the last VALIDATED ledger rather than
        # the current one. A balance read from an unvalidated ledger can still change,
        # and a payout sized against one is a payout sized against a guess.
        params["ledger_index"] = "validated"
    if entry.argument:
        if argument is None or (isinstance(argument, str) and not argument.strip()):
            raise MissingArgument(
                f"{bitcoin_method} translates to `{entry.method}`, which needs "
                f"`{entry.argument}`. Without it rippled answers about the CURRENT ledger, "
                f"which is not the one you asked about."
            )
        params[entry.argument] = argument.strip() if isinstance(argument, str) else argument
    return entry.method, params
