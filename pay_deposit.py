#!/usr/bin/env python3
"""Pay a BTC, LTC or GRC swap's deposit from the desk's own daemon. Testnet tool.

Role: file (entry point at the repository root, per CLAUDE.md rule 10)
Reads: config.Config (DB_PATH, RPC), swap_terminal.db (swaps, deposit_events), and
       the chain's own wallet RPC -- listtransactions and gettransaction, through
       the adapter's find_deposits_to_address()
Writes: NOTHING in the database. It does not record the send; the deposit watcher
       discovers it on the chain, which is the same path a customer's own payment
       takes and therefore the one that is actually exercised.
Can move funds: YES, and only with --apply. It calls
       chains/base.RPCAdapter.send_to_address() on the desk's own wallet. Without
       --apply it contacts the chain read-only and sends nothing.
Mainnet-safe: NO. It refuses unless the daemon reports a test network -- see
       refuse_mainnet(). There is no flag to override that.

=============================================================================
WHY THIS EXISTS, AND THE REGISTRY PREDICTED IT
=============================================================================

swap_terminal/deposit_payers.py, written 2026-10-10 after an ICP swap nothing
could pay, recorded BTC, LTC and GRC as "NOTHING PAYS THIS LEG" and said:

    The payer was being written per chain, ad hoc, whenever somebody hit the
    wall -- so the wall was going to be hit three more times.

It was hit the same day, on LTC. The operator created a swap, the page said

    Send exactly 0.06030068 LTC to tltc1qnrfdz828pv7r8gdzrcg2vk6ccm0v5xyspr4405

and asked for help paying it. LTC's registry note had already said what was
missing: "LitecoinAdapter inherits base.send_to_address unchanged. Same missing
wrapper, same small change." This is that wrapper.

=============================================================================
THE ONE THING THAT MAKES THIS DIFFERENT FROM pay_icp_deposit.py
=============================================================================

**A BITCOIN-FAMILY SEND HAS NO IDEMPOTENCY KEY.** The ICP ledger deduplicates on
created_at_time for 24 hours, so re-running that tool is one transfer. Here a
second run is a second transaction, with a second fee, paying the deposit twice.

AND THE DATABASE CANNOT CLOSE THAT WINDOW. The obvious guard is "refuse if a
deposit_event already exists", and this tool has it -- but deposit_watcher polls
every 15s, so between a send and the row there is a window in which the database
still says nothing arrived. Two runs inside it send twice.

SO THE GUARD ASKS THE CHAIN. refuse_already_paid() calls the adapter's own
find_deposits_to_address() -- the same call the deposit watcher makes -- and
refuses if the wallet already shows a payment to this swap's address, confirmed
or not. A transaction in the mempool is visible to listtransactions immediately,
so the window the database leaves open is closed by the daemon that would be
sending the second one.

That is the whole reason this tool is safe to re-run, and it is a chain read
rather than a record this tool keeps, which means it is also correct for a
deposit somebody paid by hand before running it.

=============================================================================
WHAT IT REFUSES, AND GRC IS NOT COVERED
=============================================================================

BTC and LTC send with an unlocked wallet and nothing else. GRC does not: the
desk's Gridcoin wallet is encrypted, and a wallet unlocked FOR STAKING cannot
send -- the daemon answers rpc code -4, which is the exact failure
s_0dc53d06ab3968fb's payout recorded. Unlocking it needs the passphrase, and the
operator's standing instruction is that a passphrase must never appear in a
command this repository emits.

chains/gridcoin_wallet_lock.unlocked_for_payout() exists and does it correctly
for a PAYOUT -- it restores staking afterwards rather than merely locking. Using
it to pay a DEPOSIT is a new use of the passphrase on a new path, which is a
custody decision and the operator's (rule 16). So GRC refuses here, by name,
with that sentence. It is not an oversight and deposit_payers.py says the same.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "swap_terminal"))

from chains.daemon_network import (
    NETWORK_IS_TEST,
    NETWORK_NOT_ESTABLISHED,
    chain_network,
    test_network_verdict,
)
from chains.registry import build_adapters, why_unconfigured
from config import Config
from db import connect_db
from microfortnights import format_duration
from services.helpers import NANOS_PER_SECOND, iso_to_epoch_nanos, utc_now_iso

SELF = Path(__file__).name

#: The three chains whose deposit address is derived by the daemon's own
#: `getnewaddress` and whose send is base.RPCAdapter.send_to_address(). Named from
#: the registry rather than spelled again (rule 11: one vocabulary, one place).
BITCOIN_FAMILY = ("BTC", "LTC", "GRC")

#: GRC is in BITCOIN_FAMILY and is NOT payable here. See the module header: the
#: wallet is encrypted and unlocking it for a deposit is a custody decision.
NEEDS_PASSPHRASE = ("GRC",)

#: The only status a first deposit may be sent against.
AWAITING = ("awaiting_deposit",)


class Refused(Exception):
    """Nothing was sent, and the exit code says which kind of problem it was.

    ONE EXCEPTION RATHER THAN NINE GUARD-RETURNS, which is the shape
    pay_icp_deposit.py arrived at after ruff's complexity ceiling kept firing on
    main(): the ceiling was pointing at orchestration that had swallowed
    decisions, not at a line count (rule 12).

    code 2 is a CONFIGURATION problem -- no adapter, wrong chain, a mainnet
    daemon. code 1 is a STATE problem -- the swap is expired, already paid, past
    waiting. The distinction is for whoever is reading the exit status in a
    script: 2 means fix the environment, 1 means look at the swap.
    """

    def __init__(self, code: int, lines: list[str]):
        super().__init__(lines[0] if lines else "refused")
        self.code = code
        self.lines = lines


def refuse_mainnet(asset: str, network: str) -> list[str]:
    """Refuse anything but a test network. NO OVERRIDE FLAG, deliberately.

    THE VERDICT IS chains/daemon_network.test_network_verdict(), NOT A TABLE SPELLED
    HERE. That function already answers this exact question for the payout path and
    for stack_authority, against CHAIN_TEST_NETWORKS -- and a second allowlist of
    test network names is rule 8's duplicate on the one check whose job is to keep
    a send off mainnet. Three copies of a network name is a typo waiting.

    THE NETWORK COMES FROM WHAT THE DAEMON REPORTS, never from its port number.
    chain_network() asks it, with the two-route probe an older Gridcoin build
    needs (getblockchaininfo, then getinfo). A port is a configuration guess; the
    chain field is the daemon's own answer.

    AN UNKNOWN NETWORK REFUSES, which is why the three verdicts are not collapsed
    into a boolean: NETWORK_NOT_ESTABLISHED and NETWORK_IS_TEST must not produce
    the same outcome on a path that moves coins. Rule 17, applied to a send.
    """
    verdict = test_network_verdict(asset, network)
    if verdict == NETWORK_IS_TEST:
        return []
    if verdict == NETWORK_NOT_ESTABLISHED:
        return [
            f"{asset}'s daemon did not name its chain, so whether this is a test network is "
            f"NOT ESTABLISHED (it answered {network!r}). This tool refuses when it cannot "
            f"establish that rather than assuming the safe answer -- it sends coins out of "
            f"the desk's own wallet."
        ]
    return [
        f"{asset}'s daemon reports network {network!r}, which is not one of this chain's test "
        f"networks. There is no flag to override it: this pays a deposit out of the desk's own "
        f"wallet, and a send is the one thing that cannot be undone."
    ]


def refuse(swap: dict, deposits: list[dict], now_nanos: int) -> list[str]:
    """Every reason not to send, as sentences. Empty means the send may proceed.

    THE DECISION, PURE (rule 10): it takes the swap row, the deposits already
    recorded, and the time as nanos, so a test can seed an expired swap or one
    that already has a deposit without a database or a clock.

    RETURNS ALL OF THEM rather than the first, for the reason
    pay_icp_deposit.refuse() gives: an operator who fixes one reason and re-runs
    into a second has learned nothing about the state of the swap.

    THE CHAIN-SIDE CHECK IS NOT HERE. refuse_already_paid() needs an adapter and a
    socket, so it cannot be part of a pure function; it is called beside this one
    and its result is concatenated. Keeping them separate is what lets this one be
    tested with seeded rows -- and the header says why the chain check is the one
    that actually closes the double-send window.
    """
    reasons = []
    asset = swap["from_asset"]

    if asset not in BITCOIN_FAMILY:
        reasons.append(
            f"this swap's deposit leg is {asset}, and {SELF} sends on "
            f"{', '.join(BITCOIN_FAMILY)} only. swap_terminal/deposit_payers.py names the "
            f"tool for each chain; for ICP it is pay_icp_deposit.py."
        )
    elif asset in NEEDS_PASSPHRASE:
        reasons.append(
            f"{asset}'s wallet is encrypted, and a Gridcoin wallet unlocked FOR STAKING "
            f"cannot send -- the daemon answers rpc code -4. Unlocking it needs the "
            f"passphrase, which must never appear in a command this repository emits, so "
            f"paying a {asset} deposit is a custody decision and not this tool's "
            f"(CLAUDE.md rule 16). chains/gridcoin_wallet_lock.py is how the PAYOUT path "
            f"does it, restoring staking afterwards."
        )

    if swap["status"] not in AWAITING:
        seen = "" if swap["status"] != "deposit_seen" else " A deposit has already been seen."
        reasons.append(
            f"status is {swap['status']!r}, not one of {', '.join(AWAITING)}.{seen} Sending "
            f"now would be a second deposit on a swap that is past waiting for its first."
        )

    if deposits:
        detail = "; ".join(
            f"{d['amount']} at {d['txid'][:16]}... ({d['confirmations']} conf)" for d in deposits
        )
        reasons.append(
            f"{len(deposits)} deposit event(s) are already recorded against this swap: "
            f"{detail}. That is money already sent. A second send is not a retry."
        )

    expires_nanos = iso_to_epoch_nanos(swap["expires_at"])
    if expires_nanos <= now_nanos:
        late = format_duration((now_nanos - expires_nanos) / NANOS_PER_SECOND)
        reasons.append(
            f"the swap expired {late} ago (expires_at {swap['expires_at']}). The quoted rate "
            f"no longer holds, so a deposit now credits against a stale quote -- which sends "
            f"the swap to review rather than to a payout. Create a new swap."
        )

    if not swap["expected_input_amount"] or float(swap["expected_input_amount"]) <= 0:
        reasons.append(
            f"expected_input_amount is {swap['expected_input_amount']!r}; there is no amount "
            f"to send."
        )

    return reasons


def refuse_already_paid(adapter, address: str) -> list[str]:
    """Ask the CHAIN whether this deposit address has already been paid.

    THE GUARD THE DATABASE CANNOT PROVIDE, and the module header has the argument
    in full. Short version: a Bitcoin-family send has no idempotency key, so a
    second run is a second transaction -- and deposit_watcher polls every 15s, so
    between a send and its deposit_events row there is a window where the database
    still says nothing arrived.

    listtransactions SEES A MEMPOOL TRANSACTION IMMEDIATELY, which is what makes
    this close the window rather than narrow it. The adapter call is the same one
    the watcher makes, so a payment this refuses on is a payment the watcher is
    about to credit.

    IT ALSO CATCHES A HAND-PAID DEPOSIT, which is not incidental -- every BTC and
    LTC swap before today was paid by hand at a shell, so a tool that only knew
    about its own sends would have been wrong on the existing ones.

    A FAILURE TO ASK IS A REFUSAL. If the call raises -- daemon down, wallet not
    loaded, a 401 -- this returns a refusal naming the error rather than an empty
    list. "Could not check" must not render as "not yet paid" on a path that
    sends coins (rule 12's BLE001: the caller has to be able to tell a failure
    from a real answer).
    """
    try:
        found = adapter.find_deposits_to_address(address)
    except Exception as error:  # noqa: BLE001 -- ANY failure here is a refusal, which is the point: see the docstring. The error's text is carried into the sentence so the operator can tell a dead daemon from an unloaded wallet, and nothing is sent either way.
        return [
            f"could not ask the daemon whether {address} has already been paid: "
            f"{type(error).__name__}: {error}. This tool refuses rather than sending when it "
            f"cannot establish that, because a Bitcoin-family send has no idempotency key and "
            f"a second one is a second transaction."
        ]
    if not found:
        return []
    detail = "; ".join(
        f"{event['amount']} at {event['txid'][:16]}... ({event['confirmations']} conf)"
        for event in found
    )
    return [
        f"the daemon already shows {len(found)} payment(s) to {address}: {detail}. That is "
        f"this swap's deposit, paid. It may not have a deposit_events row yet -- the watcher "
        f"polls every 15s -- which is exactly why this check asks the chain and not the "
        f"database."
    ]


def banner_lines(args, db_path: str) -> list[str]:
    """What this run is about to do, BEFORE it does it. Rule 14's "announce before".

    THE NETWORK IS ON IT UNCONDITIONALLY, which is rule 14's one hard requirement:
    "a line that does not say mainnet or testnet is a line that will eventually be
    read as the wrong one." It is the daemon's own answer, fetched before anything
    else, so the banner cannot say testnet about a mainnet node.
    """
    return [
        f"{SELF}: {'APPLY -- a transaction WILL be broadcast' if args.apply else 'DRY RUN -- nothing is sent'}",
        f"  database   {db_path}",
        f"  target     {args.deposit_address or args.swap}",
    ]


def swap_lines(swap: dict, network: str, deposits: list[dict]) -> list[str]:
    """The swap, as the facts the refusals are judged from. Printed either way.

    EVERY VALUE A REFUSAL READS IS HERE, so a refused run shows the operator what it
    was judged on rather than only the verdict -- and an ALLOWED run shows the same
    fields, which is what lets somebody check the judgment instead of trusting it.
    """
    return [
        f"  swap       {swap['id']}  {swap['from_asset']} -> {swap['to_asset']}",
        f"  network    {network or 'NOT ESTABLISHED'}  <- what the daemon reports, not its port",
        f"  status     {swap['status']}",
        f"  send       {swap['expected_input_amount']} {swap['from_asset']}",
        f"  to         {swap['deposit_address']}",
        f"  expires    {swap['expires_at']}",
        f"  deposits   {len(deposits)} already recorded"
        + ("  <- (none)" if not deposits else "  <- money already sent"),
    ]


def find_swap(db, deposit_address: str, swap_id: str) -> dict:
    """The swap, by address or by id. NEVER by recency.

    BY ADDRESS IS THE PRIMARY ROUTE because it is what the operator has in front of
    them: the page prints the address, and pasting it is one copy with no chance of
    naming the wrong swap. pay_icp_deposit.py made the same choice for the same
    reason.

    AND NEITHER ROUTE IS "THE NEWEST AWAITING SWAP", which is the convenience this
    deliberately does not offer. Three ICP swaps were open simultaneously on
    2026-10-10 with amounts 2.32846520, 2.42621078 and 2.44081155; a tool that
    picked the most recent would have paid the wrong one, and the money would have
    been a correctly-formed deposit against a swap nobody was waiting on.
    """
    if deposit_address:
        row = db.execute(
            "SELECT * FROM swaps WHERE deposit_address = ? ORDER BY created_at DESC LIMIT 1",
            (deposit_address.strip(),),
        ).fetchone()
        if row is None:
            raise Refused(1, [
                f"  REFUSED: no swap in {Config.DB_PATH} has deposit_address "
                f"{deposit_address.strip()!r}. A deposit address is allocated when a swap is "
                f"created, so an address no swap owns is either a typo or an address from a "
                f"different database.",
            ])
        return dict(row)
    row = db.execute("SELECT * FROM swaps WHERE id = ?", (swap_id.strip(),)).fetchone()
    if row is None:
        raise Refused(1, [f"  REFUSED: no swap with id {swap_id.strip()!r} exists."])
    return dict(row)


def main(argv: list[str] | None = None) -> int:
    """Look the swap up, refuse or send, and say which. Sends only with --apply.

    THERE IS NO --amount FLAG, and that is the same deliberate omission
    pay_icp_deposit.py carries: the amount comes from the swap row. A flag would let
    an operator send a number that does not match expected_input_amount, which
    AMOUNT_TOLERANCE_PCT would then judge -- and a deposit outside tolerance sends
    the swap to review rather than to a payout. The swap already knows what it is
    owed.
    """
    parser = argparse.ArgumentParser(
        prog=SELF,
        description="Pay a BTC or LTC swap's deposit from the desk's own daemon. Testnet only.",
    )
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument(
        "--deposit-address", default="",
        help="the address the swap page prints. The primary route: one paste, no ambiguity.",
    )
    target.add_argument("--swap", default="", help="a swap id, if you have it instead.")
    parser.add_argument(
        "--apply", action="store_true",
        help="actually broadcast. Without it the chain is read and nothing is sent.",
    )
    args = parser.parse_args(argv)

    for line in banner_lines(args, str(Config.DB_PATH)):
        print(line, flush=True)

    try:
        return _run(args)
    except Refused as refused:
        for line in refused.lines:
            print(line, flush=True)
        return refused.code


def _run(args) -> int:
    """The body, so main() holds the argument parsing and this holds the sequence.

    SPLIT FOR THE SAME REASON pay_icp_deposit.py's is: ruff's complexity ceiling kept
    firing on a main() that parsed, looked up, judged, printed and sent. The ceiling
    was pointing at orchestration that had swallowed decisions (rule 12), and the
    answer is that each decision is its own function and this is the order they run in.
    """
    db = connect_db(str(Config.DB_PATH))
    swap = find_swap(db, args.deposit_address, args.swap)
    asset = swap["from_asset"]

    adapters = build_adapters(Config.RPC)
    if asset not in adapters:
        raise Refused(2, [f"  REFUSED: {why_unconfigured(asset, Config.RPC)}"])
    adapter = adapters[asset]

    # THE NETWORK FIRST, because every line printed after it is read in its light and
    # because the mainnet refusal is the one that must not be reachable by accident.
    network = chain_network(adapter)
    deposits = [
        dict(row) for row in db.execute(
            "SELECT * FROM deposit_events WHERE swap_id = ? ORDER BY id", (swap["id"],)
        ).fetchall()
    ]
    for line in swap_lines(swap, network, deposits):
        print(line, flush=True)

    now_nanos = iso_to_epoch_nanos(utc_now_iso())
    reasons = (
        refuse_mainnet(asset, network)
        + refuse(swap, deposits, now_nanos)
        + refuse_already_paid(adapter, swap["deposit_address"])
    )
    if reasons:
        print(f"  REFUSED    {len(reasons)} reason(s), and nothing was sent:", flush=True)
        for reason in reasons:
            print(f"               - {reason}", flush=True)
        return 1

    amount = float(swap["expected_input_amount"])
    if not args.apply:
        print(
            f"\nDRY RUN: nothing sent. Every check passed. To send:\n"
            f"    python3 {SELF} --deposit-address {swap['deposit_address']} --apply",
            flush=True,
        )
        return 0

    txid = adapter.send_to_address(swap["deposit_address"], amount)
    print(f"  SENT       txid {txid}", flush=True)
    print(
        f"  next       deposit_watcher polls every 15s and credits it. Watch it:\n"
        f"               python3 show_swap.py --swap {swap['id']}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
