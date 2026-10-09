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

### 8. pyright reports 488 findings across 94 files — 87 was a subset
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

**Being fixed now, 2026-10-09, by eight agents on file-disjoint buckets** under a
written mandate: no `type: ignore`, no `pyright: ignore`, no `cast()` added to
silence a finding, no widening a parameter to `object`/`Any`, no new baseline
(rule 19), and in tests no assertion may be weakened — annotations may change,
behavior may not. Every finding is classified as (1) a real defect, which gets a
failing test, (2) a missing or wrong type, or (3) a wrong comment the finding
exposed. Anything with no honest fix comes back named rather than suppressed.

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

### 9. `static/script.js`: three entry functions are dead
`wireQuoteForm` (L122), `wireSwapForm` (L252), `wireLampFilter` (L604) read
`#quote-form`, `#swap-form`, `#lampstrip`. **Zero** templates define any of the
three — established by grepping `templates/` for the NAME, not by the import
graph (rule 2). They left with `templates/index.html`. ~266 of 666 lines.

### 10. `destinations_for()` leaks operator text to the customer
Screen 2 can render `BTC → SOL: SOL has no adapter in this process: SOL_RPC_URL
is unset... exported in the shell that starts the server`. Pre-existing;
`services/wizard.destinations_for()` passes `row["reason"]` straight through.
Invisible to `test_no_operator_facing_remedy_text_reaches_the_customer_page`
because that test GETs `/` and this only appears after a POST.

### 11. `create_quote()` stores `output_amount_estimate` unquantized
`300.0000010967742` GRC for an 8-decimal chain. Not a money-path defect —
`payout_service` runs `quantize_for_chain()` before broadcast — but it is on the
order path, so named rather than changed.

### 12. `routes/atm.CARRIED` includes `quote_id`, which no screen sets
Reachable only by a crafted POST; `answers_after_back()` clears it and two tests
pin that. Harmless, named.

### 13. `node:22-alpine` is a floating tag with 11 high vulnerabilities
**Owner: operator**, although it is filed under this section because I found it —
it needs a docker daemon, which this session does not have.

`swap_terminal/grc-sol-swap/abstergo_exchange/Dockerfile:28` and `:46`. Docker
DX reports 11 high vulnerabilities in the base image it resolved
(`sha256-2c752226…505a8`).

**I cannot verify or fix this from here and I am not going to pretend
otherwise** (rule 17): there is no docker daemon in this session, so I cannot
pull the image, cannot run a scan, and cannot tell you whether a different tag
has fewer. The count above is the language server's, relayed.

What IS establishable by reading the file, and is a real defect independent of
the vulnerability count: `22-alpine` **floats**. It resolves to whatever the
latest 22.x alpine is at build time, so two builds of this Dockerfile a week
apart produce different images — in a file whose own header is entirely about
being able to say what is running ("verify the artifact, not the deploy", rule
13). Pinning a digest would make the build reproducible and would make a
vulnerability count mean something, because it would be a count of a specific
image rather than of a moving target.

**That is a proposal, not a fix** (rule 16): it changes what gets deployed, and
choosing the digest needs a scan I cannot run. The two commands that would
settle it are yours:

```
docker pull node:22-alpine && docker image inspect node:22-alpine --format '{{index .RepoDigests 0}}'
docker scout cves node:22-alpine
```

Worth knowing before you spend time on it: this server signs Solana payouts
(`server.js:245`), so its base image is not a cosmetic concern — but the 11
highs are in the base image's own packages, not in this repository's code, and
a `node:22-alpine` with zero highs may simply not exist today.

### 14. The review screen speaks operator, not customer
Screen 5 renders, verbatim and in a highlighted panel:

> **RUN GREEN -- atomic_swap.py (P2SH HTLC on both legs); this terminal settles
> it CUSTODIALLY, with no hashlock**

`RUN GREEN` is a posture token and `atomic_swap.py` is a filename in this
repository. Neither means anything to someone about to send money, and the one
sentence under it that DOES mean something to them — "this desk holds your funds
between your deposit confirming and your payout being broadcast" — is the small
print under the jargon.

Same class as finding 10 (`destinations_for()` leaking remedy text), one screen
further on. Found by RENDERING screen 5 in Chromium, which is the only way it
was ever going to be found: no test asserts on that panel's wording.

Also on that screen, and a nit rather than a defect: the answers strip at the
top repeats "YOU SEND 0.0002 BTC" and "PAID TO mqT6…T3M", and the detail list
twelve pixels below says both again. They cannot disagree — one template, one
context — so this is visual redundancy, not rule 8's drift.

---

## Closed

| | what | commit |
|---|---|---|
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
