#!/usr/bin/env python3
"""The swap's timelock ordering, and the preimage read that makes it atomic.

Role: tests (read-only)
Reads: nothing. No chain, no network, no daemon.
Writes: nothing
Can move funds: no
Mainnet-safe: yes

THESE TWO THINGS ARE THE SWAP. Everything else in atomic_swap_xrp_grc.py is the
order of five acts; these are the decisions, and both fail in ways nothing else
would catch:

  the ORDERING    if the participant's leg outlives the initiator's, the
                  initiator takes the participant's coins and then refunds its
                  own. Both legs still fund, both transactions still succeed,
                  and the loss happens hours later when a timelock expires. No
                  integration test would see it; the swap LOOKS complete.
  the PREIMAGE    read off the counterparty's own claim transaction rather than
                  received from them. A version that used its local `secret`
                  variable would pass every end-to-end run on one machine and be
                  worthless between two parties, because a real participant has
                  no such variable.
"""

from __future__ import annotations

import hashlib

import pytest
from modules.htlc_spend import (
    hashlock_script_sig,
    preimage_from_scriptsig,
    push_data,
    refund_script_sig,
    script_pushes,
)
from modules.htlc_timelock import ROLE_INITIATOR, ROLE_PARTICIPANT, SECONDS_PER_BLOCK, lock_hours_for_role

from atomic_swap_xrp_grc import assert_timelock_ordering, swap_timelocks
from xrp_htlc_escrow import RIPPLE_EPOCH_OFFSET_SECONDS

NOW = 1_790_000_000.0      # a fixed instant; Date.now()-style drift has no place in an assertion
TIP = 3_200_000            # a plausible Gridcoin testnet height


def test_the_participants_leg_expires_first_under_the_real_policy():
    """48h against 24h, converted into two different clocks, still in the right order."""
    xrp_cancel_after, grc_timeout, why = swap_timelocks(NOW, TIP)
    assert why["initiator_hours"] == lock_hours_for_role(ROLE_INITIATOR) == 48
    assert why["participant_hours"] == lock_hours_for_role(ROLE_PARTICIPANT) == 24
    # 24 hours at Gridcoin's 90-second target: 86400 / 90 = 960 blocks.
    assert why["grc_blocks"] == 960
    assert grc_timeout == TIP + 960
    sentence = assert_timelock_ordering(xrp_cancel_after, grc_timeout, TIP, NOW)
    assert "ordering OK" in sentence
    # The margin is the initiator's extra 24 hours, and the sentence has to say
    # the GRC figure is an estimate -- an operator reading a number with no
    # denominator behind it is the failure rule 3 names.
    assert "estimated" in sentence.lower() or "ESTIMATED" in sentence


def test_an_inverted_ordering_is_REFUSED_before_anything_is_funded():
    """The one assertion that prevents a loss. It must raise, not warn.

    Constructed by hand rather than by scaling the policy, because the policy
    cannot currently produce this -- and that is exactly why it needs a test: a
    future change to lock_hours_for_role, or a flag that shortened one leg only,
    would produce it silently.
    """
    # A GRC leg 48 hours out, an XRP leg 1 hour out: the wrong way round.
    grc_timeout = TIP + int(48 * 3600 // SECONDS_PER_BLOCK["GRC"])
    xrp_cancel_after = int(NOW + 3600) - RIPPLE_EPOCH_OFFSET_SECONDS
    with pytest.raises(SystemExit, match="REFUSED before funding anything"):
        assert_timelock_ordering(xrp_cancel_after, grc_timeout, TIP, NOW)


def test_equal_expiries_are_refused_too_not_merely_inverted_ones():
    """A zero margin is not safe. Two legs expiring at the same moment is a race.

    `<= 0` rather than `< 0` in the guard, and this is what pins it: the two
    chains' clocks are independent and their block production is not, so "the
    same instant" is a coin flip decided by whichever chain moves first.
    """
    grc_blocks = int(24 * 3600 // SECONDS_PER_BLOCK["GRC"])
    grc_timeout = TIP + grc_blocks
    # Put the XRP expiry at exactly the GRC estimate.
    xrp_cancel_after = int(NOW + grc_blocks * SECONDS_PER_BLOCK["GRC"]) - RIPPLE_EPOCH_OFFSET_SECONDS
    with pytest.raises(SystemExit, match="REFUSED before funding anything"):
        assert_timelock_ordering(xrp_cancel_after, grc_timeout, TIP, NOW)


def test_the_demo_scale_shortens_both_legs_and_cannot_invert_them():
    """A flag that could shorten one leg only would be a flag that loses money.

    So the scale multiplies both, and the ordering assertion is run at several
    scales to show the property is structural rather than true at one value.
    """
    for scale in (1.0, 0.5, 0.1, 0.02):
        xrp_cancel_after, grc_timeout, why = swap_timelocks(NOW, TIP, hours_scale=scale)
        assert why["initiator_hours"] == 48 * scale
        assert why["participant_hours"] == 24 * scale
        assert "ordering OK" in assert_timelock_ordering(xrp_cancel_after, grc_timeout, TIP, NOW)


def test_the_participant_recovers_the_secret_from_the_initiators_own_claim():
    """A claim scriptSig is <sig> <preimage> OP_TRUE <redeemScript>, and the hash decides.

    This is the shape Gridcoin's own claimhtlc builds (src/rpc/htlc.cpp:
    CreateHTLCClaimScript) and the shape modules/htlc_spend.hashlock_script_sig
    builds for BTC and LTC. One reader serves both because both push the preimage.
    """
    secret = bytes(range(32))
    secret_hash = hashlib.sha256(secret).digest()
    script_sig = hashlock_script_sig(b"\x30" + b"\x11" * 71, b"\x02" + b"\x03" * 32, secret, b"\x63" + b"\xaa" * 92)
    assert [len(p) for p in script_pushes(script_sig)] == [72, 33, 32, 93]
    assert preimage_from_scriptsig(script_sig, secret_hash) == secret


def test_a_thirty_two_byte_push_that_is_not_the_preimage_is_not_returned():
    """THE HASH DECIDES, NOT THE LENGTH, and this is the test that says so.

    A naive reader takes the 32-byte push. Here there are TWO of them and only
    one is the preimage -- and the decoy is first, so a length filter returns the
    wrong bytes. Claiming a leg with the wrong 32 bytes burns the fee and leaves
    the real preimage unused while the swap's own timelock runs down.
    """
    secret = bytes(range(32))
    decoy = bytes(range(100, 132))
    assert len(decoy) == len(secret) == 32
    secret_hash = hashlib.sha256(secret).digest()
    script_sig = push_data(decoy) + push_data(secret) + b"\x51" + push_data(b"\x63" + b"\xaa" * 92)
    assert preimage_from_scriptsig(script_sig, secret_hash) == secret


def test_a_refund_transaction_yields_no_preimage_rather_than_raising():
    """A watcher polling a chain must be able to say "not this one" per block.

    A refund scriptSig carries no preimage at all. Returning None rather than
    raising is what lets the caller keep looking without an exception per block.
    """
    secret_hash = hashlib.sha256(bytes(range(32))).digest()
    refund = refund_script_sig(b"\x30" + b"\x11" * 71, b"\x02" + b"\x03" * 32, b"\x63" + b"\xaa" * 92)
    assert preimage_from_scriptsig(refund, secret_hash) is None


def test_a_hex_encoded_hash_is_refused_rather_than_never_matching():
    """64 characters is not 32 bytes, and silently never matching is the danger.

    A caller that passed the hex string would read "the counterparty has not
    revealed the preimage" off a transaction that carries it, and would wait out
    its own timelock for nothing.
    """
    secret = bytes(range(32))
    script_sig = hashlock_script_sig(b"\x30" + b"\x11" * 71, b"\x02" + b"\x03" * 32, secret, b"\x63")
    with pytest.raises(ValueError, match="32 bytes"):
        preimage_from_scriptsig(script_sig, hashlib.sha256(secret).hexdigest().encode())
