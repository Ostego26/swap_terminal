"""Does this wallet need unlocking before it can sign, and how is it put back?

Role: function level (the decision: wallet_is_encrypted; and the dispatch around it)
Reads: the daemon's `getwalletinfo`, through the adapter it is handed. Nothing else.
Writes: nothing to disk. It CALLS walletlock/walletpassphrase on an encrypted wallet.
Can move funds: no. It opens and closes the door; the caller walks through it.
Live-safe: yes for the read. The lock calls change wallet state and are the caller's
       decision -- see unlocked_for_payout below, which always restores.

WHY THIS EXISTS, MEASURED 2026-09-29. atomic_swap_xrp.py gained --chain btc|ltc|grc and
called chains/gridcoin_wallet_lock.unlocked_for_payout() on whichever adapter --chain
selected. That routine is Gridcoin-shaped end to end: lock, full unlock, body, lock,
then RE-UNLOCK FOR STAKING. Two things go wrong pointed at bitcoind:

  - Bitcoin and Litecoin do not stake, so the restore step has no counterpart and the
    final unlock_for_staking() call is asking for something the daemon has never had.
  - A wallet that is not encrypted at all has no passphrase, and `walletlock` on one
    answers with an error rather than a no-op. A fresh regtest bitcoind wallet is the
    ordinary case of this, which is exactly the wallet somebody reaches for first.

The driver also REFUSED to start unless <CHAIN>_WALLET_PASSPHRASE was set -- demanding a
secret that does not exist for an unencrypted wallet, which is a refusal an operator
cannot satisfy and cannot diagnose.

THE DECISION IS ONE FIELD AND IT IS NOT A GUESS. Bitcoin-derived daemons report
`unlocked_until` in getwalletinfo ONLY for an encrypted wallet; an unencrypted one omits
the key entirely rather than reporting 0. So presence, not value, is the test -- a
value-based test reads an unlocked-forever wallet (0 meaning locked) and an unencrypted
one identically, and those want opposite handling.

WHEN THE ANSWER CANNOT BE OBTAINED, ASSUME ENCRYPTED. That is the pre-2026-09-29
behaviour and it is the safe direction: assuming encrypted asks for a passphrase that may
be unnecessary and refuses loudly, while assuming unencrypted skips a lock and can leave
a real wallet open. One of those costs a support question and the other costs coins.
"""

from __future__ import annotations

import contextlib
import logging

from chains import gridcoin_wallet_lock

logger = logging.getLogger(__name__)

#: The getwalletinfo key a Bitcoin-derived daemon reports ONLY when the wallet is
#: encrypted. Its ABSENCE is what says "no passphrase exists"; its value says whether an
#: encrypted wallet happens to be unlocked right now, which is a different question.
ENCRYPTION_FIELD = "unlocked_until"

#: The chain whose wallet must be handed back STAKING, not merely locked. Gridcoin stakes
#: from an unlocked wallet, so a plain re-lock after a payout silently stops it earning --
#: which is why gridcoin_wallet_lock.py exists and why this module delegates rather than
#: reimplementing it (rule 8).
STAKING_CHAINS = frozenset({"GRC"})


def wallet_is_encrypted(wallet_info: object) -> bool:
    """Does this getwalletinfo response describe a wallet with a passphrase?

    Takes the RESPONSE rather than the adapter so it can be called with a seeded dict
    (rule 10) -- the whole point of separating it is that this is the decision, and a
    decision that needs a daemon to exercise is a decision nobody tests.

    A non-dict answers False rather than raising: a daemon that returns something
    unexpected has not told us there is a passphrase, and the caller's own failure to
    read it is handled where the read happens, not here.
    """
    return isinstance(wallet_info, dict) and ENCRYPTION_FIELD in wallet_info


def encryption_state(adapter) -> tuple[bool, str]:
    """(needs a passphrase, the sentence saying why we think so). Never raises.

    Returns the REASON alongside the verdict because this decides whether the driver
    refuses to start, and a refusal an operator cannot explain is one they route around
    (rule 14: state what the answer means, next to the answer).
    """
    try:
        info = adapter.call("getwalletinfo")
    except Exception as error:  # noqa: BLE001 -- checked: EVERY failure here (method absent on an older daemon, wallet not loaded, connection refused) means the same thing to this caller: we could not establish that the wallet is unencrypted. It returns True, the SAFE direction, and says which in the sentence -- the caller cannot mistake this for a measured answer.
        return True, (
            f"could not read getwalletinfo ({type(error).__name__}: {error}), so this assumes "
            f"the wallet IS encrypted. That is the safe direction: asking for a passphrase "
            f"that turns out to be unnecessary costs a question, while skipping a lock on a "
            f"wallet that has one can leave it open"
        )
    if wallet_is_encrypted(info):
        return True, f"getwalletinfo reports {ENCRYPTION_FIELD}, so the wallet is encrypted"
    return False, (
        f"getwalletinfo does not report {ENCRYPTION_FIELD}, so this wallet has no passphrase "
        f"and nothing will be locked or unlocked around the payout"
    )


@contextlib.contextmanager
def unlocked_for_payout(adapter, passphrase: str, *, chain: str, encrypted: bool = True):
    """Open the wallet for one payout and ALWAYS put it back the way it was.

    Three shapes, and which one runs is decided before the body, never inside it:

      not encrypted      nothing at all. No walletlock, no walletpassphrase. Calling
                         either on an unencrypted wallet is an error, not a no-op.
      encrypted, GRC     delegated to chains/gridcoin_wallet_lock.unlocked_for_payout,
                         which restores STAKING rather than merely locking. That module
                         owns the concept and keeps owning it (rule 8).
      encrypted, other   lock, unlock for sending, body, lock. The lock/unlock calls
                         themselves are gridcoin_wallet_lock's -- they are plain
                         walletlock/walletpassphrase and every Bitcoin-derived daemon
                         has them; only the staking restore was ever Gridcoin-specific.

    The restore runs whether the body returned, raised, or was interrupted, for the
    reason the Gridcoin module gives at length: `finally` covers KeyboardInterrupt and
    SystemExit, and an operator pressing Ctrl-C during a slow payout is exactly when a
    wallet gets left open.
    """
    if not encrypted:
        logger.info("%s wallet: not encrypted, so nothing is locked or unlocked", chain)
        yield
        return
    if chain in STAKING_CHAINS:
        with gridcoin_wallet_lock.unlocked_for_payout(adapter, passphrase):
            yield
        return
    gridcoin_wallet_lock.lock(adapter)
    gridcoin_wallet_lock.unlock_for_sending(adapter, passphrase)
    try:
        yield
    finally:
        # NO STAKING RESTORE, and that is the difference this whole module exists for.
        # Handing a Bitcoin wallet back "unlocked for staking" would leave it open
        # indefinitely for a capability the daemon does not have.
        gridcoin_wallet_lock.lock(adapter)
