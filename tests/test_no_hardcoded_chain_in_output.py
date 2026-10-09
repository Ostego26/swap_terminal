#!/usr/bin/env python3
"""The driver says GRC only when it MEANS GRC.

Role: tests (read-only)
Reads: atomic_swap_xrp.py's source, as an AST. No chain, no network.
Writes: nothing
Can move funds: no
Mainnet-safe: yes

FOUR INSTANCES OF ONE HABIT, all found in one dry run on 2026-09-29 -- the first
time this driver was pointed at Litecoin by an operator rather than by a test:

    policy: initiator 48.0h, participant 24.0h (scale=1.0); GRC 576 blocks at an estimated 150s

150s IS Litecoin's block interval; Gridcoin's is 90. So the NUMBER was right and
the LABEL was wrong, which is the worse of the two -- a reader checking that
timelock against Gridcoin's interval concludes the lock is 14.4 hours when it is
24. The other three were a recovery message ("nobody can claim the GRC"), a
second one after a failed claim, and `getnewaddress "swap-A-claims-GRC"`, which
writes that string into a LITECOIN wallet's address book where, unlike a log
line, it outlives the run.

None of these fail anything. Every one of them is a sentence that is simply
false on two of the three chains, printed at the moment an operator is deciding
whether to fund something. That is rule 16's "a wrong comment is a bug" at the
one place the comment is load-bearing, and rule 8's drift: the driver was
generalized and its prose was not.

WHY AN AST WALK AND NOT A GREP. The file legitimately contains "GRC" dozens of
times -- dict keys, the PROVEN_LIVE entry recording a real Gridcoin run, the
chain list in --help, historical notes naming what Gridcoin's createhtlc used to
require. A grep cannot tell those from a label. This walks the strings passed to
console.say() and console.check() -- what an operator actually SEES -- and
allows the ones that are about Gridcoin specifically.
"""

from __future__ import annotations

import ast
import pathlib

SOURCE = pathlib.Path(__file__).resolve().parent.parent / "atomic_swap_xrp.py"
TREE = ast.parse(SOURCE.read_text())

# Printed strings that name Gridcoin BECAUSE THEY MEAN GRIDCOIN, and stay. Each is
# a statement about that chain in particular, not a label standing in for whichever
# chain is running.
ALLOWED = (
    # The historical note about what Gridcoin's own createhtlc required until the
    # runners moved onto the chain clients. It is about Gridcoin by construction.
    "createhtlc",
    # The evidence table's own sentence. GRC is the chain that ran.
    "GRC's 2026-09-27 swaps",
    # The three client MODULES, named by filename in the refusal that tells a
    # reader what a new chain needs. atomic_grc_client.py is a path, not a label.
    "atomic_grc_client.py",
    # The legacy flag spelling, which is a real accepted option string and is
    # named alongside the current one rather than instead of it. The messages
    # around it used to name ONLY this one, on every chain -- that was the defect,
    # and it is fixed by adding --chain-amount rather than by removing this.
    "its old spelling --grc-amount",
)

#: Both spellings, because searching one missed the other. The first version of
#: this file looked for "GRC" alone and passed a whole LTC run that printed
#:
#:     step 9/10  B reads the secret OFF THE GRIDCOIN CHAIN -- never from A
#:
#: "GRIDCOIN" does not contain "GRC". A check that names one spelling of a thing
#: is a check somebody will route around by accident, which is what happened
#: within the hour of writing it.
SPELLINGS = ("GRC", "GRIDCOIN")

#: Every Console method whose argument an operator READS. `step` was missing from
#: the first version, which is the other half of how the line above survived: it
#: is a step TITLE, not a say() or a check(). The titles are the largest text on
#: the screen.
SPOKEN_METHODS = frozenset({"say", "check", "step", "banner"})


def _joined_text(node: ast.JoinedStr) -> str:
    """An f-string's literal text, with {...} standing in for each interpolation.

    `ast.Constant.value` is typed as EVERY constant kind Python has -- str,
    bytes, bool, int, float, complex, None, Ellipsis -- so the literal parts
    have to be established as str before they can be joined; `"".join(...)` over
    that union matched no overload (pyright reportCallIssue +
    reportArgumentType, 2026-10-09).

    THE NARROWING CANNOT DROP TEXT, which is the only thing that would matter
    here. CPython's parser builds a JoinedStr out of str Constants and
    FormattedValues and nothing else, so the str test is true for every literal
    part that can actually arrive; a part that somehow were not a str renders as
    the same {...} placeholder an interpolation already renders as, which this
    file's whole point is that it reads as NOT a hardcoded chain name.
    """
    return "".join(
        part.value if isinstance(part, ast.Constant) and isinstance(part.value, str) else "{...}"
        for part in node.values
    )


def _spoken_strings(tree: ast.AST) -> list[tuple[int, str]]:
    """Every string handed to console.say() or console.check(), flattened.

    An f-string's literal parts are joined with {...} standing in for each
    expression, so a chain name INTERPOLATED as {ctx.chain} does not read as a
    hardcoded one -- which is the entire distinction being tested.
    """
    spoken = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        target = node.func
        if not (isinstance(target, ast.Attribute) and target.attr in SPOKEN_METHODS):
            continue
        for argument in node.args:
            if isinstance(argument, ast.Constant) and isinstance(argument.value, str):
                spoken.append((node.lineno, argument.value))
            elif isinstance(argument, ast.JoinedStr):
                spoken.append((node.lineno, _joined_text(argument)))
    return spoken


def test_no_line_the_operator_READS_names_GRC_where_it_means_whichever_chain_is_running():
    """MUTATION: put the literal GRC back into the policy line and this fails.

    Verified 2026-09-29 -- and it is the mutation that produced the finding in the
    first place, since the literal was there when this test was written.
    """
    offenders = [
        (line, text) for line, text in _spoken_strings(TREE)
        if any(spelling in text.upper() for spelling in SPELLINGS)
        and not any(allowed in text for allowed in ALLOWED)
    ]
    assert not offenders, (
        "these lines print GRC on every chain:\n"
        + "\n".join(f"  atomic_swap_xrp.py:{line}: {text[:150]}" for line, text in offenders)
        + "\n\nInterpolate the chain ({ctx.chain} or {chain}) instead. A label naming the wrong chain "
          "beside a correct number is worse than a wrong number, because the number is what gets "
          "checked and the label is what gets believed."
    )


def test_the_wallet_LABEL_names_the_chain_it_will_live_on():
    """getnewaddress's label outlives the run, unlike anything printed.

    It is written into that daemon's address book and is what an operator reads
    months later asking what an address was for. "swap-A-claims-GRC" in a Litecoin
    wallet is a wrong answer with no expiry.
    """
    labels = []
    for node in ast.walk(TREE):
        if not isinstance(node, ast.Call):
            continue
        target = node.func
        if isinstance(target, ast.Attribute) and target.attr == "call" and node.args:
            first = node.args[0]
            if isinstance(first, ast.Constant) and first.value == "getnewaddress":
                labels.extend(node.args[1:])
    assert labels, "no getnewaddress call found; this test has stopped measuring anything"
    for label in labels:
        if isinstance(label, ast.Constant):
            # ast.Constant.value is every constant kind at once, so being a str
            # is established rather than assumed: `"GRC" not in b"..."` raises
            # TypeError, and a label that is not text is a defect in its own
            # right -- getnewaddress writes this into the daemon's address book
            # (pyright reportOperatorIssue, 2026-10-09).
            assert isinstance(label.value, str), (
                f"getnewaddress is labeled with the non-string constant {label.value!r}; a wallet "
                f"label is text an operator reads months later"
            )
            assert "GRC" not in label.value, (
                f"getnewaddress is labeled {label.value!r}, which is written into whichever chain's "
                f"wallet the swap runs against"
            )


def test_the_check_is_looking_at_a_file_that_still_says_GRC_somewhere():
    """The allowlist has to be doing work, or this test is measuring nothing.

    If a future edit removed every GRC from the file, the test above would pass
    vacuously and keep passing while somebody reintroduced one in a form it does
    not walk. This asserts the file still contains the string at all.
    """
    text = SOURCE.read_text()
    assert any(spelling in text.upper() for spelling in SPELLINGS), (
        "atomic_swap_xrp.py no longer mentions Gridcoin under any spelling, so the test above "
        "proves nothing. Either the evidence table lost its GRC entry or this check needs rewriting."
    )
    # AND THE WALK HAS TO REACH step() TITLES. Asserting the method set directly, because
    # the one line this file failed to catch was a step title rather than a say().
    assert "step" in SPOKEN_METHODS and "banner" in SPOKEN_METHODS
