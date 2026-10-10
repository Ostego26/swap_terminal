"""Which files are THIS repository's source, for the tests that sweep the whole tree.

Role: submodule (test support; one table and two pure helpers. No socket, no database,
      no chain, no key.)
Reads: the filesystem, under the repository root
Writes: nothing
Can move funds: no
Mainnet-safe: yes

=============================================================================
WHY THIS EXISTS, MEASURED 2026-10-10
=============================================================================

Twenty-three test files sweep the tree with `rglob`, each carrying its OWN inline
exclusions -- `__pycache__` here, `.venv` there, `node_modules` in a third. That is rule
8's shape exactly: the copies agreed on the day they were written, and the day something
new appeared inside the repository they disagreed about whether it was source.

That day was today. A git worktree was created at `.claude/worktrees/agent-<id>/`, which
is a COMPLETE SECOND COPY OF THE TREE living inside the tree, and three sweeps
immediately reported on it:

    test_address_literals_are_valid::test_every_address_shaped_literal_in_the_tree_decodes
    test_address_literals_are_valid::test_the_literal_count_does_not_climb_back
    test_htlc_contract_api::test_the_amount_keyword_table_has_exactly_ONE_definition_in_the_tree

The third one's message is the clearest statement of the failure:

    the amount-keyword table is spelled in
    ['.claude/worktrees/agent-.../swap_terminal/modules/htlc_contract_api.py:64',
     'swap_terminal/modules/htlc_contract_api.py:64']

One definition, counted twice, reported as two. Every sweep that asserts a count, or
asserts that something is defined exactly once, is wrong by a factor of two the moment a
nested checkout exists -- and a nested checkout is an ordinary thing: `git worktree add`
inside the repository, a tool's scratch clone, a vendored copy.

NOT A DEFECT IN THE TREE, AND STILL WORTH FIXING HERE. The worktree was temporary and is
gone. What is permanent is that these sweeps claim to measure "the tree" and mean "this
repository's own source", and nothing wrote that distinction down anywhere a reader could
find it. So it is written down once, here, and the sweeps that were actually bitten import
it.

=============================================================================
WHAT IS NOT DONE YET, NAMED RATHER THAN BASELINED (rule 19)
=============================================================================

Measured 2026-10-10: 23 test files call `rglob`, `os.walk`, `iterdir` or `glob`. THREE
cases in TWO of them were bitten and those two now use this module. The other 21 files
were not touched, and the reason they survived is worth stating rather than leaving as
luck: most of them look for a SPECIFIC named file and are indifferent to a second copy of
it appearing elsewhere. That tolerance is not a guarantee -- any of them that grows a
count or a uniqueness assertion will break the same way.

AND TWO MORE BROKE WITHIN THE HOUR, which is why that paragraph is corrected here rather
than overwritten (rule 1: the drift is the point). A second worktree was created for a
different agent and the next suite run reported:

    test_two_readiness_verdicts_answer_different_questions::
        test_NOTHING_ELSE_IN_THE_TREE_defines_a_THIRD_readiness_verdict
    test_xrp_balances::test_the_epoch_offset_has_exactly_one_definition_in_the_tree

Both are uniqueness assertions, which is exactly the shape the paragraph above predicted
would break -- and the prediction came true in the time it took to run the suite twice.
The first had its own set of SIX excluded names and still missed `.claude`; the second
tested two. Both now import from here.

SO THE COUNT IS: 4 of 23 converted, 19 not. The denominator is stated because a bare
"converted the broken ones" would read as done (rule 3). Every one of the 19 is tolerant
TODAY for the reason above and none of them is guaranteed to stay that way.

That is named work, not a baseline. There is no suppression here and no tolerated list:
this module is the one place to import from, and the remaining files are a conversion
nobody has done.
"""

from __future__ import annotations

from pathlib import Path

#: The repository root, from this file's own location rather than from a cwd -- a sweep
#: that depends on where pytest was invoked from measures a different tree depending on
#: the shell.
REPOSITORY_ROOT = Path(__file__).resolve().parent.parent

#: Directory names that are never this repository's own source. Any path with one of these
#: among its parts is skipped.
#:
#: `.claude` IS THE 2026-10-10 ADDITION and the header says what it cost. It is where
#: Claude Code puts worktrees, so a nested checkout appears there rather than anywhere a
#: previous exclusion would have caught -- and it is a COMPLETE copy of the tree, which is
#: the one shape that breaks a count rather than merely adding noise.
#:
#: `grc-sol-swap` is this repository's JavaScript suite, excluded because the Python
#: sweeps are about the Python tree; it is not a nested checkout.
NOT_SOURCE = frozenset({
    ".git",
    ".claude",
    "__pycache__",
    "node_modules",
    ".venv",
    "venv",
    ".pytest_cache",
    ".ruff_cache",
    "grc-sol-swap",
})


def is_source(path: Path) -> bool:
    """Is this path part of this repository's own source? PURE apart from no I/O at all.

    A membership test over `path.parts`, so it catches an excluded directory at ANY depth
    -- `.claude/worktrees/agent-x/swap_terminal/db.py` is excluded by its second part, and
    a `__pycache__` six levels down is excluded by its sixth.
    """
    return not NOT_SOURCE & set(path.parts)


def source_files(pattern: str = "*.py", root: Path | None = None) -> list[Path]:
    """Every source file matching `pattern`, sorted. The sweeps' one entry point.

    SORTED, so a test that reports a list reports the same order every run -- rule 14's
    "pasted output has to be self-describing a day later" applied to an assertion message
    somebody is going to read in a terminal.

    `root` defaults to the repository root and exists so a sweep can narrow to a
    subdirectory (`source_files("*.py", REPOSITORY_ROOT / "swap_terminal")`) without
    rebuilding the exclusion logic.
    """
    return sorted(path for path in (root or REPOSITORY_ROOT).rglob(pattern) if is_source(path))
