"""The index page and the health endpoint.

Role: submodule (HTTP handlers; no decision of its own)
Reads: swap_terminal.db (a `SELECT 1` liveness probe), current_app.config
Writes: nothing
Can move funds: no
Mainnet-safe: yes

/api/health deliberately echoes the database path and the allowed pairs.
That is rule 14's "echo the parameters that decide the answer": a health
response pasted into a terminal a day later has to say which database it was
talking about. It does NOT echo RPC credentials, which are in the same config
object one key away.
"""

from db import get_db
from flask import Blueprint, current_app, jsonify, render_template

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
