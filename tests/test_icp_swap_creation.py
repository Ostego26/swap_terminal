"""The ICP deposit address at swap creation: allocated in SQL, derived from the index.

Role: tests (read-only except tmp_path)
Reads: services/swap_service's db-allocated deposit path
Writes: a throwaway database under pytest's tmp_path
Can move funds: no
Mainnet-safe: yes -- no network, no adapter that can send, no replica.

WHY A THIRD SHAPE EXISTS AT ALL, since that is what these tests are about.
services/swap_service.deposit_account() knows two: BY ADDRESS (BTC, LTC, GRC --
get_new_address() derives it, so it is known before anything is written) and BY TAG
(XRP, SOL -- the account is configuration and the tag is allocated after the swap
row, because it has a FOREIGN KEY to it).

ICP is neither. Its address IS a function of an allocated index --
account_identifier(owner, subaccount) -- and icp_deposit_subaccounts carries the same
FOREIGN KEY. So the index cannot be allocated before the swap row exists, and the
address cannot be computed before the index. That ordering is the whole subject here,
and getting it wrong is silent in both directions: allocate too early and the
constraint fails; commit too early and a customer is shown
PENDING_SUBACCOUNT_ALLOCATION as something to pay.
"""

from __future__ import annotations

import pytest
from db import SCHEMA, connect_db
from services.icp_subaccount_service import ICPSubaccountAllocationError, subaccount_index_for_swap
from services.swap_service import (
    DB_ALLOCATED_DEPOSIT_ASSETS,
    DEPOSIT_ADDRESS_PENDING_ALLOCATION,
    allocate_db_deposit_address,
)
from test_xrp_destination_tags import _seed_swaps

from swap_terminal.chains.icp_account import (
    account_identifier,
    is_account_identifier,
    principal_to_text,
    subaccount_from_index,
)

OWNER = principal_to_text(bytes.fromhex("00000000000000020101"))
SWAP_IDS = ("s_one", "s_two", "s_three")


class FakeICPAdapter:
    """Only what allocate_db_deposit_address touches: the principal and two pure derivations.

    Not the real ICPAdapter, because that one would need a transport -- and the point
    of extracting this decision was that it can be called with seeded inputs (rule 10).
    The methods are the real ones from chains/icp_account, so the addresses are real.
    """

    owner_principal = OWNER

    def deposit_address(self, index: int) -> str:
        if index < 1:
            raise ValueError("index 0 is the desk's own account")
        return account_identifier(OWNER, subaccount_from_index(index))

    def validate_address(self, address: str) -> bool:
        return is_account_identifier(address)


@pytest.fixture
def db(tmp_path):
    conn = connect_db(str(tmp_path / "swap_terminal_icp_creation.db"), create=True)
    conn.executescript(SCHEMA)
    _seed_swaps(conn, SWAP_IDS)
    yield conn
    conn.close()


def test_ICP_is_the_db_allocated_asset_and_nothing_else_is():
    """A new entry here changes the creation path for that asset, so it is pinned."""
    assert frozenset({"ICP"}) == DB_ALLOCATED_DEPOSIT_ASSETS


def test_allocation_and_derivation_agree(db):
    adapter = FakeICPAdapter()
    address = allocate_db_deposit_address(db, {"ICP": adapter}, "ICP", "s_one")
    index = subaccount_index_for_swap(db, "s_one")
    assert index == 1, "the first allocation is 1, never 0 -- 0 is the desk's own account"
    assert address == adapter.deposit_address(index)
    assert address != account_identifier(OWNER), "the desk's own account must never be published"


def test_each_swap_gets_a_different_address(db):
    adapter = FakeICPAdapter()
    addresses = {allocate_db_deposit_address(db, {"ICP": adapter}, "ICP", s) for s in SWAP_IDS}
    assert len(addresses) == len(SWAP_IDS)


def test_a_second_allocation_for_one_swap_is_refused(db):
    """Two deposit addresses for one swap means a payment to the one nobody polls."""
    adapter = FakeICPAdapter()
    allocate_db_deposit_address(db, {"ICP": adapter}, "ICP", "s_one")
    with pytest.raises(Exception, match="already"):
        allocate_db_deposit_address(db, {"ICP": adapter}, "ICP", "s_one")


def test_allocating_for_a_swap_that_does_not_exist_is_refused_by_the_foreign_key(db):
    """WHY THE ORDER IS INSERT-THEN-ALLOCATE, demonstrated rather than asserted in prose.

    icp_deposit_subaccounts has a FOREIGN KEY to swaps, so allocating before the swap
    row exists cannot work -- which is what forces the placeholder-then-UPDATE shape in
    create_swap.
    """
    adapter = FakeICPAdapter()
    with pytest.raises(ICPSubaccountAllocationError, match="FOREIGN KEY") as caught:
        allocate_db_deposit_address(db, {"ICP": adapter}, "ICP", "s_not_created_yet")
    # THE MESSAGE MUST NAME THE RIGHT REMEDY, not merely fail. Before this test the
    # handler said "the likeliest cause is that this swap already has one" for EVERY
    # IntegrityError, which sends a reader hunting a duplicate allocation when the
    # swap row simply does not exist yet.
    assert "does not exist" in str(caught.value)
    assert "already holds" not in str(caught.value)


def test_a_non_db_allocated_asset_is_refused_rather_than_handled(db):
    """GRC must never reach this path; deposit_account() owns it."""
    with pytest.raises(ValueError, match="not a db-allocated"):
        allocate_db_deposit_address(db, {"ICP": FakeICPAdapter()}, "GRC", "s_one")


def test_an_address_the_adapter_itself_rejects_refuses_the_swap(db):
    """A customer handed an address the adapter rejects pays somewhere nothing watches."""

    class BadAdapter(FakeICPAdapter):
        def deposit_address(self, index: int) -> str:
            return "not-an-account-identifier"

    with pytest.raises(ValueError, match="does not validate"):
        allocate_db_deposit_address(db, {"ICP": BadAdapter()}, "ICP", "s_one")


def test_the_placeholder_is_not_address_shaped():
    """It sits in deposit_address between the INSERT and the UPDATE.

    If a future edit ever let it escape the transaction, nothing may send to it and
    nothing may publish it as payable -- so the ICP adapter's own validator must reject
    it. An empty string would satisfy NOT NULL and read as a missing value, which is the
    ambiguity rule 14 is about.
    """
    assert DEPOSIT_ADDRESS_PENDING_ALLOCATION
    assert not is_account_identifier(DEPOSIT_ADDRESS_PENDING_ALLOCATION)
