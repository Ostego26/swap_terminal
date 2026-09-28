"""Behavioral tests for the script-chain <-> Monero swap composition.

Role: tests
Reads: swap_terminal/modules/monero_swap_protocol.py and, for the cryptographic half,
      the Go DLEQ helper binary at tools/dleq_helper/dleq_helper.
Writes: nothing
Can move funds: no. Nothing here reaches a chain, a wallet or a network. Every private
      share in this file is generated inside the test and discarded when it ends.
Mainnet-safe: yes. No RPC endpoint is read and no network is contacted.
Live-safe: yes.

EVERY ASSERTION HERE IS ON AN OUTCOME, NEVER ON THE SOURCE.

There is no `inspect.getsource` in this file and there must never be one. The failure it
prevents is measured rather than hypothetical: on 2026-09-27 three tests elsewhere matched
DOCSTRING PROSE and passed while the code under them was mutated, and a fourth compared
four private keys against an attribute that does not exist and would have passed beside a
debug line echoing every key. A test that reads the implementation cannot fail when the
implementation is wrong, which is the only thing a test is for.

So: seeded inputs, real calls, assertions on returned values, raised exceptions and
message content. Where a refusal's TEXT is asserted, it is asserted because the text is
the deliverable -- an operator reads the refusal and decides what to change -- and not as
a proxy for the behavior, which is asserted separately by the exception type.

THE REDACTION TESTS ARE THE ONE PLACE THIS FILE HANDLES A VALUE IT MUST NOT LEAK, and they
do it by constructing a share from a DISTINCTIVE value and asserting that value's decimal
and hexadecimal forms appear in neither `repr`, `str`, an f-string, nor a refusal message.
That is the behavioral form of "never print a key": it tests the absence of the value in
the output rather than the presence of an `__repr__` in the source.

WHAT NEEDS THE GO BINARY, AND WHAT THE SKIP COSTS

The pure decisions -- ranges, redaction, admissibility of a point, the timelock
arithmetic, the withholding gate -- need nothing. The cross-curve proof needs
tools/dleq_helper/dleq_helper, and those tests skip without it, following
tests/test_dleq_helper.py's pattern. That skip is a real loss of coverage and it is named
rather than hidden: a suite reporting `skipped` for the cryptography while reporting
`passed` overall is the "skipped plus success in the same output" defect CLAUDE.md rule 13
names, so the skip reason says what is not being checked.
"""

from __future__ import annotations

import hashlib
import re
import secrets
from dataclasses import dataclass

import pytest
from chains import monero_keys
from modules import adaptor_ecdsa
from modules.dleq_helper import (
    DleqHelper,
    DleqProofResult,
    DleqVerdict,
    helper_is_available,
    witness_from_int,
)
from modules.ed25519_group import (
    BASEPOINT,
    FIELD_PRIME,
    GROUP_ORDER,
    Point,
    scalar_base_mul,
)
from modules.htlc_timelock import SECONDS_PER_BLOCK
from modules.monero_swap_protocol import (
    SHARE_UPPER_BOUND,
    XMR_OUTPUT_LOCK_BLOCKS,
    XMR_SECONDS_PER_BLOCK,
    XMR_TEST_NETTYPES,
    PrivateShares,
    ProtocolError,
    ShareCommitment,
    ShareRangeError,
    ShareRejected,
    TimelockPlan,
    assert_timelock_ordering,
    commit_shares,
    lock_address,
    monero_address_network,
    monero_network_is_test,
    reconstruct_spend_key,
    recover_counterparty_spend_share,
    redeem_is_safe_to_broadcast,
    redeem_presignature_may_be_released,
    rehearse,
    require_share_in_range,
    s_chain_confirmation_seconds,
    sample_shares,
    verify_share_commitment,
    xmr_lock_wait_seconds,
)

helper_required = pytest.mark.skipif(
    not helper_is_available(),
    reason=(
        "the Go DLEQ helper binary is absent, so the cross-curve half of this protocol is "
        "NOT being checked: share commitment, counterparty verification, and the full "
        "rehearsal all skip. Build it with `cd tools/dleq_helper && go build -o dleq_helper .`"
    ),
)

# The network the lock address is built on for tests. "mainnet" is the correct answer for a
# REGTEST chain and that is not a mistake: cryptonote_config.h:361 has
# `case FAKECHAIN: return mainnet;`, so a regtest wallet's address carries the mainnet
# prefix byte 18. chains/monero_keys.py's header records the measurement.
TEST_NETWORK = "mainnet"

# A digest standing in for the script-chain redeem transaction's sighash. In a real swap it
# comes from htlc_spend.legacy_sighash over a transaction that does not exist yet (the
# missing fifth component); here it only has to be 32 bytes and stable.
REDEEM_DIGEST = hashlib.sha256(b"swap_terminal/test/redeem-transaction-digest").digest()


@dataclass(frozen=True)
class FakeVerdict:
    """How a FakeHelper should LIE when asked to verify. Defaults to telling the truth.

    `secp` and `ed` of None mean "report the honest commitments"; a string replaces one, so
    a test can hand back a proof that verifies about SOME pair of keys that is not the pair
    the caller holds -- the reuse attack the `expect_*` arguments exist to refuse.
    """

    verified: bool = True
    secp: str | None = None
    ed: str | None = None
    reason: str = ""


class FakeHelper:
    """A DLEQ helper that answers exactly what a test tells it to, with no subprocess.

    It exists so the DECISIONS in monero_swap_protocol can be tested against a helper that
    LIES -- a commitment that disagrees with this repo's own derivation, a verdict of
    False, a verdict about different keys -- which is the whole point of those checks and
    is not reachable with the real binary, because the real binary is honest. A fake that
    only ever behaves correctly would test nothing the real one does not.
    """

    def __init__(self, proof: bytes, secp: str, ed: str, verdict: FakeVerdict | None = None) -> None:
        """The three honest answers, plus an optional `verdict` that overrides what `verify`
        reports. Four verdict-shaping parameters were four keyword arguments until ruff's
        PLR0913 counted seven; grouping them is rule 12's "extract, do not raise the
        ceiling", and it also stops a test from setting `verified=False` while leaving the
        commitments matching, which is a combination no real helper produces."""
        self._proof = proof
        self._secp = secp
        self._ed = ed
        self._verdict = verdict or FakeVerdict()

    def prove(self, witness: bytes):
        assert len(witness) == 32
        return DleqProofResult(
            proof=self._proof,
            commitment_secp256k1=self._secp,
            commitment_ed25519=self._ed,
        )

    def verify(self, proof: bytes, *, expect_secp256k1: str, expect_ed25519: str) -> DleqVerdict:
        assert proof == self._proof
        reported_secp = self._verdict.secp if self._verdict.secp is not None else self._secp
        reported_ed = self._verdict.ed if self._verdict.ed is not None else self._ed
        matches = (
            reported_secp.lower() == expect_secp256k1.lower()
            and reported_ed.lower() == expect_ed25519.lower()
        )
        verified = self._verdict.verified and matches
        return DleqVerdict(
            verified=verified,
            commitment_secp256k1=reported_secp,
            commitment_ed25519=reported_ed,
            reason="" if verified else self._verdict.reason,
        )


def _commitment_for(share: int, view_share: int, proof: bytes = b"\x00" * 16) -> ShareCommitment:
    """A well-formed commitment for a known share, built WITHOUT a helper.

    Used by the tests that are about admissibility and address derivation rather than about
    the proof, so they run with no binary present.
    """
    return ShareCommitment(
        adaptor_point=adaptor_ecdsa.point_to_bytes(adaptor_ecdsa.public_key_point(share)).hex(),
        spend_public=monero_keys.public_key_for_share(share).hex(),
        view_public=monero_keys.public_key_for_share(view_share).hex(),
        dleq_proof=proof,
    )


def _order_two_point() -> Point:
    """(0, -1): a point of order 2. Canonical, on the curve, not the identity, not in the
    prime-order subgroup. Derived rather than pasted as a constant, so it cannot drift from
    the field prime this module actually uses."""
    return Point.from_affine(0, FIELD_PRIME - 1)


# ---------------------------------------------------------------------------------------
# The share range. Tighter than Monero's own l-bound, because the cross-curve proof's
# statement is only well-formed below 2^252.
# ---------------------------------------------------------------------------------------


def test_the_share_bound_is_two_to_the_252_and_not_the_ed25519_group_order():
    """The bound is 2^252, which is BELOW l -- so a share Monero would accept is refused.

    This is the difference that matters and it is asserted as a behavioral one rather than
    as a comparison of two constants: `GROUP_ORDER - 1` is a perfectly good Monero scalar
    and `monero_keys.shared_private_spend_key` takes it, while this protocol refuses it,
    because above 2^252 it cannot be proven to be the same scalar on both curves.
    """
    assert SHARE_UPPER_BOUND < GROUP_ORDER
    assert require_share_in_range(SHARE_UPPER_BOUND - 1) == SHARE_UPPER_BOUND - 1
    assert require_share_in_range(1) == 1
    with pytest.raises(ShareRangeError):
        require_share_in_range(SHARE_UPPER_BOUND)
    with pytest.raises(ShareRangeError):
        require_share_in_range(GROUP_ORDER - 1)
    # And the looser bound really is looser, asserted rather than asserted-with-an-`or True`:
    # `l - 1` is a share `monero_keys` accepts, and `(l - 1) + 1 == l == 0 mod l`. So the
    # value this protocol refuses is one Monero arithmetic handles without complaint, which
    # is the whole reason the tighter bound has to live somewhere and be tested.
    assert monero_keys.shared_private_spend_key(GROUP_ORDER - 1, 1) == 0


def test_a_zero_share_is_refused_as_a_protocol_violation_and_the_message_says_why():
    """Zero is the one value that hands the counterparty the whole spend key.

    `s = s_a + s_b`, so a zero share makes `s` equal the other party's share alone. The
    refusal has to say that, because a reader who sees only "out of range" will be tempted
    to clamp it.
    """
    with pytest.raises(ShareRangeError) as raised:
        require_share_in_range(0, "spend share")
    message = str(raised.value)
    assert "zero" in message
    assert "whole key" in message
    assert "not corrected" in message


def test_a_negative_share_and_an_oversized_share_are_both_refused_by_bit_length_only():
    """Both are refused, and NEITHER refusal contains the value.

    The share is a spend-key share and an exception message reaches logs, tracebacks and
    bug reports. A bit length is what a caller needs to see that its sampling is wrong and
    is 252 bits short of being the key, so the message carries that and nothing else.
    """
    distinctive = SHARE_UPPER_BOUND + 0xDEADBEEFCAFEF00D
    with pytest.raises(ShareRangeError) as raised:
        require_share_in_range(distinctive, "spend share")
    message = str(raised.value)
    assert str(distinctive) not in message
    assert f"{distinctive:x}" not in message
    assert "deadbeefcafef00d" not in message.lower()
    assert str(distinctive.bit_length()) in message

    with pytest.raises(ShareRangeError) as negative:
        require_share_in_range(-distinctive, "spend share")
    assert str(distinctive) not in str(negative.value)
    assert "negative" in str(negative.value)


def test_a_bool_is_refused_although_bool_is_a_subclass_of_int():
    """`True` would otherwise pass as the share 1 and produce a valid proof about it.

    Not a hypothetical shape of bug: a flag threaded into the wrong parameter is exactly how
    a one-scalar share arrives, and one is a scalar the arithmetic accepts. The refusal has
    to name the TYPE, because the fix is at the call site.
    """
    with pytest.raises(ShareRangeError) as raised:
        require_share_in_range(True, "spend share")
    assert "bool" in str(raised.value)
    with pytest.raises(ShareRangeError):
        require_share_in_range("1")  # type: ignore[arg-type]
    with pytest.raises(ShareRangeError):
        require_share_in_range(b"\x01" * 32)  # type: ignore[arg-type]


def test_sampled_shares_are_in_range_and_two_samples_differ():
    """Fresh randomness per call, and inside the bound.

    Two samples differing is a weak assertion on its own -- it would pass for a counter --
    and it is here for the failure it actually catches, which is a module-level constant or
    a cached value being returned twice. The range assertion is the strong one.
    """
    first = sample_shares()
    second = sample_shares()
    for shares in (first, second):
        assert 0 < shares.spend < SHARE_UPPER_BOUND
        assert 0 < shares.view < SHARE_UPPER_BOUND
    assert (first.spend, first.view) != (second.spend, second.view)


def test_private_shares_refuses_an_out_of_range_value_at_construction():
    """The check is on the object, not only on the free function, so no caller can skip it."""
    with pytest.raises(ShareRangeError):
        PrivateShares(spend=SHARE_UPPER_BOUND, view=1)
    with pytest.raises(ShareRangeError):
        PrivateShares(spend=1, view=0)


# ---------------------------------------------------------------------------------------
# Redaction. The behavioral form of "never print a key".
# ---------------------------------------------------------------------------------------


def test_neither_repr_nor_str_nor_an_f_string_of_private_shares_contains_a_share():
    """The values must not appear in ANY of the four ways a share reaches a log.

    A frozen dataclass's generated `__repr__` interpolates every field, so the default
    would put both shares into any traceback that mentions the object, any `print()` added
    while debugging, any `logger.info(f"{shares}")`, and any pytest assertion-failure
    message. Two distinctive values are used so a partial redaction -- one field covered
    and not the other -- fails rather than passing.
    """
    spend = 0x1234567890ABCDEF1234567890ABCDEF1234567890ABCDEF1234567890AB
    view = 0x0FEDCBA0987654321FEDCBA0987654321FEDCBA0987654321FEDCBA098765
    shares = PrivateShares(spend=spend, view=view)

    # Four renderings and not five: f"{shares}" already goes through __format__, which falls
    # back to __str__, so a "{}".format(shares) or a "%s" entry would be the same path
    # spelled again -- and ruff rejects both spellings anyway (UP031, UP032).
    renderings = [repr(shares), str(shares), f"{shares}", f"{shares!r}"]
    for rendering in renderings:
        for value in (spend, view):
            assert str(value) not in rendering
            assert f"{value:x}" not in rendering
            assert f"{value:X}" not in rendering
        assert "redacted" in rendering

    # And the object still identifies its own type, so a redacted repr is not an empty one.
    assert "PrivateShares" in repr(shares)


# ---------------------------------------------------------------------------------------
# Counterparty share admissibility. Four refusals, each a way to be robbed.
# ---------------------------------------------------------------------------------------


def test_a_well_formed_commitment_is_accepted():
    share, view = 0x51DE, 0x77AC
    helper = FakeHelper(
        proof=b"proof-bytes",
        secp=adaptor_ecdsa.point_to_bytes(adaptor_ecdsa.public_key_point(share)).hex(),
        ed=monero_keys.public_key_for_share(share).hex(),
    )
    verify_share_commitment(helper, _commitment_for(share, view, proof=b"proof-bytes"))


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("adaptor_point", "02" + "ab" * 31),          # 64 hex, one byte short
        ("adaptor_point", "02" + "ab" * 33),          # one byte long
        ("spend_public", "ab" * 31),
        ("view_public", "ab" * 33),
    ],
)
def test_a_point_of_the_wrong_length_is_refused_before_any_curve_arithmetic(field, replacement):
    """Length first, so the error names the FIELD rather than surfacing from a decoder."""
    share, view = 0x51DE, 0x77AC
    commitment = _commitment_for(share, view)
    broken = ShareCommitment(**{**commitment.__dict__, field: replacement})
    with pytest.raises(ShareRejected) as raised:
        verify_share_commitment(FakeHelper(b"", "", ""), broken)
    assert "hex characters" in str(raised.value)


def test_a_non_hex_public_share_is_refused():
    share, view = 0x51DE, 0x77AC
    commitment = _commitment_for(share, view)
    broken = ShareCommitment(**{**commitment.__dict__, "spend_public": "zz" * 32})
    with pytest.raises(ShareRejected) as raised:
        verify_share_commitment(FakeHelper(b"", "", ""), broken)
    assert "not hex" in str(raised.value)


def test_the_identity_as_a_spend_share_is_refused():
    """A share of zero makes the shared key the other party's share alone.

    The private side of this is caught by `require_share_in_range`; this is the public side,
    arriving over the wire from a counterparty who never reveals a private value at all.
    Both directions have to be closed and they are closed in different functions.
    """
    share, view = 0x51DE, 0x77AC
    commitment = _commitment_for(share, view)
    broken = ShareCommitment(
        **{**commitment.__dict__, "spend_public": Point.identity().compress().hex()}
    )
    with pytest.raises(ShareRejected) as raised:
        verify_share_commitment(FakeHelper(b"", "", ""), broken)
    assert "identity" in str(raised.value)


def test_a_spend_share_with_a_torsion_component_is_refused():
    """B + (0,-1) is canonical, on the curve, not the identity, and NOT in the subgroup.

    That combination is the whole reason a torsion check is a separate call: the point
    passes every cheaper test. Eight different scalars become indistinguishable to a
    verifier that does not check, and on a cross-curve DLEQ that is exactly the failure the
    proof exists to prevent.
    """
    share, view = 0x51DE, 0x77AC
    tainted = BASEPOINT.add(_order_two_point())
    commitment = _commitment_for(share, view)
    broken = ShareCommitment(**{**commitment.__dict__, "spend_public": tainted.compress().hex()})
    with pytest.raises(ShareRejected) as raised:
        verify_share_commitment(FakeHelper(b"", "", ""), broken)
    assert "torsion" in str(raised.value)


def test_a_non_canonical_encoding_is_refused():
    """y == p is not a valid ed25519 encoding, and curve25519-dalek rejects it too."""
    share, view = 0x51DE, 0x77AC
    commitment = _commitment_for(share, view)
    broken = ShareCommitment(
        **{**commitment.__dict__, "spend_public": FIELD_PRIME.to_bytes(32, "little").hex()}
    )
    with pytest.raises(ShareRejected) as raised:
        verify_share_commitment(FakeHelper(b"", "", ""), broken)
    assert "canonical" in str(raised.value)


def test_a_torsion_component_on_the_VIEW_share_is_refused_too():
    """The view share decides nothing about spending, and it is still checked.

    A view share that does not pin one point means the two parties can watch different
    addresses, and "I cannot see the lock" is indistinguishable from "the lock was never
    funded" -- which is the ambiguity that gets a swap abandoned with money in it.
    """
    share, view = 0x51DE, 0x77AC
    commitment = _commitment_for(share, view)
    broken = ShareCommitment(
        **{**commitment.__dict__, "view_public": BASEPOINT.add(_order_two_point()).compress().hex()}
    )
    with pytest.raises(ShareRejected) as raised:
        verify_share_commitment(FakeHelper(b"", "", ""), broken)
    assert "view" in str(raised.value)
    assert "torsion" in str(raised.value)


def test_a_proof_about_different_keys_is_refused_although_it_verifies():
    """THE CHECK WHOSE ABSENCE LOOKS EXACTLY LIKE A WORKING VERIFIER.

    The helper here reports `verified` for a proof whose commitments are some OTHER pair of
    keys. That is a true statement about somebody else's keys and it is no evidence at all
    about the keys the caller holds. A verifier that accepted it would let a counterparty
    reuse any valid proof from anywhere.
    """
    share, view, other = 0x51DE, 0x77AC, 0x9001
    helper = FakeHelper(
        proof=b"proof-bytes",
        secp=adaptor_ecdsa.point_to_bytes(adaptor_ecdsa.public_key_point(share)).hex(),
        ed=monero_keys.public_key_for_share(share).hex(),
        verdict=FakeVerdict(
            secp=adaptor_ecdsa.point_to_bytes(adaptor_ecdsa.public_key_point(other)).hex(),
            ed=monero_keys.public_key_for_share(other).hex(),
            reason="about other keys",
        ),
    )
    with pytest.raises(ShareRejected) as raised:
        verify_share_commitment(helper, _commitment_for(share, view, proof=b"proof-bytes"))
    assert "same discrete logarithm" in str(raised.value)


def test_a_verdict_of_false_is_refused_and_the_reason_is_carried_through():
    share, view = 0x51DE, 0x77AC
    helper = FakeHelper(
        proof=b"proof-bytes",
        secp=adaptor_ecdsa.point_to_bytes(adaptor_ecdsa.public_key_point(share)).hex(),
        ed=monero_keys.public_key_for_share(share).hex(),
        verdict=FakeVerdict(verified=False, reason="the ring signature at bit 7 does not close"),
    )
    with pytest.raises(ShareRejected) as raised:
        verify_share_commitment(helper, _commitment_for(share, view, proof=b"proof-bytes"))
    assert "bit 7" in str(raised.value)


# ---------------------------------------------------------------------------------------
# commit_shares cross-checks the helper against this repo's own derivations.
# ---------------------------------------------------------------------------------------


def test_commit_shares_refuses_a_helper_whose_secp256k1_commitment_disagrees():
    """A helper that answers about a different scalar, or with a different encoding, is refused.

    Without this the proof goes out and the counterparty's verify fails for a reason neither
    end can diagnose -- which reads as "the swap is broken" rather than "these two
    implementations disagree about a byte order".
    """
    shares = PrivateShares(spend=0x51DE, view=0x77AC)
    wrong = adaptor_ecdsa.point_to_bytes(adaptor_ecdsa.public_key_point(0x9001)).hex()
    helper = FakeHelper(
        proof=b"p", secp=wrong, ed=monero_keys.public_key_for_share(shares.spend).hex()
    )
    with pytest.raises(ProtocolError) as raised:
        commit_shares(helper, shares)
    assert "secp256k1" in str(raised.value)


def test_commit_shares_refuses_a_helper_whose_ed25519_commitment_disagrees():
    shares = PrivateShares(spend=0x51DE, view=0x77AC)
    helper = FakeHelper(
        proof=b"p",
        secp=adaptor_ecdsa.point_to_bytes(adaptor_ecdsa.public_key_point(shares.spend)).hex(),
        ed=monero_keys.public_key_for_share(0x9001).hex(),
    )
    with pytest.raises(ProtocolError) as raised:
        commit_shares(helper, shares)
    assert "ed25519" in str(raised.value)


# ---------------------------------------------------------------------------------------
# The lock address.
# ---------------------------------------------------------------------------------------


def test_both_parties_compute_the_same_lock_address_from_the_same_four_public_shares():
    """Point addition commutes, and the whole protocol rests on the two sides agreeing.

    An address they disagree about is money sent where one of them cannot see it, which is
    why this is asserted rather than left to the reader.
    """
    mine = _commitment_for(0x51DE, 0x77AC)
    theirs = _commitment_for(0x9001, 0xB00B)
    assert lock_address(TEST_NETWORK, mine, theirs) == lock_address(TEST_NETWORK, theirs, mine)


def test_a_different_share_gives_a_different_lock_address():
    """The address depends on every input, so a substituted share cannot be silent."""
    mine = _commitment_for(0x51DE, 0x77AC)
    theirs = _commitment_for(0x9001, 0xB00B)
    other_spend = _commitment_for(0x9002, 0xB00B)
    other_view = _commitment_for(0x9001, 0xB00C)
    base = lock_address(TEST_NETWORK, mine, theirs)
    assert lock_address(TEST_NETWORK, mine, other_spend) != base
    assert lock_address(TEST_NETWORK, mine, other_view) != base


def test_the_lock_address_decodes_on_the_network_it_was_built_for():
    """Built and then read back through this repo's own decoder, not merely formatted."""
    address = lock_address("stagenet", _commitment_for(0x51DE, 0x77AC), _commitment_for(0x9001, 3))
    decoded = monero_keys.decode_address(address)
    assert decoded.network == "stagenet"
    assert decoded.kind == "primary"
    # And the keys read back out are the summed ones, not merely a well-formed string.
    assert decoded.public_spend_key == monero_keys.shared_public_key(
        monero_keys.public_key_for_share(0x51DE), monero_keys.public_key_for_share(0x9001)
    )


# ---------------------------------------------------------------------------------------
# The withholding gate. The first funder's only protection in steps 1 through 3.
# ---------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("confirmed", "unlocked", "expected"),
    [
        (False, False, False),
        (False, True, False),
        (True, False, False),
        (True, True, True),
    ],
)
def test_the_redeem_presignature_is_released_only_when_the_lock_is_confirmed_AND_unlocked(
    confirmed, unlocked, expected
):
    """BOTH conditions, and the truth table is exhaustive because an OR here loses a leg.

    Released before the Monero lock exists, the counterparty takes the script-chain coin
    having funded nothing -- the first funder recovers a scalar that opens an empty address.
    Released on confirmations alone, it is released against a lock that is NOT spendable:
    measured on the operator's host 2026-09-27, a transfer with 3 confirmations and
    unlock_time=0 still reported locked=True and contributed 0 to unlocked_balance.
    """
    allowed, reason = redeem_presignature_may_be_released(
        xmr_lock_confirmed=confirmed, xmr_lock_unlocked=unlocked
    )
    assert allowed is expected
    assert reason
    if not expected:
        assert "funded nothing" in reason or "not spendable" in reason.replace("NOT", "not")


def test_the_refusal_for_a_confirmed_but_locked_output_names_the_ten_block_lock():
    """The operator reads the screen, not the source: the number has to be in the reason."""
    _, reason = redeem_presignature_may_be_released(
        xmr_lock_confirmed=True, xmr_lock_unlocked=False
    )
    assert str(XMR_OUTPUT_LOCK_BLOCKS) in reason
    assert "locked=True" in reason


# ---------------------------------------------------------------------------------------
# Recovery. The step that converts the swap into money.
# ---------------------------------------------------------------------------------------


def _adaptor_round_trip(share: int, signing_key: int):
    """A real pre-signature over REDEEM_DIGEST under `share`'s adaptor point, completed."""
    adaptor_point = adaptor_ecdsa.public_key_point(share)
    pre_signature = adaptor_ecdsa.pre_sign(signing_key, REDEEM_DIGEST, adaptor_point)
    assert adaptor_ecdsa.pre_verify(
        adaptor_ecdsa.public_key_point(signing_key), REDEEM_DIGEST, adaptor_point, pre_signature
    )
    return pre_signature, adaptor_ecdsa.adapt(pre_signature, share)


def test_the_recovered_share_equals_the_witness_and_its_ed25519_key_is_the_committed_one():
    """The join, end to end, with no helper: recover, then confirm it opens the same key."""
    share = secrets.randbelow(SHARE_UPPER_BOUND) or 1
    signing_key = secrets.randbelow(adaptor_ecdsa.CURVE.order - 1) + 1
    pre_signature, signature = _adaptor_round_trip(share, signing_key)
    expected = monero_keys.public_key_for_share(share).hex()
    assert recover_counterparty_spend_share(pre_signature, signature, expected) == share


def test_recovery_works_when_the_low_s_negation_fired_and_when_it_did_not():
    """BOTH branches of `recover_adaptor_secret` are exercised, not whichever one came up.

    `adapt()` negates `s` for BIP62 low-S, which sends about half of all real swaps through
    the `-Y` branch of recovery. Measured 6 of 12 on 2026-09-27. A test that took one random
    trial would pass on a recovery that handled only one branch, and the other half of real
    swaps would fail -- a coin flip for money. So this loops until it has seen both.
    """
    seen_negated = False
    seen_plain = False
    order = adaptor_ecdsa.CURVE.order
    for _ in range(40):
        share = secrets.randbelow(SHARE_UPPER_BOUND) or 1
        signing_key = secrets.randbelow(order - 1) + 1
        pre_signature, signature = _adaptor_round_trip(share, signing_key)
        expected = monero_keys.public_key_for_share(share).hex()
        assert recover_counterparty_spend_share(pre_signature, signature, expected) == share
        if (pre_signature.s_a * pow(signature[1], -1, order)) % order == share:
            seen_plain = True
        else:
            seen_negated = True
        if seen_plain and seen_negated:
            break
    assert seen_plain, "40 trials produced no un-negated signature; the sampling is wrong"
    assert seen_negated, "40 trials produced no negated signature; the sampling is wrong"


def test_an_unrelated_signature_recovers_nothing_and_the_refusal_says_not_to_proceed():
    """`recover_adaptor_secret` returns None here, and None must become a refusal.

    The three situations it covers -- an unrelated r, a mangled s, an out-of-range value --
    are one answer on purpose, because the caller's action is the same in each: treat the
    counterparty's signature as unrelated and do NOT go on to reconstruct a spend key.
    """
    share = secrets.randbelow(SHARE_UPPER_BOUND) or 1
    signing_key = secrets.randbelow(adaptor_ecdsa.CURVE.order - 1) + 1
    pre_signature, _ = _adaptor_round_trip(share, signing_key)
    expected = monero_keys.public_key_for_share(share).hex()
    with pytest.raises(ProtocolError) as raised:
        recover_counterparty_spend_share(pre_signature, (12345, 67890), expected)
    assert "do not" in str(raised.value).lower()


def test_a_recovered_scalar_that_is_not_the_discrete_log_of_the_COMMITTED_ed25519_key_is_refused():
    """THE CROSS-CURVE CHECK AT THE POINT OF USE, and it is not redundant.

    `recover_adaptor_secret` guarantees only that the scalar multiplies the secp256k1
    generator to the adaptor point. The ed25519 half comes from the DLEQ, verified at a
    different time in a different function. Here the recovery is handed the public share of
    a DIFFERENT scalar -- which is what a skipped verification, a verification against the
    wrong key, or a commitment replaced after checking all look like -- and the refusal has
    to fire, because without it the scalar flows into `reconstruct_spend_key` and the first
    sign of trouble is a reconstructed wallet with a zero balance and no explanation.
    """
    share = secrets.randbelow(SHARE_UPPER_BOUND) or 1
    other = share + 1 if share + 1 < SHARE_UPPER_BOUND else share - 1
    signing_key = secrets.randbelow(adaptor_ecdsa.CURVE.order - 1) + 1
    pre_signature, signature = _adaptor_round_trip(share, signing_key)
    with pytest.raises(ProtocolError) as raised:
        recover_counterparty_spend_share(
            pre_signature, signature, monero_keys.public_key_for_share(other).hex()
        )
    message = str(raised.value)
    assert "cross-curve" in message
    assert "opens nothing" in message


# ---------------------------------------------------------------------------------------
# Reconstruction, including the sum-reduction case section 3.2 calls silent.
# ---------------------------------------------------------------------------------------


def test_the_reconstructed_spend_key_opens_the_lock_and_the_addition_commutes():
    mine = secrets.randbelow(SHARE_UPPER_BOUND) or 1
    theirs = secrets.randbelow(SHARE_UPPER_BOUND) or 1
    key = reconstruct_spend_key(mine, theirs)
    assert key == reconstruct_spend_key(theirs, mine)
    summed_public = monero_keys.shared_public_key(
        monero_keys.public_key_for_share(mine), monero_keys.public_key_for_share(theirs)
    )
    assert scalar_base_mul(key).compress() == summed_public


def test_a_share_pair_whose_sum_REDUCES_mod_l_still_opens_the_lock():
    """Section 3.2's silent case, pinned deliberately rather than left to the random seed.

    Measured in this container 2026-09-27: 961 of 2000 random share pairs from [1, 2^252)
    sum to at least l, so the sum reduces on ed25519 about half the time while the
    corresponding secp256k1 sum does not reduce at all. A test with small fixtures never
    reaches this case and passes; these two shares are chosen so that it always does.

    What must hold is that the ED25519 reconstruction is still correct -- the reduction is
    the right behavior there -- and what must NEVER be relied on is any secp256k1 sum,
    which is asserted here as an inequality so that a future change deriving one from the
    other fails this test.
    """
    mine = SHARE_UPPER_BOUND - 5
    theirs = SHARE_UPPER_BOUND - 9
    assert mine + theirs >= GROUP_ORDER, "this fixture must exercise the reducing case"

    key = reconstruct_spend_key(mine, theirs)
    assert key == (mine + theirs) - GROUP_ORDER
    summed_public = monero_keys.shared_public_key(
        monero_keys.public_key_for_share(mine), monero_keys.public_key_for_share(theirs)
    )
    assert scalar_base_mul(key).compress() == summed_public

    # The secp256k1 sum commits to the UNREDUCED integer, so it is a commitment to a
    # different scalar than the Monero spend key. This inequality is the guard.
    secp_sum = adaptor_ecdsa.public_key_point(mine) + adaptor_ecdsa.public_key_point(theirs)
    assert secp_sum != adaptor_ecdsa.public_key_point(key)
    assert secp_sum == adaptor_ecdsa.public_key_point(mine + theirs)


def test_reconstruction_range_checks_the_counterparty_share_it_was_handed():
    """That share came out of a signature a counterparty broadcast, so it is external data."""
    with pytest.raises(ShareRangeError):
        reconstruct_spend_key(1, SHARE_UPPER_BOUND)
    with pytest.raises(ShareRangeError):
        reconstruct_spend_key(1, 0)


# ---------------------------------------------------------------------------------------
# Timelocks, in seconds, across three block intervals.
# ---------------------------------------------------------------------------------------


def test_the_monero_lock_wait_is_the_max_of_the_confirmations_and_the_ten_block_lock():
    """`max`, not the sum: both counts run from the same transaction, so adding double-counts.

    And asking for fewer than ten confirmations does not make the output spendable sooner,
    which is the asymmetry the max expresses.
    """
    assert xmr_lock_wait_seconds(3) == XMR_OUTPUT_LOCK_BLOCKS * XMR_SECONDS_PER_BLOCK
    assert xmr_lock_wait_seconds(XMR_OUTPUT_LOCK_BLOCKS) == 10 * XMR_SECONDS_PER_BLOCK
    assert xmr_lock_wait_seconds(20) == 20 * XMR_SECONDS_PER_BLOCK
    # Not the sum, stated as the assertion a future "obvious" change would break.
    assert xmr_lock_wait_seconds(3) != (3 + XMR_OUTPUT_LOCK_BLOCKS) * XMR_SECONDS_PER_BLOCK
    with pytest.raises(ProtocolError):
        xmr_lock_wait_seconds(-1)


def test_script_chain_confirmation_time_uses_the_one_shared_table_and_refuses_XMR():
    """Monero is the OTHER leg and has no timelock, so it must not be answerable here.

    A default would price a Bitcoin confirmation at Gridcoin's 90 seconds -- wrong by about
    a factor of seven, in the direction that makes every margin look satisfied.
    """
    for asset, seconds in SECONDS_PER_BLOCK.items():
        assert s_chain_confirmation_seconds(asset, 6) == 6 * seconds
    with pytest.raises(ProtocolError) as raised:
        s_chain_confirmation_seconds("XMR", 6)
    assert "no timelock" in str(raised.value)
    with pytest.raises(ProtocolError):
        s_chain_confirmation_seconds("DOGE", 6)


def _safe_plan(**overrides) -> TimelockPlan:
    """A plan with generous margins on both comparisons, for a test to spoil one field of."""
    base = {
        "s_chain_asset": "GRC",
        "seconds_until_cancel": 48 * 3600,
        "seconds_until_punish": 96 * 3600,
        "xmr_confirmations": 10,
        "redeem_confirmations": 6,
        "cancel_confirmations": 6,
        "offline_allowance_seconds": 12 * 3600,
        "margin_seconds": 3600,
    }
    base.update(overrides)
    return TimelockPlan(**base)


def test_a_generous_plan_is_accepted():
    """A plan with room on both comparisons is accepted, and the acceptance is ASSERTED.

    This line read `assert_timelock_ordering(_safe_plan()) is None` when it was written --
    a bare comparison with no `assert`, so it evaluated the call, discarded the answer and
    could not fail for any reason except an exception. Ruff's B015 found it. It is recorded
    here rather than quietly corrected because it is the same defect class the module
    docstring is about: a test that cannot fail is a green check beside nothing.
    """
    assert assert_timelock_ordering(_safe_plan()) is None


def test_hazard_2_a_cancel_timelock_too_close_to_fund_into_is_refused():
    """There must be time to fund the Monero leg, confirm it, UNLOCK it, and redeem.

    If T1 arrives first the cancel path opens with the Monero already locked, and the funder
    is left dependent on the counterparty refunding. They are never left with nothing, but
    they can be left holding the wrong asset -- which is not the trade they agreed to.
    """
    with pytest.raises(ProtocolError) as raised:
        assert_timelock_ordering(_safe_plan(seconds_until_cancel=600, seconds_until_punish=96 * 3600))
    message = str(raised.value)
    assert "REFUSING" in message
    assert "Nothing was funded" in message
    assert "unlock" in message


def test_hazard_1_a_punish_window_too_short_for_the_first_funder_to_react_is_refused():
    """`T2 - T1` must cover the cancel's confirmations plus the offline allowance.

    From the moment the script leg is funded, the counterparty holds plain signatures on
    cancel and punish and can take the coin by publishing them in sequence. The only defense
    is publishing the refund in between, which requires being awake -- so this comparison is
    the first funder's liveness obligation written as a number.
    """
    with pytest.raises(ProtocolError) as raised:
        assert_timelock_ordering(
            _safe_plan(seconds_until_cancel=48 * 3600, seconds_until_punish=48 * 3600 + 600)
        )
    message = str(raised.value)
    assert "punish" in message
    assert "unresponsive" in message
    assert "Nothing was funded" in message


def test_a_punish_timelock_at_or_before_the_cancel_timelock_is_refused():
    """T2 <= T1 collapses the window to nothing or inverts it."""
    with pytest.raises(ProtocolError):
        assert_timelock_ordering(_safe_plan(seconds_until_punish=48 * 3600))
    with pytest.raises(ProtocolError):
        assert_timelock_ordering(_safe_plan(seconds_until_punish=24 * 3600))


def test_a_zero_or_negative_margin_is_refused():
    """A comparison with no margin is a comparison one slow block breaks."""
    for margin in (0, -1):
        with pytest.raises(ProtocolError) as raised:
            assert_timelock_ordering(_safe_plan(margin_seconds=margin))
        assert "margin" in str(raised.value)


def test_the_same_block_counts_are_judged_differently_on_two_chains_because_the_unit_is_TIME():
    """The incident this whole check exists because of, in one assertion.

    `atomic_swap.py::assert_ordering` compared BLOCKS REMAINING across two chains and
    refused a correctly built BTC/LTC swap on the operator's first real run, 2026-09-27,
    because Litecoin needs four times the blocks for half the time. Here the plan is
    IDENTICAL in every field except the chain: 6 confirmations is 540s on Gridcoin and
    3600s on Bitcoin, so a window that fits on one does not fit on the other. A check that
    compared block counts would accept or refuse both together, and would be wrong once.
    """
    # Computed, not guessed. The first comparison needs
    # xmr_lock_wait(3) = 10 blocks * 120s = 1200s, plus 6 script-chain confirmations, plus
    # the 600s margin:
    #     GRC   1200 + 6*90  = 540  + 600 = 2340s needed
    #     BTC   1200 + 6*600 = 3600 + 600 = 5400s needed
    # so a 4000s window before T1 clears Gridcoin by 1660s and misses Bitcoin by 1400s.
    # The second comparison is satisfied on BOTH chains (GRC needs 4740s, BTC 7800s,
    # against a 9000s punish window), so the difference in verdict comes from the first
    # comparison alone and cannot be an accident of the second.
    fields = {
        "seconds_until_cancel": 4000,
        "seconds_until_punish": 4000 + 9000,
        "xmr_confirmations": 3,
        "redeem_confirmations": 6,
        "cancel_confirmations": 6,
        "offline_allowance_seconds": 3600,
        "margin_seconds": 600,
    }
    assert_timelock_ordering(_safe_plan(s_chain_asset="GRC", **fields))
    with pytest.raises(ProtocolError):
        assert_timelock_ordering(_safe_plan(s_chain_asset="BTC", **fields))


def test_every_timelock_refusal_reports_in_microfortnights_with_the_micro_sign():
    """Rule 6: the character is µ (U+00B5), never an ASCII u, and never with a space before it.

    Asserted on the refusal a reader actually sees, over both comparisons, because an ASCII
    "u" in displayed output is a defect in this repo the same as a wrong number would be.
    """
    messages = []
    for plan in (
        _safe_plan(seconds_until_cancel=600),
        _safe_plan(seconds_until_punish=48 * 3600 + 600),
    ):
        with pytest.raises(ProtocolError) as raised:
            assert_timelock_ordering(plan)
        messages.append(str(raised.value))
    for message in messages:
        assert "µfn" in message
        assert "ufn" not in message
        assert " µfn" not in message
        assert " s)" not in message


# ---------------------------------------------------------------------------------------
# Hazard 3: the one both-legs outcome.
# ---------------------------------------------------------------------------------------


def test_the_redeem_may_be_broadcast_when_the_cancel_timelock_is_far_away():
    ok, reason = redeem_is_safe_to_broadcast(
        seconds_until_cancel=48 * 3600,
        s_chain_asset="GRC",
        redeem_confirmations=6,
        margin_seconds=3600,
    )
    assert ok is True
    assert reason


def test_the_redeem_is_REFUSED_near_the_cancel_timelock_and_the_refusal_names_both_legs():
    """The one step where a single party can end up holding both legs, said plainly.

    Broadcasting publishes the spend share. If the redeem is then re-orged out and the
    cancel confirms instead, the counterparty holds BOTH shares and its own coin. The
    refusal has to say that, because the alternative -- taking the cancel path -- costs the
    trade and not the asset, and a caller has to know which it is choosing between.
    """
    ok, reason = redeem_is_safe_to_broadcast(
        seconds_until_cancel=600,
        s_chain_asset="GRC",
        redeem_confirmations=6,
        margin_seconds=3600,
    )
    assert ok is False
    assert "both legs" in reason
    assert "re-orged" in reason
    assert "cancel path instead" in reason
    assert "µfn" in reason


def test_the_redeem_broadcast_check_refuses_a_non_positive_margin():
    for margin in (0, -60):
        ok, reason = redeem_is_safe_to_broadcast(
            seconds_until_cancel=48 * 3600,
            s_chain_asset="GRC",
            redeem_confirmations=6,
            margin_seconds=margin,
        )
        assert ok is False
        assert "margin" in reason


def test_the_redeem_broadcast_check_is_chain_aware():
    """Identical seconds and confirmations, different chain, different answer."""
    # 6 confirmations plus the 600s margin is 1140s on Gridcoin and 4200s on Bitcoin, so a
    # 2000s window before T1 is safe on one and a refusal on the other.
    arguments = {"seconds_until_cancel": 2000, "redeem_confirmations": 6, "margin_seconds": 600}
    assert redeem_is_safe_to_broadcast(s_chain_asset="GRC", **arguments)[0] is True
    assert redeem_is_safe_to_broadcast(s_chain_asset="BTC", **arguments)[0] is False


# ---------------------------------------------------------------------------------------
# The full composition, against the real Go helper.
# ---------------------------------------------------------------------------------------


@helper_required
def test_the_offline_rehearsal_establishes_every_property_it_claims():
    """The composition, end to end, with the real cross-curve proof and no chain.

    Every field asserted individually rather than as one boolean, so a failure says WHICH
    property stopped holding. `spend_key_opens_lock` is the one the regtest sweep
    demonstrated on a chain and it is the last line of the chain of reasoning: the
    reconstructed private key's public key equals the summed public spend key the lock
    address was built from.

    WHAT THIS DOES NOT ESTABLISH, stated here because a green test is where the over-reading
    happens: both sides are played by one process, so nothing here tests that a counterparty
    who is not the prover reaches the same verdict, that the Monero lock can be funded or
    unlocked, that the reconstructed key imports into a wallet, or that any script-chain
    transaction exists to carry the pre-signature -- and that last one is absent from this
    tree entirely.
    """
    with DleqHelper() as helper:
        result = rehearse(helper, TEST_NETWORK, REDEEM_DIGEST)

    assert result.dleq_verified_both_ways is True
    assert result.presignature_verified is True
    assert result.recovered_share_matches_commitment is True
    assert result.spend_key_opens_lock is True
    assert monero_keys.decode_address(result.lock_address).network == TEST_NETWORK
    # The proof is NOT a constant length: the secp256k1 signature inside it is DER-encoded
    # and runs 70 to 72 bytes, so the total ranges over 64,966 to 64,968. A test asserting
    # one number would fail about two thirds of the time.
    for size in (result.proof_bytes_initiator, result.proof_bytes_participant):
        assert 64_966 <= size <= 64_968


@helper_required
def test_a_real_commitment_survives_a_real_verification_and_a_tampered_one_does_not():
    """The honest path and the tampered path, both through the real binary.

    Flipping one byte of a 64,967-byte proof must be refused. This is the assertion the Go
    implementation's own test suite does not have -- measured: go-dleq/dleq_test.go is 78
    lines of round-trip and witness-size checks with no negative test at all -- so it is the
    one place this repo is checking something its oracle does not.
    """
    shares = sample_shares()
    with DleqHelper() as helper:
        commitment = commit_shares(helper, shares)
        verify_share_commitment(helper, commitment)

        tampered = bytearray(commitment.dleq_proof)
        tampered[100] ^= 0x01
        broken = ShareCommitment(**{**commitment.__dict__, "dleq_proof": bytes(tampered)})
        with pytest.raises(ShareRejected):
            verify_share_commitment(helper, broken)


@helper_required
def test_a_real_proof_presented_for_someone_elses_keys_is_refused():
    """Two honest proofs, and each is offered against the other's keys. Both must be refused.

    This is the reuse attack the `expect_*` arguments exist for, run against the real
    verifier: every proof here genuinely verifies, and neither is evidence about the keys it
    is presented with.
    """
    first_shares = sample_shares()
    second_shares = sample_shares()
    with DleqHelper() as helper:
        first = commit_shares(helper, first_shares)
        second = commit_shares(helper, second_shares)

        swapped = ShareCommitment(**{**first.__dict__, "dleq_proof": second.dleq_proof})
        with pytest.raises(ShareRejected) as raised:
            verify_share_commitment(helper, swapped)
        assert "same discrete logarithm" in str(raised.value)


@helper_required
def test_the_proofs_two_claimed_keys_are_exactly_the_adaptor_point_and_the_monero_share():
    """The join is an IDENTITY, not a mapping, and that is worth pinning.

    go-dleq's claimed keys are `x*G` and `x*B` against the STANDARD basepoints -- its
    prove.go computes `XA = curveA.ScalarBaseMul(xA)` and uses `AltBasePoint()` only as the
    blinder generator. So the proof's own commitments are directly usable as the adaptor
    point and the Monero spend-share public key with no conversion step. If a future helper
    version changed which generator carried the value, every derived address and every
    adaptor point in this protocol would move, and this test is what would say so.
    """
    share = secrets.randbelow(SHARE_UPPER_BOUND) or 1
    with DleqHelper() as helper:
        result = helper.prove(witness_from_int(share))
    assert result.commitment_secp256k1 == adaptor_ecdsa.point_to_bytes(
        adaptor_ecdsa.public_key_point(share)
    ).hex()
    assert result.commitment_ed25519 == monero_keys.public_key_for_share(share).hex()
    assert result.commitment_ed25519 == scalar_base_mul(share).compress().hex()


@helper_required
def test_a_rehearsal_returns_no_private_value_anywhere_in_its_result():
    """A caller that prints a RehearsalResult must not be publishing a spend share.

    `monero_swap.py` prints one, so this is not a hypothetical caller. The rehearsal holds
    both parties' shares while it runs and reduces them to booleans before returning; this
    asserts the reduction by checking that every field is a bool, an int small enough to be
    a byte count, or the address -- there is no field wide enough to hold a 252-bit scalar.
    """
    with DleqHelper() as helper:
        result = rehearse(helper, TEST_NETWORK, REDEEM_DIGEST)
    rendered = repr(result)

    # No field is wide enough to hold a 252-bit scalar. A share is 76 decimal digits or 63
    # hex characters, so a run of 30 of either in the rendering is the signal, and 30 is
    # comfortably below a share while being above every legitimate number here (a byte count
    # is 5 digits and the address is base58, which excludes 0, O, I and l).
    assert not re.search(r"\d{30}", rendered), "a long decimal run appeared in a rehearsal result"
    assert not re.search(r"\b[0-9a-fA-F]{30,}\b", rendered), (
        "a long hex run appeared in a rehearsal result"
    )

    # And the same check on the values themselves rather than on their rendering, so a field
    # that held a scalar without printing it would still fail.
    for name, value in vars(result).items():
        if isinstance(value, bool):
            continue
        if isinstance(value, int):
            assert value < 1 << 32, f"{name} is wide enough to be a scalar"
        else:
            assert isinstance(value, str), f"{name} is neither a flag, a count nor a string"


# ---------------------------------------------------------------------------------------
# Which Monero network a daemon is on, and which prefix that implies. Not the same question.
# ---------------------------------------------------------------------------------------


@pytest.mark.parametrize("nettype", sorted(XMR_TEST_NETTYPES))
def test_every_test_nettype_is_accepted_and_the_reason_names_it(nettype):
    ok, reason = monero_network_is_test(nettype)
    assert ok is True
    assert nettype in reason


@pytest.mark.parametrize("nettype", ["mainnet", "MAINNET", "main", "", None, "prod"])
def test_mainnet_and_anything_unrecognized_or_missing_is_refused(nettype):
    """Including the empty and missing cases, which are the ones a default would swallow.

    A daemon that will not say which chain it is on is not one to lock funds on, and
    "no answer" must not be treated as "probably fine". `atomic_swap.py::chain_name` takes
    the same posture for the script chain: refuse rather than assume.
    """
    ok, reason = monero_network_is_test(nettype)
    assert ok is False
    assert reason


def test_the_nettype_check_is_case_and_whitespace_insensitive():
    """A daemon's JSON is not guaranteed to be normalized, and the answer must not hinge on it."""
    assert monero_network_is_test("  Stagenet ")[0] is True
    assert monero_network_is_test("FAKECHAIN")[0] is True


def test_regtest_addresses_carry_the_MAINNET_prefix_and_the_map_is_not_the_identity():
    """The one row of the prefix map that is not the identity, and it is a live trap.

    cryptonote_config.h:361 has `case FAKECHAIN: return mainnet;`, so there is no regtest
    prefix byte at all -- a regtest wallet's addresses decode as mainnet/primary, byte 18.
    A shared address built with any other prefix would not be recognized by the wallet that
    has to sweep it. This is asserted as a behavioral consequence: an address built for
    fakechain decodes as mainnet.
    """
    assert monero_address_network("fakechain") == "mainnet"
    assert monero_address_network("stagenet") == "stagenet"
    assert monero_address_network("testnet") == "testnet"
    assert monero_address_network("mainnet") == "mainnet"

    built = lock_address(
        monero_address_network("fakechain"), _commitment_for(0x51DE, 0x77AC), _commitment_for(0x9001, 3)
    )
    assert monero_keys.decode_address(built).network == "mainnet"


def test_an_unknown_nettype_has_no_prefix_default_and_refuses():
    """Neither a mainnet default (which would build a real-network address) nor a stagenet one
    (which would build an address no daemon accepts) is better than naming the value."""
    with pytest.raises(ProtocolError) as raised:
        monero_address_network("regtest")
    assert "regtest" in str(raised.value)
    with pytest.raises(ProtocolError):
        monero_address_network("")
