"""The shared line formatting every root tool prints through.

Role: test (pure functions; opens no socket and touches no database)
Reads: swap_terminal/report_block.py
Writes: nothing
Can move funds: no
Mainnet-safe: yes
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "swap_terminal"))

from report_block import LABEL_WIDTH, clipped, labeled

# The real message, at its real length, from the operator's host on 2026-10-03.
# A fixture that invented a long string would test the arithmetic; this one tests
# the case that reached a screen.
BITCOIND_MINUS_18 = (
    "No wallet is loaded. Load a wallet using loadwallet or create a new one with createwallet. "
    "(Note: A default wallet is no longer automatically created)"
)


def test_a_message_short_enough_is_returned_untouched():
    """The common case, and the one a marker must not reach.

    Asserted as identity rather than as "no marker": a version that appended an
    empty-overrun marker ("... (+0 more chars ...)") to every short line would
    satisfy a substring assertion while making every clean line noisier.
    """
    assert clipped("No wallet is loaded.", 120) == "No wallet is loaded."


def test_the_limit_itself_is_not_a_cut():
    """An off-by-one here claims a cut that did not happen.

    The length-equals-limit case is the one a `<` instead of a `<=` gets wrong,
    and it gets it wrong in the direction that lies: a complete message labeled as
    truncated sends a reader looking for a tail that does not exist.
    """
    exact = "x" * 120

    assert clipped(exact, 120) == exact


def test_a_cut_message_says_a_tool_cut_it_and_how_much_is_missing():
    """The defect this exists for, measured on the operator's screen.

    swap_readiness.py printed this error at `str(error)[:120]` and the line read
    "... (Note: A default wallet is no" -- a sentence stopping mid-word with
    nothing saying who stopped it. Three readings, no way to choose between them:
    the daemon sent a truncated message, the terminal dropped the rest, or a tool
    cut it. Only the third is true and only the third needs no action.
    """
    result = clipped(BITCOIND_MINUS_18, 120)

    assert result.startswith(BITCOIND_MINUS_18[:120])
    assert "cut by this tool" in result, "the reader must be able to tell a tool did this, not the daemon"
    assert f"+{len(BITCOIND_MINUS_18) - 120} more chars" in result, (
        "how much is missing decides what to do about it: a lost clause is not a lost stack trace"
    )


def test_the_overrun_is_counted_and_not_merely_announced():
    """A constant "(+some more)" would pass the test above.

    Two lengths, because one of them is satisfied by any hardcoded number.
    """
    assert "+5 more chars" in clipped("y" * 125, 120)
    assert "+80 more chars" in clipped("y" * 200, 120)


def test_labeled_still_pads_to_the_shared_column():
    """The module's original job, held here because nothing else held it.

    report_block.py had no test file at all until 2026-10-03 -- open_swap.py's and
    show_swap.py's suites exercised labeled() through their own output, which pins
    the block they print and not the leaf both import. A leaf two root tools share
    is exactly rule 10's "the thing that decides is the smallest testable piece",
    and it was the one piece nothing called directly.
    """
    line = labeled("swap", "s_1a44ddb70c118d13")

    assert line == f"  {'swap':<{LABEL_WIDTH}}s_1a44ddb70c118d13"
    assert line.startswith("  "), "the two leading spaces are what CONTINUATION is derived from"
