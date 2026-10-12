#!/usr/bin/env python3
"""A suite count in HEAD's commit message must match the baseline HEAD committed.

Role: tests (read-only)
Reads: `git log -1 --format=%B` and tests/suite_baseline.tsv
Writes: nothing
Can move funds: no
Mainnet-safe: yes

=========================================================================
FIVE COMMITS, ONE MISTAKE, AND THE TOOL THAT WAS BUILT TO STOP IT
=========================================================================

CLAUDE.md: "Test before shipping, and diff the full suite line by line against a
recorded baseline rather than comparing failure counts." OPEN_FINDINGS finding 6b
records three commit messages that "stated a test count nobody counted", and
suite_baseline.py was written to end it.

Measured 2026-10-12, all mine, all in one day:

    1ecf267   claimed 5548 passed   actual 5544 passed, 4 skipped (5548 collected)
    27d8bba   claimed 5563 passed   actual 5561 passed, 4 skipped (5565 collected)
    ffe0717   claimed 5577 passed   actual 5564 passed, 4 skipped (5568 collected)
    b9f8e1b   claimed 5758 passed   actual 5754 passed, 4 skipped (5758 collected)

The first three read the COLLECTED total and wrote it as *passed*. I then added
suite_baseline.commit_line(), which prints one labelled line with nothing left to
convert -- and b9f8e1b was wrong anyway, three hours later.

THE FOURTH FAILURE HAD A DIFFERENT CAUSE AND THAT IS THE POINT. The line was printed
correctly and I never read it, because I ran

    python3 suite_baseline.py record && git commit -F - <<'MSG' ... MSG

as ONE shell command: the heredoc was composed before `record` produced the line it was
supposed to quote. No printing improvement can fix that -- the output arrives after the
message exists. Rule 19's test for a patch: does it stop the symptom being reported, or
stop the cause existing? A better-formatted line stops neither, because the line was
never read.

SO THE GATE IS HERE, where it runs on every suite run regardless of what order anybody
did things in, and it would have caught all four. `tests/suite_baseline.tsv` is committed
in the same commit as the message that describes it, so HEAD's claim and HEAD's baseline
must agree exactly -- no git archaeology, no fetching, one file and one string.

WHY NOT A commit-msg HOOK, which finding 24 floated. A hook is not version-controlled
(.git/hooks is not in the tree), so it protects only a machine somebody remembered to
set it up on -- and it would have to be installed by the person it is protecting against.
This needs no installation and fails on the next run of a suite nobody can skip.
"""

from __future__ import annotations

import pathlib
import re
import subprocess

import pytest
from conftest import root_entry_point

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
BASELINE = REPO_ROOT / "suite_baseline.tsv"
BASELINE_IN_TESTS = pathlib.Path(__file__).resolve().parent / "suite_baseline.tsv"

#: The tool that WRITES the baseline, so this reads it the same way (rule 8).
suite_baseline = root_entry_point("suite_baseline.py", "suite_baseline_for_counts")

#: The shape suite_baseline.commit_line() prints, which is the only shape a commit
#: message should carry. `collected` is the capture that matters: it is exactly the
#: number of rows in the baseline, so it can be checked without running anything.
_CLAIM = re.compile(
    r"suite\s+(?P<passed>\d+)\s+passed,\s*(?P<skipped>\d+)\s+skipped"
    r"(?:,\s*(?P<errored>\d+)\s+errored)?(?:,\s*(?P<failed>\d+)\s+failed)?"
    r"\s*\((?P<collected>\d+)\s+collected\)",
    re.IGNORECASE,
)

#: A count written in any OTHER shape. Matched so that a message which states a number
#: without the collected total is reported as unverifiable rather than passing silently
#: -- the first three bad commits were all of this shape.
_LOOSE_CLAIM = re.compile(r"suite\s+(?P<passed>\d+)\s+passed", re.IGNORECASE)


def head_message() -> str:
    """HEAD's commit message, or "" when git cannot be asked.

    "" RATHER THAN A FAILURE, because a tree exported without history is a legitimate
    way to run this suite and a test that cannot be run is not a test that failed
    (rule 13 -- "skipped" and "failed" are different outputs). The skip says which.
    """
    try:
        done = subprocess.run(
            ["git", "log", "-1", "--format=%B"],
            cwd=REPO_ROOT, capture_output=True, text=True, timeout=30, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return done.stdout if done.returncode == 0 else ""


def baseline_rows() -> int:
    """How many tests the committed baseline holds, THROUGH ITS OWN PARSER.

    suite_baseline.read_baseline() skips the file's `#` header and any blank line, and
    the first version of this function counted raw lines instead -- which read 5765 for
    a file holding 5758 tests and would have made every claim fail by the size of the
    header. Rule 8 in miniature: one file format, two readers, and the one written in a
    hurry was wrong. A test that counts the rows differently from the tool that writes
    them is comparing two things neither of which is the baseline.
    """
    return len(suite_baseline.read_baseline(
        BASELINE_IN_TESTS if BASELINE_IN_TESTS.exists() else BASELINE
    ))


def test_the_baseline_is_readable_so_this_gate_measures_something():
    """A gate over a file that is not there passes forever."""
    assert baseline_rows() > 1000, (
        "the committed baseline holds almost nothing, so every claim would compare against "
        "a number that is itself wrong"
    )


def test_a_suite_count_in_HEADs_message_matches_the_baseline_HEAD_COMMITTED():
    """THE GATE. Four of my commits this day stated a count nobody counted.

    tests/suite_baseline.tsv is committed in the same commit as the message describing
    it, so the two must agree exactly. A mismatch means the message was written from
    memory or arithmetic rather than from suite_baseline.py's own line.
    """
    message = head_message()
    if not message.strip():
        pytest.skip("git history is not available here, so HEAD's message cannot be read")

    claims = list(_CLAIM.finditer(message))
    if not claims:
        return  # covered by the next test, which is about the shape rather than the value

    rows = baseline_rows()
    for claim in claims:
        collected = int(claim.group("collected"))
        passed = int(claim.group("passed"))
        skipped = int(claim.group("skipped"))
        assert collected == rows, (
            f"HEAD's message claims {collected} collected and the baseline it commits holds "
            f"{rows}. Run `python3 suite_baseline.py record` and quote the `for the commit` "
            f"line it prints -- do NOT chain record and commit in one shell command, which is "
            f"how b9f8e1b was written before the line existed."
        )
        assert passed + skipped <= collected, (
            f"HEAD's message claims {passed} passed plus {skipped} skipped out of {collected} "
            f"collected, which does not add up"
        )


def test_a_count_written_WITHOUT_the_collected_total_is_rejected():
    """The first three bad commits were all this shape: a passed count and nothing to check it against.

    `suite 5548 passed, 4 skipped, 0 failed` is unverifiable by construction -- there is
    no number in it that any file holds -- and it was wrong three times. The collected
    total is what makes a claim checkable, which is why
    suite_baseline.commit_line() always prints it.
    """
    message = head_message()
    if not message.strip():
        pytest.skip("git history is not available here, so HEAD's message cannot be read")

    loose = [match.group(0) for match in _LOOSE_CLAIM.finditer(message)]
    checkable = [match.group(0) for match in _CLAIM.finditer(message)]

    unverifiable = [claim for claim in loose if not any(claim in whole for whole in checkable)]
    assert not unverifiable, (
        f"HEAD's message states {unverifiable} with no `(N collected)`, so nothing can check "
        f"it. Quote suite_baseline.py's `for the commit` line verbatim: it carries passed, "
        f"skipped, errored, failed AND collected, and the collected total is the one this "
        f"suite can verify."
    )
