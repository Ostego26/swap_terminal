#!/usr/bin/env python3
"""One command for the whole swap terminal: containers, workers and servers together.

Role: file (entry point -- what an operator, a launcher or a cron entry names)
Reads: docker compose's own `ps`, /proc for listeners, the supervisor's run
       directory and pid files, Config for the database path it echoes, an HTTP
       GET of / on each candidate web port, and `dfx canister id` inside the
       replica container -- the last two are the surface map's two inputs
Writes: nothing of its own. `down` signals processes through supervisor.stop_worker()
       and removes containers through `docker compose down`; neither is a file
       this writes.
Can move funds: NO, and `up` is the one to read twice: it starts a payout worker
       that CAN broadcast on every configured chain. That is why `up` echoes the
       database and the armed state before it spawns anything, exactly as
       supervisor.py start does, and why this file never passes a credential.
Live-safe: `status` is safe at any time against anything. `down` stops this
       terminal's own processes and NEVER a chain daemon -- see
       stack_authority.NEVER_STOPPED. `up` arms a payout worker.

IT LIVES AT THE REPOSITORY ROOT (rule 10): "If it is something someone runs, it
belongs at the root where it can be found without knowing the layout."

WHY IT EXISTS. Operator, 2026-10-06: "let's make sure when i hit docker up or down
that it stops everything this swap terminal uses including any workers, servers,
daemons, etc."

THE HONEST ANSWER TO THE LITERAL REQUEST IS NO, AND THAT IS WHY THIS FILE IS A
SEPARATE COMMAND RATHER THAN A HOOK. `docker compose down` is docker's own binary.
It has no pre/post hook, no plugin point and no configuration that can reach a
process outside its own containers -- so no amount of editing docker-compose.yml
makes `docker compose down` stop a host gunicorn or a host payout worker. What CAN
be made true is that ONE command does all of it, and that is this file. Typing
`docker compose down` directly will still leave the host half running; `status`
below is what says so rather than leaving it to be discovered.

WHAT IT IS FIXING, measured the same day on the live host rather than imagined:

    six workers polling one database   three host (pid files), three container
                                       orphans at pids 8/9/10. TWO payout workers.
    one gunicorn on 127.0.0.1:5101     named by no pid file, no compose file, not
                                       the supervisor and not the desktop
                                       launcher. Found because a bind failed.

Three reapers existed -- supervisor.py for the workers, swap_terminal_desktop.py
for the gunicorn it starts, compose for the containers -- each correct about its
own half and blind to the other two. None of them could answer "is anything still
running?", which is the only question that matters before starting work.

WHAT `down` DELIBERATELY LEAVES ALONE, loudly rather than silently: bitcoind,
litecoind, gridcoinresearchd and any other chain daemon. They hold the wallets
this desk spends from, CLAUDE.md's live-safety rules put stopping them outside
what this repository may do, and a `down` that took them with it could not undo
itself -- this file holds no passphrase and could not unlock a wallet again. They
are printed as LEFT RUNNING with that reason, because a daemon spared on purpose
must not read like one the scan missed (rule 14).

A FOREIGN listener on one of our ports is also left alone, under every flag. See
stack_authority.stray_verdict(): killing a stranger's process because it answered
on 5101 is precisely what this project's own orphans did to it.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "swap_terminal"))

import supervisor  # noqa: E402  -- after the sys.path.insert above, same as every root tool
from config import Config  # noqa: E402
from microfortnights import format_duration  # noqa: E402  -- same sys.path.insert

from swap_terminal.stack_authority import (  # noqa: E402
    CANDID_UI_CANISTER_NAME,
    REPLICA_STATE_PATH,
    REPLICA_STATUS_URL,
    STACK_PORTS,
    VERSION_STATUSES_WORTH_REPEATING,
    WEB_PORT_CANDIDATES,
    CanisterLookups,
    GitReading,
    canister_lookup_names,
    canister_lookup_verdict,
    chain_reachability_verdict,
    code_version_verdict,
    container_id,
    container_label,
    container_verdict,
    down_verdict,
    listening_inodes,
    pids_owning_inodes,
    port_is_free,
    proc_net_tcp_tables,
    readiness_verdict,
    replica_state_verdict,
    serving_verdict,
    stray_verdict,
    surface_map,
)

#: How long each web-port probe may take, and the marker for one that answered.
#:
#: SECONDS, and short on purpose: a gunicorn that will answer does so as soon as it
#: has bound, so the replica's 60s budget spent on four candidate ports would make
#: a down stack take four minutes to say it is down (rule 14 -- the report has to
#: arrive while the operator is still reading).
_WEB_PROBE_BUDGET_SECONDS = 3.0
_WEB_PROBE_OK = 200

#: How long `up` waits for the replica to answer, and how long one probe may take.
#:
#: THE URL ITSELF IS stack_authority.REPLICA_STATUS_URL AND NO LONGER LIVES HERE.
#: It was a private `_REPLICA_STATUS_URL` in this file until 2026-10-08, when the
#: surface map needed the same address and the choice was a second spelling or one
#: constant -- rule 8: "Two copies of one rule is not redundancy, it is a bug with
#: a delay on it." The ADDRESS is a fact about the stack and belongs beside
#: STACK_PORTS; the WAIT BUDGETS are this command's policy and stay here.
#:
#: dfx serves /api/v2/status; a 200 is the replica saying it is up. 60s because a
#: fresh `dfx start` on a cold volume takes appreciably longer than the container
#: takes to be "Started", which is the whole gap this probe closes -- measured
#: 2026-10-07, when a call issued immediately after `up` reported :4943 BOUND got
#: Connection refused.
#:
#: Seconds, not microfortnights, because they are passed to urlopen(timeout=) and to
#: time.monotonic() arithmetic -- an interface, not a report (rule 6). The figure is
#: PRINTED in microfortnights.
_REPLICA_WAIT_SECONDS = 60.0
_REPLICA_PROBE_TIMEOUT_SECONDS = 2.0

#: The compose service `dfx` runs in, and how long one `dfx canister id` may take.
#:
#: THE SERVICE NAME IS A COMPOSE SERVICE (`icp-replica`), NOT THE CONTAINER NAME
#: (`swap-icp-replica`). They differ in this project -- docker-compose.icp.yml sets
#: container_name -- and `docker compose exec` takes the service, which is the same
#: distinction chains/icp.py records at its own dfx call site. config.py's
#: ICP_DFX_SERVICE default is this string; it is repeated rather than imported
#: because that default is the APP's transport setting and this is a report asking
#: a question of a container it just started. If they ever need to agree, the fix
#: is for both to read Config, not for one to guess.
#:
#: 10s each, matching the `docker inspect` lookup in classify_listener(): both are
#: auxiliary questions that make a report self-describing, and neither is worth
#: hanging the report for. Three lookups run back to back, so the worst case is
#: 30s and the operator is told the scale before the wait (rule 14).
_DFX_SERVICE = "icp-replica"
_CANISTER_ID_TIMEOUT_SECONDS = 10.0

#: Docker's absolute path, resolved once at import.
#:
#: NOT THE BARE NAME, and this is a fix rather than a lint appeasement. S607 ("partial
#: executable path") fired on the `docker inspect` call added 2026-10-06 and did NOT
#: fire on compose() ten lines below it -- which runs the same bare "docker" -- purely
#: because compose() builds its argv in a variable and ruff cannot see the literal. So
#: one call site was flagged and an identical one was not, which is rule 8's two
#: spellings of one thing with a linter picking sides.
#:
#: Resolving it buys a real thing beyond satisfying the check: with a bare name,
#: `docker` missing from PATH surfaces as a FileNotFoundError from deep inside a
#: subprocess call, and the same error shape means both "docker is not installed" and
#: "the id does not exist". Resolved here, absence is answerable once, by name, before
#: any command runs -- which is what the `None` branch below reports.
#:
#: The `or "docker"` keeps the argv valid when docker is absent so the failure is
#: docker's own "not found" rather than a TypeError about None.
_DOCKER = shutil.which("docker") or "docker"

#: Same treatment for git, for the same two reasons: one spelling, and absence
#: answerable by name instead of as a FileNotFoundError from inside subprocess.
#:
#: git being missing is NOT a failure of this tool -- the stack runs fine without
#: it. It only means nothing can say which commit is running, which git_reading()
#: reports as exactly that rather than as an error.
_GIT = shutil.which("git") or "git"

#: The compose files this stack is assembled from, in the order `-f` wants them.
#:
#: HARDCODED RATHER THAN GLOBBED, and the glob is the bug it avoids: the directory
#: also holds docker-compose.web.armed-sol.yml, .armed-xrp.yml and .hostnet.yml,
#: which are ALTERNATIVE overlays. A glob would pass all of them to one command and
#: the last one silently wins, so `up` would arm SOL or XRP because of a filename's
#: sort order. An operator wanting an armed overlay names it with --compose-file.
COMPOSE_FILES = ("docker-compose.yml", "docker-compose.icp.yml", "docker-compose.web.yml")

#: The services `up` starts, NAMED rather than left to compose's default of "all of
#: them in every -f file".
#:
#: THIS IS A FIX, MEASURED 2026-10-07. docker-compose.yml also defines `abstergo` (a
#: GRC-SOL exchange under swap_terminal/grc-sol-swap/) and `harness` (the test
#: harness, docker/harness.Dockerfile), and NEITHER carries a `profiles:` key --
#: checked across all three files, there is not one. Compose starts every service in
#: every file it is given unless told otherwise, so the first `swap_stack.py up`
#: would have built and started a TEST HARNESS on a host holding real testnet
#: wallets, plus an exchange container nothing in this stack talks to.
#:
#: That is the opposite of what `up` is for and it is the kind of surprise a single
#: command must never have: the operator asked for one lever over their stack, not a
#: lever that also starts whatever else happens to live in the same yaml.
#:
#: `down` is deliberately NOT narrowed the same way. "Stop everything this swap
#: terminal uses" is the whole point of it, so it removes the project's containers
#: wholesale -- if abstergo or harness IS up, from a bare `docker compose up` or an
#: earlier session, `down` should take it with the rest rather than leave it behind
#: for the same reason the 5101 container was worth finding.
#: The services `up` starts. `web` IS one of them, and 5ecf442 removing it was the
#: wrong half of a correct observation.
#:
#: THE OBSERVATION WAS RIGHT: the two deployments are mutually exclusive, because
#: docker/web.Dockerfile's CMD is docker/web_workers_entrypoint.py, which starts
#: gunicorn AND the three workers. Running `web` beside supervisor.start() put six
#: workers and two payout workers on one database -- measured on the operator's host
#: 2026-10-07 from their own `up` output.
#:
#: 5ecf442 THEN PICKED THE WRONG ONE. It kept the host deployment, which leaves the
#: operator running `python app.py` in a terminal -- the Flask DEVELOPMENT server,
#: which prints "Do not use it in a production deployment" every time it starts. The
#: operator's requirement, stated three ways in one sitting: "it should all be under
#: docker", "that web page should be serving after the docker container is running",
#: "transition from just flask to a gunicorn wsgi server".
#:
#: All three are the same change, because the containerized deployment already IS
#: gunicorn: web_workers_entrypoint.py runs `gunicorn -c gunicorn.conf.py wsgi:app`.
#: `python app.py` is the dev server and the only reason it was being used is that
#: `up` was starting the wrong deployment. So `up` starts the containerized one and
#: REFUSES rather than adding to a running host one -- see cmd_up().
#:
#: WHAT IS STILL NOT SOLVED BY THIS, stated here so it is not discovered later: the
#: web container cannot make an ICP call. No dfx and no docker CLI in the image, and
#: the desk's dfx signing identity lives in the replica container. Every other pair
#: works; ICP does not. That is the next piece of work and it is NOT fixed by this
#: constant.
UP_SERVICES = ("icp-replica", "web")


def say(line: str) -> None:
    """Print immediately. Rule 14: silence is indistinguishable from hung."""
    print(line, flush=True)


def compose(
    args: list[str], files: tuple[str, ...], check: bool = False, timeout: float | None = None,
) -> subprocess.CompletedProcess:
    """Run one `docker compose` command with this stack's files, from the repo root.

    `cwd=REPO_ROOT` is not decoration. Compose resolves the relative paths INSIDE
    those files -- `build: context: .`, the `./icp:/repo` mount -- against the
    process's working directory, and the same omission in chains/icp.py broke every
    ICP call from the app earlier today (see that file's _REPO_ROOT comment). One
    measurement, two files, same fix.

    `timeout` IS OPTIONAL AND DEFAULTS TO NONE, which is what every caller before
    2026-10-08 got and still gets: `up`, `down` and `ps` are the work the operator
    asked for, and cutting one off at an arbitrary second would abandon a build or
    a teardown midway. It exists for the AUXILIARY lookups added with the surface
    map -- a `dfx canister id` that makes a report self-describing is never worth
    hanging the report for, which is the same trade classify_listener() already
    makes with timeout=10 on its `docker inspect`. The caller catches
    subprocess.TimeoutExpired; it is not swallowed here, because a lookup that
    timed out and a lookup that answered "no such canister" must not read alike.
    """
    argv = [_DOCKER, "compose"]
    for name in files:
        argv += ["-f", str(REPO_ROOT / name)]
    argv += args
    return subprocess.run(  # noqa: S603 -- no shell; argv is this file's own list plus a subcommand
        argv, cwd=REPO_ROOT, capture_output=True, text=True, check=check, timeout=timeout,
    )


def classify_listener(port: int, pid: int | None, ours_containers: frozenset[str]) -> dict:
    """One LISTEN socket on a stack port, classified into a report row.

    Reads /proc/<pid>/cmdline and /proc/<pid>/cgroup and may shell out to `docker
    inspect` for a label. Changes nothing.

    EXTRACTED FROM report_listeners() 2026-10-08, when adding the compose-membership
    check pushed that function to C901 11 > 10. Rule 12 says exactly what that
    complaint means and what it does not license: "A main() past the ceiling is
    orchestration that has swallowed decisions, which is rule 10's defect wearing a
    lint code. The fix is to extract the decision so it can be called with seeded
    inputs, not to raise the ceiling." This is the part that decides; what is left
    behind is the part that scans.

    And the extraction is what makes the containerized verdict reachable from a
    test at all. Before it, the only way to exercise that branch was to have a
    container actually listening on one of this stack's ports.
    """
    if pid is None:
        # WHY AN OWNERLESS LISTENER IS THE EXPECTED READING FOR A PUBLISHED
        # CONTAINER PORT, said here because a bare "UNKNOWN" reads as a failure.
        # Measured 2026-10-06: :4943 showed a LISTEN row and no owner. The socket
        # belongs to a root-owned docker-proxy, and pids_owning_inodes() cannot
        # read /proc/<pid>/fd for a process this user does not own -- it catches
        # PermissionError per pid and moves on, so the answer is "this user cannot
        # see what", NOT "nothing owns this port". Running as root would name it;
        # nothing here asks for that, because reading other users' fds to
        # prettify a report is a bad trade.
        return {
            "port": port, "pid": None, "verdict": "owner unknown", "container": None,
            "reason": (
                "a LISTEN socket exists and its owner is not readable by this user -- the usual "
                "cause is a root-owned docker-proxy for a published container port, which is "
                "expected rather than wrong. `sudo ss -ltnp` names it; this file will not ask for root"
            ),
            "cmdline": "",
        }

    try:
        cmdline = Path(f"/proc/{pid}/cmdline").read_text()
    except OSError:
        cmdline = ""
    verdict, reason = stray_verdict(cmdline, REPO_ROOT)

    # A CONTAINERIZED LISTENER OVERRIDES THE CMDLINE VERDICT, because a host path
    # can never appear in its argv -- docker/web.Dockerfile puts this repository
    # at /app, so stray_verdict()'s repo_root substring test cannot ever pass for
    # it and it reads FOREIGN. Measured on the live host: the gunicorn on 5101 is
    # THIS project's, and the lever is `docker stop <id>` or `swap_stack.py down`
    # rather than a pid. See container_id().
    try:
        inside = container_id(Path(f"/proc/{pid}/cgroup").read_text())
    except OSError:
        inside = None

    if inside and verdict != "refused":
        # ASK DOCKER WHAT IT IS, so the line is self-describing (rule 14). The
        # operator had to run `docker ps` themselves to turn an id into "st-ui on
        # an untagged image", which is the round trip this avoids. A failure here
        # degrades the label and never the verdict: `docker` may not be on PATH at
        # all, and a report that died because it could not pretty-print a name
        # would lose the finding it was printing.
        named = ""
        try:
            inspected = subprocess.run(  # noqa: S603 -- no shell; argv is fixed but for the id read from /proc
                [_DOCKER, "inspect", "--format", "{{.Name}} {{.Config.Image}}", inside],
                capture_output=True, text=True, timeout=10, check=False,
            )
            if inspected.returncode == 0:
                named = container_label(inspected.stdout)
        except (OSError, subprocess.SubprocessError):
            named = ""

        # THE VERDICT IS NOW DECIDED RATHER THAN ASSERTED, and that is the fix of
        # 2026-10-08. This branch used to set verdict = "container" and then state,
        # as a finished sentence, that `docker compose ps` did not list the
        # container -- for EVERY container, having asked nothing. On the operator's
        # host it therefore told them to `docker stop` the web container that was
        # serving their own page, four lines under a `docker compose ps` table
        # listing it. container_verdict() takes the listing and returns the reason
        # that matches, so the sentence and the branch cannot disagree.
        verdict, reason = container_verdict(inside, ours_containers)
        if named:
            reason = f"{named}: {reason}"

    return {
        "port": port, "pid": pid, "verdict": verdict, "reason": reason,
        "cmdline": cmdline.replace("\0", " ").strip(), "container": inside,
    }


def stack_container_ids(files: tuple[str, ...]) -> frozenset[str]:
    """Every container id `docker compose ps -q` reports for this stack. Read-only.

    ASKED RATHER THAN ASSERTED, which is the entire point of it existing.
    report_listeners() used to TELL the operator that a containerized listener
    "belongs to no compose project this stack names" without ever asking -- see
    container_verdict() for what that cost. This is the question that sentence was
    answering.

    `ps -q` rather than parsing the `ps` table: ids are what /proc/<pid>/cgroup
    yields, so comparing ids compares like with like, and a table whose column
    layout shifts between docker versions is not something a verdict should rest
    on. `--all` is deliberately NOT passed -- a stopped container cannot be
    holding a LISTEN socket, so including one would only add ids that can never
    match.

    A FAILURE RETURNS EMPTY AND IS NOT AN ERROR. docker may not be installed, the
    daemon may be down, the project may have no containers; container_verdict()
    handles the empty set by refusing to claim anything about compose membership
    rather than by guessing, so the three cases stay distinguishable to a reader.
    """
    done = compose(["ps", "-q"], files)
    if done.returncode != 0:
        return frozenset()
    return frozenset(line.strip() for line in done.stdout.splitlines() if line.strip())


def report_listeners(files: tuple[str, ...]) -> list[dict]:
    """Every process LISTENing on one of STACK_PORTS, classified. Reads, changes nothing.

    TAKES THE COMPOSE FILES as of 2026-10-08, because classifying a containerized
    listener requires knowing which containers this stack has, and only the compose
    files can answer that. See container_verdict().
    """
    ours_containers = stack_container_ids(files)
    # BOTH FAMILIES, AND READING ONLY ONE OF THEM PRODUCED A SELF-CONTRADICTING
    # REPORT ON 2026-10-07. `swap_stack.py down` printed, in the same block:
    #
    #     :4943 STILL BOUND  <- bind attempted, not inferred
    #     :5100 STILL BOUND  <- bind attempted, not inferred
    #     (none)            nothing is LISTENing on any of 4943, 5000, 5100, ...
    #
    # Two measurements, both honest, and between them an unresolvable question --
    # because this function read /proc/net/tcp and nothing else. Docker publishes a
    # port on BOTH 0.0.0.0 and ::, and a dual-stack socket bound to :: with
    # IPV6_V6ONLY off captures IPv4 connections while appearing ONLY in
    # /proc/net/tcp6. So an IPv6 listener holding one of our ports was invisible
    # here and failed the bind over in port_is_free() -- "(none)" meant "none that
    # this scan can see", which is precisely the voice rule 17 forbids.
    #
    # listening_inodes() already anticipated this: its own comment says "two rows
    # for one port means tcp and tcp6 both carry it, same socket" -- a case its
    # only caller made impossible to reach. hex_port() rsplits on the last colon,
    # so a 32-hex-character tcp6 local_address parses through the identical path
    # with no special case.
    #
    # tcp IS REQUIRED AND tcp6 IS NOT. A kernel booted with ipv6.disable=1 has no
    # /proc/net/tcp6 at all, and refusing to report anything because the optional
    # half is absent would lose the half that works. A missing /proc/net/tcp, by
    # contrast, means this report cannot be made.
    proc_net, missing = proc_net_tcp_tables(Path("/proc/net"))
    if "tcp" in missing:
        say("  listeners         /proc/net/tcp CANNOT BE READ. The port half of this report is MISSING,")
        say("                    which is not the same as 'nothing is listening'")
        return []
    if "tcp6" in missing:
        # Said out loud rather than passed over: an operator reading "(none)" is
        # entitled to know which families were actually looked at (rule 14).
        say("  listeners         /proc/net/tcp6 is absent -- IPv4 only was scanned. On a kernel with")
        say("                    IPv6 disabled that is complete; otherwise an IPv6 listener is unseen")
    inodes = listening_inodes(proc_net, set(STACK_PORTS))
    owners = pids_owning_inodes(set(inodes.values()), Path("/proc"))
    found = []
    for port, inode in sorted(inodes.items()):
        found.append(classify_listener(port, owners.get(inode), ours_containers))
    return found

def print_listeners(found: list[dict]) -> None:
    say("  http listeners    what is answering on this stack's ports")
    if not found:
        say("  (none)            nothing is LISTENing on any of "
            f"{', '.join(str(p) for p in sorted(STACK_PORTS))} <- a result, not a blank")
        return
    for row in found:
        # EVERY VERDICT IN stack_authority.LISTENER_VERDICTS NEEDS AN ENTRY HERE.
        # This indexes with [], so a verdict with no label raises mid-report and
        # takes the finding down with it -- which is why that set is named over
        # there and tests/test_stack_authority.py asserts this map covers it.
        label = {
            "ours": "OURS",
            "ours (container)": "OURS (CONTAINER)",
            "foreign": "FOREIGN",
            "refused": "CHAIN DAEMON",
            "container": "IN A CONTAINER",
            "owner unknown": "UNKNOWN",
        }[row["verdict"]]
        say(f"  {label:<17} :{row['port']} pid={row['pid']}  {STACK_PORTS[row['port']]}")
        if row.get("reason"):
            say(f"                    {row['reason']}")
        if row["cmdline"]:
            say(f"                    {row['cmdline'][:150]}")


def cmd_status(files: tuple[str, ...]) -> int:
    """Everything that is running, from all three reapers, in one place."""
    say("swap_stack: STATUS")
    code = _say_code_version()
    say(f"  repository        {REPO_ROOT}")
    say(f"  database          {Config.DB_PATH}  <- SWAP_DB_PATH")
    say(f"  compose files     {', '.join(files)}")
    say("")
    say("  containers        docker compose ps")
    done = compose(["ps"], files)
    body = done.stdout.strip()
    if done.returncode != 0:
        say(f"  FAILED            docker compose exited {done.returncode}: {done.stderr.strip() or '(no stderr)'}")
    elif len(body.splitlines()) <= 1:
        say("  (none)            no container of this stack is up <- a result, not a blank")
    else:
        for line in body.splitlines():
            say(f"                    {line}")
    say("")
    print_listeners(report_listeners(files))
    say("")
    say("  workers           supervisor.py owns these; this is its own report")
    supervisor.main(["status", "--run-dir", str(supervisor.DEFAULT_RUN_DIR)])
    say("")

    # THE SURFACE MAP, AND `status` PROBES FOR IT RATHER THAN REUSING THE LISTENER
    # SCAN ABOVE. The scan knows which of these ports has a LISTEN socket, which is
    # cheaper and is the wrong question: readiness_verdict()'s measurement is that a
    # published container port is bound by docker-proxy before anything inside has
    # opened a socket, so a url built from "bound" is a link to something that
    # answers nothing. probe_serving_port() is the same prober `up`'s page check uses,
    # so
    # the two commands cannot describe different systems (rule 8).
    #
    # IT COSTS UP TO FOUR PROBES AND THREE DOCKER LOOKUPS, which is why the scale is
    # printed first (rule 14). `status` was about a second before this; it is now
    # the slowest of the three commands when nothing is up, and an operator who
    # cannot see why would read it as hung.
    say(f"  web probe         probing {len(WEB_PORT_CANDIDATES)} candidate web ports, up to "
        f"{format_duration(_WEB_PROBE_BUDGET_SECONDS)} each -- a BOUND port above is not a serving one")
    port, _detail = probe_serving_port()
    _say_surface_map(files, port)
    _say_code_version_repeat(*code)
    return 0


def print_bound_but_no_listener(bound: list[int]) -> None:
    """Say that a stop is UNPROVEN for these ports, and name the two readings.

    Its own function because cmd_down() crossed PLR0915 when this was inlined,
    and rule 12 is explicit that the fix is to extract rather than raise the
    ceiling. The decision itself is down_verdict() in stack_authority.py; this
    is only how it reads on screen.
    """
    ports = ", ".join(f":{port}" for port in bound)
    say(f"  STOPPED, NOT PROVEN  nothing this project owns is LISTENing, but {ports} could not be")
    say("                    bound just now, so the absence is NOT proven for those ports.")
    say("                    Two readings, and this cannot tell them apart from here:")
    say("                      TIME_WAIT   the usual one. A socket whose process is already gone")
    say("                                  holds the address for ~60s; port_is_free() omits")
    say("                                  SO_REUSEADDR on purpose, so it reads as bound. Benign,")
    say("                                  clears itself, and `up` will bind once it does.")
    say("                      a listener  owned by a user whose /proc this one cannot read, so")
    say("                                  the scan above could not name it. NOT benign.")
    say(f"                    `sudo ss -ltnp | grep -E '{('|'.join(str(p) for p in bound))}'` separates them:")
    say("                    output means a listener, no output means TIME_WAIT. Re-running `down`")
    say("                    after a minute is the cheaper check -- `free` then means TIME_WAIT.")


def cmd_down(files: tuple[str, ...]) -> int:
    """Stop this terminal's processes and containers, then PROVE it.

    ORDER MATTERS AND IS NOT ARBITRARY. Workers first, containers second:
    a worker mid-payout gets its SIGTERM and its grace period while the chain
    daemons and the ICP replica are still reachable, so a send in flight can
    finish or fail cleanly rather than losing its RPC underneath it. Tearing the
    replica down first would turn an in-flight ICP payout into a dfx timeout of
    unknown outcome, which is the one failure this system cannot read afterwards.
    """
    say("swap_stack: DOWN")
    say(f"  database          {Config.DB_PATH}")
    say("  order             workers (SIGTERM + grace, absence PROVEN) -> containers -> prove ports free")
    say("  containers        STOPPED, not removed -- `docker compose down` removed the replica's")
    say("                    container on 2026-10-07 and its ledger canister went with it. Nothing")
    say("                    of this stack runs when this returns, which is what `down` is for.")
    say("  never stopped     chain daemons, and a FOREIGN listener on one of our ports. Both are")
    say("                    reported below rather than passed over in silence")
    say("")
    say("  1. workers")
    supervisor.main(["stop", "--run-dir", str(supervisor.DEFAULT_RUN_DIR)])
    say("")
    say("  2. containers -- STOPPED, NOT REMOVED. See the comment below.")
    # `stop` AND NOT `down`, AND THIS COST THE OPERATOR THEIR LEDGER STATE ONCE.
    #
    # Measured 2026-10-07. After `swap_stack.py down && swap_stack.py up`, the first
    # ICP call answered:
    #
    #     reject code DestinationInvalid, reject message Canister
    #     bkyz2-fmaaa-aaaaa-qaaaq-cai not found, error code Some("IC0301")
    #
    # The ledger canister -- holding the desk's 1000 test ICP, deployed and funded
    # across two days of work -- was not in the replica any more. `docker compose
    # down` REMOVES containers, and the replica's canister state did not survive its
    # container. docker/icp-replica.Dockerfile sets DFX_CONFIG_ROOT=/state with a
    # comment saying it is "so a `dfx start` survives a rebuild"; that claim is now in
    # doubt and is NOT established either way from here.
    #
    # `stop` satisfies what `down` is for -- nothing of this stack is running when it
    # returns, which is the question an operator asks -- while leaving the containers,
    # and therefore their writable layers, intact. The port proof below is unchanged
    # and is what makes "nothing running" a measurement rather than a claim.
    #
    # WHAT THIS DOES NOT FIX, said plainly rather than left to be rediscovered: `up`
    # RECREATES a container when its image changes, so the next rebuild of the replica
    # image loses the ledger again. The real fix is getting the replica's state onto
    # the icp-state volume, which needs a measurement of where dfx actually puts it --
    # a question no session without a running replica can answer. Until then, redeploy
    # after a replica rebuild: icp_ledger_init.py and `dfx deploy` are the path.
    done = compose(["stop"], files)
    for line in (done.stderr or done.stdout).strip().splitlines():
        say(f"                    {line}")
    if done.returncode != 0:
        say(f"  FAILED            docker compose stop exited {done.returncode}")
    say("")
    say("  3. proof -- absence is the assertion, not an exit code (rule 13)")
    remaining = report_listeners(files)
    # A SURVIVING CONTAINER OF OURS IS "NOT DOWN" TOO, and it was not counted here
    # until 2026-10-08. This read `== "ours"` alone, and every containerized
    # listener classified as "container" -- this stack's own web container
    # included -- so `down` could print "every process this file owns is gone,
    # proven by bind" while the compose service it had just told to stop was still
    # serving on 5101. Same defect down_verdict() was written to fix, one category
    # out: the measurement was taken and then not looked at by the sentence
    # claiming to have proven something from it.
    #
    # "container" -- a container that is NOT ours -- stays uncounted on purpose.
    # `down` never stopped it and must not report a failure to stop what it never
    # touched; print_listeners() prints it with its own reason and the operator
    # decides.
    still = [row for row in remaining if row["verdict"] in ("ours", "ours (container)")]
    bound = []
    for port in sorted(STACK_PORTS):
        free = port_is_free(port)
        if not free:
            bound.append(port)
        say(f"                    :{port} {'free' if free else 'STILL BOUND'}  <- bind attempted, not inferred")
    print_listeners(remaining)
    verdict = down_verdict(bound, len(still))
    if verdict == "not_down":
        say("")
        say(f"  NOT DOWN          {len(still)} listener(s) this project owns are still up and are named above.")
        say("                    They were started by something neither the supervisor nor compose names --")
        say("                    `swap_stack.py status` after killing them by pid is how to confirm.")
        return 1
    say("")
    # A SUMMARY THAT CLAIMED PROOF OVER A PORT IT HAD JUST REPORTED STILL BOUND.
    #
    # Measured 2026-10-07 on the operator's host. This block printed, verbatim:
    #
    #     :4943 STILL BOUND  <- bind attempted, not inferred
    #     :5100 STILL BOUND  <- bind attempted, not inferred
    #     (none)            nothing is LISTENing on any of 4943, 5000, 5100, ...
    #     summary           every process this file owns is gone, proven by bind.
    #
    # The summary was reached because `still` was empty, and `still` counts
    # LISTENERS -- so two failed binds were measured, printed, and then not looked
    # at by the line that claimed to have proven something by binding. That is
    # rule 13's "skipped" beside "success" in one block, and rule 14's "did
    # nothing" wearing the same face as "did work", in the file whose entire third
    # section exists to be the proof.
    #
    # WHAT A BOUND PORT WITH NO LISTENER ACTUALLY MEANS is not one thing, which is
    # why this reports rather than deciding. port_is_free() deliberately omits
    # SO_REUSEADDR, so a socket in TIME_WAIT -- the ordinary residue of a server
    # that just served a connection and exited -- reads as bound and is benign and
    # clears itself. An IPv6 listener used to read this way too; report_listeners()
    # now scans /proc/net/tcp6 for exactly that reason, so this path is narrower
    # than it was, and still not empty: a listener owned by another user with no
    # readable cgroup lands in "owner unknown", not in `still`.
    #
    # So the honest summary distinguishes the two and claims proof for neither.
    if verdict == "stopped_not_proven":
        print_bound_but_no_listener(bound)
        return 1
    say("  summary           every process this file owns is gone, proven by bind: every port in")
    say(f"                    {', '.join(str(p) for p in sorted(STACK_PORTS))} was bindable, and nothing is LISTENing on any")
    say("                    of them. Chain daemons and any FOREIGN listener are untouched, above.")
    return 0


def wait_for_http(url: str, budget_seconds: float, *, announce: bool = True) -> tuple[bool, str, float]:
    """Poll `url` until it answers. (ready, detail, seconds waited).

    GENERALIZED FROM wait_for_replica() ON 2026-10-07, BECAUSE `up` NEEDED THE
    SAME PROBE FOR THE THING A CUSTOMER ACTUALLY OPENS AND DID NOT HAVE IT.

    That gap cost the operator a round the same day. `up` reported

        4. is the replica ANSWERING? (bound is not ready)
        READY  http://127.0.0.1:4943/api/v2/status answered 200 after 0.2µfn (0.2s)

    and then `curl http://127.0.0.1:5101/atm` got `Failed to connect ... Couldn't
    connect to server`. Both true, and the report said SUCCESS: the replica was
    checked and the web service was not, so a dead page and a live one printed the
    same way. That is rule 13's "treat 'skipped' plus 'success' in the same output
    as a defect in the output", in the file whose whole job is to answer "is it
    up?" -- and I had already named this gap one round earlier and not fixed it,
    which is the part worth recording.

    One probe, two callers (rule 8). The URL and the budget are arguments because
    a cold `dfx start` and a gunicorn boot are not the same wait.

    THE DEFECT THIS FUNCTION ORIGINALLY CLOSED, kept because it is the measurement
    that justified having a probe at all rather than trusting a bind. `up` printed

        :4943 BOUND  ICP replica (dfx) -- the local ledger and the custody canister

    and the next command got `Connection refused` from the same replica. Both true: a
    published container port is bound by docker-proxy the moment the container is
    created, so binding proves docker did its part and says nothing about whether dfx
    inside has opened anything. Rule 13's "verify the artifact, not the deploy", one
    layer deeper than the bind that was already an improvement on compose's exit code.

    Progress is printed per attempt (rule 14): a cold `dfx start` can take tens of
    seconds and a silent wait is indistinguishable from a hang, which on this project
    resolves as Ctrl-C.

    `announce=False` TURNS THAT OFF, AND RULE 14 IS WHY RATHER THAN AN EXCEPTION
    TO IT. The rule's own criterion is "anything that can exceed a couple of
    seconds", and it is aimed at the failure it names: an operator who cannot
    tell working from hung reaches for Ctrl-C. That is the replica's wait --
    60 seconds against a cold `dfx start`, and it keeps the progress.

    The web probe is not that wait. Its budget is 3 seconds PER PORT and
    probe_serving_port() prints the outcome for each port as it goes, so the
    per-port line already is the progress. The per-attempt lines on top of it
    were 12 near-identical sentences before the surface map, added to `status`
    -- a read-only command whose whole value is a quick answer -- by a probe
    lifted out of `up`. Rule 14 is about output a reader can act on, and twelve
    copies of one sentence is the same defect as silence approached from the
    other side.
    """
    started = time.monotonic()
    attempt = 0
    while True:
        attempt += 1
        try:
            # THE URL IS NEVER INPUT. Every caller builds it from the literal
            # "http://127.0.0.1:" plus either a constant path or an int from
            # stack_authority.WEB_PORT_CANDIDATES, so no scheme but http can
            # appear and S310's concern (file: or a custom scheme arriving from
            # somewhere) cannot.
            #
            # This comment used to START with the word "noqa", and ruff read it as
            # a BLANKET noqa directive and then reported it as unused (RUF100).
            # A comment whose first word is noqa is a suppression, whatever the
            # rest of the sentence says -- which is a good argument for rule 19's
            # position that a suppression should be rare enough to be deliberate.
            with urllib.request.urlopen(
                url, timeout=_REPLICA_PROBE_TIMEOUT_SECONDS
            ) as answer:
                outcome: object = answer.status
        except urllib.error.HTTPError as error:
            outcome = error.code
        except (OSError, urllib.error.URLError) as error:
            outcome = getattr(error, "reason", error)
            if not isinstance(outcome, BaseException):
                outcome = error
        ready, detail = readiness_verdict(outcome)
        waited = time.monotonic() - started
        if ready:
            return True, detail, waited
        if waited >= budget_seconds:
            return False, detail, waited
        # RULE 6, AND THIS LINE WAS THE ONE PLACE IN THIS FILE STILL PRINTING BARE
        # SECONDS -- found 2026-10-08 while grepping my own additions for the same
        # mistake. "Every timing this system reports -- logs, status lines, reports,
        # diagnostics, tables -- is in microfortnights", and a progress counter read
        # against a `_SECONDS` budget is exactly the case the rule says to print
        # both halves for. Written as `elapsed <both>` rather than inside the
        # parentheses it used to sit in, because format_duration() brings its own.
        if announce:
            say(f"                    attempt {attempt}: {detail}  elapsed {format_duration(waited)}, waiting")
        time.sleep(1.0)


def probe_serving_port() -> tuple[int, str]:
    """Probe every WEB_PORT_CANDIDATES port and return serving_verdict()'s answer.

    ONE PROBER, TWO CALLERS (rule 8). This loop was inline in cmd_up() until
    2026-10-08, when `status` needed the same answer for the surface map -- and the
    alternative to extracting it was a second copy, which is the shape this
    repository keeps paying for: "The copies agree on the day they are written and
    drift from then on."

    IT MUST BE A PROBE AND NOT THE LISTENER SCAN, which is the one thing worth
    being explicit about. report_listeners() already knows which of these ports has
    a LISTEN socket, and that is cheaper -- and it is the wrong question.
    readiness_verdict()'s whole measurement is that a published container port is
    bound by docker-proxy the moment the container is created, before anything
    inside has opened a socket. A map built from "bound" would print the operator a
    link to a port that answers nothing, which is precisely what
    stack_authority.surface_map() exists to never do.

    Prints one line per port as it goes (rule 14): four ports at
    _WEB_PROBE_BUDGET_SECONDS each is a wait long enough to read as a hang.
    """
    probes: dict[int, object] = {}
    for port in WEB_PORT_CANDIDATES:
        # ONE SHORT ATTEMPT PER PORT, not the replica's 60s budget. A gunicorn that
        # is going to answer answers immediately once it has bound; the long wait
        # exists for a cold `dfx start` and spending it four times over would make
        # a down stack take four minutes to say so.
        # announce=False: the per-port line two lines down IS this probe's progress,
        # and the per-attempt lines underneath it put twelve near-identical sentences
        # in front of the surface map. See wait_for_http().
        ready, detail, _ = wait_for_http(
            f"http://127.0.0.1:{port}/", _WEB_PROBE_BUDGET_SECONDS, announce=False
        )
        probes[port] = _WEB_PROBE_OK if ready else detail
        say(f"                    :{port} {detail}")
    return serving_verdict(probes)


def canister_ids(files: tuple[str, ...]) -> CanisterLookups:
    """Ask the replica for each canister's id. ({name: id or None}, what went wrong).

    THE I/O HALF OF THE SURFACE MAP. stack_authority.surface_map() decides what may
    be printed; this is the only part that runs anything, which is why it is here
    and not there (rule 10 -- "It decides; swap_stack.py acts", as that file's own
    header puts it).

    ASKED, NEVER HARDCODED, and this is the same lesson container_verdict() cost.
    A canister id is replica-issued environment state: every fresh `dfx start` on a
    clean volume mints different ids, so an id written into this repository is a
    link to a deployment that no longer exists -- or worse, resolves and shows the
    operator somebody else's canister. fund_desk.py already says this in its own
    refusal ("The canister ids are replica-issued environment state and nothing in
    this checkout can know them"). So the ids come from `dfx canister id <name>`,
    run in the replica container where dfx's own .dfx/local/canister_ids.json is.

    THE NAMES COME FROM stack_authority.CANISTER_SURFACES, which takes them from
    icp/dfx.json -- so a fourth canister appears in the map by being added in one
    place rather than two (rule 8).

    A FAILURE IS A None AND A SENTENCE, NOT AN EXCEPTION AND NOT A GUESS. Docker
    may be absent, the daemon down, the replica not started, the canister not
    deployed; none of those is an error in a report, and all of them are things the
    operator is entitled to be told rather than left to infer from a blank
    (rule 14). The reasons are returned rather than printed so the caller keeps
    control of the order lines appear in.
    """
    lookups = canister_lookup_names()
    found: dict[str, str | None] = {}
    trouble: list[str] = []
    #: How many dfx ANSWERED were not deployed. Counted rather than inferred from
    #: `found`, where a None also covers "could not ask" -- the two must reach the
    #: map distinguishable.
    absent = 0
    ui_id = ""
    read = 0
    for index, name in enumerate(lookups, start=1):
        # THE CANDID UI IS ASKED IN THIS LOOP, NOT BESIDE IT. 4d82caf gave it its
        # own copy of the try/compose/except/verdict block, and the copy is what
        # desynchronized the counter from the work: the loop said `3/3` and then a
        # fourth lookup happened under it. Rule 8 -- two copies of one rule agree
        # on the day they are written. These two lasted one commit.
        is_ui = name == CANDID_UI_CANISTER_NAME
        say(f"                    asking {index}/{len(lookups)} {name}"
            + ("  <- the Candid UI canister itself; every CANDID link is a query against it"
               if is_ui else ""))
        ident, kind, why = _ask_canister_id(name, files)
        if is_ui:
            # `or ""` KEEPS THE DECLARED TYPE HONEST at the one place the two
            # vocabularies meet: `found` holds `str | None` and renders None as
            # "could not be read", while ui_canister_id is a `str` that
            # candid_url() tests for emptiness. Both spellings mean "no id" and
            # each is right for its own reader; converting here rather than
            # letting None travel into a str parameter is what stops the next
            # person discovering the difference the way this commit did.
            ui_id = ident or ""
        else:
            found[name] = ident
        if kind == "found":
            read += 1
            continue
        if kind == "not_deployed" and not is_ui:
            absent += 1
            trouble.append(
                f"{name}: NOT DEPLOYED -- dfx answered: {why}. This is not a failure to "
                "read; the replica does not have this canister"
            )
            continue
        # WHAT IT COSTS, NOT JUST THAT IT FAILED. The UI's consequence is different
        # in kind from a surface canister's: one row goes missing versus EVERY
        # Candid link going missing, and the operator cannot work that out from a
        # bare "could not read".
        trouble.append(
            f"{name}: {why}" + (
                ". The Candid UI canister's own id was NOT read, so no CANDID link is "
                "written -- a link built on a guessed UI id loads the wrong canister's "
                "interface and looks correct doing it" if is_ui else ""
            )
        )
    return CanisterLookups(ids=found, trouble=trouble, absent=absent, ui_id=ui_id,
                           asked=len(lookups), read=read)


def _ask_canister_id(name: str, files: tuple[str, ...]) -> tuple[str | None, str, str]:
    """One `dfx canister id`. Returns (id, kind, why) -- kind is the verdict's own.

    `None` FOR "NO ID", NEVER "". This returned "" on its two own failure paths
    while canister_lookup_verdict() -- whose vocabulary it claims to speak --
    returns None, and canister_surface_lines() asks `if ident is None`. So a
    docker timeout put an EMPTY STRING in the map and the map treated it as a
    successfully read id:

        PAGE   operator_admin       http://.localhost:4943/
        CANDID threshold_custody    http://127.0.0.1:4943/?canisterId=bd3sg-...&id=

    Three malformed URLs printed as working links, which is the precise failure
    the whole canister-id change was written to stop: canister_ids()' own
    docstring says an id nobody read "resolves and shows the operator somebody
    else's canister". Reachable whenever docker is wedged or the replica is slow
    -- _CANISTER_ID_TIMEOUT_SECONDS is 10s per lookup.

    FOUND BY A TYPE CHECKER, 2026-10-09, and that is the argument for f01e85c:
    pyright reported the declared `tuple[str, str, str]` against a `str | None`
    return, and the finding was sitting underneath 53 FALSE import errors in the
    operator's editor. A channel full of noise is a channel nobody reads.

    THE ONE PLACE A LOOKUP IS PERFORMED, extracted when merging the Candid UI's
    duplicate copy back into the loop (rule 8/9: consolidation creates dead code,
    and the cull runs with the merge). The caller decides what a failure MEANS for
    the canister it asked about; this decides nothing beyond what dfx said.

    `kind` is canister_lookup_verdict()'s vocabulary -- found / not_deployed /
    unreachable -- and the two ways to not reach dfx at all are folded into
    `unreachable` with their own `why`, because the operator's next command differs:
    a timeout is docker or the replica not answering, an OSError is docker not being
    there, and a non-zero exit is dfx answering that it cannot tell you.
    """
    try:
        done = compose(
            ["exec", "-T", _DFX_SERVICE, "dfx", "canister", "id", name],
            files, timeout=_CANISTER_ID_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return None, "unreachable", (
            f"`dfx canister id` did not answer within "
            f"{format_duration(_CANISTER_ID_TIMEOUT_SECONDS)}, so NOTHING was read for it"
        )
    except OSError as error:
        return None, "unreachable", f"could not run docker at all -- {type(error).__name__}: {error}"
    # THE LAST LINE OF stdout, NOT ALL OF IT, and the decision is
    # canister_lookup_verdict's -- it separates "not deployed" from "could not
    # ask", which this loop used to merge. That merge is what let three canisters
    # disappear unremarked on the operator's host, 2026-10-08.
    ident, kind, why = canister_lookup_verdict(done.returncode, done.stdout, done.stderr)
    if kind == "unreachable":
        why = (f"`docker compose exec -T {_DFX_SERVICE} dfx canister id {name}` "
               f"exited {done.returncode}: {why}")
    return ident, kind, why


def _say_surface_map(files: tuple[str, ...], serving_port: int) -> None:
    """Print the surface map. Both `up` and `status` end with this.

    ANNOUNCED BEFORE THE WAIT, NOT AFTER IT (rule 14: "Print the target and the
    scale up front... A line that only appears on completion is invisible during
    the wait, which is exactly when it is needed"). Three `docker compose exec`
    lookups at _CANISTER_ID_TIMEOUT_SECONDS each is up to 30s of blinking cursor,
    and on this project a blinking cursor resolves as Ctrl-C.

    THE TROUBLE LINES COME FIRST, before the map they explain. An operator reading
    `id COULD NOT BE READ` three times wants the reason above it, not below it --
    the reason is what they act on and the map is what they were asking for.
    """
    # THE SCALE, AND IT IS THE REAL ONE. This said x3 while asking four, so an
    # operator reading "up to 10.0s each" budgeted thirty seconds for a step that
    # can take forty (rule 14: announce the target and the scale UP FRONT, and the
    # scale has to be the scale). The worst case is spelled out rather than left as
    # multiplication the reader does on a line they are already waiting through.
    asking = canister_lookup_names()
    say(f"  canister ids      `dfx canister id` x{len(asking)} in the `{_DFX_SERVICE}` service, up to")
    say(f"                    {format_duration(_CANISTER_ID_TIMEOUT_SECONDS)} each "
        f"({format_duration(_CANISTER_ID_TIMEOUT_SECONDS * len(asking))} if every one times out)"
        f" -- asked, never hardcoded (ids are per-replica)")
    looked = canister_ids(files)
    for line in looked.trouble:
        say(f"  COULD NOT READ    {line}")
    # ALWAYS A COUNT, AND ALWAYS OVER ITS DENOMINATOR (rule 3). This used to print
    # `read all 3 canister ids` on a clean run and NOTHING at all on a dirty one,
    # so a partial read -- three of four, with the Candid UI missing -- had no
    # number anywhere; the operator had to count trouble lines to work out what
    # succeeded. `read 3/4` says it in the place they are already looking.
    marker = "read" if looked.read == looked.asked else "PARTIAL"
    say(f"  {marker:<16}  {looked.read}/{looked.asked} ids read, "
        f"{', '.join(asking)}")
    say("")
    for line in surface_map(serving_port, looked.ids, looked.absent, looked.ui_id):
        say(line)


def host_workers_running() -> dict[str, int | None]:
    """{name: pid} for every supervisor worker that is alive. THE DECISION `up` refuses on.

    Its own function because it IS a decision -- "may this deployment start" -- and
    rule 10 puts a decision in the smallest testable thing rather than inside
    orchestration. Extracted 2026-10-07 when ruff's PLR0915 said cmd_up had swallowed
    62 statements; rule 12's answer to that complaint is to extract, not to raise the
    ceiling, and what came out was the part that decides.

    Reads supervisor.worker_commands() -- the supervisor's OWN table -- so a fourth
    worker added there is covered without editing this file (rule 8).
    """
    running: dict[str, int | None] = {}
    for name in supervisor.worker_commands():
        state = supervisor.worker_status(name, supervisor.DEFAULT_RUN_DIR)
        if state.get("state") == "running":
            running[name] = state.get("pid")
    return running


#: How long any one read-only `git` call may take before this gives up on it.
#:
#: SHORT BECAUSE IT IS NEVER THE WORK. `up` is about to build images and start
#: containers; a report line about which commit is running is not worth delaying
#: that, and a git call that hangs (a lock held by an editor, a slow filesystem)
#: must degrade to "could not read" rather than to a blinking cursor -- which is
#: rule 14's failure mode and the same trade compose() already makes for the
#: `dfx canister id` lookups.
_GIT_TIMEOUT_SECONDS = 5.0


def _git(*args: str) -> tuple[int, str]:
    """Run one READ-ONLY git command in REPO_ROOT. Returns (returncode, output).

    Every caller below passes a read-only subcommand and there is no path here
    that writes: no fetch, no pull, no gc. That is deliberate and is stated in
    code_version_verdict()'s own comment -- a report must not mutate the checkout
    it is reporting on, and a fetch in front of `up` is a network call that can
    hang before any container starts.

    stderr is folded into the returned text ON FAILURE ONLY, because git puts the
    useful sentence there: `fatal: upstream branch 'refs/heads/...' not stored as
    a remote-tracking branch` is exactly what the operator needs to see, and a
    bare returncode would make this report say "could not read" with no why --
    which is the defect canister_lookup_verdict() was written to stop.
    """
    try:
        done = subprocess.run(  # noqa: S603 -- no shell; argv is this file's own literals
            [_GIT, *args], cwd=REPO_ROOT, capture_output=True, text=True,
            check=False, timeout=_GIT_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return 1, f"git {' '.join(args)} did not answer within {format_duration(_GIT_TIMEOUT_SECONDS)}"
    except OSError as error:  # git vanished between the which() and the run
        return 1, f"git {' '.join(args)} could not be run: {error}"
    if done.returncode != 0:
        return done.returncode, (done.stderr.strip() or done.stdout.strip() or "(no output)")
    return 0, done.stdout.strip()


def _git_local_state() -> tuple[str, str, int, str]:
    """HEAD, branch, and the count of modified TRACKED files. A reason on any failure.

    The half of the reading that needs no remote. Split from the upstream half when
    ruff counted seven returns in one function (PLR0911) -- rule 12's answer to that
    is to extract rather than raise the ceiling, and what came out is the right seam
    anyway: this half answers "which commit, and does the tree match it", the other
    answers "and is that the current one", and each is now exercisable on its own.

    `--untracked-files=no` is deliberate. A tree with `runtime/` logs, a `.db-wal`
    and a scratch script in it is the NORMAL state of a working checkout, and
    counting those would make every run report a modified tree -- a warning that
    fires always is a warning nobody reads.
    """
    if shutil.which(_GIT) is None:
        return "", "", 0, f"{_GIT} is not on PATH, so no commit can be named"
    rc, head = _git("rev-parse", "--short", "HEAD")
    if rc != 0:
        return "", "", 0, f"could not read HEAD: {head}"
    rc_b, branch = _git("rev-parse", "--abbrev-ref", "HEAD")
    named = branch if rc_b == 0 else ""
    rc_s, dirty = _git("status", "--porcelain", "--untracked-files=no")
    if rc_s != 0:
        # NOT "assume clean". A tree whose state could not be read is a tree whose
        # commit id does not describe this output, and saying so is the point.
        return head, named, 0, f"could not read the working tree: {dirty}"
    return head, named, len([line for line in dirty.splitlines() if line.strip()]), ""


def _git_upstream_state(branch: str) -> tuple[str, int, int, str]:
    """The upstream ref and (behind, ahead) against it. A reason on any failure.

    `@{upstream}` rather than a hardcoded `origin/<branch>`: the operator's branch
    and remote are theirs to name, and guessing them is the hardcoded-canister-id
    mistake with different letters. When it is not configured -- which is this
    container's own state, `fatal: upstream branch 'refs/heads/claude/xrp-adapter'
    not stored as a remote-tracking branch` -- git's sentence becomes the reason,
    because "could not read" with no why is the defect canister_lookup_verdict()
    was written to stop.

    NOTHING HERE FETCHES. The counts are against origin as the checkout last saw
    it, which code_version_verdict() states in those words rather than letting a
    reader take a match for a check against GitHub. A fetch would be a network call
    in front of `up` that can hang before any container starts, and a write to the
    refs of the checkout this is only supposed to report on.
    """
    rc_u, upstream = _git("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}")
    if rc_u != 0 or not upstream:
        return "", 0, 0, upstream or f"no upstream is configured for {branch or 'HEAD'}"
    rc_c, counts = _git("rev-list", "--left-right", "--count", f"HEAD...{upstream}")
    if rc_c != 0:
        return "", 0, 0, f"could not count commits against {upstream}: {counts}"
    try:
        ahead, behind = (int(part) for part in counts.split())
    except ValueError as error:
        # `--count` prints exactly two integers separated by a tab, left then right,
        # and HEAD is the left side. Unpacking catches BOTH ways that can fail -- a
        # non-integer and the wrong number of fields -- in one place, which is why
        # this is an unpack rather than a length check and an isdigit() sweep.
        #
        # RETURNING A REASON RATHER THAN 0/0 IS THE POINT. A fabricated "0 behind"
        # reads on screen as "your code is current", which is the exact false
        # all-clear this whole section exists to stop.
        return "", 0, 0, f"could not read `{counts}` as a commit count: {error}"
    return upstream, behind, ahead, ""


def git_reading() -> GitReading:
    """Read which commit this HOST checkout is running. Changes nothing.

    FAILS CLOSED AT EVERY STEP. Any read that does not answer carries its REASON
    into the reading, and code_version_verdict() turns that into `unknown` -- never
    into `current`. That ordering matters more than the happy path: the defect this
    whole section exists for is a report that looked right while being produced by
    old code, and a version check that quietly degrades to "looks fine" reproduces
    it exactly one level up.
    """
    head, branch, modified, reason = _git_local_state()
    if reason:
        return GitReading(head=head, branch=branch, modified=modified, reason=reason)
    upstream, behind, ahead, reason = _git_upstream_state(branch)
    return GitReading(head=head, branch=branch, modified=modified,
                      behind=behind, ahead=ahead, upstream=upstream, reason=reason)


def _say_code_version() -> tuple[str, str]:
    """Print WHICH CODE THIS IS, as the first rows of a run. Returns it for the repeat."""
    status, headline, detail = code_version_verdict(git_reading())
    say(f"  code              {headline}")
    for line in detail:
        say(f"                    {line}")
    return status, headline


def _say_code_version_repeat(status: str, headline: str) -> None:
    """Say it AGAIN at the end, for the two statuses where the output can be wrong.

    THE OPERATOR READS THE BOTTOM. `up` prints sixty-odd lines and the banner is
    gone off the top of a terminal long before the surface map arrives -- which is
    exactly what happened on 2026-10-08: the paste that revealed this defect was
    complete, and a warning in its first rows would still have been twelve screens
    above the wrong URLs it was warning about.

    Rule 14's "make 'did nothing' look different from 'did work'" is the same
    sentence: a run of stale code must not END the way a current one does.
    """
    if status not in VERSION_STATUSES_WORTH_REPEATING:
        return
    say("")
    say(f"  code              {headline}")
    say("                    ^ printed at the TOP of this run too. nothing above was produced")
    say("                    by current code, so check it against origin before acting on it.")


def replica_state(files: tuple[str, ...]) -> tuple[str, str, list[str]]:
    """Ask the LIVE replica container where its dfx state is. Changes nothing.

    Two reads and neither writes: `docker compose ps -q icp-replica` for the
    container id, then `docker inspect` for its mounts.

    THE SERVICE NAME, NOT THE CONTAINER NAME. `docker compose ps -q` resolves
    whatever compose currently calls the container, so a `container_name:` change
    in docker-compose.icp.yml cannot silently make this inspect nothing and report
    `absent` -- which would be the quietest possible false all-clear. Rule 13's
    "prefer a pid file to a `pgrep -f` pattern" is the same instinct: ask the thing
    that owns the name rather than guessing what it looks like today.

    `.Type` AND `.Destination` ONLY. Not `.Name`: the volume's full name is
    `swap_terminal_icp-replica-data` under one project name and something else
    under another, and matching it would report a correctly-mounted replica as
    unprotected the first time anyone ran with COMPOSE_PROJECT_NAME set. What the
    verdict needs is "is anything durable mounted at the dfx path", and Type plus
    Destination answers exactly that.
    """
    try:
        listed = compose(["ps", "-q", _DFX_SERVICE], files=files,
                         timeout=_CANISTER_ID_TIMEOUT_SECONDS)
    except (subprocess.TimeoutExpired, OSError) as error:
        return replica_state_verdict("", "", f"{type(error).__name__} running docker compose ps")
    if listed.returncode != 0:
        return replica_state_verdict(
            "", "", f"docker compose ps exited {listed.returncode}: "
                    f"{listed.stderr.strip() or '(no stderr)'}")
    lines = listed.stdout.strip().splitlines()
    container = lines[0].strip() if lines else ""
    if not container:
        return replica_state_verdict("", "")
    try:
        inspected = subprocess.run(  # noqa: S603 -- no shell; argv is this file's literals plus an id compose printed
            [_DOCKER, "inspect", "--format",
             "{{range .Mounts}}{{.Type}} {{.Destination}}\n{{end}}", container],
            capture_output=True, text=True, check=False,
            timeout=_CANISTER_ID_TIMEOUT_SECONDS,
        )
    except (subprocess.TimeoutExpired, OSError) as error:
        return replica_state_verdict(container, "", f"{type(error).__name__} running docker inspect")
    if inspected.returncode != 0:
        return replica_state_verdict(
            container, "", f"docker inspect exited {inspected.returncode}: "
                           f"{inspected.stderr.strip() or '(no stderr)'}")
    return replica_state_verdict(container, inspected.stdout)


def _say_replica_state(files: tuple[str, ...]) -> None:
    """The pre-flight, printed BEFORE `up` touches an image (rule 14: announce first).

    A warning after the rebuild is a post-mortem. This one has to land while the
    operator can still decide not to run the command, which is the whole difference
    between this and the paragraph in f3a42fe's commit message saying the same
    thing to nobody who was about to act on it.
    """
    say(f"  replica state     is {REPLICA_STATE_PATH} on a mount that outlives a recreate?")
    status, headline, detail = replica_state(files)
    say(f"  {('SAFE' if status == 'on_volume' else status.upper()):<16}  {headline}")
    for line in detail:
        say(f"                    {line}")


#: How long `up` waits for /api/admin/chains. Seconds; it is an argument to urlopen.
#:
#: GENEROUS ON PURPOSE, and it is a ceiling rather than an expectation. The
#: endpoint bounds its own work at 45s (admin_view.CHAIN_PROBE_BUDGET_SECONDS) and
#: answers in 0.19s when the daemons refuse fast, so this only has to be above the
#: endpoint's own budget -- otherwise `up` would report "could not ask" for a probe
#: that was about to answer, which is the false alarm that teaches an operator to
#: skip the line.
_CHAIN_PROBE_FETCH_TIMEOUT_SECONDS = 50.0


def chain_reachability(serving_port: int) -> tuple[str, str, list[str]]:
    """Ask the RUNNING APP which chains it can reach. Changes nothing.

    FROM INSIDE THE CONTAINER, WHICH IS THE ONLY VANTAGE POINT THAT ANSWERS THE
    QUESTION. `up` runs on the host, where the daemons are on 127.0.0.1 and
    perfectly reachable; the container sees them through host.docker.internal
    across a bridge that a firewall can drop and a loopback-only bind can refuse.
    Probing from here would report healthy while every swap froze -- which is
    exactly the state the operator was in on 2026-10-08.

    So this fetches the app's own /api/admin/chains, which probes from where it
    matters. Read-only: the endpoint makes one or two read RPCs per chain and
    signs nothing.
    """
    if not serving_port:
        return chain_reachability_verdict(None, "the page is not serving, so its probe cannot be asked")
    # The scheme is this file's own literal and the host is 127.0.0.1; the only
    # variable is a port this file probed. No `noqa: S310` here: ruff does not
    # raise it for an f-string whose literal prefix IS the scheme, and RUF100
    # caught the suppression I added anyway -- a noqa for a finding that never
    # fires is a claim nobody checked (rule 19).
    url = f"http://127.0.0.1:{serving_port}/api/admin/chains"
    try:
        with urllib.request.urlopen(url, timeout=_CHAIN_PROBE_FETCH_TIMEOUT_SECONDS) as answer:
            body = answer.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as error:
        # NAMED SEPARATELY because this is the shape the defect had: HTTP 500 with
        # an empty body after 60.18s is gunicorn killing the worker, not a chain
        # answering, and an operator shown "could not ask" would go looking at the
        # chains instead of at the timeout.
        return chain_reachability_verdict(None, f"{url} answered HTTP {error.code} -- if that is 500 "
                                                f"with an empty body, the worker was killed mid-probe")
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        return chain_reachability_verdict(None, f"{url}: {type(error).__name__}: {error}")
    try:
        rows = json.loads(body)
    except json.JSONDecodeError as error:
        return chain_reachability_verdict(None, f"{url} returned something that is not JSON: {error}")
    return chain_reachability_verdict(rows)


def _say_chain_reachability(serving_port: int) -> None:
    """Print it. Announced before the wait, because the wait can be real (rule 14)."""
    say("  chain daemons     can the CONTAINER reach them? asked of the app, not of this host --")
    say("                    the daemons are on 127.0.0.1 here and across a bridge from there")
    status, headline, detail = chain_reachability(serving_port)
    say(f"  {('REACHABLE' if status == 'reachable' else status.upper()):<16}  {headline}")
    for line in detail:
        say(f"                    {line}" if line else "")


def _say_page_serving() -> int:
    """Probe the web ports and say which of two things happened. Returns the port, or 0.

    EXTRACTED FROM cmd_up 2026-10-09, when adding the chain-reachability step put
    it at PLR0915 52 statements > 50. Rule 12's answer to that complaint is to
    extract rather than raise the ceiling, and this is the right thing to take
    out: it holds no decision -- probe_serving_port() makes the one decision and
    this only chooses which paragraph to print for it.
    """
    port, detail = probe_serving_port()
    if port:
        say(f"  SERVING           http://127.0.0.1:{port}/ {detail}")
        say(f"                    the swap flow is at http://127.0.0.1:{port}/")
        return port
    say(f"  NOT SERVING       {detail}")
    say("                    The containers may be up and the page is not answering, which is a")
    say("                    DIFFERENT failure from a container that never started -- the container")
    say("                    step above says which. `docker compose logs --tail=40 web` is where")
    say("                    gunicorn says why; an ImportError in a route is the usual cause")
    say("                    after a pull.")
    return 0


def _say_up_banner() -> tuple[str, str]:
    say("swap_stack: UP")
    code = _say_code_version()
    say(f"  database          {Config.DB_PATH}  <- SWAP_DB_PATH")
    say(f"  services          {', '.join(UP_SERVICES)}  <- named, NOT every service in those files:")
    say("                    docker-compose.yml defines `abstergo` and `harness` with no profiles:")
    say("                    gate, so a bare `docker compose up` starts the TEST HARNESS too")
    say("  deployment        CONTAINERIZED. `web` runs gunicorn (gunicorn.conf.py, wsgi:app) AND")
    say("                    the three workers, both inside the container. NO host worker is")
    say("                    started here and `python app.py` is not needed -- that is the Flask")
    say("                    DEVELOPMENT server, which says so itself on every start.")
    say("  about to arm      THE CONTAINER'S PAYOUT WORKER CAN BROADCAST on whatever the")
    say("                    container's environment arms. docker-compose.web.yml passes no")
    say("                    signing material, so unset means it serves and cannot send.")
    return code


def _say_refusal(running: dict[str, int | None]) -> None:
    for name, pid in running.items():
        say(f"  RUNNING           {name} pid={pid}")
    say("")
    say(f"  REFUSED           {len(running)} host worker(s) are running, and `web` starts three more.")
    say("                    That is six workers and TWO payout workers on one database, which")
    say("                    tests/test_payout_concurrency.py measures as 2 sends for 1 deposit.")
    say("                    Nothing was started. Pick ONE deployment:")
    say("                      containerized   cd swap_terminal && python supervisor.py stop")
    say("                                      then this command again")
    say("                      host            leave them running; serve with gunicorn rather")
    say("                                      than app.py:  gunicorn -c gunicorn.conf.py wsgi:app")


def _say_serving() -> None:
    for port in sorted(STACK_PORTS):
        if not port_is_free(port):
            say(f"                    :{port} bound  {STACK_PORTS[port]}")
    say("                    BOUND IS NOT READY for a published container port: docker-proxy binds")
    say("                    it when the container is created, before anything inside listens.")


def _say_icp_note() -> None:
    say("  note on ICP       ICP -> * WORKS HERE as of 9795188: dfx is in the web image and")
    say("                    reaches the replica by url with --identity anonymous, and reads need")
    say("                    no identity -- measured on the operator's replica, `icrc1_fee` with")
    say("                    --identity anonymous returned (10_000 : nat). Anonymous is explicit")
    say("                    because leaving it off made dfx CREATE an identity and print its")
    say("                    seed phrase to the terminal (2026-10-07).")
    say("                    * -> ICP DOES NOT. `transfer` debits the CALLER, so a payout needs")
    say("                    the desk's dfx identity, which exists only inside the replica")
    say("                    container. Placing it is key material and the operator's.")


def cmd_up(files: tuple[str, ...]) -> int:
    """Start the CONTAINERIZED deployment: gunicorn and the workers, inside `web`.

    IT STARTS NO HOST WORKER, and that is the whole shape of this command. The web
    container's entrypoint starts the three workers itself, so a host set alongside
    them is six workers and two payout workers on one database -- which this file
    found on the operator's host and then caused.

    So the two deployments are offered as a CHOICE, not a sum, and `up` performs the
    containerized one because that is the one that answers the operator's actual
    requirement: a page serving after `docker up`, under gunicorn, with nothing held
    in a terminal.
    """
    code = _say_up_banner()
    say("")

    # THE REFUSAL, BEFORE ANY CONTAINER STARTS. Checked rather than assumed, because
    # "the operator probably stopped them" is exactly the reasoning that produced the
    # six-worker state this guards against.
    say("  1. is a HOST deployment already running?")
    running = host_workers_running()
    if running:
        _say_refusal(running)
        return 1
    say("  (none)            no host worker is running, so the container's three are the only ones")
    say("")

    # ASKED BEFORE THE REBUILD, because afterwards the answer cannot help.
    say("  2. can this `up` cost the canisters?")
    _say_replica_state(files)
    say("")

    say("  3. containers")
    done = compose(["up", "-d", *UP_SERVICES], files)
    for line in (done.stderr or done.stdout).strip().splitlines():
        say(f"                    {line}")
    if done.returncode != 0:
        say(f"  FAILED            docker compose up exited {done.returncode}")
        return 1
    say("")

    say("  4. what is serving -- asked, not assumed")
    _say_serving()
    say("")

    say("  5. is the replica ANSWERING? (bound is not ready)")
    ready, detail, waited = wait_for_http(REPLICA_STATUS_URL, _REPLICA_WAIT_SECONDS)
    # format_duration() RATHER THAN A SECOND 1.2096, which is what this line held
    # until 2026-10-08. Rule 6 names the helper for exactly this reason -- "Use it
    # rather than writing 1.2096 again; the constant already appears separately in
    # [two files], which is two too many" -- and microfortnights.py is where the
    # constant and the `2.3µfn (2.8s)` form both live. Identical output, one fewer
    # copy of the conversion.
    shown = format_duration(waited)
    if ready:
        say(f"  READY             {REPLICA_STATUS_URL} {detail} after {shown}")
    else:
        say(f"  NOT READY         {REPLICA_STATUS_URL} {detail}, gave up after {shown}")
        say("                    The containers ARE up; the replica is not serving yet or not at all.")
        say("                    Every ICP call will fail until it is. `docker compose logs icp-replica`")
        say("                    is where dfx says why.")
    say("")

    # STEP 5, AND ITS ABSENCE IS WHY THE OPERATOR SAW A SUCCESSFUL `up` OVER A DEAD
    # PAGE. On 2026-10-07 this block printed READY for the replica, returned 0, and
    # the next command got `Couldn't connect to server` on :5101. The replica was
    # checked; the thing a customer opens was not; and the two outcomes printed the
    # same way. I had named this gap one round earlier and shipped another `up`
    # without it, which is the part this comment exists to record.
    say("  6. is the PAGE serving? (the thing a customer opens)")
    port = _say_page_serving()
    say("")

    # THE OPERATOR ASKED FOR IT IN THESE WORDS ON 2026-10-08: "so we have 3
    # canisters now. 3 different hyperlinks. where's the main landing page for the
    # atm screen?" The page check above answers "is the page up" and gives ONE url;
    # it does not say what that url serves, that `/` is now the ATM landing page
    # (it moved from /atm on 2026-10-07), that /admin exists, or which of the three
    # canisters has a page at all. Four-plus urls were being held in the operator's
    # head because nothing printed them.
    #
    # IT RUNS EVEN WHEN THE PAGE IS DEAD, deliberately. The map is MORE useful then
    # -- it is the list of what would be reachable, and it is printed as paths
    # rather than links so it cannot be read as a claim that anything serves.
    say("  7. can the container reach the chain daemons?")
    _say_chain_reachability(port)
    say("")

    say("  8. WHERE EVERYTHING IS")
    _say_surface_map(files, port)
    say("")
    _say_icp_note()
    _say_code_version_repeat(*code)
    # NOT 0 WHEN THE PAGE IS DEAD. An `up` that exits 0 over an unreachable page is
    # the same defect one layer out: a script reading the exit code, and an operator
    # skimming for errors, both conclude it worked.
    return 0 if port else 1


def build_parser() -> argparse.ArgumentParser:
    """The command line this file accepts. Its own function so a test can ask it.

    EXTRACTED 2026-10-07 BECAUSE THE FLAG SET WAS ONLY REACHABLE BY RUNNING THE
    STACK. `--compose-file` existed and `-f` did not, while the help string read
    "Repeatable, in -f order" -- and no test could catch that, because asserting
    on it meant calling main(), which runs docker. So the only check available
    was reading the help text, which was the thing that was already wrong.

    Rule 10: the decision is "which spellings does this accept", and a decision
    that can only be exercised by starting containers is a decision nobody has
    checked (rule 17).
    """
    # `__doc__ and` BECAUSE -OO STRIPS DOCSTRINGS AND THIS FILE THEN DIED BEFORE IT PARSED.
    # Measured 2026-10-09: `python3 -OO` on this file raises
    #
    #     AttributeError: 'NoneType' object has no attribute 'splitlines'
    #
    # on this line, for every action and for --help -- the tool refuses to start and says
    # nothing about why, which is the shape rule 14 is about. PYTHONOPTIMIZE is set nowhere in
    # this tree today, so this was latent rather than live; -OO or PYTHONOPTIMIZE=2 in a
    # container image or a shell is all it takes, and a launcher that cannot print its own
    # help is a bad place to discover that.
    #
    # market_context_report.py:119 already spells it this way; this is that spelling, not a
    # new one (rule 8). `or ""` would NOT do: "".splitlines() is [] and [0] is an IndexError,
    # one line further on. argparse takes description=None and prints no summary.
    parser = argparse.ArgumentParser(description=__doc__ and __doc__.splitlines()[0])
    parser.add_argument("action", choices=("status", "up", "down"))
    # `-f` AS WELL AS `--compose-file`, AND THE HELP TEXT IS WHY THIS IS A FIX
    # RATHER THAN A CONVENIENCE. Until 2026-10-07 this took only the long form
    # while its own help string read "Repeatable, in -f order" -- naming a flag
    # the parser then rejected. The operator typed `-f` from that sentence and
    # got
    #
    #     swap_stack.py: error: unrecognized arguments: -f docker-compose.yml ...
    #
    # after a `git pull`, so the first thing the new code did was refuse a
    # command this file had told them to write (rule 16: a wrong comment is a bug,
    # and help text is a comment the operator actually reads).
    #
    # `-f` is also the right alias on the merits and not only for the apology:
    # every overlay in this directory is named in `docker compose -f` form in the
    # comments above, in docs/, and in the muscle memory of anybody who has run
    # compose by hand. A tool that wraps compose and refuses compose's own flag
    # makes the operator translate at the one moment they are debugging
    # something else.
    parser.add_argument(
        "-f", "--compose-file", action="append", default=None,
        help=f"override the compose files (default: {', '.join(COMPOSE_FILES)}). Repeatable, in -f order.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    files = tuple(args.compose_file) if args.compose_file else COMPOSE_FILES
    return {"status": cmd_status, "up": cmd_up, "down": cmd_down}[args.action](files)


if __name__ == "__main__":
    raise SystemExit(main())
