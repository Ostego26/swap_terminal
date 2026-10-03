#!/usr/bin/env python3
"""Deriving a deposit address does the lock cycle too, not just the payout.

Role: tests (read-only)
Reads: services/swap_service.derive_deposit_address() and
      chains/gridcoin_wallet_lock.needs_wallet_unlock(), against a stub wallet.
      No network, no daemon, no database.
Writes: nothing
Can move funds: no
Mainnet-safe: yes

OPERATOR INSTRUCTION 2026-10-03: "on our end the terminal it will have to be able to
lock-unlock-lock-return to staking unlock our OWN hot wallet."

THE CYCLE ALREADY EXISTED AND ONLY WRAPPED THE SEND.
chains/gridcoin_wallet_lock.unlocked_for_payout() does exactly lock -> unlock past
staking -> body -> lock -> unlock for staking, with the restore in a `finally`. But
services/payout_service.py was its only caller, so it covered the wallet write that
PAYS a customer and not the one that takes their deposit.

DERIVING AN ADDRESS IS ALSO A WALLET WRITE. On a Bitcoin-derived daemon
`getnewaddress` succeeds while the keypool holds spare keys and fails with
WALLET_UNLOCK_NEEDED (rpc -13) once it must top the pool up. So an encrypted wallet
unlocked only for staking creates swaps fine until the keypool empties and then
stops -- with a customer on the page. The operator's wallet has not hit it because
their keypool is not exhausted; that is luck with a deadline, not a design.

NOT MEASURED AGAINST A GRIDCOIN DAEMON, said rather than implied: this container has
no Gridcoin node. The code and message are what the Bitcoin family documents and
Gridcoin inherits. needs_wallet_unlock() is therefore deliberately generous -- a
false negative means a swap fails for a reason the operator already solved, while a
false positive costs one unlock cycle.
"""

from __future__ import annotations

import pytest
from chains.gridcoin_wallet_lock import needs_wallet_unlock
from services.swap_service import derive_deposit_address
from valid_addresses import GRC_PAYOUT

#: DERIVED FROM tests/valid_addresses.py, NOT WRITTEN AS A LITERAL. The first
#: version pasted the operator's real deposit address
#: (mzuEXZ...) and tests/test_address_literals_are_valid.py's ceiling caught it at
#: 61 against 60 -- a ratchet doing exactly its job. Its message says why: "a
#: derived address cannot be mistyped and says what it is for". Fixing it with a
#: raised ceiling would have been rule 19's forbidden move on the day the ratchet
#: earned its keep.
DERIVED = GRC_PAYOUT
LOCKED_ERROR = "Error: Please enter the wallet passphrase with walletpassphrase first (code -13)"


class Wallet:
    """A Gridcoin wallet that refuses getnewaddress while locked, and records the calls.

    A STUB RATHER THAN A MOCK, and it records the SEQUENCE, because the sequence is
    the thing under test: an unlock that is not followed by a re-lock leaves the hot
    wallet open for sending, which is the outcome the operator's own instruction is
    about ("return to staking").
    """

    def __init__(self, fail_times=1):
        self.calls: list[tuple] = []
        self.locked = True
        self.fail_times = fail_times

    def get_new_address(self, label):
        self.calls.append(("getnewaddress", label))
        if self.locked and self.fail_times:
            self.fail_times -= 1
            raise RuntimeError(LOCKED_ERROR)
        return DERIVED

    def call(self, method, *params):
        self.calls.append((method, params))
        if method == "walletlock":
            self.locked = True
        if method == "walletpassphrase":
            self.locked = False

    def methods(self):
        return [name for name, _params in self.calls]


def test_a_LOCKED_wallet_derives_after_the_full_lock_cycle(monkeypatch):
    """The sequence, asserted in order. MEASURED 2026-10-03 as exactly this.

    MUTATION: drop the retry and let the first failure propagate. The swap cannot be
    created at all once the keypool empties, which is the latent failure this closes.
    """
    monkeypatch.setenv("GRIDCOIN_WALLET_PASSPHRASE", "not-a-real-passphrase")
    wallet = Wallet()
    assert derive_deposit_address(wallet, "GRC", "s_demo") == DERIVED

    assert wallet.methods() == [
        "getnewaddress",     # tried first -- see derive_deposit_address() for why
        "walletlock",        # lock
        "walletpassphrase",  # unlock PAST staking
        "getnewaddress",     # derive
        "walletlock",        # lock again
        "walletpassphrase",  # return to staking
    ], wallet.calls

    # THE LAST UNLOCK MUST BE THE STAKING ONE. A cycle that ended on the full unlock
    # would leave the hot wallet able to SEND, indefinitely, after every swap
    # creation -- strictly worse than never unlocking at all.
    final_unlock = [params for name, params in wallet.calls if name == "walletpassphrase"][-1]
    assert final_unlock[-1] is True, (
        f"the last walletpassphrase did not pass stakingonly=True, so the wallet is left unlocked for "
        f"SENDING after deriving an address: {final_unlock}"
    )
    assert wallet.locked is False, "the wallet should end unlocked FOR STAKING, not locked"


def test_an_UNLOCKED_wallet_does_NOT_touch_the_lock_at_all(monkeypatch):
    """Try first, unlock only if the wallet says so.

    Unlocking on every swap creation would hold the hot wallet open for sending on a
    path that only reads a key out of a pool, for every customer who loads the form.
    One failed RPC is the cheaper trade and this pins it.

    MUTATION: unlock unconditionally. This fails on the first assertion, and the
    wallet is opened for sending on every swap.
    """
    monkeypatch.setenv("GRIDCOIN_WALLET_PASSPHRASE", "not-a-real-passphrase")
    wallet = Wallet(fail_times=0)
    wallet.locked = False
    assert derive_deposit_address(wallet, "GRC", "s_demo") == DERIVED
    assert wallet.methods() == ["getnewaddress"], (
        f"the lock was touched for a wallet that answered getnewaddress fine: {wallet.calls}"
    )


def test_a_NON_LOCK_failure_propagates_UNCHANGED(monkeypatch):
    """Only the lock gets a retry. Everything else must stay a failure.

    A refused connection, a bad label and a malformed response are not things this
    process can fix and then re-attempt, and retrying them inside an unlock would
    open the hot wallet in response to an unrelated fault. Rule 12's BLE001 note is
    why this is not `except Exception: retry`.
    """
    monkeypatch.setenv("GRIDCOIN_WALLET_PASSPHRASE", "not-a-real-passphrase")

    class Broken(Wallet):
        def get_new_address(self, label):
            self.calls.append(("getnewaddress", label))
            raise ConnectionRefusedError("[Errno 111] Connection refused")

    wallet = Broken()
    with pytest.raises(ConnectionRefusedError):
        derive_deposit_address(wallet, "GRC", "s_demo")
    assert wallet.methods() == ["getnewaddress"], (
        f"the wallet lock was touched in response to a connection failure: {wallet.calls}"
    )


def test_LOCKED_with_no_passphrase_raises_THE_DAEMONS_error_plus_what_to_set(monkeypatch):
    """The operator needs to see what the daemon said, not a complaint about a variable.

    The missing variable is NAMED in the message, appended to the daemon's own text,
    and the original is chained -- so neither the cause nor the remedy is lost.
    """
    monkeypatch.delenv("GRIDCOIN_WALLET_PASSPHRASE", raising=False)
    wallet = Wallet()
    with pytest.raises(RuntimeError) as raised:
        derive_deposit_address(wallet, "GRC", "s_demo")
    message = str(raised.value)
    assert "walletpassphrase first" in message, "the daemon's own error was replaced rather than kept"
    assert "GRIDCOIN_WALLET_PASSPHRASE is not set" in message, message
    assert "keypool is exhausted" in message, "nothing explains WHY an address needs an unlocked wallet"
    assert raised.value.__cause__ is not None, "the original error was not chained"


def test_a_chain_with_no_lock_at_all_is_never_unlocked(monkeypatch):
    """BTC and LTC are not in WALLET_UNLOCK_ASSETS, so their failures just propagate."""
    monkeypatch.setenv("GRIDCOIN_WALLET_PASSPHRASE", "not-a-real-passphrase")
    wallet = Wallet()
    with pytest.raises(RuntimeError):
        derive_deposit_address(wallet, "BTC", "s_demo")
    assert wallet.methods() == ["getnewaddress"], (
        f"a BTC derivation ran Gridcoin's lock cycle: {wallet.calls}"
    )


@pytest.mark.parametrize(("text", "expected"), [
    (LOCKED_ERROR, True),
    ("Error: Please enter the wallet passphrase with walletpassphrase first.", True),
    ("WALLET_UNLOCK_NEEDED", True),
    ("[Errno 111] Connection refused", False),
    ("Invalid Gridcoin address", False),
    ("", False),
])
def test_needs_wallet_unlock_recognizes_the_lock_and_nothing_else(text, expected):
    """The recognizer, as a function, callable with a seeded error (rule 10).

    The alternative is a substring test inlined at the call site, which cannot be
    asserted on without provoking a real locked daemon.
    """
    assert needs_wallet_unlock(RuntimeError(text)) is expected, text
