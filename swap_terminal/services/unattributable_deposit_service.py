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


OUTSTANDING_SQL = """
SELECT
    asset,
    txid,
    address,
    amount,
    credits,
    discriminator,
    why,
    confirmations,
    first_seen_at,
    last_seen_at,
    resolved_at,
    resolution_note,
    -- DERIVED HERE AND NOT IN THE CALLER, because "has a human dealt with this"
    -- is the question the table exists to answer and it must answer the same way
    -- for every reader (rule 20). A root tool recomputing `resolved_at IS NULL`
    -- would be the second place that definition lived.
    CASE WHEN resolved_at IS NULL THEN 1 ELSE 0 END AS outstanding
FROM unattributable_deposits
WHERE (:asset IS NULL OR asset = :asset)
  AND (:include_resolved = 1 OR resolved_at IS NULL)
-- Oldest first, matching idx_unattributable_deposits_unresolved's own comment
-- about "the query an operator actually runs": the deposit somebody has been
-- waiting longest on is the one at the top.
ORDER BY first_seen_at ASC, id ASC
"""


def outstanding(db, asset: str | None = None, *, include_resolved: bool = False) -> list:
    """The rows an operator reads when somebody's money did not reach a swap.

    WHY THIS EXISTS, measured 2026-10-02. Nothing in the tree could show these
    rows. Four files reference the table -- the recorder, the adapter, the schema
    and pay_test_deposit.py -- and not one of them lists it, so the only way to
    see two days of unclaimed SOL was a hand-written SELECT. I wrote that SELECT
    for the operator and got the column name wrong (`reason`; it is `why`), which
    is the argument for the tool rather than an embarrassment: a query typed fresh
    each time is a query that can be wrong each time, against the one table in
    this schema whose rows are money nobody can claim.

    OUTSTANDING IS THE DEFAULT, resolved rows on request. A dealt-with deposit is
    history; an undealt-with one is somebody still owed an answer, and mixing them
    makes the operator count. Note that this is the OPPOSITE default from
    skippable_unattributable_txids() below, which deliberately INCLUDES resolved
    rows -- there the question is "may the scanner skip this" and resolved is a
    stronger yes. Same table, two questions, and the difference is stated at both
    sites (rule 8) because a reader who found one would otherwise assume the other.

    AND THE TWO ARE NOT COMPLEMENTS, which is the trap now that the skip set has
    been narrowed (2026-10-02). This function's rows are "somebody is still owed an
    answer"; that function's are "no later swap can ever claim this". An unresolved
    row whose discriminator no swap holds appears HERE as outstanding and is absent
    THERE, because the scan must keep reading it -- a future swap allocated that tag
    is what answers it. Neither list is the other's negation.
    """
    rows = db.execute(
        OUTSTANDING_SQL,
        {"asset": asset, "include_resolved": 1 if include_resolved else 0},
    ).fetchall()
    return list(rows)


#: The txids of recorded unattributable deposits that NO later swap can ever claim.
#:
#: THREE DISJOINT REASONS, each of which makes a rescan pointless rather than merely
#: expensive, and the fourth shape -- the one this predicate deliberately leaves OUT --
#: is what the whole narrowing is about. See skippable_unattributable_txids() below.
_SKIPPABLE_SQL = """
SELECT u.txid AS txid
FROM unattributable_deposits u
WHERE u.asset = ?
  AND (
        -- A HUMAN HAS DEALT WITH IT. Stronger than any of the others: whatever the
        -- chain says next, somebody has already written the answer down.
        u.resolved_at IS NOT NULL
        -- NO DISCRIMINATOR AT ALL, so no swap can ever match it.
        -- deposit_service.attributable_events() credits an event only when its `vout`
        -- EQUALS the swap's deposit_tag, and NULL equals nothing. A payment with no
        -- memo and no tag is unclaimable by construction, not by circumstance.
        OR u.discriminator IS NULL
        -- ITS DISCRIMINATOR IS ALREADY SPOKEN FOR BY A SWAP THAT WILL NEVER BE
        -- REFRESHED AGAIN. Tags are allocated MAX+1 per account and never reissued
        -- (xrp_tag_service._ALLOCATE_SQL, plus two BEFORE triggers that RAISE(ABORT)
        -- on delete and on re-point), so no FUTURE swap can take this tag either.
        -- The one swap that holds it has left the refreshed set, so nothing will
        -- credit this payment.
        OR EXISTS (
            SELECT 1 FROM swaps s
            WHERE s.from_asset = u.asset
              AND s.deposit_tag = u.discriminator
              AND s.status NOT IN ({statuses})
        )
  )
"""


def skippable_unattributable_txids(db, asset: str, still_refreshed) -> frozenset[str]:
    """Recorded unattributable txids a scan need not read again. NOT all of them.

    THE DEFECT THIS FUNCTION'S PREVIOUS VERSION WAS, established by running it rather
    than by reading it. It was `SELECT txid FROM unattributable_deposits WHERE asset = ?`
    -- every recorded row, forever -- and deposit_service.skip_txids() unions it into the
    set handed to every adapter. So the moment a payment was recorded here, no scan read
    it again, which made resolve_credited()'s own promise unreachable:

        a sender who pays before opening a swap, or pays a tag whose swap does not exist
        yet, lands here first, and if a swap is subsequently created and its refresh
        credits that txid, the stranded row is answered

    Nothing could credit that txid, because nothing would ever see the event again.
    Measured 2026-10-02 against the real schema: record one row for txid `tx_x` and
    `deposit_service.skip_txids(db, "SOL")` returns `{'tx_x'}` from that call onward.

    AND THE PROMISED PATH IS REACHABLE, which had to be established rather than assumed
    because the honest alternative was deleting the promise instead of the skip. Run
    2026-10-02 through the real allocator: insert six swaps and call
    xrp_tag_service.allocate_destination_tag() for each on one account and it returns
    1, 2, 3, 4, 5, 6. The allocator is `COALESCE(MAX(destination_tag), :below_first) + 1`,
    so every integer above the current high-water mark is a tag a LATER swap will be
    handed. A sender who puts memo 5 on a payment while only tags 1 and 2 exist is
    recorded here with discriminator 5 -- "no swap on this asset has that discriminator"
    -- and the fifth swap created after that genuinely owns the money. That is the case
    the old skip set made permanently invisible, and it is somebody's deposit.

    SO THE SET NARROWS TO WHAT CANNOT MOVE, and the three clauses are in _SKIPPABLE_SQL
    above with the argument for each beside it. What is deliberately NOT skipped is the
    one remaining shape: unresolved, carrying a discriminator, and no swap holds that
    discriminator yet. That is exactly the row a future allocation can claim.

    THE PREVIOUS VERSION'S OWN REASONING POINTED HERE AND WAS READ THE OTHER WAY. It
    argued, correctly, that "a resolved deposit has been dealt with by a human, which is
    a stronger reason not to re-read it than an open one" -- and then skipped the open
    ones too. The narrowing is the OPPOSITE of the obvious one: resolved rows are still
    skipped, and the unresolved ones come back into the scan.

    WHAT IT COSTS, measured rather than estimated, because re-reading is the exact cost
    the 2026-10-01 rate-limit fix removed and that rate limit cost a real credited
    deposit. On Solana a non-skipped signature is one getTransaction per scan. With one
    scan per shared account per cycle (scan_shared_accounts()) and the measured cycle of
    14.5µfn (17.6s), each re-read row costs 204.5 getTransaction/hour on deposit_watcher,
    plus 60/hour on reconcile_worker's 60s loop -- 264.5/hour per claimable row.

    ON THE OPERATOR'S HOST TODAY THAT IS ZERO, and that is why the precise predicate was
    worth writing instead of "skip only the resolved rows". Their one unresolved row is
    `61otPXfy...`, 0.05 SOL with memo 2, recorded against swap s_ba72c715150a063b -- which
    is `failed`, so the third clause skips it and the scan count does not move. The coarse
    version would have spent 264.5 getTransaction/hour re-reading a payment that the
    no-revival argument below says can never be credited.

    THE NO-REVIVAL ARGUMENT IS AN ABSENCE, NOT AN INVARIANT, and rule 2's distinction is
    the one that matters: "I could not find a revival path" is not "a revival path cannot
    exist". The third clause rests on it -- a swap that has left
    deposit_service.ACTIVE_STATUSES is never refreshed again, so the payment matching its
    tag is never credited. An operator tool that reopens a failed swap to accept a late
    payment is a reasonable thing to want, and the moment it exists this clause starts
    hiding the deposit that tool was built to find.
    tests/test_deposit_rate_limit.py::test_a_FAILED_swap_is_never_refreshed_back_into_an_active_status
    pins the absence so adding one fails a test instead of losing money quietly.

    `still_refreshed` IS PASSED IN, not imported, for the reason unclaimed_events() already
    gives for the same tuple: the authority is deposit_service.ACTIVE_STATUSES and that
    module imports this one, so importing it back would be a real cycle and spelling the
    statuses again here would be rule 8's duplicate with a delay on it. It is REQUIRED
    rather than defaulted, because a default of "every status" would silently skip every
    row with a matched discriminator and a default of "none" would silently skip none --
    two different wrong answers from an argument nobody passed.

    AND last_seen_at UNFREEZES FOR EXACTLY THE RE-READ ROWS. The column stopped advancing
    for every recorded row when the skip was total, because a skipped transaction never
    reaches record()'s ON CONFLICT. It now advances again for the claimable rows -- the
    ones a person is most likely to be waiting on -- and still freezes for the three
    skippable shapes. show_unattributable.py already labels the column "when the scan last
    READ this on-chain", which is true of both halves and is why that line needs no change.

    THE ROW STILL HOLDS EVERYTHING A RESCAN WOULD LEARN -- the amount, the account, the
    credit count, the confirmations and the adapter's own reason -- so re-reading is not
    how a human matches one of these. It is only how a LATER SWAP gets handed the event.
    """
    # The interpolation is a run of '?' generated from the LENGTH of `still_refreshed`;
    # the statuses themselves are bound as parameters on the line below, as is the asset.
    # Structure, not input -- the same claim deposit_service.process_active_swaps() makes
    # for the identical tuple, and checkable from these three lines.
    #
    # NO `noqa: S608` HERE, AND THAT IS NOT AN OVERSIGHT. ruff does not flag `.format()`
    # on a module constant, so a suppression would be a claim about a finding that was
    # never made -- RUF100 says so out loud, and rule 19 says a noqa is a claim you
    # checked rather than a way to decorate a line. The reasoning above is the claim.
    statuses = ",".join("?" for _ in still_refreshed)
    rows = db.execute(
        _SKIPPABLE_SQL.format(statuses=statuses),
        (asset, *still_refreshed),
    ).fetchall()
    return frozenset(str(row["txid"]) for row in rows)


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


def unclaimed_events(events, claimed: dict, still_refreshed, credited) -> list[tuple[dict, str]]:
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

    `credited` IS THE SET OF txids THAT ALREADY HAVE A deposit_events ROW, AND IT IS WHY THIS
    FUNCTION NO LONGER ASKS ABOUT STATUS FIRST. It is REQUIRED, not defaulted, because a
    default empty set reproduces the defect below silently.

    MEASURED 2026-10-01, on the first real SOL deposit ever credited by this system. The
    operator sent 0.05 devnet SOL with memo 2 for swap s_ba72c715150a063b, the Solana path
    worked -- memo read, amount read, credit seen -- and the log said:

        SOL deposit 5rHDrJYp... CANNOT BE ATTRIBUTED and is now RECORDED in
        unattributable_deposits: 0.05 SOL, 1 credit(s), Memo instruction 2: it matches swap
        s_ba72c715150a063b, which is payout_pending -- that swap is no longer refreshed, so
        this payment will never be credited to it. Nothing is credited and no swap changed; a
        person has to match this by hand.

    Every clause of which was false. The payment HAD been credited -- that is the only reason
    the swap was payout_pending -- and no person needed to do anything.

    The cause is an ordering this docstring's last paragraph assumed away.
    deposit_service.process_active_swaps() refreshes every active swap and THEN reconciles, in
    the same call:

        processed = [refresh_swap_from_chain(...) for swap in swaps]
        reconcile_shared_accounts(db, config, adapters)

    So by the time the reconciler reads the swap, that swap has left ACTIVE_STATUSES BECAUSE
    ITS OWN REFRESH JUST CREDITED THIS EVENT. "Is the swap still refreshed" was being used as a
    proxy for "was this payment credited", and the proxy inverts at the exact moment the credit
    succeeds. Every successful tag-chain swap would have produced a stranded row and a WARNING
    demanding manual reconciliation -- the output-that-looks-like-failure half of rule 14, and
    a log that cries wolf on the happy path is a log nobody reads the day something real
    happens.

    THE CREDIT IS A ROW, SO THE ROW IS THE QUESTION (rules 5 and 20). deposit_events gets a row
    the first time a watcher sees the transaction, at zero confirmations, so a txid present
    there is claimed by definition and needs no inference from a status. A genuinely late or
    duplicate payment to a finished swap has a DIFFERENT txid, is absent from deposit_events,
    and is still reported -- which is the case this function exists for and is not weakened.

    WHAT IS NOT COVERED, said rather than implied (rule 17): an event matching an ACTIVE swap
    is left entirely alone here, even though this function could see it. That swap's own
    refresh is what credits it, and a second writer deciding the same thing is rule 8's defect
    -- two copies of "whose deposit is this" that agree today.
    """
    out = []
    for event in events or []:
        # ALREADY CREDITED, ASKED BEFORE ANYTHING ELSE. A txid with a deposit_events row
        # belongs to the swap that recorded it, whatever that swap's status is now. Checked
        # first rather than inside the status branch because it is the stronger fact: a status
        # is a summary that moves, a deposit_events row is the credit itself.
        txid = str(event.get("txid") or "")
        if txid and txid in credited:
            continue
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


def unclaimed_rows(events, claimed: dict, asset: str, still_refreshed, credited) -> list[StrandedDeposit]:
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
        for event, why in unclaimed_events(events, claimed, still_refreshed, credited)
    ]


def resolve_credited(db, asset: str, credited, *, now: str) -> int:
    """Close any stranded row whose txid now HAS a deposit_events row. Returns how many.

    THE TABLE SELF-HEALS RATHER THAN ACCUMULATING FALSE POSITIVES, and that is the second half
    of the 2026-10-01 fix. unclaimed_events() stops CREATING a row for a credited payment;
    this clears the ones already written -- on the operator's host there is exactly one, from
    the first real SOL deposit, and without this it would sit in the table forever telling a
    person to match by hand a payment that was credited correctly.

    It is also the right behavior going forward and not only cleanup. A payment can legitimately
    be recorded as stranded and credited LATER: a sender who pays before opening a swap, or
    pays a tag whose swap does not exist yet, lands here first, and if a swap is subsequently
    created and its refresh credits that txid, the stranded row is answered. Leaving it open
    would make the unresolved count a number that only ever grows, which is the measurement
    rule 3 warns about -- a count whose denominator keeps changing underneath it.

    THAT PARAGRAPH WAS UNREACHABLE FOR A DAY AND IS NOT ANYMORE, said here because a promise
    in a docstring is a claim about the system and this one was false. The 2026-10-02 rate-limit
    fix put EVERY recorded txid into deposit_service.skip_txids(), so the "refresh credits that
    txid" step above could never happen: nothing read the transaction again. The skip set is now
    narrowed to the rows no later swap can claim -- see skippable_unattributable_txids(), which
    carries the measurement and the three clauses -- and a payment carrying a discriminator that
    no swap holds yet is read on every scan, exactly so this function has something to close.

    `resolution_note` SAYS WHO CLOSED IT AND WHY, because a resolved row with no reason is
    indistinguishable from one a person closed by hand, and those two want different follow-up.

    DOES NOT COMMIT; the caller owns the transaction, as everything else on this path does.
    Rows already resolved are left untouched -- `resolved_at IS NULL` in the WHERE -- so a
    note written by a person is never overwritten by this one.
    """
    txids = [t for t in (credited or ()) if t]
    if not txids:
        # Rule 14: an empty result is a result, and the caller reports the count either way.
        return 0
    placeholders = ",".join("?" for _ in txids)
    cursor = db.execute(
        # The interpolation is a run of '?' generated from the LENGTH of the list -- structure,
        # not input. Every txid is bound as a parameter below. Same claim as
        # deposit_service.process_active_swaps() makes for ACTIVE_STATUSES, and checkable from
        # this line (rule 12's S608 note).
        f"UPDATE unattributable_deposits SET resolved_at = ?, resolution_note = ?"  # noqa: S608
        f" WHERE asset = ? AND resolved_at IS NULL AND txid IN ({placeholders})",
        [
            now,
            "credited after all: a deposit_events row exists for this txid, so a swap claimed "
            "it. Closed automatically by services/unattributable_deposit_service."
            "resolve_credited(); no person acted on it.",
            asset,
            *txids,
        ],
    )
    return int(cursor.rowcount or 0)
