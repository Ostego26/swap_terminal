#!/usr/bin/env python3
"""No runtime output may tell a reader the Solana deposit strategy is unchosen. It was chosen.

Role: tests (read-only)
Reads: every tracked .py file's string literals, via ast. No network, no database, no chain.
Writes: nothing
Can move funds: no
Mainnet-safe: yes

WHY THIS GATE EXISTS, AND IT IS NOT A RATCHET. CLAUDE.md rule 19 allows exactly one thing --
stopping a measured defect class from GROWING -- and forbids parking a backlog behind a
baseline. There is no baseline here and no allowlist: the count is ZERO and the gate is clean,
which is the state rule 19 says a ratchet is only ever a way station toward.

THE DEFECT CLASS, MEASURED. The operator chose the one-account-plus-memo deposit strategy on
2026-09-29 (commit d22b2a1) and the credit path was built to it the same day. FIVE surfaces went
on telling a reader the question was open:

    services/admin_view.py        the operator's chain page      fixed 2026-09-30
    services/swap_view.py         the customer's deposit page     fixed 2026-09-30
    regtest/operator_panel.py     the SOL tab                     fixed 2026-09-30
    README.md                     the section heading             fixed 2026-09-30
    solana_chain_check.py         the PASSED summary              fixed 2026-09-30, LATER

The fifth is why this file exists. The first four were found by grepping for the phrases they
used -- "not decided", "custody choice is the operator's", "until it is settled" -- and
solana_chain_check.py spells it "still the operator's choice", so the grep missed it. It was
found when the operator ran the script and pasted the output back. That is rule 2's warning
verbatim: grep the tree for the NAME, and a phrase is not a name. A sixth spelling would be
missed by exactly the same method, so the check is a check rather than another careful grep.

WHAT IT SCANS, AND THE FIRST VERSION OF THIS PARAGRAPH WAS WRONG IN A WAY WORTH KEEPING.
It said the boundary was "string literals only, via AST", on the reasoning that the fixes quote
the stale sentences in COMMENTS -- rule 1 says the reasoning is what survives, and "this used to
say X" is most of it -- so an AST walk would see the defects and not the corrections. That is
false, and the check failed on itself the first time it ran: a DOCSTRING is an `ast.Constant`
str like any other, so every correction docstring, and this module's own, came back as an
offender. I wrote that sentence from a plausible reading of the AST instead of running it, which
is exactly rule 17's failure, committed inside the file arguing against it. Verified after, by
parsing a one-line function with a docstring and walking it: the docstring comes back as a str
Constant indistinguishable from any other.

The real boundary is two structural exclusions, and neither is a list of files:

  DOCSTRINGS ARE EXCLUDED     identified by position -- the first statement of a module, class
                              or function -- not by content. A docstring explains; it is not
                              output. This is where every correction lives.
  tests/ IS OUT OF SCOPE      the gate is about what the APPLICATION prints at an operator. A
                              test prints at nobody, and the two tests that quote these phrases
                              (this one's STALE tuple, and test_solana_adapter's assertion that
                              the refusal no longer offers a menu) quote them in order to
                              FORBID them.

Said plainly so the zero is not read as more than it is (rule 14): a stale sentence hidden in a
test's own literal would not be caught, and would harm nobody, because nothing renders it.

`admin_view._attribution_note()`'s default branch still RETURNS "not decided in this
application" and must -- for a chain that genuinely has no model. So the phrases below are the
ones that assert something about SOLANA's strategy specifically, never the generic default.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent

#: Sentences that assert the Solana deposit strategy is unchosen. Each is quoted from the
#: literal that carried it, so a reader can see this list is evidence rather than a guess.
STALE = (
    # solana_chain_check.py's PASSED summary, read off the operator's screen 2026-09-30.
    "deposit-address strategy is still the operator's choice",
    # chains/solana.py::get_new_address()'s NotImplementedError, all four of its claims.
    "no strategy has been chosen",
    "Until the operator chooses",
    "payout-side asset only",
    "The recommendation there is the memo strategy",
    # The menu it offered. Two of the three name a custody model nobody is going to build now,
    # and offering them at runtime invites re-opening a settled question.
    "fresh keypair per swap",
    "derivation from a seed",
)


def application_python_files() -> list[Path]:
    """Every .py the application runs, minus the virtualenv, caches and tests/.

    NOT `git ls-files`, because this suite runs from a plain checkout and a subprocess for a
    file list is a dependency on git being present for a check that is about text.
    """
    skip = {".venv", "venv", "__pycache__", ".git", "node_modules", "tests"}
    return sorted(
        path for path in REPO.rglob("*.py")
        if not any(part in skip for part in path.parts)
    )


def _docstring_nodes(tree: ast.AST) -> set[int]:
    """The id() of every string Constant that is a docstring rather than output.

    BY POSITION, NOT BY CONTENT: the first statement of a module, class or function. A
    triple-quoted string anywhere else is an expression whose value goes somewhere, and one at
    the top of a definition is documentation. Nothing here inspects what it says.
    """
    holders = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
    found = set()
    for node in ast.walk(tree):
        if not isinstance(node, holders) or not node.body:
            continue
        first = node.body[0]
        if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) \
                and isinstance(first.value.value, str):
            found.add(id(first.value))
    return found


def stale_claims_in(path: Path) -> list[tuple[int, str, str]]:
    """(line, phrase, literal) for every NON-DOCSTRING literal asserting the choice is open.

    A SYNTAX ERROR IS A FAILURE, NOT A SKIP. A file this cannot parse is a file this cannot
    check, and silently passing it is how a gate reports green over the thing it was built to
    see -- CLAUDE.md rule 13's "skipped must not look like success", one layer down.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    docstrings = _docstring_nodes(tree)
    return [
        (node.lineno, phrase, node.value.strip()[:120])
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
        and id(node) not in docstrings
        for phrase in STALE
        if phrase in node.value
    ]


def test_no_runtime_string_claims_the_solana_strategy_is_undecided():
    """The gate. Zero, tree-wide, with the denominator stated (rule 3)."""
    files = application_python_files()
    assert files, "found no Python files to check -- the walk itself is broken"

    offenders = {}
    for path in files:
        claims = stale_claims_in(path)
        if claims:
            offenders[path.relative_to(REPO)] = claims

    assert not offenders, (
        "These string literals tell a reader the Solana deposit strategy is unchosen. It was "
        "chosen 2026-09-29: one shared account plus a per-swap Memo instruction. Point the "
        "reader at services/swap_service.deposit_account(), which has the answer.\n"
        + "\n".join(
            f"  {path}:{line}  [{phrase!r}]  {literal}"
            for path, claims in sorted(offenders.items())
            for line, phrase, literal in claims
        )
        + f"\n  (checked {len(files)} application Python files, docstrings excluded)"
    )


def test_a_docstring_is_excluded_and_a_printed_line_in_the_same_file_is_not():
    """THE BOUNDARY, PROVEN ON A SEEDED FILE RATHER THAN ASSERTED IN PROSE.

    This test replaces one that asserted the opposite and was wrong. It claimed "comments and
    docstrings are invisible to it" and was built on that: comments are, docstrings are NOT --
    a docstring is an `ast.Constant` str like any other, and the check failed on its own module
    the first time it ran. The fix is `_docstring_nodes()`, which excludes a string by its
    POSITION as the first statement of a module, class or function.

    So the property worth pinning is not "docstrings are invisible" but "the exclusion is
    positional and does not leak". Seeded with one file carrying the same sentence twice -- once
    as a function docstring, once as a printed line -- because that is the pair the real defect
    came in: chains/solana.py's corrected docstring explains the old menu on the line above the
    message that must not offer it.
    """
    seeded = REPO / "tests" / "__seeded_boundary_check.py"  # not written; parsed from a string
    source = (
        'def f():\n'
        '    """Doc: this used to say no strategy has been chosen, and it was wrong."""\n'
        '    print("no strategy has been chosen")\n'
    )
    tree = ast.parse(source)
    docstrings = _docstring_nodes(tree)
    carried = [
        (node.lineno, id(node) in docstrings)
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
        and "no strategy has been chosen" in node.value
    ]
    assert sorted(carried) == [(2, True), (3, False)], (
        f"the docstring on line 2 must be excluded and the print on line 3 must not: {carried}"
    )
    assert not seeded.exists(), "this test writes nothing; the source above is parsed in memory"


def test_this_module_passes_its_own_check_only_because_tests_are_out_of_scope():
    """AND THE HONEST REASON, which is not the flattering one.

    This file quotes every banned phrase in its STALE tuple -- a plain literal, not a docstring,
    so `stale_claims_in()` sees all seven. It is not caught because `application_python_files()`
    excludes tests/ entirely, and the module docstring says so rather than letting the zero read
    as tree-wide. Asserted here so nobody later "tightens" the scan to include tests/ and then
    silences it with an allowlist, which is rule 19's baseline wearing a different hat.
    """
    mine = Path(__file__)
    assert mine not in application_python_files(), "tests/ is out of scope by category"
    # SET COVERAGE, NOT A COUNT, and the count is why: it came back 9 against 7, because the
    # seeded source in the test above is itself a non-docstring literal carrying the phrase
    # twice. Counting occurrences pins an incidental fact about this file's other tests; what
    # matters is that every banned phrase is still VISIBLE to the scanner from here.
    visible = {phrase for _, phrase, _ in stale_claims_in(mine)}
    assert visible == set(STALE), (
        "every banned phrase should still be visible in this file's own tuple -- if one is not, "
        f"the tuple has moved into a docstring and the test below is vacuous: {set(STALE) - visible}"
    )


@pytest.mark.parametrize("phrase", STALE)
def test_every_banned_phrase_would_actually_be_caught(phrase, tmp_path):
    """Each entry is proven to fire, so a typo in the tuple cannot make it a decoration.

    A banned phrase misspelled by one character bans nothing and looks exactly like protection.
    Seeded into a real file, parsed by the real function.
    """
    seeded = tmp_path / "seeded.py"
    seeded.write_text(f'MESSAGE = "prefix {phrase} suffix"\n', encoding="utf-8")
    assert stale_claims_in(seeded) == [(1, phrase, f"prefix {phrase} suffix")]
