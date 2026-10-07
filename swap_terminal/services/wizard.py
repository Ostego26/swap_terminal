"""Which step of the ATM flow is current, what it needs, and what it must not let you undo.

Role: submodule (decisions only -- pure functions over answers already collected;
      no database, no socket, no adapter call)
Reads: the answers dict a caller hands it, and pair rows
      services/pair_view.allowed_pair_rows() has already built
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

#: The first step a customer cannot leave by going back. See the module docstring:
#: at this point a swap row exists and a deposit address has been issued.
POINT_OF_NO_RETURN = 6

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
            "available": len(ok),
            "total": len(out),
            "selectable": bool(ok),
            "detail": (
                f"{asset}: {len(ok)} of {len(out)} directions out of {asset} can be quoted right now"
                if out
                else f"{asset}: no outbound direction exists at all"
            ),
        })
    return lamps


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
                "reason": "" if row["serviceable"] else (row.get("reason") or "this direction cannot be quoted now"),
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
