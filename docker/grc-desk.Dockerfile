# The DESK's Gridcoin daemon. The service the whole exercise was asked for.
#
# Role: container image for S1 `grc-desk` in docs/containerization_design_2026_10_03.md
# Reads: its datadir from a volume, including its own wallet.dat and
#        gridcoinresearch.conf
# Writes: that datadir -- chain, wallet, pid file, debug.log
# Can move funds: YES, it holds a wallet. It cannot SPEND without an unlock, and
#        NO WALLET PASSPHRASE EVER ENTERS THIS CONTAINER (design doc S1): the
#        unlock is an RPC call made by whoever holds the passphrase, which is a
#        different process and a different trust boundary.
# Live-safe: TESTNET ONLY as written -- port 25779 and `testnet=1` in the mounted
#        conf. Gridcoin mainnet RPC is 15715 and nothing here reaches it.
#
# THE VOLUME PATH IS THE HIGHEST-STAKES SINGLE STRING IN THE WHOLE PROPOSAL
# (design doc S1, blast radius). A second daemon pointed at the operator's OWN
# datadir is a second daemon on the operator's own wallet -- the exact defect this
# service exists to prevent, arrived at from the other direction. The compose file
# refuses to default it for that reason.
#
# BUILT FROM SOURCE, AND THE COST IS REAL. There is no official Gridcoin image, no
# Debian package, and the project needs Berkeley DB 4.8 for wallet compatibility --
# a newer BDB produces a wallet.dat the host's 5.5.1.0 binary cannot open, which on
# a container holding a wallet is the worst possible kind of "it built fine". So
# BDB 4.8 is compiled first, from the source the project's own build docs name.
#
# MEASURED COST: NOT MEASURED. This image has never been built -- there is no
# Docker daemon in the environment it was written in. Expect tens of minutes and
# over a gigabyte of build context for the toolchain. Said plainly rather than
# estimated into a number that would harden into a fact (rule 3).
#
# AND THE HONEST RECOMMENDATION, because rule 16 says a measured choice goes to the
# operator rather than being taken: the desk daemon ALREADY RUNS ON THE HOST, at
# ~/.GridcoinResearch-desk on port 25779, with its own wallet.dat proven separate
# (validateaddress on a customer address answered ismine:false, 156 addresses
# against 2). It works. This image buys a provable stop and a pinned build, and it
# costs a long compile plus moving a live wallet's datadir into a bind mount whose
# filesystem has not been checked. That is a trade worth making deliberately or not
# at all -- it is NOT owed work, and increment 3 is explicitly third in the staging
# order so that increments 1 and 2 answer "does a bind-mounted datadir work on this
# host" before the question is asked about a wallet.

FROM debian:bookworm-slim AS build

# THE REF DID NOT EXIST, AND THAT IS WORSE THAN THE MOVING TAG IT WAS RECORDED AS.
# This said `ARG GRIDCOIN_REF=5.5.1.7` until 2026-11-21, cloned below with
# `--branch`, and swap_terminal/architecture.py recorded it as `ref_is_pinned=False`
# -- "a tag upstream can move". Measured 2026-11-21 against the real remote, with
# `git ls-remote`, which needs no clone and no credential:
#
#     git ls-remote <repo> 5.5.1.7              -> NO OUTPUT
#     git ls-remote --heads <repo> | grep 5.5.1 -> nothing
#     grep '5\.5\.1\.7' over all 512 tag refs   -> refs/tags/5.5.1.7-testnet ONLY
#
# There is no tag `5.5.1.7` and no branch `5.5.1.7`. `git clone --branch 5.5.1.7`
# fails with "Remote branch 5.5.1.7 not found in upstream origin", so THIS IMAGE
# COULD NOT BE BUILT AT ALL -- not "could be built from a ref that might move". The
# image that holds this project's GRC wallet was unbuildable and nothing said so,
# because nothing in the suite or in CI builds it (there is no docker daemon in the
# environment these files are written in).
#
# `5.5.1.7-testnet` is the ref this was reaching for -- the only published ref whose
# version matches, and the desk container is a testnet desk. It is a LIGHTWEIGHT tag,
# so the SHA below is the commit itself rather than a tag object.
ARG GRIDCOIN_REF=5.5.1.7-testnet

# A TAG IS NOT A PIN, so the tag FETCHES and this SHA is what the build verifies it
# got. Same shape as docker/upstream_binaries.sha256 for the harness tarballs: the
# mutable name is how you reach the bytes, the immutable digest is what you accept.
# A retagged or force-pushed upstream now fails the build instead of silently
# building a different daemon onto a host that holds a wallet.
#
# MEASURED 2026-11-21: refs/tags/5.5.1.7-testnet -> this commit, via git ls-remote.
ARG GRIDCOIN_COMMIT=30a89f2ab0bb4f7134ca7a9c74e233e76130ebd8


# BERKELEY DB'S DIGEST, AND IT DID NOT COME FROM download.oracle.com. That host is
# not reachable from the environment this was written in, so the pin is taken from
# BITCOIN CORE'S OWN depends/packages/bdb.mk, which fetches the identical file from
# the identical path:
#
#     $(package)_download_path=https://download.oracle.com/berkeley-db
#     $(package)_file_name=db-4.8.30.NC.tar.gz
#     $(package)_sha256_hash=12edc0df75bf9abd7f82f821795bcee50f42cb2e5f76a6a281b85732798364ef
#
# That file was read out of bitcoin-28.1.tar.gz, whose own sha256 was confirmed
# against bitcoincore.org's published SHA256SUMS
# (c5ae2dd041c7f9d9b7c722490ba5a9d624f7e9a089c67090615e1ba4ad0883ba). So the chain
# is: a digest-verified Bitcoin Core source release pins this exact filename from
# this exact path. That is a stronger provenance than an unverified fetch and a
# weaker one than reading Oracle's own checksum, and saying which is rule 17's job.

# THE DEPENDENCY LIST IS MEASURED, NOT COPIED FROM UPSTREAM'S install_dependencies.sh.
# Established 2026-11-22 by running `cmake -S <src> -B <build>` natively against the clone
# at this commit and adding packages until it stopped refusing -- so every entry is one
# configure actually demanded. `cmake`, `bison` and `xxd` are the additions over the old
# autotools list; `libboost-iostreams-dev` is the one that is easy to miss, because
# find_package(Boost) SUCCEEDS and then fails on the component (CMakeLists.txt:266).
#
# WHAT WENT AWAY: libtool, autotools-dev and automake. They were here to run ./autogen.sh,
# which does not exist at this commit -- see the build step below.
RUN apt-get update && apt-get install -y --no-install-recommends \
      build-essential cmake pkg-config bison xxd python3 bsdmainutils \
      libevent-dev libboost-dev libboost-system-dev libboost-filesystem-dev \
      libboost-iostreams-dev libboost-test-dev libboost-thread-dev \
      libssl-dev libminiupnpc-dev libzmq3-dev libcurl4-openssl-dev zlib1g-dev \
      ca-certificates curl git \
    && rm -rf /var/lib/apt/lists/*

# zlib1g-dev WAS MISSING FROM THE FIRST VERSION OF THIS LIST, AND THE REASON IS A FLAW IN
# HOW THE LIST WAS DERIVED. It was built by running `cmake -S . -B <dir>` natively and
# adding packages until configure stopped refusing -- which can only ever find what THAT
# machine lacked. The machine already had zlib's headers, so CMakeLists.txt:281
# `find_package(ZLIB REQUIRED)` passed there and failed here, on the first real image
# build, with "Could NOT find ZLIB (missing: ZLIB_LIBRARY ZLIB_INCLUDE_DIR)".
#
# debian:bookworm-slim IS THE ONLY HONEST REFERENCE for this list, which is the same point
# the build step below makes about itself: a native run establishes that the invocation
# compiles, not that the image has what it needs. Measured 2026-11-22 on the first run that
# got this far -- apt layer built, clone verified against GRIDCOIN_COMMIT, configure refused.
#
# AND IT IS NOW DERIVED FROM THE SOURCE RATHER THAN FROM TRIAL AND ERROR, so the next
# failure should not be a missing package. Every unconditional `find_package(... REQUIRED)`
# in CMakeLists.txt was enumerated: Atomics (:198) and Threads (:199) are compiler
# features, Boost (:256) with components, OpenSSL (:272), CURL (:278), ZLIB (:281), Rt
# (:397) which is in libc, and Python3 Interpreter (:715). Every OTHER find_package sits
# behind an option that defaults OFF and is therefore not reached by this build:
# BerkeleyDB (SYSTEM_BDB), leveldb (SYSTEM_LEVELDB), SECP256K1, UNIVALUE, Qt5/Qt6
# (ENABLE_GUI), QRENCODE, CapnProto and Libmultiprocess (ENABLE_MULTIPROCESS).

# =========================================================================
# THE BERKELEY DB 4.8 STAGE IS GONE, AND THAT IS A CORRECTION RATHER THAN A CUT
# =========================================================================
#
# This stage used to download db-4.8.30.NC from download.oracle.com, verify it against a
# digest traced through Bitcoin Core's depends/packages/bdb.mk, patch its atomic.h for
# modern glibc, and compile it to /opt/bdb48 -- several minutes of work whose output this
# commit cannot use.
#
# MEASURED 2026-11-22 in the clone at ARG GRIDCOIN_COMMIT: `src/bdb53/` is VENDORED, 9190
# files with its own CMakeLists.txt and LICENSE, and CMakeLists.txt:148 declares
# `option(SYSTEM_BDB "Find system installation of Berkeley DB CXX 5.3" OFF)`. With it OFF --
# the default, and what this image builds -- src/CMakeLists.txt:48-52 runs
# `add_subdirectory(bdb53)`. The build supplies its own Berkeley DB and never looks for
# /opt/bdb48. Turning SYSTEM_BDB ON would ask for 5.3, not 4.8, so there is no flag that
# makes the old stage useful.
#
# THE CONSEQUENCE IS A WALLET-FORMAT CHANGE AND IT IS NAMED HERE BECAUSE NOTHING ELSE WOULD
# SAY IT: a wallet.dat this image writes is BDB 5.3, which a Berkeley DB 4.8 build cannot
# open. That is a one-way door for any datadir this daemon is pointed at. The 4.8 digest and
# its provenance are not lost -- they are in git history and in
# docs/gridcoin_regtest_2026_11_22.md -- and ARG BDB_VERSION / ARG BDB_SHA256 are removed
# rather than left unused, because an ARG nothing reads is rule 9's dead name with a
# plausible story attached.

WORKDIR /src
RUN set -eux; \
    git clone --depth 1 --branch "${GRIDCOIN_REF}" \
      https://github.com/gridcoin-community/Gridcoin-Research.git .; \
    got="$(git rev-parse HEAD)"; \
    [ "$got" = "${GRIDCOIN_COMMIT}" ] \
      || { echo "REFUSED: ${GRIDCOIN_REF} resolves to $got but ARG GRIDCOIN_COMMIT pins ${GRIDCOIN_COMMIT}. The tag moved, or the ref was changed without changing the commit. This daemon holds a wallet: establish which commit you mean with \`git ls-remote\`, change BOTH args together, and do not delete this check." >&2; exit 1; }; \
    echo "gridcoin source verified: ${GRIDCOIN_REF} = $got"; \
    cmake -S . -B /build \
      -DCMAKE_BUILD_TYPE=Release \
      -DENABLE_GUI=OFF \
      -DENABLE_TESTS=OFF \
      -DENABLE_DAEMON=ON; \
    cmake --build /build -j"$(nproc)"; \
    strip /build/bin/gridcoinresearchd || true

# =========================================================================
# IT USED TO RUN ./autogen.sh AND COULD NOT BUILD AT ALL
# =========================================================================
#
# The three lines above replace `./autogen.sh; ./configure --without-gui ...; make`.
# MEASURED 2026-11-22 in the clone at ARG GRIDCOIN_COMMIT:
#
#     autogen.sh    ABSENT      CMakeLists.txt:1  "# CMake is the official build system
#     configure.ac  ABSENT                         for Gridcoin."
#     configure     ABSENT      build_targets.sh  present
#     Makefile.am   ABSENT      grep -in cmake <this file> -> no match
#
# There is no autotools entry point at this commit, or at 5.5.1.0. So the old step died at
# `./autogen.sh: not found` -- INSIDE `set -eux`, three lines after the commit pin had
# already printed `gridcoin source verified`, and after the apt layer and the whole Berkeley
# DB compile had been paid for. The pin was fine; everything after it was unreachable.
#
# THIS FILE WAS ALREADY CORRECTED ONCE FOR BEING UNBUILDABLE, on 2026-11-21, because the ref
# it named did not exist -- the comment above still says "THIS IMAGE COULD NOT BE BUILT AT
# ALL". It remained unbuildable for a different reason three lines further down, and the
# reason that was not caught is in this file's own header: "nothing in the suite or in CI
# builds it (there is no docker daemon in the environment these files are written in)".
# Fixing the first thing that stops a build tells you nothing about the second.
#
# MOST OF THE OLD FLAGS HAVE NO CMAKE EQUIVALENT TO PORT, which is why three became three
# and not six: ENABLE_GUI, ENABLE_TESTS and ENABLE_DOCS already default OFF
# (CMakeLists.txt:98-100) and ENABLE_DAEMON defaults ON (:97). They are passed explicitly
# anyway, because a default is a thing upstream may change and this image holds a wallet.
# The LDFLAGS/CPPFLAGS pointing at /opt/bdb48 are gone with the stage that built it.
#
# WHAT IS ESTABLISHED AND WHAT IS NOT (rule 16, rule 17). This exact cmake invocation was
# run NATIVELY to completion on 2026-11-22 -- `cmake --build` exited 0 with zero errors and
# produced a binary that reported `Gridcoin Core v5.5.1.7-30a89f2ab0bb`, served RPC on
# regtest and answered every measurement in docs/gridcoin_regtest_2026_11_22.md. It has NOT
# been run as a `docker build`: apt cannot bootstrap through the proxy in the environment
# this file was edited in, which is the same gap that let the autotools breakage survive.
# IT HAS NOW BEEN BUILT AS AN IMAGE, and this paragraph is corrected rather than deleted
# because the distinction it drew is the one worth keeping. Measured on the operator's host,
# 2026-11-22: all 16 steps, `Successfully tagged swap-terminal/grc-desk:local`. The run
# before it failed at `find_package(ZLIB REQUIRED)` and nowhere else, which is what made the
# dep list above derived-from-source rather than guessed. So this build stage is a FIX, not
# a proposal. What the image log confirmed, each of which was a separate claim here:
#
#   the commit pin     printed `gridcoin source verified: 5.5.1.7-testnet = 30a89f2ab0bb...`
#                      and PASSED, rather than merely sitting ahead of a failure
#   the vendored BDB   `Performing configure step for 'BerkeleyDB_Project'` then
#                      `BerkeleyDB_Project build command succeeded` -- the build really does
#                      supply its own Berkeley DB, so dropping the 4.8 stage was correct
#   the output path    `[100%] Linking CXX executable ../bin/gridcoinresearchd`, and
#                      `Step 10/16 : COPY --from=build /build/bin/gridcoinresearchd` succeeded
#   no CLI target      the build completed with ONE binary copied. Had a CLI existed and been
#                      wanted, step 10 would have been two COPYs; had the old second COPY
#                      survived, the build would have failed on a missing file
#   the OFF defaults   "Multiprocess (IPC) build: disabled", secp256k1 static with tests OFF,
#                      no Qt probe at all -- so -DENABLE_GUI=OFF et al landed as intended
#
# WHAT IS STILL UNPROVEN IS DOWNSTREAM OF THE IMAGE, not in it: no container built from this
# image has been STARTED, so the runtime stage's library set, the uid-1000 datadir ownership
# and the CMD are untested. docs/gridcoin_regtest_2026_11_22.md keeps that list.
#
# THE OUTPUT PATH MOVED, which is why the COPY lines below changed: cmake puts the daemon at
# /build/bin/gridcoinresearchd, not /src/src/. And `strip` names ONE binary now, because
# THERE IS NO CLI TARGET -- `cmake --build --target help` offers gridcoinresearchd,
# gridcoin_util and leveldbutil and nothing else. `gridcoin-cli` does not exist at this
# commit, so the old `strip`'s second argument and the second COPY were both naming a file
# the build never produced.

FROM debian:bookworm-slim AS runtime

# THE GRIDCOIN LICENSE TRAVELS WITH THE BINARY, which is MIT's one obligation:
# "The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software." Measured 2026-10-05 and recorded
# in docs/vendoring_upstream_daemon_code.md: Gridcoin's COPYING carries the MIT body
# under NO title and opens with FIVE copyright lines (Black-Coin, NovaCoin, PPCoin,
# Bitcoin, Gridcoin). Copying Bitcoin Core's notice here would drop four holders the
# text requires be carried.
COPY --from=build /src/COPYING /usr/share/licenses/gridcoin/COPYING

# libboost-iostreams1.74.0 WAS MISSING AND THE CONTAINER COULD NOT START.
#
# Measured on the operator's host, 2026-11-22, on the FIRST run of a container from this
# image -- the image having already built clean:
#
#     gridcoinresearchd: error while loading shared libraries:
#     libboost_iostreams.so.1.74.0: cannot open shared object file
#
# repeated once per restart attempt behind `restart: on-failure:5`. The build stage installs
# libboost-iostreams-dev, so the binary links against it; this stage listed system,
# filesystem and thread and not iostreams, so the runtime had a link-time dependency with no
# run-time counterpart. The image was correct and the container was dead, which is why
# "it builds" and "it runs" had to stay separate claims.
#
# THE SET IS NOW TAKEN FROM THE BINARY RATHER THAN FROM THE BUILD STAGE'S -dev LIST.
# `ldd` on a gridcoinresearchd compiled from this same commit reports exactly three boost
# libraries -- filesystem, iostreams and thread -- and notably NOT boost_system, which is
# header-only in modern boost and arrives transitively. Everything else it needs is either
# in the base image or pulled in by libcurl4's own dependencies, which is what installed
# libbrotli1, libkrb5-3, libldap-2.5-0, libsasl2-2, librtmp1, libssh2-1, libnghttp2-14 and
# libpsl5 on the run above without any of them being named here.
#
# THAT ldd WAS RUN ON A DIFFERENT DISTRIBUTION and the distinction matters (rule 17): the
# binary was built natively on Ubuntu with boost 1.83, so what carries over is the SET of
# libraries, not the package names or the soname suffixes. Debian bookworm's are 1.74.0,
# which is why these read 1.74.0. The gnutls-vs-openssl difference in that ldd's curl chain
# is the same artifact and is why no curl dependency is named here beyond libcurl4 itself.
#
# libzmq5 AND libevent ARE KEPT THOUGH THAT ldd DID NOT SHOW THEM, deliberately rather than
# by oversight. Neither appeared in the native binary's link set, so both may be removable;
# establishing that needs an ldd of THIS image's binary, which is one `docker run` away and
# has not been done. Dropping a runtime library on the strength of a different distribution's
# build is the shape of the mistake this comment is about.
RUN apt-get update && apt-get install -y --no-install-recommends \
      dumb-init libevent-2.1-7 libevent-pthreads-2.1-7 libboost-system1.74.0 \
      libboost-filesystem1.74.0 libboost-iostreams1.74.0 libboost-thread1.74.0 \
      libminiupnpc17 libzmq5 libcurl4 openssl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# /build/bin/ AND NOT /src/src/, because cmake writes its output into the build directory
# the step above named. The old path was autotools' in-tree layout.
COPY --from=build /build/bin/gridcoinresearchd /usr/local/bin/gridcoinresearchd

# THERE IS NO SECOND COPY, AND THE MISSING FILE IS gridcoinresearch-cli. This used to copy
# it from /src/src/ and would have failed even with the path corrected: measured 2026-11-22,
# `cmake --build --target help` at ARG GRIDCOIN_COMMIT offers `gridcoinresearchd`,
# `gridcoin_util` and `leveldbutil`, and no CLI target exists. Nothing in this repository
# used it -- every Gridcoin caller in this tree reaches the daemon over JSON-RPC through
# chains/base.RPCAdapter, and `grep -c gridcoinresearch-cli` over the repository finds this
# comment and nothing else -- so its absence costs nothing here. It is called out rather
# than silently dropped because an operator used to `bitcoin-cli` will look for it, and
# because regtest/daemons.ChainConfig has a `cli_path` field that has nothing to point at
# for this chain.

# uid 1000 so a datadir the operator created is writable without a chown that would
# change ownership of a live wallet on the host.
RUN useradd --uid 1000 --create-home --shell /usr/sbin/nologin gridcoin \
    && mkdir -p /data && chown gridcoin:gridcoin /data
USER gridcoin

# 25779 BECAUSE THE TREE ALREADY KNOWS IT. network_target.CHAIN_PORTS["GRC"]
# test_ports is {25715, 25779, 9876}, so nothing in the application has to change
# to reach this daemon -- which is the whole reason 25779 was chosen over any other
# free port (design doc §2.2). The p2p port is NOT configured here: whether this
# daemon needs its own -port is NOT ESTABLISHED (§2.3), and guessing one would
# either collide with the host daemon's 32748 or silently isolate this one.
EXPOSE 25779

VOLUME ["/data"]

# -printtoconsole SO THE LOG IS THE CONTAINER'S LOG. Without it the daemon writes
# debug.log inside the datadir and `docker compose logs` shows nothing at all --
# rule 14's silence, on the process holding a wallet.
#
# NO -daemon FLAG. Forking into the background would make the container's pid 1
# exit immediately and take the daemon with it. The foreground process IS the
# container.
#
# THE REAPER, named here per rule 13: the runtime's stop of pid 1, delivered by
# dumb-init as SIGTERM, which gridcoinresearchd handles by flushing the wallet and
# the block index. The PROOF is not docker's exit code (design doc §7.3 says that
# is unestablished) -- it is reading the pid from /data/testnet/gridcoinresearchd.pid
# BEFORE the stop and polling until it is gone, plus the datadir lock as a backstop:
# a daemon that survived leaves the next start failing loudly.
ENTRYPOINT ["dumb-init", "--"]
CMD ["gridcoinresearchd", "-datadir=/data", "-printtoconsole", "-rpcport=25779"]
