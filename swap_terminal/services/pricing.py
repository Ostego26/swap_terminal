"""CoinGecko price fetch with a process-wide cache.

Role: submodule (price source)
Reads: https://api.coingecko.com/api/v3/simple/price
Writes: an in-process cache only
Can move funds: no -- but the number it returns is multiplied by the deposit
       amount to decide the payout amount, so a wrong price here becomes a
       wrong amount sent. It is the input to a fund-moving decision, not the
       decision.
Mainnet-safe: yes

No broad except: a failed fetch RAISES rather than returning a stale or zero
price. That is deliberate and is rule 12's whole point on this path -- a
`except Exception: return 0` here would make "the price API is down"
indistinguishable from "this asset is worthless", and the second one produces
a payout of zero or a division by zero rather than an error.
"""

from threading import Lock
from time import time

import requests

_cache = {"data": None, "expires_at": 0.0}
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
}


def fetch_usd_prices(ttl_seconds: int = 30) -> dict:
    with _lock:
        now = time()
        if _cache["data"] and now < _cache["expires_at"]:
            return _cache["data"]
        response = requests.get(
            COINGECKO_URL,
            params={"ids": ",".join(IDS.values()), "vs_currencies": "usd"},
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
        data = {f"{asset}_USD": float(raw[cg_id]["usd"]) for asset, cg_id in IDS.items()}
        data["fetched_at"] = now
        _cache["data"] = data
        _cache["expires_at"] = now + ttl_seconds
        return data


def derive_pair_rate(from_asset: str, to_asset: str, prices: dict) -> float:
    from_usd = prices[f"{from_asset}_USD"]
    to_usd = prices[f"{to_asset}_USD"]
    return from_usd / to_usd
