"""The Solana command map: its coverage, and the property that makes it safe.

THE SAME THREE THINGS tests/test_xrp_rpc_map.py holds, for the second translated chain --
and deliberately a separate file rather than a parameterized one. The two maps share an
INTERFACE (CONGRUENT, NO_EQUIVALENT, NATIVE_ONLY, equivalent_of, refuse_without_equivalent,
call_for, MissingArgument) so one reader can drive both; what they do not share is which
methods write, which questions are impossible, or which notes would cost money if wrong.
A single parameterized suite would have to be told all of that per chain anyway, and it
would read as one test of two chains rather than what it is: two chains, each of which has
to be right about itself.

THE MAP IS THE ALLOWLIST ON THE SOL TAB. regtest/operator_panel.READ_ONLY_RPCS is a list of
BITCOIN names and cannot gate `getHealth` or `getSupply`. So the safety argument lives here:
every Solana method this module can emit must be a read.

WRITTEN AFTER THE DEVNET RUN, WHICH IS WHY SOME NOTES CITE MEASUREMENTS. solana_chain_check.py
passed against api.devnet.solana.com on 2026-09-30 (cluster DEVNET, solana-core 4.3.0), so
seven of these shapes are confirmed and the rest are the adapter's documented reads. The tests
below hold the distinction rather than letting the file blur it.
"""

from __future__ import annotations

import pytest
from chains.solana_rpc_map import (
    CONGRUENT,
    FINALIZED,
    LAMPORTS_PER_SOL,
    MAX_SUPPORTED_TRANSACTION_VERSION,
    NATIVE_ONLY,
    NO_EQUIVALENT,
    MissingArgument,
    call_for,
    equivalent_of,
    refuse_without_equivalent,
)
from regtest.operator_panel import READ_ONLY_RPCS, console_methods

#: Every Solana JSON-RPC method that signs, broadcasts, or changes what the node does.
#:
#: A DENYLIST here and an allowlist in the module, for the reason the XRP file gives: a test
#: that re-derived the allowlist from the tables would assert the tables equal themselves.
#:
#: sendTransaction and requestAirdrop BROADCAST. simulateTransaction does not, but it takes a
#: signed transaction as input and has no place in a read console. The three subscription verbs
#: are websocket-only and would hang an HTTP client rather than answer it.
WRITES_OR_CHANGES = frozenset({
    "sendTransaction", "requestAirdrop", "simulateTransaction",
    "accountSubscribe", "signatureSubscribe", "slotSubscribe",
})


def test_EVERY_METHOD_THIS_MAP_CAN_EMIT_IS_A_READ():
    """THE SAFETY PROPERTY. The map is the allowlist on that tab, so this is what holds it.

    MUTATION: add {"sendrawtransaction": SolanaEquivalent("sendTransaction", "signature")} to
    CONGRUENT. Nothing else in the suite fails -- READ_ONLY_RPCS never sees a SOL request and
    refuse_unless_read_only() is not on that path -- and the console would broadcast whatever
    was pasted into the argument box.
    """
    emitted = {entry.method for entry in (*CONGRUENT.values(), *NATIVE_ONLY.values())}
    assert emitted, "the map emits no method at all, so this test is proving nothing"
    forbidden = sorted(emitted & WRITES_OR_CHANGES)
    assert not forbidden, (
        f"the Solana command map can emit {forbidden}, which broadcast or change the node. This "
        f"map IS the allowlist on the SOL tab -- nothing downstream re-checks it"
    )
    # AND NOTHING THAT MERELY LOOKS LIKE ONE, because a denylist only lists what somebody
    # thought of. The shape rule is that the name STARTS WITH `get`, not that it lacks a
    # substring: Solana's JSON-RPC is camelCase beginning with a verb -- get, send, sign,
    # simulate, request, and the *Subscribe family -- so the leading verb IS the read/write
    # distinction, and every method in this map begins with `get`.
    #
    # THE SUBSTRING VERSION OF THIS CHECK WAS WRITTEN FIRST AND WAS WRONG: it refused
    # `getSignaturesForAddress`, a read, because "Signatures" contains "sign". A guard that
    # fails on correct code teaches the next person to weaken it, which is how a guard stops
    # guarding.
    for method in sorted(emitted):
        assert method.startswith("get"), (
            f"{method!r} is in the map and does not begin with `get` -- on Solana the leading "
            f"verb is what says whether a method reads"
        )


def test_the_allowlist_is_an_EXACT_PARTITION_of_the_panels_own():
    """Every bitcoin-style name translates, or is recorded as untranslatable. No third state.

    A name in NEITHER table makes the console say "not mapped for Solana" -- true, and NOT the
    same claim as "has no equivalent". The operator stops asking about something that may well
    have an answer, and no test would have told them.
    """
    allowlist = set(READ_ONLY_RPCS)
    translated, impossible = set(CONGRUENT), set(NO_EQUIVALENT)

    assert not (translated & impossible), (
        f"{sorted(translated & impossible)} appear in BOTH tables, so the module contradicts "
        f"itself about whether an equivalent exists"
    )
    unaccounted = sorted(allowlist - translated - impossible)
    assert not unaccounted, (
        f"{unaccounted} are on the panel's read-only allowlist and in neither Solana table"
    )
    invented = sorted((translated | impossible) - allowlist)
    assert not invented, (
        f"{invented} are mapped as bitcoin-style names but are not on READ_ONLY_RPCS. A "
        f"Solana-only read belongs in NATIVE_ONLY"
    )


def test_NATIVE_ONLY_holds_no_bitcoin_name_and_the_console_offers_both_halves():
    overlap = sorted(set(NATIVE_ONLY) & (set(READ_ONLY_RPCS) | set(CONGRUENT) | set(NO_EQUIVALENT)))
    assert not overlap, f"{overlap} are in NATIVE_ONLY and are also bitcoin-style names"

    offered = console_methods("solana")
    assert set(offered) == set(CONGRUENT) | set(NATIVE_ONLY)
    assert len(offered) == len(set(offered)), "the console offers a method twice"
    assert offered[:len(CONGRUENT)] == sorted(CONGRUENT)
    assert offered[len(CONGRUENT):] == sorted(NATIVE_ONLY)


def test_getblockcount_maps_to_BLOCK_HEIGHT_and_not_to_SLOT():
    """The distinction that would silently corrupt a confirmation count.

    A SLOT is a scheduled leader window and a slot can be SKIPPED, so the slot number runs
    ahead of the block height and the two are never equal on a live cluster. bitcoind's
    getblockcount is a HEIGHT. Mapping it to getSlot would hand a caller a number that looks
    like a height, is always too big, and drifts further the longer the cluster runs.

    solana_chain_check.py already prints getSlot as "a DIAGNOSTIC, never a confirmation count".
    This is that same rule, held where the translation happens.

    MUTATION: point getblockcount at getSlot. This fails, and nothing else does.
    """
    assert CONGRUENT["getblockcount"].method == "getBlockHeight", (
        "getblockcount maps to something other than getBlockHeight; a slot is not a height"
    )
    note = CONGRUENT["getblockcount"].note
    assert "SKIPPED" in note, "the note does not say why the two differ"
    assert "DIAGNOSTIC, never a confirmation count" in note, (
        "the note does not carry solana_chain_check.py's own warning about getSlot"
    )
    # And getSlot is still REACHABLE, as itself, under its own name.
    assert NATIVE_ONLY["getSlot"].method == "getSlot"


def test_getbestblockhash_says_it_is_a_SIGNING_NONCE():
    """getLatestBlockhash is not the tip's identifier, and reading it as one is a real mistake.

    A Solana transaction embeds a recent blockhash to BOUND ITS LIFETIME. bitcoind's
    getbestblockhash names the tip block. Somebody treating the first as the second would be
    identifying a block by a value chosen for expiry.
    """
    entry = CONGRUENT["getbestblockhash"]
    assert entry.method == "getLatestBlockhash"
    assert "SIGNING NONCE, NOT AN IDENTIFIER" in entry.note
    assert "lastValidBlockHeight" in entry.note, "the note does not say where the expiry is"
    # The real answer to the bitcoin question is named, not left out.
    assert "getBlock" in entry.note


def test_the_balance_note_names_LAMPORTS_and_the_RENT_FLOOR():
    """Two ways to misread getBalance, and one of them is measured.

    It is in lamports, so it looks like a fortune; and an account must keep its rent-exempt
    minimum or be purged, so the figure is not all spendable. 650240 lamports for a 0-byte
    account is MEASURED on devnet 2026-09-30, not sourced.
    """
    note = CONGRUENT["getbalance"].note
    assert "LAMPORTS, NOT SOL" in note
    assert f"{LAMPORTS_PER_SOL:,}" in note, "the note does not give the divisor"
    assert "650240" in note and "MEASURED" in note, (
        "the note does not carry the measured rent floor, which is the half that is not spendable"
    )


def test_every_read_that_can_ask_for_a_COMMITMENT_asks_for_finalized():
    """The same decision as XRP's ledger_index="validated", and for the same reason.

    A default is not a statement. A balance read at a weaker commitment can still change, which
    for a deposit is the difference between crediting a customer and crediting a rollback.

    MUTATION: drop the commitment append. Every params list below loses its options object and
    this fails naming the method.
    """
    for name, entry in sorted({**CONGRUENT, **NATIVE_ONLY}.items()):
        if not entry.commitment:
            continue
        address = "HotWallet1111111111111111111111111111111111" if entry.needs_address else ""
        argument = 1 if entry.argument else None
        _method, params = call_for(name, argument, address)
        # THE KEY, not the whole object. This asserted exact equality until 2026-09-30, when
        # the three transaction-returning reads gained maxSupportedTransactionVersion in the
        # same options object -- and a test that pins a whole dict fails on a correct addition
        # to it. What this test is about is the commitment; the exact shape is
        # test_the_version_option_rides_in_the_SAME_options_object_as_the_commitment's job.
        options = params[-1] if params and isinstance(params[-1], dict) else {}
        assert options.get("commitment") == FINALIZED, (
            f"{name} does not ask for a commitment by name, so it answers at the node's default"
        )


def test_the_params_are_a_POSITIONAL_LIST_in_the_order_solana_wants():
    """Solana's third shape: positional, with the options object LAST.

    rippled takes one named object; bitcoind takes positional; Solana takes positional with
    options appended. Getting the ORDER wrong here is an invalid-params error that says nothing
    about the real mistake.
    """
    method, params = call_for("getbalance", None, "HotWallet1111111111111111111111111111111111")
    assert method == "getBalance"
    assert isinstance(params, list), "Solana params must be a positional LIST, not a dict"
    assert params[0] == "HotWallet1111111111111111111111111111111111", "the address comes first"
    assert params[-1] == {"commitment": FINALIZED}, "the options object comes last"

    method, params = call_for("getblock", 506014088)
    assert method == "getBlock"
    assert params[0] == 506014088, "the slot comes first and stays an INTEGER"

    # A method that accepts neither sends an EMPTY list, not [None] and not [{}].
    assert call_for("getHealth") == ("getHealth", [])


def test_a_call_that_needs_an_ADDRESS_or_an_ARGUMENT_refuses_rather_than_defaulting():
    """getMinimumBalanceForRentExemption with no size silently answers about ZERO bytes.

    That is the case worth naming: it is not an error, it is a confident answer to a question
    nobody asked, and 650240 is a plausible-looking number to be handed by accident (rule 17).
    """
    needing_address = [n for n, e in {**CONGRUENT, **NATIVE_ONLY}.items() if e.needs_address]
    assert needing_address, "no entry needs an address, so this test is vacuous"
    for name in needing_address:
        for blank in ("", "   ", None, 7):
            with pytest.raises(MissingArgument):
                call_for(name, 1, blank)

    needing_argument = [(n, e.argument) for n, e in {**CONGRUENT, **NATIVE_ONLY}.items() if e.argument]
    assert needing_argument, "no entry takes an argument, so this test is vacuous"
    for name, argument in needing_argument:
        for blank in ("", "   ", None):
            with pytest.raises(MissingArgument) as raised:
                call_for(name, blank, "HotWallet1111111111111111111111111111111111")
            assert argument in str(raised.value), (
                f"{name}'s refusal does not name the argument it needs ({argument})"
            )


def test_the_three_refusals_are_THREE_DIFFERENT_SENTENCES():
    no_equivalent = refuse_without_equivalent("getrawmempool")
    assert "NO Solana equivalent" in no_equivalent
    assert "there is no mempool" in no_equivalent, "the reason does not say WHY there is none"
    assert "lastValidBlockHeight" in no_equivalent, (
        "the reason does not point at the question the operator probably meant"
    )

    unmapped = refuse_without_equivalent("getblahinfo")
    assert "not mapped" in unmapped
    assert "NO Solana equivalent" not in unmapped
    assert "NO_EQUIVALENT" in unmapped, "it does not say where the stronger claim is recorded"

    for junk in ("", None, 7, []):
        assert "is not a method name" in refuse_without_equivalent(junk)

    assert refuse_without_equivalent("getblockcount") == ""
    assert refuse_without_equivalent("getHealth") == "", "a Solana-only read must not be refused"


def test_every_NO_EQUIVALENT_reason_says_what_the_chain_has_INSTEAD():
    """A dead end is a dead end; a redirection is useful.

    Most of these are questions where the operator wanted something that DOES exist -- a
    balance instead of listunspent, getSupply instead of gettxoutsetinfo, a commitment instead
    of a difficulty. The reason is where that redirection lives.
    """
    # NO CASE ASSERTION HERE, and the XRP file's equivalent one is a style rule rather than a
    # property: it demanded a lowercase first letter so the reason reads as a clause after
    # "<name> has NO equivalent:" -- and it refused "Solana has no script system", which reads
    # correctly there because the first word is the chain's name. The thing worth holding is
    # that the reason EXPLAINS and REDIRECTS, which the length check and the specific
    # assertions above it cover.
    for name, reason in sorted(NO_EQUIVALENT.items()):
        assert len(reason) > 80, f"{name}'s reason is too short to explain anything: {reason!r}"
        assert reason.rstrip().endswith("."), f"{name}'s reason is not a finished sentence"


def test_the_deposit_address_question_is_recorded_as_THE_OPERATORS():
    """Not a gap in this map, and the file must not read as though it were.

    get_new_address() refuses on Solana because how a deposit is ATTRIBUTED -- a shared account
    with a memo, or an address per swap -- is a custody decision. The listaddressgroupings entry
    is where a reader looking for "how do I give a customer a SOL deposit address" arrives, so
    it is where that has to be said.
    """
    reason = NO_EQUIVALENT["listaddressgroupings"]
    assert "UNDECIDED" in reason and "operator's choice" in reason
    assert "get_new_address() refuses" in reason, "it does not say what the code actually does"


def test_equivalent_of_and_call_for_agree_about_what_is_translatable():
    """One question, one answer, whichever door the caller came through (rule 8)."""
    assert equivalent_of("getrawmempool") is None
    assert equivalent_of("getblahinfo") is None
    assert equivalent_of(None) is None
    with pytest.raises(ValueError, match="NO Solana equivalent") as raised:
        call_for("getrawmempool")
    assert str(raised.value) == refuse_without_equivalent("getrawmempool")


def test_no_entry_carries_an_empty_answers_path():
    for name, entry in sorted({**CONGRUENT, **NATIVE_ONLY}.items()):
        assert entry.answers, f"{name} names no path into the result, so the reader gets a document"
        assert entry.method, f"{name} names no Solana method"


def test_every_read_that_RETURNS_TRANSACTIONS_declares_the_version_it_can_parse():
    """The defect the operator's first real getBlock found, 2026-09-30.

        -32015  Transaction version (0) is not supported by the requesting client. Please try
                the request again with the following configuration parameter:
                "maxSupportedTransactionVersion": 0

    A node will not hand a block to a client that has not said which transaction versions it
    understands, and it refuses the WHOLE BLOCK rather than omitting the versioned transactions
    in it. Versioned transactions have been ordinary on Solana since 2022, so getBlock and
    getTransaction were broken for every input -- and no test here would ever have shown it,
    because the tests exercise the map and the map was internally consistent with itself.

    THAT IS THIS FILE'S OWN LIMIT, WRITTEN DOWN: a map tested only against its own tables is
    tested for self-consistency, not for correctness against a chain. The run is the proof, and
    this test exists to keep a proven fact from being un-proven by a later edit.

    MUTATION: drop the returns_transactions append. This fails naming the method; nothing else
    in the suite does.
    """
    needs_version = {"getblock", "getblockhash", "getrawtransaction"}
    # THE OPTION NAME IS DERIVED, NOT SPELLED, for two reasons. The literal is 30 characters of
    # base58-legal text and tests/test_address_literals_are_valid.py flags one of those unless
    # it is a dict key -- correctly, because nothing here pays money to a key. And reading it
    # out of what call_for() produces means this test cannot disagree with the module about the
    # name: a rename finds the new one and every assertion below still bites.
    _method, sample = call_for("getblock", 1)
    version_key = next((k for k in sample[-1] if k != "commitment"), None)
    assert version_key, "getBlock sends no option beside the commitment, so there is none to check"
    for name, entry in sorted({**CONGRUENT, **NATIVE_ONLY}.items()):
        address = "HotWallet1111111111111111111111111111111111" if entry.needs_address else ""
        argument = 1 if entry.argument else None
        _method, params = call_for(name, argument, address)
        options = params[-1] if params and isinstance(params[-1], dict) else {}
        declared = version_key in options

        if name in needs_version:
            assert entry.returns_transactions, f"{name} returns transactions and does not say so"
            assert declared, (
                f"{name} translates to `{entry.method}`, which returns transactions, and does "
                f"not declare maxSupportedTransactionVersion -- the node refuses the whole "
                f"answer with -32015"
            )
            assert options[version_key] == MAX_SUPPORTED_TRANSACTION_VERSION
        else:
            assert not entry.returns_transactions, (
                f"{name} claims to return transactions; if that is true it belongs in the set "
                f"above, and if not the flag is wrong"
            )
            assert not declared, (
                f"{name} declares a transaction version and returns no transactions. Sending an "
                f"option a method does not accept is an invalid-params error, not a harmless "
                f"extra"
            )


def test_the_version_option_rides_in_the_SAME_options_object_as_the_commitment():
    """ONE options object, because Solana's params are positional and it takes one.

    Appending a second dict would be an extra positional argument where the method expects
    none -- which the node reports as invalid params, saying nothing about the real mistake.
    """
    _method, params = call_for("getblock", 493267002)
    dicts = [p for p in params if isinstance(p, dict)]
    version_key = next(k for k in dicts[0] if k != "commitment")
    assert len(dicts) == 1, f"getBlock was sent {len(dicts)} options objects, not one: {params}"
    assert dicts[0] == {"commitment": FINALIZED,
                        version_key: MAX_SUPPORTED_TRANSACTION_VERSION}
    assert params[0] == 493267002, "the slot must still come first"
