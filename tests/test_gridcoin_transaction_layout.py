"""Gridcoin serializes vContracts after nLockTime, and the parser refused the extra byte.

Role: test (pure; no chain, no network, no database)
Reads: modules/htlc_spend.parse_transaction
Writes: nothing
Can move funds: no
Mainnet-safe: yes
Live-safe: yes

WHERE THESE BYTES CAME FROM. The operator funded a real GRC leg of a real LTC->GRC atomic swap
on Gridcoin TESTNET, 2026-09-27, and the claim could not be built:

    TransactionLayoutError: could not take apart a 90-byte transaction in any known layout.
      4-byte prefix: a 136-byte scriptSig does not fit
      8-byte prefix: 5 bytes left after the last output

The parser was right to refuse -- one byte was unaccounted for and it will not guess -- but the
layout it did not know is the layout of the chain this business is built around. The raw hex
below is `gridcoinresearchd -testnet createrawtransaction` output, pasted verbatim, and its
nTime decodes to 22:35:19Z, the minute it was asked for.

CONFIRMED AGAINST THE SOURCE, not inferred from the byte: Gridcoin-Research
src/primitives/transaction.h serializes nVersion, nTime, vin, vout, nLockTime, and then
`vContracts` (std::vector<GRC::Contract>) when nVersion >= 2, else the legacy `hashBoinc`
string. An empty vector is a varint 0.
"""

from __future__ import annotations

import pathlib
import sys

import pytest

REPOSITORY_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "swap_terminal"))

from modules.htlc_spend import (  # noqa: E402  the path shim above must run first
    GRIDCOIN_EMPTY_CONTRACTS,
    LOCKTIME_LEN,
    TransactionLayoutError,
    parse_transaction,
)

# Verbatim from the operator's testnet daemon. One input, one P2PKH output, 90 bytes.
GRIDCOIN_RAW = bytes.fromhex(
    "02000000"                                                          # version 2
    "279ab96a"                                                          # nTime
    "01"                                                                # one input
    "0100000000000000000000000000000000000000000000000000000000000000"  # txid, reversed
    "00000000"                                                          # vout 0
    "00"                                                                # empty scriptSig
    "ffffffff"                                                          # sequence
    "01"                                                                # one output
    "a086010000000000"                                                  # 0.001 GRC
    "19"                                                                # 25-byte script
    "76a91420b3b410d991b671aa657a8d78781cac26ebb39f88ac"                # P2PKH
    "00000000"                                                          # nLockTime
    "00"                                                                # vContracts: EMPTY
)
GRIDCOIN_OUTPOINT = "0000000000000000000000000000000000000000000000000000000000000001"

# The same bytes with the Gridcoin tail removed -- the Peercoin layout the parser used to
# require. Kept because it proves the fix ADDED a layout rather than replacing one.
PEERCOIN_RAW = GRIDCOIN_RAW[:-1]


def test_the_real_gridcoin_transaction_parses_and_round_trips():
    """THE INCIDENT. 90 bytes off a live testnet daemon, which the parser refused."""
    parsed = parse_transaction(GRIDCOIN_RAW, GRIDCOIN_OUTPOINT, 0)
    assert len(parsed.inputs) == 1 and len(parsed.outputs) == 1
    assert parsed.prefix.hex() == "02000000279ab96a", "version plus nTime, carried verbatim"
    assert parsed.suffix.hex() == "0000000000", "nLockTime plus the empty vContracts byte"
    assert parsed.serialize() == GRIDCOIN_RAW, (
        "re-serializing must reproduce the bytes exactly -- that round trip is what proves the "
        "layout was read and not guessed"
    )


def test_the_contracts_byte_survives_a_scriptsig_replacement():
    """The reason the suffix is carried verbatim rather than rebuilt. Signing replaces one
    scriptSig; everything else must come out byte-identical, or the transaction the daemon
    accepts is not the one that was signed over."""
    parsed = parse_transaction(GRIDCOIN_RAW, GRIDCOIN_OUTPOINT, 0)
    signed = parsed.serialize({0: bytes([0x51] * 136)})
    assert signed.endswith(b"\x00\x00\x00\x00" + GRIDCOIN_EMPTY_CONTRACTS)
    assert signed.startswith(bytes.fromhex("02000000279ab96a"))
    # 136 exactly, not 137: the original already carried a length byte for its EMPTY scriptSig
    # (0x00), and varint(136) is also one byte, so only the script contents are added. My first
    # version of this assertion said +136+1 and went red -- the arithmetic was mine, not the
    # code's, and it is spelled out here because an off-by-one in a size calculation is how a
    # fee estimate silently drifts.
    assert len(signed) == len(GRIDCOIN_RAW) + 136, "only the script contents are added"


def test_the_peercoin_layout_without_contracts_still_parses():
    """The fix ADDED a layout. A transaction ending at nLockTime -- every BTC and LTC spend, and
    a Gridcoin v1 one -- must still parse, or this would have traded one chain for two."""
    parsed = parse_transaction(PEERCOIN_RAW, GRIDCOIN_OUTPOINT, 0)
    assert parsed.suffix.hex() == "00000000"
    assert len(parsed.suffix) == LOCKTIME_LEN
    assert parsed.serialize() == PEERCOIN_RAW


def test_a_non_empty_contracts_vector_is_REFUSED_rather_than_carried():
    """THE DELIBERATE REFUSAL, and it is the interesting decision in this change.

    A Gridcoin contract is a protocol message -- a beacon, a poll, a vote. The suffix is carried
    verbatim, so a non-empty vContracts would be preserved into a transaction this module then
    SIGNS. Signing bytes we cannot read is the failure with no version that can be taken back,
    which is the same reason parse_transaction refuses a best guess about the layout.

    `createrawtransaction` emits an empty vector, so the accepted case is the only one this path
    can produce -- a non-empty one means the transaction did not come from where we think it
    did, and that is worth stopping for rather than signing through."""
    with pytest.raises(TransactionLayoutError, match="NON-EMPTY Gridcoin vContracts"):
        parse_transaction(GRIDCOIN_RAW[:-1] + b"\x01", GRIDCOIN_OUTPOINT, 0)
    with pytest.raises(TransactionLayoutError, match="NON-EMPTY Gridcoin vContracts"):
        parse_transaction(GRIDCOIN_RAW[:-1] + b"\xff", GRIDCOIN_OUTPOINT, 0)


@pytest.mark.parametrize("extra", [2, 3, 5, 8])
def test_any_other_number_of_trailing_bytes_is_still_refused(extra):
    """The check widened by exactly ONE byte, and no more. A parser that tolerated any tail
    would accept a segwit serialization or a truncated transaction and sign over the wrong
    bytes -- the failure the refusal exists to prevent."""
    with pytest.raises(TransactionLayoutError, match="bytes left after the last output"):
        parse_transaction(PEERCOIN_RAW + bytes(extra), GRIDCOIN_OUTPOINT, 0)


def test_the_outpoint_check_still_decides_between_layouts():
    """Round-tripping alone is not proof: the caller's own outpoint landing at the right offset
    is what makes the layout decision unambiguous. Asking for an outpoint this transaction does
    not spend must fail even though the bytes parse perfectly."""
    with pytest.raises(TransactionLayoutError, match="the caller asked to spend"):
        parse_transaction(GRIDCOIN_RAW, "ab" * 32, 0)
    with pytest.raises(TransactionLayoutError, match="the caller asked to spend"):
        parse_transaction(GRIDCOIN_RAW, GRIDCOIN_OUTPOINT, 7)
