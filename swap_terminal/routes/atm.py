"""The ATM flow: one question per screen, the server deciding which screen.

Role: submodule (HTTP handlers; every decision is in services/wizard.py,
      services/pair_view.py and chains/amount_solve.py)
Reads: swap_terminal.db through create_quote/create_swap, current_app.config,
       and one balance per render of the amount step
Writes: a `quotes` row at step 3 and a `swaps` row at step 5, both through the
       same service functions routes/quotes.py and routes/swaps.py call. Nothing
       here writes SQL of its own.
Can move funds: NO ORDER IS SENT HERE. Step 5 creates a swap row and a deposit
       address; the payout is services/payout_service.py's, driven by the payout
       worker, exactly as for a swap made through the one-page form.
Mainnet-safe: yes to serve. It can take a customer's deposit instruction, which
       is what it is for; it signs nothing.

WHY THIS EXISTS AT /atm AND NOT AT / YET. Operator, 2026-10-07: "i really want
the UI to be like an ATM sequence of steps", and, asked whether to replace the
one-page form or run alongside it, "Replace it". It will. It is here first
because 143 tests across five files pin the current customer page, and a single
commit that both introduces this flow AND deletes that evidence leaves no way to
tell a wizard bug from a test deletion. The swap happens in its own commit, with
those tests triaged there: the ones pinning behavior this flow also has move, and
the ones pinning the old page's shape die with it (rule 2).

NO SESSION AND NO SERVER-SIDE FLOW STATE. The answers post forward as hidden
fields and the server re-decides the current step from them on every request.
Three reasons, and the third is the one that settles it:

  Config.SECRET_KEY defaults to "swap-terminal-dev", so a signed session cookie
  would be signed with a value anybody reading this repository knows.

  Flow state in SQL would be a second authority for how far a customer has got,
  and rule 15 allows exactly one. There is nothing to recover: until step 5 no
  row exists and an abandoned flow costs nothing.

  THE BROWSER CARRIES ANSWERS AND DECIDES NOTHING. Which step is current, whether
  an answer is usable, what the maximum is -- every one of those is a function
  call on this side. That is the same contract services/swap_view.py states on
  the live swap page: "Nothing is decided in your browser."

THE QUOTE IS CREATED AT STEP 3 AND CAN EXPIRE BEFORE STEP 5, which is a real
state rather than an edge case: QUOTE_TTL_SECONDS is the window and a customer
reading a confirm screen is exactly the person likely to pause. An expired quote
sends them back to the amount step with the price having moved, said plainly,
rather than failing at create_swap() with a message about a quote id.
"""

from chains.amount_solve import (
    deposit_for_desired_payout,
    max_deposit_for_capacity,
)
from db import get_db
from flask import Blueprint, current_app, redirect, render_template, request, url_for
from modules.address_authority import check_address
from services.pair_view import allowed_pair_rows
from services.payout_capacity import largest_fundable_payout
from services.pricing import derive_pair_rate, fetch_usd_prices
from services.quote_service import create_quote, get_network_fee_reserve
from services.swap_service import create_swap
from services.wizard import (
    AMOUNT_SIDES,
    REVIEW_STEP,
    STEPS,
    amount_as_number,
    answers_after_back,
    current_step,
    destinations_for,
    may_go_back,
    progress,
    reject_amount,
    reject_destination,
    reject_payout_address,
    reject_source,
    source_lamps,
)

bp = Blueprint("atm", __name__)

#: The answer fields this flow carries, in the order the steps collect them. A
#: TUPLE RATHER THAN request.form ITSELF, so a crafted POST cannot introduce a
#: field no step asked for and have it rendered back into the next page's hidden
#: inputs. Everything outside this list is dropped on the way in.
CARRIED = ("from_asset", "to_asset", "amount", "amount_side", "quote_id", "payout_address", "confirmed")


def collected() -> dict:
    """The answers this request carries, filtered to CARRIED and stripped.

    UPPERCASED FOR THE TWO ASSET FIELDS ONLY. create_quote() upper-strips its own
    pair arguments, so a lowercase `icp` would price correctly and then fail to
    match any lamp or destination option here -- the wizard would refuse an answer
    the API accepts, which is the two-authorities shape in miniature.
    """
    answers = {}
    for field in CARRIED:
        value = (request.values.get(field) or "").strip()
        if value:
            answers[field] = value.upper() if field in ("from_asset", "to_asset") else value
    return answers


def amount_ceiling(to_asset: str, from_asset: str, rate_hint: tuple[float, str]) -> tuple[float, str, float, str]:
    """(max deposit, refusal, payout ceiling, how the ceiling was read).

    THE DESK IS THE COUNTERPARTY ON EVERY PAIR, measured 2026-10-07: all 30 in
    Config.ALLOWED_PAIRS carry a settlement headline ending "this terminal settles
    it CUSTODIALLY, with no hashlock". So the binding limit on any swap is the
    desk's own inventory of the DESTINATION asset, and this is where the screen
    gets it.

    `rate_hint` is a rate already derived for display. It is a HINT and the word is
    deliberate: the quote at step 3 fixes the real rate, and this figure only sizes
    the box. A customer who types the stated maximum and finds the quote a hair
    different is reading two measurements taken a moment apart, which is what the
    screen says.
    """
    rate, rate_refusal = rate_hint
    if rate_refusal:
        # NO RATE MEANS NO MAXIMUM, and the reason travels rather than a zero. A
        # 0.0 ceiling would read as "the desk can pay nothing", which is a
        # different and much more alarming statement than "we could not price it".
        return 0.0, rate_refusal, 0.0, rate_refusal
    reserve = get_network_fee_reserve(current_app.config, to_asset)
    ceiling, how = largest_fundable_payout(current_app.config["ADAPTERS"], to_asset, reserve)
    fee_bps = int(current_app.config["DEFAULT_FEE_BPS"])
    max_deposit, refusal = max_deposit_for_capacity(ceiling, rate, fee_bps, from_asset)
    return max_deposit, refusal, ceiling, how


def render_step(answers: dict, error: str = "") -> str:
    """Draw whichever step current_step() says, with everything that screen needs.

    ONE RENDER FUNCTION AND NOT SIX HANDLERS, because the step is a decision and
    dispatching on it in the router would put that decision in two places -- the
    URL and current_step() -- which is the pair of authorities rule 8 is about.
    The template picks its partial by `step.key`, which is a lookup on a value
    this function was handed rather than a branch it made (base.html's header).
    """
    step = current_step(answers)
    rows = allowed_pair_rows(current_app.config, current_app.config["ADAPTERS"])
    lamps = source_lamps(rows)
    from_asset = answers.get("from_asset", "")
    options = destinations_for(rows, from_asset) if from_asset else []

    # THE CEILING IS READ ONLY WHEN THE AMOUNT SCREEN IS BEING DRAWN, because
    # reading it costs a price-feed request AND a get_balance() per render. Every
    # other step would pay that for a figure it does not show -- and step 1, the
    # screen every customer meets, would make two network calls before asking its
    # question. Rule 3's "prefer removing work to doing it faster".
    ceiling, ceiling_refusal, payout_ceiling, ceiling_how = 0.0, "", 0.0, ""
    if step["key"] == "amount":
        ceiling, ceiling_refusal, payout_ceiling, ceiling_how = amount_ceiling(
            answers.get("to_asset", ""), from_asset, _rate_hint(answers)
        )

    context = {
        "surface": "user",
        # THE LIMIT, AND THE SENTENCE WHEN THERE IS NO LIMIT TO STATE. Three
        # distinguishable cases reach the template and it must not collapse them:
        # a real maximum, a maximum of zero (the desk holds nothing of the
        # destination asset -- a real answer), and "could not be established"
        # (the feed or the balance did not read). payout_capacity's -1.0 sentinel
        # exists for exactly that third case (rule 13).
        "ceiling": ceiling,
        "ceiling_refusal": ceiling_refusal,
        "payout_ceiling": payout_ceiling,
        "ceiling_how": ceiling_how,
        "step": step,
        "steps": progress(step["number"]),
        "answers": answers,
        "carried": CARRIED,
        "error": error,
        "lamps": lamps,
        "options": options,
        "amount_sides": AMOUNT_SIDES,
        "back_allowed": may_go_back(step["number"])[0],
        "back_reason": may_go_back(step["number"])[1],
        "fee_bps": current_app.config["DEFAULT_FEE_BPS"],
        "quote_ttl_seconds": current_app.config["QUOTE_TTL_SECONDS"],
        "tolerance_pct": current_app.config["AMOUNT_TOLERANCE_PCT"],
        "settlement": next(
            (row["settlement"] for row in rows
             if row["from_asset"] == from_asset and row["to_asset"] == answers.get("to_asset")),
            None,
        ),
    }
    return render_template("atm.html", **context)


@bp.get("/atm")
def start():
    """Step 1, with nothing answered. A GET so the flow is linkable and bookmarkable."""
    return render_step({})


@bp.post("/atm")
def advance():
    """Take one answer, judge it, and draw the next screen -- or the same one with why not.

    A SINGLE POST TARGET rather than one per step. The step is decided from the
    answers, so a per-step URL would be a second claim about where the customer
    is, and the two would disagree the first time a browser re-posted an old form.
    """
    answers = collected()

    if request.values.get("back"):
        # GOING BACK CLEARS THE ANSWER BEING RETURNED TO AND EVERYTHING AFTER IT.
        # Keeping them would redraw the earlier question with its old answer
        # already selected and the later ones still set, so current_step() would
        # bounce straight forward again -- a back button that visibly does
        # nothing. Rule 13's "a stop that cannot prove it worked".
        allowed, reason = may_go_back(current_step(answers)["number"])
        if not allowed:
            return render_step(answers, reason)
        try:
            target = int(request.values.get("back", ""))
        except ValueError:
            # A non-numeric `back` is a crafted or mangled post, not a customer
            # action. Redrawing the current step unchanged is the honest answer:
            # nothing was asked for that can be done.
            return render_step(answers)
        return render_step(answers_after_back(answers, target))

    bad_step, error = _first_bad_answer(answers)
    if error:
        # THE FAILED ANSWER AND EVERYTHING AFTER IT ARE DROPPED, through the same
        # function the back button uses. Dropping only the failed one would leave
        # later answers to questions that are no longer settled -- an amount sized
        # for a pair the customer is being sent back to re-choose.
        return render_step(answers_after_back(answers, bad_step["number"]), error)

    if current_step(answers)["number"] == REVIEW_STEP and answers.get("confirmed"):
        return _commit(answers)

    return render_step(answers)


def _first_bad_answer(answers: dict) -> tuple[dict, str]:
    """The earliest step whose answer is unusable, and why. ({}, "") when all are.

    EVERY ANSWERED STEP IS JUDGED, IN ORDER, AND THE FIRST VERSION JUDGED ONE.
    That version asked current_step() which step was current and validated THAT
    -- but current_step() returns the first step whose answer is MISSING, so it
    named the question about to be asked rather than the answer just given. An
    unserviceable source therefore passed straight through: posting
    from_asset=SOL made step 2 current, step 2's own answer was empty, and the
    refusal rendered was "Pick the coin you want back." Measured 2026-10-07 by
    the flow test, which caught it as a title saying one thing and a heading
    saying another.

    THE SECOND HALF IS A SECURITY PROPERTY AND NOT TIDINESS. Judging a single
    step means a crafted POST that supplies every field at once has only its LAST
    step judged -- so from_asset=SOL with a valid address and confirmed=1 would
    reach _commit() with nothing having checked the pair. create_swap() has its
    own gates and would very likely refuse, but a flow that depends on the layer
    below it to catch what it was supposed to check is the arrangement this
    codebase keeps paying for. Walking the steps in order is also the same shape
    current_step() uses, so the two cannot disagree about what "answered" means.

    READS, AND DECIDES NOTHING ITSELF: the lamps, the destination list, the
    capacity ceiling and the address verdict are all fetched here and judged by
    services/wizard.py's reject_* functions, which is what lets every one of
    those judgments be tested without a Flask client.
    """
    rows = allowed_pair_rows(current_app.config, current_app.config["ADAPTERS"])
    for step in STEPS:
        needs = step["needs"]
        if not needs or not answers.get(needs):
            # Not answered yet, so there is nothing to judge and nothing after it
            # can have been answered legitimately either.
            break
        if step["key"] == "from_asset":
            error = reject_source(answers["from_asset"], source_lamps(rows))
        elif step["key"] == "to_asset":
            error = reject_destination(
                answers["to_asset"], destinations_for(rows, answers.get("from_asset", ""))
            )
        elif step["key"] == "amount":
            ceiling, refusal, _, _ = amount_ceiling(
                answers.get("to_asset", ""), answers.get("from_asset", ""), _rate_hint(answers)
            )
            error = reject_amount(
                answers["amount"], answers.get("amount_side", "send"), ceiling, refusal
            )
        elif step["key"] == "payout_address":
            verdict = check_address(answers.get("to_asset", ""), answers["payout_address"])
            # `why`, NOT `reason`, and I guessed `reason` first. AddressVerdict's
            # own docstring says `why` "is written to be read off a screen by an
            # operator, not parsed" and names the address, the encoding tried and
            # what failed -- which is exactly what this screen needs, so it is
            # passed through rather than replaced with a wording of my own.
            error = reject_payout_address(answers["payout_address"], verdict.refuses, verdict.why)
        else:
            error = ""
        if error:
            return step, error
    return {}, ""


def _rate_hint(answers: dict) -> tuple[float, str]:
    """(rate, "") for sizing the amount box, or (0.0, why it could not be read).

    NOT THE QUOTED RATE. The quote at confirm fixes the real one; this figure only
    sizes the box and the screen says so.

    IT NEVER RAISES, AND THAT IS A DEFECT I SHIPPED AND THE TEST CAUGHT. The first
    version called fetch_usd_prices() bare, which means RENDERING THE AMOUNT SCREEN
    made a live HTTP request to the price feed -- so the screen 500d whenever the
    feed was unreachable. Measured 2026-10-07 in this container: `OSError: Tunnel
    connection failed: 403 Forbidden`, and seven flow tests failed on it. The old
    one-page form never had this property; it priced only on POST to /api/quotes.

    A screen that dies because a price feed is down is worse than one that says it
    cannot size the limit: the customer can still type an amount and have it
    checked at confirm, where create_swap()'s own capacity gate is the authority
    anyway. So the failure is REPORTED, in the shape
    payout_capacity.largest_fundable_payout() already uses for the same situation
    -- a sentence rather than a number, so the caller prints the reason instead of
    a bare 0.0 that reads as a rate (rules 13 and 14).
    """
    try:
        prices = fetch_usd_prices(current_app.config["RATE_CACHE_SECONDS"])
        rate = derive_pair_rate(answers.get("from_asset", ""), answers.get("to_asset", ""), prices)
    except Exception as error:  # noqa: BLE001 -- checked: this is a NETWORK read whose every failure mode (DNS, TLS, proxy, timeout, a malformed payload) must degrade the screen rather than break it, and the caller CAN tell -- a reason is returned beside a zero rate and is rendered. The authority on whether a swap may be created is create_swap()'s own gate, not this hint.
        return 0.0, f"the price feed could not be read just now ({type(error).__name__}), so no maximum can be shown"
    if rate <= 0:
        return 0.0, f"no usable {answers.get('from_asset', '?')} -> {answers.get('to_asset', '?')} rate is available right now"
    return rate, ""


def _commit(answers: dict):
    """Create the quote and the swap, then hand off to the live page that already exists.

    STEP 6 IS /swap/<id> AND NOT A SIXTH TEMPLATE. That page already shows the
    deposit address, the attribution model, the confirmation count with its
    threshold explained, the stage strip and the live poll -- everything "send
    your coin" means. A second rendering of it would be two pages telling a
    customer where to send money, which is the duplication rule 8 is about on the
    one screen where disagreement costs the deposit.
    """
    db = get_db()
    amount, bad = amount_as_number(answers.get("amount", ""))
    if bad:
        return render_step(answers, bad)
    if answers.get("amount_side") == "receive":
        rate, rate_refusal = _rate_hint(answers)
        if rate_refusal:
            return render_step(answers, rate_refusal)
        amount, refusal = deposit_for_desired_payout(
            amount, rate, int(current_app.config["DEFAULT_FEE_BPS"]),
            answers.get("from_asset", ""),
        )
        if refusal:
            return render_step(answers, refusal)
    try:
        quote = create_quote(
            db, current_app.config, answers["from_asset"], answers["to_asset"], amount,
            adapters=current_app.config["ADAPTERS"],
        )
        swap = create_swap(
            db, current_app.config, current_app.config["ADAPTERS"], quote["id"],
            answers["payout_address"],
        )
    except Exception as error:  # noqa: BLE001 -- checked: every refusal on this path raises, and the caller is a customer who must be shown the sentence rather than a 500. The message is the service's own and says which gate refused; nothing is swallowed, because it is rendered.
        return render_step(answers, str(error))
    return redirect(url_for("ui.swap_page", swap_id=swap["id"]))
