#!/usr/bin/env python3
"""Prove the XRP adapter's wire format against a real rippled. Read-only.

Role: file (operator entry point, at the project root per CLAUDE.md rule 10)
Reads: a rippled JSON-RPC endpoint, and chains/xrp*.py
Writes: nothing. No file, no database, no ledger.
Can move funds: NO. This script imports no signing path and never submits a
        transaction. There is no flag that broadcasts, and the adapter it
        builds refuses send_to_address() structurally.
Mainnet-safe: yes to run. It identifies the network from the SERVER's
        network_id rather than from the URL, and says MAINNET in capitals.

WHY THIS EXISTS

chains/xrp.py's field names were written without access to xrpl.org. A probe
from the operator's host on 2026-09-25 confirmed most of them AND FOUND A BUG:
the transaction body arrives flat on the entry with metadata under `metaData`,
where the code looked only under `tx`/`tx_json` -- so it returned {}, skipped
the payment, and reported "no deposits" for money that had arrived.

That bug survived 620 passing tests, because the tests seeded the shape they
asserted. A seeded test cannot discover a wire format; only a real server can.
This script is that server conversation, made repeatable.

It closes the two gaps the probe left open:

  account_tx        the probe used `ledger`. account_tx is what the adapter
                    ACTUALLY calls, and its entries may nest differently again.
  a tagged payment  none of the three payments sampled carried a
                    DestinationTag, which is normal wallet-to-wallet traffic
                    and says nothing about deposits addressed to us.

USAGE

    python3 xrp_chain_check.py                           # testnet, self-bootstrapping
    python3 xrp_chain_check.py --account rSomeAccount    # check a specific one
    python3 xrp_chain_check.py --url https://s1.ripple.com:51234/   # mainnet

With no --account it finds a recent Payment on the ledger and uses ITS
destination, so the account is guaranteed to have transactions. That is what
makes a bare run meaningful instead of reporting an empty account as a pass.

EXIT CODES, and the third one is the point
    0   every method and field the adapter reads was observed
    1   at least one is wrong; each is named
    3   INCONCLUSIVE -- nothing failed, but there was nothing to look at
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent / "swap_terminal"))

from chains.xrp_address import describe_address, is_valid_classic_address
from chains.xrp_payments import (
    FIELD_DELIVERED_AMOUNT,
    FIELD_DESTINATION,
    FIELD_DESTINATION_TAG,
    FIELD_HASH,
    FIELD_TRANSACTION_RESULT,
    FIELD_TRANSACTION_TYPE,
    XRPPaymentError,
    deposit_events_from_transactions,
)
from microfortnights import format_duration

TESTNET_URL = "https://s.altnet.rippletest.net:51234/"
MAINNET_NETWORK_IDS = {0}

# Every field the adapter reads out of an account_tx entry, and whether its
# absence is fatal. Gathered here so confirming them is a read of one block.
PAYMENT_FIELDS = [
    (FIELD_HASH, True),
    (FIELD_TRANSACTION_TYPE, True),
    (FIELD_DESTINATION, True),
    (FIELD_TRANSACTION_RESULT, True),
    (FIELD_DELIVERED_AMOUNT, True),
    (FIELD_DESTINATION_TAG, False),
]


# ---------------------------------------------------------------------------
# THE DECISIONS, as pure functions (rule 10; and rule 12's note that a main()
# past the complexity ceiling is orchestration which has swallowed a decision).
# tests/test_xrp_chain_check_units.py calls these directly.
# ---------------------------------------------------------------------------

def network_banner(network_id) -> str:
    """Name the network from the SERVER's id, and shout if it is mainnet.

    A hostname can be reused or proxied; a network id cannot be mistaken by the
    client. solana_chain_check.py makes the same distinction with
    getGenesisHash, for the same reason.
    """
    if network_id in MAINNET_NETWORK_IDS:
        return f"network_id {network_id}  <- MAINNET, REAL MONEY"
    if network_id is None:
        return "network_id not reported  <- cannot confirm which network this is"
    return f"network_id {network_id}  <- not mainnet"


def server_findings(info: dict) -> list[str]:
    """What is wrong with a server_info result, as failure strings."""
    ledger = info.get("validated_ledger") or {}
    findings = [] if ledger else [
        "server_info reported no validated_ledger; this server has no validated ledger to read"
    ]
    findings.extend(
        f"server_info.validated_ledger.{name} is absent -- chains/xrp.py's reserve_xrp() reads "
        f"reserve_base_xrp and REFUSES rather than guessing, so account_balance() would fail"
        for name in ("reserve_base_xrp", "reserve_inc_xrp")
        if name not in ledger
    )
    return findings


def unwrap_shape(entry: dict) -> str:
    """Which of the three nestings this server uses. The bug was here."""
    if isinstance(entry.get("tx"), dict):
        return "nested under `tx` (rippled v1 style)"
    if isinstance(entry.get("tx_json"), dict):
        return "nested under `tx_json` (API v2 style)"
    if FIELD_TRANSACTION_TYPE in entry:
        return "FLAT on the entry  <- the shape that exposed the skipped-payment bug"
    return "*** UNRECOGNIZED -- no tx, no tx_json, no TransactionType ***"


def payment_field_report(payments: list[tuple[dict, dict]]) -> tuple[list[str], list[str]]:
    """Check every field the adapter reads. Returns (display lines, failures).

    Counted per payment rather than unioned across them, because a union says
    the NAME exists and a human reads it as a statement about every row --
    the misreading monero_chain_check.py was corrected for.
    """
    if not payments:
        return (["    (none) -- no Payments to examine, so every field below is UNCONFIRMED"], [])

    lines, failures = [], []
    total = len(payments)
    for name, required in PAYMENT_FIELDS:
        count = sum(1 for body, meta in payments if name in body or name in meta)
        if count == total:
            lines.append(f"    OK   {name:<22} present in all {total}")
        elif count:
            lines.append(f"    !    {name:<22} present in {count}/{total}  <- the NAME is confirmed")
        elif required:
            failures.append(f"no Payment carries `{name}`, and chains/xrp_payments.py REQUIRES it")
            lines.append(f"    FAIL {name:<22} MISSING and required")
        else:
            lines.append(
                f"    -    {name:<22} absent from all {total}  <- optional; for DestinationTag this is "
                f"normal wallet traffic and says nothing about deposits to us"
            )
    return lines, failures


def delivered_amount_findings(payments: list[tuple[dict, dict]]) -> list[str]:
    """The partial payment defense, checked against real responses.

    delivered_amount must be a STRING of drops. An object means an issued
    currency, which chains/xrp_payments.py refuses; anything else means the
    shape is not what the defense assumes, and the defense is the only thing
    standing between this terminal and the exploit that has drained exchanges.
    """
    findings = []
    for body, meta in payments:
        tx_hash = str(body.get(FIELD_HASH) or "?")[:16]
        delivered = meta.get(FIELD_DELIVERED_AMOUNT)
        if delivered is None:
            findings.append(
                f"payment {tx_hash} has no meta.{FIELD_DELIVERED_AMOUNT}. THIS IS THE ONE THAT MATTERS: "
                f"the credited figure comes from that field and there is deliberately no fallback to "
                f"Amount, because the fallback is the partial-payment exploit."
            )
        elif isinstance(delivered, dict):
            continue  # an issued currency; xrp_payments refuses it, which is correct
        elif not isinstance(delivered, str):
            findings.append(
                f"payment {tx_hash} reports {FIELD_DELIVERED_AMOUNT}={delivered!r} "
                f"({type(delivered).__name__}), not a drop string. The ledger sends drop counts as "
                f"STRINGS so clients cannot round them through a double."
            )
    return findings


def verdict_text(failures: list[str], payments_examined: int) -> str:
    """THREE outcomes, not two.

    No failures over zero payments confirmed nothing. Printing PASSED for that
    is the instrument reporting more than the run established -- the shape this
    session has been bitten by repeatedly.
    """
    if failures:
        body = "\n".join(f"  - {item}" for item in failures)
        return (
            f"FAILED: {len(failures)} check(s) did not pass\n{body}\n\n"
            "Each names a method or field chains/xrp.py or chains/xrp_payments.py depends on. They are\n"
            "gathered in one block per file so a wrong name is a one-line fix."
        )
    if payments_examined == 0:
        return (
            "NO FAILURES, AND THAT IS NOT A PASS.\n"
            "  No Payments were examined, so the field names -- the ones no unit test can check --\n"
            "  were NOT confirmed. Re-run, or pass --account for an account with payment history.\n"
            "  Exit status 3 means inconclusive, so a script cannot mistake this for a pass either."
        )
    return f"PASSED: every field the adapter reads was observed, over {payments_examined} real Payment(s)."


def exit_code(failures: list[str], payments_examined: int) -> int:
    """0 pass, 1 failed, 3 inconclusive. See verdict_text()."""
    if failures:
        return 1
    return 3 if payments_examined == 0 else 0


# ---------------------------------------------------------------------------
# Transport and printing.
# ---------------------------------------------------------------------------

failures: list[str] = []


def fail(message: str) -> None:
    failures.append(message)
    print(f"    FAIL  {message}", flush=True)


def step(number: int, title: str, detail: str) -> float:
    print(f"\n[{number}] {title}", flush=True)
    print(f"    {detail}", flush=True)
    return time.monotonic()


def done(started: float) -> None:
    print(f"    done in {format_duration(time.monotonic() - started)}", flush=True)


def rpc(url: str, method: str, params: dict, timeout: float = 25.0) -> dict:
    """One rippled call. Note both quirks: params is a LIST, errors are HTTP 200.

    Uses `requests` rather than urllib, matching every adapter in chains/ --
    and it happens to avoid S310, which fires on urllib.urlopen because the
    scheme is not pinned. Consistency was the reason; losing the finding is a
    side effect rather than the point.
    """
    response = requests.post(
        url,
        headers={"Content-Type": "application/json"},
        data=json.dumps({"method": method, "params": [params]}),
        timeout=timeout,
    )
    response.raise_for_status()
    payload = response.json()
    result = payload.get("result")
    if not isinstance(result, dict):
        raise RuntimeError(f"{method}: response carried no `result` object")
    if result.get("status") == "error":
        raise RuntimeError(f"{method}: {result.get('error')} -- {result.get('error_message', '')}")
    return result


def collect_payments(entries) -> list[tuple[dict, dict]]:
    """Every Payment in a response, as (body, metadata) pairs, any nesting."""
    def pair(entry):
        body = entry if FIELD_TRANSACTION_TYPE in entry else (entry.get("tx") or entry.get("tx_json") or {})
        meta = entry.get("meta") or entry.get("metaData") or {}
        merged = dict(body)
        merged.setdefault(FIELD_HASH, entry.get(FIELD_HASH))
        return merged, (meta if isinstance(meta, dict) else {})

    return [
        pair(entry)
        for entry in entries or []
        if isinstance(entry, dict)
        and isinstance(entry if FIELD_TRANSACTION_TYPE in entry else (entry.get("tx") or entry.get("tx_json")), dict)
        and (entry if FIELD_TRANSACTION_TYPE in entry else (entry.get("tx") or entry.get("tx_json") or {})).get(
            FIELD_TRANSACTION_TYPE
        ) == "Payment"
    ]


NETWORK_ERRORS = (requests.RequestException, RuntimeError, TimeoutError, OSError)


def check_server(url: str) -> dict | None:
    """Step 1: reach the server and name the network from ITS id, not the URL."""
    started = step(1, "reach the server and identify the network", "method=server_info")
    try:
        info = rpc(url, "server_info", {}).get("info") or {}
    except NETWORK_ERRORS as error:
        fail(f"could not reach {url}: {error}")
        return None
    print(f"    {network_banner(info.get('network_id'))}", flush=True)
    print(f"    build      {info.get('build_version', '(not reported)')}", flush=True)
    ledger = info.get("validated_ledger") or {}
    print(
        f"    reserve    base {ledger.get('reserve_base_xrp')} XRP, owner {ledger.get('reserve_inc_xrp')} XRP",
        flush=True,
    )
    for finding in server_findings(info):
        fail(finding)
    done(started)
    return info


def bad_address_verdict(account: str, *, from_operator: bool) -> str:
    """What an address that fails the local decoder MEANS, which depends on where it came from.

    The distinction is the whole value of this function and it was wrong until
    2026-09-26. The message was a single hardcoded string saying the account
    "came off the ledger but fails chains/xrp_address.py -- the decoder is
    wrong", which is a real and serious finding on the ledger-walk path: an
    address a validated ledger accepted and our decoder rejects means the
    decoder disagrees with the network, and that is a bug in our code.

    It is FALSE on the --account path, and that is the path an operator uses.
    Run 2026-09-26 passed the literal placeholder `rTHE_ADDRESS_IT_PRINTED`
    from a pasted command, and this script reported "the decoder is wrong"
    about a decoder that was working perfectly -- it correctly rejected `_`,
    which is not in the XRP Ledger base58 alphabet. An instrument that blames
    itself for bad input sends the reader to read xrp_address.py, and the next
    thing they do is "fix" a correct decoder.

    So the provenance decides, and the caller knows it: `given` non-empty means
    the operator supplied it.
    """
    if from_operator:
        return (
            f"{account} was supplied with --account and is not a valid address. "
            "This is the INPUT, not the decoder -- chains/xrp_address.py rejected "
            "it correctly. Pass a real account, or omit --account to have this "
            "script find one off the ledger itself."
        )
    return (
        f"{account} came off the ledger but fails chains/xrp_address.py -- the "
        "decoder is wrong. A validated ledger accepted this address and our "
        "decoder rejects it, so the two disagree and our side is the one to fix."
    )


def find_account(url: str, seq, how_many: int, given: str) -> str:
    """Step 2: an account guaranteed to have payment history.

    Self-bootstrapping on purpose. Given no --account, an empty account would
    produce a clean run that confirmed nothing -- which is the failure mode this
    whole script is written against.
    """
    # ANNOUNCES WHAT IT WILL ACTUALLY DO. With --account supplied no ledger walk
    # happens, and the first version said "walking back up to 20 validated
    # ledgers" regardless -- claiming work it did not do, which is the same
    # shape as every other correction in this file's history, just smaller.
    detail = (
        f"--account given, so no ledger walk: {given}"
        if given else
        f"no --account, so walking back up to {how_many} validated ledgers for a real Payment"
    )
    started = step(2, "find an account to examine", detail)
    account = given
    if not account and seq:
        for offset in range(how_many):
            try:
                result = rpc(url, "ledger", {"ledger_index": seq - offset, "transactions": True, "expand": True})
            except NETWORK_ERRORS as error:
                fail(f"ledger {seq - offset} could not be read: {error}")
                break
            entries = (result.get("ledger") or {}).get("transactions") or []
            found = collect_payments(entries)
            if found:
                print(f"    ledger {seq - offset} carries {len(found)} Payment(s)", flush=True)
                print(f"    entry shape: {unwrap_shape(entries[0])}", flush=True)
                account = found[0][0].get(FIELD_DESTINATION) or ""
                break
    if account:
        print(f"    using account {account}", flush=True)
        print(f"    local check:  {describe_address(account)}", flush=True)
        if not is_valid_classic_address(account) and not account.startswith(("X", "T")):
            fail(bad_address_verdict(account, from_operator=bool(given)))
    else:
        print("    (none) -- no Payment found, so account_tx cannot be exercised", flush=True)
    done(started)
    return account


def check_account_tx(url: str, account: str) -> tuple[list[dict], list[tuple[dict, dict]]]:
    """Step 3: THE GAP the earlier probe left -- account_tx is what the adapter calls."""
    started = step(
        3, "exercise account_tx, which is what the adapter actually calls",
        f"method=account_tx account={account[:16]}...",
    )
    entries: list[dict] = []
    payments: list[tuple[dict, dict]] = []
    try:
        result = rpc(url, "account_tx", {
            "account": account, "ledger_index_min": -1, "ledger_index_max": -1, "binary": False, "limit": 20,
        })
        entries = result.get("transactions") or []
        print(f"    entries returned: {len(entries)}", flush=True)
        if entries:
            shape = unwrap_shape(entries[0])
            print(f"    entry shape:      {shape}", flush=True)
            if "UNRECOGNIZED" in shape:
                fail("account_tx entries use a nesting chains/xrp_payments.py does not accept")
        payments = collect_payments(entries)
        print(f"    Payments among them: {len(payments)}", flush=True)
    except NETWORK_ERRORS as error:
        fail(f"account_tx failed: {error}")

    lines, found = payment_field_report(payments)
    for line in lines:
        print(line, flush=True)
    failures.extend(found)
    for finding in delivered_amount_findings(payments):
        fail(finding)
    done(started)
    return entries, payments


def check_real_scan(entries: list[dict], payments: list[tuple[dict, dict]], account: str) -> None:
    """Step 4: run the adapter's own scan over the real response.

    The empty-result branch is the important one. An empty scan over a response
    that DID contain inbound payments is the skipped-payment bug returning, and
    it must be a failure rather than a quiet zero.
    """
    started = step(
        4, "run the adapter's own scan over that response",
        "chains/xrp_payments.deposit_events_from_transactions(), the function the worker calls",
    )
    if not entries:
        print("    (none) -- nothing to scan on this run", flush=True)
        done(started)
        return
    try:
        scan = deposit_events_from_transactions(entries, account, 1)
    except XRPPaymentError as error:
        fail(f"the scan REFUSED real server data: {error}")
        done(started)
        return
    print(f"    credited {len(scan.events)}, deferred {len(scan.deferred)}", flush=True)
    for event in scan.events[:5]:
        print(f"        {event['amount']} XRP  tag={event['vout']}  rank={event['confirmations']}", flush=True)
    for line in scan.deferred[:5]:
        print(f"        deferred  {line}", flush=True)
    if not scan.events and not scan.deferred:
        inbound = [b for b, _ in payments if b.get(FIELD_DESTINATION) == account]
        print("        (none)  <- credited and deferred nothing", flush=True)
        if inbound:
            fail(
                f"{len(inbound)} Payment(s) to {account[:16]}... were in the response and the scan "
                f"produced nothing -- this is the silent-skip failure, not an empty account"
            )
    done(started)


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify the XRP adapter against a real rippled. Read-only.")
    parser.add_argument("--url", default=TESTNET_URL, help=f"rippled JSON-RPC endpoint (default {TESTNET_URL})")
    parser.add_argument("--account", default="", help="account for account_tx; found from the ledger if omitted")
    parser.add_argument("--ledgers", type=int, default=20, help="ledgers to search for a Payment (default 20)")
    args = parser.parse_args()

    print("xrp adapter check -- read-only, submits nothing, signs nothing", flush=True)
    print(f"  endpoint   {args.url}", flush=True)
    print("  WHAT THIS PROVES: that the method and field names in chains/xrp.py and", flush=True)
    print("  chains/xrp_payments.py match a real server. A probe on 2026-09-25 found", flush=True)
    print("  the transaction body FLAT on the entry where the code looked only under", flush=True)
    print("  `tx`, which silently skipped payments -- past 620 passing tests, because", flush=True)
    print("  a seeded test cannot discover a wire format.", flush=True)

    info = check_server(args.url)
    if info is None:
        print("\nSTOPPING: nothing else can be checked without a server.", flush=True)
        return 1

    seq = (info.get("validated_ledger") or {}).get("seq")
    account = find_account(args.url, seq, args.ledgers, args.account)
    if not account:
        print("\n" + "=" * 70, flush=True)
        print(verdict_text(failures, 0), flush=True)
        return exit_code(failures, 0)

    # Steps 3 and 4 are SKIPPED, not run and failed, when the address cannot be
    # a valid account. Run 2026-09-26 sent the placeholder `rTHE_ADDRESS_IT_PRINTED`
    # through to the server and reported "account_tx failed: actMalformed" -- which
    # reads exactly like the wrong method name or a changed wire format, the two
    # things this script exists to detect. It was neither; the server was telling us
    # the ARGUMENT was garbage. Rule 13: a step that did not run must not report the
    # same way as one that ran and found a defect.
    if not is_valid_classic_address(account) and not account.startswith(("X", "T")):
        print("\n[3] exercise account_tx, which is what the adapter actually calls", flush=True)
        print("    SKIPPED -- the address above is not valid, so account_tx could only", flush=True)
        print("    return actMalformed. That says nothing about the method or field", flush=True)
        print("    names this script checks, so it is not run.", flush=True)
        print("\n[4] run the adapter's own scan over that response", flush=True)
        print("    SKIPPED -- nothing was fetched to scan.", flush=True)
        print("\n" + "=" * 70, flush=True)
        print(verdict_text(failures, 0), flush=True)
        return exit_code(failures, 0)

    entries, payments = check_account_tx(args.url, account)
    check_real_scan(entries, payments, account)

    print("\n" + "=" * 70, flush=True)
    print(verdict_text(failures, len(payments)), flush=True)
    return exit_code(failures, len(payments))


if __name__ == "__main__":
    sys.exit(main())
