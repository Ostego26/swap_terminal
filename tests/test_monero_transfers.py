"""The deposit scan: what it credits, what it holds back, and what it refuses.

Role: test (pure function; no daemon, no socket, no database)
Reads: chains/monero_transfers.py
Writes: nothing
Can move funds: no. What it pins DOES gate a payout, which is why the
      refusal cases outnumber the happy path here.
Mainnet-safe: yes

The seeded transfer dicts below are shaped the way chains/monero_transfers.py
expects monero-wallet-rpc to shape them, AND THAT SHAPE IS UNVERIFIED -- see
THE HONEST STATUS in chains/monero.py. So these tests prove the FILTERING and
the REFUSALS are right given the shape; they cannot prove the shape. A wrong
field name passes every test here and fails on the operator's first deposit.

That is stated rather than left implied, because a green suite is exactly the
thing that would otherwise be mistaken for evidence the adapter works.
"""

import pytest
from chains.monero_transfers import MoneroTransferError, deposit_events_from_transfers

ADDRESS = "8Bstagenetsubaddressforswapnumberone"
OTHER = "8Bstagenetsubaddressforadifferentswap"


def transfer(**overrides):
    """A creditable incoming transfer, which each test then spoils one field of."""
    base = {
        "txid": "a" * 64,
        "amount": 2_500_000_000_000,  # 2.5 XMR in piconero
        "address": ADDRESS,
        "confirmations": 12,
        "subaddr_index": {"major": 0, "minor": 7},
        "unlock_time": 0,
        "locked": False,
        "double_spend_seen": False,
        "type": "in",
    }
    base.update(overrides)
    return base


def test_a_plain_deposit_becomes_one_event_at_the_subaddress_index():
    """`vout` is the minor index: which swap the money arrived for.

    Not a position in the transaction, because Monero publishes none, and not
    a zero, because that is the fabrication chains/base.py:169 documents and
    migrate_deposit_vouts.py exists to clean up.
    """
    scan = deposit_events_from_transfers([transfer()], ADDRESS, 10)
    assert scan.events == [{
        "txid": "a" * 64,
        "vout": 7,
        "address": ADDRESS,
        "amount": 2.5,
        "confirmations": 12,
    }]
    assert scan.deferred == []


def test_a_deposit_to_another_subaddress_is_not_credited_to_this_one():
    """get_transfers is account-wide, so the filter is here and not in the query.

    If it were left to the caller's scoping argument and that argument ever
    stopped working, the first swap to poll would be credited with every other
    swap's deposits.
    """
    scan = deposit_events_from_transfers([transfer(address=OTHER)], ADDRESS, 10)
    assert scan.events == []


def test_an_unmined_transfer_is_not_credited():
    """"pool" and "pending" have no confirmations to count and can still be replaced."""
    for kind in ("pool", "pending", "out"):
        scan = deposit_events_from_transfers([transfer(type=kind)], ADDRESS, 10)
        assert scan.events == [], kind


def test_a_conflicted_transfer_is_held_back_and_reported_not_dropped():
    """Money the wallet has doubts about must not be silently invisible.

    Rule 14: never let a result that exists print nothing. An operator
    watching a swap that will not credit needs the reason on the screen.
    """
    scan = deposit_events_from_transfers([transfer(double_spend_seen=True)], ADDRESS, 10)
    assert scan.events == []
    assert len(scan.deferred) == 1
    assert "double_spend_seen" in scan.deferred[0]


def test_a_time_locked_transfer_is_held_back_and_reported():
    """A custom unlock_time outlasts the ten-block consensus lock.

    Crediting one would release a swap whose payout the wallet then refuses to
    build -- the gate-versus-transfer disagreement, arriving by the other door.
    """
    scan = deposit_events_from_transfers([transfer(unlock_time=2_500_000, locked=True)], ADDRESS, 10)
    assert scan.events == []
    assert len(scan.deferred) == 1
    assert "NOT creditable until it unlocks" in scan.deferred[0]


def test_an_immature_deposit_is_credited_and_also_reported():
    """Both, and deliberately.

    The event carries the real confirmation count and the release decision
    stays in services/deposit_service.py where it is for every other chain --
    duplicating the threshold comparison here would be rule 8's two copies of
    one rule. The deferred line exists so the wait is visible while it happens.
    """
    scan = deposit_events_from_transfers([transfer(confirmations=3)], ADDRESS, 10)
    assert scan.events[0]["confirmations"] == 3
    assert "3/10 blocks" in scan.deferred[0]


def test_a_transfer_with_no_subaddress_index_raises_instead_of_inventing_one():
    """THE CENTRAL REFUSAL OF THIS MODULE.

    chains/base.py, faced with an output it cannot locate, fabricates an event
    at vout 0 carrying the amount the caller already believed -- and its own
    comment says services/deposit_service.py "cannot tell the two apart". A new
    adapter inherits the problem without inheriting the excuse (rule 19), so
    this stops.
    """
    absent = transfer()
    del absent["subaddr_index"]
    with pytest.raises(MoneroTransferError, match="nothing to identify"):
        deposit_events_from_transfers([absent], ADDRESS, 10)

    for broken in ({}, {"major": 0}, None, "0/7"):
        with pytest.raises(MoneroTransferError, match="nothing to identify"):
            deposit_events_from_transfers([transfer(subaddr_index=broken)], ADDRESS, 10)


def test_a_non_integer_subaddress_index_raises_rather_than_being_coerced():
    """A minor index that is not a number cannot become a `vout` either."""
    for bad in ("seven", None, {"nested": 1}):
        with pytest.raises(MoneroTransferError, match="non-integer"):
            deposit_events_from_transfers([transfer(subaddr_index={"major": 0, "minor": bad})], ADDRESS, 10)


def test_two_transfers_claiming_one_key_stop_the_whole_scan():
    """The check standing under the assumption the module docstring names.

    If get_transfers ever reports two incoming entries for one (transaction,
    subaddress), the key this file uses for `vout` does not identify a deposit.
    Dropping one loses money, summing them guesses, and letting both through
    lets UNIQUE(asset, txid, vout) silently overwrite. So: nothing is credited
    and the operator is told which transaction refuted it.
    """
    pair = [transfer(amount=1_000_000_000_000), transfer(amount=4_000_000_000_000)]
    with pytest.raises(MoneroTransferError, match="does not identify a deposit"):
        deposit_events_from_transfers(pair, ADDRESS, 10)


def test_the_ambiguity_refusal_names_both_amounts():
    """Because the first question an operator asks is "how much is stuck?"."""
    pair = [transfer(amount=1_000_000_000_000), transfer(amount=4_000_000_000_000)]
    with pytest.raises(MoneroTransferError) as caught:
        deposit_events_from_transfers(pair, ADDRESS, 10)
    assert "1.0" in str(caught.value)
    assert "4.0" in str(caught.value)


def test_two_transfers_to_different_subaddresses_in_one_transaction_are_fine():
    """One transaction paying two swaps is ordinary and must not trip the guard."""
    scan = deposit_events_from_transfers(
        [transfer(subaddr_index={"major": 0, "minor": 7}), transfer(subaddr_index={"major": 0, "minor": 9})],
        ADDRESS,
        10,
    )
    assert sorted(event["vout"] for event in scan.events) == [7, 9]


def test_a_non_integer_amount_raises_rather_than_being_coerced():
    """Reading a float here silently loses piconero; reading a string coerces the scale.

    Both produce a plausible number, which is the failure mode this whole path
    is built to avoid.
    """
    for bad in (2.5, "2500000000000", None, True):
        with pytest.raises(MoneroTransferError, match="atomic units"):
            deposit_events_from_transfers([transfer(amount=bad)], ADDRESS, 10)


def test_a_single_amount_matching_the_aggregate_is_credited():
    """The ordinary case, and the one every example in the spec shows.

    `"amount": 200000000000, "amounts": [200000000000]` -- one output, and the
    breakdown agrees with the total.
    """
    scan = deposit_events_from_transfers(
        [transfer(amount=2_500_000_000_000, amounts=[2_500_000_000_000])], ADDRESS, 10
    )
    assert scan.events[0]["amount"] == 2.5


def test_several_outputs_summing_to_the_aggregate_are_credited_once():
    """THE CASE THE KEY DEPENDS ON.

    Two outputs to one subaddress in one transaction arrive as ONE entry whose
    `amount` is the total. That aggregation is why (txid, subaddr_index)
    identifies a deposit at all, and it must produce exactly one event.
    """
    scan = deposit_events_from_transfers(
        [transfer(amount=3_000_000_000_000, amounts=[1_000_000_000_000, 2_000_000_000_000])],
        ADDRESS,
        10,
    )
    assert len(scan.events) == 1
    assert scan.events[0]["amount"] == 3.0


def test_an_aggregate_that_disagrees_with_its_breakdown_refuses():
    """The silent shortfall this guard exists to prevent.

    Reading `amount` while `amounts` says something else would credit the
    customer less than they sent, with no error and no log line -- the swap
    would simply settle short. Neither figure is chosen here: crediting the
    smaller short-pays them, crediting the larger over-pays from the hot
    wallet, so a human decides which it is.
    """
    with pytest.raises(MoneroTransferError, match="sum to"):
        deposit_events_from_transfers(
            [transfer(amount=1_000_000_000_000, amounts=[1_000_000_000_000, 2_000_000_000_000])],
            ADDRESS,
            10,
        )


def test_the_amount_disagreement_names_both_figures_and_the_difference():
    """An operator's first question is how much is unaccounted for."""
    with pytest.raises(MoneroTransferError) as caught:
        deposit_events_from_transfers(
            [transfer(amount=1_000_000_000_000, amounts=[3_000_000_000_000])], ADDRESS, 10
        )
    message = str(caught.value)
    assert "1000000000000" in message
    assert "3000000000000" in message
    assert "difference of 2000000000000" in message


def test_a_missing_or_empty_amounts_field_is_not_a_problem():
    """There is simply nothing to compare against, so the check says nothing.

    A guard that fired on an absent optional field would refuse ordinary
    deposits, which is a worse failure than the one it guards against.
    """
    plain = transfer()
    plain.pop("amounts", None)
    assert deposit_events_from_transfers([plain], ADDRESS, 10).events[0]["amount"] == 2.5
    assert deposit_events_from_transfers([transfer(amounts=[])], ADDRESS, 10).events[0]["amount"] == 2.5


def test_a_non_integer_in_the_breakdown_refuses():
    """It cannot be reconciled against the aggregate, so it is not waved through."""
    with pytest.raises(MoneroTransferError, match="non-integer entry"):
        deposit_events_from_transfers([transfer(amounts=[1.5, 2])], ADDRESS, 10)


def test_a_transfer_with_no_txid_raises():
    """An event without a transaction id cannot be deduplicated.

    upsert_deposit_event() keys on (asset, txid, vout), so re-scanning would
    credit it again on every cycle.
    """
    with pytest.raises(MoneroTransferError, match="no txid"):
        deposit_events_from_transfers([transfer(txid="")], ADDRESS, 10)


def test_an_empty_wallet_answer_is_an_empty_scan_not_an_error():
    """No deposits yet is a real answer and must not look like a failure."""
    for empty in ([], None):
        scan = deposit_events_from_transfers(empty, ADDRESS, 10)
        assert scan.events == []
        assert scan.deferred == []
