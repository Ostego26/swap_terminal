"""The smallest output each Bitcoin-derived chain will relay, and the arithmetic for it.

Role: function level (one decision: "is this output below the chain's dust floor?")
Reads: nothing. Every input is an argument -- no file, no environment, no network,
      no database, no daemon.
Writes: nothing
Can move funds: no
Mainnet-safe: yes

=============================================================================
WHY THIS IS A PACKAGE-ROOT LEAF AND NOT STILL IN modules/htlc_fee.py
=============================================================================

MOVED HERE 2026-11-22, unchanged, because the brokered PAYOUT path needed it and
could not reach it. The dust rule was written for the atomic-swap modules and its
only caller was modules/htlc_rpc.py -- so `chains/` and `services/`, which is where
a customer's payout is priced and sent, had no dust floor at all.

IMPORTING IT WHERE IT WAS WOULD HAVE PUT A SIGNING LIBRARY IN THE WEB PROCESS.
modules/htlc_fee.py imports modules.htlc_spend (for varint_length) and
modules.address_authority, and htlc_spend imports `ecdsa` and `base58` at module
level while atomic_htlc_scripts imports `bech32`. So `from modules.htlc_fee import
dust_threshold_satoshis` inside services/quote_service.py would make the Flask
application unimportable without the atomic-swap signing stack -- which is exactly
the deployment posture chains/xrp.py:1786 and chains/base.py:380 record and that
tests/test_optional_dependency_posture.py now pins: a host that watches deposits
and cannot sign must not be made to carry a signing library.

THE SHAPE IS THE ONE chains/base.py:1030 ALREADY PRESCRIBED -- "a package-root leaf
with no third-party imports, as script_pub_key.py and microfortnights.py already
are". This file imports nothing but __future__.

ONE TABLE AND ONE ARITHMETIC (rule 8). modules/htlc_fee.py now imports these names
back and keeps assert_no_output_is_dust(), which is HTLC-specific; every existing
importer of htlc_fee is unaffected. The compact-size boundaries and varint_length
moved here too and modules/htlc_spend.py imports them, because the length
arithmetic had to be reachable without the byte builder that uses `struct`.

VERIFIED NUMERICALLY ACROSS THE MOVE, not just by the suite: every threshold was
computed before and after for five output shapes on all three chains and compared.

    P2PKH   BTC 546   LTC 5460   GRC 1
    P2SH    BTC 540   LTC 5400   GRC 1
    P2WPKH  BTC 294   LTC 2940   GRC 1
    P2WSH   BTC 330   LTC 3300   GRC 1
    P2TR    BTC 330   LTC 3300   GRC 1

Identical in every cell, and both describe_dust_threshold() sentences byte for byte.
"""

from __future__ import annotations

# Compact-size boundaries. A varint encodes COUNTS and SCRIPT LENGTHS, and is
# not the same encoding as a script number: this repository confused the two
# once already and every HTLC refund branch was unspendable for it (see
# modules/atomic_htlc_scripts.encode_script_number).
VARINT_1BYTE_MAX = 0xFC
VARINT_2BYTE_MAX = 0xFFFF
VARINT_4BYTE_MAX = 0xFFFFFFFF


def varint_length(value: int) -> int:
    """How many bytes a compact size for `value` occupies, without building it.

    MOVED OUT OF modules/htlc_spend.py 2026-11-22, where it was
    `len(varint(value))`. That delegation was correct and unusable from here:
    varint() builds bytes with `struct`, lives beside `ecdsa` and `base58` imports,
    and this file must stay importable by a process that has neither. The thresholds
    above moved with it so there is one encoding rather than one encoding and a copy
    of its boundaries (rule 8), and htlc_spend.varint() now reads them from here.

    NOT A SCRIPT NUMBER. The comment above says why that distinction is worth
    repeating: confusing the two made every HTLC refund branch unspendable once.
    """
    if value < 0:
        raise ValueError(f"a compact size is never negative, got {value!r}")
    if value <= VARINT_1BYTE_MAX:
        return 1
    if value <= VARINT_2BYTE_MAX:
        return 3
    if value <= VARINT_4BYTE_MAX:
        return 5
    return 9


# SOLANA IS NOT IN THIS TABLE, AND THAT WAS A DECISION RATHER THAN AN OMISSION.
# Recorded here as well as at the other end, because CLAUDE.md rule 8 asks that
# where two implementations genuinely differ "the difference is the point and
# belongs in a comment at BOTH sites, naming the other one." The other one is
# chains/solana_units.py, whose RENT_EXEMPT_* constants are Solana's analogue
# of this table, and its module docstring carries the full argument. The short
# form, three reasons:
#
#   different suite       this file belongs to the atomic-swap modules; Solana
#                         was added to the Flask app, which imports none of it.
#   the signature dies    dust_threshold_satoshis(asset, script_pubkey) takes a
#                         Bitcoin scriptPubKey and prices (txout_bytes +
#                         spend_bytes) at a per-kvB relay rate. Solana has no
#                         scriptPubKey, no txout and no per-byte pricing -- its
#                         fee is 5,000 lamports per SIGNATURE.
#   not the same quantity dust is REFUSED BY RELAY: the transaction never
#                         propagates and nothing is lost. A Solana account
#                         below the rent-exempt minimum is ACCEPTED, exists,
#                         and is then collected by the runtime -- the funds go
#                         away after the fact.
#
# Adding "SOL" here would mean a parameter meaningless for one of its assets,
# which is how one table quietly becomes two rules sharing a name.
DUST_RELAY_FEE_SAT_PER_KVB: dict[str, int] = {
    # Bitcoin Core's DUST_RELAY_TX_FEE. Gives 546 for P2PKH, 294 for P2WPKH.
    "BTC": 3000,
    # Litecoin Core 0.21.4's is TEN TIMES Bitcoin's, which is the fact that
    # makes this a live defect rather than a theoretical one: 5,460 for P2PKH
    # and 2,940 for P2WPKH.
    "LTC": 30000,
    # GRIDCOIN HAS NO DUST RULE, and this is READ FROM THE POLICY SOURCE rather
    # than assumed from Bitcoin's (rule 17). gridcoin-community/
    # Gridcoin-Research, master branch, read 2026-09-25:
    #
    #   src/policy/policy.cpp   IsStandardTx() walks tx.vout and the only
    #                           amount test in it is `if (txout.nValue == 0)
    #                           return false;`. There is no dust branch.
    #   whole repository        zero occurrences of IsDust, GetDustThreshold or
    #                           DUST_RELAY_TX_FEE -- each searched separately
    #                           across the repository, not just these files.
    #   src/main.cpp:117        nMinimumInputValue = 0.
    #   src/consensus/consensus.h:26-28
    #                           the only amount floor is a FEE floor --
    #                           MIN_TX_FEE = MIN_RELAY_TX_FEE = 10000 halfords,
    #                           scaled by (1 + bytes/1000). A fee rule, not an
    #                           output rule.
    #
    # So a rate of 0, and MINIMUM_OUTPUT_SATOSHIS below is what actually binds
    # on GRC -- which is exactly Gridcoin's rule, expressed in the same
    # machinery as the other two rather than as a special case.
    #
    # WHAT THIS IS NOT: a measurement against a running Gridcoin daemon. There
    # is none in this setup. It is a reading of the current source of the
    # reference implementation, which is a weaker claim than the BTC and LTC
    # rows -- those were measured against daemons -- and an operator on an
    # older build should check their own before trusting it.
    "GRC": 0,
}

# Every chain here refuses an output worth nothing. On BTC and LTC the dust
# threshold is far above it and this never binds; on GRC it is the whole rule.
# It also catches a 0.25% platform fee that quantizes to zero, which on GRC --
# with no dust threshold above it -- is otherwise the one way to build a
# non-standard transaction that the old `<= 0` guard could never have seen,
# because that guard only ever looked at the DESTINATION.
MINIMUM_OUTPUT_SATOSHIS = 1

# Core's GetDustThreshold: the cost of spending the output being protected.
# 148 = 32 (txid) + 4 (vout) + 1 (script length) + 107 (scriptSig) + 4
# (sequence). The witness figure is the same sum with 107 scaled down by
# WITNESS_SCALE_FACTOR 4 and integer-divided: 32 + 4 + 1 + 26 + 4.
LEGACY_INPUT_SPEND_BYTES = 148
WITNESS_INPUT_SPEND_BYTES = 67

# Eight bytes of value precede every output's script.
TXOUT_VALUE_BYTES = 8

# BIP141: a witness program is OP_0..OP_16 followed by a single push of 2 to 40
# bytes, which is what Core's CScript::IsWitnessProgram() checks.
WITNESS_PROGRAM_MIN_PUSH = 2
WITNESS_PROGRAM_MAX_PUSH = 40
OP_0 = 0x00
OP_1 = 0x51
OP_16 = 0x60

# The same 1000 the fee rate uses, as an int, because the dust arithmetic is
# integer satoshis throughout and Core's is too -- its CFeeRate::GetFee
# truncates, so doing this in Decimal and rounding would give a threshold this
# repository believes and no node applies.
BYTES_PER_KVB_INT = 1000


def is_witness_program(script_pubkey: bytes) -> bool:
    """BIP141's shape test: a version opcode, then one push of 2 to 40 bytes.

    Read off the BYTES, never off a rendered address or a daemon's
    `scriptPubKey.type` string, for the same reason every other comparison in
    this package is: Bitcoin Core 28.1 and Litecoin Core 0.21.4 disagree about
    what a decoded scriptPubKey contains, and agree about the hex.
    """
    if len(script_pubkey) < WITNESS_PROGRAM_MIN_PUSH + 2 or len(script_pubkey) > WITNESS_PROGRAM_MAX_PUSH + 2:
        return False
    version = script_pubkey[0]
    if version != OP_0 and not (OP_1 <= version <= OP_16):
        return False
    return script_pubkey[1] == len(script_pubkey) - 2


def dust_threshold_satoshis(asset: str, script_pubkey: bytes) -> int:
    """The smallest value this chain will relay in an output paying `script_pubkey`.

    Args:
        asset: BTC, LTC or GRC. It selects the dust relay rate and nothing else.
        script_pubkey: the output's script, as the node laid it out. The bytes,
            never an address -- what lets one call answer for a P2WPKH
            platform-fee address and a P2PKH destination in the same
            transaction is that it reads the shape instead of being told it.

    Raises:
        ValueError: if the asset is unknown. Adding a chain means adding its
            dust relay rate here, in this one table, beside its fee rate and
            its floor (rule 11).
    """
    if asset not in DUST_RELAY_FEE_SAT_PER_KVB:
        raise ValueError(
            # SUPPORTED_ASSETS WAS NAMED HERE AND STAYED BEHIND IN modules/htlc_fee.py,
            # where it is derived from FEE_RATE_COIN_PER_KVB. Importing it back would
            # make this leaf depend on the module that depends on it, so the message
            # now lists the dust table's OWN keys -- which is the more honest answer
            # anyway: this function's question is "is there a dust rate for this
            # asset", and a fee rate is a different table (ruff caught it as F821).
            f"no dust rule for asset {asset!r}; known assets are "
            f"{', '.join(sorted(DUST_RELAY_FEE_SAT_PER_KVB))}. Adding a chain means adding its "
            f"dust relay rate here, beside its fee rate and its floor."
        )
    spend_bytes = WITNESS_INPUT_SPEND_BYTES if is_witness_program(script_pubkey) else LEGACY_INPUT_SPEND_BYTES
    txout_bytes = TXOUT_VALUE_BYTES + varint_length(len(script_pubkey)) + len(script_pubkey)
    by_rate = (txout_bytes + spend_bytes) * DUST_RELAY_FEE_SAT_PER_KVB[asset] // BYTES_PER_KVB_INT
    return max(by_rate, MINIMUM_OUTPUT_SATOSHIS)


def describe_dust_threshold(asset: str, script_pubkey: bytes) -> str:
    """One self-describing line: the threshold, and every number that decided it.

    Rule 14 -- an operator reading a refusal must not have to open this file to
    learn why 2,500 satoshis was too small.
    """
    spend_bytes = WITNESS_INPUT_SPEND_BYTES if is_witness_program(script_pubkey) else LEGACY_INPUT_SPEND_BYTES
    txout_bytes = TXOUT_VALUE_BYTES + varint_length(len(script_pubkey)) + len(script_pubkey)
    kind = "witness" if is_witness_program(script_pubkey) else "legacy"
    return (
        f"{asset} dust threshold is {dust_threshold_satoshis(asset, script_pubkey)} satoshis "
        f"= ({txout_bytes}-byte output + {spend_bytes}-byte {kind} spend) x "
        f"{DUST_RELAY_FEE_SAT_PER_KVB[asset]} sat/kvB / 1000, floored at {MINIMUM_OUTPUT_SATOSHIS}"
    )


def smallest_dust_threshold(asset: str) -> int:
    """The LOWEST dust threshold any output shape on this chain can have.

    ADDED 2026-11-22 for the quote-time payout floor, and the "smallest" is the
    whole of its correctness argument. A quote is priced before any payout address
    exists, so the exact per-shape threshold cannot be known yet -- and refusing on
    the LARGEST would reject payouts that a witness destination would have relayed
    perfectly well. Every real address's threshold is >= this, so a figure at or
    above it may still be refused later by the exact check, while a figure below it
    could not be paid to ANY address on this chain. That makes this the conservative
    direction: it never refuses a swap that would have worked.

    MEASURED, by computing every shape rather than reasoning about which is cheapest:
    P2WPKH is the minimum on all three chains (BTC 294, LTC 2940, GRC 1), because the
    witness spend costs 67 bytes against a legacy 148 and its 31-byte output is the
    smallest standard one. tests/test_ltc_chain_facts.py pins the whole table.

    Derived from the shapes rather than hardcoded, so a new dust rate or a changed
    byte constant moves this with it.
    """
    if asset not in DUST_RELAY_FEE_SAT_PER_KVB:
        raise ValueError(
            f"no dust rule for asset {asset!r}; known assets are "
            f"{', '.join(sorted(DUST_RELAY_FEE_SAT_PER_KVB))}. Adding a chain means adding its "
            f"dust relay rate to DUST_RELAY_FEE_SAT_PER_KVB above."
        )
    return min(dust_threshold_satoshis(asset, script) for script in STANDARD_OUTPUT_SHAPES.values())


#: One representative scriptPubKey per standard output shape, for
#: smallest_dust_threshold() and for the tests that pin the table. The bytes are the
#: SHAPES ONLY -- the 20- and 32-byte payloads are filler, because
#: dust_threshold_satoshis() prices length and witness-ness and reads nothing else.
STANDARD_OUTPUT_SHAPES: dict[str, bytes] = {
    "P2PKH": bytes([0x76, 0xA9, 0x14]) + b"\x11" * 20 + bytes([0x88, 0xAC]),
    "P2SH": bytes([0xA9, 0x14]) + b"\x22" * 20 + bytes([0x87]),
    "P2WPKH": bytes([0x00, 0x14]) + b"\x33" * 20,
    "P2WSH": bytes([0x00, 0x20]) + b"\x44" * 32,
    "P2TR": bytes([0x51, 0x20]) + b"\x55" * 32,
}
