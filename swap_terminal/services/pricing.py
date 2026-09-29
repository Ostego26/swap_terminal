"""CoinGecko price and market-context fetch, one HTTP call, with a process-wide cache.

Role: submodule (price source)
Reads: https://api.coingecko.com/api/v3/simple/price
Writes: an in-process cache only
Can move funds: no -- but the number fetch_usd_prices() returns is multiplied by
       the deposit amount to decide the payout amount, so a wrong price here
       becomes a wrong amount sent. It is the input to a fund-moving decision,
       not the decision.
Mainnet-safe: yes

No broad except: a failed fetch RAISES rather than returning a stale or zero
price. That is deliberate and is rule 12's whole point on this path -- a
`except Exception: return 0` here would make "the price API is down"
indistinguishable from "this asset is worthless", and the second one produces
a payout of zero or a division by zero rather than an error.

MARKET CONTEXT WAS ADDED 2026-09-27, AT THE OPERATOR'S INSTRUCTION: "we should
also keep track of the market cap and price comparison to better establish grc
prices."

The problem being answered. Before this change every swap in the tree was
priced from one number per asset -- the spot USD price -- with no volume and no
market cap beside it. GRC is thin. A spot price with no flow behind it is a
price somebody with modest size can set, and a desk quoting off it with a
600-second quote TTL (QUOTE_TTL_SECONDS) plus a 30-second price cache
(RATE_CACHE_SECONDS) is quoting off a number that is up to 630 seconds old
against a market that may not have traded in that window at all.

TWO THINGS ABOUT HOW THIS IS SHAPED, AND BOTH ARE THE POINT.

1. ONE HTTP CALL, TWO VIEWS. CoinGecko's /simple/price takes
   include_market_cap, include_24hr_vol, include_24hr_change and
   include_last_updated_at as query parameters on the SAME request, so the
   context costs no extra round trip and no extra rate-limit budget. The four
   flags and the four response keys they make appear are ONE table
   (_CONTEXT_FIELDS below) and the query parameters are derived from it, so
   adding a fifth field means editing one tuple -- rule 11's shape, and the
   same reason IDS is one table rather than paired with a hand-written dict.

   _fetch_raw() is what holds the cache and makes the request. Both public
   views read that one cache, so fetch_usd_prices() and
   fetch_market_context() called in the same TTL window make one request
   between them, not two.

   *** UNVERIFIED AGAINST A LIVE RESPONSE, AND THAT IS STATED RATHER THAN
   IMPLIED (rule 17). *** The four response key names below --
   `usd_market_cap`, `usd_24h_vol`, `usd_24h_change`, `last_updated_at` -- are
   from CoinGecko's documented shape for these flags, NOT from a response this
   code has read. Outbound HTTPS from the container this was written in is
   proxied and api.coingecko.com is denied by policy: measured 2026-09-27,
   `curl https://api.coingecko.com/api/v3/simple/price?...` returns
   "CONNECT tunnel failed, response 403" and the proxy's own status endpoint
   records it as `connect_rejected`, "gateway answered 403 to CONNECT (policy
   denial or upstream failure)", host api.coingecko.com:443. Nothing in this
   tree had a recorded enriched response to check against either -- grepped for
   `usd_market_cap`, `usd_24h_vol`, `usd_24h_change` and `last_updated_at`
   across every file: zero hits before this change.

   So the parser is written to TOLERATE a key that is absent or null rather
   than to assume one is present, which is the behavior a thin asset needs
   anyway (see point 2). If a key name is wrong, the measurable consequence is
   a market_cap_usd/volume_24h_usd/change_24h_pct of None and a price_confidence()
   verdict of UNKNOWN naming the missing field -- not a crash, and not a
   silently wrong number. fetch_usd_prices() is unaffected either way, because
   it reads only `usd`, which this code has always read.

2. THE EXISTING RETURN SHAPE IS UNCHANGED, AND fetch_market_context() IS A
   SIBLING RATHER THAN A WIDENING. Every caller of fetch_usd_prices() was read
   by NAME across the tree before choosing (rule 2), not just through the
   import graph -- open_swap.py:677, services/quote_service.py:67,
   routes/rates.py:17, atomic_swap_xrp_grc.py:567, swap_readiness.py:371 and
   tests/test_open_swap.py. Two of them make widening the returned dict wrong
   rather than merely additive:

     open_swap.py:686 prints `got {len(prices) - 1} prices`, with the comment
         "the count excludes the fetched_at stamp". That subtraction is
         hard-coded to the one non-price key that dict has ever carried.
         MEASURED 2026-09-27 by calling it with a stubbed transport: IDS has 5
         assets, the dict has 6 keys and the line prints 5. Adding the four
         context fields per asset would make that same line print 25 and call
         them prices -- a number that means something other than what it says,
         which is rule 14's defect exactly. (The first draft of this comment
         said 6 assets / 7 keys / 30 from reading the table rather than running
         it, which is rule 17's failure in miniature: the count was off by one
         asset because it was inferred instead of measured.)

     routes/rates.py:21 returns `jsonify({"prices": prices, ...})`, so the dict
         IS the public /api/rates response body. Widening it changes an HTTP
         contract as a side effect of adding a diagnostic.

   Both of those files are outside what this change was authorized to touch, so
   a widening could not even have been repaired in the same pass. A sibling
   function costs one extra name and breaks nothing.
"""

from dataclasses import dataclass, fields
from threading import Lock
from time import time

import requests

# The cache holds the RAW CoinGecko body plus both derived views, so a cache hit
# returns the identical dict object fetch_usd_prices() has always returned and
# does no repeated float() work. `_cache` keeps its name because
# tests/test_open_swap.py's stub_prices() docstring names it as the thing both
# call sites consult in production; renaming it would make that sentence wrong
# (rule 16: a wrong comment is a bug).
_cache = {"raw": None, "prices": None, "context": None, "fetched_at": 0.0, "expires_at": 0.0}
_lock = Lock()

COINGECKO_URL = "https://api.coingecko.com/api/v3/simple/price"
# One table, and everything below is DERIVED from it. It used to be paired with a
# hand-written dict literal spelling BTC_USD, LTC_USD and GRC_USD a second time,
# which is rule 8's shape: adding an asset meant editing two places, and editing
# only one produced a KeyError at quote time rather than at import. XRP was added
# 2026-09-26 and is the asset that would have hit it.
IDS = {
    "BTC": "bitcoin",
    "LTC": "litecoin",
    "GRC": "gridcoin-research",
    "XRP": "ripple",
    # THIS TABLE IS WHY ALLOWED_PAIRS ALONE IS NOT ENOUGH TO ADD A PAIR. Adding one to
    # config.ALLOWED_PAIRS without a line here makes validate_pair() accept it and then
    # create_quote() fail on a missing USD price -- an accepted swap that cannot be
    # priced, which is worse than a refused one. Note also that every id above is the
    # project's NAME and not its ticker, so a ticker guessed into this table 404s and
    # produces a missing-price refusal rather than an error naming this table.
}

# THE CONTEXT TABLE. (query flag, response key, MarketSnapshot field), and the
# request parameters, the parser and the snapshot are all derived from it.
#
# Written as one tuple rather than three lists for rule 8's reason: a flag sent
# without its key being read is a wasted parameter, and a key read without its
# flag being sent is a field that is always None. Neither failure announces
# itself -- the response simply does not contain what the reader expected --
# and pairing them here makes both impossible to write separately.
_CONTEXT_FIELDS = (
    ("include_market_cap", "usd_market_cap", "market_cap_usd"),
    ("include_24hr_vol", "usd_24h_vol", "volume_24h_usd"),
    ("include_24hr_change", "usd_24h_change", "change_24h_pct"),
    ("include_last_updated_at", "last_updated_at", "source_updated_at"),
)


@dataclass(frozen=True)
class MarketSnapshot:
    """One asset's spot price with the flow and float behind it, at one instant.

    Frozen because it is a MEASUREMENT. A snapshot whose fields can be reassigned
    after the fetch is a row that can disagree with what CoinGecko returned, and
    the whole reason this type exists is to be compared against the same fields
    fetched an hour later.

    THE THREE OPTIONAL FIELDS ARE OPTIONAL ON PURPOSE, AND None IS NOT 0.0.
    CoinGecko returns partial data for thin assets, which is precisely the class
    GRC is in, so "this asset has no market cap datum" is an ordinary response
    and not an error. It is carried as None, never coerced to zero, because zero
    is a legitimate value that would then be indistinguishable from absence --
    the same confusion rule 12 names when it says a broad catch is illegitimate
    when the caller cannot tell the failure from a real answer. change_24h_pct
    makes it sharpest: 0.0 means the price did not move, which is a fact about a
    quiet market, and None means nobody said. services/market_context.py's
    price_confidence() treats them differently and says which it saw.

    price_usd is NOT optional. A snapshot with no price is not a snapshot, and
    _fetch_raw() has already refused the whole response by then.

    source_updated_at is CoinGecko's own unix timestamp for the quote, kept
    separate from fetched_at, which is when WE asked. The gap between them is
    how stale the feed itself was, and it is not derivable from either one
    alone. In SECONDS, both of them, because they are unix timestamps and
    timestamps are an interface rather than a report (rule 6): they get
    converted to microfortnights at the print, in format_market_context_block().
    """

    asset: str
    coingecko_id: str
    price_usd: float
    market_cap_usd: float | None
    volume_24h_usd: float | None
    change_24h_pct: float | None
    source_updated_at: int | None
    fetched_at: float


# Derived, so the request cannot ask for a field the parser does not read.
CONTEXT_PARAMS = {flag: "true" for flag, _key, _field in _CONTEXT_FIELDS}

# Derived, so a test can assert the SQL columns and this type have not drifted
# apart without importing sqlite into a module that only talks to a price API.
SNAPSHOT_FIELDS = tuple(field.name for field in fields(MarketSnapshot))


def _optional_float(raw_entry: dict, key: str) -> float | None:
    """A context value as a float, or None when it is absent, null or unparseable.

    THREE WAYS A THIN ASSET'S FIELD COMES BACK UNUSABLE, and all three become
    None here rather than an exception, because none of them is an error on this
    path: the caller asked for context and the context is not available.

      absent        the key is not in the entry at all. This is what a flag the
                    API did not honor looks like, and it is also what a key name
                    being WRONG looks like -- see the unverified-shape note in
                    this module's header. Either way the reader is told None and
                    price_confidence() reports UNKNOWN naming the field.
      null          the key is present with JSON null, which float() turns into
                    a TypeError. CoinGecko does this for an asset it tracks but
                    has no cap or volume figure for.
      not a number  a string that is not numeric, from any shape change. ValueError.

    THIS IS NOT THE BROAD CATCH RULE 12 FORBIDS, and the distinction is the one
    that rule draws: the caller CAN tell the failure from a real answer, because
    a real answer is a float and this failure is None. Contrast the thing that
    would be forbidden -- returning 0.0 -- which would read as a market cap of
    zero and a volume of zero, and would make price_confidence() report a
    confident THIN verdict about a number nobody measured.

    The two exception types are named rather than caught as Exception, so a
    genuinely surprising failure still propagates.
    """
    if key not in raw_entry:
        return None
    value = raw_entry[key]
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _fetch_raw(ttl_seconds: int) -> dict:
    """The one HTTP request and the one cache. Returns the cache dict itself.

    Holds _lock for the duration, which is what makes the TTL check and the
    store atomic: two threads entering together make one request, not two.

    THE MISSING-ASSET REFUSAL LIVES HERE, not in fetch_usd_prices(), so that
    BOTH views refuse the same partial response. It used to be in
    fetch_usd_prices() and the message is unchanged, because it is the message
    open_swap.py:655 quotes by name in its own docstring.
    """
    now = time()
    if _cache["raw"] is not None and now < _cache["expires_at"]:
        return _cache
    response = requests.get(
        COINGECKO_URL,
        # The context flags cost no extra round trip: they are parameters on the
        # request this path already made. See the header.
        params={"ids": ",".join(IDS.values()), "vs_currencies": "usd", **CONTEXT_PARAMS},
        timeout=15,
    )
    response.raise_for_status()
    raw = response.json()
    # Derived from IDS rather than written out. A missing asset raises a
    # KeyError naming WHICH one, here, instead of returning a dict that is
    # quietly short one key and failing later inside derive_pair_rate()
    # where the message would be about a rate rather than about a price.
    missing = [asset for asset, cg_id in IDS.items() if cg_id not in raw]
    if missing:
        raise KeyError(
            f"CoinGecko returned no price for {', '.join(sorted(missing))} "
            f"(asked for {', '.join(sorted(IDS))}). No rate is derived from a "
            f"partial response: a swap priced off a missing leg is a swap priced wrong."
        )
    _cache["raw"] = raw
    _cache["prices"] = None
    _cache["context"] = None
    _cache["fetched_at"] = now
    _cache["expires_at"] = now + ttl_seconds
    return _cache


def fetch_usd_prices(ttl_seconds: int = 30) -> dict:
    """USD spot price per asset, plus `fetched_at`. THE SHAPE IS UNCHANGED.

    Returns {"BTC_USD": float, ..., "fetched_at": float} and nothing else. The
    header explains at length why the market context is a separate function
    instead of four more keys in here: open_swap.py:686 prints len(prices) - 1
    as a count of prices, and routes/rates.py:21 returns this dict as the
    /api/rates response body.

    float() on the price is deliberately NOT defended the way the context
    fields are. A price that is null or unparseable must raise -- open_swap.py's
    fetch_prices_or_refuse() catches TypeError and ValueError by name for
    exactly this and reports that nothing was written. Defaulting it would pay
    out zero.
    """
    with _lock:
        cache = _fetch_raw(ttl_seconds)
        if cache["prices"] is None:
            raw = cache["raw"]
            data = {f"{asset}_USD": float(raw[cg_id]["usd"]) for asset, cg_id in IDS.items()}
            data["fetched_at"] = cache["fetched_at"]
            cache["prices"] = data
        return cache["prices"]


def fetch_market_context(ttl_seconds: int = 30) -> list[MarketSnapshot]:
    """Every asset's price with its market cap, 24h volume and 24h change beside it.

    One snapshot per entry in IDS, in IDS order, sharing the single HTTP request
    and cache with fetch_usd_prices(). Called inside the same TTL window as
    fetch_usd_prices(), whichever runs second makes no request at all.

    Reads no field that is not in _CONTEXT_FIELDS, and tolerates every one of
    them being absent -- see _optional_float(). What it does NOT tolerate is a
    missing PRICE, which _fetch_raw() has already refused.

    This function makes no judgment. Whether a snapshot is trustworthy enough to
    quote from is services/market_context.py::price_confidence(), which is a
    pure function over these fields so it can be called with seeded inputs
    (rule 10: the decision is the smallest piece at the bottom).
    """
    with _lock:
        cache = _fetch_raw(ttl_seconds)
        if cache["context"] is None:
            raw = cache["raw"]
            snapshots = []
            for asset, cg_id in IDS.items():
                entry = raw[cg_id]
                values = {field: _optional_float(entry, key) for _flag, key, field in _CONTEXT_FIELDS}
                # The feed's own timestamp is a unix SECOND, not a fraction of one.
                # int() rather than float() so a row read back out of SQL compares
                # equal to what the API said, and None stays None.
                updated = values["source_updated_at"]
                snapshots.append(
                    MarketSnapshot(
                        asset=asset,
                        coingecko_id=cg_id,
                        price_usd=float(entry["usd"]),
                        market_cap_usd=values["market_cap_usd"],
                        volume_24h_usd=values["volume_24h_usd"],
                        change_24h_pct=values["change_24h_pct"],
                        source_updated_at=None if updated is None else int(updated),
                        fetched_at=cache["fetched_at"],
                    )
                )
            cache["context"] = snapshots
        return cache["context"]


def derive_pair_rate(from_asset: str, to_asset: str, prices: dict) -> float:
    from_usd = prices[f"{from_asset}_USD"]
    to_usd = prices[f"{to_asset}_USD"]
    return from_usd / to_usd
