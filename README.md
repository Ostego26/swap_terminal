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

## XRP

Brokered deposits only. **Payouts are refused** and the refusal is structural:
`chains/xrp.py` imports no signing library and reads no key path, so it could
not sign if the check were deleted. `rippled` removed signing from its public
API on purpose, so paying XRP means holding a key in this process — a custody
decision, not an implementation gap.

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

### Status: the address maths is measured, the wire format is not

| part | state |
|---|---|
| `chains/xrp_address.py` — base58 + checksum | **measured** against ACCOUNT_ZERO and ACCOUNT_ONE, 2,000 round trips, every single-character mutation rejected |
| `chains/xrp_units.py` — drops, finality ladder | **measured**, tested directly |
| `chains/xrp_payments.py` — what counts as a deposit | **rules measured**, response shape unverified |
| `chains/xrp.py` — RPC method and field names | **partly confirmed against rippled 3.4.1** — see below |

Probed 2026-09-25 against `s.altnet.rippletest.net` from the operator's host.
Confirmed: the `params: [{...}]` request shape, that errors arrive in
`result.status` rather than the HTTP code, `reserve_base_xrp` / `reserve_inc_xrp`,
and — the one that matters — `meta.delivered_amount` present as a **string**.

**The probe found a bug.** On that server's `ledger` response the transaction
body is **flat on the entry** and the metadata key is **`metaData`**, not nested
under `tx`/`tx_json`. The unwrap looked only under those two, so it returned an
empty dict and *skipped the payment silently* — reporting "no deposits" for
money that had arrived. Fixed, and an unrecognizable entry now raises rather
than reading as "not a Payment".

### Run the check

```
python3 xrp_chain_check.py                      # testnet, self-bootstrapping
python3 xrp_chain_check.py --account rSomeAcct  # a specific account
python3 xrp_chain_check.py --url https://s1.ripple.com:51234/   # mainnet
```

Read-only: submits nothing, signs nothing, and the adapter it builds refuses
`send_to_address()` structurally. It names the network from the server's
`network_id`, not the URL, and prints `MAINNET, REAL MONEY` if that is where you
pointed it.

**With no `--account` it walks back through validated ledgers, finds a real
Payment and uses its destination** — so a bare run examines an account that
actually has history, rather than reporting an empty one as a pass.

Three exit codes:

| exit | meaning |
|---|---|
| 0 | every field the adapter reads was observed over real Payments |
| 1 | at least one is wrong, each named |
| **3** | **inconclusive** — nothing failed, but there was nothing to look at |

It closes the two gaps the first probe left: **`account_tx`** specifically,
which is the method the adapter actually calls and whose entries may nest
differently again, and a payment **carrying a `DestinationTag`** — none of the
three sampled had one, which is normal wallet-to-wallet traffic and says
nothing either way about deposits addressed to us.

Step 4 runs the adapter's own scan over the real response, and an empty result
there is treated as a **failure** when the response did contain inbound
payments — because that is the skipped-payment bug returning, not an empty
account.

### Deposits are attributed by destination tag, not by address

One account, one integer tag per swap. No new key, no reserve per swap, and it
is what every exchange on this ledger does — so the attribution question
`chains/solana.py` had to hand back does not arise here.

`get_new_address()` therefore **refuses**, and says why: what is needed is a tag
allocator in `services/swap_service.py`, which is a change to how a swap is
created rather than to the adapter.

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
