"""Does the ATM flow ask the right question next, and refuse to undo what it cannot?

Role: test / measurement (seeded answer dicts and seeded pair rows, run against
      the real decision functions)
Reads: services/wizard.py, services/pair_view.asset_rollups for the comparison
Writes: nothing
Can move funds: no
Mainnet-safe: yes

WHY THE STEP DECISION IS TESTED HERE AND NOT THROUGH A PAGE. "Step 3 is
unreachable without a pair" asserted against a seeded dict is one call; asserted
against rendered markup it is a browser, a fixture, and the false passes this
project already paid for twice on 2026-10-07 -- a helper paired with
`if 'data-from=' in body`, true for every tile, and a mutation applied to the
wrong function. tests/page_markup.py exists because of the same lesson.
"""

from __future__ import annotations

import pytest
from services.pair_view import asset_rollups
from services.wizard import (
    AMOUNT_SIDES,
    AMOUNT_STEP,
    POINT_OF_NO_RETURN,
    REVIEW_STEP,
    STEPS,
    answers_after_back,
    current_step,
    destinations_for,
    may_go_back,
    progress,
    source_lamps,
    step_by_number,
)
from valid_addresses import GRC_PAYOUT


def row(from_asset: str, to_asset: str, *, serviceable: bool, reason: str = "") -> dict:
    """One pair row in the shape services/pair_view.allowed_pair_rows() returns.

    Only the keys the wizard reads are set. `serviceable` is the verdict and
    `enabled` is deliberately given the OPPOSITE value in some tests below, to
    pin that the wizard reads the verdict -- that confusion painted /admin's lamps
    green while the customer page's were red, in one process, on 2026-10-07.
    """
    return {
        "from_asset": from_asset,
        "to_asset": to_asset,
        "serviceable": serviceable,
        "enabled": True,
        "reason": reason,
    }


#: The measured ICP asymmetry of 2026-10-07, as rows: ICP -> everything works,
#: nothing -> ICP does, because an ICP payout needs the desk's dfx identity and a
#: balance read does not.
#:
#: BTC <-> XRP IS THE CONTROL and it is here because the first version of this
#: fixture had none. That version asserted GRC and LTC would read "all" in both
#: lamps, which was simply false: each of them has a dead outbound TO ICP, so both
#: read "some" in both lamps and the test failed on my arithmetic rather than on
#: the code. A control needs a chain with NO dead direction in either sense, which
#: is what these two are -- without one, "the lamps agree where there is no
#: asymmetry" is a claim the fixture cannot support.
ICP_ASYMMETRY = [
    row("ICP", "GRC", serviceable=True),
    row("ICP", "LTC", serviceable=True),
    row("GRC", "ICP", serviceable=False, reason="a *->ICP payout needs the desk's dfx identity"),
    row("LTC", "ICP", serviceable=False, reason="a *->ICP payout needs the desk's dfx identity"),
    row("GRC", "LTC", serviceable=True),
    row("LTC", "GRC", serviceable=True),
    row("BTC", "XRP", serviceable=True),
    row("XRP", "BTC", serviceable=True),
]


def test_step_one_asks_about_sending_so_its_lamp_is_outbound_only():
    """ICP must be GREEN on step 1 and the combined rollup must be AMBER.

    THE DEFECT THIS PREVENTS, and services/pair_view.asset_rollups() warned about
    it in its own docstring before the wizard existed: an asset can be "perfectly
    good as a SOURCE and unusable as a DESTINATION". ICP is measured to be exactly
    that. Step 1 asks only "can I send this", whose ICP answer is an unqualified
    yes -- so reusing the combined lamp there would make a customer hesitate over a
    direction that works.

    Both lamps are computed from the SAME rows in this test, which is the point:
    the difference is the question, not the data.
    """
    outbound = {lamp["asset"]: lamp for lamp in source_lamps(ICP_ASYMMETRY)}
    combined = {lamp["asset"]: lamp["key"] for lamp in asset_rollups(ICP_ASYMMETRY)}

    assert outbound["ICP"]["key"] == "all", "every ICP -> * direction is serviceable, so step 1 is green"
    assert outbound["ICP"]["available"] == 2
    assert outbound["ICP"]["total"] == 2
    assert outbound["ICP"]["selectable"] is True

    assert combined["ICP"] == "some", (
        "the COMBINED rollup must still be amber for ICP -- if this changed, the two lamps no longer "
        "differ and this test is no longer measuring anything"
    )
    # THE CONTROL: a chain with no dead direction either way must read the SAME in
    # both lamps, so the divergence above is specific to the asymmetry rather than
    # a constant offset between the two functions.
    assert outbound["BTC"]["key"] == combined["BTC"] == "all"
    assert outbound["XRP"]["key"] == combined["XRP"] == "all"
    # And GRC/LTC, which each have one dead outbound (to ICP), read "some" in both
    # -- asserted rather than left out, because an earlier version of this test
    # claimed they were "all" and failed on that arithmetic.
    assert outbound["GRC"]["key"] == combined["GRC"] == "some"
    assert outbound["LTC"]["key"] == combined["LTC"] == "some"


def test_a_coin_with_no_working_outbound_direction_is_not_selectable():
    """`selectable` is what the screen disables on, and it must follow the verdict.

    Seeded with `enabled: True` against `serviceable: False` on purpose: the row
    says the pair is configured and the verdict says it cannot be quoted. A lamp
    reading `enabled` would offer it.
    """
    rows = [
        row("BTC", "GRC", serviceable=False, reason="bitcoind unreachable"),
        row("BTC", "LTC", serviceable=False, reason="bitcoind unreachable"),
    ]
    lamp = next(lamp for lamp in source_lamps(rows) if lamp["asset"] == "BTC")
    assert lamp["key"] == "none"
    assert lamp["selectable"] is False
    assert lamp["available"] == 0
    assert lamp["total"] == 2
    assert "0 of 2" in lamp["detail"], "rule 14: the count goes next to the lamp, not only in a tooltip"


def test_an_unavailable_destination_is_shown_greyed_with_its_reason_not_hidden():
    """Rule 14 one level up: absent and unavailable must look different.

    A customer who came to get ICP out and finds ICP simply missing from step 2
    cannot tell "this terminal does not do that pair" from "that direction is down
    right now". Those deserve different reactions, so the option is returned with
    selectable False and the cause attached.
    """
    options = destinations_for(ICP_ASYMMETRY, "GRC")
    by_asset = {option["asset"]: option for option in options}

    assert set(by_asset) == {"ICP", "LTC"}, "every configured destination appears, working or not"
    assert by_asset["LTC"]["selectable"] is True
    assert by_asset["LTC"]["reason"] == ""
    assert by_asset["ICP"]["selectable"] is False
    assert "dfx identity" in by_asset["ICP"]["reason"], "the row's own reason reaches the screen"

    # Selectable options sort first, so the working choices are what a customer
    # meets at the top rather than interleaved with dead ones.
    assert [option["asset"] for option in options] == ["LTC", "ICP"]


def test_a_destination_with_no_reason_recorded_still_says_something():
    """An empty reason must not render as a blank -- `(none)` is a result, a gap is not."""
    options = destinations_for([row("GRC", "XRP", serviceable=False, reason="")], "GRC")
    assert options[0]["selectable"] is False
    assert options[0]["reason"], "a refused option with no recorded reason still needs a sentence"


def test_the_flow_asks_for_exactly_what_is_missing_and_in_order():
    """current_step() walks the answers and stops at the first gap.

    Built up one answer at a time rather than asserted at three points, because
    what is under test IS the order and a spot check cannot distinguish "step 3
    comes after step 2" from "step 3 comes after any two answers".
    """
    answers: dict = {}
    assert current_step(answers)["number"] == 1

    answers["from_asset"] = "ICP"
    assert current_step(answers)["number"] == 2

    answers["to_asset"] = "GRC"
    assert current_step(answers)["number"] == 3

    answers["amount"] = "1.0"
    assert current_step(answers)["number"] == 4

    # DERIVED, NOT TYPED. tests/valid_addresses.py is what
    # test_address_literals_are_valid.py's ceiling exists to push callers toward: a
    # typed address can be mistyped and says nothing about what it is for. Writing
    # one here took the tree from 60 literals to 61 and failed that ratchet, which
    # is rule 19 working as intended -- the fix is to stop writing the literal, not
    # to raise the ceiling.
    answers["payout_address"] = GRC_PAYOUT
    assert current_step(answers)["number"] == 5

    # Confirmed but no swap id yet: the commit is in flight or it failed, and step
    # 5 is where the button and any error belong.
    answers["confirmed"] = "1"
    assert current_step(answers)["number"] == 5

    answers["swap_id"] = "s_2b89b9e979a194ce"
    assert current_step(answers)["number"] == 6


def test_a_blank_answer_is_not_an_answer():
    """An empty string is what a submitted-but-blank field gives.

    Counting it as answered skips a step with nothing in it, and the customer
    arrives at the review with a missing figure rather than at the question.
    """
    assert current_step({"from_asset": ""})["number"] == 1
    assert current_step({"from_asset": "ICP", "to_asset": ""})["number"] == 2
    assert current_step({"from_asset": "ICP", "to_asset": "GRC", "amount": ""})["number"] == 3
    # And 0 is blank for this purpose too -- "how much" answered with zero is not
    # an amount, and falsiness is the behavior being pinned rather than a detail.
    assert current_step({"from_asset": "ICP", "to_asset": "GRC", "amount": 0})["number"] == 3


def test_an_existing_swap_short_circuits_every_other_answer():
    """A swap id means step 6, whatever else is missing.

    Once create_swap() has returned, the answers that produced it are history. A
    missing `confirmed` flag at that point would walk the customer back to the
    review of a swap that already exists -- and the review's button would create a
    second one.
    """
    assert current_step({"swap_id": "s_abc"})["number"] == POINT_OF_NO_RETURN
    assert current_step({"swap_id": "s_abc", "from_asset": "", "amount": ""})["number"] == POINT_OF_NO_RETURN


def test_back_works_up_to_the_review_and_never_after_it():
    """The irreversibility boundary, which is the one thing this flow must not fudge.

    At step 6 a swap row exists and a deposit address has been handed out. On ICP
    the address comes from a subaccount index that icp_deposit_subaccounts never
    reissues, so a discarded step-6 swap strands it permanently. The refusal
    carries a sentence rather than hiding the button: a customer looking at an
    address they may already have copied needs to be told, not silently ignored.
    """
    assert may_go_back(1) == (False, "This is the first question.")
    for step in (2, 3, 4, 5):
        allowed, reason = may_go_back(step)
        assert allowed is True, f"step {step} is still only answers; back must work"
        assert reason == ""

    allowed, reason = may_go_back(POINT_OF_NO_RETURN)
    assert allowed is False
    assert "already exists" in reason
    assert "do not send to this address" in reason.lower(), (
        "the refusal must warn about the address, because that is the thing that costs money"
    )


def test_the_steps_are_numbered_without_gaps_and_only_the_last_collects_nothing():
    """Structural, so a reordered or renumbered STEPS fails here rather than in use."""
    assert [step["number"] for step in STEPS] == list(range(1, len(STEPS) + 1))
    assert len(STEPS) == POINT_OF_NO_RETURN
    collects_nothing = [step["number"] for step in STEPS if step["needs"] is None]
    assert collects_nothing == [POINT_OF_NO_RETURN], (
        "only the deposit screen collects no answer; any other such step would be unreachable, "
        "because current_step() skips it and nothing would ever draw it"
    )
    # Every step must say what it asks, in the customer's words. A blank question
    # renders as an empty heading, which is rule 14's blank gap.
    for step in STEPS:
        assert step["question"].strip(), f"step {step['number']} has no question"
        assert step["hint"].strip(), f"step {step['number']} has no hint"
    assert set(AMOUNT_SIDES) == {"send", "receive"}


def test_an_unknown_step_number_raises_rather_than_resetting_the_flow():
    """Returning step 1 for a bad number looks like the flow resetting itself."""
    with pytest.raises(IndexError, match="no wizard step numbered 9"):
        step_by_number(9)
    with pytest.raises(IndexError):
        step_by_number(0)


def test_the_progress_strip_marks_exactly_one_step_current():
    """Two current steps, or none, is a strip that cannot be read."""
    for number in range(1, POINT_OF_NO_RETURN + 1):
        strip = progress(number)
        assert len(strip) == len(STEPS)
        current = [entry for entry in strip if entry["state"] == "current"]
        assert len(current) == 1, f"at step {number} the strip marks {len(current)} steps current"
        assert current[0]["number"] == number
        assert [entry["number"] for entry in strip if entry["state"] == "done"] == list(range(1, number))


# =============================================================================
# GOING BACK. The half that is easy to get wrong is not the refusal at step 6 --
# it is what a permitted back must CLEAR, because clearing too little produces a
# button that visibly does nothing and clearing too little in the other direction
# produces a pair the customer never chose.
# =============================================================================


def test_going_back_clears_the_question_being_returned_to():
    """Keeping the answer makes current_step() bounce straight forward again.

    current_step() returns the first step whose answer is MISSING. Go back to
    step 2 with step 2's answer still set and it is not missing, so the flow
    returns to step 3 immediately -- a back button that appears to do nothing,
    which is rule 13's "a stop that cannot prove it worked".
    """
    full = {
        "from_asset": "ICP", "to_asset": "GRC", "amount": "1.0",
        "amount_side": "send", "payout_address": GRC_PAYOUT,
    }
    assert current_step(full)["number"] == 5

    back_to_2 = answers_after_back(full, 2)
    assert current_step(back_to_2)["number"] == 2, "the flow must actually land on step 2"
    assert "to_asset" not in back_to_2


def test_going_back_also_clears_every_answer_AFTER_the_target():
    """The half that is easy to miss, and it silently changes the pair.

    Return to step 1 to change the source while `to_asset` is still set and the
    customer gets a pair they never chose: pick BTC and ICP->GRC becomes BTC->GRC,
    with the amount still sized for the old pair. The later answers are not merely
    stale -- they answer questions that no longer apply.
    """
    full = {
        "from_asset": "ICP", "to_asset": "GRC", "amount": "1.0",
        "amount_side": "receive", "payout_address": GRC_PAYOUT,
        "quote_id": "q_123", "confirmed": "1",
    }
    back_to_1 = answers_after_back(full, 1)
    assert current_step(back_to_1)["number"] == 1
    for gone in ("from_asset", "to_asset", "amount", "payout_address", "quote_id", "confirmed"):
        assert gone not in back_to_1, f"{gone} survived a return to step 1"


def test_the_quote_and_the_consent_are_always_discarded():
    """A quote is priced for one pair and one size; consent is to figures on a screen.

    Carrying a quote past any edit would let create_swap() be called with a quote
    that does not match what the customer agreed to. Carrying `confirmed` would
    treat consent to the old figures as consent to the new ones.

    Asserted for EVERY permitted target rather than one, because the exemption
    that matters would be a single step where the carry-forward was kept.
    """
    full = {
        "from_asset": "ICP", "to_asset": "GRC", "amount": "1.0", "amount_side": "send",
        "payout_address": GRC_PAYOUT, "quote_id": "q_123", "confirmed": "1",
    }
    for target in (1, 2, 3, 4, 5):
        cleared = answers_after_back(full, target)
        assert "quote_id" not in cleared, f"a quote survived a return to step {target}"
        assert "confirmed" not in cleared, f"consent survived a return to step {target}"


def test_the_amount_side_is_cleared_with_the_amount_and_not_after_it():
    """Returning to step 4 keeps how step 3 was answered; returning to step 3 does not.

    A customer who typed a receive-side figure, went back to the amount, and found
    the box labelled "what you send" with their receive-side number still in it
    would be looking at a figure that means something other than what the label
    says -- which is rule 14's "state what the number means" failing on the field
    the whole screen is about.
    """
    full = {
        "from_asset": "ICP", "to_asset": "GRC", "amount": "300",
        "amount_side": "receive", "payout_address": GRC_PAYOUT,
    }
    assert "amount_side" not in answers_after_back(full, 3), "the side goes with the amount"
    assert "amount_side" not in answers_after_back(full, 1)
    # Step 4 is AFTER the amount, so the amount and its side both survive.
    kept = answers_after_back(full, 4)
    assert kept["amount"] == "300"
    assert kept["amount_side"] == "receive"


def test_going_back_does_not_mutate_what_it_was_given():
    """A new dict, so a caller can render both and a test can assert the input is intact."""
    full = {"from_asset": "ICP", "to_asset": "GRC"}
    before = dict(full)
    answers_after_back(full, 1)
    assert full == before, "answers_after_back mutated its argument"


def test_the_named_step_constants_match_their_positions_in_STEPS():
    """REVIEW_STEP and AMOUNT_STEP exist so no route writes a bare 5 or 3.

    They are a second spelling of a position STEPS already defines, which is
    exactly the shape that drifts (rule 8) -- so the agreement is asserted rather
    than assumed.
    """
    assert step_by_number(REVIEW_STEP)["key"] == "confirm"
    assert step_by_number(AMOUNT_STEP)["key"] == "amount"
    assert REVIEW_STEP == POINT_OF_NO_RETURN - 1, (
        "the review must be the step immediately before the irreversible one, or a confirmed flow "
        "either commits early or never reaches the commit at all"
    )
