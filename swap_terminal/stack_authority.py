"""What belongs to this swap terminal, what does not, and what must never be stopped.

Role: submodule (decisions only -- every function here is pure or takes its
      filesystem root as an argument, so a test can run it against a fake /proc)
Reads: nothing on its own. Callers pass it text read from /proc, a directory to
       scan, the compose files on disk, the port a probe found answering, and the
       canister ids `dfx canister id` returned.
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
from collections.abc import Mapping
from dataclasses import dataclass
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
    # 5101 IS THE `web` SERVICE'S PORT. It used to read "a host gunicorn was found
    # here 2026-10-06 owned by nothing", which was a true finding and the wrong
    # label: it described an INCIDENT at this port rather than what the port IS,
    # so once the containerized deployment was serving correctly the report read
    #
    #     OURS (CONTAINER)  :5101 pid=85833  a host gunicorn was found here
    #                       2026-10-06 owned by nothing
    #
    # on the operator's own healthy stack (2026-10-08) -- a correct container
    # annotated with somebody else's orphan. The incident is kept as history
    # because it is why this port is scanned at all; it is no longer the name.
    5101: "the `web` service, where gunicorn serves the ATM and /admin "
          "(a stray host gunicorn was once found here, 2026-10-06)",
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


#: The verdicts report_listeners() can return, and what each one licenses.
#:
#: NAMED AS A SET so print_listeners()'s label map can be asserted complete rather
#: than discovered incomplete by a KeyError in front of the operator -- that map
#: indexes on the verdict with [], so a verdict with no label raises mid-report and
#: takes the finding down with it.
LISTENER_VERDICTS = (
    "ours",
    "ours (container)",
    "container",
    "foreign",
    "refused",
    "owner unknown",
)


def container_verdict(container: str | None, stack_container_ids: frozenset[str]) -> tuple[str, str]:
    """Is a containerized listener one of THIS stack's containers? (verdict, reason).

    PURE. The caller asks docker which containers the stack has and hands the ids
    in, exactly as serving_verdict() takes probe outcomes rather than opening
    sockets -- so the decision is testable with no daemon, no socket and no docker
    on PATH.

    ---------------------------------------------------------------------------
    THE DEFECT THIS EXISTS TO FIX, measured on the operator's host 2026-10-08
    ---------------------------------------------------------------------------

    `swap_stack.py status` printed this about the container that was serving the
    operator's page:

        IN A CONTAINER    :5101 pid=85833
                          in container 40bfc3701f79 = swap-web (image
                          swap-terminal/web:local), which `docker compose ps`
                          above does not list -- so it belongs to no compose
                          project this stack names. Stop it with
                          `docker stop 40bfc3701f79`

    `docker compose ps` DID list it. It was four lines further up the same
    report, named swap-web, service `web`, Up 13 hours. The tool told the
    operator to stop the web container that was serving their UI, and gave a
    reason that its own output contradicted.

    HOW IT GOT THERE, because the mechanism is the lesson. On 2026-10-06 the
    listener on 5101 genuinely WAS a container no compose project named -- a
    leftover from a `docker run` or an earlier project name -- and
    container_id()'s docstring still records that measurement correctly. The
    finding was then written into the report as a SENTENCE rather than as a
    check: every containerized listener got told it belonged to no compose
    project, because on the day it was written every containerized listener did.

    That is a measurement hardening into a fact, which is what rule 17 forbids
    and what rule 3 means by stating the denominator. The claim "`docker compose
    ps` does not list it" is cheap to actually TEST -- `docker compose ps -q` is
    the same question the sentence was asserting an answer to -- so it is tested
    here and the sentence is now produced by the branch it describes.

    WHY THIS IS WORSE THAN A WRONG COMMENT. `docker stop <id>` on a serving web
    container is a live action against the thing the operator asked to run, handed
    to them with a reason that reads as measured. Rule 16's line is "reversible by
    a deploy versus reversible only by a trade", and this is the third category it
    does not name: advice that is wrong in the direction of destroying working
    state. A refusal or a verdict that misnames its condition is the one thing it
    must not do.

    ---------------------------------------------------------------------------

    @param container  the 12-character id from container_id(), or None for a host
                      process.
    @param stack_container_ids  every container id `docker compose ps -q` reported
                      for this stack, full length. Empty means either the stack has
                      no containers or docker could not be asked -- see below.
    """
    if container is None:
        raise ValueError("container_verdict is for containerized listeners; container was None")

    # PREFIX MATCH, because the two sides are different lengths by construction:
    # container_id() returns the first twelve characters (what docker prints) and
    # `docker compose ps -q` returns the full 64. Comparing them with == would
    # never match and would send every container down the "not ours" branch --
    # which is the bug being fixed, reintroduced by the fix.
    for full in stack_container_ids:
        if full.startswith(container):
            # NO STOP COMMAND APPEARS IN THIS STRING, and that is deliberate
            # rather than a phrasing preference. The first version ended "`...
            # down` is the lever, not `docker stop`" -- which names the right
            # lever and still puts a `docker stop` in front of an operator who
            # is skimming a report for something to paste. A message must not
            # contain a command it does not want run; the test asserts the
            # absence of the substring for exactly that reason, and the way to
            # satisfy it is to not write one.
            return "ours (container)", (
                f"container {container} IS one this stack's compose files manage -- it is in "
                "the `docker compose ps` listing above. Nothing to do about it here: "
                "`swap_stack.py down` is what stops this stack's containers"
            )

    if not stack_container_ids:
        # DOCKER COULD NOT BE ASKED, OR THE STACK HAS NO CONTAINERS, and these are
        # not the same thing -- but neither licenses the "belongs to no compose
        # project" claim, because that claim requires a listing to be absent FROM.
        # Saying so is the fail-closed answer: the operator is told what is not
        # known rather than handed a stop command built on an unasked question.
        return "container", (
            f"container {container} holds this port, and this report could not establish whether "
            "any compose project manages it -- `docker compose ps -q` returned nothing, which is "
            "either a stack with no containers or a docker that could not be reached. "
            f"`docker inspect {container}` names it. NO stop is suggested, because "
            "'not in a listing' cannot be concluded from a listing that was never obtained"
        )

    return "container", (
        f"container {container} is NOT among the {len(stack_container_ids)} container(s) "
        "`docker compose ps` lists for this stack, so it belongs to no compose project this "
        "stack names -- a leftover from a `docker run` or an earlier project name is the usual "
        f"cause. The lever is `docker stop {container}`, after `docker inspect {container}` "
        "names it"
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
        # TWO READINGS AND THIS SENTENCE USED TO ASSERT ONLY ONE OF THEM, WHICH
        # MADE IT FALSE HALF THE TIME. It read "the port is bound and nothing is
        # listening behind it yet", which is correct for a PUBLISHED container port
        # -- docker-proxy binds it at container creation, so a refusal means the
        # proxy is there and the process inside has not opened anything.
        #
        # It is wrong for a port on the HOST's own loopback, which is what
        # docker-compose.web.hostnet.yml produces: under `network_mode: host`
        # there is no docker-proxy, so a refusal means nothing is bound AT ALL.
        # Caught 2026-10-07 when the web probe printed that sentence for :5101
        # under the hostnet overlay -- rule 16's wrong comment, except this one
        # prints on a screen the operator is using to decide what to fix.
        #
        # Neither reading is derivable from the refusal itself, so both are named.
        return False, (
            "connection refused -- either nothing is bound here at all, or a published container "
            "port is bound by docker-proxy and the process inside has not opened it yet. "
            "`docker compose ps` distinguishes them"
        )
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


#: The stack ports a CUSTOMER-FACING page could be on, in the order to try them.
#:
#: FOUR CANDIDATES BECAUSE THE OVERLAY DECIDES, AND `up` MUST NOT NEED TO KNOW
#: WHICH. docker-compose.web.yml publishes 127.0.0.1:5100 -> 5000;
#: docker-compose.web.hostnet.yml uses host networking and binds
#: SWAP_TERMINAL_PORT, which defaults to 5101 there; config.py's own default is
#: 5000 and 5102 is where app.py was moved when 5101 was taken. A prober that
#: hardcoded one of them would report NOT SERVING for a stack that was serving
#: perfectly well one overlay over.
#:
#: 4943 IS DELIBERATELY ABSENT. It is the ICP replica, which answers /api/v2/status
#: and not /, so probing it here would either report the page as live on the
#: replica's port or report a 404 as a dead page. It has its own probe.
WEB_PORT_CANDIDATES = (5101, 5100, 5000, 5102)


def serving_verdict(answers: dict[int, object]) -> tuple[int, str]:
    """(port, detail) for the first candidate that answered, or (0, why none did).

    PURE, so the decision "is the page serving" is testable without a socket --
    the caller does the probing and hands the outcomes in, exactly as
    listening_inodes() takes text rather than a path.

    WHY THIS IS A DECISION AND NOT A LOOP IN THE REPORT. `up` printed READY for
    the replica and said nothing about the web service on 2026-10-07, and the
    operator's next command got `Couldn't connect to server` on :5101. A dead page
    and a live one printed identically, which is rule 13's "skipped plus success
    in the same output is a defect in the output". Making the verdict a function
    is what lets a test assert that an all-refused probe does NOT read as serving.
    """
    for port in WEB_PORT_CANDIDATES:
        outcome = answers.get(port)
        if outcome is None:
            continue
        ready, detail = readiness_verdict(outcome)
        if ready:
            return port, detail
    if not answers:
        return 0, "no port was probed, so whether the page is serving is NOT ESTABLISHED"
    tried = ", ".join(
        f":{port} {readiness_verdict(answers[port])[1]}"
        for port in WEB_PORT_CANDIDATES
        if port in answers
    )
    return 0, f"nothing answered on any candidate port -- {tried}"


# =============================================================================
# WHICH CODE PRINTED THIS
#
# MEASURED FROM THE OPERATOR'S OWN PASTE, 2026-10-08, and it is the second half
# of the Candid-UI defect rather than a separate one.
#
# They ran `up` and pasted sixty lines back. Three of those lines read
#
#     CANDID threshold_custody  http://127.0.0.1:4943/?canisterId=be2us-64aaa-aaaaa-qaabq-cai&id=...
#
# naming a canister id that 4d82caf had deleted and pushed eleven minutes
# earlier -- and that their OWN dfx output, in the same terminal, had already
# contradicted. The report was wrong, the fix existed, and nothing in sixty
# lines of deliberately self-describing output said which commit had produced
# them. The operator had no way to tell a stale run from a current one, and
# neither did I: I had to infer it from a string that could only come from the
# old constant.
#
# THIS IS RULE 13 AT THE REPORT RATHER THAN AT THE DEPLOY. "When a deploy
# depends on new code actually running, verify the artifact, not the deploy."
# swap_stack.py runs on the HOST, outside both containers, so a `git pull` alone
# changes what it prints -- there is no image to rebuild and nothing to restart.
# That makes staleness cheap to fix and completely invisible, which is the worst
# combination: an operator who pulls and one who does not get output that looks
# identical.
#
# And rule 14: "Echo the parameters that decide the answer... Pasted output has
# to be self-describing a day later, because it usually is read a day later."
# The commit is the parameter that decides every other line.
#
# WHAT THIS DELIBERATELY DOES NOT DO: fetch. `up` is about to start containers
# and a network call that can hang in front of that is a worse failure than the
# one being fixed; a fetch also WRITES refs, which a report must not. So the
# comparison is against origin as the checkout last saw it, and every line that
# reports a match SAYS SO rather than letting "current" be read as "current with
# GitHub". Making it a real check would mean fetching, and that belongs to a
# command the operator runs on purpose.
#
# FAIL CLOSED, the same shape as candid_url(): when the comparison cannot be
# made, the verdict is `unknown` and says nobody checked. It never degrades to
# `current`, because a confident wrong all-clear is exactly what cost the last
# session -- and a report that silently stops checking is how an operator learns
# to ignore the line.
# =============================================================================

#: Verdicts that must be REPEATED at the end of a long run, not just in the banner.
#:
#: Only the two where the output can be WRONG relative to code that already exists.
#: `modified` and `unknown` print in the banner and stop there: a modified tree is
#: usually the operator editing on purpose, and `unknown` is this container's normal
#: state (its clone carries no origin ref for the working branch), so repeating
#: either would put a line nobody acts on at the bottom of every run. Rule 12's note
#: about a ratchet that fails on ordinary work applies to warnings too -- one that
#: fires every time is one the reader stops seeing, and it would take the two that
#: matter down with it.
VERSION_STATUSES_WORTH_REPEATING = frozenset({"stale", "diverged"})


@dataclass(frozen=True)
class GitReading:
    """What a caller read from git about the checkout swap_stack.py is running from.

    ONE OBJECT RATHER THAN SEVEN ARGUMENTS, and ruff said so (PLR0913/PLR0917)
    before the shape was obvious. It is the better design anyway: the caller takes
    one reading and hands it over, so a test seeds a reading instead of remembering
    the order of seven positional strings and ints.

    `modified` counts TRACKED files only -- see code_version_verdict().
    `behind`/`ahead` are against `upstream` AS THE CHECKOUT LAST FETCHED IT.
    `reason` carries WHY a field is empty, so `unknown` can say what stopped it
    rather than just that something did.
    """

    head: str = ""
    branch: str = ""
    modified: int = 0
    behind: int = 0
    ahead: int = 0
    upstream: str = ""
    reason: str = ""


def version_status(reading: GitReading) -> str:
    """Classify a GitReading into one of six statuses. Pure, and the whole decision.

    SEPARATE FROM THE WORDING ON PURPOSE. A mutation that breaks the classification
    and a mutation that breaks a sentence are different defects, and folding them
    into one function means a test of either passes on the other. This half is six
    lines and can be asserted on directly; code_version_verdict() below only chooses
    which paragraph to print for the answer this gives.

      stale     behind, with nothing local. The loudest: output below may describe
                a defect that is already fixed on origin.
      diverged  behind AND ahead. Same warning, plus a merge to come.
      ahead     local commits not pushed, nothing to pull. Not a correctness problem
                for the report, so it is stated and not shouted.
      modified  tracked files differ from HEAD, so the output matches NO commit.
      current   matches the last-seen origin, clean tree.
      unknown   the comparison could not be made. NEVER collapses into current.

    The order is a precedence, not a sequence of independent tests: a dirty tree on
    a stale branch is still stale, and the dirty count rides in the louder verdict's
    detail rather than replacing it.
    """
    if not reading.head or not reading.upstream:
        return "unknown"
    if reading.behind:
        return "diverged" if reading.ahead else "stale"
    if reading.modified:
        return "modified"
    return "ahead" if reading.ahead else "current"


def code_version_verdict(reading: GitReading) -> tuple[str, str, list[str]]:
    """Which commit produced this report, and whether it is the current one. Pure.

    Returns `(status, headline, detail)` ready for swap_stack.say(). Reads nothing
    and runs nothing (rule 10): the decision "may this output be trusted as current"
    is testable with a seeded GitReading, where the same logic inside the print loop
    would need a git repository in a known state to exercise at all.

    `modified` counts TRACKED files only. Untracked ones are excluded on purpose:
    `runtime/`, `*.db-wal` and a scratch script would make every run report a
    modified tree, and a warning that fires on every run is one the reader stops
    seeing.

    Nothing here fetches, so `current` means "matches what you last saw of origin"
    and the headline says that in those words rather than letting a reader take it
    for a check against GitHub.
    """
    status = version_status(reading)
    dirty = f"{reading.modified} tracked file{'' if reading.modified == 1 else 's'} MODIFIED"
    where = f"{reading.head} ({reading.branch})" if reading.branch else reading.head
    behind_n = f"{reading.behind} COMMIT{'' if reading.behind == 1 else 'S'}"
    also_dirty = [f"note: {dirty} on top of that."] if reading.modified else []
    # `git pull origin main` reads better to paste than `git pull origin/main`, and
    # the operator pastes it. Only the FIRST slash splits: a branch may contain more.
    pull = f"git pull {reading.upstream.replace('/', ' ', 1)}" if "/" in reading.upstream else "git pull"
    stale_body = [
        "anything below may describe a defect that is ALREADY FIXED on origin. read it",
        "as a record of old code, not as a reading of the system.",
    ]

    if status == "unknown" and not reading.head:
        headline = f"NOT READ: {reading.reason or 'git could not be run here'}"
        detail = [
            "this output names no commit, so a day from now nobody can tell which code",
            "produced it -- including whoever pasted it. that is the whole defect this",
            "line exists to prevent, and it is not fixed by the line failing quietly.",
        ]
    elif status == "unknown":
        headline = where
        detail = [
            f"NOT COMPARED to any origin ref: "
            f"{reading.reason or 'this checkout has no upstream for ' + (reading.branch or 'HEAD')}",
            "so this does NOT say the code is current. it says nobody checked.",
        ] + ([f"and {dirty}, so it matches no commit either."] if reading.modified else [])
    elif status == "diverged":
        headline = f"*** THIS OUTPUT IS FROM CODE {behind_n} BEHIND {reading.upstream}. ***"
        detail = [
            f"running {where}.",
            f"it also has {reading.ahead} local commit{'' if reading.ahead == 1 else 's'} "
            f"{reading.upstream} does not.",
            *stale_body,
            # THE COMMAND ALONE ON ITS LINE. The operator copies it off the screen,
            # and a remedy with half a sentence trailing it gets copied with the
            # sentence -- which is how `warbot.sh stop  # only if asked` once became
            # a pasted command with a comment on it.
            f"remedy: {pull}",
            "which will be a MERGE, not a fast-forward, because of those local commits.",
            "then re-run this command.",
            *also_dirty,
        ]
    elif status == "stale":
        headline = f"*** THIS OUTPUT IS FROM CODE {behind_n} BEHIND {reading.upstream}. ***"
        detail = [
            f"running {where}.",
            f"{reading.upstream} has {reading.behind} newer "
            f"commit{'' if reading.behind == 1 else 's'}, fetched and not merged.",
            *stale_body,
            f"remedy: {pull}",
            "then re-run this command. NOTHING NEEDS REBUILDING -- swap_stack.py runs on",
            "the host, so a pull alone changes what it prints.",
            *also_dirty,
        ]
    elif status == "modified":
        headline = f"{where} + {dirty}"
        detail = [
            "so this output matches NO commit, and the id above does not describe it.",
            "untracked files are not counted -- only changes to files git is tracking.",
        ]
    elif status == "ahead":
        headline = (f"{where}, {reading.ahead} commit{'' if reading.ahead == 1 else 's'} "
                    f"ahead of {reading.upstream}, tree clean")
        detail = ["nothing to pull. the code here is newer than the origin ref last fetched."]
    else:
        headline = f"{where}, tree clean, == {reading.upstream} as last fetched"
        detail = [
            "nothing is fetched here, so that is a match against what you last saw of",
            "origin, not a check against GitHub.",
        ]
    return status, headline, detail


# =============================================================================
# THE SURFACE MAP: one place that answers "where is everything".
#
# Operator, 2026-10-08: "so we have 3 canisters now. 3 different hyperlinks.
# where's the main landing page for the atm screen?"
#
# THAT QUESTION HAD AN ANSWER AND NOTHING IN THIS SYSTEM PRINTED IT. `up` said
# SERVING and gave one URL; `status` listed containers, listeners and workers and
# gave none. Between the Flask app, the replica and three canisters the operator
# was holding four-plus URLs in their head, and the one they most needed -- the
# landing page -- was the one that had just MOVED: `/atm` became `/` on
# 2026-10-07 (routes/atm.py::start()'s docstring records the instruction), and
# routes/ui.index() and templates/index.html went with it. So the URL in anyone's
# memory was the stale one.
#
# This is rule 14 applied to the question rather than to a wait: "Pasted output
# has to be self-describing a day later, because it usually is read a day later."
# A report that proves the stack is up and does not say what is now reachable
# makes the operator go and find out, which is the round trip rule 20 says not to
# charge them.
#
# WHY IT IS A PURE FUNCTION HERE AND NOT A PRINT LOOP IN swap_stack.py (rule 10).
# The map contains exactly one decision and it is a dangerous one: WHETHER A URL
# MAY BE WRITTEN DOWN AT ALL. A line reading `http://127.0.0.1:5101/` beside a
# port nothing answered on is worse than printing nothing -- it is the report
# asserting that something serves, which is the same class of defect as
# container_verdict()'s old sentence telling the operator to `docker stop` the
# container serving their page. Here it is a decision with seeded inputs and a
# test that asserts no URL appears when nothing answered.
# =============================================================================

#: The name dfx files the Candid UI canister under in canister_ids.json.
#:
#: THIS REPLACED A HARDCODED ID, AND THE COMMENT ABOVE THAT ID WAS FLATLY WRONG.
#: It read:
#:
#:     The Candid UI canister dfx deploys beside every local project, and it is
#:     the SAME id on every fresh replica -- unlike the project's own canisters,
#:     which are issued per replica and must be asked for.
#:
#: with CANDID_UI_CANISTER_ID = "be2us-64aaa-aaaaa-qaabq-cai" under it. Refuted by
#: the operator's own redeploy, 2026-10-08, which printed its canister creations in
#: order:
#:
#:     1  minter's wallet canister     bnz7o-iuaaa-aaaaa-qaaaa-cai
#:     2  icp_ledger_canister          bkyz2-fmaaa-aaaaa-qaaaq-cai
#:     3  the UI canister              bd3sg-teaaa-aaaaa-qaaba-cai
#:     4  default's wallet canister    be2us-64aaa-aaaaa-qaabq-cai
#:     5  threshold_custody            br5f7-7uaaa-aaaaa-qaaca-cai
#:     6  operator_admin               bw4dl-smaaa-aaaaa-qaacq-cai
#:
#: A local replica hands out ids from a fixed sequence in CREATION ORDER, so which
#: one the Candid UI gets depends on how many canisters were made before it. The
#: previous deployment created a wallet first and the UI landed fourth, which is
#: where "be2us" came from; this one created the UI third. So for a whole session
#: every CANDID row in the surface map pointed at `be2us` -- by then the DEFAULT
#: IDENTITY'S WALLET CANISTER -- and the links loaded the wrong canister's
#: interface while looking entirely correct.
#:
#: The id is now asked for exactly like the other three, and the link is simply
#: NOT PRINTED when it cannot be read. That is the rule canister_ids()'s own
#: docstring already stated and this constant was the exception to: "a canister id
#: is replica-issued environment state... an id written into this repository is a
#: link to a deployment that no longer exists -- or worse, resolves and shows the
#: operator somebody else's canister." It did exactly the "or worse".
CANDID_UI_CANISTER_NAME = "__Candid_UI"

# =============================================================================
# WHERE THE REPLICA KEEPS ITS STATE, ASKED BEFORE `up` CAN COST IT
#
# ON 2026-10-08 MY INSTRUCTION DESTROYED THREE CANISTERS ON THE OPERATOR'S HOST.
# I told them to `down && up` to pick up a stylesheet without checking what `up`
# does to the replica. Their container predated 28de99c (which added
# `icp-replica-data:/root/.local/share/dfx`) by about two hours, so its dfx state
# lived in the container's WRITABLE LAYER. `up` rebuilt both images; compose
# recreates a container whose image has changed; the layer went with it. The ICP
# ledger holding the desk's 998.9498 LICP, threshold_custody and operator_admin
# were gone, and `up` printed SERVING and exited 0.
#
# WHAT WAS WRITTEN AFTERWARDS WAS AN ARGUMENT, NOT A CHECK. f3a42fe's commit
# message says "on any container created since 28de99c the state is on a volume
# and survives, so the remaining exposure is a container older than that commit --
# which is now impossible to create." Every clause of that is plausible and none
# of it is a reading. Rule 17: "a reason to believe something is not the same as
# having checked it, and the two must never be written in the same voice." Rule
# 13 says the same thing one step later: "verify the artifact, not the deploy...
# check the pid that owns the lock is one you started."
#
# So this asks the container. `docker inspect` lists what is actually mounted
# where, and the question "is the dfx state on a volume" has a yes or no answer
# that costs one call. An operator about to run `up` is told which they have
# BEFORE the images rebuild, which is the only moment the answer can change
# anything.
#
# FAIL CLOSED, the third time in this file and for the third reason: here a false
# "you are safe" is an instruction to proceed with a command that can destroy a
# ledger. `unknown` says the check did not happen.
# =============================================================================

#: Where dfx keeps the local replica's state inside the container, and the ONE
#: place this file spells it.
#:
#: It must match docker-compose.icp.yml's `icp-replica-data:/root/.local/share/dfx`
#: exactly -- a second spelling that drifts would report the state unprotected
#: while it is fine, or the reverse. The compose file is the authority; this is the
#: reader, and replica_state_verdict() is written so a mismatch surfaces as
#: `not_durable` (loud, checkable) rather than as a quiet pass.
REPLICA_STATE_PATH = "/root/.local/share/dfx"


def replica_state_verdict(
    container: str, mounts: str, reason: str = "",
) -> tuple[str, str, list[str]]:
    """Is the replica's dfx state on a volume, or in a container that `up` can replace?

    Pure. Takes the container id (empty if none exists) and the text of
    `docker inspect --format '{{range .Mounts}}{{.Type}} {{.Destination}}...'`.

      absent       no replica container yet, so `up` will create one WITH the
                   volume. Nothing is at risk, and the line says so rather than
                   printing nothing (rule 14: "(none) is a result").
      on_volume    REPLICA_STATE_PATH is a volume or bind mount. Survives a recreate.
      not_durable  a container exists and nothing that outlives it is mounted
                   there. The 2026-10-08 shape, and the only verdict that shouts.
      unknown      the mounts could not be read. NEVER reads as on_volume.

    A BIND COUNTS AS PROTECTED and is not treated as the hazard, because the hazard
    is specifically "this data dies when the container is replaced" -- a bind
    outlives the container as surely as a volume does. docker-compose.icp.yml
    argues against a bind for a different reason (180M of replica state under the
    operator's checkout, where `git clean -xdf` would take the ledger), and that is
    a layout objection, not a durability one. A false alarm here is not harmless:
    rule 13's whole complaint about these warnings is that the false one is what
    makes the real one get ignored.

    A TMPFS DOES NOT, AND THE FIRST VERSION OF THIS SAID IT DID. The test for
    protection was "is anything mounted at that path", which a tmpfs satisfies --
    and a tmpfs is RAM, so the ledger would not survive `docker restart`, let alone
    a recreate. That is a WORSE version of the hazard being warned about, reported
    as safety. It was caught by a mutation rather than by reading it back: docker's
    mount types are volume, bind, tmpfs and npipe, and only the first two are
    storage.

    The verdict is named for what it decides -- will this outlive the container --
    rather than for the one cause it was written about. `not_durable` covers both
    "no mount at all" and "a mount that is RAM", and the detail says which.
    """
    #: The docker mount types that outlive the container they are attached to.
    #: volume and bind are storage; tmpfs is RAM and npipe is a Windows pipe.
    durable = ("volume", "bind")
    at_path = [line.split() for line in mounts.splitlines()
               if line.split()[1:2] == [REPLICA_STATE_PATH]]
    protected = [fields for fields in at_path if fields[0] in durable]
    if reason:
        return (
            "unknown",
            f"COULD NOT READ the replica's mounts: {reason}",
            ["so this does NOT say the replica's state is safe -- it says nobody checked.",
             *([f"`docker inspect {container}` names them; the one that matters is "
                f"{REPLICA_STATE_PATH}."] if container else
               ["the replica container could not even be resolved, so there is no id to "
                "inspect. `docker compose ps icp-replica` is the next thing to look at."])],
        )
    if not container:
        return (
            "absent",
            "no replica container exists yet",
            [f"`up` will create one, and docker-compose.icp.yml mounts {REPLICA_STATE_PATH}",
             "from a named volume, so its canisters will survive a later recreate."],
        )
    if protected:
        kind = protected[0][0]
        return (
            "on_volume",
            f"replica state is on a {kind} mount at {REPLICA_STATE_PATH}",
            ["so a rebuilt image recreating this container does NOT take the canisters",
             "with it -- checked on this container just now, not assumed from its age."],
        )
    return (
        "not_durable",
        "*** THE REPLICA'S dfx STATE WILL NOT SURVIVE A RECREATE. ***",
        [(f"{REPLICA_STATE_PATH} is a {at_path[0][0]} mount in container {container}, and that "
          "is RAM rather than storage." if at_path else
          f"nothing is mounted at {REPLICA_STATE_PATH} in container {container}, so the state "
          "is in its writable layer."),
         "`up` rebuilds both images, and compose RECREATES a container whose image",
         "changed -- which deletes that layer. the ICP ledger, threshold_custody and",
         "operator_admin would go with it, and `up` would still print SERVING.",
         "this is what happened on 2026-10-08 and it cost the desk's minted LICP.",
         "remedy: `docker compose -f docker-compose.yml -f docker-compose.icp.yml up -d",
         "--force-recreate icp-replica` ONCE, deliberately, accepting the loss now and",
         "redeploying -- rather than discovering it after an unrelated rebuild. THE",
         "OPERATOR'S CALL: it destroys canisters either way, and only they know",
         "whether anything is mid-flight."],
    )


def canister_lookup_names() -> tuple[str, ...]:
    """Every name `dfx canister id` is asked for, in the order asked. DERIVED, not listed.

    ONE PLACE, BECAUSE TWO WERE ALREADY WRONG (rule 8). 4d82caf added the
    __Candid_UI lookup outside the loop that counts them, and the operator's
    2026-10-08 `status` printed the result:

        canister ids      `dfx canister id` x3 in the `icp-replica` service, up to
                          8.3µfn (10.0s) each
                          asking 1/3 operator_admin
                          asking 2/3 threshold_custody
                          asking 3/3 icp_ledger_canister
                          asking __Candid_UI (the Candid UI canister itself)
        read              all 3 canister ids

    Four lookups announced as three, a counter that reached 3/3 and then kept
    going, and a summary claiming three reads after four succeeded. Rule 3: "state
    the denominator: a count without what it was counted out of has caused real
    errors here more than once." Rule 14: "State what the number means, next to the
    number" -- an operator reading `x3 ... up to 10.0s each` budgets 30 seconds for
    a step that can take 40.

    None of it was wrong by one edit. It was wrong because the count was written
    down in three places and the list of lookups in two, so adding a lookup in one
    of them left the other four spellings describing the old shape. Deriving the
    tuple here makes the next addition arrive in the header, the counter and the
    summary at once.

    The Candid UI goes LAST on purpose: the three project canisters are what the
    operator asked for, and the UI is what makes their CANDID links writable. If
    the replica is wedged, the three that matter have already been attempted when
    the fourth times out.
    """
    return (*(name for name, _serves_page, _what in CANISTER_SURFACES), CANDID_UI_CANISTER_NAME)


@dataclass(frozen=True)
class CanisterLookups:
    """What one pass of `dfx canister id` read. Produced by swap_stack.canister_ids().

    A READING OBJECT RATHER THAN A GROWING TUPLE, the same shape as GitReading and
    for the same reason: this started as `(found, trouble)`, became a 4-tuple when
    the Candid UI id was added, and `asked`/`read` would have made it six. Every
    caller unpacking six positional values in order is a transposition waiting to
    happen, and two of them are ints that mean different things.

    `ids` holds ONLY the three surface canisters, because that is what surface_map()
    renders; `ui_id` is kept apart because it is not a row, it is what makes the
    other rows' CANDID links writable.

    `asked` and `read` are counted rather than derived from len(ids), which is the
    defect this type was introduced with: len(ids) is 3 no matter how many lookups
    ran.
    """

    ids: dict[str, str | None]
    trouble: list[str]
    absent: int
    ui_id: str
    asked: int
    read: int


#: The replica's own status endpoint, and the ONE place it is spelled.
#:
#: MOVED HERE FROM swap_stack.py ON 2026-10-08, where it was `_REPLICA_STATUS_URL`
#: and was about to be spelled a second time by the surface map -- which is rule 8
#: exactly: "Two copies of one rule is not redundancy, it is a bug with a delay on
#: it." swap_stack.py imports it and still owns the WAIT BUDGETS, because how long
#: to wait for a cold `dfx start` is that file's business and the address is not.
#:
#: 127.0.0.1 and not `icp-replica`: this is the url a probe on the HOST uses, which
#: is what both callers are. The in-container url is ICP_DFX_NETWORK_URL and
#: chains/icp.py owns it.
REPLICA_STATUS_URL = "http://127.0.0.1:4943/api/v2/status"

#: The port the replica, the Candid UI and every canister page share.
REPLICA_PORT = 4943

#: Every HTTP surface the Flask app in the `swap-web` container serves, as
#: (kind, path, what it is and which file declares it).
#:
#: MEASURED FROM swap_terminal/routes/ ON 2026-10-08 by reading the decorators, not
#: from memory: atm.py has `@bp.get("/")` and `@bp.post("/")`, ui.py has
#: `/swap-lookup` and `/swap/<swap_id>`, grc_login.py has
#: `/swap/<swap_id>/address-proof`, admin.py has `/admin` plus the three
#: `/api/admin/*` reads, kill_switch.py has CONTROLS_PATH = "/admin/controls" on
#: both GET and POST, and rates/quotes/swaps/health have the five `/api/*` rows.
#:
#: THE KIND COLUMN IS THE WHOLE POINT OF THE TABLE and not decoration. The
#: operator's question was "where is the landing page", and a list that renders a
#: JSON endpoint and a customer page in the same voice does not answer it:
#:
#:   PAGE    a human opens it in a browser and a screen is rendered
#:   ADMIN   a page too, and the operator's rather than a customer's
#:   JSON    a machine reads it. Opening one in a browser shows JSON, which is not
#:           a broken page -- saying so here is cheaper than the support question
#:
#: `/swap/<id>/fragment` IS DELIBERATELY ABSENT. It exists (ui.py:112) and it is an
#: HTMX partial -- a fragment of the swap page, not a surface anybody opens. A map
#: that lists it invites somebody to open it and conclude the page is broken.
WEB_SURFACES = (
    ("PAGE", "/", "THE LANDING PAGE -- the ATM flow; GET=step 1, POST=advance (routes/atm.py)"),
    ("PAGE", "/swap/<id>", "one swap's live state; the URL a customer keeps (routes/ui.py)"),
    ("PAGE", "/swap/<id>/address-proof", "the GRC address-proof step (routes/grc_login.py)"),
    ("PAGE", "/swap-lookup", "a returning customer's way back in (routes/ui.py)"),
    ("ADMIN", "/admin", "the operator dashboard (routes/admin.py)"),
    ("ADMIN", "/admin/controls", "the kill switch -- the only operator route taking a POST (routes/kill_switch.py)"),
    ("JSON", "/api/rates", "the rate table (routes/rates.py)"),
    ("JSON", "/api/quotes", "POST: price one pair (routes/quotes.py)"),
    ("JSON", "/api/swaps", "POST: create a swap (routes/swaps.py)"),
    ("JSON", "/api/swaps/<id>", "one swap, as the page reads it (routes/swaps.py)"),
    ("JSON", "/api/health", "liveness (routes/health.py)"),
    ("JSON", "/api/admin/overview", "the dashboard's own data (routes/admin.py)"),
    ("JSON", "/api/admin/chains", "per-chain probes (routes/admin.py)"),
    ("JSON", "/api/admin/peg", "the peg reading (routes/admin.py)"),
)

#: The three canisters this stack deploys, as (name, serves_a_page, what it is).
#:
#: NAMES FROM icp/dfx.json, which is the authority for what exists; the IDS ARE NOT
#: HERE AND MUST NOT BE, because a fresh replica issues a different id for every
#: canister and a hardcoded one would be a link to somebody else's deployment.
#: canister_surface_lines() takes them as an argument for that reason and prints
#: COULD NOT BE READ rather than a guess (rule 17).
#:
#: TWO OF THE THREE WILL NEVER HAVE A PAGE, and saying so is the point of the
#: boolean. icp_ledger_canister is DFINITY's released ledger wasm (dfx.json pins
#: the ledger-suite-icp-2025-08-29 release) and threshold_custody is key
#: derivation; neither has a frontend to grow one in. An operator who has been
#: handed "three canisters, three hyperlinks" needs to know that two of those
#: links are developer surfaces, or they will keep looking for the page.
#:
#: operator_admin DOES serve its own page, and that is measured rather than
#: assumed: on 2026-10-08 http://<its-id>.localhost:4943/ answered 200 text/html,
#: 6227 bytes.
#:
#: operator_admin is FIRST because it is the one with a page.
CANISTER_SURFACES = (
    ("operator_admin", True,
     "the operator console, served BY the canister itself -- 200 text/html, 6227 bytes, measured 2026-10-08"),
    ("threshold_custody", False,
     "key derivation. NO page, and there will never be one -- Candid is its only surface"),
    ("icp_ledger_canister", False,
     "DFINITY's released ledger wasm. NO page, and there will never be one -- Candid is its only surface"),
)

#: Column the continuation text of a map line starts at, matching print_listeners().
_MAP_CONTINUATION = " " * 20

#: Gap between the URL-or-path column and the description column.
#:
#: The column itself is MEASURED FROM THE ROWS rather than fixed, because the two
#: modes differ by the whole length of a URL: `http://127.0.0.1:5101/api/admin/
#: overview` is 42 characters and the same row as a path is 19. A constant wide
#: enough for the first leaves 23 columns of whitespace in the second, which is
#: how an aligned table turns into two unrelated columns on an 80-wide terminal.
_MAP_TARGET_GAP = 2


def candid_url(canister_id: str, ui_canister_id: str) -> str:
    """The Candid UI link for one canister, or "" when the UI's id is unknown.

    Candid UI is a canister itself, so the link is a query against IT with the
    target's id as a parameter -- not a path under the target. Getting that
    backwards produces a URL that loads and shows the wrong canister's interface,
    which is why this is a function rather than an f-string at three call sites.

    `ui_canister_id` IS AN ARGUMENT AND NOT A CONSTANT as of 2026-10-08, and the
    constant it replaced was wrong on the operator's live replica for a whole
    session -- see CANDID_UI_CANISTER_NAME for the creation-order evidence.

    AN EMPTY STRING IN GIVES AN EMPTY STRING OUT, which the caller prints as a
    refusal rather than a link. Fail closed: a Candid URL built on an id nobody
    read is the "resolves and shows the operator somebody else's canister" case,
    and the one thing worse than no link is a confident wrong one.
    """
    if not ui_canister_id:
        return ""
    return f"http://127.0.0.1:{REPLICA_PORT}/?canisterId={ui_canister_id}&id={canister_id}"


def web_surface_lines(serving_port: int | None) -> list[str]:
    """The web-app half of the surface map. Pure.

    @param serving_port  the port serving_verdict() established ANSWERED, or None.
                         0 IS ACCEPTED AND MEANS NONE, because that is literally
                         what serving_verdict() returns for "nothing answered" --
                         a caller forwarding its first return value must not have
                         to remember to translate, and `if serving_port:` treating
                         0 as a port would be the one bug this function exists to
                         not have.

    THE DECISION: a URL is written only when a port answered. With none, the same
    rows print as PATHS, under a line saying so. Those are not interchangeable --
    `http://127.0.0.1:5101/admin` is a claim that something is there, and an
    operator pastes it and gets a connection refused they then have to diagnose.
    A bare `/admin` claims nothing and still answers "where is the page".
    """
    base = f"http://127.0.0.1:{serving_port}" if serving_port else ""
    if base:
        lines = [
            f"  web app           the Flask app in the `swap-web` container, answering on :{serving_port}",
            f"{_MAP_CONTINUATION}-- so every row below is a LINK, probed just now rather than assumed",
        ]
    else:
        candidates = ", ".join(str(port) for port in WEB_PORT_CANDIDATES)
        lines = [
            f"  web app           NO URL: nothing answered on {candidates}, so the rows below are",
            f"{_MAP_CONTINUATION}PATHS AND NOT LINKS -- the app is not serving them right now",
        ]
    width = max(len(base + path) for _kind, path, _what in WEB_SURFACES) + _MAP_TARGET_GAP
    lines += [
        f"    {kind:<6} {base + path:<{width}} {what}"
        for kind, path, what in WEB_SURFACES
    ]
    return lines


#: What dfx says when the replica has no such canister.
#:
#: VERBATIM FROM dfx, measured on the operator's host 2026-10-08:
#:
#:     Error: Cannot find canister id. Please issue 'dfx canister create operator_admin'.
#:
#: Matched on the stable half of that sentence. dfx interpolates the canister name
#: into the second half and has changed the wording of such messages between
#: versions, so the prefix is what is matched and a miss falls through to
#: "unreachable" -- which is the safe direction to be wrong in, because it claims
#: less.
CANISTER_ABSENT_MARKER = "Cannot find canister id"


def canister_lookup_verdict(returncode: int, stdout: str, stderr: str) -> tuple[str | None, str, str]:
    """What `dfx canister id <name>` actually told us. (id, kind, reason). PURE.

    kind is one of:
      found          an id was read.
      not_deployed   dfx ANSWERED and said the replica has no such canister.
      unreachable    dfx could not be asked, or failed for some other reason.

    ---------------------------------------------------------------------------
    WHY not_deployed EXISTS AS A SEPARATE ANSWER. Measured on the operator's host
    2026-10-08, and it cost them three canisters.
    ---------------------------------------------------------------------------

    Their replica container had been created two hours BEFORE
    `icp-replica-data:/root/.local/share/dfx` entered docker-compose.icp.yml
    (28de99c, 2026-10-07 20:38 UTC), so its dfx state lived in the container's
    WRITABLE LAYER rather than on the volume. `swap_stack.py down` stopped it and
    the layer survived, which is what `down` was rewritten to guarantee. Then `up`
    rebuilt both images, compose recreated the container because its image had
    changed, and the writable layer went with it -- the ledger holding the desk's
    998.9498 LICP, threshold_custody, and operator_admin, deployed that morning.

    `up` ASKED, AND dfx ANSWERED, AND THE REPORT SAID IT COULD NOT TELL. All three
    lookups came back with the sentence above, and all three were filed under
    "COULD NOT READ" beneath a header reading "Two causes and this cannot tell
    them apart: docker/dfx was not reachable, or the canisters are not deployed on
    this replica."

    That sentence was false at the moment it printed. dfx had just distinguished
    them. An operator reading COULD NOT READ assumes a docker hiccup and moves on;
    the fact available was "this replica has no canisters at all", which on a
    replica that had three an hour earlier is the loudest thing on the screen.
    Rule 14's "did nothing must not look like did work" and rule 17's refusal to
    state an unknown where a measurement exists, in one line of output.

    THE CONSEQUENCE IS NOT COSMETIC. Every `* -> ICP` quote, the desk's ICP
    balance and the operator console all stop working, and nothing else in the
    stack reports it: `up` printed SERVING and exited 0.
    """
    ident = stdout.strip().splitlines()[-1].strip() if stdout.strip() else ""
    if returncode == 0 and ident:
        return ident, "found", f"read as {ident}"

    complaint = (stderr or stdout).strip()
    why = complaint.splitlines()[-1] if complaint else "(no output)"
    if CANISTER_ABSENT_MARKER in complaint:
        return None, "not_deployed", why
    return None, "unreachable", why


def canister_surface_lines(
    canister_ids: Mapping[str, str | None],
    absent: int = 0,
    ui_canister_id: str = "",
) -> list[str]:
    """The ICP half of the surface map. Pure.

    @param absent  how many canisters dfx ANSWERED were not deployed, as counted
                   by canister_lookup_verdict(). Zero means either they were all
                   read or dfx could not be asked -- two states the header below
                   must not merge, which is the whole subject of that function.

    @param canister_ids  {name: id}, where a value of None means the id was NOT
                         established -- docker unreachable, dfx failed, or the
                         canister is not deployed. A name missing from the mapping
                         is treated identically to None, so a caller that could
                         not run docker at all may pass {}.

    THE DECISION: an id that was not read is printed as COULD NOT BE READ, with
    the shape of the URL it would have formed and the command that answers it.
    Never a placeholder that looks like an id, and never an empty section (rule
    14: "(none) is a result; a blank gap is ambiguous between zero rows and a
    query that broke"). Rule 17's form of the same thing: a reason to believe a
    canister id is not having read it.

    THE REPLICA'S OWN STATUS URL IS PRINTED EITHER WAY and is labeled DEBUG, not
    PAGE. It is a fixed address rather than an id-dependent one, so it is knowable
    with nothing running -- and `up`'s replica-readiness check probes it and says READY or NOT
    READY, so the operator is never left reading this line as a liveness claim.
    """
    read = {name: canister_ids.get(name) for name, _page, _what in CANISTER_SURFACES}
    if any(read.values()):
        lines = [
            "  canisters         asked of the replica with `dfx canister id`, never hardcoded: every",
            f"{_MAP_CONTINUATION}fresh replica issues different ids, so an id in a file is somebody else's",
        ]
    elif absent:
        # dfx ANSWERED: the replica has none of them. This is a different and much
        # louder fact than "could not read", and conflating the two is what let
        # three canisters disappear unremarked on 2026-10-08 -- see
        # canister_lookup_verdict() for the whole incident.
        lines = [
            "  canisters         *** THE REPLICA HAS NO CANISTERS DEPLOYED. *** Not 'could not read' --",
            f"{_MAP_CONTINUATION}dfx answered, for {absent} of {len(read)}, that it cannot find the id.",
            f"{_MAP_CONTINUATION}IF THIS REPLICA HAD CANISTERS BEFORE, THEY ARE GONE: a replica whose dfx",
            f"{_MAP_CONTINUATION}state was in the container's writable layer loses all of it when the",
            f"{_MAP_CONTINUATION}container is recreated, which `up` does whenever an image rebuilds.",
            f"{_MAP_CONTINUATION}Every * -> ICP quote, the desk's ICP balance and the operator console",
            f"{_MAP_CONTINUATION}stop working until they are back. `cd icp && dfx deploy` recreates them;",
            f"{_MAP_CONTINUATION}a redeployed ledger restores only what icp_ledger_init.did seeds, so a",
            f"{_MAP_CONTINUATION}balance that was minted after deployment must be minted again.",
        ]
    else:
        lines = [
            "  canisters         ids COULD NOT BE READ, so NONE is printed below. dfx could not be",
            f"{_MAP_CONTINUATION}asked -- docker absent, the daemon down, or the replica not answering --",
            f"{_MAP_CONTINUATION}so this says nothing about whether they are deployed. Nothing is guessed:",
            f"{_MAP_CONTINUATION}every fresh replica issues different ids, so a guess would link to",
            f"{_MAP_CONTINUATION}another deployment. `docker compose exec -T icp-replica dfx canister id",
            f"{_MAP_CONTINUATION}<name>` is the answer.",
        ]
    for name, serves_page, what in CANISTER_SURFACES:
        ident = read[name]
        if ident is None:
            marker = "NOT DEPLOYED on this replica" if absent else "id COULD NOT BE READ"
            lines.append(f"    {'PAGE' if serves_page else 'CANDID':<6} {name:<20} {marker}")
            # THE SHAPE, WITH WHICHEVER HALF IS KNOWN. A page's URL needs only the
            # canister's own id, so its shape is always printable; a Candid URL
            # needs the UI canister's id TOO, and when that was not read the
            # shape says so rather than naming an id nobody asked for.
            if serves_page:
                shape = f"http://<id>.localhost:{REPLICA_PORT}/"
            elif ui_canister_id:
                shape = f"http://127.0.0.1:{REPLICA_PORT}/?canisterId={ui_canister_id}&id=<id>"
            else:
                shape = (f"http://127.0.0.1:{REPLICA_PORT}/?canisterId=<the Candid UI canister>"
                         "&id=<id> -- neither id was read")
            lines.append(f"{_MAP_CONTINUATION}{what}")
            lines.append(f"{_MAP_CONTINUATION}its URL would be {shape} -- the id is the missing part")
            continue
        if serves_page:
            lines.append(f"    {'PAGE':<6} {name:<20} http://{ident}.localhost:{REPLICA_PORT}/")
            lines.append(f"{_MAP_CONTINUATION}{what}")
            candid = candid_url(ident, ui_canister_id)
            lines.append(
                f"{_MAP_CONTINUATION}its Candid interface: {candid}" if candid
                else f"{_MAP_CONTINUATION}its Candid interface: NOT LINKED -- "
                     f"`dfx canister id {CANDID_UI_CANISTER_NAME}` was not read, and a Candid "
                     "link built on a guessed UI id shows the wrong canister's interface"
            )
        else:
            candid = candid_url(ident, ui_canister_id)
            lines.append(
                f"    {'CANDID':<6} {name:<20} {candid}" if candid
                else f"    {'CANDID':<6} {name:<20} id {ident} read; NO CANDID LINK -- "
                     f"`dfx canister id {CANDID_UI_CANISTER_NAME}` was not read"
            )
            lines.append(f"{_MAP_CONTINUATION}{what}")
    lines.append(f"    {'DEBUG':<6} {'replica status':<20} {REPLICA_STATUS_URL}")
    # NAMED, NOT NUMBERED. This used to read "what `up` step 4 probes" and went stale
    # the moment a step was inserted ahead of it -- a printed line, in the report whose
    # whole job is to be trustworthy at a glance (rule 16: a wrong comment is a bug, and
    # this one was a wrong LINE). A step number is spelled once where the step is; naming
    # the check instead means nothing to keep in sync.
    lines.append(f"{_MAP_CONTINUATION}dfx's own health endpoint -- what `up` probes before "
                 "trusting the replica. NOT a page")
    return lines


def surface_map(
    serving_port: int | None,
    canister_ids: Mapping[str, str | None],
    absent: int = 0,
    ui_canister_id: str = "",
) -> list[str]:
    """WHERE IS EVERYTHING. The whole map, as lines, ready for swap_stack.say().

    Pure, and it is the only thing `up` and `status` both call for this -- one map,
    two readers, so the two commands cannot drift into describing different systems
    (rule 8). The I/O that produces both arguments lives in swap_stack.py: a probe
    of WEB_PORT_CANDIDATES for the port, and `dfx canister id` inside the replica
    container for the ids.

    IT IS A LIST AND NOT A PRINT so a test can read every line it would have shown
    and assert on the one thing that matters -- that a URL appears only for a port
    that answered and an id that was actually read. A print loop's output is only
    checkable by capturing stdout, which is how a report gets shipped with a URL in
    it that nothing serves.

    SPLIT IN THREE BECAUSE ONE FUNCTION WOULD CROSS C901, and rule 12 is explicit
    that the fix is to extract the decision rather than raise the ceiling. The two
    halves are also the two independent decisions -- "may a web URL be written"
    and "may a canister id be written" -- so they are worth asserting separately.
    """
    header = [
        "  surface map       WHERE EVERYTHING IS, in one place. Operator 2026-10-08: \"so we have 3",
        f"{_MAP_CONTINUATION}canisters now. 3 different hyperlinks. where's the main landing page for",
        f"{_MAP_CONTINUATION}the atm screen?\"  PAGE = a human opens it. ADMIN = a page, the",
        f"{_MAP_CONTINUATION}operator's. JSON = a machine reads it, so JSON in a browser is not a",
        f"{_MAP_CONTINUATION}broken page. CANDID/DEBUG = a developer surface and not a page at all.",
    ]
    return header + web_surface_lines(serving_port) + canister_surface_lines(canister_ids, absent, ui_canister_id)
