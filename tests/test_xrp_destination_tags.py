"""Can two swaps be given the same XRP destination tag? Measured, not reasoned about.

Role: test / measurement (seeds real rows into the real schema, runs the real
      allocator, asserts on the rows actually present)
Reads: swap_terminal/services/xrp_tag_service.py, swap_terminal/db.py's SCHEMA,
      swap_terminal/chains/xrp_units.py, and -- where it is installed --
      xrpl-py's own binary codec
Writes: a throwaway SQLite database under pytest's tmp_path
Can move funds: no. Nothing here opens a socket, signs anything, broadcasts
      anything or reads a key. There is no adapter in this file at all: the
      allocator takes a database connection and returns an integer.
Mainnet-safe: yes

=============================================================================
WHAT IS MEASURED HERE RATHER THAN ASSERTED ABOUT THE SOURCE
=============================================================================

CLAUDE.md's "verify by behavior, never by reading the code" is the shape of
every test below: the real db.SCHEMA is executed, real swap rows are seeded, the
real allocate_destination_tag() runs, and the assertions are on the rows in
xrp_destination_tags and on the exceptions SQLite itself raised. Nothing here
greps the schema text for "UNIQUE" -- that would prove the word is present and
nothing about whether it holds.

THE CONCURRENCY TEST IS THE ONE THAT MATTERS, and it is deliberately built the
same way tests/test_payout_concurrency.py is, because that file is this
repository's proof that reasoning about SQLite locking is not enough. Two payout
workers paid one swap twice through a guard that read correctly, on 2026-09-24,
and the mechanism was a SELECT that completed before the write lock was
contended. So the collision case here uses real threads, real separate
connections, and a real file on disk -- not a mock, not a simulated race, and
not a single connection pretending to be two.

WHAT THESE TESTS DO NOT PROVE (rule 17). Nothing here touches the XRP Ledger.
The tag RANGE is measured against xrpl-py's serializer, which is the reference
implementation's own Python binding and is the strongest authority reachable
from this container -- but it is a library, not a ledger, and
test_the_max_tag_constant_matches_the_reference_serializer says so in its own
docstring. The concurrency measured is threads against one local SQLite file,
which is the faithful shape for two worker processes on one host; it says
nothing about two hosts against a database on a network file system, where
SQLite's locking assumptions are different and were not tested.
"""

import sqlite3
import threading

import pytest
from chains.xrp_units import (
    FIRST_ALLOCATABLE_TAG,
    MAX_DESTINATION_TAG,
    RESERVED_DESTINATION_TAG,
    XRPTagError,
    validate_destination_tag,
)
from db import SCHEMA, connect_db
from services.xrp_tag_service import (
    XRPTagAllocationError,
    allocate_destination_tag,
    allocation_summary,
    describe_deposit_instruction,
    destination_tag_for_swap,
    swap_id_for_tag,
    validate_account,
)

# The ledger's own well-known constants, the same two tests/test_xrp_address.py
# uses. Real addresses with real checksums, so validate_account() is exercised
# against something the ledger defines rather than against a string invented
# here that happens to pass.
ACCOUNT_ZERO = "rrrrrrrrrrrrrrrrrrrrrhoLvTp"
ACCOUNT_ONE = "rrrrrrrrrrrrrrrrrrrrBZbvji"

# A real X-address, taken from tests/test_xrp_address.py's own fixtures, used to
# check the refusal rather than to check address decoding (which that file owns).
X_ADDRESS = "X7AcgcsBL6XDcUb289X4mJ8djcdyKaB5hJDWMArnXr61cqZ"

NOW = "2026-09-26T00:00:00+00:00"


def _seed_swaps(conn: sqlite3.Connection, swap_ids) -> None:
    """Insert one quote and the named swaps, so the FOREIGN KEY has a target.

    The swaps are seeded with GRC/LTC assets rather than XRP, and that is not an
    oversight: XRP is not in Config.ALLOWED_PAIRS and this suite must not need
    it to be. The tag table keys on swap_id and knows nothing about the swap's
    assets, so the allocator's behavior does not depend on them -- which is
    itself worth having pinned, because it means wiring XRP in later cannot
    change what these tests measure.
    """
    conn.execute(
        """
        INSERT INTO quotes (id, from_asset, to_asset, input_amount, quoted_rate, fee_bps,
                            network_fee_reserve, output_amount_estimate, expires_at, created_at)
        VALUES ('q_tag', 'GRC', 'LTC', 100.0, 0.001, 150, 0.001, 0.0975, ?, ?)
        """,
        (NOW, NOW),
    )
    for swap_id in swap_ids:
        conn.execute(
            """
            INSERT INTO swaps (
                id, quote_id, from_asset, to_asset, deposit_address, payout_address,
                expected_input_amount, actual_input_amount, quoted_rate, fee_bps,
                network_fee_reserve, output_amount_estimate, status, min_confirmations,
                deposit_txid, payout_txid, created_at, updated_at, credited_at,
                completed_at, expires_at, failed_reason
            ) VALUES (?, 'q_tag', 'GRC', 'LTC', 'grc_deposit_addr', 'ltc_payout_addr',
                      100.0, NULL, 0.001, 150, 0.001, 0.0975, 'awaiting_deposit', 6,
                      NULL, NULL, ?, ?, NULL, NULL, ?, NULL)
            """,
            (swap_id, NOW, NOW, NOW),
        )
    conn.commit()


def _fresh_db(tmp_path, swap_ids=("s_one", "s_two", "s_three")) -> str:
    """A real database file with the real SCHEMA and the named swaps seeded.

    executescript(SCHEMA) is what every worker runs at the top of every cycle,
    so this is the same schema state the running application has -- including
    `PRAGMA foreign_keys=ON`, which that script sets and which a bare
    connect_db() does not.
    """
    path = str(tmp_path / "swap_terminal_xrp_tags.db")
    conn = connect_db(path)
    conn.executescript(SCHEMA)
    _seed_swaps(conn, swap_ids)
    conn.close()
    return path


@pytest.fixture
def db(tmp_path):
    path = _fresh_db(tmp_path)
    conn = connect_db(path)
    conn.executescript(SCHEMA)
    yield conn
    conn.close()


# ---------------------------------------------------------------------------
# The sequence itself
# ---------------------------------------------------------------------------


def test_the_first_tag_is_one_and_not_zero(db):
    """The reserved value is skipped, and the skip is the assertion.

    RESERVED_DESTINATION_TAG is 0 and it is a LEGAL tag on the ledger -- that is
    measured in test_the_reference_serializer_carries_tag_zero below. It is
    reserved because it is the value every "no tag to send" integration emits,
    so a payment carrying it must resolve to no swap rather than to whichever
    swap was created first.
    """
    assert allocate_destination_tag(db, ACCOUNT_ZERO, "s_one") == FIRST_ALLOCATABLE_TAG
    assert FIRST_ALLOCATABLE_TAG == 1
    assert RESERVED_DESTINATION_TAG == 0
    assert swap_id_for_tag(db, ACCOUNT_ZERO, RESERVED_DESTINATION_TAG) is None


def test_consecutive_allocations_are_distinct_and_increasing(db):
    tags = [allocate_destination_tag(db, ACCOUNT_ZERO, s) for s in ("s_one", "s_two", "s_three")]
    assert tags == [1, 2, 3]
    assert len(set(tags)) == len(tags)


def test_the_sequence_is_per_account(db):
    """Two accounts each start at 1, and neither collides with the other.

    Uniqueness is scoped to the account because a tag means nothing except
    against the account it was sent to. If this were global, introducing a
    second account would start it at whatever the first had reached -- which is
    harmless but tells a reader the scope is something it is not.
    """
    first = allocate_destination_tag(db, ACCOUNT_ZERO, "s_one")
    second = allocate_destination_tag(db, ACCOUNT_ONE, "s_two")
    assert first == second == FIRST_ALLOCATABLE_TAG
    assert swap_id_for_tag(db, ACCOUNT_ZERO, first) == "s_one"
    assert swap_id_for_tag(db, ACCOUNT_ONE, second) == "s_two"


def test_a_tag_resolves_back_to_its_own_swap_and_no_other(db):
    """The reverse lookup, which is the function the deposit path needs."""
    tag_one = allocate_destination_tag(db, ACCOUNT_ZERO, "s_one")
    tag_two = allocate_destination_tag(db, ACCOUNT_ZERO, "s_two")
    assert swap_id_for_tag(db, ACCOUNT_ZERO, tag_one) == "s_one"
    assert swap_id_for_tag(db, ACCOUNT_ZERO, tag_two) == "s_two"
    assert destination_tag_for_swap(db, "s_one") == tag_one
    assert destination_tag_for_swap(db, "s_two") == tag_two
    # An unallocated tag resolves to nothing rather than to the nearest swap.
    assert swap_id_for_tag(db, ACCOUNT_ZERO, 9999) is None
    assert destination_tag_for_swap(db, "s_three") is None
    # And a tag allocated on one account does not resolve on another.
    assert swap_id_for_tag(db, ACCOUNT_ONE, tag_one) is None


# ---------------------------------------------------------------------------
# Uniqueness: the whole job
# ---------------------------------------------------------------------------


def test_the_database_refuses_a_duplicate_tag_on_one_account(db):
    """The guarantee, exercised by trying to break it directly.

    This bypasses the allocator on purpose. The allocator cannot produce a
    duplicate (it computes and claims in one statement), so asserting that it
    does not would measure the arithmetic and not the constraint. Inserting the
    duplicate by hand is what proves the DATABASE is the thing holding -- which
    is the claim, because a future caller who writes their own INSERT gets the
    same refusal.
    """
    tag = allocate_destination_tag(db, ACCOUNT_ZERO, "s_one")
    with pytest.raises(sqlite3.IntegrityError) as caught:
        db.execute(
            "INSERT INTO xrp_destination_tags (account, destination_tag, swap_id, allocated_at) "
            "VALUES (?, ?, ?, ?)",
            (ACCOUNT_ZERO, tag, "s_two", NOW),
        )
    assert "UNIQUE constraint failed" in str(caught.value)
    assert swap_id_for_tag(db, ACCOUNT_ZERO, tag) == "s_one"


def test_one_swap_cannot_hold_two_tags(db):
    allocate_destination_tag(db, ACCOUNT_ZERO, "s_one")
    with pytest.raises(XRPTagAllocationError) as caught:
        allocate_destination_tag(db, ACCOUNT_ZERO, "s_one")
    assert "already has a destination tag" in str(caught.value)
    assert destination_tag_for_swap(db, "s_one") == FIRST_ALLOCATABLE_TAG
    rows = db.execute("SELECT COUNT(*) AS n FROM xrp_destination_tags WHERE swap_id = 's_one'").fetchone()
    assert rows["n"] == 1


def test_two_threads_cannot_allocate_the_same_tag(tmp_path):
    """The collision case, with real threads on real separate connections.

    THE SHAPE IS BORROWED FROM tests/test_payout_concurrency.py ON PURPOSE. That
    file is this repository's measurement that a correct-looking guard does not
    hold under concurrency, so the bar for "tags cannot collide" is a real race
    rather than an argument. Each thread opens its OWN connection -- sqlite3
    objects are bound to the thread that created them, and two worker processes
    would each hold their own anyway.

    Both threads start from a barrier so their allocations overlap rather than
    happening to run in sequence, and every returned tag is collected.

    THE ASSERTION IS "EVERY SWAP GOT A TAG", NOT MERELY "NO TWO TAGS MATCH", AND
    THE FIRST DRAFT OF THIS TEST GOT THAT WRONG. It checked only that the issued
    tags were distinct and that no raw sqlite3.IntegrityError escaped. Mutation-
    checked on 2026-09-26 by splitting allocate_destination_tag()'s single
    statement into the check-then-insert it exists to avoid -- a SELECT for
    MAX(destination_tag), a 20ms pause, then a separate INSERT of MAX+1 -- and
    THE TEST PASSED. Measured on the split version: 6 of 12 allocations
    succeeded and 6 failed with "tag collision", because the losing thread read
    a MAX its sibling had not yet committed.

    Two things had made it blind. The PRIMARY KEY did its job, so no duplicate
    ever reached the table and the distinctness assertion was satisfied by the
    six survivors; and allocate_destination_tag() converts sqlite3.IntegrityError
    into XRPTagAllocationError, so the `isinstance(exc, sqlite3.IntegrityError)`
    filter matched nothing. A test whose green run is produced by the constraint
    catching the bug is a test that cannot tell a safe allocator from an unsafe
    one -- and CLAUDE.md is explicit that a test passing against a broken
    implementation is worse than no test.

    So the assertions now are: every seeded swap holds a tag, no attempt raised
    at all, and the tags are exactly 1..N with no gaps. A gap is the signature of
    the race even when the constraint prevented the damage, which is the whole
    distinction the first draft could not see.

    A `sqlite3.OperationalError` ("database is locked") would also fail these
    assertions, and that is a deliberate trade rather than an oversight. It is
    not a correctness failure -- the loser of a lock race retrying is the right
    outcome -- but at this scale, twenty single-statement inserts against a local
    file with a five-second busy timeout, it must not happen. If it ever does,
    failing and naming it is more useful than a pass that hides which of the two
    occurred, and the error text is included in the assertion message to say
    which.
    """
    swap_ids = [f"s_race_{index}" for index in range(20)]
    path = _fresh_db(tmp_path, swap_ids=swap_ids)

    start = threading.Barrier(2)
    issued: list[int] = []
    errors: list[BaseException] = []
    lock = threading.Lock()

    def worker(mine):
        conn = connect_db(path)
        conn.executescript(SCHEMA)
        # 5s is sqlite3's default busy timeout; named here so the reader knows
        # the loser waits for the winner's commit rather than failing instantly.
        conn.execute("PRAGMA busy_timeout = 5000")
        start.wait(timeout=30)
        for swap_id in mine:
            try:
                tag = allocate_destination_tag(conn, ACCOUNT_ZERO, swap_id)
                conn.commit()
                with lock:
                    issued.append(tag)
            except BaseException as exc:  # noqa: BLE001 -- recorded, not swallowed; what escapes is the subject
                conn.rollback()
                with lock:
                    errors.append(exc)
        conn.close()

    threads = [
        threading.Thread(target=worker, args=(swap_ids[0::2],), name="allocator-A"),
        threading.Thread(target=worker, args=(swap_ids[1::2],), name="allocator-B"),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
        assert not thread.is_alive(), "an allocator thread did not finish; test setup failed"

    # NOTHING may have raised. An XRPTagAllocationError carrying "tag collision"
    # is the race arriving through the constraint -- the damage prevented, the
    # swap left without a deposit identifier -- and it is the exact failure the
    # single-statement allocation exists to make impossible. It is named
    # separately so a regression says which of the two happened.
    collisions = [exc for exc in errors if "tag collision" in str(exc)]
    assert not collisions, (
        f"{len(collisions)} of {len(swap_ids)} allocations computed a tag a sibling thread already owned. "
        f"The constraint held, so no customer got a duplicate -- but those swaps have NO tag: "
        f"{[str(exc)[:80] for exc in collisions]}"
    )
    assert not errors, f"an allocation failed: {[f'{type(e).__name__}: {str(e)[:120]}' for e in errors]}"

    # Every swap got a tag, and the tags are the unbroken run 1..N. A GAP is the
    # signature of a lost race even where the constraint prevented the damage,
    # which is what the first draft of this test could not see.
    assert sorted(issued) == list(range(FIRST_ALLOCATABLE_TAG, FIRST_ALLOCATABLE_TAG + len(swap_ids))), (
        f"expected the unbroken run {FIRST_ALLOCATABLE_TAG}..{FIRST_ALLOCATABLE_TAG + len(swap_ids) - 1}, "
        f"got {sorted(issued)}"
    )

    conn = connect_db(path)
    rows = conn.execute("SELECT account, destination_tag, swap_id FROM xrp_destination_tags").fetchall()
    conn.close()
    tags_in_table = [row["destination_tag"] for row in rows]
    assert len(tags_in_table) == len(set(tags_in_table)), f"duplicate rows in the table: {sorted(tags_in_table)}"
    assert sorted(tags_in_table) == sorted(issued)
    assert len({row["swap_id"] for row in rows}) == len(rows) == len(swap_ids)


# ---------------------------------------------------------------------------
# Reuse: the decision, enforced by the database
# ---------------------------------------------------------------------------


def test_an_allocated_tag_cannot_be_deleted(db):
    """Never-reused, as something the database forbids rather than a convention.

    Allocation takes MAX(destination_tag) + 1, so a deleted row would let the
    sequence repeat a number already handed to a customer -- and a payment
    carrying it, arriving late from a saved address-book entry or a retried
    withdrawal, would be credited to a stranger's swap. The trigger is what
    makes that impossible rather than merely discouraged.
    """
    allocate_destination_tag(db, ACCOUNT_ZERO, "s_one")
    with pytest.raises(sqlite3.IntegrityError) as caught:
        db.execute("DELETE FROM xrp_destination_tags")
    assert "never deleted" in str(caught.value)
    assert db.execute("SELECT COUNT(*) AS n FROM xrp_destination_tags").fetchone()["n"] == 1


def test_an_allocated_tag_cannot_be_repointed_at_another_swap(db):
    allocate_destination_tag(db, ACCOUNT_ZERO, "s_one")
    for column, value in (("swap_id", "s_two"), ("destination_tag", 77), ("account", ACCOUNT_ONE)):
        with pytest.raises(sqlite3.IntegrityError) as caught:
            db.execute(f"UPDATE xrp_destination_tags SET {column} = ?", (value,))  # noqa: S608 -- checked: `column` is one of three literals written on the line above, never input
        assert "immutable once allocated" in str(caught.value)
    assert swap_id_for_tag(db, ACCOUNT_ZERO, FIRST_ALLOCATABLE_TAG) == "s_one"


def test_a_completed_swap_does_not_release_its_tag(db):
    """The reuse decision measured through the lifecycle it is about.

    A swap that completes is exactly when a naive implementation would free the
    tag. Nothing here frees it: the next allocation is 2, not 1, and the
    completed swap still owns 1 -- so a late payment carrying 1 resolves to the
    swap it was meant for.
    """
    first = allocate_destination_tag(db, ACCOUNT_ZERO, "s_one")
    db.execute("UPDATE swaps SET status = 'completed' WHERE id = 's_one'")
    db.commit()
    second = allocate_destination_tag(db, ACCOUNT_ZERO, "s_two")
    assert second == first + 1
    assert swap_id_for_tag(db, ACCOUNT_ZERO, first) == "s_one"


# ---------------------------------------------------------------------------
# Range
# ---------------------------------------------------------------------------


def test_the_tag_space_is_reported_exhausted_rather_than_overflowing(db):
    """The last tag allocates; the one after it refuses with a readable message.

    The refusal comes from the CHECK constraint, not from a Python comparison.
    That is deliberate: a Python guard would be a second copy of the bound, and
    the copy that drifts is the one nobody opens (rule 8).
    """
    db.execute(
        "INSERT INTO xrp_destination_tags (account, destination_tag, swap_id, allocated_at) VALUES (?, ?, ?, ?)",
        (ACCOUNT_ZERO, MAX_DESTINATION_TAG, "s_one", NOW),
    )
    db.commit()
    with pytest.raises(XRPTagAllocationError) as caught:
        allocate_destination_tag(db, ACCOUNT_ZERO, "s_two")
    message = str(caught.value)
    assert "EXHAUSTED" in message
    assert str(MAX_DESTINATION_TAG) in message
    assert "Reclaiming old tags is NOT the remedy" in message
    assert db.execute("SELECT COUNT(*) AS n FROM xrp_destination_tags").fetchone()["n"] == 1


def test_the_database_refuses_an_out_of_range_tag_written_by_hand(db):
    for bad in (RESERVED_DESTINATION_TAG, MAX_DESTINATION_TAG + 1, -1):
        with pytest.raises(sqlite3.IntegrityError) as caught:
            db.execute(
                "INSERT INTO xrp_destination_tags (account, destination_tag, swap_id, allocated_at) "
                "VALUES (?, ?, ?, ?)",
                (ACCOUNT_ZERO, bad, "s_one", NOW),
            )
        assert "xrp_tag_is_allocatable" in str(caught.value)


def test_validate_destination_tag_asks_two_different_questions():
    """Allocatable and receivable are not the same predicate, and 0 is the difference."""
    assert validate_destination_tag(RESERVED_DESTINATION_TAG, allocatable=False) == 0
    with pytest.raises(XRPTagError) as caught:
        validate_destination_tag(RESERVED_DESTINATION_TAG, allocatable=True)
    assert "never allocated" in str(caught.value)
    for allocatable in (True, False):
        with pytest.raises(XRPTagError):
            validate_destination_tag(MAX_DESTINATION_TAG + 1, allocatable=allocatable)
        with pytest.raises(XRPTagError):
            validate_destination_tag(-1, allocatable=allocatable)


def test_a_bool_is_not_a_destination_tag():
    """`True == 1` in Python, so a stray bool would become a tag a real swap owns.

    chains/xrp_payments.py::_classify() rejects bool at the READING end and has
    its own test pinning that. This is the same rule at the allocating end, and
    the two are deliberately both present -- a value that reaches one of them
    without the other would be coerced silently.
    """
    for value in (True, False):
        with pytest.raises(XRPTagError) as caught:
            validate_destination_tag(value, allocatable=False)
        assert "not an int" in str(caught.value)


# ---------------------------------------------------------------------------
# The range constant, measured against the reference implementation
# ---------------------------------------------------------------------------


def test_the_max_tag_constant_matches_the_reference_serializer():
    """MAX_DESTINATION_TAG is pinned to what xrpl-py's codec will actually encode.

    WHY THIS TEST EXISTS. The bound could have been written from memory as
    "DestinationTag is a uint32", and that sentence is the kind of thing rule 17
    forbids stating in the register of a measurement. So the constant is pinned
    to the reference implementation's own serializer: the value below it
    encodes, the value above it raises.

    WHAT IT DOES NOT PROVE. xrpl-py is a library, not a ledger. This shows the
    constant agrees with the encoder every XRPL client uses; it does not show a
    rippled server accepted a transaction at the bound, and nothing in this
    container can -- the suite opens no sockets. The end-to-end confirmation
    that the deposit path reads a tag at all came from the operator's host on
    2026-09-26 with tag 4242, which is far from either boundary.

    importorskip rather than a hard dependency, matching the pattern in
    tests/test_xrp_send_tagged.py: xrpl-py is optional in this tree and a
    signing library must not be required to collect the test suite. The skip
    reason names what goes UNCHECKED when it is absent, not just the missing
    module.
    """
    pytest.importorskip(
        "xrpl",
        reason="xrpl-py absent: MAX_DESTINATION_TAG is NOT checked against the reference serializer in "
        "this run, so the bound is unpinned prose here",
    )
    # Imported HERE and not at the top, with PLC0415 suppressed for the reason
    # rule 12 allows a noqa at all: a module-level import would make xrpl-py
    # mandatory to COLLECT this suite, which is the opposite of the
    # importorskip above. Same pattern and same reason as xrp_send_tagged.py.
    from xrpl.core.binarycodec.exceptions import XRPLBinaryCodecException  # noqa: PLC0415 -- optional dep
    from xrpl.core.binarycodec.types.uint32 import UInt32  # noqa: PLC0415 -- optional dep

    UInt32.from_value(MAX_DESTINATION_TAG)
    UInt32.from_value(FIRST_ALLOCATABLE_TAG)
    for beyond in (MAX_DESTINATION_TAG + 1, -1):
        with pytest.raises((OverflowError, XRPLBinaryCodecException)):
            UInt32.from_value(beyond)


def test_the_reference_serializer_carries_tag_zero():
    """Tag 0 round-trips as PRESENT, which is why it is reserved rather than used.

    This is the measurement the reservation rests on. If 0 were indistinguishable
    from an absent field there would be nothing to reserve; because it decodes
    back as `DestinationTag: 0`, a payment carrying it is a payment carrying a
    tag, and whichever swap owned tag 0 would be credited with it.

    It also pins the other half: chains/xrp_payments.py::_classify() tests
    `tag is None` rather than truthiness, and this is the wire-level reason that
    distinction is not pedantry.
    """
    pytest.importorskip(
        "xrpl",
        reason="xrpl-py absent: tag 0's wire presence is NOT measured in this run, so the reservation's "
        "justification is unverified here",
    )
    # Lazy for the same reason as the test above: see its comment.
    from xrpl.core.binarycodec import decode, encode  # noqa: PLC0415 -- optional dep
    from xrpl.models.transactions import Payment  # noqa: PLC0415 -- optional dep

    for tag in (RESERVED_DESTINATION_TAG, FIRST_ALLOCATABLE_TAG, MAX_DESTINATION_TAG):
        payment = Payment(account=ACCOUNT_ZERO, destination=ACCOUNT_ONE, amount="10", destination_tag=tag)
        assert decode(encode(payment.to_xrpl()))["DestinationTag"] == tag


# ---------------------------------------------------------------------------
# The account a sequence is numbered under
# ---------------------------------------------------------------------------


def test_a_malformed_account_is_refused_before_anything_is_written(db):
    """A typo would open a second sequence the PRIMARY KEY cannot see.

    `account` is a TEXT column, so "rrrrrrrrrrrrrrrrrrrrrhoLvTq" inserts
    happily and starts numbering from 1 again -- handing out tags that duplicate
    the real account's, with the constraint unable to object because the rows
    differ in the account column. The checksum check is what closes that, and it
    costs no network call.
    """
    for bad in ("", "   ", "not-an-address", "rrrrrrrrrrrrrrrrrrrrrhoLvTq"):
        with pytest.raises(XRPTagAllocationError):
            allocate_destination_tag(db, bad, "s_one")
    assert db.execute("SELECT COUNT(*) AS n FROM xrp_destination_tags").fetchone()["n"] == 0


def test_an_x_address_is_refused_because_it_already_carries_a_tag(db):
    with pytest.raises(XRPTagAllocationError) as caught:
        allocate_destination_tag(db, X_ADDRESS, "s_one")
    message = str(caught.value)
    assert "X-address" in message
    assert "two tags that disagree" in message
    assert db.execute("SELECT COUNT(*) AS n FROM xrp_destination_tags").fetchone()["n"] == 0


def test_validate_account_returns_the_stripped_address():
    assert validate_account(f"  {ACCOUNT_ZERO}  ") == ACCOUNT_ZERO


def test_a_tag_for_a_swap_that_does_not_exist_is_refused(db):
    """The FOREIGN KEY, which is why the swap row must be inserted first.

    The `db` fixture ran executescript(SCHEMA), which sets
    `PRAGMA foreign_keys=ON`. That pragma is per-connection and defaults OFF, so
    this test also records WHICH connections enforce it -- asserted below rather
    than described, because "the pragma is in the schema" is not evidence it was
    on.
    """
    assert db.execute("PRAGMA foreign_keys").fetchone()["foreign_keys"] == 1
    with pytest.raises(XRPTagAllocationError) as caught:
        allocate_destination_tag(db, ACCOUNT_ZERO, "s_does_not_exist")
    assert "no swap row" in str(caught.value)
    with pytest.raises(XRPTagAllocationError) as caught:
        allocate_destination_tag(db, ACCOUNT_ZERO, "")
    assert "no swap_id was given" in str(caught.value)


# ---------------------------------------------------------------------------
# What an operator reads (rule 14)
# ---------------------------------------------------------------------------


def test_the_deposit_instruction_names_both_halves_as_required():
    line = describe_deposit_instruction(ACCOUNT_ZERO, 4242)
    assert ACCOUNT_ZERO in line
    assert "DestinationTag=4242" in line
    assert "BOTH are required" in line
    # No network is claimed. This module never learns one, and asserting the
    # absence is what stops a future edit from hardcoding "mainnet" into the
    # line a customer reads.
    assert "mainnet" not in line
    assert "testnet" not in line


def test_an_empty_summary_says_none_rather_than_printing_nothing(db):
    """Rule 14: `(none)` is a result; a blank gap is ambiguous with a broken query."""
    empty = allocation_summary(db, ACCOUNT_ZERO)
    assert "(none)" in empty
    assert f"next would be {FIRST_ALLOCATABLE_TAG}" in empty
    assert "reserved" in empty

    allocate_destination_tag(db, ACCOUNT_ZERO, "s_one")
    allocate_destination_tag(db, ACCOUNT_ZERO, "s_two")
    filled = allocation_summary(db, ACCOUNT_ZERO)
    assert "2 allocated" in filled
    assert "highest=2" in filled
    assert "next would be 3" in filled
    assert "never reused" in filled
    assert str(MAX_DESTINATION_TAG - 2) in filled
