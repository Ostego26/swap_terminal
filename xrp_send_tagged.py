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
import os
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "swap_terminal"))

import requests
from chains.xrp_address import is_valid_classic_address

# derive_and_check() and MAINNET_NETWORK_IDS USED TO BE DEFINED IN THIS FILE.
#
# They moved to chains/xrp_signing.py on 2026-09-26, when chains/xrp.py's
# payout path needed the identical derivation guard. Rule 8: two copies of one
# rule is not redundancy, it is a bug with a delay on it -- the copies agree on
# the day they are written and drift from then on, invisibly, because each one
# looks correct in its own file. So there is one copy, it lives where both
# callers can reach it, and this file imports it rather than keeping a second.
#
# Nothing else about this script changed. It still signs only against
# s.altnet.rippletest.net, it still refuses a mainnet network_id before
# anything is sent, and it still never puts a secret in argv.
from chains.xrp_signing import MAINNET_NETWORK_IDS, derive_and_check
from chains.xrp_units import to_drops

TESTNET_URL = "https://s.altnet.rippletest.net:51234/"
KEY_DIRECTORY = Path.home() / ".config" / "swap_terminal" / "keys"


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


# WHERE THE FAUCET PUTS THINGS, MEASURED rather than guessed. Dumped from the
# operator's own saved faucet files 2026-09-26, keys and string LENGTHS only so
# no secret was displayed:
#
#     account.xAddress        str, len 47
#     account.address         str, len 34
#     account.classicAddress  str, len 34
#     amount                  int = 100
#     transactionHash         str, len 64
#     seed                    str, len 31     <- THE SECRET, at the TOP level
#
# The first version looked for account.secret, payload.secret and account.seed.
# The real key is payload.seed -- one level off, so it found zero accounts and
# refused to send while two funded accounts sat in that directory. Exactly the
# shape of the `balance` vs `amount` bug in fund_testnets.py, which is the
# argument for searching a named list and REPORTING which key matched rather
# than hardcoding one guess.
ADDRESS_KEYS = ("address", "classicAddress")
SECRET_KEYS = ("seed", "secret", "master_seed", "secretKey")


def _first_present(keys: tuple[str, ...], *holders: dict):
    """The first of `keys` present in any of `holders`, with the key it came from."""
    for key in keys:
        for holder in holders:
            value = holder.get(key)
            if value:
                return value, key
    return None, None


def saved_faucet_accounts() -> list[tuple[Path, str, str]]:
    """Every (file, address, secret) in the key directory, newest first.

    The secret is returned because a caller has to sign with it. Nothing in this
    file ever prints it -- only the FILE NAME it came from, and the key name it
    was found under, both of which are safe and both of which are what made the
    original bug diagnosable.
    """
    found = []
    for path in sorted(KEY_DIRECTORY.glob("xrp-testnet-*.json"), reverse=True):
        try:
            payload = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        account = payload.get("account") or {}
        address, address_key = _first_present(ADDRESS_KEYS, account, payload)
        secret, secret_key = _first_present(SECRET_KEYS, payload, account)
        if address and secret:
            # Naming the two keys that matched is the whole point of searching a
            # list instead of hardcoding one. When the faucet changes shape again
            # this line says so on the next run, rather than the run reporting
            # zero accounts and leaving the reader to dump the files by hand --
            # which is what the original bug cost. Key NAMES only; the secret's
            # value is never printed here or anywhere else in this file.
            print(f"    {path.name}: address under {address_key!r}, "
                  f"secret under {secret_key!r} (value not shown)", flush=True)
            found.append((path, address, secret))
        elif address:
            print(f"    {path.name}: address under {address_key!r} but NO secret "
                  f"under any of {', '.join(SECRET_KEYS)} -- cannot sign with "
                  f"this one", flush=True)
        else:
            # Rule 14: an unusable file must not look identical to one this glob
            # never saw. Zero accounts with no explanation is the ambiguity the
            # original bug hid inside.
            print(f"    {path.name}: no address under any of "
                  f"{', '.join(ADDRESS_KEYS)} -- skipped", flush=True)
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


def deposit_target_for_swap(db_path: str, swap_id: str) -> tuple[str, int]:
    """Read (account, tag) for a swap out of the database. Returns what to pay.

    WHY THIS EXISTS RATHER THAN THE OPERATOR TYPING THE TAG. A destination tag is
    what attributes a payment to a swap, and it is a bare integer with no checksum
    -- so a mistyped tag is not an error, it is a payment credited to a DIFFERENT
    swap or to none at all, with the ledger recording that the sender paid exactly
    what they chose to pay. The account has a checksum and would catch a typo; the
    tag has nothing.

    Reading both from the row removes the transcription entirely. It also removes
    the second failure, which is quieter: a correct tag sent to the wrong ACCOUNT.

    Added 2026-09-26 after three separate commands in one session were pasted with
    a `<placeholder>` still in them, because the value had to be carried by hand
    from a web page to a shell. A parameter a human has to copy is a parameter a
    human will eventually copy wrong.
    """
    if not Path(db_path).exists():
        raise SystemExit(
            f"REFUSED: no database at {db_path}. Pass --db, or set SWAP_DB_PATH to the same value "
            f"the server uses -- the swap has to be read from the SAME database that created it."
        )
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    try:
        row = connection.execute(
            "SELECT from_asset, deposit_address, deposit_tag, status FROM swaps WHERE id = ?",
            (swap_id,),
        ).fetchone()
    finally:
        connection.close()

    if row is None:
        raise SystemExit(f"REFUSED: no swap {swap_id} in {db_path}. Nothing was sent.")
    if row["from_asset"] != "XRP":
        raise SystemExit(
            f"REFUSED: swap {swap_id} expects {row['from_asset']}, not XRP. Sending XRP to it would "
            f"be money this terminal never credits. Nothing was sent."
        )
    # `is None`, not truthiness: 0 is a legal DestinationTag.
    if row["deposit_tag"] is None:
        raise SystemExit(
            f"REFUSED: swap {swap_id} has no deposit_tag, so a payment to the shared account could "
            f"not be attributed to it. Nothing was sent."
        )
    if not row["deposit_address"]:
        raise SystemExit(f"REFUSED: swap {swap_id} has no deposit_address. Nothing was sent.")

    print(f"    swap {swap_id} is {row['status']}, expects {row['from_asset']}", flush=True)
    return row["deposit_address"], int(row["deposit_tag"])


def main() -> int:
    parser = argparse.ArgumentParser(description="Send one tagged TESTNET XRP payment. Dry run by default.")
    parser.add_argument("--to", default="", help="destination address (default: the second saved faucet account)")
    parser.add_argument("--tag", type=int, default=4242, help="destination tag (default 4242)")
    parser.add_argument("--amount", default="10", help="XRP to send (default 10)")
    parser.add_argument(
        "--swap", default="",
        help="pay THIS swap: reads its account and destination tag from the database, so neither is "
             "typed by hand. Overrides --to and --tag.",
    )
    parser.add_argument(
        "--db", default="",
        help="database to read --swap from (default: SWAP_DB_PATH, else the server's own default)",
    )
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

    destination_tag = args.tag
    if args.swap:
        database = args.db or os.environ.get("SWAP_DB_PATH") or str(
            Path(__file__).resolve().parent / "swap_terminal" / "swap_terminal.db"
        )
        print(f"\n    reading the deposit target for {args.swap} from {database}", flush=True)
        destination, destination_tag = deposit_target_for_swap(database, args.swap)
        print(f"    account {destination}  tag {destination_tag}  <- from the swap row, not typed", flush=True)

    drops = to_drops(args.amount)
    print(f"\n    network     {refuse_mainnet()}", flush=True)
    print(f"    from        {source}  (secret read from {source_path.name}, never printed)", flush=True)
    print(f"    to          {destination}", flush=True)
    print(f"    amount      {args.amount} XRP = {drops} drops", flush=True)
    print(f"    tag         {destination_tag}   <- THE FIELD THIS EXISTS TO PRODUCE", flush=True)

    if not args.send:
        print("\nDRY RUN -- nothing was submitted. Re-run with --send.", flush=True)
        return 0

    return submit_payment(source, destination, secret, destination_tag, drops)


# Error codes a server returns when it will not sign on your behalf. Matched
# exactly rather than by substring: an earlier version tested
# `"ignInvalid" in str(status)`, which is a fragment of a guessed code and would
# also match anything else containing those eight characters.
SIGNING_REFUSED = frozenset({"notSupported", "noPermission", "internal", "srcActNotFound"})


def submit_locally_signed(source: str, destination: str, secret: str, tag: int, drops: int) -> int:
    """Sign here with xrpl-py and submit the signed blob. Returns an exit code.

    Reached only after the server refused to sign, and only after refuse_mainnet()
    has already answered -- the order matters and is asserted by the caller, since
    this is the one function in the tree that signs anything.

    Kept separate from submit_payment() rather than merged into it. The two differ
    in WHERE the key is used -- their machine versus ours -- which is the whole
    security-relevant fact about them, and rule 8 says a genuine difference belongs
    in a comment at both sites rather than collapsed into one function with a flag.

    submit_and_wait() rather than submit(): it waits for validation and raises on a
    failed final result, so "submitted" cannot be reported for a transaction the
    ledger rejected. Rule 13's "a stop that cannot prove it worked is not a stop",
    applied to a send.
    """
    try:
        # Lazy for the same reason chains/xrp_signing.derive_and_check() is lazy;
        # the note lives there now, with the import that explains it.
        import httpx  # noqa: PLC0415 -- checked: optional dependency, arrives with xrpl-py
        from xrpl.clients import JsonRpcClient  # noqa: PLC0415 -- checked: optional dependency
        from xrpl.constants import XRPLException  # noqa: PLC0415 -- checked: optional dependency
        from xrpl.models.transactions import Payment  # noqa: PLC0415 -- checked: optional dependency
        from xrpl.transaction import submit_and_wait  # noqa: PLC0415 -- checked: optional dependency
    except ImportError:
        print("\nxrpl-py is not importable, so local signing is not available.", flush=True)
        print("    pip install xrpl-py", flush=True)
        return 1

    print("\n    signing LOCALLY with xrpl-py; the seed does not leave this machine", flush=True)
    try:
        wallet = derive_and_check(secret, source)
    except RuntimeError as error:
        print(f"\nREFUSED: {error}", file=sys.stderr)
        return 1
    print(f"    derived address matches the announced source: {wallet.classic_address}", flush=True)

    payment = Payment(
        account=source,
        destination=destination,
        destination_tag=tag,
        amount=str(drops),
    )
    print(f"    built Payment with destination_tag={tag}, autofilling fee and sequence", flush=True)
    try:
        response = submit_and_wait(payment, JsonRpcClient(TESTNET_URL), wallet)
    except (XRPLException, httpx.HTTPError) as error:
        # NAMED types rather than `except Exception`. The first draft of this
        # caught Exception with a noqa: BLE001 explaining why breadth was
        # necessary, which rule 19 answers directly -- fix the code, do not
        # suppress the finding. Measured against the installed xrpl-py 5.2.0:
        # XRPLReliableSubmissionException and XRPLRequestFailureException both
        # subclass xrpl.constants.XRPLException, and the only other family that
        # reaches here is transport failure from httpx, which xrpl-py's JSON-RPC
        # client uses. Two names cover it, so no breadth is needed.
        #
        # Anything NOT in those two families now propagates, which is correct: a
        # bug in this file must not be reported to the operator as "the submit
        # failed" on a run that signs a real transaction.
        print(f"\nFAILED during submit: {type(error).__name__}: {error}", file=sys.stderr)
        print("  Nothing here retries. A tec* result already claimed a fee.", file=sys.stderr)
        return 1

    result = response.result or {}
    meta = result.get("meta") or {}
    outcome = str(meta.get("TransactionResult") or result.get("engine_result") or "(none)")
    tx_hash = result.get("hash") or (result.get("tx_json") or {}).get("hash")
    print(f"    TransactionResult  {outcome}", flush=True)
    print(f"    hash               {tx_hash or '(none)'}", flush=True)
    print(f"    validated          {result.get('validated')}", flush=True)

    if outcome != "tesSUCCESS":
        print(f"\nNOT delivered: {outcome} is not tesSUCCESS, so no value moved.", flush=True)
        return 1

    print("\nDELIVERED and validated. Now confirm the adapter reads it:", flush=True)
    print(f"    python3 xrp_chain_check.py --account {destination}", flush=True)
    print("  Step 3 should show DestinationTag PRESENT and step 4 should CREDIT it with", flush=True)
    print(f"  vout={tag} -- which is the one thing --hunt-tag cannot prove, because it", flush=True)
    print("  reads other people's payments and not ours.", flush=True)
    return 0


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
        print("That is an answer about the SERVER, not a failure of this script.", flush=True)
        return submit_locally_signed(source, destination, secret, tag, drops)
    else:
        print(f"\nNot submitted, and `{status}` is not a known signing refusal either.", flush=True)
        print("  Read the message above before retrying: a tec* code means it REACHED the ledger", flush=True)
        print("  and claimed a fee, so retrying is not free even on testnet.", flush=True)
    return 1


if __name__ == "__main__":
    sys.exit(main())
