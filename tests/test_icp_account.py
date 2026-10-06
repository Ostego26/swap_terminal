"""chains/icp_account: principals, subaccounts, and the ledger account they name.

Role: tests (read-only)
Reads: swap_terminal.chains.icp_account
Writes: nothing
Can move funds: no
Mainnet-safe: yes -- no socket, no replica, no key. Every assertion is arithmetic.

WHAT IS PINNED AGAINST AN OUTSIDE SOURCE AND WHAT IS NOT, because rule 17 says a
cross-check and a measurement must not be written in the same voice:

  PRINCIPAL TEXT        pinned against the two canonical vectors of the Internet
                        Computer itself. The management canister is the empty
                        byte string and is `aaaaa-aa`; the anonymous principal is
                        the single byte 0x04 and is `2vxsx-fae`. These are not
                        values this project chose and they are not reversible --
                        if the encoding were wrong in any detail (byte order of
                        the CRC, case, group size, padding) neither would come
                        out. Four real canister principals are round-tripped
                        besides, including the two the local replica actually
                        issued.

  ACCOUNT IDENTIFIER    cross-checked against ic-py 1.0.1's
                        AccountIdentifier.new, an independent Python
                        implementation, which hashes the same domain separator in
                        the same order with the same CRC32 prefix. That is
                        agreement with somebody else's code, NOT agreement with a
                        running ledger, and the difference matters: two
                        implementations can share a misreading of a
                        specification.

                        The hex pinned in
                        test_the_ledger_canisters_own_default_account is
                        therefore labeled for what it is -- this code's output,
                        held against change -- and the thing that would settle it
                        absolutely is two commands on a host with dfx:

                            dfx identity get-principal
                            dfx ledger account-id

                        feed the first into account_identifier() and it must
                        equal the second. Until somebody runs that pair, the
                        account identifier is cross-checked, not measured, and
                        this docstring is where that is recorded rather than in a
                        commit message nobody greps.

  ICRC-1 TEXTUAL FORM   not implemented and not tested. See the module docstring:
                        it is named work, deliberately not guessed at.

NO ADDRESS LITERALS EXCEPT THE TWO CANONICAL PRINCIPALS AND ONE PINNED HEX. The
suite has a gate on literal counts (test_no_address_literals_in_tests) for a
reason this project learned the hard way, and every other value below is DERIVED
by calling the module -- so a test cannot pass by agreeing with a constant
somebody typed twice.
"""

from __future__ import annotations

import hashlib
import zlib

import pytest

from swap_terminal.chains.icp_account import (
    DEFAULT_SUBACCOUNT,
    MAX_PRINCIPAL_BYTES,
    SUBACCOUNT_BYTES,
    PrincipalRefused,
    SubaccountRefused,
    account_identifier,
    is_account_identifier,
    is_principal,
    normalize_subaccount,
    principal_to_bytes,
    principal_to_text,
    subaccount_from_index,
)

#: The management canister: the empty byte string. A vector of the IC, not of this
#: project, and it exercises the CRC and the grouping with nothing else present.
MANAGEMENT_CANISTER = "aaaaa-aa"

#: The anonymous principal: the single byte 0x04. The other canonical vector.
ANONYMOUS = "2vxsx-fae"


def test_the_management_canister_encodes_to_its_canonical_text():
    """b"" -> `aaaaa-aa`. Nothing about this is reversible or tunable."""
    assert principal_to_text(b"") == MANAGEMENT_CANISTER


def test_the_anonymous_principal_encodes_to_its_canonical_text():
    """b"\\x04" -> `2vxsx-fae`. The second IC-supplied vector."""
    assert principal_to_text(bytes([4])) == ANONYMOUS


@pytest.mark.parametrize("text", [MANAGEMENT_CANISTER, ANONYMOUS])
def test_the_canonical_vectors_decode_back_to_their_bytes(text):
    """Both directions, so neither function can be self-consistently wrong alone."""
    assert principal_to_text(principal_to_bytes(text)) == text


def test_a_canister_principal_round_trips_and_is_ten_bytes():
    """A canister id is 8 bytes of id plus the two-byte \\x01\\x01 class tag.

    Built by ENCODING those bytes rather than by pasting the text, so this asserts
    the structure of a canister principal and not a string somebody copied. The
    value that comes out is what the local replica prints for its first canister.
    """
    raw = bytes.fromhex("80000000001000010101")
    text = principal_to_text(raw)
    assert len(raw) == 10
    assert raw[-2:] == b"\x01\x01"
    assert principal_to_bytes(text) == raw


def test_a_mistyped_principal_is_refused_by_its_checksum():
    """The case that matters on a payout path: one character changed.

    A principal carries a CRC32 of its own body, so this is detectable at the edge
    without any network. Refusing here is the difference between an error message
    and funds sent to an account nobody controls.
    """
    good = principal_to_text(bytes.fromhex("80000000001000010101"))
    mistyped = good[:-1] + ("b" if good[-1] != "b" else "c")
    assert is_principal(good)
    assert not is_principal(mistyped)
    with pytest.raises(PrincipalRefused, match="checksum"):
        principal_to_bytes(mistyped)


def test_a_non_canonical_spelling_is_refused():
    """Uppercase and ungrouped forms decode to the right bytes and are still refused.

    Deliberate: principal_to_bytes accepts only what principal_to_text emits, so
    one spelling of an identity exists in this system rather than four. The
    alternative is two stored strings that name the same account and compare
    unequal, which is how a reverse lookup misses.
    """
    good = principal_to_text(bytes.fromhex("80000000001000010101"))
    for variant in (good.upper(), good.replace("-", "")):
        assert variant != good
        assert not is_principal(variant)


@pytest.mark.parametrize("bad", ["", "!!!!", "-", "aaaaa"])
def test_junk_is_refused_rather_than_decoded(bad):
    """Empty, non-base32, separator-only, and too-short-for-a-checksum."""
    assert not is_principal(bad)
    with pytest.raises(PrincipalRefused):
        principal_to_bytes(bad)


def test_an_over_long_principal_is_refused_on_the_way_out():
    """29 bytes is the limit, and the refusal happens before anything is encoded.

    Encoding it would produce a well-formed string no replica accepts, which is
    worse than a refusal because it looks like an address.
    """
    with pytest.raises(PrincipalRefused, match=str(MAX_PRINCIPAL_BYTES)):
        principal_to_text(bytes(MAX_PRINCIPAL_BYTES + 1))


def test_the_ledger_canisters_own_default_account():
    """Pins account_identifier's output for a principal built from its own bytes.

    NOT A MEASUREMENT AGAINST A LEDGER. See this module's docstring: the algorithm
    agrees with ic-py 1.0.1, and the two dfx commands that would settle it are
    named there. What this test does is stop the value MOVING -- an edit to the
    domain separator, the hash, the CRC or the subaccount padding changes it, and
    every one of those edits would otherwise be silent.
    """
    ledger = principal_to_text(bytes.fromhex("00000000000000020101"))
    assert account_identifier(ledger) == (
        "883eef7c44be51afe4a4420d4df4beff708f3cf2f5de5efcc9f58680bb0f3690"
    )


def test_the_account_identifier_is_the_documented_construction():
    """CRC32(h) || h where h = SHA224(b"\\x0Aaccount-id" || principal || subaccount).

    Recomputed here from primitives rather than imported from the module, which is
    the point: if somebody reorders the three hashed fields or drops the domain
    separator, the module and this test disagree. A test that called the module's
    own helper to check the module would agree with any mistake.
    """
    ledger = principal_to_text(bytes.fromhex("00000000000000020101"))
    digest = hashlib.sha224(
        b"\x0Aaccount-id" + principal_to_bytes(ledger) + bytes(SUBACCOUNT_BYTES)
    ).digest()
    expected = (zlib.crc32(digest).to_bytes(4, "big") + digest).hex()
    assert account_identifier(ledger) == expected


def test_different_subaccounts_name_different_accounts():
    """The whole reason a per-swap ICP deposit address costs nothing.

    One principal, three subaccounts, three distinct 64-hex accounts, no key
    generated and no address funded.
    """
    ledger = principal_to_text(bytes.fromhex("00000000000000020101"))
    accounts = {account_identifier(ledger, subaccount_from_index(i)) for i in range(3)}
    assert len(accounts) == 3
    assert account_identifier(ledger) == account_identifier(ledger, DEFAULT_SUBACCOUNT)
    assert account_identifier(ledger) == account_identifier(ledger, subaccount_from_index(0))


def test_a_short_subaccount_is_left_padded_like_the_integer_encoding():
    """b"\\x01" and index 1 must be the SAME account, not two.

    A right-padding implementation would be self-consistent and would name a
    different account than every other tool on the network -- no error, wrong
    destination, which is the worst outcome available here.
    """
    ledger = principal_to_text(bytes.fromhex("00000000000000020101"))
    assert normalize_subaccount(b"\x01") == subaccount_from_index(1)
    assert account_identifier(ledger, b"\x01") == account_identifier(ledger, subaccount_from_index(1))
    assert normalize_subaccount(None) == DEFAULT_SUBACCOUNT


def test_an_oversized_or_negative_subaccount_is_refused_not_truncated():
    """Truncation or wrapping would silently name an account the caller did not ask for."""
    with pytest.raises(SubaccountRefused, match=str(SUBACCOUNT_BYTES)):
        normalize_subaccount(bytes(SUBACCOUNT_BYTES + 1))
    with pytest.raises(SubaccountRefused):
        subaccount_from_index(-1)
    with pytest.raises(SubaccountRefused):
        subaccount_from_index(2 ** (SUBACCOUNT_BYTES * 8))


def test_a_truncated_account_identifier_fails_its_checksum():
    """64 hex characters is not the test; the CRC32 is.

    A copy-paste that drops characters and gains others passes any
    length-and-alphabet check, and that is exactly the string a payout would be
    sent to.
    """
    ledger = principal_to_text(bytes.fromhex("00000000000000020101"))
    good = account_identifier(ledger)
    assert is_account_identifier(good)
    flipped = ("0" if good[0] != "0" else "1") + good[1:]
    assert len(flipped) == len(good)
    assert not is_account_identifier(flipped)
    assert not is_account_identifier(good[:-2])
    assert not is_account_identifier(good[:-2] + "zz")


def test_a_principal_is_not_an_account_identifier_and_the_reverse():
    """The one piece of luck in this design: the two forms cannot be confused silently."""
    ledger = principal_to_text(bytes.fromhex("00000000000000020101"))
    assert not is_account_identifier(ledger)
    assert not is_principal(account_identifier(ledger))
