"""POST /api/swaps and GET /api/swaps/<id>.

Role: submodule (HTTP handlers; the decisions are services/swap_service.py)
Reads: swap_terminal.db (quotes, swaps, deposit_events, payouts), the
       destination chain's RPC (validateaddress) and the source chain's RPC
       (getnewaddress, which derives a new wallet key)
Writes: swap_terminal.db (swaps, swap_audit_log)
Can move funds: no broadcast happens here. It does fix the PAYOUT ADDRESS for
       the swap, which is the address payout_worker will later send to -- so
       the address validation in create_swap() is the last check before a
       destination becomes final.
Mainnet-safe: yes

The `except Exception -> 400` here is the broad kind rule 12 warns about, and
it is annotated at the site with what was checked.
"""

import logging

from db import get_db
from flask import Blueprint, current_app, jsonify, request
from services.swap_service import create_swap, get_swap

logger = logging.getLogger(__name__)

bp = Blueprint("swaps", __name__)

@bp.post("/api/swaps")
def create_swap_route():
    payload = request.get_json(silent=True) or {}
    try:
        swap = create_swap(
            get_db(),
            current_app.config,
            current_app.config["ADAPTERS"],
            payload.get("quote_id", ""),
            payload.get("payout_address", ""),
        )
        return jsonify(swap), 201
    except Exception as exc:  # noqa: BLE001 -- checked: HTTP boundary, same as routes/quotes.py. Note this one now also carries RPCError from validate_address, which means "the daemon could not be asked" rather than "the address is bad" -- the message says which, and no swap row is written in either case.
        # LOGGED as well as returned. The reason already reached the browser, but
        # it reached NOTHING ELSE: the operator's terminal showed
        # `POST /api/swaps HTTP/1.1" 400` five times on 2026-09-26 with no
        # indication of why, and werkzeug's access log prints the status and
        # nothing of the body. So the one place a person was watching had the
        # least information.
        #
        # Rule 14: a failure an operator cannot distinguish from any other failure
        # is a silent one, and a bare 400 is exactly that. At WARNING because a
        # refused swap is not a server fault -- most of these are a bad payout
        # address -- but it IS something somebody needs to be able to read back.
        logger.warning(
            "POST /api/swaps REFUSED: %s  <- quote_id=%r, payout_address=%r (no swap row was written)",
            exc,
            payload.get("quote_id", ""),
            payload.get("payout_address", ""),
        )
        return jsonify({"error": str(exc)}), 400

@bp.get("/api/swaps/<swap_id>")
def get_swap_route(swap_id: str):
    swap = get_swap(get_db(), swap_id)
    if not swap:
        return jsonify({"error": "Swap not found"}), 404
    return jsonify(swap)
