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
from test_web_surfaces import fully_reachable
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


# --- every box renders, and every box has a heading ------------------------


def test_the_swap_form_lists_its_pairs_as_a_table_with_column_headings(client):
    """The pair list is a table, and its three columns are named.

    It was a `.pair-list`, which is `display: flex; flex-wrap: wrap` -- pills of
    varying width reflowing across the viewport, so the badge landed at a
    different x on every one and nothing could be read down a column.
    """
    body = client.get("/").get_data(as_text=True)
    for heading in ("direction", "status", "what would change it"):
        assert f">{heading}</th>" in body, f"the pair table lost its {heading!r} column heading"
    allowed = client.application.config["ALLOWED_PAIRS"]
    rendered = body.count('class="pair-label"')
    assert rendered == len(allowed), (
        f"{len(allowed)} pairs are allowed and the table rendered {rendered} direction cells"
    )


def test_an_enabled_pairs_reason_cell_is_not_a_blank(client, monkeypatch):
    """A table cell that could be empty says what is true of it instead.

    In the pill layout the reason simply did not render for a working pair and a
    flex row absorbed the absence invisibly. A table cell does not: a column of
    five blanks beside one sentence reads as five broken rows, and rule 14 is
    explicit that a blank gap is ambiguous between "nothing to report" and "the
    query broke".
    """
    allowed = client.application.config["ALLOWED_PAIRS"]
    fully_reachable(client, monkeypatch, *{asset for pair in allowed for asset in pair})
    body = client.get("/").get_data(as_text=True)
    assert "(nothing)" in body, "an enabled pair rendered an empty third column"
    assert "both chains reachable" in body, "and it must say WHY there is nothing to report"


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
    for column in ("amount", "confirmed", "reserved", "available", "vout", "confirmations"):
        assert f'<th scope="col">{column}</th>' not in body, (
            f"a {column!r} column on the operator page is not marked numeric"
        )
    assert '<th scope="col" class="num">amount</th>' in body, "no amount column rendered at all"
    assert 'class="num">confirmed</th>' in body, "the balance table did not render"
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
