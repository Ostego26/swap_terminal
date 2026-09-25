#!/usr/bin/env python3
"""Poll the chains for deposits against open swaps and advance their status.

Role: module (polling loop; the decisions live in services/deposit_service.py)
Reads: swap_terminal.db (swaps, deposit_events), BTC/LTC/GRC wallet RPC
       (listtransactions, getrawtransaction, gettransaction)
Writes: swap_terminal.db (deposit_events, swaps.status, swaps.credited_at,
       swap_audit_log)
Can move funds: no. It never calls sendtoaddress and never signs anything.
       It does move a swap into `payout_pending`, which is the state
       payout_worker.py acts on -- so it is one step upstream of a broadcast,
       and a confirmation threshold read wrongly here releases a payout early.
Mainnet-safe: yes to run against mainnet; it is read-only with respect to the
       chain.

REAPER: swap_terminal/supervisor.py. `supervisor.py start deposit_watcher`
spawns it and writes runtime/deposit_watcher.pid; `supervisor.py stop` sends
SIGTERM, waits, escalates to SIGKILL and then proves the process is gone. A
spawn and its reap are one change (rule 13) -- if you add a loop to this file,
add it to that file in the same commit.

Before 2026-09-24 this file was eighteen lines: a bare `while True:` with a
`time.sleep(poll_seconds)` at the bottom. Nothing in the tree started it and
nothing stopped it, and it printed nothing at all -- so a deposit watcher that
was working and one that had wedged on a chain RPC rendered identically, which
is rule 14's defect in its purest form.
"""

import os
import sys
import time
from pathlib import Path

# The application imports its own modules rootlessly (`from config import
# Config`), so running this file directly requires its package root on the
# path. supervisor.py sets cwd for the same reason. This is rule 10's layout
# gap; fixing it properly means moving entry points to the root, which is a
# large diff with no behavioral benefit on a key-holding system.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import Config
from db import SCHEMA, db_session
from services.deposit_service import ACTIVE_STATUSES, process_active_swaps
from workers.common import (
    announce_start,
    build_adapters_from_config,
    cycle_line,
    get_config_dict,
    install_stop_handler,
    sleep_until_next_cycle,
)

WORKER_NAME = "deposit_watcher"
DEFAULT_POLL_SECONDS = 15

_ACTIVE_PLACEHOLDERS = ",".join("?" for _ in ACTIVE_STATUSES)


def main(poll_seconds: int = DEFAULT_POLL_SECONDS) -> int:
    should_stop = install_stop_handler()
    announce_start(WORKER_NAME, poll_seconds, os.getpid())
    adapters = build_adapters_from_config()
    config = get_config_dict()

    cycle = 0
    while not should_stop():
        cycle += 1
        started = time.monotonic()
        with db_session(Config.DB_PATH) as db:
            db.executescript(SCHEMA)
            before = db.execute(
                # The suppression on the next line is a claim that was
                # checked: the interpolated text is a run of '?' generated
                # from ACTIVE_STATUSES' LENGTH, and the statuses themselves
                # are bound as parameters on the line after it. Structure,
                # not input.
                f"SELECT COUNT(*) AS n FROM swaps WHERE status IN ({_ACTIVE_PLACEHOLDERS})",  # noqa: S608
                ACTIVE_STATUSES,
            ).fetchone()["n"]
            processed = process_active_swaps(db, config, adapters)
            pending = db.execute("SELECT COUNT(*) AS n FROM swaps WHERE status = 'payout_pending'").fetchone()["n"]
        print(
            cycle_line(
                WORKER_NAME,
                cycle,
                time.monotonic() - started,
                {"active_swaps": before, "refreshed": len(processed), "now_payout_pending": pending},
                notes=(
                    "active_swaps=0 is expected only when no swap is open; "
                    "now_payout_pending is what payout_worker acts on"
                ),
            ),
            flush=True,
        )
        sleep_until_next_cycle(poll_seconds, should_stop)

    print(f"{WORKER_NAME}: stopped cleanly after {cycle} cycle(s)", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
