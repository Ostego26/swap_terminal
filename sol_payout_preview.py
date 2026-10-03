#!/usr/bin/env python3
"""What one SOL payout WOULD do, with nothing signed and nothing sent.

Role: file (entry point) -> the decision is SolanaAdapter.preview_payout()
Reads: the Solana cluster -- getGenesisHash, getBalance, getAccountInfo and
       getMinimumBalanceForRentExemption, all read-only -- plus config.Config for
       the endpoint. No database. No .env, no secret, no keypair: it does not read
       SOL_PAYOUT_KEYPAIR_PATH and would not know what to do with it.
Writes: NOTHING. No database, no file, no transaction.
Can move funds: no, and not "no by default" -- there is no flag, argument or
       environment variable that makes this send. It never imports
       chains/solana_signing, never passes an arming token, and calls
       preview_payout() rather than send_to_address(). The preview path is the
       whole program.
Mainnet-safe: yes to RUN -- but it will refuse off devnet, because
       chains/solana_signing.require_devnet() runs on the preview path too and
       there is no flag that turns it off.

WHY THIS FILE EXISTS, 2026-10-03.

The operator armed the SOL payout and the next thing they needed was the smallest
amount that would actually be delivered. Measured that day: NO ENTRY POINT AT THE
REPOSITORY ROOT COULD PREVIEW A SOL PAYOUT. `grep -ln "preview_payout" *.py`
returned nothing. settle_payout.py corrects a swap, show_payout_fees.py reports on
payouts that already happened, swap_readiness.py answers whether a pair can
complete -- and none of them answers "what would this send do".

So the only way to see the plan was to let a real swap reach the payout worker,
which is exactly the wrong order for a path whose broadcast has never been
exercised: no transaction chains/solana.py builds has ever reached a cluster from
any environment. The first send should be read before it is made, and rule 10 puts
the thing an operator runs at the root where it can be found by looking.

WHAT IT ANSWERS, which is more than the rent floor:

  the cluster       by GENESIS HASH, not by the hostname in SOL_RPC_URL. A
                    hostname resolves to whatever DNS says today; a genesis hash
                    is the one thing a cluster cannot lie about.
  the floor         getMinimumBalanceForRentExemption for a 0-byte account, ASKED
                    rather than assumed, and whether this amount clears it.
  destination_exists
                    whether the account is already there. An existing account can
                    receive any amount; a NEW one cannot receive less than the
                    floor, and that is the whole hazard this preview exists for.
  the headroom      the payer's balance against the amount plus the fee plus
                    anything retained, so an operator sees whether the send would
                    leave the hot wallet unable to pay the next one.

IT IS NOT A DRY RUN OF A SEND. A dry run implies a switch that makes it real, and
there is none here: this program cannot sign, so "preview" is not a mode it is in.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "swap_terminal"))

from chains.registry import build_adapters, why_unconfigured
from chains.solana_units import LAMPORTS_PER_SOL, SOL_DECIMALS, amount_to_base_units
from config import Config
from microfortnights import format_duration
from report_block import labeled

SELF = "sol_payout_preview.py"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=SELF,
        description="What one SOL payout would do. Reads the cluster; signs nothing, sends nothing.",
    )
    parser.add_argument("--to", required=True, help="the destination account this payout would credit")
    parser.add_argument("--amount", required=True, type=float, help="the payout amount, in SOL")
    parser.add_argument("--retain", type=int, default=0,
                        help="lamports to keep in the payer beyond the fee (default 0)")
    return parser


def clears_the_floor(amount_sol: float, floor_lamports: int, destination_exists: bool) -> tuple[bool, str]:
    """Would this amount be DELIVERED? The decision, as a function (rule 10).

    An EXISTING account can receive any amount. A NEW one cannot receive less than
    the rent-exempt minimum -- the runtime refuses to create an account it would
    immediately have to reap -- so a payout below the floor to a new address is
    accepted by every other check and then not delivered.

    Returns (clears, sentence). The sentence says which case it is, because "yes"
    for an existing account and "yes" for an amount above the floor are different
    reasons and only one of them survives the destination being closed and
    reopened.
    """
    lamports = amount_to_base_units(amount_sol, SOL_DECIMALS)
    if destination_exists:
        return True, (
            f"YES -- the destination account already exists, so any amount is deliverable and the "
            f"{floor_lamports:,}-lamport floor does not apply to this send. IT WOULD APPLY if that "
            f"account were ever closed, so an amount above the floor is the durable answer"
        )
    if lamports >= floor_lamports:
        return True, (
            f"YES -- {lamports:,} lamports is at or above the {floor_lamports:,}-lamport rent-exempt "
            f"minimum, so the runtime will create the account"
        )
    return False, (
        f"NO -- {lamports:,} lamports is BELOW the {floor_lamports:,}-lamport rent-exempt minimum and "
        f"the destination does not exist yet, so the runtime would refuse to create the account. The "
        f"smallest deliverable payout to a new address here is "
        f"{floor_lamports / LAMPORTS_PER_SOL:.9f} SOL"
    )


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    started = time.monotonic()

    # RULE 14: the target and the scale BEFORE the work. Four read-only RPCs against
    # a cluster that may be slow is long enough that a blinking cursor is ambiguous.
    print(f"{SELF}: previewing a payout of {args.amount} SOL to {args.to}", flush=True)
    print(f"  endpoint {Config.RPC.get('SOL', {}).get('url') or '(SOL_RPC_URL unset)'}", flush=True)
    print("  reading the cluster: genesis hash, payer balance, destination, rent minimum. "
          "NOTHING is signed and NOTHING is sent.", flush=True)

    adapters = build_adapters(Config.RPC)
    adapter = adapters.get("SOL")
    if adapter is None:
        print(f"  REFUSED: {why_unconfigured('SOL', Config.RPC)}", flush=True)
        print(labeled("done in", format_duration(time.monotonic() - started)), flush=True)
        return 2

    try:
        plan = adapter.preview_payout(args.to, args.amount, retain_lamports=args.retain)
    except Exception as exc:  # noqa: BLE001 -- checked: every refusal on this path is a sentence an operator acts on (the cluster is not devnet, the address is malformed, the balance is short), and the class name is printed beside it so the reader can tell which guard fired. A traceback would bury the sentence.
        print(f"  REFUSED by {type(exc).__name__}: {exc}", flush=True)
        print(labeled("done in", format_duration(time.monotonic() - started)), flush=True)
        return 1

    print(flush=True)
    print(plan.get("description") or "  (the preview returned no description)", flush=True)

    floor = int(plan.get("rent_minimum_lamports") or 0)
    clears, sentence = clears_the_floor(args.amount, floor, bool(plan.get("destination_exists")))
    print(flush=True)
    print(f"  WOULD IT BE DELIVERED?  {sentence}", flush=True)
    print(f"  cluster={plan.get('cluster')}  signed={plan.get('signed')}  broadcast={plan.get('broadcast')} "
          f"<- signed and broadcast are False and this program cannot make them True", flush=True)
    print(labeled("done in", format_duration(time.monotonic() - started)), flush=True)
    # A NON-ZERO EXIT FOR AN UNDELIVERABLE AMOUNT, so a script or a careful operator
    # cannot read "the preview ran" as "the payout is fine". Rule 13: a run that did
    # nothing useful must not report the same way as one that did.
    return 0 if clears else 3


if __name__ == "__main__":
    raise SystemExit(main())
