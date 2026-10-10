"""services/icp_subaccount_service: one deposit address per swap, enforced by SQL.

Role: tests (read-only except tmp_path)
Reads: the real SCHEMA and the real allocator
Writes: a throwaway database under pytest's tmp_path
Can move funds: no
Mainnet-safe: yes

BEHAVIORAL, not textual (CLAUDE.md's verification principle). Every assertion
seeds real rows into the real tables through `executescript(SCHEMA)` -- the same
script every worker runs at the top of every cycle -- runs the real allocator, and
asserts on the rows that exist or do not. Nothing here greps SQL text.

THE FIXTURE IS IMPORTED FROM tests/test_xrp_destination_tags.py rather than
rewritten. `_seed_swaps` already inserts a quote and swaps so the FOREIGN KEY has
a target, and a second copy would be rule 8's defect in a test directory: the two
would agree today and drift the first time the swaps table gains a NOT NULL
column. This module already learned that the hard way -- the first smoke run of
this allocator died on `NOT NULL constraint failed: swaps.quote_id` because it
tried to insert a swap by hand.

WHAT THE SUBJECT ACTUALLY IS. The index is not a label the payer attaches; it is 32
bytes the DESK chooses, and the customer is handed an ordinary 64-hex account
identifier derived from (principal, subaccount). So the failure modes are not
"payer forgot the tag" -- there is nothing to forget. They are: two swaps given one
index, one swap given two, or index 0 handed out, which is the desk's own account.
"""

from __future__ import annotations

import sqlite3

import pytest
from db import SCHEMA, connect_db
from test_xrp_destination_tags import _seed_swaps

from swap_terminal.chains.icp_account import account_identifier, principal_to_text, subaccount_from_index
from swap_terminal.services.icp_subaccount_service import (
    FIRST_ALLOCATABLE_INDEX,
    ICPSubaccountAllocationError,
    allocate_subaccount_index,
    subaccount_index_for_swap,
    swap_id_for_subaccount_index,
)

#: The desk's owner principal, DERIVED rather than written out -- no address
#: literals (the suite has a gate on their count, currently at its ceiling).
OWNER = principal_to_text(bytes.fromhex("00000000000000020101"))

#: A second, distinct principal, so "the sequence is per owner" can be asserted
#: rather than assumed.
OTHER_OWNER = principal_to_text(bytes.fromhex("00000000000000010101"))

SWAP_IDS = ("s_one", "s_two", "s_three")


@pytest.fixture
def db(tmp_path):
    """A real database file with the real SCHEMA and three swaps seeded."""
    conn = connect_db(str(tmp_path / "swap_terminal_icp_subaccounts.db"), create=True)
    conn.executescript(SCHEMA)
    _seed_swaps(conn, SWAP_IDS)
    yield conn
    conn.close()


def test_the_first_index_is_one_because_zero_is_the_desks_own_account(db):
    """The skip is the assertion, and the reason is measured rather than supposed.

    Subaccount 0 is the DEFAULT subaccount, which is the desk's own account.
    Measured 2026-10-06 against the real ICP ledger on the local replica:
    icrc1_balance_of with no subaccount returned 100_000_000_000 e8s -- the desk's
    whole inventory. Handing index 0 to a swap would publish that account as a
    customer deposit address.

    Contrast with XRP, which reserves tag 0 on an explicitly-labeled hypothesis
    about what senders emit. This one is not a hypothesis.
    """
    assert allocate_subaccount_index(db, OWNER, "s_one") == FIRST_ALLOCATABLE_INDEX
    assert FIRST_ALLOCATABLE_INDEX == 1
    assert swap_id_for_subaccount_index(db, OWNER, 0) is None


def test_the_database_refuses_index_zero_even_by_direct_insert(db):
    """The CHECK is the guarantee, not the Python constant.

    A future caller that bypasses this service entirely must still be unable to
    allocate the desk's own account, which is what makes this a constraint rather
    than a convention (rule 20).
    """
    with pytest.raises(sqlite3.IntegrityError, match="icp_subaccount_is_allocatable"):
        db.execute(
            "INSERT INTO icp_deposit_subaccounts (owner, subaccount_index, swap_id, allocated_at)"
            " VALUES (?, 0, 's_one', 't')",
            (OWNER,),
        )
    assert subaccount_index_for_swap(db, "s_one") is None


def test_consecutive_allocations_are_distinct_and_increasing(db):
    indexes = [allocate_subaccount_index(db, OWNER, s) for s in SWAP_IDS]
    assert indexes == [1, 2, 3]
    assert len(set(indexes)) == len(indexes)


def test_a_second_allocation_for_one_swap_is_refused(db):
    """Two indexes for one swap means two published deposit addresses.

    A payment to the one the watcher is not polling is indistinguishable from a
    customer who never paid, so this raises rather than returning the existing
    index -- returning it would make a double-allocating caller look correct.
    """
    first = allocate_subaccount_index(db, OWNER, "s_one")
    with pytest.raises(ICPSubaccountAllocationError, match="already"):
        allocate_subaccount_index(db, OWNER, "s_one")
    assert subaccount_index_for_swap(db, "s_one") == first


def test_the_sequence_is_per_owner(db):
    """A different principal has its own sequence, because the PK is (owner, index).

    This is what makes a desk rotation safe: a new principal starts at 1 without
    colliding with anything the old one issued, since the pair is what names an
    account.
    """
    assert allocate_subaccount_index(db, OWNER, "s_one") == 1
    assert allocate_subaccount_index(db, OTHER_OWNER, "s_two") == 1
    assert swap_id_for_subaccount_index(db, OWNER, 1) == "s_one"
    assert swap_id_for_subaccount_index(db, OTHER_OWNER, 1) == "s_two"


def test_a_released_row_cannot_let_an_index_repeat(db):
    """Deletion is refused by a trigger, and the reason is in the message.

    Allocation reads MAX(subaccount_index). A deleted row lets the next allocation
    repeat an index already published as a deposit address, and a late payment to
    it would credit the wrong swap -- which on this system cannot be undone.
    """
    allocate_subaccount_index(db, OWNER, "s_one")
    with pytest.raises(sqlite3.IntegrityError, match="never deleted"):
        db.execute("DELETE FROM icp_deposit_subaccounts WHERE swap_id = 's_one'")
    assert subaccount_index_for_swap(db, "s_one") == 1


@pytest.mark.parametrize("column", ["owner", "subaccount_index", "swap_id"])
def test_an_allocated_row_cannot_be_repointed(db, column):
    """All three columns are immutable, each checked rather than one standing in.

    On ICP the address is DERIVED from (owner, subaccount), so unlike a tag the
    customer cannot be told it moved -- they already hold a 64-hex string that
    resolves to the old pair forever.
    """
    allocate_subaccount_index(db, OWNER, "s_one")
    value = OTHER_OWNER if column == "owner" else ("s_two" if column == "swap_id" else 9)
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        db.execute(
            f"UPDATE icp_deposit_subaccounts SET {column} = ? WHERE swap_id = 's_one'",  # noqa: S608 -- `column` is a parametrize value from this file's own list of three column names, not input
            (value,),
        )


def test_an_allocation_without_a_swap_id_is_refused(db):
    with pytest.raises(ICPSubaccountAllocationError, match="no swap_id"):
        allocate_subaccount_index(db, OWNER, "")


def test_a_non_canonical_owner_is_refused_rather_than_stored(db):
    """Two spellings of one principal would give MAX() two separate sequences.

    That is the failure this refusal prevents: the same desk stored twice, each
    sequence starting over, two swaps handed the same index and therefore the same
    deposit address.
    """
    for variant in (OWNER.upper(), OWNER.replace("-", "")):
        with pytest.raises(ICPSubaccountAllocationError, match="canonical"):
            allocate_subaccount_index(db, OWNER.upper() if variant is None else variant, "s_one")
    assert subaccount_index_for_swap(db, "s_one") is None


def test_each_allocated_index_yields_a_distinct_deposit_address(db):
    """The point of the whole mechanism, end to end through the real derivation.

    Allocation is only useful if distinct indexes produce distinct 64-hex account
    identifiers -- that is what gets published to a customer. Derived here through
    chains/icp_account rather than asserted as a property of the allocator, because
    the two halves have to agree and this is where they meet.
    """
    addresses = {
        account_identifier(OWNER, subaccount_from_index(allocate_subaccount_index(db, OWNER, s)))
        for s in SWAP_IDS
    }
    assert len(addresses) == len(SWAP_IDS)
    assert account_identifier(OWNER) not in addresses, (
        "a published deposit address equals the desk's OWN default account -- index 0 leaked"
    )
