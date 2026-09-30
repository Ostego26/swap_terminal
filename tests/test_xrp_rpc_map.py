"""The XRP Ledger command map: its coverage, and the property that makes it safe.

WHAT MATTERS HERE, in order.

THE MAP IS THE ALLOWLIST ON THE XRP TAB. The bitcoin-style tabs are gated by
regtest/operator_panel.READ_ONLY_RPCS; the XRP console cannot be, because `fee`,
`server_state` and the account_* reads are not bitcoin method names and do not belong
on a bitcoind allowlist. So the safety argument moves here: EVERY rippled method this
module can emit must be a read. That is one test, and it is the important one.

COVERAGE IS AN EXACT PARTITION, not a subset. Every name on READ_ONLY_RPCS either
translates or is recorded as having no equivalent, with no name in both tables and
none in neither -- because a name in neither is the case where the console says "not
mapped" for something that in fact has an answer, and nobody would ever find out.

AND THE REFUSALS ARE THREE THINGS, not one. "No equivalent exists", "this needs an
argument" and "I do not know that name" send an operator in three different
directions, and collapsing them is the failure this repo keeps paying for.
"""

from __future__ import annotations

import pytest
from chains.xrp_rpc_map import (
    CONGRUENT,
    DROPS_PER_XRP,
    NO_EQUIVALENT,
    XRP_ONLY,
    MissingArgument,
    equivalent_of,
    refuse_without_equivalent,
    xrp_call_for,
)
from regtest.operator_panel import READ_ONLY_RPCS, xrp_console_methods

#: EVERY rippled method that signs, submits, proposes a key or changes server state.
#:
#: A DENYLIST here and an allowlist in the module, on purpose: the module's tables ARE
#: the allowlist, so what this file adds is an independent statement of what must
#: never appear in them. A test that re-derived the allowlist from the tables would
#: assert that the tables equal themselves.
#:
#: `sign`, `sign_for` and `wallet_propose` are the ones that take or return a SECRET.
#: `submit` and `submit_multisigned` broadcast. The four admin verbs change what the
#: server does. None of them is a read and none may ever be reachable from a browser
#: pointed at this panel.
WRITES_OR_SECRETS = frozenset({
    "sign", "sign_for", "submit", "submit_multisigned", "wallet_propose", "wallet_seed",
    "channel_authorize", "sign_and_submit",
    "stop", "connect", "ledger_accept", "validation_create", "can_delete", "logrotate",
})


def test_EVERY_METHOD_THIS_MAP_CAN_EMIT_IS_A_READ():
    """THE SAFETY PROPERTY. The map is the allowlist, so this is what holds it.

    MUTATION: add {"submitpayment": XRPEquivalent("submit", "engine_result")} to
    CONGRUENT. Nothing else in the suite fails -- the panel's own READ_ONLY_RPCS never
    sees an XRP request, refuse_unless_read_only() is not on that path, and the
    console would happily broadcast. This is the only test standing there.
    """
    emitted = {entry.method for entry in (*CONGRUENT.values(), *XRP_ONLY.values())}
    assert emitted, "the map emits no method at all, so this test is proving nothing"
    forbidden = sorted(emitted & WRITES_OR_SECRETS)
    assert not forbidden, (
        f"the XRP command map can emit {forbidden}, which sign, submit or change server state. "
        f"This map IS the allowlist on the XRP tab -- nothing downstream re-checks it"
    )
    # AND NOTHING THAT MERELY LOOKS LIKE A READ. A method whose name contains sign or
    # submit is refused by shape as well as by name, because the denylist above can
    # only ever list what somebody thought of.
    for method in sorted(emitted):
        assert "sign" not in method and "submit" not in method and "propose" not in method, (
            f"{method!r} is in the map and its NAME says it writes"
        )


def test_the_allowlist_is_an_EXACT_PARTITION_of_the_panels_own():
    """Every bitcoin-style name translates, or is recorded as untranslatable. No third state.

    A name in NEITHER table is the expensive case: the console answers "not mapped for
    the XRP Ledger", which is true and is not the same as "has no equivalent" -- so an
    operator stops asking about something that does have an answer, and no test would
    ever have told them.

    MUTATION: delete any single key from either table. This names it.
    """
    allowlist = set(READ_ONLY_RPCS)
    translated, impossible = set(CONGRUENT), set(NO_EQUIVALENT)

    assert not (translated & impossible), (
        f"{sorted(translated & impossible)} appear in BOTH tables, so the module contradicts "
        f"itself about whether an equivalent exists"
    )
    unaccounted = sorted(allowlist - translated - impossible)
    assert not unaccounted, (
        f"{unaccounted} are on the panel's read-only allowlist and in neither XRP table. The "
        f"console will say 'not mapped', which is a different claim from 'no equivalent exists' "
        f"-- decide which and record it"
    )
    invented = sorted((translated | impossible) - allowlist)
    assert not invented, (
        f"{invented} are mapped as bitcoin-style names but are not on READ_ONLY_RPCS, so the "
        f"bitcoin tabs would refuse them. An XRPL-only read belongs in XRP_ONLY"
    )


def test_XRP_ONLY_holds_no_bitcoin_name_and_the_console_offers_both_halves():
    """XRP_ONLY is for reads Bitcoin has no NAME for; a name it does have belongs above."""
    overlap = sorted(set(XRP_ONLY) & (set(READ_ONLY_RPCS) | set(CONGRUENT) | set(NO_EQUIVALENT)))
    assert not overlap, f"{overlap} are in XRP_ONLY and are also bitcoin-style names"

    offered = xrp_console_methods()
    assert set(offered) == set(CONGRUENT) | set(XRP_ONLY)
    assert len(offered) == len(set(offered)), "the console offers a method twice"
    # Bitcoin-style names first, then the XRPL-only reads -- two questions, two halves.
    assert offered[:len(CONGRUENT)] == sorted(CONGRUENT)
    assert offered[len(CONGRUENT):] == sorted(XRP_ONLY)


def test_a_call_that_needs_an_ACCOUNT_refuses_rather_than_defaulting_to_one():
    """rippled has no wallet to default to, and guessing would answer about the wrong account.

    MUTATION: default the account to "" and send it. rippled replies actNotFound or
    invalidParams, which reads like the ACCOUNT is missing rather than like the request
    was -- so the operator goes looking at the ledger for an account that was never
    named.
    """
    needing = [name for name, entry in {**CONGRUENT, **XRP_ONLY}.items() if entry.needs_account]
    assert needing, "no entry needs an account, so this test is vacuous"
    for name in needing:
        for account in ("", "   ", None, 7):
            with pytest.raises(MissingArgument) as raised:
                xrp_call_for(name, None, account)
            assert "needs one" in str(raised.value) or "needs" in str(raised.value)
        _method, params = xrp_call_for(name, None, "  rHotWallet  ")
        assert params["account"] == "rHotWallet", "the account was not stripped"
        # VALIDATED, NOT CURRENT. A balance read from an unvalidated ledger can still
        # change, and a payout sized against one is sized against a guess.
        assert params["ledger_index"] == "validated", (
            f"{name} does not ask for the validated ledger, so it can answer from one that is "
            f"still able to change"
        )


def test_a_call_that_needs_an_ARGUMENT_refuses_rather_than_asking_about_the_wrong_ledger():
    """`ledger` with no index answers about the CURRENT ledger -- confidently, and wrongly.

    This is rule 17's shape in one API call: a default that produces a plausible answer
    to a question nobody asked.
    """
    needing = [(name, entry.argument) for name, entry in CONGRUENT.items() if entry.argument]
    assert needing, "no entry takes an argument, so this test is vacuous"
    for name, argument in needing:
        for blank in ("", "   ", None):
            with pytest.raises(MissingArgument) as raised:
                xrp_call_for(name, blank)
            assert argument in str(raised.value), (
                f"{name}'s refusal does not name the argument it needs ({argument})"
            )
        _method, params = xrp_call_for(name, " VALUE ")
        assert params[argument] == "VALUE"


def test_the_three_refusals_are_THREE_DIFFERENT_SENTENCES():
    """No equivalent, not mapped, and not a name: three directions for the operator.

    Collapsing the first two would send somebody hunting for a `getdifficulty`
    equivalent that cannot exist. Collapsing the second two would tell them a typo is
    a property of the chain.
    """
    no_equivalent = refuse_without_equivalent("getdifficulty")
    assert "NO XRP Ledger equivalent" in no_equivalent
    assert "no proof of work" in no_equivalent, "the reason does not say WHY there is none"

    unmapped = refuse_without_equivalent("getblahinfo")
    assert "not mapped" in unmapped
    assert "NO XRP Ledger equivalent" not in unmapped, (
        "an unmapped name was reported as a chain that cannot answer, which stops the operator "
        "looking for something that may well exist"
    )
    assert "NO_EQUIVALENT" in unmapped, "it does not say where the stronger claim is recorded"

    for junk in ("", None, 7, []):
        assert "is not a method name" in refuse_without_equivalent(junk)

    assert refuse_without_equivalent("getblockcount") == ""
    assert refuse_without_equivalent("fee") == "", "an XRPL-only read must not be refused"


def test_every_NO_EQUIVALENT_reason_says_what_the_chain_has_INSTEAD():
    """A dead end is a dead end; a redirection is useful.

    Each of these is a question with no answer on this chain, and for most of them the
    operator wanted something that DOES exist -- a balance instead of listunspent, a
    queue depth instead of getrawmempool. The reason is where that redirection lives,
    and a bare "not supported" would waste it.
    """
    for name, reason in sorted(NO_EQUIVALENT.items()):
        assert len(reason) > 80, f"{name}'s reason is too short to explain anything: {reason!r}"
        assert reason[0].islower(), f"{name}'s reason should read as a clause, not a sentence"
        assert not reason.endswith(".."), f"{name}'s reason is punctuated oddly"


def test_getblockcounts_note_names_the_VALIDATED_index_because_a_payout_depends_on_it():
    """The one note whose absence could cost money.

    `ledger_closed` is the congruent answer to getblockcount and it is NOT the figure a
    payout's depth may be measured against: a closed ledger can still change, a
    validated one cannot. An operator who reads ledger_index off this call and treats
    it as final has the same mistake Bitcoin's confirmations exist to prevent -- and
    XRP_MIN_CONFIRMATIONS is 1 precisely because the validated ledger is final.
    """
    note = CONGRUENT["getblockcount"].note
    assert "validated_ledger.seq" in note, "the note does not name where the final index is"
    assert "NOT NECESSARILY VALIDATED" in note


def test_the_balance_note_names_DROPS_and_the_reserve():
    """Two ways to misread account_info.account_data.Balance, both expensive.

    It is in drops, so it looks like a fortune; and part of it is reserved, so it is not
    all spendable. Bitcoin's getbalance has no analogue of either.
    """
    note = CONGRUENT["getbalance"].note
    assert "DROPS" in note and f"{DROPS_PER_XRP:,}" in note, (
        "the note does not say the figure is in drops, or does not give the divisor"
    )
    assert "reserve" in note.lower(), "the note does not mention the unspendable reserve"


def test_equivalent_of_is_None_for_a_name_with_no_equivalent_AND_for_junk():
    """It answers one question -- is there a translation -- and leaves the distinction to the refusal."""
    assert equivalent_of("getdifficulty") is None
    assert equivalent_of("getblahinfo") is None
    assert equivalent_of(None) is None
    assert equivalent_of(7) is None
    assert equivalent_of("getblockcount").method == "ledger_closed"
    assert equivalent_of("fee").method == "fee"


def test_xrp_call_for_raises_the_refusal_TEXT_for_an_untranslatable_name():
    """The caller gets the same sentence whichever door it came through (rule 8)."""
    with pytest.raises(ValueError, match="NO XRP Ledger equivalent") as raised:
        xrp_call_for("getdifficulty")
    assert str(raised.value) == refuse_without_equivalent("getdifficulty")


def test_no_entry_carries_an_empty_answers_path():
    """`answers` is what makes a translated figure checkable, so it is never blank."""
    for name, entry in sorted({**CONGRUENT, **XRP_ONLY}.items()):
        assert entry.answers, f"{name} names no path into the result, so the reader gets a document"
        assert entry.method, f"{name} names no rippled method"
