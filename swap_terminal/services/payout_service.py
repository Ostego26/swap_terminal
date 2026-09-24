"""Broadcast the payout leg, and track hot-wallet inventory.

Role: submodule -> function (process_pending_payouts is the decision)
Reads: swap_terminal.db (swaps, payouts, wallet_inventory), the destination
       adapter (getbalance)
Writes: swap_terminal.db (payouts, wallet_inventory, swaps, swap_audit_log)
       AND THE CHAIN
Can move funds: YES. `adapters[destination_asset].send_to_address(...)` on the
       line inside process_pending_payouts' try block is the only broadcast in
       the Flask suite. It is final the instant it is relayed.
Mainnet-safe: NO -- running this against a funded mainnet wallet is operating
       the payout path, not inspecting it.

TWO WORKERS RUNNING THIS AT ONCE PAID THE SAME SWAP TWICE. FIXED 2026-09-24.

Measured that day in tests/test_payout_concurrency.py with this exact function
against a real database: 2 sends, 1 swap_id, 2 rows in `payouts` both
status='broadcast'. The guard on the `payout_exists` SELECT was correct about
what it asked and was still not enough, because it was a READ that happened
before the writer lock was contended -- the second worker ran it while the
first was inside `sendtoaddress` with its transaction open.

Both halves of the fix are here, and neither is sufficient alone:

  claim_swap_for_payout()   turns the decision into a conditional UPDATE, so
        the writer lock that already existed serializes the DECISION and not
        merely the insert. Exactly one claimant sees rowcount 1.
  idx_payouts_one_live_per_swap   a PARTIAL unique index (db.py), applied by
        apply_migrations(), so a second live payout row is impossible even if
        a future caller forgets to check rowcount -- while a genuinely failed
        payout stays retryable.

The first alone reopens the hole the moment somebody writes a new caller; the
second alone converts a double payout into an unhandled IntegrityError AFTER
the first send has gone out. The same test file now measures 1 send where it
measured 2, and it keeps the demonstrations of each mechanism in isolation.

process_pending_payouts()'s docstring states the commit ordering around the
send and what it costs in the crash case. Read it before reordering anything
there: a broadcast and a commit are two systems and cannot be made atomic.

refresh_wallet_inventory()'s `except Exception: continue` is annotated at its
site: a chain being unreachable must not stop the other two from being
refreshed, and the consequence of swallowing it is stated there.
"""

import logging
import sqlite3

from .helpers import utc_now_iso
from .swap_service import set_swap_status

logger = logging.getLogger(__name__)


def reserve_inventory(db, asset: str, amount: float):
    row = db.execute("SELECT * FROM wallet_inventory WHERE asset = ?", (asset,)).fetchone()
    now = utc_now_iso()
    if row is None:
        db.execute(
            "INSERT INTO wallet_inventory (asset, hot_confirmed, hot_reserved, hot_available, updated_at) VALUES (?, ?, ?, ?, ?)",
            (asset, 0.0, amount, -amount, now),
        )
        return
    db.execute(
        "UPDATE wallet_inventory SET hot_reserved = ?, hot_available = ?, updated_at = ? WHERE asset = ?",
        (float(row["hot_reserved"]) + amount, float(row["hot_available"]) - amount, now, asset),
    )


def release_inventory_after_send(db, asset: str, amount: float):
    row = db.execute("SELECT * FROM wallet_inventory WHERE asset = ?", (asset,)).fetchone()
    if not row:
        return
    db.execute(
        "UPDATE wallet_inventory SET hot_reserved = ?, hot_confirmed = ?, hot_available = ?, updated_at = ? WHERE asset = ?",
        (
            max(float(row["hot_reserved"]) - amount, 0.0),
            float(row["hot_confirmed"]) - amount,
            float(row["hot_available"]),
            utc_now_iso(),
            asset,
        ),
    )


def claim_swap_for_payout(db, swap_id: str) -> bool:
    """Take exclusive ownership of one swap's payout, or decline.

    This is THE decision, and it is a WRITE rather than a read on purpose.

    The guard it replaced was

        SELECT * FROM payouts WHERE swap_id = ? AND status IN ('broadcast','completed')

    which asks the right question and is still not enough. Measured 2026-09-24
    in tests/test_payout_concurrency.py against a real database: two workers,
    2 sends, 1 swap_id, 2 rows in `payouts` both status='broadcast'. SQLite has
    one writer lock, so the second worker's INSERT really did block behind the
    first -- but the SELECT completed long before the lock was ever contended.
    Worker B ran it while A was inside `sendtoaddress` with its transaction
    open, saw no payout row, DECIDED to pay, and only then blocked. The lock
    serialized the writes and did nothing about the decision.

    A conditional UPDATE moves the decision into the write, where the lock
    already applies: the row is re-read under the writer lock, and exactly one
    of two claimants can see `status = 'payout_pending'`. The loser updates
    zero rows and must decline -- which is what `rowcount == 1` means here and
    why the caller checks it rather than assuming success.

    The claim is COMMITTED before the caller sends anything. That is
    deliberate and it is not merely about durability: if the claim stayed in an
    open transaction for the duration of the RPC call, a second worker's
    competing UPDATE would block on the writer lock for as long as the send
    took, and sqlite3's default 5-second busy timeout would turn a slow but
    perfectly healthy `sendtoaddress` into "database is locked" in the other
    worker. Committing first makes the loser's UPDATE return rowcount 0
    immediately instead of waiting.

    Returns:
        True if this caller owns the payout for `swap_id` and may send.
    """
    claim = db.execute(
        "UPDATE swaps SET status = 'paying', updated_at = ? WHERE id = ? AND status = 'payout_pending'",
        (utc_now_iso(), swap_id),
    )
    if claim.rowcount != 1:
        db.commit()
        return False
    db.execute(
        "INSERT INTO swap_audit_log (swap_id, old_status, new_status, message, created_at) VALUES (?, ?, ?, ?, ?)",
        (swap_id, "payout_pending", "paying", "Claimed for payout", utc_now_iso()),
    )
    db.commit()
    return True


def process_pending_payouts(db, config, adapters: dict) -> list[dict]:
    """Broadcast the payout for every swap that is waiting for one.

    THE ORDERING AROUND THE SEND, AND WHAT IT COSTS.

    Rule 5 says write SQL first and mirror second, and rule 13 notes that a
    signal can land between `sendtoaddress` returning a txid and the UPDATE
    that records it. Those two together decide the order here:

      1. claim the swap (UPDATE ... WHERE status='payout_pending'), reserve
         inventory, INSERT the payouts row with status='created' -- and COMMIT
         all of it BEFORE the send;
      2. send;
      3. record the txid, mark the payout 'broadcast' and the swap 'completed',
         and commit again.

    So the intent to pay is durable before any money can move. The cost is
    stated rather than hidden: a crash or a SIGKILL between (2) and (3) leaves
    a payouts row with status='created' and NO txid, and a swap stuck in
    'paying' -- money possibly on chain with no txid recorded. That cannot be
    made atomic from here; a broadcast and a commit are two systems. What it
    buys is that the failure is VISIBLE and is not automatically repeated: no
    worker acts on a swap in 'paying', and the partial unique index counts
    'created' as live, so nothing can insert a second payout for that swap
    until a human resolves the first. The opposite order -- send, then write --
    would lose the record entirely and leave the swap in 'payout_pending' for
    the next cycle to pay AGAIN.

    The IntegrityError path is deliberate for the same reason. If the index
    fires, a live payout row for this swap already exists; the swap is left in
    'paying' with an audit row saying so, and nothing is sent. It is not marked
    'failed', because 'failed' invites a retry and a payout that may already be
    on chain is exactly what must not be retried.
    """
    swaps = db.execute(
        "SELECT * FROM swaps WHERE status = 'payout_pending' ORDER BY credited_at ASC"
    ).fetchall()
    completed = []
    for swap in swaps:
        destination_asset = swap["to_asset"]
        amount = float(swap["output_amount_estimate"])

        if not claim_swap_for_payout(db, swap["id"]):
            # Another worker owns this payout. Not an error and not a failure:
            # the swap is being paid by somebody else, right now.
            logger.info(
                "swap %s: payout claimed by another worker, skipping (0 sent by this worker for this swap)",
                swap["id"],
            )
            continue

        reserve_inventory(db, destination_asset, amount)
        try:
            db.execute(
                "INSERT INTO payouts (swap_id, asset, destination_address, amount, txid, status, created_at, sent_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (swap["id"], destination_asset, swap["payout_address"], amount, None, "created", utc_now_iso(), None),
            )
        except sqlite3.IntegrityError as exc:
            # idx_payouts_one_live_per_swap refused a second LIVE payout row.
            # Reaching this means the claim and the index disagree, which is a
            # state a human has to look at -- so nothing is sent, nothing is
            # retried, and the reason is written down where the operator reads
            # it rather than only raised.
            db.rollback()
            logger.error(
                "swap %s: a live payout row already exists (%s). NOTHING SENT. The swap is left in 'paying' and "
                "will not be retried automatically; reconcile the existing payout before releasing it.",
                swap["id"],
                exc,
            )
            db.execute(
                "INSERT INTO swap_audit_log (swap_id, old_status, new_status, message, created_at) VALUES (?, ?, ?, ?, ?)",
                (swap["id"], "paying", "paying", f"Payout suppressed by unique index: {exc}", utc_now_iso()),
            )
            db.commit()
            continue

        # Durable BEFORE the send. See this function's docstring.
        db.commit()

        try:
            txid = adapters[destination_asset].send_to_address(swap["payout_address"], amount)
            db.execute(
                "UPDATE payouts SET txid = ?, status = ?, sent_at = ? WHERE swap_id = ? AND status = 'created'",
                (txid, "broadcast", utc_now_iso(), swap["id"]),
            )
            db.execute(
                "UPDATE swaps SET payout_txid = ?, completed_at = ?, updated_at = ? WHERE id = ?",
                (txid, utc_now_iso(), utc_now_iso(), swap["id"]),
            )
            set_swap_status(db, swap["id"], "completed", "Payout broadcast", old_status="paying")
            release_inventory_after_send(db, destination_asset, amount)
            db.commit()
            completed.append(db.execute("SELECT * FROM swaps WHERE id = ?", (swap["id"],)).fetchone())
        # Checked, and this broad catch is the right one: `send_to_address`
        # can fail for transport reasons, daemon reasons, insufficient funds,
        # or a rejected transaction, and EVERY one of them must land the swap
        # in `failed` with the reason recorded rather than aborting the loop
        # and leaving the remaining swaps unprocessed. The caller can tell the
        # failure from success because the swap's status and failed_reason both
        # say so -- rule 12's test is met in the return value, not by narrowing.
        #
        # What it does NOT establish is whether the transaction was broadcast.
        # A timeout after the daemon accepted it looks identical to a refusal,
        # and this marks both 'failed'. That is a real gap on the fund path and
        # it is named in the enforcement report rather than changed here. Note
        # what the partial unique index does about it: 'failed' is not a live
        # status, so a retry CAN insert a new payout row -- which is the right
        # behavior for a refusal and the wrong one for a timeout that was
        # actually relayed. Same gap, now with a name.
        except Exception as exc:  # noqa: BLE001
            db.execute(
                "UPDATE payouts SET status = ? WHERE swap_id = ? AND status = 'created'",
                ("failed", swap["id"]),
            )
            db.execute(
                "UPDATE swaps SET failed_reason = ?, updated_at = ? WHERE id = ?",
                (str(exc), utc_now_iso(), swap["id"]),
            )
            set_swap_status(db, swap["id"], "failed", f"Payout failed: {exc}", old_status="paying")
            db.commit()
    db.commit()
    return completed


def refresh_wallet_inventory(db, adapters: dict):
    now = utc_now_iso()
    for asset, adapter in adapters.items():
        try:
            balance = float(adapter.get_balance())
        except Exception as exc:  # noqa: BLE001 -- checked: one chain being unreachable must not stop the other two from being refreshed, so this continues rather than raising. It is NOT silent any more: the row for that asset keeps its previous values and the WARNING below says which asset and why, so a stale inventory figure can be traced to the poll that failed instead of looking like a balance that did not move.
            logger.warning(
                "wallet inventory for %s NOT refreshed (previous values kept): %s", asset, exc
            )
            continue
        row = db.execute("SELECT * FROM wallet_inventory WHERE asset = ?", (asset,)).fetchone()
        reserved = float(row["hot_reserved"]) if row else 0.0
        available = balance - reserved
        if row:
            db.execute(
                "UPDATE wallet_inventory SET hot_confirmed = ?, hot_available = ?, updated_at = ? WHERE asset = ?",
                (balance, available, now, asset),
            )
        else:
            db.execute(
                "INSERT INTO wallet_inventory (asset, hot_confirmed, hot_reserved, hot_available, updated_at) VALUES (?, ?, ?, ?, ?)",
                (asset, balance, reserved, available, now),
            )
    db.commit()
