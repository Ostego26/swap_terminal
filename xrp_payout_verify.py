#!/usr/bin/env python3
"""Run the ADAPTER's armed payout path against testnet, once, to prove it works.

Role: file (entry point; the operator runs this)
Reads: ~/.config/swap_terminal/keys/xrp-testnet-*.json, and the testnet rippled
Writes: nothing on disk. Submits ONE XRP Ledger payment when --send is passed.
Can send orders: YES with --send, and ONLY to the XRP TESTNET -- the endpoint is
      pinned in this file and XRPAdapter refuses any server whose network_id says
      mainnet, before it reads an account and long before it signs.
Mainnet-safe: yes in the sense that it cannot reach mainnet. There is no flag
      that points it there; doing so means editing this file AND defeating the
      adapter's network check, which reads the SERVER's id rather than the URL.

WHY THIS EXISTS, and why xrp_send_tagged.py does not already cover it.

xrp_send_tagged.py proved that LOCAL SIGNING works: on 2026-09-26 it put a real
tagged Payment on the testnet ledger (5534F6CC...). But it signs with its own
code -- it builds the Payment and calls submit_and_wait itself. The ADAPTER,
chains/xrp.py::send_to_address(), is different code with different guards, and it
has never signed anything against any ledger. Every test of it uses seeded
responses and a stubbed submit. By rule 16's own test the armed half is a
PROPOSAL, not a fix, and this script is the one thing that changes that.

So this deliberately goes through the adapter. If it works, the claim "the XRP
payout path works" becomes a measurement instead of an inference.

THE SEED NEVER APPEARS ANYWHERE A HUMAN OR A LOG CAN SEE IT. It is read out of
the faucet file this script already knows how to find, handed straight to the
adapter as an argument, and never printed, never put in argv, never logged. It
is not a command-line option ON PURPOSE: a secret in argv is visible in `ps` to
every process on the box and lands in shell history.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "swap_terminal"))

from chains.xrp import XRPAdapter
from chains.xrp_signing import CONFIRM_XRP_SEND

# Imported rather than re-implemented (rule 8). This file and xrp_send_tagged.py
# read the SAME faucet files, and a second copy of that lookup is how the two
# would come to disagree about where the faucet puts a secret -- which is exactly
# the bug xrp_send_tagged.py shipped and had to fix on 2026-09-26.
from xrp_send_tagged import saved_faucet_accounts

TESTNET_URL = "https://s.altnet.rippletest.net:51234/"


def arming_arguments(send: bool, seed: str) -> tuple[str, str]:
    """The (seed, confirm_send) pair to hand the adapter. The one decision here.

    Extracted rather than written inline in main() because it is the only thing
    in this file that decides whether money moves, and rule 10 puts the deciding
    thing at the bottom where it can be called with seeded inputs and asserted on
    directly. Inline, the only way to test it would be to run a payment.

    BOTH are withheld on a preview, not just the token. Withholding only the
    token would still hand a live seed to a code path that, one editing mistake
    later, might use it -- and the adapter is explicit that an armed send needs
    both, so supplying one is never useful on its own. Defense that costs nothing
    is worth having on the one path in this repo that signs.
    """
    if not send:
        return "", ""
    return seed, CONFIRM_XRP_SEND


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Prove the XRP ADAPTER's armed payout path against testnet. Previews unless --send."
    )
    parser.add_argument("--send", action="store_true",
                        help="actually sign and submit. Without it this previews and refuses.")
    parser.add_argument("--amount", type=float, default=1.0, help="XRP to send (default 1.0)")
    parser.add_argument("--tag", type=int, default=None,
                        help="destination tag to set on the payment (default none)")
    args = parser.parse_args()

    print("xrp payout verify -- exercises chains/xrp.py::send_to_address(), TESTNET only", flush=True)
    print(f"  endpoint   {TESTNET_URL}", flush=True)
    print("  WHAT THIS PROVES that nothing else does: the ADAPTER's armed path has", flush=True)
    print("  never signed against a real ledger. xrp_send_tagged.py proved local", flush=True)
    print("  signing works, but it signs with its own code; this goes through the", flush=True)
    print("  adapter's guards. Until this runs with --send, that half is a PROPOSAL.", flush=True)

    accounts = saved_faucet_accounts()
    print(f"\n  saved faucet accounts: {len(accounts)}", flush=True)
    if len(accounts) < 2:  # noqa: PLR2004 -- two: one pays, one is paid
        print("\nREFUSED: two funded testnet accounts are needed (one pays, one is paid).", file=sys.stderr)
        print("    python3 fund_testnets.py --xrp     # run it twice", file=sys.stderr)
        return 1

    source_path, source, seed = accounts[0]
    destination = accounts[1][1]
    print(f"    from  {source}  (seed read from {source_path.name}, never printed)", flush=True)
    print(f"    to    {destination}", flush=True)

    adapter = XRPAdapter(url=TESTNET_URL, min_confirmations=1)
    send_seed, confirm = arming_arguments(args.send, seed)
    try:
        tx_hash = adapter.send_to_address(
            destination, args.amount,
            source=source,
            seed=send_seed,
            destination_tag=args.tag,
            confirm_send=confirm,
        )
    except Exception as error:  # noqa: BLE001 -- checked: the adapter raises FOUR distinct refusal types plus signing and transport errors, and the caller cannot tell them apart from a return value because every one of them means "nothing was sent". Nothing is swallowed: the type name and the message are both printed and the exit code is non-zero, so a refusal can never read as a success.
        print(f"\n{type(error).__name__}: {error}", flush=True)
        if args.send:
            print("\nNOT SENT. The adapter refused before signing; read the reason above.", flush=True)
            return 1
        print("\nThat refusal is the DEFAULT and is correct: this was a preview.", flush=True)
        print("Re-run with --send to arm it.", flush=True)
        return 0

    print(f"\nDELIVERED and validated.  hash={tx_hash}", flush=True)
    print("\n  The adapter's armed payout path has now run against a real ledger.", flush=True)
    print("  Confirm the money arrived, from the other side:", flush=True)
    print(f"    python3 xrp_chain_check.py --account {destination}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
