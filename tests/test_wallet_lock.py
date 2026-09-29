"""Whether a wallet needs unlocking, and what is called when it does.

Role: test
Reads: nothing. Every adapter here is a recorder.
Writes: nothing.
Can send orders: no
Live-safe: yes

WHY THIS FILE EXISTS. atomic_swap_xrp.py gained --chain btc|ltc|grc on 2026-09-29 and
kept calling chains/gridcoin_wallet_lock.unlocked_for_payout() on whichever adapter the
flag selected. That routine ends by RE-UNLOCKING FOR STAKING, which Bitcoin and Litecoin
have no counterpart for, and it calls walletlock on wallets that may not be encrypted at
all -- where that is an error rather than a no-op.

Neither failure was reachable while the driver was Gridcoin-only, and neither would have
been caught by the suite: the swap tests never reach a wallet.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "swap_terminal"))

from chains import gridcoin_wallet_lock
from chains.wallet_lock import (
    ENCRYPTION_FIELD,
    encryption_state,
    unlocked_for_payout,
    wallet_is_encrypted,
)

UNLOCK = "walletpassphrase"
LOCK = "walletlock"


class Recorder:
    """An adapter that records calls. `info` is what getwalletinfo answers."""

    def __init__(self, info=None, fail_with: Exception | None = None):
        self.info, self.fail_with, self.calls = info, fail_with, []

    def call(self, method, *params):
        self.calls.append((method, params))
        if method == "getwalletinfo":
            if self.fail_with is not None:
                raise self.fail_with
            return self.info
        return None

    def methods(self):
        return [method for method, _ in self.calls]


# ---------------------------------------------------------------------------
# The decision. PRESENCE of the field, never its value.
# ---------------------------------------------------------------------------
def test_an_encrypted_wallet_reports_the_field():
    assert wallet_is_encrypted({ENCRYPTION_FIELD: 0, "walletname": "w"}) is True


def test_an_UNLOCKED_encrypted_wallet_is_still_encrypted():
    """unlocked_until=0 means LOCKED and a future timestamp means unlocked. Both are
    encrypted wallets, and a value-based test would call one of them unencrypted."""
    assert wallet_is_encrypted({ENCRYPTION_FIELD: 1893456000}) is True


def test_an_unencrypted_wallet_OMITS_the_field_rather_than_reporting_zero():
    """THE WHOLE REASON THE TEST IS ON PRESENCE. A Bitcoin-derived daemon leaves
    unlocked_until out entirely when there is no passphrase -- it does not report 0. A
    test written against the value reads an unencrypted wallet and a locked encrypted one
    identically, and those want opposite handling: one must be left alone, the other must
    be unlocked and put back."""
    assert wallet_is_encrypted({"walletname": "regtest", "balance": 50.0}) is False


@pytest.mark.parametrize("answer", [None, "", [], 0])
def test_a_response_that_is_not_a_dict_is_not_evidence_of_a_passphrase(answer):
    assert wallet_is_encrypted(answer) is False


# ---------------------------------------------------------------------------
# Reading it off a live adapter, including when the read fails.
# ---------------------------------------------------------------------------
def test_a_failed_getwalletinfo_ASSUMES_ENCRYPTED_and_says_it_is_assuming():
    """THE SAFE DIRECTION, AND IT HAS TO BE LEGIBLE AS AN ASSUMPTION.

    Assuming encrypted asks for a passphrase that may be unnecessary and refuses loudly.
    Assuming unencrypted skips a lock and can leave a real wallet open. One costs a
    support question; the other costs coins.

    Rule 17: the sentence must not read like a measurement. It says "assumes".
    """
    encrypted, why = encryption_state(Recorder(fail_with=RuntimeError("method not found")))
    assert encrypted is True
    assert "assumes" in why and "RuntimeError" in why


def test_an_unencrypted_wallet_is_reported_as_needing_nothing():
    encrypted, why = encryption_state(Recorder({"walletname": "regtest"}))
    assert encrypted is False
    assert ENCRYPTION_FIELD in why and "no passphrase" in why


# ---------------------------------------------------------------------------
# What actually gets called around the payout.
# ---------------------------------------------------------------------------
def test_AN_UNENCRYPTED_WALLET_IS_NEVER_LOCKED_OR_UNLOCKED():
    """THE DEFECT THIS PINS. walletlock against an unencrypted wallet is an ERROR, not a
    no-op, so the pre-2026-09-29 path would have failed on the ordinary regtest bitcoind
    wallet -- after the XRP escrow was already funded, which is the expensive half."""
    adapter = Recorder({"walletname": "regtest"})
    with unlocked_for_payout(adapter, "", chain="BTC", encrypted=False):
        pass
    assert adapter.calls == [], f"an unencrypted wallet was sent {adapter.methods()}"


def test_an_encrypted_BITCOIN_wallet_is_locked_unlocked_and_LOCKED_AGAIN():
    adapter = Recorder({ENCRYPTION_FIELD: 0})
    with unlocked_for_payout(adapter, "phrase", chain="BTC", encrypted=True):
        adapter.call("sendrawtransaction", "00")
    assert adapter.methods() == [LOCK, UNLOCK, "sendrawtransaction", LOCK]


def test_a_BITCOIN_wallet_is_NOT_handed_back_unlocked_for_staking():
    """THE GRIDCOIN-SHAPED STEP, and the one that must not run anywhere else.

    gridcoin_wallet_lock's restore ends with `walletpassphrase <phrase> <a year> true` --
    a staking-only unlock, which is Gridcoin's RESTING state. Bitcoin does not stake, so
    that call asks for a capability the daemon has never had, and its effect if accepted
    would be a wallet left open for a year.

    MUTATION: route BTC through gridcoin_wallet_lock.unlocked_for_payout and this fails
    on the trailing walletpassphrase.
    """
    adapter = Recorder({ENCRYPTION_FIELD: 0})
    with unlocked_for_payout(adapter, "phrase", chain="BTC", encrypted=True):
        pass
    assert adapter.methods()[-1] == LOCK, "a non-staking chain must end LOCKED"
    assert adapter.methods().count(UNLOCK) == 1, (
        "two unlocks means the staking restore ran on a chain that does not stake"
    )


def test_GRIDCOIN_still_gets_its_staking_restore():
    """The delegation, asserted rather than assumed: GRC keeps the behaviour it had, so
    a payout does not silently stop the wallet earning."""
    adapter = Recorder({ENCRYPTION_FIELD: 0})
    with unlocked_for_payout(adapter, "phrase", chain="GRC", encrypted=True):
        pass
    assert adapter.methods().count(UNLOCK) == 2, "GRC must be re-unlocked for staking"
    assert adapter.calls[-1][0] == UNLOCK
    assert adapter.calls[-1][1][-1] is True, "the staking-only flag must be the last argument"


def test_THE_WALLET_IS_PUT_BACK_EVEN_WHEN_THE_BODY_RAISES():
    """A payout that throws mid-flight is exactly when a wallet gets left open."""
    adapter = Recorder({ENCRYPTION_FIELD: 0})
    with (pytest.raises(ValueError, match="broadcast failed"),
          unlocked_for_payout(adapter, "phrase", chain="BTC", encrypted=True)):
        raise ValueError("broadcast failed")
    assert adapter.methods()[-1] == LOCK


def test_the_passphrase_is_never_the_first_argument_of_a_lock():
    """A shape check, because the two calls take different arguments and swapping them
    would send the passphrase to walletlock -- which logs its parameters on some daemons."""
    adapter = Recorder({ENCRYPTION_FIELD: 0})
    with unlocked_for_payout(adapter, "phrase", chain="BTC", encrypted=True):
        pass
    for method, params in adapter.calls:
        if method == LOCK:
            assert params == (), f"walletlock was sent arguments: {params}"


def test_gridcoin_wallet_lock_is_still_the_one_implementation():
    """Rule 8: this module DISPATCHES, it does not reimplement. If a second walletlock
    call ever appears here, the two will agree on the day it is written and drift after."""
    source = Path(__file__).resolve().parent.parent / "swap_terminal" / "chains" / "wallet_lock.py"
    text = source.read_text()
    assert '"walletlock"' not in text and '"walletpassphrase"' not in text, (
        "wallet_lock.py spells an RPC method itself; gridcoin_wallet_lock.py owns those"
    )
    assert gridcoin_wallet_lock.lock.__module__ == "chains.gridcoin_wallet_lock"
