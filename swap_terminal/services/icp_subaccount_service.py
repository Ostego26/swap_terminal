"""Allocate the per-swap ICP deposit subaccount, in SQL, uniquely.

Role: submodule (allocation; the decision is one SQL statement and the functions
      around it validate and explain)
Reads: icp_deposit_subaccounts, and the owner principal it is handed
Writes: icp_deposit_subaccounts. Does NOT commit -- the caller owns the boundary.
Can move funds: no. It issues a deposit address component; nothing here signs,
      sends, or reads a balance.
Mainnet-safe: yes

WHY THIS EXISTS SEPARATELY FROM services/xrp_tag_service.py, which answers the
identical question for XRP and Solana. Read both before changing either -- rule 8
says that where two implementations genuinely differ, the difference belongs in a
comment at BOTH sites naming the other, and db.py's ICP_DEPOSIT_SUBACCOUNT_SCHEMA
carries the other half of this one.

The difference is who sets the label. An XRP destination tag and a Solana memo are
fields the PAYER must attach, so those services spend most of their length on what
happens when a payer omits or mistypes one. An ICP subaccount is 32 bytes the
RECEIVER chooses: the customer is handed an ordinary 64-hex account identifier and
needs to know nothing at all. There is no untagged-payment case here, because
there is no tag to forget. That is the whole reason ICP deposit attribution is
cheaper than every other chain in this system.

WHY SQL ALLOCATES AND PYTHON DOES NOT (rule 20). The uniqueness of a deposit
address is not a property to be careful about, it is a constraint:
icp_deposit_subaccounts has PRIMARY KEY (owner, subaccount_index), a UNIQUE index
on swap_id, and triggers refusing deletes and re-points. A Python loop reading the
max and adding one is the same logic in a place only its author can inspect, and
two of them racing both compute the same next index.

THE INDEX STARTS AT 1 BECAUSE 0 IS THE DESK'S OWN ACCOUNT, measured 2026-10-06
against the real ICP ledger on the local replica:

    icrc1_balance_of(record { owner = principal "ybr6p-...-cqe" })
      -> 100_000_000_000 : nat

no subaccount given, and that is the desk's entire inventory. Index 0 is the
default subaccount, so allocating it to a swap would publish the desk's own
holding account as a customer deposit address -- arriving payments would land
indistinguishably among the desk's funds and the watcher would read the inventory
as the deposit. The CHECK constraint in the schema is what actually stops it; this
module's FIRST_ALLOCATABLE_INDEX is the same number on the Python side, and the
test suite asserts the two agree rather than trusting that they do.
"""

from __future__ import annotations

import sqlite3

# ABSOLUTE, not `from ..chains...`. This repository puts swap_terminal/ on sys.path
# (the sys.path.insert idiom every entry point uses), so `services` is a TOP-LEVEL
# package and a two-dot relative import raises "attempted relative import beyond
# top-level package" -- 49 collection errors the moment services/swap_service.py
# imported this module. Every sibling here reaches chains/ the same way: see
# swap_service's `from chains.registry import ...`.
from chains.icp_account import PrincipalRefused, principal_to_bytes

from .helpers import utc_now_iso

#: The lowest index this service will ever issue. 0 is the desk's own account --
#: see the module docstring for the ledger reading that establishes it, and
#: db.py's CONSTRAINT icp_subaccount_is_allocatable for the half that enforces it.
FIRST_ALLOCATABLE_INDEX = 1

#: One statement, so there is no window between reading the maximum and inserting
#: the successor. RETURNING gives back the computed value rather than requiring a
#: second SELECT, which would be exactly the window this closes. Mirrors
#: services/xrp_tag_service._ALLOCATE_SQL, deliberately, down to the shape.
_ALLOCATE_SQL = """
INSERT INTO icp_deposit_subaccounts (owner, subaccount_index, swap_id, allocated_at)
SELECT :owner, COALESCE(MAX(subaccount_index), :below_first) + 1, :swap_id, :allocated_at
  FROM icp_deposit_subaccounts
 WHERE owner = :owner
RETURNING subaccount_index
"""


class ICPSubaccountAllocationError(RuntimeError):
    """Nothing was allocated, and no deposit address may be shown.

    A caller that catches this must not display an address: there is no
    subaccount behind it, so any address it derived would be one this database has
    no record of and no watcher will ever poll.
    """


def allocate_subaccount_index(db, owner: str, swap_id: str) -> int:
    """Allocate the next subaccount index for `swap_id` under `owner`. THE decision.

    NOT IDEMPOTENT, and that is the same choice xrp_tag_service made for the same
    reason: a second call for one swap raises rather than returning what the first
    issued. Returning it would make a double-allocating caller look correct, and
    the bug that hides is the one where two code paths both allocate and only one
    records the result. Use subaccount_index_for_swap() to read an existing one.

    DOES NOT COMMIT. The caller owns the transaction, which is what lets the swap
    row and its subaccount be written atomically -- a crash between them would
    otherwise leave a swap whose deposit address was published and never recorded.
    """
    try:
        owner = principal_to_text_checked(owner)
    except PrincipalRefused as error:
        raise ICPSubaccountAllocationError(
            f"the owner {owner!r} is not a canonical textual principal, so nothing was "
            f"allocated: {error}"
        ) from error
    if not swap_id:
        raise ICPSubaccountAllocationError(
            "no swap_id was given, so nothing was allocated. A subaccount with no swap "
            "behind it takes an index out of the sequence and matches no deposit."
        )
    try:
        row = db.execute(
            _ALLOCATE_SQL,
            {
                "owner": owner,
                "below_first": FIRST_ALLOCATABLE_INDEX - 1,
                "swap_id": swap_id,
                "allocated_at": utc_now_iso(),
            },
        ).fetchone()
    except sqlite3.IntegrityError as error:
        # NARROW ON PURPOSE (rule 12's BLE001 note). Only a constraint failure has
        # a meaning this function can translate -- the UNIQUE index on swap_id
        # firing means this swap already holds a subaccount. An OperationalError (a
        # locked database, a missing table) propagates untouched: it says the
        # allocation did not happen for a reason that is about the database rather
        # than about this swap, and reporting it as an allocation problem sends a
        # reader to the wrong place. Either way nothing is swallowed.
        # WHICH CONSTRAINT IT WAS, because the two have opposite remedies and the
        # first version of this message guessed. It said "the likeliest cause is that
        # this swap already has one" for every IntegrityError -- which sent a reader
        # looking for a duplicate allocation when the actual failure was a FOREIGN KEY
        # violation, i.e. the swap row does not exist yet. Found by a test that
        # allocated for an uncreated swap and read what came back (rule 14: the
        # message is what an operator acts on).
        text = str(error)
        if "FOREIGN KEY" in text.upper():
            explanation = (
                f"swap {swap_id} does not exist in `swaps`, and icp_deposit_subaccounts has a "
                f"FOREIGN KEY to it. Allocation must happen AFTER the swap row is inserted and "
                f"inside the same transaction -- see services/swap_service's "
                f"DB_ALLOCATED_DEPOSIT_ASSETS for why that ordering is forced"
            )
        elif "UNIQUE" in text.upper():
            explanation = (
                f"swap {swap_id} already holds a subaccount. Read it with "
                f"subaccount_index_for_swap() rather than allocating again: two subaccounts for "
                f"one swap means two published deposit addresses, and a payment to the one nobody "
                f"polls looks exactly like a customer who never paid"
            )
        else:
            explanation = (
                "a constraint this function does not recognize was violated, so the remedy is not "
                "inferable from here -- read the sqlite message"
            )
        raise ICPSubaccountAllocationError(
            f"the allocating INSERT for swap {swap_id} under {owner} violated a constraint, so "
            f"NO subaccount was issued: {error}. {explanation}."
        ) from error

    if row is None:
        raise ICPSubaccountAllocationError(
            f"the allocating INSERT for swap {swap_id} under {owner} returned no row. NOT treated "
            f"as success: RETURNING gives back the computed value, so an empty result means the "
            f"statement did not insert, and inventing an index here would hand a customer a "
            f"deposit address this database has no record of."
        )
    # dict, NOT sqlite3.Row. db.connect_db installs a dict_factory, which
    # services/xrp_tag_service.py already accounts for -- I wrote sqlite3.Row here
    # first and every allocating test failed with KeyError, which is what "mirror
    # the sibling" is worth when you mirror the shape without reading it. The
    # fallback to row[0] stays for a caller that passes a bare connection.
    index = row["subaccount_index"] if isinstance(row, dict) else row[0]
    if index < FIRST_ALLOCATABLE_INDEX:
        # Validated on the way OUT as well as constrained on the way in. The CHECK
        # is the guarantee; this catches a future edit that relaxes it, and it
        # costs one comparison against publishing the desk's own account.
        raise ICPSubaccountAllocationError(
            f"the database returned subaccount index {index}, below the first allocatable "
            f"index {FIRST_ALLOCATABLE_INDEX}. Index 0 is the desk's OWN account: publishing it "
            f"as a deposit address would mix customer payments into desk inventory."
        )
    return index


def principal_to_text_checked(owner: str) -> str:
    """The owner principal, accepted only in its canonical textual form.

    One spelling of an identity exists in this system, which is what
    chains/icp_account.principal_to_bytes enforces by re-encoding -- so an
    uppercase or ungrouped variant that names the same identity is refused rather
    than stored as a second row. Two stored spellings of one owner would give
    MAX(subaccount_index) two separate sequences and hand two swaps the same index.
    """
    principal_to_bytes(owner)
    return owner


def subaccount_index_for_swap(db, swap_id: str) -> int | None:
    """The index already allocated to `swap_id`, or None.

    None means no allocation, which is a different thing from index 0 -- there is
    no index 0 here. A caller must not treat a falsy return as an index.
    """
    row = db.execute(
        "SELECT subaccount_index FROM icp_deposit_subaccounts WHERE swap_id = ?",
        (swap_id,),
    ).fetchone()
    if row is None:
        return None
    return row["subaccount_index"] if isinstance(row, dict) else row[0]


def swap_id_for_subaccount_index(db, owner: str, index: int) -> str | None:
    """Which swap owns (owner, index), or None. The reverse lookup a watcher needs.

    This is the direction that makes the whole mechanism work: the watcher sees a
    balance appear at a derived address, converts it back to the index it polled,
    and asks whose it is.
    """
    row = db.execute(
        "SELECT swap_id FROM icp_deposit_subaccounts WHERE owner = ? AND subaccount_index = ?",
        (principal_to_text_checked(owner), index),
    ).fetchone()
    if row is None:
        return None
    return row["swap_id"] if isinstance(row, dict) else row[0]
