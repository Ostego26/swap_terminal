#!/usr/bin/env python3
"""Send the ICP deposit for one open swap, idempotently, with every guard named.

Role: file (operator entry point at the project root per CLAUDE.md rule 10; the
      decisions are the functions below and are callable with seeded inputs)
Reads: swap_terminal.db (swaps, deposit_events), the ledger canister
      (icrc1_fee, icrc1_balance_of via the chain adapter)
Writes: nothing. It writes no row and no file. The deposit it sends is recorded
      by the deposit watcher, not by this.
Can move funds: YES, and this is the only tool in the tree that moves the
      CUSTOMER side of a swap. It calls the ledger's legacy `transfer` from the
      desk's own dfx identity to the swap's deposit subaccount. Nothing is sent
      without --apply.
Mainnet-safe: NOT APPLICABLE IN THE USUAL SENSE, and the reason is worth stating
      rather than leaving a reader to work out. The Internet Computer HAS NO
      TESTNET -- the official documentation says so -- so there is no "point it
      at the test chain" posture available here. Two environments exist:

        a local replica   its token is LICP, its own genesis, worth nothing.
                          `fund_desk.py` prints that in its own output. This is
                          what the tool is for.
        mainnet ICP       real money, no faucet, no free path (the cycles faucet
                          closed to the general public in 2025).

      So the guard is the LEDGER CANISTER ID: a local replica's is not mainnet's
      (mainnet's is ryjl3-tyaaa-aaaaa-aaaba-cai, which icp/dfx.json names under
      `remote.id.ic` precisely so dfx refuses to create one there). This tool
      prints the ledger it is about to talk to, every run, before anything moves.

=============================================================================
WHY THIS EXISTS
=============================================================================

The terminal shows a deposit address and an amount, and until now nothing in the
tree could pay one on the ICP leg. HANDOFF.md section 4 records the consequence:
an ICP -> GRC swap sitting at "NOT SENT -- blocked on the dfx transport". The
customer side of a SOL swap has had `pay_test_deposit.py` since 2026-10-01; this
is its ICP counterpart.

=============================================================================
EVERY GUARD HERE IS A FAILURE SOMEBODY ALREADY PAID FOR
=============================================================================

pay_test_deposit.py's header records four, measured in one afternoon of doing
this in a shell. Three of them are chain-independent and are re-guarded here:

  stale shell variables   A block that read the row into $AMT/$PAYOUT and
      guarded on them, run as two halves, saw the PREVIOUS swap's values in the
      second half and sent a second payment to an already-paid swap. THE ANSWER
      HERE: there is no --amount flag. The amount is read from the swap row in
      the same transaction as every other field, once. A caller cannot supply a
      number that disagrees with the swap, because a caller cannot supply one.

  the wrong swap          "The newest awaiting_deposit row" was a swap from
      three hours earlier, because nothing in this system expires a swap, and
      82.65 tGRC went to its autofilled payout address. THE ANSWER HERE: the
      swap is looked up by the deposit address the operator pastes off the page,
      or by an explicit swap id. There is no "newest" selector and there will
      not be one.

  the wrong terminal      Five runs hit 127.0.0.1:80 because the shell had no
      RPC settings, and one refusal was read as a chain verdict. THE ANSWER
      HERE: the ledger canister id, the dfx service and the transport are
      printed before any call, and an unset ledger id is a named refusal.

And one that is specific to this ledger, and is the reason this file is not a
one-liner:

  a second transfer       The ICP ledger deduplicates `transfer` on
      (from, to, amount, fee, memo, created_at_time) for 24 hours. That makes
      created_at_time the idempotency key, and chains/icp.send_to_address()
      refuses to send without one. A key taken from the CLOCK is a different
      number on every run, so a retry after a timeout is a SECOND TRANSFER
      wearing the shape of a fix. THE ANSWER HERE: the key is derived from the
      swap's recorded created_at, by services/helpers.iso_to_epoch_nanos(), so
      re-running this command is idempotent BY CONSTRUCTION rather than by the
      operator remembering a flag.

=============================================================================
WHY IT DOES NOT USE THE ICP ADAPTER TO SEND
=============================================================================

chains/icp.ICPAdapter carries `can_spend = False`, honestly: every call it makes
is `--identity anonymous`, which can read a balance and cannot sign a transfer.
Giving the adapter a spending identity would change the PAYOUT path's adapter,
which is fund movement and the operator's call (rule 16).

So this builds its own transport the way fund_desk.py already does for minting --
`dfx_transport(service, timeout, identity=...)` -- and calls the same
`transfer_argument()` and `transfer_block_index()`. Three callers, one
money-moving string builder, one reply reader (rule 8). Reads still go through
the adapter, anonymously, because they can.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Mapping
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent / "swap_terminal"))

from config import Config
from db import connect_db
from microfortnights import format_duration
from services.helpers import NANOS_PER_SECOND, iso_to_epoch_nanos, utc_now_iso

from swap_terminal.chains.coin_amounts import amount_to_base_units
from swap_terminal.chains.icp import (
    ICP_DECIMALS,
    ICPCallFailed,
    dfx_transport,
    transfer_argument,
    transfer_block_index,
)
from swap_terminal.chains.registry import build_adapters

SELF = Path(__file__).name

#: The statuses in which a swap is still waiting for its deposit. Narrower than
#: fee_sweep.py's active list on purpose: `deposit_seen` and `confirming` mean a
#: deposit has ALREADY ARRIVED, and sending another is the duplicate this file
#: exists to prevent.
AWAITING = ("awaiting_deposit",)

#: The ledger's dedup window, measured on the local replica 2026-10-06 and
#: recorded in chains/icp.py: the ledger answered
#:     Err = variant { TxTooOld = record { allowed_window_nanos = 86_400_000_000_000 } }
#: 86_400_000_000_000ns is 24 hours.
DEDUP_WINDOW_NANOS = 86_400_000_000_000


def find_swap(conn, *, deposit_address: str = "", swap_id: str = "") -> dict | None:
    """The one swap named by `deposit_address` or `swap_id`, or None.

    ONE QUERY, ONE ROW, AND NO ORDERING. There is deliberately no "most recent"
    path: ordering by created_at is how a three-hour-old swap got paid and 82.65
    tGRC went to an address nobody held the key for.

    The deposit address is UNIQUE enough to be the lookup key -- db.py declares
    `idx_swaps_deposit_address` on it -- and it is the value the operator is
    reading off the page, so it is the value least likely to be transcribed from
    the wrong row.
    """
    if deposit_address:
        row = conn.execute(
            "SELECT * FROM swaps WHERE deposit_address = ?", (deposit_address,)
        ).fetchone()
    else:
        row = conn.execute("SELECT * FROM swaps WHERE id = ?", (swap_id,)).fetchone()
    return dict(row) if row else None


def deposit_rows_for(conn, swap_id: str) -> list[dict]:
    """Every deposit_event already recorded against this swap. Read-only."""
    return [
        dict(r)
        for r in conn.execute(
            "SELECT txid, vout, amount, confirmations, credited_at FROM deposit_events "
            "WHERE swap_id = ?",
            (swap_id,),
        ).fetchall()
    ]


def refuse(swap: dict, deposits: list[dict], now_nanos: int) -> list[str]:
    """Every reason not to send, as sentences. Empty means the send may proceed.

    THE DECISION, PURE (rule 10). It takes the swap row, the deposits already
    recorded, and the current time as nanos -- so a test can seed an expired
    swap, a swap with a deposit already on it, or a swap whose created_at is
    outside the ledger's window, without a database or a clock.

    RETURNS ALL OF THEM rather than the first. An operator who fixes one reason
    and re-runs only to meet a second has learned nothing about the state of the
    swap; rule 14's "state what the number means" applied to refusals.
    """
    reasons = []

    if swap["from_asset"] != "ICP":
        reasons.append(
            f"this swap's deposit leg is {swap['from_asset']}, not ICP. {SELF} sends on the "
            f"ICP ledger only; sending ICP to a swap expecting {swap['from_asset']} would "
            f"be an unattributable deposit."
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

    created_nanos = iso_to_epoch_nanos(swap["created_at"])
    age_nanos = now_nanos - created_nanos
    if age_nanos >= DEDUP_WINDOW_NANOS:
        age = format_duration(age_nanos / NANOS_PER_SECOND)
        reasons.append(
            f"the swap was created {age} ago, which is outside the ledger's 24-hour dedup "
            f"window. created_at is the idempotency key, so the ledger would answer TxTooOld "
            f"and nothing would move. Paying it needs a key from somewhere other than the "
            f"swap, which forfeits dedup -- a live-posture decision, not this tool's "
            f"(CLAUDE.md rule 16)."
        )

    if not swap["expected_input_amount"] or swap["expected_input_amount"] <= 0:
        reasons.append(
            f"expected_input_amount is {swap['expected_input_amount']!r}; there is no amount "
            f"to send."
        )

    return reasons


class Refused(Exception):
    """Nothing was sent, with an exit code and the lines that say why.

    AN EXCEPTION RATHER THAN NINE `return` STATEMENTS, and the shape is the point
    rather than a way past a lint ceiling. Every precondition here has the same
    consequence -- print why, send nothing, exit non-zero -- and writing that out
    nine times put nine returns and nine branches in main(), which is CLAUDE.md
    rule 12's defect exactly: "a main() past the ceiling is orchestration that
    has swallowed decisions."

    With the refusals raised, main() reads as the sequence it actually is
    (resolve, report, send) and preflight() owns the question "may this proceed".
    The decisions stay testable: `refuse()` is still pure, and preflight's own
    refusals carry their code so a test can assert 2 for a configuration fault
    and 1 for a state fault without reading stderr.

    CODE 2 IS CONFIGURATION, CODE 1 IS STATE. An operator who gets 2 has to edit
    their environment; one who gets 1 has to look at the swap. Collapsing both to
    1 would make a missing ledger id look like an expired swap.
    """

    def __init__(self, code: int, lines: list[str]):
        super().__init__(lines[0] if lines else "refused")
        self.code = code
        self.lines = lines


def banner_lines(args, icp: Mapping) -> list[str]:
    """The block printed before anything is read or sent (rule 14).

    PURE, SO THE BANNER IS TESTABLE. "Announce before, not only after" is only
    worth something if the announcement is correct, and the things most worth
    getting right -- which ledger, which identity, whether funds will move -- are
    exactly what a test should assert without a replica.

    It takes the parsed args and the RPC mapping rather than six keyword
    arguments: the six were all strings and floats, which is the transposition
    hazard `transfer_argument()` answers by naming, and here there is a real
    object to pass instead.
    """
    mode = "APPLY -- funds WILL move" if args.apply else "DRY RUN -- nothing will be sent"
    signer = f", with --identity {args.identity}" if args.identity else ", with dfx default identity"
    return [
        f"{SELF}: {mode}",
        f"  database      {Config.DB_PATH}",
        f"  ledger        {icp['ledger_canister_id'] or '(unset)'}"
        "  <- not mainnet's ryjl3-tyaaa-aaaaa-aaaba-cai",
        f"  transport     docker compose exec -T {icp['service']} dfx{signer}",
        "                identities live in the replica container, so this must run on the "
        "HOST (docker on PATH)",
        f"  call timeout  {format_duration(icp['timeout'])}",
    ]


def swap_lines(swap: dict, deposits: list[dict], key_nanos: int) -> list[str]:
    """What this swap is, as the operator needs to read it back (rule 14)."""
    return [
        f"  swap          {swap['id']}  {swap['from_asset']} -> {swap['to_asset']}",
        f"  status        {swap['status']}",
        f"  created_at    {swap['created_at']}",
        f"  expires_at    {swap['expires_at']}",
        f"  amount        {swap['expected_input_amount']} ICP  <- from the swap row; there is "
        "no --amount flag",
        f"  to            {swap['deposit_address']}",
        f"  payout to     {swap['payout_address']}  ({swap['to_asset']}, not touched here)",
        f"  deposits seen {len(deposits) or '(none)'}",
        f"  idempotency   {key_nanos}  <- derived from created_at, so re-running this exact",
        "                command is the SAME transfer, not a second one",
    ]


def send_outcome(reply: str) -> tuple[bool, list[str]]:
    """Did the money move? (moved, lines to print).

    THE DECISION THIS FILE EXISTS AROUND, extracted so it can be asserted with a
    seeded reply string rather than only by sending real funds (rule 10). Three
    cases, and they are not symmetric:

      a block index   the transfer is in the ledger. TxDuplicate reaches here as
                      a block index too, via transfer_block_index(), and that is
                      the success it is -- the ledger is saying "this exact
                      transfer already happened, here is where". chains/icp.py
                      shipped a version that RAISED on it and records why that
                      was a defect: a worker retrying after a timeout marked a
                      payout that SUCCEEDED as failed.

      no index        WHETHER ANYTHING MOVED IS NOT ESTABLISHED. Not "it failed".
                      The reply has to be read, and the message says that rather
                      than claiming either outcome.

      BadFee          named because it is the one a reader will want to retry and
                      must not retry blind: a retry with a different fee is a
                      DIFFERENT transfer under the dedup key, so it would not
                      dedup against the first.
    """
    index = transfer_block_index(reply)
    if index:
        return True, [
            f"  SENT          block index {index}",
            "                TxDuplicate also reports here, as the success it is -- it means "
            "this exact transfer already happened",
        ]
    return False, [
        "THE LEDGER REPLIED WITHOUT A BLOCK INDEX, so whether anything moved is NOT "
        "established here. Read the reply:",
        f"  {reply.strip()[:400]!r}",
        "`BadFee` names the fee it expected and means nothing moved; it is NOT retried "
        "automatically, because a retry with a different fee is a different transfer under "
        "the dedup key and would not dedup against the first.",
    ]


def parse_args(argv: list[str] | None = None):
    parser = argparse.ArgumentParser(
        prog=SELF,
        description=(
            "Send one swap's ICP deposit from the desk's own account to the swap's deposit "
            "subaccount. Prints what it would do and sends nothing unless --apply is given."
        ),
    )
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument(
        "--deposit-address",
        default="",
        help="the deposit address shown on the swap page. The preferred selector: it is the "
        "value being read off the screen, so it cannot name a different row than the one the "
        "operator is looking at.",
    )
    target.add_argument("--swap", default="", help="a swap id, if you have it instead.")
    parser.add_argument(
        "--identity",
        default=os.getenv("ICP_DESK_IDENTITY", ""),
        help="the dfx identity to sign as. Default: none, meaning NO --identity flag and "
        "therefore dfx's current default inside the replica container -- the identity whose "
        "principal is ICP_OWNER_PRINCIPAL and whose balance fund_desk.py tops up. Overriding "
        "it signs as somebody else, and the balance check is then about a different account; "
        "the run says so when you do.",
    )
    parser.add_argument(
        "--apply", action="store_true",
        help="actually call the ledger. Without it, nothing is sent.",
    )
    return parser.parse_args(argv)


def load_swap(db_path: str, *, deposit_address: str, swap_id: str):
    """(swap, deposits) from one connection, or (None, []). Closes what it opens."""
    conn = connect_db(db_path)
    try:
        swap = find_swap(conn, deposit_address=deposit_address, swap_id=swap_id)
        if swap is None:
            return None, []
        return swap, deposit_rows_for(conn, swap["id"])
    finally:
        conn.close()


def preflight(args, icp: Mapping) -> dict:
    """Everything that must hold before a send, or raise `Refused`.

    OWNS THE QUESTION "may this proceed", so main() does not. Reads the database
    and the ledger; sends nothing and signs nothing.
    """
    ledger = icp["ledger_canister_id"]
    if not ledger:
        raise Refused(2, [
            "REFUSED, nothing read: ICP_LEDGER_CANISTER_ID is unset. It is environment state "
            "-- every fresh replica issues a different id -- so there is no default worth "
            "guessing. Read it with:",
            f"  docker compose exec -T {icp['service']} cat /repo/.dfx/local/canister_ids.json",
        ])

    swap, deposits = load_swap(
        Config.DB_PATH, deposit_address=args.deposit_address, swap_id=args.swap
    )
    if swap is None:
        named = args.deposit_address or args.swap
        field = "deposit_address" if args.deposit_address else "id"
        raise Refused(1, [
            f"REFUSED, nothing sent: no swap in {Config.DB_PATH} has {field} {named!r}. "
            f"NOTHING was looked up by recency as a fallback, deliberately -- see find_swap().",
        ])

    now_nanos = iso_to_epoch_nanos(utc_now_iso())
    key_nanos = iso_to_epoch_nanos(swap["created_at"])
    amount = float(swap["expected_input_amount"])

    print()
    for line in swap_lines(swap, deposits, key_nanos):
        print(line)

    reasons = refuse(swap, deposits, now_nanos)
    if reasons:
        raise Refused(1, [
            f"REFUSED, nothing sent. {len(reasons)} reason(s):",
            *(f"  ! {reason}" for reason in reasons),
        ])

    adapters = build_adapters(Config.RPC)
    if "ICP" not in adapters:
        raise Refused(2, [
            "REFUSED, nothing sent: the ICP adapter was not built, which means "
            "ICP_LEDGER_CANISTER_ID or ICP_OWNER_PRINCIPAL is missing. Either alone is "
            "insufficient -- see chains/icp.py.",
        ])
    adapter = adapters["ICP"]

    # THE FEE AND THE BALANCE ARE READ FROM THE LEDGER, both through the adapter,
    # both anonymously -- reads work without an identity, and chains/icp.py's
    # `can_spend = False` is honest about the rest.
    #
    # chain_fee() is `_nat("icrc1_fee") / 10**8` and its docstring says why a
    # number copied into Python would be a second authority (rule 8). The first
    # version of this file stripped non-digits out of the raw reply, which would
    # have read a `nat64`-suffixed reply as 1000064 -- a fee 100x too large,
    # answered by BadFee, with nothing moving and the reason looking like a
    # ledger fault. A FEE THIS TOOL GUESSED is that same BadFee, and
    # chains/icp.py does not retry one.
    try:
        fee_e8s = amount_to_base_units(adapter.chain_fee(), ICP_DECIMALS)
        desk_balance = adapter.get_balance()
    except ICPCallFailed as error:
        raise Refused(1, [
            f"REFUSED, nothing sent: the ledger could not be read: {error}",
        ]) from error

    print(f"\n  desk balance  {desk_balance} ICP in the account get_balance() reads")
    if args.identity:
        print(f"                NOTE: signing as {args.identity!r}, so the balance above may be "
              f"a DIFFERENT account than the one that will be debited")
    print(f"  ledger fee    {fee_e8s} e8s  <- read from the ledger via icrc1_fee, never guessed")

    if desk_balance < amount:
        raise Refused(1, [
            f"REFUSED, nothing sent: the desk holds {desk_balance} ICP and this send is "
            f"{amount} ICP plus a fee. Top it up with:",
            f"  python3 fund_desk.py --asset ICP "
            f"--target {amount + desk_balance + 1:.0f} --apply",
        ])

    return {
        "swap": swap, "amount": amount, "fee_e8s": fee_e8s,
        "key_nanos": key_nanos, "ledger": ledger,
    }


def main() -> int:
    args = parse_args()
    icp = Config.RPC["ICP"]

    for line in banner_lines(args, icp):
        print(line)

    try:
        plan = preflight(args, icp)
    except Refused as refused:
        print(file=sys.stderr)
        for line in refused.lines:
            print(line, file=sys.stderr)
        return refused.code

    swap, amount = plan["swap"], plan["amount"]

    if not args.apply:
        print(
            f"\nDRY RUN -- NOTHING WAS SENT. Re-run with --apply to send {amount} ICP to "
            f"{swap['deposit_address']}.\n"
            f"Because the idempotency key is derived from the swap, running --apply twice is "
            f"ONE transfer: the second answers TxDuplicate with the first block index."
        )
        return 0

    e8s = amount_to_base_units(amount, ICP_DECIMALS)
    argument = transfer_argument(
        to_account=swap["deposit_address"], e8s=e8s, fee_e8s=plan["fee_e8s"],
        created_at_time_nanos=plan["key_nanos"],
    )
    print(f"\n  sending       {amount} ICP = {e8s} e8s, fee {plan['fee_e8s']} e8s")

    send = dfx_transport(icp["service"], icp["timeout"], identity=args.identity)
    try:
        reply = send(plan["ledger"], "transfer", argument)
    except ICPCallFailed as error:
        print(
            f"\nTHE CALL FAILED AND WHETHER ANYTHING MOVED IS NOT ESTABLISHED by this message: "
            f"{error}\nRe-run the SAME command: the key is derived from the swap, so if the "
            f"first attempt landed, the retry answers TxDuplicate rather than sending again.",
            file=sys.stderr,
        )
        return 1

    moved, lines = send_outcome(reply)
    print()
    for line in lines:
        print(line, file=None if moved else sys.stderr)
    if not moved:
        return 1
    print(f"  confirm       python3 show_swap.py {swap['id']}")
    print("                the deposit watcher records the deposit_event; this tool does not")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
