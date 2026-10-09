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
from chains.amount_solve import deposit_for_desired_payout, payout_for_deposit
from services.pair_view import asset_rollups
from services.wizard import (
    ADDRESS_STEP,
    AMOUNT_SIDES,
    AMOUNT_STEP,
    FURNITURE_KEYS,
    POINT_OF_NO_RETURN,
    REVIEW_STEP,
    STEPS,
    amount_as_number,
    answers_after_back,
    both_sides,
    current_step,
    destinations_for,
    may_go_back,
    progress,
    reject_amount,
    screen_furniture,
    source_lamps,
    step_by_number,
    unavailable_note,
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


def test_an_unavailable_destination_is_shown_greyed_with_a_reason_a_customer_can_act_on():
    """Rule 14 one level up: absent and unavailable must look different.

    A customer who came to get ICP out and finds ICP simply missing from step 2
    cannot tell "this terminal does not do that pair" from "that direction is down
    right now". Those deserve different reactions, so the option is returned with
    selectable False and a sentence attached.

    REWRITTEN 2026-10-09, AND THE INVARIANT GOT STRONGER RATHER THAN WEAKER (rule
    2). It used to assert `"dfx identity" in by_asset["ICP"]["reason"]` -- that the
    ROW'S OWN reason reached the screen. It did, and that was the defect: the row's
    reason is operator text, and screen 2 was rendering

        SOL has no adapter in this process: SOL_RPC_URL is unset (or 0) in the
        environment this process was started with. Nothing in the serving path
        reads a .env, so it has to be exported in the shell that starts the server

    to an unauthenticated reader on the public port. So what is asserted now is
    both halves: a sentence IS attached, and the operator text is NOT it.

    THE SEEDED REASON IS THE POSITIVE CONTROL and is why this cannot pass vacuously.
    `ICP_ASYMMETRY`'s row carries "dfx identity"; if destinations_for() ever goes
    back to passing `row["reason"]` through, the absence assertion fails. Asserting
    only `option["reason"] != ""` would pass under that reversion and under a stub
    that returns any string at all -- which is the shape of three mutations that
    survived earlier in this session because "nothing was found" and "nothing was
    looked at" produced the same green.
    """
    options = destinations_for(ICP_ASYMMETRY, "GRC")
    by_asset = {option["asset"]: option for option in options}

    assert set(by_asset) == {"ICP", "LTC"}, "every configured destination appears, working or not"
    assert by_asset["LTC"]["selectable"] is True
    assert by_asset["LTC"]["reason"] == ""
    assert by_asset["ICP"]["selectable"] is False
    assert by_asset["ICP"]["reason"] == unavailable_note(), (
        "a refused option carries the customer sentence, not whatever the row recorded"
    )
    assert "dfx identity" not in by_asset["ICP"]["reason"], (
        "the row's operator reason reached the customer screen again"
    )

    # Selectable options sort first, so the working choices are what a customer
    # meets at the top rather than interleaved with dead ones.
    assert [option["asset"] for option in options] == ["LTC", "ICP"]


def test_the_customer_sentence_names_no_mechanism_and_no_setting():
    """What unavailable_note() may NOT contain, over the vocabulary that leaked.

    A list and not one string, because the leak was not one word: screen 2 rendered
    variable names for four different chains, the phrase ".env", and the word
    "adapter", and a test pinning any one of them would have passed while the
    others shipped. Measured 2026-10-09: sixteen distinct reasons were reachable.
    """
    note = unavailable_note()
    assert note, "a refused option with no recorded reason still needs a sentence"
    for forbidden in ("_RPC", ".env", "export", "adapter", "process", "unset", "environment"):
        assert forbidden not in note, f"the customer sentence names {forbidden!r}, which its reader cannot act on"
    assert "not available right now" in note, "it has to say the direction is down, not merely be short"


def test_every_refused_destination_gets_the_same_sentence_whatever_the_cause():
    """One sentence per cause was considered and refused; this pins that decision.

    Naming the side -- "GRC cannot be paid out right now" -- reads better and is not
    always true, because the same greyed tile is produced by the SOURCE being unable
    to take a deposit. A sentence blaming the destination would then be wrong on the
    screen whose whole job is telling a customer what is possible.

    So: three rows with three different recorded causes, one sentence. If a later
    change makes the cause customer-visible again, this fails and the reasoning
    above is what has to be argued with.
    """
    rows = [
        row("GRC", "ICP", serviceable=False, reason="ICP has no adapter in this process"),
        row("GRC", "LTC", serviceable=False, reason="LTC cannot sign: no key loaded"),
        row("GRC", "XRP", serviceable=False, reason=""),
    ]
    sentences = {option["reason"] for option in destinations_for(rows, "GRC")}
    assert sentences == {unavailable_note()}, (
        f"three different causes produced {len(sentences)} different customer sentences"
    )


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


# ===========================================================================
# WHAT EACH SCREEN CARRIES BESIDE ITS QUESTION, added 2026-10-09.
#
# Operator: "the first screen only have the buttons, the colum to the right the
# fees etc OR the swap id they can enter at the bottom. next page will just be
# the buttons they want to covert into. next screen will be the either/or amount
# and get the quote and keep the fees to the right. then they can enter their
# final wallet address for their swapped crypto."
#
# TESTED HERE AND NOT THROUGH A PAGE, for this file's own reason: the
# arrangement is a function of a step NUMBER, so it is one call per claim. The
# RENDERED consequences -- that the costs really are inside the <aside>, that the
# lookup really is outside the two-column wrapper -- are in
# tests/test_atm_flow.py, where the real handler draws them. Both halves are
# needed: a correct table rendered into the wrong element is the defect class
# this project keeps paying for.
# ===========================================================================


def test_the_first_screen_carries_the_costs_the_directory_and_the_lookup():
    """Screen 1's three pieces, named one at a time so a failure says which.

    This is the operator's sentence turned into assertions. The costs go to the
    right-hand column, the swap-id box to the bottom, and the 30-direction
    directory -- which is neither, and which rule 2 would otherwise have deleted
    -- rides with the costs because it is the same KIND of thing: something you
    look up while choosing, not something you answer.
    """
    first = screen_furniture(1)
    assert first["costs"] is True, "screen 1 lost the fee column the operator asked for"
    assert first["lookup"] is True, "the swap-id box is not at the bottom of screen 1"
    assert first["pair_reference"] is True, (
        "the 30-direction directory is on no screen at all. routes/atm.start()'s docstring records "
        "that it is the ONLY answer to 'what does this terminal do', which is why it survived "
        "templates/index.html being deleted -- dropping it is a deletion, not a layout change"
    )
    assert first["estimate"] is False, (
        "screen 1 prices something, and nothing has been chosen yet -- there is no pair and no "
        "amount, so any figure there would be invented"
    )


def test_the_destination_screen_carries_nothing_but_its_buttons():
    """"next page will just be the buttons they want to covert into."

    EVERY key false, not just the ones that were true on screen 1. A test that
    checked only `costs` would pass if the swap-id box drifted onto this screen,
    and a second way out of the flow sitting beside the question is exactly what
    the one-question-per-screen premise is against.
    """
    assert set(screen_furniture(2).values()) == {False}, (
        f"screen 2 carries {[k for k, v in screen_furniture(2).items() if v]}"
    )


def test_the_amount_screen_keeps_the_fees_to_the_right_and_prices_nothing_yet():
    """"keep the fees to the right" -- and not the estimate, which has no input yet.

    The amount screen is where the figure is TYPED, so there is nothing to price
    until it has been submitted. An estimate block here would either be empty or
    be showing the previous answer, and both read as a number about this screen.
    """
    amount = screen_furniture(AMOUNT_STEP)
    assert amount["costs"] is True, "the fee column is not on the amount screen"
    assert amount["estimate"] is False
    assert amount["lookup"] is False, "a swap-id box mid-flow competes with the question"
    assert amount["pair_reference"] is False, (
        "a customer answering 'how much?' is not shopping for a pair"
    )


def test_the_address_screen_is_where_the_quote_they_asked_for_lands():
    """"get the quote" happens on screen 3; this is the screen that can show it.

    It is the FIRST step on which the pair and the amount are both settled, so it
    is the first that can state what the trade comes to. Pinned because the whole
    point of relabelling step 3's button is that pressing it visibly produces
    something -- a button called "Get the quote" that led to a bare address box
    would be rule 14's silence with a louder label on it.
    """
    address = screen_furniture(ADDRESS_STEP)
    assert address["estimate"] is True, (
        "nothing on the address screen states the price, so 'Get the quote' on the screen before it "
        "produces no visible quote"
    )
    assert address["costs"] is True, "the quote window belongs beside the figures it governs"


def test_the_review_states_the_figures_itself_and_takes_no_second_column():
    """Step 5 gets the priced pair and NOT the aside, which is the whole split.

    templates/_atm_confirm.html already lists every figure, so a right-hand
    column repeating the fee beside it would be two renderings of the numbers the
    next button commits to -- rule 8 on the one screen where disagreement costs
    the deposit. What it lacked was the send figure itself, so it is given the
    estimate and no furniture.
    """
    review = screen_furniture(REVIEW_STEP)
    assert review["estimate"] is True, (
        "the review has no priced figures, so a customer who typed the RECEIVE side is back to "
        "reading a sentence where the deposit amount goes"
    )
    assert review["costs"] is False, (
        "the review now has a second column stating the fee as well as its own list -- two "
        "renderings of one number on the screen whose button moves money"
    )


def test_the_deposit_screen_and_any_unknown_step_carry_nothing_and_do_not_raise():
    """All-false rather than an exception, which is the opposite of step_by_number().

    The difference is deliberate and both directions are pinned. step_by_number()
    is asked WHICH STEP THIS IS and refuses a wrong answer, because drawing the
    wrong screen over a customer's answers looks like the flow resetting itself.
    This one is asked whether a screen also shows the fee table, and the honest
    answer for a screen nobody listed is no -- raising would turn a newly added
    step into a 500 on a page whose question would otherwise have rendered.
    """
    for unknown in (POINT_OF_NO_RETURN, 0, -1, 99):
        carried = screen_furniture(unknown)
        assert set(carried.values()) == {False}, f"step {unknown} carries {carried}"
        assert set(carried) == set(FURNITURE_KEYS), (
            f"step {unknown} answers {sorted(carried)} and not every key -- a template asking for a "
            "missing one gets Undefined, which Jinja renders as absence and nothing reports"
        )


def test_every_step_answers_every_key_so_a_template_never_reads_undefined():
    """The shape invariant, over the whole flow.

    WHY IT IS WORTH ITS OWN TEST. In Jinja an attribute that does not exist is
    Undefined, which is falsey and renders as nothing -- so `{% if furniture.cost %}`,
    one letter out, hides the right-hand column on every screen and no test,
    template or log says so. A uniform dict is what makes that a typo in a key
    name rather than a silent layout change.
    """
    assert FURNITURE_KEYS, "the furniture vocabulary is empty, so these tests assert nothing"
    for step in STEPS:
        carried = screen_furniture(step["number"])
        assert set(carried) == set(FURNITURE_KEYS)
        assert all(isinstance(value, bool) for value in carried.values()), (
            f"step {step['number']} answers with something other than a bool: {carried}"
        )


def test_the_lookup_appears_on_exactly_one_screen():
    """One way back into an existing swap, on the screen where you have not started one.

    A swap-id box on a later screen is a second thing to do beside the question,
    and on the review it would sit next to the button that creates a swap.
    Counted over the whole flow rather than asserted per-step, so a new entry in
    the table cannot quietly add a second one.
    """
    carrying = [step["number"] for step in STEPS if screen_furniture(step["number"])["lookup"]]
    assert carrying == [1], f"the swap-id box is on steps {carrying}"


# ===========================================================================
# BOTH SIDES OF THE TRADE, which is what "get the quote" shows.
# ===========================================================================


def test_typing_the_send_side_prices_what_comes_back():
    """The forward case: the receive leg is solved and the send leg is echoed."""
    priced = both_sides({"amount": "2", "amount_side": "send",
                         "from_asset": "ICP", "to_asset": "GRC"}, 321.7, 150)
    assert priced["refusal"] == ""
    assert priced["send"] == 2.0, "the typed figure must come back unaltered on the side it was typed"
    assert priced["receive"] == payout_for_deposit(2.0, 321.7, 150, "GRC")[0]
    assert priced["side"] == "send"


def test_typing_the_receive_side_states_the_deposit_that_was_never_shown():
    """THE DEFECT THIS FUNCTION EXISTS FOR, pinned as a number rather than a phrase.

    Until 2026-10-09 a customer who answered "I want 300 GRC" saw
    "&#8776; solved from what you want" on the review where the deposit amount
    goes, and routes/atm._commit() solved the real figure only AFTER the confirm
    button -- so the one number they had to put into a wallet first appeared on
    the swap page of a swap that already existed.
    """
    priced = both_sides({"amount": "300", "amount_side": "receive",
                         "from_asset": "ICP", "to_asset": "GRC"}, 321.7, 150)
    assert priced["refusal"] == ""
    assert priced["send"] == deposit_for_desired_payout(300.0, 321.7, 150, "ICP")[0]
    assert priced["send"] > 0, "the deposit figure is still not a number"

    # AND THE RECEIVE LEG IS WHAT THAT DEPOSIT BUYS, NOT WHAT WAS ASKED FOR. The
    # deposit rounds UP, so the payout lands a hair above the figure typed, and
    # that is the figure services/quote_service.create_quote() stamps on the swap
    # row one click later. Echoing the typed 300.0 would put a number on the
    # review that the row does not contain.
    assert priced["receive"] == payout_for_deposit(priced["send"], 321.7, 150, "GRC")[0]
    assert priced["receive"] >= 300.0, "a customer would be shown less than they asked for"


def test_a_side_the_flow_does_not_know_is_refused_in_the_validators_own_words():
    """One sentence for one condition, built from AMOUNT_SIDES rather than typed.

    reject_amount() refuses the same condition, and a customer told two different
    things about one radio button has been told nothing. Asserted by comparing the
    two functions' output rather than against a literal, so a reworded sentence
    stays in step automatically.
    """
    answers = {"amount": "1", "amount_side": "sideways", "from_asset": "ICP", "to_asset": "GRC"}
    priced = both_sides(answers, 321.7, 150)
    assert priced["refusal"] == reject_amount("1", "sideways", 10.0, "")
    assert (priced["send"], priced["receive"]) == (0.0, 0.0), (
        "a refused side still produced figures, so a screen could print both the reason and a number"
    )


def test_a_missing_side_is_the_send_side_everywhere_or_the_screen_lies():
    """The default has to be the SAME one routes/atm.py commits with.

    _first_bad_answer() and _commit() both read `answers.get("amount_side", "send")`,
    so a browser that dropped the radio is treated as having typed the send side
    by the code that creates the swap. If this function defaulted the other way,
    the review would show a deposit solved for a payout and the commit would take
    the typed figure as the deposit -- the screen and the authority disagreeing
    about which number the customer meant.
    """
    without = both_sides({"amount": "2", "from_asset": "ICP", "to_asset": "GRC"}, 321.7, 150)
    explicit = both_sides({"amount": "2", "amount_side": "send",
                           "from_asset": "ICP", "to_asset": "GRC"}, 321.7, 150)
    assert without == explicit
    assert without["side"] == "send"
    # An empty string is what a submitted-but-blank field gives, and current_step()
    # already treats that as unanswered. It must not fall through to the refusal.
    assert both_sides({"amount": "2", "amount_side": "",
                       "from_asset": "ICP", "to_asset": "GRC"}, 321.7, 150) == explicit


def test_an_unusable_amount_reports_the_same_sentence_the_amount_step_does():
    """A bad figure refuses with amount_as_number()'s wording, not a second one."""
    for typed in ("", "1,5", "0", "-3"):
        answers = {"amount": typed, "amount_side": "send", "from_asset": "ICP", "to_asset": "GRC"}
        priced = both_sides(answers, 321.7, 150)
        assert priced["refusal"] == amount_as_number(typed)[1], f"{typed!r} got a second wording"
        assert (priced["send"], priced["receive"]) == (0.0, 0.0)


def test_an_unpriceable_pair_refuses_rather_than_printing_a_zero():
    """A rate of zero is not a price of zero, and the two must not render alike.

    routes/atm._rate_hint() already returns a sentence beside a zero rate for
    exactly this reason, and this is the same distinction one layer down: a
    screen that printed "you receive 0.0 GRC" for an unreadable feed would be
    telling a customer the desk values their coin at nothing.
    """
    priced = both_sides({"amount": "2", "amount_side": "send",
                         "from_asset": "ICP", "to_asset": "GRC"}, 0.0, 150)
    assert priced["refusal"], "an unpriceable pair produced figures with no reason attached"
    assert (priced["send"], priced["receive"]) == (0.0, 0.0)
