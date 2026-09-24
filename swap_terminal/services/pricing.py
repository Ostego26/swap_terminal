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
IDS = {
    "BTC": "bitcoin",
    "LTC": "litecoin",
    "GRC": "gridcoin-research",
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
        data = {
            "BTC_USD": float(raw[IDS["BTC"]]["usd"]),
            "LTC_USD": float(raw[IDS["LTC"]]["usd"]),
            "GRC_USD": float(raw[IDS["GRC"]]["usd"]),
            "fetched_at": now,
        }
        _cache["data"] = data
        _cache["expires_at"] = now + ttl_seconds
        return data


def derive_pair_rate(from_asset: str, to_asset: str, prices: dict) -> float:
    from_usd = prices[f"{from_asset}_USD"]
    to_usd = prices[f"{to_asset}_USD"]
    return from_usd / to_usd
