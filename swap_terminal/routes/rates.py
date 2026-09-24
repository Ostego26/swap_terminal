from flask import Blueprint, current_app, jsonify
from services.pricing import fetch_usd_prices, derive_pair_rate

bp = Blueprint("rates", __name__)

@bp.get("/api/rates")
def get_rates_route():
    prices = fetch_usd_prices(current_app.config["RATE_CACHE_SECONDS"])
    pairs = {}
    for from_asset, to_asset in current_app.config["ALLOWED_PAIRS"]:
        pairs[f"{from_asset}_{to_asset}"] = derive_pair_rate(from_asset, to_asset, prices)
    return jsonify({"prices": prices, "pairs": pairs})
