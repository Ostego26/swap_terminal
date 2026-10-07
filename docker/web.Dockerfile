# gunicorn AND the three supervised workers, in ONE container. The database increment.
#
# Role: container image for S5 `web` + `workers` in
#       docs/containerization_design_2026_10_03.md
# Reads: SWAP_DB_PATH and the per-chain RPC credentials from the environment;
#        swap_terminal.db from a volume
# Writes: swap_terminal.db (and its -wal and -shm sidecars) on that volume;
#        pid files, worker logs and kill_switch.lock under another
# Can move funds: YES. payout_worker is one of the three processes this starts, and
#        gunicorn serves the route that creates swaps. This is the armed container
#        if the environment arms it, and arming is the operator's decision
#        (CLAUDE.md rule 16). No credential is baked in.
# Live-safe: NO, not by default. Read §5 of the design doc before running this
#        against a database that holds real swaps, and read the stat -f check in
#        the compose file's comment before mounting one at all.
#
# ONE CONTAINER FOR FOUR PROCESSES, AND THIS IS THE LOAD-BEARING CHOICE (design
# doc §6.1). Splitting the workers out breaks services/kill_switch.py in the
# direction where a stop REPORTS SUCCESS and the payout worker keeps running:
# supervisor.pid_is_still_ours() reads /proc/<pid>/cmdline and
# unaccounted_workers() walks /proc, so both need one PID namespace. A topology
# that makes the payout stop unprovable is not a topology, whatever else it
# improves.
#
# The honest cost, from the same section: one container runs both the web surface
# and the process that signs. That is a WORSE blast radius than four containers
# would give, if four containers worked. They do not, today, without rewriting the
# stop mechanism -- and the right order is to make the stop provable across a
# boundary first, then split. Not split and then discover the stop.

FROM python:3.12-slim-bookworm AS deps

# build-essential IS NEEDED AND THEN THROWN AWAY. Several of the thirteen pinned
# requirements have C extensions (PyNaCl's libsodium binding, pandas) and not every
# platform/version pair has a wheel. Building in a stage that does not ship keeps
# a compiler out of the runtime image, which is the only place it would matter.
RUN apt-get update && apt-get install -y --no-install-recommends \
      build-essential \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /wheels
COPY requirements.txt ./
RUN python3 -m pip wheel --no-cache-dir -w /wheels -r requirements.txt

FROM python:3.12-slim-bookworm AS runtime

RUN apt-get update && apt-get install -y --no-install-recommends dumb-init \
    && rm -rf /var/lib/apt/lists/*

# dfx, SO THIS CONTAINER CAN REACH THE ICP LEDGER AT ALL.
#
# WHY IT IS HERE, measured 2026-10-06/07. Until now this stage installed only
# dumb-init, and no compose file mounts /var/run/docker.sock -- so `docker` was
# absent and chains/icp.py's original transport (`docker compose exec icp-replica
# dfx ...`) could not run. The containerized deployment is the one that serves the
# UI under gunicorn with nothing held in a terminal, which is what the operator
# asked for three times in one sitting; it was also the one deployment where ICP
# did not work.
#
# MOUNTING THE DAEMON SOCKET WOULD HAVE FIXED IT AND IS REFUSED. It hands a process
# holding wallet RPC credentials control of the whole Docker daemon, which is a
# larger grant than the problem. 03d09b2 added the alternative transport instead:
# `dfx canister call --network http://icp-replica:4943`, which needs dfx in THIS
# image and no docker at all. Set ICP_DFX_NETWORK_URL to use it.
#
# WHAT IT CAN AND CANNOT DO, measured on the operator's replica 2026-10-07 and not
# reasoned about. With an anonymous identity:
#
#     dfx --identity anonymous canister call ... icrc1_fee '(record {})'
#     (10_000 : nat)
#
# So READS need no identity, and every method the deposit side calls is a read --
# query_blocks, icrc1_balance_of, icrc1_fee, account_identifier. ICP -> * therefore
# works from this container with nothing moved.
#
# `transfer` DEBITS THE CALLER, so an ICP PAYOUT (* -> ICP) needs the desk's own dfx
# identity, which exists only inside the replica container. NOTHING HERE MOVES IT,
# and nothing here should: that is key material and the operator's (rule 16). No
# identity is created, copied or mounted by this image.
#
# THE INSTALL IS THE SAME ONE docker/icp-replica.Dockerfile ALREADY USES, including
# the version pin, rather than a second method (rule 8). Two ways of installing dfx
# in one repository is two versions in one repository the first time one of them is
# bumped, and "which dfx answered" is not a thing to debug a candid error against.
# DFX_VERSION is an ARG at the same default for the same reason.
#
# libunwind8 and ca-certificates are dfx's, carried over from that file; curl is
# needed by the installer and is NOT removed afterwards only because apt's lists
# are, which is where the size is.
ARG DFX_VERSION=0.24.3
ENV DFXVM_INIT_YES=true

# `dfxvm default` IS EXPLICIT AND THE FIRST ATTEMPT RELIED ON AN ENV VAR INSTEAD.
# Measured on the operator's host 2026-10-07: the image built, dfx was present, and
# calling it said
#
#     error: Unable to determine which dfx version to call. To set a default
#     error: version, run:  dfxvm default <version>
#
# What install.sh puts on PATH is dfxvm -- a version MANAGER and a shim -- and the
# shim refuses rather than guessing when no default is selected. I had assumed
# `ARG DFX_VERSION` would reach the installer and make it select one, because
# docker/icp-replica.Dockerfile does exactly that and its dfx works. Whatever makes
# that true there did not carry here, and the assumption is the defect: rule 17's
# "a reason to believe something is not the same as having checked it."
#
# So the version is selected by a command that names it, and `curl` STAYS -- the
# first attempt purged it, and dfxvm fetches the dfx binary itself, which a removed
# curl may be exactly what blocks.
#
# THE BUILD NOW PROVES IT. `RUN dfx --version` fails the build if dfx is not
# callable, which is rule 13's "verify the artifact, not the deploy" applied one
# stage earlier: this file could not be tested from the container that wrote it
# (internetcomputer.org is unreachable from there), so the build itself is the only
# place the check can live. A broken install is now a red build instead of a working
# container that cannot make an ICP call.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl libunwind8 \
    && sh -ci "$(curl -fsSL https://internetcomputer.org/install.sh)" \
    && /root/.local/share/dfx/bin/dfxvm default "${DFX_VERSION}" \
    && DFX_REAL="$(find /root/.local/share/dfx/versions -type f -name dfx | head -1)" \
    && { test -n "$DFX_REAL" || { echo "FAILED: no versioned dfx binary under /root/.local/share/dfx/versions"; \
         find /root/.local/share/dfx -maxdepth 3 | head -40; exit 1; }; } \
    && install -m 0755 "$DFX_REAL" /usr/local/bin/dfx \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# PYTHONUNBUFFERED=1 because every one of these four processes is a progress report
# behind a pipe. Rule 14: an operator reading `docker compose logs -f` on a
# block-buffered Python process sees a blinking cursor, and the way that resolves is
# Ctrl-C -- here, on a container that signs payouts.
#
# PYTHONDONTWRITEBYTECODE=1 so a read-only or differently-owned /app cannot produce
# __pycache__ churn, and so the image layer stays what was built.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

COPY --from=deps /wheels /wheels
COPY requirements.txt ./
RUN python3 -m pip install --no-cache-dir --no-index --find-links=/wheels -r requirements.txt \
    && rm -rf /wheels

# THE WHOLE PACKAGE, because §3.1 of the design doc is blunt about it: the import
# path IS the directory layout. Every root tool does `sys.path.insert(0, <this
# file's parent>/swap_terminal)`, and the workers are invoked by absolute path from
# supervisor.worker_commands(). Copying a subset would mean editing those paths,
# which is the mass reorganisation rule 10 explicitly refuses on a live system.
COPY swap_terminal ./swap_terminal
COPY wsgi.py gunicorn.conf.py ./
COPY docker/web_workers_entrypoint.py ./docker/web_workers_entrypoint.py

# THE 34 ROOT OPERATOR TOOLS ARE NOT COPIED, and that is deliberate (design doc
# S7). They are things an operator RUNS, not services, they need the database on a
# host path, and several of them move funds. Putting them in the image would make
# `docker compose exec web python3 rescue_payout.py` a thing somebody could reach
# for without the host's environment or its guards. They keep running on the host
# against the same database file, which is what the volume is for.

RUN useradd --uid 1000 --create-home --shell /usr/sbin/nologin swap \
    && chown -R swap:swap /app
USER swap

# THE dfx PROOF IS HERE, AFTER `USER swap`, AND THAT PLACEMENT IS THE WHOLE POINT.
#
# d8d4a6b put `RUN dfx --version` above this line and the build went GREEN while the
# runtime still failed. Measured on the operator's host:
#
#     $ docker compose ... exec -T web dfx --version
#     error: Unable to determine which dfx version to call.
#
# A build-stage RUN executes as ROOT. install.sh had put dfx under
# /root/.local/share/dfx, and `swap` (uid 1000) cannot read root's home -- so the
# dfxvm shim found no recorded version for the user that actually runs gunicorn and
# the workers. My proof proved the wrong thing: it tested the build user, and the
# question is about the runtime user.
#
# TWO CHANGES, and they are two halves of one fix. The install above now copies the
# VERSIONED dfx binary to /usr/local/bin/dfx -- so there is no dfxvm shim and no
# per-user config in the runtime path at all, and nothing depends on a HOME. And the
# proof runs HERE, as `swap`, which is the only user whose answer matters.
#
# The binary's location is DISCOVERED by `find` rather than hardcoded, because the
# layout under .../dfx/versions is not something I established, and the build fails
# with a directory listing if it is not there -- a red build that tells me the layout
# beats a guess that silently installs nothing.
RUN dfx --version

# SWAP_DB_PATH HAS NO DEFAULT HERE ON PURPOSE. config.py falls back to a path under
# its own BASE_DIR (§3.2), which inside this image is the IMAGE -- so an unset
# SWAP_DB_PATH gives a database on the container's writable layer that looks like it
# is working and is discarded on `docker compose down`. The compose file sets it
# explicitly to the volume and says so. Leaving it unset here means the compose file
# is the only place it is decided.

EXPOSE 5000

# NO HEALTHCHECK, AND THE ABSENCE IS REASONED. A GET against the app would be a
# check on gunicorn only, and would report `healthy` while all three workers were
# dead -- which is precisely the "did nothing" rendering as "did work" that rule 13
# names. The entrypoint prints each worker's start outcome and the orphan scan on
# every stop; `docker compose exec web python3 swap_terminal/supervisor.py status`
# is the real answer and it is one command. A green healthcheck that can be wrong
# about the payout worker is worse than none.

ENTRYPOINT ["dumb-init", "--"]
CMD ["python3", "docker/web_workers_entrypoint.py"]
