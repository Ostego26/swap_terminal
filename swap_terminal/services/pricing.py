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
