"""The SQLite schema the Solana bridge's swap intents belong in, and the claim that guards a payout.

Role: submodule (schema text and the two SQL statements that decide a payout)
Reads: nothing
Writes: nothing by itself -- migrate_swap_intents.py applies SCHEMA
Can move funds: no. But CLAIM_SQL is the statement that would decide whether a
        SOL payout is authorized, so everything true of the payout path is true
        of that string.
Mainnet-safe: yes to import. Applying SCHEMA only creates tables; it never
        reads or writes an existing row.

WHY THIS FILE EXISTS.

`grc-sol-swap/abstergo_exchange/swap_intents.json` is the Express server's
authority for a fund-releasing decision: the server opens it, reads
`status == 'verified'`, and pays. CLAUDE.md says that three separate ways.

    rule 5   "If a reader has to open a file to learn whether a payout is
             authorized, the authority is in the wrong place."
    rule 15  "swap_terminal.db is the only place a decision may be made from."
             And: the Express server "has no connection to swap_terminal.db at
             all ... so there are currently two systems of record for swaps,
             and nothing reconciles them."
    rule 20  "A filter, a join, or a ranking over rows already in
             swap_terminal.db is a query. A per-row Python loop over the same
             rows is the same logic in a place only its author can inspect."

So the tables below are where those intents go. NOTHING IN THIS REPOSITORY
READS THEM YET, and that is deliberate: migrating live intents and repointing
the server's read are changes to armed state and to the payout path, which
rule 16 puts with the operator. This file and migrate_swap_intents.py are the
preparation. The switch is theirs to throw, after they have seen the dry run.

WHY THE SCHEMA IS HERE AND NOT IN db.py.

db.py holds the Flask application's schema and is the obvious home. It is
avoided on purpose for one pass only: a parallel change to the payout path is
adding a partial unique index to db.py's `payouts` table at the same time, and
two branches editing one SQL string produces a merge conflict in the one file
where a botched merge is least visible. Folding these tables into db.py's
SCHEMA is a one-line import once both have landed, and it should be done --
a second schema module is rule 8's shape and is not where this ends.

--------------------------------------------------------------------------
THE CLAIM, AND WHY IT IS AN UPDATE RATHER THAN A SELECT
--------------------------------------------------------------------------

The whole point of moving this into SQL is that the decision becomes a write.

Measured on the Python side of this repository, in
tests/test_payout_concurrency.py: two payout workers polling one database both
paid one swap, through a guard that was CORRECT about what it asked --

    SELECT * FROM payouts WHERE swap_id = ? AND status IN ('broadcast','completed')

-- and was still not enough, because a read happens before the writer lock is
ever contended. Worker B runs it while worker A is inside its send with an
open transaction, sees no payout row, decides to pay, and only then blocks on
the lock. When A commits, B's write proceeds on a decision made from a snapshot
that is now stale. The lock serialized the writes and did nothing at all about
the decision.

The identical shape existed in JavaScript, in the Express server, measured in
grc-sol-swap/abstergo_exchange/tests/intent_store.test.js: two concurrent
/execute calls, two payouts authorized for one deposit. One defect, two
languages, and neither copy pointed at the other. Rule 8 is what this is.

CLAIM_SQL turns the guard into a write:

    UPDATE swap_intents SET status='paying' WHERE intent_id=? AND status='verified'

and the caller proceeds only if `cursor.rowcount == 1`. The loser updates zero
rows. SQLite's single writer lock, which was useless against a read, is exactly
right against this: it serializes the DECISION, not merely the record of it.

And it is backed by a constraint, because a guard that depends on a caller
remembering to check rowcount is a guard one refactor from being gone:

    CREATE UNIQUE INDEX idx_swap_intent_payouts_one_live_per_intent
        ON swap_intent_payouts(intent_id)
     WHERE status IN ('claimed', 'broadcast', 'completed')

A PARTIAL unique index, so a payout that genuinely FAILED can be retried by an
operator while a second live payout for one intent is impossible to insert.
Both halves are needed and neither is sufficient: the UPDATE alone leaves the
window open if a future caller forgets the rowcount check, and the index alone
turns a double payout into an unhandled IntegrityError AFTER the first send --
safe, but it reports a data problem instead of preventing a decision.

This mirrors what services/payout_service.py's claim does for `swaps`. Named at
both sites so a reader who finds one is told the other exists.
"""

# Every status an intent may hold, and what each one means for a payout.
#
# The CHECK constraint below is generated from this tuple rather than spelled
# again in the SQL (rule 11's shape: one vocabulary, derived in one place). A
# hand-maintained second copy of a status list is rule 8's failure with a delay
# on it -- the copies agree the day they are written, and nothing fails when
# one of them later disagrees.
INTENT_STATUSES: tuple[str, ...] = (
    "awaiting_deposit",  # created; no verified deposit; not payable
    "verified",          # deposit confirmed; THE ONLY PAYABLE STATUS
    "paying",            # claimed by exactly one payer; not payable again
    "paid",              # done
    "payout_failed",     # an attempt errored; needs an operator and the chain
    "expired",           # TTL passed before verification
)

# The one status a payout may be claimed from. Written once, used by CLAIM_SQL.
CLAIMABLE_STATUS = "verified"

# Payout-row statuses that count as "live" for the partial unique index. A row
# in any of these blocks a second payout for the same intent; 'failed' does not,
# which is what makes an operator-driven retry possible.
LIVE_PAYOUT_STATUSES: tuple[str, ...] = ("claimed", "broadcast", "completed")
PAYOUT_STATUSES: tuple[str, ...] = (*LIVE_PAYOUT_STATUSES, "failed")


def _sql_in_list(values: tuple[str, ...]) -> str:
    """Render a tuple of known-safe literals as a SQL IN list.

    These are module constants defined three lines up, not input from anywhere,
    which is the distinction S608 is about: the interpolation is a VOCABULARY
    this repository controls, and deriving the constraint from the tuple is the
    entire reason the tuple exists. Quoting is still applied so a future
    addition containing an apostrophe cannot produce broken SQL silently.
    """
    return ", ".join("'" + value.replace("'", "''") + "'" for value in values)


SCHEMA = f"""
CREATE TABLE IF NOT EXISTS swap_intents (
    intent_id TEXT PRIMARY KEY,
    status TEXT NOT NULL CHECK (status IN ({_sql_in_list(INTENT_STATUSES)})),
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    gridcoin_deposit_address TEXT NOT NULL,
    destination_solana_address TEXT NOT NULL,
    expected_grc_amount REAL NOT NULL,
    -- The quote is stored FLATTENED rather than as a JSON blob. Rule 5 again:
    -- a lamport figure that a payout is made from is a decision input, and a
    -- decision input inside a JSON column is a decision only its author can
    -- inspect. quoted_lamports is INTEGER because lamports are integral and
    -- REAL would reintroduce the float rounding the base unit exists to avoid.
    quoted_lamports INTEGER NOT NULL CHECK (quoted_lamports > 0),
    quoted_sol_amount REAL NOT NULL,
    quoted_grc_price_usd REAL,
    quoted_sol_price_usd REAL,
    -- Provenance, so a row that came out of the JSON file can be told from one
    -- the server wrote. A migration that leaves no trace of itself is a
    -- migration nobody can audit afterwards.
    imported_from TEXT,
    imported_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_swap_intents_status ON swap_intents(status);
CREATE INDEX IF NOT EXISTS idx_swap_intents_expires_at ON swap_intents(expires_at);

CREATE TABLE IF NOT EXISTS swap_intent_deposits (
    intent_id TEXT PRIMARY KEY,
    gridcoin_txid TEXT NOT NULL,
    confirmations INTEGER NOT NULL,
    received_grc_amount REAL NOT NULL,
    -- Nullable because the Express server hard-codes it to null today
    -- (verifyGridcoinReceiptForIntent returns sourceGridcoinAddress: null).
    -- The column exists so that the day it starts being recorded, nothing has
    -- to migrate; it is NOT evidence the value is being captured.
    source_gridcoin_address TEXT,
    verification_source TEXT,
    verified_at TEXT NOT NULL,
    FOREIGN KEY (intent_id) REFERENCES swap_intents(intent_id)
);

-- One row per payout ATTEMPT, not one per intent. A failed attempt keeps its
-- row -- with its error and the signature if one was ever produced -- because
-- the question an operator asks after a failure is "what was tried, and did
-- any of it reach the chain?", and a row that was overwritten cannot answer.
CREATE TABLE IF NOT EXISTS swap_intent_payouts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    intent_id TEXT NOT NULL,
    claim_token TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ({_sql_in_list(PAYOUT_STATUSES)})),
    destination_solana_address TEXT NOT NULL,
    lamports INTEGER NOT NULL CHECK (lamports > 0),
    signature TEXT,
    error TEXT,
    claimed_at TEXT NOT NULL,
    paid_at TEXT,
    failed_at TEXT,
    FOREIGN KEY (intent_id) REFERENCES swap_intents(intent_id)
);

CREATE INDEX IF NOT EXISTS idx_swap_intent_payouts_intent_id ON swap_intent_payouts(intent_id);

-- THE CONSTRAINT. Read the module docstring before changing it. A second LIVE
-- payout for one intent is impossible to insert; a retry after a genuine
-- failure remains possible, because 'failed' is outside the index predicate.
CREATE UNIQUE INDEX IF NOT EXISTS idx_swap_intent_payouts_one_live_per_intent
    ON swap_intent_payouts(intent_id)
 WHERE status IN ({_sql_in_list(LIVE_PAYOUT_STATUSES)});
"""

# THE CLAIM. Check `cursor.rowcount == 1` -- it is the authorization, and the
# loser updates zero rows and must not pay.
#
# The claimable status is a BOUND PARAMETER rather than an interpolated
# constant. It could legitimately have been interpolated -- it is a module
# constant three lines up, not input from anywhere, which is the distinction
# S608 is about -- but rule 19's test for whether something is a patch is
# whether it stops the symptom being reported or stops the cause existing.
# Passing it as a parameter removes the interpolation, so there is no `noqa`
# to read, no reviewer left deciding whether this one was the safe kind, and
# nothing to get wrong if someone later makes the status dynamic.
#
# Parameters, in order: (updated_at, intent_id, CLAIMABLE_STATUS). CLAIM_PARAMS
# builds them, so no caller has to remember the order or spell the status
# again.
CLAIM_SQL = """
UPDATE swap_intents
   SET status = 'paying', updated_at = ?
 WHERE intent_id = ?
   AND status = ?
"""


def claim_params(updated_at: str, intent_id: str) -> tuple[str, str, str]:
    """The parameter tuple for CLAIM_SQL, with the claimable status filled in."""
    return (updated_at, intent_id, CLAIMABLE_STATUS)


# Released back to payable ONLY by an operator, never automatically. See
# intent_store.js's defect 3: an error out of sendAndConfirmTransaction
# includes confirmation timeouts on transfers that may have landed, so
# re-arming on an error of unknown meaning is how one deposit becomes two
# transfers. There is deliberately no SQL here that does it.
