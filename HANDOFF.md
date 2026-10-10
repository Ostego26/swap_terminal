# Handoff — 2026-10-10

Written at the operator's request, mid-session. Everything below is MEASURED from
this host or pasted from the operator's terminal unless a line says otherwise.
Where something is a guess, it says so (rule 17).

Branch `claude/xrp-adapter`, last commit `f7c2257`. Suite: **4596 passed, 3 skipped,
0 failed**, baseline recorded at 4599, 0 broken at record.

---

## 1. STOP — the one thing that can destroy state

**Never run host `dfx` inside `icp/`.** Measured on the operator's host today:

```
$ dfx canister id icp_ledger_canister     # run in icp/
Error: failed to ensure cohesive network directory
Caused by: failed to remove directory .../icp/.dfx/local and its contents
Caused by: Permission denied (os error 13)
```

`icp/.dfx/local` is root-owned because **the replica runs in Docker**. Host dfx saw
a network directory it had not created, judged it incohesive, and moved to DELETE
it. Only the permission error stopped it. That directory is the local replica's
state, including the ledger holding the desk's ~998.9498 LICP.

The correct transport is in `chains/icp.py` (~line 360) and is already implemented:

| `network_url` | transport |
|---|---|
| set | `dfx canister call --network <url>` from this process |
| empty | `docker compose exec -T icp-replica dfx ...` |

`icp-replica` is the COMPOSE SERVICE name. The CONTAINER name is
`swap-icp-replica` and `docker compose exec` rejects it — that distinction is
written down at the call site because it was got wrong once.

**I handed the operator a block that used host dfx in `icp/`. That was wrong and
it is the single worst thing I did this session.** Any ICP call must go through
`docker compose exec` or `--network`, never host dfx with `dfx.json` in scope.

## 2. Where the keys are — answered, because the operator asked

**Not in ICP.** `icp/threshold_custody/src/lib.rs` line 6, its own header:
`Can move funds: NO. It never calls sign_with_ecdsa, holds no chain client`. Its
only functional endpoint is `public_key(derivation_path)`. There is no signing
endpoint.

Every swap today signs with a LOCAL key on the operator's host:

| chain | key |
|---|---|
| BTC | `desk_hot`, descriptor wallet |
| LTC | `desk_hot`, legacy wallet |
| GRC | the operator's own staking wallet (ENCRYPTED) |
| SOL | a keypair JSON under `~/.config/solana/` |
| XRP | `ST_ADAPTOR_FUNDING_SEED` in the environment |
| ICP | the desk dfx identity, **inside the replica container only** |

`chains/icp.py` has `can_spend = False` because the process calls with
`--identity anonymous`: reads work, `transfer` debits the caller. Moving custody to
canister-derived addresses is armed state and the operator's call;
`icp_custody_addresses.py` exists read-only so the two columns can be compared
first.

**And that tECDSA key does not survive `docker compose down`** — measured
2026-10-07, same canister id, same `dfx_test_key`, two different keys across one
ordinary recreation. A `docker compose down` DID happen earlier today. Funds at a
canister-derived address on a local replica are lost on container replacement, so
that custody path is not safe to arm against a local replica.

## 3. Architecture — has NOT drifted

- Docker: 8 compose files; `docker-compose.yml` and `docker-compose.web.yml` both
  touched today.
- Canisters: `threshold_custody` and `operator_admin`, Rust, wasm built for
  `wasm32-unknown-unknown/release`, plus `icp/dfx.json` and
  `docker-compose.icp.yml`.
- **There is NO asset/frontend canister** and never has been. The web app is Flask
  under gunicorn: it opens JSON-RPC sockets to bitcoind/litecoind/gridcoinresearchd,
  holds a SQLite file and shells out to dfx. A canister has no arbitrary outbound
  sockets, no filesystem and no subprocesses, so serving this backend from one is a
  rewrite, not a setting. The operator asked; this is the answer.
- `swap_terminal_desktop.py` is a FOREGROUND launcher and holds the terminal by
  design (gunicorn master + 2 workers, group-killed on SIGINT). It starts the WEB
  APP ONLY — `deposit_watcher`, `payout_worker` and `reconcile_worker` are NOT
  started by it, deliberately (`--with-workers` is explicitly not implemented).
  The detached path is `swap_stack.py up` (Docker).

## 4. Live swaps

| swap | pair | state |
|---|---|---|
| `s_0dc53d06ab3968fb` | BTC -> GRC | **deposit SENT**, txid `15c117b46dc72d7166cc4a407d03a863db41f275b2059b0553028425e068a543`. `min_confirmations` 2. Payout address `mg3gJAmhADxf2ScRuXu7HXM2oixxiQG2Ap` CONFIRMED owned by the GRC wallet. |
| `s_a4a6843038be2d74` | ICP -> GRC | awaiting deposit of 2.32846520 ICP (232846520 e8s) to `8779408f04f0e82d080fe4914d395973aecf194b49e0ddf23b09b5a767c6d9da`. NOT SENT — blocked on the dfx transport above. |
| `s_993e9efe7dad39c8` | — | retirable at 2026-10-11T00:29Z; `expire_swap.py --apply` refuses correctly until then. |

**The ICP deposit's idempotency key is `1791646582186023936`** — the swap's
`created_at` in nanos. The ledger dedups on it for 24h, so a retry with that exact
value is ONE transfer. A fresh key is a second one. `chains/icp.transfer_argument()`
builds the candid record; the argument was verified here (232846520 e8s, correct
blob escaping, exact round-trip). The fee must be read from the ledger — a
mismatch is `BadFee` naming `expected_fee`, and nothing moves.

**Quote windows are 600s.** That is too short for a hand-run deposit loop and the
operator hit it. Worth changing before the next attempt.

## 5. Funded testnet wallets

| chain | state |
|---|---|
| BTC | testnet4, SYNCED, `desk_hot`, **trusted 0.02** (both faucet payments confirmed) |
| LTC | `test`, mid-IBD, ~1.8M behind, **validation-bound not peer-bound** (0.055 MB/s against a 2 MB/s floor); ~7h at 71.7 blocks/s |
| GRC | testnet, 2000 tGRC spendable, wallet ENCRYPTED, no `getbalances` |

Faucet: CypherFaucet, keyless JSON API, `testnet_wallets.py --faucet`. Rate limit
is per address and per IP **PER NETWORK** (measured: both chains paid 7s apart).

## 6. What I got wrong today — read this before trusting a diagnostic

Five claims stated as measurements that were not:

1. **"one peer is why the sync is slow"** — printed in a block the operator pasted
   back twice and acted on. Refuted: 0.055 MB/s, validation-bound, one peer was
   feeding the node faster than it could validate.
2. **"the payout address is not yours"** — I asked the BTC daemon about a GRIDCOIN
   payout address. It IS theirs. Cost the operator real anxiety on a 600s clock.
3. **"the 0x6F ambiguity is silent"** — `address_authority` already prints
   "a version byte BTC, GRC, LTC share, so this encoding cannot narrow it further".
4. **host dfx in `icp/`** — section 1.
5. A detector that read its own search string (fourth prose-reading detector of
   the session; now an AST walk).

The pattern is one defect wearing different clothes: **a claim written in the
register of a measurement.** Before stating what the live system is doing, run the
thing that would show it false, then say which you have.

## 7. Real gap, not yet fixed

`services/swap_service.refuse_unusable_payout_address()` reads `verdict.why` ONLY
when the verdict refuses or is unchecked. For a **VALID** verdict the `why` — which
carries the `0x6F` "cannot narrow it further" sentence — is computed, returned and
dropped one line from where it would help. That is the actual hole behind the
2026-10-01 incident (82.65 tGRC to an `ismine:false` address), and the sentence
that would close it already exists.

Surfacing it is a reporting fix. REFUSING an ambiguous address is posture and the
operator's call. The ambiguity should become a structured field on
`AddressVerdict` rather than something a caller greps out of prose — this session
made the prose-reading mistake four times.

## 8. Operator's standing instructions

- American English in prose; never in identifiers.
- Timing in `µfn` (U+00B5, no space): `2.3µfn (2.8s)`. 1µfn = 1.2096s.
- Placeholders NEVER inside a code block.
- No `read`, `sudo` or `exit` inside a pasted multi-line block.
- Never move, copy, read or echo a key: not `.env`, not a keypair JSON, not a WIF,
  not `wallet.dat`, no `dumpprivkey`, never `/proc/<pid>/environ`.
- A passphrase must never appear in a command this repo emits, and the panel must
  never have a passphrase field.
- TESTNET ONLY.
- Push first, then run suites in the background.
- No workflows (standing, overrides the ultracode nudge). One agent at a time.
- Say it plainly when non-swap_terminal content lands in this window.

## 9. The GUI that landed today

`/admin/wallets` plus six sub-tabs (`/BTC /LTC /GRC /XRP /SOL /ICP`), all GET,
verified by rendering all six (200s; unknown chain 404s naming the six). `?ask` is
what makes the read-only calls. **No Send and no Receive, deliberately** — Receive
is `getnewaddress`, which derives and stores a key in `wallet.dat`; adding either
needs a POST on an unauthenticated surface and is the operator's call.

**Nothing in that work ran against a real daemon** — every figure in its tests came
from a seeded adapter. `?ask` on BTC is its first genuine exercise.
