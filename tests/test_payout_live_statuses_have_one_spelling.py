"""Does the double-payout CHECK read the same status list the INDEX enforces?

Role: test / measurement (seeds real rows, runs the real function, mutates the
      real constant and re-measures)
Reads: swap_terminal/db.py -- PAYOUT_LIVE_STATUSES, PAYOUT_UNIQUE_INDEX_SQL,
       duplicate_live_payouts()
Writes: a throwaway in-memory SQLite database
Can move funds: no -- no adapter, no socket, no signing, no broadcast
Mainnet-safe: yes

WHY THIS FILE EXISTS, which is a defect found on 2026-10-06 while confirming
two unrelated GRC swaps had settled.

`PAYOUT_UNIQUE_INDEX_SQL` derives its status list from `PAYOUT_LIVE_STATUSES`,
and the comment above it (written 2026-10-04) explains at length that spelling
the list twice is rule 8's failure at its smallest. A hundred lines below that
comment, `duplicate_live_payouts()` -- the function whose entire job is to find
double payouts the index did not stop -- spelled the three literals again:

    WHERE status IN ('created', 'broadcast', 'completed')

So the fix was real, the comment explaining it was real, and a third copy sat
below the fold in the same file. That is the specific shape of rule 8 that is
hardest to see: the duplication is not between two modules a reader would think
to compare, it is between a constant and a hand-written string in the same file,
on opposite sides of a screen.

The cost would have been silent and would have looked like good news. Add a
fourth live status and the INDEX moves (it derives) while the CHECK does not
(it did not). The diagnostic that exists to report a double payout the index
missed would stop seeing exactly the new kind the index had just started
allowing, and would report zero rows -- which reads as "no double payouts".

WHAT A TEXT ASSERTION WOULD NOT HAVE CAUGHT, and why this test mutates instead.
Asserting that the source of `duplicate_live_payouts` contains
`PAYOUT_LIVE_STATUSES` is the "the SQL text contains X" evidence CLAUDE.md
refuses. It would pass on a function that mentioned the constant and then
filtered on something else. So this test EXTENDS the real tuple with a status
no writer produces, seeds rows carrying it, and asserts the real function's
returned ROWS changed. If the list is ever hand-spelled again, the mutated
status is invisible to the query and the row count does not move.
"""

import sqlite3

import pytest

import db


@pytest.fixture
def conn():
    """A database with the real SCHEMA and foreign keys off.

    Foreign keys are off deliberately: `payouts.swap_id` references `swaps`, and
    seeding a valid swap needs a quote, a rate and an expiry that have nothing to
    do with what is being measured. The question here is which STATUS values the
    query counts, and that is answerable from `payouts` alone.
    """
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.executescript(db.SCHEMA)
    connection.execute("PRAGMA foreign_keys=OFF")
    return connection


def _seed(connection, swap_id, status):
    connection.execute(
        "INSERT INTO payouts(swap_id, asset, destination_address, amount, txid, status, created_at) "
        "VALUES (?, 'LTC', 'a-destination', 1, NULL, ?, 't')",
        (swap_id, status),
    )


def test_a_live_status_pair_is_reported_and_a_dead_one_is_not(conn):
    """The baseline, so the mutation below has something to move away from."""
    _seed(conn, "s_live", "broadcast")
    _seed(conn, "s_live", "created")
    _seed(conn, "s_dead", "failed")
    _seed(conn, "s_dead", "failed")

    assert [tuple(row) for row in db.duplicate_live_payouts(conn)] == [("s_live", 2)]


def test_extending_the_constant_extends_what_the_check_counts(conn, monkeypatch):
    """The mutation. A new live status must become visible to the query.

    'settling' is not a status anything in this repo writes -- checked 2026-10-06
    that the only values any writer produces are 'created', 'broadcast' and
    'failed' (payout_service.py:904, :1062 and the failure path). It is used here
    precisely because no production code path can produce it, so a pair of rows
    carrying it can only be counted by a query that read the tuple.
    """
    _seed(conn, "s_new", "settling")
    _seed(conn, "s_new", "settling")

    # Before: 'settling' is not live, so two rows for one swap are not a double payout.
    assert [tuple(row) for row in db.duplicate_live_payouts(conn)] == []

    monkeypatch.setattr(db, "PAYOUT_LIVE_STATUSES", (*db.PAYOUT_LIVE_STATUSES, "settling"))

    # After: the same rows, the same function, a different answer -- which is only
    # possible if the function reads the constant rather than its own copy of it.
    assert [tuple(row) for row in db.duplicate_live_payouts(conn)] == [("s_new", 2)]


def test_the_index_derives_from_the_same_constant(conn):
    """The other half of the pair, pinned so the two cannot drift apart again.

    Behavioral rather than textual: the index is BUILT, then a second live payout
    row for one swap is inserted, and the insert must fail. A dead status must
    still be insertable twice.
    """
    conn.execute(db.PAYOUT_UNIQUE_INDEX_SQL)

    _seed(conn, "s_guarded", "broadcast")
    with pytest.raises(sqlite3.IntegrityError):
        _seed(conn, "s_guarded", "created")

    _seed(conn, "s_unguarded", "failed")
    _seed(conn, "s_unguarded", "failed")
