# Containerizing swap_terminal: survey, topology proposal, and what stays the operator's

Role: design document (survey plus proposal)
Reads: this repository's source tree, `swap_terminal/swap_terminal.db` (PRAGMA
       values only, no rows), and the `pytest` run recorded at the end
Writes: nothing. No code, no Dockerfile, no compose file, no existing file
       changed by the commit that adds this one.
Can move funds: no
Mainnet-safe: yes -- nothing here was executed against any chain, any wallet,
       or any daemon. No socket was opened to any RPC endpoint.

Written 2026-10-03, against the tree on branch `claude/xrp-adapter` at the
commit this document is added on top of.

**THE SINGLE MOST IMPORTANT SENTENCE IN THIS FILE: NO DOCKER IS INSTALLED IN
THE ENVIRONMENT THIS WAS WRITTEN IN, AND NO CONTAINER WAS RUN.** Every claim
below about what a container *does* at runtime is therefore a PROPOSAL and not
a measurement, in CLAUDE.md rule 16's last row ("if the change cannot be tested
here, it is a proposal and not a fix. Say which"). The claims about what is in
*this tree* are measurements, were taken by reading and running things here,
and each one states its denominator (rule 3). Where the two kinds of statement
sit in the same paragraph, the proposal half is labeled `PROPOSAL` or
`NOT ESTABLISHED`. Rule 17 is the reason that labeling is not decoration: a
hypothesis written in the register of a measurement costs the reader the ability
to tell which they are holding, and from then on they have to re-verify
everything.

---

## 0. What prompted this, and the one structural fact that makes it timely

The operator's instruction, in their words, 2026-10-03:

> "i think this is where we start using docker to run all our supervisors, etc,
> our daemons, and the ultimately the UI and Operator panel."

It arrived at the end of a session whose subject was custody separation: the
desk's hot wallet and the operator's own wallet were measured to be the same
wallet, on all three Bitcoin-derived chains, because `BTC_RPC_WALLET`,
`LTC_RPC_WALLET` and `GRC_RPC_WALLET` are each `_env(..., "")`
(`swap_terminal/config.py:447, 455, 520`) and
`chains/base.RPCAdapter` appends `/wallet/<name>` only for a non-empty value.
An empty value addresses the daemon with no wallet path, which routes to
whatever wallet that daemon serves by default -- the same wallet a bare
`getnewaddress` reaches. `wallet_custody.py` is the tool that reports this, and
its header carries the full measurement.

BTC and LTC were separated by creating a second named wallet per daemon, which
works because Bitcoin Core 28.1 and Litecoin Core 0.21.4 both have
`createwallet` / `loadwallet` / `listwallets`.

**GRC could not be done that way.** Measured by the operator on their host today,
2026-10-03: Gridcoin v5.5.1.0 has NO multiwallet RPCs at all --
`createwallet`, `loadwallet`, `listwallets` and `unloadwallet` are all absent
from `help`. One wallet per datadir, full stop. **This measurement is the
operator's, not mine**: there is no network path from here to their daemon and
no `gridcoinresearchd` binary in this environment, so I did not and could not
re-run it (rule 17 -- say which you have). What I *can* establish here is the
corroborating shape in the tree: `wallet_custody.py:279` carries a
`noqa: BLE001` whose comment says in so many words that "listwallets is absent
on older daemons", and `swap_terminal/regtest/daemons.py`'s `CHAIN_DEFAULTS`
contains **2 of the 3** Bitcoin-derived chains -- BTC and LTC only, no GRC entry
at all -- so the regtest harness has never had a GRC daemon to start.

If separation on GRC requires a second daemon with its own datadir, then it
requires a second datadir, a second `gridcoin.conf`, a second RPC port, a second
set of RPC credentials, a second wallet lock, and a second thing that must be
started and stopped and proven stopped. **That is the connection worth making
explicit: a container is a packaging for exactly that bundle.** It is the
honest reason containers are timely here, and it is a much narrower reason than
"let us containerize everything".

---

## 1. SURVEY: every process this system starts

Counted by reading the tree, 2026-10-03. The denominator for "process kinds" is
**9**: three supervised workers, one WSGI application (in two forms), one
operator panel, one Express server, and the chain daemons (two kinds the
harness starts, plus one the operator starts by hand).

### 1.1 The three supervised workers -- 3 of 3 have a reaper, and it proves absence

| worker | path | poll | what it writes | started by | stopped by | pid file | stop proven? |
|---|---|---|---|---|---|---|---|
| deposit_watcher | `swap_terminal/workers/deposit_watcher.py` | 15s (`:68`) | credits swaps; `deposit_events` | `supervisor.start_worker()` | `supervisor.stop_worker()` | `runtime/deposit_watcher.pid` | **yes** |
| payout_worker | `swap_terminal/workers/payout_worker.py` | 10s (`:80`) | `payouts`; **BROADCASTS** | same | same | `runtime/payout_worker.pid` | **yes** |
| reconcile_worker | `swap_terminal/workers/reconcile_worker.py` | 60s (`:100`) | `wallet_inventory`, reconciliation rows | same | same | `runtime/reconcile_worker.pid` | **yes** |

`swap_terminal/supervisor.py` is 1,700+ lines and is the best-honored
instance of rule 13 in either repository. The parts that matter to this document:

- `worker_commands()` (`:137`) is the complete spawn table. **3 entries, and 3
  is the denominator for every other count in this subsection.**
- `stop_worker()` (`:769`) returns one of five outcomes, and `stopped` is
  returned **only** after `_wait_for_absence()` (`:853`) has polled
  `process_alive(pid)` until it is false. The absence is the assertion, not the
  exit code of the kill. This is rule 13's third bullet implemented literally.
- `pid_is_still_ours(pid, recorded_command)` (`:456`) guards pid reuse: a pid
  file naming a pid that now belongs to something else yields `stale-pidfile`,
  nothing is signaled, and the file is removed. "This is the case where killing
  would be the damage" is its own comment.
- `unaccounted_workers()` (`:167`) walks `/proc` for worker processes **no pid
  file accounts for**. That is the second half of rule 13 -- finding the orphan
  nobody recorded -- and it is the function containers break hardest. See §6.1.
- The CLI is exactly three verbs: `start`, `stop`, `status` (`:1630`).

**So the baseline this document is measured against is not "an unreaped mess".
It is a working, proven-stop supervisor, and any container topology that makes
the stop less provable is a regression** -- not neutral, a regression, on a
system whose `payout_worker` can broadcast.

### 1.2 The WSGI application -- two start paths, one of which multiplies processes

- `python3 swap_terminal/app.py` -- the Werkzeug development server.
  `app.py:77-78` set `DEFAULT_HOST = "127.0.0.1"`, `DEFAULT_PORT = 5000`.
  `debug_enabled()` (`:85`) returns true only for an explicit
  `SWAP_TERMINAL_DEBUG`, so the `debug=True, host="0.0.0.0"` line CLAUDE.md
  rule 13 calls "the highest severity line in the repository" is gone; the
  defaults are loopback and debug-off, and `exposure_warnings()` (`:116`) prints
  a warning when they are overridden.
- `gunicorn -c gunicorn.conf.py wsgi:app` -- the deployment path.
  `gunicorn.conf.py:110` builds `bind` from `SWAP_TERMINAL_HOST` (default
  `127.0.0.1`) and `SWAP_TERMINAL_PORT` (default `5000`); `:121`
  `workers = int(os.getenv("GUNICORN_WORKERS", "2"))`; `:122` `worker_class =
  "sync"`; `:130` `preload_app = False`.
- **`gunicorn` is not installed in this environment, so neither of the gunicorn
  claims above was executed.** `app.py`'s own `__main__` comment says the same
  thing about its logging handoff, in the same register, and for the same reason.

**`workers = 2` is the number that matters for §5.** The default deployment is
*already* two processes writing to one SQLite file, before any worker is
started, and `swap_terminal/import_spawn_guard.py` exists specifically because
of what gunicorn's fork model would do to a poll loop started at import time:
"gunicorn does not start one of them. It starts one PER WORKER... There are
simply N deposit watchers, or N payout workers, polling the same SQLite database
and each reading the same pending swap."

Reaper: gunicorn's own master process, for its workers. For the master itself
there is **no pid file and no stop that proves absence** --
`gunicorn.conf.py` has no `pidfile` setting (grepped: `^pidfile` has 0
occurrences in that file). That is the one gap in the application tier's reaping
today, and it is a gap containers would actually *close*, because a container's
stop is a signal to pid 1 plus a wait. See §3.

### 1.3 The operator panel -- a reaper, and a refusal to start a second run

`operator_panel.py` is 120KB, stdlib-only, `ThreadingHTTPServer` on
`DEFAULT_PORT = 8765` (`:68`), and **`--host` does not exist**: the bind address
is a module constant, by design, because "the Flask app makes loopback a
DEFAULT, which is right for a read-only page; a page with buttons should not
have an override to lose".

It spawns harness entry points from an allowlist (`operator_panel.RUNNABLE`),
and `swap_terminal/regtest/harness_runner.py` is the reaper: killed by pid,
never by a pgrep pattern, `terminate()` then `wait` then `kill()`, and the panel
refuses to start a second run while one is alive (`harness_runner.py:25-28,
151-168`). It also starts one `threading.Thread(target=server.serve_forever,
daemon=True)` (`:1949`) whose reaper is process exit -- acceptable because, as
`harness_runner.py:131` says, the thread holds nothing needing unwinding and the
child it watches is killed explicitly by pid.

Its header says `Writes: nothing on disk`, and `Mainnet-safe: NO, AND IT REFUSES
TO BE ASKED` -- the daemon must *say* it is on a test network before the port is
bound.

### 1.4 The Express server -- no pid file, no stop path, and a port collision

`swap_terminal/grc-sol-swap/abstergo_exchange/server.js`:

- `:80` `const PORT = Number(process.env.PORT || 5000);`
- `:768` `app.listen(PORT, ...)`
- `:79` `SOLANA_PAYER_KEYPAIR_PATH`, `:143` `loadKeypair(...)` -- **this process
  can sign and send SOL.**
- `:92-102` builds `GRIDCOIN_RPC_URL` from `GRIDCOIN_RPC_HOST` /
  `GRIDCOIN_RPC_PORT` and throws if neither form is set.
- Its authority for live swap intents is `swap_intents.json`, not
  `swap_terminal.db` -- CLAUDE.md rule 15 already names this as the tree's
  second system of record, and it is unchanged.

**Nothing in this repository starts it, stops it, or records its pid.** Grepped
for `server.js` across `.py`, `.sh`, `.js`, `.json` and `.md`: the matches are
`package.json` scripts and prose. **`PORT || 5000` is the same default as the
Flask app's `DEFAULT_PORT = 5000`** (measured: 23 occurrences of the literal
`5000` across non-test `.py`/`.js` outside `node_modules`, in 10 files). Two
servers, one default port, and the loser gets `EADDRINUSE` -- which is the
benign outcome. **The malign outcome is the one rule 14 is about: whichever
started first answers, and a reader cannot tell from a URL which of the two
they are talking to.**

This is the one tier where containers are an unambiguous improvement, because
it currently has no reaper at all and a container gives it one for free.

### 1.5 The chain daemons -- 2 kinds the harness starts, 1 the operator starts

`swap_terminal/regtest/daemons.py` `CHAIN_DEFAULTS` (`:93`), **2 entries of 2**:

```
BTC   bitcoind    bitcoin-cli    ~/regtest/btc   18443   bitcoin.conf    bitcoind.pid    ST_REGTEST_BTC
LTC   litecoind   litecoin-cli   ~/regtest/ltc   19443   litecoin.conf   litecoind.pid   ST_REGTEST_LTC
```

`start_daemon()` spawns `<daemon> -datadir=... -regtest -daemon`;
`stop_daemon()` is the reaper, called from the `finally` block of
`regtest_htlc_verify.py`'s `main()`. The stop **reads the pid from the daemon's
own pid file BEFORE asking it to stop, then polls `os.kill(pid, 0)` until
`ProcessLookupError`**. And: "A DAEMON THIS HARNESS DID NOT START IS NEVER
STOPPED BY IT" -- if RPC already answers, it adopts and leaves it running.

**GRC is not in that table.** The operator's Gridcoin daemon runs from
`/home/mpjones26/.GridcoinResearch` on rpcport `25715`, is started by hand or by
their desktop session, and **nothing in this repository starts it, stops it,
reads its pid file, or proves it stopped.** Grepped for `gridcoinresearchd`
across the tree: every match is a comment, a hint string or a pasteable command
for the operator to run -- 0 spawn sites. So of the three Bitcoin-derived
chains, **2 of 3 have a reaper in this tree and 1 of 3 has none**, and the one
without is the chain that holds the operator's own staking balance and whose
wallet the swap desk currently shares.

### 1.6 The remaining spawn sites, for completeness

- `swap_terminal_desktop.py:536` sets `SWAP_DB_PATH` in a child environment and
  re-execs itself with `--shim` (`:837`, `Path(__file__).resolve()`). A desktop
  launcher, and `install_desktop_icon.py` writes a `.desktop` file -- rule 2's
  "shell scripts and `.desktop` launchers reference files by name in ways an
  import check never sees" applies to anything that moves a path here.
- `swap_terminal/services/kill_switch.py` -- **not a new spawn, but a second
  caller of the spawn.** It imports `supervisor` directly (`:146`) and calls
  `supervisor.start_worker` / `supervisor.stop_worker` from a web request. This
  is the most container-sensitive line in the tree and §6.1 is about it.

---

## 2. SURVEY: every port

### 2.1 The classifier that refuses

`swap_terminal/network_target.py` is the authority, and it classifies **3 of the
5 chains** -- `CHAIN_PORTS` holds BTC, LTC, GRC; SOL and XRP are URLs with no
port to compare, which `_ENDPOINT_VARIABLES` (`:120`) says in a comment.

```
chain  mainnet   test ports                variable        classify() answers
BTC    8332      {18332, 18443}            BTC_RPC_PORT    MAINNET TEST UNCONFIGURED UNRECOGNIZED
LTC    9332      {19332, 19443}            LTC_RPC_PORT    same
GRC    15715     {25715, 25779, 9876}      GRC_RPC_PORT    same
SOL    --        SOL_RPC_URL                               not classified
XRP    --        XRP_RPC_URL                               not classified
```

`classify()` has **4 answers rather than 2**, and `UNRECOGNIZED` is the one that
decides this document: "an operator running a mainnet daemon on a custom
`-rpcport` lands here, and telling them 'not mainnet' would be a guess dressed
as a measurement (rule 17)".

`may_read_a_wallet(chain, port)` (`:206`) is the refusal, and it is taken
**before any socket opens**. It returns `False` for `UNCONFIGURED`, `MAINNET`
*and* `UNRECOGNIZED`. Its own docstring states the incident that produced it:
`getbalance` against GRC 15715 printed 157,797 GRC of the operator's real
staking balance into a chat log on 2026-09-25.

Its callers, measured: `swap_readiness.chain_precheck()` (which wraps it and
adds the PASS/FAIL column) and `wallet_custody.py`. Both refuse an
`UNRECOGNIZED` port.

### 2.2 THE HAZARD, stated as a hazard

**If a container topology moves a chain RPC port to a number not in
`CHAIN_PORTS`, every tool that routes through `may_read_a_wallet()` refuses the
endpoint, and the only way to make them stop refusing is to edit
`network_target.CHAIN_PORTS`.**

That is not a detail to fill in later, for a reason specific to what that table
is:

1. The refusal fires **before** the socket. The failure mode is not a timeout
   you can debug; it is a flat `port N is not a GRC port this tree knows, so
   which chain it is was NOT established`. An operator reads that as a
   configuration bug in the tool, not as a consequence of a port map.
2. The remedy -- adding the new port to `test_ports` -- **widens the set of
   ports this tree will read a wallet on.** Every port added to `test_ports` is
   a port on which `getwalletinfo` will be called and a balance printed. The
   table is narrow on purpose, and its own comment says the GRC entries are "the
   ones actually present in the operator's `gridcoin.conf` files, surveyed
   2026-09-25 -- not a guess at what is conventional."
3. There is no third option. `classify()` has no "trust me" flag and should not
   get one.

**The design consequence, and it is not negotiable within this proposal:
containers must publish each chain's RPC port at the SAME number inside and
outside.** `-p 25715:25715`, never `-p 25716:25715`. A renumber is a change to
the file that holds the mainnet refusal, and the diff would be two characters
with the blast radius of a wallet read.

The one legitimate reason to touch `CHAIN_PORTS` is the GRC second daemon in
§0: a second Gridcoin datadir needs a second RPC port, and if it is to be a test
chain the tree recognizes, that port has to be one of `{25715, 25779, 9876}` --
of which **25715 is taken by the operator's existing daemon**, leaving 25779 and
9876 already in the table. **So the second GRC daemon can be stood up without
editing `network_target.py` at all, by putting it on 25779.** That is a
measurement from the table, and it is the single cheapest fact in this document.

### 2.3 The application and panel ports

| port | who | configurable by | bind default |
|---|---|---|---|
| 5000 | Flask app (`app.py:78`) | `SWAP_TERMINAL_PORT` | `127.0.0.1` (`SWAP_TERMINAL_HOST`) |
| 5000 | Express server (`server.js:80`) | `PORT` | all interfaces (`app.listen(PORT)`) |
| 8765 | operator panel (`operator_panel.py:68`) | `--port` | `127.0.0.1`, **not overridable** |

Literal-port occurrences outside tests and `node_modules`, counted
2026-10-03 -- the denominator is the 16-port pattern set listed, which is a
denominator and not the language (CLAUDE.md's own correction about pattern
counts applies):

```
25715  39     15715  33     25779  25     5000  23
18443   8     18332   8      9876   7    19443   7
 8765   6      9332   5      8332   5    19332   5
 8899   1      3000   1    32749   0    32748   0
```

**`32749` and `32748` -- the Gridcoin p2p mainnet and testnet ports -- occur
ZERO times in this tree.** That is a finding, not an absence of one: nothing
here knows what a GRC p2p port is, so nothing here can refuse, classify or warn
about one. A container topology that gets p2p connectivity wrong produces a
daemon that syncs nothing, and the tree has no vocabulary in which to say so.
`swap_readiness.py` would report the RPC endpoint healthy throughout. **NOT
ESTABLISHED: whether a second GRC daemon on one host can share p2p with the
first, or needs its own `-port`.** What would settle it: `gridcoinresearchd
-help` for `-port`/`-bind`, and `getnetworkinfo`/`getpeerinfo` on both daemons
once the second exists. That is the operator's host, not this one.

---

## 3. SURVEY: every path assumption

### 3.1 The import path IS the directory layout

**Measured: 51 `Path(__file__)` sites outside `tests/`.** The dominant shape, in
**28 of the 34 root `.py` files**, is:

```python
sys.path.insert(0, str(Path(__file__).resolve().parent / "swap_terminal"))
```

and inside the package, three variants: `parents[2]` in
`workers/common.py:472`, `parent.parent` in `workers/*.py` and
`chains/base.py:...`, `parents[2] if root is None` in
`modules/network_selection.py`.

CLAUDE.md rule 10 admits the tree is not arranged suite -> file -> module ->
submodule -> function, and this is the part of that admission with the most
container consequence. **All 34 root `.py` files have a `__main__` block** --
measured, 34 of 34 -- so by rule 10's own definition they are files (entry
points) and they are in the right place. But they reach their modules by
inserting a path computed from their own location, which means:

- A container image that copies `swap_terminal/` **without** the repository root
  gets working modules and zero entry points.
- A container that copies the root scripts **without** `swap_terminal/` gets 34
  scripts that all fail at the first `from config import Config`.
- A container that mounts the repo at a different depth is fine, because every
  path is relative to `__file__` -- this is the one place self-relative
  resolution helps rather than hurts.

The sibling repository's `scripts/diagnostics/mammon_sandbox_verify.py` copies
"the live database AND the live script tree into a disposable temp root -- both
together, since every refresh script resolves its own DB path relative to its
own file location". **That is the same constraint, and it is the correct model
for an image here: the unit that can be copied is the whole repository root, not
a subdirectory of it.**

### 3.2 The database lives inside the source tree

`swap_terminal/config.py:94`:

```python
DB_PATH = _env("SWAP_DB_PATH", str(BASE_DIR / "swap_terminal.db"))
```

with `BASE_DIR = Path(__file__).resolve().parent` (`:...`), i.e.
`swap_terminal/swap_terminal.db`. Measured: the file is present, 139,264 bytes,
`journal_mode=wal`, 11 tables, 0 views.

**The authority database's default location is inside the directory a container
image would bake.** Every container that writes it must therefore have that one
path on a volume, and an image built without one silently gets a fresh empty
database on every `docker run` -- `sqlite3.connect()` creates a missing file.
Three root tools already guard against exactly that locally (`wallet_custody.py`,
`show_unattributable.py`, `show_fees.py` all `Path.exists()` before
`connect()`), and `wallet_custody.py`'s header says why: "a read-only tool that
leaves an empty database behind has written something while announcing that it
would not."

`SWAP_DB_PATH` is read in **20 places across 14 non-test files** (measured by
grep). `workers/common.py:203 database_census()` and `:552 db_path_source()`
exist because three workers were once started from a shell whose `SWAP_DB_PATH`
pointed somewhere else -- the comment at `:209` records it. **That defect
becomes the normal case under containers**, where each container has its own
environment by construction, and `database_census()` is the thing that would
catch it.

### 3.3 Runtime state: pid files, logs, locks, and two `runtime/` directories

- pid files: `swap_terminal/runtime/<name>.pid`, from
  `supervisor.DEFAULT_RUN_DIR = BASE_DIR / "runtime"` (`:125`) and
  `pid_file()` (`:291`). Format is a two-field record (pid, command) --
  `read_pid_record()` (`:295`) -- because the command is what
  `pid_is_still_ours()` compares.
- worker logs: `worker_log_path()` (`:671`), same directory.
- `runtime/kill_switch.lock` -- measured present, 45 bytes, mode `0600`,
  `CONTROL_LOCK_NAME` at `services/kill_switch.py:170`, taken with `fcntl`
  (`:138`).
- **TWO `runtime/` directories one level apart**, which `kill_switch.py:153-162`
  documents at length in its own comment: `supervisor.DEFAULT_RUN_DIR` resolves
  to `<repo>/swap_terminal/runtime`, and there is a second `<repo>/runtime`.
  `.gitignore:37` ignores `swap_terminal/runtime/` and `:20` ignores `*.log`.
- `swap_terminal/regtest/daemons.py` daemon pid files live in each datadir:
  `~/regtest/btc/regtest/bitcoind.pid`, `~/regtest/ltc/regtest/litecoind.pid`,
  per `CHAIN_DEFAULTS[*]["pid_name"]`.
- The GRC datadir is `/home/mpjones26/.GridcoinResearch` -- **an absolute path
  under a specific username**, which appears in prose and hint strings in this
  tree and in the operator's own configuration. A container's user namespace
  does not have that home directory unless it is given one.

### 3.4 The Solana keypair path

`swap_terminal/chains/solana_signing.py:162`
`KEYPAIR_PATH_VARIABLE = "SOL_PAYOUT_KEYPAIR_PATH"`, with two guards worth
naming because they interact with volume mounts:

- `KEYPAIR_FILE_INTEGERS = 64`, `SEED_BYTES = 32` -- a `solana-keygen` keypair
  is a JSON array of 64 integers, "MEASURED 2026-10-02 against
  @solana/web3.js".
- `KEYPAIR_FILE_MAX_BYTES = 4096` -- "so that a path pointing at something large
  -- a log, a database, a wallet dump -- is refused by SIZE before any of it is
  parsed or held in memory".

That second guard is a reason to be careful with volume mounts specifically: a
mount that lands a directory where a file was expected, or a path that resolves
to the wrong file inside a container, is refused rather than parsed. Good. But
`:155-162` also says the Node variable `SOLANA_PAYER_KEYPAIR_PATH` is
**deliberately a different name** from the Python one, because "one name for two
payout paths would mean exporting it armed both of them". A single `env_file`
handed to a compose stack is exactly the mechanism that would undo that
distinction by accident. See §4.

---

## 4. SURVEY: every secret that would have to cross into a container

**Nothing in this section is a value. No secret was read, printed, copied or
echoed. `swap_terminal/grc-sol-swap/abstergo_exchange/wgrc.json` WAS NOT
OPENED.** No `.env` exists in this checkout (measured: `ls .env*` -> no match),
and `swap_terminal/important` and `swap_terminal/test.py` -- the two plaintext-key
files CLAUDE.md names as outstanding -- are **both absent from the working tree
now** (measured). Whether they are still in history is not something this
document establishes; CLAUDE.md says they are and that remediation is the
operator's.

### 4.1 The names, by what they arm

**20 secret-bearing environment variable names, counted 2026-10-03 over the
`.py` and non-`node_modules` `.js`/`.mjs` files in this tree.** The denominator
for the Python half is the 123 `.py` files under `swap_terminal/` plus the 34 at
the root; for the JS half, the 31 non-`node_modules` `.js`/`.mjs`/`.jsx` files
under `grc-sol-swap/`.

**THAT JS DENOMINATOR IS 11, RE-MEASURED 2026-10-04, and it was already wrong
before this re-measurement.** It read 23 the day after this document was written
-- the 2026-10-04 Serum/wGRC cull removed twelve files the day after the count
was taken -- and it is 11 now because the React frontend was deleted when the
two UIs were merged onto the Flask app: eighteen of the thirty-four tracked files
in `abstergo_exchange/`, ten of them `.jsx`, plus `vite.config.js` and
`craco.config.js`.

The 20 names are NOT re-measured here and may have moved with those files; what
is re-measured is the denominator. The surviving eleven are `server.js`,
`auth.js`, `intent_store.js`, `eslint.config.js`, `services/coinGecko.js`,
`services/gridcoin.js`, `scan_solana_wallets.js`, `testAddress.js`,
`rotate_solana_key.mjs` and the two files under `tests/`. None of the deleted
eighteen read an environment variable that armed anything -- the frontend's only
contact with a backend was `fetch` and `axios` against `localhost:5000` -- so
the secret-bearing set is unlikely to have shrunk; "unlikely" is not a
measurement and this sentence is not making one.

This is rule 3's denominator problem in its purest form: the count was true the
day it was taken, nothing recounted it, and two culls moved it the same way
without anyone noticing. `S3. abstergo` below is unaffected -- it still runs
`node server.js`, which never served the frontend and carries no
`express.static`.

Signing and spending -- **these 6 are the ones that can move money**:

| name | read by | what it arms |
|---|---|---|
| `XRP_PAYOUT_SECRET_SEED` | `chains/xrp_payout_seed.py:119` | XRP signing, in-process |
| `GRIDCOIN_WALLET_PASSPHRASE` | `chains/gridcoin_wallet_lock.py:162` | `walletpassphrase` on GRC |
| `GRC_WALLET_PASSPHRASE` | `modules/atomic_grc_client.py:267` | **a SECOND GRC passphrase name** |
| `SOL_PAYOUT_KEYPAIR_PATH` | `chains/solana_signing.py:162` | names a keypair FILE |
| `SOLANA_PAYER_KEYPAIR_PATH` | `server.js:79, :143` | names a keypair FILE, Node path |
| `ST_ADAPTOR_FUNDING_SEED` | `regtest/keys.py`, `operator_panel.py` + 6 more | regtest/testnet funding key |

RPC credentials -- and the Gridcoin password has **4 spellings**, which
`gridcoin_credentials.py` exists to resolve (`GRIDCOIN_PASSWORD_VARIABLES` at
`:66`):

`BTC_RPC_USER`/`BTC_RPC_PASS`, `LTC_RPC_USER`/`LTC_RPC_PASS`,
`GRC_RPC_USER`/`GRC_RPC_PASS`, `GRIDCOIN_RPC_USER`/`GRIDCOIN_RPC_PASSWORD`,
`GRIDCOIN_RPC_PASS`, `RPC_PASS`.

Other: `SECRET_KEY` (Flask session), `GRIDCOIN_VERIFY_SHARED_SECRET` (Node),
`SOLANA_KEYPAIR_PATH` and `PAYER_KEYPAIR_PATH` (two further Node keypair-path
spellings), `COINGECKO_API_KEY`, `COINGECKO_DEMO_API_KEY`,
`COINGECKO_PRO_API_KEY`, `CRYPTOCOMPARE_API_KEY`.

Secret-bearing FILES that would have to cross as volumes, not environment:
the keypair named by `SOL_PAYOUT_KEYPAIR_PATH`; the keypair named by
`SOLANA_PAYER_KEYPAIR_PATH`; `wgrc.json`; and one `wallet.dat` per daemon
datadir -- which is the whole point of §0, since a second GRC datadir means a
second `wallet.dat`.

### 4.2 The narrowings already in place, which a container must not undo

`chains/xrp_payout_seed.py`'s header states five, and they are the standard this
proposal has to meet:

- read from the **environment at use time**, never from a file this code writes,
  **never placed in argv** ("visible in `ps` to every process on the box, and
  kept in shell history"), never logged at any level;
- **never read from `Config`**, because `services/admin_view.py` echoes `Config`
  through an allowlist and "a seed must not be one key away from something that
  renders";
- only `signing_seed()` returns the value, its one caller is the send in
  `services/payout_service.broadcast_payout()`, and there the value "is passed
  straight into `send_to_address()` as an argument expression and is never bound
  to a local name, never put in a dict, and never returned onward";
- **the adapter cannot reach it**: `chains/xrp.py` imports
  `payout_capability()`, `signing_seed_is_present()`, `signing_seed_decodes()`
  and the refusals -- and **not** `signing_seed()`.

### 4.3 The container-specific finding, and it is a real one

**Two live hazards, both measured from the tree rather than hypothesized:**

1. **`docker run --env-file` / compose `environment:` is the exact mechanism
   that defeats the two-names-on-purpose design.** There are two GRC passphrase
   names (`GRIDCOIN_WALLET_PASSPHRASE` for the Flask payout path,
   `GRC_WALLET_PASSPHRASE` for `modules/atomic_grc_client.py`) and three Node
   keypair-path names. A single env file handed to several services sets
   whichever names it contains in **every** container it is given to, so a file
   written to arm the payout worker also arms the atomic-swap client, and a file
   written to arm the Python SOL path does not arm the Node one -- or arms both,
   if whoever wrote the file added both spellings to stop a failure. The
   narrowing that exists today is "which process you exported it in"; an env
   file converts that into "which services share the file", which is a coarser
   control and is invisible in the file itself.
2. **`docker inspect` prints a container's environment.** Any value passed via
   `environment:` or `-e` is readable by anything that can talk to the Docker
   socket, is stored in the container's config JSON on disk, and appears in
   `docker compose config` output. That is weaker than the current posture,
   where a seed exists only in one process's environment and in the operator's
   shell. **PROPOSAL, NOT ESTABLISHED here** -- no Docker to run `inspect`
   against -- but it is documented behavior and the operator should treat it as
   the default until they verify otherwise on their host.

**This is a §7 operator decision and I am not picking it.** But the measurement
that bears on it is: the four narrowings in §4.2 are all about *which process*
holds the value, and container-level environment injection moves the boundary
from the process to the service definition. Any option chosen has to answer
"what can read this, and does `docker inspect` count?"

---

## 5. SURVEY: what the database does under concurrency -- THE HEADLINE FINDING

### 5.1 What is measured, here, today

```
file                swap_terminal/swap_terminal.db    139,264 bytes
PRAGMA journal_mode wal
PRAGMA busy_timeout 5000  (ms)
tables              11
views               0
sidecars present    swap_terminal.db-shm (32,768 B), swap_terminal.db-wal (0 B)
```

**`busy_timeout` is 5000ms because that is Python's `sqlite3.connect()` default,
and NOT because anything in this tree sets it.** Measured: `busy_timeout` occurs
**0 times** in every `.py` file in the repository; and a connection to a
freshly-created temp database reports the same 5000. `db.connect_db()`
(`db.py:760`) is three lines -- `sqlite3.connect(db_path)`, a row factory,
return -- with no `timeout=`, no `PRAGMA busy_timeout`, no
`PRAGMA synchronous`. WAL is set once in `SCHEMA` (`db.py:65`) and persists in
the file header, which is why the measured value is `wal` and not `delete`.

**So the contended-write budget for every writer in this system is 5 seconds of
retry, by default, by accident.** That number is not wrong -- it is a perfectly
reasonable value -- but nobody in this tree chose it, nobody states it in a
banner, and nothing would notice if a Python upgrade changed it.

### 5.2 How many processes write to it today

**Files containing write SQL (`INSERT`/`UPDATE`/`DELETE`), outside `tests/`:
24.** Split by what runs them:

```
in-process modules (imported by a long-lived process)         13
  db.py, deposit_vout_artifact.py, migrate_swap_intents.py,
  swap_intents_schema.py, routes/grc_login.py,
  services/{admin_view, deposit, grc_login, market_context,
            payout, quote, swap, unattributable_deposit,
            xrp_tag}_service.py  (9 services)
workers (their own processes)                                  2
  workers/common.py, workers/payout_worker.py
root one-shot operator tools                                   8
  correct_payout_amounts.py, fund_testnets.py,
  migrate_deposit_vouts.py, open_swap.py, rescue_payout.py,
  resolve_halted_swap.py, settle_payout.py, show_swap.py
```

**OS processes that write concurrently, in the default deployment: 5.**

```
deposit_watcher     1    poll 15s
payout_worker       1    poll 10s
reconcile_worker    1    poll 60s
gunicorn workers    2    GUNICORN_WORKERS default "2" (gunicorn.conf.py:121)
                   --
                    5
```

Plus 1 transient process per root tool invocation, 8 of which write.
**NOT ESTABLISHED: the actual contention rate on the operator's host.** Nothing
in this tree counts `database is locked` -- grepped, 0 occurrences of that string
in any `.py` -- so there is no log line, no counter and no table from which a
rate could be read. What would settle it: a `sqlite3_busy` counter in
`db.connect_db()` or a count of the exception text in worker logs over a known
number of cycles, with the cycle count as the denominator. That is a change to
code and therefore not something this survey document does.

### 5.3 What containerizing changes, and what it does NOT

**IT DOES NOT HELP. On the most likely topology it makes it worse, and this is
the finding this document is organized around.**

Three separate things change, and they stack:

**(a) The writer count does not go down; it goes up per service.** Containers
partition *namespaces*, not *locks*. SQLite's one-writer rule is per database
file, and the file is the same file. A topology of
`web` + `deposit_watcher` + `payout_worker` + `reconcile_worker` containers
sharing one volume has the same 5 writers it has today. A topology that gives
each service its own gunicorn, or that scales `web` to 2 replicas because that
is what container platforms make easy, has 7 or 9. The sibling repository's
measurement is the one to read before assuming a split helps: Mammon split one
contended staging file into four, and the contention **moved rather than
vanished** -- 0 staging lock errors in 387,039 log lines across 51 days on four
files, then 42,976 in 96,083 lines across 18 days on one. CLAUDE.md rule 15
already carries the correct reading of that: "The constraint to satisfy is 'keep
the concurrent writer off the authority's lock,' not 'give everyone their own
file.'" A container boundary satisfies neither half.

**(b) WAL mode requires a shared-memory file and POSIX byte-range locks, and a
volume mount is where those stop being guaranteed.** `swap_terminal.db-shm` is
mmap'd by every connection in WAL mode; the processes sharing it must see the
same inode with working `fcntl` locks. On a Linux host with a bind mount of a
local directory into several containers on the same kernel, that is the same
inode and the same lock table -- **PROPOSAL, and I could not run it.** Where it
is documented to fail: any path that is not a local POSIX filesystem -- NFS,
CIFS, virtiofs/gRPC-FUSE (which is what Docker Desktop on macOS and Windows uses
for host mounts), and anything layered on overlayfs's upper directory rather
than bind-mounted. SQLite's own documentation is explicit that WAL does not work
over a network filesystem. **NOT ESTABLISHED: which of these the operator's host
would be.** What would settle it: `docker info` for the storage driver, and
`stat -f` on the mounted database path inside a running container to read the
filesystem type. One command each, on their host.

**(c) The failure is silent in the direction that costs money.** This is the
part that makes it a headline rather than an operations note. If WAL locking is
broken rather than merely slow, two writers can both believe they hold the write
lock. The thing that currently makes a double payout impossible is **not** the
lock -- CLAUDE.md records that two payout workers already paid one swap twice,
2 sends for 1 `swap_id` -- it is the two mechanisms added afterward:
`services/payout_service.claim_swap_for_payout()`'s claim-by-`UPDATE`, and the
partial unique index `idx_payouts_one_live_per_swap` applied by
`db.apply_migrations()`. **Both of those are enforced by the database engine,
and both assume the engine's locking is sound.** A claim-by-`UPDATE` whose
`changes()` count is read from a connection that did not actually serialize
against the other writer returns 1 to both callers. That is the double payout
again, on chain, final, and arrived at by a volume mount.

**What containers DO fix about the database: nothing. What they fix elsewhere is
real and is in §3 and §6, but this row is a flat no.** The honest summary for
the operator is: the database is the reason to containerize *less* than the
instruction's "all our supervisors, etc" suggests, and §8's staging order puts
every database writer last for exactly this reason.

### 5.4 The one database change that WOULD help, named as work rather than done

`db.connect_db()` setting an explicit `PRAGMA busy_timeout` and an explicit
`timeout=` would replace an accidental 5000ms with a chosen one, and
`workers/common.py`'s startup banner printing it would satisfy rule 14's "echo
the parameters that decide the answer". **That is a code change, this document
writes no code, and it is named here as work rather than deferred to a
baseline (rule 19).** It is also *not* a container prerequisite -- it is worth
doing whether or not anything is containerized, which is the test for whether a
change belongs in this document's scope at all.

---

## 6. SURVEY: what breaks, file by file, with blast radius

### 6.1 `services/kill_switch.py` -- WORST. A stop that reports success and does not stop.

`services/kill_switch.py:146` is `import supervisor`. The web controls at
`/admin/controls` call `supervisor.start_worker()` and
`supervisor.stop_worker()` **in the Flask process**, which means they
`os.kill(pid, SIGTERM)` and then prove absence by polling `/proc/<pid>`.

If the Flask app and the workers are in different containers, **both halves of
that break, and they break in the worst possible combination**:

1. `stop_worker()` reads the pid file -- which a shared volume would still
   deliver -- gets a pid, and calls `process_alive(pid)`. That reads
   `/proc/<pid>/stat` (`supervisor.py:360 _proc_stat_fields`). In a different
   PID namespace that pid either does not exist or **belongs to an unrelated
   process in this container**.
2. If it does not exist: `stop_worker()` returns
   `{"outcome": "not-running", "note": "pid file was stale"}` and **unlinks the
   pid file**. The operator sees a stop that reported success, the pid file is
   gone, and the payout worker is still running in its own container, still
   polling every 10 seconds, still able to broadcast.
3. If it does exist as something unrelated: `pid_is_still_ours(pid, command)`
   compares `/proc/<pid>/cmdline` and returns `stale-pidfile`, which also
   unlinks the file and signals nothing. Same outcome, different path.
4. `unaccounted_workers()` -- the orphan scan, rule 13's second half -- walks
   `PROC_DIR` and would find **zero** worker processes, because they are not in
   this namespace. It would report "no unaccounted workers" with total
   confidence. Its own docstring already records that a stop printed the wrong
   thing on 2026-10-01 and that this function is the fix.

**This is rule 13's exact named defect: "A stop that cannot prove it worked is
not a stop", and a stop that reports `not-running` while the process runs is
worse than one that errors.** It is also rule 14's: "did nothing" rendering
identically to "did work".

Blast radius: `services/kill_switch.py` (~1,100 lines), `routes/kill_switch.py`,
`templates/admin.html`'s controls, `tests/test_kill_switch.py`, and
`supervisor.py`'s `unaccounted_workers`, `process_alive`, `pid_is_still_ours`,
`_proc_stat_fields`, `_proc_argv`, `_proc_cmdline`, `_is_zombie` -- **7 functions
whose entire implementation is "read `/proc`".** There is no small version of
this change.

**The only topologies that do not break it:**

- **(i) Flask and all three workers in ONE container, sharing one PID
  namespace.** Everything above keeps working unmodified. The container's own
  stop reaps pid 1 and the kernel reaps the rest.
- **(ii) Workers in their own containers and the kill switch replaced by
  container-API calls.** That is a rewrite of the mechanism that currently
  proves absence, on the surface that can stop a payout mid-flight, and it must
  prove absence through the container runtime rather than `/proc`. I would not
  propose it as a first increment, and §8 does not.
- **(iii) Share the PID namespace across containers.** This gives back `/proc`
  visibility and gives up most of the isolation that motivated the split.

§7 lists this as an operator decision because option (ii) is a change to how a
payout can be stopped, which is armed state.

### 6.2 `loopback.py` + `kill_switch.refuse_off_box()` -- the controls refuse, permanently

`swap_terminal/loopback.py` answers "what are MY listening sockets bound to" by
reading `/proc/self/fd` and `/proc/net/tcp*` (`socket_inodes()` `:306`,
`listening_addresses()` `:337`). `kill_switch.refuse_off_box()` (`:460`) uses
two non-redundant signals: those kernel-reported bind addresses, and
`SWAP_TERMINAL_HOST` as declared intent. It refuses if **either** says
non-loopback, and it refuses if `listening_addresses()` returns `None`
("could not look"), because "a control surface MUST refuse on it".

In a container with its own network namespace, **the way you publish a port is
to bind `0.0.0.0` inside and let the runtime map it.** So:

- `listening_addresses()` returns a non-loopback host -> `refuse_off_box()`
  refuses -> **the controls page is permanently unusable**, and the refusal is
  correct from inside the namespace and wrong about the world.
- Bind `127.0.0.1` inside the container instead and the controls work, but the
  port is unreachable from the host, so the page cannot be opened at all.
- `--network host` makes both work and removes the network isolation.

That is established from the code. **NOT ESTABLISHED: the exact strings
`/proc/net/tcp` reports inside a container's net namespace.** What would settle
it: run `python3 -c "from loopback import listening_addresses; print(listening_addresses())"`
inside a container with the app listening. One command, on a host with Docker.

Blast radius: `loopback.py` (6 functions), `kill_switch.refuse_off_box()`,
`app.exposure_warnings()`, `gunicorn.conf.py:on_starting`,
`operator_panel.py:1247`, `tests/test_kill_switch.py`. Note that `loopback.py`
*exists* because there were three copies of "is it loopback" (its header counts
them at `app.py:81`, `gunicorn.conf.py:on_starting`,
`operator_panel.py:1204`), so the merge has already happened and there is one
place to change -- which is the good news in this subsection.

### 6.3 `network_target.py` -- port classification. See §2.2.

Blast radius is small in lines and large in consequence: `CHAIN_PORTS` is one
dict, `may_read_a_wallet()` is one function, and its callers are
`swap_readiness.chain_precheck()` and `wallet_custody.py`. **The mitigation is
not to change it: publish ports 1:1 and use 25779 for the second GRC daemon.**

### 6.4 `regtest/daemons.py` -- datadirs, binaries, and a reaper that reads a pid file

`CHAIN_DEFAULTS` assumes:

- the binaries `bitcoind`/`bitcoin-cli` and `litecoind`/`litecoin-cli` are **on
  `PATH`**;
- datadirs are `~/regtest/btc` and `~/regtest/ltc`, i.e. **relative to `$HOME`**;
- the daemon writes `<datadir>/regtest/<pid_name>` and `stop_daemon()` reads it.

In a container, `$HOME` is the container's, the binaries are whatever the image
has, and -- the sharp edge -- **`stop_daemon()` reads a pid from a file and then
`os.kill(pid, 0)`s it.** Same namespace problem as §6.1. If the harness runs in
one container and the daemon in another, the pid in the file is meaningless to
the killer, and §6.1's silent-success failure recurs with a daemon instead of a
worker. The daemon case is less bad -- an orphan daemon holds a datadir lock and
the *next* start fails loudly with "Cannot obtain a lock on data directory",
which `daemons.py`'s own header already names -- but it is the same defect.

`probe_capabilities()` is the part that survives containers cleanly, and it is
worth saying so: it asks the daemon for `getblockchaininfo.chain`,
`getnetworkinfo` build strings, `getwalletinfo.descriptors` and `help <method>`
rather than branching on a version number. **A containerized daemon of an
unknown build is exactly the case that design was for.** `assert_regtest()` runs
immediately after every connection, unconditionally, with no flag to disable it.

Blast radius: `CHAIN_DEFAULTS` (2 entries), `start_daemon`, `stop_daemon`,
`regtest_htlc_verify.py`'s `finally` block, `tests/test_daemon_conf.py` (which a
concurrent agent is editing as of this writing -- see §10).

### 6.5 `wallet_custody.py` and `swap_readiness.py`

Neither breaks *structurally*, and both get **more** useful under the §0
topology, which is the honest positive finding of this section.

`wallet_custody.py` reports, per Bitcoin-derived chain whose port classifies as
TEST: the wallet name from `getwalletinfo().walletname`, and from `listwallets`
"whether a bare CLI call with no `-rpcwallet` would reach that same wallet,
which is the difference between a named wallet and an ENFORCED separation."

**On GRC, where `listwallets` does not exist, that second clause can never be
answered** -- and `wallet_custody.py:279`'s `noqa: BLE001` comment says exactly
that: the absence "costs ONE clause, which the verdict then reports as not
established rather than as an answer." **A second GRC daemon with its own
datadir is the only way GRC's separation becomes establishable at all**, because
then the separation is not "which wallet does this endpoint serve" (unanswerable
without `listwallets`) but "which *port* does this endpoint use" -- and ports are
what `network_target.classify()` already answers. That is the strongest
container argument in this document and it is a *correctness* argument, not a
packaging one.

What would need to change: `services/custody_separation.py` would want a verdict
shape for "separated by datadir rather than by wallet name". Blast radius: that
module's verdict vocabulary (`BY_DESIGN`, `DESK_OWNS`, `NOT_ESTABLISHED`, and
the others `wallet_custody.py` imports), plus `tests/test_custody_separation.py`
-- which exists, measured, and was modified by a concurrent agent while this
survey was being written. **This is a code change and is not proposed here; it
is named.**

`swap_readiness.py` is 80KB and `legs_to_check()` (`:128`) already handles
per-pair scoping because a whole-terminal gate was unsatisfiable on the
operator's host. Containers add one thing it would want to say and cannot today:
**which container each endpoint is in**, because `127.0.0.1:25715` means two
different daemons depending on which namespace the question is asked from.
That is rule 14's "echo the parameters that decide the answer" acquiring a new
parameter.

### 6.6 `config.py` -- import-time environment reads

`config.py:4` says it reads the process environment **at import time**, and
`:38` says that is why `tests/conftest.py` sets `SWAP_DB_PATH` before importing.
Measured: **23 `_env()` calls** in the class body.

That is not broken by containers -- a container sets the environment before the
process starts, which is the ideal case for import-time reads -- but it does
mean **no container can change its chain configuration without a restart**, and
it means a misconfigured container is misconfigured from pid 1 onward with no
recovery path but a restart. Worth stating because the alternative belief ("I
will just export it and retry") is wrong here.

---

## 7. WHAT CONTAINERS SOLVE HERE, AND WHAT THEY DO NOT

Stated as plainly in one direction as the other, because a design document that
only lists wins is a sales document.

### 7.1 Real, structural wins

1. **Custody separation per datadir, on a chain that has no other mechanism.**
   §0 and §6.5. GRC has no `createwallet`; separation requires a second daemon
   with a second datadir; a container is a clean way to express "this daemon,
   this datadir, this port, this credential, this wallet". This is the strongest
   item and it is a correctness win, not a convenience.
2. **Isolating the HTLC harness wallet from the desk wallet.** The harness funds
   contracts with `ST_ADAPTOR_FUNDING_SEED` and spends regtest/testnet coins;
   the desk pays customers. Today those are distinguished by which datadir a
   process points at, and the datadirs are `~/regtest/btc`, `~/regtest/ltc` and
   `/home/mpjones26/.GridcoinResearch` -- **three paths in one user's home
   directory, separated only by a string in an environment variable.** A mount
   namespace makes that separation enforced rather than conventional, and the
   enforcement is the win: a harness container that has no mount for the desk
   datadir *cannot* reach it, however wrong a variable is.
3. **A reaper for the Express server, which has none.** §1.4. Today nothing
   starts, stops or records the pid of a process that holds
   `SOLANA_PAYER_KEYPAIR_PATH` and can sign SOL. A container gives it a stop
   that signals pid 1 and waits -- which is a *better* reaper than the one it
   has, because it has none.
4. **A pid-file-free, provable stop for the gunicorn master.** §1.2. The master
   has no `pidfile` setting today.
5. **The 5000-vs-5000 port collision becomes impossible to hit by accident**,
   because each service has its own network namespace and the published ports
   are declared in one file a reader can see. §1.4.
6. **Reproducible daemon builds.** `probe_capabilities()` already asks the daemon
   what it can do rather than assuming from a version, so a pinned image is a
   thing this tree is prepared to interrogate rather than trust. §6.4.

### 7.2 Things containers do NOT solve, stated as flatly

1. **The shared SQLite writer lock.** §5.3. Not improved; probably worse;
   possibly dangerous if the volume's filesystem does not do POSIX locking
   properly, because the two mechanisms that stop a double payout are both
   enforced by the engine.
2. **A secret still has to reach the process.** §4. Containers change *where the
   value sits on its way in* and add `docker inspect` as a reader. They do not
   reduce the number of secrets (20 names) and they do not make any of them
   unnecessary.
3. **A testnet still has to sync.** A containerized Gridcoin testnet daemon
   starts empty and has to download the chain. **NOT ESTABLISHED: how long, or
   how large.** Nothing in this tree measures either. What would settle it:
   `getblockchaininfo` `blocks`/`headers`/`verificationprogress` sampled over a
   known interval, and `du -sh` on the datadir. The operator's host.
4. **The two systems of record.** `swap_intents.json` is still the Express
   server's authority for a fund-releasing decision (CLAUDE.md rule 15).
   Containerizing the Express server packages that defect; it does not touch it.
5. **Rule 10's layout gap.** 34 root entry points each inserting a
   self-relative import path is unchanged by containers, and §3.1 is why the
   image has to be the whole repository root.
6. **Anything about mainnet reachability, unless it is configured to.** A
   container with default bridge networking has full outbound access. **The
   mainnet refusals in this tree are all port- and network-id-based, not
   firewall-based**: `network_target.may_read_a_wallet()` refuses port 15715,
   and `xrp_send_tagged.refuse_mainnet()` refuses on the server's reported
   `network_id`. A container does not add a layer to that unless the operator
   asks for one, which is §8's third decision.

### 7.3 One claimed benefit I could not establish, said so

"Containers make the stop provable." **NOT ESTABLISHED, and §6.1 is evidence
against it for the specific stop that matters most.** `docker stop` sends
`SIGTERM` to pid 1, waits, then `SIGKILL`s, and `docker wait` returns an exit
code -- which is structurally the same shape as `supervisor.stop_worker()`. But
rule 13's requirement is that **the absence is the assertion**, and whether
`docker stop`'s return proves absence of *every* process in the container, or
only of pid 1, is not something I can test here. What would settle it: start a
container whose pid 1 spawns a child that ignores SIGTERM, `docker stop` it, and
read `docker ps -a` and the host process table. One experiment, on a host with
Docker.

---

## 8. PROPOSED TOPOLOGY, service by service, with the reaper named for every one

**PROPOSAL. Nothing below was run.** Every reaper is named, because rule 13's
first bullet is that a spawn and its reap are one change, and a service whose
stop cannot be proven is a defect in the design rather than a detail to fill in
later.

### S1. `grc-desk` -- the second Gridcoin daemon (the one that justifies this)

- **Runs:** `gridcoinresearchd -datadir=/data -rpcport=25779`, as the DESK's
  wallet, separate from the operator's own daemon on 25715 which **stays exactly
  where it is, uncontainerized** (§8's decision list).
- **Volumes:** `/data` -> a host directory holding this daemon's datadir,
  including its own `wallet.dat` and `gridcoin.conf`. **This is the entire point
  of the service**; without a dedicated datadir volume it is not a second
  wallet.
- **Ports:** `25779:25779` for RPC -- **1:1, chosen because 25779 is already in
  `network_target.CHAIN_PORTS["GRC"].test_ports`, so nothing in the tree has to
  change** (§2.2). p2p: **NOT ESTABLISHED** whether it needs its own `-port`
  (§2.3).
- **Environment:** `GRC_RPC_USER`, `GRC_RPC_PASS` for the consumers; the daemon
  itself reads its `gridcoin.conf` from the volume. **No wallet passphrase ever
  enters this container** -- the unlock is an RPC call made by whoever holds
  `GRIDCOIN_WALLET_PASSPHRASE`, and that is a different service.
- **REAPER:** the container runtime's stop of pid 1, **plus** the existing proof
  shape from `regtest/daemons.stop_daemon()`: read the pid from
  `/data/testnet/gridcoinresearchd.pid` *before* the stop, then poll until it is
  gone. **Do not accept `docker stop`'s exit code as the proof** -- §7.3 says why
  that is unestablished. The datadir lock is the backstop: a daemon that
  survived leaves the next start failing loudly.
- **Blast radius if wrong:** a second daemon on the wrong datadir is a second
  daemon on the operator's own wallet, which is the defect this whole exercise
  exists to fix, arrived at from the other direction. The volume path is the
  highest-stakes single string in the proposal.

### S2. `grc-testnet-sync` -- optional, and only if S1 needs a fresh chain

- **Runs:** nothing new; it is S1 before it has synced.
- Named separately only because §7.2 item 3 is unmeasured and the operator
  should decide whether S1 points at a copy of an existing datadir (fast, and
  needs a `Connection.backup()`-style copy rather than a `cp` of a live datadir)
  or syncs from zero (slow, unmeasured).
- **REAPER:** same as S1.

### S3. `abstergo` -- the Express server

- **Runs:** `node server.js`.
- **Volumes:** read-only mount for the keypair file named by
  `SOLANA_PAYER_KEYPAIR_PATH`, **and nothing else**, if and only if the operator
  decides this process should be armed. Leaving it unmounted leaves the server
  unarmed, which `auth.js:253` already describes as the supported posture:
  "a process that cannot move funds (for server.js, that means leaving
  SOLANA_PAYER_KEYPAIR_PATH unset)". `swap_intents.json` needs a volume if its
  state must survive a restart -- which is rule 15's open defect and not
  something to entrench.
- **Ports:** `PORT` set explicitly, **and not 5000**, so §1.4's collision cannot
  recur. A published port is the operator's §8 decision 4.
- **Environment:** `GRIDCOIN_RPC_HOST`/`GRIDCOIN_RPC_PORT` (or
  `GRIDCOIN_RPC_URL`), `GRIDCOIN_RPC_PASSWORD` or `RPC_PASS`,
  `GRIDCOIN_VERIFY_SHARED_SECRET`, optionally `SOLANA_PAYER_KEYPAIR_PATH`.
- **REAPER:** the runtime's stop of pid 1, with `node` as pid 1 and **no shell
  wrapper**, because a shell as pid 1 does not forward SIGTERM and the Node
  process would be SIGKILLed after the timeout instead of exiting cleanly. That
  is the single most common way a containerized stop silently stops proving
  anything.
- **Why this is the first service to move:** it is the only one with no reaper
  at all today, it writes no SQLite, and it shares no PID namespace requirement
  with anything. §9 puts it first for exactly that reason.

### S4. `harness` -- the regtest BTC/LTC daemons and the HTLC harness, together

- **Runs:** `bitcoind` and `litecoind` **and** the harness that starts and stops
  them, in **ONE container**, because `regtest/daemons.stop_daemon()` proves
  absence by `os.kill(pid, 0)` on a pid from the daemon's own pid file and that
  requires one PID namespace (§6.4).
- **Volumes:** `~/regtest/btc` and `~/regtest/ltc` equivalents. Regtest coins
  are worthless, so this is the lowest-stakes volume in the proposal.
- **Ports:** `18443` and `19443`, 1:1, both already in `CHAIN_PORTS.test_ports`.
  **Publishing them at all is optional** -- if the harness is in the same
  container it reaches them over the container's own loopback, and not
  publishing them is strictly safer.
- **Environment:** `ST_REGTEST_BTC_*`, `ST_REGTEST_LTC_*`,
  `ST_ADAPTOR_FUNDING_SEED` **if and only if** a funding step runs.
- **REAPER:** unchanged -- `stop_daemon()` from the `finally` block in
  `regtest_htlc_verify.py`'s `main()`, which already reads the pid file first and
  polls for absence. Plus the container stop as an outer backstop. **The inner
  reaper is the one that proves it; the outer one is the one that catches the
  case where the harness itself died.** Two reapers is correct here and is not
  duplication, because they prove different things.

### S5. `web` + `workers` -- ONE container, and this is the load-bearing choice

- **Runs:** `gunicorn -c gunicorn.conf.py wsgi:app` **and** the three supervised
  workers, started by `swap_terminal/supervisor.py start`, **in one PID
  namespace.**
- **Volumes:** the directory containing `swap_terminal.db` (and its `-wal` and
  `-shm` sidecars -- all three are one unit and must be one mount);
  `swap_terminal/runtime/` for pid files, logs and `kill_switch.lock`; a
  read-only mount for the keypair named by `SOL_PAYOUT_KEYPAIR_PATH` if SOL
  payouts are armed.
- **Ports:** `SWAP_TERMINAL_PORT`, and see §6.2 -- **the bind address is a
  decision the operator has to make, because `refuse_off_box()` reads the bind
  from `/proc` and will refuse a `0.0.0.0` bind.** This is not solvable inside
  the topology; it is decision 4 below.
- **Environment:** `SWAP_DB_PATH` pointed at the volume **explicitly**, never
  left to the `BASE_DIR` default (§3.2); `SWAP_TERMINAL_HOST`,
  `SWAP_TERMINAL_PORT`, `GUNICORN_WORKERS`; the per-chain RPC credentials and
  ports; and the signing secrets **only if** this is the armed deployment.
- **REAPER:** `supervisor.stop_worker()` for each of the three workers --
  unchanged, still proving absence by `/proc`, which **works precisely because
  they are in this container** -- then the runtime's stop of pid 1 for gunicorn.
  `unaccounted_workers()` still works for the same reason.
- **WHY NOT FOUR CONTAINERS.** §6.1. Splitting the workers out of the Flask
  container breaks `services/kill_switch.py` in the direction where a stop
  reports success and the payout worker keeps running. **A topology that makes
  the payout stop unprovable is not a topology, whatever else it improves.** The
  database argument (§5.3) points the same way: four containers on one SQLite
  file is four writers plus a volume-mount lock question, for zero measured
  benefit.
- **Honest cost of this choice:** one container runs both the web surface and
  the process that signs. That is a *worse* blast radius than four containers
  would give, if four containers worked. They do not, today, without rewriting
  the stop mechanism. The right order is: make the stop provable across a
  boundary first, then split. Not split and then discover the stop.

### S6. `operator-panel` -- stays on the host, and that is a recommendation

- `operator_panel.py` binds `127.0.0.1` from a module constant with **no
  `--host` flag**, deliberately, because "a page with buttons should not have an
  override to lose". Containerizing it means either binding `0.0.0.0` inside
  (deleting the guard that is the reason the constant exists) or publishing
  nothing (making the page unreachable).
- It also spawns harness entry points by pid and kills them by pid -- the same
  one-PID-namespace requirement as S4, so if it moves, it moves *into* S4.
- **Recommendation: do not containerize it.** Its reaper
  (`regtest/harness_runner.py`, killed by pid, absence asserted) is already
  correct, and nothing in §7.1 applies to it. **REAPER today and under this
  proposal: `harness_runner.stop()` plus process exit for the
  `serve_forever` daemon thread.**

### S7. Root operator tools -- not services, and they must keep running on the host

34 root `.py` entry points, 8 of which write to the database. They are
interactive, they are pasted into a terminal, their output is read off a screen
(rule 14, and CLAUDE.md's "the operator runs commands on their own machine and
pastes the output back"). **A tool that has to be run as `docker compose exec`
is a tool whose output the operator has to dig out of a container**, and
`wallet_custody.py` / `swap_readiness.py` / `show_swap.py` are read far more
often than anything else here.

They need the database, which is in S5's volume. **The operator decision is
whether that volume is also bind-mounted at a host path they can point
`SWAP_DB_PATH` at.** If it is, every root tool keeps working unchanged, which is
worth a great deal. **REAPER: none needed -- they exit.** But note §5.2: 8 of
them write, so a root tool run during a payout is a sixth concurrent writer.

---

## 9. STAGING ORDER -- increments, in cost order, each independently verifiable

Rule 10 refuses a mass reorganization on a live-money system, and this honors
that: **five increments, smallest blast radius first, each with the command that
proves it worked.** No increment depends on a later one. Any of them can be the
last one.

### Increment 1 -- `abstergo` (S3). Lowest cost, clearest win.

Why first: it touches no SQLite, has **no reaper at all today**, and shares no
PID namespace with anything. The only thing that can regress is a process that
currently nothing can stop.

Proves it worked:

```
docker compose up -d abstergo
docker compose ps abstergo                 # expect: running, and the published port
curl -s localhost:<PORT>/<a GET route>     # expect: a response from THIS server
docker compose stop abstergo
docker compose ps -a abstergo              # expect: exited
pgrep -af 'node .*server\.js'              # expect: NOTHING. the absence is the assertion.
```

That last line is the increment. It is the first time in this repository's
history that the Express server's stop can be proven.

### Increment 2 -- `harness` (S4). Regtest coins are worthless; the harness is self-reaping.

Why second: the stakes are regtest, `assert_regtest()` is unconditional, and the
existing reaper already proves absence inside one PID namespace, which this
increment preserves by putting the daemons and the harness in one container.

Proves it worked: run the real harness (`regtest_htlc_verify.py`) to completion
inside the container, then from the host:

```
docker compose exec harness bitcoin-cli -regtest getblockchaininfo | grep '"chain"'
    # expect: "regtest"  <- if this is not regtest, stop. assert_regtest() should have refused.
docker compose stop harness
docker compose ps -a harness               # expect: exited
pgrep -af 'bitcoind|litecoind'             # expect: NOTHING on the host
ls <host path>/regtest/btc/regtest/bitcoind.pid   # expect: absent, or naming a dead pid
```

### Increment 3 -- `grc-desk` (S1). The one the operator actually asked for.

Why third and not first: it is the highest-value increment and also the one
whose volume path has the worst failure mode (§8 S1's blast radius). Doing 1 and
2 first buys a measured, local answer to "does a bind-mounted datadir work on
this host" before that question is asked about a wallet.

Proves it worked, in this order, **and the first command is the one that
matters**:

```
# 1. Is this a DIFFERENT wallet from the operator's own daemon?
python3 wallet_custody.py
    # expect: GRC line naming port 25779 and a wallet that is NOT the one 25715 serves.
    # Note the limit wallet_custody.py prints for itself: with no listwallets on
    # Gridcoin, "whether a bare CLI call also reaches this wallet was not
    # established". The separation here is BY DATADIR, which is why this daemon
    # exists; the tool cannot say that word yet (§6.5).

# 2. Did the tree accept the port without being edited?
python3 -c "import sys; sys.path.insert(0,'swap_terminal'); \
  from network_target import classify, may_read_a_wallet; \
  print(classify('GRC', 25779), may_read_a_wallet('GRC', 25779))"
    # expect: TEST, (True, 'port 25779 is a test chain (mainnet is 15715)')
    # If this says UNRECOGNIZED, the port is wrong -- do NOT edit CHAIN_PORTS (§2.2).

# 3. Is the operator's own daemon untouched?
<their own check on 25715>              # expect: still running, balance unchanged

# 4. The reaper.
<read the pid from the datadir pid file FIRST>
docker compose stop grc-desk
<poll kill -0 that pid until it fails>  # the absence is the assertion
```

### Increment 4 -- `web` + `workers` as ONE container (S5). The database increment.

Why fourth: every claim in §5.3 is unestablished, and this is the increment that
would establish them. Before moving anything, **on the operator's host**:

```
docker info | grep -i 'storage driver'
docker compose exec web stat -f -c '%T' "$SWAP_DB_PATH"
    # expect a local POSIX filesystem (ext4/xfs/btrfs). If this says fuse,
    # virtiofs, nfs or 9p, STOP: WAL locking is not sound there and §5.3(c)
    # is in play on the mechanism that prevents a double payout.
```

Then, with a **copy** of the database and the payout worker **not armed**:

```
docker compose up -d web
docker compose exec web python3 swap_terminal/supervisor.py status
    # expect: all three workers, with pids, and the endpoint banner naming the
    # network per chain (rule 14). (none) is a result; a blank is a defect.
curl -s localhost:<port>/admin/controls | grep -i 'refus'
    # THE §6.2 CHECK. If the controls refuse because the bind reads as
    # non-loopback, that is the measured answer to decision 4 below.
docker compose exec web python3 swap_terminal/supervisor.py stop
docker compose exec web python3 swap_terminal/supervisor.py status
    # expect: not-running for all three, AND no unaccounted workers.
    # unaccounted_workers() is the assertion; it only works because this is one
    # PID namespace (§6.1).
```

Only after all of that: `docker compose stop web`, then `pgrep -af
'deposit_watcher|payout_worker|reconcile_worker'` on the host, **expecting
nothing**.

### Increment 5 -- NOT PROPOSED: splitting the workers out of `web`.

Named here as the increment this document declines to propose, so that nobody
reads its absence as an oversight. It requires replacing the `/proc`-based proof
of absence in `services/kill_switch.py` and 7 functions of `supervisor.py` with
something that proves absence through the container runtime, on the surface that
stops a payout. **That is a change to how armed state is stopped and it is
rule 16's second row.** The sequence that would make it safe is: prove absence
across a container boundary first, with a test, with the payout worker unarmed;
then split. Not split and then discover.

---

## 10. EVERY DECISION THAT IS THE OPERATOR'S

Rule 16 puts these on their side of the line, and rule 20's "do not ask which"
explicitly does not move them: "the line that still comes back is rule 16's and
it has not moved -- fund movement, armed state, secrets, and a fix you cannot
test here." **This document cannot test any container at all**, so the whole of
it is a proposal; these five are additionally the operator's on the merits.
Options with the trade-off measured where it could be measured.

### Decision 1 -- How do secrets reach a container?

The 6 money-moving names are in §4.1. The narrowings they have today are in
§4.2. The two container-specific hazards are in §4.3.

- **(a) `environment:` / `-e` in a compose file.** Simplest. Readable by
  `docker inspect`, stored in the container config on disk, printed by
  `docker compose config`. **Weaker than today's posture.**
- **(b) `--env-file`.** Same exposure as (a) once the container runs, plus the
  §4.3 item 1 hazard: one file sets whichever of the **two** GRC passphrase
  names and **three** Node keypair-path names it contains, in every service it
  is given to, silently arming or failing to arm paths the author was not
  thinking about.
- **(c) A mounted file, read at use time.** The keypair paths are already this
  shape, and `solana_signing.KEYPAIR_FILE_MAX_BYTES = 4096` already refuses a
  wrong path by size before parsing. **But `XRP_PAYOUT_SECRET_SEED` and the GRC
  passphrase are environment-read by design, and `xrp_payout_seed.py`'s header
  gives five reasons that are each still true.** Changing them to file reads is
  a change to the signing path.
- **(d) Nothing. Leave the armed services off containers entirely** -- which is
  what §8 S6 and S7 already recommend for the panel and the root tools.

**Measured input to this decision:** 20 secret-bearing names; 4 spellings of one
Gridcoin RPC password; 2 of one GRC wallet passphrase; 3 Node keypair-path
names. The spelling multiplicity is the measured argument against (b)
specifically.

### Decision 2 -- Do the daemons move at all?

- **(a) None move.** The operator's GRC daemon on 25715 stays; the second desk
  daemon is also started by hand on 25779 with its own datadir. **This gets the
  entire §0 custody win with zero containers.** It is the honest baseline and it
  should be compared against, not skipped.
- **(b) Only the NEW desk daemon moves (§8 S1).** The win over (a) is an
  enforced datadir boundary rather than a conventional one, plus a reproducible
  build that `probe_capabilities()` is already designed to interrogate.
- **(c) The regtest daemons move too (§8 S4).** Lowest stakes, and §9 puts it
  second for that reason.
- **(d) The operator's own daemon moves.** **I would advise against it and it is
  still theirs.** It holds their real staking balance; it is on 25715, which is
  in `test_ports` so the tree will read it; and moving it is a change to a wallet
  that is not the desk's.

### Decision 3 -- Is mainnet reachable from any container?

Measured: this tree's mainnet refusals are **port-based** (`CHAIN_PORTS`,
mainnet 8332/9332/15715) and **network-id-based**
(`xrp_send_tagged.refuse_mainnet()` asks the server for `network_id`;
`solana_signing.SolanaClusterRefused` checks the genesis hash). **None of them is
a firewall.** A container with default bridge networking reaches every mainnet
RPC endpoint on the internet.

- **(a) Default networking.** Unchanged posture; the refusals are the only guard.
- **(b) `internal: true` networks, or no network, for every container that does
  not need outbound.** This would be a genuinely NEW layer -- the first
  non-code mainnet guard in the system. **NOT ESTABLISHED: which services need
  outbound.** The price APIs (`COINGECKO_*`, `CRYPTOCOMPARE_API_KEY`) do; the
  XRP and SOL endpoints are URLs and may be remote; the Bitcoin-derived chains
  are `127.0.0.1` by default (`BTC_RPC_HOST` etc., `config.py:445, 453, 518`).
  What would settle it: run each service with outbound blocked and read which
  `swap_readiness.py` lines fail.
- **(c) An egress allowlist.** Strongest, most work, and a wrong entry presents
  as a chain that is down.

### Decision 4 -- Is the UI exposed beyond localhost?

**This one is forced into the open by §6.2 rather than being a preference**, and
that is the measured input: `kill_switch.refuse_off_box()` reads the bind from
`/proc/net/tcp` and **refuses the controls page if the bind is not loopback**.
In a container with its own net namespace, publishing a port means binding
`0.0.0.0` inside.

- **(a) Bind `0.0.0.0` inside, publish to `127.0.0.1` on the host.** The page is
  reachable at `localhost` and **the controls refuse**, correctly from inside the
  namespace and wrongly about the world. The desk's kill switch stops working.
- **(b) `--network host`.** Everything works unmodified, including
  `refuse_off_box()` and `/proc`. Gives up network isolation. **This is the
  option that preserves every guard in the tree as written**, and it should be
  read as the conservative choice rather than the lazy one.
- **(c) Keep the controls off the containerized surface.** Run the Flask app in
  a container without the controls blueprint; stop workers with
  `supervisor.py stop` by hand. Loses the web kill switch.
- **(d) Teach `loopback.py` about containers.** A change to the module that
  decides whether a button that can stop a payout mid-flight is allowed to
  render. **Armed state. Not mine, and not a small change** -- §6.2 lists 6
  call sites.

The operator panel is a separate question with a simpler answer: it has no
`--host` by design, and §8 S6 recommends leaving it on the host.

### Decision 5 -- Is the database volume also a host path the root tools can reach?

§8 S7. 34 root entry points, 8 of them writers, all of them read off a terminal.

- **(a) Bind-mount to a host path.** Every root tool keeps working with
  `SWAP_DB_PATH` set to that path. **Adds a sixth concurrent writer whenever one
  runs** (§5.2), and the writer is a process whose connection has the same
  accidental 5000ms `busy_timeout` as every other (§5.1).
- **(b) A named volume only, tools run via `exec`.** One fewer host writer;
  every diagnostic's output now has to be dug out of a container, against the
  grain of how this operator works.

---

## 11. THE RISKS, WORST FIRST

### R1. A stop that reports success while the payout worker keeps broadcasting.

§6.1. `services/kill_switch.stop_everything()` -> `supervisor.stop_worker()` ->
`os.kill` + `/proc` polling. Across a PID-namespace boundary this returns
`not-running`, **unlinks the pid file**, and the worker keeps polling every 10
seconds with the ability to sign and send. The operator sees a successful stop.
The orphan scan that exists to catch exactly this (`unaccounted_workers()`)
reports clean, because it is looking at the wrong `/proc`.

This is the worst risk in the document because it is the conjunction of every
failure mode CLAUDE.md names: an orphan that crashes nothing (rule 13), a
"did nothing" that renders as "did work" (rule 14), and a process that can
broadcast an irreversible transaction. **Mitigation: §8 S5 -- web and workers in
one container -- and §9's refusal to propose increment 5.**

### R2. A broken write lock on a volume mount, and a double payout.

§5.3. The two things preventing a double payout --
`claim_swap_for_payout()`'s claim-by-`UPDATE` and the partial unique index
`idx_payouts_one_live_per_swap` -- are both enforced by the SQLite engine and
both assume sound locking. WAL requires a shared `-shm` and working POSIX
byte-range locks; those are documented not to work on network and FUSE
filesystems, which is what Docker Desktop host mounts are. CLAUDE.md already
records that this system has paid one swap twice: 2 sends, 1 `swap_id`, 2
`broadcast` rows. **Mitigation: the `stat -f` check in §9's increment 4, run
BEFORE any database writer moves, and treat a non-local filesystem as a stop
rather than a warning.**

### R3. A secret widened by an env file, or read back by `docker inspect`.

§4.3. 20 secret names, 4 spellings of one password, 2 of one passphrase, 3 Node
keypair-path spellings. An env file arms by name across every service it is
given to, and `xrp_payout_seed.py`'s narrowings are all about *which process*
holds the value. CLAUDE.md records how a credential reached GitHub here already
-- a `.env.bak` -- and the conclusion it drew is the one that applies:
"**the backup habit is what leaked the credential**... the habit is what
actually has to stop." An env file committed beside a compose file is the same
habit with a different extension. **Mitigation: decision 1, taken explicitly,
and `.gitignore` coverage verified before any compose file exists.**

### R4 (named, below the top three, because it is loud rather than silent).

A container datadir volume pointed at the operator's own `.GridcoinResearch`.
That would make the second daemon a second process on the wallet this exercise
exists to separate from. It is R4 rather than R1 because it fails **loudly**:
`gridcoinresearchd` cannot obtain a lock on a datadir another instance holds, so
the second daemon refuses to start. The lock is the mitigation, and
`wallet_custody.py` is the check that would catch it if the lock somehow did
not (§9 increment 3, command 1).

---

## 12. What this document established, and what it did not

Established here, by reading or running something in this repository:

- 9 process kinds; 3 supervised workers with pid files and a stop that polls for
  absence; 2 of 3 Bitcoin-derived chains with a reaper in this tree; 1 of 3
  (GRC) with none; 1 server (Express) with no reaper, no pid file and no stop
  path.
- `CHAIN_PORTS` classifies 3 of 5 chains; `classify()` has 4 answers;
  `may_read_a_wallet()` refuses 3 of them; 25779 is already in GRC's
  `test_ports`, so a second GRC daemon needs no edit to `network_target.py`.
- `32749`/`32748` occur 0 times in the tree.
- Flask and Express share a default port of 5000; 23 occurrences of that literal
  across 10 non-test files.
- 51 `Path(__file__)` sites outside `tests/`; 34 of 34 root `.py` files have a
  `__main__`; 28 of 34 insert a self-relative import path.
- `SWAP_DB_PATH` defaults inside the package directory; read in 20 places across
  14 non-test files.
- `journal_mode=wal`, `busy_timeout=5000`, 11 tables, 0 views. **`busy_timeout`
  occurs 0 times in any `.py`** -- the 5000 is Python's default, not a choice.
- 24 non-test files contain write SQL; **5 OS processes write concurrently in
  the default deployment** (3 workers + `GUNICORN_WORKERS` default 2), plus one
  per invocation of 8 root tools.
- 20 secret-bearing environment variable names; 4 spellings of the Gridcoin RPC
  password; 2 of the GRC wallet passphrase; 3 Node keypair-path names. **No
  value of any of them was read.** `wgrc.json` was not opened. No `.env` exists
  in this checkout.
- `services/kill_switch.py` imports `supervisor` and calls its start/stop in the
  Flask process; `supervisor.py` has 7 functions whose implementation is reading
  `/proc`; `loopback.py` has 6 functions and 2 of them read `/proc`.

**NOT ESTABLISHED, with what would settle each:**

| claim | what would settle it |
|---|---|
| anything about container runtime behavior | a host with Docker. None here. |
| WAL locking on the operator's volume | `docker info` storage driver; `stat -f` on the mounted db path inside the container |
| the contention rate today | a `database is locked` counter, with cycles as the denominator. 0 occurrences of that string in any `.py` today. |
| `/proc/net/tcp` contents inside a net namespace | run `loopback.listening_addresses()` in a container with the app listening |
| whether `docker stop` proves absence of every process | a container whose pid 1 spawns a SIGTERM-ignoring child; stop it; read `docker ps -a` and the host process table |
| GRC testnet sync time and datadir size | `getblockchaininfo` sampled over a known interval; `du -sh` |
| whether a second GRC daemon needs its own p2p `-port` | `gridcoinresearchd -help`; `getpeerinfo` on both |
| that Gridcoin v5.5.1.0 lacks multiwallet RPCs | **the operator measured this on their host 2026-10-03. I did not and could not re-run it.** |
| gunicorn's behavior under any of this | gunicorn is not installed here; `app.py`'s own `__main__` comment says the same about its logging |

## 13. The suite, and the conditions this was written in

This document adds one file and changes none, so it cannot change behavior --
but the suite was run anyway, because "it cannot have broken anything" is a
reason to believe and not a measurement (rule 17).

```
PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -q      exit 0
  collected   3229
  passed      3225
  skipped        4
  failed         0
  errors         0
test files in tests/   117
```

**The brief for this work recorded a baseline of 3184 collected, exit 0, and
noted that concurrent agents may have moved it. They did, and the difference is
attributed rather than assumed (rule 3):**

```
3229  collected now
 -45  tests/test_correct_payout_amounts.py  <- UNTRACKED in git, i.e. not mine
      and not in the baseline. Measured by collecting that file alone: 45.
----
3184  exactly the recorded baseline.
```

So the count reconciles to the digit, and nothing is left to a plausible
reading. `tests/test_daemon_conf.py` is also modified in the working tree but
collects 15 both now and at `HEAD`, so it does not move the total.

**Two other agents were editing this tree while this was written**, which is why
the attribution above is stated rather than skipped. `git status` at the time of
the survey showed `swap_terminal/chains/solana.py`,
`swap_terminal/chains/xrp_payments.py` and `tests/test_daemon_conf.py` modified
and `correct_payout_amounts.py`, `swap_terminal/chains/payout_on_chain.py` and
`tests/test_correct_payout_amounts.py` untracked; by the time the suite was run,
`tests/test_custody_separation.py` had been modified as well. Every line number
and every count above is as of that state, and `tests/test_daemon_conf.py` in
particular bears on §6.4.

`python3 -m ruff check` was not run, because this change touches no Python
(rule 12 asks for the files you TOUCHED, and there are none).

A line number is a citation that rots on every edit above it, and
`network_target.configuring_variable()`'s own docstring records three that
rotted and were deleted rather than refreshed. Prefer the function name.
