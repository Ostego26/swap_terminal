#!/usr/bin/env python3
"""Slow sweep: refresh wallet balances and re-check every open swap.

Role: module (polling loop; the decisions live in services/)
Reads: swap_terminal.db (swaps, deposit_events, late_deposits,
       wallet_inventory), BTC/LTC/GRC wallet RPC (getbalance, listtransactions,
       getrawtransaction, gettransaction, decoderawtransaction)
Writes: swap_terminal.db (wallet_inventory, deposit_events, swaps,
       swap_audit_log, late_deposits)
Can move funds: no -- it calls neither sendtoaddress nor any signing method.
       Like deposit_watcher it CAN move a swap into `payout_pending`, which is
       one step upstream of a broadcast.
Mainnet-safe: yes; read-only with respect to the chain.

IT DOES THE SAME WORK AS deposit_watcher, MORE SLOWLY, AND IT IS A BACKSTOP RATHER
THAN A DESIGN. READ THIS BEFORE CHANGING EITHER.

Measured by reading both files on 2026-09-24: this worker's cycle is
`refresh_wallet_inventory()` plus `process_active_swaps()` at 60s;
deposit_watcher's is `process_active_swaps()` alone at 15s. So
process_active_swaps() runs in two loops on two schedules against one database.
That is not (quite) rule 8's duplicate logic -- there is one implementation,
called twice -- but it has the same consequence: every active swap is refreshed
on two independent clocks.

IT WAS HARMFUL, AND THAT IS NOW CHECKED RATHER THAN DISCLAIMED. This paragraph
used to say "nothing observed here has proven that harmful for the deposit path,
and saying so is not the same as having checked it (rule 17)". It has been
checked, and the answer was yes. From the operator's own audit trail, the
identical two transitions recorded TWICE 0.83s apart on s_95a807c181644190 --
`awaiting_deposit -> confirming` and `confirming -> payout_pending` -- because
both loops processed that swap at once. Exactly one payout row existed, so the
payout claim guard held and nobody was paid twice; the damage was a false audit
trail, which is the record a person reads to find out what happened to somebody's
money.

WHAT MAKES IT HARMLESS NOW is services/swap_service.set_swap_status(), which
since 2026-10-02 is a compare-and-swap: a stale worker's write is refused instead
of applied, no audit row is written for a transition that did not happen, and
services/deposit_service.refresh_swap_from_chain() abandons that swap for the
cycle rather than carrying on from a status it did not write. The loser of the
race now costs one cycle of latency on one swap.

WHY THE CALL STAYS, established rather than assumed. It is NOT a designed
backstop: it arrived in this repository's first commit (43661f4, 2026-09-24,
"Initial import of prior work") with no comment, no commit message and no test
claiming the intent, and the paragraph above is the nearest thing to a record of
why. But it IS the only other path that credits a deposit, because
supervisor.py has start, stop and status and NO respawn -- grepped 2026-10-02:
no restart command, no watchdog, nothing that revives a dead worker. So if
deposit_watcher dies, this 60s loop is what keeps crediting customers' deposits
until a person runs `supervisor.py status` and notices. The duplication costs
60 extra shared-account scans per hour against deposit_watcher's 204.5; the
alternative costs every uncredited deposit between a crash and a human.
Removing the only redundant crediting path is a live posture change on the
strength of an untestable resilience argument, and it is the operator's call
(rule 16), not a tidy-up.

SO THE DUPLICATION IS MEASURABLE INSTEAD OF HIDDEN. `transitions_written` on this
worker's cycle line is how many audit rows THIS pass wrote for the swaps it
refreshed, and it is zero exactly when deposit_watcher is healthy and doing the
work. `refreshed_swaps` is how many swaps are open, not how many this loop moved,
so it is in workers/common.STANDING_COUNTS along with `inventory_rows` -- without
that, this worker printed WORKED on every cycle of its life for re-reading swaps
another process had already advanced, which is rule 13's "skipped plus success in
the same output" in the one worker an operator would check to find out whether the
backstop is carrying the load.

IT IS ALSO THE ONLY THING THAT NOTICES A LATE DEPOSIT, added 2026-10-04 and the one
job this worker does that deposit_watcher does not do at all. A payment arriving at the
deposit address of a swap that has already finished was recorded NOWHERE before that
date -- not credited, not refused, not marked unattributable -- because an
address-attributed chain's deposit address is only ever scanned from inside
process_active_swaps()' `WHERE status IN (ACTIVE_STATUSES)`. Measured on the operator's
host that day: 0.0001 BTC in the hot wallet and `(none)` rows anywhere in the database.
services/late_deposit_service.py carries the full per-status measurement, the window and
its derivation; `late_deposits` on the cycle line is how many rows this pass wrote and
`late_unresolved` is how many a person has yet to deal with. It records and decides
NOTHING -- no credit, no refund, no status change on a settled swap.

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
from services.helpers import utc_now_iso
from services.late_deposit_service import late_note, reconcile_late_deposits
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
    unreachable_note,
)

WORKER_NAME = "reconcile_worker"
DEFAULT_POLL_SECONDS = 60


logger = logging.getLogger(__name__)


def backstop_note(written: int, refreshed: int) -> str:
    """What `transitions_written` MEANS, on the screen rather than in a comment.

    THE EXPLANATION ALREADY EXISTED AND THE OPERATOR COULD NOT SEE IT. It was written
    as a code comment above the key -- "zero here with refreshed_swaps non-zero is the
    healthy shape" -- and the printed line carried only inventory_note(). Measured on
    the operator's host 2026-10-02, the cycle line read:

        reconcile_worker cycle=5 IDLE refreshed_swaps=3 transitions_written=0
        inventory_rows=1 ... <- expected 3 -- one per CONSTRUCTED adapter (GRC, SOL, XRP)

    One annotation, about the third count. `transitions_written=0` is the field this
    whole commit added and the one an operator has no prior intuition for, and nothing
    on the screen said whether 0 was good news.

    That is the identical defect payout_service.inventory_note() was written to fix one
    field over, and rule 14 states the rule it breaks: "State what the number means,
    next to the number. The operator reads the screen, not the source."

    ZERO IS THE HEALTHY READING AND MUST SAY SO, because it is the counter-intuitive
    direction: on every other count on this line, more means more work done, and here
    non-zero means the BACKSTOP is doing work the primary should have done.
    """
    if not refreshed:
        return (
            "transitions_written=0 with refreshed_swaps=0 says nothing either way: no "
            "swap was open, so there was nothing to move"
        )
    if written:
        return (
            f"transitions_written={written} means THIS 60s loop moved {written} swap "
            f"status(es) that deposit_watcher's 15s loop had not -- worth a second look "
            f"while both workers are up, because this loop is the backstop and not the "
            f"primary. Check deposit_watcher's own last line"
        )
    return (
        "transitions_written=0 is the HEALTHY reading, and it is the only count on this "
        "line where zero is the good news: deposit_watcher advanced every swap at 15s and "
        "this 60s backstop pass found nothing left to move. Non-zero means the backstop "
        "is the one crediting"
    )


def transitions_written(db, swap_ids, *, since: str) -> int:
    """How many audit rows THIS cycle wrote for the swaps it refreshed. One SELECT.

    WHY THIS NUMBER EXISTS, and it is the measurable half of the two-loops defect. Before
    it, this worker's line reported `refreshed_swaps` -- how many swaps are OPEN -- and
    nothing distinguished a pass that credited a deposit from a pass that re-read three
    swaps deposit_watcher had already advanced four times over. An operator could not tell
    from the screen whether this loop was carrying the crediting or idling behind a healthy
    watcher, which is exactly the question the backstop exists to answer (rule 14: state
    what the number means, next to the number).

    IT IS A COUNT IN SQL AND NOT A TALLY IN PYTHON (rule 20). swap_audit_log is written by
    set_swap_status() inside process_active_swaps(), several layers below this function, so
    a counter threaded back up would be a second place that knows what a transition is.
    The rows are the record; counting the rows is the question.

    SCOPED TO THE SWAPS THIS CYCLE PROCESSED, not to a time window alone, so payout_worker
    claiming an unrelated swap in the same second cannot inflate it. THE REMAINING
    IMPRECISION IS NAMED RATHER THAN SMOOTHED OVER (rule 3: state the denominator): a swap
    this cycle advanced INTO `payout_pending` can be claimed by payout_worker before this
    SELECT runs, and that claim's `payout_pending -> paying` row is inside both the window
    and the id set. So this can read one higher than the transitions this loop itself wrote.
    It is a visibility figure, not a gate, and nothing branches on it.

    AN EMPTY SWAP LIST ANSWERS 0 WITHOUT A QUERY, because `IN ()` is not valid SQLite and
    because zero refreshed swaps genuinely wrote zero transitions. Rule 14: that is a
    result, and the line prints it rather than leaving a gap.
    """
    ids = [str(swap_id) for swap_id in swap_ids or ()]
    if not ids:
        return 0
    # The interpolated text is a run of '?' generated from the LENGTH of `ids`; every id
    # and the timestamp are bound as parameters below. Structure, not input -- the same
    # claim services/deposit_service.process_active_swaps() makes for ACTIVE_STATUSES, and
    # checkable from these three lines (rule 12's S608 note).
    placeholders = ",".join("?" for _ in ids)
    row = db.execute(
        f"SELECT COUNT(*) AS n FROM swap_audit_log"  # noqa: S608
        f" WHERE created_at >= ? AND swap_id IN ({placeholders})",
        (since, *ids),
    ).fetchone()
    return int(row["n"] or 0)


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
    # THE WALL CLOCK, not `started`. `started` is time.monotonic(), which is the right
    # thing to measure a duration with and cannot be compared against a timestamp column
    # (rule 6's boundary: seconds where an API demands them, and this one demands an ISO
    # string). Taken BEFORE the work so nothing this cycle writes falls outside it.
    cycle_began = utc_now_iso()
    refresh_wallet_inventory(db, adapters)
    processed = process_active_swaps(db, config, adapters)
    written = transitions_written(db, [row["id"] for row in processed], since=cycle_began)
    # THE LATE-DEPOSIT PASS, AND WHY IT IS IN *THIS* WORKER RATHER THAN IN deposit_watcher.
    #
    # deposit_watcher's 15s loop exists to credit a deposit promptly, and a late deposit is
    # the one case where promptness buys nothing: nothing is credited, nothing is released,
    # and a person has to read the row before anything happens to the coins. This worker is
    # already the 60s backstop that re-reads what the fast loop may have missed, which is the
    # same job one level out -- so the pass runs here, once a minute, and the fast loop keeps
    # its scan count unchanged.
    #
    # IT RUNS AFTER process_active_swaps(), AND THE ORDER TURNS OUT NOT TO BE LOAD-BEARING.
    # That is recorded rather than left for the next reader to rediscover, and it is the same
    # correction deposit_service.process_active_swaps() already carries about
    # reconcile_shared_accounts()' position.
    #
    # I FIRST WROTE HERE THAT THE ORDER WAS LOAD-BEARING, and the mutation refuted it
    # (2026-10-04). The argument was that running the late pass FIRST would see a swap that
    # is about to be credited this cycle, with no deposit_events row yet, and call its own
    # arriving deposit late -- the 2026-10-01 defect where a reconciler called a freshly
    # credited deposit stranded. It cannot happen in either order, and the reason is the
    # status filter: a swap that is still ACTIVE at the top of the cycle is excluded from the
    # late pass by `status NOT IN (ACTIVE_STATUSES)` whatever order the two run in, and by
    # the time it is NOT active its deposit_events row has already been written by the call
    # above, so accounted_keys() excludes it. Two independent exclusions, one per order.
    #
    # MEASURED, not reasoned alone: moving this whole block above process_active_swaps()
    # leaves all 24 tests in tests/test_late_deposits.py passing. The position stays where it
    # is because reading the swap's post-credit status is the more obviously correct one to
    # record in `swap_status`, not because a test pins it.
    #
    # IT COMMITS NOTHING OF ITS OWN: the db_session() in main() owns the transaction, exactly
    # as process_active_swaps()' own writes do.
    late = reconcile_late_deposits(db, config, adapters, now=utc_now_iso())
    late_unresolved = db.execute(
        # READ BACK FROM SQL rather than threaded up from the pass. "How many late deposits is
        # a person still sitting on" is a question about rows in the table and not about what
        # THIS pass did, so it is a SELECT -- the same split transitions_written() makes one
        # field over, and the reason `recorded` and this number are both on the line.
        "SELECT COUNT(*) AS n FROM late_deposits WHERE resolved_at IS NULL",
    ).fetchone()["n"]
    # READ BACK FROM THE TABLE, not assumed from which adapters were asked. An adapter
    # whose get_balance() raised wrote no row, and that difference is exactly what the
    # note reports.
    present = inventory_assets(db)
    return cycle_line(
        WORKER_NAME,
        cycle,
        time.monotonic() - started,
        {
            "refreshed_swaps": len(processed),
            # THE ONE COUNT ON THIS LINE THAT MEANS THIS LOOP DID THE WORK. See the
            # module docstring: zero here with refreshed_swaps non-zero is the healthy
            # shape -- deposit_watcher advanced them at 15s and this 60s pass found
            # nothing left to move. Non-zero means this loop is the one crediting, which
            # on a host where both workers are up is worth a second look.
            "transitions_written": written,
            # NOT IN workers/common.STANDING_COUNTS, AND THAT IS THE DECISION ON THIS LINE.
            # Every field in that set is one whose non-zero value is the healthy steady state,
            # so letting it mark WORKED made a worker claim work for existing. This one is the
            # opposite: it counts rows NEW this pass, it is 0 on every cycle of a healthy
            # system, and the single cycle where it is not is the cycle an operator must see.
            # So a non-zero value SHOULD flip the line to WORKED -- which is rule 14's "make
            # 'did nothing' look different from 'did work'" pointing the other way from
            # `refreshed_swaps`, and the reason both belong on the same line.
            "late_deposits": late.recorded,
            # UNRESOLVED AND NOT TOTAL. A resolved row is one a person has dealt with; leaving
            # it in would make this a number that only ever grows, which is the measurement
            # unattributable_deposit_service.resolve_credited() warns about. In
            # STANDING_COUNTS-spirit it is a standing figure rather than this pass's work --
            # it is reported beside `late_deposits` so the operator can tell "one arrived just
            # now" from "four have been sitting there", and it is deliberately NOT what flips
            # the WORKED marker.
            "late_unresolved": late_unresolved,
            "inventory_rows": len(present),
        },
        # DERIVED FROM THE CONSTRUCTED ADAPTERS, never hardcoded. The string this
        # replaced said "should be 3 (BTC/LTC/GRC)" and printed beside a CORRECT
        # inventory_rows=1 on a host where only GRC and SOL had adapters -- see
        # payout_service.inventory_note()'s docstring for the measurement.
        # BOTH COUNTS THAT NEED EXPLAINING GET IT. inventory_note() covers
        # inventory_rows; backstop_note() covers transitions_written, whose
        # explanation was a code comment the operator never sees.
        # THREE COUNTS THAT NEED EXPLAINING, THREE NOTES. late_note() is the third, and it
        # is the one where ZERO and NON-ZERO read completely differently -- see that
        # function, and services/late_deposit_service.py's header for the measurement that
        # made the field necessary at all.
        # FOUR NOW. unreachable_note() is first when it fires, because it changes how
        # EVERY other count on the line should be read: `refreshed_swaps` short by the
        # swaps on a dead chain is a different fact from `refreshed_swaps` short because
        # nothing is open, and a reader who sees the backstop note first has already
        # formed the wrong one. Empty string on a clean cycle, which is the normal case,
        # so the line does not grow a permanent fourth clause.
        notes=" ".join(part for part in (
            unreachable_note(getattr(processed, "unreachable", ())),
            f"{backstop_note(written, len(processed))}. "
            f"{late_note(late.recorded, late.targets)}. "
            f"{inventory_note(adapters, present)}",
        ) if part),
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
