"""The two-column block a root tool prints, so every tool prints the same one.

Role: shared leaf function (line formatting for terminal output)
Reads: nothing
Writes: nothing
Can move funds: no
Mainnet-safe: yes -- string formatting, no I/O of any kind.

WHY THIS IS A MODULE AND NOT A HELPER IN EACH TOOL.

`labeled()` was written inside open_swap.py on 2026-09-26. show_swap.py needed
the identical two-column block the same day, and copying six lines into it would
have been rule 8's shape at its smallest and most tempting: two spellings of one
layout, agreeing on the day they are written, drifting the first time one tool's
column is widened for a longer label. The drift is not a wrong number, but it is
a real cost -- the whole reason for a column is that an operator scanning two
pasted blocks for one value scans a straight edge, and two tools with different
widths give them a ragged one.

So the survivor owns the concept (rule 8) and lives where both root tools can
reach it, next to microfortnights.py, which is the other leaf both of them
import.

WHAT IS DELIBERATELY NOT MERGED INTO THIS, MEASURED RATHER THAN ASSUMED.

Grepped the repository root 2026-09-26 for every tool that prints an aligned
label column, because "merge the duplicates" needs to know how many there are:

    open_swap.py             labeled(), LABEL_WIDTH = 16   -> now this module
    show_swap.py             the same, by importing it     -> this module
    swap_readiness.py        record(state, name, detail), `{name:<28}` inline,
                             and it prints a VERDICT column first (PASS/FAIL/
                             SKIP) that nothing else here has
    migrate_deposit_vouts.py hand-padded f-strings inside its own report blocks

The last two are left alone on purpose. swap_readiness.record() also appends to
a results list that its exit code is computed from, so it is a different
function that happens to print a column; migrate_deposit_vouts.py would be a
large diff across a file this change is not otherwise in, which is the trade
rule 12 refuses ("clean the files you TOUCHED -- not the tree"). Naming them
here is the other half of rule 8: a reader who finds this module is told the
other spellings exist, so nobody concludes from this file that the tree has one.
"""

# THE LABEL COLUMN, spelled once for every tool that prints one.
#
# Labels are 15 characters or fewer so there is always a gap before the value.
# That is checked by a test rather than by eye, because a 16-character label
# produces `estimated payout49.24` -- still true, still unreadable, and exactly
# what the first draft of open_swap.report_lines() did with two of its labels.
LABEL_WIDTH = 16

# The indent a continuation line uses, so that a value wrapping onto a second
# line starts under the value column rather than under the label. Derived from
# LABEL_WIDTH plus the two leading spaces `labeled()` writes, because a
# hand-written 18 here would be a second copy of the width that nothing would
# notice had drifted.
CONTINUATION = " " * (2 + LABEL_WIDTH)


def labeled(label: str, value: str) -> str:
    """One line of a printed block: two spaces, the label in its column, the value.

    The value is not wrapped, truncated or reformatted. A tool printing a number
    prints the number its row holds -- a value reprinted in a different shape
    than the database holds it is how a reader concludes two figures differ when
    they do not.
    """
    return f"  {label:<{LABEL_WIDTH}}{value}"
