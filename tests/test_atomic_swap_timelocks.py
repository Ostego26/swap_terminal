#!/usr/bin/env python3
"""The swap's timelock ordering, and the preimage read that makes it atomic.

Role: tests (read-only)
Reads: nothing. No chain, no network, no daemon.
Writes: nothing
Can move funds: no
Mainnet-safe: yes

THESE TWO THINGS ARE THE SWAP. Everything else in atomic_swap_xrp.py is the
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
import re
from decimal import Decimal

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

from atomic_swap_xrp import (
    CHAIN_FIRST,
    CHAIN_LABELS,
    CHAIN_TEST_NETWORKS,
    PROVEN_LIVE,
    SCRIPT_CHAINS,
    XRP_FIRST,
    ScriptLeg,
    assert_timelock_ordering,
    chain_amount_for_rate,
    swap_timelocks,
)
from xrp_htlc_escrow import RIPPLE_EPOCH_OFFSET_SECONDS

NOW = 1_790_000_000.0      # a fixed instant; Date.now()-style drift has no place in an assertion
TIP = 3_200_000            # a plausible Gridcoin testnet height


def test_the_participants_leg_expires_first_under_the_real_policy():
    """48h against 24h, converted into two different clocks, still in the right order."""
    xrp_cancel_after, leg, why = swap_timelocks(NOW, TIP)
    assert why["initiator_hours"] == lock_hours_for_role(ROLE_INITIATOR) == 48
    assert why["participant_hours"] == lock_hours_for_role(ROLE_PARTICIPANT) == 24
    # 24 hours at Gridcoin's 90-second target: 86400 / 90 = 960 blocks.
    assert why["chain_blocks"] == 960
    assert leg.timeout_height == TIP + 960
    # The leg carries the chain it was computed for, which is what makes the height
    # above interpretable at all -- 960 blocks means 24 hours only on a 90s chain.
    assert leg.chain == "GRC" and leg.tip_height == TIP
    sentence = assert_timelock_ordering(xrp_cancel_after, leg, NOW)
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
    chain_timeout = TIP + int(48 * 3600 // SECONDS_PER_BLOCK["GRC"])
    xrp_cancel_after = int(NOW + 3600) - RIPPLE_EPOCH_OFFSET_SECONDS
    leg = ScriptLeg(chain="GRC", tip_height=TIP, timeout_height=chain_timeout)
    with pytest.raises(SystemExit, match="REFUSED before funding anything"):
        assert_timelock_ordering(xrp_cancel_after, leg, NOW)


def test_equal_expiries_are_refused_too_not_merely_inverted_ones():
    """A zero margin is not safe. Two legs expiring at the same moment is a race.

    `<= 0` rather than `< 0` in the guard, and this is what pins it: the two
    chains' clocks are independent and their block production is not, so "the
    same instant" is a coin flip decided by whichever chain moves first.
    """
    grc_blocks = int(24 * 3600 // SECONDS_PER_BLOCK["GRC"])
    chain_timeout = TIP + grc_blocks
    # Put the XRP expiry at exactly the GRC estimate.
    xrp_cancel_after = int(NOW + grc_blocks * SECONDS_PER_BLOCK["GRC"]) - RIPPLE_EPOCH_OFFSET_SECONDS
    leg = ScriptLeg(chain="GRC", tip_height=TIP, timeout_height=chain_timeout)
    with pytest.raises(SystemExit, match="REFUSED before funding anything"):
        assert_timelock_ordering(xrp_cancel_after, leg, NOW)


def test_the_demo_scale_shortens_both_legs_and_cannot_invert_them():
    """A flag that could shorten one leg only would be a flag that loses money.

    So the scale multiplies both, and the ordering assertion is run at several
    scales to show the property is structural rather than true at one value.
    """
    for scale in (1.0, 0.5, 0.1, 0.02):
        xrp_cancel_after, leg, why = swap_timelocks(NOW, TIP, hours_scale=scale)
        assert why["initiator_hours"] == 48 * scale
        assert why["participant_hours"] == 24 * scale
        assert "ordering OK" in assert_timelock_ordering(xrp_cancel_after, leg, NOW)


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
    _, _, reverse = swap_timelocks(NOW, TIP, direction=CHAIN_FIRST)
    assert forward["xrp_hours"] == 48 and forward["chain_hours"] == 24
    assert reverse["xrp_hours"] == 24 and reverse["chain_hours"] == 48
    # And the block count follows, since GRC is the initiator's leg now.
    assert reverse["chain_blocks"] == int(48 * 3600 // SECONDS_PER_BLOCK["GRC"]) == 1920


def test_both_directions_pass_their_own_ordering_check():
    for direction in (XRP_FIRST, CHAIN_FIRST):
        xrp_cancel_after, leg, _ = swap_timelocks(NOW, TIP, direction=direction)
        sentence = assert_timelock_ordering(xrp_cancel_after, leg, NOW, direction=direction)
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
    xrp_cancel_after, leg, _ = swap_timelocks(NOW, TIP, direction=CHAIN_FIRST)
    assert "ordering OK" in assert_timelock_ordering(xrp_cancel_after, leg, NOW, direction=CHAIN_FIRST)
    with pytest.raises(SystemExit, match="REFUSED before funding anything"):
        assert_timelock_ordering(xrp_cancel_after, leg, NOW, direction=XRP_FIRST)


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


# ---------------------------------------------------------------------------
# PRICING, added 2026-09-26 on the operator's instruction: "we should atomic swap
# at the known exchange rate even though it's just test net." The legs were 1 XRP
# against 1.0 GRC, which is a 1:1 swap at no rate at all -- and on testnet that
# costs nothing, which is exactly why it would have survived into somewhere it
# costs something.
# ---------------------------------------------------------------------------


def test_the_grc_leg_is_sized_by_the_rate():
    """1 XRP at 0.25 XRP per GRC buys 4 GRC. The rate divides, it does not multiply.

    Inverting it makes the swap off by the SQUARE of the price, which on testnet
    looks like a large number and nothing else -- so the direction is asserted
    with a rate whose inverse is a different answer (0.25 -> 4, and 4 -> 0.25).
    """
    assert chain_amount_for_rate(1_000_000, Decimal("0.25")) == Decimal("4.00000000")
    assert chain_amount_for_rate(1_000_000, Decimal(4)) == Decimal("0.25000000")


def test_the_grc_leg_rounds_DOWN_in_the_grc_holders_favour():
    """A rate applied with no stated rounding direction is a fee nobody agreed to.

    1 XRP at 3 XRP per GRC is 0.333... GRC. It must round DOWN, because the GRC
    leg is what the XRP buyer RECEIVES, so rounding down favours the party giving
    up the GRC rather than silently taking a sliver from them.
    """
    assert chain_amount_for_rate(1_000_000, Decimal(3)) == Decimal("0.33333333")


def test_a_non_positive_rate_is_refused_rather_than_producing_a_free_swap():
    """A zero rate divides to infinity and a negative one to a negative amount.

    Either would be handed to a daemon as an amount. Refused before anything is
    priced.
    """
    for bad in ("0", "-1", "-0.5"):
        with pytest.raises(ValueError, match="rate must be positive"):
            chain_amount_for_rate(1_000_000, Decimal(bad))


def test_the_rate_arithmetic_is_decimal_not_float():
    """A float rate rounds at the 17th digit and the quantize inherits it.

    The amount is what a daemon is asked to SEND, so the arithmetic is Decimal
    end to end. 0.1 is the classic float: three of them do not sum to 0.3.
    """
    amount = chain_amount_for_rate(1_000_000, Decimal("0.1"))
    assert amount == Decimal("10.00000000")
    assert isinstance(amount, Decimal)


# ---------------------------------------------------------------------------
# The three script chains, which is what --chain added on 2026-09-29.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("chain", "expected_blocks"),
    [("BTC", (24 * 3600 // 600)), ("LTC", (24 * 3600 // 150)), ("GRC", (24 * 3600 // 90))],
)
def test_each_script_chain_converts_the_SAME_hours_into_its_OWN_block_count(chain, expected_blocks):
    """THE POLICY IS IN HOURS AND EVERY CHAIN ENFORCES IT IN BLOCKS, at its own rate.

    This is the whole reason --chain could not be a cosmetic flag. The participant's leg
    is 24 hours on all three, and 24 hours is 144 blocks on Bitcoin, 576 on Litecoin and
    960 on Gridcoin -- a 6.7x spread. A driver that carried Gridcoin's 90-second interval
    while funding a Bitcoin HTLC would set a timeout roughly SIX AND A HALF TIMES too far
    out, and the failure would not surface until the refund was needed and the coins were
    not yet reclaimable.

    The numbers come from modules/htlc_timelock.SECONDS_PER_BLOCK, which already held all
    three before this driver could use any but GRC.
    """
    _, leg, why = swap_timelocks(NOW, TIP, chain=chain)
    assert leg.chain == chain
    assert why["chain_blocks"] == expected_blocks
    assert leg.timeout_height == TIP + expected_blocks
    assert why["chain_seconds_per_block"] == SECONDS_PER_BLOCK[chain]


def test_the_THREE_chains_do_not_all_produce_the_same_height():
    """The assertion the parametrize above cannot make on its own.

    Each case checks its chain in isolation, so a bug that ignored `chain` and used one
    interval for all three would still have to disagree with two of the three expected
    numbers -- but only if the expected numbers differ. This says they do, out loud, so
    the parametrize cannot be quietly reduced to one shared constant later.
    """
    heights = {chain: swap_timelocks(NOW, TIP, chain=chain)[1].timeout_height
               for chain in ("BTC", "LTC", "GRC")}
    assert len(set(heights.values())) == 3, f"two chains produced the same timeout height: {heights}"


def test_the_ordering_check_uses_THE_LEGS_OWN_chain_interval():
    """A leg carries its chain so the ordering check converts with the right number.

    THE MUTATION THIS CATCHES IS IN THE SIBLING TEST BELOW, NOT HERE, and that is worth
    recording because the obvious version of this test does NOT catch it. Making
    assert_timelock_ordering() read SECONDS_PER_BLOCK['GRC'] instead of the leg's chain
    was tried against this test on 2026-09-29 and it PASSED: in xrp-first the BTC leg is
    the participant's, so converting 144 blocks through 90 seconds instead of 600 makes
    it look like it expires in 3.6 hours rather than 24 -- sooner, not later, which still
    satisfies "participant before initiator". An understated expiry is invisible in this
    direction.

    So this test pins what it can honestly pin: that the sentence names the chain and its
    interval. The ordering consequence lives in the direction where it bites.
    """
    xrp_cancel_after, leg, _ = swap_timelocks(NOW, TIP, chain="BTC")
    sentence = assert_timelock_ordering(xrp_cancel_after, leg, NOW)
    assert "ordering OK" in sentence
    assert "BTC is height" in sentence, "the sentence must name the chain it converted for"
    assert f"{SECONDS_PER_BLOCK['BTC']}s" in sentence


def test_a_chain_first_BTC_leg_IS_ORDERED_CORRECTLY_and_the_wrong_interval_would_refuse_it():
    """WHERE READING THE WRONG CHAIN'S INTERVAL ACTUALLY BITES, measured rather than assumed.

    In chain-first the SCRIPT leg is the initiator's, so it carries the 48-hour lock: 288
    blocks on Bitcoin, which at Bitcoin's real 600-second interval is 48 hours and orders
    correctly against the participant's 24-hour XRP leg.

    Convert those same 288 blocks through Gridcoin's 90 seconds and they read as 7.2
    hours -- now the INITIATOR's leg appears to expire before the participant's, and
    assert_timelock_ordering raises SystemExit. So the wrong interval does not quietly
    authorize a bad swap here; it REFUSES a good one, which is the safe direction to fail
    but still wrong, and it is the direction where the defect is detectable at all.

    MUTATION: replace SECONDS_PER_BLOCK[chain] with SECONDS_PER_BLOCK['GRC'] in
    assert_timelock_ordering() and this test fails with "REFUSED before funding anything".
    Verified 2026-09-29; the xrp-first test above was tried first and did not catch it.
    """
    xrp_cancel_after, leg, why = swap_timelocks(NOW, TIP, chain="BTC", direction=CHAIN_FIRST)
    assert why["chain_hours"] == 48
    assert leg.timeout_height == TIP + int(48 * 3600 // SECONDS_PER_BLOCK["BTC"])
    sentence = assert_timelock_ordering(xrp_cancel_after, leg, NOW, direction=CHAIN_FIRST)
    assert "ordering OK" in sentence


def test_a_chain_with_no_completed_run_SAYS_SO_instead_of_inheriting_another_chains_evidence():
    """THE BANNER MAKES A CLAIM ABOUT EVIDENCE, and it was printing GRC's on every chain.

    Found by running `atomic_swap_xrp.py --chain btc` on 2026-09-29, after the
    parameterization had already passed the suite and ruff. The banner said

        BOTH directions have completed ... A failure here is a regression, not a discovery.

    on a chain no run had ever touched. That is false in the dangerous direction: it tells
    an operator that a failure is a known-good path breaking, when it would in fact be the
    first attempt and worth reading rather than retrying. Rule 17's register error, printed
    to the person deciding whether to fund something.

    PROVEN_LIVE is keyed by chain and GRC is its only entry, which is the honest state:
    BTC and LTC are exercised by seeded tests -- the block arithmetic above and the
    ordering check -- and have never been run against a chain.

    This test fails the moment somebody adds a chain to PROVEN_LIVE without a run behind
    it, which is the only way the claim can become false again.
    """
    assert set(PROVEN_LIVE) == {"GRC"}, (
        "PROVEN_LIVE changed shape. That table is the record of runs that COMPLETED ON "
        "THE CURRENT CODE PATH. It was emptied on 2026-09-29 when both runners moved off "
        "Gridcoin's createhtlc onto the chain clients, and GRC was added back the same "
        "day by an actual run of THIS code (OK=15 FAIL=0). BTC and LTC are still absent "
        "on purpose: they share every function GRC's run exercised, and sharing code is "
        "not evidence. Adding a chain without a txid from a run makes the banner lie "
        "again, and the second version of that lie is the dangerous one because the "
        "chain name is still right"
    )
    # WHAT COUNTS AS EVIDENCE, spelled as a check rather than as trust. An entry has to
    # carry the transaction identifiers a reader can go look up; a sentence that only
    # asserts "this works" is the claim, not the proof of it. Four txids because the run
    # has four on-chain acts -- escrow, HTLC funding, claim, finish -- and a path that
    # completed leaves all four behind. Hex is lowercased before matching because XRP
    # prints uppercase and the bitcoin family lowercase, and the requirement is that the
    # identifier is THERE, not which chain's convention typed it.
    txids = re.findall(r"\b[0-9a-f]{8,}\b", PROVEN_LIVE["GRC"].lower())
    assert len(txids) >= 4, (
        f"GRC's PROVEN_LIVE sentence names {len(txids)} transaction identifiers and the "
        f"run it records has four on-chain acts. An entry that cannot name them is not "
        f"evidence of a run, which is the one thing this table is for: {txids}"
    )
    for chain in SCRIPT_CHAINS:
        assert chain in CHAIN_LABELS, f"{chain} has no operator-facing label"
        assert chain in CHAIN_TEST_NETWORKS, f"{chain} has no test-network allowlist"
    # The sentence a chain gets when it HAS evidence is read straight out of this table
    # and printed to the operator, so its ENDING is pinned too: "a regression, not a
    # discovery" is the half that tells them what a failure would mean, and it is the half
    # a shortened entry would drop first. This assertion was vacuous while the table was
    # empty (all() over nothing is True) and is not any more.
    assert all("regression" in sentence for sentence in PROVEN_LIVE.values())
