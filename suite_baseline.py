#!/usr/bin/env python3
"""Record the full test suite's per-test outcome, and diff a fresh run against it.

Role: file (an entry point, at the project root -- rule 10)
Reads: the test suite, by running it; tests/suite_baseline.tsv when checking
Writes: tests/suite_baseline.tsv on `record`, and a junit report into a temp dir
Can move funds: no. It runs pytest and compares two lists of strings. It opens no
        socket, imports no chain adapter and touches no wallet.
Mainnet-safe: yes -- there is no network here at all.
Live-safe: yes. It runs the offline suite; nothing it does reaches a daemon.

WHY THIS EXISTS. CLAUDE.md has asked for it in so many words from the beginning:

    Test before shipping, and diff the full suite line-by-line against a recorded
    baseline rather than comparing failure counts. Counting failures hides a new
    break that lands the same day an old one is fixed.

The instruction was there and the MECHANISM was not. Measured 2026-10-09: no
baseline file anywhere in the tree, and no tool that reads `--collect-only` or a
junit report. So every "the suite is green at 4232" in this repository's history
was a number somebody read off a terminal and retyped, which is exactly the
practice the paragraph above forbids.

WHAT THAT COST, and it is small and it is mine, which is the argument for fixing
the cause rather than resolving to be careful. Two commits in a row on
2026-10-09 stated a test count in their message that nobody had counted:

    95e5775  "16 tests"          real: 14 functions / 24 cases
    018bc26  "19 new cases"      real: 11 cases (6 + 5)

018bc26 is the commit about a report INVENTING A FACT, and its own message
invented one. Neither number changed a line of code, which is the only reason
this is a footnote rather than an incident -- the identical habit applied to "the
suite is green" is how a new break lands the same day an old one is fixed and
the total stays put. Rule 19's test for a patch: does it stop the symptom being
reported, or stop the cause existing? Resolving to count more carefully is the
first. A recorded baseline is the second.

WHY A BASELINE FILE HERE IS NOT THE THING RULE 19 FORBIDS. Rule 19 is about a
per-file ratchet that a hygiene test compares against, so a NEW violation fails
while an existing backlog is tolerated -- "a baseline makes the check green,
green looks like done, and the backlog stops being visible as work." This file
holds no violations and tolerates nothing. It is a record of what the suite DID,
for diffing against what it does now, and CLAUDE.md asks for it by name. The
distinction that matters: nothing is ever excused by appearing in it, and a
regression cannot be silenced by adding a line -- `record` rewrites the whole
file from a real run, so a regression recorded as the new baseline is visible in
the diff of this file in the same commit.

THE DECISION IS compare(), WHICH IS PURE (rule 10). It takes two mappings of
node id -> outcome and returns a SuiteDiff. Every category it reports exists
because a bare pass/fail count hides it:

  regressed       passed before, fails now. The one everybody looks for.
  newly_failing   did not exist before and fails now. A bare total can net this
                  against a fix elsewhere and read as no change at all.
  disappeared     was in the baseline, is not in this run. Loudest category in
                  practice: deleting a failing test makes a count go green, and
                  so does deleting a passing one by accident in a bad merge.
  newly_skipped   passed before, is skipped now. A skip is not a pass, and the
                  totals line prints skips separately where a reader may not look.
  added           new node ids. NOT a failure. This is the count I kept getting
                  wrong by hand, and the only reason it is reported at all.
  fixed           failed before, passes now. Reported so a run that fixes one
                  thing and breaks another cannot read as either alone.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from xml.etree import ElementTree

REPO_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO_ROOT / "swap_terminal"))

from microfortnights import format_duration  # noqa: E402 -- after the sys.path.insert, same as every root tool

#: Where the recorded outcomes live. Beside the tests rather than at the root,
#: because it is an artifact OF the suite and not an entry point.
BASELINE_PATH = REPO_ROOT / "tests" / "suite_baseline.tsv"

#: TAB separated and SORTED, so a `git diff` of this file reads as a list of
#: changed tests. A JSON object would reorder on rewrite and show the whole file
#: as changed; a CSV would need quoting, because a parametrized node id can
#: contain a comma (`test_x[a,b]`) and routinely does. A tab cannot appear in a
#: pytest node id, which is what makes it safe as the separator here.
_SEPARATOR = "\t"

#: The outcomes a junit report can carry, mapped to the one word this tool uses.
#: A junit <testcase> with no child element is a pass; the child's tag names the
#: rest. `error` is kept DISTINCT from `failure` rather than folded into it: an
#: error is a collection or fixture failure, which usually means a whole file did
#: not run, and reporting that as one failed test understates it badly.
_OUTCOME_BY_TAG = {
    "failure": "failed",
    "error": "errored",
    "skipped": "skipped",
}
PASSED = "passed"

#: Which outcomes count as not-working. Named rather than tested inline, because
#: three functions below ask the same question and a fourth spelling is rule 8's
#: shape (two copies of one rule is a bug with a delay on it).
BROKEN = frozenset({"failed", "errored"})


@dataclass(frozen=True)
class SuiteDiff:
    """Every way two runs can differ, each kept separate. Counts are len() of these."""

    regressed: tuple[str, ...] = ()
    newly_failing: tuple[str, ...] = ()
    disappeared: tuple[str, ...] = ()
    newly_skipped: tuple[str, ...] = ()
    added: tuple[str, ...] = ()
    fixed: tuple[str, ...] = ()
    still_broken: tuple[str, ...] = ()
    baseline_total: int = 0
    current_total: int = 0
    notes: tuple[str, ...] = field(default=())

    @property
    def is_regression(self) -> bool:
        """Did anything get WORSE? `added` and `fixed` are never failures.

        `disappeared` IS one, and that is the deliberate part. A test that is
        gone cannot be asserted on, and the two ways it goes missing -- deleted
        on purpose, or lost in a merge -- look identical from here. Rule 2 says
        a test dies with the code it pins or changes to pin a stronger
        invariant, so a disappearance is either accompanied by that deletion in
        the same commit (re-`record` and the diff of the baseline file shows it)
        or it is the defect.
        """
        return bool(self.regressed or self.newly_failing or self.disappeared or self.newly_skipped)


def compare(baseline: dict[str, str], current: dict[str, str]) -> SuiteDiff:
    """Diff two node-id -> outcome mappings. Pure; no pytest, no filesystem.

    THE WHOLE POINT IS THAT NOTHING IS NETTED. A run that fixes one test and
    breaks another reports one `fixed` and one `regressed`, where a total reports
    no change -- which is the sentence in CLAUDE.md this file exists to honor.
    """
    regressed = []
    newly_failing = []
    newly_skipped = []
    fixed = []
    still_broken = []
    for node, outcome in current.items():
        was = baseline.get(node)
        if was is None:
            if outcome in BROKEN:
                newly_failing.append(node)
            continue
        if outcome in BROKEN:
            (still_broken if was in BROKEN else regressed).append(node)
        elif outcome == "skipped" and was == PASSED:
            newly_skipped.append(node)
        elif was in BROKEN:
            fixed.append(node)
    notes = []
    if not baseline:
        notes.append(
            "the baseline is EMPTY, so every test here reads as `added` and nothing can be "
            "a regression. That is not an all-clear: run `record` first."
        )
    return SuiteDiff(
        regressed=tuple(sorted(regressed)),
        newly_failing=tuple(sorted(newly_failing)),
        disappeared=tuple(sorted(set(baseline) - set(current))),
        newly_skipped=tuple(sorted(newly_skipped)),
        added=tuple(sorted(set(current) - set(baseline))),
        fixed=tuple(sorted(fixed)),
        still_broken=tuple(sorted(still_broken)),
        baseline_total=len(baseline),
        current_total=len(current),
        notes=tuple(notes),
    )


def outcomes_from_junit(xml_text: str) -> dict[str, str]:
    """Parse a junit report into node id -> outcome. The only parsing in this file.

    NODE ID IS classname::name, which is what pytest itself prints and what a
    reader can paste straight back into a `pytest` invocation. A parametrized
    case carries its parameters in `name` (`test_x[a-b]`), so each case is its
    own row -- which is the granularity the two miscounted commit messages
    needed and did not have.

    A <testcase> with no child element PASSED. Several children are possible --
    a failure plus a captured-output element -- so the first recognized tag
    wins and the rest are ignored rather than the element count being trusted.
    """
    root = ElementTree.fromstring(xml_text)  # noqa: S314 -- checked: the input is a junit report this file just told pytest to write, into a temp dir it created. It is not attacker-controlled and never leaves this process. defusedxml is not a dependency of this repo and adding one for a file that parses its own output would be the larger change.
    outcomes = {}
    for case in root.iter("testcase"):
        node = f"{case.get('classname', '')}::{case.get('name', '')}"
        outcome = PASSED
        for child in case:
            if child.tag in _OUTCOME_BY_TAG:
                outcome = _OUTCOME_BY_TAG[child.tag]
                break
        outcomes[node] = outcome
    return outcomes


def read_baseline(path: Path) -> dict[str, str]:
    """Load the recorded outcomes. A missing file is {} and the caller SAYS so.

    Not an exception, because `check` on a fresh clone is a legitimate thing to
    do and the right answer is "there is nothing to compare against" rather than
    a traceback. compare() puts that in SuiteDiff.notes so it reaches the screen.
    """
    if not path.exists():
        return {}
    outcomes = {}
    for line in path.read_text().splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        node, _, outcome = line.partition(_SEPARATOR)
        if node:
            outcomes[node] = outcome or PASSED
    return outcomes


def write_baseline(path: Path, outcomes: dict[str, str]) -> None:
    """Sorted, one test per line, with a header naming what the file is for."""
    body = "\n".join(f"{node}{_SEPARATOR}{outcome}" for node, outcome in sorted(outcomes.items()))
    path.write_text(
        "# Recorded per-test outcomes for the whole suite. Rewritten in full by\n"
        "# `python3 suite_baseline.py record`; never edited by hand, because a\n"
        "# hand-edited line is a claim no run supports.\n"
        "#\n"
        "# This is NOT a ratchet (rule 19): nothing is excused by appearing here,\n"
        "# and a regression cannot be silenced by adding a line -- re-recording\n"
        "# rewrites the file, so the regression shows up in THIS file's diff.\n"
        f"{body}\n"
    )


def run_suite(report: Path, pytest_args: tuple[str, ...]) -> tuple[int, float]:
    """Run the suite, writing a junit report. Returns (exit code, elapsed seconds).

    ANNOUNCED BEFORE THE WAIT, NOT AFTER (rule 14). The suite takes over two
    minutes on this tree, and a line that only appears on completion is
    invisible during exactly the period somebody is deciding whether it hung.
    """
    command = [sys.executable, "-m", "pytest", f"--junitxml={report}", *pytest_args]
    print(f"  running           {' '.join(command[1:])}", flush=True)
    print("  from              " + str(REPO_ROOT), flush=True)
    print(
        "  expect            a couple of minutes with no output; pytest's own progress is\n"
        "                    suppressed by the -q in pyproject.toml's addopts",
        flush=True,
    )
    started = time.monotonic()
    completed = subprocess.run(command, cwd=REPO_ROOT, check=False)  # noqa: S603 -- checked: argv is built from sys.executable and this file's own constants plus argv the operator passed to THIS tool. No shell, and nothing here is interpolated into a string.
    elapsed = time.monotonic() - started
    print(f"  finished          exit {completed.returncode} in {format_duration(elapsed)}", flush=True)
    return completed.returncode, elapsed


def _say_group(label: str, nodes: tuple[str, ...], *, limit: int = 25) -> None:
    """One group of node ids. NEVER prints nothing (rule 14): zero is `(none)`.

    A blank gap is ambiguous between "no tests in this category" and "the query
    broke", and this tool's whole job is to be read rather than re-derived.
    """
    print(f"  {label:<16}  {len(nodes)}")
    if not nodes:
        print("                    (none)")
        return
    for node in nodes[:limit]:
        print(f"                    {node}")
    if len(nodes) > limit:
        print(f"                    ... and {len(nodes) - limit} more of the {len(nodes)}")


def say_diff(diff: SuiteDiff) -> None:
    """Print the comparison. States what each number MEANS, next to the number."""
    print()
    print(f"  baseline          {diff.baseline_total} tests recorded in {BASELINE_PATH.name}")
    print(f"  this run          {diff.current_total} tests collected")
    print(
        f"  net              {diff.current_total - diff.baseline_total:+d}  <- a NET of zero can "
        f"still hide a break; the groups below are why"
    )
    for note in diff.notes:
        print(f"  NOTE              {note}")
    print()
    print("  WORSE -- any of these is a failure and sets the exit code:")
    _say_group("regressed", diff.regressed)
    _say_group("newly failing", diff.newly_failing)
    _say_group("disappeared", diff.disappeared)
    _say_group("newly skipped", diff.newly_skipped)
    print()
    print("  NOT failures, reported so a fix cannot cancel out a break in the totals:")
    _say_group("added", diff.added)
    _say_group("fixed", diff.fixed)
    _say_group("still broken", diff.still_broken)


def cmd_record(pytest_args: tuple[str, ...]) -> int:
    """Run the suite and write what it did. Refuses to record a broken run silently."""
    print("suite_baseline: RECORD")
    print(f"  writing           {BASELINE_PATH}")
    print("  note              this REPLACES the file from a real run; it is not appended to")
    with tempfile.TemporaryDirectory() as tmp:
        report = Path(tmp) / "junit.xml"
        code, _elapsed = run_suite(report, pytest_args)
        if not report.exists():
            print("  REFUSED           pytest wrote no junit report, so there is nothing to record.")
            print("                    Nothing was written. The exit code above is pytest's.")
            return code or 1
        outcomes = outcomes_from_junit(report.read_text())
    broken = sorted(node for node, outcome in outcomes.items() if outcome in BROKEN)
    write_baseline(BASELINE_PATH, outcomes)
    print(f"  recorded          {len(outcomes)} tests")
    _say_group("broken at record", tuple(broken))
    if broken:
        print("                    ^ these are recorded AS broken, which is honest and is not")
        print("                      an excuse: `check` will report them as `still broken`")
        print("                      every run until they are fixed.")
    return 0


def cmd_check(pytest_args: tuple[str, ...]) -> int:
    """Run the suite and diff it against the record. Exit 1 on anything worse."""
    print("suite_baseline: CHECK")
    print(f"  comparing against {BASELINE_PATH}")
    baseline = read_baseline(BASELINE_PATH)
    print(f"  baseline holds    {len(baseline)} tests" if baseline else "  baseline holds    (nothing)")
    with tempfile.TemporaryDirectory() as tmp:
        report = Path(tmp) / "junit.xml"
        code, _elapsed = run_suite(report, pytest_args)
        if not report.exists():
            print("  COULD NOT ASK     pytest wrote no junit report. This says NOTHING about the")
            print("                    suite -- it says the run did not happen. Not an all-clear.")
            return code or 1
        current = outcomes_from_junit(report.read_text())
    diff = compare(baseline, current)
    say_diff(diff)
    print()
    if diff.is_regression:
        print("  exit 1            SOMETHING IS WORSE than the recorded baseline -- see the groups")
        print("                    above for which. `added` and `fixed` alone would have exited 0.")
        return 1
    print("  exit 0            nothing is worse than the baseline. `added` and `fixed` do not")
    print("                    fail a check; re-run `record` to adopt this run as the new one.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Record the suite's per-test outcomes, or diff a fresh run against the record. "
            "CLAUDE.md: diff line-by-line, never by comparing failure counts."
        )
    )
    parser.add_argument("action", choices=("record", "check"))
    parser.add_argument(
        "pytest_args",
        nargs="*",
        help="passed through to pytest (e.g. a path to limit the run). Limiting the run makes "
        "`record` write a PARTIAL baseline, so do it only with `check`.",
    )
    namespace = parser.parse_args(argv)
    extra = tuple(namespace.pytest_args)
    if namespace.action == "record":
        return cmd_record(extra)
    return cmd_check(extra)


if __name__ == "__main__":
    raise SystemExit(main())
