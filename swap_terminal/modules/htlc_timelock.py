#!/usr/bin/env python3
"""Decide the locktime of an HTLC: which number, in which units, for which side.

Role: function level (the decision -- one locktime per contract, per role)
Reads: nothing. Every input is an argument; there is no environment variable,
       no file and no RPC call in this module.
Writes: nothing
Can move funds: no directly. It DECIDES the number that goes into the refund
       branch of a contract, which is the number that says whether the
       initiator ever gets their coins back. A wrong value here cannot be
       undone once a contract is funded against it.
Mainnet-safe: yes -- pure arithmetic, no I/O of any kind. What it returns is
       not safe to fund a mainnet contract with unless the height passed in
       came from a mainnet daemon; this module cannot tell which chain the
       caller asked.

WHY THIS FILE EXISTS, AND WHY THE NUMBER IS NOT A LITERAL ANY MORE.

Before 2026-09-24, `modules/atomic_swapper.py` passed `locktime=500000` in all
six swap directions. Block 500,000 was mined on BTC in December 2017 and passed
on LTC years earlier, so a contract funded against it had a refund branch that
was ALREADY claimable at the moment of funding. That is not a smaller version
of atomicity, it is the absence of it: the initiator can fund their leg, wait
for the counterparty to fund theirs, refund their own leg immediately, and then
redeem the counterparty's leg with the preimage -- taking both.

That defect was harmless only because a SECOND defect cancelled it. The
locktime was pushed into the script by a compact-size varint encoder rather
than a CScriptNum encoder, so `500000` was enforced as `128,000,254` -- a
height roughly 127 million blocks out, which made the refund branch
unspendable instead of instantly spendable. Two defects pointing in opposite
directions; fixing either one alone is strictly worse than fixing neither,
which is why the encoder fix, this module and the address split landed in one
commit.

THE HEIGHT / TIMESTAMP BOUNDARY IS 500,000,000 AND IT IS NOT NEGOTIABLE.

OP_CHECKLOCKTIMEVERIFY compares its operand against the transaction's nLockTime
and REFUSES to compare the two when they fall on opposite sides of
LOCKTIME_THRESHOLD (500,000,000): below it the value is a block height, at or
above it a unix timestamp. A contract built with a height and spent with a
timestamp (or the reverse) fails, and it fails at the moment the refund is
attempted -- which is after something has already gone wrong. So every value
this module returns is checked against that boundary before it is handed back,
and the check is an exception rather than a clamp: a locktime that lands on the
wrong side of it is not a value to round into range, it is a bug in the caller.

Heights, not timestamps, are what this module produces. The reason is that all
three chains in this tree expose `getblockcount` and the refund spend must set
its own nLockTime to match; a height is the form both ends can agree on without
either trusting the other's clock. Blocks are never converted to
microfortnights anywhere in this file (CLAUDE.md rule 6): a block is not
1.2096 seconds long, it is however long it took. The per-chain figures in
SECONDS_PER_BLOCK below are TARGET intervals used to turn a policy stated in
hours into a count of blocks, once, at the point the policy is expressed -- and
they are deliberately named as an estimate, because a chain that stalls does
not care what its target was.

THE ASYMMETRY IS THE WHOLE POINT OF HAVING TWO ROLES.

In a two-leg atomic swap the initiator's timelock MUST be longer than the
participant's. The order of events is: initiator funds leg A, participant funds
leg B, initiator redeems leg B revealing the preimage, participant redeems leg
A using it. If the INITIATOR's lock expired first, the initiator could refund
leg A while still holding the ability to redeem leg B -- both legs, one party.
With the initiator's lock strictly longer, the participant always has a window
in which the preimage is public and leg A is still locked.

INITIATOR_LOCK_HOURS is 48 and PARTICIPANT_LOCK_HOURS is 24: a 2:1 ratio, which
is the conventional choice because it leaves the participant a full day of
margin after the preimage appears. The module refuses to import if anyone edits
them into the wrong order -- see the assertion at the bottom, which is a
statement about a fund-path invariant rather than a style check.

WHAT THIS MODULE DOES NOT KNOW, STATED PLAINLY (rule 17).

It has no way to check that `current_height` came from the chain the caller
named, or from a mainnet rather than a testnet daemon. It returns arithmetic on
whatever it is given. Nothing in this tree has funded a contract built on one
of these values and then refunded it after expiry, on any chain -- that is the
only proof that would settle whether the refund branch works, and it cannot be
run from a machine with no chain access.
"""

# Bitcoin's LOCKTIME_THRESHOLD. Below it, an nLockTime or a CLTV operand is a
# BLOCK HEIGHT; at or above it, a UNIX TIMESTAMP. Mixing the two makes CLTV
# fail rather than compare, which is why every return value below is checked
# against it.
LOCKTIME_THRESHOLD = 500_000_000

# Target block interval per chain, in seconds. These are TARGETS, not
# measurements of any particular chain's recent behavior: BTC aims at 10
# minutes, LTC at 2.5, and Gridcoin's stake interval is roughly 90 seconds.
# They are used only to turn a policy stated in hours into a number of blocks,
# and they are the reason a 48-hour initiator lock is 288 blocks on BTC and
# 1152 on LTC rather than one number for all three (CLAUDE.md rule 11: one
# vocabulary, derived in one place, applied identically to every asset).
SECONDS_PER_BLOCK = {
    "BTC": 600,
    "LTC": 150,
    "GRC": 90,
}

# The two roles, in hours. See the module docstring for why the initiator's
# must be the longer of the two; the ratio is the safety margin the participant
# gets after the preimage becomes public.
INITIATOR_LOCK_HOURS = 48
PARTICIPANT_LOCK_HOURS = 24

ROLE_INITIATOR = "initiator"
ROLE_PARTICIPANT = "participant"

_ROLE_HOURS = {
    ROLE_INITIATOR: INITIATOR_LOCK_HOURS,
    ROLE_PARTICIPANT: PARTICIPANT_LOCK_HOURS,
}

SECONDS_PER_HOUR = 3600


def lock_hours_for_role(role: str) -> int:
    """How many hours of timelock this side of the swap gets.

    Raises ValueError on an unknown role rather than defaulting to one of them.
    A typo that silently selected the participant's shorter lock for the
    initiator's leg is exactly the failure the asymmetry exists to prevent, and
    a default would make it invisible.
    """
    if role not in _ROLE_HOURS:
        raise ValueError(f"unknown HTLC role {role!r}; expected one of {sorted(_ROLE_HOURS)}")
    return _ROLE_HOURS[role]


def timelock_blocks(asset: str, role: str) -> int:
    """Blocks of timelock for `role` on `asset`, from the policy in hours.

    The result is a COUNT OF BLOCKS and is never rendered in microfortnights
    (rule 6). Rounded down deliberately: fewer blocks means the lock expires
    marginally sooner, and for the participant's leg -- the shorter one -- that
    is the side of the rounding that cannot create the both-legs failure the
    asymmetry guards against.
    """
    if asset not in SECONDS_PER_BLOCK:
        raise ValueError(f"unknown asset {asset!r}; expected one of {sorted(SECONDS_PER_BLOCK)}")
    hours = lock_hours_for_role(role)
    return int(hours * SECONDS_PER_HOUR // SECONDS_PER_BLOCK[asset])


def contract_locktime(asset: str, role: str, current_height: int) -> int:
    """The absolute BLOCK HEIGHT to put in an HTLC's refund branch.

    Args:
        asset: "BTC", "LTC" or "GRC" -- the chain the contract is funded on,
            which decides how many blocks an hour is worth.
        role: ROLE_INITIATOR for the leg you fund first, ROLE_PARTICIPANT for
            the leg funded in response to it. The initiator's is the longer.
        current_height: the chain tip, from `getblockcount` on the daemon for
            THIS asset. It is passed in rather than fetched so that this
            function stays a decision with seeded inputs (rule 10) and so that
            a test can exercise it without a chain.

    Returns:
        A block height, strictly greater than `current_height` and strictly
        below LOCKTIME_THRESHOLD.

    Raises:
        ValueError: on an unknown asset or role, on a non-positive height, or
            if the computed value would cross LOCKTIME_THRESHOLD. That last
            case cannot be reached by any real chain this decade -- the
            threshold is 500 million blocks and BTC is under a million -- but
            it is checked rather than assumed, because the consequence of
            crossing it is a refund branch that CLTV refuses to evaluate at
            all, discovered at refund time.
    """
    if current_height <= 0:
        raise ValueError(f"current_height must be a positive block height, got {current_height!r}")
    if current_height >= LOCKTIME_THRESHOLD:
        raise ValueError(
            f"current_height {current_height} is at or above LOCKTIME_THRESHOLD {LOCKTIME_THRESHOLD}; "
            "that is a unix timestamp, not a block height"
        )
    locktime = current_height + timelock_blocks(asset, role)
    if locktime >= LOCKTIME_THRESHOLD:
        raise ValueError(
            f"locktime {locktime} for {asset}/{role} crosses LOCKTIME_THRESHOLD {LOCKTIME_THRESHOLD}; "
            "CHECKLOCKTIMEVERIFY would read it as a timestamp and refuse to compare it with a height"
        )
    return locktime


def describe_locktime(asset: str, role: str, current_height: int, locktime: int) -> str:
    """One line an operator can read off the screen (rule 14).

    Names the chain, the role, the tip it was derived from, the absolute height
    and the gap in BLOCKS -- never in microfortnights, because blocks are not
    times (rule 6). The approximate wall-clock figure is given in hours and
    labeled as an estimate from a target block interval, since that is what it
    is: the chain owes nobody that schedule.
    """
    blocks = locktime - current_height
    hours = blocks * SECONDS_PER_BLOCK[asset] / SECONDS_PER_HOUR
    return (
        f"{asset} {role} locktime={locktime} (height, not a timestamp) "
        f"tip={current_height} +{blocks} blocks "
        f"<- roughly {hours:.1f}h at {asset}'s {SECONDS_PER_BLOCK[asset]}s target block interval; an estimate, not a deadline the chain owes you"
    )


# A fund-path invariant, checked at import rather than documented and hoped
# for. If the initiator's lock is ever edited to be shorter than or equal to
# the participant's, the initiator can refund their own leg while still able to
# redeem the counterparty's -- both legs, one party. Failing the import is the
# correct outcome: nothing that builds a contract should start.
if INITIATOR_LOCK_HOURS <= PARTICIPANT_LOCK_HOURS:  # pragma: no cover -- guards an edit, not a runtime state
    raise ValueError(
        "INITIATOR_LOCK_HOURS must be strictly greater than PARTICIPANT_LOCK_HOURS: "
        "if the initiator's timelock expires first they can refund their own leg and still redeem the other"
    )
