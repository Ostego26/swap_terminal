#!/usr/bin/env python3
"""One quote cannot spend longer fetching prices than the server will wait for it.

Role: file (entry point -- `python3 -m pytest tests/test_price_fetch_is_bounded.py`)
Reads: services/pricing.py, gunicorn.conf.py
Writes: nothing
Can move funds: no
Live-safe: yes

Operator, 2026-10-08: "also there's something wrong with the termianl swap
portion. it freezes up."

WHAT WAS MEASURED FROM THE CODE, which is not the same as diagnosing their
freeze and is worth separating (rule 17):

    coingecko      1 request  x 15s =  15s
    coinpaprika    6 requests x 15s =  90s   ONE REQUEST PER ASSET, sequential
    worst case                       105s   in a single web request
    gunicorn timeout                  60s   <- the worker is KILLED here
    gunicorn workers                    2   <- and the other one is all that is left

Every per-call timeout is defensible on its own and the TOTAL is the defect: the
price path can outrun the server running it by 45 seconds. When it does,
gunicorn kills the worker mid-request, so the browser gets a hang and then a
dropped connection rather than an error page -- and with two sync workers, one
more request in that window leaves the whole site unresponsive. A customer's
swap page polls every 15s, so that second request is not hypothetical.

Whether the operator's feeds are actually timing out has not been measured by
anyone. What is established is the arithmetic, and these tests hold the fix to
it.
"""

from __future__ import annotations

import re
import sys
import time
from pathlib import Path

import pytest
import requests

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "swap_terminal"))

from services import coinpaprika, pricing  # noqa: E402 -- after the sys.path.insert, same as every test here

#: How much longer than its own budget a bounded fetch may take before this
#: calls it unbounded. Generous on purpose: a tight margin would fail on a slow
#: machine while proving nothing extra.
SLACK_SECONDS = 3.0

#: The budget the behavioral tests run against, instead of the real 45s default.
#:
#: THE SUITE IS NOT THE PLACE TO SPEND FORTY-FIVE SECONDS PROVING A TIMEOUT. The
#: first version of this file ran the real default and added 45s to every suite
#: run and to every mutation of it -- rule 3 says leave it faster, and a test
#: nobody wants to run is a test that gets marked skip.
#:
#: Nothing is lost by shrinking it. The behavior under test is "is the deadline
#: honored and does the per-asset loop stop", which is the same mechanism at 3s
#: as at 45s; whether the DEFAULT is a safe number is a separate claim, and
#: test_the_budget_is_under_the_timeout_that_kills_the_worker makes it against
#: the real value.
TEST_BUDGET_SECONDS = 3.0


@pytest.fixture
def every_feed_hangs(monkeypatch):
    """Make every outbound price call take its full timeout and then fail.

    THE CONDITION THE BUDGET EXISTS FOR, reproduced rather than imagined: a feed
    that is unreachable and slow about saying so is what turns six sequential
    requests into ninety seconds. A stub that fails instantly would exercise the
    fallback and prove nothing about the total.
    """
    def hang(url, *args, timeout=None, **kwargs):
        time.sleep(timeout or 0)
        raise requests.exceptions.ReadTimeout(f"stub: no answer in {timeout}s")

    monkeypatch.setattr(requests, "get", hang)
    monkeypatch.setattr(pricing, "PRICE_FETCH_BUDGET_SECONDS", TEST_BUDGET_SECONDS)
    pricing._cache["raw"] = None
    pricing._cache["expires_at"] = 0.0
    yield
    pricing._cache["raw"] = None
    pricing._cache["expires_at"] = 0.0


def test_call_timeout_is_a_ceiling_and_not_a_replacement():
    """A call with plenty of budget still gets its own timeout, unchanged."""
    assert pricing.call_timeout(deadline=145.0, now=100.0, per_call=15.0) == 15.0
    assert pricing.call_timeout(deadline=120.0, now=100.0, per_call=15.0) == 15.0


def test_the_last_call_is_squeezed_rather_than_allowed_to_overrun():
    """The off-by-one that puts the total back over the limit.

    Letting the final call run with its full per-call timeout is the easiest way
    to write a budget that does not bound anything: five calls inside the budget
    plus one 15s overrun is still 15s past the server's patience, and nothing
    about the code looks different when it happens.
    """
    assert pricing.call_timeout(deadline=112.0, now=100.0, per_call=15.0) == 12.0


def test_a_call_with_almost_no_budget_left_is_not_started():
    """Starting a request with half a second left buys a guaranteed timeout.

    It cannot succeed, it costs the half second, and -- worse -- it fails in a
    way indistinguishable from the feed being down, so the refusal blames the
    feed for this file's own bookkeeping.
    """
    assert pricing.call_timeout(deadline=100.4, now=100.0, per_call=15.0) == 0.0
    assert pricing.call_timeout(deadline=100.0, now=100.0, per_call=15.0) == 0.0
    assert pricing.call_timeout(deadline=97.0, now=100.0, per_call=15.0) == 0.0


def test_the_whole_fetch_is_bounded_when_every_feed_hangs(every_feed_hangs):
    """The measurement, end to end, through the real _fetch_raw.

    Behavioral: both feeds are driven, the fallback runs, the per-asset loop
    runs, and the clock is read. Asserting on call_timeout() alone would pass
    just as happily with the budget threaded nowhere.
    """
    started = time.monotonic()
    with pytest.raises(Exception) as raised:
        pricing._fetch_raw(30)
    took = time.monotonic() - started

    assert took <= TEST_BUDGET_SECONDS + SLACK_SECONDS, (
        f"one quote spent {took:.1f}s fetching prices against a {TEST_BUDGET_SECONDS}s budget. "
        f"Unbounded, the same path's worst case is 15s + 6x15s = 105s, and gunicorn kills the "
        f"worker at 60"
    )
    assert "budget" in str(raised.value), (
        f"the refusal must say the budget ran out, or an operator reads it as a feed outage "
        f"and goes looking at CoinGecko: {raised.value}"
    )


def test_the_budget_is_under_the_timeout_that_kills_the_worker():
    """TWO NUMBERS IN TWO FILES, and the whole fix is the relationship between them.

    A budget at or above gunicorn's timeout bounds nothing that matters: the
    worker is killed first, which is the failure being fixed. Rule 8 -- the two
    are written down separately and drift silently, and the drift is invisible
    because both files stay internally consistent.

    Read from gunicorn.conf.py's own default rather than restated here, so
    raising the server's timeout does not quietly make this test vacuous.
    """
    source = (REPO_ROOT / "gunicorn.conf.py").read_text(encoding="utf-8")
    match = re.search(r'timeout\s*=\s*int\(os\.getenv\(\s*"GUNICORN_TIMEOUT_SECONDS"\s*,\s*"(\d+)"', source)
    assert match, "gunicorn.conf.py no longer sets `timeout` in the shape this test reads"
    worker_timeout = int(match.group(1))

    assert worker_timeout > pricing.PRICE_FETCH_BUDGET_SECONDS, (
        f"the price-fetch budget is {pricing.PRICE_FETCH_BUDGET_SECONDS}s and gunicorn kills a "
        f"worker at {worker_timeout}s. A budget that is not strictly under it bounds nothing: "
        f"the request dies as a dropped connection instead of a refusal, and with two sync "
        f"workers that takes the rest of the site with it"
    )


def test_an_exhausted_budget_is_reported_as_ours_and_not_as_a_feed_outage():
    """Rule 14: state what the number means. Here, state WHOSE timeout fired.

    A refusal that reads like CoinGecko went down sends the operator to check
    CoinGecko. The remedy for this one is a configuration value in this repo,
    and the message names it.
    """
    with pytest.raises(pricing.PriceSourceError) as raised:
        pricing._budgeted(deadline=100.0, per_call=15.0, feed="CoinGecko")
    message = str(raised.value)
    assert "not called" in message, message
    assert "TIMEOUT OF OUR OWN" in message, (
        f"the message must distinguish our budget from a feed failure: {message}"
    )
    assert "ST_PRICE_FETCH_BUDGET_SECONDS" in message, (
        f"and name the lever, so the operator is not left to find it: {message}"
    )


def test_the_per_asset_loop_stops_instead_of_running_all_six(monkeypatch):
    """The 90 of the 105 seconds, and the only place a loop can overrun a deadline.

    CoinGecko fails instantly so the fallback is reached with the whole budget
    intact; CoinPaprika then hangs for whatever it is given. With a budget
    smaller than two calls, a loop that respects the deadline makes ONE request
    and records the rest as not attempted. A loop that does not makes six, and
    the only visible difference is the clock -- which is exactly how 105s got
    shipped behind six reasonable 15s timeouts.
    """
    attempts: list[str] = []

    def instant_failure(url, *args, **kwargs):
        raise requests.exceptions.ConnectionError("stub: CoinGecko refused at once")

    def hang(asset, *args, timeout=None, **kwargs):
        attempts.append(asset)
        time.sleep(timeout or 0)
        raise requests.exceptions.ReadTimeout(f"stub: no answer in {timeout}s")

    monkeypatch.setattr(requests, "get", instant_failure)
    monkeypatch.setattr(pricing, "PRICE_FETCH_BUDGET_SECONDS", TEST_BUDGET_SECONDS)
    monkeypatch.setattr(coinpaprika, "fetch_quote", hang)
    pricing._cache["raw"] = None
    pricing._cache["expires_at"] = 0.0

    started = time.monotonic()
    with pytest.raises(Exception) as raised:
        pricing._fetch_raw(30)
    took = time.monotonic() - started

    assert len(attempts) < len(pricing.IDS), (
        f"every one of {len(pricing.IDS)} assets was attempted despite the budget: {attempts}. "
        f"The loop is not checking the deadline, so the fallback alone can spend "
        f"{len(pricing.IDS)} x {coinpaprika.TIMEOUT_SECONDS}s"
    )
    assert took <= TEST_BUDGET_SECONDS + SLACK_SECONDS, f"the fallback ran {took:.1f}s"
    assert "not attempted" in str(raised.value), (
        f"the assets that were skipped must be named as SKIPPED, not reported as priced or "
        f"silently dropped -- an operator has to tell 'ran out of time' from 'CoinPaprika "
        f"said 404', which are different problems: {raised.value}"
    )
