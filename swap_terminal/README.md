# swap_terminal

This is a brokered hot-wallet swap terminal MVP for:

- GRC -> BTC
- BTC -> GRC
- GRC -> LTC
- LTC -> GRC

## Environment

> **THESE PORTS USED TO BE MAINNET AND THE BLOCK BELOW WAS COPY-PASTEABLE.** Until
> 2026-10-10 this section said `BTC_RPC_PORT=8332`, `LTC_RPC_PORT=9332` and
> `GRC_RPC_PORT=25715`. The first two are **mainnet**. `config.py:13-22` records what
> those exact numbers cost: the module defaults once pointed at mainnet, nothing in the
> serving path loads a `.env`, and an unset `GRC_RPC_PORT` fell through to 15715 so
> `payout_service.refresh_wallet_inventory()` polled the operator's **live staking
> wallet** on every cycle. Their words, 2026-09-26: *"we're still pulling from grc
> mainnet wallet and not the testnet wallet."*
>
> The defaults were fixed that day -- all three now default to `UNCONFIGURED_PORT` and
> `chains/registry.py` skips an unconfigured chain, so a missing setting refuses rather
> than guessing. **This file was not**, and it is the one a reader pastes from. A wrong
> comment is a bug (rule 16), and a wrong comment that is a runnable mainnet
> configuration on a live-money desk is the worst shape it comes in.
>
> `network_target.CHAIN_PORTS` is the authority on which port is which network, and it
> is what refuses an unrecognized one before any socket opens.

Set these before running. **Test ports only** -- the numbers below are the ones
`network_target.CHAIN_PORTS` classifies as `TEST`:

```bash
export BTC_RPC_USER=...
export BTC_RPC_PASS=...
export BTC_RPC_HOST=127.0.0.1
# 18332 testnet, 18443 regtest. MAINNET IS 8332 -- do not put it here.
export BTC_RPC_PORT=18332

export LTC_RPC_USER=...
export LTC_RPC_PASS=...
export LTC_RPC_HOST=127.0.0.1
# 19332 testnet, 19443 regtest. MAINNET IS 9332 -- do not put it here.
export LTC_RPC_PORT=19332

export GRC_RPC_USER=...
export GRC_RPC_PASS=...
export GRC_RPC_HOST=127.0.0.1
# 25715 and 25779 are both testnet; the desk daemon uses 25779.
# MAINNET IS 15715 -- do not put it here.
export GRC_RPC_PORT=25779

export SWAP_DB_PATH=$PWD/swap_terminal.db
```

## Install

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Run API

```bash
python app.py
```

The API binds `127.0.0.1:5000` with the Werkzeug debugger OFF. Both are
overridable and both default to the safe value:

```bash
SWAP_TERMINAL_HOST=0.0.0.0 SWAP_TERMINAL_PORT=5051 python app.py
```

Do not set `SWAP_TERMINAL_DEBUG=1` on a host with a funded wallet. The
debugger's console executes Python as the process holding the RPC
credentials; the startup banner says so when it is on.

## Run workers

Through the supervisor, which is also the reaper:

```bash
python supervisor.py start            # all three, with pid files
python supervisor.py status           # what is running, and against which database
python supervisor.py stop             # SIGTERM, then SIGKILL, then PROVES absence
python supervisor.py start payout_worker   # one by name
```

**Do not start them by hand in separate terminals.** Two payout workers
polling the same database both pay the same swap -- measured, 2 sends for 1
deposit, in `tests/test_payout_concurrency.py`. The supervisor's pid file is
the only thing preventing a second copy, and running the script directly walks
straight past it.

## Endpoints

- `POST /api/quotes`
- `POST /api/swaps`
- `GET /api/swaps/<swap_id>`
- `GET /api/rates`
- `GET /api/health`
