#!/usr/bin/env python3
"""The chain panel's Qt arrangement: the rail, the card, Qt's columns, the status bar.

Role: file (entry point -- `python3 -m pytest tests/test_chain_wallet_layout.py`)
Reads: services/chain_wallet_layout.py's decisions with seeded inputs, and the REAL
      Flask app with stub adapters injected into app.config["ADAPTERS"]
Writes: nothing
Can move funds: no. Every adapter here is a stand-in that answers from a dict;
      nothing opens a socket, and the module under test makes no call of any kind.
Mainnet-safe: yes
Live-safe: yes

BEHAVIORAL, NEVER TEXTUAL, which on a change whose whole subject is an ARRANGEMENT is
the distinction that decides whether this file is worth anything. Nothing here asserts
that a source file contains a string. Where this file reads markup it is reading the
OUTPUT of a render through the real app -- the artifact an operator actually sees -- and
CLAUDE.md is explicit that "the code contains a check for X" is not evidence X is
enforced.

THE TWO ASSERTIONS THIS FILE EXISTS FOR, and they are the two ways a borrowed layout
goes wrong silently:

  test_every_rail_entry_jumps_to_a_section_that_EXISTS
      A rail is a list of links, and a link to a fragment no element carries does
      NOTHING when clicked -- no error, no console message, no visual change. It is
      invisible until an operator tries it and concludes the page is broken. This walks
      every entry of every chain's rail, in both the asked and the unasked state,
      against the real render, in BOTH directions: a fixed section that loses its id
      fails, and a generated section nothing links to fails.

  test_nothing_on_any_chains_panel_is_HIDDEN
      templates/admin.html's measurement is that a tab mechanism makes a browser copy
      return 7.4% of the page with none of the headings, and this operator reads these
      pages by pasting them back. The rail is the shape that most invites somebody to
      make it real tabs, so the absence of hiding is asserted over the rendered markup
      AND over the stylesheet the page loads.

MUTATION NOTES ARE ON EVERY TEST. Each says what to break to make it fail, because a
test whose failure mode nobody has checked is a test that might assert nothing -- and
this file's subject is a layout, where "it still renders" is the easiest false pass in
the suite.
"""

from __future__ import annotations

import html
import re
from pathlib import Path

import app as app_module
import pytest
from chains.daemon_wallet import (
    LOCK_ENCRYPTED,
    LOCK_NOT_ENCRYPTED,
    LOCK_NOT_ESTABLISHED,
    LOCK_STATES,
)
from chains.solana_units import COMMITMENT_RANKS, DISCOVERY_COMMITMENT, FINALIZED_RANK
from services import chain_panel as panel
from services import chain_wallet_layout as layout

# THE STAND-INS COME FROM tests/test_chain_panel.py AND ARE NOT COPIED (rule 8).
# tests/recording_rpc_adapter.py's own header: two copies of one stand-in is the same
# defect as two copies of one rule. Thirteen other files in this suite import a stub
# from a sibling test module, so this is the established pattern here rather than a
# novelty.
#
# THE RESPONSE TABLES BELOW ARE THIS FILE'S OWN, and that is the opposite call for the
# opposite reason -- the same one test_chain_panel.py records when it lifted
# test_operator_panel.py's `_MODERN` in SHAPE rather than importing it. A fixture table
# is what each file asserts AGAINST, and these two are allowed to diverge: that one's
# table is what the PANEL is tested against, and this one's carries a deliberately
# incomplete balance set and a deliberately truncated server_info, because the absences
# are what this file is about. Importing it would couple the two and hide the
# difference.
from test_chain_panel import Answering, LedgerAdapter, TranslatedAdapter

# THE TWO SHARED DEPOSIT ACCOUNTS, IMPORTED RATHER THAN TYPED. Three of XRP's and SOL's
# six pane questions need an account, and without one they come back `needs=True` with
# the variable named and NO CALL MADE -- which is correct behavior (the account is a
# custody decision with no safe default) and means a figure table has nothing to read.
# tests/test_web_surfaces.py already owns this pair, and
# tests/test_address_literals_are_valid.py counts typed address literals against a
# ceiling, so writing two more here would push a gate that is already at its limit.
from test_web_surfaces import DEPOSIT_ACCOUNTS

REPO_ROOT = Path(__file__).resolve().parent.parent

#: All six chains, with values empty because panel_assets() reads Config.RPC's KEYS.
SIX_CHAINS = ("BTC", "LTC", "GRC", "XRP", "SOL", "ICP")

#: A modern Bitcoin Core, complete enough for all four panes AND for Qt's Total row:
#: every one of the three balance fields is reported, so the card's Total is a real sum.
#: `time` is present because Qt's first transaction column is a Date and the panel this
#: replaced had no date column at all.
CORE_COMPLETE = {
    "listwallets": ["desk_hot"],
    "listwalletdir": {"wallets": [{"name": "desk_hot"}]},
    "getwalletinfo": {"balance": 1.5, "unconfirmed_balance": 0.25,
                      "immature_balance": 0.125, "txcount": 812},
    "listtransactions": [
        {"txid": "aa" * 32, "category": "receive", "amount": 0.5, "confirmations": 6,
         "address": "mg" + "1" * 32, "label": "a swap deposit", "time": 1_700_000_000},
        {"txid": "bb" * 32, "category": "generate", "amount": 25.0, "confirmations": 0,
         "address": "", "label": "", "time": 1_700_000_500},
    ],
    "getpeerinfo": [{"addr": "172.17.0.1:18333", "subver": "/Satoshi:28.1.0/",
                     "inbound": False, "pingtime": 0.0234, "synced_blocks": 812}],
    "getblockchaininfo": {"chain": "testnet4", "blocks": 812, "headers": 812,
                          "verificationprogress": 0.9999, "pruned": False,
                          "size_on_disk": 123456, "initialblockdownload": False},
    "getnetworkinfo": {"version": 280100, "subversion": "/Satoshi:28.1.0/",
                       "protocolversion": 70016, "connections": 1},
}

#: THE SAME DAEMON WITH `immature_balance` ABSENT, which is the case Qt's Total row has
#: to refuse. A build that does not carry the field has not told us it is zero, and a
#: sum over the two it did report would be a confident number wrong by an unknown
#: amount. This is the getinfo-fallback shape in miniature.
CORE_PARTIAL = {
    **CORE_COMPLETE,
    "getwalletinfo": {"balance": 1.5, "unconfirmed_balance": 0.25, "txcount": 812},
}

#: A rippled that answers both of the panel's two folded calls, with the fields
#: chains/xrp.py itself reads: info.validated_ledger.{seq,reserve_base_xrp,
#: reserve_inc_xrp}, info.peers, account_data.{Balance,OwnerCount}.
XRP_ANSWERING = {
    "server_info": {"info": {"peers": 7, "server_state": "full", "build_version": "3.4.1",
                             "validated_ledger": {"seq": 98765, "reserve_base_xrp": 1,
                                                  "reserve_inc_xrp": 0.2}}},
    "account_info": {"account_data": {"Balance": "25000000", "OwnerCount": 2, "Sequence": 11}},
    "account_tx": {"transactions": []},
}

#: THE SAME SERVER WITH `validated_ledger` GONE. Not a hypothetical: it is absent until
#: a rippled has a validated ledger, and chains/xrp.py raises rather than falling back
#: when it is missing. Here it is the response-shape change that must render as a named
#: absence rather than as a plausible figure.
XRP_NO_LEDGER = {
    **XRP_ANSWERING,
    "server_info": {"info": {"peers": 7, "server_state": "connected",
                             "build_version": "3.4.1"}},
}

SOL_ANSWERING = {
    "getEpochInfo": {"absoluteSlot": 123_456, "blockHeight": 123_000, "epoch": 4},
    "getBalance": {"value": 650_240},
    "getClusterNodes": [{"pubkey": "a"}, {"pubkey": "b"}, {"pubkey": "c"}],
    "getAccountInfo": {"value": None},
    "getSignaturesForAddress": [],
}


class Accounted(TranslatedAdapter):
    """TranslatedAdapter plus the one method a CONFIGURED deposit account makes reachable.

    SUBCLASSED RATHER THAN WRITTEN AGAIN (rule 8), the same way test_chain_panel.py's
    `Answering` extends tests/recording_rpc_adapter.py: that file's header is explicit
    that two copies of one stand-in is the same defect as two copies of one rule.

    WHY THE EXTRA METHOD IS NEEDED AT ALL, measured rather than guessed. Setting
    XRP_DEPOSIT_ACCOUNT takes services/admin_view.chain_rows() down a branch it does not
    reach on an unconfigured terminal:

        chain_panel -> _chain_row -> admin_view.chain_rows
                    -> swap_service.why_cannot_take_deposits
                    -> swap_service.deposit_address_for_swap  (line 386)
                    -> adapters[asset].validate_address(account)

    That check exists because a tag is never reused: allocating one against an invalid
    account would burn it permanently for a swap that cannot exist. A bare
    TranslatedAdapter has no such method and the render raised AttributeError -- which
    is the right failure for a stub that was standing in for less than the real adapter.

    IT ANSWERS TRUE AND NOTHING HERE ASSERTS ON THAT. Whether an account is valid is
    chains/xrp_address.py's and chains/solana_address.py's question and
    tests/test_address_validation.py's subject; this file is about the arrangement, and a
    stub that refused would test the refusal path instead of the layout.
    """

    def __init__(self, answers: dict):
        super().__init__(answers)
        #: Recorded APART FROM `calls`, which this file reads as "what went over the
        #: wire". The real XRPAdapter.validate_address() and the Solana one both decode
        #: LOCALLY -- chains/xrp_address.py and chains/solana_address.py -- so it is not
        #: a round trip and must not count against
        #: test_the_arrangement_adds_NO_chain_call. Appending it to `calls` made that
        #: test fail for a reason that was not a call.
        self.validated: list[str] = []

    def call(self, method, *params) -> object:
        """Like TranslatedAdapter's, except that an Exception VALUE is RAISED.

        A STUB THAT CANNOT EXPRESS "THE CHAIN REFUSED" MAKES THE REFUSAL TEST PASS ON
        THE WRONG PATH, and mine did. TranslatedAdapter returns `self.answers[method]`
        whatever it is, so seeding `{"server_info": RuntimeError("rippled: noNetwork")}`
        handed the panel the exception OBJECT as a successful result. dig() then walked
        into it, found no `info` key, and the page reported "the server_info call
        ANSWERED and its reply does not carry 'info.peers'" -- a response-shape finding
        about a call that in reality had failed.

        AND THE FIRST VERSION OF THE TEST PASSED ANYWAY, which is the part worth
        keeping. It asserted `"noNetwork" in body`, and the string WAS there: the pane
        renders the raw result, and `str(RuntimeError("rippled: noNetwork"))` contains
        it. A substring check over a whole page cannot tell which region put it there,
        and the assertion it replaced now reads the status cells' own notes.

        tests/test_chain_panel.py's `Answering` already raises an Exception value for
        the same reason -- its docstring: "AN UNLISTED METHOD RAISES, because that is
        what a daemon does for a method it does not have ... a stub that returned None
        would let this panel pass while being broken against the real older daemon."
        This is that rule applied to a LISTED method whose listed answer is a failure.
        """
        if isinstance(self.answers.get(method), Exception):
            self.calls.append((method, params))
            raise self.answers[method]
        return super().call(method, *params)

    def validate_address(self, address: str) -> bool:
        self.validated.append(address)
        return True


def _adapters() -> dict:
    """One stub per chain, each answering from its own table. Opens no socket."""
    return {
        "BTC": Answering("BTC", CORE_COMPLETE),
        "LTC": Answering("LTC", CORE_COMPLETE),
        "GRC": Answering("GRC", CORE_COMPLETE),
        "XRP": Accounted(XRP_ANSWERING),
        "SOL": Accounted(SOL_ANSWERING),
        "ICP": LedgerAdapter(),
    }


def _app(adapters: dict | None = None):
    """The real application, with stub adapters and the shared deposit accounts set.

    THE ACCOUNTS ARE SET HERE AND NOT PER TEST, because without them XRP's and SOL's
    account-bearing questions are never ASKED -- services/chain_panel.translated_live()
    gets an empty account, call_for() raises MissingArgument, and the answer comes back
    `needs=True` with the variable named. That is the correct outcome for a terminal
    with no account configured, and it means every figure on those two chains reads as
    "not asked", which would make this file's figure assertions pass for the wrong
    reason on a render that measured nothing.
    """
    application = app_module.create_app() if hasattr(app_module, "create_app") else app_module.app
    if adapters is not None:
        application.config["ADAPTERS"] = adapters
    for variable, account in DEPOSIT_ACCOUNTS.items():
        application.config[variable] = account
    return application


def render(path: str, adapters: dict | None = None) -> tuple[int, str]:
    """GET one path against the real app and return (status, body)."""
    with _app(adapters).test_client() as client:
        response = client.get(path)
        return response.status_code, response.get_data(as_text=True)


def squash(markup: str) -> str:
    """One space between words, so a prose assertion cannot be beaten by a line break.

    The same helper tests/test_chain_panel.py carries, for the reason its docstring
    records: templates here wrap at about 100 columns, so any multi-word claim about
    rendered prose has to be made against normalized whitespace or it is a check that
    passes and fails on the width of the file it reads.
    """
    return re.sub(r"\s+", " ", markup)


def ids_in(markup: str) -> set[str]:
    """Every `id="..."` in the rendered page."""
    return set(re.findall(r'id="([^"]+)"', markup))


# ---------------------------------------------------------------------------
# THE RAIL
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("asset", SIX_CHAINS)
@pytest.mark.parametrize("query", ["", "?ask"])
def test_every_rail_entry_jumps_to_a_section_that_EXISTS(asset, query):
    """A rail link at a fragment no element carries does NOTHING, silently.

    No error, no console message, no visual change -- the operator clicks Transactions
    and the page sits there, and the only conclusion available to them is that it is
    broken. So every entry of every chain's rail is walked against the real render.

    BOTH STATES, because the unasked render is the one an operator LANDS on and the
    sections that carry these ids could easily have been written inside the
    `{% if data.asked %}` branch. That is precisely the mistake this parametrization
    exists to catch: it would leave every rail link on the landing page dead.

    MUTATION: wrap any fixed section in admin_chain.html in `{% if data.asked %}`, or
    change one anchor in services/chain_wallet_layout.py's tables. Fails on the chain
    that carries it, in the unasked state, naming the anchor.
    """
    status, body = render(f"/admin/wallets/{asset}{query}", _adapters())
    assert status == 200, asset
    present = ids_in(body)
    for entry in layout.rail_entries(asset, panel.panel_kind(asset)):
        assert entry.anchor in present, (
            f"{asset}{query}: the rail links at #{entry.anchor} ({entry.label}) and nothing in the "
            f"rendered page carries that id. The ids it does carry are "
            f"{sorted(i for i in present if i.startswith('cw-'))}"
        )


@pytest.mark.parametrize("asset", SIX_CHAINS)
def test_no_wallet_section_exists_that_the_rail_does_not_link_to(asset):
    """The other direction: a pane nothing navigates to is a pane nobody finds.

    THE RAIL IS THE ONLY NAVIGATION THIS PAGE HAS. A section with a `cw-` id and no
    rail entry is reachable by scrolling and by Ctrl-F and by nothing else, which on a
    30KB page is the same as not being there. The status bar and the pane sections a
    translated chain renders are the two deliberate exceptions and they are named here
    rather than filtered silently -- `cw-status` is linked by Solana's rail only, and a
    pane section is linked when it is one of the rail's entries.

    MUTATION: add a `<section class="panel cw-pane" id="cw-whatever">` to
    admin_chain.html without a matching Rail entry. Fails on every chain at once.
    """
    status, body = render(f"/admin/wallets/{asset}?ask", _adapters())
    assert status == 200, asset
    anchors = {entry.anchor for entry in layout.rail_entries(asset, panel.panel_kind(asset))}
    # TWO SECTIONS ARE STATUS-BAR FURNITURE RATHER THAN PANES, and both are named here
    # rather than filtered by a pattern, so a third one cannot appear unnoticed.
    #
    #   cw-status  Qt's status bar is not a rail entry either. Only Solana's rail links
    #              at it, for the commitment rung that lives there.
    #   cw-lock    CORE SHOWS THE WALLET LOCK AS A PADLOCK IN THE STATUS BAR and has no
    #              rail entry for it, so neither does this. The section is the expansion
    #              of the Wallet status cell -- and Gridcoin's rail DOES link at it,
    #              under Gridcoin's own word for the fact (Staking), which is why one
    #              section serves both vocabularies.
    allowed = anchors | {"cw-status", "cw-lock"}
    # A translated chain renders one section per pane question, and the rail links at
    # the two that are also rail entries. The rest are reachable from the pane above
    # them in the flow, which is how Qt's own panes read.
    allowed |= set(panel.chain_panel(_app().config, {}, asset)["qt"]["pane_anchors"].values())
    for found in ids_in(body):
        if not found.startswith("cw-"):
            continue
        assert found in allowed, (
            f"{asset}: the page carries a wallet section id={found!r} that no rail entry links to "
            f"and that is not the status bar or a pane question. The rail's anchors are "
            f"{sorted(anchors)}"
        )


def test_the_core_rail_is_QTS_FOUR_and_the_node_window_is_a_SEPARATE_group():
    """Core's rail is Overview, Send, Receive, Transactions -- and Peers is not in it.

    QT'S PEERS AND INFORMATION ARE TABS OF THE NODE WINDOW, reached from
    Window -> Node window, not entries in the icon rail. A Core user looking for Peers
    does not look down the left edge. Listing them in the rail would be a borrowed
    layout that misdescribes the thing it borrowed from, which is the failure this whole
    module is written against -- so they are a second, labeled group, which is what they
    actually are.

    MUTATION: give _NODE_WINDOW's two entries `group="rail"`. The second assertion fails
    and the rail then claims Core has six rail entries.
    """
    rail = layout.rail_entries("BTC", panel.PANEL_KIND_CORE)
    in_rail = [entry.label for entry in rail if entry.group == "rail"]
    assert in_rail == ["Overview", "Send", "Receive", "Transactions"], in_rail
    in_node = [entry.label for entry in rail if entry.group == "node"]
    assert in_node == ["Peers", "Information"], in_node
    # AND THE GROUP IS LABELED WITH QT'S MENU PATH, so a reader knows where to find it
    # in the real wallet rather than being told it is somewhere it is not.
    assert "Node window" in layout.RAIL_GROUP_LABELS["node"]
    assert "Window" in layout.RAIL_GROUP_LABELS["node"]


def test_gridcoins_rail_carries_gridcoins_OWN_entries_and_BTCs_does_not():
    """Gridcoin Research Qt is Core's layout PLUS Voting, Researcher and Magnitude.

    AND THE PER-CHAIN DIFFERENCE IS DATA, NOT MARKUP, which is the property the whole
    arrangement depends on: this asserts it by calling the function with a seeded asset
    string, which is impossible for a `{% if data.asset == 'GRC' %}` in a template.

    MUTATION: move Gridcoin's four entries out of _RAIL_EXTRA and into _RAIL_CORE. The
    second assertion fails -- BTC's rail then advertises a Voting pane for a daemon that
    has no polls.
    """
    grc = {entry.label for entry in layout.rail_entries("GRC", panel.PANEL_KIND_CORE)}
    btc = {entry.label for entry in layout.rail_entries("BTC", panel.PANEL_KIND_CORE)}
    for gridcoin_only in ("Staking", "Voting", "Researcher", "Magnitude"):
        assert gridcoin_only in grc, gridcoin_only
        assert gridcoin_only not in btc, (
            f"BTC's rail advertises {gridcoin_only}, which is a Gridcoin pane. Bitcoin Core has "
            f"no such thing and the entry would be a promise nothing can keep"
        )
    assert grc - btc == {"Staking", "Voting", "Researcher", "Magnitude"}


def test_a_rail_entry_whose_data_nothing_reads_SAYS_SO_and_renders_no_figure():
    """Gridcoin's Voting, Researcher and Magnitude. The operator's own condition:

    "Render only what this tree can actually read; a rail entry whose data nothing here
     can fetch says so rather than being faked."

    ESTABLISHED BY GREPPING THE TREE, with the counts and the denominator in
    services/chain_wallet_layout.py's header: 0 matches for a research magnitude, 0 for
    a poll, and 5 for "beacon" of which none reads one. And no research RPC is on
    chains/daemon_wallet.READ_ONLY_RPCS, so it could not be fetched from here even if a
    reader existed.

    THE SECOND HALF IS THE ONE WORTH HAVING: the rendered section must carry no digit
    that could be read as a figure. A fabricated magnitude of 0 on a page arranged like
    a wallet is indistinguishable from a real researcher with no credit, and it is the
    exact shape of the defect this condition exists to prevent.

    MUTATION: change one of those three entries to RAIL_LIVE. The first assertion fails.
    Render a `<span class="num">0</span>` in the generated section and the second does.
    """
    unreadable = [entry for entry in layout.rail_entries("GRC", panel.PANEL_KIND_CORE)
                  if entry.state == layout.RAIL_UNREADABLE]
    assert {entry.label for entry in unreadable} == {"Voting", "Researcher", "Magnitude"}
    assert all(entry.why for entry in unreadable), "an unreadable entry with no reason is a blank"

    status, body = render("/admin/wallets/GRC?ask", _adapters())
    assert status == 200
    flat = squash(body)
    assert "nothing in this repository reads a Gridcoin poll" in flat
    assert "nothing in this repository reads a beacon or a CPID" in flat
    assert "nothing in this repository reads a magnitude" in flat
    # AND THE SECTION ITSELF CARRIES NO NUMBER. Pinned by location: everything between
    # the Magnitude heading and the end of its section.
    _before, marker, after = body.partition('id="cw-magnitude"')
    assert marker, "the Magnitude section is gone, so the rail's entry points at nothing"
    section = after.partition("</section>")[0]
    # UNESCAPED BEFORE THE DIGIT SCAN. `&#39;` is an apostrophe and the first version of
    # this assertion read its `3` as a rendered figure -- a check that failed on correct
    # markup, which is worse than one that passes on broken markup because it teaches
    # the reader to ignore it.
    stripped = html.unescape(re.sub(r"<[^>]+>", " ", section))
    assert not re.search(r"\d", stripped), (
        f"the Magnitude section renders a digit, which on a page arranged like a wallet reads as "
        f"a magnitude figure: {stripped.strip()[:200]!r}"
    )


def test_the_PARTIAL_entry_names_what_it_cannot_show_rather_than_implying_it_has_it():
    """GRC's Staking is readable in one half and not the other, and says which.

    The wallet LOCK is read -- `getwalletinfo`'s `unlocked_until`, through
    chains/wallet_lock.encryption_state(), with GRC in wallet_lock.STAKING_CHAINS
    because a Gridcoin wallet unlocked FOR STAKING is a distinct state. Qt's staking
    line also shows WEIGHT, NET WEIGHT and EXPECTED TIME, and none of the three is read
    by anything in this tree.

    A LIVE ENTRY WITH NO CAVEAT WOULD BE THE WRONG ANSWER HERE: an operator clicking
    Staking and finding only a padlock has been told, by the rail, that this is the
    staking pane. The `missing` sentence is what makes the half that is absent visible.

    MUTATION: clear `missing` on that entry. Both assertions fail and the page then
    shows a lock state under a heading Gridcoin users read as a staking report.
    """
    staking = next(entry for entry in layout.rail_entries("GRC", panel.PANEL_KIND_CORE)
                   if entry.label == "Staking")
    assert staking.state == layout.RAIL_LIVE, "the lock half IS read, so this is not unreadable"
    assert staking.missing, "a partial answer with no caveat reads as a complete one"
    for figure in ("WEIGHT", "NET WEIGHT", "EXPECTED TIME"):
        assert figure in staking.missing, figure

    status, body = render("/admin/wallets/GRC?ask", _adapters())
    assert status == 200
    assert "NET WEIGHT" in squash(body), "the caveat is in the data and not on the page"
    # AND IT RENDERS IN THE SECTION THE RAIL POINTS AT, not somewhere else on the page.
    after = body.partition('id="cw-lock"')[2].partition("</section>")[0]
    assert "NET WEIGHT" in squash(after), "the caveat is on the page but not in the staking pane"
    # BTC's rail has no entry for that section, so no caveat box appears on its page.
    _status, btc = render("/admin/wallets/BTC?ask", _adapters())
    assert "NET WEIGHT" not in btc, (
        "BTC's wallet-encryption pane carries Gridcoin's staking caveat, which is a sentence "
        "about a chain that is not this one"
    )


# ---------------------------------------------------------------------------
# NOTHING IS HIDDEN
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("asset", SIX_CHAINS)
def test_the_rail_really_does_switch_panes_and_nothing_needs_javascript(asset):
    """THE RULE THIS REPLACES WAS A DEBUGGING WORKFLOW, NOT A DESIGN PRINCIPLE.

    It was test_nothing_on_any_chains_panel_is_HIDDEN, and it forbade `display: none`
    anywhere in the markup or the stylesheet. The measurement behind it was real:
    marking every panel but one hidden, exactly as a tab mechanism does, made a browser
    copy of /admin return 923 of 12,502 characters, 7.4%, and 0 of the 14 panel
    headings. That mattered because the operator diagnoses by pasting pages back.

    IT WAS OVERRIDDEN ON 2026-10-10, twice, explicitly:

      "how about the the left pane makes it's information appear until another left pane
       control is clicked instead of it being one long fucking stupid page that maes zero
       sense. APPLY MODERN INDUSTRIAL HYGIENCE AND ERGNOMICS TO THIS DISPLAY."

      "the the goddamn subtab controls should be subtabbed further. it's too schlong and
       stupid."

    and then, when the rule itself was cited back at them:

      "what's a no hiding rule? we're designing a fucking UI"

    Which settles it. A paste being complete is worth something; a wallet panel that
    opens with eleven stacked panes is worth less than nothing. So the panes switch.

    WHAT THIS ASSERTS INSTEAD, and all four are properties the old test could not express
    because it banned the mechanism rather than checking the outcome:

      the switch EXISTS         the stylesheet actually carries a :target rule, so a rail
                                entry does something. The old test guaranteed the
                                opposite.
      ONE pane by default       :first-of-type is shown with no fragment set. A tab strip
                                whose default is "all of them" is what the operator
                                called schlong.
      NO JAVASCRIPT             still asserted, and it is now load-bearing rather than
                                incidental: this page is unauthenticated on whatever
                                interface it was bound to, so a surface that executes
                                nothing is a surface nothing can be made to execute. The
                                switch is the browser's own fragment state.
      PRINT GETS EVERYTHING     the one piece of the old rule worth keeping. A printed or
                                PDF-exported panel that silently omitted five of six
                                panes is the 923-of-12,502 failure in a second medium,
                                and it is the escape hatch for the paste workflow.

    MUTATION: delete the `@media print` block and the fourth assertion fails; delete the
    `:first-of-type` rule and the second fails; delete the `:target` rules and the first.
    """
    status, body = render(f"/admin/wallets/{asset}?ask", _adapters())
    assert status == 200, asset

    # NO INLINE HIDING IN THE MARKUP. The switch belongs in the stylesheet, in one place
    # a reader can find, not sprinkled on sections where the next editor will not see it.
    for forbidden in ('style="display:none', 'style="display: none', "visibility: hidden"):
        assert forbidden not in body.lower(), f"{asset} hides a section inline: {forbidden}"

    # AND NO SCRIPT AT ALL. See the docstring: this is the security property, not a style
    # preference.
    assert "<script" not in body.lower(), f"{asset} loads a script; the rail needs none"

    sheet = (REPO_ROOT / "swap_terminal" / "static" / "chain_wallet.css").read_text()
    # COMMENTS STRIPPED FIRST, because that file's header quotes the rules it implements
    # and a check that reads prose is refuted by the sentence stating the rule. CLAUDE.md
    # records the same shape: its British-spelling scan counted the rule's own examples.
    code = re.sub(r"/\*.*?\*/", " ", sheet, flags=re.DOTALL)
    squashed = re.sub(r"\s+", " ", code)

    assert ".cw-pane:target" in squashed, (
        "the stylesheet carries no :target rule, so clicking a rail entry does nothing and "
        "the page is one long scroll again"
    )
    assert ".cw-panes > .cw-pane:first-of-type { display: block" in squashed, (
        "no default pane, so a page opened with no fragment shows either everything or "
        "nothing. Qt opens on Overview and so must this"
    )
    assert "@media print" in squashed, (
        "no print override, so a printed or exported panel silently omits every pane but "
        "the selected one -- which is the 923-of-12,502 measurement in a second medium"
    )
    # THE DEFAULT IS ONE PANE, asserted as the pair of rules rather than by rendering,
    # because a test client cannot apply CSS. The pair is what makes it one: hide all,
    # then show the first.
    assert ".cw-panes > .cw-pane { display: none" in squashed, (
        "panes are not hidden by default, so the default state is every pane stacked"
    )


@pytest.mark.parametrize("asset", SIX_CHAINS)
def test_qts_send_and_receive_are_inert_PROSE_and_not_disabled_controls(asset):
    """A grayed-out Send button is a promise. There is nothing here to enable.

    tests/test_chain_panel.py already asserts the rendered page carries no `<form`, no
    `<input`, no `<textarea`, no `<button` and no `type="password"`, on all six chains.
    This adds the half that test cannot see: that the two tabs are PRESENT as named
    regions with the sentence saying where spending happens, rather than simply absent.

    WHY ABSENT WOULD BE WORSE. A wallet GUI missing Send and Receive with nothing saying
    why reads as half-built, and the operator's next move is to go looking for the
    button. That is rule 14 at the scale of a feature.

    MUTATION: delete the generated-sections loop from admin_chain.html. The rail's Send
    and Receive entries then point at nothing (caught by the anchor test too) and the
    first assertion here fails.
    """
    status, body = render(f"/admin/wallets/{asset}?ask", _adapters())
    assert status == 200, asset
    present = ids_in(body)
    assert "cw-send" in present and "cw-receive" in present, (
        f"{asset}: Qt's Send and Receive are simply missing rather than named as inert"
    )
    flat = squash(body)
    assert "[inert]" in flat, f"{asset}: nothing marks the regions as inert in the markup itself"
    # THE SENTENCE WENT, THE MARK AND THE REAL GUARANTEES STAY. The assertions
        # below -- no <input, no <form, no disabled attribute -- are what actually
        # prevents a control appearing; the sentence only described them.
    # `disabled` IS CHECKED HERE AND NOT IN THE OTHER FILE, because a disabled attribute
    # on a non-form element would slip past a check for `<input`.
    #
    # AS AN ATTRIBUTE AND NOT AS A WORD, which is a correction this suite has had to make
    # before: tests/test_operator_panel.py records that a gate forbidding the WORD
    # forbids reporting the fact, and this page's whole Send region is the sentence
    # "there is no disabled one either". So the assertion is on the shapes an attribute
    # takes in markup.
    for shape in ('disabled>', 'disabled ', 'disabled=', "disabled/"):
        assert shape not in body.lower().replace("no disabled ", ""), (
            f"{asset} renders a disabled attribute ({shape!r}), which is the grayed-out control "
            f"this layout deliberately does not have"
        )


def test_no_chain_claims_a_core_wallet_it_does_not_have():
    """ICP, SOL and XRP have no Core-derived GUI, and the page must not imply one.

    THE NOTICE THAT SAID SO IS GONE, 2026-10-10. It was ~460 characters at the top of
    those three pages -- "THE LAYOUT IS BORROWED ON PURPOSE AND THIS CHAIN HAS NO CORE
    WALLET. There is no Bitcoin Core-derived GUI for it, there never has been..." -- and
    the operator read it off all three and said "stupid bullshit. remove", "more stupid
    bullshit", "clutter bulshit verbose word salad".

    WHY REMOVING IT IS SAFE RATHER THAN A LOST GUARANTEE. The notice defended against a
    reader concluding that XRP ships a Qt wallet. Nobody arrives at /admin/wallets/XRP
    with that question; the reader is this desk's operator looking for a balance. What
    WOULD mislead is the page using Core's own vocabulary for a chain that has none --
    calling an XRP figure "Immature balance", or giving SOL a "Node window". That is a
    real failure mode and it is what this test now checks, which is strictly more useful
    than checking for a disclaimer.

    So: the three chains without a Core wallet must not borrow Core's SPECIFIC
    vocabulary, and the three with one may.
    """
    core_only = ("Immature", "Node window")
    for asset in ("XRP", "SOL", "ICP"):
        status, body = render(f"/admin/wallets/{asset}", _adapters())
        assert status == 200, asset
        flat = squash(body)
        assert "borrowed" not in flat.lower(), (
            f"{asset} still carries a borrowed-layout disclaimer; it was deleted as clutter"
        )
        for word in core_only:
            assert word not in flat, (
                f"{asset} has no Bitcoin Core wallet, so using Core's own term {word!r} for one "
                f"of its figures is the borrowed-layout error this test exists for -- the "
                f"vocabulary has to be this chain's own"
            )
    for asset in ("BTC", "LTC", "GRC"):
        status, body = render(f"/admin/wallets/{asset}", _adapters())
        assert status == 200, asset
        assert "borrowed" not in squash(body).lower(), (
            f"{asset} has a real Core wallet; nothing should describe the resemblance as borrowed"
        )


# ---------------------------------------------------------------------------
# THE BALANCES CARD
# ---------------------------------------------------------------------------


def test_the_balance_card_is_QTS_FOUR_LINES_with_the_total_summed():
    """Available, Pending, Immature, Total -- Qt's Overview card, in Qt's order.

    MUTATION: drop the Total line from balance_card(). The third assertion fails. Qt's
    card has four rows and the fourth is the one an operator reads first.
    """
    card = layout.balance_card({
        "available": {"value": 1.5, "note": "spendable now", "reported": True},
        "pending": {"value": 0.25, "note": "in the mempool", "reported": True},
        "immature": {"value": 0.125, "note": "coinbase", "reported": True},
    })
    assert [line.label for line in card] == ["Available", "Pending", "Immature", "Total"]
    assert all(line.reported for line in card)
    assert card[-1].value == "1.87500000", card[-1]
    # AND IT RENDERS, in the card beside the recent transactions, on the real page.
    status, body = render("/admin/wallets/BTC?ask", _adapters())
    assert status == 200
    overview = body.partition('id="cw-overview"')[2].partition("</section>")[0]
    for label in ("Available", "Pending", "Immature", "Total"):
        assert label in overview, f"{label} is not in the Overview card"
    assert "1.87500000" in overview, "the Total is not rendered beside the three it sums"


def test_a_total_over_a_PARTIAL_balance_set_is_refused_rather_than_summed():
    """Qt's Total is the sum of three measurements. Two of them is not a total.

    THE DEFECT THIS STOPS. chains/daemon_wallet's `getinfo` fallback answers `balance`
    alone and reports pending and immature as unavailable, deliberately, because an
    absent field is not a zero. A Total computed over that set would equal the available
    balance -- and an operator reading it would conclude the wallet holds nothing
    immature, on a desk that mines its own regtest coins.

    MUTATION: make balance_card() treat an unreported figure as 0.0. The second
    assertion fails and the page shows `1.75000000` as a total that is wrong by whatever
    the immature balance is.
    """
    card = layout.balance_card({
        "available": {"value": 1.5, "note": "spendable now", "reported": True},
        "pending": {"value": 0.25, "note": "in the mempool", "reported": True},
        "immature": {"value": None, "note": "not reported by this build", "reported": False},
    })
    total = card[-1]
    assert total.label == "Total"
    assert total.reported is False, "a total was computed over a balance set with a hole in it"
    assert not total.value, f"a figure was rendered for an unestablished total: {total.value!r}"
    assert "NOT ESTABLISHED" in total.note and "Immature" in total.note, total.note

    status, body = render("/admin/wallets/BTC?ask", {"BTC": Answering("BTC", CORE_PARTIAL)})
    assert status == 200
    assert "1.75000000" not in body, (
        "the page renders the sum of the two reported figures as a Total, which claims a "
        "measurement of the third that nobody took"
    )
    assert "NOT ESTABLISHED" in body


def test_a_node_with_no_wallet_gets_NO_CARD_rather_than_a_card_of_zeros():
    """A node with no wallet loaded has no balance to have. That is not zero.

    Measured on the operator's host 2026-10-10 and it cost a wrong answer on screen.
    tests/test_chain_panel.py pins the absence of the string "0.00000000" on that
    render; this pins the decision one level down, where it can be called with a seeded
    input, and a card of zeros is exactly what the arrangement made easy to introduce.

    MUTATION: return a card of zeros from balance_card() when `balances` is None. Both
    assertions fail.
    """
    assert layout.balance_card(None) == ()
    assert layout.balance_card({}) == ()
    walletless = {**CORE_COMPLETE, "listwallets": []}
    status, body = render("/admin/wallets/BTC?ask", {"BTC": Answering("BTC", walletless)})
    assert status == 200
    assert "0.00000000" not in body, "a balance figure is rendered for a node with no wallet"
    assert "holds NO loaded wallet" in squash(body)
    assert "(no balance)" in squash(body), "the card region renders nothing at all rather than a sentence"


# ---------------------------------------------------------------------------
# QT'S TRANSACTION COLUMNS
# ---------------------------------------------------------------------------


def test_qts_transaction_columns_are_rendered_under_qts_own_headings():
    """Date, Type, Label, Address, Amount -- and the panel this replaced had no Date.

    THE DATE COLUMN IS THE ONE THAT WAS MISSING ENTIRELY. chains/daemon_wallet.py
    collected `time` per row and nothing rendered it, so Qt's first column was absent
    from a page whose stated purpose is answering "did the coin arrive".

    MUTATION: drop the Date column from _TX_COLUMNS_CORE. The first assertion fails.
    """
    labels = [column.label for column in layout.tx_columns(panel.PANEL_KIND_CORE)]
    assert labels[:5] == ["Date", "Type", "Label", "Address", "Amount"], labels
    status, body = render("/admin/wallets/BTC?ask", _adapters())
    assert status == 200
    transactions = body.partition('id="cw-transactions"')[2].partition("</section>")[0]
    for label in ("Date", "Type", "Label", "Address", "Amount", "Confirmations"):
        assert f">{label}</th>" in transactions, f"Qt's {label} column is not a column"
    # THE DAEMON'S OWN CATEGORY WORD, NOT COLLAPSED TO A DIRECTION. `generate` and
    # `immature` are the two that matter on a desk that mined its own coins and both
    # would read as `receive`.
    assert "generate" in transactions, "the daemon's own category word was normalized away"
    assert "a swap deposit" in transactions, "Qt's Label column renders no label"


def test_a_transaction_date_is_a_UTC_MOMENT_and_never_microfortnights():
    """Rule 6's boundary, at the one place this change could have crossed it.

    A duration is an interval and takes microfortnights. A transaction's `time` is a
    POINT on the calendar, and "microfortnights since 1970" is not a thing anybody
    reads -- services/chain_panel.core_live() draws the same line in prose for a peer's
    `conntime`.

    THE Z IS PART OF THE ASSERTION. This page is pasted into a conversation, and a
    local-time timestamp with no zone on it is a figure the reader cannot compare
    against a deposit row on /admin or a line in a daemon log.

    MUTATION: format the Date column through microfortnights.format_duration(). The
    second assertion fails.
    """
    assert layout.unix_to_utc_text(1_700_000_000) == "2023-11-14 22:13:20Z"
    assert layout.unix_to_utc_text(None) == ""
    assert layout.unix_to_utc_text("not a number") == ""
    assert layout.unix_to_utc_text(True) == "", "a bool is not a timestamp and must not format as one"

    status, body = render("/admin/wallets/BTC?ask", _adapters())
    assert status == 200
    transactions = body.partition('id="cw-transactions"')[2].partition("</section>")[0]
    assert "2023-11-14 22:13:20Z" in transactions, "the Date column renders no date"
    rows = transactions.partition("<tbody>")[2].partition("</tbody>")[0]
    assert "µfn" not in rows, (
        "a transaction row carries a microfortnight figure. A date is a moment and a "
        "confirmation count is a count; neither is a duration (rule 6)"
    )


def test_an_empty_cell_is_a_MARKED_dash_and_a_zero_is_still_a_figure():
    """Zero confirmations is the most interesting row on the page, not an absence.

    THE DEFECT THIS STOPS IS A TRUTHINESS TEST. `if not value` would render an
    unconfirmed transaction's `confirmations` of 0 as an empty cell, and 0 is exactly
    the row an operator opens this pane to look at. Same for an amount of 0.0.

    MUTATION: change tx_cells()' `value is None` to `not value`. The second assertion
    fails.
    """
    columns = layout.tx_columns(panel.PANEL_KIND_CORE)
    cells = {cell["label"]: cell for cell in layout.tx_cells(
        {"time": None, "category": "receive", "label": "", "address": "",
         "amount": 0.0, "confirmations": 0, "fee": None, "txid": "cc" * 32},
        columns,
    )}
    assert cells["Label"]["shown"] is False, "an empty label is not an absence that was marked"
    assert cells["Confirmations"]["shown"] is True, (
        "zero confirmations rendered as an empty cell. 0 is an unconfirmed transaction -- the row "
        "this pane exists for -- and not a missing field"
    )
    assert cells["Confirmations"]["text"] == "0"
    assert cells["Amount"]["shown"] is True and cells["Amount"]["text"] == "0.00000000"


def test_the_two_chains_with_no_decomposed_row_render_no_FAKE_columns():
    """XRP and SOL get the furniture, not a table with an invented schema.

    WHY THE ROWS ARE NOT DECOMPOSED HERE, and it is a rule 8 argument rather than
    laziness. `account_tx` nests its transaction under `tx` and
    `getSignaturesForAddress` answers signatures rather than transactions. Pulling those
    apart in a LAYOUT module would be a second implementation of that chain's
    transaction shape, competing with the one in chains/xrp_payments.py that the deposit
    path depends on -- on `meta.delivered_amount` versus `Amount`, which that module
    calls the single most expensive thing it knows.

    AND XRP'S DATE WOULD HAVE BEEN WRONG BY THIRTY YEARS. An `account_tx` entry's `date`
    is seconds since 2000-01-01, the Ripple epoch. Run through unix_to_utc_text() it
    would put every XRP transaction in 1970-something, on a page whose purpose is
    letting an operator see whether a deposit arrived. Nothing in this tree converts the
    Ripple epoch.

    MUTATION: return _TX_COLUMNS_CORE from tx_columns() for every kind. The first
    assertion fails, and the XRP panel then grows a Date column fed by a Ripple
    timestamp.
    """
    assert layout.tx_columns(panel.PANEL_KIND_TRANSLATED) == ()
    assert layout.tx_columns(panel.PANEL_KIND_NO_RPC) == ()
    for asset in ("XRP", "SOL"):
        status, body = render(f"/admin/wallets/{asset}?ask", _adapters())
        assert status == 200, asset
        transactions = body.partition(
            'id="cw-pane-transactions"')[2].partition("</section>")[0]
        assert transactions, f"{asset} has no Transactions region at all"
        assert ">Date</th>" not in transactions, (
            f"{asset} renders a Date column. Its timestamps are not in the Unix epoch and nothing "
            f"here converts them"
        )


# ---------------------------------------------------------------------------
# THE STATUS BAR
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("asset", SIX_CHAINS)
@pytest.mark.parametrize("query", ["", "?ask"])
def test_the_status_bar_is_never_empty_on_any_chain_in_any_state(asset, query):
    """A wallet's status bar is the part a user glances at instead of reading.

    So a blank one is rule 14's ambiguous gap in the place it costs the most: the
    operator cannot tell "no peers" from "nobody asked", and those two have opposite
    next actions.

    EVERY CELL CARRIES ITS SENTENCE, which is the second half. Rule 14 asks for what a
    number means to be next to the number, and a bar of five bare figures is five
    numbers nobody can act on.

    MUTATION: return () from any of the four *_status_bar functions. Fails on the chains
    that use it, in the state that reaches it.
    """
    status, body = render(f"/admin/wallets/{asset}{query}", _adapters())
    assert status == 200, asset
    cells = panel.chain_panel(_app(_adapters()).config, _adapters(), asset,
                              ask=bool(query))["qt"]["status"]
    assert cells, f"{asset}{query}: the status bar is empty"
    for cell in cells:
        assert cell.label, f"{asset}{query}: a status cell with no label"
        assert cell.value, f"{asset}{query}: a status cell with no value -- a blank is not a result"
        assert cell.note, f"{asset}{query}: a status cell with no sentence saying what it means"
        assert cell.state in layout.CELL_STATES, cell.state
        # NOT COLOR ALONE. This operator pastes these pages back and no CSS travels in a
        # paste, so the state has to be in the markup.
        assert cell.glyph == layout.CELL_GLYPHS[cell.state], cell
        assert cell.glyph in body, f"{asset}{query}: the cell glyph is not in the rendered page"
    assert "Status bar" in body


def test_not_asked_and_nothing_to_ask_and_a_refusal_are_THREE_different_bars():
    """The three states rule 14 says must stay distinguishable, on the bar itself.

      not asked       a page waiting for the operator. Nothing is wrong.
      nothing to ask  there is no adapter for this chain. A configuration fault.
      the chain said no  it was asked and it refused, with its own words.

    A BAR OF FIVE `[?]` CELLS FOR THE FIRST TWO WOULD BE THE DEFECT. It reads as five
    failed reads, which is the third state, which is the one that means something is
    broken.

    MUTATION: make not_asked_bar() ignore `cannot_ask`. The second assertion fails and
    an unconfigured chain reads as a page nobody has clicked on.
    """
    waiting = layout.not_asked_bar("BTC", "")
    assert len(waiting) == 1 and waiting[0].label == "Not asked", waiting
    assert "contacted nothing" in waiting[0].note

    no_adapter = layout.not_asked_bar("BTC", "there is no adapter for this chain")
    assert len(no_adapter) == 1 and no_adapter[0].label == "Nothing to ask", no_adapter
    assert "no endpoint for this chain in this process" in no_adapter[0].note
    assert waiting[0].note != no_adapter[0].note

    # AND THE THIRD IS A REFUSAL THE CHAIN ITSELF PRODUCED, which must carry the chain's
    # own words rather than a sentence this repository wrote.
    refusing = {"server_info": RuntimeError("rippled: noNetwork")}
    status, body = render("/admin/wallets/XRP?ask", {"XRP": Accounted(refusing)})
    assert status == 200
    assert "noNetwork" in body, "the chain's own refusal does not reach the screen"
    # A LINE HERE READ `assert ... or True` AND THEREFORE ASSERTED NOTHING. It was my
    # own, left behind while narrowing a check I could not get to express what I meant,
    # and it is the one defect a test file can carry that no run will ever report: the
    # suite stays green and one of its claims is decoration. Deleted rather than
    # repaired, because the claim it was reaching for -- that a refused render does not
    # read as an unasked one -- is exactly what the two assertions below it make, on the
    # status bar's own labels rather than on a substring of the page.
    bar = panel.chain_panel(
        _app().config, {"XRP": Accounted(refusing)}, "XRP", ask=True)["qt"]["status"]
    labels = [cell.label for cell in bar]
    assert "Not asked" not in labels and "Nothing to ask" not in labels, (
        f"a chain that was asked and REFUSED reports as not-asked ({labels}), which is the one "
        f"reading that tells the operator to go and click something. The three states have to "
        f"stay distinguishable"
    )
    # AND THE CELL THAT COULD NOT BE READ SAYS SO WITH THE CHAIN'S OWN WORDS IN IT,
    # rather than with a sentence this repository wrote about a chain it could not reach.
    unread = [cell for cell in bar if cell.state == layout.CELL_UNKNOWN]
    assert unread, f"every cell reads as established on a render where server_info raised: {bar}"
    assert any("noNetwork" in cell.note for cell in unread), (
        f"no status cell carries the chain's own refusal: {[cell.note for cell in unread]}"
    )


def test_a_core_bar_carries_qts_five_indicators():
    """Sync, block height, peer count, network, lock. Qt's status bar, in Qt's order.

    MUTATION: drop the Peers cell from core_status_bar(). The first assertion fails -- a
    node with no peers broadcasts into nothing, and that finding leaves the bar an
    operator glances at.
    """
    adapters = _adapters()
    result = panel.chain_panel(_app(adapters).config, adapters, "BTC", ask=True)
    labels = [cell.label for cell in result["qt"]["status"]]
    assert labels == ["Sync", "Blocks", "Peers", "Network", "Wallet"], labels
    by_label = {cell.label: cell for cell in result["qt"]["status"]}
    assert by_label["Blocks"].value == "812"
    assert by_label["Peers"].value == "1"
    assert by_label["Network"].value == "testnet4"
    # THE NETWORK CELL IS THE ONE THAT DECIDES WHETHER A COMMAND REACHES A MAINNET
    # WALLET, so its verdict is on the bar and not only four sections up.
    assert "network_is_test" in by_label["Network"].note
    # AND NONE OF THESE IS A DURATION. A block height and a peer count are counts.
    for label in ("Blocks", "Peers"):
        assert "µfn" not in by_label[label].value, label


def test_a_node_with_no_peers_WARNS_and_a_node_nobody_asked_does_not():
    """Measured on the operator's host 2026-10-10: both regtest daemons had 0 peers.

    chains/daemon_wallet.peer_rows()' own note: on regtest that is the default and it is
    invisible until it matters -- a transaction broadcast from a node with no peers is
    valid, confirms on that node's own chain, and is never seen by any other wallet. The
    operator spent an evening trying to get coins into an external wallet.

    AND "NOBODY ASKED" MUST NOT WARN, which is the half that keeps the warning worth
    reading. A bar that cries wolf on every landing page is a bar an operator stops
    looking at (rule 13's own argument about a warning that fires on every run).

    MUTATION: make the peers cell CELL_WARN whenever the row list is empty, without
    distinguishing the error case. The last assertion fails.
    """
    lonely = {**CORE_COMPLETE, "getpeerinfo": []}
    adapters = {"BTC": Answering("BTC", lonely)}
    result = panel.chain_panel(_app(adapters).config, adapters, "BTC", ask=True)
    peers = next(cell for cell in result["qt"]["status"] if cell.label == "Peers")
    assert peers.state == layout.CELL_WARN, peers
    assert peers.value == "0", "zero peers must render as the figure 0, not as an absence"
    assert "broadcasts into nothing" in peers.note

    unasked = panel.chain_panel(_app({}).config, {}, "BTC", ask=False)["qt"]["status"]
    assert all(cell.state == layout.CELL_UNKNOWN for cell in unasked), (
        "a page nobody has asked reports a warning, which makes the real warning unreadable"
    )


def test_each_of_the_THREE_lock_states_gets_its_own_cell_state():
    """The bug this test was written for, and it was invisible to every other assertion.

    THE DEFECT, found 2026-10-10 by reading a rendered page rather than by running
    anything: services/chain_wallet_layout.core_status_bar() compared the lock state
    against the literal `"unencrypted"`, and chains/daemon_wallet.LOCK_NOT_ENCRYPTED is
    `"not_encrypted"`. So an UNENCRYPTED wallet -- nothing to unlock, nothing in the way
    of a payout -- rendered in the status bar as `[!]`, the warning glyph, beside a
    sentence saying there was no passphrase.

    WHY NOTHING CAUGHT IT. A state comparison against a string nothing produces does not
    fail; it simply never matches. The cell still rendered, still carried a label, a
    value, a glyph and a sentence, and still passed the "status bar is never empty"
    check. A warning that fires on the healthy case is rule 13's argument about a
    warning that fires on every run: the reader learns to ignore it, and then misses the
    real one -- which here is an ENCRYPTED wallet that a payout cannot leave, which is
    the single fact that decides whether a GRC payout can be made at all and is
    invisible in every balance figure on every other screen.

    THREE STATES ONTO THREE, NOT TWO ONTO ONE. encryption_note()'s own docstring: "THE
    THIRD STATE IS NOT 'NOT ENCRYPTED', which is the whole reason this is a classifier"
    -- a viewer can say it could not tell, and reporting "not encrypted" for a daemon
    that never answered would be a measurement nobody took.

    MUTATION: compare against the literal `"unencrypted"` again, which is what the code
    said. The first case fails. Collapse `not_established` into either of the other two
    and the third fails.
    """
    cases = {
        # No `unlocked_until` at all: a Bitcoin-derived daemon reports that field ONLY
        # for an encrypted wallet, so its absence means there is no passphrase.
        LOCK_NOT_ENCRYPTED: (CORE_COMPLETE, layout.CELL_OK),
        # `unlocked_until` present: encrypted, and a payout needs a passphrase.
        LOCK_ENCRYPTED: ({**CORE_COMPLETE,
                          "getwalletinfo": {**CORE_COMPLETE["getwalletinfo"],
                                            "unlocked_until": 0}}, layout.CELL_WARN),
        # getwalletinfo did not answer: NOT a claim that the wallet has no passphrase.
        LOCK_NOT_ESTABLISHED: ({**CORE_COMPLETE,
                                "getwalletinfo": RuntimeError("Method not found"),
                                "getinfo": {"balance": 1.5}}, layout.CELL_UNKNOWN),
    }
    assert set(cases) == set(LOCK_STATES), (
        "a lock state exists that this test does not cover, which is how the first one "
        "stopped being covered"
    )
    for expected_lock, (answers, expected_cell) in cases.items():
        adapters = {"BTC": Answering("BTC", answers)}
        result = panel.chain_panel(_app(adapters).config, adapters, "BTC", ask=True)
        reported = result["live"]["panes"]["wallet"]["encryption"]["state"]
        assert reported == expected_lock, (
            f"the fixture no longer produces {expected_lock}: the daemon reported {reported}"
        )
        cell = next(c for c in result["qt"]["status"] if c.label == "Wallet")
        assert cell.state == expected_cell, (
            f"a {expected_lock} wallet renders {cell.state} in the status bar, expected "
            f"{expected_cell}. {cell}"
        )
        assert cell.value == expected_lock, cell
        assert "never offered" in cell.note, "the cell no longer says it offers no unlock"
        # AND THE SENTENCE IS NOT DOUBLE-PUNCTUATED. encryption_note()'s `why` already
        # ends in a period and this cell appends one; the first version produced "..it
        # can sign.. Reported", which is the kind of thing a reader stops trusting.
        assert ".. " not in cell.note, cell.note


def test_solanas_bar_carries_the_COMMITMENT_RUNG_from_the_one_place_that_owns_it():
    """Solana's equivalent of Core's confirmation count, and it is not a confirmation.

    chains/solana_units.py's header: "the integer this module produces is a COMMITMENT
    RANK, and it is named that everywhere. The ladder is below, it has four rungs, and
    it is the ONE place the vocabulary is derived." So the figures come from that module
    rather than being retyped -- a second four-rung ladder would be a second definition
    of what `finalized` means, on the figure that decides whether a deposit is credited.

    MUTATION: write the rungs out by hand in _BAR_STATIC. This still passes -- which is
    why the assertion is against the imported constants rather than against a literal,
    so the ladder and the page cannot disagree about a rung's number.
    """
    adapters = _adapters()
    result = panel.chain_panel(_app(adapters).config, adapters, "SOL", ask=True)
    commitment = next(cell for cell in result["qt"]["status"] if cell.label == "Commitment")
    assert DISCOVERY_COMMITMENT in commitment.value, commitment.value
    assert str(FINALIZED_RANK) in commitment.value, commitment.value
    for name, rung in COMMITMENT_RANKS.items():
        assert f"{rung}={name}" in commitment.note, (name, commitment.note)
    # A RUNG IS NOT A CONFIRMATION COUNT AND NOT A DURATION, and the cell says so.
    assert "not confirmations and not a duration" in commitment.note
    # AND SOLANA'S RAIL LINKS AT THE BAR for it, which is where the rung lives.
    rail = {entry.label: entry.anchor for entry in layout.rail_entries("SOL", "translated")}
    assert rail["Commitment"] == "cw-status", rail


def test_xrps_bar_carries_the_ledger_index_and_the_RESERVE_is_on_its_rail():
    """XRP's vocabulary, in Core's positions. A ledger index is a COUNT.

    THE RESERVE IS THE ENTRY WITH NO CORE COUNTERPART and it earns a rail slot because
    on this chain it is the difference between the balance and what a payout may draw
    on -- which is the question Core's "Available" answers. chains/xrp_signing.py reads
    the same two fields to decide exactly that.

    MUTATION: drop the reserve figures from _FIGURES["XRP"]. The last two assertions
    fail and the XRP page then shows a balance with nothing saying how much of it is
    locked.
    """
    adapters = _adapters()
    result = panel.chain_panel(_app(adapters).config, adapters, "XRP", ask=True)
    by_label = {cell.label: cell for cell in result["qt"]["status"]}
    assert by_label["Ledger index"].value == "98765"
    assert "COUNT" in by_label["Ledger index"].note
    assert "µfn" not in by_label["Ledger index"].value
    assert by_label["Peers"].value == "7"
    figures = {row.label: row for row in result["qt"]["figures"]}
    assert figures["Base reserve"].found and figures["Base reserve"].value == "1"
    assert figures["Owner reserve"].found and figures["Owner reserve"].value == "0.2"
    assert figures["OwnerCount"].found and figures["OwnerCount"].value == "2"
    rail = {entry.label for entry in layout.rail_entries("XRP", "translated")}
    assert "Account reserve" in rail


def test_a_reply_whose_SHAPE_changed_renders_a_named_absence_and_not_a_figure():
    """The property that makes spelling a response path in this module safe at all.

    Rule 17: a wrong guess has to become a visible, debuggable absence on the operator's
    screen rather than a plausible figure nobody can check. `server_info` without
    `validated_ledger` is not hypothetical -- it is what a rippled answers until it has
    one, and chains/xrp.py raises rather than falling back when the field is missing.

    MUTATION: give dig() a default return instead of a (found, value) pair. The second
    assertion fails and a missing ledger index renders as `None` beside eight real
    figures.
    """
    adapters = {"XRP": Accounted(XRP_NO_LEDGER)}
    result = panel.chain_panel(_app(adapters).config, adapters, "XRP", ask=True)
    figures = {row.label: row for row in result["qt"]["figures"]}
    assert figures["Ledger index"].found is False
    assert not figures["Ledger index"].value, figures["Ledger index"]
    assert "does not carry 'info.validated_ledger.seq'" in figures["Ledger index"].why
    # THE PATH IT LOOKED FOR IS ON EVERY ROW, present or absent, which on an absent row
    # is the whole debugging message (rule 14: echo the parameters that decide the
    # answer).
    assert figures["Ledger index"].where == "server_info -> info.validated_ledger.seq"
    # AND THE FIGURES THAT ARE STILL THERE ARE STILL READ, so one shape change does not
    # take the panel down.
    assert figures["Peers"].found and figures["Peers"].value == "7"
    status, body = render("/admin/wallets/XRP?ask", adapters)
    assert status == 200
    assert "info.validated_ledger.seq" in body, "the path that was not found is not on the page"
    assert "Traceback" not in body


def test_dig_tells_an_absent_field_from_a_falsey_one():
    """`info.peers` of 0 is a server connected to nobody. That is the finding.

    A bare `None` return would make it indistinguishable from a response with no `peers`
    key at all, and the renderer would print the same thing for both -- which is the
    single most useful thing this panel can tell an operator, lost to a return type.

    MUTATION: return `value or None` from dig(). The third assertion fails.
    """
    assert layout.dig({"info": {"peers": 7}}, "info.peers") == (True, 7)
    assert layout.dig({"info": {}}, "info.peers") == (False, None)
    assert layout.dig({"info": {"peers": 0}}, "info.peers") == (True, 0)
    assert layout.dig({"info": {"peers": False}}, "info.peers") == (True, False)
    # An empty path is the result itself, which is how a bare array or integer reads.
    assert layout.dig([1, 2, 3], "") == (True, [1, 2, 3])
    # AND A FALSEY RESULT ON THE EMPTY PATH IS STILL FOUND, which is the branch the
    # first version of this test could not see: `getClusterNodes` answering an EMPTY
    # ARRAY means gossip knows about nobody, and the Gossip peers figure must render 0
    # rather than "the reply does not carry that field". A mutation that returned
    # `bool(value)` from this branch passed the whole file until these two lines.
    assert layout.dig([], "") == (True, [])
    assert layout.dig(0, "") == (True, 0)
    # A path through a non-mapping is not found rather than an exception out of a render.
    assert layout.dig({"info": "a string"}, "info.peers") == (False, None)
    assert layout.dig(None, "info.peers") == (False, None)


def test_every_label_the_status_bar_asks_for_is_a_label_the_figure_table_produces():
    """Two tables that have to agree, and the drift is invisible on a screen.

    _BAR_FIGURES names figures by label and _FIGURES produces them. A label renamed in
    one and not the other makes a status cell silently disappear -- the bar still
    renders, the page still looks fine, and the figure an operator was relying on is
    gone with nothing saying so.

    MUTATION: rename "Gossip peers" in either table. Fails naming the chain and the
    label.
    """
    for asset, labels in layout._BAR_FIGURES.items():
        produced = {figure.label for figure in layout._FIGURES.get(asset, ())}
        for label in labels:
            assert label in produced, (
                f"{asset}'s status bar asks for the figure {label!r} and _FIGURES produces "
                f"{sorted(produced)}. The cell would vanish from the bar with nothing saying so"
            )
    # AND EVERY CHAIN WITH A STATIC CELL IS A CHAIN WITH FIGURES, so a static cell
    # cannot be the only thing on a bar that was supposed to carry readings too.
    for asset in layout._BAR_STATIC:
        assert asset in layout._BAR_FIGURES, asset


def test_every_figure_kind_and_rail_state_and_cell_state_is_a_NAMED_one():
    """A renderer with one case per state can be tested for covering them all.

    The same guard chains/daemon_wallet.WALLET_STATES and
    services/chain_panel.PANEL_KINDS carry, for the reason that module records: a
    renderer that branches on a bare string discovers a gap on an operator's screen.
    The template's rail glyph lookup is a dict keyed on exactly RAIL_STATES, so a
    fourth state added without a glyph would raise mid-render.

    MUTATION: add a Rail entry with state="partial". This fails, which is the point --
    see Rail.missing for why the partial case is a field rather than a fourth state.
    """
    for asset in SIX_CHAINS:
        for entry in layout.rail_entries(asset, panel.panel_kind(asset)):
            assert entry.state in layout.RAIL_STATES, (asset, entry)
            assert entry.group in layout.RAIL_GROUP_LABELS, (asset, entry)
            assert entry.what, (asset, entry.label, "an entry with no description")
            if entry.state != layout.RAIL_LIVE:
                assert entry.why, (asset, entry.label, "an absence with no reason is a blank")
    for figures in layout._FIGURES.values():
        for figure in figures:
            assert figure.kind in layout.FIGURE_KINDS, figure
            assert figure.note, figure
    assert set(layout.CELL_GLYPHS) == set(layout.CELL_STATES)


def test_the_icp_bar_WARNS_when_the_ledger_disagrees_with_our_derivation():
    """The check with no Core counterpart, and the one worth running.

    It asks the LEDGER to compute the account identifier this repository derived, from
    the same principal and subaccount. A disagreement means every ICP deposit address
    this terminal publishes is wrong in the same way: a customer pays, the watcher polls
    an account that stays at zero forever, and nothing errors.

    SO IT IS A WARNING AND NOT AN ABSENCE. A derivation that disagrees is the loudest
    thing on that chain's bar; one that was never checked is `unknown`, because not
    having looked is not the same as having found a problem.

    MUTATION: report a disagreement as CELL_UNKNOWN. The second assertion fails and the
    loudest finding on the page reads as a figure nobody asked for.
    """
    agreeing = {"ICP": LedgerAdapter()}
    cells = {cell.label: cell for cell in panel.chain_panel(
        _app(agreeing).config, agreeing, "ICP", ask=True)["qt"]["status"]}
    assert cells["Derivation"].state == layout.CELL_OK, cells["Derivation"]

    disagreeing = {"ICP": LedgerAdapter(derivation=(False, "the ledger returned a DIFFERENT identifier"))}
    cells = {cell.label: cell for cell in panel.chain_panel(
        _app(disagreeing).config, disagreeing, "ICP", ask=True)["qt"]["status"]}
    assert cells["Derivation"].state == layout.CELL_WARN, cells["Derivation"]
    assert cells["Derivation"].value == "DISAGREES"
    status, body = render("/admin/wallets/ICP?ask", disagreeing)
    assert status == 200
    assert "DISAGREES" in body and "DIFFERENT identifier" in body

    # AND A LEDGER THAT WILL NOT ANSWER IS `unknown`, NOT a disagreement: a page that
    # cannot reach the replica must not report that the derivation is wrong.
    down = {"ICP": LedgerAdapter(derivation=RuntimeError("dfx: command not found"))}
    cells = {cell.label: cell for cell in panel.chain_panel(
        _app(down).config, down, "ICP", ask=True)["qt"]["status"]}
    assert cells["Derivation"].state == layout.CELL_UNKNOWN, cells["Derivation"]
    assert "dfx: command not found" in cells["Derivation"].note


def test_the_icp_address_cell_answers_with_the_ledger_DOWN():
    """own_address() is a SHA-224 derivation over a principal. It needs no replica.

    Which is why it is the first cell: an operator who can see nothing else should still
    be able to read the account the rest of the panel is about. It answers with the
    replica stopped, with dfx absent from the image, and after the request budget is
    spent.

    MUTATION: move the address into the budgeted read loop in
    services/chain_panel.icp_live(). The second assertion fails.
    """
    down = {"ICP": LedgerAdapter(balance=RuntimeError("dfx: command not found"),
                                 fee=RuntimeError("dfx: command not found"),
                                 derivation=RuntimeError("dfx: command not found"))}
    cells = {cell.label: cell for cell in panel.chain_panel(
        _app(down).config, down, "ICP", ask=True)["qt"]["status"]}
    assert cells["Account"].state == layout.CELL_OK, cells["Account"]
    assert cells["Account"].value == "a" * 64
    assert cells["Balance"].state == layout.CELL_UNKNOWN
    assert "could not be read" in cells["Balance"].value


def test_no_region_on_any_chains_qt_panel_is_blank(client_assets=SIX_CHAINS):
    """Rule 14 over the arrangement: every heading has something under it.

    tests/test_chain_panel.py makes this assertion over a render with NO adapters. This
    makes it over a render WITH them, which is the case the arrangement introduced:
    eleven new sections, each of which could render its live branch into nothing.

    MUTATION: delete any `{% else %}` empty-state block, or the `unasked()` macro call
    from any section. The heading it belongs to then has nothing under it.
    """
    for asset in client_assets:
        for query in ("", "?ask"):
            status, body = render(f"/admin/wallets/{asset}{query}", _adapters())
            assert status == 200, asset
            sections = re.split(r"<h2>", body)[1:]
            assert sections, f"{asset} rendered no sections at all"
            for section in sections:
                heading, _, rest = section.partition("</h2>")
                text = re.sub(r"<[^>]+>", " ", rest).strip()
                assert len(text) > 6, (
                    f"{asset}{query}: the section headed {heading.strip()!r} renders {text!r}, "
                    f"which is a blank region -- a reader cannot tell that from a query that broke"
                )


def test_the_arrangement_adds_NO_chain_call(monkeypatch):
    """`?ask` must cost exactly what it cost before this page grew a rail.

    THE BUDGET ARITHMETIC THE PAGE PRINTS DEPENDS ON IT. services/chain_panel.py's
    PANEL_BUDGET_SECONDS comment counts seven reads at a 30s adapter timeout against
    gunicorn's 60s worker timeout, and the panel prints the budget, the per-call timeout
    and the worst case beside the ask link. A layout module that made one extra call
    would make all three of those figures wrong, and the failure mode is the one
    services/admin_view.py recorded on 2026-10-08: HTTP 500 after 60.18s with an empty
    body, one worker killed, nothing learned.

    MEASURED BY COUNTING WHAT THE ADAPTER WAS ASKED, not by reading the module. The
    stand-in records every call.

    MUTATION: call `adapter.deposit_address(0)` from icp_status_bar(). The ICP assertion
    fails, naming the extra call.
    """
    core = Answering("BTC", CORE_COMPLETE)
    result = panel.chain_panel(_app({"BTC": core}).config, {"BTC": core}, "BTC", ask=True)
    # 8 is services/chain_panel.core_live()'s own counted figure: seven panes plus the
    # chain_network() call it pays for deliberately. The panel's own tally is the
    # authority and this asserts the two agree, so an extra call shows up as both.
    assert result["live"]["rpc_calls"] == len(core.calls), (
        f"the panel counted {result['live']['rpc_calls']} calls and the adapter recorded "
        f"{len(core.calls)}: {core.calls}"
    )
    ledger = LedgerAdapter()
    icp = panel.chain_panel(_app({"ICP": ledger}).config, {"ICP": ledger}, "ICP", ask=True)
    assert icp["live"]["rpc_calls"] == 3, icp["live"]["rpc_calls"]
    # own_address is local and is not a call the budget counts; the three reads are.
    assert [name for name in ledger.calls if name != "own_address"] == [
        "get_balance", "chain_fee", "verify_derivation"], ledger.calls
    translated = Accounted(XRP_ANSWERING)
    panel.chain_panel(_app({"XRP": translated}).config, {"XRP": translated}, "XRP", ask=True)
    methods = [method for method, _params in translated.calls]
    assert methods == ["account_info", "account_tx", "peers", "server_info"], methods
