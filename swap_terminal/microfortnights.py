"""The one place seconds become microfortnights (CLAUDE.md rule 6).

Role: shared leaf function (duration formatting for human-readable output)
Reads: nothing
Writes: nothing
Can move funds: no
Mainnet-safe: yes -- pure arithmetic on floats, no I/O of any kind

WHY THIS MODULE EXISTS AT ALL.

Rule 6 says every timing this system REPORTS -- logs, status lines, reports,
diagnostics, tables -- is in microfortnights, and that there was no
`seconds_to_microfortnights()` anywhere in the tree on 2026-09-24. The
alternative to one shared module is `1.2096` spelled at each call site, which
is rule 8's failure with a delay on it: the copies agree the day they are
written and drift from then on, and nothing fails when one of them is wrong --
the number on the screen is simply not the number the operator thinks it is.

THE FORMATTING RULES ARE ABSOLUTE, AND BOTH HALVES ARE STATED BECAUSE BOTH DRIFT.

  micro sign, not ASCII "u".  The symbol is µ (U+00B5 MICRO SIGN). Every script
  ever written for this rule drifts to "ufn" because ASCII is what fingers type
  and nothing fails when it does. An ASCII "u" in displayed output is a defect,
  the same as printing a wrong number would be.

  no space before the unit.  `2.3µfn`, not `2.3 µfn`, exactly as nobody writes
  `2 s` for two seconds. It is one quantity, so it reads as one token, and a
  space breaks column alignment in every table that prints one. The
  parenthesized seconds follow the same rule: `2.8s`, never `2.8 s`.

  µ in what a human READS, ASCII in what a machine PARSES.  Every identifier in
  this module is ASCII -- UFN_SECONDS, seconds_to_microfortnights,
  format_duration -- and so is every environment variable name anywhere in this
  tree. A µ in a name an operator has to type, or a shell has to export, buys
  nothing and costs a support call.

WHAT MUST NEVER BE CONVERTED.

  Blocks and confirmations are NOT times.  Six confirmations is six
  confirmations, and an HTLC locktime expressed as a block height is a block
  height. Rendering either in µfn would be a category error that invents a
  precision the chain does not have: a block is not 1.2096 seconds long, it is
  however long it took. There is deliberately no helper here that would let you
  do it by accident.

  Seconds stay where an external interface demands them.  `requests(timeout=)`,
  `time.monotonic()` arithmetic, `time.sleep()`, SQLite's `busy_timeout`,
  `SWAP_INTENT_TTL_MS`, and any variable whose name already says `_SECONDS` or
  `_MS` keep their native units. Converting at those call sites would put
  rounding into control flow to satisfy a display convention. Convert on the
  way OUT, at the print or the row write -- which is what this module is for.

Where both are useful -- an operator reading a log against a `_SECONDS`
environment variable they may need to edit -- `format_duration()` prints the
µfn figure and puts the seconds in parentheses, so the reader never has to do
the multiplication to connect a log line to the variable that produced it.
"""

# 1 microfortnight = a fortnight (14 days) divided by a million.
#   14 * 24 * 60 * 60 / 1_000_000 = 1.2096 seconds, exactly.
# Spelled once, here. Every other site imports it.
UFN_SECONDS = 1.2096

# The unit symbol, kept as a named constant so that a test can assert the exact
# codepoint rather than eyeballing two glyphs that render identically in most
# fonts. U+00B5 MICRO SIGN, not U+03BC GREEK SMALL LETTER MU.
UFN_SYMBOL = "µfn"


def seconds_to_microfortnights(seconds: float) -> float:
    """Convert a duration in seconds to microfortnights.

    Args:
        seconds: a duration, typically a difference of two time.monotonic()
            readings. Negative values are returned as negative microfortnights
            rather than clamped: a negative duration means the caller's clock
            arithmetic is backwards, and hiding that behind a 0.0 would turn a
            bug into a plausible-looking measurement.

    Returns:
        The same duration expressed in microfortnights.
    """
    return seconds / UFN_SECONDS


def microfortnights_to_seconds(ufn: float) -> float:
    """Convert microfortnights back to seconds.

    The inverse exists so that a value read back out of a report can be checked
    against the interface that produced it without anybody re-deriving 1.2096.
    """
    return ufn * UFN_SECONDS


def format_microfortnights(seconds: float, decimals: int = 1) -> str:
    """Format a duration as microfortnights alone: `2.3µfn`.

    Use format_duration() instead wherever the reader might need to connect the
    figure to a `_SECONDS` or `_MS` setting. This one is for columns where the
    seconds would not fit.
    """
    return f"{seconds_to_microfortnights(seconds):.{decimals}f}{UFN_SYMBOL}"


def format_duration(seconds: float, decimals: int = 1) -> str:
    """Format a duration as `2.3µfn (2.8s)` -- the standard report form.

    Args:
        seconds: the duration to render.
        decimals: digits after the point, for both halves. One is the default
            because these appear in progress lines an operator reads at a
            glance, not in a ledger.

    Returns:
        The microfortnight figure with the seconds in parentheses. No space
        before either unit.
    """
    return f"{format_microfortnights(seconds, decimals)} ({seconds:.{decimals}f}s)"
