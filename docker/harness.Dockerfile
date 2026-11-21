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

# THE DIGEST PIN, AND IT HAS EXACTLY ONE HOME. docker/upstream_binaries.sha256 is a
# plain `sha256sum -c` manifest -- the format the verifying tool reads natively, so
# this repository writes no parser of its own (rule 8; the same argument
# tests/test_docker_build_context.py's header makes about not reimplementing
# docker's ignore matcher). No digest is restated in this Dockerfile, in
# architecture.py or in a test: architecture.UPSTREAM_BINARIES records the manifest's
# PATH and the tests assert the pointer, never a value.
#
# UNTIL 2026-11-21 THIS STAGE VERIFIED NOTHING. The two curls were followed directly
# by their tars, so the image trusted whatever TLS served -- a weaker claim than the
# Docker Hub image the header above refuses as "an unauditable binary on a machine
# that holds keys", because at least a registry digest is pinnable. The digest check
# is the missing half of that argument, not a replacement for it: release tarballs
# over a distro package and over a third-party image still stands.
COPY docker/upstream_binaries.sha256 /tmp/upstream_binaries.sha256

WORKDIR /tmp/dl
RUN set -eux; \
    arch="$(dpkg --print-architecture)"; \
    case "$arch" in \
      amd64) btc_arch=x86_64-linux-gnu; ltc_arch=x86_64-linux-gnu ;; \
      arm64) btc_arch=aarch64-linux-gnu; ltc_arch=aarch64-linux-gnu ;; \
      *) echo "unsupported architecture $arch -- this image builds for amd64 and arm64" >&2; exit 1 ;; \
    esac; \
    curl -fsSLO "https://bitcoincore.org/bin/bitcoin-core-${BITCOIN_VERSION}/bitcoin-${BITCOIN_VERSION}-${btc_arch}.tar.gz"; \
    curl -fsSLO "https://download.litecoin.org/litecoin-${LITECOIN_VERSION}/linux/litecoin-${LITECOIN_VERSION}-${ltc_arch}.tar.gz"; \
    for want in "bitcoin-${BITCOIN_VERSION}-${btc_arch}.tar.gz" "litecoin-${LITECOIN_VERSION}-${ltc_arch}.tar.gz"; do \
      grep -Eq "^[0-9a-f]{64}  ${want}$" /tmp/upstream_binaries.sha256 \
        || { echo "REFUSED: docker/upstream_binaries.sha256 pins no 64-lowercase-hex digest for ${want}. Verify that tarball against the project's own signed release and add its digest before bumping the pin." >&2; exit 1; }; \
      echo "pinned ok: ${want}"; \
    done; \
    sha256sum --ignore-missing -c /tmp/upstream_binaries.sha256 \
      || { echo "REFUSED: docker/upstream_binaries.sha256 pins no digest for bitcoin-${BITCOIN_VERSION}-${btc_arch}.tar.gz / litecoin-${LITECOIN_VERSION}-${ltc_arch}.tar.gz, or what was served did not match. Verify the tarball against the project's own signed release and add its digest before bumping the pin." >&2; exit 1; }; \
    tar -xzf "bitcoin-${BITCOIN_VERSION}-${btc_arch}.tar.gz"; \
    install -m 0755 "bitcoin-${BITCOIN_VERSION}/bin/bitcoind" "bitcoin-${BITCOIN_VERSION}/bin/bitcoin-cli" /usr/local/bin/; \
    tar -xzf "litecoin-${LITECOIN_VERSION}-${ltc_arch}.tar.gz"; \
    install -m 0755 "litecoin-${LITECOIN_VERSION}/bin/litecoind" "litecoin-${LITECOIN_VERSION}/bin/litecoin-cli" /usr/local/bin/; \
    rm -rf /tmp/dl

# BOTH CURLS MOVED ABOVE BOTH TARS, AND THAT REORDERING IS THE LOAD-BEARING PART.
# A verify placed after an extraction verifies an archive whose contents are already
# on disk and already installed -- it proves nothing about what shipped, while
# looking exactly like a check. The diff that reintroduces that is a one-line move
# that leaves the build green, so
# tests/test_upstream_daemons_are_pinned_and_verified.py compares the character
# offset of the sha256sum against the offset of the first tar and fails on the
# ordering rather than on the presence of the line.
#
# `--ignore-missing` IS REQUIRED AND IS NOT A HOLE. The manifest holds all four
# arch/project filenames and any one build downloads two, so without the flag the
# other two are "FAILED open or read" and every build fails.
#
# THE `for want` LOOP ABOVE IS WHY THAT FLAG IS NO LONGER LOAD-BEARING, and it was
# added 2026-11-21 after the first version of this stage rested the whole
# fail-closed property on a coreutils behavior that cannot be verified from here.
# The reasoning it replaces: "when NOTHING in the manifest matched, sha256sum prints
# 'no file was verified' and exits 1" -- true, MEASURED on GNU coreutils 9.4 in the
# container this was written in, and the flag has existed since 8.25 so it is
# present on bookworm. But debian:bookworm-slim ships 9.1, and 9.1's exit status
# with zero files verified was REASONED from 9.4, never observed: ftp.gnu.org
# returns 403 at this session's proxy and deb.debian.org does not resolve, so
# neither the source nor the package could be read. This file's own standard is that
# "a fail-closed design that has only been reasoned about is a fail-open design
# nobody has met yet", so the unverifiable dependency was removed rather than
# documented.
#
# WHAT THE LOOP ADDS, and it is deliberately additive rather than a replacement: it
# asserts COVERAGE -- that the manifest pins a 64-lowercase-hex line for each of the
# two filenames this build actually downloaded -- and `sha256sum` still does the
# verifying. A version bump without a new digest line is now refused by a `grep`
# whose exit status is the same on every coreutils ever shipped, and the message
# names the file instead of leaving a bare "no file was verified" in a build log.
#
# IT ALSO CLOSES THE SECOND HOLE THE OLD COMMENT NAMED AND TOLERATED: a 63-hex
# digest line is SKIPPED by `sha256sum -c` with a warning, and the run still exits 0
# if any other line verified, so a single typo silently disarmed half the check.
# `^[0-9a-f]{64}  ` does not match a short digest, so the typo is now a refusal.
# Lowercase is required on purpose -- it is what sha256sum writes, and it is what
# test_the_manifest_is_well_formed_and_has_no_duplicate_or_short_digests already
# demands, so the build and the suite agree on one canonical form.
#
# MEASURED 2026-11-21, the loop's own logic run under /usr/bin/dash (bookworm's
# /bin/sh) against the real manifest, SEVEN cases: both real arches pass; a bumped
# BITCOIN_VERSION refuses; a bumped LITECOIN_VERSION refuses after BTC passes; a
# 63-hex typo refuses; an uppercased manifest refuses; an unsupported arch refuses.
#
# STILL NOT MEASURED, and it is the honest limit on all of the above: there is no
# docker daemon in this environment (`docker info` -> "Cannot connect to the Docker
# daemon at unix:///var/run/docker.sock", client 29.9.2), so this stage has never
# been BUILT. The shell was proven; the image was not.
#
# ON A HOST WHERE THESE TWO HOSTS ARE BLOCKED, WHICH IS MOST OF THEM HERE: the
# curls fail and the build stops at this stage with curl's own error. That is the
# correct outcome and there is no fallback to add -- a regtest daemon that could not
# be fetched from the project that built it is not a daemon to substitute for. For
# the record of what this environment can reach: bitcoincore.org and
# download.litecoin.org ARE allow-listed, which is why the digests above could be
# measured at all, while every host the other three chains need (Gridcoin's
# repository, DFINITY's installer, Solana's release index) returns 403 at the proxy.
# So of the five chains this project touches, BTC and LTC are the only two whose
# upstream is reachable from here, and this stage is the only one of the four
# Dockerfiles whose downloads could be verified rather than reasoned about.

COPY docker/licenses/bitcoin-core/COPYING /usr/share/licenses/bitcoin-core/COPYING
COPY docker/licenses/litecoin-core/COPYING /usr/share/licenses/litecoin-core/COPYING
COPY docker/licenses/berkeley-db/MEASURED_LINKAGE.md /usr/share/licenses/berkeley-db/MEASURED_LINKAGE.md
FROM python:3.12-slim-bookworm AS runtime

# dumb-init as PID 1 for the same reason as abstergo: a Python process as pid 1
# gets no default SIGTERM handler, so the outer reaper would wait out the grace
# period and SIGKILL -- which would destroy the harness's finally block and with
# it the inner reaper that does the actual proving.
RUN apt-get update && apt-get install -y --no-install-recommends dumb-init \
    && rm -rf /var/lib/apt/lists/*

COPY --from=chains /usr/local/bin/bitcoind /usr/local/bin/bitcoin-cli /usr/local/bin/
COPY --from=chains /usr/local/bin/litecoind /usr/local/bin/litecoin-cli /usr/local/bin/

# THE LICENSES TRAVEL WITH THE BINARIES, which is MIT's one obligation, and until
# 2026-11-21 THIS IMAGE SHIPPED NEITHER. Measured by extracting both release
# tarballs and listing every file: bitcoin-28.1/ holds README.md, bitcoin.conf,
# bin/, share/man/ and share/rpcauth/ -- NO COPYING, NO LICENSE -- and
# litecoin-0.21.4/ likewise. So the move grc-desk.Dockerfile:90 makes,
# `COPY --from=build /src/COPYING`, is simply not available here: that image builds
# from a clone that carries the file, this one installs a prebuilt binary from a
# tarball that does not. Two images in one docker/ directory, opposite answers to
# one obligation, and the one that was wrong was wrong because of the tarball.
#
# TWO FILES AND NOT ONE, AND THE TIDY-UP IS THE BUG. The bodies are byte-for-byte
# the same MIT grant; only the copyright blocks differ, and Litecoin's carries a
# third holder ("Copyright (c) 2011-2021 The Litecoin Core developers") plus two
# different date ranges. Pointing both COPY lines at one shared file would pass
# every other check and drop a holder the text requires be carried -- which is the
# exact error docs/vendoring_upstream_daemon_code.md measured on Gridcoin.
# tests/..._pinned_and_verified.py asserts the two sources are not byte-identical.
#
# AND THE THIRD FILE IS THE ONE THAT KEEPS THE OTHER TWO HONEST. MEASURED
# 2026-11-21: both of these daemons CONTAIN Berkeley DB 4.8.30, which is Sleepycat
# and is the only license in this image with a source-disclosure condition.
# `strings -a bitcoind | grep -c '^Berkeley DB 4\.8\.30: (April  9, 2010)$'` is 1 for
# bitcoind and 1 for litecoind, 0 for both -cli binaries -- that string is BDB's own
# DB_VERSION_STRING, the library reporting itself, not a message about it. `ldd` on
# either daemon names no libdb at all, because the release builds are static, so an
# audit done with ldd calls these clean and is wrong. Shipping only the two MIT
# notices would therefore be accurate about Bitcoin Core and Litecoin Core and false
# about what is in the binaries. berkeley-db/MEASURED_LINKAGE.md states the finding,
# quotes Sleepycat clause 3, says which parts were measured and which reasoned, and
# leaves the "accompanying software" question to counsel.
#
# WHICH ALSO CORRECTS A FINDING THIS CHANGE WAS WRITTEN FROM: a source-tree audit
# concluded Bitcoin 28.1 is "CLEAN" because `--without-bdb --with-sqlite` builds a
# wallet-capable bitcoind with no BDB. That is true and was proven by a build. It is
# a fact about a build THIS IMAGE DOES NOT PERFORM -- nothing here runs ./configure,
# so no flag can exclude anything, and the artifact installed above is the prebuilt
# one that carries BDB.
#
# WHAT IS EXCLUDED, since BDB cannot be: every binary in those tarballs this image
# does not need. Four of thirteen are installed. bitcoin-wallet and litecoin-wallet
# carry the identical BDB (DB_VERSION_STRING=1, 161 and 162 DbEnv strings), and
# bitcoin-qt would drag Qt's GPL/LGPL into an image with no GUI.
# test_the_image_installs_only_the_binaries_it_declares derives that list from the
# install lines above and fails if a fifth arrives.

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
