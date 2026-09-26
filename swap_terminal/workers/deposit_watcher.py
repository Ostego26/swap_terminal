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
            # HALTED SWAPS, counted because a halt was invisible until 2026-09-26.
            #
            # The operator sent 1 XRP to a swap expecting 5. The tolerance check did
            # exactly what it should -- refused to credit a wrong amount and moved the
            # swap to 'under_review' -- and the cycle line printed
            #
            #     active_swaps=1 refreshed=1 now_payout_pending=0
            #
            # and nothing else. 'under_review' is not in ACTIVE_STATUSES, so the swap
            # left the polled set silently. The only signal was a 0 where a reader had
            # to already know to expect 1.
            #
            # A halt is the single most important thing this worker can report: it is
            # the one outcome that will not resolve on its own, because it exists
            # precisely to wait for a person. Rule 14's "make 'did nothing' look
            # different from 'did work'" -- a cycle that halted a customer's swap must
            # not read like one that found nothing to do.
            #
            # Counted as a TOTAL rather than a delta on purpose: a delta shows the
            # transition once and then reads as zero forever, so a swap sitting halted
            # for a day would be invisible to anyone who started watching after it
            # happened. The standing count keeps it on screen.
            halted = db.execute("SELECT COUNT(*) AS n FROM swaps WHERE status = 'under_review'").fetchone()["n"]
        print(
            cycle_line(
                WORKER_NAME,
                cycle,
                time.monotonic() - started,
                {
                    "active_swaps": before,
                    "refreshed": len(processed),
                    "now_payout_pending": pending,
                    "HALTED_for_review": halted,
                },
                notes=(
                    "active_swaps=0 is expected only when no swap is open; "
                    "now_payout_pending is what payout_worker acts on; "
                    "HALTED_for_review>0 means a swap is waiting on a PERSON and will "
                    "never resolve by itself -- query swaps WHERE status='under_review'"
                ),
            ),
            flush=True,
        )
        sleep_until_next_cycle(poll_seconds, should_stop)

    print(f"{WORKER_NAME}: stopped cleanly after {cycle} cycle(s)", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
