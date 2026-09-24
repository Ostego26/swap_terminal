from flask import Blueprint, current_app, jsonify, render_template
from db import get_db

bp = Blueprint("health", __name__)

@bp.get("/")
def index():
    return render_template("index.html")

@bp.get("/api/health")
def health_route():
    db = get_db()
    db.execute("SELECT 1").fetchone()
    return jsonify({
        "status": "ok",
        "db_path": current_app.config["DB_PATH"],
        "allowed_pairs": sorted([f"{a}->{b}" for a, b in current_app.config["ALLOWED_PAIRS"]]),
    })
