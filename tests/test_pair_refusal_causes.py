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

import sqlite3

import pytest
from db import init_db
from services.admin_view import pair_rows
from services.chain_reachability import reachability_rows
from services.helpers import utc_now_iso
from services.pair_view import (
    REFUSAL_KEYS,
    VERDICT_SUMMARY_KEYS,
    allowed_pair_rows,
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


# --- THE ROW SHAPE, which is where today's defect actually lived ---------------


class _Payer:
    """An adapter that is reachable and can pay, so only `unreachable` can refuse.

    Minimal on purpose: `unreachable` is the SECOND condition and short-circuits the
    four below it, so nothing past can_spend is ever asked of this object. A richer
    stub would be measuring the stub.
    """

    can_spend = True
    payout_refusal = ""


def _fresh(tmp_path, *assets_that_did_not_answer):
    """Reachability rows for the named chains, THROUGH THE REAL READER.

    THE TIMESTAMP IS GENERATED, NOT TYPED. A literal goes stale: services/
    chain_reachability.reachability_refusal() falls silent past STALE_AFTER_SECONDS, so
    a hardcoded date would make every assertion below pass vacuously the day after it
    was written -- which is a mistake this session has already made once, in a fixture
    that was fresh for ten minutes and then silently stopped gating.

    AND IT GOES THROUGH THE DATABASE AND reachability_rows() RATHER THAN BUILDING THE
    DICT. The first version of this helper returned a hand-made dict, which is what let
    the dedup assertion below fail for the right reason and nearly be "fixed" in the
    fixture: reachability_rows() is where `age_seconds` is stamped from one clock
    reading, and that stamping is the thing that makes one down chain produce ONE cause
    sentence instead of one per pair. A fixture that supplied its own ages would have
    measured the fixture (BEHAVIORAL_VERIFICATION_PRINCIPLE -- run the real thing).
    """
    path = tmp_path / "swap_terminal.db"
    if not path.exists():
        init_db(str(path))
    now = utc_now_iso()
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    with conn:
        for asset in assets_that_did_not_answer:
            conn.execute(
                "INSERT OR REPLACE INTO chain_reachability "
                "(asset, reachable, asked, detail, checked_at) VALUES (?, 0, ?, ?, ?)",
                (asset, "get_balance", "[Errno 111] Connection refused", now),
            )
    try:
        return reachability_rows(conn)
    finally:
        conn.close()


def test_the_row_builder_carries_every_refusal_key():
    """THE GATE FOR TODAY'S DEFECT, and the one the first version of this file lacked.

    test_the_verdict_dict_holds_exactly_the_named_keys above checks what
    pair_serviceability() RETURNS. allowed_pair_rows() then COPIES those keys onto a
    row, and until 2026-10-12 it copied five of six by hand -- so the verdict computed
    `unreachable`, the row dropped it, and every consumer reading the keys was blind to
    the condition that was refusing fourteen of thirty pairs on the operator's host:

        GRC -> LTC   blocked by NOTHING NAMED
        unreachable    0 of 30      <- FALSE; it was the actual cause

    The earlier gate passed throughout, because the row builder is a SECOND list and it
    asked the first. That is the lesson worth the test: merging one of three copies
    produces a fix that measurably does nothing, and the count of copies is what has to
    be established before a merge is claimed (rule 17, applied to one's own fix).
    """
    rows = allowed_pair_rows({"ALLOWED_PAIRS": {("BTC", "LTC")}, "RPC": {}}, {})

    assert rows, "ALLOWED_PAIRS was seeded, so a row must come back"
    for row in rows:
        missing_keys = sorted(set(REFUSAL_KEYS) - set(row))
        assert not missing_keys, (
            f"allowed_pair_rows() dropped {missing_keys} from its row. Every consumer that "
            f"reads the keys -- open_swap.blocked_by(), pair_verdicts.py, templates/"
            f"admin.html's cross-check -- then cannot see that condition, while `reason` "
            f"stays correct and hides it."
        )


def test_an_unreachable_leg_is_named_all_the_way_through_to_the_cause(tmp_path):
    """End to end: a chain that did not answer reaches the operator as a sentence.

    This is the path that was broken in the middle. pair_serviceability() refused
    correctly, `reason` carried the sentence correctly, and the three steps in between
    -- row, condition key, grouped cause -- lost it. So the assertion walks all of them
    rather than any one.
    """
    config = {"ALLOWED_PAIRS": {("GRC", "LTC"), ("LTC", "GRC")}, "RPC": {}}
    adapters = {"GRC": _Payer(), "LTC": _Payer()}
    rows = allowed_pair_rows(config, adapters, _fresh(tmp_path, "GRC"))

    assert len(rows) == 2
    for row in rows:
        assert not row["serviceable"], "GRC is a leg of both pairs and it did not answer"
        assert row["unreachable"], "the row must name the condition, not only the reason"
        assert not row["missing"], "GRC has an adapter here; this is not the missing case"

    causes = refusals_by_cause(rows)
    assert len(causes) == 1, "both pairs share one down chain, so one cause (deduplicated)"
    assert "GRC" in causes[0]
    printed = blocked_by(rows)
    assert NOT_ESTABLISHED not in printed
    assert "GRC" in printed


def test_the_operator_page_and_the_customer_page_refuse_the_same_pair(tmp_path):
    """THE DISAGREEMENT services/pair_view.py EXISTS TO PREVENT, measured both ways.

    From 2026-10-11 to 2026-10-12 admin_view.pair_rows() called pair_serviceability()
    WITHOUT the reachability rows while routes/atm.py passed them. Sharing the function
    was not enough: the two callers handed it different arguments, so with GRC down the
    customer page refused GRC -> LTC and /admin called it ENABLED, in one process, from
    one config -- verbatim the 2026-10-02 defect that module's header records.

    Both surfaces are asked here, with the same three inputs, and the verdicts are
    compared to each other rather than to a hand-written expectation: what must hold is
    that they AGREE, and an expectation written here would be a third opinion.
    """
    config = {"ALLOWED_PAIRS": {("GRC", "LTC"), ("LTC", "GRC")}, "RPC": {}}
    adapters = {"GRC": _Payer(), "LTC": _Payer()}
    reachability = _fresh(tmp_path, "GRC")

    customer = {row["label"]: row["serviceable"] for row in allowed_pair_rows(config, adapters, reachability)}
    operator = {
        row["label"]: row["serviceable"]
        for row in pair_rows(config, adapters, reachability)
        if row["label"] in customer
    }

    assert customer == {"GRC -> LTC": False, "LTC -> GRC": False}
    assert operator == customer, (
        "/admin and / must reach the same verdict for the same pair from the same inputs; "
        "a difference here is the two-surface disagreement pair_serviceability() was "
        "extracted to make impossible"
    )

    unreachable_states = {
        row["label"]: (row["state"], row["short_detail"])
        for row in pair_rows(config, adapters, reachability)
        if row["label"] in customer
    }
    for label, (state, short_detail) in sorted(unreachable_states.items()):
        assert state == "unreachable", f"{label} should be in the unreachable state, not {state!r}"
        # AND IT MUST NOT SAY "no adapter". The two ways to be unreachable are different
        # work -- export a variable and restart, versus go and start a daemon -- and the
        # pill naming the wrong one sends the operator to the wrong place.
        assert "did not answer" in short_detail, short_detail
        assert "unreachable here" not in short_detail, (
            f"{label} has adapters for both legs; the pill must not claim a missing one"
        )
