"""Does the stack's reaper know what it may stop, and prove what it stopped?

Role: test / measurement (seeds a fake /proc, runs the real decision functions)
Reads: swap_terminal/stack_authority.py
Writes: directories and symlinks under pytest's tmp_path
Can move funds: no. Nothing here signals a process, runs docker, or opens a socket
        to anything but a free local port it binds and closes.
Mainnet-safe: yes

WHAT THIS PINS, and all of it is a decision that would otherwise only be checkable
by running the real thing against the real host:

  - a chain daemon is NEVER stoppable, under any flag
  - a listener whose command line does not name this repository is FOREIGN and is
    left alone, which is the case that stops this project doing to a stranger what
    its own orphans did to it
  - the /proc/net/tcp parse filters on TCP_LISTEN, so the operator's open browser
    tab is not reported as a server holding the port
  - the absence proof is a BIND, not an exit code

The fake /proc is directories and symlinks because that is what the real one is:
pids_owning_inodes() takes its root as an argument for exactly this reason.
"""

from __future__ import annotations

import re
import socket
from pathlib import Path

import pytest

import swap_stack
from swap_terminal.stack_authority import (
    CANDID_UI_CANISTER_ID,
    CANISTER_SURFACES,
    DOWN_VERDICTS,
    LISTENER_VERDICTS,
    NEVER_STOPPED,
    REPLICA_STATUS_URL,
    WEB_PORT_CANDIDATES,
    WEB_SURFACES,
    canister_lookup_verdict,
    canister_surface_lines,
    container_id,
    container_label,
    container_verdict,
    down_verdict,
    hex_port,
    listening_inodes,
    pids_owning_inodes,
    port_is_free,
    proc_net_tcp_tables,
    process_name,
    readiness_verdict,
    stray_verdict,
    surface_map,
    web_surface_lines,
)

# A real /proc/net/tcp body. 0100007F is 127.0.0.1 little-endian; 13EE is 5102.
# Row 1 LISTENs on 5102. Row 2 is ESTABLISHED (st=01) on the same port -- a browser
# tab -- and must NOT be reported. Row 3 LISTENs on 9999, outside our set.
_PROC_NET_TCP = """\
  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode
   0: 0100007F:13EE 00000000:0000 0A 00000000:00000000 00:00000000 00000000  1000        0 884411
   1: 0100007F:13EE 0100007F:C350 01 00000000:00000000 00:00000000 00000000  1000        0 884412
   2: 00000000:270F 00000000:0000 0A 00000000:00000000 00:00000000 00000000  1000        0 884413
"""


def test_the_port_comes_out_of_the_kernels_hex_pair():
    assert hex_port("0100007F:13EE") == 5102
    assert hex_port("00000000:1388") == 5000
    assert hex_port("00000000:1337") == 4919


def test_an_ESTABLISHED_connection_to_our_port_is_not_a_listener():
    """The defect this avoids: counting an open browser tab as a server.

    Both rows carry local port 5102. Only the LISTEN one is a process holding the
    port; the other is the operator looking at the page. Reporting the second would
    make `down` claim a port is still bound after the server is gone.
    """
    found = listening_inodes(_PROC_NET_TCP, {5102})
    assert found == {5102: 884411}, "the ESTABLISHED row's inode 884412 must not appear"


def test_a_port_outside_the_set_is_ignored():
    assert listening_inodes(_PROC_NET_TCP, {5102}) == {5102: 884411}
    assert listening_inodes(_PROC_NET_TCP, {9999}) == {9999: 884413}
    assert listening_inodes(_PROC_NET_TCP, {5000}) == {}


def _fake_proc(root: Path, pid: int, inode: int, cmdline: str) -> None:
    """One pid under a fake /proc, holding one socket inode, with a cmdline."""
    fd_dir = root / str(pid) / "fd"
    fd_dir.mkdir(parents=True)
    (fd_dir / "5").symlink_to(f"socket:[{inode}]")
    (root / str(pid) / "cmdline").write_text(cmdline)


def test_the_pid_holding_a_socket_inode_is_found_by_scanning(tmp_path):
    _fake_proc(tmp_path, 1403174, 884411, "gunicorn\0wsgi:app\0")
    (tmp_path / "self").mkdir()          # a non-numeric entry, which must be skipped
    (tmp_path / "uptime").write_text("x")

    assert pids_owning_inodes({884411}, tmp_path) == {884411: 1403174}
    assert pids_owning_inodes({999999}, tmp_path) == {}, "an inode nobody holds is absent, not an error"


def test_a_pid_whose_fds_cannot_be_read_does_not_break_the_scan(tmp_path):
    """A process exiting between the listdir and the readlink is ordinary, not an error."""
    _fake_proc(tmp_path, 100, 884411, "python\0worker.py\0")
    (tmp_path / "200").mkdir()           # no fd/ directory at all
    assert pids_owning_inodes({884411}, tmp_path) == {884411: 100}


def test_the_process_name_is_argv0s_basename_not_the_whole_line():
    assert process_name("/usr/bin/gridcoinresearchd\0-testnet\0") == "gridcoinresearchd"
    assert process_name("tail\0-f\0bitcoind.log\0") == "tail"
    assert process_name("") == ""


def test_a_chain_daemon_is_REFUSED_and_a_log_tail_naming_one_is_not():
    """NEVER_STOPPED matches the NAME. `tail -f bitcoind.log` is not bitcoind.

    If it matched the whole command line, the tail would be refused -- harmless --
    and, far worse, the reverse mistake becomes easy to write: a pattern loose
    enough to catch a daemon by its log file is loose enough to catch a daemon this
    file has no business classifying at all.
    """
    repo = Path("/home/someone/swap_terminal")
    for daemon in NEVER_STOPPED:
        verdict, reason = stray_verdict(f"/usr/bin/{daemon}\0-testnet\0", repo)
        assert verdict == "refused", daemon
        assert reason, "a refusal with no reason is a refusal an operator cannot act on"

    verdict, _ = stray_verdict("tail\0-f\0/var/log/bitcoind.log\0", repo)
    assert verdict != "refused"


def test_a_listener_naming_this_repository_is_OURS_and_anything_else_is_FOREIGN():
    repo = Path("/home/someone/swap_terminal")

    verdict, reason = stray_verdict(f"python\0{repo}/swap_terminal/workers/payout_worker.py\0", repo)
    assert verdict == "ours"
    assert str(repo) in reason

    # The ambiguous case, and the answer is deliberately the cautious one: a
    # gunicorn whose argv does not carry the path reads FOREIGN and is left alone.
    verdict, reason = stray_verdict("gunicorn\0wsgi:app\0", repo)
    assert verdict == "foreign"
    assert "will not stop it" in reason

    verdict, _ = stray_verdict("/usr/bin/postgres\0-D\0/var/lib/postgresql\0", repo)
    assert verdict == "foreign"


def test_the_absence_proof_is_a_BIND(tmp_path):
    """port_is_free() answers by binding, which is what makes `down` provable.

    Bound to 0 first to get a port the OS says is free, then asserted free; then
    held open and asserted NOT free. Both directions, because a function that
    always returned True would pass a one-sided test and turn rule 13's proof into
    the false reassurance it replaces.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as holder:
        holder.bind(("127.0.0.1", 0))
        port = holder.getsockname()[1]
        holder.listen(1)
        assert port_is_free(port) is False, "a port with a listener on it is not free"

    assert port_is_free(port) is True, "and it is free once the holder is closed"


# --- a listener inside a container, which the cmdline test cannot classify


def test_a_containerized_process_is_recognized_by_its_cgroup_not_its_path():
    """The defect this fixes, measured on the live host within an hour of shipping it.

    stray_verdict() called the listener on 127.0.0.1:5101 FOREIGN -- "may be an
    unrelated program that legitimately holds this port". Its command line was

        /usr/local/bin/python3 /usr/local/bin/gunicorn -c /app/gunicorn.conf.py wsgi:app

    which is THIS project's gunicorn inside a container: docker/web.Dockerfile puts
    the repository at /app. The action (do not kill) was right and the reason was
    wrong, which rule 16 treats as seriously as wrong code -- an operator reading
    "unrelated program" would leave a copy of their own stack running on the belief
    that it was somebody else's.

    A host path can NEVER appear in that argv, so no amount of tuning the repo_root
    substring test reaches it. The kernel's cgroup line is the answer, and it also
    gives the operator the right lever: `docker stop <id>`, not a pid.
    """
    v2 = "0::/system.slice/docker-3f8a1b2c4d5e6f70819a2b3c4d5e6f708192a3b4c5d6e7f8091a2b3c4d5e6f70.scope\n"
    assert container_id(v2) == "3f8a1b2c4d5e"

    v1 = (
        "12:pids:/docker/3f8a1b2c4d5e6f70819a2b3c4d5e6f708192a3b4c5d6e7f8091a2b3c4d5e6f70\n"
        "11:memory:/docker/3f8a1b2c4d5e6f70819a2b3c4d5e6f708192a3b4c5d6e7f8091a2b3c4d5e6f70\n"
    )
    assert container_id(v1) == "3f8a1b2c4d5e"


def test_a_host_process_has_no_container_id():
    """The negative case, which is what makes the positive one mean anything.

    A plain systemd cgroup and a bare `0::/` must both read None -- if this returned
    an id for a host process, every worker would be reported as containerized and
    the operator would be told to `docker stop` a pid on their own machine.
    """
    assert container_id("0::/user.slice/user-1000.slice/session-3.scope\n") is None
    assert container_id("0::/\n") is None
    assert container_id("") is None


def test_something_that_merely_mentions_docker_is_not_a_container_id():
    """The id has to look like one. A cgroup path naming a docker.service is not a container.

    Without the hex-and-length check this would return "service" or similar and the
    report would print `docker stop service`, which is worse than saying nothing:
    it is an instruction that fails in a way nobody can interpret.
    """
    assert container_id("0::/system.slice/docker.service\n") is None
    assert container_id("0::/system.slice/docker-short.scope\n") is None


def test_a_container_label_strips_dockers_leading_slash():
    """`docker inspect --format '{{.Name}} {{.Config.Image}}'` writes /st-ui, docker ps writes st-ui.

    Stripped so a reader comparing this report against `docker ps` does not have to
    notice the difference -- which is the kind of one-character discrepancy that
    makes an operator wonder whether they are looking at two things.

    The real values from the live host 2026-10-06 are used: `st-ui` on image
    93c22bb2a25a, a bare SHA with no tag, which is what says the image has since
    been rebuilt and nothing names that build any more.
    """
    assert container_label("/st-ui 93c22bb2a25a") == "st-ui (image 93c22bb2a25a)"
    assert container_label("/swap-icp-replica swap-terminal/icp-replica:local") == (
        "swap-icp-replica (image swap-terminal/icp-replica:local)"
    )


def test_a_label_with_no_image_or_no_output_degrades_rather_than_raising():
    """The report must survive docker answering oddly; the verdict does not depend on it.

    An empty answer gives an empty label and swap_stack.py prints "(docker could not
    name it)" -- the FINDING is the containerized listener, and losing the pretty name
    must never lose the finding.
    """
    assert container_label("/just-a-name") == "just-a-name"
    assert container_label("") == ""
    assert container_label("   \n") == ""


def test_UP_starts_the_CONTAINERIZED_deployment_and_never_both():
    """Six workers on one database, two of them payout workers. Measured 2026-10-07.

    f756b1b's `up` passed the compose files and let compose start everything. The
    operator's own run showed what that did:

        Container swap-web Started
        ALREADY RUNNING deposit_watcher pid=3285268

    docker/web.Dockerfile's CMD is docker/web_workers_entrypoint.py, which starts
    gunicorn AND the three workers. So `up` started a container running three workers
    beside the three the host supervisor already had: SIX on one database, TWO payout
    workers, which tests/test_payout_concurrency.py measures as 2 sends for 1 deposit.

    5ecf442 fixed it by dropping `web` from UP_SERVICES and keeping the host
    deployment. THAT WAS THE WRONG HALF of a correct observation, and this test was
    written to pin it. The observation -- the deployments are mutually exclusive -- is
    right. Keeping the host one leaves the operator running `python app.py`, the Flask
    DEVELOPMENT server, in a terminal.

    So the property is not "web is excluded". It is: `up` performs EXACTLY ONE
    deployment, and the one it performs is the containerized one, because that is the
    one that serves a page under gunicorn after `docker up` with nothing in a
    terminal.

    cmd_up()'s refusal is what enforces the "exactly one" half at runtime -- it reads
    the supervisor's own worker table and declines before starting any container if a
    host worker is alive. That is checked rather than assumed precisely because "the
    operator probably stopped them" is the reasoning that produced the six-worker
    state in the first place.
    """
    assert "web" in swap_stack.UP_SERVICES, (
        "`web` is what serves the UI under gunicorn; without it `up` leaves the operator "
        "running the Flask development server by hand"
    )
    assert "harness" not in swap_stack.UP_SERVICES, "the test harness is never part of `up`"
    assert "abstergo" not in swap_stack.UP_SERVICES, "abstergo is not part of this stack"

    entrypoint = (Path(swap_stack.__file__).resolve().parent / "docker" / "web_workers_entrypoint.py").read_text()
    assert "gunicorn" in entrypoint, (
        "the web container no longer appears to run gunicorn, so the reason `up` prefers it "
        "over `python app.py` may have expired -- re-read it rather than leaving this as is"
    )


def test_UP_REFUSES_and_starts_NOTHING_when_a_host_worker_is_alive(monkeypatch, capsys):
    """The "exactly one deployment" half, RUN rather than read out of the source.

    THIS TEST USED TO GREP cmd_up's SOURCE for "REFUSED" and "worker_commands()", and
    an extraction on 2026-10-07 moved both into helpers and broke it while the
    behavior was unchanged. That is a test pinning WHERE code lives instead of what it
    does -- the same "the SQL text contains X" evidence CLAUDE.md refuses, in Python.
    It would also have passed on a cmd_up that printed the word REFUSED and started
    the containers anyway.

    So it calls the real cmd_up with one host worker alive and asserts the two things
    that matter: it returns non-zero, and compose IS NEVER RUN. The compose stub
    raises, so a cmd_up that started containers fails loudly here rather than being
    caught by a flag nobody set.
    """
    monkeypatch.setattr(swap_stack, "host_workers_running", lambda: {"payout_worker": 31337})

    def compose_must_not_run(*args, **kwargs):
        raise AssertionError("cmd_up ran docker compose despite a host worker being alive")

    monkeypatch.setattr(swap_stack, "compose", compose_must_not_run)

    assert swap_stack.cmd_up(swap_stack.COMPOSE_FILES) == 1, "a refusal must not report success"

    printed = capsys.readouterr().out
    assert "REFUSED" in printed, "the operator has to be told, not just given an exit code (rule 14)"
    assert "31337" in printed, "and told WHICH process, or they cannot act on it"
    assert "supervisor.py stop" in printed, "and how to resolve it"


def test_the_refusal_reads_the_supervisors_OWN_worker_table(monkeypatch):
    """host_workers_running() must not carry its own list of worker names (rule 8).

    A fourth worker added to supervisor.py has to be covered without editing
    swap_stack.py, or the refusal goes blind to exactly the worker somebody just
    added -- and the failure is six workers on one database, silently.
    """
    asked = []

    monkeypatch.setattr(swap_stack.supervisor, "worker_commands", lambda: {"a_new_worker": ["x"]})
    monkeypatch.setattr(
        swap_stack.supervisor, "worker_status",
        lambda name, run_dir: asked.append(name) or {"state": "running", "pid": 7},
    )

    assert swap_stack.host_workers_running() == {"a_new_worker": 7}
    assert asked == ["a_new_worker"], "it asked about the supervisor's worker, not a hardcoded set"


def test_a_refused_connection_is_NOT_READY_and_names_both_things_it_can_mean():
    """The defect: `up` said BOUND and the next command got Connection refused.

    Measured 2026-10-07. swap_stack.py's `up` printed

        :4943 BOUND  ICP replica (dfx) -- the local ledger and the custody canister

    and the very next call to http://icp-replica:4943 failed with
    `Connection refused (os error 111)`. Both statements were true: a PUBLISHED
    container port is bound by docker-proxy the instant the container is created,
    before the process inside has opened anything.

    So binding proves docker did its part and says nothing about the service -- which
    is rule 13's "verify the artifact, not the deploy" catching the bind check that
    was itself added as the improvement over trusting compose's exit code. One layer
    short of the question an operator actually has.
    AND THE OLD NAME OF THIS TEST CARRIED THE HALF-TRUTH. It was
    test_a_refused_connection_is_NOT_READY_even_though_the_port_is_bound, and it
    asserted the sentence said "nothing is listening" -- both of which assume the
    port IS bound. That holds for a PUBLISHED container port and is false for a
    port on the host's own loopback: under docker-compose.web.hostnet.yml's
    `network_mode: host` there is no docker-proxy, so a refusal means nothing is
    bound AT ALL. Caught 2026-10-07 when the new web probe printed that sentence
    for :5101 under that overlay.

    Neither reading is derivable from the refusal itself, so the verdict names
    both and points at the command that distinguishes them. Renamed rather than
    kept, because a test whose NAME asserts the wrong thing is read by everyone
    who greps for it (rule 2: its test changes to pin the stronger invariant).
    """
    ready, detail = readiness_verdict(ConnectionRefusedError(111, "Connection refused"))
    assert ready is False
    # BOTH readings present, and neither stated as the only one.
    assert "nothing is bound here at all" in detail
    assert "docker-proxy" in detail
    assert "docker compose ps" in detail, "say how to tell them apart, not just that they differ"
    # AND NOT THE OLD ASSERTION, which claimed one of the two as fact.
    assert "the port is bound and nothing is listening behind it" not in detail


def test_an_answer_is_READY_and_a_non_200_is_still_listening():
    """200 is dfx's /api/v2/status. Another code means something IS there, which is the question."""
    ready, detail = readiness_verdict(200)
    assert ready is True
    assert "200" in detail

    ready, detail = readiness_verdict(503)
    assert ready is True, "a 503 came from a process that is listening, which is what is asked"
    assert "503" in detail and "not the 200" in detail


def test_a_timeout_and_an_unrecognized_outcome_are_both_NOT_READY():
    """Defaulting to NOT ready is the safe direction: it waits rather than reporting a lie.

    The unrecognized branch matters because `up` acts on this verdict. A probe outcome
    this function does not understand must not read as success -- it would print READY
    for a replica nobody asked anything of.
    """
    ready, detail = readiness_verdict(TimeoutError("timed out"))
    assert ready is False
    assert "did not answer" in detail

    ready, detail = readiness_verdict("something nobody anticipated")
    assert ready is False
    assert "treated as NOT ready" in detail


def test_DOWN_stops_containers_and_does_NOT_remove_them(monkeypatch, capsys):
    """It removed the replica's container on 2026-10-07 and the ledger canister went with it.

    MEASURED, on the operator's host, after `swap_stack.py down && swap_stack.py up`:

        reject code DestinationInvalid, reject message Canister
        bkyz2-fmaaa-aaaaa-qaaaq-cai not found, error code Some("IC0301")

    The ICP ledger -- deployed, initialized and holding the desk's 1000 test ICP
    across two days of work -- was no longer in the replica. `docker compose down`
    removes containers, and that state did not survive its container.

    `stop` answers the same question: nothing of this stack is running when `down`
    returns, which is what an operator means by it, and the port-bind proof is what
    makes that a measurement. Removing the containers was never part of the
    requirement; it was the default verb I reached for.

    Asserted behaviorally -- the compose subcommand is captured from a stub -- because
    the whole defect was a one-word difference in an argv that no test looked at.
    """
    captured = {}

    def fake_compose(args, files, check=False):
        captured["args"] = list(args)

        class _Done:
            returncode, stdout, stderr = 0, "", ""
        return _Done()

    monkeypatch.setattr(swap_stack, "compose", fake_compose)
    monkeypatch.setattr(swap_stack.supervisor, "main", lambda argv: 0)
    monkeypatch.setattr(swap_stack, "report_listeners", lambda files: [])
    monkeypatch.setattr(swap_stack, "port_is_free", lambda port, host="127.0.0.1": True)

    swap_stack.cmd_down(swap_stack.COMPOSE_FILES)

    assert captured["args"] == ["stop"], (
        f"`down` ran `docker compose {' '.join(captured['args'])}`. `down` REMOVES containers, "
        f"which destroyed the replica's ledger canister on 2026-10-07. Use `stop`: nothing runs "
        f"after it, and the writable layer survives."
    )
    printed = capsys.readouterr().out
    assert "not removed" in printed, "the operator must be told which of the two verbs ran (rule 14)"


# =============================================================================
# THE TWO DECISIONS A `down` MAKES. Both were inlined in swap_stack.py's
# cmd_down() until 2026-10-07 and one of them was wrong there, in the block whose
# only job is to be the proof. These exist because "the only way to test it is to
# run the whole stop" is how that survived (rule 10).
# =============================================================================


def test_an_ipv6_only_listener_is_seen(tmp_path):
    """A listener that appears ONLY in /proc/net/tcp6 must be found.

    THE DEFECT THIS PINS, measured 2026-10-07 on the operator's host. `down`
    printed `:4943 STILL BOUND` and, four lines later, `(none) nothing is
    LISTENing on any of 4943, ...`. Both measurements were honest and they could
    not both be acted on, because the scan read /proc/net/tcp and nothing else
    while docker publishes on 0.0.0.0 AND ::.

    Seeded as a tcp6 LISTEN row with an EMPTY tcp table, which is the arrangement
    the old code was blind to -- an IPv6 row alongside an IPv4 one would pass
    either way and would not have failed before the fix.
    """
    (tmp_path / "tcp").write_text(
        "  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode\n"
    )
    (tmp_path / "tcp6").write_text(
        "  sl  local_address                         remote_address                        st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode\n"
        "   0: 00000000000000000000000000000000:134F 00000000000000000000000000000000:0000 0A 00000000:00000000 00:00000000 00000000     0        0 987654 1 0000 100 0\n"
    )
    body, missing = proc_net_tcp_tables(tmp_path)
    assert missing == []
    # 0x134F == 4943, the ICP replica's port.
    assert listening_inodes(body, {4943}) == {4943: 987654}


def test_a_missing_tcp6_is_reported_and_a_missing_tcp_is_not_fatal_the_same_way(tmp_path):
    """tcp is required, tcp6 is optional, and the caller is told which is absent.

    A kernel booted with ipv6.disable=1 has no /proc/net/tcp6, and refusing to
    report anything there would lose the half that works. A missing tcp means the
    report cannot be made. Returning the names rather than raising is what lets
    swap_stack.py say which out loud (rule 14) instead of printing "(none)" over
    a scan it only half ran.
    """
    (tmp_path / "tcp").write_text("header\n")
    body, missing = proc_net_tcp_tables(tmp_path)
    assert missing == ["tcp6"]
    assert body == "header\n"

    empty = tmp_path / "none"
    empty.mkdir()
    body, missing = proc_net_tcp_tables(empty)
    assert missing == ["tcp", "tcp6"]
    assert body == ""


def test_a_bound_port_with_no_listener_is_not_a_proven_stop():
    """`down` must not claim proof over a port it just reported STILL BOUND.

    THE DEFECT, measured 2026-10-07 on the operator's host. cmd_down() printed

        :4943 STILL BOUND  <- bind attempted, not inferred
        :5100 STILL BOUND  <- bind attempted, not inferred
        (none)             nothing is LISTENing on any of 4943, 5000, 5100, ...
        summary            every process this file owns is gone, proven by bind.

    It branched on the listener count alone, so two failed binds were measured,
    printed, and then not looked at by the sentence claiming to have proven
    something by binding -- rule 13's "skipped" beside "success" in one block.

    The middle verdict is the one that did not exist. It is ambiguous ON PURPOSE:
    port_is_free() omits SO_REUSEADDR so TIME_WAIT reads as bound, and this cannot
    tell that from a listener owned by an unreadable /proc.
    """
    assert down_verdict([], 0) == "down"
    assert down_verdict([4943, 5100], 0) == "stopped_not_proven"
    assert down_verdict([], 1) == "not_down"
    # A SURVIVING LISTENER WINS, and not only because it is worse: it is itself a
    # bound port, so the two findings are one finding seen twice, and reporting
    # the weaker of them would bury the lever (a pid) under a `ss` suggestion.
    assert down_verdict([4943], 1) == "not_down"
    assert set(DOWN_VERDICTS) == {"down", "stopped_not_proven", "not_down"}


def test_the_compose_file_flag_accepts_the_form_its_own_help_text_names():
    """`-f` reaches the same destination as `--compose-file`, on the REAL parser.

    THE DEFECT, 2026-10-07. swap_stack.py accepted `--compose-file` only, while
    its own help read "Repeatable, in -f order". The operator read that sentence,
    typed `-f`, and got

        swap_stack.py: error: unrecognized arguments: -f docker-compose.yml ...

    immediately after a pull -- so the first thing the new code did was refuse a
    command this file had told them to write. Rule 16's wrong comment, in the one
    place an operator actually reads comments.

    ASSERTED THROUGH swap_stack.build_parser(), not a parser this test builds.
    The first version of this test constructed its own ArgumentParser with the
    flags it expected and asserted on that, which proves only that argparse works
    -- a false pass of exactly the shape the behavioral-verification principle
    refuses. build_parser() was extracted from main() so the real thing is
    reachable without starting containers.
    """
    parser = swap_stack.build_parser()

    short = parser.parse_args(["up", "-f", "a.yml", "-f", "b.yml"])
    long_form = parser.parse_args(["up", "--compose-file", "a.yml", "--compose-file", "b.yml"])
    assert short.compose_file == ["a.yml", "b.yml"], "-f must be repeatable and ordered"
    assert short.compose_file == long_form.compose_file, "the two spellings must land in one place"

    # The default, so that an override is distinguishable from no override at all.
    assert parser.parse_args(["up"]).compose_file is None
    assert swap_stack.COMPOSE_FILES == (
        "docker-compose.yml", "docker-compose.icp.yml", "docker-compose.web.yml",
    ), "the hostnet and armed overlays are ALTERNATIVES and must stay off the default"


def test_every_compose_file_the_default_names_exists_on_disk():
    """`up` must not hand docker a `-f` for a file that is not there.

    THE ASSERTION ABOVE PINS THE STRINGS AND THAT IS NOT THE SAME CHECK. It
    compares COMPOSE_FILES against three literals, so renaming
    docker-compose.web.yml on disk leaves it green -- the tuple still equals the
    literals it is compared with, and both are now wrong together. A test that
    pins a name rather than the artifact the name resolves to is rule 13's
    "verify the artifact, not the deploy" wearing a different hat, and the
    operator finds out instead of the suite: `docker compose` exits non-zero on
    the first missing `-f` before it looks at a single service.

    WHAT MADE THIS WORTH WRITING, measured 2026-10-08. docker-compose.yml's own
    header told the operator to start the web service with

        docker compose -f docker-compose.yml -f docker-compose.stateful.yml up -d web

    and no file by that name has existed since the day it was named. The
    stateful services were split out together on 2026-10-05, then split again
    the SAME DAY into one file per required variable (docker-compose.web.yml and
    docker-compose.grc-desk.yml). Both of those files recorded the correction in
    their own headers; docker-compose.yml, the file an operator reads first, did
    not -- so the one pasteable command in it was dead for three days.

    That particular line was prose and this test could never have caught it. A
    gate over comment text cannot tell "here is the command to run" from "here
    is the command that broke", and five of the 35 `-f docker-compose*.yml`
    references in the tree are deliberate citations of the dead name inside
    paragraphs explaining the fix. Gating prose would have flagged all five and
    the only way to pass would be a marker a future reader learns to type, which
    rule 19 calls a suppression.

    So this gates the half that IS mechanical: the tuple `up` actually runs
    with. It would not have caught the stale comment, and it will catch the next
    rename -- which is the one of the two that stops an operator's command
    working without anybody writing a word.
    """
    # swap_stack.REPO_ROOT, not a path this test computes. compose() builds its
    # argv as `["-f", str(REPO_ROOT / name)]` (swap_stack.py:211), so joining
    # against the same attribute means the test resolves what docker will be
    # handed rather than something that merely agrees with it today.
    root = swap_stack.REPO_ROOT
    missing = [name for name in swap_stack.COMPOSE_FILES if not (root / name).is_file()]
    assert not missing, (
        f"COMPOSE_FILES names {missing}, which {'is' if len(missing) == 1 else 'are'} not "
        f"on disk under {root}. Present: "
        f"{sorted(q.name for q in root.glob('docker-compose*.yml')) or '(none)'}. "
        "`swap_stack.py up` passes every one of these to docker as `-f`, and docker "
        "exits non-zero on the first one it cannot open, before it reads any service."
    )


def test_the_help_text_names_only_flags_the_parser_accepts():
    """Every `-x`/`--x` the help string mentions must actually parse.

    The defect above was not that `-f` was missing -- it was that the help NAMED
    a flag the parser rejected, and nothing could notice. This generalizes it:
    whatever spelling the help advertises, the parser must take. Written as a
    loop over the advertised flags rather than a check for `-f` specifically,
    because pinning the one instance would leave the next one free to recur.
    """
    parser = swap_stack.build_parser()
    advertised = {
        token.rstrip(".,)")
        for action in parser._actions  # argparse exposes no public reader for help strings; read-only
        for token in (action.help or "").split()
        if token.startswith("-") and len(token.rstrip(".,)")) > 1
    }
    assert advertised, "this test is worthless if the help mentions no flags at all"
    accepted = {
        option
        for action in parser._actions  # same, and read-only
        for option in action.option_strings
    }
    unaccepted = sorted(advertised - accepted)
    assert not unaccepted, (
        f"the help text advertises {unaccepted}, which the parser does not accept. "
        "That is how an operator was told to type `-f` at a parser that refused it."
    )


# ---------------------------------------------------------------------------
# THE CONTAINERIZED VERDICT. Measured defect, 2026-10-08.
#
# `swap_stack.py status` on the operator's host printed, about the container
# that was serving their page:
#
#     IN A CONTAINER    :5101 pid=85833
#                       in container 40bfc3701f79 = swap-web ..., which
#                       `docker compose ps` above does not list -- so it
#                       belongs to no compose project this stack names.
#                       Stop it with `docker stop 40bfc3701f79`
#
# `docker compose ps` DID list it, four lines up the same report. The tool told
# the operator to stop the web container serving their UI and gave a reason its
# own output contradicted, because the 2026-10-06 finding ("a container no
# compose project names") had been written in as a SENTENCE rather than as a
# check. These pin the check.
# ---------------------------------------------------------------------------


def test_a_container_this_stack_manages_is_OURS_and_gets_no_stop_advice():
    """The exact case that was reported backwards."""
    full = "40bfc3701f79aa11bb22cc33dd44ee55ff6677889900aabbccddeeff00112233"
    verdict, reason = container_verdict("40bfc3701f79", frozenset({full}))

    assert verdict == "ours (container)", (
        "a container `docker compose ps` lists for this stack is this stack's own. "
        "Calling it a stray is what sent `docker stop` at a serving web container."
    )
    # THE STOP ADVICE IS THE HARM, so its absence is asserted directly rather
    # than inferred from the verdict string.
    assert "docker stop" not in reason, (
        f"a container we manage must not be handed a `docker stop`: {reason}"
    )
    assert "belongs to no compose project" not in reason, (
        f"the false claim must be gone, not merely outvoted: {reason}"
    )
    assert "swap_stack.py down" in reason, "the real lever has to be named"


def test_the_prefix_match_is_what_makes_it_work_at_all():
    """A 12-character id against 64-character ids. `==` would never match.

    This is the fix reintroducing the bug if it is written carelessly:
    container_id() returns the first twelve characters, because that is what
    docker prints, and `docker compose ps -q` returns the full 64. Compared with
    equality every container falls through to "not ours" -- which is exactly the
    behavior being fixed, with a check in front of it that cannot ever fire.
    """
    full = "aabbccddeeff00112233445566778899aabbccddeeff00112233445566778899"
    assert len(full) == 64
    short = full[:12]
    assert len(short) == 12
    assert container_verdict(short, frozenset({full}))[0] == "ours (container)"
    # And a container whose id merely SHARES A PREFIX LENGTH but not the prefix
    # is not ours. Twelve hex characters is 48 bits; a collision is not a thing
    # to design against, but a wrong answer here is a stop aimed at a stranger.
    assert container_verdict("ffffffffffff", frozenset({full}))[0] == "container"


def test_a_container_NOT_in_the_listing_is_still_reported_as_a_stray():
    """The 2026-10-06 case is real and must keep working.

    The fix must not swing the other way. A leftover from a `docker run` or an
    earlier project name genuinely is not ours, genuinely holds our port, and
    `docker stop <id>` genuinely is the lever -- so that reason has to survive.
    """
    verdict, reason = container_verdict(
        "deadbeefcafe",
        frozenset({"0" * 64, "1" * 64}),
    )
    assert verdict == "container"
    assert "docker stop deadbeefcafe" in reason, reason
    assert "belongs to no compose project" in reason, reason
    # The denominator, because rule 3 asks for it and because "not in a list of
    # two" and "not in a list of forty" are different strengths of statement.
    assert "2 container(s)" in reason, reason


def test_an_UNOBTAINED_listing_claims_nothing_either_way():
    """Empty is not evidence of absence, and this is the branch that says so.

    docker may not be installed, the daemon may be down, the project may have no
    containers. None of those licenses "belongs to no compose project this stack
    names" -- that claim needs a listing to be absent FROM. Rule 17's line
    between a reason to believe and having checked, as a code path.
    """
    verdict, reason = container_verdict("abc123abc123", frozenset())
    assert verdict == "container", "unknown membership is not ownership"
    assert "docker stop" not in reason, (
        "no stop may be suggested on the strength of a question that was never asked: "
        f"{reason}"
    )
    assert "could not establish" in reason, reason
    assert "never obtained" in reason, reason
    assert "docker inspect abc123abc123" in reason, "the operator still needs a way to look"


def test_a_host_process_is_a_programming_error_not_a_verdict():
    """None means the caller sent a non-containerized listener down this path.

    Raising rather than returning a verdict, because a silent answer here would
    classify every host process as a container with whatever reason the empty
    branch produces -- and that reason talks about docker listings, which would
    be nonsense attached to a host gunicorn.
    """
    with pytest.raises(ValueError, match="containerized"):
        container_verdict(None, frozenset({"a" * 64}))


def test_every_listener_verdict_has_a_label_in_the_report():
    """print_listeners() indexes its label map with [], so a gap is a crash.

    It raises KeyError in the middle of printing, which loses the finding it was
    printing -- the report dies on the row it most needed to show. Adding a
    verdict without a label is a one-line change that passes every other test,
    which is why this one reads the map out of the module rather than restating
    it.
    """
    source = (swap_stack.REPO_ROOT / "swap_stack.py").read_text()
    block = re.search(r"label = \{(.*?)\}\[row\[", source, re.DOTALL)
    assert block, "could not find print_listeners()'s label map; if it was restructured, follow it"
    labeled = set(re.findall(r'"([^"]+)":\s*"[^"]*"', block.group(1)))

    missing = sorted(set(LISTENER_VERDICTS) - labeled)
    assert not missing, (
        f"LISTENER_VERDICTS contains {missing}, which print_listeners()'s label map does not "
        "cover. That map indexes with [], so one of these rows raises KeyError mid-report."
    )
    # And the other direction: a label for a verdict nothing can produce is dead
    # code pretending to be coverage (rule 9).
    orphans = sorted(labeled - set(LISTENER_VERDICTS))
    assert not orphans, (
        f"print_listeners() labels {orphans}, which is not in LISTENER_VERDICTS -- either the "
        "verdict was renamed and the label was left, or the set was not updated."
    )


def test_down_counts_our_own_container_as_still_running():
    """`down` must not prove absence while its own web container still serves.

    cmd_down filtered `row["verdict"] == "ours"` and every containerized listener
    read "container", so a surviving compose service of ours was invisible to the
    proof -- `down` could print "every process this file owns is gone, proven by
    bind" over a gunicorn still answering on 5101. Read out of the source for the
    same reason as the test above: the alternative is running a real `down`.
    """
    source = (swap_stack.REPO_ROOT / "swap_stack.py").read_text()
    pattern = 'still = [row for row in remaining if row["verdict"]'
    assert pattern in source, (
        "could not find cmd_down()'s `still` filter; if it moved, follow it"
    )
    tail = source[source.index(pattern) + len(pattern) :]
    counted = set(re.findall(r'"([^"]+)"', tail[: tail.index("]")]))

    assert "ours" in counted, "a surviving host process of ours was always counted; keep it"
    assert "ours (container)" in counted, (
        "a surviving CONTAINER of ours must count as not-down too. Without it, `down` claims "
        "proof while the compose service it just stopped is still listening."
    )
    assert "container" not in counted, (
        "a container that is NOT ours must stay uncounted -- `down` never stopped it and must "
        "not report a failure to stop what it never touched."
    )


# =============================================================================
# THE SURFACE MAP: does it ever write down a URL for something that is not there?
#
# Operator, 2026-10-08: "so we have 3 canisters now. 3 different hyperlinks.
# where's the main landing page for the atm screen?"
#
# The map answers that, and its whole risk is the answer being confidently wrong.
# A printed `http://127.0.0.1:5101/admin` is a claim that something serves there;
# a printed canister id is a claim that a canister with that id exists on this
# replica. Both claims are cheap to make and expensive to read -- the operator
# pastes one, gets nothing, and now has a second problem to diagnose. These tests
# seed the two inputs and assert on the one thing: NOTHING IS WRITTEN DOWN THAT
# WAS NOT MEASURED.
#
# Same shape as container_verdict()'s tests above, and for the same reason: the
# defect that function fixed was a SENTENCE asserting the result of a check
# nobody ran.
# =============================================================================

#: Ids shaped like real ones (`dfx canister id` output), and deliberately not the
#: ids on any host -- a test that pinned the operator's own ids would start failing
#: the next time their replica volume was recreated, which is exactly the
#: per-replica churn the map refuses to hardcode for.
_SEEDED_IDS = {
    "operator_admin": "uxrrr-q7777-77774-qaaaq-cai",
    "threshold_custody": "ulvla-h7777-77774-qaacq-cai",
    "icp_ledger_canister": "uzt4z-lp777-77774-qaabq-cai",
}


def test_a_url_is_written_only_for_the_port_that_answered():
    """MUTATION: build the URL from WEB_PORT_CANDIDATES[0] instead of the argument.

    That passes every "is there a link" assertion and prints :5101 for a stack
    serving on :5100 -- the operator clicks and gets nothing. The port is the one
    piece of this that was MEASURED, so it is the one piece that must appear.
    """
    lines = web_surface_lines(5100)
    rows = [line for line in lines if line.lstrip().startswith(("PAGE", "ADMIN", "JSON"))]
    assert len(rows) == len(WEB_SURFACES), "every declared surface gets a row"
    assert all("http://127.0.0.1:5100" in row for row in rows)

    # And no other candidate port is named as a URL anywhere in the block.
    for port in WEB_PORT_CANDIDATES:
        if port != 5100:
            assert f"http://127.0.0.1:{port}" not in "\n".join(lines), (
                f"a URL on :{port} appeared while :5100 was the port that answered"
            )


def test_nothing_answered_prints_paths_and_not_one_url():
    """The whole point. MUTATION: drop the `if serving_port` and always build a URL.

    With nothing serving, every candidate port would then be rendered as a link --
    four URLs, none of which answers -- and `up` already exits 1 in that state, so
    the operator would be reading "NOT SERVING" directly above a list of links.
    """
    body = "\n".join(web_surface_lines(None))
    assert "http://" not in body, "nothing answered, so NO url may be written down"
    assert "/admin/controls" in body and "/swap-lookup" in body, (
        "the paths still have to be listed -- the question 'where is the page' has an "
        "answer even when nothing is serving it"
    )
    assert "PATHS AND NOT LINKS" in body, (
        "a reader must be told these are paths; a bare list of paths beside a healthy-looking "
        "report is ambiguous between 'not serving' and 'serving, badly formatted'"
    )


def test_serving_verdicts_zero_means_nothing_answered_not_port_zero():
    """serving_verdict() returns 0 for "none answered". MUTATION: `is not None`.

    That is the bug this guards: `if serving_port is not None` treats the 0 that
    serving_verdict() literally returns as a port, and the map prints
    `http://127.0.0.1:0/` for every surface. cmd_up forwards that return value
    straight in, so the mutation is one character and reaches the operator.
    """
    assert web_surface_lines(0) == web_surface_lines(None)
    assert "http://" not in "\n".join(web_surface_lines(0))


def test_a_canister_id_that_was_not_read_is_never_invented():
    """MUTATION: fall back to a placeholder id, or skip the canister entirely.

    A placeholder renders as a URL that looks openable; skipping it prints an
    empty section, which rule 14 forbids for exactly this reason -- "a blank gap
    is ambiguous between zero rows and a query that broke". Both are worse than
    saying what is not known.
    """
    body = "\n".join(canister_surface_lines({}))
    for name, _page, _what in CANISTER_SURFACES:
        assert name in body, f"{name} must still be named when its id could not be read"
    assert body.count("id COULD NOT BE READ") == len(CANISTER_SURFACES)

    # No canister page URL can exist, because the id is the whole hostname.
    assert ".localhost:4943/" not in body.replace("http://<id>.localhost:4943/", ""), (
        "a canister page URL was built without an id"
    )
    # And no Candid link carrying anything but the explicit `<id>` marker.
    for link in re.findall(r"id=([^\s]+)", body):
        assert link == "<id>", f"a Candid link was built with {link!r}, which was never read"

    assert "Nothing is guessed" in body and "dfx canister id" in body, (
        "the operator needs the reason and the command, not just the absence"
    )


def test_a_canister_id_that_was_read_is_printed_exactly():
    """MUTATION: truncate, lowercase or reorder the id. A canister id is a checksum.

    `dfx canister id` is the only authority for it, so the map's job is to carry
    it unaltered into both URL shapes.
    """
    body = "\n".join(canister_surface_lines(_SEEDED_IDS))
    assert "id COULD NOT BE READ" not in body
    assert f"http://{_SEEDED_IDS['operator_admin']}.localhost:4943/" in body, (
        "operator_admin serves its own page; that URL is the answer to the operator's question"
    )
    for name, ident in _SEEDED_IDS.items():
        assert f"?canisterId={CANDID_UI_CANISTER_ID}&id={ident}" in body, (
            f"{name} must have a Candid link, and it is a query against the Candid UI canister "
            "with the target as a parameter -- not a path under the target"
        )


def test_only_the_canister_that_has_a_page_is_offered_as_one():
    """MUTATION: mark all three PAGE, or mark operator_admin CANDID.

    This is the distinction the operator asked for in so many words: three
    canisters, three hyperlinks, which one is the page? Two of them never will be
    -- the ledger is DFINITY's released wasm and threshold_custody is key
    derivation -- so offering them as pages sends the operator to a 404 and then
    back here to ask again.
    """
    pages = [name for name, serves_page, _what in CANISTER_SURFACES if serves_page]
    assert pages == ["operator_admin"], (
        "exactly one canister serves a page, measured 2026-10-08 as 200 text/html 6227 bytes"
    )

    for body in ("\n".join(canister_surface_lines(_SEEDED_IDS)), "\n".join(canister_surface_lines({}))):
        for line in body.splitlines():
            if line.lstrip().startswith("PAGE"):
                assert "operator_admin" in line, f"a non-page canister was offered as a page: {line}"
            if "threshold_custody" in line or "icp_ledger_canister" in line:
                assert not line.lstrip().startswith("PAGE")
        assert "NO page, and there will never be one" in body


def test_the_replica_status_url_is_labeled_debug_and_not_a_page():
    """It is a fixed address, so it prints with nothing running -- and must not read
    as a liveness claim. MUTATION: label it PAGE, or drop the "NOT a page" clause.
    """
    body = "\n".join(canister_surface_lines({}))
    status_line = next(line for line in body.splitlines() if REPLICA_STATUS_URL in line)
    assert status_line.lstrip().startswith("DEBUG")
    assert "NOT a page" in body


def test_the_landing_page_is_named_as_the_landing_page():
    """The operator's literal question was "where's the main landing page".

    MUTATION: leave `/` as an unannotated row, or list it anywhere but first. A map
    where `/` reads like `/api/health` does not answer the question that produced
    it -- and the answer MOVED on 2026-10-07 (routes/atm.py::start()), so the URL
    in anybody's memory is the retired `/atm`.
    """
    kind, path, what = WEB_SURFACES[0]
    assert (kind, path) == ("PAGE", "/"), "the landing page is the first row of the map"
    assert "LANDING PAGE" in what
    # AND THE RETIRED URL IS NOWHERE IN THE TARGET COLUMN. Checked against the
    # targets rather than the whole block, because the descriptions legitimately
    # say "routes/atm.py" -- the file is still called that; only the URL moved.
    for line in surface_map(5101, _SEEDED_IDS):
        fields = line.split()
        if len(fields) > 1 and fields[0] in ("PAGE", "ADMIN", "JSON"):
            assert not fields[1].endswith("/atm"), (
                f"{fields[1]} is the URL retired on 2026-10-07 when the flow moved to / -- "
                "printing it sends a customer to a 404, and it is the stale URL this map exists "
                "to replace"
            )


def test_the_map_lists_every_route_the_app_actually_declares():
    """Read the decorators and compare. The map going stale IS the defect it fixes.

    `/atm` -> `/` happened on 2026-10-07 and nothing anywhere had to be updated,
    which is why the operator was holding a stale URL. A hand-maintained list of
    routes is rule 8's "two copies of one rule... the copies agree on the day they
    are written and drift from then on", so the second copy is checked against the
    first here rather than trusted.

    `/swap/<id>/fragment` is EXCLUDED ON PURPOSE: it is an HTMX partial, not a
    surface anybody opens, and a map that lists it invites somebody to open it and
    conclude the page is broken.
    """
    declared = set()
    for route_file in sorted((swap_stack.REPO_ROOT / "swap_terminal" / "routes").glob("*.py")):
        source = route_file.read_text()
        constants = dict(re.findall(r'^(\w+)\s*=\s*"([^"]+)"', source, re.MULTILINE))
        for argument in re.findall(r"@bp\.(?:get|post)\(\s*([^)]+?)\s*\)", source):
            literal = argument.strip()
            if literal.startswith(('"', "'")):
                declared.add(literal.strip("\"'"))
            elif literal in constants:
                declared.add(constants[literal])
            else:
                raise AssertionError(
                    f"{route_file.name} registers a route from {literal!r}, which this test cannot "
                    "resolve -- follow it rather than letting the comparison silently shrink"
                )

    def normalize(path: str) -> str:
        return re.sub(r"<[^>]+>", "<id>", path)

    declared = {normalize(path) for path in declared} - {"/swap/<id>/fragment"}
    mapped = {path for _kind, path, _what in WEB_SURFACES}

    assert not declared - mapped, (
        f"routes/ declares {sorted(declared - mapped)}, which the surface map does not list. "
        "A surface nothing names is the state the operator complained about."
    )
    assert not mapped - declared, (
        f"the surface map lists {sorted(mapped - declared)}, which no route declares -- a link "
        "to a path that 404s is worse than no link"
    )


def test_both_up_and_status_print_the_map():
    """One map, two commands. MUTATION: call it from `up` only.

    `up` is run once and `status` is run whenever the operator has lost track,
    which is precisely when the question "where is everything" gets asked. Read
    out of the source for the same reason as the two tests above: the alternative
    is running a real `up` against docker.
    """
    source = (swap_stack.REPO_ROOT / "swap_stack.py").read_text()
    for command in ("def cmd_up(", "def cmd_status("):
        body = source[source.index(command) : source.index(command) + 6000]
        body = body[: body.index("\ndef ") if "\ndef " in body else len(body)]
        assert "_say_surface_map(" in body, f"{command.strip()} does not print the surface map"

    assert "def probe_serving_port(" in source, (
        "the prober must be one function both commands call; two loops drift (rule 8)"
    )


def test_the_replica_status_url_is_spelled_in_exactly_one_place():
    """MUTATION: re-add the literal to swap_stack.py. Rule 8, measured by grep.

    It WAS in both for about an hour on 2026-10-08 while the map was being
    written, which is how long it takes for this kind of duplicate to appear.
    """
    assert REPLICA_STATUS_URL == "http://127.0.0.1:4943/api/v2/status"
    for name in ("swap_stack.py", "swap_terminal/stack_authority.py"):
        source = (swap_stack.REPO_ROOT / name).read_text()
        spelled = source.count('"' + REPLICA_STATUS_URL + '"')
        expected = 1 if name.endswith("stack_authority.py") else 0
        assert spelled == expected, (
            f"{name} spells the replica status URL {spelled} time(s), expected {expected} -- the "
            "address belongs to stack_authority.py and the wait budgets to swap_stack.py"
        )


def test_the_short_web_probe_is_quiet_and_the_replicas_long_wait_is_not():
    """Rule 14 by its own criterion, not applied uniformly.

    wait_for_http() prints a line per attempt, which is correct for the replica:
    60 seconds against a cold `dfx start`, where silence is indistinguishable
    from a hang and resolves as Ctrl-C on this project.

    It is wrong for the web probe. That budget is 3 seconds PER PORT and
    probe_serving_port() prints each port's outcome as it goes, so the per-port
    line already IS the progress -- and the per-attempt lines on top of it put
    twelve near-identical sentences in front of the surface map, in `status`, a
    read-only command whose value is a quick answer. Twelve copies of one
    sentence is the same defect as silence, approached from the other side.

    Read out of the source because the alternative is waiting out two real
    probes against ports nothing is serving.
    """
    source = (swap_stack.REPO_ROOT / "swap_stack.py").read_text()

    # LOCATED BY THE BUDGET CONSTANT, not by matching the url literal: the call
    # spans several lines and its f-string carries quotes that any regex written
    # here has to escape past, which is how the first version of this test ended
    # up with a pattern that would not parse.
    web_at = source.index("_WEB_PROBE_BUDGET_SECONDS, announce")
    assert web_at, "unreachable; index() raises"
    web_call = source[source.rindex("wait_for_http(", 0, web_at) : web_at + 60]
    assert "announce=False" in web_call, (
        "the per-port web probe must pass announce=False. Without it, `status` prints "
        "wait_for_http's per-attempt line for every attempt on every candidate port "
        f"before the surface map. The call reads: {web_call}"
    )

    replica_at = source.index("wait_for_http(REPLICA_STATUS_URL")
    replica_call = source[replica_at : source.index(")", replica_at) + 1]
    assert "announce=False" not in replica_call, (
        "the replica's wait must KEEP its per-attempt progress. Its budget is 60s against a "
        "cold `dfx start`, and a silent wait that long is what rule 14 exists for -- an "
        "operator who cannot tell working from hung reaches for Ctrl-C, which on this "
        f"project means killing a live cycle. The call reads: {replica_call}"
    )


# ---------------------------------------------------------------------------
# "NOT DEPLOYED" IS NOT "COULD NOT READ". Measured 2026-10-08, and the merge of
# the two cost the operator three canisters without a word of warning.
#
# Their replica container was created two hours before
# icp-replica-data:/root/.local/share/dfx entered docker-compose.icp.yml, so its
# dfx state was in the container's WRITABLE LAYER. `down` stopped it and the
# layer survived -- which is exactly what `down` was rewritten to guarantee.
# Then `up` rebuilt both images, compose recreated the container because its
# image had changed, and the layer went with it: the ICP ledger holding the
# desk's 998.9498 LICP, threshold_custody, and operator_admin.
#
# `up` asked dfx for each id. dfx answered, three times:
#
#     Error: Cannot find canister id. Please issue 'dfx canister create <name>'.
#
# and the report printed COULD NOT READ, under a header reading "Two causes and
# this cannot tell them apart: docker/dfx was not reachable, or the canisters
# are not deployed on this replica." dfx had just told them apart. `up` printed
# SERVING and exited 0.
# ---------------------------------------------------------------------------

#: What dfx actually said, verbatim, so the match is tested against the real
#: string rather than a paraphrase of it.
DFX_ABSENT_STDERR = (
    "Error: Cannot find canister id. Please issue 'dfx canister create operator_admin'."
)


def test_dfx_saying_it_cannot_find_the_id_is_NOT_DEPLOYED_and_not_unreachable():
    """The exact output, and the exact distinction that was missing."""
    ident, kind, why = canister_lookup_verdict(255, "", DFX_ABSENT_STDERR)
    assert ident is None
    assert kind == "not_deployed", (
        "dfx ANSWERED that the replica has no such canister. Filing that as 'unreachable' is "
        "what let three canisters vanish behind a line an operator reads as a docker hiccup."
    )
    assert "Cannot find canister id" in why, "the operator must see what dfx actually said"


def test_a_docker_failure_is_unreachable_and_claims_nothing_about_deployment():
    """The other direction, and it is the worse one to get wrong.

    Asserting a canister is absent because docker would not answer would tell an
    operator their canisters are gone while they are sitting there -- a false
    alarm of exactly the kind that makes the real one get ignored.
    """
    for returncode, stdout, stderr in (
        (1, "", "Cannot connect to the Docker daemon at unix:///var/run/docker.sock"),
        (1, "", 'service "icp-replica" is not running'),
        (125, "", "Error response from daemon: No such container"),
        (1, "", ""),
    ):
        ident, kind, _why = canister_lookup_verdict(returncode, stdout, stderr)
        assert ident is None
        assert kind == "unreachable", (
            f"{stderr!r} must read as unreachable, not as a claim about deployment"
        )


def test_an_id_that_was_read_is_found_even_behind_dfx_warnings():
    """dfx prints warnings above its answer on some versions.

    The LAST line is the id. Pasting the whole buffer into a url produces an
    unopenable link that LOOKS like one, which is the one thing the map must not
    do -- so this pins that the warning is dropped and the id survives.
    """
    noisy = (
        "Using the default definition for the 'local' shared network because\n"
        "/root/.config/dfx/networks.json does not exist.\n"
        "br5f7-7uaaa-aaaaa-qaaca-cai"
    )
    ident, kind, _why = canister_lookup_verdict(0, noisy, "")
    assert kind == "found"
    assert ident == "br5f7-7uaaa-aaaaa-qaaca-cai", f"got {ident!r}"


def test_an_empty_success_is_not_an_id():
    """returncode 0 with no output must not become an empty-string id.

    An empty id interpolates into `http://.localhost:4943/`, a link that resolves
    to nothing and looks deliberate.
    """
    ident, kind, _why = canister_lookup_verdict(0, "   \n", "")
    assert ident is None
    assert kind == "unreachable"


def test_the_map_SHOUTS_when_the_replica_has_none_and_says_what_it_costs():
    """The line the operator should have seen instead of COULD NOT READ."""
    shouted = "\n".join(surface_map(5100, {}, absent=3))
    assert "NO CANISTERS DEPLOYED" in shouted
    assert "THEY ARE GONE" in shouted, (
        "the map must say what it means for a replica that HAD canisters, because that is "
        "the reading an operator needs within one second of seeing it"
    )
    assert "dfx deploy" in shouted, "name the remedy"
    assert "icp_ledger_init.did" in shouted, (
        "a redeployed ledger restores only what that file seeds -- a balance minted "
        "afterwards has to be minted again, and not saying so invites a wrong all-clear"
    )
    assert "NOT DEPLOYED on this replica" in shouted, "each canister's own row must say it too"
    assert "cannot tell them apart" not in shouted, (
        "the header must stop claiming the causes are indistinguishable; dfx distinguished them"
    )


def test_the_map_still_says_COULD_NOT_READ_when_dfx_was_never_asked():
    """absent=0 with nothing read is the docker-unreachable case, and differs."""
    quiet = "\n".join(surface_map(5100, {}, absent=0))
    assert "COULD NOT BE READ" in quiet
    assert "NO CANISTERS DEPLOYED" not in quiet, (
        "an unreachable docker must not be reported as a destroyed deployment"
    )
    assert "says nothing about whether they are deployed" in quiet
