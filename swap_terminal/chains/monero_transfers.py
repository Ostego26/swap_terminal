"""Turning monero-wallet-rpc transfer records into this application's deposit events.

Role: function layer (rule 10 -- one decision per function, seeded inputs, no I/O)
Reads: nothing. The list of transfer dicts is passed in.
Writes: nothing
Can move funds: no. What it decides is whether a deposit COUNTS, which gates a
      payout downstream -- so it is on the money path even though it sends
      nothing itself.
Mainnet-safe: yes

THE SHAPE THIS HAS TO PRODUCE, AND WHY IT IS AWKWARD

services/deposit_service.py::upsert_deposit_event() keys every deposit on
(asset, txid, vout) -- db.py:110 makes that a UNIQUE constraint -- and gets
those rows from `adapter.find_deposits_to_address()`. That contract was written
for Bitcoin, where a deposit genuinely is an output at a numbered position in a
published transaction, and chains/base.py finds it by decoding the transaction
and looking for an output that pays the address.

Monero publishes no such thing. Outputs are not addressed to anyone in the
clear; the wallet scans, decrypts what belongs to it, and reports the result.
There is no "position in the transaction that paid me" to read, because from
the chain's point of view none of the outputs paid anybody in particular.

So `vout` has to come from somewhere, and the choice matters more than it
looks.

WHAT THIS FILE REFUSES TO DO, AND THE INCIDENT THAT IS THE REASON

chains/base.py:169 carries a comment headed "PROPOSAL MARKER, NOT AN
ENDORSEMENT" describing what happens there when an output cannot be located:
it FABRICATES an event at vout 0, carrying the amount the caller already
believed, in the same shape as a real one -- and the comment says plainly that
services/deposit_service.py "cannot tell the two apart". That defect is still
live in that file because fixing it would stall swaps that credit today, which
makes it the operator's call (rule 16).

A new adapter has no swaps crediting today, so it inherits the problem without
inheriting the excuse. CLAUDE.md rule 19 is explicit that the test for a patch
is whether it stops the symptom or stops the cause. Therefore: nothing in this
file ever invents an identifier. Where the wallet's answer does not determine
which output a deposit is, the scan RAISES, and the swap waits for a human
rather than being credited from a guess.

That is the same direction deposit_service.py already chose for ambiguity --
its multi_vout_groups() warning routes a suspicious sum to `under_review`
rather than to a payout. A halt is recoverable; a wrong payout is not.

THE KEY, AND THE ASSUMPTION IT RESTS ON -- READ THIS BEFORE TRUSTING IT

The identifier used for `vout` is the receiving subaddress's minor index,
which is stable, small, and meaningful: this terminal derives one subaddress
per swap, so the minor index IS which swap the money arrived for.

That is only a valid unique key if `get_transfers` reports at most ONE incoming
entry per (transaction, subaddress) -- aggregating several outputs to the same
subaddress in one transaction into a single summed entry. That is the
documented behavior as I understand it, AND IT WAS NOT VERIFIED AGAINST A
RUNNING DAEMON (see THE HONEST STATUS in chains/monero.py).

So it is not assumed. It is CHECKED, every scan, by
_reject_ambiguous_keys() below: if two entries ever collide on that key, the
scan raises instead of picking one or summing them. If the assumption is
wrong, the operator finds out from an error naming the transaction, on the
first deposit that proves it -- not from a balance that quietly disagrees with
the chain. Rule 17: a reason to believe something is not the same as having
checked it, and this is the checking.
"""

from dataclasses import dataclass, field

from .monero_units import from_atomic

# ---------------------------------------------------------------------------
# UNVERIFIED FIELD NAMES. Every key this module reads out of a wallet response
# is named here, once, so that confirming them against a real monero-wallet-rpc
# is a read of one block and fixing a wrong one is a one-line change rather
# than a hunt through the file. See THE HONEST STATUS in chains/monero.py for
# why they could not be confirmed where this was written.
# ---------------------------------------------------------------------------
FIELD_TXID = "txid"
FIELD_AMOUNT = "amount"                  # atomic units (piconero), an integer
FIELD_ADDRESS = "address"                # the subaddress that received it
FIELD_CONFIRMATIONS = "confirmations"
FIELD_SUBADDR_INDEX = "subaddr_index"    # {"major": account, "minor": index}
FIELD_UNLOCK_TIME = "unlock_time"        # 0 for an ordinary transfer
FIELD_LOCKED = "locked"
FIELD_DOUBLE_SPEND = "double_spend_seen"
FIELD_TYPE = "type"                      # "in", "pool", "pending", "out", ...

# Transfer types that represent money that has arrived and is in a block.
# "pool" and "pending" are deliberately NOT here: an unmined transfer has no
# confirmations to count and can still be replaced, and crediting one would
# mean the deposit gate saw money the chain has not committed to.
CREDITABLE_TYPES = frozenset({"in"})


class MoneroTransferError(ValueError):
    """A wallet response could not be turned into deposit events safely.

    Every raise in this module is a refusal to guess, and each carries the
    transaction it is about. None of them is recoverable by retrying the same
    scan -- they mean an assumption in this file is wrong, or the wallet
    returned something it was not expected to, and both want a human.
    """


@dataclass
class TransferScan:
    """The result of reading a wallet's transfer list for one address.

    Two lists rather than one, because "arrived but not yet creditable" and
    "did not arrive" are different facts and rule 14 forbids rendering them
    the same way. `events` is what the application may act on; `deferred` is
    money that IS there and is being held back, which an operator watching a
    swap sit at `awaiting_deposit` needs to be told about -- otherwise the
    screen says nothing while the chain says something.
    """

    events: list[dict] = field(default_factory=list)
    deferred: list[str] = field(default_factory=list)


def _minor_index(transfer: dict, txid: str) -> int:
    """The receiving subaddress's minor index, or a refusal.

    This is the value that becomes `vout`. It is read rather than derived, and
    its absence is fatal on purpose: a transfer whose subaddress is unknown
    cannot be attributed to a swap, and the alternative to raising is inventing
    a zero -- which is the exact fabrication the module docstring exists to
    refuse.
    """
    index = transfer.get(FIELD_SUBADDR_INDEX)
    if not isinstance(index, dict) or "minor" not in index:
        raise MoneroTransferError(
            f"transfer {txid} carries no {FIELD_SUBADDR_INDEX}.minor, so there is nothing to identify "
            f"which output it is. NOT credited: the only alternative would be to invent an index, and "
            f"deposit_events keys on it (db.py:110)."
        )
    try:
        return int(index["minor"])
    except (TypeError, ValueError) as error:
        raise MoneroTransferError(
            f"transfer {txid} has a non-integer {FIELD_SUBADDR_INDEX}.minor ({index['minor']!r}). NOT credited."
        ) from error


def _reject_ambiguous_keys(events: list[dict]) -> None:
    """Refuse a scan in which two events would claim the same (txid, vout).

    This is the check standing under the assumption the module docstring names
    -- that one transaction contributes at most one incoming entry per
    subaddress. If that is false, two events collide here, and the choices
    would be to drop one (losing money the chain credited), to sum them
    (guessing that they are parts of one payment) or to let both through (where
    upsert_deposit_event's UNIQUE constraint would make one silently overwrite
    the other).

    All three are wrong in a way nobody would notice, so the scan stops.
    """
    seen: dict[tuple[str, int], dict] = {}
    for event in events:
        key = (event["txid"], event["vout"])
        if key in seen:
            raise MoneroTransferError(
                f"two incoming transfers in transaction {key[0]} both report subaddress index {key[1]}, "
                f"so (txid, vout) does not identify a deposit the way db.py:110 requires. NOTHING was "
                f"credited from this scan. This refutes the assumption documented in "
                f"monero_transfers.py -- the amounts involved are "
                f"{seen[key]['amount']} and {event['amount']} XMR, and the key needs to become the "
                f"output's global index before this deposit can be credited."
            )
        seen[key] = event


def deposit_events_from_transfers(transfers, address: str, min_confirmations: int) -> TransferScan:
    """Wallet transfer records -> the deposit-event dicts the application speaks.

    Filters to the one address asked about rather than trusting the caller to
    have scoped the query, because `get_transfers` is an account-wide call and
    a scoping argument that silently stopped working would return every swap's
    deposits for whichever swap asked first.

    `min_confirmations` is used only to decide whether a transfer is reported
    as deferred; the event itself always carries the real confirmation count,
    and the gate that releases a payout stays in services/deposit_service.py
    where it is for every other chain. Duplicating the threshold comparison
    here would be rule 8's two-copies-of-one-rule, and the copy that drifts is
    always the one further from the decision.
    """
    scan = TransferScan()
    for transfer in transfers or []:
        txid = transfer.get(FIELD_TXID)
        if not txid:
            raise MoneroTransferError(
                f"a transfer for {address} has no {FIELD_TXID}. NOT credited -- an event without a "
                f"transaction id cannot be deduplicated, so re-scanning would credit it again every cycle."
            )
        if transfer.get(FIELD_TYPE) not in CREDITABLE_TYPES:
            continue
        if transfer.get(FIELD_ADDRESS) != address:
            continue
        if transfer.get(FIELD_DOUBLE_SPEND):
            scan.deferred.append(f"{txid}  double_spend_seen=true  <- NOT credited; the wallet saw a conflict")
            continue

        atomic = transfer.get(FIELD_AMOUNT)
        if not isinstance(atomic, int) or isinstance(atomic, bool):
            raise MoneroTransferError(
                f"transfer {txid} reports {FIELD_AMOUNT}={atomic!r}, which is not an integer of atomic "
                f"units. NOT credited: reading a float here would silently lose piconero, and reading a "
                f"string would coerce to the wrong scale."
            )
        confirmations = int(transfer.get(FIELD_CONFIRMATIONS, 0) or 0)

        # A custom unlock_time can hold an output beyond the ten-block
        # consensus lock, for as long as the sender chose. Crediting one would
        # release a swap whose payout the wallet then refuses to build -- the
        # gate-versus-transfer disagreement that monero_units.
        # effective_min_confirmations() exists to prevent, arriving by the
        # other door. Held back and REPORTED, never silently dropped.
        unlock_time = int(transfer.get(FIELD_UNLOCK_TIME, 0) or 0)
        if unlock_time or transfer.get(FIELD_LOCKED):
            scan.deferred.append(
                f"{txid}  {from_atomic(atomic)} XMR  unlock_time={unlock_time} locked="
                f"{bool(transfer.get(FIELD_LOCKED))}  <- arrived, NOT creditable until it unlocks"
            )
            continue

        event = {
            "txid": txid,
            "vout": _minor_index(transfer, txid),
            "address": address,
            "amount": from_atomic(atomic),
            "confirmations": confirmations,
        }
        if confirmations < min_confirmations:
            scan.deferred.append(
                f"{txid}  {event['amount']} XMR  {confirmations}/{min_confirmations} blocks  <- arrived, still maturing"
            )
        scan.events.append(event)

    _reject_ambiguous_keys(scan.events)
    return scan
