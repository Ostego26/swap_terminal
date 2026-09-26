"""The Gridcoin payout lock sequence, and that the wallet is always put back.

Role: test (a recording fake adapter; opens no socket, touches no wallet)
Reads: chains/gridcoin_wallet_lock.py
Writes: nothing
Can move funds: no
Mainnet-safe: yes

The sequence is the operator's, stated 2026-09-26: "it should LOCK the wallet no
matter what it's state. then UNLOCK it entirely. do the transaction then LOCK and
leave unlocked for staking only."

Every test here asserts on the RECORDED CALL SEQUENCE rather than on a return
value, because the order is the behavior. A version that made all the right calls
in the wrong order would satisfy any per-call assertion.
"""

import logging
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "swap_terminal"))

from chains.gridcoin_wallet_lock import (
    DEFAULT_UNLOCK_SECONDS,
    STAKING_UNLOCK_SECONDS,
    GridcoinLockError,
    unlocked_for_payout,
)

# NOT a credential, and named so the linter's S105 is answered rather than
# suppressed. It is a SENTINEL: test_the_passphrase_never_reaches_a_log_line
# searches the captured log for this exact string, so its only purpose is to be
# findable. A value that looked like a real passphrase would make the leak test
# weaker, not stronger.
LEAK_SENTINEL = "gridcoin-lock-sequence-sentinel"


class RecordingAdapter:
    """Records (method, params) in order. Optionally fails a chosen method."""

    def __init__(self, fail_on=None, fail_after=0):
        self.calls = []
        self._fail_on = fail_on
        self._fail_after = fail_after

    def call(self, method, *params):
        self.calls.append((method, params))
        if method == self._fail_on:
            if self._fail_after > 0:
                self._fail_after -= 1
            else:
                raise RuntimeError(f"simulated {method} failure")
        return {}

    @property
    def methods(self):
        return [method for method, _params in self.calls]


def test_the_sequence_is_lock_unlock_body_lock_stake():
    """The operator's five steps, in their order, asserted as a sequence."""
    adapter = RecordingAdapter()
    body_ran_at = []

    with unlocked_for_payout(adapter, LEAK_SENTINEL):
        body_ran_at.append(len(adapter.calls))

    assert adapter.methods == ["walletlock", "walletpassphrase", "walletlock", "walletpassphrase"]
    assert body_ran_at == [2], "the body must run AFTER the full unlock and BEFORE the re-lock"


def test_the_first_lock_is_unconditional():
    """The part easiest to leave out, and the reason the operator said "no matter what".

    A staking wallet is normally ALREADY unlocked, and in Bitcoin-derived wallets
    walletpassphrase against an unlocked wallet is an error rather than a no-op --
    so unlocking without locking first succeeds or fails depending on a state the
    caller never set, and the failure looks like a bad passphrase. Locking first
    makes the unlock deterministic from any starting state.
    """
    adapter = RecordingAdapter()

    with unlocked_for_payout(adapter, LEAK_SENTINEL):
        pass

    assert adapter.methods[0] == "walletlock", "nothing may precede the lock, including a state check"


def test_the_full_unlock_omits_the_stakingonly_parameter():
    """The omission IS the full unlock. Passing it at all risks a daemon reading
    the flag's presence as the request."""
    adapter = RecordingAdapter()

    with unlocked_for_payout(adapter, LEAK_SENTINEL):
        pass

    _method, params = adapter.calls[1]
    assert params == (LEAK_SENTINEL, DEFAULT_UNLOCK_SECONDS), "two parameters only -- no third"


def test_the_restore_unlocks_for_staking_with_no_timeout():
    """The resting state: timeout 0 means until the wallet stops, stakingonly true."""
    adapter = RecordingAdapter()

    with unlocked_for_payout(adapter, LEAK_SENTINEL):
        pass

    _method, params = adapter.calls[3]
    assert params == (LEAK_SENTINEL, STAKING_UNLOCK_SECONDS, True)


def test_a_failing_body_still_locks_and_returns_to_staking():
    """THE one that matters. A wallet left fully unlocked is the real hazard.

    The moment it is most likely to happen is when the payout raises: the send
    fails, the exception propagates, and the unlock is never undone. Rule 13's
    shape applied to a lock rather than a process -- a re-lock that only runs on
    the happy path is not a re-lock.
    """
    adapter = RecordingAdapter()

    with pytest.raises(RuntimeError, match="the payout blew up"), unlocked_for_payout(adapter, LEAK_SENTINEL):
        raise RuntimeError("the payout blew up")

    assert adapter.methods == ["walletlock", "walletpassphrase", "walletlock", "walletpassphrase"]
    assert adapter.calls[3][1] == (LEAK_SENTINEL, STAKING_UNLOCK_SECONDS, True)


def test_a_keyboard_interrupt_still_restores():
    """An operator pressing Ctrl-C during a slow payout is exactly when a wallet
    gets left open. KeyboardInterrupt is a BaseException, so an `except Exception`
    around the body would have missed it -- `finally` does not.
    """
    adapter = RecordingAdapter()

    with pytest.raises(KeyboardInterrupt), unlocked_for_payout(adapter, LEAK_SENTINEL):
        raise KeyboardInterrupt

    assert adapter.methods[-2:] == ["walletlock", "walletpassphrase"]


def test_a_failed_restore_is_raised_loudly_and_names_the_manual_fix():
    """A silently-still-unlocked wallet is the worst outcome available.

    So a restore failure is its own exception type, and the message says what to
    run by hand rather than leaving the operator to work it out from a traceback.
    """
    # Let the first walletlock through; fail the second.
    adapter = RecordingAdapter(fail_on="walletlock", fail_after=1)

    with pytest.raises(GridcoinLockError, match="MAY STILL BE FULLY UNLOCKED"), \
            unlocked_for_payout(adapter, LEAK_SENTINEL):
        pass


def test_a_failed_restore_does_not_hide_the_bodys_exception():
    """Both failures matter, and `raise ... from` keeps the first one reachable."""
    adapter = RecordingAdapter(fail_on="walletlock", fail_after=1)

    with pytest.raises(GridcoinLockError) as caught, unlocked_for_payout(adapter, LEAK_SENTINEL):
        raise ValueError("the original payout failure")

    assert caught.value.__context__ is not None
    chain = []
    error = caught.value
    while error is not None:
        chain.append(str(error))
        error = error.__context__
    assert any("the original payout failure" in text for text in chain), (
        "the body's exception must remain reachable, not be replaced by the restore's"
    )


def test_the_passphrase_never_reaches_a_log_line(caplog):
    """An unlock is exactly where a secret leaks, because the parameters are the secret.

    Every log line in the module names the method and omits parameters, matching
    the redaction modules/htlc_rpc.py already applies to walletpassphrase
    parameter 0. Asserted over the captured log rather than by reading the source,
    because a format string is easy to change and a test is not.
    """
    caplog.set_level(logging.DEBUG)
    adapter = RecordingAdapter()

    with unlocked_for_payout(adapter, LEAK_SENTINEL):
        pass

    assert LEAK_SENTINEL not in caplog.text, "the passphrase must never be logged"
    assert "walletpassphrase" in caplog.text, "the method name is safe and should be logged"
