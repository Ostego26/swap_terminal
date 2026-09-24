"""SQLite schema and connection handling for the swap terminal.

Role: submodule (persistence; holds no decision of its own)
Reads: swap_terminal.db
Writes: swap_terminal.db -- creates quotes, swaps, deposit_events, payouts,
       wallet_inventory and swap_audit_log if they are absent
Can move funds: no
Mainnet-safe: yes

swap_terminal.db is the ONE authority (rule 15). Everything else in this tree
that holds state -- transactions.json, gridcoin_transactions.csv,
grc-sol-swap/.../swap_intents.json -- is either a mirror or, in
swap_intents.json's case, a second system of record that nothing reconciles
with this one. Nothing new may become an authority: there is one.

Two things worth knowing before changing anything here.

WAL is on (`PRAGMA journal_mode=WAL`), so readers do not block the writer. That
is not a concurrency guarantee for the application: SQLite still has exactly
one writer lock, and a guard implemented as a SELECT can go stale between the
read and the write even though the writes themselves are serialized. That is
measured, not supposed -- see tests/test_payout_concurrency.py, where two
payout workers both pay the same swap through a guard that reads correctly.

The `except Exception` around the Flask import is deliberate and is the narrow
kind rule 12 allows: it lets the workers import this module without Flask
installed, and the failure is not silent -- get_db() raises RuntimeError
naming the missing dependency rather than returning something a caller could
mistake for a connection.
"""

import logging
import sqlite3
from contextlib import contextmanager

try:
    from flask import current_app, g
except ImportError:
    # Checked, and narrowed from `except Exception` on 2026-09-24: the only
    # thing that legitimately fails here is Flask being absent, which is the
    # supported case -- the workers use db_session() and never touch
    # request-scoped state. A broader catch would also swallow an error INSIDE
    # a Flask that is installed but broken, and then get_db() would report the
    # wrong cause. The failure is not silent either way: get_db() raises
    # RuntimeError naming the missing dependency rather than returning
    # something a caller could mistake for a connection.
    current_app = None
    g = None

logger = logging.getLogger(__name__)

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS quotes (
    id TEXT PRIMARY KEY,
    from_asset TEXT NOT NULL,
    to_asset TEXT NOT NULL,
    input_amount REAL NOT NULL,
    quoted_rate REAL NOT NULL,
    fee_bps INTEGER NOT NULL,
    network_fee_reserve REAL NOT NULL,
    output_amount_estimate REAL NOT NULL,
    expires_at TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS swaps (
    id TEXT PRIMARY KEY,
    quote_id TEXT NOT NULL,
    from_asset TEXT NOT NULL,
    to_asset TEXT NOT NULL,
    deposit_address TEXT NOT NULL,
    payout_address TEXT NOT NULL,
    expected_input_amount REAL NOT NULL,
    actual_input_amount REAL,
    quoted_rate REAL NOT NULL,
    fee_bps INTEGER NOT NULL,
    network_fee_reserve REAL NOT NULL,
    output_amount_estimate REAL NOT NULL,
    status TEXT NOT NULL,
    min_confirmations INTEGER NOT NULL,
    deposit_txid TEXT,
    payout_txid TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    credited_at TEXT,
    completed_at TEXT,
    expires_at TEXT NOT NULL,
    failed_reason TEXT,
    FOREIGN KEY (quote_id) REFERENCES quotes(id)
);

CREATE INDEX IF NOT EXISTS idx_swaps_status ON swaps(status);
CREATE INDEX IF NOT EXISTS idx_swaps_deposit_address ON swaps(deposit_address);

CREATE TABLE IF NOT EXISTS deposit_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    swap_id TEXT NOT NULL,
    asset TEXT NOT NULL,
    txid TEXT NOT NULL,
    vout INTEGER NOT NULL,
    address TEXT NOT NULL,
    amount REAL NOT NULL,
    confirmations INTEGER NOT NULL,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    credited_at TEXT,
    UNIQUE(asset, txid, vout),
    FOREIGN KEY (swap_id) REFERENCES swaps(id)
);

CREATE INDEX IF NOT EXISTS idx_deposit_events_swap_id ON deposit_events(swap_id);

CREATE TABLE IF NOT EXISTS payouts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    swap_id TEXT NOT NULL,
    asset TEXT NOT NULL,
    destination_address TEXT NOT NULL,
    amount REAL NOT NULL,
    txid TEXT,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    sent_at TEXT,
    FOREIGN KEY (swap_id) REFERENCES swaps(id)
);

CREATE INDEX IF NOT EXISTS idx_payouts_swap_id ON payouts(swap_id);

CREATE TABLE IF NOT EXISTS wallet_inventory (
    asset TEXT PRIMARY KEY,
    hot_confirmed REAL NOT NULL DEFAULT 0,
    hot_reserved REAL NOT NULL DEFAULT 0,
    hot_available REAL NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS swap_audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    swap_id TEXT NOT NULL,
    old_status TEXT,
    new_status TEXT NOT NULL,
    message TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY (swap_id) REFERENCES swaps(id)
);
"""


# The one live payout per swap, as a CONSTRAINT rather than a convention.
#
# PARTIAL ON PURPOSE. A payout that genuinely FAILED must still be retryable,
# so 'failed' is not in the list: a plain UNIQUE(swap_id) would turn one
# rejected transaction into a permanently stuck swap. 'created' IS in the list,
# because a payout row written before a send is an intent to pay that may
# already have been relayed -- see services/payout_service.py's ordering note.
#
# It is not in SCHEMA above, and that is deliberate. Every worker runs
# `executescript(SCHEMA)` at the top of every cycle, so an index that FAILED to
# build -- which is exactly what happens on a database that already contains a
# double payout -- would kill all three workers on startup, including the two
# that have nothing to do with payouts. apply_migrations() below checks first
# and reports instead.
PAYOUT_UNIQUE_INDEX_NAME = "idx_payouts_one_live_per_swap"
PAYOUT_LIVE_STATUSES = ("created", "broadcast", "completed")
PAYOUT_UNIQUE_INDEX_SQL = (
    f"CREATE UNIQUE INDEX IF NOT EXISTS {PAYOUT_UNIQUE_INDEX_NAME} "
    "ON payouts(swap_id) WHERE status IN ('created', 'broadcast', 'completed')"
)


def duplicate_live_payouts(conn: sqlite3.Connection) -> list:
    """Swaps that already have more than one LIVE payout row.

    A non-empty result is the double-payout this index exists to prevent,
    already in the database and already on chain. It is read out rather than
    inferred, because "the index would have stopped it" says nothing about
    rows written before the index existed.
    """
    return conn.execute(
        "SELECT swap_id, COUNT(*) AS live_rows FROM payouts "
        "WHERE status IN ('created', 'broadcast', 'completed') "
        "GROUP BY swap_id HAVING COUNT(*) > 1 ORDER BY swap_id"
    ).fetchall()


def apply_migrations(conn: sqlite3.Connection) -> dict:
    """Bring an EXISTING database up to the current constraints. Idempotent.

    Safe to run against a database with rows in it, and safe to run repeatedly:
    the index is IF NOT EXISTS, and the pre-check is a read.

    What it will NOT do is destroy evidence to make itself succeed. If a swap
    already has two live payout rows, the index cannot be created -- SQLite
    refuses to build a unique index over data that violates it -- and the
    honest outcome is to say so, name the swap_ids, and leave the rows alone.
    Deleting one of them to get the index built would be deleting the record of
    a payment that may be on chain.

    Returns a dict the caller can print or assert on:
        {"index_created": bool, "duplicates": [{"swap_id":…, "live_rows":…}, …]}
    """
    duplicates = duplicate_live_payouts(conn)
    if duplicates:
        rows = ", ".join(f"{row['swap_id']}={row['live_rows']}" for row in duplicates)
        logger.error(
            "%s NOT created: %d swap(s) already have more than one live payout row (%s)  <- each of those is a "
            "payout that was made twice; reconcile them on chain before this constraint can be applied. Nothing "
            "has been deleted.",
            PAYOUT_UNIQUE_INDEX_NAME,
            len(duplicates),
            rows,
        )
        return {"index_created": False, "duplicates": duplicates}
    conn.execute(PAYOUT_UNIQUE_INDEX_SQL)
    conn.commit()
    return {"index_created": True, "duplicates": []}


def dict_factory(cursor, row):
    return {col[0]: row[idx] for idx, col in enumerate(cursor.description)}


def connect_db(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = dict_factory
    return conn


def get_db() -> sqlite3.Connection:
    if current_app is None or g is None:
        raise RuntimeError("Flask is required for request-scoped database access")
    if "db" not in g:
        g.db = connect_db(current_app.config["DB_PATH"])
    return g.db


def close_db(_=None) -> None:
    if g is None:
        return
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db() -> None:
    db = get_db()
    db.executescript(SCHEMA)
    db.commit()
    apply_migrations(db)


@contextmanager
def db_session(db_path: str):
    conn = connect_db(db_path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
