#!/usr/bin/env python3
"""Market cap, 24h volume and drift beside each price -- and, with --record, the row that
makes tomorrow's comparison possible.

Role: file (operator entry point, at the project root per CLAUDE.md rule 10)
Reads: services/pricing's CoinGecko cache (one request, shared with the quote path),
       swap_terminal.db's market_context table, and Config's QUOTE_TTL_SECONDS,
       RATE_CACHE_SECONDS and DEFAULT_FEE_BPS
Writes: market_context, and ONLY with --record. Nothing else, ever.
Can move funds: no. It issues no wallet RPC and touches no quote, swap or payout row.
Mainnet-safe: yes -- there is no chain here. CoinGecko's public prices are the same numbers
       for everyone and the table is a diagnostic.
Live-safe: yes, with one caveat stated rather than buried: --record appends to the live
       swap_terminal.db, so it takes the same write lock every other worker takes. It is one
       INSERT per asset and commits immediately.

WHY THIS FILE EXISTS, which is the operator's actual request rather than the code's shape.

"we should also keep track of the market cap and price comparison to better establish grc
prices" (2026-09-27). The verdict function, the table, the writer and the pasteable block
all landed in 91bd6da -- and NOTHING CALLED THEM. collect_and_record() said so in its own
docstring and handed the wiring over, correctly, because a periodic trigger belongs at the
root and that commit was not touching the root.

A table with no writer is not tracking anything. Counted at the moment this file was
written: market_context held 0 rows and had existed for the length of one commit. "Keep
track" is a claim about rows, so until something runs on a schedule there is no GRC history
and this file says that out loud below rather than implying one.

WHY THE DEFAULT IS READ-ONLY AND --record IS A FLAG. Same reason atomic_swap.py refuses
without --run. A bare invocation of anything in this repository must not write, because the
first thing an operator does with an unfamiliar script is run it to see what it says.

THE CADENCE IS THE OPERATOR'S, AND DELIBERATELY NOT DECIDED HERE. A cron entry or a loop is
a spawn, and rule 13 says a spawn and its reaper are one change -- so this file does not
daemonize itself, does not fork, and does not respawn. It runs once and exits, which is what
cron wants and what a `watch` wants. Suggested, not installed:

    */5 * * * * cd <repo> && .venv/bin/python market_context_report.py --record >> \\
                runtime/market_context.log 2>&1

Five minutes because RATE_CACHE_SECONDS is 30s and CoinGecko's free tier is rate-limited;
288 rows per asset per day is a history worth having and is nowhere near a limit. That
number is a judgment, not a measurement, and it is the operator's to change.

WHAT IS NOT TESTED FROM HERE, said plainly because rule 17 draws the line between a finding
and a hypothesis. Egress to api.coingecko.com is DENIED from the container this was written
in -- the proxy answers 403 to CONNECT, recorded as connect_rejected -- so the fetch leg has
never made a real request. Everything below the fetch is tested against seeded rows
(tests/test_market_context_report.py), and the fetch itself is exercised only against a stub
transport. The four response key names it reads come from CoinGecko's documented shape and
not from a response this code has seen; a wrong name degrades to an UNKNOWN verdict rather
than to a number, which is the property a test does pin.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "swap_terminal"))

from config import Config
from db import SCHEMA, db_session
from microfortnights import format_duration
from services.market_context import (
    QuoteWindow,
    collect_and_record,
    fetch_market_context,
    format_market_context_block,
    recent_market_context,
)


def quote_window() -> QuoteWindow:
    """The three tuned values, read from Config rather than spelled again.

    QuoteWindow has no defaults on purpose (rule 8: a second copy of a setting the operator
    tunes agrees on the day it is written and drifts after), so this is the one place that
    knows where they come from, and the block prints all three so a verdict read a day later
    is still interpretable.
    """
    return QuoteWindow(
        quote_ttl_seconds=float(Config.QUOTE_TTL_SECONDS),
        rate_cache_seconds=float(Config.RATE_CACHE_SECONDS),
        fee_bps=float(Config.DEFAULT_FEE_BPS),
    )


def history_lines(conn, assets, limit: int) -> list[str]:
    """The comparison over time, per asset, newest first -- or `(none)` and why.

    THE EMPTY CASE IS THE ONE THAT MATTERS HERE, which is rule 14's "never let an empty
    result print nothing". A fresh market_context has no rows, and a blank section cannot be
    told from a query that broke. So a zero count prints the reason it is probably zero --
    that nothing has run with --record yet -- because the reader has the screen and not this
    docstring.
    """
    lines = [f"history  last {limit} rows per asset, newest first"]
    for asset in assets:
        rows = recent_market_context(conn, asset, limit)
        if not rows:
            lines.append(f"  {asset:<4} (none)  <- 0 rows. Expected non-zero only once something has run "
                         f"--record; the table is written by nothing else")
            continue
        lines.append(f"  {asset:<4} {len(rows)} rows")
        for row in rows:
            cap = "(none)" if row["market_cap_usd"] is None else f"${row['market_cap_usd']:,.0f}"
            volume = "(none)" if row["volume_24h_usd"] is None else f"${row['volume_24h_usd']:,.0f}"
            age = format_duration(max(0.0, time.time() - float(row["fetched_at"])))
            lines.append(f"       {row['recorded_at']}  ${row['price_usd']:,.8f}  cap={cap}  "
                         f"vol24h={volume}  age={age}")
    return lines


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__ and __doc__.splitlines()[0])
    parser.add_argument("--record", action="store_true",
                        help="APPEND one market_context row per asset. Without it, nothing is written.")
    parser.add_argument("--history", type=int, default=5, metavar="N",
                        help="how many past rows to show per asset (default 5; 0 to skip)")
    parser.add_argument("--db", default=Config.DB_PATH,
                        help=f"database to read and (with --record) append to (default {Config.DB_PATH})")
    return parser


#: Exit code for a database that is not there. 2 rather than 1, so a caller can tell
#: "I was pointed at nothing" from a failure inside the report -- the same distinction
#: absorb_db.open_both() draws for the same reason.
NO_DATABASE_EXIT = 2


def refuse_a_missing_database(db_path) -> list[str] | None:
    """Refusal lines for a path that names no database, or None if it is there.

    =========================================================================
    THIS TOOL CREATED A DATABASE AND ITS OWN TEST SAID IT WROTE NOTHING
    =========================================================================

    Measured 2026-10-10, after db.connect_db() learned to refuse a missing file:
    tests/test_market_context_report.py had eight tests, including
    `test_a_bare_run_writes_nothing`, and they ran this tool against a tmp_path with
    NO DATABASE IN IT. db_session() reached a bare sqlite3.connect(), which CREATES
    the file, and main() then ran `conn.executescript(SCHEMA)` on it -- so a "read
    only -- nothing will be written" run left behind a fully initialized empty
    database, and the test asserting it wrote nothing passed the whole time.

    That is the 2026-10-01 two-database failure with a REPORTING TOOL as the second
    writer. A typo in --db did not produce an error; it produced a third database,
    schema and all, that the next thing to look at that path would open without
    complaint.

    =========================================================================
    WHY A REFUSAL LINE RATHER THAN LETTING DatabaseNotFound PROPAGATE
    =========================================================================

    connect_db() already refuses, so the tool is SAFE either way -- this is about
    output. A traceback is the wrong answer on a terminal (rule 14): it buries the
    one useful sentence under a stack, and it exits 1, which is the same code as a
    report that failed halfway through. show_fees.py, show_swap.py, open_swap.py and
    show_unattributable.py all draw this distinction with an explicit exists() check
    and their own refusal type; this is the same shape, named here so a reader who
    finds one is told the others exist (rule 8).

    AND IT SAYS WHAT "NO DATABASE" IS NOT. "No market context" is the answer a
    healthy empty table gives. A file that was never created is a different fact, and
    those two must never share a line.
    """
    if Path(db_path).exists():
        return None
    return [
        f"  REFUSED: there is no database at {db_path}",
        "           That is NOT 'no market context has been recorded' -- which is what a healthy",
        "           empty table says. The file has never been created. The web app and the",
        "           workers create it on first run.",
        # ONE CLAUSE PER LINE, not wrapped mid-sentence. A test looking for this
        # admission across a newline fails, which is the small version of the real
        # problem: an operator grepping the output for a phrase it prints does not
        # find it either.
        "           Nothing was written, INCLUDING that file.",
        "           Until 2026-10-10 this tool created it and applied the schema, while announcing",
        "           'nothing will be written' on its own first line.",
        "           Check --db, and the working directory if the path is relative.",
    ]


def main() -> int:
    args = build_parser().parse_args()
    window = quote_window()

    # ANNOUNCE BEFORE, NOT ONLY AFTER (rule 14). A CoinGecko fetch behind a proxy can take
    # seconds or hang, and a blinking cursor is indistinguishable from a wedge -- which on
    # this system is how an operator comes to Ctrl-C something healthy.
    action = "APPEND a row per asset" if args.record else "read only -- nothing will be written"
    print(f"market_context_report  db={args.db}  mode: {action}", flush=True)

    # BEFORE THE FETCH, NOT AFTER. A CoinGecko request takes seconds; spending them
    # on a run that is about to refuse is the kind of wait that gets Ctrl-C'd, and
    # the refusal is knowable from the filesystem alone.
    refusal = refuse_a_missing_database(args.db)
    if refusal is not None:
        for line in refusal:
            print(line, flush=True)
        return NO_DATABASE_EXIT

    print(f"  fetching prices and context from CoinGecko (one request, shared cache, "
          f"ttl={format_duration(Config.RATE_CACHE_SECONDS)})...", flush=True)

    started = time.monotonic()
    with db_session(args.db) as conn:
        # Every worker runs this at startup; running it here means a database that predates
        # market_context grows the table rather than failing on the first SELECT.
        conn.executescript(SCHEMA)
        if args.record:
            recorded_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            snapshots = collect_and_record(conn, recorded_at, Config.RATE_CACHE_SECONDS)
            print(f"  recorded {len(snapshots)} rows at {recorded_at}", flush=True)
        else:
            snapshots = fetch_market_context(Config.RATE_CACHE_SECONDS)
        print(f"  fetch done in {format_duration(time.monotonic() - started)}", flush=True)
        print()
        print(format_market_context_block(snapshots, window, args.db))
        if args.history > 0:
            print()
            for line in history_lines(conn, [s.asset for s in snapshots], args.history):
                print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
