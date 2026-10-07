#!/usr/bin/env python3
"""Clear dfx's stale pid files, then exec `dfx start`. One job, done before the replica.

Role: file (the replica container's entrypoint)
Reads: the filesystem under the roots in PID_FILE_ROOTS, looking for dfx pid files
Writes: DELETES the pid files it finds, and nothing else. It creates no file, touches
        no canister state, and never writes a key.
Can move funds: no. It runs before any replica exists and issues no call.
Live-safe: it only ever runs inside the icp-replica container, which holds a LOCAL
        replica whose token is LICP and is not ICP.

WHY THIS EXISTS, measured on the operator's host 2026-10-07.

`swap_stack.py down` was changed that day to run `docker compose stop` rather than
`down`, because `down` REMOVES containers and the replica's canister state did not
survive its container -- the ICP ledger holding 1000 LICP went with it. That fix was
right and it exposed the opposite failure immediately: on the next `up`, the restarted
container logged

    Running dfx start for version 0.24.3
    Using the default configuration for the local shared network.
    Error: dfx is already running.

and the replica never listened. Sixty refused probes where a fresh container takes
about eleven seconds.

So `down`-and-remove loses canister state, and `stop`-and-restart leaves dfx unable to
start. Neither verb is right on its own, and the thing standing between them is a pid
file.

WHAT MAKES THE DELETION SAFE, AND IT IS BY CONSTRUCTION RATHER THAN BY JUDGMENT. This
script runs as the container's entrypoint, so it runs in a container that has just
started and contains no processes but this one. A dfx pid file present at that moment
CANNOT name a live dfx: there is nothing alive to name. That is why this does not
check whether the pid is running, or wait, or ask -- the question is already answered
by where the code runs.

It was also measured rather than assumed that removal is what clears it: `docker
compose rm -sf icp-replica` followed by `up` reached READY in 11.2s, while `stop`
followed by `up` never came up. Removal differs from stop only in discarding the
container's writable layer, so the file is in the layer and not in /state -- which is
why DFX_CONFIG_ROOT=/state does not already cover it.

WHY A PYTHON ENTRYPOINT FOR A FILE DELETION. The finding is a DECISION -- which paths
count as a dfx pid file -- and rule 10 puts a decision in the smallest testable thing.
stale_pid_files() takes its root as an argument, so tests/test_icp_replica_entrypoint.py
runs it against a fake tree with no container and no dfx. A shell one-liner in the
Dockerfile would be shorter and would be checkable only by starting a replica.

It execs rather than spawns, so dfx is pid 1 and `docker compose stop` signals dfx
itself -- the same reason docker/web_workers_entrypoint.py explains at length for
gunicorn.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

#: Where dfx may have left a pid file, in the order they are searched.
#:
#: BOTH, because which one dfx uses is not something this file should claim to know.
#: DFX_CONFIG_ROOT is /state in this image (docker/icp-replica.Dockerfile) and the
#: measurement says the file that blocked startup was NOT there -- removal of the
#: container cleared it and a named volume survives removal. /root is where dfx's
#: own default would put it. Searching both costs one directory walk at container
#: start and removes the need to be right about dfx's internals.
PID_FILE_ROOTS = ("/state", "/root/.local/share/dfx", "/repo/.dfx")

#: The filenames dfx uses to record a running replica. `pid` is the one that produced
#: "dfx is already running"; the others are recorded in the same directories and are
#: equally meaningless in a container that has just started.
PID_FILE_NAMES = ("pid", "replica.pid", "icx-proxy.pid", "pocket-ic-pid", "replica-configuration")


def stale_pid_files(root: Path) -> list[Path]:
    """Every dfx pid file under `root`. PURE in the sense that matters: no deletion.

    Returns a list so the caller can PRINT what it is about to remove before removing
    it (rule 14) -- a container that silently deletes files at startup is a container
    whose startup nobody can read.

    A root that does not exist yields nothing rather than raising: on a first run the
    volume is empty and /repo/.dfx may not exist at all, and that is the ordinary
    case, not an error.
    """
    if not root.exists():
        return []
    found = []
    for name in PID_FILE_NAMES:
        found.extend(path for path in root.rglob(name) if path.is_file())
    return sorted(set(found))


def clear_stale_pid_files(roots=PID_FILE_ROOTS) -> int:
    """Delete them, saying what was deleted. Returns how many."""
    removed = 0
    for root in roots:
        for path in stale_pid_files(Path(root)):
            try:
                path.unlink()
            except OSError as error:
                # SAID, NOT SWALLOWED. If the file cannot be removed, dfx will refuse
                # to start and the operator needs the reason here rather than in a
                # sixty-attempt probe loop afterwards.
                print(f"icp-replica entrypoint: COULD NOT REMOVE {path}: {error}", flush=True)
                continue
            print(f"icp-replica entrypoint: removed stale {path}", flush=True)
            removed += 1
    if removed == 0:
        # (none) IS A RESULT (rule 14). A silent startup leaves a reader unable to tell
        # "nothing to clear" from "this script did not run".
        print("icp-replica entrypoint: (none) no stale dfx pid file to clear", flush=True)
    return removed


def main(argv: list[str]) -> int:
    if not argv:
        print("icp-replica entrypoint: REFUSED, no command given to exec", flush=True)
        return 2
    clear_stale_pid_files()
    print(f"icp-replica entrypoint: exec {' '.join(argv)}", flush=True)
    os.execvp(argv[0], argv)  # noqa: S606 -- checked: argv is the image's own CMD, not input
    return 0  # unreachable; execvp replaces this process


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
