"""The per-swap reference a Solana deposit carries, and every way it refuses to guess.

Reads: chains/solana_memo.py
Writes: nothing
Can move funds: no
Mainnet-safe: yes
Live-safe: yes

WHAT THESE TESTS ARE ABOUT. Under the one-account-plus-memo strategy the operator chose on
2026-09-29, every SOL swap shares ONE deposit account, so the memo is the only thing that says
whose money an arriving deposit is. deposit_service.attributable_events() states the standard
these hold to: "an uncredited deposit is a support ticket, a misattributed one is somebody
else's money."

So the interesting assertions here are the REFUSALS. A parser that returns a plausible integer
from an ambiguous transaction is indistinguishable from a correct one until somebody is paid
wrongly.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "swap_terminal"))

from chains.solana_memo import (
    MEASURED_MEMO_PROGRAM_IDS,
    MEMO_PROGRAM_IDS,
    MEMO_PROGRAM_V1,
    MEMO_PROGRAM_V2,
    TAG_MAXIMUM,
    deposit_tag_from,
    memo_strings_in,
)


def _tx(instructions, inner=None):
    return {
        "transaction": {"message": {"instructions": instructions}},
        "meta": {"innerInstructions": [{"instructions": inner}] if inner else []},
    }


def _memo(text, program=MEMO_PROGRAM_V2):
    return {"programId": program, "parsed": text}


def test_a_single_numeric_memo_is_the_tag():
    tag, why = deposit_tag_from(_tx([_memo("4242")]))
    assert tag == 4242
    assert "4242" in why, "the successful branch explains itself too, not only the failures"


def test_TWO_MEMOS_CREDIT_NOTHING_AND_THE_REASON_IS_THE_ATTACK():
    """Taking the first memo would let a SENDER choose whose swap gets credited.

    That is the whole reason this is a refusal rather than a first-wins rule: the memo is
    attacker-controlled data, and a parser that resolves ambiguity in any fixed direction hands
    the resolution to whoever writes the transaction.
    """
    tag, why = deposit_tag_from(_tx([_memo("4242"), _memo("7")]))
    assert tag is None
    assert "ambiguous" in why
    assert "append" in why, "and it names the mechanism, so nobody 'fixes' it by taking the first"


def test_an_absent_memo_is_unattributable_rather_than_zero():
    """A missing discriminator must never collapse to a real tag value.

    `0` is a legal tag -- xrp_tag_service allocates from 0 -- so returning it for "no memo"
    would credit a real swap with a deposit that named nothing.
    """
    tag, why = deposit_tag_from(_tx([]))
    assert tag is None and "no memo" in why
    assert "human" in why, "it says who resolves it, because somebody has to"


def test_prose_memos_are_ordinary_and_not_errors():
    """Most memos on a real cluster are text. This is the common case, not a fault."""
    tag, why = deposit_tag_from(_tx([_memo("gm frens")]))
    assert tag is None and "not a decimal integer" in why
    assert "ordinary" in why


def test_a_memo_out_of_the_ALLOCATORS_range_is_refused():
    """The range is xrp_tag_service's, on purpose, so ONE allocator can serve both chains.

    Solana does not impose it -- a memo is arbitrary UTF-8 -- so without this check a number
    nobody could have issued would be treated as a tag (rule 11: one vocabulary, one place).
    """
    assert deposit_tag_from(_tx([_memo(str(TAG_MAXIMUM))]))[0] == TAG_MAXIMUM
    tag, why = deposit_tag_from(_tx([_memo(str(TAG_MAXIMUM + 1))]))
    assert tag is None and "outside the allocator's range" in why


def test_A_MEMO_EMITTED_THROUGH_CPI_IS_NOT_MISSED():
    """A memo from a program invoked via CPI appears ONLY in meta.innerInstructions.

    A reader that checked the outer list alone would miss every wallet that routes through a
    helper program -- and on this path "miss" means a customer's deposit sitting uncredited
    with nothing on screen saying why.
    """
    tag, _ = deposit_tag_from(_tx([], inner=[_memo("99")]))
    assert tag == 99


def test_BOTH_MEMO_PROGRAM_VERSIONS_ARE_READ():
    """Older wallets still emit v1 and the cluster still accepts it.

    Knowing only v2 would silently drop those deposits, which is the same money-shaped failure
    as not reading inner instructions.
    """
    assert deposit_tag_from(_tx([_memo("5", MEMO_PROGRAM_V1)]))[0] == 5
    assert deposit_tag_from(_tx([_memo("5", MEMO_PROGRAM_V2)]))[0] == 5
    assert MEMO_PROGRAM_V1 in MEMO_PROGRAM_IDS and MEMO_PROGRAM_V2 in MEMO_PROGRAM_IDS


def test_another_programs_instruction_is_not_a_memo():
    """The program id is the discriminator. A transfer carrying a parsed string is not a memo."""
    assert memo_strings_in(_tx([{"programId": "11111111111111111111111111111111", "parsed": "4242"}])) == []


def _memo_module_source() -> str:
    return (Path(__file__).resolve().parent.parent / "swap_terminal" / "chains"
            / "solana_memo.py").read_text(encoding="utf-8")


def test_EACH_PROGRAM_ID_SAYS_WHETHER_IT_IS_MEASURED_AND_THE_TWO_DIFFER():
    """ONE IS MEASURED NOW AND THE OTHER IS NOT, and this used to assert neither was.

    It was `test_THE_PROGRAM_IDS_ARE_DECLARED_UNVERIFIED_IN_THE_SOURCE`, and its docstring said
    "These constants were WRITTEN, not measured" -- of both, which was true until 2026-09-30.
    That day the operator ran `--hunt-memo 50` against devnet: ten of MEMO_PROGRAM_V2's own
    transactions were read and memo_strings_in() found a memo in all ten, while every one of the
    twenty-two reads attempted for MEMO_PROGRAM_V1 came back HTTP 429.

    IT KEPT PASSING, on the substring "NOT VERIFIED FROM THIS MACHINE", which the rewritten
    header still contains inside a sentence that now means the opposite for one of the two. That
    is the third time in this session a test has passed a substring while its premise went
    stale, and each time the substring was the reason nobody noticed -- so what is pinned here
    is not a phrase but the PROPERTY: each id states its own status, and the two statuses differ.

    Rule 17 is the whole point of the admission and it cuts both ways. Describing a measured
    constant as unverified is the same register error as the reverse: a reader who cannot tell
    which of the two ids has been exercised will either re-run a settled check or trust an
    unsettled one.
    """
    source = _memo_module_source()
    v2 = source[source.index("MEMO_PROGRAM_V2 ="):source.index("MEMO_PROGRAM_V1 =")]
    v1 = source[source.index("MEMO_PROGRAM_V1 ="):source.index("MEMO_PROGRAM_IDS =")]

    # Each constant's own comment block carries its own verdict. Sliced BACKWARD from the
    # assignment so the comment above it is what is read -- `#:` blocks precede their name.
    above_v2 = source[:source.index("MEMO_PROGRAM_V2 =")].rsplit("\n\n", 1)[-1]
    above_v1 = source[:source.index("MEMO_PROGRAM_V1 =")].rsplit("\n\n", 1)[-1]

    assert "MEASURED" in above_v2, "v2 was exercised against real traffic; say so at the constant"
    assert "2026-09-30" in above_v2, "with the date, because a bare claim ages (rule 3)"
    assert "UNMEASURED" in above_v1, "v1 still is not, and that must not be quietly inherited"
    assert above_v2 != above_v1, "the two cannot carry the same status; one run settled only one"
    assert v2 and v1  # the assignments themselves are intact between the slices

    # AND THE HEADER STILL NAMES THE INSTRUMENT, because v1 is what is left to settle.
    assert "--hunt-memo" in source
    assert "NOT VERIFIED" in source, "the unmeasured half keeps its admission"


def test_the_measured_half_is_not_described_as_a_hypothesis_anywhere_in_the_module():
    """The standing instruction was NARROWED, not deleted, and the narrowing is the finding.

    The header used to say: until that run, treat a zero-match rate as "the constant is wrong"
    before treating it as "nobody uses memos". Applied to v2 now, that sentence is wrong and
    expensive -- it would send somebody to change a constant that ten real transactions have
    confirmed, when a zero-match rate on a v2 memo means the encoding, the CPI path, or the
    transaction instead.

    MUTATION: restore the unqualified instruction and this fails, because it can no longer be
    stated of both ids at once.
    """
    source = _memo_module_source()
    assert "applies to v1 only" in source or "v1 only" in source, (
        "the treat-it-as-wrong instruction has to say WHICH id it still applies to"
    )
    header = source[:source.index("from __future__")]
    assert "STILL NOT VERIFIED" in header, "v1, named"
    assert "MEASURED 2026-09-30" in header, "v2, named, with the run that did it"


def test_THE_INSTRUMENT_solana_memo_POINTS_AT_ACTUALLY_EXISTS():
    """`--hunt-memo` is named in chains/solana_memo.py as what settles the program ids.

    It was named there before it was built, on 2026-09-29 -- a pointer to an instrument that
    did not exist, which is the same defect shape this session spent the day removing from
    other output: a screen (or a docstring) making a claim the tree does not support.

    So this asserts the flag is real, and that the hunt is NOT folded into the exit code.
    Finding no memo traffic is a fact about the cluster rather than about the adapter, and
    print_summary's failures mean "a method or field the adapter depends on did not match a
    real server". A quiet cluster must never read as a broken adapter.
    """
    root = Path(__file__).resolve().parent.parent
    check = (root / "solana_chain_check.py").read_text(encoding="utf-8")
    assert '"--hunt-memo"' in check, "the flag chains/solana_memo.py points at"
    assert "def hunt_memo(" in check
    # AND THE CALL SITE, which the first version of this test left unpinned -- so replacing
    # `if args.hunt_memo > 0:` with `if False:` kept every assertion green while the flag did
    # nothing. A defined-but-never-called instrument is the same lie as a named-but-absent one,
    # which is the defect this whole test exists for.
    assert "if args.hunt_memo > 0:" in check
    assert "hunt_memo(adapter, args.hunt_memo)" in check

    hunt = check[check.index("def hunt_memo("):check.index("def _network_line(")]
    # COMMENTS AND THE DOCSTRING STRIPPED FIRST. The docstring EXPLAINS why `failures` is not
    # touched here, so a naive substring check fails on the explanation rather than on the
    # defect -- the same trap as pinning a dead branch that a comment quotes. Rule 1 wants the
    # reasoning next to the code; a test has to read past it.
    code = "\n".join(
        line for line in hunt.splitlines()
        if not line.strip().startswith("#") and '"' not in line.strip()[:1])
    body = code[code.index("confirmed = False"):] if "confirmed = False" in code else code
    assert "failures" not in body, (
        "the hunt must not feed the exit code -- a cluster with no memo traffic is not a "
        "failing adapter"
    )
    assert "NOT CONFIRMED" in hunt, "and it says so when nothing was read back"

    # THE DENOMINATOR ASSERTION THAT USED TO BE HERE IS GONE, and the reason is the better
    # argument for dropping it than any style preference: it grepped this function's own SOURCE
    # SLICE for the word "unreadable", and on 2026-09-30 the per-id read was extracted into
    # `hunt_one_program_id()` to get hunt_memo() back under the C901 ceiling (rule 12: extract
    # the decision, do not raise the ceiling). The slice above stops at `def _network_line(`, so
    # the word moved out of it and this failed -- while the printed line it was protecting was
    # not only intact but had gained a column.
    #
    # A text check over a source slice pins WHERE code lives, which is the thing a refactor is
    # allowed to change. The invariant it was reaching for is behavioral and is pinned
    # behaviorally, by running the real function and reading the real output:
    #   tests/test_solana_chain_check_units.py
    #     ::test_ten_good_reads_then_a_throttle_storm_still_confirms_the_id  (asked N of 50)
    #     ::test_a_throttled_hunt_blames_the_endpoint_and_says_what_to_do_about_it
    # Those also cover what this one could not: that a rate limit is counted apart from an
    # unreadable transaction, which is the distinction the old single word could not express.


def test_the_measured_set_agrees_with_what_the_prose_says():
    """The data and the sentences are the same fact, so they must not disagree.

    `MEASURED_MEMO_PROGRAM_IDS` exists so solana_chain_check.py's banner can DERIVE each id's
    status rather than carry a hand-written copy -- because earlier on 2026-09-30 the operator's
    custody decision existed in five places, four went stale, and the fifth was found only when
    they read it off a screen. Having the data does not help if the prose beside the constants
    can drift from it, so this pins them to each other.

    MUTATION: add v1 to the set, or drop v2 from it, and this fails against the comments.
    """
    source = _memo_module_source()
    above_v2 = source[:source.index("MEMO_PROGRAM_V2 =")].rsplit("\n\n", 1)[-1]
    above_v1 = source[:source.index("MEMO_PROGRAM_V1 =")].rsplit("\n\n", 1)[-1]

    assert {MEMO_PROGRAM_V2} == MEASURED_MEMO_PROGRAM_IDS
    assert ("MEASURED" in above_v2) is (MEMO_PROGRAM_V2 in MEASURED_MEMO_PROGRAM_IDS)
    assert ("UNMEASURED" in above_v1) is (MEMO_PROGRAM_V1 not in MEASURED_MEMO_PROGRAM_IDS)


def test_the_measured_set_is_a_subset_of_the_ids_the_parser_actually_reads():
    """A measured id the parser does not consult would be a measurement of nothing.

    MUTATION: measure an id that is not in MEMO_PROGRAM_IDS -- a typo, a v3 added to one tuple
    and not the other -- and the banner would print MEASURED beside an id no deposit is ever
    checked against.
    """
    assert set(MEMO_PROGRAM_IDS) >= MEASURED_MEMO_PROGRAM_IDS
    assert MEASURED_MEMO_PROGRAM_IDS, (
        "empty would be honest in 2026-09-29's tree and is not honest now: v2 was measured"
    )


def test_the_chain_check_derives_the_status_rather_than_spelling_it():
    """The banner must ask the module, not restate it.

    MUTATION: hand-write "v2 is measured" into solana_chain_check.py's banner and this fails.
    That is the fifth-copy defect exactly: a fact spelled where it is printed instead of read
    from where it is decided.
    """
    root = Path(__file__).resolve().parent.parent
    check = (root / "solana_chain_check.py").read_text(encoding="utf-8")
    assert "MEASURED_MEMO_PROGRAM_IDS" in check, "the banner reads the set"
    assert MEMO_PROGRAM_V2 not in check, (
        "the chain check must not contain a program id literal -- it imports them"
    )
    assert MEMO_PROGRAM_V1 not in check