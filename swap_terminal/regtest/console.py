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
from typing import Protocol

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


class ConsoleLike(Protocol):
    """What `funding_steps.Run.console` is actually used for: four methods, not six.

    WHY THIS EXISTS. `Run.console` was declared `Console`, the concrete class below,
    and `operator_panel.py:2122` assigns a WRAPPER into it --
    `regtest/operator_panel.SaysEachLineOnce`, which prints a given line once and
    then stops repeating it, so a page redraw does not restate the same five
    payment lines. pyright refused the assignment. Not a defect: that wrapper
    defines `say` and `should_say` and forwards everything else through
    `__getattr__`, so nothing raises -- but `inspect.getmembers` cannot see a
    `__getattr__` name, which is what made the surface look incomplete on a first
    reading of it.

    THE FOUR MEMBERS WERE COUNTED, NOT GUESSED. Grepped `funding_steps.py` for
    what it reaches off a console: `say` five times, `elapsed` twice, `step` once,
    `check` once. `banner` and `summary` are never called on it. Declaring the
    concrete class there OVER-PROMISES by two methods, which is the same defect
    `step_console.StepNarrator` documents at length for the other console.

    MAKING THE WRAPPER SUBCLASS Console WOULD BE WRONG, and that is worth saying
    because it is the shorter fix. `SaysEachLineOnce` holds a DIFFERENT console
    instance and delegates to it; inheriting would give it Console's own `results`
    list and step counter alongside the one it wraps, so `summary()` would read the
    empty inherited state rather than the real one. A wrapper that inherits from
    what it wraps has two of everything.

    POSITIONAL-ONLY, AND HERE IT IS LOAD-BEARING RATHER THAN TIDY:
    `SaysEachLineOnce.say` names its parameter `line`, and `Console.say` names it
    `text`. pyright matches parameter NAMES for an ordinary parameter, so without
    `/` the wrapper would not satisfy a protocol written against `Console`'s
    spelling -- the exact case `step_console.py` measured the same day.

    THIS IS NOT step_console.StepReporter AND THE TWO MUST NOT BE MERGED. There are
    two classes named `Console` in this tree, and their `check()` disagrees about
    what a verdict is: this one takes `outcome: str` and returns `str`, the other
    takes `ok: bool` and returns `bool`. See this module's OK/FAIL constants and
    `step_console.Console.check`'s refusal, which exists because crossing them
    printed OK and exited 0. One protocol over both would have to accept either,
    which is how that crossing becomes legal again.
    """

    def say(self, text: str, /) -> None: ...
    def step(self, number: int, chain: str, title: str, /) -> None: ...
    def check(self, label: str, got: object, expected: object, outcome: str, /) -> str: ...
    def elapsed(self) -> str: ...


class Console:
    """Printer for a run. Holds the step counter and the outcome tallies."""

    def __init__(self, total_steps: int, stream=None) -> None:
        self.total_steps = total_steps
        # RESOLVED AT CONSTRUCTION, NOT AT IMPORT, and that is a fix rather than a
        # style change. `stream=sys.stdout` as a DEFAULT ARGUMENT is evaluated once,
        # when this `def` executes -- so every Console built without an explicit stream
        # held the stdout object that existed when this module was first imported, not
        # the one in place when it was constructed.
        #
        # WHAT THAT COST, 2026-10-10: a test of testnet_wallets.main() could not read
        # its own output. main() builds its own Console, pytest installs its capture
        # before importing the test modules, and the captured object therefore belonged
        # to pytest's SESSION-level capture rather than the test's -- so capsys AND
        # capfd both returned '' while the output sat plainly visible in pytest's
        # "captured stdout" dump. Present on the screen, absent from readouterr().
        #
        # Every existing caller either passes a stream explicitly or wants whatever
        # stdout is current, so this changes nothing for them: the two can only differ
        # when something replaced sys.stdout after import, and in that case the new
        # behavior is the correct one. It is also why no test had hit this -- every
        # other test in this repo passes stream=io.StringIO(), and a main() test
        # cannot.
        self.stream = sys.stdout if stream is None else stream
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

    def check(self, label: str, got: object, expected: object, outcome: object) -> str:
        """Print one assertion: what it got, what it wanted, how it went.

        Returns the outcome so a caller can record it in the same expression.
        Every call increments exactly one tally, and a FAIL is also appended to
        `failures` so the summary at the end can name them without the operator
        scrolling back through several thousand lines of mining output.

        `outcome: object` RATHER THAN `str`, AND THE REFUSAL BELOW IS WHY. This is the
        MIRROR of step_console.Console.check's refusal, added 2026-10-10 when the
        crossing finally happened in this direction. That module's header predicted it
        exactly -- "a reader moving a line between a `from regtest.console import FAIL,
        OK, Console` file (11 precedents) and a `from step_console import Console`
        file" -- and C16 hardened only the half that had been crossed.

        WHAT A BOOL DID HERE, MEASURED before this guard existed:

            Console(1).check("a bool failure", "got", "want", False)
              counts   {'OK': 0, 'FAIL': 0, 'XFAIL': 0, 'SKIP': 0, False: 1}
              failures []
              printed  "0     a bool failure: got=... expected=..."

        counts[FAIL] stays ZERO, `failures` stays EMPTY, and a phantom `False` key is
        added to the tally -- so a failed check is invisible to summary() and to every
        exit code read off counts[FAIL]. That is C16's incident ("a FAIL produced a
        clean exit, silently") in the opposite direction, and it is WORSE here, because
        step_console at least printed the wrong word loudly while this prints `0`.

        MEASURED 2026-10-10 ACROSS THE TREE: 5 files import step_console and pass 68
        bool outcomes and 0 verdict strings; 8 files import this one and pass 61 verdict
        strings and 0 bools. Both populations were already clean -- this guard fixes NO
        existing call. The first violation was testnet_wallets.py, written the same day
        by the author of this comment, and its own test caught it. That is the argument
        for a guard rather than a note: the trap is for the NEXT caller, and the next
        caller arrives by copy-paste rather than by writing a wrong type on purpose.

        `self.counts.get(outcome, 0) + 1` IS WHAT MADE IT SILENT and is deliberately
        left alone. The `.get` is correct for a verdict this tally has not seen yet;
        what was missing is that an outcome which is not a verdict never reaches it.
        """
        if outcome not in self.counts:
            raise TypeError(
                f"check({label!r}) was given outcome={outcome!r} ({type(outcome).__name__}), which "
                f"is not one of {sorted(self.counts)}. If that came from step_console.py, its "
                f"Console takes a BOOL in this position -- a different class with the same name. A "
                f"bool here would print '0' or '1', add a phantom key to the tally, leave FAIL at 0 "
                f"and append nothing to failures, so a failed check would be invisible to the "
                f"summary and to the exit code."
            )
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
