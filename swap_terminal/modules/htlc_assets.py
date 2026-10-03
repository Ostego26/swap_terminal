#!/usr/bin/env python3
"""HOW a pair settles: which of the two atomic drivers covers it, or neither.

Role: submodule (one decision -- settlement_mode(), callable with two strings and
      no chain, no database, no socket and no clock)
Reads: modules/htlc_timelock.SECONDS_PER_BLOCK, which is the existing authority for
      "this chain has a P2SH HTLC and this driver knows its block interval". Nothing
      else. The chain client classes are read only by script_client_classes(), which
      imports them inside the function body on purpose -- see there.
Writes: nothing
Can move funds: no. Nothing here signs, builds, broadcasts or chooses an amount. It
      answers a question about which code path WOULD move the funds, and the answer
      is prose plus a token.
Mainnet-safe: yes. It is a pure function of two asset strings.

=============================================================================
THE DEFECT, MEASURED 2026-10-03
=============================================================================

This tree has TWO atomic settlement mechanisms and a THIRD, custodial path, and
nothing on any screen said which one a given pair used. The operator did not know
and argued about it, and the argument was unresolvable from the screens:

    atomic_swap.py          both legs as P2SH HTLCs. CLIENTS = {BTC, GRC, LTC} at
                            line 148, ASSETS derived from it, ASSET_PAIRS derived
                            from ASSETS -- six directed pairs.
    atomic_swap_xrp.py      XRP against BTC/LTC/GRC, where the XRP leg is an
                            EscrowCreate carrying a PREIMAGE-SHA-256 crypto-condition
                            rather than a P2SH script. A different protocol on one
                            side, interlocked by the same sha256.
    the Flask terminal      open_swap.py, swap_terminal/services/, workers/, routes/.
                            CUSTODIAL AND TRUST-BASED: the customer sends to an
                            address this desk owns (a key derived in the hot wallet
                            by getnewaddress, or the one shared XRP account), and the
                            desk pays out of its own inventory. There is no hashlock
                            and no atomicity anywhere on that path.

MEASURED, NOT ASSUMED (rule 17): grepped swap_terminal/services/, workers/ and
routes/ for `atomic`, `htlc` and `HTLC` on 2026-10-03. Three files mention an atomic
driver and all three mentions are COMMENTS -- services/pricing.py:72 and
services/coinpaprika.py:276 cite atomic_swap_xrp.py as a fellow reader of a rate,
services/admin_view.py:1316 cites atomic_swap.py:435 for a finding. Zero imports,
zero calls. `grep -rn '^\\s*\\(import\\|from\\)\\s*atomic_swap'` over every .py in the
tree finds five lines and every one is in tests/. So a swap opened through the web
terminal, or through open_swap.py, CANNOT be atomic today no matter what the pair is,
and a pair having a working atomic driver says nothing about how the terminal will
settle it.

That is the sentence this module exists to put on a screen, because the two facts
are both true at once and only one of them was visible:

    GRC -> XRP   atomic_swap_xrp.py covers this pair.
                 A swap opened here is custodial and has no hashlock.

=============================================================================
WHY THE ASSET SETS ARE DERIVED AND NOT RE-LISTED (rules 8 and 11)
=============================================================================

Before this module there were THREE hand-written copies of "the assets with a P2SH
HTLC client", found by grepping for the client class names on 2026-10-03:

    atomic_swap.py:148            CLIENTS = {"BTC": BTCClient, "GRC": GRCClient, "LTC": LTCClient}
    atomic_swap_xrp.py:1224       SCRIPT_CLIENTS = {"BTC": BTCClient, "LTC": LTCClient, "GRC": GRCClient}
    tests/test_htlc_contract_api.py:55
                                  CLIENTS = {"BTC": BTCClient, "LTC": LTCClient, "GRC": GRCClient}

Three spellings of one dict, in three key orders, agreeing on the day each was
written. atomic_swap_xrp.py's own comment beside its copy says the map is "imported
rather than re-implemented", which was the intention and was not what the line did.
A fourth copy is what rule 8 calls a bug with a delay on it, so this module is the
survivor and all three now read SCRIPT_HTLC_CLIENTS from here.

The VOCABULARY underneath them is derived one level further down.
SCRIPT_HTLC_ASSETS is `tuple(sorted(SECONDS_PER_BLOCK))` -- exactly what
atomic_swap_xrp.SCRIPT_CHAINS was already derived from -- because a chain belongs in
that table only if this tree knows the block interval that turns a timelock policy
in hours into a height, and a chain whose interval is unknown cannot have a contract
built on it. So adding a fourth script chain means adding one row to
modules/htlc_timelock.SECONDS_PER_BLOCK and one client class here, and
script_client_classes() REFUSES rather than silently covering less than the
vocabulary claims if the second half is forgotten.

=============================================================================
HOW A MODULE AND A ROOT ENTRY POINT RESOLVE RULE 10 HERE
=============================================================================

Rule 10: files are entry points at the repository root, files run modules, and a
module must never import a root file. The decision therefore cannot live in either
driver, and it cannot reach UP into one to read a table.

That is why PROVEN_LIVE MOVED HERE from atomic_swap_xrp.py rather than being read
from there. It is the record of which swaps have actually COMPLETED on this code
path, which is a fact about the tree and not about one entry point -- and the moment
a second surface needed it (this module, open_swap.py, show_swap.py,
swap_readiness.py, the web pair list) a root-level table would have forced either an
upward import or a second copy of the evidence. The driver imports it back and still
prints it from `say_what_has_actually_run()`, so nothing about that banner changed.

The import direction that is already established and is not being fought: both root
drivers put `swap_terminal/` on sys.path and import `modules.*` absolutely
(atomic_swap.py:110, atomic_swap_xrp.py:259). A root file importing this module is
the normal arrangement here; this module importing a root file would not be.

=============================================================================
COVERED IS NOT RUN. THEY ARE DIFFERENT CLAIMS AND ARE REPORTED SEPARATELY
=============================================================================

atomic_swap_xrp.py's own docstring is careful about this and so is this module. A
driver that has a client, a network allowlist and a funding method for a chain
COVERS that chain. A chain a full swap has COMPLETED on, with txids, is PROVEN. The
two were conflated inside that driver once already: it printed Gridcoin's two
completed swaps on every chain, which told a Bitcoin operator that a failure was a
regression on a route no run had ever taken.

So settlement_mode() returns a sentence that says which of the two it has, and
`proven` is read off a table of runs rather than asserted in prose -- PROVEN_LIVE for
the escrow driver, PROVEN_SCRIPT_PAIRS for the script one. As of 2026-10-03:

    XRP -> GRC    covered AND run green   OK=15 FAIL=0, 2026-09-29
    XRP -> LTC    covered AND run green   OK=15 FAIL=0, 2026-09-29
    BTC -> GRC    covered AND run green   OK=13 FAIL=0, 2026-09-30
    XRP -> BTC    covered, NEVER RUN      BTC has a client; no swap has completed
    GRC/LTC/BTC -> XRP
                  covered, NEVER RUN      chain-first has not been run on any chain
    the other five script pairs
                  covered, NEVER RUN      GRC -> BTC included, although it is the same
                                          five acts in the other order over the same
                                          two daemons

THE BTC -> GRC ROW WAS MISSING FROM THIS TABLE UNTIL IT WAS CHECKED, and the way it was
missing is the thing worth recording. The first draft of this module stated that no
script<->script swap had ever completed, and said so in the register of a measurement:
"grepped atomic_swap.py for OK=, FAIL=, COMPLETED and PROVEN". That grep was run and
found nothing -- of the file it was pointed at. The evidence is in
docs/branch_coverage.md:156, with six txids, and the claim was false for three days
before anybody wrote it down. Rule 17's exact shape: the denominator of a grep is the
files it searched, and a reason to believe something is not the same as having checked
it.
"""

from __future__ import annotations

from modules.htlc_timelock import SECONDS_PER_BLOCK

#: The assets whose HTLC is a P2SH script -- the ones atomic_swap.py can put EITHER
#: leg on, and the ones atomic_swap_xrp.py can put its non-XRP leg on. DERIVED from
#: modules/htlc_timelock.SECONDS_PER_BLOCK, which is the table that has to know a
#: chain's block interval before a timelock on it can be expressed at all.
#:
#: Sorted, so every surface that prints it prints the same order. Measured 2026-10-03:
#: ('BTC', 'GRC', 'LTC').
SCRIPT_HTLC_ASSETS: tuple[str, ...] = tuple(sorted(SECONDS_PER_BLOCK))

#: The assets whose hashlock is NOT a script. XRP's EscrowCreate carries a
#: PREIMAGE-SHA-256 crypto-condition whose fingerprint is sha256 of the same preimage
#: the P2SH branch commits to -- which is the only reason one secret opens both legs
#: -- but it is not a redeem script, there is no scriptSig to read on that side, and
#: it needs its own driver. One member today, and it is a tuple rather than a bare
#: string so that a second escrow-style chain is a row rather than a rewrite.
ESCROW_HTLC_ASSETS: tuple[str, ...] = ("XRP",)

#: The three modes, as tokens a caller may branch on. The prose is in the second
#: element of settlement_mode()'s return value and is never branched on.
MODE_SCRIPT_HTLC = "atomic-script-htlc"
MODE_ESCROW_HTLC = "atomic-xrp-escrow"
MODE_BROKERED_ONLY = "brokered-only"

#: Every mode, so a caller rendering a key or a legend reads it off the table rather
#: than spelling the three again (the shape services/pair_view.CUSTOMER_STATES uses).
MODES: tuple[str, ...] = (MODE_SCRIPT_HTLC, MODE_ESCROW_HTLC, MODE_BROKERED_ONLY)

#: The driver each atomic mode names, as an operator would type it. Not a path in a
#: comment: these strings are PRINTED, and a reader who cannot find the file the
#: sentence names has been told nothing.
SCRIPT_DRIVER = "atomic_swap.py"
ESCROW_DRIVER = "atomic_swap_xrp.py"

#: WHICH CHAIN THE INITIATOR IS ON, for an XRP<->script swap. The initiator picks the
#: secret, funds first, and takes the LONGER timelock; the participant funds second,
#: takes the shorter one, and claims last with the secret the initiator was forced to
#: publish. Both directions run the same protocol; what changes is which chain is
#: CLAIMED first, and therefore which of the two preimage readers is used.
#:
#: MOVED HERE FROM atomic_swap_xrp.py:475 ON 2026-10-03, for the reason the module
#: docstring gives about PROVEN_LIVE: this module has to say which direction a pair
#: would be run in, and spelling "xrp-first" a second time here while the driver spelled
#: it there is the duplicated vocabulary rule 11 forbids -- two strings that have to be
#: equal, in two files, with nothing checking. The driver imports them back, so every
#: existing `== XRP_FIRST` comparison and `choices=` list is unchanged.
#:
#: LEGACY_CHAIN_FIRST (`grc-first`, the pre-2026-09-29 spelling) deliberately stayed in
#: the driver: it is a CLI compatibility alias that argparse maps and never carries
#: further, so it is an interface detail of that entry point rather than part of the
#: vocabulary a decision is made against.
XRP_FIRST = "xrp-first"
CHAIN_FIRST = "chain-first"
DIRECTIONS = (XRP_FIRST, CHAIN_FIRST)

#: The direction every PROVEN_LIVE entry below was run in, named once so the table and
#: the sentence that reads it cannot disagree. The driver's own comment inside
#: PROVEN_LIVE says chain-first "is NOT listed, on EITHER chain", and
#: test_every_PROVEN_LIVE_sentence_names_the_direction_it_ran_in holds this constant
#: against the sentences, so a chain-first entry cannot be dropped in under a constant
#: that still says otherwise.
PROVEN_DIRECTION = XRP_FIRST

#: The chains a full XRP<->script swap has actually COMPLETED on, and the evidence.
#:
#: MOVED HERE FROM atomic_swap_xrp.py ON 2026-10-03, unchanged in content. It was a
#: root-file table and five surfaces needed it; see "HOW A MODULE AND A ROOT ENTRY
#: POINT RESOLVE RULE 10 HERE" above. The driver imports it back and prints it from
#: say_what_has_actually_run() exactly as before.
#:
#: Absence from this table is not a gap in the table -- it is the honest state of a
#: chain, and every surface says so out loud rather than letting silence read as
#: reassurance.
PROVEN_LIVE: dict[str, str] = {
    # EARNED 2026-09-29, ON THIS PATH, BY A RUN. It was emptied earlier the same day when
    # both runners moved off Gridcoin's createhtlc/claimhtlc onto the chain clients: the
    # 2026-09-27 evidence belonged to the route that produced it, and carrying it across
    # would have told an operator a failure was a regression on a route nothing had taken.
    # This entry is a different run, of this code, with its own txids.
    "GRC": (
        "xrp-first COMPLETED on this code path 2026-09-29, OK=15 FAIL=0: XRP escrow "
        "56E03AA90B97BD8E.. (OfferSequence 21051299), GRC HTLC 25749c35389772e9.. at vout 1 "
        "on P2SH 2N775AaLXRuxBoXS8q.., claim c8ec9f79e541bfa7.. crediting 66.09001328 GRC, "
        "XRP finish C72EBF3F56D97180.. and B's balance +1000000 drops. The secret was read "
        "back out of the claim's 237-byte scriptSig, not out of memory. A failure here is a "
        "regression, not a discovery."
    ),
    # EARNED 2026-09-29, the second chain and the first one this driver could not fund at
    # all that morning. It took three attempts and each failure was a real defect the seeded
    # tests could not have found: the script client read Config.RPC directly so a
    # conf-resolved daemon failed at step 5b; then create_contract() was called positionally
    # in the BTC/GRC order, which LTC does not share.
    "LTC": (
        "xrp-first COMPLETED on this code path 2026-09-29, OK=15 FAIL=0: XRP escrow "
        "27627931A82718BF.. (OfferSequence 21051302), LTC HTLC 6e06a4c906c1d5db.. at vout 1 on "
        "P2SH QbWXa1K6v74M7qWcZN8bXNPu4WMJMybuFh, claim 7bd4f0ef9602c2a1.. paying 0.02217136 LTC "
        "to rltc1qrlv7f9majkfujxn6cgspgx998nc60umpjce6vv, XRP finish 0DEB63047A0CDDE3.. and B's "
        "balance 118999970 -> 119999970 drops. The secret was read back out of the claim's "
        "236-byte scriptSig. Priced at the live rate, 44.90069981 XRP per LTC from CoinPaprika, "
        "not a hand-supplied figure. A failure here is a regression, not a discovery."
    ),
    # chain-first is NOT listed, on EITHER chain. It is the same five acts in the other order
    # and shares every function, but it has not been run on this path and sharing code is not
    # evidence -- that is the whole reason this table is keyed by what ran rather than by what
    # should work. Two of the three defects the LTC run found were in code both directions
    # share, and the xrp-first tests were green for all of them.
}

#: The DIRECTED script<->script pairs a full swap has actually COMPLETED on, and the
#: evidence. The same record PROVEN_LIVE is, for the other driver.
#:
#: THIS TABLE EXISTS BECAUSE THE FIRST DRAFT OF THIS MODULE CLAIMED IT WAS EMPTY, and
#: that claim was false. It said "no completed script<->script swap is recorded anywhere
#: in this tree", measured by grepping atomic_swap.py for OK=, FAIL=, COMPLETED and
#: PROVEN -- which is a measurement of ONE FILE presented as a statement about the tree.
#: docs/branch_coverage.md:156 records BTC -> GRC completing OK=13 FAIL=0 on 2026-09-30
#: with six txids. Rule 17 exactly: a reason to believe something is not the same as
#: having checked it, and the denominator of a grep is the files it was pointed at.
#:
#: KEYED BY DIRECTED PAIR, not by chain, because there is no single "initiator chain" to
#: key on when both legs are scripts -- unlike PROVEN_LIVE, where one side is always XRP
#: and PROVEN_DIRECTION can turn a chain into a direction. BTC -> GRC ran; GRC -> BTC has
#: not, and they are different claims for the same reason the two XRP directions are.
#:
#: THE EVIDENCE IS COPIED FROM docs/branch_coverage.md RATHER THAN SUMMARIZED, so the
#: txids a reader would check are the ones the run produced. Carried HERE because a
#: Markdown file is a mirror and never an authority for a decision (rule 5), and this
#: decision is read by five surfaces; the document stays as the long-form record.
PROVEN_SCRIPT_PAIRS: dict[tuple[str, str], str] = {
    ("BTC", "GRC"): (
        "COMPLETED 2026-09-30, OK=13 FAIL=0, and the THIRD PAIR SHAPE this tree has run: two "
        "script chains, both legs P2SH, no XRP and no EscrowFinish anywhere. BTC HTLC "
        "9817d072ca6c1176.. on P2SH 2N7ZzkWTmYEbxjszGJ57Wdi5txGrpn9vu3j, GRC HTLC "
        "df7c2043dfe568bb.. at vout 1, GRC claim f2803eae34bbd252.. paying 999.99 GRC, the secret "
        "read off the GRC chain (32 bytes, never messaged), BTC claim 1aa16133e4ea29c8.. paying "
        "0.0001 BTC. Recorded in docs/branch_coverage.md. A failure here is a regression, not a "
        "discovery."
    ),
    # THE OTHER FIVE DIRECTED PAIRS ARE NOT LISTED, and GRC -> BTC in particular is not,
    # although it is the same five acts in the other order over the same two daemons.
    # docs/branch_coverage.md makes the argument at length and from a measurement: the
    # XRP<->LTC run took three attempts, both failures were in code the XRP<->GRC run had
    # already exercised, and all fifteen tests covering the broken call passed because the
    # test double carried the same wrong signature. "Shares code with something that ran"
    # is the claim those attempts refuted.
}

#: Why an asset has no atomic path at all, per asset, in the words an operator needs.
#:
#: SOL is the whole table today. The reason is not "nobody got round to it": there is
#: no HTLC on that chain in this tree, the stub that claimed one could not run, and it
#: was deleted in c4ea027 ("Delete the fake Solana HTLC, and correct a constant that
#: predicted its own staleness"). atomic_swap.py's own --pairs output has said this
#: since then; this is the same sentence where the brokered surfaces can reach it,
#: rather than a second wording of it (rule 8).
#:
#: TWO LENGTHS PER ENTRY, (short, long), FOR THE REASON settlement_verdict() GIVES
#: ABOUT ITS OWN TWO FORMS: a matrix cell and a per-swap block want the same fact at
#: different sizes, and the way to stop a short form contradicting a long one is to
#: keep them in one place rather than to compose one of them somewhere else. The
#: short one still carries the commit hash, because the hash is the only part a
#: reader can act on without asking anybody.
NO_HTLC_REASON: dict[str, tuple[str, str]] = {
    "SOL": (
        "SOL has no HTLC (c4ea027)",
        "SOL has no HTLC in this tree -- the stub that existed could not run and was deleted "
        "(c4ea027), so there is nothing for a hashlock to commit to on that chain",
    ),
}

#: What the brokered terminal actually does, appended to EVERY sentence this module
#: returns including the atomic ones.
#:
#: IT IS ON THE ATOMIC SENTENCES DELIBERATELY, and that is the defect being fixed
#: rather than noise. The pair the operator argued about was GRC -> XRP, where BOTH
#: statements are true at once: an atomic driver covers it, and a swap opened through
#: this terminal does not use that driver. A sentence carrying only the first half is
#: how a custodial deposit gets made by somebody who believes a hashlock is holding
#: it. Rule 14: state what the number means next to the number.
BROKERED_PATH_NOTE = (
    "A swap opened through this terminal does NOT use either atomic driver: that path is "
    "CUSTODIAL and trust-based -- the deposit goes to an address this desk owns and the payout "
    "comes out of the desk's own inventory, with no hashlock and no atomicity. Nothing in "
    "swap_terminal/services, workers or routes imports either driver (grepped 2026-10-03)."
)


def script_client_classes() -> dict[str, type]:
    """asset -> the client class that owns its P2SH HTLC. THE ONLY copy of this map.

    Three copies of this dict existed on 2026-10-03 and the module docstring names
    all three with their line numbers. They are all this function now.

    WHY THE IMPORTS ARE IN THE BODY AND NOT AT MODULE SCOPE. The settlement decision
    is read by the brokered web surfaces -- services/pair_view.py and the two
    operator tools -- and modules/atomic_btc_client.py and its two siblings pull in
    the whole JSON-RPC and logging stack behind them. A page that only wants to know
    HOW a pair would settle must not import three chain clients to find out, and rule
    12 is explicit that import-time weight and import-time side effects are a cost
    the linter cannot see. Deferring them keeps settlement_mode() a pure function of
    two strings, which is also what makes it testable without a chain (rule 10).

    WHY THE MAP IS SPELLED OUT RATHER THAN ASSEMBLED FROM THE ASSET NAME. The three
    modules and classes do follow a convention -- modules/atomic_btc_client.py holds
    BTCClient -- so `importlib.import_module(f"modules.atomic_{asset.lower()}_client")`
    would derive it and remove the last hand-written line. It is not done because rule
    2's guard cuts the other way: a reader greping the tree for `BTCClient` to find its
    callers would find none, and "I could not find a caller" is exactly the sentence
    that rule tells us not to trust. A name assembled at runtime is invisible to the
    only search method that works across shell scripts, launchers and docs.

    THE CROSS-CHECK IS THE DERIVATION. SCRIPT_HTLC_ASSETS comes from
    modules/htlc_timelock.SECONDS_PER_BLOCK, so the two halves of "this chain can carry a
    P2SH HTLC" -- the block interval that expresses a timelock, and the client that builds
    and spends the script -- are kept in two places on purpose and compared here. A client
    for a chain whose interval nobody recorded RAISES, because every timelock built for it
    would be a KeyError in the middle of a run. A chain whose interval exists and whose
    client does not is OMITTED, for the reason the comment in the body gives.
    """
    # The three noqa: PLC0415 below are a checked claim and not a quieted finding
    # (rule 19). Deferred on purpose: a web page asking how a pair settles must not
    # drag three JSON-RPC clients in with it, and the docstring above says so at
    # length. ruff's I001 reformats the block if the reason rides on the first line,
    # so the reason is here and the markers are bare.
    from modules.atomic_btc_client import BTCClient  # noqa: PLC0415
    from modules.atomic_grc_client import GRCClient  # noqa: PLC0415
    from modules.atomic_ltc_client import LTCClient  # noqa: PLC0415

    classes: dict[str, type] = {"BTC": BTCClient, "GRC": GRCClient, "LTC": LTCClient}
    # A CHAIN IN THE VOCABULARY WITH NO CLIENT IS OMITTED, NOT RAISED, and that is the
    # difference between the two halves of this check.
    #
    # SCRIPT_HTLC_ASSETS can legitimately be ahead of this map for the length of one
    # commit: somebody adds a fourth chain's block interval to
    # modules/htlc_timelock.SECONDS_PER_BLOCK before its client exists. Raising here
    # would take atomic_swap.py's --help and --pairs down with it, and the tree already
    # has the RIGHT handling for that state -- atomic_swap_xrp.py's
    # refuse_if_the_htlc_cannot_be_funded() declines that chain before a single network
    # call, with a message naming what it needs. Omitting the chain is what keeps that
    # guard reachable instead of pre-empting it with an import error.
    #
    # The two sets ARE equal today and a test asserts it, so settlement_mode() cannot go
    # on claiming an atomic path for a chain no driver can fund without that test failing.
    # That is the cross-check; this is not the place to enforce it.
    extra = [asset for asset in classes if asset not in SCRIPT_HTLC_ASSETS]
    if extra:
        raise KeyError(
            f"{extra} have an HTLC client here and are NOT in SCRIPT_HTLC_ASSETS, so "
            f"modules/htlc_timelock.SECONDS_PER_BLOCK does not know their block interval. Every timelock "
            f"built for them would be arithmetic on a missing number. Add the interval there."
        )
    # Rebuilt in SCRIPT_HTLC_ASSETS order so every caller iterating it prints the same
    # order as every surface printing the vocabulary itself.
    return {asset: classes[asset] for asset in SCRIPT_HTLC_ASSETS if asset in classes}


def has_proven_run(from_asset: str, to_asset: str) -> bool:
    """Has a full swap of THIS directed pair actually completed on THIS code path?

    READ OFF PROVEN_LIVE, never asserted. The table is keyed by the SCRIPT chain and
    every entry is PROVEN_DIRECTION, which for an XRP<->script pair means the XRP leg
    was funded first -- so the proven directed pairs are XRP -> <chain in the table>
    and nothing else.

    FOR A SCRIPT<->SCRIPT PAIR IT IS PROVEN_SCRIPT_PAIRS, read by the directed pair.
    This function returned False for every such pair in its first draft, on the strength
    of grepping atomic_swap.py for OK=, FAIL= and COMPLETED -- which found nothing,
    because the evidence is in docs/branch_coverage.md and not in the driver. BTC -> GRC
    completed OK=13 FAIL=0 on 2026-09-30. A grep of one file is not a measurement of the
    tree, and reporting it as one is what rule 17 forbids.

    What atomic_swap.py itself prints as PROVEN after step 1 is "both daemons answered,
    both are on test networks, and the timelock ordering holds" -- a statement about a
    precondition, not about a swap. That is why it is not, and should not be, the table.
    """
    source, destination = from_asset.strip().upper(), to_asset.strip().upper()
    if (source, destination) in PROVEN_SCRIPT_PAIRS:
        return True
    if PROVEN_DIRECTION != XRP_FIRST:
        # Unreachable while the constant above holds, and here rather than in a
        # comment because the mapping from PROVEN_LIVE's key to a DIRECTED pair
        # depends on it entirely. If a chain-first run is ever recorded, this
        # function is the thing that has to change with it.
        raise NotImplementedError(
            f"PROVEN_LIVE is keyed by script chain and read as {PROVEN_DIRECTION}; this function only knows "
            f"how to turn an xrp-first table into directed pairs"
        )
    return source in ESCROW_HTLC_ASSETS and destination in PROVEN_LIVE




#: The three words that go in front of a verdict, as a reader scans a column.
#:
#: SEPARATE FROM THE MODE TOKENS ON PURPOSE, and the pair GRC -> XRP is why. The MODE
#: says which mechanism COULD settle the pair; the word says what EVIDENCE there is
#: for it. Two pairs can share `atomic-xrp-escrow` and differ entirely in whether
#: anything has ever run -- XRP -> GRC has txids and GRC -> XRP has none -- so
#: collapsing them into one token would flatten exactly the distinction
#: atomic_swap_xrp.py's own PROVEN_LIVE comment exists to keep.
WORD_PROVEN = "RUN GREEN"
WORD_COVERED = "COVERED, NOT RUN"
WORD_BROKERED = "BROKERED ONLY"

#: The short custodial clause, for a surface with a column rather than a paragraph.
#: It is the same claim BROKERED_PATH_NOTE makes at length, and the long one is built
#: from the measurement while this is built from nothing -- so they are two LENGTHS
#: of one fact rather than two statements that could come to disagree.
#:
#: WHY A SHORT FORM EXISTS AT ALL, measured on this repository's own operator page:
#: services/admin_view.pair_rows() put a full explanatory paragraph on every affected
#: pair and /admin rendered that one sentence TWENTY-TWO times -- 1767 of the page's
#: 3156 visible words. One source, so the copies could not disagree, and unreadable
#: anyway. A list surface gets this; a single-swap surface gets the paragraph.
BROKERED_PATH_SHORT = "this terminal settles it CUSTODIALLY, with no hashlock"


def settlement_verdict(from_asset: str, to_asset: str) -> dict:
    """Everything decided about how this directed pair settles. The only derivation.

    THE DECISION, and it is the smallest testable piece (rule 10): two strings in, no
    chain, no database, no config, no clock and no socket. Every surface in the tree
    reads this; none of them re-derives any part of it.

    The keys, and what each is for:

      mode      MODE_SCRIPT_HTLC / MODE_ESCROW_HTLC / MODE_BROKERED_ONLY. The only
                field a caller may BRANCH on.
      driver    the entry point that would settle it atomically, or "" for a
                brokered-only pair. A filename an operator can run, not a path in
                prose: a reader who cannot find the file has been told nothing.
      protocol  how the hashlock is expressed. The reason there are two drivers and
                not one, in four words.
      proven    whether a full swap of THIS DIRECTED PAIR has completed, read off
                PROVEN_LIVE by has_proven_run(). Never True for a brokered-only pair.
      headline  the short form, for a column, a pill or a matrix cell.
      why       the long form, for a single-swap block or an operator page, ending
                in BROKERED_PATH_NOTE.

    WHY ONE FUNCTION RETURNS BOTH LENGTHS. Two functions, each composing its own
    sentence from the same facts, is rule 8's shape at its most tempting -- the two
    agree the day they are written and the first thing to drift is which one says
    "NOT RUN". Here the word, the driver and the cause are chosen once and the two
    lengths are assembled from them, so a surface cannot show a short verdict that
    contradicts the long one beside it. That is not hypothetical in this repository:
    /admin and / disagreed about the same pair in the same process on 2026-10-02
    because two functions evaluated one rule.

    ValueError when the two assets are equal. That is not a swap and no surface here
    produces one -- Config.ALLOWED_PAIRS holds no such pair, services/admin_view's
    matrix skips the diagonal, atomic_swap.py refuses it by name, and open_swap.py's
    parse_pair feeds two different assets -- so a same-asset call is a caller bug and
    is better raised than answered with a mode that would be a lie in all three
    directions.
    """
    source, destination = from_asset.strip().upper(), to_asset.strip().upper()
    if source == destination:
        raise ValueError(
            f"settlement_verdict({from_asset!r}, {to_asset!r}): a swap needs two different assets, and "
            f"{source} -> {source} is not one. Nothing was decided."
        )

    if source in SCRIPT_HTLC_ASSETS and destination in SCRIPT_HTLC_ASSETS:
        # BOTH LEGS ARE A P2SH SCRIPT, so one driver owns both and the protocol is
        # symmetric over the asset set: six directed pairs because three assets.
        #
        # `proven` IS READ OFF PROVEN_SCRIPT_PAIRS AND IS NOT FALSE FOR ALL OF THEM.
        # It was hardcoded False here in the first draft, with a comment claiming that
        # was a measurement; see PROVEN_SCRIPT_PAIRS for what the measurement actually
        # missed and why a grep of atomic_swap.py could not have found it.
        mode, driver = MODE_SCRIPT_HTLC, SCRIPT_DRIVER
        proven = (source, destination) in PROVEN_SCRIPT_PAIRS
        protocol = "P2SH HTLC on both legs"
        covered = (
            f"`{SCRIPT_DRIVER} --from {source} --to {destination}` funds both legs as P2SH HTLCs "
            f"committed to one sha256, the initiator's timelock strictly longer."
        )
        if proven:
            evidence = f"{WORD_PROVEN}: {PROVEN_SCRIPT_PAIRS[(source, destination)]}"
        else:
            evidence = (
                f"No completed {source} -> {destination} swap is recorded in this tree, so it is covered "
                f"code rather than proven code. modules/htlc_assets.PROVEN_SCRIPT_PAIRS is the record "
                f"and it holds "
                f"{', '.join(f'{a} -> {b}' for a, b in sorted(PROVEN_SCRIPT_PAIRS)) or '(none)'}."
            )
        detail = f"{covered} {evidence}"
    elif (
        (source in ESCROW_HTLC_ASSETS and destination in SCRIPT_HTLC_ASSETS)
        or (destination in ESCROW_HTLC_ASSETS and source in SCRIPT_HTLC_ASSETS)
    ):
        # ONE LEG IS AN ESCROW AND THE OTHER IS A SCRIPT, which is a different
        # protocol on each side and therefore a different driver. The --direction flag
        # names the INITIATOR's chain: the leg funded FIRST, taking the longer
        # timelock, whose claim is what publishes the secret for the other side.
        escrow_side = source if source in ESCROW_HTLC_ASSETS else destination
        script_side = destination if source in ESCROW_HTLC_ASSETS else source
        direction = XRP_FIRST if source in ESCROW_HTLC_ASSETS else CHAIN_FIRST
        mode, driver = MODE_ESCROW_HTLC, ESCROW_DRIVER
        protocol = f"{escrow_side} EscrowCreate with a PREIMAGE-SHA-256 condition against a P2SH HTLC"
        proven = has_proven_run(source, destination)
        covered = (
            f"`{ESCROW_DRIVER} --chain {script_side.lower()} --direction {direction}` funds the "
            f"{escrow_side} leg with EscrowCreate under a PREIMAGE-SHA-256 crypto-condition rather than "
            f"a P2SH script, interlocked with the {script_side} HTLC by the same sha256."
        )
        if proven:
            evidence = f"{WORD_PROVEN}: {PROVEN_LIVE[script_side]}"
        else:
            # NAME WHAT THE RECORD DOES HOLD, not just what it lacks. A reader told
            # only "not run" cannot tell an untried direction from an untried chain,
            # and those have different remedies.
            # NO LEADING WORD_COVERED HERE: `why` already opens with the word, and
            # repeating it mid-sentence reads as a second verdict about a second
            # thing. `proven` keeps its prefix because there the word introduces the
            # evidence itself rather than restating the headline.
            evidence = (
                f"No {source} -> {destination} swap has completed on this code path, so a "
                f"failure there would be a discovery rather than a regression. modules/htlc_assets."
                f"PROVEN_LIVE is the record and it holds "
                f"{', '.join(f'XRP -> {chain}' for chain in sorted(PROVEN_LIVE)) or '(none)'}."
            )
        detail = f"{covered} {evidence}"
    else:
        # NEITHER MECHANISM REACHES THIS PAIR. Say which asset is the reason and why,
        # because "brokered only" without a cause is the blank gap rule 14 forbids: a
        # reader cannot tell a chain with no hashlock from a pair nobody has wired yet.
        mode, driver, proven = MODE_BROKERED_ONLY, "", False
        without = [
            asset for asset in (source, destination)
            if asset not in SCRIPT_HTLC_ASSETS and asset not in ESCROW_HTLC_ASSETS
        ]
        reasons = [
            NO_HTLC_REASON.get(
                asset,
                (
                    f"{asset} has no HTLC driver here",
                    f"{asset} has no HTLC driver in this tree -- it is in neither "
                    f"modules/htlc_assets.SCRIPT_HTLC_ASSETS {SCRIPT_HTLC_ASSETS} nor ESCROW_HTLC_ASSETS "
                    f"{ESCROW_HTLC_ASSETS}",
                ),
            )
            for asset in without
        ]
        # THE CAUSE STANDS IN FOR THE PROTOCOL HERE, because there is no protocol to
        # name and "no hashlock on either leg" beside BROKERED_PATH_SHORT's "with no
        # hashlock" said one thing twice and the cause not at all. `(none)` rather
        # than a blank if `without` is ever empty, which would mean this branch was
        # reached by a pair both of whose assets ARE covered -- a contradiction worth
        # printing rather than hiding (rule 14).
        protocol = "; ".join(short for short, _ in reasons) or "(none)"
        detail = (
            f"no atomic path exists for {source} -> {destination} in this tree, and it is a protocol gap "
            f"rather than unfinished wiring: {'; '.join(long for _, long in reasons) or '(none)'}."
        )

    word = WORD_BROKERED if mode == MODE_BROKERED_ONLY else (WORD_PROVEN if proven else WORD_COVERED)
    return {
        "from_asset": source,
        "to_asset": destination,
        "mode": mode,
        "driver": driver,
        "protocol": protocol,
        "proven": proven,
        # THE SHORT FORM NAMES THE DRIVER TOO, because the driver IS the answer to
        # "what do I run instead", and a pill reading only "COVERED, NOT RUN" sends
        # the reader back to the source to find out which file that was.
        "headline": (
            f"{word} -- {driver} ({protocol}); {BROKERED_PATH_SHORT}"
            if driver
            else f"{word} -- {protocol}; {BROKERED_PATH_SHORT}"
        ),
        "why": (
            f"{word} via {driver}: {detail} {BROKERED_PATH_NOTE}"
            if driver
            else f"{word} -- {detail} {BROKERED_PATH_NOTE}"
        ),
    }


def settlement_mode(from_asset: str, to_asset: str) -> tuple[str, str]:
    """`(mode, why)` for this directed pair: the token to branch on and the sentence.

    THE NAMED ENTRY POINT the surfaces were written against, and a thin reader of
    settlement_verdict() rather than a second evaluation of it. A caller that wants
    the short form, the driver alone or `proven` as a boolean calls settlement_verdict()
    directly; nothing recomputes a verdict from the asset sets.

    `why` is never blank and never a bare token. It names WHICH driver covers the
    pair, whether that pair has been RUN GREEN or is merely COVERED, and -- for a
    brokered-only pair -- WHY no atomic path exists, and it always ends with
    BROKERED_PATH_NOTE because the swap in front of the reader is a brokered one
    whatever this verdict says.
    """
    verdict = settlement_verdict(from_asset, to_asset)
    return verdict["mode"], verdict["why"]


def settlement_line(from_asset: str, to_asset: str) -> str:
    """`<mode>  <- <why>`, the one composition a two-column tool prints.

    THE ARROW IS THE THING BEING SPELLED ONCE. open_swap.py, show_swap.py and
    swap_readiness.py all print `value  <- what it means` (rule 14: state what the
    number means next to the number), and three f-strings assembling that from a mode
    and a sentence would be rule 8's smallest and most tempting shape -- the same
    argument swap_terminal/report_block.py makes about a label column. The first thing
    to drift would be the arrow, which is the marker an operator's eye scans for.
    """
    mode, why = settlement_mode(from_asset, to_asset)
    return f"{mode}  <- {why}"
