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

from microfortnights import format_duration


class Console:
    """Announce before acting, print what was expected beside what arrived.

    See this module's header for why it is shared and why regtest/console.py is
    not merged into it.
    """

    def __init__(self, total_steps: int = 9) -> None:
        self.total_steps = total_steps
        self.started = time.monotonic()
        self.results: list[tuple[str, bool]] = []

    def _elapsed(self) -> str:
        return format_duration(time.monotonic() - self.started)

    def banner(self, text: str) -> None:
        print("\n" + "=" * 78 + f"\n{text}\n" + "=" * 78, flush=True)

    def step(self, number: int, title: str) -> None:
        print(f"\nstep {number}/{self.total_steps}  {title}   [{self._elapsed()}]", flush=True)

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
              f"[{self._elapsed()}]", flush=True)
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
