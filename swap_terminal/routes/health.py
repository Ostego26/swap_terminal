"""The health endpoint.

Role: submodule (HTTP handler; no decision of its own)
Reads: swap_terminal.db (a `SELECT 1` liveness probe), current_app.config
Writes: nothing
Can move funds: no
Mainnet-safe: yes

THE INDEX PAGE MOVED OUT OF THIS FILE on 2026-09-26, to routes/ui.py, along
with the two other pages a customer sees. This module used to own `/` because
there was one template and nowhere else to put it; now there is a customer
surface (routes/ui.py) and an operator surface (routes/admin.py), and a health
probe that also happens to serve the home page belongs to neither. Nothing
imported `health.index` -- grepped by name across every .py, .html, .js and .sh
in the tree, and no test referenced the `/` route at all -- so the move breaks
no caller. `render_template` went with it and is no longer imported here.

/api/health deliberately echoes the database path and the allowed pairs.
That is rule 14's "echo the parameters that decide the answer": a health
response pasted into a terminal a day later has to say which database it was
talking about. It does NOT echo RPC credentials, which are in the same config
object one key away.
"""

from db import get_db
from flask import Blueprint, current_app, jsonify

bp = Blueprint("health", __name__)

@bp.get("/api/health")
def health_route():
    db = get_db()
    db.execute("SELECT 1").fetchone()
    return jsonify({
        "status": "ok",
        "db_path": current_app.config["DB_PATH"],
        "allowed_pairs": sorted([f"{a}->{b}" for a, b in current_app.config["ALLOWED_PAIRS"]]),
    })
