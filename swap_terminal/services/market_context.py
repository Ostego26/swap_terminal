"""Is a spot price trustworthy enough to quote from, and the history to ask it of.

Role: submodule -> function (price_confidence() is the decision; everything else
       carries rows to and from it)
Reads: swap_terminal.db (market_context), CoinGecko through services/pricing.py
Writes: swap_terminal.db (market_context) -- append only, never updates or
       deletes a row
Can move funds: NO, AND DELIBERATELY NOT WIRED WHERE IT COULD. See the
       docstring of price_confidence() for exactly what wiring it in would mean
       and why that decision is the operator's (rule 16).
Mainnet-safe: yes -- it reads a price API and writes its own table. It broadcasts
       nothing, signs nothing and touches no chain RPC.

WHY THIS FILE EXISTS. Operator instruction 2026-09-27: "we should also keep
track of the market cap and price comparison to better establish grc prices."

services/pricing.py now fetches market cap, 24h volume and 24h change alongside
the spot price, in the same HTTP request. This file is the two things that makes
useful: a place to PUT them so the next cycle can read what the last one saw
(rule 5: SQL is the authority, and rule 7: a measurement that only exists in a
log is not learning), and ONE function that turns a snapshot into a verdict so
the judgment can be called with seeded inputs instead of being inlined into a
quote (rule 10).

THE ROW STORES FACTS, NOT THE VERDICT, AND THAT IS A RULE 8 DECISION RATHER
THAN AN OMISSION. The verdict is a pure function of the row plus the config
window, so storing it would put the same rule in two places -- the stored
verdict and the function -- and they would agree on the day a row was written
and drift from then on, exactly the way Mammon's weather-city parser drifted in
three files. Worse, the drift would be silent and backwards: change a threshold
and every historical row still asserts the old answer, so the table would
disagree with the code about a past that cannot be re-measured. Store what
CoinGecko said; derive the verdict at read time, from whichever version of the
rule is current.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from microfortnights import format_duration
from services.pricing import SNAPSHOT_FIELDS, MarketSnapshot, fetch_market_context

# ---------------------------------------------------------------------------
# The verdict vocabulary. ONE tuple, ordered best to worst, and the severity
# ranking, the allowed values and the "worst finding wins" combination are all
# derived from this order (rule 11: one vocabulary, derived in one place).
#
# A hand-written severity dict beside a hand-written list of names is rule 8's
# shape: adding a verdict to one and not the other produces either a name no
# ranking knows or a ranking for a name nothing emits, and neither announces
# itself.
#
# WHY THIS ORDER, since it decides which finding a caller sees:
#
#   OK        every check that could run, ran, and none of them fired.
#   STALE     the price can drift by more than the fee inside the window the
#             quote is valid for. A KNOWN, BOUNDED, QUANTIFIED problem: the
#             magnitude is in the finding.
#   THIN      the market is thin enough that the quote's own size, or the asset's
#             own turnover, means the spot price is not a price anybody can
#             transact at. Worse than STALE because a stale price is still a
#             real price that existed; a thin one may never have.
#   UNKNOWN   a field needed to make the judgment is absent, so no bound at all
#             can be put on either of the above. WORST, and that is the whole
#             point of having it rather than defaulting a missing field to zero:
#             "I cannot tell" is the answer a desk must not mistake for "fine",
#             and CoinGecko returning partial data for a thin asset is the
#             ORDINARY case for GRC, not an error.
# ---------------------------------------------------------------------------
VERDICTS: tuple[str, ...] = ("OK", "STALE", "THIN", "UNKNOWN")
_SEVERITY = {verdict: rank for rank, verdict in enumerate(VERDICTS)}

# A day, in seconds. Named because the 24h volume and the 24h change are both
# quoted over this window and every scaling below divides by it; spelling 86400
# four times is the two-copies problem at the level of a literal.
#
# SECONDS, and not converted to microfortnights: it is arithmetic in control
# flow, not a report (rule 6). The µfn conversion happens at the print, in
# format_market_context_block().
SECONDS_PER_DAY = 86400.0

# Turnover below which an asset is called thin REGARDLESS of the swap size:
# 24h volume divided by market cap.
#
# This one is a judgment and is labeled as such rather than dressed up as a
# measurement (rule 17). The reasoning: at a turnover of 0.001 a whole day's
# trading is one thousandth of the float, so any holder of 0.1% of supply can
# double the day's volume single-handed, and the spot price is therefore set by
# an amount of money a single participant can deploy. It is NOT calibrated
# against GRC's actual turnover, because api.coingecko.com is unreachable from
# where this was written (see services/pricing.py's header: CONNECT 403) and no
# recorded response existed in the tree to read one out of. The first real
# market_context rows will calibrate it; until then it is a stated guess with
# its reasoning attached.
THIN_TURNOVER = 0.001


@dataclass(frozen=True)
class Finding:
    """One check's outcome. `code` is for assertions, `message` is for a human.

    The split is deliberate and it is there because of a mistake the operator
    named on 2026-09-27: three tests written that day asserted on docstring and
    message TEXT and passed while the code was wrong. A message is prose and may
    be reworded in any commit; `code` is the behavior. Tests assert on codes and
    verdicts. Nothing in this file lets a check fire without a code.
    """

    code: str
    verdict: str
    message: str


@dataclass(frozen=True)
class PriceConfidence:
    """The verdict for one asset, with every finding that produced it.

    `verdict` is the WORST of the findings' verdicts, or "OK" when none fired.
    `findings` keeps them all, including the informational ones, because the
    reason a quote is fine is as useful to a reader as the reason it is not --
    and because a caller that only sees the worst cannot tell one problem from
    three.
    """

    asset: str
    verdict: str
    reason: str
    findings: tuple[Finding, ...]

    @property
    def is_ok(self) -> bool:
        return self.verdict == "OK"


def worst_verdict(verdicts) -> str:
    """The most severe verdict in an iterable, or "OK" when it is empty.

    Derived from VERDICTS' order, so a new verdict slots in by being placed in
    that tuple and nothing here changes. Raises KeyError naming the value for a
    verdict that is not in the vocabulary, rather than sorting it to one end --
    an unknown severity silently ranked as harmless is how a check gets written
    that can never fire.
    """
    ranked = [(_SEVERITY[verdict], verdict) for verdict in verdicts]
    if not ranked:
        return "OK"
    return max(ranked)[1]


def expected_drift_bps(change_24h_pct: float, window_seconds: float) -> float:
    """How far the price can be expected to have moved over `window_seconds`, in bps.

    SQUARE ROOT OF TIME, NOT LINEAR PRORATION, and the difference is large enough
    that the choice is the whole check. A price is a random walk to a first
    approximation, so its expected displacement grows with the SQUARE ROOT of the
    elapsed time, not with the time. Measured at the window this system actually
    runs (RATE_CACHE_SECONDS 30 + QUOTE_TTL_SECONDS 600 = 630 seconds, which is
    630/86400 = 0.00729 of a day):

        linear proration    0.00729 of the daily move
        sqrt of time        0.0854  of the daily move    -- 11.7x larger

    So linear proration understates the 630-second exposure by nearly twelve
    times, and a threshold set against it would essentially never fire: it would
    take a 206% move in a day to put 150 bps of drift into the window, where the
    sqrt scaling puts it there at a 17.6% day. Gridcoin has 17.6% days.

    WHAT THIS IS AND IS NOT (rule 17). `change_24h_pct` is ONE REALIZED RETURN,
    not a volatility estimate. Using it as a proxy for sigma is an approximation,
    and it is the only volatility signal /simple/price carries -- there is no
    standard deviation in that response to use instead. It understates a market
    that chopped 20% in both directions and ended flat (change_24h_pct near zero,
    real volatility high) and overstates one that drifted 20% in a straight line.
    The direction of the error is not knowable from this endpoint. A caller that
    needs a real sigma needs /coins/{id}/market_chart, which is a second request
    and a different change.

    `abs()` because a fall is as expensive as a rise: the drift that matters is
    magnitude, not sign. A bp is a hundredth of a percent, hence the * 100.
    """
    return abs(change_24h_pct) * 100.0 * math.sqrt(window_seconds / SECONDS_PER_DAY)


def window_volume_usd(volume_24h_usd: float, window_seconds: float) -> float:
    """The USD volume that trades in `window_seconds`, if a day's flow were even.

    THE EVENNESS IS AN ASSUMPTION AND IT IS STATED HERE RATHER THAN BURIED. Real
    volume clusters -- around exchange hours, around news, around one whale --
    so a thin asset's quiet window carries far less than this and its busy
    window far more. The figure is therefore an OPTIMISTIC bound on how much
    flow a quote's validity window can absorb: the real answer is usually worse.
    That direction is the safe one for a check that fires on too LITTLE volume.
    """
    return volume_24h_usd * (window_seconds / SECONDS_PER_DAY)


@dataclass(frozen=True)
class QuoteWindow:
    """The three config values that decide how a snapshot is judged, as one thing.

    quote_ttl_seconds, rate_cache_seconds and fee_bps are config.Config's
    QUOTE_TTL_SECONDS, RATE_CACHE_SECONDS and DEFAULT_FEE_BPS. There are NO
    DEFAULTS here on purpose: writing 600, 30 and 150 into this class would be a
    second copy of three settings the operator tunes, agreeing with config.py the
    day it is written and drifting silently after (rule 8). A caller must pass
    them, and a caller that cannot is a caller that does not know which window it
    is asking about.

    WHY THIS TYPE EXISTS AT ALL, since it is three floats. Two reasons, and the
    second is the one that made it happen:

      exposure_seconds is a DERIVATION, and it belongs next to its inputs rather
      than being re-summed at four call sites. The sum is the whole subtlety of
      this check (see its docstring) and it is not something a reader should have
      to notice being done correctly four times.

      ruff's PLR0913 refused price_confidence() at nine arguments under this
      repository's declared standard. Rule 19 forbids adding a suppression to
      make a check pass, and rule 12 says the fix for a complexity finding is to
      extract, so the arguments were grouped instead of silenced. The operator's
      sketch of this function was
      `price_confidence(asset, price, market_cap, volume_24h, change_24h)` plus
      the windows; it is two grouped arguments instead, which also removes a real
      hazard -- market_cap_usd and volume_24h_usd are both floats, so a
      transposed pair at a call site is invisible to every checker and produces a
      confident wrong verdict. Fields cannot be transposed.
    """

    quote_ttl_seconds: float
    rate_cache_seconds: float
    fee_bps: float

    @property
    def exposure_seconds(self) -> float:
        """How old the price can be when a customer acts on it. THE SUM, not either one.

        A customer accepting a quote at the last legal instant is acting on a
        price that was fetched up to rate_cache_seconds before the quote was
        created and is then honored for quote_ttl_seconds after it. At this
        repository's defaults that is 30 + 600 = 630 seconds of exposure to one
        number. Using QUOTE_TTL_SECONDS alone would understate it by the cache;
        using RATE_CACHE_SECONDS alone would understate it twentyfold.
        """
        return float(self.rate_cache_seconds) + float(self.quote_ttl_seconds)


def _price_finding(snapshot: MarketSnapshot) -> Finding | None:
    """UNKNOWN when the price itself is unusable. Short-circuits everything else.

    With no positive finite price there is no notional to compare against flow
    and no base for a drift, so every other check would be arithmetic on
    nothing. NaN and inf are caught as well as None and zero: math.isfinite is
    the test, because a NaN price compares False to every threshold and would
    otherwise pass every check silently.
    """
    price = snapshot.price_usd
    if price is None or not math.isfinite(price) or price <= 0:
        return Finding(
            "price_unusable",
            "UNKNOWN",
            f"{snapshot.asset}: price_usd={price!r}  <- expected a positive finite number; nothing can be "
            f"judged from this and no swap can be priced from it either",
        )
    return None


def _availability_findings(snapshot: MarketSnapshot, window: QuoteWindow) -> list[Finding]:
    """One UNKNOWN per context field that is absent or unusable, naming the field.

    THIS IS THE CASE THAT HAD TO BE HANDLED WELL RATHER THAN CRASHED ON.
    CoinGecko returns partial data for thin assets, and GRC is a thin asset, so
    a missing market cap or volume is the ORDINARY response and not an error.

    A zero cap or a zero volume is treated as absent here, and that is the one
    place this file collapses None and 0.0 deliberately. The distinction is kept
    in the ROW -- services/pricing.py records whichever it was -- and does not
    matter for the judgment: an asset with a literal zero dollars of volume and
    an asset with no volume figure are equally impossible to take a fraction of,
    and the turnover division would be a ZeroDivisionError on a zero cap. Each
    message prints the repr, so a reader can see which it was.

    change_24h_pct is the exception and 0.0 does NOT land here: a flat market is
    an answer about the market, absence is the absence of an answer, and treating
    them alike would report a quiet day as unjudgeable.
    """
    exposure = format_duration(window.exposure_seconds)
    findings: list[Finding] = []
    if snapshot.market_cap_usd is None or snapshot.market_cap_usd <= 0:
        findings.append(
            Finding(
                "market_cap_unavailable",
                "UNKNOWN",
                f"{snapshot.asset}: market_cap_usd={snapshot.market_cap_usd!r}  <- no usable market cap, so "
                f"turnover cannot be computed and the float behind this price is unknown. CoinGecko returns "
                f"partial data for thin assets; this is not an error, it is the absence of an answer",
            )
        )
    if snapshot.volume_24h_usd is None or snapshot.volume_24h_usd <= 0:
        findings.append(
            Finding(
                "volume_unavailable",
                "UNKNOWN",
                f"{snapshot.asset}: volume_24h_usd={snapshot.volume_24h_usd!r}  <- no usable 24h volume, so "
                f"neither turnover nor the volume expected during the {exposure} exposure window can be "
                f"computed. A swap size cannot be checked against flow that was not reported",
            )
        )
    if snapshot.change_24h_pct is None:
        findings.append(
            Finding(
                "change_unavailable",
                "UNKNOWN",
                f"{snapshot.asset}: change_24h_pct=None  <- no 24h change, so no bound can be put on how far "
                f"this price may drift during the {exposure} it is honored for. Note that 0.0 would NOT land "
                f"here: a flat market is an answer, absence is not",
            )
        )
    return findings


def turnover_finding(snapshot: MarketSnapshot) -> Finding | None:
    """THIN when a day's volume is a small enough fraction of the float.

    Independent of swap size: this is about whether the spot price is set by an
    amount of money one participant can deploy. Returns None when either input
    is unusable -- _availability_findings() has already reported that, and a
    second finding about the same absence would double-count it.
    """
    cap, volume = snapshot.market_cap_usd, snapshot.volume_24h_usd
    if not cap or cap <= 0 or not volume or volume <= 0:
        return None
    turnover = volume / cap
    if turnover < THIN_TURNOVER:
        return Finding(
            "thin_turnover",
            "THIN",
            f"{snapshot.asset}: 24h volume ${volume:,.0f} is {turnover:.6f} of a ${cap:,.0f} market cap  <- "
            f"below {THIN_TURNOVER}, so a holder of {THIN_TURNOVER:.1%} of supply could double a day's volume "
            f"alone and this spot price is set by that much money",
        )
    return Finding(
        "turnover",
        "OK",
        f"{snapshot.asset}: turnover {turnover:.6f} (24h volume ${volume:,.0f} / market cap ${cap:,.0f})  <- at "
        f"or above {THIN_TURNOVER}",
    )


def _size_finding(
    snapshot: MarketSnapshot, window: QuoteWindow, swap_notional_usd: float | None
) -> Finding | None:
    """THIN when the swap is at least as large as the flow its own window expects.

    Only fires when a notional was supplied. Without one there is no fraction to
    take, and returning OK because nobody named a size would be a confident
    answer to a question nobody asked -- so the no-size case is reported as the
    window volume with "(none)" for the share (rule 14).
    """
    volume = snapshot.volume_24h_usd
    if not volume or volume <= 0:
        return None
    exposure = format_duration(window.exposure_seconds)
    expected = window_volume_usd(volume, window.exposure_seconds)
    if swap_notional_usd is None:
        return Finding(
            "window_volume",
            "OK",
            f"{snapshot.asset}: ${expected:,.2f} of volume expected during the {exposure} exposure window  <- "
            f"no swap size was supplied, so the share of it is (none), not zero",
        )
    if swap_notional_usd >= expected:
        return Finding(
            "notional_exceeds_window_volume",
            "THIN",
            f"{snapshot.asset}: a ${swap_notional_usd:,.2f} swap is {swap_notional_usd / expected:.2f}x the "
            f"${expected:,.2f} of volume expected during the {exposure} this quote is exposed for  <- the swap "
            f"IS the market for that window, so the spot price it is quoted from is not a price it can "
            f"transact at",
        )
    return Finding(
        "notional_share_of_window_volume",
        "OK",
        f"{snapshot.asset}: a ${swap_notional_usd:,.2f} swap is {swap_notional_usd / expected:.4f} of the "
        f"${expected:,.2f} expected during the {exposure} exposure window",
    )


def _drift_finding(snapshot: MarketSnapshot, window: QuoteWindow) -> Finding | None:
    """STALE when the expected drift over the exposure window reaches the fee.

    At or above rather than strictly above: a drift exactly equal to the fee
    leaves the desk nothing, and the boundary belongs on the cautious side of a
    number that is an approximation to begin with (see expected_drift_bps).
    """
    change = snapshot.change_24h_pct
    if change is None:
        return None
    exposure = format_duration(window.exposure_seconds)
    drift_bps = expected_drift_bps(change, window.exposure_seconds)
    if drift_bps >= window.fee_bps:
        return Finding(
            "drift_exceeds_fee",
            "STALE",
            f"{snapshot.asset}: moved {change:+.2f}% in 24h, which scales (sqrt of time) to {drift_bps:.1f}bps "
            f"over the {exposure} exposure window  <- at or over the {window.fee_bps:.0f}bps fee, so the price "
            f"can move further than the desk earns while the quote is still valid",
        )
    return Finding(
        "drift_vs_fee",
        "OK",
        f"{snapshot.asset}: moved {change:+.2f}% in 24h -> {drift_bps:.1f}bps expected drift over {exposure}, "
        f"under the {window.fee_bps:.0f}bps fee",
    )


def price_confidence(
    snapshot: MarketSnapshot,
    window: QuoteWindow,
    *,
    swap_notional_usd: float | None = None,
) -> PriceConfidence:
    """Is this spot price trustworthy enough to quote a swap from? REPORT ONLY.

    *** THIS IS NOT WIRED INTO THE ORDER PATH, ON PURPOSE. ***

    Nothing in services/quote_service.py, services/payout_service.py or
    routes/quotes.py calls this function, and no call was added. What wiring it
    in WOULD mean, stated precisely so the operator is choosing between real
    options rather than a vague one:

      to REFUSE on a verdict    create_quote() would raise instead of returning a
                                quote whenever the verdict is not OK. That turns
                                a thin-market reading into a swap the customer
                                cannot make -- it changes whether a pair may
                                trade at all, which is the first line of rule
                                16's fund-movement list.
      to WIDEN the fee          create_quote() would add the expected drift to
                                fee_bps, so output_amount_estimate falls. That
                                changes the amount services/payout_service.py
                                later broadcasts. It is a pricing change.
      to CAP the size           refusing a swap_notional_usd above the window
                                volume would change what size may trade.
      to BADGE the quote        returning the verdict alongside the quote for the
                                page to display, changing no number. This is the
                                only one of the four that is not fund movement --
                                and it still adds a field to an HTTP response, so
                                it is a change to a surface rather than a
                                diagnostic.

    All four are the operator's call. Three move money and the fourth changes a
    contract. What this function does today is produce a verdict a report can
    print and a row a later cycle can compare against, which is the half that is
    free.

    IT IS PURE. No clock, no network, no database, no config lookup -- every
    input is an argument, so it can be called with seeded values and asserted on
    directly, which is what tests/test_market_context.py does for each check.

    The verdict is the WORST finding's verdict, and `reason` is the first message
    at that severity. All findings are kept, including informational ones, because
    a caller that sees only the worst cannot tell one problem from three.
    """
    price_problem = _price_finding(snapshot)
    if price_problem is not None:
        return PriceConfidence(snapshot.asset, "UNKNOWN", price_problem.message, (price_problem,))

    # Availability first, because `reason` is the first message at the worst
    # severity and "I could not tell" outranks anything derived from what was
    # missing. Then turnover, size, drift. Each check returns None when its own
    # inputs are unusable rather than reporting the same absence twice.
    findings: list[Finding] = list(_availability_findings(snapshot, window))
    findings.extend(
        result
        for result in (
            turnover_finding(snapshot),
            _size_finding(snapshot, window, swap_notional_usd),
            _drift_finding(snapshot, window),
        )
        if result is not None
    )

    verdict = worst_verdict(finding.verdict for finding in findings)
    if verdict == "OK":
        reason = f"{snapshot.asset}: every check that could run, ran, and none fired ({len(findings)} checks)"
    else:
        reason = next(finding.message for finding in findings if finding.verdict == verdict)
    return PriceConfidence(snapshot.asset, verdict, reason, tuple(findings))


# ---------------------------------------------------------------------------
# Persistence. The columns are DERIVED from MarketSnapshot's fields so the
# INSERT cannot drift from the type it inserts; db.py's DDL is checked against
# the same tuple by tests/test_market_context.py, which reads PRAGMA
# table_info rather than the DDL text (behavioral verification: the schema as
# the database actually built it, not as the source spells it).
#
# db.py is NOT imported here and does not import this module. It is imported by
# every worker at startup and pulls in nothing heavier than sqlite3 and
# chains/xrp_units; making it import services/pricing.py would put `requests`
# into that path for the sake of a column list.
# ---------------------------------------------------------------------------
MARKET_CONTEXT_COLUMNS = SNAPSHOT_FIELDS

_INSERT_SQL = (
    f"INSERT INTO market_context ({', '.join(MARKET_CONTEXT_COLUMNS)}, recorded_at) "  # noqa: S608 -- checked: MARKET_CONTEXT_COLUMNS is derived from MarketSnapshot's dataclass fields, which are identifiers in this repository's own source. No value is interpolated; every value below is a ? parameter.
    f"VALUES ({', '.join('?' * len(MARKET_CONTEXT_COLUMNS))}, ?)"
)


def record_market_context(conn, snapshots, recorded_at: str) -> int:
    """APPEND one row per snapshot. Returns how many were written.

    APPEND ONLY, and there is no update path and no delete path in this module.
    Rule 7: the record of what the system observed is evidence, and the whole
    value of this table is that a row written an hour ago still says what it
    said. A row that can be corrected is a row that can be corrected wrongly,
    and nothing downstream could tell.

    NO UNIQUE CONSTRAINT ON (asset, fetched_at) EITHER, and that is considered
    rather than forgotten: two processes fetching in the same TTL window get the
    same fetched_at from the shared cache, so a unique index would make the
    second one's insert an IntegrityError -- turning a duplicate observation,
    which is harmless, into an exception on a diagnostic path. A duplicate row
    is deduplicated at read time by anything that cares; a failed write is lost
    evidence.

    `recorded_at` is passed in rather than taken from the clock here so the
    caller's one timestamp covers the whole batch and so a test can seed it. It
    is an ISO string, matching every other timestamp column in this schema.

    Commits, because a caller that fetched and did not persist has done the
    network work and kept nothing.
    """
    rows = [
        (*(getattr(snapshot, column) for column in MARKET_CONTEXT_COLUMNS), recorded_at)
        for snapshot in snapshots
    ]
    conn.executemany(_INSERT_SQL, rows)
    conn.commit()
    return len(rows)


def recent_market_context(conn, asset: str, limit: int = 20) -> list[dict]:
    """The last `limit` rows for one asset, newest first. The comparison over time.

    This is the reason the table exists rather than a print: "better establish
    grc prices" is a question about what GRC's price, cap and volume were doing
    an hour and a day ago, and that question can only be asked of rows.

    A SELECT with an ORDER BY and a LIMIT, in SQL, because it is a filter and a
    ranking over rows already in the database (rule 20). `asset` is a PARAMETER,
    not interpolated -- it is the one value here that could come from outside.
    """
    return conn.execute(
        "SELECT * FROM market_context WHERE asset = ? ORDER BY fetched_at DESC, id DESC LIMIT ?",
        (asset, int(limit)),
    ).fetchall()


def collect_and_record(conn, recorded_at: str, ttl_seconds: int = 30) -> list[MarketSnapshot]:
    """Fetch the context and append it. The composed step, for whatever runs it.

    NO CALLER IN THE TREE AS OF 2026-09-27, and saying so is the point rather
    than hiding it (rule 9 forbids leaving dead code, and rule 17 forbids
    implying a wiring that does not exist). It is one call from being periodic --
    a worker loop, or routes/rates.py which already fetches prices on every
    /api/rates request -- and both of those files were outside what this change
    was authorized to touch, so the wiring is NAMED WORK handed to the operator,
    not a baseline and not a pretense.

    It is deliberately not given a `__main__` block: an entry point an operator
    runs lives at the project root (rule 10), and adding one three directories
    down is how a path gets hardcoded into a caller no import graph can see.
    """
    snapshots = fetch_market_context(ttl_seconds)
    record_market_context(conn, snapshots, recorded_at)
    return snapshots


def format_market_context_block(snapshots, window: QuoteWindow, db_path: str) -> str:
    """The pasteable block. Announces its parameters, and (none) is a result.

    Rule 14, every clause of it that applies to a report this short:

      echoes the parameters that decide the answer -- the database, the two
      windows and the fee, because a verdict read a day later is unreadable
      without them and the reader has the screen, not the source;
      states what each number means next to it;
      prints "(none)" for an empty snapshot list rather than an empty section,
      because a blank gap cannot be told from a fetch that broke;
      reports every duration in microfortnights with the seconds in parentheses,
      through microfortnights.format_duration() rather than a second copy of
      1.2096 (rule 6).
    """
    lines = [
        f"market context  db={db_path}",
        f"  windows: RATE_CACHE_SECONDS={format_duration(window.rate_cache_seconds)} + "
        f"QUOTE_TTL_SECONDS={format_duration(window.quote_ttl_seconds)} = "
        f"{format_duration(window.exposure_seconds)} of exposure to one price; "
        f"DEFAULT_FEE_BPS={window.fee_bps:.0f}bps is what a drift is measured against",
    ]
    if not snapshots:
        lines.append("  (none)  <- zero snapshots. Expected one per services/pricing.IDS entry; zero means the "
                     "fetch returned nothing, not that the market is empty")
        return "\n".join(lines)
    for snapshot in snapshots:
        confidence = price_confidence(snapshot, window)
        cap = "(none)" if snapshot.market_cap_usd is None else f"${snapshot.market_cap_usd:,.0f}"
        volume = "(none)" if snapshot.volume_24h_usd is None else f"${snapshot.volume_24h_usd:,.0f}"
        change = "(none)" if snapshot.change_24h_pct is None else f"{snapshot.change_24h_pct:+.2f}%"
        feed_age = (
            "(none)"
            if snapshot.source_updated_at is None
            else format_duration(snapshot.fetched_at - snapshot.source_updated_at)
        )
        lines.append(
            f"  {snapshot.asset:<4} ${snapshot.price_usd:<14,.8f} cap={cap} vol24h={volume} chg24h={change} "
            f"feed_age={feed_age}  <- {confidence.verdict}"
        )
        # EVERY FINDING THAT FIRED, NOT JUST THE WORST ONE, and this was measured wrong on
        # the operator's first real run (2026-09-27, GRC live):
        #
        #     GRC  $0.02292385  cap=$11,601,617  vol24h=$960  chg24h=-17.77%   <- THIN
        #          GRC: 24h volume $960 is 0.000083 of a $11,601,617 market cap
        #
        # GRC tripped BOTH checks at that instant. Turnover was 0.000083 against a 0.001
        # threshold, AND the -17.77% day scaled to 151.7bps of expected drift over the 630s
        # exposure window against a 150bps fee. The block printed one of them, because it
        # printed `confidence.reason` -- which is the first message at the WORST severity, and
        # THIN outranks STALE. The stale-price finding, the one that says the market can move
        # further than the desk earns while the quote is still valid, was invisible.
        #
        # That is rule 14's defect: the operator reads the screen, not the source, and a check
        # that fired and was not printed is worse than one that never ran. price_confidence()
        # was always right and kept all of them; only the printer discarded them. So the
        # headline verdict stays the worst (a reader needs one word), and every firing finding
        # gets its own line under it, each labeled with its own severity so two at different
        # severities cannot be read as one.
        fired = [finding for finding in confidence.findings if finding.verdict != "OK"]
        if not fired:
            lines.append(f"       {confidence.reason}")
        else:
            lines.extend(f"       [{finding.verdict}] {finding.message}" for finding in fired)
            if len(fired) > 1:
                lines.append(f"       ^ {len(fired)} checks fired; the verdict above is the worst of them")
    return "\n".join(lines)
