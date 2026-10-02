"""POST /api/quotes -- price a swap and record the quote.

Role: submodule (HTTP handler; the decision is services/quote_service.py)
Reads: CoinGecko simple/price (through services.pricing), swap_terminal.db, and
       -- for a SOL-destined quote only -- the Solana RPC endpoint, through the
       adapter it passes down (getMinimumBalanceForRentExemption, cached for
       RENT_FLOOR_CACHE_SECONDS)
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
            # THE ADAPTERS, so the quote can ask a chain a question it cannot
            # answer from settings. Added 2026-10-02 for one such question: the
            # smallest SOL payout the network will deliver to an address that
            # has never been used, which is a CLUSTER parameter and not a
            # constant (services/quote_service.require_deliverable_sol_payout).
            # The same dict routes/swaps.py hands create_swap().
            adapters=current_app.config["ADAPTERS"],
        )
        return jsonify(quote), 201
    except Exception as exc:  # noqa: BLE001 -- checked: this is an HTTP boundary, the one place a broad catch is right. Every failure below it (bad pair, non-positive amount, price feed down) is a client-visible 400 carrying the reason, and NOTHING downstream reads a value from this -- the alternative is a 500 with a stack trace and no quote either way. The response body always says which failure it was, so the caller can tell them apart.
        return jsonify({"error": str(exc)}), 400
