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

import pytest
from db import SCHEMA, dict_factory
from network_target import configuring_variable

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
    deposit_address = overrides.get("deposit_address", "Saddr")
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
    reachable = {"XRP", "GRC"}
    monkeypatch.setitem(
        client.application.config, "ADAPTERS", {asset: object() for asset in reachable}
    )
    body = client.get("/").get_data(as_text=True)

    for from_asset, to_asset in allowed:
        option = f'value="{from_asset}:{to_asset}"'
        if from_asset in reachable and to_asset in reachable:
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
    monkeypatch.setitem(client.application.config, "ADAPTERS", {"XRP": object()})
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
    monkeypatch.setitem(
        client.application.config, "ADAPTERS", {asset: object() for asset in every_asset}
    )
    body = client.get("/").get_data(as_text=True)

    for from_asset, to_asset in allowed:
        assert f'value="{from_asset}:{to_asset}"' in body, (from_asset, to_asset)
    assert 'badge-word">DISABLED<' not in body, "nothing is unreachable here, so nothing may be badged DISABLED"
    assert "pair-off" not in body, "no pair may be marked off when every pair is reachable"

    # And nothing else -- with the disabled set DERIVED, not written out.
    #
    # This second half used to hardcode ("XRP", "SOL", "XMR"), which was the very
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
