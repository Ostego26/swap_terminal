#!/usr/bin/env python3
"""The step console every verifier prints through: announce, assert, summarize.

Role: submodule (output only; it holds no decision about any chain)
Reads: nothing
Writes: stdout
Can move funds: no
Mainnet-safe: yes

WHY THIS IS SHARED, AND WHY regtest/console.py IS NOT IT.

Three verifiers now print in this shape -- regtest_htlc_verify.py (BTC and LTC),
xrp_htlc_escrow.py (XRPL), grc_htlc_verify.py (Gridcoin) -- and the conventions
are not cosmetic. They are CLAUDE.md rules: microfortnights with the seconds in
parentheses (rule 6), `(none)` rather than a blank for an empty result, every
assertion printing what arrived beside what was wanted, and a summary that
distinguishes "did nothing" from "did work" (rule 14). A second and third copy
of that would drift, and the drift would be invisible because each copy looks
right in its own file (rule 8).

regtest/console.py stays where it is and is NOT merged into this. It is built
around nine steps across TWO chains with a spawned daemon per chain, so its
`check()` prefixes every label with the asset and its state includes which chain
is speaking -- a single-chain verifier importing it would carry a ChainConfig it
has no use for. The two are named at each other so a reader who finds one knows
the other exists: that one is the two-chain harness's console, this one is for a
verifier that talks to one chain.

AND THE PARAGRAPH ABOVE NAMED THE HARMLESS DIFFERENCE AND NOT THE DANGEROUS ONE,
which is why this one exists (2026-10-09). The two classes share a NAME, share
five method names, and disagree about the fourth argument of the one method that
decides whether something passed:

    step_console.Console.check(label, got, expected, ok: bool)    -> bool
    regtest.console.Console.check(label, got, expected, outcome: str) -> str

regtest/console.py's verdicts are STRINGS -- OK = "OK", FAIL = "FAIL",
SKIP = "SKIP", XFAIL = "XFAIL" -- and every non-empty string is truthy. So
`step_console_instance.check(label, got, expected, FAIL)` prints "OK  ", appends
a PASS to self.results, and summary() returns exit code 0. A FAIL constant
produces a pass and a clean exit, silently, which is precisely rule 13's "a cycle
that did no work must not report the same way as one that did".

MEASURED 2026-10-09, AND IT IS LATENT RATHER THAN LIVE: 19 files import a Console,
8 this one and 11 that one, and NO file imports both. All 82 check() calls in the
8 files that import this one pass a real boolean -- checked, not assumed. What
makes it worth a guard rather than a note is that the two populations are one
copy-paste apart: a reader moving a line between a `from regtest.console import
FAIL, OK, Console` file (11 precedents) and a `from step_console import Console`
file (8 precedents) gets a silent wrong verdict and an exit 0, and nothing in
either file looks wrong.

So check() REFUSES a non-bool rather than trusting the annotation. That is the
same shape as the six validating boundaries widened to `object` on the same day,
and `ok: object` is the honest type for the same reason: this function takes
whatever arrives and answers with a named refusal.

The step COUNT is a constructor argument rather than the hardcoded `/9` this
grew up with. All three verifiers happen to have nine steps and the fourth will
not.
"""

from __future__ import annotations

import time
from typing import Protocol

from microfortnights import format_duration


class StepNarrator(Protocol):
    """A console a helper may only SAY lines through. One method, because some bodies use one.

    WHY THESE PROTOCOLS EXIST, AND IT IS NOT A STYLE PREFERENCE. Most functions
    handed a Console only `say()` a line and `check()` an assertion. Declaring
    `Console` for those OVER-PROMISES: it says "I may banner, step, summarize and
    read your stopwatch" when the body does none of it, and a reader has to read
    the body to find out which. The cost showed up as 19 pyright errors on
    2026-10-09 -- every one of them a test recorder that implements exactly the
    methods its caller uses being refused by a signature naming a class with six.

    The fix is NOT to make those recorders subclass Console. Their whole value is
    failing loudly the day a production function reaches for something new;
    inheriting would have the shipped Console answer instead, silently, and
    `_QuietConsole` -- whose entire contract is "says nothing" -- would inherit
    three methods that print.

    WHY THIS IS SPLIT FROM StepReporter RATHER THAN ONE PROTOCOL WITH BOTH, and it
    was MEASURED rather than reasoned: annotating xrp_balances.
    _sequence_from_the_creating_tx -- which only ever says lines -- as the
    two-method protocol turned 19 pyright errors into 7 NEW ones, because
    tests/test_xrp_balances.py's `_CountingConsole` implements `say` and nothing
    else. A Protocol that promises more than its caller uses is the same defect
    this file is fixing, one level down, and it fails the same way.

    THERE IS NO THIRD PROTOCOL FOR `check` ALONE even though two functions in
    atomic_swap_xrp.py use only that one (`_pinned_chain_amount`,
    `_rated_chain_amount`). Both are reached through resolve_chain_amount(), which
    hands the SAME console to a sibling that needs both, so the narrower type
    could never be the parameter there -- and no stub in the tree implements
    `check` without `say`, so the name would have no reader. Split a Protocol when
    a caller needs less AND something can supply less; a split nothing can use is
    surface for its own sake.

    POSITIONAL-ONLY (`/`) IS LOAD-BEARING AND WAS MEASURED, not chosen for taste.
    pyright matches parameter NAMES for a normal parameter, so a recorder written
    `def say(self, *_args, **_kwargs)` does not satisfy `say(self, text: str)`, and
    a recorder that names the parameter `line` instead of `text` does not either.
    Both spellings exist in tests/ today. `/` is also the TRUER claim: every
    production call site in this tree passes these positionally.
    """

    def say(self, text: str, /) -> None: ...


class StepReporter(StepNarrator, Protocol):
    """Say a line and check an assertion. What most helpers handed a console use.

    See StepNarrator above for why these are protocols at all, why a stub must
    never subclass Console, and why the parameters are positional-only.

    `ok: bool` HERE AGAINST `ok: object` ON Console.check, DELIBERATELY. The
    concrete method takes `object` so it can REFUSE a non-bool at runtime -- see
    its docstring for the regtest/console.py collision that earns the refusal --
    and a parameter type is contravariant, so a method accepting `object` already
    satisfies a protocol promising only `bool`. Verified against pyright 1.1.414
    rather than assumed. The Protocol states what an honest caller passes; the
    implementation states what it will physically accept and then rejects the
    rest. Loosening this to `object` would hand every caller written against the
    Protocol permission to pass the truthy FAIL string the guard exists to catch.

    NAMED `StepReporter` AND THERE IS NO SECOND NAME FOR IT. Two independent
    designs of this protocol were written the same day, one called `StepReporter`
    and one `StepConsole`, with identical members. Landing both would be rule 8's
    defect created on purpose -- two copies of one rule, agreeing on the day they
    are written and drifting from then on. One survives and owns the concept; if
    you arrive here holding the name `StepConsole`, this is the thing you meant.
    """

    # `ok` IS NOT POSITIONAL-ONLY AND THE OTHER THREE ARE. Measured 2026-10-09 after
    # the all-positional version refused four PRODUCTION call sites:
    # atomic_swap_xrp.py:974, 1118, 1124, 1346 and 1352 pass `ok=False` as a
    # KEYWORD, which reads better at a site whose whole point is that the check
    # fails. And nothing is lost by allowing it: every stub in tests/ that names
    # this parameter at all names it `ok` (grepped -- the rest take *args), so the
    # name-matching hazard that makes `say(text, /)` positional-only does not exist
    # here. The first three stay positional-only because every call passes them so.
    def check(self, label: str, got: object, expected: object, /, ok: bool) -> bool: ...


class StepSession(StepReporter, Protocol):
    """The whole surface a top-level RUNNER drives, as against the two a helper needs.

    THE THIRD RUNG, and it exists for the same reason as the first two: a signature
    naming the concrete `Console` is nominal, so a recorder implementing exactly
    what the function calls is refused on its NAME. StepNarrator is one method
    (say), StepReporter is two (+ check), and this is five -- the set a runner that
    owns a whole swap uses: it banners its sections, numbers its steps, says lines,
    checks assertions, and summarizes.

    MEASURED RATHER THAN LISTED FROM THE CLASS: grepped `console.<member>` across
    atomic_swap_xrp.py's two runners, 2026-10-09 -- say 31, check 19, step 10,
    banner 2, summary 1. Nothing reads `_elapsed`, `results`, `total_steps` or
    `started`, which is the four members declaring `Console` was promising on their
    behalf.

    WHY IT STOPS HERE AND DOES NOT BECOME "everything Console has". The point of
    every rung is that something OTHER than Console can satisfy it. A protocol that
    mirrored the class would be satisfiable only by the class, which is the nominal
    typing it replaces wearing a structural costume.

    FOUND BY A TEST, which is worth recording because it is the argument for the
    whole ladder. tests/test_swap_runners_report_completion.py drives both runners
    with a recorder implementing these five and nothing else; against
    `console: Console` it was refused, and the refusal was about the recorder's
    ancestry rather than anything it could not do.
    """

    def banner(self, text: str, /) -> None: ...
    def step(self, number: int, title: str, /) -> None: ...
    def summary(self) -> int: ...
    def elapsed(self) -> str: ...


class Console:
    """Announce before acting, print what was expected beside what arrived.

    See this module's header for why it is shared and why regtest/console.py is
    not merged into it.

    It satisfies StepReporter above, which is what most of its callers should
    declare: a function that only says lines and checks assertions has no business
    naming a class that also owns the step count and the stopwatch.
    """

    def __init__(self, total_steps: int = 9) -> None:
        self.total_steps = total_steps
        self.started = time.monotonic()
        self.results: list[tuple[str, bool]] = []

    # PUBLIC, AND IT WAS `_elapsed` UNTIL 2026-10-09. Two things were wrong with the
    # underscore. xrp_htlc_escrow.wait_validated() called `console._elapsed()` from
    # another module -- a private member of another class, reached across a file
    # boundary, which no annotation could ever have described honestly. And
    # regtest/console.Console exposes the identical concept as a PUBLIC `elapsed()`,
    # so one idea had two visibilities in two classes with the same name (rule 8).
    # Three call sites in total, two of them inside this class.
    def elapsed(self) -> str:
        return format_duration(time.monotonic() - self.started)

    def banner(self, text: str) -> None:
        print("\n" + "=" * 78 + f"\n{text}\n" + "=" * 78, flush=True)

    def step(self, number: int, title: str) -> None:
        print(f"\nstep {number}/{self.total_steps}  {title}   [{self.elapsed()}]", flush=True)

    def say(self, text: str) -> None:
        print(f"          {text}", flush=True)

    def check(self, label: str, got: object, expected: object, ok: object) -> bool:
        """Print one assertion, record it, and return the verdict.

        `ok: object` RATHER THAN `ok: bool`, AND THE REFUSAL BELOW IS WHY. See this
        module's header: regtest/console.py's Console has the same name and the same
        method with a verdict STRING in this position, every one of which is truthy,
        so crossing the two turns a FAIL into a printed OK and an exit code of 0.
        Annotating `bool` did not stop that and could not -- nothing type-checks the
        82 call sites at runtime, and the crossing arrives by copy-paste rather than
        by anyone writing a wrong type on purpose.

        `is not True and is not False` rather than isinstance, because
        `isinstance(1, bool)` is False but `isinstance(True, int)` is True and the
        asymmetry invites exactly one more wrong assumption. Identity against the two
        singletons says what is meant: this takes a verdict, not a truthy value.
        """
        if ok is not True and ok is not False:
            raise TypeError(
                f"check({label!r}) was given ok={ok!r} ({type(ok).__name__}), which is not a bool. "
                f"If that came from regtest/console.py's OK/FAIL/SKIP/XFAIL, those are STRINGS and "
                f"every one of them is truthy -- passing FAIL here would have printed OK and exited 0. "
                f"That module's Console is a different class with the same name; this one wants True "
                f"or False."
            )
        shown = got if got not in (None, "", [], {}) else "(none)"
        print(f"          {'OK  ' if ok else 'FAIL'}  {label}: got={shown}  expected={expected}  "
              f"[{self.elapsed()}]", flush=True)
        self.results.append((label, ok))
        return ok

    def summary(self) -> int:
        self.banner("SUMMARY")
        failures = [label for label, ok in self.results if not ok]
        print(f"  OK={len(self.results) - len(failures)}  FAIL={len(failures)}", flush=True)
        if failures:
            print("\n  unexpected failures, in the order they happened:", flush=True)
            for label in failures:
                print(f"    - {label}", flush=True)
        else:
            print("\n  unexpected failures: (none)", flush=True)
        return 1 if failures else 0
