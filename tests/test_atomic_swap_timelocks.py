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
from chains.xrp_crypto_condition import preimage_from_escrow_finish, preimage_fulfillment
from modules.htlc_spend import (
    hashlock_script_sig,
    preimage_from_scriptsig,
    push_data,
    refund_script_sig,
    script_pushes,
)
from modules.htlc_timelock import ROLE_INITIATOR, ROLE_PARTICIPANT, SECONDS_PER_BLOCK, lock_hours_for_role

from atomic_swap_xrp_grc import (
    GRC_FIRST,
    XRP_FIRST,
    assert_timelock_ordering,
    swap_timelocks,
)
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


# ---------------------------------------------------------------------------
# THE REVERSE DIRECTION, added 2026-09-26 after the xrp-first swap completed.
# Both of these would be silent: the ordering one funds two legs whose expiries
# are inverted and every transaction succeeds, and the reader one claims with
# bytes that are not the secret.
# ---------------------------------------------------------------------------


def test_the_longer_lock_follows_the_ROLE_not_the_chain():
    """Reversing the direction must move the 48 hours to the other chain.

    The first version of swap_timelocks() gave XRP the initiator's hours
    unconditionally. Running grc-first against that would have put the LONGER
    lock on the participant's XRP leg and the shorter one on the initiator's GRC
    leg -- expiries inverted, both legs funding fine, and the loss arriving hours
    later. This asserts the hours swap over.
    """
    _, _, forward = swap_timelocks(NOW, TIP, direction=XRP_FIRST)
    _, _, reverse = swap_timelocks(NOW, TIP, direction=GRC_FIRST)
    assert forward["xrp_hours"] == 48 and forward["grc_hours"] == 24
    assert reverse["xrp_hours"] == 24 and reverse["grc_hours"] == 48
    # And the block count follows, since GRC is the initiator's leg now.
    assert reverse["grc_blocks"] == int(48 * 3600 // SECONDS_PER_BLOCK["GRC"]) == 1920


def test_both_directions_pass_their_own_ordering_check():
    for direction in (XRP_FIRST, GRC_FIRST):
        xrp_cancel_after, grc_timeout, _ = swap_timelocks(NOW, TIP, direction=direction)
        sentence = assert_timelock_ordering(xrp_cancel_after, grc_timeout, TIP, NOW, direction=direction)
        assert "ordering OK" in sentence
        assert direction in sentence


def test_the_ordering_check_is_not_hardcoded_to_GRC_expiring_first():
    """The reverse direction's correct timelocks must FAIL the forward check.

    This is the test that proves the check reads the direction rather than
    asserting "GRC before XRP" unconditionally. grc-first's timelocks are
    correct FOR grc-first and inverted for xrp-first, so scoring them under the
    wrong direction has to refuse -- otherwise the check would have passed the
    reverse direction while the expiries were the wrong way round.
    """
    xrp_cancel_after, grc_timeout, _ = swap_timelocks(NOW, TIP, direction=GRC_FIRST)
    assert "ordering OK" in assert_timelock_ordering(xrp_cancel_after, grc_timeout, TIP, NOW, direction=GRC_FIRST)
    with pytest.raises(SystemExit, match="REFUSED before funding anything"):
        assert_timelock_ordering(xrp_cancel_after, grc_timeout, TIP, NOW, direction=XRP_FIRST)


def test_an_unknown_direction_is_refused_rather_than_defaulted():
    with pytest.raises(ValueError, match="unknown swap direction"):
        swap_timelocks(NOW, TIP, direction="grc-to-xrp-ish")


def test_the_participant_recovers_the_secret_from_an_escrow_finish():
    """The XRPL half of the read, and the mirror of the scriptSig half.

    When the XRP leg is the one CLAIMED, the secret is in the EscrowFinish's
    Fulfillment field, not in a scriptSig. Without this reader the swap can only
    run in one direction.
    """
    secret = bytes(range(32))
    secret_hash = hashlib.sha256(secret).digest()
    finish = {"TransactionType": "EscrowFinish", "Fulfillment": preimage_fulfillment(secret)}
    assert preimage_from_escrow_finish(finish, secret_hash) == secret
    # xrpl-py and rippled both nest the submitted fields under tx_json in some
    # responses, so both shapes are read.
    assert preimage_from_escrow_finish({"tx_json": finish}, secret_hash) == secret


def test_a_fulfillment_for_a_different_secret_is_refused():
    """THE HASH DECIDES HERE TOO. A well-formed fulfillment is not proof.

    Anyone may submit an EscrowFinish carrying a valid fulfillment for a
    different secret -- on a shared account two unrelated swaps do it without
    anybody being adversarial. Claiming the other leg with the wrong 32 bytes
    burns a fee and leaves the real secret unused while a timelock runs down.
    """
    ours = hashlib.sha256(bytes(range(32))).digest()
    theirs = preimage_fulfillment(bytes(range(100, 132)))
    assert preimage_from_escrow_finish({"Fulfillment": theirs}, ours) is None


def test_a_cancel_or_a_plain_payment_yields_no_secret_rather_than_raising():
    """A participant polling a ledger says "not this one" without an exception."""
    secret_hash = hashlib.sha256(bytes(range(32))).digest()
    assert preimage_from_escrow_finish({"TransactionType": "EscrowCancel"}, secret_hash) is None
    assert preimage_from_escrow_finish({}, secret_hash) is None


def test_a_malformed_fulfillment_yields_none_and_does_not_index_off_the_end():
    """Read off a public ledger, so the bytes are untrusted input.

    Truncated, non-hex, wrong tag, and a length byte claiming more than is there
    all have to return None rather than raising -- a length an attacker controls
    must not become an IndexError inside a swap.
    """
    secret_hash = hashlib.sha256(b"").digest()
    for bad in ("", "A0", "zz", "A0FF8020" + "00" * 4, "B0028000", "A0028100"):
        assert preimage_from_escrow_finish({"Fulfillment": bad}, secret_hash) is None


def test_the_empty_preimage_fulfillment_round_trips():
    """A0028000 -- the vector where every length in the encoding is different."""
    assert preimage_from_escrow_finish({"Fulfillment": "A0028000"}, hashlib.sha256(b"").digest()) == b""
