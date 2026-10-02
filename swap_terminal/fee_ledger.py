"""What fee each completed swap ACTUALLY kept, derived from the rows it left behind.

Role: submodule -> the derivation is the SQL constant below (the decision)
Reads: swap_terminal.db -- the `swaps` and `payouts` tables, nothing else. No
       chain, no daemon, no price feed, no file. It opens no socket.
Writes: NOTHING. No INSERT, no UPDATE, no DELETE, no view, no schema. Every
       statement in this file is a SELECT.
Can move funds: no. It reports on money that has already moved -- every row it
       returns is a payout that was broadcast and cannot be unsent. It names no
       command that writes, and it does not change a quote, a rate or a fee.
Mainnet-safe: yes. It reads whatever database it is handed and reaches nothing
       else.

WHY THIS FILE EXISTS, AND THE NUMBER THAT PROMPTED IT.

The operator asked, 2026-10-01, for the swap fee to be "comparable to other
exchanges out there". Answering that needs the fee this terminal charges, and
measured 2026-10-01 nothing in the tree could say what it was. `fee_bps` is
stored per swap and `DEFAULT_FEE_BPS=150` is the schedule, but the fee is never
materialized as a number anywhere: not in a column, not in a report, not on the
swap page, not in a worker's cycle line. The only place it existed was inside
one expression in services/quote_service.create_quote(), as a multiplication
whose result was immediately folded into `output_amount_estimate` and never
separated out again.

So the question "what have we earned" had no answer in a system that had
already completed live swaps, and the question "is 150bps competitive" was
being asked about a figure nobody had ever read off a screen. That is rule 14's
failure at the level of a whole quantity rather than a log line.

AND THE SCHEDULE IS NOT THE FEE. THIS IS THE FINDING, AND IT IS WHY THE
DERIVATION BELOW DOES NOT SIMPLY REPRINT `fee_bps`.

Established by reading the two call sites against each other, then pinned by
the seeded tests in tests/test_fee_ledger.py rather than left as a reading:

  services/quote_service.create_quote()   output_amount_estimate =
                                          max(EXPECTED * rate * (1 - f) - R, 0)
  services/deposit_service.refresh_swap_from_chain()
                                          credits on the CONFIRMED total, and
                                          updates actual_input_amount to it --
                                          and NEVER recomputes
                                          output_amount_estimate from it
  services/payout_service.process_pending_payouts()
                                          amount = output_amount_estimate

The payout is therefore priced on what the swap EXPECTED to receive, while the
fee is charged against what it ACTUALLY received, and the two are allowed to
differ by config.AMOUNT_TOLERANCE_PCT -- 0.01, so one percent either side.
Outside that band the swap halts into `under_review` and pays nothing, which is
the gate working. INSIDE the band it pays out at the expected-amount estimate,
and the difference is kept or given away silently.

Algebraically, with G the realized gross (actual * rate), E the quoted gross
(expected * rate), f the fee fraction and R the network fee reserve:

    retained = G - paid = G - (E*(1-f) - R)
    drift    = retained - G*f - R = (1-f)*(G - E)

so the realized fee is the scheduled fee plus `(1-f)` times the deposit
mismatch, and at the tolerance edges with f=0.015 that is +/-98.5bps:

    deposit at 0.99x expected   realized fee ~ 51.5bps   (we gave away 98.5)
    deposit at 1.00x expected   realized fee   150.0bps  (the schedule)
    deposit at 1.01x expected   realized fee ~248.5bps   (we kept 98.5 extra)

A 150bps schedule whose realized value spans 51.5 to 248.5bps is not a 150bps
fee, and which end of that range a given customer lands on is decided by how
their wallet rounded an amount -- not by anything this desk chose. That is the
measurement the "comparable to other exchanges" question actually needs, and it
has to be on a screen before a rate can be argued about.

WHAT THIS FILE DOES NOT DO, DELIBERATELY (rule 16).

It does not fix the mismatch. Recomputing the payout from `actual_input_amount`
changes what a customer is paid, which is live posture and the operator's
decision, not this report's. It does not change `DEFAULT_FEE_BPS`. It does not
send the retained fee anywhere -- collecting it to an address is fund movement
and belongs to the operator too. This file measures, prints, and stops.

WHY THE DERIVATION IS SQL AND NOT A PYTHON LOOP (rules 5 and 20).

Every term is arithmetic over columns already in the database, so SQLite can
express all of it and a per-row Python loop would put the same rule somewhere
only its author can inspect. The SELECT below is the one place the derivation
exists; the Python in this file chooses columns and formats them, and holds no
arithmetic of its own. The tests seed real rows into the real tables and assert
on the rows this SELECT returns (the behavioral-verification principle), which
is the only way the clamp case below can be checked at all.

THE ONE CASE THAT DOES NOT RECONCILE, AND IT IS REPORTED RATHER THAN HIDDEN.

`output_amount_estimate` is `max(..., 0)`. When the gross is smaller than the
network fee reserve the clamp fires, the payout is zero, and `retained` becomes
the whole gross -- so `retained_bps` is 10000 and the identity
`retained = G*f + R + drift` does not hold. That is not an error in this report;
it is a swap that paid out nothing. `reconciles` below is the arithmetic check
that catches it, and show_fees.py prints the row with the discrepancy named
instead of quietly averaging it into a total.
"""

from __future__ import annotations

from typing import NamedTuple

# WHICH PAYOUTS COUNT AS MONEY THAT MOVED.
#
# 'broadcast' AND a non-NULL txid, both, because they are not the same claim and
# the gap between them is the failure services/payout_service.process_pending_
# payouts() documents at length: a row is INSERTed as 'created' before the send,
# and the status and txid are written after it. A crash in between leaves
# 'created' with no txid -- money possibly on chain, unrecorded. Such a row must
# not enter a fee total, because the amount it would contribute is not known to
# have been paid; it is also exactly the row an operator needs to see, which is
# unresolved_payouts()'s job in services/admin_view.py and not this file's.
#
# The predicate is written INLINE in the SELECT below rather than held in a
# constant and interpolated in. An f-string here earned ruff's S608 ("SQL built
# by interpolation"), and the available answers were a `noqa` claiming the
# interpolated value is an identifier this repo controls, or not interpolating.
# Project Mammon rule 19 forbids the first -- "never add a suppression to make a
# check pass" -- so the literal is in the query and this comment carries the
# reasoning. Nothing is lost: what a constant would have given a test is a
# string to compare against, and tests/test_fee_ledger.py instead seeds a
# payout row in each non-counting state and asserts it is ABSENT from the
# result, which is the stronger claim (the behavioral-verification principle).

# THE DERIVATION. One expression per quantity, each named, none of them repeated
# in Python. The CTE exists because SQLite does not promise that a result alias
# is visible to a later expression in the SAME select list, and every figure in
# the outer SELECT is built from `realized_gross` and `quoted_gross`.
#
# `s.actual_input_amount IS NOT NULL` is not defensive padding: that column is
# NULL until a deposit is seen (services/deposit_service.py writes it from
# seen_total), and NULL propagates, so without the filter a swap paid out by
# hand with no deposit row would contribute a row of NULLs to a report about
# money. It is a JOIN on payouts anyway, so such a row is already unusual --
# the filter makes it absent rather than blank.
#
# 10000.0, never 10000: SQLite integer division would floor `fee_bps / 10000` to
# 0 and make every drift figure equal to the gross difference.
FEE_LEDGER_SQL = """
WITH basis AS (
    SELECT
        s.id                                        AS swap_id,
        s.from_asset                                AS from_asset,
        s.to_asset                                  AS to_asset,
        s.fee_bps                                   AS fee_bps,
        s.quoted_rate                               AS quoted_rate,
        s.expected_input_amount                     AS expected_input,
        s.actual_input_amount                       AS actual_input,
        s.network_fee_reserve                       AS reserve,
        s.output_amount_estimate                    AS estimate,
        s.completed_at                              AS completed_at,
        p.amount                                    AS paid,
        p.txid                                      AS payout_txid,
        p.sent_at                                   AS sent_at,
        s.actual_input_amount * s.quoted_rate       AS realized_gross,
        s.expected_input_amount * s.quoted_rate     AS quoted_gross
    FROM swaps s
    JOIN payouts p ON p.swap_id = s.id
    WHERE p.status = 'broadcast' AND p.txid IS NOT NULL
      AND s.actual_input_amount IS NOT NULL
)
SELECT
    basis.*,
    realized_gross - paid AS retained,
    (1 - fee_bps / 10000.0) * (realized_gross - quoted_gross) AS drift_coin,
    CASE WHEN realized_gross > 0
         THEN (realized_gross - paid) / realized_gross * 10000 END AS retained_bps,
    CASE WHEN realized_gross > 0
         THEN reserve / realized_gross * 10000 END AS reserve_bps,
    CASE WHEN realized_gross > 0
         THEN (1 - fee_bps / 10000.0) * (realized_gross - quoted_gross)
              / realized_gross * 10000 END AS drift_bps
FROM basis
ORDER BY sent_at ASC, swap_id ASC
"""

# HOW CLOSE THE IDENTITY HAS TO COME BEFORE IT COUNTS AS HOLDING.
#
# retained_bps = fee_bps + reserve_bps + drift_bps is exact in real arithmetic
# and is not in float: every term above is a REAL column multiplied and divided
# through, and the live GRC figures in this project carry fifteen significant
# digits (quoted_rate 8995.69403675488). Measured on the seeded tests, the
# residual lands under 1e-9 bps; a tenth of a basis point is four orders of
# magnitude above that and still far below anything that could hide a real
# discrepancy, since the smallest one this report exists to catch -- the
# max(...,0) clamp -- moves retained_bps to 10000.
RECONCILE_TOLERANCE_BPS = 0.1

#: BELOW THIS A DRIFT IS ZERO, FOR COUNTING PURPOSES ONLY.
#:
#: `drift_coin` is (1-f) times the difference between two REAL columns multiplied
#: through a rate carrying fifteen significant digits, so a deposit that matched
#: the quote EXACTLY computes to something like 1e-17 rather than to 0.0. Counting
#: `!= 0` would report every swap as having drifted. 1e-12 of a coin is a
#: thousandth of a satoshi: nothing a chain can express rounds into it.
#:
#: It decides a COUNT and a sentence, never a payout and never a total -- the
#: sums above are over the exact values.
DRIFT_IS_ZERO_COIN = 1e-12


class FeeRow(NamedTuple):
    """One completed swap's fee, with every term that makes it up.

    Each field is a column the SELECT above returned; nothing is recomputed
    here. `reconciles` is the exception and it is a CHECK rather than a
    derivation -- it asserts the identity the SQL claims, so a future edit to
    one of those expressions cannot silently stop adding up.
    """

    swap_id: str
    from_asset: str
    to_asset: str
    fee_bps: float
    quoted_rate: float
    expected_input: float
    actual_input: float
    reserve: float
    paid: float
    payout_txid: str
    sent_at: str
    realized_gross: float
    quoted_gross: float
    retained: float
    retained_bps: float
    reserve_bps: float
    drift_coin: float
    drift_bps: float

    @property
    def reserve_charged(self) -> bool:
        """Was the network fee reserve WITHHELD FROM THIS CUSTOMER'S payout?

        TWO PRICING REGIMES LIVE IN THIS TABLE, and a report over history has to
        tell them apart. Until 2026-10-02 create_quote() computed

            max(gross * (1 - f) - reserve, 0)

        so every payout was short by a flat reserve. After the operator's "fix the
        regressive reserve" it computes max(gross * (1 - f), 0) and the reserve is
        a cost the desk carries. Rows written on either side of that sit side by
        side here forever.

        SO THE IDENTITY DIFFERS PER ROW, which is why this is derived rather than
        assumed. Checked before shipping: a NEW 56 GRC swap under the old identity
        reports a residual of -1.8bps, far outside RECONCILE_TOLERANCE_BPS, so
        every small new payout would have printed DOES NOT RECONCILE -- a false
        alarm on the one line that is supposed to mean something is wrong.

        Decided by which identity the row is CLOSER to, not by a date. A date would
        need a migration boundary nobody recorded, and the arithmetic says it
        directly: the two candidates differ by the whole reserve, which is orders of
        magnitude above float noise.
        """
        # DRIFT IS IN BOTH CANDIDATES, and leaving it out got this wrong on the
        # first run. The identities are
        #
        #     charged      retained = gross_fee + drift + reserve
        #     not charged  retained = gross_fee + drift
        #
        # and a short deposit moves `retained` by the drift, which on a mismatched
        # swap is far larger than the reserve. Comparing against the undrifted
        # candidates made a -0.886 drift look 0.01 closer to the no-reserve
        # identity, so an old-regime row with a short deposit was classified as new
        # and then reported DOES NOT RECONCILE -- the exact false alarm this
        # property was added to prevent, reintroduced by omitting a term.
        base = self.gross_fee + self.drift_coin
        return abs(self.retained - (base + self.reserve)) < abs(self.retained - base)

    @property
    def gross_fee(self) -> float:
        """The scheduled fee on this swap's realized gross, in the destination asset."""
        return self.realized_gross * self.fee_bps / 10000.0

    @property
    def reconciles(self) -> bool:
        """Does retained_bps equal the fee, plus the reserve IF it was charged, plus drift?

        False means the max(...,0) clamp fired in create_quote() -- the payout
        was zero and the whole gross was retained. It is a real state and the
        report prints such a row with this said out loud, because averaging it
        into a fee total would report a 10000bps fee as if it were a price.
        """
        return abs(self.residual_bps) <= RECONCILE_TOLERANCE_BPS

    @property
    def residual_bps(self) -> float:
        """By how much the identity misses, in bps. Zero when it holds."""
        charged = self.reserve_bps if self.reserve_charged else 0.0
        return self.retained_bps - (self.fee_bps + charged + self.drift_bps)


def fee_rows(db) -> list[FeeRow]:
    """Every broadcast payout's fee, oldest first.

    Returns an empty list when nothing has been paid out, which is a result and
    not an error -- show_fees.py prints "(none)" and says what the absence means
    (rule 14). It is NOT the same answer as "the fee was zero".
    """
    return [
        FeeRow(
            swap_id=row["swap_id"],
            from_asset=row["from_asset"],
            to_asset=row["to_asset"],
            fee_bps=float(row["fee_bps"]),
            quoted_rate=float(row["quoted_rate"]),
            expected_input=float(row["expected_input"]),
            actual_input=float(row["actual_input"]),
            reserve=float(row["reserve"]),
            paid=float(row["paid"]),
            payout_txid=row["payout_txid"],
            sent_at=row["sent_at"] or "",
            realized_gross=float(row["realized_gross"]),
            quoted_gross=float(row["quoted_gross"]),
            retained=float(row["retained"]),
            retained_bps=float(row["retained_bps"]),
            reserve_bps=float(row["reserve_bps"]),
            drift_coin=float(row["drift_coin"]),
            drift_bps=float(row["drift_bps"]),
        )
        for row in db.execute(FEE_LEDGER_SQL).fetchall()
    ]


class AssetTotal(NamedTuple):
    """Everything retained in ONE destination asset, and the fee that implies.

    PER ASSET, NEVER POOLED. Summing GRC and LTC retentions into one figure
    would be adding coins of different value and calling the result revenue;
    Project Mammon rule 11 refuses the same move for settlement cadences for the
    same reason -- the mean of two populations describes neither. A USD total is
    a separate question that needs a price feed this file deliberately does not
    open.

    `swaps` is the denominator for every rate here (rule 3): a weighted fee with
    no count behind it cannot be told from a single lucky swap.
    """

    asset: str
    swaps: int
    gross: float
    retained: float
    drift: float
    #: The sum of the ABSOLUTE drifts, and the count of swaps that had one.
    #:
    #: A NET DRIFT ALONE IS THE MOST MISLEADING LINE THIS REPORT CAN PRINT, and
    #: that is measured rather than feared: the first run of show_fees.py over a
    #: seeded ledger of four swaps -- one exact, one at 0.99x, one at 1.01x, one
    #: clamped -- printed `drift +0.00000000 GRC` while two of the four rows
    #: underneath it showed -99.5bps and +97.5bps. The two offsetting swaps summed
    #: to within a rounding of zero, so the asset line said no mismatch had
    #: happened on a ledger where half the customers were charged the wrong fee.
    #:
    #: The net is still the right figure for "what did this cost us"; it is simply
    #: not an answer to "is this happening". Both are printed, which is rule 3's
    #: "state the denominator" applied to a signed total: a net without the gross
    #: movement behind it cannot be told from no movement at all.
    drift_abs: float
    drifted: int
    weighted_bps: float
    scheduled_bps: float
    unreconciled: int
    #: How many of these payouts had the reserve WITHHELD from the customer, and
    #: what that came to. Both, because the count alone does not say what it cost
    #: anybody and the total alone does not say how concentrated it was.
    #:
    #: It is history as of 2026-10-02 and is reported rather than dropped: these
    #: are real amounts real customers did not receive, and a fee report that
    #: silently stopped mentioning them would make the change invisible in the one
    #: place it is measurable.
    reserve_charged_swaps: int
    reserve_charged_total: float


def asset_totals(rows: list[FeeRow]) -> list[AssetTotal]:
    """Totals per destination asset, from the rows the SELECT returned.

    The weighted fee is retained/gross, NOT the mean of the per-swap bps: the
    mean treats a 0.001 SOL swap and a 10 SOL swap as equal evidence about what
    this desk charges, and the one number anybody wants from this report is
    "what fraction of what came in did we keep".

    `scheduled_bps` is the same weighting applied to the fee each swap was
    QUOTED, so the two columns are comparable by construction -- a scheduled
    figure averaged a different way than the realized one would make every
    difference between them unreadable.

    Unreconciled rows are COUNTED and still summed. They are real payouts and
    real retention; what they are not is a fee rate, which is why the count
    rides alongside instead of the rows being dropped. Dropping them would make
    a clamped payout vanish from a report about money.
    """
    assets: dict[str, list[FeeRow]] = {}
    for row in rows:
        assets.setdefault(row.to_asset, []).append(row)
    totals = []
    for asset, group in sorted(assets.items()):
        gross = sum(row.realized_gross for row in group)
        retained = sum(row.retained for row in group)
        scheduled = sum(row.realized_gross * row.fee_bps for row in group)
        totals.append(
            AssetTotal(
                asset=asset,
                swaps=len(group),
                gross=gross,
                retained=retained,
                drift=sum(row.drift_coin for row in group),
                drift_abs=sum(abs(row.drift_coin) for row in group),
                drifted=sum(1 for row in group if abs(row.drift_coin) > DRIFT_IS_ZERO_COIN),
                reserve_charged_swaps=sum(1 for row in group if row.reserve_charged),
                reserve_charged_total=sum(row.reserve for row in group if row.reserve_charged),
                # Guarded, and the guard is not cosmetic: a gross of zero is
                # reachable (a swap credited at zero would be), and 0/0 raises
                # rather than returning something misleading. 0.0 here means
                # "no basis to measure a rate against", which is what the
                # printed line says beside it.
                weighted_bps=(retained / gross * 10000) if gross > 0 else 0.0,
                scheduled_bps=(scheduled / gross) if gross > 0 else 0.0,
                unreconciled=sum(1 for row in group if not row.reconciles),
            )
        )
    return totals
