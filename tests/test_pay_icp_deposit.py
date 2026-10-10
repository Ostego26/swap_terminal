"""pay_icp_deposit's refusals and its reading of the ledger's reply.

Role: test (seeded rows and seeded reply strings; no replica, no ledger, no dfx,
      no network, and nothing is ever sent)
Reads: pay_icp_deposit.py, db.py's SCHEMA
Writes: an in-memory SQLite database per test
Can move funds: no. Every function exercised here is pure or reads a temporary
      database. The send path is NOT called: `send_outcome()` is given reply
      strings directly, which is the whole reason it was extracted.
Mainnet-safe: yes

WHY THE REFUSALS ARE THE THING UNDER TEST RATHER THAN THE SEND.

The send is four lines and the ledger is the authority on whether it worked. What
this tool adds over `dfx canister call ... transfer` is the set of conditions it
refuses under, and each of those is a failure somebody already paid for --
pay_test_deposit.py's header records four from one afternoon, including 82.65
tGRC to an address nobody held the key for because "the newest awaiting_deposit
row" was a swap from three hours earlier.

So a test that sent successfully would prove the easy half. These prove the half
that costs money when it is missing.
"""

from __future__ import annotations

import sqlite3

import pytest
from db import SCHEMA, dict_factory
from services.helpers import iso_to_epoch_nanos

import pay_icp_deposit as subject

#: A swap that is ready to pay. Each test mutates one field away from this, so
#: the thing under test is always the single difference.
GOOD = {
    "id": "s_1234567890abcdef",
    "quote_id": "q_1234567890abcdef",
    "from_asset": "ICP",
    "to_asset": "GRC",
    "deposit_address": "d220b5a9955e7667fb429419a2eca19a82c84c1fef034789b4d5930db19b601d",
    "payout_address": "mg3gJAmhADxf2ScRuXu7HXM2oixxiQG2Ap",
    "expected_input_amount": 2.42621078,
    "status": "awaiting_deposit",
    "created_at": "2026-10-10T17:00:00+00:00",
    "expires_at": "2026-10-10T17:10:00+00:00",
}

#: Inside the quote window and inside the ledger's 24h dedup window.
NOW = iso_to_epoch_nanos("2026-10-10T17:05:00+00:00")


def swap(**overrides) -> dict:
    return {**GOOD, **overrides}


# ---------------------------------------------------------------------------
# REFUSALS


def test_a_ready_swap_is_not_refused():
    """The control. Without it, a refusal that fires on everything would pass."""
    assert subject.refuse(swap(), [], NOW) == []


def test_a_non_icp_deposit_leg_is_refused():
    """Sending ICP to a swap expecting BTC is an unattributable deposit."""
    reasons = subject.refuse(swap(from_asset="BTC"), [], NOW)
    assert any("not ICP" in r for r in reasons)


@pytest.mark.parametrize(
    "status", ["deposit_seen", "confirming", "payout_pending", "paying", "completed", "failed"]
)
def test_any_status_past_awaiting_is_refused(status):
    """MUTATION: widen AWAITING to fee_sweep.py's active list.

    `deposit_seen` and `confirming` mean a deposit has ALREADY ARRIVED. Sending
    again is the duplicate this file exists to prevent, and it is the one that
    reads as reasonable -- the swap is still "active", so a looser check waves it
    through.
    """
    reasons = subject.refuse(swap(status=status), [], NOW)
    assert any(status in r for r in reasons)


def test_an_existing_deposit_event_is_refused_and_the_amount_is_named():
    """That is money already sent. A second send is not a retry."""
    deposits = [
        {"txid": "a" * 64, "vout": 0, "amount": 2.42621078, "confirmations": 1, "credited_at": None}
    ]
    reasons = subject.refuse(swap(), deposits, NOW)
    assert any("already recorded" in r and "2.42621078" in r for r in reasons)


def test_an_expired_swap_is_refused():
    """A deposit against a stale quote credits into review, not into a payout."""
    late = iso_to_epoch_nanos("2026-10-10T17:11:00+00:00")
    reasons = subject.refuse(swap(), [], late)
    assert any("expired" in r for r in reasons)


def test_a_swap_older_than_the_ledgers_dedup_window_is_refused():
    """MUTATION: drop the 24h check.

    created_at is the idempotency key, so a swap older than the window cannot be
    paid idempotently at all -- the ledger answers TxTooOld and nothing moves.
    Paying it needs a key from somewhere other than the swap, which forfeits
    dedup, and that is a live-posture decision rather than this tool's.
    """
    much_later = NOW + subject.DEDUP_WINDOW_NANOS
    reasons = subject.refuse(swap(), [], much_later)
    assert any("TxTooOld" in r for r in reasons)


def test_the_dedup_window_is_the_measured_twenty_four_hours():
    """Pinned against the ledger's own reply, recorded in chains/icp.py."""
    assert subject.DEDUP_WINDOW_NANOS == 86_400_000_000_000


@pytest.mark.parametrize("amount", [0, 0.0, None])
def test_a_swap_with_no_amount_to_send_is_refused(amount):
    reasons = subject.refuse(swap(expected_input_amount=amount), [], NOW)
    assert any("no amount to send" in r for r in reasons)


def test_every_reason_is_reported_not_just_the_first():
    """An operator who fixes one reason and meets a second has learned nothing.

    This swap is wrong three ways at once, and all three must be named in one
    run.
    """
    reasons = subject.refuse(
        swap(from_asset="BTC", status="completed", expected_input_amount=0), [], NOW
    )
    assert len(reasons) >= 3


# ---------------------------------------------------------------------------
# THE LOOKUP, AND THE ABSENCE OF A RECENCY FALLBACK


def seeded_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = dict_factory
    conn.executescript(SCHEMA)
    conn.execute(
        "INSERT INTO quotes (id, from_asset, to_asset, input_amount, quoted_rate, fee_bps, "
        "network_fee_reserve, output_amount_estimate, expires_at, created_at) "
        "VALUES (?, 'ICP', 'GRC', 2.42621078, 1.0, 150, 0.01, 2.0, ?, ?)",
        (GOOD["quote_id"], GOOD["expires_at"], GOOD["created_at"]),
    )
    conn.execute(
        "INSERT INTO swaps (id, quote_id, from_asset, to_asset, deposit_address, payout_address, "
        "expected_input_amount, quoted_rate, fee_bps, network_fee_reserve, "
        "output_amount_estimate, status, min_confirmations, created_at, updated_at, expires_at) "
        "VALUES (?, ?, 'ICP', 'GRC', ?, ?, ?, 1.0, 150, 0.0001, 2.0, ?, 1, ?, ?, ?)",
        (
            GOOD["id"], GOOD["quote_id"], GOOD["deposit_address"], GOOD["payout_address"],
            GOOD["expected_input_amount"], GOOD["status"], GOOD["created_at"],
            GOOD["created_at"], GOOD["expires_at"],
        ),
    )
    return conn


def test_the_swap_is_found_by_its_deposit_address():
    """The selector the operator is actually reading off the page."""
    conn = seeded_db()
    try:
        found = subject.find_swap(conn, deposit_address=GOOD["deposit_address"])
        assert found is not None
        assert found["id"] == GOOD["id"]
    finally:
        conn.close()


def test_the_swap_is_found_by_its_id():
    conn = seeded_db()
    try:
        assert subject.find_swap(conn, swap_id=GOOD["id"])["id"] == GOOD["id"]
    finally:
        conn.close()


def test_an_unknown_address_returns_none_rather_than_the_newest_swap():
    """MUTATION: add an `ORDER BY created_at DESC LIMIT 1` fallback.

    THIS IS THE 82.65 tGRC TEST. pay_test_deposit.py's header records what the
    fallback cost: with no new swap created, "the newest awaiting_deposit row"
    was a swap from three hours earlier with an autofilled Bitcoin testnet payout
    address, and the payment went to it. `ismine: false`.

    A database holding exactly one swap is the case where a recency fallback
    looks harmless and is not: it returns that swap for ANY address typed.
    """
    conn = seeded_db()
    try:
        assert subject.find_swap(conn, deposit_address="d" * 64) is None
        assert subject.find_swap(conn, swap_id="s_nonexistent") is None
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# READING THE LEDGER'S REPLY


def test_a_block_index_reads_as_sent():
    moved, lines = subject.send_outcome("(variant { Ok = 42 : nat64 })")
    assert moved
    assert any("block index 42" in line for line in lines)


def test_txduplicate_reads_as_sent_because_it_is():
    """The ledger is saying "this exact transfer already happened; here is where".

    chains/icp.py shipped a version that RAISED on this and records why that was
    a defect: a worker retrying after a timeout marked a payout that SUCCEEDED as
    failed. The whole point of the idempotency key is to produce this reply.
    """
    reply = "(variant { Err = variant { TxDuplicate = record { duplicate_of = 7 : nat64 } } })"
    moved, lines = subject.send_outcome(reply)
    assert moved, "TxDuplicate must read as success; it is what dedup looks like"
    assert any("7" in line for line in lines)


def test_a_reply_with_no_block_index_does_not_claim_either_outcome():
    """MUTATION: make this say "failed".

    "Whether anything moved is NOT established" is the honest reading, and the
    difference matters: a caller told "failed" retries with a fresh key.
    """
    moved, lines = subject.send_outcome("(variant { Err = variant { Something = null } })")
    assert not moved
    joined = " ".join(lines)
    assert "NOT established" in joined
    assert "failed" not in joined.lower()


def test_badfee_is_named_and_not_retried():
    """A retry with a different fee is a different transfer under the dedup key."""
    reply = "(variant { Err = variant { BadFee = record { expected_fee = record { e8s = 10_000 } } } })"
    moved, lines = subject.send_outcome(reply)
    assert not moved
    assert any("BadFee" in line and "NOT retried" in line for line in lines)


# ---------------------------------------------------------------------------
# THE BANNER, AND THE EXIT CODES


class Args:
    def __init__(self, apply=False, identity=""):
        self.apply = apply
        self.identity = identity


ICP_SETTINGS = {
    "ledger_canister_id": "bkyz2-fmaaa-aaaaa-qaaaq-cai",
    "service": "icp-replica",
    "timeout": 60.0,
}


def test_the_banner_says_whether_funds_will_move():
    """Rule 14: the parameter that decides the answer, before anything happens."""
    assert "DRY RUN" in subject.banner_lines(Args(apply=False), ICP_SETTINGS)[0]
    assert "funds WILL move" in subject.banner_lines(Args(apply=True), ICP_SETTINGS)[0]


def test_the_banner_names_the_ledger_and_distinguishes_it_from_mainnet():
    """The IC has no testnet, so the ledger id IS the network statement."""
    joined = " ".join(subject.banner_lines(Args(), ICP_SETTINGS))
    assert "bkyz2-fmaaa-aaaaa-qaaaq-cai" in joined
    assert "ryjl3-tyaaa-aaaaa-aaaba-cai" in joined, "mainnet's id must be named to contrast with"


def test_the_banner_says_which_identity_signs():
    assert "dfx default identity" in " ".join(subject.banner_lines(Args(), ICP_SETTINGS))
    named = " ".join(subject.banner_lines(Args(identity="desk"), ICP_SETTINGS))
    assert "--identity desk" in named


def test_configuration_faults_exit_2_and_state_faults_exit_1():
    """So a missing ledger id does not read like an expired swap."""
    assert subject.Refused(2, ["config"]).code == 2
    assert subject.Refused(1, ["state"]).code == 1


def test_an_unset_ledger_id_refuses_with_code_2_and_says_how_to_read_it():
    """Environment state, so there is no default worth guessing."""
    with pytest.raises(subject.Refused) as caught:
        subject.preflight(Args(), {**ICP_SETTINGS, "ledger_canister_id": ""})
    assert caught.value.code == 2
    assert any("canister_ids.json" in line for line in caught.value.lines)
