"""Prove that importing the Flask application starts nothing in the background.

Role: submodule -> function (describe_spawn_delta is the decision)
Reads: this process's own threads (threading.enumerate) and, on Linux, its
       child pids from /proc/self/task/<tid>/children
Writes: nothing
Can move funds: no -- it observes this process and never touches a chain, a
       wallet, a key or the database
Mainnet-safe: yes

WHY THIS EXISTS: GUNICORN TURNS ONE BACKGROUND LOOP INTO N OF THEM.

Gunicorn's model is to fork `workers` worker processes, each of which imports
the WSGI application. That is fine for request handling -- serving HTTP is
stateless here -- and it is the specific way CLAUDE.md rule 13's damage arrives
through a door nobody has walked through yet.

If anything ever starts a thread or a poll loop at import time or inside
create_app(), gunicorn does not start one of them. It starts one PER WORKER,
because each worker imports the application again after the fork. Nothing
crashes. Nothing logs an error. There are simply N deposit watchers, or N
payout workers, polling the same SQLite database and each reading the same
pending swap.

This repository has already measured what the plural form of that costs. From
CLAUDE.md's closing section: "two payout workers paid the same swap twice -- 2
sends, 1 swap_id, 2 'broadcast' rows." Those were two hand-started processes.
A gunicorn deployment with `workers = 4` would make four the default, and would
do it silently on every restart.

WHAT IS TRUE TODAY, MEASURED RATHER THAN ASSUMED (2026-09-25).

The three polling loops live in workers/deposit_watcher.py,
workers/payout_worker.py and workers/reconcile_worker.py, they are separate
processes, and swap_terminal/supervisor.py is the only thing that starts them.
Measured by walking the AST of every module reachable from `import app` and
looking for threading/multiprocessing/subprocess primitives: none appear.
`services/pricing.py` imports `threading.Lock`, which is a mutex and not a
spawn. `transactions.py:802` does start a `threading.Thread`, and it is inside
a Tk GUI method in a module the application never imports.

So the property holds. This module is what makes it hard to UNDO -- a
documented invariant is a sentence somebody has to read, and this one is
checked by the machine at the moment it would be violated.

WHY THE CHECK IS A DELTA AND NOT AN ABSOLUTE COUNT.

"The process has one thread" is not the invariant and asserting it would be
wrong in two directions. Gunicorn's `gthread` worker class runs a THREAD POOL
by design -- those threads are gunicorn's, they serve requests, and they are
not a background loop of ours. Meanwhile a future `sync` worker would satisfy
"one thread" while happily running a forked child.

The invariant is narrower and is the one that matters: *importing the
application adds no thread and no child process*. That is measured by
snapshotting before the import and again after, and comparing. It is true under
every worker class, it stays true if gunicorn's own threading changes, and it
becomes false on exactly the change this guard exists to catch.

WHY IT RAISES RATHER THAN WARNS.

Refusing to boot means no HTTP is served. Booting anyway means N copies of
whatever was spawned. Serving HTTP is recoverable by a deploy; a second payout
broadcast is on-chain and final (CLAUDE.md's opening: "there is no such thing
as reversible by a trade"). So the guard fails closed, and the error names the
thread or pid it found rather than saying only that something was wrong.
"""

from __future__ import annotations

import threading
from pathlib import Path


def thread_snapshot() -> set[tuple[int, str]]:
    """Every live thread in this process, as (ident, name) pairs.

    The name is carried along with the ident purely so that a violation can be
    reported in terms a reader recognizes -- `Thread-3 (poll_loop)` identifies
    the offending code, where a bare thread ident identifies nothing.
    """
    return {(t.ident, t.name) for t in threading.enumerate() if t.ident is not None}


def child_pid_snapshot() -> set[int]:
    """Every direct child process of this process, or an empty set if unknown.

    Reads /proc/<pid>/task/<tid>/children, which is Linux-specific and is only
    populated when the kernel was built with CONFIG_PROC_CHILDREN. There is no
    portable way to ask this question without a third-party dependency, and
    adding one to a host that holds wallet credentials is a worse trade than
    covering only the platform the operator actually deploys on.

    RETURNING AN EMPTY SET IS AMBIGUOUS AND THE CALLER IS TOLD SO. It means
    either "no children" or "this platform cannot say", and
    children_are_observable() is the separate question that distinguishes them.
    Collapsing the two would be CLAUDE.md rule 12's BLE001 in structural form:
    a failure that reads to the caller as a real answer. The thread half of the
    check works everywhere regardless, and it is the half that catches the
    likely mistake -- a background thread started in create_app().
    """
    children: set[int] = set()
    task_dir = Path("/proc/self/task")
    if not task_dir.is_dir():
        return children
    for tid_dir in task_dir.iterdir():
        children_file = tid_dir / "children"
        try:
            raw = children_file.read_text(encoding="utf-8")
        except OSError:
            # Checked: a task directory can vanish between listing it and
            # reading it (the thread exited), and `children` is absent on
            # kernels without CONFIG_PROC_CHILDREN. Neither is a statement
            # about child processes, so neither may add or remove one. This is
            # the one place the ambiguity above is created, and
            # children_are_observable() is how a caller learns of it.
            continue
        children.update(int(part) for part in raw.split())
    return children


def children_are_observable() -> bool:
    """True when child_pid_snapshot() is answering rather than declining.

    Exists so that a caller -- and the report in wsgi.py -- can say "no child
    processes were started" only when that was actually measured, and say "not
    observable on this platform" otherwise. CLAUDE.md rule 17: a reason to
    believe something is not the same as having checked it.
    """
    task_dir = Path("/proc/self/task")
    if not task_dir.is_dir():
        return False
    return any((tid_dir / "children").exists() for tid_dir in task_dir.iterdir())


def describe_spawn_delta(
    before_threads: set[tuple[int, str]],
    after_threads: set[tuple[int, str]],
    before_children: set[int],
    after_children: set[int],
) -> list[str]:
    """Name everything that appeared between the two snapshots.

    THIS IS THE DECISION (CLAUDE.md rule 10), and it is a pure function of four
    sets so that a test can seed it directly instead of having to arrange a
    real fork. An empty list means nothing was spawned; a non-empty list is the
    report, one human-readable line per new thread or pid.

    Threads that DISAPPEARED are deliberately not reported. A thread ending
    during import is not a background loop taking hold, and flagging it would
    make the guard cry wolf on ordinary library warm-up.
    """
    findings = [
        f"thread {name!r} (ident={ident}) was started during import"
        for ident, name in sorted(after_threads - before_threads, key=lambda pair: (pair[1], pair[0]))
    ]
    findings.extend(
        f"child process pid={pid} was forked during import"
        for pid in sorted(after_children - before_children)
    )
    return findings


def assert_nothing_spawned(
    before_threads: set[tuple[int, str]],
    after_threads: set[tuple[int, str]],
    before_children: set[int],
    after_children: set[int],
    context: str = "importing the application",
) -> None:
    """Raise RuntimeError naming what was spawned, or return None.

    Fails closed, for the reason in the module docstring: no HTTP is a deploy
    away from fixed, and a duplicate payout is not.
    """
    findings = describe_spawn_delta(before_threads, after_threads, before_children, after_children)
    if not findings:
        return
    detail = "\n".join(f"    ! {line}" for line in findings)
    raise RuntimeError(
        f"{context} started background work, and under gunicorn that runs once PER WORKER:\n"
        f"{detail}\n"
        "    The WSGI process serves HTTP and starts no workers. The three polling loops belong to\n"
        "    swap_terminal/supervisor.py, which is their reaper (CLAUDE.md rule 13). If this is a\n"
        "    deliberate change, the reaper has to arrive in the same commit -- not this guard's removal."
    )
