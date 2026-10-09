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
