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
   routes/rates.py:17, atomic_swap_xrp.py:567, swap_readiness.py:371 and
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
from datetime import datetime
from threading import Lock
from time import time

import requests

# The cache holds the RAW CoinGecko body plus both derived views, so a cache hit
# returns the identical dict object fetch_usd_prices() has always returned and
# does no repeated float() work. `_cache` keeps its name because
# tests/test_open_swap.py's stub_prices() docstring names it as the thing both
# call sites consult in production; renaming it would make that sentence wrong
# (rule 16: a wrong comment is a bug).
# `source` NAMES THE FEED THAT ANSWERED, added 2026-09-30 with the fallback. A
# price whose origin is not recorded is a number nobody can check a day later,
# and two feeds that disagree would be indistinguishable in a quote row (rule
# 14's "echo the parameters that decide the answer").
_cache = {"raw": None, "prices": None, "context": None, "fetched_at": 0.0, "expires_at": 0.0,
          "source": ""}
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
    # ICP added 2026-10-06. The CoinGecko id is "internet-computer" -- the project's
    # name, not the ticker, exactly as every line above it.
    #
    # LOOKED UP, NOT DERIVED, and the lookup is why. From the operator's host:
    #
    #     /api/v3/search?query=internet computer
    #       internet-computer | Internet Computer | ICP | rank 56
    #
    # The naming pattern would have produced the same string this time, which is
    # luck rather than method: the sibling CoinPaprika search returned
    # `ict-internet-computer-technology` (rank 0) one row below the real asset, and
    # services/coinpaprika.py's own header records three ids written from the
    # pattern that were wrong. A near-name that prices SOMETHING is the dangerous
    # case, because it does not 404.
    #
    # CONFIRMED BY PRICING, not merely by existing. This comment first said the id
    # was "still unconfirmed ... came from /search, not from /simple/price", and that
    # was true for about ten minutes. The batch endpoint this table is actually used
    # against then answered from the operator's host:
    #
    #     /api/v3/simple/price?ids=internet-computer,bitcoin,gridcoin-research&vs_currencies=usd
    #       {"internet-computer":{"usd":3.45},"bitcoin":{"usd":86167},
    #        "gridcoin-research":{"usd":0.00926626}}   [http 200]
    #
    # Asked in the SAME batch shape _coingecko_raw() uses, which is the distinction
    # that mattered: this function joins every id here into one request, so an id the
    # endpoint does not know makes the response PARTIAL and the missing-asset refusal
    # turns that into "no quote for ANY pair". A /search hit would not have ruled
    # that out.
    #
    # Two independent feeds also agree on the number -- CoinGecko $3.45 against
    # CoinPaprika $3.444476013739092, 0.16% apart -- which is stronger evidence that
    # both ids name the same asset than either feed alone could give. The decoy
    # `ict-internet-computer-technology` would not have done that.
    "ICP": "internet-computer",
    # SOL added 2026-09-29. The CoinGecko id is "solana" -- the project's name, not the
    # ticker, exactly as every line above it. "sol" would 404 and surface as a missing-price
    # refusal rather than an error naming this table.
    #
    # INERT UNTIL A PAIR IS ENABLED, and added ahead of one on purpose. A pair in
    # config.ALLOWED_PAIRS without a line here makes validate_pair() accept the swap and then
    # create_quote() fail on a missing USD price -- an accepted swap that cannot be priced,
    # which is worse than a refused one. That is the exact failure XRP produced on 2026-09-26
    # ("No quote: 'XRP_NETWORK_FEE_RESERVE'", one table over), so this side is prepared first
    # and the pair stays the operator's decision (rule 16).
    #
    # NOTE WHICH DIRECTION IS EVEN POSSIBLE, AND THIS COMMENT WAS TWO VERSIONS BEHIND THE
    # CODE. It said "chains/solana.py:700 send_to_address() raises NotImplementedError and
    # :278 sets can_spend = False". Neither clause is true any more: a devnet payout path was
    # built 2026-10-02 (send_to_address() previews, and raises SolanaSendNotArmed rather than
    # NotImplementedError), and can_spend has been DERIVED from SOL_PAYOUT_KEYPAIR_PATH since
    # 2026-10-03 rather than hardcoded. A line number in a comment rots on every edit above
    # it, which is why this names the mechanism instead of a location.
    #
    # WHAT IS STILL TRUE is the only thing this note needed: no pair in
    # config.ALLOWED_PAIRS pays out in SOL -- measured 2026-10-03 against the real Config,
    # three pairs take SOL as the INPUT and zero take it as the output -- so there is
    # deliberately no SOL_NETWORK_FEE_RESERVE to match this line:
    # quote_service.get_network_fee_reserve() is keyed on `to_asset`, and SOL is not one
    # today. Enabling such a pair is live posture and the operator's (rule 16), and it would
    # need a reserve here first.
    "SOL": "solana",
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
    try:
        raw, source = _coingecko_raw()
    except Exception as coingecko_error:  # noqa: BLE001 -- checked: a non-200, a transport failure, a shape that is not JSON and a partial response all mean the same thing to this function -- CoinGecko did not price this, try the other feed. The reason is not swallowed: it is carried into the fallback's own failure message below, so a run where BOTH feeds fail reports BOTH reasons.
        try:
            raw, source = _coinpaprika_raw()
        except Exception as paprika_error:
            raise PriceSourceError(
                f"no price feed answered. CoinGecko: {type(coingecko_error).__name__}: "
                f"{coingecko_error}. CoinPaprika: {type(paprika_error).__name__}: {paprika_error}. "
                f"NO RATE IS DERIVED FROM A MISSING PRICE -- a quote is refused rather than "
                f"computed from a stale or zero one"
            ) from paprika_error
    _cache["raw"] = raw
    _cache["source"] = source
    _cache["prices"] = None
    _cache["context"] = None
    _cache["fetched_at"] = now
    _cache["expires_at"] = now + ttl_seconds
    return _cache


class PriceSourceError(RuntimeError):
    """No feed answered. Raised rather than returning a zero or a stale price.

    A distinct type because open_swap.py's fetch_prices_or_refuse() catches
    TypeError and ValueError by name for the unparseable-price case, and "every
    feed is down" is a different thing an operator can act on differently.
    """


def required_assets() -> tuple[str, ...]:
    """The assets a response may NOT be missing: the ones some pair can actually trade.

    NARROWED FROM ALL OF IDS ON 2026-09-30, and the reason is measured rather than
    hypothetical. IDS carries BTC, LTC, GRC, XRP and SOL; Config.ALLOWED_PAIRS names the
    first four. SOL is priced because a pair may want it one day and is traded by nothing
    today -- and until this change a response missing SOL refused EVERY quote, on every
    pair, because _require_every_asset() demanded the whole table.

    That is not a theoretical hazard. chains/solana_rpc_map's own history is a wrong
    CoinPaprika id 404ing, and `grc-gridcoinresearch` is the standing reminder that the
    obvious spelling does. A wrong id for an untraded asset, or an outage on one, would
    have taken the terminal down for the four assets that DO trade.

    DERIVED, IN ONE PLACE. Three call sites read it -- this refusal,
    fetch_usd_prices() and _snapshots_from_raw() -- and a hand-kept list beside any of
    them would be rule 8's shape on the question "may this quote be priced".

    IT FAILS CLOSED ON AN EMPTY ALLOWED_PAIRS: with no pair enabled, every asset in IDS is
    required, which is the behavior this replaced. Requiring NOTHING would let a garbage
    response into the cache and turn a clear refusal here into a KeyError deeper in, which
    is the shape this function exists to prevent.

    CONFIG IS IMPORTED INSIDE THE FUNCTION, not at module scope. config.py reads the
    process environment at import time, and this module's own header warns that a
    read-only report which triggers that import is how family resolution broke elsewhere.
    Deferring it also keeps the answer live: an operator who changes ALLOWED_PAIRS and
    restarts gets the new set without this module caching the old one.
    """
    from config import (  # noqa: PLC0415 -- checked: deferred deliberately. config.py reads os.environ at IMPORT time, and pricing.py is imported by read-only reports; making this module's import trigger that one is the import-time side effect rule 12 names. It is also read per call so a changed ALLOWED_PAIRS is picked up rather than frozen.
        Config,
    )

    traded = {asset for pair in Config.ALLOWED_PAIRS for asset in pair}
    required = tuple(asset for asset in IDS if asset in traded)
    return required or tuple(IDS)


def _require_every_asset(raw: dict, source: str) -> dict:
    """The missing-asset refusal, applied identically to both feeds.

    Derived from required_assets() rather than written out. A missing asset raises a
    KeyError naming WHICH one, here, instead of returning a dict that is quietly short one
    key and failing later inside derive_pair_rate() where the message would be about a
    rate rather than about a price.

    THE MESSAGE KEEPS ITS SHAPE from the CoinGecko-only version, because open_swap.py:655
    quotes it by name in its own docstring. What changed is that it now says what was
    REQUIRED rather than what was asked for -- those differ the moment an asset is priced
    without being traded, and printing the asked-for list beside a refusal about a
    narrower set would be the more confusing half of the pair.
    """
    required = required_assets()
    missing = [asset for asset in required if IDS[asset] not in raw]
    if missing:
        raise KeyError(
            f"{source} returned no price for {', '.join(sorted(missing))} "
            f"(required {', '.join(sorted(required))}; asked for "
            f"{', '.join(sorted(IDS))}). No rate is derived from a "
            f"partial response: a swap priced off a missing leg is a swap priced wrong."
        )
    return raw


def _coingecko_raw() -> tuple[dict, str]:
    """CoinGecko's /simple/price, keyed by its own ids. The original path, unchanged.

    KEPT AS THE FIRST TRY rather than replaced. Measured 2026-09-29 and -30 from
    the operator's host, this returns 403 on every request -- from AWS CloudFront's
    edge, not from CoinGecko, so an API key cannot help because the key rides in a
    header the origin never reads. But the block is on a client IP and not on this
    code: it works from other hosts, it is the shape every field name here was
    written against, and one aggregator is a single point of failure whichever one
    it is.

    THAT 403 NO LONGER HOLDS, MEASURED 2026-10-06 FROM THE SAME HOST. The paragraph
    above is kept rather than rewritten because the drift is the point (rule 1) and
    because the decision it justified -- keeping this as the first try -- turns out
    to have been right for a reason nobody could have argued at the time:

        /api/v3/simple/price?ids=internet-computer,bitcoin,gridcoin-research&vs_currencies=usd
          {"internet-computer":{"usd":3.45},"bitcoin":{"usd":86167},
           "gridcoin-research":{"usd":0.00926626}}   [http 200]

    So the PRIMARY feed is answering again, and whichever feed a given quote was
    priced from is no longer predictable from this file -- it is whichever answered,
    which is what fetch_market_context() already records in `source` for exactly
    this reason. A reader who assumed "in practice it is always CoinPaprika" from
    the paragraph above would be wrong today and may be right again tomorrow; the
    recorded `source` is the only thing that knows.

    What is NOT established: whether the block lifted, or whether it only ever
    covered this endpoint while /api/v3/search stayed open. Both were tried the same
    minute and both answered, so the question is moot for this code and is left
    unanswered rather than guessed (rule 17).
    """
    response = requests.get(
        COINGECKO_URL,
        # The context flags cost no extra round trip: they are parameters on the
        # request this path already made. See the header.
        params={"ids": ",".join(IDS.values()), "vs_currencies": "usd", **CONTEXT_PARAMS},
        timeout=15,
    )
    response.raise_for_status()
    return _require_every_asset(response.json(), "CoinGecko"), "CoinGecko"


def _coinpaprika_raw() -> tuple[dict, str]:
    """CoinPaprika, reshaped into CoinGecko's own response shape.

    THE RESHAPE IS THE WHOLE DESIGN. fetch_usd_prices() and
    fetch_market_context() both index raw[cg_id] and read the four
    _CONTEXT_FIELDS keys, and neither needed a line changed to gain a second
    feed -- because what changes is where the dict came from, not what it looks
    like. A second parser would be rule 8's shape: two readers of one concept,
    agreeing on the day they are written.

    ONE REQUEST PER ASSET, unlike CoinGecko's one for all of them. CoinPaprika's
    /v1/tickers takes a single id, so this is len(IDS) round trips -- which the
    cache makes once per TTL window rather than once per quote.

    GRC'S MARKET CAP IS DERIVED and the snapshot has no way to say so, which is
    the one place this reshape loses information. CoinPaprika reports
    market_cap 0 for GRC while reporting total_supply, and
    services/market_context.turnover_finding() returns None on a zero cap --
    so the thinness check on the one asset that needs it would silently not
    run. chains/coinpaprika.derive_market_cap() computes supply x price and
    flags it; MarketSnapshot carries no such flag, so the value goes in
    unflagged here and the flag survives only where PaprikaQuote is used
    directly (chain_balances.py --level). Naming that gap rather than leaving
    it: a cap that is derived and a cap that was reported are the same float
    and different claims.
    """
    from services.coinpaprika import (  # noqa: PLC0415 -- checked: imported inside the function so that a host without `requests` can still import services.pricing for its pure helpers, which is the same reason the drivers defer their price imports.
        PAPRIKA_IDS,
        fetch_quote,
    )

    raw, unpriced = {}, {}
    for asset, cg_id in IDS.items():
        try:
            quote = fetch_quote(asset)
        except Exception as error:  # noqa: BLE001 -- checked: one asset failing must not lose the others; every failure is collected and _require_every_asset() below raises naming exactly which assets are missing, which is more useful than the first exception.
            unpriced[asset] = f"{type(error).__name__}: {error}"
            continue
        raw[cg_id] = {
            "usd": quote.price_usd,
            "usd_market_cap": quote.market_cap_usd,
            "usd_24h_vol": quote.volume_24h_usd,
            "usd_24h_change": quote.change_24h_pct,
            "last_updated_at": _unix_from_iso(quote.source_updated_at),
        }
    if unpriced:
        raise KeyError(
            f"CoinPaprika could not price {', '.join(sorted(unpriced))}: "
            + "; ".join(f"{asset}: {why}" for asset, why in sorted(unpriced.items()))
            + f". Ids are <symbol>-<slug> and are NOT guessable -- see PAPRIKA_IDS, where "
              f"{', '.join(sorted(PAPRIKA_IDS))} are mapped and each carries the 200 that "
              f"confirmed it. A 404 here is a wrong id, not a missing asset."
        )
    return _require_every_asset(raw, "CoinPaprika"), "CoinPaprika"


def _unix_from_iso(stamp) -> int | None:
    """CoinPaprika's ISO-8601 `last_updated` as the unix second CoinGecko sends.

    None rather than a guess when it cannot be parsed: MarketSnapshot carries
    source_updated_at as optional precisely so "the feed did not say when" is
    expressible, and services/market_context reads its absence as a staleness it
    cannot measure rather than as a fresh price.
    """
    if not stamp:
        return None
    try:
        # No Z-to-+00:00 rewrite: Python 3.11+ parses the trailing Z directly, and
        # ruff's FURB162 flags the replace as dead. Verified against CoinPaprika's
        # own "2026-09-29T21:35:13Z" on 3.11.
        return int(datetime.fromisoformat(str(stamp)).timestamp())
    except (TypeError, ValueError):
        return None


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
            # ONLY WHAT THE RESPONSE ACTUALLY CARRIES. This built a key for every asset in
            # IDS, so an absent untraded asset raised KeyError here even once
            # _require_every_asset() stopped demanding it -- the narrowing would have moved
            # the failure four lines instead of removing it. Every REQUIRED asset is present
            # by the time this runs, so a short dict can only be short an untraded one, and
            # derive_pair_rate() raises by name if anything ever reads a key that is gone.
            data = {f"{asset}_USD": float(raw[cg_id]["usd"])
                    for asset, cg_id in IDS.items() if cg_id in raw}
            data["fetched_at"] = cache["fetched_at"]
            cache["prices"] = data
        return cache["prices"]


def last_price_source() -> str:
    """Which feed the cached prices came from, or "" if nothing is cached.

    READ, NEVER FETCHED. A caller asking which feed answered must not cause a
    fetch: that would make a provenance question into an outbound request, and
    the answer would be about a different call than the one it is describing.
    """
    with _lock:
        return _cache["source"] or ""


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
            cache["context"] = _snapshots_from_raw(cache["raw"], cache["fetched_at"])
        return cache["context"]


def _snapshots_from_raw(raw: dict, fetched_at: float) -> list[MarketSnapshot]:
    """One MarketSnapshot per IDS entry, from a raw body already in hand.

    PURE, AND THAT IS WHY IT IS SEPARATE. It was inline in
    fetch_market_context() until 2026-09-30, when cached_market_context() needed
    the identical objects WITHOUT being allowed to make a request -- and two
    loops building one dataclass from one dict shape is rule 8's failure with a
    delay on it: the day one of them gained a field the other would still be
    reading four.
    """
    snapshots = []
    for asset, cg_id in IDS.items():
        if cg_id not in raw:
            # Same reason as fetch_usd_prices(): an untraded asset may be absent, and a
            # snapshot list that raised on one would make the depth badge able to refuse a
            # quote -- which services/quote_service._confidence_for_display() exists to
            # prevent, and which it already got wrong once.
            continue
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
                fetched_at=fetched_at,
            )
        )
    return snapshots


def cached_market_context() -> tuple[MarketSnapshot, ...]:
    """What is already in the cache, and NEVER a request. Empty when nothing is.

    THE ADMIN PAGE IS THE CALLER AND THE NO-FETCH PART IS THE POINT. /admin
    renders from the database and the configuration and contacts nothing --
    routes/admin.py's header says why at length: six chains at a 30s timeout is
    a three-minute page, which is rule 14's blinking cursor, and the resolution
    an operator reaches for is Ctrl-C. A pricing panel that fetched would put
    the page's load time behind a third-party API, so an operator would lose
    the whole picture -- swaps in flight, stuck payouts, worker state -- on the
    day a price feed went down. The panel therefore reports what the QUOTE path
    last fetched, which is also the more useful reading: it is the number a
    customer was actually quoted from.

    Returns () rather than raising or fetching when the cache is cold, and the
    caller must render that as "(not fetched)" rather than as an empty table --
    zero assets priced and nothing having asked yet are different facts (rule 14).

    Building the snapshots and storing them back is not a fetch: the raw body is
    already here, and this is the same lazy fill fetch_market_context() does.
    """
    with _lock:
        if _cache["raw"] is None:
            return ()
        if _cache["context"] is None:
            _cache["context"] = _snapshots_from_raw(_cache["raw"], _cache["fetched_at"])
        return tuple(_cache["context"])


def cache_state() -> dict:
    """Which feed answered, when, and when it goes stale. Reads, never fetches.

    One reader for the whole cache header rather than three accessors, because
    a source without the time it was fetched is a provenance claim with no date
    on it -- and the admin panel needs both in the same breath.

    `expires_at` is carried so the page can say whether the next quote will
    re-fetch. The TTL is not a field on the cache: it is the argument the last
    caller passed, so expires_at - fetched_at is the only place the TTL that
    actually applied survives.
    """
    with _lock:
        return {
            "source": _cache["source"] or "",
            "fetched_at": _cache["fetched_at"] or None,
            "expires_at": _cache["expires_at"] or None,
            "cached": _cache["raw"] is not None,
        }


def derive_pair_rate(from_asset: str, to_asset: str, prices: dict) -> float:
    from_usd = prices[f"{from_asset}_USD"]
    to_usd = prices[f"{to_asset}_USD"]
    return from_usd / to_usd
