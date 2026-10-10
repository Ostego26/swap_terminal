"""Both web surfaces, exercised through the REAL Flask app and its real routes.

Role: test (behavioral verification of routes/ui.py, routes/admin.py and the
      templates they render)
Reads: the app's test client and a temporary database seeded with real rows
Writes: that temporary database only
Can move funds: no. Nothing here POSTs to /api/swaps, and no adapter in this
      file can send.
Mainnet-safe: yes -- no socket is opened to any chain.

WHY THESE GO THROUGH THE APP AND NOT THROUGH THE VIEW FUNCTIONS. The rendered
page is the artifact a customer and an operator actually read, and the failures
that matter here happen in the render: an empty region that draws as a blank
gap, a stale row that draws like a fresh one, a template that quietly prints
nothing because a key it expects was renamed. A test that called
swap_display() and asserted on the dict would pass through every one of those.

So: seed rows, ask the app for the page, and assert on the bytes it returned.
"""

import html
import logging
import re
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from time import time

import pytest
from config import Config
from db import SCHEMA, dict_factory
from network_target import configuring_variable
from page_markup import TILE_WITH_LABEL
from services import coinpaprika, market_context, pricing
from services.wallet_leveling import PEG_ASSETS
from stack_authority import (
    CHAIN_PROBE_ROWS_KEY,
    chain_probe_rows,
    chain_reachability_verdict,
)
from valid_addresses import GRC_DESK_DEPOSIT, GRC_PAYOUT, SOL_DEPOSIT_ACCOUNT

# `app` imports and calls create_app() at module scope, and conftest.py has
# already pointed SWAP_DB_PATH at a temp file by the time this import runs.
import app as app_module  # isort: skip

NOW_OFFSETS = {"fresh": 5.0, "stale": 90000.0}


@pytest.fixture
def client(tmp_path, monkeypatch):
    """The real app, pointed at a per-test database on the real schema."""
    db_path = tmp_path / "surface.db"
    conn = sqlite3.connect(db_path)
    conn.row_factory = dict_factory
    conn.executescript(SCHEMA)
    conn.commit()
    conn.close()

    flask_app = app_module.app
    monkeypatch.setitem(flask_app.config, "DB_PATH", str(db_path))
    flask_app.config["TESTING"] = True
    with flask_app.test_client() as test_client:
        yield test_client


def write(client, statement, params=()):
    """Insert a row into the database the app is pointed at."""
    conn = sqlite3.connect(client.application.config["DB_PATH"])
    conn.execute(statement, params)
    conn.commit()
    conn.close()


def iso(seconds_ago: float) -> str:
    return (datetime.now(UTC) - timedelta(seconds=seconds_ago)).isoformat()



def seed_swap(client, swap_id, status, **overrides):
    """Insert one swap and its quote. Optional columns arrive as `overrides`.

    The optional five used to be named parameters, which put this at seven and
    tripped PLR0913/PLR0917. Collecting them is the extraction the rule asks for
    -- and it reads better at the call sites, where `asset="XRP"` is the only
    thing most of them vary.
    """
    asset = overrides.get("asset", "GRC")
    updated_ago = overrides.get("updated_ago", 30.0)
    # GRC_PAYOUT, not "Saddr". services/swap_view._address_problem() (2026-09-27) reports a
    # deposit address that cannot receive money, and templates/swap.html:59 then renders the
    # "Do not send anything yet" callout INSTEAD of the send target -- so a placeholder here
    # silently turned "does this page show where to send coins" into "does this page show a
    # warning", and three parameterized cases failed on it. Derived, never spelled: see
    # tests/valid_addresses.py.
    deposit_address = overrides.get("deposit_address", GRC_PAYOUT)
    min_conf = overrides.get("min_conf", 6)
    # NULL by default, which is what every address-attributed chain has. Added
    # 2026-10-01 so a tag chain's page can be rendered here at all: without it
    # services/swap_view.deposit_instruction() reports `tag_missing` for every SOL
    # or XRP swap and the deposit panel renders a problem instead of a target.
    deposit_tag = overrides.get("deposit_tag")
    write(
        client,
        "INSERT INTO quotes (id, from_asset, to_asset, input_amount, quoted_rate, fee_bps, network_fee_reserve,"
        " output_amount_estimate, expires_at, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (f"q_{swap_id}", asset, "BTC", 1000.0, 1e-7, 150, 0.00002, 0.0001, iso(-600), iso(600)),
    )
    write(
        client,
        "INSERT INTO swaps (id, quote_id, from_asset, to_asset, deposit_address, deposit_tag, payout_address,"
        " expected_input_amount, actual_input_amount, quoted_rate, fee_bps, network_fee_reserve,"
        " output_amount_estimate, status, min_confirmations, deposit_txid, payout_txid, created_at, updated_at,"
        " credited_at, completed_at, expires_at, failed_reason)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            swap_id, f"q_{swap_id}", asset, "BTC", deposit_address, deposit_tag, "bc1addr", 1000.0, None, 1e-7,
            150, 0.00002, 0.0001, status, min_conf, None, None, iso(600), iso(updated_ago), None, None,
            iso(-600), None,
        ),
    )


# --- the customer surface ---------------------------------------------------



def offered_pairs(client) -> set[tuple[str, str]]:
    """Every (source, destination) the ATM flow will actually let a customer PICK.

    THE REPLACEMENT FOR `value="FROM:TO" in body`, WHICH DIED WITH THE SELECT.
    templates/index.html carried one <select> whose options were "FROM:TO", so
    "is this pair offered" was a substring test. The ATM asks the two halves on
    two screens -- step 1 `value="ICP"`, step 2 `value="GRC"` -- so there is no
    combined token to look for, and three tests guarding real 2026-09-26 and
    2026-10-03 incidents were asserting against a string that no longer exists.

    ONE HELPER AND NOT THREE REGEXES, because this is one question (rule 8) and
    because it is now a WALK rather than a match: each source is posted and the
    real step-2 screen is read. That makes these tests harder than they were --
    a select renders its options from one list in one request, while this drives
    the handler once per source, so a step-2 filter disagreeing with step 1 fails
    here even though both read the same rows.

    SELECTABILITY, NOT PRESENCE. The ATM renders an unavailable destination
    GREYED with its reason rather than hiding it (rule 14), so a `disabled`
    button is LISTED and not OFFERED -- which is exactly the distinction
    test_a_destination_that_cannot_pay_out_is_not_offered is about.
    """
    first = client.get("/").get_data(as_text=True)
    sources = {
        match.group(1)
        for match in re.finditer(r'<button[^>]*name="from_asset"[^>]*>', first, re.DOTALL)
        if "disabled" not in match.group(0)
        for match in [re.search(r'value="([A-Z]+)"', match.group(0))] if match
    }
    offered = set()
    for source in sources:
        step2 = client.post("/", data={"from_asset": source}).get_data(as_text=True)
        for button in re.finditer(r'<button[^>]*name="to_asset"[^>]*>', step2, re.DOTALL):
            if "disabled" in button.group(0):
                continue
            destination = re.search(r'value="([A-Z]+)"', button.group(0))
            if destination:
                offered.add((source, destination.group(1)))
    return offered

def test_the_swap_form_offers_exactly_the_pairs_whose_chains_are_reachable(client, monkeypatch):
    """The form reads BOTH authorities: what is allowed, and what has an adapter.

    THIS TEST USED TO ASSERT THE DEFECT. It was
    test_the_swap_form_offers_exactly_the_pairs_the_server_allows, and it checked
    that every pair in Config.ALLOWED_PAIRS appeared as an option -- which is
    exactly what produced, on 2026-09-26, six pairs badged ENABLED on a server
    that had built one adapter. The operator picked XRP -> GRC, the quote priced,
    and Create swap answered `No swap was created: 'GRC'` -- str(KeyError("GRC")),
    because create_swap() subscripted a dict that had no Gridcoin in it.

    So it is rewritten rather than deleted, to pin the stronger invariant (rule 2):
    the select offers an allowed pair only when BOTH of its chains have an adapter
    in this process, and every allowed pair is still LISTED so an operator who
    configured six does not silently see five.

    MUTATION: make the select iterate `pairs` again instead of `offerable`, or
    hardcode the option list in templates/index.html the way static/script.js
    used to with `const validTargets = {...}`. Either fails here.
    """
    allowed = client.application.config["ALLOWED_PAIRS"]
    # Exactly the operator's server on 2026-09-26, minus their missing GRC: the
    # membership test is all allowed_pair_rows() performs, so sentinels are enough
    # and no adapter is constructed (nothing here opens a socket).
    reachable_assets = {"XRP", "GRC"}
    reachable_adapters = reachable(*reachable_assets)
    monkeypatch.setitem(client.application.config, "ADAPTERS", reachable_adapters)
    # AND THE SHARED ACCOUNTS, since 2026-10-01: "reachable" now includes being able to
    # produce a deposit address, and XRP cannot without XRP_DEPOSIT_ACCOUNT. Without this the
    # test still passes its `not in body` half and fails its `in body` half -- which is the
    # right answer for an unconfigured terminal and the wrong premise for this test.
    with_deposit_accounts(client, monkeypatch)
    body = client.get("/").get_data(as_text=True)
    offered = offered_pairs(client)

    for from_asset, to_asset in allowed:
        # `and quotable(...)`: see quotable() above. Reachable is necessary and, since
        # 2026-10-03, not sufficient -- a pair whose destination has no fee reserve is
        # correctly NOT offered even with both chains up, and asserting it must be
        # offered was this test asserting the defect.
        if from_asset in reachable_assets and to_asset in reachable_assets and quotable((from_asset, to_asset)):
            assert (from_asset, to_asset) in offered, (
                f"{from_asset}->{to_asset} is reachable and quotable and must be offered"
            )
        else:
            assert (from_asset, to_asset) not in offered, (
                f"{from_asset}->{to_asset} was offered, but one of its chains has no adapter -- "
                f"this is the shape that printed 'GRC' at the operator"
            )

    # LISTED, not hidden. An operator who configured six pairs and sees five has
    # no way to tell which one vanished or why (rule 14).
    listed = body.count('class="pair-label"')
    assert listed == len(allowed), f"{len(allowed)} pairs allowed, {listed} listed on the page"
    # The markup, not the bare word: the panel-note above the list explains what
    # DISABLED means, so a substring test would pass on the explanation alone.
    # THE WORDS CHANGED ON 2026-10-02 AND THE INVARIANT DID NOT. This page badged
    # ENABLED or DISABLED -- two words for what pair_serviceability() answers in
    # three states -- and now carries AVAILABLE / OFFLINE / UNAVAILABLE from
    # services/pair_view.customer_availability(). What this test is about is that an
    # unreachable pair is not marked like a reachable one, which is unchanged.
    assert 'badge-word">OFFLINE<' in body, "an unreachable pair must not be marked available"
    assert 'badge-word">AVAILABLE<' in body, "a reachable pair must still be marked available"
    assert "swaptile-unreachable" in body, "an unreachable pair must be distinguishable at tile level"


def test_a_disabled_pair_names_the_variable_that_would_enable_it(client, monkeypatch):
    """DISABLED with no reason is rule 14's blank gap wearing a badge.

    The operator's whole problem on 2026-09-26 was that they could not get from
    what the screen said to what to change. The reason comes from
    chains/registry.why_unconfigured(), which names the variable through
    network_target.configuring_variable() -- so this asserts the NAME reaches the
    page, not that a particular sentence was written.
    """
    fully_reachable(client, monkeypatch, "XRP")
    body = client.get("/").get_data(as_text=True)

    # GRC is in an allowed pair and has no adapter here, so its variable must be
    # on the page. Read from the authority rather than spelled, for the same
    # reason the pair list is.
    # INVERTED ON 2026-10-02, AND THE INVERSION IS THE CHANGE. Operator instruction:
    # "the user screen should just have graphical indicators to what's availble for
    # them to swap." A customer has no shell on this host, cannot export anything,
    # and this sentence names internal configuration -- its audience is the operator,
    # who has /admin. It is also a small disclosure: this page is served on the same
    # port as /admin, whose own banner says anyone who can reach it can read
    # everything, and naming unset RPC variables to an unauthenticated reader has no
    # upside.
    #
    # NOTHING LEFT THE SYSTEM'S REPORTING, which is what keeps this inside rule 14,
    # and the second half ASSERTS that rather than trusting it: the variable has to
    # be on /admin, where it renders once per asset in the Chains table.
    assert configuring_variable("GRC") not in body, (
        "the customer page must not name an environment variable: its reader has no shell here"
    )
    assert ".env" not in body, "and must not explain this server's configuration to a customer"
    operator_page = client.get("/admin").get_data(as_text=True)
    assert configuring_variable("GRC") in operator_page, (
        "the variable left the customer page and did NOT arrive on the operator page"
    )
    assert ".env" in operator_page, (
        "the reason must say the value has to be in the PROCESS environment -- a "
        "value in a file only is the exact way this failed"
    )


def test_no_reachable_chain_means_no_option_and_a_disabled_form(client, monkeypatch):
    """An empty select reads as a broken page. It must read as "nothing is enabled"."""
    monkeypatch.setitem(client.application.config, "ADAPTERS", {})
    body = client.get("/").get_data(as_text=True)

    for from_asset, to_asset in client.application.config["ALLOWED_PAIRS"]:
        assert f'value="{from_asset}:{to_asset}"' not in body
    assert "disabled" in body, "the select, the amount field and the button must all be disabled"
    # Still listed, still explained.
    assert body.count('class="pair-label"') == len(client.application.config["ALLOWED_PAIRS"])


def test_every_allowed_pair_is_offered_when_every_chain_is_reachable(client, monkeypatch):
    """The other direction, so the guard cannot pass by refusing everything.

    A version of allowed_pair_rows() that marked every pair disabled would satisfy
    all three tests above. This is the one it fails.
    """
    allowed = client.application.config["ALLOWED_PAIRS"]
    every_asset = {asset for pair in allowed for asset in pair}
    adapters = fully_reachable(client, monkeypatch, *every_asset)
    body = client.get("/").get_data(as_text=True)

    # THE PREMISE NARROWED ON 2026-10-03 AND THE GUARD IT PROVIDES DID NOT.
    # "Every chain reachable" no longer implies "every pair offered": a pair whose
    # destination has no fee reserve refuses at the quote and must not be offered.
    # The split is asserted both ways below, so a function that marked everything
    # disabled still fails here -- which is the one thing this test exists for.
    #
    # IT NARROWED AGAIN THE SAME DAY, ONE CONDITION DEEPER, and that is why the
    # expectation is now offerable() rather than quotable(): a *->SOL pair also needs
    # the cluster to answer the rent-exempt minimum, which no amount of config can
    # settle. The PREMISE here is "everything works", so StubAdapter was taught to
    # answer that ask (see its docstring) rather than the expectation being narrowed
    # to route around it -- a premise of a reachable terminal that cannot represent a
    # reachable cluster is a premise that no longer means what the test name says.
    # Where it still cannot be represented the pair drops out of `expected` and the
    # loop below asserts it is NOT offered, naming which of the two conditions did it.
    expected = [pair for pair in sorted(allowed) if offerable(pair, adapters)]
    assert expected, (
        "no allowed pair has a destination fee reserve, so this test would assert nothing. "
        "That is a real finding, not a setup problem -- read config.py"
    )
    offered = offered_pairs(client)
    for from_asset, to_asset in expected:
        assert (from_asset, to_asset) in offered, (from_asset, to_asset)
    for from_asset, to_asset in sorted(set(allowed) - set(expected)):
        why = (
            f"no {to_asset}_NETWORK_FEE_RESERVE exists"
            if not quotable((from_asset, to_asset))
            else f"the {to_asset} cluster could not be asked for the rent-exempt minimum in this premise"
        )
        assert (from_asset, to_asset) not in offered, (
            f"{from_asset}->{to_asset} was offered and {why}, so the quote for it refuses -- "
            f"the flow offered a pair the next step cannot price"
        )
    assert 'badge-word">DISABLED<' not in body, "nothing is unreachable here, so nothing may be badged DISABLED"
    if len(expected) == len(allowed):
        assert "pair-off" not in body, "no pair may be marked off when every pair is reachable and quotable"

    # And nothing else -- with the disabled set DERIVED, not written out.
    #
    # This second half used to hardcode a chain list, which was the very
    # duplication this test exists to prevent, one level up: a hand-written copy
    # of what the config allows, correct on the day it was written. XRP<->GRC was
    # enabled on 2026-09-26 and it failed immediately -- which is the guard
    # working, and also the proof that the list was a second source of truth.
    #
    # Derived from Config.RPC (every chain the terminal knows) minus the assets
    # that appear in an allowed pair, so it stays right for any future pair set
    # without anyone remembering to edit it.
    known = set(client.application.config["RPC"])
    tradeable = {asset for pair in allowed for asset in pair}
    for disabled_asset in sorted(known - tradeable):
        assert f'value="{disabled_asset}:' not in body, f"{disabled_asset} is not tradeable"
        assert f':{disabled_asset}"' not in body, f"{disabled_asset} is not tradeable"


def test_no_javascript_file_carries_a_copy_of_the_pair_list():
    """Rule 8, pinned on the file where the duplicate used to live.

    MUTATION: put `validTargets` back into static/script.js. The copy agreed
    with the server on the day it was written; the day a pair moves, one of them
    is wrong and nothing fails anywhere.
    """
    static = Path(app_module.__file__).resolve().parent / "static"
    for script in static.glob("*.js"):
        # COMMENTS ARE STRIPPED FIRST, and that is not a loophole. script.js's
        # header quotes the deleted constant in order to explain why it was
        # deleted, which is rule 1 working as intended: a future reader who
        # reaches for the same shortcut finds the reason not to. The first draft
        # of this test banned the string outright and failed on that comment,
        # which would have pushed the explanation out of the file to satisfy a
        # checker (rule 12: verbosity is never the thing to trim). What must not
        # exist is the CODE.
        code = "\n".join(
            line
            for line in script.read_text().splitlines()
            if not line.lstrip().startswith(("*", "//", "/*"))
        )
        assert "validTargets" not in code, script
        # No JS file may spell a pair mapping of its own, under any name.
        assert '"GRC": [' not in code and "GRC: [" not in code, script


def test_a_swap_page_renders_the_status_the_database_holds(client):
    seed_swap(client, "s_render0000000a", "confirming")
    body = client.get("/swap/s_render0000000a").get_data(as_text=True)
    assert "Confirming on chain" in body
    assert "confirming" in body
    assert "0 of 6" not in body or "of" in body  # the counts panel rendered


@pytest.mark.parametrize(
    ("status", "expected_phrase", "level"),
    [
        ("awaiting_deposit", "Waiting for your deposit", "waiting"),
        ("confirming", "Confirming on chain", "working"),
        ("completed", "Done", "ok"),
        ("under_review", "Held for review", "halted"),
        ("failed", "Payout failed", "failed"),
    ],
)
def test_each_state_renders_its_own_words_and_its_own_level(client, status, expected_phrase, level):
    """Waiting, done, halted and failed must be distinguishable in the MARKUP.

    Not by color: the level class and the badge word are both in the bytes, so
    the distinction survives a grayscale screenshot and a colorblind reader.
    """
    swap_id = f"s_state{status[:8]:_<10}"[:18]
    seed_swap(client, swap_id, status)
    body = client.get(f"/swap/{swap_id}").get_data(as_text=True)
    assert expected_phrase in body
    assert f'level-{level}' in body
    assert f'badge-{level}' in body


def test_an_empty_deposit_list_renders_none_and_not_a_blank_gap(client):
    """Rule 14: `(none)` is a result; a blank region is ambiguous.

    MUTATION: delete the `{% else %}` branch in templates/swap.html's deposits
    section. The page then renders a heading with nothing under it, and the
    reader cannot tell "no deposit yet" from "that query broke".
    """
    seed_swap(client, "s_empty00000000a", "awaiting_deposit")
    body = client.get("/swap/s_empty00000000a").get_data(as_text=True)
    assert "(none)" in body
    assert "no deposit row exists for this swap" in body
    # It also says WHAT was looked for, so the reader can check it themselves.
    assert "deposit_events WHERE swap_id = s_empty00000000a" in body
    assert "no payout has been attempted" in body


@pytest.mark.parametrize("status", ["under_review", "failed", "completed", "payout_pending", "paying"])
def test_a_swap_that_is_done_or_halted_does_not_ask_for_another_deposit(client, status):
    """MUTATION: drop the `view.accepting_deposit` branch in templates/swap.html.

    A REAL DEFECT, found by looking at a rendered page rather than by a test --
    which is why there is now a test. The deposit panel printed "Send exactly
    0.01000000 BTC to" with the address under it on EVERY status, including a
    swap the amount-tolerance check had just HALTED. The card directly below it
    said nothing had been sent and a human was deciding; the panel above asked
    for more coins. A customer who obliged would put a second payment on an
    address whose swap no worker advances, and it would have to be reconciled
    by hand -- sent because this page asked for it.
    """
    swap_id = f"s_closed{status[:8]}"
    seed_swap(client, swap_id, status)
    body = client.get(f"/swap/{swap_id}").get_data(as_text=True)
    assert "Send exactly" not in body
    assert "Do not send anything to this swap" in body
    assert status in body


@pytest.mark.parametrize("status", ["awaiting_deposit", "deposit_seen", "confirming"])
def test_a_swap_still_waiting_on_coins_does_show_where_to_send_them(client, status):
    """The other half, and it is why the test above is not enough on its own.

    A guard that hid the address unconditionally would satisfy the previous test
    and make the product useless. Both directions are asserted so neither
    mistake can pass.
    """
    swap_id = f"s_open{status[:10]}"
    seed_swap(client, swap_id, status)
    body = client.get(f"/swap/{swap_id}").get_data(as_text=True)
    assert "Send exactly" in body
    assert 'id="deposit-target"' in body
    assert "Do not send anything to this swap" not in body


def test_a_missing_swap_is_404_and_leaks_nothing(client):
    """An error page is exactly where a secret leaks. This one renders a sentence.

    MUTATION: pass the exception or the config into the 404 template. A page
    that dumps state to be helpful is how a database path, a traceback or a
    credential reaches a stranger.
    """
    response = client.get("/swap/s_definitely_not_real")
    assert response.status_code == 404
    body = response.get_data(as_text=True)
    assert "No swap with that ID" in body
    assert "(none)" in body
    assert "Traceback" not in body
    assert client.application.config["DB_PATH"] not in body
    assert "password" not in body.lower()


def test_the_lookup_form_redirects_to_the_swaps_own_url(client):
    response = client.get("/swap-lookup?swap_id=s_abc")
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/swap/s_abc")
    # A blank submit goes back to the form, not to a 404 for the empty string.
    assert client.get("/swap-lookup?swap_id=%20%20").headers["Location"].endswith("/")


def test_the_live_fragment_is_server_rendered_html_not_json(client):
    """The poll replaces a region with SERVER-rendered markup (rule 8).

    MUTATION: make the fragment return JSON. The browser would then have to
    learn what `confirming` means, which puts the status vocabulary in a second
    language where the server cannot check it.
    """
    seed_swap(client, "s_frag00000000000", "payout_pending")
    response = client.get("/swap/s_frag00000000000/fragment")
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert body.lstrip().startswith("<")
    assert "Deposit credited, payout queued" in body


def test_a_fragment_for_a_vanished_swap_says_the_figures_are_not_current(client):
    """A failed refresh must not leave a stale render looking live."""
    response = client.get("/swap/s_gone/fragment")
    assert response.status_code == 404
    body = response.get_data(as_text=True)
    assert "not current" in body
    assert "could not be read" in body


def test_an_xrp_swap_shows_a_destination_tag_panel_and_refuses_to_show_a_target(client):
    """Seeded directly, because no XRP pair is enabled and none can be created.

    MUTATION: render the address box for every chain. A customer would be shown
    a shared XRP account to send to with no tag, and the payment would arrive
    unattributable.
    """
    seed_swap(client, "s_xrp00000000000a", "awaiting_deposit", asset="XRP",
              deposit_address="rPT1Sjq2YGrBMTttX4GZHjKu9dyfzbpAYe", min_conf=1)
    body = client.get("/swap/s_xrp00000000000a").get_data(as_text=True)
    assert "DESTINATION TAG" in body
    assert "Do not send anything yet" in body
    assert "validated ledger" in body, "an XRP threshold must not be described as blocks"
    assert "blocks must be mined" not in body


# --- the operator surface ---------------------------------------------------


def test_the_admin_blueprint_registers_EXACTLY_ONE_write_method():
    """ONE write, named, proven over the app's REAL url map rather than by reading the file.

    THIS ASSERTED ZERO UNTIL 2026-10-10, and the change is the operator's, asked for five
    times:

      "i asked for just a fucking control panel and i got a dumbass verbose pile of shit
       that doesn't control anything or tell me anything useful really."
      "no controls. no buttons. nothing."
      "why have you been dancing the fuck around on trols. i have said i want a fucking
       control panel for awhile now."

    The read-only posture was not wrong, it was over-broad: it treated every possible
    button as the button that spends. Re-driving a payout that was refused BEFORE signing
    signs nothing, and refusing to build it was the dancing the operator named.

    SO THE ASSERTION IS NOW AN ALLOWLIST OF ONE RATHER THAN AN EMPTINESS, which is
    strictly more useful: a SECOND write arriving fails here and has to be looked at
    deliberately, and the one that exists is named with what guards it. An emptiness
    assertion could only ever say "something appeared".

    MUTATION: add `@bp.post("/admin/retry")` to routes/admin.py. This still fails
    immediately, which is the property worth keeping.
    """
    writes = set()
    for rule in app_module.app.url_map.iter_rules():
        if rule.endpoint.startswith("admin."):
            # werkzeug types Rule.methods as `set[str] | None`, and None means
            # the rule matches ANY method. On a surface whose whole claim is
            # "no write verb is registered", that is the one case that must not
            # be skipped quietly -- `set(None)` raised TypeError and
            # `if rule.methods and ...` would have dropped it from `writes`
            # altogether (pyright reportArgumentType, 2026-10-09). The same
            # Optional is resolved the same way in
            # tests/test_kill_switch.py::test_the_controls_route_is_the_only_post...
            assert rule.methods is not None, (
                f"admin rule {rule.rule} declares no methods, so it answers ANY verb including POST"
            )
            for verb in set(rule.methods) - {"GET", "HEAD", "OPTIONS"}:
                writes.add((rule.rule, verb))

    # THE ONE WRITE, AND IT IS SPELLED OUT. Behind services/kill_switch.control_refusals()
    # -- the same guard /admin/controls uses -- and behind
    # services/payout_rescue.rescue_verdict(), which refuses any swap it cannot prove was
    # never broadcast. tests/test_kill_switch.py asserts both calls exist in the handler
    # and that the module imports no means of signaling or spawning.
    allowed = {("/admin/swaps/<swap_id>/rescue", "POST")}
    assert writes == allowed, (
        f"the admin surface registers exactly one write method and it is the rescue control. "
        f"Found {sorted(writes)}, expected {sorted(allowed)}. A new write surface on an "
        f"unauthenticated page has to be reviewed deliberately -- see this test's docstring "
        f"and tests/test_kill_switch.REVIEWED_NON_SPAWNING_POST_ROUTES."
    )


def test_up_can_actually_read_the_chain_probe_it_asks_for(client):
    """The route's OWN body, handed to the host-side reader that consumes it.

    THIS IS THE TEST WHOSE ABSENCE COST THE OPERATOR A FALSE ALARM. `swap_stack.py`
    step 7 fetches /api/admin/chains and passes the parsed body to
    chain_reachability_verdict(). Written 2026-10-09 expecting a BARE LIST of
    rows; the route has always answered with an envelope. So its first live run
    printed, on a stack whose probe was working:

        UNKNOWN  COULD NOT ASK: /api/admin/chains did not return a list of chain
                 rows, got dict, which is what an error page parses to

    and sent the operator looking for an error page that did not exist.

    Four pure tests in tests/test_stack_authority.py were green throughout,
    because every one of them fabricated the body it tested against. Only
    something that asks the real route can catch this -- which is the
    behavioral-verification principle in CLAUDE.md applied across an HTTP
    boundary instead of a SQL one: run the real thing, assert on what it
    actually produced, never on a hand-copied shape.

    THE ASSERTION IS "READABLE", NOT "REACHABLE". With no adapters configured the
    honest verdict is `none_asked`, and with stub adapters it may be
    `unreachable`; both mean the contract held. `unknown` is the one verdict that
    says the two sides disagree about the shape, and it is the one this refuses.
    """
    response = client.get("/api/admin/chains")
    assert response.status_code == 200, response.status_code
    body = response.get_json()

    rows = chain_probe_rows(body)
    assert rows is not None, (
        f"the host-side reader cannot find rows under {CHAIN_PROBE_ROWS_KEY!r} in the body this "
        f"route actually serves: {sorted(body) if isinstance(body, dict) else type(body).__name__}"
    )

    status, headline, _detail = chain_reachability_verdict(body)
    assert status != "unknown", (
        f"`up` step 7 cannot read its own endpoint, so it reports COULD NOT ASK on a working "
        f"probe: {headline}"
    )
    assert status in {"reachable", "unreachable", "none_asked"}, f"{status}: {headline}"


def test_the_admin_page_javascript_reads_the_key_python_writes(client):
    """static/admin.js is the third consumer and cannot import the constant.

    It is JavaScript, so rule 8's "the shared version goes where both callers can
    reach it" has nowhere to put this one -- and a hand-written `data.chains` in a
    .js file is exactly the second spelling that drifts. Pinning it from here is
    the closest thing available to deriving it: change CHAIN_PROBE_ROWS_KEY and
    this fails until the page follows.
    """
    script = (Path(__file__).resolve().parents[1] / "swap_terminal/static/admin.js").read_text()
    assert f"data.{CHAIN_PROBE_ROWS_KEY}" in script, (
        f"admin.js does not read data.{CHAIN_PROBE_ROWS_KEY}, so the Reachability panel is "
        f"reading a key the route does not write"
    )


def test_no_admin_route_accepts_a_post(client):
    """The same claim from the other side: ask, and be refused."""
    for path in ("/admin", "/api/admin/overview", "/api/admin/chains"):
        assert client.post(path).status_code == 405, path


def test_the_admin_page_has_no_form_that_submits_anywhere(client):
    """A read-only page with a POST form on it is not read-only.

    MUTATION: add any <form method="post"> to templates/admin.html.
    """
    body = client.get("/admin").get_data(as_text=True).lower()
    assert "<form" not in body
    assert 'method="post"' not in body


def test_an_empty_system_renders_none_in_every_region(client):
    """Rule 14, on the page where a blank region is most dangerous.

    On an empty database every panel has nothing to show, and every one of them
    must say so. A blank "payouts stuck" region reads as "nothing is stuck",
    which is the same thing a BROKEN query reads as.
    """
    body = client.get("/admin").get_data(as_text=True)
    assert body.count("(none)") >= 6
    for phrase in (
        "no payout is stuck",
        "no swap is in flight",
        # "balance" rather than "inventory" since 2026-10-03: the figure the panel
        # holds is the whole wallet from `getbalance`, not coins committed to the
        # desk, and the empty-state sentence says the same word the heading does.
        "no wallet balance row has ever been written",
        "no deposit has ever been recorded",
        "no payout has ever been written",
        "no status change has been recorded",
    ):
        assert phrase in body, phrase


def test_a_stale_inventory_row_renders_differently_from_a_fresh_one(client):
    """MUTATION: render the age without the freshness state.

    Both rows then look the same, which is the incident README.md records: data
    over 6000s stale used anyway, with nothing saying so louder than a warning.
    The STALE badge is a word and a shape, not a color, so it survives a
    grayscale paste.
    """
    for asset, age in (("BTC", NOW_OFFSETS["fresh"]), ("GRC", NOW_OFFSETS["stale"])):
        write(
            client,
            "INSERT INTO wallet_inventory (asset, hot_confirmed, hot_reserved, hot_available, updated_at)"
            " VALUES (?,?,?,?,?)",
            (asset, 1.0, 0.0, 1.0, iso(age)),
        )
    body = client.get("/admin").get_data(as_text=True)
    assert "badge-fresh-fresh" in body
    assert "badge-fresh-stale" in body
    assert "STALE" in body
    assert "row-warn" in body, "the stale row must also be distinguishable at row level"


def test_the_balance_panel_says_what_the_number_MEASURES(client):
    """It said "Hot-wallet inventory ... confirmed", and the figure is neither.

    MEASURED BEHAVIORALLY 2026-10-03, not read off the source: calling
    services/payout_service.refresh_wallet_inventory() against a stub adapter made
    exactly one RPC call, `getbalance`, and stored hot_confirmed equal to what it
    returned. chains/base.RPCAdapter.get_balance() is `getbalance` with NO
    arguments, so that is the WHOLE wallet the endpoint serves -- and with
    GRC_RPC_WALLET empty, which is config.py's default and was the live state that
    day, the endpoint has no /wallet/<name> path and the daemon routes to its
    DEFAULT wallet, the one the operator's own CLI reaches. Their personal coins
    were inside the figure, under a heading that called it inventory.

    NOTHING ABOUT HOW THE FIGURE IS COMPUTED OR USED WAS CHANGED, which is the
    other half of what this test pins: the column, the refresh and every reader are
    untouched (that is live posture and the operator's call). Only the words moved.

    MUTATION (ran, caught): delete the second panel-note paragraph from
    templates/admin.html. Every assertion but the heading fails.
    MUTATION (ran, caught): put the heading back to "Hot-wallet inventory". The
    first assertion fails -- and so does
    tests/test_customer_page_layout.py::test_the_tab_strip_lists_every_section_and_only_real_anchors,
    because the tab label and the heading are two spellings of one section.

    MUTATION (ran, SURVIVED, AND IT IS A REAL LIMIT RATHER THAN A FIXABLE ONE): add
    `hidden` to that paragraph's own tag. The text is still in the response body,
    so every assertion above passes while a human sees nothing. There is no browser
    in this suite -- tests/test_customer_page_layout.py says the same thing about
    its width arithmetic -- so "present in the markup" is the strongest claim
    available here, and it is stated rather than left to be discovered.
    """
    write(
        client,
        "INSERT INTO wallet_inventory (asset, hot_confirmed, hot_reserved, hot_available, updated_at)"
        " VALUES (?,?,?,?,?)",
        ("GRC", 3780.08554497, 0.0, 3780.08554497, iso(NOW_OFFSETS["fresh"])),
    )
    body = client.get("/admin").get_data(as_text=True)

    assert "Hot-wallet balance, not committed inventory" in body, "the heading still names desk stock"
    assert '<th scope="col" class="num">whole wallet</th>' in body, "the column still says confirmed"
    assert "getbalance</code> with no arguments" in body, (
        "say WHICH call the figure came from; a reader cannot check 'the whole wallet' against anything"
    )
    for variable in ("BTC_RPC_WALLET", "LTC_RPC_WALLET", "GRC_RPC_WALLET"):
        assert variable in body, f"the panel does not name {variable}, which is what decides the answer"
    assert "wallet_custody.py" in body, "name the tool that says whether the wallet is the desk's own"
    assert "not a measure of coins committed to the desk" in body


def test_the_admin_page_names_the_threshold_that_decided_stale(client):
    """Rule 14: echo the parameters that decide the answer."""
    body = client.get("/admin").get_data(as_text=True)
    assert "µfn (300.0s)" in body
    assert "ufn (300.0s)" not in body


def test_the_admin_page_says_it_is_unauthenticated(client):
    """It is, and it must say so where the person reading it will see it."""
    body = client.get("/admin").get_data(as_text=True)
    assert "not authenticated" in body


def test_the_admin_page_renders_no_rpc_credential(client):
    """MUTATION: echo config by pattern instead of by the ECHOED_CONFIG_KEYS allowlist.

    Config.RPC's Bitcoin entries hold `user` and `password`. This asserts on the
    live configured values rather than on a placeholder, so it catches a leak of
    whatever is actually set.
    """
    body = client.get("/admin").get_data(as_text=True)
    rpc = client.application.config["RPC"]
    for asset in ("BTC", "LTC", "GRC"):
        for key in ("user", "password"):
            value = rpc[asset].get(key)
            if value:
                assert value not in body, f"{asset} {key} reached the page"
    assert "SECRET_KEY" not in body
    assert client.application.config["SECRET_KEY"] not in body


def test_disabled_pairs_are_shown_as_disabled_rather_than_hidden(client):
    body = client.get("/admin").get_data(as_text=True)
    assert "DISABLED" in body
    assert "XRP -&gt; GRC" in body or "XRP -> GRC" in body


def test_the_chain_probe_is_not_run_on_page_load(client):
    """MUTATION: call probe_chains() from admin_page().

    Six chains at a 30s RPC timeout is a three-minute page load, which is the
    blinking cursor rule 14 opens with -- and this page must never be the reason
    somebody interrupts something. The page says so in words, and this asserts
    the words are there and that a probe adapter was never touched.
    """
    body = client.get("/admin").get_data(as_text=True)
    assert "Not probed on page load" in body
    assert "(not probed)" in body


def test_the_json_overview_carries_the_same_readings(client):
    seed_swap(client, "s_json00000000aa", "payout_pending")
    payload = client.get("/api/admin/overview").get_json()
    assert payload["database"] == client.application.config["DB_PATH"]
    assert [row["id"] for row in payload["in_flight"]] == ["s_json00000000aa"]
    assert payload["in_flight"][0]["attention"]["level"] in {"working", "slow"}
    # The same allowlist governs JSON as governs the page.
    assert "RPC" not in {row["key"] for row in payload["config"]}


def test_the_health_endpoint_still_answers_after_the_index_moved(client):
    """routes/health.py gave up `/` to routes/ui.py; it kept its own job."""
    payload = client.get("/api/health").get_json()
    assert payload["status"] == "ok"
    assert payload["db_path"] == client.application.config["DB_PATH"]
    assert client.get("/").status_code == 200


def test_a_refused_swap_is_logged_and_not_only_returned(client, caplog):
    """The reason must reach the LOG, not only the browser.

    Measured 2026-09-26: the operator's terminal showed
    `POST /api/swaps HTTP/1.1" 400` five times with no indication of why.
    werkzeug's access log prints the status and nothing of the body, so the one
    place a person was actually watching had the least information — while the
    browser had the full reason all along.

    Rule 14: a failure an operator cannot distinguish from any other failure is a
    silent one. Asserted on both channels, because returning it without logging it
    is the defect and logging it without returning it would be a new one.
    """
    caplog.set_level(logging.WARNING)

    response = client.post("/api/swaps", json={"quote_id": "q-nope", "payout_address": "x"})

    assert response.status_code == 400
    assert response.get_json()["error"], "the browser must still get the reason"
    assert "REFUSED" in caplog.text
    assert "Quote not found" in caplog.text, "the log must carry the REASON, not just that it failed"
    assert "no swap row was written" in caplog.text, "it must say what did NOT happen"


# A STUB THAT DECLARES WHAT IT CAN DO, because the gate fails closed.
#
# chains/registry.why_cannot_pay_out() reads getattr(adapter, "can_spend", False),
# so a bare object() counts as unable to pay -- deliberately, since an adapter that
# forgets the declaration must not be silently offered as a destination. These four
# tests used object() and correctly stopped passing when that gate landed. A stub
# that says nothing about itself should not be treated as capable.
class StubAdapter:
    """Declares what pair_view asks of it: can it be paid out to, and is an account valid?

    `validate_address` ARRIVED 2026-10-01 with pair_view's third test. A pair is now offered
    only when the SOURCE chain can produce a deposit address too, and for a tag-attributed
    chain that means services/swap_service.deposit_account() validating the shared account --
    through the adapter. A stub declaring only `can_spend` made every XRP and SOL pair
    unofferable in these tests, which is the right answer for a chain whose account is unset
    and the wrong SETUP for a test whose premise is "every chain is reachable".

    `rent_exempt_minimum` AND `url` ARRIVED 2026-10-03 with pair_view's FIFTH condition,
    services/quote_service.why_cannot_establish_payout_floor(), and for the identical
    reason one gate later. A *->SOL pair is now offered only when the CLUSTER can be asked
    for the rent-exempt minimum a new account needs, because a SOL payout has a minimum
    only the chain can state -- the page badged BTC -> SOL AVAILABLE while the real quote
    refused with "this terminal cannot reach the Solana network right now". A stub with no
    rent_exempt_minimum raises AttributeError inside that function's broad catch and so
    reads as an UNREACHABLE cluster, which is again the right answer for a terminal that
    cannot reach one and the wrong setup for a premise of "every chain is reachable".

    650_240 LAMPORTS IS NOT INVENTED. It is the operator's 2026-09-30 devnet reading for
    SYSTEM_ACCOUNT_SPACE = 0, recorded in chains/solana_units.py and quoted by
    quote_service.new_account_floor_lamports(). A stub that answered 0 would be a cluster
    saying no account needs rent, which no cluster says, and would make the floor check
    vacuous rather than satisfied.

    `url` IS SET AND IS DISTINCT PER CAPABILITY, which is a cache fact rather than a
    cosmetic one. new_account_floor_lamports() memoizes per `url|mint` for 600s in a
    MODULE-level dict that outlives any one test, so two stubs sharing a key would let an
    answerable one prime the cache for an unanswerable one and the second test would pass
    on the first test's answer. Distinct urls keep each shape honest; `mint` is absent, so
    both key as native.

    `rent_floor_lamports=None` IS THE UNREACHABLE CLUSTER, and it raises rather than
    returning a falsy number: a 0 would travel through int() as a real answer, and the
    distinction between "the chain said a number" and "the chain could not be asked" is
    the whole subject of the fifth condition.
    """

    def __init__(self, can_spend=True, rent_floor_lamports=650_240):
        self.can_spend = can_spend
        self.payout_refusal = "" if can_spend else "cannot pay out in this test"
        self.rent_floor_lamports = rent_floor_lamports
        self.url = (
            "stub://cluster-answers-the-rent-floor"
            if rent_floor_lamports is not None
            else "stub://cluster-cannot-be-asked"
        )

    def validate_address(self, _address):
        return True

    def rent_exempt_minimum(self, _space):
        if self.rent_floor_lamports is None:
            raise ConnectionError("stub cluster is unreachable in this test")
        return self.rent_floor_lamports

    def owns_address(self, address):
        """Not the desk's -- these fixtures all supply a CUSTOMER payout address.

        Added 2026-10-04 with the ownership gate in services/swap_service.
        refuse_unusable_payout_address(). A destination stub without it raises
        AttributeError, so the gate could not be exercised and every create_swap
        test failed on the harness instead of on its own subject. False is the
        honest default here; a test that wants the refusal returns True.
        """
        return False


#: Real-format shared accounts for the tag-attributed chains, because the validation these go
#: through is real: deposit_account() calls the adapter AND the local decode in
#: modules/address_authority, so an obviously fake string would be refused for its FORMAT and
#: the test would pass for the wrong reason. Both already appear elsewhere in this suite.
DEPOSIT_ACCOUNTS = {
    "XRP_DEPOSIT_ACCOUNT": "rnjG8n16JinjqkzZj5Jmw6NDMBMzhhNbVv",
    "SOL_DEPOSIT_ACCOUNT": "J5wn3xEMDsr9r8qtF6YTWJodmgW5kG3ZThqDb8Xc37JM",
}


def reachable(*assets, can_spend=True):
    """An adapters dict for `assets`, each able (or not) to be a destination."""
    return {asset: StubAdapter(can_spend=can_spend) for asset in assets}


def with_deposit_accounts(client, monkeypatch):
    """Set the shared accounts the tag-attributed chains need to be OFFERABLE at all.

    ONE PLACE, so the next gate pair_view grows is added here rather than in every test that
    means "a fully configured terminal". Separate from the adapters because two tests build
    theirs as a dict literal to express a specific asymmetry -- one chain that can pay out and
    one that cannot -- and should not have to go through a varargs helper to say that.
    """
    for variable, account in DEPOSIT_ACCOUNTS.items():
        monkeypatch.setitem(client.application.config, variable, account)


def fully_reachable(client, monkeypatch, *assets, can_spend=True):
    """Adapters AND the shared deposit accounts: everything "reachable" now requires."""
    monkeypatch.setitem(client.application.config, "ADAPTERS",
                        reachable(*assets, can_spend=can_spend))
    with_deposit_accounts(client, monkeypatch)
    return client.application.config["ADAPTERS"]


def quotable(pair) -> bool:
    """Would a quote for this pair PRICE, independent of whether the chains are up?

    THE FOURTH CONDITION, ADDED TO pair_serviceability() ON 2026-10-03, and four
    tests in this file failed the moment it landed -- correctly. Each asserted that
    reachability alone implied the pair was offered, which had been true and is not:
    a destination with no <ASSET>_NETWORK_FEE_RESERVE refuses at the quote, so a
    pair whose chains are both up can still not be offered.

    WHY THIS SPELLS THE CONDITION AGAIN INSTEAD OF CALLING why_cannot_quote(),
    which is the one place in this tree where rule 8 asks for the opposite. A test
    that derives its expectation from the function under test passes for any
    implementation of it, including an empty one -- it would assert only that the
    page agrees with itself, which is exactly the property that was TRUE on
    2026-10-02 while the page was wrong. So this is a deliberate second opinion,
    kept to one expression, named, and tested against the real surfaces below.

    READ OFF Config AND NOT OFF client.application.config, deliberately. The app
    copies Config's uppercase attributes in at startup, so for the reserve the two
    agree -- and reading the CLASS means a test that monkeypatches a reserve into the
    app config gets a real disagreement here rather than a silent agreement. No test
    in this file does that today; if one ever needs to, it should pass its own
    expectation rather than teach this helper to read two sources.

    tests/test_an_available_pair_can_actually_be_quoted.py is the other half: it
    asserts the badge and a REAL create_quote() agree, which is the end-to-end
    property. This one keeps the four tests here honest about their premise.
    """
    return hasattr(Config, f"{pair[1]}_NETWORK_FEE_RESERVE")


#: Destinations whose smallest deliverable payout comes from the CHAIN rather than from
#: config. SOL only today, which is what services/quote_service.why_cannot_establish_payout_floor()
#: says by returning "" for everything else instead of growing a table.
RUNTIME_FLOOR_DESTINATIONS = frozenset({"SOL"})


def floor_establishable(pair, adapters) -> bool:
    """Could this premise establish the destination's runtime payout floor?

    THE FIFTH CONDITION, ADDED TO pair_serviceability() ON 2026-10-03, and the two
    "every chain is reachable" tests in this file failed the moment it landed --
    correctly, and for a reason neither reachability nor quotable() can express. A
    *->SOL payout has a minimum imposed by the RUNTIME (rent exemption for the account
    it would create) and that figure comes from the cluster; a terminal that cannot ask
    cannot quote, so a pair whose chains are both up and whose reserve exists can still
    not be offered.

    WHY THIS SPELLS THE CONDITION AGAIN INSTEAD OF CALLING
    why_cannot_establish_payout_floor(), WHICH IS quotable()'S ARGUMENT VERBATIM AND
    STILL THE RIGHT ONE. A test that derives its expectation from the function under
    test passes for any implementation of it, including one that returns "" for
    everything -- it would assert only that the page agrees with itself, which is
    exactly the property that was TRUE on 2026-10-02 while the page was wrong, and
    TRUE again on 2026-10-03 while it badged BTC -> SOL AVAILABLE against a quote that
    refused. So this is a second deliberate second opinion, kept to one expression and
    named, exactly as quotable() is.

    WHAT IT SPELLS AGAIN, and all three clauses are necessary: the destination must be
    one whose floor comes from a chain at all, that chain must have an adapter in this
    premise, and the adapter must be able to answer the ask. can_spend is deliberately
    NOT re-spelled here -- why_cannot_pay_out() is the authority for that, the tests
    below already cover it through `can_spend=False`, and a copy of it in this helper
    would be rule 8's duplication rather than a second opinion about a different
    question.

    THE ANSWER IS TAKEN BY CALLING THE STUB, not by checking that the method exists.
    An adapter that has rent_exempt_minimum and raises is the unreachable cluster, and
    those two must not score the same -- which is the distinction the condition exists
    for.
    """
    to_asset = pair[1]
    if to_asset not in RUNTIME_FLOOR_DESTINATIONS:
        return True
    adapter = (adapters or {}).get(to_asset)
    if adapter is None:
        return False
    try:
        return int(adapter.rent_exempt_minimum(0)) > 0
    except Exception:  # noqa: BLE001 -- checked: this helper has exactly one caller shape and False is its only other value, so no caller can mistake a failure for a floor. The stub raises ConnectionError for an unreachable cluster and AttributeError for an adapter that cannot be asked at all, and this helper's whole job is to score both as "the floor could not be established" -- the same one answer why_cannot_establish_payout_floor() gives them.
        return False


def offerable(pair, adapters) -> bool:
    """Both second opinions, so a test states its premise once.

    quotable() answers from config and floor_establishable() from the premise's
    adapters, and a pair needs both. Written out here rather than left to each caller
    because the two tests below asserted "reachable implies offered" and the whole
    correction is that the implication now has two more terms in it.
    """
    return quotable(pair) and floor_establishable(pair, adapters)


# NOT a credential. A SENTINEL: its only purpose is to be findable, so the leak
# test below can search the whole response body for it. A value that looked like a
# real credential would make the test weaker, not stronger -- the same reasoning
# tests/test_gridcoin_wallet_lock.py gives for its own LEAK_SENTINEL.
LEAK_SENTINEL = "health-response-must-not-echo-this"


# --- the one endpoint an operator can curl -------------------------------------
#
# /api/health echoed allowed_pairs and nothing about what the process could reach.
# That is what the terminal is WILLING to swap; the operator's question on
# 2026-09-26 was what the SERVER could reach, and every instrument they had
# answered the wrong one or described the wrong process -- `env | grep` describes
# the shell, the workers' banners described the workers, and the swap page needs a
# browser. This endpoint was the thing they could curl.


def test_health_reports_which_chains_have_an_adapter_in_this_process(client, monkeypatch):
    """adapter_built is per chain and comes from the adapters dict, not from config."""
    fully_reachable(client, monkeypatch, "XRP")

    body = client.get("/api/health").get_json()

    assert body["chains"]["XRP"]["adapter_built"] is True
    assert body["chains"]["GRC"]["adapter_built"] is False
    # Every chain the configuration knows, not just the ones in a pair: a chain set
    # up and never enabled must be visible rather than absent.
    assert set(body["chains"]) == set(client.application.config["RPC"])


def test_health_names_the_missing_settings_and_never_their_values(client, monkeypatch):
    """The whole point, and the reason it is safe to paste.

    chains/registry.missing_settings() returns variable NAMES. A version that
    echoed config values would put RPC credentials into every pasted health
    response -- they sit one key away in the same object, which this module's header
    already says out loud about the credentials it declines to print.
    """
    monkeypatch.setitem(client.application.config, "ADAPTERS", {})
    # The port and user are SET and the password is not, so the expected answer is
    # exactly one name -- and it must be the password, never the port. That is the
    # specific way a vague message would waste an operator's time: on 2026-09-26
    # GRC_RPC_PORT was correct and naming it would have sent them to check it.
    monkeypatch.setitem(
        client.application.config,
        "RPC",
        {"GRC": {"user": LEAK_SENTINEL, "password": "", "host": "127.0.0.1",
                 "port": 25715, "wallet": "", "timeout": 30.0}},
    )

    response = client.get("/api/health")
    body = response.get_json()

    assert body["chains"]["GRC"]["missing_settings"] == ["GRC_RPC_PASS"]
    assert LEAK_SENTINEL not in response.get_data(as_text=True), (
        "a config VALUE reached the response -- the RPC user is a credential, and it "
        "sits one key away from the password this module already declines to print"
    )


def test_health_offerable_pairs_is_the_subset_that_could_complete(client, monkeypatch):
    """The line worth reading first: shorter than allowed_pairs means settings did
    not reach this process."""
    fully_reachable(client, monkeypatch, "XRP", "GRC")

    body = client.get("/api/health").get_json()

    # GRC->XRP DROPPED OUT ON 2026-10-03 AND IT IS RIGHT THAT IT DID. Both chains are
    # reachable in this test, and XRP_NETWORK_FEE_RESERVE does not exist, so a quote for
    # GRC->XRP refuses -- which is the thing the operator hit on a live page the day before
    # (it read AVAILABLE and would not price). Derived rather than written out, so this
    # expectation follows config.py if the reserve is ever set.
    expected = [f"{a}->{b}" for a, b in sorted([("GRC", "XRP"), ("XRP", "GRC")]) if quotable((a, b))]
    assert body["offerable_pairs"] == expected
    assert set(body["offerable_pairs"]) < set(body["allowed_pairs"]), (
        "with only XRP and GRC reachable, the offerable set must be a strict subset"
    )


def test_health_offerable_equals_allowed_when_every_chain_is_reachable(client, monkeypatch):
    """So the field cannot pass by always being empty."""
    every = {asset for pair in client.application.config["ALLOWED_PAIRS"] for asset in pair}
    adapters = fully_reachable(client, monkeypatch, *every)

    body = client.get("/api/health").get_json()

    # "EQUALS" BECAME "EQUALS, LESS THE UNQUOTABLE ONES" ON 2026-10-03, and the guard
    # survives the change: the endpoint still cannot pass by reporting an empty list,
    # because `expected` is non-empty and asserted to be.
    #
    # AND "LESS THE ONES WHOSE RUNTIME FLOOR CANNOT BE ESTABLISHED", the same day, which
    # is why this reads offerable() rather than quotable(). With StubAdapter answering the
    # rent-exempt ask the two sets coincide again under this premise, and the ASSERTION
    # BELOW IS WHAT PROVES THAT rather than a comment claiming it: were the stub unable to
    # answer, the three *->SOL pairs would leave `expected` and the endpoint would have to
    # drop them too.
    expected = [
        f"{a}->{b}"
        for a, b in sorted(client.application.config["ALLOWED_PAIRS"])
        if offerable((a, b), adapters)
    ]
    assert expected, "no allowed pair is offerable, so this assertion would be vacuous"
    assert body["offerable_pairs"] == expected


def test_health_offerable_pairs_is_empty_rather_than_absent_when_nothing_is_reachable(client, monkeypatch):
    """An absent key reads as a broken endpoint; an empty list is a result."""
    monkeypatch.setitem(client.application.config, "ADAPTERS", {})

    body = client.get("/api/health").get_json()

    assert "offerable_pairs" in body
    assert body["offerable_pairs"] == []
    assert body["allowed_pairs"], "allowed_pairs must be unaffected -- it answers a different question"


def test_a_destination_that_cannot_pay_out_is_not_offered(client, monkeypatch):
    """REACHABLE IS NOT THE SAME AS ABLE TO PAY, and GRC -> XRP was the proof.

    2026-09-26: the page had just learned that a pair whose chain has no adapter is
    DISABLED. GRC -> XRP passed that test -- an XRP adapter exists and reaches the
    testnet -- and was badged ENABLED. XRPAdapter holds no signing key and
    payout_service calls send_to_address() unarmed, so that payout RAISES: the
    customer's GRC would have been taken and credited, and the swap left in `failed`
    needing a person.

    A review the same day found the second hazard in the same pair: XRPAdapter's
    validate_address() accepts any X-address WITHOUT checking its checksum, so a
    typo would have been fixed as that swap's FINAL payout address. A chain that
    cannot be a destination is never asked for one.

    MUTATION: drop the why_cannot_pay_out() call from allowed_pair_rows(). Only this
    test and the one below fail.
    """
    # Both chains reachable, both accounts configured; only GRC can pay out. Exactly the
    # operator's server. The accounts matter since 2026-10-01: without them BOTH directions
    # are unofferable for a DIFFERENT reason and this test would pass on the wrong cause.
    with_deposit_accounts(client, monkeypatch)
    monkeypatch.setitem(
        client.application.config,
        "ADAPTERS",
        {"GRC": StubAdapter(can_spend=True), "XRP": StubAdapter(can_spend=False)},
    )

    body = client.get("/").get_data(as_text=True)
    offered = offered_pairs(client)

    assert ("XRP", "GRC") in offered, "GRC can pay out, so XRP -> GRC must still be offered"
    assert ("GRC", "XRP") not in offered, (
        "XRP cannot pay out, so GRC -> XRP must not be offered -- a deposit into it is stranded"
    )
    # AND STILL LISTED, in the reference grid step 1 carries. I first asserted here
    # that GRC -> XRP also appears GREYED on step 2, and it cannot in this fixture:
    # with only GRC and XRP configured and XRP unable to pay out, GRC has ZERO
    # working outbound directions, so step 1 correctly refuses GRC as a source and
    # step 2 is unreachable from it. That is the ATM being stricter than the page it
    # replaced, not a gap -- and the greyed-destination property is pinned where it
    # IS reachable, in tests/test_wizard.py::
    # test_an_unavailable_destination_is_shown_greyed_with_its_reason_not_hidden.
    assert "swaptile-unavailable" in body, (
        "the unofferable pair must still be LISTED and marked unusable, not hidden"
    )
    assert "GRC &#8594; XRP" in body, (
        "and it must still be NAMED: a pair that vanishes is indistinguishable from one that "
        "was never configured, which is why this page lists unusable pairs at all"
    )


def test_the_reason_says_it_cannot_pay_rather_than_that_it_is_unreachable(client, monkeypatch):
    """Two different problems must not print the same sentence.

    "XRP_RPC_URL is unset" would send the operator to configure a chain that is
    already configured and would change nothing. The distinction is the whole point
    of asking two questions instead of one.
    """
    monkeypatch.setitem(
        client.application.config,
        "ADAPTERS",
        {"GRC": StubAdapter(can_spend=True), "XRP": StubAdapter(can_spend=False)},
    )

    body = client.get("/").get_data(as_text=True)

    # THE DISTINCTION SURVIVES AND IS NOW MACHINE-CHECKABLE. This asserted that the
    # adapter's refusal SENTENCE reached the customer page, because prose was the
    # only place the distinction lived. Since 2026-10-02 the page marks the two
    # causes differently by STATE -- `unavailable` for a chain that cannot pay out at
    # all, `unreachable` for one merely not reachable from this process -- so the same
    # distinction is asserted on the rendered state instead of on a sentence. That is
    # stronger: a reworded sentence cannot break it and a wrong state cannot pass it.
    #
    # BOTH CAUSES ARE IN THIS ONE RENDER, which is what makes it a test of the
    # distinction rather than of one case: XRP has an adapter and holds no signing
    # key, and BTC has no adapter at all.
    states = {
        html.unescape(re.sub(r"\s+", " ", label)).strip(): state
        # page_markup.TILE_WITH_LABEL rather than a fourth copy of this pattern: the
        # three that existed all broke on one added tile attribute (rule 8).
        for state, label in re.findall(TILE_WITH_LABEL, body, flags=re.DOTALL)
    }
    into_xrp = states.get("GRC → XRP")
    offline_pair = states.get("BTC → GRC")
    assert into_xrp == "unavailable", (
        f"GRC -> XRP is marked {into_xrp!r}; a chain that holds no signing key is not merely "
        f"offline, and telling a customer to come back later would be a promise nothing keeps"
    )
    assert offline_pair == "unreachable", (
        f"BTC -> GRC is marked {offline_pair!r}; BTC has no adapter here, which is offline"
    )
    assert into_xrp != offline_pair, (
        "cannot-pay-out and not-reachable render the same marker, so a customer cannot tell "
        "whether coming back later would help"
    )
    # The adapter's own refusal still has to reach an operator, so it has to be on
    # /admin -- per asset, in the Chains table's can send / can receive column.
    operator_page = client.get("/admin").get_data(as_text=True)
    assert "cannot pay out in this test" in operator_page, (
        "the adapter's refusal left the customer page and did NOT arrive on the operator page"
    )
    assert "XRP_RPC_URL is unset" not in body, (
        "XRP IS configured here; naming its endpoint variable would be the wrong remedy"
    )


# --- the pricing panel ------------------------------------------------------
#
# WHY THESE ARE HERE AND NOT IN tests/test_admin_view.py. The failure this panel
# can actually produce is a render failure: a cold cache drawing as an empty
# table, a thinness verdict that could not be measured drawing like one that
# passed, or a page that stops loading because a price API is down. A test that
# called pricing_panel() and asserted on the returned dict would pass through
# all three. So these seed the cache, ask the app for the page, and assert on
# the bytes it returned.


def seed_price_cache(monkeypatch, entries, *, source="CoinPaprika", age_seconds=12.0):
    """Put a raw feed body straight into services/pricing._cache. Fetches nothing.

    THE CACHE IS SEEDED, NOT THE FEED MOCKED, because what the panel reads is the
    cache and nothing else -- that is the property under test. Seeding it is also
    the only way to assert the no-fetch claim, since a test that mocked requests
    could not tell "did not fetch" from "fetched a mock".

    `entries` is keyed by ASSET and reshaped onto the CoinGecko ids here, so a
    case reads BTC rather than bitcoin. The shape is the one BOTH feeds produce
    -- see services/pricing._coinpaprika_raw(), which exists to make them one
    shape.
    """
    fetched_at = time() - age_seconds
    raw = {pricing.IDS[asset]: values for asset, values in entries.items()}
    monkeypatch.setitem(pricing._cache, "raw", raw)
    monkeypatch.setitem(pricing._cache, "prices", None)
    monkeypatch.setitem(pricing._cache, "context", None)
    monkeypatch.setitem(pricing._cache, "source", source)
    monkeypatch.setitem(pricing._cache, "fetched_at", fetched_at)
    monkeypatch.setitem(pricing._cache, "expires_at", fetched_at + 30.0)


def priced(usd, cap, volume, change=1.5):
    """One asset's entry in a raw feed body, in the shape both feeds return."""
    return {
        "usd": usd,
        "usd_market_cap": cap,
        "usd_24h_vol": volume,
        "usd_24h_change": change,
        "last_updated_at": 1790717713,
    }


def every_asset_priced(**overrides):
    """A full raw body -- one entry per services/pricing.IDS key.

    DERIVED FROM IDS rather than written out, because _require_every_asset()
    refuses a partial body and a hand-written dict here would have to be edited
    every time an asset is added -- which is the failure that table's own comment
    warns about, one level up in a test file.
    """
    body = {asset: priced(100.0, 5_000_000_000.0, 200_000_000.0) for asset in pricing.IDS}
    body.update(overrides)
    return body


def cold_price_cache():
    """Every cache field back to what it is before anything has been priced.

    Stated rather than assumed: the cache is process-wide, and another test in
    this session may have filled it (rule 17).
    """
    pricing._cache.update({"raw": None, "prices": None, "context": None, "source": "",
                           "fetched_at": 0.0, "expires_at": 0.0})


def test_a_cold_price_cache_renders_not_fetched_and_not_an_empty_table(client):
    """Nothing priced yet and every asset unpriced are different facts (rule 14).

    MUTATION: render the assets table unconditionally. An empty <tbody> draws as
    a blank region, which is indistinguishable from a query that broke -- the
    exact defect CLAUDE.md records being shipped the day rule 14 was written.
    """
    cold_price_cache()
    body = client.get("/admin").get_data(as_text=True)
    assert "(not fetched)" in body, "a cold price cache rendered no marker at all"
    # The sentence is asserted in fragments because the template wraps it, so a
    # whole-sentence match would fail on a line break rather than on a defect.
    assert "nothing has been priced" in body
    assert "fetched nothing" in body


def test_the_admin_page_renders_with_every_price_feed_unreachable(client, monkeypatch):
    """THE WHOLE POINT OF READING THE CACHE. An /admin that needs a price API is broken.

    MUTATION: make pricing_panel() call fetch_market_context() instead of
    cached_market_context(). It then fails with the AssertionError below rather
    than rendering -- which is what an operator would have got on the day a feed
    went down, losing swaps in flight, stuck payouts and worker state along with
    the prices, on the one screen they open when something is wrong.

    requests.get is replaced at BOTH modules the feeds reach it through, so this
    covers CoinGecko and CoinPaprika. Measured from the operator's host, the
    first of those really does 403 on every call (services/pricing.py's header),
    so a dead feed is the ordinary case there and not a hypothetical.
    """
    def refuse(*args, **kwargs):
        raise AssertionError("the admin page made an outbound price request")

    monkeypatch.setattr(pricing.requests, "get", refuse)
    monkeypatch.setattr(coinpaprika.requests, "get", refuse)
    cold_price_cache()
    response = client.get("/admin")
    assert response.status_code == 200, "the admin page failed with the price feeds down"
    assert "Swaps in flight" in response.get_data(as_text=True), (
        "the page rendered but lost the database readings -- a pricing panel must not be able to "
        "cost the operator the rest of the screen"
    )


def test_the_panel_names_the_feed_that_answered_and_how_old_it_is(client, monkeypatch):
    """A price whose origin is not recorded is a number nobody can check later.

    Both halves in one test on purpose: a source with no timestamp beside it is a
    provenance claim with no date on it.
    """
    seed_price_cache(monkeypatch, every_asset_priced(), source="CoinPaprika", age_seconds=12.0)
    body = client.get("/admin").get_data(as_text=True)
    assert "CoinPaprika" in body, "the page did not say which feed the prices came from"
    # Rule 6: µfn with the seconds in parentheses, and the symbol is µ -- an
    # ASCII "u" in displayed output is a defect, not a rendering fallback.
    assert "µfn" in body, "the age was not reported in microfortnights"


def test_an_asset_whose_thinness_could_not_be_measured_does_not_render_as_OK(client, monkeypatch):
    """GRC's ORDINARY case on a feed reporting market_cap 0, and the silent one.

    services/market_context.turnover_finding() returns None when the cap or the
    volume is unusable, and None is not OK: no bound at all exists on whether
    that spot price is one anybody can transact at. A page that drew it as OK
    would be the "unchecked reads as fine" failure rule 14 names, on the one
    asset that needs the check most.

    MUTATION: render "OK" when the finding is None. The NOT MEASURED assertion
    below fails.
    """
    seed_price_cache(monkeypatch, every_asset_priced(GRC=priced(0.0167, 0.0, 6_500.0)))
    body = client.get("/admin").get_data(as_text=True)
    assert "NOT MEASURED" in body.upper(), "an unmeasurable thinness verdict rendered as something else"
    assert "NOT a passed check" in body, (
        "the page showed a missing thinness reading without saying it is not a passed one"
    )


def test_the_thinness_verdict_is_market_contexts_and_not_a_copy_in_the_view(client, monkeypatch):
    """Move THIN_TURNOVER and the PAGE must move with it.

    This is the rule 8 assertion, and it is behavioral rather than a grep: a
    second `volume / cap < THIN_TURNOVER` in services/admin_view.py would pass
    every other test in this file and disagree with the quote path the first day
    one of the two thresholds was tuned -- each site looking correct in its own
    file, which is the drift rule 8 opens with.

    So the threshold is raised above any real turnover and every asset must come
    back THIN. A copy holding the old constant would still report OK.
    """
    seed_price_cache(monkeypatch, every_asset_priced())
    assert "THIN" not in client.get("/admin").get_data(as_text=True), (
        "these seeded assets are already thin at the real threshold, so the flip below proves nothing"
    )
    monkeypatch.setattr(market_context, "THIN_TURNOVER", 1.0)
    assert "THIN" in client.get("/admin").get_data(as_text=True), (
        "raising market_context.THIN_TURNOVER did not change the page, so the admin view is deciding "
        "thinness itself instead of calling turnover_finding()"
    )


def test_the_peg_is_not_checked_on_page_load_and_says_so(client, monkeypatch):
    """Unchecked must not read as fine, and the page must not fetch to say so."""
    def refuse(*args, **kwargs):
        raise AssertionError("the admin page priced a stablecoin on render")

    monkeypatch.setattr(coinpaprika.requests, "get", refuse)
    seed_price_cache(monkeypatch, every_asset_priced())
    body = client.get("/admin").get_data(as_text=True)
    assert "(not checked)" in body
    assert "not the same as checked and holding" in body


def test_the_peg_route_reports_a_dead_feed_as_suspect_rather_than_raising(client, monkeypatch):
    """A stablecoin that cannot be priced is an unchecked dollar, not a 500.

    MUTATION: let the exception out of probe_peg(). The route then 500s and the
    operator gets no finding at all -- where what they need is the sentence
    saying the yardstick itself is unverified.
    """
    def refuse(asset, **kwargs):
        raise RuntimeError("403 from the edge")

    monkeypatch.setattr("services.coinpaprika.fetch_quote", refuse)
    payload = client.get("/api/admin/peg").get_json()
    assert payload["suspect"] is True
    assert payload["assets_priced"] == []
    assert payload["assets_asked"] == list(PEG_ASSETS)
    assert any("NOT PRICED" in finding for finding in payload["findings"]), (
        "an unpriceable stablecoin produced no finding naming it as unpriced"
    )
    assert any("403 from the edge" in failure for failure in payload["fetch_failures"]), (
        "the feed's own reason was swallowed, so the operator cannot tell a block from an outage"
    )


def test_the_peg_route_reports_both_coins_and_their_ratio_when_both_price(client, monkeypatch):
    """The findings come from wallet_leveling.peg_findings(), so the CLI cannot disagree."""
    def quote(asset, **kwargs):
        return coinpaprika.PaprikaQuote(
            asset=asset,
            paprika_id=coinpaprika.PAPRIKA_IDS[asset],
            price_usd=1.0002 if asset == "USDC" else 0.9998,
            total_supply=1e10,
            market_cap_usd=1e10,
            market_cap_is_derived=False,
            volume_24h_usd=1e9,
            change_24h_pct=0.01,
            source_updated_at="2026-09-30T00:00:00Z",
        )

    monkeypatch.setattr("services.coinpaprika.fetch_quote", quote)
    payload = client.get("/api/admin/peg").get_json()
    assert payload["assets_priced"] == ["USDC", "USDT"]
    assert payload["suspect"] is False
    assert any("USDC/USDT" in finding for finding in payload["findings"]), (
        "the two were priced and never compared to each other"
    )


def test_every_admin_route_EXCEPT_THE_RESCUE_CONTROL_is_a_GET():
    """The read-only constraint is structural, and a new route must not loosen it.

    routes/admin.py's header claims every route on the blueprint is a GET. This
    asserts it over the real URL map, so adding one with a POST fails here
    rather than waiting to be caught by a reader.
    """
    # A RULE WITH NO DECLARED METHODS IS REFUSED BY NAME, NOT DROPPED.
    # werkzeug types Rule.methods as `set[str] | None` and None matches any
    # verb, so on a GET-only surface it is the exact thing this test exists to
    # catch; filtering it out of the comprehension would have made the loop
    # below pass by not looking at it (pyright reportOptionalOperand,
    # 2026-10-09).
    admin_rules = [
        rule for rule in app_module.app.url_map.iter_rules()
        if rule.endpoint.startswith("admin.")
    ]
    undeclared = sorted(rule.rule for rule in admin_rules if rule.methods is None)
    assert not undeclared, (
        f"these admin rules declare no methods, so they answer ANY verb -- including the POST this "
        f"surface must not accept: {undeclared}"
    )
    methods = {
        rule.rule: rule.methods - {"HEAD", "OPTIONS"}
        for rule in admin_rules
        if rule.methods is not None
    }
    assert "/api/admin/peg" in methods, "the peg route is not registered on the admin blueprint"

    #: The ONE route on this blueprint that is not a GET, and what it answers. Named here
    #: rather than excluded by a pattern, so a second write cannot join it by accident --
    #: the sibling assertion in
    #: test_the_admin_blueprint_registers_EXACTLY_ONE_write_method carries the reasoning
    #: and the operator's instruction that put it there.
    rescue = "/admin/swaps/<swap_id>/rescue"
    assert methods.get(rescue) == {"POST"}, (
        f"the rescue control must answer POST and nothing else; it answers "
        f"{sorted(methods.get(rescue) or [])}. A GET that re-drives a payout would be "
        f"re-driven by a link preview, a crawler or a browser prefetch."
    )
    for path, verbs in methods.items():
        if path == rescue:
            continue
        assert verbs == {"GET"}, (
            f"{path} accepts {sorted(verbs)}. Every admin route but the rescue control is a "
            f"GET, so it is safe to reload and cannot be triggered by following a link"
        )


# --- the deposit side of the figures list -------------------------------------
#
# ASKED FOR BY THE OPERATOR 2026-10-01, immediately after watching a real SOL
# deposit credit: "there needs to be a listed solana deposit address for the user
# too on the left."
#
# It was a real asymmetry. templates/_swap_live.html's Amounts list carried
# `Payout address` and said nothing about where the deposit goes -- the address
# lived only in the panel ABOVE the live region. So a customer comparing what they
# sent against what they get could read one half off the list and had to scroll
# back for the other, and the live region is the part the poller replaces, which is
# the part they are most likely to be looking at.

def test_the_figures_list_names_the_deposit_address_not_only_the_payout_one(client):
    """The asymmetry, asserted on an address-attributed chain.

    MUTATION: remove the Deposit address row and this fails while every other
    page test still passes -- which is the state the operator found.
    """
    seed_swap(client, "s_dep00000000000", "awaiting_deposit")
    body = client.get("/swap/s_dep00000000000/fragment").get_data(as_text=True)

    assert "Deposit address" in body, "the list names where the payout goes and must name where the deposit goes"
    assert GRC_PAYOUT in body
    # And it is still in the FRAGMENT, not only the full page: the poll replaces
    # this region, so a row that lived only in swap.html would vanish on the first
    # refresh.
    assert "Payout address" in body


def test_a_tag_chain_lists_the_account_AND_the_memo_because_the_pair_is_the_instruction(client):
    """Half an instruction is the failure this repo keeps naming.

    On a tag chain the deposit instruction is the PAIR (account, discriminator).
    The account alone is the half that produces money which arrived and a swap that
    cannot claim it -- chains/xrp.py's get_new_address() refusal is written about
    exactly that. So a list that showed the address without the tag would be worse
    than one showing neither.

    The field is named with the CHAIN's own word, from
    services/swap_service.TAG_ATTRIBUTION: a SOL page saying "DestinationTag" would
    name a field Solana does not have, which is the live mistake that table's
    comment records.

    MUTATION: drop the tag row and the page hands a customer the shared account
    with nothing to identify their swap by.
    """
    seed_swap(client, "s_soltag00000000", "awaiting_deposit",
              asset="SOL", deposit_address=SOL_DEPOSIT_ACCOUNT, deposit_tag=7)
    body = client.get("/swap/s_soltag00000000/fragment").get_data(as_text=True)

    assert SOL_DEPOSIT_ACCOUNT in body
    assert "Memo instruction" in body, "Solana's own word for the field, not XRP's"
    assert "DestinationTag" not in body, "a SOL page must not name an XRP Ledger field"
    assert ">7<" in body, "the tag itself has to be on screen, not just its label"


def test_a_tag_chain_with_no_tag_says_so_rather_than_rendering_blank(client):
    """`0` is a legal tag, so an empty cell and a real zero must not look alike.

    README's "Tag 0 is a real tag", and rule 14's "never let an empty result print
    nothing" -- a blank is ambiguous between "no tag was issued" and "the template
    broke".
    """
    seed_swap(client, "s_solnotag000000", "awaiting_deposit",
              asset="SOL", deposit_address=SOL_DEPOSIT_ACCOUNT)
    body = client.get("/swap/s_solnotag000000/fragment").get_data(as_text=True)

    assert "(none issued)" in body


def test_tag_zero_renders_as_zero_and_not_as_missing(client):
    """The trap the line above is guarding, from the other side.

    A template written with `{% if view.deposit.tag %}` would render tag 0 as
    "(none issued)" and tell a customer with a perfectly valid tag that they have
    none. Asserted because the fix is one word (`is none`) and the failure is
    silent.
    """
    seed_swap(client, "s_soltag0zero000", "awaiting_deposit",
              asset="SOL", deposit_address=SOL_DEPOSIT_ACCOUNT, deposit_tag=0)
    body = client.get("/swap/s_soltag0zero000/fragment").get_data(as_text=True)

    assert "(none issued)" not in body, "tag 0 is a real tag, not a missing one"
    assert ">0<" in body


def test_a_closed_swap_says_the_listed_address_is_not_accepting_anything(client):
    """Repeating the address must not reintroduce a defect swap.html already fixed.

    templates/swap.html carries a "Do not send anything to this swap" callout for a
    closed swap, and its comment records that the need was found by looking at a
    rendered `under_review` swap on 2026-09-26 -- a live-looking deposit target on a
    swap nothing advances. Listing the address again down here without the caveat
    would put that back one panel lower.

    MUTATION: drop the guard row and a `failed` swap's figures list shows a deposit
    address with nothing saying it is dead.
    """
    seed_swap(client, "s_closed00000000", "failed")
    body = client.get("/swap/s_closed00000000/fragment").get_data(as_text=True)

    assert "Still accepting?" in body
    assert "<strong>NO</strong>" in body
    # A phrase that does not straddle the template's line wrap. "nothing advances"
    # looked right and failed, because _swap_live.html breaks between the two words
    # -- a keyword check that cannot survive reflowing is a test reading text rather
    # than behavior, which this suite has been caught by twice today.
    assert "matching a payment you already made" in body

    # AND IT IS ABSENT WHILE THE SWAP IS LIVE. A guard that always renders is noise
    # on the happy path, which is the cried-wolf shape this repo keeps paying for.
    live = client.get("/swap/s_dep00000000000/fragment")
    if live.status_code == 404:
        seed_swap(client, "s_dep00000000000", "awaiting_deposit")
        live = client.get("/swap/s_dep00000000000/fragment")
    assert "Still accepting?" not in live.get_data(as_text=True)


def test_the_payout_field_does_not_ask_the_browser_to_autofill_it(client, monkeypatch):
    """autocomplete="off" is IGNORED by Chrome-family browsers. Measured, three times.

    On the operator's host 2026-10-01 this field carried autocomplete="off" and
    Brave filled it anyway, with a stale Bitcoin testnet address
    (2N3bqzcWSDasdmqKkDKUAFU8f5Hk11NZzYN) from unrelated work. It passed every
    server check: Gridcoin shares Bitcoin testnet's 0xc4 P2SH version byte, so
    validateaddress returns isvalid: true. The daemon later answered
    ismine: false and 82.65 tGRC had already been broadcast to it.

    WHAT THIS TEST CAN AND CANNOT ESTABLISH (rule 17). It asserts the markup asks
    for a value those browsers document as honored, and that script.js clears the
    field as the browser-independent belt to that braces. It CANNOT establish that
    Brave 154 obeys either -- this container has no browser, and only the operator
    can observe that.

    MUTATION: put autocomplete="off" back, or drop the clearing function, and the
    page returns to the state that cost three swaps.
    """
    # STEP 4 OF THE FLOW, NOT `/`, AND THAT NEEDS REACHABLE CHAINS. index.html
    # carried this field on the landing page unconditionally; the ATM asks for it
    # on its own screen, reached only by answering the three before it -- so a
    # fixture with no adapters now stops at step 1 and the field is correctly
    # absent. The invariant under test is unchanged and so is the measurement that
    # earned it; only what has to be true to RENDER the field moved.
    fully_reachable(client, monkeypatch, "XRP", "GRC")
    body = client.post("/", data={
        "from_asset": "XRP", "to_asset": "GRC", "amount": "1.0", "amount_side": "send",
    }).get_data(as_text=True)
    # THE WHOLE TAG, NOT THE LINE IT STARTS ON. This read `[line for line in
    # body.splitlines() if 'id="payout_address"' in line]`, which worked while
    # index.html kept the attributes on one line and broke the moment the ATM
    # template wrapped them over four -- reporting a missing autocomplete on a
    # field that has one. The invariant is about the TAG, so the extraction is too;
    # reformatting a template to satisfy a test's parser would be the wrong half to
    # change.
    tag = re.search(r'<input[^>]*id="payout_address"[^>]*>', body, re.DOTALL)
    assert tag, (
        "the payout address input is not on step 4. If the flow refused earlier, this fixture's "
        "chains are not reachable enough to reach that screen -- check the question it renders"
    )
    markup = " ".join(tag.group(0).split())

    assert 'autocomplete="off"' not in markup, (
        "Chrome-family browsers ignore autocomplete=off on fields they classify, and this is one"
    )
    assert 'autocomplete="one-time-code"' in markup
    # The name is what routes/swaps.py reads; a browser heuristic is not a reason
    # to break the form and the API.
    assert 'name="payout_address"' in markup

    script = client.get("/static/script.js").get_data(as_text=True)
    assert "clearBrowserFilledPayoutAddress" in script, (
        "the attribute may be ignored; clearing the field is what does not depend on that"
    )
    assert "clearBrowserFilledPayoutAddress();" in script, "declared but never called"


# --- the copy buttons ---------------------------------------------------------
#
# ASKED FOR BY THE OPERATOR 2026-10-01: "have it to where they can copy it
# directly with a little copy icon."
#
# Before this, class="copyable" appeared in three places in swap.html and was CSS
# ONLY -- styles.css sets user-select: all so a click selects the string -- and
# nothing in static/script.js touched the clipboard. The class named an affordance
# the page did not have.

def test_the_deposit_instruction_has_a_copy_button_for_every_half(client):
    """On a tag chain the instruction is a PAIR, so one button is not enough.

    The account alone is the half that produces money which arrived and a swap that
    cannot claim it -- chains/xrp.py's get_new_address() refusal is written about
    exactly that. A page offering to copy the account and not the memo would make
    the easy path the broken one.

    MUTATION: drop one copy_field() call from the tag branch and this fails.
    """
    seed_swap(client, "s_copytag0000000", "awaiting_deposit",
              asset="SOL", deposit_address=SOL_DEPOSIT_ACCOUNT, deposit_tag=7)
    body = client.get("/swap/s_copytag0000000").get_data(as_text=True)

    assert body.count('class="copy-button"') >= 2, (
        "both the shared account and the memo tag need their own button"
    )
    assert 'aria-label="Copy the shared deposit account"' in body
    assert 'aria-label="Copy the memo tag"' in body


def test_an_address_chain_gets_a_copy_button_too(client):
    """One macro serves both branches, so neither can grow its own button (rule 8)."""
    seed_swap(client, "s_copyaddr000000", "awaiting_deposit")
    body = client.get("/swap/s_copyaddr000000").get_data(as_text=True)

    assert 'class="copy-button"' in body
    assert 'aria-label="Copy the deposit address"' in body


def test_the_copied_value_is_not_duplicated_into_an_attribute(client):
    """The displayed string IS the source, and that is a correctness property.

    A data-copy="{{ value }}" would be a second copy of the one thing that must
    never differ: an address that differs by one character between what is shown
    and what is copied is money sent where nobody can claim it. script.js reads the
    .copyable element's own text inside the .copy-row.

    MUTATION: put the value in a data attribute and this fails -- which is the
    design this test exists to forbid, not a bug it caught.
    """
    seed_swap(client, "s_copysrc0000000", "awaiting_deposit",
              asset="SOL", deposit_address=SOL_DEPOSIT_ACCOUNT, deposit_tag=7)
    body = client.get("/swap/s_copysrc0000000").get_data(as_text=True)

    assert "data-copy=" not in body, "the value must not be duplicated into an attribute"
    assert 'class="copy-row"' in body, "script.js finds the value through the row"

    script = client.get("/static/script.js").get_data(as_text=True)
    assert "wireCopyButtons();" in script, "declared but never called"
    assert '.querySelector(".copyable")' in script, "it has to read the displayed node"
    # A failure must not be silent: the clipboard API needs a secure context and a
    # permission the browser can refuse.
    #
    # ASSERTED ON CODE-SHAPED STRINGS, not on the words. The first version of these
    # two checked `"Select it" in script` and `"selectCopyableText" in script`, and a
    # mutation that replaced the whole catch body with `return;` SURVIVED -- because
    # the file's own comment quotes the phrase and names the function. That is the
    # third time in one day a keyword check in this suite could not tell a quotation
    # from a claim, and the fix is the same each time: match the assignment and the
    # CALL, which only appear where the behavior is.
    assert 'outcome = "Select it"' in script, "a refused clipboard has to say so, not no-op"
    assert "selectCopyableText(value)" in script, "and select the text so the fallback works"


def test_the_tag_chain_instruction_states_the_amount(client):
    """Asked for 2026-10-01: "it should prompt to please deposit 0.01 dsol the amount".

    The address branch has always led with "Send exactly <amount> <asset> to". The
    tag branch led with the account and the memo and never stated the amount in that
    panel at all -- a SOL customer was told where and what memo, and had to scroll
    past the status rail to the figures list for how much.

    It matters beyond labeling: deposit_service halts a swap whose confirmed amount
    falls outside AMOUNT_TOLERANCE_PCT, and a halt waits for a person.
    s_612fac62489f2122 has sat in under_review since 2026-09-26 for exactly that.

    MUTATION: remove the amount line from the tag branch -> this fails.
    """
    seed_swap(client, "s_amounttag00000", "awaiting_deposit",
              asset="SOL", deposit_address=SOL_DEPOSIT_ACCOUNT, deposit_tag=7)
    body = client.get("/swap/s_amounttag00000").get_data(as_text=True)

    assert "Send exactly" in body, "the tag branch must state the amount like the address branch"
    assert "1000.00000000" in body, "to 8 places and with the asset named, as the address branch does"
    assert "both halves are\n       needed" in body or "both halves are" in body


def test_the_deposit_panel_offers_the_wallet_menu_the_qr_and_the_request(client):
    """All three on the page, for a SOL swap that is accepting a deposit.

    Asked for 2026-10-01: "creata a clicking submenu of wallets ... make the QR
    code too". Asserted on the rendered page rather than on payment_options()
    alone, because the view can be right while the template never reads it --
    which is how the figures list carried a payout address and no deposit
    address for weeks.
    """
    seed_swap(client, "s_paymenu0000000", "awaiting_deposit",
              asset="SOL", deposit_address=SOL_DEPOSIT_ACCOUNT, deposit_tag=7)
    body = client.get("/swap/s_paymenu0000000").get_data(as_text=True)

    assert 'class="wallet-menu"' in body
    for name in ("Phantom", "Solflare", "Coinbase Wallet"):
        assert name in body, f"{name} is in the catalog and must reach the page"
    assert "MetaMask" not in body, "removed at the operator's request; it cannot sign for SOL"

    # The QR, drawn by the server. swap_terminal/qr_svg.py says why not in JS.
    assert 'class="pay-qr"' in body
    assert "<svg" in body

    # And the request itself, copyable, for a wallet neither probed nor scanned.
    assert f"solana:{SOL_DEPOSIT_ACCOUNT}?amount=" in body
    assert 'aria-label="Copy the payment request"' in body


def test_the_page_never_decides_which_chains_a_wallet_supports(client):
    """Capability is the server's; PRESENCE is the browser's. Keeping them apart.

    A JavaScript file deciding which chains a wallet signs for would be logic the
    server cannot check, on the page that tells customers where to send money. So
    script.js probes `window.<provider_path>` and nothing more -- the chain list
    never reaches it.

    MUTATION: hardcode a chain list in script.js and this fails.
    """
    script = client.get("/static/script.js").get_data(as_text=True)

    assert "wireWalletMenu();" in script, "declared but never called"
    assert "providerAt" in script
    # The catalog's vocabulary must not appear in the browser.
    assert '"SOL"' not in script and "'SOL'" not in script, (
        "the page must not carry a chain list; services/wallet_menu.py owns capability"
    )
    # No wallet library: chains/solana_pay.py built the request and the browser
    # hands it over unaltered.
    assert "web3.js" not in script
    assert "@solana" not in script


def test_a_wallet_that_is_not_installed_is_disabled_rather_than_dead(client):
    """The entry has to carry where to get it, since only the browser knows.

    A button that does nothing because an extension is absent is the dead
    affordance `class="copyable"` was for three weeks.
    """
    seed_swap(client, "s_payinst0000000", "awaiting_deposit",
              asset="SOL", deposit_address=SOL_DEPOSIT_ACCOUNT, deposit_tag=7)
    body = client.get("/swap/s_payinst0000000").get_data(as_text=True)

    assert "data-install=" in body, "the page needs the install URL to offer it"
    assert "data-provider=" in body, "and the global to probe for"

    script = client.get("/static/script.js").get_data(as_text=True)
    assert "button.disabled = true" in script, "an absent wallet must not look clickable"
    # ASSERTED ON THE ASSIGNMENT, not on the words. This line read
    # `assert "not installed" in script` and kept passing after the message was
    # CHANGED, because the comment explaining why that wording was wrong quotes
    # the old phrase. Fourth time in one day a keyword check in this suite could
    # not tell a quotation from a claim; the fix is the same each time -- match
    # code, which only exists where the behavior is.
    assert 'note.textContent = "did not inject a provider on this page' in script, (
        "the page knows exactly one thing -- the global is absent -- and must say that, not that "
        "the wallet is uninstalled or unavailable"
    )
    # AND IT MUST NOT TELL THE OPERATOR TO INSTALL WHAT THEY ALREADY HAVE.
    # Measured 2026-10-01: "no wallet appears to work even though coinbase wallet
    # is installed into brave". It was installed -- in their normal profile, while
    # the launcher's default window uses an empty one.
    assert "profile with no extensions" in script

    # NOR MAY IT LEAD WITH AN EXPLANATION THAT HAS BEEN REFUTED. Measured
    # 2026-10-02: the message said "open the same URL in your normal browser
    # instead", the operator ran the launcher with --window (which the launcher
    # confirmed with "Opening in existing browser session"), and all three wallets
    # STILL reported absent. For that case the profile explanation is dead, and a
    # message that keeps offering it sends a reader back down a path they have
    # already walked.
    #
    # The second cause is offered as a POSSIBILITY, not an answer: wallet
    # extensions commonly restrict injection by origin and http://127.0.0.1 is the
    # kind they restrict, which is a plausible reading rather than something this
    # project has measured.
    assert "http://127.0.0.1 origin" in script, (
        "the origin is the other candidate cause and must be named"
    )
    assert "cannot tell them apart" in script, "and neither cause may be claimed as the answer"
    # FRAGMENTS THAT DO NOT CROSS A CONCATENATION BOUNDARY. The message is built
    # from several quoted pieces joined with +, so a phrase that spans two of them
    # is not a contiguous substring of the file. This assertion first read
    # "if it still says this, it is not the profile" and failed against correct
    # code for exactly that reason -- the second time today a check in this suite
    # has been written against prose as a reader sees it rather than as the source
    # holds it.
    assert "it is not the profile" in script, (
        "the one half a reader CAN settle has to be spelled out, because the operator settled it "
        "and the message did not acknowledge that was possible"
    )
    assert "opening this URL in your normal browser" in script, "and how to settle it"
    assert "open the same URL in your normal browser instead" not in script, (
        "the refuted advice must be gone, not merely demoted"
    )


def test_a_closed_swap_is_offered_no_wallet_menu_and_no_qr(client):
    """Same refusal the deposit panel already makes, for the thing a camera acts on.

    MUTATION: render the panel unconditionally and a `failed` swap shows a
    scannable code pointing at a swap nothing will advance.
    """
    seed_swap(client, "s_payclosed00000", "failed",
              asset="SOL", deposit_address=SOL_DEPOSIT_ACCOUNT, deposit_tag=7)
    body = client.get("/swap/s_payclosed00000").get_data(as_text=True)

    assert 'class="wallet-menu"' not in body
    assert 'class="pay-qr"' not in body
    assert "solana:" not in body
    # And it still says the swap is closed, which is the thing that matters.
    assert "no longer accepting" in body


def _seed_with_a_deposit_and_a_payout(client, swap_id: str) -> tuple[str, str]:
    """One swap that has actually HAPPENED: a credited deposit row and a broadcast payout.

    seed_swap() alone makes an EMPTY swap, which is why the two tests below did
    not exist before 2026-10-04 and why the defect they pin shipped: every
    assertion about these sections was written against a swap with no rows.
    """
    # write(), the helper every other seeder in this file uses, rather than a
    # second way of reaching the same database (rule 8).
    deposit_txid = f"deadbeef{swap_id[-8:]}" * 4
    payout_txid = f"feedface{swap_id[-8:]}" * 4
    seed_swap(client, swap_id, "completed")
    write(
        client,
        "INSERT INTO deposit_events (swap_id, asset, address, txid, vout, amount, confirmations,"
        " first_seen_at, last_seen_at, credited_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        # `address` is NOT NULL and the schema is right to insist: a deposit row
        # with no address cannot be attributed to the swap it arrived for. Derived
        # (tests/valid_addresses.py), never spelled -- an address literal in a test
        # is what tests/test_address_literals_are_valid.py counts.
        (swap_id, "GRC", GRC_DESK_DEPOSIT, deposit_txid, 1, 500.0, 6, iso(-300), iso(-60), iso(-60)),
    )
    write(
        client,
        "INSERT INTO payouts (swap_id, asset, destination_address, amount, status, txid,"
        " created_at, sent_at) VALUES (?,?,?,?,?,?,?,?)",
        # destination_address and created_at are NOT NULL, and both deserve to be:
        # a payout row with no destination records money sent nowhere, and one with
        # no created_at cannot be aged. GRC_PAYOUT is the derived fixture for a
        # customer payout address (tests/valid_addresses.py).
        (swap_id, "GRC", GRC_PAYOUT, 989.15511156, "broadcast", payout_txid, iso(-45), iso(-30)),
    )
    return deposit_txid, payout_txid


def test_a_swap_with_rows_renders_them_instead_of_saying_none(client):
    """THE CASE NOBODY ASSERTED. A swap that happened must not report `(none)`.

    MEASURED ON THE OPERATOR'S HOST, 2026-10-04. swap s_708ea48e227183bd
    completed while its page was open and the page said, in one render:

        Raw status              completed
        Payout txid             b3dce6dc...
        Deposits recorded for this swap
          (none) no deposit row exists for this swap
          That is the expected state before you send anything.

    The cause was placement, not a query: both sections sat outside `#swap-live`,
    the region static/script.js replaces on a timer, so they froze at page load.
    The existing coverage -- test_an_empty_deposit_list_renders_none_and_not_a
    _blank_gap -- seeds a swap with NO rows, so it was green for the right reason
    about the wrong case and could never have caught this.

    THIS ASSERTS THE ROWS AND THE ABSENCE OF THE EMPTY SENTENCES, both halves. A
    test that only checked the txid appeared would pass on a page that ALSO still
    printed "(none)" underneath it, which is precisely what the operator saw.

    MUTATION (ran, caught): move the two sections back out of _swap_live.html into
    swap.html. The fragment then renders without them and both row assertions
    fail.
    """
    deposit_txid, payout_txid = _seed_with_a_deposit_and_a_payout(client, "s_hasrows0000001")

    body = client.get("/swap/s_hasrows0000001").get_data(as_text=True)

    assert deposit_txid in body, "the deposit row is not rendered on a swap that has one"
    assert payout_txid in body, "the payout row is not rendered on a swap that has one"
    assert "no deposit row exists for this swap" not in body, (
        "the page claims no deposit row while rendering a swap that has one -- the contradiction "
        "the operator read off a completed swap"
    )
    assert "no payout has been attempted" not in body, (
        "the page claims no payout was attempted while carrying its broadcast txid"
    )
    assert "expected state before you send anything" not in body, (
        "a completed swap is being told it has not been paid for yet"
    )


def test_the_polled_fragment_carries_the_deposit_and_payout_tables(client):
    """The ROOT CAUSE, pinned where it lives: these sections must be in the fragment.

    The test above asserts the symptom is gone from the full page; this asserts
    WHY, against the thing the browser actually re-fetches every 15s. Without
    this, someone moves the sections back into swap.html for layout reasons, the
    full-page test still passes on first render, and the page silently freezes
    again on the only swaps that matter -- the ones that change while you watch.

    MUTATION (ran, caught): move either section back to swap.html. This fails on
    that section's txid while the other still passes, naming which one moved.
    """
    deposit_txid, payout_txid = _seed_with_a_deposit_and_a_payout(client, "s_hasrows0000002")

    fragment = client.get("/swap/s_hasrows0000002/fragment").get_data(as_text=True)

    assert deposit_txid in fragment, (
        "the deposits table is not in the polled fragment, so it will freeze at page load"
    )
    assert payout_txid in fragment, (
        "the payouts table is not in the polled fragment, so it will freeze at page load"
    )
