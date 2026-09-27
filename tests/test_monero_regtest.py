"""The regtest driver's refusals and its reaper, without starting a daemon.

Role: test (read-only; starts no process, opens no socket)
Reads: nothing
Writes: only into pytest's tmp_path
Can move funds: no
Mainnet-safe: yes
Live-safe: yes

WHAT IS TESTED HERE AND WHAT IS NOT

monero_regtest.py's job -- mine a private chain and produce one transfer -- needs
monerod, which no test should require. What IS tested is everything that decides
whether it is SAFE to start, plus the reaper, and those are the parts a defect
would make expensive:

  the two data-dir refusals   ~/.bitmonero holds the real chains, and a blockchain
                              inside the checkout is committed or gitignored and
                              both are wrong
  the reaper                  rule 13: the assertion is the process's ABSENCE, not
                              the exit code of a kill, and close is idempotent
  the plan path               a bare invocation must start nothing

The nettype check is NOT tested here, because faking it would mean asserting that
a stub returns what the stub was told to return. The real guard is in monerod
anyway: generateblocks is refused unless the nettype is FAKECHAIN
(src/rpc/core_rpc_server.cpp:1956), which is belt this file cannot remove.
"""

from __future__ import annotations

import os
import pathlib
import subprocess
import sys
import time

import pytest

REPOSITORY_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "swap_terminal"))
sys.path.insert(0, str(REPOSITORY_ROOT))

from chains.monero_keys import decode_address  # noqa: E402  both path shims above come first
from step_console import Console  # noqa: E402  same

from monero_regtest import (  # noqa: E402  same
    COINBASE_UNLOCK_BLOCKS,
    DEFAULT_BLOCKS,
    DEFAULT_DAEMON_PORT,
    DEFAULT_WALLET_PORT,
    MONERO_ADDRESS_CHARS,
    PID_FILES,
    REGTEST_NETTYPE,
    RegtestError,
    build_parser,
    process_is_alive,
    refuse_dangerous_data_dir,
    stop_one,
    write_pid,
)


def test_the_block_default_clears_the_coinbase_lock_with_margin():
    """80 rather than 61. A run that mines exactly to the unlock boundary fails for a
    reason that reads like a wallet bug, so the default leaves twenty blocks of room
    -- and at difficulty 1 the extra blocks cost nothing."""
    assert COINBASE_UNLOCK_BLOCKS == 60
    assert DEFAULT_BLOCKS > COINBASE_UNLOCK_BLOCKS
    assert DEFAULT_BLOCKS - COINBASE_UNLOCK_BLOCKS >= 15


def test_the_default_ports_are_not_moneros_real_ones():
    """18081/38081 are mainnet and stagenet. A regtest daemon on either is one typo
    away from a wallet that meant a real network, and the point of this file is that
    such a typo cannot matter."""
    assert DEFAULT_DAEMON_PORT not in (18081, 38081, 18089, 38089)
    assert DEFAULT_WALLET_PORT not in (18083, 38083)


def test_the_nettype_checked_is_the_one_generateblocks_is_gated_on():
    """monerod spells regtest "fakechain" in get_info, and that is the same FAKECHAIN
    the generateblocks gate compares against. If this constant drifted, the pre-mine
    check would pass on a network the mine would then be refused on -- or worse."""
    assert REGTEST_NETTYPE == "fakechain"


def test_a_data_dir_inside_dot_bitmonero_is_refused():
    """The one mistake here that could cost something: ~/.bitmonero holds the real
    stagenet and mainnet chains."""
    with pytest.raises(RegtestError, match=r"inside ~/\.bitmonero"):
        refuse_dangerous_data_dir(pathlib.Path.home() / ".bitmonero" / "regtest")
    with pytest.raises(RegtestError, match=r"inside ~/\.bitmonero"):
        refuse_dangerous_data_dir(pathlib.Path.home() / ".bitmonero")


def test_a_data_dir_inside_the_repository_is_refused():
    with pytest.raises(RegtestError, match="inside the checkout"):
        refuse_dangerous_data_dir(REPOSITORY_ROOT / "chain")
    with pytest.raises(RegtestError, match="inside the checkout"):
        refuse_dangerous_data_dir(REPOSITORY_ROOT / "swap_terminal" / "deep" / "chain")


def test_a_directory_of_its_own_is_allowed(tmp_path):
    """The refusals must not be so broad that the documented default is refused too."""
    refuse_dangerous_data_dir(tmp_path / "xmr-regtest")
    refuse_dangerous_data_dir(pathlib.Path.home() / "xmr-regtest")
    assert build_parser().parse_args([]).data_dir.endswith("xmr-regtest")


def test_a_relative_data_dir_is_resolved_before_it_is_judged(tmp_path, monkeypatch):
    """`--data-dir chain` from inside the checkout must be refused, which only works
    if the path is resolved against the working directory first. A string comparison
    on the unresolved path would let it through."""
    monkeypatch.chdir(REPOSITORY_ROOT)
    with pytest.raises(RegtestError, match="inside the checkout"):
        refuse_dangerous_data_dir(pathlib.Path("chain"))


def test_no_run_flag_starts_nothing():
    """A bare invocation prints the plan and exits 0. Asserted by running the real
    script, because the thing being checked is that no process appears -- and a
    function call could not show that."""
    completed = subprocess.run(
        [sys.executable, str(REPOSITORY_ROOT / "monero_regtest.py")],
        capture_output=True, text=True, timeout=60, check=False,
    )
    assert completed.returncode == 0
    assert "PLAN ONLY -- nothing started" in completed.stdout
    assert "FAKECHAIN" in completed.stdout, "the plan must say why this is safe"
    assert "monerod" in completed.stdout


def test_stop_with_no_pid_files_reports_gone_rather_than_failing(tmp_path):
    """Stopping what was never started is a success, and it must not read as one that
    did work -- rule 14. The label says "gone or never started"."""
    console = Console(total_steps=1)
    for which in PID_FILES:
        assert stop_one(console, tmp_path, which) is True
    assert all(ok for _, ok in console.results)


def test_stop_asserts_the_absence_of_a_real_process(tmp_path):
    """Rule 13, and the assertion that matters: the process must be GONE afterwards,
    and that is checked by asking the operating system, not by trusting the kill.

    A real child is started -- `sleep 300`, which will not exit on its own -- its pid
    is recorded exactly as the driver records one, and the check is os.kill(pid, 0)
    raising ProcessLookupError after stop_one returns.
    """
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)"])
    try:
        write_pid(tmp_path, "monerod", child.pid)
        assert process_is_alive(child.pid), "premise: it is alive before the stop"

        console = Console(total_steps=1)
        assert stop_one(console, tmp_path, "monerod") is True

        # process_is_alive, NOT os.kill(pid, 0): this test's child is a child of this
        # process, so once killed it is a zombie until wait() below, and the naive
        # check would report it alive forever. That is the defect
        # test_process_is_alive_calls_a_zombie_dead pins, and using the naive check
        # here is how it was found.
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if not process_is_alive(child.pid):
                break
            time.sleep(0.1)
        else:
            pytest.fail(f"pid {child.pid} survived stop_one")

        assert not (tmp_path / PID_FILES["monerod"]).exists(), "the pid file is removed too"
        assert stop_one(console, tmp_path, "monerod") is True, "close must be idempotent"
    finally:
        child.poll()
        if child.returncode is None:
            child.kill()
        child.wait()


def test_an_unreadable_pid_file_is_a_failure_and_not_a_silent_pass(tmp_path):
    """Garbage in the pid file means the reaper does not know what to kill, and that
    must not report the same way as "already gone"."""
    (tmp_path / PID_FILES["monerod"]).write_text("not-a-pid\n")
    console = Console(total_steps=1)
    assert stop_one(console, tmp_path, "monerod") is False


def test_the_address_length_constant_matches_the_decoder(tmp_path):
    """MONERO_ADDRESS_CHARS must agree with chains/monero_keys, which owns the
    structure. Two spellings of one fact is rule 8's defect with a delay on it."""
    stagenet = (
        "537wxk1vzCDembafqWxfTgNcZGoK6rAsbP1JHKiQkjYLLzNDtgMTUKACBguFzx2XnFf1FQVqogcjd9LXTQ52jGiVBV52C1V"
    )
    assert len(stagenet) == MONERO_ADDRESS_CHARS
    assert decode_address(stagenet).network == "stagenet"


def test_process_is_alive_calls_a_zombie_dead():
    """THE DEFECT THIS FILE FOUND, pinned.

    `os.kill(pid, 0)` succeeds on a zombie -- a killed child whose parent has not
    called wait() is still in the process table -- so the obvious liveness check
    reports "STILL RUNNING" for a process that is dead and merely unreaped. That is
    the false alarm that teaches an operator to stop believing a stop, which is the
    exact failure CLAUDE.md rule 13 is about.

    It did not bite in normal use only by luck of topology: --run and --stop are
    separate invocations, so init has already reaped the daemons by the time --stop
    looks. This test creates the case that topology hides.
    """
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    try:
        # NEITHER child.wait() NOR child.poll() in this loop. poll() calls
        # waitpid(WNOHANG), which REAPS the child -- so the first draft of this test
        # destroyed the zombie it was trying to observe and then skipped itself for
        # not finding one. Only /proc is read here.
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if _proc_state(child.pid) in {"Z", "X"}:
                break
            time.sleep(0.05)
        state = _proc_state(child.pid)
        if state not in {"Z", "X"}:
            pytest.skip(f"could not observe a zombie (state {state!r}); /proc may be unavailable")

        os.kill(child.pid, 0)  # the premise: the naive check says it is alive
        assert process_is_alive(child.pid) is False, "a zombie must read as gone"
    finally:
        child.wait()

    assert process_is_alive(child.pid) is False, "and still gone once reaped"


def _proc_state(pid: int) -> str:
    """The state character from /proc/<pid>/stat, or '?'. Test-local on purpose: the
    driver reads it for a decision, and a test that reused that function to build its
    own premise would be checking the function against itself."""
    try:
        stat = pathlib.Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return "?"
    tail = stat.rpartition(")")[2].split()
    return tail[0] if tail else "?"


def test_process_is_alive_says_yes_for_something_actually_running():
    """The other direction, so the function cannot pass the zombie test by always
    answering False."""
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        assert process_is_alive(child.pid) is True
        assert process_is_alive(os.getpid()) is True, "and the test process itself"
    finally:
        child.kill()
        child.wait()
    assert process_is_alive(child.pid) is False
