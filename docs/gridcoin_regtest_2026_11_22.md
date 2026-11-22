# Gridcoin regtest, measured against a daemon that was actually built

    Role: measurement record (what a real Gridcoin regtest node did, and what it refused)
    Reads: nothing at runtime
    Writes: nothing
    Can move funds: no
    Mainnet-safe: yes -- prose
    Live-safe: yes

Every figure below was taken on 2026-11-22 from a `gridcoinresearchd` compiled from the
exact commit `docker/grc-desk.Dockerfile` pins, run on **regtest only**, on loopback only,
in a throwaway datadir, against a wallet whose entire contents were a genesis premine.

    binary      Gridcoin Core v5.5.1.7-30a89f2ab0bb
    source      ref 5.5.1.7-testnet, commit 30a89f2ab0bb4f7134ca7a9c74e233e76130ebd8
                (clone verified with `git rev-parse HEAD`)
    network     regtest, selected with `-regtest` ON THE COMMAND LINE, RPC on 127.0.0.1:35715
    reaper      the pid from the daemon's own pid file, then `kill -0` polled until it
                raised ProcessLookupError. The absence is the assertion (rule 13).

**It exists because the previous round of GRC claims were READINGS written in the voice of
measurements** (rule 17). The source said a thing; nothing had asked a daemon. Three of those
readings were right, one was wrong in a way that mattered, and one was wrong in the other
direction. The table below is the part that is now behavior.

---

## 1. What the daemon actually answered

| question | grade before | answer |
|---|---|---|
| `getblockhash 0` on regtest | READ from `chainparams.h:33` | `4692b9564c585f76f41e02e304767b2ad16f1d18f72efcf0a724efe01e065371` -- **identical** to the pinned constant |
| does `getblockchaininfo` report a network name? | READ | **no.** Six keys: `blocks difficulty errors in_sync moneysupply testnet`. There is no `chain` key |
| `getblockchaininfo.testnet` on **regtest** | READ | **`False`** -- the same answer mainnet gives |
| the genesis premine | READ from `chainparams.cpp:62-85` | `listunspent` -> **10 entries, 1,000,000.00 GRC** |
| is CLTV active from genesis? | READ (`BlockV14Height=0`) | **block 1 has `version = 14`** |
| `sendtoaddress` under the devbuild cripple | READ | **blocked**, `code -4`, "The transaction was rejected" |

The second and third rows together are the defect that
`swap_terminal/chains/gridcoin_genesis.py` was written for, and this is the first time either
was observed rather than deduced. One boolean cannot name three networks, and the two it
conflates are mainnet and regtest.

## 2. The old network gate refused this node as mainnet

Run against the live daemon above, `funding_steps.grc_boolean_network_signals(...,
regtest_identified=False)` -- which is exactly what the gate did before 2026-11-22 --
returned:

    positives            (none)
    mainnet statements   ['getblockchaininfo.testnet=False',
                          'getinfo.testnet=False',
                          'getmininginfo.testnet=False']

Three positive statements of mainnet and nothing to set against them, so
`assert_test_network()` raised `REFUSING TO RUN ... POSITIVELY STATED MAINNET`, unoverridably,
against a chain whose coins are minted from nothing.

The committed gate, run against the same daemon in the same state, **accepted** it:

    OK    getblockhash(0)                     genesis 4692b9564c585f76… is Gridcoin regtest
    SKIP  getblockchaininfo.testnet           False
    SKIP  getinfo.testnet                     False
    SKIP  getmininginfo.testnet               False
    OK    test network stated by the daemon   getblockhash(0)=the Gridcoin regtest genesis

The three `SKIP`s are the point: `False` is consistent with regtest and establishes nothing,
so it is not counted toward the positive statement the gate requires. The genesis carries it
alone.

## 3. Block production is ON DEMAND but STAKE-BOUNDED, and that overturned a conclusion

**`generatetoaddress` works.** Ten blocks from the premine returned instantly.

**It then stops, and this is the finding.** Asked for 1000 blocks it refused partway:

    Failed to mine block 13 of 1000: CreateCoinStake: no stake found
                                     (need a mature UTXO with sufficient weight)

Gridcoin is proof-of-stake, and `TryMineRegtestBlock` runs the real coinstake pipeline. From
the ten premine UTXOs the chain reached **tip 26** and stalled; grinding in 50-block batches
for 31 further rounds produced **4 more blocks and 31 stake-exhausted refusals**.

So the conclusion recorded earlier in the day -- that GRC's production locktime of `tip+1920`
"is two `generatetoaddress` calls" -- **was wrong**, and it was wrong because it was computed
from an RPC's documented argument range rather than from running it.

**`setmocktime` is what unblocks it, and the effect is not marginal.** Interleaving a mock-clock
advance with each small batch:

    tip 26 -> 729      703 blocks      1.7µfn (2.1s)        ~340 blocks/s
    tip 729 -> 2650    1921 blocks     6.3µfn (7.6s)        the FULL production locktime

So `contract_locktime("GRC", ROLE_INITIATOR, tip) == tip + 1920` is reachable in **under eight
seconds** -- but only with `setmocktime` in the mining loop. A GRC leg that calls
`generatetoaddress` alone, as the BTC and LTC legs do, stalls at about block 26 and then
fails on a timeout with a message about the chain not advancing.

That is the single most important integration requirement on this page: **for GRC, "mine N
blocks" is `setmocktime` + `generatetoaddress`, not `generatetoaddress`.**

## 4. The conf is per-network AND the daemon writes one if you do not

    Using data directory <datadir>/regtest

The effective datadir is `<datadir>/<net>/`, and the config file the daemon reads is
`<datadir>/<net>/gridcoinresearch.conf`. Three measured consequences:

- **A conf at the datadir root is not it.** One placed there did not supply working RPC
  credentials; `rt:rt` came back `401` with `ThreadRPCServer incorrect password attempt`.
- **The daemon AUTO-CREATES that file with random `rpcuser` and `rpcpassword`** when it is
  absent -- a 536-byte file carrying those two keys plus six `addnode` lines. That is the real
  reason `regtest/daemons.GRC_CREDENTIALS_ARE_PER_NETWORK` is true: the credentials are
  per-network because the conf is per-network and is *generated* per network.
- **`-rpcpassword` on the command line does not satisfy `-server`.** It still exits with
  `To use the "-server" option, you must set a rpcpassword in the configuration file:` --
  and that message ends in a colon with nothing after it, naming no file.

Pre-placing the conf in the net subdirectory before the first start is what worked.

Also measured, and it is the fail-safe direction: an unknown command-line argument is
**rejected** (`Invalid parameter -noncruncher=1`) rather than ignored, so a typo cannot
silently start a daemon on the wrong network. A conf-only setting is not a valid argument.

## 5. The `grc-desk` image cannot be built, for three independent reasons

Measured in the clone, not inferred from a failed build:

    autogen.sh    ABSENT        CMakeLists.txt:1  "# CMake is the official build system for Gridcoin."
    configure.ac  ABSENT        build_targets.sh  present
    configure     ABSENT        grep -in cmake docker/grc-desk.Dockerfile -> no match
    Makefile.am   ABSENT

1. `docker/grc-desk.Dockerfile:124` runs `./autogen.sh`, then `./configure` at `:125`, inside
   `RUN set -eux`. It dies at `:124` -- after the apt layer and after the whole Berkeley DB
   4.8 compile. The commit pin at `:121-122` passes three lines earlier, so the log reads
   `gridcoin source verified: 5.5.1.7-testnet = 30a89f2…` and then `./autogen.sh: not found`.
2. The BDB 4.8.30.NC stage is **superseded**. `src/bdb53/` is vendored (9190 files, its own
   `CMakeLists.txt`), and `SYSTEM_BDB` defaults OFF, which runs `add_subdirectory(bdb53)`.
   A wallet this build writes is therefore **BDB 5.3** format, which a 4.8 build cannot read.
3. Both `COPY` lines are wrong. The cmake build puts the daemon at `<build>/bin/`, not
   `/src/src/`, and **there is no `gridcoin-cli` target at all** -- `cmake --build --target help`
   offers `gridcoinresearchd`, `gridcoin_util` and `leveldbutil` and nothing else.

**What does work**, run natively here to completion:

    cmake -S <src> -B <build> -DCMAKE_BUILD_TYPE=Release -DENABLE_GUI=OFF -DENABLE_TESTS=OFF -DENABLE_DAEMON=ON
    cmake --build <build> -j3          # exit 0, zero errors, -> <build>/bin/gridcoinresearchd

`ENABLE_GUI`, `ENABLE_TESTS` and `ENABLE_DOCS` already default OFF and `ENABLE_DAEMON`
defaults ON, so most of the old `--without-gui --disable-tests --disable-bench` flags have no
cmake equivalent to port. Dependencies, found by running configure until it stopped
complaining rather than by copying upstream's list: `build-essential cmake pkg-config bison
xxd git curl`, plus `libboost-*-dev libevent-dev libssl-dev libcurl4-openssl-dev libzmq3-dev
libminiupnpc-dev`.

**The Dockerfile change itself is NOT made here and is a proposal, not a fix** (rule 16):
`docker build`'s apt step cannot bootstrap through this environment's proxy, so the first real
test of any replacement build stage is a run on the operator's host.

Why this went unnoticed: the file's own comment says it. It was corrected on 2026-11-21
because the ref it named did not exist -- *"THIS IMAGE COULD NOT BE BUILT AT ALL"* -- and it
remained unbuildable for a different reason three lines further down, because *"nothing in the
suite or in CI builds it (there is no docker daemon in the environment these files are written
in)"*. An unbuilt Dockerfile is an untested one, and fixing the first thing that stops a build
tells you nothing about the second.

## 6. Native HTLC RPCs exist and are script-incompatible with this tree

`src/htlc.cpp` and `src/htlc.h` exist, and `src/rpc/server.cpp` registers three
`heritage_pure_gridcoin` wallet commands: `createhtlc` (`:436`), `claimhtlc` (`:435`) and
`refundhtlc` (`:487`). None appears in `chains/gridcoin_rpc_commands.COMMANDS`, which was
built from 5.5.1.0.

They cannot spend what this tree builds. Both opcode sequences were decoded:

    this tree        OP_IF   OP_SHA256 <hash:32> OP_EQUALVERIFY
                             OP_DUP OP_HASH160 <pubkeyHASH:20> OP_EQUALVERIFY OP_CHECKSIG
                     OP_ELSE <locktime> OP_CHECKLOCKTIMEVERIFY OP_DROP
                             OP_DUP OP_HASH160 <pubkeyHASH:20> OP_EQUALVERIFY OP_CHECKSIG
                     OP_ENDIF

    Gridcoin native  OP_IF   OP_SHA256 <hash:32> OP_EQUALVERIFY <PUBKEY:33> OP_CHECKSIG
                     OP_ELSE <timeout> OP_CHECKLOCKTIMEVERIFY OP_DROP <PUBKEY:33> OP_CHECKSIG
                     OP_ENDIF

Pubkey-hash against bare pubkey: different scripts, different P2SH addresses, different
scriptSigs. And `src/htlc.cpp:83-111` is a strict parser that walks the expected opcodes in
order, so `claimhtlc` and `refundhtlc` **refuse** a contract built here. The reasoning for
keeping this tree's shape is written at the site, in
`swap_terminal/modules/atomic_htlc_scripts.py`.

---

## What is still not established

- **No HTLC has been funded or spent on a Gridcoin regtest chain.** Everything above is the
  chain and the daemon, not a swap. `sendtoaddress` is blocked by the devbuild cripple, so a
  real GRC regtest HTLC run needs `-devbuild=override`, which has not been exercised here.
- ~~**The replacement Dockerfile build stage has not been built as an image**, only natively.~~
  **IT HAS, later the same day, on the operator's host: all 16 steps and
  `Successfully tagged swap-terminal/grc-desk:local`.** The run before it failed at exactly
  one thing -- `find_package(ZLIB REQUIRED)`, because the dep list had been derived by
  running cmake on a machine that already had zlib's headers -- and the fix was one package
  plus enumerating every unconditional `find_package` from the source instead. The image log
  independently confirmed the vendored Berkeley DB 5.3 building itself
  (`BerkeleyDB_Project build command succeeded`), the `/build/bin/` output path, and that no
  CLI target exists. ~~**What is still untested is one layer further out: no container from
  that image has been STARTED**, so the runtime stage's library set, the uid-1000 `/data`
  ownership and the CMD are unproven.~~ **A CONTAINER NOW RUNS AND ANSWERS RPC**, later the
  same day, and starting it found three defects the build could not have:

      libboost_iostreams.so.1.74.0: cannot open shared object file
          The build stage installed libboost-iostreams-dev; the runtime stage installed
          system, filesystem and thread and not iostreams. A link-time dependency with no
          run-time counterpart. Guarded now by
          tests/test_runtime_stage_has_a_lib_for_every_boost_dev.py.

      Gridcoin: Error initializing settings.
          uid. The image's user is 1000; the operator is 1001, and the bind-mounted datadir
          was mode 775 owned by 1001:1001 -- so uid 1000 matched neither owner nor group and
          fell through to `others`, r-x, no write. The Dockerfile's claim that uid 1000 makes
          an operator-created datadir "writable without a chown" holds only where the
          operator IS 1000. docker-compose.grc-regtest.yml now carries a `user:` key.

      Recv failure: Connection reset by peer
          `RPC: bound and listening on 127.0.0.1:35715` -- inside the container's namespace.
          Docker forwards the published port to the container's BRIDGE address, where nothing
          was listening, so a healthy daemon looked dead. rpcbind=0.0.0.0 plus
          rpcallowip=172.16.0.0/12 fixes it, and the log then reads `bound and listening on
          0.0.0.0:35715 (-rpcbind)`. The same shape was already recorded at
          OPEN_FINDINGS.md:39-40 for the GRC desk and was not applied here.

  **AND THEN THE CHAIN ANSWERED, FROM INSIDE A CONTAINER:**

      getblockhash 0  ->  4692b9564c585f76f41e02e304767b2ad16f1d18f72efcf0a724efe01e065371
      listunspent     ->  10 UTXOs totalling 1000000.0

  Identical to the native measurements in section 1, which closes the chain end to end:
  pinned source, built image, running container, live chain. The genesis hash is the one
  `chains/gridcoin_genesis.py` keys the network gate on, so that gate is now exercised
  against a containerized node and not only a native one.
- **Nothing was measured about 5.5.1.0**, the version this tree's own command table describes.
  It has no regtest at all, so none of this page applies to it, and which Gridcoin a GRC leg is
  pointed at is a decision rather than a measurement.
- **The `setmocktime` mining loop has not been run through the harness**, only directly. The
  figure that matters -- 1921 blocks in 6.3µfn (7.6s) -- is a floor for what the harness could
  achieve, not a measurement of the harness achieving it.
