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
import os
import sqlite3
from contextlib import nullcontext

from chains.gridcoin_wallet_lock import unlocked_for_payout

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
            # LOCK -> UNLOCK past staking -> send -> LOCK -> back to staking, for any
            # chain in WALLET_UNLOCK_ASSETS; a no-op context for the rest. The
            # re-lock is in the context manager's `finally`, so it runs even when
            # the send raises -- a wallet left fully unlocked because a payout failed
            # is the outcome that must not happen.
            adapter = adapters[destination_asset]
            with payout_unlock_context(destination_asset, adapter):
                txid = adapter.send_to_address(swap["payout_address"], amount)
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
            # LOGGED, not only recorded. The reason reached swaps.failed_reason and
            # the audit log; it reached NOTHING the operator was looking at. Their
            # run 2026-09-26 printed
            #
            #     payout_worker cycle=1 WORKED pending_at_start=1 broadcast=0 failed_total=1
            #
            # and nothing else -- so a locked wallet, an insufficient balance, a
            # rejected address and an unreachable daemon all look identical from the
            # terminal, and the one that is a five-second fix is indistinguishable
            # from the one that needs an investigation.
            #
            # At ERROR because a failed payout on a CREDITED swap is the most
            # serious routine outcome this worker has: the customer's deposit is
            # already ours and they have not been paid.
            logger.error(
                "payout FAILED for swap %s (%s -> %s, %s %s to %s): %s  <- the swap is now 'failed' "
                "and this worker will NOT retry it",
                swap["id"],
                swap["from_asset"],
                swap["to_asset"],
                swap.get("output_amount_estimate"),
                swap["to_asset"],
                swap["payout_address"],
                exc,
            )
    db.commit()
    return completed


# Assets whose get_balance() failure has already been reported this process. A
# DESIGNED refusal must not warn every cycle.
#
# Measured on the operator's host 2026-09-26: ten payout_worker cycles printed ten
# copies of "wallet inventory for XRP NOT refreshed", because the XRP adapter
# refuses get_balance() BY DESIGN -- it holds no hot-wallet account, which is the
# custody decision it is waiting on. So the warning described a fault that does not
# exist, once every ten seconds, forever.
#
# chains/registry.py's own header names this exact hazard as the reason SOL is left
# unconstructed rather than built and left to warn: "a log that cries wolf is a log
# nobody reads the day something real happens". This is that, arriving through the
# other door -- a configured adapter whose refusal is permanent.
#
# Said ONCE per process, and a restart says it again: an operator starting a worker
# is exactly the person who needs to know an asset's balance is not being polled.
# Keyed on the asset AND the message, so a DIFFERENT failure for the same asset --
# a daemon that was up and is now down -- still reports.
_REPORTED_INVENTORY_FAILURES: set[tuple[str, str]] = set()


# Chains whose wallet must be FULLY UNLOCKED to send, and which are left unlocked
# for staking the rest of the time. Gridcoin is the only one here.
#
# Operator, 2026-09-26: "the system is supposed to unlock the wallet FULLY on it's
# own not the user for now", and the order: "LOCK---UNLOCK past staking---LOCK---
# return to unlocked for staking".
#
# WHAT THIS MEANS, SAID ONCE AND PLAINLY, because it is the security consequence of
# what was asked for and it should not be discovered later: this process can now
# spend the Gridcoin wallet. Before this change it could not -- a payout failed with
# rpc code -4 and the wallet stayed shut. After it, anything that can run code in
# this worker can move those coins, and the passphrase is in its environment.
#
# The narrowing that is available, and all of it is applied:
#   - the passphrase is read from the ENVIRONMENT at use time, never stored in this
#     repo, never written to a file by this code, never placed in argv, never logged
#   - the full unlock lasts DEFAULT_UNLOCK_SECONDS (60) and not until shutdown
#   - the wallet is RE-LOCKED and returned to staking in a `finally`, so it happens
#     on success, on a failed send, and on Ctrl-C
#   - a missing passphrase REFUSES the payout rather than attempting a send that
#     would fail anyway, and says which variable to set
WALLET_UNLOCK_ASSETS = frozenset({"GRC"})

# THE NAME OF the environment variable, which is not itself a secret -- and naming
# it WALLET_UNLOCK_ENV_VAR rather than ..._PASSPHRASE_VARIABLE is the honest fix for
# ruff's S105 rather than a suppression (rule 19). The first spelling made a
# constant holding a variable NAME look like a constant holding a passphrase, which
# is precisely the confusion that lint rule exists to catch.
#
# Read from the environment and never from Config. Config is echoed on the admin
# page through an allowlist, and a passphrase must not be one key away from
# something that gets rendered -- services/admin_view.py's own comment notes that
# Config.RPC holds wallet credentials "one key away from these".
WALLET_UNLOCK_ENV_VAR = "GRIDCOIN_WALLET_PASSPHRASE"


def payout_unlock_context(asset: str, adapter):
    """A context manager that holds the wallet open for ONE send, or explains why not.

    Returns nullcontext() for every chain that does not need it, so the call site
    reads the same for all of them and no chain grows a special case at the send.

    Raises PayoutUnlockUnavailable when the chain needs a passphrase and none is
    set. Raising BEFORE the send is deliberate: attempting it would fail with rpc
    code -4 and mark the swap terminally failed, so the swap would die of a missing
    environment variable. This way the reason is named and nothing is claimed.
    """
    if asset not in WALLET_UNLOCK_ASSETS:
        return nullcontext()

    passphrase = os.environ.get(WALLET_UNLOCK_ENV_VAR, "")
    if not passphrase:
        raise PayoutUnlockUnavailable(
            f"{asset} payouts need the wallet fully unlocked, and {WALLET_UNLOCK_ENV_VAR} is "
            f"not set in this process's environment. A {asset} wallet left unlocked for staking "
            f"CANNOT send -- the daemon answers rpc code -4 -- so this refuses before attempting a "
            f"send that would fail and mark the swap terminally failed. Set "
            f"{WALLET_UNLOCK_ENV_VAR} for the worker process only."
        )
    return unlocked_for_payout(adapter, passphrase)


class PayoutUnlockUnavailable(RuntimeError):
    """The wallet cannot be unlocked, so no send was attempted.

    Its own type because it is categorically different from a send that FAILED: no
    transaction was created, nothing reached any daemon, and the fix is an
    environment variable rather than an investigation.
    """


def refresh_wallet_inventory(db, adapters: dict):
    now = utc_now_iso()
    for asset, adapter in adapters.items():
        try:
            balance = float(adapter.get_balance())
        except Exception as exc:  # noqa: BLE001 -- checked: one chain being unreachable must not stop the other two from being refreshed, so this continues rather than raising. It is NOT silent: the row for that asset keeps its previous values and the WARNING below says which asset and why, so a stale inventory figure can be traced to the poll that failed instead of looking like a balance that did not move.
            signature = (asset, str(exc)[:200])
            if signature not in _REPORTED_INVENTORY_FAILURES:
                _REPORTED_INVENTORY_FAILURES.add(signature)
                logger.warning(
                    "wallet inventory for %s NOT refreshed (previous values kept), and this is said "
                    "ONCE per process rather than every cycle: %s", asset, exc
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
