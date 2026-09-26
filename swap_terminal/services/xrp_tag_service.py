"""Allocating and resolving XRP destination tags: the per-swap deposit identifier.

Role: submodule -> function (allocate_destination_tag is the decision; the
      guarantee behind it is in db.py's SCHEMA, not here)
Reads: swap_terminal.db (xrp_destination_tags)
Writes: swap_terminal.db (xrp_destination_tags -- insert only, never update,
      never delete; the database enforces that with two triggers)
Can move funds: no. It issues no RPC, imports nothing that can sign, and
      touches no key. It DOES decide which swap a future XRP deposit will be
      attributed to, so a wrong answer here misattributes money even though
      this module never moves any. That is why the uniqueness guarantee is a
      constraint and not a check in this file.
Mainnet-safe: yes. It opens no socket and names no network.

=============================================================================
WHY THIS EXISTS AT ALL
=============================================================================

Bitcoin, Litecoin, Gridcoin and Monero answer "where should the customer pay?"
with a fresh address per swap, derived by a wallet that keeps the key.
chains/xrp.py::get_new_address() REFUSES to do that, and its refusal names this
module as the alternative:

    "The XRP Ledger attributes deposits with a DESTINATION TAG on a single
     account: an integer per swap, no new key, no funding reserve, and it is
     what every exchange on this ledger uses. What is needed is a tag allocator
     in services/ -- a change to how a swap is created, not to this adapter."

That refusal is correct and is not weakened here. Deriving an XRPL account per
swap would cost a base reserve per swap AND put a signing key per swap on this
host; a tag costs one integer.

The other end of the path already exists and is already measured.
chains/xrp_payments.py reads `DestinationTag` into the deposit event's `vout`
field -- `vout` being the integer discriminator in db.py's
UNIQUE(asset, txid, vout) -- and that was confirmed end to end against rippled
3.4.1 on testnet on 2026-09-26: a locally-signed Payment carrying tag 4242 was
credited by the adapter with `vout=4242`. So the reading half is proven on a
real ledger. This is the writing half.

=============================================================================
WHERE THE GUARANTEE LIVES, AND WHY IT IS NOT IN THIS FILE
=============================================================================

Uniqueness is the entire job. Two live swaps sharing a tag means one customer's
deposit is credited against the other customer's swap, and the payout that
follows is broadcast and final -- there is no exchange to call (see CLAUDE.md's
opening section on what is irrecoverable here).

So it is a DATABASE constraint. Not a `SELECT ... WHERE destination_tag = ?`
followed by an INSERT, which is a read that can go stale before the write. That
is not a theoretical objection in this repository: it is exactly how two payout
workers paid one swap twice on 2026-09-24. The guard was a correct SELECT that
completed before the write lock was ever contended, and
tests/test_payout_concurrency.py has the measurement. The fix there was a
claim-by-UPDATE plus a partial unique index; the fix here is the same idea in
its simpler form, because allocation has no state machine to claim against.

db.py's SCHEMA therefore carries all four guarantees, and the long comment
above the table is where the reasoning is written down:

    PRIMARY KEY (account, destination_tag)     no two rows share a tag
    UNIQUE idx_xrp_tag_one_per_swap            no swap gets two tags
    CONSTRAINT xrp_tag_is_allocatable          1..4294967295
    two BEFORE triggers that RAISE(ABORT)      no row is ever deleted or
                                               re-pointed

This module's contribution is a single SQL statement that computes the next tag
and inserts it in the same statement, so there is no window between choosing a
value and claiming it:

    INSERT INTO xrp_destination_tags (...)
    SELECT :account, COALESCE(MAX(destination_tag), 0) + 1, :swap_id, :now
      FROM xrp_destination_tags WHERE account = :account
    RETURNING destination_tag

One statement, so SQLite's single writer lock serializes the DECISION and not
merely the write. If two connections race, the loser either reads the winner's
row and takes the next number or fails with a locking error -- it cannot
duplicate, because there is no moment at which it holds a chosen-but-unclaimed
value. Measured in tests/test_xrp_destination_tags.py with real threads against
a real file.

=============================================================================
TAGS ARE NEVER REUSED. THE REASONING, AT LENGTH, BECAUSE IT IS THE INTERESTING
DECISION AND THE TEMPTING ANSWER IS WRONG
=============================================================================

The tempting answer is to free a tag when its swap completes: tags then stay
small, the table stays short, and the numbers a customer has to type stay
readable.

It is wrong, and the reason is that NOTHING ON THE XRP LEDGER EXPIRES A TAG.

A destination tag is just a number a sender puts in a field. Once a customer
has been told "pay account X with tag 7", that instruction lives wherever they
put it -- an address-book entry in their wallet, a saved withdrawal template at
their exchange, a screenshot, a support email. There is no protocol mechanism
by which it stops working, and no way for this terminal to learn that a copy of
it still exists. A payment carrying tag 7 can therefore arrive at any time:
minutes after the swap completed, or months.

If tag 7 has been reallocated by then, that payment is credited to a DIFFERENT
customer's swap, and the payout goes to that customer's address. The money is
gone and the ledger's record says the sender paid exactly what they were told
to pay. Reuse converts a late deposit -- an ordinary, recoverable support
ticket -- into an irreversible misattribution.

The three concrete ways a late payment happens, none of them exotic:

  a retried withdrawal    an exchange whose first send failed, retried against
                          the saved destination days later
  a returning customer    somebody who swapped once, kept the deposit details,
                          and used them again instead of asking for new ones
  a partial top-up        a customer who underpaid, saw the swap fail, and sent
                          the difference afterwards

So allocation reads MAX(destination_tag) over EVERY row for the account,
including rows whose swaps completed years ago, and rows are never deleted. The
sequence only ever goes up. That is not a convention this module promises: the
BEFORE DELETE trigger in db.py makes a DELETE fail, so a future writer who
tries to reclaim tags gets an error instead of a silent hazard.

WHAT NEVER REUSING COSTS, WITH THE DENOMINATOR (rule 3). The tag space is
4,294,967,295 allocatable values. At 1,000 swaps a day that lasts 11,759 years;
at 10,000 a day, 1,176 years; at a million a day, 12 years. Exhaustion is
therefore not a cost this broker pays -- but it IS a real boundary rather than
an imaginary one, so the CHECK constraint refuses the overflow and
allocate_destination_tag() turns it into a message naming the account, rather
than letting a tag of 4,294,967,296 be handed to a customer and silently fail
to serialize on the sender's side.

WHAT IS NOT DECIDED HERE. Whether an ARRIVING payment against a completed
swap's tag is refunded, credited or escalated is a fund-moving decision and
belongs to the operator (rule 16). This module only guarantees that such a
payment resolves to the swap it was actually meant for -- swap_id_for_tag() is
that lookup -- rather than to somebody else's.

=============================================================================
WHAT IS NOT WIRED, STATED PLAINLY (rule 17)
=============================================================================

This module is the mechanism. It is NOT connected to swap creation, and XRP is
not in Config.ALLOWED_PAIRS, so no swap can be created for it and nothing calls
allocate_destination_tag() in the running application today. Two things stand
between this and a working XRP deposit path, and both are named here rather
than left for someone to discover:

1. `swaps.deposit_address` IS ONE COLUMN AND AN XRP DEPOSIT INSTRUCTION IS A
   PAIR. Every other chain's instruction is a single address. XRP's is
   (account, tag), and both halves are mandatory -- a payment to the right
   account with no tag is the `deferred` case chains/xrp_payments.py reports
   and cannot credit. services/swap_service.py::create_swap() takes
   `get_new_address()`'s return value straight into that one column, so wiring
   this in means deciding how the pair is stored and how it is rendered to the
   customer. describe_deposit_instruction() below is the rendering; the storage
   decision is not made here.

2. THE DEPOSIT WATCHER WOULD CREDIT EVERY TAG TO WHICHEVER SWAP IT IS SCANNING
   FOR. Measured by reading the code, not by running it, and flagged as a
   finding rather than fixed: services/deposit_service.py::
   refresh_swap_from_chain() calls
   `adapter.find_deposits_to_address(swap["deposit_address"])` and then
   `upsert_deposit_event(db, swap["id"], ...)` for every event returned. For a
   per-address chain that is correct, because the address IS the swap. For XRP
   the account is shared, so that scan returns every tagged payment to the
   account and attributes all of them to the one swap being refreshed. The
   missing filter is `event["vout"] == the swap's own tag`, and swap_id_for_tag()
   is the function that answers it -- but adding the filter changes the one
   function that decides, for EVERY chain, that a deposit is confirmed. That is
   fund movement (rule 16), so it is reported, not done.

Neither of those is a defect in this module; both are why "the allocator
exists" is not the same sentence as "XRP deposits work".
"""

from __future__ import annotations

import logging
import sqlite3
import time

from chains.xrp_address import is_valid_classic_address, looks_like_x_address
from chains.xrp_units import (
    FIRST_ALLOCATABLE_TAG,
    MAX_DESTINATION_TAG,
    RESERVED_DESTINATION_TAG,
    XRPTagError,
    validate_destination_tag,
)
from microfortnights import format_duration

from .helpers import utc_now_iso

logger = logging.getLogger(__name__)


class XRPTagAllocationError(Exception):
    """A destination tag could not be allocated, and nothing was written.

    Every raise in this module is a refusal rather than a retryable hiccup, and
    each one says which constraint refused. That matters more than usual here:
    the alternative to refusing is handing a customer a tag that some other
    swap also owns, and the customer's money then arrives correctly addressed
    to the wrong swap. A caller that sees this exception has NOT been given a
    tag and must not display one.
    """


# ONE STATEMENT, ON PURPOSE. See "WHERE THE GUARANTEE LIVES" above: computing
# the next value and claiming it in a single INSERT is what removes the window
# a check-then-insert leaves open. Named parameters rather than positional
# because :account appears twice and positional would invite the classic
# transposition.
#
# COALESCE's default is FIRST_ALLOCATABLE_TAG - 1 so that the first allocation
# on a fresh account is FIRST_ALLOCATABLE_TAG itself -- 1, not 0. The reserved
# 0 is chains/xrp_units.RESERVED_DESTINATION_TAG and the reasoning is there.
#
# MAX over EVERY row for the account, including rows whose swaps are long
# finished. That is the never-reuse rule, and it holds only because nothing
# deletes rows -- which db.py's BEFORE DELETE trigger enforces rather than
# hopes for.
#
# RETURNING requires SQLite 3.35+. Measured 2026-09-26 in this container:
# sqlite3.sqlite_version is 3.45.1, and the clause returns the computed value.
# It is used rather than a read-back by lastrowid because a second SELECT would
# be a second statement, which is the window this statement exists to close.
_ALLOCATE_SQL = """
INSERT INTO xrp_destination_tags (account, destination_tag, swap_id, allocated_at)
SELECT :account, COALESCE(MAX(destination_tag), :below_first) + 1, :swap_id, :allocated_at
  FROM xrp_destination_tags
 WHERE account = :account
RETURNING destination_tag
"""


def validate_account(account: str) -> str:
    """The XRPL account tags are allocated against, checked before anything is written.

    TWO REFUSALS, both of which would otherwise be silent.

    A MALFORMED ACCOUNT would still insert happily -- `account` is a TEXT
    column and the PRIMARY KEY does not care what is in it. What it would do is
    open a SECOND tag sequence under the typo, numbered from 1, overlapping
    every tag already issued against the real account. Two customers would then
    be told the same tag for different swaps, and the constraint could not see
    it because the rows differ in the account column. The checksum is verified
    locally by chains/xrp_address.py against the ledger's own ACCOUNT_ZERO and
    ACCOUNT_ONE constants, so this costs no network call.

    AN X-ADDRESS IS REFUSED even though chains/xrp.py::validate_address()
    accepts one as valid, and the difference is deliberate rather than an
    inconsistency between the two. An X-address ENCODES A DESTINATION TAG
    INSIDE ITSELF. Allocating a separate tag against one produces a deposit
    instruction carrying two tags that disagree, and which one the sender's
    wallet transmits is the sender's implementation detail. validate_address()
    is answering "can this be paid?", which is a different question from "can
    this be the account a tag sequence is numbered under?".
    """
    account = (account or "").strip()
    if not account:
        raise XRPTagAllocationError(
            "no XRP account was given to allocate a destination tag against. NOTHING was written. A tag "
            "means nothing on its own -- it is only an identifier relative to the account it is sent to "
            "-- so an empty account would number a sequence under the empty string and collide with "
            "nothing."
        )
    if looks_like_x_address(account):
        raise XRPTagAllocationError(
            f"{account} is an X-address, which ENCODES A DESTINATION TAG INSIDE ITSELF. NOTHING was "
            f"written: allocating a second tag against it would produce a deposit instruction carrying "
            f"two tags that disagree, and which one reaches the ledger is the sending wallet's choice, "
            f"not ours. Allocate against the classic address instead. Note chains/xrp.py::"
            f"validate_address() accepts X-addresses on purpose -- it answers 'can this be paid?', which "
            f"is a different question from this one."
        )
    if not is_valid_classic_address(account):
        raise XRPTagAllocationError(
            f"{account} is not a valid XRPL classic address (checksum verified locally, no network call). "
            f"NOTHING was written. A typo here would not fail: `account` is a TEXT column, so it would "
            f"quietly open a SECOND tag sequence numbered from {FIRST_ALLOCATABLE_TAG}, overlapping every "
            f"tag already issued against the real account -- and the PRIMARY KEY could not object, "
            f"because the rows differ in the account column."
        )
    return account


def _explain_integrity_error(error: sqlite3.IntegrityError, account: str, swap_id: str) -> str:
    """Turn SQLite's constraint message into one that says what to do about it.

    Extracted so allocate_destination_tag() stays a single decision (rule 10)
    and so each branch can be exercised with a seeded database rather than only
    reached by accident.

    The strings matched here are SQLite's own, and they were MEASURED against
    sqlite3 3.45.1 on 2026-09-26 rather than recalled:

        "CHECK constraint failed: xrp_tag_is_allocatable"
        "UNIQUE constraint failed: xrp_destination_tags.swap_id"
        "UNIQUE constraint failed: xrp_destination_tags.account,
                                   xrp_destination_tags.destination_tag"
        "FOREIGN KEY constraint failed"

    A message this does not recognize falls through to the original text rather
    than to a guess. That is the point of the fallback: an unrecognized
    constraint failure is a constraint this function has not been taught, and
    printing SQLite's own words is more useful than a sentence invented for it.
    """
    message = str(error)
    if "xrp_tag_is_allocatable" in message:
        return (
            f"the destination tag space for account {account} is EXHAUSTED: the highest tag already "
            f"allocated is {MAX_DESTINATION_TAG}, and the next value would be {MAX_DESTINATION_TAG + 1}, "
            f"which the XRP Ledger cannot carry -- a DestinationTag is 32 bits wide. NOTHING was written "
            f"and no tag was issued. Tags are never reused here (see this module's docstring), so the "
            f"remedy is a new account, which is an operator decision. Reclaiming old tags is NOT the "
            f"remedy: it is what makes a late payment credit the wrong swap."
        )
    if "xrp_destination_tags.swap_id" in message:
        return (
            f"swap {swap_id} already has a destination tag allocated. NOTHING was written and no second "
            f"tag was issued. Two tags for one swap would mean the deposit instruction a customer sees "
            f"depends on which row was read. If you need the existing one, call "
            f"destination_tag_for_swap(); allocation is deliberately not idempotent, because silently "
            f"returning the old tag would hide a caller that allocates twice."
        )
    if "xrp_destination_tags.destination_tag" in message:
        return (
            f"tag collision on account {account}: the computed next tag is already allocated. NOTHING was "
            f"written. This should be unreachable -- the value is computed and claimed in ONE statement, "
            f"so there is no window in which another writer can take it -- so reaching it means the "
            f"single-statement allocation in this module has been split, or a row was inserted by "
            f"something other than allocate_destination_tag(). The constraint held and no customer was "
            f"given a duplicate tag, which is the constraint doing its job."
        )
    if "FOREIGN KEY" in message:
        return (
            f"there is no swap row with id {swap_id}, so no tag was allocated and NOTHING was written. "
            f"The swap must be inserted BEFORE its tag: xrp_destination_tags has a FOREIGN KEY into "
            f"swaps(id), for the same reason deposit_events does -- a tag belonging to no swap is an "
            f"identifier nothing will ever resolve. Note this only fires on a connection with "
            f"`PRAGMA foreign_keys=ON`, which db.py's SCHEMA sets and a bare connect_db() does not."
        )
    return (
        f"allocating a destination tag for swap {swap_id} on account {account} violated a constraint this "
        f"function has not been taught to explain, and NOTHING was written. SQLite's own message, "
        f"unedited, because inventing a friendlier one would describe a rule that may not be the rule "
        f"that fired: {message}"
    )


def allocate_destination_tag(db, account: str, swap_id: str) -> int:
    """Allocate the next destination tag for `swap_id` on `account`. THE decision.

    Returns the tag as an int. Raises XRPTagAllocationError, having written
    nothing, if it cannot -- and a caller that catches it must not display a
    tag, because there is no tag to display.

    NOT IDEMPOTENT, and that is a choice rather than an omission. A second call
    for the same swap raises instead of returning the tag the first call
    issued. Returning it would make a double-allocating caller look correct,
    and the shape of bug that hides is the one where two code paths both
    "allocate" and only one of them records the result. Use
    destination_tag_for_swap() to read an existing allocation.

    DOES NOT COMMIT. The caller owns the transaction boundary, exactly as
    services/swap_service.py::set_swap_status() does and for the same reason:
    it is what lets a swap row and its tag be written atomically, so a crash
    between them cannot leave a swap whose deposit instruction was shown to a
    customer but never recorded.
    """
    account = validate_account(account)
    if not swap_id:
        raise XRPTagAllocationError(
            "no swap_id was given, so nothing was allocated. A tag with no swap behind it is an "
            "identifier that will never resolve to anything -- it would sit in the table taking a number "
            "out of the sequence and matching no deposit."
        )
    started = time.monotonic()
    try:
        row = db.execute(
            _ALLOCATE_SQL,
            {
                "account": account,
                "below_first": FIRST_ALLOCATABLE_TAG - 1,
                "swap_id": swap_id,
                "allocated_at": utc_now_iso(),
            },
        ).fetchone()
    except sqlite3.IntegrityError as error:
        # NARROW ON PURPOSE (rule 12's BLE001 note). Only IntegrityError is
        # caught, because only a constraint failure has a meaning this function
        # can translate. An OperationalError -- a locked database, a missing
        # table -- propagates untouched: it means the allocation did not happen
        # for a reason that is about the database rather than about this tag,
        # and reporting it as a tag problem would send a reader to the wrong
        # place. Nothing is swallowed either way; every path here either
        # returns a tag or raises.
        raise XRPTagAllocationError(_explain_integrity_error(error, account, swap_id)) from error

    if row is None:
        raise XRPTagAllocationError(
            f"the allocating INSERT for swap {swap_id} on account {account} returned no row. NOT treated "
            f"as success and no tag was issued: RETURNING gives back the computed value, so an empty "
            f"result means the statement did not insert, and inventing a tag at that point would hand a "
            f"customer a number this database has no record of."
        )
    tag = row["destination_tag"] if isinstance(row, dict) else row[0]
    # Validated on the way OUT as well as being constrained on the way in. The
    # CHECK is the guarantee; this catches the case where a future edit relaxes
    # it, and it costs one comparison. allocatable=True because this is a tag
    # being handed out, which is the strict question -- see
    # chains/xrp_units.validate_destination_tag().
    try:
        tag = validate_destination_tag(tag, allocatable=True)
    except XRPTagError as error:
        raise XRPTagAllocationError(
            f"the database returned destination tag {tag!r} for swap {swap_id}, which this application "
            f"must not hand out: {error}. The row was written; the caller is NOT given a tag, because "
            f"displaying one the application considers invalid is worse than failing here."
        ) from error
    logger.info(
        "XRP destination tag allocated  account=%s  tag=%d  swap_id=%s  in %s  <- the customer must send "
        "to BOTH: account and tag. A payment to this account with no tag cannot be attributed and is "
        "reported as deferred, not credited. Tags are never reused (see services/xrp_tag_service.py).",
        account,
        tag,
        swap_id,
        format_duration(time.monotonic() - started),
    )
    return tag


def destination_tag_for_swap(db, swap_id: str) -> int | None:
    """The tag already allocated to a swap, or None if it has none.

    None means "no row", which for this table means no tag was ever allocated
    -- rows are never deleted, so absence is unambiguous rather than being
    indistinguishable from a reclaimed allocation. That unambiguity is a
    consequence of the never-reuse rule and is worth naming as a benefit of it.
    """
    row = db.execute(
        "SELECT destination_tag FROM xrp_destination_tags WHERE swap_id = ?",
        (swap_id,),
    ).fetchone()
    if row is None:
        return None
    return int(row["destination_tag"] if isinstance(row, dict) else row[0])


def swap_id_for_tag(db, account: str, tag) -> str | None:
    """Which swap a RECEIVED destination tag belongs to, or None.

    This is the reverse of allocation and it is the function the deposit path
    needs -- see point 2 of "WHAT IS NOT WIRED" in this module's docstring.
    chains/xrp_payments.py puts the tag in the event's `vout`, so the call is
    `swap_id_for_tag(db, account, event["vout"])`.

    allocatable=False, and the difference matters. A tag that ARRIVED may be 0:
    the ledger permits it, a real sender can set it, and it decodes back as
    present rather than absent (measured -- chains/xrp_units.py point 4). This
    terminal never ALLOCATES 0, so looking one up correctly returns None; that
    is the reservation working, not a lookup failure, and refusing to perform
    the lookup would turn a payment that needs an operator's attention into an
    exception in a scan.

    None means "no swap owns this tag", which is money that arrived and cannot
    be attributed -- the same condition
    chains/xrp_payments.py::deposit_events_from_transactions() already reports
    as `deferred`. It is never a license to credit the nearest swap.
    """
    account = validate_account(account)
    tag = validate_destination_tag(tag, allocatable=False)
    row = db.execute(
        "SELECT swap_id FROM xrp_destination_tags WHERE account = ? AND destination_tag = ?",
        (account, tag),
    ).fetchone()
    if row is None:
        return None
    return str(row["swap_id"] if isinstance(row, dict) else row[0])


def describe_deposit_instruction(account: str, tag: int) -> str:
    """The operator- and customer-readable deposit instruction (rule 14).

    BOTH HALVES ARE MANDATORY AND THE LINE SAYS SO, because the failure it
    prevents is the common one: a customer who pays the account and omits the
    tag has sent real money that this terminal cannot attribute to their swap.
    chains/xrp_payments.py refuses to guess -- correctly -- so the payment sits
    in the account until an operator matches it by hand.

    Deliberately no network name. This module never learns one, and inventing
    "mainnet" here would be the wrong-comment bug in the place a customer
    reads. chains/xrp.py::network() asks the SERVER, which is the only honest
    source, and the banner that prints this can add it.
    """
    return (
        f"send XRP to {account} with DestinationTag={tag}  <- BOTH are required; without the tag the "
        f"payment cannot be attributed to this swap"
    )


def allocation_summary(db, account: str) -> str:
    """A one-line pasteable state of the tag sequence for an account (rule 14).

    `(none)` rather than a blank, because a blank is ambiguous between "no tags
    allocated" and "the query broke" -- which is the defect rule 14 names
    explicitly. The reserved value is echoed as well, so a reader can see why
    the first tag is 1 without opening the source.
    """
    row = db.execute(
        "SELECT COUNT(*) AS allocated, MAX(destination_tag) AS highest FROM xrp_destination_tags "
        "WHERE account = ?",
        (account,),
    ).fetchone()
    allocated = int(row["allocated"] if isinstance(row, dict) else row[0])
    if not allocated:
        return (
            f"XRP destination tags for {account}: (none) allocated  <- next would be "
            f"{FIRST_ALLOCATABLE_TAG}; {RESERVED_DESTINATION_TAG} is legal on the ledger and is reserved, "
            f"never allocated"
        )
    highest = int(row["highest"] if isinstance(row, dict) else row[1])
    remaining = MAX_DESTINATION_TAG - highest
    return (
        f"XRP destination tags for {account}: {allocated} allocated, highest={highest}, next would be "
        f"{highest + 1}, {remaining} of {MAX_DESTINATION_TAG} remaining  <- never reused, so this only "
        f"counts up"
    )
