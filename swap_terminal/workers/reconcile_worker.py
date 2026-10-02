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

import logging
import os
import sys
import time
from pathlib import Path

# See deposit_watcher.py for why this sys.path line exists (rule 10's layout gap).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import Config
from db import SCHEMA, db_session
from services.deposit_service import process_active_swaps
from services.payout_service import (
    inventory_assets,
    inventory_note,
    refresh_wallet_inventory,
)
from workers.common import (
    CycleFailures,
    announce_start,
    build_adapters_from_config,
    cycle_line,
    get_config_dict,
    install_stop_handler,
    sleep_until_next_cycle,
)

WORKER_NAME = "reconcile_worker"
DEFAULT_POLL_SECONDS = 60


logger = logging.getLogger(__name__)


def run_cycle(db, config: dict, adapters: dict, cycle: int, started: float) -> str:
    """One reconcile cycle: do the work, return the line that describes it.

    EXTRACTED SO THE WIRING CAN BE TESTED, and the reason is a mutation that
    SURVIVED. inventory_note() replaced a literal annotation that told the operator a
    healthy system was broken; its own tests passed, and reverting main()'s call site
    to the old literal ALSO passed, because every test asserted on the function and
    none on the caller. That is the fourth call-site mutation to survive in this
    session -- a right function whose result the caller discards or ignores -- and the
    cycle body being inline in a while loop is what made it untestable.

    This is rule 10's shape: main() is orchestration and must hold no decision, and
    "what does this cycle's line say" is a decision. It is also rule 12's C901 note in
    miniature -- a main() that has swallowed the work is what pushes the complexity up.

    WHAT IS STILL NOT COVERED, said rather than implied (rule 17): main() calling THIS
    function is one line in a loop and no test drives it. That seam is far narrower
    than the one it replaces -- deleting it means the worker prints nothing at all
    every cycle, which `supervisor.py status` now shows as a frozen last line -- but it
    is not zero, and claiming otherwise would be the same overstatement the note itself
    was fixing.
    """
    refresh_wallet_inventory(db, adapters)
    processed = process_active_swaps(db, config, adapters)
    # READ BACK FROM THE TABLE, not assumed from which adapters were asked. An adapter
    # whose get_balance() raised wrote no row, and that difference is exactly what the
    # note reports.
    present = inventory_assets(db)
    return cycle_line(
        WORKER_NAME,
        cycle,
        time.monotonic() - started,
        {"refreshed_swaps": len(processed), "inventory_rows": len(present)},
        # DERIVED FROM THE CONSTRUCTED ADAPTERS, never hardcoded. The string this
        # replaced said "should be 3 (BTC/LTC/GRC)" and printed beside a CORRECT
        # inventory_rows=1 on a host where only GRC and SOL had adapters -- see
        # payout_service.inventory_note()'s docstring for the measurement.
        notes=inventory_note(adapters, present),
    )


def main(poll_seconds: int = DEFAULT_POLL_SECONDS) -> int:
    should_stop = install_stop_handler()
    announce_start(WORKER_NAME, poll_seconds, os.getpid())
    adapters = build_adapters_from_config()
    config = get_config_dict()

    failures = CycleFailures(WORKER_NAME)
    cycle = 0
    while not should_stop():
        cycle += 1
        started = time.monotonic()
        # EVERY CYCLE IS GUARDED, and the measurement is in
        # workers/common.CycleFailures. A devnet DNS lookup failed for a moment on
        # 2026-10-01 and this worker DIED -- there was no try/except anywhere in any
        # of the three workers, so one chain being briefly unreachable terminated
        # the process and stopped deposits being credited on EVERY chain, while
        # payout_worker went on printing IDLE as though there were simply nothing to
        # pay. The operator found it with `supervisor.py status` half an hour later.
        #
        # `except Exception` and NOT BaseException: KeyboardInterrupt and SystemExit
        # must still end the process. A Ctrl-C that only logged a failed cycle and
        # carried on would be a worker the operator cannot stop, which is worse
        # than the crash this replaces (rule 13).
        #
        # This is rule 12's legitimate broad catch, and the test it names is met:
        # the caller CAN tell the failure from a real answer. A failed cycle prints
        # FAILED with the exception type, the reason and a count of consecutive
        # failures -- it does not print IDLE, and it does not print nothing.
        try:
            with db_session(Config.DB_PATH) as db:
                db.executescript(SCHEMA)
                line = run_cycle(db, config, adapters, cycle, started)
            print(line, flush=True)
        except Exception as exc:
            print(failures.record(cycle, time.monotonic() - started, exc), flush=True)
            logger.exception("%s cycle=%d failed", WORKER_NAME, cycle)
        else:
            failures.clear()
        sleep_until_next_cycle(poll_seconds, should_stop)

    print(f"{WORKER_NAME}: stopped cleanly after {cycle} cycle(s)", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
