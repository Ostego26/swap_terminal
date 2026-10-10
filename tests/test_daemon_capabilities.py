"""The GRC / LTC / BTC capability map and equivalence tree.

Operator instruction, 2026-10-09:

    don't forget grc is 2014 bitcoin and bitcoin is way more advanced now

    we probably should've pre-emptively mapped the grc and modern ltc and btc
    capabilities and differences to avoid compatibility and integration
    confusion and cross chain incommunicado.

    yeah a tree of equivalence betwen rpc comamnds for ltc, btc, and grc sounds
    bout right

These pin the shape of that map, the three-valued answer rule 17 requires, and
the one divergence nothing absorbs yet.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from chains.coin_amounts import CHAIN_DECIMALS
from chains.daemon_capabilities import (
    _REFUSAL_REMEDIES,
    BITCOIN_FAMILY,
    CAPABILITIES,
    EQUIVALENTS,
    MEASURED,
    NOT_THIS_DEPLOYMENT,
    RELEASE_HISTORY,
    UPSTREAM_SOURCE,
    _capability,
    absence_note,
    calls_for,
    differences_for,
    has,
    jobs_that_diverge,
    refusal_remedy,
    refusal_shape,
    serves_wallet_path,
    unresolved_divergences,
    unverified_on_this_deployment,
    wallet_path_warning,
)
from conftest import root_entry_point
from modules.atomic_swapper import SUPPORTED_ASSETS

_WALLET_PATH = "multi-wallet HTTP endpoint (/wallet/<name>)"


def test_the_family_tuple_is_derived_and_not_a_sixth_spelling():
    """IT WAS A SIXTH SPELLING, written in the commit meant to stop duplication.

    `BITCOIN_FAMILY = ("BTC", "LTC", "GRC")` was the first version of that line.
    config.py:308 and workers/common.py:108 both already record that this tuple is
    spelled in five places, and config.py:1339 says there is deliberately no
    tuple in that file "-- rule 8 counts copies". I added a sixth anyway, in
    35edf70, whose whole subject was consolidating scattered chain knowledge, and
    found it only by going looking afterwards.

    It now derives from coin_amounts.CHAIN_DECIMALS, whose own docstring explains
    that XRP and SOL are absent because they convert to integer base units before
    sending -- a property of not being a Bitcoin-family daemon, decided at the one
    place that had to decide it. So the sets coincide for the same reason rather
    than by luck.

    ASSERTED ON THE SOURCE AS WELL AS THE VALUE, and the value half alone is NOT
    enough -- measured. Restoring the literal `("BTC", "LTC", "GRC")` passed the
    value comparison clean, because a hand-written copy that AGREES is exactly
    what a value comparison cannot distinguish from a derivation. That is the same
    shape as the vacuous screen test caught earlier the same day: the assertion
    was true for a reason unrelated to what it claimed to check.

    So the source assertion is the one that bites, and it is justified the way
    test_up_actually_asks_before_it_prints_SERVING is: the claim is "this value is
    DERIVED", which has no behavior to exercise, because a correct derivation and
    a correct copy evaluate identically until the day they do not.
    """
    source = (Path(__file__).resolve().parents[1]
              / "swap_terminal/chains/daemon_capabilities.py").read_text()
    assignment = next(
        line for line in source.splitlines() if line.startswith("BITCOIN_FAMILY")
    )
    assert "CHAIN_DECIMALS" in assignment, (
        f"BITCOIN_FAMILY is not derived; it is assigned: {assignment.strip()}"
    )
    assert '"BTC"' not in assignment, (
        f"BITCOIN_FAMILY spells the chains out, which makes it another copy of a tuple this "
        f"tree already spells five times: {assignment.strip()}"
    )

    assert tuple(CHAIN_DECIMALS) == BITCOIN_FAMILY, (
        f"BITCOIN_FAMILY {BITCOIN_FAMILY} is not derived from CHAIN_DECIMALS "
        f"{tuple(CHAIN_DECIMALS)} -- one of them is a hand-written copy"
    )
    # ORDER, NOT JUST MEMBERSHIP. wallet_custody.py:805 does
    # `enumerate(SCRIPT_CHAINS, start=1)` for a numbered display, and
    # icp_custody_addresses.py loops twice, so a reorder would silently renumber
    # an operator-facing report.
    assert BITCOIN_FAMILY == ("BTC", "LTC", "GRC"), (
        f"the order changed to {BITCOIN_FAMILY}; a numbered report elsewhere follows it"
    )


def test_the_five_sibling_tuples_agree_today_and_each_names_the_others():
    """Rule 8's HARDER half: they genuinely differ, so they are not merged.

    Five names, one membership, five different concepts -- wallet custody, swapper
    support, fee measurability, P2PKH addresses, and having a Bitcoin Core release
    to compare against. They CAN diverge: a bech32-only Bitcoin fork belongs in
    SCRIPT_CHAINS and not in _P2PKH_CHAINS. Collapsing them would assert an
    identity nobody has established, which is why rule 8 asks instead for "a
    comment at BOTH sites, naming the other one".

    What was missing is exactly that: none of the five named the others, so the
    coincidence read as a copy somebody forgot to merge. This asserts the
    agreement TODAY, which makes a future divergence a deliberate edit to a
    failing test rather than a silent drift nobody sees.
    """
    # conftest.root_entry_point(), NOT a sixth copy of the loader. I started to
    # write one here -- in the test for a commit about consolidating duplicated
    # knowledge -- which is the same reflex that made BITCOIN_FAMILY a sixth
    # spelling one commit earlier. OPEN_FINDINGS has asked for this helper since
    # the five copies were counted.
    siblings = {
        "chains/daemon_capabilities.BITCOIN_FAMILY": BITCOIN_FAMILY,
        "modules/atomic_swapper.SUPPORTED_ASSETS": SUPPORTED_ASSETS,
        "wallet_custody.SCRIPT_CHAINS": root_entry_point("wallet_custody.py").SCRIPT_CHAINS,
        "show_payout_fees.MEASURABLE": root_entry_point("show_payout_fees.py").MEASURABLE,
        "icp_custody_addresses._P2PKH_CHAINS": root_entry_point("icp_custody_addresses.py")._P2PKH_CHAINS,
    }
    for name, value in siblings.items():
        assert tuple(value) == BITCOIN_FAMILY, (
            f"{name} is {tuple(value)} and BITCOIN_FAMILY is {BITCOIN_FAMILY}. If that "
            f"divergence is INTENDED, say so in both comments and change this test -- the "
            f"point is that it cannot happen quietly"
        )

    # AND EACH ONE NAMES THE OTHERS, which is the half a value comparison cannot
    # check. A reader who finds one of these must be told the other four exist.
    repo = Path(__file__).resolve().parents[1]
    sources = {
        "wallet_custody.py": repo / "wallet_custody.py",
        "show_payout_fees.py": repo / "show_payout_fees.py",
        "icp_custody_addresses.py": repo / "icp_custody_addresses.py",
        "atomic_swapper.py": repo / "swap_terminal/modules/atomic_swapper.py",
        "daemon_capabilities.py": repo / "swap_terminal/chains/daemon_capabilities.py",
    }
    for label, path in sources.items():
        text = path.read_text()
        assert "BITCOIN_FAMILY" in text, f"{label} does not name BITCOIN_FAMILY"
        assert "_P2PKH_CHAINS" in text, f"{label} does not name _P2PKH_CHAINS"
        assert "SCRIPT_CHAINS" in text, f"{label} does not name SCRIPT_CHAINS"


def test_a_chain_nobody_checked_answers_None_and_not_False():
    """RULE 17 MADE STRUCTURAL, and it is the whole reason has() is three-valued.

    "not recorded" is not "absent". A two-valued answer would turn every gap in
    the table into a measurement nobody took, which is the exact failure the
    operator named: presenting a hypothesis in the register of a measurement.

    MUTATION: `return asset in capability.present_on`. Every unrecorded pair
    then reads as a confident False, and a caller writing `if not has(...)`
    cannot tell the two apart.
    """
    # rpcbind is recorded present on BTC/LTC with an EMPTY absent_on, because
    # whether gridcoinresearchd accepts the option was never tested here.
    assert has("GRC", "rpcbind") is None, (
        "whether GRC accepts rpcbind is unverified, and None is the only honest answer"
    )
    assert has("BTC", "rpcbind") is True
    said = absence_note("GRC", "rpcbind")
    assert "NOT RECORDED" in said and "nobody checked" in said, said
    assert "says nothing either way" in said, said


def test_every_row_says_whether_it_was_measured_or_read_off_release_notes():
    """The evidence field is not decoration; a row without it is a row in two voices.

    WIDENED 2026-10-09 when UPSTREAM_SOURCE arrived, and the STRONGER invariant is
    the point rather than the third name: the list it checks against is now derived
    from the two filters instead of spelled here, so a FOURTH kind added without
    being classified as measured-here-or-not fails this test. The old literal pair
    would have had to be edited by hand on every addition, which is a hand-maintained
    copy of a vocabulary -- rule 8's failure with a delay on it.
    """
    classified = {MEASURED, *NOT_THIS_DEPLOYMENT}
    for capability in CAPABILITIES:
        assert capability.evidence in classified, (
            f"{capability.name} carries evidence {capability.evidence!r}, which no filter in "
            f"the module classifies -- it is in neither MEASURED nor NOT_THIS_DEPLOYMENT, so "
            f"unverified_on_this_deployment() silently omits it"
        )
        assert capability.recorded_at.strip(), f"{capability.name} does not say where it came from"


def test_the_unverified_rows_are_listed_and_are_the_ones_i_could_not_test():
    """Rule 17: say which you have, and make the list callable rather than prose.

    These are rows not established against the operator's OWN daemons -- there is no
    GRC daemon in this session to ask. If one of them ever gets measured there, it
    moves to MEASURED and drops off this list.

    NO LONGER "these three", AND NOT A COUNT AT ALL. It said three and there are
    four, because 2026-10-09 added a row for Gridcoin's split config files. A test
    that asserts a count has to be edited every time the map grows, and the edit is
    the kind nobody thinks about -- so this asserts the PROPERTY every member must
    have, plus the specific rows that must not quietly become measured.
    """
    unverified = {c.name for c in unverified_on_this_deployment()}
    assert _WALLET_PATH in unverified, "the wallet-path row was never tested against a GRC daemon"
    assert "rpcbind" in unverified
    assert any("rpcallowip" in name for name in unverified)
    for capability in unverified_on_this_deployment():
        assert capability.evidence in NOT_THIS_DEPLOYMENT, (
            f"{capability.name} is listed as unverified-here but its evidence "
            f"{capability.evidence!r} is not one of the not-this-deployment kinds"
        )
        assert capability.evidence != MEASURED


def test_asking_about_a_non_bitcoin_chain_raises():
    """XRP, SOL and ICP have no Core release to compare against.

    Raising rather than returning None, because a caller asking about XRP here
    has a bug and a None would be read as "XRP lacks it" -- the same shape as
    the `except Exception: return None` in resolve_underlying_family() that cost
    a whole investigation (CLAUDE.md rule 12).
    """
    for outsider in ("XRP", "SOL", "ICP"):
        with pytest.raises(KeyError, match="not Bitcoin-derived"):
            has(outsider, "gettxout")
        with pytest.raises(KeyError, match="not Bitcoin-derived"):
            calls_for(outsider, "which network is this daemon on")


def test_an_unrecorded_capability_or_job_raises_rather_than_reading_as_absent():
    """A typo must not read as a capability gap."""
    with pytest.raises(KeyError, match="no capability recorded"):
        has("GRC", "gettxoutt")
    with pytest.raises(KeyError, match="no equivalence recorded"):
        calls_for("GRC", "whatever it is I meant")


def test_the_absence_sentence_says_it_was_never_there():
    """Rule 14: "GRC has no gettxout" invites somebody to go and install something.

    It was never there, and the sentence has to say so, name the release, and
    name what to do instead -- otherwise the operator's next move is a bad one.
    """
    said = absence_note("GRC", "gettxout")
    assert "never there" in said, said
    assert "getrawtransaction" in said, f"the sentence must name the route that works: {said}"
    assert MEASURED in said, f"and whether this was measured: {said}"


def test_the_equivalence_tree_covers_all_three_chains_per_divergent_job():
    """A row that names two chains leaves the third's caller guessing."""
    for row in EQUIVALENTS:
        named = set(row.calls)
        assert named <= set(BITCOIN_FAMILY), f"{row.job} names a non-family chain: {named}"
        assert named == set(BITCOIN_FAMILY), (
            f"{row.job!r} does not say what to call on {set(BITCOIN_FAMILY) - named}, which is "
            f"the cross-chain gap this tree exists to close"
        )


def test_every_recorded_job_actually_diverges():
    """A row where all three are called identically did not need to exist.

    DERIVED FROM THE MAPPING rather than trusting the author: jobs_that_diverge()
    filters on the calls, so a row added with three identical entries is caught
    here instead of padding the table.
    """
    assert set(jobs_that_diverge()) == {row.job for row in EQUIVALENTS}, (
        "a row in EQUIVALENTS is called identically on all three chains, so it is not a "
        "divergence and belongs in CAPABILITIES or nowhere"
    )


def test_the_wallet_path_is_the_one_divergence_nothing_absorbs():
    """The interesting half of the table, and how it was found.

    Six of the seven jobs have a resolver -- a function that already handles the
    difference. The wallet path does not: two separate URL builders append
    /wallet/<name> for any chain, and the rule neither of them has is that the
    endpoint is Core 0.17+.
    """
    unresolved = [row.job for row in unresolved_divergences()]
    assert unresolved == ["reach a named wallet"], unresolved
    for row in EQUIVALENTS:
        if row.job not in unresolved:
            assert row.resolver.strip(), f"{row.job} claims to be resolved and names nothing"


def test_grc_is_refused_a_wallet_path_and_the_modern_chains_are_not():
    """The fact the warning rests on."""
    assert serves_wallet_path("GRC") is False
    assert serves_wallet_path("BTC") is True
    assert serves_wallet_path("LTC") is True


def test_the_warning_fires_only_on_a_configuration_that_cannot_work():
    """A warning that fires on a working config is one the reader learns to skip.

    Rule 13's own words, and the reason this is three assertions rather than one:
    quiet when unset, quiet on a chain that HAS the endpoint, loud only for the
    combination that is broken.
    """
    assert wallet_path_warning("GRC", "") == "", "unset must be silent"
    assert wallet_path_warning("GRC", "   ") == "", "whitespace is unset"
    assert wallet_path_warning("BTC", "desk_hot") == "", "BTC serves /wallet/<name>; nothing to say"
    loud = wallet_path_warning("GRC", "desk_hot")
    assert "desk_hot" in loud, f"the warning must name the value that caused it: {loud}"
    assert "/wallet/desk_hot" in loud, f"and the URL it will build: {loud}"
    assert "never there" in loud, f"and that it is not a daemon to go and fix: {loud}"


def test_the_per_chain_difference_list_is_derived_not_written_twice():
    """Rule 11's shape: a capability added to the table appears here with no edit.

    MUTATION: hand-write differences_for()'s GRC list. It then agrees on the day
    it is written and drifts from the table from then on, which is rule 8 exactly.
    """
    grc = differences_for("GRC")
    assert grc, "GRC differs from its siblings in several recorded ways and the list is empty"
    # Derived means: every entry traces to a row where GRC is on one side and a
    # sibling on the other. A hand-written list could say anything.
    both_sided = [c for c in CAPABILITIES if c.present_on and c.absent_on]
    expected = [c for c in both_sided if "GRC" in c.present_on + c.absent_on]
    assert len(grc) == len(expected), (
        f"differences_for('GRC') returned {len(grc)} sentences for {len(expected)} two-sided rows"
    )
    # And it reports BOTH directions -- the `account` field and getinfo are
    # things GRC HAS and the modern chains do not, which a map written as
    # "what GRC is missing" would silently drop.
    assert any("HAS" in note for note in grc), (
        "every difference is reported as an absence, so the two capabilities GRC has and "
        "BTC/LTC do not are invisible -- and those are the ones that break modern-first code"
    )


# ---------------------------------------------------------------------------
# A THIRD EVIDENCE KIND, and the reason it exists rather than being folded into
# one of the two that were already here.
#
# The rpcallowip rows were settled on 2026-10-09 by cloning Gridcoin, checking
# out tag 5.5.1.0, compiling ClientAllowed() and WildcardMatch() verbatim, and
# running them on the operator's exact config. That is the chain's own code
# executing -- far stronger than release history -- and it is still NOT the
# operator's binary, which could have been built from anywhere in history.
#
# Filing it as MEASURED would have claimed their daemon was tested. Filing it as
# RELEASE_HISTORY would have left the row reading "NOT checked against this
# deployment" after the decisive check had been done.
# ---------------------------------------------------------------------------


def test_the_three_evidence_kinds_are_distinct_strings():
    """Two of them collapsing would silently reclassify every row that used it."""
    kinds = (MEASURED, RELEASE_HISTORY, UPSTREAM_SOURCE)
    assert len(set(kinds)) == 3, kinds


def test_only_MEASURED_counts_as_established_on_this_deployment():
    """NOT_THIS_DEPLOYMENT must hold every kind EXCEPT MEASURED.

    THE MUTATION THIS CATCHES IS THE ONE THAT ALMOST SHIPPED. The filter used to
    read `c.evidence == RELEASE_HISTORY`. Adding UPSTREAM_SOURCE without widening
    it would have dropped every upstream-source row out of
    unverified_on_this_deployment() -- a row moving from "go and check this" to
    invisible by being investigated MORE.
    """
    assert MEASURED not in NOT_THIS_DEPLOYMENT
    assert set(NOT_THIS_DEPLOYMENT) == {RELEASE_HISTORY, UPSTREAM_SOURCE}


def test_every_evidence_string_in_use_is_one_of_the_three():
    """A row with a hand-written evidence string is invisible to both filters."""
    known = {MEASURED, RELEASE_HISTORY, UPSTREAM_SOURCE}
    for capability in CAPABILITIES:
        assert capability.evidence in known, f"{capability.name}: {capability.evidence!r}"


def test_unverified_on_this_deployment_is_exactly_the_non_measured_rows():
    """Reached by construction rather than by a count, so adding a row cannot stale it."""
    expected = tuple(c for c in CAPABILITIES if c.evidence != MEASURED)
    assert unverified_on_this_deployment() == expected
    assert all(c.evidence != MEASURED for c in unverified_on_this_deployment())


# ---------------------------------------------------------------------------
# THE TWO GRIDCOIN ANSWERS THAT ARE OPPOSITE TO BITCOIN'S, and a single edit
# needs both right. Measured on the operator's host 2026-10-09:
#
#   config file   GRC splits per network (<datadir>/testnet/...); BTC/LTC use
#                 ONE file with a [regtest] section.
#   subnet form   GRC takes a WILDCARD and matches CIDR against nothing;
#                 BTC/LTC take CIDR and refuse to start on a wildcard.
#
# Getting either backwards reproduces the other's symptom, which is what made
# this cost five rounds.
# ---------------------------------------------------------------------------

_SPLIT_CONF = "one config file with [network] sections"
_CIDR = "rpcallowip in CIDR form (172.18.0.0/16)"


@pytest.mark.parametrize("name", [_SPLIT_CONF, _CIDR])
def test_both_inverted_conventions_are_recorded_as_ABSENT_on_GRC(name):
    """absent_on, not merely missing from present_on.

    A chain in neither tuple is UNKNOWN and has() returns None, which would read
    as "nobody has checked" -- and both of these were checked, at a cost.
    """
    assert has("GRC", name) is False
    assert has("BTC", name) is True
    assert has("LTC", name) is True


def test_the_split_config_row_names_the_path_the_daemon_actually_reads():
    """The remedy is a PATH, and a remedy that does not state it is not a remedy.

    This is the row that cost five rounds; every one of those edits went to
    <datadir>/gridcoinresearch.conf while the daemon read
    <datadir>/testnet/gridcoinresearch.conf. If the sentence an operator reads
    does not contain that second path, the row has not delivered its finding.
    """
    note = absence_note("GRC", _SPLIT_CONF)
    assert "testnet/gridcoinresearch.conf" in note
    assert "Using data directory" in note, "the log line that names it must be quoted"


def test_the_two_rows_point_at_each_other():
    """Rule 8: a reader who finds one must be told the other exists.

    They are not duplicates -- they are two different answers for the same two
    chains -- which is exactly the case rule 8 says belongs in a comment at BOTH
    sites rather than being merged.
    """
    split = _capability(_SPLIT_CONF)
    cidr = _capability(_CIDR)
    assert "rpcallowip" in split.recorded_at, "the split-conf row must name the other one"
    assert "wildcard" in cidr.instead.lower(), "the CIDR row must give GRC's actual form"


def test_the_GRC_remedy_is_a_wildcard_and_the_BTC_remedy_is_not():
    """If these two sentences ever agree, one of them is wrong.

    MUTATION CHECKED: copying GRC's `instead` onto the rpcbind row -- the shape of
    mistake that makes a daemon refuse to start -- fails here.
    """
    cidr_note = _capability(_CIDR).instead
    assert "172.18.*" in cidr_note
    assert "/16" in cidr_note, "it must say what the wildcard is equivalent TO"


# ---------------------------------------------------------------------------
# PUTTING THE REMEDY ON THE SCREEN. For the last three of the five rounds the GRC
# 403 cost on 2026-10-09, the answer was already in this file -- in a row nothing
# consulted. `swapterm chains` printed the bare 403 and stopped. A capability map
# no report reads is documentation, and rule 5's test applies: if a reader has to
# open a file to learn whether something is authorized, the authority is in the
# wrong place. Same for learning why it is NOT.
# ---------------------------------------------------------------------------

_GRC_403 = "did not answer: 403 Client Error: Forbidden for url: http://host.docker.internal:25779/"
_REFUSED = (
    "did not answer: HTTPConnectionPool(host='host.docker.internal', port=18443): "
    "Failed to establish a new connection: [Errno 111] Connection refused"
)


def test_a_GRC_403_names_the_FILE_PATH_first_and_the_syntax_second():
    """BOTH causes, and the file path FIRST, because that is the one that was blocking.

    MUTATION CHECKED, AND THE MUTATION IS THE MISTAKE I MADE: listing only the
    rpcallowip row -- which was the first version of _REFUSAL_REMEDIES -- fails
    here. That version would have handed the operator the syntax fix a fifth
    time, the fix they had already applied correctly to a file the daemon does
    not read.
    """
    remedy = refusal_remedy("GRC", _GRC_403)
    assert "testnet/gridcoinresearch.conf" in remedy, "the file path must be there"
    assert "172.18.*" in remedy, "and the syntax"
    assert remedy.index("testnet/gridcoinresearch.conf") < remedy.index("172.18.*"), (
        "the file path must come FIRST -- it is the cause no amount of staring at the "
        "syntax reveals, and the syntax fix alone is what cost four of the five rounds"
    )
    assert remedy.startswith("(1)"), "two causes are numbered so neither reads as the whole answer"


def test_a_refused_connection_on_BTC_or_LTC_names_rpcbind_and_not_the_GRC_answer():
    """The conventions are INVERTED and handing over the wrong one breaks the daemon.

    A wildcard in a modern bitcoin.conf makes Core refuse to START, so printing
    GRC's remedy here would be worse than printing nothing.
    """
    for asset in ("BTC", "LTC"):
        remedy = refusal_remedy(asset, _REFUSED)
        assert "rpcallowip" in remedy
        assert "172.18.*" not in remedy, f"{asset} must never be told to write a wildcard"


def test_the_two_failure_shapes_are_told_apart():
    assert refusal_shape(_GRC_403) == "403"
    assert refusal_shape(_REFUSED) == "refused"
    assert refusal_shape("did not answer: ReadTimeout") is None
    assert refusal_shape("") is None


@pytest.mark.parametrize(
    ("asset", "detail", "why"),
    [
        ("GRC", "did not answer: ReadTimeout", "a shape nobody recorded"),
        ("XRP", _GRC_403, "a 403 on a chain with no recorded remedies"),
        ("BTC", _GRC_403, "BTC CAN 403 too, but no remedy is recorded for that shape"),
        ("GRC", _REFUSED, "GRC refusing a connection is not the 403 case"),
    ],
)
def test_nothing_recorded_says_NOTHING_rather_than_the_nearest_sentence(asset, detail, why):
    """Silence is right here, and it is the lesson from a defect shipped the same day.

    stack_authority's network line printed GRC's "no bech32" sentence for XRP, a
    chain its table had no row for, because it branched on a bare None. The raw
    failure detail always prints either way, so a chain with no recorded remedy
    loses nothing by this returning "".
    """
    assert refusal_remedy(asset, detail) == "", why


def test_every_capability_named_in_the_remedy_table_actually_exists():
    """A typo'd name here is a KeyError on an operator's screen mid-outage.

    _capability() raises rather than returning None by design, so this is the test
    that keeps the raise from being the first thing anybody notices.
    """
    for asset, entries in _REFUSAL_REMEDIES.items():
        for shape, name in entries:
            assert shape in ("403", "refused"), f"{asset}: unknown shape {shape!r}"
            assert _capability(name).instead.strip(), f"{asset}/{name}: empty remedy"


# ---------------------------------------------------------------------------
# WHICH OF A ROW'S TWO REMEDIES ANSWERS THIS ASSET'S FAILURE.
#
# Measured on the operator's screen 2026-10-10, mid-outage. Their litecoind failed
# to come up on testnet and `swapterm chains` printed, under the LTC refusal:
#
#   RECORDED REMEDY (LTC): nothing is needed: a pre-0.12 daemon binds its RPC port
#   on all interfaces and `rpcallowip` is the only control...
#
# That is the rpcbind row's `instead`, which answers "this chain LACKS the
# capability, what do you do without it" -- GRC's question. LTC HAS rpcbind and was
# not using it. The row carried the right knowledge and the lookup asked it the
# wrong question, so an operator whose daemon had just failed read "nothing is
# needed" as their remedy.
# ---------------------------------------------------------------------------

_LTC_REFUSED = (
    "did not answer: HTTPConnectionPool(host='host.docker.internal', port=19443): "
    "Failed to establish a new connection: [Errno 111] Connection refused"
)


def test_a_chain_that_HAS_rpcbind_is_told_how_to_USE_it():
    """MUTATION CHECKED: reverting _remedy_for() to always return `instead` fails here.

    And it fails on the exact string an operator read during an outage.
    """
    remedy = refusal_remedy("LTC", _LTC_REFUSED)
    assert "nothing is needed" not in remedy, (
        "that is the GRC-facing sentence and it is the opposite of LTC's remedy"
    )
    assert "rpcbind" in remedy
    assert "LOOPBACK ONLY" in remedy, "it must say WHY nothing was accepting"


def test_the_rpcbind_remedy_names_THE_SECTION_for_every_network_in_play():
    """The half that goes wrong silently, and it cost five rounds on GRC.

    A section header that does not match the running network means every line under
    it is skipped, the daemon starts cleanly, and nothing says so. BTC Core 28 wants
    [testnet4]; Litecoin 0.21 wants [test]; regtest wants [regtest]. They are not
    interchangeable.
    """
    remedy = refusal_remedy("BTC", _LTC_REFUSED.replace("19443", "18443"))
    for section in ("[regtest]", "[test]", "[testnet4]"):
        assert section in remedy, f"{section} is not named"
    assert "ERRORS OUT if rpcbind is given without rpcallowip" in remedy, (
        "the two have to go in together and Core refuses to start otherwise"
    )


def test_a_chain_that_LACKS_a_capability_still_gets_instead():
    """GRC's two 403 rows are about capabilities it genuinely does not have.

    `when_unused` must not displace `instead` for the case `instead` was written for.
    """
    remedy = refusal_remedy("GRC", "did not answer: 403 Client Error: Forbidden")
    assert "testnet/gridcoinresearch.conf" in remedy
    assert "172.18.*" in remedy


def test_when_unused_is_empty_on_rows_that_have_only_one_remedy():
    """Most rows describe a capability a chain lacks; a second sentence would be noise.

    Asserted so the field does not get filled in reflexively on every new row -- an
    empty one is the honest default and the fallback handles it.

    TWO ROWS NOW, since 2026-10-10. `getwalletinfo` is on every one of the three and
    the field says what a caller that HAS it still gets wrong: reading the balance and
    not `unlocked_until` cannot tell a funded wallet from a funded wallet that nothing
    can leave. That is precisely the question this field exists for, so the list grew
    by a deliberate decision -- which is what a hardcoded list is for.

    AND THE STRUCTURAL INVARIANT IS ASSERTED BESIDE IT, because the list alone only
    makes a new row deliberate and cannot make it CORRECT. `when_unused` answers "this
    chain has it and is not using it properly", so a row with an empty `present_on` has
    nobody to answer it for -- and that is the shape of the 2026-10-10 defect this
    field was added for: the rpcbind row's `instead` was printed at an LTC operator
    whose daemon had just failed to bind, and it opened "nothing is needed". The field
    was right and it was answering a different question than the one being asked.
    """
    filled = [c.name for c in CAPABILITIES if c.when_unused]
    assert filled == ["getwalletinfo", "rpcbind"], (
        f"these rows have a use-it-properly remedy today; {filled} claim one. Adding one is "
        f"fine and is meant to be deliberate -- say which chain it is for and why."
    )
    for capability in CAPABILITIES:
        if capability.when_unused:
            assert capability.present_on, (
                f"{capability.name} carries a when_unused remedy and NO chain is listed as "
                f"having it, so there is nobody for that sentence to be addressed to. "
                f"`instead` is the field for a capability a chain lacks."
            )
