"""The ICP chain adapter: balances and deposits on an ICRC-1 ledger, through dfx.

Role: module (a stage; the decisions it needs live in chains/icp_account.py and
      services/icp_subaccount_service.py, which it calls)
Reads: the ICRC-1 ledger canister named in Config.RPC["ICP"], by running dfx
Writes: nothing in this repository. icrc1_transfer writes to a ledger.
Can send orders: it CAN move funds -- send_to_address calls icrc1_transfer. See
      WHAT HAS NEVER BEEN EXERCISED below: that path has not been run once.
Live-safe: against a LOCAL REPLICA, yes, and that is the only thing it has been
      pointed at. Nothing here defaults to mainnet (config.py's ICP entry has
      empty defaults on purpose), and ICP has no testnet, so "the local replica"
      is the whole of the safe territory.

WHY THE TRANSPORT IS dfx AND NOT AN HTTP CLIENT, measured rather than preferred.
SOL and XRP adapters hold a URL and speak JSON-RPC over HTTP. ICP speaks candid
inside a CBOR agent envelope, and an update call additionally needs
representation-independent request-id hashing plus certificate verification on the
way back. There is one Python library for that, `ic-py`, and it does not install
here -- measured 2026-10-06, three ways:

    pip install ic-py
      -> ERROR: Failed building wheel for antlr4-python3-runtime
    pip install antlr4-python3-runtime==4.13.2   (succeeds alone)
    then import ic.candid
      -> Exception: Could not deserialize ATN with version 3 (expected 4)

ic-py's generated .did parser is locked to the antlr 4.9-era runtime, whose sdist
will not build on this machine. Hand-rolling the agent protocol instead was
considered and rejected: it is a second implementation of somebody else's wire
format (rule 8) on a path that moves money, and the half that matters most --
certificate verification -- is the half that cannot be tested from here, so a
mistake in it would be silent. dfx IS the reference implementation, it is pinned in
docker/icp-replica.Dockerfile, and it already holds the identities under /state.

THE TRANSPORT IS A PARAMETER, which is what makes this file testable. `call` is
injected and defaults to the dfx-in-compose implementation; every test in
tests/test_icp_adapter.py passes a function returning seeded candid text, so the
parsing, the refusals and the amount arithmetic are exercised with no container,
no network, and no ledger. That is the same shape rule 10 asks for -- the decision
is a function that can be called with seeded inputs.

WHAT HAS NEVER BEEN EXERCISED, stated plainly because rule 16 draws the line at
"a fix you cannot test here is a proposal": send_to_address has not been run
against any ledger, local or otherwise. Its candid argument is constructed from
the real interface (dfinity/ic rs/ledger_suite/icp/ledger.did, read rather than
recalled) and its tests assert on the text it builds, which is not the same as a
transfer having happened. THIS PARAGRAPH IS THE ONE TO DELETE FIRST once the
operator has run a transfer on the local replica. The read paths have
been exercised against the real released ICP ledger on the local replica --
icrc1_balance_of, icrc1_fee, icrc1_decimals and icrc1_symbol all answered, and
the balance agreed with the account identifier this repository derived.

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

import re
import shlex
import subprocess

from .coin_amounts import amount_to_base_units
from .icp_account import (
    PrincipalRefused,
    account_identifier,
    is_account_identifier,
    principal_to_bytes,
    subaccount_from_index,
)

#: ICP is 8 decimals (e8s). Named here for readability at the call sites below;
#: coin_amounts.CHAIN_DECIMALS is the table, and the ledger confirmed 8 via
#: icrc1_decimals on 2026-10-06.
ICP_DECIMALS = 8

#: What `dfx canister call` prints for a nat: digits with underscore separators and
#: a candid type suffix, e.g. `(100_000_000_000 : nat)`. Matched rather than
#: eval'd -- this is somebody else's output format and a regex that fails to match
#: raises below instead of producing a number.
_NAT_RESULT = re.compile(r"\(\s*([\d_]+)\s*:\s*nat(?:64)?\s*\)")

#: `(variant { Ok = 3 : nat64 })` from transfer. The block index is the closest
#: thing ICP has to a txid.
_TRANSFER_OK = re.compile(r"Ok\s*=\s*([\d_]+)")

#: `(blob "\0a\1b...")` from account_identifier. dfx prints a candid blob as a
#: quoted string of \xx escapes, with printable ASCII bytes left as themselves --
#: which is why _blob_to_hex below cannot simply strip backslashes.
_BLOB_RESULT = re.compile(r'blob\s*"((?:[^"\\]|\\.)*)"')


def _hex_to_blob(hex_text: str) -> str:
    r"""A 64-hex account identifier as a candid blob literal for dfx.

    EVERY byte is escaped as \xx, including the printable ones. dfx accepts a
    mixed form, but emitting one would mean this function's output changes shape
    with the VALUE -- and a 32-byte identifier containing an accidental `"` or `\`
    would then need escaping rules this does not have. Uniform escapes have no such
    case.
    """
    return '"' + "".join(f"\\{b:02x}" for b in bytes.fromhex(hex_text)) + '"'


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


def dfx_transport(service: str, timeout: float):
    """Return a `call(canister, method, argument) -> str` that runs dfx in compose.

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

    def call(canister: str, method: str, argument: str) -> str:
        argv = [
            "docker", "compose",
            "-f", "docker-compose.yml",
            "-f", "docker-compose.icp.yml",
            "exec", "-T", service,
            "dfx", "canister", "call", canister, method, argument,
        ]
        try:
            done = subprocess.run(  # noqa: S603 -- checked: no shell, argv is a fixed list, and the only caller-supplied elements are a canister id, a method name and a candid argument this repository builds
                argv, capture_output=True, text=True, timeout=timeout, check=False,
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


class ICPAdapter:
    """Balances, deposits and transfers on one ICRC-1 ledger.

    Constructed by chains/registry.build_adapters only when BOTH
    ledger_canister_id and owner_principal are configured -- either alone is
    useless, and config.py defaults both to empty strings so that an unconfigured
    checkout builds no ICP adapter at all rather than one aimed at mainnet.
    """

    def __init__(self, ledger_canister_id: str, owner_principal: str, service: str = "icp-replica", timeout: float = 60.0, call=None):
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
        self.service = service
        self.timeout = timeout
        self._call = call if call is not None else dfx_transport(service, timeout)

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
        The ledger deduplicates on (from, to, amount, fee, memo, created_at_time)
        within a 24-hour window and returns TxDuplicate carrying the ORIGINAL block
        index. With created_at_time null the ledger stamps its own time, so two
        identical calls are two different transactions and a retry after a timeout
        DOUBLE-PAYS. A caller that may retry -- which is every payout worker -- must
        pass a value derived from the swap rather than from a clock, and then a retry
        is answered with the first transfer's block index instead of sending again.

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
        raise ICPCallFailed(
            f"transfer of {amount} ICP ({e8s} e8s, fee {fee_e8s} e8s) to {address} did not return a "
            f"block index. Whether anything moved is NOT established by this message -- read the "
            f"ledger's reply: {out.strip()[:400]!r}. A `TxDuplicate` means an earlier identical "
            f"transfer already happened and carries its block index; a `BadFee` names the fee the "
            f"ledger expects and is NOT retried here, because a retry is a second send."
        )
