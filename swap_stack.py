#!/usr/bin/env python3
"""One command for the whole swap terminal: containers, workers and servers together.

Role: file (entry point -- what an operator, a launcher or a cron entry names)
Reads: docker compose's own `ps`, /proc for listeners, the supervisor's run
       directory and pid files, and Config for the database path it echoes
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
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "swap_terminal"))

import supervisor  # noqa: E402  -- after the sys.path.insert above, same as every root tool
from config import Config  # noqa: E402

from swap_terminal.stack_authority import (  # noqa: E402
    STACK_PORTS,
    container_id,
    listening_inodes,
    pids_owning_inodes,
    port_is_free,
    stray_verdict,
)

#: The compose files this stack is assembled from, in the order `-f` wants them.
#:
#: HARDCODED RATHER THAN GLOBBED, and the glob is the bug it avoids: the directory
#: also holds docker-compose.web.armed-sol.yml, .armed-xrp.yml and .hostnet.yml,
#: which are ALTERNATIVE overlays. A glob would pass all of them to one command and
#: the last one silently wins, so `up` would arm SOL or XRP because of a filename's
#: sort order. An operator wanting an armed overlay names it with --compose-file.
COMPOSE_FILES = ("docker-compose.yml", "docker-compose.icp.yml", "docker-compose.web.yml")


def say(line: str) -> None:
    """Print immediately. Rule 14: silence is indistinguishable from hung."""
    print(line, flush=True)


def compose(args: list[str], files: tuple[str, ...], check: bool = False) -> subprocess.CompletedProcess:
    """Run one `docker compose` command with this stack's files, from the repo root.

    `cwd=REPO_ROOT` is not decoration. Compose resolves the relative paths INSIDE
    those files -- `build: context: .`, the `./icp:/repo` mount -- against the
    process's working directory, and the same omission in chains/icp.py broke every
    ICP call from the app earlier today (see that file's _REPO_ROOT comment). One
    measurement, two files, same fix.
    """
    argv = ["docker", "compose"]
    for name in files:
        argv += ["-f", str(REPO_ROOT / name)]
    argv += args
    return subprocess.run(  # noqa: S603 -- no shell; argv is this file's own list plus a subcommand
        argv, cwd=REPO_ROOT, capture_output=True, text=True, check=check,
    )


def report_listeners() -> list[dict]:
    """Every process LISTENing on one of STACK_PORTS, classified. Reads, changes nothing."""
    try:
        proc_net = Path("/proc/net/tcp").read_text()
    except OSError as error:
        say(f"  listeners         CANNOT BE READ: {error}. The port half of this report is MISSING,")
        say("                    which is not the same as 'nothing is listening'")
        return []
    inodes = listening_inodes(proc_net, set(STACK_PORTS))
    owners = pids_owning_inodes(set(inodes.values()), Path("/proc"))
    found = []
    for port, inode in sorted(inodes.items()):
        pid = owners.get(inode)
        if pid is None:
            found.append({"port": port, "pid": None, "verdict": "owner unknown", "cmdline": ""})
            continue
        try:
            cmdline = Path(f"/proc/{pid}/cmdline").read_text()
        except OSError:
            cmdline = ""
        verdict, reason = stray_verdict(cmdline, REPO_ROOT)
        # A CONTAINERIZED LISTENER OVERRIDES THE CMDLINE VERDICT, because a host path
        # can never appear in its argv -- docker/web.Dockerfile puts this repository
        # at /app, so stray_verdict()'s repo_root substring test cannot ever pass for
        # it and it reads FOREIGN. Measured on the live host: the gunicorn on 5101 is
        # THIS project's, in a container `docker compose ps` does not list, and the
        # lever is `docker stop <id>` rather than a pid. See container_id().
        try:
            inside = container_id(Path(f"/proc/{pid}/cgroup").read_text())
        except OSError:
            inside = None
        if inside and verdict != "refused":
            verdict = "container"
            reason = (
                f"in container {inside}, which `docker compose ps` above does not list -- so it "
                f"belongs to no compose project this stack names. Stop it with `docker stop {inside}`, "
                f"after `docker inspect {inside}` names it"
            )
        found.append({
            "port": port, "pid": pid, "verdict": verdict, "reason": reason,
            "cmdline": cmdline.replace("\0", " ").strip(), "container": inside,
        })
    return found


def print_listeners(found: list[dict]) -> None:
    say("  http listeners    what is answering on this stack's ports")
    if not found:
        say("  (none)            nothing is LISTENing on any of "
            f"{', '.join(str(p) for p in sorted(STACK_PORTS))} <- a result, not a blank")
        return
    for row in found:
        label = {
            "ours": "OURS", "foreign": "FOREIGN", "refused": "CHAIN DAEMON",
            "container": "IN A CONTAINER", "owner unknown": "UNKNOWN",
        }[row["verdict"]]
        say(f"  {label:<17} :{row['port']} pid={row['pid']}  {STACK_PORTS[row['port']]}")
        if row.get("reason"):
            say(f"                    {row['reason']}")
        if row["cmdline"]:
            say(f"                    {row['cmdline'][:150]}")


def cmd_status(files: tuple[str, ...]) -> int:
    """Everything that is running, from all three reapers, in one place."""
    say("swap_stack: STATUS")
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
    print_listeners(report_listeners())
    say("")
    say("  workers           supervisor.py owns these; this is its own report")
    supervisor.main(["status", "--run-dir", str(supervisor.DEFAULT_RUN_DIR)])
    return 0


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
    say("  never stopped     chain daemons, and a FOREIGN listener on one of our ports. Both are")
    say("                    reported below rather than passed over in silence")
    say("")
    say("  1. workers")
    supervisor.main(["stop", "--run-dir", str(supervisor.DEFAULT_RUN_DIR)])
    say("")
    say("  2. containers")
    done = compose(["down"], files)
    for line in (done.stderr or done.stdout).strip().splitlines():
        say(f"                    {line}")
    if done.returncode != 0:
        say(f"  FAILED            docker compose down exited {done.returncode}")
    say("")
    say("  3. proof -- absence is the assertion, not an exit code (rule 13)")
    remaining = report_listeners()
    still = [row for row in remaining if row["verdict"] == "ours"]
    for port in sorted(STACK_PORTS):
        free = port_is_free(port)
        say(f"                    :{port} {'free' if free else 'STILL BOUND'}  <- bind attempted, not inferred")
    print_listeners(remaining)
    if still:
        say("")
        say(f"  NOT DOWN          {len(still)} listener(s) this project owns are still up and are named above.")
        say("                    They were started by something neither the supervisor nor compose names --")
        say("                    `swap_stack.py status` after killing them by pid is how to confirm.")
        return 1
    say("")
    say("  summary           every process this file owns is gone, proven by bind. Chain daemons")
    say("                    and any FOREIGN listener are untouched and listed above.")
    return 0


def cmd_up(files: tuple[str, ...]) -> int:
    """Containers first, then workers. The reverse of `down`, for the same reason."""
    say("swap_stack: UP")
    say(f"  database          {Config.DB_PATH}  <- SWAP_DB_PATH")
    say("  about to spawn    A PAYOUT WORKER THAT CAN BROADCAST, on every chain this environment arms.")
    say("                    Stop now if this database is pointed at a funded mainnet wallet.")
    say("  order             containers (so a worker's first cycle finds its replica up) -> workers")
    say("")
    say("  1. containers")
    done = compose(["up", "-d"], files)
    for line in (done.stderr or done.stdout).strip().splitlines():
        say(f"                    {line}")
    if done.returncode != 0:
        say(f"  FAILED            docker compose up exited {done.returncode}; NOT starting workers")
        return 1
    say("")
    say("  2. workers")
    supervisor.main(["start", "--run-dir", str(supervisor.DEFAULT_RUN_DIR)])
    say("")
    say("  note              the customer UI is NOT started here. It is `python app.py` from")
    say("                    swap_terminal/, or the `web` compose service -- and the compose one")
    say("                    cannot make an ICP call (no docker CLI in that image; see")
    say("                    chains/icp.py). `status` lists whichever is up.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("action", choices=("status", "up", "down"))
    parser.add_argument(
        "--compose-file", action="append", default=None,
        help=f"override the compose files (default: {', '.join(COMPOSE_FILES)}). Repeatable, in -f order.",
    )
    args = parser.parse_args(argv)
    files = tuple(args.compose_file) if args.compose_file else COMPOSE_FILES
    return {"status": cmd_status, "up": cmd_up, "down": cmd_down}[args.action](files)


if __name__ == "__main__":
    raise SystemExit(main())
