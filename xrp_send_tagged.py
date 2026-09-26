#!/usr/bin/env python3
"""Send ONE tagged testnet XRP payment, to verify the deposit path end to end.

Role: file (operator test fixture, at the project root per CLAUDE.md rule 10)
Reads: a faucet secret from ~/.config/swap_terminal/keys/ (0600), and the XRPL
       testnet endpoint
Writes: nothing to disk. ONE transaction to the XRP TESTNET, and only with
       --send.
Can move funds: TESTNET XRP ONLY, which is worth nothing by construction and
       is handed out free by a faucet. The endpoint is pinned to
       s.altnet.rippletest.net and there is no flag that changes it; reaching
       mainnet would mean editing this file.
Mainnet-safe: it cannot reach mainnet. It also refuses to run against an
       account whose network_id says mainnet, checked before anything is sent.

WHY THIS EXISTS, AND WHY IT IS NOT IN chains/

xrp_chain_check.py has confirmed every field the adapter reads except one: a
DestinationTag on a payment addressed to an account we control. That is the
field the ENTIRE attribution scheme rests on -- it becomes `vout`, it is how a
deposit is matched to a swap -- and neither the docs nor a stranger's account
could supply it. Measured on the operator's own faucet account 2026-09-26: one
payment, from the faucet, no tag.

chains/xrp.py cannot sign, deliberately and structurally: it imports no signing
library and reads no key path, because paying XRP out of a hot wallet is a
custody decision for the operator (rule 16). That property is not weakened
here. This is a TEST FIXTURE at the project root, it signs nothing itself, and
the adapter remains unable to move money.

TWO WAYS TO SIGN, AND THIS TRIES THE FREE ONE FIRST

rippled's `submit` has a legacy form taking tx_json plus a `secret`, where the
SERVER signs. Public mainnet servers disable it; a public testnet server may
not. If it works, one tagged payment costs no dependency at all. If the server
refuses, it says so and the fallback is `pip install xrpl-py` -- reported
rather than attempted, because adding a dependency is worth a decision.

THE SECRET NEVER TOUCHES argv

It is read from the 0600 faucet file and placed in the POST body. Passing it on
a command line would put it in /proc for every user on the machine and in shell
history -- the same defect measured in chain_tx.sh on 2026-09-25, where a
canary appeared twice in `ps` output.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "swap_terminal"))

import requests
from chains.xrp_address import is_valid_classic_address
from chains.xrp_units import to_drops

TESTNET_URL = "https://s.altnet.rippletest.net:51234/"
KEY_DIRECTORY = Path.home() / ".config" / "swap_terminal" / "keys"
MAINNET_NETWORK_IDS = {0}


def rpc(method: str, params: dict) -> dict:
    """One rippled call. params is a LIST of one object; errors arrive as HTTP 200."""
    response = requests.post(
        TESTNET_URL,
        headers={"Content-Type": "application/json"},
        data=json.dumps({"method": method, "params": [params]}),
        timeout=40,
    )
    response.raise_for_status()
    payload = response.json()
    result = payload.get("result")
    if not isinstance(result, dict):
        raise RuntimeError(f"{method}: no `result` object in the response")
    return result


def saved_faucet_accounts() -> list[tuple[Path, str, str]]:
    """Every (file, address, secret) in the key directory, newest first.

    The secret is returned because a caller has to sign with it, and is never
    printed by anything in this file.
    """
    found = []
    for path in sorted(KEY_DIRECTORY.glob("xrp-testnet-*.json"), reverse=True):
        try:
            payload = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        account = payload.get("account") or {}
        address = account.get("address") or account.get("classicAddress")
        secret = account.get("secret") or payload.get("secret") or account.get("seed")
        if address and secret:
            found.append((path, address, secret))
    return found


def refuse_mainnet() -> str:
    """Ask the server which network it is on, and refuse anything but a test one."""
    info = rpc("server_info", {}).get("info") or {}
    network_id = info.get("network_id")
    if network_id in MAINNET_NETWORK_IDS:
        raise RuntimeError(
            f"the endpoint reports network_id {network_id}, which is MAINNET. Nothing was sent. This "
            f"file is pinned to {TESTNET_URL} so this should be impossible -- if you see it, the "
            f"hostname now resolves somewhere else."
        )
    return f"network_id {network_id}, build {info.get('build_version')}"


def main() -> int:
    parser = argparse.ArgumentParser(description="Send one tagged TESTNET XRP payment. Dry run by default.")
    parser.add_argument("--to", default="", help="destination address (default: the second saved faucet account)")
    parser.add_argument("--tag", type=int, default=4242, help="destination tag (default 4242)")
    parser.add_argument("--amount", default="10", help="XRP to send (default 10)")
    parser.add_argument("--send", action="store_true",
                        help="actually submit. Without this nothing is sent and the request is described")
    args = parser.parse_args()

    accounts = saved_faucet_accounts()
    print(f"saved faucet accounts in {KEY_DIRECTORY}: {len(accounts)}", flush=True)
    for path, address, _ in accounts:
        print(f"    {address}  from {path.name}", flush=True)
    if not accounts:
        print("\nREFUSED: no faucet account with a secret was found. Run:\n"
              "    python3 fund_testnets.py --xrp", file=sys.stderr)
        return 2

    source_path, source, secret = accounts[0]
    destination = args.to or next((a for _, a, _ in accounts[1:]), "")
    if not destination:
        print("\nREFUSED: only one faucet account exists, so there is nowhere to send. Either run\n"
              "    python3 fund_testnets.py --xrp\n"
              "again to create a second, or pass --to <address>.", file=sys.stderr)
        return 2
    if not is_valid_classic_address(destination):
        print(f"\nREFUSED: {destination} fails the checksum in chains/xrp_address.py. Nothing was sent.",
              file=sys.stderr)
        return 2

    drops = to_drops(args.amount)
    print(f"\n    network     {refuse_mainnet()}", flush=True)
    print(f"    from        {source}  (secret from {source_path.name}, never printed)", flush=True)
    print(f"    to          {destination}", flush=True)
    print(f"    amount      {args.amount} XRP = {drops} drops", flush=True)
    print(f"    tag         {args.tag}   <- THE FIELD THIS EXISTS TO PRODUCE", flush=True)

    if not args.send:
        print("\nDRY RUN -- nothing was submitted. Re-run with --send.", flush=True)
        return 0

    return submit_payment(source, destination, secret, args.tag, drops)


# Error codes a server returns when it will not sign on your behalf. Matched
# exactly rather than by substring: an earlier version tested
# `"ignInvalid" in str(status)`, which is a fragment of a guessed code and would
# also match anything else containing those eight characters.
SIGNING_REFUSED = frozenset({"notSupported", "noPermission", "internal", "srcActNotFound"})


def submit_payment(source: str, destination: str, secret: str, tag: int, drops: int) -> int:
    """Submit via rippled's legacy server-side signing. Returns an exit code.

    Extracted so main() stays under the return-count ceiling (rule 12: extract
    the decision rather than raise the ceiling), and so the outcome mapping --
    which exit code for which engine_result -- is one readable block.
    """
    try:
        result = rpc("submit", {
            "secret": secret,
            "tx_json": {
                "TransactionType": "Payment",
                "Account": source,
                "Destination": destination,
                "DestinationTag": tag,
                "Amount": str(drops),
            },
        })
    except RuntimeError as error:
        print(f"\nFAILED: {error}", file=sys.stderr)
        return 1

    status = str(result.get("engine_result") or result.get("error") or "(none)")
    message = result.get("engine_result_message") or result.get("error_message") or "(no message)"
    print(f"\n    engine_result   {status}", flush=True)
    print(f"    message         {message}", flush=True)
    tx_hash = (result.get("tx_json") or {}).get("hash")
    if tx_hash:
        print(f"    hash            {tx_hash}", flush=True)

    if status in ("tesSUCCESS", "terQUEUED"):
        print("\nSUBMITTED. Wait a few seconds for validation, then:", flush=True)
        print(f"    python3 xrp_chain_check.py --account {destination}", flush=True)
        print("  Step 3 should now show DestinationTag PRESENT, and step 4 should CREDIT it", flush=True)
        print("  instead of deferring -- the last unobserved field in the XRP deposit path.", flush=True)
        return 0

    if status in SIGNING_REFUSED:
        print("\nThis server will not sign on your behalf; public servers usually disable it.", flush=True)
        print("That is an answer, not a failure of this script. The fallback needs one dependency:", flush=True)
        print("    pip install xrpl-py", flush=True)
        print("  Say so rather than assuming -- adding a dependency is a decision.", flush=True)
    else:
        print(f"\nNot submitted, and `{status}` is not a known signing refusal either.", flush=True)
        print("  Read the message above before retrying: a tec* code means it REACHED the ledger", flush=True)
        print("  and claimed a fee, so retrying is not free even on testnet.", flush=True)
    return 1


if __name__ == "__main__":
    sys.exit(main())
