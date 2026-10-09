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

import inspect
import re
import socket
import subprocess
from pathlib import Path

import pytest

import swap_stack
from swap_terminal.stack_authority import (
    CANDID_UI_CANISTER_NAME,
    CANISTER_SURFACES,
    CHAIN_EXIT_CODES,
    CHAIN_PROBE_ROWS_KEY,
    DOWN_VERDICTS,
    LISTENER_VERDICTS,
    NEVER_STOPPED,
    REPLICA_STATE_PATH,
    REPLICA_STATUS_URL,
    VERSION_STATUSES_WORTH_REPEATING,
    WEB_PORT_CANDIDATES,
    WEB_SURFACES,
    GitReading,
    candid_url,
    canister_lookup_names,
    canister_lookup_verdict,
    canister_surface_lines,
    chain_exit_code,
    chain_probe_envelope,
    chain_probe_rows,
    chain_reachability_verdict,
    code_version_verdict,
    container_id,
    container_label,
    container_verdict,
    down_verdict,
    hex_port,
    listening_inodes,
    pids_owning_inodes,
    port_is_free,
    probe_detail,
    proc_net_tcp_tables,
    process_name,
    readiness_verdict,
    replica_state_verdict,
    serving_verdict,
    stray_verdict,
    surface_map,
    version_status,
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
    verdict = readiness_verdict(ConnectionRefusedError(111, "Connection refused"))
    ready, summary, advice = verdict
    assert ready is False
    # THE SHORT REASON IS SHORT, which is the half a progress line can use. It was
    # one 40-word string until 2026-10-09, so a web probe's four per-port lines
    # each carried the whole paragraph -- see probe_detail().
    assert summary == "connection refused", summary
    # BOTH readings present, and neither stated as the only one.
    assert "nothing is bound here at all" in advice
    assert "docker-proxy" in advice
    assert "docker compose ps" in advice, "say how to tell them apart, not just that they differ"
    # AND NOT THE OLD ASSERTION, which claimed one of the two as fact.
    assert "the port is bound and nothing is listening behind it" not in advice
    # The joined form still says everything it used to: nothing was lost in the
    # split, it was only made separable.
    assert probe_detail(verdict) == f"{summary} -- {advice}"


def test_an_answer_is_READY_and_a_non_200_is_still_listening():
    """200 is dfx's /api/v2/status. Another code means something IS there, which is the question."""
    verdict = readiness_verdict(200)
    ready, summary, advice = verdict
    assert ready is True
    assert "200" in summary
    # NO ADVICE, AND probe_detail() MUST NOT LEAVE A DANGLING SEPARATOR. A 200
    # needs no gloss, so the joined form is the summary alone rather than
    # "answered 200 -- ".
    assert advice == "", advice
    assert probe_detail(verdict) == summary

    ready, summary, advice = readiness_verdict(503)
    assert ready is True, "a 503 came from a process that is listening, which is what is asked"
    assert "503" in summary and "not the 200" in advice


def test_a_timeout_and_an_unrecognized_outcome_are_both_NOT_READY():
    """Defaulting to NOT ready is the safe direction: it waits rather than reporting a lie.

    The unrecognized branch matters because `up` acts on this verdict. A probe outcome
    this function does not understand must not read as success -- it would print READY
    for a replica nobody asked anything of.
    """
    ready, summary, advice = readiness_verdict(TimeoutError("timed out"))
    assert ready is False
    assert "did not answer" in advice
    assert summary == "timed out", summary

    ready, summary, advice = readiness_verdict("something nobody anticipated")
    assert ready is False
    assert "treated as NOT ready" in advice
    # AND THE UNRECOGNIZED BRANCH MUST NOT FIRE ON AN ORDINARY OUTCOME, which is
    # the half that was broken in practice rather than in theory. See
    # test_a_refused_port_is_never_reported_as_an_unrecognized_outcome below:
    # swap_stack.probe_serving_port() stored this function's own rendered sentence
    # and serving_verdict() fed it back in, so every connection refusal on every
    # candidate port landed here.
    assert "unrecognized" in summary


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


def _refused() -> tuple[bool, str, str]:
    """A refusal verdict, as readiness_verdict() produces it.

    THROUGH THE REAL readiness_verdict() rather than a hand-written 3-tuple, for
    the same reason _chain_body() builds through chain_probe_envelope(): a
    fabricated verdict cannot disagree with the author's belief about the shape,
    and that is what let the double-interpretation defect this file's tests now
    cover go unnoticed. No port argument -- the verdict does not depend on which
    port refused, and a parameter nobody reads is rule 9's dead name.
    """
    return readiness_verdict(ConnectionRefusedError(111, "Connection refused"))


def test_a_refused_port_is_never_reported_as_an_unrecognized_outcome():
    """serving_verdict() had NO direct test, and this is the defect that cost.

    MEASURED 2026-10-09 by running `swap_stack.py chains` with nothing up:

        NOT SERVING  nothing answered on any candidate port -- :5101 unrecognized
                     probe outcome 'connection refused -- either nothing is bound
                     here at all, or ...' -- treated as NOT ready rather than
                     guessed, :5100 unrecognized probe outcome '...' [x4, one line]

    probe_serving_port() stored readiness_verdict()'s RENDERED SENTENCE and
    serving_verdict() passed it back INTO readiness_verdict(), where a str is
    neither an int nor an OSError and fell through to the final branch. So the
    most ordinary outcome a web probe has -- a refusal -- was reported as
    `unrecognized`, i.e. the fail-closed branch firing on the normal case. Rule
    13: "a warning that fires on every run is one the reader learns to ignore."

    Four tests covered readiness_verdict() and all four passed throughout,
    because none of them went through serving_verdict(). This is that test.
    """
    port, headline, detail = serving_verdict({p: _refused() for p in WEB_PORT_CANDIDATES})
    assert port == 0, f"every port refused and this reported {port} as serving"
    said = " ".join([headline, *detail])
    assert "unrecognized" not in said, (
        f"a plain connection refusal is reported as an unrecognized outcome, which sends the "
        f"operator looking for something exotic: {said}"
    )
    assert "connection refused" in said, f"and it must still say what happened: {said}"


def test_the_same_reason_on_every_port_is_explained_once_not_once_per_port():
    """Rule 14, from the other side: five copies of one paragraph is not output.

    The same run printed the refusal paragraph FIVE times on one screen -- once
    per candidate port as progress, then four more concatenated into a single
    160-column headline. wait_for_http()'s own announce=False comment already
    names this defect ("twelve near-identical sentences"); it arrived again by a
    different route, which is why the grouping is now asserted rather than
    reviewed.

    MUTATION: key `by_reason` by port instead of by summary. The headline then
    repeats the reason per port and `detail` carries one advice line per port.
    """
    _port, headline, detail = serving_verdict({p: _refused() for p in WEB_PORT_CANDIDATES})
    assert len(WEB_PORT_CANDIDATES) >= 2, "this test needs more than one candidate port to mean anything"
    assert headline.count("connection refused") == 1, (
        f"one reason shared by {len(WEB_PORT_CANDIDATES)} ports is named "
        f"{headline.count('connection refused')} times: {headline}"
    )
    for port in WEB_PORT_CANDIDATES:
        assert f":{port}" in headline, f"every port asked must still be named: {headline}"
    assert len(detail) == 1, f"one distinct reason must yield one advice line, got {len(detail)}: {detail}"
    assert "docker compose ps" in detail[0], detail[0]


def test_two_ports_failing_differently_both_get_their_reason():
    """The grouping must not collapse DIFFERENT failures, only identical ones.

    A refusal and a timeout mean different things to fix -- nothing bound versus
    something accepting and not answering -- so a summary that showed only the
    first would send the operator at the wrong half.
    """
    first, *rest = WEB_PORT_CANDIDATES
    answers = {first: readiness_verdict(TimeoutError("timed out"))}
    answers.update({port: _refused() for port in rest})
    port, headline, detail = serving_verdict(answers)
    assert port == 0
    assert "timed out" in headline and "connection refused" in headline, headline
    assert len(detail) == 2, f"two distinct reasons, two advice lines: {detail}"


def test_a_port_that_answered_wins_over_the_ones_that_did_not():
    """The positive path, in candidate order rather than dict order."""
    answers = {port: _refused() for port in WEB_PORT_CANDIDATES}
    answers[WEB_PORT_CANDIDATES[-1]] = readiness_verdict(200)
    port, headline, detail = serving_verdict(answers)
    assert port == WEB_PORT_CANDIDATES[-1], f"the port that answered 200 is {port}"
    assert "200" in headline, headline
    assert detail == [], f"a serving page has no failure advice to give: {detail}"


def test_nothing_probed_is_not_an_all_clear():
    """Rule 14: "(none) is a result". An empty probe set must not read as serving."""
    port, headline, _detail = serving_verdict({})
    assert port == 0
    assert "NOT ESTABLISHED" in headline, headline


def test_say_wrapped_keeps_a_value_inside_the_report_width(capsys):
    """Prose that arrives as a VALUE has no author at the print site.

    readiness_verdict()'s advice is 150 characters. Printed raw under a 20-column
    indent it ran to 190 and the terminal broke it at an arbitrary column with no
    indent, so the continuation ran back under the label column and read as a new
    field. A report whose columns stop lining up halfway down is one an operator
    stops trusting.
    """
    advice = readiness_verdict(ConnectionRefusedError(111, "refused"))[2]
    assert len(advice) > 100, f"this test is pointless if the advice is short: {len(advice)}"
    swap_stack.say_wrapped(" " * 20, advice)
    lines = capsys.readouterr().out.splitlines()
    assert len(lines) > 1, "a 150-character value under a 20-column indent must wrap"
    for line in lines:
        assert len(line) <= swap_stack._REPORT_WIDTH, f"{len(line)} columns: {line}"
        assert line.startswith(" " * 20), f"a continuation line lost its indent: {line!r}"


def test_say_wrapped_prints_the_label_even_when_the_value_is_empty(capsys):
    """textwrap.wrap("") is [], and a label with nothing under it is rule 14's blank gap."""
    swap_stack.say_wrapped(" " * 20, "", first="  LABEL   ")
    out = capsys.readouterr().out
    assert "LABEL" in out, f"an empty value swallowed its own label: {out!r}"


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
    # A SEEDED UI ID, because the Candid link is a query against that canister and
    # it is no longer a constant -- see CANDID_UI_CANISTER_NAME. Deliberately NOT
    # the "be2us" that used to be hardcoded: a test that happens to pass the old
    # constant back in would not notice the constant returning.
    ui = "bd3sg-teaaa-aaaaa-qaaba-cai"
    body = "\n".join(canister_surface_lines(_SEEDED_IDS, ui_canister_id=ui))
    assert "id COULD NOT BE READ" not in body
    assert f"http://{_SEEDED_IDS['operator_admin']}.localhost:4943/" in body, (
        "operator_admin serves its own page; that URL is the answer to the operator's question"
    )
    for name, ident in _SEEDED_IDS.items():
        assert f"?canisterId={ui}&id={ident}" in body, (
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


def test_an_unread_candid_ui_id_writes_NO_LINK_rather_than_a_guessed_one():
    """The fix for the live defect of 2026-10-08, pinned at fail-closed.

    THE DEFECT. `CANDID_UI_CANISTER_ID = "be2us-64aaa-aaaaa-qaabq-cai"` was a
    hardcoded constant, under a comment asserting the Candid UI "is the SAME id on
    every fresh replica -- unlike the project's own canisters, which are issued per
    replica and must be asked for." The operator's redeploy printed its creations
    in order and refuted it: a local replica hands out ids from a fixed sequence in
    CREATION ORDER, the UI canister landed THIRD that time (bd3sg-teaaa-aaaaa-qaaba-cai),
    and be2us had become the default identity's WALLET canister. Every CANDID row
    in the map pointed at the wallet, loading the wrong canister's interface while
    looking entirely correct.

    That is the exact failure canister_ids()'s own docstring describes -- "an id
    written into this repository is a link to a deployment that no longer exists --
    or worse, resolves and shows the operator somebody else's canister" -- and this
    constant was the one place in the file exempt from it.

    WHY FAIL CLOSED RATHER THAN FALL BACK. There is no id to fall back TO: any
    default is the guess that just failed. A missing link costs the operator one
    `dfx canister id` call; a wrong one costs them whatever they conclude from the
    wrong canister's interface.
    """
    no_ui = "\n".join(canister_surface_lines(_SEEDED_IDS, ui_canister_id=""))

    assert "?canisterId=" not in no_ui, (
        "a Candid URL was written with no UI canister id. Whatever id it carries is a guess, "
        "and a guessed Candid link shows the wrong canister's interface."
    )
    assert "be2us" not in no_ui, "the old hardcoded id must not survive as a fallback anywhere"
    assert "NO CANDID LINK" in no_ui, "the absence must be stated, not left as a blank (rule 14)"

    # EVERY refusing line names the lookup, not just one of them. The first
    # version of this assertion was `CANDID_UI_CANISTER_NAME in no_ui` -- true if
    # ANY line named it -- and a mutation that stripped the name from one of the
    # two refusal sites survived, because the other still carried it. There are
    # two shapes (a page canister's "its Candid interface: NOT LINKED" and a
    # Candid-only row's "NO CANDID LINK"), so the check has to be per-line.
    refusals = [
        line for line in no_ui.splitlines()
        if "NO CANDID LINK" in line or "NOT LINKED" in line
    ]
    assert len(refusals) >= 2, (
        f"expected a refusal for the page canister AND each Candid-only one; got {refusals}"
    )
    for line in refusals:
        assert CANDID_UI_CANISTER_NAME in line, (
            f"this refusal does not name {CANDID_UI_CANISTER_NAME}, so the operator is told the "
            f"link is missing without being told which lookup produces it: {line.strip()}"
        )
    # The ids that WERE read are still printed: one unread id must not suppress
    # three known ones.
    for ident in _SEEDED_IDS.values():
        assert ident in no_ui, f"{ident} was read and must still appear"


def test_the_candid_url_builder_refuses_an_empty_ui_id():
    """The decision itself, below the rendering. Pure.

    candid_url() returning "" for an unknown UI id is what makes every caller's
    fail-closed branch reachable; a builder that returned a URL with an empty
    canisterId would produce `?canisterId=&id=...`, which loads and shows nothing
    while looking like a link somebody meant to write.
    """
    assert candid_url("bkyz2-fmaaa-aaaaa-qaaaq-cai", "") == ""
    built = candid_url("bkyz2-fmaaa-aaaaa-qaaaq-cai", "bd3sg-teaaa-aaaaa-qaaba-cai")
    assert built == (
        "http://127.0.0.1:4943/?canisterId=bd3sg-teaaa-aaaaa-qaaba-cai"
        "&id=bkyz2-fmaaa-aaaaa-qaaaq-cai"
    ), built
    # The order matters and is the thing that was easy to get backwards: the UI
    # canister is what the browser loads, the target is its parameter.
    assert built.index("canisterId=bd3sg") < built.index("id=bkyz2")


# =============================================================================
# WHICH CODE PRINTED THIS
#
# The operator ran `up` on 2026-10-08 and pasted sixty lines back. Three of them
# named `be2us-64aaa-aaaaa-qaabq-cai` as the Candid UI canister -- an id that
# 4d82caf had deleted and pushed eleven minutes earlier, and that their own dfx
# output in the same terminal had already contradicted. Nothing in sixty lines of
# deliberately self-describing output said which commit produced them.
#
# These tests pin the half that must not regress: a report that cannot establish
# it is current says so, and NEVER degrades into saying it is.
# =============================================================================


def _reading(**fields) -> GitReading:
    """A GitReading that is current unless a test says otherwise.

    The default is the HAPPY case on purpose: every test below then names only the
    one field it is about, so a reader sees the input that produces the verdict
    rather than seven keyword arguments of noise.
    """
    base = {
        "head": "4d82caf", "branch": "claude/xrp-adapter", "modified": 0,
        "behind": 0, "ahead": 0, "upstream": "origin/claude/xrp-adapter", "reason": "",
    }
    return GitReading(**{**base, **fields})


def test_version_status_classifies_every_case():
    """The classification table, asserted as a table."""
    assert version_status(_reading()) == "current"
    assert version_status(_reading(behind=1)) == "stale"
    assert version_status(_reading(behind=1, ahead=2)) == "diverged"
    assert version_status(_reading(ahead=2)) == "ahead"
    assert version_status(_reading(modified=3)) == "modified"
    assert version_status(_reading(upstream="", reason="no upstream")) == "unknown"
    assert version_status(_reading(head="", reason="git is not on PATH")) == "unknown"


def test_an_uncomparable_checkout_never_reads_as_current():
    """FAIL CLOSED. The one assertion this whole section exists for.

    A version check that degrades to "looks fine" when it cannot check is worse
    than no version check, because it converts "nobody looked" into an all-clear
    the operator has no reason to doubt. Same shape as candid_url() returning ""
    rather than a link built on an id nobody read.
    """
    for broken in (
        _reading(upstream="", reason="fatal: upstream branch not stored as a remote-tracking branch"),
        _reading(head="", reason="git is not on PATH"),
        _reading(upstream="", reason="could not count commits against origin/x: boom"),
    ):
        status, headline, detail = code_version_verdict(broken)
        assert status == "unknown", f"{broken} classified as {status}"
        said = " ".join([headline, *detail]).lower()
        for claim in ("tree clean", "== origin", "as last fetched", "up to date"):
            assert claim not in said, (
                f"a reading that could not be compared printed {claim!r}, which an operator "
                f"reads as an all-clear: {said}"
            )
        assert "nobody checked" in said or "names no commit" in said, (
            f"an unknown verdict must say nobody checked, not merely omit the claim: {said}"
        )


def test_a_stale_run_cannot_be_skimmed_as_a_current_one():
    """Rule 14: 'did nothing' must not look like 'did work'. Here: old must not look new."""
    _s, current_head, _d = code_version_verdict(_reading())
    stale_status, stale_head, stale_detail = code_version_verdict(_reading(behind=1))
    assert stale_status == "stale"
    assert "***" in stale_head and "***" not in current_head, (
        "the stale headline must be visually distinct from the current one; an operator "
        f"skims sixty lines and reads the shape. current={current_head!r} stale={stale_head!r}"
    )
    assert "BEHIND origin/claude/xrp-adapter" in stale_head
    said = " ".join(stale_detail)
    assert "ALREADY FIXED" in said, "a reader must be told the defect below may be fixed already"
    assert "NOTHING NEEDS REBUILDING" in said, (
        "the remedy is cheap only because swap_stack.py runs on the host; an operator who "
        "thinks a pull needs a rebuild will not do it mid-session"
    )


def test_the_remedy_is_a_command_the_operator_can_paste():
    """`git pull origin claude/xrp-adapter`, not `git pull origin/claude/xrp-adapter`.

    The operator pastes what this prints. A ref spelled with the slash is not a
    valid `git pull` invocation, and a remedy that errors is worse than none --
    it costs the round trip AND the trust in the next line this tool prints.
    """
    _status, _headline, detail = code_version_verdict(_reading(behind=2))
    remedy = next(line for line in detail if line.startswith("remedy:"))
    assert "git pull origin claude/xrp-adapter" in remedy, remedy
    assert "origin/claude/xrp-adapter" not in remedy, (
        f"the upstream ref was pasted whole into a pull command: {remedy}"
    )


def test_a_dirty_tree_on_a_stale_branch_is_still_stale_and_still_says_dirty():
    """Precedence, and the fact that the quieter half is not lost to it."""
    status, headline, detail = code_version_verdict(_reading(behind=1, modified=3))
    assert status == "stale", "a dirty tree must not downgrade a stale warning"
    said = " ".join([headline, *detail])
    assert "3 tracked files MODIFIED" in said, (
        f"the dirty count disappeared when the branch was also stale: {said}"
    )


def test_counts_read_as_english_at_one():
    """One commit, not 1 commits. The line is read by a person, every run."""
    _s, one, _d = code_version_verdict(_reading(behind=1))
    _s, two, _d = code_version_verdict(_reading(behind=2))
    assert "1 COMMIT BEHIND" in one and "2 COMMITS BEHIND" in two
    _s, head_one, _d = code_version_verdict(_reading(modified=1))
    assert "1 tracked file MODIFIED" in head_one, head_one


def test_every_status_worth_repeating_is_one_the_classifier_can_produce():
    """Rule 8's shape: a second spelling of a vocabulary drifts from the first.

    VERSION_STATUSES_WORTH_REPEATING is a hand-written set of status strings, and
    a typo in it would make the end-of-run warning silently never fire -- the
    failure would be a MISSING line, which nothing else would ever notice.
    """
    producible = {
        version_status(_reading(**case))
        for case in ({}, {"behind": 1}, {"behind": 1, "ahead": 2}, {"ahead": 2},
                     {"modified": 1}, {"upstream": ""}, {"head": ""})
    }
    assert producible >= VERSION_STATUSES_WORTH_REPEATING, (
        f"{VERSION_STATUSES_WORTH_REPEATING - producible} can never be returned, so the "
        f"end-of-run warning for it would never fire"
    )
    assert {"stale", "diverged"} == VERSION_STATUSES_WORTH_REPEATING, (
        "the two where the output can be WRONG relative to code that already exists"
    )


def test_the_repeat_fires_only_for_the_two_loud_statuses(capsys):
    """And it fires at all. Both halves, because either one alone is a broken warning."""
    for quiet in ("current", "modified", "ahead", "unknown"):
        swap_stack._say_code_version_repeat(quiet, "whatever")
        assert capsys.readouterr().out == "", f"{quiet} printed a second warning nobody acts on"
    for loud in sorted(VERSION_STATUSES_WORTH_REPEATING):
        swap_stack._say_code_version_repeat(loud, "*** 1 COMMIT BEHIND origin/x. ***")
        printed = capsys.readouterr().out
        assert "1 COMMIT BEHIND" in printed, f"{loud} printed no end-of-run warning: {printed!r}"
        assert "nothing above was produced" in printed, (
            f"the repeat must say what it is repeating ABOUT, not just repeat: {printed!r}"
        )


def test_git_reading_carries_the_reason_a_read_failed(monkeypatch):
    """Every failure path, and each one must arrive with git's own sentence attached.

    A reading that says `unknown` with no reason tells the operator to go and find
    out -- the round trip rule 20 says not to charge them. canister_lookup_verdict()
    was written for the same defect one command over.
    """
    answers: dict[tuple, tuple[int, str]] = {}
    monkeypatch.setattr(swap_stack, "_git", lambda *a: answers.get(a, (0, "")))
    monkeypatch.setattr(swap_stack.shutil, "which", lambda _name: "/usr/bin/git")

    answers = {
        ("rev-parse", "--short", "HEAD"): (0, "4d82caf"),
        ("rev-parse", "--abbrev-ref", "HEAD"): (0, "claude/xrp-adapter"),
        ("status", "--porcelain", "--untracked-files=no"): (0, ""),
        ("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}"):
            (128, "fatal: no upstream configured for branch 'claude/xrp-adapter'"),
    }
    no_upstream = swap_stack.git_reading()
    assert no_upstream.head == "4d82caf" and no_upstream.upstream == ""
    assert "no upstream configured" in no_upstream.reason, no_upstream.reason

    answers[("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}")] = (0, "origin/main")
    answers[("rev-list", "--left-right", "--count", "HEAD...origin/main")] = (0, "not a number")
    unparseable = swap_stack.git_reading()
    assert unparseable.upstream == "", (
        "an unreadable commit count must not leave the upstream set with 0 behind, which "
        "would print as 'your code is current'"
    )
    assert "not a number" in unparseable.reason, unparseable.reason

    answers[("rev-list", "--left-right", "--count", "HEAD...origin/main")] = (0, "2\t5")
    counted = swap_stack.git_reading()
    assert (counted.ahead, counted.behind) == (2, 5), (
        "`--left-right --count` prints LEFT then RIGHT, and HEAD is the left side -- "
        "transposing them turns 'you are 5 behind' into 'you are 5 ahead'"
    )
    assert version_status(counted) == "diverged"


def test_a_tree_that_could_not_be_read_is_not_reported_clean(monkeypatch):
    """The quietest fail-open there was, and the easiest to write by accident.

    THE STUB FAILS EXACTLY ONE READ, and the first version of this test did not --
    it answered every call after the branch lookup with the same error, so the
    upstream read failed too and produced `unknown` by itself. A mutation making
    `_git_local_state` report a clean tree on a failed `git status` SURVIVED that
    test, because the verdict was already being decided by the wrong failure. The
    stub below lets everything else succeed, so the tree read is the only thing
    that can produce the verdict.
    """
    monkeypatch.setattr(swap_stack.shutil, "which", lambda _name: "/usr/bin/git")
    monkeypatch.setattr(swap_stack, "_git", lambda *a: (
        (0, "4d82caf") if a[:2] == ("rev-parse", "--short") else
        (0, "claude/x") if a == ("rev-parse", "--abbrev-ref", "HEAD") else
        (1, "fatal: index.lock exists") if a[0] == "status" else
        (0, "origin/claude/x") if a[-1] == "@{upstream}" else
        (0, "0\t0")
    ))
    reading = swap_stack.git_reading()
    assert version_status(reading) == "unknown", (
        f"a tree that could not be read was classified {version_status(reading)}: {reading}"
    )
    assert "index.lock" in reading.reason, reading.reason


def test_current_disclaims_the_fetch_it_did_not_do():
    """`current` means 'matches what you last fetched', and must say so in those words.

    Nothing on this path makes a network call -- deliberately, because a fetch in
    front of `up` can hang before any container starts, and it would write refs the
    report is only supposed to read. So a detail reading "the code here is current"
    would be a claim about GitHub that nobody checked: the same fail-open as the
    unknown case, wearing the happy path's clothes. An operator who last fetched
    yesterday would read it and stop looking, which is exactly the 2026-10-08
    failure with the warning inverted.
    """
    status, headline, detail = code_version_verdict(_reading())
    assert status == "current"
    said = " ".join([headline, *detail])
    assert "as last fetched" in said, said
    assert "nothing is fetched here" in said, (
        f"the current verdict must disclaim the fetch it did not do, or it reads as a "
        f"check against GitHub: {said}"
    )


# =============================================================================
# THE COUNT AND THE WORK IT COUNTS
#
# 4d82caf added a fourth `dfx canister id` lookup outside the loop that counts
# them, and the operator's 2026-10-08 `status` printed the result: `x3` in the
# header, `asking 1/3`..`3/3` and then a fourth ask under it, and `read all 3
# canister ids` after four succeeded. One commit, four spellings of a count that
# lived in three places.
#
# These pin ANNOUNCED == PERFORMED, which is the invariant that broke, rather
# than the number 4 -- which would pass just as happily the next time a lookup is
# added without the header.
# =============================================================================


def _surface_map_output(capsys, answer, serving_port=5100) -> str:
    """Run the REAL printer with only the dfx call stubbed, and return what it said.

    Behavioral verification: the defect was in what reached the screen, so the
    assertion has to be on what reaches the screen. Asserting that
    canister_lookup_names() has four entries would have passed throughout the whole
    period the header said three.
    """
    original = swap_stack._ask_canister_id
    swap_stack._ask_canister_id = answer
    try:
        swap_stack._say_surface_map((), serving_port)
    finally:
        swap_stack._ask_canister_id = original
    return capsys.readouterr().out


def _all_found(name, _files):
    return f"bkyz2-{name[:5]}-cai", "found", ""


def test_the_announced_number_of_lookups_is_the_number_performed(capsys):
    """The exact defect, pinned as a relationship rather than as a literal."""
    out = _surface_map_output(capsys, _all_found)
    announced = re.search(r"`dfx canister id` x(\d+) in the", out)
    assert announced, f"the header no longer announces a count at all:\n{out}"
    asks = re.findall(r"asking (\d+)/(\d+) (\S+)", out)
    assert asks, f"no progress counter was printed:\n{out}"
    performed = len(asks)
    assert int(announced.group(1)) == performed, (
        f"the header announced {announced.group(1)} lookups and {performed} were performed. "
        f"an operator reads the header to budget the wait, and this is the line that lied "
        f"on 2026-10-08:\n{out}"
    )
    assert {int(total) for _i, total, _n in asks} == {performed}, (
        f"the counter's denominator disagrees with the number of asks: {asks}"
    )
    assert [int(i) for i, _t, _n in asks] == list(range(1, performed + 1)), (
        f"the counter does not run 1..N without a gap or a repeat: {asks}"
    )


def test_every_canister_the_map_renders_is_a_canister_that_was_asked_for():
    """Rule 8: the lookup list is DERIVED from the surface rows, not typed beside them.

    A hand-maintained second list is the defect with a delay on it -- a row added to
    CANISTER_SURFACES and not to the lookups would render as `id COULD NOT BE READ`
    forever, which looks like a replica problem and is not one.
    """
    looked_up = canister_lookup_names()
    rendered = {name for name, _serves_page, _what in CANISTER_SURFACES}
    assert rendered <= set(looked_up), (
        f"{rendered - set(looked_up)} is rendered in the map and never asked for"
    )
    assert CANDID_UI_CANISTER_NAME in looked_up, (
        "the Candid UI canister's id makes every CANDID link writable; not asking for it "
        "is the hardcoded-id defect returning"
    )
    assert len(looked_up) == len(rendered) + 1, (
        f"the lookup list is not the surface rows plus the Candid UI: {looked_up}"
    )
    assert looked_up[-1] == CANDID_UI_CANISTER_NAME, (
        "the UI goes last: if the replica is wedged, the three the operator asked about "
        "have already been attempted when the fourth times out"
    )


def test_a_partial_read_is_counted_and_does_not_look_like_a_complete_one(capsys):
    """Rule 3 (state the denominator) and rule 14 (did-nothing must not look like did-work).

    The old line printed `read all 3 canister ids` on a clean run and NOTHING on a
    dirty one, so a three-of-four read had no number anywhere -- the operator had to
    count trouble lines to work out what had succeeded.
    """
    complete = _surface_map_output(capsys, _all_found)
    assert re.search(r"read\s+4/4 ids read", complete), complete

    def ui_missing(name, files):
        if name == CANDID_UI_CANISTER_NAME:
            return "", "unreachable", "exited 1: Cannot find canister id"
        return _all_found(name, files)

    partial = _surface_map_output(capsys, ui_missing)
    assert re.search(r"PARTIAL\s+3/4 ids read", partial), (
        f"a three-of-four read must carry its own count and its own word:\n{partial}"
    )
    # SCOPED TO THE SUMMARY LINE, and the first version of this was not: it banned
    # "4/4" anywhere in the output, which the progress counter legitimately prints
    # as `asking 4/4 __Candid_UI`. Four asks DID happen; three succeeded. The
    # assertion is about what the summary claims, not about the digits appearing.
    summary = [line for line in partial.splitlines() if "ids read" in line]
    assert len(summary) == 1, f"expected exactly one summary line: {summary}"
    assert "4/4" not in summary[0], f"a partial read reported a complete count: {summary[0]}"
    assert "PARTIAL" not in complete, (
        f"a complete read was marked partial, which is the alarm that makes real ones "
        f"get ignored:\n{complete}"
    )


def _trouble_block(out: str, name: str) -> list[str]:
    """The COULD NOT READ line for `name`, PLUS its wrapped continuations.

    IT USED TO FILTER FOR ONE LINE carrying both the name and "COULD NOT READ",
    which worked only while the whole message was on that one line. It is not:
    the trouble text quotes docker's stderr verbatim, so on the 2026-10-09
    `status` run these four lines came out at 326 to 495 columns and the terminal
    broke each at an arbitrary column with no indent. swap_stack.py now wraps
    them under the label column, and the single-line filter then found the label
    and dropped every word after the first wrap -- so the assertion below failed
    on output that said exactly what it demanded, two lines further down.

    Reading the BLOCK is also the stronger claim, which is why this is not merely
    a repair (rule 2: its test changes to pin the stronger invariant). What the
    test cares about is that the cost is stated WITH the failure, where an
    operator reading that failure will see it -- not merely somewhere in 120
    lines of report. Searching the whole output would have passed for a sentence
    printed thirty lines away under a different heading.
    """
    lines = out.splitlines()
    for at, line in enumerate(lines):
        if name in line and "COULD NOT READ" in line:
            block = [line]
            for following in lines[at + 1:]:
                # A continuation is indented to the label column and is not itself
                # a new label. `  LABEL  text` has non-space before column 20.
                if following.startswith(" " * 20) and following.strip():
                    block.append(following)
                else:
                    break
            return block
    return []


def test_a_missing_candid_ui_says_what_it_costs(capsys):
    """Not "could not read" -- WHICH links stop being written, and why none is faked."""
    out = _surface_map_output(capsys, lambda name, files: (
        ("", "unreachable", "exited 1: Cannot find canister id")
        if name == CANDID_UI_CANISTER_NAME else _all_found(name, files)
    ))
    cost = _trouble_block(out, CANDID_UI_CANISTER_NAME)
    assert cost, f"the failed Candid UI lookup produced no trouble line:\n{out}"
    said = " ".join(cost)
    assert "no CANDID link is written" in said, (
        f"the operator is told a lookup failed without being told what stops working: {said}"
    )
    assert "wrong canister's interface" in said, (
        f"and without being told why a guessed id is not the safer option: {said}"
    )


# =============================================================================
# WHERE THE REPLICA KEEPS ITS STATE
#
# On 2026-10-08 my instruction to `down && up` destroyed three canisters,
# including the ICP ledger holding the desk's 998.9498 LICP. What was written
# afterwards was an ARGUMENT -- "any container created since 28de99c has the
# volume, so the exposure is now impossible to create" -- and rule 17 is about
# exactly that: a reason to believe is not a reading. These pin the reading.
# =============================================================================

_ON_VOLUME = f"volume {REPLICA_STATE_PATH}\nbind /repo\n"
_IN_LAYER = "bind /repo\nvolume /some/other/path\n"


def test_replica_state_classifies_every_case():
    assert replica_state_verdict("abc123", _ON_VOLUME)[0] == "on_volume"
    assert replica_state_verdict("abc123", _IN_LAYER)[0] == "not_durable"
    assert replica_state_verdict("", "")[0] == "absent"
    assert replica_state_verdict("abc123", "", "docker inspect exited 1")[0] == "unknown"


def test_a_bind_at_the_dfx_path_is_protected_not_an_alarm():
    """The hazard is "dies when the container is replaced", and a bind does not.

    docker-compose.icp.yml argues against a bind for a different reason -- 180M of
    replica state under the operator's checkout, where `git clean -xdf` would take
    the ledger -- and that is a layout objection, not a durability one. Reporting a
    bind as in_layer would be a false alarm, and rule 13's whole complaint about
    these warnings is that the false one is what makes the real one get ignored.
    """
    status, headline, _detail = replica_state_verdict("abc123", f"bind {REPLICA_STATE_PATH}\n")
    assert status == "on_volume", "a bind mount at the dfx path survives a recreate"
    assert "bind" in headline, f"and the operator should be told which it is: {headline}"


def test_a_tmpfs_at_the_dfx_path_is_not_protection():
    """RAM, not storage -- and the first version of this verdict called it safe.

    The protection test was "is anything mounted at that path", which a tmpfs
    satisfies. A tmpfs holding the ledger would not survive `docker restart`, let
    alone the recreate this whole check is about, so reporting it as on_volume is
    a WORSE hazard reported as safety. A mutation caught it; no amount of reading
    the line back did.
    """
    status, _headline, detail = replica_state_verdict("abc123", f"tmpfs {REPLICA_STATE_PATH}\n")
    assert status == "not_durable", (
        "a tmpfs is RAM; the ledger would not survive a restart, let alone a recreate"
    )
    assert "RAM rather than storage" in detail[0], (
        f"and the operator must be told WHICH kind of not-durable they have, because the "
        f"remedy differs: {detail[0]}"
    )


def test_state_that_cannot_survive_a_recreate_is_shouted_and_priced():
    """Not "unprotected" -- WHAT is lost, by what mechanism, and that `up` still exits 0."""
    status, headline, detail = replica_state_verdict("abc123", _IN_LAYER)
    assert status == "not_durable"
    assert "***" in headline, f"the one verdict that must not be skimmed past: {headline}"
    said = " ".join(detail)
    for owed in ("RECREATES", "ledger", "threshold_custody", "operator_admin", "SERVING"):
        assert owed in said, (
            f"the operator is told the state is unprotected without being told {owed!r} -- "
            f"which is the fact that makes it worth stopping for: {said}"
        )
    assert "OPERATOR'S CALL" in said, (
        "the remedy destroys canisters either way, so it is proposed and not performed "
        "(rule 16: live posture comes back)"
    )


def test_mounts_that_could_not_be_read_are_never_reported_safe():
    """FAIL CLOSED, and here a false all-clear is permission to run a destructive command."""
    status, headline, detail = replica_state_verdict("abc123", "", "docker inspect exited 1: no such object")
    assert status == "unknown"
    said = " ".join([headline, *detail]).lower()
    assert "nobody checked" in said, said
    assert "does not say the replica's state is safe" in said, (
        f"the disclaimer has to be explicit, not merely an absence: {said}"
    )
    # THE AFFIRMATIVE PHRASES ONLY, which the first version of this got wrong: it
    # banned the bare word "safe", and the unknown verdict correctly CONTAINS it,
    # in "this does NOT say the replica's state is safe". Banning a word rather
    # than a claim fails on the sentence that makes the claim honestly.
    for claim in ("is on a volume mount", "is on a bind mount", "does not take the canisters"):
        assert claim not in said, f"an unreadable inspect printed {claim!r}: {said}"
    assert "no such object" in said, "git's... docker's own sentence is what the operator acts on"


def test_an_absent_container_says_so_rather_than_printing_nothing():
    """Rule 14: "(none) is a result; a blank gap is ambiguous between zero and broken"."""
    status, headline, detail = replica_state_verdict("", "")
    assert status == "absent"
    said = " ".join([headline, *detail])
    assert "no replica container exists yet" in said
    assert "named volume" in said, (
        f"and that the one `up` is about to create WILL be protected, which is the "
        f"reassurance that stops this reading as a warning: {said}"
    )


def test_the_dfx_state_path_matches_the_compose_file_that_mounts_it():
    """Rule 8: two spellings of one path, and a drift here is silent in both directions.

    If REPLICA_STATE_PATH stopped matching the compose file's mount destination, a
    correctly-protected replica would report in_layer (a false alarm, which teaches
    the operator to ignore the line) or an unprotected one would report on_volume
    (permission to run the command that cost three canisters). The compose file is
    the authority; this asserts the reader still agrees with it.
    """
    compose_file = Path(swap_stack.__file__).parent / "docker-compose.icp.yml"
    body = compose_file.read_text(encoding="utf-8")
    mounts = re.findall(r'^\s*-\s*"([^"]+):([^":]+)"\s*$', body, re.MULTILINE)
    destinations = [dest for _source, dest in mounts]
    assert REPLICA_STATE_PATH in destinations, (
        f"{REPLICA_STATE_PATH} is not mounted anywhere in {compose_file.name}; its mount "
        f"destinations are {destinations}. Either the compose file moved the state and this "
        f"constant did not follow, or the volume was removed entirely"
    )


def test_the_steps_up_prints_are_numbered_consecutively():
    """Inserting a step renumbers the rest, and I got this wrong in the same commit.

    SOURCE-BASED ON PURPOSE, and it is the one place in this file where that is the
    honest choice: the numbers are literals in say() calls, running cmd_up() needs
    docker, and the claim under test -- "these literals count 1..N" -- has no
    behavior to exercise. The behavioral-verification principle is about not
    accepting "the SQL text contains X" as proof a GATE is enforced; there is no
    gate here, only a sequence of printed labels.
    """
    steps = re.findall(r'say\("  (\d+)\. ', inspect.getsource(swap_stack.cmd_up))
    assert steps, "cmd_up prints no numbered steps at all"
    assert [int(n) for n in steps] == list(range(1, len(steps) + 1)), (
        f"the steps `up` prints are not 1..{len(steps)} in order: {steps}. inserting a step "
        f"renumbers every one after it, and an operator reading '3' twice cannot tell which "
        f"check they are watching"
    )


def test_a_failed_lookup_is_unknown_and_not_absent():
    """A FAIL-OPEN I SHIPPED AND CAUGHT BY RUNNING IT, not by reading it back.

    `docker compose ps -q` returns no container id when it FAILS, exactly as it
    does when no container exists. The branches were ordered `if not container ->
    absent` before `if reason -> unknown`, so a lookup that never happened answered

        absent -- `up` will create one, and docker-compose.icp.yml mounts
        /root/.local/share/dfx from a named volume, so its canisters will survive

    for a replica that might be holding the ledger in its writable layer. That is
    the precise false all-clear the whole verdict exists to refuse, reached through
    the door of argument ordering.

    It was invisible in the seeded tests because every one of them passed a
    container id with a reason, which never exercises the overlap. It surfaced the
    first time the function ran against a real docker that could not answer.
    """
    status, headline, detail = replica_state_verdict("", "", "docker compose ps exited 1: no daemon")
    assert status == "unknown", (
        f"a failed lookup classified as {status} -- `absent` here reads as 'nothing is at "
        f"risk', which is permission to run a command that can destroy the ledger"
    )
    said = " ".join([headline, *detail])
    assert "no daemon" in said, f"docker's own sentence is what the operator acts on: {said}"
    assert "named volume, so its canisters will survive" not in said, (
        "the absent branch's reassurance must not be reachable without a reading"
    )


def test_no_verdict_prints_a_command_with_a_placeholder_in_it():
    """`docker inspect ?` reached the operator, and a command in output gets run.

    The id was a literal "?" passed to dodge the ordering bug above -- so the
    workaround and the defect were the same line. Rule 14's "pasted output has to
    be self-describing" cuts here: a command naming an id that does not exist is
    worse than no command, because the reader spends the attempt before doubting
    the line.
    """
    for container, reason in (("", "docker compose ps exited 1"), ("abc123", "docker inspect exited 1")):
        _status, headline, detail = replica_state_verdict(container, "", reason)
        for line in [headline, *detail]:
            assert "?`" not in line and "inspect ?" not in line, (
                f"a placeholder leaked into a command the operator is invited to run: {line}"
            )
        if not container:
            assert "docker inspect" not in " ".join(detail), (
                "with no container id resolved there is nothing to inspect, so the remedy "
                "must point at resolving it instead"
            )


def test_the_preflight_inspects_the_stack_up_will_act_on(monkeypatch, capsys):
    """`-f` overrides which compose files `up` uses, and the pre-flight must follow.

    It read the module-level COMPOSE_FILES while cmd_up acts on its `files`
    argument, so `swap_stack.py up -f something.yml` would have resolved the
    DEFAULT stack's replica, inspected THAT container's mounts, and printed the
    answer as a pre-flight for a rebuild of a different one. A reading of the
    wrong thing is worse than no reading: it carries the authority of having
    checked.
    """
    seen: list[tuple] = []

    class _Done:
        returncode, stdout, stderr = 1, "", "stubbed: nothing ran"

    def fake_compose(args, files, **_kwargs):
        seen.append((tuple(args), files))
        return _Done()

    monkeypatch.setattr(swap_stack, "compose", fake_compose)
    swap_stack._say_replica_state(("alpha.yml", "beta.yml"))
    capsys.readouterr()
    assert seen, "the pre-flight ran no compose command at all"
    args, files = seen[0]
    assert files == ("alpha.yml", "beta.yml"), (
        f"the pre-flight asked compose about {files} while `up` was given "
        f"('alpha.yml', 'beta.yml')"
    )
    assert args[:2] == ("ps", "-q"), (
        f"the container is resolved by asking compose, not by guessing a name: {args}"
    )


# =============================================================================
# CAN THE CONTAINER REACH THE CHAIN DAEMONS?
#
# `up` asked whether the replica answers and whether the page serves, and never
# asked this. On 2026-10-08 that cost the operator a day: BTC, GRC and LTC
# balances sat 34,301 seconds stale on /admin while ICP and SOL were 112 seconds
# fresh, every Confirm froze, and `up` printed SERVING and exited 0 through all
# of it. The cause was ufw dropping packets on the docker bridge plus two
# daemons bound loopback-only -- none of which is visible from the host, where
# `curl 127.0.0.1:18443` works perfectly.
# =============================================================================


def _chain_body(**reachable) -> dict:
    """A WHOLE /api/admin/chains body. `reachable=` per asset: True, False or None.

    BUILT THROUGH THE REAL chain_probe_envelope() RATHER THAN SPELLED OUT, and
    that is the fix this helper exists to carry rather than a tidy-up. It used to
    be `_chain_rows()` returning a bare list, with a docstring claiming
    "/api/admin/chains' shape" -- and the endpoint has never served a bare list.
    routes/admin.py wraps the rows in an envelope and static/admin.js reads
    `data.chains`, so every test in this section asserted on a shape no reader
    would ever be handed, and the one reader that mattered --
    `swap_stack.py`'s step 7 -- printed "COULD NOT ASK ... got dict" on the
    operator's working stack 2026-10-09.

    Four tests green on a check that had never once produced a real verdict. The
    helper is what made that possible: a fabricated body cannot disagree with the
    author's belief about the body. Going through the producer means the next
    change to that shape breaks these tests instead of hiding from them.

    The ROWS are still fabricated here, deliberately -- probe_chain() makes
    network calls, and the verdict logic under test is about what the rows SAY,
    not about getting them. The envelope is what was wrong and the envelope is
    what is now real. tests/test_web_surfaces.py drives the actual route end to
    end, which is the half no pure test can reach.
    """
    rows = [
        {"asset": asset, "probed": state is not None, "reachable": state,
         "network": "regtest" if state else None,
         "detail": "answered" if state else f"did not answer: stub for {asset}"}
        for asset, state in reachable.items()
    ]
    return chain_probe_envelope(rows, "2026-10-09T00:00:00+00:00", len(rows))


def test_the_probe_refuses_a_url_it_was_never_meant_to_open():
    """The guard that makes `noqa: S310` a claim rather than a promise.

    The comment at that call site said "the URL is never input" and was correct
    about both callers -- which is not the same thing as a constraint, and is
    exactly rule 2's "I could not find a caller is not there is no caller" wearing
    a lint code. A third caller passing a `file:` URL would have been read off
    local disk with the comment still reading true.

    MUTATION: delete the startswith check. This fails; nothing else in the suite
    does, because no existing caller violates it -- which is the point.
    """
    for refused in ("file:///etc/passwd", "http://evil.example/", "ftp://127.0.0.1:4943/"):
        with pytest.raises(ValueError, match="loopback only"):
            swap_stack.wait_for_http(refused, 0.1, announce=False)


def test_only_a_reachable_verdict_exits_zero():
    """`chains` is a health check, and three of its four verdicts are not success.

    Rule 13: "treat 'skipped' plus 'success' in the same output as a defect". A
    check that exits 0 because NOBODY ASKED is worse than no check, because
    something downstream reads the 0 and stops looking. `none_asked` means no
    adapter has a read-only probe and `unknown` means the endpoint could not be
    read; neither is an all-clear.
    """
    assert chain_exit_code("reachable") == 0
    for verdict in ("unreachable", "none_asked", "unknown"):
        assert chain_exit_code(verdict) == 1, f"{verdict} exited 0, which reads as an all-clear"


def test_every_verdict_the_reader_can_return_has_an_exit_code():
    """DERIVED FROM THE VERDICTS THEMSELVES, not from a list written twice.

    Drives chain_reachability_verdict() through each of its four outcomes and
    asks chain_exit_code() for every status it produced. A fifth verdict added
    without a code fails here rather than in front of the operator.
    """
    produced = {
        chain_reachability_verdict(None, "connection refused")[0],
        chain_reachability_verdict({"error": "boom"})[0],
        chain_reachability_verdict(_chain_body(ICP=None, SOL=None))[0],
        chain_reachability_verdict(_chain_body(BTC=True, XRP=True))[0],
        chain_reachability_verdict(_chain_body(BTC=False, XRP=True))[0],
    }
    assert produced == set(CHAIN_EXIT_CODES), (
        f"the verdicts the reader produces and the ones with exit codes differ: "
        f"produced-only {produced - set(CHAIN_EXIT_CODES)}, "
        f"coded-only {set(CHAIN_EXIT_CODES) - produced}"
    )
    for status in produced:
        assert chain_exit_code(status) in (0, 1), status


def test_an_unknown_verdict_raises_rather_than_defaulting_to_failure():
    """`.get(status, 1)` would hide a bug behind a plausible code forever.

    An unmapped verdict reading as failure LOOKS safe, which is why it would
    never be noticed -- the same "right for the wrong reason" shape the trouble
    branch of chain_reachability_verdict() was found to have.
    """
    with pytest.raises(KeyError, match="no exit code for chain verdict"):
        chain_exit_code("probably_fine")


def test_the_envelope_derives_its_own_attempt_count():
    """`probes_attempted` must come from the rows, not from a second count.

    MUTATION: return a fixed 0. An operator reading "0 of 6 could be probed"
    beside six answered rows cannot tell which number to believe, and rule 3's
    denominator is the whole point of the field.
    """
    body = chain_probe_envelope(
        [{"asset": "BTC", "probed": True}, {"asset": "ICP", "probed": False}], "t", 6
    )
    assert body["probes_attempted"] == 1, body
    assert body["adapters_configured"] == 6, body
    assert body[CHAIN_PROBE_ROWS_KEY] == body["chains"], (
        "the constant and the literal key have drifted, which is the whole defect"
    )


def test_the_reader_accepts_what_the_builder_builds():
    """The contract, in one line, from both ends.

    This is the assertion whose absence cost a day of UNKNOWN: the producer and
    the host-side consumer of /api/admin/chains were never once checked against
    each other.
    """
    rows = [{"asset": "BTC", "probed": True, "reachable": True}]
    assert chain_probe_rows(chain_probe_envelope(rows, "t", 1)) == rows


def test_a_bare_list_of_rows_is_refused_rather_than_tolerated():
    """Accepting both shapes is how the next reader gets it wrong too.

    Nothing emits a bare list. If this ever starts passing, the extractor has
    been loosened to accept a shape no route serves, and the question "which
    shape does it send?" becomes unanswerable from the reader again.
    """
    assert chain_probe_rows([{"asset": "BTC", "probed": True, "reachable": True}]) is None


def test_a_chain_with_no_probe_is_not_counted_as_a_failure():
    """ICP and SOL have no read-only probe BY DESIGN, and an alarm that includes
    them is the false alarm that teaches an operator to skim past the real one."""
    status, headline, detail = chain_reachability_verdict(
        _chain_body(BTC=True, XRP=True, ICP=None, SOL=None)
    )
    assert status == "reachable", f"{status}: {headline}"
    said = " ".join([headline, *detail])
    assert "ICP, SOL" in said and "by design" in said, (
        f"the unprobed chains must be named AND excused, or the operator reads the count as "
        f"a gap: {said}"
    )


def test_one_unreachable_chain_is_named_and_shouted():
    status, headline, detail = chain_reachability_verdict(
        _chain_body(BTC=False, GRC=False, LTC=False, XRP=True, ICP=None, SOL=None)
    )
    assert status == "unreachable"
    # STARTSWITH, NOT `in`. The marker is written at BOTH ends of the headline, so
    # `"***" in headline` stayed true when a mutation stripped the leading one --
    # the same weak-assertion shape that let a Candid-link mutation survive earlier
    # today. The first characters are what an operator's eye lands on.
    assert headline.startswith("***"), f"the one verdict that must not be skimmed: {headline}"
    assert "CANNOT REACH" in headline, (
        f"and it must say so in the words an operator scans for, not in prose: {headline}"
    )
    for asset in ("BTC", "GRC", "LTC"):
        assert asset in headline, f"{asset} is unreachable and not in the headline: {headline}"
    assert "XRP" not in headline, f"a chain that ANSWERED is named as unreachable: {headline}"
    said = " ".join(detail)
    assert "answered: XRP" in said, f"the working chains must be named too: {said}"
    for owed in ("NOT being watched", "refuses at create_swap", "60s"):
        assert owed in said, (
            f"the operator is told a chain is unreachable without being told {owed!r} -- which "
            f"is the part that makes it worth stopping for: {said}"
        )
    assert "asked from INSIDE the container" in said, (
        "an operator whose own curl works will dismiss this unless told where it was asked from"
    )


def test_an_endpoint_that_could_not_be_read_never_reads_as_reachable():
    """FAIL CLOSED. The third time in this file, and here a false all-clear means
    the operator stops looking while deposits go uncredited."""
    for rows, trouble in (
        # A VALID BODY WITH A TROUBLE REASON. This case is the one that isolates
        # the `trouble` branch: every other input here is ALSO caught by the
        # shape check below it, so a mutation disabling `if trouble:` survived
        # until this line existed -- the verdict was right for the wrong reason.
        (_chain_body(BTC=True, XRP=True), "the page is not serving, so its probe cannot be asked"),
        (None, "connection refused"),
        ({"error": "boom"}, ""),
        ("<html>500</html>", ""),
        ([{"not": "a chain row"}], ""),
    ):
        status, headline, detail = chain_reachability_verdict(rows, trouble)
        assert status == "unknown", f"{rows!r}/{trouble!r} classified as {status}"
        said = " ".join([headline, *detail]).lower()
        assert "nobody asked" in said or "says nothing about reachability" in said, said
        assert "all " not in said, f"an unreadable answer claimed a count: {said}"


def test_the_empty_case_says_nothing_was_established():
    """Rule 14: "(none) is a result". No probeable chain is not an all-clear."""
    status, headline, _detail = chain_reachability_verdict(_chain_body(ICP=None, SOL=None))
    assert status == "none_asked", f"{status}: {headline}"
    assert status != "reachable", "zero chains probed must never report as all reachable"


def test_up_actually_asks_before_it_prints_SERVING():
    """The wiring, which the pure tests above cannot reach.

    SOURCE-BASED, and justified the same way the step-numbering test is: cmd_up
    needs docker to run, and the claim under test is "this function calls that
    one", which has no behavior to exercise without a stack. A mutation deleting
    the call from cmd_up left every verdict test green, because a verdict nobody
    invokes is indistinguishable from one that always agrees.
    """
    source = inspect.getsource(swap_stack.cmd_up)
    assert "_say_chain_reachability(" in source, (
        "cmd_up no longer asks whether the container can reach the chain daemons. That check "
        "exists because `up` printed SERVING and exit 0 for 9.5 hours while three chains were "
        "unreachable and every Confirm froze"
    )
    assert "chain daemons" in source, (
        "the step is called but no longer announces itself, so an operator watching the probe "
        "cannot tell what it is waiting on (rule 14)"
    )


@pytest.mark.parametrize("failure", [
    # BOTH OF _ask_canister_id's OWN FAILURE PATHS, and the first version of this
    # test only drove the timeout. A mutation restoring "" on the OSError branch
    # SURVIVED -- both were broken, one was pinned. The two are different causes
    # (a wedged replica versus docker not being on PATH) and nothing about the
    # code makes them share a fate, so nothing about the test may assume it.
    subprocess.TimeoutExpired("dfx", 10),
    OSError("no such file or directory: docker"),
], ids=["docker timed out", "docker could not be run"])
def test_a_lookup_that_could_not_run_yields_no_id_rather_than_an_empty_one(monkeypatch, failure):
    """"" AND None BOTH MEAN "NO ID" AND ONLY ONE OF THEM RENDERS AS ONE.

    _ask_canister_id() returned "" on its two own failure paths -- a docker
    timeout and an OSError -- while canister_lookup_verdict(), whose vocabulary
    it claims to speak, returns None. canister_surface_lines() asks
    `if ident is None`, so "" sailed past it and the map printed:

        PAGE   operator_admin       http://.localhost:4943/
        CANDID threshold_custody    http://127.0.0.1:4943/?canisterId=bd3sg-...&id=

    Three malformed URLs as working links, from the one function whose docstring
    says an unread id "resolves and shows the operator somebody else's canister".
    Reachable whenever docker is wedged: the per-lookup timeout is 10s.

    Found by pyright reporting the declared return type against the actual one,
    2026-10-09 -- underneath 53 FALSE import errors in the operator's editor.
    """
    def never_answers(*_args, **_kwargs):
        raise failure

    monkeypatch.setattr(swap_stack, "compose", never_answers)
    looked = swap_stack.canister_ids(())

    for name, ident in looked.ids.items():
        assert ident is None, (
            f"{name} came back as {ident!r} from a lookup that never ran. None is what the map "
            f"renders as 'id COULD NOT BE READ'; anything else it treats as an id and builds a "
            f"URL from"
        )
    assert looked.ui_id == "", f"the Candid UI id must be '' and not None here: {looked.ui_id!r}"
    assert looked.read == 0, looked.read

    # AND THE MAP ITSELF, because the type is only half the claim (rule: verify by
    # behavioral outcome). No row may carry a URL when no id was read.
    rendered = surface_map(5100, looked.ids, absent=0, ui_canister_id=looked.ui_id)
    for line in rendered:
        assert "http://.localhost" not in line, f"a URL was built from an empty id: {line.strip()}"
        assert not line.rstrip().endswith("&id="), f"a Candid link with no target: {line.strip()}"
