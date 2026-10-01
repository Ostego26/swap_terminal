"""Record money that arrived at a shared deposit account and belongs to no swap.

Role: submodule -> function (stranded_rows() is the decision; record() is the write)
Reads: nothing from a chain. It is handed what an adapter already read.
Writes: swap_terminal.db (unattributable_deposits) and nothing else
Can move funds: no. Nothing here credits, sends, or changes a swap's status.
Mainnet-safe: yes. Append-and-touch on one table that no gate reads.

WHY THIS FILE EXISTS, AND IT IS A MEASURED HOLE RATHER THAN A TIDINESS ONE.

On a tag-attributed chain every swap shares one deposit account, so a deposit is matched to
a swap by a discriminator the sender has to include -- a DestinationTag on XRP, a Memo
instruction on Solana. A deposit that arrives without one, or with one that matches nothing,
is real money in an account the terminal controls that no swap can claim.

Counted 2026-10-01, that money reached no durable record anywhere in this tree:

    chains/solana.py      records the drop on `unattributable_drops` and logs it at WARNING.
                          Grepped: that attribute is written there and read NOWHERE under
                          swap_terminal/ -- the only consumer in the repository is the
                          diagnostic script solana_chain_check.py.
    chains/xrp.py         defers and PRINTS the same situation once per adapter instance,
                          with a comment explaining that printing it per swap misrepresents
                          the scale. Correct, and still only a print.
    services/deposit_service.attributable_events()
                          filters out every event whose discriminator is not THIS swap's.
                          Correct per swap. But a deposit whose discriminator matches NO
                          open swap is filtered by every call and recorded by none -- and
                          unlike the two above, nothing even logs that one.

CLAUDE.md rule 5: "a measurement that only exists in a log is not learning. If it decides
something, it belongs in SQL where the next cycle can read it." A stranded deposit decides
something -- it decides that a person has to go and look -- and on mainnet the only thing
that would tell them was a log line in a worker's output.

WHAT THIS DELIBERATELY DOES NOT DO.

It does not guess. A row here is a statement that attribution FAILED, and nothing in this
module tries to repair it: no nearest-amount matching, no most-recent-swap fallback, no
"probably theirs". The reasoning is chains/solana.py's and it has not changed -- crediting a
deposit to whichever swap was being refreshed pays the wrong person, and an uncredited
deposit is a support ticket while a misattributed one is somebody else's money.

It also holds no swap_id, now or later. See db.py's comment on the table: a resolution is a
person's note, not a foreign key, because a swap_id here would make this table a second
opinion on who owns a deposit.
"""

from __future__ import annotations

import logging
from typing import NamedTuple

from .swap_service import TAG_ATTRIBUTION

logger = logging.getLogger(__name__)


class StrandedDeposit(NamedTuple):
    """One deposit that could not be attributed. Chain-agnostic on purpose.

    ONE SHAPE FOR BOTH CHAINS, because there is one rule -- "arrived, nobody can claim it" --
    and rule 8 says two copies of one rule is a bug with a delay on it. Solana's
    UnattributableCredit and XRP's deferred payments are the two sources; this is what they
    both become before anything is written, so the table cannot learn a per-chain exception.

    `discriminator` is None when the deposit carried NO tag or memo at all, and an integer
    when it carried one that matched no open swap. Those are two different support
    conversations and db.py's column comment says which.
    """

    asset: str
    txid: str
    address: str
    amount: float
    credits: int
    why: str
    confirmations: int
    discriminator: int | None = None


def stranded_rows(drops, asset: str, *, confirmations: int = 0) -> list[StrandedDeposit]:
    """Solana's UnattributableCredit list as StrandedDeposit rows. PURE -- no db, no chain.

    EXTRACTED AS A FUNCTION rather than inlined into the caller (rule 10), so the mapping can
    be asserted on with seeded inputs. It is also the only place that knows Solana's drop
    shape, which is what keeps `record()` chain-agnostic: an XRP source adds a sibling
    converter here and nothing downstream changes.

    CONFIRMATIONS DEFAULT TO ZERO AND THAT IS NOT A GUESS. The Solana drop is recorded inside
    `_attributable`, which runs per transaction and does not carry the commitment rank, so the
    adapter genuinely does not hand this over. Zero means "not known from this source" and the
    column exists for the sources that do know; recording a plausible 1 would be inventing a
    confirmation count on a money record, which is the register rule 17 forbids.
    """
    return [
        StrandedDeposit(
            asset=asset,
            txid=drop.signature,
            address=drop.address,
            amount=float(drop.amount),
            credits=int(drop.credits),
            why=drop.why,
            confirmations=confirmations,
            # NO DISCRIMINATOR BY CONSTRUCTION: the Solana drop happens precisely because
            # deposit_tag_from() found no usable memo. A source that DID find one that matched
            # nothing would set this, and that source does not exist yet -- see the module
            # docstring on attributable_events(), which is where it would come from.
            discriminator=None,
        )
        for drop in drops or []
    ]


def record(db, rows, *, now: str) -> int:
    """Write or touch one row per stranded deposit. Returns how many were NEW.

    IDEMPOTENT BECAUSE THE WATCHER POLLS. find_deposits_to_address() is called once per active
    swap, every cycle, over the SAME shared account -- which is the measurement chains/xrp.py
    already records: two unattributable payments printed four lines because two swaps each
    scanned the same account. So this sees the same deposit many times and must not accumulate
    a row per sighting. First sighting inserts; every later one bumps `last_seen_at`.

    `last_seen_at` IS WORTH BUMPING rather than ignoring the duplicate. It answers "is this
    still there, or did somebody move it" without opening the chain, and a row whose
    last_seen_at stops advancing while the scan keeps running is itself a finding.

    AN ALREADY-RESOLVED ROW IS STILL TOUCHED, AND NOT REOPENED. A person wrote that
    resolution; a later sighting of the same transaction is not new information about who owns
    it, and clearing `resolved_at` would make the scan undo a human's decision on every cycle.
    So the UPDATE leaves resolved_at and resolution_note alone -- the one thing in this module
    that must not be driven by a poll.

    `now` IS A PARAMETER, not datetime.now() inside. The timestamps are what an operator reads
    to decide how long money has been sitting, so they have to be assertable in a test without
    patching the clock (the same reason xrp_escrow.cancel_verdict() takes the clock).
    """
    inserted = 0
    for row in rows:
        # UPSERT RATHER THAN SELECT-THEN-INSERT: two workers can scan the same shared account
        # at once, and a check-then-write would race into an IntegrityError on the UNIQUE
        # constraint. ON CONFLICT makes the collision the normal path instead of an exception.
        db.execute(
            """
            INSERT INTO unattributable_deposits
                (asset, txid, address, amount, credits, discriminator, why,
                 confirmations, first_seen_at, last_seen_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(asset, txid) DO UPDATE SET
                last_seen_at = excluded.last_seen_at,
                confirmations = excluded.confirmations,
                amount = excluded.amount,
                credits = excluded.credits
            """,
            (row.asset, row.txid, row.address, row.amount, row.credits,
             row.discriminator, row.why, row.confirmations, now, now),
        )
        # rowcount is 1 for both the insert and the update, so it cannot tell them apart.
        # `changes()` cannot either. The first-sighting test is whether first_seen_at is this
        # call's `now`, which only an inserted row has.
        first_seen = db.execute(
            "SELECT first_seen_at FROM unattributable_deposits WHERE asset = ? AND txid = ?",
            (row.asset, row.txid),
        ).fetchone()
        # BY NAME, NOT BY POSITION. db.connect_db sets row_factory = dict_factory, so a row is
        # a dict and `row[0]` raises KeyError -- which is what the first version of this did.
        if first_seen is not None and first_seen["first_seen_at"] == now:
            inserted += 1
            logger.warning(
                "%s deposit %s to the shared account CANNOT BE ATTRIBUTED and is now RECORDED "
                "in unattributable_deposits: %s %s, %d credit(s), %s. Nothing is credited and "
                "no swap changed; a person has to match this by hand.",
                row.asset, row.txid, row.amount, row.asset, row.credits, row.why,
            )
    return inserted


def discriminator_name(asset: str) -> str:
    """The chain's own word for the integer that matches a deposit to a swap.

    DERIVED FROM THE ONE PLACE THAT KNOWS (swap_service.TAG_ATTRIBUTION) rather than spelled
    again here -- rule 11, and the exact mistake that file's own comment records: a SOL refusal
    that said "DestinationTag", a field the Solana blockchain does not have.

    IMPORTED AT MODULE LEVEL, and I first wrote this as a deferred import with a `noqa: PLC0415`
    claiming it broke a cycle. Checked instead of assumed (rule 19: a noqa is a claim you
    checked): swap_service imports only .helpers and .xrp_tag_service, so there is no cycle to
    break and the suppression was a claim about nothing.
    """
    _variable, name, _network = TAG_ATTRIBUTION.get(asset, ("", "discriminator", ""))
    return name


def unclaimed_events(events, claimed: dict, still_refreshed) -> list[tuple[dict, str]]:
    """Events at the shared account that NO swap will credit, each with why. PURE.

    THE HOLE THIS CLOSES IS BIGGER THAN THE NO-MEMO ONE AND NOTHING EVEN LOGGED IT.
    services/deposit_service.attributable_events() keeps the events whose discriminator equals
    THIS swap's, which is correct per swap. Run across every active swap, the leftover -- an
    event matching none of them -- is filtered by every call and recorded by none. Counted
    2026-10-01: no reconciliation of the shared account exists anywhere in the tree, so that
    money reached no log line and no row.

    `still_refreshed` IS PASSED IN, not copied. The authority is
    deposit_service.ACTIVE_STATUSES, and that module imports this one -- so importing it back
    would be a real cycle, and spelling the tuple again here would be rule 8's duplicate with a
    delay on it. The caller has it; it hands it over.

    `claimed` MAPS discriminator -> (swap_id, status) FOR EVERY SWAP ON THE ASSET, not only the
    active ones, and that distinction is the reason this returns a REASON per event rather than
    a bare list. Two different situations, and neither is "nobody has this tag":

        no swap has it          the sender invented a reference, or sent without one and the
                                chain carried something else in that field
        a swap has it, and it   deposit_service.ACTIVE_STATUSES is
        is not active           ("awaiting_deposit", "deposit_seen", "confirming"), so
                                process_active_swaps() never refreshes that swap again. A
                                late or duplicate payment to a finished swap is therefore
                                never credited -- as stranded as one with no tag, and I would
                                have missed it by only looking for unmatched tags.

    WHAT IS NOT COVERED, said rather than implied (rule 17): an event matching an ACTIVE swap
    is left entirely alone here, even though this function could see it. That swap's own
    refresh is what credits it, and a second writer deciding the same thing is rule 8's defect
    -- two copies of "whose deposit is this" that agree today.
    """
    out = []
    for event in events or []:
        tag = event.get("vout")
        if tag is None:
            # NO DISCRIMINATOR AT ALL is the adapter's case, not this one: chains/solana.py
            # drops those before they become events, and chains/xrp.py classifies them out. An
            # event reaching here always carries one, so this is a shape guard rather than a
            # branch with a story -- and it skips rather than guessing, because an event with
            # no tag and no adapter drop behind it is a response this code does not understand.
            continue
        entry = claimed.get(tag)
        if entry is None:
            out.append((event, "no swap on this asset has that discriminator"))
            continue
        swap_id, status = entry
        if status not in still_refreshed:
            out.append((event, (
                f"it matches swap {swap_id}, which is {status} -- that swap is no longer "
                f"refreshed, so this payment will never be credited to it"
            )))
    return out


def unclaimed_rows(events, claimed: dict, asset: str, still_refreshed) -> list[StrandedDeposit]:
    """unclaimed_events() as StrandedDeposit rows, with the discriminator recorded.

    THE DISCRIMINATOR IS SET HERE and None from stranded_rows(), which is the distinction
    db.py's column comment draws: an integer means "sent with a reference that matches no open
    order", NULL means "sent with no reference at all". Two different support conversations,
    and the table can tell them apart because these two converters do.
    """
    return [
        StrandedDeposit(
            asset=asset,
            txid=event["txid"],
            address=event["address"],
            amount=float(event["amount"]),
            # ONE CREDIT PER EVENT, because an event IS one credit -- the adapters emit one row
            # per (transaction, account). `credits` exists for the Solana drop, which sums
            # several credits in one transaction before any event is built.
            credits=1,
            why=f"{discriminator_name(asset)} {event['vout']}: {why}",
            confirmations=int(event.get("confirmations") or 0),
            discriminator=int(event["vout"]),
        )
        for event, why in unclaimed_events(events, claimed, still_refreshed)
    ]
