#!/usr/bin/env python3
"""Whether an XRPL escrow can be reclaimed, and who gets the drops if it is.

Role: submodule (a decision, callable with a dict and a clock -- rule 10)
Reads: nothing. One escrow object and one timestamp, both arguments.
Writes: nothing
Can move funds: no -- but it now BUILDS a payload that would, so the line is worth stating
      precisely rather than as a bare "no". escrow_cancel_tx() returns a dict. Nothing here
      signs it, nothing here submits it, and this module imports no transport at all (asserted
      by tests/test_xrp_escrow_verdict.py, which reads the import statements rather than
      trusting this sentence). A caller has to supply a signer and a submitter, and doing so is
      armed state and the operator's (rule 16).
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

THE OPEN QUESTION IS ANSWERED, AND THE ANSWER IS NO. This module used to say that building the
EscrowCancel was deliberately not done, because that transaction needs `Owner` and
`OfferSequence` -- the SEQUENCE OF THE EscrowCreate -- and nothing here knew whether an
`account_objects` escrow entry carries it. Rather than guess, `cancel_inputs()` printed the
question into the operator's own report. They ran it 2026-09-30 against the testnet:

    EscrowCancel buildable from this entry?  NO, missing OfferSequence
    Owner=rnjG8n16JinjqkzZj5Jmw6NDMBMzhhNbVv OfferSequence=(absent)

FOUR entries -- two escrows, each listed under both the sender's and the destination's owner
directory -- and `OfferSequence` was absent from every one, while `PreviousTxnID` was present in
every one. So the sequence has to come from the creating transaction, which is one `tx` read
away, and `offer_sequence_from()` below is that read's decision half. Asking rather than
assuming cost one line of output and settled it in one run; guessing would have repeated the
mistake xrp_htlc_escrow.py's header already records, where it claimed rippled's server-side
`submit` "may" be allowed on a public testnet server and the first real run answered
`notSupported`.

WHAT THIS MODULE STILL DOES NOT DO: submit anything. `escrow_cancel_tx()` below builds the
payload and nothing here signs it or opens a socket. Reclaiming spends a fee and is armed state,
so it stays the operator's (rule 16).
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


def offer_sequence_from(created: object) -> tuple[int | None, str]:
    """The EscrowCreate's own Sequence, read off a `tx` response. Returns (sequence, why).

    THE ONE READ `cancel_inputs()` NAMES, and it is here rather than inline in a report because
    it is a decision with three outcomes that a caller must tell apart (rule 10):

        (int, "")           the sequence, and an EscrowCancel can be built
        (None, reason)      the response is not an EscrowCreate, so this is the wrong
                            transaction and using its Sequence would cancel a different escrow
        (None, reason)      no readable Sequence at all

    WHY THE TransactionType IS CHECKED AND NOT ASSUMED. `PreviousTxnID` on a ledger entry points
    at the transaction that LAST MODIFIED it, which for an untouched escrow is its EscrowCreate
    and after any other modification is not. An EscrowCancel built from the wrong Sequence does
    not fail safe -- `OfferSequence` plus `Owner` is how the ledger IDENTIFIES an escrow, so a
    wrong sequence names a DIFFERENT escrow of the same owner, and the operator's own account
    holds two. Cancelling the wrong one of two 1-XRP escrows is exactly the kind of quiet
    mis-action that has no error message.

    A `tx` RESPONSE, WHICH NESTS. rippled returns the transaction's fields at the top level of
    `result` on some API versions and under `tx_json` on others, so both are read. A response
    carrying neither is reported, not defaulted.
    """
    if not isinstance(created, dict):
        return None, f"not a tx response: {type(created).__name__}"

    body = created.get("tx_json") if isinstance(created.get("tx_json"), dict) else created
    kind = body.get("TransactionType")
    if kind != "EscrowCreate":
        return None, (
            f"this transaction is a {kind!r}, not an EscrowCreate. PreviousTxnID points at "
            "whatever LAST modified the entry, so on a modified escrow it is not the creation -- "
            "and OfferSequence from the wrong transaction names a different escrow of the same "
            "owner, which the ledger would cancel without complaint"
        )

    sequence = body.get("Sequence")
    if not isinstance(sequence, int) or isinstance(sequence, bool):
        return None, f"the EscrowCreate carries no readable Sequence (got {sequence!r})"
    return sequence, ""


def escrow_cancel_tx(sender: str, owner: str, offer_sequence: int) -> dict:
    """The EscrowCancel payload. No condition and no fulfillment: this is the timelock branch.

    MOVED HERE FROM xrp_htlc_escrow.py ON 2026-09-30, and the move is rules 8 and 10 rather
    than tidying. It was defined in a root ENTRY POINT -- the nine-step testnet verifier -- so
    anything else that wanted to build a cancel had two options: import from a script whose
    import side effects are a nine-step run's worth of module-level setup, or write a second
    copy. A second copy of a transaction payload is rule 8's bug with a delay on it, and this
    one is a payload that moves money.

    It belongs beside the verdict that decides WHETHER to cancel and the lookup that supplies
    its one missing field: this module is the escrow's function layer, and xrp_htlc_escrow.py
    imports it from here now.

    `sender` IS SEPARATE FROM `owner` ON PURPOSE. Anybody may submit an EscrowCancel once
    CancelAfter has passed; `Owner` is the account that CREATED the escrow and is where the
    drops go back to. Defaulting one to the other would bake in "the creator cancels it", and
    the whole reason cancel_verdict() reports `returns_to` on every verdict is that those two
    are not the same question.
    """
    return {
        "TransactionType": "EscrowCancel",
        "Account": sender,
        "Owner": owner,
        "OfferSequence": offer_sequence,
    }


def reclaimable(escrows, now_unix: float) -> list[tuple[dict, EscrowVerdict]]:
    """Just the ones whose drops can go home now, each with its verdict.

    A LIST RATHER THAN A COUNT, because the next question after "is anything reclaimable" is
    always "which one and how much", and a count cannot answer it (rule 14: state what the
    number means, and a bare number here means one more round trip).
    """
    judged = [(escrow, cancel_verdict(escrow, now_unix)) for escrow in escrows or []]
    return [pair for pair in judged if pair[1].state == RECLAIMABLE]
