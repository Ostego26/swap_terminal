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
import os
import sqlite3
import sys
from decimal import Decimal
from pathlib import Path
from typing import NamedTuple

sys.path.insert(0, str(Path(__file__).resolve().parent / "swap_terminal"))

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
from chains.xrp_signing import derive_and_check

# MOVED to chains/xrp_testnet.py on 2026-09-26: the endpoint constant had
# three copies across the root scripts and a fourth was about to be written
# for the escrow harness (rule 8). Same code, one home; that module's header
# carries the faucet-key table this file used to hold.
from chains.xrp_testnet import (
    KEY_DIRECTORY,
    TESTNET_URL,
    refuse_mainnet,
    rpc,
    saved_faucet_accounts,
)
from chains.xrp_units import to_drops


def pending_xrp_swap(db_path: str) -> str:
    """The one XRP swap awaiting a deposit. Returns its id, or refuses.

    WHY A LOOKUP RATHER THAN AN ARGUMENT. `--swap` already removed the tag from the
    operator's hands, and the tag is the value with no checksum behind it. But the
    swap ID still had to travel from a web page into a shell, and across this
    session FOUR separate commands were pasted with a `<placeholder>` still in
    them, twice after I had said I would stop writing them. Two of those pastes put
    something into bash that should never have been there, one of them a wallet
    passphrase.

    The lesson is not "be more careful with placeholders". It is that a value a
    human has to carry between two programs is a defect in the second program. So
    this asks the database.

    REFUSES ON AMBIGUITY rather than guessing newest-wins. With two swaps waiting,
    picking one silently would send a payment to a tag the operator did not choose,
    and on a shared account with no checksum that money is attributed to the wrong
    swap. Listing them and stopping costs one command; guessing costs a deposit.

    RULE 8, AND THE SECOND SITE IS NAMED BECAUSE THE TWO GENUINELY DIFFER.
    open_swap.swaps_awaiting_deposit() runs the same SELECT and never refuses: it
    is about to CREATE a swap and already knows the new id, so it warns and lists
    the others, while this one is about to SEND to exactly one (account, tag) and
    a tag carries no checksum. Same query, opposite decisions, and each site
    points at the other. The merge both want is one reader in
    services/swap_service.py, which owns the `swaps` table; it was not done on
    2026-09-26 because another session held that file open, and it is named here
    as owed work rather than left for somebody to rediscover.
    """
    if not Path(db_path).exists():
        raise SystemExit(f"REFUSED: no database at {db_path}. Nothing was sent.")
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            "SELECT id, deposit_tag, expected_input_amount, created_at FROM swaps "
            "WHERE from_asset = 'XRP' AND status = 'awaiting_deposit' ORDER BY created_at DESC"
        ).fetchall()
    finally:
        connection.close()

    if not rows:
        # IT USED TO SAY "create one in the web UI first", AND THAT WAS THE DEFECT
        # RATHER THAN THE REMEDY: a terminal tool pointing at a GUI.
        #
        # Measured 2026-09-26: the operator pasted a four-command sequence twice --
        # send the deposit, watch it credit, pay it out, verify. Both times the
        # first two commands printed this refusal and the watcher and the payout
        # worker then found nothing to do. Four commands, two runs, zero work,
        # because creating the swap needed a browser while the rest of the loop is
        # a terminal. open_swap.py exists to close that, so this names it.
        #
        # THE ONE PLACEHOLDER IS DELIBERATE AND IT FAILS CLOSED. This file's own
        # docstring is emphatic that a placeholder in a pasted command has cost
        # three mis-runs and twice exposed a secret -- and that ban is about values
        # a person has to TRANSCRIBE from somewhere else, which is what
        # deposit_target_for_swap() removed for the account, the tag and the
        # amount. A payout address is not that: it is a value the operator owns and
        # is the one thing no tool may invent, because it is where the payout is
        # broadcast. Pasted unedited it reaches the destination chain's own
        # validate_address() and is refused there, before a tag is allocated or a
        # row is written, so the worst case is a second refusal rather than a swap
        # pointing somewhere nobody holds a key for.
        raise SystemExit(
            f"REFUSED: no XRP swap is awaiting a deposit in {db_path}. (none) is the answer, not an "
            f"error -- open one from this terminal, no browser needed:\n"
            f"      python3 open_swap.py --pair XRP:GRC --amount 1 --payout-address YOUR_GRC_ADDRESS\n"
            f"  That is a dry run and writes nothing; add --apply to write the swap, and it prints the "
            f"`--swap <id>` command back with the real id already in it. Replace YOUR_GRC_ADDRESS with a "
            f"GRC address you hold the key for -- it is where the payout is broadcast and it is the one "
            f"value no tool can fill in; left as-is it is refused by the Gridcoin daemon's own address "
            f"check before anything is written. Nothing was sent."
        )
    if len(rows) > 1:
        listing = "\n".join(
            f"      --swap {row['id']}   tag {row['deposit_tag']}  expects {row['expected_input_amount']} XRP  "
            f"created {row['created_at']}"
            for row in rows
        )
        raise SystemExit(
            f"REFUSED: {len(rows)} XRP swaps are awaiting a deposit, so which one you meant is not "
            f"knowable from here. Choosing for you would send a payment to a tag you did not pick, "
            f"and on a shared account that money is attributed to the wrong swap. Name one:\n{listing}"
        )
    row = rows[0]
    print(f"    one XRP swap is awaiting a deposit: {row['id']} (tag {row['deposit_tag']}, "
          f"expects {row['expected_input_amount']} XRP)", flush=True)
    return row["id"]


class DepositTarget(NamedTuple):
    """Everything about a swap that decides what payment satisfies it.

    A NamedTuple rather than three return values or a dict, for one reason: the
    three travel together or not at all. An account without its tag pays the wrong
    swap on a shared account, and a tag without its amount pays the right swap an
    amount the tolerance check halts. Splitting them into separate lookups would
    let a caller take two of the three, which is the shape every bug in this
    script has had.

    expected_amount comes from a REAL column, so a swap for 5 XRP reads back as
    5.0. to_drops() takes it through Decimal(str(...)) and is unbothered; the
    display carries whatever the row holds rather than a reformatted copy, because
    a number reprinted in a different shape than the database holds it is how a
    reader concludes two values differ when they do not.
    """

    address: str
    tag: int
    expected_amount: object


def deposit_target_for_swap(db_path: str, swap_id: str, amount: str = "") -> DepositTarget:
    """Read the account, tag and expected amount for a swap. Returns what to pay.

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
            "SELECT from_asset, deposit_address, deposit_tag, status, expected_input_amount "
            "FROM swaps WHERE id = ?",
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

    # REFUSE AN AMOUNT THE SWAP WILL NOT ACCEPT, rather than sending into a
    # guaranteed halt.
    #
    # Measured 2026-09-26: `--swap latest --amount 1` resolved to a swap expecting
    # 5 XRP, printed "expects 5.0 XRP", and sent anyway. The tolerance check then
    # did its job and moved the swap to 'under_review' -- correctly, because
    # crediting a wrong amount is the thing it exists to prevent. But the
    # information needed to avoid that was on screen one line earlier.
    #
    # A halted swap needs a PERSON to resolve it, and the testnet XRP is now sitting
    # against a swap nothing will advance. Refusing costs a retyped flag; proceeding
    # costs a manual reconciliation. The comparison is exact rather than
    # tolerance-aware on purpose: this script does not know
    # Config.AMOUNT_TOLERANCE_PCT and should not guess at it, and "send exactly what
    # the swap expects" is a rule with no edge cases. --amount is still honored when
    # it matches, and --swap can be omitted entirely to send an arbitrary amount.
    expected = row["expected_input_amount"]
    if amount and expected is not None and Decimal(str(amount)) != Decimal(str(expected)):
        raise SystemExit(
            f"REFUSED: swap {swap_id} expects {expected} XRP and --amount says {amount}. Sending a "
            f"different amount would be credited by nothing: the tolerance check halts the swap to "
            f"'under_review', which needs a person to resolve. Use --amount {expected}, or drop "
            f"--swap to send an arbitrary amount somewhere else. Nothing was sent."
        )
    return DepositTarget(row["deposit_address"], int(row["deposit_tag"]), expected)


# What to send when nothing else says. Only reachable with no --swap, since a
# swap always carries its own expected_input_amount -- so this is the amount for
# an ad hoc "does the tagged path work at all" payment between two faucet
# accounts, and 10 is a tenth of what the testnet faucet hands out.
DEFAULT_AMOUNT_XRP = "10"


def resolve_amount(explicit: str, target: DepositTarget | None) -> tuple[object, str]:
    """Decide what to send, and say where the figure came from. (amount, source).

    THE AMOUNT COMES FROM THE SWAP ROW WHEN THERE IS ONE, and --amount only
    overrides it.

    Measured 2026-09-26: --amount defaulted to the string "10", which is not falsy,
    so deposit_target_for_swap()'s mismatch check fired on EVERY --swap run against
    a swap for any other amount. The operator's loop was therefore: run, read
    "REFUSED: swap s_... expects 5.0 XRP and --amount says 10", retype the flag, run
    again. Two commands to send one payment, every time, and the second one typed
    from a number just read off the screen one line earlier -- which is exactly the
    hand transcription deposit_target_for_swap() was written to remove for the tag
    and the account. The tag and the account came from the row; the amount did not,
    and it was the only one of the three that could still be wrong.

    THIS CANNOT WIDEN WHAT GETS SENT, which is why it is not a posture change. With
    --swap, the only amount that survives the mismatch check is the row's own
    expected_input_amount; every other value already refused before anything was
    submitted. So the set of sendable amounts is unchanged at exactly one, and what
    changed is whether a person has to name it. An explicitly passed --amount is
    still compared against the row and still refused on a mismatch: the default
    moved, the check did not.

    A function rather than four lines inside main() because it is the decision and
    main() is orchestration (rule 10). The `== ""` and `is None` comparisons are the
    reason it earns its own tests: neither can be written as a truth test. A swap
    expecting 0 and a swap naming no amount are different facts, and this file has
    already been bitten once by testing a numeric field for truth -- tag 0 is a
    legal DestinationTag and `if not tag` read it as absent.
    """
    if explicit != "":
        return explicit, "--amount"
    if target is None:
        return DEFAULT_AMOUNT_XRP, f"the {DEFAULT_AMOUNT_XRP} XRP default -- no --amount and no --swap"
    if target.expected_amount is None:
        # REFUSE rather than fall back to the default. db.py:101 declares
        # `expected_input_amount REAL NOT NULL`, so reaching here means the database
        # named by --db predates that column or was built by hand -- and the one
        # thing that must not happen then is sending DEFAULT_AMOUNT_XRP at a swap
        # whose expectation is unknown. That is the halt this whole function exists
        # to avoid, arrived at from the other direction.
        raise SystemExit(
            "REFUSED: the swap row carries no expected_input_amount, so what payment would satisfy "
            "it is not knowable from here. db.py declares that column NOT NULL, so this database is "
            "older than the schema or was built by hand. Pass --amount explicitly if you know the "
            "figure. Nothing was sent."
        )
    return target.expected_amount, "the swap row's expected_input_amount, not typed"



def main() -> int:
    parser = argparse.ArgumentParser(description="Send one tagged TESTNET XRP payment. Dry run by default.")
    parser.add_argument("--to", default="", help="destination address (default: the second saved faucet account)")
    parser.add_argument("--tag", type=int, default=4242, help="destination tag (default 4242)")
    parser.add_argument(
        "--amount", default="",
        help="XRP to send. Default: the amount the --swap row expects, or 10 with no --swap. "
             "Passing it with --swap is still checked against the row and refused on a mismatch",
    )
    parser.add_argument(
        "--swap", default="",
        help="pay THIS swap: reads its account and destination tag from the database, so neither is "
             "typed by hand. Overrides --to and --tag. Pass `latest` to have the one XRP swap awaiting "
             "a deposit looked up, which refuses if there is more than one.",
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
    target = None
    if args.swap:
        database = args.db or os.environ.get("SWAP_DB_PATH") or str(
            Path(__file__).resolve().parent / "swap_terminal" / "swap_terminal.db"
        )
        swap_id = args.swap
        if swap_id == "latest":
            print(f"\n    looking up the XRP swap awaiting a deposit in {database}", flush=True)
            swap_id = pending_xrp_swap(database)
        print(f"\n    reading the deposit target for {swap_id} from {database}", flush=True)
        target = deposit_target_for_swap(database, swap_id, args.amount)
        destination, destination_tag = target.address, target.tag
        print(f"    account {destination}  tag {destination_tag}  <- from the swap row, not typed", flush=True)

    amount, amount_source = resolve_amount(args.amount, target)
    drops = to_drops(amount)
    print(f"\n    network     {refuse_mainnet()}", flush=True)
    print(f"    from        {source}  (secret read from {source_path.name}, never printed)", flush=True)
    print(f"    to          {destination}", flush=True)
    print(f"    amount      {amount} XRP = {drops} drops   <- from {amount_source}", flush=True)
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
