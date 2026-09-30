#!/usr/bin/env python3
"""GRC's market cap has to be derived, or its thinness check silently skips.

Role: tests (read-only)
Reads: the JSON the operator's host actually returned on 2026-09-29, pasted in
        below verbatim. No network.
Writes: nothing
Can move funds: no
Mainnet-safe: yes

THE ROWS ARE REAL. Every payload in this file is what CoinPaprika returned from
the operator's machine on 2026-09-29, not a shape this repository imagined. That
is the behavioral-verification principle applied to an API: a seeded test cannot
discover a wire format, so the format is pinned from a response that was actually
read. services/pricing.py's own header records the opposite case -- four field
names taken from documentation rather than a response, marked UNVERIFIED.

WHAT THIS IS REALLY GUARDING. CoinPaprika reports `market_cap: 0` for GRC while
reporting `total_supply: 459382131`. Zero is not a cap; it is a field the feed
does not compute for an asset that small. And market_context.turnover_finding()
returns None when the cap is zero or absent -- so a GRC snapshot taken straight
from this feed produces NO thinness finding at all. The asset whose price most
needs a confidence figure is the one that would silently not get one. Everything
below exists because that failure is invisible.
"""

from __future__ import annotations

import pathlib

import pytest
from services.coinpaprika import (
    PAPRIKA_IDS,
    PaprikaError,
    derive_market_cap,
    fetch_quote,
    pair_rate,
    quote_from_ticker,
)
from services.market_context import THIN_TURNOVER

# Verbatim from the operator's host, 2026-09-29 21:35Z.
GRC_TICKER = {
    "id": "grc-gridcoin", "name": "GridCoin", "symbol": "GRC", "rank": 7533,
    "total_supply": 459382131, "max_supply": 0, "beta_value": 0.0723576,
    "first_data_at": "2015-02-28T00:00:00Z", "last_updated": "2026-09-29T21:35:13Z",
    "quotes": {"USD": {
        "price": 0.016687182941158063, "volume_24h": 299.2754905121976,
        "volume_24h_change_24h": -95.51000213623047,
        "market_cap": 0, "market_cap_change_24h": 0,
        "percent_change_24h": -8.90999984741211, "percent_change_7d": -35.47999954223633,
        "ath_price": 0.5590373, "percent_from_price_ath": -97.02,
    }},
}

XRP_TICKER = {
    "id": "xrp-xrp", "symbol": "XRP", "total_supply": 99987030526,
    "last_updated": "2026-09-29T21:35:00Z",
    "quotes": {"USD": {"price": 1.493710814860317, "volume_24h": 3001648812.502295,
                       "market_cap": 93923355781, "percent_change_24h": 0.5}},
}

LTC_TICKER = {
    "id": "ltc-litecoin", "symbol": "LTC", "total_supply": 75561000,
    "last_updated": "2026-09-29T21:35:00Z",
    "quotes": {"USD": {"price": 67.30437294473235, "volume_24h": 382586716.8549246,
                       "market_cap": 5085416588, "percent_change_24h": -1.2}},
}

# 459382131 * 0.016687182941158063, to the dollar.
GRC_DERIVED_CAP = 7665794

#: Which ids returned a 200 from the operator's host, and which were written from
#: CoinPaprika's naming pattern and never fetched. Two sets rather than one list,
#: because the whole point is that they are different KINDS of claim.
#:
#: UNCONFIRMED IS EMPTY AS OF 2026-09-30, and the sets stay because emptying one
#: is the outcome, not the end of the check. Four ids got their 200 on 2026-09-29;
#: USDC and USDT moved the same day, on the first `chain_balances.py --level` run
#: that fetched them; SOL moved 2026-09-30. Each was shipped unfetched on purpose
#: and each moved the moment there was a number -- a label that only ever gets
#: stricter is a label nobody trusts.
#:
#: What still bites with UNCONFIRMED empty is the coverage assertion at the end of
#: the walk below: a NEW id must be declared in one of these two sets, so it
#: arrives either with its 200 recorded or with the marker. The loop is vacuous
#: today and is one added id away from being the check again.
MEASURED = frozenset({"BTC", "LTC", "GRC", "XRP", "USDC", "USDT", "SOL"})
UNCONFIRMED = frozenset()


def test_GRCs_market_cap_is_DERIVED_because_the_feed_reports_zero():
    """The measurement that makes GRC's thinness visible at all.

    MUTATION: treat a reported 0 as a real cap (drop the `> 0` test in
    derive_market_cap) and this fails -- market_cap_usd comes back 0.0 and
    market_context.turnover_finding() then returns None, which is the SILENT
    failure this whole file is about. Verified 2026-09-29.
    """
    quote = quote_from_ticker("GRC", GRC_TICKER)
    assert quote.market_cap_is_derived is True, (
        "the cap was taken as reported. CoinPaprika reported 0, and a 0 cap makes the turnover "
        "check return None rather than THIN"
    )
    assert quote.market_cap_usd == pytest.approx(GRC_DERIVED_CAP, rel=1e-6)
    assert quote.total_supply == 459382131


def test_a_REPORTED_cap_is_used_as_is_and_is_NOT_labeled_derived():
    """The other branch. XRP's feed computes a cap, so nothing is multiplied.

    The label is the point: a derived cap and a reported one are the same float
    and different claims, and a reader must not have to guess which they hold.
    """
    quote = quote_from_ticker("XRP", XRP_TICKER)
    assert quote.market_cap_is_derived is False
    assert quote.market_cap_usd == 93923355781


def test_GRC_is_measurably_thin_and_the_others_are_not():
    """The number the operator's question was reaching for.

    Turnover is 24h volume over market cap -- how much of the float changes
    hands in a day. market_context.THIN_TURNOVER is 0.001, and GRC is far below
    it while the other two are far above. This is what supply buys you: not the
    price, which is circular, but whether the price can be trusted.
    """
    grc = quote_from_ticker("GRC", GRC_TICKER)
    assert grc.turnover is not None, "a None turnover is the skipped check this file exists to prevent"
    assert grc.turnover < THIN_TURNOVER / 20, (
        f"GRC turnover {grc.turnover} is not far below the {THIN_TURNOVER} line; the seeded rows or "
        f"the arithmetic changed"
    )
    for payload in (XRP_TICKER, LTC_TICKER):
        quote = quote_from_ticker(payload["symbol"], payload)
        assert quote.turnover > THIN_TURNOVER, f"{quote.asset} should be nowhere near thin"


def test_a_missing_cap_AND_a_missing_supply_gives_None_rather_than_zero():
    """None is not 0.0. Zero cap would read as the thinnest possible market."""
    cap, derived = derive_market_cap(0, None, 1.0)
    assert cap is None and derived is False
    cap, derived = derive_market_cap(None, 0, 1.0)
    assert cap is None and derived is False


def test_a_response_with_no_price_RAISES_instead_of_pricing_a_swap_from_nothing():
    """services/pricing.py's rule, restated here: a failed fetch must not return 0.

    `except Exception: return 0` would make "the API is down" indistinguishable
    from "this asset is worthless", and the second pays out zero.
    """
    with pytest.raises(PaprikaError) as raised:
        quote_from_ticker("GRC", {"id": "grc-gridcoin", "quotes": {"USD": {}}})
    assert "no USD price" in str(raised.value)


def test_a_price_of_zero_is_refused_rather_than_divided_by():
    with pytest.raises(PaprikaError):
        quote_from_ticker("GRC", {"quotes": {"USD": {"price": 0}}})


def test_the_pair_rate_runs_asset_over_XRP_which_is_the_direction_that_went_wrong():
    """--rate is XRP per unit of the script chain, so it is asset_USD / XRP_USD.

    Inverting it makes the swap off by the square of the price. That is not
    hypothetical: on 2026-09-29 --rate 66.1 was given for a GRC leg where
    0.01512859 was correct, a factor of 4,367, and it was harmless only because
    the coins were testnet.

    The GRC figure below is checked against that measured run: --rate 0.01512859
    produced 66.10001328 GRC for one XRP.
    """
    xrp = quote_from_ticker("XRP", XRP_TICKER)
    ltc = quote_from_ticker("LTC", LTC_TICKER)
    grc = quote_from_ticker("GRC", GRC_TICKER)

    assert pair_rate(ltc, xrp) == pytest.approx(45.05850281, rel=1e-6)
    assert pair_rate(grc, xrp) == pytest.approx(0.011171, rel=1e-4)
    # One XRP buys 1/rate units, which is the number the driver prints.
    assert 1 / pair_rate(ltc, xrp) == pytest.approx(0.02219337, rel=1e-6)


def test_an_unknown_asset_says_ids_are_NOT_GUESSABLE_and_how_to_look_one_up():
    """The mistake this message prevents was made on the way to writing the file.

    `grc-gridcoinresearch` -- the obvious guess, and CoinGecko's own spelling --
    returns {"error":"id not found"}. The real id is `grc-gridcoin`.
    """
    with pytest.raises(PaprikaError) as raised:
        fetch_quote("DOGE")
    assert "NOT guessable" in str(raised.value) and "/v1/search/" in str(raised.value)


def test_EVERY_UNMEASURED_ID_is_marked_UNCONFIRMED_where_it_is_declared():
    """Rule 17, held as a test rather than as an intention.

    An id written from CoinPaprika's naming pattern and never fetched must carry
    the marker where it is DECLARED -- not somewhere else in the file that a
    reader of the table would not see.

    EVERY ID IS MEASURED AS OF 2026-09-30, so the loop below runs zero times and
    the coverage assertion after it is what currently holds: a new id has to be
    named in MEASURED or in UNCONFIRMED, and naming it in the second turns the
    loop back on. Stated rather than left for a reader to work out from an empty
    frozenset, because a test whose body does not execute looks like a passing
    test and is not one (rule 14, applied to a test's own output).

    THE FIRST VERSION OF THIS TEST SLICED 600 CHARACTERS BEFORE THE ID and looked
    for the word in that window. It passed until the comment above the ids grew
    past 600 characters, at which point it failed on a file that was correct. A
    check with a magic distance in it measures the distance. This walks upward
    from the declaration through its own contiguous comment block instead, which
    is the thing a reader actually reads.
    """
    source = (pathlib.Path(__file__).resolve().parent.parent
              / "swap_terminal" / "services" / "coinpaprika.py").read_text()
    lines = source.splitlines()
    for asset in UNCONFIRMED:
        declared = next((number for number, line in enumerate(lines)
                         if line.strip().startswith(f'"{asset}":')), None)
        assert declared is not None, f"{asset} is not declared in PAPRIKA_IDS at all"
        # THE DECLARATION LINE ITSELF, or the contiguous comment block above it.
        # Either is what a reader scanning the table sees; requiring the block
        # alone failed on the two ids that sit under another id's comment.
        block, cursor = [lines[declared]], declared - 1
        while cursor >= 0 and lines[cursor].strip().startswith("#"):
            block.append(lines[cursor])
            cursor -= 1
        assert any("UNCONFIRMED" in line for line in block), (
            f"{asset}'s id sits with no UNCONFIRMED marker in the comment block above it, beside "
            f"{len(MEASURED)} ids that were confirmed by a 200. A reader of that table cannot tell "
            f"a measured id from a guessed one"
        )
    assert set(PAPRIKA_IDS) == MEASURED | UNCONFIRMED, (
        f"PAPRIKA_IDS covers {sorted(PAPRIKA_IDS)}; this test knows {sorted(MEASURED | UNCONFIRMED)}. "
        f"A new id is either measured -- say so and move it -- or unconfirmed and needs the marker"
    )


def test_an_UNMEASURABLE_turnover_is_None_and_not_zero():
    """0.0 turnover is the thinnest market there is; None is "nobody measured".

    Returning zero for an asset whose cap or volume the feed did not report
    would hand market_context a THIN verdict it has no evidence for -- a
    fabricated measurement, which is worse than a missing one because it looks
    like a finding.

    THIS TEST WAS ADDED AFTER A MUTATION FOUND NOTHING. The distinction was
    written into turnover's docstring and asserted nowhere, so replacing the
    `return None` with `return 0.0` left the suite green. Verified 2026-09-29 by
    running that mutation again; it now fails here.
    """
    no_cap = quote_from_ticker("GRC", {"quotes": {"USD": {"price": 1.0, "volume_24h": 500.0}}})
    assert no_cap.market_cap_usd is None
    assert no_cap.turnover is None, (
        "an asset with no cap reported a turnover. That figure would be compared against "
        "THIN_TURNOVER and would decide a confidence verdict from nothing"
    )

    no_volume = quote_from_ticker("GRC", {"total_supply": 1000,
                                          "quotes": {"USD": {"price": 1.0, "market_cap": 1000}}})
    assert no_volume.market_cap_usd == 1000
    assert no_volume.turnover is None, "an asset with no volume reported must not read as zero turnover"
