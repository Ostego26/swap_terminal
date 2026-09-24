#!/usr/bin/env python3
"""Slow sweep: refresh wallet balances and re-check every open swap.

Role: module (polling loop; the decisions live in services/)
Reads: swap_terminal.db (swaps, deposit_events, wallet_inventory), BTC/LTC/GRC
       wallet RPC (getbalance, listtransactions, getrawtransaction)
Writes: swap_terminal.db (wallet_inventory, deposit_events, swaps,
       swap_audit_log)
Can move funds: no -- it calls neither sendtoaddress nor any signing method.
       Like deposit_watcher it CAN move a swap into `payout_pending`, which is
       one step upstream of a broadcast.
Mainnet-safe: yes; read-only with respect to the chain.

IT DOES THE SAME WORK AS deposit_watcher, MORE SLOWLY, AND THAT IS THE POINT
WORTH KNOWING BEFORE CHANGING EITHER.

Measured by reading both files on 2026-09-24: this worker's cycle is
`refresh_wallet_inventory()` plus `process_active_swaps()` at 60s;
deposit_watcher's is `process_active_swaps()` alone at 15s. So
process_active_swaps() runs in two loops on two schedules against one database.
That is not (quite) rule 8's duplicate logic -- there is one implementation,
called twice -- but it has the same consequence: with both workers running,
every active swap is refreshed on two independent clocks, and the two can
interleave the way tests/test_payout_concurrency.py shows two payout workers
can. Nothing observed here has proven that harmful for the deposit path, and
saying so is not the same as having checked it (rule 17): what was checked is
that the two call the same function, not what happens when their cycles
overlap. Whether this loop should exist at all is a question for the operator,
because merging it into deposit_watcher changes when deposits get credited,
which is upstream of a payout.

REAPER: swap_terminal/supervisor.py. See deposit_watcher.py's header.
"""

import os
import sys
import time
from pathlib import Path

# See deposit_watcher.py for why this sys.path line exists (rule 10's layout gap).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import Config
from db import SCHEMA, db_session
from services.deposit_service import process_active_swaps
from services.payout_service import refresh_wallet_inventory
from workers.common import (
    announce_start,
    build_adapters,
    cycle_line,
    get_config_dict,
    install_stop_handler,
    sleep_until_next_cycle,
)

WORKER_NAME = "reconcile_worker"
DEFAULT_POLL_SECONDS = 60


def main(poll_seconds: int = DEFAULT_POLL_SECONDS) -> int:
    should_stop = install_stop_handler()
    announce_start(WORKER_NAME, poll_seconds, os.getpid())
    adapters = build_adapters()
    config = get_config_dict()

    cycle = 0
    while not should_stop():
        cycle += 1
        started = time.monotonic()
        with db_session(Config.DB_PATH) as db:
            db.executescript(SCHEMA)
            refresh_wallet_inventory(db, adapters)
            processed = process_active_swaps(db, config, adapters)
            inventory = db.execute("SELECT COUNT(*) AS n FROM wallet_inventory").fetchone()["n"]
        print(
            cycle_line(
                WORKER_NAME,
                cycle,
                time.monotonic() - started,
                {"refreshed_swaps": len(processed), "inventory_rows": inventory},
                notes=(
                    "inventory_rows should be 3 (BTC/LTC/GRC); fewer means a getbalance call is failing "
                    "and refresh_wallet_inventory swallowed it"
                ),
            ),
            flush=True,
        )
        sleep_until_next_cycle(poll_seconds, should_stop)

    print(f"{WORKER_NAME}: stopped cleanly after {cycle} cycle(s)", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
