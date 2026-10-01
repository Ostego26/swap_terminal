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


def test_the_start_banner_never_prints_a_credential():
    """`user` and `password` are one key away from `host` and `port`.

    Asserted on the supervisor's own banner and not only on the workers',
    because this is the banner an operator pastes into a chat window when
    something is wrong -- which is exactly how a leaked RPC password would
    travel. endpoint_summary() formats no credential today; this is what keeps
    a future `**rpc` in an f-string from being a silent one.
    """
    original = supervisor.Config.RPC
    poisoned = {
        asset: {**values, "user": "canary-rpc-user", "password": "canary-rpc-password"}
        for asset, values in original.items()
    }
    supervisor.Config.RPC = poisoned
    try:
        text = "\n".join(supervisor.endpoint_summary())
    finally:
        supervisor.Config.RPC = original

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
    entry = supervisor.Config.RPC["GRC"]
    original = {key: entry[key] for key in ("port", "user", "password")}
    try:
        entry.update({"port": 25715, "user": "fixture-user", "password": "fixture-auth"})
        text = "\n".join(supervisor.endpoint_summary())
    finally:
        entry.update(original)

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
    entry = supervisor.Config.RPC["GRC"]
    original = {key: entry[key] for key in ("port", "user", "password")}
    try:
        entry.update({"port": 25715, "user": "fixture-user", "password": "fixture-auth"})
        text = "\n".join(supervisor.endpoint_summary())
    finally:
        entry.update(original)

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
    original = {key: entry[key] for key in ("port", "user", "password")}
    try:
        entry.update({"port": 0, "user": "", "password": ""})
        text = "\n".join(supervisor.endpoint_summary())
    finally:
        entry.update(original)

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
    original = {key: entry[key] for key in ("port", "user", "password")}
    try:
        entry.update({"port": 25715, "user": "fixture-user", "password": "fixture-auth"})
        # _dying_table()'s worker exits immediately, so this spawns and reaps a
        # harmless `python3 -c` and nothing is left behind -- the banner is what
        # is under test, not the spawn. main() returns 1 for the death, which is
        # confirm_spawned() doing its job and not a failure of this test.
        assert supervisor.main(["start", "--run-dir", str(tmp_path)], commands=_dying_table()) == 1
    finally:
        entry.update(original)
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
    original = {key: entry[key] for key in ("port", "user", "password")}
    try:
        entry.update({"port": 25715, "user": "fixture-user", "password": "fixture-auth"})
        warning = supervisor.spawn_warning()
    finally:
        entry.update(original)

    assert warning == ("a payout worker CAN broadcast. Stop now if this database is pointed at a funded "
                       "mainnet wallet.")
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
