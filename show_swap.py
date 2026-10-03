#!/usr/bin/env python3
"""Show the swaps waiting on a person, and any one swap in full. Read-only.

Role: file (entry point, at the repository root per CLAUDE.md rule 10)
Reads: config.Config (DB_PATH, AMOUNT_TOLERANCE_PCT) and swap_terminal.db --
       the `swaps`, `deposit_events` and `payouts` tables, through
       services/admin_view.py and services/swap_service.get_swap(). It opens no
       socket: no chain, no daemon, no price feed, nothing over the network.
Writes: NOTHING. No INSERT, no UPDATE, no DELETE, no schema, and not even the
       database file -- the path is checked with Path.exists() before
       sqlite3.connect(), because connect() CREATES a missing file and a
       read-only tool that leaves an empty database behind has written
       something while announcing that it would not.
Can move funds: no. It has no --resolve, no --credit and no --refund, it
       constructs no adapter, it imports nothing that can sign, and there is no
       code path here that changes a swap's status. Resolving a halted swap is
       a decision about money and belongs to the operator (rule 16); this tool
       exists to put the evidence for that decision on the screen.
Mainnet-safe: yes. It chooses no network and reaches none. It reads whatever
       database config.Config names and says which one on its first line.

WHY THIS FILE EXISTS, MEASURED 2026-09-26.

The deposit watcher's cycle line carries a halted count, added the day before
this file, and it was doing its job -- the halt used to be invisible:

    deposit_watcher cycle=4 WORKED active_swaps=0 refreshed=0 now_payout_pending=0
    HALTED_for_review=1 in 0.0µfn (0.0s)  <- ... HALTED_for_review>0 means a swap is
    waiting on a PERSON and will never resolve by itself -- query swaps WHERE
    status='under_review'

The note ended by naming a SQL FRAGMENT to an operator sitting in a shell with
nothing to run it in. So the instrument reported a problem and handed over half
a query: rule 14's "the operator reads the screen, not the source", failed at
the last step, because a hint nobody can act on is silence one step removed.

AND THERE WAS NOWHERE ELSE TO LOOK, WHICH IS WHY THIS IS A NEW FILE RATHER THAN
A FIXED SENTENCE. Established before writing any of it, by running the real code
against a seeded `under_review` swap rather than by reading it (rule 17):

  services/admin_view.overview()   status_counts() reported `under_review: 1`,
                                   and swaps_in_flight() returned NOTHING -- the
                                   halt is correctly not "in flight", since
                                   IN_FLIGHT_STATUSES is derived from the rail
                                   and a halt is a departure from the rail. The
                                   swap's own id reached the page only through
                                   the recent-deposits table, and the string in
                                   `swaps.failed_reason` -- the sentence saying
                                   WHICH amounts disagreed -- appeared nowhere
                                   in the whole result.
  /swap/<id>                       shows the halt properly, headline, reason and
                                   all. It needs the id first, and it is a web
                                   page.
  the repository root              open_swap.py CREATES a swap; swap_readiness.py
                                   is a config preflight; xrp_send_tagged.py and
                                   xrp_payout_verify.py send; migrate_deposit_
                                   vouts.py reports on one specific artifact.
                                   Read every argument parser at the root: not
                                   one of them lists a swap.

So the answer existed in the database and in the display functions, and there
was no way to reach it from a terminal.

WHAT IT REIMPLEMENTS: NOTHING, AND THAT IS THE LOAD-BEARING PART.

    which swaps are halted    services/admin_view.halted_swaps(), which is
                              services/admin_view.swaps_with_status() -- the
                              same SELECT, the same columns and the same
                              attention() verdict the admin page's in-flight
                              table uses, over a different status set
    what "halted" means       services/swap_view.HALTED_STATUSES, derived from
                              STATUS_MEANINGS, so this tool and the watcher's
                              counter cannot come to name different sets
    one swap's state          services/swap_service.get_swap() and
                              services/swap_view.swap_display() -- the identical
                              call the /swap/<id> page makes, so this terminal
                              and that page cannot disagree about a swap
    every duration            microfortnights.format_duration() (rule 6)
    the printed column        report_block.labeled(), shared with open_swap.py

There is no SQL in this file. There is no status vocabulary in this file. The
one thing it owns is the choice of WHICH facts to put on a terminal screen, and
those are the functions below, each callable with seeded dictionaries.

WHAT IT DELIBERATELY DOES NOT PRINT.

Not one command that would change a row. The obvious "helpful" ending for a
report like this is a ready-made `UPDATE swaps SET status=...` for the operator
to paste, and that is precisely what must not be here: it moves money, in a
direction this tool has no way to know is right, and it would be pasted at the
moment somebody is tired and staring at a stuck swap. Every command this file
prints is another run of this same read-only file.
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
from deposit_vout_artifact import multi_vout_groups
from microfortnights import format_duration
from report_block import CONTINUATION, labeled
from services.admin_view import halted_swaps, status_counts
from services.helpers import utc_now_iso
from services.swap_service import get_swap
from services.swap_view import HALTED_STATUSES, elapsed_seconds, swap_display
from workers.common import db_path_source, get_config_dict, root_tool_command


class ShowRefused(RuntimeError):
    """This run cannot show what was asked, and nothing was written.

    Its own type, and ONE place prints it (main()), for the reason
    open_swap.SwapRefused gives: a refusal is a sentence the operator has to
    act on, and a traceback buries it under a stack they cannot act on.
    """


# HOW THIS TOOL NAMES ITSELF when it prints a follow-up command.
#
# Through the same workers/common.root_tool_command() the deposit watcher uses,
# so there is one spelling of "how you run a tool at the root" rather than two
# that agree today. Absolute, for the reason that function's docstring measures:
# the watcher prints from cwd=swap_terminal/ and the operator's shell may be
# anywhere, and a command that only works from one directory is a command that
# fails silently for whoever is not standing in it.
SELF = "show_swap.py"


def detail_command(swap_id: str, db_path: str = "") -> str:
    """The command that shows ONE swap, with the real id already in it.

    No placeholder, ever. A `<swap id>` in a printed command has cost this
    project three mis-runs and twice put something into a shell that should
    never have been there -- once a wallet passphrase -- which is why
    open_swap.next_command() has its own test for the same property and why this
    has one too: a literal `<` in what this returns is a failure.
    """
    # --db IS PART OF "pastes from anywhere", not an extra. Flagged by two reviewers
    # 2026-09-26: this printed `show_swap.py --swap <id>` with no --db, so after a
    # `--db <somewhere>` run the ready-made command answered about Config.DB_PATH
    # instead -- a different database, silently, from a line whose only job is to be
    # copied. open_swap.py's apply_command() and next_command() had the identical
    # defect and were fixed in the same pass.
    #
    # Empty when the caller was not given one, so the common command stays short.
    # shlex.quote because a database path may contain a space.
    if db_path:
        return root_tool_command(SELF, "--swap", swap_id, "--db", shlex.quote(db_path))
    return root_tool_command(SELF, "--swap", swap_id)


def amount_text(value, asset: str, absent: str) -> str:
    """An amount with its unit, or a stated reason there is no number.

    Rule 14: never let an empty result print nothing, and say what the absence
    MEANS. `None` in these columns is not zero -- `actual_input_amount` is NULL
    until a deposit is seen at all, and printing `0.0 XRP` for it would tell the
    reader a payment of nothing arrived.

    The value is printed exactly as the row holds it, not reformatted: a number
    reprinted in a different shape than the database holds it is how a reader
    concludes two figures differ when they do not.
    """
    if value is None:
        return absent
    return f"{value} {asset}"


def header_lines(db_path: str, config: dict, explicit_db: str = "") -> list[str]:
    """What this run is about to read, printed BEFORE it reads it (rule 14).

    Announce before, not only after: the database path is the parameter that
    decides every answer below it, and it is the first thing to check when the
    watcher says HALTED_for_review=1 and this tool says (none).
    """
    tolerance = float(config["AMOUNT_TOLERANCE_PCT"])
    return [
        "show swap -- READ-ONLY. It changes no status, resolves nothing, writes no row and creates no file.",
        labeled("database", f"{db_path}  <- {db_path_source(db_path, explicit_db)}. A swap in any other "
                            f"database is invisible to both the workers and this"),
        labeled("halted means", f"{', '.join(HALTED_STATUSES)}  <- exactly what deposit_watcher's "
                                f"HALTED_for_review counts, from services/swap_view.HALTED_STATUSES"),
        labeled("tolerance", f"AMOUNT_TOLERANCE_PCT={tolerance} -> {tolerance * 100:.2f}% either side of "
                             f"expected_input_amount. A CONFIRMED deposit outside that band is what halts a "
                             f"swap instead of paying it out"),
    ]


def halted_swap_block(row: dict, now_iso: str, db_path: str = "") -> list[str]:
    """One halted swap, as many lines as it takes to answer "why, and what now".

    Every figure comes from the row admin_view.halted_swaps() returned, so this
    cannot disagree with the database: there is no arithmetic here at all. The
    one thing it adds is the SENTENCE beside each number saying what the number
    means, because the operator reads the screen and a pasted block is usually
    read a day later.
    """
    waited = elapsed_seconds(row.get("updated_at"), now_iso)
    since = row.get("updated_at") or "(no updated_at recorded)"
    age = "(not measurable -- updated_at is missing or unreadable)" if waited is None else format_duration(waited)
    asset = row.get("from_asset") or "?"
    confirmations = row.get("max_confirmations")

    # SEEN IS NOT THE FIGURE THE GATE COMPARED, and saying so is the single most
    # useful line in this block. services/deposit_service.refresh_swap_from_chain()
    # writes `actual_input_amount` from seen_total -- every deposit row, confirmed
    # or not -- and then compares confirmed_total, which counts only the rows at or
    # past min_confirmations. The two are equal often enough that a reader who is
    # not told will assume they always are, and then read a halt as arithmetic that
    # does not add up.
    reason = row.get("failed_reason") or (
        "(none) -- swaps.failed_reason is empty, so NOTHING recorded why this swap halted. The status was "
        "set without the reason the tolerance check writes; check swap_audit_log for this swap id"
    )
    # The heading starts at column 0 and its fields are labeled at column 2, the
    # same column open_swap.py's blocks use. Indenting the heading too would put a
    # swap id where a label goes, and the column is the thing that makes a pasted
    # block scannable -- see report_block.labeled().
    return [
        "",
        f"{row['id']}   {row.get('from_asset')} -> {row.get('to_asset')}   [{row.get('status')}]",
        labeled("reason", f"{reason}"),
        labeled("halted since", f"{since}  ({age} ago)  <- swaps.updated_at, the moment the status changed"),
        labeled("expected", f"{amount_text(row.get('expected_input_amount'), asset, '(none)')}  <- "
                            f"swaps.expected_input_amount, the figure the quote was priced on"),
        labeled("seen", f"{amount_text(row.get('actual_input_amount'), asset, '(none) -- no deposit row has been seen at all')}"
                        f" across {row.get('deposit_rows')} deposit row(s)  <- swaps.actual_input_amount is the SEEN "
                        f"total; the gate compared the CONFIRMED total, which is the number in the reason above"),
        labeled("confirmations", f"{'(none) -- no deposit rows' if confirmations is None else confirmations}"
                                 f" of {row.get('min_confirmations')} required  <- a COUNT of confirmations, "
                                 f"never a duration (rule 6)"),
        labeled("payout", f"{amount_text(row.get('output_amount_estimate'), row.get('to_asset') or '?', '(none)')}"
                          f" was the estimate; NOTHING has been broadcast. No worker advances a swap in this "
                          f"status -- it is outside deposit_service.ACTIVE_STATUSES and payout_service only "
                          f"claims payout_pending"),
        labeled("full detail", f"{detail_command(row['id'], db_path)}"),
    ]


def halted_lines(rows: list[dict], now_iso: str, counts: list[dict], db_path: str = "") -> list[str]:
    """The whole halted section, including the answer when there are none.

    "(none)" is a result and a blank gap is not (rule 14) -- but an empty
    section here has a second job. If the deposit watcher says
    HALTED_for_review=1 and this says none, the two are reading DIFFERENT
    DATABASES, and that is the one conclusion an operator cannot reach from a
    blank space. So the empty case says it, and prints the status tally so the
    reader can see whether this database has any swaps in it at all.
    """
    if rows:
        lines = [
            "",
            f"halted swaps: {len(rows)}  <- each is waiting on a PERSON and will not resolve by itself. "
            f"Oldest first.",
        ]
        for row in rows:
            lines.extend(halted_swap_block(row, now_iso, db_path))
        return lines

    tally = ", ".join(f"{row['status']} {row['swaps']}" for row in counts) or "(no swaps at all in this database)"
    return [
        "",
        "halted swaps: (none)  <- no swap in THIS database is waiting on a person.",
        CONTINUATION + f"every status here: {tally}",
        CONTINUATION + "If deposit_watcher's cycle line says HALTED_for_review is above zero while this says "
                       "(none), the two are reading different files: compare the database path above against "
                       "SWAP_DB_PATH in the environment that worker was started with.",
    ]


def what_the_deposit_rows_are(events) -> str:
    """"every payment" -- EXCEPT WHEN TWO ROWS ARE ONE PAYMENT, WHICH IS NOT RARE.

    MEASURED ON THE OPERATOR'S HOST 2026-10-03, the first BTC -> GRC swap to
    settle. The screen said

        deposit rows   2  <- every payment attributed to this swap, confirmed or not
                       0.0003 BTC  0 confirmation(s)  NOT counted  ... vout 1
                       0.0003 BTC  2 confirmation(s)  COUNTED      ... vout 0

    and `bitcoin-cli gettransaction` settled what the screen could not: there was
    ONE payment, of 0.0003, at vout 1. Both `details` entries carry vout 1 -- the
    send and the receive halves of a payment into our own wallet -- and
    find_deposits_to_address() drops the send, so the duplicate is not those two
    halves.

    THE ROW THAT RELEASED THE PAYOUT RECORDS A VOUT THE TRANSACTION DOES NOT HAVE,
    and that is the measurement. The counted row is the one at vout 0, which pays
    no output on this transaction; the row carrying the chain's true vout 1 sat at
    0 confirmations and was never counted. upsert_deposit_event() keys on
    (asset, txid, vout), so the two never collide and both persist.

    WHICH BRANCH WROTE THE COUNTED ROW IS NOT ESTABLISHED FROM HERE, and saying so
    is the point (rule 17). chains/base.py's FABRICATED branch -- the one its own
    comment heads "PROPOSAL MARKER, NOT AN ENDORSEMENT" -- emits vout 0 with the
    amount the WALLET SUMMARY reported and a confirmation count from a second RPC.
    But the real branch also emits 0, because it reads `int(vout.get("n", 0))`, so
    a decode missing `n` produces vout 0 with the OUTPUT's value. The two are
    indistinguishable in the row, which is exactly the property the proposal marker
    is about: services/deposit_service.py cannot tell a fabricated event from a
    real one.

    THE PAYOUT WAS CORRECT FOR A REASON THAT DOES NOT GENERALIZE. Both candidate
    branches put 0.0003 in that row -- the wallet summary and the output agree for
    a simple one-recipient payment -- so confirmed_total was 0.0003 and the payout
    equaled the quote. They stop agreeing as soon as a transaction pays the address
    more than once, or pays it alongside anything the summary nets against.
    seen_total sums EVERY row and was 0.0006 until the credit overwrote the column;
    had both rows confirmed, confirmed_total would be 0.0006 and
    payout_service.payout_amount() scales the quote by actual/expected -- a 2x
    payout. chains/base.py:411 anticipated that double-count and expected it to
    halt the swap at `under_review`. It did not halt, because only one row ever
    confirmed.

    AN EARLIER VERSION OF THIS DOCSTRING HAD THE TWO ROWS THE OTHER WAY ROUND,
    claiming the decoded vout-1 row was the counted one and that a frozen
    fabricated row was harmless. It is kept in git rather than quietly replaced
    (rule 1: the drift is the point): the inverted reading made the finding sound
    like a cosmetic duplicate, when what the rows actually say is that a real
    payout was released by a row whose vout does not exist on the transaction.

    SO THE COUNT GETS A SENTENCE INSTEAD OF A CLAIM. The condition is already
    detected -- deposit_service.refresh_swap_from_chain() calls
    warn_on_multi_vout_rows() on these same rows every cycle -- and it went only to
    a log the operator does not read while the screen asserted the opposite. This
    is the same authority (deposit_vout_artifact.multi_vout_groups), read rather
    than restated, so the two cannot disagree about what counts as multi-vout.

    NOTHING ABOUT THE CREDIT CHANGES HERE. Which rows are written, which are
    counted, and whether the fabricated branch should raise instead are all the
    credit path, and they are the operator's (rule 16). What this fixes is a line
    that told them two rows were two payments.
    """
    count = len(events)
    groups = multi_vout_groups(events)
    if not groups:
        return "every payment attributed to this swap, confirmed or not"
    rows_in_groups = sum(len(rows) for rows in groups.values())
    return (f"{count} ROWS, NOT {count} PAYMENTS -- {len(groups)} transaction(s) carry {rows_in_groups} rows "
            f"between them (same txid, different vout). At most one vout per transaction can be the real "
            f"output: check it with `gettransaction <txid> true` before trusting a row's vout. chains/"
            f"base.py's FABRICATED branch writes vout 0 with the amount the WALLET SUMMARY reported, and "
            f"its real branch also writes 0 when the decode omits `n`, so the row cannot say which it is "
            f"(see its PROPOSAL MARKER). `seen` sums every row; the credit counts only those at or above "
            f"min_confirmations")


def deposit_event_lines(events: list[dict], min_confirmations: int) -> list[str]:
    """Every deposit row for one swap, with confirmed/not marked per row.

    This is the evidence behind a halt: which payments arrived, how much each
    was, and which of them the gate counted. A row below min_confirmations is
    in `seen` and not in `confirmed`, and that is exactly the distinction that
    makes a halt look like bad arithmetic when it is not, so it is marked per
    row rather than left for the reader to compare two columns.
    """
    if not events:
        return [CONTINUATION + "(none) -- no deposit has been attributed to this swap yet"]
    lines = []
    for event in events:
        confirmations = int(event.get("confirmations") or 0)
        counted = "COUNTED by the gate" if confirmations >= min_confirmations else (
            f"NOT counted -- below min_confirmations={min_confirmations}"
        )
        # credited_at IS THE SWAP'S CREDIT, NOT THIS ROW'S, AND THE LINE HAS TO SAY SO
        # WHEN THE TWO DISAGREE.
        #
        # services/deposit_service._credit_confirmed_deposit() stamps it with
        # `UPDATE deposit_events SET credited_at = ? WHERE swap_id = ? AND credited_at
        # IS NULL` -- every row of the swap, including rows the gate did NOT count. So
        # on the operator's screen 2026-10-03 one line read
        #
        #     0.0003 BTC  0 confirmation(s)  NOT counted -- below min_confirmations=2
        #                 ... credited_at 2026-10-03T15:15:48.191703+00:00
        #
        # which is "not counted" and "credited" in one row, nine words apart. Rule 13's
        # "'skipped' plus 'success' in the same output is a defect in the OUTPUT",
        # arriving inside a single line instead of across two.
        #
        # The timestamp is not wrong and the write is not changed here -- that is the
        # credit path, and it is the operator's (rule 16). What was wrong is a column
        # name that invites one reading when the row says the other.
        credited = event.get("credited_at") or "(not credited)"
        if event.get("credited_at") and confirmations < min_confirmations:
            credited += "  <- the SWAP's credit timestamp, stamped on every row; this row was NOT counted"
        lines.append(
            CONTINUATION + f"{event.get('amount')} {event.get('asset')}  {confirmations} confirmation(s)  "
            f"{counted}  txid {event.get('txid')} vout {event.get('vout')}  credited_at {credited}"
        )
    return lines


def payout_lines(payouts: list[dict]) -> list[str]:
    """Every payout row for one swap. "(none)" is the normal answer for a halt."""
    if not payouts:
        return [CONTINUATION + "(none) -- no payout row exists, so nothing has been broadcast for this swap"]
    return [
        CONTINUATION + f"{row.get('amount')} {row.get('asset')} to {row.get('destination_address')}  "
        f"status {row.get('status')}  txid {row.get('txid') or '(none)'}  created {row.get('created_at')}  "
        f"sent {row.get('sent_at') or '(not recorded as sent)'}"
        for row in payouts
    ]


def what_the_seen_total_counts(swap: dict) -> str:
    """Which sum swaps.actual_input_amount is holding right now. It is TWO different sums.

    THE LABEL WAS WRONG, AND IT WAS WRONG ABOUT THE FIGURE THE PAYOUT IS SCALED
    FROM. It read, unconditionally:

        swaps.actual_input_amount, the SEEN total over every deposit row

    Measured on the operator's screen 2026-10-03, on the first BTC -> GRC swap to
    settle:

        seen           0.0003 BTC
        deposit rows   2
                       0.0003 BTC  0 confirmation(s)  NOT counted
                       0.0003 BTC  2 confirmation(s)  COUNTED by the gate

    Two rows of 0.0003 under a label that says it sums every row, showing 0.0003.
    A reader checking the arithmetic finds it does not add up and has no way to
    learn why from the screen.

    BOTH SUMS ARE REAL AND THE COLUMN HOLDS WHICHEVER RAN LAST.
    services/deposit_service.refresh_swap_from_chain() writes `seen_total` -- every
    row, confirmed or not -- and then _credit_confirmed_deposit() OVERWRITES it
    with `confirmed_total`, the rows at or above min_confirmations. So before the
    credit the old label was right, and from the credit onward it was wrong.

    WHY THIS MATTERS BEYOND TIDINESS: services/payout_service.payout_amount()
    scales the quote by actual_input_amount / expected_input_amount. The figure on
    this line is a multiplier on what gets broadcast, so a reader who believes it
    counts unconfirmed rows believes the payout tracks money that has not
    confirmed. On this swap the two sums happened to agree on the counted row, and
    the payout was exactly the quote.
    """
    return ("the CONFIRMED total -- rows at or above min_confirmations, which is what "
            "_credit_confirmed_deposit() wrote at the credit and what the payout is scaled by"
            if swap.get("credited_at")
            else "the SEEN total over every deposit row, counted or not -- it becomes the CONFIRMED "
                 "total when the swap is credited")


def swap_lines(view: dict, now_iso: str) -> list[str]:
    """One swap in full, from the dict services/swap_view.swap_display() returns.

    The SAME dict /swap/<id> renders, so this terminal block and that web page
    cannot disagree about a swap's headline, its confirmation progress or its
    quote window. Nothing is re-derived here; the fields are chosen and given a
    column.
    """
    swap = view["swap"]
    attention = view["attention"]
    confirmations = view["confirmations"]
    deposit = view["deposit"]
    window = view["window"]
    from_asset = swap.get("from_asset") or "?"
    to_asset = swap.get("to_asset") or "?"
    waited = elapsed_seconds(swap.get("updated_at"), now_iso)
    tag = deposit.get("tag")

    lines = [
        "",
        f"swap {swap.get('id')}   {from_asset} -> {to_asset}",
        labeled("status", f"{view['status']}  <- {attention['headline']} [{attention['level']}]"),
        labeled("what it means", attention["detail"]),
    ]
    if not view["status_known"]:
        # "status unknown", not "status vocabulary": the label column is 16 wide
        # and the longer spelling filled it exactly, which prints the label and
        # the value with no gap between them.
        lines.append(labeled("status unknown", "this status is NOT one services/swap_view.py knows, so the code "
                                               "that writes it and the code that reads it have gone out of step"))
    if swap.get("failed_reason"):
        lines.append(labeled("recorded reason", f"{swap['failed_reason']}  <- swaps.failed_reason"))
    lines.extend([
        labeled("expected", f"{amount_text(swap.get('expected_input_amount'), from_asset, '(none)')}  <- what "
                            f"this swap was created to receive"),
        labeled("seen", f"{amount_text(swap.get('actual_input_amount'), from_asset, '(none) -- nothing seen yet')}"
                        f"  <- swaps.actual_input_amount, {what_the_seen_total_counts(swap)}"),
        labeled("confirmations", f"{confirmations['seen']} of {confirmations['threshold']} required, "
                                 f"{confirmations['rows']} deposit row(s)  <- {confirmations['source']}"),
        labeled("deposit target", f"{deposit.get('address') or '(none)'}  <- attributed by "
                                  f"{deposit.get('model')}"),
        labeled("destination tag", f"{tag}  <- MANDATORY on {from_asset}; the account above is shared by every "
                                   f"swap" if tag is not None else
                                   f"(none) -- {from_asset} deposits are attributed by address, so no tag applies"),
        labeled("payout address", f"{swap.get('payout_address')}  <- FINAL; a payout is final the moment it is "
                                  f"broadcast"),
        labeled("payout (est.)", f"{amount_text(swap.get('output_amount_estimate'), to_asset, '(none)')}  <- "
                                 f"after the fee and the network-fee reserve"),
        labeled("created", f"{swap.get('created_at')}"),
        labeled("updated", f"{swap.get('updated_at')}"
                           f"{'' if waited is None else f'  ({format_duration(waited)} ago)'}"),
        labeled("credited", f"{swap.get('credited_at') or '(none) -- the deposit was never accepted'}"),
        labeled("quote window", f"{window['display']}  <- {window['note']}"),
    ])
    # The two section headers carry their own COUNT rather than an empty value.
    # `labeled("deposit rows", "")` printed a label followed by trailing spaces,
    # which is rule 14's ambiguous gap in miniature: a reader cannot tell a
    # section with no rows from one whose rows failed to render, and the count
    # answers it before the rows do.
    events = swap.get("deposit_events") or []
    payouts = swap.get("payouts") or []
    lines.append(labeled("deposit rows", f"{len(events)}  <- {what_the_deposit_rows_are(events)}"))
    lines.extend(deposit_event_lines(events, int(swap.get("min_confirmations") or 0)))
    lines.append(labeled("payout rows", f"{len(payouts)}  <- a row exists only once a payout has been claimed"))
    lines.extend(payout_lines(payouts))
    return lines


def resolution_lines(rows: list[dict]) -> list[str]:
    """What resolving a halt would mean, and why nothing here does it (rule 16).

    Printed only when something is actually halted. It names no command that
    changes a row, deliberately: every way out of a halt moves money, in a
    direction this tool cannot know is right, and a ready-made `UPDATE` would be
    pasted at exactly the moment somebody is tired and staring at a stuck swap.
    """
    if not rows:
        return []
    # Indented four, not two: two spaces plus a word is the shape of a label row,
    # and tests/test_show_swap.py checks every one of those for a gap at the label
    # column. Prose is not a label row and must not be read as one.
    return [
        "",
        "WHAT HAPPENS NEXT, and why this tool stops here.",
        "    A halted swap is holding a deposit that did not match what the swap expected. Every way out of it "
        "moves money -- pay out at the quoted rate, pay out what the deposit is actually worth, or send the "
        "coins back -- and which one is right depends on facts no program here has: what the customer intended, "
        "what the desk is willing to do, and whose the coins are.",
        "    So this is a report and it ends. There is no --resolve, no --credit and no --refund, and nothing "
        "above is a command that writes. The evidence for the decision is on the screen; the decision is the "
        "operator's (rule 16).",
    ]


def read_report(db_path: str, swap_id: str, now_iso: str) -> list[str]:
    """Open the database read-only-in-practice, and build whichever report was asked for.

    Path.exists() BEFORE connecting, on purpose: sqlite3.connect() CREATES a
    missing file, so a tool that says it writes nothing must not reach connect()
    for a path that is not there. Same check open_swap.read_open_swaps() makes,
    for the same reason.
    """
    if not Path(db_path).exists():
        raise ShowRefused(
            f"there is no database at {db_path}, so there is nothing to show. That is not an error about a "
            f"swap: the file has never been created. The workers and the web app create it on first run, and "
            f"`python3 open_swap.py ... --apply` creates it too. Nothing was written, including that file."
        )
    connection = connect_db(db_path)
    try:
        if swap_id:
            swap = get_swap(connection, swap_id)
            if swap is None:
                raise ShowRefused(
                    f"no swap with id {swap_id} exists in {db_path}. Run this tool with no --swap to list the "
                    f"swaps that are waiting on a person, each with its real id already in a command."
                )
            return swap_lines(swap_display(swap, now_iso), now_iso)
        rows = halted_swaps(connection, now_iso)
        return halted_lines(rows, now_iso, status_counts(connection), db_path) + resolution_lines(rows)
    except sqlite3.OperationalError as error:
        # NAMED, not broad. This is what a database file with no `swaps` table
        # raises ("no such table: swaps"), which is an ordinary state for a file
        # created but never initialized -- and it must NEVER read as "no swap is
        # halted", which is the same answer a healthy empty table gives. That
        # confusion is the whole failure mode this tool was written against.
        raise ShowRefused(
            f"{db_path} exists but could not be queried ({error}). Until that is fixed this run cannot tell an "
            f"empty database from a missing table, so it is reporting neither. Nothing was written."
        ) from error
    finally:
        connection.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Show the swaps waiting on a person, or one swap in full. Read-only: it writes nothing.",
        epilog=(
            "With no arguments it lists every halted swap -- the rows behind deposit_watcher's "
            "HALTED_for_review count -- and prints a ready-made --swap command for each one. It never changes "
            "a status: resolving a halt moves money and is the operator's decision."
        ),
    )
    parser.add_argument(
        "--swap", default="", metavar="ID",
        help="show this one swap in full, whatever its status. Without it, every halted swap is listed.",
    )
    parser.add_argument(
        "--db", default="",
        help=f"database to read (default: Config.DB_PATH, currently {Config.DB_PATH}). It must be the same "
             f"file the workers read, or this tool is answering about something else.",
    )
    return parser


def run(args) -> int:
    """Orchestration only: announce, read, print (rule 10). Every decision is above."""
    started = time.monotonic()
    db_path = args.db or Config.DB_PATH
    config = get_config_dict()
    now_iso = utc_now_iso()

    for line in header_lines(db_path, config, args.db):
        print(line, flush=True)
    if args.swap:
        print(labeled("showing", f"one swap: {args.swap}"), flush=True)

    for line in read_report(db_path, args.swap, now_iso):
        print(line, flush=True)

    print(flush=True)
    print(labeled("read in", f"{format_duration(time.monotonic() - started)}  <- nothing was written"), flush=True)
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return run(args)
    except ShowRefused as refusal:
        print(f"\nREFUSED: {refusal}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
