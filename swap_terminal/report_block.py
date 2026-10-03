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


def clipped(text: str, limit: int) -> str:
    """A long message cut to `limit`, SAYING SO. Short text is returned untouched.

    MEASURED ON THE OPERATOR'S SCREEN 2026-10-03, which is why this is a function
    and not an inline slice. swap_readiness.py printed a daemon's own error at
    `str(error)[:120]`, and the line that reached them read:

        FAIL  BTC wallet  RPCError: No wallet is loaded. Load a wallet using
                          loadwallet or create a new one with createwallet. (Note:
                          A default wallet is no  <- this daemon has 1 wallet(s) ...

    "A default wallet is no" is a sentence that stops mid-word, with nothing
    saying a tool did it. The reader has three readings and no way to choose: the
    daemon sent a truncated message, the terminal dropped the rest, or something
    cut it deliberately. Only the third is true, and it is the only one that
    requires no action. That ambiguity is rule 14's defect exactly -- a blank gap
    is ambiguous between zero rows and a query that broke -- one layer down, in
    the middle of a string instead of at the end of a block.

    So the marker names the cutter and the size of what was dropped, because a
    reader who needs the tail needs to know there IS a tail and roughly how much:
    a 9-character overrun is a lost clause, a 2000-character one is a stack trace
    that belongs somewhere other than a status line.

    WHY `limit` IS AN ARGUMENT AND NOT A CONSTANT HERE. The two callers in
    swap_readiness.py both pass 120, and a shared default would make that look
    like a decided width when it is not -- the sites that clip in this tree pass
    80, 120 and 200, and nothing has established which is right for a given
    column. One spelling of the MARKER is what rule 8 asks for; one spelling of
    every width would be a guess dressed as a standard.

    STILL CLIPPING WITHOUT A MARKER, counted 2026-10-03 by grepping the tree for
    `[:80]`, `[:120]`, `[:160]` and `[:200]` outside tests -- seven sites, named
    rather than baselined (rule 19):

        swap_terminal_desktop.py:972            note[:80]
        swap_terminal/services/payout_service.py:1041   str(exc)[:200]
        swap_terminal/chains/solana_fee_quote.py:311    str(result)[:200]
        swap_terminal/chains/solana.py:1519             str(result)[:200]
        swap_terminal/chains/solana.py:1995             str(value)[:200]
        rescue_payout.py:178                    swap['failed_reason'][:200]
        fund_testnets.py:328                    response.text[:200]

    Not swept here, for rule 12's reason: those are files this change is not
    otherwise in, and a nine-file diff for a display marker on a live-money
    system buys less than it costs. Each is one call to this function when
    somebody is next in that file.
    """
    if len(text) <= limit:
        return text
    return f"{text[:limit]}... (+{len(text) - limit} more chars, cut by this tool to keep the line readable)"
