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

import pytest

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
        after = [
            supervisor.read_pid_record(supervisor.pid_file(tmp_path, name))[0] for name in table
        ]
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
        pids = [supervisor.read_pid_record(supervisor.pid_file(tmp_path, n))[0] for n in table]
        assert all(not _gone(pid) for pid in pids)
    finally:
        for name in table:
            supervisor.stop_worker(name, tmp_path, grace_seconds=5.0)
        for pid in pids:
            _reap(pid)


def test_a_CURRENT_process_appends_nothing_to_its_cycle_line():
    """A healthy cycle line must be byte-identical to what it was before this feature.

    The note is the whole value of the change and it is also the whole risk: a
    staleness line on every cycle of every healthy worker would be noise, and
    noise is what gets filtered out before the one line that matters appears.
    """
    assert stale_code_note() == "", (
        "this process started after the newest .py file, so it is not stale and must "
        "say nothing"
    )
    line = cycle_line("payout_worker", 3, 0.2, {"pending_at_start": 0, "broadcast": 0})
    assert "\n" not in line, "a current worker's cycle line gained a second line"
    assert "STALE" not in line


@pytest.mark.skipif(not Path("/proc/1").exists(), reason="needs /proc/1")
def test_a_process_that_really_predates_the_tree_says_so():
    """pid 1 started at boot, which is before any .py in this tree was last written.

    A real pid rather than a stub, because the thing being checked is that the
    wiring to /proc and to the tree is live -- code_freshness() itself is already
    tested against seeded timestamps in tests/test_supervisor.py.
    """
    note = stale_code_note(pid=1)
    assert "*** STALE CODE ***" in note, f"pid 1 was not reported stale: {note!r}"
    assert "BEFORE the newest .py file" in note, "the note does not say which way the gap runs"
    assert "restart" in note, (
        "the note must name the fix. `start` alone does NOT reload a running worker, and "
        "an operator who runs it reads spawned=0 already-running=3 as success"
    )
    assert "ufn" not in note, "rule 6: the unit is µfn, never an ASCII u"
