#!/usr/bin/env python3
"""Broadcast the payout leg of every swap whose deposit has confirmed.

Role: module (polling loop; the decisions live in services/payout_service.py)
Reads: swap_terminal.db (swaps, payouts, wallet_inventory), BTC/LTC/GRC wallet
       RPC (getbalance)
Writes: swap_terminal.db (payouts, wallet_inventory, swaps, swap_audit_log)
       AND THE CHAIN
Can move funds: YES. This is the only process in the Flask suite that calls
       `sendtoaddress`. Every cycle it may broadcast a transaction that cannot
       be recalled, to the address recorded on the swap, for
       output_amount_estimate.
Mainnet-safe: NO -- not in the sense the other headers mean it. Running this
       against a funded mainnet wallet is not an inspection, it is operating
       the payout path. Read the two paragraphs below before starting it.

TWO OF THESE MUST NOT RUN AT ONCE, AND NOTHING IN THE CODE PREVENTS IT.

Measured 2026-09-24 in tests/test_payout_concurrency.py, against a real SQLite
database with the real process_pending_payouts(): two overlapping workers both
paid the SAME swap -- 2 sends, 1 swap_id, 2 rows in `payouts`, both
status='broadcast'.

SQLite's single writer lock does not prevent it. The guard

    SELECT * FROM payouts WHERE swap_id = ? AND status IN ('broadcast','completed')

is a READ, and it happens before the write lock is ever contended. The second
worker runs it while the first is still inside `sendtoaddress` with its
transaction open, sees no payout row, decides to pay, and only then blocks on
the lock. The lock serialized the writes and did nothing about the decision.

The fix is a claim-by-UPDATE plus a partial unique index, both demonstrated
against a real database in that test file. It is NOT applied, because changing
the payout path is fund movement and belongs to the operator (rule 16). Until
it is applied, the pid file written by supervisor.py is the only thing standing
between one payout and two, and a pid file is a convention rather than a
constraint: starting this script by hand in a second terminal walks straight
past it.

REAPER: swap_terminal/supervisor.py. SIGTERM is handled so that the current
cycle FINISHES before exit -- on this worker specifically, dying between
`sendtoaddress` returning a txid and the UPDATE that records it means money on
chain with no row in the database.
"""

import os
import sys
import time
from pathlib import Path

# See deposit_watcher.py for why this sys.path line exists (rule 10's layout gap).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import Config
from db import SCHEMA, db_session
from services.payout_service import process_pending_payouts, refresh_wallet_inventory
from workers.common import (
    announce_start,
    build_adapters,
    cycle_line,
    get_config_dict,
    install_stop_handler,
    sleep_until_next_cycle,
)

WORKER_NAME = "payout_worker"
DEFAULT_POLL_SECONDS = 10


def main(poll_seconds: int = DEFAULT_POLL_SECONDS) -> int:
    should_stop = install_stop_handler()
    announce_start(WORKER_NAME, poll_seconds, os.getpid())
    print(
        "  CAN BROADCAST   this worker calls sendtoaddress. A second copy of it can pay the same swap twice; "
        "see tests/test_payout_concurrency.py for the measurement.",
        flush=True,
    )
    adapters = build_adapters()
    config = get_config_dict()

    cycle = 0
    while not should_stop():
        cycle += 1
        started = time.monotonic()
        with db_session(Config.DB_PATH) as db:
            db.executescript(SCHEMA)
            pending = db.execute("SELECT COUNT(*) AS n FROM swaps WHERE status = 'payout_pending'").fetchone()["n"]
            if pending:
                # Announce before, not only after: a broadcast is about to
                # happen and the operator should see that BEFORE the RPC call,
                # not in a summary line that appears once it has completed.
                print(f"  {WORKER_NAME} cycle={cycle}: {pending} swap(s) pending payout, broadcasting now", flush=True)
            refresh_wallet_inventory(db, adapters)
            completed = process_pending_payouts(db, config, adapters)
            failed = db.execute("SELECT COUNT(*) AS n FROM swaps WHERE status = 'failed'").fetchone()["n"]
        print(
            cycle_line(
                WORKER_NAME,
                cycle,
                time.monotonic() - started,
                {"pending_at_start": pending, "broadcast": len(completed), "failed_total": failed},
                notes=(
                    "pending_at_start=0 while swaps are open may mean deposit_watcher is not running; "
                    "failed_total is cumulative, not this cycle"
                ),
            ),
            flush=True,
        )
        sleep_until_next_cycle(poll_seconds, should_stop)

    print(f"{WORKER_NAME}: stopped cleanly after {cycle} cycle(s)", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
