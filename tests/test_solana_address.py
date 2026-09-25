"""Solana address decisions, against vectors cross-checked with an independent implementation.

Role: test (pure functions; no chain, no socket)
Reads: swap_terminal/chains/solana_address.py
Writes: nothing
Can move funds: no
Mainnet-safe: yes

WHERE THESE VECTORS CAME FROM, BECAUSE THAT IS THE WHOLE VALUE OF THE FILE.

chains/solana_address.py hand-writes two pieces of cryptography -- ed25519
curve membership and Solana's program-derived-address loop -- rather than
importing them, and hand-written crypto that merely looks right is worth
nothing. Both were cross-checked on 2026-09-25 against `solders`, the Rust
implementation the Solana Python ecosystem uses, in a throwaway virtualenv:

    is_on_curve()               7 named vectors + 4,000 random 32-byte values
                                0 mismatches
    associated_token_address()  300 random (owner, mint, token-program) triples
                                across the Token and Token-2022 programs
                                0 mismatches

solders is deliberately NOT a dependency of this repository -- see the module
docstring for why a compiled Rust extension is a bad trade on a host that holds
wallet credentials. The reference established these vectors; these tests hold
them from here on, so a future edit to the field arithmetic fails here rather
than on a cluster.

The off-curve vector below is a REAL associated token account, derived by
solders for a real wallet and the wrapped-SOL mint. It is the thing the code is
about, not a value chosen to make a test pass.
"""

from __future__ import annotations

import base58
import pytest
from chains.solana_address import (
    ASSOCIATED_TOKEN_PROGRAM_ID,
    TOKEN_2022_PROGRAM_ID,
    TOKEN_PROGRAM_ID,
    WRAPPED_SOL_MINT,
    SolanaAddressError,
    associated_token_address,
    decode_address,
    describe_address,
    is_on_curve,
    is_valid_address,
)

# A real wallet public key. It appears in tests/test_no_key_material_is_tracked.py
# as the public half of the keypair that was committed and has been untracked;
# it is a PUBLIC key and publishing it discloses nothing.
WALLET = "BGdUSPGWiwStabibSXwiLwJCsk6iXDTeyL6fgWbNcAvN"

# WALLET's associated token account for the wrapped-SOL mint, derived by
# solders. Off-curve by construction: it is a PDA.
WALLET_WSOL_ATA = "2TJwPdpDwgGrcQjW5K5E2uNxEqBTNkQsZ46axFy5bNm3"


def test_a_real_wallet_decodes_to_thirty_two_bytes():
    assert len(decode_address(WALLET)) == 32


@pytest.mark.parametrize(
    "address",
    [WALLET, "11111111111111111111111111111111", TOKEN_PROGRAM_ID, TOKEN_2022_PROGRAM_ID,
     ASSOCIATED_TOKEN_PROGRAM_ID, WRAPPED_SOL_MINT, "SysvarRent111111111111111111111111111111111"],
)
def test_the_named_on_curve_vectors_are_on_curve(address):
    """All seven agreed with solders. The program IDs being on-curve is not a
    typo: Solana's well-known program addresses were vanity-ground from real
    keypairs, so they sit on the curve even though nothing signs with them."""
    assert is_valid_address(address)
    assert is_on_curve(address)


def test_a_real_associated_token_account_is_off_curve():
    """The vector that matters. An ATA is a PDA, so no private key exists for
    it -- which is why validate_address() refuses one as a payout destination."""
    assert is_valid_address(WALLET_WSOL_ATA)
    assert not is_on_curve(WALLET_WSOL_ATA)


def test_the_ata_derivation_reproduces_the_reference_value():
    """Seed order is [owner, token_program, mint] and getting it wrong yields a
    different, equally well-formed address that holds nothing."""
    assert associated_token_address(WALLET, WRAPPED_SOL_MINT) == WALLET_WSOL_ATA


def test_token_2022_derives_a_different_account_for_the_same_owner_and_mint():
    """Both are valid addresses and only one holds the balance, which is why
    chains/solana.py asks the mint account which program owns it rather than
    defaulting silently."""
    original = associated_token_address(WALLET, WRAPPED_SOL_MINT, TOKEN_PROGRAM_ID)
    token_2022 = associated_token_address(WALLET, WRAPPED_SOL_MINT, TOKEN_2022_PROGRAM_ID)
    assert original != token_2022
    assert not is_on_curve(token_2022)


@pytest.mark.parametrize("bad", ["", "   ", "nope!!!", "0OIl", "l" * 10])
def test_malformed_strings_are_not_valid_addresses(bad):
    assert not is_valid_address(bad)


def test_a_thirty_one_byte_key_is_refused_even_though_it_is_base58():
    """Base58 has no fixed output length, so a truncated key still looks like an
    address to a human. This is the check a length-blind validator misses."""
    short = base58.b58encode(b"\x01" * 31).decode("ascii")
    assert not is_valid_address(short)
    with pytest.raises(SolanaAddressError) as exc:
        decode_address(short)
    assert "31" in str(exc.value)


def test_is_on_curve_raises_rather_than_returning_false_for_a_non_address():
    """'Not an address' and 'an address nobody can sign for' are different
    answers, and a caller that cannot tell them apart reports the wrong one."""
    with pytest.raises(SolanaAddressError):
        is_on_curve("definitely not base58 !!!")


def test_describe_address_says_which_of_the_three_things_it_is():
    assert "on-curve" in describe_address(WALLET)
    assert "OFF-CURVE" in describe_address(WALLET_WSOL_ATA)
    assert "INVALID" in describe_address("!!!")
    # The off-curve line has to say what it COSTS, not just what it is: an
    # operator reading "OFF-CURVE" learns nothing actionable (CLAUDE.md rule 14).
    assert "unrecoverable" in describe_address(WALLET_WSOL_ATA)
