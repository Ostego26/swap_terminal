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

THE SECOND LOOP THAT DOES THIS, named here because a reader who finds one must
be told the other exists (rule 8). workers/reconcile_worker.py calls the same
process_active_swaps() every 60s, in its own process, as a BACKSTOP: this file
is the primary at 15s, and if this process dies nothing revives it --
supervisor.py has no respawn -- so that loop is the only thing still crediting
deposits until a person looks. Its header carries the established reasoning, the
measurement of the duplicated audit rows the two produced, and what makes the
duplication harmless now (the compare-and-swap in
services/swap_service.set_swap_status()). Do not remove either without reading
that header.

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

import logging
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
    CycleFailures,
    announce_start,
    build_adapters_from_config,
    cycle_line,
    get_config_dict,
    install_stop_handler,
    root_tool_command,
    sleep_until_next_cycle,
)

WORKER_NAME = "deposit_watcher"
DEFAULT_POLL_SECONDS = 15

_ACTIVE_PLACEHOLDERS = ",".join("?" for _ in ACTIVE_STATUSES)

# THE TOOL THAT ANSWERS THE HALT, named as a command rather than as a query.
#
# This note used to end with `query swaps WHERE status='under_review'`. The
# counter beside it was right -- the halt had been invisible before it, and the
# operator now saw HALTED_for_review=1 on every cycle -- but a SQL FRAGMENT is
# not a thing a person sitting in a shell can run. The instrument reported a
# problem and handed over half a query, which by rule 14 is silence one step
# removed: the operator reads the screen, not the source, and a hint they cannot
# act on tells them only that something is wrong.
#
# Absolute path, because this process's cwd is swap_terminal/ and the tool is at
# the repository root -- see workers/common.root_tool_command() for the
# measurement. tests/test_show_swap.py asserts the named file exists, so a rename
# fails a test rather than leaving the note pointing at nothing (rule 2: grep for
# the NAME, not the import graph -- nothing imports this string).
HALTED_REVIEW_COMMAND = root_tool_command("show_swap.py")

# The standing explanation of the three counts, in one copy so that the halted
# and unhalted forms below cannot drift apart.
_CYCLE_NOTE = (
    "active_swaps=0 is expected only when no swap is open; "
    "now_payout_pending is what payout_worker acts on; "
    "HALTED_for_review>0 means a swap is waiting on a PERSON and will never resolve by itself"
)


def halted_note(halted: int) -> str:
    """The cycle line's note, which grows a command when a swap is actually halted.

    Rule 14's "make 'did nothing' look different from 'did work'" applied to the
    note rather than to the counts: a cycle with nothing halted does not need a
    command, and printing one every fifteen seconds for a condition that is not
    happening is how an operator learns to skim the tail of this line -- which is
    where the command would be on the one cycle that mattered.

    So the command appears exactly when it is actionable, and it carries the count
    with it so the sentence stands alone in a pasted log a day later.

    A function, not an f-string at the call site, because this is the decision the
    whole change is about and it has to be callable with a seeded count (rule 10).
    """
    if not halted:
        return _CYCLE_NOTE
    return (
        f"{_CYCLE_NOTE}. {halted} is waiting now -- see which one and why, read-only, writes nothing: "
        f"{HALTED_REVIEW_COMMAND}"
    )


logger = logging.getLogger(__name__)


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
                #
                # THIS COUNT AND show_swap.py's LISTING MUST NAME THE SAME SET. The tool
                # the note hands over reads services/swap_view.HALTED_STATUSES, which is
                # derived from STATUS_MEANINGS; this is the literal. They are not one
                # expression because an `IN (?)` built from a tuple's length needs SQL
                # assembled by interpolation, and that needs an S608 suppression -- rule
                # 19 does not allow buying a check pass with one. The guard is a test
                # instead: tests/test_show_swap.py asserts the status this line counts is
                # exactly the set the tool lists, so a second halted status added to the
                # vocabulary fails there rather than producing a count of 2 beside a list
                # of 1.
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
                    # HALTED_for_review is a STANDING CONDITION rather than work this
                    # cycle did, and workers/common.STANDING_COUNTS names it as one.
                    # Counted as work, it made every cycle print WORKED for as long as
                    # a single swap sat halted -- see that constant for the measurement.
                    notes=halted_note(halted),
                ),
                flush=True,
            )
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
