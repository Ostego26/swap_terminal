# swap_terminal

This is a brokered hot-wallet swap terminal MVP for:

- GRC -> BTC
- BTC -> GRC
- GRC -> LTC
- LTC -> GRC

## Environment

Set these before running:

```bash
export BTC_RPC_USER=...
export BTC_RPC_PASS=...
export BTC_RPC_HOST=127.0.0.1
export BTC_RPC_PORT=8332

export LTC_RPC_USER=...
export LTC_RPC_PASS=...
export LTC_RPC_HOST=127.0.0.1
export LTC_RPC_PORT=9332

export GRC_RPC_USER=...
export GRC_RPC_PASS=...
export GRC_RPC_HOST=127.0.0.1
export GRC_RPC_PORT=25715

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
