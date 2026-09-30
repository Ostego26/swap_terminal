#!/usr/bin/env python3
"""What every saved XRP testnet account holds, and how much of it can move.

Role: file (operator entry point, at the project root per CLAUDE.md rule 10)
Reads: a rippled JSON-RPC endpoint, and the saved faucet files under
        chains/xrp_testnet.KEY_DIRECTORY -- addresses only. The seed sits in
        those same files and this script never touches it.
Writes: nothing. No file, no database, no ledger.
Can move funds: NO. It builds no transaction, signs nothing, and never calls
        send_to_address() or submit. There is no flag that broadcasts.

        IT DOES IMPORT FROM chains/xrp_signing.py, and saying "imports no
        signing path" would have been the easier sentence and a false one. What
        it takes from that module is reserve_drops(), which is arithmetic over
        two numbers the server reported -- the one implementation of the reserve
        calculation, reused rather than repeated (rule 8). No signing function is
        imported, no seed is read, and tests/test_xrp_balances.py walks this
        file's AST to hold that: it asserts the names called here include no
        sign, submit or send.
Mainnet-safe: yes to run. It names the network from the SERVER's network_id
        rather than from the URL, and refuse_mainnet() raises before any account
        is read if that id says mainnet.

WHY A BALANCE NEEDS THREE NUMBERS AND NOT ONE

`Balance` is not what an account can spend. The XRP Ledger holds a BASE RESERVE
against every account and an OWNER RESERVE against every ledger object it owns,
and an escrow is a ledger object. So the account that funds a swap sees its
balance fall by the escrowed drops AND its reserve rise while the escrow is
outstanding -- two movements, one of which no `Balance` line explains.

That is the question this script was written for, 2026-09-29: the operator
watched 66.10 GRC leave and 66.09 arrive seconds later and asked why. On the
Gridcoin side the answer was that both addresses come out of one wallet. On the
XRP side the arithmetic is genuinely different, and printing a bare balance
would have hidden it.

AND THE FIRST VERSION GOT THE ESCROW HALF WRONG, found by the operator's first
run the same day. Both of their accounts printed the same 1 XRP escrow with the
sentence "it raises this account's reserve by one increment", and one of those
accounts reported OwnerCount 0 two lines above:

    rBfM7je6e9Ca2cMvuRn7cr9xExFgDa5NGx
        reserve   1.000000 XRP (1000000 base + 200000 x 0 owned objects)
        escrow    1.000000 XRP to rBfM7je6... -- ... raises this account's reserve

account_objects lists an escrow under BOTH parties: the object is threaded into
the sender's owner directory and the destination's. Only the SENDER pays its
reserve, which is why the other account read OwnerCount 1 for the same escrow.
So the line claimed a reserve cost on an account that bears none, and described
incoming money as locked-away money. `Account` is the sender, and comparing it to
the address being reported is what separates the two; each escrow now says OUT or
IN and says whose it is. The OwnerCount cross-check that would have caught it at
the time is in report_account() and prints MISMATCH.

The same run printed `CancelAfter=843784768`, which is unreadable and was also
already in the past -- 2026-09-27T00:39:28Z, two days earlier. That escrow was
cancellable and its 1 XRP recoverable, and nothing said so. Both timestamps are
now decoded and each says whether it has passed.

Both reserve figures are read from server_info.validated_ledger. They are NOT
hardcoded here and must not be: the base reserve has been 20 XRP, then 10, then
1, and a compiled-in number would eventually report spendable balance that does
not exist. chains/xrp_signing.reserve_drops() is the one implementation of that
arithmetic and this script calls it rather than repeating it (rule 8).

USAGE

    python3 xrp_balances.py                          # every saved faucet account
    python3 xrp_balances.py --account rSomeAddress   # one account, saved or not

EXIT CODES
    0   every account asked for was read
    1   at least one could not be, and the reason is printed beside it
    3   INCONCLUSIVE -- there were no accounts to look at, which is not a pass
"""

from __future__ import annotations

import argparse
import datetime
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "swap_terminal"))

# NO `noqa: E402` ON THESE FIVE, unlike every other root script here. Ruff does
# not flag them: the sys.path.insert above sits after `from __future__` and the
# module docstring and nothing else, and E402 allows exactly that prologue. The
# other root scripts carry the suppression because they import something BEFORE
# the insert. Rule 19 -- a suppression is a claim you checked, so an unnecessary
# one is a false claim, and RUF100 catches it.
from chains.xrp_address import is_valid_classic_address
from chains.xrp_escrow import cancel_inputs, cancel_verdict
from chains.xrp_signing import reserve_drops
from chains.xrp_testnet import TESTNET_URL, refuse_mainnet, rpc, saved_faucet_accounts
from chains.xrp_units import from_drops, unix_from_ripple_time
from step_console import Console

NOTHING_TO_LOOK_AT = 3

# An escrow is the ledger object THIS repo creates, so it gets named separately
# from the rest of an account's owned objects. account_objects takes a `type`
# filter; asking for escrows specifically means an account with forty trust
# lines does not bury the one row that explains a swap.
ESCROW_TYPE = "escrow"


def _when(field: str, ripple_seconds) -> str:
    """A FinishAfter as a date, and whether it has passed.

    ITS CancelAfter BRANCH IS NO LONGER REACHED FROM THIS FILE.
    chains/xrp_escrow.cancel_verdict() answers that one, because it is a DECISION and this is
    a formatter -- it could not be called with a seeded object and a fixed clock while it lived
    here (rule 10). The branch stays rather than being deleted for one reason: it is what makes
    this function safe to call on either field, and the alternative is a formatter that raises
    on an input it obviously handles. If no second caller appears, it should go (rule 9).

    RAW RIPPLE SECONDS ARE UNREADABLE and printing them was a defect. The first
    version of this script showed an operator `CancelAfter=843784768` on their own
    escrow. That is 2026-09-27T00:39:28Z -- two days before the line was printed,
    so the escrow was already cancellable and its 1 XRP recoverable, and nothing
    on the screen said so. Rule 14: state what the number means, next to it.
    """
    if ripple_seconds is None:
        return f"{field:<12} not set"
    unix = unix_from_ripple_time(ripple_seconds)
    stamp = datetime.datetime.fromtimestamp(unix, datetime.UTC).isoformat()
    passed = time.time() >= unix
    if field == "CancelAfter":
        verdict = ("PASSED -- this escrow can be canceled now and its drops returned to the sender"
                   if passed else "not yet reached")
    else:
        verdict = ("PASSED -- the destination may finish it now" if passed
                   else "not yet reached; it cannot be finished before this")
    return f"{field:<12} {ripple_seconds} = {stamp} ({verdict})"


def escrows_held(address: str) -> tuple[list[dict], str]:
    """Every outstanding escrow on `address`, and a sentence about the ask.

    Returns ([], reason) rather than raising when the call fails. An escrow list
    is an EXPLANATION of the reserve, not the balance itself -- a server that
    will not answer account_objects should not stop the balance being printed,
    and a silent empty list would be indistinguishable from an account with no
    escrows (rule 14). The reason string is what keeps those two apart.
    """
    try:
        result = rpc("account_objects", {"account": address, "type": ESCROW_TYPE,
                                         "ledger_index": "validated"})
    except Exception as error:  # noqa: BLE001 -- checked: this is requests/HTTP/JSON shape, all of which mean "the escrow list is unavailable" and none of which mean "there are no escrows". The caller cannot mistake the two because the reason is returned alongside and printed.
        return [], f"account_objects unavailable ({type(error).__name__}: {error})"
    if result.get("error"):
        return [], f"account_objects said {result.get('error')}: {result.get('error_message')}"
    objects = result.get("account_objects")
    if not isinstance(objects, list):
        return [], f"account_objects returned {type(objects).__name__}, not a list"
    return objects, f"{len(objects)} outstanding"


def report_account(console: Console, address: str, base_reserve, inc_reserve) -> bool:
    """Print one account's three numbers. True if the ledger answered."""
    try:
        result = rpc("account_info", {"account": address, "ledger_index": "validated"})
    except Exception as error:  # noqa: BLE001 -- checked: network and JSON failures alike mean this account was NOT read, which is exactly what False reports to main(); the exception text is printed rather than swallowed.
        return console.check(address, f"{type(error).__name__}: {error}", "account_info", False)
    if result.get("error"):
        # An account the faucet funded and the ledger has never seen reads
        # actNotFound. That is a real, common answer -- the funding transaction
        # did not make it -- and it must not print as a network failure.
        return console.check(address, f"{result.get('error')}: {result.get('error_message')}",
                             "an account on this ledger", False)
    data = result.get("account_data") or {}
    raw = data.get("Balance")
    if not isinstance(raw, str):
        return console.check(address, f"Balance={raw!r}", "a drop count as a JSON string", False)
    drops = int(raw)
    # Converted ONCE, here, because it is read twice below -- by the reserve
    # arithmetic and by the OwnerCount cross-check -- and two separate
    # `int(...) if not None` expressions is how those two stop agreeing.
    raw_owner_count = data.get("OwnerCount")
    owner_count = None if raw_owner_count is None else int(raw_owner_count)
    held, why = reserve_drops(base_reserve, inc_reserve, owner_count)
    spendable = drops - held
    console.say(f"{address}")
    console.say(f"    balance   {from_drops(drops):.6f} XRP ({drops} drops)")
    console.say(f"    reserve   {from_drops(held):.6f} XRP ({why})")
    # The one number an operator actually acts on, and it can be NEGATIVE: an
    # account whose reserve rose above its balance is not broken, it is an
    # account that owns more objects than it can now afford to keep funded, and
    # printing it as 0 would hide that.
    console.say(f"    spendable {from_drops(spendable):.6f} XRP ({spendable} drops)"
                f"{'  <- BELOW RESERVE, nothing can leave this account' if spendable < 0 else ''}")
    objects, escrow_why = escrows_held(address)
    if not objects:
        # Rule 14: an empty section must say it is empty. "(none)" and the reason
        # together are what keep "this account holds no escrows" apart from "the
        # escrow list could not be read", which a blank gap would merge.
        console.say(f"    escrow    (none) -- {escrow_why}")
    outgoing = 0
    for one in objects:
        amount = one.get("Amount")
        if not isinstance(amount, str):
            # An escrow of an issued token carries Amount as an object, not a
            # drop string. This repo only ever escrows XRP, so seeing one is
            # information rather than an error -- printed whole rather than
            # coerced into a number it is not.
            console.say(f"    escrow    Amount is not a drop string: {one}")
            continue
        # WHOSE ESCROW IS THIS. account_objects lists an escrow under BOTH the
        # sender and the destination -- the object is threaded into two owner
        # directories -- but only the SENDER pays its reserve. `Account` is the
        # sender, so comparing it to the address being reported is what separates
        # money this account has locked away from money waiting to arrive.
        mine = one.get("Account") == address
        outgoing += 1 if mine else 0
        side = ("OUT  locked by this account; its drops have already left the balance above, and it "
                "is holding one reserve increment") if mine else (
               f"IN   locked by {one.get('Account')}; it costs THIS account no balance and no reserve, "
               f"and the drops are not yours until it is finished")
        console.say(f"    escrow    {from_drops(int(amount)):.6f} XRP {one.get('Account')} -> "
                    f"{one.get('Destination')}")
        console.say(f"              {side}")
        # THE CANCEL VERDICT IS A DECISION AND IT LIVES IN chains/xrp_escrow.py NOW.
        # `_when('CancelAfter', ...)` reached the same answer inside a display helper in this
        # entry point -- a decision in a report builder, which is rule 10's opening complaint
        # and which is why it could not be called with a seeded object and a fixed clock. The
        # verdict also carries WHERE THE DROPS GO, which the old line did not say: EscrowCancel
        # has no destination, so they return to the escrow's own Account and not to whoever
        # cancels it. That is the fact somebody needs before deciding whether to bother.
        verdict = cancel_verdict(one, time.time())
        console.say(f"              CancelAfter  {one.get('CancelAfter')} -> {verdict.state}")
        console.say(f"                           {verdict.reason}")
        console.say(f"              {_when('FinishAfter', one.get('FinishAfter'))}")
        # WHAT AN EscrowCancel WOULD NEED, PRINTED BECAUSE NOBODY HERE KNOWS THE ANSWER.
        # The reclaim itself is not written. It needs `Owner` and `OfferSequence`, and whether an
        # account_objects entry carries the second is not established anywhere in this tree --
        # no recorded escrow response exists to check against, and guessing a protocol field is
        # the mistake xrp_htlc_escrow.py's header already records once (it claimed server-side
        # `submit` "may" work; the first real run answered notSupported). So this line asks the
        # question as code: one run against the real account settles it, and the verdict above
        # stays useful whether the answer is yes or no.
        inputs = cancel_inputs(one)
        ready = "YES, both fields present" if inputs.ready else f"NO, missing {', '.join(inputs.missing)}"
        console.say(f"              EscrowCancel buildable from this entry?  {ready}")
        console.say(f"                           Owner={inputs.owner or '(absent)'} "
                    f"OfferSequence={inputs.offer_sequence if inputs.offer_sequence is not None else '(absent)'}")
        console.say(f"                           {inputs.how_to_get_it}")
        console.say("                           Nothing was submitted. Reclaiming an escrow "
                    "spends a fee and returns the drops to its own Account, so whether to do it "
                    "is the operator's call.")
    # THE CROSS-CHECK THAT WOULD HAVE CAUGHT THE DEFECT THIS BLOCK WAS REWRITTEN
    # FOR. OwnerCount counts objects this account PAYS FOR, so the escrows it
    # sent must not exceed it. The first version of this script printed "raises
    # this account's reserve" beside an OwnerCount of 0 and neither line looked
    # at the other -- see the module header.
    if owner_count is not None and outgoing > owner_count:
        console.say(f"    MISMATCH  {outgoing} escrow(s) sent by this account but OwnerCount is "
                    f"{owner_count}. One of the two readings is wrong and the reserve above is "
                    f"computed from OwnerCount, so treat the spendable figure as unproven.")
    return console.check(address, f"{from_drops(spendable):.6f} XRP spendable", "read", True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--account", action="append", default=[],
                        help="a classic address to read. Repeatable. Default: every saved faucet account.")
    args = parser.parse_args()

    console = Console(total_steps=3)
    console.banner("XRP BALANCES -- read-only. No transaction is built and nothing is signed.")
    console.say(f"XRP endpoint={TESTNET_URL}")

    console.step(1, "which network this endpoint is on")
    try:
        console.check("network", refuse_mainnet(), "a test network", True)
    except Exception as error:  # noqa: BLE001 -- checked: refuse_mainnet raises on MAINNET and on any transport failure, and both must stop the run here. The message names which, and no account is read either way.
        console.check("network", f"{type(error).__name__}: {error}", "a test network", False)
        return console.summary()

    console.step(2, "the reserve, asked of the server rather than hardcoded")
    info = (rpc("server_info", {}).get("info") or {}).get("validated_ledger") or {}
    base_reserve, inc_reserve = info.get("reserve_base_xrp"), info.get("reserve_inc_xrp")
    if base_reserve is None:
        console.check("reserve_base_xrp", "absent", "a number from validated_ledger", False)
        console.say("REFUSED to guess it. The base reserve has been 20, 10 and 1 XRP; a compiled-in "
                    "value would report spendable balance that does not exist.")
        return console.summary()
    console.check("reserve", f"base {base_reserve} XRP, increment {inc_reserve} XRP per owned object",
                  "both read from validated_ledger", True)

    console.step(3, "each account")
    addresses = list(args.account)
    if addresses:
        console.say(f"{len(addresses)} address(es) given on the command line; the saved faucet files "
                    f"were not read")
        for address in addresses:
            if not is_valid_classic_address(address):
                console.check(address, "not a classic address", "r... base58, 25-35 chars", False)
    else:
        console.say("no --account given, so reading every saved faucet file. Addresses only; the seed "
                    "in those files is never read by this script:")
        addresses = [address for _, address, _ in saved_faucet_accounts()]
    if not addresses:
        console.say("(none) -- no --account was given and no saved faucet file held an address. "
                    "INCONCLUSIVE: nothing failed, and nothing was looked at.")
        console.summary()
        return NOTHING_TO_LOOK_AT
    console.say(f"{len(addresses)} account(s) to read")
    for address in addresses:
        report_account(console, address, base_reserve, inc_reserve)
    return console.summary()


if __name__ == "__main__":
    raise SystemExit(main())
