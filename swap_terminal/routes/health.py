"""The health endpoint.

Role: submodule (HTTP handler; no decision of its own)
Reads: swap_terminal.db (a `SELECT 1` liveness probe), current_app.config
       (DB_PATH, ALLOWED_PAIRS, RPC and ADAPTERS). No socket: it reports whether
       an adapter was CONSTRUCTED, never whether its daemon answers -- asking the
       daemon would make a liveness probe depend on six of them.
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

AND IT NOW ECHOES WHAT THIS PROCESS CAN REACH, WHICH COST AN AFTERNOON TO LEARN.

Until 2026-09-26 it reported `allowed_pairs` and nothing else about the chains.
That is what the terminal is WILLING to swap, and the operator's question was what
this PROCESS can reach -- a different question, and the one that mattered. Their
workers printed `GRC rpc=127.0.0.1:25715` from a shell that had the settings
exported; the server had been started from a different environment, so
chains/registry.build_adapters() built no Gridcoin adapter, create_swap()
subscripted `adapters["GRC"]`, and the page showed `No swap was created: 'GRC'` --
str(KeyError("GRC")).

Every instrument they had was either in the wrong process or answering the wrong
question. `env | grep` describes the SHELL, not the running server. The swap page
now shows it, but a browser is not what someone in a terminal has. This endpoint
was the one thing they could curl, and it was echoing the half that was never in
doubt.

So `chains` names, per chain, whether an adapter exists in THIS process and which
environment variables are missing if not, and `offerable_pairs` is the subset of
`allowed_pairs` whose both chains are reachable. Both read the same functions the
swap page and create_swap()'s refusal read, so three surfaces cannot disagree
(rule 8). Variable NAMES only: chains/registry.missing_settings() returns names,
never values, which is what keeps this safe to paste.
"""

from chains.registry import missing_settings
from db import get_db
from flask import Blueprint, current_app, jsonify
from services.pair_view import allowed_pair_rows, offerable_pairs

bp = Blueprint("health", __name__)

@bp.get("/api/health")
def health_route():
    db = get_db()
    db.execute("SELECT 1").fetchone()
    config = current_app.config
    adapters = config["ADAPTERS"]
    rpc = config.get("RPC") or {}
    return jsonify({
        "status": "ok",
        "db_path": config["DB_PATH"],
        "allowed_pairs": sorted([f"{a}->{b}" for a, b in config["ALLOWED_PAIRS"]]),
        # The subset that could actually complete. Equal to allowed_pairs when every
        # chain is reachable; shorter is the answer to "did my settings reach this
        # process?" and it is the line worth reading first.
        "offerable_pairs": sorted(row["label"].replace(" -> ", "->") for row in offerable_pairs(
            allowed_pair_rows(config, adapters)
        )),
        # Every chain the configuration knows, not just the ones in a pair, so a
        # chain that was set up and never enabled is visible rather than absent
        # (rule 14: an absent row is indistinguishable from a row nobody rendered).
        # `missing` carries variable NAMES and never their values.
        "chains": {
            asset: {
                "adapter_built": asset in adapters,
                "missing_settings": missing_settings(rpc, asset),
            }
            for asset in sorted(rpc)
        },
    })
