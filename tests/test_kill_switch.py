"""The start and stop buttons, verified by killing and spawning real processes.

Role: test (spawns and reaps harmless local processes; binds a loopback socket)
Reads: swap_terminal/services/kill_switch.py, swap_terminal/loopback.py,
       swap_terminal/supervisor.py, swap_terminal/routes/kill_switch.py
Writes: pid files, logs and lock files under pytest's tmp_path
Can move funds: no
Mainnet-safe: yes -- the only thing these tests ever spawn is
       `python3 -c "time.sleep(...)"`. No worker, no RPC, no chain, and the one
       test that drives a START through HTTP drives the REFUSAL, never a spawn.

WHY THE SUPERVISED COMMAND IS INJECTED, and it matters more here than in
tests/test_supervisor.py. supervisor.worker_commands() returns the real argv for
the three workers, and one of them BROADCASTS PAYOUTS. Every test that starts
anything passes its own command table through ControlTarget(commands=...), so the
process under supervision is a real process with a real pid and a harmless argv.
That is not a paraphrase of the code under test: operate(), stop_everything(),
start_everything(), single_flight() and the route are the real ones.

THE ASSERTIONS ARE ABOUT THE OPERATING SYSTEM, NEVER ABOUT THE MESSAGE. Rule 13:
"a stop that cannot prove it worked is not a stop. Follow it with a check that
the process is gone, and make the absence the assertion -- not the exit code of
the kill." A test that asserted `"STOPPED" in result["headline"]` would pass
against a stop that reports success and signals nothing, which is exactly the
mutation this file is built to kill:

  MUTATION 1  stop_everything() believes stop_worker()'s outcome string instead
              of asking the OS again
              -> killed by test_a_stop_that_reports_stopped_over_a_live_process_is_not_proven,
                 which hands it a result dict saying "stopped" about a process
                 that is demonstrably alive and asserts the verdict is NOT PROVEN.
  MUTATION 2  single_flight() checks the pid file instead of holding the flock
              (check-then-act)
              -> killed by test_two_concurrent_starts_spawn_exactly_one_set,
                 which runs two real processes and counts processes in /proc.
  MUTATION 3  the route ignores operate()'s return value and re-renders the
              panel (the "correct function, caller throws the answer away" shape
              this repository keeps finding)
              -> killed by test_the_route_renders_the_verdict_of_the_action_it_ran.
  MUTATION 4  start_everything() ignores confirm_spawned()'s return
              -> killed by test_a_worker_that_dies_on_spawn_is_reported_as_died.
  MUTATION 5  the stale-pidfile row is counted as a failed kill again
              -> killed by test_a_stale_pid_file_is_not_reported_as_a_failed_kill.
  MUTATION 6  refuse_off_box() treats "could not look" as permission
              -> killed by test_controls_refuse_when_the_bind_cannot_be_established.
  MUTATION 7  the acknowledgment is compared against anything other than the
              warning that is live right now
              -> killed by test_a_start_is_refused_when_the_warning_has_changed.
"""

import contextlib
import ipaddress
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import loopback
import pytest
import supervisor
from services import kill_switch
from services.kill_switch import ControlTarget, RequestFacts

# app imports and calls create_app() at module scope; conftest.py has already
# pointed SWAP_DB_PATH at a temp file by the time this import runs.
import app as app_module  # isort: skip

#: "Bound to every interface", DERIVED RATHER THAN SPELLED. ruff's S104 flags the
#: literal wherever it appears, including inside a test fixture and inside an
#: assertion about a parser, and rule 19 forbids a `noqa` that only quiets a
#: finding. ip_address(0) IS the all-zeros address -- which is exactly what the
#: kernel writes as `00000000` in /proc/net/tcp -- so deriving it is both lint-clean
#: and a truer statement of where the value comes from.
ALL_INTERFACES = str(ipaddress.ip_address(0))
ALL_INTERFACES_V6 = str(ipaddress.IPv6Address(0))

#: A child that outlives the test unless something kills it. Long enough that a
#: leak is a failure rather than a timing coincidence.
SLEEP_SECONDS = 120

#: A child that exits immediately, for the "spawned and died" path. Exit code 3
#: rather than 1, so a hardcoded 1 cannot pass.
DYING_SOURCE = "import sys; sys.exit(3)"

#: Facts that ALLOW. Every field seeded, which is the point of RequestFacts: the
#: decision is callable without a socket and without a server (rule 10).
ALLOWING = RequestFacts(
    headers={"Host": "127.0.0.1:5000"},
    remote_addr="127.0.0.1",
    env={},
    listening={"127.0.0.1"},
)

WARNING = "a payout worker CAN broadcast on TESTCHAIN. This is a test fixture."


def _sleeper(token: str) -> dict[str, list[str]]:
    """A harmless worker table whose argv carries a token unique to one test.

    The token is what makes the process COUNTABLE in /proc without matching any
    other test's child, any editor holding this file open, or the pytest process
    itself -- the false-positive shape supervisor._proc_argv()'s docstring
    records being caught by a mutation that had survived.
    """
    return {"sleeper": [sys.executable, "-c", f"import time; time.sleep({SLEEP_SECONDS})  # {token}"]}


def _live_pids_carrying(token: str) -> list[int]:
    """Every live pid whose /proc cmdline carries this token. Asks the kernel."""
    found = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        argv = supervisor._proc_argv(int(entry.name))
        if any(token in part for part in argv):
            found.append(int(entry.name))
    return found


def _wait_until_scanned(pid: int, name: str, run_dir) -> list[int]:
    """Poll unaccounted_workers() until it sees `pid`, or give up after a bounded wait.

    WHY A WAIT AND NOT ONE SCAN. supervisor.unaccounted_workers() reads _proc_argv(),
    which reads /proc/<pid>/cmdline UNSETTLED -- deliberately, because that scan runs
    against long-lived workers where the fork/exec window has closed years ago in
    computer terms. supervisor._settled_proc_cmdline() exists for the other case and
    its own comment records the measurement: 400 trials, and _proc_cmdline() read
    EMPTY for a just-spawned process.

    A test that calls Popen and scans on the next line is in exactly that window. It
    passed every time it was run alone and FAILED ONCE in a full-suite run on
    2026-10-07, which is the shape of a race rather than of a defect: the child had
    not finished exec, /proc gave it an empty cmdline, and the scan correctly skipped
    something that did not yet look like a worker.

    So the production code is right and the test's setup was. "Flake" is not a root
    cause (CLAUDE.md) -- this is the cause, and waiting for the condition the test
    DEPENDS ON is the fix, not a retry around the assertion it is making.

    Returns the pids the scan attributes to `name`, so the caller still asserts on the
    scan's real answer rather than on this function's patience.
    """
    deadline = time.monotonic() + 5.0
    while True:
        seen = supervisor.unaccounted_workers([name], run_dir).get(name, [])
        if pid in seen or time.monotonic() >= deadline:
            return seen
        time.sleep(0.02)


def _kill_all(pids) -> None:
    for pid in pids:
        try:
            os.kill(pid, 9)
        except ProcessLookupError:
            continue
        with contextlib.suppress(ChildProcessError, OSError):
            os.waitpid(pid, 0)


def _ack(warning: str = WARNING) -> str:
    return kill_switch.acknowledgment_token(warning)


# --- loopback.py: what the bind is, and what may not be believed about it -----


@pytest.mark.parametrize(
    ("hex_address", "expected"),
    [
        # MEASURED on this machine 2026-10-02: a socket bound to 127.0.0.1
        # appears as 0100007F and one bound to 0.0.0.0 as 00000000. The byte
        # order is the whole function -- 0100007F is 127.0.0.1, NOT 1.0.0.127.
        ("0100007F:1F90", "127.0.0.1"),
        ("00000000:1F90", ALL_INTERFACES),
        ("0101007F:0050", "127.0.1.1"),
        # IPv6, four groups each byte-swapped individually. ::1 is 31 zeros and
        # a one in the last group. This half is NOT measured against a live
        # socket -- this sandbox has no AF_INET6 at all -- and the docstring
        # says so; it is a parse checked against the documented format.
        ("00000000000000000000000001000000:1F90", "::1"),
        ("00000000000000000000000000000000:1F90", ALL_INTERFACES_V6),
        ("notahexfield:1F90", None),
        ("0100:1F90", None),
        ("", None),
    ],
)
def test_decode_proc_address_reads_the_kernels_byte_order(hex_address, expected):
    """A parser that guessed 0.0.0.0 for input it did not recognize would refuse
    a loopback control surface forever and look like a policy decision doing it."""
    assert loopback.decode_proc_address(hex_address) == expected


def test_only_listen_rows_belonging_to_our_own_inodes_are_counted():
    """THE INODE FILTER IS LOAD-BEARING, not tidy.

    Measured 2026-10-02: /proc/net/tcp listed six LISTEN rows, two bound to
    0.0.0.0 and belonging to processes nothing to do with this application.
    Reading the file without matching inodes against /proc/self/fd reports this
    process as bound to all interfaces because something else on the host is --
    which on a control surface means refusing to render a private panel forever.

    MUTATION: drop the `fields[_INODE_COLUMN] not in inodes` guard. The
    0.0.0.0 row appears and this fails.
    """
    table = (
        "  sl  local_address rem_address   st tx_queue rx:when retrnsmt   uid  timeout inode\n"
        "   0: 0100007F:1F90 00000000:0000 0A 00000000:00000000 00:00000000 00000000  1000        0 111\n"
        "   1: 00000000:0016 00000000:0000 0A 00000000:00000000 00:00000000 00000000     0        0 222\n"
        "   2: 0100007F:1389 0100007F:C350 01 00000000:00000000 00:00000000  1000        0 333\n"
    )
    assert loopback.listening_hosts_in(table, {"111"}) == {"127.0.0.1"}
    # An ESTABLISHED row of ours is not a LISTEN row.
    assert loopback.listening_hosts_in(table, {"333"}) == set()
    # The stranger's 0.0.0.0 listener is visible in the file and is not ours.
    assert loopback.listening_hosts_in(table, {"222"}) == {ALL_INTERFACES}


def test_an_unreadable_proc_is_none_and_not_an_empty_answer(monkeypatch, tmp_path):
    """"Could not look" and "nothing found" are different, and the caller must
    fail closed on the first. None is how they are told apart."""
    monkeypatch.setattr(loopback, "PROC_SELF_FD", tmp_path / "does-not-exist")
    assert loopback.socket_inodes() is None
    assert loopback.listening_addresses() is None


def test_listening_addresses_sees_a_socket_this_process_really_bound():
    """The ALLOW direction, against a real socket rather than seeded text.

    This is the one test in the file that proves the /proc read works end to end
    on this platform: a real listener, bound in this process, found by matching
    this process's own socket inodes.
    """
    listener = socket.socket()
    try:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        hosts = loopback.listening_addresses()
        assert hosts is not None, "/proc could not be read, so this platform cannot run this test"
        assert "127.0.0.1" in hosts
    finally:
        listener.close()


@pytest.mark.parametrize(
    ("host", "expected"),
    [
        ("127.0.0.1", True),
        # The whole 127/8 block is genuinely loopback. A listener on 127.0.0.2 is
        # not reachable off-box and calling it exposed would refuse a private panel.
        ("127.0.0.2", True),
        ("::1", True),
        # A dual-stack socket reports v4 peers in the mapped form, and
        # `.is_loopback` is False for it -- which would refuse the operator's own
        # browser on a v6 host.
        ("::ffff:127.0.0.1", True),
        (ALL_INTERFACES, False),
        (ALL_INTERFACES_V6, False),
        ("10.0.0.5", False),
        # A NAME has no answer here without resolving it, and resolution is a
        # network question. Callers that must accept a name use LOOPBACK_HOSTS.
        ("localhost", False),
        ("", False),
    ],
)
def test_is_loopback_host_answers_about_addresses_only(host, expected):
    assert loopback.is_loopback_host(host) is expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("http://127.0.0.1:5000", "127.0.0.1"),
        ("127.0.0.1:5000", "127.0.0.1"),
        ("localhost", "localhost"),
        ("http://[::1]", "::1"),
        ("[::1]:8765", "::1"),
        # THE VULNERABILITY host_of() EXISTS FOR. A prefix match on
        # "http://127.0.0.1" passes this, and an attacker who controls any domain
        # can register that subdomain.
        ("http://127.0.0.1.attacker.com", "127.0.0.1.attacker.com"),
        ("http://evil-localhost", "evil-localhost"),
    ],
)
def test_host_of_parses_rather_than_matching_either_end(value, expected):
    assert loopback.host_of(value) == expected
    if expected != "localhost" and not expected.startswith("::"):
        assert expected not in loopback.LOOPBACK_HOSTS or expected == "127.0.0.1"


# --- the refusals: who may drive these controls at all ------------------------


def test_controls_refuse_when_the_bind_cannot_be_established():
    """MUTATION 6. None means "/proc could not be read", and a control surface
    must refuse on it: a platform where the bind cannot be established is a
    platform where this page cannot know whether it is private."""
    facts = RequestFacts(headers={"Host": "127.0.0.1"}, remote_addr="127.0.0.1", env={}, listening=None)
    refusals = kill_switch.refuse_off_box(facts)
    assert refusals, "an unknowable bind must refuse, not be assumed safe"
    assert "NOT" in refusals[0] and "KNOWN" in refusals[0]
    # And the panel refuses to render the buttons for the same reason.
    assert kill_switch.switch_panel(facts, run_dir=Path("/nonexistent"))["controls_enabled"] is False


def test_controls_refuse_a_server_listening_off_box():
    facts = RequestFacts(
        headers={"Host": "127.0.0.1"}, remote_addr="127.0.0.1", env={}, listening={ALL_INTERFACES, "127.0.0.1"}
    )
    refusals = kill_switch.refuse_off_box(facts)
    assert any(ALL_INTERFACES in line for line in refusals)
    assert any("SWAP_TERMINAL_HOST=127.0.0.1" in line for line in refusals)


def test_an_empty_swap_terminal_host_is_refused_rather_than_read_as_the_default():
    """os.getenv returns "" for a variable that is SET AND EMPTY, so the
    `'127.0.0.1'` default in gunicorn.conf.py's f-string does not apply and the
    bind becomes ":5000" -- an empty host on ALL INTERFACES. app.py's
    development server reads the same value as loopback (`host or DEFAULT_HOST`).
    The two servers disagree, so refusing is the only answer correct under both.
    """
    facts = RequestFacts(
        headers={"Host": "127.0.0.1"},
        remote_addr="127.0.0.1",
        env={"SWAP_TERMINAL_HOST": ""},
        listening={"127.0.0.1"},
    )
    refusals = kill_switch.refuse_off_box(facts)
    assert refusals, "SWAP_TERMINAL_HOST= binds all interfaces under gunicorn and must refuse"
    assert "ALL interfaces" in refusals[-1]


def test_an_unset_swap_terminal_host_with_loopback_sockets_is_allowed():
    """The other direction, so the refusal above is not simply "always refuse"."""
    assert kill_switch.refuse_off_box(ALLOWING) == []
    assert kill_switch.control_refusals(ALLOWING) == []


@pytest.mark.parametrize(
    ("headers", "remote_addr", "needle"),
    [
        ({"Origin": "https://evil.example", "Host": "127.0.0.1"}, "127.0.0.1", "evil.example"),
        # DNS rebinding: the attacker's domain resolves to 127.0.0.1, which makes
        # their page same-origin and silences the Origin check. The Host header
        # still carries their domain.
        ({"Host": "rebind.attacker.com"}, "127.0.0.1", "rebind.attacker.com"),
        ({"Host": "127.0.0.1", "X-Forwarded-For": "203.0.113.9"}, "127.0.0.1", "X-Forwarded-For"),
        ({"Host": "127.0.0.1", "Forwarded": "for=203.0.113.9"}, "127.0.0.1", "Forwarded"),
        ({"Host": "127.0.0.1"}, "203.0.113.9", "203.0.113.9"),
        ({"Host": "127.0.0.1"}, None, "no peer address"),
    ],
)
def test_a_request_that_did_not_come_from_this_machine_is_refused(headers, remote_addr, needle):
    """Binding loopback keeps the NETWORK out. It does not keep out a page the
    operator merely VISITS: a cross-origin form POST needs no CORS preflight, so
    the attacker cannot read the response but THE ACTION HAPPENS."""
    facts = RequestFacts(headers=headers, remote_addr=remote_addr, env={}, listening={"127.0.0.1"})
    refusals = kill_switch.refuse_cross_origin(facts)
    assert refusals, f"{headers} from {remote_addr} must be refused"
    assert any(needle in line for line in refusals)


def test_a_missing_origin_is_allowed_because_the_check_is_aimed_at_browsers():
    """curl and these tests send no Origin, and a page-driven request always
    carries one. Refusing a missing Origin would refuse every non-browser caller
    while stopping nothing."""
    assert kill_switch.refuse_cross_origin(ALLOWING) == []


def test_an_operate_call_refuses_before_taking_the_lock_or_touching_anything(tmp_path):
    """A refused request must not create the lock file: a refusal that writes is
    a refusal that can be used to block the operator's own stop."""
    facts = RequestFacts(headers={"Host": "127.0.0.1"}, remote_addr="127.0.0.1", env={}, listening=None)
    result = kill_switch.operate("stop", facts, target=ControlTarget(run_dir=tmp_path))
    assert result["verdict"] == kill_switch.VERDICT_REFUSED
    assert result["workers"] == []
    assert not kill_switch.lock_path(tmp_path).exists()


def test_an_unknown_action_is_refused_rather_than_defaulting_to_either(tmp_path):
    """There is no default. A typo, or a crafted form field, must not fall
    through to start OR to stop."""
    result = kill_switch.operate("sTaRt", ALLOWING, _ack(), target=ControlTarget(run_dir=tmp_path), warning=WARNING)
    assert result["verdict"] == kill_switch.VERDICT_REFUSED
    assert "is not an action" in result["refusals"][0]
    assert not list(tmp_path.glob("*.pid"))


# --- the stop, proven against the operating system ----------------------------


def test_a_real_stop_proves_absence_by_asking_the_os_again(tmp_path):
    """The central test. A real process, signaled, and the ABSENCE is the
    assertion -- os.kill(pid, 0) raising ProcessLookupError, not the verdict."""
    token = f"killsw-stop-{os.getpid()}"
    table = _sleeper(token)
    start = kill_switch.start_everything(run_dir=tmp_path, commands=table)
    pid = start["workers"][0]["pid"]
    try:
        assert supervisor.process_alive(pid), "setup: the child must be running before it is stopped"
        result = kill_switch.stop_everything(["sleeper"], tmp_path, grace_seconds=5.0)

        # THE OPERATING SYSTEM, FIRST. Everything after this is about reporting.
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)

        assert result["verdict"] == kill_switch.VERDICT_STOPPED
        row = result["workers"][0]
        assert row["gone"] is True
        assert "SIGTERM" in row["signals"]
        assert str(pid) in row["proof"] and "ABSENT" in row["proof"]
        assert result["counts"]["stopped"] == 1
        assert result["counts"]["failed"] == 0
        assert not supervisor.pid_file(tmp_path, "sleeper").exists()
    finally:
        _kill_all(_live_pids_carrying(token))


def test_a_stop_that_reports_stopped_over_a_live_process_is_not_proven(tmp_path, monkeypatch):
    """MUTATION 1, AND IT IS THE ONE THIS WHOLE FILE EXISTS FOR.

    If stop_everything() trusted stop_worker()'s outcome string, a stop that
    signaled nothing -- or signaled and failed -- would report STOPPED, and a
    test asserting on the headline would pass. So: a real live process, and a
    stop_worker() that lies about it exactly the way a broken reaper would.

    The verdict must be NOT PROVEN, because absence was asked of the OS and the
    answer was no. If this test ever passes with `verdict == stopped-and-proven`,
    the proof has been removed and the page is reporting a message instead of a
    fact.
    """
    token = f"killsw-liar-{os.getpid()}"
    child = subprocess.Popen(_sleeper(token)["sleeper"])  # sys.executable plus this file's own literal source
    try:
        assert supervisor.process_alive(child.pid), "setup: the bystander must be alive"

        monkeypatch.setattr(
            supervisor,
            "stop_worker",
            lambda name, run_dir, grace: {
                "worker": name,
                "outcome": "stopped",
                "pid": child.pid,
                "signals": ["SIGTERM"],
                "seconds": 0.01,
            },
        )
        result = kill_switch.stop_everything(["sleeper"], tmp_path, grace_seconds=1.0)

        assert supervisor.process_alive(child.pid), "setup: nothing should have actually killed it"
        assert result["verdict"] == kill_switch.VERDICT_NOT_PROVEN, (
            "a 'stopped' outcome over a process the OS still reports as alive was believed; "
            "the absence is not being asked for"
        )
        assert result["workers"][0]["gone"] is False
        assert "STILL ALIVE" in result["workers"][0]["proof"]
        assert result["counts"]["failed"] == 1
    finally:
        _kill_all([child.pid])


def test_a_stop_with_nothing_running_does_not_render_like_a_stop_that_stopped(tmp_path):
    """Rule 13's fourth bullet: twelve cycles printed `exit_code=0` beside
    "skipping this cycle", and a reader skimming for errors found none. These
    three outcomes must be three different words, three different headlines and
    three different glyphs."""
    nothing = kill_switch.stop_everything(["sleeper"], tmp_path, grace_seconds=1.0)
    assert nothing["verdict"] == kill_switch.VERDICT_NOTHING_RUNNING
    assert nothing["counts"]["signaled"] == 0

    token = f"killsw-distinct-{os.getpid()}"
    table = _sleeper(token)
    kill_switch.start_everything(run_dir=tmp_path, commands=table)
    try:
        stopped = kill_switch.stop_everything(["sleeper"], tmp_path, grace_seconds=5.0)
    finally:
        _kill_all(_live_pids_carrying(token))

    assert stopped["verdict"] != nothing["verdict"]
    assert stopped["headline"] != nothing["headline"]
    assert kill_switch.VERDICT_GLYPHS[stopped["verdict"]] != kill_switch.VERDICT_GLYPHS[nothing["verdict"]]
    # And neither of them may share anything with the NOT PROVEN reading.
    assert len({nothing["verdict"], stopped["verdict"], kill_switch.VERDICT_NOT_PROVEN}) == 3


def test_a_stale_pid_file_is_not_reported_as_a_failed_kill(tmp_path, monkeypatch):
    """MUTATION 5. A stale pid file means the number now belongs to something
    else and we deliberately left it alone. Counting it as a failed kill reports
    a kill that never happened as one that failed -- and f702d5e measured that
    outcome arriving 38% of the time when a stop races a start, so this is the
    common reading and not the exotic one."""
    token = f"killsw-stale-{os.getpid()}"
    child = subprocess.Popen(_sleeper(token)["sleeper"])  # sys.executable plus this file's own literal source
    try:
        monkeypatch.setattr(
            supervisor,
            "stop_worker",
            lambda name, run_dir, grace: {
                "worker": name,
                "outcome": "stale-pidfile",
                "pid": child.pid,
                "signals": [],
                "note": "pid belongs to another process now",
            },
        )
        result = kill_switch.stop_everything(["sleeper"], tmp_path, grace_seconds=1.0)

        assert result["counts"]["failed"] == 0, "nothing failed to die; nothing was signaled"
        assert result["counts"]["stale_pidfile"] == 1
        # IT STILL REFUSES THE VERDICT. Naming the right problem is not
        # downgrading it: the pid file stopped being evidence, so this stop
        # cannot say our worker is gone.
        assert result["verdict"] == kill_switch.VERDICT_NOT_PROVEN
        assert "NOT signaled" in result["workers"][0]["proof"]
        assert "STILL ALIVE" not in result["workers"][0]["proof"], (
            "a stale pid file read as 'the worker refused to die'; those are different problems"
        )
    finally:
        _kill_all([child.pid])


def test_stop_proof_says_a_different_thing_for_each_of_the_four_outcomes():
    """The decision, called with seeded values rather than by arranging four
    processes in four states (rule 10)."""
    sentences = {
        kill_switch.stop_proof("stopped", 4021, True, ["SIGTERM"]),
        kill_switch.stop_proof("failed", 4021, False, ["SIGTERM", "SIGKILL"]),
        kill_switch.stop_proof("stale-pidfile", 4021, False, []),
        kill_switch.stop_proof("not-running", None, True, []),
    }
    assert len(sentences) == 4, f"two outcomes render the same sentence: {sentences}"


def test_an_orphan_makes_a_clean_stop_not_proven(tmp_path, monkeypatch):
    """THE SECOND HALF OF RULE 13, and the one the Mammon incident turned on.

    stop_worker() makes absence the assertion for the pid IN THE FILE, which
    cannot see a worker alive under some other pid. On 2026-10-01 a stop printed
    `not running` three times and `untouched=3` -- every line true -- while a
    worker polled on. So: a real process matching a worker's argv, with no pid
    file naming it, and the verdict must refuse.

    worker_commands() is patched because unaccounted_workers() asks IT for the
    marker to scan for, not the injected table -- the scan, the /proc read and
    the process are all real.
    """
    token = f"killsw-orphan-{os.getpid()}"
    table = _sleeper(token)
    monkeypatch.setattr(supervisor, "worker_commands", lambda python_executable=sys.executable: table)
    child = subprocess.Popen(table["sleeper"])  # sys.executable plus this file's own literal source
    try:
        # No pid file was ever written for it, which is the whole point.
        assert not supervisor.pid_file(tmp_path, "sleeper").exists()
        assert child.pid in _wait_until_scanned(child.pid, "sleeper", tmp_path), (
            "setup: the scan must see the orphan, or this test proves nothing"
        )

        result = kill_switch.stop_everything(["sleeper"], tmp_path, grace_seconds=1.0)
        assert result["verdict"] == kill_switch.VERDICT_NOT_PROVEN
        assert result["counts"]["orphans"] == 1
        assert any(str(child.pid) in line for line in result["orphans"])
        # AND IT WAS NOT SIGNALLED. Killing a process this page cannot prove is
        # ours is how a recycled pid gets killed.
        assert supervisor.process_alive(child.pid), "an orphan must be reported and left alone"
    finally:
        _kill_all([child.pid])


# --- the start: the ceremony, the lock, and what it arms ----------------------


def test_a_start_is_refused_when_the_warning_has_changed(tmp_path):
    """MUTATION 7. supervisor.spawn_warning() is NOT a constant -- it says "CAN
    broadcast on GRC, LTC" or "CANNOT BROADCAST ANYTHING" or "will refuse because
    the passphrase is unset", derived from the live configuration. A form rendered
    an hour ago may carry an acknowledgment of a warning that is no longer true.
    """
    stale = kill_switch.acknowledgment_token("a payout worker CANNOT BROADCAST ANYTHING.")
    result = kill_switch.operate(
        "start", ALLOWING, stale, target=ControlTarget(run_dir=tmp_path, commands=_sleeper("never")), warning=WARNING
    )
    assert result["verdict"] == kill_switch.VERDICT_NOT_ACKNOWLEDGED
    assert not list(tmp_path.glob("*.pid")), "NOTHING may be spawned by an unacknowledged start"
    # The warning showing right now is re-rendered, so the operator reads the
    # sentence that actually applies rather than the one they agreed to.
    assert result["spawn_warning"] == WARNING


def test_an_empty_acknowledgment_is_refused(tmp_path):
    result = kill_switch.operate(
        "start", ALLOWING, "", target=ControlTarget(run_dir=tmp_path, commands=_sleeper("never")), warning=WARNING
    )
    assert result["verdict"] == kill_switch.VERDICT_NOT_ACKNOWLEDGED
    assert not list(tmp_path.glob("*.pid"))


def test_the_acknowledged_warning_is_the_one_shown_and_is_fingerprinted(tmp_path):
    """The token binds the acknowledgment to the TEXT that was read. Two
    different warnings must not fingerprint the same, or "it was shown before the
    spawn" is a hope rather than a fact."""
    panel_warning = "a payout worker CAN broadcast on GRC. Stop now."
    other = "a payout worker CANNOT BROADCAST ANYTHING."
    assert kill_switch.acknowledgment_token(panel_warning) != kill_switch.acknowledgment_token(other)

    token = f"killsw-ack-{os.getpid()}"
    result = kill_switch.operate(
        "start",
        ALLOWING,
        kill_switch.acknowledgment_token(panel_warning),
        target=ControlTarget(run_dir=tmp_path, commands=_sleeper(token)),
        warning=panel_warning,
    )
    try:
        assert result["verdict"] == kill_switch.VERDICT_STARTED
        assert result["spawn_warning"] == panel_warning
    finally:
        _kill_all(_live_pids_carrying(token))


def test_already_running_does_not_render_like_spawned(tmp_path):
    """Rule 13 again, pointed at start: "I have just started the desk" and "the
    desk was already running and I changed nothing" are two readings the operator
    is entitled to tell apart."""
    token = f"killsw-already-{os.getpid()}"
    table = _sleeper(token)
    try:
        first = kill_switch.start_everything(run_dir=tmp_path, commands=table)
        second = kill_switch.start_everything(run_dir=tmp_path, commands=table)

        assert first["verdict"] == kill_switch.VERDICT_STARTED
        assert second["verdict"] == kill_switch.VERDICT_ALREADY_RUNNING
        assert first["headline"] != second["headline"]
        assert second["counts"] == {"spawned": 0, "already_running": 1, "died": 0}
        assert len(_live_pids_carrying(token)) == 1, "the second start spawned a second copy"
    finally:
        _kill_all(_live_pids_carrying(token))


def test_a_worker_that_dies_on_spawn_is_reported_as_died(tmp_path):
    """MUTATION 4. Popen() returning is not evidence that the thing it spawned
    survived its own imports. The operator's 2026-10-01 run printed

        started    deposit_watcher pid=3998319 log=...

    three times, with nothing polling for deposits. The real exit code is
    asserted -- 3, not a hardcoded 1 -- and the pid file must be GONE, because a
    file claiming a running process that is not running is rule 13's stale
    record.
    """
    result = kill_switch.start_everything(run_dir=tmp_path, commands={"dier": [sys.executable, "-c", DYING_SOURCE]})
    assert result["verdict"] == kill_switch.VERDICT_DIED
    row = result["workers"][0]
    assert row["outcome"] == "DIED"
    assert row["exit_code"] == 3
    assert "NOT polling" in row["proof"]
    assert not supervisor.pid_file(tmp_path, "dier").exists(), (
        "a pid file naming a dead process would be read by a later stop"
    )


def test_a_start_refuses_when_an_orphan_of_that_worker_is_already_polling(tmp_path, monkeypatch):
    """start_worker()'s already-running check reads the PID FILE, and a worker
    started by hand has none. So the check says "not running", the spawn
    proceeds, and the host runs TWO payout workers against one database -- which
    this repository has already produced once: "2 sends, 1 swap_id, 2 broadcast
    rows, on-chain and final".

    The flock cannot close this one: the other copy was never under the lock. The
    /proc scan is the only thing that can see it, so the scan decides.
    """
    token = f"killsw-double-{os.getpid()}"
    table = _sleeper(token)
    monkeypatch.setattr(supervisor, "worker_commands", lambda python_executable=sys.executable: table)
    child = subprocess.Popen(table["sleeper"])  # sys.executable plus this file's own literal source
    try:
        # WAIT FOR THE PRECONDITION THIS TEST DEPENDS ON, which it did not until
        # 2026-10-10. It called Popen and then start_everything() on the next line,
        # and start_everything()'s scan reads /proc/<pid>/cmdline UNSETTLED -- so a
        # child that had not finished exec was correctly skipped, no orphan was
        # found, and the verdict came back as a normal start.
        #
        # MEASURED 2026-10-10, 400 trials on an IDLE machine: the child was not yet
        # visible in /proc 18 times, 4.5%. Under a full-suite run it is worse, which
        # is why this passed every time it was run alone and failed in the full suite.
        #
        # _wait_until_scanned() has existed for exactly this since 2026-10-07 and its
        # docstring records the same failure on the same shape -- it just had ONE
        # caller (line 557) and this site was never converted. Rule 8: the knowledge
        # existed and a second site did not use it. "Flake" is not a root cause; the
        # fork/exec window is, and waiting on the condition is the fix rather than a
        # retry around the assertion.
        assert child.pid in _wait_until_scanned(child.pid, "sleeper", tmp_path), (
            "the orphan never became visible to the /proc scan, so this test cannot say "
            "anything about what start_everything() does when one IS visible"
        )
        result = kill_switch.start_everything(names=["sleeper"], run_dir=tmp_path, commands=table)
        assert result["verdict"] == kill_switch.VERDICT_ORPHAN_BLOCKED
        assert result["orphan_count"] == 1
        assert len(_live_pids_carrying(token)) == 1, "a second copy was spawned beside the orphan"
        assert not list(tmp_path.glob("*.pid"))
        assert supervisor.process_alive(child.pid), "the orphan must be reported, never signaled"
    finally:
        _kill_all(_live_pids_carrying(token))


# --- single flight across two processes ---------------------------------------


_CONCURRENT_START = """
import json, sys, time
from pathlib import Path
from services.kill_switch import ControlTarget, RequestFacts, acknowledgment_token, operate

run_dir, token, deadline, out = sys.argv[1], sys.argv[2], float(sys.argv[3]), sys.argv[4]
warning = "concurrent-start-fixture"
table = {"sleeper": [sys.executable, "-c", "import time; time.sleep(120)  # " + token]}
facts = RequestFacts(headers={"Host": "127.0.0.1"}, remote_addr="127.0.0.1", env={}, listening={"127.0.0.1"})
# Both processes wake on the same wall clock, so they reach the flock together.
while time.time() < deadline:
    time.sleep(0.002)
result = operate(
    "start",
    facts,
    acknowledgment_token(warning),
    target=ControlTarget(run_dir=Path(run_dir), commands=table),
    warning=warning,
)
Path(out).write_text(json.dumps({"verdict": result["verdict"]}))
"""


def test_two_concurrent_starts_spawn_exactly_one_set(tmp_path):
    """MUTATION 2, AND IT IS WHY THE LOCK IS THE MECHANISM RATHER THAN A CHECK.

    gunicorn.conf.py runs TWO sync workers and either can serve this POST. With
    `if not already_running(): start()` -- two steps in two processes -- both can
    pass the first before either reaches the second, and what is left is the
    supervised set PLUS a full set of orphans with no pid file naming them: two
    payout workers polling one database. gunicorn's own startup banner is written
    for this class of failure ("anything else means N copies of a background
    loop").

    TWO REAL PROCESSES, not two threads: an flock is held per OPEN FILE
    DESCRIPTION, so two threads in one process can both "acquire" it and a
    threaded test would pass against no lock at all. They are synchronized on a
    wall-clock deadline rather than started and hoped about.

    THE ASSERTION IS A PROCESS COUNT FROM /proc, not the two verdicts. The
    verdicts are checked too, but a count of live processes carrying the token is
    the thing the operator actually cares about and the only thing a check-then-act
    implementation cannot fake.
    """
    token = f"killsw-flight-{os.getpid()}"
    env = {
        **os.environ,
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONPATH": str(Path(supervisor.__file__).resolve().parent),
    }
    deadline = time.time() + 2.0
    outs = [tmp_path / "a.json", tmp_path / "b.json"]
    children = [
        subprocess.Popen(  # sys.executable plus this file's own literal source and tmp paths
            [sys.executable, "-c", _CONCURRENT_START, str(tmp_path), token, str(deadline), str(out)],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        for out in outs
    ]
    try:
        for child in children:
            stdout, _ = child.communicate(timeout=60)
            assert child.returncode == 0, f"the concurrent starter failed:\n{stdout.decode('utf-8', 'replace')}"

        verdicts = sorted(json.loads(out.read_text())["verdict"] for out in outs)

        # THE COUNT FIRST. One set spawned, whatever the two said.
        live = _live_pids_carrying(token)
        assert len(live) == 1, (
            f"two concurrent starts spawned {len(live)} copies of the worker; the lock is not the mechanism"
        )
        # One won, one was told why it lost -- and being told is not the same as
        # being quietly skipped.
        assert verdicts == sorted([kill_switch.VERDICT_STARTED, kill_switch.VERDICT_IN_FLIGHT]), verdicts
    finally:
        _kill_all(_live_pids_carrying(token))


def test_the_refused_caller_is_told_who_holds_the_lock(tmp_path):
    """A refusal that cannot say who holds the lock sends the operator looking
    for a process with no name. And the holder's record must SURVIVE the refusal:
    `open(path, "w")` truncates BEFORE the flock fails, so the natural spelling
    has the refused caller destroy the record and then report a holder of ""."""
    path = kill_switch.lock_path(tmp_path)
    with kill_switch.single_flight(path) as holder:
        assert holder is None, "the first caller must get the lock"
        with kill_switch.single_flight(path) as second:
            assert second is not None, "the second caller must be refused, not blocked"
            assert f"pid={os.getpid()}" in second
            assert "held_since=" in second
        # The record is still there after the refused attempt.
        assert f"pid={os.getpid()}" in path.read_text(encoding="utf-8")


def test_a_stop_is_refused_while_another_action_holds_the_lock(tmp_path):
    """STOP TAKES THE SAME LOCK, and not for symmetry: f702d5e measured
    /proc/<pid>/cmdline reading EMPTY for a just-spawned live process 152 times
    in 400, so a stop racing a start cannot tell whether the pid it is about to
    signal is ours. Serializing removes the window instead of reporting it."""
    with kill_switch.single_flight(kill_switch.lock_path(tmp_path)) as holder:
        assert holder is None
        result = kill_switch.operate("stop", ALLOWING, target=ControlTarget(run_dir=tmp_path))
    assert result["verdict"] == kill_switch.VERDICT_IN_FLIGHT
    assert result["headline"] != kill_switch.VERDICT_HEADLINES[kill_switch.VERDICT_NOTHING_RUNNING], (
        "a stop that did nothing because it was refused must not read like a stop that found nothing"
    )
    assert any("held by another start or stop" in line for line in result["refusals"])


# --- the shapes the template depends on ---------------------------------------


def test_every_verdict_has_a_distinct_word_headline_and_glyph():
    """Rule 13's fourth bullet, mechanically: ten readings, and no two of them
    may be mistaken for each other."""
    assert len(set(kill_switch.ALL_VERDICTS)) == len(kill_switch.ALL_VERDICTS)
    assert set(kill_switch.VERDICT_HEADLINES) == set(kill_switch.ALL_VERDICTS)
    assert set(kill_switch.VERDICT_GLYPHS) == set(kill_switch.ALL_VERDICTS)
    headlines = [kill_switch.VERDICT_HEADLINES[v] for v in kill_switch.ALL_VERDICTS]
    assert len(set(headlines)) == len(headlines), "two verdicts share a headline"


def test_all_three_result_builders_return_the_same_core_keys(tmp_path):
    """A template that tolerates a missing key is the one that renders a blank
    when a key is misspelled, which is rule 14's blank gap with nothing to notice
    it by. Asserted rather than claimed in a docstring."""
    refusal = kill_switch.refusal_result("stop", ["because"])
    stop = kill_switch.stop_everything(["sleeper"], tmp_path, grace_seconds=1.0)
    start = kill_switch.start_everything(run_dir=tmp_path, commands={"dier": [sys.executable, "-c", DYING_SOURCE]})
    for name, result in (("refusal", refusal), ("stop", stop), ("start", start)):
        missing = kill_switch.RESULT_CORE_KEYS - set(result)
        assert not missing, f"{name} result is missing {sorted(missing)}"


def test_a_refused_render_withholds_the_inventory_and_still_says_why():
    """If this server is reachable off-box the reader may be a stranger, and the
    panel's operational detail -- every pid, the database path, the run directory
    -- is exactly what /admin's banner warns about disclosing. The refusal has to
    SAY WHY without handing over the inventory."""
    facts = RequestFacts(headers={"Host": "127.0.0.1"}, remote_addr="127.0.0.1", env={}, listening={ALL_INTERFACES})
    panel = kill_switch.switch_panel(facts, run_dir=Path("/nonexistent"))
    assert panel["controls_enabled"] is False
    assert panel["refusals"], "a refusal that says nothing is a control an operator debugs for an hour"
    assert panel["workers"] == []
    assert panel["spawn_warning"] == ""
    assert "withheld" in panel["database"]
    # The EVIDENCE is still printed, because an operator reading a refusal has to
    # see which signal produced it.
    assert any("listening sockets" in line for line in panel["bind_evidence"])


def test_the_panel_names_what_it_cannot_reach():
    """A button that claims to stop EVERYTHING and reaches three things is rule
    13's orphan with a green tick over it. The unreachable spawn sites are data,
    so the page cannot drift from the module's inventory."""
    named = " ".join(name + detail for name, detail in kill_switch.CANNOT_REACH).lower()
    for needle in ("gunicorn", "swap_terminal_desktop", "operator_panel", "by hand"):
        assert needle in named, f"{needle} is a spawn site this page cannot reach and must say so"
    assert kill_switch.REACHES, "the page must also say what it DOES reach"


def test_every_supervised_worker_says_what_starting_it_arms(tmp_path):
    """The inverse of admin_view.WORKER_STOPPED_CONSEQUENCES. "What stopping
    costs" is not the answer to "what does starting do", and the payout line is
    the whole reason start needs a ceremony stop does not."""
    for name in supervisor.worker_commands():
        assert name in kill_switch.WORKER_ARMS, f"{name} does not say what starting it arms"
    assert "BROADCASTS PAYOUTS" in kill_switch.WORKER_ARMS["payout_worker"]
    rows = kill_switch._worker_rows_now(list(supervisor.worker_commands()), tmp_path)
    assert len({row["arms"] for row in rows}) == len(rows), "two workers share one 'arms' sentence"
    assert len({row["stopped_consequence"] for row in rows}) == len(rows)


# --- through the real HTTP route ----------------------------------------------


@pytest.fixture
def client():
    """The real app, with its real url map. No database rows are needed: this
    surface reads pid files and /proc, not the swap tables."""
    app_module.app.config["TESTING"] = True
    with app_module.app.test_client() as test_client:
        yield test_client


@pytest.fixture
def loopback_listener():
    """A real listening socket on 127.0.0.1, so loopback.listening_addresses()
    has something true to find in THIS process.

    Flask's test client does not bind anything, so without this the panel
    correctly refuses with "this process holds no listening TCP socket" -- which
    is the right default and makes the ALLOW direction untestable through HTTP
    unless a real socket exists. Binding one is the honest way to test it: the
    /proc read is the real one.
    """
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    yield listener
    listener.close()


#: THE WRITE SURFACES REVIEWED AGAINST THIS FILE'S CLAIM, enumerated from the
#: app's real url map
#: on 2026-10-02 rather than from memory. The first draft of the test below
#: asserted that /admin/controls was the ONLY POST in the application and that was
#: simply false -- three others were already there:
#:
#:     /api/quotes                     prices a quote and writes a quotes row
#:     /api/swaps                      creates a swap and derives a deposit address
#:     /swap/<id>/address-proof        verifies a signature against a challenge
#:
#: None of them spawns a process and none of them broadcasts, which is the
#: distinction that actually matters and the one the test now makes. Writing the
#: broader claim and checking it afterwards is exactly the rule 17 failure of
#: saying a plausible thing in the register of a measurement, caught here only
#: because the assertion was run.
#:
#: RENAMED FROM PRE_EXISTING_POST_ROUTES ON 2026-10-07, when /atm arrived and this
#: gate caught it: "a write surface arrived that nobody has reviewed against this
#: file's claims: ['/atm']", which is the gate working exactly as intended.
#:
#: The rename is not cosmetic. Adding a route written today to a set called
#: PRE_EXISTING would be false on its face AND would be rule 19's forbidden move
#: -- "never add a baseline line for code you are writing now". What this set
#: actually holds is "POST routes somebody has checked cannot signal, spawn or
#: broadcast", which is a claim a new route can legitimately join by being
#: checked. So the name says that, and joining it requires the check below.
#:
#: `/`, REVIEWED 2026-10-07 AND HERE IS THE REVIEW. The ATM flow's single POST
#: writes a `quotes` row and a `swaps` row by calling services/quote_service.
#: create_quote() and services/swap_service.create_swap() -- the SAME two
#: functions /api/quotes and /api/swaps already call, so it adds no capability
#: either of those does not have. Grepped 2026-10-07 for subprocess, Popen,
#: os.kill, signal, nohup, execv, broadcast, sendtoaddress and send_to_address in
#: routes/atm.py: none present. Its imports are the two service functions above
#: plus read-only helpers (pair_view, payout_capacity, pricing,
#: address_authority, wizard). The payout -- the only broadcast in a swap's life
#: -- is services/payout_service.py's, reached by the payout worker, exactly as
#: for a swap created through the one-page form.
#: `/atm` BECAME `/` ON 2026-10-07 and this gate caught the move, which is the
#: second time tonight it has caught a route of mine arriving unreviewed. The
#: ATM flow replaced templates/index.html on the operator's instruction, so its
#: POST handler moved to the customer entry point -- SAME function, same imports,
#: same two service calls. The review below is unchanged because nothing about
#: the handler changed; only the URL it answers on.
REVIEWED_NON_SPAWNING_POST_ROUTES = frozenset(
    {"/api/quotes", "/api/swaps", "/swap/<swap_id>/address-proof", "/"}
)


def test_the_controls_route_is_the_only_post_that_can_signal_or_spawn_a_process():
    """Asserted over the app's REAL url map, so the claim survives the next edit
    rather than depending on somebody reading three files.

    The claim is not "the only POST" -- see REVIEWED_NON_SPAWNING_POST_ROUTES above, which
    is what measuring it produced. It is that exactly one route can send a signal
    or spawn a process, and this is it. A fourth POST appearing that is not in
    that set fails here, which is the point: a new write surface should have to be
    looked at deliberately.
    """
    # `@bp.get` and `@bp.post` on one path register TWO rules, so the set of
    # PATHS is what is asked about rather than a path-keyed mapping -- which
    # silently kept only one of the two and compared the wrong method list.
    #
    # A RULE THAT DECLARES NO METHODS IS REFUSED BY NAME, NOT SKIPPED. werkzeug
    # types Rule.methods as `set[str] | None` -- None is a rule that matches any
    # method -- and the obvious narrowing, `if rule.methods and "POST" in
    # rule.methods`, would drop such a rule out of `posts` without a word. That
    # is precisely this gate's failure mode: an unreviewed write surface that is
    # absent from the set it is compared against passes. So the Optional is
    # resolved by asserting it away first (pyright reportOperatorIssue x2,
    # 2026-10-09), and the `is not None` filter below only restates what that
    # assertion has already established.
    declared = [(rule.rule, rule.methods) for rule in app_module.app.url_map.iter_rules()]
    undeclared = sorted(path for path, methods in declared if methods is None)
    assert not undeclared, (
        f"these url rules declare no methods, so this gate cannot classify them and a POST "
        f"could hide among them: {undeclared}"
    )
    classified = [(path, methods) for path, methods in declared if methods is not None]
    posts = {path for path, methods in classified if "POST" in methods}
    gets = {path for path, methods in classified if "GET" in methods}
    assert "/admin/controls" in posts, "the controls endpoint does not accept a POST; the buttons do nothing"
    assert "/admin/controls" in gets, "the controls page cannot be opened, only submitted to"
    unexpected = posts - REVIEWED_NON_SPAWNING_POST_ROUTES - {"/admin/controls"}
    assert not unexpected, (
        f"a write surface arrived that nobody has reviewed against this file's claims: {sorted(unexpected)}"
    )


def test_the_page_refuses_and_renders_no_button_when_no_bind_can_be_established(client):
    """Under pytest this process holds no listening socket, so this is the
    genuine refusal path rather than a mocked one. The buttons must be ABSENT and
    the reason PRESENT: a control that silently vanishes is a control an operator
    debugs for an hour."""
    body = client.get("/admin/controls").get_data(as_text=True)
    assert 'data-controls-enabled="no"' in body
    assert "<form" not in body, "a refusing page must render no button"
    assert "THESE CONTROLS ARE REFUSING" in body
    assert "listening sockets" in body, "the refusal must show the evidence that produced it"


def test_the_page_renders_both_buttons_when_the_bind_is_provably_loopback(client, loopback_listener):
    """The ALLOW direction through the real route, against a real socket."""
    body = client.get("/admin/controls").get_data(as_text=True)
    assert 'data-controls-enabled="yes"' in body
    assert 'name="action" value="stop"' in body
    assert 'name="action" value="start"' in body
    # The start form carries the fingerprint of the warning it is showing, and the
    # box is required -- the ceremony is in the markup, not in a comment.
    assert 'name="acknowledgment"' in body
    assert "required" in body


def test_the_route_renders_the_verdict_of_the_action_it_ran(client, loopback_listener):
    """MUTATION 3: the handler ignores operate()'s return and re-renders the
    panel. That is the shape this repository keeps finding -- a correct function
    whose caller throws the answer away -- and it looks exactly like a working
    feature until somebody checks whether the page says what happened.

    A REAL STOP THROUGH THE REAL ROUTE. It targets the live run directory, where
    this sandbox has no pid files and no worker processes, so the honest verdict
    is NOTHING WAS RUNNING -- and that verdict appearing is the proof the result
    was carried through, because a freshly rendered panel carries no verdict at
    all.
    """
    response = client.post("/admin/controls", data={"action": "stop"})
    body = response.get_data(as_text=True)
    assert response.status_code == 200
    assert 'data-verdict="' in body, "the POST response carries no verdict; the result was discarded"
    assert kill_switch.VERDICT_NOTHING_RUNNING in body
    assert "NOTHING WAS RUNNING" in body
    # The scan is reported either way, and "could not look" is never printed as
    # "nothing found".
    assert "Anything alive that no pid file accounts for" in body


def test_a_start_through_the_route_without_the_acknowledgment_spawns_nothing(client, loopback_listener):
    """THE ONLY START THIS SUITE DRIVES THROUGH HTTP IS A REFUSED ONE, on purpose
    and stated so a reader does not think the arming path is covered here: the
    route uses the REAL worker table, and the real payout worker broadcasts. What
    is proven end to end is the gate -- the acknowledgment is checked by the
    endpoint and not only by the page that renders the checkbox."""
    before = {name: supervisor.worker_status(name, supervisor.DEFAULT_RUN_DIR) for name in supervisor.worker_commands()}
    body = client.post("/admin/controls", data={"action": "start", "acknowledgment": "stale0decafbad"}).get_data(
        as_text=True
    )
    assert kill_switch.VERDICT_NOT_ACKNOWLEDGED in body
    assert "NOTHING WAS SPAWNED" in body
    after = {name: supervisor.worker_status(name, supervisor.DEFAULT_RUN_DIR) for name in supervisor.worker_commands()}
    assert before == after, "a refused start changed a worker's state"


def test_an_unknown_action_through_the_route_is_refused_rather_than_defaulted(client, loopback_listener):
    body = client.post("/admin/controls", data={"action": "restart"}).get_data(as_text=True)
    assert kill_switch.VERDICT_REFUSED in body
    assert "is not an action" in body


def test_the_rendered_page_shows_microfortnights_with_seconds_in_parentheses(client, loopback_listener):
    """Rule 6, on the surface most likely to be pasted back. The unit is written
    with U+00B5 and never a lowercase u, and there is no space before it."""
    body = client.post("/admin/controls", data={"action": "stop"}).get_data(as_text=True)
    assert "µfn" in body, "a duration is reported without the microfortnight unit"
    assert "ufn" not in body.replace("µfn", ""), "an ASCII 'u' in a displayed unit is a defect"
    assert " µfn" not in body, "there is no space between the number and the unit"


# --- /admin links to the controls, and its own promise stays true -------------


def test_the_operator_page_still_promises_it_changes_nothing(client):
    """The sentence the controls were kept OFF that page to preserve.

    The operator asked for a start and stop button on 2026-10-02. The obvious home
    was /admin, and putting it there would have made that page's first sentence false
    -- "Nothing on this page changes anything ... those belong to you at a shell, not
    to a web page" -- about the one property that lets a reader trust everything below
    it. So the controls are their own page and their own blueprint.

    This asserts the promise is STILL TRUE of /admin rather than merely still printed:
    the page says it, and the page carries no form and no POST target.
    """
    body = client.get("/admin").get_data(as_text=True)
    assert "Nothing on this page changes anything" in body
    assert "<form" not in body.lower(), (
        "a form on /admin is a way to change something, which that sentence denies"
    )
    assert 'method="post"' not in body.lower()


def test_the_operator_page_LINKS_to_the_controls_and_says_what_they_do(client):
    """A guarantee kept by hiding the control would be a worse page, not a safer one.

    So /admin names the exception and points at it. The link text has to say what the
    page does -- an operator should not have to click an unlabeled link on a live-money
    system to find out whether it stops something.
    """
    body = client.get("/admin").get_data(as_text=True)
    assert "/admin/controls" in body, "the controls page is unreachable from the operator page"
    assert "start and stop the workers" in body
    assert "proves each is gone" in body, "and says the stop is proven, not merely reported"
    assert "acknowledged" in body, "and that starting arms a broadcaster"
    assert "loopback" in body, "and that it refuses off-box"


# =============================================================================
# THE REFUSAL'S REMEDY WAS WRONG FOR A CONTAINER, AND ACTIVELY HARMFUL.
#
# Measured on the operator's host 2026-10-10. The controls panel refused with
# "This server is listening on 0.0.0.0 ... Set SWAP_TERMINAL_HOST=127.0.0.1 and
# restart the server." That deployment is a container, where 0.0.0.0 is the ONLY
# bind that works -- and docker-compose.web.yml:102 carries the measurement in a
# comment one line below the port it publishes:
#
#   127.0.0.1 INSIDE A CONTAINER IS THE CONTAINER, AND THAT IS WHY THE UI CAME
#   UP EMPTY. Measured on the operator's host 2026-10-05
#
# So a security refusal printed, as its remedy, the exact change this repository
# had already recorded as breaking the page.
#
# THE VERDICT DOES NOT CHANGE AND MUST NOT. From inside a container the publish
# mapping is invisible: `ports: 127.0.0.1:5100:5000` restricts reachability at the
# docker proxy on the HOST, and nothing the container can read says so. So a
# loopback-published container and a LAN-exposed host are indistinguishable from
# in here, and a surface that arms a payout worker refuses both.
# =============================================================================


#: The non-loopback bind these tests seed. A NAMED CONSTANT WITH ONE `noqa` rather
#: than the literal in four places, and the reason is what rule 19 asks a suppression
#: to carry: S104 exists to catch code that BINDS all interfaces, and this is a fixture
#: STRING describing a bind that happened in another process entirely -- it is the
#: input to a refusal, which is the opposite of the hazard the rule is about. Four
#: literals would have meant four suppressions saying the same thing.
_ALL_INTERFACES = "0.0.0.0"  # noqa: S104 -- checked: a test fixture value, never a bind. See above.


def _exposed(*, in_container: bool, publish: str | None = None) -> kill_switch.RequestFacts:
    """A 0.0.0.0 bind, with the publish declaration as an argument rather than a second fixture.

    `publish=None` is "the variable is absent", which is what every caller written
    before 2026-10-10 means and is why the default is None rather than loopback: a
    fixture whose default RELAXED the guard would have quietly turned every existing
    test in this file into a test of the permitted path.
    """
    env = {"SWAP_TERMINAL_HOST": _ALL_INTERFACES}
    if publish is not None:
        env[kill_switch.PUBLISH_HOST_VARIABLE] = publish
    return kill_switch.RequestFacts(
        env=env,
        listening={_ALL_INTERFACES},
        in_container=in_container,
    )


def test_a_container_is_STILL_REFUSED_exactly_as_a_host_is():
    """An UNDECLARED container refuses exactly as a host does. Still the default.

    WHAT THIS TEST USED TO CLAIM, AND WHY THE CLAIM NARROWED. It said: "If this ever
    passes with zero refusals, a security control has been loosened by a commit whose
    stated purpose was to fix a sentence." On 2026-10-10 the operator decided the
    loosening -- see publish_verdict() -- so that sentence would now be read by a
    future reader as "nobody ever loosened this", which is the wrong-comment bug
    (rule 16) on the test that exists to notice a loosening.

    The claim that survives is narrower and is the one worth holding: a container that
    DECLARED NOTHING is indistinguishable from an exposed host, and refuses as one.
    The permitted case is its own test below and names the declaration it requires.
    """
    assert kill_switch.refuse_off_box(_exposed(in_container=True)), (
        "a container with a 0.0.0.0 bind must still refuse -- the publish mapping is "
        "not visible from inside it, so private and exposed are indistinguishable"
    )
    assert len(kill_switch.refuse_off_box(_exposed(in_container=True))) == len(
        kill_switch.refuse_off_box(_exposed(in_container=False))
    ), "the same two refusals fire in both deployments"


def test_a_container_is_NOT_told_to_set_the_loopback_bind():
    """The harmful half, pinned.

    MUTATION CHECKED: dropping the in_container branch restores the instruction
    that empties the UI.
    """
    blob = "\n".join(kill_switch.refuse_off_box(_exposed(in_container=True)))
    assert "do NOT set SWAP_TERMINAL_HOST=127.0.0.1" in blob
    assert "0.0.0.0 is the correct bind here" in blob
    assert "the UI came up empty" in blob, "and it cites the measurement, not an opinion"


def test_a_HOST_still_gets_the_original_remedy_which_is_correct_there():
    """gunicorn binds what SWAP_TERMINAL_HOST says, so on a host this IS the fix."""
    blob = "\n".join(kill_switch.refuse_off_box(_exposed(in_container=False)))
    assert "Set SWAP_TERMINAL_HOST=127.0.0.1 and restart the server." in blob
    assert "do NOT set" not in blob
    assert "container" not in blob.lower(), "no container hedging on a host deployment"


def test_the_container_remedy_names_the_check_IT_CANNOT_DO():
    """A remedy that cannot be followed from here has to say where it CAN be.

    The publish is the thing that decides, it lives on the host, and this process
    cannot read it -- so the sentence names the command rather than implying this
    page could have answered it.
    """
    blob = "\n".join(kill_switch.refuse_off_box(_exposed(in_container=True)))
    assert "CANNOT SEE" in blob
    assert "docker compose port web 5000" in blob
    assert "127.0.0.1:5100:5000" in blob and "5100:5000" in blob, (
        "both publish forms, because the difference between them IS the answer"
    )


def test_the_env_refusal_stops_claiming_a_container_was_configured_off_box():
    """"configured to be reachable off-box" is simply false of a container.

    Inside one, 0.0.0.0 is what reaches the docker proxy and says nothing about who
    can reach THAT.
    """
    container = "\n".join(kill_switch.refuse_off_box(_exposed(in_container=True)))
    host = "\n".join(kill_switch.refuse_off_box(_exposed(in_container=False)))
    assert "configured to be reachable off-box" in host
    assert "configured to be reachable off-box" not in container
    assert "says NOTHING about who can reach the published port" in container


def test_container_detection_is_presence_only_and_never_gates_the_buttons():
    """NOT A SECURITY CONTROL AND IT MUST NEVER BECOME ONE.

    A planted /.dockerenv buys an attacker a differently worded refusal and nothing
    else. This asserts the flag cannot flip a refusal into permission, which is the
    property that makes a presence check good enough.
    """
    loopback = kill_switch.RequestFacts(
        env={"SWAP_TERMINAL_HOST": "127.0.0.1"}, listening={"127.0.0.1"}, in_container=True
    )
    assert kill_switch.refuse_off_box(loopback) == [], (
        "a genuinely loopback-bound container is permitted, as before"
    )
    for flag in (True, False):
        assert kill_switch.refuse_off_box(_exposed(in_container=flag)), (
            "and the flag cannot turn an exposed bind into a permitted one"
        )


def test_in_a_container_reads_a_marker_FILE_and_not_a_cgroup_parse(tmp_path, monkeypatch):
    """/proc/1/cgroup has had three formats across cgroup v1, v2 and rootless podman.

    A regex over it is a thing that silently stops matching, which here would
    silently restore the wrong remedy. A missing marker degrades to the HOST
    wording, which is the safe direction.
    """
    monkeypatch.setattr(kill_switch, "_CONTAINER_MARKERS", (tmp_path / "nope",))
    assert kill_switch.in_a_container() is False
    present = tmp_path / "dockerenv"
    present.write_text("")
    monkeypatch.setattr(kill_switch, "_CONTAINER_MARKERS", (tmp_path / "nope", present))
    assert kill_switch.in_a_container() is True


# ---------------------------------------------------------------------------
# THE LOOSENING, 2026-10-10, AT THE OPERATOR'S INSTRUCTION.
#
# /admin's controls refused on the only deployment they have: a container, where
# SWAP_TERMINAL_HOST must be 0.0.0.0 because 127.0.0.1 binds the container's own
# loopback and the published port then reaches nothing (measured 2026-10-05). The
# publish mapping is what makes that private and it is not visible from inside.
#
# So this is a security control being relaxed on purpose, and these tests exist to
# pin the EXACT condition under which it relaxes and the several under which it
# does not. Rule 16 put the decision with the operator; it did not move the
# obligation to show which way each case goes.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("in_container", "publish", "expected"),
    [
        (False, None, kill_switch.PUBLISH_NO_MAPPING),
        (False, "127.0.0.1", kill_switch.PUBLISH_NO_MAPPING),
        (True, None, kill_switch.PUBLISH_NOT_DECLARED),
        (True, "", kill_switch.PUBLISH_NOT_DECLARED),
        (True, "   ", kill_switch.PUBLISH_NOT_DECLARED),
        (True, "127.0.0.1", kill_switch.PUBLISH_LOOPBACK),
        (True, "localhost", kill_switch.PUBLISH_LOOPBACK),
        (True, _ALL_INTERFACES, kill_switch.PUBLISH_EXPOSED),
        (True, "192.168.1.10", kill_switch.PUBLISH_EXPOSED),
    ],
)
def test_the_publish_verdict_on_every_shape_of_declaration(in_container, publish, expected):
    """Nine seeded cases, and the two rows that matter most are the ones that look dull.

    `(False, "127.0.0.1")` is a HOST that exported the variable. It must read
    NO_MAPPING, not LOOPBACK: on a host there is no publish mapping, the bind IS the
    reachability, and a process that could declare its own privacy would be handing
    itself the pass. That is the whole reason publish_verdict() checks the container
    marker FIRST rather than reading the variable and then qualifying it.

    `(True, "")` is the os.getenv hazard this file already carries a measurement for
    in the other direction: a variable that is set and empty returns "" rather than
    the default, and "" is not a host. Empty and absent both fail closed.
    """
    facts = _exposed(in_container=in_container, publish=publish)
    assert kill_switch.publish_verdict(facts) == expected


def test_a_container_with_a_DECLARED_LOOPBACK_publish_is_PERMITTED():
    """The loosening itself. One condition, and this is it.

    MUTATION CHECKED BOTH WAYS. Removing the `publish != PUBLISH_LOOPBACK` guard from
    either branch leaves refusals here (the change does nothing); removing the
    container check from publish_verdict() makes the host test above pass this too.
    """
    assert kill_switch.refuse_off_box(_exposed(in_container=True, publish="127.0.0.1")) == [], (
        "a container whose publish is declared loopback has nothing left to refuse on: the "
        "0.0.0.0 bind is the only one that works in there, and the publish is what restricts it"
    )


@pytest.mark.parametrize("publish", [None, "", _ALL_INTERFACES, "192.168.1.10"])
def test_every_OTHER_container_declaration_still_refuses(publish):
    """Four ways to be a container and not be permitted. All four still refuse.

    The permitted case is one value of one variable. Everything else -- absent, empty,
    and any non-loopback host -- lands where it did before this existed.
    """
    assert kill_switch.refuse_off_box(_exposed(in_container=True, publish=publish)), (
        f"a container declaring {publish!r} was permitted, which is not the condition the "
        f"operator authorized"
    )


def test_a_HOST_cannot_declare_its_own_privacy():
    """The variable relaxes nothing on a host, and a host is where the bind IS the answer.

    This is the hole an "operator asserts it is fine" flag would have had, and it is
    why the declaration is read only behind a container marker. A host process with a
    0.0.0.0 bind is reachable off-box; no environment variable changes that, and
    nothing here lets one pretend otherwise.
    """
    assert kill_switch.refuse_off_box(_exposed(in_container=False, publish="127.0.0.1")), (
        "a host exported the publish variable and was permitted -- on a host there is no "
        "publish mapping to declare, so this would be a process authorizing itself"
    )


@pytest.mark.parametrize(
    ("listening", "why"),
    [
        (None, "/proc could not be read, so the bind was never established"),
        (set(), "no listening socket, so the bind was never established"),
    ],
)
def test_a_declared_publish_does_NOT_relax_an_UNESTABLISHED_bind(listening, why):
    """The two NOT-ESTABLISHED branches are untouched, and the distinction is the point.

    A declared publish says WHO CAN REACH the port. It says nothing about what this
    process is bound to, and these two refusals are about not knowing that at all. A
    control surface that cannot establish its own bind must refuse whatever anyone
    declares about the host -- which is the same "None means could not look, and that
    is not the same as an empty set" that loopback.listening_addresses() returns None
    to express in the first place.
    """
    facts = kill_switch.RequestFacts(
        env={"SWAP_TERMINAL_HOST": "127.0.0.1", kill_switch.PUBLISH_HOST_VARIABLE: "127.0.0.1"},
        listening=listening,
        in_container=True,
    )
    assert kill_switch.refuse_off_box(facts), why


def test_a_declared_publish_does_not_make_the_controls_simply_ON():
    """Permitted by the bind check is not permitted, full stop. The browser hole is separate.

    refuse_cross_origin() exists because binding loopback keeps the network out and
    does not keep out a page the operator merely VISITS -- a cross-origin form POST
    needs no preflight and the ACTION HAPPENS. A publish declaration is about the
    network and must not reach that check, so this asserts the two are still
    independent on the one deployment where the bind check now passes.
    """
    facts = kill_switch.RequestFacts(
        headers={"Origin": "https://evil.example", "Host": "127.0.0.1:5000"},
        remote_addr="127.0.0.1",
        env={"SWAP_TERMINAL_HOST": _ALL_INTERFACES, kill_switch.PUBLISH_HOST_VARIABLE: "127.0.0.1"},
        listening={_ALL_INTERFACES},
        in_container=True,
    )
    assert kill_switch.refuse_off_box(facts) == [], "precondition: the bind check passes here"
    assert kill_switch.control_refusals(facts), "and the cross-origin POST is still refused"


def test_the_remedy_map_is_TOTAL_over_the_verdicts_that_reach_it():
    """Completeness asserted, not trusted -- and PUBLISH_LOOPBACK deliberately absent.

    THE SHAPE THIS COPIES IS daemon_network.bech32_prefix_status(), and the reason it
    is copied is that the alternative shipped a defect this session: a renderer that
    branched on a bare None and INVENTED a fact about XRP for a case nobody had
    enumerated. A map with a completeness test cannot do that; a map with a plausible
    default can.

    PUBLISH_LOOPBACK is absent because it never reaches _remedy_for() -- it is the
    permitted verdict, and a remedy for a refusal that did not happen is a sentence
    with no reader. Asserting its absence is what stops someone "completing" the map
    and leaving a dead sentence behind.
    """
    reaching = [v for v in kill_switch.PUBLISH_VERDICTS if v != kill_switch.PUBLISH_LOOPBACK]
    assert sorted(kill_switch._REMEDY_FOR) == sorted(reaching), (
        "every verdict that can reach _remedy_for() needs its own remedy, and no others"
    )
    assert kill_switch.PUBLISH_LOOPBACK not in kill_switch._REMEDY_FOR
    for verdict in reaching:
        assert kill_switch._remedy_for(verdict).strip(), f"{verdict} got an empty remedy"


def test_the_two_container_remedies_DIFFER_and_share_the_facts():
    """One preamble, two tails. A reader of either gets the measurement and the right action.

    They were one string until the verdict stopped being the same for every container.
    Two full copies would have agreed on the day they were written (rule 8); the shared
    preamble is what makes the drift impossible, and this asserts both halves of that.
    """
    undeclared = kill_switch._remedy_for(kill_switch.PUBLISH_NOT_DECLARED)
    exposed = kill_switch._remedy_for(kill_switch.PUBLISH_EXPOSED)
    for shared in ("do NOT set SWAP_TERMINAL_HOST=127.0.0.1", "the UI came up empty",
                   "docker compose port web 5000"):
        assert shared in undeclared and shared in exposed, f"{shared!r} is in one remedy only"
    assert "NOTHING HERE DECLARED IT" in undeclared
    assert "DECLARES A NON-LOOPBACK PUBLISH" in exposed
    assert undeclared != exposed, "two verdicts, two instructions"


def test_the_permitted_page_SAYS_what_it_accepted_and_what_it_did_not_prove():
    """Rule 14: a page that permits an action names what it accepted, not only what it checked.

    This is the one that keeps the loosening honest on screen. The operator reading an
    ENABLED panel has to be able to see that the buttons are on because of a
    declaration rather than a measurement, and has to see the thing the declaration
    does NOT cover -- every other container on the compose bridge reaches this port
    directly, without going through the publish at all. That is OPEN_FINDINGS finding
    4, it is unaffected by a loopback publish, and a docstring is not where an
    operator reads it.
    """
    evidence = "\n".join(kill_switch.bind_evidence(_exposed(in_container=True, publish="127.0.0.1")))
    assert kill_switch.PUBLISH_HOST_VARIABLE in evidence
    assert "DECLARATION and not a measurement" in evidence
    assert "reaches this port DIRECTLY" in evidence, "the residual exposure, on the page"
    assert "finding 4" in evidence, "and where the rest of it is written down"


def test_the_publish_line_is_ABSENT_on_a_plain_host_and_present_when_it_is_IGNORED():
    """Silence where it would be noise, a sentence where somebody has a wrong expectation.

    PUBLISH_NO_MAPPING is every non-compose deployment, and a line reading "not
    applicable" on every render is the noise rule 14 warns trains a reader to skip the
    block that matters -- the same argument trust_note() makes for not printing GNOME
    advice at an LXQt operator (C43).

    BUT A HOST THAT SET THE VARIABLE GETS TOLD IT IS IGNORED, because that operator
    believes they changed something and did not. A silently ignored setting is the
    quietest kind of wrong.
    """
    bare = "\n".join(kill_switch.bind_evidence(_exposed(in_container=False)))
    assert kill_switch.PUBLISH_HOST_VARIABLE not in bare, "no publish line on a plain host"

    confused = "\n".join(kill_switch.bind_evidence(_exposed(in_container=False, publish="127.0.0.1")))
    assert "SET BUT IGNORED" in confused
    assert "no publish to declare" in confused
