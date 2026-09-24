from flask import Blueprint, current_app, jsonify, request
from db import get_db
from services.swap_service import create_swap, get_swap

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
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400

@bp.get("/api/swaps/<swap_id>")
def get_swap_route(swap_id: str):
    swap = get_swap(get_db(), swap_id)
    if not swap:
        return jsonify({"error": "Swap not found"}), 404
    return jsonify(swap)
