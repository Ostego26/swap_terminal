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

TWO WORKERS RUNNING THIS AT ONCE PAY THE SAME SWAP TWICE. Measured 2026-09-24
in tests/test_payout_concurrency.py with this exact function against a real
database: 2 sends, 1 swap_id, 2 rows in `payouts` both status='broadcast'. The
guard on the `payout_exists` SELECT is correct about what it asks and is still
not enough, because it is a READ that happens before the writer lock is
contended -- the second worker runs it while the first is inside
`sendtoaddress` with its transaction open. The fix (claim-by-UPDATE plus a
partial unique index on payouts) is demonstrated in that test file and is a
PROPOSAL for the operator, because it changes the payout path.

refresh_wallet_inventory()'s `except Exception: continue` is annotated at its
site: a chain being unreachable must not stop the other two from being
refreshed, and the consequence of swallowing it is stated there.
"""

import logging

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


def process_pending_payouts(db, config, adapters: dict) -> list[dict]:
    swaps = db.execute(
        "SELECT * FROM swaps WHERE status = 'payout_pending' ORDER BY credited_at ASC"
    ).fetchall()
    completed = []
    for swap in swaps:
        destination_asset = swap["to_asset"]
        amount = float(swap["output_amount_estimate"])
        payout_exists = db.execute(
            "SELECT * FROM payouts WHERE swap_id = ? AND status IN ('broadcast','completed')",
            (swap["id"],),
        ).fetchone()
        if payout_exists:
            continue
        reserve_inventory(db, destination_asset, amount)
        db.execute(
            "INSERT INTO payouts (swap_id, asset, destination_address, amount, txid, status, created_at, sent_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (swap["id"], destination_asset, swap["payout_address"], amount, None, "created", utc_now_iso(), None),
        )
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
            set_swap_status(db, swap["id"], "completed", "Payout broadcast", old_status="payout_pending")
            release_inventory_after_send(db, destination_asset, amount)
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
        # it is named in the enforcement report rather than changed here.
        except Exception as exc:  # noqa: BLE001
            db.execute(
                "UPDATE payouts SET status = ? WHERE swap_id = ? AND status = 'created'",
                ("failed", swap["id"]),
            )
            db.execute(
                "UPDATE swaps SET failed_reason = ?, updated_at = ? WHERE id = ?",
                (str(exc), utc_now_iso(), swap["id"]),
            )
            set_swap_status(db, swap["id"], "failed", f"Payout failed: {exc}", old_status="payout_pending")
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
