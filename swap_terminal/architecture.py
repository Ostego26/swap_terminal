"""What this system IS: which containers exist, what runs in them, and what signs for each chain.

Role: submodule (declarations only -- every name here is data, no function in this
      file touches the filesystem, the network, a database or a chain)
Reads: nothing at import. The tables below are declarations; the TEST is what reads
       the compose files, the adapters and the credential modules and asserts they
       agree.
Writes: nothing.
Can move funds: no. Nothing here is consulted by any payout, quote or deposit path --
       by design, see WHAT THIS FILE IS NOT below.
Mainnet-safe: yes to import and yes to read. It names credential VARIABLES and never
       a credential.

=============================================================================
WHY THIS FILE EXISTS. Operator, 2026-10-11: "do you need to fucking write that
shit down about docker because this is like the 2nd or 3rd time you've fucked up
our architecture."
=============================================================================

They were right, and it was four times in one session, every one the same shape: the
architecture was reconstructed from memory because no single place states it. The four:

  1. `${VAR:?}` versus `${VAR:-}` -- compose interpolates EVERY variable in EVERY file
     it reads BEFORE selecting a service, so one bad `:?` breaks every command in the
     project, including commands for unrelated services.
  2. The ICP payout identity was looked for at a host path. It lives on the `icp-state`
     named volume, which is why docker-compose.web.armed-icp.yml mounts the volume
     rather than a file.
  3. The operator was told to set ICP_DFX_NETWORK_URL, which docker-compose.web.yml
     already defaults.
  4. Answering "what does `docker compose up` actually start" required reading
     `include:` and `profiles:` from scratch, mid-conversation.

AND WRITING IT DOWN IN PROSE WOULD HAVE BEEN THE SAME BUG WITH A LONGER FUSE. The
same session fixed, in compose files and module headers that nobody had re-run:
`stop_grace_period`'s justifying comment, off by a factor of three on its own
arithmetic; "exactly two variables carry a default" where six do; "SOL is NOT in
Config.ALLOWED_PAIRS" where it has been a destination on five pairs since 2026-10-06;
"no payout has ever completed for ICP" hours after the call site was fixed; and
"COMPOSE_FILE lives in a file this repository must never read or write" after
configure_env.py started writing it. Prose about structure goes stale silently, and
every one of those sentences was written by someone who had just measured it.

So the tables below are DATA, and
tests/test_architecture_is_what_the_tree_does.py derives the same facts from the
compose files and the Python modules and fails on any disagreement. A reader can
trust this file exactly as far as that test runs, which is the only kind of trust
a document about structure can earn.

=============================================================================
WHAT THIS FILE IS NOT
=============================================================================

IT IS NOT CONSULTED BY ANYTHING THAT RUNS. No route, service, worker or adapter
imports it, and none should. It is a description, and a description that starts
deciding becomes a second authority to disagree with the first -- which is CLAUDE.md
rule 15's whole subject. chains/registry.py decides which adapters exist;
Config.ALLOWED_PAIRS decides which pairs are offered; the compose files decide what
starts. This file asserts that it has read them correctly.

IT IS ALSO NOT THE KEYRING'S DOCUMENTATION. icp/README.md and
icp/threshold_custody/threshold_custody.did own that, and copying their content here
would be rule 8's two-copies-of-one-rule. KEYRING below records only the shape and
points at them.

=============================================================================
THE TOPOLOGY, in the operator's own words (2026-10-11)
=============================================================================

    "docker is the container, and then icp canisters hold the keyring for all
     applicable chains, not all apply, like sol, or xrp, but where this gives us
     capability for expansion of other swaps."

Asked which sense of "applicable" that was, they chose: the keyring holds all five
external chains -- BTC, LTC and GRC through threshold ECDSA (secp256k1), SOL and XRP
through threshold Schnorr (Ed25519) -- and what does NOT apply to SOL and XRP is
consensus-backed CHAIN STATE, not custody. Reading those chains stays an
RPC-provider-trust leg; holding their keys does not. icp/README.md's research section
had this right and an earlier reading of the instruction had it wrong.

Which makes the last clause exact: a new chain gets custody for free by adding a
derivation path under an existing curve. There is no per-chain custody design to
invent, only a path and the chain's own transaction format.

=============================================================================
THE THREE LAYERS, AND WHAT IS TRUE OF EACH TODAY
=============================================================================

  docker      the container layer. SERVICES below, plus -- since 2026-11-21 --
              UPSTREAM_BINARIES and UPSTREAM_SOURCE, which record where the chain
              daemons inside those containers COME FROM and what proves the bytes.
              A reader who opens this module for the topology now finds the
              provenance in the same place, because "which project built the
              bitcoind in the harness, and what verified it" had no home at all
              before that date and was therefore reconstructed from a Dockerfile
              comment every time it was asked. This is the layer that was
              repeatedly got wrong and is the one the test checks hardest.
  custody     CUSTODY below. TODAY every chain signs from its own credential and
              NOTHING routes through a canister: `threshold_custody`, `keyring` and
              `derivation_path` appear ZERO times in routes/, services/ and
              workers/, measured 2026-10-11.
  keyring     KEYRING below. The canisters exist, name their curves and derivation
              paths, and CANNOT SIGN -- which is increment 1b's whole safety
              argument and is pinned by tests/test_icp_canister_cannot_sign.py.

So custody and keyring are two layers that do not yet meet, and that gap is the
project rather than a defect. Writing it as one layer would be the thing this file
exists to stop.
"""

from __future__ import annotations

from typing import NamedTuple

# ---------------------------------------------------------------------------
# LAYER 1: DOCKER
# ---------------------------------------------------------------------------


class Service(NamedTuple):
    """One compose service, and whether a bare `docker compose up` starts it."""

    #: The compose file that DECLARES it -- the one carrying its `build:`. The armed
    #: overlays and hostnet are not here: they patch `web` rather than declaring a
    #: service, which OVERLAYS records.
    declared_in: str
    #: The `profiles:` entry gating it, or "" for none. A profile means an explicit
    #: --profile is required, which is how a service opts OUT of a bare `up`.
    profile: str
    #: Whether a bare `docker compose up` (no -f, no --profile) starts it. DERIVED,
    #: and the test derives it the same way: reachable from docker-compose.yml through
    #: `include:` AND carrying no profile.
    starts_by_default: bool
    #: What pid 1 runs, and everything it spawns, ONE ENTRY PER PROCESS GROUP. The
    #: `web` entry is the one that matters, and the number of entries is NOT the number
    #: of processes: its gunicorn entry covers a master plus GUNICORN_WORKERS children.
    #:
    #: THIS COMMENT SAID "four processes, not one" FOR ONE COMMIT, against a six-entry
    #: tuple describing eight processes. In the file written because the architecture
    #: had been got wrong four times, carrying a count in prose beside the data that
    #: contradicts it -- which is rule 16's wrong-comment bug and is exactly the thing
    #: this module exists to stop, committed by this module. It is recorded rather than
    #: quietly corrected because the lesson is the mechanism, not the number: a count
    #: belongs in a test, and WEB_PROCESS_COUNT below is now derived from the
    #: Dockerfile, gunicorn.conf.py and supervisor.worker_commands() instead of typed
    #: here.
    processes: tuple[str, ...]
    #: One line on what it is for.
    role: str


SERVICES = {
    "web": Service(
        declared_in="docker-compose.web.yml",
        profile="",
        starts_by_default=True,
        processes=(
            "dumb-init (pid 1, no --single-child -- so it signals the whole group)",
            "docker/web_workers_entrypoint.py (pid 1's child; starts and reaps the rest)",
            "gunicorn -c gunicorn.conf.py wsgi:app (2 sync workers)",
            "workers/deposit_watcher.py",
            "workers/payout_worker.py",
            "workers/reconcile_worker.py",
        ),
        role="the Flask UI and the three supervised workers, in ONE container because "
             "supervisor.unaccounted_workers() walks /proc",
    ),
    "icp-replica": Service(
        declared_in="docker-compose.icp.yml",
        profile="",
        starts_by_default=True,
        processes=("dfx start --host 0.0.0.0:4943 --artificial-delay 0",),
        role="the local ICP replica. ICP has no testnet, so this is the testnet in the "
             "only sense ICP has one -- see icp/README.md",
    ),
    "abstergo": Service(
        declared_in="docker-compose.yml",
        profile="abstergo",
        starts_by_default=False,
        processes=("node server.js (the Express bridge)",),
        role="the Gridcoin -> Solana bridge in grc-sol-swap/. Opt-in: it keeps its swap "
             "intents in swap_intents.json, which is CLAUDE.md rule 5's worst item",
    ),
    "harness": Service(
        declared_in="docker-compose.yml",
        profile="harness",
        starts_by_default=False,
        processes=("an idle sleep; regtest bitcoind/litecoind are started INSIDE it on demand",),
        role="the regtest harness. Opt-in since 2026-10-10, because it brings up regtest "
             "daemons on a host whose other daemons hold funded testnet wallets",
    ),
    "grc-desk": Service(
        declared_in="docker-compose.grc-desk.yml",
        profile="",
        starts_by_default=False,
        processes=("gridcoinresearchd",),
        role="the desk's Gridcoin daemon. NOT in docker-compose.yml's include:, so it "
             "needs an explicit -f. Never built or run anywhere yet",
    ),
}

#: How many PROCESSES run inside the `web` container, as opposed to how many entries
#: SERVICES["web"].processes has. Eight:
#:
#:     1   dumb-init                     docker/web.Dockerfile ENTRYPOINT
#:     1   web_workers_entrypoint.py     docker/web.Dockerfile CMD
#:     1   gunicorn master               it spawns its own workers
#:     2   gunicorn sync workers         gunicorn.conf.py `workers`, default 2
#:     3   the supervised swap workers   supervisor.worker_commands()
#:
#: NOT TYPED AS A LITERAL ANYWHERE THAT MATTERS: tests/test_architecture_is_what_the_
#: tree_does.py derives each line of that from its own source and asserts the total,
#: so raising GUNICORN_WORKERS or adding a fourth swap worker fails the test rather
#: than leaving this block describing the old container. That is the whole difference
#: between this module and the five stale descriptions its header lists.
WEB_PROCESS_COUNT = 8

#: Files that PATCH `web` rather than declaring a service, and what each adds. They are
#: reached through the COMPOSE_FILE line in .env -- normally written by
#: `configure_env.py --arm <chain>` -- and NOT through `include:`, which
#: tests/test_compose_default_project.py gates.
#:
#: A BARE `docker compose up` ON A CHECKOUT BUILT FROM .env.example IS ARMED FOR GRC.
#: .env.example:62 states that outright and records why: on 2026-10-10 the operator came
#: up unarmed three times running, because arming meant remembering two -f flags, and a
#: GRC payout then failed with "GRIDCOIN_WALLET_PASSPHRASE is not set" AFTER a customer's
#: deposit had confirmed. So the act of arming is deliberate and reversible but is an
#: .env edit rather than a command-line flag, and docker-compose.yml's own paragraph
#: arguing the opposite is corrected in place rather than deleted.
OVERLAYS = {
    "docker-compose.web.armed-grc.yml": "GRIDCOIN_WALLET_PASSPHRASE, for the brokered GRC payout path",
    "docker-compose.web.armed-sol.yml": "mounts SOL_PAYOUT_KEYPAIR_HOST_FILE read-only at "
                                        "/keys/sol_payout.json and sets SOL_PAYOUT_KEYPAIR_PATH to it",
    "docker-compose.web.armed-xrp.yml": "XRP_PAYOUT_SECRET_SEED",
    "docker-compose.web.armed-icp.yml": "mounts the icp-state VOLUME (not a host path -- that was "
                                        "mistake 2 above) and sets ICP_PAYOUT_IDENTITY",
    "docker-compose.web.hostnet.yml": "replaces the container's whole network mode",
}

#: The files docker-compose.yml pulls in with `include:`, which is what makes a bare
#: `up` read them. Everything else needs an explicit -f or a COMPOSE_FILE entry.
INCLUDED = ("docker-compose.icp.yml", "docker-compose.web.yml")


# ---------------------------------------------------------------------------
# LAYER 1, CONTINUED: WHERE THE CHAIN DAEMONS IN THOSE CONTAINERS COME FROM
# ---------------------------------------------------------------------------
#
# ADDED 2026-11-21, answering the operator's question "can you just integrate these
# independent daemons that open source into our own closed source?". The measured
# answer is that MIT permits it outright and the cheapest way to satisfy it is to
# copy NONE of their code: what lives in this repository is a version string, a
# digest and a license notice -- about 3KB -- and the binaries arrive from the
# projects that built them.
#
# SO THIS IS THE ANSWER AS DATA. Three facts about the chain layer had no home
# before today -- which project each daemon comes from, what proves the bytes, and
# what license travels with them -- and the first two were not written down
# anywhere at all. A paragraph would have been the fourth stale description in this
# module's own header (rule 8 / this file's whole reason), so the facts are a table
# and tests/test_architecture_is_what_the_tree_does.py derives every derivable one
# from the Dockerfiles and the manifest.
#
# NO DIGEST APPEARS HERE. docker/upstream_binaries.sha256 is its one home, the way
# KEYRING below records shape and points at icp/README.md rather than copying it.
# These rows record WHERE the digest lives and WHAT consumes it; the test proves the
# pointer is correct and never asserts a digest's value.


class UpstreamBinary(NamedTuple):
    """One upstream project whose PREBUILT release tarball an image installs."""

    #: The project as it names itself, for a notice and a log line.
    project: str
    #: The host the tarball is fetched from. DERIVED by the test out of the
    #: `installed_by` Dockerfile's curl lines, so a row pointing at the wrong
    #: project fails rather than misleading.
    host: str
    #: The Dockerfile that downloads, verifies and installs it.
    installed_by: str
    #: The `ARG` that decides WHICH release. The version is the one fact written
    #: twice -- here as an ARG and inside the manifest's filenames -- and it cannot
    #: drift, because `sha256sum --ignore-missing -c` verifies nothing and exits 1
    #: when the manifest covers no downloaded filename. The build refuses
    #: disagreement; the test's job is to say so in English at suite time instead of
    #: leaving a cryptic coreutils line in a build log.
    pin_variable: str
    #: The ONE manifest. Both rows carry the same value on purpose: more than one
    #: manifest is more than one home for the same kind of fact.
    digest_manifest: str
    #: Whether the project publishes its own checksum manifest. NOT DERIVED and not
    #: derivable -- it is a dated measurement of someone else's website, and no
    #: offline test can establish it. MEASURED 2026-11-21: bitcoincore.org serves
    #: SHA256SUMS (unsigned plain text) plus a detached SHA256SUMS.asc;
    #: download.litecoin.org serves a CLEARSIGNED SHA256SUMS.asc at the release
    #: root, which `gpg --verify` reports as "Good signature from David Burkett".
    #: This field is what tells the operator which one-time verification applies to
    #: which project, and it is this module's second undefended claim after
    #: LOCAL_REPLICA_KEY_IS_EPHEMERAL. Saying which facts the test does NOT cover is
    #: rule 17's job.
    upstream_publishes_sums: bool
    #: The architectures the image can build for. DERIVED from the Dockerfile's own
    #: `case "$arch"` arms, and the manifest must pin a digest for every one.
    arches: tuple[str, ...]
    #: The ONLY binaries installed out of the tarball. DERIVED from the install
    #: lines. This is the exclusion that is actually available on a prebuilt
    #: artifact: four of the thirteen binaries in the two tarballs are installed,
    #: and the wallet tools left behind carry the same Berkeley DB as the daemons
    #: while `bitcoin-qt` would additionally drag Qt's GPL/LGPL into an image with
    #: no GUI.
    binaries: tuple[str, ...]
    #: Whether the SHIPPED daemon contains Berkeley DB. NOT DERIVED -- the binary is
    #: not in this repository, by design, so nothing offline can read it.
    #:
    #: MEASURED 2026-11-21 against both release tarballs, and it CORRECTS the audit
    #: this change was written from, which concluded Bitcoin 28.1 is clean because
    #: `--without-bdb --with-sqlite` builds a wallet-capable bitcoind with no BDB.
    #: That is true of a build from source and this image does not build from
    #: source. `strings -a <daemon> | grep -c '^Berkeley DB 4\\.8\\.30: (April  9,
    #: 2010)$'` is 1 for bitcoind and 1 for litecoind, 0 for both -cli binaries;
    #: that string is BDB's own DB_VERSION_STRING. `ldd` names no libdb on either,
    #: because the release builds are static -- so an audit done with ldd calls them
    #: clean and is wrong, which is how this gets missed.
    bundles_berkeley_db: bool
    #: The file recording that finding, or "" where there is nothing to record. It
    #: exists so the MIT notices cannot be read as the whole story: Sleepycat is the
    #: only license in these images with a source-disclosure condition.
    copyleft_note: str
    #: Where the notice lands inside the image. Unique per project, asserted.
    license_in_image: str
    #: The tracked file that notice is COPYed from. It cannot come from the tarball:
    #: measured 2026-11-21 by listing every file in both, neither ships a COPYING.
    license_source: str


UPSTREAM_BINARIES = {
    "BTC": UpstreamBinary(
        project="Bitcoin Core",
        host="bitcoincore.org",
        installed_by="docker/harness.Dockerfile",
        pin_variable="BITCOIN_VERSION",
        digest_manifest="docker/upstream_binaries.sha256",
        upstream_publishes_sums=True,
        arches=("x86_64-linux-gnu", "aarch64-linux-gnu"),
        binaries=("bitcoind", "bitcoin-cli"),
        bundles_berkeley_db=True,
        copyleft_note="docker/licenses/berkeley-db/MEASURED_LINKAGE.md",
        license_in_image="/usr/share/licenses/bitcoin-core/COPYING",
        license_source="docker/licenses/bitcoin-core/COPYING",
    ),
    "LTC": UpstreamBinary(
        project="Litecoin Core",
        host="download.litecoin.org",
        installed_by="docker/harness.Dockerfile",
        pin_variable="LITECOIN_VERSION",
        digest_manifest="docker/upstream_binaries.sha256",
        upstream_publishes_sums=True,
        arches=("x86_64-linux-gnu", "aarch64-linux-gnu"),
        binaries=("litecoind", "litecoin-cli"),
        bundles_berkeley_db=True,
        copyleft_note="docker/licenses/berkeley-db/MEASURED_LINKAGE.md",
        license_in_image="/usr/share/licenses/litecoin-core/COPYING",
        license_source="docker/licenses/litecoin-core/COPYING",
    ),
}


class UpstreamSource(NamedTuple):
    """One upstream project an image COMPILES rather than downloads a binary of."""

    project: str
    built_by: str
    #: The `ARG` naming what gets checked out.
    ref_variable: str
    #: Whether that ARG's value is a commit SHA. DERIVED: 40 hex characters or not.
    #:
    #: A TAG IS NOT A PIN, and this field says so out loud rather than leaving it to
    #: be noticed. docs/vendoring_upstream_daemon_code.md already states the standard
    #: this build misses -- "The upstream project, path, and COMMIT SHA. Not a
    #: version string and not a branch name" -- and grc-desk.Dockerfile:44 is
    #: `ARG GRIDCOIN_REF=5.5.1.7`, cloned with `--branch`, which upstream can move.
    ref_is_pinned: bool
    #: Every host its build stage fetches from. DERIVED from the clone and curl
    #: lines, and it is what keeps UPSTREAM_BINARIES' coverage test from demanding a
    #: row for Berkeley DB's tarball.
    fetches_from: tuple[str, ...]
    #: Whether anything verifies those downloads. DERIVED: does the Dockerfile run a
    #: checksum check at all? It does not.
    digest_verified: bool
    license_in_image: str


#: NOT CHANGED BY THIS WORK, AND THAT IS DELIBERATE (rule 16). grc-desk is the one
#: image in this repository that holds a WALLET, its provenance is weaker than the
#: harness's in both ways a build can be weak -- a moving tag for the source and no
#: digest at all for Berkeley DB 4.8.30 from download.oracle.com -- and I cannot
#: establish the right commit SHA from here, because github.com/gridcoin-community
#: returns 403 at this session's proxy and download.oracle.com is not allow-listed.
#: Changing how a fund-holding daemon is built is the operator's.
#:
#: WHAT THIS WORK DOES INSTEAD is make the gap VISIBLE AS DATA, with a test that
#: fails the day the tree and the table disagree in either direction: somebody pins
#: a real SHA and leaves this saying False, so the table understates the tree's
#: safety and the next reader redoes the work; or somebody replaces a SHA with a
#: moving tag and leaves it saying True, which overstates it on an image holding a
#: wallet. That is a recorded fact, not a ratchet: nothing is tolerated by appearing
#: here, and the flag is derived rather than allowed.
UPSTREAM_SOURCE = {
    "GRC": UpstreamSource(
        project="Gridcoin Research",
        built_by="docker/grc-desk.Dockerfile",
        ref_variable="GRIDCOIN_REF",
        ref_is_pinned=False,
        fetches_from=("download.oracle.com", "github.com"),
        digest_verified=False,
        license_in_image="/usr/share/licenses/gridcoin/COPYING",
    ),
}

#: THE DECISION ITSELF, AS DATA: no upstream daemon source or binary is committed to
#: this repository. A sentence saying that would be prose about structure, which is
#: the thing this module exists because it goes stale silently; an empty tuple with a
#: test behind it is the same claim in a form that can fail.
#:
#: Measured 2026-11-21: 567 tracked files, and ZERO whose first bytes are ELF or
#: gzip magic. A CLEAN GATE and not a ratchet (rule 19) -- the asserted count is
#: zero, there is no baseline, nothing is tolerated, and it starts at zero the way
#: rule 18's spelling gate did.
#:
#: THE DENOMINATOR WAS WRONG IN THE FIRST DRAFT OF THIS COMMENT, and it is corrected
#: here rather than overwritten because the mistake is instructive. It said 561,
#: which was the tracked count BEFORE this change -- measured honestly, then made
#: stale by the six files this same commit adds (the manifest, three notices, the
#: licenses README and the test). Rule 3's "a count without what it was counted out
#: of has caused real errors more than once", committed inside the change that
#: broke it. The number a reader can check is 567; the number the gate asserts is
#: the ZERO, which is why the gate is the thing that survives and the count is
#: commentary.
#:
#: WHY IT IS WORTH A GATE RATHER THAN A HABIT. This repository has already decided
#: this twice with numbers, in its own .gitignore: on tools/dleq_helper, "a
#: committed binary is a thing nobody can review and everybody trusts"; and on the
#: cargo/dfx output, which "cost 891 files and 198MB of build output committed to
#: this repository". A bitcoind is a strictly larger version of that trade, and the
#: next person who finds it easier to commit a tarball than to add a digest is who
#: this is for.
#:
#: IT IS ALSO THE ONLY EXCLUSION AVAILABLE FOR SLEEPYCAT. Berkeley DB cannot be
#: removed from a prebuilt daemon, and every obligation in Sleepycat clause 3
#: triggers on DISTRIBUTION. This repository distributes nothing: the image is built
#: on the operator's host from the projects' own tarballs.
VENDORED_UPSTREAM_TREES: tuple[str, ...] = ()

#: The same fact from the other side, named because a reader asks it directly. False
#: is what makes the Berkeley DB finding in docker/licenses/berkeley-db/ a thing to
#: record rather than a thing to remedy today.
UPSTREAM_BINARIES_ARE_REDISTRIBUTED = False


# ---------------------------------------------------------------------------
# LAYER 2: CUSTODY AS IT IS TODAY
# ---------------------------------------------------------------------------


class Custody(NamedTuple):
    """What signs a payout on one chain, today, and what gates whether it can."""

    #: "daemon" -- the chain daemon holds the wallet and signs its own sends, so
    #: reachable means payable and this process reads no signing material at all.
    #: "process" -- this process reads a credential and signs.
    signer: str
    #: The environment variable the RUNNING PROCESS reads, or "" for a daemon chain.
    #: NEVER a value. For SOL this is the in-container path, not the host one.
    credential_variable: str
    #: Where `can_spend` comes from. "class" means inherited from
    #: chains/base.RPCAdapter, which is True because the daemon signs. "derived" means
    #: computed per instance in __init__ from whether the credential is present, and
    #: the class-level value is a fail-closed False that registry.why_cannot_pay_out()
    #: reads through getattr.
    can_spend_from: str


CUSTODY = {
    "BTC": Custody("daemon", "", "class"),
    "LTC": Custody("daemon", "", "class"),
    # GRC's daemon signs too, but an ENCRYPTED wallet must be unlocked first, which is
    # the one credential a daemon chain needs. See GRC_PASSPHRASE_GATES_BOTH_LEGS and
    # GRC_PASSPHRASE_SECOND_NAME below -- this entry is the narrowest true statement
    # about it and both notes are load-bearing.
    "GRC": Custody("daemon", "GRIDCOIN_WALLET_PASSPHRASE", "class"),
    "SOL": Custody("process", "SOL_PAYOUT_KEYPAIR_PATH", "derived"),
    "XRP": Custody("process", "XRP_PAYOUT_SECRET_SEED", "derived"),
    # A dfx IDENTITY NAME, not a key: the PEM behind it lives on the icp-state volume
    # and nothing in this repository opens it.
    "ICP": Custody("process", "ICP_PAYOUT_IDENTITY", "derived"),
}

#: THE GRC PASSPHRASE IS NOT A PAYOUT-ONLY CREDENTIAL, AND THIS TABLE IMPLIED IT WAS
#: FOR ONE COMMIT. services/swap_service.derive_deposit_address() calls
#: chains/gridcoin_wallet_lock.unlocked_for_payout() around `getnewaddress`, because an
#: encrypted Gridcoin wallet will not issue an address while locked -- the log line
#: there spells the sequence out: "lock -> unlock -> derive -> lock -> unlock for
#: staking".
#:
#: SO `configure_env.py --arm grc` GATES GRC IN BOTH DIRECTIONS. On an encrypted wallet
#: an unarmed deployment cannot take a GRC DEPOSIT either, not merely fail to pay one
#: out -- which inverts what a reader would conclude from the CUSTODY row alone, and is
#: why the row above now points here instead of explaining itself. The three callers of
#: unlocked_for_payout() measured 2026-10-11: services/payout_service.py:1582 (the
#: payout), services/swap_service.py:579 (the DEPOSIT address), and
#: modules/atomic_grc_client.py:554 (the HTLC leg, which reads the other variable name
#: below).
#:
#: NOT A DEFECT, AND NAMED ANYWAY. The arming overlay is the right mechanism and the
#: unlock is genuinely required; what was wrong was the description, in the file written
#: to stop descriptions being wrong.
GRC_PASSPHRASE_GATES_BOTH_LEGS = True

#: THE GRIDCOIN PASSPHRASE HAS TWO NAMES AND ONLY ONE OF THEM IS CONFIGURABLE.
#: Measured 2026-10-11: GRIDCOIN_WALLET_PASSPHRASE appears in 28 .py files, 4
#: .env.example lines and 4 compose files; GRC_WALLET_PASSPHRASE appears in ZERO of
#: either and is read only at modules/atomic_grc_client.py:392, as an IMPORT-TIME
#: default argument -- a hazard that module's own header documents and did not fix.
#: That client is reachable from the web container (modules/htlc_assets.py imports
#: GRCClient), so `configure_env.py --arm grc` and the armed-grc overlay arm the
#: brokered payout path and leave the HTLC leg reading a variable nothing sets.
#:
#: THE MECHANISM IS A DERIVED NAME, NOT A SECOND CONSTANT, and that is why grepping for
#: a literal nearly missed it. atomic_swap.py:758 builds the variable as
#: `f"{asset}_WALLET_PASSPHRASE"` -- one pattern serving all three HTLC chains, giving
#: BTC_WALLET_PASSPHRASE, LTC_WALLET_PASSPHRASE and GRC_WALLET_PASSPHRASE. The Flask
#: app names GRC's specifically as GRIDCOIN_WALLET_PASSPHRASE. So the collision is at
#: GRC ONLY, where a generic convention and a specific one meet, and the HTLC driver
#: refuses with "GRC_WALLET_PASSPHRASE is not set" -- a variable the operator has never
#: set and .env.example has never mentioned.
#:
#: WHERE IT CAME FROM, in the operator's words (2026-10-11): "i know why there's
#: multiple grc wallet pahrases. you got confused and were trying to integrate my gui
#: wallet for grc testnet instead of doing what i said which you finally did and was
#: build it's own daemon from the code." The daemon built from source is the `grc-desk`
#: service. So the second name is not a design decision to be respected -- it is debris
#: from an integration that was not what was asked for, and it is recorded here because
#: the next reader would otherwise spend the same effort deciding whether to preserve
#: it. The answer is no.
#:
#: RECONCILED 2026-10-11, once the operator supplied the fact that made it decidable.
#: chains/wallet_lock.passphrase_env_var(chain) is now the one resolver: it returns
#: GRIDCOIN_WALLET_PASSPHRASE for GRC and keeps `f"{chain}_WALLET_PASSPHRASE"` for every
#: other chain -- BTC and LTC wallets can be encrypted and nothing configures a
#: passphrase for either, so the obvious convention is still the most useful thing a
#: refusal can name there. Both suites call it: atomic_swap.py no longer builds the
#: name, and modules/atomic_grc_client.py resolves it at CONSTRUCTION rather than in an
#: import-time default argument -- a second defect that module's own header documented
#: and left in place for weeks, and one that could only ever read "" because it read a
#: name nothing sets.
#:
#: tests/test_one_name_for_the_gridcoin_passphrase.py holds all of it, including the
#: assertion the defect actually needed and neither half had: that the name the code
#: asks for is one .env.example and a compose file can SUPPLY. Both old halves were
#: internally consistent -- the driver asked for a name and its error printed that same
#: name -- which is why nothing caught it.
#:
#: WHY IT WAS SURFACED RATHER THAN FIXED FIRST, since the reversal is the instructive
#: part: changing which credential a signing path reads is rule 16's fund movement, and
#: what could not be established from the tree was whether two names were a deliberate
#: separation worth keeping. They were not. That is not a fact any measurement here
#: could have produced -- it was history only the operator had.
#:
#: THE NAME BELOW IS NOW HISTORICAL and is kept so a reader meeting the old wording in
#: a comment can find out what happened. The comment at atomic_grc_client.py:940 saying
#: the variable's absence "is not an omission" is about a different thing entirely --
#: the __main__ demo block's unset-variable check -- and was never a justification for
#: the split.
# S105 fires on the constant's own name plus a string literal, and it is right to.
# CHECKED: the value is a variable NAME, not a value. The literal is exactly what
# `grep -rn GRC_WALLET_PASSPHRASE` matches, which is the point of recording it, and no
# passphrase appears anywhere in this file or is reachable from it. The alternative was
# renaming the constant until the heuristic stopped noticing, which is gaming a linter
# rather than answering it (rule 19).
GRC_PASSPHRASE_SECOND_NAME = "GRC_WALLET_PASSPHRASE"  # noqa: S105


# ---------------------------------------------------------------------------
# LAYER 3: THE KEYRING
# ---------------------------------------------------------------------------

#: The three rust canisters icp/dfx.json declares, and what each is. SHAPE ONLY --
#: icp/README.md and the two .did files own the detail, and copying it here would be
#: two copies of one rule (rule 8).
#:
#: WHAT THE KEYRING CANNOT DO IS THE POINT OF IT. threshold_custody exposes exactly
#: two methods, `config()` and `public_key()`, and no signing call appears in either
#: crate's source. tests/test_icp_canister_cannot_sign.py pins that for BOTH canisters
#: with a per-canister forbidden list -- `http_request` is forbidden in
#: threshold_custody, where it could only be the management canister's outcall, and
#: ALLOWED in operator_admin, which defines a method of that name to serve its own
#: console.
#:
#: THE WASM-LEVEL PIN IS SKIPPED unless ST_CHECK_WASM is set, so an ordinary
#: `python3 -m pytest` proves the absence from the SOURCE only. Run
#: `ST_CHECK_WASM=1 python3 -m pytest tests/test_icp_canister_cannot_sign.py` before
#: trusting a deployment; it is the only check that would catch a signing call arriving
#: through a macro, a re-export or a dependency.
KEYRING = {
    "threshold_custody": "holds one threshold key and reports PUBLIC halves only. "
                         "Three curves: secp256k1_ecdsa (BTC/LTC/GRC), ed25519 (SOL/XRP), "
                         "bip340secp256k1 (taproot, no asset uses it yet). Cannot sign.",
    "operator_admin": "configuration and audit: posture, fee bps, pairs, per-asset "
                      "confirmations, armed flags, an audit log, and an operator console "
                      "served from its own http_request query. Holds no key material.",
    "icp_ledger_canister": "DFINITY's RELEASED ledger wasm (ledger-suite-icp-2025-08-29), "
                           "not a hand-rolled ICRC-1 -- an adapter tested against a ledger "
                           "written by the adapter's author proves only that they agree.",
}

#: The derivation scheme, in one place, as modules/keyring_paths.derivation_path()
#: builds it. Recorded as a shape rather than re-implemented, because the function is
#: the authority and this is a description of it.
DERIVATION_SCHEME = (b"swap_terminal", b"v1", b"<network>", b"<ASSET>", b"<index as 4 big-endian bytes>")

#: WHY ICP IS ABSENT FROM THE CURVE TABLE and a reader looks for it first: a canister's
#: ICP identity is its own principal. There is no threshold key to derive and no address
#: to compute, so a row would describe a derivation that does not exist.
KEYRING_ASSETS = ("BTC", "LTC", "GRC", "SOL", "XRP")

#: ONE KEY NAME SERVES ALL THREE CURVES, AND THAT IS CORRECT -- checked against primary
#: sources on 2026-10-11 after a review agent claimed otherwise.
#:
#: The claim was that icp/threshold_custody_init.did's shipped
#: `(record { key_name = "dfx_test_key" })` asks for an Ed25519 key under a name that
#: cannot have one, which would make the SOL and XRP legs of the keyring unusable
#: locally -- threshold_custody/src/lib.rs uses the one configured name for both
#: EcdsaKeyId.name and SchnorrKeyId.name. It would have been a real defect in the
#: canister layer. IT IS NOT TRUE. DFINITY's threshold-Schnorr page lists the supported
#: key ids for the feature as `dfx_test_key` ("Only available on the local development
#: environment started by dfx"), `test_key_1` and `key_1`, and separately gives an
#: Ed25519 `dfx_test_key` public key. Fetched from
#: raw.githubusercontent.com/dfinity/portal/master/docs/building-apps/network-features/
#: signatures/t-schnorr.mdx, HTTP 200, 12691 bytes.
#:
#: RECORDED BECAUSE THE AGENT'S VERSION WAS NEARLY PASSED ON AS A FINDING. It arrived
#: with a file-and-line citation and was wrong, which is rule 17's failure mode wearing
#: someone else's evidence: a reason to believe is not a check.
#:
#: AND THE SAME PAGE RESOLVES WHAT LOOKED LIKE A CONTRADICTION WITH THE LINE BELOW. It
#: publishes FIXED public keys for `dfx_test_key` on both curves, which would mean a
#: deterministic key -- while this tree measured the local key CHANGING across one
#: container restart. Both are true: the published values are labeled the POCKETIC test
#: keys, and PocketIC is a different harness from `dfx start`. So a deterministic
#: published value exists and does not apply to the replica this project runs.
#:
#: MEASURED 2026-10-07 AND IT SHAPES EVERYTHING ABOVE: the local replica's threshold key
#: does NOT survive container recreation. The same canister id and the same
#: `dfx_test_key` name produced two DIFFERENT public keys across one ordinary
#: stop-and-start, which means every custody address derived from it changed. So a
#: custody address computed against the local replica is valid for that replica's
#: lifetime and no longer -- never write one into a swap, a config file or a document as
#: though it were stable.
LOCAL_REPLICA_KEY_IS_EPHEMERAL = True
