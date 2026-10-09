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

import pytest
from chains.daemon_capabilities import (
    BITCOIN_FAMILY,
    CAPABILITIES,
    EQUIVALENTS,
    MEASURED,
    RELEASE_HISTORY,
    absence_note,
    calls_for,
    differences_for,
    has,
    jobs_that_diverge,
    serves_wallet_path,
    unresolved_divergences,
    unverified_on_this_deployment,
    wallet_path_warning,
)

_WALLET_PATH = "multi-wallet HTTP endpoint (/wallet/<name>)"


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
    """The evidence field is not decoration; a row without it is a row in two voices."""
    for capability in CAPABILITIES:
        assert capability.evidence in (MEASURED, RELEASE_HISTORY), (
            f"{capability.name} carries evidence {capability.evidence!r}, which is neither "
            f"a measurement nor release history"
        )
        assert capability.recorded_at.strip(), f"{capability.name} does not say where it came from"


def test_the_unverified_rows_are_listed_and_are_the_ones_i_could_not_test():
    """Rule 17: say which you have, and make the list callable rather than prose.

    These three are Bitcoin Core release history, not readings of the operator's
    daemons -- there is no GRC daemon in this session to ask. If one of them ever
    gets measured, it moves to MEASURED and drops off this list.
    """
    unverified = {c.name for c in unverified_on_this_deployment()}
    assert _WALLET_PATH in unverified, "the wallet-path row was never tested against a GRC daemon"
    assert "rpcbind" in unverified
    assert any("rpcallowip" in name for name in unverified)
    for capability in unverified_on_this_deployment():
        assert capability.evidence == RELEASE_HISTORY


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
