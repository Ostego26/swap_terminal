"""`restart` closes the gap between `stop` and `start`, and refuses rather than double-spawn.

Role: test (submodule -> function)
Reads: supervisor.py, workers/common.py, and /proc
Writes: only inside tmp_path
Can move funds: no. Every worker here is a sleeper.
Mainnet-safe: yes

WHY `restart` EXISTS, measured on the operator's host 2026-10-05.

supervisor.py has detected stale worker code since 2026-10-03 and prints
`*** STALE CODE ***` with the gap and the newest file. All three workers had been
running code 46029.9µfn (55677.8s) older than the tree -- about fifteen and a half
hours, spanning the window in which supervisor.py's OWN double-spawn defect was
fixed. The detection was perfect and nobody saw it.

Its advice was "fix: `stop` then `start`", and following that advice produced this,
in the gap between the two commands:

    summary  running=0/3  <- 0 means no worker is polling and deposits will not
                             be credited
    it holds awaiting_deposit 5

Five open swaps with nothing watching their deposit addresses, for as long as it
took to remember the second command. That is the gap `restart` closes.

THE REFUSAL IS THE REASON THIS IS NOT A SHELL ALIAS. `stop; start` runs start
whatever stop did. If the stop could not prove absence, that starts a SECOND
worker beside a live one and the pid file then names only the new one, leaving the
first an orphan nothing can reap -- rule 13's exact damage, and the same shape as
the start_worker double-spawn fixed the same day, arrived at from the other side.
"""

from __future__ import annotations

import contextlib
import os
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "swap_terminal"))

import supervisor  # noqa: E402  -- after the sys.path.insert above
from workers.common import cycle_line, stale_code_note  # noqa: E402

SLEEPING_CHILD = "import time; time.sleep(120)"


def _table() -> dict[str, list[str]]:
    return {
        name: [sys.executable, "-c", SLEEPING_CHILD]
        for name in ("deposit_watcher", "payout_worker", "reconcile_worker")
    }


def _reap(pid: int) -> None:
    with contextlib.suppress(ChildProcessError, OSError):
        os.waitpid(pid, 0)


def _pid_from_file(run_dir: Path, name: str) -> int:
    """The pid this worker's pid file names, or a named assertion failure.

    read_pid_record() returns None for a pid file that is missing, empty, or
    junk -- all of which mean "this file tells us nothing". For a RESTART test
    that is the failure under test, not a precondition: subscripting it
    directly (`read_pid_record(...)[0] for name in table`) died with `TypeError:
    'NoneType' object is not subscriptable`, naming neither the worker nor the
    reason, where what the reader needs is "the restart reported success and
    left no pid it can point at" -- rule 13's "a stop that cannot prove it
    worked is not a stop", arrived at from the start side. Found by pyright
    (reportOptionalSubscript x2, 2026-10-09).
    """
    record = supervisor.read_pid_record(supervisor.pid_file(run_dir, name))
    assert record is not None, (
        f"no readable pid file for worker {name!r} after a restart that returned 0 -- a restart "
        f"that cannot name the pid it left cannot be proven to have restarted anything (rule 13)"
    )
    return record[0]


def _gone(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    return False


def test_restart_replaces_every_worker_and_the_old_pids_are_gone(tmp_path):
    """The whole point: new processes, and the old ones proven dead by the OS."""
    table = _table()
    before = [supervisor.start_worker(name, argv, tmp_path)["pid"] for name, argv in table.items()]
    try:
        code = supervisor.main(
            ["restart", "--run-dir", str(tmp_path), "--grace-seconds", "5"], commands=table
        )
        assert code == 0, f"restart returned {code}"
        after = [_pid_from_file(tmp_path, name) for name in table]
        assert set(before).isdisjoint(after), (
            f"pid files still name the old processes {before}; nothing was restarted, which "
            f"is the failure that would leave stale code running while reporting success"
        )
        survivors = [pid for pid in before if not _gone(pid)]
        assert not survivors, f"old worker pids survived the restart: {survivors}"
    finally:
        for name in table:
            supervisor.stop_worker(name, tmp_path, grace_seconds=5.0)
        for pid in before:
            _reap(pid)


def test_restart_REFUSES_and_starts_nothing_when_an_orphan_is_alive(tmp_path):
    """THE DANGEROUS CASE. A live worker no pid file accounts for must block the start.

    The orphan has to look like a real worker to unaccounted_workers(), which builds
    its marker from worker_commands() -- the REAL script path -- and not from any
    table a caller substitutes. That is right for production and it means a bare
    sleeper can never match, so the sleeper is given the real marker as an argv
    element, which is exactly the pair of conditions supervisor.py:249-252 tests.
    Without that, this test would pass vacuously by finding no orphan at all --
    which is how it was first written, and it reported a pass.
    """
    assert supervisor.PROC_DIR.is_dir(), "this test's premise is a platform with /proc"
    marker = supervisor.worker_commands()["payout_worker"][-1]
    # sys.executable and a literal argv, no shell and no outside input. Kept as a
    # plain comment and NOT as `noqa: S603`: pyproject.toml already disables S603
    # for tests/, so the directive matched nothing and RUF100 said so -- a `noqa`
    # that suppresses no finding is an unread claim (rule 19), while the reasoning
    # behind it is still worth a reader's time (rule 1).
    orphan = subprocess.Popen(
        [sys.executable, "-c", SLEEPING_CHILD, marker],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        for _ in range(100):
            if supervisor.unaccounted_workers(["payout_worker"], tmp_path):
                break
            time.sleep(0.02)
        assert supervisor.unaccounted_workers(["payout_worker"], tmp_path), (
            "the orphan is not visible to unaccounted_workers(), so the refusal below "
            "would be asserting on nothing"
        )
        code = supervisor.main(
            ["restart", "payout_worker", "--run-dir", str(tmp_path), "--grace-seconds", "3"],
            commands=_table(),
        )
        assert code != 0, "restart started a worker beside a live orphan"
        assert not supervisor.pid_file(tmp_path, "payout_worker").exists(), (
            "a pid file was written, so a SECOND worker was started next to the orphan "
            "and the file now names only one of them"
        )
    finally:
        orphan.kill()
        orphan.wait(timeout=10)


def test_restart_on_nothing_running_is_just_a_start(tmp_path):
    """No worker running is not an error. It is the ordinary cold start."""
    table = _table()
    code = supervisor.main(
        ["restart", "--run-dir", str(tmp_path), "--grace-seconds", "5"], commands=table
    )
    pids = []
    try:
        assert code == 0, f"restart on a cold run dir returned {code}"
        pids = [_pid_from_file(tmp_path, n) for n in table]
        assert all(not _gone(pid) for pid in pids)
    finally:
        for name in table:
            supervisor.stop_worker(name, tmp_path, grace_seconds=5.0)
        for pid in pids:
            _reap(pid)


def test_a_CURRENT_process_appends_nothing_to_its_cycle_line(monkeypatch):
    """A healthy cycle line must be byte-identical to what it was before this feature.

    The note is the whole value of the change and it is also the whole risk: a
    staleness line on every cycle of every healthy worker would be noise, and
    noise is what gets filtered out before the one line that matters appears.

    THE VERDICT IS SEEDED, AND IT WAS NOT UNTIL 2026-11-21. This test read the LIVE
    tree -- `stale_code_note()` with no patch, asserting the empty string -- which
    made it an assertion about WHEN THE SUITE RAN rather than about the function.
    It fails whenever any .py in the tree is written while the suite is in flight,
    because the pytest process then genuinely predates the newest file and the
    feature correctly fires. Measured when it did fail, mid-run, on an edit to
    swap_terminal/architecture.py:

        AssertionError: assert '*** STALE CO...lone will NOT' == ''
        + *** STALE CODE *** this worker predates the tree: this process started
          62.3µfn (75.4s) BEFORE the newest .py file was modified

    Nothing was broken. The suite had edited a file under itself, which is an
    ordinary thing to do and must not read as a regression -- and re-running the
    same test alone, with no write in flight, passed in 0.2s.

    THIS IS THE SAME DEFECT ITS OWN SIBLING BELOW ALREADY DIAGNOSED AND FIXED, and
    that is why it is worth the paragraph (rule 8: when you touch a rule, grep for
    it -- the fix had been applied to ONE of the two sites). Twenty-three lines
    down, test_a_STALE_verdict_produces_the_sentence_an_operator_needs says "A test
    whose verdict depends on when the suite is run relative to the last edit is not
    a test of anything", having been flaky for exactly this reason with pid 1. It
    seeds its verdict. This one, asserting the opposite branch of the same
    function, kept reading the real clock -- so the pair disagreed about whether
    the hazard existed, and the half that was still exposed was the half that fires
    on every developer who edits during a run.

    THE LIVE TREE IS STILL COVERED, deliberately, by
    test_the_note_is_WIRED_to_a_real_pid_and_a_real_tree immediately below: it
    walks a real pid against a real tree and asserts the verdict is one of the
    THREE, which is the only honest assertion available when both inputs move. The
    split is the same one the sibling's docstring sets out -- gathering is tested
    against seeded timestamps, and what the operator READS is tested here.
    """
    monkeypatch.setattr(
        "workers.common.worker_code_freshness",
        lambda _pid: {
            "verdict": supervisor.CODE_CURRENT,
            "reason": "this process started 62.3µfn (75.4s) AFTER the newest .py file was modified",
            "newest_path": "/x/swap_terminal/supervisor.py",
            "code_modified_at": 0.0,
            "process_started_at": 1.0,
            "delta_seconds": 75.4,
        },
    )
    assert stale_code_note() == "", (
        "a CURRENT verdict must produce no note at all: a staleness line on every "
        "cycle of every healthy worker is the noise this feature cannot afford"
    )
    line = cycle_line("payout_worker", 3, 0.2, {"pending_at_start": 0, "broadcast": 0})
    assert "\n" not in line, "a current worker's cycle line gained a second line"
    assert "STALE" not in line


def test_the_note_is_WIRED_to_a_real_pid_and_a_real_tree():
    """The gathering half: a real pid, a real walk, and one of the three verdicts.

    Asserts WHICH THREE, not which one. The verdict for any given pid depends on
    when that process started relative to the newest .py file, and both move.
    """
    freshness = supervisor.worker_code_freshness(os.getpid())
    assert freshness["verdict"] in (
        supervisor.CODE_CURRENT,
        supervisor.CODE_STALE,
        supervisor.CODE_NOT_ESTABLISHED,
    ), freshness
    assert freshness["newest_path"], "the tree walk found no .py file at all"


def test_a_STALE_verdict_produces_the_sentence_an_operator_needs(monkeypatch):
    """The deciding half, against a SEEDED verdict rather than a real process.

    THIS TEST USED TO USE pid 1 AND IT WAS FLAKY. The premise was "pid 1 started at
    boot, which is before any .py in this tree was last written". That is false in a
    container: measured 2026-10-05, /proc/1 reported a start time 5345.3µfn (6465.7s)
    AFTER the newest .py, so pid 1 was correctly CURRENT and the test failed. It had
    passed earlier the same day purely because the edit happened to be later than the
    boot at that moment. A test whose verdict depends on when the suite is run
    relative to the last edit is not a test of anything.

    The split is the fix, and it follows what each half owns:
      supervisor.worker_code_freshness()   gathers -- two reads and a call, already
                                           tested against seeded timestamps in
                                           tests/test_supervisor.py, and wired above
      common.stale_code_note()             decides what the operator READS, which is
                                           the only thing this file introduced

    So the verdict is seeded here and the assertions are about the SENTENCE.

    ITS TWIN ABOVE WAS STILL READING THE REAL CLOCK UNTIL 2026-11-21, and the
    reasoning in this docstring is what settled it: the CURRENT branch of the same
    function was asserted against the live tree and failed the first time the suite
    edited a .py under itself. Both halves are seeded now. If a third verdict ever
    grows a sentence, seed it too rather than finding a process in that state.
    """
    monkeypatch.setattr(
        "workers.common.worker_code_freshness",
        lambda _pid: {
            "verdict": supervisor.CODE_STALE,
            "reason": "this process started 46029.9µfn (55677.8s) BEFORE the newest .py file was modified",
            "newest_path": "/x/swap_terminal/supervisor.py",
            "code_modified_at": 1.0,
            "process_started_at": 0.0,
            "delta_seconds": -55677.8,
        },
    )
    note = stale_code_note()
    assert "*** STALE CODE ***" in note
    assert "BEFORE the newest .py file" in note, "the note does not say which way the gap runs"
    assert "/x/swap_terminal/supervisor.py" in note, "the note must name the file that is newer"
    assert "restart" in note, (
        "the note must name the fix. `start` alone does NOT reload a running worker, and "
        "an operator who runs it reads spawned=0 already-running=3 as success"
    )
    assert "ufn" not in note, "rule 6: the unit is µfn, never an ASCII u"

    # And it reaches the operator THROUGH the cycle line, which is the only place
    # all three workers render anything.
    line = cycle_line("payout_worker", 9, 0.2, {"pending_at_start": 0})
    assert "*** STALE CODE ***" in line, "the note never reached the line an operator reads"
