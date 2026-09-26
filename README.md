# swap_terminal

## Running the API: which of the two servers you are actually on

There are two ways this application serves HTTP and they are not
interchangeable. The banner each one prints says which you are looking at.

    production      gunicorn -c gunicorn.conf.py wsgi:app
    development     python3 swap_terminal/app.py

**gunicorn is the production server.** `wsgi.py` at the repository root is the
entry point it imports, and `gunicorn.conf.py` beside it carries the settings.
Under gunicorn, `app.py`'s `if __name__ == "__main__":` block is **never
reached** -- which is strictly better than relying on its defaults, because
nothing is then depending on a keyword argument being right. `app.py` keeps
that block for local development and it is unchanged.

`app.py`'s `debug` and `host` handling still does its job, and its job is now
narrower. It governs the development server only:

    SWAP_TERMINAL_DEBUG   unset/off by default. Turning it on starts the
                          Werkzeug interactive console, which executes Python
                          as a process holding the wallet RPC credentials.
                          gunicorn has no equivalent and never runs it -- so
                          under gunicorn this variable does nothing at all.
    SWAP_TERMINAL_HOST    127.0.0.1 by default. gunicorn.conf.py reads THE SAME
                          variable for its `bind`, deliberately, so that moving
                          from one server to the other does not silently change
                          what the API is reachable from.
    SWAP_TERMINAL_PORT    5000 by default, and also shared with gunicorn.

Gunicorn-only settings, all with defaults that need no configuration:

    GUNICORN_WORKERS           2. Sync workers. See the warning below.
    GUNICORN_TIMEOUT_SECONDS   60, above the 30s default of the wallet RPC
                               calls a request can be waiting on.
    GUNICORN_LOG_LEVEL         info.

### The WSGI process serves HTTP and starts no workers

**Gunicorn forks N worker processes and every one of them imports the
application.** Anything started at import time, or inside `create_app()`, is
therefore not started once -- it is started **N times, in N processes**, and
nothing crashes and nothing logs an error.

This repository has already measured what the plural form of that costs. Two
payout workers paid the same swap twice: 2 sends, 1 swap id, 2 `broadcast`
rows, on-chain and final. Those were two processes started by hand.
`GUNICORN_WORKERS=4` would make four the default, silently, on every restart.

So the division of labor is fixed and no setting changes it:

| | |
|---|---|
| the gunicorn process | serves HTTP. Starts no workers, ever. |
| `swap_terminal/supervisor.py` | owns the deposit watcher, the payout worker and the reconcile worker, and is their reaper. |

`preload_app`, `workers`, `threads` and the worker class are tuning for the
first row. **`preload_app` is not a fix for a background thread** and it is
worth saying because it looks like one: only the forking thread survives
`fork()`, so preloading turns "N copies running" into "zero copies running, in
a parent that has forked away from them". A different wrong answer, not a right
one.

Two mechanisms enforce this, and neither of them is this paragraph:

- `wsgi.py` snapshots the process's threads and child pids around
  `import app` and **raises** if either grew. It runs in every worker, because
  every worker imports `wsgi`. A worker that spawned something during import
  never serves a request.
- `gunicorn.conf.py`'s `post_fork`/`post_worker_init` hooks re-measure across
  the fork -- the window `wsgi.py` cannot see -- and print the result either
  way, so a clean boot says `spawn guard: 0 threads, 0 child processes started`
  rather than saying nothing.

`tests/test_wsgi_spawn_guard.py` holds it in the suite, including a test that
starts a real thread in a real subprocess and asserts the real guard refuses.

Start the workers separately, and only ever once:

    python3 swap_terminal/supervisor.py start
    python3 swap_terminal/supervisor.py status
    python3 swap_terminal/supervisor.py stop

## Solana

SOL is wired into the Flask app as a chain adapter
(`swap_terminal/chains/solana.py`). **The read-only half is implemented; the two
methods that move funds refuse**, and this section says which is which and what
the operator still has to decide.

**No SOL trading pair is enabled.** `Config.ALLOWED_PAIRS` is unchanged, so no
SOL quote can be produced and no SOL swap can be created. Adding a pair is live
posture and is the operator's. The adapter is also only constructed when
`SOL_RPC_URL` is set -- otherwise it is left out of the adapters dict entirely,
so an unconfigured Solana endpoint cannot make the workers log a warning every
cycle about a fault that does not exist.

### Configuration

    SOL_RPC_URL            no default, deliberately. The other three chains
                           default to MAINNET ports; defaulting this would
                           mean picking somebody's public cluster. Unset makes
                           every call refuse with a message saying so.
    SOL_RPC_COMMITMENT     processed. Discovery reads at the LOWEST level so a
                           deposit that has landed but not settled is visible;
                           the credit gate is separate and is the rank below.
    SOL_SPL_MINT           empty = native SOL. Set it to a mint (wGRC) for the
                           SPL path.
    SOL_HOT_WALLET         the PUBLIC key paid out from. There is no keypair
                           path variable here; nothing in the Python adapter
                           can sign.
    SOL_MIN_CONFIRMATIONS  3. **Not a count of blocks** -- see below.
    SOL_RPC_TIMEOUT        30 seconds.

### Confirmations are a category mismatch, and what was done about it

Every other chain here is proof-of-work: a deposit gets safer as blocks pile on
top, and `min_confirmations` counts them. **Solana has no such count.** It has
commitment levels -- `processed`, `confirmed`, `finalized` -- describing how
much of the validator set has voted.

Two tempting numbers were both rejected, because each reads exactly like a
confirmation count and neither is one:

- Solana's own `confirmations` field is blocks *since the transaction was
  confirmed*, is `null` once finalized, and is **not monotonic in safety**: a
  `processed` transaction at depth 500 is less safe than a `finalized` one at
  depth 5.
- Slot depth is monotonic, which is what makes it worse -- it carries no safety
  claim at all.

So the integer is a **commitment rank** on an explicit four-rung ladder, named
that everywhere, derived in one place (`chains/solana_units.py`):

    0  unknown      the cluster has no status for this signature
    1  processed
    2  confirmed
    3  finalized    <- the default threshold

`min_confirmations` therefore means "the integer a deposit's own integer must
reach before it is credited" on every chain, which is what one vocabulary asks
for. It does **not** mean 3 SOL-rungs is comparable to 3 BTC blocks, and
nothing compares them.

**A Bitcoin-style value is refused rather than obeyed.** Setting
`SOL_MIN_CONFIRMATIONS=6` -- copying Gridcoin, an entirely reasonable thing to
type -- would ask for a rung that cannot exist, so every SOL deposit would sit
at rank 3 forever, no payout would fire, and nothing would be logged as wrong.
The adapter raises at construction with the ladder printed in the message.

Slot depth is still *reported*, labeled `diagnostic only -- NOT the gate`.

### Rent replaces dust; fees are per signature

- **Rent is not dust, and is deliberately not in `modules/htlc_fee.py`.** A
  dust output is *refused by relay* -- nothing is lost. A Solana account below
  the rent-exempt minimum is *accepted*, exists, and is then collected by the
  runtime; the funds go away afterward. `htlc_fee.dust_threshold_satoshis()`
  also takes a Bitcoin `scriptPubKey` and a per-kvB relay rate, neither of
  which Solana has. The reasoning is recorded at both ends.
- **Fees are 5,000 lamports per signature**, not `rate x size`. A one-signature
  transfer costs the same whether it moves one lamport or a million SOL.
- The adapter **asks the chain** (`getMinimumBalanceForRentExemption`) rather
  than trusting a constant; the constants in `solana_units.py` are labeled
  reference values, because nothing here has read them off a cluster.

### wGRC and SPL tokens

wGRC is an SPL token, not native SOL, so the token path is first-class rather
than an afterthought:

- **Balance** is read from the owner's associated token account with
  `getTokenAccountBalance`, never `getBalance` -- which would report the
  account's *lamports* and be wrong by whatever the token is worth.
- **Decimals come from the mint**, read off the mint account. Never SOL's 9.
- **Deposits** are matched on `owner` **and** `mint`. Matching on owner alone
  would credit another token's deposit at this mint's decimals.
- **Token-2022** mints derive a *different* associated token account for the
  same owner and mint. Both are well-formed addresses and only one holds the
  balance, so the adapter asks the mint account which program owns it.
- **Payout addresses must be on-curve.** An associated token account is a
  Program Derived Address: valid, 32 bytes, base58 -- and no private key exists
  for it. A customer pasting their token account instead of their wallet hands
  over a string that passes every syntactic check, and the chain does not
  refuse the transfer. `validate_address()` does.

What still needs the operator: whether the hot wallet or the recipient pays the
rent-exempt minimum when a recipient has no associated token account yet. The
transfer plan reports the cost and does not choose.

### Solana deposit addresses -- the operator's decision

**This is the one thing that is handed back rather than chosen, and
`get_new_address()` refuses until it is settled.**

Bitcoin, Litecoin and Gridcoin answer this with `getnewaddress`: the *daemon*
derives a key, stores it in `wallet.dat`, and this application never holds a
secret. Solana has no equivalent -- no wallet daemon, no keystore, nothing on
the other end of an RPC that can mint an address and remember how to spend from
it. **So the application must hold something, and which something is a custody
decision.**

| option | what is held | what breaks |
|---|---|---|
| **fresh keypair per swap** | a private key **per open swap**, stored by this app | the secret surface grows with business. Every open swap is a key that must be encrypted at rest, backed up, and swept after settlement. A backup that captures the database captures spendable funds. This repository has already leaked a Solana keypair once, through a `.bak` file. |
| **one deposit account + per-swap memo/reference** | nothing new. One public key; the private key stays wherever the hot wallet already lives | attribution. Two customers sending identical amounts within a poll interval, or a sender whose wallet drops the memo, produce a deposit that must be matched to a swap by something other than the address -- and a wrong attribution **pays the wrong person**. |
| **derivation from a seed** | one secret, deriving many addresses | the seed becomes the single thing that must never leak, and it is worse than one key: it controls every address ever derived, past and future. Solana has no BIP32-style *watch-only* public derivation, so the seed must be present to derive at all -- the app cannot hold a public parent the way a Bitcoin xpub allows. |

**Recommendation: one deposit account plus a per-swap reference, and treat
attribution as the thing to engineer.**

The reasoning, against what this codebase already does:

1. **It is the only option that adds no secret.** `app.py`'s own header says
   this process "never calls sendtoaddress" and that its holding wallet RPC
   credentials is already the reason the debugger defaults to off. Every other
   option makes the Flask process, or its database, custodial. The repository's
   own history is the argument: a committed keypair reached GitHub through a
   backup file, and `tests/test_no_key_material_is_tracked.py` exists because
   of it.
2. **The attribution problem is already solved here, for the same reason.**
   `swap_terminal.db` is the authority, swaps already carry an
   `expected_input_amount` and a tolerance band, and
   `refresh_swap_from_chain()` already routes an out-of-range deposit to
   `under_review` for a human rather than crediting it. A memo strategy
   inherits that machinery: an unmatched or ambiguous deposit halts, which is
   the same safe direction.
3. **A halt is recoverable and a wrong payout is not.** The failure mode of the
   memo option is a swap that stops and waits for an operator. The failure mode
   of a per-swap keypair is a leaked key, and of a seed, all of them at once.
4. **The Node bridge never had to solve this** -- `server.js` takes deposits in
   *Gridcoin* and pays out in SOL, so it needs no Solana deposit address. There
   is no existing choice here to be consistent with.

What the operator has to decide before this can be built, and the reason it is
not a code question: whether an unmatched deposit to the shared account halts
for manual review (safe, and more operator work) or is auto-matched by amount
within a window (less work, and a wrong attribution pays the wrong person). The
second is fund movement.

### What has not been proven

**Nothing in the Solana path has been exercised against a Solana cluster.**
Measured 2026-09-25: `api.devnet.solana.com` and `release.anza.xyz` both return
403 from this environment's proxy, so no RPC endpoint was reachable and
`solana-test-validator` could not be installed -- its only distribution
channels are those two hosts and `github.com`, all denied.

So the RPC method names, parameter shapes and response field names were written
from Solana's JSON-RPC documentation and from the shapes `server.js` already
relies on, and **if one of them is wrong, the tests still pass and the adapter
fails on the operator's first real call.** The pure functions are tested
directly; the adapter is tested against seeded responses. The proof that it
talks to a real cluster is a run against `solana-test-validator` or devnet, and
it has not happened.

### Proving it against a real cluster -- `solana_chain_check.py`

`solana_chain_check.py` at the repository root is the run that would settle it.
It is **read-only**: it signs nothing, broadcasts nothing, opens no database and
reads no key, and there is no flag that changes that.

    source .venv/bin/activate
    export SOL_RPC_URL=http://127.0.0.1:8899          # a local test validator
    python3 solana_chain_check.py

    # or devnet, with an address and a mint to inspect:
    export SOL_RPC_URL=https://api.devnet.solana.com
    python3 solana_chain_check.py --address <wallet> --mint <spl mint>

It **identifies the network from `getGenesisHash`, not from the URL**, because a
cluster cannot lie about its genesis and a hostname can -- an operator with a
"devnet" alias pointed at mainnet would otherwise read the word devnet all the
way to a real transfer. Mainnet prints as `MAINNET-BETA  <- REAL MONEY`.

Every step announces before it runs, carries its own elapsed time in
microfortnights, and says what its number means. `(none)` is printed for an
empty result rather than a blank gap. It exits non-zero if any step's response
does not have the shape `chains/solana.py` expects -- which is the specific
thing that could not be verified where the adapter was written, so a failure
here is the useful outcome, not a broken script.

`tests/test_solana_chain_check_units.py` covers what is provable without a
cluster: the genesis mapping, the exit codes and the output properties. The rest
is the operator's run.

The two cryptographic derivations *were* verified, against `solders` in a
throwaway virtualenv: ed25519 curve membership over 7 named vectors and 4,000
random keys, and associated-token-account derivation over 300 random triples
across both token programs. Zero mismatches. `solders` is deliberately not a
dependency.

## Ethereum: deliberately not implemented

Measured and deferred on 2026-09-25. This is a decided question, not a gap
nobody looked at — the numbers below are the reason, so they do not have to be
re-derived.

### ETH does not fit the amount columns, and no cheap type does

`db.py` stores every amount as `REAL`. ETH has **18 decimals**.

| type | exact integers up to | in ETH |
|---|---|---|
| `REAL` (IEEE double) | 2⁵³ = 9,007,199,254,740,992 | **0.009 ETH** |
| `INTEGER` (int64) | 2⁶³−1 = 9,223,372,036,854,775,807 | **9.22 ETH** |
| uint256 (what the EVM uses) | 1.16 × 10⁷⁷ | 1.16 × 10⁵⁹ |

A `REAL` cannot hold 0.01 ETH in wei. An `INTEGER` overflows on a 10 ETH swap,
and an ERC-20 with 18 decimals and a low unit price blows past it entirely.
Float arithmetic loses wei on ordinary amounts — measured over 200,000 random
values, `9.048138690642862` ETH comes out 80 wei high, `17.137403264689` comes
out 1,600 wei low.

**int64 is ample for every other chain**, which is why only ETH forces this:

```
BTC/LTC/GRC   8 decimals    92,233,720,368 coins
XRP           6 decimals     9,223,372,036,854
SOL           9 decimals         9,223,372,036
XMR          12 decimals             9,223,372
ETH          18 decimals                  9.22   <- the only one that fails
```

### The migration that would be needed, priced

A minimal big-endian `BLOB` of base units, measured against every alternative
over 100,000 rows after `VACUUM`:

| option | bytes/row | exact | verdict |
|---|---|---|---|
| `REAL` (today) | 17.0 | no | — |
| **`BLOB` minimal** | **13.6** | yes | cheapest, and **smaller than today** |
| `INTEGER` + overflow `BLOB` | 15.1 | yes | +10.5% and two representations for one value |
| `TEXT` decimal | 28.3 | yes | 1.7× |
| `BLOB` fixed-32 | 41.4 | yes | 2.4× for sortability nothing uses |
| `TEXT` zero-padded | 89.3 | yes | 5.3× |

So storage is not the obstacle — the `BLOB` is *cheaper* than the current
`REAL`, because SQLite packs a short integer into few bytes while a `REAL` is
always 8 bytes.

**The obstacle is blast radius.** It would touch 7 amount columns across 4
tables, the 5 Python `sum(float(...))` sites, and the event-dict contract of
**all six existing adapters** — because ETH in base units while the others use
floats is the duplication rule 8 exists to prevent. It also requires an
asset→decimals authority that does not exist yet: XMR, SOL and XRP each declare
their own, and BTC/LTC/GRC declare none (only `SATOSHI = 1e-8` as a float
tolerance in `chains/base.py`).

### What would have been easy

Worth recording, so nobody assumes ETH is hard everywhere. Unlike Solana, ETH
uses BIP32, so per-swap deposit addresses can be derived from an **extended
public key with no spend key on the web host** — better than any account-model
chain here on that axis. And ETH has contracts, so unlike Monero it could join
the atomic-swap path.

The awkward parts are gas (sweeping a deposit address means funding it with ETH
first, and an ERC-20 transfer needs ETH the depositor never sent) and the
18-decimal storage above.

### If this is revisited

Do the asset→decimals authority first; it is owed under rule 11 regardless of
ETH, and it is the prerequisite for everything else here.

## Gridcoin: a staking wallet cannot pay out

Operator, 2026-09-26: *"for grc you always have to lock, fully unlock, and then
lock the wallet and then leave it regularly unlocked for staking."*

This is a precondition **no other chain here has**, and it is the one a GRC
payout leg fails on. A Gridcoin wallet that stakes is normally left unlocked
**for staking only**, and a staking-only unlock **cannot send**. So the sequence
around a payout is:

1. lock
2. fully unlock (not staking-only)
3. send
4. lock again
5. unlock for staking, which is the normal resting state

Leaving it fully unlocked between payouts is a security regression on a live
wallet, which is why step 5 is part of the sequence and not an afterthought.

**This terminal does not automate any of it, deliberately.** A full unlock needs
the wallet passphrase, and that is a secret the swap terminal does not hold and
should not — automating the unlock means storing the passphrase somewhere the
payout path can reach, which converts a compromised terminal into a drained
wallet. `modules/htlc_rpc.py` already redacts `walletpassphrase` parameter 0 from
every log line for the same reason.

What it does instead is **tell you before a swap is created**, rather than
letting `sendtoaddress` fail opaquely mid-payout with a customer's deposit
already taken. `swap_readiness.py` reports the lock state as its own check,
separate from the balance, because a funded wallet that cannot send is a
different failure from an empty one.

**The field names are NOT confirmed.** No Gridcoin daemon is reachable from the
environment this was written in, so `unlocked_until` and the staking-only key are
read *when present* and an unrecognized response is reported as **NOT
ESTABLISHED** rather than as unlocked — guessing "fine" there means a swap
created against a wallet that cannot pay it. The check echoes the keys
`getwalletinfo` actually returned, which is how the real names get confirmed: the
same way the Monero and XRP field names were, from a run on the operator's host.

## XRP

Brokered deposits only, **and XRP is not a tradeable pair** — `Config.ALLOWED_PAIRS`
names none, so no XRP quote can be produced and no XRP swap can be created.

`rippled` removed signing from its public API on purpose, so paying XRP means
holding a key in this process — a custody decision, not an implementation gap,
and it is still the operator's.

**Until 2026-09-26 this section said "payouts are refused" and the refusal was an
absence**: no signing library was imported into `chains/xrp.py`, so it could not
have signed if the check had been deleted. That was honest and it was a dead end,
because it left no way to inspect what a payout *would* be before deciding
whether to allow one.

`send_to_address()` now **previews by default** and is armed only at the call
site. Four structural properties, none of them a configuration value:

| property | what it means |
| --- | --- |
| mainnet is refused **by network id** | `server_info.network_id`, not the URL, because a hostname resolves to whatever DNS says today. A **missing** or unreadable id refuses too — not reading the network is not the same as reading a safe one. The preview path runs this check as well, so mainnet cannot even be previewed against. |
| the default is a **preview** | it reads the server, converts the amount, checks the reserve, prints the whole plan, and then refuses. A caller who forgets to arm it cannot send. |
| an exact **arming token**, not a boolean | `confirm_send=CONFIRM_XRP_SEND`. A truthy variable, a parsed config value or a positional argument that drifted could each supply a `True`; none of them can spell a string. |
| **no key anywhere in the adapter** | no seed, no key path, no `Config` field, no environment variable. The seed is a function argument. So there is **no `.env` edit that arms a payout.** |

Plus `submit_and_wait()` rather than `submit()`, with the ledger's final result
and its `validated` flag both asserted before any hash is returned — so a
transaction the ledger rejected cannot be written into `payouts` as broadcast.

**Nothing is wired to the payout worker.** `services/payout_service.py:219` calls
`send_to_address(address, amount)` with two positional arguments, which is
refused before it reaches the network at all (no source account). Wiring it up is
the operator's decision.

**The signing path has never run against a live ledger from a session.** It is
verified against seeded responses and a stubbed `submit_and_wait` in
`tests/test_xrp_adapter.py`. The one real-ledger evidence in this repository is
`xrp_send_tagged.py`'s testnet payment on 2026-09-26 — and the adapter reuses
that file's derivation guard rather than a copy of it.

### The partial payment exploit, and why this code reads one field

An XRP `Payment`'s `Amount` is what the sender *asked* to deliver. With
`tfPartialPayment` set the ledger may deliver **less**, and the transaction
still succeeds, still reports `tesSUCCESS`, and still shows the original larger
`Amount`. What actually arrived is in `meta.delivered_amount` and nowhere else.

Credit `Amount` and you can be drained: claim a million XRP, deliver one drop,
get credited a million. This is the best-known integration mistake on this
ledger and it has taken real money off real exchanges.

`chains/xrp_payments.py` reads `meta.delivered_amount` and **never** falls back
to `Amount` — a missing field refuses rather than degrades, because the
fallback *is* the exploit. Two mutation tests pin it.

It also refuses an **issued currency**: `delivered_amount` is a drop string for
XRP and a JSON object for an IOU. Crediting the object as XRP would pay out real
XRP for a token the depositor minted themselves.

### Status: the wire format is now measured too

| part | state |
|---|---|
| `chains/xrp_address.py` — base58 + checksum | **measured** against ACCOUNT_ZERO and ACCOUNT_ONE, 2,000 round trips, every single-character mutation rejected |
| `chains/xrp_units.py` — drops, finality ladder | **measured**, tested directly |
| `chains/xrp_payments.py` — what counts as a deposit | **rules measured**, and the response shape now **confirmed against rippled 3.4.1** |
| `chains/xrp.py` — RPC method and field names | **confirmed against rippled 3.4.1**, all six fields — see below |

Updated 2026-09-26. The heading above read "the wire format is not" until that
day, when the last field was observed. All six fields in `PAYMENT_FIELDS` have
now been seen on a real ledger:

| field | how it was confirmed |
|---|---|
| `hash`, `TransactionType`, `Destination`, `TransactionResult`, `delivered_amount` | `account_tx` against a funded testnet account, 1 real Payment, 2026-09-26 |
| `DestinationTag` | **9 tagged Payments of 38 in testnet ledger 21060800**, found by `--hunt-tag 200` after walking 13 ledgers / 50 Payments in 2.4µfn (2.9s) |

`DestinationTag` was the last one and it took a different method, because
ordinary faucet traffic is untagged — three separate runs reported it "absent
from all 1". Rather than spend a dependency on sending one, `--hunt-tag N` reads
**other people's** tagged traffic off the testnet: the wire spelling is the same
fact whoever sent the payment. That confirms rippled sends a key named
`DestinationTag` and that `deposit_events_from_transactions()` credits a real
tagged Payment with the tag as `vout`. It does **not** confirm that *our* sender
populates the field — see the sender section below.

Probed 2026-09-25 against `s.altnet.rippletest.net` from the operator's host.
Confirmed: the `params: [{...}]` request shape, that errors arrive in
`result.status` rather than the HTTP code, `reserve_base_xrp` / `reserve_inc_xrp`,
and — the one that matters — `meta.delivered_amount` present as a **string**.

**Two nestings are real, and which one you get depends on the method:**

| method | shape |
|---|---|
| `ledger` (expand=true) | transaction **flat on the entry**, metadata under `metaData` |
| `account_tx` | transaction **nested under `tx`** |

The adapter calls only `account_tx`. This was first reported as a bug that
"would have lost deposits" — it would not have: the original unwrap handled
`tx`, which is what `account_tx` returns. The impact was inferred from `ledger`
alone and stated as measured, before anyone had looked at `account_tx`.

The wider unwrap stays anyway, because `xrp_chain_check.py` *does* call
`ledger`, and because the genuine improvement was the other half: an entry whose
body cannot be located now **raises** instead of returning `{}`, which read as
"not a Payment" and was indistinguishable from a real answer.

### Run the check

```
python3 xrp_chain_check.py                      # testnet, self-bootstrapping
python3 xrp_chain_check.py --account rSomeAcct  # a specific account
python3 xrp_chain_check.py --url https://s1.ripple.com:51234/   # mainnet
```

Read-only: submits nothing, signs nothing, and **never calls
`send_to_address()`**. That last clause used to read "the adapter it builds
refuses `send_to_address()` structurally", which the payout path made wrong on
2026-09-26 — and it was the wrong guarantee to cite anyway: what makes this
script safe is that it does not call the method, which is a property of the
script and has not changed. It names the network from the server's `network_id`,
not the URL, and prints `MAINNET, REAL MONEY` if that is where you pointed it.

**With no `--account` it walks back through validated ledgers, finds a real
Payment and uses its destination** — so a bare run examines an account that
actually has history, rather than reporting an empty one as a pass.

Three exit codes:

| exit | meaning |
|---|---|
| 0 | every **required** field was observed over real Payments, and nothing disagreed |
| 1 | at least one is wrong, each named |
| **3** | **inconclusive** — nothing failed, but there was nothing to look at |

Exit 0 has two different summary lines and the distinction matters. The
unqualified `PASSED: every field the adapter reads was observed` is printed only
when that is true. When a field the adapter reads went unseen, the summary says
so by name — `PASSED, WITH 1 FIELD(S) STILL UNOBSERVED: DestinationTag` — because
until 2026-09-26 it printed the unqualified claim over a run whose own step 3,
two screens above, said `DestinationTag absent from all 1`. The body was honest
and the conclusion was not, and the conclusion is the line a human reads.

`--hunt-tag N` walks N validated ledgers for anyone's tagged Payment, read-only,
and stops at the first hit. Finding none is **not** a failure and is not counted
as one: that is a fact about testnet traffic, not a defect in this code.

It closed the two gaps the first probe left, and both are now closed:
**`account_tx`** specifically, which is the method the adapter actually calls and
whose entries nest differently from `ledger`; and a payment **carrying a
`DestinationTag`**, which `--hunt-tag` found on 2026-09-26 after three runs had
reported it absent.

Step 4 runs the adapter's own scan over the real response, and an empty result
there is treated as a **failure** when the response did contain inbound
payments — because that is the skipped-payment bug returning, not an empty
account.

### Deposits are attributed by destination tag, not by address

One account, one integer tag per swap. No new key, no reserve per swap, and it
is what every exchange on this ledger does — so the attribution question
`chains/solana.py` had to hand back does not arise here.

`get_new_address()` therefore **refuses**, and says why: what is needed is a tag
allocator, which is a change to how a swap is created rather than to the adapter.

### The destination-tag allocator — `services/xrp_tag_service.py`

`allocate_destination_tag(db, account, swap_id) -> int`. One account, one integer
per swap, allocated from 1 upward and **never reused**.

**Uniqueness is enforced by the database, not by Python.** Two live swaps sharing
a tag means one customer's deposit is credited against the other's swap and the
payout is broadcast and final, so the guarantee is four constraints in `db.py`'s
`SCHEMA` rather than a `SELECT` followed by an `INSERT`:

| guarantee | mechanism |
| --- | --- |
| no two rows share a tag on an account | `PRIMARY KEY (account, destination_tag)` |
| no swap gets two tags | `UNIQUE idx_xrp_tag_one_per_swap` |
| `1..4294967295` | `CONSTRAINT xrp_tag_is_allocatable` |
| no row is deleted or re-pointed | two `BEFORE` triggers that `RAISE(ABORT)` |

A check-then-insert was not an option and the reason is measured in this repo,
not argued: two payout workers paid one swap twice on 2026-09-24 through a guard
that read correctly, because the `SELECT` completed before the write lock was
contended (`tests/test_payout_concurrency.py`). Allocation is therefore ONE
statement — `INSERT ... SELECT COALESCE(MAX(destination_tag), 0) + 1 ...
RETURNING destination_tag` — so there is no moment at which a caller holds a
chosen-but-unclaimed number.

**The range is 32 bits, measured rather than recalled.** `xrpl.org` is not
reachable from the container this was written in, so the bound was taken from the
reference implementation's own serializer: `UInt32.from_value(4294967295)`
encodes, `4294967296` raises `OverflowError`, and a real `Payment` round-trips
`DestinationTag` at `0`, `1` and `4294967295`.
`tests/test_xrp_destination_tags.py` re-runs that against the installed
`xrpl-py` and skips if it is absent, naming what goes unchecked. What that does
**not** prove is that a rippled server accepted a transaction at the bound; no
socket is opened by the suite.

**Tags are never reused, and that is the interesting decision.** Nothing on the
XRP Ledger expires a tag. Once a customer has been told "pay account X with tag
7", that instruction lives in their wallet's address book, their exchange's saved
withdrawal template, or an email — and a payment carrying it can arrive months
later, from a retried withdrawal, a returning customer, or a top-up after an
underpayment. If tag 7 has been reallocated by then, that money is credited to a
stranger's swap and the payout is irreversible. So `MAX(destination_tag)` is
taken over **every** row including long-completed swaps, and the `BEFORE DELETE`
trigger makes reclaiming them fail rather than merely discouraged. The cost is
exhaustion, and it is not a real cost: 4,294,967,295 tags is 11,759 years at
1,000 swaps a day and 1,176 years at 10,000.

**Tag 0 is reserved, unallocated, and still readable.** `0` is a legal tag that
decodes back as *present* — which is exactly what makes it dangerous, because it
is the value an uninitialized int, an empty form field and a "0 means none"
convention all produce. Reserving it turns that whole class of mistaken payment
from "credited to an unrelated swap" into "arrived with a tag nothing owns",
which `chains/xrp_payments.py` already reports as `deferred` for an operator.
The reading end stays permissive on purpose: the tag on an incoming payment was
chosen by the sender. `validate_destination_tag(tag, allocatable=...)` carries
both questions as one flag so they cannot drift apart.

**What is NOT wired, and XRP is still untradeable.** `XRP` is not in
`Config.ALLOWED_PAIRS`, so nothing in the running application calls the
allocator. Two things stand between it and a working XRP deposit path, both
named in the module's docstring:

1. `swaps.deposit_address` is one column and an XRP deposit instruction is the
   pair `(account, tag)`. Both halves are mandatory, so wiring this in means
   deciding how the pair is stored and rendered.
2. `services/deposit_service.py::refresh_swap_from_chain()` scans
   `swap["deposit_address"]` and credits **every** returned event to that swap.
   For a per-address chain the address *is* the swap; for XRP the account is
   shared, so it would attribute every tagged payment to whichever swap is being
   refreshed. The missing filter is the event's `vout` against the swap's own
   tag, and `swap_id_for_tag()` is the function that answers it — but the change
   is to the one function that decides, for every chain, that a deposit is
   confirmed. That is fund movement, so it is reported rather than done.

### Producing a tagged payment — `xrp_send_tagged.py`

Dry run by default; `--send` submits. Testnet only: the endpoint is pinned in the
file and `refuse_mainnet()` asks the server for its `network_id` before anything
is signed, so reaching mainnet means editing the source.

```
python3 xrp_send_tagged.py                  # dry run, shows what it would send
python3 xrp_send_tagged.py --send           # submit one tagged Payment
python3 xrp_send_tagged.py --send --tag 7 --amount 5
```

It reads both funded faucet accounts out of `~/.config/swap_terminal/keys/` and
pays the second from the first. **Where the secret lives was measured, not
assumed** — the faucet writes it at top-level `seed`, and the first version of
this script looked for `account.secret`, `payload.secret` and `account.seed`,
three guesses none of which matched. It reported `saved faucet accounts: 0` and
refused to send while two funded accounts sat in that directory, and the whole
suite stayed green because every test fixture wrote the shape the code already
handled. `ADDRESS_KEYS` / `SECRET_KEYS` are now searched as a union across both
nesting levels, and each run prints which key name matched for each file — names
only; the secret is never printed anywhere in this file.

**Two signing paths, and the difference is where the key is used.** First it
tries rippled's server-side `submit` with a `secret`. Public servers disable
that, and `s.altnet.rippletest.net` answered `notSupported` on 2026-09-26 — an
answer about the server, not a failure. It then falls back to signing locally
with **`xrpl-py` (optional dependency, 5.2.0 measured)**, which is imported
lazily: the dry run, the field survey and the server-side path all work without
it, and a module-level import would make a signing library mandatory just to
collect the test suite.

**The derivation guard is why local signing is safe to add.** Server-side
`submit` sends the secret and `Account` separately, so the server derives the key
and rejects a mismatch. Signing here, *we* choose which account the transaction
claims — so a seed paired with the wrong address would sign a Payment from an
account the dry run never displayed: the operator reads one address and a
different one is debited. Since `saved_faucet_accounts()` reads the address and
the secret from separate key names over two nesting levels, nothing structurally
guarantees they came from the same file. `derive_and_check()` therefore derives
the address from the seed and **refuses** unless it equals the announced source.
The refusal names both addresses, which are public, and never the seed.

Submission uses `submit_and_wait()` rather than `submit()`, so a transaction the
ledger rejected cannot be reported as sent.

### Confirmed end to end, 2026-09-26

`--hunt-tag` proves rippled's wire spelling from other people's traffic; it
cannot prove that **our** sender populates the field, because it never reads a
payment we produced. That last step is now done. Locally signed, submitted,
validated:

```
TransactionResult  tesSUCCESS
hash               5534F6CC68DB7AA7237519BC8BB01791172C23FB3E1B57D44E4CD0AD07E100BD
validated          True
```

and read back through the adapter on the destination account:

```
[3] DestinationTag         present in 1/2   <- the NAME is confirmed
[4] credited 1, deferred 1
        10.0 XRP  tag=4242  rank=1
        deferred  A233F36F...  NO DestinationTag
```

So the full loop holds: our sender sets the tag, `account_tx` returns it,
`deposit_events_from_transactions()` credits the payment with the tag as `vout`
and the amount from `delivered_amount`. The `deferred` row is the earlier
untagged faucet payment, correctly held back rather than guessed at — both
outcomes on one response, which is the pair worth seeing together.

That leaves **no unverified field** in the XRP deposit path. What remains on the
deposit side is not verification but wiring: the destination-tag allocator now
exists at `services/xrp_tag_service.py` and is deliberately NOT connected to swap
creation, for the two reasons given in its own section above.

The payout side is no longer "refuses structurally". As of 2026-09-26 it is a
preview-by-default mechanism that cannot reach mainnet and cannot be armed by
configuration — no seed, no key path, no `Config` field and no environment
variable arms it, so there is no `.env` edit that turns it on. What remains there
is not code:

- the **custody decision** — whether this terminal should hold an XRP hot wallet
  at all, and which account. Nothing in the change decides it.
- ~~**one run of the armed path against testnet.**~~ **DONE, 2026-09-26.** This
  was the last open item and it is closed. The operator ran
  `xrp_payout_verify.py --send` from their host, which goes through
  `chains/xrp.py::send_to_address()` rather than around it:

  ```
  XRP payout  derived address matches the announced source: rnjG8n16JinjqkzZj5Jmw6NDMBMzhhNbVv
  XRP payout  submitting and waiting for validation (network_id 1 (NOT mainnet; mainnet is [0]))
  XRP payout  TransactionResult tesSUCCESS  validated=True  waited 5.6µfn (6.8s)
  XRP payout  DELIVERED and validated  hash=E118CA9636765E5F5954A49800E207BEB8AC50BCA603BBD8F5CECB2477EFD8E8
  ```

  So the armed half is no longer a PROPOSAL: the adapter's own guards ran, the
  derivation check passed against a real wallet, the transaction was signed
  locally, submitted, and validated by the ledger. Every session before this one
  could only say the path was tested against seeded responses and a stubbed
  submit, because `s.altnet.rippletest.net:51234` is unreachable through the
  agent proxy — measured as a connection reset, not assumed. No session could
  close this; only the operator could.

  What that does **not** make true: this is testnet, and the custody question
  above is still open and is still the thing standing between a working mechanism
  and a payout that pays a customer. `Config.ALLOWED_PAIRS` names no XRP pair.

  The unarmed, preview and refusal paths are a fix, and as of 2026-09-26 they are
  measured rather than merely tested: the operator ran a real preview against
  rippled 3.4.1 from their host. It read `server_info` and `account_info`, named
  the network from `network_id 1`, computed the reserve, and refused for want of
  the arming token — signing nothing. Two figures the code had labeled
  unconfirmed came back present in that run, `validated_ledger.base_fee_xrp` (10
  drops) and `account_data.OwnerCount` (0), so both docstrings that said they had
  never been read off a live server have been corrected. Their fallbacks stay:
  one server answering once is evidence about that server, not a guarantee about
  the field.

  That run also found a defect. The plan was emitted TWICE on a refusal — printed
  before the arming check and appended to the refusal message — so a caller that
  surfaced the exception saw the same eight lines twice, on the one path whose
  whole purpose is that an operator reads the plan before arming it. The copy
  inside the exception is the one kept, because it survives being caught and
  logged; the live print moved below the arming check, where rule 14's "announce
  before" most wants it anyway: immediately before the one irreversible step
  rather than before a guard that usually stops. Pinned by a PAIR of tests, since
  removing the duplicate by deleting the print would have passed the first and
  lost the announcement an armed send needs.

`services/payout_service.py:219` is the call site, and it is NOT connected. It
calls `send_to_address(address, amount)` with two positional arguments, which is
refused two guards EARLIER than the arming check, because it supplies no source
account — a stronger position than was designed for, and recorded rather than
smoothed over.

### Tag 0 is a real tag

`0` is a legal `DestinationTag` and `if tag:` reads it as absent.
`chains/xrp_payments.py` has always been correct here — it tests `tag is None`,
and separately rejects `bool`, because `True == 1` would otherwise become tag 1.
Neither was pinned by a test until 2026-09-26, so an edit to `if not tag:` would
have passed all 689 tests while making every deposit tagged 0 arrive, be held
back as unattributable, and wait for a hand match. Money arrives, the swap does
not credit, nothing fails. Both are pinned now, mutation-checked by making that
exact edit.

Whether the allocator ever *issues* 0 is a separate question, and it is no longer
open: `services/xrp_tag_service.py` reserves 0 and starts at 1, because 0 is what
every "no tag to send" integration emits. That does not license tightening the
reader — the tag on an incoming payment was chosen by the sender, and a reader
that drops a legal value is wrong either way.

### Two rippled quirks that bite

**`params` is a list containing one object** — `{"method": "...", "params":
[{...}]}`. Not an object like Monero's JSON-RPC, not a positional list like
bitcoind's.

**Errors arrive as HTTP 200**, with `result.status == "error"`. A client that
only checks `raise_for_status()` reads every failure as a success. `call()`
checks explicitly.

### Finality is binary

The XRP Ledger does not reorganize, so a payment is either in a validated
ledger or it is not — there is no depth to accumulate. `XRP_MIN_CONFIRMATIONS`
must be `1`, and any other value is **refused at construction**: a `6` copied
from a Bitcoin-shaped config would leave every XRP deposit below an unreachable
threshold forever, with nothing in any log saying why.

The base reserve is asked of the server, never hardcoded — it has been 20 XRP,
then 10, then 1, and a stale constant would overstate spendable balance.

### Configuration

```
XRP_RPC_URL=              # unset means no XRP adapter is constructed at all
XRP_MIN_CONFIRMATIONS=1
```

`ALLOWED_PAIRS` is unchanged, matching how Solana and Monero landed. No XRP
swap can be created; enabling it is the operator's (rule 16).

## Monero

Brokered only. There is no atomic-swap path for Monero and there is not going
to be one -- see **Why there is no XMR HTLC** at the end of this section, which
is written down so it is a decided question rather than one that gets reopened.

### Status: the arithmetic is measured, the wire format is not

This is the single most important thing to know before turning it on.

| part | state |
|---|---|
| `chains/monero_units.py` -- atomic units, the confirmation floor | **measured**, tested directly |
| `chains/monero_transfers.py` -- which transfers count as a deposit | **measured**, tested directly |
| `chains/monero.py` -- RPC method and field names | **UNVERIFIED** |

The third row moved on 2026-09-25 and the table above keeps the old wording
because the drift is the point. Those names were originally written from prior
knowledge, because the environment they were written in could not reach
`getmonero.org` (403 through the proxy) or run a daemon.

**They have since been checked against the published spec**, fetched from the
operator's host (`docs.getmonero.org/rpc-library/wallet-rpc/`, 380,318 bytes).
All six method names, the `in` array returned by `get_transfers`, the
`{major, minor}` shape of `subaddr_index`, `txid`, `type: "in"`, `locked`,
`unlock_time`, `double_spend_seen` and `unlocked_balance` are all in the
request/response spec as this code reads them, and `validate_address`'s
`any_net_type` does default to false, which the adapter relies on.

So it is no longer a guess. Three things are still open, and they are why
`monero_chain_check.py` should still be the first thing you run:

1. **Documentation is not a daemon.** A spec can lag a release, and nothing
   above came from a wallet answering a real call.
2. **The multi-output amount.** Every `amounts` example in the spec has one
   element, so nothing published proves `amount` is the *sum* when several
   outputs arrive. `_reject_amount_disagreement()` checks it at runtime and
   refuses rather than short-paying a customer silently.
3. **`locked` is described contradictorily** -- `locked - boolean; Is the
   output spendable`, which is the opposite of what the field name says. This
   code follows the name, so the worst case is a deposit deferred that was
   fine: a delay, printed, never a wrong credit.

### Verify it first

```
monero-wallet-rpc --stagenet --rpc-bind-port 38083 \
    --wallet-file <wallet> --prompt-for-password --disable-rpc-login

python3 monero_chain_check.py --port 38083
```

Read-only: it sends nothing, signs nothing, constructs its adapter with
`can_spend=False` regardless of configuration, and has no flag that broadcasts.
It identifies the network from the daemon rather than from the port, and prints
`MAINNET  <- REAL MONEY` if that is where you pointed it.

Three exit codes, because there are three outcomes:

| exit | meaning |
|---|---|
| 0 | every method and field was observed against real transfers |
| 1 | at least one name is wrong; each is listed by name |
| **3** | **inconclusive** -- no failures, but the wallet had no transfers, so nothing was confirmed |

Exit 3 is deliberate. A wallet with no incoming transfers confirms none of the
field names, and exiting 0 would tell a shell `&&` exactly what a real pass
tells it. **Send a small amount to the wallet on stagenet and run it again** --
that is the only thing that confirms the field names.

### Configuration

```
XMR_RPC_HOST=127.0.0.1
XMR_RPC_PORT=38083          # no default: monero-wallet-rpc binds wherever you told it
XMR_RPC_USER=               # empty for --disable-rpc-login; digest auth otherwise
XMR_RPC_PASS=
XMR_ACCOUNT_INDEX=0
XMR_MIN_CONFIRMATIONS=10
XMR_WALLET_CAN_SPEND=       # see below -- leave unset for a deposit watcher
```

`XMR_RPC_PORT` unset means no Monero adapter is constructed at all, exactly as
`SOL_RPC_URL` works for Solana.

### Run the watcher view-only

Monero splits the view key from the spend key, so a wallet opened from the view
key alone sees every incoming transfer and is *cryptographically incapable* of
sending. Bitcoin, Litecoin and Gridcoin have no equivalent -- in
`chains/base.py` every subclass inherits `send_to_address` unconditionally.

`XMR_WALLET_CAN_SPEND` is therefore **false by default**, and
`send_to_address()` refuses before making any network call. The worker banner
says which you are on:

```
  XMR  rpc=127.0.0.1:38083 account=0 min_confirmations=10 blocks view-only (cannot send)
  XMR  rpc=127.0.0.1:38083 account=0 min_confirmations=10 blocks CAN SPEND  <- this wallet is armed for payouts
```

### Things Monero does differently, and where each one lives

- **12 decimals, not 8.** `chains/base.py` hardcodes `SATOSHI = 1e-8`. Amounts
  convert through `Decimal`, never by multiplying the float: measured, `2.11`
  XMR loses a piconero to `int(float * 10**12)`.
- **Ten-block spend lock, by consensus.** `XMR_MIN_CONFIRMATIONS` below 10 is
  clamped up, because a lower setting does not buy a faster payout -- it buys a
  payout that fails at `transfer` time. The banner says when it raised your
  number.
- **`get_balance()` reports the UNLOCKED balance**, not the total. The total
  includes outputs inside that lock, which cannot fund a payout.
- **No vouts.** `vout` carries the receiving subaddress's minor index -- read
  from the wallet, never invented. Where the wallet's answer does not determine
  which output a deposit is, the scan **raises** and the swap waits for a human
  rather than being credited from a guess. That is deliberately unlike
  `chains/base.py:169`, which fabricates an event at vout 0 and whose own
  comment admits the caller cannot tell it from a real one.
- **Subaddresses are a real per-swap deposit address**, so the attribution
  problem that `chains/solana.py` had to hand back to the operator does not
  arise here at all.

### Not enabled for trading

`ALLOWED_PAIRS` is **unchanged**, so no XMR quote or swap can be created. This
matches how Solana landed. Enabling it is one line in `config.py` and it is
deliberately the operator's, because it changes what gets traded (rule 16).

### Known gap: proving a payout

On the Bitcoin-derived chains a payout proves itself -- anyone can look up the
txid and see the output. Monero publishes no such link, and the only proof is
the transaction key, checked by the recipient against their own address.
Storing it needs a schema change on `payouts` **and** creates a new class of
secret: a tx key reveals the amount and destination to whoever holds it. Left
undone and written up at the bottom of `chains/monero.py` rather than
half-built.

### Why there is no XMR HTLC

Monero has no scripting language: no `OP_CHECKLOCKTIMEVERIFY`, no P2SH, no
hashlock. Everything under `modules/atomic_*` is inapplicable, not unported.

XMR<->BTC atomic swaps do exist, and they work by replacing the Monero-side
HTLC with a 2-of-2 shared spend key where the Bitcoin-side spend reveals the
scalar that completes it. That requires a discrete-log-equality proof that the
same secret is committed on **two different curves** (ed25519 and secp256k1).
No implementation targets Gridcoin. Writing cross-curve proofs for a path where
a revealed preimage cannot be un-revealed is not a trade worth making here.

## Regtest HTLC verification

`regtest_htlc_verify.py` at the repository root drives the real HTLC modules
against local `bitcoind` and `litecoind` regtest daemons and reports which
branch of a funded contract actually spends. It is not a pytest test -- it
needs two external daemons, starts processes, and mines four-figure numbers of
blocks -- so it lives at the root and is run by hand:

    source .venv/bin/activate
    python3 regtest_htlc_verify.py --wipe

It REFUSES to run against any chain whose `getblockchaininfo` reports anything
but `regtest`, unconditionally and with no flag to override. See the module
docstring for the flags and the `ST_REGTEST_*` environment variables.

The parts of it that can be proven without a daemon -- the scriptSig layout,
the sighash, the key and address encodings, the verdict wording -- are covered
by `tests/test_regtest_harness_units.py` and run with the normal suite.

## The deposit vout artifact

`migrate_deposit_vouts.py` at the repository root reports -- and, when you name
rows, deletes -- the fabricated `vout=0` rows in `deposit_events`.

Before 2026-09-25, `chains/base.py::_extract_matching_vouts()` matched a
transaction's outputs on `scriptPubKey.addresses`, a field removed in Bitcoin
Core 22.0. On a Core 22+ node nothing ever matched, so every deposit was
credited by the fabricated fallback beside it: `vout=0`, with the amount from
the wallet's `listtransactions` summary. The search now finds the real output,
and `deposit_events` has `UNIQUE(asset, txid, vout)` -- so a deposit whose real
output is at `vout=N` INSERTS a second row rather than updating the old one,
and `refresh_swap_from_chain()` sums both. The swap's total doubles and it
halts in `under_review`. That is a halt rather than a wrong payout, but the
swap stops moving until somebody clears it.

Dry run by default. It opens the database `mode=ro`, so SQLite -- not the
script's control flow -- enforces that:

    source .venv/bin/activate
    python3 migrate_deposit_vouts.py --db /path/to/swap_terminal.db

Stop the workers first if you intend to apply anything
(`python3 swap_terminal/supervisor.py stop`): a `deposit_watcher` poll will
re-insert a row the migration has just deleted.

It does not decide which rows are fabricated, and it cannot. One transaction
may legitimately pay the deposit address twice, which produces exactly the same
shape, so the dry run prints every row with its `first_seen_at` and both totals
-- with and without the `vout=0` row -- and you decide. Deleting takes both
`--apply` and an explicit `--delete-event-id` per row; a backup is taken with
`sqlite3.Connection.backup()` first, and its path is printed.
