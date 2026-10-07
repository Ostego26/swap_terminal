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

import socket
from pathlib import Path

import swap_stack
from swap_terminal.stack_authority import (
    DOWN_VERDICTS,
    NEVER_STOPPED,
    container_id,
    container_label,
    down_verdict,
    hex_port,
    listening_inodes,
    pids_owning_inodes,
    port_is_free,
    proc_net_tcp_tables,
    process_name,
    readiness_verdict,
    stray_verdict,
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


def test_a_refused_connection_is_NOT_READY_even_though_the_port_is_bound():
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
    """
    ready, detail = readiness_verdict(ConnectionRefusedError(111, "Connection refused"))
    assert ready is False
    assert "nothing is listening" in detail


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
    monkeypatch.setattr(swap_stack, "report_listeners", list)
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
