"""The administrator's read-only surface. GET only, and that is structural.

Role: submodule (HTTP handlers; every decision is in services/admin_view.py)
Reads: swap_terminal.db (every table, SELECT only), current_app.config,
       supervisor.py's pid files, services/pricing.py's price cache (read on
       every render, fetched on none), and -- only on /api/admin/chains and
       /api/admin/peg -- one read-only RPC call per probeable chain and one
       price lookup per stablecoin
Writes: NOTHING
Can move funds: no. Every route this blueprint registers is a GET -- four of
       them now. There is no POST, no PUT, no PATCH and no DELETE here, so there
       is no HTTP method by which this surface could change anything, whatever a
       future template were to render. tests/test_web_surfaces.py asserts that
       over the app's real URL map rather than by reading this file, so the
       count going up does not need this sentence edited to stay checked.
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
from flask import Blueprint, current_app, jsonify, render_template
from services.admin_view import overview, probe_chains, probe_peg
from services.helpers import utc_now_iso

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
