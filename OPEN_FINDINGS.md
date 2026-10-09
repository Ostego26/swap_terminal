# Open findings

Operator, 2026-10-09: *"just keep a list of everything i keep finding, homie."*

A file and not a chat message, because a chat message scrolls and a session ends.
Each entry says what was measured, who owns it, and what it costs if left. When
one is fixed it moves to **Closed** with the commit, rather than being deleted —
the drift is the point (rule 1), and a list that only shows what is left cannot
be checked against what was claimed.

**Owner** is either `operator` (live posture, armed state, secrets, or a host
this session cannot reach — rule 16) or `claude` (everything else).

---

## Open — operator

### 1. BTC and LTC daemons are bound loopback-only
**Measured** on the host 2026-10-08:

```
LISTEN  127.0.0.1:18443   BTC
LISTEN  127.0.0.1:19443   LTC
LISTEN          *:25779   GRC   (bind is fine)
```

The container reaches the host as `172.17.0.1` and sends from `172.18.0.0/16`.
After the ufw rule, BTC and LTC answer `ConnectionRefused` in 0.00s — fast, so no
longer a freeze, but still unreachable.

**Remedy**, in `bitcoin.conf` and `litecoin.conf`, **inside the `[regtest]`
section if one exists** — Bitcoin Core since 0.17 only applies these to a
non-mainnet chain from that chain's section, and warns rather than failing, so a
global placement looks applied and is not:

```
rpcbind=127.0.0.1
rpcbind=172.17.0.1
rpcallowip=127.0.0.1
rpcallowip=172.18.0.0/16
```

**Costs while open**: every pair sourcing or paying out in BTC or LTC refuses at
`create_swap`, after the quote. Deposits on those chains are not watched.

**RE-MEASURED 2026-10-09 FROM INSIDE THE CONTAINER** (`swap_stack.py chains`),
because I had read `/admin`'s "configured / tradeable YES" as a reachability fact
and told the operator finding 1 "may be resolved." It is not. Configured is not
reachable, and the two live in different columns for that reason:

    BTC  did not answer: ... port=18443 ... [Errno 111] Connection refused
    LTC  did not answer: ... port=19443 ... [Errno 111] Connection refused
    GRC  did not answer: 403 Client Error: Forbidden
    XRP  answered

Only XRP answers. Three of four are unreachable, and BTC/LTC fail DIFFERENTLY
from GRC: `Connection refused` means the socket is not accepting at all (bound
loopback-only -> `rpcbind`), where GRC's `403` means it accepted and declined the
caller by IP (-> `rpcallowip` alone). Different remedies, and the report was
truncating off the word that distinguished them until C27 below.

### 2. GRC rejects the container's RPC with 403
**Measured**: `did not answer: 403 Client Error: Forbidden for url:
http://host.docker.internal:25779/`. Socket open, RPC refused — so `rpcallowip`,
not the bind. **Remedy**: `rpcallowip=172.18.0.0/16` in `gridcoinresearch.conf`.

### 3. The ufw rule names a subnet docker can reassign
`172.18.0.0/16` was read from `docker network inspect` on 2026-10-09. Docker
assigns compose network subnets when it creates them, so a `down` + `up` can hand
out a different one and the rule goes **silently** stale — straight back to
dropped packets and 30s hangs, with no new symptom to explain it.
**Remedy**: pin the subnet in `docker-compose.yml`. Not done: it recreates the
network and both containers. Say the word.

### 4. That rule gives every container on the bridge wallet RPC
`docker-compose.yml` also defines `abstergo` and `harness`, and `up`'s own banner
warns a bare `docker compose up` starts the test harness. On that bridge the
harness would reach the hot wallets exactly as the web container does.

### 5. `/admin` and `/` share one port
The links between the two surfaces are gone (`125cacd`) and typing `/admin` still
reaches it. The operator's words were "the operator page will be a secure page
locally". Three ways, each changing how the kill switch is reached:
a second gunicorn on its own loopback port; a loopback-only `before_request` on
`admin`+`kill_switch`; or a shared secret from the environment.

### 6. Standing items from earlier in the session
- `GRC_FEE_SWEEP_DESTINATION` unset against 187.42147411 GRC accrued.
- GRC's unconditional `can_spend = True`.
- `payout_service.payout_unlock_context()` locks before it proves.
- `s_d503f64d6c5e601f` (9,049.69 GRC against a 2,000 GRC desk) still unrevived.

---

## Open — claude

### 7. `create_swap()` makes 2–3 unbounded chain RPCs
`swap_service.py:745` (payout address), `:386` (deposit account) or `:332` plus
`get_new_address` — each at the adapter's 30s default, against gunicorn's 60s
worker timeout. Same shape as the price path (`67589aa`) and the chain probe
(`e03a006`), both of which are now bounded.

**Not fixed because it is the order path** (rule 16): a deadline that aborts
partway could leave a swap row written with its deposit address unallocated,
which is worse than slow. No chain daemons here to test against, so anything
written would be a proposal. Less urgent since the firewall fix — this only hangs
when a daemon *drops* rather than refuses — but a daemon can wedge while
listening.

### 8. pyright: 488 → 6, and the six are decisions — DONE
Surfaced by `f01e85c`, which removed 53 FALSE import errors that were burying
them. The first inventory here counted `*.py` AT THE ROOT ONLY and said 87
across 18 files. **That denominator was wrong, and it was wrong in the direction
that matters** (rule 3: a count without what it was counted out of has caused
real errors here): running pyright over the whole tree on 2026-10-09 gives

```
  223  reportArgumentType
  163  reportAttributeAccessIssue
   33  reportOptionalMemberAccess
   17  reportIncompatibleMethodOverride
   16  reportOptionalSubscript
   12  reportOperatorIssue
    7  reportReturnType
    7  reportCallIssue
    4  reportOptionalOperand
    3  reportAssignmentType
    2  reportPossiblyUnbound
    1  reportIndexIssue
  ---
  488  across 94 files
```

292 of the 488 are in `tests/`, which the root-only run could not see at all.
The largest single file is `swap_terminal/transactions.py` at 65.

**Not all 488 are bugs and most are not**, and the shape of the majority is now
established rather than guessed. Three root causes account for most of it:

| root cause | roughly |
|---|---|
| `Config.RPC` typed `dict[str, dict[str, object]]`, splatted into four different adapter `__init__`s | ~50 |
| test stub consoles (`_Recorder`, `_QuietConsole`, `_PricingRecorder`) passed where a real `Console` is declared | ~25 |
| `swap_terminal/transactions.py`'s one typing gap, repeated | up to 65 |

`Config.RPC`'s own comment states the contract — "each dict is built for the
`__init__` signature it is splatted into" — and nothing verifies it. A renamed
key is a runtime `TypeError` found by whichever tool splats it first. Writing
that contract as four `TypedDict`s makes it checked, which is the improvement;
the silenced squiggle is a side effect.

**DONE, 2026-10-09: 488 → 85, across 94 files → 17.** Eight agents on
file-disjoint buckets, under a written mandate: no `type: ignore`, no
`pyright: ignore`, no `cast()` added to silence a finding, no widening a parameter
to `object`/`Any`, no new baseline (rule 19), and in tests no assertion weakened —
annotations may change, behavior may not. **Zero suppressions and zero baseline
entries were added by any of the eight.**

| bucket | | before | after |
|---|---|---|---|
| A | `Config.RPC` and its consumers | 53 | **0** |
| B | the library under `swap_terminal/` | 97 | 6 |
| C | atomic-swap tools | 25 | **0** |
| D | operator panel and reports | 21 | 3 |
| E | Solana tests | 77 | 31 |
| F | panel and price tests | 71 | 30 |
| G | atomic/XRP/HTLC/address tests | 78 | 16 |
| H | the long tail of `tests/` | 61 | 22 |

Commits `8287f1c`, `7ced839`, `35a1e77`, `8425058`, `97a1321`, `8f78ad7`,
`e7195cf`, `7e41762`, plus `e12d539`, `da4a3fa`, `f903e0b`, `533b6ba`.
Full suite after: **4109 passed, 3 skipped, 0 failed.**

**Round two landed the Protocols: 85 → 6** (`fa8f564`, `f5eb911`). Almost every
round-one remainder was ONE shape — a production function declaring a concrete
class for a parameter whose body uses two or three of its members, so a
deliberately-partial test stub is refused. Five agents converged on `Protocol`
independently, and three of them had designed the SAME protocol under three
different names; merging that before handing it out is why only one shipped.

Four of the five round-two agents were killed mid-run by a weekly rate limit,
after editing and before self-verifying. Their tree passed 4110 tests and ruff and
measured 7; the verification they never did, and the last finding, are in
`f5eb911`.

**The six that remain are each a decision, not a backlog:**

| where | why it stays |
|---|---|
| `identity.py:442,471`, `htlc_spend.py:344`, `keys.py:286` | the `ecdsa` stub — **yours**, see #15 |
| `transactions.py:360` | pandas attaches `DatetimeIndex.normalize` from a decorator. Typing the parameter `Any` makes it VANISH; that clean zero was given back, because both callers pass an `Index`. A true type with one honest finding beats a false type with none |
| `test_solana_transaction.py:411` | `parse_transfer_transaction`'s input is an artifact this program just produced. Widening to `object` would remove real checking on a **signing path** to satisfy one test asserting a refusal |

Measured along the way and worth keeping: **pyright does not error on a TypedDict
subscripted with a `str` variable — it silently yields `Unknown`**, so five
findings would have gone to zero with nothing checked. And **pyright does not
credit `__getattr__` toward structural matching**, which is why
`SaysEachLineOnce`'s three forwarders are now spelled out.

The subset where a `None` actually reaches a use — the class that raises at
runtime — is still the part worth reading first:

| where | finding |
|---|---|
| `pay_test_deposit.py:557` | `"splitlines"` is not a known attribute of `None` |
| `pay_test_deposit.py:619` | `Object of type "None" is not subscriptable` ×2 |
| `swap_readiness.py:1298` | `Operator "/" not supported for "None"` |
| `swap_readiness.py:1416` | `Object of type "None" is not subscriptable` ×2 |
| `swap_stack.py:1463` | `"splitlines"` is not a known attribute of `None` |
| `swap_terminal_desktop.py:986` | `"splitlines"` is not a known attribute of `None` |
| `tools/mutate.py:68` | `shutil.which("git")` is `str \| None` and goes straight into `subprocess.run([GIT, ...])` — and the comment three lines above claims a refusal the code does not perform |
| `tests/test_chain_balances.py:315` | `Decimal \| None` passed to `float()` in a report about money |
| `tests/test_address_network.py:416` | `len()` on `bytes \| None`; `+` on `bytes` and `bytes \| None` |

**One of this class was already a real bug and is now closed — see C11.** That
is the argument for the whole entry: the signal existed, and it was unreadable
under 53 false positives.

### 11. `create_quote()` stores `output_amount_estimate` unquantized
`300.0000010967742` GRC for an 8-decimal chain. Not a money-path defect —
`payout_service` runs `quantize_for_chain()` before broadcast — but it is on the
order path, so named rather than changed.

### 13. Docker base images: 9 warnings, 5 that ship, 1 that matters
**Owner: operator**, filed here because I found it — settling it needs a docker
daemon, which this session does not have.

Docker DX flagged five Dockerfiles across several pastes, one file at a time.
Taken as a stream that reads like nine problems. Counted properly it is not, and
the distinction the editor cannot make is the whole finding: **it warns on every
`FROM`, including build stages whose contents are thrown away.**

| Dockerfile | line | stage | base image | ships? | reported |
|---|---|---|---|---|---|
| `docker/grc-desk.Dockerfile` | 42 | `build` | `debian:bookworm-slim` | **no** | 4 crit / 11 high |
| `docker/grc-desk.Dockerfile` | 81 | `runtime` | `debian:bookworm-slim` | yes | 4 crit / 11 high |
| `docker/harness.Dockerfile` | 27 | `chains` | `debian:bookworm-slim` | **no** | 4 crit / 11 high |
| `docker/harness.Dockerfile` | 74 | `runtime` | `python:3.12-slim-bookworm` | yes | 8 high |
| `docker/icp-replica.Dockerfile` | 25 | **single stage** | `rust:1.90-bookworm` | yes | **13 crit / 169 high** |
| `docker/web.Dockerfile` | 31 | `deps` | `python:3.12-slim-bookworm` | **no** | — |
| `docker/web.Dockerfile` | 45 | `runtime` | `python:3.12-slim-bookworm` | yes | — |
| `.../abstergo_exchange/Dockerfile` | 28 | `deps` | `node:22-alpine` | **no** | 11 high |
| `.../abstergo_exchange/Dockerfile` | 46 | `runtime` | `node:22-alpine` | yes | 11 high |

Four of the nine are builder stages. `grc-desk` copies three files out of its
builder and `harness` copies four binaries; `web` copies `/wheels`; `abstergo`
copies `node_modules`. Nothing else from those stages reaches a running
container, so their CVE counts are build-time noise. **Established by reading
every `FROM` and every `COPY --from` in the tree**, not by assuming the warnings
were duplicates.

**THE ONE THAT MATTERS IS `icp-replica.Dockerfile`, and it is the only Dockerfile
here that is not multi-stage.** A full Rust 1.90 toolchain is the runtime image,
so all 182 of its reported vulnerabilities are in the container that actually
runs — against 11 and 8 for the others. It is also the largest image in the
topology by a wide margin, for a container whose job after build is to run
`dfx start`.

What keeps this from being urgent, and it is the file's own header rather than my
reading of it: *"Can move funds: NO, and structurally. A local replica has its own
genesis and its own threshold keys; the key named `dfx_test_key` exists only
inside this container and controls nothing on any real chain."* It is a local dev
replica holding nothing. So this is image hygiene and image size, not an exposure.

**THE FIX IS A SECOND STAGE, AND IT IS A PROPOSAL** (rule 16: a change I cannot
test here is a proposal, not a fix — and this one also changes what gets
deployed). Build the replica and `dfx` in the `rust:1.90-bookworm` stage, then
`COPY --from=` the binaries into a `debian:bookworm-slim` runtime, exactly the
shape `grc-desk.Dockerfile` already uses twenty lines of comment to justify. The
toolchain, `rustup`, the wasm target and the cargo registry all stop shipping.
I cannot build it to confirm the binaries' runtime deps come across — that is
what the `libunwind8` line in the current file is for and it would need checking
in the new stage.

**SEPARATELY, AND IT IS TRUE OF ALL FIVE FILES: not one base image is pinned by
digest.** Zero `FROM ... @sha256:` in the tree, measured. `22-alpine`,
`bookworm-slim` and `3.12-slim-bookworm` all float, so two builds a week apart
produce different images — in files whose headers are about being able to say
what is running ("verify the artifact, not the deploy", rule 13), and
`icp-replica.Dockerfile`'s own comment says *"PINNED, NOT LATEST. A replica
version is a consensus implementation"* while pinning the dfx version and leaving
its base floating. Pinning digests would also make a vulnerability count mean
something, because it would be a count of a specific image rather than of a
moving target.

The commands that settle the counts are yours; I can run none of them:

```
docker image inspect node:22-alpine --format '{{index .RepoDigests 0}}'
docker scout cves rust:1.90-bookworm
docker scout cves debian:bookworm-slim
```

Worth knowing before spending time on it: these counts are of the BASE images'
own Debian/Alpine packages, not of anything in this repository, and a
`rust:1.90-bookworm` with zero criticals may simply not exist. The two-stage
change is the one that moves the number regardless of what upstream ships.


### 15. `ecdsa` has no type information, and every in-file fix is dishonest
**Owner: operator** — it changes how every `ecdsa` import in the tree resolves.

4 findings: `swap_terminal/identity.py:442` and `:471`,
`swap_terminal/modules/htlc_spend.py:344`, `swap_terminal/regtest/keys.py:286`.

`ecdsa` 0.19.2 ships no `py.typed` and typeshed has no stub, so pyright infers
`SigningKey.get_verifying_key()` from its body — `return self.verifying_key`.
`__init__` sets that attribute to `None` (`ecdsa/keys.py:765`) and **every
classmethod that fills it in assigns through a LOCAL named `self`**
(`self = cls(_error__please_use_generate=True)`, keys.py:795), which pyright does
not count as a declaration. The inferred type is therefore exactly `None`, not
`VerifyingKey | None` — while the library's own docstring says
`:rtype: VerifyingKey`.

**Every in-file option was tested and none is honest.** `if vk is None: raise`,
`isinstance(vk, VerifyingKey)` and a declared local `vk: VerifyingKey | None` all
narrow `None` to `Never`, which removes the report by removing the *analysis* of
the two lines after it, and guards nothing at runtime that is not already a crash.

The proposal is `typings/ecdsa/keys.pyi` declaring
`verifying_key: VerifyingKey | None`; pyright reads `./typings` with no config
change. It was not done because a partial stub makes any name it omits an error,
so it has to be complete enough for every `ecdsa` import in the tree. The
mechanism and the rejected options are written into docstrings at both live sites.

### 16. Three fixes landed without the test that would hold them — CLOSED
Each is mutation-checked by hand, and each needed a file the agent that found it
could not reach. Named rather than absorbed.

| fix | the test that is owed |
|---|---|
| `Config.NETWORK` never existed, so every chain derived testnet (`8287f1c`) | `tests/test_icp_custody_addresses.py`: seed GRC at 15715 and BTC at 18443, assert `expected_network("GRC") == MAINNET` and `("BTC") == TESTNET`. Fails against the old expression, which returns one value for all three |
| `run_xrp_first()` returned `False` on its success path (`35a1e77`) | drive BOTH runners through stubbed submit/fund/claim and assert `is True`. **No test drives a runner at all today** |
| `apply_correction` would write NULL for a SKIP plan (`8425058`) | `apply_correction(db, row, Correction(SKIP, 1.0, None, ...))` raises `ValueError` naming the verdict and leaves `payouts.amount` unchanged |

All three now have one, each mutation-checked against the original defect
(`f3f1a8a`, and the commit this line lands in).

**AND THE FIRST ATTEMPT AT ONE OF THEM WAS GREEN FOR THE WRONG REASON**, which is
worth more than the test. The `Config.NETWORK` test called `expected_network()`
directly — and reinstating the defect produced **zero failures**, because the
broken code never called that function at all. The mutation caught the test, not
the code.

The reason it could not be tested properly is rule 10: the decision was one line
inside a sixty-line `main()`, so the only way to exercise it was to run the whole
tool. It is now `icp_custody_addresses.networks_for()`, and the same mutation
fails with `got {'BTC': 'testnet', 'LTC': 'testnet', 'GRC': 'testnet'}` — three
the same, which is the defect in one line.

Still open, and the same shape: each POST route on the operator panel, with its
field absent, returns 400 with the field name in the error — nine routes, no
test.

### 17. Duplication the pass surfaced and did not merge
All of it is rule 8, none of it is a defect today, and every one was established
by grepping for the NAME rather than the import graph (rule 2).

- **`("BTC", "LTC", "GRC")` is spelled five times**: `wallet_custody.SCRIPT_CHAINS`,
  `modules/atomic_swapper.SUPPORTED_ASSETS`, `chains/registry._BITCOIN_DERIVED`,
  `workers/common.endpoint_lines()`'s inline tuple, `modules/htlc_assets.SCRIPT_HTLC_ASSETS`.
  No sixth was added — `config.RpcSettings`'s annotations are now the one spelling
  a checker reads, and it is named from the others' neighborhood.
- **`workers/common.db_path_source(db_path: str, explicit_db="")` never reads
  `db_path`.** It reads only `explicit_db` and `os.environ`. 9 call sites; the
  honest fix is to delete the parameter.
- **`host_of` / `LOOPBACK_HOSTS` are duplicated** between `operator_panel.py:1246-1249`
  and `swap_terminal/loopback.py`, which its own docstring names as owed work and
  which `kill_switch.py:544` **already claims is done**. Verified safe to merge:
  the bodies are character-identical and `loopback.py` is stdlib-only with no
  import-time side effects, so the panel keeps its stdlib-only invariant. Not done
  because completing it means rewriting the paragraph of `loopback.py`'s docstring
  that would otherwise become false — it wants to be one commit by someone who can
  touch both.
- **Four copies of the console recorder in tests** (`test_chain_balances.py:74`,
  `test_xrp_balances.py:240`, `test_atomic_swap_xrp_driver.py:302`, `_QuietConsole:178`),
  three of them byte-identical. Belongs in `tests/conftest.py`.
- **Five copies of the `spec_from_file_location` entry-point loader**
  (`test_operator_panel.py:50`, `test_solana_payout.py:1199`, `test_grc_htlc_verify.py:58`,
  `test_reclaim_funding.py:43`, `test_icp_replica_entrypoint.py:34`).
  `test_solana_payout.py` names `test_operator_panel.py::_entry()` in a comment as
  the thing it copied — and all five carried the same unchecked-`spec.loader` hole,
  now closed in four of them separately. One `conftest.py` helper fixes all five.
- **Two classes are named `Console`** (`step_console.py` and `regtest/console.py`),
  and their `check()` disagrees about whether the verdict is a `bool` or a string.
  Not merged — they are genuinely different consoles — but `step_console.check` now
  REFUSES a non-bool, because crossing them printed OK and exited 0 (C16).
- **Six report lines still exceed 150 columns**, measured on one `swap_stack.py
  status` run after C25: 255 and 222 from `swap_terminal/workers/common.py`
  (`database`, `it holds`), and 165/174/285/212 from
  `swap_terminal/supervisor.py` (`network`, `none`, and the two host-worker
  warnings). All six are HAND-WRITTEN `say()` strings whose author chose the
  width, not values arriving from elsewhere — so `say_wrapped()` is the wrong
  tool for them and the fix is to reflow the source string. Not done because
  neither file was otherwise touched this pass, and rule 12 is explicit that a
  sweep across files you were not already in is a large diff with no behavioral
  benefit. Named work, not a baseline (rule 19).

- **"everything stops safely. all services daemons are done" — the chain daemons
  are deliberately NOT included, and that is the one part of the ask I did not
  build.** `bitcoind`, `litecoind` and `gridcoinresearchd` hold the wallets this
  desk spends from, nothing in this repo has their passphrase, and
  `stack_authority.py`'s own header refuses to mark them stoppable *"ever, under
  any flag"* for a specific reason: a stop it performed could not be undone by
  the `up` that follows. So `down` and `restart` stop every worker, container and
  server this terminal owns, PROVE each port free, and report the daemons as LEFT
  RUNNING with that reason attached.

  If you do want `swapterm down` to take the daemons too, say so and I will add
  it as a separate explicit flag rather than folding it into `down` — it is
  irreversible from here, it would need your passphrase to recover from, and it
  is exactly the live-posture call rule 16 sends back to you. Everything else in
  the ask ("all services, database, etc.") is already covered: the workers get
  SIGTERM plus a grace period while the daemons are still reachable, so a send in
  flight finishes or fails cleanly rather than losing its RPC underneath it, and
  the database is closed by those workers exiting.

- **Should the GRC wallet-path warning become a refusal?** `wallet_path_warning()`
  currently logs at WARNING. A hard refusal at URL-construction time would fail
  fast and visibly instead of producing a 404 mid-payout, and on a configuration
  that CANNOT work that is strictly safer. I did not ship it because it is the
  order path and this session cannot see your `.env`: if `GRC_RPC_WALLET` were
  set on the live host, a refusal would stop GRC dead. The evidence says it is
  unset (your 403 came from a bare `http://host:25779/`), so the refusal is
  probably a no-op — "probably" being exactly why it is yours (rule 16).

- **Three capability rows are unverified against your daemons** (rule 17), all
  Bitcoin Core release history rather than readings: the `/wallet/<name>`
  endpoint at 0.17, `rpcbind` at 0.12, and CIDR `rpcallowip` at 0.10 with
  wildcards removed in 0.12. `unverified_on_this_deployment()` lists them. The
  cheap check for all three is `gridcoinresearchd help` plus the daemon's own
  startup log, which says if it rejected a netmask.

- **Two unrelated functions are both named `readiness_verdict`**, with different
  arities and different questions. `stack_authority.readiness_verdict(outcome)`
  interprets ONE probe of an HTTP endpoint and returns
  `(ready, summary, advice)`. `swap_terminal_desktop.py:167`'s
  `readiness_verdict(status_code, body, error, db_path)` decides whether the
  launcher's own server came up against the RIGHT DATABASE — a different
  question with a four-argument signature. Not a duplicate to merge: they
  genuinely differ, and rule 8 says the difference then "belongs in a comment at
  BOTH sites, naming the other one." Neither site names the other today. Cheap
  to fix and I did not, because `swap_terminal_desktop.py` is outside what this
  pass touched (rule 12) and the comment should be written by whoever next has a
  reason to be in that file.

- **`up` reports the code version of the HOST CHECKOUT and starts the container
  without rebuilding it.** `git_reading()`'s own docstring says it reads "which
  commit this HOST checkout is running", and `cmd_up` runs `docker compose up -d
  icp-replica web` with no `--build`. `docker/web.Dockerfile:150` is `COPY
  swap_terminal ./swap_terminal` — the app is BAKED IN, and only `/data` and
  `/runtime` are mounts. So after a `git pull`, `up` prints

      code 3e41ad1 (xrp-adapter), tree clean

  while the container keeps serving whatever was baked at its last build. That is
  precisely the shape of defect rule 13 names ("verify the artifact, not the
  deploy") in the section written to prevent it, and the same shape as C19 above:
  a check reporting on something adjacent to the thing it claims to check.

  Two ways to close it and they are not equivalent. Rebuilding on every `up` is
  honest and costs the operator a build on a command they run to look at things.
  Reading the commit back OUT of the container (`docker compose exec web cat
  <baked marker>`) and comparing to `git_reading()` is cheap and reports the real
  state, including "container is 4 commits behind, run with --build". The second
  is what rule 13 actually asks for. NOT DONE: it needs a commit marker baked at
  image build time, which is a Dockerfile change, and I would want to watch one
  real build before claiming it works.

---

## Closed

| | what | commit |
|---|---|---|
| C34 | **`swapterm` on PATH, plus Up / Down / Restart icons.** Operator: *"i need like icons on the desktop and shell commands like swaptermi up or down"*, *"swapterm restart"*. New `restart` action, and a `swapterm` wrapper installed to `~/.local/bin` with the venv interpreter and repo path baked in at install time — same reason the `.desktop` `Exec=` is rewritten: a desktop session's `python3` is the SYSTEM one, with neither xrpl-py nor gunicorn. All four launchers route through that one wrapper, so an icon cannot start the stack a different way from the shell. One `.desktop` template renders all three actions. `--desktop-files` also drops them on the desktop, and the GNOME trust step is **said rather than attempted** (`gio` needs the session bus, which a session over ssh does not have, and a swallowed failure leaves an icon that silently does nothing) | `this commit` |
| C35 | **`restart` refuses to start on a stop it could not prove** — the gate, not the sequence. Rule 13's own incident is twelve cycles printing `exit_code=0` beside "another cycle is already running pid=...; skipping": a stale process holding the lock, every cycle reporting success, zero work done. `down → up` unconditionally reintroduces exactly that, is one line shorter, and reads as obviously correct. Mutation-checked both ways: removing the gate, and returning `down`'s exit code instead of `up`'s. Exit code is `up`'s so `swapterm restart && …` chains on the stack actually SERVING | `this commit` |
| C32 | **GRC/LTC/BTC capability knowledge was spelled in prose across eleven files** — all correct, all measured, none askable. `chains/base.py:339`, `payout_quantization.py:211`, `rpc_method_support.py:21`, `script_chain.py:549`, `htlc_fee.py:232`, `htlc_rpc.py:377`, `funding_steps.py:505`, `daemons.py:853`, `admin_view.py:1496`, `custody_separation.py:234`, `fee_sweep.py:564` — and `daemons.py:658` says out loud that one file held two copies of the `uptime` fact. New `chains/daemon_capabilities.py` is the one place to ask: a capability map with the Core release each feature arrived in, and an **equivalence tree keyed by the JOB** (operator: *"a tree of equivalence betwen rpc comamnds for ltc, btc, and grc"*). Every row names the function that ALREADY resolves the divergence rather than re-implementing it. **Evidence is a field** — `MEASURED` vs `RELEASE_HISTORY` — and `unverified_on_this_deployment()` lists the three rows I could not test here (rule 17) | `this commit` |
| C33 | **A `/wallet/<name>` path would be built for GRC, which has no such endpoint.** `chains/base.RPCAdapter.url` AND `chains/daemon_conf.rpc_url()` both append it whenever a wallet is configured, for any chain, with no test between them — two copies of one rule, and the rule neither has is that multi-wallet HTTP arrived in Core **0.17**, which `base.py:339` already records Gridcoin as predating. **Latent, not live**: `GRC_RPC_WALLET` defaults to `""` and the 403 came from a bare `http://host:25779/`. One env var from breaking every GRC call including payouts, and it would read as a transport failure rather than a config error. Now reported at startup by `wallet_path_warning()`; **turning it into a refusal is the operator's call (rule 16)** — it is the order path and I cannot see your `.env` | `this commit` |
| C31 | A test I wrote this session was **flaky by construction** and had been passing on luck since `8287f1c`. `assert grc.startswith("S")` over a RANDOM `generate_key()` — but a one-byte version prefix does not pin the leading base58 digit. Measured over 2000 random keys: **272 failures, 13.6%**, against **0** for the version-byte assertion that replaced it. It surfaced in a full-suite run and passed in isolation, the shape that gets written off as a flake. `0x3E`'s 25-byte range straddles a base58 carry where `0x00` and `0x30` do not, which is why the same heuristic is safe for BTC and LTC and not for GRC. **And the repo already knew**: `test_address_network.py:91` records that a `not startswith("S")` check "called it testnet, which is what it was written to prevent", and two more files note `startswith("S")` "had it answering backwards" — three written refutations, and I wrote a fourth instance. Now asserts `network_of_version(b58decode_check(addr)[0])`, the round trip through production rather than a literal | `this commit` |
| C29 | The ATM review screen showed a customer `You send 8.061e-05 BTC` one click before **Create the swap**. Python's float `str()` switches to scientific notation below 1e-4, and the templates interpolated `{{ estimate.send }}` — a raw float straight into Jinja. At the BTC price this desk quotes, 0.0001 BTC is roughly ten dollars, so this was most small swaps, not an edge case. Operator: *"do all scientific notation in engineering notation where all powers are multiples of 3 since we have a number with a unit which is btc."* New `engineering_notation.py` on the `microfortnights.py` pattern — one module, one convention — renders `80.61e-6`; registered once as a `coin` Jinja filter. **Measured surface: 4 raw-float renderings against 19 already using `'%.8f'\|format`, which cannot produce an exponent. Those 4 were the whole defect** | `this commit` |
| C30 | And my first two tests for C29 were VACUOUS. At the stub prices (BTC 62000 / GRC 0.031 = 2,000,000 GRC per BTC) a 1000 GRC payout solves to 0.00050762 BTC, which writes plainly — so "no scientific notation on this screen" asserted nothing, and the mutation reverting `\| coin` passed clean. The boundary is 197 GRC; the tests now use 100 GRC and each **asserts `needs_engineering(solved)` first**, so they fail loudly rather than going quietly vacuous if the stub prices move. Separately, the review screen has no `<aside>` at all (`_SCREEN_FURNITURE` gives step 5 `("estimate",)`), so the costs partial needed step 4 — one test, two posts, both partials, each independently mutation-checked | `this commit` |
| C27 | The cause of a chain failure was truncated off the report. `str(detail)[:150]` kept the HEAD of requests' message — where the host and port are, which the row and the config already say — and dropped the TAIL, which is where `[Errno 111] Connection refused` or `timed out` sits. Those two have DIFFERENT remedies (`rpcbind` vs a firewall rule), so the report named neither, and it cost a live diagnosis on 2026-10-09: BTC and LTC read as the same failure as GRC's 403 when they are not. `probe_failure_detail()` keeps both ends and says how many characters it dropped between them; a message short enough to read (GRC's 403, at 72 chars) is untouched | `this commit` |
| C28 | `chains`' exit line ended in a dangling `"; "` on every non-zero exit — a conditional tail that rendered empty. A report that trails off reads as output that was cut short, which is the ambiguity rule 14 forbids for a blank region. One sentence per case now | `this commit` |
| C25 | A report line is wrapped by whoever WROTE it, and these had no author at the print site: `COULD NOT READ` quoted docker's stderr verbatim. Measured on one `status` run — four lines at 326/332/336/495 columns, all one call site, each broken by the terminal at an arbitrary column with NO indent so the continuation ran back under the label column and read as a new field. New `say_wrapped()` wraps values at 96 columns under their label. **13 lines over 150 columns → 6, widest 495 → 285**; the 6 remaining are in `supervisor.py` (4) and `workers/common.py` (1), untouched this pass (rule 12) | `this commit` |
| C26 | The surface map told a reader with ONE missing id that neither was read, and a reader with BOTH missing that one was. `its URL would be <shape> -- the id is the missing part` appended a singular gloss to every branch while `shape` itself carried `-- neither id was read` in only the third — two `--` clauses, one of them wrong in two branches out of three. Which ids are missing decides what the operator goes and looks up, so it is named per branch now | `this commit` |
| C21 | Every web-probe refusal reported as `unrecognized probe outcome`. `probe_serving_port()` stored `readiness_verdict()`'s rendered SENTENCE and `serving_verdict()` fed it back INTO `readiness_verdict()`, where a `str` is neither an `int` nor an `OSError` and fell through to the fail-closed branch — so the most ordinary outcome a web probe has fired the "nobody anticipated this" path, with the careful two-readings advice coming back out inside a `repr()`. Found by running the new `chains` action; `serving_verdict()` had **no direct test**, which is why four tests of `readiness_verdict()` stayed green through it | `this commit` |
| C22 | One 40-word paragraph printed **five times on one screen** — once per candidate port as progress, then four more concatenated into a single 119-column headline. `readiness_verdict()` now returns the short reason and the advice separately, progress lines take the reason, and `serving_verdict()` gives the advice once per DISTINCT reason. Exactly the defect `wait_for_http`'s own `announce=False` comment was written to stop, arriving by another route | `this commit` |
| C23 | `swap_stack.py` had no read-only way to ask the one question that cost 2026-10-08. `up` asks it (step 7); `status` does not, so the only way to ask was to run the command that STARTS CONTAINERS — worst of all right after a daemon restart, which is exactly when you want to re-ask. New `chains` action: read-only, starts nothing, exits 0 only when every probeable chain answered | `this commit` |
| C24 | `choices=("status", "up", "down")` and the dispatch dict were two hand-written spellings of the action list, 26 lines apart. Nothing had failed because nobody had yet added an action to one and not the other — rule 8's "they agree on the day they are written". `choices` is now derived from `ACTIONS` | `this commit` |
| C19 | `up` step 7 could never read the endpoint it asks. `/api/admin/chains` has always answered with an envelope (`probed_at`, `adapters_configured`, `probes_attempted`, `chains`) and `static/admin.js:107` has always read `data.chains`; the host-side reader expected the rows to BE the body, so its first live run printed `COULD NOT ASK ... got dict, which is what an error page parses to` on a WORKING probe and sent the operator looking for an error page that does not exist. Four pure tests were green throughout because every one fabricated the body it tested against — the same defect as `3e41ad1`, one file over. Builder and extractor now share `CHAIN_PROBE_ROWS_KEY`; the test helper builds through the real producer; a behavioral test feeds the route's own body to the reader | `this commit` |
| C20 | `swap_stack.py:731` carried an unsuppressed `S310` on `HEAD` — a lint error I committed. The comment beside it claimed "the URL is never input", true of both callers and not a constraint: a third caller passing `file:///` would have been read off local disk with the comment still reading true. Now a guard, so the `noqa` is backed by the line above it | `this commit` |
| C1 | Candid links pointed at the default identity's wallet canister | `4d82caf` |
| C2 | No output said which commit produced it | `697c1db` |
| C3 | Four canister lookups announced as three | `764ec96` |
| C4 | `up` argued the replica's state was safe instead of checking | `e4d919d` |
| C5 | The pre-flight answered "nothing is at risk" for a lookup that never happened | `f925acc` |
| C6 | Operator and customer surfaces linked to each other, five rows deep | `125cacd` |
| C7 | One quote could spend 105s fetching prices against a 60s worker | `67589aa` |
| C8 | The chain probe died at 60s and named none of the six | `e03a006` |
| C9 | `up` never asked whether the container could reach the chains | `1bdfef1` |
| C10 | Pylance reported 53 import errors, all false | `f01e85c` |
| C11 | A docker timeout returned `""` for a canister id, and the map rendered it as `http://.localhost:4943/` — three malformed URLs printed as working links. Found by pyright, under the 53 | this commit |
| C12 | The four-screen ATM: one scrolling column became four screens, fees to the right. Rendered in Chromium at two widths, ten renders, zero overflow | `418e5a2` |
| C13 | The review screen told a customer `RUN GREEN -- atomic_swap.py (P2SH HTLC on both legs)` above "Create the swap", and screen 2 printed which RPC variables are unset on this host to an unauthenticated reader. Five hand-written copies of one custody fact, two of them already disagreeing (was 10 and 14) | `58d6f62` |
| C14 | `static/script.js`: seven dead functions, 666 → 395 lines — AND the payout-address autofill guard was not running on the ATM flow at all. 82.65 tGRC went to an autofilled address on 2026-10-01; the guard left with `index.html` on 10-07 (was 9) | `332d7f7` |
| C15 | Eight signatures each refused a type their own annotation said could not arrive, with the precedent inside one of the signatures | `e12d539` |
| C16 | A `FAIL` from the other `Console` printed `OK` and returned exit code 0 | `da4a3fa` |
| C17 | `routes/atm.CARRIED` listed `quote_id`, which no template sets — established by grep over `templates/`, now removed (was 12) | `58d6f62` |
| C18 | Five tests would have reported a missing element as `'NoneType' object has no attribute 'group'` | `533b6ba` |
