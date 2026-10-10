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
import importlib
import os
import signal
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest
import supervisor
from conftest import poisoned_rpc_table
from db import SCHEMA
from workers import common
from workers.common import endpoint_lines

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

    # read_pid_record() returns None for a pid file that is missing, empty or
    # junk, which here would mean `spawned=1` was printed over a worker whose
    # pid nothing recorded -- rule 13's "a stop that cannot prove it worked",
    # one step earlier. Subscripting it directly died with `TypeError:
    # 'NoneType' object is not subscriptable` and said none of that (pyright
    # reportOptionalSubscript, 2026-10-09).
    record = supervisor.read_pid_record(supervisor.pid_file(tmp_path, "sleeper"))
    assert record is not None, (
        "start printed spawned=1 but left no readable pid file for 'sleeper', so the process it "
        "spawned cannot be named, checked, or reaped"
    )
    pid = record[0]

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


def test_the_start_banner_names_every_chain_the_workers_will_watch():
    """The gap this file's banner had on 2026-10-01, pinned so it cannot return.

    MEASURED, from the live host, while the operator was rehearsing a
    devnet-SOL -> testnet-GRC swap. `python3 swap_terminal/supervisor.py start`
    printed BTC, LTC and GRC and NOT ONE WORD about SOL or XRP -- immediately
    above its own line warning that `a payout worker CAN broadcast`. The
    deposit_watcher it was about to spawn is the process that would or would
    not see that Solana deposit, and the banner read identically whether or not
    the supervisor had inherited SOL_RPC_URL at all.

    The cause was rule 8: endpoint_summary() had its own
    `for asset in ("BTC", "LTC", "GRC"):` loop, a second copy of what
    workers/common.endpoint_lines() already did, written when three chains was
    all there was. Two chains arrived in the other copy and not in this one.

    So the assertion is the stronger one rather than "SOL appears": the
    supervisor's banner must CONTAIN endpoint_lines() verbatim. A test that
    only looked for the string "SOL" would pass again the moment a sixth chain
    is added to one copy and not the other, which is the identical defect one
    chain later.
    """
    summary = supervisor.endpoint_summary()
    chain_lines = endpoint_lines()

    assert chain_lines, "endpoint_lines() returned nothing; there would be nothing to check"
    for line in chain_lines:
        assert line in summary, (
            f"the supervisor banner is missing a chain line that the workers print:\n"
            f"  missing: {line!r}\n  banner:\n" + "\n".join(f"    {s}" for s in summary)
        )

    # And every chain the application can be configured for is named, so that a
    # chain cannot be silently absent from the banner printed above "about to
    # spawn". Named explicitly, because the loop above would be satisfied by
    # two implementations that agree on being wrong together.
    text = "\n".join(summary)
    for asset in ("BTC", "LTC", "GRC", "SOL", "XRP"):
        assert any(line.strip().startswith(asset) for line in summary), (
            f"{asset} is absent from the start banner:\n{text}"
        )


def test_the_start_banner_still_says_the_database_and_refuses_to_claim_a_network():
    """The two things endpoint_summary() says that endpoint_lines() does not.

    Delegating the chain rendering must not drop them. The database path is
    what makes the "stop now if this is pointed at a funded mainnet wallet"
    warning one line above actionable, and the network disclaimer is rule 17
    in output form: this process opens no socket, so it cannot know which
    network a daemon is on.
    """
    summary = supervisor.endpoint_summary()
    text = "\n".join(summary)

    assert summary[0].strip().startswith("database"), f"the database must lead the banner:\n{text}"
    assert str(supervisor.Config.DB_PATH) in summary[0]
    assert "NOT VERIFIED" in text


def test_the_start_banner_never_prints_a_credential(monkeypatch):
    """`user` and `password` are one key away from `host` and `port`.

    Asserted on the supervisor's own banner and not only on the workers',
    because this is the banner an operator pastes into a chat window when
    something is wrong -- which is exactly how a leaked RPC password would
    travel. endpoint_summary() formats no credential today; this is what keeps
    a future `**rpc` in an f-string from being a silent one.
    """
    # ONE DEFINITION, SHARED WITH tests/test_worker_reporting.py's identical check,
    # and monkeypatch rather than a try/finally: a restore in `finally` does not run
    # if the setup above it raises, and the thing left behind would be a Config.RPC
    # full of canaries for every test after this one in the same process.
    monkeypatch.setattr(supervisor.Config, "RPC", poisoned_rpc_table(supervisor.Config.RPC))
    text = "\n".join(supervisor.endpoint_summary())

    assert "canary-rpc-password" not in text
    assert "canary-rpc-user" not in text


# A worker that dies the instant it is started, which is what an ImportError, a
# SWAP_DB_PATH that cannot be opened, or a config error raised at
# class-definition time all look like from the supervisor's side. Exit code 3
# rather than 1 so the assertions below prove the REAL code is reported and not
# a hardcoded one.
DIES_IMMEDIATELY = "import sys; sys.stderr.write('ImportError: no module named nonesuch\\n'); sys.exit(3)"


def _dying_table() -> dict[str, list[str]]:
    return {"dier": [sys.executable, "-c", DIES_IMMEDIATELY]}


def test_start_reports_a_worker_that_died_the_instant_it_was_spawned(tmp_path, capsys):
    """Rule 13 pointed at `start` instead of `stop`: verify the artifact.

    FOUND 2026-10-01, while the operator was restarting the three workers to
    pick up SOL_RPC_URL. start_worker() returned `"started"` because
    subprocess.Popen() RETURNED, and nothing asked a second time. A worker that
    exits immediately produced

        started           deposit_watcher pid=3998319 log=.../deposit_watcher.log

    and a pid file naming a dead process -- three green lines beside a database
    nothing was polling. stop_worker() has always made the ABSENCE of the
    process the assertion rather than the kill's exit code; this is the same
    assertion pointed the other way.

    Four things are asserted, because the previous behavior satisfied a test
    that only checked the word DIED appeared:

      the outcome is marked, and distinctly (rule 14)
      the REAL exit code is printed -- 3, not a hardcoded 1
      the pid file is GONE, because a file naming a dead process is the stale
          record rule 13 warns about
      main() returns NON-ZERO, so a launcher chaining off `start` does not
          carry on as though the workers were up
    """
    assert supervisor.main(["start", "--run-dir", str(tmp_path)], commands=_dying_table()) == 1
    out = capsys.readouterr().out

    assert "DIED" in out, f"a dead worker must not be reported as started:\n{out}"
    assert "exited 3" in out, f"the real exit code must be printed, not a hardcoded one:\n{out}"
    assert "died=1" in out
    assert "spawned=0" in out
    assert not supervisor.pid_file(tmp_path, "dier").exists(), (
        "the pid file names a process that is not running; a later `stop` would read it"
    )
    # Rule 14: say what happened where the operator is already looking, rather
    # than leaving them to go and find the log.
    assert "ImportError: no module named nonesuch" in out


def test_start_confirms_a_healthy_worker_is_still_there_rather_than_assuming_it(tmp_path, capsys):
    """The other half, which a test for the dying case alone would not cover.

    A confirm step that reported DIED for everything would pass the test above.
    This asserts the live worker is still reported as started, that the line
    says the check was made, and that the counts are the other way round.
    """
    table = _sleeper_table()
    try:
        assert supervisor.main(["start", "--run-dir", str(tmp_path)], commands=table) == 0
        out = capsys.readouterr().out
        assert "started" in out
        assert "DIED" not in out
        assert "died=0" in out
        assert "spawned=1" in out
        # The line has to say the question was asked of the operating system,
        # because "started" on its own is exactly what it printed before.
        assert "asked of the OS" in out
    finally:
        pid_record = supervisor.read_pid_record(supervisor.pid_file(tmp_path, "sleeper"))
        if pid_record:
            supervisor.stop_worker("sleeper", tmp_path, grace_seconds=5)
            _reap_zombie(pid_record[0])


def test_confirm_spawned_sleeps_nothing_when_nothing_was_spawned():
    """`start` on three already-running workers must not pay the settle.

    The operator's own transcript of 2026-10-01 has exactly this case --
    `spawned=0  already-running=3` -- and it reported `in 0.0µfn (0.0s)`. A
    settle slept unconditionally would have made a command that did nothing
    take a second, which is rule 14's "did nothing must not look like did work"
    arriving as a duration.
    """
    already = [{"worker": "a", "outcome": "already-running", "pid": 1}]
    started = time.monotonic()
    assert supervisor.confirm_spawned(already) is already
    assert time.monotonic() - started < 0.2


# --- can a payout actually send ----------------------------------------------

def test_the_start_banner_says_a_grc_payout_cannot_unlock_when_no_passphrase_is_set(monkeypatch):
    """The gap that cost three live rehearsals on 2026-10-01.

    The operator ran a devnet SOL -> testnet GRC swap three times. All three
    times the whole pipeline worked -- memo attributed, deposit credited, swap
    advanced, payout worker claimed it -- and all three times the GRC leg died on

        GRIDCOIN_WALLET_PASSPHRASE is not set in this process's environment

    because the supervisor had been started from a shell without it. And all
    three times this banner said, two lines below, `a payout worker CAN
    broadcast`. False in the direction that cost the rounds: it could not
    broadcast at all. The banner warned about the danger of SUCCEEDING and said
    nothing about a guaranteed failure.

    A worker inherits the shell that spawned it and nothing downstream can see
    which shell that was, so this is exactly rule 14's "echo the parameters that
    decide the answer".

    MUTATION: drop unlock_readiness_lines() from endpoint_summary() and this
    fails while every other banner test passes -- the state of three rehearsals.
    """
    monkeypatch.delenv("GRIDCOIN_WALLET_PASSPHRASE", raising=False)
    # monkeypatch.setitem RATHER THAN update-and-restore, which is the idiom
    # tests/test_swap_readiness.py already uses on this same table. It drops the
    # hand-maintained `original` dict -- a copy of three key names that had to stay
    # in step with the three being set -- and it restores even when the body raises
    # before `finally` is reached.
    entry = supervisor.Config.RPC["GRC"]
    monkeypatch.setitem(entry, "port", 25715)
    monkeypatch.setitem(entry, "user", "fixture-user")
    monkeypatch.setitem(entry, "password", "fixture-auth")
    text = "\n".join(supervisor.endpoint_summary())

    assert "GRIDCOIN_WALLET_PASSPHRASE IS NOT SET" in text
    assert "WILL refuse before sending" in text
    # And it says what to do about it, where the operator is looking.
    assert "shell that starts this process" in text


def test_the_banner_never_prints_the_passphrase_or_its_length(monkeypatch):
    """The one thing this line must never do, asserted rather than intended.

    A banner that helpfully echoed the value would put a wallet passphrase into
    every log the supervisor writes and into every terminal paste. The length is
    excluded too: it is not the secret but it narrows one, and nothing on screen
    needs it.
    """
    monkeypatch.setenv("GRIDCOIN_WALLET_PASSPHRASE", "canary-wallet-passphrase")
    # monkeypatch.setitem RATHER THAN update-and-restore, which is the idiom
    # tests/test_swap_readiness.py already uses on this same table. It drops the
    # hand-maintained `original` dict -- a copy of three key names that had to stay
    # in step with the three being set -- and it restores even when the body raises
    # before `finally` is reached.
    entry = supervisor.Config.RPC["GRC"]
    monkeypatch.setitem(entry, "port", 25715)
    monkeypatch.setitem(entry, "user", "fixture-user")
    monkeypatch.setitem(entry, "password", "fixture-auth")
    text = "\n".join(supervisor.endpoint_summary())

    assert "canary-wallet-passphrase" not in text
    assert "24" not in text.split("GRC payout unlock")[1].split("\n")[0], "not even the length"
    # It reports PRESENCE and deliberately does not claim correctness: "set" and
    # "works" are different claims and a wrong passphrase still fails at the send.
    assert "IS set" in text
    assert "NOT a claim that it is the right passphrase" in text


def test_an_unconfigured_grc_gets_no_unlock_line_at_all(monkeypatch):
    """A passphrase warning for a chain nobody set up is cried-wolf noise.

    services/payout_service.py already fixed this shape once, for XRP's
    get_balance() refusal printing every ten seconds. A chain with no adapter
    watches nothing and pays nothing, so it has no unlock to report.
    """
    monkeypatch.delenv("GRIDCOIN_WALLET_PASSPHRASE", raising=False)
    entry = supervisor.Config.RPC["GRC"]
    # monkeypatch.setitem RATHER THAN update-and-restore: it drops the
    # hand-maintained `original` dict -- a copy of three key names that had to stay
    # in step with the three being set -- and it restores even when the body raises
    # before `finally` is reached. Same idiom as tests/test_swap_readiness.py on
    # this same table.
    monkeypatch.setitem(entry, "port", 0)
    monkeypatch.setitem(entry, "user", "")
    monkeypatch.setitem(entry, "password", "")
    text = "\n".join(supervisor.endpoint_summary())

    assert "payout unlock" not in text
    assert "not configured" in text, "and it still says the chain is unconfigured"


def test_the_spawn_warning_does_not_contradict_the_unlock_line(monkeypatch, capsys, tmp_path):
    """Two sentences in one safety block must not disagree. MEASURED 2026-10-01.

    The commit that added the unlock line produced this, four lines apart:

        GRC payout unlock  *** GRIDCOIN_WALLET_PASSPHRASE IS NOT SET *** so
                           every GRC payout WILL refuse before sending ...
        about to spawn     a payout worker CAN broadcast. Stop now if this
                           database is pointed at a funded mainnet wallet.

    The first was written to fix a banner that was silent about a guaranteed
    failure; the second was left asserting the opposite. An operator reading top
    to bottom is told the payout cannot send and then that it can, which is worse
    than either sentence alone -- they now have to work out which one the program
    believes, in the block whose whole job is to be read before money moves.

    Asserted through main() rather than on spawn_warning() alone, because the
    defect was the two lines COEXISTING and only the rendered block shows that.

    MUTATION: restore the unconditional sentence and this fails on the
    contradiction while every other banner test passes -- the shipped state.
    """
    monkeypatch.delenv("GRIDCOIN_WALLET_PASSPHRASE", raising=False)
    entry = supervisor.Config.RPC["GRC"]
    # monkeypatch.setitem RATHER THAN update-and-restore: it drops the
    # hand-maintained `original` dict -- a copy of three key names that had to stay
    # in step with the three being set -- and it restores even when the body raises
    # before `finally` is reached. Same idiom as tests/test_swap_readiness.py on
    # this same table.
    monkeypatch.setitem(entry, "port", 25715)
    monkeypatch.setitem(entry, "user", "fixture-user")
    monkeypatch.setitem(entry, "password", "fixture-auth")
    # _dying_table()'s worker exits immediately, so this spawns and reaps a
    # harmless `python3 -c` and nothing is left behind -- the banner is what
    # is under test, not the spawn. main() returns 1 for the death, which is
    # confirm_spawned() doing its job and not a failure of this test.
    assert supervisor.main(["start", "--run-dir", str(tmp_path)], commands=_dying_table()) == 1
    out = capsys.readouterr().out

    assert "IS NOT SET" in out, "setup: the unlock line must be present for there to be a contradiction"
    assert "a payout worker CAN broadcast. Stop now" not in out, (
        "the unconditional sentence contradicts the unlock line four lines above it:\n" + out
    )
    # It still carries the mainnet caution, because a chain needing no unlock IS
    # unaffected by a Gridcoin passphrase -- "nothing can send" would be its own
    # wrong line.
    assert "funded mainnet wallet" in out
    assert "those payouts refuse" in out


def test_the_spawn_warning_is_the_original_one_when_every_unlock_is_ready(monkeypatch):
    """The other half. A version that always hedged would pass the test above.

    When the passphrase IS set the danger is the real one -- the worker will
    broadcast -- and the warning must say so without qualification.
    """
    monkeypatch.setenv("GRIDCOIN_WALLET_PASSPHRASE", "canary-wallet-passphrase")
    entry = supervisor.Config.RPC["GRC"]
    # monkeypatch.setitem RATHER THAN update-and-restore: it drops the
    # hand-maintained `original` dict -- a copy of three key names that had to stay
    # in step with the three being set -- and it restores even when the body raises
    # before `finally` is reached. Same idiom as tests/test_swap_readiness.py on
    # this same table.
    monkeypatch.setitem(entry, "port", 25715)
    monkeypatch.setitem(entry, "user", "fixture-user")
    monkeypatch.setitem(entry, "password", "fixture-auth")
    warning = supervisor.spawn_warning()

    # PROPERTIES, NOT THE LITERAL SENTENCE. This asserted the exact string, which
    # broke the moment the warning started naming WHICH chains are payable
    # (2026-10-01) -- an improvement the test scored as a regression. A literal
    # assertion on a prose line pins the wording rather than the claim, and the
    # claim is what matters: unqualified "can broadcast", no "cannot", and the
    # mainnet caution present.
    assert "CAN broadcast" in warning
    assert "CANNOT" not in warning, "a version that always hedged would pass the test above"
    assert "GRC" in warning, "naming the payable chain is what makes the caution checkable"
    assert "mainnet wallet" in warning
    assert "canary-wallet-passphrase" not in warning


# --- rule 13's second half: prove the absence, not the pid file's absence -------
#
# MEASURED ON THE OPERATOR'S HOST 2026-10-01. Three workers were observed cycling
# (deposit_watcher cycle=57, payout_worker cycle=106) against the wrong database.
# Twenty minutes later `stop` reported
#
#     not running       deposit_watcher  <- nothing to stop; pid file was stale
#     not running       payout_worker  <- nothing to stop; pid file was stale
#     not running       reconcile_worker  <- nothing to stop; pid file was stale
#     summary           stopped=0  failed=0  untouched=3
#
# Every line is TRUE of the pid in each file. `untouched=3` reads as "nothing
# needed doing", and whether a worker was still alive under some other pid was not
# established either way. That is the Mammon incident rule 13 is written from --
# "daemon caches that no pid file in warbot.sh names, which is why full_stop has to
# pgrep -f for them."
#
# Signalling still goes only to a pid read from a pid file and checked against
# /proc; this file's header is right that a pattern is the wrong thing to signal
# on. What is added is VERIFICATION, which reports and never signals.


def _worker_stand_in(marker: str):
    """A harmless process whose argv contains a worker's script path.

    A real worker would open a database and talk to chains. What the scan matches
    on is the script path in /proc/<pid>/cmdline, so a sleep carrying that path in
    argv exercises the identical code path and nothing else.
    """
    # No noqa: S603 is not enabled for tests/, and an unused directive is noise
    # claiming a check nobody needed (rule 19). The argv is sys.executable, a
    # literal, and a path from worker_commands() -- no shell, no user string.
    return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)", marker])


def test_no_orphan_prints_none_rather_than_nothing(tmp_path):
    """Rule 14: "no orphans" and "this check did not run" must not share a blank gap.

    The sentence also has to say the scan HAPPENED, because that is what upgrades
    the pid-file outcome above it from reported to proven.
    """
    lines = supervisor.unaccounted_lines(list(supervisor.worker_commands()), tmp_path)
    assert len(lines) == 1
    assert "none" in lines[0]
    assert "/proc scanned" in lines[0]
    # "verdict", not "stop": the same block prints under `status`, where no stop
    # happened, and asserting the word "stop" is what let that wrong sentence ship.
    assert "PROVEN rather than merely reported" in lines[0]
    assert "PROVEN stop" not in lines[0], (
        "this block prints under status too, where there was no stop to prove"
    )


def test_a_live_worker_with_no_pid_file_is_reported_as_an_orphan(tmp_path):
    """THE CASE `untouched=3` COULD NOT SEE."""
    names = list(supervisor.worker_commands())
    marker = supervisor.worker_commands()["payout_worker"][-1]
    process = _worker_stand_in(marker)
    try:
        time.sleep(0.5)
        found = supervisor.unaccounted_workers(names, tmp_path)
        assert "payout_worker" in found
        assert process.pid in found["payout_worker"]

        lines = supervisor.unaccounted_lines(names, tmp_path)
        assert any("*** ORPHAN ***" in line for line in lines)
        assert any(str(process.pid) in line for line in lines)
        assert any("`stop` did NOT touch it" in line for line in lines)
    finally:
        process.kill()
        process.wait()


def test_a_process_a_pid_file_names_is_not_an_orphan(tmp_path):
    """The whole point of "unaccounted": a supervised worker must not be reported.

    Otherwise every healthy `status` prints an orphan warning, which is the
    cried-wolf noise that gets a check ignored -- and this file has already fixed
    that shape once, for XRP's get_balance().
    """
    names = list(supervisor.worker_commands())
    marker = supervisor.worker_commands()["payout_worker"][-1]
    process = _worker_stand_in(marker)
    try:
        time.sleep(0.5)
        assert supervisor.unaccounted_workers(names, tmp_path), "the premise: it IS found with no pid file"

        (tmp_path / "payout_worker.pid").write_text(f"{process.pid}\n{sys.executable} {marker}\n")
        assert supervisor.unaccounted_workers(names, tmp_path) == {}
        assert "none" in supervisor.unaccounted_lines(names, tmp_path)[0]
    finally:
        process.kill()
        process.wait()


def test_the_scan_never_signals_anything(tmp_path):
    """It REPORTS. Killing a process this supervisor did not start is the operator's
    decision (rule 16), not a reporting function's -- and the process this scan
    finds may be a worker somebody started deliberately from another shell.
    """
    names = list(supervisor.worker_commands())
    marker = supervisor.worker_commands()["payout_worker"][-1]
    process = _worker_stand_in(marker)
    try:
        time.sleep(0.5)
        supervisor.unaccounted_workers(names, tmp_path)
        supervisor.unaccounted_lines(names, tmp_path)
        time.sleep(0.3)
        assert process.poll() is None, "the scan killed a process it was only supposed to report"
    finally:
        process.kill()
        process.wait()


def test_the_stop_summary_counts_orphans_and_exits_nonzero(tmp_path, capsys):
    """The one line most likely to be the only one read.

    `stopped=0 failed=0 untouched=3` cannot say "and one is still running", which
    is rule 13's "'skipped' plus 'success' in the same output is a defect in the
    OUTPUT". The exit code moves too: a stop that left something running has not
    succeeded.
    """
    names = ["payout_worker"]
    marker = supervisor.worker_commands()["payout_worker"][-1]
    process = _worker_stand_in(marker)
    try:
        time.sleep(0.5)
        code = supervisor.command_stop(names, tmp_path, 0.1)
        out = capsys.readouterr().out

        assert "orphans=1" in out
        assert "an orphan survived this stop" in out
        assert "*** ORPHAN ***" in out
        assert code != 0, "a stop that left a worker running must not report success"
    finally:
        process.kill()
        process.wait()


def test_a_clean_stop_still_exits_zero_and_says_the_scan_ran(tmp_path, capsys):
    """The ratchet guard: the new check must not fail an ordinary quiet stop."""
    code = supervisor.command_stop(["payout_worker"], tmp_path, 0.1)
    out = capsys.readouterr().out
    assert code == 0
    assert "orphans=0" in out
    assert "an orphan survived" not in out
    assert "/proc scanned" in out


def test_a_log_tail_is_not_mistaken_for_a_worker(tmp_path):
    """MUTATION-FOUND. Matching the worker NAME instead of its script path survived.

    `tail -f payout_worker.log`, an editor with the file open, and a grep for the
    name all carry "payout_worker" in their command line and are not workers. A
    false orphan report is the cried-wolf noise that gets a check ignored -- a
    shape this file has already fixed once, for XRP's get_balance() -- and here it
    would appear on every `status` an operator runs while watching a log.

    So the match is on the SCRIPT PATH from worker_commands(), and this is the test
    that distinguishes the two.
    """
    names = list(supervisor.worker_commands())
    marker = supervisor.worker_commands()["payout_worker"][-1]
    # The log file beside the script: the same name, one extension apart, which is
    # exactly what an operator has open in another terminal. This is the SUBSTRING
    # case -- argv[0] is a python here, so only the exact-element match rejects it.
    process = _worker_stand_in(f"{marker}.log")
    try:
        time.sleep(0.5)
        assert process.poll() is None, "the stand-in died; this test would pass on nothing"
        argv = supervisor._proc_argv(process.pid)
        assert f"{marker}.log" in argv, f"the fixture must carry the near-miss path: {argv}"
        assert marker not in argv, "the fixture must NOT carry the exact path, or it is a real match"

        assert supervisor.unaccounted_workers(names, tmp_path) == {}, (
            "a process merely NAMING a worker was reported as that worker"
        )
    finally:
        process.kill()
        process.wait()


def test_a_non_python_holding_the_script_path_is_not_a_worker(tmp_path):
    """MUTATION-FOUND, and the near-miss test above could not catch it.

    Removing the `"python" not in Path(argv[0]).name` check survived the suite,
    because every stand-in was a python. The case it guards is an editor, a grep or
    a tail holding the worker's own source path as an EXACT argv element:

        vim    /.../swap_terminal/workers/payout_worker.py
        grep x /.../swap_terminal/workers/payout_worker.py

    Both satisfy the element match. Only the interpreter check rejects them, and
    without it every `status` an operator runs with the source open would print
    *** ORPHAN ***.

    /bin/sh with the path as a trailing argument, NOT `exec -a vim`: dash has no
    `exec -a`, and the first version of this fixture used it, so /bin/sh printed
    "exec: -a: not found", the process died instantly, _proc_argv() returned [],
    and the test passed on a DEAD process while the mutation survived it. Third
    fixture in this suite to be green for the wrong reason, which is why the
    liveness and shape assertions below come before the one under test.
    """
    names = list(supervisor.worker_commands())
    marker = supervisor.worker_commands()["payout_worker"][-1]
    process = subprocess.Popen(["/bin/sh", "-c", "sleep 30", marker])
    try:
        time.sleep(0.5)
        assert process.poll() is None, "the stand-in died; this test would pass on nothing"
        argv = supervisor._proc_argv(process.pid)
        assert marker in argv[1:], f"the fixture must hold the script path as an argv element: {argv}"
        assert "python" not in Path(argv[0]).name, f"the fixture must not be a python: {argv}"

        assert supervisor.unaccounted_workers(names, tmp_path) == {}, (
            "a non-python holding the script path as an argument was reported as a live worker"
        )
    finally:
        process.kill()
        process.wait()


def test_without_proc_the_scan_says_it_could_not_look(tmp_path, monkeypatch):
    """MUTATION-FOUND. Removing the /proc guard survived, because the suite has /proc.

    "Nothing found" and "could not look" are different answers, and only the first
    makes a stop proven. On a platform with no /proc the honest line says the
    pid-file outcome is the ONLY evidence -- the same distinction show_swap.py
    draws between an empty table and a missing one.
    """
    monkeypatch.setattr(supervisor, "PROC_DIR", tmp_path / "no-proc-here")
    assert supervisor.unaccounted_workers(list(supervisor.worker_commands()), tmp_path) == {}

    lines = supervisor.unaccounted_lines(list(supervisor.worker_commands()), tmp_path)
    assert len(lines) == 1
    assert "NOT ESTABLISHED" in lines[0]
    assert "no /proc" in lines[0]
    assert "only evidence" in lines[0]
    assert "none" not in lines[0], "could-not-look must never render as no-orphans"


# --- the spawn warning must not claim a capability the process lacks -----------


def test_the_spawn_warning_says_nothing_can_be_paid_when_nothing_can(monkeypatch):
    """MEASURED ON THE OPERATOR'S HOST 2026-10-01, one screen after NOT READY.

    With GRC_RPC_PASS unset, chains/registry had built exactly one adapter -- SOL
    -- and SOL is deliberately never a TO asset (config.ALLOWED_PAIRS carries
    ("SOL","GRC") and not the reverse, because chains/solana.py cannot sign). So
    nothing could be paid out at all, and the banner printed, immediately above
    spawning three workers:

        about to spawn    a payout worker CAN broadcast. Stop now if this
                          database is pointed at a funded mainnet wallet.

    THE SAME DEFECT THIS FUNCTION WAS WRITTEN TO FIX, ONE CASE OVER. It derives
    its sentence from unlock_readiness_lines(), which reports a missing PASSPHRASE
    for a CONFIGURED payout chain. Asked about a process with no payout chain at
    all, that function correctly returns nothing -- existence is not its question
    -- so "no blockers" and "nothing to block" rendered identically.
    """
    monkeypatch.setattr(supervisor, "build_adapters", lambda rpc: {"SOL": CannotSign()}, raising=False)
    monkeypatch.setattr(
        "chains.registry.build_adapters", lambda rpc: {"SOL": CannotSign()}
    )
    warning = supervisor.spawn_warning()

    assert "CANNOT BROADCAST ANYTHING" in warning
    assert "CAN broadcast" not in warning.replace("CANNOT BROADCAST ANYTHING", ""), (
        "the all-clear wording must not survive anywhere in the no-payable case"
    )
    assert "adapters built: SOL" in warning
    assert "nothing retries" in warning, "the consequence is what makes this actionable"


class CanSign:
    """An adapter that can broadcast. A bare placeholder stood in for this until 2026-10-02.

    These fixtures used a plain object because payable_assets() took asset NAMES and asked
    nothing of the adapter. It now takes the adapters and reads
    chains/registry.why_cannot_pay_out(), because the banner was naming XRP as payable on
    the operator's host while XRP holds no signing key. The VALUE is now the question.

    chains/base.py reads `getattr(adapter, "can_spend", False)` -- fail-closed on purpose
    -- so a placeholder with no attributes is an adapter that CANNOT pay, and a test
    asserting GRC gets named was asserting a PASS for one.
    """

    can_spend = True
    payout_refusal = ""


class CannotSign:
    """An adapter with no signing key, which is what SOL and XRP actually are.

    Spelled out rather than left as a bare placeholder in the nothing-payable test: that
    test passed for the right reason before and would now pass for a second reason too,
    and a test that would pass either way is not pinning which.
    """

    can_spend = False
    payout_refusal = "holds no signing key, so a payout raises"


def test_the_spawn_warning_names_the_payable_chains_when_there_are_some(monkeypatch):
    """The danger is real when a chain CAN send, and the sentence has to name which.

    "a payout worker CAN broadcast" with no chain named was the original wording,
    and it cannot be checked against anything. Naming the chains makes the mainnet
    caution concrete.
    """
    monkeypatch.setattr("chains.registry.build_adapters", lambda rpc: {"GRC": CanSign()})
    monkeypatch.setenv("GRIDCOIN_WALLET_PASSPHRASE", "present-for-this-test-only")
    warning = supervisor.spawn_warning()

    assert "CAN broadcast on GRC" in warning
    assert "CANNOT" not in warning
    assert "mainnet wallet" in warning


def test_a_payable_chain_with_no_passphrase_still_says_which_will_refuse(monkeypatch):
    """The case this function originally fixed, unchanged by the new first branch."""
    monkeypatch.setattr("chains.registry.build_adapters", lambda rpc: {"GRC": CanSign()})
    monkeypatch.delenv("GRIDCOIN_WALLET_PASSPHRASE", raising=False)
    warning = supervisor.spawn_warning()

    assert "CANNOT BROADCAST ANYTHING" not in warning, (
        "GRC IS payable here -- the blocker is the unlock, which is a different sentence"
    )
    assert "GRIDCOIN_WALLET_PASSPHRASE is unset" in warning
    assert "nothing retries" in warning


def test_the_supervisor_banner_carries_the_census_not_just_the_path(tmp_path, monkeypatch):
    """THE SAME GAP, ONE FILE OVER, found 2026-10-02.

    workers/common.database_census() was added the day before, after three workers
    polled the wrong database for an hour while every cycle printed IDLE. The lesson
    was that the PATH ALONE does not catch it -- a path is only wrong relative to
    what you expected.

    This banner printed the path alone, two lines above spawning those same workers.
    So the one screen an operator reads BEFORE anything starts had exactly the
    weakness the fix was written for, and it went unnoticed because the census was
    landing in the worker LOGS, which nobody reads until after something has gone
    wrong.
    """
    path = tmp_path / "banner.db"
    connection = sqlite3.connect(path)
    connection.executescript(SCHEMA)
    connection.commit()
    connection.close()
    monkeypatch.setattr(supervisor.Config, "DB_PATH", str(path), raising=False)
    monkeypatch.delenv("SWAP_DB_PATH", raising=False)

    lines = supervisor.endpoint_summary()
    database = next(line for line in lines if line.strip().startswith("database"))
    holds = next(line for line in lines if "it holds" in line)

    assert str(path) in database
    assert "IS NOT SET in this shell" in database, "the path's provenance rides with the path"
    assert "NO SWAPS AT ALL" in holds


def test_the_banner_census_and_the_worker_census_are_the_same_function(tmp_path, monkeypatch):
    """Rule 8. Two spellings of "what is in this database" would drift, and the
    whole point is that an operator can compare the supervisor's line against the
    worker's own banner line and have them agree."""
    path = tmp_path / "shared.db"
    connection = sqlite3.connect(path)
    connection.executescript(SCHEMA)
    connection.commit()
    connection.close()
    monkeypatch.setattr(supervisor.Config, "DB_PATH", str(path), raising=False)

    holds = next(line for line in supervisor.endpoint_summary() if "it holds" in line)
    assert common.database_census(str(path)) in holds


# --- status names the log file (and the last line in it) ----------------------
#
# The supervisor computed `run_dir / f"{name}.log"` inside start_worker() and
# nowhere else, so `status` -- the command an operator runs an hour later when
# nothing seems to be happening -- printed three pid lines and no path. On
# 2026-10-02 the path was guessed instead, three times, and every guess was wrong
# the same way: run_dir is BASE_DIR / "runtime" with BASE_DIR being
# supervisor.py's own directory, so logs are under swap_terminal/runtime/ and not
# ./runtime/ at the repository root. The information was in the process printing
# the answer.


def test_the_log_path_has_ONE_definition_and_it_is_under_the_supervisors_own_directory(tmp_path):
    """The derivation, asserted directly, because the bug was its spelling.

    Every wrong guess assumed run_dir was relative to the repository root. It is
    not: DEFAULT_RUN_DIR is BASE_DIR / "runtime" and BASE_DIR is
    Path(supervisor.__file__).parent.
    """
    assert supervisor.worker_log_path(tmp_path, "deposit_watcher") == tmp_path / "deposit_watcher.log"
    # Asserted as PARTS rather than as one path equality. The parts are what the
    # guesses got wrong -- the parent directory, not the spelling of "runtime" --
    # and ruff's SIM300 reads a module constant as the literal side, so the whole
    # path comparison only passes written backwards. The parts say more anyway.
    assert supervisor.DEFAULT_RUN_DIR.name == "runtime"
    assert supervisor.DEFAULT_RUN_DIR.parent == Path(supervisor.__file__).resolve().parent
    assert supervisor.DEFAULT_RUN_DIR.parent.name == "swap_terminal", (
        "the logs are under swap_terminal/runtime/, which is the fact three guesses got wrong"
    )


def test_status_PRINTS_the_log_path_for_every_worker(tmp_path, capsys):
    """The wiring, driven through main() rather than the formatter.

    status_log_lines() being correct is not the fix -- command_status() calling it
    is. Three mutations in this session survived a right function whose call site
    discarded the result, so the call site is what is driven here.
    """
    table = {name: [sys.executable, "-c", SLEEPING_CHILD]
             for name in ("deposit_watcher", "payout_worker", "reconcile_worker")}
    assert supervisor.main(["status", "--run-dir", str(tmp_path)], commands=table) == 0
    out = capsys.readouterr().out
    for name in table:
        assert str(tmp_path / f"{name}.log") in out, f"{name}'s log path is not on the screen"


def test_status_shows_the_LAST_LOG_LINE_so_quiet_and_wedged_look_different(tmp_path, capsys):
    """Rule 14's actual subject: `running pid=1234` says nothing about which it is.

    A tail on a quiet log and a tail on a wedged process render identically, and
    that is what makes an operator Ctrl-C a healthy cycle. The last line answers it
    at a glance, which is why status prints it rather than only naming the file.
    """
    (tmp_path / "sleeper.log").write_text(
        "2026-10-02T12:00:00Z INFO cycle_start\n"
        "2026-10-02T12:00:01Z INFO did not re-read 1 transaction(s)\n"
        "\n"
    )
    assert supervisor.main(["status", "--run-dir", str(tmp_path)], commands=_sleeper_table()) == 0
    out = capsys.readouterr().out
    assert "did not re-read 1 transaction(s)" in out, "the last NON-EMPTY line, not the blank one"


def test_an_empty_log_says_so_rather_than_printing_a_blank(tmp_path, capsys):
    """`(none)`-shaped: an empty log and a log that could not be read are different
    facts, and a blank line beside `running` is ambiguous between both and a
    healthy quiet worker."""
    (tmp_path / "sleeper.log").write_text("")
    assert supervisor.main(["status", "--run-dir", str(tmp_path)], commands=_sleeper_table()) == 0
    out = capsys.readouterr().out
    assert "the worker wrote nothing at all" in out


def test_a_MISSING_log_reads_differently_for_stopped_and_for_running(tmp_path):
    """A running worker with no log file is a real anomaly: start_worker opens the
    handle BEFORE spawning, so the file exists by the time a pid does. A stopped
    worker with no log file is just a worker that has never run, which is not news.
    Collapsing the two would hide the first."""
    stopped = supervisor.status_log_lines(tmp_path, "deposit_watcher", "stopped")
    running = supervisor.status_log_lines(tmp_path, "deposit_watcher", "running")
    assert "no log file yet" in stopped[0]
    assert "NO LOG FILE" in running[0]
    assert stopped != running, "the two cases must not render identically"


def test_a_stopped_workers_log_is_STILL_printed(tmp_path):
    """The log of a worker that is not running is the only evidence of why it
    stopped, which makes it the thing most worth printing -- a status that goes
    quiet about it withholds exactly what is being asked for."""
    (tmp_path / "reconcile_worker.log").write_text("Traceback: boom\n")
    lines = supervisor.status_log_lines(tmp_path, "reconcile_worker", "stopped")
    assert any("Traceback: boom" in line for line in lines)


# --- the pid-reuse guard fails CLOSED when /proc gave an answer ----------------


#: This file's platform premise, stated once.
PROC_DIR_EXISTS = supervisor.PROC_DIR.exists()


def test_an_EMPTY_cmdline_for_a_live_pid_is_an_ANSWER_and_the_answer_is_NO():
    """The defect that read as a flake, and it was a SIGTERM to a stranger.

    pid_is_still_ours() returned True for EVERY empty cmdline read, with the reasoning
    in _proc_cmdline()'s docstring: the pid file is the best evidence available, and
    refusing would leave an orphan running. Sound for the case it was written for -- no
    /proc, where the question cannot be answered -- and wrong for the case it was also
    catching.

    MEASURED 2026-10-02 over 400 trials: _proc_cmdline() read EMPTY for a just-spawned
    LIVE process 152 times (38%), because between fork and exec there is no cmdline.
    So a stop racing a spawn asked "is this ours", could not tell, said yes, and
    signalled it. test_stop_refuses_to_signal_a_recycled_pid caught it as
    `assert 'stopped' == 'stale-pidfile'` -- the bystander it spawns to PROVE the guard
    works was the process the guard killed.

    On Linux an empty cmdline for a LIVE pid is never one of our workers: a zombie, a
    kernel thread, or a process mid-fork. None of those is the thing the pid file names.
    """
    assert PROC_DIR_EXISTS, "this test's premise is a platform that has /proc"
    bystander = subprocess.Popen([sys.executable, "-c", SLEEPING_CHILD])
    try:
        # The real window, not a stub: ask before the child has necessarily exec'd.
        verdicts = []
        for _ in range(60):
            child = subprocess.Popen([sys.executable, "-c", SLEEPING_CHILD])
            verdicts.append(
                supervisor.pid_is_still_ours(child.pid, "/usr/bin/python3 /x/workers/payout_worker.py")
            )
            child.kill()
            child.wait(timeout=10)
        assert not any(verdicts), (
            f"the guard claimed {sum(verdicts)} of {len(verdicts)} unrelated processes as ours; "
            f"each one is a SIGTERM to a process this repo never started"
        )
    finally:
        bystander.send_signal(signal.SIGKILL)
        bystander.wait(timeout=10)


def test_the_NO_PROC_case_still_fails_OPEN_because_the_question_is_unanswerable(monkeypatch):
    """The half of the original trade that was RIGHT and must not be lost.

    With no /proc, nothing distinguishes our worker from a stranger -- and an orphan
    holding a lock while every cycle prints exit_code=0 is the documented live failure
    (rule 13). So that case still answers yes. The change is narrower than "fail
    closed": it fails closed only where /proc gave an answer.
    """
    monkeypatch.setattr(supervisor, "PROC_DIR", Path("/nonexistent-proc-for-this-test"))
    monkeypatch.setattr(supervisor, "_proc_cmdline", lambda _pid: "")
    assert supervisor.pid_is_still_ours(4242, "/usr/bin/python3 /x/workers/payout_worker.py"), (
        "on a platform with no /proc the pid file is still the best evidence there is"
    )


def test_a_RECORDED_command_that_matches_is_still_ours():
    """The ordinary path, so a version that always refused would not pass the two above."""
    own = f"{sys.executable} -c {SLEEPING_CHILD}"
    child = subprocess.Popen([sys.executable, "-c", SLEEPING_CHILD])
    try:
        # WAIT FOR THE EXEC rather than assuming it. This is the same 38% window the
        # test above measures, and a positive test that raced it would be the flake
        # again with the sign flipped.
        for _ in range(500):
            if supervisor._proc_cmdline(child.pid):
                break
            time.sleep(0.002)
        assert supervisor._proc_cmdline(child.pid), "the child never exec'd; the test below is vacuous"
        assert supervisor.pid_is_still_ours(child.pid, own), (
            "a live process whose recorded command matches its cmdline IS ours"
        )
    finally:
        child.send_signal(signal.SIGKILL)
        child.wait(timeout=10)


def test_an_ALREADY_RUNNING_worker_is_NOT_described_by_the_capability_banner(tmp_path, capsys):
    """The banner said a payout worker CAN broadcast beside a worker it did not spawn.

    MEASURED ON THE OPERATOR'S HOST 2026-10-03, and it cost a credited swap:

        about to spawn    a payout worker CAN broadcast on GRC, SOL, XRP.
        ...
        ALREADY RUNNING   payout_worker pid=2065613  <- nothing was spawned for this one

    The running worker had been started BEFORE the SOL payout was armed. It refused
    the payout with "this adapter cannot sign or broadcast a Solana transfer, and
    holds no key that could" -- a string that no longer exists anywhere in the tree
    -- and the swap landed `failed` with 10 GRC already credited.

    Both lines were true. The banner describes the code ON DISK and is printed
    BEFORE the spawn, so it cannot know which workers are already up; the outcome
    line says nothing was spawned. A capability claim beside a did-nothing line is
    rule 13's exact failure ("treat 'skipped' plus 'success' in the same output as a
    defect in the output"), and the reader has no way to tell the two apart.

    THREE THINGS NOW SAY SO, and this asserts all three because each is read by a
    different reader: the banner scopes its own claim, the per-worker line corrects
    it where an operator looks for that worker, and the summary names the remedy.

    MUTATION: drop any one of them. Its assertion here fails, and the output goes
    back to claiming a capability for a process that may not have it.
    """
    table = _sleeper_table()
    first = supervisor.start_worker("sleeper", table["sleeper"], tmp_path)
    try:
        capsys.readouterr()
        supervisor.command_start(["sleeper"], tmp_path, table)
        body = capsys.readouterr().out

        assert "WORKERS SPAWNED BY THIS COMMAND, IF ANY" in body, (
            "the capability banner does not scope itself to what this command spawns, so it reads as a "
            "claim about every running worker"
        )
        assert "THE CODE IT LOADED WHEN IT STARTED" in body, (
            "the ALREADY RUNNING line does not say the process may be running older code, which is the "
            "fact that cost a swap"
        )
        assert "says nothing about this process" in body, body
        assert "NOT RELOADED" in body and "`stop` then `start`" in body, (
            "nothing tells the operator that start cannot reload code, which is the remedy they need"
        )
    finally:
        supervisor.stop_worker("sleeper", tmp_path, grace_seconds=5.0)
        _reap_zombie(first["pid"])


def test_the_NOT_RELOADED_line_is_ABSENT_when_everything_was_actually_spawned(tmp_path, capsys):
    """A line printed on every successful start is a line people stop reading (rule 14).

    So it must appear only when something was skipped -- otherwise the warning that
    matters is buried in the warning that never does.
    """
    table = _sleeper_table()
    capsys.readouterr()
    supervisor.command_start(["sleeper"], tmp_path, table)
    body = capsys.readouterr().out
    status = supervisor.worker_status("sleeper", tmp_path)
    try:
        assert "spawned=1" in body, body
        assert "NOT RELOADED" not in body, (
            "the reload warning printed on a start that spawned everything, which trains the reader to "
            "skip it on the run where it is true"
        )
    finally:
        supervisor.stop_worker("sleeper", tmp_path, grace_seconds=5.0)
        if status.get("pid"):
            _reap_zombie(status["pid"])


# --- both documented-ish invocations have to reach the CLI --------------------


@pytest.mark.parametrize(
    ("form", "argv"),
    [
        ("script", ["swap_terminal/supervisor.py", "--help"]),
        ("module", ["-m", "swap_terminal.supervisor", "--help"]),
    ],
)
def test_the_supervisor_cli_starts_under_both_invocations(form, argv):
    """`-m swap_terminal.supervisor` died on an import, naming a module nobody knows.

    MEASURED ON THE OPERATOR'S HOST 2026-10-03, while a BTC deposit sat
    uncredited and the question was whether the workers were running:

        ModuleNotFoundError: No module named 'config'

    `config` is not a name an operator has any reason to recognize, nothing in
    the message says the invocation is what is wrong, and all four traceback
    frames are runpy. It is indistinguishable from a broken install, so the
    reading it invites is "the supervisor is broken" -- which is the wrong thing
    to go and look at.

    supervisor.py was the ONE entry point in this tree without the
    `sys.path.insert` bootstrap, which is the opposite of what you would guess:
    all three workers carry it and so does migrate_swap_intents.py, while the
    file the README names three times relied on Python putting the script's own
    directory on sys.path, which only happens under the script form.

    --help IS THE ASSERTION ON PURPOSE. It exercises every import in the module
    and builds the parser, and it spawns nothing, signals nothing and writes no
    pid file -- so this test cannot leave a worker behind, which rule 13 would
    make this suite's problem rather than this test's.
    """
    # sys.executable and a literal argv, no shell and no user input -- which is why
    # this is safe. The sentence is kept as a plain comment and NOT as `noqa: S603`:
    # pyproject.toml already disables S603 for tests/, so the directive matched
    # nothing and RUF100 flagged it. A `noqa` that suppresses no finding is an unread
    # claim (rule 19), while the reasoning behind it is still worth a reader's time
    # (rule 1) -- so the claim goes and the reasoning stays.
    #
    # check=False IS EXPLICIT because this test asserts on returncode itself. The
    # default is already False; PLW1510 wants it said out loud, and here it is load
    # bearing: check=True would raise before the assertion below could report WHICH
    # form failed and print its stderr, which is the only useful output this test has.
    result = subprocess.run(
        [sys.executable, *argv],
        cwd=Path(__file__).resolve().parent.parent,
        capture_output=True, text=True, timeout=60, check=False,
    )

    assert result.returncode == 0, (
        f"the {form} form failed:\n{result.stderr}"
    )
    assert "No module named" not in result.stderr
    assert "--grace" in result.stdout or "usage:" in result.stdout, (
        "a zero exit with no usage text would mean something other than the parser answered"
    )


def test_a_FRESH_SPAWN_is_recognized_as_ours_without_racing_its_own_exec():
    """The other half of the 2026-10-02 fix, and the defect it introduced.

    That fix made an empty cmdline answer NO so a `stop` racing a `start` could not
    SIGTERM a bystander. pid_is_still_ours() has THREE callers and they fail in
    opposite directions: NO means "refuse to signal" at the stop sites, and
    "not ours, so spawn another" at start_worker. The same answer that closed a stray
    SIGTERM opened a double-spawn.

    MEASURED 2026-10-05: 5 of 40 immediate reads after Popen said NOT ours (12.5%),
    each one a start_worker() that would have spawned a second copy. After the fix,
    0 of 120, worst call 20.5ms against a 500ms budget.

    Mutation-checked by pointing pid_is_still_ours() back at _proc_cmdline().
    """
    assert PROC_DIR_EXISTS, "this test's premise is a platform that has /proc"
    own = f"{sys.executable} -c {SLEEPING_CHILD}"
    verdicts = []
    for _ in range(40):
        child = subprocess.Popen([sys.executable, "-c", SLEEPING_CHILD])
        try:
            # NO WAIT. Asking immediately is the whole point -- this is the window.
            verdicts.append(supervisor.pid_is_still_ours(child.pid, own))
        finally:
            child.kill()
            child.wait(timeout=10)
    assert all(verdicts), (
        f"the guard disowned {len(verdicts) - sum(verdicts)} of {len(verdicts)} of its "
        f"OWN just-spawned processes; each one is a start_worker() that spawns a second "
        f"copy and orphans the first (rule 13)"
    )


def test_start_worker_REFUSES_when_proc_is_still_mid_exec(tmp_path, monkeypatch):
    """start_worker() must not spawn a second copy because /proc has not caught up.

    DETERMINISTIC, AND THE FIRST VERSION OF THIS TEST WAS NOT. It called
    start_worker() twice back to back, twenty times, and asserted every second call
    said already-running. Mutation-checked by pointing pid_is_still_ours() back at
    _proc_cmdline(): IT STILL PASSED. By the time the second call reads /proc, the
    first child has had a Popen return, a pid-file write and a function return to
    exec in, so the window is almost always shut. A test that cannot fail pins
    nothing, and twenty real spawns is a slow way to pin nothing (rule 9).

    So the window is INJECTED instead of raced. _proc_cmdline() returns "" for the
    first read and the truth afterwards, which is exactly what the kernel does for a
    process between fork and exec -- and is why the original start-twice test failed
    once in a full-suite run and passed 5/5 alone.

    What this asserts is the DECISION: given an empty first read of a live process
    that the pid file names, start_worker answers already-running rather than
    spawning. With pid_is_still_ours() on the bare read, the empty answer means "not
    ours" and a second copy is started while the pid file keeps naming only one of
    them -- an orphan nothing can reap, through the guard meant to prevent it.
    """
    first = supervisor.start_worker("sleeper", _sleeper_table()["sleeper"], tmp_path)
    reads = {"n": 0}
    real = supervisor._proc_cmdline

    def one_empty_read(pid: int) -> str:
        reads["n"] += 1
        return "" if reads["n"] == 1 else real(pid)

    try:
        monkeypatch.setattr(supervisor, "_proc_cmdline", one_empty_read)
        second = supervisor.start_worker("sleeper", _sleeper_table()["sleeper"], tmp_path)
        if second["outcome"] == "started":
            stray = second.get("process")
            if stray is not None:
                stray.kill()
                stray.wait(timeout=10)
        # THE OUTCOME FIRST, THE SCAFFOLDING SECOND. Both assertions fail under the
        # mutation, and the order decides which message the reader gets: with the
        # retry-count check first it reported "the empty read was never retried",
        # which is true and tells you nothing about the bug. The defect is that a
        # second copy got spawned, so that is the sentence that has to come out.
        assert second["outcome"] == "already-running", (
            "an empty first read of a LIVE pid the pid file names was taken as "
            "'not ours' and a second copy was spawned"
        )
        assert second["pid"] == first["pid"]
        # And only then: did the injection actually fire? A green pass with reads==1
        # would mean this test proved nothing, which is worse than a red one.
        assert reads["n"] >= 2, (
            "the empty read was never retried, so this test did not exercise the "
            "settling path at all"
        )
    finally:
        monkeypatch.undo()
        supervisor.stop_worker("sleeper", tmp_path, grace_seconds=5.0)
        # EVERY PROCESS THIS TEST STARTED, BY ITS Popen HANDLE, and not through the
        # pid file. Mutation-checking this test against the bare read made the point:
        # the mutation spawns a second copy, the pid file then names only that one,
        # stop_worker() reaps it, and _reap_zombie(first["pid"]) BLOCKED for the
        # sleeper's full 120 seconds -- so the test failed by timing out instead of by
        # saying what was wrong. That hang IS the orphan the defect creates, which
        # makes it a fair demonstration and a terrible diagnostic. Cleanup that
        # depends on the thing under test cannot be trusted to clean up after it.
        handle = first.get("process")
        if handle is not None:
            handle.kill()
            handle.wait(timeout=10)
        _reap_zombie(first["pid"])


def test_a_DEAD_pid_resolves_immediately_and_does_not_pay_the_settle_wait():
    """A dead pid must cost nothing, because stop_everything() walks several in a row.

    Measured 2026-10-05: one poll, 0.000s.

    WHAT MAKES IT PROMPT IS THE process_alive() CHECK EXISTING, NOT ITS POSITION,
    and the first version of this docstring said the position -- "if the order were
    reversed every stop would stall for the full budget". THAT IS FALSE, and swapping
    the two lines to check it is what showed so: on the first pass the deadline has
    not arrived either way, so the loop reaches process_alive() and exits regardless
    of which is written first. The ordering only decides anything on the final pass,
    where both answers are "" anyway.

    Mutation-checked the way that does bite: removing the process_alive() branch makes
    a dead pid wait out the whole budget.
    """
    assert PROC_DIR_EXISTS, "this test's premise is a platform that has /proc"
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait(timeout=10)
    # Not reaped by os.waitpid, so the pid still exists as a zombie on some
    # platforms; either way the cmdline is empty and the answer must be prompt.
    started = time.monotonic()
    live = supervisor._settled_proc_cmdline(child.pid)
    elapsed = time.monotonic() - started
    assert live == "", "a dead or zombie process has no cmdline"
    assert elapsed < supervisor._CMDLINE_SETTLE_SECONDS / 2, (
        f"took {elapsed:.3f}s of a {supervisor._CMDLINE_SETTLE_SECONDS}s budget; "
        f"_settled_proc_cmdline() must exit on a dead pid instead of waiting one out"
    )


def test_the_reaper_reads_the_SAME_variable_the_container_spawner_does(monkeypatch, tmp_path):
    """Found 2026-10-06 on the operator's live host, by a `status` that contradicted itself.

    docker-compose.web.yml sets ST_WORKER_RUN_DIR=/runtime and
    docker/web_workers_entrypoint.py honors it, so the container's three workers wrote
    their pid files to /runtime. supervisor.DEFAULT_RUN_DIR read NO variable, so it
    looked in /app/swap_terminal/runtime. One variable, honored by the spawner and
    ignored by the reaper -- rule 13's "every spawn needs a reaper" failing not because
    the reaper was missing but because it was looking in the wrong place.

    Pasted from the live host, before the fix:

        stopped   payout_worker pid=None  no pid file
        *** ORPHAN *** payout_worker is ALIVE at pid 9 with no pid file naming it
        summary   running=0/3

    Three workers polling, reported as none running. A `stop` would have printed
    success having signaled nothing. The /proc walk is the only reason it was visible.

    This test pins the agreement rather than the path: it reads the value the
    ENTRYPOINT module computes and the value THIS module computes under the same
    environment, and asserts they are equal. A test that hardcoded "/runtime" would
    pass while the two drifted to different defaults.
    """
    spawner_source = (Path(__file__).resolve().parents[1] / "docker" / "web_workers_entrypoint.py").read_text()
    assert 'os.getenv("ST_WORKER_RUN_DIR"' in spawner_source, (
        "the spawner no longer reads this variable, so what this test pins has moved"
    )

    # The container's setting, and the one case that was broken.
    monkeypatch.setenv("ST_WORKER_RUN_DIR", str(tmp_path / "a-volume"))
    reloaded = importlib.reload(supervisor)
    assert (tmp_path / "a-volume") == reloaded.DEFAULT_RUN_DIR

    # And the host's, where the variable is unset and the default must NOT move --
    # the live host's three workers have pid files at the old path and a changed
    # default would orphan them, which is the defect this fixes, inverted.
    monkeypatch.delenv("ST_WORKER_RUN_DIR", raising=False)
    reloaded = importlib.reload(supervisor)
    assert reloaded.DEFAULT_RUN_DIR == reloaded.BASE_DIR / "runtime"


def test_status_with_no_host_worker_does_not_claim_deposits_are_uncredited(tmp_path, capsys):
    """The summary counts HOST workers. It must not assert what it cannot see.

    MEASURED ON THE OPERATOR'S HOST, 2026-10-08. This line read

        running=0/3  <- expected 3 while swaps are open; 0 means no worker is
                        polling and deposits will not be credited

    while the containerized deployment had been up fourteen hours and /admin --
    served by that container, reading the container's own /runtime -- showed
    deposit_watcher, payout_worker and reconcile_worker all RUNNING at pids 8, 9
    and 10. The count was right about the host and the sentence after it was
    false about the system.

    WHY THIS ONE IS WORTH A TEST RATHER THAN A CORRECTION. The remedy the false
    alarm invites is the double-pay condition: an operator with an open swap who
    believes nothing is polling starts the host workers, `web` already runs
    three, and six workers with TWO payout workers against one SQLite file is
    what gunicorn.conf.py records as 2 sends for 1 swap. A wrong status line
    whose fix costs money is not in the same category as a wrong status line.
    """
    table = {name: [sys.executable, "-c", SLEEPING_CHILD]
             for name in ("deposit_watcher", "payout_worker", "reconcile_worker")}
    assert supervisor.main(["status", "--run-dir", str(tmp_path)], commands=table) == 0
    out = capsys.readouterr().out

    assert "running=0/3" in out, "the count itself must still be printed"
    assert "HOST workers" in out, (
        "the count must say WHICH workers it counted -- it walks one run directory and a "
        "containerized deployment runs its own three where this cannot see them"
    )
    assert "deposits will not be credited" not in out, (
        "the summary must not assert that deposits are uncredited. It cannot see the "
        "containerized deployment's workers, so with `web` up the claim is simply false."
    )
    # The two readings the operator has to tell apart, and the warning that makes
    # the wrong one safe.
    assert "If NO deployment is running" in out, "the genuinely-broken case must still be named"
    assert "CONTAINERIZED" in out, "the other reading must be named, not left to be deduced"
    assert "swap_stack.py status" in out, "name the command that can see both deployments"
    assert "TWO payout workers" in out, (
        "the warning against resolving this line by starting the host workers must be on "
        "the screen, because that is the action the line invites"
    )


# =============================================================================
# endpoint_summary() ON A HOST THAT CANNOT IMPORT THE APPLICATION
# =============================================================================
#
# Measured on the operator's host 2026-10-10, with the containerized stack UP and
# serving and all three workers running under pid files:
#
#     workers           supervisor.py owns these; this is its own report
#   swap_terminal supervisor: STATUS
#     run directory     .../swap_terminal/runtime
#   Traceback (most recent call last):
#     [...] from services.payout_service import unlock_readiness_lines
#   ModuleNotFoundError: No module named 'bech32'
#
# `python3 swap_stack.py status` printed six sections and died. The worker table,
# the endpoint lines and the unlock readiness -- everything the operator ran the
# command FOR -- never appeared, on the one command they run when something is
# already wrong. The deployment is containerized, so requirements.txt is installed
# in the web image and the host has no reason to carry the application's wheels.

#: The block that fakes an absent dependency. A SUBPROCESS AND NOT monkeypatch,
#: because bech32 IS installed in this suite's environment: inserting a meta_path
#: blocker here and then importing would leave half-initialized modules in
#: sys.modules for every test that ran afterwards, and the failure would land
#: somewhere else entirely.
_BLOCKED_IMPORT_PROBE = '''
import os
import sys

sys.path.insert(0, "swap_terminal")


class Blocker:
    """Raise ModuleNotFoundError for one module, exactly as a missing wheel does."""

    def find_spec(self, name, path=None, target=None):
        if name == %(blocked)r:
            raise ModuleNotFoundError("No module named %(blocked)r", name=%(blocked)r)
        return None


sys.meta_path.insert(0, Blocker())
os.environ.setdefault("SWAP_DB_PATH", %(db)r)
import supervisor

for line in supervisor.endpoint_summary():
    print(line)
'''


def _summary_with_module_missing(blocked: str, db_path: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-c", _BLOCKED_IMPORT_PROBE % {"blocked": blocked, "db": db_path}],
        cwd=str(Path(__file__).resolve().parents[1]),
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )


def test_endpoint_summary_degrades_instead_of_killing_status(tmp_path):
    """It RETURNS LINES on a host that cannot import the application. It never raises.

    THE PROPERTY IS THE EXIT CODE AND THE ABSENCE OF A TRACEBACK, not the wording.
    endpoint_summary()'s contract is to hand the banner a list of lines; the caller
    prints the worker table after it, and that table -- pid files and /proc, nothing
    from the application -- is what `status` exists for. A raise here throws away the
    part that still worked.
    """
    run = _summary_with_module_missing("bech32", str(tmp_path / "x.db"))

    assert run.returncode == 0, f"endpoint_summary() raised instead of degrading:\n{run.stderr[-2000:]}"
    assert "Traceback" not in run.stderr, f"a traceback reached the operator:\n{run.stderr[-2000:]}"
    assert run.stdout.strip(), "rule 14: a blank result is ambiguous between 'nothing' and 'this died'"


def test_the_degraded_lines_name_the_module_the_remedy_and_what_is_unaffected(tmp_path):
    """Three facts, because without all three the line invites the wrong action.

      the module      the operator is holding a working stack and a broken command.
                      Without the name they cannot fix it, and `absent.name` is the
                      only place the name exists.
      what still      the worker table below comes from pid files and /proc. If the
      works           operator reads this as "status is broken" they will go looking
                      at the containers, which are fine.
      the remedy      one line they can run.

    AND IT MUST SAY THE STACK IS NOT IMPLICATED. This is a report that could not be
    produced, not a finding about the terminal -- and "unlock readiness could not be
    read" is one careless sentence away from reading as "the wallet cannot unlock",
    which is the opposite of the truth and would send them to re-enter a passphrase
    that is already correct.
    """
    run = _summary_with_module_missing("bech32", str(tmp_path / "x.db"))
    out = run.stdout

    assert "bech32" in out, "the missing module is not named, so the operator cannot act"
    assert "pip install" in out and "requirements.txt" in out, "no remedy on the screen"
    assert "WORKER TABLE BELOW IS UNAFFECTED" in out
    assert "says NOTHING about whether the stack works" in out, (
        "a report that could not be produced must not read as a finding about the terminal"
    )
    # The database path is still echoed, because it is the one thing this can know
    # without importing anything, and it is the question the census would have answered.
    assert "SWAP_DB_PATH" in out


def test_one_guard_covers_every_route_to_the_missing_module(tmp_path):
    """MUTATION: guard only the payout_service import and this test fails.

    THIS IS THE TEST FOR THE FIRST ATTEMPT BEING WRONG. Guarding
    `from services.payout_service import unlock_readiness_lines` alone moved the
    traceback rather than removing it, because workers.common reaches the same module
    by an entirely different route:

        workers.common -> services.deposit_service -> swap_service
                       -> modules.address_authority -> modules.address_network -> bech32

    So the assertion is made against a module that only the SECOND route reaches.
    Patching imports one at a time is rule 19's symptom-chasing; the fact being
    reported is "this host cannot import the application", and it is one fact.
    """
    # modules.address_network imports both; base58 is reached through swap_service's
    # chain only, so blocking it exercises the route the first attempt missed.
    for blocked in ("bech32", "base58"):
        run = _summary_with_module_missing(blocked, str(tmp_path / "x.db"))
        assert run.returncode == 0, (
            f"blocking `{blocked}` still crashed endpoint_summary(), so the guard does not cover "
            f"every route to it:\n{run.stderr[-1500:]}"
        )
        assert blocked in run.stdout, f"the degraded line does not name `{blocked}`"
