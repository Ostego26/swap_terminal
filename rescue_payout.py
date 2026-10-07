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
#:   "invalid amount (rpc code -3)"
#:                                 ADDED 2026-10-03, after the first BTC -> LTC swap
#:                                 recorded exactly this and the tool refused to
#:                                 re-drive it. The refusal was correct for its own
#:                                 reason -- an unrecognized message is absence of
#:                                 evidence -- and the evidence exists:
#:
#:                                 RPC CODE -3 IS RPC_TYPE_ERROR, raised by Bitcoin
#:                                 Core's AmountFromValue() while CONVERTING the
#:                                 `amount` parameter. It happens before the wallet
#:                                 is touched, before an input is selected, before a
#:                                 transaction exists -- so there are no signed
#:                                 bytes and nothing can have been relayed.
#:
#:                                 MEASURED, NOT REASONED. The operator ran
#:                                 createrawtransaction with the same amount class
#:                                 and got the identical -3 "Invalid amount" while
#:                                 producing NO transaction, and a valid amount on
#:                                 the same command returned a hex string. That is
#:                                 the parser failing in isolation, with the wallet
#:                                 not involved at all.
#:
#:                                 THE CODE IS PART OF THE MARKER on purpose.
#:                                 "invalid amount" alone could plausibly be some
#:                                 other daemon's wording for a post-broadcast
#:                                 condition; "(rpc code -3)" pins it to the
#:                                 parameter-conversion error, which is the thing
#:                                 that proves nothing was signed. chains/base.py
#:                                 includes the code in the message it records,
#:                                 which is why this can be matched at all.
#:
#:                                 AND THE CAUSE IS FIXED, so this marker is for
#:                                 swaps that failed BEFORE c36250f:
#:                                 chains/coin_amounts.fit_to_chain_precision() now
#:                                 fits the amount to the chain's eight decimals
#:                                 before the send, so a payout no longer reaches
#:                                 the parser with seventeen.
#:   "not set in this process's environment"
#:                                 ADDED 2026-10-07, after the first ICP deposit
#:                                 this system ever credited -- 1.00000000 ICP,
#:                                 block index 1, to the subaccount for
#:                                 s_ebb03e8dc1b96e1c -- had its GRC payout refuse
#:                                 and this tool correctly declined to re-drive it.
#:                                 The refusal was right for its own reason: an
#:                                 unrecognized message is absence of evidence. The
#:                                 evidence exists and is recorded here rather than
#:                                 inferred from the wording, which is what this
#:                                 list demands of every entry.
#:
#:                                 THE RAISE PRECEDES THE BODY, STRUCTURALLY.
#:                                 services/payout_service.py:828 is
#:                                 `with payout_unlock_context(destination_asset,
#:                                 adapter):` and broadcast_payout() is at :835,
#:                                 INSIDE it. payout_unlock_context() is a plain
#:                                 function returning a context manager -- not a
#:                                 @contextmanager generator -- so its
#:                                 `if not passphrase: raise` at :1111 fires while
#:                                 the `with` EXPRESSION is being evaluated, before
#:                                 any context manager exists and therefore before
#:                                 the body can be entered at all. There is no path
#:                                 from that raise to a send.
#:
#:                                 THE EXCEPTION TYPE IS CATEGORICALLY SEPARATE.
#:                                 PayoutUnlockUnavailable's own docstring: "no
#:                                 transaction was created, nothing reached any
#:                                 daemon". It exists as its own class precisely so
#:                                 it cannot be confused with a send that failed.
#:
#:                                 AND THE WORKER LOG SHOWS NO SEND BETWEEN THEM.
#:                                 On the operator's host: the amount-quantization
#:                                 line at 20:26:09,280, then `payout FAILED` at
#:                                 20:26:09,285. Five milliseconds and no RPC.
#:
#:                                 Three independent places, which is the standard
#:                                 this file's header sets. MATCHED ON THE
#:                                 ENVIRONMENT CLAUSE rather than on "GRC payouts
#:                                 need the wallet fully unlocked", because
#:                                 payout_service.py builds that sentence from
#:                                 `{asset}` and WALLET_UNLOCK_ASSETS can grow -- a
#:                                 marker naming GRC would silently stop matching
#:                                 the day a second chain joined, which is rule 8's
#:                                 drift in a safety check.
PRE_SIGNING_MARKERS = (
    "holds no key that could",
    "solanasendnotarmed",
    "cannot pay out",
    "solanaclusterrefused",
    "pynacl is not importable",
    "invalid amount (rpc code -3)",
    "not set in this process's environment",
)


def refused_before_signing(reason: str) -> str:
    """The PRE_SIGNING_MARKERS fragment this reason matches, or "" for none.

    EXTRACTED FROM rescue_verdict() ON 2026-10-03, when a marker was added and
    there was nowhere to test the matching with seeded inputs -- the comparison was
    a list comprehension inside a function that also needs a swap row and payout
    rows. CLAUDE.md rule 10 puts the thing that DECIDES at the bottom, callable on
    its own, and this is the decision the whole file turns on: whether a recorded
    failure is provably pre-broadcast.

    CASE-INSENSITIVE, matching the contract PRE_SIGNING_MARKERS documents. The
    reason text comes from a daemon or an adapter and its capitalization is not
    ours to rely on; the markers are written lowercase for that reason.

    "" RATHER THAN False, so the caller can name WHICH marker matched in its own
    sentence. An operator reading "matches 'invalid amount (rpc code -3)'" can go
    and check that claim; "matched: True" gives them nothing to check.
    """
    lowered = (reason or "").lower()
    for marker in PRE_SIGNING_MARKERS:
        if marker in lowered:
            return marker
    return ""


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
    matched = refused_before_signing(reason)
    if not matched:
        return False, (
            f"the recorded reason is not one this tool can prove happened BEFORE signing, so it refuses: "
            f"a send can raise AFTER the node accepted the transaction. Read the reason and decide by "
            f"hand. Recognized markers are {', '.join(PRE_SIGNING_MARKERS)}. The reason recorded was: "
            f"{(swap['failed_reason'] or '')[:200]}"
        )
    return True, (
        f"the recorded reason matches {matched!r}, which is raised BEFORE anything is signed, and no "
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
