#!/usr/bin/env python3
"""The escrow reclaim verdict, seeded, with the clock as an argument.

Role: tests (read-only)
Reads: chains/xrp_escrow and chains/xrp_units. No network, no database, no chain.
Writes: nothing
Can move funds: no
Mainnet-safe: yes

WHAT THIS PINS, AND WHY EACH ONE IS HERE RATHER THAN OBVIOUS.

The verdict exists because xrp_balances.py printed `CancelAfter=843784768` at the operator on
2026-09-29 and that escrow -- 1 XRP -- had been reclaimable since 2026-09-27T00:39:28Z. The
number was on the screen and the fact was not. So the first test below is that exact object with
a clock seeded to the moment it was printed: a regression on the only instance anybody has seen.

THE EPOCH TRAP IS THE REASON FOR THE SECOND SET. Ripple seconds and Unix seconds differ by
946,684,800 -- thirty years -- so reading one as the other never looks like a type error. It
looks like an escrow expiring in 1996 or in 2056, both of which a verdict would answer
confidently and wrongly. `843784768` read as Unix is 1996-09-26; read correctly it is
2026-09-27. A test that seeded `now` from the real clock would pass either way for the next
thirty years, which is why every clock here is a literal.

AND THE BOUNDARY, which is the case a live clock cannot reach on purpose: CancelAfter exactly
equal to now. cancel_verdict treats equality as reclaimable and its docstring says so; that is a
choice, and a choice nothing asserts is a choice that gets silently reversed by a later `>` for
looking tidier.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from chains import xrp_escrow
from chains.xrp_escrow import (
    CANCEL_NEEDS,
    NEVER_BY_TIME,
    NOT_YET,
    RECLAIMABLE,
    UNREADABLE,
    cancel_inputs,
    cancel_verdict,
    escrow_cancel_tx,
    offer_sequence_from,
    reclaimable,
)
from chains.xrp_units import RIPPLE_EPOCH_OFFSET_SECONDS, unix_from_ripple_time

#: The operator's real escrow, as `account_objects` rendered it on 2026-09-29, reduced to the
#: fields the verdict reads. `Amount` is drops, so this is 1 XRP.
THE_STRANDED_ESCROW = {
    "Account": "rTHEIRS",
    "Amount": "1000000",
    "CancelAfter": 843784768,
}

#: 2026-09-29T12:00:00Z, near enough to when the unreadable line was printed. A LITERAL, for the
#: reason the module docstring gives: a live clock makes the epoch bug invisible. (Written wrong
#: the first time -- 1790424000, which is BEFORE the CancelAfter -- and four tests failed saying
#: NOT_YET. Worth the comment: the literal that makes this test honest is also the one nothing
#: else in the file checks, so it is derived below rather than trusted.)
WHEN_IT_WAS_PRINTED = 1790683200.0


def test_the_stranded_escrow_reads_reclaimable_at_the_moment_it_was_printed():
    """The regression. This object, that clock, and the answer that was missing from the screen."""
    verdict = cancel_verdict(THE_STRANDED_ESCROW, WHEN_IT_WAS_PRINTED)
    assert verdict.state == RECLAIMABLE
    assert verdict.cancel_after_unix == 1790469568  # 2026-09-27T00:39:28Z
    # The clock is two days and change past it, and the sentence has to carry that -- a bare
    # RECLAIMABLE does not tell an operator whether this happened minutes or months ago.
    assert verdict.cancel_after_unix < WHEN_IT_WAS_PRINTED
    assert "213632s ago" in verdict.reason


def test_the_cancel_after_is_ripple_seconds_and_not_unix_seconds():
    """Thirty years, and a wrong reading answers confidently instead of failing.

    843784768 as Unix seconds is 1996-09-26. The offset is the whole difference between "this
    was reclaimable two days ago" and "this expired before XRP existed", and either reading
    produces RECLAIMABLE -- so the state alone cannot catch it. The timestamp has to be asserted.
    """
    verdict = cancel_verdict(THE_STRANDED_ESCROW, WHEN_IT_WAS_PRINTED)
    assert verdict.cancel_after_unix == 843784768 + RIPPLE_EPOCH_OFFSET_SECONDS
    assert verdict.cancel_after_unix == unix_from_ripple_time(843784768)
    assert verdict.cancel_after_unix != 843784768


def test_equality_is_reclaimable_and_the_reason_says_zero_seconds():
    """The boundary a real clock cannot be aimed at.

    Seeded to CancelAfter exactly, the verdict is RECLAIMABLE and reports 0s waited. If somebody
    later tightens `>=` to `>` this flips to NOT_YET, which is the reversal this test exists to
    make loud rather than tidy.
    """
    at_the_instant = float(unix_from_ripple_time(843784768))
    verdict = cancel_verdict(THE_STRANDED_ESCROW, at_the_instant)
    assert verdict.state == RECLAIMABLE
    assert "0s ago" in verdict.reason

    one_second_early = cancel_verdict(THE_STRANDED_ESCROW, at_the_instant - 1)
    assert one_second_early.state == NOT_YET
    assert "1s away" in one_second_early.reason


def test_an_escrow_with_no_cancel_after_is_never_freed_by_waiting():
    """NOT_YET and NEVER_BY_TIME are the two the operator must not confuse.

    NOT_YET means come back later. NEVER_BY_TIME means later is not a plan -- there is no
    CancelAfter, so no amount of waiting makes an EscrowCancel valid. Collapsing them into one
    "cannot cancel yet" is how somebody ends up waiting on an escrow that no clock will release.
    """
    verdict = cancel_verdict({"Account": "rTHEIRS", "Amount": "1000000"}, WHEN_IT_WAS_PRINTED)
    assert verdict.state == NEVER_BY_TIME
    assert verdict.cancel_after_unix is None
    assert "no amount of waiting" in verdict.reason


@pytest.mark.parametrize(
    "escrow",
    [
        {"Account": "rTHEIRS", "CancelAfter": "soon"},
        {"Account": "rTHEIRS", "CancelAfter": "843784768x"},
        {"Account": "rTHEIRS", "CancelAfter": []},
        "not an object at all",
        None,
        1790469568,
    ],
)
def test_anything_it_cannot_read_says_so_instead_of_guessing(escrow):
    """UNREADABLE is a state, not an exception and not a default to NOT_YET.

    This is CLAUDE.md rule 12's BLE001 complaint in the small: a handler that turns "I could not
    parse this" into a value the caller cannot tell from a real answer. An escrow whose
    CancelAfter is a string must not read as an escrow that is merely not due yet.
    """
    verdict = cancel_verdict(escrow, WHEN_IT_WAS_PRINTED)
    assert verdict.state == UNREADABLE
    assert verdict.cancel_after_unix is None
    assert verdict.reason


def test_every_verdict_carries_where_the_money_would_go():
    """`returns_to` on all four states, because it is asked BEFORE the answer is yes.

    EscrowCancel has no destination field: the drops return to the escrow's own `Account`, never
    to whoever submits the cancel. That is the fact that makes reclaiming somebody else's escrow
    pointless-but-harmless, and it belongs on the not-yet verdict too -- an operator deciding
    whether to bother waiting needs it then, not afterwards.
    """
    due = cancel_verdict(THE_STRANDED_ESCROW, WHEN_IT_WAS_PRINTED)
    not_due = cancel_verdict(THE_STRANDED_ESCROW, 1.0)
    timeless = cancel_verdict({"Account": "rTHEIRS"}, WHEN_IT_WAS_PRINTED)
    assert due.returns_to == "rTHEIRS"
    assert not_due.returns_to == "rTHEIRS"
    assert timeless.returns_to == "rTHEIRS"
    assert "rTHEIRS" in due.reason


def test_an_escrow_with_no_account_says_it_cannot_say():
    """An empty `returns_to` would render as a blank where an address goes.

    Rule 14: never let an absent value print nothing. A missing `Account` is ambiguous between
    "no account" and "the renderer dropped it", so the verdict answers in words.
    """
    verdict = cancel_verdict({"CancelAfter": 843784768}, WHEN_IT_WAS_PRINTED)
    assert verdict.state == RECLAIMABLE
    assert verdict.returns_to
    assert "cannot be said" in verdict.returns_to


def test_reclaimable_returns_the_objects_and_not_a_count():
    """A count cannot answer "which one, and how much".

    The filter keeps the escrow beside its verdict for exactly that: the next question after
    "is anything reclaimable" is always the amount, which lives on the object and not the verdict.
    """
    not_due = {"Account": "rMINE", "Amount": "5000000", "CancelAfter": 999999999}
    timeless = {"Account": "rMINE", "Amount": "7000000"}
    pairs = reclaimable([THE_STRANDED_ESCROW, not_due, timeless, "junk"], WHEN_IT_WAS_PRINTED)
    assert len(pairs) == 1
    escrow, verdict = pairs[0]
    assert escrow is THE_STRANDED_ESCROW
    assert escrow["Amount"] == "1000000"
    assert verdict.state == RECLAIMABLE


@pytest.mark.parametrize("empty", [None, [], ()])
def test_reclaimable_over_nothing_is_an_empty_list_and_not_an_error(empty):
    """An account with no escrows is the common case, not an edge case."""
    assert reclaimable(empty, WHEN_IT_WAS_PRINTED) == []


def test_nothing_in_this_module_can_submit_anything():
    """The module is a verdict, and its header claims it imports no transport.

    CHECKED FROM THE IMPORT STATEMENTS, NOT FROM THE TEXT. Four checks in this suite have
    already tripped on their own subject matter appearing in a comment or a docstring, and this
    module's docstring names `EscrowCancel` and `submit` repeatedly because explaining what it
    refuses to do is most of what the header is for. A substring scan would fail on the
    explanation. The AST sees only what is actually imported.

    If somebody wires a client in here to "just do the cancel", the claim on the file stops
    being true and this fails -- which is the point: rule 1 says the header is what survives,
    so the header is what gets held.
    """
    tree = ast.parse(Path(xrp_escrow.__file__).read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            imported.add(node.module.split(".")[0])

    transport = {"requests", "urllib", "urllib3", "http", "socket", "httpx", "xrpl", "websockets"}
    assert not imported & transport, f"transport reached a pure verdict module: {imported & transport}"
    assert imported == {"__future__", "typing"}, f"unexpected import in a pure module: {imported}"

    # The absolute set above is empty of siblings because the epoch conversion comes in
    # relatively, and that import is the one this module SHOULD have: rule 8 says the Ripple
    # epoch offset lives in chains/xrp_units and is not respelled here. Named explicitly so the
    # allowlist above cannot be read as "this module imports nothing at all".
    relative = {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.level}
    assert relative == {"xrp_units"}, f"a pure module reached sideways: {relative}"


# ---------------------------------------------------------------------------
# cancel_inputs: whether an EscrowCancel COULD be built, which is a separate
# question from whether it MAY be, and both are separate from doing it.
# ---------------------------------------------------------------------------


def test_the_two_fields_an_escrow_cancel_needs_are_named_in_one_place():
    """Exactly two, and `Owner` is the object's `Account` under another name.

    Rule 11's shape at the smallest scale: the field list is one tuple, and the readiness check
    derives its `missing` from it rather than respelling the names. A third copy of "Owner and
    OfferSequence" is where the two would start to disagree.
    """
    assert CANCEL_NEEDS == ("Owner", "OfferSequence")


def test_the_stranded_escrow_as_account_objects_showed_it_cannot_supply_offer_sequence_yet():
    """The open question, asked as a test rather than answered by assumption.

    THIS IS NOT A CLAIM ABOUT XRPL. It is a claim about what this code does with an entry that
    has no `OfferSequence` -- because nobody here has seen a real one. The operator's account
    settles it on the next run; until then the function must report the absence and name the
    read that would supply it, not default to something a caller could mistake for ready.
    """
    inputs = cancel_inputs({**THE_STRANDED_ESCROW, "PreviousTxnID": "A1B2C3"})
    assert inputs.ready is False
    assert inputs.missing == ("OfferSequence",)
    assert inputs.owner == "rTHEIRS"
    assert inputs.offer_sequence is None
    assert "A1B2C3" in inputs.how_to_get_it  # the one read away, named


def test_an_entry_carrying_offer_sequence_reads_ready():
    """The other branch, so "not ready" is a finding and not this function's only answer."""
    inputs = cancel_inputs({**THE_STRANDED_ESCROW, "OfferSequence": 42})
    assert inputs.ready is True
    assert inputs.missing == ()
    assert inputs.offer_sequence == 42
    assert inputs.owner == "rTHEIRS"


def test_no_previous_txn_id_says_the_object_cannot_supply_it_at_all():
    """Two different "missing OfferSequence" answers, and the difference is actionable.

    With `PreviousTxnID` there is a read that gets it. Without, there is no pointer to the
    EscrowCreate on this entry at all, and telling an operator "one read away" would send them
    looking for a read that does not exist.
    """
    inputs = cancel_inputs(THE_STRANDED_ESCROW)
    assert inputs.ready is False
    assert "PreviousTxnID` is absent" in inputs.how_to_get_it
    assert "one read away" not in inputs.how_to_get_it


def test_a_boolean_is_not_a_sequence_number():
    """`True` is an int in Python and is not a Sequence.

    A bare isinstance(raw, int) accepts it, and `OfferSequence=True` would then read as ready
    with sequence 1 -- a transaction built against ledger sequence 1. The exclusion is explicit
    in the function and so is the reason.
    """
    inputs = cancel_inputs({**THE_STRANDED_ESCROW, "OfferSequence": True})
    assert inputs.ready is False
    assert inputs.offer_sequence is None


def test_a_string_sequence_is_not_accepted_as_a_sequence():
    """account_objects returns some numbers as strings and this is not one to coerce.

    Coercing here would put the guess back: if a real response carries it as a string, the run
    that shows so should say NO and be read, not quietly succeed on a conversion nobody checked.
    """
    inputs = cancel_inputs({**THE_STRANDED_ESCROW, "OfferSequence": "42"})
    assert inputs.ready is False
    assert inputs.offer_sequence is None


@pytest.mark.parametrize("escrow", ["junk", None, 42, []])
def test_a_non_object_is_not_ready_and_says_what_it_got(escrow):
    """Same rule 12 complaint as the verdict: unreadable must not resolve to a usable answer."""
    inputs = cancel_inputs(escrow)
    assert inputs.ready is False
    assert inputs.missing == CANCEL_NEEDS
    assert type(escrow).__name__ in inputs.how_to_get_it


def test_readiness_is_not_authorization():
    """A ready entry is a statement about a dict, and nothing here can act on it.

    REWRITTEN 2026-09-30 BECAUSE IT PASSED ON A NAME TECHNICALITY. It asserted
    `not hasattr(xrp_escrow, "build_cancel")` and `not hasattr(xrp_escrow, "cancel_escrow")`,
    and then escrow_cancel_tx() moved into this module from xrp_htlc_escrow.py -- a function
    that builds exactly the transaction those two names stood for. The test stayed green because
    neither spelling was the one chosen. That is the fourth time in this session a check has
    passed on a string while its premise changed, and the pattern is always the same: it pinned
    a NAME where it meant a PROPERTY.

    THE PROPERTY IS: this module can describe and build, and cannot act. Building a dict is not
    authorization -- submitting is, and submitting needs a signer and a socket, neither of which
    exists here (the import check above reads the statements rather than trusting this
    sentence). Reclaiming an escrow spends a fee, which is armed state and the operator's
    (rule 16).
    """
    inputs = cancel_inputs({**THE_STRANDED_ESCROW, "OfferSequence": 42})
    assert inputs.ready is True
    assert not hasattr(inputs, "submit")

    # Every public callable is a describer or a builder. A name that is neither has to be read
    # rather than pass silently, so the list is explicit and adding to it is a deliberate act.
    #
    # DEFINED HERE, NOT MERELY VISIBLE HERE: the imported names (`NamedTuple`,
    # `unix_from_ripple_time`) are what this module USES, and listing them would make the
    # assertion about its imports, which the import test above already covers precisely.
    # Filtered by __module__ so an import cannot pad the set and a definition cannot hide in it.
    defined = {
        name for name, value in vars(xrp_escrow).items()
        if not name.startswith("_") and callable(value)
        and getattr(value, "__module__", "") == xrp_escrow.__name__
    }
    assert defined == {"cancel_verdict", "reclaimable", "cancel_inputs",
                       "offer_sequence_from", "escrow_cancel_tx",
                       "EscrowVerdict", "CancelInputs"}, (
        f"a new callable appeared in a module whose header promises it cannot act: {defined}"
    )


# ---------------------------------------------------------------------------
# offer_sequence_from: THE ONE READ, now that the operator's run settled that
# account_objects does not carry the field.
#
# Measured 2026-09-30 on the XRP testnet. Four entries -- two escrows, each
# listed under both the sender's and the destination's owner directory --
# and `OfferSequence` was absent from every one while `PreviousTxnID` was
# present in every one. So the question cancel_inputs() printed into the
# report is answered, and the answer is that the sequence comes from the
# creating transaction.
# ---------------------------------------------------------------------------

#: A `tx` response for an EscrowCreate, reduced to the fields the lookup reads.
A_REAL_ESCROW_CREATE = {
    "TransactionType": "EscrowCreate",
    "Account": "rnjG8n16JinjqkzZj5Jmw6NDMBMzhhNbVv",
    "Sequence": 11,
    "validated": True,
}


def test_the_sequence_is_read_off_the_creating_transaction():
    """The happy path, and the whole point of the PreviousTxnID pointer."""
    sequence, why = offer_sequence_from(A_REAL_ESCROW_CREATE)
    assert sequence == 11
    assert why == ""


def test_the_same_fields_nested_under_tx_json_are_read_too():
    """rippled puts them under `tx_json` on some API versions and at the top level on others.

    Reading only one shape would mean a lookup that works against one endpoint and silently
    finds nothing against another -- and "finds nothing" here renders as "cannot build the
    cancel", which looks like a protocol fact rather than a parsing miss.
    """
    sequence, why = offer_sequence_from({"tx_json": A_REAL_ESCROW_CREATE, "validated": True})
    assert sequence == 11
    assert why == ""


def test_a_transaction_that_is_not_an_EscrowCreate_IS_REFUSED_AND_THE_REASON_IS_MONEY():
    """THE REFUSAL THAT MATTERS MOST HERE, and it is not a type check for tidiness.

    `PreviousTxnID` points at whatever LAST MODIFIED the ledger entry. On an untouched escrow
    that is its EscrowCreate; after any other modification it is not. And `OfferSequence` plus
    `Owner` is how the ledger IDENTIFIES an escrow -- so a sequence taken from the wrong
    transaction names a DIFFERENT escrow of the same owner, which the ledger cancels without
    complaint.

    That is not hypothetical for this operator: their account holds TWO 1-XRP escrows to the
    same destination, one reclaimable and one not due for another 85888s. Cancelling the wrong
    one is a silent mis-action with no error message anywhere.
    """
    sequence, why = offer_sequence_from({**A_REAL_ESCROW_CREATE, "TransactionType": "EscrowFinish"})
    assert sequence is None
    assert "not an EscrowCreate" in why
    assert "different escrow of the same owner" in why, "say what the wrong sequence would DO"


@pytest.mark.parametrize("sequence", [None, "11", 11.0, True, [], {}])
def test_a_sequence_that_is_not_an_integer_is_refused_rather_than_coerced(sequence):
    """`True` is an int in Python and is not a sequence number.

    int(True) is 1, so a coercing reader would build an EscrowCancel against ledger sequence 1.
    Same exclusion and same reason as cancel_inputs() above -- stated at both because the two
    read the field from different places (rule 8).
    """
    got, why = offer_sequence_from({**A_REAL_ESCROW_CREATE, "Sequence": sequence})
    assert got is None
    assert "no readable Sequence" in why


@pytest.mark.parametrize("response", ["not a dict", None, 11, []])
def test_a_non_response_says_what_it_got(response):
    got, why = offer_sequence_from(response)
    assert got is None
    assert type(response).__name__ in why


def test_the_lookup_answers_exactly_what_cancel_inputs_said_was_missing():
    """The two halves fit, and this asserts the seam rather than assuming it.

    cancel_inputs() reports `missing == ("OfferSequence",)` and names `PreviousTxnID` as the one
    read. offer_sequence_from() is that read. If the field names ever drift apart, a reader
    following the report's instruction would arrive at a function that returns something else.
    """
    entry = {**THE_STRANDED_ESCROW, "PreviousTxnID": "F74EFFDB"}
    inputs = cancel_inputs(entry)
    assert inputs.missing == ("OfferSequence",)
    assert "F74EFFDB" in inputs.how_to_get_it

    sequence, _ = offer_sequence_from(A_REAL_ESCROW_CREATE)
    supplied = cancel_inputs({**entry, "OfferSequence": sequence})
    assert supplied.ready is True
    assert supplied.offer_sequence == sequence


def test_the_cancel_payload_carries_only_the_four_fields_the_ledger_needs():
    """MOVED HERE FROM xrp_htlc_escrow.py, so the payload is pinned where it now lives.

    tests/test_xrp_escrow_payloads.py still covers it from the entry point's side -- that is not
    duplication but the seam: the entry point must keep getting the same dict after the move.

    NO Fee, NO Sequence, NO SigningPubKey. Those are the submitter's to autofill, and a payload
    that pre-set them would silently override whatever the signer computed.
    """
    payload = escrow_cancel_tx("rSUBMITTER", "rCREATOR", 11)
    assert payload == {
        "TransactionType": "EscrowCancel",
        "Account": "rSUBMITTER",
        "Owner": "rCREATOR",
        "OfferSequence": 11,
    }


def test_the_submitter_and_the_owner_are_not_collapsed():
    """Anybody may cancel an expired escrow, and the drops go to its OWNER regardless.

    MUTATION: default `owner` to `sender`. Every call in xrp_htlc_escrow.py passes the same
    account for both -- it is the creator cancelling its own escrow -- so the suite would stay
    green while the function became unable to express the case the verdict exists to describe:
    a third party reclaiming somebody's expired escrow on their behalf.
    """
    payload = escrow_cancel_tx("rANYBODY", "rCREATOR", 11)
    assert payload["Account"] == "rANYBODY"
    assert payload["Owner"] == "rCREATOR"
    assert payload["Account"] != payload["Owner"]