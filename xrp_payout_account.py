#!/usr/bin/env python3
"""The XRP account your signing seed controls, so XRP_DEPOSIT_ACCOUNT can be set.

Role: file (operator entry point at the project root per CLAUDE.md rule 10; the
      decision is derived_account() below and is callable with a seeded input)
Reads: XRP_PAYOUT_SECRET_SEED from the ENVIRONMENT, and optionally the testnet
      rippled XRP_RPC_URL names (one account_info call, read-only)
Writes: nothing. No row, no file, no environment variable -- it PRINTS the export
      line for the operator to run, because this process cannot set a variable in
      their shell and must never edit .env or any secret.
Can send orders: no. It derives a public address and submits nothing.
Live-safe: yes. The one network call is account_info, a read.

THE SEED IS NEVER PRINTED, NEVER LOGGED, AND NEVER A COMMAND-LINE ARGUMENT.
It is read from the environment by this process and used for exactly one thing:
Wallet.from_seed(), whose public half is the address. argv is world-readable
through /proc and `ps`, which is why there is no --seed flag and never will be;
chains/xrp_payout_seed.signing_seed() is the same contract one layer down.

WHY THIS EXISTS, MEASURED 2026-10-03. XRP_DEPOSIT_ACCOUNT has been the single
remaining readiness FAIL on this host all day, and it is what makes GRC->XRP,
XRP->BTC, XRP->GRC and XRP->LTC render UNAVAILABLE on the customer page. The
operator asked to have it set, and nothing in this tree could say WHAT to set it
to:

    chains/xrp_signing.derive_and_check(secret, announced)   VERIFIES a pairing
    chains/xrp_payout_seed.signing_seed_is_present()         a bool, by design
    xrp_payout_verify.py                                     needs the account
    xrp_balances.py                                          needs an address

Every one of those takes the account as an input. The question "which account
does the seed I already exported control" had no answer in the tree, so the only
paths to it were reading a faucet keyfile by hand or pasting a seed into a python
one-liner -- and the second is how a seed ends up in shell history.

ONE ACCOUNT SERVES BOTH DIRECTIONS, AND THAT IS THE DESIGN RATHER THAN AN
OVERSIGHT. services/swap_service.payout_source_account() reads the same variable
through the same table as deposit_account() -- TAG_ATTRIBUTION[asset][0] -- and
its docstring says why at length: an XRP customer's deposit lands in
XRP_DEPOSIT_ACCOUNT and an XRP payout is debited from it, exactly as the single
Gridcoin wallet takes deposits in and pays out of one balance. A second variable
for the send side is the failure that docstring names: the deposit side and the
payout side quietly pointing at two different accounts, each file looking correct
on its own. So there is one value to set here, not two.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent / "swap_terminal"))

from chains.xrp_address import is_valid_classic_address
from chains.xrp_payout_seed import SIGNING_SEED_ENV_VAR, signing_seed
from config import Config
from report_block import labeled

#: What Config holds for the deposit account, named once so the three places
#: below that mention it cannot drift (rule 8).
DEPOSIT_ACCOUNT_VARIABLE = "XRP_DEPOSIT_ACCOUNT"


def seed_shape(seed: str) -> str:
    """What SHAPE the exported value has, said without printing any of it.

    MEASURED ON THE OPERATOR'S HOST 2026-10-03: XRP_PAYOUT_SECRET_SEED was SET and
    xrpl-py raised ValueError reading it. The first version of this refusal said
    only "could not read it as a seed (ValueError)" and named the length
    requirement -- true, actionable in principle, and it left the operator to
    inspect a live key by eye to find out which way it was wrong.

    SO THIS REPORTS SHAPE FACTS AND NEVER CONTENT: a length, a prefix FAMILY, and
    whether the value contains characters a shell would have stripped if the export
    had been written differently. None of those narrows a 29-character base58 secret
    to anything an attacker could use, and each one points at a different fix.

    Probed against the installed xrpl-py the same day, so the branches below are
    measured rather than guessed:

        "sEdTM1uX8pu2do5XvTnutH6HsouMaM2 "   trailing space  -> ACCEPTED
        '"sEdTM1uX8pu2do5XvTnutH6HsouMaM2"'  literal quotes  -> ValueError
        "a" * 64                             hex-looking     -> ValueError
        "notaseed"                           garbage         -> ValueError

    The trailing-space result is why whitespace is reported but not blamed: it
    decodes fine, so a value that fails AND has whitespace failed for another
    reason.
    """
    length = len(seed)
    facts = [f"{length} characters"]
    if seed != seed.strip():
        facts.append("leading or trailing whitespace (which xrpl-py tolerates, so this is not the cause)")
    if any(ch in seed for ch in "\"'"):
        facts.append("QUOTE CHARACTERS inside the value -- an `export VAR=\"...\"` whose quotes were "
                     "themselves quoted keeps them, and xrpl-py rejects that")
    stripped = seed.strip().strip("\"'")
    if stripped and all(ch in "0123456789abcdefABCDEF" for ch in stripped):
        facts.append("all hexadecimal -- that is a master seed or entropy in hex, not the base58 secret "
                     "Wallet.from_seed() reads")
    elif stripped.startswith("sEd"):
        facts.append("starts with 'sEd' (an ed25519 seed), so the prefix is right and the rest is not")
    elif stripped.startswith("s"):
        facts.append("starts with 's' (a secp256k1 seed), so the prefix is right and the rest is not")
    elif stripped and stripped[0].isdigit():
        facts.append("starts with a digit -- xrpl's 'secret numbers' are six groups of six digits and "
                     "need Wallet.from_secret_numbers(), which the payout path does not use")
    else:
        facts.append("does not start with 's', so it is not an XRP seed at all")
    return "; ".join(facts)


def derived_account(seed: str) -> tuple[str, str]:
    """(classic address, "") for a usable seed, or ("", why not). NEVER RAISES.

    THE DECISION, at the bottom where it can be called with a seeded input
    (rule 10). Every refusal is a sentence rather than an exception because this
    tool's whole output is a line an operator reads, and a traceback from
    xrpl-py's internals says nothing about which variable to fix.

    THE RETURNED REASON NEVER CONTAINS THE SEED, which is why the invalid-seed
    branch reports the exception TYPE and a fixed sentence rather than str(error).
    xrpl-py's own messages for a malformed seed have included the offending value,
    and this function's output is printed, pasted and kept.
    """
    if not seed:
        return "", (f"{SIGNING_SEED_ENV_VAR} is not set in this process, so there is no seed to derive "
                    f"an account from. Export it in this shell first")
    try:
        # Imported here rather than at module scope, deliberately: on a host without
        # xrpl-py this tool must still print WHY it cannot derive an account, and a
        # top-level import would make it a traceback instead (rule 14).
        from xrpl.wallet import Wallet  # noqa: PLC0415
    except ImportError:
        return "", ("xrpl-py is not importable in this interpreter, so no account can be derived. "
                    "pip install xrpl-py")
    try:
        wallet = Wallet.from_seed(seed)
    except Exception as error:  # noqa: BLE001 -- checked: returns a refusal naming the TYPE only. str(error) is deliberately NOT included: xrpl-py has echoed the offending seed in its own message, and this output gets pasted.
        # THE SAME CALL THE PAYOUT MAKES, WHICH IS WHY THIS IS A LIVE FINDING AND NOT A
        # TOOL PROBLEM. chains/xrp_signing.py:397 derives the signing wallet with
        # Wallet.from_seed(secret) -- this exact function. A value it cannot read here
        # cannot be read there either, so an XRP payout would fail at SIGNING TIME,
        # after the customer's deposit was confirmed and irreversible. Finding it with
        # no XRP swap in existence costs a re-export.
        #
        # AND THIS IS WHY NO ALTERNATE CONSTRUCTOR IS TRIED. xrpl-py also offers
        # from_entropy(), from_secret() and from_secret_numbers(), and reaching for one
        # of them would let this tool print an account the payout path still cannot
        # sign for -- a second spelling of "derive the wallet" that disagrees with the
        # first (rule 8), and disagrees exactly where it costs a stranded deposit. The
        # shape line below names the constructor that WOULD read the value, as
        # information, and the fix is to export a seed from_seed accepts.
        return "", (f"{SIGNING_SEED_ENV_VAR} is set but xrpl-py could not read it as a seed "
                    f"({type(error).__name__}). The value is NOT printed. Its shape: "
                    f"{seed_shape(seed)}. A base58 seed is 29 characters. THIS IS NOT ONLY A PROBLEM FOR "
                    f"THIS TOOL -- chains/xrp_signing.py derives the payout wallet with the same "
                    f"Wallet.from_seed(), so an XRP payout would fail at signing, after a deposit was "
                    f"confirmed")
    address = str(wallet.classic_address)
    if not is_valid_classic_address(address):
        return "", (f"the derived address {address!r} does not decode as a classic address, which should be "
                    f"impossible from a seed xrpl-py accepted -- treat this as a bug rather than a "
                    f"configuration problem")
    return address, ""


def agreement_line(derived: str, configured: str) -> str:
    """What the already-set variable says versus what the seed derives.

    THE CASE THAT COSTS MONEY IS NOT "UNSET", IT IS "SET TO SOMETHING ELSE", and
    that is the whole reason this comparison is a line of its own.
    chains/xrp_signing.derive_and_check()'s docstring has the threat model: local
    signing lets US choose the account a transaction CLAIMS, so a seed paired with
    an address it does not control signs a Payment debiting an account nobody
    announced. The payout path refuses that before signing -- but it refuses at
    payout time, after a customer's deposit is confirmed and irreversible, which
    is the same ordering the funding gate was added for this morning.

    So a mismatch is reported here, where nothing has been taken, in the one place
    an operator is already looking at both values.
    """
    if not configured:
        return (f"(unset) -- {DEPOSIT_ACCOUNT_VARIABLE} is not set in this process. Every XRP swap is "
                f"refused while it is empty, and the four XRP pairs render UNAVAILABLE on the customer "
                f"page")
    if configured == derived:
        return (f"{configured}  <- AGREES with the seed. This account takes XRP deposits IN and XRP "
                f"payouts are debited OUT of it; one account, both directions, per "
                f"services/swap_service.payout_source_account()")
    return (f"{configured}  <- DISAGREES with the seed, which derives {derived}. An XRP payout would be "
                f"refused by chains/xrp_signing.derive_and_check() before signing -- but at PAYOUT time, "
                f"after the customer's deposit is confirmed and irreversible. Fix the variable, not the "
                f"check")


def main() -> int:
    print("xrp payout account -- READ-ONLY. Derives a PUBLIC address from the seed already in this "
          "process.", flush=True)
    print("  the seed itself is never printed, never logged, and is not a command-line argument "
          "(argv is world-readable through /proc).", flush=True)
    print(flush=True)

    seed_present = bool(signing_seed())
    print(labeled("seed", f"{SIGNING_SEED_ENV_VAR} is "
                          f"{'SET in this process' if seed_present else 'NOT SET in this process'}  <- "
                          f"presence only; whether it is the RIGHT seed is what the account below "
                          f"answers"), flush=True)

    address, refusal = derived_account(signing_seed())
    if refusal:
        print(labeled("account", f"NOT DERIVED  <- {refusal}"), flush=True)
        print(flush=True)
        print(f"Nothing was written and no variable was changed. This tool cannot set "
              f"{DEPOSIT_ACCOUNT_VARIABLE} in your shell.", flush=True)
        return 1

    configured = str(getattr(Config, DEPOSIT_ACCOUNT_VARIABLE, "") or "")
    print(labeled("account", f"{address}  <- derived from the seed. PUBLIC: this is what a customer is "
                             f"told to pay into"), flush=True)
    print(labeled("configured", agreement_line(address, configured)), flush=True)
    print(flush=True)
    if configured == address:
        print("Already set and agreeing. Nothing to do.", flush=True)
        return 0
    # The export line is printed rather than run: this process cannot change the
    # parent shell's environment, and editing .env is forbidden here. Printing it
    # also means the operator sees exactly what they are about to set.
    print("To set it, paste this in the shell that starts the workers -- the workers read what was "
          "exported in THEIR shell:", flush=True)
    print(f"    export {DEPOSIT_ACCOUNT_VARIABLE}={address}", flush=True)
    print(flush=True)
    print("Then restart the workers so they pick it up, and re-check:", flush=True)
    print("    python3 swap_terminal/supervisor.py stop && python3 swap_terminal/supervisor.py start", flush=True)
    print("    python3 swap_readiness.py", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
