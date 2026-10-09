"""Which step of the ATM flow is current, what it needs, and what it must not let you undo.

Role: submodule (decisions only -- pure functions over answers already collected;
      no database, no socket, no adapter call)
Reads: the answers dict a caller hands it, pair rows
      services/pair_view.allowed_pair_rows() has already built, and -- since
      2026-10-09 -- a rate and a fee the caller has already read, for both_sides()
Writes: nothing
Can move funds: no. It decides which screen to draw. The commit at the end of the
      flow is routes/swaps.py calling services/swap_service.create_swap(), exactly
      as the one-page form did.
Mainnet-safe: yes to import and yes to call.

WHY A STEP MACHINE AND NOT A TALLER PAGE. Operator, 2026-10-07: "i really want
the UI to be like an ATM sequence of steps." The page it replaces showed all of
it at once -- hero, availability lamps, the pair grid, the terms, a quote form, a
create form and a lookup box, on one scroll -- with the quote and create panels
numbered 1 and 2 inside that scroll. An ATM asks one question per screen, shows
what you have chosen so far, and puts the irreversible action behind a review.

THE DECISION LIVES HERE AND NOT IN THE TEMPLATE, which is rule 10 and also the
only way this is testable. "Step 3 is unreachable without a pair" asserted
against a seeded answers dict is one function call; asserted against a rendered
page it is a browser and a fixture. The lamp defect of 2026-10-07 took two
attempts precisely because the decision was being read out of markup -- see
tests/page_markup.py, which exists because seven tests broke on an added
attribute.

NOTHING IS DECIDED IN THE BROWSER. Each step posts and asks the server what comes
next, the same contract services/swap_view.py already states on the live swap
page: "Nothing is decided in your browser: each refresh asks the server what the
status means." A wizard that tracked its own position client-side would be a
second authority for how far a customer has got, and rule 5 is about exactly that.

WHERE BACK STOPS WORKING, AND WHY IT IS A HARD BOUNDARY. Steps 1 to 5 are
answers; step 6 is a swap row and a deposit address that has been handed out. A
"back" from step 6 would orphan an address the customer may already have pasted
into a wallet -- and on ICP it would orphan an allocated subaccount index that
icp_deposit_subaccounts will never reissue. So may_go_back() refuses there rather
than offering a button that silently does something else.
"""

from __future__ import annotations

from chains.amount_solve import deposit_for_desired_payout, payout_for_deposit

from .asset_identity import color_class_for, symbol_for, symbol_title_for
from .pair_view import ASSET_ROLLUP_STATES

#: The lamp vocabulary, BORROWED rather than restated. services/pair_view.py owns
#: the level/word/note for each of all/some/none, and the existing customer page
#: renders its legend from the same table. A second vocabulary here would be a
#: wizard whose green means something slightly different from the green one screen
#: over -- rule 8's drift, on the one element whose whole job is to be comparable.
_LAMP_VOCABULARY = {state["key"]: state for state in ASSET_ROLLUP_STATES}

#: The flow, in order. `needs` is the answer the step collects; `question` is what
#: the screen asks, in the customer's words rather than the schema's.
#:
#: A TUPLE OF DICTS AND NOT SIX FUNCTIONS, because the ORDER is the thing under
#: test and a dispatch table hides it. current_step() walks this list and returns
#: the first entry whose `needs` is unanswered, so adding a step is one entry here
#: and the gaps cannot be reordered by accident.
STEPS: tuple[dict, ...] = (
    {
        "number": 1,
        "key": "from_asset",
        "needs": "from_asset",
        "question": "What are you sending?",
        "hint": "Pick the coin you already have. The lamp shows whether it can be sent right now.",
    },
    {
        "number": 2,
        "key": "to_asset",
        "needs": "to_asset",
        "question": "What do you want back?",
        "hint": "Only the coins your chosen one can actually be swapped for are shown.",
    },
    {
        "number": 3,
        "key": "amount",
        "needs": "amount",
        "question": "How much?",
        "hint": "Type either side -- what you are sending, or what you want to receive.",
    },
    {
        "number": 4,
        "key": "payout_address",
        "needs": "payout_address",
        "question": "Where should it go?",
        "hint": "Your own address on the receiving chain. It is checked before you go on.",
    },
    {
        "number": 5,
        "key": "confirm",
        "needs": "confirmed",
        "question": "Is this right?",
        "hint": "Every figure once more. The next button creates the swap.",
    },
    {
        "number": 6,
        "key": "deposit",
        "needs": None,
        "question": "Send your coin",
        "hint": "The swap exists and this address belongs to it alone.",
    },
)

#: The review screen -- the last one that is still only answers, and the one
#: whose button commits. Named rather than written as 5, because a literal 5 in a
#: route is PLR2004 and, worse, is a second claim about where the review is.
REVIEW_STEP = 5

#: The step that asks how much. Named for the same reason REVIEW_STEP is: a bare
#: 3 in answers_after_back() is PLR2004 and a second claim about which step owns
#: the amount.
AMOUNT_STEP = 3

#: The first step a customer cannot leave by going back. See the module docstring:
#: at this point a swap row exists and a deposit address has been issued.
POINT_OF_NO_RETURN = 6

#: The step that asks where the payout goes. Named for REVIEW_STEP's reason, and
#: used by screen_furniture() below to decide which screens carry a priced
#: estimate -- the first screen after the amount is settled is the first one that
#: CAN carry one.
ADDRESS_STEP = 4

#: WHAT EACH SCREEN CARRIES BESIDE ITS ONE QUESTION, keyed by step number.
#:
#: Operator, 2026-10-09: "the first screen only have the buttons, the colum to
#: the right the fees etc OR the swap id they can enter at the bottom. next page
#: will just be the buttons they want to covert into. next screen will be the
#: either/or amount and get the quote and keep the fees to the right. then they
#: can enter their final wallet address for their swapped crypto."
#:
#: WHAT THIS REPLACES, because the shape of the old answer is why a table is
#: needed now. routes/atm.py carried ONE boolean, `show_reference = step.number
#: == 1`, and templates/atm.html hung THREE panels off it -- the swap lookup, the
#: 30-direction matrix and the costs table -- stacked below the question in one
#: scrolling column. The instruction above splits those three apart: the costs go
#: to a right-hand column on TWO screens, the lookup stays at the bottom of ONE,
#: and the matrix is reference material that belongs with the costs rather than
#: with the lookup. One boolean cannot say that, and three booleans computed in a
#: route are three decisions outside the module that owns the flow (rule 10).
#:
#: A TABLE AND NOT A CHAIN OF `if`s, for the reason STEPS itself is a tuple of
#: dicts: the arrangement across screens is the thing under test, and an `elif`
#: ladder hides it. A reader can see at a glance that `lookup` is true exactly
#: once, which is the property that matters -- a swap-id box on every screen
#: would be a second way in competing with the question in front of you.
#:
#: STEP 2 IS DELIBERATELY EMPTY: "next page will just be the buttons they want to
#: covert into."
#:
#: THE REVIEW TAKES `estimate` AND NOT `costs`, AND THE SPLIT IS THE WHOLE REASON
#: THIS IS A TABLE OF NAMES RATHER THAN A BOOLEAN. Step 5 already states every
#: figure in templates/_atm_confirm.html, so a right-hand column repeating the fee
#: beside it would be two renderings of the numbers the next button commits to --
#: rule 8 on the one screen where disagreement costs the deposit. What it DID
#: lack is the send figure itself: a customer who typed the RECEIVE side read
#: "&#8776; solved from what you want" where the amount goes. So the review is
#: given the priced pair and no aside, and _atm_confirm.html puts the number in
#: its own list.
#:
#: STEP 6 HAS NO ENTRY AND THAT IS NOT AN OVERSIGHT: screen_furniture() returns
#: all-false for any step not listed, so the deposit screen -- which is normally a
#: redirect to /swap/<id> anyway -- carries nothing.
_SCREEN_FURNITURE: dict[int, tuple[str, ...]] = {
    1: ("costs", "pair_reference", "lookup"),
    AMOUNT_STEP: ("costs",),
    ADDRESS_STEP: ("costs", "estimate"),
    REVIEW_STEP: ("estimate",),
}

#: Every key screen_furniture() answers, so a caller gets the same dict shape for
#: every step and a template can ask for any of them without a `default`. Derived
#: from the table rather than typed again (rule 11's shape): a piece of furniture
#: added above arrives here with no edit, and one removed cannot linger as a key
#: nothing sets.
FURNITURE_KEYS: tuple[str, ...] = tuple(
    sorted({name for names in _SCREEN_FURNITURE.values() for name in names})
)

#: What the amount step was given, so step 3 can echo the side the customer typed
#: rather than silently converting it. "send" means they typed what they are
#: sending; "receive" means they typed what they want back and
#: chains/amount_solve.deposit_for_desired_payout() solved the deposit.
AMOUNT_SIDES = ("send", "receive")


def step_by_number(number: int) -> dict:
    """The STEPS entry with this number. KeyError-equivalent rather than a default.

    IndexError on an unknown number is deliberate and is not laziness: a caller
    asking for step 9 has a bug, and returning step 1 would render the first
    screen over a customer's half-finished answers -- which looks like the flow
    resetting itself for no reason.
    """
    for step in STEPS:
        if step["number"] == number:
            return step
    raise IndexError(f"no wizard step numbered {number}; STEPS runs 1..{len(STEPS)}")


def source_lamps(rows: list[dict]) -> list[dict]:
    """Step 1's lamps: can this coin be SENT? Outbound directions only.

    THE COMBINED LAMP IS THE WRONG LAMP HERE, and services/pair_view.asset_rollups()
    says why in its own docstring before this file existed: an asset can be
    "perfectly good as a SOURCE and unusable as a DESTINATION", and ICP is measured
    to be exactly that -- ICP -> BTC/GRC/LTC all quote, and nothing -> ICP does,
    because a payout needs the desk's dfx identity where a read does not.

    So the combined rollup shows ICP amber. On step 1 the customer's question is
    only "can I send this", whose answer for ICP is an unqualified yes. Painting it
    amber there would make them hesitate over a direction that works, and painting
    the same amber on step 2 would understate a direction that does not work at
    all. One lamp cannot answer two questions with opposite answers.

    Derived from the rows pair_view already built, never re-evaluated -- a second
    evaluation of serviceability is the defect that module's header records.
    """
    assets = sorted({row["from_asset"] for row in rows} | {row["to_asset"] for row in rows})
    lamps = []
    for asset in assets:
        out = [row for row in rows if row["from_asset"] == asset]
        # `serviceable`, not `enabled`. KeyError rather than .get() for the reason
        # pair_view states: a row shape missing the verdict must fail loudly instead
        # of rolling up as unavailable and looking like news.
        ok = [row for row in out if row["serviceable"]]
        if not out or not ok:
            key = "none"
        elif len(ok) == len(out):
            key = "all"
        else:
            key = "some"
        lamps.append({
            "asset": asset,
            "key": key,
            # level/word/note come from pair_view's table, so this lamp and the one
            # on the page it replaces cannot disagree about what a colour means.
            "level": _LAMP_VOCABULARY[key]["level"],
            "word": _LAMP_VOCABULARY[key]["word"],
            "available": len(ok),
            "total": len(out),
            "selectable": bool(ok),
            # THE COIN'S IDENTITY, READ FROM services/asset_identity.py RATHER
            # THAN SPELLED IN THE TEMPLATE. Rule 8: five surfaces show a coin,
            # and a symbol written into one Jinja file is a symbol the other
            # four do not have -- with nothing failing, because a missing glyph
            # renders as an empty span. The step-2 options below carry the same
            # three keys from the same three functions.
            "symbol": symbol_for(asset),
            "symbol_title": symbol_title_for(asset),
            "color_class": color_class_for(asset),
            "detail": (
                f"{asset}: {len(ok)} of {len(out)} directions out of {asset} can be quoted right now"
                if out
                else f"{asset}: no outbound direction exists at all"
            ),
        })
    return lamps


def unavailable_note() -> str:
    """Why a greyed destination is greyed, in words a CUSTOMER can act on.

    WHAT IT REPLACES, AND IT WAS A DISCLOSURE PROBLEM AS WELL AS A READABILITY ONE.
    destinations_for() used to pass `row["reason"]` straight through, so screen 2
    rendered, to an unauthenticated reader, on the public port:

        BTC -> SOL: SOL has no adapter in this process: SOL_RPC_URL is unset (or 0)
        in the environment this process was started with. Nothing in the serving
        path reads a .env, so it has to be exported in the shell that starts the
        server -- a value set only in a file, or only in another shell, does not
        reach here.

    That is the same text tests/test_customer_page_layout.py::
    test_no_operator_facing_remedy_text_reaches_the_customer_page was written to
    keep off the customer page, and it kept passing because it only GETs `/` --
    screen 2 is reachable only by a POST. The guard was right and could not see here.

    NOTHING LEAVES THE SYSTEM'S REPORTING, which is rule 14's condition and was
    checked before this was written rather than assumed. Measured 2026-10-09 with
    two chains up and four down: screen 2 could show 16 distinct reasons, and every
    one is on /admin -- the four per-chain causes verbatim, and the other twelve as
    the two per-chain halves they are concatenated from ("<source cause> Also:
    <destination cause>"). A first pass at that check compared whole strings and
    scored twelve as missing; the halves were there all along.

    ONE SENTENCE FOR EVERY CAUSE, and the alternative was considered and refused.
    Naming the side -- "GRC cannot be paid out right now" -- reads better and is
    not always true: the same greyed tile is produced by the SOURCE being unable to
    take a deposit, and a sentence blaming the destination would then be wrong on a
    screen whose whole job is to tell a customer what is possible. "This direction"
    is true under every cause, and the direction is already the tile's label.

    NO ARGUMENT, DELIBERATELY. It takes neither asset because it says nothing about
    either, and a parameter nothing reads is a parameter a later reader will wire a
    claim into. If a cause ever becomes worth distinguishing FOR A CUSTOMER -- "this
    pair is retired" against "this pair is down" -- that is a new return value from
    the row, not a sentence assembled here.
    """
    return "this direction is not available right now. Pick another destination, or try again later"


def destinations_for(rows: list[dict], from_asset: str) -> list[dict]:
    """Step 2's options: what `from_asset` can become, with each one's own verdict.

    UNSERVICEABLE DESTINATIONS ARE RETURNED, NOT FILTERED OUT, and that is rule
    14's "never let an empty result print nothing" applied one level up. A customer
    who came looking to get BTC and finds BTC simply absent from the list cannot
    tell "this terminal does not do that" from "that direction is down right now",
    and the two deserve different reactions. They are returned with
    `selectable: False` and the reason, so the screen can show them greyed with
    the cause attached.
    """
    out = [row for row in rows if row["from_asset"] == from_asset]
    return sorted(
        (
            {
                "asset": row["to_asset"],
                "selectable": bool(row["serviceable"]),
                # Same three keys as source_lamps() above, from the same table.
                "symbol": symbol_for(row["to_asset"]),
                "symbol_title": symbol_title_for(row["to_asset"]),
                "color_class": color_class_for(row["to_asset"]),
                "reason": "" if row["serviceable"] else unavailable_note(),
            }
            for row in out
        ),
        key=lambda option: (not option["selectable"], option["asset"]),
    )


def current_step(answers: dict) -> dict:
    """Which screen to draw, given what has been answered. The decision.

    Returns the STEPS entry for the first step whose answer is missing, or step 6
    once a swap exists. `answers` is whatever the caller has collected; a key
    present but empty counts as UNANSWERED, because an empty string is what a
    submitted-but-blank form field gives and treating it as an answer would skip a
    step with nothing in it.

    A SWAP ID SHORT-CIRCUITS TO STEP 6 regardless of the other answers. Once
    create_swap() has returned, the answers that produced it are history and the
    only screen that can be correct is the deposit one -- a missing `confirmed`
    flag at that point would otherwise walk the customer back to the review of a
    swap that already exists.
    """
    if answers.get("swap_id"):
        return step_by_number(POINT_OF_NO_RETURN)
    for step in STEPS:
        needs = step["needs"]
        if needs is None:
            continue
        if not answers.get(needs):
            return step
    # Every answer collected and no swap id: the review is submitted and the
    # commit has not happened (or failed). Step 5 is the correct screen -- it is
    # where the error belongs and where the button lives.
    return step_by_number(POINT_OF_NO_RETURN - 1)


def may_go_back(step_number: int) -> tuple[bool, str]:
    """(allowed, reason-when-not). Back works up to the review and never after it.

    A REFUSAL WITH A SENTENCE RATHER THAN A HIDDEN BUTTON. The customer at step 6
    is looking at a deposit address they may already have copied; a back button
    that quietly did nothing, or that reset the flow and issued a second address,
    are both worse than one that says what it cannot do.

    On ICP the cost is concrete and not merely tidy: the address is derived from a
    subaccount index allocated by
    services/icp_subaccount_service.py's INSERT ... SELECT MAX(index)+1, and
    icp_deposit_subaccounts never reissues one. A discarded step-6 swap strands
    that index permanently.
    """
    if step_number >= POINT_OF_NO_RETURN:
        return False, (
            "This swap already exists and its deposit address has been issued, so there is nothing "
            "to go back to. Start a new swap if you need different figures -- and do not send to "
            "this address if you do."
        )
    if step_number <= 1:
        return False, "This is the first question."
    return True, ""


def progress(step_number: int) -> list[dict]:
    """The step strip: every step with its state, for the bar across the top.

    `done`/`current`/`future` rather than a percentage, because a percentage of a
    six-step flow is a number nobody acts on and the template already renders this
    vocabulary on the live swap page's stage strip.
    """
    return [
        {
            "number": step["number"],
            "question": step["question"],
            "state": (
                "done" if step["number"] < step_number
                else "current" if step["number"] == step_number
                else "future"
            ),
        }
        for step in STEPS
    ]


def screen_furniture(step_number: int) -> dict:
    """What this screen carries beside its question. Every key present, always.

    Returns one bool per FURNITURE_KEYS, so templates/atm.html can ask
    `furniture.costs` on any step without a default and without knowing which
    steps are in the table. A missing key rendering as falsey in Jinja is exactly
    the silent failure this shape removes: `{% if furniture.cost %}` -- one
    letter out -- would hide the whole right-hand column on every screen and
    nothing would fail, because an undefined attribute is false and an absent
    panel renders as absence (rule 14).

    AN UNKNOWN STEP GETS ALL-FALSE RATHER THAN RAISING, which is the opposite of
    step_by_number() twenty lines up and the difference is the point. That one is
    asked "which step is this?" and a wrong answer draws the wrong screen over a
    customer's answers, so it refuses. This one is asked "does this screen also
    show the fee table?", and the honest answer for a screen nobody listed is no.
    Raising here would turn a new step into a 500 on a page that would otherwise
    have rendered its question correctly.
    """
    carries = _SCREEN_FURNITURE.get(step_number, ())
    return {key: key in carries for key in FURNITURE_KEYS}


def both_sides(answers: dict, rate: float, fee_bps: int) -> dict:
    """Both legs of the trade from whichever one the customer typed. AN ESTIMATE.

    Returns {"send", "receive", "refusal", "side"}. `send` is in the source asset
    and `receive` in the destination; `refusal` is "" or the sentence to print
    instead of the figures.

    IT TAKES `answers` AND NOT SIX ARGUMENTS, and the first version took the six
    -- typed, side, rate, fee_bps, from_asset, to_asset -- with a `noqa: PLR0913`
    arguing they were all load-bearing. They are, and that was still the wrong
    shape: four of them are things this module already reads out of `answers`
    everywhere else (current_step, answers_after_back, every reject_*), so
    spelling them as parameters made ONE caller responsible for unpacking a dict
    in the same order this function would have put it back together. Rule 19 is
    explicit that a suppression is a claim you checked rather than a way past a
    finding, and rule 12 says the fix for a limit is to change the shape, not to
    raise the ceiling. The two genuine outsiders -- a rate read from a feed and a
    fee read from config -- stay as parameters, because this module reads neither
    and must not start.

    WHY THE SCREEN NEEDS THIS AND WHAT IT FIXES, which is a defect rather than a
    nicety. A customer who answers "I want 300 GRC" tells this flow the RECEIVE
    side, and until 2026-10-09 the deposit it solves for them was never shown
    before they committed: templates/_atm_confirm.html printed

        <dt>You send</dt><dd>&#8776; solved from what you want ICP</dd>

    -- a sentence where the number goes. routes/atm._commit() calls
    chains/amount_solve.deposit_for_desired_payout() AFTER the confirm button, so
    the one figure the customer has to put into a wallet was first visible on the
    swap page, after the swap existed. They agreed to send an amount nobody had
    told them. This function is what lets the address screen and the review state
    it, and _commit() still re-solves it from the same inputs rather than
    trusting anything carried through the browser.

    "ESTIMATE" IS NOT A HEDGE, IT IS THE ACCURATE WORD. `rate` is a read of the
    price feed by whoever called this; the rate a swap is priced at is fixed by
    services/quote_service.create_quote() at confirm, from its own read. The two
    are minutes apart at most and will usually agree to several digits, and
    "usually" is precisely why the screen has to say which it is holding (rule
    17). templates/_atm_costs.html prints the quote window beside it.

    THE SOLVERS ARE amount_solve's, NOT ARITHMETIC WRITTEN HERE. Each side has a
    rounding direction that file argues at length and gets opposite ways round;
    reimplementing either as `amount * rate` in a service would be the third
    spelling of a product that already exists twice, and the one that quietly
    rounds the wrong way (rule 8).
    """
    # DEFAULTED TO "send", THE SAME DEFAULT routes/atm._first_bad_answer() AND
    # _commit() APPLY TO THE SAME FIELD. A customer whose browser dropped the
    # radio is treated as having typed the send side by all three, so the figure
    # this screen shows is the figure the commit will use -- which is the only
    # property that matters when a display and an authority read one answer.
    side = answers.get("amount_side") or "send"
    if side not in AMOUNT_SIDES:
        # Same sentence reject_amount() gives for the same condition, built from
        # the same tuple, so a customer cannot be told two things about one field.
        return {
            "side": side,
            "send": 0.0,
            "receive": 0.0,
            "refusal": f"Choose whether that figure is what you send or what you receive ({' or '.join(AMOUNT_SIDES)}).",
        }
    amount, bad = amount_as_number(answers.get("amount", ""))
    if bad:
        return {"side": side, "send": 0.0, "receive": 0.0, "refusal": bad}
    send, receive, refusal = _solved_legs(answers, rate, fee_bps, side, amount)
    return {"side": side, "send": send, "receive": receive, "refusal": refusal}


def _solved_legs(answers: dict, rate: float, fee_bps: int, side: str, amount: float) -> tuple[float, float, str]:
    """(send, receive, "") or (0.0, 0.0, why not). The solving half of both_sides().

    ITS OWN FUNCTION FOR THE REASON amount_as_number() IS, thirty lines down: with
    it inlined, both_sides() had SEVEN return statements against PLR0911's six.
    Rule 12 is explicit that a crossed limit means extracting the decision rather
    than raising the ceiling or adding a `noqa`, and rule 19 that a suppression is
    a claim you checked rather than a way past a finding. The split is also where
    the seam already was: above this line is "which side did they mean and is the
    figure usable", below it is "what do the solvers say".

    FIGURES OR A REASON, NEVER BOTH. Every refusal path zeroes both legs, so a
    caller reading `send` without checking `refusal` gets 0.0 rather than a
    plausible number that means nothing -- the shape rule 12 names as "the caller
    cannot tell the failure from a real answer". The first version passed the
    typed figure through on the forward path's refusal and
    test_an_unpriceable_pair_refuses_rather_than_printing_a_zero caught it.
    """
    if side == "receive":
        send, refusal = deposit_for_desired_payout(
            amount, rate, fee_bps, answers.get("from_asset", "")
        )
        if refusal:
            return 0.0, 0.0, refusal
        # THE RECEIVE LEG IS WHAT THE SOLVED DEPOSIT BUYS, NOT WHAT WAS ASKED FOR,
        # and the difference is small, real, and the swap row's.
        #
        # deposit_for_desired_payout() rounds the deposit UP, so the payout it
        # produces is a hair ABOVE the figure typed -- that is its whole contract,
        # "send X to receive AT LEAST Y". Measured on the live flow 2026-10-09 for
        # "I want 300 GRC" out of BTC: the solved deposit is 0.00015229 BTC and
        # services/quote_service.create_quote() stamps output_amount_estimate =
        # 300.0113 on the swap. Echoing the typed 300.0 here would put a number on
        # the review that the row created one click later does not contain, which
        # is rule 8 on the two figures a customer checks afterwards.
        #
        # So both legs come out of the same forward function the quote applies,
        # and the screen and the row agree by construction rather than by luck.
        receive, forward_refusal = payout_for_deposit(
            send, rate, fee_bps, answers.get("to_asset", "")
        )
        return (0.0, 0.0, forward_refusal) if forward_refusal else (send, receive, "")
    receive, refusal = payout_for_deposit(amount, rate, fee_bps, answers.get("to_asset", ""))
    return (0.0, 0.0, refusal) if refusal else (amount, receive, "")


# =============================================================================
# PER-STEP REFUSALS. Each takes what the caller has already read and returns the
# sentence to show, or "" to go on. Separate from current_step() because that one
# answers "which screen" and these answer "is this answer usable" -- two questions
# whose answers differ (a bad address keeps you on step 4; a missing one puts you
# there), and collapsing them is how a flow starts rejecting answers by sending
# the customer somewhere unexplained.
#
# NO I/O HERE. The caller reads the lamps, the destination list and the address
# verdict; these only judge. That is what lets the whole flow be tested without a
# Flask client, an adapter or a chain.
# =============================================================================


def reject_source(asset: str, lamps: list[dict]) -> str:
    """"" when `asset` may be sent, else why not."""
    if not asset:
        return "Pick the coin you are sending."
    match = next((lamp for lamp in lamps if lamp["asset"] == asset), None)
    if match is None:
        return f"This terminal does not swap {asset}."
    if not match["selectable"]:
        # The lamp's own sentence, rather than a second wording of it. A customer
        # who reads "0 of 5 directions out of ICP can be quoted right now" on the
        # tile and something different on the error has been told two things.
        return match["detail"]
    return ""


def reject_destination(asset: str, options: list[dict]) -> str:
    """"" when `asset` may be received from the chosen source, else why not."""
    if not asset:
        return "Pick the coin you want back."
    match = next((option for option in options if option["asset"] == asset), None)
    if match is None:
        return "That pair is not one this terminal swaps."
    if not match["selectable"]:
        return match["reason"]
    return ""


def amount_as_number(typed: str) -> tuple[float, str]:
    """(amount, "") or (0.0, why it is not a usable number).

    ITS OWN FUNCTION BECAUSE reject_amount() CROSSED PLR0911 (7 returns > 6) WITH
    IT INLINE, and rule 12 is explicit that the fix is to extract the decision
    rather than raise the ceiling or add a noqa. It is also the half worth calling
    alone: a caller that wants the number AND the refusal gets both without
    re-parsing, which is how a template ends up with its own float() call.

    A BLANK AND A NON-NUMBER ARE DIFFERENT SENTENCES. "Type an amount" is for
    somebody who has not answered; naming the text back is for somebody who
    answered with "1,5" or "1.0.0" and needs to see what was read.
    """
    if not str(typed).strip():
        return 0.0, "Type an amount."
    try:
        amount = float(typed)
    except (TypeError, ValueError):
        return 0.0, f"{typed!r} is not a number."
    if amount <= 0:
        return 0.0, "An amount has to be more than zero."
    return amount, ""


def reject_amount(typed: str, side: str, ceiling: float, ceiling_reason: str) -> str:
    """"" when the typed amount is usable, else why not. Does NOT price anything.

    `ceiling` is the maximum DEPOSIT the desk can honor, already solved by
    chains/amount_solve.max_deposit_for_capacity(), and `ceiling_reason` is its
    refusal if it had one. Checked here rather than at the confirm screen because
    services/quote_service.py performs no capacity check at all and
    services/swap_service.create_swap() performs it at the very end -- so without
    this the customer answers every question and is refused on the last screen,
    which is the dead end an ATM exists not to have.
    """
    if side not in AMOUNT_SIDES:
        return f"Choose whether that figure is what you send or what you receive ({' or '.join(AMOUNT_SIDES)})."
    amount, bad = amount_as_number(typed)
    if bad:
        return bad
    if ceiling_reason:
        # AN UNREADABLE CEILING DOES NOT BLOCK, AND THE FIRST VERSION OF THIS
        # FUNCTION RETURNED `ceiling_reason` HERE AND DID.
        #
        # That was wrong in two ways and the second is the one that matters. It
        # contradicted the screen it governs: templates/_atm_amount.html says, for
        # exactly this case, "You can still type an amount; it is checked again
        # before anything is created" -- so the page invited an answer the
        # validator then refused. Two of mine disagreeing on one screen, which is
        # the defect class this session has been finding all evening.
        #
        # And it inverts payout_capacity's own distinction, which I was careful
        # about everywhere else in this file. largest_fundable_payout() returns
        # -1.0 for NOT ESTABLISHED precisely because 0.0 is a legitimate answer
        # for an empty wallet "and the two must not render the same way". A price
        # feed that did not answer, or a balance RPC that timed out, is not the
        # desk refusing -- it is nothing having been measured.
        #
        # FAIL-CLOSED IS NOT FREE HERE, which is the argument for passing. The
        # authority on whether a swap may exist is
        # services/swap_service.create_swap(), which runs the real capacity gate
        # at creation and refuses then. Blocking at step 3 on an unreadable
        # balance adds no safety the authority does not already provide, and costs
        # a hard stop on a transient RPC failure -- for a figure this screen itself
        # calls a hint.
        #
        # A CEILING OF ZERO WITH NO REASON IS DIFFERENT and still blocks, below:
        # that is a measurement saying the desk holds nothing of the destination
        # asset, and sending into it would take a deposit nothing can pay out.
        return ""
    if ceiling == 0:
        return (
            "This desk cannot pay out any of the coin you asked for right now, so no amount of "
            "what you are sending can be swapped for it."
        )
    if side == "send" and amount > ceiling:
        return (
            f"The most this desk can take right now is {ceiling}, because that is all it can pay out "
            f"on the other side. Send that or less."
        )
    return ""


def reject_payout_address(address: str, refuses: bool, reason: str) -> str:
    """"" when the address can receive, else the verdict's own sentence.

    `refuses`/`reason` come from modules/address_authority.check_address(), read
    by the caller. The verdict is NOT re-derived here: that module is the one
    authority on whether a string is an address on a chain, and a second opinion
    in a wizard is rule 8's shape on the field that decides where money lands.
    """
    if not address.strip():
        return "Type the address you want paid."
    if refuses:
        return reason or "That address cannot receive on this chain."
    return ""


def answers_after_back(answers: dict, target: int) -> dict:
    """`answers` with the target step's answer and everything after it cleared.

    A NEW DICT, not a mutation, so a caller can render the old and new side by side
    and a test can assert the input was not touched.

    WHY CLEARING IS NECESSARY AND NOT TIDINESS. current_step() returns the first
    step whose answer is missing. Go back to step 2 while step 2's answer is still
    set and it is not missing, so the flow bounces straight forward again -- a back
    button that visibly does nothing, which is rule 13's "a stop that cannot prove
    it worked" wearing a different hat.

    AND EVERYTHING AFTER IT, WHICH IS THE HALF THAT IS EASY TO MISS. Returning to
    step 1 to change the source while `to_asset` is still set leaves a pair the
    customer never chose -- ICP->GRC becomes BTC->GRC silently, with the amount and
    the quote still sized for the old one. The later answers are not merely stale,
    they are answers to questions that no longer apply.

    THE QUOTE ALWAYS GOES, AND IT NEEDS ITS OWN LINE. A quote is priced for one
    pair and one size; carrying it past any edit would let create_swap() be called
    with a quote that does not match the screen the customer just agreed to. It is
    popped explicitly because `quote_id` is no step's `needs` -- the loop above
    cannot reach it.

    `confirmed` NEEDS NO SUCH LINE AND USED TO HAVE ONE. Step 5's `needs` IS
    "confirmed", so the loop already clears it for every target from 1 to 5, and
    those are every target may_go_back() permits. The explicit pop was therefore
    dead on every reachable path -- found 2026-10-07 by a mutation that deleted it
    and passed all seventeen tests, which is what a surviving mutation is for.
    Kept as a comment rather than as a line, because consent to figures that have
    changed is not consent and the next reader should know where that is enforced
    (rule 9: delete the dead code, keep the reasoning).
    """
    cleared = dict(answers)
    for step in STEPS:
        if step["number"] >= target and step["needs"]:
            cleared.pop(step["needs"], None)
    cleared.pop("quote_id", None)
    # amount_side is not a STEPS answer -- it is HOW step 3 was answered -- so it
    # is cleared with the amount rather than surviving it, or a customer who typed
    # a receive-side figure and went back finds the box labelled for the other side.
    if target <= AMOUNT_STEP:
        cleared.pop("amount_side", None)
    return cleared
