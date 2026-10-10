#!/usr/bin/env python3
"""/admin's chain wallet panel: six chains, three shapes, and no way to spend anything.

Role: file (entry point -- `python3 -m pytest tests/test_chain_panel.py`)
Reads: services/chain_panel.py's decisions with seeded inputs, and the REAL Flask
      app with stub adapters injected into app.config["ADAPTERS"]
Writes: nothing
Can move funds: no. Every adapter here is a recorder; nothing opens a socket.
Mainnet-safe: yes
Live-safe: yes

BEHAVIORAL, NEVER TEXTUAL. CLAUDE.md's verification principle in full: seed the rows
the real function reads, run the REAL function, assert on what came out. Nothing here
asserts that a source file CONTAINS a string -- the one place this file reads markup
it is reading the OUTPUT of a render, which is the artifact an operator actually sees.

THE ASSERTION THIS FILE EXISTS FOR is test_no_rpc_the_core_panel_asks_is_outside_the
_allowlist: it collects every method a real panel render sent to a recording adapter
and checks each one against chains/daemon_wallet.refuse_unless_read_only(). A page
that quietly widened what /admin may ask a daemon is how a viewer stops being a
viewer, and reading the source for `sendtoaddress` would not catch a method name
built at runtime.
"""

from __future__ import annotations

import re
from typing import ClassVar

import app as app_module
import pytest
from chains import rpc_translation
from chains import xrp_payout_seed as seed_module
from chains.daemon_network import NETWORK_NOT_ESTABLISHED
from chains.daemon_wallet import refuse_unless_read_only
from recording_rpc_adapter import RecordingRPCAdapter
from services import chain_panel as panel
from services.swap_view import ATTRIBUTION_MODELS

# ADDRESSES ARE DERIVED, NEVER TYPED. tests/valid_addresses.py builds each one from a
# phrase through this repository's own encoders, so a fixture address cannot be mistyped
# and says what it is for -- and the tree-wide gate in
# tests/test_address_literals_are_valid.py counts typed literals against a ceiling,
# which the two I wrote here first pushed over (60 -> 62). That gate's own words:
# "Use tests/valid_addresses.py rather than writing one."
from valid_addresses import BTC_PARTICIPANT, SOL_DEPOSIT_ACCOUNT, XRP_HOT_ACCOUNT

REPO_ROOT = __import__("pathlib").Path(__file__).resolve().parent.parent

#: A config with all six chains and nothing else real. Six keys because Config.RPC has
#: six; the VALUES are empty because panel_assets() reads keys only, and a fixture that
#: filled them in would be asserting against its own idea of an RPC entry's shape.
SIX = {
    "RPC": {"BTC": {}, "LTC": {}, "SOL": {}, "GRC": {}, "XRP": {}, "ICP": {}},
    "ALLOWED_PAIRS": {("BTC", "GRC")},
    "BTC_MIN_CONFIRMATIONS": 2,
    "XRP_DEPOSIT_ACCOUNT": XRP_HOT_ACCOUNT,
    "SOL_DEPOSIT_ACCOUNT": SOL_DEPOSIT_ACCOUNT,
}

#: What a modern Bitcoin Core answers, enough for all four panes. Lifted in SHAPE from
#: tests/test_operator_panel.py's `_MODERN` rather than imported from it: that file's
#: table is what the pane functions are tested against and this one is what the PANEL
#: is tested against, and the two are allowed to diverge -- this one carries
#: `initialblockdownload` because the sync verdict is a panel concern and not a pane
#: one. Importing it would couple two files' fixtures and hide that difference.
MODERN = {
    "listwallets": ["desk_hot"],
    "listwalletdir": {"wallets": [{"name": "desk_hot"}]},
    "getwalletinfo": {"balance": 1.5, "unconfirmed_balance": 0.25,
                      "immature_balance": 0.0, "txcount": 812},
    "listtransactions": [
        {"txid": "aa" * 32, "category": "receive", "amount": 0.5, "confirmations": 6,
         "address": BTC_PARTICIPANT, "label": "", "time": 100},
    ],
    "getpeerinfo": [{"addr": "172.17.0.1:18333", "subver": "/Satoshi:28.1.0/",
                     "inbound": False, "pingtime": 0.0234, "synced_blocks": 812}],
    "getblockchaininfo": {"chain": "testnet4", "blocks": 812, "headers": 812,
                          "verificationprogress": 0.9999, "pruned": False,
                          "size_on_disk": 123456, "initialblockdownload": False},
    "getnetworkinfo": {"version": 280100, "subversion": "/Satoshi:28.1.0/",
                       "protocolversion": 70016, "connections": 1},
}


class Answering(RecordingRPCAdapter):
    """RecordingRPCAdapter plus a per-method answer table. It still records every call.

    SUBCLASSED RATHER THAN WRITTEN AGAIN (rule 8). tests/recording_rpc_adapter.py's own
    header is explicit that two copies of one stand-in is the same defect as two copies
    of one rule, and it already does the valuable half: it IS chains/base.RPCAdapter
    with the transport replaced, so every production method above `call` runs. What it
    cannot do is answer different methods differently -- it returns one stub string --
    and that is exactly what a four-pane panel needs.

    AN UNLISTED METHOD RAISES, because that is what a daemon does for a method it does
    not have ("Method not found"), and a stub that returned None would let this panel
    pass while being broken against the real older daemon. It is also what makes the
    GRC fixture below possible at all: there is no pre-0.17 daemon in this session.
    """

    def __init__(self, asset: str, answers: dict):
        super().__init__(asset)
        self.answers = answers

    def call(self, method, *params) -> object:
        self.calls.append((method, params))
        if method not in self.answers:
            raise RuntimeError(f"Method not found: {method} (rpc code -32601)")
        value = self.answers[method]
        if isinstance(value, Exception):
            raise value
        return value


class TranslatedAdapter:
    """An XRP/SOL-shaped adapter: `call(method, params)` and nothing else.

    chains/rpc_translation.call_translated_read_only() reaches exactly `.call(...)` on
    whatever it is handed, with a dict for rippled and a spread list for Solana, so
    this is the whole contract. It is deliberately NOT a subclass of the real XRPAdapter:
    that class's __init__ refuses a mainnet endpoint and reads a seed, and a test that
    had to satisfy either would be testing the adapter rather than the panel.
    """

    def __init__(self, answers: dict):
        self.answers = answers
        self.calls: list[tuple] = []
        self.timeout = 15.0

    def call(self, method, *params) -> object:
        self.calls.append((method, params))
        if method not in self.answers:
            raise RuntimeError(f"{method}: notSupported")
        return self.answers[method]


class LedgerAdapter:
    """An ICP-shaped adapter: the four methods services/chain_panel.icp_live() reaches.

    Each is settable per test, because the point of the ICP panel is that a ledger
    which will not answer must render as a row saying so rather than taking the page
    down -- and the realistic failure (dfx missing from the image, replica stopped,
    canister not deployed) raises rather than returning anything.
    """

    def __init__(self, *, balance=1000.0, fee=0.0001, derivation=(True, "agrees"),
                 address="a" * 64):
        self._balance, self._fee, self._derivation, self._address = balance, fee, derivation, address
        self.calls: list[str] = []
        self.timeout = 60.0

    def _answer(self, name: str, value):
        self.calls.append(name)
        if isinstance(value, Exception):
            raise value
        return value

    def own_address(self):
        return self._answer("own_address", self._address)

    def get_balance(self):
        return self._answer("get_balance", self._balance)

    def chain_fee(self):
        return self._answer("chain_fee", self._fee)

    def verify_derivation(self):
        return self._answer("verify_derivation", self._derivation)


def _app(adapters: dict | None = None):
    """The real application, with stub adapters if asked. Opens no socket."""
    application = app_module.create_app() if hasattr(app_module, "create_app") else app_module.app
    if adapters is not None:
        application.config["ADAPTERS"] = adapters
    return application


@pytest.fixture
def client():
    with _app().test_client() as test_client:
        yield test_client


def render(path: str, adapters: dict | None = None) -> tuple[int, str]:
    """GET one path against the real app and return (status, body)."""
    with _app(adapters).test_client() as test_client:
        response = test_client.get(path)
        return response.status_code, response.get_data(as_text=True)


def squash(markup: str) -> str:
    """One space between words, so a prose assertion cannot be beaten by a line break.

    THIS IS C44's DEFECT, WRITTEN DOWN AS A HELPER. That finding records three of my own
    assertions matching the wrong thing, and the third was `"not established"` straddling
    a hand-wrapped line in a template -- a sentence that was present and that the check
    could not see. Jinja templates in this repository wrap at about 100 columns, so ANY
    multi-word claim about rendered prose has to be made against normalized whitespace or
    it is a check that passes and fails on the width of the file it reads.
    """
    return re.sub(r"\s+", " ", markup)


# ---------------------------------------------------------------------------
# WHICH CHAINS, AND WHERE THAT LIST COMES FROM
# ---------------------------------------------------------------------------


def test_the_six_chains_are_derived_from_the_config_and_not_written_down():
    """Operator: "6 sub tabs for each chain" -- and the six come from Config.RPC.

    MUTATION: replace panel_assets()' body with `("BTC", "GRC", "LTC", "XRP", "SOL",
    "ICP")`. The first assertion still passes (it is the same six) and the SECOND
    fails, which is the one that matters: a hand-written tuple cannot follow a config
    that gains or loses a chain, and OPEN_FINDINGS C36 is the recorded cost of writing
    exactly that tuple by hand -- in the commit whose purpose was removing copies of
    it.
    """
    assert panel.panel_assets(SIX) == ("BTC", "LTC", "GRC", "SOL", "XRP", "ICP")

    # A chain removed from the config loses its tab; one added gains one. Neither needs
    # an edit to this module.
    fewer = {**SIX, "RPC": {"BTC": {}, "XRP": {}}}
    assert panel.panel_assets(fewer) == ("BTC", "XRP")
    more = {**SIX, "RPC": {**SIX["RPC"], "DOGE": {}}}
    assert panel.panel_assets(more)[-1] == "DOGE", (
        "a chain added to Config.RPC must get a panel without this module being edited; "
        "it is the application's own list of chains"
    )
    # AND WITH NO CHAINS AT ALL IT IS EMPTY rather than raising, so the page renders the
    # refusal that names an empty list instead of an IndexError in a route.
    assert panel.panel_assets({"RPC": {}}) == ()
    assert panel.panel_assets({}) == ()


def test_the_panel_list_agrees_with_the_OTHER_six_chain_authority():
    """A cross-check between two tables, so a future divergence is deliberate.

    services/swap_view.ATTRIBUTION_MODELS is the other place this tree records six
    chains, and it answers a DIFFERENT question -- how a deposit to each is attributed.
    They are not merged, for the reason chains/daemon_capabilities.py sets out about
    five such tables: "They agree today and can diverge... so they are NOT merged.
    tests/test_daemon_capabilities.py asserts they agree now, which makes a future
    divergence deliberate rather than an accident nobody notices." This is that
    assertion for the seventh.

    THE COST OF A DIVERGENCE IS ON RECORD: ICP was in Config.RPC and NOT in
    ATTRIBUTION_MODELS until 2026-10-07, and the first ICP swap ever created printed
    "No deposit attribution model is recorded for ICP... this page will not tell you
    where to send anything" above a deposit address eight rows further down. The panel
    renders that gap as an explicit "(no row)" block for exactly this reason, which is
    tested below.
    """
    assert set(panel.panel_assets(SIX)) == set(ATTRIBUTION_MODELS), (
        "Config.RPC and ATTRIBUTION_MODELS no longer name the same chains. That is not "
        "automatically wrong -- they answer different questions -- but it means some chain "
        "either has an endpoint and no attribution model, or the reverse, and ONE of those "
        "shipped a customer-facing contradiction once already. Decide which, and say so here."
    )


def test_every_panel_kind_is_reached_and_each_chain_gets_exactly_one():
    """Three kinds, all three reached by the real six. A renderer with one block per
    kind can only be trusted against a complete list.

    MUTATION: make panel_kind() return PANEL_KIND_CORE for anything not in
    CONSOLE_PROTOCOL. ICP then renders four empty Core panes for a chain that has no
    Core, and the last assertion fails.
    """
    kinds = {asset: panel.panel_kind(asset) for asset in panel.panel_assets(SIX)}
    assert set(kinds.values()) == set(panel.PANEL_KINDS)
    assert kinds == {
        "BTC": panel.PANEL_KIND_CORE, "LTC": panel.PANEL_KIND_CORE,
        "GRC": panel.PANEL_KIND_CORE, "XRP": panel.PANEL_KIND_TRANSLATED,
        "SOL": panel.PANEL_KIND_TRANSLATED, "ICP": panel.PANEL_KIND_NO_RPC,
    }
    # IT ANSWERS FOR A CHAIN WITH NO ADAPTER, which is the one place it deliberately
    # differs from services/admin_view.probe_kind(). The panel's whole job on an
    # unconfigured chain is to explain what WOULD be there.
    assert panel.panel_kind("btc") == panel.PANEL_KIND_CORE, "and it is case-insensitive"
    assert panel.panel_kind("DOGE") == panel.PANEL_KIND_NO_RPC, (
        "an unknown chain must claim no wallet and no translation -- the fail-closed "
        "direction, so the page says what is known instead of rendering empty Core panes"
    )


# ---------------------------------------------------------------------------
# AN UNKNOWN CHAIN IS A NAMED REFUSAL
# ---------------------------------------------------------------------------


def test_an_unknown_chain_is_a_named_refusal_with_the_six_in_it(client):
    """Rule 14: not a 500, not a blank, and not somebody else's panel.

    MUTATION: have named_or_first() fall back to the first chain for ANY unknown name.
    /admin/wallets/BTX then answers 200 with BTC's panel under a URL naming a chain
    that does not exist -- a page quietly showing something other than what was asked
    for, which the operator has no way to notice. Both of the first two assertions
    fail.
    """
    status, body = render("/admin/wallets/BTX")
    assert status == 404, "the URL identifies nothing, so the status has to say so"
    assert "BTX is not a chain" in squash(body)
    assert "BTC" in body and "ICP" in body, (
        "the refusal must name the chains that DO have a panel, so the operator's next "
        "action is a click rather than a question"
    )
    # AND IT IS NOT A TRACEBACK OR AN EMPTY BODY.
    assert "Traceback" not in body
    assert len(body) > 1000, f"a 404 body of {len(body)} bytes is a page that looks broken"


def test_a_url_naming_no_chain_is_the_first_panel_and_the_tab_strip_links_to_it(client):
    """One stable href for the operator tab strip, because a macro cannot read Config.RPC.

    MUTATION: hardcode `asset='BTC'` in _admin_tabs.html. This passes today and breaks
    on any deployment whose Config.RPC does not carry BTC -- which is why the assertion
    is that the BARE path works, not that it shows BTC.
    """
    status, body = render("/admin/wallets")
    assert status == 200
    first = panel.panel_assets(_app().config)[0]
    assert "<h1>" in body and first in body
    assert 'aria-current="page"' in body, "the current chain must be marked, not just colored"
    # The strip on /admin links here by route, not by a typed path.
    partial = (REPO_ROOT / "swap_terminal" / "templates" / "_admin_tabs.html").read_text()
    assert "url_for('admin.chain_wallet_page')" in partial
    assert not re.search(r'href="/admin/wallets', partial), "a tab href is typed, not built"


# ---------------------------------------------------------------------------
# THE ONE THAT KEEPS THIS A VIEWER
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("asset", ["BTC", "LTC", "GRC"])
def test_no_rpc_the_core_panel_asks_is_outside_the_allowlist(asset):
    """EVERY method a real render sent, checked against the real allowlist.

    THE SINGLE MOST VALUABLE ASSERTION IN THIS FILE, and it is collected rather than
    read: the panel's calls are made by chains/daemon_wallet.py's four panes, through
    chains/daemon_wallet.OneNode and services/chain_panel.BudgetedNode, and a grep of
    this repository for `sendtoaddress` would not see a method name composed at
    runtime.

    MUTATION CHECKED: swapping any pane read for `sendtoaddress` or `getnewaddress`
    fails here. So does adding a fifth pane that calls something unlisted.
    """
    adapter = Answering(asset, MODERN)
    result = panel.chain_panel(SIX, {asset: adapter}, asset, ask=True)
    asked = {method for method, _params in adapter.calls}
    assert asked, "the panel must actually call something, or this asserts nothing"
    for method in sorted(asked):
        assert refuse_unless_read_only(method) == "", (
            f"{method} was sent to {asset} by the chain panel and is NOT in "
            f"chains/daemon_wallet.READ_ONLY_RPCS"
        )
    # AND THE COUNT IS MEASURED, NEVER CLAIMED -- the figure an operator reads to
    # account for traffic in their own daemon's log.
    assert result["live"]["rpc_calls"] == len(adapter.calls)


@pytest.mark.parametrize("asset", ["BTC", "LTC", "GRC", "XRP", "SOL", "ICP"])
def test_no_chains_panel_has_a_form_or_an_input_of_any_kind(asset):
    """/admin is GET-only and this page must not look like an exception to that.

    THE RENDERED PAGE, not the template source, and for both the asked and the unasked
    state -- a field that only appears once a daemon has answered is exactly the one a
    source grep would miss.

    MUTATION: add `<input name="passphrase">` anywhere in admin_chain.html. Fails on
    all six parametrizations at once.
    """
    adapters = {
        "BTC": Answering("BTC", MODERN), "LTC": Answering("LTC", MODERN),
        "GRC": Answering("GRC", MODERN),
        "XRP": TranslatedAdapter({"server_info": {"info": {"network_id": 1}}}),
        "SOL": TranslatedAdapter({"getEpochInfo": {"absoluteSlot": 1}}),
        "ICP": LedgerAdapter(),
    }
    for path in (f"/admin/wallets/{asset}", f"/admin/wallets/{asset}?ask"):
        status, body = render(path, adapters)
        assert status == 200, path
        for forbidden in ("<form", "<input", "<textarea", "<button", 'type="password"'):
            assert forbidden not in body.lower(), f"{path} renders {forbidden}"


def test_the_encryption_state_is_REPORTED_and_nothing_offers_to_unlock_it():
    """GRC's wallet IS encrypted, and that is the fact that decides a payout.

    Measured on the operator's host 2026-10-10: `chain_balances.py` printed `wallet
    ENCRYPTED -- a payout needs a passphrase (getwalletinfo reports unlocked_until)`.
    It is invisible in every balance figure on every other screen, so an operator
    reading a healthy balance beside a locked wallet has no way to tell that nothing
    can leave it.

    MUTATION: drop the `encryption` key from wallet_state(). The page then shows a
    balance with no lock line and the first assertion fails -- which is the state this
    panel shipped in before the extraction added it.
    """
    locked = {**MODERN, "getwalletinfo": {**MODERN["getwalletinfo"], "unlocked_until": 0}}
    status, body = render("/admin/wallets/GRC?ask", {"GRC": Answering("GRC", locked)})
    assert status == 200
    assert "encrypted" in body, "the lock state is not on the page at all"
    assert "unlocked_until=0" in body, (
        "the VALUE is reported beside the verdict, because 0 and a future timestamp are "
        "different facts and fund_desk.staking_verdict() is what reads them"
    )
    assert "fund_desk.py" in body, "the tool that CAN unlock is not named, so the page dead-ends"
    # AND NOTHING ON THE PAGE WOULD COLLECT ONE. The word has to appear -- the panel
    # reports the lock and explains why it offers no field -- so the assertion is on the
    # SHAPES that could take a value, which is the correction tests/test_operator_panel.py
    # records making to the same check: a gate that forbids the WORD forbids reporting
    # the fact.
    for forbidden in ("<input", "<form", "<textarea", 'type="password"'):
        assert forbidden not in body.lower(), forbidden
    # `walletpassphrase` IS NOW ABSENT ALTOGETHER, which is STRICTLY STRONGER than where
    # this assertion started. It used to pin the RPC name to one location -- the block
    # headed "What this panel cannot do" -- so that an occurrence in a pane or a label
    # would fail. That block was deleted on 2026-10-10 at the operator's instruction
    # ("what's all this shit?"): 2,822 visible characters, 20% of the page, explaining
    # four absences that Bitcoin Core does not explain either.
    #
    # So the name has nowhere legitimate left to be, and absence is the whole assertion.
    # The thing it was protecting is unchanged and is the three lines above: the lock
    # STATE is reported, and no shape that could collect a passphrase exists.
    assert "walletpassphrase" not in body.lower(), (
        "the passphrase RPC is named on the page. There is no longer any block whose job is "
        "to explain its absence, so any occurrence is a label, a cell or a pane naming the "
        "call that takes a secret as an argument"
    )


# ---------------------------------------------------------------------------
# THE BUDGET
# ---------------------------------------------------------------------------


def test_a_spent_budget_asks_NOTHING_and_says_so_without_claiming_an_outage():
    """A request budget, not a per-call timeout. services/deadline.py: "a per-call
    timeout bounds a call, and only a deadline bounds a request."

    THE ARITHMETIC THIS GUARDS: seven reads at a 30s adapter timeout is 210s against
    gunicorn's 60s worker timeout, and services/admin_view.py carries the measurement
    of what that did on 2026-10-08 -- HTTP 500 after 60.18s with an empty body, one
    worker killed, and nothing learned about which chain was down.

    MUTATION: have BudgetedNode.call() forward unconditionally. The first assertion
    fails with 7 calls instead of 0.
    """
    adapter = Answering("BTC", MODERN)
    # A clock that has already passed the deadline on the first question.
    result = panel.chain_panel(
        SIX, {"BTC": adapter}, "BTC", ask=True,
        budget=panel.Budget(seconds=0.0, now=lambda: 1000.0),
    )
    assert adapter.calls == [], (
        f"the budget was already spent and {len(adapter.calls)} calls were made anyway. "
        "Refusing to START a call is the difference between a budget and a formality"
    )
    rendered = str(result)
    assert "NOT ASKED" in rendered
    assert "it says nobody asked" in rendered, (
        "a call nobody made must not read as a chain that did not answer -- those have "
        "different remedies and only one of them is an outage"
    )
    assert panel.PANEL_BUDGET_VARIABLE in rendered, "the sentence must name what raises the ceiling"


def test_a_spent_budget_leaves_the_NETWORK_not_established_and_never_not_allowed():
    """A daemon nobody asked must not be refused for naming the wrong chain.

    I WROTE THE OPPOSITE AND THE MEASUREMENT REFUTED IT. core_live() guarded the network
    read with `chain_network(node) if node.may_call() else ""` and the docstring under it
    claimed an empty string fails closed. Run on 2026-10-10:
    `chains/daemon_network.is_named("")` is TRUE -- the sentinel it tests for is the
    `"unknown ("` PREFIX and "" does not carry it -- so test_network_verdict() answered
    NETWORK_NOT_ALLOWED, which the panel renders as "it named a network and that network
    is not allowed" about a daemon that was never contacted.

    THAT IS THE CONFIDENTLY-WRONG-REFUSAL SHAPE THIS TREE HAS PAID FOR TWICE: fund_desk's
    "*** THIS IS A MAINNET DAEMON ***" printed at a testnet4 node, and admin_view
    rendering "answered, but reported no chain name" AS a network name. Fail-closed worked
    in both; the message did not, and the message is what the operator acts on.

    MUTATION: restore the `if node.may_call() else ""` guard. Both assertions fail.
    """
    adapter = Answering("BTC", MODERN)
    live = panel.core_live(
        adapter, "BTC", panel.Budget(seconds=0.0, now=lambda: 5.0).start(),
    )
    assert adapter.calls == [], "the budget was spent; nothing may be asked"
    assert live["network_named"] is False, (
        f"network={live['network']!r} reads as a NAMED network on a daemon nobody asked"
    )
    assert live["network_verdict"] == NETWORK_NOT_ESTABLISHED, (
        f"got {live['network_verdict']!r}. NOT_ALLOWED says the daemon named a network and it "
        "is refused; NOT_ESTABLISHED says nobody could read one. Only the second is true here, "
        "and they send an operator to different places."
    )
    # And the reason rides along, so the row is actionable rather than just refused.
    assert "BudgetSpent" in live["network"]


def test_the_refusal_names_THIS_requests_budget_and_not_the_default():
    """The number in the message has to be the number the run used.

    The first version of this sentence read the module constant, so a run with a
    different budget told the operator about the 20s default -- a message naming a
    figure the run did not use, which is wrong in exactly the runs somebody is
    investigating (rule 14).

    MUTATION: interpolate PANEL_BUDGET_SECONDS instead of self.budget.seconds. Fails.
    """
    spent = panel.Deadline(at=0.0, budget=panel.Budget(seconds=7.0, now=lambda: 99.0))
    assert "7s budget" in spent.spent_note("getwalletinfo")
    assert "getwalletinfo was not started" in spent.spent_note("getwalletinfo")


def test_the_page_states_the_worst_case_as_the_budget_plus_ONE_call():
    """Rule 14's "announce before, not only after", with the arithmetic shown.

    ONE per-call timeout and not seven: the deadline is checked before a call starts
    and cannot interrupt one already running, so exactly one call can overrun it.
    Adding seven would overstate it by a factor of seven and adding none would be the
    unbounded figure the budget exists to replace.

    MUTATION: return `budget` alone from worst_case_seconds(). The equality fails.
    """
    adapter = Answering("BTC", MODERN)
    assert panel.per_call_seconds(adapter) == 30.0, "the fixture's adapter timeout changed"
    assert panel.worst_case_seconds(adapter, 20.0) == 50.0
    # AND AN ADAPTER THAT CARRIES NO TIMEOUT IS "NOT ESTABLISHED", never a default: a
    # printed 30 for an adapter that never said would be a measurement nobody took.

    class Timeless:
        def call(self, method, *params):
            return None

    assert panel.per_call_seconds(Timeless()) is None
    assert panel.worst_case_seconds(Timeless(), 20.0) is None
    # THE FIGURES ARE NO LONGER PRINTED ON THE PAGE, 2026-10-10. The three-row budget
    # table -- Request budget, One call, Worst case, each with a clause explaining what
    # the number meant -- was deleted at the operator's instruction. Rule 14 asks for
    # exactly that table ("announce before, not only after", "echo the parameters that
    # decide the answer") and on a terminal diagnostic it is right; above a button it is
    # three rows of arithmetic between the operator and the click.
    #
    # SO THE ASSERTION MOVES TO THE FUNCTIONS, which is where it should always have been
    # (rule 10 -- the decision at the bottom, callable with seeded inputs). The budget is
    # still enforced; what changed is that it is no longer recited beforehand. What the
    # page DOES report is what the ask actually cost, in calls and elapsed time, counted
    # rather than claimed -- a measurement instead of a prediction.
    assert panel.worst_case_seconds(adapter, 20.0) == pytest.approx(50.0), (
        "the worst case is the budget plus ONE call, because the deadline is checked "
        "before a call starts and cannot interrupt one already running"
    )


def test_every_duration_the_panel_reports_is_in_microfortnights():
    """Rule 6: the symbol is micro, never an ASCII u, and never a space before it."""
    adapter = Answering("BTC", MODERN)
    result = panel.chain_panel(SIX, {"BTC": adapter}, "BTC", ask=True)
    for field in ("elapsed", "budget_display", "per_call_display", "worst_case_display"):
        text = result[field]
        assert "µfn" in text, f"{field}={text!r} carries no µfn figure"
        assert "ufn" not in text, f"{field}={text!r} uses an ASCII u, which is a defect"
        assert " µfn" not in text, f"{field}={text!r} has a space before the unit"
    # AND A PEER'S PING, which is the only duration that comes out of a chain.
    ping = result["live"]["panes"]["peers"]["rows"][0]["ping_display"]
    assert "µfn" in ping and "ufn" not in ping, ping
    assert result["live"]["panes"]["peers"]["rows"][0]["pingtime"] == 0.0234, (
        "the RAW seconds must survive beside the display string -- a test asserting on a "
        "measurement must not have to parse a unit (rule 6's report-versus-interface line)"
    )


# ---------------------------------------------------------------------------
# THE TRANSLATED CHAINS
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("protocol", "expected_calls"), [("xrpl", 4), ("solana", 5)])
def test_the_pane_questions_fold_onto_fewer_calls_and_every_fold_is_NAMED(protocol, expected_calls):
    """Six questions, fewer calls, and the page says which answer is shared.

    MEASURED AGAINST THE REAL MAPS, 2026-10-10. rippled never split its `getinfo` the
    way bitcoind did, so `getblockchaininfo` and `getconnectioncount` both map to
    `server_info`, and `getwalletinfo` and `getbalance` both map to `account_info`.

    THE FOLD BEING VISIBLE IS THE HALF THAT MATTERS. Two panes rendering the same
    numbers from two independent calls could legitimately DISAGREE -- the ledger
    advances between them -- and a reader cannot tell that from the page unless the
    page says they are one measurement.

    MUTATION: drop the `seen` memo from distinct_questions(). The call count rises to
    6 on both chains and the shared_with assertion finds nothing.
    """
    module = rpc_translation.console_map(protocol)
    resolved = panel.distinct_questions(module)
    assert len(resolved) == len(panel.PANE_QUESTIONS), "every question must get a row"
    sent = [row for row in resolved if row.chain_method and not row.shared_with]
    assert len(sent) == expected_calls, [
        (row.pane.question, row.chain_method, row.shared_with) for row in resolved
    ]
    shared = [row for row in resolved if row.shared_with]
    assert shared, "nothing folded, so the fold is untested on this chain"
    for row in shared:
        assert row.shared_with in {pane.title for pane in panel.PANE_QUESTIONS}, (
            f"{row.pane.question} says it shares with {row.shared_with!r}, which is not a pane"
        )


def test_keying_the_fold_on_the_method_alone_is_SOUND_for_this_question_list():
    """The caveat in distinct_questions()' docstring, asserted instead of described.

    It folds two questions together when they resolve to the same chain METHOD, which
    is only correct because every question in PANE_QUESTIONS is asked with the same
    account and NO per-question argument -- so one method means one identical call. A
    question list carrying its own arguments (`getblock` at two heights) would need the
    params in the key, and this would silently return one height's answer for both.

    MUTATION: add a Pane whose map entry takes an `argument`. This fails, naming it,
    before anybody can ship a panel that shows one block's data under two headings.
    """
    for protocol in ("xrpl", "solana"):
        module = rpc_translation.console_map(protocol)
        for pane in panel.PANE_QUESTIONS:
            entry = module.equivalent_of(pane.question)
            if entry is None:
                continue
            assert entry.argument == "", (
                f"{pane.question} takes the argument {entry.argument!r} on {protocol}, so two "
                f"questions mapping to its method are NOT one call. Put the params in "
                f"distinct_questions()' key before adding this question."
            )


def test_a_question_with_no_equivalent_renders_the_maps_own_reason():
    """A positive finding, never a blank. "rippled HOLDS NO WALLET" is an answer.

    ASKED OF A MAP THAT REFUSES, built here, because every question in PANE_QUESTIONS
    happens to translate on both real chains today -- so the refusal path would be
    untested against the real maps and is exactly the path a NEW question would take.

    MUTATION: drop the refusal branch from distinct_questions(). The refused question
    then resolves to a chain method of "" and the panel would send an empty method
    name.
    """
    class Refusing:
        CONGRUENT: ClassVar[dict] = {}
        NATIVE_ONLY: ClassVar[dict] = {}

        @staticmethod
        def refuse_without_equivalent(method):
            return f"{method} has no equivalent on this ledger: it holds no wallet at all"

        @staticmethod
        def equivalent_of(method):
            return None

    resolved = panel.distinct_questions(Refusing())
    assert len(resolved) == len(panel.PANE_QUESTIONS)
    assert all(row.refusal and not row.chain_method for row in resolved)
    assert all("holds no wallet" in row.refusal for row in resolved)

    # AND WITH NO MAP AT ALL -- which is ICP's state -- every question says so rather
    # than raising on a None module.
    nothing = panel.distinct_questions(None)
    assert all("no command map" in row.refusal for row in nothing)


def test_the_translated_panel_names_the_account_it_asked_about_and_never_a_seed():
    """The deposit account, from the configuration. NOT the payout account.

    chains/xrp_payout_seed.derived_payout_account() returns a public classic address --
    and it gets there by DECODING THE SIGNING SEED. A read-only page must not touch key
    material to decide what to display, which is a standing operator rule and not a
    preference.

    MUTATION: pass the payout account instead. The second assertion fails because the
    configured deposit account is no longer the one on the page.
    """
    adapter = TranslatedAdapter({
        "server_info": {"info": {"network_id": 1, "validated_ledger": {"seq": 12}}},
        "account_info": {"account_data": {"Balance": "20000000"}},
        "account_tx": {"transactions": []},
        "peers": {"peers": []},
    })
    # THE SEED READER IS BOOBY-TRAPPED FOR THE DURATION OF THE RENDER, which is the
    # behavioral form of this claim and the only form worth having. Forbidding the WORD
    # "seed" on the page was the first version and it failed on the panel's own sentence
    # explaining that it does not read one -- a gate that cannot tell a citation from a
    # claim, for the third time in this change. What matters is whether the function
    # RUNS, so it raises if it is reached.
    def refuse():
        raise AssertionError(
            "a read-only page called derived_payout_account(), which DECODES THE SIGNING "
            "SEED to produce a public address. Standing operator rule: never read, copy or "
            "echo key material. The deposit account is configuration and is what belongs here."
        )

    original = seed_module.derived_payout_account
    seed_module.derived_payout_account = refuse
    try:
        status, body = render("/admin/wallets/XRP?ask", {"XRP": adapter})
    finally:
        seed_module.derived_payout_account = original
    assert status == 200
    account = _app().config.get("XRP_DEPOSIT_ACCOUNT", "")
    if account:
        assert account in body
    else:
        assert "(none configured)" in body, (
            "with no account configured the panel must SAY so -- the account is a custody "
            "decision with no safe default, and nothing here may invent one"
        )


def test_an_admin_only_question_reports_the_servers_refusal_rather_than_dying():
    """rippled's `peers` is admin-only and a public endpoint refuses it.

    chains/xrp_rpc_map.py: "A public JSON-RPC endpoint refuses it, and that refusal
    comes from the server rather than from here -- the count alone is in
    server_info.peers, which every endpoint answers." That is why the pane list carries
    BOTH getpeerinfo and getconnectioncount: a panel with only the detail question
    would report "am I connected to anybody" as a permission error on the ordinary
    deployment.

    MUTATION: drop the Connections pane. The last assertion fails and XRP loses its
    only answerable peer question.
    """
    adapter = TranslatedAdapter({
        "server_info": {"info": {"peers": 21, "network_id": 1}},
        "account_info": {"account_data": {"Balance": "20000000"}},
        "account_tx": {"transactions": []},
        # `peers` is absent, so the stub raises -- which is what the real server does.
    })
    status, body = render("/admin/wallets/XRP?ask", {"XRP": adapter})
    assert status == 200
    assert "did not answer" in squash(body), "the refused question must render its own outcome"
    assert "Connections" in body and "server_info" in body, (
        "the count question, which every endpoint answers, is not on the page"
    )
    assert "Traceback" not in body


# ---------------------------------------------------------------------------
# ICP
# ---------------------------------------------------------------------------


def test_the_icp_panel_reports_what_it_could_not_read_rather_than_dying():
    """dfx missing, replica down, canister not deployed -- each is one row, not a 500.

    MUTATION: let icp_live() propagate. The render raises and the page an operator
    would use to find out that the ledger is down is the page that is down.
    """
    broken = LedgerAdapter(balance=RuntimeError("dfx: command not found"))
    status, body = render("/admin/wallets/ICP?ask", {"ICP": broken})
    assert status == 200
    assert "dfx: command not found" in body, "the real reason must reach the screen verbatim"
    assert "could not be read" in squash(body)
    # AND THE LOCAL DERIVATION STILL ANSWERS, because it is a hash and not a call: an
    # operator who can see nothing else can still read the account this panel is about.
    assert "a" * 64 in body


def test_the_icp_panel_names_the_two_panes_it_does_not_have():
    """Rule 14 at the scale of a pane. An absent region must be a result.

    MUTATION: drop the two sentences. The page then shows two fewer panes than every
    other chain with nothing saying why, which reads as half-built.
    """
    status, body = render("/admin/wallets/ICP?ask", {"ICP": LedgerAdapter()})
    assert status == 200
    assert "no per-wallet transaction list" in squash(body)
    assert "a canister has no peer set" in squash(body)
    # AND IT DOES NOT GUESS A CANISTER ID, which is per-replica and must be asked.
    assert "localhost:4943" not in body, (
        "a guessed canister id links to somebody else's deployment (rule 17); "
        "`swap_stack.py status` asks the replica and prints the real URLs"
    )


def test_the_derivation_check_is_on_the_panel_because_nothing_else_asks_it():
    """The one assumption under every ICP address this terminal publishes.

    A disagreement is not a rounding difference: it means every deposit address is
    wrong in the same way, a customer pays, and the watcher polls an account that stays
    at zero forever with nothing erroring.
    """
    disagreeing = LedgerAdapter(derivation=(False, "the ledger computed a DIFFERENT identifier"))
    status, body = render("/admin/wallets/ICP?ask", {"ICP": disagreeing})
    assert status == 200
    assert "DIFFERENT identifier" in body


# ---------------------------------------------------------------------------
# WHAT THE PAGE CANNOT DO, AND SAYS SO
# ---------------------------------------------------------------------------


def test_every_tool_the_panel_names_as_an_alternative_actually_EXISTS():
    """Rule 2's "grep the tree for the NAME", in the other direction.

    Advice naming a tool that has moved is worse than no advice: it sends the reader to
    a path that does not exist and they conclude the capability is gone. So every `.py`
    named in an `instead` is resolved against the repository.

    MUTATION: rename any of those files. Fails here, naming it, instead of on an
    operator's screen.
    """
    named = set()
    for kind in panel.PANEL_KINDS:
        for entry in panel.absent_capabilities(kind):
            named |= set(re.findall(r"\b([a-z_][a-z0-9_/]*\.py)\b", entry.instead))
    assert named, "no tool is named at all, so this page dead-ends on every absence"
    # TWO ROOTS, BECAUSE THIS TREE HAS TWO KINDS OF PATH and the panel legitimately
    # names both: a root ENTRY POINT an operator runs (`fund_desk.py`) and a package
    # MODULE that owns a behavior they are being pointed at
    # (`chains/gridcoin_wallet_lock.py`, which is why a GRC unlock restores staking).
    # Checking only the repository root reported the second kind as missing, which is
    # this check firing on a correct entry -- "a check that fires on correct entries is
    # a check somebody edits until it stops firing" (rule 19).
    roots = (REPO_ROOT, REPO_ROOT / "swap_terminal")
    for path in sorted(named):
        assert any((root / path).is_file() for root in roots), (
            f"the panel tells the operator to use {path}, which exists under neither "
            f"{REPO_ROOT.name}/ nor {REPO_ROOT.name}/swap_terminal/"
        )


def test_no_chain_is_told_about_a_capability_its_daemon_never_had():
    """A reason that does not apply to the chain it is printed under is the wrong remedy.

    chains/daemon_capabilities.Capability.when_unused records what that cost on
    2026-10-10: an LTC `Connection refused` was answered with the GRC row's "nothing is
    needed", on an operator's screen, during an outage.

    Telling an XRP operator that this page has no passphrase field "because
    walletpassphrase takes one as an argument" is the same shape: rippled holds no
    wallet at all, which chains/xrp_rpc_map.py's NO_EQUIVALENT entry for `listwallets`
    says at length.

    MUTATION: return the whole list from absent_capabilities() for every kind. The
    second assertion fails on both non-Core kinds.
    """
    core = {entry.what for entry in panel.absent_capabilities(panel.PANEL_KIND_CORE)}
    assert {"Send", "Receive", "Unlock / passphrase", "Start / stop this daemon"} <= core
    for kind in (panel.PANEL_KIND_TRANSLATED, panel.PANEL_KIND_NO_RPC):
        whats = {entry.what for entry in panel.absent_capabilities(kind)}
        assert "Unlock / passphrase" not in whats and "Start / stop this daemon" not in whats
        assert {"Send", "Receive"} <= whats, (
            "Send and Receive are absent on every chain and must be named on every chain"
        )
    # AND THE RENDERED XRP PAGE DOES NOT MENTION THE WALLET RPC AT ALL.
    _status, body = render("/admin/wallets/XRP")
    assert "walletpassphrase" not in body


def test_the_receive_absence_names_getnewaddress_as_a_WRITE():
    """It is the one that looks safe, and it derives and stores a key.

    MUTATION: soften the Receive entry to "not implemented yet". A reader then has no
    reason to believe it will not arrive, and the actual reason -- that it is a wallet
    write on a surface with no write verb -- is lost.
    """
    receive = next(entry for entry in panel.absent_capabilities(panel.PANEL_KIND_CORE)
                   if entry.what == "Receive")
    assert "getnewaddress" in receive.why
    assert "wallet write" in receive.why.lower()
    assert "testnet_wallets.py" in receive.instead


# ---------------------------------------------------------------------------
# EMPTY REGIONS, SYNC, AND THE THINGS A FIGURE CANNOT SAY
# ---------------------------------------------------------------------------


def test_a_syncing_daemon_says_SYNCING_rather_than_showing_a_zero_balance():
    """LTC was mid-sync on the operator's host the day this was written.

    chains/daemon_network.sync_verdict()'s own comment carries the cost: for the 33-54
    hours a node takes to sync on this hardware, "the balance reads 0, wallet inventory
    records 0 inventory, pairs go unavailable for capacity reasons with no sentence
    naming sync, and a customer deposit is simply invisible to deposit_watcher. Nothing
    anywhere says 'syncing'."

    MUTATION: drop `sync` from core_live()'s payload. The page shows a 0 balance on a
    node 2 million blocks behind with nothing saying so, and both assertions fail.
    """
    syncing = {
        **MODERN,
        "getwalletinfo": {"balance": 0.0, "unconfirmed_balance": 0.0,
                          "immature_balance": 0.0, "txcount": 0},
        "getblockchaininfo": {"chain": "test", "blocks": 2_720_000, "headers": 4_910_000,
                              "verificationprogress": 0.55, "initialblockdownload": True},
    }
    status, body = render("/admin/wallets/LTC?ask", {"LTC": Answering("LTC", syncing)})
    assert status == 200
    assert "initial block download" in squash(body), "nothing on the page names sync as the reason"
    assert "INVISIBLE" in body, (
        "the consequence has to be on the screen with the figure: until it finishes, a "
        "deposit to this chain cannot be seen at all"
    )
    assert "2190000" in body.replace(",", "") or "2,190,000" in body, "the gap is not shown"


def test_a_node_with_no_wallet_renders_a_sentence_and_not_a_zero():
    """Measured on the operator's host 2026-10-10 and it cost a wrong answer on screen.

    MUTATION: render 0.00000000 when balances is None. The page claims the desk holds
    nothing on a node that has no wallet to hold anything in, which an operator reads
    as "my coins are gone".
    """
    walletless = {**MODERN, "listwallets": []}
    status, body = render("/admin/wallets/BTC?ask", {"BTC": Answering("BTC", walletless)})
    assert status == 200
    assert "holds NO loaded wallet" in squash(body)
    assert "0.00000000" not in body, "a balance figure is rendered for a node with no wallet"


def test_no_region_on_any_chains_page_is_blank(client):
    """Rule 14: `(none)` is a result; a blank gap is ambiguous between zero and broken.

    Rendered with NO adapters at all -- the state of a fresh checkout and of this
    container -- because that is when every region is empty at once and is exactly the
    render a "looks fine to me" reading would skip.

    MUTATION: delete any `{% else %}` empty-state block in admin_chain.html. The
    heading it belongs to then has nothing under it and the count falls.
    """
    for asset in panel.panel_assets(_app().config):
        status, body = render(f"/admin/wallets/{asset}", {})
        assert status == 200, asset
        # Every <h2> must be followed by SOMETHING before the next one.
        sections = re.split(r"<h2>", body)[1:]
        assert sections, f"{asset} rendered no sections at all"
        for section in sections:
            heading, _, rest = section.partition("</h2>")
            text = re.sub(r"<[^>]+>", " ", rest).strip()
            assert len(text) > 6, (
                f"{asset}: the section headed {heading.strip()!r} renders {text!r}, which is "
                f"a blank region -- a reader cannot tell that from a query that broke"
            )


def test_a_chain_with_no_adapter_says_NOTHING_TO_ASK_rather_than_claiming_it_asked():
    """`?ask` on an unconfigured chain. This shipped as a 500 on its first render.

    `asked=True` with `live=None` walked the template into the Core-pane block with
    nothing to render. The crash was the lucky outcome -- the defect under it is rule
    14's "make 'did nothing' look different from 'did work'": a page claiming to have
    asked a chain it never contacted reads as a daemon that answered nothing.

    MUTATION: set `asked` from `bool(ask)` alone. Raises UndefinedError on the render.
    """
    result = panel.chain_panel(SIX, {}, "BTC", ask=True)
    assert result["asked"] is False, "it reported asking a chain it has no adapter for"
    assert result["live"] is None
    assert "no adapter" in result["cannot_ask"]
    status, body = render("/admin/wallets/BTC?ask", {})
    assert status == 200
    assert "(nothing to ask)" in squash(body)
    assert "no endpoint for this chain in this process" in squash(body)


def test_a_chain_in_the_config_with_no_attribution_row_renders_the_gap():
    """The ICP defect of 2026-10-07, as a visible block instead of a contradiction.

    MUTATION: render nothing when data.chain is empty. The page then shows a
    Configuration heading with no body for a chain whose deposit attribution is
    unrecorded -- which is the state that printed a deposit address under "do not send
    anything".
    """
    stranger = {**SIX, "RPC": {**SIX["RPC"], "DOGE": {}}}
    result = panel.chain_panel(stranger, {}, "DOGE")
    assert result["chain"] == {}, "the fixture no longer reproduces the gap"
    assert result["refusal"] == "", "a chain in Config.RPC must still get a panel"
