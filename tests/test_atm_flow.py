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
from chains.icp_account import account_identifier, subaccount_from_index
from db import SCHEMA, dict_factory
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
    assert "CUSTODIALLY" in body, "the settlement verdict's own word must reach the screen"
    assert "holds your funds" in body
    assert 'name="confirmed" value="1"' in body, "the commit button is on the review and nowhere else"


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
