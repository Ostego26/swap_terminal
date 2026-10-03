"""The start and stop buttons: two HTTP handlers over services/kill_switch.py.

Role: submodule (HTTP handlers only; every decision is in
      services/kill_switch.py and nothing is decided here)
Reads: the request's headers and peer address, the submitted form, and
      whatever services/kill_switch.switch_panel() reads on its behalf --
      supervisor's pid files, /proc, /proc/net/tcp, SWAP_TERMINAL_HOST and
      Config.DB_PATH
Writes: on a POST only, and only through services/kill_switch.operate():
      runtime/kill_switch.lock, worker pid files, worker logs, and SIGNALS to
      the processes those pid files name
Can move funds: **YES, INDIRECTLY.** POST with action=start spawns
      workers/payout_worker.py, which signs and broadcasts payouts. It is the
      only route in this application that can signal or spawn a PROCESS, and the
      only one that can arm something that broadcasts. It is NOT the only POST --
      /api/quotes, /api/swaps and /swap/<id>/address-proof were already here, and
      a first draft of this header said "the only write method on any web
      surface", which the url map refuted the moment a test asked it. None of
      those three spawns anything or sends a transaction. `stop` moves nothing in
      either direction. The spawn warning must be read and
      acknowledged before a start, and the acknowledgment is of the warning's
      TEXT -- see services/kill_switch.acknowledgment_token().
Mainnet-safe: `stop` yes. `start` is exactly as mainnet-safe as a payout worker
      on this host, which is the thing the warning says out loud.

=============================================================================
WHY THIS IS ITS OWN BLUEPRINT AND NOT A ROUTE ON routes/admin.py
=============================================================================

routes/admin.py's header claims every route it registers is a GET, and
tests/test_web_surfaces.py asserts that over the app's REAL url map -- not by
reading the file -- so adding a POST there would fail the suite rather than
quietly loosening the claim. That test is correct and it should keep passing: the
operator's read-only picture is read-only, and the one surface that is not must
be separately named, separately reviewed, and separately findable by anyone
grepping for what can write.

So: `admin.*` endpoints remain GET-only, and `kill_switch.*` is the one
blueprint in this application whose POST can signal or spawn a process. A reader
asking "what on the web can start or stop something" gets one answer, and it is
this file. tests/test_kill_switch.py asserts it over the real url map, and lists
the three POSTs that were already here so a fourth cannot arrive unexamined.

=============================================================================
WHY A FORM POST AND NOT JSON, AND WHY THE ANSWER IS RENDERED RATHER THAN REDIRECTED
=============================================================================

A FORM, because a control surface whose stop button needs JavaScript to work is
a control surface that does nothing when a script fails to load -- and the
moment it is needed most is the moment something on this host is already
misbehaving. services/kill_switch.refuse_cross_origin() is written for a form
POST for the same reason, and its docstring says why it does not lean on a
content type that forces a CORS preflight the way operator_panel.py's JSON API
does.

RENDERED, NOT REDIRECTED, and this is the one place POST-then-redirect-then-GET
is the wrong pattern. The proof of a stop is perishable: `pid 4021 was signaled
and the operating system now reports it ABSENT` is a statement about a moment,
and a redirect throws it away and re-renders a page that can only say what is
true now. "Nothing is running" after a stop and "nothing was running before it"
are the two readings rule 13 is about, and only the POST response can tell them
apart. The cost is that a reload re-submits, which the browser warns about, and
which is also why the acknowledgment token is checked on every start rather than
once.

THE PANEL IS REBUILT AFTER THE ACTION, with the result carried through it. Not
the other way around: a page rendered before the stop would show three running
workers above a block saying they are gone.
"""

from flask import Blueprint, render_template, request
from services.kill_switch import ControlTarget, RequestFacts, operate, switch_panel

bp = Blueprint("kill_switch", __name__)

#: One URL for both methods, so a GET of the page and the POST that acts cannot
#: drift onto different guards -- services/kill_switch.control_refusals() is the
#: single implementation and both handlers below reach it through the same
#: RequestFacts.
CONTROLS_PATH = "/admin/controls"


def _facts() -> RequestFacts:
    """The four things about this request that decide whether it may act.

    ONE helper rather than the same line in both handlers: the failure that shape
    invites is a handler that passes the headers and forgets the socket scan,
    which refuses nothing and renders the buttons anyway -- a check present in the
    code and absent from the answer. See RequestFacts' own docstring.
    """
    return RequestFacts.observed(request.headers, request.remote_addr)


@bp.get(CONTROLS_PATH)
def controls_page():
    """Render the panel. Signals nothing, spawns nothing, writes nothing.

    A GET that is genuinely a GET: it reads pid files and /proc and renders. It
    does not take the lock -- taking a lock to render a page would let one
    operator's reload refuse another's stop.
    """
    return render_template("kill_switch_page.html", panel=switch_panel(_facts()))


@bp.post(CONTROLS_PATH)
def controls_action():
    """Do exactly one of two things, prove what happened, and render the proof.

    FOUR LINES OF LOGIC AND NONE OF THEM IS A DECISION. The order of the checks --
    is the action real, may this caller act, is anyone else mid-action, was the
    warning acknowledged -- is services/kill_switch.operate()'s, where it can be
    called with seeded facts and no socket. Rule 10: the decision is the smallest
    testable piece at the bottom, and a handler is the layer above it.

    `request.form.get(...)` with "" DEFAULTS RATHER THAN A 400. An absent action
    is refused by operate() with a sentence naming the two that exist, and an
    absent acknowledgment is refused with the warning re-rendered. A 400 would
    say "bad request" to an operator who needs to be told which of the two
    happened -- and a stop button that answers with a status code and no page is
    rule 14's silence on the surface least able to afford it.

    `target=ControlTarget()` IS PASSED EXPLICITLY, not left to operate()'s
    default. Both resolve to the same live target. It is written out because this
    is the call site where the real pid directory and the REAL worker commands --
    the payout worker that broadcasts -- are chosen, and a reader of this handler
    should be able to see that choice being made rather than inherit it from a
    default argument two files away.
    """
    result = operate(
        request.form.get("action", ""),
        _facts(),
        request.form.get("acknowledgment", ""),
        target=ControlTarget(),
    )
    # REBUILT AFTER THE ACTION AND CARRYING THE RESULT. The result is passed
    # THROUGH rather than re-derived from the fresh panel, which is the defect this
    # codebase keeps finding in the other direction: a correct function whose
    # caller throws the answer away, indistinguishable from a working feature until
    # somebody checks whether the page says anything.
    return render_template("kill_switch_page.html", panel=switch_panel(_facts(), result=result))
