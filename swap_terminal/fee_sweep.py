"""Whether a retained fee may be swept, how much of it, and what a sweep must leave behind.

Role: submodule -> the decisions are the functions below, each callable with
      seeded rows (CLAUDE.md rule 10). The writers commit; they are the only
      things in this tree that write `fee_sweeps`.
Reads: swap_terminal.db -- `fee_sweeps`, `swaps` and `wallet_inventory`, plus
      whatever fee_ledger.py reads for the accrual. It opens no socket: a balance
      is passed IN as a float by the caller, never read here. No chain, no
      daemon, no price feed, no file, no environment.
Writes: `fee_sweeps` only, through record_sweep_intent(), record_sweep_broadcast()
      and record_sweep_failed(). Nothing else -- no swap, no payout, no
      inventory row, no quote, no audit row.
Can move funds: NO. It signs nothing, broadcasts nothing, constructs no adapter
      and imports nothing that can send. It decides whether collect_fees.py may,
      and records what collect_fees.py did.
Mainnet-safe: yes to import and yes to call every reader. The three writers write
      a row in this terminal's own database and reach no chain; the one that
      matters is record_sweep_broadcast(), which must be called INSIDE the
      wallet-unlock context -- see its docstring for the 2026-09-26 run that
      makes that an ordering requirement rather than a preference.

=============================================================================
WHY THIS EXISTS. OPERATOR, 2026-10-04: "i NEED to collect a fee to be profitable"
=============================================================================

The fee is already theirs and already in the hot wallet. services/quote_service.py
charges it as a SUBTRACTION -- `output_amount_estimate = max(gross * (1 - fee_bps
/ 10000.0), 0.0)` -- so the desk keeps 150bps by sending that much less. Nothing
leaves, nothing is booked, and the coin simply stays where the customer's deposit
landed.

Which means that before this module there were two things the desk could not do
with its own revenue, and they are different problems:

  SEE IT      fee_ledger.py answered this on 2026-10-01: `retained =
              realized_gross - paid`, per completed swap, in SQL. show_fees.py
              prints it. That half was done.
  TAKE IT     there was no way at all. The retained fee is indistinguishable from
              a customer deposit awaiting payout by looking at the wallet, because
              it IS the same coin in the same wallet.

This module is the second half, and it is almost entirely about the ONE thing
that can go wrong: sweeping coin that is owed to a customer.

=============================================================================
THE SAFETY PROPERTY, AND IT IS THE ONLY ONE THAT MATTERS HERE
=============================================================================

A sweep that strands a payout is worse than no sweep at all. No sweep costs the
desk a delay; a stranded payout means a customer's deposit is confirmed,
irreversible and already ours, and the coin that would have paid them has been
sent somewhere else. That is the 2026-10-03 failure services/payout_capacity.py
is named after -- a 0.001 BTC deposit taken against a 9049.69 GRC payout and a
3780.09 GRC wallet -- except caused by us rather than by an unfunded wallet, and
on purpose rather than by omission.

So the arithmetic below is stated in full, printed on screen by collect_fees.py,
and REFUSES rather than warns:

    unswept   = accrued - swept            what the desk is owed by itself
    obligated = what the wallet must keep  see obligation() -- the floor
    headroom  = balance - obligated - fee  what could leave without harm
    refuse when the sweepable amount, quantized to what the chain can express,
                  exceeds the headroom

AND THE FAIL-CLOSED DIRECTION IS THE OPPOSITE OF payout_capacity's, WHICH IS WHY
IT IS WRITTEN HERE RATHER THAN REUSED. services/payout_capacity.
why_the_payout_cannot_be_funded() returns `unchecked` -- warn and proceed -- for a
chain whose payable balance cannot be read, because by the time it is asked the
customer's deposit is already credited and refusing does not give them their
money back. A SWEEP has no such sunk cost: nothing is owed to anybody by not
sweeping, so an unreadable balance REFUSES. Same three-state vocabulary, opposite
resolution of the middle state, and the difference is named at both sites (rule 8).

=============================================================================
WHAT COUNTS AS AN OBLIGATION, WHICH IS A CHOICE AND IS MEASURED
=============================================================================

services/swap_view.TERMINAL_STATUSES is `{completed, under_review, failed}`, so
the swaps a worker will still pay are everything else: awaiting_deposit,
deposit_seen, confirming, payout_pending, paying. Those are the obligation floor,
each at payout_amount() plus its own network_fee_reserve, because the wallet has
to hold the payout AND the chain fee that sends it (the reason
payout_capacity.why_the_payout_cannot_be_funded() adds rather than subtracts it).

`under_review` AND `failed` ARE NOT IN THE FLOOR, AND THEY ARE REPORTED BESIDE IT
RATHER THAN DROPPED. Both can become payouts again -- resolve_halted_swap.py hands
a halted swap to payout_worker and settle_payout.py corrects a failed one -- so
they are not nothing. They are excluded from the FLOOR for two measured reasons:

  - each of those paths carries its own funding check at the moment it runs.
    resolve_halted_swap.funding_line() calls why_the_payout_cannot_be_funded()
    immediately before the operator authorizes the resolution, which is a better
    place to ask than here: it asks about the balance as it will be THEN, after
    any sweep.
  - counting them would block every sweep on this desk indefinitely. Measured on
    the operator's host 2026-10-03 and recorded in correct_payout_amounts.py's
    header: 23 payout rows, 8 of them `status=failed` with no txid. A floor that
    retains for every row that could conceivably be revived is a floor that never
    lets a profitable desk take its own fee, which is the outcome this whole
    module exists to prevent.

So they print on their own line with their own total, and the operator decides.
Rule 16's line, in the place it belongs: the refusal is mechanical, the judgment
about a halted swap is theirs.

=============================================================================
WHY THE AMOUNT IS DERIVED AND THERE IS NO --amount FLAG
=============================================================================

The sweep amount is `accrued - swept`, both of which are queries. A flag letting
an operator name a figure would be a second authority for what the desk is owed,
and the two would disagree the first time somebody typed a number from a report
they ran yesterday -- rule 8's duplicate on the one line that moves money out.
What the operator decides is WHETHER to sweep (`--apply`) and WHERE to (the
destination variable). What they do not decide is how much, because that is
already written down in rows.

=============================================================================
WHY THE SELECTION IS SQL AND THE PER-ROW AMOUNT IS NOT (rules 8, 20)
=============================================================================

Rule 20 says a filter, a join or a ranking over rows already in the database is a
query, and both halves of the accrual are: swept_by_asset() is one GROUP BY, and
OPEN_SWAPS_SQL selects and sums in SQL. The per-swap PAYOUT AMOUNT is the one
thing that stays in Python, and not because SQLite could not multiply:
services/payout_service.payout_amount() is the authority for what a customer
receives, its docstring records the 2026-10-03 run where re-deriving that figure
would have paid 0.0985 on a swap quoted 0.0975, and a SQL copy of it here would be
a second spelling of the expression that decides every payout in this terminal.
Rule 20's own words -- it governs "which way to resolve a duplicate you are
already touching" -- resolve this toward the existing single authority. The
obligation floor must agree with what the payout worker will actually send, and
the only way to guarantee that is to ask the same function.
"""

from __future__ import annotations

from typing import NamedTuple

from chains.payout_quantization import quantize_for_chain
from config import FEE_SWEEP_DESTINATION_TEMPLATE
from services.helpers import utc_now_iso
from services.payout_service import payout_amount

#: The three states a `fee_sweeps` row can hold. Spelled once so the writers below
#: and the readers in collect_fees.py cannot drift (rule 8).
SWEEP_CREATED = "created"
SWEEP_BROADCAST = "broadcast"
SWEEP_FAILED = "failed"

#: How much of this asset has already left, per asset. ONE GROUP BY rather than a
#: Python sum over fetched rows (rule 20).
#:
#: WHICH SWEEPS COUNT AS MONEY ALREADY GONE, and the predicate is written INLINE
#: rather than held in a Python list and interpolated in. That choice is
#: fee_ledger.py's, made for this exact lint rule and recorded in its own header:
#: an f-string here earns ruff's S608, and the available answers were a `noqa`
#: claiming the interpolated value is an identifier this repo controls, or not
#: interpolating. Rule 19 forbids the first. Nothing is lost -- what a constant
#: would have given a test is a string to compare against, and
#: tests/test_collect_fees.py instead seeds a sweep row in each of the three states
#: and asserts which ones move the swept total, which is the stronger claim (the
#: behavioral-verification principle).
#:
#: 'created' IS COUNTED, and that is the direction that cannot lose money. A row
#: INSERTed before a send that never reported back is money POSSIBLY on chain with
#: no txid, exactly like a `payouts` row in 'created'; counting it as still
#: unswept would re-send it. The cost of over-counting is a fee that stays in the
#: wallet until somebody looks; the cost of under-counting is a double send that
#: comes out of customer deposits. 'failed' is NOT counted, so a send the daemon
#: refused leaves the fee sweepable -- the same reason 'failed' is absent from
#: db.PAYOUT_LIVE_STATUSES.
SWEPT_SQL = """
SELECT asset                  AS asset,
       SUM(amount)            AS swept,
       COUNT(*)               AS sweeps
FROM fee_sweeps
WHERE status IN ('created', 'broadcast')
GROUP BY asset
ORDER BY asset
"""

#: Every sweep whose send never reported back. The alarm, and the same shape as
#: services/admin_view.unresolved_payouts() for the same reason.
UNRESOLVED_SWEEPS_SQL = """
SELECT id, asset, destination_address, amount, created_at
FROM fee_sweeps
WHERE status = 'created'
ORDER BY id ASC
"""

#: The swaps a worker will still pay out in `asset`, with every column
#: payout_amount() reads plus the reserve the wallet needs to send it.
#:
#: THE THREE TERMINAL STATUSES ARE SPELLED HERE, which is a second copy of
#: services/swap_view.TERMINAL_STATUSES and is the one place in this module that
#: admits to being one. Interpolating that set would earn ruff's S608 and rule 19
#: forbids answering a finding with a suppression, so the copy is in the SQL and
#: this comment carries the obligation: tests/test_collect_fees.py seeds ONE swap
#: in EVERY status services/swap_view.STATUS_MEANINGS knows about and asserts that
#: obligation() retains for exactly the non-terminal ones. A status added to that
#: vocabulary, or moved into or out of TERMINAL_STATUSES, therefore fails the suite
#: by name rather than quietly changing what this wallet is allowed to keep. That
#: is rule 11's "did every consumer follow automatically?" answered by a test
#: instead of by a derivation, which is the trade S608 forces and is why it is
#: written down rather than left to be noticed.
OPEN_SWAPS_SQL = """
SELECT id,
       status,
       to_asset,
       output_amount_estimate,
       actual_input_amount,
       expected_input_amount,
       network_fee_reserve
FROM swaps
WHERE to_asset = ?
  AND status NOT IN ('completed', 'under_review', 'failed')
ORDER BY created_at ASC
"""

#: The swaps that are terminal but could be REVIVED into a payout: a halted one
#: through resolve_halted_swap.py, a failed one through settle_payout.py. Reported
#: beside the floor, never added to it -- see this module's docstring for the two
#: measured reasons.
#:
#: 'completed' is the only status excluded, because a completed swap has its payout
#: broadcast and a broadcast cannot be re-sent.
REVIVABLE_SWAPS_SQL = """
SELECT id,
       status,
       output_amount_estimate,
       actual_input_amount,
       expected_input_amount,
       network_fee_reserve
FROM swaps
WHERE to_asset = ? AND status IN ('under_review', 'failed')
ORDER BY created_at ASC
"""


class Obligation(NamedTuple):
    """What the wallet must keep for `asset`, and everything that decided it.

    Every field is on the screen, because the refusal this feeds is the one an
    operator has to be able to check by eye (rule 14: state what the number means,
    next to the number).

      floor        the figure that actually protects payouts: the larger of
                   `open_total` and `reserved`. A reservation standing without an
                   open swap row should be impossible, and honoring it anyway
                   costs nothing while assuming it away could cost a payout.
      open_total   sum over the non-terminal swaps of payout_amount() + reserve
      open_swaps   how many. The denominator for `open_total` (rule 3) -- a floor
                   with no count behind it cannot be told from one large swap.
      reserved     wallet_inventory.hot_reserved: payouts claimed and not yet
                   sent, written by payout_service.reserve_inventory()
      revivable    what a halted or failed swap would need if it were resolved.
                   NOT in the floor; reported so the operator can see it.
      revivable_swaps  its denominator.
    """

    asset: str
    floor: float
    open_total: float
    open_swaps: int
    reserved: float
    revivable: float
    revivable_swaps: int


def swept_by_asset(db) -> dict[str, tuple[float, int]]:
    """Per asset, (how much has been swept, in how many sweeps). {} when none have.

    An absent asset means no sweep has EVER been recorded for it, which is not the
    same fact as a sweep of zero -- collect_fees.py prints the two differently
    (rule 14).
    """
    return {
        str(row["asset"]): (float(row["swept"] or 0.0), int(row["sweeps"]))
        for row in db.execute(SWEPT_SQL).fetchall()
    }


class Retention(NamedTuple):
    """One asset's fee, in the three figures an operator needs side by side.

    THE ONE DERIVATION TWO SURFACES READ, AND THAT IS WHY IT IS HERE RATHER THAN
    IN EITHER (rule 8). services/admin_view.py renders it in the hot-wallet panel
    and show_fees.py prints it under each asset total; the alternative was both of
    them joining fee_ledger's accrual to `fee_sweeps` themselves, which is two
    spellings of one subtraction on the page an operator reads to tell profit from
    float.

      accrued    fee_ledger.py's `retained` summed over this asset's delivered
                 payouts. GROSS: the chain fees already paid to DELIVER those
                 payouts are not subtracted (show_payout_fees.py measures those).
      swept      what `fee_sweeps` records as having left, counting 'created' as
                 gone -- see SWEPT_SQL for why that direction.
      sweepable  accrued - swept. NOT quantized here: this is a reporting figure
                 and the sendable amount is fee_sweep.sweep_plan()'s, which is the
                 only place that decides what moves. A reader comparing the two
                 sees the same number at two precisions.
      swaps      the denominator for `accrued`, and `sweeps` for `swept` (rule 3).
    """

    asset: str
    accrued: float
    swept: float
    sweepable: float
    swaps: int
    sweeps: int


def retention_by_asset(db) -> list[Retention]:
    """Every destination asset's fee: earned, collected, and still collectable.

    Reads only. Calls fee_ledger.fee_rows() and fee_ledger.asset_totals() for the
    accrual rather than deriving it -- that SELECT is the one place the arithmetic
    exists -- and swept_by_asset() for the other half.

    IMPORTED INSIDE THE FUNCTION so that importing this module costs nothing a
    decision needs: fee_ledger is only wanted by this one reporting helper, and
    every writer and every verdict above is reachable without it. The modules do
    not import each other at top level in either direction, so there is no cycle
    to avoid here -- this is about keeping the decision path's import surface to
    what the decisions use (rule 10).

    An asset with no delivered payout is ABSENT rather than present with zeroes,
    which is the same choice fee_ledger.asset_totals() makes: "nothing has been
    delivered in this asset" and "the fee was zero" are different facts and a
    caller must be able to tell them apart (rule 14).
    """
    from fee_ledger import (  # noqa: PLC0415 -- checked: see this function's docstring. It is a reporting-only dependency and importing it at module scope would put it on every decision path that reads this file.
        asset_totals,
        fee_rows,
    )

    already = swept_by_asset(db)
    rows = []
    for total in asset_totals(fee_rows(db)):
        swept, sweeps = already.get(total.asset, (0.0, 0))
        rows.append(Retention(
            asset=total.asset,
            accrued=total.retained,
            swept=swept,
            sweepable=total.retained - swept,
            swaps=total.swaps,
            sweeps=sweeps,
        ))
    return rows


def unresolved_sweeps(db) -> list[dict]:
    """Sweeps recorded as intended and never reported sent. Money possibly on chain.

    Nothing retries these and nothing resolves them automatically, deliberately:
    the only honest way out is for somebody to look at the chain and say which
    happened. Returned so collect_fees.py can print them as an alarm instead of
    folding them into a total.
    """
    return [dict(row) for row in db.execute(UNRESOLVED_SWEEPS_SQL).fetchall()]


def _owed_for(rows) -> tuple[float, int]:
    """(what these swaps need from the wallet, how many there are). THE SUM.

    payout_amount() per row rather than an expression here, and the reserve added
    rather than subtracted -- both for the reasons in this module's docstring. It
    is a loop over already-fetched rows with no I/O and no try/except, so there is
    no half-applied state for a raising row to leave behind: a KeyError on a
    malformed row aborts the whole figure rather than silently producing a smaller
    floor, which is the direction that refuses rather than the direction that
    sweeps.
    """
    total = 0.0
    count = 0
    for row in rows:
        amount, _how = payout_amount(row)
        total += amount + float(row["network_fee_reserve"] or 0.0)
        count += 1
    return total, count


def obligation(db, asset: str) -> Obligation:
    """What `asset`'s hot wallet must retain. THE DECISION THE REFUSAL IS BUILT ON.

    At the bottom where it can be called with seeded rows and no chain (rule 10):
    it reads three tables and takes no balance, no adapter and no network.
    """
    open_total, open_swaps = _owed_for(
        db.execute(OPEN_SWAPS_SQL, (asset,)).fetchall()
    )
    revivable, revivable_swaps = _owed_for(db.execute(REVIVABLE_SWAPS_SQL, (asset,)).fetchall())
    row = db.execute(
        "SELECT hot_reserved FROM wallet_inventory WHERE asset = ?", (asset,)
    ).fetchone()
    reserved = float(row["hot_reserved"] or 0.0) if row else 0.0
    return Obligation(
        asset=asset,
        floor=max(open_total, reserved),
        open_total=open_total,
        open_swaps=open_swaps,
        reserved=reserved,
        revivable=revivable,
        revivable_swaps=revivable_swaps,
    )


#: The four verdicts. A sweep either happens, is refused, or there is nothing to do,
#: and the third is split in two because "nothing has accrued" and "it has all been
#: swept already" are different facts about a desk (rule 14: make "did nothing" look
#: different from "did work", and from the other kind of did-nothing).
SWEEP = "SWEEP"
REFUSE = "REFUSE"
NOTHING_ACCRUED = "NOTHING ACCRUED"
ALREADY_SWEPT = "ALREADY SWEPT"


class SweepPlan(NamedTuple):
    """What would happen to one asset's retained fee, and the sentence for it.

    `amount` is None for every verdict that sends nothing, so a caller cannot
    accidentally broadcast a figure for a refused or skipped asset -- the same
    shape, for the same reason, as correct_payout_amounts.Correction.corrected.
    """

    asset: str
    verdict: str
    amount: float | None
    destination: str
    why: str
    #: What the chain will charge to move it, from <ASSET>_NETWORK_FEE_RESERVE.
    #: Carried on the plan rather than looked up again by the printer, so the
    #: figure in the refusal and the figure on the line cannot differ.
    chain_fee: float


class Accrual(NamedTuple):
    """One asset's fee ledger: what it earned, what has already left, in how many sweeps.

    GROUPED RATHER THAN PASSED LOOSE, because ruff's PLR0913 put sweep_plan() at
    eight parameters and the honest answers were to remove arguments or to group
    them -- a `noqa` claiming eight is fine is the suppression rule 19 forbids.
    Grouping is the better of the two here: these three are one fact about one
    asset, they are read from the same two queries, and `Accrual(asset=...,
    accrued=..., swept=...)` at a call site names what each number is where a bare
    float could be swapped with its neighbor silently.

    `sweeps` is the DENOMINATOR for `swept` (rule 3): a swept total with no count
    behind it cannot be told from one large sweep, and it is how collect_fees.py
    says "nothing has ever been swept" differently from "a sweep of zero".
    """

    asset: str
    accrued: float
    swept: float
    sweeps: int


class WalletCeiling(NamedTuple):
    """How much of an asset could leave this wallet right now, and how that was read.

    EXACTLY services/payout_capacity.largest_fundable_payout()'S RETURN, in a
    tuple, with its -1.0 NOT-ESTABLISHED sentinel already translated to None. The
    translation happens at the one boundary -- from_ceiling() below -- rather than
    inside sweep_plan(), so this module holds no magic number whose meaning lives
    in another file, and a caller cannot pass -1.0 in and have it read as a wallet
    holding less than nothing.
    """

    amount: float | None
    how: str

    @classmethod
    def from_largest_fundable_payout(cls, amount: float, how: str) -> WalletCeiling:
        """Translate that function's (float, str) pair, sentinel included.

        -1.0 means NOT ESTABLISHED there -- chosen over 0.0 precisely because 0.0
        is a legitimate answer for an empty wallet and the two must not render the
        same way -- so `< 0` and not `== -1.0`: a future sentinel of -2.0 must not
        arrive here as a readable balance.
        """
        return cls(None if amount < 0 else amount, how)


def sweep_plan(
    *,
    accrual: Accrual,
    destination: str,
    ceiling: WalletCeiling,
    owed: Obligation,
    chain_fee: float,
) -> SweepPlan:
    """What to do with one asset's retained fee. THE DECISION.

    Pure: no database, no adapter, no environment, no clock. Every input is a
    number or a string the caller read, which is what makes each branch assertable
    with seeded values (rule 10).

    `ceiling` CARRIES services/payout_capacity.largest_fundable_payout()'S ANSWER
    AND ITS OWN SENTENCE, passed in rather than read here.
    That function is already the authority for "how much of this asset could leave
    this wallet right now" -- it is what swap_readiness.py and open_swap.py print
    -- so reading a balance again here would be a second answer to one question
    (rule 8), and the two could disagree while sitting on the same screen. None
    means NOT ESTABLISHED, which is that function's -1.0 sentinel translated at the
    boundary by the caller; translating it there rather than accepting -1.0 here
    keeps this decision free of a magic number whose meaning lives in another
    module.

    THE ORDER OF THE BRANCHES IS THE DESIGN, and the first two come before the
    destination check on purpose: an asset with nothing to sweep must not nag about
    an unset variable it does not need. An operator who has never paid out in LTC
    should not be told to export LTC_FEE_SWEEP_DESTINATION.

      nothing accrued   no completed payout in this asset has retained anything, so
                        there is no fee. NOT the same as zero: show_fees.py draws
                        the same distinction for the same reason.
      already swept     the accrual is real and has been collected. This is the
                        state a SECOND RUN sees, and it is what makes this tool
                        idempotent -- see fee_sweeps' DDL in db.py.
      no destination    REFUSED, naming the variable. A defaulted fee address is a
                        transaction to somewhere nobody chose and cannot be undone.
      ceiling unknown   REFUSED. The opposite resolution from services/
                        payout_capacity.py's `unchecked`, and this module's
                        docstring says why: refusing a sweep costs nobody anything,
                        where refusing a payout strands a credited deposit.
      would strand      REFUSED, with the whole arithmetic in the sentence. THE
                        PROPERTY THIS MODULE EXISTS FOR.
      sweep             the amount, the destination, and the chain fee named.

    THE CHAIN FEE DOES NOT REFUSE, AND THAT IS DELIBERATE (rule 16). A fee smaller
    than what the chain charges to move it is reported with both numbers so the
    operator can read the comparison off the screen, and swept anyway if they say
    so. Measured costs, from this repository's own config.py and
    chains/payout_quantization.py: GRC is a flat 0.001 (9 of 9 of the operator's
    payouts, zero variance), BTC 0.0000282 at one input and 0.00084240 at 2701,
    LTC about 0.0000150 per input, XRP 10 drops, SOL 5,000 lamports. Whether 0.001
    GRC is worth paying to move 0.002 GRC is a judgment about a desk's own money,
    not a safety question, and inventing a threshold here would be this module
    deciding something nobody asked it to.
    """
    # `accrued` AND `swept` ARE SEPARATE PARAMETERS AND THE DIFFERENCE IS COMPUTED
    # HERE, not passed in. A caller handing over one `unswept` number could not
    # distinguish the first two verdicts below -- both of them see a non-positive
    # remainder -- and those are the two states an operator most needs told apart:
    # one says this desk has earned nothing in this asset, the other says it has
    # earned and already collected it. A single input would have made that
    # distinction unaskable, which is the shape of defect
    # services/payout_service.payable_assets() records about its own old signature.
    asset = accrual.asset
    unswept = accrual.accrued - accrual.swept
    # QUANTIZED HERE, IN THE DECISION, AND NOWHERE ELSE (rule 8).
    #
    # THE DEFECT THIS FIXES WAS FOUND BY RUNNING THE TOOL TWICE, 2026-10-04, and it
    # is rule 14's "did nothing must not look like did work" in its most annoying
    # form. The accrued fee on the seeded live swap is 1.3593541055132334 GRC and
    # the chain can express 1.35935410, so a first sweep records and sends the
    # quantized figure and leaves 5.5e-9 GRC -- half a hundredth of a satoshi --
    # as the remainder. Unquantized, that remainder is `> 0`, so EVERY subsequent
    # run reported GRC under SWEEP and offered a command to collect it, and
    # --apply then did nothing because the amount quantizes to zero at the send.
    # A tool whose report says there is money to collect forever is one an
    # operator stops reading.
    #
    # So the amount this plan carries is the amount a chain could actually move,
    # and a remainder that cannot be moved reads as ALREADY SWEPT with the residue
    # named. chains/payout_quantization.quantize_for_chain() is idempotent and
    # never rounds upward -- measured over 60,016 amounts per chain -- so the
    # figure here is the figure the adapter will send, and collect_fees.apply_sweep()
    # passes it through verbatim rather than quantizing a second time.
    sendable, quantization = quantize_for_chain(unswept, asset)
    if accrual.accrued <= 0:
        return SweepPlan(asset, NOTHING_ACCRUED, None, destination, (
            f"no delivered payout in {asset} has retained anything, so there is no fee to collect. That "
            f"is NOT a fee of zero -- nothing has been delivered in {asset} to charge one on"
        ), chain_fee)
    if sendable <= 0:
        return SweepPlan(asset, ALREADY_SWEPT, None, destination, (
            f"every {asset} fee the ledger shows has already been swept, so this run sends nothing. This "
            f"is the state a SECOND run sees, and it is what makes the tool safe to re-run. The "
            f"remainder is {unswept!r} {asset}, which is below what this chain can express "
            f"({quantization or 'nothing is left at all'}) -- it stays in the wallet and becomes "
            f"sweepable once more accrues on top of it"
        ), chain_fee)
    if not destination:
        return SweepPlan(asset, REFUSE, None, destination, (
            f"REFUSED: {FEE_SWEEP_DESTINATION_TEMPLATE.format(asset=asset)} is not set, so there is no "
            f"address to sweep {asset} to. Nothing is defaulted and nothing is guessed: a fee address "
            f"this tool chose would be a final, unrecoverable transaction to somewhere nobody picked. "
            f"Export that variable with an address you control, then run this again"
        ), chain_fee)
    if ceiling.amount is None:
        return SweepPlan(asset, REFUSE, None, destination, (
            f"REFUSED: the {asset} payout wallet's balance was NOT established ({ceiling.how}), so "
            f"whether a sweep would leave it able to fund {owed.open_swaps} open swap(s) could not be "
            f"checked. Refusing costs nothing -- the fee stays in the wallet and stays sweepable -- "
            f"where sweeping on an unverified balance could strand a payout whose deposit is already "
            f"ours and irreversible"
        ), chain_fee)
    # THE SUBTRACTION, AND THE WHOLE SAFETY PROPERTY IS IN THIS ONE LINE.
    #
    # `ceiling` already has the sweep transaction's own chain fee taken out of the
    # balance -- that is what services/payout_capacity.largest_fundable_payout()
    # returns, and `ceiling.how` is its own sentence saying so. Subtracting
    # chain_fee again here would double-charge it and under-sweep, which is the
    # safe direction and still wrong; printing it twice would be worse, because the
    # operator would read two different numbers for one fee.
    headroom = ceiling.amount - owed.floor
    if sendable > headroom:
        return SweepPlan(asset, REFUSE, None, destination, (
            f"REFUSED: sweeping {sendable} {asset} would leave this wallet unable to fund what it owes. "
            f"The arithmetic: {ceiling.how} = {ceiling.amount} {asset} could leave, minus {owed.floor} "
            f"{asset} obligated, leaves headroom {headroom} {asset} -- and the unswept fee is "
            f"{sendable} {asset}, which is {sendable - headroom} {asset} MORE than that. The obligated "
            f"figure is the larger of {owed.open_total} {asset} for {owed.open_swaps} open swap(s) "
            f"(each at the payout services/payout_service.payout_amount() will compute, plus that "
            f"payout's own chain fee) and {owed.reserved} {asset} already reserved in "
            f"wallet_inventory. A sweep that strands a payout is worse than no sweep: the customer's "
            f"deposit is already confirmed and irreversible, and not paying them does not give it "
            f"back. Fund the wallet, or let the open swaps settle and run this again"
        ), chain_fee)
    return SweepPlan(asset, SWEEP, sendable, destination, (
        f"{sendable} {asset} of retained fee would be swept to {destination}"
        f"{' (' + quantization + ')' if quantization else ''}. The arithmetic: "
        f"{ceiling.how} = {ceiling.amount} {asset} could leave, minus {owed.floor} {asset} obligated for "
        f"{owed.open_swaps} open swap(s), leaves headroom {headroom} {asset} -- which covers it with "
        f"{headroom - sendable} {asset} to spare. The chain charges about {chain_fee} {asset} to move "
        f"it, paid by the wallet out of its own inputs rather than deducted from this amount, and it "
        f"is already out of the ceiling above"
    ), chain_fee)


def record_sweep_intent(db, asset: str, destination: str, amount: float) -> int:
    """INSERT the 'created' row and COMMIT. Returns its id. Call BEFORE the send.

    DURABLE BEFORE THE BROADCAST, which is the ordering
    services/payout_service.process_pending_payouts() uses and records the reason
    for: a row written after a send cannot exist for a send that was interrupted,
    and the interrupted case is the one where money is on chain with nothing
    saying so. Written first, the worst outcome is a 'created' row for a send that
    never happened -- which costs an unswept fee until somebody looks, and
    collect_fees.py prints every one of them as an alarm.

    IT IS COUNTED AS SWEPT THE MOMENT IT COMMITS -- SWEPT_SQL's predicate includes
    'created' -- so a
    second run of collect_fees.py cannot re-send this amount even if this process
    dies between here and the broadcast. That is the idempotency, and it is a
    committed row rather than a lock or a flag precisely so it survives the process
    that wrote it.
    """
    cursor = db.execute(
        "INSERT INTO fee_sweeps (asset, destination_address, amount, txid, status, created_at, sent_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (asset, destination, amount, None, SWEEP_CREATED, utc_now_iso(), None),
    )
    db.commit()
    return int(cursor.lastrowid)


def record_sweep_broadcast(db, sweep_id: int, txid: str) -> bool:
    """Make a delivered sweep durable. Commits. CALL INSIDE THE UNLOCK CONTEXT.

    THE PLACEMENT IS THE WHOLE POINT OF THIS FUNCTION, and it is not a style
    preference. services/payout_service._record_broadcast() sits inside the
    `with payout_unlock_context(...)` block, before the re-lock can raise, because
    on 2026-09-26 it did NOT: the send succeeded, 55.52645238 GRC left the wallet
    with txid 3e09dc9cfd7a61da..., and then the context's restore raised because
    Gridcoin refuses a staking unlock timeout of 0. Control jumped from the `with`
    straight to the except clause, the txid was discarded, and the swap was marked
    failed with no txid. Money out, no record -- the worst outcome available on a
    path that moves funds.

    A sweep has exactly that shape: GRC needs the wallet fully unlocked, so a
    sweep's send is wrapped in the same context, and a txid recorded after the
    `with` would be lost to the same re-lock. So collect_fees.py calls this inside
    it and sets its `recorded` flag only after this returns, which is what lets it
    treat a failed re-lock as a wallet problem rather than as a lost sweep.

    COMPARE-AND-SWAP ON THE STATUS AND THE txid, so a row that something else
    already resolved matches zero rows and this returns False rather than
    overwriting a recorded txid with a second one. Returned rather than raised:
    the money is already on chain by the time this is called, and an exception here
    would turn a delivered sweep into a traceback.
    """
    moved = db.execute(
        "UPDATE fee_sweeps SET txid = ?, status = ?, sent_at = ?"
        " WHERE id = ? AND status = ? AND txid IS NULL",
        (txid, SWEEP_BROADCAST, utc_now_iso(), sweep_id, SWEEP_CREATED),
    ).rowcount
    db.commit()
    return bool(moved)


def record_sweep_failed(db, sweep_id: int, reason: str) -> bool:
    """Mark a sweep whose send RAISED, so the fee stays sweepable. Commits.

    'failed' is outside SWEPT_SQL's predicate, so this releases the amount back
    into the unswept figure and the next run offers it again. That is only correct
    when nothing reached a chain, which is why collect_fees.py calls this solely
    from the branch where the send raised BEFORE any txid came back -- the same
    distinction services/payout_service.py draws between a failed send and a failed
    re-lock after a recorded broadcast.

    THE REASON IS STORED NOWHERE, and that is a real limitation stated rather than
    hidden: `fee_sweeps` has no failed_reason column, because `payouts` has none
    either and the sentence belongs where the operator is already reading --
    collect_fees.py prints it, and `reason` is taken here so the signature says the
    caller must have one. Adding a column would make this table the second place in
    the tree that records why a send failed.
    """
    moved = db.execute(
        "UPDATE fee_sweeps SET status = ? WHERE id = ? AND status = ? AND txid IS NULL",
        (SWEEP_FAILED, sweep_id, SWEEP_CREATED),
    ).rowcount
    db.commit()
    return bool(moved)
