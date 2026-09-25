"""Watch the source chain for a swap's deposit and credit it when confirmed.

Role: submodule -> function (refresh_swap_from_chain is the decision)
Reads: the source chain adapter (listtransactions, getrawtransaction),
       swap_terminal.db (swaps, deposit_events)
Writes: swap_terminal.db (deposit_events, swaps, swap_audit_log)
Can move funds: no broadcast here -- but this is the module that decides a
       deposit is CONFIRMED and moves the swap to `payout_pending`, which is
       the state payout_worker.py broadcasts against. The confirmation
       comparison on the `confirmations >= min_confirmations` line is the
       gate between "somebody sent us coins" and "we send coins back", and a
       confirmations value that is wrong in the low direction stalls a swap
       forever while one that is wrong in the high direction releases a payout
       against an unconfirmed deposit.
Mainnet-safe: yes; read-only with respect to the chain.

The amount tolerance (AMOUNT_TOLERANCE_PCT) sends an out-of-range deposit to
`under_review` rather than crediting or refunding it. That is the right
default: a human decides what happens to a deposit that does not match its
quote.

TWO ROWS FOR ONE (asset, txid) ARE NOW WARNED ABOUT, AND NOTHING ELSE CHANGED.

refresh_swap_from_chain() sums EVERY deposit_events row for the swap, and
db.py's UNIQUE(asset, txid, vout) lets one transaction contribute several rows.
Summing them is correct when they are several real outputs, and it is a DOUBLE
COUNT when one of them is the vout=0 row that
chains/base._extract_matching_vouts() fabricated on every Core 22+ deposit
before 2026-09-25. deposit_vout_artifact.py carries the full mechanism and the
measurement; the short version is that the double count lands the swap in
`under_review` -- a halt rather than a wrong payout, but a swap that has
stopped moving.

Nothing here can tell a fabricated row from a genuine second output, so nothing
here tries. The warning below is a DIAGNOSTIC: it changes no figure, no
threshold and no status, and refresh_swap_from_chain() computes and decides
exactly what it did before. What it removes is the silence -- a condition that
doubles a credited amount had no way of announcing itself, and every instance
of this artifact so far was found by somebody already looking for it.

Resolving the rows is a one-time migration (migrate_deposit_vouts.py at the
repository root), not a permanent branch in this function. A branch here that
skipped or preferred one of the rows would be rule 19's patch: it stops the
symptom being reported instead of stopping the cause existing, and it would
still be running years after the last fabricated row was deleted -- silently
suppressing a genuine two-output deposit.
"""

import logging

# Rootless, the same way chains/base.py reaches script_pub_key.py. No
# sys.path.insert is needed here: this module is only ever importable as
# `services.deposit_service`, which already requires swap_terminal/ to be on
# sys.path for the `services` package itself to resolve.
from deposit_vout_artifact import multi_vout_groups

from .helpers import utc_now_iso
from .swap_service import set_swap_status

logger = logging.getLogger(__name__)

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


def warn_on_multi_vout_rows(swap_id: str, rows) -> None:
    """Say so when one (asset, txid) contributes more than one row to the sum.

    Called with the rows refresh_swap_from_chain() has ALREADY selected, so it
    costs no extra query -- which is why the grouping is the in-memory
    multi_vout_groups() rather than the table-wide SQL beside it in
    deposit_vout_artifact.py. The two are asserted to agree in
    tests/test_deposit_vout_artifact.py, because two implementations of one
    rule agree on the day they are written and drift from then on (rule 8).

    Returns None and touches nothing. It is a diagnostic and has no say in what
    gets credited: extracting it into its own function is what keeps that
    visible, since a caller can see at the call site that the return value is
    not used.

    Vouts are OUTPUT INDICES and confirmations are COUNTS. Neither is ever
    rendered in microfortnights (rule 6); only the durations in this system are.

    HOW OFTEN THIS CAN FIRE, measured from the tree rather than guessed, because
    a warning that repeats forever is one an operator learns to scroll past.
    refresh_swap_from_chain() is reached only through process_active_swaps(),
    which selects ACTIVE_STATUSES -- awaiting_deposit, deposit_seen,
    confirming. Two loops call it: deposit_watcher at DEFAULT_POLL_SECONDS=15
    and reconcile_worker at 60, so an affected swap warns about five times a
    minute WHILE IT IS ACTIVE. It does not stay active: the double count sends
    it to under_review, which is not in ACTIVE_STATUSES, so the swap leaves the
    polled set and the warnings stop on their own. A legitimate two-output
    deposit warns for the same short window and then reaches payout_pending,
    which is also outside ACTIVE_STATUSES. Neither case produces an unbounded
    stream.
    """
    for (asset, txid), group in multi_vout_groups(rows).items():
        vouts = ", ".join(str(int(row["vout"])) for row in group)
        total = sum(float(row["amount"]) for row in group)
        logger.warning(
            "swap %s: transaction %s contributes %d deposit_events rows at vout %s, totalling %.8f %s, and ALL of "
            "them are summed  <- correct if they are several real outputs; a DOUBLE COUNT if one is the vout=0 row "
            "fabricated before 2026-09-25. Nothing here can tell those apart. Run migrate_deposit_vouts.py to see "
            "the rows and decide.",
            swap_id,
            txid,
            len(group),
            vouts,
            total,
            asset,
        )


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
    # Diagnostic only, and placed here rather than lower down so that it is
    # read against the two sums immediately below it -- those are the lines the
    # warning is about. Its return value is unused on purpose (see the
    # function's docstring): nothing about the credit decision depends on it.
    warn_on_multi_vout_rows(swap["id"], rows)
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
    # The f-string interpolates a run of '?' generated from the LENGTH of
    # ACTIVE_STATUSES -- structure, not input. The statuses themselves are
    # bound as parameters on the line below. That is what the suppression
    # claims and it is what a reviewer can check from this line (rule 12's
    # S608 note: "a reviewer should be able to see which from the line").
    placeholders = ",".join("?" for _ in ACTIVE_STATUSES)
    swaps = db.execute(
        f"SELECT * FROM swaps WHERE status IN ({placeholders}) ORDER BY created_at ASC",  # noqa: S608
        ACTIVE_STATUSES,
    ).fetchall()
    processed = [refresh_swap_from_chain(db, config, adapters, swap) for swap in swaps]
    db.commit()
    return processed
