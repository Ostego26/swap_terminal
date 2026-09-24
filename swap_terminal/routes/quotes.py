from flask import Blueprint, current_app, jsonify, request
from db import get_db
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
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400
