"""The settlement verdict: which mechanism covers a pair, and that none of them is this one.

Role: test (pure functions and rendered strings; opens no socket, touches no database,
      runs no driver and broadcasts nothing)
Reads: swap_terminal/modules/htlc_assets.py, and the three surfaces that print its
      verdict -- atomic_swap.py's --pairs block, show_swap.py's settlement_block() and
      swap_readiness.py's check_settlement(). Nothing else.
Writes: nothing
Can move funds: no
Mainnet-safe: yes

=============================================================================
THE DEFECT, 2026-10-03
=============================================================================

This tree holds TWO atomic settlement mechanisms and a THIRD, custodial path, and
NOTHING ON ANY SCREEN SAID WHICH ONE A GIVEN PAIR USED. The operator did not know
which applied to a pair they were about to deposit against, argued about it, and was
right to: the question was unanswerable from any output the tree produced.

What the three are, measured rather than recalled:

    atomic_swap.py          P2SH HTLC on BOTH legs. Its CLIENTS dict covered
                            {BTC, GRC, LTC}, ASSETS was derived from it and ASSET_PAIRS
                            from ASSETS -- six directed pairs.
    atomic_swap_xrp.py      XRP against one of those three, where the XRP leg is an
                            EscrowCreate carrying a PREIMAGE-SHA-256 crypto-condition
                            rather than a script. A different protocol on one side,
                            interlocked by the same sha256.
    the Flask terminal      CUSTODIAL. The customer sends to an address this desk owns
                            and the desk pays out of its own inventory. No hashlock, no
                            atomicity. Grepped swap_terminal/services, workers and
                            routes for `atomic`, `htlc` and `HTLC`: three mentions, all
                            of them comments, ZERO imports and zero calls.

So both of these were true at once about GRC -> XRP and only one was visible:

    atomic_swap_xrp.py covers it with a hashlock.
    A swap opened through this terminal is trust-based.

=============================================================================
WHAT EACH TEST BELOW HOLDS, AND THE MUTATION THAT PROVED IT
=============================================================================

Every test here was mutation-checked on 2026-10-03: the smallest change to the
production code that SHOULD break it was made, the test was confirmed to fail, and
the change was reverted. A test that cannot be made to fail is not a test, and this
repository's own standard (CLAUDE.md, "Verify by behavior") asks for the outcome
rather than for the code looking right. Each test names its mutation in its
docstring.

THE THREE MODES ARE ASSERTED AGAINST EACH OTHER rather than one at a time, because
the cheapest way to fake all of this is a function that returns a constant. A test
suite that checked "GRC -> XRP is atomic-xrp-escrow" and nothing else would pass
against `return MODE_ESCROW_HTLC, "..."`, so the distinctness of the three verdicts
is itself an assertion here.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "swap_terminal"))

from modules.htlc_assets import (
    BROKERED_PATH_NOTE,
    CHAIN_FIRST,
    ESCROW_HTLC_ASSETS,
    MODE_BROKERED_ONLY,
    MODE_ESCROW_HTLC,
    MODE_SCRIPT_HTLC,
    MODES,
    NO_HTLC_REASON,
    PROVEN_DIRECTION,
    PROVEN_LIVE,
    PROVEN_SCRIPT_PAIRS,
    SCRIPT_DRIVER,
    SCRIPT_HTLC_ASSETS,
    WORD_BROKERED,
    WORD_COVERED,
    WORD_PROVEN,
    XRP_FIRST,
    custodial_customer_note,
    has_proven_run,
    script_client_classes,
    settlement_line,
    settlement_mode,
    settlement_verdict,
)
from modules.htlc_timelock import SECONDS_PER_BLOCK

import atomic_swap
import atomic_swap_xrp
import show_swap
import swap_readiness

#: The three pairs the operator asked about by name, one per mode. Spelled here so a
#: reader can see at a glance that the three cases are three DIFFERENT shapes rather
#: than three calls with the same answer.
GRC_TO_XRP = ("GRC", "XRP")
BTC_TO_LTC = ("BTC", "LTC")
GRC_TO_SOL = ("GRC", "SOL")


def test_THE_THREE_MODES_ARE_THREE_DIFFERENT_ANSWERS():
    """A constant-returning settlement_mode() must not pass this suite.

    THE POINT OF ASSERTING DISTINCTNESS. Checking one pair's mode in isolation is
    satisfied by `return MODE_ESCROW_HTLC, "..."`, which would be wrong about the
    other two and right about the one the test looked at. So the three modes are
    asserted to differ from each other AND the three sentences are asserted to
    differ, because a function could also return one correct token with one shared
    sentence.

    MUTATION RUN: replaced settlement_verdict()'s body with a single
    `return {... "mode": MODE_ESCROW_HTLC, "why": "x", "headline": "x" ...}`. This
    test failed on the first assertion; the per-pair tests below passed for GRC ->
    XRP, which is exactly the hole this one closes.
    """
    modes = {pair: settlement_mode(*pair)[0] for pair in (GRC_TO_XRP, BTC_TO_LTC, GRC_TO_SOL)}
    assert len(set(modes.values())) == 3, f"three pairs, {len(set(modes.values()))} distinct verdicts: {modes}"
    assert modes[GRC_TO_XRP] == MODE_ESCROW_HTLC
    assert modes[BTC_TO_LTC] == MODE_SCRIPT_HTLC
    assert modes[GRC_TO_SOL] == MODE_BROKERED_ONLY
    assert set(modes.values()) == set(MODES), (
        "MODES must enumerate exactly the tokens settlement_mode() can return; a legend built from it "
        "would otherwise explain a state nobody can see, or miss one a reader is looking at"
    )
    sentences = {pair: settlement_mode(*pair)[1] for pair in (GRC_TO_XRP, BTC_TO_LTC, GRC_TO_SOL)}
    assert len(set(sentences.values())) == 3, "three verdicts sharing one sentence is one verdict"


def test_GRC_to_XRP_names_the_XRP_DRIVER_and_says_it_has_NOT_been_run():
    """The pair the operator argued about, and the one both halves of this change exist for.

    THREE CLAIMS IN ONE SENTENCE, and each is separately load-bearing:

      which driver    atomic_swap_xrp.py, named so a reader can run it. A verdict that
                      says "atomic" without the filename sends somebody to the source.
      which protocol  an escrow under a PREIMAGE-SHA-256 condition, NOT a P2SH script.
                      That is the reason there are two drivers rather than one.
      covered vs run  GRC -> XRP is chain-first, and chain-first has never been run on
                      any chain. The driver's own PROVEN_LIVE comment says so. This
                      must NOT read as RUN GREEN.

    MUTATION RUN: changed has_proven_run() to `return True`. This test failed on the
    WORD_COVERED assertion while test_XRP_to_GRC_is_RUN_GREEN still passed, which is
    the direction that matters -- the dangerous error is claiming evidence that does
    not exist.
    """
    mode, why = settlement_mode(*GRC_TO_XRP)
    assert mode == MODE_ESCROW_HTLC
    assert "atomic_swap_xrp.py" in why, "the sentence must name the driver an operator would run"
    assert "PREIMAGE-SHA-256" in why
    assert CHAIN_FIRST in why, "GRC is the initiator here, so the direction flag is chain-first"
    assert WORD_COVERED in why and WORD_PROVEN not in why, (
        "chain-first has not been run on any chain; a sentence reading RUN GREEN about it would tell an "
        "operator that a failure was a regression on a route nothing has taken"
    )
    assert not settlement_verdict(*GRC_TO_XRP)["proven"]


def test_XRP_to_GRC_is_RUN_GREEN_and_carries_the_real_evidence():
    """The other direction of the same pair, and it IS proven. The distinction is the test.

    COVERED AND RUN ARE DIFFERENT CLAIMS, and the two directions of ONE pair are what
    make that concrete: XRP -> GRC completed OK=15 FAIL=0 on 2026-09-29 with txids,
    and GRC -> XRP has never been run. A verdict keyed on the unordered pair would
    give both the same answer and be wrong about one of them.

    THE EVIDENCE IS THE DRIVER'S OWN SENTENCE, not a paraphrase. PROVEN_LIVE's text is
    carried verbatim, so the txids a reader would check are the ones the run produced.

    MUTATION RUN: swapped settlement_verdict()'s direction test to
    `direction = CHAIN_FIRST if source in ESCROW_HTLC_ASSETS else XRP_FIRST`. THIS test
    still PASSED and test_GRC_to_XRP... failed, which is the measured result rather
    than the one expected: `why` for XRP -> GRC still contains "xrp-first", because the
    PROVEN_LIVE sentence it quotes opens with that word whatever the flag says. So the
    inversion is caught at one end only, by the other direction's test asserting
    `CHAIN_FIRST in why`. Named here because a mutation note that claims a failure
    nobody saw is the thing rule 17 is about.
    """
    mode, why = settlement_mode("XRP", "GRC")
    assert mode == MODE_ESCROW_HTLC
    assert WORD_PROVEN in why
    assert XRP_FIRST in why
    assert "OK=15 FAIL=0" in why, "the evidence is the run's own numbers, not a word meaning 'yes'"
    assert PROVEN_LIVE["GRC"] in why, "carried verbatim from the record, not re-worded"
    assert settlement_verdict("XRP", "GRC")["proven"]


def test_BTC_to_LTC_names_the_SCRIPT_DRIVER_and_claims_no_run():
    """Both legs are a P2SH script, so one driver owns both -- and THIS pair has not run.

    THE PAIR IS CHOSEN RATHER THAN REPRESENTATIVE, and that is the correction this test
    carries. Its first draft was about "script<->script pairs" in general and asserted
    that none had ever completed, on the strength of grepping atomic_swap.py for OK=,
    FAIL= and COMPLETED. The grep was real and found nothing; the claim was false, because
    the evidence lives in docs/branch_coverage.md:156 and records BTC -> GRC completing
    OK=13 FAIL=0 on 2026-09-30 with six txids. So BTC -> LTC is the pair that genuinely
    has not run, and test_BTC_to_GRC_is_RUN_GREEN... below is the other half.

    config.py's own comment calls BTC<->LTC "proven by atomic_swap.py's own BTC<->LTC
    coverage", which is COVERAGE, and this verdict says so in the word it uses.

    MUTATION RUN: changed the script branch's `proven` to a bare True. This test failed
    on `WORD_PROVEN not in why`.
    """
    mode, why = settlement_mode(*BTC_TO_LTC)
    assert mode == MODE_SCRIPT_HTLC
    assert "atomic_swap.py" in why and "atomic_swap_xrp.py" not in why, (
        "a script<->script pair is the OTHER driver's, and naming the XRP one would send a reader to a "
        "file that cannot build either of these legs"
    )
    assert "P2SH" in why
    assert "--from BTC --to LTC" in why, "the sentence should be runnable, not descriptive"
    assert WORD_COVERED in why and WORD_PROVEN not in why
    assert not settlement_verdict(*BTC_TO_LTC)["proven"]


def test_BTC_to_GRC_is_RUN_GREEN_and_GRC_to_BTC_is_not():
    """The script pair that HAS completed, and the fact that its reverse has not.

    THIS TEST IS THE CORRECTION ITSELF. The first draft of modules/htlc_assets.py stated
    that no script<->script swap had ever completed, and stated it as a measurement:
    "grepped atomic_swap.py for OK=, FAIL=, COMPLETED and PROVEN". The grep ran and found
    nothing in that file. docs/branch_coverage.md:156 records BTC -> GRC completing OK=13
    FAIL=0 on 2026-09-30 -- two script chains, both legs P2SH, no XRP and no EscrowFinish
    anywhere, with six txids. The denominator of a grep is the files it searched, which is
    rule 17 at the exact point it is easiest to miss: the measurement was taken and was
    not a measurement of the claim being made.

    GRC -> BTC IS NOT PROVEN, and it is the same five acts in the other order over the
    same two daemons. docs/branch_coverage.md argues that from a measurement rather than
    from principle: the XRP<->LTC run took three attempts, both failures were in code the
    XRP<->GRC run had already exercised, and all fifteen tests covering the broken call
    passed because the test double carried the same wrong signature. "Shares code with
    something that ran" is the claim those attempts refuted.

    MUTATION RUN: emptied PROVEN_SCRIPT_PAIRS to {}. This test failed on the first
    assertion, and test_BTC_to_LTC... still passed -- which is why both exist.
    """
    verdict = settlement_verdict("BTC", "GRC")
    assert verdict["proven"], "BTC -> GRC completed OK=13 FAIL=0 on 2026-09-30, with txids"
    assert verdict["mode"] == MODE_SCRIPT_HTLC
    assert WORD_PROVEN in verdict["why"] and WORD_PROVEN in verdict["headline"]
    assert "OK=13 FAIL=0" in verdict["why"], "the evidence is the run's own numbers"
    assert PROVEN_SCRIPT_PAIRS[("BTC", "GRC")] in verdict["why"], "carried verbatim from the record"

    reverse = settlement_verdict("GRC", "BTC")
    assert not reverse["proven"], "the reverse direction has not been run and sharing code is not evidence"
    assert WORD_COVERED in reverse["why"]
    # ONE DIRECTED PAIR, NOT A PAIR. A table keyed on the unordered pair would give both
    # the same answer and be wrong about one of them, which is the same defect
    # atomic_swap_xrp.py's PROVEN_LIVE comment records for the two XRP directions.
    assert set(PROVEN_SCRIPT_PAIRS) == {("BTC", "GRC")}, (
        "the record of what has RUN. A change here is a claim about what completed on a chain and is "
        "earned by a run, never by a code change"
    )
    for from_asset, to_asset in PROVEN_SCRIPT_PAIRS:
        assert from_asset in SCRIPT_HTLC_ASSETS and to_asset in SCRIPT_HTLC_ASSETS, (
            "a script-pair run must name two script chains; an XRP leg belongs in PROVEN_LIVE"
        )


def test_GRC_to_SOL_is_BROKERED_ONLY_and_cites_the_commit_that_deleted_the_stub():
    """No atomic path exists, and the sentence says WHY rather than only that.

    "BROKERED ONLY" with no cause is the blank gap rule 14 forbids: a reader cannot
    tell a chain with no hashlock from a pair nobody has wired yet, and those have
    different remedies. SOL is the first: atomic_swap.py's --pairs block has said
    since c4ea027 that "the stub that existed could not run and was deleted", and the
    commit hash is the only part of that a reader can act on without asking anybody.

    GRC -> SOL IS A REAL ALLOWED PAIR, not a hypothetical: it is in
    Config.ALLOWED_PAIRS (the three *->SOL directions were enabled 2026-10-03), so
    this is a verdict a customer page renders rather than an edge case.

    MUTATION RUN: emptied NO_HTLC_REASON to {}. This test failed on the c4ea027
    assertion and the generic fallback sentence appeared instead -- which confirmed
    both that the table is what supplies the cause and that the fallback does not
    silently invent one.
    """
    mode, why = settlement_mode(*GRC_TO_SOL)
    assert mode == MODE_BROKERED_ONLY
    assert "SOL has no HTLC" in why
    assert "c4ea027" in why, "the commit that deleted the stub is the evidence; without it this is folklore"
    assert "atomic_swap.py" not in why and "atomic_swap_xrp.py" not in why, (
        "naming a driver here would promise a path that does not exist, which is the exact claim this "
        "verdict exists to refuse"
    )
    assert not settlement_verdict(*GRC_TO_SOL)["proven"]


def test_EVERY_verdict_says_the_BROKERED_path_is_custodial_including_the_atomic_ones():
    """The half of the defect that an atomic verdict alone would hide.

    THIS IS THE DEFECT, STATED AS A TEST. The operator was about to deposit against a
    pair for which an atomic driver exists, through a terminal that does not use it.
    A verdict reading only "ATOMIC via atomic_swap_xrp.py" is TRUE and is how a
    custodial deposit gets made by somebody who believes a hashlock is holding it.

    So the custodial note is on all three modes, and the assertion covers every pair
    in Config.ALLOWED_PAIRS rather than a sample -- a sampled assertion would pass on
    the day a branch stops appending it.

    MUTATION RUN: removed `{BROKERED_PATH_NOTE}` from the escrow branch's `why` only.
    This test failed and named XRP -> GRC, XRP -> LTC, XRP -> BTC and the three
    chain-first directions, while every per-pair test above still passed. That is the
    whole argument for asserting over the real pair list.
    """
    # The deferred-import marker below is a checked claim: Config is imported in the
    # test BODY so
    # this file's module-level import block stays free of application configuration,
    # which conftest.py has to prepare (it reads the environment at class-definition
    # time). The reason sits above the line because ruff's I001 reformats an import
    # block whose first line carries a long trailing comment.
    from config import Config  # noqa: PLC0415

    for from_asset, to_asset in sorted(Config.ALLOWED_PAIRS):
        _mode, why = settlement_mode(from_asset, to_asset)
        assert BROKERED_PATH_NOTE in why, (
            f"{from_asset} -> {to_asset}'s verdict does not say the swap in front of the reader is "
            f"custodial. An atomic driver covering a pair and this terminal using it are different facts"
        )
        assert "CUSTODIAL" in why


def test_the_SCRIPT_ASSET_VOCABULARY_is_DERIVED_from_the_timelock_table():
    """One vocabulary, one derivation (rule 11). Three hand-written copies before this.

    WHAT WAS MEASURED 2026-10-03, by grepping for the client class names: the dict
    {BTC: BTCClient, GRC: GRCClient, LTC: LTCClient} existed at atomic_swap.py:148, at
    atomic_swap_xrp.py:1224 in a different key order with a comment claiming it was
    "imported rather than re-implemented", and at tests/test_htlc_contract_api.py:55.
    Three spellings of one fact. Both drivers and that test now read
    script_client_classes().

    SECONDS_PER_BLOCK is the authority underneath, because a chain belongs in this
    vocabulary only if the tree knows the block interval that turns a timelock policy
    in hours into a height -- and a chain whose interval is unknown cannot have a
    contract built on it at all.

    MUTATION RUN: added `"DOGE": 60` to modules/htlc_timelock.SECONDS_PER_BLOCK. This
    test failed on the SCRIPT_HTLC_ASSETS equality AND on the client-coverage
    assertion, and atomic_swap_xrp's CAN_FUND_THE_HTLC correctly came back WITHOUT
    DOGE -- which is the behavior that keeps that driver's refusal reachable instead
    of pre-empting it with an import error.
    """
    # Written with the derivation on the left because ruff's SIM300 reads the other
    # order as a Yoda condition. The claim is the same equality either way.
    assert tuple(sorted(SECONDS_PER_BLOCK)) == SCRIPT_HTLC_ASSETS
    assert SCRIPT_HTLC_ASSETS == ("BTC", "GRC", "LTC"), (
        "measured 2026-10-03; a change here is a real change to which pairs can be atomic and should be "
        "read rather than re-baselined"
    )
    # BOTH DRIVERS READ THE SAME ONE, which is the property the three copies did not have.
    assert tuple(sorted(atomic_swap.CLIENTS)) == atomic_swap.ASSETS == SCRIPT_HTLC_ASSETS
    assert atomic_swap_xrp.SCRIPT_CHAINS == SCRIPT_HTLC_ASSETS
    assert sorted(atomic_swap_xrp.SCRIPT_CLIENTS) == sorted(atomic_swap.CLIENTS)
    assert atomic_swap.CLIENTS is not atomic_swap_xrp.SCRIPT_CLIENTS, (
        "separate dict objects, so a test monkeypatching one driver's client map does not silently "
        "rewrite the other's"
    )
    # EVERY ASSET IN THE VOCABULARY HAS A CLIENT TODAY, and that is what lets
    # settlement_mode() claim an atomic path for all of them. If this ever fails, the
    # verdict is claiming a chain no driver can fund.
    assert sorted(script_client_classes()) == list(SCRIPT_HTLC_ASSETS)
    assert set(atomic_swap_xrp.CAN_FUND_THE_HTLC) == set(SCRIPT_HTLC_ASSETS)


def test_a_SAME_ASSET_PAIR_is_REFUSED_rather_than_given_a_mode():
    """Two equal assets are not a swap, and no mode would be true of them.

    RAISED RATHER THAN ANSWERED, because all three verdicts would be lies: there is no
    driver, no escrow and no brokered path for GRC -> GRC. Every caller in the tree
    already refuses it -- Config.ALLOWED_PAIRS holds no such pair,
    services/admin_view's matrix skips the diagonal, atomic_swap.py refuses it by
    name, and open_swap.parse_pair() refuses it at the argument since 2026-10-03.

    MUTATION RUN: removed the `if source == destination: raise` guard. This test
    failed, and settlement_mode("GRC", "GRC") then returned MODE_SCRIPT_HTLC with the
    sentence "`atomic_swap.py --from GRC --to GRC` funds both legs as P2SH HTLCs
    committed to one sha256" -- a runnable-looking command that atomic_swap.py itself
    refuses, printed in the register of a measurement. That is worse than an exception
    by exactly the margin rule 17 describes.
    """
    with pytest.raises(ValueError, match="two different assets"):
        settlement_mode("GRC", "GRC")
    with pytest.raises(ValueError):
        settlement_verdict("xrp", " XRP ")


def test_the_pair_is_NORMALIZED_so_case_and_spacing_cannot_change_the_verdict():
    """`grc:xrp` and `GRC:XRP` must settle the same way.

    open_swap.parse_pair() upper-cases, and swap_readiness.parse_pair() does too, but
    a decision that depended on its callers having done so would be a decision with a
    precondition nobody states. The verdict echoes the normalized assets so a reader
    of a row can see what was actually decided about.

    MUTATION RUN: removed `.strip().upper()` from settlement_verdict(). This test
    failed, and lower-case input then fell through to MODE_BROKERED_ONLY claiming
    "grc has no HTLC driver in this tree" about Gridcoin.
    """
    assert settlement_mode("grc", "xrp") == settlement_mode("GRC", "XRP")
    assert settlement_mode("  btc ", "ltc") == settlement_mode("BTC", "LTC")
    verdict = settlement_verdict(" grc ", "xrp")
    assert (verdict["from_asset"], verdict["to_asset"]) == GRC_TO_XRP


def test_an_UNKNOWN_asset_is_brokered_only_and_names_both_vocabularies():
    """A chain nobody has wired must not read like a chain with no hashlock.

    The two causes have different remedies -- one needs an HTLC that cannot exist, the
    other needs a client and a block interval -- so the fallback sentence names the
    two tables a reader would have to add a row to, rather than reusing SOL's wording.

    MUTATION RUN: pointed the fallback at NO_HTLC_REASON["SOL"] instead of building a
    sentence. This test failed on the "neither" assertion, and `DOGE -> GRC` then
    reported that SOL has no HTLC, which is a true sentence about the wrong asset.
    """
    mode, why = settlement_mode("DOGE", "GRC")
    assert mode == MODE_BROKERED_ONLY
    assert "DOGE" in why
    assert "neither" in why and "SCRIPT_HTLC_ASSETS" in why and "ESCROW_HTLC_ASSETS" in why
    assert "c4ea027" not in why, "DOGE's absence has nothing to do with the Solana stub"


def test_PROVEN_LIVE_moved_to_this_module_and_the_driver_still_prints_it():
    """The evidence table has one home, and the driver reads it rather than holding it.

    WHY IT MOVED (rule 10). A module must never import a root entry point, so a
    settlement decision in modules/ could not reach a table in atomic_swap_xrp.py. The
    alternatives were an upward import or a second copy of the evidence, and a second
    copy of EVIDENCE is the worst thing in that file to duplicate: the whole purpose of
    say_what_has_actually_run() is that an operator can tell a regression from a
    discovery, and two copies would eventually disagree about which.

    MUTATION RUN: changed the driver's import to `PROVEN_LIVE = {}` defined locally.
    This test failed on the identity assertion, and
    test_atomic_swap_timelocks.py::test_PROVEN_LIVE... failed too -- so the existing
    suite already guards the shape, and this guards that both files see one object.
    """
    assert atomic_swap_xrp.PROVEN_LIVE is PROVEN_LIVE
    assert set(PROVEN_LIVE) == {"GRC", "LTC"}, (
        "the record of what has RUN. A change here is a claim about what completed on a chain and is "
        "earned by a run, never by a code change"
    )
    assert "PROVEN_LIVE: dict" not in Path(atomic_swap_xrp.__file__).read_text(), (
        "the table must not be redefined in the driver; two definitions of the evidence is how one of "
        "them comes to describe a route nothing took"
    )


def test_PROVEN_DIRECTION_cannot_drift_from_the_sentences_it_describes():
    """The constant that turns a chain-keyed table into DIRECTED pairs.

    PROVEN_LIVE is keyed by SCRIPT CHAIN, so has_proven_run() needs to know which
    direction those runs were in -- and `XRP -> GRC is proven` versus `GRC -> XRP is
    proven` is the difference between a regression and a discovery. The constant is
    held against the sentences so that a chain-first entry cannot be added under a
    constant that still says xrp-first.

    MUTATION RUN: set PROVEN_DIRECTION = CHAIN_FIRST. This test failed on the sentence
    check, AND test_XRP_to_GRC_is_RUN_GREEN began reporting XRP -> GRC as not run
    while GRC -> XRP reported RUN GREEN -- the inversion visible from two directions.
    """
    assert PROVEN_DIRECTION == XRP_FIRST
    for chain, sentence in sorted(PROVEN_LIVE.items()):
        assert sentence.startswith(PROVEN_DIRECTION), (
            f"{chain}'s evidence does not open with {PROVEN_DIRECTION!r}, so PROVEN_DIRECTION no longer "
            f"describes the table and has_proven_run() is mapping the key to the wrong directed pair"
        )
    # AND THE DERIVED DIRECTED PAIRS ARE EXACTLY THOSE, in both directions of the test.
    for chain in SCRIPT_HTLC_ASSETS:
        assert has_proven_run("XRP", chain) is (chain in PROVEN_LIVE)
        assert has_proven_run(chain, "XRP") is False, "chain-first has not been run on any chain"


def test_the_DIRECTION_vocabulary_is_spelled_once():
    """Two strings that must stay equal, in two files, with nothing checking -- until now.

    XRP_FIRST and CHAIN_FIRST were literals in atomic_swap_xrp.py, and the settlement
    decision has to name the same two. Rule 11: one vocabulary, derived in one place.
    LEGACY_CHAIN_FIRST stays in the driver on purpose -- it is a CLI alias argparse
    maps and never carries further, so it is an interface detail of that entry point.

    THE LITERALS ARE PINNED, not only their equality, and the mutation run is why. The
    first draft asserted only `driver.CHAIN_FIRST == CHAIN_FIRST`, which compares one
    object with itself the moment the driver imports it -- so respelling it in
    modules/htlc_assets.py passed this test AND the whole xrp driver suite, because
    everything downstream was consistently wrong together. These two strings are a
    PUBLIC INTERFACE: they are what an operator types after `--direction` and what
    argparse's `choices` accepts, so a rename breaks every recorded command.

    MUTATIONS RUN, both: (a) added `CHAIN_FIRST = "chain-first"` back into the driver,
    shadowing the import -- this test failed on the source check; (b) changed
    CHAIN_FIRST in modules/htlc_assets.py to "chainfirst" -- this test failed on the
    literal check, which the equality check alone could not see.
    """
    assert (XRP_FIRST, CHAIN_FIRST) == ("xrp-first", "chain-first"), (
        "these are the two values an operator types after --direction and that argparse accepts; a rename "
        "silently invalidates every command anybody has written down"
    )
    assert (atomic_swap_xrp.XRP_FIRST, atomic_swap_xrp.CHAIN_FIRST) == (XRP_FIRST, CHAIN_FIRST)
    assert atomic_swap_xrp.DIRECTIONS == (XRP_FIRST, CHAIN_FIRST)
    assert atomic_swap_xrp.LEGACY_CHAIN_FIRST not in (XRP_FIRST, CHAIN_FIRST)
    source = Path(atomic_swap_xrp.__file__).read_text()
    assert 'XRP_FIRST = "' not in source and 'CHAIN_FIRST = "chain-first"' not in source, (
        "the driver has re-spelled a direction it imports; two copies of one vocabulary is rule 11's defect"
    )


def test_the_SHORT_and_LONG_forms_agree_about_the_verdict():
    """A column and a paragraph, built from one set of facts rather than two sentences.

    WHY THIS MATTERS HERE SPECIFICALLY. /admin and / disagreed about the same pair in
    the same process on 2026-10-02 because two functions evaluated one rule. The
    settlement verdict is rendered in two lengths -- a matrix cell and tooltip on
    /admin and swap_readiness.py's column get the headline, open_swap.py and
    show_swap.py get the paragraph -- so the two must not be able to say different
    things.

    MUTATION RUN: hardcoded `"headline": f"{WORD_COVERED} -- ..."` so the short form
    ignored `proven`. This test failed on XRP -> GRC, where the long form said RUN
    GREEN and the short form said COVERED, NOT RUN about one pair.
    """
    for pair in (GRC_TO_XRP, ("XRP", "GRC"), BTC_TO_LTC, GRC_TO_SOL):
        verdict = settlement_verdict(*pair)
        word = WORD_PROVEN if verdict["proven"] else WORD_COVERED
        if verdict["mode"] == MODE_BROKERED_ONLY:
            assert verdict["headline"].startswith("BROKERED ONLY")
            assert verdict["why"].startswith("BROKERED ONLY")
            continue
        assert verdict["headline"].startswith(word), f"{pair}: short form disagrees with `proven`"
        assert verdict["why"].startswith(word), f"{pair}: long form disagrees with `proven`"
        assert verdict["driver"] in verdict["headline"] and verdict["driver"] in verdict["why"]


def test_settlement_line_is_the_mode_then_the_arrow_then_the_sentence():
    """The one composition the two-column tools print, so three of them cannot drift.

    The arrow is what an operator's eye scans for in these blocks, and three f-strings
    assembling `f"{mode}  <- {why}"` would be rule 8's shape at its smallest.

    MUTATION RUN: changed the separator in settlement_line() from "  <- " to " - ".
    This test failed; the open_swap and show_swap surface tests PASSED, which is the
    measured result and not the one expected -- they assert on the words in the
    sentence rather than on the arrow, so the composition has exactly one guard and it
    is this one. Recorded as measured rather than as assumed (rule 17).
    """
    mode, why = settlement_mode(*GRC_TO_XRP)
    assert settlement_line(*GRC_TO_XRP) == f"{mode}  <- {why}"


def test_an_unproven_script_pair_names_the_DRIVER_it_has_no_run_for():
    """MUTATION: put back "No completed BTC -> LTC swap is recorded in this tree".

    That sentence was true when it was written and became false on 2026-10-05, when
    s_b3ff505b6cac5c15 completed BTC -> LTC through the terminal -- 0.001 BTC in at 2
    confirmations, 1.19550983 LTC out, 18.1s open to `completed`. A completed BTC ->
    LTC swap IS recorded in this tree, in the `swaps` table, and show_swap.py printed
    that sentence directly over the finished row.

    THE VERDICT IS STILL `covered`, AND THIS TEST PINS BOTH HALVES. The custodial
    path shares no code with the driver -- nothing in services, workers or routes
    imports either one -- so a swap that ran there is not evidence about this one.
    Recording it as evidence would be the COVERED-IS-NOT-RUN conflation this module
    exists to prevent, arriving from the other direction: a swap that RAN is not
    evidence for a driver it never entered. What was wrong was the DENOMINATOR of the
    sentence, which is the identical defect PROVEN_SCRIPT_PAIRS already records about
    its own BTC -> GRC row (rule 17: the denominator of a grep is the files it was
    pointed at).
    """
    pair = ("BTC", "LTC")
    assert pair not in PROVEN_SCRIPT_PAIRS, (
        "BTC -> LTC completed CUSTODIALLY on 2026-10-05, not through the driver. If this "
        "entry exists, a custodial run was recorded as atomic evidence."
    )
    verdict = settlement_verdict(*pair)
    assert verdict["proven"] is False
    assert verdict["mode"] == MODE_SCRIPT_HTLC

    why = verdict["why"]
    assert SCRIPT_DRIVER in why, "the sentence must say WHICH record has no run"
    assert "is recorded in this tree" not in why, (
        "a claim about every record in the tree; the measurement behind it only covered "
        "the driver's runs, and a completed custodial swap of this pair now exists"
    )
    assert "CUSTODIAL" in why, "a reader must be told why the custodial completion is not evidence"

    # AND THE PROVEN PAIR IS UNTOUCHED: this narrows a sentence, not a verdict.
    assert settlement_verdict("BTC", "GRC")["proven"] is True


def test_the_ESCROW_ASSET_is_not_also_a_script_asset():
    """The two vocabularies must not overlap, or one pair would match two branches.

    XRP has no script and the three script chains have no escrow; an asset in both
    sets would make the script<->script branch swallow a pair that needs the escrow
    driver, which is a verdict naming the wrong driver at the moment somebody is
    deciding what to run.

    MUTATION RUN: added "GRC" to ESCROW_HTLC_ASSETS. This test failed, and GRC -> XRP
    then printed "`atomic_swap_xrp.py --chain xrp --direction xrp-first` funds the GRC
    leg with EscrowCreate" -- the two sides swapped, because the branch takes the first
    asset it finds in the escrow set as the escrow side. A runnable-looking command
    naming XRP as the script chain, which has no script at all.
    """
    assert not set(ESCROW_HTLC_ASSETS) & set(SCRIPT_HTLC_ASSETS)
    assert ESCROW_HTLC_ASSETS == ("XRP",), "measured 2026-10-03; one escrow-style chain"
    assert not set(NO_HTLC_REASON) & (set(ESCROW_HTLC_ASSETS) | set(SCRIPT_HTLC_ASSETS)), (
        "an asset cannot both have an HTLC driver and be listed as having none"
    )


# =============================================================================
# THE SURFACES. Each of these runs the real function and reads the real output.
# =============================================================================


def test_atomic_swap_PAIRS_output_is_derived_and_no_longer_claims_XRP_IS_REFUSED(capsys):
    """--pairs told the operator a working pair was refused. It cannot say that again.

    THE WRONG COMMENT, QUOTED. Until 2026-10-03 print_pairs() said of the XRP driver:
    "Its --chain flag offers btc and ltc but REFUSES them: it funds with Gridcoin's
    createhtlc, and the fix is to route it through the clients above." Both runners
    moved onto those clients on 2026-09-29, XRP<->LTC completed OK=15 FAIL=0 the same
    day, and CAN_FUND_THE_HTLC has held all three chains since. So the one screen whose
    job is to answer "which pairs can I run" named a working pair as refused.

    MUTATION RUN: put the literal "REFUSES them: it funds with Gridcoin's createhtlc"
    back into print_pairs(). This test failed on the `createhtlc` assertion.
    """
    # Deferred-import marker below, checked: Console is a display dependency of the
    # surface under test,
    # imported here so this file's module-level imports stay to the decision itself.
    from step_console import Console  # noqa: PLC0415

    console = Console(total_steps=1)
    assert atomic_swap.print_pairs(console) == 0
    printed = capsys.readouterr().out
    assert "createhtlc" not in printed, "the refusal that no longer happens must not be described as if it does"
    assert "REFUSES" not in printed
    # EVERY DIRECTED XRP PAIR, with its evidence, because the two directions differ.
    assert "XRP -> GRC   atomic_swap_xrp.py   RUN GREEN" in printed
    assert "GRC -> XRP   atomic_swap_xrp.py   covered, NOT RUN" in printed
    assert "XRP -> BTC   atomic_swap_xrp.py   covered, NOT RUN" in printed
    assert "SOL has no HTLC (c4ea027)" in printed
    # THE SCRIPT PAIRS CARRY THEIR EVIDENCE TOO, added when the BTC -> GRC run was found:
    # six bare `--from X --to Y` lines say what the flags are and nothing about which of
    # them has ever completed.
    assert "--from BTC --to GRC   RUN GREEN" in printed
    assert "--from BTC --to LTC   covered, NOT RUN" in printed


def test_show_swap_prints_the_settlement_for_a_swaps_own_pair():
    """The block an operator reads about an EXISTING swap says how it settles.

    On an existing swap the question is live rather than hypothetical: the deposit may
    already be sitting in a desk-owned address, and "is anything holding this but
    trust?" is what a reader of that block is asking.

    THIS TESTS THE FUNCTION; THE CALL SITE IS TESTED WHERE ITS FIXTURES ARE. Removing
    `*settlement_block(...)` from swap_lines() would NOT break this test, so claiming it
    would be the kind of unverified mutation note this file exists to avoid -- that
    mutation is caught by
    tests/test_show_swap.py::test_the_SETTLEMENT_verdict_is_on_the_block_for_an_existing_swap,
    which drives the real tool against a seeded database.

    MUTATION RUN: made settlement_block() return `[labeled("settlement", "")]`. This
    test failed on the `atomic_swap_xrp.py` assertion, which is the half a shorter
    verdict would quietly drop.
    """
    lines = show_swap.settlement_block("GRC", "XRP")
    assert lines, "a swap with a known pair must print a settlement verdict"
    assert "settlement" in lines[0]
    joined = " ".join(line.strip() for line in lines)
    assert "atomic_swap_xrp.py" in joined and "CUSTODIAL" in joined
    assert all(line.startswith("  ") for line in lines), "every line sits in the value column"


def test_show_swap_says_NOT_ESTABLISHED_rather_than_crashing_on_an_unreadable_pair():
    """A malformed swaps row must not take down the block that exists to diagnose it.

    swap_lines() reads `swap.get("from_asset") or "?"`, so a row written by an older
    schema arrives as "?" on both sides -- and settlement_mode("?", "?") raises
    ValueError by design, because two equal assets are not a swap. A terminal block
    that crashes on a bad row is worse than one that says it cannot tell: the operator
    is reading it precisely BECAUSE something is wrong.

    MUTATION RUN: replaced settlement_block()'s guard with a bare
    `return wrapped("settlement", settlement_line(from_asset, to_asset))`. This test
    failed with the ValueError; the WHOLE of tests/test_show_swap.py still passed,
    because every swap it seeds has two readable assets. That is the measured reason
    this test exists as a unit rather than being left to the end-to-end suite -- the
    row shape it guards is the one no fixture produces.
    """
    lines = show_swap.settlement_block("?", "?")
    assert lines and "NOT ESTABLISHED" in lines[0]
    joined = " ".join(line.strip() for line in lines)
    assert "could not be read" in joined and "from_asset" in joined and "to_asset" in joined
    # AND THE OTHER SHAPE: one side readable, the other not.
    assert "NOT ESTABLISHED" in show_swap.settlement_block("GRC", "?")[0]
    # AND A ROW WHOSE TWO SIDES AGREE, which is a different cause and says so.
    assert "same asset" in " ".join(show_swap.settlement_block("GRC", "GRC"))


def test_swap_readiness_reports_settlement_per_pair_and_once_in_summary():
    """The preflight that says READY now says what READY settles as.

    Every pair this file reports READY is a pair that will settle custodially, and no
    line it printed said so. The per-pair lines carry the SHORT form because this file
    prints a column; the long form is the two root tools'.

    NOT A FAIL CONDITION, asserted: a brokered pair is not a broken pair, so a FAIL
    here would be cried-wolf noise and a SKIP would read as "not checked".

    MUTATION RUN: changed check_settlement() to record(FAIL, ...). This test failed on
    the state assertion, and `swap_readiness.py --pair GRC:XRP` then exited 1 on a
    terminal with nothing wrong with it -- a preflight that fails by design is a
    preflight an operator stops reading.
    """
    swap_readiness._results.clear()
    swap_readiness.check_settlement(GRC_TO_XRP)
    try:
        recorded = list(swap_readiness._results)
    finally:
        swap_readiness._results.clear()
    assert recorded, "(none) is a result, but there is one pair in scope here"
    assert all(state == swap_readiness.PASS for state, _name, _detail in recorded), (
        "settlement is a statement of fact about a working terminal, not a precondition that failed"
    )
    summary = recorded[0][2]
    assert "CUSTODIAL" in summary and "no hashlock" in summary
    assert "0 of which this terminal settles atomically" in summary
    per_pair = [(name, detail) for _state, name, detail in recorded if name == "GRC->XRP settles"]
    assert len(per_pair) == 1, f"one line per directed pair; got {[n for _s, n, _d in recorded]}"
    assert "atomic_swap_xrp.py" in per_pair[0][1]
    assert WORD_COVERED in per_pair[0][1]
    # THE COLUMN IS 28 WIDE in this file's record(); a name past it ragged-edges every
    # line below it, which is the whole reason report_block.LABEL_WIDTH has a test.
    for _state, name, _detail in recorded:
        assert len(name) <= 28, f"{name!r} overflows swap_readiness.record()'s name column"


def test_the_web_pair_rows_carry_the_same_verdict_as_the_terminal():
    """/admin, the customer offer list and the two root tools read ONE decision.

    services/admin_view.pair_rows() and services/pair_view.allowed_pair_rows() both
    carry it, and neither re-derives it. That is the arrangement the 2026-10-02 defect
    was about: two functions evaluating one rule had /admin and / disagreeing about the
    same pair in the same process.

    MUTATION RUN: changed admin_view.pair_rows() to set
    `"settlement_headline": "ATOMIC"` directly. This test failed on the equality
    against settlement_verdict(), while every existing admin_view test passed.
    """
    # The three deferred-import markers below are the same checked claim as the other
    # test-body
    # import: conftest.py prepares the environment Config reads at class-definition
    # time, and the two services pull in the Flask-side import graph that this file's
    # decision-level tests do not need.
    from config import Config  # noqa: PLC0415
    from services.admin_view import pair_rows  # noqa: PLC0415
    from services.pair_view import allowed_pair_rows  # noqa: PLC0415

    config = {"ALLOWED_PAIRS": Config.ALLOWED_PAIRS, "RPC": Config.RPC}
    for row in pair_rows(config, {}):
        expected = settlement_verdict(row["from_asset"], row["to_asset"])
        assert row["settlement"] == expected, f"{row['label']}: /admin re-derived the verdict"
        assert row["settlement_headline"] == expected["headline"]
    customer = allowed_pair_rows(config, {})
    assert customer, "(none) would mean ALLOWED_PAIRS is empty, which it is not"
    for row in customer:
        assert row["settlement"] == settlement_verdict(row["from_asset"], row["to_asset"])


def test_the_admin_page_states_once_that_every_pair_is_custodial():
    """The page-level sentence, and it is once per page rather than once per cell.

    MEASURED REASON FOR "ONCE PER PAGE": this panel's per-pair `detail` once rendered
    the same remedy sentence twenty-two times, 1767 of the page's 3156 visible words.
    A settlement note on every cell would repeat that with a different sentence, so the
    part that does not vary is said once and the part that does rides on each cell's
    label and tooltip.

    JINJA COMMENTS ARE STRIPPED BEFORE COUNTING, and the first draft of this test did
    not do that and was wrong because of it. `{#- ... -#}` blocks never reach a browser,
    and this template's explanatory comments are long enough to contain the same phrases
    the markup does -- the first run counted 2 where a reader sees 1, because the
    comment above the matrix cell explains why the note is said once. A gate that counts
    source text is not measuring what anybody reads.

    MUTATION RUN: deleted the panel note from templates/admin.html. This test failed;
    tests/test_web_surfaces.py and tests/test_admin_view.py both still passed.
    """
    template = Path(__file__).resolve().parent.parent / "swap_terminal" / "templates" / "admin.html"
    markup = re.sub(r"\{#.*?#\}", "", template.read_text(), flags=re.DOTALL)
    assert markup.count("settles custodially") == 1, (
        "said once, not per cell -- and not zero, which is the state this change exists to end"
    )
    assert "cell.settlement_headline" in markup, "the per-pair half must reach the cell"
    assert re.search(r"aria-label=.*settlement_headline", markup, re.IGNORECASE), (
        "a tooltip alone is invisible to a keyboard user and to a screen reader"
    )


def test_the_customer_sentence_does_not_vary_with_the_settlement_mode():
    """The one key on the verdict that must NOT follow `mode`, `driver` or `proven`.

    THIS IS THE SUBTLE HALF OF THE 2026-10-09 CHANGE and the one a later reader is
    most likely to "fix" in the wrong direction, so it is pinned rather than only
    commented. `mode` says which mechanism COULD settle a pair. It does not say how
    a swap opened through this terminal IS settled, which is custodially, always --
    BROKERED_PATH_NOTE records the grep (nothing in services/, workers/ or routes/
    imports either driver) and routes/atm.py's commit path contains no HTLC,
    hashlock or preimage.

    So a customer sentence keyed on `mode` would begin claiming atomicity the day
    somebody marked a pair script-HTLC-capable while the ATM still settled it
    custodially -- on the screen where a customer decides whether to send money.

    ASSERTED ACROSS ALL THREE MODES AT ONCE, over pairs whose verdicts genuinely
    differ, so the test cannot pass by accident on a tree where every pair happens
    to share a mode.
    """
    pairs = (GRC_TO_XRP, BTC_TO_LTC, GRC_TO_SOL)
    verdicts = {pair: settlement_verdict(*pair) for pair in pairs}
    assert len({verdict["mode"] for verdict in verdicts.values()}) > 1, (
        "every pair here shares a mode, so mode-independence is untested"
    )

    for pair, verdict in verdicts.items():
        assert verdict["customer"] == custodial_customer_note(*pair), (
            f"{pair}'s customer sentence is not the one custodial_customer_note() derives"
        )
        # The sentence differs between pairs ONLY in the two asset names. Strip
        # those and every pair must read identically, whatever its mode.
        skeleton = verdict["customer"].replace(pair[0], "<FROM>").replace(pair[1], "<TO>")
        assert skeleton == (
            custodial_customer_note("<FROM>", "<TO>")
        ), f"{pair} ({verdict['mode']}) says something its mode decided"


def test_the_customer_sentence_names_no_mechanism_no_file_and_no_posture_word():
    """What may not reach a customer, over the module's own vocabulary.

    DERIVED FROM THE MODULE RATHER THAN TYPED, so a fourth posture word or a third
    driver cannot arrive and go unchecked -- the identical argument
    settlement_verdict()'s own `headline` gets right for the operator surfaces.

    THE HEADLINE IS ASSERTED TO STILL CONTAIN THEM, which is the half that stops
    this being a request to strip the vocabulary everywhere. An operator reading a
    matrix cell needs the driver filename; that is what the cell is for.
    """
    banned = {WORD_BROKERED, WORD_COVERED, WORD_PROVEN, "P2SH", "HTLC", "hashlock",
              "atomic_swap.py", "atomic_swap_xrp.py", "CUSTODIALLY"}
    for pair in (GRC_TO_XRP, BTC_TO_LTC, GRC_TO_SOL):
        note = custodial_customer_note(*pair)
        for word in banned:
            assert word not in note, f"{pair}'s customer sentence names {word!r}"
        assert not re.search(r"\((?:[0-9a-f]{7,40})\)", note), f"{pair}'s customer sentence carries a git sha"

    # The operator form keeps every one of them, or the two lengths have collapsed
    # into one and the operator surfaces have quietly lost the driver.
    operator = settlement_verdict("BTC", "GRC")["headline"]
    assert "atomic_swap.py" in operator and "CUSTODIALLY" in operator, (
        "the operator pill lost the driver or the custody clause; this change was not supposed to touch it"
    )
