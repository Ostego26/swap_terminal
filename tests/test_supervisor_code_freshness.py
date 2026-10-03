"""Does a RUNNING worker predate the code on disk? (CLAUDE.md rule 13)

Role: test (seeded timestamps, plus real local `python3 -c "time.sleep()"` children)
Reads: swap_terminal/supervisor.py, /proc for the children it spawns itself
Writes: .py files and pid files under pytest's tmp_path
Can move funds: no
Mainnet-safe: yes -- the only process these tests ever spawn is a sleeping
        interpreter. No worker, no RPC, no chain, and nothing here signals
        anything it did not start.

WHY THIS FILE IS SEPARATE FROM tests/test_supervisor.py.

That file is the reaper's proof: it spawns, kills, and asserts the absence. This
one is about a claim the supervisor makes rather than an action it takes -- "the
process you are looking at is older than the code you just edited" -- and the
measurements it is written from are the four 2026-10-03 failures recorded above
supervisor.code_freshness().

TWO HALVES, TESTED SEPARATELY, WHICH IS THE POINT OF THE SPLIT (rule 10).

  the DECISION    code_freshness(started_at, modified_at) is a function of two
                  floats. Every verdict, including the ones that are awkward to
                  arrange in reality, is two numbers and an assert -- no process,
                  no file, no clock.
  the GATHERING   process_start_time() and newest_code_file() read /proc and the
                  filesystem, and are checked against processes this file really
                  spawns and files it really writes. They contain no logic, so
                  there is nothing to seed in them.

THE ASYMMETRY IS THE REQUIREMENT. There must be no path to CURRENT that does not
establish it: a status that says "up to date" because it could not tell ends an
investigation, which is what made the four failures expensive. The verdict
matrix below enumerates the missing-evidence cases rather than sampling them.
"""

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
import supervisor

SLEEPING_CHILD = "import time; time.sleep(120)"


@pytest.fixture
def sleeper():
    """A real live local process to read a real start time out of /proc.

    Reaped in the fixture's teardown, and the absence is not asserted here --
    tests/test_supervisor.py owns that proof. This one only needs a pid that
    exists for the duration.
    """
    process = subprocess.Popen([sys.executable, "-c", SLEEPING_CHILD])
    try:
        yield process
    finally:
        process.kill()
        process.wait()


def _code_tree(root: Path, mtime: float, name: str = "newest.py") -> Path:
    """A directory holding one .py file whose mtime is exactly `mtime`.

    Real file, real stat, real walk -- only the time is seeded. os.utime takes
    (atime, mtime) in SECONDS, which is an interface and not a report, so rule 6
    does not apply to it.
    """
    root.mkdir(parents=True, exist_ok=True)
    source = root / name
    source.write_text("# the code on disk\n", encoding="utf-8")
    os.utime(source, (mtime, mtime))
    return source


# ---------------------------------------------------------------------------
# THE DECISION
# ---------------------------------------------------------------------------


def test_a_process_started_before_the_code_changed_is_STALE():
    """The 2026-10-03 shape: the fix is on disk, the process predates it."""
    verdict = supervisor.code_freshness(process_started_at=1000.0, code_modified_at=2000.0)
    assert verdict["verdict"] == supervisor.CODE_STALE
    assert verdict["delta_seconds"] == -1000.0
    # The reason has to say which way round it is, because "STALE" alone does not
    # tell the operator whether to restart or to go looking for a bad edit.
    assert "BEFORE" in verdict["reason"]


def test_a_process_started_after_the_code_changed_is_CURRENT():
    verdict = supervisor.code_freshness(process_started_at=2000.0, code_modified_at=1000.0)
    assert verdict["verdict"] == supervisor.CODE_CURRENT
    assert verdict["delta_seconds"] == 1000.0


@pytest.mark.parametrize(
    ("started_at", "modified_at", "why"),
    [
        (None, 1000.0, "no start time: /proc unreadable, or the process is gone"),
        (1000.0, None, "no code time: nothing under the package directory could be stat'd"),
        (None, None, "neither"),
    ],
)
def test_missing_evidence_is_NOT_ESTABLISHED_and_never_CURRENT(started_at, modified_at, why):
    """NOT ESTABLISHED is a verdict, not a gap (rule 14), and never CURRENT.

    This is the property the whole feature turns on. An absence of evidence
    rendered as CURRENT would be the one answer that stops an operator looking,
    which is precisely what the four 2026-10-03 failures needed them to keep
    doing.
    """
    verdict = supervisor.code_freshness(started_at, modified_at)
    assert verdict["verdict"] == supervisor.CODE_NOT_ESTABLISHED, why
    assert verdict["verdict"] != supervisor.CODE_CURRENT
    # It must SAY what it could not read. A verdict with an empty reason is a
    # blinking cursor with a label on it.
    assert verdict["reason"]
    assert verdict["delta_seconds"] is None


@pytest.mark.parametrize("delta", [0.0, 0.5, -0.5, 1.9, -1.9])
def test_a_difference_inside_the_measurement_RESOLUTION_is_NOT_ESTABLISHED(delta):
    """btime has one-second granularity, so sub-second ranking is not a fact.

    Both signs are checked. The negative half is the one that matters: a process
    that started half a second before the newest file COULD be stale, and the
    honest answer is that this comparison cannot tell -- not STALE (which would
    cry wolf on `pull; start`, the most ordinary sequence there is) and above all
    not CURRENT.
    """
    verdict = supervisor.code_freshness(1000.0 + delta, 1000.0)
    assert verdict["verdict"] == supervisor.CODE_NOT_ESTABLISHED
    assert "measurement error" in verdict["reason"]


def test_the_RESOLUTION_boundary_is_inclusive_on_both_sides():
    """Exactly at the resolution, a verdict IS established -- in both directions.

    Pinned because the comparison is `<=` / `>=` and a mutation to `<` / `>` moves
    the boundary case into NOT ESTABLISHED, which no other test here would notice.
    """
    resolution = supervisor.CODE_FRESHNESS_RESOLUTION_SECONDS
    assert supervisor.code_freshness(1000.0 + resolution, 1000.0)["verdict"] == supervisor.CODE_CURRENT
    assert supervisor.code_freshness(1000.0 - resolution, 1000.0)["verdict"] == supervisor.CODE_STALE


def test_the_resolution_is_a_parameter_so_a_caller_can_say_what_it_can_measure():
    """Seeded resolution, so the band is not a constant only the module can state."""
    assert (
        supervisor.code_freshness(1000.0, 900.0, resolution_seconds=1000.0)["verdict"]
        == supervisor.CODE_NOT_ESTABLISHED
    )
    assert (
        supervisor.code_freshness(1000.0, 900.0, resolution_seconds=1.0)["verdict"]
        == supervisor.CODE_CURRENT
    )


def test_the_verdict_carries_the_numbers_it_was_computed_from():
    """Rule 14: echo the parameters that decide the answer.

    The printed line names the file and its time precisely so an operator can see
    an implausible one -- a `cp -p` deploy that preserved old mtimes is the hole
    in this definition, and the only way to spot it is to read the time.
    """
    verdict = supervisor.code_freshness(2000.0, 1000.0, newest_path="/x/y.py")
    assert verdict["newest_path"] == "/x/y.py"
    assert verdict["process_started_at"] == 2000.0
    assert verdict["code_modified_at"] == 1000.0


# ---------------------------------------------------------------------------
# THE GATHERING
# ---------------------------------------------------------------------------


def test_the_start_time_of_a_real_process_is_now_and_not_the_epoch(sleeper):
    """Read out of /proc for a process this test just spawned.

    The assertion is a window rather than an equality because the only other
    clock available is the one being converted against. A function that returned
    the raw tick count, or seconds-since-boot, would pass a "not None" test and
    fail this one: on this host boot was over an hour ago, so an unconverted
    answer is off by more than the window by orders of magnitude.
    """
    started = supervisor.process_start_time(sleeper.pid)
    assert started is not None
    assert abs(started - time.time()) < 60.0


def test_a_later_process_has_a_LATER_start_time_than_an_earlier_one(sleeper):
    """Ordering, which is the only property the verdict actually uses."""
    first = supervisor.process_start_time(sleeper.pid)
    time.sleep(0.05)
    second = subprocess.Popen([sys.executable, "-c", SLEEPING_CHILD])
    try:
        later = supervisor.process_start_time(second.pid)
    finally:
        second.kill()
        second.wait()
    assert first is not None and later is not None
    assert later >= first


def test_a_pid_that_does_not_exist_has_NO_start_time_rather_than_zero():
    """None means could not look. 0.0 would mean "started at the epoch".

    Which would make every unreadable process infinitely stale -- a confident
    wrong verdict, the failure mode this file exists to prevent, arriving through
    the sentinel rather than through the logic.
    """
    assert supervisor.process_start_time(2**30) is None


def test_without_proc_there_is_no_boot_time_and_so_no_verdict(monkeypatch, tmp_path):
    """"Could not look" propagates all the way to NOT ESTABLISHED.

    PROC_DIR is pointed at an empty directory rather than a missing one, so this
    also covers a /proc that exists and does not answer -- a container with a
    masked procfs. Both are the same answer: not CURRENT.
    """
    monkeypatch.setattr(supervisor, "PROC_DIR", tmp_path / "no-proc-here")
    assert supervisor.boot_time() is None
    assert supervisor.process_start_time(os.getpid()) is None
    assert supervisor.worker_code_freshness(os.getpid())["verdict"] == supervisor.CODE_NOT_ESTABLISHED


def test_a_btime_line_that_is_not_a_number_is_not_a_boot_time(monkeypatch, tmp_path):
    """A corrupt or unexpected /proc/stat answers None, not a crash and not a 0."""
    (tmp_path / "stat").write_text("cpu 1 2 3\nbtime notanumber\n", encoding="utf-8")
    monkeypatch.setattr(supervisor, "PROC_DIR", tmp_path)
    assert supervisor.boot_time() is None


def test_a_proc_stat_without_btime_at_all_answers_None(monkeypatch, tmp_path):
    (tmp_path / "stat").write_text("cpu 1 2 3\nprocesses 99\n", encoding="utf-8")
    monkeypatch.setattr(supervisor, "PROC_DIR", tmp_path)
    assert supervisor.boot_time() is None


def test_the_newest_py_file_is_found_and_pycache_and_runtime_are_not(tmp_path):
    """__pycache__ would make every worker stale moments after it started.

    A .pyc's mtime moves when the tree is IMPORTED, so including it would be a
    verdict generated by the act of observing. runtime/ holds this supervisor's
    own pid files and logs, which are not code.
    """
    # TWO REAL .py FILES, AND THE NEWEST ONE IS IN A SUBDIRECTORY. Both details
    # are load-bearing and both were missing from the first draft, where the
    # mutations that took the OLDEST file and that made the walk NON-RECURSIVE
    # both survived:
    #
    #   two files   with one eligible file the maximum and the minimum are the
    #               same file, so "newest" asserts nothing.
    #   nested      the workers import from chains/, services/ and workers/, so a
    #               walk that only looked at the top level would miss nearly all
    #               of the code -- and would still find A file, and still answer
    #               CURRENT. Putting the newest file one level down is what makes
    #               glob-instead-of-rglob fail here.
    _code_tree(tmp_path / "src", 500.0, name="edited_last_week.py")
    newest_source = _code_tree(tmp_path / "src" / "pkg", 1000.0)

    for ignored, mtime in (
        (tmp_path / "src" / "__pycache__" / "newest.cpython-312.pyc", 9000.0),
        (tmp_path / "src" / "__pycache__" / "decoy.py", 9000.0),
        (tmp_path / "src" / "runtime" / "decoy.py", 9000.0),
    ):
        ignored.parent.mkdir(parents=True, exist_ok=True)
        ignored.write_text("x\n", encoding="utf-8")
        os.utime(ignored, (mtime, mtime))

    found = supervisor.newest_code_file(tmp_path / "src")
    assert found is not None
    assert Path(found[0]) == newest_source
    assert found[1] == 1000.0


def test_a_tree_with_no_python_in_it_answers_None_not_a_fresh_tree(tmp_path):
    """An empty or unreadable tree must not render as "nothing has changed"."""
    (tmp_path / "empty").mkdir()
    assert supervisor.newest_code_file(tmp_path / "empty") is None
    assert (
        supervisor.code_freshness(time.time(), None)["verdict"] == supervisor.CODE_NOT_ESTABLISHED
    )


def test_the_real_package_tree_has_python_in_it_so_the_default_root_is_right():
    """The default root is swap_terminal/ -- the directory the workers import from.

    If this ever answers None, the verdict on the live host is permanently NOT
    ESTABLISHED and nothing else here would say so: every other test in this file
    passes its own root.
    """
    found = supervisor.newest_code_file()
    assert found is not None
    assert Path(found[0]).name.endswith(".py")
    assert Path(found[0]).is_relative_to(supervisor.BASE_DIR)


# ---------------------------------------------------------------------------
# THE TWO HALVES TOGETHER, AGAINST A REAL PROCESS AND REAL FILES
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("offset", "expected"),
    [
        (10.0, supervisor.CODE_STALE),
        (-10.0, supervisor.CODE_CURRENT),
        (0.0, supervisor.CODE_NOT_ESTABLISHED),
    ],
)
def test_a_real_live_process_against_a_real_file_whose_mtime_is_seeded(sleeper, tmp_path, offset, expected):
    """End to end with nothing paraphrased: a real pid, /proc, and a real stat.

    The file's mtime is set relative to THE PROCESS'S OWN START TIME as read from
    /proc, which is what makes all three verdicts deterministic -- no sleeping, no
    race against a scheduler, and the offsets are far outside the resolution band.
    """
    started = supervisor.process_start_time(sleeper.pid)
    assert started is not None
    _code_tree(tmp_path / "src", started + offset)
    assert supervisor.worker_code_freshness(sleeper.pid, tmp_path / "src")["verdict"] == expected


# ---------------------------------------------------------------------------
# WHAT THE OPERATOR READS
# ---------------------------------------------------------------------------


def test_the_STALE_line_says_it_is_stale_names_the_file_and_gives_the_REMEDY():
    """"Did my fix deploy?" was the question all four times. The line answers it.

    The remedy is asserted because `start` on a running worker is a no-op and an
    operator who has just pulled reasonably believes otherwise -- that belief cost
    a credited swap on 2026-10-03.
    """
    verdict = supervisor.code_freshness(1000.0, 2000.0, newest_path="/x/fix.py")
    block = "\n".join(supervisor.code_freshness_lines("running", 4242, verdict))
    assert "*** STALE CODE ***" in block
    assert "/x/fix.py" in block
    assert "stop" in block and "start" in block


def test_the_CURRENT_line_states_what_it_did_NOT_check():
    """mtime cannot see an exported credential, and the line has to say so.

    One of the four failures was `KeyError: 'BTC'` from a worker whose ENVIRONMENT
    predated the credentials. No file changes when a variable is exported, so a
    bare "CURRENT" would have been read as "this worker has everything it needs"
    -- which is the same class of wrong answer as a false CURRENT about the code.
    /proc/<pid>/environ is never read: it holds the wallet passphrase.
    """
    verdict = supervisor.code_freshness(2000.0, 1000.0, newest_path="/x/fix.py")
    block = "\n".join(supervisor.code_freshness_lines("running", 4242, verdict))
    assert "CURRENT" in block
    assert "credential" in block
    assert "*** STALE CODE ***" not in block


def test_the_NOT_ESTABLISHED_line_refuses_to_be_read_as_up_to_date():
    verdict = supervisor.code_freshness(None, 1000.0)
    block = "\n".join(supervisor.code_freshness_lines("running", 4242, verdict))
    assert "NOT ESTABLISHED" in block
    assert "not 'up to date'" in block


@pytest.mark.parametrize(
    ("state", "pid"),
    [("stopped", None), ("stopped", 4242), ("unknown", 4242)],
)
def test_a_worker_that_is_not_running_gets_a_line_saying_so_rather_than_silence(state, pid):
    """Rule 14: a silent omission reads as "no problem found".

    A stopped worker has no process to be out of date, and for `unknown` the pid
    belongs to a stranger -- ranking a stranger's start time against our source
    tree would be a confident number about nothing.
    """
    block = "\n".join(supervisor.code_freshness_lines(state, pid))
    assert block.strip()
    assert "*** STALE CODE ***" not in block
    assert supervisor.CODE_CURRENT not in block


def test_the_renderer_NEVER_calls_CURRENT_without_a_CURRENT_verdict():
    """The forbidden direction, asserted at the rendering layer too.

    The decision function cannot answer CURRENT from an absence; this pins that
    the printer cannot invent one either -- the two layers fail independently and
    only one of them is covered by the verdict matrix above.
    """
    for started, modified in ((None, 1000.0), (1000.0, None), (None, None), (1000.0, 2000.0)):
        verdict = supervisor.code_freshness(started, modified)
        block = "\n".join(supervisor.code_freshness_lines("running", 4242, verdict))
        assert "code CURRENT" not in block


# ---------------------------------------------------------------------------
# THE CLI, WHICH IS THE ONLY SURFACE THE OPERATOR ACTUALLY SEES
# ---------------------------------------------------------------------------


def _status_output(capsys, monkeypatch, tmp_path, run_dir, code_mtime: float | None) -> str:
    """Run the REAL `status` against a worker this function really started.

    Only the ROOT of the code walk is substituted -- newest_code_file() itself,
    process_start_time(), worker_status() and command_status() are the real
    functions, and the pid is a real pid. Substituting the root rather than the
    verdict is what keeps this a behavioral test: a mutation anywhere in the
    gathering or the decision still fails it.
    """
    if code_mtime is not None:
        _code_tree(tmp_path / "src", code_mtime)
    real_walk = supervisor.newest_code_file
    monkeypatch.setattr(
        supervisor, "newest_code_file", lambda root=None: real_walk(tmp_path / "src")
    )
    # endpoint_summary() imports the chain registry and prints the banner; it is
    # not what this test is about, and letting it run would make the output depend
    # on which RPC credentials happen to be exported here.
    monkeypatch.setattr(supervisor, "endpoint_summary", lambda: ["  database          (stubbed for this test)"])
    table = {"sleeper": [sys.executable, "-c", SLEEPING_CHILD]}
    assert supervisor.main(["start", "sleeper", "--run-dir", str(run_dir)], commands=table) == 0
    capsys.readouterr()
    assert supervisor.main(["status", "sleeper", "--run-dir", str(run_dir)], commands=table) == 0
    return capsys.readouterr().out


def test_status_NAMES_a_running_worker_that_predates_the_code(capsys, monkeypatch, tmp_path):
    """The whole point, through the command an operator actually types.

    The worker is started FIRST and the code is dated afterward, which is the
    2026-10-03 sequence exactly: the process is already up, then the fix lands.
    """
    run_dir = tmp_path / "run"
    output = _status_output(capsys, monkeypatch, tmp_path, run_dir, time.time() + 600.0)
    try:
        assert "*** STALE CODE ***" in output
        assert "stale=1" in output
        # And it must not also be reported as fine. Two verdicts on one worker is
        # the contradiction-in-one-banner defect this file's subject already has a
        # measured instance of (see spawn_warning()).
        assert "code CURRENT" not in output
    finally:
        supervisor.main(["stop", "sleeper", "--run-dir", str(run_dir)], commands={"sleeper": []})


def test_status_says_CURRENT_for_a_worker_started_after_the_newest_file(capsys, monkeypatch, tmp_path):
    run_dir = tmp_path / "run"
    output = _status_output(capsys, monkeypatch, tmp_path, run_dir, time.time() - 600.0)
    try:
        assert "code CURRENT" in output
        assert "stale=0" in output
        assert "unknown-age=0" in output
        assert "*** STALE CODE ***" not in output
    finally:
        supervisor.main(["stop", "sleeper", "--run-dir", str(run_dir)], commands={"sleeper": []})


def test_the_code_summary_line_is_printed_even_when_nothing_is_wrong(capsys, monkeypatch, tmp_path):
    """"No stale worker" and "this check did not run" must not render alike.

    A summary that only appears when there is a problem cannot be trusted to be
    absent for the right reason -- which is rule 14's blank-gap ambiguity moved
    into the one line most likely to be the only one read.
    """
    run_dir = tmp_path / "run"
    output = _status_output(capsys, monkeypatch, tmp_path, run_dir, time.time() - 600.0)
    try:
        assert "  code              stale=" in output
        assert "of running=1" in output
    finally:
        supervisor.main(["stop", "sleeper", "--run-dir", str(run_dir)], commands={"sleeper": []})


def test_status_reports_NOT_ESTABLISHED_rather_than_CURRENT_when_it_cannot_look(capsys, monkeypatch, tmp_path):
    """No code to compare against: the verdict is withheld, not guessed.

    The tree handed to the walk has no .py file in it at all, so
    newest_code_file() answers None -- the gathering failure, arriving at the CLI.
    """
    run_dir = tmp_path / "run"
    (tmp_path / "src").mkdir()
    output = _status_output(capsys, monkeypatch, tmp_path, run_dir, None)
    try:
        assert "NOT ESTABLISHED" in output
        assert "unknown-age=1" in output
        assert "code CURRENT" not in output
        assert "*** STALE CODE ***" not in output
    finally:
        supervisor.main(["stop", "sleeper", "--run-dir", str(run_dir)], commands={"sleeper": []})


def test_status_still_reports_only_and_leaves_the_stale_worker_RUNNING(capsys, monkeypatch, tmp_path):
    """REPORTING ONLY, asserted rather than asserted-in-a-docstring.

    Restarting a payout worker is a live-posture action (rule 16). A `status` that
    killed or respawned a worker on the strength of a FILE MTIME would be deciding
    when money moves from a timestamp, so the absence of that behavior is pinned
    here: after a STALE verdict the process is still alive and its pid file still
    names it.
    """
    run_dir = tmp_path / "run"
    output = _status_output(capsys, monkeypatch, tmp_path, run_dir, time.time() + 600.0)
    try:
        assert "*** STALE CODE ***" in output
        record = supervisor.read_pid_record(supervisor.pid_file(run_dir, "sleeper"))
        assert record is not None, "status removed the pid file of a stale worker"
        # Ask the OS, not the report (rule 13).
        os.kill(record[0], 0)
        assert supervisor.worker_status("sleeper", run_dir)["state"] == "running"
    finally:
        supervisor.main(["stop", "sleeper", "--run-dir", str(run_dir)], commands={"sleeper": []})


def test_with_NOTHING_RUNNING_the_code_line_says_nothing_was_checked(capsys, monkeypatch, tmp_path):
    """stale=0 of running=0 is not an all-clear, and it must not read as one.

    The first live run of this feature printed `stale=0  unknown-age=0` followed by
    "every running worker started after the newest .py file was modified" with no
    worker running at all -- vacuously true, and exactly rule 14's
    did-nothing-looks-like-did-work in the line that was added to fix it.
    """
    monkeypatch.setattr(supervisor, "endpoint_summary", lambda: ["  database          (stubbed for this test)"])
    table = {"sleeper": [sys.executable, "-c", SLEEPING_CHILD]}
    assert supervisor.main(["status", "sleeper", "--run-dir", str(tmp_path / "cold")], commands=table) == 0
    output = capsys.readouterr().out
    assert "of running=0" in output
    assert "NOTHING WAS CHECKED" in output
    assert "every running worker started after" not in output
