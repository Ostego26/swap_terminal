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
