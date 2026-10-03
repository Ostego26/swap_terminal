#!/usr/bin/env python3
"""Re-drive a swap whose payout was REFUSED BEFORE SIGNING. Sends nothing itself.

Role: file (root entry point; the decision is rescue_verdict() below)
Reads: swap_terminal.db -- swaps, payouts, wallet_inventory
Writes: swap_terminal.db, and only with --apply: the swap's status back to
       'payout_pending', the standing inventory reservation released, and an audit
       row. Dry run writes nothing.
Can move funds: NOT DIRECTLY -- it signs nothing, broadcasts nothing and calls no
       send method. It hands a swap back to workers/payout_worker.py, which then
       broadcasts. That is a fund-path change and the operator's to ask for
       (CLAUDE.md rule 16); this file refuses every swap it cannot prove was never
       broadcast.
Mainnet-safe: it chooses no network and opens no socket.

WHY THIS EXISTS, MEASURED 2026-10-03.

The operator's first GRC -> SOL swap credited 10 GRC and then failed:

    status          failed
    recorded reason this adapter cannot sign or broadcast a Solana transfer, and
                    holds no key that could.
    payout rows     1 ... status failed  txid (none)

That string does not exist anywhere in the current tree. The payout worker was
running code loaded BEFORE the SOL payout was armed, and
`supervisor.py start` reported "ALREADY RUNNING ... nothing was spawned" two lines
after printing "a payout worker CAN broadcast on GRC, SOL, XRP" -- a capability
claim about the code on disk, beside a did-nothing line about the process. Rule 13's
exact failure, and it cost a swap.

'failed' is deliberately never retried, because a payout that MIGHT already be on
chain must not be re-sent. So the deposit was credited and nothing could move the
swap forward. Operator instruction: "godamit. let's rescue the swap."

WHAT MAKES THIS SAFE, AND WHERE THE PROOF STOPS.

"failed with no txid" IS NOT PROOF OF NO BROADCAST, and that is the whole hazard
this file is built around. A send can raise AFTER the node accepted the
transaction -- a socket timeout on sendTransaction leaves the money moving and the
code in an exception handler. So the refusal must be proven to have happened BEFORE
anything was signed, and that proof comes from three independent places:

  the payout row's STATUS   db.PAYOUT_LIVE_STATUSES is ('created','broadcast',
                            'completed'). A 'created' row with no txid is exactly
                            the crash-between-send-and-record case
                            payout_service's own docstring warns about: money
                            possibly on chain. ANY live row refuses the rescue,
                            with or without a txid.
  a txid ON ANY ROW         refuses outright. A txid is a broadcast.
  the RECORDED REASON       must match a refusal that happens before signing.
                            PRE_SIGNING_MARKERS below is that list, and a reason
                            this file does not recognize REFUSES rather than
                            assuming. An unrecognized reason is not evidence of
                            safety; it is absence of evidence, and rule 2's
                            distinction applies: "I could not find a broadcast" is
                            not "there was no broadcast".

THE STANDING RESERVATION IS THE PART THAT WOULD HAVE BEEN MISSED.
services/payout_service.py commits the claim, the inventory reservation and the
payouts row BEFORE the send, and release_inventory_after_send() is called only on
the correction path -- NOT on a failed send. Measured 2026-10-03: the failed
attempt therefore leaves its amount standing in wallet_inventory.hot_reserved. Hand
the swap back without releasing it and payout_worker reserves the same amount a
second time, which silently halves the hot wallet's apparent availability for every
future payout. So --apply releases it in the same transaction as the status change.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "swap_terminal"))

from config import Config
from db import PAYOUT_LIVE_STATUSES, db_session
from microfortnights import format_duration
from report_block import labeled
from services.helpers import utc_now_iso

SELF = "rescue_payout.py"

#: Fragments of swaps.failed_reason that PROVE the refusal happened before anything
#: was signed. Matched case-insensitively as substrings.
#:
#: EACH ONE IS A REFUSAL RAISED BEFORE THE SIGNING CALL, verified against the
#: raising site rather than inferred from the wording:
#:
#:   "holds no key that could"     the pre-arming SOL adapter's refusal. The exact
#:                                 string the operator's failed swap recorded, from
#:                                 code that imported nothing able to sign.
#:   SolanaSendNotArmed            chains/solana_signing.require_send_confirmation()
#:                                 raises before load_payout_keypair() is reached,
#:                                 so the key file is not even opened.
#:   "cannot pay out"              chains/registry.why_cannot_pay_out()'s verdict,
#:                                 raised by create_swap/payout before any adapter
#:                                 send method is called.
#:   SolanaClusterRefused          require_devnet() refuses after ONE getGenesisHash
#:                                 and before any balance read, blockhash or submit.
#:
#: DELIBERATELY NOT HERE, and each absence is the point:
#:   SolanaWireMismatch            raised AFTER signing. Nothing was submitted on
#:                                 that path either, but the signed bytes exist and
#:                                 a rescue should be a person's decision, not this
#:                                 file's.
#:   a timeout, a transport error, a bare exception
#:                                 the case this whole file is cautious about. Money
#:                                 may be on chain.
#:   "pynacl is not importable"    ADDED 2026-10-03, after the operator's rescue
#:                                 re-drove the swap and it failed on this instead.
#:                                 PyNaCl is an OPTIONAL dependency on purpose, so a
#:                                 host without it refuses rather than crashing a
#:                                 read-only deposit watcher at import.
#:
#:                                 PROVABLY PRE-BROADCAST AT BOTH RAISE SITES,
#:                                 checked rather than inferred from the wording:
#:                                 chains/solana_signing._public_key_bytes():527 is
#:                                 reached from derive_and_check(), which runs BEFORE
#:                                 sign_message() in signed_transfer_wire(); and
#:                                 sign_message():716 is the signing call itself, so
#:                                 if it raises there are no signed bytes to submit.
#:                                 Either way nothing reached a cluster.
PRE_SIGNING_MARKERS = (
    "holds no key that could",
    "solanasendnotarmed",
    "cannot pay out",
    "solanaclusterrefused",
    "pynacl is not importable",
)


def rescue_verdict(swap, payout_rows) -> tuple[bool, str]:
    """May this swap be handed back to the payout worker? The decision, as a function.

    Returns (allowed, reason). The reason is printed either way, because a refusal
    an operator cannot read is a refusal they will route around.

    ORDER IS CHEAPEST-AND-MOST-FATAL FIRST, the same shape
    chains/solana_signing.signed_transfer_wire() uses: status, then any txid, then
    the recorded reason. The first refusal is the one reported, so an operator acts
    on one fact rather than three.
    """
    if swap["status"] != "failed":
        return False, (
            f"the swap is '{swap['status']}', not 'failed'. This tool exists for a swap whose payout "
            f"was refused before signing; anything else is either already moving or waiting on a person"
        )
    live = [row for row in payout_rows if row["status"] in PAYOUT_LIVE_STATUSES]
    if live:
        statuses = ", ".join(sorted({row["status"] for row in live}))
        return False, (
            f"{len(live)} payout row(s) are LIVE ({statuses}). A live row means a payout was claimed and "
            f"may be ON CHAIN -- a 'created' row with no txid is exactly the crash-between-send-and-record "
            f"case. Re-driving could double-send. Prove the payment's absence or presence on the "
            f"destination chain first; settle_payout.py is the tool when it WAS delivered"
        )
    with_txid = [row for row in payout_rows if row["txid"]]
    if with_txid:
        return False, (
            f"{len(with_txid)} payout row(s) carry a txid ({with_txid[0]['txid']}). A txid is a "
            f"broadcast. Nothing here may re-drive a swap that has one"
        )
    reason = (swap["failed_reason"] or "").lower()
    if not reason:
        return False, (
            "swaps.failed_reason is empty, so there is no evidence about WHEN the refusal happened. "
            "Absence of a recorded reason is not evidence that nothing was broadcast"
        )
    matched = [marker for marker in PRE_SIGNING_MARKERS if marker in reason]
    if not matched:
        return False, (
            f"the recorded reason is not one this tool can prove happened BEFORE signing, so it refuses: "
            f"a send can raise AFTER the node accepted the transaction. Read the reason and decide by "
            f"hand. Recognized markers are {', '.join(PRE_SIGNING_MARKERS)}. The reason recorded was: "
            f"{(swap['failed_reason'] or '')[:200]}"
        )
    return True, (
        f"the recorded reason matches {matched[0]!r}, which is raised BEFORE anything is signed, and no "
        f"payout row is live or carries a txid. This swap was provably never broadcast"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog=SELF,
        description="Hand a swap whose payout was refused before signing back to the payout worker.",
    )
    parser.add_argument("--swap", required=True, help="the swap id to re-drive")
    parser.add_argument("--db", default="", help=f"database (default: {Config.DB_PATH})")
    parser.add_argument("--apply", action="store_true",
                        help="actually write. Without it nothing is written and the checks still run.")
    args = parser.parse_args(argv)
    started = time.monotonic()
    db_path = args.db or Config.DB_PATH

    print(f"{SELF}: {'APPLY -- rows WILL be written' if args.apply else 'DRY RUN -- nothing is written'}",
          flush=True)
    print(f"  database   {db_path}", flush=True)
    print(f"  swap       {args.swap}", flush=True)
    print("  this tool SIGNS NOTHING and BROADCASTS NOTHING. It hands the swap to payout_worker, which "
          "does.", flush=True)

    with db_session(str(db_path)) as db:
        swap = db.execute("SELECT * FROM swaps WHERE id = ?", (args.swap,)).fetchone()
        if swap is None:
            print(f"  REFUSED: no swap with id {args.swap} exists in {db_path}", flush=True)
            return 2
        rows = db.execute("SELECT * FROM payouts WHERE swap_id = ? ORDER BY id", (args.swap,)).fetchall()
        print(f"  status     {swap['status']}", flush=True)
        print(f"  payout rows {len(rows)}"
              + ("".join(f"\n               {r['amount']} {r['asset']} status={r['status']} "
                         f"txid={r['txid'] or '(none)'}" for r in rows) or "  (none)"), flush=True)

        allowed, reason = rescue_verdict(swap, rows)
        print(f"  verdict    {'ALLOWED' if allowed else 'REFUSED'} -- {reason}", flush=True)
        if not allowed:
            print(labeled("done in", format_duration(time.monotonic() - started)), flush=True)
            return 3

        asset, amount = swap["to_asset"], float(swap["output_amount_estimate"])
        inventory = db.execute("SELECT * FROM wallet_inventory WHERE asset = ?", (asset,)).fetchone()
        reserved = float(inventory["hot_reserved"]) if inventory else 0.0
        print(f"  reservation {asset} hot_reserved={reserved}  <- the failed attempt reserved {amount} "
              f"and nothing released it; --apply releases that much so the worker does not reserve it "
              f"twice", flush=True)

        if not args.apply:
            print(f"\nDRY RUN: nothing written. To re-drive it:\n"
                  f"    python3 {SELF} --swap {args.swap} --apply", flush=True)
            print(labeled("done in", format_duration(time.monotonic() - started)), flush=True)
            return 0

        # utc_now_iso(), NOT utc_now(). The first version used utc_now(), which returns
        # a datetime -- so every timestamp went into SQLite through the default
        # datetime adapter, which Python 3.12 deprecates and which renders
        # "2026-10-03 11:52:00+00:00" with a SPACE where every other row in this
        # database has a "T". The operator saw three DeprecationWarnings on their
        # 3.12 host; this container is 3.11 and printed none, which is why it
        # shipped. services/helpers.py has both functions and the rest of the tree
        # uses the _iso one.
        now = utc_now_iso()
        # ONE TRANSACTION for the release and the status change. Either both land or
        # neither does: a released reservation on a swap still 'failed' loses the
        # record of money owed, and a 'payout_pending' swap with the reservation
        # still standing is the double-reservation this exists to prevent.
        db.execute(
            "UPDATE wallet_inventory SET hot_reserved = MAX(hot_reserved - ?, 0), "
            "hot_available = hot_confirmed - MAX(hot_reserved - ?, 0), updated_at = ? WHERE asset = ?",
            (amount, amount, now, asset),
        )
        db.execute(
            "UPDATE swaps SET status = 'payout_pending', failed_reason = ?, updated_at = ? "
            "WHERE id = ? AND status = 'failed'",
            (f"re-driven by {SELF} on {now}; previous refusal: {swap['failed_reason']}", now, args.swap),
        )
        db.execute(
            # COLUMN NAMES READ OFF THE SCHEMA, NOT RECALLED. The first version of this
            # INSERT said (from_status, to_status, reason) and the table is
            # (old_status, new_status, message) -- db.py:311. That is the second
            # column-name guess to miss in this session; the first cost the operator a
            # query that died with "no such column: reason".
            "INSERT INTO swap_audit_log (swap_id, old_status, new_status, message, created_at) "
            "VALUES (?, 'failed', 'payout_pending', ?, ?)",
            (args.swap, f"{SELF}: {reason}", now),
        )
        db.commit()
        print("  WROTE      status -> payout_pending, reservation released, audit row written", flush=True)
        print("  next       payout_worker picks it up on its next cycle. Watch it:", flush=True)
        print(f"               python3 show_swap.py --swap {args.swap}", flush=True)
    print(labeled("done in", format_duration(time.monotonic() - started)), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
