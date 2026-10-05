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

THIS PARAGRAPH'S PREMISE EXPIRED AND IS CORRECTED RATHER THAN LEFT (rule 16: a
wrong comment is a bug). It said "chains/solana.py cannot send -- `can_spend =
False` and send_to_address() raises NotImplementedError, deliberately, because
this terminal takes SOL in and never pays it out." That was true when this file
was written. Since 2026-10-02 chains/solana.py has a devnet-only payout path
(send_to_address() previews by default and raises SolanaSendNotArmed, never
NotImplementedError), and since 2026-10-03 `can_spend` is derived from
SOL_PAYOUT_KEYPAIR_PATH rather than hardcoded.

WHAT IS UNCHANGED, and it is the reason this file still shells out: THIS SCRIPT
NEVER OPENS A KEY. It pays a test deposit INTO the terminal, which is the
customer's side of a swap and has nothing to do with the payout path; the
keypair PATH is handed to the Solana CLI and the key itself is never read by
this process. The application's own signing is confined to
chains/solana_signing.signed_transfer_wire(), which is a different file, a
different direction and reachable only with an arming token.
"""

from __future__ import annotations

import argparse
import os
import shutil
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
from chains.solana import SolanaAdapter
from chains.solana_units import (
    SOL_DECIMALS,
    amount_to_base_units,
    base_units_to_amount,
    decimal_amount,
)
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
    """Refuse when the terminal's GRC wallet OWNS the payout address.

    THIS CHECK INVERTED ON 2026-10-05, AND THE DOCSTRING IT REPLACED PREDICTED IT.
    What stood here was:

        Refuse unless the GRC wallet holds the key for the payout address.
        FOR A REHEARSAL ONLY, and the asymmetry is deliberate and stated because
        it reverses in production: paying yourself is the POINT here, and for a
        real customer an owned payout address would be the alarm instead.

    Both sentences were true. The premise under them was that the GRC daemon this
    terminal talks to IS the operator's own wallet, so "ismine there" meant "I can
    spend what I receive". That premise ended when GRC_RPC_PORT moved from 25715
    (the testnet GUI wallet) to 25779 (the desk daemon): the adapter is now the
    DESK, and an address the desk owns is the desk paying ITSELF -- which moves
    nothing, marks the swap completed anyway, and is the exact condition
    services/payout_service refuses on the real order path.

    Measured when it fired: swap s_7170571c428b7912, SOL -> GRC, payout address
    mg3gJAmhADxf2ScRuXu7HXM2oixxiQG2Ap. This function refused it for holding no
    key, while the terminal's own gate requires precisely that. Two guards, one
    address, opposite verdicts -- rule 8's drift, except the copies never agreed
    in the first place; they agreed about a topology that changed underneath them.

    WHAT IS NO LONGER CHECKED BY ANYONE, AND IT IS NAMED RATHER THAN DROPPED. The
    original guard existed because 82.65 tGRC went to an address nobody held a key
    for on 2026-10-01. That risk is unchanged and this process can no longer rule
    it out: the wallet that SHOULD own a customer payout is a different daemon,
    with different credentials, which this shell does not hold. So the caller
    prints that it was not checked, with the command that would check it, instead
    of a line claiming an assurance nobody produced.

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
    if result.verdict is False:
        return None
    if result.verdict is True:
        return Refusal(
            f"the terminal's GRC wallet OWNS the payout address {payout_address}",
            "that is the desk paying itself: the coins never leave, and the swap is marked completed "
            "anyway. services/payout_service refuses the same condition on the real order path, so a "
            "deposit sent now would be taken and then stranded. Use a payout address from the "
            "CUSTOMER wallet, not from the daemon GRC_RPC_PORT names.",
        )
    return Refusal(
        f"ownership of {payout_address} is NOT ESTABLISHED -- {result.why}",
        "nobody answered, which is not the same as 'not yours'. If the reason above is a connection "
        "error the endpoint is wrong or down and the question is still answerable.",
    )


def ownership_lines(payout_address: str) -> list[str]:
    """What to print once ownership_refusal() has passed. Lines, not prints.

    EXTRACTED 2026-10-05 rather than raising a lint ceiling. Adding the
    not-checked paragraph below pushed main() past PLR0915, and rule 12 is
    explicit that a main() over the ceiling is orchestration that has swallowed a
    decision -- so the decision comes out and becomes callable with a seeded
    address, which is also what makes the wording testable.

    The second half is the load-bearing part. It says what this process did NOT
    verify, because the alternative is a blank where an assurance used to be: this
    line read "payout owned YES -- the GRC wallet holds the key" until the adapter
    moved to the desk, and silently dropping it would leave a reader thinking the
    old check still ran.
    """
    return [
        "  payout owned    NO by the terminal's GRC wallet, asked of the daemon just now",
        "                  <- which is what a real payout needs: an address the desk owns",
        "                     would be the desk paying itself",
        "  recipient can   NOT CHECKED. The wallet that should own this address is a",
        "  spend it        DIFFERENT daemon and this shell holds no credentials for it.",
        "                  82.65 tGRC went to an address nobody held a key for on",
        "                  2026-10-01 and nothing here can rule that out. To check:",
        f"                      gridcoinresearchd -testnet validateaddress {payout_address}",
    ]


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


#: Lamports left unspent so the transfer's own fee and a little slack are covered.
#:
#: A Solana signature costs 5000 lamports and this transaction carries one, plus a
#: memo instruction. 50_000 is ten times that, which is 0.00005 SOL -- far below
#: anything worth saving and far above anything the fee can become. It is NOT a
#: rent-exempt allowance: the recipient is the shared deposit account, which
#: already holds a balance (readiness prints it), so no account is being created.
FEE_HEADROOM_LAMPORTS = 50_000


def sender_pubkey(keypair: str) -> str:
    """The PUBLIC key of a keypair file, via the CLI. "" when it cannot be had.

    NO KEY MATERIAL IS READ HERE. `solana address --keypair <path>` prints the
    public key and nothing else; this process never opens the file, exactly as it
    never opens it to sign. That is the same boundary transfer_command() draws, and
    it is what makes a balance check possible without crossing it.

    RESOLVED WITH shutil.which, NOT A BARE NAME. ruff's S607 flags a partial
    executable path, and which() is the answer this repo already uses --
    swap_terminal_desktop.py resolves the browser binary that way. A suppression
    claiming "solana is on PATH" would be the kind rule 19 forbids, and resolving
    it makes "the CLI is not installed" an explicit case rather than an OSError two
    lines later.

    "" means NOT ESTABLISHED, never "no such key": no CLI, a CLI that failed, or
    empty output. The caller must not turn that into a refusal.

    Its own function because sender_refusal() was at seven returns and ruff's
    PLR0911 ceiling is six -- and the honest fix for that is to extract the part
    that is really a separate question (rule 19, and rule 10's "the decision is the
    smallest testable piece"), not to raise the ceiling.
    """
    solana = shutil.which("solana")
    if not solana:
        return ""
    try:
        found = subprocess.run(  # noqa: S603 -- checked: argv is a which()-resolved absolute path, a literal subcommand and flag, and the keypair path this tool was given. No shell, no user string.
            [solana, "address", "--keypair", keypair],
            capture_output=True, text=True, check=False, timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return found.stdout.strip() if found.returncode == 0 else ""


class SenderFunding(NamedTuple):
    """What was learned about the sender's balance, and the line that says so.

    BOTH, because a verdict with no sentence is a check nobody can see ran.
    Measured on the operator's host 2026-10-02, on code written two hours
    earlier: sender_refusal() returned None for "verified and sufficient" AND
    for "could not ask", so the dry run printed nothing in either case. The
    operator's block showed `payout owned YES` and then silence about funding,
    and there was no way to tell a passed check from a skipped one.

    That is rule 14's "make did-nothing look different from did-work" in the one
    file whose next statement hands money to the Solana CLI -- and it is the same
    defect class this session has been removing from the operator's tooling all
    evening, shipped into it by me.
    """

    refusal: Refusal | None
    line: str


def sender_funding(keypair: str, amount: float) -> SenderFunding:
    """Whether the SENDING keypair can cover the transfer, and what was established.

    WHY THIS EXISTS. Every other refusal in this file is about configuration or
    about the swap; nothing asked whether the money is there. So the tool printed

        about to send   solana transfer ... 0.25 ...

    and handed the send to the CLI, which is where an insufficient balance would
    surface -- after this file had announced it was sending. Rule 14's "announce
    before, not only after" cuts both ways: a block that says it is about to do
    something has claimed it can.

    It matters more at 0.25 SOL than it did at 0.01. Every earlier rehearsal sent
    0.01; the operator's 2026-10-02 swap is twenty-five times that, and the funding
    keypair has been paying for a day of rehearsals.

    A FAILURE TO ASK IS NOT A REFUSAL, and it is not silence either. No CLI, no
    public key, or a cluster that will not answer all let the send proceed --
    because "I could not check your balance" is not "you have no money", and
    refusing on it would block a send that would have worked. But the LINE says
    which happened, so the operator is never left to guess whether the check ran.
    Rule 17's line between a reason to believe and having checked, printed rather
    than merely obeyed.
    """
    sender = sender_pubkey(keypair)
    if not sender:
        return SenderFunding(None, (
            "NOT CHECKED -- `solana address --keypair` did not answer, so the sender's balance is "
            "unknown. The send will proceed and the CLI's own failure is the only thing that will "
            "catch an empty keypair"
        ))
    try:
        adapter = SolanaAdapter(**Config.RPC["SOL"])
        result = adapter.call("getBalance", sender, {"commitment": "finalized"})
        lamports = int(result["value"] if isinstance(result, dict) else result)
    except Exception as error:  # noqa: BLE001 -- checked: every failure means "the balance could not be read", the line below says so and names the exception, and nothing turns it into a refusal. A cluster that will not answer must not block a send that would have worked, and the HTTP 429s this devnet account draws make that a live case.
        return SenderFunding(None, (
            f"NOT CHECKED -- {type(error).__name__} reading {sender}'s balance "
            f"({str(error)[:70]}). Unknown, NOT insufficient: the send proceeds"
        ))

    needed = amount_to_base_units(amount, SOL_DECIMALS) + FEE_HEADROOM_LAMPORTS
    # decimal_amount() ON EVERY FIGURE, not str() or an f-string's default.
    # Measured on the operator's host 2026-10-02: this line printed
    #
    #     against 0.25005 needed (0.25 plus 5e-05 for the fee)
    #
    # because base_units_to_amount() returns a float and a float's repr goes
    # exponential at both ends -- 0.00005 is str()'d as "5e-05". Scientific
    # notation in a money figure, on the screen somebody reads immediately before
    # sending. chains/solana_units.decimal_amount() already existed for exactly
    # this (it was private to solana_pay.py, for the URI; see its docstring for the
    # measurement), so it moved rather than being written twice (rule 8).
    held = decimal_amount(base_units_to_amount(lamports, SOL_DECIMALS))
    want = decimal_amount(base_units_to_amount(needed, SOL_DECIMALS))
    headroom = decimal_amount(base_units_to_amount(FEE_HEADROOM_LAMPORTS, SOL_DECIMALS))
    sending = decimal_amount(amount)
    if lamports >= needed:
        return SenderFunding(None, (
            f"{held} SOL in {sender}, against {want} needed "
            f"({sending} plus {headroom} for the fee) <- asked of the cluster just now"
        ))
    return SenderFunding(
        Refusal(
            f"the sending keypair {sender} holds {held} SOL and this transfer needs {want} SOL "
            f"({sending} plus {headroom} for the fee)",
            "fund it with `solana airdrop 1 --url devnet` (devnet airdrops are rate-limited, so retry "
            "rather than assuming it failed), or create a smaller swap. Nothing was sent.",
        ),
        f"{held} SOL in {sender} -- NOT ENOUGH, see the refusal below",
    )


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


def main(argv: list[str] | None = None) -> int:
    """An `argv` parameter because every other root tool here has one.

    show_swap.py, show_fees.py, open_swap.py, swap_readiness.py and
    swap_terminal_desktop.py all take it. The reason is testability, and it was
    earned twice on 2026-10-02: a mutation deleting the funding check's CALL from
    this function survived the whole suite, because every test called the refusal
    directly and nothing drove main() -- a check written correctly and wired to
    nothing, which is indistinguishable from no check. Without this parameter a
    test must monkeypatch sys.argv, and a bare parse_args() under pytest reads
    pytest's own arguments, which cost swap_readiness.py two tests the same day.
    """
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--keypair", required=True,
                        help="path to the SENDING devnet keypair. Never read by this process; "
                             "handed to the Solana CLI, which signs.")
    parser.add_argument("--swap", default="",
                        help="a specific swap id. Default: the newest SOL swap awaiting a deposit, "
                             "whose id is printed before anything is sent.")
    parser.add_argument("--dry-run", action="store_true",
                        help="run every check and print the command, send nothing.")
    args = parser.parse_args(argv)

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
            for line in ownership_lines(str(swap.get("payout_address") or "")):
                print(line)
        funding = sender_funding(args.keypair, float(swap["expected_input_amount"]))
        print(f"  sender funded   {funding.line}")
        if funding.refusal:
            refusals.append(funding.refusal)

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
