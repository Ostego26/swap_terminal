"""The 2-of-2 the adaptor swap needs, and the two footguns that fail identically on a node.

Role: test (pure; no chain, no network, no database)
Reads: modules/adaptor_swap_scripts
Writes: nothing
Can move funds: no
Mainnet-safe: yes
Live-safe: yes

WHAT IS TESTED HERE, AND WHAT CANNOT BE.

An adaptor-signature XMR swap cannot reuse this repo's HTLC: both of its branches end in a
single-key OP_CHECKSIG, and an adaptor signature has no purchase on an output the spender can
already sign alone. docs/monero_swap_protocol.md section 2 specifies the five transactions
that replace it; modules/adaptor_swap_scripts builds the scripts and signature assembly.

Everything here is a pure function of keys, scripts and transactions, so it is tested hard.

WHAT IS NOT ESTABLISHED, said plainly rather than left as a gap: NO 2-OF-2 P2SH HAS EVER
BEEN SPENT ON GRIDCOIN FROM THIS REPOSITORY. Two things ARE established and they answer
different halves --

  READ from Gridcoin-Research src/script.cpp, 2026-09-28: OP_CHECKMULTISIG is fully
  implemented in EvalScript, TX_MULTISIG is a Solver template, the P2SH path recurses into
  the subscript, and no P2SH sigop cap appears.
  MEASURED on 2026-09-27: CLTV is CONSENSUS-enforced on Gridcoin, because the daemon refused
  to MINE an early refund (generateblock: "TestBlockValidity failed") rather than merely
  declining to relay it.

Neither is a 2-of-2 spending. That wants a regtest chain with both branches exercised, which
is the same standard "Verify by behavior, never by SQL text" holds gate logic to, and it is
named work rather than an assumption.
"""

from __future__ import annotations

import hashlib
import pathlib
import sys

import pytest

REPOSITORY_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "swap_terminal"))

from modules.adaptor_swap_scripts import (  # noqa: E402  the path shim above must run first
    COMPRESSED_PUBKEY_BYTES,
    MULTISIG_REQUIRED,
    MULTISIG_TOTAL,
    OP_0,
    OP_2,
    OP_CHECKMULTISIG,
    AdaptorScriptError,
    two_of_two_p2sh_script,
    two_of_two_redeem_script,
    two_of_two_script_sig,
    two_of_two_sighash,
)
from modules.atomic_htlc_scripts import p2sh_script_for  # noqa: E402  same
from modules.htlc_spend import (  # noqa: E402  same
    legacy_sighash,
    parse_transaction,
    public_key_for,
    sign_digest,
)
from regtest.keys import generate_key  # noqa: E402  same


def _keys():
    """Two distinct in-process keypairs. NOT dumpprivkey, and never written anywhere."""
    first, second = generate_key(), generate_key()
    assert first.address != second.address
    return first, second


def _pubkeys():
    first, second = _keys()
    return (public_key_for(first.private_key, compressed=True),
            public_key_for(second.private_key, compressed=True))


def test_the_redeem_script_is_exactly_two_of_two_and_nothing_else():
    """No hashlock, and that absence is the design. The hashlock is what the adaptor
    REPLACES, not something it joins -- an output carrying both would still be spendable by
    whoever learns the preimage, which is the leak the adaptor exists to avoid."""
    first, second = _pubkeys()
    script = two_of_two_redeem_script(first, second)

    assert script[:1] == OP_2, "the required-signature count comes first"
    assert script[-1:] == OP_CHECKMULTISIG
    assert script[-2:-1] == OP_2, "the key count comes immediately before the opcode"
    assert first in script and second in script

    # STRUCTURALLY, NOT BY COUNTING OPCODE BYTES. My first version asserted
    # `script.count(bytes([0xA8])) == 0` for "no OP_SHA256" and it FAILED -- a compressed
    # public key is 33 arbitrary bytes, so 0xA8 and 0x63 appear inside them by chance. A
    # script is opcodes and PUSHES, and a byte inside a push is data. Stripping the two pushes
    # and checking what remains is the only version of this assertion that means anything.
    without_pushes = (
        script[:1]
        + script[1 + 1 + COMPRESSED_PUBKEY_BYTES + 1 + COMPRESSED_PUBKEY_BYTES:]
    )
    assert without_pushes == OP_2 + OP_2 + OP_CHECKMULTISIG, (
        f"the opcodes outside the two key pushes must be exactly 2/2/CHECKMULTISIG, got "
        f"{without_pushes.hex()} -- no OP_SHA256 and no OP_IF, because there is no hashlock "
        f"and there are no branches"
    )
    # 1 + (1+33) + (1+33) + 1 + 1
    assert len(script) == 2 + 2 * (1 + COMPRESSED_PUBKEY_BYTES) + 1


def test_the_key_order_is_preserved_and_the_script_is_not_sorted():
    """A SORTED variant would be friendlier and is refused.

    Both parties derive this script independently from the same two public keys. If one sorts
    and the other does not they compute DIFFERENT P2SH addresses, the swap dies at funding,
    and nothing in the failure says which side sorted. So the order is the caller's, fixed
    once at setup, and this test pins that swapping the arguments changes the script."""
    first, second = _pubkeys()
    forward = two_of_two_redeem_script(first, second)
    reversed_order = two_of_two_redeem_script(second, first)

    assert forward != reversed_order, "the order must be part of the script"
    assert two_of_two_p2sh_script(forward) != two_of_two_p2sh_script(reversed_order), (
        "and therefore part of the address, which is what makes a mismatch visible at funding"
    )
    assert forward.index(first) < forward.index(second)
    assert reversed_order.index(second) < reversed_order.index(first)


def test_two_identical_keys_are_refused_because_that_is_spendable_alone():
    """A 2-of-2 naming one key twice is spendable by whoever holds that key ALONE. It is not
    a 2-of-2, and it is the same degenerate case build_htlc_redeem_script() refuses when both
    HTLC branches hash to one key -- the defect that made every contract unclaimable on
    2026-09-24, arriving from the other direction."""
    first, _second = _pubkeys()
    with pytest.raises(AdaptorScriptError, match="spendable by whoever holds it ALONE"):
        two_of_two_redeem_script(first, first)


@pytest.mark.parametrize(
    ("bad", "because"),
    [
        (b"", "empty"),
        (bytes(33), "0x00 prefix is not a compressed key"),
        (bytes([0x02]) + bytes(31), "32 bytes, one short"),
        (bytes([0x02]) + bytes(33), "34 bytes, one long"),
        (bytes([0x04]) + bytes(64), "uncompressed"),
        (bytes([0x05]) + bytes(32), "0x05 is not a compressed prefix"),
    ],
)
def test_only_a_compressed_secp256k1_key_is_accepted(bad, because):
    """UNCOMPRESSED KEYS ARE REFUSED RATHER THAN SUPPORTED, and the reason is not purity: a
    2-of-2 whose keys are encoded differently still works, but the redeem script's hash160
    then depends on an encoding choice nobody records, so the counterparty rebuilding the
    script from the same two keys computes a different address."""
    good, _other = _pubkeys()
    with pytest.raises(AdaptorScriptError):
        two_of_two_redeem_script(bad, good)
    with pytest.raises(AdaptorScriptError):
        two_of_two_redeem_script(good, bad)


def test_a_non_bytes_key_is_refused_naming_its_type():
    """A hex STRING is the plausible wrong argument -- it is 66 characters and looks like a
    key. This repository has already lost a run to exactly that class of confusion: the swap
    driver passed a hex string where all three clients declare bytes, and it died three frames
    deep in push_data with "can't concat str to bytes"."""
    good, _other = _pubkeys()
    with pytest.raises(AdaptorScriptError, match="str, not bytes"):
        two_of_two_redeem_script(good.hex(), good)


def test_the_p2sh_wrapper_is_the_same_one_the_htlc_path_uses():
    """Delegation, asserted, because it is what lets htlc_vout() locate a contract funded
    here by the same scriptPubKey match that has found every contract this repo has funded."""
    first, second = _pubkeys()
    script = two_of_two_redeem_script(first, second)
    assert two_of_two_p2sh_script(script) == p2sh_script_for(script)
    # And it really is P2SH: OP_HASH160 <20 bytes> OP_EQUAL
    wrapper = two_of_two_p2sh_script(script)
    assert wrapper[:2] == bytes([0xA9, 0x14]) and wrapper[-1:] == bytes([0x87])
    assert wrapper[2:22] == hashlib.new("ripemd160", hashlib.sha256(script).digest()).digest()


# ---------------------------------------------------------------------------------------
# THE TWO FOOTGUNS. Both produce a bare script failure on a node with nothing to say which
# one it was, which is why each is refused or asserted here instead.
# ---------------------------------------------------------------------------------------


def test_the_script_sig_leads_with_OP_0_and_it_is_not_padding():
    """OP_CHECKMULTISIG pops one item MORE than it uses -- a consensus bug from 2010 that can
    never be fixed, because fixing it would invalidate old transactions. The extra pop must be
    present and must be an empty push.

    A missing OP_0 consumes the FIRST SIGNATURE as the dummy, so the script fails having
    looked entirely correct: two signatures were supplied, both were valid, and one was eaten.
    """
    first, second = _pubkeys()
    script = two_of_two_redeem_script(first, second)
    # 10 bytes: 0x30, a length, seven body bytes, and the SIGHASH byte. My first version was
    # 8 bytes and the module refused it as too short -- correctly, which is the check working.
    signature = bytes([0x30, 0x07]) + bytes(7) + bytes([0x01])
    other = bytes([0x30, 0x07]) + bytes(6) + bytes([0x02]) + bytes([0x01])

    script_sig = two_of_two_script_sig(signature, other, script)
    assert script_sig[:1] == OP_0, "the dummy pop comes first or a signature is eaten"
    assert script_sig.endswith(bytes([len(script)]) + script) or script in script_sig, (
        "a P2SH spend ends with the redeem script"
    )


def test_the_signature_order_matches_the_key_order():
    """OP_CHECKMULTISIG walks signatures and keys IN LOCKSTEP and never backtracks.

    Signatures for keys [A, B] supplied as [sig_B, sig_A] verify NEITHER: it tries sig_B
    against A and fails, discards A, tries sig_B against B and succeeds, then has sig_A left
    with no keys remaining. The argument names here are `first` and `second` to match
    two_of_two_redeem_script()'s, so the pairing is visible at both call sites.

    Asserted by POSITION in the bytes, because that is the only thing consensus reads."""
    first_key, second_key = _keys()
    first_pub = public_key_for(first_key.private_key, compressed=True)
    second_pub = public_key_for(second_key.private_key, compressed=True)
    script = two_of_two_redeem_script(first_pub, second_pub)

    digest = hashlib.sha256(b"a digest to sign over").digest()
    first_sig = sign_digest(first_key.private_key, digest)
    second_sig = sign_digest(second_key.private_key, digest)
    assert first_sig != second_sig

    script_sig = two_of_two_script_sig(first_sig, second_sig, script)
    assert script_sig.index(first_sig) < script_sig.index(second_sig), (
        "the signature for the FIRST key in the script must come first"
    )
    # And the transposed assembly is a different scriptSig -- which is the whole hazard: it is
    # well formed, it is the same length, and it verifies nothing.
    transposed = two_of_two_script_sig(second_sig, first_sig, script)
    assert transposed != script_sig
    assert len(transposed) == len(script_sig), (
        "identical length is why this failure gives a node nothing to report"
    )


@pytest.mark.parametrize(
    ("bad", "match"),
    [
        (b"", "empty or not bytes"),
        (bytes([0x02, 0x07]) + bytes(8), "not DER"),
        (bytes([0x30, 0x02, 0x01]), "too short"),
    ],
)
def test_a_signature_that_is_not_der_with_a_sighash_byte_is_refused(bad, match):
    """A raw 64-byte (r, s) pair is what most signing libraries hand back by default, and it
    reaches the node as a malformed push. sign_digest() produces low-S DER with the SIGHASH
    byte appended; anything else is refused HERE rather than by a node that will only say the
    script failed."""
    first, second = _pubkeys()
    script = two_of_two_redeem_script(first, second)
    good = bytes([0x30, 0x07]) + bytes(7) + bytes([0x01])
    with pytest.raises(AdaptorScriptError, match=match):
        two_of_two_script_sig(bad, good, script)
    with pytest.raises(AdaptorScriptError, match=match):
        two_of_two_script_sig(good, bad, script)


def test_the_sighash_is_over_the_REDEEM_SCRIPT_and_not_the_p2sh_wrapper():
    """A P2SH input signs over the redeem script, never the P2SH scriptPubKey. Signing the
    wrapper produces a signature that verifies against nothing, and the node reports a bare
    script failure with no hint which of the two scripts was used.

    This is the digest an ADAPTOR PRE-SIGNATURE is made over too, which is why it has a named
    function: the pre-signature and the completed signature must provably be over the same
    bytes, and one definition is what makes that true rather than hoped."""
    first, second = _pubkeys()
    script = two_of_two_redeem_script(first, second)
    wrapper = two_of_two_p2sh_script(script)

    # A one-input, one-output Bitcoin-layout transaction spending some outpoint.
    outpoint = bytes.fromhex("11" * 32) + (0).to_bytes(4, "little")
    raw = (
        bytes.fromhex("02000000")
        + b"\x01" + outpoint + b"\x00" + bytes.fromhex("ffffffff")
        + b"\x01" + (50_000).to_bytes(8, "little") + bytes([25]) + bytes(25)
        + bytes(4)
    )
    parsed = parse_transaction(raw)

    over_redeem = two_of_two_sighash(parsed, 0, script)

    # ANCHORED, NOT COMPARED TO ITSELF. My first version asserted
    # `two_of_two_sighash(..., script) != two_of_two_sighash(..., wrapper)` -- which stays true
    # even if the function wraps its argument internally, because then BOTH sides wrap and the
    # two are still different. A mutation replacing the body with
    # `legacy_sighash(parsed, i, p2sh_script_for(redeem_script))` SURVIVED it.
    #
    # The assertion has to pin what the digest is OVER, so it is compared against the
    # primitive called with the redeem script directly. That is the most dangerous mutation in
    # this file's set: a signature over the wrapper verifies against nothing, and the node
    # reports only that the script failed.
    assert over_redeem == legacy_sighash(parsed, 0, script), (
        "the digest must be over the REDEEM SCRIPT itself"
    )
    assert over_redeem != legacy_sighash(parsed, 0, wrapper), (
        "and must not be over the P2SH scriptPubKey"
    )
    assert len(over_redeem) == 32


def test_the_required_and_total_counts_are_declared_separately_on_purpose():
    """Both are 2 and they are two constants, because OP_CHECKMULTISIG takes them at OPPOSITE
    ENDS of the script -- and transposing an m-of-n silently produces something a single party
    can spend. A reader checking "is this really 2-of-2" should find the answer twice."""
    assert MULTISIG_REQUIRED == MULTISIG_TOTAL == 2
    first, second = _pubkeys()
    script = two_of_two_redeem_script(first, second)
    assert script[0] == 0x50 + MULTISIG_REQUIRED, "OP_2 is OP_1 + 1, i.e. 0x50 + n"
    assert script[-2] == 0x50 + MULTISIG_TOTAL
