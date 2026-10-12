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
import sqlite3
import subprocess
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


#: Failure classes that mean THE TRANSPORT ITSELF FAILED, as opposed to a chain that
#: answered with an error or an adapter that declined to ask.
#:
#: OSError COVERS MOST OF IT AND THAT IS NOT A COINCIDENCE: requests.ConnectionError and
#: requests.Timeout are both OSError subclasses (checked, not recalled -- their MRO is
#: ConnectionError -> RequestException -> OSError), as is the builtin ConnectionError.
#: So chains/base.RPCAdapter, which lets requests' exception propagate untouched, is
#: covered by the first entry alone.
#:
#: subprocess.TimeoutExpired IS LISTED SEPARATELY because it is NOT an OSError -- it
#: inherits SubprocessError -- and chains/icp.py talks to the replica by running `dfx`
#: rather than by posting HTTP. A dfx call that never returns is as much a transport
#: failure as a dropped SYN, and leaving it out would mean an unreachable ICP replica
#: was never gated.
_TRANSPORT_FAILURES = (OSError, subprocess.TimeoutExpired)


def is_transport_failure(exc: BaseException) -> bool:
    """Did the TRANSPORT fail, as opposed to the chain answering or the adapter declining?

    THE DECISION THIS WHOLE MODULE TURNS ON, and it was missing for the first hour this
    table existed. Measured on the operator's host 2026-10-11, the first run of the new
    gate recorded:

        XRP did NOT answer  via get_balance
            get_balance() answers 'what is MY balance' and this adapter has no account
            of its own, deliberately

    THAT IS NOT AN OUTAGE. XRPAdapter.get_balance() refuses BY DESIGN -- the XRP Ledger
    deposit and payout account is XRP_DEPOSIT_ACCOUNT, supplied at the call site, and
    the adapter deliberately holds none so that it is never one configuration value away
    from being a payout path (its own docstring argues this at length). Recording that
    as unreachable made the gate refuse every XRP pair while XRP was perfectly fine --
    a working chain blocked by a check added to stop chains choking.

    This module's header already said the distinction mattered ("a chain whose
    get_balance refuses by design is not an unreachable chain, and this column is what
    lets the two be told apart") and then nothing told them apart. Rule 17: the comment
    was a reason to believe, not a check.

    THE CAUSE CHAIN IS WALKED, not just the exception, because two adapters re-raise:

        chains/solana.py   `except requests.RequestException as exc: raise
                           SolanaRPCError(...) from exc` -- so the OSError is the
                           __cause__ and the outer class is not one.
        chains/icp.py      wraps a dfx failure in ICPCallFailed(RuntimeError).

    XRP's by-design refusal is a BARE raise with no `from`, so its __cause__ is None.
    That absence IS the distinguishing fact, and it is what this function reads.

    __context__ IS FOLLOWED AS WELL AS __cause__, because an adapter that re-raises
    inside an `except` block without `from` still has the original attached there. A
    `raise X` that happens NOT to be inside an except block has neither, which is the
    deliberate-refusal case.

    FALSE IS THE SAFE ANSWER AND IS WHAT UNKNOWN SHAPES GET. An adapter that invents a
    new failure class and chains nothing will read as "not a transport failure", so no
    row is written and nothing is gated -- the chain keeps being offered and fails at
    the step that touches it, with that step's own reason. The opposite default would
    refuse a working chain on an exception nobody anticipated, which is the defect this
    function exists to undo.
    """
    seen = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, _TRANSPORT_FAILURES):
            return True
        current = current.__cause__ or current.__context__
    return False


def forget_reachability(db, asset: str) -> None:
    """Remove any stored claim about `asset`. For when the probe CANNOT establish one.

    DELETE AND NOT A WRITE, because a missing row is already this table's "nobody has
    asked" and reachability_refusal() is silent for it. An adapter that declines to
    answer leaves no evidence either way, so the honest state is no claim at all.

    WHY IT IS NOT ENOUGH TO JUST SKIP THE WRITE. The operator's database already held
    `XRP did NOT answer`, written before is_transport_failure() existed. Skipping future
    writes would leave that claim standing until it aged past STALE_AFTER_SECONDS -- ten
    minutes of refusing every XRP pair, for a chain that was never down. Deleting it
    self-heals on the first cycle after the fix, which is the difference between a fix
    and a fix that arrives in ten minutes.

    AN RPC ERROR REPLY IS EVIDENCE OF REACHABILITY AND IS DELIBERATELY NOT RECORDED AS
    SUCH. A daemon that answers "insufficient funds", or a 401, has demonstrably been
    reached -- so `reachable=True` would be defensible. It is not written, because this
    table's one job is to say which chains are answering USEFULLY, and an operator
    reading "GRC ANSWERED" for a daemon that rejects every call would be misled by a
    technically true row. Both choices have the same effect on the gate -- neither
    refuses -- so the tie is broken on which row reads honestly to a person.

    NO COMMIT, for the same reason record_reachability() does not: the caller commits
    once per cycle (rule 15, one writer lock).
    """
    db.execute("DELETE FROM chain_reachability WHERE asset = ?", (asset,))


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


def reachability_rows(db, now: datetime | None = None) -> dict:
    """Every stored reading, keyed by asset, each stamped with its age. {} when empty.

    A DICT RATHER THAN A QUERY PER ASSET, because every caller wants several: the pair
    list asks about both legs of up to 30 pairs, and 60 single-row queries against the
    one writer lock to answer one page is the shape rule 15 warns about.

    `age_seconds` IS STAMPED HERE, FROM ONE CLOCK READING, AND THAT IS A BUG FIX RATHER
    THAN A CONVENIENCE. reachability_refusal() names the age in its sentence -- "GRC is
    not answering" and "GRC was not answering nine minutes ago" are different claims
    and only the second is true -- and it used to compute that age per CALL. It is
    called once per ordered pair, so one down chain produced a DIFFERENT STRING for
    every pair it is a leg of, and services/pair_view.refusals_by_cause() deduplicates
    by string. Measured 2026-10-12:

        2 == 1 ... ['the GRC chain did not answer ... (0.00003µfn ago, via get_balance)',
                    'the GRC chain did not answer ... (0.0001µfn ago, via get_balance)']

    for two pairs. On the operator's host, with GRC and LTC both refusing, that is
    FOURTEEN cause lines for TWO down daemons -- and refusals_by_cause()'s own docstring
    claims the opposite, which makes it the wrong-comment bug (rule 16) in the function
    whose whole job is to collapse them.

    ONE READING FOR THE WHOLE RENDER is also the pattern services/admin_view.overview()
    already states for freshness: "Computing the time per row would let two rows on the
    same screen disagree about what `now` is, which is a small thing that makes a
    staleness table impossible to reason about." Same decision, one layer down.

    `now` is seedable for the same reason overview() takes `now_iso`: a test pins the
    clock rather than racing it.
    """
    moment = utc_now() if now is None else now
    return {
        row["asset"]: {
            "reachable": bool(row["reachable"]),
            "asked": row["asked"],
            "detail": row["detail"],
            "checked_at": row["checked_at"],
            # None for a timestamp that does not parse, which reachability_refusal()
            # treats exactly as too old -- see row_age_seconds() for why that is not 0.0.
            "age_seconds": row_age_seconds(row["checked_at"], moment),
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
    # THE AGE IS TAKEN FROM THE ROW WHEN THE ROW CARRIES ONE, so every pair in one
    # render names the same age for the same chain and the sentences deduplicate. See
    # reachability_rows(), which stamps it from one clock reading, and which has the
    # measurement of what per-call ages did to refusals_by_cause().
    #
    # THREE SOURCES IN PRIORITY ORDER, and the fallback is kept rather than removed: an
    # explicit `age_seconds` is a caller pinning the decision for a test, a stamped row
    # is the production path, and computing it here serves a hand-built dict -- which
    # several tests and services/swap_service pass. `"age_seconds" in row` rather than
    # `row.get(...) is not None` because None is a MEANINGFUL stamped value (a timestamp
    # that did not parse) and must not fall through to being recomputed into the same
    # None at a cost.
    if age_seconds is not None:
        age = age_seconds
    elif "age_seconds" in row:
        age = row["age_seconds"]
    else:
        age = row_age_seconds(row["checked_at"])
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


def reachability_from_path(db_path: str) -> tuple[dict, str, str]:
    """The stored readings at `db_path`, a one-line verdict, and the detail. Never raises.

    ({}, ...) ON ANY FAILURE, which is the posture
    services/swap_service._unreachable_leg_for_swap() takes and for the same reason:
    this gate's own failure must not become a refusal. An unreadable table means
    "reachability was not considered", which is what reachability=None means to
    pair_serviceability() -- emphatically not "every chain is down". A tool handed a
    wrong --db would otherwise report thirty unavailable pairs and blame the chains.

    THREE RETURN VALUES AND NOT ONE, because a report that silently drops the sixth
    condition is rule 14's blank gap: a reader cannot tell a database with no rows from
    a database that could not be opened, and those are different problems with
    different fixes. The HEADLINE is one line so a label column stays readable; the
    DETAIL is whatever the authority said, in full, and db.connect_db()'s refusal in
    particular is a five-clause remedy worth keeping whole.

    WHY IT IS HERE AND NOT IN THE TOOLS. It was pair_verdicts.reachability_or_empty()
    for six hours on 2026-10-12, and then open_swap.py needed the same thing -- the
    operator's decision, that day, on its catalog calling a pair available whose chain
    had not answered in ten minutes. Two callers of one idea is rule 8's shape, and the
    survivor belongs with the table it reads and with reachability_rows(), which it
    wraps. A third caller gets it for free; neither tool re-derives what a failed read
    means.

    THE CONNECTION IS OPENED AND CLOSED HERE rather than handed in, which is the
    difference from reachability_rows() above: a root tool has no app context and no
    long-lived connection, and an unclosed read handle on a WAL database is a reader
    that pins the write-ahead log.
    """
    # IMPORTED HERE AND NOT AT MODULE SCOPE. Every other function in this file takes an
    # open connection, so the module has never depended on db.py; importing it at the
    # top would put the whole schema in the import graph of the three workers that only
    # ever call record_reachability(). PLC0415 is silenced with the reason rather than
    # the import moved, because the reason is the point (rule 19: a noqa is a claim).
    from db import connect_db  # noqa: PLC0415 -- see the comment above

    try:
        db = connect_db(db_path)
    except (sqlite3.Error, OSError, RuntimeError, ValueError) as error:
        return {}, f"NOT CONSIDERED -- {db_path} could not be opened", str(error)
    try:
        rows = reachability_rows(db)
    except sqlite3.Error as error:
        return {}, "NOT CONSIDERED -- the chain_reachability table could not be read", str(error)
    finally:
        db.close()
    if not rows:
        return {}, f"NOT CONSIDERED -- {db_path} has no chain_reachability rows yet", (
            "The payout and reconcile workers write a row every cycle, from a call they "
            "were already making, so an empty table means neither worker has run against "
            "this database. Until one does, the sixth condition refuses nothing and every "
            "pair is judged on configuration alone."
        )
    return rows, f"{len(rows)} chain(s) recorded in {db_path}", ""
