#!/usr/bin/env python3
"""Send the devnet SOL deposit for an open swap, with every guard that failed today.

Role: file (operator entry point at the project root per CLAUDE.md rule 10; the
      decisions are the functions below and are callable with seeded inputs)
Reads: swap_terminal.db (swaps, deposit_events), the GRC daemon
      (validateaddress, for payout-address ownership), SOL_RPC_URL and
      SOL_DEPOSIT_ACCOUNT from the environment
Writes: nothing. It writes no row and no file. The deposit it sends is recorded
      by the deposit watcher, not by this.
Can move funds: it moves DEVNET SOL, which is worth nothing by construction and
      cannot be anything else -- see THE DEVNET PIN below. It cannot touch a
      mainnet Solana endpoint and it never signs a Gridcoin transaction.
Mainnet-safe: yes for Solana, by construction. It makes one READ-ONLY call to
      whatever Gridcoin daemon GRC_RPC_PORT names, so pointing that at mainnet
      means this asks mainnet whether an address is yours. It sends nothing
      there and holds no passphrase.

WHY THIS EXISTS, AND IT IS NOT CONVENIENCE.

Measured 2026-10-01. The devnet-SOL -> testnet-GRC rehearsal needed the customer
side of the swap -- a Solana payment to the shared account carrying the swap's
memo -- and nothing in this tree could do it. fund_testnets.py's Solana step is
read-only. So it was a shell block, pasted, and it went wrong four times in one
afternoon:

  stale shell variables    The block read the row into $TAG/$AMT/$PAYOUT and
      guarded on them. Run as two halves, the second half saw values from the
      PREVIOUS swap, passed its own guard, and sent a second 0.05 SOL to a swap
      that was already paid. That duplicate is still sitting in
      unattributable_deposits, correctly, as money nobody can claim.
  the wrong swap           With no new swap created, "the newest
      awaiting_deposit row" was a swap from three hours earlier -- because
      nothing in this system ever expires a swap. It had an autofilled Bitcoin
      testnet payout address, and 82.65 tGRC went to it. ismine: false.
  an unowned payout        Two of the three addresses tried were valid Gridcoin
      testnet addresses that the wallet holds no key for. `validateaddress`
      says well-formed, never yours, and nothing was asking the second question.
  the wrong terminal       Five separate runs hit 127.0.0.1:80 because the shell
      had no GRC_RPC_*, and one of those produced a verdict I read as "this
      chain cannot answer ownership" when it was a connection refusal.

None of those is a Python problem; every one is a shell problem, and the fix is
to stop doing it in a shell. Each is now a named refusal below, checked before
anything is sent, with the reason on screen.

THE DEVNET PIN, which is the one guarantee that cannot be argued with.

`--url devnet` is passed to the Solana CLI unconditionally and there is no flag
to change it, exactly as fund_testnets.py pins its hosts. SOL_RPC_URL is ALSO
checked and must name devnet: the two have to agree, because the deposit watcher
polls SOL_RPC_URL and a payment sent to a different cluster than the one being
watched is a payment nobody sees. Mainnet SOL cannot leave through this file.

WHY IT SHELLS OUT TO THE SOLANA CLI RATHER THAN SIGNING HERE.

chains/solana.py cannot send -- `can_spend = False` and send_to_address() raises
NotImplementedError, deliberately, because this terminal takes SOL in and never
pays it out. Building and signing a transfer here would mean this repository
reading a keypair file, and the operator's standing rule is that nothing here
moves, copies or reads back a key. The CLI already holds that responsibility and
is the thing that signed every manual send today. So the keypair PATH is passed
and the key itself is never opened by this process.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path
from typing import NamedTuple

# The rootless-import gap CLAUDE.md rule 10 names: the application imports its
# own modules as `from config import Config`, so swap_terminal/ has to be on the
# path before any of them can be reached. E402 is not raised here (pyproject's
# settings cover it for root entry points), so this is a plain comment rather
# than a noqa claiming a check nobody asked for (rule 19).
sys.path.insert(0, str(Path(__file__).resolve().parent / "swap_terminal"))

from chains.gridcoin import GridcoinAdapter
from chains.registry import missing_settings
from config import Config
from db import db_session
from microfortnights import format_duration
from services.helpers import utc_now_iso
from services.swap_view import elapsed_seconds, remaining_seconds

#: The cluster this file is allowed to touch, and the only one. Both the CLI
#: argument and the SOL_RPC_URL check below are pinned to it.
DEVNET_CLI_URL = "devnet"

#: What a devnet SOL_RPC_URL looks like. Substring rather than equality because a
#: devnet endpoint may be a provider's URL rather than api.devnet.solana.com, and
#: the thing that matters is that it is not mainnet.
DEVNET_MARKERS = ("devnet",)


class Refusal(NamedTuple):
    """One reason nothing was sent, and what to do about it.

    A type rather than a string so main() can print every reason at once instead
    of stopping at the first. An operator who is told three things are wrong
    fixes them in one pass; one who is told the first, fixes it, and is then told
    the second has paid for two round trips (rule 14).
    """

    what: str
    fix: str


def devnet_refusal(rpc_url: str) -> Refusal | None:
    """Refuse unless SOL_RPC_URL names devnet. THE DECISION, so it is testable.

    The CLI argument is pinned to devnet regardless, so this is not what stops
    mainnet SOL moving -- that is structural. What this stops is subtler and was
    worth its own check: a payment sent to devnet while the deposit watcher polls
    a DIFFERENT cluster is a payment nobody ever sees, which looks exactly like
    the attribution failures this project spent a day chasing.
    """
    if not rpc_url:
        return Refusal(
            "SOL_RPC_URL is not set in this process's environment",
            "export SOL_RPC_URL=https://api.devnet.solana.com in this shell. Nothing in the serving "
            "path reads a .env, so a value set in a file or in another shell does not reach here.",
        )
    if not any(marker in rpc_url.lower() for marker in DEVNET_MARKERS):
        return Refusal(
            f"SOL_RPC_URL is {rpc_url!r}, which does not name devnet",
            f"this file only ever sends to {DEVNET_CLI_URL} and there is no flag to change that. A "
            f"deposit sent to devnet while the watcher polls somewhere else is a deposit nobody sees.",
        )
    return None


def account_refusal(account: str) -> Refusal | None:
    """Refuse unless the shared deposit account is configured."""
    if not account:
        return Refusal(
            "SOL_DEPOSIT_ACCOUNT is not set in this process's environment",
            "export it in this shell. It is the account every SOL swap shares, and the memo is what "
            "tells the swaps apart -- so without it there is nowhere to send.",
        )
    return None


def swap_refusals(swap: dict | None, already_seen: int) -> list[Refusal]:
    """Everything wrong with the swap itself. PURE, and the reason it is a list.

    `already_seen` is the number of deposit_events rows this swap already has,
    and a non-zero count is the duplicate-send guard. It is counted from the
    ROW rather than inferred from the swap's status, for the same reason
    services/unattributable_deposit_service.py had to learn on 2026-10-01: a
    status is a summary that moves, a deposit_events row is the deposit itself.
    """
    if swap is None:
        return [Refusal(
            "no SOL swap is awaiting a deposit",
            "create one on the swap page first. Nothing in this system expires a swap, so an OLD open "
            "swap would also match -- which is how 82.65 tGRC went to a three-hour-old swap's "
            "autofilled Bitcoin address. Check the id this prints against the page you just used.",
        )]
    refusals = []
    if swap.get("deposit_tag") is None:
        refusals.append(Refusal(
            f"swap {swap['id']} has no deposit_tag",
            "SOL attributes deposits by memo and the account is shared, so a payment without the tag "
            "cannot be matched to it. This should be impossible -- create_swap() allocates the tag in "
            "the same transaction as the swap row -- so a swap in this state is worth investigating "
            "rather than working around.",
        ))
    if not swap.get("expected_input_amount"):
        refusals.append(Refusal(
            f"swap {swap['id']} has no expected_input_amount",
            "there is nothing to send. The amount is read from the row precisely so it cannot disagree "
            "with the quote the customer accepted.",
        ))
    if already_seen:
        refusals.append(Refusal(
            f"swap {swap['id']} already has {already_seen} deposit event(s) recorded",
            "it has been paid. Sending again produces a second payment with the same memo to a swap "
            "that will not credit it -- which is exactly the duplicate now sitting in "
            "unattributable_deposits from 2026-10-01, unclaimable by anyone.",
        ))
    return refusals


def ownership_refusal(payout_address: str) -> Refusal | None:
    """Refuse unless the GRC wallet holds the key for the payout address.

    FOR A REHEARSAL ONLY, and the asymmetry is deliberate and stated because it
    reverses in production: paying yourself is the POINT here, and for a real
    customer an owned payout address would be the alarm instead. This file is
    testnet tooling, so it checks the rehearsal's invariant.

    THREE ANSWERS, because chains/base.address_ownership() has three.
    `None` is refused and is NOT treated as a no: on 2026-10-01 a `None` from a
    shell with no GRC_RPC_* was read as "this chain cannot answer ownership",
    which was a connection refusal wearing a verdict's clothes. The reason the
    daemon gave is printed, so that cannot happen silently again.
    """
    missing = missing_settings(Config.RPC, "GRC")
    if missing:
        return Refusal(
            f"GRC is not configured in this shell ({', '.join(missing)} unset), so ownership of "
            f"{payout_address} could not be asked",
            "export them in this shell -- the same ones the supervisor needs. This is NOT a statement "
            "about the address; five runs on 2026-10-01 hit 127.0.0.1:80 for exactly this reason.",
        )
    result = GridcoinAdapter(**Config.RPC["GRC"]).address_ownership(payout_address)
    if result.verdict is True:
        return None
    if result.verdict is False:
        return Refusal(
            f"the GRC wallet holds NO KEY for the payout address {payout_address}",
            "a payout there would be unspendable by you, which is what happened to 82.65 tGRC on "
            "2026-10-01. Create a swap whose payout address came from `getnewaddress` on this daemon.",
        )
    return Refusal(
        f"ownership of {payout_address} is NOT ESTABLISHED -- {result.why}",
        "nobody answered, which is not the same as 'not yours'. If the reason above is a connection "
        "error the endpoint is wrong or down and the question is still answerable.",
    )


def open_sol_swap(db, swap_id: str = "") -> dict | None:
    """The SOL swap awaiting a deposit, or the named one. Reads, writes nothing.

    Newest first when no id is given, and main() PRINTS the id it chose, because
    "the newest open swap" was the wrong swap once today and the only thing that
    would have caught it was seeing the id on screen.
    """
    if swap_id:
        row = db.execute("SELECT * FROM swaps WHERE id = ?", (swap_id,)).fetchone()
        return dict(row) if row else None
    row = db.execute(
        "SELECT * FROM swaps WHERE from_asset = 'SOL' AND status = 'awaiting_deposit'"
        " ORDER BY created_at DESC LIMIT 1"
    ).fetchone()
    return dict(row) if row else None


def open_sol_swap_count(db) -> int:
    """How many SOL swaps are awaiting a deposit. For the line that says so.

    open_sol_swap() takes ORDER BY created_at DESC LIMIT 1 when no id is given,
    and its docstring already records that "the newest open swap" was the wrong
    swap once -- which is why the id is printed. Printing the id answers "which
    one did it pick"; it does not answer "out of how many", and those are
    different questions for a reader deciding whether to trust the pick.

    MEASURED 2026-10-01: THREE SOL swaps were open at once, all for 0.01 SOL, all
    with the same payout address. The id on screen was the only distinguishing
    mark, and nothing said the other two existed. Rule 3's "state the
    denominator", on a tool that sends money.
    """
    row = db.execute(
        "SELECT COUNT(*) AS n FROM swaps WHERE from_asset = 'SOL' AND status = 'awaiting_deposit'"
    ).fetchone()
    return int(row["n"] if isinstance(row, dict) else row[0])


def quote_age_line(swap: dict, now_iso: str) -> str:
    """How old the rate this swap will pay out at is, and that nothing re-prices it.

    WHY A SEND TOOL SAYS THIS. Established by reading rather than assumed:
    services/swap_view.quote_window()'s docstring records that NOTHING in the tree
    sets swaps.status='expired' and no worker reads swaps.expires_at at all -- the
    only read is get_quote_or_raise(), which guards QUOTE REUSE before a swap
    exists. So a swap that has been open for hours still pays at the rate it was
    quoted at, whatever the market has done since.

    MEASURED 2026-10-01: three swaps sat open from 20:36 UTC onward, quoted when
    1 SOL bought 8995 GRC. Four hours later the same SOL bought 10719 -- GRC had
    fallen from $0.0141 to $0.0110, so paying the oldest of them would have
    delivered about 17 GRC less than a fresh quote, roughly 16% short.

    It reports and does not refuse. Paying a stale quote is sometimes exactly what
    a rehearsal wants, and whether a customer gets the old rate or a new one
    changes what they are paid -- which is live posture and the operator's
    decision (rule 16), not a send tool's.
    """
    age = elapsed_seconds(swap.get("created_at"), now_iso)
    window = remaining_seconds(swap.get("expires_at"), now_iso)
    if age is None:
        return "(not measurable -- this swap has no readable created_at)"
    aged = f"quoted {format_duration(age)} ago"
    if window is None:
        return f"{aged}; expires_at is unreadable, so whether the window passed was NOT established"
    if window >= 0:
        return f"{aged}; {format_duration(window)} left in the quoted window"
    return (
        f"{aged}; the quoted window PASSED {format_duration(-window)} ago. NOTHING re-prices a swap -- "
        f"no worker reads swaps.expires_at and nothing sets status='expired' -- so this will pay out at "
        f"the OLD rate. Create a new swap if you want the current one"
    )


def deposit_events_for(db, swap_id: str) -> int:
    row = db.execute(
        "SELECT COUNT(*) AS n FROM deposit_events WHERE swap_id = ?", (swap_id,)
    ).fetchone()
    return int(row["n"] if isinstance(row, dict) else row[0])


def transfer_command(keypair: str, account: str, amount, tag) -> list[str]:
    """The argv handed to the Solana CLI. Separated so it can be asserted on.

    --url devnet is here and unconditional. --allow-unfunded-recipient because
    the shared account may legitimately hold nothing yet; rent is the sender's
    problem and the CLI handles it.

    THE MEMO IS THE TAG AND NOTHING ELSE. chains/solana_memo.py reads the whole
    memo as the discriminator, so a decorated memo ("swap 5", "tag=5") does not
    match and the payment strands.
    """
    return [
        "solana", "transfer", account, str(amount),
        "--from", keypair,
        "--fee-payer", keypair,
        "--with-memo", str(tag),
        "--url", DEVNET_CLI_URL,
        "--allow-unfunded-recipient",
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--keypair", required=True,
                        help="path to the SENDING devnet keypair. Never read by this process; "
                             "handed to the Solana CLI, which signs.")
    parser.add_argument("--swap", default="",
                        help="a specific swap id. Default: the newest SOL swap awaiting a deposit, "
                             "whose id is printed before anything is sent.")
    parser.add_argument("--dry-run", action="store_true",
                        help="run every check and print the command, send nothing.")
    args = parser.parse_args()

    print("pay test deposit -- DEVNET SOL ONLY, and there is no flag that changes that")
    print(f"  database        {Config.DB_PATH}")
    print(f"  SOL_RPC_URL     {os.environ.get('SOL_RPC_URL', '(unset)')}  <- what the watcher polls")
    print(f"  deposit account {os.environ.get('SOL_DEPOSIT_ACCOUNT', '(unset)')}")
    print(f"  keypair         {args.keypair}  <- not opened here; the Solana CLI signs")

    refusals: list[Refusal] = [
        refusal for refusal in (devnet_refusal(os.environ.get("SOL_RPC_URL", "")),
                                account_refusal(os.environ.get("SOL_DEPOSIT_ACCOUNT", "").strip()))
        if refusal
    ]

    with db_session(Config.DB_PATH) as db:
        swap = open_sol_swap(db, args.swap)
        seen = deposit_events_for(db, swap["id"]) if swap else 0
        open_count = open_sol_swap_count(db)
    refusals.extend(swap_refusals(swap, seen))

    if swap:
        chosen = (
            f"named by --swap; {open_count} SOL swap(s) are open"
            if args.swap
            else f"the NEWEST of {open_count} open SOL swap(s), picked by created_at DESC. Pass --swap "
                 f"with an id to choose"
        )
        print(f"  swap            {swap['id']}  created {swap.get('created_at')}")
        print(f"  chosen          {chosen}")
        print(f"  quote           {quote_age_line(swap, utc_now_iso())}")
        print(f"  memo tag        {swap.get('deposit_tag')}  <- the whole memo, undecorated")
        print(f"  amount          {swap.get('expected_input_amount')} SOL  <- read from the row, so it "
              f"cannot disagree with the quote")
        print(f"  payout          {swap.get('payout_address')}")
        ownership = ownership_refusal(str(swap.get("payout_address") or ""))
        if ownership:
            refusals.append(ownership)
        else:
            print("  payout owned    YES -- the GRC wallet holds the key, asked of the daemon just now")

    if refusals:
        print(f"\n  REFUSED: {len(refusals)} reason(s). NOTHING was sent and nothing was written.")
        for refusal in refusals:
            print(f"    ! {refusal.what}")
            print(f"      -> {refusal.fix}")
        return 1

    command = transfer_command(args.keypair, os.environ["SOL_DEPOSIT_ACCOUNT"].strip(),
                               swap["expected_input_amount"], swap["deposit_tag"])
    print(f"\n  about to send   {' '.join(command)}")
    if args.dry_run:
        print("  dry run         nothing was sent")
        return 0
    import time  # noqa: PLC0415 -- only the send path times itself; the refusal path needs no clock
    started = time.monotonic()
    done = subprocess.run(command, check=False)  # noqa: S603 -- argv is transfer_command()'s own list: a literal CLI name, this file's constants, and three values read from the swap ROW. No shell, no user string.
    print(f"  solana exit     {done.returncode} in {format_duration(time.monotonic() - started)}")
    if done.returncode == 0:
        print("  next            the deposit watcher polls every 15s; watch "
              "swap_terminal/runtime/deposit_watcher.log")
    return done.returncode


if __name__ == "__main__":
    sys.exit(main())
