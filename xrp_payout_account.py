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

import contextlib
import io
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent / "swap_terminal"))

from chains import xrp_testnet
from chains.xrp_address import is_valid_classic_address
from chains.xrp_payout_seed import SIGNING_SEED_ENV_VAR, signing_seed
from config import Config
from report_block import CONTINUATION, labeled

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


def read_saved_keys() -> list[tuple[Path, str, str]]:
    """Every saved faucet keyfile, ONCE, with the helper's own chatter discarded.

    TWO DEFECTS IN ONE, BOTH MEASURED ON THE OPERATOR'S SCREEN 2026-10-03.

    chains/xrp_testnet.saved_faucet_accounts() prints a provenance line per file --
    "xrp-testnet-...json: address under 'address', secret under 'seed' (value not
    shown)" -- to STDOUT. Two callers here meant TWO sets of those lines, and
    because they print as a side effect of the READ they landed ABOVE the "saved
    keys" label that introduces them:

            xrp-testnet-20260925T233739Z.json: address under 'address', ...
            xrp-testnet-20260925T233156Z.json: address under 'address', ...
          saved keys      2 faucet keyfile(s) in ~/.config/swap_terminal/keys/
                          xrp-testnet-20260925T233739Z.json  rnjG8n16...  USABLE
                          xrp-testnet-20260925T233156Z.json  rBfM7je6...  USABLE
            xrp-testnet-20260925T233739Z.json: address under 'address', ...
            xrp-testnet-20260925T233156Z.json: address under 'address', ...

    Rule 14's "pasted output has to be self-describing a day later" -- this block
    is not, and nothing in it says why the same two files are named three times.

    SO: ONE READ, AND THE CHATTER IS DISCARDED RATHER THAN REDIRECTED. It is not
    this tool's report: the verdict lines already name the file, the address and
    what is wrong with it, which is strictly more than the key names say. The lines
    stay valuable to the helper's OTHER callers -- xrp_send_tagged.py prints them
    as its account listing -- so the function is not changed; this wrapper just
    does not forward them.

    It is also the reason seed_for_export() and saved_key_verdicts() take their
    accounts as an argument: one read, one place (rule 8), rather than two reads
    whose chatter had to be suppressed twice in two different ways.
    """
    with contextlib.redirect_stdout(io.StringIO()):
        return xrp_testnet.saved_faucet_accounts()


def saved_key_verdicts(accounts=None) -> list[tuple[str, str, str]]:
    """Every saved faucet keyfile, as (filename, address, verdict). NO SECRET IS RETURNED.

    WHY THIS IS HERE, 2026-10-03. xrp_payout_account.py found that
    XRP_PAYOUT_SECRET_SEED held nine characters that are not a seed, and the
    operator's answer was "don't know, help me resolve this". The tree already
    reads faucet keyfiles -- chains/xrp_testnet.saved_faucet_accounts(), which
    globs ~/.config/swap_terminal/keys/xrp-testnet-*.json and resolves the secret
    through SECRET_KEYS because WHERE THE FAUCET PUTS THINGS was measured rather
    than guessed. What nothing did was say whether a saved file is USABLE.

    USABLE MEANS TWO THINGS AND BOTH ARE CHECKED LOCALLY. The secret must decode,
    and it must control the address stored beside it. The second is the one that
    matters and the one no file format guarantees:
    chains/xrp_testnet.saved_faucet_accounts()'s own comment says the address and
    the secret are read from separate key names over two nesting levels, so nothing
    structurally ties them to the same faucet response. A seed paired with an
    address it does not control is exactly what
    chains/xrp_signing.derive_and_check() refuses -- at signing time, after a
    deposit.

    Offline: base58check and a key derivation, no rippled.
    """
    verdicts = []
    for path, address, secret in (read_saved_keys() if accounts is None else accounts):
        derived, refusal = derived_account(secret)
        if refusal:
            verdict = f"NOT USABLE -- the stored secret does not decode ({refusal.split('. ')[0]})"
        elif derived != address:
            verdict = (f"NOT USABLE -- the stored secret decodes but controls {derived}, NOT the address "
                       f"stored in this file. derive_and_check() would refuse this pairing at signing")
        else:
            verdict = "USABLE -- the stored secret decodes AND controls this address"
        verdicts.append((path.name, address, verdict))
    return verdicts


def seed_for_export(accounts=None) -> tuple[str, str]:
    """The newest USABLE saved secret, for command substitution only. (secret, why not).

    THE ONE PLACE IN THIS TOOL THAT RETURNS A SECRET, and main() will only write it
    to a NON-TTY stdout. The point is a seed that travels from the 0600 keyfile
    into the environment WITHOUT passing across a terminal, a scrollback buffer, or
    a pasted block:

        export XRP_PAYOUT_SECRET_SEED="$(python3 xrp_payout_account.py --print-seed-for-export)"

    Run interactively it refuses, because then stdout IS the terminal and printing
    there is the thing being avoided. That check is in main(), not here, so this
    function stays callable from a test.

    IT REUSES chains/xrp_testnet's RESOLUTION RATHER THAN RE-READING THE JSON. The
    obvious alternative -- printing a one-liner that digs the secret out with
    json.loads and a key name -- would be a second spelling of SECRET_KEYS and
    ACCOUNT_KEYS in a shell command nobody can test (rule 8), and those tables
    exist because the faucet's shape was measured.
    """
    # saved_faucet_accounts() PRINTS TO STDOUT, AND IN THIS MODE STDOUT IS THE
    # VARIABLE. Caught by tests/test_xrp_payout_account.py before it shipped: the
    # captured value came back as
    #
    #     "    xrp-testnet-1.json: address under 'address', secret under 'secret'
    #      (value not shown)\nsEdTM1uX8pu2do5XvTnutH6HsouMaM2"
    #
    # so `export VAR="$(...)"` would have set a seed with a diagnostic line glued
    # to the front of it, and the failure would have surfaced as "xrpl-py could not
    # read it as a seed" -- the exact message the operator is already stuck on, now
    # with a second cause. That line is useful to its other callers and is not
    # removed; it is sent to STDERR, where the operator still sees which file was
    # used and the shell does not capture it.
    for _path, address, secret in (read_saved_keys() if accounts is None else accounts):
        derived, refusal = derived_account(secret)
        if not refusal and derived == address:
            return secret, ""
    return "", ("no saved keyfile in ~/.config/swap_terminal/keys/ holds a secret that decodes AND "
                "controls the address stored beside it. Nothing was printed")


def print_seed_for_export() -> int:
    """stdout becomes the variable, so NOTHING else may be written to it.

    EXTRACTED FROM main() RATHER THAN SUPPRESSING PLR0911. Adding this mode took
    main() to seven returns against a ceiling of six, and CLAUDE.md rule 12 says
    what to do: extract, never raise the ceiling. It also happens to be the right
    shape -- this mode shares no output with the readable report and must not.

    THE TTY REFUSAL IS THE GUARD THAT MAKES THE WHOLE MODE SAFE. Printing a seed is
    exactly what the rest of this tool exists to avoid, so the one path that does
    it refuses when stdout is a terminal: inside `$(...)` stdout is a pipe and the
    value flows file -> environment, while run by hand it refuses and explains. The
    seed therefore never reaches a scrollback buffer, a `script` log, or a pasted
    block.

    Diagnostics go to STDERR, so a refusal is visible to the operator without ever
    becoming part of the captured value. A refusal that printed to stdout would
    silently export its own error message as the seed.
    """
    if sys.stdout.isatty():
        print("REFUSED: stdout is a terminal, and this mode exists to keep a seed OFF a terminal.",
              file=sys.stderr, flush=True)
        print(f'Use it inside command substitution:\n'
              f'    export {SIGNING_SEED_ENV_VAR}="$(python3 xrp_payout_account.py '
              f'--print-seed-for-export)"', file=sys.stderr, flush=True)
        return 2
    secret, refusal = seed_for_export()
    if refusal:
        print(f"REFUSED: {refusal}", file=sys.stderr, flush=True)
        return 1
    # No newline, no label: the whole of stdout becomes the variable, and a trailing
    # newline inside $() is stripped by the shell anyway -- but `end=""` means the
    # same bytes reach a Python caller that captures this without a shell.
    print(secret, end="", flush=True)
    return 0


def main() -> int:
    if "--print-seed-for-export" in sys.argv[1:]:
        return print_seed_for_export()

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
        # THE REMEDY, NOT JUST THE DIAGNOSIS. A refusal that names what is wrong and
        # stops is where this tool was yesterday, and the operator's reply to it was
        # "don't know, help me resolve this" -- which is the correct response to a
        # message that says a value is bad and nothing about where a good one lives.
        print(flush=True)
        accounts = read_saved_keys()
        verdicts = saved_key_verdicts(accounts)
        print(labeled("saved keys", f"{len(verdicts)} faucet keyfile(s) in "
                                    f"~/.config/swap_terminal/keys/" if verdicts else
                                    "(none) in ~/.config/swap_terminal/keys/ -- no saved faucet account "
                                    "to fall back on"), flush=True)
        for name, address, verdict in verdicts:
            print(CONTINUATION + f"{name}  {address}  {verdict}", flush=True)
        _secret, why_not = seed_for_export(accounts)
        print(flush=True)
        if why_not:
            print(f"Nothing was written and no variable was changed. {why_not[0].upper()}{why_not[1:]}.",
                  flush=True)
            print(f"A usable keyfile is what this needs; {DEPOSIT_ACCOUNT_VARIABLE} is derived from the "
                  f"seed, so there is nothing to set until the seed is real.", flush=True)
            return 1
        print("One of those is usable. This moves its seed from the 0600 keyfile into the environment "
              "WITHOUT it crossing your terminal -- the tool refuses to print it to a tty:", flush=True)
        print(f'    export {SIGNING_SEED_ENV_VAR}="$(python3 xrp_payout_account.py '
              f'--print-seed-for-export)"', flush=True)
        print("Then re-run this tool: it will derive the account and print the export line for "
              f"{DEPOSIT_ACCOUNT_VARIABLE}.", flush=True)
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
