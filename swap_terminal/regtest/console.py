"""Say what is about to happen, what happened, and what was expected.

Role: submodule (operator-facing output for the regtest harness)
Reads: nothing
Writes: stdout only
Can move funds: no
Mainnet-safe: yes -- it prints, and nothing else.

WHY THIS FILE EXISTS RATHER THAN print() AT EACH SITE (CLAUDE.md rule 14).

This harness is written on a machine that has no bitcoind and no litecoind and
therefore cannot run it. It will first execute on somebody else's computer,
where a bare traceback costs a round trip: the operator pastes back an
exception, and the next question is always "what was it doing when that
happened, and what did it expect instead". So every stage announces its target
and its scale BEFORE it acts, and every assertion prints the value it got
beside the value it wanted, on the same line, whether it passed or failed.

The three shapes this module enforces, each of which rule 14 names:

  announce before, not only after.  `say()` is called with the intent before
    the RPC, not with the result after it. A line that appears only on
    completion is invisible during the wait, which is exactly when the
    operator is deciding whether to press Ctrl-C.

  an empty result is a result.  `value()` renders None and "" as `(none)`.
    A blank gap is ambiguous between zero rows and a query that broke.

  "did nothing" must not look like "did work."  There are four outcomes, not
    two: OK, FAIL, XFAIL (a failure this harness predicted and whose arrival
    is the harness working) and SKIP. They print as different tokens and are
    counted separately in the summary.

DURATIONS ARE MICROFORTNIGHTS AND BLOCK HEIGHTS ARE NOT (rule 6).

`format_duration` from the shared `microfortnights` module renders every
elapsed time as `2.3µfn (2.8s)`. There is deliberately no helper here that
formats a height, a confirmation count or a locktime, because those are not
times: a block is not 1.2096 seconds long, it is however long it took. Heights
print as bare integers and the label beside them says what they are.

NOTHING PRINTED HERE MAY BE A PREIMAGE. The chain-safety rules are absolute
about it: log `secret_hash`, never `secret`. `redact()` exists so that a value
which should never reach a terminal has one obvious way to be rendered, and
the harness passes the preimage through it at the two places it is mentioned
at all.
"""

from __future__ import annotations

import sys
import time

from microfortnights import format_duration

# The four outcomes. XFAIL is the one that matters most in this harness: a
# known defect confirmed against a real chain is a successful measurement, and
# printing it as FAIL would invite somebody to "fix" the assertion.
OK = "OK"
FAIL = "FAIL"
# XFAIL means MEASURED, CONFIRMED, AND DELIBERATELY NOT MADE GREEN. It has had
# no user since 2026-09-25: regtest/steps.py's step 7 was the only one, marking
# a redeem that could not push the preimage, and that defect is fixed. The
# constant stays because a future known defect will want exactly this
# vocabulary -- but a redeem, a contract creation or a refund that FAILS must
# never be re-marked XFAIL to quiet a run. That is the one move this harness
# exists to prevent, and tests/test_regtest_harness_units.py holds it.
XFAIL = "XFAIL"
SKIP = "SKIP"

_INDENT = " " * 10


def value(raw: object) -> str:
    """Render a value so that an empty one is still visible.

    None and the empty string become `(none)`. An empty list or dict becomes
    `(none)` too, with its type named, because "the node returned no outputs"
    and "the call broke" must not render identically (rule 14).
    """
    if raw is None:
        return "(none)"
    if isinstance(raw, str):
        return raw or "(none: empty string)"
    if isinstance(raw, (list, tuple, dict, set)) and not raw:
        return f"(none: empty {type(raw).__name__})"
    return str(raw)


def redact(_secret: bytes) -> str:
    """The only rendering of a preimage this harness will produce.

    It takes the value so that call sites read honestly -- the byte string IS
    in scope there -- and returns a fixed string that contains none of it. The
    parameter is underscore-prefixed because using it would be the defect.
    """
    return "<preimage withheld: see secret_hash>"


class Console:
    """Printer for a run. Holds the step counter and the outcome tallies."""

    def __init__(self, total_steps: int, stream=sys.stdout) -> None:
        self.total_steps = total_steps
        self.stream = stream
        self.counts = {OK: 0, FAIL: 0, XFAIL: 0, SKIP: 0}
        self.failures: list[str] = []
        self._step_started = time.monotonic()

    def _write(self, line: str) -> None:
        # flush on every line: this harness is watched live, and a buffered
        # progress line is the same as no progress line (rule 14).
        print(line, file=self.stream, flush=True)

    def banner(self, text: str) -> None:
        self._write("")
        self._write("=" * 78)
        self._write(text)
        self._write("=" * 78)

    def step(self, number: int, chain: str, title: str) -> None:
        """Announce a stage before it acts. Prints the counter and the scale."""
        self._step_started = time.monotonic()
        self._write("")
        self._write(f"step {number}/{self.total_steps}  [{chain}]  {title}")

    def say(self, text: str) -> None:
        """A detail line under the current step. Intent, not result."""
        self._write(f"{_INDENT}{text}")

    def elapsed(self) -> str:
        """The current step's elapsed time, in microfortnights with seconds."""
        return format_duration(time.monotonic() - self._step_started)

    def check(self, label: str, got: object, expected: object, outcome: str) -> str:
        """Print one assertion: what it got, what it wanted, how it went.

        Returns the outcome so a caller can record it in the same expression.
        Every call increments exactly one tally, and a FAIL is also appended to
        `failures` so the summary at the end can name them without the operator
        scrolling back through several thousand lines of mining output.
        """
        self.counts[outcome] = self.counts.get(outcome, 0) + 1
        line = f"{_INDENT}{outcome:<5} {label}: got={value(got)}  expected={value(expected)}  [{self.elapsed()}]"
        self._write(line)
        if outcome == FAIL:
            self.failures.append(f"{label}: got={value(got)} expected={value(expected)}")
        return outcome

    def summary(self) -> None:
        """The tallies, and every FAIL named. Never an empty block."""
        self.banner("SUMMARY")
        self._write(
            f"  OK={self.counts[OK]}  FAIL={self.counts[FAIL]}  "
            f"XFAIL={self.counts[XFAIL]} (predicted failures: these are the harness working)  "
            f"SKIP={self.counts[SKIP]}"
        )
        self._write("")
        if self.failures:
            self._write("  unexpected failures, in the order they happened:")
            for item in self.failures:
                self._write(f"    - {item}")
        else:
            self._write("  unexpected failures: (none)")
