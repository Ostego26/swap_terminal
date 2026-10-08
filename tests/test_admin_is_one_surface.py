#!/usr/bin/env python3
"""The operator's pages are one surface: tabs, one shell, one palette.

Role: file (entry point -- `python3 -m pytest tests/test_admin_is_one_surface.py`)
Reads: the Flask app, the admin templates, swap_terminal/static/*.css
Writes: nothing
Can move funds: no
Live-safe: yes

Operator, 2026-10-08: "make all the admin pages under one admin page that is
found with tabls or hyperlinks."

WHAT WAS WRONG, three things rather than one:

  navigation   /admin/controls was reachable only through a sentence in the
               middle of /admin's prose, and /admin from it only through a
               breadcrumb. Two pages that are one surface, joined by text.
  shell        kill_switch_page.html extended NOTHING -- its own doctype, its
               own stylesheet, no topnav, no footer. Its header recorded why:
               "base.html, static/styles.css and templates/admin.html were
               off-limits to the change that wrote this." A constraint on one
               change, not a property of the page.
  palette      and because it owned its stylesheet, it owned a palette:
               THIRTEEN literal colors describing a LIGHT page, #1b1f23 ink on
               #ffffff, inside a terminal that is phosphor green on near-black
               everywhere else. Plus a prefers-color-scheme block holding a
               THIRD set. A light page and a dark one do not read as one surface
               however the navigation between them is arranged.

THE CUSTOMER-DIRECTION RULE IS NOT HERE ANY MORE. This file used to carry
test_the_customer_page_is_NOT_changed_by_that, which pinned the customer page's
link to /admin so that removing it "has to mean to". On 2026-10-08 the operator
meant to -- "operator and swap should not be acceabbile to eat other" -- so that
link, the surface nav and five operator->customer links in admin.html are all
gone, and the invariant is now BOTH directions in
tests/test_surfaces_do_not_link_to_each_other.py. The old test is deleted rather
than kept passing against a weaker claim (rule 2: its test dies with it or
changes to pin the stronger invariant). It did its job -- the removal was a
decision rather than a side effect.

TABS AND NOT A MERGE, and that is a safety property. /admin opens "Operator
surface - read-only" and its own header records that no form on it posts
anywhere. The controls page arms a payout worker that broadcasts. Folding the
second into the first would put a start button on the page whose stated
guarantee is that nothing on it changes anything -- and that guarantee is what
makes the dashboard safe to leave open. The ask said "tabs or hyperlinks", so it
is satisfied without spending it.
"""

from __future__ import annotations

import re
from pathlib import Path

import app as app_module
import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
TEMPLATES = REPO_ROOT / "swap_terminal" / "templates"
STATIC = REPO_ROOT / "swap_terminal" / "static"

#: The two pages the strip joins, and the tab each should mark as current.
ADMIN_PAGES = (("/admin", "System state"), ("/admin/controls", "Start / stop workers"))


def _app():
    """The real application, built the way wsgi.py builds it."""
    return app_module.create_app() if hasattr(app_module, "create_app") else app_module.app


@pytest.fixture
def client():
    with _app().test_client() as test_client:
        yield test_client


@pytest.mark.parametrize(("path", "expected_tab"), ADMIN_PAGES)
def test_both_admin_pages_carry_the_strip_and_mark_their_own_tab(client, path, expected_tab):
    """Rendered, not read as source: the macro has to be CALLED on both pages.

    A partial that exists and is included by one page is the defect this
    replaces -- /admin/controls was reachable and /admin was not reachable back
    from it in any way a reader would find.
    """
    response = client.get(path)
    assert response.status_code == 200, f"{path} -> {response.status_code}"
    html = response.data.decode()

    assert 'class="admin-tabs"' in html, f"{path} does not render the operator tab strip"
    # BOTH tabs on BOTH pages: a strip that only shows the page you are on is a
    # breadcrumb, which is what this replaced.
    for _other_path, label in ADMIN_PAGES:
        assert f">{label}</a>" in html, f"{path} does not link to the {label!r} tab"

    current = re.findall(r'class="admin-tab admin-tab-here"[^>]*>([^<]*)<', html)
    assert current == [expected_tab], (
        f"{path} marks {current} as the current tab, expected exactly [{expected_tab!r}]. "
        "Marking none leaves the operator unable to tell which page they are on; marking "
        "two is a template passing the wrong key."
    )
    assert 'aria-current="page"' in html, (
        "the current tab must be marked with aria-current as well as a color -- this "
        "stylesheet's standing requirement is that no state is carried by color alone"
    )


@pytest.mark.parametrize(("path", "_tab"), ADMIN_PAGES)
def test_both_admin_pages_use_the_one_shell(client, path, _tab):
    """Same topnav, same footer, same stylesheet. The thing "one surface" means.

    styles.css must be present on BOTH -- it is what defines :root's palette, and
    kill_switch.css's thirteen tokens now point at it rather than carrying
    colors of their own. A page that loaded only kill_switch.css would render
    with every one of those tokens unresolved.
    """
    html = client.get(path).data.decode()
    # THE HEADER, NOT THE NAV INSIDE IT. This asserted `class="topnav"` until the
    # operator asked for the operator surface to stop linking to the customer one
    # -- which suppresses that nav on exactly these two pages. The invariant
    # ("both pages are in the one shell") did not change; the evidence for it did,
    # and a test that pins the evidence rather than the invariant fails on a
    # correct change. base.html's topbar and footer are the shell.
    assert 'class="topbar"' in html, f"{path} has no topbar -- it is not in the shared shell"
    assert 'class="brand"' in html, f"{path} has no brand mark -- it is not in the shared shell"
    assert "styles.css" in html, (
        f"{path} does not load styles.css. kill_switch.css's tokens reference that file's "
        ":root palette, so without it they resolve to nothing."
    )
    assert "microfortnights" in html, f"{path} is missing the shell's unit footer (rule 6)"


def test_the_controls_page_loads_its_own_sheet_AFTER_the_shared_one():
    """Order matters: :root has to be defined before the tokens that cite it.

    Read out of the rendered HTML rather than the template, because the template
    puts the link inside a block and only the render says where that block
    lands.
    """
    with _app().test_client() as test_client:
        html = test_client.get("/admin/controls").data.decode()
    shared = html.index("styles.css")
    own = html.index("kill_switch.css")
    assert shared < own, (
        "kill_switch.css is linked BEFORE styles.css. Its tokens are var() references into "
        "styles.css's :root, so this order leaves them undefined."
    )


def test_the_kill_switch_stylesheet_holds_no_colour_of_its_own():
    """One palette. A second set of values is rule 8 in CSS.

    kill_switch.css held thirteen literal colors for a light theme and another
    thirteen in a prefers-color-scheme block -- three definitions of "this is a
    warning", each correct in its own file, agreeing about nothing. The names
    stay (`--killsw-proven` is a verdict this page reports and the dashboard does
    not) because they are a LAYER over the palette; what is gone is the second
    set of VALUES, which is the half that drifts.
    """
    css = (STATIC / "kill_switch.css").read_text()
    # Comments legitimately quote the old values while explaining the change.
    without_comments = re.sub(r"/\*.*?\*/", "", css, flags=re.DOTALL)
    literals = re.findall(r"#[0-9a-fA-F]{3,8}\b", without_comments)
    assert not literals, (
        f"kill_switch.css declares {literals}. Every value must come from styles.css's :root "
        "-- that is what makes the controls page and the dashboard one surface rather than "
        "two themes behind a tab strip."
    )
    assert "prefers-color-scheme" not in without_comments, (
        "the prefers-color-scheme block is back. styles.css deleted its own for the reason in "
        "its header, and with these tokens pointing at that palette this block would override "
        "them on a dark-preferring browser only -- a page whose theme depends on a setting the "
        "rest of the terminal ignores."
    )


def test_the_dashboard_still_posts_nowhere():
    """The guarantee the tabs exist in order NOT to spend.

    /admin says "Operator surface - read-only" and its header records that no
    form on it posts anywhere. The whole argument for tabs over a merge is that
    this stays true, so it is asserted rather than trusted -- an include of
    _kill_switch.html into admin.html would be a one-line change that no other
    test in this suite would notice.
    """
    rendered = (TEMPLATES / "admin.html").read_text()
    assert "_kill_switch.html" not in rendered, (
        "admin.html includes the kill-switch panel. That puts a start button -- which arms a "
        "payout worker that broadcasts -- on the page whose stated guarantee is that nothing "
        "on it changes anything. Use the tab strip; see _admin_tabs.html."
    )

    with _app().test_client() as test_client:
        html = test_client.get("/admin").data.decode()
    assert "<form" not in html, (
        "a form is rendered on /admin. The page's own header claims there is none, and a page "
        "that says read-only while carrying a form is the wrong-comment defect with a button "
        "attached."
    )


def test_the_strip_links_are_built_from_the_routes_and_not_typed():
    """url_for, so renaming a route moves the tab with it.

    A hardcoded "/admin/controls" in the partial would survive a route rename and
    become a 404 that only an operator finds -- which is the same class of drift
    as the `/atm` prose that outlived the move to `/`.
    """
    partial = (TEMPLATES / "_admin_tabs.html").read_text()
    assert "url_for('admin.admin_page')" in partial
    assert "url_for('kill_switch.controls_page')" in partial
    # No typed absolute paths anywhere in the tab list.
    assert not re.search(r"href=\"/admin", partial), (
        "a tab href is typed rather than built with url_for"
    )


def test_the_surface_the_strip_cannot_link_to_is_named():
    """Rule 14: the canister console's absence must be a result, not a gap.

    There are three operator surfaces and the strip can only link two. The
    third is served by the operator_admin canister, whose id this process cannot
    know -- every fresh replica issues different ones and nothing mounts the
    replica's state into this container. An operator who knows about three
    surfaces and sees two tabs is owed the reason, and a tab pointing at a
    guessed id would link to another deployment (rule 17).
    """
    partial = (TEMPLATES / "_admin_tabs.html").read_text()
    assert "operator_admin" in partial, "the canister console is not named"
    assert "swap_stack.py status" in partial, (
        "the thing that CAN resolve the console's address is not named, so the note says "
        "'you cannot get there from here' and stops"
    )

    # JINJA COMMENTS STRIPPED FIRST, and the first version of this assertion did
    # not: it searched the whole file for a replica URL and flagged the partial's
    # own header, which cites `http://<its-id>.localhost:4943/` while EXPLAINING
    # that the id is unknowable here. A gate that cannot tell a citation from a
    # claim fails on the comment that documents the fix -- the same mistake this
    # file makes in the stylesheet test one function up, and the same remedy.
    markup = re.sub(r"\{#.*?#\}", "", partial, flags=re.DOTALL)
    assert "localhost:4943" not in markup, (
        "the partial's MARKUP contains a replica URL. If that includes a canister id it is a "
        "guess, and a guessed id links to somebody else's deployment."
    )


@pytest.mark.parametrize(("path", "_tab"), ADMIN_PAGES)
def test_no_operator_page_offers_a_way_into_the_customer_flow(client, path, _tab):
    """Operator, 2026-10-08: "you can get to the swap terminal from the operator
    terminal . NO."

    THERE WERE TWO ROUTES AND THE SECOND WAS THE EASIER ONE TO HIT. base.html's
    topnav rendered both surface links on every page with only the `here` class
    conditional -- and the brand above it was `url_for('atm.start')`
    unconditionally, so clicking the logo on the operator dashboard dropped you
    into the customer ATM. A logo is where everybody clicks to get back, so the
    one nobody would have listed was the one that would actually have fired.

    ASSERTED OVER THE WHOLE RENDERED PAGE, not just the header, because the
    point is that no operator page offers the route -- a link added to the footer
    or into a prose paragraph later would satisfy a header-only check while being
    the same defect.
    """
    html = client.get(path).data.decode()

    # Every href on the page, resolved to the paths this app actually serves.
    hrefs = set(re.findall(r'href="([^"]*)"', html))
    customer = {"/", "/swap-lookup"}
    offending = sorted(hrefs & customer)
    assert not offending, (
        f"{path} links to {offending}, which is the customer flow. The operator surface must "
        "not offer a way into it. The operator's own navigation is the tab strip in "
        "_admin_tabs.html; base.html suppresses the surface nav when surface == 'admin'."
    )

    # And the brand specifically, by name, because it is the route that was
    # missed the first time this was asked for.
    brand = re.search(r'class="brand" href="([^"]*)"', html)
    assert brand, f"{path} has no brand link at all -- base.html's header changed shape"
    assert brand.group(1) != "/", (
        f"{path}'s brand/logo points at the customer ATM. On the operator surface it must go "
        "to the operator's own home."
    )
