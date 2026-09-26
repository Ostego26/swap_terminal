"""The lock/unlock sequence a Gridcoin payout needs, with a guaranteed return to staking.

Role: submodule (wraps four wallet RPCs in one sequence; holds no decision of its own)
Reads: nothing from disk or the environment. The passphrase is an argument.
Writes: the wallet's LOCK STATE on the daemon. No file, no database.
Can send orders: no, not by itself -- it unlocks and re-locks. The caller's body
      is what sends, and this guarantees the wallet is put back regardless of
      whether that body succeeded, raised, or the process was interrupted mid-way.
Mainnet-safe: it does not care which chain it is pointed at, which is the reason
      the caller must. Unlocking a mainnet staking wallet is exactly as easy as
      unlocking a testnet one, and NOTHING here checks. Call it behind the same
      network classification everything else on the GRC path uses.

THE SEQUENCE, as the operator stated it 2026-09-26:

    "it should LOCK the wallet no matter what it's state. then UNLOCK it
     entirely. do the transaction then LOCK and leave unlocked for staking only."

So: lock -> full unlock -> body -> lock -> unlock for staking.

WHY THE FIRST LOCK IS UNCONDITIONAL, which is the part that is easy to leave out.
A Gridcoin wallet that stakes is normally already unlocked, for staking only. In
Bitcoin-derived wallets `walletpassphrase` against an already-unlocked wallet is
an ERROR rather than a no-op, so unlocking without locking first succeeds or
fails depending on a state the caller did not set and cannot see -- and the
failure looks like a bad passphrase. Locking first makes the unlock deterministic
from any starting state. (Gridcoin's exact behavior on that call was NOT measured
here -- no daemon is reachable from this environment -- so this follows the
operator's instruction and the Bitcoin convention, and is marked as such per rule
17.)

WHY THE RESTORE IS IN A `finally`, which is the part that matters most. A wallet
left fully unlocked is a security regression on a live wallet, and the moment it
is most likely to happen is when the payout raises: the send fails, the exception
propagates, and the unlock never gets undone. That is rule 13's shape applied to
a lock rather than a process -- every unlock needs its re-lock, and a re-lock that
only runs on the happy path is not one. The restore therefore runs on success, on
exception, and on KeyboardInterrupt, and a failure to restore is raised LOUDLY
rather than swallowed, because a silently-still-unlocked wallet is the worst
outcome available.

THE PASSPHRASE IS AN ARGUMENT AND IS NEVER STORED. Not read from the
environment here, not written to a file, not placed in argv, not logged. Every
log line in this module names the method and omits the parameters, matching the
redaction modules/htlc_rpc.py already applies to `walletpassphrase` parameter 0.
The caller decides where the passphrase comes from, which keeps that decision --
and its risk -- outside this module.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager

logger = logging.getLogger(__name__)

# `walletpassphrase <passphrase> <timeout> [stakingonly]`, from the wallet's own
# `help wallet` output, read 2026-09-26. The third parameter is the staking-only
# switch; omitting it is what produces a full unlock that can send.
_METHOD_UNLOCK = "walletpassphrase"
_METHOD_LOCK = "walletlock"

# Seconds the full unlock lasts. A payout is one sendtoaddress, so this is a
# ceiling for the whole sequence rather than a duration to fill, and it is
# deliberately short: it is the window in which a compromised process could spend
# the wallet. It is SECONDS because the RPC takes seconds -- rule 6's boundary,
# where an external API's unit is not converted to satisfy a display convention.
DEFAULT_UNLOCK_SECONDS = 60

# Timeout for the staking unlock. 0 means "until the wallet is stopped", which is
# the resting state a staking wallet is supposed to be left in.
STAKING_UNLOCK_SECONDS = 0


class GridcoinLockError(RuntimeError):
    """A lock, unlock or restore did not do what it was asked.

    Its own type because the restore failing is categorically worse than the
    payout failing, and a caller may want to tell them apart: a failed payout is
    a retry, while a failed restore means a wallet is sitting in a state the
    operator did not choose.
    """


def lock(adapter) -> None:
    """walletlock, unconditionally. Safe on an already-locked wallet."""
    logger.info("gridcoin wallet: %s (parameters omitted)", _METHOD_LOCK)
    adapter.call(_METHOD_LOCK)


def unlock_for_sending(adapter, passphrase: str, seconds: int = DEFAULT_UNLOCK_SECONDS) -> None:
    """Full unlock: `walletpassphrase <passphrase> <seconds>` with stakingonly OMITTED.

    The omission IS the full unlock. Passing the third parameter as False would
    also work on a wallet that accepts it, but omitting it cannot be misread and
    cannot be broken by a daemon that treats the flag's presence as the request.
    """
    logger.info("gridcoin wallet: %s for %ds (parameters omitted)", _METHOD_UNLOCK, seconds)
    adapter.call(_METHOD_UNLOCK, passphrase, seconds)


def unlock_for_staking(adapter, passphrase: str) -> None:
    """Staking-only unlock: the resting state. `walletpassphrase <passphrase> 0 true`."""
    logger.info("gridcoin wallet: %s stakingonly (parameters omitted)", _METHOD_UNLOCK)
    adapter.call(_METHOD_UNLOCK, passphrase, STAKING_UNLOCK_SECONDS, True)


@contextmanager
def unlocked_for_payout(adapter, passphrase: str, seconds: int = DEFAULT_UNLOCK_SECONDS):
    """lock -> full unlock -> [your body] -> lock -> unlock for staking. Always restores.

    Usage:

        with unlocked_for_payout(grc_adapter, passphrase):
            txid = grc_adapter.send_to_address(address, amount)

    The restore runs whether the body returned, raised, or was interrupted. If the
    restore ITSELF fails, that is raised as GridcoinLockError -- chained from the
    body's exception when there was one, so neither is lost. A wallet left in a
    state nobody chose must never be a silent outcome.
    """
    lock(adapter)
    unlock_for_sending(adapter, passphrase, seconds)
    try:
        yield
    finally:
        # BaseException-safe by construction: `finally` runs for
        # KeyboardInterrupt and SystemExit too, which a bare `except Exception`
        # around the body would not have caught. An operator pressing Ctrl-C
        # during a slow payout is exactly when a wallet gets left open.
        try:
            lock(adapter)
            unlock_for_staking(adapter, passphrase)
        except Exception as error:
            logger.error(
                "gridcoin wallet: FAILED to restore the lock state -- the wallet may still be "
                "fully unlocked. Lock it by hand: %s",
                error,
            )
            raise GridcoinLockError(
                "the payout sequence could not restore the wallet's lock state. THE WALLET MAY "
                "STILL BE FULLY UNLOCKED -- run walletlock, then walletpassphrase <passphrase> 0 "
                "true, by hand. Nothing here retries, because a retry that also fails would "
                "bury this message."
            ) from error
