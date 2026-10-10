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
from db import db_session
from microfortnights import format_duration
from report_block import labeled

# RE-EXPORTED ON PURPOSE, 2026-10-10. These three moved to services/payout_rescue.py
# because the web container has no root tools in it (docker/web.Dockerfile copies
# swap_terminal/ and nothing else), so a route could not import a gate that lived here.
#
# They stay importable from THIS module because it is still this tool's public surface:
# tests/test_rescue_payout.py imports them from here, and so would anybody who found the
# gate by reading the tool that uses it. A re-export is the one honest way to move an
# implementation without moving its callers.
#
# `noqa: F401` IS A CLAIM AND HERE IS WHAT WAS CHECKED (rule 19): ruff is right that this
# module does not USE refused_before_signing or PRE_SIGNING_MARKERS -- rescue_verdict()
# does, inside the service. They are imported for re-export alone. Verified by running
# `python3 -c "import rescue_payout; rescue_payout.refused_before_signing('')"` and by
# tests/test_rescue_payout.py, which imports all three from this name.
from services.payout_rescue import (  # noqa: F401
    PRE_SIGNING_MARKERS,
    apply_rescue,
    refused_before_signing,
    rescue_verdict,
)

SELF = "rescue_payout.py"

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
        # THE THREE WRITES MOVED TO services/payout_rescue.apply_rescue() ON 2026-10-10,
        # when the operator panel grew a Rescue button and there were suddenly two callers.
        # They were spelled here, including the comment explaining why the reservation has
        # to be released in the same transaction as the status change -- that reasoning is
        # now at the function, which is where the next reader of either caller will find it.
        #
        # ONE IMPLEMENTATION IS NOT A TIDY-UP HERE. The three statements have to happen
        # together or the swap is left either owing money with no reservation or holding a
        # reservation nothing will release. Two copies of that, drifting, is rule 8's bug
        # with a delay on it on the one path where the delay costs a double payout.
        outcome = apply_rescue(db, swap, reason, actor=SELF)
        db.commit()
        # THE FIGURE, NOT A SENTENCE SAYING A FIGURE EXISTS. This printed "reservation
        # released" with no amount; apply_rescue() returns what it actually released, so
        # the line now carries the number an operator would otherwise go and query for
        # (rule 14: state what the number means, next to the number). `moved` is the
        # guarded UPDATE's rowcount -- 0 means another caller got there first, which the
        # web button made a real race rather than a theoretical one, and that must not
        # render the same as a rescue that worked.
        print(
            f"  WROTE      status -> payout_pending ({'moved' if outcome['moved'] else 'ALREADY MOVED by '
            'another caller -- nothing changed'}), released {outcome['released']} "
            f"{outcome['asset']} of reservation, audit row written",
            flush=True,
        )
        print("  next       payout_worker picks it up on its next cycle. Watch it:", flush=True)
        print(f"               python3 show_swap.py --swap {args.swap}", flush=True)
    print(labeled("done in", format_duration(time.monotonic() - started)), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
