from .helpers import utc_now_iso
from .swap_service import set_swap_status


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
        except Exception as exc:
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
        except Exception:
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
