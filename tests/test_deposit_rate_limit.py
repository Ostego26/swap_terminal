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

    def find_deposits_to_address(self, address, tx_limit=None, skip_txids=frozenset()):
        out = []
        for event in self.events:
            if event["txid"] in skip_txids:
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

    MUTATION: drop skip_txids from the refresh call site and the adapter reads
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


# --- the half the first fix missed --------------------------------------------
#
# MEASURED ON THE OPERATOR'S HOST ACROSS 2026-10-01 AND 10-02. Two signatures
# burned the devnet rate limit on every cycle for two days:
#
#     could not read transaction 5rHDrJYp... HTTP 429 ... SKIPPED it
#     could not read transaction 61otPXfy... HTTP 429 ... SKIPPED it
#     read 5 of 7 listed transaction(s); 2 were unreadable
#
# settled_txids() is a JOIN from deposit_events to swaps, so it can only name a
# transaction that reached a swap. Those two reached none -- that is what
# unattributable MEANS -- so nothing could skip them, and the limit they burned is
# what made a REAL deposit come back as "read 5 of 7".

#: The operator's two actual stranded signatures, so the fixture is their case.
STRANDED = (
    "5rHDrJYpZJA58kg7r11kCL4wcVKCvmuLMaGtUsN5dAgaQ5BU1jEj7bmh5rM7HkCJM8rfooQJpQRvJcGUreRW1mnK",
    "61otPXfyEEwuUstjmroX5gvkR4v142mT1ZcBAGn5Goy1k1rhWZHKSR2skQZdtdGSNTCHabhabu2PaJxq2Zt6RKAC",
)


def seed_unattributable(db, txid: str, *, asset: str = "SOL", resolved: str | None = None) -> None:
    """One recorded unattributable deposit, through the real table."""
    db.execute(
        "INSERT INTO unattributable_deposits (asset, txid, address, amount, credits,"
        " discriminator, why, confirmations, first_seen_at, last_seen_at, resolved_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (asset, txid, ACCOUNT, 0.05, 1, None,
         "no memo instruction -- unattributable, and a human has to match it",
         3, "2026-10-01T00:00:00+00:00", "2026-10-01T00:00:00+00:00", resolved),
    )
    db.commit()


def test_a_recorded_unattributable_txid_is_in_the_skip_set(db):
    """The set the adapter is handed, built from BOTH sources."""
    seed_unattributable(db, STRANDED[0])
    assert deposit_service.unattributable_txids(db, "SOL") == {STRANDED[0]}
    assert deposit_service.skip_txids(db, "SOL") == {STRANDED[0]}
    assert deposit_service.settled_txids(db, "SOL") == frozenset(), (
        "it reached no swap, which is exactly why settled_txids could never name it"
    )


def test_both_sources_land_in_one_set(db):
    """A settled txid and an unattributable one, together, from one call."""
    seed_swap(db, "s_one", 1)
    seed_deposit_event(db, "s_one", "tx_done", 1, 3)
    seed_unattributable(db, STRANDED[1])

    combined = deposit_service.skip_txids(db, "SOL")
    assert combined == {"tx_done", STRANDED[1]}


def test_a_resolved_unattributable_txid_is_still_skipped(db):
    """A deposit a human has already dealt with is a STRONGER reason not to re-read
    it than an open one, not a weaker one."""
    seed_unattributable(db, STRANDED[0], resolved="2026-10-02T00:00:00+00:00")
    assert STRANDED[0] in deposit_service.skip_txids(db, "SOL")


def test_the_skip_set_is_per_asset(db):
    """A GRC row must not suppress a SOL read. Same property settled_txids has."""
    seed_unattributable(db, STRANDED[0], asset="GRC")
    assert deposit_service.skip_txids(db, "SOL") == frozenset()
    assert deposit_service.skip_txids(db, "GRC") == {STRANDED[0]}


def test_the_stranded_signatures_are_not_read_again_on_a_refresh(db):
    """THE 429 LEAK, as calls avoided rather than as a parameter passed.

    The CountingAdapter reproduces the one property that makes the skip matter --
    a per-transaction read -- so this measures what the operator's rate limit was
    actually paying for.
    """
    seed_swap(db, "s_live", 11)
    for txid in STRANDED:
        seed_unattributable(db, txid)

    events = [event(STRANDED[0], None, amount=0.05), event(STRANDED[1], None, amount=0.05),
              event("tx_real", 11)]
    adapter = CountingAdapter(events)
    swap_row = db.execute("SELECT * FROM swaps WHERE id = 's_live'").fetchone()
    deposit_service.refresh_swap_from_chain(db, CONFIG, {"SOL": adapter}, dict(swap_row))

    assert adapter.read == ["tx_real"], (
        f"the two recorded-unattributable signatures were read again: {adapter.read}"
    )


def test_a_real_deposit_still_credits_with_stranded_rows_present(db):
    """The property that makes the skip safe to ship: it must not suppress the swap.

    The operator's failure mode was the opposite -- the stranded pair's rate limit
    starved a real deposit out of being credited -- so this asserts the deposit
    still lands.
    """
    seed_swap(db, "s_live", 11)
    for txid in STRANDED:
        seed_unattributable(db, txid)

    adapter = CountingAdapter([event(STRANDED[0], None, amount=0.05), event("tx_real", 11)])
    swap_row = db.execute("SELECT * FROM swaps WHERE id = 's_live'").fetchone()
    refreshed = deposit_service.refresh_swap_from_chain(db, CONFIG, {"SOL": adapter}, dict(swap_row))

    assert refreshed["status"] == "payout_pending"
    assert float(refreshed["actual_input_amount"]) == pytest.approx(0.01)


def test_a_skipped_unattributable_row_keeps_the_row_it_already_has(db):
    """record_unattributable() UPSERTs, so leaving a txid out of `events` cannot
    delete what is already recorded -- which is what makes skipping it safe.

    What stops advancing is last_seen_at and confirmations. That is correct:
    nothing is looking at the transaction any more, and first_seen_at is the figure
    a human matching it works from.
    """
    seed_unattributable(db, STRANDED[0])
    before = db.execute(
        "SELECT * FROM unattributable_deposits WHERE txid = ?", (STRANDED[0],)
    ).fetchone()

    seed_swap(db, "s_live", 11)
    adapter = CountingAdapter([event(STRANDED[0], None, amount=0.05), event("tx_real", 11)])
    swap_row = db.execute("SELECT * FROM swaps WHERE id = 's_live'").fetchone()
    deposit_service.refresh_swap_from_chain(db, CONFIG, {"SOL": adapter}, dict(swap_row))

    after = db.execute(
        "SELECT * FROM unattributable_deposits WHERE txid = ?", (STRANDED[0],)
    ).fetchone()
    assert after is not None, "the row must survive its transaction being skipped"
    assert after["amount"] == before["amount"]
    assert after["why"] == before["why"]
    assert after["first_seen_at"] == before["first_seen_at"], (
        "first_seen_at is what a human matching this works from"
    )


# --- the two status transitions nothing took ----------------------------------
#
# MEASURED 2026-10-02 with `coverage run --branch` over the whole suite:
# services/deposit_service.py sat at 97% with lines 350-351 and the False side of
# 354 never taken. Both are on the credit path, and the first is the ORDINARY
# progression for a Solana deposit.


def test_a_deposit_seen_at_zero_then_partly_confirmed_moves_to_confirming(db):
    """THE ORDINARY TWO-CYCLE PROGRESSION, and nothing exercised it.

    The second `confirming` transition can only fire when the swap was ALREADY
    `deposit_seen` on entry: the first block takes an awaiting_deposit swap
    straight to `confirming` whenever max_confirmations > 0, so this one needs a
    PREVIOUS cycle to have seen the deposit at zero. On Solana that is the normal
    shape -- a transaction is listed at `processed` or `confirmed` (rank 1 or 2)
    before it is finalized at 3.

    Two refreshes, which is what makes it a real test rather than a seeded status.
    """
    seed_swap(db, "s_grow", 5)

    first = CountingAdapter([event("tx_grow", 5, confirmations=0)])
    row = dict(db.execute("SELECT * FROM swaps WHERE id = 's_grow'").fetchone())
    after_first = deposit_service.refresh_swap_from_chain(db, CONFIG, {"SOL": first}, row)
    assert after_first["status"] == "deposit_seen", "zero confirmations is seen, not confirming"

    second = CountingAdapter([event("tx_grow", 5, confirmations=1)])
    after_second = deposit_service.refresh_swap_from_chain(
        db, CONFIG, {"SOL": second}, dict(after_first))

    assert after_second["status"] == "confirming", (
        "1 of 3 is confirming -- past zero and short of the threshold"
    )
    assert after_second["credited_at"] is None, "and it must NOT be credited yet"


def test_a_swap_already_under_review_is_not_re_flagged_every_cycle(db):
    """The False side of the `current_status != "under_review"` guard.

    Without it, every refresh of a halted swap would rewrite failed_reason and
    push another identical audit row -- and that trail is the only record of why
    a person is owed an answer.

    WHAT IT DOES NOT PROTECT, measured by this test's first version asserting that
    it did: `updated_at` advances anyway. An unconditional
    `UPDATE swaps SET actual_input_amount = ?, deposit_txid = ?, updated_at = ?`
    runs near the top of refresh_swap_from_chain(), before any status logic, so
    every refresh moves it whatever the guard decides.

    THAT MAKES show_swap.py's "halted since ... <- swaps.updated_at, the moment
    the status changed" TRUE ONLY BY ACCIDENT: updated_at is not the moment the
    status changed, it is the last refresh. It reads correctly today because
    under_review is outside deposit_service.ACTIVE_STATUSES, so the watcher never
    polls a halted swap and the column freezes at the halt. A future change to
    that tuple would make an operator's "halted 3µfn ago" wrong about a swap that
    had been waiting for days, and nothing would fail. Recorded here rather than
    fixed: the annotation is accurate for every path that runs today, and
    rewriting the column's semantics is a change to what every other reader of it
    means (rule 16's line).

    Reachable because this function is public: anything calling the refresh
    directly reaches it, which is what this test does.
    """
    seed_swap(db, "s_halt", 7)
    # 0.05 against an expected 0.01 is far outside AMOUNT_TOLERANCE_PCT.
    adapter = CountingAdapter([event("tx_big", 7, amount=0.05)])
    row = dict(db.execute("SELECT * FROM swaps WHERE id = 's_halt'").fetchone())
    halted = deposit_service.refresh_swap_from_chain(db, CONFIG, {"SOL": adapter}, row)

    assert halted["status"] == "under_review"
    first_reason = halted["failed_reason"]
    audit_before = db.execute(
        "SELECT COUNT(*) AS n FROM swap_audit_log WHERE swap_id = 's_halt'"
    ).fetchone()["n"]

    again = deposit_service.refresh_swap_from_chain(
        db, CONFIG, {"SOL": CountingAdapter([event("tx_big", 7, amount=0.05)])}, dict(halted))

    assert again["status"] == "under_review"
    assert again["failed_reason"] == first_reason, "the reason must not be rewritten"
    audit_after = db.execute(
        "SELECT COUNT(*) AS n FROM swap_audit_log WHERE swap_id = 's_halt'"
    ).fetchone()["n"]
    assert audit_after == audit_before, "no second identical audit row"
