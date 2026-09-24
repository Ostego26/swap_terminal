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

## Run workers

In separate terminals:

```bash
python workers/deposit_watcher.py
python workers/payout_worker.py
python workers/reconcile_worker.py
```

## Endpoints

- `POST /api/quotes`
- `POST /api/swaps`
- `GET /api/swaps/<swap_id>`
- `GET /api/rates`
- `GET /api/health`
