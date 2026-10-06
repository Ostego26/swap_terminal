#!/usr/bin/env python3
"""Generate the ICP ledger canister's init argument file from account identifiers.

Role: file (entry point -- an operator runs this before `dfx deploy
      icp_ledger_canister`)
Reads: its command line, and nothing else. No socket, no replica, no database.
Writes: the init argument file it is told to write, and only that
Can move funds: NO. It writes a candid text file. It holds no key, reaches no
      network, and the ledger it configures is a canister on a local replica whose
      token is not ICP (see WHAT THIS CREATES below).
Mainnet-safe: yes in the sense that it cannot touch mainnet, and the dfx.json
      entry it is written for carries `remote.id.ic` naming the real ledger at
      ryjl3-tyaaa-aaaaa-aaaba-cai precisely so dfx refuses to CREATE a ledger
      there. Deploying this to mainnet is not a thing this file can do.

AT THE PROJECT ROOT because rule 10 puts entry points there: this is something a
person runs, so it has to be findable by looking rather than by knowing the
layout.

WHY THIS FILE EXISTS AT ALL -- it is forced by two things that are measured, not
preferences.

  1. DFINITY's own ICP ledger setup documentation, read 2026-10-06 at
     docs/defi/token-ledgers/setup/icp_ledger_setup.mdx, says verbatim:

         "`dfx.json` does not support referring to values through environment
          variables. Values must be hardcoded in plain text."

     and the values it wants hardcoded are the MINTER and DEVELOPER account
     identifiers, obtained by running `dfx ledger account-id` under two
     identities on the replica.

  2. Those identifiers are environment state. The same identity has a different
     account on every fresh replica, which is the same reason `.dfx/` is
     gitignored here: a checkout that hardcodes them claims to know something only
     a running replica can answer, and the claim goes stale silently the first
     time somebody recreates the container.

The resolution dfx itself offers is `init_arg_file`, a path in dfx.json pointing
at a candid text file. So the FILE is generated and ignored by git, dfx.json names
the path, and nothing environment-specific is tracked. This is rule 5's
authority-versus-mirror test applied to a build input: the authority is the
replica's identities, and this file is a mirror of them written for one consumer.

WHAT THIS CREATES, AND IT IS NOT ICP. The local ledger's token is symbol `LICP`,
name "Local ICP" -- DFINITY's own choice in the same document, and it is kept
rather than renamed to ICP on purpose. A local ledger is a different token on a
different network with its own genesis; calling it ICP in operator-facing output
would make a screenshot of a local swap indistinguishable from a real one. The
terminal's ASSET code is a separate question from the ledger's token symbol, and
whoever wires it has to answer it deliberately.

WHY EVERY ACCOUNT IDENTIFIER IS VALIDATED BEFORE IT IS WRITTEN. An ICP account
identifier is 64 hex characters carrying a CRC32 of its own body, so a truncated
or mistyped one is detectable here, for free, with no network -- and the cost of
not detecting it is specific rather than general: `minting_account` is where the
ledger mints FROM, and `initial_values` is the whole starting supply. A mistyped
minter is a ledger whose mint authority belongs to nobody; a mistyped holder is
the entire initial balance credited to an account no identity on the replica can
spend from. Both produce a ledger that deploys cleanly and is useless, with the
first symptom arriving at a transfer.

chains/icp_account.is_account_identifier is the one place that question is
answered (rule 8), and it checks the CRC32 rather than the length and alphabet --
which matters because any 64 hex characters pass the weaker test, and that is
exactly what a truncated copy-paste produces.

THE FEE IS AN ARGUMENT HERE AND NOWHERE ELSE. 10_000 e8s is DFINITY's own example
value and is what mainnet charges; it is a parameter of this file rather than a
constant in the codebase because the running ledger reports its own fee through
`icrc1_fee()`, and that is the only authority any adapter may read it from. A
number copied into Python would be a second authority that drifts the day a
ledger changes it, with nothing failing (rule 8).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

# Imported after the sys.path.insert above, which is the idiom every entry point
# in this repo needs (rule 10's layout gap). No `noqa: E402` on these two lines:
# ruff does not flag it here, and RUF100 said so -- a suppression for a finding
# that was never raised is a claim nobody checked (rule 19).
from swap_terminal.chains.coin_amounts import amount_to_base_units
from swap_terminal.chains.icp_account import is_account_identifier

#: DFINITY's own symbol and name for a locally deployed ICP ledger. Kept rather
#: than renamed: see WHAT THIS CREATES in the module docstring.
# Both lines carry a suppression directive, and this comment deliberately does
# not spell the directive keyword anywhere -- ruff scans comment text for it
# regardless of position or quoting, so an explanatory line that merely mentions
# it becomes a blanket directive and is then reported unused. Two attempts at
# this comment failed that way, which is worth a sentence rather than a silent
# third rewrite.
#
# What is suppressed: S105, which reads any string assigned to a name it finds
# suggestive as a hardcoded credential. Checked -- "LICP" is a token ticker and
# "Local ICP" a display name, both bound for a candid file and a terminal. This
# file holds no secret of any kind and reaches no network.
LOCAL_TOKEN_SYMBOL = "LICP"  # noqa: S105
LOCAL_TOKEN_NAME = "Local ICP"  # noqa: S105

#: ICP is 8 decimals -- e8s -- the same precision as BTC, LTC and GRC, so
#: chains/coin_amounts is the converter rather than a second one written here.
ICP_DECIMALS = 8


class LedgerInitRefused(ValueError):
    """The init arguments would produce a ledger that deploys cleanly and is wrong.

    A distinct type so a caller can tell a bad argument from a bug in this file.
    Every refusal names the value it rejected and what the ledger would have done
    with it.
    """


def check_account(account: str, role: str) -> str:
    """Return `account` if it is a valid ICP account identifier, else refuse.

    THE DECISION, and it is one line of real work with a long reason: the CRC32
    inside an account identifier is the only thing standing between a typo and a
    ledger whose entire supply is unspendable. `role` is in the message because
    "invalid account identifier" sent to an operator holding two of them answers
    nothing.
    """
    if not is_account_identifier(account):
        raise LedgerInitRefused(
            f"the {role} account identifier {account!r} is not valid. It must be 64 hex "
            f"characters whose leading 4 bytes are the CRC32 of the remaining 28 -- this "
            f"check failed, which means it is truncated, mistyped, or not an account "
            f"identifier at all (a PRINCIPAL is a different thing and will fail here). "
            f"Get it from the replica with `dfx ledger account-id` under the right identity. "
            f"Refused rather than written: the ledger would deploy cleanly and the first "
            f"symptom would arrive at a transfer."
        )
    return account


def init_argument(
    *,
    minter: str,
    balances: dict[str, int],
    transfer_fee_e8s: int,
    token_symbol: str = LOCAL_TOKEN_SYMBOL,
    token_name: str = LOCAL_TOKEN_NAME,
) -> str:
    """The candid text for `(variant { Init = record { ... } })`.

    KEYWORD-ONLY, because five of these are strings and integers that would
    transpose silently -- the minter and a holder are both 64-hex, and swapping
    them gives a ledger whose mint authority is the desk and whose supply is held
    by the minter. That is the transposition hazard PLR0913 exists to flag, and
    keyword-only is the answer to it rather than a noqa.

    REFUSES A MINTER THAT ALSO HOLDS AN INITIAL BALANCE. This is a judgment rather
    than a measured requirement of the ledger: transfers TO the minting account
    are burns and transfers FROM it are mints, so an account that is both the mint
    source and a funded holder makes every later balance reading ambiguous about
    which it was. Nothing in the ledger forbids it; this refuses it so that no
    measurement taken against this ledger has to be qualified.
    """
    check_account(minter, "minter")
    if not balances:
        raise LedgerInitRefused(
            "no initial balances were given, so the ledger would start with a total supply of "
            "zero and no account able to pay a fee. Refused rather than written: a ledger "
            "nothing can transfer on looks identical to a broken adapter."
        )
    for account in balances:
        check_account(account, "initial balance holder")
    if minter in balances:
        raise LedgerInitRefused(
            f"the minter account {minter} also appears in initial_values. Refused: transfers "
            f"TO the minting account are burns and transfers FROM it are mints, so an account "
            f"that is both mint source and funded holder makes every balance reading taken "
            f"against this ledger ambiguous about which happened. Use a separate identity for "
            f"the minter (`dfx identity new minter`), which is what DFINITY's own setup does."
        )
    if transfer_fee_e8s < 0:
        raise LedgerInitRefused(f"a transfer fee cannot be negative and this is {transfer_fee_e8s}")
    for account, e8s in balances.items():
        if e8s <= 0:
            raise LedgerInitRefused(
                f"initial balance for {account} is {e8s} e8s. Refused: a zero or negative "
                f"opening balance is not a funded account, and writing it would make the "
                f"deploy look like it funded something."
            )

    entries = "\n".join(
        f"        record {{\n"
        f'          "{account}";\n'
        f"          record {{ e8s = {e8s} : nat64 }};\n"
        f"        }};"
        for account, e8s in balances.items()
    )
    return (
        "(variant {\n"
        "  Init = record {\n"
        f'    minting_account = "{minter}";\n'
        "    initial_values = vec {\n"
        f"{entries}\n"
        "    };\n"
        "    send_whitelist = vec {};\n"
        f"    transfer_fee = opt record {{ e8s = {transfer_fee_e8s} : nat64 }};\n"
        f'    token_symbol = opt "{token_symbol}";\n'
        f'    token_name = opt "{token_name}";\n'
        "  }\n"
        "})\n"
    )


def main() -> int:
    """Announce the target and the scale BEFORE writing, then say what was written.

    Rule 14: an operator reading this on a terminal has to be able to tell a
    refusal from a success from a no-op without opening the file, and the
    parameters that decide the answer are echoed because pasted output is read a
    day later.
    """
    parser = argparse.ArgumentParser(
        description="Write the ICP ledger canister's init argument file for a local replica."
    )
    parser.add_argument("--minter", required=True, help="64-hex account identifier of the minter identity (`dfx identity use minter; dfx ledger account-id`)")
    parser.add_argument("--fund", required=True, action="append", metavar="ACCOUNT:ICP", help="an account identifier and an opening balance in ICP, e.g. a026...2042:100. Repeatable.")
    parser.add_argument("--transfer-fee-e8s", type=int, default=10_000, help="default 10000, which is DFINITY's own example value and what mainnet charges. An adapter must still read icrc1_fee() rather than this.")
    parser.add_argument("--out", default="icp/icp_ledger_init.did", help="where to write it. Must match dfx.json's init_arg_file for this canister.")
    args = parser.parse_args()

    balances: dict[str, int] = {}
    for pair in args.fund:
        account, separator, amount = pair.rpartition(":")
        if not separator:
            print(f"--fund {pair!r} is not ACCOUNT:ICP -- no colon found. Nothing written.", file=sys.stderr)
            return 2
        try:
            e8s = amount_to_base_units(float(amount), ICP_DECIMALS)
        except ValueError as error:
            print(f"--fund {pair!r}: {amount!r} is not a number of ICP ({error}). Nothing written.", file=sys.stderr)
            return 2
        if account in balances:
            print(f"--fund names {account} twice. Refused rather than merged: which balance was meant is not inferable. Nothing written.", file=sys.stderr)
            return 2
        balances[account] = e8s

    out = Path(args.out)
    print(f"writing ICP ledger init arguments -> {out}")
    print(f"  token            {LOCAL_TOKEN_SYMBOL} ({LOCAL_TOKEN_NAME})  <- NOT ICP; a local ledger is its own token")
    print(f"  minter           {args.minter}")
    print(f"  transfer fee     {args.transfer_fee_e8s} e8s  <- the ledger reports its own via icrc1_fee(); this only seeds it")
    print(f"  funded accounts  {len(balances)}")
    for account, e8s in balances.items():
        print(f"    {account}  {e8s} e8s ({e8s / 10**ICP_DECIMALS:.8f})")

    try:
        text = init_argument(minter=args.minter, balances=balances, transfer_fee_e8s=args.transfer_fee_e8s)
    except LedgerInitRefused as error:
        print(f"REFUSED, nothing written: {error}", file=sys.stderr)
        return 1

    existed = out.is_file()
    previous = out.read_text() if existed else None
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text)
    if previous == text:
        print(f"wrote {out} ({len(text)} bytes) -- IDENTICAL to what was already there, so nothing changed")
    elif existed:
        print(f"wrote {out} ({len(text)} bytes) -- REPLACED a different file; the ledger's init args have CHANGED, which only takes effect on a fresh canister, not an upgrade")
    else:
        print(f"wrote {out} ({len(text)} bytes) -- new file")
    print("next: dfx deploy icp_ledger_canister   (dfx reads this path via init_arg_file)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
