"""Collecting the retained fee: what is swept, what is refused, and what a re-run does.

Role: tests (read-only against the real schema; writes only to pytest's tmp_path)
Reads: the real db.SCHEMA, the real fee_ledger derivation, the real
        fee_sweep decisions and the real collect_fees.py main(). No chain, no
        daemon, no network, and none of the operator's databases.
Writes: a temp SQLite file per test, created by the real schema
Can move funds: no. Every adapter here is tests/recording_rpc_adapter.py, which
        records calls and opens no socket, so the "broadcast" assertions are
        assertions on a recorded call list rather than on anything reaching a
        chain.
Mainnet-safe: yes.

OPERATOR, 2026-10-04: "i NEED to collect a fee to be profitable." The fee was
already theirs and already in the hot wallet -- services/quote_service.py charges
it as a subtraction, so the desk keeps 150bps by sending that much less -- and
what did not exist was any way to see it apart from inventory or to take it out.
collect_fees.py is the taking-it-out half. These are the tests for the two things
that can go wrong with that, and neither of them is arithmetic:

  SWEEPING TWICE       the accrued fee is DERIVED from completed swaps, so it does
                       not go down when it is collected. A second run against a
                       derived accrual re-sends it, and the second send comes out
                       of customer deposits.
  SWEEPING INVENTORY   the hot wallet holds customer deposits awaiting payout AND
                       retained fees, in the same coin. Sweeping more than the fee
                       spends money owed to a customer whose deposit is already
                       confirmed and irreversible.

BEHAVIORAL THROUGHOUT (CLAUDE.md's "Verify by behavior, never by reading the
code"). Every test seeds real rows into the real `swaps`, `payouts` and
`fee_sweeps` tables, runs the real collect_fees.main(), and asserts on the rows
ACTUALLY PRESENT afterwards and on the recorded `sendtoaddress` calls. No test
here asserts on printed text alone; where a sentence IS the subject -- a refusal
has to name the variable an operator must export -- the row count is asserted
beside it, so a test cannot pass on a message about a sweep that happened anyway.

THE FIXTURE ROWS ARE tests/test_fee_ledger.py'S, IMPORTED RATHER THAN RE-SEEDED.
That file's seeding is anchored to the operator's live 2026-10-01 devnet-SOL ->
testnet-GRC swap, read back out of their database -- quoted_rate 8995.69403675488,
output_amount_estimate 88.5975862620356, fee_bps 150, reserve 0.01 -- and its
test_the_live_swap_reproduces_its_recorded_payout asserts the seed arithmetic
against the figure the real create_quote() produced. Writing a second seeder here
would be rule 8's duplicate on the rows that decide every amount below, and it
would not carry that anchor.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "swap_terminal"))

from chains.payout_quantization import quantize_for_chain  # noqa: E402
from db import SCHEMA, connect_db  # noqa: E402

import collect_fees  # noqa: E402
from swap_terminal.fee_sweep import OPEN_SWAPS_PARAMS, OPEN_SWAPS_SQL, obligation  # noqa: E402
from tests.recording_rpc_adapter import RecordingRPCAdapter  # noqa: E402
from tests.test_fee_ledger import Seed, seed_payout, seed_swap  # noqa: E402
from tests.valid_addresses import GRC_PAYOUT  # noqa: E402

#: The fee that one seeded live swap retains, to eight decimals.
#:
#: NOT WRITTEN DOWN AS A LITERAL ANYWHERE BELOW. It is read back out of
#: fee_ledger.py in accrued_grc(), so a change to that derivation fails these
#: tests by moving the swept amount rather than by leaving them green against a
#: number this file remembers. tests/test_fee_ledger.py is where the figure itself
#: is pinned (1.3593541055132334 GRC on the live swap).

#: NOT A CREDENTIAL. chains/gridcoin_wallet_lock.py needs
#: GRIDCOIN_WALLET_PASSPHRASE present for a GRC send to be attempted at all, and
#: this is a sentinel whose only purpose is to be distinctive -- named the way
#: tests/test_gridcoin_wallet_lock.py names LEAK_SENTINEL so ruff's S105 is
#: answered rather than suppressed (rule 19).
UNLOCK_SENTINEL = "collect-fees-unlock-sentinel"

#: The destination variable collect_fees.py reads for GRC, spelled once.
GRC_DESTINATION_VAR = "GRC_FEE_SWEEP_DESTINATION"


@pytest.fixture
def db_path(tmp_path) -> str:
    """A real database built by the real SCHEMA, so `fee_sweeps` is the real table."""
    path = str(tmp_path / "collect_fees.db")
    connection = sqlite3.connect(path)
    connection.executescript(SCHEMA)
    connection.commit()
    connection.close()
    return path


class Sweeper(RecordingRPCAdapter):
    """A GRC adapter with a settable balance, and an optional failing wallet restore.

    SUBCLASSED RATHER THAN WRITTEN FRESH, so the send that is asserted on is the
    real RPCAdapter.send_to_address() path -- the same code the payout worker runs
    -- with only the transport recorded. A hand-written stub with its own
    `send_to_address` would assert that THIS FILE can call a method, which is the
    mistake tests/recording_rpc_adapter.py exists to prevent.

    `fail_staking_restore` RAISES ON THE SECOND walletpassphrase, which is the
    staking unlock in chains/gridcoin_wallet_lock.unlocked_for_payout()'s
    `finally`. That reproduces 2026-09-26 exactly: the send succeeded, the first
    walletlock succeeded, and the staking unlock was refused -- so the restore
    raises AFTER the money is gone, which is the ordering
    test_a_failed_wallet_restore_does_not_lose_the_txid is about.
    """

    def __init__(self, balance: float = 10_000.0, *, fail_staking_restore: bool = False):
        super().__init__("GRC")
        self._balance = balance
        self._fail_staking_restore = fail_staking_restore
        self._unlocks = 0

    def call(self, method, *params):
        if method == "walletpassphrase":
            self._unlocks += 1
            if self._fail_staking_restore and self._unlocks == 2:
                # Gridcoin's own refusal, which is what actually happened.
                raise RuntimeError("Timeout cannot be negative or zero. (rpc code -8)")
        return super().call(method, *params)

    def get_balance(self) -> float:
        return self._balance


def armed(monkeypatch, destination: str = GRC_PAYOUT) -> None:
    """Everything a GRC sweep needs in the environment, and nothing else.

    The passphrase AND the destination, because they are refused at different
    layers and a test that sets only one is measuring the wrong refusal:
    collect_fees.py refuses a missing destination itself, while a missing
    passphrase raises out of services/payout_service.payout_unlock_context() at
    the send.
    """
    monkeypatch.setenv("GRIDCOIN_WALLET_PASSPHRASE", UNLOCK_SENTINEL)
    monkeypatch.setenv(GRC_DESTINATION_VAR, destination)


def seed_one_completed_grc_swap(db_path: str, swap_id: str = "s_live") -> None:
    """One delivered GRC payout, so the fee ledger has something to retain."""
    connection = connect_db(db_path)
    try:
        paid = seed_swap(connection, swap_id)
        seed_payout(connection, swap_id, paid)
        connection.commit()
    finally:
        connection.close()


def seed_open_grc_swap(db_path: str, swap_id: str, status: str, expected_input: float) -> None:
    """One swap in `status` paying out in GRC, with nothing broadcast for it.

    The obligation floor is built from these: fee_sweep.OPEN_SWAPS_SQL selects
    every swap whose status is not terminal, and services/payout_service.
    payout_amount() prices each one.
    """
    connection = connect_db(db_path)
    try:
        seed_swap(connection, swap_id, Seed(expected_input=expected_input))
        connection.execute("UPDATE swaps SET status = ? WHERE id = ?", (status, swap_id))
        connection.commit()
    finally:
        connection.close()


def sweep_rows(db_path: str) -> list[dict]:
    """Every `fee_sweeps` row, oldest first. THE THING EVERY TEST ASSERTS ON."""
    connection = connect_db(db_path)
    try:
        return [dict(row) for row in connection.execute(
            "SELECT id, asset, destination_address, amount, txid, status FROM fee_sweeps ORDER BY id"
        ).fetchall()]
    finally:
        connection.close()


def accrued_grc(db_path: str) -> float:
    """What fee_ledger.py says this database retained in GRC. Read, never remembered.

    THE AUTHORITY FOR THE EXPECTED SWEEP AMOUNT, asked of the same derivation the
    tool asks, so a test cannot pass by agreeing with a figure this file wrote
    down. It is NOT a reimplementation: it calls fee_rows() and asset_totals() and
    reads `.retained`, which is what collect_fees.py does.
    """
    from swap_terminal.fee_ledger import (  # noqa: PLC0415 -- checked: imported here so the helper is readable beside the thing it asks; the module is already imported by collect_fees at the top of this file, so nothing extra is loaded.
        asset_totals,
        fee_rows,
    )

    connection = connect_db(db_path)
    try:
        return next(total.retained for total in asset_totals(fee_rows(connection)) if total.asset == "GRC")
    finally:
        connection.close()


def run(monkeypatch, db_path: str, adapter, *flags: str) -> int:
    """collect_fees.main() with `adapter` as the only chain. Returns its exit code.

    build_adapters() IS PATCHED AND NOTHING ELSE IS. The decisions, the SQL, the
    quantization, the wallet lock sequence, the send dispatch and every write are
    the real ones; only the transport is replaced, by the shared recording adapter.
    Patching further -- stubbing sweep_plan(), or the broadcast -- would make these
    tests about this file rather than about the tool.
    """
    monkeypatch.setattr(collect_fees, "build_adapters", lambda _rpc: {"GRC": adapter})
    return collect_fees.main(["--db", db_path, *flags])


# --------------------------------------------------------------- the report only


def test_the_default_run_writes_nothing_and_broadcasts_nothing(monkeypatch, db_path, capsys):
    """REPORT ONLY has to be read-only in BOTH directions, and that is two assertions.

    A tool that moves funds behind a flag has exactly one failure that matters
    before the flag is passed, and it is that something moved anyway. So this
    asserts on the recorded call list (nothing reached the transport) AND on the
    table (no row was written), because either one alone would pass for the wrong
    reason: a run that recorded an intent and failed to send would satisfy the
    first, and a run that sent without recording would satisfy the second.

    MUTATION: in collect_fees.main(), change `if args.apply:` to `if True:` around
    the apply loop. This fails on both assertions. Verified 2026-10-04.
    """
    armed(monkeypatch)
    seed_one_completed_grc_swap(db_path)
    adapter = Sweeper()

    assert run(monkeypatch, db_path, adapter) == 0

    assert adapter.sent == [], "a report-only run reached the transport with a send"
    assert sweep_rows(db_path) == [], "a report-only run wrote a fee_sweeps row"
    printed = capsys.readouterr().out
    assert "REPORT ONLY" in printed
    assert "SWEEP  1 asset(s)" in printed, printed


def test_an_asset_with_no_delivered_payout_reports_a_result_rather_than_a_blank(
    monkeypatch, db_path, capsys
):
    """`(none)` is a result and a blank gap is not (rule 14).

    An empty ledger and a broken query must not render the same way, and "no fee
    was earned" is not the same fact as "the fee was zero" -- show_fees.py draws
    the same distinction over the same rows.
    """
    adapter = Sweeper()

    assert run(monkeypatch, db_path, adapter) == 0

    printed = capsys.readouterr().out
    assert "(none)" in printed
    assert "NOT a fee of zero" in printed, printed
    assert sweep_rows(db_path) == []


# ------------------------------------------------------------------ --apply


def test_apply_sweeps_exactly_the_accrued_fee_and_records_it(monkeypatch, db_path):
    """The amount sent, the amount recorded and the amount accrued are ONE number.

    THREE READINGS OF IT, and all three have to agree or the tool is wrong in a way
    no single assertion catches:

      the wire     the `sendtoaddress` parameter the adapter recorded -- what the
                   chain was actually asked to move
      the row      `fee_sweeps.amount` -- what the desk's own record says left
      the ledger   fee_ledger.py's retained figure for GRC, quantized to what the
                   chain can express

    This is tests/test_payout_quantization.py's shape and its reason: a tool that
    sent the right amount and recorded a different one would pass a wire-only
    assertion, and one that recorded correctly and sent the quote's over-precise
    figure would pass a row-only one. The operator's host had the second defect on
    23 of 23 payout rows.

    MUTATION: in collect_fees.apply_sweep(), pass `plan.amount` to
    record_sweep_intent() instead of the quantized `amount`. The row assertion
    fails and the wire assertion keeps passing. Verified 2026-10-04.
    """
    armed(monkeypatch)
    seed_one_completed_grc_swap(db_path)
    adapter = Sweeper()
    expected, _note = quantize_for_chain(accrued_grc(db_path), "GRC")

    assert run(monkeypatch, db_path, adapter, "--apply") == 0

    assert adapter.sent == [expected], "the figure on the wire is not the accrued fee"
    rows = sweep_rows(db_path)
    assert len(rows) == 1, f"one sweep, one row: {rows}"
    assert rows[0]["amount"] == expected
    assert rows[0]["asset"] == "GRC"
    assert rows[0]["destination_address"] == GRC_PAYOUT
    assert rows[0]["status"] == "broadcast"
    assert rows[0]["txid"], "a broadcast sweep with no txid is money out with no record"


def test_a_second_run_sweeps_nothing(monkeypatch, db_path, capsys):
    """IDEMPOTENCY, AND IT IS THE HARD PART.

    The accrued figure is DERIVED from completed swaps, so it is exactly the same
    on the second run as on the first -- nothing about `swaps` or `payouts` changes
    when a fee is collected, because the retained coin never had a row of its own.
    The ONLY thing standing between a second `--apply` and a second send is the
    `fee_sweeps` row the first one wrote.

    So this asserts that the second run reached the transport ZERO times and that
    the table still holds ONE row, rather than asserting on the word ALREADY SWEPT
    -- a tool that printed that sentence and sent anyway would pass a text check.

    THE FIRST RUN LEAVES A SUB-SATOSHI RESIDUE, AND THAT WAS A DEFECT FOUND BY
    THIS TEST, 2026-10-04. The accrued fee is 1.3593541055132334 GRC and the chain
    can express 1.35935410, so 5.5e-9 GRC is left behind. Before sweep_plan()
    quantized, that remainder read as `> 0` and every later run reported GRC under
    SWEEP and offered a command to collect it, while --apply sent nothing because
    the amount quantizes to zero at the send -- a report claiming there is money to
    collect, forever. The ALREADY SWEPT assertion below is what holds that fix.

    MUTATION: in fee_sweep.sweep_plan(), replace `unswept = accrual.accrued -
    accrual.swept` with `unswept = accrual.accrued`. The second run sends again,
    `second.sent` has one entry and the table grows to two rows. Verified
    2026-10-04.
    """
    armed(monkeypatch)
    seed_one_completed_grc_swap(db_path)
    first = Sweeper()
    assert run(monkeypatch, db_path, first, "--apply") == 0
    assert len(first.sent) == 1, "the first run has to have swept, or this measures nothing"

    second = Sweeper()
    assert run(monkeypatch, db_path, second, "--apply") == 0

    assert second.sent == [], "the second run swept the same fee again"
    assert len(sweep_rows(db_path)) == 1, "a second fee_sweeps row was written for one accrual"
    assert "ALREADY SWEPT  1 asset(s)" in capsys.readouterr().out


def test_a_sweep_recorded_but_never_reported_sent_blocks_a_re_run(monkeypatch, db_path):
    """A 'created' row is money POSSIBLY on chain, and it must count as GONE.

    This is the state a process killed between the committed intent and the
    broadcast leaves behind, and the two directions are not symmetric: counting it
    as still-sweepable risks a second send of coin that already left, where
    counting it as gone costs a fee that stays in the wallet until somebody looks.
    db.py's `fee_sweeps` DDL records that choice; this is the behavioral pin.

    MUTATION: in fee_sweep.SWEPT_SQL, narrow the predicate to
    `status = 'broadcast'`. This fails -- the run sweeps the whole accrual again on
    top of a row that may already be on chain. Verified 2026-10-04.
    """
    armed(monkeypatch)
    seed_one_completed_grc_swap(db_path)
    connection = connect_db(db_path)
    try:
        connection.execute(
            "INSERT INTO fee_sweeps (asset, destination_address, amount, txid, status, created_at)"
            " VALUES ('GRC', ?, ?, NULL, 'created', '2026-10-04T00:00:00+00:00')",
            (GRC_PAYOUT, accrued_grc(db_path)),
        )
        connection.commit()
    finally:
        connection.close()
    adapter = Sweeper()

    assert run(monkeypatch, db_path, adapter, "--apply") == 0

    assert adapter.sent == [], "a stranded 'created' sweep did not stop a second send"
    assert len(sweep_rows(db_path)) == 1


def test_a_failed_send_leaves_the_fee_sweepable(monkeypatch, db_path):
    """A send the daemon refused must not count as collected.

    The other direction from the test above, and the reason 'failed' is outside
    SWEPT_SQL's predicate: nothing reached a chain, so the fee is still in the
    wallet and the next run should offer it again. Asserted on the row's status and
    on the second run actually sending, not on the message.
    """
    armed(monkeypatch)
    seed_one_completed_grc_swap(db_path)

    class Refusing(Sweeper):
        def call(self, method, *params):
            if method == "sendtoaddress":
                raise RuntimeError("Insufficient funds (rpc code -4)")
            return super().call(method, *params)

    assert run(monkeypatch, db_path, Refusing(), "--apply") == 0
    rows = sweep_rows(db_path)
    assert len(rows) == 1 and rows[0]["status"] == "failed", rows
    assert rows[0]["txid"] is None

    retry = Sweeper()
    assert run(monkeypatch, db_path, retry, "--apply") == 0
    assert len(retry.sent) == 1, "a refused send left the fee uncollectable"


# ------------------------------------------------------------------ refusals


def test_an_unset_destination_refuses_and_names_the_variable(monkeypatch, db_path, capsys):
    """A defaulted fee address is a final transaction to somewhere nobody chose.

    The refusal has to NAME the variable, because the person reading it is the only
    one who can set it and rule 14's complaint is about output that reports a
    problem without the remedy. The row assertion is beside it so this cannot pass
    on a message about a sweep that happened anyway.

    MUTATION: give config.fee_sweep_destination() a default --
    `return _env(..., "some-address")`. This fails: the verdict becomes SWEEP and
    the run broadcasts to an address no operator picked. Verified 2026-10-04.
    """
    monkeypatch.setenv("GRIDCOIN_WALLET_PASSPHRASE", UNLOCK_SENTINEL)
    monkeypatch.delenv(GRC_DESTINATION_VAR, raising=False)
    seed_one_completed_grc_swap(db_path)
    adapter = Sweeper()

    assert run(monkeypatch, db_path, adapter, "--apply") == 5, "a refusal must not exit 0"

    assert adapter.sent == [], "a sweep went out with no configured destination"
    assert sweep_rows(db_path) == []
    printed = capsys.readouterr().out
    assert GRC_DESTINATION_VAR in printed, printed
    assert "REFUSE  1 asset(s)" in printed, printed


def test_a_sweep_that_would_strand_an_open_swaps_payout_is_refused(monkeypatch, db_path, capsys):
    """THE SAFETY PROPERTY. A sweep that strands a payout is worse than no sweep.

    Seeded to be exactly that case and nothing else: one delivered payout, so a fee
    is accrued and sweepable; one swap still in `payout_pending`, whose payout a
    worker will send; and a wallet balance that covers that payout and its chain
    fee with less than the accrued fee to spare. Every other condition is
    satisfiable -- the destination is set, the passphrase is set, the balance reads
    -- so the only thing that can refuse is the obligation arithmetic.

    THE BALANCE IS DERIVED FROM THE OBLIGATION rather than chosen, because a
    hand-picked number would stop measuring the refusal the first time
    payout_amount() changed what an open swap needs. It is set to the floor plus
    the chain fee plus a hair, which leaves headroom smaller than any real fee.

    MUTATION: in fee_sweep.sweep_plan(), change `headroom = ceiling.amount -
    owed.floor` to `headroom = ceiling.amount`. This fails -- the sweep is allowed
    and the wallet is left unable to fund a credited customer's payout. Verified
    2026-10-04.
    """
    armed(monkeypatch)
    seed_one_completed_grc_swap(db_path)
    seed_open_grc_swap(db_path, "s_open", "payout_pending", expected_input=0.5)
    connection = connect_db(db_path)
    try:
        floor = obligation(connection, "GRC").floor
    finally:
        connection.close()
    assert floor > 0, "the obligation floor is zero, so this test is not measuring a refusal"
    # The chain fee is GRC_NETWORK_FEE_RESERVE; a hundredth of a coin of headroom
    # is far below the ~1.36 GRC the seeded swap retained.
    adapter = Sweeper(balance=floor + 0.001 + 0.01)

    assert run(monkeypatch, db_path, adapter, "--apply") == 5

    assert adapter.sent == [], "a sweep went out that would strand an open swap's payout"
    assert sweep_rows(db_path) == []
    printed = capsys.readouterr().out
    assert "would leave this wallet unable to fund what it owes" in printed, printed
    assert "obligated" in printed


def test_an_unreadable_balance_refuses_rather_than_sweeping_on_an_unknown(
    monkeypatch, db_path, capsys
):
    """A sweep cannot check the floor without a balance, so it refuses.

    THE OPPOSITE RESOLUTION FROM services/payout_capacity.py's `unchecked`, which
    warns and proceeds for a PAYOUT. The asymmetry is the point and is recorded at
    both sites: by the time that gate is asked the customer's deposit is already
    credited and refusing does not give it back, where refusing a sweep costs
    nobody anything -- the fee stays in the wallet and stays sweepable.

    MUTATION: in fee_sweep.WalletCeiling.from_largest_fundable_payout(), replace
    `cls(None if amount < 0 else amount, how)` with `cls(amount, how)`, so
    services/payout_capacity.py's -1.0 NOT-ESTABLISHED sentinel reads as a
    balance. This fails. It is the mutation worth running rather than deleting the
    `is None` branch, because deleting that branch raises a TypeError on the
    subtraction and a test that passes on a traceback is measuring the wrong thing
    -- here the refusal still happens (a balance of -1.0 leaves negative headroom)
    and the SENTENCE is wrong, which is the failure an operator would actually be
    handed. Verified 2026-10-04.
    """
    armed(monkeypatch)
    seed_one_completed_grc_swap(db_path)

    class Unreadable(Sweeper):
        def get_balance(self):
            raise RuntimeError("connection refused")

    adapter = Unreadable()
    assert run(monkeypatch, db_path, adapter, "--apply") == 5

    assert adapter.sent == []
    assert sweep_rows(db_path) == []
    assert "was NOT established" in capsys.readouterr().out


# ---------------------------------------------------- the wallet lock ordering


def test_a_failed_wallet_restore_does_not_lose_the_txid(monkeypatch, db_path, capsys):
    """2026-09-26, ONE CHAIN OVER: money out, no record. It must not happen again.

    services/payout_service.py recorded a delivered payout AFTER the
    `with payout_unlock_context(...)` block. The send succeeded -- 55.52645238 GRC
    left the wallet, txid 3e09dc9cfd7a61da... -- and then the context's restore
    raised, because Gridcoin refuses a staking unlock timeout of 0. Control jumped
    past the record and the swap was written down as failed with txid (none).

    A sweep has exactly that shape, so this seeds exactly that failure: the send
    succeeds, the first walletlock succeeds, and the staking unlock is refused with
    Gridcoin's own rpc code -8 message. The sweep must still be recorded as
    'broadcast' WITH its txid, because the money is gone and the wallet's lock
    state is a different problem.

    MUTATION: in collect_fees.apply_sweep(), move the record_sweep_broadcast()
    call and the `recorded = True` out of the `with` block to just after it. This
    fails -- the row reads 'failed' with txid NULL for a sweep that was
    broadcast, which is the original defect reproduced. Verified 2026-10-04.
    """
    armed(monkeypatch)
    seed_one_completed_grc_swap(db_path)
    adapter = Sweeper(fail_staking_restore=True)

    assert run(monkeypatch, db_path, adapter, "--apply") == 0

    assert len(adapter.sent) == 1, "the send has to have happened, or this measures a quiet path"
    rows = sweep_rows(db_path)
    assert len(rows) == 1, rows
    assert rows[0]["status"] == "broadcast", (
        f"a broadcast sweep was recorded as {rows[0]['status']!r} because the wallet restore raised: "
        f"money out, no record"
    )
    assert rows[0]["txid"], "the txid of a transaction that already left the wallet was discarded"
    printed = capsys.readouterr().out
    assert "act on the WALLET, not on this sweep" in printed, printed
    assert UNLOCK_SENTINEL not in printed, "the passphrase reached the output"


def test_the_grc_send_goes_through_the_lock_cycle_the_payout_path_uses(monkeypatch, db_path):
    """lock -> full unlock -> send -> lock -> staking unlock, in that order.

    REUSED AND NOT REIMPLEMENTED (rule 8): a second lock dance in this tree would
    be two rules about the wallet's security state, drifting from the day they were
    written. Asserted as a SEQUENCE of recorded methods, which is the only way to
    show the send happened between the unlock and the re-lock rather than merely
    that all five calls were made.

    MUTATION: in collect_fees.apply_sweep(), replace
    `with payout_unlock_context(asset, adapters[asset]):` with
    `with nullcontext():`. This fails -- the recorded methods are
    ['sendtoaddress'] alone, which on a real staking wallet is the rpc code -4
    refusal that made every GRC payout fail before 2026-09-26. Verified 2026-10-04.
    """
    armed(monkeypatch)
    seed_one_completed_grc_swap(db_path)
    adapter = Sweeper()

    assert run(monkeypatch, db_path, adapter, "--apply") == 0

    methods = [method for method, _params in adapter.calls]
    # THE OWNERSHIP READ COMES FIRST AND THE ASSERTION NOW SAYS SO -- a stronger
    # invariant than the equality it replaces, not a weaker one.
    #
    # This asserted the five lock-cycle methods as the WHOLE list until 2026-10-04,
    # when fee_sweep.destination_refusal() added a validateaddress/getaddressinfo
    # read of the destination and the list gained a leading sixth. The equality
    # failed on a change that violated nothing it was written to protect: the real
    # invariant is "the send happened BETWEEN the unlock and the re-lock", which a
    # read-only call in front cannot break.
    #
    # So it is split in two, and the first half is new ground. A destination read
    # BEFORE any walletpassphrase means the address is checked while the wallet
    # still cannot spend -- so a refusal cannot leave a wallet unlocked, and the
    # window in which this process can send is as short as the send itself. Had
    # that call landed inside the unlock context it would have passed the old
    # equality's successor just as happily while widening that window.
    ownership_reads = [m for m in methods if m in ("validateaddress", "getaddressinfo")]
    assert ownership_reads, (
        "the destination was never asked about, so a sweep into the desk's own wallet would "
        "broadcast, cost a chain fee and move nothing -- the defect destination_refusal() exists "
        "for, and it cannot refuse on an answer nobody requested"
    )
    assert methods.index(ownership_reads[0]) < methods.index("walletpassphrase"), (
        f"the destination was read AFTER the wallet was unlocked for spending, which widens the "
        f"send window for a question answerable with the wallet locked: {methods}"
    )
    assert [m for m in methods if m.startswith("wallet") or m == "sendtoaddress"] == [
        "walletlock", "walletpassphrase", "sendtoaddress", "walletlock", "walletpassphrase",
    ], methods


# ------------------------------------------------ what counts as an obligation


def test_the_obligation_floor_retains_for_exactly_the_non_terminal_statuses(db_path):
    """Every status in the vocabulary, seeded, and the split asserted against it.

    IT RUNS THE REAL QUERY NOW, AND THE PARAGRAPH IT REPLACES IS WHY THAT MATTERS.
    This used to read:

    > THIS TEST IS THE DERIVATION fee_sweep.OPEN_SWAPS_SQL COULD NOT HAVE. That
    > query spells the three terminal statuses as SQL literals, because
    > interpolating services/swap_view.TERMINAL_STATUSES earns ruff's S608 and rule
    > 19 forbids answering a finding with a suppression -- so the agreement between
    > the two is held here instead, behaviorally.

    and it held that agreement by SPELLING THE PREDICATE A SECOND TIME, right here,
    which is the duplicate rule 8 is about rather than a guard against it: the copy
    in this file agreed with the copy in fee_sweep.py on the day both were written,
    and on 2026-10-07 a ninth status (`expired`) was added to TERMINAL_STATUSES and
    the two disagreed. A test carrying its own copy of the thing under test can only
    ever assert that its copy is self-consistent -- which is the exact failure
    BEHAVIORAL_VERIFICATION_PRINCIPLE names: "never accept 'the SQL text contains X'
    as evidence a gate is enforced."

    So it executes fee_sweep.OPEN_SWAPS_SQL with fee_sweep.OPEN_SWAPS_PARAMS, which
    is what obligation() runs, and the suppression that forced the duplicate is
    gone: the statuses are bind values through json_each() rather than interpolated
    text. A status added to STATUS_MEANINGS, or moved into or out of
    TERMINAL_STATUSES, still fails this by name rather than quietly changing what
    the hot wallet is allowed to keep.

    Asserted as a SET of swap ids rather than as a total, so a failure says WHICH
    status moved sides instead of only that a number changed.

    MUTATION: add 'payout_pending' to the SQL's NOT IN list. This fails, naming
    that swap -- and it is the status that matters most, since a payout_pending
    swap is one a worker will pay on its next poll. Verified 2026-10-04.

    RE-MUTATED 2026-10-07 AGAINST THE PARAMETERIZED FORM, AND THE RESULT CHANGED
    WHAT THIS TEST IS FOR -- recorded rather than quietly left, because a mutation
    that no longer applies reads as a mutation that survived.

    The old mutation (add 'payout_pending' to the SQL's NOT IN list) still fails
    this, and so does narrowing the predicate any other way: `AND status <>
    'paying'` appended to it fails with `s_paying` missing from the left side.
    Verified 2026-10-07.

    But editing TERMINAL_STATUSES no longer fails it AT ALL, in either direction --
    verified by removing 'expired' from the set and running this file: 43 passed.
    That is not a weakness that crept in, it is the duplicate being gone: both the
    query and this test's expectation now derive from the same frozenset, so they
    cannot disagree, and a test asserting that a thing equals itself is the
    tautology the old hand-spelled copy was hiding behind. What still has to be
    asserted BEHAVIORALLY is that the floor actually changes when a status moves
    sides, and that lives in tests/test_expire_swap.py::
    test_an_expired_swap_leaves_the_obligation_floor -- which builds the floor with
    obligation(), retires a swap with the real tool, and builds it again. That one
    DOES fail when 'expired' leaves TERMINAL_STATUSES.

    What this test still pins on its own, and the mutations that prove it:
      - obligation() agrees with the real query's row set, for every status in the
        vocabulary at once ( `AND status <> 'paying'` fails it)
      - `revivable` is exactly under_review and failed, so an expired swap is not
        reported as a payout waiting to happen (adding 'expired' to
        REVIVABLE_SWAPS_SQL fails it, and fails test_expire_swap.py with it)
    """
    from services.swap_view import (  # noqa: PLC0415 -- checked: imported here beside the assertion that reads it, so a reader sees what the expectation is derived from. The module is already imported transitively by collect_fees at the top of this file.
        STATUS_MEANINGS,
        TERMINAL_STATUSES,
    )

    for status in sorted(STATUS_MEANINGS):
        seed_open_grc_swap(db_path, f"s_{status}", status, expected_input=0.01)
    connection = connect_db(db_path)
    try:
        retained_for = {
            str(row["id"])
            # THE QUERY ITSELF, with the parameters obligation() passes it. Not a
            # paraphrase and not a fragment: the assertion below compares the real
            # predicate's answer against TERMINAL_STATUSES, where comparing against
            # obligation()'s own count alone would agree with itself whatever the
            # predicate said.
            for row in connection.execute(OPEN_SWAPS_SQL, ("GRC", OPEN_SWAPS_PARAMS)).fetchall()
        }
        owed = obligation(connection, "GRC")
    finally:
        connection.close()

    assert retained_for == {
        f"s_{status}" for status in STATUS_MEANINGS if status not in TERMINAL_STATUSES
    }, "fee_sweep.OPEN_SWAPS_SQL and services/swap_view.TERMINAL_STATUSES have drifted apart"
    assert owed.open_swaps == len(retained_for), (
        "obligation() counted a different number of open swaps than the query selects"
    )
    assert owed.revivable_swaps == len(
        [status for status in STATUS_MEANINGS if status in {"under_review", "failed"}]
    ), "a halted or failed swap must be REPORTED as revivable even though it is not in the floor"


def test_a_reservation_with_no_open_swap_still_holds_the_floor(db_path):
    """wallet_inventory.hot_reserved is honored even when no open swap explains it.

    `floor` is the LARGER of the two figures, deliberately: a reservation standing
    without an open swap row should be impossible -- reserve_inventory() adds one
    per claimed payout and release_inventory_after_send() subtracts it -- and
    honoring it anyway costs an unswept fee, where assuming it away could cost a
    payout that was already claimed.

    MUTATION: in fee_sweep.obligation(), `floor=max(open_total, reserved)` ->
    `floor=open_total`. This fails. Verified 2026-10-04.
    """
    connection = connect_db(db_path)
    try:
        connection.execute(
            "INSERT INTO wallet_inventory (asset, hot_confirmed, hot_reserved, hot_available, updated_at)"
            " VALUES ('GRC', 500.0, 42.5, 457.5, '2026-10-04T00:00:00+00:00')"
        )
        connection.commit()
        owed = obligation(connection, "GRC")
    finally:
        connection.close()

    assert owed.open_swaps == 0
    assert owed.open_total == 0.0
    assert owed.reserved == 42.5
    assert owed.floor == 42.5, "a standing reservation did not hold the floor"


# ----------------------------------------------------------------- the register


def test_an_unknown_asset_is_refused_rather_than_matching_nothing(monkeypatch, db_path, capsys):
    """"--asset GCR swept nothing" and "GRC has nothing to sweep" must not read alike.

    Rule 14's "did nothing must not look like did work", reached by a typo, on a
    flag whose whole purpose is to narrow what moves money.
    """
    armed(monkeypatch)
    seed_one_completed_grc_swap(db_path)
    adapter = Sweeper()

    assert run(monkeypatch, db_path, adapter, "--apply", "--asset", "GCR") == 2

    assert adapter.sent == []
    assert sweep_rows(db_path) == []
    assert "is not a chain this terminal knows" in capsys.readouterr().err


def test_asset_narrows_what_apply_sends_and_says_what_it_left(monkeypatch, db_path, capsys):
    """A partial collection must not read as a complete one.

    Seeded with a retention in GRC and an --asset naming a DIFFERENT configured
    chain, so the GRC sweep is reported and deliberately not sent. The footer has
    to say so, because an operator who narrowed a run and saw no mention of what
    was skipped would read it as finished.
    """
    armed(monkeypatch)
    seed_one_completed_grc_swap(db_path)
    adapter = Sweeper()

    assert run(monkeypatch, db_path, adapter, "--apply", "--asset", "BTC") == 0

    assert adapter.sent == [], "--asset BTC swept GRC"
    assert sweep_rows(db_path) == []
    assert "STILL SWEEPABLE and NOT sent" in capsys.readouterr().out
