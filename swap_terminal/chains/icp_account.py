"""An ICP account from a principal and a subaccount, and the text forms of both.

Role: function (the decisions: what a principal's bytes are, what account a
      (principal, subaccount) pair names, and whether a string is either)
Reads: nothing. No socket, no file, no environment, no replica.
Writes: nothing
Can move funds: no
Mainnet-safe: yes -- every function here is arithmetic over bytes. The same code
      answers for the local replica and for mainnet because an account
      identifier is not a network-dependent quantity.

WHY A SEPARATE MODULE FROM THE ADAPTER, which is the same argument
chains/pubkey_address.py and chains/xrp_address.py already make: deriving an
address is a DECISION, an adapter is orchestration, and rule 10 puts the decision
at the bottom where it can be called with seeded inputs. Every address bug this
project has had was a decision inlined somewhere it could not be tested.

TWO ENCODINGS, AND THEY ARE NOT INTERCHANGEABLE. This is the part a reader has
to get right before writing anything that touches ICP.

  PRINCIPAL          base32 of CRC32||bytes, lowercase, grouped in fives with
                     dashes: `ryjl3-tyaaa-aaaaa-aaaba-cai`. It names an identity
                     or a canister. It is NOT an ICP ledger address.

  ACCOUNT IDENTIFIER 64 hex characters: CRC32||SHA224(domain || principal ||
                     subaccount). It is what the ICP ledger's `transfer` takes,
                     what `dfx ledger account-id` prints, and what every ICP
                     wallet will accept as a destination. It is a one-way hash --
                     an account identifier cannot be turned back into the
                     principal that produced it.

A principal pasted into a field that wants an account identifier is not a typo
that fails; the two have different lengths and alphabets, so it fails loudly,
which is the one piece of luck in this design.

THE THIRD ENCODING THAT IS DELIBERATELY ABSENT. ICRC-1 also defines a TEXTUAL
ACCOUNT form -- principal, a checksum, then the subaccount in hex after a dot --
and this module does not implement it. Not because it is unnecessary but because
I have not verified it against a reference implementation, and rule 17 says a
plausible reading of a spec must not be written in the same voice as a
measurement. The legacy account identifier below IS cross-checked (see the next
section), so it is the one this module offers. Adding the ICRC-1 textual form is
named work, not a gap to paper over: it needs a vector from something that is not
this file.

HOW THE ALGORITHMS HERE WERE ESTABLISHED, because "it looked right" has cost this
project real investigations:

  principal text     reproduces both canonical vectors exactly. The management
                     canister is the empty byte string and encodes to `aaaaa-aa`;
                     the anonymous principal is the single byte 0x04 and encodes
                     to `2vxsx-fae`. Both are checked in
                     tests/test_icp_account.py, and both came out right on the
                     first run of this code rather than being tuned until they
                     did.

  account identifier MEASURED against dfx, 2026-10-06, on the local replica in
                     docker-compose.icp.yml:

                         dfx identity get-principal  ->  a 29-byte principal
                         dfx ledger account-id       ->  a0263999...6c12042

                     account_identifier() on the first returns the second
                     exactly. The pair is pinned in
                     tests/test_icp_account.test_dfx_ledger_account_id_agrees_with_this_module.

                     THIS PARAGRAPH USED TO SAY "cross-check against somebody
                     else's code, not a measurement against a running ledger",
                     naming ic-py 1.0.1's `AccountIdentifier.new` and the command
                     that would settle it. That was the honest state for about an
                     hour and it is kept here because the distinction is the
                     point: ic-py still agrees, line for line -- sha224 over
                     b"\\x0Aaccount-id", then the principal's bytes, then a
                     32-byte subaccount, with CRC32 of that digest prepended --
                     but agreement between two implementations is not what the
                     claim rests on any more.

                     What the measurement FOUND, which is the argument for taking
                     it rather than trusting the agreement: the dfx identity's
                     principal is 29 bytes, exactly MAX_PRINCIPAL_BYTES. The
                     limit below was written from a specification and reads like
                     an edge case; it is the ordinary size of a real identity, so
                     an off-by-one there would have refused every
                     self-authenticating principal on the network while every
                     canister-id test passed.

THE EQUIVALENCE THE WHOLE DEPOSIT DESIGN RESTS ON, MEASURED 2026-10-06 against
the real ICP ledger canister (ledger-suite-icp-2025-08-29) running on the local
replica. Until this was run it was the one claim here with nothing behind it but
ic-py and a reading of the specification:

    the ledger was FUNDED by a 64-hex account identifier this module derived,
    written into its init arguments by icp_ledger_init.py --

        a0263999097bcda484aa158ee3a227034a1a5043f0db346ffecaf4b3a6c12042
        100_000_000_000 e8s

    and then QUERIED the other way, by principal with no subaccount --

        icrc1_balance_of(record { owner = principal "ybr6p-...-cqe" })
          -> 100_000_000_000 : nat

So account_identifier(p) and the ICRC-1 account (owner = p, subaccount = default)
are THE SAME ACCOUNT on a real ledger. That is what makes a per-swap deposit
address work at all: the deposit instruction handed to a customer is the 64-hex
form, and the adapter detects the payment through icrc1_balance_of on the
(principal, subaccount) pair. If the two encodings disagreed, the terminal would
publish an address, the customer would pay it, and the watcher would query an
account that stays at zero forever -- no error anywhere.

Two other values came back from the same ledger and both matter here:

    icrc1_decimals -> 8      so chains/coin_amounts.CHAIN_DECIMALS is the table
                             that answers for ICP, not a second one.
    icrc1_fee      -> 10_000 e8s. Which is what DFINITY's documented example
                             seeds and what mainnet charges -- and it is STILL
                             not a constant anywhere in this codebase, for the
                             reason at the end of this docstring. Agreeing with
                             the default does not make a copied number an
                             authority.

WHAT ICP CHANGES ABOUT THE TERMINAL'S ASSUMPTIONS, recorded here because this is
the first file in the repository to encounter it:

  no confirmations   a transfer is final when the ledger returns its block index.
                     There is no depth to wait for and no reorg to wait out, so
                     the GRC/BTC/LTC `confirmations >= N` question has no ICP
                     analogue. An ICP deposit is either in the ledger or it is
                     not. Whatever the adapter reports for `confirmations` is a
                     compatibility value for the existing deposit watcher, and
                     the place that decides it must say so out loud.

  no UTXOs           balances, not outputs. There is no vout to read, so the
                     whole `_raw_tx_for_vouts` family in chains/base.py has
                     nothing to do here.

  subaccounts are    one principal owns 2**256 accounts, and the owner picks the
  free               subaccount. That makes a per-swap deposit address cost
                     nothing -- no key to generate, no address to fund, no
                     keypool to flush. It is the BTC per-swap-address model
                     without the key management, and it is strictly better than
                     the XRP destination tag and Solana memo mechanisms, which
                     exist because those chains have one account and need the
                     payer to label the payment. ICP needs nothing from the
                     payer.

  8 decimals         e8s, the same precision as BTC, LTC and GRC, so
                     chains/coin_amounts.CHAIN_DECIMALS is the table that already
                     answers for it rather than a second one.

NO FEE CONSTANT IN THIS FILE, and that is deliberate. The ICP ledger's transfer
fee is a value the ledger canister reports through `icrc1_fee()`, and a number
copied into Python here would be a second authority for it -- rule 8's shape,
with the drift arriving the day a ledger changes its fee and nothing fails.
"""

from __future__ import annotations

import base64
import hashlib
import zlib

#: The domain separator the ICP ledger hashes before anything else. The leading
#: byte is the LENGTH of the string that follows (10 == len("account-id")), which
#: is a convention the IC uses throughout its hashed structures; it is not a null
#: and must not be "cleaned up".
_ACCOUNT_DOMAIN = b"\x0Aaccount-id"

#: CRC32 is four bytes, big-endian, in both encodings here.
_CRC_BYTES = 4

#: A subaccount is exactly 32 bytes on the ICP ledger. Not "up to" 32: the ledger
#: hashes a fixed-width field, so a 31-byte subaccount and that same subaccount
#: with a leading zero are DIFFERENT accounts if either is passed through
#: unpadded. Everything in this module pads to 32 and refuses anything longer.
SUBACCOUNT_BYTES = 32

#: The default subaccount -- 32 zero bytes. `dfx ledger account-id` with no
#: --subaccount prints the account identifier for this one, which is why it is the
#: vector the tests reach for first.
DEFAULT_SUBACCOUNT = bytes(SUBACCOUNT_BYTES)

#: ICP is 8 decimals -- e8s. CONFIRMED by asking the deployed ledger
#: (icrc1_decimals -> (8 : nat8), 2026-10-06) rather than inferred from the name.
#:
#: IT LIVES HERE AND NOT IN chains/icp.py, which is where it started. That module
#: imports subprocess for the dfx transport, and chains/payout_quantization.py is pure
#: arithmetic that must not pull a process launcher in to learn a number -- the same
#: circular-and-heavyweight import problem that module's own header records for
#: SOL_DECIMALS. This file imports nothing but hashlib, base64 and zlib.
ICP_DECIMALS = 8

#: A principal is at most 29 bytes. The limit is in the interface specification
#: and in ic-py's own MAX_LENGTH_IN_BYTES; it is asserted here so that a string
#: that base32-decodes to something absurd is refused by LENGTH before anything
#: hashes it.
MAX_PRINCIPAL_BYTES = 29


class PrincipalRefused(ValueError):
    """A string was not a principal, or bytes were not a usable principal.

    A DISTINCT TYPE rather than bare ValueError, for the reason rule 12's BLE001
    note gives: a caller that wants to tell "the customer pasted something that
    is not an ICP identity" apart from "this code has a bug" cannot do it by
    reading a message. Every refusal here carries what was rejected and why.
    """


class SubaccountRefused(ValueError):
    """A subaccount was not something this module will hash into an account."""


def principal_to_text(raw: bytes) -> str:
    """The textual principal for `raw`: base32(CRC32 || raw), grouped in fives.

    Lowercase and unpadded, which is the canonical form and the only form the
    IC's own tools emit. Reproduces `aaaaa-aa` for b"" and `2vxsx-fae` for
    b"\\x04".
    """
    if len(raw) > MAX_PRINCIPAL_BYTES:
        raise PrincipalRefused(
            f"a principal is at most {MAX_PRINCIPAL_BYTES} bytes and this is {len(raw)}. "
            f"Refused rather than encoded: the result would be a well-formed string that "
            f"no replica will accept, which is worse than a refusal because it looks like "
            f"an address."
        )
    checksum = zlib.crc32(raw).to_bytes(_CRC_BYTES, "big")
    encoded = base64.b32encode(checksum + raw).decode("ascii").lower().rstrip("=")
    return "-".join(encoded[i : i + 5] for i in range(0, len(encoded), 5))


def principal_to_bytes(text: str) -> bytes:
    """The bytes behind a textual principal, with the checksum CHECKED.

    THE CHECKSUM IS THE WHOLE POINT and it is why this is not three lines of
    base32 at a call site. A principal carries a CRC32 of its own body, so a
    mistyped or truncated one is detectable here, at the edge, rather than
    arriving at the ledger as a valid-looking account identifier for an account
    nobody controls -- which on a payout path means funds sent somewhere
    unrecoverable.

    Verified by re-encoding rather than by comparing the CRC alone: that also
    catches a string whose grouping, case or padding differs from the canonical
    form, so `principal_to_bytes` only accepts what `principal_to_text` emits.
    """
    if not text:
        raise PrincipalRefused("the empty string is not a principal")
    compact = text.replace("-", "").upper()
    if not compact.isalnum():
        raise PrincipalRefused(
            f"{text!r} contains characters that are neither base32 nor the group separator"
        )
    try:
        raw = base64.b32decode(compact + "=" * (-len(compact) % 8))
    except ValueError as error:
        # NARROW, AND MEASURED RATHER THAN ASSUMED. base64.b32decode raises
        # binascii.Error for a non-base32 digit and a bare ValueError for a
        # non-ASCII character, and binascii.Error IS a ValueError subclass
        # (checked: issubclass(binascii.Error, ValueError) is True on 3.11), so
        # this one clause covers both without importing binascii for a name whose
        # inheritance is the thing that matters. Nothing is swallowed: the handler
        # re-raises, carrying the input, so no caller can mistake a decode failure
        # for an answer -- which is the test rule 12's BLE001 note sets.
        raise PrincipalRefused(f"{text!r} is not base32: {error}") from error
    if len(raw) < _CRC_BYTES:
        raise PrincipalRefused(
            f"{text!r} decodes to {len(raw)} bytes, which is shorter than the "
            f"{_CRC_BYTES}-byte checksum every principal starts with"
        )
    body = raw[_CRC_BYTES:]
    if principal_to_text(body) != text:
        raise PrincipalRefused(
            f"{text!r} is not a valid principal: its checksum does not match its body. "
            f"This is the mistyped-or-truncated case, and it is refused here rather than "
            f"hashed into an account identifier that would look perfectly well-formed."
        )
    return body


def is_principal(text: str) -> bool:
    """Whether `text` is a canonical textual principal. Never raises.

    For the places that need a yes/no -- a form field, a display branch -- and
    must not have to wrap a try/except to get one. Anything that then needs the
    bytes calls principal_to_bytes and lets it refuse.
    """
    try:
        principal_to_bytes(text)
    except PrincipalRefused:
        return False
    return True


def subaccount_from_index(index: int) -> bytes:
    """The 32-byte subaccount for a non-negative integer, big-endian.

    WHY AN INTEGER AND NOT A HASH OF THE SWAP ID. A counter allocated in SQL can
    be UNIQUE by constraint; a hash is unique by argument. services/
    xrp_tag_service.py already made this choice for destination tags, with a
    uniqueness constraint in the table and an allocating INSERT ... RETURNING, and
    an ICP subaccount is the same problem with a wider field -- so it gets the
    same answer rather than a second one (rule 8). This function is only the
    encoding half; nothing here allocates, and the allocation belongs in SQL
    beside the swap row so a crash cannot leave a deposit instruction shown to a
    customer and not recorded.

    BIG-ENDIAN, which matters for a reason that is not aesthetics: it is what
    ic-py's AccountIdentifier.new does with its integer sub_account, so a
    subaccount this function produces and one produced by that reference
    implementation for the same index are the same 32 bytes.
    """
    if index < 0:
        raise SubaccountRefused(
            f"a subaccount index is non-negative and this is {index}. Refused rather than "
            f"wrapped: a negative index has no two's-complement reading here that any other "
            f"implementation would agree with."
        )
    try:
        return index.to_bytes(SUBACCOUNT_BYTES, "big")
    except OverflowError as error:
        raise SubaccountRefused(
            f"index {index} does not fit in {SUBACCOUNT_BYTES} bytes, so there is no "
            f"subaccount for it"
        ) from error


def normalize_subaccount(subaccount: bytes | None) -> bytes:
    """Exactly 32 bytes, left-padded with zeros, or DEFAULT_SUBACCOUNT for None.

    LEFT-padded, matching the integer encoding above, so that
    normalize_subaccount(b"\\x01") and subaccount_from_index(1) are the same
    account. A right-padding implementation would be self-consistent and would
    name a different account than every other tool, which is the worst available
    outcome: no error, wrong destination.
    """
    if subaccount is None:
        return DEFAULT_SUBACCOUNT
    if len(subaccount) > SUBACCOUNT_BYTES:
        raise SubaccountRefused(
            f"a subaccount is at most {SUBACCOUNT_BYTES} bytes and this is {len(subaccount)}. "
            f"Refused rather than truncated: truncation would silently name a different "
            f"account than the caller asked for."
        )
    return subaccount.rjust(SUBACCOUNT_BYTES, b"\x00")


def account_identifier(principal: str, subaccount: bytes | None = None) -> str:
    """The 64-hex ICP ledger account identifier for (principal, subaccount).

    This is THE decision this module exists for: the string a customer's wallet
    is given as a deposit destination, and the string the ledger is handed as a
    payout destination.

    No `0x` prefix. ic-py's `AccountIdentifier.to_str` adds one and the ICP
    ledger's own candid takes a bare 64-character hex blob, as does
    `dfx ledger account-id`'s output. Emitting the bare form means a value from
    here can be pasted into a dfx command or a candid argument unedited; a caller
    that wants the prefix can add one, and a caller that wants to compare against
    dfx would otherwise have to strip it.
    """
    owner = principal_to_bytes(principal)
    digest = hashlib.sha224(_ACCOUNT_DOMAIN + owner + normalize_subaccount(subaccount)).digest()
    checksum = zlib.crc32(digest).to_bytes(_CRC_BYTES, "big")
    return (checksum + digest).hex()


def is_account_identifier(text: str) -> bool:
    """Whether `text` is a 64-hex account identifier whose checksum checks out.

    Never raises. THE CHECKSUM IS CHECKED, not merely the length and alphabet,
    because the length-and-alphabet test passes for any 64 hex characters -- which
    is what a truncated copy-paste produces, and what a payout would then be sent
    to. CRC32 makes a corrupted identifier detectable without the ledger.
    """
    if len(text) != (_CRC_BYTES + hashlib.sha224().digest_size) * 2:
        return False
    try:
        raw = bytes.fromhex(text)
    except ValueError:
        return False
    return raw[:_CRC_BYTES] == zlib.crc32(raw[_CRC_BYTES:]).to_bytes(_CRC_BYTES, "big")
