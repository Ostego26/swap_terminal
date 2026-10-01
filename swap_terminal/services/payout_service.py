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

from chains.gridcoin_wallet_lock import GridcoinLockError, unlocked_for_payout
from modules.address_authority import check_address

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

        # THE BURN GUARD. Added 2026-09-27 at the operator's instruction ("make this burn
        # proof"), and it is the LAST place a bad address can be stopped: the next fund-path
        # statement in this function is adapter.send_to_address(), after which the money is
        # on a chain and nobody -- not us, not the customer, not the miner -- can spend it.
        # Not stolen. Not recoverable. Gone.
        #
        # The defect being closed: modules/address_network.is_valid_address() landed in
        # f805efa and NOTHING ON THE FUND PATH CALLED IT. An undecodable payout address went
        # straight through to the daemon.
        #
        # WHY NOT THAT FUNCTION, AND WHY THIS IS A TABLE LOOKUP INSTEAD. is_valid_address()
        # understands bech32, Bitcoin-alphabet base58check and XRP-alphabet base58check.
        # A Solana address is plain base58 of an ed25519 key with no checksum at all --
        # measured 2026-09-27, every valid SOL fixture in tests/valid_addresses.py returns
        # False from it. So the one-line version of this guard would refuse every Solana
        # payout the day a SOL pair is enabled, on a swap whose deposit is ALREADY OURS and
        # already credited. That is a worse outcome than
        # the burn: the burn costs one payout, the false refusal costs every customer of
        # that chain while their money sits in our wallet. modules/address_authority.py is
        # the per-asset table that avoids it.
        #
        # NO_VALIDATOR PASSES THROUGH, LOUDLY, for the same reason: refusing a chain we
        # cannot check IS that outage, arriving by the door a future chain comes in by.
        # tests/test_address_authority.py closes the hole at the other end by asserting
        # every asset this terminal can reach HAS a validator, so the gap fails the suite
        # instead of either burning money or stranding a payout.
        #
        # PLACED AFTER THE CLAIM AND BEFORE reserve_inventory(), which is not arbitrary.
        # Claiming first means exactly one worker owns this swap, so the refusal is written
        # once. Refusing before the reserve and before the INSERT means a refused payout
        # leaves NO reserved-inventory row and NO payouts row in 'created' -- the two
        # dangling states this function's own docstring is about. The swap lands in 'failed'
        # by the same route a failed send does (set_swap_status + failed_reason + audit),
        # because 'payout_pending' would be re-read and re-refused on every cycle forever,
        # which is rule 14's "did nothing must not look like did work" turned into a loop.
        verdict = check_address(destination_asset, swap["payout_address"])
        if verdict.refuses:
            db.execute(
                "UPDATE swaps SET failed_reason = ?, updated_at = ? WHERE id = ?",
                (f"payout address refused before send: {verdict.why}", utc_now_iso(), swap["id"]),
            )
            set_swap_status(
                db, swap["id"], "failed",
                f"NOTHING WAS SENT. Payout address refused: {verdict.why}", old_status="paying",
            )
            db.commit()
            # Rule 14: the refusal names the address AND the reason, on the screen the
            # operator is actually looking at. `swaps.failed_reason` reached nothing they
            # were watching on 2026-09-26, which is the measurement recorded at the
            # send-failure logger.error() further down this function.
            logger.error(
                "payout REFUSED BEFORE SENDING for swap %s (%s -> %s, %s %s): address %r is not a valid "
                "%s address -- %s  <- NOTHING was sent, no inventory was reserved, no payouts row was "
                "written, and the swap is now 'failed' and will NOT be retried. Money sent to this string "
                "would be unspendable by anybody.",
                swap["id"], swap["from_asset"], swap["to_asset"], amount, destination_asset,
                swap["payout_address"], destination_asset, verdict.why,
            )
            continue
        if verdict.unchecked:
            # Rule 14 again. `.unchecked` rather than `state == NO_VALIDATOR` since
            # 2026-09-27: there are TWO ways to pass without being verified, and the second
            # one is the one that actually happened. NO_VALIDATOR is "no validator for this
            # chain"; UNDETERMINED is "a validator ran and could not place the address",
            # which is what Litecoin's regtest hrp `rltc` and its second P2SH byte 0x3A both
            # produced on the operator's live regtest swap. Both proceed, for the same reason
            # -- a gap in our tables is not evidence against a customer's address -- and both
            # must say out loud that nothing was checked.
            logger.warning(
                "payout for swap %s is going to a %s address that was NOT CHECKED (%s): %s  <- the send is "
                "proceeding, because refusing an address we cannot place would break a working chain, which "
                "is worse than the burn this guard prevents.",
                swap["id"], destination_asset, verdict.state, verdict.why,
            )

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
            recorded = False
            try:
                with payout_unlock_context(destination_asset, adapter):
                    txid = adapter.send_to_address(swap["payout_address"], amount)
                    # RECORDED INSIDE THE CONTEXT, BEFORE THE RE-LOCK CAN RAISE.
                    #
                    # This block sat AFTER the `with` until 2026-09-26, and the first
                    # real payout this code ever made is what found it. The send
                    # succeeded -- 55.52645238 GRC left the wallet, txid
                    # 3e09dc9cfd7a61da..., confirmed afterwards in the operator's own
                    # listtransactions -- and then the context's restore raised,
                    # because the staking unlock was being sent a timeout of 0 that
                    # Gridcoin refuses. Control jumped from the `with` straight to the
                    # except clause below, `txid` was discarded, and the swap was
                    # marked `failed` with `txid (none)`.
                    #
                    # Money out, no record: the single worst outcome available on this
                    # path, and it was caused by a wallet-housekeeping call that has
                    # nothing to do with whether the payment was delivered.
                    #
                    # The commit is what makes it durable, and `recorded` is set only
                    # after it returns -- so a database failure here is still a
                    # payout failure, while a LOCK failure after it is not.
                    _record_broadcast(db, swap, amount, txid)
                    recorded = True
            except GridcoinLockError:
                # THE PAYOUT IS ALREADY DURABLE. The wallet's lock state is a separate
                # problem with its own loud message (chains/gridcoin_wallet_lock.py
                # distinguishes "locked, not staking" from "may still be unlocked"),
                # and treating it as a payout failure is what mislabeled a delivered
                # payment. Re-raised when the send never got as far as being recorded,
                # because then it IS the payout's failure.
                if not recorded:
                    raise
                logger.exception(
                    "payout for swap %s WAS BROADCAST as %s and is recorded as completed. The wallet's "
                    "lock state could not be restored afterwards -- read the message above and act on "
                    "the wallet, NOT on the swap.",
                    swap["id"],
                    txid,
                )
                db.execute(
                    "INSERT INTO swap_audit_log (swap_id, old_status, new_status, message, created_at)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (
                        swap["id"],
                        "completed",
                        "completed",
                        f"payout broadcast {txid}; wallet lock restore FAILED afterwards",
                        utc_now_iso(),
                    ),
                )
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


# What the payouts row reads while the swap reads the key. The two move together in
# both paths; see _record_broadcast() for the run that proved a literal wrong here.
_PAYOUT_STATUS_BEFORE = {"paying": "created", "failed": "failed"}


def _record_broadcast(db, swap, amount, txid: str, *, old_status: str = "paying") -> None:
    """Make a delivered payout durable. Commits. Called INSIDE the unlock context.

    A function rather than inline, so the ordering that matters can be asserted
    directly (rule 10): everything here must be committed before the wallet's
    re-lock is attempted, because the re-lock can raise and a raised re-lock used to
    discard the txid of a payment that had already left the wallet.

    THE DESTINATION ASSET IS DERIVED HERE, NOT PASSED. It was a parameter until ruff
    put the count at six (PLR0913), and the honest fix was to remove an argument
    rather than to suppress the finding (rule 19). It is always swap["to_asset"] --
    payout_service.py:181 is the only place it was ever computed -- so passing it
    added a way for a caller to name one asset while handing over another swap's row,
    on the function that releases inventory against that asset. One fewer argument
    and one fewer disagreement.

    `old_status` IS KEYWORD-ONLY AND A PARAMETER BECAUSE THERE IS A SECOND CALLER,
    which is the one
    that made this function worth having. settle_payout.py corrects a swap whose
    payout was delivered but recorded as failed -- the exact record this function's
    original bug produced -- and for it the previous status is `failed`, not
    `paying`. Writing an audit row that claims `paying -> completed` for a swap that
    has been sitting in `failed` would falsify the one trail that explains the
    correction. Everything else it does is identical, including the inventory
    release, which the failure path never performed: a payout that failed left its
    reservation standing, so a correction that skipped release_inventory_after_send()
    would leave the hot wallet permanently short on paper.
    """
    destination_asset = swap["to_asset"]
    # THE PAYOUT ROW'S PRIOR STATUS IS DERIVED FROM THE SWAP'S, not filtered on a
    # literal. This line read `AND status = 'created'` and that was a bug with a
    # measurement behind it: on 2026-09-26 settle_payout.py corrected a swap whose
    # payout row the failure path had already moved to `failed`, so this UPDATE
    # matched NOTHING. The swap read `completed` with its txid while its payout row
    # still read `status failed  txid (none)` -- an internally inconsistent record,
    # which is worse than the one it was correcting.
    #
    # The two statuses move together in both paths and always have: the live path has
    # swap `paying` / payout `created`, and the failure path sets swap `failed` AND
    # payout `failed` (this file, the except clause above). So one mapping, in one
    # place, rather than a second parameter -- which also keeps the argument count
    # under PLR0913 without a suppression.
    #
    # It stays a FILTER rather than becoming an unconditional UPDATE, because a swap
    # can hold an old `failed` payout row beside a new `created` one after a retry,
    # and `WHERE txid IS NULL` alone would write the same txid onto both.
    payout_status_before = _PAYOUT_STATUS_BEFORE[old_status]
    db.execute(
        "UPDATE payouts SET txid = ?, status = ?, sent_at = ? WHERE swap_id = ? AND status = ? AND txid IS NULL",
        (txid, "broadcast", utc_now_iso(), swap["id"], payout_status_before),
    )
    db.execute(
        "UPDATE swaps SET payout_txid = ?, completed_at = ?, updated_at = ? WHERE id = ?",
        (txid, utc_now_iso(), utc_now_iso(), swap["id"]),
    )
    set_swap_status(db, swap["id"], "completed", "Payout broadcast", old_status=old_status)
    release_inventory_after_send(db, destination_asset, amount)
    db.commit()


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


def unlock_readiness_lines(configured_assets) -> list[str]:
    """Whether each payout-unlock chain CAN be unlocked, for a startup banner. PURE-ish.

    Reads os.environ and nothing else. Never returns the passphrase, never
    returns its length, and never claims it is CORRECT -- only that one is
    present. "Set" and "works" are different claims and this makes the weaker
    one (rule 17); a wrong passphrase still fails at the send, and saying
    otherwise here would be the reassuring answer rather than the measured one.

    WHY THIS EXISTS, MEASURED THREE TIMES ON 2026-10-01. The operator ran a
    devnet SOL -> testnet GRC rehearsal. All three times the whole pipeline
    worked -- memo attributed, deposit credited, swap advanced, payout worker
    claimed it -- and all three times the GRC leg died on

        GRIDCOIN_WALLET_PASSPHRASE is not set in this process's environment

    because the supervisor had been started from a shell without it. And all
    three times supervisor.py's start banner had said, immediately above the
    spawn:

        about to spawn    a payout worker CAN broadcast. Stop now if this
                          database is pointed at a funded mainnet wallet.

    Which was false in the direction that cost three rounds: it could not
    broadcast at all. The banner warned about the danger of succeeding while
    saying nothing about a guaranteed failure, and the environment is exactly
    the kind of parameter rule 14 says to echo -- "echo the parameters that
    decide the answer", because a worker inherits the shell that spawned it and
    nothing downstream can see which shell that was.

    `configured_assets` is PASSED IN rather than read from Config here, so the
    caller decides what "configured" means and this function cannot disagree
    with the chain lines printed beside it (rule 8). A chain that has no adapter
    gets no line: a passphrase warning for a chain nobody set up is the
    cried-wolf noise this file already fixed once for XRP's get_balance().
    """
    lines = []
    for asset in sorted(WALLET_UNLOCK_ASSETS & set(configured_assets)):
        if os.environ.get(WALLET_UNLOCK_ENV_VAR, ""):
            lines.append(
                f"  {asset} payout unlock  {WALLET_UNLOCK_ENV_VAR} IS set in this process, so a payout can "
                f"attempt the unlock. NOT a claim that it is the right passphrase -- a wrong one still "
                f"fails at the send."
            )
        else:
            lines.append(
                f"  {asset} payout unlock  *** {WALLET_UNLOCK_ENV_VAR} IS NOT SET *** so every {asset} payout "
                f"WILL refuse before sending and the swap will land in 'failed', which nothing retries. A "
                f"{asset} wallet unlocked for staking cannot send (rpc code -4). Export it in the shell that "
                f"starts this process; a value set in a file, or in another shell, does not reach here."
            )
    return lines
