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

ARG GRIDCOIN_REF=5.5.1.7
ARG BDB_VERSION=4.8.30.NC

RUN apt-get update && apt-get install -y --no-install-recommends \
      build-essential libtool autotools-dev automake pkg-config bsdmainutils \
      libevent-dev libboost-dev libboost-system-dev libboost-filesystem-dev \
      libboost-test-dev libboost-thread-dev libssl-dev libminiupnpc-dev \
      libzmq3-dev libcurl4-openssl-dev ca-certificates curl git \
    && rm -rf /var/lib/apt/lists/*

# BERKELEY DB 4.8, AND IT IS NOT OPTIONAL FOR A WALLET. The atomic_mutex patch is
# the long-standing fix for building 4.8 against modern glibc, where the bundled
# configure test picks a symbol that no longer exists.
WORKDIR /bdb
RUN set -eux; \
    curl -fsSLO "https://download.oracle.com/berkeley-db/db-${BDB_VERSION}.tar.gz"; \
    tar -xzf "db-${BDB_VERSION}.tar.gz"; \
    sed -i 's/__atomic_compare_exchange/__atomic_compare_exchange_db/' \
      "db-${BDB_VERSION}/src/dbinc/atomic.h"; \
    cd "db-${BDB_VERSION}/build_unix"; \
    ../dist/configure --enable-cxx --disable-shared --with-pic --prefix=/opt/bdb48; \
    make -j"$(nproc)" && make install

WORKDIR /src
RUN set -eux; \
    git clone --depth 1 --branch "${GRIDCOIN_REF}" \
      https://github.com/gridcoin-community/Gridcoin-Research.git .; \
    ./autogen.sh; \
    ./configure \
      --without-gui \
      --disable-tests \
      --disable-bench \
      LDFLAGS="-L/opt/bdb48/lib" \
      CPPFLAGS="-I/opt/bdb48/include"; \
    make -j"$(nproc)"; \
    strip src/gridcoinresearchd src/gridcoinresearch-cli || true

FROM debian:bookworm-slim AS runtime

# THE GRIDCOIN LICENSE TRAVELS WITH THE BINARY, which is MIT's one obligation:
# "The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software." Measured 2026-10-05 and recorded
# in docs/vendoring_upstream_daemon_code.md: Gridcoin's COPYING carries the MIT body
# under NO title and opens with FIVE copyright lines (Black-Coin, NovaCoin, PPCoin,
# Bitcoin, Gridcoin). Copying Bitcoin Core's notice here would drop four holders the
# text requires be carried.
COPY --from=build /src/COPYING /usr/share/licenses/gridcoin/COPYING

RUN apt-get update && apt-get install -y --no-install-recommends \
      dumb-init libevent-2.1-7 libevent-pthreads-2.1-7 libboost-system1.74.0 \
      libboost-filesystem1.74.0 libboost-thread1.74.0 libminiupnpc17 libzmq5 \
      libcurl4 openssl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY --from=build /src/src/gridcoinresearchd /usr/local/bin/gridcoinresearchd
COPY --from=build /src/src/gridcoinresearch-cli /usr/local/bin/gridcoinresearch-cli

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
