#!/usr/bin/env python3
"""Run the ADAPTER's armed payout path against testnet, once, to prove it works.

Role: file (entry point; the operator runs this)
Reads: ~/.config/swap_terminal/keys/xrp-testnet-*.json in the default mode, the
      ENVIRONMENT (XRP_PAYOUT_SECRET_SEED) and Config (XRP_DEPOSIT_ACCOUNT) with
      --via-service, and the testnet rippled either way
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

IT RAN, 2026-09-26, and the claim is now a measurement:

    TransactionResult tesSUCCESS  validated=True  waited 5.6µfn (6.8s)
    hash=E118CA9636765E5F5954A49800E207BEB8AC50BCA603BBD8F5CECB2477EFD8E8

This file is kept rather than deleted as a completed one-off (rule 2 would
otherwise ask). It is the regression check for the armed path: every future
change to send_to_address(), to the derivation guard or to the reserve
arithmetic can be re-proved against a real ledger by running it again, which no
test in tests/ can do -- they all stub the socket.

THE SEED NEVER APPEARS ANYWHERE A HUMAN OR A LOG CAN SEE IT. It is read out of
the faucet file this script already knows how to find, or out of the environment
with --via-service, handed straight to the adapter as an argument, and never
printed, never put in argv, never logged. It is not a command-line option ON
PURPOSE: a secret in argv is visible in `ps` to every process on the box and
lands in shell history.

--via-service: THE SAME PROOF ONE LAYER UP, ADDED 2026-10-02

The default mode above calls chains/xrp.py::send_to_address() directly, with a
seed from a faucet file. That proves the ADAPTER works and it proves nothing
about the thing a customer's swap actually goes through, which is
services/payout_service.broadcast_payout(): the function that decides a chain
needs a source account, reads XRP_PAYOUT_SECRET_SEED out of the environment, and
passes the arming token. Until 2026-10-02 that function did not exist and the
call site passed two positional arguments, so every XRP payout refused and the
swap landed in `failed` with the deposit already credited.

--via-service runs THAT function instead. It reads the seed from
XRP_PAYOUT_SECRET_SEED and the paying account from XRP_DEPOSIT_ACCOUNT exactly as
the payout worker does -- through services/swap_service.payout_source_account(),
the same authority, not a second copy of the lookup (rule 8) -- so what it
exercises is the worker's path with the worker's configuration.

WHAT IT DOES NOT TAKE FROM THE WORKER IS THE ENDPOINT, deliberately. The url
stays pinned to chains/xrp_testnet.TESTNET_URL and ignores XRP_RPC_URL, because
this file's header promises there is no flag that points it at mainnet and
reading an operator's url would be exactly such a flag. The adapter's own
network_id refusal still runs on top of that, so two independent things would
have to be wrong.

THE PREVIEW IS STILL THE DEFAULT. Without --send this resolves the account,
reports whether the seed is present WITHOUT reading its value, and then calls
send_to_address() with the source and NO seed -- which prints the full plan and
refuses. That is the same refusal a forgotten opt-in gets, so what the operator
reads before arming is what the guard produces rather than a rehearsal of it.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "swap_terminal"))

from chains.xrp import XRPAdapter
from chains.xrp_payout_seed import SIGNING_SEED_ENV_VAR, signing_seed_is_present
from chains.xrp_signing import CONFIRM_XRP_SEND, XRPSendNotArmed

# Imported rather than re-implemented (rule 8). This file and xrp_send_tagged.py
# read the SAME faucet files, and a second copy of that lookup is how the two
# would come to disagree about where the faucet puts a secret -- which is exactly
# the bug xrp_send_tagged.py shipped and had to fix on 2026-09-26.
from chains.xrp_testnet import TESTNET_URL, saved_faucet_accounts
from services.payout_service import broadcast_payout
from services.swap_service import payout_source_account
from workers.common import get_config_dict

# The endpoint is chains/xrp_testnet.TESTNET_URL -- one spelling for every
# XRP script, so refuse_mainnet() and the submit below cannot end up asking
# two different servers (rule 8).


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


def report_preview_refusal(error: BaseException) -> int:
    """Say whether a preview's refusal was the EXPECTED one, and return the exit code.

    THE DEFECT THIS FIXES, MEASURED IN THIS CONTAINER 2026-10-02. Both preview paths
    in this file printed, unconditionally:

        ConnectionError: ('Connection aborted.', ConnectionResetError(104, ...))

        That refusal is the DEFAULT and is correct: this was a preview.

    It was not correct and it was not the default. The testnet is unreachable from
    here, so NO guard ran at all -- and the script said the outcome was the expected
    one and exited 0. That is rule 14's "make did-nothing look different from did
    work" and rule 13's "treat skipped plus success in the same output as a defect in
    the output", in the one script whose entire job is establishing whether a payout
    path works.

    TWO INSTANCES, BOTH FIXED (rule 19: when you find N, fix them). The faucet mode
    and --via-service had the same four lines; they now share this function.

    THE EXPECTED REFUSAL IS XRPSendNotArmed AND ONLY THAT. A preview withholds the
    arming token, so that is the guard it is supposed to land on, and reaching it
    proves everything before it ran: the addresses decoded, the server answered, the
    network was not mainnet, the balance was read and the reserve fit. Any other
    exception means the preview stopped EARLIER than the arming check and established
    less than it looks like it did -- a mainnet refusal, a reserve shortfall and a
    reset connection are three different problems and none of them is "working as
    intended".
    """
    print(f"\n{type(error).__name__}: {error}", flush=True)
    if isinstance(error, XRPSendNotArmed):
        print("\nThat refusal is the DEFAULT and is correct: this was a preview, and reaching the", flush=True)
        print("arming check means every guard before it ran -- addresses, network, balance, reserve.", flush=True)
        print("Re-run with --send to arm it.", flush=True)
        return 0
    print("\n*** THIS IS NOT THE PREVIEW REFUSAL ***  The run stopped BEFORE the arming check, so", flush=True)
    print("nothing above it was established. Read the exception: a mainnet id, a reserve shortfall", flush=True)
    print("and an unreachable endpoint are three different problems. Nothing was sent either way.", file=sys.stderr)
    return 1


def via_service(amount: float, destination: str, send: bool) -> int:
    """Run the PAYOUT WORKER's own path: broadcast_payout(), its config, its environment.

    Added 2026-10-02. See --via-service in this module's docstring for why the
    default mode does not cover this: it calls the adapter directly, and the thing a
    customer's swap goes through is services/payout_service.broadcast_payout().

    THE ACCOUNT AND THE SEED COME FROM WHERE THE WORKER GETS THEM, through the same
    functions and not through copies of the lookups (rule 8):

      the account   services/swap_service.payout_source_account(), reading
                    XRP_DEPOSIT_ACCOUNT out of the config dict
                    workers/common.get_config_dict() builds -- which is literally the
                    dict the payout worker passes in.
      the seed      chains/xrp_payout_seed.signing_seed(), called inside
                    broadcast_payout(). NOTHING IN THIS FILE READS ITS VALUE. This
                    function asks signing_seed_is_present(), which returns a bool, so
                    the seed cannot be printed from here even by accident.

    THE ENDPOINT IS PINNED AND XRP_RPC_URL IS IGNORED, which is the one place this
    deliberately differs from the worker. Reading the operator's url would be a flag
    that points this file at mainnet, and its header promises there is none.

    Returns an exit code. A preview returns 0 because the refusal IS the expected
    outcome; a --send that refused returns 1 because then it is a failure.
    """
    print("\n  via-service mode: this runs services/payout_service.broadcast_payout(),", flush=True)
    print("  the function a customer's swap actually goes through. Endpoint is pinned", flush=True)
    print(f"  to {TESTNET_URL} and XRP_RPC_URL is ignored.", flush=True)

    config = get_config_dict()
    try:
        source = payout_source_account(config, "XRP")
    except ValueError as error:
        print(f"\nREFUSED: {error}", file=sys.stderr)
        return 1

    # Rule 14: echo the parameters that decide the answer, and say what the number
    # MEANS next to it. Presence only -- never the value, and never its length.
    present = signing_seed_is_present()
    print(f"    from      {source}  (XRP_DEPOSIT_ACCOUNT; the account this DEBITS)", flush=True)
    print(f"    to        {destination}", flush=True)
    print(f"    amount    {amount} XRP", flush=True)
    # Two plain variables rather than a conditional inside the f-string: a newline
    # inside an f-string EXPRESSION is a syntax error before Python 3.12 (PEP 701),
    # and this repository's declared ruff target is py312 while hosts still run 3.11
    # -- so `ruff check` passes it and `python3 -m compileall` is what catches it.
    # Measured here, on 3.11, which is why this is written out.
    state = "SET" if present else "NOT SET"
    meaning = (
        "SET is not CORRECT; a wrong seed is refused by derive_and_check() before signing"
        if present
        else "so every payout refuses before signing. Export it to arm this."
    )
    print(f"    seed      {SIGNING_SEED_ENV_VAR} is {state}  <- {meaning}", flush=True)

    adapter = XRPAdapter(url=TESTNET_URL, min_confirmations=1)
    if not send:
        # The PREVIEW, produced by the guard rather than rehearsed: source and no
        # seed is exactly what a forgotten opt-in looks like, so what prints here is
        # what the refusal prints.
        try:
            adapter.send_to_address(destination, amount, source=source)
        except Exception as error:  # noqa: BLE001 -- checked: the adapter raises four distinct refusal types plus signing and transport errors, every one of them meaning "nothing was sent", and a caller cannot tell them apart from a return value. Nothing is swallowed: report_preview_refusal() prints the type and the message and DISTINGUISHES the expected refusal from every other one.
            return report_preview_refusal(error)
        print("\n*** NOTHING REFUSED, WHICH SHOULD BE IMPOSSIBLE ***  send_to_address() was called", flush=True)
        print("with a source and NO seed, so it must raise XRPSendNotArmed. It returned instead.", file=sys.stderr)
        return 1

    try:
        tx_hash = broadcast_payout(adapter, "XRP", config, destination, amount)
    except Exception as error:  # noqa: BLE001 -- checked: same set as above plus PayoutSigningUnavailable and the ValueError from payout_source_account. All mean nothing was sent; the type name is printed and the exit code is non-zero, so a refusal can never read as a success.
        print(f"\n{type(error).__name__}: {error}", flush=True)
        print("\nNOT SENT. Read the reason above.", file=sys.stderr)
        return 1

    print(f"\nDELIVERED and validated through the worker's own path.  hash={tx_hash}", flush=True)
    print(f"    python3 xrp_chain_check.py --account {destination}", flush=True)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Prove the XRP ADAPTER's armed payout path against testnet. Previews unless --send."
    )
    parser.add_argument("--send", action="store_true",
                        help="actually sign and submit. Without it this previews and refuses.")
    parser.add_argument("--amount", type=float, default=1.0, help="XRP to send (default 1.0)")
    parser.add_argument("--tag", type=int, default=None,
                        help="destination tag to set on the payment (default none)")
    parser.add_argument("--via-service", action="store_true",
                        help="run services/payout_service.broadcast_payout() -- the worker's own path -- "
                             "reading XRP_PAYOUT_SECRET_SEED from the environment and XRP_DEPOSIT_ACCOUNT "
                             "from config, instead of the faucet files. Needs --to.")
    parser.add_argument("--to", default="",
                        help="the destination account, required with --via-service")
    args = parser.parse_args()

    if args.via_service:
        if not args.to:
            print("REFUSED: --via-service needs --to <account>. There is no faucet file to take a "
                  "destination from in this mode, and guessing one would send real testnet XRP to an "
                  "account nobody chose.", file=sys.stderr)
            return 1
        return via_service(args.amount, args.to.strip(), args.send)

    print("xrp payout verify -- exercises chains/xrp.py::send_to_address(), TESTNET only", flush=True)
    print(f"  endpoint   {TESTNET_URL}", flush=True)
    print("  WHAT THIS PROVES that nothing else does: the ADAPTER's armed path has", flush=True)
    print("  never signed against a real ledger. xrp_send_tagged.py proved local", flush=True)
    print("  signing works, but it signs with its own code; this goes through the", flush=True)
    print("  adapter's guards. First proved 2026-09-26: tesSUCCESS, validated, hash", flush=True)
    print("  E118CA96... -- so this is now a REGRESSION check, not a first proof.", flush=True)

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
        if args.send:
            print(f"\n{type(error).__name__}: {error}", flush=True)
            print("\nNOT SENT. The adapter refused before signing; read the reason above.", flush=True)
            return 1
        # SHARED with --via-service rather than repeated (rule 19: two instances of one
        # defect, both fixed). This used to print "That refusal is the DEFAULT and is
        # correct" for ANY exception, including the ConnectionError this container
        # produces -- so an unreachable endpoint exited 0 and read as a working guard.
        return report_preview_refusal(error)

    print(f"\nDELIVERED and validated.  hash={tx_hash}", flush=True)
    print("\n  The adapter's armed payout path has now run against a real ledger.", flush=True)
    print("  Confirm the money arrived, from the other side:", flush=True)
    print(f"    python3 xrp_chain_check.py --account {destination}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
