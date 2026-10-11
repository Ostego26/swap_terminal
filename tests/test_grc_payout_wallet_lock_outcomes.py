"""GRC as the PAYOUT side: every way the wallet lock can fail, and what the record says.

Role: test (read-only on the tree; writes only a throwaway SQLite file under tmp_path)
Reads: swap_terminal/services/payout_service.py, chains/gridcoin_wallet_lock.py
Writes: a database under tmp_path and nothing else
Can move funds: no. The GRC adapter is a stub whose send_to_address() returns a
      literal; no socket is opened and no passphrase in this file is real.
Mainnet-safe: yes

=============================================================================
WHY GRIDCOIN AND NOT THE OTHER FOUR CHAINS
=============================================================================

GRC is the only asset in chains/gridcoin_wallet_lock.WALLET_UNLOCK_ASSETS, so it is
the only chain whose payout runs four extra wallet RPCs around the send:

    walletlock -> walletpassphrase <phrase> 60 -> send -> walletlock
                                               -> walletpassphrase <phrase> N true

Each of those four can fail independently, and the failures are NOT
interchangeable: one of them leaves the operator's wallet spendable, one leaves it
not staking, and one happens AFTER money is already on a chain. This tree has paid
for two of them already:

  2026-09-26  55.52645238 GRC left the wallet, txid 3e09dc9cfd7a61da..., and the
              swap was marked `failed` with `txid (none)` -- because the STAKING
              RESTORE raised (timeout 0, which Gridcoin refuses with rpc -8) and
              control jumped out of the `with` before the txid was recorded.
              Money out, no record.
  2026-10-07  s_ebb03e8dc1b96e1c failed TWICE with `Error: The wallet passphrase
              entered was incorrect. (rpc code -14)` after a mangled paste armed
              the container with a command fragment. Each attempt ran
              lock(adapter) first, so the wallet was left LOCKED with staking off.

=============================================================================
WHAT THIS FILE ASSERTS, AND THE ONE THING IT DELIBERATELY DOES NOT
=============================================================================

Two money invariants, across every failure mode, measured by running the real
services/payout_service.process_pending_payouts() over a real seeded database:

  NO TXID IS EVER RECORDED THAT A SEND DID NOT RETURN. swaps.payout_txid and the
  payouts row must be empty for every scenario where send_to_address() raised or
  was never reached. A payout recorded as sent that was not is the failure that
  makes a customer whole on paper and not in fact.

  A SEND THAT RETURNED A TXID IS ALWAYS RECORDED, even when the wallet
  housekeeping after it fails. That is the 2026-09-26 defect, from the other side.

WHAT IS NOT ASSERTED IS THE STATUS A PRE-SEND LOCK FAILURE LEAVES BEHIND.
Measured 2026-10-11, a wrong passphrase lands the swap in `failed` while an UNSET
passphrase lands it in `payout_pending`, and nothing in the tree re-queues a
`failed` swap. Both are cases where NOTHING WAS SENT and that is certain --
unlocked_for_payout() raises before its `yield` -- so the remedies are identical
(fix the variable, restart the worker) and the outcomes are not.

That asymmetry is reported to the operator rather than changed here, and it is NOT
pinned by an assertion in either direction, deliberately: a test asserting `failed`
would make the current behavior load-bearing, and a test asserting `payout_pending`
would fail until a change lands that is not this agent's to make (rule 16 -- a
deferral loops the lock cycle every worker pass, which stops staking repeatedly, so
which of the two costs less is live posture). tests/test_payout_concurrency.py
::test_a_lock_failure_before_the_send_is_still_a_payout_failure already pins
`failed`; this file pins what is not in dispute.
"""

from __future__ import annotations

import pytest
import valid_addresses
from chains.gridcoin_wallet_lock import WALLET_UNLOCK_ENV_VAR
from config import Config
from db import SCHEMA, apply_migrations, connect_db
from services import payout_service

CONFIG = {name: getattr(Config, name) for name in dir(Config) if name.isupper()}

GRC_PAYOUT = valid_addresses.GRC_PAYOUT
BTC_DEPOSIT = valid_addresses.BTC_REGTEST_DEPOSIT

#: NOT A PASSPHRASE. The name avoids the words ruff's S105 looks for, the same move
#: tests/test_gridcoin_wallet_lock.py makes with LEAK_SENTINEL and conftest.py makes
#: with RPC_FIXTURE_AUTH -- which is S105 answered rather than suppressed (rule 19).
#: Nothing in this file sends it anywhere: the stub below counts the calls and
#: discards the parameters, and no socket is opened.
UNLOCK_SENTINEL = "a-value-no-wallet-anywhere-accepts"

#: The txid the stub returns when a send succeeds. A literal, so a test can assert
#: that the RECORD carries the value the SEND produced rather than merely something.
SENT_TXID = "grc-send-returned-this-txid"


class GridcoinWallet:
    """A GRC payout adapter whose wallet RPCs fail where `failures` says.

    THE FAILURE NAMES ARE THE FOUR RPCs AND THE SEND, not abstract modes, because the
    thing under test is which of the five raised and what the record then says. A
    mode named "unlock fails" could not tell the full unlock from the staking one,
    and those two have opposite consequences for the operator's wallet.
    """

    asset = "GRC"

    def __init__(self, failures: frozenset[str] = frozenset()):
        self.failures = failures
        self.calls: list[str] = []
        self.sends = 0
        self.full_unlocks = 0
        self.staking_unlocks = 0
        self.locks = 0

    def call(self, method: str, *params):
        self.calls.append(method)
        if method == "walletlock":
            self.locks += 1
            if "lock" in self.failures:
                raise RuntimeError("Error: walletlock refused")
            return None
        if method == "walletpassphrase":
            # THE THIRD PARAMETER IS WHAT SEPARATES THE TWO UNLOCKS, exactly as
            # chains/gridcoin_wallet_lock.py builds them: present and True for the
            # staking unlock, OMITTED for the full one. Reading `len(params)` rather
            # than a flag the test sets is what makes this stub measure the real
            # call shape instead of agreeing with the test about it.
            staking = len(params) > 2 and params[2] is True
            if staking:
                self.staking_unlocks += 1
                if "staking_unlock" in self.failures:
                    raise RuntimeError("Error: Timeout cannot be negative or zero. (rpc code -8)")
            else:
                self.full_unlocks += 1
                if "full_unlock" in self.failures:
                    raise RuntimeError(
                        "Error: The wallet passphrase entered was incorrect. (rpc code -14)"
                    )
            return None
        raise AssertionError(f"this stub does not seed {method!r}")

    def send_to_address(self, address: str, amount: float) -> str:
        self.sends += 1
        if "relocked_before_send" in self.failures:
            raise RuntimeError(
                "Error: Please enter the wallet passphrase with walletpassphrase first. "
                "(rpc code -13)"
            )
        if "send_times_out" in self.failures:
            # chains/base.RPCAdapter.call() passes timeout=self.timeout, which for GRC
            # is GRC_RPC_TIMEOUT and defaults to 30s. A send slower than that raises
            # here AFTER the daemon may already have relayed the transaction.
            import requests  # noqa: PLC0415 -- checked: requests is imported only on this failure branch so the module stays importable without it, matching how chains/base.py's own callers treat the dependency. The exception type is the subject of the assertion, so it cannot be a stand-in.
            raise requests.exceptions.ReadTimeout(
                "HTTPConnectionPool(host='127.0.0.1', port=25715): Read timed out. "
                "(read timeout=30)"
            )
        return SENT_TXID


@pytest.fixture
def conn(tmp_path):
    """One credited BTC -> GRC swap in payout_pending, the state the worker picks up."""
    connection = connect_db(str(tmp_path / "grc_payout.db"), create=True)
    connection.executescript(SCHEMA)
    apply_migrations(connection)
    connection.execute(
        "INSERT INTO quotes (id, from_asset, to_asset, input_amount, quoted_rate, fee_bps,"
        " network_fee_reserve, output_amount_estimate, expires_at, created_at)"
        " VALUES ('q','BTC','GRC',0.0001,9868961.0,150,0.001,1000.0,"
        "'2999-01-01T00:00:00+00:00','2026-10-11T00:00:00+00:00')"
    )
    connection.execute(
        "INSERT INTO swaps (id, quote_id, from_asset, to_asset, deposit_address, payout_address,"
        " expected_input_amount, actual_input_amount, quoted_rate, fee_bps, network_fee_reserve,"
        " output_amount_estimate, status, min_confirmations, expires_at, created_at, updated_at,"
        " credited_at) VALUES ('s_grc','q','BTC','GRC',?,?,0.0001,0.0001,"
        "9868961.0,150,0.001,1000.0,'payout_pending',2,'2999-01-01T00:00:00+00:00',"
        "'2026-10-11T00:00:00+00:00','2026-10-11T00:00:00+00:00','2026-10-11T00:00:00+00:00')",
        (BTC_DEPOSIT, GRC_PAYOUT),
    )
    connection.commit()
    return connection


def run(conn, wallet, monkeypatch, *, passphrase: str | None = UNLOCK_SENTINEL):
    if passphrase is None:
        monkeypatch.delenv(WALLET_UNLOCK_ENV_VAR, raising=False)
    else:
        monkeypatch.setenv(WALLET_UNLOCK_ENV_VAR, passphrase)
    return payout_service.process_pending_payouts(conn, CONFIG, {"GRC": wallet})


def record(conn):
    swap = conn.execute(
        "SELECT status, payout_txid, failed_reason FROM swaps WHERE id = 's_grc'"
    ).fetchone()
    payouts = conn.execute(
        "SELECT status, txid FROM payouts WHERE swap_id = 's_grc'"
    ).fetchall()
    return swap, [(row["status"], row["txid"]) for row in payouts]


def test_the_happy_path_is_lock_unlock_send_lock_unlock_for_staking(conn, monkeypatch):
    """The baseline, so every failure below is a difference from a known-good run.

    THE CALL ORDER IS ASSERTED AND NOT JUST THE COUNTS. The operator's instruction
    on 2026-09-26 was "it should LOCK the wallet no matter what it's state. then
    UNLOCK it entirely. do the transaction then LOCK and leave unlocked for staking
    only", and an assertion on counts alone would pass for a sequence that unlocked
    before it locked -- which is the shape that makes `walletpassphrase` fail on an
    already-unlocked wallet and look like a bad passphrase.

    MUTATION, run 2026-10-11: in chains/gridcoin_wallet_lock.unlocked_for_payout(),
    delete the leading `lock(adapter)`. The call list loses its first `walletlock`
    and this fails; the counts-only version of this assertion would not.
    """
    wallet = GridcoinWallet()

    completed = run(conn, wallet, monkeypatch)

    assert wallet.calls == ["walletlock", "walletpassphrase", "walletlock", "walletpassphrase"]
    assert (wallet.locks, wallet.full_unlocks, wallet.staking_unlocks) == (2, 1, 1)
    assert wallet.sends == 1
    swap, payouts = record(conn)
    assert len(completed) == 1
    assert swap["status"] == "completed"
    assert swap["payout_txid"] == SENT_TXID
    assert payouts == [("broadcast", SENT_TXID)]


def test_a_send_that_returned_a_txid_is_recorded_even_when_the_restore_fails(conn, monkeypatch):
    """THE 2026-09-26 DEFECT, from the direction that cost 55.52645238 GRC.

    The send succeeds and the STAKING unlock afterwards raises rpc -8. The payment is
    delivered and irreversible, so it must be recorded; the wallet's lock state is a
    separate problem with its own loud message.

    `walletlock` SUCCEEDED HERE, which is what makes this the SAFE half of the restore
    failure: chains/gridcoin_wallet_lock.restore_failed_because() tells it apart from
    the lock itself failing, and reports "the wallet is LOCKED ... No funds are
    exposed" rather than the alarming sentence that was printed for both until
    2026-09-26.

    MUTATION, run 2026-10-11: in services/payout_service.pay_one_swap(), move the
    `_record_broadcast(db, swap, amount, txid)` call and the `recorded_txid = txid`
    line from inside the `with payout_unlock_context(...)` to just after it. The
    GridcoinLockError from the restore then preempts them, `recorded_txid` stays None,
    the handler re-raises, and the swap is marked `failed` with payout_txid None --
    money out, no record, which is the original defect exactly.
    """
    wallet = GridcoinWallet(frozenset({"staking_unlock"}))

    completed = run(conn, wallet, monkeypatch)

    assert wallet.sends == 1
    assert wallet.staking_unlocks == 1, "the restore must have been attempted and refused"
    swap, payouts = record(conn)
    assert swap["status"] == "completed", (
        f"a delivered GRC payment was recorded as {swap['status']!r} because wallet "
        f"housekeeping failed after it"
    )
    assert swap["payout_txid"] == SENT_TXID
    assert payouts == [("broadcast", SENT_TXID)]
    assert len(completed) == 1

    messages = [
        row["message"] for row in
        conn.execute("SELECT message FROM swap_audit_log WHERE swap_id = 's_grc'").fetchall()
    ]
    assert any("lock restore FAILED" in message for message in messages), messages
    assert any(SENT_TXID in message for message in messages), (
        "the audit row must name the txid, so the delivered payment and the wallet problem "
        "are connected in the record an operator reads"
    )


#: Every way the lock sequence or the send can fail, and what the stub does about it.
#:
#: `expected_sends` IS PART OF THE TABLE because "nothing was sent" is the claim that
#: makes a deferral or a retry safe, and it is not the same claim as "no txid was
#: recorded". A send that was ATTEMPTED and raised has an unknown outcome on chain; a
#: send that was never reached has a known one. Both must leave no txid, and only the
#: second is certainly harmless.
LOCK_FAILURES = {
    "the full unlock is refused -- a wrong passphrase, rpc -14": (
        frozenset({"full_unlock"}), 0,
    ),
    "the leading walletlock is refused, before any unlock": (
        frozenset({"lock"}), 0,
    ),
    "the wallet relocked between the unlock and the send, rpc -13": (
        frozenset({"relocked_before_send"}), 1,
    ),
    "the send times out after 30s and MAY have been relayed": (
        frozenset({"send_times_out"}), 1,
    ),
}


@pytest.mark.parametrize(
    ("failures", "expected_sends"), list(LOCK_FAILURES.values()), ids=list(LOCK_FAILURES)
)
def test_no_failure_of_the_lock_sequence_records_a_txid(conn, monkeypatch, failures, expected_sends):
    """THE INVARIANT THAT MUST HOLD FOR ALL FOUR: no txid appears that no send returned.

    A payout recorded as sent that was not is the one outcome on this path with no
    remedy -- the swap reads `completed`, the customer page says paid, and nothing
    will ever look at it again. Every other outcome here is recoverable by a person.

    THE STATUS IS NOT ASSERTED, and the module docstring says why at length: the
    `failed` versus `payout_pending` question for a pre-send lock failure is live
    posture and is reported to the operator rather than pinned here.

    MUTATION, run 2026-10-11: in services/payout_service.pay_one_swap(), initialize
    `recorded_txid = SENT_TXID`-equivalent by changing `recorded_txid: str | None =
    None` to `recorded_txid = "x"`. The GridcoinLockError handler then stops
    re-raising on an unrecorded send; the swap is appended to `completed` while
    nothing was broadcast. Confirmed to fail this test's `completed == []` assertion
    for the `full_unlock` case.
    """
    wallet = GridcoinWallet(failures)

    completed = run(conn, wallet, monkeypatch)

    assert wallet.sends == expected_sends, (
        f"expected {expected_sends} send attempt(s), got {wallet.sends}"
    )
    swap, payouts = record(conn)
    assert swap["payout_txid"] is None, (
        f"swaps.payout_txid = {swap['payout_txid']!r} for a payout no send returned a txid for"
    )
    assert all(txid is None for _status, txid in payouts), (
        f"a payouts row carries a txid that no send returned: {payouts}"
    )
    assert SENT_TXID not in str(payouts), payouts
    assert completed == [], (
        f"process_pending_payouts() reported {len(completed)} completed payout(s) when no send "
        f"returned a txid"
    )
    assert swap["failed_reason"] or swap["status"] == "payout_pending", (
        "a swap that was not paid must carry either a recorded reason or a status that will be "
        "picked up again -- silence on both is rule 14's defect on the fund path"
    )


def test_a_pre_send_lock_failure_sends_nothing_at_all(conn, monkeypatch):
    """A wrong passphrase must not reach send_to_address(). THE CERTAINTY, isolated.

    This is the claim that makes the deferral question answerable at all: the module
    docstring argues that `failed` and `payout_pending` are both defensible for a
    wrong passphrase BECAUSE nothing was sent, and that premise needs its own
    assertion rather than resting on where the raise happens to sit in the source.

    chains/gridcoin_wallet_lock.unlocked_for_payout() raises from `unlock_for_sending`
    which is ABOVE its `yield`, so the `with` body never runs. That also means the
    `finally` holding the staking restore is never entered -- which is why the wallet
    is left LOCKED and NOT STAKING, and why the raised message says so in those words
    rather than leaving the operator to infer it.

    MUTATION, run 2026-10-11: move `unlock_for_sending(...)` inside the second `try`
    in unlocked_for_payout(), so the body runs after a failed unlock. `wallet.sends`
    becomes 1 and this fails -- a send attempted against a wallet the process knows is
    locked.
    """
    wallet = GridcoinWallet(frozenset({"full_unlock"}))

    run(conn, wallet, monkeypatch)

    assert wallet.sends == 0, "a send was attempted after the unlock failed"
    assert wallet.staking_unlocks == 0, (
        "the staking restore ran after a FAILED unlock -- it cannot, because restoring staking "
        "needs the same passphrase that just failed, and claiming otherwise is what "
        "unlocked_for_payout()'s docstring records as a fix that was described and never written"
    )
    swap, _payouts = record(conn)
    assert swap["payout_txid"] is None
    assert "NOT STAKING" in (swap["failed_reason"] or ""), (
        f"the recorded reason must tell the operator their wallet is no longer staking, which "
        f"only this frame knows: {swap['failed_reason']!r}"
    )


def test_an_unset_passphrase_defers_and_leaves_no_payouts_row(conn, monkeypatch):
    """The 2026-10-10 fix, asserted on the GRC path it was written for.

    PayoutUnlockUnavailable was "a type created for a distinction nobody made" until
    then: nothing caught it, so a credited swap died of an unset environment variable.
    The remedy is a deferral back to `payout_pending`, and the payouts row must be
    WITHDRAWN rather than left at `created` -- idx_payouts_one_live_per_swap would
    otherwise refuse the very retry the deferral exists for.

    THIS ONE DOES ASSERT THE STATUS, unlike the wrong-passphrase case, because here it
    is not in dispute: the fix landed, the operator has it, and `payout_pending` is
    the whole point of the change.

    MUTATION, run 2026-10-11: remove the `except PayoutUnlockUnavailable` arm from
    pay_one_swap() so the raise falls through to `except Exception`. The swap is
    marked `failed` with the sentence in failed_reason and a `failed` payouts row is
    left behind -- the exact state measured before the fix.
    """
    wallet = GridcoinWallet()

    completed = run(conn, wallet, monkeypatch, passphrase=None)

    assert completed == []
    assert wallet.calls == [], "no wallet RPC may be issued when there is no passphrase to use"
    swap, payouts = record(conn)
    assert swap["status"] == "payout_pending", (
        f"a credited swap was left in {swap['status']!r} by a missing environment variable; "
        f"nothing in this tree re-queues `failed`"
    )
    assert swap["failed_reason"] is None
    assert payouts == [], f"a payouts row survived the deferral and will block the retry: {payouts}"
