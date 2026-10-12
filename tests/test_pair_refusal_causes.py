#!/usr/bin/env python3
"""Every condition that can refuse a pair must be NAMED when it is the one refusing.

Role: tests (read-only)
Reads: services/pair_view.REFUSAL_KEYS, VERDICT_SUMMARY_KEYS, pair_serviceability()
       and refusals_by_cause(); open_swap.blocked_by(). Rows are seeded dicts; the
       one real pair_serviceability() call is made against an EMPTY adapters dict,
       which short-circuits on its first condition and opens no socket.
Writes: nothing
Can move funds: no
Mainnet-safe: yes

WHAT THIS FILE IS FOR, AND IT IS A GATE RATHER THAN A DESCRIPTION.

open_swap.blocked_by() turns the pair rows into the sentence an operator reads when
a pair they asked for is unavailable. It spelled THREE of the conditions by hand and
carried a fallback -- "Cause NOT ESTABLISHED ... this is a defect in pair_catalog()"
-- for a cause it did not know. That fallback was the right instinct and it was
load-bearing four times:

    cannot_take       2026-10-01   caught by a test run the day it was added
    cannot_quote      2026-10-02   never caught; shipped unnamed
    cannot_establish  2026-10-03   never caught; shipped unnamed
    unreachable       2026-10-11   never caught; shipped unnamed, by me

MEASURED 2026-10-12 against the version of blocked_by() at commit 73d38be, by
exec'ing that function alone out of `git show HEAD:open_swap.py` and handing it one
row carrying exactly one refusal:

    unreachable only         -> Cause NOT ESTABLISHED -- run swap_readiness.py; ...
    cannot_quote only        -> Cause NOT ESTABLISHED -- run swap_readiness.py; ...
    cannot_establish only    -> Cause NOT ESTABLISHED -- run swap_readiness.py; ...

Three of the six conditions, unnamed. That is not hypothetical on the operator's
host: on 2026-10-11 LTC and GRC both stopped answering, so `unreachable` was the
condition refusing most of ALLOWED_PAIRS, and the tool written to replace the web
page would have told them the tool was broken rather than that a daemon was down.

THE FIX WAS NOT A FOURTH HAND-EDIT. Rule 19's test for a patch is whether the cause
stops existing: the cause was a SECOND list of the conditions, kept in a different
file from the function that evaluates them, so there is one list now
(services/pair_view.REFUSAL_KEYS) and refusals_by_cause() iterates it.

WHICH MEANS THE REAL GATE IS EXHAUSTIVENESS, and it is the first test below. A
seventh condition added to pair_serviceability() and not to REFUSAL_KEYS fails
test_the_verdict_dict_holds_exactly_the_named_keys, which is the only shape of check
that catches the thing that went wrong four times. Asserting that the six we know
about are handled would pass on the day a seventh is added -- which is exactly what
the three test files that already cover pair_serviceability() did.

WHY THE ROWS ARE SEEDED AND NOT PRODUCED BY SIX REAL POSTURES. Each condition needs
a whole posture to provoke -- an unconfigured chain, a stale reachability row, an
unarmed payout leg, an unset deposit account, a missing fee reserve, an unreachable
Solana cluster -- and four of those six already have a file that builds the posture
and asserts the sentence (tests/test_xrp_pair_reasons.py,
tests/test_allowed_pairs_are_serviceable.py, tests/test_chain_reachability.py,
tests/test_an_available_pair_can_actually_be_quoted.py). What is NOT covered
anywhere, and is the entire defect, is the REPORTING step: given a verdict that
refuses for reason R, does the sentence name R. That step takes a row and returns a
string, so a row is the honest input, and the exhaustiveness test above is what
keeps the seeded row shape tied to the real one.
"""

from __future__ import annotations

import pytest
from services.pair_view import (
    REFUSAL_KEYS,
    VERDICT_SUMMARY_KEYS,
    pair_serviceability,
    refusals_by_cause,
)

from open_swap import blocked_by

#: The sentence blocked_by() prints when it recognizes no cause. Spelled once here;
#: every test below asserts on this constant rather than on a substring of its own,
#: so rewording the fallback cannot quietly make these tests stop checking for it.
NOT_ESTABLISHED = "Cause NOT ESTABLISHED"

#: One distinct sentence per prose condition, so a test can tell WHICH key was
#: reported rather than only that something was. `missing` is absent: it is a list of
#: asset names rather than a sentence, which is the one asymmetry in REFUSAL_KEYS and
#: the reason refusals_by_cause() special-cases it.
PROSE_CAUSE = {
    "unreachable": "LTC did not answer the last time a worker asked",
    "cannot_pay": "GRC cannot pay out: GRIDCOIN_WALLET_PASSPHRASE is unset",
    "cannot_take": "XRP cannot take deposits: XRP_DEPOSIT_ACCOUNT is unset",
    "cannot_quote": "no payout cost has been recorded for GRC",
    "cannot_establish": "the SOL cluster could not be asked for its rent-exempt minimum",
}


def _row(**refusals) -> dict:
    """One pair row refusing for exactly the named reasons and nothing else.

    EVERY KEY IN REFUSAL_KEYS IS PRESENT, empty where it is not under test, because
    an absent key and an empty one are different inputs and the production rows
    always carry all six. A test seeding only the key it cares about would pass
    against a refusals_by_cause() that read `row[key]` directly and crash in
    production on a row shape it never saw.
    """
    row = {key: [] if key == "missing" else "" for key in REFUSAL_KEYS}
    row.update(refusals)
    return row


def test_the_verdict_dict_holds_exactly_the_named_keys():
    """THE GATE. A seventh condition fails here rather than reaching an operator.

    Called for real rather than asserted from the source, with an empty adapters
    dict: `missing` refuses first, every later condition short-circuits, and nothing
    opens a socket. The KEYS are what is under measurement and they do not depend on
    which condition won.
    """
    verdict = pair_serviceability({"ALLOWED_PAIRS": {("BTC", "LTC")}, "RPC": {}}, {}, "BTC", "LTC")
    assert set(verdict) == set(REFUSAL_KEYS) | set(VERDICT_SUMMARY_KEYS), (
        "pair_serviceability() returned a key that is neither a named refusal nor a "
        "summary. If it is a new CONDITION, add it to services/pair_view.REFUSAL_KEYS "
        "-- refusals_by_cause() and open_swap.blocked_by() then report it with no "
        "further edit. Leaving it out is how cannot_quote, cannot_establish and "
        "unreachable each shipped unnamed; see this file's header."
    )


def test_refusal_keys_has_no_duplicates_and_leads_with_missing():
    """The list's own shape, because refusals_by_cause() depends on both facts.

    `missing` must be FIRST because it is the only non-prose entry and the only one
    pair_serviceability() uses to short-circuit everything else; a duplicate would
    make a cause print twice, which reads as two problems.
    """
    assert len(set(REFUSAL_KEYS)) == len(REFUSAL_KEYS)
    assert REFUSAL_KEYS[0] == "missing"
    assert set(PROSE_CAUSE) == set(REFUSAL_KEYS) - {"missing"}, (
        "this file's PROSE_CAUSE table must cover every prose condition, or the "
        "parametrised test below silently stops testing one of them"
    )


@pytest.mark.parametrize("key", sorted(PROSE_CAUSE))
def test_every_prose_condition_is_named_when_it_is_the_only_one_refusing(key):
    """The authority's OWN sentence reaches the output, verbatim, for each of the five.

    Verbatim and not paraphrased: each sentence is built by the module that refused,
    so that module stays the one place that knows why (rule 8). A summary written
    here would be a second copy and would drift the first time a variable is renamed.
    """
    sentence = PROSE_CAUSE[key]
    assert refusals_by_cause([_row(**{key: sentence})]) == [sentence]


@pytest.mark.parametrize("key", sorted(PROSE_CAUSE))
def test_blocked_by_names_each_prose_condition_rather_than_calling_itself_defective(key):
    """THE INVERTED DEFECT ASSERTION. Three of these five printed NOT ESTABLISHED.

    Measured at commit 73d38be for `unreachable`, `cannot_quote` and
    `cannot_establish` -- the exact three blocked_by()'s hand-written list never
    learned. The other two passed there and are asserted here anyway, because what
    failed was the LIST and a test that covers only the broken half cannot tell a
    fixed list from a longer hand-written one.
    """
    sentence = PROSE_CAUSE[key]
    printed = blocked_by([_row(**{key: sentence})])
    assert NOT_ESTABLISHED not in printed
    assert sentence in printed
    assert printed.startswith("Blocked by: ")


def test_a_chain_with_no_adapter_is_named_by_asset_and_not_by_sentence():
    """`missing` is a LIST, which is why it is special-cased.

    The remedy is deliberately not restated -- create_swap()'s refusal names the
    exact variables and /admin carries why_unconfigured() in full -- so this asserts
    the asset names reach the line and that the grammar agrees with the count.
    """
    one = refusals_by_cause([_row(missing=["LTC"])])
    assert one == ["LTC has no adapter in this process"]
    two = refusals_by_cause([_row(missing=["LTC", "GRC"])])
    assert two == ["GRC, LTC have no adapter in this process"]


def test_one_cause_shared_by_six_pairs_is_named_once():
    """Deduplicated, because printing one problem six times reads as six problems.

    This is the operator's own 2026-10-11 shape: GRC stopped answering, and GRC is a
    leg of six directions in ALLOWED_PAIRS, so the same sentence arrived on six rows.
    """
    sentence = PROSE_CAUSE["unreachable"]
    rows = [_row(unreachable=sentence) for _ in range(6)]
    assert refusals_by_cause(rows) == [sentence]


def test_causes_are_reported_in_the_order_the_conditions_are_evaluated():
    """REFUSAL_KEYS' order, which is cheapest-refusal-first.

    So a reader sees the configuration causes before the network ones. Asserted
    against REFUSAL_KEYS rather than against a hand-written expected list, which is
    the same reason the list exists at all.
    """
    rows = [_row(missing=["BTC"])] + [_row(**{key: text}) for key, text in PROSE_CAUSE.items()]
    reported = refusals_by_cause(rows)
    expected = ["BTC has no adapter in this process"] + [
        PROSE_CAUSE[key] for key in REFUSAL_KEYS if key != "missing"
    ]
    assert reported == expected


def test_a_row_refusing_for_nothing_still_reaches_the_fallback():
    """The fallback is KEPT, not replaced, and it must still be reachable.

    refusals_by_cause() returning [] cannot distinguish "no rows" from "a row with no
    refusal in it"; blocked_by() is the caller that knows it filtered to unavailable
    pairs, so the defect sentence stays there. A row that is unavailable with no key
    set is a bug, and an absent explanation that names itself as one is better than an
    invented explanation.
    """
    assert NOT_ESTABLISHED in blocked_by([_row()])
    assert NOT_ESTABLISHED in blocked_by([])
