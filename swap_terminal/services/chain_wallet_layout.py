#!/usr/bin/env python3
"""The Qt wallet's own furniture -- rail, status bar, balance card, transaction columns.

Role: submodule -> function (every per-chain layout decision behind /admin's chain
      tab; services/chain_panel.py assembles the panel and calls this for the Qt
      shape, and templates/admin_chain.html renders what comes back without
      deciding anything)
Reads: NOTHING of its own. Every function here is pure and takes the payload
      services/chain_panel.py already built -- the `live` dict one of its three
      *_live() functions returned, plus the asset and the panel kind. No socket, no
      file, no database, no environment variable, no clock.
Writes: nothing
Can move funds: no, and structurally rather than by assertion. This module makes no
      call of any kind: it has no adapter, no `run`, no transport and no import that
      could reach one. What it produces is strings and tuples for a template. The
      Send and Receive rail entries it emits carry `state = RAIL_INERT`, which is a
      LABEL -- there is no code path from any of them to a daemon, and
      templates/admin_chain.html renders them as prose rather than as a control.
Mainnet-safe: yes. Pure, so it is safe to import and to call with a mainnet panel's
      payload; it adds no round trip to a render that was going to make none.
Live-safe: yes. Starts nothing, stops nothing, holds no lock.

=============================================================================
WHY THIS FILE EXISTS. Operator, 2026-10-10:
=============================================================================

    "spin up an agent for the gui to make each sub tab for each chain daemon to
     look almost identical to it's qt core gui wallets."

The panel that existed before this was CORRECT and did not LOOK like a wallet. It
had Bitcoin Core's four read panes, under Core's own headings, asked of the real
daemon -- and it rendered them as six stacked `<section class="panel">` blocks of
key/value tables, one after another, in source order. Everything a Core user reads
at a glance was on the page and none of it was where they look for it:

    Core Qt                                 the panel before this change
    --------------------------------------  --------------------------------------
    an icon rail down the left, four         nothing. The only navigation was the
    entries, always visible                  browser's scrollbar
    Overview: a Balances card (Available,    a `kvt` table of three rows, with
    Pending, Immature, Total) with Recent    Recent transactions 400 lines further
    transactions BESIDE it                   down the document
    Transactions: Date, Type, Label,         txid, category, amount, fee, conf,
    Address, Amount                          address, label -- the same facts in a
                                             different order under different names,
                                             with no date column at all
    a status bar across the bottom: sync     five facts spread across two sections
    progress, block height, peer count,      and one subbox, none of them adjacent
    network, lock

So what this module adds is not information. It is the ARRANGEMENT, which is the
half the operator asked for -- and the arrangement is a decision per chain, which
is why it is a table in a Python module rather than markup in a template.

=============================================================================
THE THREE CONSTRAINTS THAT SHAPED EVERY CHOICE BELOW, AND NONE OF THEM BENT.
=============================================================================

1. QT'S SEND TAB IS A FORM AND THIS SURFACE HAS NO FORM AT ALL.

   routes/admin.py registers no write verb -- tests/test_web_surfaces.py asserts
   that over the app's real url_map -- and tests/test_chain_panel.py asserts the
   RENDERED page contains no `<form`, no `<input`, no `<textarea`, no `<button` and
   no `type="password"`, on all six chains, in both the asked and the unasked state.

   A DISABLED FORM WOULD HAVE BEEN THE OBVIOUS MOVE AND IT IS THE WRONG ONE, for two
   separate reasons that happen to agree. The mechanical one: a `<input disabled>` is
   still an `<input>`, so it fails the test, and weakening the test to fit a layout
   would be trading the structural guarantee for a picture of a control. The real
   one: a grayed-out Send button is a promise that the capability is one setting away,
   and on this surface it is not -- there is no POST route to enable, no signing path
   wired to a browser, and the operator's standing instruction is that a passphrase
   must never appear in a command this repository emits.

   So Send and Receive are RAIL_INERT: named, in Qt's own place in the rail, carrying
   the sentence that says where spending actually happens and why it is not here.
   Rule 14 at the scale of a feature -- a wallet with no Send tab and nothing saying
   why reads as half-built, and the operator's next move is to go looking for the
   button.

2. QT'S TABS HIDE EACH OTHER AND NOTHING ON THIS PAGE MAY BE HIDDEN.

   templates/admin.html carries the measurement that settled this, taken before any
   of it was built: marking every panel but one `display: none`, exactly as a tab
   mechanism does, made a browser copy return 923 of 12,502 characters -- 7.4% -- and
   0 of the 14 panel headings, with nothing in the copy saying the rest existed. This
   operator reads these pages by pasting them back.

   So the rail is an IN-PAGE JUMP LIST over sections that are all in the document and
   all in the flow, which is the same mechanism admin.html's own tab strip uses. What
   the operator gets is one click to any pane; what they do not get is anything hidden
   from a copy, from Ctrl-F, or from a screen reader. `anchor` on each Rail entry is
   the fragment id, and it is produced HERE rather than in the template so that a test
   can assert the rail links at something that exists (tests/test_chain_wallet_layout.py
   does exactly that, both directions).

3. THREE OF THE SIX CHAINS HAVE NO QT WALLET AND THIS MUST NOT PRETEND THEY DO.

   ICP, SOL and XRP have no Bitcoin Core-derived GUI, have never had one, and
   inventing one would be the confidently-wrong-answer shape this repository has
   already paid for more than once. They get the same FURNITURE -- a rail, a status
   bar, a balance card, a transactions region -- in each chain's own real vocabulary,
   and BORROWED_LAYOUT_NOTICE below is rendered on each of their pages saying in so
   many words that the resemblance is a layout borrowed deliberately and not a claim
   that such a wallet exists.

=============================================================================
AND A RAIL ENTRY WHOSE DATA NOTHING IN THIS TREE CAN FETCH SAYS SO.
=============================================================================

Gridcoin Research Qt's rail is Core's four plus its own, and its Overview carries
researcher figures Core has no concept of -- magnitude, CPID, beacon status, poll
participation. RAIL_UNREADABLE is for exactly those: the entry is rendered, in
Gridcoin's own place in the rail, saying what Qt shows there and that nothing in this
repository reads it.

ESTABLISHED BY GREPPING THE TREE ON 2026-10-10, not assumed. The denominator is every
`.py` and `.html` file in this repository:

    magnitude   0 matches that are about Gridcoin research magnitude. The 17 hits are
                `atomic_htlc_scripts.py`'s sign-and-magnitude integer encoding,
                `engineering_notation.py`'s decimal magnitudes, and prose in
                `quote_service.py` and `solana.py`. No RPC, no field, no reader.
    beacon      5 matches, and NONE of them reads a beacon. `modules/script_chain.py`
                and `modules/htlc_spend.py` mention Gridcoin contracts -- "a beacon, a
                poll, a vote" -- while explaining the `vContracts` vector they must
                skip when parsing a transaction. `services/custody_separation.py`
                recognizes the ACCOUNT LABEL `validateaddress` returns for a beacon
                address ("Beacon Address for CPID 09ff...") in order to tell the
                operator's own address from a swap's, which is an address-ownership
                question and not a researcher-status read. Named here so a reader who
                finds that file is not misled into thinking the figure is available.
    polls       0 matches. No `listpolls`, no `getpollresults`, nothing.

AND IT COULD NOT BE ADDED FROM HERE EVEN IF IT WERE WANTED, which is the stronger
half: every method a core panel sends goes through chains/daemon_wallet.READ_ONLY_RPCS
and tests/test_chain_panel.py collects what a real render asked a recording adapter
and checks each name against it. No research RPC is on that allowlist. So the honest
rendering is the only available one.

STAKING IS THE PARTIAL CASE AND IT GETS THE `missing` FIELD RATHER THAN A FOURTH
STATE. The lock half IS readable and is already on the page -- `getwalletinfo`'s
`unlocked_until`, through chains/wallet_lock.encryption_state(), with GRC in
wallet_lock.STAKING_CHAINS because a Gridcoin wallet unlocked for staking is a
distinct state from unlocked for spending. What is NOT readable is the figures Qt's
own staking line shows: weight, net weight, and expected time to stake. So the entry
is RAIL_LIVE, and `missing` names the three it cannot show. A fourth rail state would
have made every consumer branch on it; one more string on the entry does not.

=============================================================================
WHY THE FIGURES ARE A (METHOD, PATH) TABLE AND NOT THE COMMAND MAP'S `answers`.
=============================================================================

chains/xrp_rpc_map.py and chains/solana_rpc_map.py each carry an `answers` string per
entry, and the first version of this module tried to walk it. It cannot be walked, and
that is not a defect in those files -- it is what the field is FOR. Read what is
actually in them:

    "info.build_version / info.peers"     two paths and a slash
    "length of the array"                 an instruction, not a path
    "absoluteSlot / blockHeight"          two
    "the array"                           prose
    "value.blockhash"                     an actual path

`answers` is a sentence telling a human which field carries the figure the Bitcoin
method would have returned. A status bar needs a MACHINE-READABLE path. Those are two
different artifacts and conflating them would mean either rewriting the maps -- which
this change may not do; chains/ is off-limits to it -- or a parser that silently
resolves "length of the array" to nothing.

SO THE PATHS ARE SPELLED HERE, AND EVERY ONE OF THEM CITES THE READER IN chains/ THAT
ALREADY DEPENDS ON IT. That is the evidence, and rule 8 asks for the pointer at both
sites: these are the same paths the production adapters read, so a response shape that
changed would break a payout before it broke this panel, and the panel is not the
place that would find out first.

AND A PATH THAT DOES NOT RESOLVE RENDERS AS "NOT FOUND", NAMING THE PATH IT LOOKED
FOR. That is the property that makes spelling a path here safe at all (rule 17): a
wrong guess becomes a visible, debuggable absence on the operator's screen rather than
a plausible figure nobody can check. `dig()` returns a found flag and never a default.

=============================================================================
WHAT IS DELIBERATELY NOT HERE.
=============================================================================

NO NEW CHAIN CALL OF ANY KIND. This module adds zero round trips: `?ask` costs exactly
what it cost before, which is what lets the budget arithmetic in
services/chain_panel.py stay true without being re-derived. Every figure below is read
out of a response some existing pane already collected.

NO DATE COLUMN ON XRP, and the reason is the kind of thing a borrowed layout gets
wrong silently. An `account_tx` entry's `date` is seconds since 2000-01-01 -- the
Ripple epoch -- not since 1970. Rendering it through a Unix formatter would put every
XRP transaction thirty years in the past, on a page whose whole purpose is letting an
operator see whether a deposit arrived. Nothing in this tree converts the Ripple
epoch, so the column is absent and `_TX_COLUMNS` says why at the site.

NO DURATION IS INVENTED. Rule 6's boundary, stated once for everything below: a block
height, a ledger index, a slot, a peer count, a confirmation count and a commitment
rung are COUNTS and are never rendered as microfortnights. A wallet transaction's
`time` is a MOMENT -- a point, not an interval -- so unix_to_utc_text() renders it as
a UTC timestamp, which is also not microfortnights. The only durations this panel
reports are the request budget, the elapsed ask and a peer's ping, and all three are
already formatted by services/chain_panel.py through microfortnights.format_duration().
Nothing here formats a duration at all, which is why this module does not import that
one.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import NamedTuple

# SOLANA'S COMMITMENT LADDER, IMPORTED AND NOT RETYPED. chains/solana_units.py's own
# header: "the integer this module produces is a COMMITMENT RANK, and it is named that
# everywhere. The ladder is below, it has four rungs, and it is the ONE place the
# vocabulary is derived (rule 11: one vocabulary, derived in one place)." A four-rung
# ladder written out again here would agree with it today and would be the second
# definition of what `finalized` means -- on the figure that decides whether a Solana
# deposit is credited. It is used by _BAR_STATIC below, where the paragraph beside the
# table says what the two rungs are for.
#
# THE IMPORT IS SAFE AND THAT IS CHECKED, NOT ASSUMED: chains/solana_units.py imports
# `__future__`, `decimal` and one relative import of chains/coin_amounts.py, all pure,
# with no import-time side effect -- which rule 12 names as one of the two things the
# linter cannot check. It imports nothing from services/, so there is no cycle, and it
# opens no socket, so importing it does not make this module able to make a call.
from chains.daemon_network import NETWORK_IS_TEST, SYNC_SYNCED

# THE STATE VOCABULARIES, IMPORTED BECAUSE SPELLING ONE BY HAND WAS A MEASURED DEFECT.
#
# THE BUG, found 2026-10-10 by reading a rendered page rather than by running a test:
# this module compared the wallet lock state against the literal `"unencrypted"`, and
# chains/daemon_wallet.LOCK_NOT_ENCRYPTED is `"not_encrypted"`. So an UNENCRYPTED wallet
# -- nothing to unlock, nothing in the way of a payout -- rendered in the status bar as
# `[!]`, the warning glyph, beside a sentence saying there was no passphrase. A warning
# that fires on the healthy case is rule 13's own argument about a warning that fires on
# every run: the reader learns to ignore it and then misses the real one, which here is
# an ENCRYPTED wallet that a payout cannot leave.
#
# AND IT WAS INVISIBLE TO EVERY ASSERTION IN THE FILE, which is the half worth writing
# down. The cell rendered, carried a label, a value, a glyph and a sentence, and passed
# the "status bar is never empty" check -- a state comparison against a string nothing
# produces does not fail, it just never matches. Importing the names is what makes a
# renamed state a NameError instead of a cell that quietly stops being right (rule 11:
# one vocabulary, derived in one place, applied identically everywhere).
#
# THE SAME ARGUMENT COVERS THE OTHER TWO. `SYNC_SYNCED` and `NETWORK_IS_TEST` were
# spelled correctly by luck, and chains/daemon_network.py's own comment says those
# verdict tuples exist so "a renderer with one case per state can be tested for covering
# them all".
from chains.daemon_wallet import LOCK_ENCRYPTED, LOCK_NOT_ENCRYPTED
from chains.solana_units import (
    BALANCE_COMMITMENT,
    COMMITMENT_RANKS,
    DISCOVERY_COMMITMENT,
    FINALIZED_RANK,
)

# =============================================================================
# THE RAIL
# =============================================================================

#: What this page can actually do with a rail entry Qt has. NAMED, so a renderer with
#: one case per state can be tested for covering them all rather than discovering a gap
#: on an operator's screen -- the same guard chains/daemon_wallet.WALLET_STATES and
#: services/chain_panel.PANEL_KINDS carry, for the same reason.
#:
#: THE THREE ARE NOT DEGREES OF THE SAME THING, which is why they are three words and
#: not a boolean with a note. An operator reading the rail has to be able to tell:
#:
#:   live        there is a section below and it renders what this chain reported.
#:   inert       Qt has this and it WRITES. There is a section below, it is prose, and
#:               it names the tool at the repository root that has the capability. The
#:               distinction from `unreadable` is that nothing is missing -- the
#:               capability is deliberately elsewhere.
#:   unreadable  Qt has this and nothing in this repository reads the data. There is a
#:               section below and it says that, with what Qt shows there. The
#:               distinction from `inert` is that this one is a GAP rather than a
#:               boundary, and it would be closed by code rather than by policy.
RAIL_LIVE = "live"
RAIL_INERT = "inert"
RAIL_UNREADABLE = "unreadable"
RAIL_STATES = (RAIL_LIVE, RAIL_INERT, RAIL_UNREADABLE)


class Rail(NamedTuple):
    """One entry in the wallet's left-hand rail. Pure data; the template renders it.

    `anchor` is the fragment id of the section this entry jumps to, and it is produced
    here rather than written into markup so that the rail and the sections cannot drift
    apart without a test noticing (tests/test_chain_wallet_layout.py asserts in both
    directions over a real render).

    `group` is which part of the Qt interface this entry belongs to, and it exists
    because Core's Peers and Information are NOT rail entries -- they are tabs of the
    Node window, reached from Window -> Node window. Putting them in the rail would be
    the borrowed-layout error this module's header is about: a Core user looking for
    Peers does not look down the left edge. So they are rendered as a second, labeled
    group, which is what they actually are.

    `missing` is "" for an entry whose data is entirely available, and otherwise names
    what Qt shows in this pane that this repository cannot read. See the header for why
    this is a field rather than a fourth RAIL_STATE.

    `generated` SAYS WHO OWNS THE SECTION THE ANCHOR POINTS AT, and it exists because
    the alternative was a duplicated sentence. True means the template renders this
    entry's section from the entry itself -- which is right for Send, Receive, and
    Gridcoin's three unreadable research panes, whose entire content IS `what` and `why`.
    False means a FIXED section in the template's branch for this kind already carries
    that id and renders real data under it, and the entry is a link to it.

    THE CASE THAT FORCED THE FLAG: ICP's Transactions. That chain genuinely has no
    transaction list -- a deposit IS the balance appearing at an allocated subaccount --
    and services/chain_panel.icp_live() already returns the sentence saying so, under
    `no_transactions_pane`. Writing a second copy of that sentence into this table so a
    generated section could render it would be rule 8's two copies of one rule, on a
    claim about a chain. So that entry is `generated=False` and the ICP branch's own
    region carries the anchor, which means one sentence, in the module that owns it.

    THE INVARIANT IS TESTED RATHER THAN TRUSTED: tests/test_chain_wallet_layout.py walks
    every rail entry of every chain against a real render and asserts the anchor appears
    as an `id` in the markup -- both directions, so a fixed section that loses its id and
    a rail entry pointing at nothing both fail. A dead jump link is invisible to a reader
    until they click it and nothing happens.
    """

    anchor: str
    label: str
    what: str
    state: str
    why: str
    group: str = "rail"
    missing: str = ""
    generated: bool = False


#: Qt's four rail entries, for the three chains that have a Core-derived GUI.
#:
#: THE ORDER IS QT'S, NOT A READING ORDER, and it is the point of the whole table. A
#: Bitcoin Core user's hand goes to the second entry for Send and the fourth for
#: Transactions without reading either label, and reordering them to put the readable
#: ones first would break the one property the operator asked for.
_RAIL_CORE: tuple[Rail, ...] = (
    Rail("cw-overview", "Overview",
         "the Balances card -- available, pending, immature, total -- beside the most recent "
         "wallet transactions, which is exactly where Core puts them",
         RAIL_LIVE, ""),
    Rail("cw-send", "Send",
         "Qt's Send tab builds, signs and broadcasts a transaction",
         RAIL_INERT,
         "this surface registers no write method of any kind, so there is no form here and "
         "there is no disabled one either -- a grayed-out button would promise that spending is "
         "one setting away, and it is not. Sending is a named entry point at the repository "
         "root, run by the operator in their own shell",
         "rail", "", True),
    Rail("cw-receive", "Receive",
         "Qt's Receive tab asks the daemon for a fresh address",
         RAIL_INERT,
         "a Receive tab is `getnewaddress`, which DERIVES AND STORES A KEY in wallet.dat. That "
         "is a wallet write and not a read: it changes the file the operator backs up, and it "
         "is deliberately absent from the read-only allowlist this panel calls through",
         "rail", "", True),
    Rail("cw-transactions", "Transactions",
         "Qt's five columns -- date, type, label, address, amount -- newest first",
         RAIL_LIVE, ""),
)

#: Core's Node window, which is NOT the rail. See Rail.group.
_NODE_WINDOW: tuple[Rail, ...] = (
    Rail("cw-peers", "Peers",
         "one row per connected peer: address, build, direction, ping, their height",
         RAIL_LIVE, "", "node"),
    Rail("cw-information", "Information",
         "the daemon's own account of itself -- network, build, chain position, disk",
         RAIL_LIVE, "", "node"),
)

#: THE PER-CHAIN EXTRAS, keyed by asset. This is the table services/chain_panel.py's
#: caller was told to add rather than branching on an asset string in markup: a
#: `{% if data.asset == 'GRC' %}` in a template is a chain decision in a place that
#: cannot be called with seeded inputs and cannot be found by looking (rule 10).
#:
#: A CHAIN WITH NO ENTRY GETS NO EXTRAS, which is why this is a plain `.get(asset, ())`
#: and not a lookup that raises. Adding a seventh chain to Config.RPC must not 500 this
#: page, and the default -- Core's four and nothing else -- is the correct shape for
#: any further Bitcoin-derived daemon.
_RAIL_EXTRA: dict[str, tuple[Rail, ...]] = {
    # GRIDCOIN RESEARCH QT. Core's layout plus its own, and three of the four are
    # unreadable from here -- established by grepping the tree, with the denominator
    # and the hit counts in this module's header rather than in a sentence here.
    "GRC": (
        # ANCHORED AT cw-lock, WHICH EVERY CORE CHAIN'S PANEL ALREADY HAS. Gridcoin's
        # own word for the fact in that section is "staking" -- a wallet unlocked FOR
        # STAKING is a different state from one unlocked for spending -- so the rail
        # entry is labeled Gridcoin's way and points at the one section that reports
        # the lock. A second section would be the same reading under two headings.
        Rail("cw-lock", "Staking",
             "Gridcoin Qt's status bar carries a staking indicator, and its Overview a "
             "staking line",
             RAIL_LIVE, "",
             "rail",
             "Qt also shows staking WEIGHT, NET WEIGHT and EXPECTED TIME TO STAKE on this line "
             "and none of the three is read by anything in this repository. What this pane DOES "
             "carry is the half that is read: the wallet lock, which on Gridcoin is the fact that decides whether "
             "the wallet is staking at all -- a wallet unlocked FOR STAKING is a different state "
             "from one unlocked for spending, and chains/gridcoin_wallet_lock.py exists because "
             "a payout here has to put the wallet back the way it found it"),
        Rail("cw-voting", "Voting",
             "Gridcoin Qt's rail has a Voting entry: the open polls, and how this wallet voted",
             RAIL_UNREADABLE,
             "nothing in this repository reads a Gridcoin poll. There is no poll RPC on the "
             "read-only allowlist this panel calls through, so the figure could not be fetched "
             "from here even if a reader existed -- and inventing one would be a page that looks "
             "like a wallet and reports a vote nobody cast",
             "rail", "", True),
        Rail("cw-researcher", "Researcher",
             "Gridcoin Qt's Overview shows the CPID, the beacon's status and its expiry",
             RAIL_UNREADABLE,
             "nothing in this repository reads a beacon or a CPID. The one file that recognizes "
             "a beacon ADDRESS -- services/custody_separation.py, by the account label "
             "`validateaddress` returns for it -- is answering a different question: whose "
             "address is this, so that the operator's own staking address is not mistaken for a "
             "swap's. That is address ownership and not researcher status, and it is named here "
             "so a reader who finds it does not conclude the figure is available",
             "rail", "", True),
        Rail("cw-magnitude", "Magnitude",
             "Gridcoin Qt's Overview shows the magnitude this CPID is earning against",
             RAIL_UNREADABLE,
             "nothing in this repository reads a magnitude. Every match for the word in this "
             "tree is about integer sign-and-magnitude encoding or decimal order of magnitude; "
             "none is about research credit",
             "rail", "", True),
    ),
    # XRP. An account is a row on a ledger and there is no wallet at the server at all,
    # which chains/xrp_rpc_map.py's NO_EQUIVALENT entry for `listwallets` says at
    # length. So "Receive" is not an address derivation -- it is a DESTINATION TAG
    # against one shared account, which is this terminal's whole deposit model on this
    # chain (services/swap_service.TAG_ATTRIBUTION).
    "XRP": (
        Rail("cw-overview", "Account reserve",
             "the part of the balance that cannot be spent at all: the base reserve plus the "
             "owner reserve times OwnerCount",
             RAIL_LIVE, "",
             "rail",
             "there is no Qt pane this corresponds to, because no Core wallet has the concept. "
             "It is in the rail because on this chain it is the difference between the balance "
             "and what a payout may draw on, which is the question Core's Available answers"),
    ),
    "SOL": (
        Rail("cw-status", "Commitment",
             "which rung of the commitment ladder a figure was read at, and which rung this "
             "terminal requires before it credits a deposit",
             RAIL_LIVE, "",
             "rail",
             "there is no Qt pane this corresponds to. Bitcoin has one notion of settlement -- "
             "depth in blocks -- and Solana has four named rungs, so the question Core's "
             "confirmation count answers is asked differently here and gets its own entry"),
    ),
    "ICP": (
        Rail("cw-ledger", "Subaccounts",
             "how a deposit is addressed on this chain: one principal, one subaccount index per "
             "swap, and the account identifier derived from the pair",
             RAIL_LIVE, "",
             "rail",
             "the per-swap allocation is in the database and this page does not read the "
             "database -- deliberately, because every swap-level fact is already on /admin and "
             "this page is about the DAEMONS. What this pane DOES carry is the desk's own account "
             "and the derivation check; which index belongs to which swap is /admin's deposit table"),
        Rail("cw-blockindex", "Block index",
             "the ledger block index an ICRC-1 transfer returns, which is this chain's "
             "equivalent of a txid",
             RAIL_UNREADABLE,
             "nothing on this page reads a block index. It is RETURNED BY A TRANSFER -- a write "
             "-- and the ledger's query interface here is the three ICRC-1 reads below. The "
             "indexes this terminal has seen are in the database, on /admin's deposit table, "
             "beside the swaps they settled",
             "rail", "", True),
    ),
}

#: The rail for a chain with no Core wallet. SAME FURNITURE, NOT A COPY OF CORE'S
#: LABELS -- "Overview" and "Transactions" are ordinary English and mean the same thing
#: on any ledger, so they stay; "Send" and "Receive" stay because they are the two
#: capabilities an operator will look for and their absence has to be stated on every
#: chain, not only on the three with a Qt build.
#:
#: TWO VARIANTS AND NOT ONE, BECAUSE THE ANCHORS DIFFER AND A DEAD JUMP LINK IS THE
#: FAILURE THIS TABLE EXISTS TO AVOID. A translated chain's panel renders one section
#: per pane question, so its Transactions entry points at that pane's own id; ICP's
#: panel has no pane sections at all -- it has a ledger section and a region saying why
#: there is no transaction list -- so its entries point at those. One shared tuple would
#: have had to name anchors that exist on one kind and not the other, which renders as a
#: link that silently does nothing when clicked.
_RAIL_INERT_PAIR: tuple[Rail, ...] = (
    Rail("cw-send", "Send",
         "paying out on this chain",
         RAIL_INERT,
         "this surface registers no write method of any kind, so there is no form here and no "
         "disabled one. Paying out is a named entry point at the repository root, run by the "
         "operator in their own shell",
         "rail", "", True),
    Rail("cw-receive", "Receive",
         "how a deposit is addressed on this chain",
         RAIL_INERT,
         "there is no key derivation to offer here and nothing on this page would perform one. "
         "How a deposit to this chain is attributed is on the Configuration section above, which "
         "is the same row /admin's chain table renders",
         "rail", "", True),
)

_RAIL_TRANSLATED: tuple[Rail, ...] = (
    Rail("cw-overview", "Overview",
         "what this account holds, and the figures that decide how much of it can move",
         RAIL_LIVE, ""),
    *_RAIL_INERT_PAIR,
    Rail("cw-pane-transactions", "Transactions",
         "the account's recent entries, as this chain reports them",
         RAIL_LIVE, ""),
)

_RAIL_NO_RPC: tuple[Rail, ...] = (
    Rail("cw-ledger", "Overview",
         "what the desk's own account holds, and what a transfer out of it costs",
         RAIL_LIVE, ""),
    *_RAIL_INERT_PAIR,
    # NOT GENERATED: services/chain_panel.icp_live() already owns the sentence saying
    # why this chain has no transaction list, and the ICP branch renders it under this
    # id. A generated section would mean a second copy of that claim (see Rail.generated).
    Rail("cw-transactions", "Transactions",
         "whether there is a transaction list on this chain at all",
         RAIL_UNREADABLE,
         "there is none, and that is a fact about the Internet Computer rather than a gap here. "
         "The section this links to carries the reason, in the words of the function that owns "
         "it"),
)


#: WHAT EACH RAIL GROUP IS CALLED ON THE PAGE. Rail.group's two values, spelled once.
#:
#: THE SECOND LABEL NAMES QT'S MENU PATH and that is the point of having a second group
#: at all: a Core user looking for Peers does not look down the left edge, they open
#: Window -> Node window. A rail that listed Peers alongside Overview would be a
#: borrowed layout that misdescribes the thing it borrowed from, which is the failure
#: mode this whole module is written against.
RAIL_GROUP_LABELS = {
    "rail": "Wallet",
    "node": "Node window (Qt: Window → Node window)",
}


def rail_entries(asset: str, kind: str) -> tuple[Rail, ...]:
    """The whole rail for one chain, in Qt's order, with this chain's extras folded in.

    THE BASE COMES FROM THE KIND AND THE EXTRAS FROM THE ASSET, which is the split the
    layout constraint asks for: a template branches on `kind` and never on an asset
    string, so anything genuinely per-chain has to arrive as data from here.

    THE EXTRAS GO BEFORE THE NODE WINDOW AND AFTER THE RAIL, because they ARE rail
    entries on the builds that have them -- Gridcoin's Voting sits in the rail beside
    Transactions, not in a debug window. Appending them after Peers and Information
    would put a rail entry below a Node-window tab and misdescribe both.

    AN UNKNOWN KIND GETS THE NO-RPC RAIL rather than raising, and that is the
    fail-closed direction: that rail claims no Core wallet, no keystore and no command
    map, which is the safe thing to claim about a chain this module has never heard of.
    services/chain_panel.panel_kind() already answers PANEL_KIND_NO_RPC for a chain in
    neither of its tables, so this is a second belt on the same trousers rather than a
    live branch -- said here because a reader will wonder which.
    """
    if kind == "core_wallet":
        return _RAIL_CORE + _RAIL_EXTRA.get(str(asset).upper(), ()) + _NODE_WINDOW
    base = _RAIL_TRANSLATED if kind == "translated" else _RAIL_NO_RPC
    return base + _RAIL_EXTRA.get(str(asset).upper(), ())


def pane_anchor(title: str) -> str:
    """One pane question's fragment id, derived from its title. ONE place, both readers.

    THE RAIL AND THE SECTION BOTH CALL THIS, which is the whole reason it is a function
    rather than a slug spelled twice. services/chain_panel.PANE_QUESTIONS is where the
    titles live -- Overview, Spendable, Transactions, Peers, Connections, Information --
    and a translated chain's panel renders one section per question. The rail has to
    link at those sections, so the id has to be derived identically in both places or
    the link is dead.

    DERIVED FROM THE TITLE AND NOT STORED ON THE PANE, deliberately: adding a field to
    that NamedTuple would change a table three other modules read, to carry a value
    that is a pure function of a field already on it. A renamed title moves the rail
    link and the section id together, which is the property that matters.

    NON-ALPHANUMERICS BECOME HYPHENS AND RUNS COLLAPSE, so a title with a slash or a
    space cannot produce an id a browser will not match. Prefixed `cw-pane-` so it
    cannot collide with a fixed section's id in the same document.
    """
    slug = "".join(char if char.isalnum() else "-" for char in str(title).lower())
    while "--" in slug:
        slug = slug.replace("--", "-")
    return "cw-pane-" + slug.strip("-")


#: ONE SENTENCE PER PANEL KIND, SAYING WHAT THE READER IS LOOKING AT. Rule 14 applied
#: to a resemblance: a page arranged like Bitcoin Core, headed XRP, is a page that will
#: eventually be read as a claim that XRP has a Core wallet. It does not, has never had
#: one, and three of the six chains here are in that position.
#:
#: THE CORE ENTRY IS NOT AN APOLOGY AND SAYS SOMETHING DIFFERENT: on BTC, LTC and GRC
#: the resemblance is the POINT, these are the real daemon's answers under the real
#: GUI's headings, and what the reader needs to know is which two tabs are missing and
#: that the rest is genuine.
BORROWED_LAYOUT_NOTICE: dict[str, str] = {
    "core_wallet": (
        "This is arranged as that chain's own Core Qt wallet arranges it -- the rail down the "
        "left, the Balances card beside Recent transactions, Qt's five transaction columns, and "
        "the status bar across the bottom -- and every figure in it came from the real daemon. "
        "Two of Qt's rail entries WRITE and are therefore inert here, each saying so in its own "
        "place rather than being left out."
    ),
    "translated": (
        "THE LAYOUT IS BORROWED ON PURPOSE AND THIS CHAIN HAS NO CORE WALLET. There is no "
        "Bitcoin Core-derived GUI for it, there never has been, and nothing on this page is a "
        "claim that one exists. What is borrowed is the ARRANGEMENT -- a rail, a balance card, a "
        "transactions region, a status bar -- so that an operator moving between six chain tabs "
        "finds the same fact in the same place. Every label below is this chain's own word for "
        "its own thing, and where this chain has no equivalent for a question the command map's "
        "own reason is printed instead of a figure."
    ),
    "no_rpc": (
        "THE LAYOUT IS BORROWED ON PURPOSE AND THIS CHAIN HAS NO CORE WALLET -- nor any JSON-RPC "
        "interface to translate one into. This ledger is reached by running `dfx` with a Candid "
        "argument. What is borrowed is the ARRANGEMENT, so that an operator moving between six "
        "chain tabs finds the same fact in the same place; the vocabulary is the Internet "
        "Computer's own, and the panes this chain genuinely does not have say so rather than "
        "rendering empty."
    ),
}


# =============================================================================
# READING A FIGURE OUT OF A RESPONSE
# =============================================================================


def dig(value: object, path: str) -> tuple[bool, object]:
    """Walk a dotted path into a decoded JSON response. (found, value). NEVER a default.

    THE TWO-TUPLE IS THE WHOLE POINT, and it is chains/daemon_wallet._read()'s shape
    for the same reason: a caller must be able to tell "the response does not carry
    that field" from "the field is there and its value is falsey". `info.peers` of 0 is
    a node connected to nobody, which is the single most useful finding this panel
    produces; a bare `None` return would make it indistinguishable from a response that
    had no `peers` key at all, and the renderer would print the same thing for both.

    AN EMPTY PATH MEANS THE RESULT ITSELF, which is how a figure reads from a response
    that is a bare array or a bare integer -- `getClusterNodes` answers a list and
    `getBlockHeight` answers a number, neither of which has a field to name.

    NO EXCEPTION ESCAPES AND NONE IS CAUGHT EITHER, because there is nothing here that
    raises: every step is a Mapping membership test. A path through a list index is
    deliberately NOT supported -- no figure in _FIGURES needs one, and supporting it
    would mean parsing `[0]` out of the path string, which is a parser this module has
    no reason to own.
    """
    if not path:
        return True, value
    current = value
    for step in path.split("."):
        if not isinstance(current, Mapping) or step not in current:
            return False, None
        current = current[step]
    return True, current


#: How a figure is to be read and rendered. NOT a unit conversion -- see Figure.kind.
FIGURE_TEXT = "text"
FIGURE_COUNT = "count"
FIGURE_LENGTH = "length"
FIGURE_KINDS = (FIGURE_TEXT, FIGURE_COUNT, FIGURE_LENGTH)


class Figure(NamedTuple):
    """One status-bar or balance-card figure, and where in a response it lives.

    `method` is the CHAIN'S OWN method name -- `server_info`, `getEpochInfo` -- because
    that is the key services/chain_panel.translated_live() folds its answers under, and
    the fold is what makes two panes share one call. Keying on the pane question
    instead would have meant looking up which pane won the fold.

    `kind` says how to RENDER it and never converts a unit. FIGURE_LENGTH is the one
    that does arithmetic, and it is `len()` of an array -- the only shape Solana's
    `getClusterNodes` offers for a peer count, which solana_rpc_map.py spells as
    "length of the array" in prose for exactly this reason. FIGURE_COUNT and
    FIGURE_TEXT both render the value as it arrived.

    NO FIGURE HERE IS A DURATION AND NONE MAY BECOME ONE. A ledger index, a slot, a
    block height, an OwnerCount and a peer count are counts (rule 6: "blocks and
    confirmations are NOT times and are never converted"). The unit-bearing ones --
    XRP's drops, SOL's lamports -- are rendered as they arrived with the unit named in
    `note`, because dividing them here would put a second copy of a per-chain divisor
    in a module that has no business owning one (chains/xrp_units.py and
    chains/solana_units.py own them, and this module makes no call that would let it
    ask either).
    """

    label: str
    method: str
    path: str
    note: str
    kind: str = FIGURE_TEXT


#: THE PER-CHAIN FIGURES, keyed by asset. Every path below is one a module in chains/
#: already reads, and the citation is the evidence -- these are not guesses about a
#: response shape, they are the shapes this repository's payout and deposit paths
#: depend on. A path that stopped resolving would break a payout before it broke this
#: panel; `dig()` makes it render as a named absence either way.
_FIGURES: dict[str, tuple[Figure, ...]] = {
    "XRP": (
        # chains/xrp.py:755 -- `(result.get("info") or {}).get("validated_ledger")`,
        # read by reserve_xrp() to decide what a payout may draw on.
        Figure("Ledger index", "server_info", "info.validated_ledger.seq",
               "the last VALIDATED ledger. chains/xrp_rpc_map.py is explicit that this is the "
               "one a client should trust, and it is a COUNT -- never a duration",
               FIGURE_COUNT),
        Figure("Peers", "server_info", "info.peers",
               "how many peers this server has. A server with none relays into nothing, which "
               "is the same finding Core's Peers pane exists for",
               FIGURE_COUNT),
        Figure("Server state", "server_info", "info.server_state",
               "rippled's own word for whether it is in sync: `full` or `proposing` is "
               "participating, `connected` or `syncing` is not yet"),
        Figure("Build", "server_info", "info.build_version",
               "the rippled build. Core's Information pane shows the same fact"),
        # chains/xrp_signing.py:441 and chains/xrp.py:880 -- reserve_base_xrp and
        # reserve_inc_xrp, read together with OwnerCount to compute what is spendable.
        Figure("Base reserve", "server_info", "info.validated_ledger.reserve_base_xrp",
               "XRP, not drops, and NOT spendable: every funded account must hold at least this "
               "much. Asked of the server rather than hardcoded, because it has been 20, then "
               "10, then 1"),
        Figure("Owner reserve", "server_info", "info.validated_ledger.reserve_inc_xrp",
               "XRP per owned object. The account's own reserve is this times OwnerCount"),
        # chains/xrp.py:736-744 -- account_data.Balance as a STRING, and OwnerCount.
        Figure("Balance", "account_info", "account_data.Balance",
               "DROPS, as a JSON string -- the ledger sends drop counts as strings specifically "
               "so a client cannot round one through a double. 1 XRP is 1,000,000 drops"),
        Figure("OwnerCount", "account_info", "account_data.OwnerCount",
               "how many ledger objects this account owns. It multiplies the owner reserve, so "
               "it is the figure that decides how much of the balance is locked",
               FIGURE_COUNT),
        Figure("Sequence", "account_info", "account_data.Sequence",
               "the account's next transaction sequence number. A COUNT",
               FIGURE_COUNT),
    ),
    "SOL": (
        # chains/solana.py's getEpochInfo fields; the slot/blockHeight distinction is
        # chains/solana_rpc_map.py's own at length -- a SLOT can be skipped, so the
        # slot number runs ahead of the block height and the two are not the same fact.
        Figure("Block height", "getEpochInfo", "blockHeight",
               "blocks actually produced. NOT the slot: a slot is a scheduled leader window and "
               "a slot can be SKIPPED, so the slot number runs ahead of this",
               FIGURE_COUNT),
        Figure("Slot", "getEpochInfo", "absoluteSlot",
               "the current slot, which is the number a cluster explorer shows. A COUNT",
               FIGURE_COUNT),
        Figure("Epoch", "getEpochInfo", "epoch",
               "which epoch the cluster is in. A COUNT, not a time",
               FIGURE_COUNT),
        # chains/solana.py:1039-1044 -- getBalance answers {"context", "value"} and a
        # bare integer from a stub or an older proxy. The documented shape is the path;
        # the bare-integer shape renders as a named absence rather than as a guess.
        Figure("Balance", "getBalance", "value",
               "LAMPORTS, not SOL -- divide by 1,000,000,000. And not all of it is spendable: "
               "an account must keep its rent-exempt minimum"),
        # chains/solana_rpc_map.py: getClusterNodes answers an array and the count is
        # its length rather than a field, which is why this one is FIGURE_LENGTH.
        Figure("Gossip peers", "getClusterNodes", "",
               "how many nodes GOSSIP knows about -- not how many this node has a connection "
               "to, which is the difference solana_rpc_map.py records. A COUNT",
               FIGURE_LENGTH),
    ),
}


class FigureRow(NamedTuple):
    """One resolved figure, ready to render. `found` is False for every absence.

    `where` NAMES THE METHOD AND THE PATH ON EVERY ROW, present or absent, which is
    rule 14's "echo the parameters that decide the answer". On a row that resolved it
    lets a reader check the figure against the raw response printed further down the
    page; on a row that did not, it is the whole debugging message -- the operator can
    see which field was looked for and that it was not there.
    """

    label: str
    value: str
    note: str
    where: str
    found: bool
    why: str


def figure_rows(asset: str, answers_by_method: Mapping) -> tuple[FigureRow, ...]:
    """This chain's figures, resolved against the calls that were actually made.

    `answers_by_method` IS WHAT translated_live() ALREADY BUILT -- the fold, keyed by
    chain method -- so this makes no call and cannot make one. A method that was never
    asked, or that the chain refused, produces a row saying which; it does not produce
    a blank and it does not produce a zero (rule 14, and on this page the difference
    between "this account holds nothing" and "nobody asked" is the difference between
    going to bed and not).

    THREE DISTINCT ABSENCES AND EACH GETS ITS OWN SENTENCE. They are different facts
    and a reader has to be able to act on which one they have:

      the method was not asked       no answer under that key at all
      the chain answered an error    `ok` is False, and the chain's own words are in it
      the field was not in the reply the call succeeded and `dig` found no such path

    The third is the one worth having: it is how a response shape that changed under
    this panel shows up as a named absence rather than as a plausible wrong figure.
    """
    rows: list[FigureRow] = []
    for figure in _FIGURES.get(str(asset).upper(), ()):
        where = f"{figure.method}" + (f" -> {figure.path}" if figure.path else " -> the result itself")
        answer = answers_by_method.get(figure.method)
        if not isinstance(answer, Mapping):
            rows.append(FigureRow(figure.label, "", figure.note, where, False, (
                f"NOT ASKED -- no {figure.method} call was made on this render, so this figure "
                f"was not read. This says nothing about the chain"
            )))
            continue
        if not answer.get("ok"):
            rows.append(FigureRow(figure.label, "", figure.note, where, False, (
                f"the {figure.method} call did not answer, so this figure could not be read: "
                f"{answer.get('error') or '(no reason was reported, which is itself a defect)'}"
            )))
            continue
        found, value = dig(answer.get("result"), figure.path)
        if not found:
            rows.append(FigureRow(figure.label, "", figure.note, where, False, (
                f"the {figure.method} call ANSWERED and its reply does not carry {figure.path!r}. "
                f"That is a response shape this panel did not expect -- the raw reply is printed "
                f"below this chain's panes, so the field it does carry can be read off it"
            )))
            continue
        rows.append(FigureRow(figure.label, _render_figure(figure, value), figure.note,
                              where, True, ""))
    return tuple(rows)


def _render_figure(figure: Figure, value: object) -> str:
    """One figure's value as the string the page shows. No unit conversion, ever.

    FIGURE_LENGTH IS THE ONLY ARITHMETIC and it is `len()`. A value that is not a
    sequence under that kind renders its own type rather than a number, because
    "getClusterNodes answered something that is not a list" is a finding and
    `len()`-ing it would raise inside a render.
    """
    if figure.kind == FIGURE_LENGTH:
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
            return str(len(value))
        return f"(not a list -- the reply carries {type(value).__name__})"
    return str(value)


# =============================================================================
# THE BALANCE CARD
# =============================================================================


class Money(NamedTuple):
    """One line of the Balances card. `reported` False means the daemon did not say.

    THE DISTINCTION IS THE CARD'S WHOLE REASON FOR EXISTING in this shape. Core's
    Overview shows four numbers and a reader takes all four as measurements; a build
    that does not carry `immature_balance` has not told us it is zero, and printing 0
    there would be a measurement nobody took. chains/daemon_wallet._balances_and_info()
    already makes that distinction per field and this carries it through to the card.
    """

    label: str
    value: str
    note: str
    reported: bool


#: Qt's Balances card, in Qt's order. Available, Pending, Immature, Total.
#:
#: THE LABELS ARE QT'S OWN WORDS and chains/daemon_wallet._BALANCE_FIELDS' keys are
#: the lowercase versions of the first three, which is not a coincidence -- that table
#: was written from the same GUI. The mapping is spelled out rather than derived by
#: `.title()` so that a renamed field in that module shows up here as a named absence
#: instead of a silently dropped row.
_BALANCE_CARD = (("available", "Available"), ("pending", "Pending"), ("immature", "Immature"))


def balance_card(balances: Mapping | None, decimals: int = 8) -> tuple[Money, ...]:
    """Core's four Overview numbers, Total included. () when no balance was established.

    TOTAL IS REPORTED ONLY WHEN ALL THREE PARTS WERE, and that is the decision this
    function exists to hold. Qt's Total is the sum of the three above it, and summing a
    partial set would produce a confident number that is wrong by whatever the daemon
    did not report -- on the `getinfo` fallback route, which answers `balance` alone,
    Total would read as the available balance and an operator would believe the wallet
    held nothing immature. So an incomplete set renders Total as NOT ESTABLISHED,
    naming which parts were missing.

    AN EMPTY TUPLE RATHER THAN A CARD OF ZEROS when `balances` is None. A node with no
    wallet loaded has no balance to have, which is a different fact from a balance of
    zero -- tests/test_chain_panel.py pins it by asserting the string "0.00000000"
    does not appear on that render, and a card of zeros is exactly what it is
    guarding against.
    """
    if not isinstance(balances, Mapping) or not balances:
        return ()
    lines: list[Money] = []
    absent: list[str] = []
    total = 0.0
    for key, label in _BALANCE_CARD:
        figure = balances.get(key)
        if not isinstance(figure, Mapping) or not figure.get("reported"):
            absent.append(label)
            lines.append(Money(label, "", (
                figure.get("note") if isinstance(figure, Mapping) else
                "this balance set carries no entry under this name at all, which is a gap in "
                "what was collected rather than a fact about the wallet"
            ) or "", False))
            continue
        value = float(figure.get("value") or 0.0)
        total += value
        lines.append(Money(label, f"{value:.{decimals}f}", str(figure.get("note") or ""), True))
    if absent:
        lines.append(Money("Total", "", (
            f"NOT ESTABLISHED. Qt's Total is the sum of the three above it and this daemon did "
            f"not report {', '.join(absent)}, so a sum here would be wrong by however much that "
            f"is -- which is not zero and is not known"
        ), False))
    else:
        lines.append(Money("Total", f"{total:.{decimals}f}",
                           "the three above, summed. Every one of them was reported", True))
    return tuple(lines)


# =============================================================================
# THE TRANSACTIONS LIST
# =============================================================================


def unix_to_utc_text(value: object) -> str:
    """A unix timestamp as `YYYY-MM-DD HH:MM:SSZ`, or "" when there is none.

    A MOMENT, NOT A DURATION, which is why this is not microfortnights (rule 6). The
    unit boundary that rule draws is between an INTERVAL -- how long something took,
    how long is left -- and a POINT on the calendar. "Microfortnights since 1970" is
    not a thing anybody reads, and services/chain_panel.core_live() draws the same
    line in prose for a peer's `conntime`.

    UTC AND SAID SO WITH THE Z, because this page is pasted into a conversation and a
    local-time timestamp with no zone on it is a figure the reader cannot compare
    against anything -- the deposit row on /admin, a daemon log line, or the other
    chain's tab.

    "" FOR ANYTHING UNREADABLE rather than a guess or an exception. A `time` field that
    is absent, a string, or out of the range the platform can represent all mean the
    same thing to the reader -- this row carries no date -- and the caller renders that
    as a named absence. The broad catch below is the legitimate kind: the failure IS
    the return value and the caller cannot mistake "" for a date.
    """
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return ""
    try:
        return datetime.fromtimestamp(float(value), tz=UTC).strftime("%Y-%m-%d %H:%M:%SZ")
    except (OverflowError, OSError, ValueError):
        # NOT a blind except: these are exactly the three the stdlib raises for a
        # timestamp outside the platform's range, and anything else here would be a
        # defect in this function that should surface rather than be swallowed.
        return ""


class TxColumn(NamedTuple):
    """One column of the transactions table, and where in a row its value comes from.

    `key` is a key of the row dict the chain's own reader produced, and `kind` says how
    to render it. The columns are Qt's and the keys are this repository's, so this table
    is the join between them -- which is the thing a template must not hold, because a
    renamed key would then be a silently empty column instead of a named absence.
    """

    label: str
    key: str
    kind: str
    note: str = ""


#: QT'S TRANSACTION COLUMNS, in Qt's order, for the three Core-derived chains.
#:
#: Core Qt shows: a status icon, Date, Type, Label, Address, Amount. The status icon is
#: a confirmation count with a tooltip, so it is rendered as the count -- a NUMBER
#: (rule 6: confirmations are a count and never a duration) -- under its own heading,
#: because an icon that only exists as a tooltip is a fact hidden from a paste.
#:
#: `txid` IS KEPT AND QT DOES NOT SHOW IT IN THE LIST. Qt puts it in the
#: double-click detail dialog, and there is no dialog here: a transaction id is the one
#: field an operator takes to a block explorer, so dropping it to match the GUI exactly
#: would make the table prettier and less useful. Named as a deviation rather than left
#: as one.
_TX_COLUMNS_CORE: tuple[TxColumn, ...] = (
    TxColumn("Date", "time", "unix",
             "the daemon's own `time` for the entry, in UTC"),
    TxColumn("Type", "category", "text",
             "the daemon's own word -- send, receive, generate, immature, orphan -- and NOT "
             "collapsed to a direction: `generate` and `immature` are the two that matter on a "
             "desk that mined its own coins, and both would read as `receive`"),
    TxColumn("Label", "label", "text", "the wallet's label for the address, if it has one"),
    TxColumn("Address", "address", "mono", ""),
    TxColumn("Amount", "amount", "amount", "positive in, negative out, as the daemon signs it"),
    TxColumn("Confirmations", "confirmations", "count",
             "Qt shows this as a status icon with a tooltip. A COUNT, never a duration"),
    TxColumn("Fee", "fee", "amount", "Qt shows the fee in the detail dialog rather than the list"),
    TxColumn("Transaction id", "txid", "mono",
             "Qt shows this in the double-click detail dialog, not in the list. There is no "
             "dialog here and it is the one field that goes to a block explorer, so it is a "
             "column"),
)


def tx_columns(kind: str) -> tuple[TxColumn, ...]:
    """Which transaction columns this chain's rows have. () for a chain with no rows.

    ONLY THE CORE CHAINS HAVE A DECOMPOSED ROW, and that is a measured boundary rather
    than an omission. chains/daemon_wallet.recent_transactions() builds a row dict per
    entry with the fields Qt's columns show, so BTC, LTC and GRC have something to put
    in a table. XRP's and SOL's transaction answers come back as the chain's own
    structure -- `account_tx`'s entries nest the transaction under `tx`, and
    `getSignaturesForAddress` answers signatures rather than transactions -- and
    decomposing either HERE would be a second implementation of that chain's
    transaction shape, in a layout module, competing with the one in
    chains/xrp_payments.py that the deposit path depends on. Rule 8's exact shape, on
    the one field that has drained real exchanges (`meta.delivered_amount` versus
    `Amount`).

    So those two render their raw answer with a sentence saying why it is raw, which is
    a page that tells the truth about what it has rather than a table that invents a
    schema. The alternative is named work, not a hidden gap.
    """
    return _TX_COLUMNS_CORE if kind == "core_wallet" else ()


def tx_cells(row: Mapping, columns: tuple[TxColumn, ...], decimals: int = 8) -> tuple[dict, ...]:
    """One transaction row, as the cells the template prints. Each cell says if it is empty.

    `shown` IS FALSE FOR AN ABSENT VALUE AND THE TEMPLATE RENDERS A DASH, which is the
    one place on this page where a dash rather than a sentence is correct: a table of
    twenty-five rows cannot carry a paragraph per empty cell, the COLUMN's note already
    says what the field is, and the region's own empty state covers the case where
    there are no rows at all. Rule 14's requirement is that a reader can tell zero from
    broken, and an explicit dash in a column whose heading is live does that.

    A ZERO IS NOT ABSENT. `confirmations` of 0 is an unconfirmed transaction -- the most
    interesting row on the page -- and an `amount` of 0.0 is a real figure. So the test
    is `is None`, never truthiness, which is the defect this comment exists to stop
    somebody reintroducing.
    """
    cells = []
    for column in columns:
        value = row.get(column.key)
        if value is None or value == "":
            cells.append({"label": column.label, "text": "", "kind": column.kind, "shown": False})
            continue
        if column.kind == "unix":
            text = unix_to_utc_text(value)
            cells.append({"label": column.label, "text": text, "kind": column.kind,
                          "shown": bool(text)})
            continue
        if column.kind == "amount" and isinstance(value, (int, float)):
            cells.append({"label": column.label, "text": f"{float(value):.{decimals}f}",
                          "kind": column.kind, "shown": True})
            continue
        cells.append({"label": column.label, "text": str(value), "kind": column.kind,
                      "shown": True})
    return tuple(cells)


# =============================================================================
# THE STATUS BAR
# =============================================================================

#: What a status-bar cell is saying. Three, and they are not degrees of one thing.
#:
#:   ok        a figure was read and it is not a cause for concern.
#:   warn      a figure was read and it is one -- a node with no peers, a wallet that
#:             is locked, a network that may not be touched, a chain mid-sync.
#:   unknown   nothing was read. NOT the same as `warn`, and conflating them is the
#:             defect rule 14 is about: an operator who cannot tell "no peers" from
#:             "nobody asked" has no way to know whether to act.
CELL_OK = "ok"
CELL_WARN = "warn"
CELL_UNKNOWN = "unknown"
CELL_STATES = (CELL_OK, CELL_WARN, CELL_UNKNOWN)

#: A GLYPH PER STATE, BECAUSE COLOR IS THE THIRD CHANNEL AND NEVER THE FIRST. This
#: stylesheet's standing requirement, and on this page it is not a nicety: the operator
#: reads these pages by PASTING THEM BACK, where no CSS travels at all. Each glyph is
#: ASCII so it survives any encoding a paste goes through -- a Unicode shape that
#: arrives as a replacement character is worse than no shape, because it is a signal
#: the reader can see and cannot read.
CELL_GLYPHS = {CELL_OK: "[ok]", CELL_WARN: "[!]", CELL_UNKNOWN: "[?]"}


class Cell(NamedTuple):
    """One cell of the status bar across the bottom of the wallet.

    `value` IS ALREADY A STRING and `note` always says what it means, because this bar
    is five short figures with no room for a sentence beside each -- so the sentence is
    the title and the note, and neither is optional. Rule 14's "state what the number
    means, next to the number".
    """

    label: str
    value: str
    note: str
    state: str
    glyph: str


def _cell(label: str, value: str, note: str, state: str) -> Cell:
    """One cell, with its glyph looked up rather than written at the call site."""
    return Cell(label, value, note, state, CELL_GLYPHS.get(state, CELL_GLYPHS[CELL_UNKNOWN]))


def not_asked_bar(asset: str, cannot_ask: str) -> tuple[Cell, ...]:
    """The status bar on a render that contacted nothing. Never empty, never a zero.

    TWO REASONS NOTHING WAS ASKED AND THEY ARE NOT THE SAME FACT. "You have not
    clicked" is a page waiting for the operator; "there is no adapter for this chain" is
    a configuration fault they have to go and fix. The panel already carries that
    distinction in `cannot_ask` and this keeps it in the bar, because the bar is the
    part of a wallet a user glances at and it must not read as an outage either way.

    ONE CELL AND NOT FIVE EMPTY ONES. A bar of five `[?]` cells looks like five failed
    reads; one cell saying nothing was asked is the actual state.
    """
    if cannot_ask:
        return (_cell("Nothing to ask", "(no adapter)", (
            f"{cannot_ask}. No figure here is zero, missing or stale -- there is no endpoint for "
            f"this chain in this process at all, so nothing was contacted and nothing could have "
            f"been. The chain is {asset}"
        ), CELL_UNKNOWN),)
    return (_cell("Not asked", "(no figure)", (
        f"this page has contacted nothing. Every figure above came from the configuration and "
        f"from tables in this repository, so no balance, height, peer count or lock state is "
        f"shown -- that is a page waiting to be told to ask {asset}, not a daemon that answered "
        f"nothing and not a balance of zero"
    ), CELL_UNKNOWN),)


def core_status_bar(live: Mapping) -> tuple[Cell, ...]:
    """Core Qt's status bar: sync, block height, peers, network, lock. Five cells, always.

    EVERY ONE OF THE FIVE IS ALREADY IN THE PANEL'S PAYLOAD and this is the arrangement
    rather than a second collection of them -- `sync` and `network` from
    chains/daemon_network.py through services/chain_panel.core_live(), the peer rows and
    the encryption note from chains/daemon_wallet.wallet_pane(). Re-deriving any of them
    here would be rule 8's two copies of one rule on the question that decides whether a
    command reaches a mainnet wallet.

    WHY SYNC AND NETWORK ARE THE TWO THAT CAN WARN LOUDEST. A node mid-sync reports a
    balance of 0 and cannot see a deposit at all, and an endpoint whose network was
    never named reads exactly like one pointing at testnet until a daemon says which.
    Both are on the operator's screen as a glyph and a word in the bar they glance at,
    rather than only in a table four sections up.
    """
    sync = live.get("sync") or {}
    node = live.get("node") if isinstance(live.get("node"), Mapping) else {}
    panes = live.get("panes") or {}
    wallet = panes.get("wallet") or {}
    peers = (panes.get("peers") or {}).get("rows") or []
    peer_error = (panes.get("peers") or {}).get("error") or ""
    node_fields = panes.get("node") if isinstance(panes.get("node"), Mapping) else node

    blocks = sync.get("blocks")
    behind = sync.get("behind")
    synced = str(sync.get("state") or "")
    # THE VERDICT STRING IS NOT REINTERPRETED HERE. chains/daemon_network.sync_verdict()
    # owns which states mean "fine"; this asks only whether the one it returned is the
    # settled one, by name, so a new state added there reads as a warning rather than
    # as an all-clear. Fail-closed: an unrecognized verdict warns.
    sync_cell = _cell("Sync", synced or "(not established)", (
        f"{sync.get('why') or 'the daemon reported its chain position and nothing was wrong with it'}"
        f"{'' if behind is None else f'. Behind by {behind} block(s) -- a COUNT, never a duration'}"
    ), CELL_OK if synced == SYNC_SYNCED else CELL_WARN if synced else CELL_UNKNOWN)

    height = _reported(node_fields, "blocks")
    blocks_cell = _cell("Blocks", str(blocks if blocks is not None else height or "(not reported)"), (
        "the daemon's own block count. A COUNT and never a duration -- Qt shows the same figure "
        "at the right of its status bar"
    ), CELL_OK if (blocks is not None or height) else CELL_UNKNOWN)

    peers_cell = _cell("Peers", str(len(peers)) if not peer_error else "(not established)", (
        peer_error or (
            "a node with NO peers broadcasts into nothing: the transaction is valid, it confirms "
            "on this node's own chain, and no other wallet ever sees it"
            if not peers else
            "connected peers, counted from getpeerinfo's own rows rather than from a summary field"
        )
    ), CELL_UNKNOWN if peer_error else CELL_OK if peers else CELL_WARN)

    named = bool(live.get("network_named"))
    verdict = str(live.get("network_verdict") or "")
    network_cell = _cell("Network", str(live.get("network") or "(not named)"), (
        f"{verdict} -- only `network_is_test` is permission; the other two both refuse, and for "
        f"different reasons. Reachable is not the question: an alias pointing at mainnet reads "
        f"the same as one pointing at testnet until a daemon says which"
    ), CELL_OK if named and verdict == NETWORK_IS_TEST else CELL_WARN if named else CELL_UNKNOWN)

    # THE THREE LOCK STATES MAP ONTO THREE CELL STATES, ONE EACH, AND NOT TWO ONTO ONE.
    # chains/daemon_wallet.encryption_note()'s own docstring: "THE THIRD STATE IS NOT
    # 'NOT ENCRYPTED', which is the whole reason this is a classifier" -- a viewer can
    # say it could not tell, and reporting "not encrypted" for a daemon that never
    # answered would be a measurement nobody took. So:
    #
    #   not_encrypted    ok       nothing has to be unlocked before this wallet signs.
    #   encrypted        warn     THE fact that decides whether a payout can be made at
    #                             all, and it is invisible in every balance figure on
    #                             every other screen.
    #   not_established  unknown  nobody could tell, which is neither of the above.
    encryption = wallet.get("encryption") or {}
    lock_state = str(encryption.get("state") or "")
    lock_cell = _cell("Wallet", lock_state or "(not established)", (
        f"{str(encryption.get('why') or 'the wallet lock state was not reported').rstrip('. ')}. "
        f"Reported, never offered: nothing on this page would collect a passphrase"
    ), CELL_OK if lock_state == LOCK_NOT_ENCRYPTED
       else CELL_WARN if lock_state == LOCK_ENCRYPTED
       else CELL_UNKNOWN)

    return (sync_cell, blocks_cell, peers_cell, network_cell, lock_cell)


def _reported(fields: object, key: str) -> str:
    """One node_summary() field's value as text, or "" when the daemon did not say it.

    node_summary() wraps every value in {"value", "reported"} so the daemon's silence
    stays visible, and this is the one line that takes the wrapper off. Returning ""
    rather than None keeps the caller's `or` chains readable without making a reported
    empty string indistinguishable from an absence -- no field this is used for can be
    a meaningful empty string, which is said here because it is the assumption.
    """
    if not isinstance(fields, Mapping):
        return ""
    entry = fields.get(key)
    if not isinstance(entry, Mapping) or not entry.get("reported"):
        return ""
    return str(entry.get("value"))


#: WHICH FIGURES GO IN THE BORROWED STATUS BAR, PER CHAIN, IN CORE'S CELL ORDER.
#:
#: A TABLE AND NOT A LIST OF LABELS INSIDE THE FUNCTION, which is where this started
#: and ruff's PERF401 was the second reason to move it: the first is that a label list
#: inside a loop inside a function is per-chain knowledge in the one place a test cannot
#: reach without rendering a page. Keyed by asset, so a seventh chain gets an empty bar
#: of figures rather than a KeyError, and so tests/test_chain_wallet_layout.py can
#: assert that every label named here is a label _FIGURES actually produces -- which is
#: the drift this table could otherwise develop silently.
#:
#: CORE'S ORDER IS sync / height / peers, AND THAT IS WHAT EACH ROW MIRRORS. XRP leads
#: with `server_state` because that is what rippled answers "am I in sync" with, and SOL
#: leads with its commitment rung (added as a static cell below, because it is a
#: configured constant rather than something a response carries) because that is what
#: settlement means on that cluster. Neither is a block count dressed up as one.
_BAR_FIGURES: dict[str, tuple[str, ...]] = {
    "XRP": ("Server state", "Ledger index", "Peers"),
    "SOL": ("Block height", "Slot", "Gossip peers"),
}

#: CELLS THAT ARE NOT READ FROM A RESPONSE, per chain. Configured facts, not measurements.
#:
#: SOL's COMMITMENT RUNG IS THE ONLY ENTRY AND IT IS WHY THIS TABLE EXISTS. It is not in
#: _FIGURES because no reply carries it: it is what this terminal ASKS FOR, and the two
#: commitments it asks for are different on purpose -- discovery reads as low as the
#: cluster permits so a landed deposit is visible, and the GATE reads the rank off the
#: response and credits nothing below `finalized`. An operator looking at a Solana
#: balance needs both halves, and the rung is the fact Core's confirmation count stands
#: in for on every other chain.
#:
#: ITS STATE IS CELL_OK RATHER THAN CELL_UNKNOWN because it was not read and does not
#: need to be: these are this process's own settings, which are known with the cluster
#: down. Rule 14's "echo the parameters that decide the answer".
_BAR_STATIC: dict[str, tuple[Cell, ...]] = {
    "SOL": (
        _cell("Commitment", f"{DISCOVERY_COMMITMENT} / credits at rank {FINALIZED_RANK}", (
            f"two rungs and they are different on purpose. Discovery reads at "
            f"`{DISCOVERY_COMMITMENT}`, which is the LOWEST this cluster's history methods "
            f"accept, so a deposit that has landed but is not settled is VISIBLE; a balance is "
            f"read at `{BALANCE_COMMITMENT}`, because an unsettled balance can go away. Nothing "
            f"is credited below rank {FINALIZED_RANK} on the ladder "
            f"{', '.join(f'{value}={name}' for name, value in COMMITMENT_RANKS.items())}. These "
            f"are RUNGS, not confirmations and not a duration"
        ), CELL_OK),
    ),
}


def translated_status_bar(asset: str, live: Mapping) -> tuple[Cell, ...]:
    """The borrowed status bar for XRP and SOL, built from this chain's own figures.

    THE CELLS ARE THIS CHAIN'S VOCABULARY AND THE POSITIONS ARE CORE'S, which is the
    whole borrowed-layout idea: an operator who reads BTC's bar left to right as
    sync / height / peers / network / lock finds the same KINDS of fact in the same
    places here, under the names this chain actually uses.

    THE ACCOUNT CELL STANDS WHERE THE LOCK CELL DOES, and it is the honest substitute:
    there is no wallet at either server to lock. What decides whether a payout can
    happen on these chains is not a passphrase, it is whether this process was given an
    account to ask about at all -- and an empty account is NOT worked around anywhere,
    because it is a custody decision with no safe default.

    A LABEL IN _BAR_FIGURES THAT _FIGURES DOES NOT PRODUCE IS SKIPPED RATHER THAN
    RENDERED EMPTY, and the drift that would cause is caught by a test instead of by a
    reader: tests/test_chain_wallet_layout.py asserts the two tables agree, both
    directions, for every chain in either.
    """
    asset = str(asset).upper()
    by_method = {}
    for row in live.get("answers") or ():
        method = str(row.get("translated") or "")
        answer = row.get("answer")
        if method and isinstance(answer, Mapping):
            by_method.setdefault(method, answer)
    rows = {row.label: row for row in figure_rows(asset, by_method)}

    cells: list[Cell] = list(_BAR_STATIC.get(asset, ()))
    cells.extend(_figure_cell(rows[label]) for label in _BAR_FIGURES.get(asset, ()) if label in rows)
    protocol = str(live.get("protocol") or "")
    cells.append(_cell("Network", protocol or "(not established)", (
        f"the command map this chain is asked through is `chains/{protocol}_rpc_map`. WHICH "
        f"network the endpoint is on is not answered by these reads: it is the endpoint's own "
        f"configuration, which is on the Configuration section above"
    ), CELL_OK if protocol else CELL_UNKNOWN))
    account = str(live.get("account") or "")
    cells.append(_cell("Account", account or "(none configured)", (
        "the shared deposit account from the configuration, NOT the payout account -- deriving "
        "the payout account would mean decoding the signing seed, and a read-only page must not "
        "touch key material to decide what to display"
        if account else
        "no account was configured, so the three questions that need one could not be asked. "
        "That is not worked around here: the account is a custody decision with no safe default"
    ), CELL_OK if account else CELL_WARN))
    return tuple(cells)


def _figure_cell(row: FigureRow) -> Cell:
    """One resolved figure as a status-bar cell. An absence is `unknown`, never `warn`.

    A FIGURE THAT WAS NOT READ IS NOT A BAD FIGURE, and this is the single line where
    that distinction is enforced for the borrowed bars. `warn` would make an unasked
    render look like a cluster in trouble.
    """
    return _cell(row.label, row.value or "(not established)",
                 row.why or row.note, CELL_OK if row.found else CELL_UNKNOWN)


def icp_status_bar(live: Mapping) -> tuple[Cell, ...]:
    """ICP's borrowed status bar: the three ICRC-1 reads and the local derivation.

    THE DERIVATION CELL IS THE ONE WITH NO CORE COUNTERPART AND IT IS THE LOUDEST. It
    asks the LEDGER to compute the account identifier this repository derived, from the
    same principal and subaccount. A disagreement means every ICP deposit address this
    terminal publishes is wrong in the same way: a customer pays, the watcher polls an
    account that stays at zero forever, and nothing errors. So a derivation that
    disagrees warns, and one that was not checked reads `unknown` rather than passing.

    THE ADDRESS CELL IS ALWAYS READABLE, which is why it is first. own_address() is a
    SHA-224 derivation over the configured principal -- local, no call -- so it answers
    with the replica down, with dfx absent from the image, and after the request budget
    is spent. An operator who can see nothing else should still be able to read the
    account the rest of the panel is about.
    """
    rows = {str(row.get("key")): row for row in (live.get("rows") or ())}
    own = live.get("own_address") or {}
    cells: list[Cell] = []
    address = str(own.get("address") or "")
    cells.append(_cell("Account", address or "(not derived)", (
        "the desk's own account identifier, derived LOCALLY from the configured principal -- no "
        "call, so it is readable with the replica down"
        if address else
        f"the derivation itself failed, which is a configuration fact rather than an outage: "
        f"{own.get('error') or '(no reason was reported)'}"
    ), CELL_OK if address else CELL_WARN))
    for key, label, note in (
        ("balance", "Balance", "ICP at the desk's own account. This ledger has no pending and no "
                               "immature: a transfer is final when the ledger returns a block index"),
        ("fee", "Fee", "what a transfer from here costs, read from the LEDGER rather than from a "
                       "constant -- a mismatch is a BadFee naming the expected figure, and nothing moves"),
    ):
        row = rows.get(key)
        if not isinstance(row, Mapping):
            cells.append(_cell(label, "(not asked)", (
                f"no {key} read was made on this render. {note}"
            ), CELL_UNKNOWN))
            continue
        if not row.get("ok"):
            cells.append(_cell(label, "(could not be read)", (
                f"{row.get('error') or '(no reason was reported, which is itself a defect)'}"
            ), CELL_UNKNOWN))
            continue
        cells.append(_cell(label, str(row.get("value")), note, CELL_OK))
    derivation = rows.get("derivation")
    if not isinstance(derivation, Mapping):
        cells.append(_cell("Derivation", "(not asked)", (
            "the ledger was not asked whether it agrees with this repository's address "
            "derivation. That check has no Core counterpart and nothing else in this tree asks it"
        ), CELL_UNKNOWN))
    elif not derivation.get("ok"):
        cells.append(_cell("Derivation", "(could not be read)",
                           str(derivation.get("error") or "(no reason was reported)"), CELL_UNKNOWN))
    else:
        value = derivation.get("value")
        agrees = bool(value[0]) if isinstance(value, Sequence) and value else False
        cells.append(_cell("Derivation", "agrees" if agrees else "DISAGREES", (
            f"{value}. The ledger computed the account identifier this repository derived, from "
            f"the same principal and subaccount. A disagreement means every ICP deposit address "
            f"this terminal publishes is wrong in the same way"
        ), CELL_OK if agrees else CELL_WARN))
    return tuple(cells)


# =============================================================================
# THE ASSEMBLER
# =============================================================================


class Asking(NamedTuple):
    """What one render's ask produced: whether it happened, what came back, why not.

    THREE FIELDS AND ONE OBJECT, which is the shape services/chain_panel.Budget uses
    and for the same reason its docstring gives about its two: they are not
    independent. ruff's PLR0913 flagged qt_layout() at six parameters and rule 12 is
    explicit that the fix is to extract rather than to raise the ceiling -- but the
    grouping is not a formality, because these three disagreeing is a defect this page
    has already shipped.

    THE 500 THAT MADE THEM ONE THING. `?ask` on a chain with no adapter set asked=True
    while `live` stayed None, and the template walked into the pane block with nothing
    to render. The crash was the lucky outcome; the defect under it is rule 14's "make
    'did nothing' look different from 'did work'", and a page claiming to have asked a
    chain it never contacted reads as a daemon that answered nothing. Carrying the three
    in one object means a reader sees all three at the call site and `qt_layout()`
    checks them together in one place.

    `cannot_ask` IS NOT THE SAME AS `asked` BEING FALSE, which is the distinction the
    status bar renders differently: "you have not clicked" is a page waiting for the
    operator, and "there is no adapter for this chain" is a configuration fault they
    have to go and fix.
    """

    asked: bool
    live: object = None
    cannot_ask: str = ""


def qt_layout(asset: str, kind: str, asking: Asking, *, panes: Sequence = ()) -> dict:
    """The whole Qt shape for one chain's panel. One dict, and it holds no decision.

    `panes` IS PASSED IN RATHER THAN IMPORTED, AND THAT IS NOT STYLE -- IT IS THE CYCLE.
    services/chain_panel.py imports this module, so this module importing
    PANE_QUESTIONS back out of it would be a circular import that fails at startup. The
    caller already has the tuple; handing it over keeps the dependency pointing one way
    and makes this function callable from a test with a seeded pane list, which is how
    tests/test_chain_wallet_layout.py asserts the rail's Transactions link resolves to
    the section the Transactions pane renders.

    AN EMPTY `panes` IS LEGAL and produces an empty anchor map. That is the honest
    answer for a caller that has no pane list, and it fails visibly rather than
    silently: the rail's Transactions link on a translated chain then points at an id
    nothing carries, which is exactly what the anchor test fails on.

    EVERY BRANCH HERE IS ON `kind` OR ON `asked`, never on an asset string, which is
    the property the template depends on: a `{% if data.asset == 'GRC' %}` in markup
    would be a chain decision in a place that cannot be called with seeded inputs
    (rule 10). The per-chain knowledge is in _RAIL_EXTRA and _FIGURES above, both
    keyed tables, both reachable from a test with a seeded asset string.

    `asking.asked` IS CHECKED SEPARATELY FROM `asking.live` BEING NON-NONE, and the two
    are not interchangeable -- see Asking above for the 500 that made them one object.
    Both are checked here, so a caller that gets it wrong produces the not-asked bar
    instead of a traceback.

    THE STATUS BAR IS NEVER EMPTY on any render of any chain in any state, and that is
    the one invariant worth stating as such: a wallet's status bar is the part a user
    glances at instead of reading, and a blank one is rule 14's ambiguous gap in the
    place it costs the most.
    """
    kind = str(kind)
    asset = str(asset).upper()
    rail = rail_entries(asset, kind)
    layout = {
        "rail": rail,
        "rail_states": RAIL_STATES,
        # THE GROUPS THE RAIL ACTUALLY USES, IN THE ORDER THEY APPEAR, derived from the
        # entries rather than listed. The template renders one labeled block per group,
        # and a hardcoded ("rail", "node") there would render an empty "Node window"
        # heading on the three chains that have no node window -- rule 14's blank
        # region, produced by a list that outlived its reason.
        "rail_groups": tuple(
            {"key": group, "label": RAIL_GROUP_LABELS.get(group, group)}
            for group in dict.fromkeys(entry.group for entry in rail)
        ),
        "borrowed": BORROWED_LAYOUT_NOTICE.get(kind, BORROWED_LAYOUT_NOTICE["no_rpc"]),
        "is_core": kind == "core_wallet",
        "tx_columns": tx_columns(kind),
        "balances": (),
        "tx_rows": (),
        "figures": (),
        # EVERY PANE QUESTION'S FRAGMENT ID, KEYED BY TITLE, so the template can put the
        # id on the section and the rail can link at it without either spelling a slug.
        # Built for all three kinds even though only `translated` renders pane sections:
        # the map costs nothing, and a template that has to ask whether a key exists
        # before using it is a template holding a branch.
        "pane_anchors": {pane.title: pane_anchor(pane.title) for pane in panes},
        "status": not_asked_bar(asset, asking.cannot_ask),
    }
    live = asking.live
    if not asking.asked or not isinstance(live, Mapping):
        return layout
    if kind == "core_wallet":
        # `pane_payload` AND NOT `panes`, WHICH IS THE PARAMETER. Two different things
        # with nearly one name: `panes` is the QUESTION list this function was handed,
        # and this is the ANSWER payload chains/daemon_wallet.wallet_pane() produced.
        # Rebinding the parameter here worked and read as a bug.
        pane_payload = live.get("panes") or {}
        wallet = pane_payload.get("wallet") or {}
        columns = layout["tx_columns"]
        layout["balances"] = balance_card(wallet.get("balances"))
        layout["tx_rows"] = tuple(
            tx_cells(row, columns)
            for row in ((pane_payload.get("transactions") or {}).get("rows") or ())
            if isinstance(row, Mapping)
        )
        layout["status"] = core_status_bar(live)
        return layout
    if kind == "translated":
        by_method = {}
        for row in live.get("answers") or ():
            method = str(row.get("translated") or "")
            answer = row.get("answer")
            if method and isinstance(answer, Mapping):
                by_method.setdefault(method, answer)
        layout["figures"] = figure_rows(asset, by_method)
        layout["status"] = translated_status_bar(asset, live)
        return layout
    layout["status"] = icp_status_bar(live)
    return layout
