"""The administrator's read-only surface. GET only, and that is structural.

Role: submodule (HTTP handlers; every decision is in services/admin_view.py)
Reads: swap_terminal.db (every table, SELECT only), current_app.config,
       supervisor.py's pid files, services/pricing.py's price cache (read on
       every render, fetched on none), and -- only on /api/admin/chains,
       /api/admin/peg and /admin/wallets/<asset>?ask, each of which has to be
       asked for -- read-only chain calls: one or two per probeable chain for
       the probe, one price lookup per stablecoin for the peg, and a bounded
       handful against ONE chain for the wallet panel
Writes: NOTHING
Can move funds: no. Every route this blueprint registers is a GET. There is no
       POST, no PUT, no PATCH and no DELETE here, so there is no HTTP method by
       which this surface could change anything, whatever a future template were
       to render. tests/test_web_surfaces.py asserts that over the app's real URL
       map rather than by reading this file.

       THAT SENTENCE USED TO END "-- four of them now" AND THE COUNT IS GONE
       rather than updated to six. It was a number in prose beside a routing
       table that grows, which is the drift CLAUDE.md rule 3 is about ("a count
       without what it was counted out of"), and worse, it invited exactly the
       edit it got: a reader adding a route updates the number and believes they
       have kept the file honest. The CLAIM is "no write verb is registered", the
       count was never evidence for it, and the url_map test is.
Mainnet-safe: yes

=============================================================================
THIS SURFACE IS UNAUTHENTICATED, AND THAT IS A REAL QUESTION, NOT A DETAIL.
=============================================================================

Nothing in this application authenticates a caller -- app.py's own exposure
warning has said so since 2026-09-24 -- and adding an authentication system was
explicitly out of scope for the change that built this page. So the protection
is the bind address and nothing else:

    SWAP_TERMINAL_HOST defaults to 127.0.0.1, so by default /admin is reachable
    only from the machine running the app. Setting it to anything else publishes
    this page, and every operational fact on it, to whoever can reach the port.

app.exposure_warnings() now names /admin specifically in that case, so an
operator who binds an interface reads it in the startup banner rather than
discovering it. What this page deliberately does NOT contain is any secret: the
config echo is an allowlist (services/admin_view.ECHOED_CONFIG_KEYS) precisely
because Config.RPC carries wallet credentials one key away from the endpoints,
and tests/test_web_surfaces.py asserts no RPC password reaches the rendered
page.

It does contain deposit addresses, payout addresses, txids, swap ids and
balances. Swap ids are the one thing worth naming: services/helpers.new_id()
documents an id as "the only thing standing between a stranger and
GET /api/swaps/<id>", and this page lists them. That is an argument for keeping
the bind on loopback, and it is the operator's call, not this file's.

WHY THE CHAIN PROBE AND THE PEG CHECK ARE SEPARATE ROUTES AND NOT PART OF THE PAGE.

Six chains at a 30s RPC timeout is a page that can take three minutes to load,
and a load like that is the blinking cursor rule 14 opens with -- the operator
cannot tell it from hung, and the resolution is to interrupt. So /admin renders
immediately from the database and the configuration, and reachability is asked
for deliberately. The page says, before anything is fetched, how many chains
will be contacted and that each contact is one read-only call.

The peg check is the same shape for the same reason, and the pricing panel is
the third instance of the rule: it reads services/pricing.py's cache -- the
numbers the quote path last fetched -- and contacts nothing, so a price API
being down costs the operator the pricing panel's freshness and not the whole
page.
"""

from db import get_db
from flask import Blueprint, current_app, jsonify, render_template, request
from services.admin_view import overview, probe_chains, probe_peg
from services.chain_panel import chain_panel
from services.helpers import utc_now_iso
from services.kill_switch import RequestFacts, control_refusals
from services.payout_rescue import apply_rescue, rescue_verdict

# ONE SPELLING OF /api/admin/chains' BODY, shared with the host-side reader in
# swap_stack.py. stack_authority is pure stdlib and live-safe to import (its own
# header says so), which is why the contract lives there rather than in
# services/admin_view.py -- `up` runs on the HOST and must not drag flask, the
# adapters or the supervisor in to learn one key name.
from stack_authority import chain_probe_envelope

bp = Blueprint("admin", __name__)


@bp.get("/admin")
def admin_page():
    """The whole read-only picture, server-rendered.

    Server-rendered rather than fetched for the same reason the customer's
    status page is: an operator looking at a blank region cannot tell an empty
    table from a query that broke, and a page that needs JavaScript to say
    anything is a page that says nothing when a script fails to load. Every
    region in the template renders `(none)` for an empty result.
    """
    return render_template(
        "admin.html",
        data=overview(get_db(), current_app.config, current_app.config["ADAPTERS"]),
    )


# THE CHAIN WALLET PANEL: ONE VIEW, TWO RULES, SIX CHAINS, AND NOTHING BUT GET.
#
# Operator, 2026-10-10: "basically recreate the gui wallets of btc, grc, and ltc, and
# we also need a control panel for xrp, sol, and icp that are cromulent" ... "this
# should be a tab under the admin and 6 sub tabs for each chain."
#
# THE BARE PATH IS THE FIRST CHAIN'S PANEL, which is what lets the operator tab strip
# in templates/_admin_tabs.html carry one stable href: a Jinja macro cannot know which
# chain comes first, because that is services/chain_panel.panel_assets()' answer and it
# depends on Config.RPC. named_or_first() is the decision and it is in that module, not
# here -- a route that picked a default would be the decision buried where it cannot be
# called with seeded inputs (rule 10).
#
# WHY SIX URLS AND NOT ONE PAGE WITH SIX PANELS: services/chain_panel.py's header
# carries both halves of that argument, the paste measurement from
# templates/admin.html and the twenty-minutes-of-RPC arithmetic from
# services/admin_view.py. Short version -- a page that hid five panels would return a
# fourteenth of itself when the operator pastes it, and a page that asked six daemons
# would be killed by the worker timeout before it said which one was down.
@bp.get("/admin/wallets")
@bp.get("/admin/wallets/<asset>")
def chain_wallet_page(asset: str = ""):
    """One chain's wallet panel. A GET, and asking the daemon is a GET too.

    `?ask` IS THE SOCKET BOUNDARY AND IT IS STILL A READ. Without it this page contacts
    nothing at all: it renders from Config.RPC, from the adapters that were constructed
    at startup, and from the pure capability tables. With it, the panel makes a bounded
    number of read-only calls to that one chain -- and it stays a GET for the same
    reason /api/admin/chains is one: it changes nothing, so it is safe to reload and it
    is not a "button that does something" in the sense the read-only constraint is
    about.

    THE ANSWER IS 404 FOR A CHAIN THAT HAS NO PANEL, WITH A SENTENCE IN THE BODY. The
    URL identifies nothing, so the status has to say so -- but a 404 body is a render
    and not an error path, and services/chain_panel.refuse_unknown_asset() names the
    six so the operator's next action is a click rather than a question (rule 14).

    IT NEVER RAISES ON A CHAIN THAT WILL NOT ANSWER. Every failure -- no adapter, a
    refused connection, a wallet that is not loaded, a budget that ran out, dfx missing
    from this image -- comes back inside the payload as that region's own sentence,
    because a diagnostic page that dies is a page that cannot be used to find out what
    is wrong.
    """
    data = chain_panel(
        current_app.config,
        current_app.config["ADAPTERS"],
        asset,
        # PRESENCE, NOT A VALUE. `?ask=1` and a bare `?ask` both mean the same thing on
        # purpose: testing for "1" would make `?ask=true` and `?ask=yes` render the
        # page that contacted nothing, with nothing on it saying the request was
        # ignored -- which is rule 14's "did nothing must not look like did work" at
        # the one place an operator is waiting to see whether a daemon answered. The
        # page echoes whether it asked, either way.
        ask="ask" in request.args,
    )
    return render_template("admin_chain.html", data=data), (404 if data["refusal"] else 200)


@bp.get("/api/admin/overview")
def admin_overview_route():
    """The same read-only picture as JSON, for a terminal or another tool.

    Exists so an operator can pipe it through `jq` instead of reading a table,
    and so a test can assert on values rather than on markup.
    """
    return jsonify(overview(get_db(), current_app.config, current_app.config["ADAPTERS"]))


@bp.get("/api/admin/chains")
def admin_chains_route():
    """Probe every configured chain, read-only, and say which answered.

    A GET, and it stays a GET: it changes nothing, so it is safe to reload, and
    it is not a "button that does something" in the sense the read-only
    constraint is about. It makes one read call per probeable chain, names the
    network the DAEMON reports rather than the one its port implies, and never
    raises -- services/admin_view.probe_chain() reports a failure in its return
    value so the table can render "did not answer" beside the reason.
    """
    adapters = current_app.config["ADAPTERS"]
    chains = probe_chains(adapters)
    # THE BODY IS BUILT BY stack_authority.chain_probe_envelope(), NOT BY A DICT
    # LITERAL HERE. It used to be a literal, and swap_stack.py's `up` read the
    # same endpoint expecting a bare list -- so step 7 printed "COULD NOT ASK ...
    # got dict" on a working probe for as long as it existed. One producer, one
    # key, and the host-side reader pulls the rows out through the same constant
    # (rule 8). static/admin.js is the third consumer and cannot share the symbol;
    # it names this function in a comment instead.
    return jsonify(chain_probe_envelope(chains, utc_now_iso(), len(adapters)))


@bp.get("/api/admin/peg")
def admin_peg_route():
    """Price the two stablecoins and say whether this page's dollar is a dollar.

    A GET, like the chain probe and for the same reason: it changes nothing, so
    it is safe to reload. Two HTTPS reads, no key, no chain, and the findings
    come from the same services/wallet_leveling.peg_findings() that
    `chain_balances.py --level` prints, so the two surfaces cannot disagree.

    Never raises -- services/admin_view.probe_peg() reports a feed that would
    not answer as suspect=True with the reason in `findings`, because a peg
    that could not be checked must not render like a peg that held.
    """
    return jsonify(probe_peg())


# =============================================================================
# THE FIRST CONTROL ON THIS SURFACE
# =============================================================================
#
# THE OPERATOR ASKED FOR THIS FIVE TIMES AND I DEFERRED TWICE, 2026-10-10:
#
#   "i asked for just a fucking control panel and i got a dumbass verbose pile of
#    shit that doesn't control anything or tell me anything useful really."
#   "no controls. no buttons. nothing."
#   "why have you been dancing the fuck around on trols. i have said i want a
#    fucking control panel for awhile now."
#
# They are right that it was dancing. The reasoning I kept giving -- that a write
# verb on an unauthenticated page is a custody decision -- is sound and does not
# apply to THIS action, and I should have separated the two instead of treating
# every button as the same button.
#
# WHY RESCUE IS THE ONE THAT GOES FIRST, AND WHY IT IS SAFE TO PRESS:
#
#   it signs nothing            this handler and the service under it call no send
#                               method and open no socket to a chain. They move a
#                               row from 'failed' to 'payout_pending'.
#   the gate is the safety      services/payout_rescue.rescue_verdict() refuses any
#                               swap it cannot PROVE was never broadcast: any live
#                               payout row, any txid on any row, an empty
#                               failed_reason, or a reason not in
#                               PRE_SIGNING_MARKERS. That gate is older than this
#                               button, is tested in 22 cases, and the button
#                               cannot bypass it -- it calls the same function with
#                               the same arguments the CLI does.
#   no passphrase, ever         nothing here collects one and nothing here needs
#                               one. The operator's standing instruction is that a
#                               passphrase must never appear in a command this
#                               repository emits and the panel must never have a
#                               field for one. That is untouched and is why
#                               wallet unlock is NOT a button.
#   it is what was needed       the swap this was built for is real:
#                               s_0dc53d06ab3968fb, a BTC deposit confirmed at
#                               2026-10-10T17:21:14 and a GRC payout that refused
#                               before signing four seconds later because
#                               GRIDCOIN_WALLET_PASSPHRASE was not in the worker's
#                               environment. 1000.08070022 GRC owed, nothing sent,
#                               and the fix was a command the operator had to be
#                               handed.
#
# WHAT IT DOES CAUSE: workers/payout_worker.py picks the swap up on its next cycle
# and broadcasts. So this is one step upstream of a send, exactly as the CLI is,
# and the handler says so on the page rather than in this comment alone.
#
# THE GUARD IS THE SAME ONE /admin/controls USES -- services/kill_switch's
# RequestFacts.observed() and control_refusals(), which refuse an off-box caller
# and a cross-origin post. Reused rather than re-derived: two implementations of
# "may this caller act" is the shape where a page offers a control the endpoint
# refuses, or worse renders a refusal over an endpoint that accepts (rule 8, and
# control_refusals' own docstring makes the same argument).


@bp.post("/admin/swaps/<swap_id>/rescue")
def rescue_swap_action(swap_id: str):
    """Hand one swap whose payout was refused before signing back to the payout worker.

    FOUR STEPS AND NONE OF THEM IS A DECISION THIS FUNCTION MAKES. May this caller
    act (kill_switch.control_refusals), may this swap be re-driven
    (payout_rescue.rescue_verdict), what gets written (payout_rescue.apply_rescue).
    Rule 10: the handler is the layer above the decision, and every one of those three
    is callable with seeded inputs and no socket.

    IT RENDERS THE ADMIN PAGE EITHER WAY, with the outcome on it. A refusal that
    answered with a status code and no page would be rule 14's silence on the surface
    least able to afford it -- the operator pressed a button about money and has to be
    told which of four things happened: refused caller, no such swap, refused swap, or
    done.

    COMMITS ONLY AFTER apply_rescue() RETURNS. The service does not commit, so a raise
    anywhere in it leaves the swap exactly as it was.
    """
    refusals = control_refusals(RequestFacts.observed(request.headers, request.remote_addr))
    db = get_db()
    swap = db.execute("SELECT * FROM swaps WHERE id = ?", (swap_id,)).fetchone()
    rows = db.execute(
        "SELECT * FROM payouts WHERE swap_id = ? ORDER BY id", (swap_id,)
    ).fetchall()

    if refusals:
        outcome = {"swap_id": swap_id, "done": False, "why": "; ".join(refusals)}
    elif swap is None:
        outcome = {"swap_id": swap_id, "done": False,
                   "why": f"no swap with id {swap_id} exists in this database"}
    else:
        allowed, reason = rescue_verdict(swap, rows)
        if not allowed:
            outcome = {"swap_id": swap_id, "done": False, "why": reason}
        else:
            written = apply_rescue(db, swap, reason, actor="the operator panel")
            db.commit()
            outcome = {
                "swap_id": swap_id,
                "done": written["moved"],
                "why": reason if written["moved"]
                       else "another caller moved this swap first; nothing was changed",
                "released": f"{written['released']} {written['asset']}",
            }
    return render_template(
        "admin.html",
        data=overview(get_db(), current_app.config, current_app.config["ADAPTERS"]),
        rescue=outcome,
    )
