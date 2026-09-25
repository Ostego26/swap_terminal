#!/usr/bin/env python3
"""Prove the Monero adapter's wire format against a real wallet. Read-only.

Role: file (operator entry point, at the project root per CLAUDE.md rule 10)
Reads: a running monero-wallet-rpc, and chains/monero*.py
Writes: nothing to disk, nothing to the database. Derives a subaddress ONLY
        with --derive-address, which is off by default.
Can move funds: NO. This script imports no signing path, never calls
        `transfer`, and constructs its adapter with can_spend=False
        unconditionally -- so send_to_address() refuses before any network call
        even if someone adds one. There is no flag that broadcasts.
Mainnet-safe: yes to run. It identifies the network from the daemon rather
        than from the port and says MAINNET in capitals if that is where it is.

WHY THIS EXISTS, AND WHY IT IS THE MOST IMPORTANT FILE IN THE MONERO WORK

chains/monero.py says plainly that its RPC method names and response field
names were never verified: `getmonero.org` returns 403 through the proxy of
the environment they were written in, so no documentation could be opened and
no daemon run. The arithmetic is measured, the wire format is a HYPOTHESIS.

The danger in that is specific and it is not "the adapter might not work". It
is that 50 unit tests PASS against seeded responses shaped by the same
hypothesis, so a green suite says nothing at all about the field names -- and
a green suite is exactly what gets mistaken for evidence. An experiment that
can only confirm is not an experiment.

So this script is the experiment. It asks a real wallet, reads the answers,
and checks each name the adapter depends on against what actually came back.
Every FIELD_* constant in chains/monero_transfers.py and every _METHOD_*
constant in chains/monero.py is either OBSERVED or reported as unconfirmed,
by name, with the count of transfers it had to look at.

It follows regtest_htlc_verify.py and solana_chain_check.py: the operator runs
it, pastes the output back, and the output is self-describing a day later.

USAGE

    python3 monero_chain_check.py                     # reads XMR_* from the environment
    python3 monero_chain_check.py --host 127.0.0.1 --port 18082
    python3 monero_chain_check.py --derive-address    # also derive one subaddress

Point it at a STAGENET wallet first. Starting one, for reference:

    monero-wallet-rpc --stagenet --rpc-bind-port 38083 \
        --wallet-file <wallet> --prompt-for-password --disable-rpc-login
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

# The application imports its own modules rootlessly, so an entry point at the
# project root has to put swap_terminal/ on the path before importing any of
# them. That is CLAUDE.md rule 10's layout gap, the same shim every other entry
# point in this tree carries. (solana_chain_check.py needs `noqa: E402` on each
# import for this; here ruff does not flag them, and rule 19 says a directive
# nobody needs is not added just for symmetry.)
sys.path.insert(0, str(Path(__file__).resolve().parent / "swap_terminal"))

from chains.monero import (
    _METHOD_CREATE_ADDRESS,
    _METHOD_GET_BALANCE,
    _METHOD_GET_TRANSFERS,
    _METHOD_VALIDATE_ADDRESS,
    MoneroAdapter,
)
from chains.monero_transfers import (
    FIELD_ADDRESS,
    FIELD_AMOUNT,
    FIELD_CONFIRMATIONS,
    FIELD_DOUBLE_SPEND,
    FIELD_LOCKED,
    FIELD_SUBADDR_INDEX,
    FIELD_TXID,
    FIELD_TYPE,
    FIELD_UNLOCK_TIME,
    deposit_events_from_transfers,
)
from chains.monero_units import from_atomic
from microfortnights import format_duration

# Every field chains/monero_transfers.py reads, and whether the adapter can
# work without it. "required" means a missing one raises rather than degrades.
TRANSFER_FIELDS = [
    (FIELD_TXID, True),
    (FIELD_AMOUNT, True),
    (FIELD_ADDRESS, True),
    (FIELD_SUBADDR_INDEX, True),
    (FIELD_CONFIRMATIONS, False),
    (FIELD_TYPE, False),
    (FIELD_UNLOCK_TIME, False),
    (FIELD_LOCKED, False),
    (FIELD_DOUBLE_SPEND, False),
]


# ---------------------------------------------------------------------------
# THE DECISIONS, AS PURE FUNCTIONS.
#
# CLAUDE.md rule 12 on C901: "A main() past the ceiling is orchestration that
# has swallowed decisions... The fix is to extract the decision so it can be
# called with seeded inputs, not to raise the ceiling." A first draft of this
# file had main() at 29 complexity and 33 branches, and every one of those
# branches was a judgment about whether a wallet's answer is acceptable --
# exactly the thing that should be callable from a test.
#
# So the judgments are here, they take dicts and return findings, and
# tests/test_monero_chain_check_units.py calls them directly. main() below is
# transport and printing.
# ---------------------------------------------------------------------------


def balance_findings(balance: dict) -> list[str]:
    """What is wrong with a get_balance result, as a list of failure strings.

    Empty list means nothing is wrong. Both field names are checked, not just
    the one the adapter reads, because `balance` being present while
    `unlocked_balance` is absent is a very different diagnosis from neither
    being there -- the first means the field was renamed, the second means the
    whole method is wrong.
    """
    findings = []
    for name in ("balance", "unlocked_balance"):
        if name not in balance:
            findings.append(f"{_METHOD_GET_BALANCE} returned no `{name}` -- chains/monero.py reads it")
        elif not isinstance(balance[name], int) or isinstance(balance[name], bool):
            findings.append(
                f"{_METHOD_GET_BALANCE}.{name} is {type(balance[name]).__name__}, not an integer of "
                f"atomic units -- reading it at the wrong scale is wrong by a factor of a trillion"
            )
    return findings


def address_findings(verdict: dict) -> list[str]:
    """What is wrong with a validate_address result for the wallet's OWN address."""
    findings = []
    if not verdict.get("valid"):
        findings.append(
            f"{_METHOD_VALIDATE_ADDRESS} says this wallet's OWN primary address is invalid, which "
            f"cannot be right -- the method name or its result shape is wrong"
        )
    if "nettype" not in verdict:
        findings.append(
            f"{_METHOD_VALIDATE_ADDRESS} returned no `nettype` -- the network cannot be confirmed from "
            f"the daemon, and a port is not proof of a network"
        )
    return findings


def network_banner(nettype: str) -> str:
    """The loudest thing on the screen when this is pointed at real money."""
    return "  <- REAL MONEY" if nettype == "mainnet" else ""


def transfer_field_report(transfers: list[dict]) -> tuple[list[str], list[str]]:
    """Check every field name the adapter depends on. Returns (lines, failures).

    THE CENTRAL CHECK OF THIS SCRIPT. Each FIELD_* constant in
    chains/monero_transfers.py is looked for across every transfer the wallet
    returned; a required one that appears in none of them is a failure, and an
    optional one is reported as absent rather than silently defaulted.

    With no transfers to look at, EVERY name is reported unconfirmed and none
    is a failure -- because "I could not check" and "it is wrong" are different
    sentences (rule 17), and a run against an empty wallet must not be
    mistaken for a pass. The caller is responsible for saying so; see
    verdict_text().
    """
    observed: set[str] = set()
    for entry in transfers:
        observed.update(entry.keys())

    # COUNTED PER TRANSFER, not just unioned, and that distinction was found by
    # running this against a stand-in wallet where one of two transfers was
    # missing `subaddr_index`. The union said "OK subaddr_index present" --
    # true as a statement about the NAME, and read by a human as a statement
    # about every row. Step 4 caught the bad row, but step 3 had already told
    # the operator it was fine. Printing "present in 1/2" cannot be misread.
    present = {name: sum(1 for entry in transfers if name in entry) for name, _ in TRANSFER_FIELDS}

    lines, failures = [], []
    for name, required in TRANSFER_FIELDS:
        count = present.get(name, 0)
        if not transfers:
            lines.append(f"    ?  {name:<20} unconfirmed (no transfers to look at)")
        elif count == len(transfers):
            lines.append(f"    OK {name:<20} present in all {count}")
        elif count:
            lines.append(
                f"    !  {name:<20} present in {count}/{len(transfers)}  <- the NAME is confirmed; "
                f"{len(transfers) - count} transfer(s) lack it"
            )
        elif required:
            failures.append(f"no incoming transfer carries `{name}`, and chains/monero_transfers.py REQUIRES it")
            lines.append(f"    FAIL {name:<18} MISSING and required")
        else:
            lines.append(f"    -  {name:<20} absent from every transfer  <- optional; the adapter defaults it")

    unexpected = observed - {name for name, _ in TRANSFER_FIELDS}
    lines.append(f"    fields the adapter ignores   {', '.join(sorted(unexpected)) if unexpected else '(none)'}")
    return lines, failures


def exit_code(failures: list[str], transfer_count: int) -> int:
    """0 pass, 1 failed, 3 INCONCLUSIVE -- and the third one is the point.

    CLAUDE.md rule 13: "Treat 'skipped' plus 'success' in the same output as a
    defect in the output. A cycle that did no work must not report the same way
    as one that did." A run against a wallet with no transfers confirmed none
    of the field names this script exists to confirm, and exiting 0 for it
    would tell every caller -- a shell `&&`, a CI step, an operator reading
    `echo $?` -- exactly what a real pass tells them.

    This was shipped as 0 and corrected the same session, after a stand-in run
    printed "NOT THE SAME AS A PASS" above an exit status of zero.
    """
    if failures:
        return 1
    return 3 if transfer_count == 0 else 0


def verdict_text(failures: list[str], transfer_count: int) -> str:
    """The closing block, and the reason this function exists at all.

    There are THREE outcomes here, not two, and collapsing them is the exact
    defect this whole file is written against: a run with no failures against a
    wallet that had no transfers has confirmed NOTHING about the field names,
    and printing "PASSED" for it would be the instrument reporting more than
    the run established.
    """
    if failures:
        body = "\n".join(f"  - {item}" for item in failures)
        return (
            f"FAILED: {len(failures)} check(s) did not pass\n{body}\n\n"
            "Each one names a method or field chains/monero.py or chains/monero_transfers.py depends on.\n"
            "They are gathered in one block per file precisely so a wrong name is a one-line fix."
        )
    if transfer_count == 0:
        return (
            "NO FAILURES, AND THAT IS NOT THE SAME AS A PASS.\n"
            "  This wallet had no incoming transfers, so the transfer field names -- the ones that\n"
            "  actually matter, and the ones no unit test can check -- were NOT confirmed. Send a small\n"
            "  amount to this wallet on stagenet and run this again.\n"
            "  Exit status 3 means inconclusive, so a script cannot mistake this for a pass either."
        )
    return (
        f"PASSED: every method and field the adapter depends on was observed, over "
        f"{transfer_count} real transfer(s)."
    )


# ---------------------------------------------------------------------------
# Transport and printing.
# ---------------------------------------------------------------------------

failures: list[str] = []


def fail(message: str) -> None:
    failures.append(message)
    print(f"    FAIL  {message}", flush=True)


def step(number: int, title: str, detail: str) -> float:
    """Announce before, not only after (rule 14). Returns the start time."""
    print(f"\n[{number}] {title}", flush=True)
    print(f"    {detail}", flush=True)
    return time.monotonic()


def done(started: float) -> None:
    print(f"    done in {format_duration(time.monotonic() - started)}", flush=True)


def parse_args():
    parser = argparse.ArgumentParser(description="Verify the Monero adapter against a real wallet. Read-only.")
    parser.add_argument("--host", default=os.getenv("XMR_RPC_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.getenv("XMR_RPC_PORT", "0")))
    parser.add_argument("--user", default=os.getenv("XMR_RPC_USER", ""))
    parser.add_argument("--account-index", type=int, default=int(os.getenv("XMR_ACCOUNT_INDEX", "0")))
    parser.add_argument(
        "--derive-address",
        action="store_true",
        help=(
            f"also call {_METHOD_CREATE_ADDRESS}. OFF by default because it MUTATES the wallet: it "
            f"derives a real subaddress that this script then forgets, so nothing will ever watch it. "
            f"Harmless, but it is a write, and a script that says read-only should not do one quietly."
        ),
    )
    return parser.parse_args()


def announce(adapter, args) -> None:
    print("monero adapter check -- read-only, sends nothing, signs nothing", flush=True)
    print(f"  wallet rpc      {adapter.url}", flush=True)
    print(f"  account index   {args.account_index}", flush=True)
    print(f"  authentication  {'digest as ' + args.user if args.user else 'none (--disable-rpc-login)'}", flush=True)
    print(f"  can spend       {adapter.can_spend}  <- hardcoded False in this script", flush=True)
    print(f"  derive address  {args.derive_address}  <- the only call here that would mutate the wallet", flush=True)
    print(
        "  WHAT THIS PROVES: that the method and field names in chains/monero.py and\n"
        "  chains/monero_transfers.py match a real wallet. Those names were written without access to a\n"
        "  daemon or to Monero's documentation, and no unit test can check them -- a unit test seeds the\n"
        "  same shape it asserts. See this file's docstring.",
        flush=True,
    )


def check_balance(adapter, account_index: int) -> dict | None:
    started = step(1, "reach the wallet", f"POST {adapter.url}  method={_METHOD_GET_BALANCE}")
    try:
        balance = adapter.call(_METHOD_GET_BALANCE, {"account_index": account_index})
    except Exception as error:  # noqa: BLE001 -- checked: this is the diagnostic runner. Every failure is named, printed and counted into the exit code, so the caller can always tell a failure from a pass.
        fail(f"could not reach the wallet: {error}")
        return None
    print(f"    wallet answered, {len(balance)} field(s) in the result", flush=True)
    for finding in balance_findings(balance):
        fail(finding)
    if "unlocked_balance" in balance and "balance" in balance:
        print(f"    total     {from_atomic(balance['balance'])} XMR", flush=True)
        print(
            f"    unlocked  {from_atomic(balance['unlocked_balance'])} XMR  <- what get_balance() "
            f"reports, and what payout_service.py would fund from",
            flush=True,
        )
    done(started)
    return balance


def check_network(adapter, account_index: int) -> None:
    started = step(
        2, "identify the network FROM THE DAEMON",
        f"method={_METHOD_VALIDATE_ADDRESS} on this wallet's own primary address",
    )
    try:
        primary = adapter.call("get_address", {"account_index": account_index})
        address = (primary.get("addresses") or [{}])[0].get("address") or primary.get("address")
        if not address:
            fail("get_address returned no address, so the network could not be identified")
        else:
            verdict = adapter.call(_METHOD_VALIDATE_ADDRESS, {"address": address})
            nettype = verdict.get("nettype", "(not reported)")
            print(f"    nettype   {nettype.upper()}{network_banner(nettype)}", flush=True)
            print(f"    primary   {address[:12]}...{address[-6:]}  (truncated: not a secret, just noise)", flush=True)
            for finding in address_findings(verdict):
                fail(finding)
    except Exception as error:  # noqa: BLE001 -- checked: see check_balance; named, printed, counted.
        fail(f"could not identify the network: {error}")
    done(started)


def check_transfer_fields(adapter, account_index: int) -> list[dict]:
    started = step(
        3, "check every transfer field name the adapter depends on",
        f"method={_METHOD_GET_TRANSFERS} in=true account_index={account_index}",
    )
    transfers: list[dict] = []
    try:
        result = adapter.call(_METHOD_GET_TRANSFERS, {"in": True, "account_index": account_index})
        if "in" not in result:
            fail(f"{_METHOD_GET_TRANSFERS} returned no `in` key -- chains/monero.py reads result['in']")
        transfers = result.get("in") or []
    except Exception as error:  # noqa: BLE001 -- checked: see check_balance; named, printed, counted.
        fail(f"{_METHOD_GET_TRANSFERS} failed: {error}")

    print(f"    incoming transfers examined  {len(transfers)}", flush=True)
    if not transfers:
        print(
            "    (none)  <- NOT a pass. With no transfers to look at, every field name below is\n"
            "            UNCONFIRMED. Send a small amount to this wallet on stagenet and run this\n"
            "            again; that is the only thing that can confirm them.",
            flush=True,
        )
    lines, found = transfer_field_report(transfers)
    for line in lines:
        print(line, flush=True)
    failures.extend(found)
    done(started)
    return transfers


def check_real_scan(adapter, transfers: list[dict]) -> None:
    started = step(
        4, "run the REAL deposit scan over those transfers",
        "chains/monero_transfers.deposit_events_from_transfers(), the function the worker calls",
    )
    if not transfers:
        print("    (none)  <- skipped: no transfers. This step proves nothing on this run.", flush=True)
        done(started)
        return
    targets = sorted({entry.get(FIELD_ADDRESS, "(no address field)") for entry in transfers})
    for target in targets:
        try:
            scan = deposit_events_from_transfers(transfers, target, adapter.min_confirmations)
            print(f"    {target[:12]}...  credited {len(scan.events)}, deferred {len(scan.deferred)}", flush=True)
            for line in scan.deferred:
                print(f"        deferred  {line}", flush=True)
        except Exception as error:  # noqa: BLE001 -- checked: a refusal here IS the finding, so it is named and counted rather than raised out of the report and losing the other targets.
            fail(f"the deposit scan REFUSED real wallet data for {target[:12]}...: {error}")
    done(started)


def check_derive(adapter, enabled: bool) -> None:
    if not enabled:
        print("\n[5] derive a subaddress -- SKIPPED (pass --derive-address; it writes to the wallet)", flush=True)
        return
    started = step(5, "derive a subaddress (MUTATES THE WALLET)", f"method={_METHOD_CREATE_ADDRESS}")
    try:
        fresh = adapter.get_new_address("monero_chain_check")
        print(f"    derived   {fresh[:12]}...{fresh[-6:]}", flush=True)
        print("    NOTE: nothing watches this subaddress. It is real, and this script forgets it.", flush=True)
    except Exception as error:  # noqa: BLE001 -- checked: see check_balance; named, printed, counted.
        fail(f"{_METHOD_CREATE_ADDRESS} failed: {error}")
    done(started)


def main() -> int:
    args = parse_args()
    if not args.port:
        print(
            "REFUSED: no wallet port. Pass --port, or set XMR_RPC_PORT.\n"
            "  Nothing was checked, and this is NOT a clean result -- a check of nothing reads exactly\n"
            "  like a check that found nothing.\n"
            "  monero-wallet-rpc has no conventional port the way bitcoind has 8332; it binds wherever\n"
            "  --rpc-bind-port put it.",
            file=sys.stderr,
        )
        return 2

    # Password from the environment only. Never a command-line argument: argv is
    # visible to every process on the host through /proc, and this file is meant
    # to be safe to run on the live machine.
    #
    # can_spend is hardcoded False rather than read from configuration, so this
    # script stays safe to point at the payout wallet.
    adapter = MoneroAdapter(
        host=args.host, port=args.port, user=args.user, password=os.getenv("XMR_RPC_PASS", ""),
        account_index=args.account_index, can_spend=False,
    )

    announce(adapter, args)
    if check_balance(adapter, args.account_index) is None:
        print("\nSTOPPING: nothing else can be checked without a wallet.", flush=True)
        return 1
    check_network(adapter, args.account_index)
    transfers = check_transfer_fields(adapter, args.account_index)
    check_real_scan(adapter, transfers)
    check_derive(adapter, args.derive_address)

    print("\n" + "=" * 70, flush=True)
    print(verdict_text(failures, len(transfers)), flush=True)
    return exit_code(failures, len(transfers))


if __name__ == "__main__":
    sys.exit(main())
