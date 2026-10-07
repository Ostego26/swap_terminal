"""The ICP chain adapter: balances and deposits on an ICRC-1 ledger, through dfx.

Role: module (a stage; the decisions it needs live in chains/icp_account.py and
      services/icp_subaccount_service.py, which it calls)
Reads: the ICRC-1 ledger canister named in Config.RPC["ICP"], by running dfx
Writes: nothing in this repository. icrc1_transfer writes to a ledger.
Can send orders: it CAN move funds -- send_to_address calls icrc1_transfer. See
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

_COMPOSE_FILES = (_REPO_ROOT / "docker-compose.yml", _REPO_ROOT / "docker-compose.icp.yml")


def dfx_transport(service: str, timeout: float, network_url: str = ""):
    """Return a `call(canister, method, argument) -> str` that reaches the ledger.

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
            dfx_argv += ["--network", network_url]
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
            argv = [
                "docker", "compose",
                "-f", str(_COMPOSE_FILES[0]),
                "-f", str(_COMPOSE_FILES[1]),
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
                f"{done.stderr.strip() or '(no stderr)'}. Command was: {shlex.join(argv)}"
            )
        return done.stdout

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

    def find_deposits_to_address(self, address: str, tx_limit: int = 500, skip_txids=frozenset()):
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

        window = min(int(tx_limit), chain_length)
        start = chain_length - window
        page = self._call_json(
            "query_blocks",
            f"(record {{ start = {start} : nat64; length = {window} : nat64 }})",
        )
        archived = page.get("archived_blocks") or []
        if archived:
            raise ICPCallFailed(
                f"the ledger reports {len(archived)} archived block range(s) overlapping the scan of "
                f"blocks {start}..{chain_length - 1}, so part of the history this scan needs is NOT "
                f"in the reply. Refusing rather than returning a partial answer: a missing range "
                f"means a deposit that was made and will never be seen, which is indistinguishable "
                f"from a customer who did not pay. Reading an archive canister is not implemented."
            )

        first = int(page.get("first_block_index", start))
        found = []
        for offset, block in enumerate(page.get("blocks") or []):
            index = str(first + offset)
            if index in skip_txids:
                continue
            transfer = transfer_operation(block)
            if transfer is None:
                continue
            if bytes(transfer.get("to") or []) != target:
                continue
            e8s = int((transfer.get("amount") or {}).get("e8s", 0))
            if e8s <= 0:
                continue
            found.append({
                "txid": index,
                "vout": 0,
                "address": address,
                "amount": e8s / 10**ICP_DECIMALS,
                "confirmations": self.deposit_confirmations(),
            })
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
        argument = (
            "(record { memo = 0 : nat64; "
            f"amount = record {{ e8s = {e8s} : nat64 }}; "
            f"fee = record {{ e8s = {fee_e8s} : nat64 }}; "
            "from_subaccount = null; "
            f"to = {_hex_to_blob(address)}; "
            f"created_at_time = opt record {{ timestamp_nanos = {created_at_time_nanos} : nat64 }} }})"
        )
        out = self._call(self.ledger_canister_id, "transfer", argument)
        ok = _TRANSFER_OK.search(out)
        if ok:
            return ok.group(1).replace("_", "")

        # TxDuplicate IS SUCCESS, AND RAISING ON IT WAS A DEFECT THIS FILE SHIPPED.
        # The ledger is saying "this exact transfer already happened; its block index
        # is <n>". That is precisely what the idempotency key exists to produce, so a
        # retry after a timeout must get the SAME answer the first call would have
        # given -- not an exception. Measured on the local replica 2026-10-06: the
        # same call twice left desk 999.7499 and subaccount 1 at 0.25 both times, the
        # second answering TxDuplicate, so the funds genuinely did not move twice.
        #
        # The version before this returned the block index on Ok and RAISED here,
        # with a message telling the caller to read it as success. That put the
        # decision in prose a payout worker would have had to parse, and the failure
        # mode is specific: a worker retrying after a timeout sees an exception and
        # marks a payout that SUCCEEDED as failed (rule 10 -- the decision belongs in
        # a function, not in an error string).
        duplicate = _TRANSFER_DUPLICATE.search(out)
        if duplicate:
            return duplicate.group(1).replace("_", "")
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
