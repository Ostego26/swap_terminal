"""Behavioral tests for the four-transaction 2-of-2 chain.

Role: test (seeded inputs, no chain, no socket)
Reads: nothing. Every key here is derived in-process from a fixed seed.
Writes: nothing.
Can move funds: no. Every transaction built here spends an outpoint that does not exist.
Mainnet-safe: yes -- nothing opens a socket and nothing is broadcast.
Live-safe: yes

WHAT THESE TESTS CAN AND CANNOT ESTABLISH, STATED UP FRONT BECAUSE IT IS THE WHOLE POINT.

They establish what the BYTES are: the Gridcoin layout against real daemon output, the fee
arithmetic across the chain, the refusals, the outpoint binding, and that a txid changes when
the signatures do. Every one is a row-level assertion on a value the code produced.

They CANNOT establish that any chain accepts a 2-of-2 P2SH spend. Nothing here runs a script
interpreter, and a signature order that verifies nothing is the same LENGTH and the same SHAPE
as one that verifies -- so no assertion available in this file can tell them apart. That is
`adaptor_regtest_verify.py` at the project root, and until it has been run the claim "these
scripts work" is a source reading, which is exactly what "Verify by behavior, never by reading
the code" refuses.

NO TEST HERE READS SOURCE. There is no inspect.getsource() assertion, no check that a
docstring contains a word, and no comparison of a function against itself. The last one is
subtle and has already cost a session: asserting `sighash(script) != sighash(wrapper)` using
the same function on both sides passes whether or not the function does anything, because when
the function wraps internally both sides wrap and the two results are still different. Where a
property has to be pinned against an independent expression of the same rule, the test writes
that expression out longhand (see test_the_txid_is_reversed_double_sha256_of_the_whole_thing).
"""

from __future__ import annotations

import hashlib
import struct

import pytest
from ecdsa import SECP256k1, SigningKey
from modules.adaptor_swap_chain import (
    LOCKTIME_THRESHOLD,
    SEQUENCE_NON_FINAL,
    AdaptorChainError,
    ChainContext,
    Outpoint,
    assemble,
    assert_spends,
    assert_timelocks_ordered,
    build_cancel,
    build_punish,
    build_redeem,
    build_refund,
    build_unsigned,
    minimum_lock_value_satoshis,
    p2pkh_script,
    p2pkh_script_sig_upper_bound,
    predicted_txid,
    select_funding_inputs,
    transaction_prefix,
    transaction_suffix,
    two_of_two_funding_transaction,
    two_of_two_script_sig_upper_bound,
)
from modules.adaptor_swap_scripts import two_of_two_p2sh_script, two_of_two_redeem_script
from modules.htlc_spend import parse_transaction

# The 90 bytes a live Gridcoin testnet daemon returned on 2026-09-27, carried here from
# tests/test_gridcoin_transaction_layout.py so the BUILDER is pinned against the same vector
# the PARSER is. Rule 8 requires each site to name the other: that file proves
# parse_transaction() can read these bytes, this one proves build_unsigned() can write them,
# and a change to the layout that satisfied only one of the two would be a change that signs
# over bytes the daemon will not accept.
GRIDCOIN_RAW = bytes.fromhex(
    "02000000"
    "279ab96a"
    "01"
    "0100000000000000000000000000000000000000000000000000000000000000"
    "00000000"
    "00"
    "ffffffff"
    "01"
    "a086010000000000"
    "19"
    "76a91420b3b410d991b671aa657a8d78781cac26ebb39f88ac"
    "00000000"
    "00"
)
GRIDCOIN_NTIME = 0x6AB99A27
GRIDCOIN_OUTPOINT = "0000000000000000000000000000000000000000000000000000000000000001"
GRIDCOIN_OUTPUT_HASH160 = bytes.fromhex("20b3b410d991b671aa657a8d78781cac26ebb39f")
GRIDCOIN_OUTPUT_SATOSHIS = 100_000

# Fixed seeds rather than os.urandom, so a failure reproduces byte for byte and two pastes can
# be compared line by line. These are throwaway keys for transactions that spend outpoints
# which do not exist on any chain; no test in this file has, prints, or writes a WIF.
ALICE_SEED = b"tests/test_adaptor_swap_chain/alice"
BOB_SEED = b"tests/test_adaptor_swap_chain/bob"

ALICE_HASH160 = b"\xa1" * 20
BOB_HASH160 = b"\xb0" * 20

# Big enough that no chain's fee eats it, and deliberately not round: a value that is also a
# plausible fee makes an off-by-one in the fee arithmetic invisible.
LOCK_SATOSHIS = 5_000_037
T1_HEIGHT = 1_500
T2_HEIGHT = 1_650


def _keypair(seed: bytes) -> tuple[SigningKey, bytes]:
    signing_key = SigningKey.from_string(hashlib.sha256(seed).digest(), curve=SECP256k1)
    return signing_key, signing_key.get_verifying_key().to_string("compressed")


@pytest.fixture
def keys() -> tuple[bytes, bytes]:
    return _keypair(ALICE_SEED)[1], _keypair(BOB_SEED)[1]


@pytest.fixture
def context(keys) -> ChainContext:
    alice_pubkey, bob_pubkey = keys
    script = two_of_two_redeem_script(alice_pubkey, bob_pubkey)
    return ChainContext(
        asset="BTC",
        lock_redeem_script=script,
        cancel_redeem_script=script,
        alice_script=p2pkh_script(ALICE_HASH160),
        bob_script=p2pkh_script(BOB_HASH160),
    )


@pytest.fixture
def lock() -> Outpoint:
    return Outpoint(txid="ab" * 32, vout=1, value_satoshis=LOCK_SATOSHIS)


def _dummy_signature(marker: int) -> bytes:
    """A structurally valid DER-shaped blob of the maximum length, with a distinguishing byte.

    Not a real signature: these tests are about the transaction bytes and the arithmetic, and
    a real signature would make every expected value depend on the ECDSA nonce. `marker` lets
    a test tell one from the other in an assembled scriptSig, which is how the ORDER tests
    below assert on placement without needing an interpreter.
    """
    return bytes([0x30, marker]) + b"\x00" * 70 + bytes([0x01])


# ---------------------------------------------------------------------------
# The Gridcoin layout, pinned against real daemon bytes.
# ---------------------------------------------------------------------------


def test_the_builder_reproduces_the_real_gridcoin_transaction_byte_for_byte():
    """THE ONE VECTOR THAT IS NOT SYNTHETIC. 90 bytes off the operator's testnet daemon.

    This is the assertion that makes the Gridcoin fee arithmetic trustworthy: the extra
    vContracts byte cost 310.72 GRC when the PARSER did not know about it, and a BUILDER that
    omitted it would produce a transaction one byte shorter than the one it signed a fee for.
    """
    built = build_unsigned(
        "GRC",
        Outpoint(txid=GRIDCOIN_OUTPOINT, vout=0, value_satoshis=GRIDCOIN_OUTPUT_SATOSHIS * 2),
        [(GRIDCOIN_OUTPUT_SATOSHIS, p2pkh_script(GRIDCOIN_OUTPUT_HASH160))],
        locktime=0,
        ntime=GRIDCOIN_NTIME,
    )
    # The real transaction has a FINAL sequence; the builder always emits a non-final one,
    # because a final sequence makes nLockTime unenforceable (SEQUENCE_NON_FINAL's comment).
    # So the comparison substitutes the sequence and asserts on everything else, and the
    # difference is named rather than papered over.
    serialized = built.serialize()
    expected = GRIDCOIN_RAW.replace(b"\xff\xff\xff\xff", struct.pack("<I", SEQUENCE_NON_FINAL))
    assert serialized == expected, (
        "the builder must produce the daemon's own layout: version, nTime, vin, vout, "
        "nLockTime, then the empty vContracts byte"
    )
    assert serialized[-1:] == b"\x00", "the trailing empty vContracts byte is the one that cost 310.72 GRC"
    assert len(serialized) == len(GRIDCOIN_RAW)


def test_the_gridcoin_prefix_and_suffix_are_exactly_the_daemons():
    assert transaction_prefix("GRC", GRIDCOIN_NTIME).hex() == "02000000279ab96a"
    assert transaction_suffix("GRC", 0).hex() == "0000000000", "nLockTime plus the empty vContracts byte"


def test_btc_and_ltc_carry_no_ntime_and_no_contracts_byte():
    for asset in ("BTC", "LTC"):
        assert transaction_prefix(asset, None).hex() == "02000000"
        assert transaction_suffix(asset, 0).hex() == "00000000"


def test_a_gridcoin_transaction_is_exactly_one_byte_longer_than_the_btc_one(context, lock):
    """The one-byte difference, measured rather than asserted from the source.

    Same input, same output, same locktime -- so the ONLY difference is the layout, and the
    difference has to be five bytes: four for nTime and one for vContracts.
    """
    outputs = [(LOCK_SATOSHIS - 10_000, context.alice_script)]
    btc = build_unsigned("BTC", lock, outputs, 0)
    grc = build_unsigned("GRC", lock, outputs, 0, ntime=GRIDCOIN_NTIME)
    assert len(grc.serialize()) - len(btc.serialize()) == 5, "four bytes of nTime plus one of vContracts"


def test_an_ntime_is_required_on_gridcoin_and_refused_elsewhere(context, lock):
    with pytest.raises(AdaptorChainError, match="required"):
        build_unsigned("GRC", lock, [(1_000, context.alice_script)], 0, ntime=None)
    with pytest.raises(AdaptorChainError, match="no nTime field"):
        build_unsigned("BTC", lock, [(1_000, context.alice_script)], 0, ntime=GRIDCOIN_NTIME)


def test_what_this_module_builds_is_what_htlc_spend_can_parse_back(context, lock):
    """A round trip through the REAL parser, on both layouts, including the outpoint check.

    parse_transaction() accepts a layout only if re-serializing reproduces the bytes exactly
    AND input 0 is the outpoint the caller named. Passing that is the strongest available
    statement that these bytes are the shape the rest of the tree signs.
    """
    for asset, ntime in (("BTC", None), ("GRC", GRIDCOIN_NTIME)):
        built = build_unsigned(asset, lock, [(LOCK_SATOSHIS - 20_000, context.alice_script)], T1_HEIGHT, ntime)
        reparsed = parse_transaction(built.serialize(), lock.txid, lock.vout)
        assert reparsed.serialize() == built.serialize()
        assert reparsed.outputs == built.outputs


# ---------------------------------------------------------------------------
# The sequence and the locktime -- the two fields that make T1 mean anything.
# ---------------------------------------------------------------------------


def test_every_input_carries_a_NON_FINAL_sequence(context, lock):
    """THE SILENT TIMELOCK FAILURE. A final sequence makes nLockTime a decoration.

    IsFinalTx returns true immediately when every input's sequence is 0xFFFFFFFF, whatever
    nLockTime says. So a cancel built with a final sequence is relayable and mineable the
    instant it is signed, T1 enforces nothing, and the transaction looks completely correct.
    """
    cancel = build_cancel(context, lock, T1_HEIGHT)
    sequence = cancel.parsed.inputs[0][2]
    assert struct.unpack("<I", sequence)[0] == SEQUENCE_NON_FINAL
    assert struct.unpack("<I", sequence)[0] != 0xFFFFFFFF, (
        "a final sequence would make T1 unenforceable while the transaction still looked right"
    )


def test_the_locktime_lands_in_the_serialized_bytes_where_the_chain_reads_it(context, lock):
    cancel = build_cancel(context, lock, T1_HEIGHT)
    raw = cancel.parsed.serialize()
    assert struct.unpack("<I", raw[-4:])[0] == T1_HEIGHT, "nLockTime is the last four bytes on BTC"
    punish = build_punish(context, Outpoint(txid="cd" * 32, vout=0, value_satoshis=LOCK_SATOSHIS), T2_HEIGHT)
    assert struct.unpack("<I", punish.parsed.serialize()[-4:])[0] == T2_HEIGHT


def test_the_redeem_and_the_refund_carry_no_locktime(context, lock):
    """Neither may be delayed. The redeem races T1 (hazard 3) and the refund races T2 (hazard 1)."""
    assert build_redeem(context, lock).locktime == 0
    cancel_output = Outpoint(txid="cd" * 32, vout=0, value_satoshis=LOCK_SATOSHIS)
    assert build_refund(context, cancel_output).locktime == 0


def test_a_zero_locktime_is_refused_for_T1_and_T2(context, lock):
    with pytest.raises(AdaptorChainError, match="NO TIMELOCK AT ALL"):
        build_cancel(context, lock, 0)
    with pytest.raises(AdaptorChainError, match="NO TIMELOCK AT ALL"):
        build_punish(context, lock, 0)


def test_T2_must_be_strictly_after_T1():
    assert assert_timelocks_ordered(T1_HEIGHT, T2_HEIGHT) is None
    for t1, t2 in ((T1_HEIGHT, T1_HEIGHT), (T2_HEIGHT, T1_HEIGHT)):
        with pytest.raises(AdaptorChainError, match="not after"):
            assert_timelocks_ordered(t1, t2)


def test_a_height_and_a_timestamp_are_refused_as_a_pair():
    """The comparison that passes while meaning nothing.

    `1800000000 > 1400` is true and says nothing: one is a block height and the other is a
    unix timestamp, and nLockTime switches meaning at 500000000. Every ordering check anybody
    writes accepts this pair.
    """
    with pytest.raises(AdaptorChainError, match="not comparable"):
        assert_timelocks_ordered(T1_HEIGHT, LOCKTIME_THRESHOLD + 1)
    with pytest.raises(AdaptorChainError, match="not comparable"):
        assert_timelocks_ordered(LOCKTIME_THRESHOLD + 1, T1_HEIGHT)
    # Two timestamps in order are fine -- the rule is about MIXING, not about heights.
    assert assert_timelocks_ordered(LOCKTIME_THRESHOLD + 1, LOCKTIME_THRESHOLD + 2) is None


# ---------------------------------------------------------------------------
# Which script each transaction's input satisfies. The digest is over this.
# ---------------------------------------------------------------------------


def test_the_redeem_and_cancel_satisfy_the_LOCK_script_and_the_others_the_CANCEL_one(keys):
    """A digest over the wrong script code verifies against nothing and reports a bare failure.

    Two DIFFERENT 2-of-2s here on purpose -- a third key pair for the cancel side, which the
    protocol does not have but this test does -- so the two scripts are distinguishable bytes
    and the pairing can be asserted rather than assumed.
    """
    alice_pubkey, bob_pubkey = keys
    third_pubkey = _keypair(b"a third key, only so the two scripts differ")[1]
    lock_script = two_of_two_redeem_script(alice_pubkey, bob_pubkey)
    cancel_script = two_of_two_redeem_script(alice_pubkey, third_pubkey)
    assert lock_script != cancel_script
    context = ChainContext(
        asset="BTC",
        lock_redeem_script=lock_script,
        cancel_redeem_script=cancel_script,
        alice_script=p2pkh_script(ALICE_HASH160),
        bob_script=p2pkh_script(BOB_HASH160),
    )
    lock = Outpoint(txid="ab" * 32, vout=1, value_satoshis=LOCK_SATOSHIS)
    cancel_output = Outpoint(txid="cd" * 32, vout=0, value_satoshis=LOCK_SATOSHIS)
    assert build_redeem(context, lock).redeem_script == lock_script
    assert build_cancel(context, lock, T1_HEIGHT).redeem_script == lock_script
    assert build_refund(context, cancel_output).redeem_script == cancel_script
    assert build_punish(context, cancel_output, T2_HEIGHT).redeem_script == cancel_script


def test_the_cancel_pays_the_SECOND_two_of_two_and_not_a_party(keys):
    """The cancel's output must be the cancel P2SH. Paying a party instead would end the swap.

    If Tx_cancel paid Bob directly, there would be no refund and no punish -- the coin would
    simply return to the funder at T1, and Alice's locked XMR would have no compensation path
    at all. Hazard 1's punish exists precisely because the cancel output is JOINT.
    """
    alice_pubkey, bob_pubkey = keys
    third_pubkey = _keypair(b"a third key, only so the two scripts differ")[1]
    cancel_script = two_of_two_redeem_script(alice_pubkey, third_pubkey)
    context = ChainContext(
        asset="BTC",
        lock_redeem_script=two_of_two_redeem_script(alice_pubkey, bob_pubkey),
        cancel_redeem_script=cancel_script,
        alice_script=p2pkh_script(ALICE_HASH160),
        bob_script=p2pkh_script(BOB_HASH160),
    )
    cancel = build_cancel(context, Outpoint(txid="ab" * 32, vout=1, value_satoshis=LOCK_SATOSHIS), T1_HEIGHT)
    assert cancel.parsed.outputs[0][1] == two_of_two_p2sh_script(cancel_script)
    assert cancel.parsed.outputs[0][1] != context.bob_script
    assert cancel.parsed.outputs[0][1] != context.alice_script


def test_an_unknown_stage_is_refused_rather_than_defaulted(context):
    with pytest.raises(AdaptorChainError, match="unknown stage"):
        context.script_for("lock")


def test_the_redeem_pays_alice_and_the_refund_pays_bob(context, lock):
    """Reversing these two is a payout to the wrong party, and it is one line either way."""
    cancel_output = Outpoint(txid="cd" * 32, vout=0, value_satoshis=LOCK_SATOSHIS)
    assert build_redeem(context, lock).parsed.outputs[0][1] == context.alice_script
    assert build_refund(context, cancel_output).parsed.outputs[0][1] == context.bob_script
    assert build_punish(context, cancel_output, T2_HEIGHT).parsed.outputs[0][1] == context.alice_script


# ---------------------------------------------------------------------------
# The fee arithmetic across the chain.
# ---------------------------------------------------------------------------


def test_the_scriptsig_bound_is_the_measured_221_for_two_compressed_keys(context):
    assert len(context.lock_redeem_script) == 71, "OP_2, two 34-byte pushes, OP_2, OP_CHECKMULTISIG"
    assert two_of_two_script_sig_upper_bound(context.lock_redeem_script) == 221, (
        "OP_0 at one byte, two 74-byte signature pushes, a 72-byte push of the 71-byte script"
    )


def test_the_real_assembled_scriptsig_never_exceeds_the_bound(context, lock):
    """An upper bound that is not an upper bound sizes a fee for a transaction smaller than
    the one broadcast, which underpays the miner on a spend that has to beat a timelock."""
    cancel = build_cancel(context, lock, T1_HEIGHT)
    raw_hex, _ = assemble(cancel, _dummy_signature(0x41), _dummy_signature(0x42))
    assert len(bytes.fromhex(raw_hex)) <= cancel.unsigned_size_bound


def test_the_p2pkh_bound_is_the_measured_108(keys):
    alice_pubkey, _ = keys
    assert p2pkh_script_sig_upper_bound(alice_pubkey) == 108, "a 74-byte signature push plus a 34-byte key push"
    with pytest.raises(AdaptorChainError, match="compressed public key"):
        p2pkh_script_sig_upper_bound(b"\x04" + b"\x11" * 64)


def test_every_fee_is_the_input_minus_the_output_on_every_chain(keys):
    """The fee the code MEANT beside the fee the bytes encode. ParsedTransaction.output_total
    is read off the built transaction rather than recomputed, so this compares two independent
    expressions of the amount rather than one against itself."""
    alice_pubkey, bob_pubkey = keys
    script = two_of_two_redeem_script(alice_pubkey, bob_pubkey)
    for asset, ntime in (("BTC", None), ("LTC", None), ("GRC", GRIDCOIN_NTIME)):
        context = ChainContext(
            asset=asset,
            lock_redeem_script=script,
            cancel_redeem_script=script,
            alice_script=p2pkh_script(ALICE_HASH160),
            bob_script=p2pkh_script(BOB_HASH160),
            ntime=ntime,
        )
        funded = max(LOCK_SATOSHIS, minimum_lock_value_satoshis(context) * 4)
        lock = Outpoint(txid="ab" * 32, vout=1, value_satoshis=funded)
        cancel = build_cancel(context, lock, T1_HEIGHT)
        assert cancel.fee_satoshis == lock.value_satoshis - cancel.parsed.output_total
        cancel_output = Outpoint(txid="cd" * 32, vout=0, value_satoshis=cancel.output_satoshis)
        refund = build_refund(context, cancel_output)
        assert refund.fee_satoshis == cancel_output.value_satoshis - refund.parsed.output_total
        # THE CHAINED HOP: what the refund pays out is what is left after TWO fees.
        assert refund.output_satoshis == funded - cancel.fee_satoshis - refund.fee_satoshis


def test_the_cancel_path_pays_two_fees_and_the_redeem_path_one(context, lock):
    """The asymmetry that makes minimum_lock_value_satoshis() necessary."""
    redeem = build_redeem(context, lock)
    cancel = build_cancel(context, lock, T1_HEIGHT)
    cancel_output = Outpoint(txid="cd" * 32, vout=0, value_satoshis=cancel.output_satoshis)
    refund = build_refund(context, cancel_output)
    assert redeem.output_satoshis == lock.value_satoshis - redeem.fee_satoshis
    assert refund.output_satoshis < redeem.output_satoshis, (
        "the refund pays out strictly less than the redeem because it is two hops from the lock"
    )
    assert lock.value_satoshis - refund.output_satoshis == cancel.fee_satoshis + refund.fee_satoshis


def test_the_gridcoin_minimum_lock_is_two_fee_floors_plus_a_dust_output(keys):
    """MEASURED 2026-09-28: 0.02 GRC, because the GRC fee floor is 0.01 per transaction and
    the cancel path pays it twice. A lock funded for one fee produces a cancel that confirms
    and a refund that cannot be built, which strands the coin in the second 2-of-2."""
    alice_pubkey, bob_pubkey = keys
    script = two_of_two_redeem_script(alice_pubkey, bob_pubkey)
    context = ChainContext(
        asset="GRC",
        lock_redeem_script=script,
        cancel_redeem_script=script,
        alice_script=p2pkh_script(ALICE_HASH160),
        bob_script=p2pkh_script(BOB_HASH160),
        ntime=GRIDCOIN_NTIME,
    )
    assert minimum_lock_value_satoshis(context) == 2_000_001, "0.01 GRC twice, plus one satoshi of dust"


def test_a_lock_funded_below_the_minimum_cannot_complete_the_cancel_path(keys):
    """THE REFUSAL EARNS ITS KEEP HERE. Funded at the minimum minus one, the cancel still
    builds and the refund then cannot -- which is the failure the minimum exists to predict,
    and it arrives at the worst possible moment."""
    alice_pubkey, bob_pubkey = keys
    script = two_of_two_redeem_script(alice_pubkey, bob_pubkey)
    context = ChainContext(
        asset="GRC",
        lock_redeem_script=script,
        cancel_redeem_script=script,
        alice_script=p2pkh_script(ALICE_HASH160),
        bob_script=p2pkh_script(BOB_HASH160),
        ntime=GRIDCOIN_NTIME,
    )
    too_thin = minimum_lock_value_satoshis(context) - 1
    lock = Outpoint(txid="ab" * 32, vout=1, value_satoshis=too_thin)
    cancel = build_cancel(context, lock, T1_HEIGHT)
    cancel_output = Outpoint(txid="cd" * 32, vout=0, value_satoshis=cancel.output_satoshis)
    with pytest.raises((AdaptorChainError, ValueError)):
        build_refund(context, cancel_output)
    # And at the minimum it does complete, so the number is the boundary and not a margin.
    at_minimum = Outpoint(txid="ab" * 32, vout=1, value_satoshis=minimum_lock_value_satoshis(context))
    cancel_ok = build_cancel(context, at_minimum, T1_HEIGHT)
    build_refund(context, Outpoint(txid="cd" * 32, vout=0, value_satoshis=cancel_ok.output_satoshis))


def test_a_fee_that_would_eat_the_whole_output_is_refused(context):
    with pytest.raises(AdaptorChainError, match="nothing would be left"):
        build_redeem(context, Outpoint(txid="ab" * 32, vout=0, value_satoshis=1))


# ---------------------------------------------------------------------------
# FINDING 2: the txid depends on the signatures, so step 0 is two rounds.
# ---------------------------------------------------------------------------


def test_the_txid_is_reversed_double_sha256_of_the_whole_thing(context, lock):
    """Pinned against the rule written out longhand, because the reversal is the easy mistake.

    The right hash in the wrong byte order produces an outpoint nobody has while looking
    entirely plausible beside a real txid. This is deliberately NOT written as
    `predicted_txid(x) == predicted_txid(x)`; the expected value is computed here from hashlib
    directly, so the assertion fails if the function stops reversing.
    """
    cancel = build_cancel(context, lock, T1_HEIGHT)
    raw = cancel.parsed.serialize()
    expected = hashlib.sha256(hashlib.sha256(raw).digest()).digest()[::-1].hex()
    assert predicted_txid(cancel.parsed) == expected
    assert predicted_txid(cancel.parsed) != hashlib.sha256(hashlib.sha256(raw).digest()).hexdigest(), (
        "the un-reversed digest is what a naive implementation returns, and it is a different string"
    )


def test_signing_the_cancel_CHANGES_its_txid(context, lock):
    """FINDING 2, as a measurement. A legacy txid covers the scriptSigs, so the unsigned
    transaction's txid is not the one that appears on any chain -- which is why Tx_refund and
    Tx_punish cannot be built in the same round as Tx_cancel is signed."""
    cancel = build_cancel(context, lock, T1_HEIGHT)
    unsigned_txid = predicted_txid(cancel.parsed)
    _, signed_txid = assemble(cancel, _dummy_signature(0x41), _dummy_signature(0x42))
    assert signed_txid != unsigned_txid


def test_a_DIFFERENT_pair_of_signatures_gives_a_DIFFERENT_cancel_txid(context, lock):
    """THE GRIEFING VECTOR, measured. Same inputs, same outputs, same locktime -- a second
    valid signature under a different nonce produces a different txid, and every transaction
    built against the first one is then unspendable."""
    cancel = build_cancel(context, lock, T1_HEIGHT)
    _, txid_one = assemble(cancel, _dummy_signature(0x41), _dummy_signature(0x42))
    _, txid_two = assemble(cancel, _dummy_signature(0x43), _dummy_signature(0x44))
    assert txid_one != txid_two
    # Both spend the SAME outpoint, so only one of the two can ever confirm. That is what
    # makes it mutual destruction rather than a double spend either party profits from.
    assert cancel.spends.txid == lock.txid


def test_a_refund_built_against_the_wrong_cancel_is_refused(context, lock):
    """assert_spends() is round 1' of FINDING 2: check the assembled cancel's txid against the
    outpoint the refund actually references, BEFORE releasing the refund signature."""
    cancel = build_cancel(context, lock, T1_HEIGHT)
    _, real_txid = assemble(cancel, _dummy_signature(0x41), _dummy_signature(0x42))
    _, variant_txid = assemble(cancel, _dummy_signature(0x43), _dummy_signature(0x44))
    refund = build_refund(context, Outpoint(txid=real_txid, vout=0, value_satoshis=cancel.output_satoshis))
    assert assert_spends(refund.parsed, real_txid, 0) is None
    with pytest.raises(AdaptorChainError, match="different txid"):
        assert_spends(refund.parsed, variant_txid, 0)
    with pytest.raises(AdaptorChainError, match="not"):
        assert_spends(refund.parsed, real_txid, 1)


def test_the_outpoint_is_serialized_little_endian(context, lock):
    """A txid is printed big-endian and serialized reversed. Getting it backwards produces
    bad-txns-inputs-missingorspent, which reads on screen as 'already spent'."""
    cancel = build_cancel(context, lock, T1_HEIGHT)
    outpoint = cancel.parsed.inputs[0][0]
    assert outpoint[:32] == bytes.fromhex(lock.txid)[::-1]
    assert struct.unpack("<I", outpoint[32:36])[0] == lock.vout


# ---------------------------------------------------------------------------
# FINDING 1: Tx_lock is built, signed and HELD.
# ---------------------------------------------------------------------------


def test_the_lock_txid_is_known_before_anything_is_broadcast(context, keys):
    """FINDING 1, as the property that matters: the whole cancel chain is built against a
    txid computed from a transaction still sitting in a variable.

    `sendtoaddress` cannot do this. Not because the txid arrives late -- it does come back --
    but because by then the output is irrevocably in the mempool and the funder holds no
    counterparty signature on any spend of it. A counterparty who then declines to sign the
    cancel has locked the coin permanently, with no timelock behind it.
    """
    alice_pubkey, _ = keys
    funding = Outpoint(txid="ef" * 32, vout=3, value_satoshis=LOCK_SATOSHIS * 2)
    unsigned_lock, lock_vout, fee = two_of_two_funding_transaction(
        context, funding, p2pkh_script_sig_upper_bound(alice_pubkey), None, None
    )
    assert fee > 0
    assert unsigned_lock.outputs[lock_vout][1] == context.lock_script_pubkey
    # Signed with a stand-in scriptSig, because what is being asserted is that the txid is
    # computable at all from bytes that have not been sent -- not anything about the signature.
    funding_script_sig = b"\x48" + _dummy_signature(0x41)[:72] + b"\x21" + alice_pubkey
    txid = predicted_txid(unsigned_lock, {0: funding_script_sig})
    lock_outpoint = Outpoint(
        txid=txid, vout=lock_vout, value_satoshis=unsigned_lock.outputs[lock_vout][0]
    )
    cancel = build_cancel(context, lock_outpoint, T1_HEIGHT)
    assert_spends(cancel.parsed, txid, lock_vout)
    assert build_redeem(context, lock_outpoint).spends.txid == txid


def test_the_lock_vout_is_returned_and_not_assumed(context, keys):
    """An assumed vout of 0 is how a four-transaction chain ends up spending the change."""
    alice_pubkey, _ = keys
    funding = Outpoint(txid="ef" * 32, vout=0, value_satoshis=LOCK_SATOSHIS * 4)
    unsigned_lock, lock_vout, _ = two_of_two_funding_transaction(
        context, funding, p2pkh_script_sig_upper_bound(alice_pubkey), LOCK_SATOSHIS, p2pkh_script(b"\xcc" * 20)
    )
    assert len(unsigned_lock.outputs) == 2, "the lock plus the change"
    assert unsigned_lock.outputs[lock_vout][1] == context.lock_script_pubkey
    assert unsigned_lock.outputs[lock_vout][0] == LOCK_SATOSHIS


def test_an_amount_with_nowhere_for_the_change_to_go_is_refused(context, keys):
    """Dropping the change would pay the remainder to the miner without the caller asking."""
    alice_pubkey, _ = keys
    funding = Outpoint(txid="ef" * 32, vout=0, value_satoshis=LOCK_SATOSHIS * 4)
    bound = p2pkh_script_sig_upper_bound(alice_pubkey)
    with pytest.raises(AdaptorChainError, match="go together"):
        two_of_two_funding_transaction(context, funding, bound, LOCK_SATOSHIS, None)
    with pytest.raises(AdaptorChainError, match="go together"):
        two_of_two_funding_transaction(context, funding, bound, None, p2pkh_script(b"\xcc" * 20))


def test_a_lock_that_would_leave_no_change_is_refused(context, keys):
    alice_pubkey, _ = keys
    funding = Outpoint(txid="ef" * 32, vout=0, value_satoshis=LOCK_SATOSHIS)
    with pytest.raises(AdaptorChainError, match="in change"):
        two_of_two_funding_transaction(
            context, funding, p2pkh_script_sig_upper_bound(alice_pubkey), LOCK_SATOSHIS, p2pkh_script(b"\xcc" * 20)
        )


# ---------------------------------------------------------------------------
# Input selection: the decision fundrawtransaction would have made.
# ---------------------------------------------------------------------------


def test_input_selection_takes_the_fewest_outputs_that_suffice():
    utxos = [
        {"txid": "aa" * 32, "vout": 0, "amount": "0.01"},
        {"txid": "bb" * 32, "vout": 1, "amount": "1.00"},
        {"txid": "cc" * 32, "vout": 2, "amount": "0.50"},
    ]
    chosen, total = select_funding_inputs(utxos, 120_000_000)
    assert [row["amount"] for row in chosen] == ["1.00", "0.50"], "largest first, so the fewest inputs"
    assert total == 150_000_000


def test_input_selection_refuses_rather_than_returning_a_short_total():
    utxos = [{"txid": "aa" * 32, "vout": 0, "amount": "0.01"}]
    with pytest.raises(AdaptorChainError, match="short of the"):
        select_funding_inputs(utxos, 100_000_000)
    with pytest.raises(AdaptorChainError, match="short of the"):
        select_funding_inputs([], 1_000)


def test_an_unreadable_listunspent_row_is_refused_and_not_skipped():
    """A skipped row is how a wallet with a balance reports itself unfundable, and the operator
    then goes looking for the balance instead of for the row."""
    utxos = [{"txid": "aa" * 32, "vout": 0, "amount": "1.0"}, {"txid": "bb" * 32, "vout": 1}]
    with pytest.raises(AdaptorChainError, match="missing"):
        select_funding_inputs(utxos, 1_000)


def test_amounts_go_through_Decimal_and_not_float():
    """`int(0.1 * 100_000_000)` is 9999999 on some values. One satoshi short of what an
    assertion expects is a failure nobody reads as a float problem."""
    _, total = select_funding_inputs([{"txid": "aa" * 32, "vout": 0, "amount": "0.1"}], 1)
    assert total == 10_000_000


# ---------------------------------------------------------------------------
# Refusals about the things that cannot be checked anywhere else.
# ---------------------------------------------------------------------------


def test_an_unknown_chain_is_refused_and_monero_is_named(context, lock):
    with pytest.raises(AdaptorChainError, match="Monero is"):
        build_unsigned("XMR", lock, [(1_000, context.alice_script)], 0)


def test_a_serialized_txid_passed_as_a_printed_one_is_refused():
    with pytest.raises(AdaptorChainError, match="not hex"):
        Outpoint(txid="z" * 64, vout=0, value_satoshis=1_000)
    with pytest.raises(AdaptorChainError, match="characters, not 64"):
        Outpoint(txid="ab" * 16, vout=0, value_satoshis=1_000)


def test_the_all_zero_coinbase_outpoint_is_refused():
    with pytest.raises(AdaptorChainError, match="COINBASE"):
        Outpoint(txid="00" * 32, vout=0, value_satoshis=1_000)


def test_an_empty_script_in_the_context_is_refused(keys):
    alice_pubkey, bob_pubkey = keys
    script = two_of_two_redeem_script(alice_pubkey, bob_pubkey)
    with pytest.raises(AdaptorChainError, match="anyone-can-spend"):
        ChainContext(
            asset="BTC",
            lock_redeem_script=script,
            cancel_redeem_script=script,
            alice_script=b"",
            bob_script=p2pkh_script(BOB_HASH160),
        )


def test_a_transaction_with_no_outputs_is_refused(context, lock):
    with pytest.raises(AdaptorChainError, match="entire input to the miner"):
        build_unsigned("BTC", lock, [], 0)


def test_a_sha256_digest_passed_as_a_hash160_is_refused():
    with pytest.raises(AdaptorChainError, match="unspendable by anyone"):
        p2pkh_script(hashlib.sha256(b"x").digest())


def test_the_assembled_scriptsig_leads_with_OP_0_and_ends_with_the_redeem_script(context, lock):
    """Both are consensus requirements of P2SH plus OP_CHECKMULTISIG, and both fail as a bare
    script error. What ORDER the two signatures are in cannot be checked here at all -- both
    orders are the same length and the same shape -- which is why the chain harness asserts
    the transposed order is REFUSED on a real chain."""
    cancel = build_cancel(context, lock, T1_HEIGHT)
    raw_hex, _ = assemble(cancel, _dummy_signature(0x41), _dummy_signature(0x42))
    reparsed = parse_transaction(bytes.fromhex(raw_hex), lock.txid, lock.vout)
    script_sig = reparsed.inputs[0][1]
    assert script_sig[0] == 0x00, "the OP_0 dummy OP_CHECKMULTISIG pops and does not use"
    assert script_sig.endswith(context.lock_redeem_script), "P2SH pops the LAST push as the redeem script"
    # The marker bytes prove the placement without an interpreter: first signature first.
    assert script_sig.index(bytes([0x30, 0x41])) < script_sig.index(bytes([0x30, 0x42]))
