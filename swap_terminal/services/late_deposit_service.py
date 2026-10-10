"""Money that arrived for a swap that had already finished. RECORD it; decide nothing.

Role: submodule -> function. Four decisions live here as functions:
       window_seconds / discovery_floor_seconds (how far back to look, and the
       floor that window may never drop below), late_scan_targets (whose deposit
       address a cycle must re-read), late_rows (which scanned events are
       unaccounted for) and late_note (what the count MEANS on the screen).
       reconcile_late_deposits() is orchestration and holds none of them.
Reads: swap_terminal.db (swaps, deposit_events, late_deposits) and the source
       chain adapter's find_deposits_to_address()
Writes: swap_terminal.db (late_deposits) and nothing else. It does not touch
       swaps, deposit_events, payouts or swap_audit_log -- see "IT DECIDES
       NOTHING" below, which is the whole design of this module.
Can move funds: NO, and this is the strongest such claim in the deposit path.
       It writes one table that nothing on the order path reads. It cannot
       credit, refund, re-price, re-arm or change the status of any swap, and
       db.py's comment on `late_deposits` is the schema-level half of the same
       promise.
Mainnet-safe: yes; read-only with respect to the chain.

=============================================================================
THE DEFECT, MEASURED END TO END ON THE OPERATOR'S HOST 2026-10-04
=============================================================================

A payment that arrives at a swap's deposit address AFTER that swap has finished
was never recorded anywhere. Not credited, not refused, not marked
unattributable -- invisible.

    swap s_10b5946333612e06   BTC->GRC   status=completed
    expected_input_amount     0.0001
    credited_at               2026-10-04T16:13:09.315366+00:00
    deposit row id=24         vout=1  amount=0.0001  confs=2  txid 7ea61ac9c7038f58...
    payout                    986.89613973 GRC broadcast

The operator then sent a SECOND 0.0001 BTC to the same address, txid
1abce90c0e7fdfe5ebd3155bdd96036a6d119ccd85d1a9aa8b6fc88e9c6b87ff, confirmed on
chain -- their `desk_hot` balance went 10.00110000 -> 10.00120000. Queried
read-only afterwards: `(none)` rows in deposit_events for that txid, `(none)`
rows in unattributable_deposits. So 0.0001 BTC sat in the hot wallet with
nothing in the database aware of it.

=============================================================================
WHICH STATUSES ARE SCANNED, ESTABLISHED BY RUNNING IT (rule 17)
=============================================================================

The cause was handed to me as a hypothesis and is recorded here as a
measurement. services/deposit_service.ACTIVE_STATUSES is
("awaiting_deposit", "deposit_seen", "confirming"), and on an ADDRESS-attributed
chain a swap's deposit address is only ever scanned from inside
refresh_swap_from_chain(), which is only ever reached from
process_active_swaps()'s `WHERE status IN (...)`. shared_scan_targets() -- the
other function that chooses scan targets -- contributes nothing for those
chains at all, because it iterates swap_service.TAG_ATTRIBUTED_ASSETS.

Measured 2026-10-04 by seeding ONE BTC swap per status against the real schema,
running the real process_active_swaps() with a counting stub adapter, and
reading the rows back. The full status vocabulary is the eight
services/swap_view.py enumerates:

    status             adapter scans   deposit_events written   unattributable
    awaiting_deposit         1                 1                      0
    deposit_seen             1                 1                      0
    confirming               1                 1                      0
    payout_pending           0                 0                      0   <-
    paying                   0                 0                      0   <-
    completed                0                 0                      0   <-
    under_review             0                 0                      0   <-
    failed                   0                 0                      0   <-

Five of eight statuses, and the address is not merely un-credited -- it is
never read. `completed` is the operator's case; `failed` and `under_review` are
the same hole, and `under_review` is the worse one, because that is the HALT
path: a swap is sent there precisely when its deposit did not match its quote,
which is the state a customer is most likely to respond to by sending again.

AND THE TAG-ATTRIBUTED CHAINS DO NOT HAVE THIS HOLE. Measured the same way on
XRP with a payment carrying a finished swap's DestinationTag:
deposit_service.reconcile_shared_accounts() scans the shared account once per
cycle whether or not anything is open, its `claimed` map is EVERY swap on the
asset rather than the active ones, and unattributable_deposit_service.
unclaimed_events() records the payment with the discriminator and a `why` that
names the swap and its status -- for payout_pending, paying, completed,
under_review and failed alike, 1 row each, 0 credited. That is why this module
covers the address-attributed chains only, and
`services/deposit_service.TAG_ATTRIBUTED_ASSETS` is read rather than restated so
the two halves cannot drift apart (rule 8).

=============================================================================
IT DECIDES NOTHING, AND THAT IS NOT MODESTY
=============================================================================

No credit, no refund, no status change, no payout. A settled swap is not
rewritten -- the same refusal migrate_deposit_vouts.py makes, for the same
reason: the swap already paid out, and re-deriving its deposit total from a new
row would either halt a finished swap or re-arm a payout against money nobody
decided to accept. The operator decides; this makes the decision POSSIBLE by
making the money visible, which is the only thing that was missing.

The mechanical guarantee is that this module's only INSERT targets
`late_deposits`, and nothing on the order path reads that table. Not a comment
either: tests/test_late_deposits.py asserts the swap row, its deposit_events
rows and its swap_audit_log rows are byte-for-byte unchanged across a pass that
records a late deposit.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import NamedTuple

# Rootless, the same way services/deposit_service.py reaches
# deposit_vout_artifact.py: swap_terminal/ is already on sys.path for the
# `services` package to have resolved at all.
#
# modules/htlc_timelock.py IS SAFE TO IMPORT FROM HERE, AND THAT WAS CHECKED RATHER
# THAN ASSUMED (rule 12's import-time-side-effect note, rule 19's "a noqa is a claim
# you checked"). modules/__init__.py warns that importing most of that package is NOT
# free -- atomic_htlc_scripts.py raises at import time if SECRET_HASH is unset. This
# module is the exception and says so in its own header: "no environment variable, no
# file and no RPC call in this module", pure arithmetic. Verified 2026-10-04 by
# importing it alone into a bare interpreter: it pulls in no `chains` module, no
# `requests`, and the only top-level statements are constants plus one assertion about
# the lock-hour ordering.
#
# IMPORTED RATHER THAN RE-SPELLED because it is the one place in this tree that knows
# a chain's target block interval (rule 11: one vocabulary, derived in one place). A
# second copy of `{"BTC": 600, "LTC": 150, "GRC": 90}` here would be rule 8's bug with
# a delay on it, and the delay would be measured in a window that silently stopped
# covering a chain whose interval somebody corrected.
# PAYMENT_UNIQUE_KEY AND NOT A SECOND COPY OF ITS COLUMN NAMES. This module both
# READS deposit_events (accounted_keys) and WRITES late_deposits (its ON CONFLICT
# target), and on 2026-10-10 those two were keyed differently from each other for one
# commit -- the widening to (asset, txid, vout, address) touched the writer and not
# the reader. Importing the tuple is what makes a third drift impossible rather than
# merely unlikely.
from db import PAYMENT_UNIQUE_KEY
from microfortnights import format_duration
from modules.htlc_timelock import SECONDS_PER_BLOCK

from .deposit_service import ACTIVE_STATUSES
from .helpers import parse_iso
from .swap_service import TAG_ATTRIBUTED_ASSETS

logger = logging.getLogger(__name__)


#: How far back a late-deposit pass looks, in seconds.
#:
#: SECONDS AND NOT MICROFORTNIGHTS, which is rule 6's boundary rather than an
#: oversight: this is arithmetic against ISO timestamps in a WHERE clause, an
#: interface rather than a report. Every PRINTED form of it goes through
#: microfortnights.format_duration(), which is why late_note() below renders
#: `71428.6µfn (86400.0s)` and this constant holds 86400.
#:
#: ===================== HOW THIS NUMBER WAS CHOSEN =====================
#:
#: THE FLOOR IS DERIVED FROM THE TREE AND IS 1800s. discovery_floor_seconds()
#: below computes it and tests/test_late_deposits.py asserts this constant
#: exceeds it for every address-attributed asset, so the window cannot quietly
#: stop covering the case it exists for. Per asset it is
#:
#:     Config.QUOTE_TTL_SECONDS + min_confirmations * SECONDS_PER_BLOCK[asset]
#:
#:     asset  TTL   min_conf  block target   floor
#:     BTC    600       2         600s        1800s   <- the binding one
#:     LTC    600       2         150s         900s
#:     GRC    600       6          90s        1140s
#:
#: The first term is the span during which a customer is still acting on a LIVE
#: deposit instruction (Config.QUOTE_TTL_SECONDS defaults to 600). The second is
#: the longest a payment sent at the very instant a swap stopped being watched
#: can take to become discoverable, from that asset's own credit threshold
#: (Config.*_MIN_CONFIRMATIONS: BTC 2, LTC 2, GRC 6) and its target block
#: interval. A window below 1800s provably misses a BTC payment that was already
#: in flight when the swap closed -- which is not a hypothetical shape, it is
#: exactly "their first send was slow so they resent".
#:
#: 86400s IS 48x THAT FLOOR, AND THE MULTIPLE IS NOT DERIVED FROM ANYTHING,
#: WHICH IS SAID RATHER THAN DRESSED UP (rule 3: if a number cannot be measured,
#: say that instead of letting an estimate harden into a fact). The floor covers
#: only a payment already in flight. The case this module exists for is a HUMAN
#: one -- they send twice, they resend after a slow first send, they reuse the
#: address for a later swap -- and there is no measurement of human latency
#: anywhere in this tree to derive a window from. 24 hours is chosen as the
#: coarsest period an operator reconciles over, so a payment that arrives
#: overnight is still recorded by the time anybody looks.
#:
#: WHAT THE WINDOW COSTS, with the denominator named. One
#: `find_deposits_to_address()` call per distinct (asset, address) among the
#: swaps that LEFT ACTIVE_STATUSES inside the window, once per reconcile cycle
#: (DEFAULT_POLL_SECONDS=60, so 60 cycles/hour). On a Bitcoin-derived chain that
#: call is one `listtransactions` plus one getrawtransaction/gettransaction per
#: matching receive, against a local daemon.
#:
#: THE DENOMINATOR CANNOT BE MEASURED FROM THIS CHECKOUT and that is stated
#: rather than estimated: swap_terminal/swap_terminal.db holds 0 swaps, 0
#: deposit_events and 0 payouts (counted read-only 2026-10-04), so "how many
#: swaps finish per day" has no value here. What IS bounded is the shape: the
#: cost scales with swaps FINISHED per day and not with the window's length past
#: that, because a day-old swap drops out as a new one arrives. On the operator's
#: host, where deposit_service.shared_scan_targets()' own measurement records 3
#: open swaps, that is single digits of addresses and therefore a few hundred
#: listtransactions per hour on a loopback daemon.
#:
#: NOT AN ENVIRONMENT VARIABLE, deliberately. Every tunable in config.py that is
#: read on the money path is one an operator can get wrong in the dangerous
#: direction; this one can only make a RECORD appear or not appear, and a
#: constant beside its own derivation is more useful to the next reader than a
#: variable whose default lives in a different file.
LATE_DEPOSIT_WINDOW_SECONDS = 86_400

#: A scanned payment is recorded at this many confirmations or more.
#:
#: ONE, AND IT IS A RECORDING THRESHOLD RATHER THAN A MONEY GATE. A transaction
#: with zero confirmations has not been mined and can still be replaced or
#: dropped, so recording it would write a permanent row asserting the desk holds
#: money it may never receive -- a false record of money, which on this table is
#: the one failure mode worse than a missing one, because the whole point of the
#: row is that somebody acts on it.
#:
#: NOTHING IS LOST BY WAITING. The reconcile loop runs every 60s against a window
#: of 86400s, so a payment that confirms is recorded on a later pass with 1439
#: passes to spare; a payment that never confirms is money that never arrived and
#: has nothing to record.
#:
#: THE SAME REASONING chains/base.MIN_FABRICATED_CONFIRMATIONS GIVES, and the
#: constant is deliberately NOT imported from there (rule 8's "if they genuinely
#: differ, the difference is the point and belongs in a comment at BOTH sites").
#: That one bounds when a SYNTHETIC deposit_events row may be emitted on the
#: crediting path; this one bounds when a record is written on a path that credits
#: nothing. They agree on the value today and they answer different questions, so
#: tying them together would make a change to either one move the other.
MIN_RECORDED_CONFIRMATIONS = 1


def accounted_key(row) -> tuple:
    """The identity of one payment, as db.PAYMENT_UNIQUE_KEY defines it, minus `asset`.

    ONE FUNCTION FOR BOTH SIDES OF A SET MEMBERSHIP TEST, which is the whole point.
    accounted_keys() builds the set from database rows and late_rows() builds the
    probe from a scanned chain event; before 2026-10-10 each spelled `(txid, vout)`
    separately and they were wrong TOGETHER, which is precisely why nothing failed.
    Two spellings of one key cannot disagree if there is only one spelling.

    `asset` IS EXCLUDED because it is the WHERE clause, not part of the tuple:
    accounted_keys() selects a single asset, so carrying it in every member would be
    a constant compared against itself.

    NORMALIZATION IS THE OTHER HALF OF THE BUG. A database row's vout is an INTEGER
    and a scanned event's may be a string; an address may carry whitespace on one side
    and not the other. A tuple that differs only by type is a tuple that is never
    found, and the symptom is a late deposit silently recorded twice rather than
    dropped -- the opposite failure, equally quiet. str()/int()/strip() here means
    both sides are normalized by the same code.

    A MISSING ADDRESS BECOMES "" RATHER THAN None, because the column is nullable on
    rows written before addresses were recorded, and None != "" would make an old row
    account for nothing.
    """
    vout = row.get("vout") if hasattr(row, "get") else row["vout"]
    return (
        str(row["txid"]),
        int(vout or 0),
        ((row.get("address") if hasattr(row, "get") else row["address"]) or "").strip(),
    )


class LateDeposit(NamedTuple):
    """One payment that arrived for a swap that had already finished.

    A VALUE TYPE SO THE FILTER CAN BE TESTED WITHOUT A DATABASE (rule 10).
    late_rows() is pure and returns these; record() is the only thing that knows
    SQL. The split is what lets a test assert on the decision -- "is this event
    unaccounted for" -- with seeded dicts rather than by standing up a chain.

    `swap_status` IS THE STATUS AT RECORDING TIME and db.py's column comment says
    why it is carried rather than joined: a row read a week later has to say
    whether the money arrived after a COMPLETED payout or after a FAILED one, and
    those are different conversations with the customer.
    """

    swap_id: str
    swap_status: str
    asset: str
    txid: str
    vout: int
    address: str
    amount: float
    confirmations: int


def discovery_floor_seconds(config: dict, assets) -> dict[str, float]:
    """The shortest window that can cover an in-flight payment, per asset. PURE.

    EXTRACTED AS A FUNCTION so the window can be checked against it by a test
    rather than by a reader comparing two comments (rule 10: the decision is the
    smallest testable piece). The derivation is in LATE_DEPOSIT_WINDOW_SECONDS'
    own comment; this is the arithmetic.

    AN ASSET WITH NO KNOWN BLOCK INTERVAL IS LEFT OUT RATHER THAN DEFAULTED, and
    the omission is the honest answer (rule 17): modules/htlc_timelock.
    SECONDS_PER_BLOCK knows BTC, LTC and GRC, which is every address-attributed
    asset this terminal trades. A fourth one added without a block interval would
    produce a floor computed from a number nobody measured, and a floor that is
    wrong in the low direction is a window that looks justified and is not. The
    caller sees a missing key; tests/test_late_deposits.py asserts every
    address-attributed asset HAS one, so adding a chain fails a test by name
    instead of silently narrowing the window.

    min_confirmations IS READ FROM CONFIG AND NOT FROM A SWAP ROW, which is the
    opposite of what deposit_service.settled_txids() does and the difference is
    the point. That function asks "has THIS swap's deposit settled", and
    min_confirmations is copied onto the swap at creation, so the row is the only
    correct source. This asks "how long could a payment on this CHAIN take to
    become discoverable" -- a question about the chain and the current
    configuration, asked before any particular payment exists.
    """
    ttl = float(config.get("QUOTE_TTL_SECONDS") or 0)
    floors: dict[str, float] = {}
    for asset in sorted(assets):
        block_seconds = SECONDS_PER_BLOCK.get(asset)
        if block_seconds is None:
            continue
        confirmations = int(config.get(f"{asset}_MIN_CONFIRMATIONS") or 0)
        floors[asset] = ttl + confirmations * float(block_seconds)
    return floors


def address_attributed_assets(adapters: dict) -> list[str]:
    """The assets whose deposits are attributed BY ADDRESS and that we can scan.

    DERIVED BY SUBTRACTION rather than listed, which is rule 11's "one
    vocabulary, derived in one place" and also the only form that cannot rot:
    swap_service.TAG_ATTRIBUTED_ASSETS is the authority for which chains share an
    account, so a chain added to it leaves this set automatically and a chain
    added to neither arrives here by default. Listing BTC/LTC/GRC would be a
    second copy of that vocabulary, and the copy would be wrong the day a fourth
    UTXO chain is wired.

    THE DEFAULT DIRECTION IS THE SAFE ONE, and it is worth saying which way round
    that is. A new address-attributed chain that nobody remembered to add here
    would be a chain whose late deposits are invisible -- the defect this module
    exists for. A new TAG-attributed chain wrongly left in this set would be
    scanned by both this pass and reconcile_shared_accounts(), which costs a
    duplicate scan and records nothing wrong, because late_rows() below filters
    on deposit_events rows that the shared-account path writes identically. So
    the failure mode of forgetting is a wasted RPC call rather than lost money.

    Keyed on the ADAPTERS rather than on config: an asset with no adapter has
    nothing to scan with, which is the same reading
    deposit_service.shared_scan_targets() gives for the same situation.
    """
    return sorted(set(adapters) - set(TAG_ATTRIBUTED_ASSETS))


# The swaps whose deposit address a late pass must re-read.
#
# IN SQL AND NOT A PYTHON LOOP (rule 20). This is a filter, a window and an ordering
# over rows already in swap_terminal.db, so it is a query: it answers the same way for
# every reader, it can be run at a sqlite3 prompt against the live database to see
# exactly what a pass will scan, and it has no half-applied state on a row that raises
# -- which the family-bleed loop CLAUDE.md rule 20 cites did have.
#
# `status NOT IN ACTIVE_STATUSES` RATHER THAN A LIST OF THE FIVE FINISHED ONES. The
# five were MEASURED (this module's header carries the table) and spelling them here
# would be a second copy of deposit_service.ACTIVE_STATUSES' complement -- rule 8's bug
# with a delay on it, and the delay would be a new status nobody added to this list,
# whose late deposits would then be invisible exactly as `completed`'s were. The
# complement is derived, so a status added anywhere is covered here by construction.
#
# `updated_at` IS THE CLOCK, and it is the only column that exists for all five. A
# completed swap has completed_at, a credited one has credited_at, and a failed one has
# neither -- it has failed_reason and nothing dated. swap_service.set_swap_status()
# writes updated_at on every transition, so it is "when this swap last moved", which is
# the quantity the window is about.
#
# IT DOES NOT FILTER ON EXPIRY. A swap whose quote expired long ago can still be the
# one somebody paid into; `expires_at` is about whether a NEW deposit may be credited,
# and nothing here credits anything.
_TARGETS_SQL = """
SELECT id, status, from_asset, deposit_address, updated_at
FROM swaps
WHERE status NOT IN ({statuses})
  AND from_asset IN ({assets})
  AND updated_at >= ?
  AND deposit_address IS NOT NULL
  AND deposit_address <> ''
ORDER BY updated_at DESC, id ASC
"""


def late_scan_targets(db, assets, *, cutoff: str) -> list:
    """The swap rows a late pass must re-read the deposit address of. ONE SELECT.

    `cutoff` IS AN ISO TIMESTAMP HANDED IN rather than computed here, for the
    reason unattributable_deposit_service.record() gives for `now`: the caller
    owns the clock, so a test can assert what a window includes and excludes
    without patching time. reconcile_late_deposits() derives it from
    LATE_DEPOSIT_WINDOW_SECONDS.

    AN EMPTY ASSET LIST ANSWERS `[]` WITHOUT A QUERY, because `IN ()` is not valid
    SQLite and because an installation with no address-attributed adapter
    genuinely has no targets. Rule 14: that is a result, and the caller's line
    prints it rather than leaving a gap.
    """
    assets = [str(asset) for asset in assets or ()]
    if not assets:
        return []
    # Both interpolations are runs of '?' generated from the LENGTH of a tuple this
    # repository controls; every status, every asset and the cutoff are bound as
    # parameters on the line below. Structure, not input -- the same claim
    # deposit_service.process_active_swaps() makes for the identical tuple, and
    # checkable from these four lines (rule 12's S608 note).
    #
    # NO `noqa: S608` HERE, AND THAT IS NOT AN OVERSIGHT -- the same reading
    # unattributable_deposit_service.skippable_unattributable_txids() records for the
    # identical shape. ruff does not flag `.format()` on a module constant, so a
    # suppression would be a claim about a finding that was never made; RUF100 says so
    # out loud, and rule 19 says a noqa is a claim you checked rather than decoration.
    # The reasoning above is the claim.
    sql = _TARGETS_SQL.format(
        statuses=",".join("?" for _ in ACTIVE_STATUSES),
        assets=",".join("?" for _ in assets),
    )
    # The cutoff is the last positional parameter, after the statuses and the assets,
    # matching the order the three `?` runs appear in _TARGETS_SQL.
    return db.execute(sql, (*ACTIVE_STATUSES, *assets, cutoff)).fetchall()


def accounted_keys(db, asset: str) -> frozenset[tuple[str, int]]:
    """Every (txid, vout) on this asset that some deposit_events row already holds.

    THE IDEMPOTENCY HALF THAT IS ABOUT CREDITING rather than about re-recording.
    An output already in deposit_events has been seen by the crediting path and
    belongs to a swap; it is not a late deposit whatever its swap's status is now,
    and the swap's own credited deposit is the obvious member of this set -- on
    the operator's measured case, txid 7ea61ac9c7038f58... at vout=1.

    THE KEY IS db.PAYMENT_UNIQUE_KEY AND IS DERIVED FROM IT, NOT SPELLED HERE.

    =========================================================================
    IT USED TO BE (txid, vout) ASSET-WIDE, AND THAT DROPPED A REAL LATE DEPOSIT
    =========================================================================

    This docstring used to justify the narrower key like this, and the sentence was
    true when it was written:

        "deposit_events is UNIQUE(asset, txid, vout), so an output credited to a
        DIFFERENT swap cannot also be this swap's."

    It stopped being true in 4e71b37, EARLIER THE SAME DAY, when the constraint was
    widened to (asset, txid, vout, address) -- because on ICP the txid IS the ledger
    block index and every swap gets its own subaccount, so two different swaps
    legitimately share a (txid, vout) and are told apart only by the address. That
    widening touched the two places that WRITE (deposit_events and late_deposits) and
    left this one, which READS deposit_events to decide, keyed on the old constraint.

    MEASURED, against a throwaway database with the real SCHEMA: an old settled ICP
    row at txid '2' vout 0 for subaccount A made ('2', 0) accounted for the whole
    asset, and a real late payment at block index 2 of the REBUILT ledger into a
    DIFFERENT swap's subaccount B was filtered out by late_rows() with a bare
    `continue`. No late_deposits row, nothing in the pass's count, nothing in
    show_late_deposits.py -- and this pass is, by its own header, the only thing that
    records money arriving for a swap that has already finished. The amount in the
    measurement is 2.44081155 ICP, which is the operator's own figure from today.

    That is rule 8's exact damage model: two copies of one rule, agreeing on the day
    they were written. The third copy is why the key is now DERIVED from
    db.PAYMENT_UNIQUE_KEY rather than typed out again -- the next widening cannot
    leave this reader behind, because there is nothing here left to forget.

    EVERY ROW ON THE ASSET AND NOT ONLY THIS SWAP'S, still, and the reason survives
    the widening: a payment already in deposit_events has been seen by the crediting
    path and belongs to whichever swap's address it was paid to. Recording it as late
    as well would be a second row claiming the same coins, which is the shape db.py's
    comment on unattributable_deposits refuses. What changed is only that "the same
    coins" now includes the address, which is what makes two subaccounts two payments
    instead of one.

    ONE SELECT AND NO JOIN, unchanged: `asset` lives on deposit_events and nothing
    else is needed, which is the smaller answer to rule 20's test.

    THE COLUMN LIST IS INTERPOLATED AND THE `noqa: S608` IS EARNED. The names come
    from db.PAYMENT_UNIQUE_KEY, a module-level tuple of literals in this repository's
    own source -- they are IDENTIFIERS, not input, and no caller can reach them. The
    only value in the statement, `asset`, is a parameter.
    """
    # asset is the first element of the key and is the WHERE clause, not part of the
    # tuple this returns, so the selected columns are the rest of it. Derived rather
    # than sliced by index: a key reordered in db.py must not silently reorder here.
    columns = [name for name in PAYMENT_UNIQUE_KEY if name != "asset"]
    rows = db.execute(
        f"SELECT {', '.join(columns)} FROM deposit_events WHERE asset = ?",  # noqa: S608 -- see docstring
        (asset,),
    ).fetchall()
    return frozenset(accounted_key(row) for row in rows)


def late_rows(events, swap, accounted) -> list[LateDeposit]:
    """The scanned events that no deposit_events row accounts for. PURE -- no db, no chain.

    THE DECISION, and it is one line of filtering with three reasons behind each
    clause. Pure so it can be called with seeded dicts and asserted on directly
    (rule 10), which is also how the "already credited" case is pinned without a
    chain: hand it the swap's own deposit event and it returns nothing.

    `accounted` IS AN ARGUMENT rather than something read here, so this function has
    no database handle and cannot acquire one. A filter that could query is a filter
    that will eventually decide something. It is accounted_keys()' frozenset of
    (txid, vout) pairs, and a test can hand it a bare set.

    THE CONFIRMATION FLOOR IS APPLIED HERE and MIN_RECORDED_CONFIRMATIONS carries
    the argument: a zero-confirmation sighting is not yet money.

    THE ADDRESS IS RE-CHECKED AGAINST THE SWAP'S OWN, even though the adapter was
    asked for exactly that address. find_deposits_to_address() on the
    Bitcoin-derived chains is a whole-wallet `listtransactions` filtered in
    Python, so what comes back is whatever that filter let through -- and a row
    written against the wrong address on THIS path would attribute a stranger's
    payment to a customer's finished swap. One comparison is cheaper than the
    conversation that follows from getting it wrong.
    """
    address = (swap["deposit_address"] or "").strip()
    rows = []
    for event in events or ():
        txid = str(event.get("txid") or "")
        if not txid or event.get("vout") is None:
            continue
        vout = int(event["vout"])
        # THE SAME TUPLE accounted_keys() RETURNS, built by the same function, because
        # a set membership test is only as good as the two sides agreeing. Before
        # 2026-10-10 this compared (txid, vout) against a set keyed the same way and
        # both were wrong together, which is why nothing failed: on ICP the txid is a
        # ledger block index and two swaps' subaccounts share it.
        #
        # THE ADDRESS CHECK BELOW DOES NOT COVER THIS. It compares the event to THIS
        # swap's address; the set is asset-wide and holds OTHER swaps' addresses. An
        # event for this swap's address was being dropped because a different swap had
        # a row at the same block index.
        if accounted_key({"txid": txid, "vout": vout, "address": event.get("address")}) in accounted:
            continue
        if (event.get("address") or "").strip() != address:
            continue
        confirmations = int(event.get("confirmations") or 0)
        if confirmations < MIN_RECORDED_CONFIRMATIONS:
            continue
        amount = float(event.get("amount") or 0)
        if amount <= 0:
            continue
        rows.append(LateDeposit(
            swap_id=swap["id"],
            swap_status=swap["status"],
            asset=swap["from_asset"],
            txid=txid,
            vout=vout,
            address=address,
            amount=amount,
            confirmations=confirmations,
        ))
    return rows


def record(db, rows, *, now: str) -> int:
    """Write or touch one row per late deposit. Returns how many were NEW.

    IDEMPOTENT BECAUSE THE LOOP POLLS, and this is the half of idempotency that is
    about re-recording rather than about crediting. reconcile_late_deposits() runs
    every 60s over a window of 86400s, so it sees the same payment up to 1440
    times and must not accumulate a row per sighting. First sighting inserts;
    every later one bumps last_seen_at and confirmations.

    UPSERT RATHER THAN SELECT-THEN-INSERT, for the reason
    unattributable_deposit_service.record() gives: deposit_watcher and
    reconcile_worker are two processes against one database, and a check-then-write
    would race into an IntegrityError on the UNIQUE constraint. ON CONFLICT makes
    the collision the normal path instead of an exception.

    THE CONFLICT TARGET IS db.PAYMENT_UNIQUE_KEY AND IT MUST STAY EQUAL TO IT, all four
    columns. `address` joined that key on 2026-10-10 -- db.py's SCHEMA carries the
    measurement at deposit_events' own UNIQUE clause -- and this clause did not follow in
    the same edit. SQLite does not treat that as a near-miss: an ON CONFLICT target that
    does not name an actual unique index raises

        OperationalError: ON CONFLICT clause does not match any PRIMARY KEY or
                          UNIQUE constraint

    on EVERY insert, so recording a late deposit stopped working outright rather than
    degrading. Caught by tests/test_late_deposits.py, 17 cases, in the suite diff
    immediately after the widening -- which is the argument for diffing the suite line by
    line against a recorded baseline rather than comparing failure counts: this arrived in
    the same run as 30 added tests, and a net of +13 would have read as progress.

    A loud failure, at least, and that is not luck: it is SQLite refusing an upsert whose
    target it cannot resolve, rather than silently inserting a duplicate row. The widening
    could not have made this path quietly wrong.

    swap_id AND swap_status ARE NOT RE-POINTED BY THE UPDATE, which is the same
    refusal deposit_service.upsert_deposit_event() makes about swap_id and for a
    stronger reason here: `swap_status` is deliberately the status AT FIRST
    SIGHTING (db.py's column comment), so letting a later pass overwrite it with
    today's status would destroy the one fact the column exists to carry. An
    operator who resolves a late deposit after moving the swap would find the row
    had quietly agreed with them.

    AN ALREADY-RESOLVED ROW IS STILL TOUCHED AND NOT REOPENED. A person wrote that
    resolution; a later sighting of the same output is not new information about
    who sent it, and clearing resolved_at would make the scan undo a human's
    decision every minute. Same rule, same reason, as the unattributable table.

    `now` IS A PARAMETER, not datetime.now() inside: the timestamps are what an
    operator reads to decide how long money has been sitting, so they have to be
    assertable without patching the clock.

    IT LOGS AT WARNING ON A NEW ROW, because a late deposit is money the desk holds
    and does not account for. The log line is NOT the record, though -- rule 5: a
    measurement that only exists in a log is not learning, and the row is what the
    operator acts on. The line exists so the pass is not silent in a terminal
    (rule 14).

    DOES NOT COMMIT; the caller owns the transaction, as everything else on this
    path does.
    """
    inserted = 0
    for row in rows:
        db.execute(
            """
            INSERT INTO late_deposits
                (swap_id, swap_status, asset, txid, vout, address, amount,
                 confirmations, first_seen_at, last_seen_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(asset, txid, vout, address) DO UPDATE SET
                last_seen_at = excluded.last_seen_at,
                confirmations = excluded.confirmations,
                amount = excluded.amount
            """,
            (row.swap_id, row.swap_status, row.asset, row.txid, row.vout,
             row.address, row.amount, row.confirmations, now, now),
        )
        # rowcount is 1 for both the insert and the update and `changes()` cannot tell
        # them apart either, which is the measurement
        # unattributable_deposit_service.record() already records. The first-sighting
        # test is whether first_seen_at is THIS call's `now`, which only an inserted
        # row has.
        #
        # BY NAME, NOT BY POSITION: db.connect_db sets row_factory to a dict factory,
        # so `found[0]` raises KeyError -- the mistake that function's comment records.
        found = db.execute(
            "SELECT first_seen_at FROM late_deposits WHERE asset = ? AND txid = ? AND vout = ?",
            (row.asset, row.txid, row.vout),
        ).fetchone()
        if found is not None and found["first_seen_at"] == now:
            inserted += 1
            logger.warning(
                "LATE DEPOSIT: %.8f %s arrived at %s, which is the deposit address of swap %s "
                "-- a swap that was already %s and is no longer watched. txid %s vout %d, %d "
                "confirmation(s). RECORDED in late_deposits and NOTHING ELSE: the swap is "
                "unchanged, nothing was credited, nothing was refunded and no payout was "
                "re-armed. The desk is holding these coins and no swap accounts for them; a "
                "person has to decide what happens to them.",
                row.amount, row.asset, row.address, row.swap_id, row.swap_status,
                row.txid, row.vout, row.confirmations,
            )
    return inserted


class LatePass(NamedTuple):
    """What one late-deposit pass did. Three numbers, because one cannot be read alone.

    `recorded` ALONE IS AMBIGUOUS and that is why this is a tuple rather than an int.
    A zero means "nothing arrived late" when some swap was actually looked at, and
    "nothing was looked at" when none was -- two different things an operator would do
    two different things about, rendered identically by a bare 0 (rule 14's `(none)` is
    a result). late_note() needs `targets` to tell them apart, so the caller is handed
    it rather than asked to recount it.

    `addresses` IS THE SCAN COST, on the line where the window is explained. It is how
    many find_deposits_to_address() calls this pass made, which is the figure
    LATE_DEPOSIT_WINDOW_SECONDS' comment reasons about and the one that would move if
    somebody widened the window.
    """

    recorded: int
    targets: int
    addresses: int


def reconcile_late_deposits(db, config: dict, adapters: dict, *, now: str) -> LatePass:
    """Re-read recently finished swaps' deposit addresses. Returns what the pass did.

    ORCHESTRATION AND NOTHING ELSE (rule 10). The window is a constant with its
    derivation beside it, which swaps to scan is late_scan_targets()' SELECT, which
    events are unaccounted for is late_rows(), and what the count means is
    late_note(). This function reads the first, loops the second, and writes.

    ONE SCAN PER DISTINCT (asset, address), not one per swap. Two swaps sharing a
    deposit address should not happen on an address-attributed chain -- swap_service
    derives a fresh one per swap -- but `should not happen` is not a reason to make
    the cost quadratic if it does, and this is the same per-cycle scan cache
    deposit_service.scan_shared_accounts() keeps for the tag-attributed chains.

    THE SKIP SET IS DELIBERATELY NOT PASSED. deposit_service.skip_txids() exists to
    stop Solana re-reading settled signatures, and settled is exactly what every
    transaction on a finished swap is -- so handing it over would skip the swap's own
    credited deposit AND every late payment that this pass had already recorded and
    whose confirmations it still wants to advance. On the Bitcoin-derived chains the
    parameter is accepted and ignored anyway (chains/base.find_deposits_to_address()
    says so in its docstring), and the address-attributed chains are the only ones
    this pass covers, so the default empty set costs nothing today and is the correct
    value rather than the convenient one.

    ONE ADAPTER FAILURE DOES NOT LOSE THE OTHER ASSETS' ROWS, and that is the one
    broad catch in this module (rule 12). The caller is reconcile_worker's cycle,
    which already guards the whole cycle -- but a cycle that dies on GRC being
    briefly unreachable would silently stop recording BTC's late deposits too, and
    this pass is the ONLY thing that records them. The handler cannot be mistaken for
    a real answer: it logs the exception with the asset and address, and the count it
    returns is the rows actually written rather than a zero standing in for "nothing
    arrived" -- the distinction rule 12 says a broad catch must preserve.
    """
    assets = address_attributed_assets(adapters)
    cutoff = _cutoff_iso(now, LATE_DEPOSIT_WINDOW_SECONDS)
    targets = late_scan_targets(db, assets, cutoff=cutoff)
    # Per-asset, computed once rather than once per target, because it answers the same
    # question for every address on an asset (rule 8). The same reasoning
    # scan_shared_accounts() gives for hoisting skip_txids() out of its loop.
    accounted: dict[str, frozenset[tuple[str, int]]] = {}
    scanned: dict[tuple[str, str], list[dict]] = {}
    recorded = 0
    for swap in targets:
        asset = swap["from_asset"]
        address = (swap["deposit_address"] or "").strip()
        if asset not in accounted:
            accounted[asset] = accounted_keys(db, asset)
        key = (asset, address)
        if key not in scanned:
            try:
                scanned[key] = adapters[asset].find_deposits_to_address(address)
            # NO `noqa: BLE001`, AND IT WAS CHECKED RATHER THAN ASSUMED: ruff does not
            # raise BLE001 for a broad catch whose handler calls logger.exception(), so a
            # suppression here would be a claim about a finding nobody made (RUF100 flags
            # exactly that, and it did). Rule 12's test for whether the catch is legitimate
            # is met on its own terms and the docstring carries the argument: the caller CAN
            # tell this failure from a real answer, because the address is logged with its
            # traceback and the returned count is rows WRITTEN rather than a zero standing
            # in for "nothing arrived".
            except Exception:
                logger.exception(
                    "late-deposit scan of %s address %s FAILED -- this address was NOT checked "
                    "this pass and a late payment to it would not have been recorded. The other "
                    "assets in this pass still ran; the next cycle retries in 60s.",
                    asset, address,
                )
                scanned[key] = []
        recorded += record(db, late_rows(scanned[key], swap, accounted[asset]), now=now)
    logger.info(
        "late-deposit pass: %d recently finished swap(s) inside the %.0fs window, %d distinct "
        "address(es) scanned, %d NEW late deposit(s) recorded%s",
        len(targets), float(LATE_DEPOSIT_WINDOW_SECONDS), len(scanned), recorded,
        "" if targets else " -- (none) to scan",
    )
    return LatePass(recorded=recorded, targets=len(targets), addresses=len(scanned))


def _cutoff_iso(now: str, window_seconds: float) -> str:
    """`now` minus the window, as an ISO string the SQL can compare against.

    A HELPER AND NOT A DECISION: the window itself is
    LATE_DEPOSIT_WINDOW_SECONDS, and this only subtracts it. It is a separate
    function so the subtraction can be exercised directly -- a cutoff computed in
    the wrong direction would widen the window to everything, which looks like
    working harder rather than like a bug.

    SECONDS ARITHMETIC AND NOT MICROFORTNIGHTS, which is rule 6's boundary: the
    comparison is against a timestamp column, an interface. The PRINTED window
    goes through format_duration() in late_note().
    """
    return (parse_iso(now) - timedelta(seconds=window_seconds)).isoformat()


def late_note(recorded: int, targets: int, *, window_seconds: float = LATE_DEPOSIT_WINDOW_SECONDS) -> str:
    """What `late_deposits` MEANS, on the screen rather than in a comment.

    Rule 14: "state what the number means, next to the number -- the operator
    reads the screen, not the source". This is the same shape as
    reconcile_worker.backstop_note() and payout_service.inventory_note(), and it
    is needed for the same reason: this is a brand-new field on the cycle line and
    nobody has a prior intuition for what a value of 0 or 3 is telling them.

    ZERO AND NON-ZERO READ COMPLETELY DIFFERENTLY, which is the point. Unlike
    `transitions_written`, non-zero here is NOT merely worth a second look -- it is
    money the desk is holding that no swap accounts for, so the note says so in
    those words and names the table to read. Zero says `(none)` explicitly rather
    than leaving the reader to infer it from a bare 0.

    IT ECHOES THE WINDOW, because the window is the parameter that decides the
    answer (rule 14: "echo the parameters that decide the answer"). A `0` with no
    window beside it is ambiguous between "nothing arrived late" and "nothing
    finished recently enough to be looked at", and the `targets` count is what
    separates those two -- so it is on the line as well.
    """
    window = format_duration(window_seconds)
    if recorded:
        return (
            f"late_deposits={recorded} means {recorded} payment(s) arrived at the deposit "
            f"address of a swap that had ALREADY FINISHED -- the desk is holding those coins "
            f"and no swap accounts for them. NOTHING was credited, refunded or changed: read "
            f"the late_deposits table (swap_id, swap_status, amount, txid, vout) and decide. "
            f"It counts rows NEW this pass, so it returns to 0 once each is recorded, and "
            f"{targets} finished swap(s) were in the {window} window"
        )
    if not targets:
        return (
            f"late_deposits=0 with no finished swap inside the {window} window: (none) to "
            f"check, so this says nothing either way about late payments"
        )
    return (
        f"late_deposits=0 is the expected reading: {targets} swap(s) finished inside the "
        f"{window} window, their deposit addresses were re-read, and (none) carried a payment "
        f"that no deposit_events row already accounts for"
    )


#: The rows an operator reads when the desk is holding coins no swap accounts for.
#:
#: HERE AND NOT IN THE ROOT TOOL, for the reason rule 20 gives: a filter over rows
#: already in swap_terminal.db is a query, and a query that decides what
#: "outstanding" means must answer the same way for every reader. The precedent is
#: unattributable_deposit_service.OUTSTANDING_SQL, which derives the same predicate
#: as a column for the same reason -- a root tool recomputing `resolved_at IS NULL`
#: would be the second place that definition lived, which is rule 8's bug with a
#: delay on it.
#:
#: swap_status IS THE CARRIED ONE, not a join to swaps. db.py:271-276 says why in
#: the schema: an operator reading this a week later needs the status AT THE MOMENT
#: the money arrived, because that is what decides whether the desk is holding a
#: customer's extra send (the payout already completed) or may still owe the
#: original payout too (it failed). Joining to swaps would give today's status,
#: which is not the one that made this a late deposit. So this SELECT must NOT
#: join, and that is a deliberate omission rather than a missing feature.
OUTSTANDING_SQL = """
SELECT
    swap_id,
    swap_status,
    asset,
    txid,
    vout,
    address,
    amount,
    confirmations,
    first_seen_at,
    last_seen_at,
    resolved_at,
    resolution_note,
    CASE WHEN resolved_at IS NULL THEN 1 ELSE 0 END AS outstanding
FROM late_deposits
WHERE (:asset IS NULL OR asset = :asset)
  AND (:include_resolved = 1 OR resolved_at IS NULL)
-- Oldest first: the deposit somebody has been waiting longest on is the one that
-- belongs at the top, the same ordering unattributable_deposit_service uses and
-- for the same reason.
ORDER BY first_seen_at ASC, id ASC
"""


def outstanding(db, asset: str | None = None, *, include_resolved: bool = False) -> list:
    """Late deposits, oldest first; unresolved only unless asked for all of them.

    Reads. Writes nothing, decides nothing, and in particular does NOT resolve:
    deciding that one of these payments belongs to somebody, and sending them
    coins, is fund movement and the operator's call (rule 16). This puts the
    evidence for that call on a screen.

    `asset=None` means every asset rather than none -- the SQL spells that as
    `:asset IS NULL OR asset = :asset` so the filter is one predicate instead of
    two code paths.
    """
    return list(
        db.execute(
            OUTSTANDING_SQL,
            {"asset": asset, "include_resolved": 1 if include_resolved else 0},
        ).fetchall()
    )
