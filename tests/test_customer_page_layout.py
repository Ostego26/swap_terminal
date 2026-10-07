"""The customer pages are tabulated and boxed, and the tabulating did not eat a fact.

Role: test (behavioral verification of templates/index.html, templates/swap.html,
      templates/_swap_live.html, static/styles.css, and the parts of
      templates/admin.html this pass touched, through the REAL Flask app)
Reads: the app's test client and a temporary database seeded with real rows
Writes: that temporary database only
Can move funds: no. Nothing here POSTs to /api/swaps and no adapter in this file
      can send.
Mainnet-safe: yes -- no socket is opened to any chain.
Live-safe: yes

WHY THIS FILE EXISTS. The operator's whole brief on 2026-10-02 was "it's too
spread out and needs to be tabulated and boxed", and the change that answers it
is almost entirely layout -- which is the class of change that silently deletes
information. A field tidied out of a template does not fail anything: the page
still renders, still returns 200, and the fact is simply gone. So every box this
pass created is asserted to RENDER WITH ITS HEADING, every empty case is asserted
to still say something, and the copy targets are asserted to carry the exact
untruncated string.

EVERY ASSERTION BELOW IS ON RENDERED OUTPUT, not on template source, with two
named exceptions that say so at the site: the two that read static/styles.css,
because a stylesheet has no rendered output without a browser and this container
has none (rule 17 -- these tests establish that the declarations EXIST, and
whether the result looks right is the operator's to judge).

THE ADDRESSES ARE DERIVED, NEVER SPELLED. tests/valid_addresses.py is the
authority; tests/test_address_literals_are_valid.py is the clean gate that stops
a hand-written one arriving. A placeholder here would also change what the page
does, because services/swap_view._address_problem() renders a warning instead of
a send target for an address that cannot receive money.
"""

import html
import pathlib
import re
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest
from db import SCHEMA, dict_factory

# IMPORTED, NOT COPIED (rule 8). `fully_reachable` is the one definition of "a
# fully configured terminal" in this suite -- an adapters dict AND the shared
# deposit accounts a tag chain needs to be offerable at all -- and its own
# docstring says it exists so the next gate pair_view grows is added in one
# place. A second copy here would agree on the day it was written and drift the
# first time that gate changes, which is the failure mode that rule is about.
from services.admin_view import PAGE_SECTIONS, chain_rows, pair_assets, pair_matrix, pair_rows
from services.pair_view import CUSTOMER_STATES, allowed_pair_rows
from services.swap_view import ATTRIBUTION_MODELS
from test_web_surfaces import StubAdapter, cold_price_cache, fully_reachable, with_deposit_accounts
from valid_addresses import GRC_PAYOUT, SOL_DEPOSIT_ACCOUNT

import app as app_module  # isort: skip

STYLESHEET = pathlib.Path(__file__).resolve().parents[1] / "swap_terminal" / "static" / "styles.css"


@pytest.fixture
def client(tmp_path, monkeypatch):
    """The real app, pointed at a per-test database on the real schema."""
    db_path = tmp_path / "layout.db"
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


def iso(seconds_ago: float) -> str:
    return (datetime.now(UTC) - timedelta(seconds=seconds_ago)).isoformat()


def seed_swap(client, swap_id, status="confirming", **overrides):
    """One swap and its quote, with the optional columns arriving as overrides."""
    asset = overrides.get("asset", "GRC")
    address = overrides.get("deposit_address", GRC_PAYOUT)
    tag = overrides.get("deposit_tag")
    reason = overrides.get("failed_reason")
    conn = sqlite3.connect(client.application.config["DB_PATH"])
    conn.execute(
        "INSERT INTO quotes (id, from_asset, to_asset, input_amount, quoted_rate, fee_bps,"
        " network_fee_reserve, output_amount_estimate, expires_at, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (f"q_{swap_id}", asset, "BTC", 1000.0, 1e-7, 150, 2e-5, 1e-4, iso(-600), iso(600)),
    )
    conn.execute(
        "INSERT INTO swaps (id, quote_id, from_asset, to_asset, deposit_address, deposit_tag,"
        " payout_address, expected_input_amount, actual_input_amount, quoted_rate, fee_bps,"
        " network_fee_reserve, output_amount_estimate, status, min_confirmations, deposit_txid,"
        " payout_txid, created_at, updated_at, credited_at, completed_at, expires_at, failed_reason)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (swap_id, f"q_{swap_id}", asset, "BTC", address, tag, "bc1addr", 1000.0,
         overrides.get("actual_input_amount"), 1e-7, 150, 2e-5, 1e-4, status, 6, None, None,
         iso(600), iso(30), None, None, iso(-600), reason),
    )
    conn.commit()
    conn.close()
    return swap_id


def rows_of(markup: str) -> list[tuple[str, str]]:
    """Every (row label, row value) pair the page rendered as a table row.

    A `th scope="row"` is the thing that guarantees the alignment the brief asked
    for -- it puts the label in column one and the value in column two, so every
    value on the page starts at the same x position by construction rather than
    because a grid's minmax() happened to agree. Asserting on these pairs is
    therefore asserting on the structure that delivers the alignment, which is as
    close to the visual claim as a test without a browser can get.
    """
    pairs = []
    for match in re.finditer(
        r'<th scope="row"[^>]*>(?P<label>.*?)</th>\s*(?P<cells>(?:<td.*?</td>\s*)+)',
        markup,
        flags=re.DOTALL,
    ):
        label = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", match.group("label"))).strip()
        value = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", match.group("cells"))).strip()
        # The templates write HTML entities (&#8594; for the arrow, &#181; for
        # micro), so a test that compared against the CHARACTERS would be
        # comparing against something the page never contains. Unescaped here,
        # once, instead of spelling entities into every assertion.
        pairs.append((html.unescape(label), html.unescape(value)))
    return pairs


def status_card_of(markup: str) -> str:
    """Exactly the .status-card element, found by matching its own closing tag.

    AN EARLIER VERSION OF THIS SLICED FROM THE CARD TO THE RAIL, AND A MUTATION
    SURVIVED IT. Moving the recorded reason to just past the card's own </div>
    still leaves it before `<ol class="rail">`, so a slice bounded by the rail
    contained the reason either way and the test passed on a page where the
    reason had been moved back out of the card -- which is the exact arrangement
    the test exists to refuse. Counting <div> against </div> from the card's
    start is what distinguishes the two.
    """
    start = markup.index('<div class="status-card')
    depth = 0
    for match in re.finditer(r"<div\b|</div>", markup[start:]):
        depth += 1 if match.group(0) != "</div>" else -1
        if depth == 0:
            return markup[start : start + match.end()]
    raise AssertionError("the status-card div is never closed")


def labels_of(markup: str) -> list[str]:
    return [label for label, _ in rows_of(markup)]


def value_for(markup: str, label: str) -> str:
    for row_label, value in rows_of(markup):
        if row_label == label:
            return value
    raise AssertionError(f"no table row is labeled {label!r}; the page has {labels_of(markup)}")


#: One direction tile, as the page renders it: the state class, then its contents.
#:
#: ONE PATTERN RATHER THAN THREE COPIES, and three copies is why this exists. Until
#: 2026-10-07 three tests each carried
#:
#:     r'<li class="swaptile swaptile-([a-z]+)">(.*?)</li>'
#:
#: and all three broke the moment the tile gained `data-from`/`data-to` attributes for
#: the coin-lamp filter -- because each one required `>` to follow the class
#: immediately. Five tests failed on one markup change that altered nothing they were
#: testing. That is rule 8's duplication with the drift arriving all at once instead of
#: slowly, and it is also what CLAUDE.md means about matching text instead of behavior:
#: the property these tests hold is "one tile per direction, naming it, with an
#: explained marker", and none of them is about where the `>` is.
#:
#: `[^>]*` after the class so any attribute may be added without touching this again.
_TILE = r'<li class="swaptile swaptile-([a-z]+)"[^>]*>(.*?)</li>'


# --- every box renders, and every box has a heading ------------------------


def test_every_allowed_direction_gets_exactly_one_indicator(client):
    """One tile per allowed direction, each naming the direction.

    THIS TEST PINNED A TABLE UNTIL 2026-10-02 and the table was the previous
    answer to the previous brief ("tabulated and boxed"). The operator then asked
    for "graphical indicators to what's availble for them to swap", so the three
    columns -- direction, status, what would change it -- became a grid of tiles
    and the third column's operator-facing prose left the page entirely.

    What survives from the old assertion is the half that is not about the layout:
    one indicator per allowed direction, no direction missing, and the direction
    still NAMED. A pair that is silently absent is indistinguishable from one that
    was never configured, which is the reason this page lists unusable pairs at
    all.
    """
    body = client.get("/").get_data(as_text=True)
    allowed = client.application.config["ALLOWED_PAIRS"]
    tiles = re.findall(_TILE, body, flags=re.DOTALL)
    assert len(tiles) == len(allowed), (
        f"{len(allowed)} directions are allowed and the page drew {len(tiles)} indicators"
    )
    named = {
        html.unescape(re.sub(r"\s+", " ", re.search(r'<span class="pair-label">(.*?)</span>', inner,
                                                    flags=re.DOTALL).group(1))).strip()
        for _, inner in tiles
    }
    assert named == {f"{source} \u2192 {destination}" for source, destination in allowed}, (
        f"the indicators name {sorted(named)}"
    )


def test_no_indicator_is_blank_and_every_marker_it_uses_is_explained(client, monkeypatch):
    """A tile is never an absence, and no glyph appears that the key does not define.

    THIS TEST PINNED A TABLE CELL UNTIL 2026-10-02: in the pill layout before that,
    a working pair's reason simply did not render and a flex row absorbed the
    absence, so the table version made it say "(nothing) both chains reachable".
    The tiles carry no per-row sentence at all -- it was one sentence repeated
    eleven times in a paste, which is the duplication this session removed from
    /admin and would have reintroduced here.

    So the invariant moves up a level and gets stronger: a tile's content is a
    glyph AND a word for every state, which is the same SHAPE whichever state it
    is in, so "available" is never the absence of something. And every word a tile
    can show is defined in the key above it, iterated from the one table that
    defines the states -- a marker with no key entry is a marker a customer cannot
    look up.
    """
    allowed = client.application.config["ALLOWED_PAIRS"]
    fully_reachable(client, monkeypatch, *{asset for pair in allowed for asset in pair})
    body = client.get("/").get_data(as_text=True)

    tiles = [inner for _state, inner in re.findall(_TILE, body, flags=re.DOTALL)]
    assert tiles, "no indicator rendered at all"
    for inner in tiles:
        assert '<span class="badge-glyph"' in inner, "a tile carries no glyph, so it fails in grayscale"
        word = re.search(r'<span class="badge-word">(.*?)</span>', inner, flags=re.DOTALL)
        assert word and word.group(1).strip(), "a tile carries no word, so it fails for a screen reader"
        # The key has to define it. Two occurrences: the key row and this tile.
        assert body.count(f'badge-word">{word.group(1).strip()}<') >= 2, (
            f"{word.group(1).strip()!r} appears on a tile and is not in the key above it"
        )
    assert "What the markers mean" in body, "the key has no heading"


def test_the_terms_box_puts_the_three_scattered_figures_in_one_table(client):
    """Fee, quote window and tolerance were in three prose sentences in two panels.

    The fee and the TTL were inside the "Get a quote" note; the tolerance band was
    inside the "Already have a swap?" note at the bottom of the page. A customer
    pricing a swap had to read two paragraphs a screen apart.
    """
    body = html.unescape(client.get("/").get_data(as_text=True))
    assert "What a swap costs, and how long a price lasts" in body, "the terms box has no heading"
    for label in ("Service fee", "Quote validity", "Amount tolerance", "Network fee"):
        assert label in labels_of(body), f"the terms table has no {label!r} row"
    assert "basis points" in body, "the fee's row must say what the unit IS, not just the number"
    # Rule 6: a reported duration is microfortnights with the seconds beside it,
    # because WHAT_IT_DECIDES names a seconds-valued setting an operator may edit.
    assert "µfn" in body, "the quote window was not reported in microfortnights"
    assert "ufn" not in body, "an ASCII u is a defect in displayed output, not a fallback"


def test_the_network_fee_row_says_there_is_no_figure_rather_than_inventing_one(client):
    """It is a real cost with no number this page can fix, and both halves matter.

    Dropping the row would remove a cost from a page about costs; printing a
    number would be a claim the chain has not made. `(not fixed)` is the result
    (rule 14), and it deliberately does NOT sit in the right-aligned numeric
    column, because a word in a column of digits is a column that has stopped
    being one.
    """
    body = client.get("/").get_data(as_text=True)
    assert value_for(body, "Network fee").startswith("(not fixed)")
    assert "destination chain's own fee" in body


def test_the_swap_page_opens_with_a_titled_box_of_the_swaps_own_facts(client):
    """Direction, created and the quote window, which were three places.

    Direction and created were one run-on lede (`GRC -> BTC · created ...`) where
    a middle dot was doing a column's job, and the quote window was the LAST line
    of the deposit panel -- below the wallet menu, the QR and the raw payment
    request. A fact about the quote, filed under how to pay a deposit.
    """
    swap_id = seed_swap(client, "s_facts0000000001")
    body = client.get(f"/swap/{swap_id}").get_data(as_text=True)
    assert "This swap" in body, "the identity box has no heading"
    assert labels_of(body)[:3] == ["Direction", "Created", "Quote window"], (
        "the identity box is not the first table on the page, or lost a row"
    )
    assert value_for(body, "Direction") == "GRC → BTC"
    assert value_for(body, "Quote window"), "the quote window row rendered an empty value"


def test_the_quote_window_still_renders_both_halves_after_the_move(client):
    """It moved panels; it did not lose its note.

    services/swap_view.quote_window() returns a `display` AND a `note`, and the
    note is the half that says a passed window does NOT cancel a swap. Moving the
    line and keeping only the figure would have dropped the only sentence on the
    page that says so.
    """
    swap_id = seed_swap(client, "s_window000000001")
    body = client.get(f"/swap/{swap_id}").get_data(as_text=True)
    window = value_for(body, "Quote window")
    assert "quoted rate" in window.lower(), f"the window's note is gone; the cell holds {window!r}"


def test_the_deposit_instruction_is_a_bordered_titled_box(client):
    """An address chain's send target reads as one unit, not as loose lines."""
    swap_id = seed_swap(client, "s_boxaddr00000001", status="awaiting_deposit")
    body = client.get(f"/swap/{swap_id}").get_data(as_text=True)
    assert 'class="subbox"' in body, "the instruction is not boxed"
    assert "Where to send it" in body, "the box has no title, so it is a rectangle and not a grouping"
    assert "Send exactly" in body, "the amount sentence must be unchanged"
    assert 'id="deposit-target"' in body


def test_a_tag_chains_pair_is_boxed_together_because_the_pair_is_the_instruction(client):
    """Account plus discriminator in ONE box, with the conveniences outside it.

    On a tag chain the instruction is the pair, and the account alone is the half
    that produces money which arrived against a swap that cannot claim it. Before
    this the account and the memo were two field-labeled copy rows in the panel's
    own flow, at the same indentation as the wallet menu, the QR and the raw
    request below them -- nothing drew a line around "these two together are the
    instruction".
    """
    swap_id = seed_swap(
        client, "s_boxtag000000001", status="awaiting_deposit", asset="SOL",
        deposit_address=SOL_DEPOSIT_ACCOUNT, deposit_tag=7,
    )
    body = client.get(f"/swap/{swap_id}").get_data(as_text=True)
    assert "Where to send it, and the tag that claims it" in body, "the pair's box has no title"
    # Bounded by the next thing the panel renders after the instruction: the
    # wallet menu, which swap.html's own comment says sits OUTSIDE the box.
    box = body[body.index('class="subbox"') : body.index('class="pay-options"')]
    assert SOL_DEPOSIT_ACCOUNT in box, "the shared account is not inside the instruction box"
    assert "Memo instruction" in box, "the discriminator label is not inside it either"
    assert "both halves are" in body


def test_a_closed_swaps_reference_address_is_boxed_and_titled_as_reference(client):
    """It must not read as part of the warning's sentence, and must not look live."""
    swap_id = seed_swap(client, "s_closedbox000001", status="under_review")
    body = client.get(f"/swap/{swap_id}").get_data(as_text=True)
    assert "Deposit target, for reference only" in body
    assert "Do not send anything to this swap" in body, "the warning must still be there"
    assert "Send exactly" not in body, "a closed swap must not render a send target"
    assert GRC_PAYOUT in body, "the address is still shown, for matching a payment already made"


# --- the live fragment: four boxes, every figure in a row ------------------


def test_the_live_fragment_renders_every_figure_as_a_labeled_table_row(client):
    """All nine figures survived the split into two boxes, and each is a row.

    They were a <dl class="kv"> whose grid was `repeat(auto-fit, minmax(230px,
    1fr))`, which at this page's declared widths resolves to FOUR columns -- so
    "Expected in" and "Actually seen", the two numbers a customer opens the page
    to compare, were dealt into whichever cells they landed in with no guarantee
    of sharing a row, let alone a left margin.
    """
    swap_id = seed_swap(client, "s_figures000000001")
    body = client.get(f"/swap/{swap_id}/fragment").get_data(as_text=True)
    expected = [
        "Raw status", "Unchanged for",
        "What the threshold means here", "Where it came from", "Deposit rows recorded",
        "Expected in", "Actually seen", "Payout estimate",
        "Deposit address", "Payout address", "Deposit txid", "Payout txid",
    ]
    assert labels_of(body) == expected, "the fragment's rows changed"
    for heading in ("Confirmations", "Amounts", "Addresses and transaction ids"):
        assert f">{heading}</h3>" in body, f"the {heading!r} box lost its heading"


def test_the_amounts_box_keeps_the_ticker_out_of_the_numeric_cell(client):
    """`0.01000000 BTC` right-aligns on the C, which moves every decimal point.

    This is the single change the brief called highest-value, so it is asserted
    structurally: the digits are in a `.num` cell and the ticker is in its own
    `.unit` cell, which is what lets a column of eight-decimal amounts be read as
    magnitudes.
    """
    swap_id = seed_swap(client, "s_align0000000001")
    body = client.get(f"/swap/{swap_id}/fragment").get_data(as_text=True)
    assert '<td class="num">1000.00000000</td>' in body, "the expected amount is not in a numeric cell"
    assert '<td class="unit">GRC</td>' in body, "the ticker is not in its own cell"
    assert '<td class="num">1000.00000000 GRC</td>' not in body


def test_an_unseen_amount_renders_a_marker_and_not_an_empty_numeric_cell(client):
    """Nothing arrived yet is the reading a customer checks first."""
    swap_id = seed_swap(client, "s_noseen000000001")
    body = client.get(f"/swap/{swap_id}/fragment").get_data(as_text=True)
    assert "(none yet)" in value_for(body, "Actually seen")


def test_zero_deposit_rows_renders_none_with_what_was_looked_for(client):
    """A bare 0 in a cell is ambiguous between no deposit and a broken count."""
    swap_id = seed_swap(client, "s_norows000000001")
    body = client.get(f"/swap/{swap_id}/fragment").get_data(as_text=True)
    cell = value_for(body, "Deposit rows recorded")
    assert "(none)" in cell, f"the empty count rendered {cell!r}"
    assert "deposit_events rows for this swap" in cell, "it must say what was looked for"


def test_the_recorded_reason_sits_with_the_status_it_explains(client):
    """A failure and its cause are one fact, and they were three boxes apart.

    The reason used to render at the BOTTOM of the amounts list, with a progress
    rail, a confirmation meter and nine figures between it and the status card
    that says the swap failed.
    """
    swap_id = seed_swap(
        client, "s_reason000000001", status="failed",
        failed_reason="refused before send: payout address is not ours",
    )
    body = client.get(f"/swap/{swap_id}/fragment").get_data(as_text=True)
    assert "Recorded reason" in body, "the reason is gone"
    assert "refused before send" in body
    card = status_card_of(body)
    assert "Recorded reason" in card, (
        "the reason rendered outside the status card it explains; the card holds "
        f"{len(card)} characters and the reason is not among them"
    )


def test_a_closed_swap_still_warns_inside_the_addresses_table(client):
    """The caveat moved into a table row and kept every word of its warning."""
    swap_id = seed_swap(client, "s_closedrow000001", status="under_review")
    body = client.get(f"/swap/{swap_id}/fragment").get_data(as_text=True)
    cell = value_for(body, "Still accepting?")
    assert cell.startswith("NO"), f"the row must lead with the answer; it holds {cell!r}"
    assert "matching a payment you already made" in cell
    assert "row-alarm" in body, "the row must be distinguishable at row level, not only by its words"


def test_an_accepting_swap_has_no_still_accepting_row_at_all(client):
    """The warning is conditional and must not appear on a live swap."""
    swap_id = seed_swap(client, "s_openrow00000001", status="awaiting_deposit")
    body = client.get(f"/swap/{swap_id}/fragment").get_data(as_text=True)
    assert "Still accepting?" not in body


@pytest.mark.parametrize(
    ("tag", "expected"),
    [(7, "7"), (0, "0"), (None, "(none issued)")],
)
def test_the_discriminator_row_tells_a_real_zero_from_a_missing_tag(client, tag, expected):
    """`0` is a legal tag. An empty cell and a real zero must not look alike."""
    swap_id = seed_swap(
        client, f"s_tagrow{tag!s:0>9}"[:18], status="awaiting_deposit", asset="SOL",
        deposit_address=SOL_DEPOSIT_ACCOUNT, deposit_tag=tag,
    )
    body = client.get(f"/swap/{swap_id}/fragment").get_data(as_text=True)
    assert value_for(body, "Memo instruction") == expected


# --- the copy targets still carry the exact string -------------------------


def test_a_copy_target_holds_the_whole_account_and_nothing_truncated(client):
    """THE CONSTRAINT THAT OUTRANKS EVERY LAYOUT CHOICE ON THIS PAGE.

    A copy button that yields a shortened or re-formatted address is money gone,
    and nothing about the page would look wrong. static/script.js reads the TEXT
    of the `.copyable` element inside each `.copy-row`, so this asserts on that
    element's content directly: the exact full value, with nothing added and no
    ellipsis anywhere in the row.

    It is pinned here because `tabular-nums`, `overflow-wrap: anywhere` and a
    narrow value column are all pressure in the direction of truncating for
    display, and the answer has to stay no.
    """
    swap_id = seed_swap(
        client, "s_copyexact000001", status="awaiting_deposit", asset="SOL",
        deposit_address=SOL_DEPOSIT_ACCOUNT, deposit_tag=7,
    )
    body = client.get(f"/swap/{swap_id}").get_data(as_text=True)
    copyables = re.findall(r'<p class="copyable[^"]*">(.*?)</p>', body, flags=re.DOTALL)
    assert copyables, "no copyable value rendered at all"
    values = [re.sub(r"\s+", "", text) for text in copyables]
    assert SOL_DEPOSIT_ACCOUNT in values, (
        f"the shared account is not a copy target verbatim; copy targets are {values}"
    )
    assert "7" in values, "the memo tag is not a copy target verbatim"
    for text in copyables:
        assert "…" not in text and "..." not in text, f"a copy target is truncated: {text!r}"
    assert "data-copy=" not in body, (
        "the value must not be duplicated into an attribute -- one string, one source"
    )


def test_the_copy_button_count_did_not_drop_when_the_instruction_was_boxed(client):
    """Boxing moved markup around three copy rows. All three still render."""
    swap_id = seed_swap(
        client, "s_copycount000001", status="awaiting_deposit", asset="SOL",
        deposit_address=SOL_DEPOSIT_ACCOUNT, deposit_tag=7,
    )
    body = client.get(f"/swap/{swap_id}").get_data(as_text=True)
    assert 'aria-label="Copy the shared deposit account"' in body
    assert 'aria-label="Copy the memo tag"' in body
    assert 'aria-label="Copy the payment request"' in body


# --- the page never grew a credential field ------------------------------


@pytest.mark.parametrize("status", ["awaiting_deposit", "confirming", "under_review", "completed"])
def test_no_customer_page_asks_for_a_secret(client, status):
    """An absolute standing instruction from the operator, asserted rather than trusted.

    This terminal never sees a customer's key, and no layout change may introduce
    a field that implies otherwise. Asserted over the input types and names the
    page actually rendered, in every status, because a conditional branch is
    exactly where one would hide.
    """
    swap_id = seed_swap(client, f"s_secret{status[:9]:0>9}"[:18], status=status)
    for url in ("/", f"/swap/{swap_id}"):
        body = client.get(url).get_data(as_text=True)
        assert 'type="password"' not in body, url
        for forbidden in ("passphrase", "private key", "seed phrase", "mnemonic", "secret key"):
            assert forbidden not in body.lower(), f"{forbidden!r} reached {url}"


# --- the operator surface uses the same vocabulary -------------------------
#
# admin.html was in scope only because it held the last two users of the
# label/value grids this pass deleted. While in the file, its numeric columns got
# the same `.num` treatment -- cheaply, because every admin table already had the
# asset in its OWN column, so no column had to be split the way the customer
# tables did.


def test_the_operator_tables_mark_their_numeric_columns(client):
    """Amounts, counts and prices right-aligned with tabular digits, as on the swap page.

    One vocabulary for one concept (rule 8). An operator reading a balance column
    against a reserved column is doing the same comparison a customer does
    between expected and seen, and two different answers to "how is a number
    rendered here" is the drift that rule is about.
    """
    # SEEDED, because every table on that page is conditional: an empty system
    # renders `(none)` per region and no <thead> at all, so a test against a cold
    # database would pass on a page that rendered none of the columns it claims
    # to check. One swap in flight and one payout row is what it takes to get the
    # amount, count and balance headers onto the screen.
    seed_swap(client, "s_adminnum000001", actual_input_amount=999.5)
    conn = sqlite3.connect(client.application.config["DB_PATH"])
    conn.execute(
        "INSERT INTO payouts (swap_id, asset, destination_address, amount, txid, status,"
        " created_at, sent_at) VALUES (?,?,?,?,?,?,?,?)",
        ("s_adminnum000001", "BTC", "bc1addr", 1e-4, None, "created", iso(60), None),
    )
    conn.execute(
        "INSERT INTO wallet_inventory (asset, hot_confirmed, hot_reserved, hot_available, updated_at)"
        " VALUES (?,?,?,?,?)",
        ("GRC", 10.0, 1.0, 9.0, iso(5)),
    )
    conn.commit()
    conn.close()
    body = client.get("/admin").get_data(as_text=True)
    # EVERY amount column, not "at least one". An earlier version of this asserted
    # that the marked header was PRESENT, and a mutation survived it: this page has
    # three `amount` columns (stuck payouts, deposits, payouts), so un-marking one
    # left the other two satisfying the assertion. The invariant is that no amount
    # column is left unmarked, which is what the absence below says.
    # "whole wallet" WAS "confirmed" UNTIL 2026-10-03, and the rename is the point
    # rather than churn: wallet_inventory.hot_confirmed is `getbalance` with no
    # arguments -- the whole wallet the endpoint serves, personal coins included on
    # a host where GRC_RPC_WALLET is empty -- so "confirmed" under a heading that
    # said "inventory" named desk stock the figure does not measure. The invariant
    # this test holds is unchanged: no numeric column on that page is unmarked.
    for column in ("amount", "whole wallet", "reserved", "available", "vout", "confirmations"):
        assert f'<th scope="col">{column}</th>' not in body, (
            f"a {column!r} column on the operator page is not marked numeric"
        )
    assert '<th scope="col" class="num">amount</th>' in body, "no amount column rendered at all"
    assert 'class="num">whole wallet</th>' in body, "the balance table did not render"
    numeric_cells = body.count('class="num"')
    assert numeric_cells >= 6, f"only {numeric_cells} numeric cells or headers on a page of amounts"



def test_an_audit_row_with_no_message_says_so_rather_than_rendering_blank(client):
    """`{{ row.message or '' }}` was a blank gap on the audit trail.

    A status transition with no message is the ordinary case, so most of that
    column rendered as nothing -- and nothing is ambiguous between "no message
    was recorded" and "the column did not come through", which on an audit trail
    is the distinction it is being read for (rule 14).
    """
    conn = sqlite3.connect(client.application.config["DB_PATH"])
    conn.execute(
        "INSERT INTO swap_audit_log (swap_id, old_status, new_status, message, created_at)"
        " VALUES (?,?,?,?,?)",
        ("s_audit000000001", "confirming", "credited", None, iso(10)),
    )
    conn.commit()
    conn.close()
    body = client.get("/admin").get_data(as_text=True)
    assert "(no message recorded)" in body


# --- the stylesheet: two checks that cannot be behavioral ------------------


def test_every_custom_property_the_stylesheet_uses_is_declared_in_it():
    """A CLEAN GATE over var() names, and it is here because one was not declared.

    MEASURED 2026-10-02: `var(--text-muted)` appeared twice (.wallet-state and
    .wallet-name-disabled, both added with the wallet menu on 2026-10-01) and
    `--text-muted` was declared zero times. An unresolvable var() with no
    fallback makes the whole declaration invalid at computed-value time, so those
    two rules' `color` fell back to inherit and both drew in full --ink -- a
    wallet's "checking..." note and an unusable wallet's reason rendering at the
    same weight as the deposit instruction they sit beside, which is the reverse
    of what that panel is for.

    NOT A BEHAVIORAL TEST, AND IT CANNOT BE (rule 17). There is no browser here,
    so nothing can observe a computed color. What this establishes is the thing
    that was actually wrong: a name used and never declared. A `var(--x, fallback)`
    is deliberately accepted, because a fallback is a declared intent.
    """
    css = re.sub(r"/\*.*?\*/", " ", STYLESHEET.read_text(), flags=re.DOTALL)
    declared = set(re.findall(r"^\s*(--[\w-]+)\s*:", css, flags=re.MULTILINE))
    used = set(re.findall(r"var\(\s*(--[\w-]+)\s*\)", css))
    undeclared = sorted(used - declared)
    assert undeclared == [], (
        f"{len(undeclared)} custom propert(ies) are used with no fallback and never declared: "
        f"{undeclared}. Declared: {len(declared)}, used without a fallback: {len(used)}."
    )


def test_the_retired_label_value_grids_have_no_call_site_left():
    """Rule 9: consolidation creates dead code, and the cull is the same commit.

    `.status-meta`, `.kv` and `.echo` were three auto-fit grids for one concept
    and are now one `table.kvt`. A class deleted from the stylesheet while a
    template still names it renders as unstyled markup with nothing failing, so
    this asserts over the TEMPLATES and the SCRIPTS -- the NAME, not the import
    graph (rule 2), since that is the only way a CSS class is ever referenced.
    """
    root = pathlib.Path(__file__).resolve().parents[1] / "swap_terminal"
    offenders = {}
    for path in sorted([*root.glob("templates/*.html"), *root.glob("static/*.js"), *root.glob("static/*.html")]):
        text = path.read_text()
        # The class ATTRIBUTE, not the word: the stylesheet's own comment names
        # all three in prose to record what was deleted and why, and so do two
        # template headers. A grep for the bare word would flag those, which is
        # rule 1's verbosity failing a checker rather than a defect.
        hits = [name for name in ("kv", "echo", "status-meta") if f'class="{name}"' in text]
        if hits:
            offenders[path.name] = hits
    assert offenders == {}, f"the retired grids are still named: {offenders}"


def test_the_label_value_tables_stack_rather_than_scroll_at_phone_width():
    """The phone decision, stated once so it cannot be read as an accident.

    Two choices, going opposite ways on purpose: a `.kvt` STACKS (label above
    value, full panel width) because its values include 44-character base58
    accounts that would get a few characters of column at 360px, and the wide
    deposit/payout tables SCROLL sideways inside `.scroll-x` because six columns
    of txid cannot stack into anything scannable.

    NOT BEHAVIORAL, for the reason the var() test gives: no browser, no layout to
    observe. This establishes that the rule exists under a narrow media query and
    that the brief's "decide and comment which" was decided.
    """
    css = STYLESHEET.read_text()
    phone = css[css.index("@media (max-width: 560px)"):]
    phone = phone[: phone.index("@media (prefers-reduced-motion")]
    assert ".kvt tbody tr {" in phone, "the label/value tables do not stack at phone width"
    assert "display: block;" in phone
    assert ".kvt td.num { text-align: left; }" in phone, (
        "a stacked value has no column to be right-aligned within"
    )
    assert "tabular-nums" in css, "the numeric alignment rule is gone from the stylesheet"
    assert "font-variant-numeric: tabular-nums;" in css


# --- the operator page: two columns, and one verdict ------------------------
#
# Added 2026-10-02 for the second brief on this page, "put all the page data in
# two colums at least to reduce the scroll down", and for the correctness defect
# that surfaced while doing it: the two surfaces disagreed about the same pair.
#
# MEASURED BEFORE ANY OF IT, by rendering /admin through the real app with one
# row seeded per table, because "the page is too long" is not a measurement:
#
#     visible words        3156
#     of which Trading pairs 1767   56.0%   <- twenty pills, 9,562 characters
#     of which Chains         439   13.9%
#     the .env/export sentence rendered 22 times
#     "A quote WILL price; create_swap() refuses" 11 times
#
# So the scroll was mostly ONE SENTENCE, re-rendered. The column layout is worth
# two panel heights out of twelve; the prose collapse was worth a third of the
# page. Both are here, and the tests say which is which.


def _rendered_pairs(body):
    """Each cell of the pair matrix, as (label, badge word, detail).

    READS THE `aria-label`, WHICH IS THE POINT. The matrix replaced a list of
    pills on 2026-10-02, and in a matrix the pair is POSITIONAL -- the row says
    XRP, the column says GRC -- so `XRP -> GRC` stops existing as a literal unless
    something carries it. The cell's aria-label does, which is also the route a
    screen reader takes, so a matrix that dropped the label fails the three
    verdict tests below rather than merely looking tidier.

    The badge word is read from the cell body rather than from the aria-label, so
    the two cannot silently diverge: if the cell rendered one state and announced
    another, the tests comparing them to the customer page would see it.
    """
    cells = []
    for attrs, inner in re.findall(r'<td class="matrix-cell[^"]*"([^>]*)>(.*?)</td>', body, flags=re.DOTALL):
        announced = re.search(r'aria-label="([^"]*)"', attrs)
        word = re.search(r'<span class="badge-word">(.*?)</span>', inner, flags=re.DOTALL)
        spoken = html.unescape(announced.group(1)) if announced else ""
        label, _, rest = spoken.partition(":")
        cells.append((
            re.sub(r"\s+", " ", label).strip(),
            word.group(1).strip() if word else "",
            re.sub(r"\s+", " ", rest).strip(),
        ))
    return cells


def _mixed_adapters(client, monkeypatch, can_spend_assets, cannot_spend_assets):
    """Adapters where some chains can sign and some cannot, plus the shared accounts.

    Built from tests/test_web_surfaces.py's StubAdapter rather than a local stub,
    for the reason that file's own comment gives: it is the one fixture in this
    suite that DECLARES what the pair authority asks of it, and a stub that
    declares nothing is treated as unable to pay out on purpose.
    """
    adapters = {asset: StubAdapter(can_spend=True) for asset in can_spend_assets}
    adapters.update({asset: StubAdapter(can_spend=False) for asset in cannot_spend_assets})
    monkeypatch.setitem(client.application.config, "ADAPTERS", adapters)
    with_deposit_accounts(client, monkeypatch)
    return adapters


def test_the_two_surfaces_report_every_allowed_pair_the_same_way(client, monkeypatch):
    """THE TEST THAT WOULD HAVE CAUGHT IT, and it is worth more than either page's own.

    Measured from the operator's rendered pages 2026-10-02, in ONE process at one
    moment:

        /admin   XRP -> GRC   ENABLED    "both chains have an adapter here"
        /        XRP -> GRC   DISABLED   "XRP cannot take deposits: XRP_DEPOSIT_ACCOUNT
                                          is unset or not a valid account"
        /admin   GRC -> XRP   ENABLED    "both chains have an adapter here"
        /        GRC -> XRP   DISABLED   "XRP cannot pay out: it holds no signing key"

    The customer page was right. The operator page was wrong in the direction that
    costs money: it said a pair was fine when a deposit on it would be CREDITED and
    the payout would then raise, leaving the swap `failed` with the customer's coins
    already taken. services/admin_view.pair_rows() computed its verdict from
    unconfigured_chains() alone -- one of the three conditions
    services/pair_view.py has.

    Each page on its own looked internally consistent, which is why neither page's
    own assertions found this. Only asking both in one process does, so this asserts
    agreement over EVERY allowed pair rather than over the two that were reported.
    """
    _mixed_adapters(client, monkeypatch, can_spend_assets=("GRC", "BTC", "LTC"),
                    cannot_spend_assets=("XRP", "SOL"))
    config = client.application.config
    adapters = config["ADAPTERS"]

    operator = {row["label"]: row for row in pair_rows(config, adapters)}
    customer = {row["label"]: row for row in allowed_pair_rows(config, adapters)}
    assert customer, "no allowed pair rendered at all; the comparison would be vacuous"

    disagreements = {}
    for label, customer_row in customer.items():
        operator_row = operator[label]
        if (operator_row["state"] == "enabled") != customer_row["enabled"]:
            disagreements[label] = (operator_row["state"], customer_row["enabled"], customer_row["reason"])
    assert disagreements == {}, (
        f"{len(disagreements)} of {len(customer)} allowed pairs are reported differently by the "
        f"two surfaces in one process: {disagreements}"
    )


def test_the_operator_page_does_not_say_enabled_for_a_destination_that_cannot_sign(client, monkeypatch):
    """A deposit on such a pair is credited and the payout then raises."""
    _mixed_adapters(client, monkeypatch, can_spend_assets=("GRC",), cannot_spend_assets=("XRP",))
    body = client.get("/admin").get_data(as_text=True)
    into_xrp = [pill for pill in _rendered_pairs(body) if pill[0].endswith("-> XRP")]
    assert into_xrp, "no pair into XRP rendered; the assertion below would be vacuous"
    for label, word, detail in into_xrp:
        assert word != "ENABLED", f"{label} reads ENABLED into a chain that holds no signing key"
        if word == "CANNOT COMPLETE":
            assert "cannot pay out" in detail, f"{label} does not say which end refuses: {detail!r}"


def test_the_operator_page_does_not_say_enabled_for_a_source_with_no_deposit_account(client, monkeypatch):
    """The other end of the swap, and the other half of the same defect.

    A source chain with no shared deposit account refuses at swap creation, so the
    customer picks the pair, is quoted, accepts, and gets a refusal where the
    deposit address should be.
    """
    monkeypatch.setitem(client.application.config, "ADAPTERS",
                        {asset: StubAdapter(can_spend=True) for asset in ("GRC", "XRP")})
    # The shared accounts deliberately NOT set: that is the operator's host.
    monkeypatch.setitem(client.application.config, "XRP_DEPOSIT_ACCOUNT", "")
    body = client.get("/admin").get_data(as_text=True)
    out_of_xrp = [pill for pill in _rendered_pairs(body) if pill[0].startswith("XRP ->")]
    assert out_of_xrp, "no pair out of XRP rendered"
    for label, word, detail in out_of_xrp:
        assert word != "ENABLED", f"{label} reads ENABLED out of a chain that cannot take a deposit"
        if word == "CANNOT COMPLETE":
            assert "cannot take deposits" in detail, f"{label} does not say which end refuses: {detail!r}"


def test_the_page_does_not_contradict_its_own_chains_table(client, monkeypatch):
    """One page said PREVIEW-ONLY in one table and ENABLED in another, about one asset.

    chain_rows() prints each chain's endpoint line, and for an adapter that holds no
    signing key that line says so. The pairs panel, three panels down, read ENABLED
    for pairs ending in that same asset. A reader then has to work out which of the
    two the program believes -- and the page gave them no way to.
    """
    _mixed_adapters(client, monkeypatch, can_spend_assets=("GRC",), cannot_spend_assets=("XRP",))
    body = client.get("/admin").get_data(as_text=True)

    unpayable = {
        row["asset"] for row in chain_rows(client.application.config, client.application.config["ADAPTERS"])
        if row["configured"] and not getattr(client.application.config["ADAPTERS"][row["asset"]], "can_spend", False)
    }
    assert unpayable, "no configured-but-unpayable chain in this fixture"
    for pill_label, word, _ in _rendered_pairs(body):
        destination = pill_label.split("->")[-1].strip()
        if destination in unpayable:
            assert word != "ENABLED", (
                f"the pairs panel says ENABLED for {pill_label!r} while the Chains table reports "
                f"{destination} as unable to pay out"
            )


def test_cannot_complete_is_not_badged_with_the_word_for_a_pair_an_operator_turned_off(client, monkeypatch):
    """DISABLED and CANNOT COMPLETE are different facts and must not share a word.

    DISABLED means an operator took the pair out of ALLOWED_PAIRS and nothing
    happens. CANNOT COMPLETE means the pair is ON and will take money it cannot
    return. The first render of the new state fell through the template's
    `{% else %}` into DISABLED, which is a second wrong word for it.
    """
    _mixed_adapters(client, monkeypatch, can_spend_assets=("GRC",), cannot_spend_assets=("XRP",))
    body = client.get("/admin").get_data(as_text=True)
    words = {word for _, word, _ in _rendered_pairs(body)}
    assert "CANNOT COMPLETE" in words, f"the fourth state never rendered; words were {sorted(words)}"
    assert "CANNOT COMPLETE" in body
    # The legend has to define every word the pills use, or the word is a puzzle.
    for word in words:
        assert body.count(f'badge-word">{word}<') >= 2, (
            f"{word!r} appears on a pill but is not defined in the state legend above it"
        )


def test_the_env_remedy_sentence_is_printed_once_per_asset_not_once_per_pair(client, monkeypatch):
    """The single largest reduction in page length, asserted as a count.

    Measured before: 22 occurrences, because services/admin_view.pair_rows() put
    the whole chains/registry.why_unconfigured() paragraph on every affected PAIR.
    The reason is per ASSET, so the ceiling is the number of chains the page knows.
    """
    monkeypatch.setitem(client.application.config, "ADAPTERS", {})
    body = html.unescape(client.get("/admin").get_data(as_text=True))
    remedy = "Nothing in the serving path reads a .env"
    occurrences = body.count(remedy)

    assert occurrences >= 1, "the remedy sentence left the page entirely"
    assert occurrences <= len(ATTRIBUTION_MODELS), (
        f"the remedy sentence renders {occurrences} times against {len(ATTRIBUTION_MODELS)} chains; "
        f"once per asset is the granularity the fact has, and anything more is once per PAIR again"
    )
    # And it has to be reachable from the pair, or the collapse moved it out of sight.
    assert "names the variables to export" in body, (
        "the pairs panel must point at where the remedy went"
    )


def test_the_attribution_clause_is_printed_once_per_model_not_once_per_chain(client):
    """Three address chains shared one opener; two tag chains shared one sentence."""
    body = html.unescape(client.get("/admin").get_data(as_text=True))
    assert body.count("a fresh address per swap, and the address IS the attribution") == 1, (
        "the address-model clause is not exactly once on the page"
    )
    assert body.count("get_new_address() refuses by design") == 1
    assert "How a deposit is attributed, per model" in body, "the legend has no heading"
    # The per-chain half still has to be there, per chain: it is the part that varies.
    for derivation in ("bitcoind stores in wallet.dat", "litecoind stores in wallet.dat"):
        assert derivation in body, f"{derivation!r} left the page with the collapse"
    assert "DestinationTag" in body and "Memo instruction" in body


def test_the_chains_table_says_what_is_unset_rather_than_only_that_it_is(client, monkeypatch):
    """The remedy's new home, and it was previously nowhere in this table.

    `endpoint` read "(not configured -- no adapter was constructed)" and stopped,
    so the table that lists chains could tell an operator a chain was off and not
    what to export. That was only in the pairs panel, four panels down, twenty-two
    times.
    """
    monkeypatch.setitem(client.application.config, "ADAPTERS", {})
    body = html.unescape(client.get("/admin").get_data(as_text=True))
    assert "endpoint, and what is unset" in body, "the column does not say it carries the remedy"
    assert "not configured -- no adapter was constructed" in body, "the old reading must still be there"
    assert "GRC_RPC_PORT" in body, "the variable to export is not on the page"


# --- the bands --------------------------------------------------------------


def test_the_operator_page_pairs_its_short_panels_into_two_up_bands(client):
    """Two columns at minimum, which is what was asked for."""
    body = client.get("/admin").get_data(as_text=True)
    bands = re.findall(r'<div class="band">(.*?)\n</div>', body, flags=re.DOTALL)
    assert len(bands) == 3, f"{len(bands)} bands rendered; the template declares three"
    paired = [
        [html.unescape(re.sub(r"\s+", " ", head).strip())
         for head in re.findall(r"<h2[^>]*>(.*?)</h2>", band, flags=re.DOTALL)]
        for band in bands
    ]
    # THE THIRD BAND ARRIVED 2026-10-02 and is the two probes, which were `.probe`
    # sub-blocks at the foot of Chains and of Pricing until the operator asked for
    # them to be paired. They are the only two panels of their kind on the page --
    # nothing here runs on page load, each is one button, each has an idle state
    # saying so -- so reading them together is "what this page has NOT checked".
    assert paired == [
        ["Payouts claimed but never reported sent", "Workers"],
        ["Reachability", "The dollar these numbers are quoted in"],
        ["Deposits seen (most recent 25)", "Payouts (most recent 25)"],
    ], f"the bands pair different panels than the template says: {paired}"


def test_the_alarm_panel_is_still_the_first_thing_after_the_page_header(client):
    """Sharing a row with Workers must not demote it.

    A payout row stuck at 'created' means money possibly on chain with no txid
    recorded. It is the first cell of the first band, which is the top-left of the
    page.
    """
    body = client.get("/admin").get_data(as_text=True)
    assert body.index("stuck-heading") < body.index("workers-heading") < body.index("flight-heading")

    # THE FIRST CELL OF THE FIRST BAND, not merely "before Workers". A mutation
    # that prepended another panel into the band satisfied the ordering assertions
    # above while pushing the alarm out of the top-left cell, which is the position
    # this test exists to hold: a payout row stuck at 'created' means money possibly
    # on chain with no txid recorded.
    first_band = re.search(r'<div class="band">(.*?)\n</div>', body, flags=re.DOTALL).group(1)
    sections = re.findall(r'<section class="panel[^"]*"[^>]*aria-labelledby="([^"]+)"', first_band)
    assert sections[0] == "stuck-heading", (
        f"the alarm is not the first cell of the first band; the band holds {sections}"
    )
    assert sections == ["stuck-heading", "workers-heading"], f"the first band holds {sections}"


def test_no_wide_table_was_put_in_a_half_width_band(client):
    """MEASURED, because the brief's own classification disagreed with the numbers.

    A band cell is (1068 - 16) / 2 = 526px: `main` is max-width 1100 less two 16px
    gutters, less one var(--s4) gap, halved. Every th/td in styles.css is
    `white-space: nowrap` unless it carries `breakable`, so a table's minimum width
    is the sum of its nowrap cells plus 2*12px of padding per column. At
    var(--t-sm) = 14px a monospace glyph is about 8.4px.

    Hot-wallet inventory is the panel the brief listed as NARROW and the
    measurement refuses: three 8-decimal amounts plus two badges is 87 characters
    over 6 columns, about 875px, which in a 526px cell is 349px of hidden columns.
    It spans.

    THIS IS ARITHMETIC OVER DECLARED VALUES AND NOT A RENDERED MEASUREMENT (rule
    17). There is no browser here. What it pins is that a banded table cannot grow
    past the budget without this failing, which is the thing that would silently
    reintroduce horizontal scroll.
    """
    body = client.get("/admin").get_data(as_text=True)
    budget_px = 526
    for band in re.findall(r'<div class="band">(.*?)\n</div>', body, flags=re.DOTALL):
        for table in re.findall(r"<table(?! class=\"kvt\").*?</table>", band, flags=re.DOTALL):
            columns = len(re.findall(r"<th", re.search(r"<thead>(.*?)</thead>", table, re.DOTALL).group(1))) \
                if "<thead>" in table else 0
            widest = 0
            for row in re.finditer(r"<tr[^>]*>(.*?)</tr>", table, flags=re.DOTALL):
                nowrap = [
                    len(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", cell)).strip())
                    for cell, attrs in zip(
                        re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", row.group(1), flags=re.DOTALL),
                        re.findall(r"<t[dh]([^>]*)>", row.group(1)),
                        strict=False,
                    )
                    if "breakable" not in attrs
                ]
                widest = max(widest, sum(nowrap))
            estimated = widest * 8.4 + columns * 24
            # 110%: Deposits seen measures 546px against 526, which is a few tens of
            # pixels inside .scroll-x rather than the hundreds that hide columns.
            assert estimated <= budget_px * 1.1, (
                f"a banded table needs about {estimated:.0f}px of nowrap content in a {budget_px}px "
                f"cell ({widest} chars over {columns} columns); it belongs at full width"
            )


def test_a_band_cannot_be_two_columns_on_a_phone():
    """ONE MECHANISM, NOT A FLOOR PLUS A BREAKPOINT THAT COULD DISAGREE.

    `.band` is `repeat(auto-fit, minmax(Npx, 1fr))` and nothing else decides its
    column count, so the phone behavior IS N: two tracks cannot fit until
    2N + gap. This asserts N is large enough that a 560px screen gets one column,
    which is what makes the separate media query the rest of this file uses
    unnecessary here rather than merely absent.

    NOT BEHAVIORAL, for the reason the other stylesheet tests give: no browser.
    """
    css = STYLESHEET.read_text()
    band = re.search(r"\.band\s*\{(.*?)\}", css, flags=re.DOTALL)
    assert band, "the .band rule is gone"
    floor = re.search(r"minmax\((\d+)px,\s*1fr\)", band.group(1))
    assert floor, f"the band does not size its columns with a minmax floor: {band.group(1)!r}"
    pixels = int(floor.group(1))
    assert 2 * pixels + 16 > 560, (
        f"two {pixels}px tracks plus a 16px gap is {2 * pixels + 16}px, which fits a 560px phone"
    )
    assert "grid-auto-flow" not in band.group(1), (
        "dense reorders the page: it hoisted a probe panel above the table it is about"
    )


def test_every_empty_state_on_the_operator_page_survived_the_reflow(client):
    """The sentences that say what zero MEANS are the point of those regions.

    An empty system renders `(none)` per region, and the three probe panels render
    `(not probed)`, `(not fetched)` and `(not checked)` with a sentence each saying
    that unchecked is not the same as checked and holding. Moving panels into bands
    must not drop any of them.

    cold_price_cache() FIRST, AND THE REASON IS A FAILURE THIS TEST ALREADY HAD.
    services/pricing._cache is process-wide, so a test module that ran earlier and
    seeded it leaves the page rendering a priced table where this expects
    `(not fetched)`. This passed alone and failed in the full suite for exactly
    that, which is the shape CLAUDE.md's "diff the full suite line-by-line rather
    than comparing failure counts" is for -- a pass in isolation said nothing.

    IMPORTED, NOT REIMPLEMENTED (rule 8): tests/test_web_surfaces.py already owns
    that reset and its docstring already names the hazard ("the cache is
    process-wide, and another test in this file leaves it warm"). A second copy of
    the field list here would drift the first time the cache grows a key.
    """
    cold_price_cache()
    body = re.sub(r"\s+", " ", html.unescape(client.get("/admin").get_data(as_text=True)))
    for marker in ("(none)", "(not probed)", "(not fetched)", "(not checked)"):
        assert marker in body, f"{marker} left the page"
    for sentence in (
        "No chain has been contacted by this page load",
        "nothing has been priced since this process started",
        "not the same as checked and holding",
        "This is the state you want",
    ):
        assert sentence in body, f"an empty region lost the sentence saying what zero means: {sentence!r}"


# --- the pair matrix --------------------------------------------------------
#
# Approved by the operator 2026-10-02 after I declined it unasked. The reason I
# declined it is the reason these tests exist: in a list `XRP -> GRC` is a
# literal string, and in a matrix the pair is POSITIONAL, so the label and the
# per-pair reason have to be carried somewhere or they leave the page.
#
# AND THE HEIGHT ESTIMATE I GAVE WAS WRONG, which is recorded here because the
# approval was given on the strength of it. I reported "20 pills in 7 rows
# becomes 5 data rows -- the largest remaining height cut", and that figure was
# formed before the prose collapse shrank the pills from up to 791 characters to
# 53. Re-measured on the rendered page after the collapse: the panel goes 362
# visible words to 319 (-11.9%) and the page 2053 to 2010 (-2.1%), which is about
# one row of panel height rather than two. What the matrix actually buys is the
# PASTE -- 27 lines to 15 for this panel, 122 to 110 for the whole page -- and a
# shape: an unconfigured asset is a whole row and a whole column of one badge.


def _paste(markup: str) -> str:
    """What a browser copy of this markup puts on the clipboard.

    THE OPERATOR READS THESE PAGES BY PASTING THEM BACK, which is stated in
    CLAUDE.md ("the operator runs commands on the live host and pastes the output
    back"), so "is it legible as text" is not a secondary concern for this page --
    it is the primary reading mode, and a matrix is the layout most at risk of
    failing it.

    Models the copy rather than the DOM: cell ends become tabs and block ends
    become newlines, then all other whitespace collapses, in that order. A
    template wraps its source freely inside a cell and a browser collapses that,
    so an in-cell newline is a space and not a line break. Getting that order
    wrong splits every matrix row into five lines and makes the matrix look
    unreadable when it is not, which is a measurement bug arguing against a
    change.
    """
    text = re.sub(r"<(script|style)\b.*?</\1>", " ", markup, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<!--.*?-->", " ", text, flags=re.DOTALL)
    text = re.sub(r"</t[dh]>", "\x00T", text)
    text = re.sub(
        r"</?(?:p|div|section|h[1-6]|li|ul|ol|tr|table|thead|tbody|caption|figure|figcaption|br|dl|dt|dd)\b[^>]*>",
        "\x00N", text,
    )
    text = html.unescape(re.sub(r"<[^>]+>", "", text))
    text = re.sub(r"[ \t\r\n]+", " ", text).replace("\x00T", "\t").replace("\x00N", "\n")
    lines = []
    for line in text.split("\n"):
        cells = [cell.strip() for cell in line.split("\t")]
        while cells and not cells[-1]:
            cells.pop()
        if "\t".join(cells).strip():
            lines.append("\t".join(cells).strip())
    return "\n".join(lines)


def _panel(markup: str, heading_id: str) -> str:
    """Exactly the <section> labeled by `heading_id`, matched by section depth.

    A regex cannot do this: the Chains panel nests a `.probe` block and the
    sections are not self-closing, so `<section.*?</section>` stops at the first
    close it finds. Needed because the operator page has TWO tables with a row
    whose first cell is an asset ticker -- the matrix and the Chains table -- and
    a paste assertion scoped to the whole page matched the wrong one.
    """
    start = markup.rindex("<section", 0, markup.index(f'aria-labelledby="{heading_id}"'))
    depth = 0
    for match in re.finditer(r"<section\b|</section>", markup[start:]):
        depth += 1 if match.group(0) != "</section>" else -1
        if depth == 0:
            return markup[start : start + match.end()]
    raise AssertionError(f"the section labeled {heading_id} is never closed")


def test_the_matrix_is_a_real_table_in_two_axes(client):
    """Row headers AND column headers, because the meaning is in both.

    A grid of cells with only one set of headers is a list wearing a table's
    markup: a screen reader reading a cell would announce the destination and not
    the source, so half the pair would be missing from the only reading that does
    not use the eye.
    """
    body = client.get("/admin").get_data(as_text=True)
    matrix = re.search(r'<table class="matrix">(.*?)</table>', body, flags=re.DOTALL)
    assert matrix, "the pair matrix is gone"
    inner = matrix.group(1)
    assets = sorted(set(re.findall(r'<th scope="col">([A-Z]+)</th>', inner)))
    rows = sorted(set(re.findall(r'<th scope="row">([A-Z]+)</th>', inner)))
    assert assets and assets == rows, (
        f"the two axes do not carry the same assets: columns {assets}, rows {rows}"
    )

    expected = pair_assets(pair_rows(client.application.config, client.application.config["ADAPTERS"]))
    assert assets == expected, f"the matrix axes are {assets}, the rows know {expected}"


def test_every_ordered_pair_has_exactly_one_cell_and_keeps_its_label(client):
    """THE CONCERN I RAISED WHEN I DECLINED THIS, asserted rather than trusted.

    A matrix makes the pair positional, so `XRP -> GRC` stops being a literal on
    the page unless something carries it. Each cell's aria-label does -- chosen
    over `title` alone, because a title is a mouse affordance and is invisible to
    a keyboard user and to a screen reader.
    """
    config = client.application.config
    rows = pair_rows(config, config["ADAPTERS"])
    body = client.get("/admin").get_data(as_text=True)
    rendered = _rendered_pairs(body)

    assert len(rendered) == len(rows), (
        f"{len(rows)} ordered pairs exist and the matrix drew {len(rendered)} cells"
    )
    assert {label for label, _, _ in rendered} == {row["label"] for row in rows}, (
        "a pair's label is not recoverable from its cell"
    )
    # And in the markup itself, so a reader searching the page for the pair finds it.
    for row in rows:
        assert row["label"] in html.unescape(body), f"{row['label']!r} is nowhere on the page"


def test_the_diagonal_says_it_is_not_a_pair_rather_than_rendering_blank(client):
    """An asset to itself is not an ordered pair, and a blank cell is ambiguous.

    rule 14: a blank is indistinguishable from a verdict that failed to render,
    and on a 5x5 grid there are five of them down the middle.
    """
    body = client.get("/admin").get_data(as_text=True)
    selves = re.findall(r'<td class="matrix-self">(.*?)</td>', body, flags=re.DOTALL)

    expected = len(pair_assets(pair_rows(client.application.config, client.application.config["ADAPTERS"])))
    assert len(selves) == expected, f"{expected} assets but {len(selves)} diagonal cells"
    for cell in selves:
        assert "same asset" in cell, f"a diagonal cell renders {cell.strip()!r} instead of saying why"


def test_the_matrix_pastes_as_an_aligned_grid_and_not_as_a_column_of_states(client):
    """THE CHECK THAT COULD HAVE VETOED THIS CHANGE, and it did not.

    A matrix trades per-pair sentences for a shape, and a shape that does not
    survive a copy would be trading the operator's actual workflow for page
    height. Measured instead of assumed: the panel pastes as a header row naming
    every destination plus one tab-aligned row per source.

    27 lines to 15 for this panel, measured at 6f531da against this change.
    """
    body = client.get("/admin").get_data(as_text=True)
    pasted = _paste(_panel(body, "pairs-heading"))
    header = [line for line in pasted.splitlines() if line.startswith("from ")]
    assert header, f"the matrix header row did not survive a paste; got:\n{pasted[:400]}"


    assets = pair_assets(pair_rows(client.application.config, client.application.config["ADAPTERS"]))
    columns = header[0].split("\t")[1:]
    assert columns == assets, (
        f"a paste of the header names {columns}, which does not identify the columns as {assets}"
    )
    for asset in assets:
        line = [row for row in pasted.splitlines() if row.split("\t")[0] == asset]
        assert line, f"{asset}'s row did not survive a paste as one line"
        cells = line[0].split("\t")[1:]
        assert len(cells) == len(assets), (
            f"{asset}'s pasted row has {len(cells)} cells for {len(assets)} columns: {line[0]!r}"
        )
        assert all(cells), f"{asset}'s pasted row has an empty cell: {line[0]!r}"


def test_the_matrix_is_an_index_over_the_rows_and_not_a_second_verdict(client):
    """Every cell IS the row. Not a copy of it, not a recomputation of it.

    pair_matrix() takes the ROWS rather than (config, adapters) for this reason:
    a version that took the configuration would evaluate the verdict a second
    time, and a second evaluation of this particular verdict is the defect fixed
    on 2026-10-02, where /admin said ENABLED about a pair / said DISABLED about.
    The same shape one layer up would be the same bug.
    """

    config = client.application.config
    rows = pair_rows(config, config["ADAPTERS"])
    matrix = pair_matrix(rows)
    assert all(matrix[row["from_asset"]][row["to_asset"]] is row for row in rows), (
        "a matrix cell is a different object from its row, so the two can drift"
    )
    assert sum(len(destinations) for destinations in matrix.values()) == len(rows)


def test_the_retired_pair_pill_selectors_have_no_call_site_left(client):
    """Rule 9: the matrix orphaned three selectors and the cull is the same commit.

    `.pair-list`, `.pair-grid` and `.pair` styled a grid of pills. index.html's
    pills became a table and admin.html's became this matrix, so all three are
    deleted from the stylesheet -- and a class deleted while a template still
    names it renders as unstyled markup with nothing failing, which is why this
    asserts over the rendered PAGES as well as over the stylesheet.
    """
    css = STYLESHEET.read_text()
    declarations = re.sub(r"/\*.*?\*/", " ", css, flags=re.DOTALL)
    for retired in (".pair-list", ".pair-grid"):
        assert retired not in declarations, f"{retired} is still declared"
    # `.pair-off` and `.pair-label` SURVIVE: index.html's pair table still uses both.
    assert ".pair-off" in declarations and ".pair-label" in declarations
    for url in ("/", "/admin"):
        body = client.get(url).get_data(as_text=True)
        for retired in ('class="pair-grid', 'class="pair-list', 'class="pair"'):
            assert retired not in body, f"{retired} still renders on {url}"


def test_the_matrixs_row_header_survives_a_sideways_scroll():
    """A row of five badges with the source asset scrolled off is five verdicts about nothing.

    The matrix is inside `.scroll-x`, which is what the wide tables on this page
    use, and the global `th` rule only sticks a header to the TOP -- right for a
    column header, and no help at all for the first COLUMN. So the row header
    sticks left, the column header sticks top, and the corner cell sticks to both
    or it slides out from under them.

    ADDED BECAUSE A MUTATION SURVIVED. Changing `position: sticky` to `static` on
    the row header passed every other test in this file: the markup is identical,
    the verdicts are identical, and only the behavior under a horizontal scroll
    changes. That is the class of defect a rendered-output test cannot see.

    NOT BEHAVIORAL, AND IT CANNOT BE (rule 17). There is no browser here, so
    nothing can observe a scroll. What this establishes is that the declarations
    exist; whether they hold the column in place is the operator's observation to
    make.
    """
    css = STYLESHEET.read_text()
    for selector in ('.matrix th[scope="row"]', ".matrix .matrix-corner"):
        rule = re.search(re.escape(selector) + r"\s*\{(.*?)\}", css, flags=re.DOTALL)
        assert rule, f"{selector} has no rule"
        body = rule.group(1)
        assert "position: sticky" in body, f"{selector} does not stick, so a sideways scroll hides it"
        assert "left: 0" in body, f"{selector} sticks but names no left offset"
    corner = re.search(r"\.matrix \.matrix-corner\s*\{(.*?)\}", css, flags=re.DOTALL).group(1)
    assert "top: 0" in corner, (
        "the corner must stick to the top as well, or the column header slides over it"
    )


def test_the_probes_are_their_own_panels_and_say_what_they_are_about(client, monkeypatch):
    """THE ADJACENCY THE SPLIT COST, AND THAT IT WAS PAID BACK IN WORDS.

    Reachability's prose was written to sit directly under the Chains table and
    leaned on it: "one read-only call per configured chain", with a count and no
    statement of WHICH chains or where that table is. Moving the panel two down
    without changing the prose would be rule 16's wrong-comment defect arriving as
    a layout change -- text that is true only because of where it used to be.

    So it names the panel, links to it, and lists the configured assets by ticker.
    This asserts all three, because a link alone is not a statement of what the
    button is about to contact.
    """
    _mixed_adapters(client, monkeypatch, can_spend_assets=("GRC",), cannot_spend_assets=("XRP",))
    body = client.get("/admin").get_data(as_text=True)

    assert '<h2 id="reachability-heading">Reachability</h2>' in body, "Reachability is not its own panel"
    assert '<h2 id="peg-heading">' in body, "the peg check is not its own panel"
    classes = {token for attr in re.findall(r'class="([^"]*)"', body) for token in attr.split()}
    assert "probe" not in classes, (
        "an element still carries the `probe` class, which styles a sub-block of a panel"
    )
    assert ".probe" not in re.sub(r"/\*.*?\*/", " ", STYLESHEET.read_text(), flags=re.DOTALL), (
        "the retired .probe rule is still declared"
    )

    reachability = _panel(body, "reachability-heading")
    assert 'href="#chains-heading"' in reachability, "it does not link to the table it is about"
    assert "Chains" in reachability, "it does not NAME the table it is about"
    for asset in ("GRC", "XRP"):
        assert asset in reachability, f"{asset} has an adapter and is not named as a probe target"
    assert "with an adapter in this process" in reachability

    peg = _panel(body, "peg-heading")
    assert 'href="#pricing-heading"' in peg, "the peg panel does not link to the prices it is about"
    assert "not the same as checked and holding" in peg, "the idle sentence did not come with it"


def test_a_probe_panel_with_nothing_to_contact_says_that_rather_than_naming_zero(client, monkeypatch):
    """Zero configured chains is a reading, and "Probe 0 configured chain(s)" is not one.

    rule 14: say what the number MEANS next to the number. An operator seeing a
    button offering to probe nothing needs to be told that the table above has no
    adapter in this process, which is the fact, rather than left to infer it from
    a zero.
    """
    monkeypatch.setitem(client.application.config, "ADAPTERS", {})
    reachability = _panel(client.get("/admin").get_data(as_text=True), "reachability-heading")
    assert "no chain in that table has an adapter in this process" in reachability, (
        "an empty probe target renders a bare count instead of saying what zero means"
    )
    assert "nothing to\n     contact" in reachability or "nothing to contact" in re.sub(
        r"\s+", " ", reachability
    )


# --- the customer's availability indicators ---------------------------------
#
# Operator instruction 2026-10-02, verbatim: "the user screen should just have
# graphical indicators to what's availble for them to swap."
#
# The page was a three-column table whose third column rendered
# chains/registry.why_unconfigured() per pair -- a paragraph naming which RPC
# variables are unset and telling the reader to export them in the shell that
# starts the server. A customer has no shell on this host. The audience for that
# text is the operator, who has /admin.


def _tiles(body):
    """Each indicator as (state key, direction, badge word)."""
    found = []
    for state, inner in re.findall(_TILE, body, flags=re.DOTALL):
        label = re.search(r'<span class="pair-label">(.*?)</span>', inner, flags=re.DOTALL)
        word = re.search(r'<span class="badge-word">(.*?)</span>', inner, flags=re.DOTALL)
        found.append((
            state,
            html.unescape(re.sub(r"\s+", " ", label.group(1)).strip()) if label else "",
            word.group(1).strip() if word else "",
        ))
    return found


def test_no_operator_facing_remedy_text_reaches_the_customer_page(client, monkeypatch):
    """THE CHANGE, ASSERTED AS AN ABSENCE, AND IT IS ALSO A DISCLOSURE FIX.

    The removed paragraph told an unauthenticated reader which RPC variables are
    unset on this host, on the same port as /admin, whose own banner says anyone
    who can reach it can read everything. I agree with that reading of it; the
    instruction stands either way.

    Asserted over the configuration's own variable names rather than a hardcoded
    list, so a chain added later cannot leak a name this test never heard of.
    """
    monkeypatch.setitem(client.application.config, "ADAPTERS", {})
    body = client.get("/").get_data(as_text=True)

    for phrase in (
        "_RPC_PORT", "_RPC_USER", "_RPC_PASS", "_RPC_URL", "_DEPOSIT_ACCOUNT",
        ".env", "export", "adapter", "environment this process was started with",
    ):
        assert phrase not in body, f"the customer page names {phrase!r}, which its reader cannot act on"
    for asset in client.application.config["RPC"]:
        assert f"{asset}_RPC" not in body, f"{asset}'s RPC settings are named to a customer"


def test_every_reason_that_left_the_customer_page_is_on_the_operator_page(client, monkeypatch):
    """THE RULE 14 CHECK, and it is the condition this change had to meet.

    Nothing may leave the system's reporting; what changed is which surface
    carries it. So for each cause, the sentence a customer no longer sees has to
    be findable on /admin -- and the two that were NOT there before this change
    (why_cannot_pay_out()'s and why_cannot_take_deposits()'s) were added to the
    Chains table, per asset, in the same commit.

    Asked of both surfaces in ONE process, which is the only way the question
    means anything: each page on its own looks internally consistent.
    """
    # Every chain reachable, no shared deposit accounts, XRP unable to sign: that
    # renders all three causes at once.
    monkeypatch.setitem(client.application.config, "ADAPTERS", {
        **{asset: StubAdapter(can_spend=True) for asset in ("BTC", "GRC", "LTC", "SOL")},
        "XRP": StubAdapter(can_spend=False),
    })
    monkeypatch.setitem(client.application.config, "XRP_DEPOSIT_ACCOUNT", "")
    monkeypatch.setitem(client.application.config, "SOL_DEPOSIT_ACCOUNT", "")

    customer = client.get("/").get_data(as_text=True)
    operator = client.get("/admin").get_data(as_text=True)
    rows = allowed_pair_rows(client.application.config, client.application.config["ADAPTERS"])

    causes = {row["cannot_pay"] for row in rows if row["cannot_pay"]}
    causes |= {row["cannot_take"] for row in rows if row["cannot_take"]}
    assert causes, "this fixture produced no refusal at all; the assertion would be vacuous"
    for cause in causes:
        assert cause not in customer, f"an operator-facing reason is still on the customer page: {cause!r}"
        assert cause in operator, (
            f"this reason is on NEITHER page, so it left the system's reporting: {cause!r}"
        )


def test_the_indicators_and_the_form_cannot_disagree(client, monkeypatch):
    """A direction shown as available that the form does not offer is a new contradiction.

    Exactly the kind removed between /admin and / earlier today, so it is pinned
    rather than left to inspection: the set of directions marked available must be
    the set of options the select renders, both ways.
    """
    monkeypatch.setitem(client.application.config, "ADAPTERS", {
        "GRC": StubAdapter(can_spend=True), "XRP": StubAdapter(can_spend=False),
    })
    with_deposit_accounts(client, monkeypatch)
    body = client.get("/").get_data(as_text=True)

    marked = {direction for state, direction, _ in _tiles(body) if state == "available"}
    offered = {
        f"{source} → {destination}"
        for source, destination in re.findall(r'<option value="([A-Z]+):([A-Z]+)"', body)
    }
    assert marked == offered, (
        f"marked available but not offered: {sorted(marked - offered)}; "
        f"offered but not marked available: {sorted(offered - marked)}"
    )


def test_an_offline_direction_and_an_unusable_one_are_different_markers(client, monkeypatch):
    """A customer deciding whether to come back later needs these to differ.

    `missing` is a chain this process cannot reach, which changes without a
    release. cannot_pay / cannot_take do not change while this server runs as it
    is, so telling that customer to come back later would be a promise nothing is
    going to keep. Both causes are in one render here.
    """
    monkeypatch.setitem(client.application.config, "ADAPTERS", {
        "GRC": StubAdapter(can_spend=True), "XRP": StubAdapter(can_spend=False),
    })
    with_deposit_accounts(client, monkeypatch)
    states = {direction: state for state, direction, _ in _tiles(client.get("/").get_data(as_text=True))}

    assert states.get("GRC → XRP") == "unavailable", (
        f"a destination that holds no signing key is marked {states.get('GRC ' + chr(8594) + ' XRP')!r}"
    )
    assert states.get("BTC → GRC") == "unreachable", "a chain with no adapter here is offline"
    assert states["GRC → XRP"] != states["BTC → GRC"], (
        "the two causes share a marker, so a customer cannot tell whether to come back"
    )


def test_the_markers_are_legible_as_pasted_text(client, monkeypatch):
    """THE CHECK THAT COULD HAVE VETOED THIS, applied to the indicators.

    This operator reads these pages by pasting them back, so an indicator that is
    a colored border and nothing else would have failed at their actual workflow.
    Each tile pastes as ONE line -- the direction and then a glyph and a word --
    and the key above pastes as three tab-separated lines. Measured: the panel is
    twenty lines for thirteen directions.
    """
    monkeypatch.setitem(client.application.config, "ADAPTERS", {
        "GRC": StubAdapter(can_spend=True), "XRP": StubAdapter(can_spend=False),
    })
    with_deposit_accounts(client, monkeypatch)
    pasted = _paste(_panel(client.get("/").get_data(as_text=True), "pairs-heading"))
    lines = pasted.splitlines()

    for state, direction, word in _tiles(client.get("/").get_data(as_text=True)):
        matching = [line for line in lines if line.startswith(direction)]
        assert matching, f"{direction} ({state}) did not survive a paste at all"
        assert len(matching) == 1, f"{direction} pasted as {len(matching)} lines: {matching}"
        assert word in matching[0], (
            f"{direction} pasted as {matching[0]!r}, which does not say whether it is usable"
        )
    # And the three markers are distinguishable from each other as text.
    words = {word for _, _, word in _tiles(client.get("/").get_data(as_text=True))}
    assert len(words) >= 2, "this fixture should render at least two different markers"
    assert all(any(word in line for line in lines) for word in words)


def test_the_tiles_are_one_column_at_phone_width():
    """360px, which is the requirement, and it falls out of the floor.

    The grid's floor is in `rem` rather than `px` deliberately: a tile's width is
    set by its longest line of WORDS, so a reader at 200% text size gets fewer,
    wider tiles instead of three columns of two-word lines. 15rem is 240px at the
    default size, so two tracks need 480px plus a gap and cannot fit a 360px
    screen with its gutters -- no media query required, and one mechanism rather
    than a floor and a breakpoint that could disagree.

    NOT BEHAVIORAL (rule 17): no browser here. It establishes the floor is big
    enough, which is the thing a later change could break silently.
    """
    css = STYLESHEET.read_text()
    rule = re.search(r"\.swapgrid\s*\{(.*?)\}", css, flags=re.DOTALL)
    assert rule, "the indicator grid has no rule"
    floor = re.search(r"minmax\((\d+(?:\.\d+)?)rem,\s*1fr\)", rule.group(1))
    assert floor, f"the grid does not size its columns with a rem minmax: {rule.group(1)!r}"
    pixels = float(floor.group(1)) * 16
    assert 2 * pixels + 8 > 360 - 32, (
        f"two {pixels:.0f}px tracks fit a 360px phone less its 16px gutters, so the tiles "
        f"would be two columns there"
    )


def test_no_customer_facing_note_promises_a_chain_will_come_back():
    """ADDED BECAUSE A MUTATION SURVIVED, and the claim it broke was mine.

    services/pair_view.customer_availability()'s own comment says "neither note
    promises anything and neither names a variable" -- and nothing checked the
    first half. Rewriting the OFFLINE note to "Temporarily down, try again in a
    few minutes" passed every other test in this file.

    It matters more than tone. `missing` means no adapter for a chain in THIS
    server process, and nothing in this application knows whether or when that
    changes: it is a daemon somebody else starts or a variable somebody else
    exports. A page that tells a customer to come back in a few minutes is making
    a forecast out of a fact, which is the register rule 17 is about, aimed at a
    customer instead of at the operator.

    So the note may say what IS true now and may not say what WILL be. Asserted
    over the table rather than over the rendered page, because the table is where
    the words are chosen and a new state would get its note from the same place.
    """

    assert CUSTOMER_STATES, "the state table is empty, so there is nothing to show"
    for state in CUSTOMER_STATES:
        note = state["note"].lower()
        for promise in (
            "try again", "come back", "minutes", "shortly", "soon", "will be back",
            "later", "wait a", "check back",
        ):
            assert promise not in note, (
                f"the {state['key']!r} note promises something this application cannot know: "
                f"{state['note']!r} contains {promise!r}"
            )
        # And no note may name a setting: that is the operator's text, not this page's.
        for internal in ("_RPC", "_DEPOSIT_ACCOUNT", ".env", "export", "adapter"):
            assert internal.lower() not in note, (
                f"the {state['key']!r} note names {internal!r}, which its reader cannot act on"
            )
        assert note.strip(), f"the {state['key']!r} state has no note at all"


def test_the_operator_legend_defines_ENABLED_as_all_three_conditions(client):
    """ADDED BECAUSE A MUTATION SURVIVED, on a defect I introduced and then fixed.

    /admin's state legend said ENABLED means "in Config.ALLOWED_PAIRS, and both
    chains have an adapter in this process" -- which was the TWO-condition verdict
    pair_rows() used before it started reading pair_view.pair_serviceability().
    The verdict gained a third condition in that same commit and this sentence did
    not, so the page defined ENABLED as something weaker than what it takes to be
    badged ENABLED. Found by auditing the customer page's reasons against this
    one, fixed, and then a mutation put it back without failing anything.

    The three conditions are pair_serviceability()'s, so the legend has to name
    all three or it is describing a different gate from the one that ran.
    """
    body = html.unescape(client.get("/admin").get_data(as_text=True))
    legend = re.sub(
        r"\s+", " ", body[body.index("What each state means") : body.index("Every ordered pair")]
    )
    for condition in (
        "Config.ALLOWED_PAIRS",
        "adapter in this process",
        "can take a deposit",
        "can pay out",
    ):
        assert condition in legend, (
            f"the ENABLED legend does not name {condition!r}, so it defines ENABLED as something "
            f"other than what pair_serviceability() checks"
        )


# --- the operator page's tabs -----------------------------------------------
#
# Operator instruction 2026-10-02: "also, our operator page should be tabs."
#
# THE MEASUREMENT THAT CHOSE THE MECHANISM, taken on the real page before
# anything was built. A browser copy does not include `display: none` content,
# and this operator reads these pages by pasting them back -- two defects were
# caught that way today. Marking every panel but one hidden, exactly as any tab
# mechanism does, a paste returned 923 of 12,502 characters (7.4%) and 0 of the
# 14 panel headings, with nothing in it saying the rest existed.
#
# So the tabs navigate and nothing hides. The tests below pin that: the strip
# exists and is complete, every panel is still in a paste, and nothing on the
# page claims a tab role it does not implement.


def _hidden_from_a_copy(markup: str) -> list[str]:
    """Every panel a browser copy would skip, by heading.

    A copy skips `display: none` and `visibility: hidden` content, and the
    attribute forms of the same thing -- `hidden` and `aria-hidden="true"` -- are
    how a tab mechanism expresses it in markup. Any of them on a panel means that
    panel leaves a paste.
    """
    skipped = []
    for opening, inner in re.findall(
        r'(<section class="(?:panel|hero)[^"]*"[^>]*>)(.*?)</section>', markup, flags=re.DOTALL
    ):
        concealed = (
            " hidden" in opening
            or 'aria-hidden="true"' in opening
            or "display:none" in opening.replace(" ", "")
            or "visibility:hidden" in opening.replace(" ", "")
        )
        if concealed:
            heading = re.search(r"<h[12][^>]*>(.*?)</h[12]>", inner, flags=re.DOTALL)
            skipped.append(
                html.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", heading.group(1))).strip())
                if heading else "(a panel with no heading)"
            )
    return skipped


def test_a_paste_of_the_operator_page_still_contains_every_panel(client):
    """THE TEST THE WHOLE DESIGN EXISTS TO PASS.

    If this fails, the operator's primary way of reading the page has started
    returning a fraction of it with nothing saying anything is missing -- which is
    worse than removing a panel, because removing one is visible.

    Asserted three ways, because one would not be enough: every heading survives
    the copy, no panel carries a concealing attribute, and nothing declares the
    ARIA tab roles whose contract is one visible panel.
    """
    markup = client.get("/admin").get_data(as_text=True)
    pasted = _paste(markup)

    headings = [
        html.unescape(re.sub(r"\s+", " ", heading).strip())
        for heading in re.findall(r'<h2 id="[a-z-]+">(.*?)</h2>', markup, flags=re.DOTALL)
    ]
    assert len(headings) >= 14, f"only {len(headings)} panel headings rendered; the page has fourteen"
    missing = [heading for heading in headings if heading not in pasted]
    assert missing == [], f"{len(missing)} of {len(headings)} panels would not survive a copy: {missing}"

    assert _hidden_from_a_copy(markup) == [], (
        f"these panels carry a concealing attribute, so a paste loses them: "
        f"{_hidden_from_a_copy(markup)}"
    )
    for role in ('role="tablist"', 'role="tab"', 'role="tabpanel"'):
        assert role not in markup, (
            f"{role} promises one visible panel and arrow-key movement between tabs, and this page "
            f"implements neither -- claiming it describes the page wrongly to the reader who depends "
            f"on the description most"
        )


def test_the_tab_strip_lists_every_section_and_only_real_anchors(client):
    """Bidirectional, because two places name one set of sections.

    services/admin_view.PAGE_SECTIONS renders the strip and the headings live in
    the template -- rule 8's shape, so the correspondence is checked both ways
    over the RENDERED page rather than trusted. A section added to the template
    without a tab fails here, and a tab pointing at an anchor that does not exist
    fails here.
    """

    markup = client.get("/admin").get_data(as_text=True)
    strip = re.search(r'<nav class="tabstrip"[^>]*>(.*?)</nav>', markup, flags=re.DOTALL)
    assert strip, "the tab strip is gone"

    links = re.findall(r'<a href="#([a-z-]+)">(.*?)</a>', strip.group(1), flags=re.DOTALL)
    assert [anchor for anchor, _ in links] == [anchor for anchor, _ in PAGE_SECTIONS], (
        "the strip does not render PAGE_SECTIONS in order"
    )

    heading_text = {
        anchor: html.unescape(re.sub(r"\s+", " ", text).strip())
        for anchor, text in re.findall(r'<h2 id="([a-z-]+)">(.*?)</h2>', markup, flags=re.DOTALL)
    }
    assert set(heading_text) == {anchor for anchor, _ in PAGE_SECTIONS}, (
        f"only on the page: {sorted(set(heading_text) - {a for a, _ in PAGE_SECTIONS})}; "
        f"only in PAGE_SECTIONS: {sorted({a for a, _ in PAGE_SECTIONS} - set(heading_text))}"
    )

    # A LABEL MAY BE SHORTER THAN ITS HEADING AND MAY NOT SAY SOMETHING ELSE. That
    # is what keeps the two spellings from drifting into two different names for
    # one section without forcing the strip to repeat "Payouts claimed but never
    # reported sent" at pill width.
    for anchor, label in links:
        words = set(re.findall(r"[a-z]+", html.unescape(label).lower()))
        heading_words = set(re.findall(r"[a-z]+", heading_text[anchor].lower()))
        assert words <= heading_words, (
            f"the tab {label!r} says {sorted(words - heading_words)}, which its heading "
            f"{heading_text[anchor]!r} does not"
        )


def test_every_tab_is_a_same_document_link_so_a_probe_result_cannot_be_discarded(client):
    """THE PROBE RESULTS, AND WHY THIS MECHANISM MAKES THEM SAFE WITHOUT TRYING.

    Reachability and the peg check FETCH on click and can take 30 seconds per
    chain; static/admin.js writes the answer into `#probe-result` and
    `#peg-result` in place. A real tab switch would have to hide or destroy that
    region, so the operator could pay for a probe and then lose it by looking at
    another tab.

    Here there is no switch to survive: every tab is a fragment-only link, so the
    document never reloads and nothing is re-rendered. That is asserted rather
    than argued -- a tab that gained an `href` to a path, or a `target`, would
    navigate and would take the result with it.
    """
    markup = client.get("/admin").get_data(as_text=True)
    strip = re.search(r'<nav class="tabstrip"[^>]*>(.*?)</nav>', markup, flags=re.DOTALL).group(1)

    for attributes in re.findall(r"<a ([^>]*)>", strip):
        href = re.search(r'href="([^"]*)"', attributes)
        assert href, f"a tab has no href: {attributes!r}"
        assert href.group(1).startswith("#"), (
            f"the tab {href.group(1)!r} is not a same-document link, so following it reloads the page "
            f"and discards any probe result the operator has paid for"
        )
        assert "target=" not in attributes, "a tab that opens elsewhere takes the probe result with it"

    # And the two regions the probes write into are present and outside anything
    # that could remove them: they are in panels, and no panel is concealed.
    for region in ('id="probe-result"', 'id="peg-result"'):
        assert region in markup, f"{region} is gone, so a probe has nowhere to render"
    assert _hidden_from_a_copy(markup) == [], "a probe region sits in a panel a copy would skip"


def test_the_sticky_strip_clears_the_topbar_it_sits_under():
    """Two constants that must agree, so a test says so rather than a comment.

    `.topbar` is `position: sticky; top: 0` with a `min-height`, and the strip
    sticks below it. If the strip's `top` were less than that height the strip
    would sit under the topbar; if the headings' `scroll-margin-top` did not clear
    both, a jump would land with the heading hidden behind them.

    NOT BEHAVIORAL (rule 17): there is no browser here and nothing can observe an
    overlap. What this holds is that the numbers cannot drift apart silently.
    """
    css = STYLESHEET.read_text()
    topbar = re.search(r"\.topbar-inner\s*\{(.*?)\}", css, flags=re.DOTALL)
    assert topbar, "the topbar rule is gone"
    bar_height = re.search(r"min-height:\s*(\d+)px", topbar.group(1))
    assert bar_height, f"the topbar declares no min-height: {topbar.group(1)!r}"

    strip = re.search(r"\.tabstrip\s*\{(.*?)\}", css, flags=re.DOTALL)
    assert strip, "the tab strip has no rule"
    offset = re.search(r"top:\s*(\d+)px", strip.group(1))
    assert offset, f"the sticky strip names no top offset: {strip.group(1)!r}"
    assert offset.group(1) == bar_height.group(1), (
        f"the strip sticks at {offset.group(1)}px under a topbar {bar_height.group(1)}px tall, so one "
        f"of them covers the other"
    )
    assert f"calc({bar_height.group(1)}px" in css, (
        "the headings' scroll-margin-top does not clear the topbar, so a jump lands behind it"
    )


def test_the_strip_wraps_at_phone_width_rather_than_hiding_labels():
    """Fourteen labels do not fit 360px, and the choice is wrap, not scroll.

    A data table that overflows gets `.scroll-x`, because its columns are of one
    kind and a reader knows more exist. A NAVIGATION strip that overflows hides
    labels, and a hidden nav label is a section the operator cannot discover. So
    it wraps -- about five rows at phone width -- and stops being sticky there,
    because five rows pinned to the top of a 360px screen is most of the screen.
    """
    css = STYLESHEET.read_text()
    strip_list = re.search(r"\.tabstrip ul\s*\{(.*?)\}", css, flags=re.DOTALL)
    assert strip_list, "the strip's list has no rule"
    assert "flex-wrap: wrap" in strip_list.group(1), "the strip does not wrap, so labels fall off the row"
    assert "overflow-x" not in strip_list.group(1), (
        "the strip scrolls sideways, which hides labels and makes a section undiscoverable"
    )

    phone = css[css.index("@media (max-width: 560px)"):]
    phone = phone[: phone.index("@media (prefers-reduced-motion")]
    assert ".tabstrip { position: static; }" in phone, (
        "a five-row strip stays pinned at phone width, where it is most of the screen"
    )


def test_the_strip_says_a_copy_of_the_page_is_still_complete(client):
    """ADDED BECAUSE A MUTATION SURVIVED, and the sentence is rule 14's.

    An operator who has just been told "the operator page is tabs" has every
    reason to assume a copy contains one tab -- that is what tabs normally mean,
    and it is what the measurement said a hiding implementation would do: 7.4% of
    the text and none of the headings. The first time they paste this page back
    they would wonder which tab they had sent.

    So the strip says it, in words, next to the thing that raises the question.
    Deleting the sentence leaves the page BEHAVING correctly and the operator
    unable to know it, which is the same shape as a cycle printing exit_code=0
    beside "skipping": the reader cannot tell the good case from the bad one.
    """
    strip = re.search(
        r'<nav class="tabstrip"[^>]*>(.*?)</nav>',
        client.get("/admin").get_data(as_text=True), flags=re.DOTALL,
    )
    assert strip, "the tab strip is gone"
    note = re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", strip.group(1)))).strip()
    assert "Copying the page copies all of it" in note, (
        f"the strip does not tell the operator a copy is complete; it says {note[-120:]!r}"
    )
    assert "Every section is on this page below" in note, (
        "and it does not say the sections are all still present"
    )
