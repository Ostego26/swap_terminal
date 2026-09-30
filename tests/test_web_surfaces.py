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

import logging
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from time import time

import pytest
from db import SCHEMA, dict_factory
from network_target import configuring_variable
from services import coinpaprika, market_context, pricing
from services.wallet_leveling import PEG_ASSETS
from valid_addresses import GRC_PAYOUT

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
    write(
        client,
        "INSERT INTO quotes (id, from_asset, to_asset, input_amount, quoted_rate, fee_bps, network_fee_reserve,"
        " output_amount_estimate, expires_at, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (f"q_{swap_id}", asset, "BTC", 1000.0, 1e-7, 150, 0.00002, 0.0001, iso(-600), iso(600)),
    )
    write(
        client,
        "INSERT INTO swaps (id, quote_id, from_asset, to_asset, deposit_address, payout_address,"
        " expected_input_amount, actual_input_amount, quoted_rate, fee_bps, network_fee_reserve,"
        " output_amount_estimate, status, min_confirmations, deposit_txid, payout_txid, created_at, updated_at,"
        " credited_at, completed_at, expires_at, failed_reason)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            swap_id, f"q_{swap_id}", asset, "BTC", deposit_address, "bc1addr", 1000.0, None, 1e-7, 150, 0.00002,
            0.0001, status, min_conf, None, None, iso(600), iso(updated_ago), None, None, iso(-600), None,
        ),
    )


# --- the customer surface ---------------------------------------------------


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
    body = client.get("/").get_data(as_text=True)

    for from_asset, to_asset in allowed:
        option = f'value="{from_asset}:{to_asset}"'
        if from_asset in reachable_assets and to_asset in reachable_assets:
            assert option in body, f"{from_asset}->{to_asset} is reachable and must be offered"
        else:
            assert option not in body, (
                f"{from_asset}->{to_asset} was offered, but one of its chains has no adapter -- "
                f"this is the shape that printed 'GRC' at the operator"
            )

    # LISTED, not hidden. An operator who configured six pairs and sees five has
    # no way to tell which one vanished or why (rule 14).
    listed = body.count('class="pair-label"')
    assert listed == len(allowed), f"{len(allowed)} pairs allowed, {listed} listed on the page"
    # The markup, not the bare word: the panel-note above the list explains what
    # DISABLED means, so a substring test would pass on the explanation alone.
    assert 'badge-word">DISABLED<' in body, "an unreachable pair must be badged DISABLED, not ENABLED"
    assert 'badge-word">ENABLED<' in body, "a reachable pair must still be badged ENABLED"
    assert "pair-off" in body, "a DISABLED pair must carry admin.html's own pair-off marker"


def test_a_disabled_pair_names_the_variable_that_would_enable_it(client, monkeypatch):
    """DISABLED with no reason is rule 14's blank gap wearing a badge.

    The operator's whole problem on 2026-09-26 was that they could not get from
    what the screen said to what to change. The reason comes from
    chains/registry.why_unconfigured(), which names the variable through
    network_target.configuring_variable() -- so this asserts the NAME reaches the
    page, not that a particular sentence was written.
    """
    monkeypatch.setitem(client.application.config, "ADAPTERS", reachable("XRP"))
    body = client.get("/").get_data(as_text=True)

    # GRC is in an allowed pair and has no adapter here, so its variable must be
    # on the page. Read from the authority rather than spelled, for the same
    # reason the pair list is.
    assert configuring_variable("GRC") in body, "the page must say what to set"
    assert ".env" in body, (
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
    monkeypatch.setitem(client.application.config, "ADAPTERS", reachable(*every_asset))
    body = client.get("/").get_data(as_text=True)

    for from_asset, to_asset in allowed:
        assert f'value="{from_asset}:{to_asset}"' in body, (from_asset, to_asset)
    assert 'badge-word">DISABLED<' not in body, "nothing is unreachable here, so nothing may be badged DISABLED"
    assert "pair-off" not in body, "no pair may be marked off when every pair is reachable"

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


def test_the_admin_blueprint_registers_no_write_method():
    """READ-ONLY, proven over the app's REAL url map rather than by reading the file.

    MUTATION: add `@bp.post("/admin/retry")` to routes/admin.py. This fails
    immediately. Reading the source and seeing only `@bp.get` proves nothing
    about the next edit, which is exactly what this constraint has to survive.
    """
    writes = set()
    for rule in app_module.app.url_map.iter_rules():
        if rule.endpoint.startswith("admin."):
            writes |= set(rule.methods) - {"GET", "HEAD", "OPTIONS"}
    assert writes == set(), f"the admin surface must be read-only; found {sorted(writes)}"


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
        "no wallet inventory row has ever been written",
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
    """Declares only what pair_view asks of it: can it be paid out to?"""

    def __init__(self, can_spend=True):
        self.can_spend = can_spend
        self.payout_refusal = "" if can_spend else "cannot pay out in this test"


def reachable(*assets, can_spend=True):
    """An adapters dict for `assets`, each able (or not) to be a destination."""
    return {asset: StubAdapter(can_spend=can_spend) for asset in assets}


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
    monkeypatch.setitem(client.application.config, "ADAPTERS", reachable("XRP"))

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
    monkeypatch.setitem(client.application.config, "ADAPTERS", reachable("XRP", "GRC"))

    body = client.get("/api/health").get_json()

    assert body["offerable_pairs"] == ["GRC->XRP", "XRP->GRC"]
    assert set(body["offerable_pairs"]) < set(body["allowed_pairs"]), (
        "with only XRP and GRC reachable, the offerable set must be a strict subset"
    )


def test_health_offerable_equals_allowed_when_every_chain_is_reachable(client, monkeypatch):
    """So the field cannot pass by always being empty."""
    every = {asset for pair in client.application.config["ALLOWED_PAIRS"] for asset in pair}
    monkeypatch.setitem(client.application.config, "ADAPTERS", reachable(*every))

    body = client.get("/api/health").get_json()

    assert body["offerable_pairs"] == body["allowed_pairs"]


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
    # Both chains reachable; only GRC can pay out. Exactly the operator's server.
    monkeypatch.setitem(
        client.application.config,
        "ADAPTERS",
        {"GRC": StubAdapter(can_spend=True), "XRP": StubAdapter(can_spend=False)},
    )

    body = client.get("/").get_data(as_text=True)

    assert 'value="XRP:GRC"' in body, "GRC can pay out, so XRP -> GRC must still be offered"
    assert 'value="GRC:XRP"' not in body, (
        "XRP cannot pay out, so GRC -> XRP must not be offered -- a deposit into it is stranded"
    )
    assert "pair-off" in body, "the unofferable pair must still be LISTED, with its reason"


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

    assert "cannot pay out in this test" in body, "the adapter's own refusal must reach the page"
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


def test_every_admin_route_including_the_peg_check_is_still_a_GET():
    """The read-only constraint is structural, and a new route must not loosen it.

    routes/admin.py's header claims every route on the blueprint is a GET. This
    asserts it over the real URL map, so adding one with a POST fails here
    rather than waiting to be caught by a reader.
    """
    methods = {
        rule.rule: rule.methods - {"HEAD", "OPTIONS"}
        for rule in app_module.app.url_map.iter_rules()
        if rule.endpoint.startswith("admin.")
    }
    assert "/api/admin/peg" in methods, "the peg route is not registered on the admin blueprint"
    for path, verbs in methods.items():
        assert verbs == {"GET"}, f"{path} accepts {sorted(verbs)}, and this surface is GET-only"
