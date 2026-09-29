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


def test_THE_PROGRAM_IDS_ARE_DECLARED_UNVERIFIED_IN_THE_SOURCE():
    """These constants were WRITTEN, not measured, and the module has to keep saying so.

    Nothing in the container this was written in can reach a Solana cluster, so no real memo
    transaction has confirmed the program id. That is exactly the shape of error
    chains/monero_transfers.py carried -- seeded tests passing green over an unverified wire
    format -- and the only thing separating the two is that one of them says which it is
    (rule 17). This test fails if that admission is ever quietly deleted.
    """
    source = (Path(__file__).resolve().parent.parent / "swap_terminal" / "chains"
              / "solana_memo.py").read_text(encoding="utf-8")
    assert "NOT VERIFIED FROM THIS MACHINE" in source
    assert "--hunt-memo" in source, "and it names the instrument that would settle it"


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
    assert "unreadable" in hunt, (
        "with the denominator: 'no memo over 20 read' and 'no memo over 20 unreadable' are "
        "different facts (rule 3)"
    )
