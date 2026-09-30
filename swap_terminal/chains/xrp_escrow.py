#!/usr/bin/env python3
"""Whether an XRPL escrow can be reclaimed, and who gets the drops if it is.

Role: submodule (a decision, callable with a dict and a clock -- rule 10)
Reads: nothing. One escrow object and one timestamp, both arguments.
Writes: nothing
Can move funds: no. It returns a verdict. Nothing here builds, signs or submits a
      transaction, and this module imports no transport.
Mainnet-safe: yes. It names no endpoint and asks no server which network it is on.
Live-safe: yes.

WHY THIS EXISTS, and the escrow that caused it.

xrp_balances.py printed `CancelAfter=843784768` to the operator on 2026-09-29. That is
2026-09-27T00:39:28Z -- two days BEFORE the line was printed -- so an escrow holding 1 XRP was
already reclaimable and nothing on the screen said so. The fix that day made the line readable,
and it put the verdict in `_when()`, a display helper inside an entry point: a DECISION inside a
report builder, which is rule 10's opening complaint. It is here now, as a function that can be
called with a seeded object and a fixed clock, and xrp_balances.py renders what it returns.

THE ONE FACT THAT MAKES THIS SAFE TO ACT ON, and it is worth stating because the opposite is the
natural assumption: EscrowCancel HAS NO DESTINATION. After CancelAfter, anybody may submit it,
and the drops go back to the escrow's own `Account` -- the party who created it. Cancelling
somebody else's expired escrow returns THEIR money to THEM and costs the canceller a fee. So
"who may cancel" is not the interesting question and "where does the money go" has exactly one
answer, which every verdict below carries.

WHAT THIS MODULE DELIBERATELY DOES NOT DO: build the EscrowCancel. That transaction needs
`Owner` and `OfferSequence` -- the SEQUENCE OF THE EscrowCreate -- and an escrow entry returned
by `account_objects` is not known here to carry it; the sequence belongs to the creating
transaction, which `PreviousTxnID` points at. Nothing in this tree has a recorded escrow ledger
entry to check that against, and no XRPL endpoint is reachable from where this was written.
Guessing the field would be the same mistake xrp_htlc_escrow.py's header already records: it
claimed rippled's server-side `submit` "may" be allowed on a public testnet server, and the
first real run answered `notSupported`. One `account_objects` response settles it, so
`cancel_inputs()` below reports what the real response carried instead of assuming -- it is the
question asked as code rather than as a hypothesis (rule 17). Until an answer comes back the
reclaim is a PROPOSAL and this module is the half that is not (rule 16).
"""

from __future__ import annotations

from typing import NamedTuple

from .xrp_units import unix_from_ripple_time

#: What a verdict can be. Four, and the difference between the middle two is the whole point.
RECLAIMABLE = "RECLAIMABLE"        # CancelAfter has passed; the drops can go home now
NOT_YET = "NOT_YET"                # CancelAfter is in the future
NEVER_BY_TIME = "NEVER_BY_TIME"    # no CancelAfter at all: waiting cannot free it
UNREADABLE = "UNREADABLE"          # the object did not carry what this needs


class EscrowVerdict(NamedTuple):
    """One escrow's reclaim state, with the sentence an operator reads.

    `returns_to` is on every verdict rather than only the reclaimable one, because the question
    it answers -- where would the money go -- is the one somebody asks BEFORE deciding whether
    to bother, and an answer that appears only once the answer is yes is an answer arriving late.
    """

    state: str
    reason: str
    returns_to: str
    cancel_after_unix: int | None


def cancel_verdict(escrow: object, now_unix: float) -> EscrowVerdict:
    """May this escrow be reclaimed by waiting, and where do its drops go?

    PURE, AND THE CLOCK IS AN ARGUMENT. `now_unix` is passed rather than read, so the boundary
    case -- a CancelAfter exactly equal to now -- is testable without waiting for a second to
    tick. XRPL's own rule is that the cancel is allowed once the ledger's close time is AFTER
    CancelAfter; this treats equality as reclaimable and says so in the reason, which is the
    direction that can only be wrong by being one second early on a value the ledger rounds
    anyway.

    RIPPLE SECONDS, NOT UNIX SECONDS, and that conversion is chains/xrp_units' and not this
    module's (rule 8). The two differ by 946,684,800 -- thirty years -- so reading one as the
    other does not look like an error, it looks like an escrow that expires in 1996 or in 2056.
    That is the defect this whole line of work started from.
    """
    if not isinstance(escrow, dict):
        return EscrowVerdict(UNREADABLE, f"not an escrow object: {type(escrow).__name__}", "", None)

    owner = str(escrow.get("Account") or "")
    returns_to = owner or "(the object names no Account, so where the drops go cannot be said)"

    raw = escrow.get("CancelAfter")
    if raw is None:
        return EscrowVerdict(
            NEVER_BY_TIME,
            "no CancelAfter, so no amount of waiting frees it. It can only be finished by "
            "whoever can satisfy its condition -- or, with no condition, by its destination. "
            "Waiting is not a recovery path here.",
            returns_to, None,
        )
    try:
        cancel_after = unix_from_ripple_time(raw)
    except (TypeError, ValueError):
        return EscrowVerdict(
            UNREADABLE,
            f"CancelAfter is {raw!r}, which is not a number of Ripple seconds",
            returns_to, None,
        )

    if now_unix >= cancel_after:
        waited = int(now_unix - cancel_after)
        return EscrowVerdict(
            RECLAIMABLE,
            f"CancelAfter passed {waited}s ago. An EscrowCancel submitted now returns the drops "
            f"to {returns_to} -- EscrowCancel has no destination, so they go to the account that "
            f"created the escrow and not to whoever cancels it.",
            returns_to, cancel_after,
        )
    return EscrowVerdict(
        NOT_YET,
        f"CancelAfter is {int(cancel_after - now_unix)}s away. Until then it cannot be cancelled "
        f"by anybody, and the drops stay where they are.",
        returns_to, cancel_after,
    )


#: What EscrowCancel requires, and there are exactly two. `Owner` is the account that CREATED
#: the escrow -- which the object calls `Account`, a rename worth naming because reading them as
#: two different accounts is the obvious mistake -- and `OfferSequence` is the SEQUENCE NUMBER
#: OF THE EscrowCreate TRANSACTION, which is not the same thing as any sequence on the object.
CANCEL_NEEDS = ("Owner", "OfferSequence")


class CancelInputs(NamedTuple):
    """Whether an EscrowCancel could be built from this object, without building one.

    `ready` is the whole point and it is deliberately not a bool the caller can shrug at: when
    it is False, `missing` names the field and `how_to_get_it` names the one read that would
    supply it. Rule 14 -- state what the number means, next to the number.
    """

    ready: bool
    owner: str
    offer_sequence: int | None
    missing: tuple[str, ...]
    how_to_get_it: str


def cancel_inputs(escrow: object) -> CancelInputs:
    """Can the two fields EscrowCancel needs be read off this `account_objects` entry?

    THIS FUNCTION EXISTS BECAUSE THE ANSWER IS NOT KNOWN HERE, AND GUESSING IT IS THE FAILURE
    MODE THIS FILE'S HEADER ALREADY RECORDS ONCE. `xrp_htlc_escrow.py` claimed rippled's
    server-side `submit` "may" be allowed on a public testnet server; the first real run answered
    `notSupported`. So rather than write a reclaim against a field this tree has never seen in a
    real response, the report prints what the real response actually carried and one run settles
    it (rule 17: run the thing that would show it false).

    THE FALLBACK IS NAMED RATHER THAN IMPLEMENTED. If `OfferSequence` is absent, the sequence
    lives on the EscrowCreate transaction, which `PreviousTxnID` points at -- one `tx` call
    away. That is a read, and it is cheap, and it is still not written until somebody has seen
    whether it is needed: an unnecessary lookup wired in on a guess is the same cost as a
    missing one, paid on every run instead of once.

    NOTHING HERE SUBMITS. A True `ready` is a statement about a dict, not authorization: whether
    to spend a fee reclaiming somebody's escrow is the operator's call (rule 16, armed state).
    """
    if not isinstance(escrow, dict):
        return CancelInputs(False, "", None, CANCEL_NEEDS,
                            f"not an escrow object: {type(escrow).__name__}")

    owner = str(escrow.get("Account") or "")
    raw = escrow.get("OfferSequence")
    sequence = raw if isinstance(raw, int) and not isinstance(raw, bool) else None

    missing = tuple(
        name for name, present in (("Owner", bool(owner)), ("OfferSequence", sequence is not None))
        if not present
    )
    if not missing:
        return CancelInputs(True, owner, sequence, (), "both fields are on the object")

    how = []
    if "Owner" in missing:
        how.append("no `Account` on the object, which is where `Owner` comes from -- the "
                   "account_objects read itself is suspect")
    if "OfferSequence" in missing:
        previous = escrow.get("PreviousTxnID")
        if previous:
            where = f"one read away: `tx {previous}`, then take that transaction's own Sequence"
        else:
            where = ("nowhere on this entry, and `PreviousTxnID` is absent too, so there is no "
                     "pointer to the EscrowCreate either -- this object cannot supply it at all")
        how.append(
            "`OfferSequence` is the EscrowCreate's own Sequence and is not on this entry: " + where
        )
    return CancelInputs(False, owner, sequence, missing, "; ".join(how))


def reclaimable(escrows, now_unix: float) -> list[tuple[dict, EscrowVerdict]]:
    """Just the ones whose drops can go home now, each with its verdict.

    A LIST RATHER THAN A COUNT, because the next question after "is anything reclaimable" is
    always "which one and how much", and a count cannot answer it (rule 14: state what the
    number means, and a bare number here means one more round trip).
    """
    judged = [(escrow, cancel_verdict(escrow, now_unix)) for escrow in escrows or []]
    return [pair for pair in judged if pair[1].state == RECLAIMABLE]
