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
`listwallets`), one more with `--swap` (`validateaddress`/`getaddressinfo`), one
against a **second** daemon on GRC with `--swap` (`validateaddress`/
`getaddressinfo`, named by `GRC_OPERATOR_RPC_*` — see the GRC section), and no
network call at all for XRP or SOL. It never calls `getnewaddress` — that is a
wallet **write** — never `sendtoaddress`, never opens a key file, and never reads
an rpcpassword out of a conf file or a `.env`. It refuses a mainnet or
unrecognized port before any socket opens, on both endpoints.

**The GRC section is no longer NOT ESTABLISHED.** It was, when this file was
written, about whether Gridcoin carries the multiwallet RPCs. The operator ran the
read-only command it asked for and it does not, so that section is now the real
route — a second daemon — rather than a probe.

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

## GRC — a second daemon, because a second wallet is not available

### Copy block data only. NEVER `wallet.dat`.

This is first because it is the one mistake nothing in this tree can detect.
Copying `wallet.dat` into the desk's new datadir copies the operator's **keys**,
so both daemons hold the same keys, and that is the exact opposite of separating
them. The desk's deposit addresses would still be addresses the operator's wallet
can spend; a customer deposit into one would still be a self-transfer; and the
balance would look right, which is what makes it the dangerous mistake rather than
merely a wrong one.

`wallet_custody.py`'s new cross-daemon check is the one thing that WOULD see it —
it would report

    NOT SEPARATED    GRC operator daemon: the OPERATOR's own daemon at
                     127.0.0.1:25715 reports ismine=true for the desk's deposit
                     address … BOTH daemons hold the key: that is one wallet
                     reached through two endpoints, or a wallet.dat that was
                     copied.

— and it can only see it for a swap that already exists. No balance, no
`getwalletinfo` and no port check distinguishes a copied wallet from a separate
one. So: do not copy it. A fresh datadir creates its own `wallet.dat` on first
start, with its own keys, which is the whole point.

What is safe to copy, and the only reason to copy anything, is **block data** —
`blk*.dat`, `rev*.dat`, `blocks/`, `chainstate`, `txleveldb`, depending on the
build. That is public chain data; copying it saves the sync. If you are not sure
which files are which, copy nothing and let the daemon sync.

### Why a second daemon and not a second wallet: measured, not assumed

Gridcoin sits on the **pre-0.17 Bitcoin RPC surface**, which this tree has
measured repeatedly rather than inferred:

    getaddressinfo   absent, rpc code -32601        chains/base.py:436
    gettxout         absent, "unknown command"      modules/rpc_method_support.py
    getbalances      absent on Gridcoin             chain_balances.py:205
    signrawtransactionwithkey  absent               modules/htlc_rpc.py:269
    getblockchaininfo carries no `chain`;
      the testnet flag is getinfo.testnet          services/admin_view.py:1383

This section used to say the multiwallet question "was not established" and gave
two commands to settle it. **It is settled.** Measured on the operator's Gridcoin
v5.5.1.0 testnet daemon, 2026-10-03:

    gridcoinresearchd -testnet help | grep -iE '^(createwallet|loadwallet|listwallets|unloadwallet)'
      -> (none of the multiwallet RPCs exist on this build)

So Gridcoin has **one wallet per datadir**. There is no `-rpcwallet`, no
`/wallet/<name>` endpoint, and no `walletname` field. The BTC/LTC route above
cannot work here and no configuration can make it: `GRC_RPC_WALLET` must stay
**empty**, because a non-empty value makes `chains/base.RPCAdapter.url` address
`http://host:port/wallet/<name>`, a path this daemon family does not serve.

That is also why the `GRC wallet` line reads

    NOT ESTABLISHED  GRC wallet: getwalletinfo answered with no `walletname`
                     field, so which wallet this endpoint serves was not
                     established.

and always will. It is reporting a field that does not exist, not a field nobody
configured. Six of `wallet_custody.py`'s seven checks answered after BTC and LTC
were separated; this was the seventh, and the fix is not to that line.

### The route: a second datadir, its own conf, its own ports

Every command below is **yours to run**. Nothing in this session created a
datadir, started a daemon, funded an address, wrote a conf or edited a `.env`;
the paths and ports here are what to type, not a record of something done.

**1. A new datadir, and a `gridcoin.conf` in it.** Your daemon's current one is
`/home/mpjones26/.GridcoinResearch` (rpcport 25715, v5.5.1.0, in sync, balance
3780.08554497 as of 2026-10-03) — leave it alone.

    mkdir -p $HOME/.GridcoinResearch-desk

    # $HOME/.GridcoinResearch-desk/gridcoin.conf
    testnet=1                     # the desk daemon is TESTNET; without this it is mainnet
    rpcport=25779                 # see below: already an allowed test port
    port=32750                    # p2p. MUST differ from 32748 (see below)
    server=1
    listen=1
    rpcallowip=127.0.0.1
    rpcuser=<a user you choose, different from the operator daemon's>
    rpcpassword=<a password you choose, different from the operator daemon's>

Its own `rpcuser`/`rpcpassword`, not a copy of the operator daemon's: the whole
check below rests on the two endpoints being two endpoints, and sharing a
credential makes it one mistake away from being neither.

**2. 25779 needs no change to this tree, and 25716 would.**
`swap_terminal/network_target.py:100` reads

    "GRC": ChainPorts(15715, frozenset({25715, 25779, 9876}), "GRC_RPC_PORT",
                      "25715 or 25779 testnet")

so **25779 is already an allowed test port** — `wallet_custody.py`,
`swap_readiness.py` and `chain_balances.py` will all read it. An earlier draft of
this section suggested 25716, which is **not** in that set: every one of those
tools refuses an unrecognized port before opening a socket, on the grounds that
an unknown port may be a mainnet daemon on a custom `-rpcport`. Use 25779 (or
9876). Do not add a port to that table as a side effect of this work.

**3. The p2p port must differ, or the second daemon will not start.**
`gridcoinresearchd -help` reports `-port=<port>` defaulting to **32749, testnet
32748**. Two daemons cannot bind one port, so the second needs a third distinct
value — 32750 above. This is the failure that looks like nothing: the daemon
exits or the log says the port is in use, and from the terminal's side it is
indistinguishable from a daemon that is merely down.

**4. A fresh datadir must SYNC before it is useful.** A daemon that has just
started has no chain, so `validateaddress` answers about address format but
balances, confirmations and payouts are all wrong until it is caught up. Check it
the way the tree already does:

    gridcoinresearchd -datadir=$HOME/.GridcoinResearch-desk -testnet getinfo

and look at `blocks` and `testnet`. Give it the time it needs before pointing the
terminal at it; copying block data (and **not** `wallet.dat`) is the way to
shorten that.

**5. Fund it.** The desk pays customers out of this wallet, so it needs a balance
of its own. Send from your own testnet wallet or a faucet to an address derived
**in the desk daemon**, and read the amount it needs off

    python3 /home/user/swap_terminal/swap_readiness.py --pair GRC:XRP

whose `GRC wallet` line prints the largest single payout that wallet can fund.
Rehearsal 2026-10-01: a BTC→GRC swap took its deposit and then could not pay,
because the payout needed 9049.68583412 GRC against 3780.08854497 spendable. A
freshly created wallet has 0.00000000, which fails the same way after the
customer's deposit is already irreversible.

**6. Point the terminal at the desk daemon.** In the shell that starts the
workers — `config.py` reads the process environment and nothing in the serving
path reads a `.env`, so a value set in a file or another shell does not reach
them:

    export GRC_RPC_PORT=25779
    export GRC_RPC_USER=<the desk conf's rpcuser>
    export GRC_RPC_PASS=<the desk conf's rpcpassword>
    # GRC_RPC_WALLET stays EMPTY. This daemon family has one wallet per datadir
    # and serves no /wallet/<name> path, so a non-empty value breaks the endpoint.

**7. Name the OPERATOR's daemon, so the check has a second endpoint to ask.**
These four are read by `swap_terminal/gridcoin_credentials.operator_endpoint()`
and are new with this change:

    export GRC_OPERATOR_RPC_PORT=25715      # the operator's own daemon, TESTNET
    export GRC_OPERATOR_RPC_USER=<the operator conf's rpcuser>
    export GRC_OPERATOR_RPC_PASS=<the operator conf's rpcpassword>
    export GRC_OPERATOR_RPC_HOST=127.0.0.1  # optional; this is the default

They are `config.py`'s own `GRC_RPC_<FIELD>` shape with `OPERATOR` inserted to
say whose daemon it is. Three rules about them, and the first is absolute:

- **The password is read from the ENVIRONMENT and from nowhere else.** The tool
  never opens `gridcoinresearch.conf`, never reads a `.env`, and never prints the
  value. This repository has already published a live `GRIDCOIN_RPC_PASSWORD`
  once, through a `.env.bak` that reached GitHub, so a diagnostic that parsed an
  rpcpassword out of a conf file would put that secret in its own process,
  tracebacks and pasted output.
- **There is no fallback to `GRC_RPC_*`.** With any of the three unset the check
  refuses and names which. The convenient alternative would ask the **desk**
  daemon whether it owns the desk's own address, get the `ismine: true` it always
  gets, and report "one wallet serves both" — about one daemon compared with
  itself.
- **The port is classified before any socket opens.** 15715 is mainnet and is
  refused; so is any port not in `{25715, 25779, 9876}`.

### Verify, with a swap

The cross-daemon check needs `--swap` with a **GRC deposit leg**, because the
address it asks about is that swap's own `deposit_address` from
`swap_terminal.db`. The alternative — `getnewaddress` — is a wallet **write**: it
derives and stores a key, and a read-only audit must not pay for its answer with
a new key.

    python3 /home/user/swap_terminal/wallet_custody.py --swap <a GRC->* swap id>

The `GRC operator daemon` line is the one to read:

| line reads | what it means |
| --- | --- |
| `SEPARATED` | `ismine=false` from the operator's daemon. The wallet holding the operator's coins does **not** hold the key the desk derived. This is the answer you are working toward. |
| `NOT SEPARATED` | `ismine=true` from the operator's daemon. One wallet reached through two endpoints — or a copied `wallet.dat`. Start over with a fresh datadir. |
| `NOT THE DESK'S` | `ismine=false` from **both**. Separated from the operator, and the desk cannot spend its own deposit either. Check whether `GRC_RPC_PORT` changed after that swap row was written. |
| `NOT ESTABLISHED` | the four variables are unset, the port was refused, or the daemon did not answer. The reason is printed on the line. Never a pass. |

**What a `SEPARATED` line does not establish**, and both limits are printed on the
line itself rather than left here:

- that no **other** wallet of yours holds that key. One daemon and one datadir
  were asked. A second datadir, a restored backup, a watch-only import — none of
  them were asked and none of them could be from one endpoint.
- that the desk's coins were never yours. Funding is unaudited, here as
  everywhere else in that report: no chain and no RPC says whose money a coin is.

### One more hazard the other two chains do not have

**The wallet passphrase.** `services/payout_service.WALLET_UNLOCK_ENV_VAR` is
`GRIDCOIN_WALLET_PASSPHRASE`, read from the environment at use time, and three
rehearsals on 2026-10-01 died on it being absent from the shell the supervisor was
started from — after the deposit was credited. A new desk wallet you encrypt needs
its passphrase in that variable, in that shell.

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

    python3 /home/user/swap_terminal/wallet_custody.py --swap <a GRC->* swap id> ; echo "exit=$?"

**`--swap` is required for a clean run**, and this line used to omit it. Three of
the checks are per-swap — the deposit address, the GRC cross-daemon ownership
question, and whether an XRP payout left the desk — and without a swap each one
reports `NOT ESTABLISHED` with "you did not ask me", which is correct and is not a
pass.

Exit 0 only when every line answered `SEPARATED`, `BY DESIGN` or `DESK OWNS IT`.
`NOT ESTABLISHED` is deliberately **not** a pass: a daemon that did not answer
exits non-zero with the reason printed, because a green verdict by default is the
defect the tool exists to remove.

**ON A HOST WITH GRC CONFIGURED, EXIT 0 IS NOT REACHABLE, AND THAT IS NOT A BUG
TO ROUTE AROUND.** The `GRC wallet` line asks for `getwalletinfo().walletname`,
and that field does not exist on this daemon family (measured 2026-10-03: no
multiwallet RPCs), so it reports `NOT ESTABLISHED` however the desk is set up. The
honest reading is therefore the LINE and not the exit code:

- `GRC wallet` → always `NOT ESTABLISHED`. Nothing to fix; the field is absent.
- `GRC operator daemon` → this is the GRC verdict. `SEPARATED` is the answer you
  are working toward.

Do not "fix" the exit code by adding `NOT ESTABLISHED` to the tool's good states.
That would make every unanswered daemon on every chain read as a pass, which is
the one failure the whole tool is shaped to prevent, and it would buy a green
light for a line that is honest about a question this chain cannot answer.

A clean run establishes that the desk signs with keys in a wallet or account that
is not the one your own CLI reaches by default. It does **not** establish:

- that the coins in those wallets were never yours. No chain and no RPC says whose
  money a coin is; there is no `isdesks` beside `ismine`. A named wallet separates
  the **keys**, and nothing can audit how it was funded.
- that an address the desk does not control belongs to a customer. On all five
  chains the desk cannot tell a customer's account from a second account you hold.

Both limits are printed at the top of every run, before any verdict, because a
limit in a footnote has already been misread by the time it is reached.
