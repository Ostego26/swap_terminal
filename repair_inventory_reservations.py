#!/usr/bin/env python3
"""Report, and with --apply repair, a `wallet_inventory` figure the payout rows do not justify.

Role: file (operator entry point at the project root per CLAUDE.md rule 10; every
      decision is a function below -- justified_reservations() is SQL, plan_for_asset()
      is callable with seeded rows and no database at all)
Reads: swap_terminal.db ONLY -- `wallet_inventory` and `payouts`. No chain, no
      adapter, no daemon, no RPC, no .env, no key material, no price feed, no
      file outside the database.
Writes: with --apply ONLY -- `wallet_inventory.hot_reserved` and
      `hot_available`, or a DELETE of one row; plus exactly one
      `wallet_inventory_corrections` row per correction, in the SAME transaction.
      Never `hot_confirmed` on an asset whose balance can be read. Never
      `payouts`, never `swaps`, never `swap_audit_log`, never a deposit row.
Can move funds: NO, AND THIS IS THE SENTENCE TO READ BEFORE THE FLAG.
      **--apply HERE BROADCASTS NOTHING.** It corrects a database column and
      moves no coins. There is no send call in this file, no signature, no
      arming token, no keypair path, no adapter: `grep -n "send\\|sign\\|broadcast"`
      over it finds only this paragraph and the prose explaining other tools.
      That is worth stating prominently because every OTHER --apply at this
      project root can move money -- collect_fees.py broadcasts a sweep,
      settle_payout.py and rescue_payout.py reconcile real sends,
      correct_payout_amounts.py rewrites what a customer is recorded as having
      been paid -- so an operator has learned to treat the flag as dangerous.
      Here the risk is the opposite one: the figure stays wrong until it is run.
Mainnet-safe: yes in both directions. The report is a read. --apply writes three
      numbers that no payout path consults, and records what each one was.

=============================================================================
TWO DEFECTS, MEASURED ON THE OPERATOR'S LIVE HOST 2026-10-04
=============================================================================

Read read-only out of their `wallet_inventory` and `payouts`:

    asset  hot_confirmed            hot_reserved          hot_available            updated_at
    BTC    10.0012                  0.0                   10.0012                  2026-10-04T17:19:17
    GRC    3780.08454497            9882.372957331736     -6102.288412361736       2026-10-04T17:19:17
    LTC    100.0                    0.0                   100.0                    2026-10-04T17:19:17
    SOL    28.087120892             0.0                   28.087120892             2026-10-04T17:19:17
    XRP    -59.231412662192405      0.0                   -59.231412662192405      2026-10-04T14:31:20

    GRC payouts:  broadcast 10 rows, 10343.81698888 total
                  failed     5 rows,  9993.425862093694 total

-----------------------------------------------------------------------------
DEFECT 1 -- XRP's "balance" IS THE NEGATED SUM OF ITS OWN PAYOUTS, EXACTLY
-----------------------------------------------------------------------------

    payouts.id=23  3.3155893288590605   (the GRC->XRP swap)
    payouts.id=24  55.91582333333334    (the BTC->XRP swap)
    sum            59.231412662192405
    hot_confirmed -59.231412662192405   <- exact negation

REPRODUCED BIT FOR BIT 2026-10-04, by calling the real functions against an
empty seeded database in the order the payout worker calls them -- not by
reading the code and agreeing with it (rule 17, and the behavioral-verification
principle):

    reserve_inventory(db, "XRP", 3.3155893288590605)    release_inventory_after_send(...)
    reserve_inventory(db, "XRP", 55.91582333333334)     release_inventory_after_send(...)
    -> {'asset': 'XRP', 'hot_confirmed': -59.231412662192405,
        'hot_reserved': 0.0, 'hot_available': -59.231412662192405}

    hot_confirmed == -59.231412662192405  ->  True

So the code path is established rather than suspected:

  THE CREATOR IS reserve_inventory(), NOT release_inventory_after_send().
  services/payout_service.reserve_inventory() has an INSERT branch for an asset
  with no row -- `(asset, 0.0, amount, -amount)` -- and that is the only way an
  XRP row can come into existence, because the other writer,
  refresh_wallet_inventory(), calls adapter.get_balance() first and
  chains/xrp.XRPAdapter.get_balance() RAISES by design ("answers 'what is MY
  balance' and this adapter has no account of its own, deliberately"). So the
  refresh logs its WARNING, `continue`s, and never inserts or credits anything.

  release_inventory_after_send() IS THE DEBITER. It subtracts the amount from
  hot_confirmed with no floor, on a row whose hot_confirmed was never a balance.

  A LEDGER WITH A DEBIT SIDE AND NO CREDIT SIDE. The figure is not a wrong
  balance, it is not a balance at all: it is an accumulator of payouts with the
  sign flipped, and it would keep getting more negative with every XRP payout
  for as long as the row exists.

  THE ROW IS WRONG TWICE. Both debits are the PRE-quantization figures --
  3.3155893288590605 rather than the 3.315589 the XRP Ledger actually sent, and
  correct_payout_amounts.py has since corrected the payout row itself -- so the
  accumulation predates the 2026-10-03 quantize-before-send fix and would not
  reconcile against the chain even as an accumulator.

  SOL DOES NOT SHARE THIS SHAPE, and it was checked rather than assumed because
  SOL_HOT_WALLET is read differently from a Bitcoin-style wallet.
  chains/solana.SolanaAdapter.get_balance() returns a figure whenever
  SOL_HOT_WALLET is set, so SOL's row IS credited on every reconcile cycle. The
  row above is its own evidence: 28.087120892 is positive and carries nine
  decimals, which is lamport precision from getBalance, and the two SOL payouts
  (ids 14 and 15, 0.000761654 each) debited it exactly as XRP's did -- the
  difference is that the next refresh overwrote it. THAT IS THE WHOLE
  ASYMMETRY: release_inventory_after_send()'s hot_confirmed debit is harmless
  for every asset whose balance can be read, because it is overwritten within a
  cycle, and permanent for the one asset whose cannot.

-----------------------------------------------------------------------------
DEFECT 2 -- GRC's RESERVATION EXCEEDS ITS BALANCE
-----------------------------------------------------------------------------

hot_reserved 9882.372957331736 against hot_confirmed 3780.08454497 gives
hot_available -6102.288412361736. services/payout_service.py already records
the mechanism in prose -- "a payout that failed left its reservation standing,
so a correction that skipped release_inventory_after_send() would leave the hot
wallet permanently short on paper" -- and until this tool nothing measured or
repaired it.

The mechanism, read off services/payout_service.process_pending_payouts():

    reserve_inventory(amount)          hot_reserved += amount
    INSERT INTO payouts ... 'created'
    broadcast_payout(...)
      success  -> payouts 'broadcast'  AND release_inventory_after_send()
      failure  -> payouts 'failed'     AND NO RELEASE AT ALL

THE RESERVED FIGURE DOES NOT EQUAL THE FAILED TOTAL, so "every failure leaked"
is not the story and this tool does not tell it:

    failed GRC total    9993.425862093694
    hot_reserved        9882.372957331736
    difference            111.05290476195842   reserved is SHORT by this

WHAT I ESTABLISHED ABOUT THE 111.05, AND WHERE I STOPPED. The figure is within
about 46 ulps of 2 x 55.52645238097888 -- twice the amount of payout id=3,
swap s_5ca7e29438479d29:

    2 * 55.52645238097888  =  111.05290476195776
    the discrepancy        =  111.05290476195842
    apart by                   6.6e-13, where one ulp at this magnitude is 1.4e-14

That is a lead with a named mechanism behind it rather than a coincidence:
settle_payout.py's own source comment records that every --apply performed its
four writes TWICE until 2026-09-26, that release_inventory_after_send()
"subtracts from hot_confirmed WITHOUT a floor", and that the second call "took
the asset's confirmed balance 55.5 GRC below the truth" -- 55.5 GRC being
id=3's amount, on the one swap that tool was written to correct. A double
release subtracts a reservation twice, which is exactly a hot_reserved figure
LOWER than the reservations still standing.

IT DOES NOT CLOSE ARITHMETICALLY AND I AM NOT GOING TO WRITE THAT IT DOES
(rule 17). A single double-release accounts for one times 55.526, not two, and
the residue is 46 ulps rather than 1 -- consistent with hot_reserved being a
running float accumulator that has taken dozens of additions and subtractions,
but consistent is not measured. The rows that would settle it are the
operator's and cannot be read from here, which is why THIS TOOL PRINTS THE
PER-ROW ACCOUNT rather than embedding a story: every payout row that justifies
the reservation, every row that could have leaked one, and the arithmetic
between them, on screen, for the operator to read off.

-----------------------------------------------------------------------------
WHAT THE WRONG FIGURE ACTUALLY COSTS -- MEASURED, AND IT IS NOT THE PAYOUT PATH
-----------------------------------------------------------------------------

The obvious fear is that a negative hot_available refuses a legitimate customer
GRC payout on a wallet holding 3780 GRC. MEASURED 2026-10-04 AGAINST A SEEDED
ROW CARRYING THE OPERATOR'S OWN FIGURES, and it is FALSE:

    seeded  GRC hot_confirmed=3780.08454497  hot_reserved=9882.372957331736
                hot_available=-6102.288412361736

    largest_fundable_payout(adapters,"GRC",0.001)
      -> (3780.08354497, "3780.08454497 GRC spendable less 0.001 GRC reserved ...")
    why_the_payout_cannot_be_funded(adapters,"GRC",100.0,0.001)
      -> FundingVerdict(refuses=False, unchecked=False, why='')

    then hot_confirmed and hot_available ZEROED, same two calls
      -> byte-identical answers

Both functions in services/payout_capacity.py call `adapter.get_balance()`
themselves and read NO database column -- this confirms the earlier
measurement recorded in db.py's schema comment. So the blast radius is not the
customer payout path.

IT IS THE FEE SWEEP, and that reader was missing from db.py's own list of who
reads this table (three of four; corrected in the same commit as this file).
swap_terminal/fee_sweep.obligation() reads hot_reserved and sets
`floor = max(open_total, reserved)`. Measured the same day with the same seeded
row:

    obligation(db,"GRC")
      -> floor=9882.372957331736  open_total=0.0  open_swaps=0
         reserved=9882.372957331736

A retention floor of 9882.37 GRC against a wallet holding 3780.08, with zero
open swaps. Every GRC fee sweep is refused for as long as that figure stands --
which is the sweep that surfaced the defect in the first place. The desk cannot
take its own fee out, and the refusal names a reservation that nothing owes.

=============================================================================
WHAT THE REPAIR IS, PER ASSET, AND WHICH COLUMNS IT WRITES
=============================================================================

THE JUSTIFIED RESERVATION IS DERIVED IN SQL, not by a Python loop over rows
(rules 5 and 20). It is a filtered sum over rows already in the database, which
is the definition of a query: justified_reservations() is one statement, so it
has no half-applied state on a row that is odd, it answers the same way for
every reader, and an operator can paste it under the tool's own output and get
the same number. The status set it filters on is db.PAYOUT_RESERVED_STATUSES,
which is printed on screen on every run, and it is `('created',)` -- NOT
PAYOUT_LIVE_STATUSES. That difference is argued at length beside the constant
in db.py and summarized here because it is the whole hinge of the correction: a
`broadcast` row has already had its release, so counting it would double every
delivered payout into the floor, and a `failed` row never had one, so counting
it would declare the leak correct.

  TRIM      hot_reserved is above what the rows justify. Write the justified
            figure, and recompute hot_available from it. The money this frees
            is not money -- it is a number that was holding a sweep back.

  DROP      the row should not exist. XRP's case: hot_confirmed is NEGATIVE,
            which no balance read can produce, and no reservation is justified.
            See the next section.

  OK        the reservation is exactly what the rows justify. Nothing is
            written, and the asset is named on screen as untouched rather than
            omitted -- an asset missing from a report is indistinguishable from
            an asset the query did not reach (rule 14).

  REFUSE    something is true that this tool will not decide. The one case:
            hot_confirmed is negative AND a reservation IS justified, so the row
            cannot be dropped without discarding a live obligation and cannot be
            trimmed into a figure that means anything. Named, with the rows, and
            left alone.

WHY XRP IS DROPPED AND NOT SET TO A NUMBER. There is no number to write.

  0.0 WOULD BE A CLAIM, AND A FALSE ONE -- that the account holds nothing. It
  holds XRP; this process simply cannot read how much, by design.
  services/admin_view.py renders hot_confirmed as the asset's balance, so a
  zero would put "XRP 0" on the operator's screen as a measurement.

  ANY OTHER NUMBER WOULD BE INVENTED. The only figure available is the one
  already there, and it is the negated payout sum.

  ABSENCE IS THE TREE'S OWN "NOT ESTABLISHED", and this is the part that makes
  deletion a restoration rather than a loss. chains/xrp.py's get_balance()
  docstring states the expected state in advance: "refresh_wallet_inventory()
  calls get_balance() on every adapter, so XRP gets no wallet_inventory row and
  the operator sees no XRP figure on the admin page", and
  services/payout_service.inventory_note() already reports a missing row for
  such an asset as correct -- "an asset whose hot wallet is unset refuses by
  design and is expected here". So the absent row is the documented, SELF-
  DESCRIBING state, and the present row is the anomaly. Dropping it is rule 14
  applied to a table: `(none)` is a result, and a wrong number is not.

  AND IT IS SAFE BECAUSE NOTHING NEEDS IT. reserve_inventory() recreates the row
  on the next XRP payout -- that is how this one appeared -- fee_sweep reads 0.0
  for an absent asset, admin_view omits it, and payout_capacity never looked.

THE DISCRIMINATOR IS A NEGATIVE hot_confirmed, AND IT IS SOUND RATHER THAN
CONVENIENT. No balance read on any of the five chains can return a negative
number: a Bitcoin-derived `getbalance`, Solana's getBalance in lamports and the
XRP Ledger's reserve-adjusted drops are all non-negative by construction. So
hot_confirmed < 0 is not "an asset that looks uncreditable", it is proof that
the only writes this row has ever had were debits. The tool does not consult the
adapter table, does not import chains/, and does not hardcode "XRP" -- which
means an asset that grows this shape later is caught by the same rule, and XRP
is caught only for as long as it has it.

HOT_CONFIRMED IS NEVER WRITTEN, ON ANY ASSET. For a readable asset the figure
comes from the chain and refresh_wallet_inventory() overwrites it on the next
reconcile cycle, so writing it here would be a number with a lifetime of
seconds that the operator might read as a correction. For XRP the row goes
entirely. There is no third case. The columns written are:

    hot_reserved    the justified figure, derived in SQL from `payouts`
    hot_available   hot_confirmed - hot_reserved, which is what db.py's schema
                    comment says the column holds and what
                    refresh_wallet_inventory() computes. Leaving it stale after
                    moving hot_reserved would correct one number and falsify the
                    one derived from it.

IDEMPOTENT, AND THE GUARD IS THE PLAN AND NOT A FLAG. A second run re-derives
the justified figure, finds hot_reserved already equal to it, and returns OK --
so there is nothing to apply, no correction row is written, and the section
prints `(none)` rather than an empty gap. The DROP case is idempotent by
absence: the row is gone, so the next run does not see the asset at all.

=============================================================================
THE CAUSE, WHICH THIS TOOL DOES NOT FIX, AND WHY THAT IS NOT A DODGE
=============================================================================

Rule 19 says fix the cause, not the symptom, and the cause of defect 1 is one
line in services/payout_service.release_inventory_after_send():

    float(row["hot_confirmed"]) - amount

debiting a hot_confirmed that was never credited. The fix is for it to refuse
that debit on a row whose hot_confirmed has never been a balance -- leaving the
figure alone rather than driving it further negative, and saying so.

THAT IS A CHANGE TO WHAT A PAYOUT DOES, so it is the live-posture half and it
comes back to the operator as a PROPOSAL rather than shipped from here (rule 16:
"anything changing what gets sent ... the payout path"). It is on the function
that runs immediately after a real send has left the wallet, in the worker that
holds the only record of it, and the one outcome that must never happen on that
path is an exception between the broadcast and the record -- which
services/payout_service.py's own 2026-09-26 comment records costing a payout
that went out with `txid (none)` against it. A guard added there by a session
that cannot run the payout worker is a fix I cannot test (rule 16 again), and
the repair below removes the damage without touching the send.

WHAT THE PROPOSAL IS, PRECISELY, SO THE OPERATOR DOES NOT HAVE TO DERIVE IT:
make the hot_confirmed debit conditional on the row having been credited, and
keep the hot_reserved release unconditional. The reserved half is pure desk
bookkeeping that this tool's own arithmetic depends on; the confirmed half is a
cache of a chain read that is either overwritten within a cycle (every readable
asset) or meaningless (XRP). Done that way the change is a no-op for BTC, LTC,
GRC and SOL by construction, and the whole of its behavior change is on the one
asset whose figure is already wrong.

=============================================================================
WHAT IT REFUSES BEFORE READING ANYTHING
=============================================================================

A database path that does not exist is a refusal and not a creation. Connecting
would make an empty database, every asset would read OK against a table with no
rows, and the tool would report a clean repair of nothing -- which is rule 14's
"did nothing must not look like did work" in its worst form, on a tool whose
whole output is reassurance.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path
from typing import NamedTuple

sys.path.insert(0, str(Path(__file__).resolve().parent / "swap_terminal"))

from config import Config
from db import (
    INVENTORY_CORRECTIONS_SQL,
    INVENTORY_CORRECTIONS_TABLE,
    PAYOUT_RESERVED_STATUSES,
    db_session,
)
from microfortnights import format_duration
from report_block import labeled
from services.helpers import utc_now_iso

SELF = "repair_inventory_reservations.py"

# THE FOUR VERDICTS. Each is a different fact about an asset and none of them
# prints like another (rule 14): a trimmed row, a dropped row, a row that was
# already right and a row this tool will not decide are four outcomes, and
# collapsing the last two into "nothing written" would hide the only one that
# needs a human.
TRIM = "TRIM"
DROP = "DROP"
OK = "OK"
REFUSE = "REFUSE"

#: The status set that justifies a reservation, echoed on screen on every run so
#: the output says what decided the answer (rule 14). Imported rather than spelled
#: here: db.py owns the vocabulary and carries the argument for why it is
#: `('created',)` and not PAYOUT_LIVE_STATUSES.
RESERVED_STATUSES = PAYOUT_RESERVED_STATUSES

# ---------------------------------------------------------------------------
# THE SQL. Both statements are a filter and a sum over rows already in the
# database, which rule 20 answers without asking: a view-shaped derivation goes
# in SQL. The status lists are formatted into the text because a SQL parameter
# cannot bind a variable-length IN list in SQLite and these are literals from
# db.py, never input -- the same claim chains/ makes wherever it interpolates an
# identifier, and the reason it is written down is that `noqa: S608` is a claim
# somebody checked (rule 19).
# ---------------------------------------------------------------------------

_RESERVED_IN = ", ".join(f"'{status}'" for status in RESERVED_STATUSES)

#: Per asset, what the rows justify holding back, and over how many rows. LEFT JOIN
#: from wallet_inventory so an asset with a row and NO justifying payout still
#: appears with 0.0 -- which is GRC's and XRP's case and the whole point. An INNER
#: JOIN would have silently dropped exactly the assets being repaired.
JUSTIFIED_SQL = f"""
SELECT inv.asset                                    AS asset,
       inv.hot_confirmed                            AS hot_confirmed,
       inv.hot_reserved                              AS hot_reserved,
       inv.hot_available                             AS hot_available,
       inv.updated_at                                AS updated_at,
       COALESCE(SUM(p.amount), 0.0)                  AS justified,
       COUNT(p.id)                                   AS justifying_rows
  FROM wallet_inventory AS inv
  LEFT JOIN payouts AS p
         ON p.asset = inv.asset
        AND p.status IN ({_RESERVED_IN})
 GROUP BY inv.asset
 ORDER BY inv.asset ASC
"""  # noqa: S608 -- checked: the only interpolation is _RESERVED_IN, built from db.PAYOUT_RESERVED_STATUSES, which is a literal tuple in this repository's own source. No value on this line comes from a caller, the environment, a chain or a row.

#: Every payout row for one asset, grouped so the operator can read the per-row
#: account off the screen. `justifies` is the discriminator the sum above applies,
#: returned as a column so the printed line and the arithmetic cannot disagree
#: about which rows were counted (rule 8).
PER_ROW_SQL = f"""
SELECT p.id                    AS id,
       p.swap_id               AS swap_id,
       p.status                AS status,
       p.amount                AS amount,
       p.txid                  AS txid,
       CASE WHEN p.status IN ({_RESERVED_IN}) THEN 1 ELSE 0 END AS justifies
  FROM payouts AS p
 WHERE p.asset = ?
 ORDER BY p.id ASC
"""  # noqa: S608 -- checked: same single interpolation as JUSTIFIED_SQL above, from the same literal tuple. The asset is a bound parameter.


class Plan(NamedTuple):
    """What to do about one asset, and everything that decided it.

    PURE DATA, produced by plan_for_asset() from one row plus one number, so the
    decision can be asserted on directly with seeded values and no database
    (rule 10). The printed block, the correction row and the exit code are all
    built from this -- so none of the three can disagree with the others about
    what was decided.

      verdict    TRIM, DROP, OK or REFUSE
      justified  what the payout rows justify holding back
      reserved   what hot_reserved currently says
      leaked     reserved - justified. Positive means held back for nothing;
                 NEGATIVE means hot_reserved is BELOW what is owed, which is the
                 111.05 shape and is reported rather than corrected upward --
                 raising a reservation would tighten a retention floor on the
                 strength of arithmetic nobody has reconciled yet.
      why        the sentence an operator reads, and the sentence the correction
                 row carries. One string, so they cannot drift.
    """

    asset: str
    verdict: str
    justified: float
    reserved: float
    leaked: float
    why: str


def plan_for_asset(row, justified: float, justifying_rows: int) -> Plan:
    """THE DECISION. One inventory row plus what its payout rows justify -> a verdict.

    THE ORDER OF THE BRANCHES IS LOAD-BEARING. The negative-hot_confirmed test
    comes FIRST, because an uncreditable row's hot_reserved figure is not a
    quantity to trim -- the row itself is the thing that should not exist, and
    trimming its reservation to zero would leave the negated payout sum sitting in
    hot_confirmed looking like a balance, which is the defect with its one visible
    symptom removed. That is the patch rule 19 names: it stops the symptom being
    reported without stopping the cause existing.

    A NEGATIVE hot_confirmed IS PROOF AND NOT A HEURISTIC. Every balance read in
    this tree is non-negative by construction -- a Bitcoin-derived `getbalance`,
    Solana's getBalance in lamports, the XRP Ledger's reserve-adjusted drops -- so
    a negative figure cannot have come from one. The only writer that can produce
    it is release_inventory_after_send()'s unfloored subtraction on a row
    refresh_wallet_inventory() has never credited.

    THE REFUSE BRANCH IS THE ONE THAT KEEPS THIS HONEST. An uncreditable row that
    DOES justify a reservation cannot be dropped -- that would discard a live
    obligation for a payout still in flight -- and cannot be trimmed into a figure
    that means anything, because hot_available would then be derived from a
    hot_confirmed that is a payout sum. So it is named, with its rows, and left
    exactly as it stands. Nothing in the operator's measured data takes this
    branch; it exists because the alternative is a tool that guesses when it does.
    """
    reserved = float(row["hot_reserved"] or 0.0)
    confirmed = float(row["hot_confirmed"] or 0.0)
    leaked = reserved - justified
    if confirmed < 0.0:
        if justified > 0.0:
            return Plan(row["asset"], REFUSE, justified, reserved, leaked, (
                f"hot_confirmed is {confirmed!r}, and no balance read on any chain this terminal "
                f"serves can be negative -- so this row was NEVER CREDITED and every write it has "
                f"had was a debit. It would be dropped for that reason, BUT {justifying_rows} payout "
                f"row(s) justify {justified!r} {row['asset']} of reservation, and dropping the row "
                f"would discard a live obligation. NOTHING WAS WRITTEN. Resolve those payout rows "
                f"first, then re-run; this tool will not decide between a row that should not exist "
                f"and a payout that has not left"
            ))
        return Plan(row["asset"], DROP, justified, reserved, leaked, (
            f"hot_confirmed is {confirmed!r}. No balance read on any chain this terminal serves can "
            f"be negative, so this row was NEVER CREDITED: refresh_wallet_inventory() calls "
            f"get_balance(), that call refuses for this asset, and the figure is what "
            f"release_inventory_after_send() left behind by subtracting each payout from a zero it "
            f"found -- the negated sum of this asset's own payouts. No reservation is justified "
            f"({justifying_rows} justifying row(s)), so there is nothing in the row to keep. It is "
            f"DROPPED rather than zeroed: 0.0 would be a claim that the account is empty, and it is "
            f"not -- this process cannot read it, which is what an ABSENT row already means here. "
            f"reserve_inventory() recreates it on the next payout, which is how this one appeared"
        ))
    if leaked > 0.0:
        return Plan(row["asset"], TRIM, justified, reserved, leaked, (
            f"hot_reserved says {reserved!r} {row['asset']} is held back, and the payout rows justify "
            f"{justified!r} across {justifying_rows} row(s) in status {list(RESERVED_STATUSES)} -- so "
            f"{leaked!r} {row['asset']} is reserved against nothing. That is a payout that failed "
            f"without release_inventory_after_send() ever running: the failure path moves the payouts "
            f"row to 'failed' and performs no release. hot_reserved is corrected to {justified!r} and "
            f"hot_available recomputed as hot_confirmed - hot_reserved = "
            f"{confirmed - justified!r}. hot_confirmed is NOT touched -- it comes from the chain and "
            f"the next reconcile cycle overwrites it"
        ))
    if leaked < 0.0:
        return Plan(row["asset"], REFUSE, justified, reserved, leaked, (
            f"hot_reserved says {reserved!r} {row['asset']} but the payout rows justify MORE -- "
            f"{justified!r} across {justifying_rows} row(s) -- so the stored figure is {-leaked!r} "
            f"{row['asset']} BELOW what is owed. NOTHING WAS WRITTEN, and the direction is the reason: "
            f"raising a reservation TIGHTENS swap_terminal/fee_sweep.obligation()'s retention floor, "
            f"which is a posture change on the desk's own money, decided by arithmetic nobody has "
            f"reconciled against a chain. It is also the known shape of a DOUBLE RELEASE -- "
            f"settle_payout.py performed its writes twice until 2026-09-26, and each pass subtracted "
            f"the same reservation. Read the per-row account below and settle it by hand"
        ))
    return Plan(row["asset"], OK, justified, reserved, leaked, (
        f"hot_reserved is {reserved!r} {row['asset']} and the payout rows justify exactly that across "
        f"{justifying_rows} row(s), so there is nothing to correct. This is the row state a second run "
        f"of this tool sees after a correction"
    ))


def available_after(row, reserved: float) -> float:
    """hot_available for a row whose hot_reserved is becoming `reserved`.

    ONE FUNCTION BECAUSE THERE ARE ALREADY TWO SPELLINGS OF THIS SUBTRACTION in
    the tree -- services/payout_service.refresh_wallet_inventory() computes
    `balance - reserved`, and db.py's schema comment states the column holds
    `hot_confirmed - hot_reserved`. A third spelling inlined into the UPDATE here
    would be rule 8's shape, and this one is the one that has to agree with the
    refresh, because the refresh is what overwrites it minutes later. A reader
    comparing the two now compares two call sites of one derivation.
    """
    return float(row["hot_confirmed"] or 0.0) - reserved


def correction_message(row, plan: Plan) -> str:
    """The sentence the `wallet_inventory_corrections` row carries. It must survive without git.

    EVERYTHING NEEDED TO UNDO THIS BY HAND IS IN IT, in prose, beside the three
    original figures the row stores in their own columns: which asset, what the
    row said, what was written, which payout rows justified it, and which status
    set decided that. Rule 1's reasoning applied to a database row -- the only
    place a future reader will look is next to the thing that changed, and for a
    money column "next to" means the same database rather than a commit message.
    """
    return (
        f"{SELF}: {plan.verdict} {row['asset']}. Row as it stood: hot_confirmed={row['hot_confirmed']!r} "
        f"hot_reserved={row['hot_reserved']!r} hot_available={row['hot_available']!r} "
        f"updated_at={row['updated_at']!r}. Reservation justified by payout rows in status "
        f"{list(RESERVED_STATUSES)} = {plan.justified!r}; difference {plan.leaked!r}. {plan.why}"
    )


def apply_plan(db, row, plan: Plan) -> tuple[bool, str]:
    """Write ONE correction and its audit row in ONE transaction. (wrote, sentence).

    THE COMPARE-AND-SWAP IS ON hot_reserved. A row whose reservation changed
    between the read and this write -- a payout worker claiming a swap while this
    tool is printing, which is exactly what a live host does -- matches zero rows,
    and because the correction INSERT is in the same transaction and is skipped
    with it, there is no way to produce an audit row for a correction that did not
    happen. The other direction is the one that matters more and has the same
    guard: no correction without an audit row, which is the one outcome a tool
    that rewrites a money column must never leave behind.

    THE SHAPE IS correct_payout_amounts.apply_correction()'S, DELIBERATELY, and
    the difference is named at both sites (rule 8). That one swaps on the amount
    AND the txid and writes to swap_audit_log, because the object it corrects is a
    payout row and a payout row has a swap. This one swaps on hot_reserved and
    writes to wallet_inventory_corrections, because the object it corrects is keyed
    by asset and has no swap -- db.py's constant carries that argument in full.

    COMMITS PER ASSET. Five assets are five independent corrections, and a batch
    transaction would mean one asset's concurrent writer rolling back four
    corrections that were already right -- the opposite of what a repair tool
    should do when it is interrupted (rule 5: write as you go).
    """
    if plan.verdict == DROP:
        moved = db.execute(
            "DELETE FROM wallet_inventory WHERE asset = ? AND hot_reserved = ? AND hot_confirmed = ?",
            (row["asset"], row["hot_reserved"], row["hot_confirmed"]),
        ).rowcount
        now_confirmed = now_reserved = now_available = None
    else:
        corrected_available = available_after(row, plan.justified)
        moved = db.execute(
            "UPDATE wallet_inventory SET hot_reserved = ?, hot_available = ?, updated_at = ? "
            "WHERE asset = ? AND hot_reserved = ?",
            (plan.justified, corrected_available, utc_now_iso(), row["asset"], row["hot_reserved"]),
        ).rowcount
        now_confirmed = float(row["hot_confirmed"] or 0.0)
        now_reserved = plan.justified
        now_available = corrected_available
    if not moved:
        db.rollback()
        return False, (
            f"REFUSED: {row['asset']} no longer holds hot_reserved={row['hot_reserved']!r}, so the row "
            f"changed between the read and the write -- a payout worker reserving or releasing while "
            f"this ran is the ordinary cause. NOTHING was written and no correction row either; both "
            f"are in one transaction. Re-run the report and read the row again"
        )
    db.execute(
        f"INSERT INTO {INVENTORY_CORRECTIONS_TABLE} "  # noqa: S608 -- checked: INVENTORY_CORRECTIONS_TABLE is a literal constant in db.py and is an IDENTIFIER, which a SQL parameter cannot bind. Every VALUE below is a bound parameter.
        "(asset, action, was_hot_confirmed, was_hot_reserved, was_hot_available, "
        " now_hot_confirmed, now_hot_reserved, now_hot_available, justified_by, message, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            row["asset"], plan.verdict,
            float(row["hot_confirmed"] or 0.0), float(row["hot_reserved"] or 0.0),
            float(row["hot_available"] or 0.0),
            now_confirmed, now_reserved, now_available,
            f"payouts.status IN {list(RESERVED_STATUSES)}",
            correction_message(row, plan),
            utc_now_iso(),
        ),
    )
    db.commit()
    if plan.verdict == DROP:
        return True, (
            f"WROTE: {row['asset']} wallet_inventory row DELETED (it held hot_confirmed="
            f"{row['hot_confirmed']!r}, which was never a balance). A {INVENTORY_CORRECTIONS_TABLE} row "
            f"holding all three original figures was written in the same transaction"
        )
    return True, (
        f"WROTE: {row['asset']} hot_reserved {row['hot_reserved']!r} -> {plan.justified!r}, "
        f"hot_available {row['hot_available']!r} -> {now_available!r}, hot_confirmed UNTOUCHED at "
        f"{row['hot_confirmed']!r}. A {INVENTORY_CORRECTIONS_TABLE} row holding all three original "
        f"figures was written in the same transaction"
    )


def per_row_lines(db, asset: str) -> list[str]:
    """The per-row account for one asset: which payout rows justify, which do not.

    THE WHOLE REASON THIS TOOL PRINTS ROWS AND NOT JUST TOTALS. The operator's GRC
    discrepancy is 111.05290476195842, which is NOT the failed total and so is not
    a clean "every failure leaked" story -- that difference can only be settled by
    looking at which rows are which, and the rows are on their host, not here. A
    tool that printed one difference would hand back a number with no way to act
    on it.

    EVERY row for the asset is printed, not only the ones that justify. A reader
    checking the arithmetic needs the denominator (rule 3): a `failed` row is a
    candidate leak and a `broadcast` row is one that was released, and both have
    to be visible for the sum to be checkable by eye.
    """
    rows = db.execute(PER_ROW_SQL, (asset,)).fetchall()
    if not rows:
        return [f"      (none) -- no payouts row of any status exists for {asset}"]
    # A COLUMN HEADER, because a blank first column cannot be told from a column
    # that is not there. Without it the unmarked rows read as indented further than
    # the marked ones for no stated reason, and the operator has to infer that the
    # gap IS the answer -- which is rule 14's "state what the number means, next to
    # the number" applied to a flag rather than a figure.
    # The widths are the data line's own, below, so the header sits over the
    # columns it names: `id=` plus 5 is 8, then 10 and 1, then 22 and 1.
    lines = [f"      {'justifies':<9}  {'id':<9}{'status':<11}{'amount':<23}swap / txid"]
    for row in rows:
        mark = "JUSTIFIES" if row["justifies"] else "    --   "
        txid = row["txid"] or "(none)"
        lines.append(
            f"      {mark}  id={row['id']:<5} {row['status']:<10} {row['amount']!r:<22} "
            f"swap={row['swap_id']} txid={txid}"
        )
    justify_count = sum(1 for row in rows if row["justifies"])
    lines.append(
        f"      {justify_count} of {len(rows)} row(s) justify a reservation; a row marked JUSTIFIES is "
        f"in status {list(RESERVED_STATUSES)} and is counted into the figure above"
    )
    return lines


def _report_one_asset(db, row, args, *, index: int, total: int) -> tuple[Plan, bool]:
    """Print one asset's block and, under --apply, correct it. (plan, whether a write landed).

    EXTRACTED FROM main() FOR RULE 12'S C901 REASON, WHICH IS RULE 10'S: a main()
    past the ceiling is orchestration that has swallowed decisions, and the fix is
    to extract the thing that decides rather than to raise the ceiling or write a
    noqa (rule 19). main() now names the stages -- announce, refuse, read, report
    each row, summarize -- and this holds what happens to one row.

    THE COUNTER IS IN THE HEADER LINE because the per-row account makes this block
    long: five assets with twenty payout rows between them scrolls, and an operator
    who paged away from the top needs to know whether they are looking at the last
    asset or the second of five (rule 14: a counter and a denominator on anything
    that can run past a couple of lines).
    """
    plan = plan_for_asset(row, float(row["justified"] or 0.0), int(row["justifying_rows"]))
    print(f"  [{index}/{total}] {plan.asset}  {plan.verdict}", flush=True)
    print(labeled("    stored", f"hot_confirmed={row['hot_confirmed']!r} "
                                f"hot_reserved={row['hot_reserved']!r} "
                                f"hot_available={row['hot_available']!r} "
                                f"updated_at={row['updated_at']}"), flush=True)
    print(labeled("    justified", f"{plan.justified!r} across {row['justifying_rows']} payout row(s); "
                                   f"difference {plan.leaked!r}  <- positive means reserved against "
                                   f"nothing"), flush=True)
    print(labeled("    reading", plan.why), flush=True)
    for line in per_row_lines(db, plan.asset):
        print(line, flush=True)

    wrote = False
    if args.apply and plan.verdict in (TRIM, DROP):
        wrote, sentence = apply_plan(db, row, plan)
        print(labeled("    applied", sentence), flush=True)
    elif plan.verdict in (TRIM, DROP):
        print(labeled("    applied", "NOTHING -- this is the report. Nothing was written to any row, "
                                     "and no correction row exists for it"), flush=True)
    print(flush=True)
    return plan, wrote


def _announce(args, db_path: str) -> list[str]:
    """What is about to be read, and the sentence about --apply. BEFORE the work (rule 14).

    THE "MOVES NO COINS" LINE IS PRINTED ON EVERY RUN, dry or not. Every other
    --apply at this project root can move money, so an operator has correctly
    learned to hesitate at the flag; a tool that corrects a column and does not
    say so inherits a caution it does not need, and the cost of that caution here
    is a fee sweep that stays refused.
    """
    return [
        labeled("tool", f"{SELF}  <- reads swap_terminal.db ONLY. No chain, no adapter, no RPC"),
        labeled("database", f"{db_path}  <- the rows reported are THESE"),
        labeled("mode", "--apply: WRITING hot_reserved / hot_available, or DELETING a row"
                if args.apply else
                "REPORT ONLY (dry run). Nothing is written. Add --apply to correct"),
        labeled("moves coins", "NO. This tool BROADCASTS NOTHING and holds no key, on either mode. "
                               "--apply corrects a database column and moves no coins"),
        labeled("justified by", f"payouts.status IN {list(RESERVED_STATUSES)}  <- the ONLY status whose "
                                f"reservation is still owed; 'broadcast' already had its release and "
                                f"'failed' never had one"),
        labeled("audit", f"{INVENTORY_CORRECTIONS_TABLE}, one row per correction, same transaction, "
                         f"holding all three original figures"),
    ]


class Tally(NamedTuple):
    """How many rows took each verdict, plus how many writes actually landed.

    A NAMED TUPLE RATHER THAN FIVE KEYWORD ARGUMENTS, and the reason is rule 12's
    PLR0913 note read the way rule 19 asks: _summary_line() took six parameters
    and the honest fix was to notice that five of them are one thing -- the result
    of a run -- rather than to suppress the finding or raise a ceiling. It also
    removes the failure that shape invites, which is a caller passing `drops` into
    `refusals` at a call site where every argument is an int.

    `written` is separate from `trims + drops` ON PURPOSE. A plan can be refused at
    write time by apply_plan()'s compare-and-swap -- a payout worker reserving
    while this ran -- so "3 to TRIM" and "3 applied" are different claims and the
    output must be able to disagree with itself out loud (rule 13: a stop that
    cannot prove it worked is not a stop).
    """

    assets: int = 0
    trims: int = 0
    drops: int = 0
    refusals: int = 0
    oks: int = 0
    written: int = 0

    def counting(self, verdict: str) -> Tally:
        """This tally with one more row of `verdict`. Returns a new one; counts nothing twice."""
        if verdict == TRIM:
            return self._replace(trims=self.trims + 1)
        if verdict == DROP:
            return self._replace(drops=self.drops + 1)
        if verdict == REFUSE:
            return self._replace(refusals=self.refusals + 1)
        return self._replace(oks=self.oks + 1)

    @property
    def correctable(self) -> int:
        """TRIM plus DROP: the rows --apply would write. REFUSE is deliberately not in it."""
        return self.trims + self.drops


def _summary_line(db_path: str, tally: Tally) -> str:
    """One line whose numbers all carry their denominator (rule 3).

    "DID NOTHING" AND "DID WORK" ARE DIFFERENT SENTENCES (rule 14 / rule 13), and
    the all-clear is spelled out rather than being the absence of findings --
    `0 of 5` with nothing after it reads identically to a query that returned no
    rows.
    """
    if not tally.assets:
        return (f"(none) -- wallet_inventory holds NO rows at all in {db_path}. That is not a clean "
                f"result: this tool found nothing to check, which on a desk that has paid anything "
                f"means the table or the path is wrong")
    if not (tally.correctable or tally.refusals):
        return (f"0 corrections needed across all {tally.assets} wallet_inventory row(s) in {db_path}; "
                f"every hot_reserved equals what its payout rows justify")
    parts = []
    if tally.trims:
        parts.append(f"{tally.trims} to TRIM")
    if tally.drops:
        parts.append(f"{tally.drops} to DROP")
    if tally.refusals:
        parts.append(f"{tally.refusals} REFUSED (nothing written; a human has to look)")
    if tally.oks:
        parts.append(f"{tally.oks} already correct")
    return f"{', '.join(parts)} of {tally.assets} wallet_inventory row(s) in {db_path}"


def _footer(args, db_path: str, tally: Tally) -> list[str]:
    """The lines after the per-asset blocks. Extracted so main() stays orchestration.

    RULE 12'S C901 NOTE APPLIED RATHER THAN SUPPRESSED: main() was 13 branches,
    and the branches were these -- which sentence to print about what was or was
    not written. That is a decision about output, it is now a function that can be
    called with a seeded Tally and asserted on, and main() is left naming the
    stages in order (rule 10).
    """
    lines = [labeled("summary", _summary_line(db_path, tally))]
    if args.apply:
        # THREE SENTENCES AND NOT TWO, because "nothing was written" has two causes
        # and they need different actions (rule 14: make "did nothing" look
        # different from "did work", and from the other kind of did-nothing).
        # Nothing to do is a clean run; every write refused means a payout worker
        # moved the rows under this one and it should be run again.
        if tally.written:
            written = (f"{tally.written} of {tally.correctable} correction(s) applied, each with a "
                       f"{INVENTORY_CORRECTIONS_TABLE} row in the same transaction. Re-run without "
                       f"--apply to confirm it now reports 0 corrections needed")
        elif tally.correctable:
            written = (f"(none) OF {tally.correctable} THAT WERE DUE -- every write was refused by the "
                       f"compare-and-swap, which means the rows moved between the read and the write. "
                       f"No {INVENTORY_CORRECTIONS_TABLE} row was created either. The `applied` lines "
                       f"above say which row, and re-running is the remedy")
        else:
            written = (f"(none), and nothing was due -- no row met TRIM or DROP, so there was nothing "
                       f"to write and no {INVENTORY_CORRECTIONS_TABLE} row was created")
        lines.append(labeled("written", written))
    elif tally.correctable:
        lines.append(labeled("to correct", f"{_rerun_command(args, db_path, '--apply')}"
                                           f"  <- writes a database column. BROADCASTS NOTHING"))
    if tally.refusals:
        lines.append(labeled("needs a human", f"{tally.refusals} row(s) were REFUSED and NOTHING was "
                                              f"written for them. Read each `reading` line above: this "
                                              f"tool will not decide them"))
    return lines


def _rerun_command(args, db_path: str, *extra: str) -> str:
    """The exact command to run next, with --db only when it was given.

    Echoing a --db the operator did not pass would teach them to pass it, and the
    default is read from Config at import time -- so a pasted command carrying it
    would pin a path that a changed SWAP_DB_PATH no longer means.
    """
    flags = [f"python3 {SELF}"]
    if args.db:
        flags.append(f"--db {db_path}")
    flags.extend(extra)
    return " ".join(flags)


def refusal_before_reading(db_path: str) -> str | None:
    """Why not to open this database at all, or None. A read of the filesystem, nothing else.

    A MISSING PATH IS A REFUSAL AND NOT A CREATION. sqlite3.connect() makes an
    empty file, every asset would then read OK against a table with no rows, and
    the tool would report a clean repair of nothing -- the worst form of rule 14's
    "did nothing must not look like did work", on a tool whose entire output is
    reassurance. correct_payout_amounts.py refuses on the same ground and the
    sentence is kept close to its wording on purpose.
    """
    if not Path(db_path).exists():
        return (f"{db_path} does not exist. NOT created -- connecting would make an empty database, "
                f"every asset would read as correct against a table with no rows, and this tool would "
                f"report a clean repair of nothing. Point --db at the real database, or export "
                f"SWAP_DB_PATH")
    return None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(f"{SELF}: report, and with --apply repair, a wallet_inventory reservation the "
                     f"payout rows do not justify. BROADCASTS NOTHING ON EITHER MODE."),
    )
    parser.add_argument("--db", default="", help=f"database to repair (default: {Config.DB_PATH})")
    parser.add_argument("--apply", action="store_true",
                        help="WRITE the corrections. This moves no coins: it corrects hot_reserved and "
                             "hot_available, or deletes a row that was never credited, and records every "
                             "original figure. Without it nothing is written.")
    parser.add_argument("--asset", default="",
                        help="limit the report to one asset (e.g. GRC). Omit for every row.")
    return parser


def main(argv: list[str] | None = None) -> int:
    started = time.monotonic()
    args = build_parser().parse_args(argv)
    db_path = args.db or str(Config.DB_PATH)
    for line in _announce(args, db_path):
        print(line, flush=True)
    print(flush=True)

    refusal = refusal_before_reading(db_path)
    if refusal:
        print(f"REFUSED: {refusal}", file=sys.stderr, flush=True)
        return 2

    tally = Tally()
    with db_session(db_path) as db:
        # The corrections table is ensured here as well as in db.apply_migrations(),
        # from the SAME constant, so a tool run against a database whose app has not
        # restarted still has somewhere to put its audit row. One spelling of the
        # DDL, two appliers -- which is rule 8 satisfied rather than a duplicate.
        db.executescript(INVENTORY_CORRECTIONS_SQL)
        db.commit()

        rows = [row for row in db.execute(JUSTIFIED_SQL).fetchall()
                if not args.asset or row["asset"] == args.asset]
        if args.asset and not rows:
            print(f"  (none) -- no wallet_inventory row for asset {args.asset!r}. Run without --asset "
                  f"to see which assets have one", flush=True)

        tally = tally._replace(assets=len(rows))
        for index, row in enumerate(rows, start=1):
            plan, wrote = _report_one_asset(db, row, args, index=index, total=len(rows))
            tally = tally.counting(plan.verdict)._replace(written=tally.written + (1 if wrote else 0))

    for line in _footer(args, db_path, tally):
        print(line, flush=True)
    print(labeled("took", format_duration(time.monotonic() - started)), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
