# A LOCAL INTERNET COMPUTER REPLICA, PLUS THE TOOLCHAIN TO BUILD CANISTERS FOR IT.
#
# Role: image (the ICP half of the topology, so it is reproducible rather than a
#       host install nobody can reproduce)
# Reads: icp/ from the mounted repository
# Writes: /state (the replica's own .dfx), a named volume
# Can move funds: NO, and structurally. A local replica has its own genesis and
#       its own threshold keys; the key named `dfx_test_key` exists only inside
#       this container and controls nothing on any real chain. Cycles here are
#       free and worthless by construction.
# Mainnet-safe: it cannot reach mainnet in any sense that matters -- `dfx start`
#       serves a private network. Deploying to mainnet is a different command
#       with a different identity and is deliberately not wired here.
#
# WHY THIS EXISTS RATHER THAN `sh -ci "$(curl ...)"` ON THE HOST. dfx is not
# installed on the operator's machine and the research that scoped this work
# could not install it either, so every statement about a canister so far has
# been "it compiles" rather than "it runs". That gap is the entire reason this
# image exists: the terminal half of the diagram has been reproducible under
# compose since this session started, and the ICP half was a host dependency
# nobody had.
#
# PINNED, NOT LATEST. A replica version is a consensus implementation; "whatever
# dfx installed this morning" is not a thing to debug a signature against.
FROM rust:1.90-bookworm

ARG DFX_VERSION=0.24.3

# python3-minimal IS FOR THE ENTRYPOINT AND IS NOT ASSUMED PRESENT. rust:1.90-bookworm
# carries a rust toolchain and a Debian base; it does NOT promise an interpreter, and
# an ENTRYPOINT that cannot run is a container that never starts -- a worse failure
# than the stale-pid one it exists to fix, and one that would have arrived as "the
# replica is down" with nothing naming the cause.
#
# -minimal rather than python3: this needs a path walk and os.execvp, no stdlib
# extras, and the image is already a full rust toolchain so the marginal size is
# noise either way. `RUN python3 --version` below makes a missing interpreter a RED
# BUILD rather than a container that exits instantly -- the same lesson the web
# image's dfx install learned the hard way on 2026-10-07, where a check placed in the
# wrong stage passed while the runtime failed.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl libunwind8 python3-minimal \
    && rm -rf /var/lib/apt/lists/*
RUN python3 --version

# The wasm target is the one thing a canister build cannot proceed without, and
# installing it in the image rather than at build time means a cold `dfx deploy`
# does not silently reach the network for a toolchain component.
RUN rustup target add wasm32-unknown-unknown

ENV DFXVM_INIT_YES=true
# THIS IMAGE SELECTS NO dfx VERSION EXPLICITLY AND ITS dfx WORKS ANYWAY. I could
# not establish why, and that is written here rather than left as a thing that is
# fine (rule 17: a reason to believe is not the same as having checked).
#
# docker/web.Dockerfile copied this install on 2026-10-07 and the result, measured
# on the operator's host, was:
#
#     error: Unable to determine which dfx version to call. To set a default
#     error: version, run:  dfxvm default <version>
#
# install.sh puts dfxvm -- a version MANAGER plus a shim -- on PATH, and the shim
# refuses rather than guessing when no default is recorded. Here a default evidently
# IS recorded, since this container's `dfx start` runs and answers canister calls.
# The ARG above is in scope for this RUN exactly as it was in the web image, so
# whatever makes the difference is not that.
#
# web.Dockerfile now runs `dfxvm default "${DFX_VERSION}"` explicitly and proves the
# result with `RUN dfx --version`, so a broken install is a red build rather than a
# container that cannot make an ICP call. THE SAME TWO LINES BELONG HERE, and they
# are deliberately not added yet: this image works, that one is unproven until the
# operator's next build, and changing both at once would leave nothing to compare if
# the build fails. Apply it here once web's build is green.
RUN sh -ci "$(curl -fsSL https://internetcomputer.org/install.sh)"
ENV PATH="/root/.local/share/dfx/bin:${PATH}"

# IDENTITIES OUTSIDE THE WORKING DIRECTORY, so the repository mount is not written
# into by the replica. A replica that keeps its keys in the source tree turns
# `git status` into a question about key material.
#
# AND IT DOES *NOT* MAKE A `dfx start` SURVIVE ANYTHING, WHICH IS WHAT THIS COMMENT
# CLAIMED FOR AS LONG AS IT EXISTED. It read "so a `dfx start` survives a rebuild",
# and that sentence was the reason two separate incidents were misdiagnosed:
# 8455339 (a `docker compose down` destroyed the ledger holding 1000 LICP) and
# 1373fa3 (the threshold key changed across one ordinary container recreation,
# 038b01b0... -> 03508d61...). Both were read as surprising. Neither was.
#
# MEASURED INSIDE THE RUNNING CONTAINER ON THE OPERATOR'S HOST, 2026-10-07:
#
#     /state                    40K    .config only  <- the named volume
#     /root/.local/share/dfx    180M   network/local/<hash>/state  <- the replica,
#                                      the canisters, the ledger
#     /repo/.dfx                2.3M   canister ids and wasm copies
#
# DFX_CONFIG_ROOT sets where dfx keeps its CONFIG. It does not move its DATA. So
# the volume has been faithfully persisting forty kilobytes of config while a
# hundred and eighty megabytes of canister state sat on the container's writable
# layer and died with it, every single time.
#
# docker-compose.icp.yml now mounts a second named volume at
# /root/.local/share/dfx, which is the path that actually holds it. The proof is
# 1373fa3's own test, which is the only thing that settles this: call
# threshold_custody.public_key on the same canister id, `down`, `up`, call it
# again, and the hex must match.
ENV DFX_CONFIG_ROOT=/state
WORKDIR /repo

# --host 0.0.0.0 because the default binds loopback INSIDE the container, which
# from anywhere else is nothing at all -- the identical mistake that made the web
# UI unreachable earlier in this project (127.0.0.1 in a container is the
# container). --artificial-delay 0 because a local replica's default block pacing
# makes an interactive deploy feel hung, which rule 14 calls a defect.
# AN ENTRYPOINT THAT CLEARS dfx's STALE PID FILE, AND WHY THE CMD IS UNCHANGED.
#
# Measured on the operator's host 2026-10-07. swap_stack.py's `down` was changed that
# day from `docker compose down` to `stop`, because `down` REMOVES containers and this
# replica's canister state did not survive its container -- the ledger holding 1000
# LICP went with it. Correct fix, and it exposed the opposite failure on the very next
# `up`, when the RESTARTED container logged
#
#     Running dfx start for version 0.24.3
#     Using the default configuration for the local shared network.
#     Error: dfx is already running.
#
# and never listened: sixty refused probes against about eleven seconds for a fresh
# container. So remove-and-recreate loses canister state and stop-and-restart cannot
# start dfx, and the thing between the two verbs is a pid file.
#
# `docker compose rm -sf icp-replica` then `up` reached READY in 11.2s while `stop`
# then `up` never did, and removal differs from stop only in discarding the writable
# layer -- so the file is in the LAYER, which is why DFX_CONFIG_ROOT=/state above does
# not already cover it.
#
# The deletion is safe BY CONSTRUCTION rather than by judgment: an entrypoint runs in a
# container that has just started and holds no other process, so a pid file there
# cannot name a live dfx. docker/icp_replica_entrypoint.py says this at length and
# execs the CMD, so dfx is still pid 1 and `docker compose stop` signals dfx itself.
COPY docker/icp_replica_entrypoint.py /usr/local/bin/icp_replica_entrypoint.py
ENTRYPOINT ["python3", "/usr/local/bin/icp_replica_entrypoint.py"]
CMD ["dfx", "start", "--host", "0.0.0.0:4943", "--artificial-delay", "0"]
