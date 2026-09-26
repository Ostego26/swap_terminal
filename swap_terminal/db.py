"""SQLite schema and connection handling for the swap terminal.

Role: submodule (persistence; holds no decision of its own)
Reads: swap_terminal.db
Writes: swap_terminal.db -- creates quotes, swaps, deposit_events, payouts,
       wallet_inventory, swap_audit_log and xrp_destination_tags if they are
       absent, plus the two triggers that make an allocated XRP destination
       tag immutable and undeletable
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

# Rootless, the same way services/deposit_service.py reaches
# deposit_vout_artifact.py: swap_terminal/ is already on sys.path for `db` to
# have been importable at all. chains/__init__.py is deliberately empty of code
# and chains/xrp_units.py imports only `decimal`, so this pulls in no adapter,
# opens no socket and reads no credential -- which is what makes it safe for a
# module every worker imports at startup.
from chains.xrp_units import FIRST_ALLOCATABLE_TAG, MAX_DESTINATION_TAG

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


# THE XRP TAG DDL IS DERIVED, NOT SPELLED A SECOND TIME.
#
# The two bounds in the CHECK come from chains/xrp_units.py, which is where
# the measurement behind them lives. Writing `BETWEEN 1 AND 4294967295` into
# the schema literal would put the same rule in two files, which is rule 8's
# bug-with-a-delay-on-it: the copies agree the day they are written, and the
# one that drifts is whichever file the next reader does not open. Mammon's
# contract_horizon_sql() is generated from _HORIZON_SUFFIXES for exactly this
# reason, and this is the same shape.
#
# An f-string building SQL would normally be S608 territory. It is not
# interpolating input or an identifier here -- both values are module-level
# integer constants from this application's own source, and ruff does not
# flag it because there is no execute() call in sight: this is a DDL string
# handed to executescript() at startup.
XRP_DESTINATION_TAG_SCHEMA = f"""
-- THE XRP DEPOSIT IDENTIFIER. One shared account, one integer per swap.
--
-- Every other chain here hands out a fresh deposit address, so `swaps.deposit_
-- address` alone identifies who paid. The XRP Ledger does not work that way:
-- chains/xrp.py::get_new_address() refuses precisely because deriving an
-- account per swap would cost a base reserve and put a signing key per swap on
-- this host, and the ledger's own answer -- the one every exchange on it uses
-- -- is a `DestinationTag`: an integer carried by the payment, read by
-- chains/xrp_payments.py into the event's `vout` field.
--
-- WHY THIS IS A TABLE AND NOT A COLUMN ON `swaps`.
--
-- The invariant that matters is UNIQUENESS, and it is worth stating what it
-- costs to lose: two open swaps sharing a tag means one customer's deposit is
-- credited to the other customer's swap, and the payout that follows is on
-- chain and final. A nullable `swaps.xrp_destination_tag` could carry a UNIQUE
-- index too, but it could not carry the other three guarantees below, and it
-- would be NULL for five of the six assets -- a column that means nothing for
-- most rows is a column readers have to learn the exception for.
--
-- FOUR GUARANTEES, ALL OF THEM IN THE DATABASE (rules 5, 15 and 20). None is a
-- Python check, because a Python check-then-insert is a read that can go stale
-- before the write -- which is not a hypothesis here: it is exactly how two
-- payout workers paid one swap twice on 2026-09-24, measured in
-- tests/test_payout_concurrency.py, and the fix was the same shape as this.
--
--   PRIMARY KEY (account, destination_tag)
--        No two rows share a tag on one account. Per ACCOUNT rather than
--        globally because that is the real scope -- a tag means nothing except
--        against the account it was sent to -- and it keeps the numbers small
--        if the operator ever moves accounts.
--
--   UNIQUE swap_id (idx_xrp_tag_one_per_swap)
--        No swap gets two tags. A swap with two tags is a swap whose deposit
--        instructions differ depending on which row you read.
--
--   CONSTRAINT xrp_tag_is_allocatable
--        FIRST_ALLOCATABLE_TAG..MAX_DESTINATION_TAG, which is 1..4294967295.
--        The upper bound is the protocol's, MEASURED against xrpl-py's own
--        serializer (chains/xrp_units.py point 4). The LOWER bound is OURS: 0
--        is a perfectly legal tag and is reserved unallocated, because it is
--        what every "no tag to send" integration emits. The reasoning is at
--        chains/xrp_units.RESERVED_DESTINATION_TAG and is not repeated here.
--        The constraint is NAMED so the IntegrityError says which rule was
--        broken -- measured: "CHECK constraint failed: xrp_tag_is_allocatable".
--
--   xrp_destination_tags_are_never_released / _repointed
--        Two BEFORE triggers that RAISE(ABORT). This is the reuse decision,
--        expressed as something the database will not let anyone do rather than
--        as a convention a future writer can forget. TAGS ARE NEVER REUSED:
--        the XRP Ledger puts no expiry on a tag, an address-book entry or a
--        withdrawal retry can carry one months after a swap completed, and a
--        reallocated tag turns that late payment into a credit against a
--        stranger's swap. Allocation reads MAX(destination_tag) over ALL rows,
--        so as long as no row is ever deleted the sequence cannot go backward
--        -- the DELETE trigger is what makes that "cannot" rather than
--        "should not". The cost is exhaustion, and it is not a real cost: at
--        4,294,967,295 tags, 1,000 swaps a day lasts 11,759 years and 10,000 a
--        day lasts 1,176.
--
-- THERE IS NO `retired_at` COLUMN, on purpose. Whether a swap is finished is
-- already `swaps.status`, and a second copy of that fact here would be rule
-- 8's two-copies-drift: the row that says a tag is retired and the swap that
-- says it is still open, each correct in its own table. Join instead.
--
-- ORDERING NOTE FOR A CALLER: the FOREIGN KEY means the swap row must exist
-- before its tag is allocated. Measured 2026-09-26 and worth knowing before
-- relying on it -- `PRAGMA foreign_keys` defaults to OFF on a fresh
-- sqlite3 connection and is turned ON by the pragma at the top of this SCHEMA,
-- so the constraint bites on any connection that ran executescript(SCHEMA)
-- (every worker, every cycle) and does not on a bare connect_db().
CREATE TABLE IF NOT EXISTS xrp_destination_tags (
    account TEXT NOT NULL,
    destination_tag INTEGER NOT NULL,
    swap_id TEXT NOT NULL,
    allocated_at TEXT NOT NULL,
    PRIMARY KEY (account, destination_tag),
    CONSTRAINT xrp_tag_is_allocatable CHECK (destination_tag BETWEEN {FIRST_ALLOCATABLE_TAG} AND {MAX_DESTINATION_TAG}),
    FOREIGN KEY (swap_id) REFERENCES swaps(id)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_xrp_tag_one_per_swap ON xrp_destination_tags(swap_id);

CREATE TRIGGER IF NOT EXISTS xrp_destination_tags_are_never_released
BEFORE DELETE ON xrp_destination_tags
BEGIN
    SELECT RAISE(ABORT, 'xrp_destination_tags rows are never deleted: allocation reads MAX(destination_tag), so a deleted row lets the next tag repeat one already given out, and a late payment carrying it would credit the wrong swap');
END;

CREATE TRIGGER IF NOT EXISTS xrp_destination_tags_are_never_repointed
BEFORE UPDATE OF account, destination_tag, swap_id ON xrp_destination_tags
BEGIN
    SELECT RAISE(ABORT, 'xrp_destination_tags: account, destination_tag and swap_id are immutable once allocated. Re-pointing a tag at a different swap misattributes every payment already in flight against it');
END;
"""

# One string for executescript(). Concatenated rather than interpolated into
# SCHEMA itself so that SCHEMA stays a plain literal and only the part that
# genuinely needs derived values is an f-string.
#
# ORDER: after `swaps`, because xrp_destination_tags has a FOREIGN KEY into
# it. SQLite resolves foreign key TARGETS at DML time rather than at CREATE
# time, so this ordering is for a human reader rather than for the engine.
SCHEMA = SCHEMA + XRP_DESTINATION_TAG_SCHEMA


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
