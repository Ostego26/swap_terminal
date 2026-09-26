"""Correcting a swap whose payout was delivered but recorded as failed.

Role: test (seeded database and a stub wallet; opens no socket, signs nothing)
Reads: settle_payout.py
Writes: a temporary database per test
Can move funds: no
Mainnet-safe: yes

THE RECORD THIS EXISTS FOR, 2026-09-26. The first real XRP -> GRC payout delivered
55.52645238 GRC and was written down as failed with txid (none), because the txid was
stored after the wallet's unlock context exited and the context's re-lock raised.
services/payout_service.py no longer does that; the swap it already did it to still
needed correcting, and nothing in the tree could.

EVERY TEST GOES THROUGH THE REAL main(), because the refusals ARE the safety and
each one is a branch. The wallet is a stub that answers listtransactions and
gettransaction the way Gridcoin does -- including the `receive` row that appears
beside every `send` to an address the wallet owns, which is the operator's case and
the one that would double-count if the matcher keyed on the wrong category.
"""

import sqlite3
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "swap_terminal"))

from db import SCHEMA, connect_db  # noqa: E402

import settle_payout  # noqa: E402

ADDRESS = "mmr6ATb3tmV9wFuU9CuHd5gBeHHXLR5wke"
AMOUNT = 55.52645238
TXID = "3e09dc9cfd7a61da0000000000000000000000000000000000000000000000ff"


def seed(db_path, *, status="failed", txid=None, payout_rows=1, reason="the re-lock failed"):
    conn = connect_db(str(db_path))
    conn.executescript(SCHEMA)
    conn.execute(
        "INSERT INTO quotes (id, from_asset, to_asset, input_amount, quoted_rate, fee_bps,"
        " network_fee_reserve, output_amount_estimate, expires_at, created_at)"
        " VALUES ('q_s','XRP','GRC',1.0,56.38,150,0.01,?,'2999-01-01T00:00:00+00:00','2026-09-26T00:00:00+00:00')",
        (AMOUNT,),
    )
    conn.execute(
        "INSERT INTO swaps (id, quote_id, from_asset, to_asset, deposit_address, deposit_tag,"
        " payout_address, expected_input_amount, actual_input_amount, quoted_rate, fee_bps,"
        " network_fee_reserve, output_amount_estimate, status, min_confirmations, deposit_txid,"
        " payout_txid, created_at, updated_at, credited_at, completed_at, expires_at, failed_reason)"
        " VALUES ('s_x','q_s','XRP','GRC','rDeposit',4,?,1.0,1.0,56.38,150,0.01,?,?,1,'xrptxid',"
        " ?, '2026-09-26T00:00:00+00:00','2026-09-26T00:01:00+00:00','2026-09-26T00:00:30+00:00',"
        " NULL,'2999-01-01T00:00:00+00:00',?)",
        (ADDRESS, AMOUNT, status, txid, reason),
    )
    for _ in range(payout_rows):
        conn.execute(
            "INSERT INTO payouts (swap_id, asset, destination_address, amount, txid, status, created_at)"
            " VALUES ('s_x','GRC',?,?,NULL,'created','2026-09-26T00:01:00+00:00')",
            (ADDRESS, AMOUNT),
        )
    # The reservation the failure path never released.
    conn.execute(
        "INSERT INTO wallet_inventory (asset, hot_confirmed, hot_reserved, hot_available, updated_at)"
        " VALUES ('GRC', 4190.0, ?, ?, '2026-09-26T00:01:00+00:00')",
        (AMOUNT, 4190.0 - AMOUNT),
    )
    conn.commit()
    conn.close()


class WalletStub:
    """listtransactions and gettransaction, shaped the way Gridcoin answers them."""

    can_spend = True
    payout_refusal = ""

    def __init__(self, rows=None, confirmations=7):
        if rows is None:
            # BOTH SIDES of one transaction, which is what the operator saw: their
            # payout address is their own, so the wallet reports the send AND the
            # receive for the same txid. A matcher keyed on anything but `send` counts
            # this payment twice.
            rows = [
                {"category": "send", "address": ADDRESS, "amount": -AMOUNT, "txid": TXID, "confirmations": confirmations},
                {"category": "receive", "address": ADDRESS, "amount": AMOUNT, "txid": TXID, "confirmations": confirmations},
            ]
        self.rows = rows
        self.confirmations = confirmations
        self.calls = []

    def call(self, method, *params):
        self.calls.append(method)
        if method == "listtransactions":
            return self.rows
        if method == "gettransaction":
            return {"txid": params[0], "confirmations": self.confirmations}
        raise AssertionError(f"unexpected RPC {method}")


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "settle.db"


def run(monkeypatch, db_path, argv, wallet=None):
    """main() with the GRC adapter stubbed and no socket opened."""
    stub = wallet if wallet is not None else WalletStub()
    monkeypatch.setattr(settle_payout, "build_adapters", lambda _rpc: {"GRC": stub})
    monkeypatch.setattr(settle_payout, "unconfigured_chains", lambda _a, *_assets: [])
    return settle_payout.main([*argv, "--db", str(db_path)]), stub


def read(db_path, statement):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(statement).fetchone()
    finally:
        conn.close()


# --- the dry run is the default -----------------------------------------------

def test_the_dry_run_changes_nothing(monkeypatch, db_path, capsys):
    seed(db_path)

    code, _ = run(monkeypatch, db_path, ["--swap", "s_x"])

    assert code == 0
    row = read(db_path, "SELECT status, payout_txid FROM swaps WHERE id = 's_x'")
    assert row["status"] == "failed", "a dry run must not change the status"
    assert row["payout_txid"] is None
    out = capsys.readouterr().out
    assert "DRY RUN" in out
    assert TXID in out, "it must still say which payment it found"
    assert "--apply" in out


def test_apply_records_the_txid_and_completes_the_swap(monkeypatch, db_path):
    seed(db_path)

    code, _ = run(monkeypatch, db_path, ["--swap", "s_x", "--apply"])

    assert code == 0
    swap = read(db_path, "SELECT * FROM swaps WHERE id = 's_x'")
    payout = read(db_path, "SELECT * FROM payouts WHERE swap_id = 's_x'")
    assert swap["status"] == "completed"
    assert swap["payout_txid"] == TXID
    assert swap["completed_at"], "a completed swap needs its timestamp"
    assert payout["status"] == "broadcast"
    assert payout["txid"] == TXID
    assert payout["sent_at"]


def test_the_reservation_the_failure_left_behind_is_released(monkeypatch, db_path):
    """The failure path never released it, so a correction that skipped this would
    leave the hot wallet permanently short on paper."""
    seed(db_path)
    before = read(db_path, "SELECT hot_reserved, hot_available FROM wallet_inventory WHERE asset = 'GRC'")
    assert before["hot_reserved"] == pytest.approx(AMOUNT), "the seed must model the stuck reservation"

    run(monkeypatch, db_path, ["--swap", "s_x", "--apply"])

    after = read(db_path, "SELECT hot_reserved FROM wallet_inventory WHERE asset = 'GRC'")
    assert after["hot_reserved"] == pytest.approx(0.0)


def test_the_previous_reason_is_kept_rather_than_blanked(monkeypatch, db_path):
    """A swap reading `completed` with no trace of its time as `failed` loses the one
    thing a reader of the row will ask."""
    seed(db_path, reason="the re-lock failed and ate the txid")

    run(monkeypatch, db_path, ["--swap", "s_x", "--apply"])

    swap = read(db_path, "SELECT failed_reason FROM swaps WHERE id = 's_x'")
    assert "CORRECTED by settle_payout.py" in swap["failed_reason"]
    assert "the re-lock failed and ate the txid" in swap["failed_reason"]
    assert TXID in swap["failed_reason"]


def test_the_audit_row_says_failed_to_completed_not_paying_to_completed(monkeypatch, db_path):
    """MUTATION: drop old_status='failed'. The trail then claims a transition that
    never happened, which is the only record of why this swap changed."""
    seed(db_path)

    run(monkeypatch, db_path, ["--swap", "s_x", "--apply"])

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT old_status, new_status FROM swap_audit_log WHERE swap_id = 's_x'").fetchall()
    conn.close()
    transitions = [(row["old_status"], row["new_status"]) for row in rows]
    assert ("failed", "completed") in transitions, transitions
    assert ("paying", "completed") not in transitions


# --- the refusals, which are the safety ----------------------------------------

def test_a_wallet_with_no_matching_send_refuses_and_leaves_failed_standing(monkeypatch, db_path):
    """THE MOST IMPORTANT REFUSAL. No payment means `failed` is CORRECT.

    A tool that marked a swap completed without finding the payment would turn a
    truthful record into a false one, which is worse than the bug it fixes.
    """
    seed(db_path)
    empty = WalletStub(rows=[])

    with pytest.raises(SystemExit) as caught:
        run(monkeypatch, db_path, ["--swap", "s_x", "--apply"], wallet=empty)

    assert "never broadcast" in str(caught.value)
    assert "must stand" in str(caught.value)
    row = read(db_path, "SELECT status, payout_txid FROM swaps WHERE id = 's_x'")
    assert row["status"] == "failed"
    assert row["payout_txid"] is None


def test_only_the_send_side_counts_not_the_receive(monkeypatch, db_path):
    """The operator's payout address is their OWN (ismine = True), so the wallet
    reports both a send and a receive for one txid. A receive-only wallet -- money
    that arrived from somewhere else for the same amount -- is not this payout."""
    seed(db_path)
    receive_only = WalletStub(rows=[
        {"category": "receive", "address": ADDRESS, "amount": AMOUNT, "txid": TXID, "confirmations": 9},
    ])

    with pytest.raises(SystemExit, match="never broadcast"):
        run(monkeypatch, db_path, ["--swap", "s_x", "--apply"], wallet=receive_only)


def test_a_swap_that_already_has_a_txid_is_refused(monkeypatch, db_path):
    """Overwriting a recorded txid would destroy the only link to a real payment."""
    seed(db_path, status="completed", txid="already-recorded")

    with pytest.raises(SystemExit, match="already records payout_txid"):
        run(monkeypatch, db_path, ["--swap", "s_x", "--apply"])


def test_a_swap_that_is_not_failed_is_refused(monkeypatch, db_path):
    seed(db_path, status="payout_pending")

    with pytest.raises(SystemExit, match="not 'failed'"):
        run(monkeypatch, db_path, ["--swap", "s_x", "--apply"])


def test_an_unknown_swap_is_refused(monkeypatch, db_path):
    seed(db_path)

    with pytest.raises(SystemExit, match="no swap s_nope"):
        run(monkeypatch, db_path, ["--swap", "s_nope", "--apply"])


def test_two_payout_rows_without_a_txid_are_refused(monkeypatch, db_path):
    """Which row a payment belongs to is not knowable from here."""
    seed(db_path, payout_rows=2)

    with pytest.raises(SystemExit, match="payout row"):
        run(monkeypatch, db_path, ["--swap", "s_x", "--apply"])


def test_two_matching_sends_refuse_and_ask_for_a_txid(monkeypatch, db_path):
    seed(db_path)
    second = "aa" * 32
    ambiguous = WalletStub(rows=[
        {"category": "send", "address": ADDRESS, "amount": -AMOUNT, "txid": TXID, "confirmations": 7},
        {"category": "send", "address": ADDRESS, "amount": -AMOUNT, "txid": second, "confirmations": 3},
    ])

    with pytest.raises(SystemExit) as caught:
        run(monkeypatch, db_path, ["--swap", "s_x", "--apply"], wallet=ambiguous)

    assert "--txid" in str(caught.value)
    assert TXID in str(caught.value) and second in str(caught.value), "list the candidates"


def test_a_named_txid_the_wallet_does_not_confirm_is_refused(monkeypatch, db_path):
    """--txid is a disambiguator, not the evidence. The wallet is the evidence."""
    seed(db_path)

    with pytest.raises(SystemExit, match="not among the wallet transactions"):
        run(monkeypatch, db_path, ["--swap", "s_x", "--txid", "bb" * 32, "--apply"])


def test_a_named_txid_that_matches_is_used(monkeypatch, db_path):
    """So --txid cannot pass by refusing everything."""
    seed(db_path)
    second = "aa" * 32
    ambiguous = WalletStub(rows=[
        {"category": "send", "address": ADDRESS, "amount": -AMOUNT, "txid": TXID, "confirmations": 7},
        {"category": "send", "address": ADDRESS, "amount": -AMOUNT, "txid": second, "confirmations": 3},
    ])

    code, _ = run(monkeypatch, db_path, ["--swap", "s_x", "--txid", second, "--apply"], wallet=ambiguous)

    assert code == 0
    assert read(db_path, "SELECT payout_txid FROM swaps WHERE id = 's_x'")["payout_txid"] == second


# --- the amount comparison ------------------------------------------------------

def test_a_different_amount_is_not_this_payout(monkeypatch, db_path):
    """The amount is part of the identity. A send of the wrong size to the right
    address is some other payment."""
    seed(db_path)
    wrong = WalletStub(rows=[
        {"category": "send", "address": ADDRESS, "amount": -1.0, "txid": TXID, "confirmations": 7},
    ])

    with pytest.raises(SystemExit, match="never broadcast"):
        run(monkeypatch, db_path, ["--swap", "s_x", "--apply"], wallet=wrong)


def test_the_float_round_trip_still_matches():
    """The database holds REAL and the wallet reports a decimal string, so the two
    spellings of one amount are not equal as floats. Compared as Decimal within one
    satoshi, which is why this is a tolerance and not ==."""
    rows = [{"category": "send", "address": ADDRESS, "amount": "-55.52645238", "txid": TXID}]

    assert settle_payout.matching_wallet_sends(rows, ADDRESS, 55.52645238) == rows


def test_an_unparseable_amount_can_never_become_a_match():
    """The one failure that would matter in the matcher: a row it cannot read must be
    'not this payment', never 'close enough'."""
    rows = [{"category": "send", "address": ADDRESS, "amount": "not-a-number", "txid": TXID}]

    assert settle_payout.matching_wallet_sends(rows, ADDRESS, 55.52645238) == []
    assert settle_payout.matching_wallet_sends([{"category": "send", "address": ADDRESS, "amount": None}], ADDRESS, 1.0) == []
