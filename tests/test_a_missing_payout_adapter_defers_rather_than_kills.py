"""A worker with no adapter for a swap's destination defers it; it does not mark it failed.

Role: tests (services/payout_service.defer_for_missing_adapter())
Reads: a throwaway SQLite database under tmp_path; one stub adapter that opens no socket
Writes: nothing outside tmp_path
Can move funds: no -- the stub's send_to_address() raises if it is ever called, which is
       itself one of the assertions
Mainnet-safe: yes

=============================================================================
A CREDITED SWAP WAS KILLED WITH failed_reason "'GRC'"
=============================================================================

`adapter = adapters[destination_asset]` sat INSIDE the try in
process_pending_payouts(), whose handler writes str(exc) into swaps.failed_reason and
sets the swap to 'failed'. str(KeyError('GRC')) is "'GRC'" and nothing else.

MEASURED, before the fix: one payout_pending BTC->GRC swap seeded into a throwaway
database, the real process_pending_payouts() called with adapters={'BTC': ...} and no
GRC entry, produced

    status        = failed
    failed_reason = "'GRC'"
    payouts       = asset=GRC amount=1000.0 status=failed txid=None
    audit         = paying -> failed : Payout failed: 'GRC'

after the customer's deposit was already irreversible.

=============================================================================
WHY THE FIX IS A DEFERRAL AND NOT A BETTER ERROR MESSAGE
=============================================================================

A missing adapter is a configuration fault in THAT PROCESS, not a fact about the swap.
workers/payout_worker.py builds adapters once from its own environment, and the web
process that created the swap is a different process with a different environment --
on the host, a different shell. So the next worker start, with the variable set, pays
this swap correctly. Marking it failed throws that away for a reason that will not be
true in five minutes, and nothing in the tree re-queues a failed swap.

THE TREE ALREADY FIXED THIS ONCE, IN THE OTHER DIRECTION. On 2026-09-26 the operator's
browser produced "No swap was created: GRC" because the SERVER process lacked
GRC_RPC_PORT while the workers had it; services/swap_service.py gained
unconfigured_chains + why_unconfigured for it. create_swap got a gate and the payout
worker did not -- the same defect, one process over (rule 8), which is why the fix
calls chains/registry.why_unconfigured() rather than composing its own sentence.

THIS IS ALSO THE SHAPE OF s_0dc53d06ab3968fb, the swap the operator asked to have
rescued on 2026-10-10. Whether that particular swap died by this exact path is not
asserted here -- its failure is in the operator's database, not in this suite (rule
17) -- but services/payout_rescue.py exists because a payout that failed BEFORE
signing is recoverable, and a missing adapter is the purest case of one.
"""

from __future__ import annotations

import pytest
import valid_addresses
from config import Config
from db import SCHEMA, apply_migrations, connect_db
from services import payout_service

CONFIG = {name: getattr(Config, name) for name in dir(Config) if name.isupper()}

#: A GRC address that PASSES the address authority, which is load-bearing. The first
#: version of this test used the literal "SgrcPayoutAddress" and the swap was refused
#: by refuse_payout_before_sending() before the adapter was ever looked up -- so it
#: measured the address guard and reported it as the adapter guard. tests/
#: valid_addresses.py builds a real base58check testnet address for exactly this.
GRC_PAYOUT = valid_addresses.GRC_PAYOUT

#: A payout chain that is NOT in WALLET_UNLOCK_ASSETS, for the send path. LTC_PARTICIPANT
#: is a tltc bech32 address the address authority accepts; the name is the fixture's
#: own, from its HTLC origins, and what matters here is only that it is a valid LTC
#: testnet address.
LTC_PAYOUT = valid_addresses.LTC_PARTICIPANT


class BitcoinOnlyAdapter:
    """The only adapter this worker has. Sending is an assertion failure, not a stub.

    A send that happened would be a real broadcast on a real chain in production, so
    "nothing was sent" is asserted by making the call impossible rather than by
    counting afterwards.
    """

    asset = "BTC"

    def send_to_address(self, *_args, **_kwargs):
        raise AssertionError("a payout was broadcast for a chain this worker cannot pay")


@pytest.fixture
def conn(tmp_path):
    connection = connect_db(str(tmp_path / "payout.db"), create=True)
    connection.executescript(SCHEMA)
    apply_migrations(connection)
    connection.execute(
        "INSERT INTO quotes (id, from_asset, to_asset, input_amount, quoted_rate, fee_bps,"
        " network_fee_reserve, output_amount_estimate, expires_at, created_at)"
        " VALUES ('q','BTC','GRC',0.0001,9868961.0,150,0.001,1000.0,"
        "'2999-01-01T00:00:00+00:00','2026-10-10T00:00:00+00:00')"
    )
    connection.execute(
        "INSERT INTO swaps (id, quote_id, from_asset, to_asset, deposit_address, payout_address,"
        " expected_input_amount, actual_input_amount, quoted_rate, fee_bps, network_fee_reserve,"
        " output_amount_estimate, status, min_confirmations, expires_at, created_at, updated_at,"
        " credited_at) VALUES ('s_credited','q','BTC','GRC','bcrt1qdeposit',?,0.0001,0.0001,"
        "9868961.0,150,0.001,1000.0,'payout_pending',2,'2999-01-01T00:00:00+00:00',"
        "'2026-10-10T00:00:00+00:00','2026-10-10T00:00:00+00:00','2026-10-10T00:00:00+00:00')",
        (GRC_PAYOUT,),
    )
    connection.commit()
    return connection


@pytest.fixture
def ltc_conn(tmp_path):
    """The same credited swap, paying out in LTC -- a chain that needs no wallet unlock.

    SEPARATE FROM `conn` rather than parameterized, because the two fixtures exist for
    opposite reasons: `conn` pays GRC so the locked-wallet arm is reachable, and this one
    pays LTC so the send arm is. A single parameterized fixture would make each test
    silently exercise whichever it happened to get.
    """
    connection = connect_db(str(tmp_path / "payout_ltc.db"), create=True)
    connection.executescript(SCHEMA)
    apply_migrations(connection)
    connection.execute(
        "INSERT INTO quotes (id, from_asset, to_asset, input_amount, quoted_rate, fee_bps,"
        " network_fee_reserve, output_amount_estimate, expires_at, created_at)"
        " VALUES ('q','BTC','LTC',0.0001,100.0,150,0.001,0.01,"
        "'2999-01-01T00:00:00+00:00','2026-10-10T00:00:00+00:00')"
    )
    connection.execute(
        "INSERT INTO swaps (id, quote_id, from_asset, to_asset, deposit_address, payout_address,"
        " expected_input_amount, actual_input_amount, quoted_rate, fee_bps, network_fee_reserve,"
        " output_amount_estimate, status, min_confirmations, expires_at, created_at, updated_at,"
        " credited_at) VALUES ('s_credited','q','BTC','LTC','bcrt1qdeposit',?,0.0001,0.0001,"
        "100.0,150,0.001,0.01,'payout_pending',2,'2999-01-01T00:00:00+00:00',"
        "'2026-10-10T00:00:00+00:00','2026-10-10T00:00:00+00:00','2026-10-10T00:00:00+00:00')",
        (LTC_PAYOUT,),
    )
    connection.commit()
    return connection


def swap_row(conn):
    return conn.execute("SELECT * FROM swaps WHERE id = 's_credited'").fetchone()


def run(conn):
    return payout_service.process_pending_payouts(conn, CONFIG, {"BTC": BitcoinOnlyAdapter()})


def test_the_swap_is_left_payable_rather_than_marked_failed(conn):
    """THE REGRESSION TEST. MUTATION: move the lookup back inside the try.

    payout_pending is the status that gets picked up again. 'failed' and 'paying' both
    strand the swap -- nothing in the tree re-queues either -- so the assertion is on
    the exact status and not merely on "not failed".
    """
    assert run(conn) == [], "a payout was reported completed for a chain with no adapter"

    row = swap_row(conn)
    assert row["status"] == "payout_pending", (
        f"a credited swap was left in {row['status']!r}; only payout_pending is picked up again"
    )


def test_no_failure_reason_is_written_because_the_swap_did_not_fail(conn):
    """failed_reason "'GRC'" was the whole symptom, and an empty one is the fix.

    show_swap.py and /admin both read failed_reason as a post-mortem, so a reason on a
    pending swap is a lie about a swap that is still going to be paid. The old value was
    also useless on its own terms: str(KeyError('GRC')) is the asset name in quotes and
    says nothing about what to do.
    """
    run(conn)
    row = swap_row(conn)

    assert row["failed_reason"] is None, (
        f"failed_reason = {row['failed_reason']!r} on a swap that has not failed"
    )
    assert row["completed_at"] is None


def test_nothing_is_reserved_and_no_payout_row_is_written(conn):
    """It returns BEFORE reserve_inventory() and before the INSERT, deliberately.

    The old path wrote a payouts row at status='failed' and reserved inventory for a send
    that never happened. The row is the worse of the two: idx_payouts_one_live_per_swap
    treats 'failed' as not-live, so a retry inserts a SECOND row -- correct for a refusal
    and wrong for anything that may have been relayed -- and the reservation has to be
    unwound by hand.
    """
    run(conn)

    assert conn.execute("SELECT COUNT(*) AS n FROM payouts").fetchone()["n"] == 0
    reserved = conn.execute(
        "SELECT COALESCE(SUM(amount), 0) AS held FROM inventory_reservations"
    ).fetchone()["held"] if _has_table(conn, "inventory_reservations") else 0
    assert reserved == 0, f"{reserved} was reserved for a payout that cannot be sent"


def _has_table(conn, name: str) -> bool:
    """Whether the schema carries `name`.

    GUARDED RATHER THAN ASSUMED, because the reservation mechanism's table name is not
    this test's subject: if it is renamed, this test should keep asserting the two facts
    it is actually about (no payout row, nothing sent) rather than failing on a lookup.
    """
    return bool(conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
    ).fetchone())


def test_the_reason_is_in_the_swaps_own_audit_trail(conn):
    """Rule 14: a worker log the operator is not reading at the time is not a report.

    The deferral has to be visible where they look at a stuck swap -- /admin and
    show_swap.py both render swap_audit_log -- and it has to name the VARIABLE, because
    that is the thing they have to change. chains/registry.why_unconfigured() supplies
    the sentence so there is one copy of it (rule 8).
    """
    run(conn)
    messages = [
        row["message"]
        for row in conn.execute(
            "SELECT message FROM swap_audit_log WHERE swap_id = 's_credited' ORDER BY id"
        ).fetchall()
    ]

    deferrals = [text for text in messages if "deferred" in text.lower()]
    assert deferrals, f"nothing in the audit trail says why the payout did not happen: {messages}"
    note = deferrals[0]
    assert "GRC" in note
    assert "GRC_RPC_PORT" in note, "the variable the operator has to set is not named"
    assert "failed" not in note.lower(), "a deferral must not describe itself as a failure"


def test_a_worker_that_CAN_pay_is_unaffected(ltc_conn):
    """The guard must not become a refusal for a chain that is configured.

    MUTATION: invert the membership test. This is the test that catches it -- without it,
    `if destination_asset in adapters` would defer every payout and every other test in
    this file would still pass.

    IT PAYS LTC AND NOT GRC, and the first version of this test did use GRC and FAILED --
    with `assert sent, "a configured chain's payout was deferred"` and a captured log
    reading `payout FAILED ... GRIDCOIN_WALLET_PASSPHRASE is not set ... <- the swap is
    now 'failed'`. GRC is in chains/gridcoin_wallet_lock.WALLET_UNLOCK_ASSETS, so paying
    it needs a passphrase this suite does not set and must never set. The test was wrong
    about its own fixture -- AND the code was wrong about the swap, which is how the
    locked-wallet deferral below came to be written. The fixture is now a chain that
    needs no unlock, so this test measures the adapter guard and nothing else.
    """
    sent = []

    class LitecoinAdapter:
        asset = "LTC"

        def send_to_address(self, address, amount, **_kwargs):
            sent.append((address, amount))
            return "ltc_txid_0001"

    completed = payout_service.process_pending_payouts(
        ltc_conn, CONFIG, {"BTC": BitcoinOnlyAdapter(), "LTC": LitecoinAdapter()}
    )

    assert sent, "a configured chain's payout was deferred"
    assert sent[0][0] == LTC_PAYOUT
    assert completed, "the payout was sent but not reported completed"
    assert ltc_conn.execute(
        "SELECT status FROM swaps WHERE id = 's_credited'"
    ).fetchone()["status"] == "completed"


# =============================================================================
# THE SAME DEFECT ONE CASE OVER: AN UNSET PASSPHRASE ALSO KILLED THE SWAP
# =============================================================================
#
# PayoutUnlockUnavailable has its own type, and its docstring says why: "categorically
# different from a send that FAILED: no transaction was created, nothing reached any
# daemon, and the fix is an environment variable rather than an investigation."
# payout_unlock_context()'s says "the reason is named and nothing is claimed."
#
# NOTHING CAUGHT THE TYPE. The raise fell through to `except Exception`, which wrote
# that sentence into swaps.failed_reason and set the swap to 'failed' -- so the swap
# died of an unset environment variable, which is the exact thing the raise was written
# to prevent. A type created for a distinction nobody ever made.
#
# Found by the test above failing on its own fixture, which is the ordinary way (rule
# 17): the measurement came from running the code, not from reading it.


def test_an_unset_wallet_passphrase_defers_instead_of_failing(conn, monkeypatch):
    """THE REGRESSION TEST. MUTATION: delete the `except PayoutUnlockUnavailable` arm.

    GRC is the one asset in WALLET_UNLOCK_ASSETS, so this is the live case: a worker
    started without GRIDCOIN_WALLET_PASSPHRASE, with a perfectly good GRC adapter and a
    credited swap.

    THE PASSPHRASE IS EXPLICITLY DELETED rather than assumed absent, so the test states
    its own precondition instead of depending on the suite's environment -- and it never
    sets one, which is this repository's standing rule about passphrases.
    """
    monkeypatch.delenv("GRIDCOIN_WALLET_PASSPHRASE", raising=False)
    sent = []

    class GridcoinAdapter:
        asset = "GRC"

        def send_to_address(self, address, amount, **_kwargs):
            sent.append((address, amount))
            return "grc_txid_0001"

    completed = payout_service.process_pending_payouts(conn, CONFIG, {"GRC": GridcoinAdapter()})

    assert sent == [], "a send was attempted with no passphrase; the unlock would answer rpc -4"
    assert completed == []

    row = swap_row(conn)
    assert row["status"] == "payout_pending", (
        f"the swap is in {row['status']!r}; an unset environment variable is not a dead swap"
    )
    assert row["failed_reason"] is None


def test_the_deferred_swap_holds_no_payout_row_and_no_reservation(conn, monkeypatch):
    """Both have to be withdrawn, and for different reasons.

      the payouts row    is INSERTed and committed BEFORE the send, so leaving it at
                         'created' blocks the very retry this deferral is for --
                         idx_payouts_one_live_per_swap refuses a second live row.
      the reservation     has to come back or the hot wallet is permanently short on
                         paper, which is the correction repair_inventory_reservations.py
                         exists to make by hand.
    """
    monkeypatch.delenv("GRIDCOIN_WALLET_PASSPHRASE", raising=False)

    class GridcoinAdapter:
        asset = "GRC"

        def send_to_address(self, *_args, **_kwargs):
            raise AssertionError("must not be reached")

    payout_service.process_pending_payouts(conn, CONFIG, {"GRC": GridcoinAdapter()})

    live = conn.execute(
        "SELECT COUNT(*) AS n FROM payouts WHERE swap_id = 's_credited' AND status = 'created'"
    ).fetchone()["n"]
    assert live == 0, "a live payouts row was left behind and will block the retry"

    held = conn.execute(
        "SELECT hot_reserved FROM wallet_inventory WHERE asset = 'GRC'"
    ).fetchone()
    assert held is None or float(held["hot_reserved"]) == 0.0, (
        f"{held['hot_reserved']} GRC is still reserved for a payout that was never sent"
    )


def test_the_deferral_is_in_the_audit_trail_and_says_nothing_was_sent(conn, monkeypatch):
    """The operator reading a stuck swap must be able to tell this from a failed send.

    "NOTHING SENT" is the load-bearing phrase. A payout that may have been relayed and
    one that provably was not demand opposite actions, and payout_unlock_context() raises
    BEFORE entering the `with` -- so here "nothing happened" is a fact, not a hope.
    """
    monkeypatch.delenv("GRIDCOIN_WALLET_PASSPHRASE", raising=False)

    class GridcoinAdapter:
        asset = "GRC"

        def send_to_address(self, *_args, **_kwargs):
            raise AssertionError("must not be reached")

    payout_service.process_pending_payouts(conn, CONFIG, {"GRC": GridcoinAdapter()})

    messages = [
        row["message"]
        for row in conn.execute(
            "SELECT message FROM swap_audit_log WHERE swap_id = 's_credited' ORDER BY id"
        ).fetchall()
    ]
    deferrals = [text for text in messages if "deferred" in text.lower()]
    assert deferrals, f"nothing says why the payout did not happen: {messages}"
    note = deferrals[0]
    assert "NOTHING SENT" in note
    assert "GRIDCOIN_WALLET_PASSPHRASE" in note, "the variable to set is not named"
    # And the passphrase itself must never be in there. It is not set in this process,
    # so this asserts the shape rather than a value: no audit row may carry the word
    # the panel is forbidden to have a field for.
    assert "passphrase=" not in note.lower()


def test_a_passphrase_is_never_written_anywhere_by_the_deferral(conn, monkeypatch):
    """A standing rule of this repository, asserted rather than trusted.

    The deferral writes a logger line, an audit row and a status. If a future version
    composed its message from the environment, the passphrase would reach the database
    and the terminal at once. Setting a sentinel is the only way to assert the negative.
    """
    sentinel = "sentinel-value-that-must-not-be-recorded"
    monkeypatch.setenv("GRIDCOIN_WALLET_PASSPHRASE", sentinel)

    class LockRefusingAdapter:
        asset = "GRC"

        def walletpassphrase(self, *_args, **_kwargs):
            raise RuntimeError("wallet unlock refused by the daemon")

        def send_to_address(self, *_args, **_kwargs):
            raise AssertionError("must not be reached")

    # It does not matter which arm this takes -- unlock failure or send failure -- only
    # that the sentinel reaches no row and no column.
    payout_service.process_pending_payouts(conn, CONFIG, {"GRC": LockRefusingAdapter()})

    for table, column in (("swap_audit_log", "message"), ("swaps", "failed_reason")):
        values = [
            str(row[column] or "")
            for row in conn.execute(f"SELECT {column} FROM {table}").fetchall()  # noqa: S608 -- literals above
        ]
        assert not any(sentinel in value for value in values), (
            f"the wallet passphrase was written into {table}.{column}"
        )


def test_the_decision_is_callable_with_seeded_inputs(conn):
    """Rule 10: the decision is a function at the bottom, not a branch inside the loop.

    It was written inline first and ruff's C901 took process_pending_payouts() to 11 --
    the linter pointing at the layering rather than at a line count (rule 12). Being
    callable directly is what makes the two arms assertable without seeding a whole
    payout cycle, and this test is the proof that it is.
    """
    swap = dict(swap_row(conn))

    assert payout_service.defer_for_missing_adapter(conn, swap, "GRC", {"BTC": object()}) is True
    assert payout_service.defer_for_missing_adapter(conn, swap, "GRC", {"GRC": object()}) is False


def test_the_address_warning_decision_is_also_callable(conn):
    """The other function the C901 extraction produced, with its behavior unchanged.

    It is a decision ("must the operator be told nothing was checked") that had been
    written as a logging call inside orchestration. Extracting it did not change what it
    does, and that is what this asserts -- an unchecked verdict warns, a clean one does
    not, and neither refuses.
    """
    class Verdict:
        def __init__(self, unchecked):
            self.unchecked = unchecked
            self.refuses = False
            self.state = "NO_VALIDATOR"
            self.why = "no validator for this chain"

    swap = dict(swap_row(conn))
    assert payout_service.warn_if_address_unchecked(swap, Verdict(True), "GRC") is True
    assert payout_service.warn_if_address_unchecked(swap, Verdict(False), "GRC") is False
