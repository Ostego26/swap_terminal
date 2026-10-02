"""Watch the source chain for a swap's deposit and credit it when confirmed.

Role: submodule -> function (refresh_swap_from_chain is the decision)
Reads: the source chain adapter (listtransactions, getrawtransaction),
       swap_terminal.db (swaps, deposit_events)
Writes: swap_terminal.db (deposit_events, swaps, swap_audit_log)
Can move funds: no broadcast here -- but this is the module that decides a
       deposit is CONFIRMED and moves the swap to `payout_pending`, which is
       the state payout_worker.py broadcasts against. The confirmation
       comparison on the `confirmations >= min_confirmations` line is the
       gate between "somebody sent us coins" and "we send coins back", and a
       confirmations value that is wrong in the low direction stalls a swap
       forever while one that is wrong in the high direction releases a payout
       against an unconfirmed deposit.
Mainnet-safe: yes; read-only with respect to the chain.

The amount tolerance (AMOUNT_TOLERANCE_PCT) sends an out-of-range deposit to
`under_review` rather than crediting or refunding it. That is the right
default: a human decides what happens to a deposit that does not match its
quote.

TWO ROWS FOR ONE (asset, txid) ARE NOW WARNED ABOUT, AND NOTHING ELSE CHANGED.

refresh_swap_from_chain() sums EVERY deposit_events row for the swap, and
db.py's UNIQUE(asset, txid, vout) lets one transaction contribute several rows.
Summing them is correct when they are several real outputs, and it is a DOUBLE
COUNT when one of them is the vout=0 row that
chains/base._extract_matching_vouts() fabricated on every Core 22+ deposit
before 2026-09-25. deposit_vout_artifact.py carries the full mechanism and the
measurement; the short version is that the double count lands the swap in
`under_review` -- a halt rather than a wrong payout, but a swap that has
stopped moving.

Nothing here can tell a fabricated row from a genuine second output, so nothing
here tries. The warning below is a DIAGNOSTIC: it changes no figure, no
threshold and no status, and refresh_swap_from_chain() computes and decides
exactly what it did before. What it removes is the silence -- a condition that
doubles a credited amount had no way of announcing itself, and every instance
of this artifact so far was found by somebody already looking for it.

Resolving the rows is a one-time migration (migrate_deposit_vouts.py at the
repository root), not a permanent branch in this function. A branch here that
skipped or preferred one of the rows would be rule 19's patch: it stops the
symptom being reported instead of stopping the cause existing, and it would
still be running years after the last fabricated row was deleted -- silently
suppressing a genuine two-output deposit.
"""

import logging

# Rootless, the same way chains/base.py reaches script_pub_key.py. No
# sys.path.insert is needed here: this module is only ever importable as
# `services.deposit_service`, which already requires swap_terminal/ to be on
# sys.path for the `services` package itself to resolve.
from deposit_vout_artifact import multi_vout_groups

from .helpers import utc_now_iso
from .swap_service import TAG_ATTRIBUTED_ASSETS, TAG_ATTRIBUTION, set_swap_status
from .unattributable_deposit_service import record as record_unattributable
from .unattributable_deposit_service import resolve_credited, stranded_rows, unattributable_txids, unclaimed_rows

logger = logging.getLogger(__name__)

ACTIVE_STATUSES = ("awaiting_deposit", "deposit_seen", "confirming")


def upsert_deposit_event(db, swap_id: str, asset: str, event: dict):
    existing = db.execute(
        "SELECT * FROM deposit_events WHERE asset = ? AND txid = ? AND vout = ?",
        (asset, event["txid"], int(event["vout"])),
    ).fetchone()
    now = utc_now_iso()
    if existing:
        db.execute(
            "UPDATE deposit_events SET confirmations = ?, last_seen_at = ? WHERE id = ?",
            (int(event["confirmations"]), now, existing["id"]),
        )
        return existing
    db.execute(
        """
        INSERT INTO deposit_events (
            swap_id, asset, txid, vout, address, amount, confirmations,
            first_seen_at, last_seen_at, credited_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            swap_id, asset, event["txid"], int(event["vout"]), event["address"],
            float(event["amount"]), int(event["confirmations"]), now, now, None,
        ),
    )
    return None


def record_what_nobody_can_claim(db, asset: str, adapter) -> int:
    """Put the adapter's unattributable deposits in SQL. Returns how many were new.

    CALLED RIGHT AFTER THE SCAN, where the drops exist. The adapter records them on itself
    during find_deposits_to_address() and -- counted 2026-10-01 -- nothing under
    swap_terminal/ read that attribute: the only consumer in the tree was the diagnostic
    script. So real money arriving in the shared account that no swap could claim reached a
    log line and nothing else, on the live path, which is rule 5's "a measurement that only
    exists in a log is not learning" on the one path where the measurement is somebody's
    deposit.

    hasattr, NOT isinstance OR A FLAG. Three of the four adapters are UTXO chains where the
    deposit ADDRESS identifies the swap, so nothing can be unattributable and they have no
    such attribute -- asking for it by name is the honest test, and it keeps this function
    from needing to know which chains exist. A new tag-attributed adapter is picked up by
    growing the attribute, not by editing a list here (rule 11: one vocabulary, derived).

    IT CLEARS NOTHING. The adapter owns that list's lifetime -- find_deposits_to_address()
    resets it per scan -- and clearing it here would mean two owners for one piece of state,
    with the diagnostic reading an emptied list depending on call order.

    THE RETURN VALUE IS NOT A GATE. Nothing downstream branches on it; the caller discards it
    and the swap's own crediting is computed exactly as before. A stranded deposit is a fact
    about the ACCOUNT and this swap has no claim on it either way, so letting it change this
    swap's outcome would be attributing by proximity -- which is the mistake the whole
    mechanism exists to refuse.
    """
    drops = getattr(adapter, "unattributable_drops", None)
    if not drops:
        return 0
    return record_unattributable(db, stranded_rows(drops, asset), now=utc_now_iso())


def warn_on_multi_vout_rows(swap_id: str, rows) -> None:
    """Say so when one (asset, txid) contributes more than one row to the sum.

    Called with the rows refresh_swap_from_chain() has ALREADY selected, so it
    costs no extra query -- which is why the grouping is the in-memory
    multi_vout_groups() rather than the table-wide SQL beside it in
    deposit_vout_artifact.py. The two are asserted to agree in
    tests/test_deposit_vout_artifact.py, because two implementations of one
    rule agree on the day they are written and drift from then on (rule 8).

    Returns None and touches nothing. It is a diagnostic and has no say in what
    gets credited: extracting it into its own function is what keeps that
    visible, since a caller can see at the call site that the return value is
    not used.

    Vouts are OUTPUT INDICES and confirmations are COUNTS. Neither is ever
    rendered in microfortnights (rule 6); only the durations in this system are.

    HOW OFTEN THIS CAN FIRE, measured from the tree rather than guessed, because
    a warning that repeats forever is one an operator learns to scroll past.
    refresh_swap_from_chain() is reached only through process_active_swaps(),
    which selects ACTIVE_STATUSES -- awaiting_deposit, deposit_seen,
    confirming. Two loops call it: deposit_watcher at DEFAULT_POLL_SECONDS=15
    and reconcile_worker at 60, so an affected swap warns about five times a
    minute WHILE IT IS ACTIVE. It does not stay active: the double count sends
    it to under_review, which is not in ACTIVE_STATUSES, so the swap leaves the
    polled set and the warnings stop on their own. A legitimate two-output
    deposit warns for the same short window and then reaches payout_pending,
    which is also outside ACTIVE_STATUSES. Neither case produces an unbounded
    stream.
    """
    for (asset, txid), group in multi_vout_groups(rows).items():
        vouts = ", ".join(str(int(row["vout"])) for row in group)
        total = sum(float(row["amount"]) for row in group)
        logger.warning(
            "swap %s: transaction %s contributes %d deposit_events rows at vout %s, totalling %.8f %s, and ALL of "
            "them are summed  <- correct if they are several real outputs; a DOUBLE COUNT if one is the vout=0 row "
            "fabricated before 2026-09-25. Nothing here can tell those apart. Run migrate_deposit_vouts.py to see "
            "the rows and decide.",
            swap_id,
            txid,
            len(group),
            vouts,
            total,
            asset,
        )


def attributable_events(events, swap: dict) -> list[dict]:
    """The events that belong to THIS swap. Pure, so it is testable without a chain.

    A no-op for an address-attributed chain: the address already identified the
    swap, so every event is this swap's and the list comes back unchanged.

    For a tag-attributed chain (services/swap_service.TAG_ATTRIBUTED_ASSETS) the
    address identifies the TERMINAL, not the swap, and the event's `vout` carries
    the DestinationTag. Only events whose tag equals this swap's deposit_tag are
    this swap's.

    A swap on a tag-attributed chain with NO deposit_tag credits NOTHING, rather
    than falling back to crediting everything. That combination should be
    impossible -- create_swap() allocates the tag inside the same transaction as
    the INSERT -- but "should be impossible" is not a reason to make the failure
    mode "credit every customer's deposit to this swap". The safe direction when
    attribution is unknown is to attribute nothing: an uncredited deposit is a
    support ticket, a misattributed one is somebody else's money.
    """
    events = list(events or [])
    if swap["from_asset"] not in TAG_ATTRIBUTED_ASSETS:
        return events

    tag = swap.get("deposit_tag")
    if tag is None:
        logger.error(
            "swap %s is on %s, which attributes deposits by tag, but has NO deposit_tag -- "
            "crediting NOTHING rather than crediting every payment to the shared account. "
            "%d event(s) were left unattributed.",
            swap["id"], swap["from_asset"], len(events),
        )
        return []

    # `is not None` and not truthiness: tag 0 is a legal DestinationTag, and
    # `if event.get("vout")` would drop it. Same trap pinned in
    # tests/test_xrp_payments.py.
    #
    # THIS DOES NOT CALL services/xrp_tag_service.swap_id_for_tag(), and the
    # difference is stated here and in that function per rule 8. It answers the
    # same question -- which swap owns this tag -- for a SINGLE arriving tag,
    # with its own account validation and a SELECT per call. This filters a
    # whole event list against ONE swap's already-known tag, so the tag is in
    # hand and no lookup is needed; unclaimed_rows() below needs every allocated
    # tag on an asset and builds a dict from one SELECT for the same reason. Two
    # shapes of one question, not two copies of one rule -- but a third caller
    # wanting the single lookup belongs in that function rather than in a third
    # copy here.
    return [event for event in events if event.get("vout") is not None and event["vout"] == tag]


def skip_txids(db, asset: str) -> frozenset[str]:
    """Every txid a scan of this asset need not read again. TWO sources, one set.

    settled_txids() names transactions that reached a swap and passed its
    confirmation threshold. unattributable_txids() names transactions that reached
    NO swap and were recorded for a human instead. Between them they are every
    transaction whose verdict is final, and each source's own docstring carries the
    argument for why re-reading it changes no decision.

    ONE SET BECAUSE THE ADAPTER ASKS ONE QUESTION. It does not care why a
    transaction is finished with; it cares whether to spend a getTransaction on it.
    Two parameters would make every adapter take an argument shaped around this
    service's table layout.

    THE PARAMETER WAS CALLED `settled_txids` AND THAT NAME IS NOW A LIE -- an
    unattributable transaction is not settled, it is the opposite: nothing was
    credited and somebody still has to act. Renamed through the three adapters in
    the same change rather than left as a correct value under a wrong name, which
    is the wrong-comment-is-a-bug rule applied to an identifier a caller reads.
    """
    return settled_txids(db, asset) | unattributable_txids(db, asset)


def settled_txids(db, asset: str) -> frozenset[str]:
    """Transactions on `asset` with nothing left to teach a scan. One SELECT.

    THE RATE-LIMIT FIX'S OTHER HALF, and it is the half that has to be right.
    chains/solana.py skips any signature in this set, so a txid included here
    will NOT be re-read -- and a txid included WRONGLY is a deposit that stops
    being watched before it is credited.

    THE CONDITION IS `confirmations >= the swap's own min_confirmations`, read
    from the row rather than from config, because min_confirmations is copied
    onto each swap AT CREATION. A swap created when SOL_MIN_CONFIRMATIONS was 1
    must settle at 1 even if the setting is 3 now; using the current config
    would re-read it forever, and using it the other way round would stop
    watching a swap that had not reached its own threshold.

    WHY THIS IS SAFE, from the caller rather than from optimism.
    refresh_swap_from_chain() upserts the scanned events and then reads EVERY
    stored deposit_events row back out of the database, computing seen_total,
    confirmed_total, max_confirmations and every status transition from those
    rows. The scan feeds the upsert and nothing else. A row already at or above
    the threshold cannot change: its amount is fixed and its rank cannot rise
    past the ceiling the gate reads. So skipping its transaction changes no
    decision -- which is a claim tests/test_deposit_rate_limit.py checks by
    running a refresh with the scan returning nothing for a settled deposit and
    asserting the swap still advances.

    A DEPOSIT STILL CONFIRMING IS NEVER IN HERE. That is the whole reason the
    comparison is against the threshold rather than against "has a row".
    """
    rows = db.execute(
        "SELECT DISTINCT d.txid AS txid FROM deposit_events d"
        " JOIN swaps s ON s.id = d.swap_id"
        " WHERE s.from_asset = ? AND d.confirmations >= s.min_confirmations",
        (asset,),
    ).fetchall()
    return frozenset(str(row["txid"]) for row in rows)


def refresh_swap_from_chain(db, config, adapters: dict, swap: dict) -> dict:
    asset = swap["from_asset"]
    adapter = adapters[asset]
    # FINISHED TRANSACTIONS ARE NOT RE-READ. On Solana each one costs a
    # getTransaction call, and re-reading the whole history every 15s
    # rate-limited a real deposit out of being credited on 2026-10-01. See
    # skip_txids() for the two sources and for why skipping either changes no
    # decision, and chains/solana.py for the measurement. Every other adapter
    # accepts the argument and ignores it: their discovery is one call.
    events = adapter.find_deposits_to_address(
        swap["deposit_address"], skip_txids=skip_txids(db, asset)
    )
    record_what_nobody_can_claim(db, asset, adapter)

    # FILTER BY TAG BEFORE CREDITING, and this is a money bug that would only
    # appear once XRP went live. For BTC, LTC and GRC the deposit ADDRESS is the
    # swap -- every event the adapter returns for that address belongs to this
    # swap by construction, so crediting them all is correct.
    #
    # For a tag-attributed chain it is not. Every XRP swap shares ONE account, so
    # find_deposits_to_address() returns every tagged payment made to the whole
    # terminal, and crediting them unfiltered would attribute all of them to
    # whichever swap the worker happened to be refreshing. One customer's deposit
    # credited to another customer's swap, and the ledger says the sender paid
    # exactly what they were told to pay.
    #
    # The event's `vout` carries the DestinationTag -- see
    # chains/xrp_payments.py::_classify(), which puts it there because `vout` is
    # the integer discriminator in the adapter contract -- so the match is
    # event["vout"] against this swap's own deposit_tag.
    #
    # Reported by the tag-allocator work on 2026-09-26 and deliberately left
    # unfixed then, because this is the one function that decides, for every
    # chain, that a deposit is confirmed. Fixed now that XRP is being taken live.
    events = attributable_events(events, swap)
    for event in events:
        upsert_deposit_event(db, swap["id"], asset, event)
    rows = db.execute(
        "SELECT * FROM deposit_events WHERE swap_id = ? ORDER BY id ASC",
        (swap["id"],),
    ).fetchall()
    # Diagnostic only, and placed here rather than lower down so that it is
    # read against the two sums immediately below it -- those are the lines the
    # warning is about. Its return value is unused on purpose (see the
    # function's docstring): nothing about the credit decision depends on it.
    warn_on_multi_vout_rows(swap["id"], rows)
    seen_total = sum(float(row["amount"]) for row in rows)
    confirmed_total = sum(float(row["amount"]) for row in rows if int(row["confirmations"]) >= int(swap["min_confirmations"]))
    max_confirmations = max([int(row["confirmations"]) for row in rows], default=0)
    deposit_txid = rows[0]["txid"] if rows else None
    db.execute(
        "UPDATE swaps SET actual_input_amount = ?, deposit_txid = ?, updated_at = ? WHERE id = ?",
        (seen_total or None, deposit_txid, utc_now_iso(), swap["id"]),
    )
    expected = float(swap["expected_input_amount"])
    tolerance_pct = float(config["AMOUNT_TOLERANCE_PCT"])
    low = expected * (1 - tolerance_pct)
    high = expected * (1 + tolerance_pct)
    current_status = swap["status"]
    if rows and current_status == "awaiting_deposit":
        new_status = "deposit_seen" if max_confirmations <= 0 else "confirming"
        set_swap_status(db, swap["id"], new_status, "Deposit detected", old_status=current_status)
        current_status = new_status
    if rows and 0 < max_confirmations < int(swap["min_confirmations"]) and current_status in {"deposit_seen", "awaiting_deposit"}:
        set_swap_status(db, swap["id"], "confirming", "Deposit is confirming", old_status=current_status)
        current_status = "confirming"
    if confirmed_total > 0:
        if confirmed_total < low or confirmed_total > high:
            if current_status != "under_review":
                db.execute(
                    "UPDATE swaps SET failed_reason = ?, updated_at = ? WHERE id = ?",
                    (f"Confirmed amount {confirmed_total} outside tolerance for expected {expected}", utc_now_iso(), swap["id"]),
                )
                set_swap_status(db, swap["id"], "under_review", "Amount outside tolerance", old_status=current_status)
                current_status = "under_review"
        elif current_status in {"confirming", "deposit_seen", "awaiting_deposit"}:
            db.execute(
                "UPDATE swaps SET credited_at = ?, updated_at = ?, actual_input_amount = ? WHERE id = ?",
                (utc_now_iso(), utc_now_iso(), confirmed_total, swap["id"]),
            )
            db.execute(
                "UPDATE deposit_events SET credited_at = ? WHERE swap_id = ? AND credited_at IS NULL",
                (utc_now_iso(), swap["id"]),
            )
            set_swap_status(db, swap["id"], "payout_pending", "Deposit fully confirmed", old_status=current_status)
    db.commit()
    refreshed = db.execute("SELECT * FROM swaps WHERE id = ?", (swap["id"],)).fetchone()
    return refreshed


def process_active_swaps(db, config, adapters: dict) -> list[dict]:
    # The f-string interpolates a run of '?' generated from the LENGTH of
    # ACTIVE_STATUSES -- structure, not input. The statuses themselves are
    # bound as parameters on the line below. That is what the suppression
    # claims and it is what a reviewer can check from this line (rule 12's
    # S608 note: "a reviewer should be able to see which from the line").
    placeholders = ",".join("?" for _ in ACTIVE_STATUSES)
    swaps = db.execute(
        f"SELECT * FROM swaps WHERE status IN ({placeholders}) ORDER BY created_at ASC",  # noqa: S608
        ACTIVE_STATUSES,
    ).fetchall()
    processed = [refresh_swap_from_chain(db, config, adapters, swap) for swap in swaps]
    reconcile_shared_accounts(db, config, adapters)
    db.commit()
    return processed


def reconcile_shared_accounts(db, config, adapters: dict) -> int:
    """Once per cycle, record what arrived at a shared account that NO swap will credit.

    WHY HERE AND NOT IN refresh_swap_from_chain(). The question is "does any swap claim this
    discriminator", and the per-swap refresh is handed ONE swap -- it cannot answer it. Asking
    it per swap would also be the noise-at-scale shape chains/xrp.py already fixed once: that
    adapter defers its unattributable lines per INSTANCE because find_deposits_to_address runs
    once per active swap over the same account, and two payments printed four lines. This runs
    once per cycle per asset, so one stranded deposit is one row and one log line however many
    swaps are open.

    ONE EXTRA SCAN PER TAG-ATTRIBUTED ASSET PER CYCLE, and that is the cost. With one active
    swap it doubles the scans for that asset; with ten it adds a eleventh. It buys the only
    view of the shared account that exists -- attributable_events() sees one swap's slice and
    discards the rest, which is how money matching no swap came to be filtered by every call
    and recorded by none.

    THE CLAIMED SET IS EVERY SWAP ON THE ASSET, not the active ones. A payment carrying a
    COMPLETED swap's tag is never credited either -- ACTIVE_STATUSES is the filter above, so
    that swap is never refreshed again -- and it is as stranded as one with no tag. Looking
    only for unmatched tags would have missed a whole category, which is what reading
    ACTIVE_STATUSES rather than assuming its contents turned up.

    IT CREDITS NOTHING AND CHANGES NO SWAP. Events matching an ACTIVE swap are left entirely
    alone, even though this function can see them: that swap's own refresh credits them, and a
    second writer deciding the same thing is rule 8's duplicate on the path where the two
    copies disagreeing means somebody is paid twice.
    """
    recorded = 0
    for asset in sorted(TAG_ATTRIBUTED_ASSETS):
        adapter = adapters.get(asset)
        if adapter is None:
            continue
        # THE ACCOUNT COMES FROM CONFIG, AND THE FIRST VERSION TOOK IT FROM AN ACTIVE SWAP.
        # That was wrong twice over. The small half: swap_service.deposit_account() reads it
        # from config (`config.get(variable)` for the TAG_ATTRIBUTION variable), so config is
        # the authority and a swap's deposit_address is a COPY -- my comment claiming the
        # reverse had it backwards.
        #
        # The half that mattered: deriving the ASSET SET from active swaps meant no active swap
        # => no reconciliation at all. Money arriving at the shared account while nothing is
        # open was still invisible, and that is the likeliest way a deposit strands -- a sender
        # who pays late, pays twice, or pays before opening a swap. The feature would have been
        # blind to exactly the case it exists for, and every test passed because they all seeded
        # an active swap. Found when the live table came back empty and I went to say why.
        variable, _name, _network = TAG_ATTRIBUTION[asset]
        address = (config.get(variable) or "").strip()
        if not address:
            # NO CONFIGURED ACCOUNT MEANS NO ACCOUNT TO SCAN, not an error: swap_service
            # refuses to create a swap on this asset while it is empty, so there is nothing
            # that could have arrived for it.
            continue
        # EVERY SWAP ON THE ASSET, active or not -- see unclaimed_events() on why a completed
        # swap still claims its tag.
        claimed = {
            int(row["deposit_tag"]): (row["id"], row["status"])
            for row in db.execute(
                "SELECT id, status, deposit_tag FROM swaps WHERE from_asset = ?"
                " AND deposit_tag IS NOT NULL",
                (asset,),
            ).fetchall()
        }
        # EVERY txid THAT ALREADY HAS A deposit_events ROW FOR THIS ASSET.
        #
        # This is what stops the reconciler calling a freshly credited deposit stranded, and
        # the defect it fixes was measured on the first real SOL deposit this system ever
        # credited (2026-10-01). The loop above refreshes each active swap and credits its
        # deposit, which moves that swap OUT of ACTIVE_STATUSES; then this function ran, saw
        # `payout_pending`, and concluded the payment "will never be credited to it" -- about
        # the very payment that had just been credited. unclaimed_events() has the full
        # measurement.
        #
        # A JOIN rather than two queries: the question is "txids credited on this asset", and
        # deposit_events carries swap_id while the asset lives on swaps (rule 20 -- the join
        # belongs in SQL, where it answers the same way for every reader).
        credited = {
            str(row["txid"])
            for row in db.execute(
                "SELECT DISTINCT d.txid AS txid FROM deposit_events d"
                " JOIN swaps s ON s.id = d.swap_id WHERE s.from_asset = ?",
                (asset,),
            ).fetchall()
        }
        now = utc_now_iso()
        # The same skip as refresh_swap_from_chain's, and it matters MORE here:
        # this scan runs once per cycle whether or not any swap is open, so on a
        # quiet system it was the entire source of the rate limiting.
        #
        # A settled txid is by definition claimed, so leaving it out of `events`
        # cannot make it look stranded -- unclaimed_rows() would have filtered it
        # on `credited` anyway, and the two sets are computed from the same
        # deposit_events rows.
        #
        # AN ALREADY-RECORDED UNATTRIBUTABLE TXID IS THE SAME SHAPE OF SAFE, and it
        # is the half that was missing: record_unattributable() UPSERTs, so a row
        # left out of `events` keeps the row it already has rather than losing it.
        # What changes is last_seen_at and confirmations, which stop advancing --
        # correct, since nothing is looking at the transaction any more, and
        # first_seen_at is the figure a human matching it works from.
        events = adapter.find_deposits_to_address(address, skip_txids=skip_txids(db, asset))
        rows = unclaimed_rows(events, claimed, asset, ACTIVE_STATUSES, credited)
        recorded += record_unattributable(db, rows, now=now)
        # AND CLOSE ANY ROW THAT TURNS OUT TO HAVE BEEN CREDITED. Written before this fix, or
        # written legitimately for a payment whose swap was created afterwards -- either way a
        # stranded row whose txid now has a deposit_events row has been answered, and leaving
        # it open makes the unresolved count a number that only grows.
        resolved = resolve_credited(db, asset, credited, now=now)
        if resolved:
            logger.info(
                "%s: %d stranded deposit row(s) CLOSED because a deposit_events row now exists "
                "for their txid -- they were credited after all and no person needed to act",
                asset, resolved,
            )
    return recorded
