"""The reaper, verified by killing a real process (CLAUDE.md rule 13).

Role: test (spawns and reaps harmless local processes)
Reads: swap_terminal/supervisor.py
Writes: pid files and logs under pytest's tmp_path
Can move funds: no
Mainnet-safe: yes -- the only thing these tests ever spawn is
        `python3 -c "time.sleep(...)"`. No worker, no RPC, no chain.

WHY THE SUPERVISED COMMAND IS INJECTED.

supervisor.worker_commands() returns the real argv for the three workers, and
running those would start a payout worker against whatever database the
environment points at. So every test here passes its own command table. That
is not a paraphrase of the code under test -- start_worker(), stop_worker(),
worker_status() and main() are the real functions, and the process they
supervise is a real process with a real pid. Only the argv is harmless.

Rule 13's assertion is the point of this file: "a stop that cannot prove it
worked is not a stop. Follow it with a check that the process is gone, and
make the absence the assertion -- not the exit code of the kill." Each stop
test below asserts os.kill(pid, 0) raises ProcessLookupError afterwards. It
does not look at what stop_worker returned to decide whether the process died;
it asks the operating system.
"""

import contextlib
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest
import supervisor

# A child that ignores SIGTERM, to exercise the SIGKILL escalation. It installs
# the handler before signalling readiness so there is no window where a TERM
# would be honored by the default disposition.
SIGTERM_IGNORING_CHILD = (
    "import signal, sys, time; "
    "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
    "print('ready', flush=True); "
    "time.sleep(120)"
)

SLEEPING_CHILD = "import time; time.sleep(120)"


def _sleeper_table(source: str = SLEEPING_CHILD) -> dict[str, list[str]]:
    return {"sleeper": [sys.executable, "-c", source]}


def _assert_really_gone(pid: int) -> None:
    """The absence IS the assertion (rule 13). Ask the OS, not the killer."""
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def _reap_zombie(pid: int) -> None:
    """Clear the zombie left behind when a test's child dies.

    start_new_session=True detaches the process group but the child is still
    ours to wait on, and a zombie still answers kill(pid, 0). Tests that assert
    absence must reap first or they are asserting on a corpse.
    """
    with contextlib.suppress(ChildProcessError):
        os.waitpid(pid, 0)


def test_start_writes_a_pid_file_naming_a_live_process(tmp_path):
    result = supervisor.start_worker("sleeper", _sleeper_table()["sleeper"], tmp_path)
    try:
        assert result["outcome"] == "started"
        assert supervisor.process_alive(result["pid"])
        record = supervisor.read_pid_record(supervisor.pid_file(tmp_path, "sleeper"))
        assert record is not None
        pid, command = record
        assert pid == result["pid"]
        # The command is recorded so that stop can refuse to signal a recycled
        # pid. A pid file that stores only a number cannot tell the difference.
        assert "time.sleep" in command
    finally:
        supervisor.stop_worker("sleeper", tmp_path, grace_seconds=5.0)
        _reap_zombie(result["pid"])


def test_stop_proves_the_process_is_gone(tmp_path):
    started = supervisor.start_worker("sleeper", _sleeper_table()["sleeper"], tmp_path)
    pid = started["pid"]
    assert supervisor.process_alive(pid)

    stopped = supervisor.stop_worker("sleeper", tmp_path, grace_seconds=5.0)
    _reap_zombie(pid)

    assert stopped["outcome"] == "stopped"
    assert stopped["signals"] == ["SIGTERM"]
    _assert_really_gone(pid)
    # The pid file goes with the process. A pid file outliving its process is
    # the stale record that makes the next `start` refuse for no reason.
    assert not supervisor.pid_file(tmp_path, "sleeper").exists()


def test_stop_escalates_to_sigkill_and_still_proves_absence(tmp_path):
    started = supervisor.start_worker("sleeper", _sleeper_table(SIGTERM_IGNORING_CHILD)["sleeper"], tmp_path)
    pid = started["pid"]
    # Wait for the child to have installed its SIGTERM handler. Without this
    # the test would sometimes measure the default disposition instead.
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and not supervisor._proc_cmdline(pid):
        time.sleep(0.02)
    time.sleep(0.5)

    stopped = supervisor.stop_worker("sleeper", tmp_path, grace_seconds=1.0)
    _reap_zombie(pid)

    assert stopped["outcome"] == "stopped"
    assert stopped["signals"] == ["SIGTERM", "SIGKILL"]
    _assert_really_gone(pid)


def test_stop_with_nothing_running_says_so_instead_of_claiming_success(tmp_path):
    result = supervisor.stop_worker("sleeper", tmp_path, grace_seconds=1.0)
    # Rule 13: "treat 'skipped' plus 'success' in the same output as a defect
    # in the output." A stop that had nothing to stop must not be reported the
    # same way as a stop that reaped something.
    assert result["outcome"] == "not-running"
    assert result["signals"] == []


def test_stop_refuses_to_signal_a_recycled_pid(tmp_path):
    """The failure a pid file has that a pgrep pattern does not.

    Between a worker exiting and an operator running stop, its pid can be
    reused by anything -- including the operator's own shell. Signalling it
    would be strictly worse than failing to stop something already gone.
    """
    # A live process that is NOT the one we recorded.
    bystander = subprocess.Popen([sys.executable, "-c", SLEEPING_CHILD])
    try:
        supervisor.write_pid_record(
            supervisor.pid_file(tmp_path, "sleeper"),
            bystander.pid,
            "/usr/bin/python3 /somewhere/workers/payout_worker.py",
        )
        result = supervisor.stop_worker("sleeper", tmp_path, grace_seconds=1.0)

        assert result["outcome"] == "stale-pidfile"
        assert result["signals"] == []
        # The bystander is untouched. This is the whole point.
        assert supervisor.process_alive(bystander.pid)
        assert not supervisor.pid_file(tmp_path, "sleeper").exists()
    finally:
        bystander.send_signal(signal.SIGKILL)
        bystander.wait(timeout=10)


def test_start_twice_reports_already_running_rather_than_spawning_a_second(tmp_path):
    first = supervisor.start_worker("sleeper", _sleeper_table()["sleeper"], tmp_path)
    try:
        second = supervisor.start_worker("sleeper", _sleeper_table()["sleeper"], tmp_path)
        assert second["outcome"] == "already-running"
        assert second["pid"] == first["pid"]
    finally:
        supervisor.stop_worker("sleeper", tmp_path, grace_seconds=5.0)
        _reap_zombie(first["pid"])


def test_status_distinguishes_stopped_running_and_stale(tmp_path):
    assert supervisor.worker_status("sleeper", tmp_path)["state"] == "stopped"

    started = supervisor.start_worker("sleeper", _sleeper_table()["sleeper"], tmp_path)
    try:
        assert supervisor.worker_status("sleeper", tmp_path)["state"] == "running"
    finally:
        supervisor.stop_worker("sleeper", tmp_path, grace_seconds=5.0)
        _reap_zombie(started["pid"])

    assert supervisor.worker_status("sleeper", tmp_path)["state"] == "stopped"


def test_read_pid_record_treats_junk_as_no_record(tmp_path):
    path = supervisor.pid_file(tmp_path, "sleeper")
    path.parent.mkdir(parents=True, exist_ok=True)
    assert supervisor.read_pid_record(path) is None  # missing
    path.write_text("", encoding="utf-8")
    assert supervisor.read_pid_record(path) is None  # empty
    path.write_text("not-a-pid\n", encoding="utf-8")
    assert supervisor.read_pid_record(path) is None  # junk


# --- the CLI, end to end -----------------------------------------------------


def test_cli_start_status_stop_round_trip(tmp_path, capsys):
    table = _sleeper_table()
    assert supervisor.main(["start", "--run-dir", str(tmp_path)], commands=table) == 0
    out = capsys.readouterr().out
    assert "spawned=1" in out
    assert "already-running=0" in out
    # Rule 14: the block has to name what it is about to do, before doing it.
    assert "database" in out
    assert "NOT VERIFIED" in out  # the network claim is labeled as unverified

    pid = supervisor.read_pid_record(supervisor.pid_file(tmp_path, "sleeper"))[0]

    assert supervisor.main(["status", "--run-dir", str(tmp_path)], commands=table) == 0
    assert "running=1/1" in capsys.readouterr().out

    assert supervisor.main(["stop", "--run-dir", str(tmp_path), "--grace-seconds", "5"], commands=table) == 0
    _reap_zombie(pid)
    out = capsys.readouterr().out
    assert "stopped=1" in out
    assert "failed=0" in out
    _assert_really_gone(pid)


def test_cli_stop_on_a_cold_tree_prints_none_not_a_blank_gap(tmp_path, capsys):
    assert supervisor.main(["stop", "--run-dir", str(tmp_path)], commands=_sleeper_table()) == 0
    out = capsys.readouterr().out
    assert "not running" in out
    assert "stopped=0" in out


def test_cli_rejects_an_unknown_worker_name(tmp_path, capsys):
    assert supervisor.main(["stop", "nonesuch", "--run-dir", str(tmp_path)], commands=_sleeper_table()) == 2
    assert "unknown worker" in capsys.readouterr().out


def test_real_worker_table_names_the_three_workers_that_exist():
    """The table is the kill list. If a worker is not in it, nothing reaps it."""
    table = supervisor.worker_commands()
    assert set(table) == {"deposit_watcher", "payout_worker", "reconcile_worker"}
    for name, argv in table.items():
        script = argv[-1]
        assert script.endswith(f"workers/{name}.py")
        assert Path(script).exists(), f"{name} is in the kill list but its script is missing: {script}"


def test_a_zombie_is_not_alive(tmp_path):
    """Pin the invariant that three other tests in this file found the hard way.

    `os.kill(pid, 0)` succeeds for a zombie, so a supervisor that used only
    that signalled a dead process, waited out both grace periods and then
    reported "STILL ALIVE after SIGTERM+SIGKILL" about a process that had
    already exited. A zombie runs no code, holds no database lock and cannot
    broadcast a transaction, so it is not alive for any question this file
    asks. Without _is_zombie() in process_alive(), the last assertion here is
    False and the test fails.
    """
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.poll()  # not wait(): leaving it unreaped is the condition under test
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and not supervisor._is_zombie(child.pid):
        time.sleep(0.02)

    assert supervisor._is_zombie(child.pid), "child never became a zombie; test setup failed"
    # The naive check that is not enough:
    os.kill(child.pid, 0)
    # The one the supervisor uses:
    assert supervisor.process_alive(child.pid) is False
    child.wait(timeout=10)
