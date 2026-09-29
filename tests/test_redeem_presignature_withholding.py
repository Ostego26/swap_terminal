"""The withholding rule, and the observation it consumes.

Role: test (the security property that had no caller)
Reads: nothing. Every value is seeded here.
Writes: nothing.
Can send orders: no
Live-safe: yes

WHY THIS FILE EXISTS. Measured 2026-09-29 by grepping the tree for the NAME rather than
the import graph: `monero_swap_protocol.redeem_presignature_may_be_released` had no caller
anywhere -- not in production, not in the regtest harness, not even in `rehearse()`. Two
unit tests in tests/test_monero_swap_protocol.py exercised the predicate itself, and
nothing exercised it GUARDING anything, because nothing called it.

Meanwhile `adaptor_steps.step_6_build_and_hold` computed the redeem pre-signature before
Tx_lock was broadcast -- the earliest moment the code could manage, for the value the
protocol says must be withheld until the Monero lock is confirmed AND spendable.

So the tests here are about the JOIN between the gate and the construction, which is the
part that was missing. The predicate's own truth table is tested where the predicate lives
and is deliberately not repeated (rule 8: a second copy of a rule is a bug with a delay).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "swap_terminal"))

from chains.monero_transfers import MoneroLockState, lock_observation
from regtest import adaptor_join
from regtest.keys import generate_key

# 0.01 XMR, in piconero. A realistic swap amount rather than a round number, so the
# reason strings these produce are the ones an operator would actually read.
LOCK_AMOUNT = 10_000_000_000
DIGEST = bytes(range(32))


def _transfer(amount: int, *, locked: bool = False, unlock_time: int = 0, kind: str = "in") -> dict:
    return {
        "type": kind, "amount": amount, "txid": "c9cd65f2" * 8,
        "locked": locked, "unlock_time": unlock_time, "confirmations": 12,
    }


# ---------------------------------------------------------------------------
# The observation. This is the half that did not exist at all: the gate takes two
# booleans and nothing in the tree computed them from a chain.
# ---------------------------------------------------------------------------
def test_an_empty_lock_is_not_confirmed_and_says_the_amount_it_wanted():
    state = lock_observation([], LOCK_AMOUNT)
    assert state.confirmed is False
    assert state.unlocked is False
    assert "0.0 XMR" in state.reason and "0.01" in state.reason, (
        "the reason has to name BOTH what arrived and what was needed -- a refusal saying "
        "only 'not funded' sends the operator to look up the requirement (rule 14)"
    )


def test_a_lock_funded_short_of_the_amount_is_NOT_confirmed():
    """A dust payment is not a funded lock, and a gate that asked only 'did anything
    arrive' would release the redeem pre-signature against one."""
    state = lock_observation([_transfer(LOCK_AMOUNT // 2)], LOCK_AMOUNT)
    assert state.confirmed is False


def test_a_funded_but_LOCKED_transfer_is_confirmed_and_NOT_unlocked():
    """THE DISTINCTION THE PUBLISHED DOCUMENTATION GETS BACKWARDS, and the reason the gate
    takes two booleans rather than one. Measured on the operator's host 2026-09-27: a
    transfer with 3 confirmations and unlock_time=0 still reported locked=True and
    contributed 0 to unlocked_balance. Confirmed does not imply spendable."""
    state = lock_observation([_transfer(LOCK_AMOUNT, locked=True)], LOCK_AMOUNT)
    assert state.confirmed is True
    assert state.unlocked is False
    assert "spendable" in state.reason


def test_a_custom_unlock_time_holds_it_back_even_when_locked_is_false():
    """A sender-chosen unlock_time can hold an output past the ten-block consensus lock for
    as long as they like. Reading `locked` alone would miss it."""
    state = lock_observation([_transfer(LOCK_AMOUNT, unlock_time=9_999_999)], LOCK_AMOUNT)
    assert (state.confirmed, state.unlocked) == (True, False)


def test_a_confirmed_and_spendable_lock_is_both():
    state = lock_observation([_transfer(LOCK_AMOUNT)], LOCK_AMOUNT)
    assert (state.confirmed, state.unlocked) == (True, True)


@pytest.mark.parametrize("kind", ["pool", "pending", "out"])
def test_an_unmined_transfer_does_not_count_toward_the_lock(kind: str):
    """A transfer in the pool has no confirmations to count and can still be replaced.
    Counting one would credit money the chain has not committed to -- and here it would
    release the redeem pre-signature against it."""
    state = lock_observation([_transfer(LOCK_AMOUNT, kind=kind)], LOCK_AMOUNT)
    assert state.confirmed is False


# ---------------------------------------------------------------------------
# The join. A gate beside the construction leaves a window; a gate inside it does not.
# ---------------------------------------------------------------------------
def _release(state: MoneroLockState):
    bob = generate_key()
    return adaptor_join.release_redeem_presignature(
        private_key=bob.private_key,
        digest=DIGEST,
        adaptor_point=adaptor_join.adaptor_point_for_share(12345),
        spend_public_at_setup="aa" * 32,
        lock_state=state,
    )


def test_an_unfunded_lock_WITHHOLDS_and_carries_the_gates_own_reason():
    state = lock_observation([], LOCK_AMOUNT)
    with pytest.raises(adaptor_join.RedeemPresignatureWithheld) as raised:
        _release(state)
    assert "funded nothing" in str(raised.value), (
        "the message must be the GATE's, not one composed here -- an operator reading it "
        "should be reading what redeem_presignature_may_be_released decided"
    )


def test_a_confirmed_but_locked_lock_WITHHOLDS():
    """The case that costs money if it is got wrong: the funder can see their XMR on the
    chain and it is still not spendable."""
    with pytest.raises(adaptor_join.RedeemPresignatureWithheld):
        _release(MoneroLockState(confirmed=True, unlocked=False, reason="seeded"))


def test_NOTHING_IS_CONSTRUCTED_WHEN_THE_GATE_REFUSES():
    """THE ASSERTION THIS FILE EXISTS FOR, and the one a passing `pytest.raises` does not
    make. The protocol's rule is stricter than 'do not send it': monero_swap_protocol.py
    says nothing may compute the pre-signature earlier "to have it ready", because a value
    that exists in a process is a value the next line somebody writes can send.

    A `raise` after the construction would satisfy every other test in this file. So this
    one watches `pre_sign_leg` itself and asserts it was never reached -- the difference
    between 'the caller did not get it' and 'it does not exist'.
    """
    reached = []
    real = adaptor_join.pre_sign_leg
    adaptor_join.pre_sign_leg = lambda *a, **k: reached.append(1) or real(*a, **k)
    try:
        with pytest.raises(adaptor_join.RedeemPresignatureWithheld):
            _release(MoneroLockState(confirmed=True, unlocked=False, reason="seeded"))
    finally:
        adaptor_join.pre_sign_leg = real
    assert reached == [], "the pre-signature was CONSTRUCTED behind a refusal"


def test_a_ready_lock_RELEASES_a_leg_that_verifies():
    leg = _release(lock_observation([_transfer(LOCK_AMOUNT)], LOCK_AMOUNT))
    assert leg.label == "redeem"
    assert leg.spend_public_at_setup == "aa" * 32


def test_WITHHELD_IS_NOT_AN_AdaptorJoinError():
    """A withholding is the protocol WORKING. AdaptorJoinError means this repository
    disagrees with itself. A caller that logged the first as the second would report the
    one security property doing its job as a defect."""
    assert not issubclass(adaptor_join.RedeemPresignatureWithheld, adaptor_join.AdaptorJoinError)
