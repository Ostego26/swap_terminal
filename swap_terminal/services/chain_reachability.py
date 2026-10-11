"""Whether each chain answered, last time anybody asked. One writer, many readers.

Role: submodule -> function (the two decisions are record_reachability and
      reachability_refusal)
Reads: swap_terminal.db (chain_reachability)
Writes: swap_terminal.db (chain_reachability) -- one row per asset, upserted
Can move funds: no, and the stronger statement is that nothing irreversible reads
      this. It gates whether a swap may be ATTEMPTED. A stale row can refuse a
      pair that would have worked, or admit one that fails at the next step with
      its own reason. No payout, no signature and no broadcast consults it: those
      read the chain, because a row in a table is not a confirmation.
Mainnet-safe: yes. It opens no socket of its own; it records what a caller that
      already opened one found.

=============================================================================
WHY THIS EXISTS. Operator, 2026-10-11, after a GRC -> LTC swap died:
=============================================================================

    "how bout ya stop guessing at fucking timeouts and assuming shit and have a
     robust and graceful way or verifying each step of the process was completed
     so nothing chokes"

THE FACT WAS ALREADY BEING MEASURED EVERY CYCLE AND THROWN AWAY. From their
payout_worker.log, cycles 69 to 80:

    wallet inventory for LTC NOT refreshed ... ConnectTimeoutError(... port=19332
    ... 'Connection to host.docker.internal timed out. (connect timeout=30.0)')
    wallet inventory for GRC NOT refreshed ... port=25715 ... same
    payout_worker cycle=71 IDLE ... in 50.1microfortnights (60.6s)

So the terminal learned, roughly once a minute, that LTC and GRC were
unreachable. It wrote that to a log file nobody was reading, and then offered
GRC -> LTC on the customer page and spent four 30s connect timeouts trying to
create the swap -- past the 60s gunicorn worker timeout, so the customer got a
500 with no reason and no deposit address.

CLAUDE.md rule 7 names this exactly: "A measurement that only exists in a log is
not learning. If it decides something, it belongs in SQL where the next run can
read it."

=============================================================================
WHY A STORED FACT AND NOT A PROBE AT RENDER TIME
=============================================================================

services/admin_view.probe_chain() already asks this question well, and it is
deliberately wired to ONE explicit endpoint rather than to any page. Its own
docstring says why: "a page whose load time is the sum of six RPC timeouts is a
page an operator interrupts, which is rule 14's opening failure."

That is true of the customer's pair list and much more true of the keystroke
handler behind POST /api/address/check. Reading a row is free. Asking six chains
is 180s when they are all dropping. So the asking stays where it already
happens -- on a worker cycle, which is going to pay that cost anyway -- and
everything that renders reads the row.

THREE STATES, AND THE THIRD IS A MISSING ROW. `reachable` is NOT NULL on purpose:
"nobody has asked yet" is already expressible as the absence of a row, and a NULL
would be a second spelling of one state. A fresh database has no rows, which is
why reachability_refusal() returns "" for an unknown asset -- see its docstring
for why silence is the only safe answer there.
"""

from __future__ import annotations

import logging
from datetime import datetime

from microfortnights import format_duration
from services.helpers import parse_iso, utc_now, utc_now_iso

logger = logging.getLogger(__name__)

#: How old a reading may be before a refusal stops being made from it.
#:
#: SECONDS, because it is an arithmetic difference between two timestamps and rule 6
#: keeps seconds where the arithmetic is; every PRINT of it below goes through
#: format_duration() and comes out in microfortnights.
#:
#: 600 IS DERIVED FROM THE THING THAT WRITES THE ROW, not chosen for roundness. Both
#: the payout worker and the reconcile worker call refresh_wallet_inventory() once per
#: cycle, and a cycle on the operator's host measured 60.5s with two chains dropping
#: (payout_worker.log, cycles 69-80). So a healthy deployment rewrites every row about
#: ten times inside this window, and a row older than it means the workers are not
#: running -- which is a different problem and must NOT be reported as a chain being
#: down.
#:
#: WHAT HAPPENS AT THE BOUNDARY IS A REFUSAL TO REFUSE, which is the safe direction: a
#: stale row stops gating, the swap attempt proceeds, and it then fails at the step
#: that actually touches the chain, with that step's own reason. The alternative --
#: trusting an old row -- would refuse a pair whose daemon came back twenty minutes ago.
STALE_AFTER_SECONDS = 600.0


def record_reachability(db, asset: str, asked: str, reachable: bool, detail: str = "") -> None:
    """Record that `asset` did or did not answer `asked`. Upsert; one row per asset.

    CALLED FROM A LOOP THAT ALREADY HAS THE ANSWER. The caller is
    services/payout_service.refresh_wallet_inventory(), which makes the call and
    catches the failure; this records the outcome it already has rather than asking
    again. Nothing here opens a socket, which is what makes it safe to call on every
    cycle.

    NO COMMIT. The caller commits once at the end of its loop, as it already does for
    the inventory rows -- one transaction per cycle rather than one per chain, which
    matters because SQLite has a single writer lock (rule 15) and three workers share
    it.

    `detail` IS AN EXCEPTION STRING AND IS STORED AS-IS. It is written by one caller,
    from a transport or RPC failure, and is rendered to an operator. It is never a
    secret: a connection error names a host and a port, which the operator configured.
    A reader that wanted to put it in front of a customer should use
    reachability_refusal() instead, whose sentence is written for that.
    """
    db.execute(
        """
        INSERT INTO chain_reachability (asset, reachable, asked, detail, checked_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(asset) DO UPDATE SET
            reachable = excluded.reachable,
            asked = excluded.asked,
            detail = excluded.detail,
            checked_at = excluded.checked_at
        """,
        (asset, 1 if reachable else 0, asked, detail, utc_now_iso()),
    )


def reachability_rows(db) -> dict:
    """Every stored reading, keyed by asset. {} on a database with no rows yet.

    A DICT RATHER THAN A QUERY PER ASSET, because every caller wants several: the pair
    list asks about both legs of up to 30 pairs, and 60 single-row queries against the
    one writer lock to answer one page is the shape rule 15 warns about.
    """
    return {
        row["asset"]: {
            "reachable": bool(row["reachable"]),
            "asked": row["asked"],
            "detail": row["detail"],
            "checked_at": row["checked_at"],
        }
        for row in db.execute("SELECT * FROM chain_reachability").fetchall()
    }


def row_age_seconds(checked_at: str, now: datetime | None = None) -> float | None:
    """How old a `checked_at` is, in seconds. None if it cannot be read as a timestamp.

    NONE RATHER THAN A RAISE, and rather than zero. A row whose timestamp does not
    parse is a row of unknown age, and reachability_refusal() treats unknown age the
    same as too old -- it stops refusing. Returning 0.0 would make an unreadable
    timestamp look like the freshest possible reading, which is the one answer that
    turns a corrupt row into a confident refusal.

    SECONDS, because it is subtraction between two datetimes (rule 6: seconds stay
    where the arithmetic is). Every caller that PRINTS it runs it through
    format_duration() first.
    """
    try:
        then = parse_iso(checked_at)
    except (TypeError, ValueError):
        return None
    if then.tzinfo is None:
        # A naive timestamp cannot be compared with an aware one, and guessing which
        # zone it meant is how an age comes out hours wrong. Unknown is the honest
        # answer and it is the safe one -- see this function's docstring.
        return None
    return ((now or utc_now()) - then).total_seconds()


def reachability_refusal(rows: dict, asset: str, age_seconds: float | None = None) -> str:
    """Why `asset` cannot be used right now, or "" if there is no reason to say so.

    SILENCE IS THE ANSWER FOR EVERYTHING THIS CANNOT ESTABLISH, and there are two such
    cases. Both return "" so the pair stays offered and fails later with a real reason.

      NO ROW         nobody has asked yet. A fresh database, or a chain that was just
                     configured. Refusing here would make every pair unavailable until
                     a worker cycle completed, which on a cold start means the
                     customer page is empty for a minute for no reason.
      A STALE ROW    older than STALE_AFTER_SECONDS, which means the workers are not
                     running rather than that the chain is down. That is a different
                     problem and reporting it as a chain outage would send somebody to
                     restart a daemon that is fine.

    `age_seconds` MAY BE SEEDED, and that is what makes this decision testable without
    a clock (rule 10: the decision is the smallest testable piece). Left as None it is
    computed from the row, which is what every production caller does.

    THE SENTENCE NAMES THE AGE, because "GRC is not answering" and "GRC was not
    answering nine minutes ago" are different claims and only the second is true.
    """
    row = rows.get(asset)
    if row is None:
        return ""
    if row["reachable"]:
        return ""
    age = row_age_seconds(row["checked_at"]) if age_seconds is None else age_seconds
    if age is None or age > STALE_AFTER_SECONDS:
        logger.debug(
            "chain_reachability row for %s is %s old (limit %s), so it is NOT being used to "
            "refuse. A row this old means the workers are not writing it, not that the chain "
            "is down.",
            asset,
            "of unknown age" if age is None else format_duration(age),
            format_duration(STALE_AFTER_SECONDS),
        )
        return ""
    return (
        f"the {asset} chain did not answer when it was last asked "
        f"({format_duration(age)} ago, via {row['asked']})"
    )
