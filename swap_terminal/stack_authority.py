"""What belongs to this swap terminal, what does not, and what must never be stopped.

Role: submodule (decisions only -- every function here is pure or takes its
      filesystem root as an argument, so a test can run it against a fake /proc)
Reads: nothing on its own. Callers pass it text read from /proc, a directory to
       scan, and the compose files on disk.
Writes: nothing. No process is signaled from this file and no command is run.
Can move funds: no. There is no chain call, no RPC and no send path here.
Live-safe: yes to import and yes to call. It decides; swap_stack.py acts.

WHY THIS FILE EXISTS, which is a defect measured on 2026-10-06 rather than a tidy
idea. The operator asked that one command stop everything this terminal uses. It
could not, because THREE REAPERS EXIST AND NONE KNOWS ABOUT THE OTHER TWO:

    swap_terminal/supervisor.py      the three workers, by pid file, stop PROVEN
    swap_terminal_desktop.py         the gunicorn IT started, by process group
    docker compose                   the containers, per compose file

Each is correct about its own half and blind to the rest. What that produced on
the live host the same day, in one session:

  - six workers polling one database (three host, three container orphans), two
    of them payout workers, saved only by which process held credentials
  - a gunicorn holding 127.0.0.1:5101 that NOTHING names -- not a pid file, not a
    compose file, not the supervisor, not the desktop launcher. It was found by
    `ss -ltnp` because a port bind failed, which is the same way every orphan in
    this project has ever been found: an operator noticing something odd.

That is rule 13's "every spawn needs a reaper" failing one level up: the reapers
exist, and no one of them can answer "is anything still running?"

WHAT THIS FILE DELIBERATELY REFUSES TO DECIDE. It will not mark a chain daemon
stoppable, ever, under any flag. bitcoind, litecoind and gridcoinresearchd are the
operator's own processes, they hold the wallets this desk spends from, and
CLAUDE.md's live-safety rules say not to stop live services or remove armed state.
A `down` that took the wallets with it would also be unrecoverable by a `up`: this
file has no passphrase and could not unlock them again. They are reported as
LEFT RUNNING with that reason attached, because rule 14's "make 'did nothing' look
different from 'did work'" cuts both ways -- a daemon left alone on purpose must
not look like one the scan missed.

IDENTIFYING A SERVER BY THE PORT IT HOLDS, NOT BY ITS COMMAND LINE. Rule 13 says
prefer a pid file to a `pgrep -f` pattern, because "a pattern matches what the
command line happens to look like today; renaming a script silently orphans it."
An orphan has no pid file by definition, so the pattern is the only thing left --
and for a SERVER there is something better than both: the port. A process holding
127.0.0.1:5102 is the thing answering that URL whatever it calls itself, which is
the actual question an operator has. So listeners are found through /proc/net/tcp
and matched to pids by socket inode.

That identity is strong enough to report and NOT strong enough to kill, which is
the distinction `stray_verdict()` draws. Something else on this machine may
legitimately hold a port in our range; killing it because it answered on 5101
would be this project doing to a stranger exactly what its own orphans did to it.
So a stray is only ever stoppable when its command line names THIS repository, and
even then only when the caller passes the flag that says so.
"""

from __future__ import annotations

import socket
from pathlib import Path

#: The ports this terminal's HTTP surfaces are reachable on, and what each means.
#:
#: NOT DERIVED FROM Config, and that is the decision on this line. Config tells you
#: where a server WOULD bind if you started one now; this set has to cover where a
#: server that is ALREADY RUNNING bound, possibly hours ago under a different
#: SWAP_TERMINAL_PORT. The 5101 gunicorn that prompted this file is exactly that
#: case: nothing in the current environment named 5101, and a process was on it.
#:
#: Measured on the live host 2026-10-06: 5100 was the container's published port,
#: 5101 an orphaned host gunicorn, 5102 the app.py started to replace it. 5000 is
#: config.py's default and 4943 is the ICP replica's.
STACK_PORTS = {
    4943: "ICP replica (dfx) -- the local ledger and the custody canister",
    5000: "config.py's default SWAP_TERMINAL_PORT",
    5100: "the usual published port of the `web` compose service",
    5101: "a host gunicorn was found here 2026-10-06 owned by nothing",
    5102: "where app.py was moved when 5101 was already taken",
}

#: Processes this file will NEVER report as stoppable. See the module docstring.
#:
#: Matched against the process NAME (argv[0]'s basename) rather than the whole
#: command line, because a command line can contain a daemon's name for innocent
#: reasons -- `tail -f bitcoind.log` must not be treated as bitcoind, and a grep
#: for the pattern would be.
#: dfx's /api/v2/status answers this when the replica is up.
_HTTP_OK = 200

#: What docker prints for a container id, and what this file prints to match it.
_SHORT_ID_LENGTH = 12

NEVER_STOPPED = {
    "bitcoind": "holds the BTC wallet this desk spends from; stopping it is the operator's",
    "litecoind": "holds the LTC wallet this desk spends from; stopping it is the operator's",
    "gridcoinresearchd": "holds the GRC wallet AND its passphrase state; a stop cannot be undone from here",
    "solana-test-validator": "a chain daemon, same rule as the three above",
    "rippled": "a chain daemon, same rule as the three above",
}


#: Fields a /proc/net/tcp row must have before field 9 (the socket inode) can be
#: read. Named rather than inline because the row is a kernel format, not an
#: arbitrary 10: sl, local, rem, st, tx:rx, tr:when, retrnsmt, uid, timeout, inode.
_PROC_NET_TCP_FIELDS = 10


def hex_port(hex_address: str) -> int:
    """The port out of a /proc/net/tcp local_address field (`0100007F:13EE` -> 5102).

    Its own function because it is the one piece of that format worth testing
    directly: the port is the SECOND field, hex, uppercase, and the address half
    is little-endian bytes which this deliberately does not decode. Nothing here
    needs to know which interface -- a bind to 127.0.0.1 and a bind to 0.0.0.0 are
    both "something is on that port", and the difference is a separate question
    that kill_switch.refuse_off_box() already owns.
    """
    return int(hex_address.rsplit(":", 1)[1], 16)


def listening_inodes(proc_net_tcp: str, ports: set[int]) -> dict[int, int]:
    """Socket inodes LISTENing on any of `ports`, as {port: inode}.

    Pure: it takes the text, not the path, so a test seeds a real /proc/net/tcp
    body and asserts on the answer without a listening socket anywhere.

    st == 0A is TCP_LISTEN. Filtering on it matters rather than being tidy: an
    ESTABLISHED connection TO one of these ports (a browser's open tab, a curl)
    has the same local port in its row, and counting those would report the
    operator's own browser as a server holding the port.
    """
    found: dict[int, int] = {}
    for line in proc_net_tcp.splitlines()[1:]:
        fields = line.split()
        if len(fields) < _PROC_NET_TCP_FIELDS or fields[3] != "0A":
            continue
        port = hex_port(fields[1])
        if port in ports:
            # The LAST listener wins only in the sense that a port has one; two
            # rows for one port means tcp and tcp6 both carry it, same socket.
            #
            # THAT CASE BECAME REACHABLE ON 2026-10-07 and was not before. This
            # comment described a dual-stack socket while the only caller read
            # /proc/net/tcp alone, so tcp6 rows never arrived here -- which is
            # what let an IPv6-only listener hold one of our ports invisibly.
            # proc_net_tcp_tables() now supplies both tables, and the sentence
            # above is a measurement rather than a plan.
            found[port] = int(fields[9])
    return found


def pids_owning_inodes(inodes: set[int], proc_root: Path) -> dict[int, int]:
    """{inode: pid} for every socket inode owned by a process under `proc_root`.

    `proc_root` is an argument so a test can build a fake /proc out of directories
    and symlinks, which is what tests/test_stack_authority.py does. Reading the
    real /proc is the caller's choice, not this function's.

    A pid whose fd directory cannot be read is SKIPPED rather than raising: /proc
    is a live filesystem and a process exiting between the listdir and the readlink
    is ordinary, not an error. This is the one broad-ish catch in this file and it
    is narrow (OSError) and its caller can tell: an inode that no pid claims simply
    does not appear in the result, and report_listeners() says "owner unknown"
    rather than silently dropping the port.
    """
    wanted = {f"socket:[{inode}]": inode for inode in inodes}
    owners: dict[int, int] = {}
    for entry in proc_root.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            for fd in (entry / "fd").iterdir():
                target = str(fd.readlink())
                if target in wanted:
                    owners[wanted[target]] = int(entry.name)
        except OSError:
            continue
    return owners


def process_name(cmdline: str) -> str:
    """The basename of argv[0] out of a /proc/<pid>/cmdline, NUL-separated.

    Written out because NEVER_STOPPED is matched against this and not against the
    whole line -- see that constant's comment for why `tail -f bitcoind.log` must
    not read as bitcoind.
    """
    argv0 = cmdline.split("\0", 1)[0]
    return Path(argv0).name if argv0 else ""


def stray_verdict(cmdline: str, repo_root: Path) -> tuple[str, str]:
    """Classify one discovered listener: (verdict, reason).

    THE THREE VERDICTS, and the line between them is what makes this file safe to
    act on:

      refused    its name is in NEVER_STOPPED. A chain daemon. No flag reaches it.
      ours       its command line names `repo_root`, so it is something this
                 project started and a stop is defensible.
      foreign    something else on this machine holds one of our ports. REPORTED
                 and never stopped, whatever flag is passed -- killing it because
                 it answered on 5101 is what this project's own orphans did to it.

    The repo_root test is a substring of the command line and that is deliberate
    rather than lazy: a worker is `python /path/to/repo/swap_terminal/workers/...`
    and a gunicorn is `gunicorn ... wsgi:app` run with that cwd, so the path
    appears in argv for the first and may not for the second. A gunicorn that does
    NOT carry the path therefore reads `foreign` and is left alone, which is the
    correct answer for an ambiguous case: it is reported loudly and the operator
    decides.
    """
    name = process_name(cmdline)
    if name in NEVER_STOPPED:
        return "refused", NEVER_STOPPED[name]
    if str(repo_root) in cmdline:
        return "ours", f"its command line names {repo_root}"
    return "foreign", (
        "nothing in its command line names this repository, so this file will not stop it -- "
        "it may be an unrelated program that legitimately holds this port"
    )


def container_id(cgroup: str) -> str | None:
    """The container id out of a /proc/<pid>/cgroup body, or None for a host process.

    WHY THIS EXISTS, and it is a defect in stray_verdict() measured within an hour
    of shipping it. On the live host 2026-10-06, the listener on 127.0.0.1:5101 was:

        /usr/local/bin/python3 /usr/local/bin/gunicorn -c /app/gunicorn.conf.py wsgi:app

    stray_verdict() called it FOREIGN -- "nothing in its command line names this
    repository, so this file will not stop it -- it may be an unrelated program that
    legitimately holds this port." The ACTION was right and the REASON was wrong,
    which rule 16 calls a bug of the same seriousness as wrong code: that is this
    project's own gunicorn, inside a container, and `/app` is where
    docker/web.Dockerfile puts this repository. A host path could never appear in
    its argv, so the repo_root substring test cannot ever pass for it.

    It is invisible to `docker compose ps` because it belongs to no compose project
    this stack names -- a leftover from a `docker run` or an earlier project name.
    So the operator needs a DIFFERENT lever than a pid: `docker stop <id>`, and the
    id is the thing worth printing.

    The cgroup line is read rather than the command line because it is the kernel's
    own answer to "is this pid in a container", where a path like /app is a guess
    that a host directory named /app would satisfy by accident.

    Both layouts are handled: cgroup v2 writes
    `0::/system.slice/docker-<64hex>.scope` and v1 writes `.../docker/<64hex>`.
    Returns the first twelve characters, which is what docker itself prints.
    """
    for line in cgroup.splitlines():
        for marker in ("docker-", "docker/", "containerd-", "libpod-"):
            if marker in line:
                tail = line.rsplit(marker, 1)[1]
                ident = tail.split(".")[0].strip("/")
                if len(ident) >= _SHORT_ID_LENGTH and all(c in "0123456789abcdef" for c in ident):
                    return ident[:_SHORT_ID_LENGTH]
    return None


def readiness_verdict(outcome: object) -> tuple[bool, str]:
    """Interpret one probe of a service's own endpoint: (ready, what it means).

    WHY A PROBE AT ALL, when swap_stack.py already binds the port. Measured
    2026-10-07: `up` reported

        :4943 BOUND  ICP replica (dfx) -- the local ledger and the custody canister

    and the very next command got `Connection refused (os error 111)` from
    http://icp-replica:4943. Both are true. A PUBLISHED container port is bound by
    docker-proxy the instant the container is created, before the process inside has
    opened anything -- so "bound" proves docker did its part and says nothing about
    the service.

    That is rule 13's "verify the artifact, not the deploy" catching my own check:
    binding was already the improvement over trusting compose's exit code, and it is
    still one layer short of the question an operator has, which is "can I use it".

    Takes the OUTCOME of a probe -- an HTTP status int, or the exception raised --
    rather than performing one, so the interpretation is testable without a socket.
    """
    if isinstance(outcome, int):
        # dfx's /api/v2/status answers 200. Anything else came from something that
        # is listening, which is what this is asked to decide, so it is reported
        # with its code rather than collapsed into "not ready".
        if outcome == _HTTP_OK:
            return True, f"answered {outcome}"
        return True, f"answered {outcome} -- listening, but not the 200 dfx's status gives"
    if isinstance(outcome, ConnectionRefusedError):
        return False, "connection refused -- the port is bound and nothing is listening behind it yet"
    if isinstance(outcome, TimeoutError):
        return False, "timed out -- something accepted the connection and did not answer"
    if isinstance(outcome, OSError):
        return False, f"{type(outcome).__name__}: {outcome}"
    return False, f"unrecognized probe outcome {outcome!r} -- treated as NOT ready rather than guessed"


def container_label(inspect_output: str) -> str:
    """A human label out of `docker inspect --format '{{.Name}} {{.Config.Image}}'`.

    WHY THE REPORT NEEDS THIS. On the live host 2026-10-06 the status output named a
    container id and nothing else, so the operator ran `docker ps` to find out what
    it was -- and the answer was worth having: the container was `st-ui`, with NO
    PUBLISHED PORTS, on an image that is a bare SHA with no tag. That says three
    things the id alone did not: it is on host networking (which is how a container
    with no port mapping holds the host's 127.0.0.1:5101, and why
    docker-compose.web.hostnet.yml is the overlay it came from), it is this project's
    UI, and its image has since been rebuilt so nothing names that build any more.

    Rule 14: "Pasted output has to be self-describing a day later, because it usually
    is read a day later." A bare id is not.

    Docker prefixes a container name with a slash in its JSON; it is stripped here
    because every other place docker prints a name does not have it, and a reader
    comparing this line to `docker ps` should not have to notice the difference.
    """
    parts = inspect_output.split()
    if not parts:
        return ""
    name = parts[0].lstrip("/")
    image = parts[1] if len(parts) > 1 else ""
    return f"{name} (image {image})" if image else name


def port_is_free(port: int, host: str = "127.0.0.1") -> bool:
    """Can this port be bound right now? The ABSENCE assertion for a stop.

    Rule 13: "a stop that cannot prove it worked is not a stop. Follow it with a
    check that the process is gone, and make the absence the assertion -- not the
    exit code of the kill." `docker compose down` exiting 0 says compose thinks it
    removed its containers; binding the port says nothing is serving there.

    SO_REUSEADDR is deliberately NOT set. With it, this would succeed against a
    socket in TIME_WAIT and report free a port that is not yet reusable by the
    thing we are about to start -- which would turn the proof into the false
    reassurance it exists to replace.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        try:
            probe.bind((host, port))
        except OSError:
            return False
    return True


# =============================================================================
# THE TWO DECISIONS A `down` MAKES, extracted here on 2026-10-07 because both
# were inlined in swap_stack.py's cmd_down() and one of them was WRONG THERE for
# as long as it existed -- in the block whose whole purpose is to be the proof.
# Rule 10: the thing that decides is the smallest, most testable piece at the
# bottom, and "the only way to test it is to run the whole thing" is how this
# defect survived. These are callable with seeded inputs.
# =============================================================================

#: The families of /proc/net TCP tables a listener scan must read. BOTH, and
#: reading only the first produced a report that contradicted itself -- see
#: proc_net_tcp_tables() below.
PROC_NET_TCP_TABLES = ("tcp", "tcp6")


def proc_net_tcp_tables(proc_net: Path) -> tuple[str, list[str]]:
    """Concatenated /proc/net/tcp + /proc/net/tcp6 text, and which tables were MISSING.

    `proc_net` is a directory argument rather than a hardcoded /proc/net so a
    test seeds both tables as ordinary files and asserts on the answer with no
    socket open anywhere -- the same reason listening_inodes() takes text.

    WHY BOTH, measured 2026-10-07 on the operator's host. `swap_stack.py down`
    printed, in one block:

        :4943 STILL BOUND  <- bind attempted, not inferred
        (none)             nothing is LISTENing on any of 4943, 5000, 5100, ...

    Two honest measurements and no way to reconcile them, because the scan read
    /proc/net/tcp alone. Docker publishes a port on 0.0.0.0 AND ::, and a socket
    bound to :: with IPV6_V6ONLY off answers IPv4 connections while appearing
    only in /proc/net/tcp6. So an IPv6 listener on one of our ports was invisible
    to the scan and failed the bind in port_is_free(): "(none)" meant "none this
    scan can see", which rule 17 forbids writing in a measurement's voice.

    listening_inodes() had anticipated it -- its comment reads "two rows for one
    port means tcp and tcp6 both carry it, same socket" -- about a case its only
    caller could never produce. hex_port() rsplits on the last colon, so a
    32-hex-character tcp6 local_address parses through the identical path.

    A MISSING TABLE IS RETURNED, NOT RAISED, and the two are not equivalent. A
    kernel booted with ipv6.disable=1 has no tcp6 at all, and there the IPv4 scan
    is complete; a missing tcp means the report cannot be made. The caller says
    which out loud (rule 14) instead of printing "(none)" over a scan it only
    half ran.
    """
    body = ""
    missing: list[str] = []
    for table in PROC_NET_TCP_TABLES:
        try:
            body += (proc_net / table).read_text()
        except OSError:
            missing.append(table)
    return body, missing


#: What a `down` established. Ordered by how much it claims, least first.
DOWN_VERDICTS = ("not_down", "stopped_not_proven", "down")


def down_verdict(bound_ports: list[int] | tuple[int, ...], owned_listeners: int) -> str:
    """What a stop actually established. One of DOWN_VERDICTS.

    `bound_ports` are the stack ports that could NOT be bound after the stop;
    `owned_listeners` is how many LISTEN sockets the scan attributed to this
    project. Both are measurements the caller has already taken -- this only
    decides what they add up to, which is exactly the step that was wrong.

    THE DEFECT, measured 2026-10-07 on the operator's host. cmd_down() branched
    on the listener count alone and then printed

        summary  every process this file owns is gone, proven by bind.

    immediately below its own two lines reading `STILL BOUND`. Two failed binds
    were measured, printed, and not looked at by the sentence claiming to have
    proven something by binding. Rule 13's "skipped" beside "success" in one
    block; rule 14's "did nothing" wearing the face of "did work".

    The three answers, and the middle one is the one that did not exist:

      not_down             a listener this project owns survived the stop. The
                           lever is a pid and `status` names it.
      stopped_not_proven   nothing of ours is LISTENing and some port would not
                           bind. Ambiguous ON PURPOSE: port_is_free() omits
                           SO_REUSEADDR, so a socket in TIME_WAIT -- the ordinary
                           residue of a server that just served and exited --
                           reads as bound and is benign; and a listener owned by
                           a user whose /proc this one cannot read lands in
                           "owner unknown" rather than here. The caller prints
                           both readings and how to separate them.
      down                 nothing LISTENing and every port bindable. The only
                           answer that may say "proven".

    A LISTENER WINS OVER A BOUND PORT, and not merely because it is worse: a
    surviving listener is a port that is bound, so the two findings are the same
    finding seen twice, and reporting the weaker one would bury the lever.
    """
    if owned_listeners:
        return "not_down"
    if bound_ports:
        return "stopped_not_proven"
    return "down"
