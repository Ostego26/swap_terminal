"""The ICP chain adapter: balances and deposits on an ICRC-1 ledger, through dfx.

Role: module (a stage; the decisions it needs live in chains/icp_account.py and
      services/icp_subaccount_service.py, which it calls)
Reads: the ICRC-1 ledger canister named in Config.RPC["ICP"], by running dfx
Writes: nothing in this repository. The ledger's legacy `transfer` writes to a ledger.
Can send orders: it CAN move funds -- send_to_address calls the ledger's legacy
      `transfer`. THESE TWO LINES SAID `icrc1_transfer` UNTIL 2026-10-07 and that
      was wrong at both sites: send_to_address() has called legacy `transfer` since
      2026-10-06, and its own docstring explains at length why it has to -- an
      icrc1_transfer takes an ICRC-1 Account and a customer supplies a 64-hex
      account identifier, which is a non-invertible SHA224 over one. A header
      naming the wrong ledger method is the wrong-comment-is-a-bug case (rule 16):
      a reader checking whether this file can mint would have gone looking at the
      ICRC-1 endpoint, which this file never calls. See
      THE PAYOUT PATH IS EXERCISED, 2026-10-06, against the real released ICP ledger on
the local replica. This section used to say send_to_address "has not been run
against any ledger, local or otherwise" and that the tests asserting on its
constructed argument were "not the same as a transfer having happened". It has now
happened:

    idempotency key   1791291996571772365      (real time, captured once)
    before            desk 1000.0      subaccount 1 0.0
    send 0.25 ICP     block index 1
    after             desk 999.7499    subaccount 1 0.25
    SAME key again    TxDuplicate
    after             desk 999.7499    subaccount 1 0.25      unchanged

The desk moved by 0.2501 for a 0.25 transfer, which is the amount plus the
0.0001 fee icrc1_fee() reported -- so the fee this adapter reads and the fee the
ledger charged are the same number, measured rather than assumed. And the retry
with the same key did not move funds: dedup behaves as the interface documents.

IT TOOK THREE REFUSALS TO GET THERE and each one corrected something real, which is
the argument for a local ledger existing at all. A missing `blob` keyword (candid
read 32 bytes as UTF-8 text); a created_at_time a year in the past (TxTooOld, 24h
window, which refuted this file's own advice about not using a clock); and then
raising on TxDuplicate, which the run above is what exposed. Every one of them would
otherwise have been found by a payout worker.

MEASURED THROUGH THIS CLASS, 2026-10-06, which is a different claim from "the
ledger answered". build_adapters(Config.RPC)["ICP"] with the two variables set,
against the deployed ledger on the local replica:

    ledger        bkyz2-fmaaa-aaaaa-qaaaq-cai
    own_address() a0263999...6c12042     identical to `dfx ledger account-id`
    get_balance() 1000.0 ICP             from (100_000_000_000 : nat)
    chain_fee()   0.0001 ICP             from icrc1_fee(), (10_000 : nat)
    deposit_address(1)                   eecc42b3...25eb4d, != own_address()

So the whole path is exercised: the dfx subprocess transport, the underscore-
separated candid nat regex, the e8s division, and the derivation agreeing with the
IC's own tooling. Everything before this run went through a seeded transport, which
proves the parsing against text I wrote rather than text dfx emits.

WHAT ICP DOES NOT HAVE, and where each one is handled:

  confirmations   a transfer is final when the ledger returns a block index. There
                  is nothing to wait for. deposit_confirmations() returns a fixed
                  value and says in its docstring that it is a COMPATIBILITY value
                  for the existing watcher rather than a chain fact.
  UTXOs           balances, not outputs, so none of chains/base.py's vout family
                  applies. This adapter deliberately does not subclass RPCAdapter.
  a mempool       there is no unconfirmed state to read. A balance either includes
                  a payment or the payment has not happened.
"""

from __future__ import annotations

import json
import logging
import re
import shlex
import subprocess
from pathlib import Path

from .coin_amounts import amount_to_base_units
from .icp_account import (
    ICP_DECIMALS,
    PrincipalRefused,
    account_identifier,
    is_account_identifier,
    principal_to_bytes,
    subaccount_from_index,
)

logger = logging.getLogger(__name__)

#: HOW MANY BLOCKS ONE query_blocks ASKS FOR. The ledger caps a reply and will send
#: fewer, which find_deposits_to_address() handles by advancing from what came back
#: rather than from what it asked for -- so this is a request size and not an
#: assumption about the reply.
#:
#: 1000 rather than one call for the whole chain, because a single enormous `length`
#: is the request a ledger is most likely to refuse or truncate, and a truncation
#: that went uncounted is the defect this whole function was rewritten for.
PAGE_BLOCKS = 1000

#: THE CEILING ON BLOCKS ONE SCAN WILL READ, AND EXCEEDING IT RAISES.
#:
#: It replaces `tx_limit: int = 500`, which was a trailing WINDOW: the scan read the
#: last 500 blocks and returned [] for a deposit below them, with no error and no log
#: line. The number is kept generous rather than tuned because its job changed -- it is
#: no longer deciding what gets scanned, it is deciding when to REFUSE instead of
#: silently covering part of the chain. A local replica will not reach it for a very
#: long time; the ICP mainnet ledger is already far past it, and that is the correct
#: answer there: scanning it block by block is not a thing this adapter can do, and
#: saying so beats missing deposits.
MAX_BLOCKS_SCANNED = 100_000

#: What `dfx canister call` prints for a nat: digits with underscore separators and
#: a candid type suffix, e.g. `(100_000_000_000 : nat)`. Matched rather than
#: eval'd -- this is somebody else's output format and a regex that fails to match
#: raises below instead of producing a number.
_NAT_RESULT = re.compile(r"\(\s*([\d_]+)\s*:\s*nat(?:64)?\s*\)")

#: `(variant { Ok = 3 : nat64 })` from transfer. The block index is the closest
#: thing ICP has to a txid.
_TRANSFER_OK = re.compile(r"Ok\s*=\s*([\d_]+)")

#: `Err = variant { TxDuplicate = record { duplicate_of = 1 : nat64 } }`. The ledger
#: saying "this exact transfer already happened, here is its block index" -- which is
#: SUCCESS for a retry, and the reason send_to_address returns it rather than raising.
_TRANSFER_DUPLICATE = re.compile(r"TxDuplicate\s*=\s*record\s*\{\s*duplicate_of\s*=\s*([\d_]+)")

#: `(blob "\0a\1b...")` from account_identifier. dfx prints a candid blob as a
#: quoted string of \xx escapes, with printable ASCII bytes left as themselves --
#: which is why _blob_to_hex below cannot simply strip backslashes.
_BLOB_RESULT = re.compile(r'blob\s*"((?:[^"\\]|\\.)*)"')


def _hex_to_blob(hex_text: str) -> str:
    r"""A 64-hex account identifier as a candid blob literal for dfx.

    THE `blob` KEYWORD IS PART OF THE VALUE AND LEAVING IT OFF IS WHY THE FIRST
    TRANSFER FAILED. A bare quoted string in candid is `text`, so dfx tried to read
    32 arbitrary bytes as UTF-8 and refused, on the live replica, 2026-10-06:

        to = "\ee\cc\42\b3..."
             ^^^^^^^^^^^^^^^^^^^^ Not valid unicode text
        Error: Failed to create argument blob.

    The asymmetry was visible in this file the whole time: _BLOB_RESULT already
    matched `blob "..."` coming BACK from account_identifier, so the reader knew the
    form the writer omitted.

    EVERY byte is escaped as \xx, including the printable ones. dfx accepts a
    mixed form, but emitting one would mean this function's output changes shape
    with the VALUE -- and a 32-byte identifier containing an accidental `"` or `\`
    would then need escaping rules this does not have. Uniform escapes have no such
    case.
    """
    return 'blob "' + "".join(f"\\{b:02x}" for b in bytes.fromhex(hex_text)) + '"'


def _blob_to_hex(escaped: str) -> str:
    r"""The hex of a candid blob literal dfx printed, undoing its mixed escaping.

    dfx leaves printable ASCII as literal characters and escapes the rest as \xx,
    so this walks the string rather than stripping backslashes -- a blob whose bytes
    happen to spell letters would otherwise lose them.
    """
    out = bytearray()
    index = 0
    while index < len(escaped):
        char = escaped[index]
        # index + 3 <= len, so the two hex characters after the backslash both
        # exist. A trailing lone backslash falls through to the literal branch rather
        # than reading past the end.
        if char == "\\" and index + 3 <= len(escaped):
            pair = escaped[index + 1 : index + 3]
            try:
                out.append(int(pair, 16))
            except ValueError:
                out.extend(pair[:1].encode())
                index += 2
                continue
            index += 3
            continue
        out.extend(char.encode())
        index += 1
    return out.hex()


class ICPCallFailed(RuntimeError):
    """A ledger call did not produce an answer this adapter will act on.

    NEVER raised in a way a caller could mistake for a zero balance, which is the
    specific hazard rule 12's BLE001 note names: "this account holds nothing" and
    "the question could not be asked" must not be the same value, because the first
    means refuse the payout and the second means try again.
    """


#: The repository root, resolved from THIS FILE rather than from the working directory.
#:
#: WHY THIS IS NOT `Path.cwd()` AND NOT A BARE RELATIVE PATH. Measured 2026-10-06,
#: before any ICP swap had been attempted through the UI: dfx_transport() built
#:
#:     ["docker", "compose", "-f", "docker-compose.yml", "-f", "docker-compose.icp.yml", ...]
#:
#: (that command was two -f flags then; it is one now -- see _COMPOSE_FILES below)
#:
#: and passed no `cwd=`, so both paths resolved against whatever directory the
#: CALLING process happened to be in. Every caller that matters is in the wrong one:
#:
#:     docker-compose.yml       from repo root: PRESENT | from swap_terminal/: ABSENT
#:     docker-compose.icp.yml   from repo root: PRESENT | from swap_terminal/: ABSENT
#:
#: and swap_terminal/ is exactly where the app runs, because app.py imports
#: `from routes.ui import bp` -- the package directory has to be the working
#: directory for that to resolve. So every ICP call from the web app or a worker
#: would have failed with compose's "no configuration file provided", which names
#: the missing file and not the reason, on a path that had been measured working
#: from a shell at the repo root all day.
#:
#: That is the gap between "tested" and "tested the way it runs": every measurement
#: behind the ICP adapter was taken from an operator shell at the root, which is the
#: one working directory the app never has.
#:
#: parents[2] because this file is <root>/swap_terminal/chains/icp.py -- chains,
#: swap_terminal, root. Asserted at import rather than trusted: a file moved one
#: level without updating this constant would otherwise point at swap_terminal/ and
#: fail at the first ICP call instead of at startup.
_REPO_ROOT = Path(__file__).resolve().parents[2]

#: The compose files the `docker compose exec` transport passes as `-f`.
#:
#: ONE FILE SINCE 2026-10-10, AND IT WAS TWO: docker-compose.yml AND
#: docker-compose.icp.yml. docker-compose.yml now declares `include:` for the icp and
#: web files, so `icp-replica` reaches this command through the one file, and passing
#: the second as well would be the same file arriving twice -- once imported, once as
#: a `-f` override. Whether compose permits that, errors on it, or silently picks one
#: is a question about compose's own merge rules that could not be measured where this
#: change was written (running docker was forbidden there, `docker compose config`
#: included), so no command in this tree passes a file `include:` already supplies.
#: Rule 17: not guessing is the whole of it. swap_stack.py's COMPOSE_FILES records the
#: same reduction and the same reason.
#:
#: IT STAYS A TUPLE, and dfx_transport() loops over it rather than indexing, because
#: indexing `[0]` and `[1]` is what made a two-element tuple a shape assumption --
#: a third entry or a second removal would have had to be made in two places.
_COMPOSE_FILES = (_REPO_ROOT / "docker-compose.yml",)


#: Lines of dfx output that carry KEY MATERIAL and must never be surfaced.
#:
#: WHY THIS IS NOT OPTIONAL AND NOT A STYLE CHOICE. Measured twice on 2026-10-07, the
#: second time WITH `--identity anonymous` already in the command:
#:
#:     Creating the "default" identity.
#:       - generating new key at /home/swap/.config/dfx/identity/default/identity.pem
#:     Your seed phrase: <24 words>
#:
#: dfx bootstraps its identity store on first run in a fresh container and prints the
#: mnemonic, BEFORE it honors --identity. So the flag was necessary -- anonymous is
#: the right caller for every read here -- and it is not sufficient: it cannot stop
#: dfx from writing that line.
#:
#: What IS this repository's to control is whether the line is passed on, and it was
#: not: ICPCallFailed embedded dfx's stderr and stdout verbatim, so the mnemonic went
#: into an exception message, a worker log, and the operator's terminal. The
#: operator's standing instruction is that a key is never moved, copied, read back or
#: echoed. A secret that arrives from a subprocess is still a secret.
#:
#: Matched on the LINE, and the whole line is dropped rather than the words masked --
#: a partial mnemonic is still a reduced search space, and nothing downstream needs
#: any part of it.
_SECRET_MARKERS = ("seed phrase", "identity.pem", "private key", "secret key")


def redact_secrets(text: str) -> str:
    """Drop every line of dfx output that carries key material. See _SECRET_MARKERS.

    Returns the text with each offending line replaced by a marker that says one was
    removed -- rule 14's "(none) is a result": a silently shortened error message
    would leave a reader wondering what they are not being told, and the fact that
    dfx emitted a key IS diagnostic information worth keeping.
    """
    kept = []
    removed = 0
    for line in text.splitlines():
        if any(marker in line.lower() for marker in _SECRET_MARKERS):
            removed += 1
            continue
        kept.append(line)
    if removed:
        kept.append(
            f"[{removed} line(s) of dfx output withheld: they carried key material. dfx bootstraps "
            f"an identity on first run in a fresh container and prints its mnemonic; this adapter "
            f"never passes that on. The identity is unused -- every call here is --identity anonymous.]"
        )
    return "\n".join(kept)


def transfer_argument(
    *, to_account: str, e8s: int, fee_e8s: int, created_at_time_nanos: int, memo: int = 0
) -> str:
    """The candid argument for the ledger's legacy `transfer`. THE MONEY-MOVING STRING.

    KEYWORD-ONLY, because every parameter is a number or a hex string and three of
    them are interchangeable at a call site: `e8s`, `fee_e8s` and
    `created_at_time_nanos` are all ints, and transposing the first two would send
    the fee as the amount. That is the transposition hazard ruff's PLR0913 exists to
    flag, answered by naming rather than by a suppression (rule 19).

    EXTRACTED 2026-10-07, when fund_desk.py needed to build the same record to MINT.
    A mint is this identical call -- the legacy `transfer`, from the ledger's
    minting account -- so a second f-string in the top-up tool would have been two
    copies of the one string in this repository that decides where money goes
    (rule 8). The copies agree on the day they are written; the field this one would
    have drifted on is `fee`, which a mint must send as 0 and a payout must send as
    `icrc1_fee()`.

    THE FEE IS A PARAMETER AND HAS NO DEFAULT, for that exact reason. Defaulting it
    either way makes one of the two callers silently wrong: 0 on a desk payout is
    `BadFee` (recoverable, loud), and `icrc1_fee()` on a mint is also `BadFee` --
    the minting account is charged nothing, so naming a fee from it is a mismatch.
    Neither default is safe, so there is none.

    `blob` AND UNIFORM ESCAPING COME FROM _hex_to_blob, which carries the record of
    the first failed transfer: omitting the keyword made candid read 32 bytes as
    `text` and answer "Not valid unicode text / Failed to create argument blob".

    created_at_time IS REQUIRED HERE TOO, as an int rather than an optional. The
    ledger deduplicates on (from, to, amount, fee, memo, created_at_time), so a
    caller that has no key to pass must be made to decide rather than handed a null
    that turns every retry into a second transaction -- see send_to_address()'s
    refusal, which is where that decision is enforced for the payout path.
    """
    return (
        f"(record {{ memo = {memo} : nat64; "
        f"amount = record {{ e8s = {e8s} : nat64 }}; "
        f"fee = record {{ e8s = {fee_e8s} : nat64 }}; "
        "from_subaccount = null; "
        f"to = {_hex_to_blob(to_account)}; "
        f"created_at_time = opt record {{ timestamp_nanos = {created_at_time_nanos} : nat64 }} }})"
    )


def transfer_block_index(reply: str) -> str:
    """The block index a `transfer` reply reports, or "" if it reports none.

    TxDuplicate IS SUCCESS, AND RAISING ON IT WAS A DEFECT THIS FILE SHIPPED. The
    ledger is saying "this exact transfer already happened; its block index is <n>".
    That is precisely what the idempotency key exists to produce, so a retry after a
    timeout must get the SAME answer the first call would have given -- not an
    exception. Measured on the local replica 2026-10-06: the same call twice left
    desk 999.7499 and subaccount 1 at 0.25 both times, the second answering
    TxDuplicate, so the funds genuinely did not move twice.

    The version before that returned the block index on Ok and RAISED on duplicate,
    with a message telling the caller to read it as success. That put the decision in
    prose a payout worker would have had to parse, and the failure mode is specific:
    a worker retrying after a timeout sees an exception and marks a payout that
    SUCCEEDED as failed (rule 10 -- the decision belongs in a function, not in an
    error string).

    EXTRACTED FROM send_to_address() 2026-10-07 alongside transfer_argument(), and
    for the same reason: fund_desk.py reads the reply of the same call, and "which
    replies mean the money moved" is not a question two files may answer separately.
    The underscores the ledger prints in large numbers are stripped here, in one
    place, rather than at each caller.

    "" RATHER THAN None, so a caller can write `if index:` and so the one thing this
    function must never do -- turn an unrecognized reply into a plausible block
    index -- is impossible by type.
    """
    for pattern in (_TRANSFER_OK, _TRANSFER_DUPLICATE):
        found = pattern.search(reply)
        if found:
            return found.group(1).replace("_", "")
    return ""


def dfx_transport(service: str, timeout: float, network_url: str = "", identity: str = ""):
    """Return a `call(canister, method, argument) -> str` that reaches the ledger.

    `identity` NAMES THE dfx IDENTITY TO SIGN AS, and it was added 2026-10-07 for
    the one caller that needs an identity neither default can give it: a top-up of
    the desk's own balance is a transfer FROM the ledger's minting account, which
    only the `minter` identity can sign (icp_ledger_init.py writes that account
    into `minting_account`, and transfers from it are mints).

    EMPTY KEEPS EXACTLY THE BEHAVIOR BOTH TRANSPORTS ALREADY HAD, which is why
    this is a parameter on the existing launcher rather than a second one. Under
    the URL transport, empty still means `--identity anonymous` -- the measured
    correct identity there, and the thing that stops dfx GENERATING a key and
    echoing its mnemonic (see the long note in `call` below). Under the compose
    transport, empty still means NO --identity flag at all, so dfx uses the
    replica container's own default, which is the identity a * -> ICP payout must
    sign with; tests/test_icp_adapter.py asserts both of those by name.

    A SECOND LAUNCHER WAS THE ALTERNATIVE AND IS THE THING THIS REPOSITORY KEEPS
    PAYING FOR (rule 8). icp_custody_addresses.py:99-102 already states the rule
    for this exact function -- it routes through here "rather than a second
    implementation of 'run dfx in compose'" -- and a top-up tool building its own
    argv would have had to re-derive the absolute compose paths, the explicit cwd,
    the `-T`, and redact_secrets() on both streams, each of which exists because
    of a separate measured failure.

    TWO TRANSPORTS, CHOSEN BY `network_url`, because the one that works depends on
    where this process is running -- and that is the whole reason this parameter
    exists:

      network_url set    `dfx canister call --network <url>` run IN THIS PROCESS's
                         container or host. No docker needed, so a process with no
                         docker CLI and no daemon socket can reach the replica over
                         the compose network (`http://icp-replica:4943`).
      network_url empty  `docker compose exec -T <service> dfx ...`, the original.
                         Works from the host, where docker is on PATH.

    WHY THE SECOND IS NOT ENOUGH, measured 2026-10-06/07. docker/web.Dockerfile's
    runtime stage installs only dumb-init: no docker CLI, and no compose file mounts
    /var/run/docker.sock. So the containerized deployment -- which is the one that
    serves the UI under gunicorn with nothing in a terminal, and the one the operator
    asked for three times -- could not make an ICP call at all. Mounting the daemon
    socket would fix it and must not be done: it hands a process holding wallet RPC
    credentials control of the whole Docker daemon.

    WHAT THIS DOES NOT FIX, and it is the half that is not mine to decide: dfx in the
    web image can READ the ledger with no identity (every method this adapter calls
    for deposit detection -- query_blocks, icrc1_balance_of, icrc1_fee,
    account_identifier -- is public), but `transfer` DEBITS THE CALLER, so an ICP
    PAYOUT needs the desk's dfx identity, which currently exists only inside the
    replica container. Placing that identity is key material and the operator's
    (rule 16). Until then: ICP -> * works in either deployment, * -> ICP works only
    where the identity is.

    THAT READ/SEND SPLIT IS REASONED, NOT MEASURED, and the difference matters
    (rule 17): it follows from the ledger's candid -- the query methods take no
    caller and `transfer` has a `from_subaccount` resolved against the caller -- and
    it has NOT been run against a replica with an anonymous identity from this
    session, which has no replica. `verify_derivation()` is the cheapest thing that
    would settle it: it calls only read methods.

    `service` is a COMPOSE SERVICE name (`icp-replica`), not a container name
    (`swap-icp-replica`). They differ in this project and `docker compose exec`
    takes the former -- passing the container name gives "no such service". The
    first draft of this function carried a leftover `container.removeprefix(...) if
    False else "icp-replica"`, which hardcoded the right answer while pretending to
    use the argument; that is worse than either, because the parameter looked
    honored.

    Built as a closure rather than a method so the adapter holds a plain callable
    and a test can pass any other one. argv is a fixed list with no shell, and the
    argument is passed as one element -- a candid record contains braces and quotes
    and would be mangled by a shell.
    """

    def call(canister: str, method: str, argument: str, output: str = "idl") -> str:
        # ABSOLUTE compose paths AND an explicit cwd, which are two fixes for one
        # measurement (see _REPO_ROOT). The absolute -f paths are what make the call
        # work from any working directory; the cwd is what makes compose resolve the
        # RELATIVE paths INSIDE those files -- `build: context: .`, the `./icp:/repo`
        # mount -- against the root as well. Fixing only the -f paths would move the
        # failure from "no configuration file provided" to a wrong build context,
        # which is the harder one to read.
        dfx_argv = ["dfx", "canister", "call", "--output", output]
        if network_url:
            # --identity anonymous, AND LEAVING IT OFF PRINTED A SEED PHRASE TO THE
            # OPERATOR'S TERMINAL. Measured 2026-10-07, the first real call through
            # this transport from inside the web container:
            #
            #     Creating the "default" identity.
            #     - generating new key at /home/swap/.config/dfx/identity/default/identity.pem
            #     Your seed phrase: <24 words, printed in full>
            #
            # dfx has no identity in a fresh container, so it CREATED one and echoed
            # its mnemonic on stdout. Two separate defects in one line:
            #
            #   1. KEY MATERIAL WAS GENERATED AND DISPLAYED by a read-only balance
            #      check. The operator's standing instruction is that a key is never
            #      moved, read back or echoed, and this repository printed a brand new
            #      one. It held nothing and the container is discarded by
            #      `swap_stack.py down`, which is luck about where it happened rather
            #      than a property of the code.
            #   2. Every call would write to the filesystem on first use, so a
            #      read-only path was not read-only.
            #
            # Anonymous is not a workaround, it is the CORRECT identity for this
            # transport, and that is measured rather than assumed: the operator ran
            # `dfx --identity anonymous ... icrc1_fee` against their replica and got
            # (10_000 : nat). Every method reached from here -- query_blocks,
            # icrc1_balance_of, icrc1_fee, account_identifier -- is public and takes
            # no caller.
            #
            # A TRANSFER UNDER THIS TRANSPORT WILL NOW FAIL LOUDLY, debiting an empty
            # anonymous account, and that is the right failure: a * -> ICP payout
            # needs the DESK's identity, which exists only in the replica container.
            # Silently creating a key and signing with it is the alternative, and it
            # is what just happened.
            dfx_argv += ["--identity", identity or "anonymous", "--network", network_url]
        elif identity:
            # THE COMPOSE TRANSPORT TAKES AN IDENTITY ONLY WHEN ONE IS ASKED FOR,
            # and the asymmetry with the branch above is deliberate rather than an
            # oversight. Inside the replica container dfx HAS identities -- the
            # desk's own is the default there, and `minter` exists beside it
            # (docker-compose.icp.yml keeps them on the icp-state volume) -- so
            # omitting the flag selects the right one for a payout. Defaulting to
            # anonymous here would make every ICP payout debit an empty account,
            # which is what tests/test_icp_adapter.py pins by name.
            dfx_argv += ["--identity", identity]
        dfx_argv += [canister, method, argument]

        if network_url:
            # IN-PROCESS dfx. No docker, no compose, no cwd requirement -- dfx resolves
            # nothing relative here, it just opens an HTTP connection to the url.
            argv = dfx_argv
            run_cwd = None
        else:
            # ABSOLUTE compose paths AND an explicit cwd, which are two fixes for one
            # measurement (see _REPO_ROOT). The absolute -f paths are what make the call
            # work from any working directory; the cwd is what makes compose resolve the
            # RELATIVE paths INSIDE those files -- `build: context: .`, the `./icp:/repo`
            # mount -- against the root as well. Fixing only the -f paths would move the
            # failure from "no configuration file provided" to a wrong build context,
            # which is the harder one to read.
            compose_flags: list[str] = []
            for path in _COMPOSE_FILES:
                compose_flags += ["-f", str(path)]
            argv = [
                "docker", "compose",
                *compose_flags,
                "exec", "-T", service,
                *dfx_argv,
            ]
            run_cwd = _REPO_ROOT
        try:
            done = subprocess.run(  # noqa: S603 -- checked: no shell, argv is a fixed list, and the only caller-supplied elements are a canister id, a method name and a candid argument this repository builds
                argv, capture_output=True, text=True, timeout=timeout, check=False,
                cwd=run_cwd,
            )
        except subprocess.TimeoutExpired as error:
            raise ICPCallFailed(
                f"dfx did not answer within {timeout}s for {method} on {canister}. The question "
                f"was NOT answered -- this is not an empty result."
            ) from error
        if done.returncode != 0:
            raise ICPCallFailed(
                f"dfx exited {done.returncode} for {method} on {canister}: "
                f"{redact_secrets(done.stderr.strip()) or '(no stderr)'}. "
                f"Command was: {shlex.join(argv)}"
            )
        # REDACTED ON THE WAY OUT TOO, not only in the error path. dfx's identity
        # bootstrap goes to whichever stream it chooses, and this value is parsed,
        # logged and in some paths shown. No candid value line contains any of
        # _SECRET_MARKERS, so removing those lines cannot damage a real answer -- and
        # the one time it changes anything is the time a mnemonic would otherwise have
        # been returned into the application.
        return redact_secrets(done.stdout)

    return call


def transfer_operation(block: dict) -> dict | None:
    """The Transfer record inside one query_blocks block, or None.

    PUBLIC, and it started private. chains/payout_on_chain.py reads it too -- the payout
    read-back asks the same question of the same reply shape as the deposit scan -- and a
    name two modules import is not a private name. The alternative was a second copy of
    these three shape assumptions in that file, which is rule 8's defect with the amount
    of a payout riding on it.

    WHY THIS IS A FUNCTION AND NOT THREE LINES INLINE: it is where every shape
    assumption about the ledger's reply lives, and rule 10 puts a decision somewhere
    it can be called with seeded inputs. Three of those assumptions are not guessable
    and were read off the live reply:

      `operation` is an OPTIONAL variant, so JSON gives a LIST of zero or one element
      -- not the variant directly. An empty list is a block whose operation is absent.
      the variant itself is a one-key object: {"Transfer": {...}}, {"Mint": {...}}.
      A Mint is NOT a deposit: the local ledger's own first block is a Mint of
      100_000_000_000 e8s to the desk, which is the initial supply the init arguments
      granted. Counting it would credit a swap with the desk's entire inventory.
    """
    operation = ((block or {}).get("transaction") or {}).get("operation") or []
    if not operation:
        return None
    variant = operation[0] or {}
    return variant.get("Transfer")


class ICPAdapter:
    """Balances, deposits and transfers on one ICRC-1 ledger.

    Constructed by chains/registry.build_adapters only when BOTH
    ledger_canister_id and owner_principal are configured -- either alone is
    useless, and config.py defaults both to empty strings so that an unconfigured
    checkout builds no ICP adapter at all rather than one aimed at mainnet.
    """

    # WHY ICP CANNOT PAY OUT, SAID OUT LOUD. The verdict was already correct and
    # SILENT, which is the half this fixes.
    #
    # This class does not subclass base.RPCAdapter, so registry.py's
    # `getattr(adapter, "can_spend", False)` fail-closed and ICP read as unpayable
    # -- the right answer, reached by an absence. What the operator's /admin page
    # then printed, 2026-10-07, was:
    #
    #     ICP  receive NO
    #     ICP cannot pay out, and its adapter does not say why
    #     -- see chains/base.RPCAdapter.can_spend
    #
    # That fallback sentence is registry.py's, for exactly this case. Every other
    # unpayable chain names what to set: SOL names SOL_PAYOUT_KEYPAIR_PATH, XRP
    # names XRP_PAYOUT_SECRET_SEED. ICP named nothing, so the page could show
    # `in 0/5` and not say what would change it (rule 14: state what the number
    # means, next to the number).
    #
    # DECLARING THESE CHANGES NO BEHAVIOR, and that is checked rather than assumed:
    # getattr already returned False and now returns False explicitly. What changes
    # is the sentence beside it.
    #
    # THE REASON IS A PROPERTY OF THE LEDGER AND NOT A MISSING SETTING, which is
    # why the wording differs from SOL's and XRP's. `transfer` debits the CALLER,
    # so a payout has to be signed by the desk's own dfx identity -- and reads need
    # no identity at all, which is why ICP -> * works while * -> ICP does not.
    # Placing that identity is key material and the operator's (rule 16).
    can_spend = False
    payout_refusal = (
        "ICP cannot pay out from this process: the ledger's `transfer` debits the CALLER, so a "
        "payout must be signed by the desk's own dfx identity, and this process has none -- it "
        "makes every ledger call with `--identity anonymous`, which is sufficient for balances "
        "and fees and cannot move funds. This is why ICP -> * works and * -> ICP does not. "
        "Placing the desk identity where this process can reach it is a custody decision with no "
        "default: it is key material, and nothing in this repository moves, copies or reads it."
    )

    def __init__(self, ledger_canister_id: str, owner_principal: str, call):
        # `call` IS REQUIRED AND THE TRANSPORT SETTINGS ARE GONE FROM HERE.
        #
        # Until 2026-10-07 this took service, timeout and network_url as well and
        # built its own transport when `call` was None. Adding network_url pushed it
        # to six arguments and ruff's PLR0913/PLR0917 refused it -- correctly, and the
        # fix is the one rule 12 names for C901: "extract the decision so it can be
        # called with seeded inputs, not raise the ceiling."
        #
        # What the lint was pointing at is a layering error (rule 10): HOW the ledger
        # is reached -- compose exec against a service, or dfx against a url, with
        # what timeout -- is not something this adapter decides or needs to know. It
        # needs a callable. chains/registry.py builds the transport from Config.RPC
        # and hands it over, which is also what every test already did: all four
        # construction sites in tests/ passed `call=` and none passed service or
        # timeout, so the parameters existed for exactly one caller.
        #
        # Required rather than defaulted, because a default transport is a silent
        # choice of endpoint -- the same reason Config.RPC["ICP"] defaults its ledger
        # id and owner principal to empty instead of to mainnet's.
        """Validate what can be validated before anything opens a connection.

        Five parameters and no PLR0913 suppression: ruff does not raise it here and
        RUF100 said so when I added one anyway. They ARE the connection plus the
        injected transport, and they arrive as **Config.RPC["ICP"], a dict built
        for this signature -- the same contract RPCAdapter.__init__ has with its
        six.

        The owner principal is checked HERE, at construction, because every
        address this adapter derives depends on it: a mistyped principal would
        make every deposit address wrong in the same way, and the CRC32 inside a
        textual principal makes that detectable for free. Failing at construction
        means an unconfigured or mistyped desk never reaches the point of handing
        an address to a customer.
        """
        try:
            principal_to_bytes(owner_principal)
        except PrincipalRefused as error:
            raise ValueError(
                f"ICP owner_principal {owner_principal!r} is not a canonical textual principal, so "
                f"no adapter was constructed: {error}"
            ) from error
        if not ledger_canister_id:
            raise ValueError("ICP ledger_canister_id is empty; refusing to construct an adapter with no ledger")
        self.ledger_canister_id = ledger_canister_id
        self.owner_principal = owner_principal
        # self.service and self.timeout went with the parameters, 2026-10-07. Grepped
        # the tree first (rule 2: the NAME, not the import graph): nothing in
        # swap_terminal/ or tests/ ever read either one. They were state this object
        # carried about a transport it no longer builds.
        self._call = call

    # -- derivation, no I/O -------------------------------------------------

    def own_address(self) -> str:
        """The desk's own 64-hex account identifier: the default subaccount.

        This is the account the desk's inventory sits in -- measured on the local
        replica, it held the whole 1000 LICP. It is NEVER handed to a customer as a
        deposit address; services/icp_subaccount_service allocates indexes from 1
        for that, and the database refuses 0.
        """
        return account_identifier(self.owner_principal)

    def deposit_address(self, subaccount_index: int) -> str:
        """The 64-hex account identifier for an allocated subaccount index.

        Refuses index 0 as a second guard. The CHECK constraint in db.py is the
        guarantee; this catches a caller that derived an index some other way, and
        costs one comparison against publishing the desk's own account.
        """
        if subaccount_index < 1:
            raise ValueError(
                f"subaccount index {subaccount_index} is below 1. Index 0 is the DESK'S OWN "
                f"account -- publishing it as a deposit address would mix customer payments into "
                f"desk inventory and the watcher would read the inventory as the deposit."
            )
        return account_identifier(self.owner_principal, subaccount_from_index(subaccount_index))

    def get_new_address(self, label: str) -> str:
        """REFUSES. An ICP deposit address cannot be derived by the adapter alone.

        services/swap_service.derive_deposit_address() calls this for every chain whose
        deposits are attributed BY ADDRESS, and ICP is such a chain -- but its address
        is account_identifier(owner, subaccount), and the subaccount index must be
        ALLOCATED, uniquely, in the same database transaction as the swap row.
        services/icp_subaccount_service owns that and this adapter has no database.

        So it refuses rather than returning something address-shaped, exactly as
        chains/xrp.py refuses for its own reason. The two reasons differ and both are
        worth knowing: XRP cannot derive a per-swap account because each would need
        funding past the base reserve, so it uses one shared account and a tag. ICP CAN
        derive 2**256 addresses for free -- the obstacle is only that uniqueness is a
        database constraint, not an arithmetic property.

        WHAT A WRONG ANSWER HERE WOULD COST, which is why this is a refusal and not a
        best effort: returning own_address() would publish the DESK'S OWN account as a
        customer deposit address, mixing the payment into desk inventory and leaving the
        watcher reading the inventory as the deposit. Returning subaccount 1 for every
        swap would attribute every customer's payment to whichever swap was checked
        first. Both are silent.
        """
        raise ICPCallFailed(
            f"ICP cannot derive a deposit address from the adapter alone (label {label!r}). The "
            f"address is account_identifier(owner, subaccount) and the subaccount index must be "
            f"allocated in SQL, in the same transaction as the swap row, by "
            f"services/icp_subaccount_service.allocate_subaccount_index(). Refusing rather than "
            f"returning the desk's own account, which would mix a customer's payment into desk "
            f"inventory and make the watcher read the inventory as the deposit."
        )

    def validate_address(self, address: str) -> bool:
        """Whether `address` is a 64-hex account identifier with a valid checksum.

        The CHECKSUM, not the length and alphabet: any 64 hex characters pass the
        weaker test, and that is exactly what a truncated copy-paste produces --
        which on a payout path is funds sent somewhere unrecoverable.
        """
        return is_account_identifier(address)

    def owns_address(self, address: str) -> bool | None:
        """True if this is the desk's own default account, else None -- never False.

        None rather than False, matching chains/solana.SolanaAdapter.owns_address
        and for the same reason: this adapter cannot enumerate the desk's
        subaccounts, so it does not know that an arbitrary account is NOT the
        desk's. Returning False would be a claim it cannot support, and the
        payout-to-the-desk gate reads that claim. An allocated subaccount belongs
        to a swap and is answered from SQL by
        services/icp_subaccount_service.swap_id_for_subaccount_index, not here.
        """
        if address == self.own_address():
            return True
        return None

    # -- reads --------------------------------------------------------------

    def _nat(self, method: str, argument: str = "()") -> int:
        """Call a method returning a candid nat and parse it, or raise.

        A regex that does not match RAISES rather than returning 0. The whole
        reason this helper exists separately is that "the ledger says zero" and
        "dfx printed something this code does not understand" must never be the
        same value.
        """
        out = self._call(self.ledger_canister_id, method, argument)
        found = _NAT_RESULT.search(out)
        if not found:
            raise ICPCallFailed(
                f"{method} on {self.ledger_canister_id} returned text this adapter cannot read as "
                f"a nat, so NO value was produced (it is not zero): {out.strip()!r}"
            )
        return int(found.group(1).replace("_", ""))

    def _account_argument(self, subaccount_index: int | None) -> str:
        """The candid `Account` record for the desk's principal and a subaccount.

        Built as text because dfx takes text. The subaccount is 32 bytes written as
        a vec of nat8, which is what the ICRC-1 Account type declares.
        """
        if subaccount_index is None:
            return f'(record {{ owner = principal "{self.owner_principal}" }})'
        octets = "; ".join(str(b) for b in subaccount_from_index(subaccount_index))
        return (
            f'(record {{ owner = principal "{self.owner_principal}"; '
            f"subaccount = opt vec {{ {octets} }} }})"
        )

    def get_balance(self) -> float:
        """The desk's own spendable balance, in ICP, from its default subaccount."""
        return self._nat("icrc1_balance_of", self._account_argument(None)) / 10**ICP_DECIMALS

    def subaccount_balance(self, subaccount_index: int) -> float:
        """The balance at one allocated deposit subaccount, in ICP.

        THIS IS THE DEPOSIT WATCHER'S WHOLE JOB on ICP. There is no block to scan
        and no transaction to match: the balance appearing at the subaccount IS the
        deposit, because nothing else can pay into it.
        """
        return self._nat("icrc1_balance_of", self._account_argument(subaccount_index)) / 10**ICP_DECIMALS

    def chain_fee(self) -> float:
        """The ledger's own transfer fee, in ICP, read from the ledger every time.

        NOT a constant, and not cached. The ledger reports it through icrc1_fee()
        and that is the only authority: a number copied into Python would be a
        second one, drifting the day a ledger changes its fee with nothing failing
        (rule 8). Measured 10_000 e8s on the local replica, which matches mainnet --
        and agreement is still not a reason to hardcode it.
        """
        return self._nat("icrc1_fee") / 10**ICP_DECIMALS

    def _call_json(self, method: str, argument: str):
        """A ledger call parsed as JSON rather than as candid text.

        dfx 0.24.3 supports `--output json` (checked: `idl, raw, pp, json`), and for
        anything nested that is the difference between parsing and hoping. query_blocks
        returns records inside variants inside optionals inside a vec; a regex over
        that, on the path that decides whether a customer gets credited, is the kind of
        second implementation of somebody else's format rule 8 warns about -- except
        the cost of a miss here is an uncredited deposit rather than a merge conflict.

        TWO SHAPE FACTS that JSON output forces and that the code below relies on,
        measured against the live ledger rather than assumed:

          a blob is a LIST OF INTEGERS, so bytes(value) is the conversion and no
          escape handling is involved at all.
          every number is a STRING ("100000000000"), because candid nat64 exceeds
          what JSON numbers promise, so int() is required and a bare == against an
          int would silently never match.
        """
        raw = self._call(self.ledger_canister_id, method, argument, "json")
        try:
            return json.loads(raw)
        except json.JSONDecodeError as error:
            raise ICPCallFailed(
                f"{method} on {self.ledger_canister_id} did not return JSON, so NOTHING was parsed "
                f"(this is not an empty result): {error}. Output began {raw[:200]!r}"
            ) from error

    def _blocks_page(self, start: int, length: int, chain_length: int):
        """One page of blocks as (first_block_index, blocks). RAISES rather than return a gap.

        BOTH REFUSALS HERE EXIST FOR ONE REASON and it is this adapter's recurring one: a
        range this scan needed and did not read is indistinguishable, to the caller, from
        a customer who did not pay. So neither is allowed to become a short list.

          archived_blocks  old blocks migrate off the ledger into separate archive
                           canisters, and `blocks` then covers only what the ledger still
                           holds. A scan that read `blocks` and ignored this field would
                           silently miss deposits. Measured on the local replica
                           2026-10-06: `archived_blocks = vec {}` with chain_length 2, so
                           this has never fired here -- which is exactly why it is written
                           now rather than when it first bites.
          an empty page    the ledger sent nothing for a range it did not say was archived.
                           Breaking out of the caller's loop would turn the unread
                           remainder into an empty list, which is the 500-block trailing
                           window's defect arriving by a second route.

        A SHORTER-THAN-REQUESTED PAGE IS NOT AN ERROR and is deliberately not refused
        here: the ledger caps a reply, and the caller advances by `first + len(blocks)`
        rather than by `length`, so the unread blocks are read by the next iteration
        instead of being skipped. Refusing a short page would make a legitimate cap fatal;
        refusing an EMPTY one is different, because there is no progress to make from it
        and the loop would not terminate.

        EXTRACTED 2026-10-11 for ruff's C901 (rule 12: extract the decision, do not raise
        the ceiling), behavior unchanged.
        """
        page = self._call_json(
            "query_blocks",
            f"(record {{ start = {start} : nat64; length = {length} : nat64 }})",
        )
        archived = page.get("archived_blocks") or []
        if archived:
            raise ICPCallFailed(
                f"the ledger reports {len(archived)} archived block range(s) overlapping the scan of "
                f"blocks {start}..{start + length - 1}, so part of the history this scan needs is NOT "
                f"in the reply. Refusing rather than returning a partial answer: a missing range means "
                f"a deposit that was made and will never be seen, which is indistinguishable from a "
                f"customer who did not pay. Reading an archive canister is not implemented."
            )
        blocks = page.get("blocks") or []
        if not blocks:
            raise ICPCallFailed(
                f"query_blocks returned no blocks for {start}..{start + length - 1} and reported no "
                f"archived ranges, so {chain_length - start} block(s) of the history this scan needs "
                f"went unread for a reason the ledger did not give. Refusing rather than returning a "
                f"partial answer."
            )
        return int(page.get("first_block_index", start)), blocks

    def _deposit_event_from_block(self, block, index: str, target: bytes, address: str):
        """One block as a deposit event for `address`, or None. THE DECISION, so it is testable.

        FOUR WAYS TO BE NONE and each is an ordinary state rather than an error:

          not a Transfer    the ledger records Mint, Burn and Approve too, and none of
                            them pays an account identifier.
          paid someone else the ledger is one shared history, so most blocks in any scan
                            are other people's. Compared as BYTES against the decoded
                            target rather than as hex strings, because hex casing is not
                            normalized by the ledger and a case mismatch would read as a
                            stranger's payment.
          amount <= 0       a zero-value Transfer is not a deposit. `<= 0` and not `== 0`
                            deliberately: a negative would mean the reply is not the shape
                            this code believes, and crediting it would be worse than
                            ignoring it.
          no amount at all  `(transfer.get("amount") or {})` covers a reply missing the
                            field, which reads as zero and is then declined by the line
                            above.

        EXTRACTED 2026-10-11 from find_deposits_to_address()' inner loop, unchanged in
        behavior. The reason is in the call site's comment and in rule 12's reading of
        C901: the method had grown a paging loop around this filter and the decision could
        no longer be exercised without seeding a whole ledger.
        """
        transfer = transfer_operation(block)
        if transfer is None:
            return None
        if bytes(transfer.get("to") or []) != target:
            return None
        e8s = int((transfer.get("amount") or {}).get("e8s", 0))
        if e8s <= 0:
            return None
        return {
            "txid": index,
            "vout": 0,
            "address": address,
            "amount": e8s / 10**ICP_DECIMALS,
            "confirmations": self.deposit_confirmations(),
        }

    def find_deposits_to_address(self, address: str, tx_limit: int = MAX_BLOCKS_SCANNED,
                                 skip_txids=frozenset(), from_block: int = 0):
        """Every ledger Transfer INTO `address`, as deposit events. THE WATCHER'S JOB.

        Returns the shape services/deposit_service.record_deposit_event() consumes --
        txid, vout, address, amount, confirmations -- so the ICP path needs no special
        case there.

        THE BLOCK INDEX IS THE TXID, and that is the whole reason this reads blocks
        instead of balances. A balance is a single number: report it as an event and a
        second deposit to the same subaccount either collides with the first row
        (deposit_events is keyed on asset+txid+vout and the update path does not touch
        amount, so the second payment would be invisible) or arrives as a new row that
        SUMS with the first, crediting 0.75 for a 0.5 balance. refresh_swap_from_chain()
        sums every row, so only one-event-per-payment is correct. A block index is
        unique per payment and never reused, which is exactly what that key needs.

        vout is 0 for every event. ICP has no outputs; the column exists because the
        UTXO chains need it, and a constant keeps the key (asset, txid, vout) unique
        per payment without inventing a meaning for it.

        confirmations is 1 and means FINAL -- see deposit_confirmations() for why that
        is a compatibility value rather than a chain fact.

        ARCHIVED BLOCKS ARE REFUSED, NOT SKIPPED, and this is the part that would
        otherwise lose a customer's money. Old blocks migrate off the ledger into
        separate archive canisters; `blocks` then covers only what the ledger still
        holds, and `archived_blocks` names the ranges that moved. A scan that read
        `blocks` and ignored that field would silently miss deposits -- the customer
        paid, the watcher polls forever, and nothing errors. So a non-empty
        archived_blocks over the range being scanned RAISES. Measured on the local
        replica 2026-10-06: `archived_blocks = vec {}` with chain_length 2, so the
        refusal has never fired here and that is precisely why it is written now
        rather than when it first bites.

        =====================================================================
        IT USED TO SCAN ONLY THE LAST 500 BLOCKS AND RETURN [] FOR ANYTHING OLDER
        =====================================================================

        The two lines were:

            window = min(int(tx_limit), chain_length)
            start = chain_length - window

        so blocks [0, chain_length - 500) were never examined, and a Transfer into the
        swap's subaccount below that was not in the page. The loop never saw it and this
        returned [] -- with no error and no log line. The swap sat at awaiting_deposit
        forever while the money was provably in its subaccount.

        AND THE REFUSAL WRITTEN FOR EXACTLY THIS COULD NOT FIRE. The archived_blocks
        check above exists because "a missing range means a deposit that was made and
        will never be seen, which is indistinguishable from a customer who did not pay"
        -- and it was computed over the window that WAS requested, so it reported an
        empty vec and declined. The identical partial answer produced by this function's
        own bound was returned as a normal empty list. The same defect, with the guard
        sitting beside it looking at the wrong range.

        THE BLOCK INDEX IS GLOBAL, which is why 500 is not 500 of this swap's payments.
        Every ledger transaction advances it: every other swap's deposit, every payout,
        every fund_desk.py top-up. So the window is consumed by traffic that has nothing
        to do with this address.

        LATENT UNTIL IT IS NOT. On the operator's replica chain_length was 2 when this
        was measured, and below 500 the arithmetic is a no-op -- min(500, 2) is 2 and
        start is 0, so the whole chain was scanned and nothing was dropped. What flips
        it, in order of likelihood: 500 ledger transactions accumulating locally (one per
        deposit, payout and top-up, one-way and unmonitored); the watcher being down
        across 500 blocks; and pointing ICP_LEDGER_CANISTER_ID at the ICP MAINNET ledger,
        where the index is global across the whole network and 500 blocks is MINUTES.

        =====================================================================
        IT NOW PAGES FORWARD FROM `from_block` AND REFUSES RATHER THAN TRUNCATES
        =====================================================================

        FORWARD FROM A FLOOR, not backward from the head, so no block between the floor
        and the head is ever skipped. `from_block` defaults to 0 -- the whole chain --
        because that is the only floor this function can establish on its own: it is
        handed an address and a skip set, and neither says "every block below N has
        already been examined". A caller that can establish one (a scan cursor in
        swap_terminal.db, which does not exist yet) can pass it and pay for far less.

        `tx_limit` IS NOW A CEILING ON BLOCKS SCANNED, AND EXCEEDING IT RAISES. That is
        the symmetry the archived-blocks branch already had and this did not: a partial
        answer is refused rather than returned, because the caller cannot tell a
        truncated [] from a customer who did not pay. On a chain longer than the ceiling
        the watcher now fails loudly for that swap -- and only for that swap, because
        services/deposit_service.process_active_swaps() gained per-swap isolation the
        same day, so one raising swap no longer stops the cycle and the cycle line names
        the asset. That fix had to land first for this one to be safe.

        EVERY SCAN LOGS ITS RANGE (rule 14). A bounded scan and a complete one used to
        render identically, which is what let this hide: the whole point of the log line
        is that "scanned 0..1" and "scanned 400000..400500" are visibly different
        answers to the same question.
        """
        if not self.validate_address(address):
            raise ICPCallFailed(
                f"{address!r} is not a valid ICP account identifier, so no scan was attempted. "
                f"Returning no deposits for an unreadable address would look like an unpaid customer."
            )
        target = bytes.fromhex(address)

        # One cheap call for chain_length. Asking for length 1 rather than 0 because a
        # zero-length request is a shape the ledger need not answer usefully, and the
        # response carries chain_length regardless of how many blocks come back.
        head = self._call_json("query_blocks", "(record { start = 0 : nat64; length = 1 : nat64 })")
        chain_length = int(head.get("chain_length", 0))
        if chain_length == 0:
            return []

        start = max(0, int(from_block))
        to_scan = chain_length - start
        if to_scan <= 0:
            logger.info(
                "ICP scan of %s: from_block=%d is at or past chain_length=%d, so there is nothing "
                "new to read. This is a RESULT, not a skipped scan.",
                address, start, chain_length,
            )
            return []
        if to_scan > int(tx_limit):
            raise ICPCallFailed(
                f"scanning blocks {start}..{chain_length - 1} for {address} would read {to_scan} "
                f"blocks, over the ceiling of {tx_limit}. REFUSING rather than scanning a trailing "
                f"window: until 2026-10-11 this read only the last {tx_limit} blocks and returned [] "
                f"for a deposit below them -- no error, no log line, and a swap that polled forever "
                f"with the money provably in its subaccount. A truncated [] is indistinguishable "
                f"from a customer who did not pay, which is the same reason the archived-block "
                f"refusal above exists. Remedies: pass from_block, once a scan cursor exists in "
                f"swap_terminal.db; or raise tx_limit if this chain really is this long and the "
                f"scan is affordable. On the ICP MAINNET ledger neither is enough and an archive "
                f"reader is required."
            )

        found = []
        scanned_to = start
        while scanned_to < chain_length:
            length = min(PAGE_BLOCKS, chain_length - scanned_to)
            first, blocks = self._blocks_page(scanned_to, length, chain_length)
            for offset, block in enumerate(blocks):
                index = str(first + offset)
                if index in skip_txids:
                    continue
                # ONE DECISION, ONE FUNCTION (rule 10). Extracted when ruff's C901 took
                # this method to 13 -- which rule 12 reads as orchestration having
                # swallowed decisions, and the decision here is "is this block a payment
                # to this address, and for how much". It is now callable with a seeded
                # block and asserted on directly, which the paging loop around it is not.
                event = self._deposit_event_from_block(block, index, target, address)
                if event is not None:
                    found.append(event)
            # FROM WHAT CAME BACK, not from what was asked for. A ledger that returns a
            # shorter page than requested must not have the gap counted as scanned --
            # advancing by `length` would skip exactly the blocks it declined to send.
            scanned_to = first + len(blocks)

        # RULE 14: THE RANGE IS ON EVERY SCAN, COMPLETE OR NOT. A bounded scan and a
        # complete one rendered identically before today, which is what let the trailing
        # window hide for as long as it did. `(none)` is a result and is printed as one.
        logger.info(
            "ICP scan of %s: read blocks %d..%d of chain_length=%d (%d block(s)) and found %s. "
            "The range is printed because a scan that covered part of the chain and one that "
            "covered all of it used to look the same, and a deposit below the window was returned "
            "as an empty list.",
            address, start, chain_length - 1, chain_length, chain_length - start,
            f"{len(found)} deposit(s)" if found else "(none)",
        )
        return found

    def deposit_confirmations(self) -> int:
        """Always 1, and this is a COMPATIBILITY value rather than a chain fact.

        ICP has no confirmation depth. A transfer is final when the ledger returns
        a block index -- there is no reorg to wait out and no mempool to clear. The
        existing deposit watcher asks every adapter for a confirmation count, so
        this answers 1 to mean "final", and says so here rather than letting a
        reader infer that ICP has a one-block rule.
        """
        return 1

    # -- the path that moves money -----------------------------------------

    def ledger_account_identifier(self, subaccount_index: int | None = None) -> str:
        """Ask the LEDGER to compute the account identifier, and return its answer.

        A free cross-check the ledger offers (`account_identifier : (Account) ->
        (AccountIdentifier) query`): it does the same SHA224-plus-CRC32 derivation
        chains/icp_account does, from the same principal and subaccount. If the two
        ever disagree, the deposit address this terminal publishes is not the account
        the ledger credits -- a customer would pay and the watcher would poll an
        account that stays at zero forever, with nothing erroring.

        Returns the 64-hex form. Compare it with own_address() or
        deposit_address(n); verify_derivation() below does exactly that.
        """
        out = self._call(self.ledger_canister_id, "account_identifier", self._account_argument(subaccount_index))
        found = _BLOB_RESULT.search(out)
        if not found:
            raise ICPCallFailed(
                f"account_identifier on {self.ledger_canister_id} returned text this adapter cannot "
                f"read as a blob, so NO identifier was produced (it is not empty): {out.strip()[:200]!r}"
            )
        return _blob_to_hex(found.group(1))

    def verify_derivation(self, subaccount_index: int | None = None) -> tuple[bool, str]:
        """(agrees, explanation) -- does this repository's derivation match the ledger's?

        THE CHECK WORTH RUNNING BEFORE ANY PAYOUT, because it is the one assumption
        underneath every ICP address this system publishes and it costs one query
        call. A disagreement is not a rounding difference: it means every deposit
        address is wrong in the same way.
        """
        ours = self.own_address() if subaccount_index is None else self.deposit_address(subaccount_index)
        theirs = self.ledger_account_identifier(subaccount_index)
        where = "the desk's default account" if subaccount_index is None else f"subaccount {subaccount_index}"
        if ours == theirs:
            return True, f"{where}: this repository and the ledger derive the same account ({ours})"
        return False, (
            f"{where}: DISAGREEMENT. chains/icp_account derived {ours} and the ledger's own "
            f"account_identifier says {theirs}. Every ICP address this terminal publishes is wrong "
            f"in the same way; do not send or publish anything until this is resolved."
        )

    # -- the path that moves money -----------------------------------------

    def send_to_address(self, address: str, amount: float, created_at_time_nanos: int | None = None) -> str:
        """Transfer `amount` ICP to a 64-hex account identifier. Returns the block index.

        THE LEDGER'S LEGACY `transfer`, NOT icrc1_transfer, and the reason is the gap
        that blocked this method until 2026-10-06. icrc1_transfer takes an ICRC-1
        Account -- a principal plus an optional subaccount -- and a customer supplies a
        64-hex ACCOUNT IDENTIFIER, which is SHA224 over that pair and CANNOT BE
        INVERTED. The legacy `transfer` takes the identifier directly, as a blob:

            type AccountIdentifier = blob;        // 32 bytes, CRC32 || SHA224
            transfer : (TransferArgs) -> (TransferResult);

        read from the deployed ledger's own interface
        (dfinity/ic rs/ledger_suite/icp/ledger.did), not from memory.

        THE FEE IS READ FROM THE LEDGER AND PASSED EXPLICITLY, because legacy
        `transfer` requires it and rejects a mismatch with `BadFee` naming
        `expected_fee`. A BadFee is NOT retried here at the expected figure: a retry
        is a second send attempt, and this method must never make two where a caller
        asked for one. It refuses and reports what the ledger said instead.

        created_at_time IS THE IDEMPOTENCY KEY AND THE DEFAULT IS THE DANGEROUS ONE.
        The ledger deduplicates on (from, to, amount, fee, memo, created_at_time) and
        returns TxDuplicate carrying the ORIGINAL block index. With created_at_time
        null the ledger stamps its own time, so two identical calls are two different
        transactions and a retry after a timeout DOUBLE-PAYS.

        IT MUST BE A REAL TIMESTAMP WITHIN 24 HOURS OF LEDGER TIME, and this
        paragraph used to get that wrong. It said to pass "a value derived from the
        swap rather than from a clock", which read as licence to use any
        swap-deterministic number. The first attempt used a fixed constant and the
        ledger refused it outright, measured on the local replica 2026-10-06:

            Err = variant { TxTooOld = record {
              allowed_window_nanos = 86_400_000_000_000 : nat64 } }

        86_400_000_000_000ns is 24 hours. The constant was a year in the past, so it
        was not merely non-idempotent -- it could not be sent at all. Nothing moved.

        So the key is the swap's RECORDED CREATION TIME: real time, captured once
        when the swap row was written, and reused unchanged on every retry. That
        satisfies both constraints at once -- the same value on a retry gives dedup,
        and a value from when the swap was created is inside the window for any swap
        being paid promptly. What it must NOT be is `time.time_ns()` at send time,
        because that is a different number on every retry, which is the double-pay
        case wearing the shape of a fix.

        THE COROLLARY IS A REAL LIMIT, not a detail: a swap whose recorded creation
        time is more than 24 hours old CANNOT be paid idempotently through this
        field. The ledger will refuse with TxTooOld, and whoever pays it has to
        decide between a fresh key (no dedup protection) and not paying -- which is a
        live-posture decision and does not belong in this adapter.

        None is still the default because this signature has to match what the payout
        path calls today, and silently inventing a key from a clock would be the same
        bug wearing a safety label. The refusal below is what stops it mattering.
        """
        if not self.validate_address(address):
            raise ICPCallFailed(
                f"{address!r} is not a valid ICP account identifier (64 hex with a matching "
                f"CRC32), so NOTHING was sent. A principal is a different thing and fails here."
            )
        e8s = amount_to_base_units(amount, ICP_DECIMALS)
        if e8s <= 0:
            raise ICPCallFailed(f"{amount} ICP is {e8s} e8s; refusing to send a non-positive amount")
        if created_at_time_nanos is None:
            raise ICPCallFailed(
                "refusing to send with created_at_time unset. The ICP ledger deduplicates on "
                "(from, to, amount, fee, memo, created_at_time) for 24 hours and returns the "
                "ORIGINAL block index for a repeat -- but only if created_at_time is given. With it "
                "null the ledger stamps its own time, every retry is a NEW transaction, and a "
                "payout worker that times out and retries pays twice. Pass a value derived from "
                "the swap (not from a clock) and a retry becomes idempotent. NOTHING was sent."
            )

        fee_e8s = self._nat("icrc1_fee")
        argument = transfer_argument(
            to_account=address, e8s=e8s, fee_e8s=fee_e8s,
            created_at_time_nanos=created_at_time_nanos,
        )
        out = self._call(self.ledger_canister_id, "transfer", argument)
        index = transfer_block_index(out)
        if index:
            return index

        raise ICPCallFailed(
            f"transfer of {amount} ICP ({e8s} e8s, fee {fee_e8s} e8s) to {address} did not return a "
            f"block index. Whether anything moved is NOT established by this message -- read the "
            f"ledger's reply: {out.strip()[:400]!r}. TxDuplicate is NOT among the cases that reach "
            f"here -- it is handled above as the success it is. Of what remains: `BadFee` names the "
            f"fee the ledger expects and is NOT retried here, because a retry is a second send. "
            f"`TxTooOld` means created_at_time is outside the ledger's window "
            f"(86_400_000_000_000ns = 24h): the key must be a REAL timestamp near now, so a swap "
            f"older than a day cannot be paid idempotently through it at all."
        )
