#!/usr/bin/env python3
"""Print the commands that deploy and read the operator posture canister. Sends nothing.

Role: file (root entry point; the decisions are seed_posture() and candid_seed())
Reads: swap_terminal/config.py only -- ALLOWED_PAIRS, DEFAULT_FEE_BPS and the
      per-chain *_MIN_CONFIRMATIONS. No database, no chain, no replica, no socket.
Writes: nothing. It prints.
Can move funds: NO. It holds no key, builds no transaction, and the canister it
      describes cannot spend either -- icp/operator_admin/src/lib.rs has no method
      that produces a signature and makes no outcall.
Mainnet-safe: it reaches nothing at all. The canister ids it prints are local
      replica ids, and dfx.json gives operator_admin no `remote.id.ic`, so there is
      no mainnet counterpart to reach by accident.

=============================================================================
WHY THIS EXISTS: ONE AUTHORITY FOR THE SEED
=============================================================================

The canister's `init` takes the posture it starts holding, so the console says
something real on its first load instead of an empty page somebody has to populate
by hand. That seed has to come from somewhere, and there were three options:

  a tracked init_arg_file     which is what threshold_custody and the ledger use.
                              REJECTED here: the file would hold ALLOWED_PAIRS, the
                              fee and the thresholds -- a second copy of config.py's
                              values, in a format nothing validates, drifting from
                              the day it is written (rule 8).
  typed by hand at deploy     thirty pairs in candid syntax, retyped whenever they
                              change. Rejected for the obvious reason.
  DERIVED, printed, pasted    this file. config.py stays the one authority; the
                              canister is seeded FROM it; nothing is stored twice.

So the output is a command, not a file. Paste it and the canister holds exactly
what the terminal is configured with at that moment -- and if the two later
disagree, that is a real fact about the deployment rather than an artifact of a
stale file nobody re-generated.

=============================================================================
WHAT THE CANISTER IS FOR, AND WHAT IT IS NOT
=============================================================================

The operator asked twice to move the admin surface onto the canister. Half of that
is possible and half is not, and the line is sharp:

  THE DASHBOARD CANNOT MOVE. Every number on /admin comes out of
  swap_terminal.db or a loopback JSON-RPC call to a daemon on 127.0.0.1. A canister
  has no filesystem and an outcall cannot reach loopback, so balances, swap rows,
  payout rows, the obligation floor and the peg check stay in Flask.
  THE AUTHORITY CAN. Which directions may trade, the fee, the confirmation
  thresholds, which chains are armed -- those are configuration, and a canister
  holds them with one writer, caller gating and an audit trail by construction.

And the canister serves its own console rather than leaning on the generated
Candid UI, which renders method names and raw variants with no labels. See
icp/operator_admin/src/lib.rs for why that is HTML from the canister rather than an
asset canister with a JavaScript agent.

NOTHING IN THE TERMINAL READS IT YET. That is rule 16's line: wiring the terminal
to take its posture from here changes what gets traded, and it introduces a
question with no default -- what posture the desk holds when the replica is down.
Fail closed and a stopped container stops the desk; fail open and the authority is
a cache. The console says this at the top of every page.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path
from typing import ClassVar, Protocol

sys.path.insert(0, str(Path(__file__).resolve().parent / "swap_terminal"))

from config import Config
from microfortnights import format_duration
from report_block import labeled

SELF = "icp_operator_admin.py"

#: The canister name in icp/dfx.json. Named once here so a rename is one edit.
CANISTER = "operator_admin"

#: The assets whose *_MIN_CONFIRMATIONS this file seeds from.
#:
#: DERIVED FROM Config RATHER THAN LISTED, which is the whole point of this file:
#: a hand-written list would be a fourth place the chain vocabulary lives, and the
#: one most likely to be forgotten when a chain is added. Any attribute matching
#: `<ASSET>_MIN_CONFIRMATIONS` is a threshold, by construction.
CONFIRMATION_SUFFIX = "_MIN_CONFIRMATIONS"


class PostureSource(Protocol):
    """What a `config` has to declare to be seeded from. THE PARAMETER'S REAL TYPE.

    `type[Config]` is what these two functions promised -- not in writing, which is
    part of why it went unnoticed: the parameter was `config=Config` with no
    annotation at all, so a checker inferred the whole class from the default. And
    it is an over-promise. Measured 2026-10-09, Config is 973 lines declaring 24
    uppercase members -- the database path, the secret key, every RPC endpoint, the
    quote TTL -- and the seed reads TWO of them by name plus one derived family. A
    signature naming the whole class refuses any stand-in that is not that class,
    so the only way to type-check tests/test_operator_admin_seed.py's Stub would
    have been to make it inherit from Config -- and that is the one thing it must
    not do.

    MEASURED, not argued (2026-10-09, by running it rather than reading it):
    `class StubSubclass(Config)` with the same two chains declared on it makes
    configured_assets() return

        ['BTC', 'GRC', 'ICP', 'LTC', 'SOL', 'XRP']

    instead of ['BTC', 'GRC'], because the derivation is `dir(config)` and
    inheritance hands it the real config's six thresholds. seed_posture() then
    reports six confirmations for a two-chain stub.
    test_the_threshold_assets_are_derived_and_not_listed goes red. The stub exists
    to fail loudly when this file reaches for something new; inheritance replaces
    that with the shipped Config quietly answering, which is the same class of
    defect as a blind `except Exception` returning a plausible value.

    THE `<ASSET>_MIN_CONFIRMATIONS` MEMBERS ARE DELIBERATELY ABSENT, and that is
    the point of the file rather than an omission. They are reached through
    `dir()` and `getattr()` -- see CONFIRMATION_SUFFIX above -- and that reach IS
    the derivation. Spelling BTC/LTC/GRC/SOL/XRP/ICP here would be a second list of
    the chain vocabulary, in the one place most likely to be forgotten when a chain
    is added (rule 8), and it would be a list no checker could even hold up against
    the dynamic lookup it claims to describe.

    ClassVar ON BOTH MEMBERS IS MANDATORY, not decoration: these are read off the
    CLASS (`type[PostureSource]`, never an instance -- Config is never
    instantiated), and pyright 1.1.414 refuses a plain annotation with
    "ALLOWED_PAIRS is not defined as a ClassVar in protocol" and a @property with
    "a property defined within a protocol class cannot be accessed as a class
    variable". Both variants were run before this was written.
    """

    ALLOWED_PAIRS: ClassVar[set[tuple[str, str]]]
    DEFAULT_FEE_BPS: ClassVar[int]



def configured_assets(config: type[PostureSource] = Config) -> list[str]:
    """Every asset `config` declares a confirmation threshold for, sorted.

    SORTED so the generated command is byte-identical across runs. An unordered
    seed would make two deploys of the same configuration produce two different
    command lines, which is the kind of noise that makes a diff unreadable and a
    paste un-reviewable.
    """
    return sorted(
        name[: -len(CONFIRMATION_SUFFIX)]
        for name in dir(config)
        if name.endswith(CONFIRMATION_SUFFIX) and name.isupper()
    )


def seed_posture(config: type[PostureSource] = Config) -> dict:
    """The posture to install, read out of `config`. THE DERIVATION, as a function.

    `config` IS A PARAMETER so a test can hand in a stand-in and assert on the
    result without the real environment -- the same reason
    services/late_deposit_service.late_scan_targets() takes its cutoff rather than
    computing one.

    IT READS, IT DOES NOT DECIDE. Every value here is whatever config.py says, with
    no defaulting and no filtering: a pair the terminal cannot currently quote is
    still in ALLOWED_PAIRS and is still seeded, because the canister's job is to
    mirror the configured posture rather than to second-guess it. Validation
    belongs to the canister, which refuses on its own terms.
    """
    return {
        "pairs": sorted(tuple(pair) for pair in config.ALLOWED_PAIRS),
        "fee_bps": int(config.DEFAULT_FEE_BPS),
        "confirmations": [
            (asset, int(getattr(config, f"{asset}{CONFIRMATION_SUFFIX}")))
            for asset in configured_assets(config)
        ],
        # ARMED IS EMPTY AND IS NOT GUESSED. Whether a chain can pay out is not a
        # config value -- it is `adapter.can_spend`, which for SOL depends on a
        # keypair file existing and for GRC on a passphrase being in the
        # environment at payout time. Seeding this from anything available here
        # would be a claim about armed state, which is exactly the category rule 16
        # sends back to the operator. The console prints the command to set it.
        "armed": [],
    }


def candid_text(value) -> str:
    """One Python value as candid text. THE ENCODER.

    SMALL AND HAND-ROLLED on purpose: the alternative is a candid library in a
    read-only printer, and the three shapes needed here -- a record, a vec and a
    tuple -- are a dozen lines. The canister validates everything this produces, so
    a malformed encoding fails loudly at the dfx call rather than silently.

    STRINGS ARE QUOTED WITH ESCAPES, because an asset code should never contain a
    quote and a seed that silently produced broken candid would be worse than one
    that refused.
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    if isinstance(value, tuple):
        return "record { " + "; ".join(candid_text(item) for item in value) + " }"
    if isinstance(value, list):
        return "vec { " + "; ".join(candid_text(item) for item in value) + " }"
    if isinstance(value, dict):
        fields = "; ".join(f"{key} = {candid_text(item)}" for key, item in value.items())
        return "record { " + fields + " }"
    raise TypeError(f"no candid encoding for {type(value).__name__}: {value!r}")


def candid_seed(posture: dict) -> str:
    """The whole init argument, as the one-line candid text dfx takes.

    nat16 AND nat32 ARE ANNOTATED, and leaving them off is a real failure rather
    than a style point: candid infers an unannotated integer as `int`, and the
    canister declares `fee_bps : nat16`, so an unannotated 150 is a type error at
    the dfx call. The annotation is what makes the printed command work the first
    time.
    """
    pairs = [{"from": f, "to": t} for f, t in posture["pairs"]]
    confirmations = [(asset, f"{blocks} : nat32") for asset, blocks in posture["confirmations"]]
    return (
        "(record { "
        f"pairs = {candid_text(pairs)}; "
        f"fee_bps = {posture['fee_bps']} : nat16; "
        "confirmations = vec { "
        + "; ".join(f'record {{ "{asset}"; {blocks} }}' for asset, blocks in confirmations)
        + " }; "
        f"armed = {candid_text(posture['armed'])}"
        " })"
    )


#: The `docker compose exec` prefix every dfx command this file prints is built on.
#:
#: ONE `-f`, AND IT WAS TWO UNTIL 2026-10-10. docker-compose.yml declares
#: `include:` for docker-compose.icp.yml, so `icp-replica` is reachable through
#: the one file, and naming the second as well would hand compose the same file
#: twice -- once imported, once as a `-f` override. Nothing in this tree does
#: that any more, because what compose makes of it was not measurable where the
#: change was written (rule 17). swap_stack.py's COMPOSE_FILES carries the full
#: reasoning; this is the same reduction in a command an operator PASTES, which
#: is the half that has to be right the first time.
COMPOSE = "docker compose -f docker-compose.yml exec -T icp-replica dfx"

#: Where the generated init argument goes. dfx.json names this path.
#:
#: A FILE AND NOT A `--argument` ON THE COMMAND LINE, and the repository's own gate
#: is what settled it: tests/test_dfx_canisters_are_buildable.py::
#: test_a_canister_whose_CANDID_takes_init_ARGUMENTS_declares_them fails a canister
#: whose candid takes an init argument that dfx.json does not supply, because a
#: plain `dfx deploy` then dies with "Expected arguments but found none" -- measured
#: on the operator's host 2026-10-07 against threshold_custody. Printing a command
#: with `--argument` works only for somebody who read the printout.
#:
#: TRACKED, NOT GITIGNORED, and that is the same gate's other half. It forbids a
#: tracked init_arg_file that carries ENVIRONMENT STATE -- which it measures as a
#: 64-hex account identifier, because the same identity has a different account on
#: every fresh replica. This file holds pairs, a fee and confirmation counts: the
#: same values on every replica there will ever be, so tracking it is what lets a
#: fresh checkout deploy with nothing to generate. The ledger's file is the opposite
#: case and is correctly ignored.
#:
#: IT IS A MIRROR AND config.py IS THE AUTHORITY (rule 5). It exists because dfx
#: cannot read one: DFINITY's own ledger documentation says "`dfx.json` does not
#: support referring to values through environment variables. Values must be
#: hardcoded in plain text." So the values are written out for exactly one consumer,
#: by one named generator -- this file -- and
#: tests/test_operator_admin_seed.py asserts the mirror matches the authority, so a
#: config change without a regenerate fails the suite instead of deploying a stale
#: posture.
INIT_ARG_FILE = Path(__file__).resolve().parent / "icp" / "operator_admin_init.did"

#: The header the generated file carries, so nobody edits it by hand.
GENERATED_HEADER = """\
// GENERATED by icp_operator_admin.py --write. Do not edit.
//
// This is a MIRROR of swap_terminal/config.py's ALLOWED_PAIRS, DEFAULT_FEE_BPS and
// *_MIN_CONFIRMATIONS, written out because dfx cannot read a Python value and
// "dfx.json does not support referring to values through environment variables"
// (DFINITY's own ledger setup documentation). config.py stays the authority.
//
// Regenerate after any change to those values:
//     python3 icp_operator_admin.py --write
//
// tests/test_operator_admin_seed.py fails if this file and config.py disagree, so a
// forgotten regenerate is a red suite rather than a stale posture on a replica.
"""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog=SELF,
        description=(
            "Print the deploy and read commands for the operator posture canister. This tool "
            "prints; it reaches nothing and changes nothing."
        ),
    )
    parser.add_argument("--canister-id", default="",
                        help="the deployed canister id, if you have it. With it, the console URL "
                             "and the read commands are printed too.")
    parser.add_argument("--write", action="store_true",
                        help=f"regenerate {INIT_ARG_FILE.name}, the init argument dfx reads. This "
                             f"is the only thing this tool writes, and it writes nothing else.")
    args = parser.parse_args(argv)
    started = time.monotonic()
    posture = seed_posture()

    print(f"{SELF}: PRINTS ONLY -- nothing is deployed, called or changed by this run", flush=True)
    print("  seeded from   swap_terminal/config.py, which stays the one authority for these "
          "values", flush=True)
    print(f"  pairs         {len(posture['pairs'])}  <- Config.ALLOWED_PAIRS", flush=True)
    print(f"  fee           {posture['fee_bps']} bps  <- Config.DEFAULT_FEE_BPS", flush=True)
    print(f"  thresholds    {len(posture['confirmations'])}  <- "
          f"{', '.join(f'{a}={b}' for a, b in posture['confirmations'])}", flush=True)
    print("  armed         (none) -- NOT guessed. Whether a chain can pay out is "
          "adapter.can_spend, which depends on a keypair file or a passphrase at send time, not "
          "on config. Set it deliberately with the command below.", flush=True)

    wanted = GENERATED_HEADER + candid_seed(posture) + "\n"
    present = INIT_ARG_FILE.read_text() if INIT_ARG_FILE.exists() else ""
    if args.write:
        INIT_ARG_FILE.write_text(wanted)
        print(f"\n  WROTE      {INIT_ARG_FILE}"
              f"{'  (unchanged)' if present == wanted else '  (CHANGED)'}", flush=True)
    elif present != wanted:
        # RULE 14: "DID NOTHING" MUST NOT LOOK LIKE "DID WORK". A stale mirror is the
        # one state where this tool's printout and the thing dfx will actually read
        # disagree, so it is said here rather than left to the test suite.
        print(f"\n  STALE      {INIT_ARG_FILE.name} does NOT match config.py. dfx reads that "
              f"file, so a deploy now would install a posture that is not the configured one. "
              f"Regenerate with: python3 {SELF} --write", flush=True)
    else:
        print(f"\n  init arg   {INIT_ARG_FILE.name} matches config.py", flush=True)

    print("\n  1. BUILD AND DEPLOY. dfx reads the init argument from the file above, so there "
          "is nothing to paste:", flush=True)
    print(f"       {COMPOSE} deploy {CANISTER}", flush=True)

    print("\n  2. THE CONSOLE. It renders the posture, the audit log and the command that "
          "changes each field, in the terminal's own palette:", flush=True)
    if args.canister_id:
        print(f"       http://{args.canister_id}.localhost:4943/", flush=True)
    else:
        print("       the URL is http://<canister-id>.localhost:4943/ -- re-run with "
              "--canister-id to have it printed, or read the id with:", flush=True)
        print(f"       {COMPOSE.replace(' dfx', '')} cat /repo/.dfx/local/canister_ids.json",
              flush=True)

    print("\n  3. CHANGING IT. Writes are CONTROLLER ONLY and each takes a one-line reason that "
          "goes into the audit entry beside the old and new values:", flush=True)
    target = args.canister_id or CANISTER
    for example in (
        f"{COMPOSE} canister call {target} set_fee_bps '(150 : nat16, \"why\")'",
        f"{COMPOSE} canister call {target} add_pair "
        "'(record { from = \"BTC\"; to = \"GRC\" }, \"why\")'",
        f"{COMPOSE} canister call {target} set_confirmations '(\"BTC\", 2 : nat32, \"why\")'",
        f"{COMPOSE} canister call {target} set_armed '(\"GRC\", true, \"why\")'",
    ):
        print(f"       {example}", flush=True)

    print("\n  NOT WIRED. Nothing in swap_terminal/ reads this canister: the terminal still takes "
          "ALLOWED_PAIRS, DEFAULT_FEE_BPS and the thresholds from its own environment, so a value "
          "changed here changes what the CANISTER says and nothing else. Wiring it changes what "
          "gets traded and needs a decision about what posture the desk holds when the replica is "
          "down -- which is yours, not a follow-up commit.", flush=True)
    print(labeled("done in", format_duration(time.monotonic() - started)), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
