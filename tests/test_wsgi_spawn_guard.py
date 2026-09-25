"""The WSGI entry point must start nothing in the background.

Role: test (behavioral verification of the gunicorn N-workers guard)
Reads: wsgi.py and swap_terminal/import_spawn_guard.py, and runs a real
       subprocess that imports the application
Writes: nothing outside pytest's temp directory
Can move funds: no -- no test here opens a socket to a chain, and the
       subprocess below points SWAP_DB_PATH at a temp file
Mainnet-safe: yes

WHAT IS BEING PINNED, AND WHY IT IS WORTH A TEST RATHER THAN A COMMENT.

Gunicorn forks N workers and each one imports the application. A background
thread or poll loop started at import time -- or inside create_app() -- is
therefore started N times, in N processes, and nothing crashes and nothing
logs. CLAUDE.md rule 13's damage model: "an orphan does not crash anything."
This tree has already measured the plural form of it (2 sends, 1 swap_id, 2
'broadcast' rows for one swap).

"Do not start workers in create_app()" written in a README is a sentence
somebody has to read. These tests are the machine reading it.

THE TWO HALVES, BECAUSE THEY FAIL FOR DIFFERENT REASONS.

  the decision, seeded      describe_spawn_delta() is a pure function of four
                            sets (CLAUDE.md rule 10: the thing that decides is
                            the smallest testable piece). Seeded directly, no
                            fork required.
  the real import           a subprocess actually imports wsgi and reports its
                            own thread and child counts. This is the clause
                            CLAUDE.md's verification principle asks for --
                            "run the real script, assert on what is actually
                            there" -- and it is the one that would catch a
                            spawn introduced anywhere in the import graph,
                            including in a module nobody thought to check.

AND THE MUTATION CHECK. test_the_guard_catches_a_real_thread_that_was_really
_started spawns a genuine thread in a real subprocess and asserts the real
guard refuses. Without it, every assertion above would still pass if
describe_spawn_delta() returned [] unconditionally -- which is precisely the
failure mode a guard has, and it is silent.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
import threading
from pathlib import Path

from import_spawn_guard import (
    assert_nothing_spawned,
    child_pid_snapshot,
    describe_spawn_delta,
    thread_snapshot,
)

REPO_ROOT = Path(__file__).resolve().parent.parent


def _run_child(body: str, db_path: Path) -> subprocess.CompletedProcess:
    """Run a snippet in a fresh interpreter rooted at the repository.

    A subprocess rather than an import, because the question is what a FRESH
    process does on its first import of the application -- which is exactly
    what a gunicorn worker is. Importing wsgi inside the pytest process would
    measure a process that has already imported half the tree.
    """
    return subprocess.run(
        [sys.executable, "-c", textwrap.dedent(body)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        env={
            "PATH": "/usr/bin:/bin",
            "SWAP_DB_PATH": str(db_path),
            "PYTHONPATH": str(REPO_ROOT),
        },
        check=False,
    )


# --- the decision, seeded ----------------------------------------------------


def test_nothing_changed_is_reported_as_nothing():
    assert describe_spawn_delta({(1, "MainThread")}, {(1, "MainThread")}, set(), set()) == []


def test_a_new_thread_is_reported_by_name():
    findings = describe_spawn_delta(
        {(1, "MainThread")},
        {(1, "MainThread"), (2, "deposit_poll")},
        set(),
        set(),
    )
    assert len(findings) == 1
    # The NAME has to appear: "something was spawned" does not tell an operator
    # which module to go and look at, and that is the whole value of the line.
    assert "deposit_poll" in findings[0]


def test_a_forked_child_is_reported_by_pid():
    findings = describe_spawn_delta({(1, "MainThread")}, {(1, "MainThread")}, set(), {4242})
    assert len(findings) == 1
    assert "4242" in findings[0]


def test_a_thread_that_ended_is_not_a_finding():
    # A library warming up and tearing a thread down during import is not a
    # background loop taking hold. Flagging it would make the guard cry wolf,
    # and a guard people disable protects nothing (CLAUDE.md rule 19).
    assert describe_spawn_delta({(1, "MainThread"), (9, "gone")}, {(1, "MainThread")}, set(), set()) == []


def test_assert_nothing_spawned_is_silent_when_nothing_spawned():
    assert assert_nothing_spawned({(1, "MainThread")}, {(1, "MainThread")}, set(), set()) is None


def test_assert_nothing_spawned_raises_and_names_both_the_thread_and_the_reaper():
    try:
        assert_nothing_spawned({(1, "MainThread")}, {(1, "MainThread"), (7, "payout_poll")}, set(), set())
    except RuntimeError as exc:
        message = str(exc)
    else:
        raise AssertionError("the guard returned instead of raising on a spawned thread")
    assert "payout_poll" in message
    # The message has to point at the reaper, or an operator who trips this has
    # been told there is a problem and not where the workers are supposed to
    # live (CLAUDE.md rule 14: state what the number means, next to the number).
    assert "supervisor.py" in message


# --- the real import ---------------------------------------------------------


def test_importing_the_real_wsgi_module_starts_no_thread_and_no_child(tmp_path):
    """Import wsgi for real, in a fresh process, and count what it started.

    This is the invariant itself rather than a model of it. If any module in
    the application's import graph starts a thread or forks, this fails --
    including one added years from now by somebody who never read the README.
    """
    result = _run_child(
        """
        import threading, sys
        sys.path.insert(0, "swap_terminal")
        from import_spawn_guard import child_pid_snapshot, children_are_observable
        before_threads = len(threading.enumerate())
        before_children = child_pid_snapshot()
        import wsgi
        print("THREADS", len(threading.enumerate()) - before_threads)
        print("CHILDREN", len(child_pid_snapshot() - before_children))
        print("OBSERVABLE", children_are_observable())
        print("APP", wsgi.app is not None)
        """,
        tmp_path / "spawn.db",
    )
    assert result.returncode == 0, f"importing wsgi failed:\n{result.stderr}"
    assert "THREADS 0" in result.stdout, result.stdout
    assert "APP True" in result.stdout, result.stdout
    # The child count is only an assertion when the platform can answer it.
    # Asserting "0 children" on a kernel that cannot report children would be
    # CLAUDE.md rule 17's error: presenting a decline as a measurement.
    if "OBSERVABLE True" in result.stdout:
        assert "CHILDREN 0" in result.stdout, result.stdout


def test_the_guard_catches_a_real_thread_that_was_really_started(tmp_path):
    """Start a genuine thread in a real process; the real guard must refuse.

    THE MUTATION CHECK. Every other assertion in this file would still pass if
    describe_spawn_delta() returned [] unconditionally, because the application
    genuinely spawns nothing -- a broken guard and a clean tree produce the
    same output. This is the only test here that fails when the guard stops
    working, and it uses the real functions on a real thread rather than seeded
    sets.
    """
    result = _run_child(
        """
        import sys, threading, time
        sys.path.insert(0, "swap_terminal")
        from import_spawn_guard import assert_nothing_spawned, child_pid_snapshot, thread_snapshot

        before_threads, before_children = thread_snapshot(), child_pid_snapshot()

        # Stand in for what a future create_app() might do: a daemon poll loop,
        # the exact shape workers/deposit_watcher.py has. It is never joined,
        # which is the point -- this is what an unreaped spawn looks like.
        stop = threading.Event()
        threading.Thread(target=stop.wait, name="a_poll_loop_somebody_added", daemon=True).start()
        while threading.active_count() < 2:
            time.sleep(0.01)

        try:
            assert_nothing_spawned(before_threads, thread_snapshot(), before_children, child_pid_snapshot())
        except RuntimeError as exc:
            print("REFUSED")
            print("NAMED", "a_poll_loop_somebody_added" in str(exc))
        else:
            print("GUARD LET IT THROUGH")
        """,
        tmp_path / "spawn2.db",
    )
    assert result.returncode == 0, result.stderr
    assert "REFUSED" in result.stdout, f"the guard did not catch a real spawned thread:\n{result.stdout}"
    assert "NAMED True" in result.stdout, result.stdout


# --- the snapshot helpers themselves -----------------------------------------


def test_thread_snapshot_includes_the_thread_calling_it():
    names = {name for _ident, name in thread_snapshot()}
    assert threading.current_thread().name in names


def test_child_pid_snapshot_returns_a_set_of_ints():
    # Weak on purpose. The strong claim -- "this process has no children" -- is
    # not true inside a pytest run that may itself fork, so what is pinned here
    # is the SHAPE the delta function relies on, not a count.
    assert all(isinstance(pid, int) for pid in child_pid_snapshot())
