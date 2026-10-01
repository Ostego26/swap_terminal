"""Settled transactions are not re-read, and a credit still happens without them.

Role: test (real database on the real schema; a counting stub adapter, no socket)
Reads: services/deposit_service.py, chains/solana.py's skip
Writes: a temp database under pytest's tmp_path
Can move funds: no
Mainnet-safe: yes

WHY THIS FILE EXISTS, MEASURED ON THE OPERATOR'S HOST 2026-10-01.

A devnet SOL deposit was sent, finalized on chain, and never credited. The
deposit watcher's log:

    SOL deposit scan for CUBnQ5QB... read 0 of 7 listed transaction(s);
    7 were unreadable and are named above.
    getSignaturesForAddress returned HTTP 429:
      "Connection rate limits exceeded"

Solana discovery is the only chain here that costs one RPC call PER TRANSACTION.
find_deposits_to_address listed every signature on the shared deposit account
and called getTransaction on ALL of them, every cycle -- including five credited
hours earlier. With two open swaps plus the per-cycle reconciler that is roughly
24 calls every 15 seconds against a public endpoint, and it grows without bound
as the account accumulates history. The endpoint started refusing, nothing
credited, and eventually the signature call itself 429'd and killed the worker.

The guard in workers/common.py keeps the worker alive. This is the cause.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "swap_terminal"))

import pytest
from db import SCHEMA, apply_migrations, connect_db

from swap_terminal.services import deposit_service

ACCOUNT = "CUBnQ5QBfYkL71TCqSdecAQ9xjfGmAdu6Hs3fjQeLorp"
CONFIG = {"AMOUNT_TOLERANCE_PCT": 0.01, "SOL_DEPOSIT_ACCOUNT": ACCOUNT, "SOL_MIN_CONFIRMATIONS": 3}


class CountingAdapter:
    """Records which signatures a scan asked to read, like the real N+1 cost.

    NOT a mock of the skip. It reproduces the one property that makes the skip
    matter -- a per-transaction read -- so the test measures calls avoided rather
    than asserting that a parameter was passed.
    """

    can_spend = True
    payout_refusal = ""

    def __init__(self, events):
        self.events = events
        self.read = []

    def find_deposits_to_address(self, address, tx_limit=None, settled_txids=frozenset()):
        out = []
        for event in self.events:
            if event["txid"] in settled_txids:
                continue
            self.read.append(event["txid"])
            out.append(dict(event))
        return out

    def validate_address(self, address):
        return True


def event(txid, tag, amount=0.01, confirmations=3):
    return {"txid": txid, "vout": tag, "address": ACCOUNT, "amount": amount,
            "confirmations": confirmations}


@pytest.fixture
def db(tmp_path):
    conn = connect_db(str(tmp_path / "t.db"))
    conn.executescript(SCHEMA)
    apply_migrations(conn)
    conn.execute(
        "INSERT INTO quotes (id, from_asset, to_asset, input_amount, quoted_rate, fee_bps,"
        " network_fee_reserve, output_amount_estimate, expires_at, created_at)"
        " VALUES ('q','SOL','GRC',0.01,8700.0,150,0.01,86.0,"
        "'2999-01-01T00:00:00+00:00','2026-10-01T00:00:00+00:00')"
    )
    conn.commit()
    return conn


def seed_swap(db, swap_id, tag, *, status="awaiting_deposit", min_conf=3):
    db.execute(
        "INSERT INTO swaps (id, quote_id, from_asset, to_asset, deposit_address, deposit_tag,"
        " payout_address, expected_input_amount, quoted_rate, fee_bps, network_fee_reserve,"
        " output_amount_estimate, status, min_confirmations, expires_at, created_at, updated_at)"
        " VALUES (?,'q','SOL','GRC',?,?,'mgFsSymndJpBm5FGpLVTabndYBQdcH4x3b',0.01,8700.0,150,0.01,"
        "86.0,?,?,'2999-01-01T00:00:00+00:00','2026-10-01T00:00:00+00:00','2026-10-01T00:00:00+00:00')",
        (swap_id, ACCOUNT, tag, status, min_conf),
    )
    db.commit()


def seed_deposit_event(db, swap_id, txid, tag, confirmations):
    db.execute(
        "INSERT INTO deposit_events (swap_id, asset, txid, vout, address, amount, confirmations,"
        " first_seen_at, last_seen_at)"
        " VALUES (?,'SOL',?,?,?,0.01,?,'2026-10-01T00:00:00+00:00','2026-10-01T00:00:00+00:00')",
        (swap_id, txid, tag, ACCOUNT, confirmations),
    )
    db.commit()


# --- which transactions are settled -------------------------------------------

def test_a_deposit_at_its_threshold_is_settled(db):
    seed_swap(db, "s_done", 1, min_conf=3)
    seed_deposit_event(db, "s_done", "tx_done", 1, confirmations=3)

    assert deposit_service.settled_txids(db, "SOL") == {"tx_done"}


def test_a_deposit_still_confirming_is_NEVER_settled(db):
    """The assertion the whole fix rests on.

    MUTATION: compare on `>= 1` or on "has a row" and this fails -- and the live
    consequence is a deposit that stops being watched before it is credited,
    which is strictly worse than the rate limiting this fix is for.
    """
    seed_swap(db, "s_confirming", 2, min_conf=3)
    seed_deposit_event(db, "s_confirming", "tx_pending", 2, confirmations=1)

    assert deposit_service.settled_txids(db, "SOL") == frozenset()


def test_the_threshold_comes_from_the_SWAP_and_not_from_config(db):
    """min_confirmations is copied onto each swap AT CREATION.

    A swap created when SOL_MIN_CONFIRMATIONS was 1 must settle at 1 even though
    the setting is 3 now. Reading the current config would re-read that
    transaction forever; reading it the other way round would stop watching a
    swap that had not reached its own threshold.
    """
    seed_swap(db, "s_old", 3, min_conf=1)
    seed_deposit_event(db, "s_old", "tx_old", 3, confirmations=1)

    assert deposit_service.settled_txids(db, "SOL") == {"tx_old"}, (
        "settled at the swap's own threshold of 1, not at the current config's 3"
    )


def test_another_asset_s_deposits_are_not_included(db):
    """The set is per asset, because the scan it feeds is per account."""
    seed_swap(db, "s_sol", 4, min_conf=3)
    seed_deposit_event(db, "s_sol", "tx_sol", 4, confirmations=3)

    assert deposit_service.settled_txids(db, "GRC") == frozenset()


# --- what the scan actually re-reads ------------------------------------------

def test_a_settled_transaction_is_not_read_again(db):
    """The call avoided, counted. This is the 429 that cost a credited deposit.

    MUTATION: drop settled_txids from the refresh call site and the adapter reads
    both transactions -- which is the behaviour measured on the operator's host,
    where seven were re-read every fifteen seconds.
    """
    seed_swap(db, "s_new", 9, status="awaiting_deposit")
    seed_swap(db, "s_old", 8, status="awaiting_deposit")
    seed_deposit_event(db, "s_old", "tx_settled", 8, confirmations=3)

    adapter = CountingAdapter([event("tx_settled", 8), event("tx_fresh", 9)])
    swap = dict(db.execute("SELECT * FROM swaps WHERE id = 's_new'").fetchone())
    deposit_service.refresh_swap_from_chain(db, CONFIG, {"SOL": adapter}, swap)

    assert "tx_settled" not in adapter.read, "a settled transaction must not cost another RPC call"
    assert adapter.read == ["tx_fresh"], f"only the unsettled one, got {adapter.read}"


def test_the_swap_still_credits_even_though_its_deposit_was_not_re_read(db):
    """THE SAFETY CLAIM, checked rather than argued.

    refresh_swap_from_chain() upserts the scanned events and then reads every
    stored deposit_events row back out of the database, computing confirmed_total
    and every status transition from THOSE. So a settled deposit that the scan
    skipped entirely must still drive the swap forward.

    Here the scan returns NOTHING at all -- the only transaction is settled -- and
    the swap must still reach payout_pending off the stored row.

    MUTATION: make the status decisions read the scanned events instead of the
    stored rows and this fails, which is the design this test protects.
    """
    seed_swap(db, "s_credit", 7, status="confirming")
    seed_deposit_event(db, "s_credit", "tx_settled", 7, confirmations=3)

    adapter = CountingAdapter([event("tx_settled", 7)])
    swap = dict(db.execute("SELECT * FROM swaps WHERE id = 's_credit'").fetchone())
    deposit_service.refresh_swap_from_chain(db, CONFIG, {"SOL": adapter}, swap)

    assert adapter.read == [], "nothing needed re-reading"
    row = db.execute("SELECT status, credited_at FROM swaps WHERE id = 's_credit'").fetchone()
    assert row["status"] == "payout_pending", "the stored row credits the swap on its own"
    assert row["credited_at"] is not None


def test_the_reconciler_also_skips_settled_transactions(db):
    """It runs once per cycle whether or not a swap is open.

    On a quiet system this scan was the ENTIRE source of the rate limiting, so
    skipping here matters more than in the per-swap refresh.
    """
    seed_swap(db, "s_done", 5, status="completed")
    seed_deposit_event(db, "s_done", "tx_settled", 5, confirmations=3)

    adapter = CountingAdapter([event("tx_settled", 5), event("tx_orphan", 99)])
    deposit_service.reconcile_shared_accounts(db, CONFIG, {"SOL": adapter})

    assert adapter.read == ["tx_orphan"], f"only the unclaimed one, got {adapter.read}"
    # And the orphan is still recorded, so skipping did not cost the reconciler
    # the thing it exists for.
    rows = db.execute("SELECT txid FROM unattributable_deposits").fetchall()
    assert [row["txid"] for row in rows] == ["tx_orphan"]
