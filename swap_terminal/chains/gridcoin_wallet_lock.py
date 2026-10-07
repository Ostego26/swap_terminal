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
import os
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

# Timeout for the staking unlock.
#
# THIS WAS 0, AND GRIDCOIN REFUSES 0. Measured on the operator's live testnet
# daemon 2026-09-26, on the first payout this code ever ran:
#
#     walletpassphrase <passphrase> 0 true
#     -> Timeout cannot be negative or zero. (rpc code -8)
#
# The comment here used to read: `0 means "until the wallet is stopped", which is
# the resting state a staking wallet is supposed to be left in.` That is Bitcoin
# Core's behavior, asserted about Gridcoin without ever being run against one --
# rule 17's "a reason to believe something is not the same as having checked it",
# and it was the ONLY one of the four calls in this sequence that had never touched
# a real daemon. The other three worked on the first try.
#
# THE NUMBER BELOW IS NOT MEASURED EITHER, and it is named as such (rule 16: a fix
# you cannot test here is a proposal). What IS measured is that 0 is rejected. One
# year of seconds is chosen because a staking wallet's resting state should outlast
# any session, and because a value this far from a boundary is unlikely to hit a
# second undocumented limit. If this daemon rejects it too, restore_failed_because()
# below says so by name and the wallet is left LOCKED, which is the safe direction.
#
# Overridable so the operator can correct it without a code change, since they are
# the one who can test it. Named _SECONDS, so it stays seconds (rule 6's boundary:
# the RPC takes seconds and converting at an external API's call site would put
# rounding into control flow).
STAKING_UNLOCK_SECONDS = int(os.environ.get("GRIDCOIN_STAKING_UNLOCK_SECONDS", "31536000"))


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
    """Staking-only unlock: the resting state. `walletpassphrase <phrase> <seconds> true`.

    The seconds are printed, because the value is the thing that was wrong once and
    an operator reading a log needs to see which number was sent.
    """
    logger.info(
        "gridcoin wallet: %s stakingonly for %ds (parameters omitted)",
        _METHOD_UNLOCK,
        STAKING_UNLOCK_SECONDS,
    )
    adapter.call(_METHOD_UNLOCK, passphrase, STAKING_UNLOCK_SECONDS, True)


#: Chains whose wallet must be FULLY UNLOCKED to write, and which are returned to a
#: staking unlock afterwards. Gridcoin is the only one.
#:
#: MOVED HERE FROM services/payout_service.py ON 2026-10-03, and the move is what
#: made the fix possible rather than a tidy-up. services/swap_service.py needs both
#: of these to do the same lock cycle around getnewaddress -- and payout_service
#: already imports swap_service, so importing back would be a cycle. They belong
#: below both services anyway: this module owns the lock concept, and dependencies
#: point downward (rule 10). payout_service re-exports them so its own callers and
#: tests are unchanged, which is one definition and no second spelling (rule 8).
WALLET_UNLOCK_ASSETS = frozenset({"GRC"})

#: THE NAME OF the environment variable, which is not itself a secret -- and naming
#: it WALLET_UNLOCK_ENV_VAR rather than ..._PASSPHRASE_VARIABLE is the honest fix for
#: ruff's S105 rather than a suppression (rule 19). The first spelling made a
#: constant holding a variable NAME look like a constant holding a passphrase, which
#: is precisely the confusion that lint rule exists to catch.
#:
#: Read from the environment and never from Config. Config is echoed on the admin
#: page through an allowlist, and a passphrase must not be one key away from
#: something that gets rendered.
WALLET_UNLOCK_ENV_VAR = "GRIDCOIN_WALLET_PASSPHRASE"


#: What a Bitcoin-derived daemon says when an operation needs the wallet unlocked.
#:
#: RPC code -13 is WALLET_UNLOCK_NEEDED, and the message is the one every fork
#: prints. Both are matched because a caller may see either the code (through a
#: structured RPC error) or only the text (through a wrapper that kept the message
#: and dropped the code) -- chains/base.rpc_error_from_body() returns a string, so
#: in this tree it is usually the text.
#:
#: NOT MEASURED AGAINST A GRIDCOIN DAEMON FROM HERE, and said rather than implied:
#: this container has no Gridcoin node. The code and the message are what the
#: Bitcoin family documents and what Gridcoin inherits; needs_wallet_unlock() is
#: deliberately generous, because a false NEGATIVE means a swap fails for a reason
#: the operator has already solved, while a false positive costs one unlock cycle.
WALLET_UNLOCK_NEEDED_CODE = -13
WALLET_UNLOCK_NEEDED_MARKERS = (
    "please enter the wallet passphrase",
    "wallet_unlock_needed",
    "walletpassphrase first",
    "-13",
)


def needs_wallet_unlock(error: Exception) -> bool:
    """Is this failure "the wallet is locked" rather than anything else?

    A FUNCTION SO IT CAN BE CALLED WITH A SEEDED ERROR (rule 10). The alternative
    is a substring test inlined at the call site, which cannot be asserted on
    without provoking a real locked daemon.

    Deliberately NOT a catch-all: a connection refused, a bad address and a
    malformed request must all stay failures. Only the lock gets a retry, because
    only the lock is something this process can fix and then re-attempt.
    """
    text = str(error).lower()
    return any(marker in text for marker in WALLET_UNLOCK_NEEDED_MARKERS)


def restore_failed_because(locked: bool, error: Exception) -> str:
    """The operator-facing sentence for a failed restore. Two OUTCOMES, not one.

    THE OLD MESSAGE SAID "THE WALLET MAY STILL BE FULLY UNLOCKED" FOR BOTH, AND ON
    2026-09-26 THAT WAS FALSE AND IT ALARMED THE OPERATOR. The restore is two calls
    in order -- walletlock, then the staking unlock. Their daemon logged the
    walletlock succeeding and then rejected the staking timeout, so the wallet was
    LOCKED: `getwalletinfo` read `unlocked_until 0` when they checked. The message
    had them hunting an exposure that did not exist, and the remedy it printed
    (`walletpassphrase <passphrase> 0 true`) was the exact call that had just
    failed.

    So the two cases are told apart by whether the walletlock got through:

      locked=False  the LOCK failed, so the full unlock may still be in force. This
                    is the real hazard and the wallet is spendable by this process
                    until the timeout expires.
      locked=True   the lock succeeded and only the staking unlock failed. The
                    wallet is SAFE and not staking. Different problem, different
                    urgency, and saying the first when it is the second is how an
                    instrument loses its reader.
    """
    if not locked:
        return (
            f"the payout sequence could not LOCK the wallet. IT MAY STILL BE FULLY UNLOCKED, and a full "
            f"unlock lasts {DEFAULT_UNLOCK_SECONDS}s from when it was granted -- run walletlock by hand "
            f"now, then restore staking the way you normally do. The daemon said: {error}"
        )
    rejected_timeout = "-8" in str(error) or "negative or zero" in str(error).lower()
    hint = (
        f" The daemon rejected the timeout: set GRIDCOIN_STAKING_UNLOCK_SECONDS to a positive number of "
        f"seconds it accepts (currently {STAKING_UNLOCK_SECONDS})."
        if rejected_timeout
        else ""
    )
    return (
        f"the wallet is LOCKED -- that call succeeded -- but the staking unlock did not, so it is NOT "
        f"staking. No funds are exposed. Restore staking the way you normally do.{hint} The daemon "
        f"said: {error}"
    )


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

    "ALWAYS RESTORES" IS FALSE FOR ONE PATH AND THAT PATH FIRED TWICE ON
    2026-10-07. The sentence above describes the body raising. It does NOT cover
    `unlock_for_sending` itself raising, because that call is one statement ABOVE
    the `try`, so the `finally` holding unlock_for_staking() is never entered.

    Measured on the operator's host. A mangled paste armed the container with a
    command fragment instead of the passphrase, and s_ebb03e8dc1b96e1c failed
    twice with `Error: The wallet passphrase entered was incorrect. (rpc code
    -14)`. Each attempt ran lock(adapter) at the line above, then raised -- so the
    wallet was left LOCKED, with staking off, and nothing said so.

    THE EXPOSURE IS THE SAFE DIRECTION AND THE COST IS REAL ANYWAY. Leaving a
    wallet LOCKED cannot spend, which is the right way to fail; but an operator who
    was staking is no longer staking, and the only notice was a payout failure
    message about a passphrase.

    WHY THE FIX IS NOT "MOVE THE LOCK INSIDE THE TRY". Restoring the staking
    unlock needs THE PASSPHRASE, and a wrong passphrase is exactly what failed --
    so there is nothing this function could do to restore it. What it can do is
    SAY so.

    AND UNTIL 2026-10-07 IT DID NOT, which is the second half of the same defect.
    The paragraph above used to end "...which is what unlock_for_sending()'s
    refusal now carries", and that was a claim about code that was never written:
    unlock_for_sending()'s body raises the daemon's own RPCError and says nothing
    about the lock this function took one line earlier. So the measurement was
    recorded, the fix was described, and the description was shipped instead of the
    fix -- which is worse than either, because a reader checking whether the
    failure explains itself finds a docstring saying it does.

    It is written below now, HERE rather than in unlock_for_sending(), and the
    placement is the point: this function is what called lock(), so this is the
    only frame that knows the wallet was staking a moment ago. unlock_for_sending()
    has other callers that did not lock first, and a message from there claiming
    staking was turned off would be wrong for every one of them (rule 8 -- the
    difference belongs where it is true).
    """
    lock(adapter)
    try:
        unlock_for_sending(adapter, passphrase, seconds)
    except Exception as error:
        # NOT A RETRY AND NOT A RESTORE. There is nothing to restore with: the
        # staking unlock needs the same passphrase that just failed. This re-raises
        # with what the operator has to know -- their wallet is locked, it is not
        # staking, and only they hold what fixes it.
        reason = (
            f"the wallet was LOCKED by this call and the unlock then FAILED, so it is locked AND "
            f"NOT STAKING. Nothing was sent. This cannot be restored from here: re-enabling staking "
            f"needs the same passphrase that just failed, which this process does not have. Run "
            f"`walletpassphrase <your passphrase> {STAKING_UNLOCK_SECONDS} true` against this daemon "
            f"to put it back to staking. The daemon said: {error}"
        )
        logger.error("gridcoin wallet: unlock for sending FAILED -- %s", reason)
        raise GridcoinLockError(reason) from error
    try:
        yield
    finally:
        # BaseException-safe by construction: `finally` runs for
        # KeyboardInterrupt and SystemExit too, which a bare `except Exception`
        # around the body would not have caught. An operator pressing Ctrl-C
        # during a slow payout is exactly when a wallet gets left open.
        locked = False
        try:
            lock(adapter)
            # Set only after the lock RETURNED. It is what tells the two failure
            # outcomes apart, and guessing it from which line raised is how the old
            # message came to claim an exposure that was not there.
            locked = True
            unlock_for_staking(adapter, passphrase)
        except Exception as error:
            reason = restore_failed_because(locked, error)
            logger.error("gridcoin wallet: restore FAILED -- %s", reason)
            raise GridcoinLockError(reason) from error
