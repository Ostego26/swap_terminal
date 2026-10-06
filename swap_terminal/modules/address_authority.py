"""Which validator answers for which asset -- ONE table, so no caller has to choose.

Role: submodule (one decision per asset; no I/O, no chain, no database)
Reads: nothing. Every input is an argument.
Writes: nothing
Can move funds: no. It is the check that stops something else from moving them into a
       string nobody can spend, which is the opposite.
Mainnet-safe: yes -- it opens no socket and reads no environment.

WHY THIS EXISTS, AND WHY THE OBVIOUS SIMPLER VERSION IS THE WORSE BUG.

Operator, 2026-09-27: "obviously spin up an agent and make this burn proof", and a moment
later "i mean, make the receive path burn proof too".

The defect being fixed is that `modules/address_network.is_valid_address()` was added in
f805efa and NOTHING ON THE FUND PATH CALLED IT. An undecodable payout address reached
`adapter.send_to_address()` and the money was gone -- not stolen, not recoverable,
unspendable by anybody.

The obvious fix is one line at the send site:

    if not is_valid_address(address): refuse

**THAT LINE WOULD HAVE BROKEN TWO WORKING CHAINS**, and saying why is the whole reason this
module is a table instead of a call. `is_valid_address()` understands exactly three
encodings -- bech32, Bitcoin-alphabet base58check, and XRP-alphabet base58check -- and this
repository can reach one more that looks nothing like any of them:

    Solana    plain base58 of a 32-byte ed25519 key with NO CHECKSUM AT ALL.
              chains/solana_address.py owns it. Length is the only structure there is.

So the blanket guard refuses every valid Solana payout. MEASURED 2026-09-29 rather than
asserted: SOL is NOT in Config.ALLOWED_PAIRS today -- the set is BTC, GRC, LTC and XRP --
so this trap is LATENT and not live, and saying otherwise would be the register error rule
17 is about. What makes it worth a table anyway is that enabling a pair is a one-line
change and this refusal would land on a swap whose deposit has ALREADY been taken and
credited. A false refusal there is worse than the burn it was meant to prevent: the burn
costs one customer's payout, and the outage costs every customer of that chain while their
money sits in our wallet.

Measured here 2026-09-27, by calling the real functions on the real fixtures rather than by
reading either module (rule 17). `tests/test_address_authority.py::
test_the_blanket_guard_would_have_refused_solana` is that measurement, kept as a
test so the trap cannot be walked into again by someone who reads only the send site.

WHAT AN ASSET WITH NO VALIDATOR DOES, AND HOW THAT WAS DECIDED.

NO_VALIDATOR is a third state beside VALID and INVALID, and it is neither of them. It is
never silently "valid" and never silently "invalid"; a caller reads the state and decides.

The fund-path callers here all decide the same way: **NO_VALIDATOR PASSES THROUGH, LOUDLY.**
Refusing a chain we cannot check is the false-refusal outage above, arriving through a
different door -- and it is the door a FUTURE chain arrives by, which is exactly when
nobody is watching for it.

That would normally be a hole big enough to drive the original defect through, so it is
closed at the other end: `test_every_asset_this_terminal_can_reach_has_a_validator` asserts
that every asset in `chains/registry` and every asset in `Config.ALLOWED_PAIRS` has an entry
in VALIDATORS. So NO_VALIDATOR is unreachable for every asset that exists today -- measured,
not assumed -- and a new chain added without a validator fails the suite rather than either
burning money or breaking a payout. A runtime pass-through plus a build-time clean gate is
strictly better than either answer alone, which is why neither answer alone was taken.

WHY THE PER-ASSET CHECK IS TIGHTER THAN "DOES IT DECODE".

`is_valid_address()` answers "does this decode as SOMETHING". That is not the question a
payout asks. A perfectly valid Litecoin address handed to a Bitcoin payout is decodable,
well-formed, mainnet, and burns the money just as completely as a typo does. Same for an XRP
classic address in a BTC field: it decodes under XRP's alphabet, so the blanket check says
yes.

So each validator answers "is this a well-formed address in a format THIS chain uses", and
the vocabulary it reads -- BECH32_HRPS_BY_ASSET and BASE58_VERSION_ASSETS -- lives in
modules/address_network.py beside the tables it was derived from, not here (rule 11).

WHAT IT DELIBERATELY DOES NOT CLAIM. BTC, LTC and GRC all declare PUBKEY_ADDRESS 111 and
SCRIPT_ADDRESS 196, so a testnet base58 address CANNOT be attributed to one of the three by
decoding, and this module does not pretend otherwise: BASE58_VERSION_ASSETS maps 0x6F to all
three and the verdict says the encoding cannot narrow it further. Claiming more would refuse
every valid Bitcoin testnet address in this repository.

That table is a SET PER BYTE rather than one chain or None, and the difference is not
bookkeeping: 0x05 is Bitcoin's SCRIPT_ADDRESS and Litecoin's and NOT Gridcoin's, and the
one-or-None shape could only say "shared" -- which this module read as "accept for anything",
so a `3...` Bitcoin address passed as a GRIDCOIN payout destination. Found by mutating the
byte, not by reading the code.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import NamedTuple

from chains import icp_account, solana_address, xrp_address
from modules.address_network import (
    BASE58_VERSION_ASSETS,
    BASE58_VERSIONED_HASH160_LEN,
    BECH32_HRPS_BY_ASSET,
    MAINNET,
    P2PKH_VERSIONS,
    P2SH_VERSIONS,
    TESTNET,
    UNKNOWN,
    base58check_payload,
    bech32_hrp,
    decode_segwit_address,
)
from network_target import classify

# THE THREE STATES. A caller that collapses them to a boolean has thrown away the one that
# matters: chains/base.RPCAdapter.validate_address()'s own docstring records what that cost
# when an outage and a malformed address produced the same False.
VALID = "VALID"
INVALID = "INVALID"
NO_VALIDATOR = "NO_VALIDATOR"

# FOUR STATES, AND THE FOURTH WAS ADDED AFTER A LIVE RUN REFUTED THREE.
#
# UNDETERMINED means: this string IS a well-formed address under some encoding -- a bech32
# checksum that holds, or a base58check payload of the right size -- and this repository's
# tables cannot classify it. It is NOT a refusal.
#
# WHY IT EXISTS, measured 2026-09-27 on the operator's own regtest run rather than reasoned
# about. A BTC->LTC atomic swap against litecoind 0.21.4 produced two addresses this module
# could not place, because two entries were missing from modules/address_network.py:
#
#     rltc1q7u6dnatxpsds4wvq2svx3h64v8s03cf69xg52q   Litecoin REGTEST bech32 -- hrp `rltc`
#                                                    was absent, so it fell past the bech32
#                                                    branch and was tried as base58
#     QYqEyFb1v76QraQ3uo5wVkGtJrC4Rf5vU3             version 0x3A, Litecoin's second P2SH
#                                                    byte, absent from P2SH_VERSIONS
#
# Both entries are now present, so neither of those two strings reaches this state any more.
# **That fix is not the point.** The point is that a MISSING TABLE ENTRY and a MISSING
# VALIDATOR produce the identical outcome -- a false refusal on a real customer payout -- and
# the module header's whole argument is that a false refusal is worse than the burn being
# prevented. Returning INVALID for "a version byte I have never seen" was that failure
# waiting for the third missing entry.
#
# So the rule this state encodes, and it is the honest line: **REFUSE ONLY WHAT IS ACTIVELY
# CONTRADICTED.** Three cases, and only two of them are refusals:
#
#   decodes as NOTHING at all          INVALID.       Nothing on any chain can ever spend it.
#                                                     This is the burn, and it is definite.
#   decodes and names ANOTHER chain    INVALID.       A real address, on the wrong chain. Also
#     or another NETWORK                              definite, and also a burn.
#   decodes and cannot be placed       UNDETERMINED.  "I could not tell" (rule 17), which must
#                                                     never be written in the same voice as
#                                                     "it is wrong".
UNDETERMINED = "UNDETERMINED"

# What `network` carries when the encoding simply does not express one. An XRP classic
# address is the case: the XRP Ledger has no testnet address format -- the network is a
# property of the server you submit to -- which tests/valid_addresses.py already says out
# loud. Reporting UNKNOWN there would invite a caller to treat it as a failed measurement
# rather than an absent question.
NOT_EXPRESSED = "not-expressed"


class AddressVerdict(NamedTuple):
    """(state, why, network). All three, always, because all three get printed.

    `why` is written to be read off a screen by an operator, not parsed (rule 14). It names
    the address, the encoding that was tried, and what failed -- "starts with tb1 but is not
    valid bech32 (checksum or charset)" sends somebody to a different fix than "valid bech32,
    hrp=ltc, which is Litecoin and not Bitcoin", and a bare False sends them nowhere.

    `network` is MAINNET, TESTNET, NOT_EXPRESSED or UNKNOWN. It is
    filled in even when the state is INVALID where the encoding still said something, so a
    refusal can explain itself.
    """

    state: str
    why: str
    network: str

    @property
    def refuses(self) -> bool:
        """True only for INVALID. Neither NO_VALIDATOR nor UNDETERMINED is a refusal.

        A property rather than `state == INVALID` at six call sites, because the whole hazard
        this module exists inside is a caller inventing its own reading of a state it did not
        expect. One place decides what refuses, and every guard reads it.
        """
        return self.state == INVALID

    @property
    def unchecked(self) -> bool:
        """True when the address PASSED but was not actually verified. Rule 14's trigger.

        Two ways to get here and a caller treats them the same way -- proceed, and say out
        loud that nothing was checked:

          NO_VALIDATOR   no validator for this asset at all
          UNDETERMINED   a validator ran and could not place the address

        They are separate STATES because they need different fixes (add a validator; add a
        table entry) and one property because every fund-path caller does the same thing with
        both. A guard that logged only one of the two would be silent for the case the live
        2026-09-27 run actually produced.
        """
        return self.state in (NO_VALIDATOR, UNDETERMINED)


def _bitcoin_family(asset: str, address: str) -> AddressVerdict:
    """The verdict for a Bitcoin-derived chain: BTC, LTC or GRC.

    Three chains, one function, because they differ ONLY in their vocabulary entries and a
    second copy of this logic per chain is rule 8's bug with a delay on it -- the delay being
    however long it takes for somebody to fix a checksum case in one of the three.

    bech32 is decided FIRST, and on the hrp the string CLAIMS rather than on whether it
    decodes. `ltc1...` handed to BTC must be refused as "that is Litecoin", not as "bad
    checksum": the checksum is fine, and telling an operator to check it would send them
    looking for a typo that is not there.

    SPLIT IN TWO because ruff's PLR0911 fired at nine returns, and rule 12 answers a
    complexity finding by extracting the decision rather than by raising the ceiling. The
    split falls where the encodings do, so each half can be called with seeded inputs and
    asserted on directly -- which is rule 10's whole argument for putting a decision at the
    bottom.
    """
    if bech32_hrp(address) is not None:
        return _bitcoin_bech32(asset, address)
    unplaceable = _unknown_hrp_but_valid_bech32(address)
    if unplaceable is not None:
        return unplaceable
    return _bitcoin_base58(asset, address)


def _unknown_hrp_but_valid_bech32(address: str) -> AddressVerdict | None:
    """UNDETERMINED when this is real bech32 under an hrp no table here knows. Else None.

    THE rltc CASE, and the reason this function exists rather than the branch just falling
    through to base58. `rltc1q7u6dnatxpsds4wvq2svx3h64v8s03cf69xg52q` came off
    `litecoin-cli getnewaddress` on 2026-09-27; `rltc` was missing from BECH32_HRPS, so
    bech32_hrp() returned None, the string was tried as base58check, failed, and the verdict
    read "decodes as neither bech32 nor base58check" -- a confident, wrong sentence about a
    perfectly good address that a payout was about to be sent to.

    `rltc` is in the table now. This is what happens to the NEXT one: a bech32 checksum that
    holds is evidence the string is an address, and the honest verdict is "I cannot place it",
    which no caller here treats as a refusal.
    """
    if not isinstance(address, str) or "1" not in address:
        return None
    # decode_segwit_address(), not bech32.bech32_decode(). The library decoder
    # verifies BIP-173's checksum constant only, so it answered None for every bech32m address
    # and this UNDETERMINED net -- the whole point of which is "unrecognized is not refused" --
    # could never catch a taproot address under an unknown hrp either.
    decoded = decode_segwit_address(address.strip())
    if not decoded.ok:
        return None
    hrp = decoded.hrp
    return AddressVerdict(
        UNDETERMINED,
        f"{address!r} is VALID bech32 -- its checksum holds -- under hrp {hrp!r}, which is in no table this "
        f"repository knows. NOT refused: an hrp this module has never seen is a gap in "
        f"modules/address_network.BECH32_HRPS_BY_ASSET, not evidence against the address. Litecoin's "
        f"regtest hrp `rltc` was exactly this gap until 2026-09-27 and every regtest payout address hit it",
        UNKNOWN,
    )


def _bitcoin_bech32(asset: str, address: str) -> AddressVerdict:
    """The bech32 half. Called only for a string that CLAIMS an hrp this repository knows."""
    hrps = BECH32_HRPS_BY_ASSET[asset]
    claimed = bech32_hrp(address)
    if not hrps:
        # GRC. Its entry is an empty table rather than an absent key precisely so this
        # branch can be reached with a definite answer. atomic_grc_client.py's usage
        # example carried `tgrc1qexampleparticipantaddress...` until 2026-09-27, which
        # is a format that has never existed on any Gridcoin network.
        return AddressVerdict(
            INVALID,
            f"{address!r} is bech32 (hrp {claimed!r}) and {asset} has no bech32 format at all -- it is "
            f"base58 only, so nothing on any {asset} network can receive this",
            UNKNOWN,
        )
    if claimed not in hrps:
        return AddressVerdict(
            INVALID,
            f"{address!r} is a valid-shaped bech32 address with hrp {claimed!r}, which belongs to "
            f"{_asset_owning_hrp(claimed)}, not to {asset}. Paying it from a {asset} wallet burns it: the "
            f"address is well-formed, just not on this chain",
            UNKNOWN,
        )
    # THE SITE THAT CAUSED THE OUTAGE. `bech32.bech32_decode()` here refused every Taproot
    # address with the sentence below -- "the checksum or the character set is wrong" -- which
    # is a confident, false statement about a perfectly spendable address, and this module's own
    # header says a false refusal is WORSE than the burn it was written to prevent. Measured
    # consequence before the fix: services/swap_service.py raised ValueError so no swap paying
    # out to bc1p... could be created at all, and services/payout_service.py set an ALREADY
    # CREDITED swap to status='failed' -- terminal, never retried, customer's coin in our wallet.
    #
    # decode_segwit_address() implements BIP-350: the checksum constant is chosen by the witness
    # version, so this now accepts v1+ under 0x2BC830A3 while still refusing a v0 address that
    # carries the bech32m constant (a corruption, not a spelling). Verified against every
    # published BIP-350 vector, valid and invalid, with the decoded programs compared byte for
    # byte against the spec's expected scriptPubKeys.
    decoded = decode_segwit_address(address.strip())
    if not decoded.ok:
        return AddressVerdict(
            INVALID,
            f"{address!r} claims hrp {claimed!r}, which IS a {asset} prefix, but {decoded.why}",
            UNKNOWN,
        )
    return AddressVerdict(
        # THE ENCODING IS NAMED, not assumed to be "bech32". An operator reading a payout log
        # needs to know a P2TR address was recognized AS one -- "valid LTC bech32" beside a
        # ltc1p... address is the kind of near-miss that makes a reader doubt the whole line.
        # decoded.why carries the witness version and program length too (rule 14: state what
        # the value means, next to the value).
        VALID,
        f"{address!r}: valid {asset} {decoded.encoding}, hrp={claimed} ({hrps[claimed]}) -- {decoded.why}",
        hrps[claimed],
    )


def _bitcoin_base58(asset: str, address: str) -> AddressVerdict:
    """The base58check half, under BITCOIN's alphabet only.

    XRP's alphabet is deliberately NOT tried here, and that is the tightening this module
    exists for: modules/address_network.decodes_as_address() tries both, so an XRP classic
    address handed in as a BTC payout passes its check. It is a real address. It is just not
    on this chain, and the money is as gone as if the string had been noise.
    """
    payload = base58check_payload(address)
    if payload is None:
        return AddressVerdict(
            INVALID,
            f"{address!r} decodes as neither bech32 nor base58check under Bitcoin's alphabet, so it is "
            f"not a {asset} address in any form. Nothing can ever spend money sent to it",
            UNKNOWN,
        )
    if len(payload) != BASE58_VERSIONED_HASH160_LEN:
        return AddressVerdict(
            INVALID,
            f"{address!r} is base58check but decodes to {len(payload)} bytes, not "
            f"{BASE58_VERSIONED_HASH160_LEN}. The checksum holds and the payload is the wrong size, which "
            f"is what an address from another encoding entirely looks like",
            UNKNOWN,
        )
    version = payload[0]
    if version not in BASE58_VERSION_ASSETS:
        # UNDETERMINED, NOT INVALID, and this line is the 0x3A incident. Litecoin declares
        # TWO P2SH version bytes per network and only one of them was in the table, so
        # `QYqEyFb1v76QraQ3uo5wVkGtJrC4Rf5vU3` -- the HTLC contract address litecoind itself
        # produced on 2026-09-27 -- was refused as though it were malformed. A 21-byte
        # payload whose base58check checksum holds is an address; an unrecognized version
        # byte is a gap in BASE58_VERSION_ASSETS, not evidence against it.
        return AddressVerdict(
            UNDETERMINED,
            f"{address!r} is valid base58check -- 21 bytes, checksum holds -- with version byte "
            f"{version:#04x}, which is in no table this repository knows. NOT refused: an unrecognized "
            f"version byte is a gap in modules/address_network.BASE58_VERSION_ASSETS, and refusing it is how "
            f"a valid {asset} address gets rejected. Litecoin's 0x3A was exactly this gap until 2026-09-27",
            UNKNOWN,
        )
    # A SET, NOT ONE NAME, and the membership test is the whole check. The table used to map a
    # byte to one chain or to None meaning "shared", and a mutation found what that cost: 0x05
    # is Bitcoin's SCRIPT_ADDRESS and Litecoin's, and NOT Gridcoin's -- a fact the old shape
    # could not express, so a `3...` Bitcoin P2SH address was accepted as a GRIDCOIN payout
    # address. `asset not in chains` says exactly what is known and refuses exactly what is
    # contradicted.
    chains = BASE58_VERSION_ASSETS[version]
    network = _network_of(version)
    if asset not in chains:
        named = " or ".join(sorted(chains))
        return AddressVerdict(
            INVALID,
            f"{address!r} decodes with version byte {version:#04x}, which belongs to {named} and not to "
            f"{asset}. It is a real, well-formed {named} address; sending {asset} to it burns it",
            network,
        )
    # A byte shared by several chains is ACCEPTED for each of them and SAYS SO. Refusing 0x6F
    # would refuse every valid BTC, LTC and GRC testnet address there is.
    shared = "" if len(chains) == 1 else (
        f" -- a version byte {', '.join(sorted(chains))} share, so this encoding cannot narrow it further"
    )
    return AddressVerdict(
        VALID, f"{address!r}: valid base58check, version byte {version:#04x} ({network}){shared}", network
    )


def _network_of(version: int) -> str:
    """MAINNET or TESTNET for a version byte this repository knows, else UNKNOWN.

    Read off modules/address_network's own tables rather than restated here, which is why
    this is one line and not a fourth copy of the version vocabulary (rule 8).
    """
    return P2PKH_VERSIONS.get(version) or P2SH_VERSIONS.get(version) or UNKNOWN


def _asset_owning_hrp(hrp: str) -> str:
    """Which chain an hrp belongs to, for a refusal message. Never raises.

    Its only job is making the sentence "that is Litecoin, not Bitcoin" possible, and a
    refusal that cannot name the other chain is a refusal an operator has to investigate.
    """
    for asset, table in BECH32_HRPS_BY_ASSET.items():
        if hrp in table:
            return asset
    return "no chain this repository knows"


def _xrp(address: str) -> AddressVerdict:
    """The XRP verdict, delegated to chains/xrp_address.py, which owns that encoding.

    An X-address is accepted, matching chains/xrp.XRPAdapter.validate_address() exactly --
    and this module is NOT the place to change that. That adapter's docstring records an
    open review finding about X-address checksums, and `services/swap_service.create_swap()`
    already refuses XRP as a payout destination one check earlier via why_cannot_pay_out().
    Disagreeing here would give one repository two answers for one address (rule 8), and the
    stricter one would arrive without the measurement that should justify it (rule 17).

    NOT_EXPRESSED rather than UNKNOWN for the network: the XRP Ledger has no testnet address
    format. That is an absent question, not a failed measurement.
    """
    raw = address.strip() if isinstance(address, str) else ""
    if xrp_address.looks_like_x_address(raw):
        return AddressVerdict(VALID, f"{raw!r}: XRP X-address (carries its own destination tag)", NOT_EXPRESSED)
    if xrp_address.is_valid_classic_address(raw):
        return AddressVerdict(VALID, f"{raw!r}: valid XRP classic address (checksum verified locally)", NOT_EXPRESSED)
    return AddressVerdict(
        INVALID,
        f"{address!r} is not a valid XRP Ledger account: it is neither an X-address nor a classic address "
        f"whose double-SHA256 checksum holds. XRP uses its OWN base58 alphabet, so a Bitcoin address pasted "
        f"here fails on the alphabet before the checksum",
        UNKNOWN,
    )




def _solana(address: str) -> AddressVerdict:
    """The SOL verdict, delegated to chains/solana_address.py, which owns that encoding.

    SOLANA HAS NO CHECKSUM, and that is not a gap in this check -- it is the format. An
    address is base58 of a 32-byte ed25519 public key and nothing more, so "valid" here means
    "decodes to 32 bytes" and cannot mean more. Said out loud in the verdict text, because an
    operator reading VALID for a Solana address is entitled to know it is a weaker statement
    than the same word for Bitcoin.

    on-curve is REPORTED and does not refuse. An off-curve key is a Program Derived Address,
    which is a legitimate payout destination (an Associated Token Account is one), so
    refusing it would be a false refusal of the most common SPL destination there is.
    """
    raw = address.strip() if isinstance(address, str) else ""
    if not solana_address.is_valid_address(raw):
        return AddressVerdict(
            INVALID,
            f"{address!r} is not a Solana address: it does not decode as base58 to 32 bytes. Solana has no "
            f"checksum, so length is the only structure there is and a truncated key is the failure mode",
            UNKNOWN,
        )
    shape = "on-curve (a key could exist for it)" if solana_address.is_on_curve(raw) else (
        "off-curve, i.e. a Program Derived Address -- legitimate, and what an Associated Token Account is"
    )
    return AddressVerdict(
        VALID,
        f"{raw!r}: 32-byte Solana public key, {shape}. NOTE: Solana addresses carry NO CHECKSUM, so this "
        f"cannot catch a typo the way a Bitcoin or XRP check can -- only a wrong LENGTH",
        NOT_EXPRESSED,
    )


def _icp(address: str) -> AddressVerdict:
    """The ICP verdict, delegated to chains/icp_account.py, which owns that encoding.

    AN ICP ACCOUNT IDENTIFIER CARRIES A CHECKSUM, unlike Solana's, so this is a real
    check rather than a length test: 64 hex characters whose leading 4 bytes are the
    CRC32 of the remaining 28. A truncated or mistyped identifier fails it, which on a
    payout path is the difference between an error and funds sent somewhere
    unrecoverable.

    THE NETWORK IS NOT_EXPRESSED, AND THAT IS A PROPERTY OF ICP RATHER THAN A GAP. An
    account identifier is a hash; nothing in it says which network it belongs to, and
    ICP has no testnet to distinguish from mainnet in the first place (DFINITY's own
    documentation: "there is no testnet for ICP"). So the burn-proofing the Bitcoin
    family gets from a version byte and XRP gets from its own encoding has no ICP
    analogue -- said out loud here, because an operator reading VALID for an ICP address
    is entitled to know it is a weaker statement about WHERE than the same word for
    Bitcoin.

    A PRINCIPAL IS REFUSED, and it is the likely paste error: `ybr6p-5dyeb-...` is an
    identity, not a ledger address, and the two are different lengths over different
    alphabets. The message names the distinction rather than saying "invalid", because
    an operator holding both needs to know which one they pasted.
    """
    raw = address.strip() if isinstance(address, str) else ""
    if icp_account.is_account_identifier(raw):
        return AddressVerdict(
            VALID,
            f"{raw!r}: 64-hex ICP account identifier, CRC32 checksum verified locally with no network "
            f"call. NOTE: an account identifier is a SHA224 hash and expresses NO network, and ICP has "
            f"no testnet, so this says nothing about mainnet-versus-anything",
            NOT_EXPRESSED,
        )
    if icp_account.is_principal(raw):
        return AddressVerdict(
            INVALID,
            f"{raw!r} is a PRINCIPAL, not an ICP ledger address. A principal names an identity; the "
            f"ledger is paid at an ACCOUNT IDENTIFIER, which is 64 hex characters derived from a "
            f"principal and a subaccount by SHA224 -- and the derivation is one-way, so a principal "
            f"cannot be converted here. Get the account identifier instead (`dfx ledger account-id`)",
            NOT_EXPRESSED,
        )
    return AddressVerdict(
        INVALID,
        f"{address!r} is not an ICP account identifier: it is not 64 hex characters whose leading 4 "
        f"bytes are the CRC32 of the remaining 28. A truncated copy-paste fails exactly here, which is "
        f"the point -- the length-and-alphabet test alone would accept it",
        UNKNOWN,
    )


# THE AUTHORITY. Asset -> the one function that knows that asset's address format.
#
# Every entry DELEGATES to the module that already owns the encoding rather than
# reimplementing it (rule 8). Nothing in this file decodes anything itself: the Bitcoin
# family reads modules/address_network.py's tables, XRP reads chains/xrp_address.py, SOL
# reads chains/solana_address.py and ICP reads chains/icp_account.py. That is deliberate -- a second implementation of an
# encoding would agree with the first on the day it was written and drift from then on, and
# the drift would be invisible until a payout burned.
#
# Keyed by the SAME asset strings chains/registry.py and Config.ALLOWED_PAIRS use, and
# tests/test_address_authority.py asserts the key sets line up so a new chain cannot arrive
# with no validator and no noise.
VALIDATORS: dict[str, Callable[[str], AddressVerdict]] = {
    "BTC": lambda address: _bitcoin_family("BTC", address),
    "LTC": lambda address: _bitcoin_family("LTC", address),
    "GRC": lambda address: _bitcoin_family("GRC", address),
    "XRP": _xrp,
    "SOL": _solana,
    "ICP": _icp,
}


def check_address(asset: str, address: str) -> AddressVerdict:
    """The verdict for `address` on `asset`. The one entry point every guard calls.

    An empty or non-string address is INVALID before any validator is consulted, and that is
    not defensive padding -- but THE REASON GIVEN HERE WAS WRONG. It said
    "`swaps.payout_address` is a nullable column"; db.py's schema declares it
    `payout_address TEXT NOT NULL`. Checked, not recalled.

    The guard stays, for the reasons that are actually true. NOT NULL does not exclude the
    EMPTY STRING, which is what a daemon returning an error where an address was expected
    writes -- the 2026-09-27 accident's neighbour. And this function is not reached only from
    that column: services/, modules/htlc_fee.py's PLATFORM_FEE_<ASSET>_ADDRESS (an environment
    variable, absent by default) and the regtest harness all call it, and an environment
    variable really can be None. A guard justified by a fact that is false is one a future
    reader deletes after checking the schema (rule 16: a wrong comment is a bug, fixed with
    the same seriousness as the code).

    Rule 14 wants the reason printable, so it names what it actually got.

    An asset with no entry is NO_VALIDATOR, never INVALID and never VALID. See the module
    header for how the fund path reads that and how the gap is closed by a test rather than
    by guessing at runtime.
    """
    validator = VALIDATORS.get(asset)
    if validator is None:
        return AddressVerdict(
            NO_VALIDATOR,
            f"there is no address validator for asset {asset!r}; this repository has one for "
            f"{', '.join(sorted(VALIDATORS))}. The address was NOT checked and was NOT refused -- refusing a "
            f"chain we cannot check would break that chain's payouts, which is the more expensive failure",
            UNKNOWN,
        )
    if not isinstance(address, str) or not address.strip():
        return AddressVerdict(
            INVALID, f"no {asset} address at all: {address!r} is not a non-empty string", UNKNOWN
        )
    return validator(address)


def check_receive_address(asset: str, address: str, expected_network: str | None) -> AddressVerdict:
    """check_address(), plus the NETWORK question -- which only the receive path asks.

    THE TWO PATHS ARE NOT SYMMETRIC AND THIS FUNCTION IS THE DIFFERENCE.

    A payout address comes from OUTSIDE: a customer types it, so the guard there catches a
    stranger's typo and the network is whatever their wallet is on. A DEPOSIT address comes
    from OUR OWN DAEMON via `getnewaddress`, so a typo is not the risk. What is:

      - a daemon on the WRONG NETWORK. `gridcoinresearchd getnewaddress` with no `-testnet`
        put RyyNX8E7tDRzACL47h8JKKDUXf9aMCz8YV into the operator's live MAINNET staking
        wallet on 2026-09-27. A terminal that believes it is on testnet would have handed
        that address to a customer as a testnet deposit target.
      - a wallet that answered with an error string instead of an address
      - a truncated RPC response
      - a row written correctly and read back corrupted

    and the loss lands on the CUSTOMER rather than on us, which is why this half is the
    worse of the two: they cannot detect it before paying.

    `expected_network` is MAINNET, TESTNET, or None meaning NOT ESTABLISHED. None does NOT
    refuse -- rule 17: "I could not tell" and "it is wrong" must never be the same value, and
    network_target.classify() returns UNRECOGNIZED for a daemon on a custom -rpcport, which
    is an ordinary configuration and not a fault.

    A mismatch IS a refusal, and the asymmetry with the payout path is deliberate and
    measured: at the moment a deposit address is chosen, NOTHING HAS MOVED. No deposit has
    been taken, no row has been written, no instruction has been published. The refusal costs
    a retry. Letting it through costs a customer's whole deposit, paid into a chain we are
    not watching. On the payout path the same refusal would strand a swap whose deposit is
    already ours, which is why payout_service does not do this.
    """
    verdict = check_address(asset, address)
    if verdict.state != VALID or expected_network is None:
        return verdict
    if verdict.network in (NOT_EXPRESSED, UNKNOWN):
        # The encoding does not carry a network (XRP) or could not be narrowed to one. Say
        # so in the verdict rather than passing silently: an operator who asked for a network
        # check is entitled to know it did not happen on this chain.
        return verdict._replace(
            why=f"{verdict.why}. NETWORK NOT CHECKED: this address does not express one, and this process "
            f"is configured for {expected_network}"
        )
    if verdict.network != expected_network:
        return AddressVerdict(
            INVALID,
            f"{address!r} is a {verdict.network} {asset} address, and this process is configured for "
            f"{expected_network} {asset}. That is the 2026-09-27 accident exactly: a daemon started without "
            f"-testnet answers `getnewaddress` with a MAINNET address, and a customer paying it would send "
            f"real money to a chain nothing here is watching. NOTHING WAS WRITTEN",
            verdict.network,
        )
    return verdict._replace(why=f"{verdict.why}, and {verdict.network} matches this process's configuration")


def expected_network(asset: str, rpc: dict | None) -> str | None:
    """Which network this process BELIEVES it is on for `asset`, or None when not established.

    Derived from network_target.classify() rather than from a second reading of the port,
    because that function already owns the port vocabulary and has the four-answer shape this
    needs -- MAINNET, TEST, UNCONFIGURED, UNRECOGNIZED (rule 8).

    Only two of the four produce a comparable answer:

      MAINNET        -> MAINNET
      TEST           -> TESTNET
      UNCONFIGURED   -> None. Nothing was set; there is nothing to compare against.
      UNRECOGNIZED   -> None. A mainnet daemon on a custom -rpcport lands here, and
                        classify()'s own docstring refuses to call that "not mainnet".
                        Turning it into TESTNET would be a guess dressed as a measurement.

    None for XRP and SOL too, and that is honest rather than a gap: those two are not
    in network_target.CHAIN_PORTS because they have no conventional port to classify against
    -- that module's own comment says so -- so there is nothing here to compare a decoded
    network TO. The address-side check still runs; only the cross-check is skipped.
    """
    entry = (rpc or {}).get(asset) or {}
    verdict = classify(asset, int(entry.get("port") or 0))
    if verdict == "MAINNET":
        return MAINNET
    if verdict == "TEST":
        return TESTNET
    return None
