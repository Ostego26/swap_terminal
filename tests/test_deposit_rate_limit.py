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

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "swap_terminal"))

import pytest
from db import SCHEMA, apply_migrations, connect_db
from valid_addresses import BTC_PARTICIPANT, BTC_REFUND, GRC_PAYOUT, solana_address_for

from swap_terminal.chains.solana import UnattributableCredit
from swap_terminal.services import deposit_service
from swap_terminal.services.xrp_tag_service import allocate_destination_tag

ACCOUNT = "CUBnQ5QBfYkL71TCqSdecAQ9xjfGmAdu6Hs3fjQeLorp"
CONFIG = {"AMOUNT_TOLERANCE_PCT": 0.01, "SOL_DEPOSIT_ACCOUNT": ACCOUNT, "SOL_MIN_CONFIRMATIONS": 3}

#: The account a swap created BEFORE an operator repointed SOL_DEPOSIT_ACCOUNT still names.
#: DERIVED rather than typed, which tests/test_address_literals_are_valid.py asks for by
#: name: a derived address cannot be mistyped, and the phrase says what it is for.
PREVIOUS_ACCOUNT = solana_address_for("swap_terminal shared scan previous deposit account")


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
        # EVERY SCAN, IN ORDER, WITH THE ADDRESS IT ASKED ABOUT. `read` counts the
        # per-transaction getTransaction cost; this counts the getSignaturesForAddress
        # that precedes it, which is the call the shared-scan change removes. Measured
        # 2026-10-02 on the live host: 4 of these per cycle, 818/hour.
        self.scans = []

    def find_deposits_to_address(self, address, tx_limit=None, skip_txids=frozenset()):
        self.scans.append(address)
        out = []
        for event in self.events:
            if event["txid"] in skip_txids:
                continue
            self.read.append(event["txid"])
            out.append(dict(event))
        return out

    def validate_address(self, address):
        return True

    def owns_address(self, address):
        """Not the desk's -- these fixtures all supply a CUSTOMER payout address.

        Added 2026-10-04 with the ownership gate in services/swap_service.
        refuse_unusable_payout_address(). A destination stub without it raises
        AttributeError, so the gate could not be exercised and every create_swap
        test failed on the harness instead of on its own subject. False is the
        honest default here; a test that wants the refusal returns True.
        """
        return False


class DroppingAdapter(CountingAdapter):
    """A CountingAdapter that also carries refused credits, like the real Solana one.

    `unattributable_drops` is what chains/solana.find_deposits_to_address() populates for
    every credit it could not attribute, and record_what_nobody_can_claim() asks for it BY
    NAME rather than by isinstance -- so growing the attribute is how a test, or a new
    tag-attributed adapter, opts in. Three of the four live adapters are UTXO chains and
    have no such attribute at all, which is why CountingAdapter above does not either.
    """

    def __init__(self, events, drops=()):
        super().__init__(events)
        self._drops = list(drops)
        # EMPTY UNTIL A SCAN RUNS, which is the real adapter's lifetime and not a detail.
        # chains/solana.find_deposits_to_address() assigns `self.unattributable_drops = []`
        # at the top of every call and appends during it, so the list describes THAT scan.
        # A stub that populated this in __init__ makes "record the drops before the scan"
        # look correct -- and that mutation SURVIVED against the first version of this
        # class, which is why the lifetime is reproduced here rather than the value.
        self.unattributable_drops = []

    def find_deposits_to_address(self, address, tx_limit=None, skip_txids=frozenset()):
        self.unattributable_drops = list(self._drops)
        return super().find_deposits_to_address(address, tx_limit=tx_limit, skip_txids=skip_txids)


def a_drop(signature, amount=0.25, credits=1):
    """One refused credit in the shape chains/solana.UnattributableCredit has."""
    return UnattributableCredit(
        signature=signature, credits=credits, amount=amount, address=ACCOUNT,
        why="no usable memo instruction, so nothing says whose money this is",
    )


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

    assert deposit_service.settled_txids(db, "SOL", address=ACCOUNT) == {"tx_done"}


def test_a_deposit_still_confirming_is_NEVER_settled(db):
    """The assertion the whole fix rests on.

    MUTATION: compare on `>= 1` or on "has a row" and this fails -- and the live
    consequence is a deposit that stops being watched before it is credited,
    which is strictly worse than the rate limiting this fix is for.
    """
    seed_swap(db, "s_confirming", 2, min_conf=3)
    seed_deposit_event(db, "s_confirming", "tx_pending", 2, confirmations=1)

    assert deposit_service.settled_txids(db, "SOL", address=ACCOUNT) == frozenset()


def test_the_threshold_comes_from_the_SWAP_and_not_from_config(db):
    """min_confirmations is copied onto each swap AT CREATION.

    A swap created when SOL_MIN_CONFIRMATIONS was 1 must settle at 1 even though
    the setting is 3 now. Reading the current config would re-read that
    transaction forever; reading it the other way round would stop watching a
    swap that had not reached its own threshold.
    """
    seed_swap(db, "s_old", 3, min_conf=1)
    seed_deposit_event(db, "s_old", "tx_old", 3, confirmations=1)

    assert deposit_service.settled_txids(db, "SOL", address=ACCOUNT) == {"tx_old"}, (
        "settled at the swap's own threshold of 1, not at the current config's 3"
    )


def test_another_asset_s_deposits_are_not_included(db):
    """The set is per asset, because the scan it feeds is per account."""
    seed_swap(db, "s_sol", 4, min_conf=3)
    seed_deposit_event(db, "s_sol", "tx_sol", 4, confirmations=3)

    assert deposit_service.settled_txids(db, "GRC", address=ACCOUNT) == frozenset()


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


def seed_unattributable(
    db, txid: str, *, asset: str = "SOL", resolved: str | None = None,
    discriminator: int | None = None,
) -> None:
    """One recorded unattributable deposit, through the real table.

    `discriminator` DEFAULTS TO None BECAUSE THE OPERATOR'S FIRST TWO ROWS WERE None --
    no memo at all -- and it is a parameter because that column is now what decides
    whether the scan reads the transaction again. unattributable_deposit_service.
    skippable_unattributable_txids() skips a NULL discriminator by construction (no swap
    can ever match it) and keeps reading an integer one that no swap holds yet, because a
    later allocation can hand that tag to a swap that owns the money.
    """
    db.execute(
        "INSERT INTO unattributable_deposits (asset, txid, address, amount, credits,"
        " discriminator, why, confirmations, first_seen_at, last_seen_at, resolved_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (asset, txid, ACCOUNT, 0.05, 1, discriminator,
         "no memo instruction -- unattributable, and a human has to match it",
         3, "2026-10-01T00:00:00+00:00", "2026-10-01T00:00:00+00:00", resolved),
    )
    db.commit()


def skippable(db, asset="SOL"):
    """The real narrowed predicate, asked the way skip_txids() asks it."""
    return deposit_service.skippable_unattributable_txids(
        db, asset, deposit_service.ACTIVE_STATUSES, address=ACCOUNT
    )


def test_a_recorded_unattributable_txid_WITH_NO_DISCRIMINATOR_is_in_the_skip_set(db):
    """The set the adapter is handed, built from BOTH sources.

    NO DISCRIMINATOR IS THE UNCLAIMABLE SHAPE, and that is why this row is skipped
    rather than because it is recorded. attributable_events() credits an event only
    when its `vout` EQUALS a swap's deposit_tag, and NULL equals nothing -- so no
    swap, present or future, can ever take this payment.
    """
    seed_unattributable(db, STRANDED[0])
    assert skippable(db) == {STRANDED[0]}
    assert deposit_service.skip_txids(db, "SOL", address=ACCOUNT) == {STRANDED[0]}
    assert deposit_service.settled_txids(db, "SOL", address=ACCOUNT) == frozenset(), (
        "it reached no swap, which is exactly why settled_txids could never name it"
    )


def test_both_sources_land_in_one_set(db):
    """A settled txid and an unattributable one, together, from one call."""
    seed_swap(db, "s_one", 1)
    seed_deposit_event(db, "s_one", "tx_done", 1, 3)
    seed_unattributable(db, STRANDED[1])

    combined = deposit_service.skip_txids(db, "SOL", address=ACCOUNT)
    assert combined == {"tx_done", STRANDED[1]}


def test_a_resolved_unattributable_txid_is_still_skipped(db):
    """A deposit a human has already dealt with is a STRONGER reason not to re-read
    it than an open one, not a weaker one."""
    seed_unattributable(db, STRANDED[0], resolved="2026-10-02T00:00:00+00:00")
    assert STRANDED[0] in deposit_service.skip_txids(db, "SOL", address=ACCOUNT)


def test_the_skip_set_is_per_asset(db):
    """A GRC row must not suppress a SOL read. Same property settled_txids has."""
    seed_unattributable(db, STRANDED[0], asset="GRC")
    assert deposit_service.skip_txids(db, "SOL", address=ACCOUNT) == frozenset()
    assert deposit_service.skip_txids(db, "GRC", address=ACCOUNT) == {STRANDED[0]}


# --- the rows the scan must KEEP reading --------------------------------------
#
# THE DEFECT THESE PIN, AND IT WAS MINE, SHIPPED 2026-10-02. skip_txids() was
# settled_txids() | every row in unattributable_deposits, so a payment recorded here
# was never read from the chain again -- which made
# unattributable_deposit_service.resolve_credited()'s own documented path unreachable:
# "a sender who pays ... a tag whose swap does not exist yet, lands here first, and if a
# swap is subsequently created and its refresh credits that txid, the stranded row is
# answered". Nothing could credit it, because nothing would ever see the event again.
#
# The narrowing is the OPPOSITE of the obvious one: resolved rows stay skipped, and the
# unresolved ones come back -- but only the unresolved ones a later swap could actually
# claim, which is what keeps the 429 cost at zero on the operator's host.


def test_the_allocator_WILL_hand_a_later_swap_a_tag_above_the_high_water_mark(db):
    """The load-bearing claim, driven through the real allocator rather than read off it.

    The whole narrowing rests on this: a sender can put a reference on a payment that no
    swap holds YET, and a swap created afterwards is genuinely handed that number.
    `_ALLOCATE_SQL` is `COALESCE(MAX(destination_tag), :below_first) + 1`, so every
    integer above the current mark is a future tag. Asserted by allocating, because
    "I read the SQL and it looks monotonic" is a hypothesis (rule 17).
    """
    allocated = []
    for i in range(6):
        seed_swap(db, f"s_alloc{i}", None)
        allocated.append(allocate_destination_tag(db, ACCOUNT, f"s_alloc{i}", "SOL"))
    db.commit()
    assert allocated == [1, 2, 3, 4, 5, 6], (
        f"a payment referencing 5 while only 1 and 2 existed is a payment the FIFTH later "
        f"swap owns; got {allocated}"
    )


def test_a_row_whose_tag_NO_swap_holds_is_READ_AGAIN(db):
    """The claimable shape, as a call MADE rather than as a set computed.

    Nothing holds tag 5, so tag 5 is still to be allocated and this payment may turn out
    to be a swap's. The scan has to keep reading it or the event never reaches the swap
    that owns it.
    """
    seed_unattributable(db, STRANDED[1], discriminator=5)
    assert skippable(db) == frozenset(), "a claimable row is not skippable"

    seed_swap(db, "s_live", 11)
    adapter = CountingAdapter([event(STRANDED[1], 5, amount=0.05), event("tx_real", 11)])
    deposit_service.process_active_swaps(db, CONFIG, {"SOL": adapter})

    assert STRANDED[1] in adapter.read, (
        f"the claimable payment was skipped, so no later swap can ever be handed it; "
        f"read={adapter.read}"
    )


def test_a_payment_recorded_BEFORE_its_swap_existed_is_credited_to_the_LATER_swap(db):
    """THE PROMISED PATH, END TO END, and the reason defect 1 is a money defect.

    resolve_credited()'s docstring promises this and the total skip set made it
    impossible. Here the payment arrives referencing tag 5 before any swap holds it, is
    recorded as unattributable, and THEN the swap that owns tag 5 is created. The real
    process_active_swaps() must credit it and the stranded row must close itself.

    MUTATION -- AND THIS IS THE CALL-SITE ONE. Revert deposit_service.skip_txids() to
    `settled_txids(db, asset) | <every row in unattributable_deposits>` and this fails:
    the event is skipped, deposit_events stays empty, the swap sits in awaiting_deposit
    and the stranded row stays open forever. A narrowed predicate whose CALLER still
    unions the wide set is a correct function nobody asked.
    """
    seed_unattributable(db, STRANDED[1], discriminator=5)
    seed_swap(db, "s_later", 5)

    adapter = CountingAdapter([event(STRANDED[1], 5, amount=0.01, confirmations=3)])
    deposit_service.process_active_swaps(db, CONFIG, {"SOL": adapter})

    credited = db.execute(
        "SELECT swap_id, txid FROM deposit_events WHERE txid = ?", (STRANDED[1],)
    ).fetchall()
    assert [dict(row) for row in credited] == [{"swap_id": "s_later", "txid": STRANDED[1]}], (
        f"the payment reached no swap; deposit_events={[dict(r) for r in credited]}"
    )
    swap = db.execute("SELECT status FROM swaps WHERE id = 's_later'").fetchone()
    assert swap["status"] == "payout_pending"
    row = db.execute(
        "SELECT resolved_at, resolution_note FROM unattributable_deposits WHERE txid = ?",
        (STRANDED[1],),
    ).fetchone()
    assert row["resolved_at"] is not None, (
        "the stranded row stayed open on a payment that WAS credited, which is the false "
        "positive resolve_credited() exists to close"
    )
    assert "credited after all" in row["resolution_note"]


def test_the_operator_s_OWN_unresolved_row_is_still_skipped(db):
    """THE COST HALF, measured against their actual row rather than a hypothetical.

    Their one unresolved row is 61otPXfy..., 0.05 SOL with memo 2, against swap
    s_ba72c715150a063b -- which is `failed`. A failed swap is never refreshed again
    (the test below drives that through the real function), tags are never reissued, so
    no swap present or future can claim this payment. Re-reading it would cost 204.5
    getTransaction/hour on deposit_watcher's 14.5µfn (17.6s) cycle plus 60/hour on
    reconcile_worker, against an endpoint that answers HTTP 429 -- for a verdict that
    cannot change. That is why the predicate is precise instead of "skip only resolved".
    """
    seed_swap(db, "s_failed", 2, status="failed")
    seed_unattributable(db, STRANDED[1], discriminator=2)

    assert skippable(db) == {STRANDED[1]}

    seed_swap(db, "s_live", 11)
    adapter = CountingAdapter([event(STRANDED[1], 2, amount=0.05), event("tx_real", 11)])
    deposit_service.process_active_swaps(db, CONFIG, {"SOL": adapter})
    assert adapter.read == ["tx_real"], (
        f"a payment no swap can ever claim cost a getTransaction; read={adapter.read}"
    )


def test_a_row_whose_tag_belongs_to_an_ACTIVE_swap_is_READ(db):
    """The clause is "a swap that will never be refreshed again", not "any swap".

    A row can be recorded while no swap holds its tag and then the tag gets allocated.
    At that moment the row becomes CLAIMABLE, so a predicate that skipped on "some swap
    holds this tag" would hide the deposit at the exact instant it became creditable.
    """
    seed_swap(db, "s_active", 5, status="confirming")
    seed_unattributable(db, STRANDED[1], discriminator=5)
    assert skippable(db) == frozenset(), (
        "its swap is still refreshed, so its own refresh is what credits this payment"
    )


def test_a_RESOLVED_row_is_skipped_even_when_its_tag_is_unclaimed(db):
    """Resolution outranks claimability, which is the previous version's own argument.

    "A resolved deposit has been dealt with by a human, which is a stronger reason not to
    re-read it than an open one." That sentence survives the narrowing unchanged -- it was
    always right, and it was the open rows it was wrongly applied to.
    """
    seed_unattributable(
        db, STRANDED[1], discriminator=5, resolved="2026-10-02T00:00:00+00:00"
    )
    assert skippable(db) == {STRANDED[1]}


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


# --- the absence the skip set depends on --------------------------------------
#
# unattributable_txids()'s safety argument rests on two claims. One is an
# invariant (a discriminator is never reissued: the allocator is MAX+1). The
# other is an ABSENCE -- nothing revives a swap into ACTIVE_STATUSES -- and rule
# 2's distinction is the whole reason these tests exist: "I could not find a
# revival path" is not "a revival path cannot exist". Adding one is a reasonable
# feature (an operator tool that reopens a failed swap to accept a late payment),
# and the moment it lands the skip set starts hiding the very deposit that tool
# was built to find. These fail then, instead of the money going quiet.


def test_a_FAILED_swap_is_never_refreshed_back_into_an_active_status(db):
    """The absence, driven through the real function rather than grepped for.

    A grep found exactly one set_swap_status(..., "confirming", ...) outside tests,
    guarded by `current_status in {"deposit_seen", "awaiting_deposit"}`. That is
    evidence, not a measurement -- rule 17 -- so this seeds a failed swap with a
    matching on-chain deposit and asserts the real refresh leaves it failed.

    IF THIS TEST EVER FAILS, DO NOT FIX THE TEST. It means a revival path now
    exists, and unattributable_deposit_service.unattributable_txids() must stop
    skipping rows whose `why` came from unclaimed_events() -- read that docstring
    before changing anything here.
    """
    seed_swap(db, "s_failed", 2, status="failed")
    adapter = CountingAdapter([event(STRANDED[1], 2, confirmations=9)])
    swap = db.execute("SELECT * FROM swaps WHERE id = 's_failed'").fetchone()
    deposit_service.refresh_swap_from_chain(db, CONFIG, {"SOL": adapter}, swap)
    db.commit()
    after = db.execute("SELECT status FROM swaps WHERE id = 's_failed'").fetchone()
    assert after["status"] == "failed", (
        "a failed swap came back to life, so a stranded deposit matching it could now be "
        "credited -- and the skip set would never let the scanner see it again"
    )


def test_the_allocator_never_REISSUES_a_discriminator(db):
    """The invariant half -- and it is a real invariant, which my grep had not established.

    If a tag could be reused, "no swap on this asset has that discriminator" could
    later become "swap Y has it", and swap Y's deposit would be permanently
    invisible: the scanner skips the txid before any swap ever sees the event. That
    is the one case in this whole mechanism that loses a customer's money rather
    than merely freezing a column.

    I ARGUED THIS FROM "nothing outside tests deletes from xrp_destination_tags",
    WHICH IS THE WEAK FORM. The schema is stronger and I had not read it: a BEFORE
    DELETE trigger, xrp_destination_tags_are_never_released, ABORTS the delete, and
    its own message names this hazard -- "a deleted row lets the next tag repeat one
    already given out, and a late payment carrying it would credit the wrong swap".
    A sibling trigger blocks re-pointing account, destination_tag or swap_id.

    So this asserts the trigger rather than the absence of callers. The distinction
    matters: an absence is one commit away from being false, a trigger fails that
    commit.
    """
    seed_swap(db, "s_one", None)
    seed_swap(db, "s_two", None)
    first = allocate_destination_tag(db, ACCOUNT, "s_one", "SOL")
    second = allocate_destination_tag(db, ACCOUNT, "s_two", "SOL")
    db.commit()
    assert second > first, "tags must only ever climb"
    with pytest.raises(sqlite3.IntegrityError, match="never deleted"):
        db.execute(
            "DELETE FROM xrp_destination_tags WHERE destination_tag = ?", (first,)
        )
    db.rollback()
    with pytest.raises(sqlite3.IntegrityError, match="immutable once allocated"):
        db.execute(
            "UPDATE xrp_destination_tags SET swap_id = 's_two' WHERE destination_tag = ?",
            (first,),
        )
    db.rollback()
    assert db.execute(
        "SELECT MAX(destination_tag) AS m FROM xrp_destination_tags WHERE account = ?",
        (ACCOUNT,),
    ).fetchone()["m"] == second, "so MAX() cannot fall and the next tag cannot repeat one"


# --- ONE SCAN PER SHARED ACCOUNT PER CYCLE ------------------------------------
#
# MEASURED ON THE OPERATOR'S LIVE HOST 2026-10-02, from
# swap_terminal/runtime/deposit_watcher.log AND NOWHERE ELSE -- so every figure here is
# deposit_watcher's own, not a tree-wide total:
#
#     scans per cycle                4           3 awaiting_deposit swaps + 1 reconciler
#     cycle period                   14.5µfn (17.6s)   observed gaps 18, 17, 18, 17, 18
#     getSignaturesForAddress/hour   818         204.5 cycles x 4
#     one scan per cycle             204.5/hour
#     saving, IN THAT PROCESS        614/hour    75% of deposit_watcher's own scans
#
# reconcile_worker.py:92 calls the same function at 60s in a SEPARATE PROCESS, with its own
# adapter and its own connection, and that count was not measured. Nothing in this change
# dedupes across the two and nothing could; the two tests at the end of this file are about
# what stops two processes crediting one payment twice, which is a different question.
#
# On a tag-attributed chain every swap shares ONE deposit account, so each of those
# per-swap scans read the same account and got byte-identical results back, and then
# the reconciler read it again. It scaled with OPEN SWAPS, not with deposits.
#
# These tests count SCANS rather than asserting that a parameter was passed, for the
# same reason the file's own CountingAdapter exists: the thing that cost a credited
# deposit was a call being made, so the call is what gets measured.


def seed_btc_swap(db, swap_id, address, *, status="awaiting_deposit"):
    """An ADDRESS-attributed swap. Its behavior must not change by one call.

    BTC, LTC and GRC derive a fresh deposit address per swap, so there is nothing
    shared and nothing to save -- and a change that quietly gave two BTC swaps one
    scan would credit one customer's deposit against the other's swap, which is the
    money bug the whole tag filter exists to prevent on the chains that DO share.
    """
    db.execute(
        "INSERT INTO swaps (id, quote_id, from_asset, to_asset, deposit_address, deposit_tag,"
        " payout_address, expected_input_amount, quoted_rate, fee_bps, network_fee_reserve,"
        " output_amount_estimate, status, min_confirmations, expires_at, created_at, updated_at)"
        " VALUES (?,'q','BTC','GRC',?,NULL,?,0.01,8700.0,150,"
        "0.01,86.0,?,2,'2999-01-01T00:00:00+00:00','2026-10-01T00:00:00+00:00',"
        "'2026-10-01T00:00:00+00:00')",
        (swap_id, address, GRC_PAYOUT, status),
    )
    db.commit()


def test_three_open_swaps_and_the_reconciler_make_ONE_scan_not_four(db):
    """THE CHANGE, counted. Four scans of one account per cycle became one.

    Seeded as the live host was: three awaiting_deposit SOL swaps sharing the one
    configured deposit account, refreshed through the REAL process_active_swaps().

    MUTATION THAT MUST FAIL -- and it is the call-site shape that has survived four
    times in this codebase: pass the per-swap scan anyway by reverting the loop to
    `refresh_swap_from_chain(db, config, adapters, swap)`, or drop `scans=scans` from
    the reconciler call. Either makes this 4 (or 2) instead of 1.
    """
    seed_swap(db, "s_a", 11)
    seed_swap(db, "s_b", 12)
    seed_swap(db, "s_c", 13)
    adapter = CountingAdapter([])

    deposit_service.process_active_swaps(db, CONFIG, {"SOL": adapter})

    assert adapter.scans == [ACCOUNT], (
        f"one scan of the shared account per cycle; got {len(adapter.scans)} "
        f"({adapter.scans}). Four was 818 getSignaturesForAddress/hour at a 17.6s cycle"
    )


def test_the_shared_account_is_still_scanned_with_no_open_swap_at_all(db):
    """The reconciler's scan is the +1, and it must not become a 0.

    A sender who pays late, pays twice, or pays before opening a swap is the
    likeliest way a deposit strands, and that is exactly the case with no active
    swap. An optimization that only scanned when a swap was open would have been
    blind to it -- the same defect reconcile_shared_accounts() already records
    having had once, when it derived its asset set from the active swaps.
    """
    adapter = CountingAdapter([])

    deposit_service.process_active_swaps(db, CONFIG, {"SOL": adapter})

    assert adapter.scans == [ACCOUNT], (
        "with zero open swaps the shared account is still read once, for the reconciler"
    )


def test_every_sharing_swap_is_credited_from_the_one_scan(db):
    """The saving must not cost a credit. Both swaps reach payout_pending from one scan.

    Two SOL swaps, two tagged payments, ONE scan. Each swap gets its own
    deposit_events row at its own tag and its own credit -- which is what the shared
    event list has to deliver, because handing every swap the same list is only sound
    if attributable_events() then gives each one its own slice.

    MUTATION: make refresh_swap_from_chain() ignore `scans` and rescan -- this still
    passes, which is why the scan COUNT is asserted in its own test above. MUTATION
    that fails here: hand each swap the unfiltered list by removing the
    attributable_events() call, and both swaps credit both payments.
    """
    seed_swap(db, "s_a", 11)
    seed_swap(db, "s_b", 12)
    adapter = CountingAdapter([event("tx_for_a", 11, amount=0.01, confirmations=3),
                               event("tx_for_b", 12, amount=0.01, confirmations=3)])

    deposit_service.process_active_swaps(db, CONFIG, {"SOL": adapter})

    assert adapter.scans == [ACCOUNT], "one scan"
    rows = {r["swap_id"]: dict(r) for r in db.execute(
        "SELECT swap_id, txid, vout, amount FROM deposit_events ORDER BY swap_id").fetchall()}
    assert set(rows) == {"s_a", "s_b"}, f"each swap credited from the one scan; got {rows}"
    assert rows["s_a"]["txid"] == "tx_for_a" and rows["s_a"]["vout"] == 11
    assert rows["s_b"]["txid"] == "tx_for_b" and rows["s_b"]["vout"] == 12
    statuses = {r["id"]: r["status"] for r in db.execute("SELECT id, status FROM swaps")}
    assert statuses == {"s_a": "payout_pending", "s_b": "payout_pending"}, (
        f"both credited and advanced from a single shared scan; got {statuses}"
    )


def test_one_event_cannot_be_credited_to_two_swaps(db):
    """THE DOUBLE-CREDIT QUESTION, asked by seeding the thing that should be impossible.

    Two swaps are given the SAME tag by writing the rows directly, which no live path
    does -- xrp_destination_tags holds PRIMARY KEY (account, destination_tag) and
    UNIQUE idx_xrp_tag_one_per_swap, with BEFORE triggers that RAISE(ABORT) on delete
    or re-point, so allocate_destination_tag() cannot produce this. The point is that
    the shared event list does not depend on that uniqueness holding.

    THE BACKSTOP IS db.py's UNIQUE(asset, txid, vout) ON deposit_events, and this is
    what it guarantees: upsert_deposit_event() SELECTs on exactly that triple first
    and UPDATEs confirmations on the row it finds rather than inserting a second one,
    and it never re-points swap_id. refresh_swap_from_chain() then sums only
    `WHERE swap_id = ?`. So one payment can become one row owned by one swap, and the
    second swap credits nothing -- an uncredited swap is a support ticket, a
    double-credited one is a payout against money that arrived once.

    MUTATION: there is no mutation of THIS change that breaks it, which is the
    finding. It is pinned here so that a future change to upsert_deposit_event() --
    an INSERT OR REPLACE, or dropping the SELECT -- fails a test rather than paying
    somebody twice.
    """
    seed_swap(db, "s_first", 11)
    seed_swap(db, "s_second", 11)
    adapter = CountingAdapter([event("tx_once", 11, amount=0.01, confirmations=3)])

    deposit_service.process_active_swaps(db, CONFIG, {"SOL": adapter})

    rows = db.execute("SELECT swap_id, txid, vout FROM deposit_events").fetchall()
    assert len(rows) == 1, f"UNIQUE(asset, txid, vout) allows exactly one row; got {len(rows)}"
    credited = db.execute(
        "SELECT id FROM swaps WHERE credited_at IS NOT NULL ORDER BY id").fetchall()
    assert [r["id"] for r in credited] == [rows[0]["swap_id"]], (
        "the payment credits the swap that owns the row and no other"
    )
    amounts = db.execute(
        "SELECT COALESCE(SUM(actual_input_amount), 0) AS total FROM swaps").fetchone()["total"]
    assert amounts == 0.01, (
        f"0.01 arrived once and 0.01 is credited in total; {amounts} would be a double count"
    )


def test_a_swap_pointing_at_a_repointed_account_is_still_scanned(db):
    """The address is the key, and this is the deposit that keying by ASSET would lose.

    `swaps.deposit_address` is a COPY of the configured account taken at creation, so
    an operator who repoints SOL_DEPOSIT_ACCOUNT leaves every open swap pointing at
    the OLD account -- the one its customer was actually told to pay into. A shared
    scan keyed on the asset alone would have scanned only the new account and
    SILENTLY STOPPED WATCHING that swap.

    MUTATION: key shared_scan_targets() and refresh_swap_from_chain() on the asset
    instead of (asset, address) and this fails -- the swap is handed the new
    account's empty event list and is never credited.
    """
    seed_swap(db, "s_old_account", 11)
    db.execute("UPDATE swaps SET deposit_address = ? WHERE id = 's_old_account'", (PREVIOUS_ACCOUNT,))
    db.commit()
    adapter = CountingAdapter([event("tx_to_old", 11, amount=0.01, confirmations=3)])

    deposit_service.process_active_swaps(db, CONFIG, {"SOL": adapter})

    assert sorted(adapter.scans) == sorted([ACCOUNT, PREVIOUS_ACCOUNT]), (
        f"both the configured account and the swap's own are scanned; got {adapter.scans}"
    )
    row = db.execute("SELECT status, credited_at FROM swaps WHERE id = 's_old_account'").fetchone()
    assert row["status"] == "payout_pending" and row["credited_at"] is not None, (
        "a deposit to the account the customer was given is still credited"
    )


def test_two_address_attributed_swaps_still_get_their_own_scan_each(db):
    """BTC, LTC and GRC are untouched, and the assertion is the scan COUNT.

    Each of these swaps has its OWN deposit address, so there is nothing shared: two
    swaps must still make two scans, each asking about its own address. Collapsing
    them would be the money bug in reverse -- one address's events applied to a swap
    expecting a different address.
    """
    seed_btc_swap(db, "b_one", BTC_PARTICIPANT)
    seed_btc_swap(db, "b_two", BTC_REFUND)
    adapter = CountingAdapter([])

    deposit_service.process_active_swaps(db, CONFIG, {"BTC": adapter})

    assert sorted(adapter.scans) == sorted([BTC_PARTICIPANT, BTC_REFUND]), (
        f"one scan per address-attributed swap, each for its own address; got {adapter.scans}"
    )


def test_a_freshly_credited_deposit_is_not_called_stranded_by_the_shared_scan(db):
    """The 2026-10-01 false alarm, re-pinned under the shared scan.

    The scan now happens BEFORE the loop credits, so its event list is pre-credit --
    and the reconciler would call the very payment the loop just credited "money
    nobody will ever claim" if it took its verdict from that list's age. It does not:
    `claimed` and `credited` are still read from the database AFTER the loop, and
    only the network read moved up.

    MUTATION: compute `credited` inside scan_shared_accounts() and pass it down, or
    move the reconcile_shared_accounts() call above the loop, and a stranded row
    appears for a swap that worked perfectly.
    """
    seed_swap(db, "s_fresh", 11)
    adapter = CountingAdapter([event("tx_fresh", 11, amount=0.01, confirmations=3)])

    deposit_service.process_active_swaps(db, CONFIG, {"SOL": adapter})

    assert db.execute("SELECT status FROM swaps WHERE id = 's_fresh'").fetchone()["status"] == (
        "payout_pending"), "setup: the deposit was credited this cycle"
    stranded = db.execute("SELECT txid FROM unattributable_deposits").fetchall()
    assert stranded == [], (
        f"a deposit credited this cycle is not stranded; got {[r['txid'] for r in stranded]}"
    )


def test_a_payment_matching_no_swap_is_still_recorded_from_the_shared_scan(db):
    """And the reconciler must still DO its job off the reused list.

    The mirror of the test above: a tagged payment no swap claims has to reach
    unattributable_deposits, from the same shared event list. A change that fixed the
    false alarm by never recording anything would pass that test and fail this one.
    """
    seed_swap(db, "s_mine", 11)
    adapter = CountingAdapter([event("tx_nobodys", 999, amount=0.05, confirmations=3)])

    deposit_service.process_active_swaps(db, CONFIG, {"SOL": adapter})

    stranded = [dict(r) for r in db.execute(
        "SELECT txid, discriminator FROM unattributable_deposits").fetchall()]
    assert len(stranded) == 1 and stranded[0]["txid"] == "tx_nobodys", (
        f"the unclaimed payment is recorded from the one scan; got {stranded}"
    )
    assert db.execute(
        "SELECT COUNT(*) AS n FROM deposit_events").fetchone()["n"] == 0, (
        "and it credits nothing: no swap claims tag 999"
    )


def test_the_adapter_s_own_drops_are_recorded_even_with_no_open_swap(db):
    """What moving the reconciler's scan up front BUYS, and it is not only a saved call.

    chains/solana.find_deposits_to_address() records every credit it had to refuse for
    carrying no usable memo on `unattributable_drops`, and record_what_nobody_can_claim()
    is what puts those in SQL. Before this change that only happened inside a per-swap
    refresh, so a cycle with NO open swap read the shared account (the reconciler's scan)
    and threw the adapter's own refusals away -- real money arriving that nobody can claim,
    reaching a log line and nothing else, in exactly the situation that strands a deposit
    most often.

    Now every scan a cycle makes goes through scan_shared_accounts(), which records the
    drops right where they exist. This is an ADDITION to a diagnostic table and it credits
    nothing: unattributable_deposits is read by a person and by show_unattributable.py, and
    no gate anywhere reads it.

    MUTATION: drop the config-account half of shared_scan_targets() and this fails -- the
    reconciler makes its own scan again, outside the one place that records the drops.
    """
    adapter = DroppingAdapter([], drops=[a_drop(signature="tx_no_memo", amount=0.25)])

    deposit_service.process_active_swaps(db, CONFIG, {"SOL": adapter})

    assert adapter.scans == [ACCOUNT], "one scan, made through scan_shared_accounts()"
    rows = db.execute(
        "SELECT txid, amount, discriminator FROM unattributable_deposits").fetchall()
    assert len(rows) == 1 and rows[0]["txid"] == "tx_no_memo", (
        f"the adapter's own refusal is in SQL, not only in a log; got {[dict(r) for r in rows]}"
    )
    assert rows[0]["discriminator"] is None, (
        "no discriminator by construction: the drop exists because no usable memo was found"
    )


def test_a_settled_transaction_is_not_re_read_through_the_SHARED_scan(db):
    """The 2026-10-01 rate-limit fix, re-pinned at its NEW call site.

    skip_txids() used to be computed inside refresh_swap_from_chain(), once per swap. It is
    now computed once in scan_shared_accounts(), and a skip set that fails to reach the
    adapter is invisible: a re-read transaction looks exactly like a first read, and the
    only symptom is the HTTP 429 that stopped a real deposit being credited.

    MUTATION: drop `skip_txids=` from the shared scan and this fails. It SURVIVED before
    this test existed, because the only coverage of the skip ran through the per-swap
    fallback path -- the branch the live workers no longer take.
    """
    seed_swap(db, "s_new", 9)
    seed_swap(db, "s_old", 8)
    seed_deposit_event(db, "s_old", "tx_settled", 8, confirmations=3)
    adapter = CountingAdapter([event("tx_settled", 8), event("tx_fresh", 9)])

    deposit_service.process_active_swaps(db, CONFIG, {"SOL": adapter})

    assert adapter.scans == [ACCOUNT], "one scan"
    assert "tx_settled" not in adapter.read, (
        f"a settled transaction costs no further getTransaction; read={adapter.read}"
    )
    assert "tx_fresh" in adapter.read, "and the unsettled one is still read, so the skip is not total"


# --- the two workers run this function in two processes -----------------------
#
# workers/reconcile_worker.py:18-20 says so in its own header and a grep confirms it:
# deposit_watcher.py:153 and reconcile_worker.py:92 both call process_active_swaps(), at 15s
# and 60s, in SEPARATE PROCESSES with separate adapter instances and separate SQLite
# connections. Nothing in this change dedupes across them and nothing could: there is no
# shared memory between two processes.
#
# So the question these two tests answer is not "is the scan shared across processes" -- it
# is not -- but "can two processes holding two scan results credit one payment twice".


def test_two_processes_sharing_one_cycle_each_credit_the_deposit_once(db, tmp_path):
    """Both workers, each with its OWN connection and its OWN scan, over one payment.

    This is the shape of the concurrency the operator's audit trail shows: the same two
    status transitions recorded twice, 0.83s apart, by the two workers processing one swap.
    The credit itself must still happen once.

    WHAT MAKES IT ONCE is db.py's UNIQUE(asset, txid, vout) on deposit_events plus the fact
    that both credit writes are ABSOLUTE ASSIGNMENTS rather than increments: `UPDATE swaps
    SET credited_at = ?, actual_input_amount = ?` and `UPDATE deposit_events SET credited_at
    = ? WHERE credited_at IS NULL`. Running them twice writes the same figures twice.
    """
    seed_swap(db, "s_both", 11)
    second = connect_db(str(tmp_path / "t.db"))

    # The deposit_watcher cycle, then the reconcile_worker cycle, each with its own adapter.
    deposit_service.process_active_swaps(
        db, CONFIG, {"SOL": CountingAdapter([event("tx_one", 11, amount=0.01, confirmations=3)])})
    deposit_service.process_active_swaps(
        second, CONFIG, {"SOL": CountingAdapter([event("tx_one", 11, amount=0.01, confirmations=3)])})

    rows = db.execute("SELECT swap_id, txid, vout FROM deposit_events").fetchall()
    assert len(rows) == 1, f"one payment, one row, two processes; got {len(rows)}"
    row = db.execute(
        "SELECT actual_input_amount, status FROM swaps WHERE id = 's_both'").fetchone()
    assert row["actual_input_amount"] == 0.01, (
        f"0.01 arrived once and 0.01 is credited; {row['actual_input_amount']} is a double count"
    )
    assert row["status"] == "payout_pending"


def test_the_unique_constraint_refuses_a_second_row_for_one_payment(db, tmp_path):
    """NAME THE CONSTRAINT AND MAKE IT SPEAK. UNIQUE(asset, txid, vout), db.py.

    upsert_deposit_event() SELECTs that triple before inserting, so the two workers almost
    always take the UPDATE branch. The case that matters is the interleave where BOTH miss
    the SELECT and both reach the INSERT -- and what stops a second row there is not the
    application's check-then-act, which has no lock across it, but the constraint. This
    asserts the constraint itself, by trying the INSERT the losing worker would make.

    The raise is what the workers' `except Exception` turns into a FAILED cycle (visible,
    retried next cycle) rather than into a second credit.
    """
    seed_swap(db, "s_one", 11)
    deposit_service.process_active_swaps(
        db, CONFIG, {"SOL": CountingAdapter([event("tx_one", 11, amount=0.01, confirmations=3)])})
    assert db.execute("SELECT COUNT(*) AS n FROM deposit_events").fetchone()["n"] == 1

    with pytest.raises(sqlite3.IntegrityError) as raised:
        db.execute(
            "INSERT INTO deposit_events (swap_id, asset, txid, vout, address, amount,"
            " confirmations, first_seen_at, last_seen_at)"
            " VALUES ('s_one','SOL','tx_one',11,?,0.01,3,'2026-10-02T00:00:00+00:00',"
            "'2026-10-02T00:00:00+00:00')",
            (ACCOUNT,),
        )
    assert "deposit_events.asset" in str(raised.value), (
        f"the refusal names the constraint that produced it; got {raised.value}"
    )


# --- the decision, called directly with seeded inputs (rule 10) ---------------

def test_shared_scan_targets_names_only_tag_attributed_assets(db):
    """BTC is absent by construction, not by a hardcoded list.

    services/swap_service.TAG_ATTRIBUTED_ASSETS is the authority and this function
    reads it, so a new tag-attributed chain is picked up by growing that set rather
    than by editing this function (rule 11).
    """
    seed_swap(db, "s_sol", 11)
    seed_btc_swap(db, "b_one", BTC_PARTICIPANT)
    swaps = db.execute("SELECT * FROM swaps ORDER BY id").fetchall()

    targets = deposit_service.shared_scan_targets(
        swaps, CONFIG, {"SOL": CountingAdapter([]), "BTC": CountingAdapter([])})

    assert targets == [("SOL", ACCOUNT)], (
        f"only the shared SOL account; the BTC swap's own address is not shared. got {targets}"
    )


def test_shared_scan_targets_is_the_union_of_config_and_every_open_swap(db):
    """Both halves, and the duplicate collapsed. This is where the saving comes from."""
    seed_swap(db, "s_current", 11)
    seed_swap(db, "s_current_too", 12)
    seed_swap(db, "s_old", 13)
    db.execute("UPDATE swaps SET deposit_address = ? WHERE id = 's_old'", (PREVIOUS_ACCOUNT,))
    db.commit()
    swaps = db.execute("SELECT * FROM swaps ORDER BY id").fetchall()

    targets = deposit_service.shared_scan_targets(swaps, CONFIG, {"SOL": CountingAdapter([])})

    assert targets == sorted([("SOL", ACCOUNT), ("SOL", PREVIOUS_ACCOUNT)]), (
        f"three swaps on two accounts are two scans, not three. got {targets}"
    )


def test_shared_scan_targets_is_empty_without_an_adapter_or_an_account(db):
    """Neither is an error, and both must produce no scan rather than a KeyError.

    swap_service refuses to create a swap on an asset whose account variable is
    empty, and reconcile_shared_accounts() already treats a missing adapter as
    nothing to scan. `(none)` is the result here too.
    """
    seed_swap(db, "s_sol", 11)
    swaps = db.execute("SELECT * FROM swaps").fetchall()

    assert deposit_service.shared_scan_targets(swaps, CONFIG, {}) == [], "no adapter, no scan"
    assert deposit_service.shared_scan_targets(
        swaps, {"AMOUNT_TOLERANCE_PCT": 0.01, "SOL_DEPOSIT_ACCOUNT": "  "},
        {"SOL": CountingAdapter([])}) == [("SOL", ACCOUNT)], (
        "an unset account contributes nothing, but the open swap's own address still does"
    )


def test_the_cycle_says_it_scanned_once_and_for_how_many_swaps(db, caplog):
    """Rule 14: the work that stopped happening still has to be visible.

    Four scans printed four adapter lines. One prints one, and an operator watching
    the count fall needs the line to say why -- so the remaining line names the
    account, how many swaps the single result was handed to, and what the per-swap
    shape would have cost.

    MUTATION: delete the logger.info() from scan_shared_accounts() and this fails,
    which is the point: an optimization that makes the log quieter about real work
    is rule 14's defect wearing a performance win.
    """
    seed_swap(db, "s_a", 11)
    seed_swap(db, "s_b", 12)
    seed_swap(db, "s_c", 13)
    adapter = CountingAdapter([])

    # The module's own logger name rather than a literal: this file imports it as
    # swap_terminal.services.deposit_service and the workers import it as
    # services.deposit_service, so a literal would pin one import path (rule 8).
    with caplog.at_level("INFO", logger=deposit_service.logger.name):
        deposit_service.process_active_swaps(db, CONFIG, {"SOL": adapter})

    lines = [r.getMessage() for r in caplog.records if "scanned ONCE" in r.getMessage()]
    assert len(lines) == 1, f"one line per shared account per cycle, not a flood; got {lines}"
    assert ACCOUNT in lines[0], "the account it read, echoed (rule 14)"
    assert "3 active swap(s)" in lines[0], "how many swaps the one result served"
    assert "4 scans" in lines[0], "and what the per-swap shape would have cost"
