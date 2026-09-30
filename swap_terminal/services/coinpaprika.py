#!/usr/bin/env python3
"""Prices from CoinPaprika, because CoinGecko is blocked at its edge.

Role: submodule (price source)
Reads: https://api.coinpaprika.com/v1/tickers/<id>
Writes: nothing. No cache of its own; the caller holds one if it wants one.
Can move funds: no -- but the number this returns is what a swap is priced
        from, so a wrong price here becomes a wrong amount sent. It is the input
        to a fund-moving decision, not the decision.
Mainnet-safe: yes

WHY A SECOND SOURCE, MEASURED RATHER THAN ASSUMED

services/pricing.py fetches from CoinGecko and on 2026-09-29 that returned 403
from the operator's host for every request. Two hypotheses were tested and both
are dead:

    plain:403           a bare request
    with-UA:403         with a browser User-Agent

The body says what it is, and it is not CoinGecko:

    <TITLE>ERROR: The request could not be satisfied</TITLE>
    <H1>403 ERROR</H1>  Request blocked.

That is AWS CloudFront's WAF refusing at the edge, before the API sees the
request. So an API key would not help -- the key travels in a header the origin
never reads -- and neither would a different User-Agent. The block is on the
client's IP or ASN, and nothing in this repository can change it.

MEASURED THE SAME MINUTE, from the same host, on this API:

    xrp-xrp      200   $1.493710814860317   vol24h $3,001,648,812
    btc-bitcoin  200   $83,395.85957216246  vol24h $21,997,990,578
    ltc-litecoin 200   $67.30437294473235   vol24h $382,586,716
    grc-gridcoin 200   $0.016687182941158063  vol24h $299.2754905121976

WHY market_cap HAS TO BE DERIVED, AND WHY THAT IS THE WHOLE GRIDCOIN STORY

CoinPaprika reports `market_cap: 0` for GRC while reporting
`total_supply: 459382131`. Zero is not a market cap; it is a field the feed does
not compute for an asset this small. And market cap is not decoration here --
services/market_context.turnover_finding() returns None when the cap is zero or
absent, so a GRC snapshot taken straight from this feed SKIPS THE THINNESS CHECK
SILENTLY. The one asset that needs it is the one that would not get it.

Supply times price is the only route to that number, and it is the answer to the
operator's question of 2026-09-29 -- "if we can know the entire gridcoin money
supply and a reference price, are we able to calculate anything useful": for
price, no, because cap is supply times price and deriving the price back out of
it is circular. For CONFIDENCE in a price, yes, and it is the difference between
GRC's thinness being measured and being skipped:

    supply          459,382,131        (max_supply 0 -- Gridcoin has NO cap;
                                        coins are minted by research rewards and
                                        staking, so this number moves)
    price           $0.01668718
    derived cap     $7,665,794
    24h volume      $299.28
    turnover        0.0039% of cap per day

Against the other three, measured the same minute: LTC 7.52% per day, XRP 3.20%,
BTC 1.31%. GRC turns over roughly 1,900 times less than Litecoin relative to its
size, and market_context.THIN_TURNOVER (0.001) calls anything below 0.1% THIN --
GRC is 26 times below that line.

The practical consequence, which is what an operator needs rather than a ratio:
a $10 swap is 3.3% of a day's GRC volume and a $100 swap is a third of it. At
that size the swap is not priced BY the market, it IS the market.

A DERIVED CAP IS LABELED AS DERIVED. The snapshot records which it got, because
"CoinPaprika said $7.6M" and "we multiplied two of its numbers" are different
claims and a reader must not have to guess which they are holding (rule 17).
"""

from __future__ import annotations

from dataclasses import dataclass

import requests

TICKER_URL = "https://api.coinpaprika.com/v1/tickers/{id}"

#: CoinPaprika's own ids, which are `<symbol>-<slug>` and NOT guessable. Measured
#: 2026-09-29: `grc-gridcoinresearch` -- the obvious guess, and the spelling
#: CoinGecko uses -- returns {"error":"id not found"}. The real one came from
#: /v1/search/?q=gridcoin&c=currencies and is `grc-gridcoin`. Every id below
#: except SOL's was confirmed by a 200 from the operator's host that minute.
PAPRIKA_IDS = {
    "BTC": "btc-bitcoin",
    "LTC": "ltc-litecoin",
    "GRC": "grc-gridcoin",
    "XRP": "xrp-xrp",
    # SOL IS STILL UNCONFIRMED and the two stablecoins are not any more. All three
    # were written from CoinPaprika's naming pattern; the first `--level` run on
    # 2026-09-29 fetched USDC and USDT and got
    #
    #     USDC: $1.0003730499669257
    #     USDT: $0.9996531383088825
    #     USDC/USDT = 1.000720161454463172734562835
    #
    # so those two ids are measured now and the marker moved off them. SOL's has
    # still never been fetched -- nothing has asked for it -- and stays marked.
    # `grc-gridcoinresearch` is the standing reminder that the obvious spelling
    # 404s, so a caller that needs SOL should expect that and look it up with
    # /v1/search/?q=<name>&c=currencies (rule 17).
    #
    # The two stablecoins are here for services/wallet_leveling.peg_findings(),
    # which checks the dollar this terminal quotes in against the two coins that
    # actually define it. That check DEGRADES rather than fails when an id is
    # wrong: an unpriced stablecoin produces a finding saying the peg is
    # unchecked, never silence -- which is what made it safe to ship these two
    # before they had been fetched.
    "SOL": "sol-solana",  # UNCONFIRMED
    "USDC": "usdc-usd-coin",  # measured 2026-09-29: $1.0003730499669257
    "USDT": "usdt-tether",  # measured 2026-09-29: $0.9996531383088825
}

# The request timeout. Seconds, because that is what requests takes -- rule 6's
# boundary: convert on the way out to a report, never on the way in to an API.
TIMEOUT_SECONDS = 15


class PaprikaError(Exception):
    """A price could not be fetched or was not usable.

    RAISES RATHER THAN RETURNING ZERO, the same rule services/pricing.py states:
    `except Exception: return 0` would make "the price API is down"
    indistinguishable from "this asset is worthless", and the second produces a
    payout of zero or a division by zero rather than an error.
    """


@dataclass(frozen=True)
class PaprikaQuote:
    """One asset's price with the float and flow behind it.

    Frozen because it is a MEASUREMENT (the reason services/pricing.MarketSnapshot
    is). `market_cap_is_derived` is carried beside the cap rather than inferred
    from it, because a derived cap and a reported one are the same float and
    different claims.
    """

    asset: str
    paprika_id: str
    price_usd: float
    total_supply: float | None
    market_cap_usd: float | None
    market_cap_is_derived: bool
    volume_24h_usd: float | None
    change_24h_pct: float | None
    source_updated_at: str | None

    @property
    def turnover(self) -> float | None:
        """24h volume as a fraction of market cap, or None if either is unusable.

        None rather than 0.0: an absent cap and a market that did not trade are
        different facts, and returning zero for the first would report the
        thinnest possible market for an asset nobody measured.
        """
        if not self.market_cap_usd or not self.volume_24h_usd:
            return None
        return self.volume_24h_usd / self.market_cap_usd


def derive_market_cap(reported, total_supply, price_usd: float) -> tuple[float | None, bool]:
    """The market cap to use, and whether it had to be computed.

    ZERO IS TREATED AS ABSENT, and that is the decision this function exists for.
    CoinPaprika returns `market_cap: 0` for GRC -- not null, not missing, zero --
    and a zero cap propagates into market_context.turnover_finding(), which
    returns None for a cap of zero and therefore SKIPS the thinness check
    entirely. The asset that most needs measuring is the one whose feed does not
    compute the number the measurement needs.

    Supply times price is the fallback, and it is exact rather than an estimate:
    market cap is DEFINED as that product. What it inherits is the supply
    figure's own staleness, which is why the derivation is labeled.
    """
    if reported and float(reported) > 0:
        return float(reported), False
    if total_supply and float(total_supply) > 0 and price_usd > 0:
        return float(total_supply) * price_usd, True
    return None, False


def quote_from_ticker(asset: str, payload: dict) -> PaprikaQuote:
    """A PaprikaQuote from one /v1/tickers response. No network.

    EXTRACTED so it can be called with the exact JSON the operator pasted rather
    than with a shape this file imagined (rule 10, and the behavioral-
    verification principle: seed the real rows, assert on the real output).
    """
    quotes = (payload.get("quotes") or {}).get("USD") or {}
    raw_price = quotes.get("price")
    if raw_price is None:
        raise PaprikaError(
            f"{asset}: CoinPaprika returned no USD price in {sorted(payload)}. No rate is derived "
            f"from a partial response: a swap priced off a missing leg is a swap priced wrong."
        )
    price = float(raw_price)
    if price <= 0:
        raise PaprikaError(f"{asset}: CoinPaprika reported a price of {price}, which cannot be quoted from")
    supply = payload.get("total_supply")
    cap, derived = derive_market_cap(quotes.get("market_cap"), supply, price)
    return PaprikaQuote(
        asset=asset,
        paprika_id=payload.get("id", ""),
        price_usd=price,
        total_supply=None if supply is None else float(supply),
        market_cap_usd=cap,
        market_cap_is_derived=derived,
        volume_24h_usd=_optional_float(quotes.get("volume_24h")),
        change_24h_pct=_optional_float(quotes.get("percent_change_24h")),
        source_updated_at=payload.get("last_updated"),
    )


def _optional_float(value) -> float | None:
    """A float, or None when the feed did not say. None is not 0.0.

    Same distinction services/pricing._optional_float() draws and for the same
    reason: 0.0 for change_24h_pct means the price did not move, which is a fact
    about a quiet market, and None means nobody reported.
    """
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def fetch_quote(asset: str, *, timeout: float = TIMEOUT_SECONDS) -> PaprikaQuote:
    """One asset, one request. Raises PaprikaError with the asset named."""
    paprika_id = PAPRIKA_IDS.get(asset)
    if paprika_id is None:
        raise PaprikaError(
            f"{asset} has no CoinPaprika id in PAPRIKA_IDS (which covers {', '.join(sorted(PAPRIKA_IDS))}). "
            f"Ids are <symbol>-<slug> and are NOT guessable -- grc-gridcoinresearch 404s where "
            f"grc-gridcoin works -- so look it up with /v1/search/?q=<name>&c=currencies rather than "
            f"inventing one."
        )
    try:
        response = requests.get(TICKER_URL.format(id=paprika_id), timeout=timeout)
        response.raise_for_status()
        payload = response.json()
    except requests.RequestException as error:
        raise PaprikaError(f"{asset} ({paprika_id}): {type(error).__name__}: {error}") from error
    except ValueError as error:
        raise PaprikaError(f"{asset} ({paprika_id}): response was not JSON: {error}") from error
    return quote_from_ticker(asset, payload)


def pair_rate(base: PaprikaQuote, quote: PaprikaQuote) -> float:
    """How many `quote` units one `base` unit costs. base_USD / quote_USD.

    THE DIRECTION IS THE THING THAT GOES WRONG. atomic_swap_xrp.py's --rate is
    "XRP per unit of the script chain", so pricing an LTC leg is
    pair_rate(LTC_quote, XRP_quote) = LTC_USD / XRP_USD. Inverting it makes the
    swap off by the square of the price, and on testnet that looks like a large
    number and nothing else -- which is exactly what happened on 2026-09-29, when
    --rate 66.1 was given for GRC where 0.01512859 was correct, a factor of 4,367
    in the wrong direction.
    """
    if quote.price_usd <= 0:
        raise PaprikaError(f"cannot divide by {quote.asset} price {quote.price_usd}")
    return base.price_usd / quote.price_usd
