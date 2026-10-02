#!/usr/bin/env python3
"""What this terminal has actually charged, per swap and per asset. Read-only.

Role: file (entry point, at the repository root per CLAUDE.md rule 10)
Reads: config.Config (DB_PATH, DEFAULT_FEE_BPS, AMOUNT_TOLERANCE_PCT) and
       swap_terminal.db -- the `swaps` and `payouts` tables, through
       fee_ledger.py. It opens no socket: no chain, no daemon, no price feed.
Writes: NOTHING. Not a row, not a view, not the database file -- the path is
       checked with Path.exists() before sqlite3.connect(), because connect()
       CREATES a missing file and a read-only tool that leaves an empty database
       behind has written something while announcing that it would not. Same
       guard, for the same reason, as show_swap.read_report().
Can move funds: no. It has no --set, no --collect and no --sweep, it constructs
       no adapter, it imports nothing that can sign, and it changes no fee. The
       fee rate and where the fee goes are both fund movement and the operator's
       decision (rule 16); this tool exists to put the evidence for those
       decisions on a screen.
Mainnet-safe: yes. It chooses no network and reaches none.

WHY THIS FILE EXISTS.

The operator asked, 2026-10-01, for the swap fee to be "comparable to other
exchanges out there". Measured that day, nothing in the tree could say what the
fee WAS: `DEFAULT_FEE_BPS=150` is the schedule, `fee_bps` is stored per swap, and
the charge itself is never materialized -- not in a column, not on the swap page,
not in a worker's cycle line, not in any root tool. Every argument parser at the
repository root was read; not one of them reports a fee.

So the fee was being discussed without ever having been read off a screen, and
the answer turned out not to be 150bps. fee_ledger.py's module docstring carries
the derivation; the short version is that the payout is priced on what a swap
EXPECTED to receive while the fee is charged against what it ACTUALLY received,
and those may differ by AMOUNT_TOLERANCE_PCT. At the shipped 1% band a 150bps
schedule realizes anywhere from about 52 to about 249bps.

WHAT IT REIMPLEMENTS: NOTHING.

    the fee derivation     fee_ledger.FEE_LEDGER_SQL -- one SELECT, and the only
                           place the arithmetic exists (rules 5 and 20)
    the per-asset totals   fee_ledger.asset_totals()
    the printed column     report_block.labeled(), shared with open_swap.py and
                           show_swap.py
    how a tool names
    itself in a command    workers/common.root_tool_command()
    every duration         microfortnights.format_duration() (rule 6)

There is no SQL in this file and no arithmetic about money in it. The one thing
it owns is which facts go on the screen and what sentence stands beside each
number.
"""

from __future__ import annotations

import argparse
import shlex
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "swap_terminal"))

from config import Config
from db import connect_db
from fee_ledger import DRIFT_IS_ZERO_COIN, FeeRow, asset_totals, fee_rows
from microfortnights import format_duration
from report_block import CONTINUATION, labeled
from workers.common import db_path_source, get_config_dict, root_tool_command


class FeesRefused(RuntimeError):
    """This run cannot report what was asked, and nothing was written.

    Its own type, and ONE place prints it (main()), for the reason
    show_swap.ShowRefused gives: a refusal is a sentence the operator has to act
    on, and a traceback buries it under a stack they cannot act on.
    """


SELF = "show_fees.py"

#: Below this, a CHARGED reserve is shown as "<0.1" rather than rounded to 0.0.
#:
#: One decimal is right for a figure that is usually 1-2bps, and wrong for the one
#: case where the reserve is tiny: a flat 0.01 GRC against a 3133 GRC gross is
#: 0.032bps, which prints as 0.0. The row WAS charged it, and a zero says it was
#: not. Half the smallest displayable value, so anything that would round to 0.0
#: takes the "<0.1" form instead.
RESERVE_SHOWS_AT_BPS = 0.05



def self_command(db_path: str) -> str:
    """The command that runs this tool again, with the database already in it.

    --db IS PART OF "pastes from anywhere", not an extra. show_swap.py and
    open_swap.py both had the identical defect and both were fixed on
    2026-09-26: a printed command with no --db answers about Config.DB_PATH, so
    after a `--db <somewhere>` run the ready-made line silently reports on a
    different database. Empty when the caller was not given one, so the common
    command stays short. shlex.quote because a path may contain a space.
    """
    if db_path and db_path != str(Config.DB_PATH):
        return root_tool_command(SELF, "--db", shlex.quote(db_path))
    return root_tool_command(SELF)


def header_lines(db_path: str, config: dict, explicit_db: str = "") -> list[str]:
    """What this run is about to read, printed BEFORE it reads it (rule 14).

    The three parameters below decide every number underneath them, and a pasted
    block is usually read a day later -- so the schedule and the tolerance are
    echoed rather than left to be looked up. The tolerance is here specifically
    because it is the setting that makes the realized fee differ from the
    schedule, and a reader who does not know its value cannot interpret a single
    `drift` figure in the report.
    """
    tolerance = float(config["AMOUNT_TOLERANCE_PCT"])
    scheduled = float(config["DEFAULT_FEE_BPS"])
    return [
        "swap fees -- READ-ONLY. It changes no fee, collects nothing, writes no row and creates no file.",
        labeled("database", f"{db_path}  <- {db_path_source(db_path, explicit_db)}. Fees earned in any "
                            f"other database are invisible to this run"),
        labeled("schedule now", f"DEFAULT_FEE_BPS={scheduled:.0f}bps  <- what a swap created RIGHT NOW would "
                                f"be quoted. Each row below carries the fee_bps IT was quoted, which may "
                                f"differ"),
        labeled("tolerance", f"AMOUNT_TOLERANCE_PCT={tolerance} -> {tolerance * 100:.2f}% either side of "
                             f"expected_input_amount is ACCEPTED and paid out at the expected-amount "
                             f"estimate. That band is why realized != scheduled below"),
        labeled("counted", "a payout row with status='broadcast' AND a txid. A payout still 'created', or "
                           "'broadcast' with no txid, is money whose delivery is NOT established and is "
                           "excluded -- see `unresolved payouts` on the admin page"),
    ]


def total_lines(totals: list, rows: list[FeeRow], db_path: str = "") -> list[str]:
    """The per-asset answer, and the answer when there is none.

    "(none)" is a result and a blank gap is not (rule 14). The empty case says
    what the absence MEANS -- no payout has been delivered from THIS database --
    because "no fee was earned" and "zero was charged" are different facts and a
    blank line is ambiguous between them.
    """
    if not totals:
        return [
            "",
            "fees earned: (none)  <- no payout in THIS database has been broadcast with a txid, so no fee "
            "has been realized. That is NOT the same as a zero fee: nothing has been delivered to charge "
            "one on.",
            CONTINUATION + f"If a swap you expected is missing, check the database path above, and run "
                           f"{root_tool_command('show_swap.py')} to see what state it is actually in. This "
                           f"same report again: {self_command(db_path)}",
        ]

    lines = [
        "",
        f"fees earned, per destination asset  <- NEVER pooled across assets: adding coins of different "
        f"value and calling the result revenue describes neither (rule 11's shape). {len(rows)} delivered "
        f"payout(s) in total.",
    ]
    for total in totals:
        lines.extend([
            "",
            f"{total.asset}   {total.swaps} delivered payout(s)",
            labeled("received", f"{total.gross:.8f} {total.asset}  <- the realized gross: what each deposit "
                                f"was worth at the rate that swap was quoted"),
            # THE SENTENCE HERE CARRIED THE RETRACTED CLAIM, and the operator's
            # 2026-10-02 run showed it still saying "and then spent on the chain's
            # own fee". It was not spent on the chain's fee -- that is the whole
            # finding, measured from their own reconciliation -- and the reserve is
            # now part of this total for only SOME rows. I updated the per-row
            # display and added the `reserve charged` line below, and left this
            # sentence asserting the thing both of those exist to correct.
            labeled("retained", f"{total.retained:.8f} {total.asset}  <- received minus paid out: the fee, "
                                f"plus the flat reserve on whichever rows predate 2026-10-02. That reserve "
                                f"was never spent on the chain's fee -- the wallet pays that separately -- "
                                f"so on those rows it was margin. See `reserve charged` below"),
            labeled("realized fee", f"{total.weighted_bps:.1f}bps  <- retained over received, weighted by "
                                    f"size across {total.swaps} payout(s). NOT the mean of the per-swap "
                                    f"figures: the mean treats a dust swap as equal evidence to a large one"),
            labeled("scheduled fee", f"{total.scheduled_bps:.1f}bps  <- the fee those same swaps were "
                                     f"QUOTED, weighted the same way, so the two lines are comparable"),
            labeled("drift, net", f"{total.drift:+.8f} {total.asset}  <- the whole difference between "
                                  f"realized and scheduled. Positive means customers sent MORE than they "
                                  f"were quoted and were paid the quoted amount anyway; negative means they "
                                  f"sent less and were paid in full"),
            labeled("reserve charged", f"{total.reserve_charged_total:.8f} {total.asset} withheld from "
                                       f"{total.reserve_charged_swaps} of {total.swaps} payout(s)  <- the "
                                       f"flat network-fee reserve, SUBTRACTED from what those customers "
                                       f"received. Stopped on 2026-10-02: it funded nothing (sendtoaddress "
                                       f"delivers the full amount and the wallet pays the fee separately) "
                                       f"and being flat it cost a small swap sixty times what it cost a "
                                       f"large one. Rows priced after that show 0 here"),
            labeled("drift, gross", f"{total.drift_abs:.8f} {total.asset} across {total.drifted} of "
                                    f"{total.swaps} payout(s)  <- READ THIS BESIDE THE NET, NOT INSTEAD OF "
                                    f"IT. Overpaid and underpaid swaps cancel in the net, so a net near zero "
                                    f"means the errors offset -- NOT that nobody was charged the wrong fee. "
                                    f"This line is how many were and by how much in total"),
        ])
        if total.drifted:
            lines.append(
                CONTINUATION + f"{total.drifted} of {total.swaps} payout(s) were NOT charged the fee they "
                               f"were quoted. Per-swap figures below."
            )
        if total.unreconciled:
            lines.append(
                CONTINUATION + f"{total.unreconciled} of these {total.swaps} payout(s) do NOT reconcile -- "
                               f"the payout was clamped to zero by max(...,0) in create_quote(), so the "
                               f"whole gross was retained and the figures above include it. Marked per row "
                               f"below."
            )
    return lines


def swap_lines(rows: list[FeeRow]) -> list[str]:
    """One line per delivered payout, oldest first, with the drift called out.

    A per-swap line and not just a total, because the total hides the thing worth
    seeing: two swaps can average to the schedule while neither one was charged
    it. The drift column is the per-customer version of that.
    """
    if not rows:
        return []
    lines = [
        "",
        f"every delivered payout, oldest first  <- {len(rows)} row(s). `realized` is what THIS customer was "
        f"charged; `quoted` is what they were told.",
    ]
    for row in rows:
        if abs(row.drift_coin) < DRIFT_IS_ZERO_COIN:
            mismatch = "deposit matched the quote exactly"
        elif row.expected_input > 0:
            mismatch = f"deposit was {row.actual_input / row.expected_input:.4f}x the quoted amount"
        else:
            # Not reachable from create_quote(), which raises for input_amount <= 0
            # -- established 2026-10-01 by reading it, not assumed. It is handled
            # anyway because the alternative is a ZeroDivisionError traceback in a
            # money report, which is rule 14's failure in its worst form: the
            # operator loses the whole block, including the rows that were fine.
            mismatch = (f"deposit was {row.actual_input} against an expected amount of "
                        f"{row.expected_input}, so no ratio can be computed")
        # THE DECOMPOSITION IS PRINTED ONLY WHERE IT ADDS UP. On a clamped row the
        # first run of this report printed `10000.0bps = 150 quoted + 20000.0
        # reserve +0.0 drift` -- three correct terms and a sum that is wrong by a
        # factor of two, because the identity does not hold when max(...,0) fires.
        # The DOES NOT RECONCILE line below said so, and that is not enough: a
        # reader scanning a column of sums should never meet one that does not add,
        # whatever a later line admits. So such a row shows the realized figure
        # alone and the reason underneath it.
        # THE RESERVE TERM APPEARS ONLY WHERE IT WAS ACTUALLY CHARGED. Printing
        # "+0.0 reserve" on a row priced without one would imply the reserve is
        # still part of the fee and merely rounded away, which is the opposite of
        # what changed on 2026-10-02.
        # A CHARGED RESERVE MUST NEVER PRINT AS ZERO. On the operator's 2026-10-02
        # run s_e820c23626002c37 read "+ 0.0 reserve" -- its 0.032bps rounding away
        # at one decimal -- directly above a row with NO reserve term at all. The
        # two states are "charged a hundredth of a basis point" and "not charged
        # anything", and `+ 0.0 reserve` beside `+0.0 drift` makes them look the
        # same. The PRESENCE of the term is the signal, and a zero undermines it.
        reserve_term = ""
        if row.reserve_charged:
            shown = f"{row.reserve_bps:.1f}" if row.reserve_bps >= RESERVE_SHOWS_AT_BPS else "<0.1"
            reserve_term = f"+ {shown} reserve "
        realized = (
            f"{row.retained_bps:.1f}bps  = {row.fee_bps:.0f} quoted {reserve_term}"
            f"{row.drift_bps:+.1f} drift  <- {mismatch}"
            if row.reconciles
            else f"{row.retained_bps:.1f}bps  <- NOT a fee rate; see the note below this row"
        )
        lines.extend([
            "",
            f"{row.swap_id}   {row.from_asset} -> {row.to_asset}   sent {row.sent_at or '(not recorded)'}",
            labeled("realized", realized),
            labeled("retained", f"{row.retained:.8f} {row.to_asset} of {row.realized_gross:.8f} received; "
                                f"{row.paid:.8f} paid out"),
            labeled("txid", f"{row.payout_txid}  <- final; a payout is final the moment it is broadcast"),
        ])
        if not row.reconciles:
            lines.append(
                CONTINUATION + f"DOES NOT RECONCILE: {row.residual_bps:+.1f}bps unexplained. The payout was "
                               f"{row.paid:.8f}, which means create_quote()'s max(...,0) clamped it -- the "
                               f"gross was smaller than the {row.reserve:.8f} {row.to_asset} network fee "
                               f"reserve. The whole deposit was retained and no fee RATE describes this row."
            )
    return lines


def closing_lines(totals: list, config: dict, db_path: str = "") -> list[str]:
    """What the numbers imply, and why this tool does none of it (rule 16).

    Printed only when there is something to say about. It names no command that
    changes a fee, a payout or a rate, deliberately: every one of those moves
    money, and a ready-made setting to paste would be pasted at exactly the
    moment somebody is comparing their fee to Binance's and feeling slow.

    Indented four, not two: two spaces plus a word is the shape of a label row,
    and prose must not be read as one.
    """
    if not totals:
        return []
    tolerance = float(config["AMOUNT_TOLERANCE_PCT"])
    return [
        "",
        "WHAT THESE NUMBERS SAY, and where this tool stops.",
        f"    The realized fee is not the scheduled one, and the gap is structural rather than a rounding "
        f"artifact. services/quote_service.create_quote() prices the payout on expected_input_amount; "
        f"services/deposit_service.py credits the swap on the CONFIRMED total and never recomputes that "
        f"payout. Any deposit within {tolerance * 100:.2f}% of the quote is accepted and paid at the quoted "
        f"figure, so the customer's rounding decides which end of the band they land on.",
        "    The flat network-fee reserve was the OTHER source of that gap and is gone as of 2026-10-02: "
        "it was withheld from every payout while funding nothing, and being a flat amount against a "
        "percentage fee it cost a small swap sixty times what it cost a large one. It is now a cost the "
        "desk carries out of its own fee, and create_swap() refuses a swap whose fee would not cover it. "
        "Rows above marked with a reserve term predate that and are history, not current pricing.",
        "    Three separate decisions follow from that and all three are the operator's, because each one "
        "changes what a customer is paid or what a desk charges: whether to recompute the payout from the "
        "amount actually received, what the schedule should be, and where the retained fee should go. "
        "Nothing here does any of them.",
        f"    This is a report and it ends. There is no --set, no --collect and no --sweep; nothing above is "
        f"a command that writes, and the only command this file prints is another run of itself: "
        f"{self_command(db_path)}",
    ]


def read_report(db_path: str, config: dict) -> list[str]:
    """Open the database, derive the ledger, build the report.

    Path.exists() BEFORE connecting, on purpose: sqlite3.connect() CREATES a
    missing file, so a tool that says it writes nothing must not reach connect()
    for a path that is not there.
    """
    if not Path(db_path).exists():
        raise FeesRefused(
            f"there is no database at {db_path}, so there are no fees to report. That is not "
            f"'no fees were earned': the file has never been created. The workers and the web app create "
            f"it on first run. Nothing was written, including that file."
        )
    connection = connect_db(db_path)
    try:
        rows = fee_rows(connection)
    except sqlite3.OperationalError as error:
        # NAMED, not broad. This is what a database file with no `swaps` or
        # `payouts` table raises, which is an ordinary state for a file created
        # but never initialized -- and it must NEVER read as "no fee was earned",
        # which is the answer a healthy empty table gives. Those two must not
        # share a line.
        raise FeesRefused(
            f"{db_path} exists but could not be queried ({error}). Until that is fixed this run cannot tell "
            f"an empty database from a missing table, so it is reporting neither. Nothing was written."
        ) from error
    finally:
        connection.close()
    totals = asset_totals(rows)
    return (total_lines(totals, rows, db_path) + swap_lines(rows)
            + closing_lines(totals, config, db_path))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Report the fee this terminal actually charged, per swap and per asset. Read-only.",
        epilog=(
            "It counts only payouts whose delivery is established -- status 'broadcast' with a txid. "
            "It changes no fee and collects nothing: the rate, the payout arithmetic and where the fee goes "
            "are fund-moving decisions and belong to the operator."
        ),
    )
    parser.add_argument(
        "--db", default="",
        help=f"database to read (default: Config.DB_PATH, currently {Config.DB_PATH}). It must be the same "
             f"file the workers write, or this tool is answering about something else.",
    )
    return parser


def run(args) -> int:
    """Orchestration only: announce, read, print (rule 10). Every decision is above."""
    started = time.monotonic()
    db_path = args.db or Config.DB_PATH
    config = get_config_dict()

    for line in header_lines(str(db_path), config, args.db):
        print(line, flush=True)
    for line in read_report(str(db_path), config):
        print(line, flush=True)

    print(flush=True)
    print(labeled("read in", f"{format_duration(time.monotonic() - started)}  <- nothing was written"),
          flush=True)
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return run(args)
    except FeesRefused as refusal:
        print(f"\nREFUSED: {refusal}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
