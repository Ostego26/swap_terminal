# Separating the terminal's hot wallets from the operator's own

Written 2026-10-03, after the operator's instruction: *"these two hot wallets are
used by the swap terminal machine NOT THE USER."*

Everything in this file is the operator's to run. **Nothing here was run by the
agent that wrote it**, and the agent created no wallet, funded nothing, edited no
`.env` and sent no transaction — the reason each step is written out rather than
done is that every one of them either moves funds or changes what the live
terminal can spend, which under CLAUDE.md rule 16 is yours. Where a fact could
not be established from this container, the step says **NOT ESTABLISHED** and
gives the read-only command that settles it, rather than a guess in the register
of an instruction (rule 17).

The diagnostic that checks the result is:

    python3 /home/user/swap_terminal/wallet_custody.py
    python3 /home/user/swap_terminal/wallet_custody.py --swap <swap id>

It is read-only: two RPC reads per Bitcoin-derived chain (`getwalletinfo`,
`listwallets`), one more with `--swap` (`validateaddress`/`getaddressinfo`), and
no network call at all for XRP or SOL. It never calls `getnewaddress` — that is a
wallet **write** — never `sendtoaddress`, and never opens a key file. It refuses a
mainnet or unrecognized port before any socket opens.

---

## What is wrong, and why it does not look wrong

This is a **custodial** desk. The customer deposits to an address the desk owns
and the desk pays out of its own inventory
(`swap_terminal/modules/htlc_assets.py` says so: "comes out of the desk's own
inventory, with no hashlock and no atomicity"). So a GRC→XRP swap has to move
coins **out** of the customer's wallet and **into** the terminal's, and move XRP
**out** of the terminal's account and **into** the customer's.

Measured on the live host 2026-10-03 — these two are the operator's own
measurements and could not be re-run from the agent's container, which has no
network path to those daemons:

1. Swap `s_539d922e9ef0a5d8` completed. Its 500 GRC deposit went to
   `moaSBv8gcwXRnmQhxJJAjUvXMd542jsNNz`, derived by `getnewaddress` on the
   operator's **own** `gridcoinresearchd`. The wallet balance moved
   `3780.08654497 → 3780.08554497`: the 0.001 fee, and nothing else. The
   transaction carries `category: send` **and** `category: receive` for the same
   address. A self-transfer. No custody moved.
2. The XRP payout went `rnjG8n16JinjqkzZj5Jmw6NDMBMzhhNbVv` →
   `rBfM7je6e9Ca2cMvuRn7cr9xExFgDa5NGx`, `Amount` 3315589 drops, `Fee` 10,
   `tesSUCCESS`, validated. Both are the operator's own testnet faucet accounts.

Measured **in this tree**, which is the mechanism behind (1):

    BTC_RPC_WALLET -> ''     swap_terminal/config.py:447
    LTC_RPC_WALLET -> ''     swap_terminal/config.py:455
    GRC_RPC_WALLET -> ''     swap_terminal/config.py:520

`chains/base.RPCAdapter.url` appends `/wallet/<name>` **only** when that value is
non-empty. Verified by construction the same day:

    RPCAdapter(wallet="")         -> http://127.0.0.1:25715
    RPCAdapter(wallet="desk_hot") -> http://127.0.0.1:25715/wallet/desk_hot

An empty value therefore addresses the daemon with **no wallet path**, and the
daemon routes the call to whichever wallet it serves by default — the same wallet
`gridcoinresearchd getnewaddress` with no `-rpcwallet` reaches. The desk's deposit
addresses and the operator's own coins are in one wallet, which is exactly what
makes a deposit into one a self-transfer.

**The reason this does not look wrong on any screen** is that nothing distinguishes
a self-transfer from a real deposit: the swap completed, the payout validated, and
`wallet_inventory.hot_confirmed` recorded the whole wallet as desk stock. That
last one was measured behaviorally rather than read off the source — running
`services/payout_service.refresh_wallet_inventory()` against a stub adapter made
exactly one RPC call, `getbalance`, and stored `hot_confirmed` equal to what it
returned. The display surfaces now say so; **how the number is computed and what
reads it was not changed**, because that is live posture.

---

## Before you change anything: four things that break if you skip them

These are in this order because each one costs something different if it is missed.

### 1. An open swap's deposit address stays in the OLD wallet

A deposit address is derived once, at swap creation, by `getnewaddress` in
whatever wallet the endpoint served **at that moment**
(`services/swap_service.deposit_account()` → `derive_deposit_address()`). Switch
`GRC_RPC_WALLET` while a swap is open and the new wallet does not hold the key for
that swap's deposit address: the deposit arrives, the new wallet cannot see it,
and the swap cannot be paid from the money it was paid.

So: drain first. Let every open swap reach a terminal status, or resolve it, and
then check each one that mattered:

    python3 /home/user/swap_terminal/wallet_custody.py --swap <swap id>

`DESK OWNS IT` for the `<asset> deposit addr` line means the payout wallet holds
the key for that swap's deposit address. `NOT THE DESK'S` means it does not, and
on a custodial desk that is the defect this step exists to avoid.

### 2. A new wallet is EMPTY, and an empty payout wallet fails after the deposit

Measured 2026-10-03 on the operator's host, before any of this work: the first
BTC→GRC swap took its deposit (0.001 BTC, 2 of 2 confirmations, credited and
irreversible) and then failed on `Insufficient funds (rpc code -4)` because the
payout needed 9049.68583412 GRC against 3780.08854497 spendable. A freshly created
wallet holds nothing at all, so the same failure is one swap away.

Fund the new wallet **before** pointing the terminal at it, and check the ceiling:

    python3 /home/user/swap_terminal/swap_readiness.py --pair GRC:XRP

The `GRC wallet` line prints the largest single payout that wallet can fund, and
says which wallet the figure came out of.

### 3. Setting `*_RPC_WALLET` to a wallet the daemon cannot serve breaks the chain

The variable becomes a URL path. If the daemon has no such wallet loaded, or has
no multiwallet endpoint at all, **every** RPC call on that chain fails — including
the deposit watcher's. Check with the diagnostic before starting the workers; a
`NOT ESTABLISHED` line naming a `getwalletinfo` failure is what that state looks
like.

### 4. Stop the workers for the switch, and start them after the check

`swap_terminal/supervisor.py` owns the worker lifecycle. A worker holding the old
endpoint keeps using it until it is restarted, so a half-switched host has one
process on each wallet. Stopping and starting workers is yours; this file does not
run it.

---

## BTC and LTC — a second named wallet on the same daemon

Bitcoin Core and Litecoin Core both carry `createwallet`, `loadwallet` and the
`/wallet/<name>` endpoint, and the tree already relies on them:
`chains/wallet_hint.which_wallets_are_on_disk()` names `loadwallet` and
`createwallet` as the remedy for rpc code -18, and
`regtest/daemons.CHAIN_DEFAULTS` gives the binaries and datadirs
(`bitcoind`/`bitcoin-cli`, `~/regtest/btc`, port 18443;
`litecoind`/`litecoin-cli`, `~/regtest/ltc`, port 19443).

**Read-only first** — what is loaded now:

    bitcoin-cli  -datadir=$HOME/regtest/btc -regtest listwallets
    bitcoin-cli  -datadir=$HOME/regtest/btc -regtest getwalletinfo | grep walletname
    litecoin-cli -datadir=$HOME/regtest/ltc -regtest listwallets

**Create the desk's wallet.** This writes — it creates a wallet and its keys:

    bitcoin-cli  -datadir=$HOME/regtest/btc -regtest createwallet desk_hot
    litecoin-cli -datadir=$HOME/regtest/ltc -regtest createwallet desk_hot

`createwallet` also loads it for the current daemon run. It is **not** persistent
across restarts on every build, so add it to the daemon's conf as well, which is
the `-wallet=` form:

    # ~/regtest/btc/bitcoin.conf
    [regtest]
    wallet=desk_hot          # the desk's
    wallet=                  # the default wallet, so your own CLI keeps working

Naming **both** is what gets you the strongest separation an RPC read can show:
with two wallets loaded, Core refuses a bare wallet RPC with rpc code -19
("Wallet file not specified"), so an operator's own `getnewaddress` with no
`-rpcwallet` cannot silently land in the desk's wallet. With only the desk wallet
loaded it still can, and `wallet_custody.py` says so in those words.

**Fund it once.** On regtest, mine to an address of the desk wallet:

    ADDR=$(bitcoin-cli -datadir=$HOME/regtest/btc -regtest -rpcwallet=desk_hot getnewaddress)
    bitcoin-cli -datadir=$HOME/regtest/btc -regtest generatetoaddress 101 "$ADDR"

101 blocks, not 1: coinbase outputs need 100 confirmations before they are
spendable, and `getbalance` reports only what is spendable —
`chain_balances.py` prints the immature column beside it for exactly this reason.
On testnet rather than regtest, send from your own wallet or a faucet instead.

**Then set the variable, in the shell that starts the terminal.** Yours to edit:

    export BTC_RPC_WALLET=desk_hot
    export LTC_RPC_WALLET=desk_hot

Nothing in the serving path reads a `.env` — `config.py` sees only the process
environment — so a value set in a file, or in another shell, does not reach the
workers.

**Re-check:**

    python3 /home/user/swap_terminal/wallet_custody.py

The `BTC wallet` and `LTC wallet` lines should read `SEPARATED`, and the sentence
should name the other loaded wallet. If it reads `MISCONFIGURED`, the daemon is
serving a different wallet from the one the variable asked for, and neither name
is the answer until they agree.

---

## GRC — NOT ESTABLISHED from here, and the probe that settles it

**Do not copy the BTC steps onto Gridcoin on the strength of their looking alike.**
Gridcoin sits on the **pre-0.17 Bitcoin RPC surface**, which this tree has
measured repeatedly rather than assumed:

    getaddressinfo   absent, rpc code -32601        chains/base.py:436
    gettxout         absent, "unknown command"      modules/rpc_method_support.py
    getbalances      absent on Gridcoin             chain_balances.py:205
    signrawtransactionwithkey  absent               modules/htlc_rpc.py:269
    getblockchaininfo carries no `chain`;
      the testnet flag is getinfo.testnet          services/admin_view.py:1383

Whether `createwallet`, `listwallets` and the `/wallet/<name>` endpoint exist on
the build you are running **was not established** — there is no Gridcoin daemon
reachable from the container this was written in, and the question cannot be
answered by reading this repository. Settle it with two read-only commands:

    gridcoinresearchd -testnet help listwallets
    gridcoinresearchd -testnet help createwallet
    gridcoinresearchd -testnet getwalletinfo

- If `listwallets` and `createwallet` are real commands **and** `getwalletinfo`
  carries a `walletname` field, follow the BTC/LTC section with
  `GRC_RPC_WALLET=desk_hot`, then re-run the diagnostic. A `NOT ESTABLISHED` line
  naming a `getwalletinfo` failure after you set the variable means the endpoint
  path is not supported and the variable has to go back to empty.
- If they are not, the separation is a **second daemon** rather than a second
  wallet, and that route is certain to work because it is already how this tree
  separates Gridcoin testnet from Gridcoin mainnet: its own `-datadir`, its own
  `wallet.dat`, its own `-rpcport`.

      # the desk's Gridcoin testnet daemon, separate datadir and port
      gridcoinresearchd -datadir=$HOME/grc-desk -testnet -rpcport=25716 \
                        -rpcuser=<user> -rpcpassword=<pass> -daemon

      export GRC_RPC_PORT=25716
      export GRC_RPC_USER=<user>
      export GRC_RPC_PASS=<pass>
      # GRC_RPC_WALLET stays EMPTY: this daemon has exactly one wallet and it is
      # the desk's, so the default wallet IS the desk's wallet.

  Two things to know before taking that route. First, **25716 is not a port this
  tree recognizes.** `network_target.CHAIN_PORTS["GRC"].test_ports` is
  `{25715, 25779, 9876}`, so `wallet_custody.py`, `swap_readiness.py` and
  `chain_balances.py` will all refuse to read it — `UNRECOGNIZED`, on the grounds
  that an unknown port may be a mainnet daemon on a custom `-rpcport`. Use 25779
  or 9876 if either is free, or adding the new port to that table is a one-line
  change somebody should make deliberately rather than as a side effect.
  Second, with `GRC_RPC_WALLET` empty the diagnostic will report `NOT SEPARATED`
  even when the separation is real, because what it can establish is that the
  endpoint has no wallet path — it cannot see that the daemon behind it is a
  different daemon. That is the tool's honest limit, and it is the reason this
  route needs a note in your own records rather than a green line on a screen.

Gridcoin has one more hazard the other two do not: **the wallet passphrase.**
`services/payout_service.WALLET_UNLOCK_ENV_VAR` is `GRIDCOIN_WALLET_PASSPHRASE`,
read from the environment at use time, and three rehearsals on 2026-10-01 died on
it being absent from the shell the supervisor was started from — after the
deposit was credited. A new desk wallet you encrypt needs its passphrase in that
variable, in that shell.

---

## XRP — one desk account is CORRECT; what has to be separate is the customer

There is one variable, `XRP_DEPOSIT_ACCOUNT`, and that is the design rather than
an omission. `services/swap_service.payout_source_account()` reads the **same**
variable through the same table as `deposit_account()`: an XRP customer's deposit
lands in that account and an XRP payout is debited from it, exactly as one
Gridcoin wallet takes deposits in and pays out of one balance. Deposits are told
apart by an integer `DestinationTag`, not by an address. **Do not try to split it
into two variables** — a second variable for the send side is the failure that
function's docstring names: the deposit side and the payout side quietly pointing
at two different accounts, each file looking correct on its own.

So the two things to get right are different from the Bitcoin-derived chains.

**One: `XRP_DEPOSIT_ACCOUNT` must be the account whose seed you hold.** A seed
paired with an account it does not control signs a `Payment` debiting an account
nobody announced, and `chains/xrp_signing.derive_and_check()` refuses that — but
it refuses at **payout** time, after the customer's deposit is confirmed and
irreversible. Establish it before, with either of:

    python3 /home/user/swap_terminal/xrp_payout_account.py
    python3 /home/user/swap_terminal/wallet_custody.py

Both read `XRP_PAYOUT_SECRET_SEED` from the environment and **neither prints it**:
the derivation lives in `chains/xrp_payout_seed.derived_payout_account()`, which
returns a public classic address or a refusal naming only the exception type.
`AGREES` / `BY DESIGN` is the answer you want. `MISCONFIGURED` means the variable
and the seed name two accounts — fix the variable, not the check.

**Two: the account you pay OUT to has to be somebody else's.** This is the half
the 2026-10-03 payout did not satisfy, and no code can establish it. The XRP
Ledger has no owner field; `chains/xrp.XRPAdapter.owns_address()` returns `None`
by design for that reason, and a desk cannot tell its operator's second faucet
account from a stranger's. `wallet_custody.py --swap <id>` will tell you whether
a payout **left** the desk's account and says in the same sentence that "it left
the desk" is the whole of the claim.

What that means practically: a rehearsal where both accounts are yours is a valid
test of the *mechanism* and proves nothing about *custody*. If you want the
custody half demonstrated, the destination has to be an account you do not hold
the seed for — a second person's testnet account, or a faucet account whose seed
you discard before the run and never re-import.

---

## SOL — two variables, and they must be two accounts

Solana is the one chain where the deposit account and the hot wallet are separate
settings by design, and `config.py` carries the reason: "an XRP account is funded
past a base reserve and holds nothing else; a Solana deposit account is an
ordinary keypair's public key, and whoever holds that key holds every deposit
between arrival and payout."

    SOL_DEPOSIT_ACCOUNT   where customer deposits land, attributed by a Memo
                          instruction (services/swap_service.TAG_ATTRIBUTION)
    SOL_HOT_WALLET        the PUBLIC key a payout is debited from
    SOL_PAYOUT_KEYPAIR_PATH
                          the keypair file for SOL_HOT_WALLET. Deliberately NOT a
                          Config field: config.py reads the environment at class
                          definition time, so a key path there would be baked into
                          every process that imports config, including the
                          read-only deposit watcher.

Create two keypairs with the Solana CLI, on **devnet**:

    solana-keygen new --outfile ~/.config/solana/desk-deposit.json
    solana-keygen new --outfile ~/.config/solana/desk-hot.json
    solana address -k ~/.config/solana/desk-deposit.json
    solana address -k ~/.config/solana/desk-hot.json

Fund them once on devnet (`solana airdrop 2 <address> --url devnet`), then export
the two public keys and the hot wallet's keypair path — yours to edit:

    export SOL_DEPOSIT_ACCOUNT=<desk-deposit public key>
    export SOL_HOT_WALLET=<desk-hot public key>
    export SOL_PAYOUT_KEYPAIR_PATH=$HOME/.config/solana/desk-hot.json

`SOL_PAYOUT_KEYPAIR_PATH` must be the keypair **for** `SOL_HOT_WALLET`. Nothing in
this tree pairs them for you at configuration time, and
`chains/solana_payout_keypair.py` deliberately never opens the file — it answers
only "is a path exported". The file itself is read in exactly one place,
`chains/solana_signing.load_payout_keypair()`, after an exact-string arming token
has matched and after the cluster has been proven to be devnet by genesis hash.

**Re-check:**

    python3 /home/user/swap_terminal/wallet_custody.py
    python3 /home/user/swap_terminal/sol_payout_preview.py

The `SOL accounts` line should read `SEPARATED`. `NOT SEPARATED` means the two
variables hold one account, which on this chain is a choice rather than the
design: every deposit would land in the account that pays every payout, and
whoever holds that one key would hold both.

---

## After all five: what a clean run looks like, and what it still does not prove

    python3 /home/user/swap_terminal/wallet_custody.py ; echo "exit=$?"

Exit 0 only when every line answered `SEPARATED`, `BY DESIGN` or `DESK OWNS IT`.
`NOT ESTABLISHED` is deliberately **not** a pass: a daemon that did not answer
exits non-zero with the reason printed, because a green verdict by default is the
defect the tool exists to remove.

A clean run establishes that the desk signs with keys in a wallet or account that
is not the one your own CLI reaches by default. It does **not** establish:

- that the coins in those wallets were never yours. No chain and no RPC says whose
  money a coin is; there is no `isdesks` beside `ismine`. A named wallet separates
  the **keys**, and nothing can audit how it was funded.
- that an address the desk does not control belongs to a customer. On all five
  chains the desk cannot tell a customer's account from a second account you hold.

Both limits are printed at the top of every run, before any verdict, because a
limit in a footnote has already been misread by the time it is reached.
