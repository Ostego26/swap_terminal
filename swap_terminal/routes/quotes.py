"""POST /api/quotes -- price a swap and record the quote.

Role: submodule (HTTP handler; the decision is services/quote_service.py)
Reads: CoinGecko simple/price (through services.pricing), swap_terminal.db
Writes: swap_terminal.db (quotes)
Can move funds: no. A quote is a promise about a rate, not a transfer -- but
       the rate it stores is what services/payout_service.py later pays out
       against, so a wrong quote becomes a wrong payout amount two steps later.
Mainnet-safe: yes

The `except Exception -> 400` here is the broad kind rule 12 warns about, and
it is annotated at the site with what was checked.
"""

from db import get_db
from flask import Blueprint, current_app, jsonify, request
from services.quote_service import create_quote

bp = Blueprint("quotes", __name__)

@bp.post("/api/quotes")
def create_quote_route():
    payload = request.get_json(silent=True) or {}
    try:
        quote = create_quote(
            get_db(),
            current_app.config,
            payload.get("from_asset", ""),
            payload.get("to_asset", ""),
            payload.get("input_amount", 0),
        )
        return jsonify(quote), 201
    except Exception as exc:  # noqa: BLE001 -- checked: this is an HTTP boundary, the one place a broad catch is right. Every failure below it (bad pair, non-positive amount, price feed down) is a client-visible 400 carrying the reason, and NOTHING downstream reads a value from this -- the alternative is a 500 with a stack trace and no quote either way. The response body always says which failure it was, so the caller can tell them apart.
        return jsonify({"error": str(exc)}), 400
