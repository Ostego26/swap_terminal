#!/usr/bin/env python3
"""One chain's wallet, as a Core GUI would show it -- and the equivalent for the three without one.

Role: submodule -> function (every decision behind /admin's chain tab; routes/admin.py
      is thin and templates/admin_chain.html decides nothing)
Reads: config.Config through the dict the caller passes, the adapters
      chains/registry.build_adapters() produced, and -- ONLY when `ask=True`, never
      on an ordinary render -- a bounded number of read-only calls per chain
Writes: NOTHING. No INSERT, no UPDATE, no file, no environment variable. It does not
      even read the database: every swap-level fact an operator needs is already on
      /admin and this page is about the DAEMONS.
Can move funds: no, and the enforcement is structural rather than asserted. On a
      bitcoin-family chain every method called is in chains/daemon_wallet.READ_ONLY_RPCS
      and tests/test_chain_panel.py collects what a real pane asked a recording
      adapter to prove it. On XRP and SOL every name goes through a command map whose
      entries are reads by construction (chains/rpc_translation.py's header). On ICP
      the three calls are ICRC-1 queries. Nothing here signs, submits, unlocks,
      derives a key, or reads one.
Mainnet-safe: yes to import and to render. `ask=True` makes read-only calls against
      whatever endpoint is configured, which on a mainnet endpoint reads a mainnet
      balance and changes nothing -- and the panel RENDERS the network the daemon
      reports (chains/daemon_network.test_network_verdict) so an operator can see
      which they are looking at.
Live-safe: yes. It starts and stops nothing, holds no lock, and the whole surface is
      GET.

=============================================================================
WHY THIS PAGE EXISTS. Operator, 2026-10-10, having asked more than once:
=============================================================================

    "basically recreate the gui wallets of btc, grc, and ltc, and we also need a
     control panel for xrp, sol, and icp that are cromulent"
    "this should be a tab under the admin and 6 sub tabs for each chain"

WHAT A READ-ONLY SURFACE CAN AND CANNOT BE, stated first because it is the shape of
everything below. Bitcoin Core's GUI has six tabs and two of them WRITE: Send signs
and broadcasts, and Receive calls `getnewaddress`, which derives and stores a key in
wallet.dat. /admin has no write route at all -- routes/admin.py registers GET only
and tests/test_web_surfaces.py asserts that over the app's real url_map rather than
by reading the file -- so this page can carry Core's four READ panes and neither of
its two write ones.

THAT IS NOT PAPERED OVER. absent_capabilities() below is rendered on every chain's
panel and names each missing capability, why it is missing, and the tool at the
repository root that has it. Rule 14 is explicit that a blank is ambiguous between
"zero" and "the query broke"; a wallet GUI missing half its tabs with nothing saying
so is that failure at the scale of a whole feature, and an operator would reasonably
conclude the page was broken rather than bounded.

=============================================================================
THREE PANEL KINDS, AND THE SIX CHAINS ARE NOT INTERCHANGEABLE.
=============================================================================

  core_wallet   BTC, LTC, GRC. A Bitcoin Core release and a wallet daemon, so
                chains/daemon_wallet.py's four panes ARE the Core GUI's four read
                tabs, asked of the real daemon. They are still not the same build:
                chains/daemon_capabilities.py records what each one lacks and the
                evidence for each row, and differences_for() is rendered per chain so
                "not reported" never has to be read as "zero".
  translated    XRP, SOL. No Core, no wallet daemon, no keystore -- an account is a
                row on a ledger. The SAME four questions are asked through
                chains/rpc_translation.py, which is the mapping the operator asked
                for on 2026-09-30, and the questions with no answer on that chain
                come back with the map's own reason instead of a blank.
  no_rpc        ICP. Not JSON-RPC at all: chains/icp.py reaches the ledger by
                shelling out to `dfx` with a Candid argument, so there is no
                `{method, params}` to translate and no map to write. Its panel is the
                three ICRC-1 queries that answer the same operator questions, plus
                the canister facts that are knowable without asking anything.

DERIVED, NOT LISTED. panel_assets() reads Config.RPC's keys and orders them by
chains/daemon_capabilities.BITCOIN_FAMILY; panel_kind() answers from BITCOIN_FAMILY
and chains/rpc_translation.CONSOLE_PROTOCOL. A hand-written `("BTC", "GRC", "LTC",
"XRP", "SOL", "ICP")` here would be the SEVENTH spelling of a chain tuple in this
tree, and OPEN_FINDINGS' C36 is the recorded cost of writing the sixth -- in the
commit whose entire purpose was consolidating duplicated chain knowledge.

=============================================================================
SIX ROUTES' WORTH OF PAGES, ONE AT A TIME, AND WHY NOT ONE PAGE WITH SIX PANELS.
=============================================================================

templates/admin.html's own tab strip is in-document anchors and its comment records
the measurement that decided it: hiding panels makes a browser copy return 923 of
12,502 characters with none of the headings, and this operator reads these pages by
pasting them back. So a six-panel page could not hide five of them -- and a six-panel
page that asks SIX daemons on one render is the blinking cursor rule 14 opens with.
Six chains times up to seven reads times a 30s adapter timeout is twenty minutes in
one request, against gunicorn's 60s worker timeout; services/admin_view.py carries
the measurement of what that actually did on 2026-10-08 (`HTTP 500 after 60.18s with
an empty body`, one worker killed, nothing learned).

So: one chain per URL, the sub-tabs are links between those URLs, and a paste of any
one of them contains that chain's whole panel with nothing hidden.

=============================================================================
AND THE DAEMON IS NOT ASKED UNTIL SOMEBODY ASKS.
=============================================================================

The panel renders from configuration and from the capability tables with NO socket at
all, exactly as /admin does and for the reason routes/admin.py sets out at length. The
live half arrives only on `?ask`, and the page says BEFORE the click how many calls it
will make, what the budget is, and what the worst case costs -- which is rule 14's
"announce before, not only after", at the one place where the alternative is an
operator pressing Ctrl-C on a page that was working.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from time import monotonic
from typing import NamedTuple

from chains import daemon_wallet, rpc_translation
from chains.daemon_capabilities import (
    BITCOIN_FAMILY,
    differences_for,
    wallet_path_warning,
)
from chains.daemon_network import (
    bech32_prefix_status,
    chain_network,
    is_named,
    sync_verdict,
    test_network_verdict,
)
from microfortnights import format_duration
from services.admin_view import chain_rows, no_probe_reason, probe_kind
from services.asset_identity import color_class_for, symbol_for, symbol_title_for

# THE QT SHAPE, 2026-10-10. Operator: "make each sub tab for each chain daemon to look
# almost identical to it's qt core gui wallets."
#
# A SECOND MODULE AND NOT MORE OF THIS ONE, for the reason rule 10 gives about decision
# distance: this file answers "what can this chain tell me", and that one answers "where
# does a Core user look for it". They are different questions with different per-chain
# tables, and this file was already 1111 lines. Everything over there is pure and takes
# the payload below as its input, so a test can seed a `live` dict and assert on the
# rail, the balance card, the columns and the status bar without a Flask app -- which is
# the half of this that could not be tested while the arrangement lived in markup.
from services.chain_wallet_layout import Asking, qt_layout
from services.deadline import MINIMUM_USEFUL_CALL_SECONDS, call_timeout
from services.swap_service import TAG_ATTRIBUTION

#: Which shape of panel a chain gets. NAMED, so a template with one block per kind can
#: be tested for covering them all rather than discovering a gap on an operator's
#: screen -- the same guard chains/daemon_wallet.WALLET_STATES and
#: chains/daemon_network.SYNC_STATES carry, for the same reason.
PANEL_KIND_CORE = "core_wallet"
PANEL_KIND_TRANSLATED = "translated"
PANEL_KIND_NO_RPC = "no_rpc"
PANEL_KINDS = (PANEL_KIND_CORE, PANEL_KIND_TRANSLATED, PANEL_KIND_NO_RPC)


def panel_assets(config: Mapping) -> tuple[str, ...]:
    """The six chains, in the order the sub-tabs show them. DERIVED from two authorities.

    Config.RPC's KEYS are the membership: a chain this application has an RPC entry
    for gets a panel, configured or not, because "BTC is not configured" is the answer
    to a question an operator would otherwise ask by reading source (rule 14 -- an
    absent tab is indistinguishable from a tab nobody rendered).

    chains/daemon_capabilities.BITCOIN_FAMILY is the ORDER: the three with a Core GUI
    first, in that tuple's own order, then everything else in Config.RPC's order. The
    grouping is the point rather than the alphabet -- the first three panels are the
    same four panes asked of three daemons, and the last three are each a different
    kind of thing, so a reader moving left to right crosses one boundary instead of
    three.

    NEITHER HALF IS SPELLED HERE. BITCOIN_FAMILY is itself derived from
    chains/coin_amounts.CHAIN_DECIMALS, and its own comment records that writing
    `("BTC", "LTC", "GRC")` by hand there made it the SIXTH copy of that tuple -- found
    only by going looking afterwards, in the commit whose purpose was removing copies
    (OPEN_FINDINGS C36). A seventh here would be the same mistake with the same
    excuse.

    Takes the CONFIG MAPPING rather than importing config.Config, so it can be called
    with a seeded dict and so a route passes the app's own config (rule 10). A config
    with no RPC key answers () rather than raising: the honest answer for "this
    application knows no chains" is an empty strip, which the caller renders as a
    refusal naming the key.
    """
    configured = tuple(config.get("RPC") or ())
    family = tuple(asset for asset in BITCOIN_FAMILY if asset in configured)
    return family + tuple(asset for asset in configured if asset not in family)


def panel_kind(asset: str) -> str:
    """Which of PANEL_KINDS this chain gets. One of three, decided from the tables.

    ASKED OF THE CHAIN AND NOT OF ITS ADAPTER, which is the one place this
    deliberately differs from services/admin_view.probe_kind() -- and rule 8 asks for
    the difference at both sites, so that function's docstring is the other half.

      probe_kind(adapter)  "what can I ask this OBJECT". Decided by what the adapter
                           HAS, so a chain with no adapter has no answer at all.
      panel_kind(asset)    "what SHAPE of panel does this chain's story take". It has
                           to answer for an UNCONFIGURED chain, because the panel's
                           whole job on an unconfigured chain is to explain what would
                           be there -- which pane set, which capability table, which
                           tool. An adapter-shaped question returns None there and the
                           page would have nothing to say.

    A chain in neither table is PANEL_KIND_NO_RPC, which is the fail-closed direction:
    it claims no translation and no wallet, and the panel then says what is actually
    known rather than rendering empty Core panes for a chain that has no Core.
    """
    asset = asset.upper()
    if asset in BITCOIN_FAMILY:
        return PANEL_KIND_CORE
    if asset in rpc_translation.CONSOLE_PROTOCOL:
        return PANEL_KIND_TRANSLATED
    return PANEL_KIND_NO_RPC


def named_or_first(asset: object, known: tuple[str, ...]) -> object:
    """Which chain a URL means: the one it names, or the FIRST when it names none.

    /admin/wallets and /admin/wallets/<asset> are one view, because the operator tab
    strip needs a single stable href and a macro in templates/ cannot know which chain
    is first -- that is panel_assets()' answer and it depends on Config.RPC. So the bare
    URL means "the first panel" and the strip links to it.

    A NON-CHAIN NAME PASSES STRAIGHT THROUGH to refuse_unknown_asset(), and that is the
    whole reason this is separate from it. Folding the two would make /admin/wallets/BTX
    render BTC's panel under a URL naming a chain that does not exist -- a page that
    quietly shows something other than what was asked for, which is worse than a
    refusal because the operator has no way to notice it happened.

    WITH NO CHAINS CONFIGURED AT ALL it returns the input unchanged, so what the reader
    gets is the refusal that names an empty list rather than an IndexError inside a
    route.
    """
    if (not isinstance(asset, str) or not asset.strip()) and known:
        return known[0]
    return asset


def refuse_unknown_asset(asset: object, known: tuple[str, ...]) -> str:
    """"" if this is a chain with a panel, else the sentence saying it is not.

    A NAMED REFUSAL RATHER THAN A 404 WITH NO BODY OR A BLANK PANEL, and it names the
    six so the operator's next action is a click rather than a question. The route
    still answers 404 -- the URL genuinely identifies nothing -- but with this
    sentence in it, which is the difference between "you mistyped BTX" and a page that
    looks broken.

    THE INPUT IS `object`, NOT `str`. This is reached from a URL path segment, so it
    arrives as whatever a browser sent; a non-string or an empty segment is refused by
    the same sentence rather than raising `AttributeError: 'NoneType' has no attribute
    'upper'` three frames inside a template.
    """
    if not isinstance(asset, str) or not asset.strip():
        return (
            f"no chain was named in this URL, so there is no panel to show. "
            f"The chains with a panel are {', '.join(known) or '(none -- see below)'}."
        )
    if asset.upper() not in known:
        return (
            f"{asset.upper()} is not a chain this application has an RPC entry for, so there is "
            f"no panel for it. The chains with one are {', '.join(known) or '(none)'} -- that "
            f"list is Config.RPC's own keys, so a chain missing from it is missing from the "
            f"application and not just from this page."
        )
    return ""


class Pane(NamedTuple):
    """One question a Core wallet GUI answers, under the bitcoin method that answers it.

    THE QUESTION IS THE BITCOIN METHOD NAME, on every chain, which is what makes one
    pane list serve all six. On a bitcoin-family daemon it is sent as-is; on XRP and
    SOL chains/rpc_translation.py turns it into that chain's equivalent and carries
    the map's own note about how the two differ. A per-chain question list would be
    three lists that agree on the day they are written (rule 8), and the thing that
    varies is not the question -- it is the answer.
    """

    question: str
    title: str
    what: str


#: WHAT BITCOIN CORE'S GUI SHOWS WITHOUT WRITING ANYTHING: its four read tabs --
#: Overview, Transactions, Peers, Information -- plus the two figures its status bar
#: carries, the spendable balance and the connection count. In the order a reader goes
#: down the page.
#:
#: BOTH OF `getwalletinfo` AND `getbalance` ARE HERE AND THAT IS NOT REDUNDANT, though
#: it looks it on a Bitcoin daemon where one response carries the other's number:
#:
#:   getwalletinfo       the three numbers the Overview pane shows (available, pending,
#:                       immature) plus txcount and the lock field. On XRP it maps to
#:                       `account_info` and ANSWERS `account_data` -- the whole account
#:                       row.
#:   getbalance          one number, and on XRP it maps to the SAME call answering
#:                       `account_data.Balance` -- with the map's note attached, which
#:                       is the one an operator needs and which nothing else on this
#:                       page says: the figure is in DROPS, and the base reserve plus
#:                       the owner reserve is not spendable at all.
#:
#: `getpeerinfo` AND `getconnectioncount` ARE BOTH HERE FOR THE SAME REASON, and the
#: second is the one that will actually answer on XRP. chains/xrp_rpc_map.py marks
#: `peers` ADMIN-ONLY: "A public JSON-RPC endpoint refuses it, and that refusal comes
#: from the server rather than from here -- the count alone is in server_info.peers,
#: which every endpoint answers." A panel carrying only the detail question would report
#: "am I connected to anybody" as a permission error on the ordinary deployment.
#:
#: THE PAIRS FOLD BACK TOGETHER ON A TRANSLATED CHAIN, which is what distinct_questions()
#: is for -- measured against the real maps, 2026-10-10: six questions become FOUR calls
#: on XRP (account_info and server_info each answer two) and FIVE on SOL (getClusterNodes
#: answers two). On a bitcoin-family chain they do not fold, because chains/daemon_wallet
#: .wallet_pane() asks its own fixed set and this list is what the page is LABELED with
#: there rather than what it sends.
PANE_QUESTIONS: tuple[Pane, ...] = (
    Pane("getwalletinfo", "Overview",
         "what this wallet or account holds, and whether it is locked"),
    Pane("getbalance", "Spendable",
         "the one number a send can draw on -- read the note, the units differ per chain"),
    Pane("listtransactions", "Transactions",
         "did the coin arrive, and is it confirmed yet"),
    Pane("getpeerinfo", "Peers",
         "is this node connected to anybody -- a node with no peers broadcasts into nothing"),
    Pane("getconnectioncount", "Connections",
         "how many peers, which is the half a public endpoint will answer when the "
         "per-peer detail above is admin-only"),
    Pane("getblockchaininfo", "Information",
         "which network, how far synced, which build"),
)


class Asked(NamedTuple):
    """One pane question, resolved against a chain's command map. Pure data.

    `chain_method` is what will actually be sent, `shared_with` names the EARLIER pane
    question that already sends it (and is "" when this one is asked on its own), and
    `refusal` is the map's own reason when that chain cannot answer at all.
    """

    pane: Pane
    chain_method: str
    shared_with: str
    refusal: str


def distinct_questions(module, questions: tuple[Pane, ...] = PANE_QUESTIONS) -> list[Asked]:
    """Which pane questions become which calls, with the duplicates folded. PURE.

    THE DEDUPLICATION IS NOT A MICRO-OPTIMIZATION, IT IS MEASURED ROUND TRIPS. rippled
    never split its `getinfo` the way bitcoind did -- chains/xrp_rpc_map.py says so in
    the comment above its own table -- so `getblockchaininfo` and `getconnectioncount`
    both map to `server_info`, and `getwalletinfo` and `getbalance` both map to
    `account_info`. Asking the pane questions naively would send each of those twice, to
    the same endpoint, inside one page render. Counted against the real maps on
    2026-10-10:

        XRP   6 questions -> 4 calls   (account_info x2, server_info x2 folded)
        SOL   6 questions -> 5 calls   (getClusterNodes x2 folded)

    AND THE FOLD IS VISIBLE RATHER THAN SILENT, which is the half that matters to a
    reader. `shared_with` names the pane whose call this one is reading, so the page can
    say "answered by the same server_info as Overview" instead of printing one figure
    twice with nothing saying they are one measurement. Two panes rendering the same
    numbers from two independent calls could legitimately DISAGREE -- the ledger
    advances between them -- and a reader cannot tell that from this page unless the
    page says which.

    KEYED ON THE CHAIN METHOD ALONE, AND THAT IS SOUND ONLY BECAUSE OF WHAT THIS LIST
    IS. Every question in PANE_QUESTIONS is asked with the same account and no
    per-question argument, so two questions that resolve to one method resolve to one
    identical CALL. A question list carrying its own arguments -- `getblock` at two
    heights, say -- would need the params in the key, and this would quietly return one
    height's answer for both. The assertion lives in tests/test_chain_panel.py rather
    than in this paragraph.

    `module` is a command map (chains/xrp_rpc_map or chains/solana_rpc_map) or None.
    None answers every question with the no-map refusal, so a caller needs no branch.
    """
    resolved: list[Asked] = []
    seen: dict[str, str] = {}
    for pane in questions:
        if module is None:
            resolved.append(Asked(pane, "", "", (
                "there is no command map for this chain, so this question cannot be translated "
                "into anything to send"
            )))
            continue
        refusal = module.refuse_without_equivalent(pane.question)
        if refusal:
            resolved.append(Asked(pane, "", "", refusal))
            continue
        entry = module.equivalent_of(pane.question)
        chain_method = entry.method
        resolved.append(Asked(pane, chain_method, seen.get(chain_method, ""), ""))
        seen.setdefault(chain_method, pane.title)
    return resolved


class Absent(NamedTuple):
    """One thing a Core GUI has that this page does not, why, and what does have it."""

    what: str
    why: str
    instead: str


#: WHAT THIS PANEL CANNOT DO, NAMED ON EVERY CHAIN'S PAGE.
#:
#: Rule 14's "(none) is a result; a blank gap is ambiguous" applied to a capability
#: rather than to a table cell. A wallet GUI with no Send tab and no Receive tab, and
#: nothing on the page saying why, invites exactly one conclusion -- that the page is
#: broken or half-built -- and the operator's next move is to go looking for the button.
#:
#: EVERY `instead` IS A FILE THAT EXISTS AT THE REPOSITORY ROOT, checked by
#: tests/test_chain_panel.py rather than trusted: rule 2's "grep the tree for the NAME"
#: in the other direction, because advice naming a tool that has moved is worse than no
#: advice at all. It sends the reader to a path that does not exist and they conclude
#: the capability is gone.
_ABSENT: tuple[Absent, ...] = (
    Absent(
        "Send",
        "this surface registers no write method of any kind -- routes/admin.py is GET only "
        "and tests/test_web_surfaces.py asserts that over the app's real url_map. Spending is "
        "CLAUDE.md rule 16's live posture: the operator's call once measured, never a button "
        "that appears while a viewer is being built",
        "settle_payout.py, rescue_payout.py and fund_desk.py at the repository root; "
        "/admin/controls is the only page in this application that takes a POST at all, and "
        "what it does is start and stop the supervised workers",
    ),
    Absent(
        "Receive",
        "a Receive tab is `getnewaddress`, which DERIVES AND STORES A KEY in wallet.dat. That "
        "is a wallet write, not a read -- it is deliberately absent from "
        "chains/daemon_wallet.READ_ONLY_RPCS, a staking-only wallet may refuse it, and it "
        "changes the file the operator backs up. It would also need a POST, which this "
        "surface does not have",
        "testnet_wallets.py at the repository root -- `python3 testnet_wallets.py --all` "
        "prepares the wallet this application reads and derives a receiving address for it",
    ),
    Absent(
        "Unlock / passphrase",
        "a passphrase must never appear in a command this repository emits, and no page here "
        "may have a field for one: `walletpassphrase` takes it as an ARGUMENT, so a form would "
        "put it in a POST body, the browser's autofill and this server's request log. The lock "
        "STATE is reported below -- that is the fact that decides whether a payout can be made "
        "at all -- and nothing on this page offers to change it",
        "fund_desk.py, which unlocks for one payout and always puts the wallet back (on GRC it "
        "restores STAKING rather than merely locking -- chains/gridcoin_wallet_lock.py)",
    ),
    Absent(
        "Start / stop this daemon",
        "rule 13 and the live-safety rules: nothing in this application starts or stops a "
        "chain daemon. This page never started them, so it knows no binary, no datadir and no "
        "service manager, and inventing a command line for the process that stakes the "
        "operator's wallet is the guess rule 17 forbids",
        "your own shell. /admin/controls stops and starts the three SUPERVISED WORKERS and "
        "proves each is gone -- those are this application's own processes, not the daemons",
    ),
)


def absent_capabilities(kind: str) -> tuple[Absent, ...]:
    """What is missing from this panel, for this kind of chain. Never empty.

    THE LIST IS FILTERED BY KIND RATHER THAN PRINTED WHOLE, because two of the four
    entries are claims about a WALLET DAEMON and XRP, SOL and ICP have none. Telling an
    operator that the XRP panel has no passphrase field "because walletpassphrase takes
    one as an argument" would be a true sentence about a method that chain has never
    had -- rippled holds no wallet at all, which chains/xrp_rpc_map.py's NO_EQUIVALENT
    entry for `listwallets` says at length. A reason that does not apply to the chain
    it is printed under is the wrong-remedy defect chains/daemon_capabilities.Capability
    .when_unused records costing an operator a wrong answer during an outage.

    Send stays on every kind, because every chain here can be a payout destination or
    is explicitly refused as one, and that refusal is on /admin's chain row.
    """
    if kind == PANEL_KIND_CORE:
        return _ABSENT
    return tuple(
        entry for entry in _ABSENT
        if entry.what not in ("Unlock / passphrase", "Start / stop this daemon")
    )


#: HOW LONG ONE `?ask` MAY SPEND TALKING TO A CHAIN, in seconds.
#:
#: THE ARITHMETIC, because the point of a budget is that somebody added it up
#: (services/deadline.py's header: "a per-call timeout bounds a call, and only a
#: deadline bounds a request"):
#:
#:     gunicorn worker timeout          60s   gunicorn.conf.py
#:     one adapter call, BTC/LTC/GRC    30s   Config.RPC[asset]["timeout"] default
#:     calls one core panel makes        7    counted, not claimed -- see
#:                                            daemon_wallet._Counted
#:     7 x 30                          210s   which is what no budget costs
#:
#: 20s is deliberately well under 60 rather than just under it, because the deadline is
#: checked BEFORE a call starts and cannot interrupt one in flight: the worst case is
#: the budget plus one whole per-call timeout, 20 + 30 = 50s, and that is the number
#: that has to fit. A 45s budget would have been 75s worst case -- over the worker
#: timeout, which is exactly the failure this is copied from: /api/admin/chains
#: returned HTTP 500 after 60.18s with an empty body on 2026-10-08, one worker killed
#: and nothing learned about which chain was down.
#:
#: The page PRINTS both figures beside the ask link, with the per-call timeout read off
#: the adapter rather than assumed, so an operator who raised <ASSET>_RPC_TIMEOUT sees
#: what their own configuration costs (rule 14: echo the parameters that decide the
#: answer).
PANEL_BUDGET_SECONDS = float(os.getenv("ST_CHAIN_PANEL_BUDGET_SECONDS", "20"))

#: The environment variable that raises it, named once so the page and this module
#: cannot disagree about its spelling (rule 8). ASCII, like every other variable name
#: in this tree.
PANEL_BUDGET_VARIABLE = "ST_CHAIN_PANEL_BUDGET_SECONDS"


class Budget(NamedTuple):
    """How long one render may spend talking to a chain, and the clock that measures it.

    TWO FIELDS THAT TRAVEL TOGETHER, and one object rather than two parameters for the
    reason regtest/daemons.WalletSite gives about its three: they are not independent.
    Every function below that can make a call needs both -- the deadline to compare
    against and the clock to compare with -- and passing them separately took
    chain_panel() to six parameters, which ruff's PLR0913 flagged. Rule 12 says that is
    the layering talking and the fix is to extract, not to raise the ceiling.

    `now` IS INJECTABLE SO A TEST CAN EXHAUST THE BUDGET WITHOUT SLEEPING, and it is
    monotonic() rather than time() for the reason services/admin_view.probe_chains()
    gives about the same deadline: a wall-clock step -- an NTP correction mid-render --
    would otherwise move the deadline under it.
    """

    seconds: float
    now: Callable[[], float] = monotonic

    def start(self) -> Deadline:
        """The absolute moment this budget runs out, measured from NOW."""
        return Deadline(self.now() + self.seconds, self)


class Deadline(NamedTuple):
    """An absolute deadline, and the budget it came from so a refusal can name it.

    THE BUDGET RIDES ALONG RATHER THAN BEING LOOKED UP FROM THE MODULE CONSTANT, which
    is what the first version of the refusal message did -- so a test passing a 0.0s
    budget produced a sentence telling the operator about the 20s default. A message
    that names a figure the run did not use is rule 14's wrong-number defect, and here
    it would have been wrong in exactly the runs somebody was investigating.
    """

    at: float
    budget: Budget

    def may_call(self) -> bool:
        """Is there enough budget left to be worth starting a call? Pure of side effects.

        services/deadline.call_timeout() is the decision and it is NOT reimplemented
        here: it owns "a budget is a ceiling, and the last call must not be allowed to
        put the total back over the server's limit", with its own tests. This is the
        boolean form of its answer.
        """
        return call_timeout(self.at, self.budget.now(), MINIMUM_USEFUL_CALL_SECONDS) > 0.0

    def spent_note(self, what: str) -> str:
        """The sentence a refused call renders. Rule 14: which number, and what it means."""
        return (
            f"NOT ASKED -- this request's {self.budget.seconds:.0f}s budget for talking to the "
            f"chain was already spent, so {what} was not started. This says NOTHING about "
            f"whether the chain would have answered; it says nobody asked. At least one call "
            f"above it was slow or timed out -- find it there, not here. "
            f"{PANEL_BUDGET_VARIABLE} raises the ceiling"
        )


class BudgetSpent(RuntimeError):
    """Raised INSTEAD of starting a call once this request's budget is gone.

    AN EXCEPTION AND NOT A RETURN VALUE, which is the opposite of what rule 12 asks for
    almost everywhere else, and the reason is where it has to be caught. Every pane in
    chains/daemon_wallet.py already routes every failure through `_read()`, which turns
    any exception into `(None, "TypeName: reason")` and renders it as that pane's own
    sentence. Raising therefore reaches the operator as "Overview could not be read:
    BudgetSpent: ..." in the pane that did not get asked, with the other panes intact --
    and it required NO change to four functions that were already correct.

    A return value would have meant threading a deadline through `_read`, `wallet_state`,
    `wallet_balances`, `recent_transactions`, `peer_rows` and `node_summary`, changing
    six signatures and the regtest panel that calls them, to express something the
    adapter boundary can express on its own.
    """


class BudgetedNode:
    """One adapter, refusing to START a call once the deadline has passed.

    THE BUDGET IS ENFORCED WHERE A CALL BEGINS, which is the only place that bounds a
    request: a check between PANES would let the three calls inside wallet_state() run
    unbounded, and that is 90s on its own at a 30s per-call timeout.

    It counts as well as refuses, so `calls` is the number of round trips this render
    actually cost -- the figure an operator watching their own daemon's log needs in
    order to account for the traffic this page generates. See
    chains/daemon_wallet._Counted for the two hand-written literals that were both
    wrong before counting replaced them.

    IT POLICES NOTHING ELSE. `method` is forwarded unexamined: the allowlist is
    chains/daemon_wallet.READ_ONLY_RPCS and the panes' fixed call sites, and a second
    gate hidden inside a timer would be a place for the two to disagree about what is
    permitted (rule 8).
    """

    def __init__(self, adapter, deadline: Deadline) -> None:
        self._adapter = adapter
        self._deadline = deadline
        self.calls = 0

    def may_call(self) -> bool:
        """Is there enough budget left to be worth starting a call?"""
        return self._deadline.may_call()

    def call(self, method: str, *params):
        if not self.may_call():
            raise BudgetSpent(self._deadline.spent_note(method))
        self.calls += 1
        return self._adapter.call(method, *params)


def per_call_seconds(adapter) -> float | None:
    """The adapter's own per-call timeout, or None when it does not carry one.

    READ OFF THE ADAPTER, NEVER ASSUMED, because it is configurable per chain
    (<ASSET>_RPC_TIMEOUT) and the worst-case arithmetic printed beside the ask link is
    only true for the value this deployment actually has. None renders as NOT
    ESTABLISHED rather than as a default: an adapter with no `timeout` attribute has
    not told us, and printing 30 for it would be a measurement nobody took (rule 17).
    """
    timeout = getattr(adapter, "timeout", None)
    return float(timeout) if isinstance(timeout, (int, float)) else None


def worst_case_seconds(adapter, budget: float) -> float | None:
    """Budget plus one whole per-call timeout, or None when the timeout is unknown.

    ONE PER-CALL TIMEOUT AND NOT N, and that is the whole reason this is a function
    with a name. The deadline is checked before a call STARTS, so the last call allowed
    through may run its full timeout after the budget is otherwise exhausted -- but
    only one can, because the next check fails. Adding N timeouts would overstate it by
    a factor of seven and adding none would be the unbounded number the budget exists
    to replace.
    """
    per_call = per_call_seconds(adapter)
    return None if per_call is None else budget + per_call


def core_live(adapter, asset: str, deadline: Deadline) -> dict:
    """The four Core read panes, asked of a real daemon. The bitcoin-family live half.

    chains/daemon_wallet.wallet_pane() DOES THE WORK and this wraps it in exactly two
    things it does not know about: the request budget (BudgetedNode) and the
    `run`-shaped handle it takes (daemon_wallet.OneNode). That is the entire adaptation
    -- which is the argument for having extracted those functions rather than written
    a second set for this surface.

    `network` AND `sync` COME FROM THE SAME AUTHORITIES /admin's probe USES rather than
    being re-derived here: chain_network() for the name and test_network_verdict() for
    whether that network may be touched at all. The pane's Information tab reports the
    daemon's own fields; these two answer the questions a FIGURE cannot -- "is this the
    chain I think it is" and "is a balance of 0 a sync rather than an empty wallet".

    chain_network() COSTS ONE EXTRA getblockchaininfo AND THAT IS A DELIBERATE TRADE,
    counted: a core ask is 8 calls, of which 7 are the panes and the 8th is this one
    asking a question node_summary() already has an answer to. It could be avoided by
    reading `chain` out of the pane's own payload -- and that would be route ONE of
    chain_network()'s two, written a second time, without route two. Route two is
    `getinfo.testnet`, the boolean a pre-0.17 build answers with, and GRC's
    getblockchaininfo HAS NO `chain` KEY at all: services/admin_view._ask_network()
    records what that cost on the operator's screen on 2026-09-30, when a second
    implementation of this same question rendered the sentence "answered, but reported
    no chain name" AS a network name, on the one chain that host had configured. This
    is the check that stands between a command and a mainnet wallet (rule 8 names the
    activation gate as the place a duplicate costs the most), so it is asked once,
    through the one function that owns it, and the call is paid for.

    AND IT IS CALLED UNGUARDED WHEN THE BUDGET IS GONE, which costs nothing and is the
    only way the refusal reads correctly. The comment at the call site records the
    measurement that forced it: the guard that used to be there produced a wrong
    REFUSAL rather than no answer, and this docstring asserted the opposite until it
    was run.
    """
    node = BudgetedNode(adapter, deadline)
    pane = daemon_wallet.wallet_pane(daemon_wallet.OneNode(node))
    # RULE 6 AT THE WAY OUT, AND THIS IS THE ONE DURATION IN THE PAYLOAD. A peer's
    # `pingtime` is seconds from getpeerinfo, and rule 6's boundary is "convert on the
    # way *out*, at the print or the row write, not on the way in" -- so the raw value
    # stays where chains/daemon_wallet.peer_rows() put it (a test asserting on a
    # measurement must not have to parse a unit) and the DISPLAY string is added beside
    # it. A template cannot call format_duration(), so a template doing this would mean
    # either a Jinja filter or a hand-written multiplication in markup, and the constant
    # already lives in exactly one place.
    #
    # `conntime` IS DELIBERATELY NOT CONVERTED. It is a unix timestamp -- a moment, not a
    # duration -- and microfortnights since 1970 is not a thing anybody reads. The
    # footer's own sentence draws the same line for confirmation counts and block
    # heights.
    for peer in pane["peers"]["rows"]:
        ping = peer.get("pingtime")
        peer["ping_display"] = (
            format_duration(float(ping)) if isinstance(ping, (int, float)) else ""
        )
    # UNGUARDED ON PURPOSE, AND THE GUARD THAT WAS HERE WAS A MEASURED DEFECT. It read
    # `chain_network(node) if node.may_call() else ""`, and the docstring below it
    # claimed an empty string would fail closed. Run against a spent budget on
    # 2026-10-10 it did the opposite: daemon_network.is_named("") is TRUE -- the
    # sentinel it tests for is the "unknown (" PREFIX, and "" does not carry it -- so
    # test_network_verdict() answered NETWORK_NOT_ALLOWED, which renders as "it named a
    # network and that network is not allowed" about a daemon nobody asked. That is the
    # confidently-wrong-refusal shape this repository has already paid for twice:
    # fund_desk's "*** THIS IS A MAINNET DAEMON ***" printed at a testnet4 node, and
    # admin_view's "it reports its network as answered, but reported no chain name".
    #
    # Calling it unguarded costs NOTHING when the budget is gone, because BudgetedNode
    # refuses both of chain_network()'s two routes and chain_network() never raises: it
    # collects the reason from each and returns its OWN sentinel with both in it. So the
    # page reads "NOT NAMED -- unknown (getblockchaininfo: BudgetSpent; getinfo:
    # BudgetSpent)", the verdict is NETWORK_NOT_ESTABLISHED, and the function that owns
    # the sentinel is the one that built it (rule 8). A guard here was me producing a
    # sentinel value by hand for a vocabulary another module owns.
    network = chain_network(node)
    chain_info = pane["node"]
    return {
        "panes": pane,
        "network": network,
        "network_named": is_named(network),
        "network_verdict": test_network_verdict(asset, network),
        # THE PANE'S OWN getblockchaininfo FIELDS, RESHAPED FOR sync_verdict(), which is
        # pure and takes the raw mapping. node_summary() wraps every value in
        # {"value", "reported"} so the daemon's silence stays visible, and that wrapper
        # is exactly what has to come back off before the verdict can read it -- doing
        # it here rather than giving node_summary a second return shape keeps one
        # answer for "what did the daemon say".
        "sync": sync_verdict({
            key: chain_info[key]["value"]
            for key in ("blocks", "headers", "verificationprogress", "initialblockdownload")
            if key in chain_info and chain_info[key]["reported"]
        }),
        "bech32": bech32_prefix_status(asset, network),
        "rpc_calls": node.calls,
    }


def translated_live(adapter, protocol: str, account: str, deadline: Deadline) -> dict:
    """The same four questions, asked of a chain that is not Bitcoin. XRP and SOL.

    ONE ANSWER PER PANE QUESTION, in PANE_QUESTIONS' order, so the XRP and SOL panels
    read down the page in the same order as the three Core ones. An operator comparing
    two chains is comparing two panels, and a different running order would make that
    comparison a reading exercise.

    THREE OUTCOMES PER PANE AND THEY RENDER DIFFERENTLY (rule 14, and
    chains/rpc_translation.call_translated_read_only() draws the same distinction one
    level down):

      refused     this chain has no equivalent for that question, with the map's own
                  reason. It is a positive finding -- "rippled HOLDS NO WALLET" is an
                  answer -- and never a blank.
      shared      an earlier pane's call already answered it. The answer is reused and
                  the pane says which, so one measurement is never printed twice as if
                  it were two.
      asked       the call was made; `ok` says whether the chain answered.

    `account` IS REQUIRED BY THREE OF THE FOUR QUESTIONS and comes from
    services/swap_service.TAG_ATTRIBUTION -- the table that already owns which config
    variable holds each tag-attributed chain's shared account. An empty account is NOT
    worked around here: call_for() raises MissingArgument, the answer comes back
    `needs=True` with the variable named, and that is the correct outcome because the
    account is a CUSTODY decision with no safe default (config.py says so at length for
    both chains).

    THE DESK'S PAYOUT ACCOUNT IS DELIBERATELY NOT USED, and it is the one that would
    have been tempting. chains/xrp_payout_seed.derived_payout_account() returns a
    public classic address -- but it gets there by DECODING THE SEED, and a read-only
    page must not touch key material to decide what to display. The deposit account is
    configuration, is already on /admin, and is the account the desk's XRP actually
    sits in.
    """
    module = rpc_translation.console_map(protocol)
    answers: list[dict] = []
    by_method: dict[str, dict] = {}
    for asked in distinct_questions(module):
        row = {"pane": asked.pane, "translated": asked.chain_method,
               "shared_with": asked.shared_with, "refusal": asked.refusal}
        if asked.refusal:
            answers.append(row)
            continue
        if asked.shared_with:
            row["answer"] = by_method[asked.chain_method]
            answers.append(row)
            continue
        answer = rpc_translation.call_translated_read_only(
            adapter, protocol, asked.pane.question, account=account,
        )
        by_method[asked.chain_method] = answer
        row["answer"] = answer
        answers.append(row)
    return {
        "protocol": protocol,
        "account": account,
        "answers": answers,
        # COUNTED THE SAME WAY EVERY OTHER COUNT ON THIS PAGE IS: by tallying what was
        # sent, never by len(PANE_QUESTIONS). The fold means the two differ -- 4
        # questions, 3 calls on XRP -- and the whole reason the fold is visible is that
        # a reader must be able to see one call answering three panes.
        "rpc_calls": len(by_method),
    }


def icp_live(adapter, deadline: Deadline) -> dict:
    """ICP's equivalent of the four panes: three ICRC-1 queries and a derivation check.

    WHAT EACH ONE ANSWERS, mapped onto the Core questions it stands in for:

      own_address        the account the desk's inventory sits in. PURE -- it is a
                         SHA-224 derivation over the configured principal, local, no
                         call -- so it is reported even when nothing can be asked.
      icrc1_balance_of   Overview. One number, and ICP has no pending or immature: a
                         transfer is final when the ledger returns a block index.
      icrc1_fee          what a payout from here costs, read from the ledger rather
                         than from a constant (chains/icp.chain_fee()'s own argument).
      verify_derivation  THE CHECK WORTH RUNNING, and it has no Core counterpart at
                         all. It asks the LEDGER to compute the account identifier this
                         repository derived, from the same principal and subaccount. A
                         disagreement means every ICP deposit address this terminal
                         publishes is wrong in the same way -- a customer pays and the
                         watcher polls an account that stays at zero forever, with
                         nothing erroring.

    THERE IS NO TRANSACTIONS PANE AND NO PEERS PANE, and both absences are facts about
    the Internet Computer rather than gaps here. A subaccount's BALANCE is the deposit
    -- services/icp_subaccount_service.py's own argument, because nothing else can pay
    into it -- so there is no per-wallet transaction list to show; and a canister has no
    peer set to report. Each is said in the returned payload rather than left as an
    empty region.

    EVERY CALL IS A SUBPROCESS, NOT A SOCKET, and that is this chain's distinctive
    failure mode. chains/icp.dfx_transport() runs `dfx` -- in this process when
    ICP_DFX_NETWORK_URL is set, which is the only transport the web container has, since
    that image carries dfx and no docker CLI. If dfx is absent or the replica is down
    the error is reported verbatim (redacted by chains/icp.redact_secrets() on the way
    out, which is why nothing here formats a dfx error itself).

    CANISTER IDS ARE NOT ASKED FOR AND MUST NOT BE GUESSED. Every fresh replica issues
    different ones, they live in the replica's own state, and `dfx canister id` is what
    reads them -- which is why templates/_admin_tabs.html names the canister console
    without linking to it. `swap_stack.py status` asks the replica and prints the URLs.
    """
    # BOUND METHODS, NOT LAMBDAS. `lambda: adapter.get_balance()` is what this was and
    # ruff's PLW0108 is right that it adds a frame and a closure for nothing: the bound
    # method IS the deferred call. Deferred at all because the table is the list of
    # QUESTIONS and the loop below decides whether each one is asked -- building it with
    # the calls already made would ask all three before the budget was consulted once.
    reads = (
        ("balance", "Overview -- the desk's own spendable balance, in ICP",
         adapter.get_balance),
        ("fee", "what the ledger charges for a transfer, read from the ledger itself",
         adapter.chain_fee),
        ("derivation", "does the LEDGER agree with this repository's address derivation",
         adapter.verify_derivation),
    )
    node = BudgetedNode(adapter, deadline)
    rows = []
    for key, what, read in reads:
        if not node.may_call():
            rows.append({"key": key, "what": what, "ok": False, "value": None,
                         "error": deadline.spent_note(key)})
            continue
        node.calls += 1
        try:
            rows.append({"key": key, "what": what, "ok": True, "value": read(), "error": ""})
        except Exception as error:  # noqa: BLE001 -- checked: the failure IS the return value and the caller can tell, because `ok` is False and `value` is None. Every way this fails -- dfx missing from the image, the replica down, the canister not deployed, a candid reply this adapter cannot parse -- is one row's worth of reportable fact on a diagnostic page, and chains/icp.py raises at least three distinct types for them. A page that dies because a ledger is down is a page that cannot be used to find out that the ledger is down.
            rows.append({"key": key, "what": what, "ok": False, "value": None,
                         "error": f"{type(error).__name__}: {error}"})
    return {
        "own_address": _icp_own_address(adapter),
        "rows": rows,
        "rpc_calls": node.calls,
        "no_transactions_pane": (
            "the Internet Computer has no per-wallet transaction list to show here: an ICP "
            "deposit IS the balance appearing at an allocated subaccount, because nothing else "
            "can pay into it. /admin's deposit table is where those arrivals are recorded"
        ),
        "no_peers_pane": (
            "a canister has no peer set, so there is no Peers pane to render. Whether the "
            "replica is answering at all is what `swap_stack.py up` checks, by reading "
            "/api/v2/status and refusing to report SERVING until it answers 200"
        ),
    }


def _icp_own_address(adapter) -> dict:
    """The desk's own account identifier, derived locally. {address, error}.

    ITS OWN FUNCTION BECAUSE IT IS NOT A CALL and must not be inside the budgeted loop:
    chains/icp.own_address() is account_identifier(owner_principal), a hash, so it
    answers with the replica down, with dfx absent, and after the budget is spent. An
    operator who can see nothing else should still be able to read the account the rest
    of the panel is about.
    """
    try:
        return {"address": adapter.own_address(), "error": ""}
    except Exception as error:  # noqa: BLE001 -- checked: reported in `error` and the caller renders it; the only way a local derivation fails is a malformed principal in the configuration, which is a reportable configuration fact rather than an outage.
        return {"address": "", "error": f"{type(error).__name__}: {error}"}


def tab_rows(config: Mapping, adapters: Mapping, current: str) -> list[dict]:
    """The six sub-tabs: which chain, which is current, and what each one is.

    `configured` IS ON THE TAB, not only inside the panel, so an operator can see from
    the strip which chains have an adapter at all -- the question they came to ask on a
    host where nothing is answering. The tab is still rendered and still linked for an
    unconfigured chain, because its panel's job there is to say what is missing
    (chains/registry.why_unconfigured() names the variables, through
    services/admin_view.chain_rows()).
    """
    return [
        {
            "asset": asset,
            "symbol": symbol_for(asset),
            "symbol_title": symbol_title_for(asset),
            "color_class": color_class_for(asset),
            "kind": panel_kind(asset),
            "configured": adapters.get(asset) is not None,
            "here": asset == current,
        }
        for asset in panel_assets(config)
    ]


#: One sentence per panel kind, for the line under the heading. Rule 14: say what the
#: reader is looking at, next to it -- a page headed "LTC" showing four Core panes and a
#: page headed "XRP" showing four translated answers look similar and are not.
_KIND_NOTES = {
    PANEL_KIND_CORE: (
        "a Bitcoin Core-derived daemon with its own wallet, so these are Core's four READ "
        "panes -- Overview, Transactions, Peers, Information -- asked of the real daemon. The "
        "three are NOT the same build: what this one lacks is listed under Capabilities, with "
        "the evidence for each row"
    ),
    PANEL_KIND_TRANSLATED: (
        "no Core, no wallet daemon and no keystore -- an account here is a row on a ledger and "
        "its key lives wherever its holder put it. The same four questions are asked through "
        "this chain's command map, and a question with no answer on this chain comes back with "
        "the reason rather than with a blank"
    ),
    PANEL_KIND_NO_RPC: (
        "not JSON-RPC at all: this ledger is reached by running `dfx` with a Candid argument, "
        "so there is no method name to translate and no command map to write. What is below is "
        "the equivalent information for what this chain actually is"
    ),
}


def chain_panel(config: Mapping, adapters: Mapping, asset: object = "", *,
                ask: bool = False, budget: Budget | None = None) -> dict:
    """One chain's whole panel. Opens NO socket unless `ask` is True.

    THE ASSEMBLER, AND IT HOLDS NO DECISION OF ITS OWN (rule 10): membership comes from
    panel_assets(), the shape from panel_kind(), the configuration row from
    services/admin_view.chain_rows(), the capability differences from
    chains/daemon_capabilities, the absences from absent_capabilities(), and the live
    half from the three *_live() functions. What this does is choose which of the three
    to call and put the pieces in one dict, so the route is one line and the template
    branches on `kind` rather than on an asset string.

    `refusal` NON-EMPTY MEANS EVERY OTHER KEY IS ABSENT, and the caller answers 404
    with that sentence. It is returned rather than raised because a 404 body is a
    render, not an error path, and a template that has to catch an exception to say
    "BTX is not a chain" is a template holding a decision.

    `ask` IS THE WHOLE SOCKET BOUNDARY. False -- the default, and what every ordinary
    page load does -- reads configuration and pure tables and contacts nothing, so this
    page cannot be slow because a daemon is slow. True makes the bounded reads and
    carries `elapsed` so the operator can see what they cost, in microfortnights
    (rule 6).

    `budget` CARRIES BOTH THE CEILING AND THE CLOCK -- see Budget above for why they are
    one object and not two parameters.
    """
    known = panel_assets(config)
    asset = named_or_first(asset, known)
    refusal = refuse_unknown_asset(asset, known)
    if refusal:
        return {"asset": "", "refusal": refusal, "known": known}

    asset = str(asset).upper()
    kind = panel_kind(asset)
    adapter = adapters.get(asset)
    budget = Budget(PANEL_BUDGET_SECONDS) if budget is None else budget
    panel = {
        "asset": asset,
        "refusal": "",
        "known": known,
        "kind": kind,
        "kind_note": _KIND_NOTES[kind],
        "symbol": symbol_for(asset),
        "symbol_title": symbol_title_for(asset),
        "color_class": color_class_for(asset),
        "tabs": tab_rows(config, adapters, asset),
        "chain": _chain_row(config, adapters, asset),
        "panes": PANE_QUESTIONS,
        "absent": absent_capabilities(kind),
        # THE CAPABILITY TABLE, PER CHAIN, AND ONLY WHERE IT SPEAKS FOR THE CHAIN.
        # chains/daemon_capabilities.differences_for() RAISES for a chain that is not
        # Bitcoin-derived, which is correct -- "asking about them is a bug in the caller
        # rather than a gap here" -- so it is asked only of the three it covers rather
        # than guarded with a try. That is the same reason panel_kind() exists.
        "differences": differences_for(asset) if kind == PANEL_KIND_CORE else (),
        "wallet_warning": (
            wallet_path_warning(asset, str(getattr(adapter, "wallet", "") or ""))
            if kind == PANEL_KIND_CORE and adapter is not None else ""
        ),
        "probe_kind": probe_kind(adapter) if adapter is not None else "none",
        "no_probe_reason": (
            no_probe_reason(asset)
            if adapter is not None and probe_kind(adapter) == "none" else ""
        ),
        # ASKED MEANS ASKED, NOT REQUESTED, and the distinction shipped as a 500 the
        # first time this page was rendered against a container with no adapters:
        # `?ask` on an unconfigured chain set asked=True while `live` stayed None, and
        # the template walked into the Core-pane block with nothing to render.
        #
        # The crash was the lucky outcome. The defect under it is rule 14's "make 'did
        # nothing' look different from 'did work'": a page that said it had asked a
        # chain it never contacted would have been a page an operator reads as "this
        # daemon answered nothing", when what happened is that there was nothing to
        # ask. `cannot_ask` carries the reason and the two render differently.
        "asked": bool(ask) and adapter is not None,
        "budget_seconds": budget.seconds,
        "budget_variable": PANEL_BUDGET_VARIABLE,
        "per_call_seconds": per_call_seconds(adapter),
        "worst_case_seconds": worst_case_seconds(adapter, budget.seconds) if adapter else None,
        # THE SAME THREE FIGURES AS µfn WITH THE SECONDS IN PARENTHESES (rule 6), built
        # here rather than in the template because format_duration() is a Python
        # function and the 1.2096 constant must not acquire a second home in markup.
        #
        # THE SECONDS ARE KEPT ALONGSIDE, which rule 6 asks for specifically in this
        # case: these are governed by an environment variable whose name ends in
        # _SECONDS, and "the reader should never have to do the multiplication to
        # connect a log line to the variable that produced it."
        "budget_display": format_duration(budget.seconds),
        "per_call_display": (
            format_duration(per_call_seconds(adapter))
            if per_call_seconds(adapter) is not None else ""
        ),
        "worst_case_display": (
            format_duration(worst_case_seconds(adapter, budget.seconds))
            if adapter is not None and worst_case_seconds(adapter, budget.seconds) is not None
            else ""
        ),
        "live": None,
        "elapsed": "",
        "cannot_ask": "" if adapter is not None else (
            "there is no adapter for this chain, so there is nothing to ask. The variables that "
            "would build one are named on the row above"
        ),
    }
    if not ask or adapter is None:
        # THE QT SHAPE IS BUILT ON THIS PATH TOO, and that is the whole point of
        # qt_layout() taking `asked` as its own argument. A wallet with no status bar is
        # not a wallet; what an unasked render gets is a bar with one cell saying
        # nothing was asked, and a `cannot_ask` render gets a different cell saying
        # there is no adapter. Rule 14: those are two facts and the first version of
        # this page had one sentence for both.
        panel["qt"] = qt_layout(
            asset, kind,
            Asking(asked=False, cannot_ask=panel["cannot_ask"]),
            panes=PANE_QUESTIONS,
        )
        return panel
    started = budget.now()
    panel["live"] = _ask(config, adapter, asset, kind, budget.start())
    # IN MICROFORTNIGHTS, WITH THE SECONDS IN PARENTHESES (rule 6), through
    # microfortnights.format_duration() rather than by multiplying here -- the constant
    # already appears in one place and a second multiplication would be a second place
    # for it to be wrong.
    panel["elapsed"] = format_duration(budget.now() - started)
    # AFTER `live`, NECESSARILY. qt_layout() reads the balances, the transaction rows,
    # the peer rows and the sync verdict out of the payload _ask() just produced -- it
    # is an ARRANGEMENT of what was collected and never a second collection of it, so
    # it cannot run before there is something to arrange. It makes no call: `?ask` costs
    # exactly what it cost before this page grew a rail, which is what keeps the budget
    # arithmetic printed above true without being re-derived.
    panel["qt"] = qt_layout(
        asset, kind, Asking(asked=True, live=panel["live"]), panes=PANE_QUESTIONS,
    )
    return panel


def _ask(config: Mapping, adapter, asset: str, kind: str, deadline: Deadline) -> dict:
    """Which live half this chain gets. The one branch, in one place.

    A DICT DISPATCH WOULD NOT HELP HERE and this is three named branches on purpose:
    the three functions take genuinely different arguments -- core_live needs the asset
    for its network allowlist, translated_live needs the protocol and the account, and
    icp_live needs neither -- so a table would hold three different call shapes and the
    branch would move inside it.
    """
    if kind == PANEL_KIND_CORE:
        return core_live(adapter, asset, deadline)
    if kind == PANEL_KIND_TRANSLATED:
        variable, _discriminator, _network = TAG_ATTRIBUTION.get(asset, ("", "", ""))
        return translated_live(
            adapter, rpc_translation.CONSOLE_PROTOCOL[asset],
            str(config.get(variable) or "").strip(), deadline,
        )
    return icp_live(adapter, deadline)


def _chain_row(config: Mapping, adapters: Mapping, asset: str) -> dict:
    """This chain's row out of services/admin_view.chain_rows(). No socket.

    THE WHOLE CONFIGURATION HALF OF THIS PAGE IS THAT FUNCTION'S ROW, not a second
    assembly of the same facts (rule 8). It already answers configured-or-not, the
    endpoint without ever formatting a credential, why_unconfigured()'s named
    variables, why this chain cannot pay out, why it cannot take deposits, the
    confirmation threshold and the sentence explaining it, and whether any pair enables
    it. Rebuilding any of that here would be six copies that agree today.

    SIX ROWS ARE BUILT TO USE ONE, which is deliberate rather than overlooked. The
    function is pure and socket-free, and asking it for one asset would mean either a
    second entry point into it or a parameter it does not have -- both of which cost
    more than the five rows this discards. If it ever opens a socket this becomes
    wrong, which is why the claim is written here rather than assumed.

    An asset with no row answers {} rather than raising: chain_rows() iterates
    services/swap_view.ATTRIBUTION_MODELS, and a chain in Config.RPC but not in that
    table has no attribution model recorded -- a real gap, and one the panel reports as
    an absent row instead of a 500. ICP was in exactly that state until 2026-10-07 and
    it broke the first ICP swap ever created.
    """
    for row in chain_rows(config, adapters):
        if row["asset"] == asset:
            return row
    return {}
