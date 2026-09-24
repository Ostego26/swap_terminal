from .helpers import utc_now_iso
from .swap_service import set_swap_status

ACTIVE_STATUSES = ("awaiting_deposit", "deposit_seen", "confirming")


def upsert_deposit_event(db, swap_id: str, asset: str, event: dict):
    existing = db.execute(
        "SELECT * FROM deposit_events WHERE asset = ? AND txid = ? AND vout = ?",
        (asset, event["txid"], int(event["vout"])),
    ).fetchone()
    now = utc_now_iso()
    if existing:
        db.execute(
            "UPDATE deposit_events SET confirmations = ?, last_seen_at = ? WHERE id = ?",
            (int(event["confirmations"]), now, existing["id"]),
        )
        return existing
    db.execute(
        """
        INSERT INTO deposit_events (
            swap_id, asset, txid, vout, address, amount, confirmations,
            first_seen_at, last_seen_at, credited_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            swap_id, asset, event["txid"], int(event["vout"]), event["address"],
            float(event["amount"]), int(event["confirmations"]), now, now, None,
        ),
    )
    return None


def refresh_swap_from_chain(db, config, adapters: dict, swap: dict) -> dict:
    asset = swap["from_asset"]
    adapter = adapters[asset]
    events = adapter.find_deposits_to_address(swap["deposit_address"])
    for event in events:
        upsert_deposit_event(db, swap["id"], asset, event)
    rows = db.execute(
        "SELECT * FROM deposit_events WHERE swap_id = ? ORDER BY id ASC",
        (swap["id"],),
    ).fetchall()
    seen_total = sum(float(row["amount"]) for row in rows)
    confirmed_total = sum(float(row["amount"]) for row in rows if int(row["confirmations"]) >= int(swap["min_confirmations"]))
    max_confirmations = max([int(row["confirmations"]) for row in rows], default=0)
    deposit_txid = rows[0]["txid"] if rows else None
    db.execute(
        "UPDATE swaps SET actual_input_amount = ?, deposit_txid = ?, updated_at = ? WHERE id = ?",
        (seen_total or None, deposit_txid, utc_now_iso(), swap["id"]),
    )
    expected = float(swap["expected_input_amount"])
    tolerance_pct = float(config["AMOUNT_TOLERANCE_PCT"])
    low = expected * (1 - tolerance_pct)
    high = expected * (1 + tolerance_pct)
    current_status = swap["status"]
    if rows and current_status == "awaiting_deposit":
        new_status = "deposit_seen" if max_confirmations <= 0 else "confirming"
        set_swap_status(db, swap["id"], new_status, "Deposit detected", old_status=current_status)
        current_status = new_status
    if rows and 0 < max_confirmations < int(swap["min_confirmations"]) and current_status in {"deposit_seen", "awaiting_deposit"}:
        set_swap_status(db, swap["id"], "confirming", "Deposit is confirming", old_status=current_status)
        current_status = "confirming"
    if confirmed_total > 0:
        if confirmed_total < low or confirmed_total > high:
            if current_status != "under_review":
                db.execute(
                    "UPDATE swaps SET failed_reason = ?, updated_at = ? WHERE id = ?",
                    (f"Confirmed amount {confirmed_total} outside tolerance for expected {expected}", utc_now_iso(), swap["id"]),
                )
                set_swap_status(db, swap["id"], "under_review", "Amount outside tolerance", old_status=current_status)
                current_status = "under_review"
        elif current_status in {"confirming", "deposit_seen", "awaiting_deposit"}:
            db.execute(
                "UPDATE swaps SET credited_at = ?, updated_at = ?, actual_input_amount = ? WHERE id = ?",
                (utc_now_iso(), utc_now_iso(), confirmed_total, swap["id"]),
            )
            db.execute(
                "UPDATE deposit_events SET credited_at = ? WHERE swap_id = ? AND credited_at IS NULL",
                (utc_now_iso(), swap["id"]),
            )
            set_swap_status(db, swap["id"], "payout_pending", "Deposit fully confirmed", old_status=current_status)
    db.commit()
    refreshed = db.execute("SELECT * FROM swaps WHERE id = ?", (swap["id"],)).fetchone()
    return refreshed


def process_active_swaps(db, config, adapters: dict) -> list[dict]:
    swaps = db.execute(
        f"SELECT * FROM swaps WHERE status IN ({','.join('?' for _ in ACTIVE_STATUSES)}) ORDER BY created_at ASC",
        ACTIVE_STATUSES,
    ).fetchall()
    processed = []
    for swap in swaps:
        processed.append(refresh_swap_from_chain(db, config, adapters, swap))
    db.commit()
    return processed
