#!/usr/bin/env python3
"""The suite baseline: diff two runs test-by-test, never by comparing totals.

Role: tests (offline; no pytest subprocess is spawned from here)
Reads: suite_baseline.py
Writes: tmp_path only
Can move funds: no
Mainnet-safe: yes
Live-safe: yes

compare() IS THE DECISION and every test here calls it with seeded mappings, which
is the whole reason it takes two dicts instead of running pytest itself. Nothing in
this file runs the suite -- a test that ran the suite to test the suite-runner would
take four minutes and prove less.

WHY THE TOOL EXISTS, since the tests are the place that survives: CLAUDE.md has
asked for it from the beginning -- "diff the full suite line-by-line against a
recorded baseline rather than comparing failure counts. Counting failures hides a
new break that lands the same day an old one is fixed." Measured 2026-10-09: no
baseline file anywhere in the tree and no tool that read a junit report or
--collect-only. Three commits that day stated a test count nobody had counted, the
third while claiming --collect-only had been used. None changed a line of code,
which is the only reason it is a footnote; the same habit applied to "the suite is
green" is the sentence above, exactly.
"""

from __future__ import annotations

import pytest
from conftest import root_entry_point

suite_baseline = root_entry_point("suite_baseline.py")

PASSED = suite_baseline.PASSED
compare = suite_baseline.compare


def test_a_fix_and_a_break_in_one_run_are_BOTH_reported():
    """THE SENTENCE FROM CLAUDE.md, as a test. A total nets these to zero.

    This is the entire reason the tool exists, so if only one test in this file
    survives it should be this one.
    """
    baseline = {"a::x": PASSED, "a::y": "failed"}
    current = {"a::x": "failed", "a::y": PASSED}
    diff = compare(baseline, current)
    assert diff.regressed == ("a::x",)
    assert diff.fixed == ("a::y",)
    assert diff.baseline_total == diff.current_total == 2, (
        "the totals are IDENTICAL, which is what a failure count would have reported"
    )
    assert diff.is_regression


def test_a_test_that_DISAPPEARED_is_a_regression():
    """Deleting a failing test makes a count go green. So does losing one in a merge.

    Rule 2: a test dies with the code it pins or changes to pin a stronger
    invariant -- so a disappearance is either accompanied by that deletion in the
    same commit, which shows in the baseline file's own diff, or it is the defect.
    """
    diff = compare({"a::x": PASSED, "a::gone": "failed"}, {"a::x": PASSED})
    assert diff.disappeared == ("a::gone",)
    assert diff.is_regression, "a failing test vanishing must not read as an improvement"


def test_a_test_newly_SKIPPED_is_a_regression():
    """A skip is not a pass, and the totals line prints skips where nobody looks."""
    diff = compare({"a::x": PASSED}, {"a::x": "skipped"})
    assert diff.newly_skipped == ("a::x",)
    assert diff.is_regression


def test_a_test_that_was_ALREADY_skipped_is_not_a_regression():
    """Three skips have been in this suite the whole time; they are not news."""
    diff = compare({"a::x": "skipped"}, {"a::x": "skipped"})
    assert diff.newly_skipped == ()
    assert not diff.is_regression


def test_NEW_tests_are_not_a_failure_and_are_counted_separately():
    """This is the count I kept getting wrong by hand, which is why it is reported."""
    diff = compare({"a::x": PASSED}, {"a::x": PASSED, "a::new1": PASSED, "a::new2": PASSED})
    assert diff.added == ("a::new1", "a::new2")
    assert not diff.is_regression
    assert diff.current_total - diff.baseline_total == 2


def test_a_NEW_test_that_fails_is_a_regression_even_though_it_is_new():
    """`added` is innocent; a new test that FAILS is not.

    MUTATION CHECKED: folding newly_failing into `added` -- which is tempting since
    both are new node ids -- lets a broken new test land green.
    """
    diff = compare({"a::x": PASSED}, {"a::x": PASSED, "a::new": "failed"})
    assert diff.newly_failing == ("a::new",)
    assert diff.is_regression


def test_an_ERROR_is_kept_distinct_from_a_FAILURE():
    """A junit <error> is usually a whole file that did not run.

    Reporting that as one failed test understates it badly, so the outcome word is
    preserved even though both are "broken" for the verdict.
    """
    diff = compare({"a::x": PASSED}, {"a::x": "errored"})
    assert diff.regressed == ("a::x",)
    assert diff.is_regression


def test_a_test_broken_in_BOTH_runs_is_reported_but_is_not_a_NEW_regression():
    """still_broken exists so a known failure does not read as today's work.

    It is NOT silenced either -- it prints every run, which is the difference
    between this file and a ratchet (rule 19): nothing is excused by being recorded.
    """
    diff = compare({"a::x": "failed"}, {"a::x": "failed"})
    assert diff.still_broken == ("a::x",)
    assert diff.regressed == ()
    assert not diff.is_regression


def test_an_EMPTY_baseline_says_so_and_cannot_read_as_an_all_clear():
    """`check` on a fresh clone is legitimate; the answer is "nothing to compare".

    Rule 14: an empty result must not print nothing. Everything reads as `added`
    here, which looks like a clean run unless the note says why.
    """
    diff = compare({}, {"a::x": PASSED})
    assert diff.notes, "an empty baseline must be stated, not inferred from zero regressions"
    assert "EMPTY" in diff.notes[0]
    assert not diff.is_regression
    assert diff.added == ("a::x",)


def test_two_identical_runs_report_nothing_at_all():
    diff = compare({"a::x": PASSED}, {"a::x": PASSED})
    assert not diff.is_regression
    assert diff.regressed == diff.added == diff.fixed == diff.disappeared == ()


@pytest.mark.parametrize(
    ("group", "why"),
    [
        ("added", "new tests are the point of counting, not a failure"),
        ("fixed", "a fix must never fail a check"),
        ("still_broken", "a known failure is reported, not re-raised as new"),
    ],
)
def test_the_three_innocent_groups_never_set_the_verdict(group, why):
    """Asserted per group so adding one to is_regression by accident fails loudly."""
    diff = compare({"a::x": "failed"}, {"a::x": "failed", "a::new": PASSED})
    assert getattr(diff, group) is not None
    assert not diff.is_regression, why


def test_the_junit_parser_reads_each_outcome_including_a_parametrized_case():
    """One row per CASE, which is the granularity three wrong commit messages needed."""
    xml = """<?xml version="1.0"?>
    <testsuites><testsuite name="pytest" tests="5">
      <testcase classname="tests.test_a" name="test_ok"/>
      <testcase classname="tests.test_a" name="test_param[a-b]"/>
      <testcase classname="tests.test_a" name="test_bad"><failure message="boom">tb</failure></testcase>
      <testcase classname="tests.test_a" name="test_err"><error message="fixture">tb</error></testcase>
      <testcase classname="tests.test_a" name="test_skip"><skipped message="why"/></testcase>
    </testsuite></testsuites>"""
    outcomes = suite_baseline.outcomes_from_junit(xml)
    assert outcomes == {
        "tests.test_a::test_ok": PASSED,
        "tests.test_a::test_param[a-b]": PASSED,
        "tests.test_a::test_bad": "failed",
        "tests.test_a::test_err": "errored",
        "tests.test_a::test_skip": "skipped",
    }


def test_a_testcase_with_captured_output_beside_a_failure_still_reads_as_failed():
    """Several children are possible, so the element COUNT must not be trusted."""
    xml = """<?xml version="1.0"?>
    <testsuites><testsuite name="pytest">
      <testcase classname="tests.test_b" name="test_x">
        <failure message="boom">tb</failure>
        <system-out>stdout noise</system-out>
      </testcase>
      <testcase classname="tests.test_b" name="test_y">
        <system-out>stdout noise only -- this one PASSED</system-out>
      </testcase>
    </testsuite></testsuites>"""
    outcomes = suite_baseline.outcomes_from_junit(xml)
    assert outcomes["tests.test_b::test_x"] == "failed"
    assert outcomes["tests.test_b::test_y"] == PASSED, (
        "a child element that is not an outcome must not be read as one"
    )


def test_the_baseline_file_round_trips_and_is_sorted(tmp_path):
    """Sorted and tab-separated so `git diff` of it reads as a list of changed tests."""
    path = tmp_path / "suite_baseline.tsv"
    outcomes = {"z::c": PASSED, "a::b": "failed", "m::p[x,y]": "skipped"}
    suite_baseline.write_baseline(path, outcomes)
    assert suite_baseline.read_baseline(path) == outcomes
    body = [line for line in path.read_text().splitlines() if not line.startswith("#")]
    assert body == sorted(body), "unsorted would show the whole file as changed on rewrite"
    assert any("m::p[x,y]" in line for line in body), (
        "a parametrized id contains a comma, which is why this is TSV and not CSV"
    )


def test_a_missing_baseline_file_reads_as_empty_rather_than_raising(tmp_path):
    """A traceback out of a report tells the operator nothing (rule 14)."""
    assert suite_baseline.read_baseline(tmp_path / "nope.tsv") == {}


def test_the_recorded_file_says_it_is_not_a_ratchet(tmp_path):
    """Rule 19 forbids a baseline that absorbs a backlog. This one must say it is not.

    The distinction is real and has to be IN the file, because the next reader finds
    the file before they find the tool: nothing is excused by appearing here, and a
    regression cannot be silenced by adding a line -- recording rewrites the whole
    file from a real run, so the regression shows up in this file's own diff.
    """
    path = tmp_path / "suite_baseline.tsv"
    suite_baseline.write_baseline(path, {"a::x": PASSED})
    header = path.read_text()
    assert "not a ratchet" in header.lower()
    assert "never edited by hand" in header.lower()


# ---------------------------------------------------------------------------
# A NARROWED RUN. The first version of this tool was unusable for the case it is
# most needed in: its own --help said a narrowed run was fine "only with
# `check`", and a `check tests/test_one.py` then compared 31 collected tests
# against 4270 recorded ones and called the other 4239 `disappeared`. A tool that
# cries wolf 4239 times is a tool nobody runs, and a tool nobody runs is rule
# 19's patch rather than its fix.
# ---------------------------------------------------------------------------


def test_a_narrowed_run_puts_other_files_OUT_OF_SCOPE_rather_than_missing():
    """MUTATION CHECKED: dropping `limited` makes this one file report 2 disappeared."""
    baseline = {"a::x": PASSED, "a::y": PASSED, "b::p": PASSED, "b::q": PASSED}
    current = {"a::x": PASSED, "a::y": PASSED}
    diff = compare(baseline, current, limited=True)
    assert diff.disappeared == (), "file b was not run, so its tests are not missing"
    assert not diff.is_regression
    assert any("OUT OF SCOPE" in note for note in diff.notes)


def test_a_narrowed_run_STILL_catches_a_test_deleted_from_a_file_it_DID_run():
    """The scoping must not become a blanket excuse -- that would make it useless.

    This is the half that keeps `limited` honest: narrowing the scope to the files
    that ran is not the same as ignoring deletions.
    """
    baseline = {"a::x": PASSED, "a::gone": PASSED, "b::p": PASSED}
    current = {"a::x": PASSED}
    diff = compare(baseline, current, limited=True)
    assert diff.disappeared == ("a::gone",), "a deletion inside a file that ran is still caught"
    assert diff.is_regression


def test_a_FULL_run_treats_every_absence_as_disappeared():
    """The default, and the only mode that can see a whole file deleted or renamed."""
    baseline = {"a::x": PASSED, "b::p": PASSED}
    diff = compare(baseline, {"a::x": PASSED})
    assert diff.disappeared == ("b::p",)
    assert diff.is_regression
    assert not any("NARROWED" in note for note in diff.notes)


def test_the_narrowed_note_says_what_it_CANNOT_see():
    """Rule 17 one level up: a scoped check must state the limit of its own scope.

    Saying "4239 out of scope" without "a whole file deleted cannot be seen from
    here" would leave a reader thinking a green narrowed check meant more than it does.
    """
    diff = compare({"a::x": PASSED, "b::p": PASSED}, {"a::x": PASSED}, limited=True)
    note = " ".join(diff.notes)
    assert "whole file" in note
    assert "Run with no pytest arguments" in note, "and it must say how to get the full check"


def test_the_module_part_of_a_node_id_survives_parameters():
    """Scoping keys off the file, and a parametrized id carries :: and [] and commas."""
    assert suite_baseline._module_of("tests.test_a::test_x[a,b]") == "tests.test_a"
    assert suite_baseline._module_of("tests.test_a") == "tests.test_a"
    assert suite_baseline._module_of("") == ""
