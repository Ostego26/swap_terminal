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

### 1. BTC and LTC daemons are bound loopback-only — CLOSED 2026-10-10
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

**CLOSED 2026-10-10. All four probeable chains answer and `swapterm chains` exits
0 for the first time.** The remedy above was applied verbatim, appended at EOF of
each conf because `[regtest]` was already the last section header in both (line 4),
so end-of-file is inside it — no new header, and the section question that the
remedy flags was checked before writing rather than assumed:

```
LISTEN  172.17.0.1:18443   bitcoind   pid 2465490
LISTEN  172.17.0.1:19443   litecoind  pid 2465635
LISTEN   127.0.0.1:18443   bitcoind              <- kept, so the host CLI still works
LISTEN   127.0.0.1:19443   litecoind
```

```
REACHABLE   all 4 probeable chain(s) answered: BTC, GRC, LTC, XRP
  BTC   regtest   pays bcrt1... addresses, and only those
  GRC   testnet   no bech32 on this chain -- its addresses are base58
  LTC   regtest   pays rltc1... addresses, and only those
  XRP   testnet (network_id 1)
```

**The LTC blocker was never reachability.** That `pays rltc1...` line is the one
added on 2026-10-09 when the probe's `network` field was found to have zero
readers, and it stated the wall directly: the operator's external wallet is
`tltc1q37khgpktccdwpxq6vmkt6gtrnra3x39tvcyx62` on **testnet**, while the desk's
litecoind was `-regtest` on 19443. A regtest node issues and accepts `rltc1...`
and its coins exist only on that node, so no choice of deposit asset changed it.

**RESOLVED 2026-10-10 by moving the daemons, at the operator's instruction** —
"our grc, ltc, and btc daemons should have peers and not be regtest anyone and
just full testnet now", with the 33-54 hour sync accepted explicitly. All four
probeable chains are now on a test network and `swapterm chains` exits 0:

```
BTC   testnet4      pays tb1... addresses, and only those
GRC   testnet       no bech32 -- base58 only, which names no network
LTC   test          pays tltc1... addresses, and only those
XRP   testnet (network_id 1)
```

Two things the migration cost that were not the sync, and both were section or
port collisions rather than anything conceptual:

- **BTC needs `[testnet4]`, LTC needs `[test]`.** Core v28.1.0 has two testnets
  with different flags, data directories and conf section names; Litecoin v0.21.4
  has one. Using `[test]` for Bitcoin Core 28 would have reproduced finding 2's
  failure exactly — daemon starts, silently ignores every line under the header,
  container back to `Connection refused`.
- **litecoind could not bind Litecoin's testnet P2P port 19335**, held by the
  operator's `litecoin-qt`: *"Error: Failed to listen on any port."* Nothing to do
  with RPC — every `[test] rpcport="19443"` line was being read correctly, the
  daemon just died before it got to serving. `port=19444` inside `[test]` fixed it
  and leaves the GUI wallet alone.

**The RPC ports did NOT have to change.** 18443 and 19443 are in
`network_target.CHAIN_PORTS`' *test* sets, not just regtest, so keeping them meant
no `.env` edit, no ufw change (finding 3's rule 20 already allows them), and no
collision with `litecoin-qt` on the default testnet RPC port 19332.

**Still owed before a swap can pay out:** neither conf's named wallets
(`wallet=desk_hot`, `wallet=regtest_htlc_harness`) exist on the fresh testnet
directories, so both chains need `createwallet` and faucet coins — testnet coins
cannot be minted, and `fund_testnets.py` refuses at its `assert_regtest` check.
`regtest_htlc_harness` is now a misnomer in both files and worth dropping: the
harness cannot run on testnet at all.

### 2. GRC rejects the container's RPC with 403
**Measured**: `did not answer: 403 Client Error: Forbidden for url:
http://host.docker.internal:25779/`. Socket open, RPC refused — so `rpcallowip`,
not the bind.

**THE FILE, NAMED CORRECTLY 2026-10-09 AFTER I NAMED IT WRONG REPEATEDLY.** The
operator runs THREE GRC wallets and the remedy above pointed at the wrong one's
conf for several rounds, which is why applying it changed nothing:

    /usr/local/bin/gridcoinresearch -datadir=~/.GridcoinResearch -min    mainnet GUI
    gridcoinresearch -testnet -disableupdatecheck ...                    testnet GUI (external)
    gridcoinresearchd -datadir=~/.GridcoinResearch-desk -daemon          THE DESK

`~/.GridcoinResearch/gridcoinresearch.conf` belongs to the GUI wallets (measured:
`rpcport=15715`, `rpcallowip=127.0.0.1`). The desk daemon has its own datadir, so
the file to edit is **`~/.GridcoinResearch-desk/gridcoinresearch.conf`**.

**CLOSED 2026-10-09. The container gets HTTP 200.** The remedy below is kept as
written, struck through, because it is wrong twice over and the way it was wrong
is the finding.

> ~~`rpcallowip=172.18.0.0/16` in `~/.GridcoinResearch-desk/gridcoinresearch.conf`,
> keeping the existing `rpcallowip=127.0.0.1`, then restart THAT daemon only. If a
> `/16` still 403s afterward, that build does not honor CIDR and the pre-0.10 form
> `rpcallowip=172.18.*.*` is what it understands.~~

**Wrong about the FILE, and that was the active blocker.** A TestNet Gridcoin
reads `<datadir>/testnet/gridcoinresearch.conf`, not `<datadir>/gridcoinresearch.conf`
— `src/util/system.cpp:811-815` passes `net_specific=true` with the comment
*"Unlike in Bitcoin, the net specific flag is TRUE, because we still use split
config files."* The two timestamps are the whole story:

    <datadir>/gridcoinresearch.conf          modified 2026-10-09 16:21   edited all day
    <datadir>/testnet/gridcoinresearch.conf  modified 2026-10-04 14:22   actually READ

The testnet file held `rpcallowip=127.0.0.1` and nothing else, five days stale.
Adding the subnet there turned the 403 into a 200 on the first attempt.

**Wrong about CIDR too, independently.** Gridcoin 5.5.1.0 passes the raw
`rpcallowip` string to `WildcardMatch` against the peer's address TEXT
(`src/rpc/server.cpp:531`, `util.cpp:143`), where `/` is a literal — so
`172.18.0.0/16` matches nothing, ever. The wildcard `172.18.*` is what it reads,
and it is exactly equivalent. CIDR arrived in commit `924f36eb` (2026-08-23), on
`development` and tag `5.5.1.7-testnet` only. Both facts and their citations are
now rows in `chains/daemon_capabilities.py`, and `swapterm chains` prints them
under a 403 rather than leaving them in a file nothing reads.

**Why every piece of evidence pointed the wrong way**, which is the part worth
keeping:

- loopback kept working, so the conf was "obviously" being read. It does not
  follow: `127.0.0.0/8` is hardcoded allowed at `src/rpc/server.cpp:521-526`
  BEFORE the allow list is consulted. That line granted nothing and would have
  behaved identically beside `rpcallowip=garbage`.
- `LISTEN *:25779` proved *some* `rpcallowip` reached `gArgs`, which read as "the
  conf is honored". It was — the **stale** file's own loopback line is what
  widens the bind at `src/rpc/server.cpp:661`.
- the release line logs nothing whatsoever about `rpcallowip`, so a wrong file and
  an unparseable value are indistinguishable from outside.
- `Using data directory <datadir>/testnet` was printed at 23:14:31 and read past
  four more times.

**Lesson, and it is one level deeper than the one this entry used to carry.** It
said "which conf is as much a part of a config remedy as the line itself" after
naming the wrong *datadir* — and then the same entry named the wrong *file inside
the right datadir* for five more rounds. Writing the lesson down did not stop the
repeat, because what was missing was never the principle: it was asking the daemon
which file it opened. It prints that every run.

**Still unestablished:** which subnet is doing the work. `172.17.*` and `172.18.*`
went in together — `host.docker.internal` resolves to `172.17.0.1` (the DEFAULT
bridge's gateway, which the container is not attached to) while the container is
`172.18.0.3`. Narrowing it costs one restart and bears directly on finding 3.

### 3. The ufw rule names a subnet docker can reassign — PINNED 2026-10-10 (C52)
`172.18.0.0/16` was read from `docker network inspect` on 2026-10-09. Docker
assigns compose network subnets when it creates them, so a `down` + `up` can hand
out a different one and the rule goes **silently** stale — straight back to
dropped packets and 30s hangs, with no new symptom to explain it.
**Remedy**: pin the subnet in `docker-compose.yml`. Not done: it recreates the
network and both containers. Say the word.

**THE WORD WAS SAID, 2026-10-10.** `docker-compose.yml` now carries a `networks:`
block — there was none in any of the three `-f` files, which is why docker was
choosing — pinning the default network to
`${SWAP_TERMINAL_BRIDGE_SUBNET:-172.18.0.0/16}`. The default is **the value rule 20
already names**, deliberately: pinning it to anything else would silently require a
ufw edit to stay correct, and a change that needs a second change in a second system
is what this entry is about.

**AND IT IS READ BACK, which is the half that was missing from the remedy as
written.** A pin nobody verifies is rule 13's deploy-not-the-artifact defect wearing
different clothes, so `swap_stack.py status` now prints the live subnet beside the
declared one: `OK`, `DRIFTED` (naming both, and naming the silent symptom — every
chain reads NOT CONFIGURED in a container whose environment is correct), `ABSENT`
(no network yet; `up` will create it from the pin) or `UNKNOWN`, which never reads as
a match. The read asks the **container** which network it is on rather than guessing
`<project>_default`, because under `COMPOSE_PROJECT_NAME` a hardcoded name inspects
nothing and reports `ABSENT` on a running stack — the quietest possible false
all-clear, and the same hazard `replica_state()` already records for volume names.

**IT TAKES A `down` + `up` TO APPLY.** Docker will not move an existing network onto
a new subnet; until then `status` reports DRIFTED and says so. Nothing on that
network is durable state.

**THE SOURCE ADDRESS IS SETTLED, 2026-10-10, AND I HAD IT RIGHT THEN TALKED
MYSELF OUT OF IT.** The container sends from **172.18.0.3** — its own address, as
the first `docker inspect` said. Deduced from two measurements rather than observed
directly, and the premises matter because the direct observation is not available:
ufw rule 20 is `18443,19443,25779/tcp ALLOW IN 172.18.0.0/16`, so a packet arriving
from 172.17.0.1 would be dropped and never reach a daemon — and GRC answers 200.
Corroborated by a throwaway listener on :25999, which **timed out** from the same
container on the same bridge, because 25999 is not in rule 20.

So `host.docker.internal` -> 172.17.0.1 is the DESTINATION, which is why `rpcbind`
has to name it; the source is never rewritten for host-destined bridge traffic. The
`rpcallowip=172.17.*` line added to the GRC conf while chasing this does nothing.
Harmless, and removing it costs a restart.

**Sharper after finding 2, 2026-10-09.** `host.docker.internal` resolves inside
the container to `172.17.0.1` — the gateway of the DEFAULT `bridge` network, which
the web container is NOT attached to (it is `172.18.0.3` on
`swap_terminal_default`). Docker's `host-gateway` always maps to the default
bridge, never the per-network gateway. So the allow list has to cover whichever
address actually arrives, and that is a second moving part on top of the subnet
reassignment this entry already names. Pinning the subnet fixes one half; pointing
`*_RPC_HOST` at the container's own gateway instead of `host.docker.internal`
would fix the other and make the allow list a single tight entry. Both are
operator calls — one recreates containers, the other edits `.env`.

### 4. That rule gives every container on the bridge wallet RPC
`docker-compose.yml` also defines `abstergo` and `harness`, and `up`'s own banner
warns a bare `docker compose up` starts the test harness. On that bridge the
harness would reach the hot wallets exactly as the web container does.

**AND SINCE 2026-10-10 IT IS ALSO THE RESIDUAL EXPOSURE OF THE /admin CONTROLS
(C53), which is why it is now printed on the page rather than only written here.**
The controls accept a container whose publish is declared loopback — but the publish
is only the HOST path to port 5000. Every other container on this bridge reaches that
port **directly**, without going through the publish at all. `bind_evidence()` says
so in the line that explains why the buttons are enabled, and names this finding.
Unchanged by pinning the subnet: that fixes which addresses the firewall admits from
outside, not who is already inside.

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

### 6b. Three commit messages stated a test count nobody counted — MECHANISM ADDED

**Measured 2026-10-09.** Three consecutive commits on this branch put a test count
in their message that no command had produced:

    95e5775   "16 tests"        real: 14 functions / 24 cases
    018bc26   "19 new cases"    real: 11 cases (6 + 5)
    b8a9eaf   "+19 (12 -> 31)"  real: 14 -> 31, +17

The third is the worst of the three: its message also claims the number was
"counted with `--collect-only` this time rather than from memory", and it was not.
None of the three changed a line of code, which is the only reason this is a
footnote rather than an incident.

**The cause was a missing mechanism, not carelessness.** CLAUDE.md has asked for
this from the beginning — *"diff the full suite line-by-line against a recorded
baseline rather than comparing failure counts. Counting failures hides a new break
that lands the same day an old one is fixed."* Measured the same day: **no baseline
file anywhere in the tree, and no tool reading a junit report or `--collect-only`.**
Every "the suite is green at N" in this branch's history was a number read off a
terminal and retyped.

**Done**: `suite_baseline.py` at the root, with `tests/suite_baseline.tsv` recorded
from a real run. `record` rewrites the file from a full run and REFUSES a narrowed
one (a partial baseline looks clean rather than broken); `check` diffs and reports
`regressed`, `newly failing`, `disappeared`, `newly skipped`, `added`, `fixed` and
`still broken` as separate groups, so a fix can never cancel a break in a total.
A narrowed `check` scopes out files it did not collect and says what it cannot see.

**Not a ratchet** (rule 19), and the file's own header says so: nothing is excused
by appearing in it, and a regression cannot be silenced by adding a line — recording
rewrites the whole file from a real run, so a regression recorded as the new baseline
shows up in that file's diff in the same commit.

**What remains mine**: using it. The tool existing is not the habit.

**It paid for itself on the first real run.** The initial `record` came back
`broken at record 1` and named
`tests.test_kill_switch::test_a_start_refuses_when_an_orphan_of_that_worker_is_already_polling`.
That test passes alone and had passed in every full-suite run this branch recorded
by eye — because those runs were read as "4249 passed, 0 failed" and this one
printed the node id. It is a genuine flake by construction: it calls
`subprocess.Popen` and then `start_everything()` on the next line, whose /proc scan
reads `cmdline` unsettled, so a child that has not finished exec is correctly
skipped and no orphan is found.

**Measured 2026-10-10, 400 trials on an idle machine: the child was not yet visible
18 times, 4.5%.** Worse under full-suite load.

`_wait_until_scanned()` has existed in that same file since 2026-10-07 for exactly
this, and its docstring records the identical failure on the identical shape — it
had ONE caller and this site was never converted. Rule 8 again: the knowledge
existed and a second site did not use it. Fixed by waiting on the precondition and
asserting it, not by retrying the assertion; 40 consecutive runs, 0 failures.


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
- **The five existing `spec_from_file_location` copies are not migrated yet.**
  **CLOSED 2026-10-10 — all five are migrated, and the helper now has tests (C47).**
  It was five careful edits rather than a find-and-replace, as this entry said: the
  two redundant `sys.path.insert` lines are gone, the three exception types collapsed
  to the helper's `ImportError`/`FileNotFoundError` (nothing pinned any of them —
  checked, not assumed), the collection-time site still loads at collection time, and
  the `docker/` one still works because the helper takes a relative path. Measured on
  the diff rather than estimated: **144 lines removed, 105 added, net −39** across the
  five files, and the five loader bodies became five one-line calls — most of what is
  added back is comment, because each site now has to say what is specific to it and
  what the helper owns (rule 8's harder half). The entry below is the same finding seen
  from the other end.

- **Four copies of the console recorder in tests** (`test_chain_balances.py:74`,
  `test_xrp_balances.py:240`, `test_atomic_swap_xrp_driver.py:302`, `_QuietConsole:178`),
  three of them byte-identical. Belongs in `tests/conftest.py`.
  **CLOSED 2026-10-10 (C49–C51), and this entry was wrong in two ways.** Three were
  byte-identical, as it says — confirmed by `ast.unparse` with docstrings stripped rather
  than by eye — but `_QuietConsole` is NOT a fourth copy: it discards instead of recording
  AND its `check()` returns `True` unconditionally rather than the `ok` it was handed, which
  is a different contract, not a smaller one. The real count, measured over all of `tests/`:
  **22 console-shaped stub definitions, 19 distinct bodies, 2 bodies at more than one
  site** — the recorder triple, plus a pair this entry never named (`_QuietRun` twice in
  `test_grc_htlc_verify.py`, eleven lines under a module-level `_SilentRun` that is a strict
  superset of both). Now **17 definitions, 17 distinct bodies, 0 duplicated**, held by a
  CLEAN GATE with no allowlist (`test_step_console.py::test_no_two_console_stubs_in_tests_SHARE_A_BODY`).
  Two line numbers in this entry were also stale, which is why the closure re-measured
  rather than trusting them.

- **Eight duplicate class bodies remain in `tests/`, and they are named work rather than a
  baseline** (rule 19). The console-stub gate above covers console stubs only; the same
  scan over EVERY base-less class in `tests/` reads **178 definitions, 163 distinct bodies,
  8 bodies at more than one site, 23 definitions inside those**:

  | sites | lines | where |
  |---|---|---|
  | 2 | 10 | `test_payout_concurrency.py:664` / `:710` `LockedWallet` (one file) |
  | 2 | 6 | `test_solana_chain_check_units.py:629` `Response` / `:2065` `_Throttled` (one file) |
  | 2 | 5 | `test_desktop_launcher.py:988` `NeverExits` / `:1030` `Alive` (one file) |
  | 2 | 4 | `test_xrp_adapter.py:552` / `test_xrp_payout_wiring.py:146` `StubResponse` |
  | 4 | 3 | `test_open_swap.py:333` `Payer` / `test_payable_assets.py:47` `Signs` / `test_supervisor.py:904` + `test_swap_readiness.py:644` `CanSign` |
  | 2 | 3 | `test_payable_assets.py:54` / `test_supervisor.py:921` `CannotSign` |
  | 7 | 2 | `_Key` — `test_funding_payload_is_json.py:67` and six in `test_operator_panel.py` |
  | 2 | 2 | `test_htlc_spend.py:1044` / `:2126` `Holder` (one file) |

  Four of the eight are two copies in ONE file, which is the cheapest kind to fix and the
  kind nothing had to be greped for to find. `_Key` is `class _Key: address = "ours"` and
  the judgment there is **not** to hoist it: each copy sits beside a `listtransactions` row
  in the same test body that spells `"ours"` again, so the value is part of that test's
  fixture and sharing it would make a reader jump to learn what `address` is. Said here
  rather than silently skipped. Widening the gate to all classes requires clearing the
  other seven first.
- **Five copies of the `spec_from_file_location` entry-point loader**
  (`test_operator_panel.py:50`, `test_solana_payout.py:1199`, `test_grc_htlc_verify.py:58`,
  `test_reclaim_funding.py:43`, `test_icp_replica_entrypoint.py:34`).
  `test_solana_payout.py` names `test_operator_panel.py::_entry()` in a comment as
  the thing it copied — and all five carried the same unchecked-`spec.loader` hole,
  now closed in four of them separately. One `conftest.py` helper fixes all five.
  **CLOSED 2026-10-10 (C47). It did.**
- **Two classes are named `Console`** (`step_console.py` and `regtest/console.py`),
  and their `check()` disagrees about whether the verdict is a `bool` or a string.
  Not merged — they are genuinely different consoles — but `step_console.check` now
  REFUSES a non-bool, because crossing them printed OK and exited 0 (C16).
  **AND I ALMOST MADE IT THREE, 2026-10-10 (C50).** The shared recorder that closed the
  entry above was first called `RecordingConsole`, which is the name of a different
  recorder in `tests/test_swap_runners_report_completion.py:96` — whose `check()` puts
  verdicts in a separate `results` list and NOT in `lines`. Two console stubs under one
  name disagreeing about what `check()` does is this exact entry, one layer down, created
  inside the commit consolidating duplicates. Caught before pushing; the shared one is
  `conftest.TranscriptConsole`, named for the transcript, and both docstrings now name the
  other.
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

- **[DETECTION CLOSED 2026-10-10 by `CHAIN_ENVELOPE_VERSION`, and the cost of its
  absence was measured the same hour.]** The mismatch is now DETECTED, and it is
  detected without the Dockerfile change this entry had been waiting on.

  **What it cost while open.** The sync lines shipped at 00:30 — `probe_chain()`
  putting a `sync` field in each row, the report shouting STILL SYNCING with block
  heights. The operator pulled, ran `swapterm chains`, and a BTC daemon at 74% of
  its initial block download rendered as a plain healthy chain. No warning, no
  numbers, nothing. **Nothing on that screen was wrong**: `probe_chain()` runs
  INSIDE the container, the app is baked into the image, the container had not been
  rebuilt, so the rows arrived without `sync` and the renderer correctly printed
  nothing for a field nobody reported. The feature was invisible and so was its
  absence.

  **Why a version number and not the commit marker this entry asked for.** The
  question is not "which commit is the container on" but "does the container
  produce the fields this report reads". A commit hash answers the first and needs
  a build-arg pipeline to even obtain — which is why this sat open for two days. An
  integer the PRODUCER owns answers the second, needs no Dockerfile change, and is
  asserted against the producer by a test so forgetting to bump it is a failure
  rather than a silent blind spot.

  `envelope_staleness_lines()` renders three outcomes and names the RIGHT remedy
  for each, which matters because the two are opposite: an older container needs
  `swapterm rebuild` and says why a `git pull` does not fix it; a newer one needs
  `git pull`. A missing version key is treated as the OLDER case, not an unknown
  one — v1 had no such key, so its absence dates the container precisely, and
  calling it "could not tell" would have put the most likely stale container in the
  quiet branch. That is the body the operator's container actually served, and the
  test uses it verbatim.

  **Verified on the real deployment** at 01:04: after `swapterm rebuild`, the
  report shows the sync blocks for BTC and LTC and prints NO staleness warning —
  the check passing is the absence of its own output.

  **What is still open, and it is narrower than it was:** this covers
  `/api/admin/chains` only. `up`'s own `code` line still reports the HOST
  checkout's commit, and any OTHER container-served surface has no equivalent
  marker. The general fix is still the baked commit marker. Original note follows.

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
| C52 | **The compose bridge had no `networks:` block at all, so docker chose the subnet — and the operator's firewall rule names one by hand.** Two copies of one fact in two systems that cannot see each other (rule 8), and the failure mode is silent: a reassignment does not error, the rule simply stops matching, and every chain reads NOT CONFIGURED in a container whose environment is perfectly correct. Pinned to `${SWAP_TERMINAL_BRIDGE_SUBNET:-172.18.0.0/16}` — **the value ufw rule 20 already names**, because pinning it to anything else silently requires a second edit in a second system. **And read back, which the remedy as written omitted**: `swap_stack.py status` prints the live subnet beside the declared one via `bridge_subnet_verdict()` (matches / drifted / absent / unknown, and unknown never reads as a match). The read asks the CONTAINER which network it is on rather than guessing `<project>_default`, because under `COMPOSE_PROJECT_NAME` a hardcoded name inspects nothing and reports ABSENT on a running stack — the quietest false all-clear, and the hazard `replica_state()` already records for volume names | `this commit` |
| C53 | **/admin's controls refused on the operator's only deployment, and the operator decided it.** Inside a container `SWAP_TERMINAL_HOST` must be 0.0.0.0 — 127.0.0.1 binds the container's own loopback and the published port reaches nothing (measured 2026-10-05) — so the socket scan saw 0.0.0.0 and `refuse_off_box()` refused, on a stack whose publish is `127.0.0.1:5100:5000` and is therefore private. **The design is the COUPLING, not the variable**: compose interpolates ONE `${}` token into both the host side of `ports:` and `SWAP_TERMINAL_PUBLISH_HOST`, so the declaration cannot drift from the publish — a separate `PUBLISH_IS_LOOPBACK=1` flag would have been two copies of one fact, agreeing the day it was written. Fails closed on absent, empty and non-loopback; reads the variable **only behind a container marker**, so a host cannot declare its own privacy; does NOT relax either not-established branch, because a declared publish says who can reach the port and nothing about what this process is bound to. Seven mutations, seven caught. A compose gate asserts the two halves are the same token — nothing in Python would catch the decoupling, and a security control reading a stale fact fails silently | `this commit` |
| C54 | **And the remedy I first wrote for it dropped three sentences the old one carried.** Two tests I wrote yesterday failed, pinning `do NOT set SWAP_TERMINAL_HOST=127.0.0.1`, `the UI came up empty` and `docker compose port web 5000` — the measurement, the instruction and the check. My replacement had all the new information and none of the old, which is a message regression a reader would only notice while standing in front of the refusal. Fixed by keeping the container facts as a shared preamble and splitting only the TAIL per verdict, so the two remedies cannot drift. **The tests were the only thing that noticed**, which is the argument for having written them before the change they now constrain | `this commit` |
| C49 | **Three character-identical console recorders in three files, and a fourth pair nobody had counted.** `ast.unparse` with docstrings stripped, over every base-less class in `tests/`: 22 console-stub definitions, 19 distinct bodies, 2 bodies at more than one site — the `_Recorder`/`_Recorder`/`_PricingRecorder` triple (whose third docstring read *"Same shape as the other recorders here"*, rule 8's finding written down and left in place), and `_QuietRun` TWICE in `test_grc_htlc_verify.py`, eleven lines below a module-level `_SilentRun` that is a strict superset of both. Now `conftest.TranscriptConsole`, instantiated 18 times across the three files it absorbed (13 / 1 / 4, counted) plus twice in its own tests, and both `_QuietRun`s are `_SilentRun()` — the **stricter** stub, because its `node()` raises and that is exactly the guard the copies lacked. **17 definitions, 17 distinct bodies, 0 duplicated**, held by a clean gate with no allowlist plus a non-vacuity floor, since a gate asserting an absence is loudest when its scanner has stopped working. Also merged: the closest non-identical pair in the whole tree, two 15-line `_Run` stubs at **0.99 similarity** differing in one token (the name of the enclosing test's local) | `this commit` |
| C50 | **And I created the defect I was removing: a second class named `RecordingConsole`.** `tests/test_swap_runners_report_completion.py:96` already had one, whose `check()` records `(label, ok)` into `results` and puts NO check line in `lines`. Two console stubs, one name, disagreeing about what `check()` does — which is `OPEN_FINDINGS`' own "two classes are named `Console`" entry, one layer down, written inside the commit consolidating duplicates. Caught by scanning for stub classes rather than by anything failing. Renamed to `TranscriptConsole` (a transcript: every say and check in one ordered list) against the other's SESSION shape (`step_console.StepSession`, the five-member surface a runner drives); both docstrings name the other, and a test asserts the difference so a later merge cannot flatten them quietly | `this commit` |
| C51 | **A claim that lived only in a comment, in two of the three copies: "a recorder with fewer arguments would silently accept a call the real Console rejects."** It is now four tests (five cases) in `test_step_console.py`, which is the file whose whole subject is two Consoles that look identical — 8 cases there before this commit, 15 after, the other six being the gate and its floor. Signatures are compared as (name, kind, is-required) per parameter against the real `Console` — names because a checker matches them for anything not positional-only, kinds because a keyword call reaches one and raises in the other, and **is-required because its absence SURVIVED a mutation**: the first version compared names and kinds only, and `def check(self, label, got, expected, ok=None)` passed clean, which is word for word the hazard the comment described. Annotations are deliberately NOT compared and a negative control proves it. Six mutations of the recorder, six caught; two of the gate, two caught | `this commit` |
| C47 | **The survivor of a five-copy merge was the one thing with no tests.** `conftest.root_entry_point()` absorbed five hand-written `spec_from_file_location` loaders and **nine call sites across seven files** now depend on it — and nothing asserted on any of its guards, so a mutation in it would have reported as thirty `AttributeError`s in files about the ICP replica, the GRC HTLC harness and the teller pane. That is precisely how the unchecked-`spec.loader` hole survived in all five copies long enough to be **diagnosed four separate times**. Six mutations, six caught: dropping the existence check, the `spec is None` guard, the `sys.modules` registration, the cleanup on a failed exec, the two deliberate module names, and re-adding the dead `sys.path.insert`. **The `spec.loader is None` branch is NOT covered and the module docstring says so** with the measurement — a directory, `README.md` and a nonexistent `.py` reach the other two branches, and nothing in this tree produces a spec carrying no loader; a test asserting the SOURCE contains the check would be "the SQL text contains X", which the behavioral-verification principle forbids | `this commit` |
| C48 | **And my first version of the `sys.path` test was wrong for the reason the whole file is about.** It loaded the REAL `suite_baseline.py` and asserted `sys.path` was unchanged — but every root entry point in this tree does its own `sys.path.insert(0, <root>/swap_terminal)` as its first statement (`suite_baseline.py:80`, `operator_panel.py:59`, `reclaim_funding.py:52`, `grc_htlc_verify.py:79`), because a rule 10 root file cannot import this repository's modules without it. So the assertion measured the MODULE, not the loader, and failed on the first run. It now loads a synthetic entry point that touches nothing, and the comment carries the measurement rather than the assumption | `this commit` |
| C45 | **The window icon starts a DIFFERENT deployment and said it was "Swap Terminal".** Clicked on the operator's host: gunicorn on **:5000**, host database, `swap workers NOT STARTED HERE`, and **all five chains NOT CONFIGURED** on a machine whose `.env` is fully configured. `config.py:17` records why, deliberately: nothing in the serving path loads a `.env`, because `load_dotenv()` at import makes every later import order-dependent (rule 12). `docker compose` reads `.env` by itself, so the containerized stack gets the chain ports and a host gunicorn gets none. Two deployments, one blind to the operator's config, and the only thing that said so was five lines offering the remedy for a *different* problem ("set BTC_RPC_PORT", when what they have is a `.env` this server does not read). Icon renamed to **Swap Terminal: Window (dev server)** with a Comment naming the port, the reason, and `swapterm up` | `this commit` |
| C46 | And the launcher now prints the actual remedy when nothing is reachable — naming `swapterm up`, both ports, and the warning that both servers can run at once over one database. The count comes back from the child as a **sentinel line**, stripped before display, rather than being recovered by searching the rendered text for `NOT CONFIGURED` — the same read-a-fact-out-of-prose defect removed from `serving_verdict()` and from three assertions in `test_desktop_launcher.py` today. `-1` means NOT ESTABLISHED and stays silent; only an actual `0` prints the remedy, and the first version got that right by accident (`if configured:` is false only for 0, and -1 is truthy) | `this commit` |
| C42 | **The icons did nothing when clicked. `StartupNotify=true` was why.** Diagnosed from the operator's OWN machine: `~/Desktop/mammon-restart.desktop` has worked since September and differs in exactly two ways — `StartupNotify=false`, and `Exec` invoking `/bin/bash` explicitly. With `StartupNotify=true` the desktop waits for a startup-notification completion that a terminal emulator opening a shell script never sends, so the launch sits pending and silently gives up. `Terminal=true` and `StartupNotify=true` are near mutually exclusive, and every working launcher on that desktop says false. Both now match the measured pattern, on both templates | `this commit` |
| C43 | **The installer printed four GNOME `gio set metadata::trusted` commands at an operator running LXQt**, where pcmanfm-qt reads the execute bit and no such metadata exists. Advice for a desktop you are not running is noise, and rule 14's argument is that noise trains the reader to skip the block that matters. `trust_note()` now has three cases — needs it, does not, and NOT ESTABLISHED — and the third is the one that was missing: saying nothing on an unknown desktop would leave a GNOME user with dead icons and no reason | `this commit` |
| C44 | **And three of my own assertions matched prose instead of directives.** `"StartupNotify=true" not in entry` was true of the template's own COMMENT, which quotes the wrong value to explain why it is wrong; `"gio set" not in lxqt` was true of the message that exists to say the step is unnecessary; and `"not established"` straddled a hand-wrapped line break. A `.desktop` is an ini file and is now read as one, and prose assertions collapse whitespace first. **Fourth instance today** of an assertion true of something adjacent to its claim — the others were the vacuous ATM screen test, the `BITCOIN_FAMILY` value comparison, and the menu line | `this commit` |
| C41 | **A THIRD hardcoded spelling of the icon list, in the same function, surviving the commit that derived the other two.** `menu  "Swap Terminal" (the window), plus Up, Down and Restart` — printed to the operator after `rebuild` got an icon. **And C39's test passed anyway**: it joined `closing_lines()` into one string and asserted each action appeared in it, which the `shell` line alone satisfied, so the wrong `menu` line was invisible to it. A test that passes because the right word appears on the wrong line is a test passing for a reason unrelated to its claim — the third time today one of mine has. Now derived from `ACTION_ENTRIES`, and asserted **per line**: the menu line must name every icon AND must not name an action in `NO_ICON` | `this commit` |
| C39 | **The installer advertised a wrong command set, and the operator read it.** `rebuild` went into `swap_stack.ACTIONS` in `a7bc6d2`; `install_desktop_icon.py` kept its own hand-written list and printed `shell  swapterm up \| down \| restart \| status \| chains` on their host — rule 8 in operator-facing text, found by them reading it rather than by anything failing. Both printed spellings now derive from `ACTIONS`, and the coverage is **asserted rather than the list**: every action is either an icon or named in `NO_ICON` with a reason, so a new action can be deliberately iconless and cannot be accidentally iconless. Mutation-checked with the real defect — adding an action to `ACTIONS` and nothing else | `this commit` |
| C40 | **And the icon set had the stale-code trap `rebuild` exists to fix.** With only Up / Down / Restart, an icon-driven operator pulls, clicks Up, and serves whatever was baked into the web image at its last build — with nothing on screen saying so. `rebuild` now has an icon, and its Comment says when to use it (after a `git pull`), why Up alone is not enough (the app is baked in), and that it leaves the ICP replica alone | `this commit` |
| C38 | **`swapterm rebuild` — the action a `git pull` actually needs.** `docker/web.Dockerfile:150` is `COPY swap_terminal ./swap_terminal`, so the app is BAKED IN and the only mounts are `/data` and `/runtime`: a pull changes nothing the container serves, while `up` reports the HOST checkout's commit and passes no `--build`. That is rule 13's "verify the artifact, not the deploy" failing in the section written to prevent it. `rebuild` builds **`web` only, by name** — a bare `docker compose build` builds every service in all three `-f` files, and `up` RECREATES a container whose image changed, which is what destroyed the ledger canister on 2026-10-07 while `up` went on printing SERVING. Then it `restart`s rather than `up`s, because a fresh image is no reason to skip proving the stop. A failed build changes NOTHING — the old image is still serving. Three mutations each caught: bare build, ignored exit code, `cmd_up` instead of `cmd_restart` | `this commit` |
| C36 | **I made `("BTC","LTC","GRC")` a sixth spelling, in the commit about consolidating duplicated chain knowledge.** `config.py:308` and `workers/common.py:108` both already record it as spelled five times, and `config.py:1339` says there is deliberately no tuple in that file *"-- rule 8 counts copies"*. Found only by going looking afterwards. `BITCOIN_FAMILY` now derives from `coin_amounts.CHAIN_DECIMALS`, whose own docstring explains XRP/SOL are absent because they send integer base units — a property of not being Bitcoin-family, decided where it had to be. The other five are **not** merged: they are five different concepts (wallet custody, swapper support, fee measurability, P2PKH, Core release) that can diverge, so each now NAMES the other four, which is rule 8's harder half. **The value test was not enough** — restoring the literal passed it clean, so the derivation is asserted on the source | `this commit` |
| C37 | And twenty minutes later I nearly wrote the **sixth** copy of the `spec_from_file_location` loader, in the test for that same commit. `OPEN_FINDINGS` has said *"One conftest.py helper fixes all five"* since they were counted. `conftest.root_entry_point()` is now that helper, and it is better than all five: it takes a RELATIVE PATH (one of the five loads `docker/icp_replica_entrypoint.py`, not a root file), keeps the two deliberate distinct module names, drops the `sys.path.insert` that conftest already does, and **checks the file exists first** — which `test_solana_payout.py` measured as the gap, since a nonexistent path still produces a perfectly good spec, so `spec is None` never fires for the realistic failure | `this commit` |
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
