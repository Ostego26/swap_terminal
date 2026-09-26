"""The pages a customer looks at: the swap form and one swap's status.

Role: submodule (HTTP handlers; every decision is in services/swap_view.py)
Reads: swap_terminal.db (swaps, deposit_events, payouts) through
       services/swap_service.get_swap(), and current_app.config for the pair
       list and the quote TTL
Writes: nothing. Both handlers here are GETs and neither writes a row.
Can move funds: no. Creating a swap and creating a quote are POSTs and they are
       in routes/swaps.py and routes/quotes.py, unchanged; these two handlers
       only render.
Mainnet-safe: yes

WHY THE STATUS PAGE IS A SERVER-RENDERED URL AND NOT A JAVASCRIPT VIEW.

Two reasons, and the second is the one that matters on this system.

  It has to work with no JavaScript. A customer waiting on a deposit needs the
  address and the confirmation count; a page that renders those only after a
  fetch resolves shows a blank box to anyone with scripting off, a blocked CDN
  or a flaky connection -- and a blank box is rule 14's ambiguous gap, which
  here reads as "this swap does not exist".

  It has to be a LINK. A swap is a wait measured in blocks, so the customer
  closes the tab and comes back. /swap/<id> survives that; a single-page view
  holding the id in a variable does not.

The page polls itself with fetch() for live updates when JavaScript is
available, and the polling replaces the SAME server-rendered fragments with
freshly server-rendered ones -- it does not re-decide anything in the browser.
That is deliberate: the status vocabulary lives in services/swap_view.py, in
Python, beside the modules that write those statuses (rule 8).

WHY THE PAIR LIST IS SERVER-RENDERED FROM Config.ALLOWED_PAIRS.

The page this replaced carried this in static/script.js:

    const validTargets = { GRC: ["BTC", "LTC"], BTC: ["GRC"], LTC: ["GRC"] }

-- a hand-written second copy of Config.ALLOWED_PAIRS, in a language the server
cannot check, deciding what a customer is offered. It happened to agree on
2026-09-26. It is exactly rule 8's "bug with a delay on it": the day a pair is
enabled or disabled, one of the two copies is wrong, and the UI either hides a
live pair or offers one the API will refuse. The select options are now rendered
from the authority, and that copy is deleted.

AND ALLOWED_PAIRS WAS NOT THE WHOLE AUTHORITY, MEASURED 2026-09-26.

This section, and index.html's header beside it, both claimed that "a pair that is
not in the set is not rendered at all, so the form cannot offer something the
server would refuse." The first half was true and the second did not follow. The
operator was offered six pairs, every one badged ENABLED, on a server whose
process environment set no BTC_RPC_PORT, no LTC_RPC_PORT and no GRC_RPC_PORT --
so chains/registry.build_adapters() had constructed exactly one adapter, XRP. They
picked XRP -> GRC, got a priced quote, and got `No swap was created: 'GRC'`.

ALLOWED_PAIRS says what this terminal is WILLING to swap. The adapters dict says
what it can REACH. The page was reading the first and printing a claim about the
second, which is the "instrument reporting more than the run established" shape
this codebase keeps paying for. Both are now read, disabled pairs are LISTED with
the reason rather than hidden (admin.html already did it that way), and the select
offers only the pairs that could actually complete.

allowed_pair_rows() ITSELF NOW LIVES IN services/pair_view.py. It moved there the
moment /api/health needed the same answer: a health probe importing from the
customer surface would make a reader ask why, and "because that is where the
decision happened to be written" is rule 10's defect. The page reads it; it does
not own it.
"""

from db import get_db
from flask import Blueprint, current_app, redirect, render_template, request, url_for
from services.helpers import utc_now_iso
from services.pair_view import allowed_pair_rows, offerable_pairs
from services.swap_service import get_swap
from services.swap_view import swap_display

bp = Blueprint("ui", __name__)


@bp.get("/")
def index():
    pairs = allowed_pair_rows(current_app.config, current_app.config["ADAPTERS"])
    # The select iterates `offerable`; the list iterates `pairs`. Two names for two
    # jobs, filtered in Python rather than in the template so the rule stays where a
    # test can call it -- and through services/pair_view.offerable_pairs(), which
    # /api/health also uses, so the page and the endpoint cannot come to disagree
    # about which pairs can complete.
    offerable = offerable_pairs(pairs)
    return render_template(
        "index.html",
        pairs=pairs,
        offerable=offerable,
        quote_ttl_seconds=current_app.config["QUOTE_TTL_SECONDS"],
        fee_bps=current_app.config["DEFAULT_FEE_BPS"],
        tolerance_pct=current_app.config["AMOUNT_TOLERANCE_PCT"],
    )


@bp.get("/swap-lookup")
def swap_lookup():
    """Turn a typed swap id into a redirect to its page. A GET, and it reads nothing.

    It exists so the "already have a swap?" form works with JavaScript off: a
    plain GET form cannot build the path /swap/<id> by itself, and the
    alternative -- assembling the URL in the browser -- would put the one piece
    of routing this page needs into a language the server cannot check.

    An empty id goes back to the form rather than to a 404 for the empty string,
    because a blank submit is a slip and not a missing swap.
    """
    swap_id = (request.args.get("swap_id") or "").strip()
    if not swap_id:
        return redirect(url_for("ui.index"))
    return redirect(url_for("ui.swap_page", swap_id=swap_id))


@bp.get("/swap/<swap_id>")
def swap_page(swap_id: str):
    """One swap's status, fully rendered server-side.

    A missing swap returns 404 with a page that says so in words. It does NOT
    echo anything about why: an id that is not in the table and an id that was
    mistyped are the same answer here, and there is nothing to disclose about
    either.
    """
    swap = get_swap(get_db(), swap_id)
    if not swap:
        return render_template("swap_not_found.html", swap_id=swap_id), 404
    return render_template("swap.html", view=swap_display(swap, utc_now_iso()))


@bp.get("/swap/<swap_id>/fragment")
def swap_fragment(swap_id: str):
    """The live-updating part of the status page, re-rendered by the SERVER.

    This is what the page's poll fetches. Handing back rendered HTML rather than
    JSON is the whole point: the browser does not learn what `confirming` means,
    it just replaces a region. The status vocabulary stays in one language, in
    one file, next to the code that writes it.
    """
    swap = get_swap(get_db(), swap_id)
    if not swap:
        return render_template("swap_not_found_fragment.html", swap_id=swap_id), 404
    return render_template("_swap_live.html", view=swap_display(swap, utc_now_iso()))
