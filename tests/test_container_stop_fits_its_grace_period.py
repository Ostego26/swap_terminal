"""Does `docker compose down` have time to prove the workers are gone? Measured, not read.

Role: submodule (tests)
Reads: docker/web_workers_entrypoint.py (imported and CALLED), swap_terminal/supervisor.py,
       docker-compose.web.yml's `stop_grace_period` for the web service, and /proc
       for the absence of processes this file itself spawned
Writes: pid files and worker logs under pytest's tmp_path, nothing else
Can move funds: no. Every process it starts is `python3 -c "time.sleep(...)"`;
       nothing here imports a chain adapter, opens an RPC socket or touches a wallet
Mainnet-safe: yes, and network-independent -- there is no network call in this file

WHY IT EXISTS. Operator, 2026-10-11: "when i fucking type docker up or docker down
this should start/stop every subprocess or pid". The container's stop path was
WRITTEN to do that -- docker/web_workers_entrypoint.py stops the three workers,
proves their absence through /proc, and only then asks gunicorn to go -- and the
only thing checking it was tests/test_supervisor.py and tests/test_stack_authority.py
reading the file as TEXT. CLAUDE.md: "Never accept 'the code contains a check for X'
as evidence X is enforced."

WHAT MEASURING IT FOUND, 2026-10-11. supervisor.stop_worker() spends up to
`grace_seconds` proving absence after SIGTERM and up to `grace_seconds` again after
SIGKILL. The entrypoint ran it in a `for name in worker_commands()` loop, so the
budget was that times the number of workers:

    grace=2s   three SIGTERM-deaf workers   6.22s   3.11x one worker's grace
    projected onto the shipped 15s                  47s FOR THE WORKERS ALONE
    plus gunicorn's own 20s                         ~67s
    docker-compose.web.yml stop_grace_period        45s

So the runtime SIGKILLed the entrypoint mid-proof and the container exited reporting
success having proven nothing -- the exact failure the 45s was chosen to prevent. The
comment choosing it read the 15 as the total for all three rather than per worker.
supervisor.stop_workers_together() signals every worker and then proves absence
against ONE shared deadline: 1.05x measured, ~16s projected, ~36s with gunicorn.

THE FIXTURE'S OWN FIRST VERSION LIED, and that is recorded because it is the trap
anyone re-measuring this will walk into. A worker spawned as
`python3 -c "signal.signal(SIGTERM, SIG_IGN); sleep(600)"` is NOT deaf for the first
~50ms of its life: the interpreter is still starting and SIGTERM still has its
default disposition. The first run signaled worker one inside that window, killed it,
and reported a worker "stopped" by SIGTERM alone in 0.05s -- a 2.11x multiplier that
was an artifact of the fixture rather than a finding (rule 17). deaf_worker_argv()
below has the child touch a file AFTER installing SIG_IGN, and
start_deaf_workers() refuses to return until every one of those files exists.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "swap_terminal"))

import supervisor  # noqa: E402

# The loader-by-path lives in ONE place (rule 8). See its docstring for why it is
# public and why it leaves the environment to its caller.
from test_connect_db_refuses_a_missing_database import load_entrypoint  # noqa: E402
from test_docker_build_context import load_compose  # noqa: E402

#: The grace each measurement gives a worker. Small enough that the suite does not
#: wait 45 seconds, large enough to dwarf the poll interval so the multiplier is the
#: thing being measured rather than scheduler noise.
MEASUREMENT_GRACE_SECONDS = 1.5

#: A multiplier at or above this says the grace is being spent per worker rather than
#: once. Measured: 1.05x for the group stop, 3.11x for the serial loop it replaced --
#: so this sits between them with room on both sides for a loaded host.
SERIAL_MULTIPLIER_CEILING = 2.0


def deaf_worker_argv(ready_file: Path) -> list[str]:
    """A worker that IGNORES SIGTERM and says so by touching `ready_file`.

    The order inside the child is the whole point and must not be rearranged: the
    handler is installed BEFORE the file is touched, so the file's existence is proof
    that SIGTERM is already being ignored. Touch-then-install would reintroduce the
    race documented in this module's header.
    """
    return [
        sys.executable,
        "-c",
        "import signal, time, pathlib, sys;"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN);"
        "pathlib.Path(sys.argv[1]).write_text('ready');"
        "time.sleep(600)",
        str(ready_file),
    ]


def start_deaf_workers(run_dir: Path, names: list[str]) -> None:
    """Start one SIGTERM-deaf process per name and do not return until all are deaf.

    Raises rather than returning early on timeout: a measurement taken against a
    worker that had not yet installed its handler is the artifact this module's
    header describes, and a fixture that degrades quietly into it would report a
    passing budget for a stop that is still serial.
    """
    for name in names:
        result = supervisor.start_worker(name, deaf_worker_argv(run_dir / f"{name}.ready"), run_dir)
        assert result["outcome"] == "started", f"{name}: {result}"
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if all((run_dir / f"{name}.ready").exists() for name in names):
            return
        time.sleep(0.02)
    missing = [name for name in names if not (run_dir / f"{name}.ready").exists()]
    pytest.fail(f"workers never confirmed SIG_IGN, so no measurement is possible: {missing}")


def measured_multiplier(run_dir: Path, names: list[str]) -> float:
    """Stop every name at once and return the elapsed time as a multiple of one grace."""
    start_deaf_workers(run_dir, names)
    started = time.monotonic()
    results = supervisor.stop_workers_together(
        names, run_dir, grace_seconds=MEASUREMENT_GRACE_SECONDS,
    )
    elapsed = time.monotonic() - started
    # Every one of them has to actually be gone, or the elapsed time is measuring a
    # stop that did not happen. SIGKILL is what gets there, since they ignore SIGTERM.
    for result in results:
        assert result["outcome"] == "stopped", f"{result['worker']} was not proven gone: {result}"
        assert "SIGKILL" in result["signals"], f"{result['worker']} claims to have honored a signal it ignores: {result}"
    return elapsed / MEASUREMENT_GRACE_SECONDS


def compose_stop_grace_seconds() -> float:
    """The web service's `stop_grace_period`, in seconds, from the compose file itself."""
    compose = load_compose(REPOSITORY_ROOT / "docker-compose.web.yml")
    raw = compose["services"]["web"]["stop_grace_period"]
    assert isinstance(raw, str) and raw.endswith("s"), f"unhandled duration form {raw!r}"
    return float(raw[:-1])


def test_the_grace_is_spent_once_for_the_whole_set_not_once_per_worker(tmp_path):
    """Three deaf workers must cost about ONE grace, not three.

    MUTATION: put the serial loop back --
    `[supervisor.stop_worker(n, run_dir, g) for n in names]` -- and this fails at
    3.11x against a ceiling of 2.0x.
    """
    names = list(supervisor.worker_commands())
    assert len(names) > 1, "a one-worker set cannot distinguish serial from grouped"
    multiplier = measured_multiplier(tmp_path, names)
    assert multiplier < SERIAL_MULTIPLIER_CEILING, (
        f"{len(names)} workers took {multiplier:.2f}x one worker's grace "
        f"({MEASUREMENT_GRACE_SECONDS}s). At or above {SERIAL_MULTIPLIER_CEILING}x the grace is "
        f"being spent per worker, which is what overran the compose stop_grace_period."
    )


def test_the_container_stop_fits_inside_its_compose_grace_period(tmp_path, monkeypatch):
    """The measured worst case at the SHIPPED constants must fit in `stop_grace_period`.

    This is the assertion the overrun actually needed, and it is made against three
    numbers in three different files rather than against any comment: the multiplier
    is measured here, the two grace constants come from the entrypoint module, and the
    budget comes from docker-compose.web.yml. Raise either constant, add a fourth
    worker, or lower the compose grace period, and this fails with the arithmetic in
    the message -- which is what the prose comment at docker-compose.web.yml's
    stop_grace_period could not do, having got it wrong.
    """
    # RUN_DIR and both grace constants are read at IMPORT in that module, so the
    # environment has to be right before load_entrypoint() rather than after.
    monkeypatch.setenv("ST_WORKER_RUN_DIR", str(tmp_path / "run"))
    # DELETED rather than assumed absent. The projection below is about the SHIPPED
    # defaults; a host that happened to export either variable would make this test
    # pass or fail on its own environment instead of on the tree.
    monkeypatch.delenv("ST_WORKER_GRACE_SECONDS", raising=False)
    monkeypatch.delenv("ST_GUNICORN_GRACE_SECONDS", raising=False)
    entrypoint = load_entrypoint()

    names = list(supervisor.worker_commands())
    multiplier = measured_multiplier(tmp_path, names)

    workers = multiplier * entrypoint.WORKER_GRACE_SECONDS
    worst_case = workers + entrypoint.GUNICORN_GRACE_SECONDS
    budget = compose_stop_grace_seconds()
    assert worst_case < budget, (
        f"the container's stop needs {worst_case:.0f}s and compose SIGKILLs it at {budget:.0f}s. "
        f"{len(names)} workers measured at {multiplier:.2f}x a grace of "
        f"{entrypoint.WORKER_GRACE_SECONDS}s = {workers:.0f}s, then gunicorn's "
        f"{entrypoint.GUNICORN_GRACE_SECONDS}s. A stop killed mid-proof exits reporting "
        f"success having proven nothing, which is what stop_grace_period exists to prevent."
    )


def test_the_entrypoint_stops_every_worker_in_one_call(tmp_path, monkeypatch):
    """stop_workers() must hand ALL the names to ONE group stop.

    Asserted on the recorded call rather than on the source text, because the
    budget above is only kept by the entrypoint actually using the group path: a
    future edit back to a per-name loop would leave both the function and its
    docstring looking right and the budget silently blown.
    """
    monkeypatch.setenv("ST_WORKER_RUN_DIR", str(tmp_path / "run"))
    entrypoint = load_entrypoint()

    calls = []

    def recording_stop(names, run_dir, grace_seconds=None):
        calls.append(list(names))
        return [{"worker": n, "outcome": "not-running", "pid": None, "signals": []} for n in names]

    monkeypatch.setattr(entrypoint.supervisor, "stop_workers_together", recording_stop)
    monkeypatch.setattr(entrypoint.supervisor, "unaccounted_lines", lambda *_a, **_k: [])
    monkeypatch.setattr(entrypoint.supervisor, "unaccounted_workers", lambda *_a, **_k: {})

    assert entrypoint.stop_workers() is True
    expected = list(supervisor.worker_commands())
    assert calls == [expected], (
        f"expected one group call carrying every worker, got {calls}. "
        "One call per name is the serial budget this module measured at 3.11x."
    )
