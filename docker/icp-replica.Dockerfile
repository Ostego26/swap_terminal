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

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl libunwind8 \
    && rm -rf /var/lib/apt/lists/*

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

# STATE OUTSIDE THE WORKING DIRECTORY, so a `dfx start` survives a rebuild and so
# the repository mount is not written into by the replica. A replica that keeps
# its state in the source tree turns `git status` into a question about consensus.
ENV DFX_CONFIG_ROOT=/state
WORKDIR /repo

# --host 0.0.0.0 because the default binds loopback INSIDE the container, which
# from anywhere else is nothing at all -- the identical mistake that made the web
# UI unreachable earlier in this project (127.0.0.1 in a container is the
# container). --artificial-delay 0 because a local replica's default block pacing
# makes an interactive deploy feel hung, which rule 14 calls a defect.
CMD ["dfx", "start", "--host", "0.0.0.0:4943", "--artificial-delay", "0"]
