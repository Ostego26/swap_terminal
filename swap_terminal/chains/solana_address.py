"""Solana addresses: what one is, whether it can hold SOL, and where its token account lives.

Role: function level (the bottom of CLAUDE.md rule 10's stack -- every name
      here is a pure function of its arguments)
Reads: nothing. No socket, no file, no environment, no clock.
Writes: nothing
Can move funds: no. It DERIVES addresses that a payout would be sent to, which
      makes it fund-ADJACENT in the way rule 16 means: a wrong derivation here
      sends tokens somewhere nobody owns. Nothing in this file signs or
      broadcasts, and the derivations below are cross-checked -- see "HOW THIS
      WAS VERIFIED".
Mainnet-safe: yes. It cannot reach a chain even if asked to.

WHY THIS IS NOT chains/base.py's validate_address().

RPCAdapter asks a wallet daemon `validateaddress` and RAISES when the daemon
cannot be reached, because on that path a transport failure and a malformed
address produce the same False and the operator goes and checks the customer's
address during an outage. That whole problem does not exist here: a Solana
address is a self-describing 32-byte ed25519 public key in base58, and deciding
whether a string is one needs no network at all. So this answers locally,
always, and can never confuse "the chain is down" with "that address is wrong".

THREE DIFFERENT QUESTIONS, AND THEY ARE NOT THE SAME QUESTION.

  is_valid_address()     is this string a 32-byte base58 public key? Syntax.
  is_on_curve()          is that key a point on ed25519 -- i.e. could anybody
                         hold a private key for it? A Program Derived Address
                         is a valid 32-byte key that is deliberately NOT on the
                         curve, so no private key exists and nothing can ever
                         sign for it.
  associated_token_address()   given a wallet and an SPL mint, where does that
                         wallet's token balance actually live?

The middle one is why they are separate, and it is a money question rather than
a taxonomy one. An Associated Token Account is a PDA. It is a perfectly valid
address, it is what an SPL balance sits in, and it is EXACTLY THE WRONG THING
to accept as a payout address for native SOL: a customer who pastes their USDC
account address instead of their wallet address gives you a string that passes
every syntactic check. Sending to it is not an error the chain refuses.

HOW THIS WAS VERIFIED (CLAUDE.md rule 17: run the thing that would show it
false). Both derivations below are hand-written rather than taken from a
dependency, so neither is trustworthy on the strength of looking right:

  is_on_curve()               agreed with solders' Pubkey.is_on_curve() on 7
                              named vectors (a real wallet key, the System,
                              Token, Token-2022, Associated-Token and Rent
                              addresses, and a derived ATA) and on 4,000
                              random 32-byte values. 0 mismatches.
  associated_token_address()  agreed with solders' Pubkey.find_program_address()
                              on 300 random (owner, mint, token-program)
                              triples across both the Token and Token-2022
                              programs. 0 mismatches.

That cross-check ran in a throwaway virtualenv and solders is deliberately NOT
a dependency of this repository: the reference was used to establish the test
vectors, and tests/test_solana_address.py pins them from here on. Adding a
compiled Rust extension to a host that holds wallet credentials, to compute two
hashes and a Legendre symbol, is a worse trade than 40 lines that are tested.

WHY THE ARITHMETIC IS SPELLED OUT RATHER THAN IMPORTED.

ed25519 is the twisted Edwards curve -x^2 + y^2 = 1 + d*x^2*y^2 over the prime
field p = 2^255 - 19, with d = -121665/121666. A compressed point is 32 bytes
little-endian: the low 255 bits are y, the top bit is the sign of x. A key is
ON the curve when the x^2 implied by that y is a quadratic residue mod p --
which is Euler's criterion, one modular exponentiation, and is what the code
below computes. It does not recover x, because nothing here needs x.
"""

from __future__ import annotations

import hashlib

import base58

# The ed25519 field prime, 2^255 - 19.
FIELD_PRIME = 2**255 - 19

# The curve constant d = -121665/121666 mod p, computed rather than written as
# a 77-digit literal: a literal is a thing that can be mistyped and a thing no
# reader can check, and this is evaluated once at import.
CURVE_D = (-121665 * pow(121666, FIELD_PRIME - 2, FIELD_PRIME)) % FIELD_PRIME

# A Solana public key is exactly 32 bytes. Not "at least", not "about" --
# base58 has no fixed output length, so a string can decode to 31 or 33 bytes
# and still look like an address to a human.
PUBKEY_BYTES = 32

# The domain separator Solana appends when hashing a Program Derived Address,
# so that a PDA can never collide with a hash computed for another purpose.
PDA_MARKER = b"ProgramDerivedAddress"

# The three program addresses the SPL token path needs. These are network
# constants -- the same on mainnet, devnet, testnet and a local validator --
# and they are the identifiers the external system uses, so they are spelled
# exactly as Solana spells them (CLAUDE.md rule 18: nothing at all in what a
# machine parses).
#
# The three S105 suppressions are a checked claim, not a way to quiet a finding
# (rule 19). ruff flags any assignment whose NAME contains "token" and whose
# value is a long string literal. These are public, published, network-wide
# program addresses -- every Solana client on earth ships the same three
# literals -- and none of them is a credential. The thing S105 exists to catch,
# a secret pasted into source, is covered here by
# tests/test_no_key_material_is_tracked.py, which matches on shape.
TOKEN_PROGRAM_ID = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"  # noqa: S105
TOKEN_2022_PROGRAM_ID = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"  # noqa: S105
ASSOCIATED_TOKEN_PROGRAM_ID = "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL"  # noqa: S105

# Native SOL wrapped as an SPL mint. Named here because it is the one mint that
# is ALSO the native asset, and confusing the two is a real way to read a
# balance of zero from an account that holds nine figures of lamports.
WRAPPED_SOL_MINT = "So11111111111111111111111111111111111111112"


class SolanaAddressError(ValueError):
    """A string was not a usable Solana address, and the message says why.

    A ValueError subclass rather than chains/base.RPCError: nothing here talks
    to an RPC, so a failure is always a statement about the INPUT and never
    about reachability. That distinction is the one rule 12's BLE001 note is
    about, and here it is structural rather than a matter of care.
    """


def decode_address(address: str) -> bytes:
    """Return the 32 raw bytes of a Solana address, or raise SolanaAddressError.

    Raises rather than returning None because every caller here needs the bytes
    to continue; a None would be checked in four places or, worse, in three.
    """
    if not isinstance(address, str) or not address.strip():
        raise SolanaAddressError("empty or non-string Solana address")
    try:
        raw = base58.b58decode(address.strip())
    # Broad on purpose, and it is NOT rule 12's BLE001: base58 raises several
    # unrelated types for malformed input across its versions, every one of
    # them means the same thing ("this string is not base58"), and the failure
    # is RE-RAISED with its cause attached rather than swallowed. No caller can
    # mistake this for an answer about a valid address. ruff agrees -- BLE001
    # does not fire on a handler that re-raises, so there is no suppression
    # here to be an unread claim (rule 19).
    except Exception as exc:
        raise SolanaAddressError(f"not valid base58: {address!r} ({exc})") from exc
    if len(raw) != PUBKEY_BYTES:
        raise SolanaAddressError(
            f"a Solana address is {PUBKEY_BYTES} bytes; {address!r} decodes to {len(raw)}. "
            "Base58 has no fixed length, so a truncated or extended key still looks like an address."
        )
    return raw


def is_valid_address(address: str) -> bool:
    """True if `address` is syntactically a Solana public key.

    SYNTAX ONLY, and the name of the next function is the rest of the story.
    This says nothing about whether the account exists, whether it is funded,
    whether it is rent-exempt, or whether anyone can sign for it.
    """
    try:
        decode_address(address)
    except SolanaAddressError:
        return False
    return True


def is_on_curve(address: str) -> bool:
    """True if a private key could exist for this address.

    False means the address is a Program Derived Address: a valid 32-byte key
    chosen specifically to be OFF the curve so that no private key exists for
    it. Associated Token Accounts are PDAs, which is why this matters on the
    payout path -- see the module docstring.

    Raises SolanaAddressError for a string that is not an address at all,
    rather than returning False. "Not an address" and "an address nobody can
    sign for" are different answers and a caller that cannot tell them apart
    will eventually report the wrong one to a customer.
    """
    return _raw_is_on_curve(decode_address(address))


def _raw_is_on_curve(raw: bytes) -> bool:
    """is_on_curve() for the 32 raw bytes, with no base58 round trip.

    Split out for one measured reason (CLAUDE.md rule 3: prefer removing work
    to doing it faster). find_program_address() calls this up to 256 times per
    derivation; routing each candidate through base58 encode-then-decode would
    do 512 base-58 conversions to answer a question about bytes it already
    holds. One implementation, two entry points -- the alternative is a second
    copy of the field arithmetic, which is rule 8's bug with a delay on it.
    """
    # Low 255 bits are y; the top bit is the sign of x and is not needed to
    # decide membership.
    y = int.from_bytes(raw, "little") & ((1 << 255) - 1)
    if y >= FIELD_PRIME:
        # A non-canonical encoding: y is not reduced mod p. curve25519-dalek
        # rejects these, so this matches what the chain itself would do rather
        # than what the arithmetic would tolerate.
        return False
    y_squared = (y * y) % FIELD_PRIME
    numerator = (y_squared - 1) % FIELD_PRIME
    denominator = (CURVE_D * y_squared + 1) % FIELD_PRIME
    if denominator == 0:
        return False
    x_squared = (numerator * pow(denominator, FIELD_PRIME - 2, FIELD_PRIME)) % FIELD_PRIME
    if x_squared == 0:
        # x = 0 is a genuine root, and the exponentiation below would report 0
        # rather than 1 for it. Handled explicitly because Euler's criterion is
        # a statement about non-zero residues.
        return True
    # Euler's criterion: a non-zero value is a square mod p exactly when
    # v^((p-1)/2) == 1. If x^2 has no square root, no point with this y exists.
    return pow(x_squared, (FIELD_PRIME - 1) // 2, FIELD_PRIME) == 1


def create_program_address(seeds: list[bytes], program_id: bytes) -> bytes | None:
    """One PDA candidate, or None when the candidate landed ON the curve.

    Returning None is not a failure: it is the answer "this seed set does not
    produce a program address", and find_program_address() below is the loop
    that tries the next bump. Solana's own create_program_address has exactly
    this shape.
    """
    digest = hashlib.sha256()
    for seed in seeds:
        digest.update(seed)
    digest.update(program_id)
    digest.update(PDA_MARKER)
    candidate = digest.digest()
    return None if _raw_is_on_curve(candidate) else candidate


def find_program_address(seeds: list[bytes], program_id: bytes) -> tuple[bytes, int]:
    """The canonical PDA for these seeds, with the bump seed that produced it.

    Counts DOWN from 255, which is not an implementation detail: "the canonical
    bump" is defined as the largest one that yields an off-curve address, and a
    loop that counted up would find a different, equally valid PDA that no
    other Solana client would agree with.
    """
    for bump in range(255, -1, -1):
        candidate = create_program_address([*seeds, bytes([bump])], program_id)
        if candidate is not None:
            return candidate, bump
    raise SolanaAddressError("no bump seed produced an off-curve address; this is cryptographically improbable")


def associated_token_address(owner: str, mint: str, token_program_id: str = TOKEN_PROGRAM_ID) -> str:
    """Where `owner`'s balance of `mint` lives: the Associated Token Account.

    THE SEED ORDER IS PART OF THE PROTOCOL and is [owner, token_program, mint]
    -- note that the TOKEN program goes in the middle while the ASSOCIATED
    TOKEN program is the deriving program. Reordering them produces a valid
    off-curve address that no wallet, explorer or token program will ever look
    at, and a transfer to it is not refused.

    `token_program_id` is a parameter rather than a constant because Token-2022
    mints derive a DIFFERENT ATA for the same owner and mint. Defaulting to the
    original program is right for wGRC and for every mint created before 2022,
    and getting it wrong is silent: both answers are well-formed addresses.
    Which program a mint belongs to is readable from the chain -- it is the
    `owner` field of the mint account -- and SolanaAdapter.token_program_for_mint()
    is what asks.
    """
    derived, _bump = find_program_address(
        [decode_address(owner), decode_address(token_program_id), decode_address(mint)],
        decode_address(ASSOCIATED_TOKEN_PROGRAM_ID),
    )
    return base58.b58encode(derived).decode("ascii")


def describe_address(address: str) -> str:
    """One self-describing line about an address, for an operator to read.

    CLAUDE.md rule 14: "state what the number means, next to the number." An
    operator pasting an address into a diagnostic wants to know which of the
    three questions above it fails, not a bare True/False.
    """
    if not is_valid_address(address):
        try:
            decode_address(address)
        except SolanaAddressError as exc:
            return f"{address!r}  INVALID  <- {exc}"
    if is_on_curve(address):
        return f"{address}  valid, on-curve  <- a wallet; a private key can exist for it"
    return (
        f"{address}  valid, OFF-CURVE  <- a Program Derived Address (an Associated Token Account "
        "is one). No private key exists for it, and native SOL sent here is unrecoverable by its "
        "apparent owner."
    )
