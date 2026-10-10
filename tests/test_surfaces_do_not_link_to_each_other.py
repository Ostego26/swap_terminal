#!/usr/bin/env python3
"""The operator surface and the customer surface do not link to each other.

Role: file (entry point -- `python3 -m pytest tests/test_surfaces_do_not_link_to_each_other.py`)
Reads: the Flask app, rendered page HTML
Writes: nothing
Can move funds: no
Live-safe: yes

Operator, 2026-10-08: "operator and swap should not be acceabbile to eat other.
the operator page will be a secure page locally."

THE SECOND TIME THEY ASKED, AND THE FIRST TIME I DID HALF OF IT. When they said
"you can get to the swap terminal from the operator terminal . NO." I removed
the topnav from the operator shell, wrote a comment saying the customer
direction "was NOT what the operator objected to", and reported it done. Five
`<a href="{{ url_for('ui.swap_page', ...) }}">` cells in admin.html survived --
one per row of five different tables -- so the operator surface still linked
into the customer flow from every row of every swap table on it. The nav was the
crossing I could see; the rows were the ones an operator actually clicks.

WHY THIS IS A TEST AND NOT A COMMENT. A crossing is one `href` away, in either
of two directions, in any of eight templates, and nothing about adding one looks
wrong while you are writing it -- a swap id that is a link is the obvious thing
to write. Checking by reading is what produced the half-fix: I grepped for the
nav I remembered and not for `url_for`.

HOW IT CHECKS, and it is deliberately not a text match. It RENDERS each page
through the real app, pulls every URL-bearing attribute out of the HTML, and
resolves each one back through the app's own url_map to the endpoint it would
reach. The assertion is on the BLUEPRINT that endpoint belongs to. A text match
on "/admin" would miss `url_for('admin.admin_page')` producing a path that moved,
and would fire on the word appearing in prose; resolving the URL cannot do
either. It also means a route RENAMED still fails if it is linked across, which
is the drift a literal would let through.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "swap_terminal"))

import app as app_module  # noqa: E402 -- after the sys.path.insert, same as every test here
from services.chain_panel import panel_assets  # noqa: E402 -- same reason

#: Which blueprint belongs to which surface. The split is the app's own: every
#: endpoint is `<blueprint>.<view>`, so this needs no second list of paths to
#: drift from the routing table (rule 8).
#:
#: `static` and `health` are NEITHER. Both surfaces load the same stylesheet, and
#: /api/health is a liveness probe that belongs to no page -- calling either one
#: a crossing would make the rule unenforceable and get it deleted.
OPERATOR = frozenset({"admin", "kill_switch"})
CUSTOMER = frozenset({"atm", "ui", "grc_login", "quotes", "rates", "swaps"})
NEUTRAL = frozenset({"static", "health"})

#: What a seeded table cell renders as. Named so the fixture and the assertion
#: that the fixture WORKED cannot drift apart (rule 8).
SEEDED_ROW = "s_seeded_row"

#: Pages to render, and the surface each one is.
#:
#: /swap/<id> for an id that does not exist renders swap_not_found.html, which is
#: its own customer template and its own set of links -- so the 404 path is
#: covered rather than skipped for being inconvenient to seed.
#: THE CHAIN PANELS ARE DERIVED rather than listed, 2026-10-10. One page per chain and
#: the chains come from Config.RPC, so a hand-written six would stop covering the
#: surface the day a chain is added -- and a crossing added to admin_chain.html would
#: then be checked on five pages and missed on the sixth. Importing the authority costs
#: one line and cannot drift.
CHAIN_PAGES = tuple(
    (f"/admin/wallets/{asset}", "operator")
    for asset in panel_assets(dict(app_module.app.config))
)

PAGES = (
    ("/admin", "operator"),
    ("/admin/wallets", "operator"),
    *CHAIN_PAGES,
    ("/admin/controls", "operator"),
    ("/", "customer"),
    ("/swap-lookup", "customer"),
    ("/swap/s_does_not_exist", "customer"),
)

#: Attributes that carry a URL the page will actually fetch or follow.
#:
#: `data-*` is in here because this app navigates by it: swap.html carries
#: data-swap-fragment and admin.html carries data-probe-url and data-peg-url,
#: each read by a script that fetches it. A rule that only looked at href would
#: be satisfied by moving a crossing into a data attribute, which is the shape
#: of fix that makes a checker worthless.
URL_ATTRIBUTES = re.compile(
    r'(?:href|src|action|data-[a-z-]*(?:url|fragment|href))\s*=\s*"([^"]+)"', re.IGNORECASE
)


def _app():
    return app_module.create_app() if hasattr(app_module, "create_app") else app_module.app


@pytest.fixture
def client():
    with _app().test_client() as test_client:
        yield test_client


class _AnyField(float):
    """A table cell value that answers every attribute and formats as either shape.

    WHY THE ADMIN TABLES HAVE TO BE FILLED AT ALL. Rendered against an empty
    database, /admin returns 58,925 bytes containing 31 URLs and ZERO table rows
    -- nine empty-state blocks instead. Every `url_for` inside a `{% for %}` is
    therefore invisible to a check that only scans what rendered, which is
    exactly where the five crossings this test exists for were living. A
    mutation restoring one of them SURVIVED the first version of this file.

    So the rows are seeded and the real template renders them. This class is what
    makes that cheap: admin.html reads `row.swap_id`, `row.id`, `row.asset`,
    `row.status` and formats `row.amount` with `%.8f`, and a float subclass whose
    __getattr__ returns more of itself satisfies every one of those -- numeric
    where a number is formatted, "s_seeded_row" where a string is printed, and
    url_for builds `/swap/s_seeded_row` from it.

    It is deliberately NOT a realistic swap. The claim under test is "does this
    page link across", which depends on the template and not on the values; a
    fixture that modelled a real swap would be a second source of truth about
    swap shape, drifting from the first (rule 8).
    """

    def __new__(cls):
        return super().__new__(cls, 0.0)

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        return _AnyField()

    def __str__(self):
        return SEEDED_ROW

    __repr__ = __str__


@pytest.fixture
def filled_admin_tables(monkeypatch):
    """Make every empty list on /admin render one row, through the real template.

    THE MODULE IS FOUND THROUGH THE APP, not by name. This tree imports its own
    modules rootlessly (`from config import Config`), so `routes.admin` and
    `swap_terminal.routes.admin` are two DIFFERENT module objects with two
    different `overview` names -- patching the one you can spell in an import
    statement patches the copy the app is not using, and the first attempt at
    this fixture did exactly that and silently changed nothing. Resolving it from
    `view_functions` asks the app which module it actually calls.
    """
    view = _app().view_functions["admin.admin_page"]
    module = sys.modules[view.__module__]
    real = module.overview

    def seeded(*args, **kwargs):
        data = real(*args, **kwargs)
        return {
            key: ([_AnyField()] if isinstance(value, list) and not value else value)
            for key, value in data.items()
        }

    monkeypatch.setattr(module, "overview", seeded)
    return seeded


def _blueprint_of(url: str, adapter) -> str | None:
    """Which blueprint this URL would reach, or None if it leaves the app.

    Returns None for anything that is not a route of this application --
    absolute URLs, mailto:, bare fragments -- because those are not a crossing
    between two surfaces of this app and saying otherwise would be a false
    positive. A path that LOOKS internal and matches nothing raises instead, so
    a typo'd link cannot pass as "external".
    """
    if url.startswith(("http://", "https://", "mailto:", "data:", "#", "javascript:")):
        return None
    path = url.split("?", 1)[0].split("#", 1)[0]
    if not path.startswith("/"):
        return None
    endpoint, _args = adapter.match(path, method="GET", query_args="")
    return endpoint.split(".", 1)[0]


@pytest.mark.parametrize(("path", "surface"), PAGES)
def test_a_page_links_only_within_its_own_surface(client, filled_admin_tables, path, surface):
    """Render it, resolve every URL on it, and name the offender if there is one."""
    assert filled_admin_tables  # the fixture is the point, not a side effect
    response = client.get(path)
    assert response.status_code in (200, 404, 302), f"{path} -> {response.status_code}"
    adapter = _app().url_map.bind("localhost")
    forbidden = CUSTOMER if surface == "operator" else OPERATOR

    # A REDIRECT IS A CROSSING TOO, and /swap-lookup is why this branch exists:
    # it renders no HTML at all, it only turns a typed swap id into a redirect.
    # The first version of this test asserted 200-or-404 and failed on it, and
    # the easy fix -- drop the route from the matrix -- would have left the one
    # route in the app whose entire output is a destination unchecked. A 302
    # carrying an operator Location from a customer route is exactly the
    # crossing being forbidden, with no anchor tag to find.
    urls = (
        [response.headers.get("Location", "")]
        if response.status_code == 302
        else URL_ATTRIBUTES.findall(response.data.decode())
    )

    crossings = []
    for url in urls:
        try:
            blueprint = _blueprint_of(url, adapter)
        except Exception as error:  # noqa: BLE001 -- werkzeug raises several routing types; the
            # url and the error are both reported, so this never hides a failure as a pass
            pytest.fail(f"{path} carries {url!r}, which matches no route of this app: {error}")
        if blueprint in forbidden:
            crossings.append(f"{url} -> {blueprint}")

    assert not crossings, (
        f"the {surface} page {path} links into the other surface: {crossings}. "
        f"operator (" + ", ".join(sorted(OPERATOR)) + ") and customer ("
        + ", ".join(sorted(CUSTOMER)) + ") must not be reachable from each other; "
        "the operator page is reached deliberately, by typing its URL"
    )

    # THE POSITIVE CONTROL, and without it this test passes when it checks NOTHING.
    # Three mutations proved that: narrowing the attribute pattern to match
    # nothing, scanning an empty list instead of the body, and dropping the
    # redirect's Location all left every assertion above vacuously true, because
    # "no crossings found" and "nothing was looked at" produce the same green.
    # Rule 14's "make 'did nothing' look different from 'did work'" applied to a
    # test: a scan that resolves no URL at all on a real page is broken, not clean.
    # AND THE SEEDING'S OWN GUARD. If filled_admin_tables stops filling, /admin
    # renders its empty states again, every loop body vanishes, and a crossing
    # inside one goes unscanned -- while this file stays green, because an
    # unscanned crossing and an absent one look identical from here. A mutation
    # disabling the fixture survived until this line existed.
    if surface == "operator" and path == "/admin":
        assert SEEDED_ROW in response.data.decode(), (
            f"{path} rendered no seeded rows, so every url_for inside a table loop went "
            f"unscanned. The fixture is not reaching the view -- check that it patches the "
            f"module the app actually calls, which is resolved from view_functions and not "
            f"by import name"
        )

    own_side = OPERATOR if surface == "operator" else CUSTOMER
    resolved = [_blueprint_of(url, adapter) for url in urls]
    assert any(blueprint in own_side for blueprint in resolved), (
        f"{path} yielded no URL resolving to its own surface ({sorted(own_side)}) out of "
        f"{len(urls)} scanned. Every page here links somewhere within itself, so this means "
        f"the scan found nothing rather than that the page is clean"
    )


def test_every_blueprint_is_assigned_to_a_side():
    """A new blueprint must be classified, not silently default to allowed.

    THE FAILURE THIS CATCHES IS A SILENT ONE. An unclassified blueprint is in
    neither OPERATOR nor CUSTOMER, so nothing above would ever flag a link to it
    -- a new `reports` blueprint on the operator side could be linked from the
    customer flow forever and every test here would stay green. Rule 8: the
    routing table is the authority, and this asserts the classification still
    covers it rather than letting the two drift apart quietly.
    """
    registered = {rule.endpoint.split(".", 1)[0] for rule in _app().url_map.iter_rules()}
    classified = OPERATOR | CUSTOMER | NEUTRAL
    unclassified = registered - classified
    assert not unclassified, (
        f"{sorted(unclassified)} are registered blueprints that this file does not place on a "
        f"side. Add each to OPERATOR, CUSTOMER or NEUTRAL -- an unclassified blueprint is "
        f"invisible to every check above, which is worse than a wrong answer"
    )
    stale = classified - registered - NEUTRAL
    assert not stale, (
        f"{sorted(stale)} are classified here and registered nowhere -- rule 9: a name kept "
        f"for a caller that no longer exists"
    )


def test_the_surfaces_actually_overlap_in_the_url_space():
    """The rule is worth enforcing only because both surfaces are on one origin.

    /admin and / are the SAME host and port, so nothing but a link's absence
    separates them in a browser. This records that, so a reader does not take the
    test above for a claim that the operator page is network-isolated -- it is
    not, and the operator's own words were "the operator page will be a secure
    page locally", which is a thing still to be done and not a thing this asserts.
    """
    adapter = _app().url_map.bind("localhost")
    assert _blueprint_of("/admin", adapter) in OPERATOR
    assert _blueprint_of("/", adapter) in CUSTOMER
