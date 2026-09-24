"""GET /api/rates -- current USD prices and the derived pair rates.

Role: submodule (HTTP handler; no decision of its own)
Reads: CoinGecko simple/price, through the cache in services/pricing.py
Writes: nothing
Can move funds: no
Mainnet-safe: yes -- it talks to a price API, not to a chain.
"""

from flask import Blueprint, current_app, jsonify
from services.pricing import derive_pair_rate, fetch_usd_prices

bp = Blueprint("rates", __name__)

@bp.get("/api/rates")
def get_rates_route():
    prices = fetch_usd_prices(current_app.config["RATE_CACHE_SECONDS"])
    pairs = {}
    for from_asset, to_asset in current_app.config["ALLOWED_PAIRS"]:
        pairs[f"{from_asset}_{to_asset}"] = derive_pair_rate(from_asset, to_asset, prices)
    return jsonify({"prices": prices, "pairs": pairs})
