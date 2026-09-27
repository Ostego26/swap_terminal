"""Market context: one request, a shape that did not change, and a verdict that reports.

Role: test (read-only against the tree; opens no socket and touches no chain)
Reads: swap_terminal/services/pricing.py, swap_terminal/services/market_context.py,
       swap_terminal/db.py's SCHEMA, and a temp SQLite file per test
Writes: a temp database under pytest's tmp_path
Can move funds: no
Mainnet-safe: yes -- requests.get is replaced in every test that touches the fetch,
       and the two tests that do not replace it make it RAISE, which is how they
       prove the decision is pure.

WHY THIS FILE EXISTS. Operator instruction 2026-09-27: "we should also keep track
of the market cap and price comparison to better establish grc prices." The change
under test adds market cap, 24h volume, 24h change and the feed's own timestamp to
the CoinGecko call services/pricing.py already made, a pure function that turns
those into a verdict, and a market_context table to keep them in.

WHAT IS PROVEN HERE AND WHAT IS NOT, stated up front because the difference is the
whole of rule 17.

  PROVEN: that the four flags ride the existing request and add no second one;
  that fetch_usd_prices()'s returned dict has exactly the keys it had before;
  that an absent, null or unparseable context field becomes None and NOT 0.0, and
  that the verdict says so rather than passing; that the drift check uses
  sqrt-of-time and fires at the fee boundary; that a swap larger than its own
  window's volume is THIN; that UNKNOWN outranks both; that the rows round-trip
  through the real schema with NULLs intact; that nothing on the quote or payout
  path calls the decision.

  NOT PROVEN, AND CANNOT BE FROM HERE: that CoinGecko's response actually spells
  the four keys `usd_market_cap`, `usd_24h_vol`, `usd_24h_change` and
  `last_updated_at`. Outbound HTTPS to api.coingecko.com is denied by this
  container's proxy -- measured 2026-09-27, CONNECT answered 403, recorded by the
  proxy as a policy denial -- and no recorded enriched response existed anywhere
  in the tree to read one out of (grepped for all four names: zero hits). Every
  fetch test below feeds a body this file wrote, so it proves the PARSER, not the
  contract. If a key name is wrong the measurable consequence is a None and an
  UNKNOWN verdict naming the field, which test_a_key_this_parser_does_not_know_
  becomes_unknown_rather_than_zero demonstrates directly.

ASSERTIONS ARE ON BEHAVIOR, NEVER ON PROSE. Findings carry a `code` alongside
their human message precisely so tests can assert the code: the operator reported
on 2026-09-27 that three tests written that day matched docstring text and passed
while the code was wrong. Nothing below asserts on a message except the two tests
whose SUBJECT is the printed block -- and those assert on the units and on the
literal "(none)", which are rule 6 and rule 14 requirements about the output
itself, not paraphrases of it.
"""

from __future__ import annotations

import ast
import inspect
import math
import sqlite3
from pathlib import Path

import pytest
from db import SCHEMA
from services import market_context as market_context_module
from services import pricing
from services.market_context import (
    MARKET_CONTEXT_COLUMNS,
    THIN_TURNOVER,
    VERDICTS,
    PriceConfidence,
    QuoteWindow,
    expected_drift_bps,
    format_market_context_block,
    price_confidence,
    recent_market_context,
    record_market_context,
    window_volume_usd,
    worst_verdict,
)
from services.pricing import (
    CONTEXT_PARAMS,
    IDS,
    SNAPSHOT_FIELDS,
    MarketSnapshot,
    fetch_market_context,
    fetch_usd_prices,
)

# This repository's own defaults, from config.py, used as the window under test.
# Spelled here rather than imported from Config because these are the values the
# NUMBERS in this file were computed against -- if the operator retunes
# QUOTE_TTL_SECONDS the arithmetic below should be re-derived deliberately, not
# silently follow and keep passing while asserting something else.
QUOTE_TTL = 600.0
RATE_CACHE = 30.0
FEE_BPS = 150.0
EXPOSURE = RATE_CACHE + QUOTE_TTL  # 630 seconds
SECONDS_PER_DAY = 86400.0

# Only ever printed by format_market_context_block(), never opened. A literal under
# /tmp here trips ruff's S108, which is a rule about creating files in a world-writable
# directory -- nothing here creates one, and the fix is to stop writing a path that
# looks like it might rather than to suppress a finding (rule 19).
DISPLAY_DB_PATH = "/srv/swap_terminal/swap_terminal.db"


def window(**overrides) -> QuoteWindow:
    fields = {"quote_ttl_seconds": QUOTE_TTL, "rate_cache_seconds": RATE_CACHE, "fee_bps": FEE_BPS}
    fields.update(overrides)
    return QuoteWindow(**fields)


def snapshot(**overrides) -> MarketSnapshot:
    """A GRC-shaped snapshot that passes every check, so one override isolates one check.

    The baseline numbers are chosen to sit clear of every threshold rather than to
    look like today's GRC: a $1,000,000 cap with $50,000 of 24h volume is a
    turnover of 0.05, fifty times THIN_TURNOVER, and a -1% day scales to 29bps of
    drift against a 150bps fee. A fixture that sat near a boundary would make
    every test's failure ambiguous between the override and the baseline.
    """
    fields = {
        "asset": "GRC",
        "coingecko_id": "gridcoin-research",
        "price_usd": 0.05,
        "market_cap_usd": 1_000_000.0,
        "volume_24h_usd": 50_000.0,
        "change_24h_pct": -1.0,
        "source_updated_at": 1_759_000_000,
        "fetched_at": 1_759_000_060.0,
    }
    fields.update(overrides)
    return MarketSnapshot(**fields)


def codes(confidence: PriceConfidence) -> set[str]:
    return {finding.code for finding in confidence.findings}


class RecordingTransport:
    """Stands in for requests.get. Counts calls and hands back a body this file wrote.

    Counting is the point of the class rather than a lambda: "the context costs no
    extra HTTP round trip" is the central claim of the change, and the only way to
    prove it is to assert on the number of requests made.
    """

    def __init__(self, body: dict):
        self.body = body
        self.calls: list[dict] = []

    def __call__(self, url, params=None, timeout=None):
        self.calls.append({"url": url, "params": params, "timeout": timeout})
        return self

    # The response half of the same object, so there is one thing to reason about.
    def raise_for_status(self):
        return None

    def json(self):
        return self.body


def full_body(**per_asset) -> dict:
    """A complete enriched response for every id in IDS, optionally overridden per id."""
    body = {}
    for cg_id in IDS.values():
        entry = {
            "usd": 1.25,
            "usd_market_cap": 2_000_000.0,
            "usd_24h_vol": 80_000.0,
            "usd_24h_change": -2.5,
            "last_updated_at": 1_759_000_000,
        }
        entry.update(per_asset.get(cg_id, {}))
        body[cg_id] = entry
    return body


@pytest.fixture(autouse=True)
def _clear_price_cache():
    """The cache is process-wide, so a test that left one behind would seed the next.

    Cleared before AND after: before so an earlier test's fetch cannot satisfy this
    one's TTL check and make a request count of zero look like a pass, and after so
    this file cannot leak a stubbed body into the rest of the suite.
    """
    pricing._cache.update({"raw": None, "prices": None, "context": None, "fetched_at": 0.0, "expires_at": 0.0})
    yield
    pricing._cache.update({"raw": None, "prices": None, "context": None, "fetched_at": 0.0, "expires_at": 0.0})


# ---------------------------------------------------------------------------
# One request, two views
# ---------------------------------------------------------------------------


def test_the_context_flags_ride_the_request_that_was_already_being_made(monkeypatch):
    """The whole justification for this shape: the context is free.

    CoinGecko's /simple/price accepts include_market_cap, include_24hr_vol,
    include_24hr_change and include_last_updated_at as parameters on the same
    request, so asking for them costs no extra round trip and no extra
    rate-limit budget. If a future edit split the context into a second call,
    this test goes red on the call count -- which is the failure worth catching,
    because a second call would still return correct data while doubling the
    requests against a public API that rate-limits.
    """
    transport = RecordingTransport(full_body())
    monkeypatch.setattr(pricing.requests, "get", transport)
    fetch_market_context(30)
    assert len(transport.calls) == 1
    params = transport.calls[0]["params"]
    for flag in CONTEXT_PARAMS:
        assert params[flag] == "true"
    assert params["vs_currencies"] == "usd"


def test_the_flags_are_derived_from_the_one_table_and_not_listed_twice():
    """Rule 11: one vocabulary, derived in one place.

    CONTEXT_PARAMS is built from _CONTEXT_FIELDS, so a flag sent without its
    response key being read -- or a key read without its flag being sent -- is not
    expressible. A hand-written parameter dict beside a hand-written parser is the
    shape that fails silently: the response simply does not contain what the reader
    expected, and nothing raises.
    """
    assert set(CONTEXT_PARAMS) == {flag for flag, _key, _field in pricing._CONTEXT_FIELDS}
    assert len(CONTEXT_PARAMS) == len(pricing._CONTEXT_FIELDS)


def test_prices_and_context_together_make_one_request_not_two(monkeypatch):
    """Both views read the one cache, so whichever runs second makes no request.

    This is what makes the sibling function cheap rather than a doubling. It is
    also what open_swap.py's cache-warming comment already relies on for prices
    alone, extended to the context.
    """
    transport = RecordingTransport(full_body())
    monkeypatch.setattr(pricing.requests, "get", transport)
    fetch_usd_prices(30)
    fetch_market_context(30)
    fetch_usd_prices(30)
    assert len(transport.calls) == 1


def test_the_existing_return_shape_did_not_change(monkeypatch):
    """fetch_usd_prices() has exactly the keys it had before: one per asset, plus fetched_at.

    Two callers make this load-bearing rather than tidy. open_swap.py:686 prints
    `len(prices) - 1` as a count of prices, a subtraction hard-coded to the one
    non-price key the dict has ever carried; and routes/rates.py:21 returns this
    dict as the /api/rates response body, so an extra key is a change to an HTTP
    contract. Widening it would have made the first line print a number that means
    something other than what it says, which is rule 14's defect.
    """
    transport = RecordingTransport(full_body())
    monkeypatch.setattr(pricing.requests, "get", transport)
    prices = fetch_usd_prices(30)
    assert set(prices) == {f"{asset}_USD" for asset in IDS} | {"fetched_at"}
    assert len(prices) - 1 == len(IDS)


def test_a_response_missing_one_asset_still_refuses_the_whole_fetch(monkeypatch):
    """The partial-response refusal moved into _fetch_raw() and must still fire, for BOTH views.

    It used to live inside fetch_usd_prices(). It is now in the shared fetch, so
    the context view inherits it -- which is the point: "a swap priced off a
    missing leg is a swap priced wrong" applies to a snapshot as much as to a
    price, and a KeyError naming the asset is what open_swap.py:655 documents by
    name and catches.
    """
    body = full_body()
    del body[IDS["GRC"]]
    monkeypatch.setattr(pricing.requests, "get", RecordingTransport(body))
    with pytest.raises(KeyError) as prices_error:
        fetch_usd_prices(30)
    assert "GRC" in str(prices_error.value)
    pricing._cache.update({"raw": None, "prices": None, "context": None, "expires_at": 0.0})
    with pytest.raises(KeyError) as context_error:
        fetch_market_context(30)
    assert "GRC" in str(context_error.value)


# ---------------------------------------------------------------------------
# Partial data: the case that had to be handled well rather than crashed on
# ---------------------------------------------------------------------------


def test_an_absent_context_field_is_none_and_never_zero(monkeypatch):
    """CoinGecko returns partial data for thin assets, and GRC is a thin asset.

    A market cap coerced to 0.0 would be indistinguishable from a real zero, and
    price_confidence() would then report a confident THIN turnover verdict about a
    number nobody measured. None is the only value that carries "nobody said".
    """
    body = full_body(**{IDS["GRC"]: {"usd": 0.05}})
    for key in ("usd_market_cap", "usd_24h_vol", "usd_24h_change", "last_updated_at"):
        body[IDS["GRC"]].pop(key, None)
    monkeypatch.setattr(pricing.requests, "get", RecordingTransport(body))
    grc = next(shot for shot in fetch_market_context(30) if shot.asset == "GRC")
    assert grc.price_usd == 0.05
    assert grc.market_cap_usd is None
    assert grc.volume_24h_usd is None
    assert grc.change_24h_pct is None
    assert grc.source_updated_at is None


def test_a_null_or_unparseable_context_field_is_none_too(monkeypatch):
    """JSON null and a non-numeric string are the other two ways a field arrives unusable.

    float(None) raises TypeError and float("n/a") raises ValueError; both are
    caught by name in _optional_float and become None. Catching them is legitimate
    under rule 12 precisely because the caller can still tell the failure from a
    real answer -- a real answer is a float.
    """
    body = full_body(
        **{IDS["GRC"]: {"usd": 0.05, "usd_market_cap": None, "usd_24h_vol": "n/a", "usd_24h_change": None}}
    )
    monkeypatch.setattr(pricing.requests, "get", RecordingTransport(body))
    grc = next(shot for shot in fetch_market_context(30) if shot.asset == "GRC")
    assert grc.market_cap_usd is None
    assert grc.volume_24h_usd is None
    assert grc.change_24h_pct is None


def test_a_key_this_parser_does_not_know_becomes_unknown_rather_than_zero(monkeypatch):
    """What happens if CoinGecko's real key names are not the ones this code reads.

    They are UNVERIFIED -- api.coingecko.com is unreachable from here (CONNECT
    403) and nothing in the tree had a recorded enriched response. So the failure
    mode matters more than usual, and this test pins it: a body that spells the
    fields differently produces Nones and an UNKNOWN verdict that NAMES the
    missing fields, not a crash and not a silently wrong number. The price, which
    this code has always read, is unaffected.
    """
    body = full_body(
        **{
            IDS["GRC"]: {
                "usd": 0.05,
                "market_cap": 1_000_000.0,
                "total_volume": 50_000.0,
                "price_change_percentage_24h": -3.0,
            }
        }
    )
    for key in ("usd_market_cap", "usd_24h_vol", "usd_24h_change", "last_updated_at"):
        body[IDS["GRC"]].pop(key, None)
    monkeypatch.setattr(pricing.requests, "get", RecordingTransport(body))
    grc = next(shot for shot in fetch_market_context(30) if shot.asset == "GRC")
    assert grc.price_usd == 0.05
    confidence = price_confidence(grc, window())
    assert confidence.verdict == "UNKNOWN"
    assert codes(confidence) >= {"market_cap_unavailable", "volume_unavailable", "change_unavailable"}


def test_a_flat_market_is_an_answer_and_absence_is_not():
    """0.0 and None must not be treated alike, and this is where that is decided.

    change_24h_pct == 0.0 means the price did not move, which is a fact about a
    quiet market and is perfectly judgeable: it scales to 0bps of drift. None means
    nobody reported a change, and no bound can be put on the drift at all. A
    version of this code that coerced the missing case to 0.0 would report the most
    reassuring possible verdict for the least information.
    """
    flat = price_confidence(snapshot(change_24h_pct=0.0), window())
    assert "change_unavailable" not in codes(flat)
    assert "drift_vs_fee" in codes(flat)
    assert flat.verdict == "OK"

    absent = price_confidence(snapshot(change_24h_pct=None), window())
    assert "change_unavailable" in codes(absent)
    assert "drift_vs_fee" not in codes(absent)
    assert "drift_exceeds_fee" not in codes(absent)
    assert absent.verdict == "UNKNOWN"


def test_a_zero_market_cap_reports_as_unavailable_rather_than_dividing_by_it():
    """A literal zero cap is unusable for turnover and would be a ZeroDivisionError.

    It is reported as unavailable, with the repr in the message so a reader can see
    it was a zero rather than a None. What must not happen is a traceback out of a
    diagnostic, and what must not happen either is a turnover of infinity being
    compared against a threshold.
    """
    confidence = price_confidence(snapshot(market_cap_usd=0.0), window())
    assert confidence.verdict == "UNKNOWN"
    assert "market_cap_unavailable" in codes(confidence)
    assert "thin_turnover" not in codes(confidence)
    assert "turnover" not in codes(confidence)


@pytest.mark.parametrize("bad_price", [None, 0.0, -1.0, float("nan"), float("inf")])
def test_an_unusable_price_short_circuits_to_unknown(bad_price):
    """With no positive finite price, every other check is arithmetic on nothing.

    NaN is in the list deliberately: it compares False against every threshold, so
    a version that only tested `price is None or price <= 0` would let a NaN
    through and report OK. That is the silent failure this parametrization exists
    to make loud.
    """
    confidence = price_confidence(snapshot(price_usd=bad_price), window())
    assert confidence.verdict == "UNKNOWN"
    assert codes(confidence) == {"price_unusable"}


# ---------------------------------------------------------------------------
# The drift check: sqrt of time, and the fee boundary
# ---------------------------------------------------------------------------


def test_drift_scales_with_the_square_root_of_time_not_linearly():
    """The scaling choice IS the check, and linear proration would disable it.

    A price is a random walk to a first approximation, so expected displacement
    grows with sqrt(t). At this system's 630-second exposure window that is
    sqrt(630/86400) = 0.0854 of the daily move, against 630/86400 = 0.00729 for
    linear proration -- an 11.7x difference. Under linear proration a 150bps
    threshold would need a 206% day to fire; under sqrt-of-time it fires at 17.6%.
    Gridcoin has 17.6% days. This test asserts the ratio, so a silent switch back
    to proration goes red here rather than in six months of verdicts that never
    fired.
    """
    linear_fraction = EXPOSURE / SECONDS_PER_DAY
    sqrt_fraction = math.sqrt(EXPOSURE / SECONDS_PER_DAY)
    assert expected_drift_bps(100.0, EXPOSURE) == pytest.approx(100.0 * 100.0 * sqrt_fraction)
    assert expected_drift_bps(100.0, EXPOSURE) / (100.0 * 100.0 * linear_fraction) == pytest.approx(11.7, abs=0.1)


def test_drift_is_signless_because_a_fall_costs_the_same_as_a_rise():
    """The desk's exposure is to magnitude. A -20% day is not safer than a +20% day."""
    assert expected_drift_bps(-20.0, EXPOSURE) == expected_drift_bps(20.0, EXPOSURE)


def test_a_drift_exactly_at_the_fee_is_stale():
    """The boundary is >=, not >, and the boundary is where a guard is broken by accident.

    A drift exactly equal to the fee leaves the desk nothing, and the figure is an
    approximation to begin with (change_24h is one realized return, not a sigma),
    so the boundary belongs on the cautious side. The change that puts drift at
    exactly FEE_BPS is computed by inverting expected_drift_bps rather than
    guessed, so this test asserts the boundary rather than a number near it.
    """
    change_at_boundary = FEE_BPS / (100.0 * math.sqrt(EXPOSURE / SECONDS_PER_DAY))
    assert expected_drift_bps(change_at_boundary, EXPOSURE) == pytest.approx(FEE_BPS)
    at = price_confidence(snapshot(change_24h_pct=change_at_boundary), window())
    assert at.verdict == "STALE"
    assert "drift_exceeds_fee" in codes(at)
    # `reason` must be the WORST finding's message, not the first one's. This snapshot
    # produces an informational turnover finding BEFORE the stale drift finding, so a
    # version that reported findings[0] would hand the operator a reassuring sentence
    # about turnover as the reason a quote was flagged. Caught by mutation: taking
    # findings[0] left every other assertion in this file green.
    assert at.findings[0].verdict == "OK"
    assert at.reason == next(f.message for f in at.findings if f.code == "drift_exceeds_fee")

    just_under = price_confidence(snapshot(change_24h_pct=change_at_boundary * 0.99), window())
    assert "drift_exceeds_fee" not in codes(just_under)
    assert "drift_vs_fee" in codes(just_under)
    assert just_under.verdict == "OK"


def test_the_drift_check_reads_the_sum_of_both_windows(monkeypatch):
    """Exposure is RATE_CACHE_SECONDS + QUOTE_TTL_SECONDS, not either alone.

    A customer accepting at the last legal instant acts on a price fetched up to
    the cache TTL before the quote was created and honored for the quote TTL after
    it. Asserted behaviorally: a change that is NOT stale against the quote TTL
    alone IS stale against the sum, so a version reading only one window would go
    red here.
    """
    change_for_ttl_only = FEE_BPS / (100.0 * math.sqrt(QUOTE_TTL / SECONDS_PER_DAY))
    assert expected_drift_bps(change_for_ttl_only, QUOTE_TTL) == pytest.approx(FEE_BPS)
    just_under_ttl_alone = change_for_ttl_only * 0.98
    assert expected_drift_bps(just_under_ttl_alone, QUOTE_TTL) < FEE_BPS
    assert expected_drift_bps(just_under_ttl_alone, EXPOSURE) > FEE_BPS
    confidence = price_confidence(snapshot(change_24h_pct=just_under_ttl_alone), window())
    assert confidence.verdict == "STALE"


# ---------------------------------------------------------------------------
# The thinness checks
# ---------------------------------------------------------------------------


def test_a_swap_at_least_as_large_as_its_windows_volume_is_thin():
    """The size check: the swap must not be the market for its own validity window.

    window_volume_usd() is inverted to place the notional exactly at the boundary,
    so this asserts the >= rather than a number near it. Below the boundary the
    finding is informational and the verdict is OK.
    """
    volume = 50_000.0
    expected = window_volume_usd(volume, EXPOSURE)
    assert expected == pytest.approx(volume * EXPOSURE / SECONDS_PER_DAY)

    at = price_confidence(snapshot(volume_24h_usd=volume), window(), swap_notional_usd=expected)
    assert at.verdict == "THIN"
    assert "notional_exceeds_window_volume" in codes(at)

    under = price_confidence(snapshot(volume_24h_usd=volume), window(), swap_notional_usd=expected * 0.5)
    assert under.verdict == "OK"
    assert "notional_share_of_window_volume" in codes(under)


def test_no_swap_size_reports_the_window_volume_and_does_not_pass_the_size_check():
    """Rule 14: (none) is a result. A share nobody asked for must not read as zero.

    Without a notional there is no fraction to take, so the finding reports the
    window volume and the code says which question was answered. A version that
    emitted notional_share_of_window_volume with a zero share would be a confident
    answer to a question nobody asked.
    """
    confidence = price_confidence(snapshot(), window())
    assert "window_volume" in codes(confidence)
    assert "notional_share_of_window_volume" not in codes(confidence)
    assert "notional_exceeds_window_volume" not in codes(confidence)
    assert confidence.verdict == "OK"


def test_turnover_below_the_threshold_is_thin_regardless_of_swap_size():
    """Thinness the swap does not cause: a price set by an amount one actor can deploy.

    The threshold is inverted to sit exactly at it, and THIN_TURNOVER is read from
    the module rather than respelled, so retuning it does not leave this test
    asserting an old number. A turnover AT the threshold passes -- the check is
    strictly below -- and that boundary is asserted in both directions.
    """
    cap = 1_000_000.0
    at_threshold = price_confidence(
        snapshot(market_cap_usd=cap, volume_24h_usd=cap * THIN_TURNOVER), window()
    )
    assert "turnover" in codes(at_threshold)
    assert "thin_turnover" not in codes(at_threshold)

    below = price_confidence(snapshot(market_cap_usd=cap, volume_24h_usd=cap * THIN_TURNOVER * 0.9), window())
    assert below.verdict == "THIN"
    assert "thin_turnover" in codes(below)


# ---------------------------------------------------------------------------
# The verdict vocabulary
# ---------------------------------------------------------------------------


def test_unknown_outranks_thin_which_outranks_stale():
    """All three pairs of the ranking, each proven through a snapshot that triggers both.

    THE FIRST VERSION OF THIS TEST DID NOT COVER THE RANKING IT IS NAMED FOR, and
    that was caught by mutation rather than by reading it: reordering VERDICTS to
    put UNKNOWN BELOW THIN left the whole file green. The reason is that the
    original snapshot produced UNKNOWN and STALE findings and no THIN one, so
    swapping UNKNOWN and THIN could not change the answer -- UNKNOWN outranked
    STALE either way. A test of an ordering has to make both sides of each
    comparison actually occur.

    So each case below fires two checks at different severities and asserts the
    worse one wins AND that `reason` is that finding's message:

      UNKNOWN vs THIN   no market cap (UNKNOWN) while the swap is larger than its
                        window's volume (THIN). Reporting THIN here would tell the
                        operator a measured story about thinness for a snapshot
                        whose float was never measured.
      THIN vs STALE     turnover below the threshold (THIN) while the 24h move
                        scales past the fee (STALE). A stale price is a real price
                        that existed; a thin one may never have.
      UNKNOWN vs STALE  no market cap while the move is past the fee.
    """
    change_past_fee = 2 * FEE_BPS / (100.0 * math.sqrt(EXPOSURE / SECONDS_PER_DAY))
    cap = 1_000_000.0

    unknown_and_thin = price_confidence(
        snapshot(market_cap_usd=None, volume_24h_usd=50_000.0, change_24h_pct=-1.0),
        window(),
        swap_notional_usd=1e9,
    )
    assert {"market_cap_unavailable", "notional_exceeds_window_volume"} <= codes(unknown_and_thin)
    assert unknown_and_thin.verdict == "UNKNOWN"
    assert unknown_and_thin.reason == next(
        f.message for f in unknown_and_thin.findings if f.code == "market_cap_unavailable"
    )

    thin_and_stale = price_confidence(
        snapshot(market_cap_usd=cap, volume_24h_usd=cap * THIN_TURNOVER * 0.5, change_24h_pct=change_past_fee),
        window(),
    )
    assert {"thin_turnover", "drift_exceeds_fee"} <= codes(thin_and_stale)
    assert thin_and_stale.verdict == "THIN"
    assert thin_and_stale.reason == next(
        f.message for f in thin_and_stale.findings if f.code == "thin_turnover"
    )

    unknown_and_stale = price_confidence(
        snapshot(market_cap_usd=None, change_24h_pct=change_past_fee), window()
    )
    assert {"market_cap_unavailable", "drift_exceeds_fee"} <= codes(unknown_and_stale)
    assert unknown_and_stale.verdict == "UNKNOWN"


def test_worst_verdict_refuses_a_verdict_outside_the_vocabulary():
    """An unranked verdict must raise, not sort to the harmless end.

    A check emitting a verdict nothing ranks would otherwise be silently treated as
    the mildest, which is a check that can never fire -- the exact failure Mammon's
    rule 19 describes finding when two of five sell vetoes turned out unreachable.
    """
    assert worst_verdict([]) == "OK"
    assert worst_verdict(["OK", "THIN", "STALE"]) == "THIN"
    with pytest.raises(KeyError):
        worst_verdict(["OK", "PROBABLY_FINE"])


def test_every_finding_a_check_can_emit_uses_a_verdict_in_the_vocabulary():
    """Exercises each check across its branches and asserts the verdicts are all known.

    Not a text check: it calls the real function over inputs chosen to reach every
    branch, and asserts that whatever came back is rankable. A new check whose
    verdict is a typo goes red here.
    """
    change_at_boundary = FEE_BPS / (100.0 * math.sqrt(EXPOSURE / SECONDS_PER_DAY))
    cases = [
        price_confidence(snapshot(), window()),
        price_confidence(snapshot(), window(), swap_notional_usd=1.0),
        price_confidence(snapshot(), window(), swap_notional_usd=1e9),
        price_confidence(snapshot(change_24h_pct=change_at_boundary), window()),
        price_confidence(snapshot(market_cap_usd=None, volume_24h_usd=None, change_24h_pct=None), window()),
        price_confidence(snapshot(volume_24h_usd=1.0), window()),
        price_confidence(snapshot(price_usd=None), window()),
    ]
    seen = set()
    for confidence in cases:
        assert confidence.verdict in VERDICTS
        for finding in confidence.findings:
            assert finding.verdict in VERDICTS
            assert finding.code
            seen.add(finding.code)
    assert seen >= {
        "price_unusable",
        "market_cap_unavailable",
        "volume_unavailable",
        "change_unavailable",
        "thin_turnover",
        "turnover",
        "window_volume",
        "notional_share_of_window_volume",
        "notional_exceeds_window_volume",
        "drift_exceeds_fee",
        "drift_vs_fee",
    }


def test_the_decision_makes_no_network_call(monkeypatch):
    """Purity, proven by making the transport explode rather than by reading the code.

    price_confidence() is the piece that must be callable with seeded inputs
    (rule 10), and a hidden fetch inside it would make every verdict depend on a
    live API. Replacing requests.get with a raise is the behavioral proof.
    """
    def explode(*_args, **_kwargs):
        raise AssertionError("price_confidence() must not touch the network")

    monkeypatch.setattr(pricing.requests, "get", explode)
    assert price_confidence(snapshot(), window()).verdict == "OK"


def test_the_decision_is_not_wired_into_the_quote_or_payout_path():
    """Rule 16: changing what gets quoted at what price is the operator's call.

    Asserted by reading the SOURCE of the modules that would have to call it --
    which is a text check, and is the right shape here precisely because the claim
    IS about absence: there is no behavior to observe when nothing calls a
    function. Grepped by NAME across the package rather than through the import
    graph (rule 2), because a call added through getattr or a late import would not
    appear in an import.
    """
    package = Path(market_context_module.__file__).resolve().parent.parent
    # The modules that would have to do the wiring: everything that decides a quote, a
    # payout or an HTTP response, plus the worker loops. db.py is excluded on purpose --
    # it names `market_context` as a TABLE in SQL, which is the persistence this change
    # is for and not a call to the decision.
    candidates = [
        *sorted((package / "services").glob("*.py")),
        *sorted((package / "routes").glob("*.py")),
        *sorted((package / "workers").glob("*.py")),
    ]
    assert len(candidates) > 10, f"expected the service/route/worker modules, found {len(candidates)}"
    # WALKED AS AN AST, NOT GREPPED, and the first draft of this test is why. A substring
    # search for "price_confidence(" matched services/pricing.py's own DOCSTRING, which
    # points a reader at the decision by name -- so the test failed on a sentence that is
    # exactly what rule 1 asks for. A comment naming a function is documentation; a
    # ast.Call node naming it is a wiring. Only the second is what rule 16 is about, and
    # only the AST can tell them apart.
    offenders = []
    for path in candidates:
        if path.name == "market_context.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
                if name in {"price_confidence", "collect_and_record", "confidence_for"}:
                    offenders.append(f"{path.name}:{node.lineno} calls {name}()")
            elif isinstance(node, ast.ImportFrom) and "market_context" in (node.module or ""):
                offenders.append(f"{path.name}:{node.lineno} imports from {node.module}")
            elif isinstance(node, ast.Import):
                offenders.extend(
                    f"{path.name}:{node.lineno} imports {alias.name}"
                    for alias in node.names
                    if "market_context" in alias.name
                )
    assert offenders == [], f"the decision is wired somewhere it was not meant to be: {offenders}"


# ---------------------------------------------------------------------------
# The row, in the real schema
# ---------------------------------------------------------------------------


@pytest.fixture
def db(tmp_path: Path):
    """A real database built from db.py's REAL SCHEMA, not a hand-written CREATE TABLE.

    Behavioral verification (CLAUDE.md's closing section): a test that writes its
    own approximation of the table proves nothing about the one the application
    builds. This runs the same executescript() every worker runs at startup.
    """
    conn = sqlite3.connect(tmp_path / "market_context_test.db")
    conn.row_factory = lambda cursor, row: {col[0]: row[i] for i, col in enumerate(cursor.description)}
    conn.executescript(SCHEMA)
    conn.commit()
    yield conn
    conn.close()


def test_the_tables_columns_are_the_snapshots_fields(db):
    """The one place the DDL and the dataclass could drift apart, closed by a test.

    db.py deliberately does NOT import services/pricing.py -- it is imported by
    every worker at startup and pulling `requests` in for a column list would be a
    real cost -- so the column names are spelled in the DDL and derived in the
    INSERT. That is exactly rule 8's two-copies shape, and the honest answer is a
    check rather than a comment claiming they match.

    Read from PRAGMA table_info, which is the schema as SQLite actually built it,
    not as the DDL text spells it.
    """
    built = [row["name"] for row in db.execute("PRAGMA table_info(market_context)")]
    assert MARKET_CONTEXT_COLUMNS == SNAPSHOT_FIELDS
    assert built == ["id", *MARKET_CONTEXT_COLUMNS, "recorded_at"]


def test_a_null_context_field_round_trips_as_none_through_sql(db):
    """The whole point of the nullable columns, checked against the database.

    A DEFAULT 0 on any of the three, or a NOT NULL, would turn a thin asset's
    missing datum into a real-looking zero at the moment it is persisted -- and
    then every later comparison would be against a number nobody measured. Written
    and read back through the real schema.
    """
    record_market_context(
        db,
        [snapshot(market_cap_usd=None, volume_24h_usd=None, change_24h_pct=None, source_updated_at=None)],
        "2026-09-27T00:00:00+00:00",
    )
    row = recent_market_context(db, "GRC")[0]
    assert row["market_cap_usd"] is None
    assert row["volume_24h_usd"] is None
    assert row["change_24h_pct"] is None
    assert row["source_updated_at"] is None
    assert row["price_usd"] == 0.05


def test_recording_appends_and_reads_back_newest_first(db):
    """History is the deliverable: "better establish grc prices" is a question about rows.

    Two fetches an hour apart, both kept, newest first. A writer that UPDATEd in
    place would leave one row and this test would go red on the count -- which is
    the failure that matters, because the comparison over time is the entire
    reason the table exists.
    """
    record_market_context(db, [snapshot(price_usd=0.05, fetched_at=1_000.0)], "2026-09-27T00:00:00+00:00")
    record_market_context(db, [snapshot(price_usd=0.07, fetched_at=4_600.0)], "2026-09-27T01:00:00+00:00")
    rows = recent_market_context(db, "GRC")
    assert [row["price_usd"] for row in rows] == [0.07, 0.05]
    assert [row["fetched_at"] for row in rows] == [4_600.0, 1_000.0]


def test_a_duplicate_observation_is_not_an_error(db):
    """Two processes inside one cache window report the same fetched_at, and that is fine.

    A UNIQUE(asset, fetched_at) would turn a harmless duplicate into an
    IntegrityError on a diagnostic path, which is lost evidence to prevent a
    non-problem. Asserted rather than asserted-about: the second write succeeds and
    both rows are there.
    """
    shot = snapshot()
    record_market_context(db, [shot], "2026-09-27T00:00:00+00:00")
    record_market_context(db, [shot], "2026-09-27T00:00:01+00:00")
    assert len(recent_market_context(db, "GRC")) == 2


def test_the_asset_filter_is_a_parameter_and_selects_only_that_asset(db):
    """One asset's history, and the filter is a bound parameter rather than interpolated."""
    record_market_context(db, [snapshot(asset="GRC"), snapshot(asset="XRP", price_usd=2.5)], "2026-09-27T00:00:00+00:00")
    assert [row["asset"] for row in recent_market_context(db, "GRC")] == ["GRC"]
    assert [row["price_usd"] for row in recent_market_context(db, "XRP")] == [2.5]


def test_nothing_in_this_module_updates_or_deletes_a_row():
    """Append-only, established over the module's own source rather than asserted in prose.

    Rule 7: the record of what the system observed is evidence, and a row that can
    be corrected is a row that can be corrected wrongly with nothing downstream
    able to tell. There are no triggers enforcing this -- db.py's comment says why
    -- so this is what holds it.
    """
    source = inspect.getsource(market_context_module).upper()
    assert "UPDATE MARKET_CONTEXT" not in source
    assert "DELETE FROM MARKET_CONTEXT" not in source


# ---------------------------------------------------------------------------
# The report
# ---------------------------------------------------------------------------


def test_an_empty_snapshot_list_prints_none_rather_than_nothing():
    """Rule 14: (none) is a result; a blank gap cannot be told from a query that broke.

    Asserting on the literal "(none)" is asserting on the output, which IS this
    function's behavior -- it is not matching a docstring.
    """
    block = format_market_context_block([], window(), DISPLAY_DB_PATH)
    assert "(none)" in block
    assert DISPLAY_DB_PATH in block


def test_the_block_echoes_the_parameters_that_decide_the_answer():
    """Pasted output has to be self-describing a day later, because it is read a day later.

    The verdict is meaningless without the two windows and the fee it was computed
    against, so all three are on the screen next to it.
    """
    block = format_market_context_block([snapshot()], window(), DISPLAY_DB_PATH)
    assert "RATE_CACHE_SECONDS" in block
    assert "QUOTE_TTL_SECONDS" in block
    assert "DEFAULT_FEE_BPS" in block
    assert "GRC" in block


def test_every_duration_in_the_block_is_microfortnights_with_the_micro_sign():
    """Rule 6, and the half of it that drifts: µ (U+00B5), never an ASCII u, no space.

    An ASCII "u" in displayed output is a defect the same as a wrong number, and
    every script ever written for this rule has drifted to "ufn" because ASCII is
    what fingers type and nothing failed when it did. This is what fails.
    """
    block = format_market_context_block([snapshot()], window(), DISPLAY_DB_PATH)
    assert "µfn" in block
    assert "ufn" not in block
    assert " µfn" not in block
    # 630s / 1.2096 = 520.83, so the exposure window prints as 520.8µfn. The figure is
    # asserted rather than only the unit, because a conversion that used the wrong
    # constant would still contain "µfn". (The first draft of this line asserted 521.0
    # from arithmetic done in my head and went red -- which is the assertion earning its
    # place, and rule 17's point that a number should be run rather than reckoned.)
    assert "520.8µfn (630.0s)" in block


def test_a_missing_feed_timestamp_prints_none_rather_than_an_arithmetic_error():
    """source_updated_at is optional, and the feed-age column is a subtraction over it.

    The block must not raise, and must not print a plausible-looking age, when the
    feed did not say when it last updated.
    """
    block = format_market_context_block([snapshot(source_updated_at=None)], window(), DISPLAY_DB_PATH)
    assert "feed_age=(none)" in block
