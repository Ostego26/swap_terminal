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
        if not (isinstance(target, ast.Attribute) and target.attr in {"say", "check"}):
            continue
        for argument in node.args:
            if isinstance(argument, ast.Constant) and isinstance(argument.value, str):
                spoken.append((node.lineno, argument.value))
            elif isinstance(argument, ast.JoinedStr):
                spoken.append((node.lineno, "".join(
                    part.value if isinstance(part, ast.Constant) else "{...}"
                    for part in argument.values)))
    return spoken


def test_no_line_the_operator_READS_names_GRC_where_it_means_whichever_chain_is_running():
    """MUTATION: put the literal GRC back into the policy line and this fails.

    Verified 2026-09-29 -- and it is the mutation that produced the finding in the
    first place, since the literal was there when this test was written.
    """
    offenders = [
        (line, text) for line, text in _spoken_strings(TREE)
        if "GRC" in text and not any(allowed in text for allowed in ALLOWED)
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
    assert "GRC" in SOURCE.read_text(), (
        "atomic_swap_xrp.py no longer mentions GRC anywhere, so the test above proves nothing. "
        "Either the evidence table lost its Gridcoin entry or this check needs rewriting."
    )
