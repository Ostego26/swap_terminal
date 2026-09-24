"""Create a swap from a quote, and read one back.

Role: submodule -> function (create_swap is the decision)
Reads: swap_terminal.db (quotes, swaps, deposit_events, payouts), the
       destination adapter (validateaddress) and the source adapter
       (getnewaddress)
Writes: swap_terminal.db (swaps, swap_audit_log)
Can move funds: no broadcast. It DERIVES a deposit address in the hot wallet
       and fixes the payout address, and it sets min_confirmations from
       config -- the threshold that later decides when a payout is released.
Mainnet-safe: yes

set_swap_status() writes the status and the audit row together, so every
transition is recorded. It does NOT commit: the caller owns the transaction
boundary, which is what lets create_swap() insert the swap and its first audit
row atomically.
"""

from .helpers import new_id, parse_iso, utc_now_iso


def get_min_confirmations(config, asset: str) -> int:
    return int(config[f"{asset}_MIN_CONFIRMATIONS"])


def set_swap_status(db, swap_id: str, new_status: str, message: str | None = None, old_status: str | None = None):
    current = old_status
    if current is None:
        row = db.execute("SELECT status FROM swaps WHERE id = ?", (swap_id,)).fetchone()
        current = row["status"] if row else None
    db.execute(
        "UPDATE swaps SET status = ?, updated_at = ? WHERE id = ?",
        (new_status, utc_now_iso(), swap_id),
    )
    db.execute(
        "INSERT INTO swap_audit_log (swap_id, old_status, new_status, message, created_at) VALUES (?, ?, ?, ?, ?)",
        (swap_id, current, new_status, message, utc_now_iso()),
    )


def get_quote_or_raise(db, quote_id: str) -> dict:
    quote = db.execute("SELECT * FROM quotes WHERE id = ?", (quote_id,)).fetchone()
    if not quote:
        raise ValueError("Quote not found")
    if parse_iso(quote["expires_at"]) <= parse_iso(utc_now_iso()):
        raise ValueError("Quote expired")
    return quote


def create_swap(db, config, adapters: dict, quote_id: str, payout_address: str) -> dict:
    quote = get_quote_or_raise(db, quote_id)
    to_asset = quote["to_asset"]
    from_asset = quote["from_asset"]
    payout_address = payout_address.strip()
    if not adapters[to_asset].validate_address(payout_address):
        raise ValueError(f"Invalid {to_asset} payout address")
    swap_id = new_id("s")
    deposit_address = adapters[from_asset].get_new_address(f"swap_{swap_id}")
    now = utc_now_iso()
    swap = {
        "id": swap_id,
        "quote_id": quote["id"],
        "from_asset": from_asset,
        "to_asset": to_asset,
        "deposit_address": deposit_address,
        "payout_address": payout_address,
        "expected_input_amount": float(quote["input_amount"]),
        "actual_input_amount": None,
        "quoted_rate": float(quote["quoted_rate"]),
        "fee_bps": int(quote["fee_bps"]),
        "network_fee_reserve": float(quote["network_fee_reserve"]),
        "output_amount_estimate": float(quote["output_amount_estimate"]),
        "status": "awaiting_deposit",
        "min_confirmations": get_min_confirmations(config, from_asset),
        "deposit_txid": None,
        "payout_txid": None,
        "created_at": now,
        "updated_at": now,
        "credited_at": None,
        "completed_at": None,
        "expires_at": quote["expires_at"],
        "failed_reason": None,
    }
    db.execute(
        """
        INSERT INTO swaps (
            id, quote_id, from_asset, to_asset, deposit_address, payout_address,
            expected_input_amount, actual_input_amount, quoted_rate, fee_bps,
            network_fee_reserve, output_amount_estimate, status, min_confirmations,
            deposit_txid, payout_txid, created_at, updated_at, credited_at,
            completed_at, expires_at, failed_reason
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            swap["id"], swap["quote_id"], swap["from_asset"], swap["to_asset"], swap["deposit_address"],
            swap["payout_address"], swap["expected_input_amount"], swap["actual_input_amount"], swap["quoted_rate"],
            swap["fee_bps"], swap["network_fee_reserve"], swap["output_amount_estimate"], swap["status"],
            swap["min_confirmations"], swap["deposit_txid"], swap["payout_txid"], swap["created_at"],
            swap["updated_at"], swap["credited_at"], swap["completed_at"], swap["expires_at"], swap["failed_reason"],
        ),
    )
    db.execute(
        "INSERT INTO swap_audit_log (swap_id, old_status, new_status, message, created_at) VALUES (?, ?, ?, ?, ?)",
        (swap_id, None, "awaiting_deposit", "Swap created", now),
    )
    db.commit()
    return swap


def get_swap(db, swap_id: str) -> dict | None:
    swap = db.execute("SELECT * FROM swaps WHERE id = ?", (swap_id,)).fetchone()
    if not swap:
        return None
    deposit_events = db.execute(
        "SELECT * FROM deposit_events WHERE swap_id = ? ORDER BY id ASC",
        (swap_id,),
    ).fetchall()
    payouts = db.execute(
        "SELECT * FROM payouts WHERE swap_id = ? ORDER BY id ASC",
        (swap_id,),
    ).fetchall()
    swap["deposit_events"] = deposit_events
    swap["payouts"] = payouts
    return swap
