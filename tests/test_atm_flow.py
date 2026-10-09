"""Can a customer actually get from step 1 to a created swap, one question at a time?

Role: test / measurement (drives the real Flask handlers against a real schema
      and stub adapters; asserts on what the server renders and what it writes)
Reads: routes/atm.py, services/wizard.py, chains/amount_solve.py
Writes: a per-test SQLite database on the real schema
Can move funds: no. The stub adapters sign nothing and open no socket.
Mainnet-safe: yes

WHY A FLOW TEST AND NOT ONLY THE UNIT TESTS IN test_wizard.py. Those assert the
DECISIONS -- which step is current, what a back clears, which lamp is right. This
asserts the decisions are WIRED: that the hidden fields carry, that a refusal
redraws the question rather than advancing, and that the review's button reaches
create_swap(). Every defect this project has paid for in the UI was a correct
decision read from the wrong place, so both halves are needed.

THE STUB ADAPTER IS test_web_surfaces.StubAdapter, IMPORTED RATHER THAN COPIED.
It already declares the five things pair_view and quote_service ask of a
reachable chain, each with a measured reason in its docstring -- a second stub
here would agree on the day it was written and then drift, which is rule 8's
shape in a test fixture. It is extended with get_balance() only, because
services/payout_capacity.largest_fundable_payout() is the one thing the ATM asks
for that the pair page never did.
"""

from __future__ import annotations

import re
import sqlite3

import pytest
import routes.atm as atm_module
import services.quote_service as quote_module
from chains.amount_solve import (
    CHAIN_PRECISION,
    deposit_for_desired_payout,
    payout_for_deposit,
    quantize_down,
)
from chains.icp_account import account_identifier, subaccount_from_index
from config import Config
from db import SCHEMA, dict_factory
from engineering_notation import (
    EXPONENT_STEP,
    engineering_notation,
    needs_engineering,
)
from modules.htlc_assets import (
    WORD_BROKERED,
    WORD_COVERED,
    WORD_PROVEN,
    settlement_verdict,
)
from test_web_surfaces import DEPOSIT_ACCOUNTS, StubAdapter
from valid_addresses import GRC_PAYOUT, base58_testnet

import app as app_module  # isort: skip

#: USD spot prices, FIXED, so these tests never touch the network.
#:
#: NOT A CONVENIENCE -- THE FIRST VERSION OF THIS FILE DID touch it, and the
#: failure was the useful kind. Rendering the amount screen called
#: services/pricing.fetch_usd_prices() bare, so seven tests failed with
#: `OSError: Tunnel connection failed: 403 Forbidden` -- which is also what a
#: CUSTOMER would have got, as a 500, whenever the feed was unreachable. The route
#: now degrades with a sentence instead; these figures let the flow be exercised
#: on the path where the feed DOES answer.
#:
#: ICP at 10.0 and GRC at 0.031 make the ICP->GRC rate 322.58, which is close
#: enough to the live 323.37 that the capacity arithmetic behaves as it does in
#: production without pinning a live price into a test.
USD_PRICES = {
    "BTC_USD": 62000.0, "LTC_USD": 70.0, "GRC_USD": 0.031,
    "ICP_USD": 10.0, "SOL_USD": 140.0, "XRP_USD": 0.52,
    "fetched_at": 0.0,
}

#: What the desk can pay out, per asset, in these tests. A REAL-LOOKING GRC
#: BALANCE because the operator's own container reads 407.51074481 and the
#: capacity ceiling is the figure step 3 is about -- a round 1000 would make a
#: rounding error at the eighth decimal invisible.
BALANCES = {"GRC": 407.51074481, "ICP": 1000.0, "BTC": 0.5, "LTC": 20.0}


class FundedStub(StubAdapter):
    """StubAdapter plus get_balance(), which the ATM's capacity ceiling needs.

    SUBCLASSED RATHER THAN REIMPLEMENTED so every gate the parent satisfies keeps
    being satisfied: pair_view grew from three conditions to five over 2026-10-01
    to 10-03, and each one was added to that stub with the measurement that forced
    it. A fresh stub here would start failing those gates the next time one is
    added, and the failure would look like an ATM bug.
    """

    def __init__(self, asset: str, can_spend: bool = True):
        super().__init__(can_spend=can_spend)
        self.asset = asset
        # ICP's DEPOSIT ADDRESS IS DERIVED FROM A PRINCIPAL, so an ICP stub without
        # one refuses at create_swap() with "'FundedStub' object has no attribute
        # 'owner_principal'". Found 2026-10-07 by this file's last failing test --
        # and the route RENDERED that refusal rather than returning a 500, which is
        # the degradation the price-feed fix put there and is worth noting as the
        # one thing that behaved correctly about this failure.
        #
        # A REAL-SHAPE PRINCIPAL AND NOT A PLACEHOLDER, because
        # chains/icp_account.py derives the 64-hex account identifier from it by
        # the real algorithm -- a malformed string would be refused for its FORMAT
        # and the test would pass for the wrong reason, exactly as
        # test_web_surfaces.DEPOSIT_ACCOUNTS says of its own XRP and SOL accounts.
        # This is a PUBLIC identifier, the equivalent of an address; no key
        # material is involved and none is in this repository.
        self.owner_principal = "ybr6p-5dyeb-d5vhs-rl366-n2zsw-muftf-orv27-ey4hk-3y4de-fkhhf-cqe"

    def get_balance(self) -> float:
        return BALANCES.get(self.asset, 0.0)

    def deposit_address(self, subaccount_index: int) -> str:
        """ICP's per-swap account identifier, derived the way the real adapter derives it.

        chains/icp.ICPAdapter.deposit_address() is `account_identifier(principal,
        subaccount_from_index(index))`, and this calls the SAME two functions
        rather than returning a plausible 64-hex string. An invented one would be
        refused by modules/address_authority's ICP validator for its format and
        the test would fail for the wrong reason -- or worse, pass while exercising
        none of the derivation the deposit address actually depends on.

        The index-0 refusal is reproduced too, because it is a guard rather than a
        detail: index 0 is the DESK'S OWN account, and publishing it as a deposit
        address would mix customer payments into desk inventory.
        """
        if subaccount_index < 1:
            raise ValueError(f"subaccount index {subaccount_index} is below 1; index 0 is the desk's own")
        return account_identifier(self.owner_principal, subaccount_from_index(subaccount_index))

    def get_new_address(self, label: str) -> str:
        """A per-swap address for the bitcoin-family chains, unique per label.

        The real adapters call `getnewaddress`, which returns a fresh address each
        time. Returning a CONSTANT here would make every swap on a chain share one
        deposit address, so the uniqueness this flow depends on would be untested
        -- and tests/valid_addresses.py derives from a phrase for exactly this, so
        the label becomes the phrase and each swap gets its own valid address.
        """
        return base58_testnet(f"atm flow deposit for {self.asset} {label}")


@pytest.fixture
def client(tmp_path, monkeypatch):
    """The real app on the real schema, with GRC/ICP/BTC/LTC reachable and prices fixed."""
    db_path = tmp_path / "atm.db"
    conn = sqlite3.connect(db_path)
    conn.row_factory = dict_factory
    conn.executescript(SCHEMA)
    conn.commit()
    conn.close()

    flask_app = app_module.app
    monkeypatch.setitem(flask_app.config, "DB_PATH", str(db_path))
    monkeypatch.setitem(
        flask_app.config, "ADAPTERS",
        {asset: FundedStub(asset) for asset in ("GRC", "ICP", "BTC", "LTC")},
    )
    for variable, account in DEPOSIT_ACCOUNTS.items():
        monkeypatch.setitem(flask_app.config, variable, account)
    # PATCHED WHERE IT IS USED, not where it is defined. routes/atm.py and
    # services/quote_service.py each did `from services.pricing import
    # fetch_usd_prices`, so they hold their own references and patching
    # services.pricing alone would leave both calling the real feed -- the
    # classic monkeypatch miss, and here it would have shown up as the same 403.
    monkeypatch.setattr(atm_module, "fetch_usd_prices", lambda _ttl=0: dict(USD_PRICES))
    monkeypatch.setattr(quote_module, "fetch_usd_prices", lambda _ttl=0: dict(USD_PRICES))

    flask_app.config["TESTING"] = True
    with flask_app.test_client() as test_client:
        yield test_client


def post(client, **answers):
    """One step's POST, with every answer this flow carries."""
    return client.post("/", data=answers, follow_redirects=False)


def asking(response) -> str:
    """Which question the page is actually ASKING, out of its <h1>.

    NOT `question in body`, AND THE DIFFERENCE CAUGHT MY OWN BAD ASSERTIONS. The
    progress strip lists all six questions on every screen, so "What do you want
    back?" is in the body of step 1 and `not in body` can never be true. Four of
    these tests asserted that and were measuring nothing; one of them hid a real
    route bug for a round. The <h1> is the one element that says where you are.
    """
    match = re.search(r'<h1 id="atm-question" class="atm-question">(.*?)</h1>', response.get_data(as_text=True))
    return match.group(1).strip() if match else "(no question heading rendered)"


def test_the_flow_opens_on_the_first_question(client):
    """A GET, so the flow is linkable and survives a closed tab."""
    page = client.get("/")
    assert page.status_code == 200
    body = page.get_data(as_text=True)
    assert asking(page) == "What are you sending?"
    assert 'class="atm-strip"' in body, "the progress strip is what makes this read as a sequence"
    # Every coin appears as a choice, working or not -- "not offered" and "down
    # right now" must look different (rule 14).
    for asset in ("BTC", "GRC", "ICP", "LTC", "SOL", "XRP"):
        assert f'value="{asset}"' in body, f"{asset} is in ALLOWED_PAIRS and must be shown"


def test_a_coin_the_desk_cannot_serve_is_offered_disabled_with_its_count(client, monkeypatch):
    """SOL and XRP have no adapter here, so no SOL/XRP direction can be quoted."""
    body = client.get("/").get_data(as_text=True)
    # The lamp's own count reaches the screen, which is rule 14's "state what the
    # number means, next to the number".
    assert "0/5 out" in body, "a coin with nothing serviceable must show its denominator"


def test_picking_a_source_advances_to_the_destination_question(client):
    """Step 1 answered -> step 2, and only what ICP can become is offered."""
    page = post(client, from_asset="ICP")
    assert page.status_code == 200
    body = page.get_data(as_text=True)
    assert asking(page) == "What do you want back?"
    # The answer is carried as a hidden field, because there is no session.
    assert 'name="from_asset" value="ICP"' in body
    # GRC is reachable here and is a real ICP destination.
    assert 'name="to_asset" value="GRC"' in body


def test_an_unserviceable_source_redraws_the_same_question_with_the_reason(client):
    """A refusal must not advance, and it must say why in the lamp's own words."""
    page = post(client, from_asset="SOL")
    body = page.get_data(as_text=True)
    assert page.status_code == 200
    assert asking(page) == "What are you sending?", "a refused answer stays on its own question"
    assert "SOL" in body
    assert "0 of 5 directions out of SOL" in body, "the lamp's sentence is the error's sentence"


def test_the_amount_question_states_the_desks_own_limit_before_you_type(client):
    """The whole reason this is an ATM: the limit comes before the typing.

    services/quote_service.py performs no capacity check and create_swap()
    performs it last, so without this the customer answers every question and is
    refused on the final screen.
    """
    page = post(client, from_asset="ICP", to_asset="GRC")
    body = page.get_data(as_text=True)
    assert asking(page) == "How much?"
    # Either side, which is what the operator asked for.
    assert 'value="send"' in body
    assert 'value="receive"' in body
    assert "I am sending this much ICP" in body
    assert "I want this much GRC" in body


def test_an_amount_over_what_the_desk_can_pay_is_refused_at_the_amount_step(client):
    """Refused HERE and not at the confirm screen, which is the dead end removed.

    The desk holds 407.51074481 GRC in this fixture, so an ICP deposit large
    enough to need more GRC than that cannot be honored -- and the customer learns
    it on the screen where they typed it.
    """
    page = post(client, from_asset="ICP", to_asset="GRC", amount="1000000", amount_side="send")
    body = page.get_data(as_text=True)
    assert asking(page) == "How much?", "an over-limit amount stays on the amount question"
    assert "The most this desk can take right now is" in body


def test_a_usable_amount_advances_to_the_address_question(client):
    """A small ICP send is well inside the GRC the desk holds."""
    page = post(client, from_asset="ICP", to_asset="GRC", amount="0.001", amount_side="send")
    body = page.get_data(as_text=True)
    assert asking(page) == "Where should it go?"
    assert "Your GRC address" in body
    assert 'name="amount" value="0.001"' in body, "the amount carries forward"


def test_an_address_the_chain_would_refuse_stays_on_the_address_question(client):
    """modules/address_authority's verdict, not a second opinion written here."""
    page = post(
        client, from_asset="ICP", to_asset="GRC", amount="0.001", amount_side="send",
        payout_address="not-an-address",
    )
    assert asking(page) == "Where should it go?"


def test_a_valid_address_reaches_the_review_which_names_the_counterparty(client):
    """The review must say who holds the funds, which no screen said before.

    Measured 2026-10-07: all 30 pairs settle CUSTODIALLY in this terminal, so a
    customer confirming any pair extends credit to this desk between their deposit
    confirming and their payout broadcasting. Commit 26e2d79's own message was
    "Two atomic settlement mechanisms, a custodial third, and no screen saying
    which one a pair used".
    """
    page = post(
        client, from_asset="ICP", to_asset="GRC", amount="0.001", amount_side="send",
        payout_address=GRC_PAYOUT,
    )
    body = page.get_data(as_text=True)
    assert asking(page) == "Is this right?"
    assert "Who you are trading with" in body
    # THE MODULE'S OWN SENTENCE, CHARACTER FOR CHARACTER, rather than a word out of
    # it. This used to assert `"CUSTODIALLY" in body` and `"holds your funds" in
    # body` -- two words out of two DIFFERENT sentences, one from the operator pill
    # and one hand-written in the template beneath it, and the pairing is what made
    # the defect invisible: the pill rendered "RUN GREEN -- atomic_swap.py (P2SH
    # HTLC on both legs); this terminal settles it CUSTODIALLY, with no hashlock"
    # to a customer and the assertion was satisfied by its last clause.
    #
    # Comparing against settlement_verdict()'s own output makes the template unable
    # to compose a sentence of its own without failing here, which is the property
    # the change is for (rule 8: five hand-written copies of this fact existed).
    assert settlement_verdict("ICP", "GRC")["customer"] in body, (
        "the review composed its own custody sentence instead of rendering the one the module derives"
    )
    assert "holding your funds" in body, "the screen must still say who holds the money"
    assert 'name="confirmed" value="1"' in body, "the commit button is on the review and nowhere else"


def test_the_review_shows_a_customer_none_of_the_settlement_machinery(client):
    """The operator pill must not be what a customer reads before pressing commit.

    MEASURED, NOT ASSERTED FROM TASTE. templates/_atm_confirm.html rendered
    `settlement.headline` -- which modules/htlc_assets.settlement_verdict() builds
    for "a column, a pill or a matrix cell" -- and across Config.ALLOWED_PAIRS that
    put one of three posture words, a driver FILENAME, a script type and, for the
    pairs with no driver, a git sha in front of someone about to send money:

        RUN GREEN -- atomic_swap.py (P2SH HTLC on both legs); this terminal
            settles it CUSTODIALLY, with no hashlock
        BROKERED ONLY -- SOL has no HTLC (c4ea027); ICP has no HTLC driver here;
            this terminal settles it CUSTODIALLY, with no hashlock

    Counted the same day: 30 of 30 pairs settle custodially, under 18 BROKERED
    ONLY, 9 COVERED NOT RUN and 3 RUN GREEN. The custody did not vary; the part
    being shown did.

    THE BANNED WORDS ARE DERIVED FROM THE MODULE, not typed here, so a fourth
    posture word or a third driver cannot arrive and go unchecked -- the same
    reasoning tests/test_customer_page_layout.py gives for asserting over the
    configuration's own variable names.
    """
    body = post(
        client, from_asset="ICP", to_asset="GRC", amount="0.001", amount_side="send",
        payout_address=GRC_PAYOUT,
    ).get_data(as_text=True)

    posture_words = {WORD_BROKERED, WORD_COVERED, WORD_PROVEN}
    drivers = {verdict["driver"] for verdict in
               (settlement_verdict(a, b) for a, b in Config.ALLOWED_PAIRS) if verdict["driver"]}
    assert posture_words and drivers, "this assertion would be vacuous with nothing to ban"

    for word in posture_words | drivers | {"P2SH", "HTLC", "hashlock"}:
        assert word not in body, f"the review screen shows a customer {word!r}"
    assert not re.search(r"\((?:[0-9a-f]{7,40})\)", body), "a git sha reached the review screen"


def test_going_back_from_the_review_returns_to_the_address_question(client):
    """Back posts, so the server decides what to clear."""
    page = client.post(
        "/",
        data={
            "from_asset": "ICP", "to_asset": "GRC", "amount": "0.001", "amount_side": "send",
            "payout_address": GRC_PAYOUT, "back": "4",
        },
    )
    assert asking(page) == "Where should it go?", "back must actually land on step 4"


def test_confirming_creates_a_swap_and_hands_off_to_its_own_page(client):
    """The commit, and step 6 is /swap/<id> rather than a sixth template.

    That page already shows the deposit address, the attribution model, the
    confirmation threshold with its meaning and the live poll. A second rendering
    would be two pages telling a customer where to send money.
    """
    page = client.post(
        "/",
        data={
            "from_asset": "ICP", "to_asset": "GRC", "amount": "0.001", "amount_side": "send",
            "payout_address": GRC_PAYOUT, "confirmed": "1",
        },
    )
    assert page.status_code == 302, f"expected a redirect to the swap page, got {page.status_code}"
    assert "/swap/s_" in page.headers["Location"], page.headers["Location"]

    # AND THE ROW EXISTS, which is the assertion that matters: a redirect to a
    # swap page proves a redirect, not a swap (the behavioral-verification
    # principle -- assert on the rows the real code wrote).
    swap_id = page.headers["Location"].rsplit("/", 1)[-1]
    conn = sqlite3.connect(client.application.config["DB_PATH"])
    conn.row_factory = dict_factory
    row = conn.execute("SELECT * FROM swaps WHERE id = ?", (swap_id,)).fetchone()
    conn.close()
    assert row is not None, f"no swaps row for {swap_id}"
    assert row["from_asset"] == "ICP"
    assert row["to_asset"] == "GRC"
    assert row["payout_address"] == GRC_PAYOUT
    assert row["status"] == "awaiting_deposit"
    assert row["deposit_address"], "a swap with no deposit address cannot be paid into"


def test_a_confirmed_flag_on_an_incomplete_flow_creates_nothing(client):
    """Two conditions guard the commit, and this is why there are two.

    routes/atm.advance() calls _commit() only when `confirmed` is present AND the
    current step is REVIEW_STEP. A stray `confirmed` in a crafted post must not be
    able to create a swap from a flow that never answered the questions.
    """
    page = post(client, from_asset="ICP", confirmed="1")
    assert page.status_code == 200
    assert asking(page) == "What do you want back?", "it must ask the next unanswered question"

    conn = sqlite3.connect(client.application.config["DB_PATH"])
    count = conn.execute("SELECT COUNT(*) FROM swaps").fetchone()[0]
    conn.close()
    assert count == 0, "a confirmed flag with no answers created a swap"


def test_a_field_no_step_asked_for_is_dropped_rather_than_echoed(client):
    """CARRIED is a tuple so a crafted post cannot inject a hidden field.

    Anything outside it is dropped on the way in, which is what stops this page
    reflecting attacker-supplied markup back into the next screen's hidden inputs.
    """
    page = post(client, from_asset="ICP", injected="<script>alert(1)</script>")
    body = page.get_data(as_text=True)
    assert "injected" not in body
    assert "alert(1)" not in body


# ===========================================================================
# THE SCREEN'S ARRANGEMENT, RENDERED. Added 2026-10-09.
#
# Operator: "the first screen only have the buttons, the colum to the right the
# fees etc OR the swap id they can enter at the bottom. next page will just be
# the buttons they want to covert into. next screen will be the either/or amount
# and get the quote and keep the fees to the right. then they can enter their
# final wallet address for their swapped crypto."
#
# tests/test_wizard.py asserts WHICH screens carry what, against a seeded step
# number. These assert the rendered consequence: that the fee table really is in
# the <aside> and not in the question's panel, that the lookup really is outside
# the two-column wrapper, that "Get the quote" really produces figures. A correct
# decision rendered into the wrong element is this project's recurring defect --
# "a correct decision read from the wrong place" is why this file exists at all.
#
# THE ELEMENT BOUNDARIES ARE FOUND BY COUNTING TAGS, NOT BY SLICING TO THE NEXT
# ONE. tests/test_customer_page_layout.status_card_of() records why: a slice
# bounded by whatever came next still contained the thing after it had been moved
# out, and the mutation survived.
# ===========================================================================


def element(body: str, open_tag: str, tag: str = "div") -> str:
    """Exactly one element's markup, from `open_tag` to its own matching close.

    Depth-counted rather than sliced to the next `</tag>`, because every
    assertion below is of the form "X is INSIDE this and Y is not" and a slice
    that overruns the element makes both halves of that true.
    """
    start = body.index(open_tag)
    depth = 0
    for match in re.finditer(rf"<{tag}\b|</{tag}>", body[start:]):
        depth += 1 if match.group(0) != f"</{tag}>" else -1
        if depth == 0:
            return body[start : start + match.end()]
    raise AssertionError(f"{open_tag} is never closed")


def reading(markup: str) -> str:
    """An element's markup as a reader sees it: tags gone, whitespace collapsed.

    NEEDED BECAUSE A FIGURE AND ITS TICKER ARE ON TWO LINES in the templates, so
    `"0.94416244 ICP" in markup` is false for a page that shows exactly that --
    which is a test failing on where a newline is rather than on what the screen
    says. The same reason tests/test_customer_page_layout.rows_of() normalizes
    before comparing, and the same reason tests/page_markup.py exists.
    """
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", markup)).strip()


def test_the_first_screen_keeps_the_coin_buttons_alone_with_the_question(client):
    """The question's panel holds the buttons and NOTHING ELSE the operator moved.

    THE STATE BEING REPLACED, measured in Chromium at 1280px before this change:
    four panels stacked down one column -- coins, swap-id lookup, the 30-direction
    matrix, the costs table -- 1291px of page with the right 40% empty. The
    question was one panel of four.

    Asserted on the question's own <section>, depth-counted, so "the fee table
    moved to the right-hand column" cannot be satisfied by it merely still being
    somewhere on the page.
    """
    body = client.get("/").get_data(as_text=True)
    question = element(body, '<section class="panel atm"', "section")

    for asset in ("BTC", "GRC", "ICP", "LTC"):
        assert f'name="from_asset" value="{asset}"' in question, f"{asset}'s button left the question"

    assert "What a swap costs" not in question, "the costs table is still inside the question's panel"
    assert "Service fee" not in question
    assert 'name="swap_id"' not in question, "the swap-id box is still inside the question's panel"
    assert "swaptile" not in question, "the 30-direction matrix is still inside the question's panel"


def test_the_costs_are_in_a_right_hand_column_and_not_a_panel_below(client):
    """"the colum to the right the fees etc" -- an <aside>, inside the flex row.

    <aside> AND NOT A SECOND <section>: it is content related to the question and
    separable from it, so a screen reader announces a complementary landmark and
    a customer tabbing through reaches the coin buttons before the fee table.
    """
    body = client.get("/").get_data(as_text=True)
    assert '<aside class="atm-aside"' in body, "there is no right-hand column at all"

    screen = element(body, '<div class="atm-screen"', "div")
    assert '<aside class="atm-aside"' in screen, (
        "the aside is outside the two-column wrapper, so it cannot sit beside the question"
    )
    aside = element(body, '<aside class="atm-aside"', "aside")
    for label in ("Service fee", "Quote validity", "Amount tolerance", "Network fee"):
        assert label in aside, f"the {label!r} row is not in the right-hand column"


def test_the_swap_id_box_is_at_the_bottom_below_both_columns(client):
    """"OR the swap id they can enter at the bottom."

    OUTSIDE the two-column wrapper, which is the structural half of "at the
    bottom": it is neither the question nor reference, it is the other thing you
    might have come to do. Position is asserted as well, because a lookup box
    that rendered first would be "at the bottom" in no sense a customer cares
    about.
    """
    body = client.get("/").get_data(as_text=True)
    assert 'name="swap_id"' in body, "a returning customer has no way back into their swap"

    screen = element(body, '<div class="atm-screen"', "div")
    assert 'name="swap_id"' not in screen, (
        "the swap-id box is inside the two-column wrapper, so it is beside the question rather "
        "than under it"
    )
    assert body.index('<div class="atm-screen"') < body.index('name="swap_id"'), (
        "the swap-id box renders before the question it is an alternative to"
    )


def test_the_pair_directory_survived_the_move_and_is_still_complete(client):
    """All 30 directions, still listed, still collapsed, now in the right column.

    IT HAD TO GO SOMEWHERE AND DELETING IT WAS NOT AN OPTION.
    routes/atm.start()'s docstring records that it is the only answer to "what
    does this terminal do at all", and that it survived templates/index.html
    being deleted for that reason. A layout change that quietly dropped it would
    be a deletion wearing a reflow's clothes -- which is the failure
    tests/test_customer_page_layout.py's header is about.
    """
    body = client.get("/").get_data(as_text=True)
    allowed = client.application.config["ALLOWED_PAIRS"]
    aside = element(body, '<aside class="atm-aside"', "aside")

    tiles = re.findall(r'<li class="swaptile swaptile-([a-z]+)"[^>]*>', aside)
    assert len(tiles) == len(allowed), (
        f"{len(allowed)} directions are allowed and the right-hand column lists {len(tiles)}"
    )
    assert "<details>" in aside, (
        "the directory is no longer collapsed, so thirty rows are open beside a question whose "
        "whole premise is one thing per screen"
    )
    assert "What the markers mean" in aside, "the key moved away from the badges it explains"


def test_the_limit_line_prints_a_figure_the_chain_could_actually_hold(client):
    """No seventeen-digit floats on the screen a customer sizes a deposit against.

    MEASURED 2026-10-09 on this very flow: the desk holds 407.51074481 GRC and
    services/payout_capacity.largest_fundable_payout() subtracts a 0.001 reserve
    in float, so the ceiling line rendered

        ... that is all this desk can pay out on the other side (407.50974481000003 GRC)

    GRC has eight decimals. Five of those digits describe nothing that exists on
    any chain, and what a customer learns from them is that this terminal cannot
    count -- on the one line they are about to size a deposit against (rule 14:
    state what the number means, next to the number).

    THE GATE IS NOT TOUCHED AND THIS TEST SAYS SO. `ceiling` -- the figure an
    amount is actually refused against -- is already cut to the SOURCE chain's
    precision by max_deposit_for_capacity(), and re-rounding it here would be a
    second authority over a refusal. Only the parenthetical display figure goes
    through quantize_down().
    """
    body = post(client, from_asset="ICP", to_asset="GRC").get_data(as_text=True)
    limit = re.search(r'<p class="atm-limit[^"]*">(.*?)</p>', body, flags=re.DOTALL)
    assert limit, "the amount screen states no limit at all, which is the dead end it exists to remove"

    for figure in re.findall(r"\d+\.(\d+)", reading(limit.group(1))):
        assert len(figure) <= CHAIN_PRECISION["GRC"], (
            f"the limit line prints {figure!r} after the point -- more decimals than GRC's "
            f"{CHAIN_PRECISION['GRC']}, so it is showing float noise as if it were a balance. "
            f"The line reads: {reading(limit.group(1))!r}"
        )

    # AND THE FIGURE IS STILL THE RIGHT ONE, cut rather than replaced: a test that
    # only counted digits would pass against a line that had stopped printing the
    # capacity at all.
    assert "407.50974481" in reading(limit.group(1)), (
        "the GRC the desk can pay out is no longer on the line; the digit check above would pass "
        "for a screen that simply dropped it"
    )


def test_the_destination_screen_carries_no_second_column_at_all(client):
    """"next page will just be the buttons they want to covert into."

    Every piece of furniture absent, not just the fee table: a swap-id box or a
    pair directory drifting onto this screen is the same defect as the costs
    doing it.
    """
    body = post(client, from_asset="ICP").get_data(as_text=True)
    assert asking_body(body) == "What do you want back?"
    assert "atm-aside" not in body, "the destination screen grew a right-hand column"
    assert 'name="swap_id"' not in body
    assert "swaptile" not in body
    assert "What a swap costs" not in body


def test_the_amount_screen_keeps_the_fees_to_the_right_of_the_either_or(client):
    """"the either/or amount and get the quote and keep the fees to the right."

    All three clauses in one test because they are one sentence: the sides, the
    button, and the column.
    """
    body = post(client, from_asset="ICP", to_asset="GRC").get_data(as_text=True)
    assert asking_body(body) == "How much?"

    question = element(body, '<section class="panel atm"', "section")
    assert 'value="send"' in question and 'value="receive"' in question, "the either/or is gone"
    assert "Get the quote" in question, (
        "the amount screen's button does not say what the operator called it"
    )

    aside = element(body, '<aside class="atm-aside"', "aside")
    assert "Service fee" in aside and "Quote validity" in aside, "the fees are not to the right"
    # And the reference material does NOT follow mid-flow: a customer answering
    # "how much?" is not shopping for a pair.
    assert "swaptile" not in aside and 'name="swap_id"' not in body


def test_getting_the_quote_actually_produces_figures_on_the_next_screen(client):
    """A button called "Get the quote" must visibly produce one (rule 14).

    THE LABEL IS THE CLAIM AND THIS IS THE CHECK ON IT. No quotes row is written
    by that button -- create_quote() runs at the confirm button and this file's
    other tests pin that -- so what "get the quote" has to mean is that the
    figures appear. If they did not, the relabelling would be a louder word for
    the same silence.
    """
    body = post(client, from_asset="ICP", to_asset="GRC",
                amount="0.001", amount_side="send").get_data(as_text=True)
    assert asking_body(body) == "Where should it go?"

    aside = element(body, '<aside class="atm-aside"', "aside")
    assert "What this one comes to" in aside, "pressing 'Get the quote' showed no quote"
    assert "You send" in aside and "You receive" in aside, "only one leg of the trade is stated"
    assert "estimate" in aside, (
        "the figures are not called an estimate. No quote row exists yet and the rate is fixed at "
        "the confirm button, so a screen that implied a held price would be claiming something "
        "this flow does not do"
    )

    # The figures are the solvers', not markup: the receive leg must be what the
    # deposit actually buys at the fixture's rate.
    expected, refusal = payout_for_deposit(
        0.001, USD_PRICES["ICP_USD"] / USD_PRICES["GRC_USD"],
        int(client.application.config["DEFAULT_FEE_BPS"]), "GRC",
    )
    assert refusal == ""
    assert f"{expected} GRC" in reading(aside), f"the screen does not state {expected} GRC"


def test_no_amount_on_the_review_screen_is_in_scientific_notation(client):
    """THE DEFECT, END TO END, on the screen before "Create the swap".

    The operator's own 2026-10-09 render, walking BTC -> GRC for 1000 GRC:

        You send       8.061e-05 BTC
        You receive    1000.09376816 GRC

    Nobody reads `8.061e-05` as an amount of money, and it is the figure a
    customer is about to put into a wallet. Python's float str() switches to
    scientific notation below 1e-4, and these two rows were the only amount
    renderings in templates/ that interpolated a raw float -- measured the same
    day, 19 of the 23 use `'%.8f'|format` and cannot produce an exponent.

    ASSERTED ON THE RENDERED SCREEN, not on engineering_notation(). The formatter
    has its own sixteen tests in tests/test_engineering_notation.py and every one
    of them passes whether or not any template calls it. A mutation reverting
    either `| coin` to `{{ estimate.send }}` fails here and nowhere else, which
    is the behavioral-verification principle: run the real thing and assert on
    what it actually produced.
    """
    # 100 GRC, NOT 1000, AND THE FIGURE IS LOAD-BEARING. At these stub prices
    # (BTC_USD 62000 / GRC_USD 0.031 = 2,000,000 GRC per BTC) a 1000 GRC payout
    # solves to 0.00050762 BTC, which writes PLAINLY -- so the first version of
    # this test asserted "no scientific notation" on a screen that could never
    # have shown any, and the mutation reverting `| coin` passed it clean. The
    # boundary is 197 GRC; 100 GRC solves to 5.077e-05, which str() renders as
    # scientific notation, and it is inside the desk's 407.51074481 GRC balance.
    #
    # The guard below is what keeps that true: if the stub prices move, this test
    # fails LOUDLY instead of quietly going vacuous again.
    page = post(client, from_asset="BTC", to_asset="GRC", amount="100",
                amount_side="receive", payout_address=GRC_PAYOUT)
    assert asking(page) == "Is this right?"
    # BOTH PARTIALS, ON THE TWO SCREENS THAT CARRY THEM. They are different files
    # and a mutation reverting one passed a test that read only the other --
    # measured, this test was green with `| coin` removed from _atm_costs.html.
    #
    # And they are not on the same screen, which is the part that caught me out:
    # services/wizard._SCREEN_FURNITURE gives the REVIEW step `("estimate",)`
    # only, so step 5 has no <aside> at all and reading for one raised
    # ValueError. `costs` is on steps 1, 3 and 4, and step 4 is the one that also
    # has a solved estimate to print.
    body = page.get_data(as_text=True)
    figures = reading(element(body, '<dl class="atm-review"', "dl"))

    address_step = post(client, from_asset="BTC", to_asset="GRC", amount="100",
                        amount_side="receive")
    aside = element(address_step.get_data(as_text=True), '<aside class="atm-aside"', "aside")
    figures += "  " + reading(aside)

    rate = USD_PRICES["BTC_USD"] / USD_PRICES["GRC_USD"]
    solved, refusal = deposit_for_desired_payout(
        100.0, rate, int(client.application.config["DEFAULT_FEE_BPS"]), "BTC"
    )
    assert not refusal, refusal
    assert needs_engineering(solved), (
        f"this test is VACUOUS: the solved deposit {solved!r} writes plainly as {solved}, so "
        f"the screen would pass the assertions below with the formatter removed. Pick a smaller "
        f"payout, or the stub prices have moved."
    )

    # PYTHON'S OWN EXPONENT SPELLING, which is zero-padded to two digits --
    # `e-05`, not `e-5`. Searching for a bare "e-" would match the engineering
    # form this screen is now SUPPOSED to print and the test could never pass.
    scientific = re.findall(r"\d[eE][-+]0\d", figures)
    assert not scientific, (
        f"the review screen shows scientific notation to a customer: {scientific} in {figures}"
    )

    # And every exponent that IS shown is a multiple of three (the instruction).
    for exponent in re.findall(r"\d[eE]([-+]?\d+)", figures):
        assert int(exponent) % EXPONENT_STEP == 0, (
            f"exponent {exponent} on the review screen is not a multiple of {EXPONENT_STEP}: "
            f"{figures}"
        )


def test_the_deposit_figure_the_review_shows_is_the_solved_one_formatted(client):
    """The formatting must not change WHICH number is shown, only how.

    Guards the direction a display fix can go wrong: `| coin` rounding, or being
    applied to the wrong branch, would put a different figure in front of the
    customer than the solver produced -- which is worse than scientific
    notation, because it looks fine.

    Derived from the real solver rather than a literal, the same way
    test_a_customer_who_typed_what_they_want_is_told_what_to_send is.
    """
    rate = USD_PRICES["BTC_USD"] / USD_PRICES["GRC_USD"]
    fee_bps = int(client.application.config["DEFAULT_FEE_BPS"])
    solved, refusal = deposit_for_desired_payout(100.0, rate, fee_bps, "BTC")
    assert not refusal, refusal
    # SAME GUARD, SAME REASON as the test above. With a payout whose deposit
    # writes plainly, engineering_notation(solved) IS the plain string and this
    # test would pass against a template that never calls the formatter.
    assert needs_engineering(solved), (
        f"this test is VACUOUS: {solved!r} needs no exponent, so asserting its engineering form "
        f"is on screen asserts nothing about the formatter"
    )

    page = post(client, from_asset="BTC", to_asset="GRC", amount="100",
                amount_side="receive", payout_address=GRC_PAYOUT)
    review = element(page.get_data(as_text=True), '<dl class="atm-review"', "dl")
    assert engineering_notation(solved) in reading(review), (
        f"the screen does not show the solved deposit {solved!r} "
        f"(as {engineering_notation(solved)}): {reading(review)}"
    )


def test_a_customer_who_typed_what_they_want_is_told_what_to_send(client):
    """THE DEFECT, END TO END: a number where a sentence used to be.

    templates/_atm_confirm.html printed "&#8776; solved from what you want" in
    the "You send" row for a RECEIVE-side answer, and routes/atm._commit() solved
    the real deposit only after the confirm button -- so the one figure a
    customer has to put into a wallet first appeared on the swap page of a swap
    that already existed. They agreed to send an amount nobody had told them.

    Asserted against the solver rather than against a literal, so the test cannot
    pass by agreeing with a hard-coded number that drifted from the arithmetic.
    """
    page = post(client, from_asset="ICP", to_asset="GRC", amount="300", amount_side="receive",
                payout_address=GRC_PAYOUT)
    body = page.get_data(as_text=True)
    assert asking(page) == "Is this right?"

    review = element(body, '<dl class="atm-review"', "dl")
    assert "solved from what you want" not in review, (
        "the review still shows a phrase where the deposit amount goes"
    )
    rate = USD_PRICES["ICP_USD"] / USD_PRICES["GRC_USD"]
    fee_bps = int(client.application.config["DEFAULT_FEE_BPS"])
    deposit, refusal = deposit_for_desired_payout(300.0, rate, fee_bps, "ICP")
    assert refusal == ""
    assert f"{deposit} ICP" in reading(review), (
        f"the review does not state the {deposit} ICP deposit; it reads {reading(review)!r}"
    )


def test_what_the_review_promised_is_what_the_swap_row_records(client):
    """The screen and the row must carry the same two figures, not nearly the same.

    THE REASON THIS IS AN ASSERTION AND NOT A COMMENT. The displayed payout is
    computed by chains/amount_solve.payout_for_deposit() and the stored one by
    services/quote_service.create_quote(); they are two expressions for one
    relationship, which is rule 8's shape and already bit once in this change --
    `deposit * payout_multiplier(rate, fee)` and `(deposit * rate) * (1 - fee)`
    differ in the sixth decimal, measured, so the first version of that function
    showed a customer a number the row did not contain.

    Driven through the REAL handler and read back out of the REAL row, because
    that is the only comparison that covers both expressions at once.
    """
    review_body = post(client, from_asset="ICP", to_asset="GRC", amount="300",
                       amount_side="receive", payout_address=GRC_PAYOUT).get_data(as_text=True)
    review = element(review_body, '<dl class="atm-review"', "dl")

    created = post(client, from_asset="ICP", to_asset="GRC", amount="300", amount_side="receive",
                   payout_address=GRC_PAYOUT, confirmed="1")
    assert created.status_code == 302, created.get_data(as_text=True)[:400]

    conn = sqlite3.connect(client.application.config["DB_PATH"])
    conn.row_factory = dict_factory
    row = conn.execute(
        "SELECT expected_input_amount, output_amount_estimate FROM swaps"
    ).fetchone()
    conn.close()

    assert f"{row['expected_input_amount']} ICP" in reading(review), (
        f"the review promised a different deposit than the swap row's "
        f"{row['expected_input_amount']} ICP"
    )
    # THE PAYOUT IS COMPARED AT THE CHAIN'S OWN PRECISION, NOT DIGIT FOR DIGIT,
    # and the difference is a measurement rather than a loosened assertion.
    #
    # create_quote() stores output_amount_estimate UNQUANTIZED: this swap's row
    # holds 300.0000010967742 GRC, eight decimals more than Gridcoin can
    # represent. That is not a defect on the money path -- services/
    # payout_service.py runs chains/payout_quantization.quantize_for_chain()
    # before anything is broadcast, and that module exists precisely because an
    # over-precise figure handed to the GRC daemon is rounded half UP and sends a
    # satoshi more than intended. So the row keeps the raw estimate and the send
    # is cut later.
    #
    # The screen cuts it at display time instead, which is why the two strings
    # differ. What must hold is that they are the SAME NUMBER at the precision
    # the chain can actually pay, and that the screen never promises MORE than
    # the row -- which is still strong enough to catch the drift this change
    # already hit: `deposit * payout_multiplier()` and `(deposit * rate) * (1 -
    # fee)` differ by one unit in XRP's sixth decimal, exactly AT the
    # quantization step rather than below it.
    shown, refusal = quantize_down(row["output_amount_estimate"], "GRC")
    assert refusal == ""
    assert f"{shown} GRC" in reading(review), (
        f"the review promised a different payout than the swap row's "
        f"{row['output_amount_estimate']} GRC (cut to {shown}) -- the displayed and the stored "
        "expression have drifted, which is the whole hazard of having two of them"
    )
    assert shown <= row["output_amount_estimate"], (
        "the screen's payout is ABOVE the one the row records, so the terminal is promising more "
        "than it quoted"
    )


def test_a_price_feed_that_cannot_be_read_says_so_instead_of_printing_zeros(client, monkeypatch):
    """Rule 14: never a blank, and never a figure, where the feed did not answer.

    routes/atm._rate_hint() already degrades rather than 500ing -- that was a
    defect this file caught on 2026-10-07 -- and the estimate has to inherit the
    same property, because it is rendered on two screens that previously made no
    price call at all. A zero here would read as "the desk values your coin at
    nothing", which is a much worse statement than "we could not price it".
    """
    def dead_feed(_ttl=0):
        raise OSError("Tunnel connection failed: 403 Forbidden")

    monkeypatch.setattr(atm_module, "fetch_usd_prices", dead_feed)

    # BOTH SIDES, because they take DIFFERENT arms of the review's fallback and
    # only one of them can still show a number. A customer who typed the SEND
    # side already knows their deposit -- it is the figure they typed -- so the
    # screen keeps it. A customer who typed the RECEIVE side has no derivable
    # number at all, and that is the single state the old "solved when you
    # confirm" wording still exists for. Testing only the first would leave the
    # one arm that prints a sentence unexercised.
    for side, amount, still_shown in (("send", "0.001", "0.001 ICP"), ("receive", "300", "300 GRC")):
        page = post(client, from_asset="ICP", to_asset="GRC", amount=amount,
                    amount_side=side, payout_address=GRC_PAYOUT)
        body = page.get_data(as_text=True)
        assert page.status_code == 200, f"a dead price feed 500d the {side} side"
        assert asking(page) == "Is this right?", f"a dead feed moved the {side} side off the review"

        review = reading(element(body, '<dl class="atm-review"', "dl"))
        assert "0.0 GRC" not in review, f"the {side} side printed a payout of zero for a dead feed"
        assert "0.0 ICP" not in review, f"the {side} side printed a deposit of zero for a dead feed"
        assert "price feed could not be read" in body, (
            f"the {side} side shows no figures and does not say why"
        )
        # The half that IS still knowable stays on the screen: the figure they
        # typed. Dropping it would make a readable feed and a dead one look the
        # same from the customer's side, which is a worse answer than either.
        assert still_shown in review, f"the {side} side lost the figure the customer typed"

    # AND THE SENTENCE IS REACHED, on the one path that reaches it. Asserted
    # explicitly because a template arm nothing renders is a template arm nobody
    # notices has broken.
    receive_body = post(client, from_asset="ICP", to_asset="GRC", amount="300",
                        amount_side="receive", payout_address=GRC_PAYOUT).get_data(as_text=True)
    assert "solved when you confirm" in reading(element(receive_body, '<dl class="atm-review"', "dl")), (
        "a customer who typed what they want back, with no readable price, is shown a blank where "
        "the deposit amount goes -- which is ambiguous between zero and broken (rule 14)"
    )


def asking_body(body: str) -> str:
    """`asking()` for a body a caller already holds, so the <h1> is read once."""
    match = re.search(r'<h1 id="atm-question" class="atm-question">(.*?)</h1>', body)
    return match.group(1).strip() if match else "(no question heading rendered)"
