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

    def check(self, label: str, got: object, expected: object, ok: bool) -> bool:
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
