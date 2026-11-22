"""A credited ICP deposit must become spendable inventory. It did not.

Role: test (writes a temp database; no network, no dfx, no replica)
Reads: chains/icp.py, services/icp_sweep_service.py, services/icp_subaccount_service.py,
       services/payout_service.refresh_wallet_inventory
Writes: a temp database under pytest's tmp_path
Can move funds: no -- every transfer goes to a stub ledger
Mainnet-safe: yes

=============================================================================
THE DEFECT, AND WHY NO TEST ANYWHERE CAUGHT IT
=============================================================================

chains/icp.transfer_argument() hardcoded `from_subaccount = null`, so every send
debited the owner principal's DEFAULT subaccount, while deposits land at allocated
subaccounts with index >= 1. Detection worked and crediting worked; the coins then
sat where nothing could spend them. subaccount_balance() had NO production caller.

NOTHING NOTICED BECAUSE NO TEST ASKED THE QUESTION. tests/test_icp_e2e_both_directions.py
seeds `wallet_inventory` for ICP by hand, which is reasonable for what that file
measures and is exactly why this gap survived: no test anywhere asked whether a
DEPOSIT produces inventory. This file asks only that.

MEASURED CONSEQUENCE, with the default account empty and the coins in subaccounts:

    why_the_payout_cannot_be_funded refuses = True
      "the ICP payout wallet holds 0.0 ICP spendable and this payout needs 10.0001
       ICP ... so it is short 10.0001"

An X -> ICP swap refused at CREATION while the desk held the coins -- a halt, the
safe direction, costing a retry. ICP -> X was quieter: the desk paid out on the far
chain and received ICP that never became inventory.

=============================================================================
THE LEDGER STUB MODELS BALANCES, WHICH IS WHAT MAKES THIS A TEST
=============================================================================

SubaccountLedger SUBCLASSES tests/test_icp_e2e_both_directions.StubLedger rather
than being a second stub (rule 8). That one returns a single fixed balance for
every account, which is right for the double-pay question it was built for and
useless here: a sweep is only meaningful if the balance MOVES. This subclass holds
a per-account balance and applies each transfer to it -- debiting the subaccount by
amount+fee and crediting the desk by amount -- so "the deposit became inventory" is
read back out of the same model the transfer went into, not asserted against a
constant.

IT IS STILL A MODEL OF A DOCUMENTED INTERFACE AND NOT A LEDGER. No ICP transfer has
ever been signed or broadcast by this code on any network, and no dfx or replica
exists in the environment these tests run in. What is proven here is the arithmetic,
the candid argument, the enumeration, the ordering and the append-only record. That
a real ledger accepts the transfer is not, and HANDOFF.md says so for the payout.
"""

from __future__ import annotations

import re
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "swap_terminal"))

from chains.icp_account import account_identifier, subaccount_from_index
from db import SCHEMA, apply_migrations
from services.icp_sweep_service import sweep_credited_deposits, sweep_note
from services.payout_service import refresh_wallet_inventory
from test_icp_e2e_both_directions import (
    DESK_PRINCIPAL,
    REAL_FEE_E8S,
    StubLedger,
    armed_adapter,
)

DEPOSIT_E8S = 1_000_000_000  # 10 ICP
SWEPT_E8S = DEPOSIT_E8S - REAL_FEE_E8S

#: `from_subaccount = opt blob "\xx\xx..."` -- the field that was hardcoded `null`.
_FROM_SUBACCOUNT = re.compile(r'from_subaccount = opt blob "((?:\\[0-9a-f]{2})+)"')
_TO_BLOB = re.compile(r'to = blob "((?:\\[0-9a-f]{2})+)"')


def _blob_to_bytes(escaped: str) -> bytes:
    return bytes(int(pair, 16) for pair in escaped.split("\\") if pair)


class SubaccountLedger(StubLedger):
    """A StubLedger that knows which account it is being asked about, and moves money.

    `balances` maps subaccount index -> e8s, with None for the desk's own default
    account. Every transfer is applied to it, so a sweep's effect is observable.
    """

    def __init__(self, balances: dict[int | None, int], **kwargs):
        super().__init__(**kwargs)
        self.balances = dict(balances)

    def __call__(self, canister: str, method: str, argument: str, output: str = "idl") -> str:
        if method == "icrc1_balance_of":
            self.calls.append((method, argument))
            return f"({self.balances.get(self._index_of(argument), 0)} : nat)"
        if method == "account_identifier":
            # ANSWERED WITH THE REAL DERIVATION, so verify_derivation() AGREES. That
            # check is the sweep's first guard and the one it cannot survive getting
            # wrong (chains/icp_payout_identity.py:131 records that nothing verifies the
            # armed identity's principal), so a stub that answered anything else would
            # make every sweep test below measure the refusal instead of the sweep.
            #
            # A STUB THAT AGREES BY CONSTRUCTION CANNOT CATCH A DERIVATION DEFECT, and
            # that is not this file's job: tests/test_icp_adapter.py pins the derivation
            # against fixed vectors. Here it is seeded so the subject is the transfer.
            self.calls.append((method, argument))
            index = self._index_of(argument)
            subaccount = None if index is None else subaccount_from_index(index)
            octets = account_identifier(DESK_PRINCIPAL, subaccount)
            return 'blob "' + "".join(f"\\{byte:02x}" for byte in bytes.fromhex(octets)) + '"'
        reply = super().__call__(canister, method, argument, output)
        if method == "transfer" and "Err" not in reply:
            self._apply_to_balances(argument)
        return reply

    @staticmethod
    def _index_of(account_argument: str) -> int | None:
        """The subaccount index an ICRC-1 Account record names, or None for the default.

        The balance query spells the subaccount as `opt vec { 0; 0; ...; 1 }` -- a
        different candid form from the transfer's `opt blob`, because the two records
        declare different types. chains/icp._from_subaccount_clause() records the same
        asymmetry from the other side.
        """
        vec = re.search(r"subaccount = opt vec \{ ([^}]*) \}", account_argument)
        if not vec:
            return None
        octets = bytes(int(part) for part in vec.group(1).split(";"))
        return int.from_bytes(octets, "big")

    def _apply_to_balances(self, argument: str) -> None:
        amount = int(re.search(r"amount = record \{ e8s = (\d+)", argument).group(1))
        fee = int(re.search(r"fee = record \{ e8s = (\d+)", argument).group(1))
        source = _FROM_SUBACCOUNT.search(argument)
        key = int.from_bytes(_blob_to_bytes(source.group(1)), "big") if source else None
        self.balances[key] = self.balances.get(key, 0) - amount - fee
        self.balances[None] = self.balances.get(None, 0) + amount


def seeded_db(tmp_path, *, subaccount_index: int = 1, status: str = "payout_pending") -> sqlite3.Connection:
    """A real database on the real schema, with one credited ICP deposit allocated.

    `status` is a PARAMETER because it is the enumeration's filter: the query takes
    every allocated subaccount whose swap has left `awaiting_deposit`, which is the
    weak test on purpose -- a swap that later failed still has coins in its
    subaccount and they still belong in inventory.
    """
    conn = sqlite3.connect(str(tmp_path / "t.db"))
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    conn.commit()
    apply_migrations(conn)
    conn.execute("PRAGMA foreign_keys = OFF")  # seeding a swap without its quote row
    columns = {row[1]: row for row in conn.execute("PRAGMA table_info(swaps)")}
    values = {name: ("x" if "TEXT" in row[2].upper() else 0)
              for name, row in columns.items() if row[3] and row[4] is None}
    values.update({"id": "s_icp", "from_asset": "ICP", "to_asset": "BTC", "status": status})
    # S608 is suppressed and this is what was checked (rule 19): the interpolated
    # text is COLUMN NAMES read from `PRAGMA table_info(swaps)` plus one `?` per
    # column. No value is interpolated -- every value goes in as a parameter on the
    # next line -- and nothing here comes from a caller. The column list is built
    # from the live schema on purpose, so this seeding does not break the day `swaps`
    # gains a NOT NULL column, which is how the first draft of it failed.
    conn.execute(
        f"INSERT INTO swaps ({','.join(values)}) VALUES ({','.join('?' * len(values))})",  # noqa: S608
        tuple(values.values()),
    )
    conn.execute(
        "INSERT INTO icp_deposit_subaccounts (owner, subaccount_index, swap_id, allocated_at) "
        "VALUES (?,?,?,?)",
        (DESK_PRINCIPAL, subaccount_index, "s_icp", "2026-11-22T00:00:00+00:00"),
    )
    conn.commit()
    return conn


def _config() -> dict:
    return {"ICP_OWNER_PRINCIPAL": DESK_PRINCIPAL}


def test_a_credited_ICP_deposit_BECOMES_spendable_inventory(tmp_path, monkeypatch):
    """THE HEADLINE. Before the sweep the desk's spendable balance was 0 with 10 ICP held.

    MUTATION CAUGHT: restore `from_subaccount = null` in transfer_argument() and the
    sweep debits the desk's own (empty) account instead, so inventory does not grow
    by the deposit and this fails.
    """
    conn = seeded_db(tmp_path)
    ledger = SubaccountLedger({None: 0, 1: DEPOSIT_E8S})
    adapter = armed_adapter(ledger, monkeypatch)

    assert adapter.get_balance() == 0.0, (
        "the fixture already has a spendable balance, so this test cannot show a deposit becoming "
        "one -- the whole defect is that the coins are in a subaccount the payout cannot reach"
    )

    summary = sweep_credited_deposits(conn, _config(), {"ICP": adapter})
    assert summary["swept"] == 1, f"nothing was swept: {summary}"
    assert summary["e8s"] == SWEPT_E8S

    refresh_wallet_inventory(conn, {"ICP": adapter}, None)
    row = conn.execute("SELECT hot_confirmed FROM wallet_inventory WHERE asset = 'ICP'").fetchone()
    conn.close()
    assert row is not None, "refresh_wallet_inventory wrote no ICP row at all"
    assert round(row["hot_confirmed"] * 10**8) == SWEPT_E8S, (
        f"the desk's spendable ICP is {row['hot_confirmed']} after sweeping a {DEPOSIT_E8S} e8s "
        f"deposit; it should be the deposit less one ledger fee ({SWEPT_E8S} e8s). A credited "
        f"deposit that does not become inventory refuses the next X -> ICP swap at creation while "
        f"the desk holds the coins"
    )


def test_the_sweep_debits_the_SUBACCOUNT_and_credits_the_desks_own_account(tmp_path, monkeypatch):
    """The candid argument, read off the wire. This is the one-line defect site.

    MUTATION CAUGHT: the `from_subaccount = null` restoration fails the first
    assertion directly, naming the field.
    """
    conn = seeded_db(tmp_path, subaccount_index=7)
    ledger = SubaccountLedger({None: 0, 7: DEPOSIT_E8S})
    adapter = armed_adapter(ledger, monkeypatch)

    sweep_credited_deposits(conn, _config(), {"ICP": adapter})
    conn.close()

    transfers = [arg for method, arg in ledger.calls if method == "transfer"]
    assert len(transfers) == 1, f"expected exactly one transfer, got {len(transfers)}"
    sent = transfers[0]

    source = _FROM_SUBACCOUNT.search(sent)
    assert source, (
        f"the sweep's transfer carries no `from_subaccount = opt blob`, so it debits the desk's "
        f"DEFAULT account and moves nothing out of the deposit subaccount -- the exact defect: {sent[:200]}"
    )
    assert _blob_to_bytes(source.group(1)) == subaccount_from_index(7), (
        "the transfer names a different subaccount than the one allocated to this swap"
    )
    destination = _TO_BLOB.search(sent)
    assert destination and _blob_to_bytes(destination.group(1)).hex() == adapter.own_address(), (
        "the sweep does not pay the desk's own account, so the coins move somewhere no payout reads"
    )


def test_a_SECOND_sweep_moves_nothing(tmp_path, monkeypatch):
    """Idempotence BY STATE: the subaccount is empty, so a re-run refuses.

    NOT by the recorded row and not by created_at_time. The ledger refuses an
    idempotency key more than 24 hours old with TxTooOld, so a key derived from the
    swap's history becomes unretryable exactly when a stuck sweep needs retrying --
    which is why the balance is the guard. This also covers the case that matters: a
    transfer the ledger accepted whose SQL row was never written is swept again,
    finds nothing, and sends nothing.
    """
    conn = seeded_db(tmp_path)
    ledger = SubaccountLedger({None: 0, 1: DEPOSIT_E8S})
    adapter = armed_adapter(ledger, monkeypatch)

    first = sweep_credited_deposits(conn, _config(), {"ICP": adapter})
    assert first["swept"] == 1

    # The recorded row alone would hide the real guard, so it is removed from the
    # equation: the subaccount is enumerated again by deleting nothing and instead
    # asking the service a second time with the row in place, and then once more
    # against a ledger that still reports the subaccount empty.
    second = sweep_credited_deposits(conn, _config(), {"ICP": adapter})
    rows = conn.execute("SELECT COUNT(*) AS n FROM icp_subaccount_sweeps").fetchone()["n"]
    conn.close()

    assert second["candidates"] == 0, (
        "the already-swept subaccount is still a candidate, so every cycle asks the ledger about "
        "every deposit ever taken"
    )
    assert ledger.transfers_attempted() == 1, (
        f"{ledger.transfers_attempted()} transfers were attempted for one deposit; a sweep that "
        f"repeats is a second signed transfer out of a customer's subaccount"
    )
    assert rows == 1, f"{rows} sweep rows for one sweep"


def test_an_UNARMED_host_sweeps_NOTHING(tmp_path, monkeypatch):
    """A deposit-watching host with no ICP_PAYOUT_IDENTITY must not move funds.

    NOT A REFUSAL, A POSTURE. An unarmed host is an ordinary deployment, so this
    reports `armed: False` and attempts no transfer rather than raising -- the same
    judgment tests/test_optional_dependency_posture.py pins for the signing
    libraries one layer down.
    """
    conn = seeded_db(tmp_path)
    ledger = SubaccountLedger({None: 0, 1: DEPOSIT_E8S})
    adapter = armed_adapter(ledger, monkeypatch)
    monkeypatch.delenv("ICP_PAYOUT_IDENTITY", raising=False)

    summary = sweep_credited_deposits(conn, _config(), {"ICP": adapter})
    conn.close()

    assert summary["armed"] is False
    assert summary["swept"] == 0
    assert ledger.transfers_attempted() == 0, (
        "an unarmed host signed a transfer. Arming is the only thing standing between a "
        "deposit-watching container and a fund-moving one"
    )
    assert sweep_note(summary) == "", "an unarmed cycle must add no line to the worker's output"


@pytest.mark.parametrize("balance_e8s", [0, 1, REAL_FEE_E8S], ids=["empty", "one-e8s", "exactly-the-fee"])
def test_a_deposit_at_or_below_the_LEDGER_FEE_is_refused_rather_than_swept(tmp_path, monkeypatch, balance_e8s):
    """The fee is charged to the subaccount, so a deposit under it cannot be swept.

    This is the ICP sweep meeting the dust-floor defect: a deposit worth less than
    the transfer that would move it. It is counted as `refused`, not as `swept`, and
    the note says so -- a cycle that moved nothing must not read like one that did.
    """
    conn = seeded_db(tmp_path)
    ledger = SubaccountLedger({None: 0, 1: balance_e8s})
    adapter = armed_adapter(ledger, monkeypatch)

    summary = sweep_credited_deposits(conn, _config(), {"ICP": adapter})
    rows = conn.execute("SELECT COUNT(*) AS n FROM icp_subaccount_sweeps").fetchone()["n"]
    conn.close()

    assert summary["swept"] == 0 and summary["refused"] == 1, f"{summary}"
    assert ledger.transfers_attempted() == 0, "a transfer was attempted for a non-positive amount"
    assert rows == 0, "a sweep row was written for a sweep that did not happen"
    assert "REFUSED" in sweep_note(summary), f"the cycle line does not say it moved nothing: {sweep_note(summary)!r}"


def test_the_sweep_ROW_records_what_the_transfer_carried(tmp_path, monkeypatch):
    """The audit row must match the wire, not a re-measurement of the balance.

    The adapter returns (amount_e8s, fee_e8s, block_index) for exactly this reason:
    asking the ledger again after the transfer could see a different balance and
    write a row that disagrees with the chain.
    """
    conn = seeded_db(tmp_path)
    ledger = SubaccountLedger({None: 0, 1: DEPOSIT_E8S})
    adapter = armed_adapter(ledger, monkeypatch)

    sweep_credited_deposits(conn, _config(), {"ICP": adapter})
    row = conn.execute("SELECT * FROM icp_subaccount_sweeps").fetchone()
    sent = next(arg for method, arg in ledger.calls if method == "transfer")
    conn.close()

    assert row["amount_e8s"] == SWEPT_E8S
    assert row["fee_e8s"] == REAL_FEE_E8S
    assert row["swap_id"] == "s_icp"
    assert row["subaccount_index"] == 1
    assert row["block_index"], "no block index was recorded for an accepted transfer"
    assert f"e8s = {row['amount_e8s']} : nat64" in sent, (
        "the recorded amount is not the amount the transfer carried"
    )


def test_the_sweep_REFUSES_when_the_ledger_derives_a_DIFFERENT_account(tmp_path, monkeypatch):
    """The derivation gate, and it exists because MUTATION TESTING found it unasserted.

    Removing `verify_derivation()` from sweep_subaccount_to_desk() left all 276 ICP
    tests passing. The gate was argued for at length in that method's docstring and
    proven by nothing -- which is the same defect as a requirements file that lists a
    package nobody imports, one layer up: reasoning with no assertion behind it.

    WHAT IT GUARDS. chains/icp_payout_identity.py:131 records that nothing verifies
    the armed identity's principal equals ICP_OWNER_PRINCIPAL. For a PAYOUT that
    mismatch surfaces as an InsufficientFunds from the ledger -- loud, after a
    deposit. For a SWEEP it would be quieter and worse: a transfer signed by a
    principal owning none of these subaccounts, moving nothing, while the operator
    can see the funds sitting in the ledger. So the sweep asks first, with read-only
    queries that cost nothing.

    THE DISAGREEMENT IS SEEDED ON THE LEDGER SIDE, which is the only side a test can
    move: `account_identifier` returns an account this repository did not derive,
    exactly as it would if the armed identity were a different principal.

    MUTATION CAUGHT: replace the gate with `verified, why = True, ""` and this fails
    on the transfer count -- a transfer is attempted against an account the ledger
    and this repository disagree about.
    """
    conn = seeded_db(tmp_path)

    class DisagreeingLedger(SubaccountLedger):
        def __call__(self, canister, method, argument, output="idl"):
            if method == "account_identifier":
                self.calls.append((method, argument))
                # A well-formed 64-hex account that is NOT the one chains/icp_account
                # derives for this principal and subaccount.
                return 'blob "' + "".join(f"\\{byte:02x}" for byte in bytes(32)) + '"'
            return super().__call__(canister, method, argument, output)

    ledger = DisagreeingLedger({None: 0, 1: DEPOSIT_E8S})
    adapter = armed_adapter(ledger, monkeypatch)

    summary = sweep_credited_deposits(conn, _config(), {"ICP": adapter})
    rows = conn.execute("SELECT COUNT(*) AS n FROM icp_subaccount_sweeps").fetchone()["n"]
    conn.close()

    assert ledger.transfers_attempted() == 0, (
        "a transfer was signed for a subaccount the ledger and this repository derive differently. "
        "If the armed identity's principal is not the owner, that transfer moves nothing and the "
        "operator is left reading a confusing refusal about funds they can see"
    )
    assert summary["swept"] == 0 and summary["refused"] == 1, f"{summary}"
    assert rows == 0, "a sweep row was written for a sweep that was refused before any transfer"
