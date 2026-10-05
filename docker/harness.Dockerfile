# The regtest BTC and LTC daemons AND the harness that reaps them, in ONE container.
#
# Role: container image for S4 `harness` in docs/containerization_design_2026_10_03.md
# Reads: ST_REGTEST_BTC_* and ST_REGTEST_LTC_* from the environment; each daemon's
#        datadir from a volume
# Writes: the two regtest datadirs on their volume, including pid files
# Can move funds: only regtest coins, which have no value by construction.
#        regtest/daemons.py's assert_regtest() is unconditional, so a datadir that
#        is not regtest refuses rather than proceeds.
# Live-safe: yes, and this is the reason this is increment 2 rather than 4 -- the
#        worst case is worthless coins on a throwaway chain.
#
# ONE CONTAINER FOR THREE PROCESSES, and the reason is the reap, not convenience.
# regtest/daemons.stop_daemon() proves absence with os.kill(pid, 0) against a pid
# read from the daemon's OWN pid file inside the datadir. That requires the harness
# and the daemons to share a PID namespace. Split them and the harness reads a pid
# that means nothing in its namespace, and a stop that cannot prove it worked is
# not a stop (CLAUDE.md rule 13).
#
# TWO REAPERS HERE, AND THAT IS CORRECT RATHER THAN DUPLICATION (design doc S4):
#   inner   stop_daemon() from regtest_htlc_verify.py's own finally block. This is
#           the one that PROVES absence.
#   outer   the container runtime's stop of pid 1, which catches the case the inner
#           one cannot: the harness itself dying before its finally block runs.
# They prove different things, so rule 8 does not ask for them to be merged.

FROM debian:bookworm-slim AS chains

ARG BITCOIN_VERSION=28.1
ARG LITECOIN_VERSION=0.21.4

# BINARIES FROM THE PROJECTS' OWN RELEASE TARBALLS, not a distro package and not a
# third-party image. Neither Bitcoin Core nor Litecoin Core ships in Debian stable,
# and a random Docker Hub image is an unauditable binary on a machine that holds
# keys -- even regtest-only, it shares a daemon with the host.
#
# BOTH VERSIONS ARE NOW MEASURED, AND ONE OF THEM MOVED BECAUSE OF IT. The first
# draft pinned BITCOIN_VERSION=27.1 as "a current release", chosen because nothing
# in this repository records what the host runs -- regtest/daemons.binary_version()
# asks the binary at runtime precisely because the answer was never written down.
#
# Measured on the operator's host 2026-10-05:
#
#     Bitcoin Core version v28.1.0
#     Litecoin Core version v0.21.4
#
# So the Bitcoin pin moves 27.1 -> 28.1 to match, and the Litecoin pin was already
# right. Matching the host matters more than being current: the harness opens the
# same regtest datadirs the host daemons use, and a chainstate written by a newer
# bitcoind is not readable by an older one. A container that downgrades the binary
# over a shared datadir is a corruption waiting for whoever starts second.
#
# These are the first versions this repository has ever recorded.
RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /tmp/dl
RUN set -eux; \
    arch="$(dpkg --print-architecture)"; \
    case "$arch" in \
      amd64) btc_arch=x86_64-linux-gnu; ltc_arch=x86_64-linux-gnu ;; \
      arm64) btc_arch=aarch64-linux-gnu; ltc_arch=aarch64-linux-gnu ;; \
      *) echo "unsupported architecture $arch -- this image builds for amd64 and arm64" >&2; exit 1 ;; \
    esac; \
    curl -fsSLO "https://bitcoincore.org/bin/bitcoin-core-${BITCOIN_VERSION}/bitcoin-${BITCOIN_VERSION}-${btc_arch}.tar.gz"; \
    tar -xzf "bitcoin-${BITCOIN_VERSION}-${btc_arch}.tar.gz"; \
    install -m 0755 "bitcoin-${BITCOIN_VERSION}/bin/bitcoind" "bitcoin-${BITCOIN_VERSION}/bin/bitcoin-cli" /usr/local/bin/; \
    curl -fsSLO "https://download.litecoin.org/litecoin-${LITECOIN_VERSION}/linux/litecoin-${LITECOIN_VERSION}-${ltc_arch}.tar.gz"; \
    tar -xzf "litecoin-${LITECOIN_VERSION}-${ltc_arch}.tar.gz"; \
    install -m 0755 "litecoin-${LITECOIN_VERSION}/bin/litecoind" "litecoin-${LITECOIN_VERSION}/bin/litecoin-cli" /usr/local/bin/; \
    rm -rf /tmp/dl

FROM python:3.12-slim-bookworm AS runtime

# dumb-init as PID 1 for the same reason as abstergo: a Python process as pid 1
# gets no default SIGTERM handler, so the outer reaper would wait out the grace
# period and SIGKILL -- which would destroy the harness's finally block and with
# it the inner reaper that does the actual proving.
RUN apt-get update && apt-get install -y --no-install-recommends dumb-init \
    && rm -rf /var/lib/apt/lists/*

COPY --from=chains /usr/local/bin/bitcoind /usr/local/bin/bitcoin-cli /usr/local/bin/
COPY --from=chains /usr/local/bin/litecoind /usr/local/bin/litecoin-cli /usr/local/bin/

WORKDIR /app
ENV PYTHONUNBUFFERED=1

# PYTHONUNBUFFERED BECAUSE THE HARNESS IS A PROGRESS REPORT. Rule 14: the CLI gate
# opens two URLs per city at timeout=10 and a phase can legitimately work for
# minutes; behind a pipe, Python block-buffers and the operator sees a blinking
# cursor, which is the state they resolve with Ctrl-C.

COPY requirements.txt ./
RUN python3 -m pip install --no-cache-dir -r requirements.txt

COPY swap_terminal ./swap_terminal
COPY regtest_htlc_verify.py grc_htlc_verify.py atomic_swap.py ./

# NON-ROOT. The daemons bind 18443/19443, both above 1024, and write only to their
# datadirs. uid 1000 so a bind-mounted datadir created by the operator is writable.
RUN useradd --uid 1000 --create-home --shell /usr/sbin/nologin harness \
    && chown -R harness:harness /app
USER harness

# DEFAULTS POINT AT THE VOLUME, not at ~ inside the image. daemons.py defaults
# these to ~/regtest/btc and ~/regtest/ltc; inside a container that is the image's
# writable layer, so a chain would be rebuilt from genesis on every restart and the
# pid files would die with the container -- a pid file that cannot outlive the thing
# it names can never prove a stop after a restart.
ENV ST_REGTEST_BTC_DATADIR=/data/btc \
    ST_REGTEST_LTC_DATADIR=/data/ltc

# NOT PUBLISHED BY DEFAULT (see compose). The harness reaches both daemons over the
# container's own loopback because it lives here, so publishing 18443/19443 buys
# nothing and widens the surface. The design doc says the same: "not publishing them
# is strictly safer".
EXPOSE 18443 19443

ENTRYPOINT ["dumb-init", "--"]
# NO DEFAULT RUN OF THE HARNESS. `docker compose up harness` starting an HTLC
# verification on its own would be a container that moves coins because it was
# started, which is the wrong default even on regtest. It idles; the operator runs
# the harness with `docker compose exec`, or overrides the command.
CMD ["python3", "-c", "import time, sys; print('harness container idle. run: docker compose exec harness python3 regtest_htlc_verify.py --help', flush=True); [time.sleep(3600) for _ in iter(int, 1)]"]
