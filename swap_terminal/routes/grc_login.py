"""The address-proof panel: issue a challenge, take a signature, say what happened.

Role: submodule (HTTP handlers; every decision is in
      services/grc_login_service.py and chains/grc_message_signing.py)
Reads: swap_terminal.db (swaps, address_proof_challenges) and -- on the POST
      only -- one read-only RPC method, `verifymessage`, on our own Gridcoin
      daemon.
Writes: swap_terminal.db (address_proof_challenges). The GET writes at most one
      row: it issues a challenge if the swap has no outstanding one. The POST
      writes at most one row: the conditional UPDATE that records a proof.
Can move funds: NO, and not by inheritance either. Neither handler can send,
      sign, unlock or cancel anything. Recording an address as proven does not
      change what gets paid, where, how much, or whether a swap may proceed --
      see services/grc_login_service.py's "THIS IS NOT A GATE" section, which
      says why that boundary is the operator's to cross and not this file's.
Mainnet-safe: yes. The one RPC method reachable from here is pure signature
      math and mutates nothing on any daemon.

=============================================================================
A GET THAT WRITES, NAMED RATHER THAN HIDDEN
=============================================================================

Rendering the panel ISSUES a challenge, so `GET /swap/<id>/address-proof` can
insert a row. That is unusual enough to state out loud.

It is a GET because the panel has to be reachable by following a link and by a
plain page load with no JavaScript -- the same two reasons routes/ui.py gives
for the status page being a server-rendered URL. And it is nearly idempotent in
practice: issue_challenge() REUSES an outstanding, unexpired challenge rather
than minting a new one, so a reload, a back button or a poll writes nothing.
Only the first view inside each 15-minute window inserts.

The alternative -- a POST to start -- would have meant a customer with
scripting off cannot begin, and a challenge nobody can get is a feature nobody
can use.

=============================================================================
WHY THIS IS A SEPARATE BLUEPRINT AND THE PARTIAL IS NOT WIRED IN
=============================================================================

The panel is built as a standalone partial (templates/_grc_address_proof.html)
plus its own stylesheet (static/grc_address_proof.css), and it is NOT included
into templates/swap.html. That is a coordination decision, not a design one:
another agent is restructuring index.html, swap.html, _swap_live.html,
_badges.html, _copy_field.html, base.html and styles.css into a boxed layout at
the time this was written, and editing those would have collided and lost one
side's work.

So this blueprint serves the panel at its own URL, through
templates/grc_address_proof_page.html -- a self-contained page that extends
nothing, so it does not depend on base.html's shape either. The panel is
reachable and testable now, and the one-line include that puts it on the swap
page is named in this commit's report rather than guessed at here.

=============================================================================
WHAT THE HTTP STATUS MEANS, AND WHY IT IS NOT ALWAYS 200
=============================================================================

The brief this was written to requires that a failed verification and an
unreachable daemon be distinguishable -- "your signature did not verify" and
"we could not check right now" are different sentences and different outcomes.
That distinction is in the rendered page, and it is ALSO in the status line,
because a status code is what a reverse proxy's log, a monitor and a `curl -i`
can see:

  200  the challenge was proven, or the panel was simply rendered
  400  we checked, or we read the submission, and it is refused. The answer is
       about what was pasted.
  503  WE COULD NOT CHECK. No verdict was reached, nothing was refused, and the
       challenge is still valid.

A refusal returning 200 would make an outage and a wrong signature identical to
everything outside the browser, which is CLAUDE.md rule 13's "skipped plus
success in the same output is a defect in the output" applied to HTTP.
"""

import logging

from db import get_db
from flask import Blueprint, current_app, render_template, request
from services.grc_login_service import (
    OUTCOME_PROVEN,
    PROOF_ASSET,
    UNAVAILABLE_OUTCOMES,
    Submission,
    proof_panel,
    verify_address_proof,
)
from services.swap_service import get_swap

logger = logging.getLogger(__name__)

bp = Blueprint("grc_login", __name__)

# The three statuses, as a mapping from the outcome vocabulary rather than a
# chain of `if`s in the handler. One place, so the page's `data-outcome` and the
# status line cannot come to disagree about whether something was refused or
# merely unanswerable (rule 8).
HTTP_OK = 200
HTTP_REFUSED = 400
HTTP_CANNOT_CHECK = 503


def status_for(outcome: str) -> int:
    """The HTTP status one outcome deserves. A decision, so it is a function (rule 10).

    Callable with a bare string in a test, which is how
    tests/test_grc_address_proof.py checks that an unreachable daemon and a
    refused signature do not share a status -- the distinction the brief required,
    asserted on the thing a monitor outside the browser can actually see.
    """
    if outcome == OUTCOME_PROVEN:
        return HTTP_OK
    if outcome in UNAVAILABLE_OUTCOMES:
        return HTTP_CANNOT_CHECK
    return HTTP_REFUSED


@bp.get("/swap/<swap_id>/address-proof")
def address_proof_page(swap_id: str):
    """The panel, with a live challenge. Issues one if the swap has none outstanding.

    A swap that does not exist gets a 404 and says so in words, the same way
    routes/ui.py::swap_page() does -- and for the same reason it discloses
    nothing about why: an id that was never issued and an id that was mistyped
    are the same answer here.

    It must also 404 rather than issue a challenge, and that is not merely
    tidiness: address_proof_challenges.swap_id carries a FOREIGN KEY to swaps(id),
    so an insert for an unknown swap would fail at the database with an
    IntegrityError and render a 500 for what is an ordinary wrong URL.
    """
    swap = get_swap(get_db(), swap_id)
    if not swap:
        return render_template("swap_not_found.html", swap_id=swap_id), 404
    panel = proof_panel(get_db(), swap_id)
    return render_template("grc_address_proof_page.html", panel=panel)


@bp.post("/swap/<swap_id>/address-proof")
def submit_address_proof(swap_id: str):
    """Check a pasted address and signature against the challenge, and re-render.

    THE RESULT IS RENDERED, NOT DISCARDED. That sentence is in this docstring
    because the failure it names is the one that keeps surviving in this
    codebase: a correct decision function whose caller throws the answer away,
    which looks exactly like a working feature until somebody checks whether the
    page changes. tests/test_grc_address_proof.py mutation-tests this handler by
    dropping `result=outcome` from the render call, and the test that catches it
    is the one asserting the page says what happened.

    NOTHING ABOUT THE SUBMITTED VALUES IS LOGGED HERE. Not the signature, which
    is replayable, and emphatically not a paste that turned out to be key
    material -- services/grc_login_service.py decides that and logs the SHAPE
    only. This handler deliberately has no logging call carrying any field of the
    submission, because `logger.info("...%s", value)` leaves the value in
    `record.args` whether or not a handler ever formats it.
    """
    swap = get_swap(get_db(), swap_id)
    if not swap:
        return render_template("swap_not_found.html", swap_id=swap_id), 404

    # request.form, not get_json: the form has to work with scripting off, which
    # is the same requirement routes/ui.py's header sets out for the status page.
    submission = Submission(
        challenge=request.form.get("challenge", ""),
        address=request.form.get("address", ""),
        signature=request.form.get("signature", ""),
    )
    # .get(), so a chain with no configured RPC yields None rather than a
    # KeyError. The service turns None into its own outcome -- "we could not
    # check", not "your signature failed" -- because an unconfigured daemon is our
    # problem and not the customer's. This is the shape routes/ui.py's header
    # records getting wrong on 2026-09-26, where a page claimed six pairs were
    # ENABLED on a server that had built exactly one adapter.
    adapter = current_app.config["ADAPTERS"].get(PROOF_ASSET)
    outcome = verify_address_proof(get_db(), adapter, swap_id, submission)

    panel = proof_panel(get_db(), swap_id, result=outcome)
    return render_template("grc_address_proof_page.html", panel=panel), status_for(outcome.outcome)
