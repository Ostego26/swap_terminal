"""What the fabricated-vout deposit artifact looks like, and the one place that decides.

Role: submodule -> function (multi_vout_groups() and assess_swap() are the
      decisions; everything above them in migrate_deposit_vouts.py is
      orchestration and printing)
Reads: rows handed to it as arguments, or swap_terminal.db through the one
      SELECT constant below, on a connection the CALLER opened. This module
      opens no database and reads no environment.
Writes: NOTHING. There is no INSERT, UPDATE or DELETE anywhere in this file,
      and that is structural rather than incidental -- the single DELETE that
      resolves the artifact lives in migrate_deposit_vouts.py behind --apply,
      where an operator has to ask for it by row id.
Can move funds: no. It opens no socket, reads no key, signs nothing and
      broadcasts nothing. It DESCRIBES rows whose sum decides whether a payout
      is released (services/deposit_service.py:66-93), which is exactly why
      the describing half and the deleting half are in different files.
Mainnet-safe: yes -- pure functions over dictionaries, plus one SELECT.

================================================================================
THE ARTIFACT
================================================================================

Before 2026-09-25, chains/base.RPCAdapter._extract_matching_vouts() matched a
transaction's outputs on `scriptPubKey.addresses` -- a field DEPRECATED in
Bitcoin Core 0.20 and REMOVED in 22.0. On a Core 22+ node that field is simply
absent, so the list was empty for every output, nothing ever matched, and the
function fell through to the branch beside it that FABRICATES a deposit event:

    {"txid": txid, "vout": 0, "address": address,
     "amount": float(amount),                      <- from listtransactions
     "confirmations": self.get_confirmations(txid)} <- a second RPC

The failure is not an exception and nothing logged it. Every deposit_events row
written against a Core 22+ node therefore carries `vout=0` regardless of which
output the coins actually arrived at.

The fix makes the search find the real output. What that fix does to rows
already in the database is the reason this module exists:

  db.py:110                       UNIQUE(asset, txid, vout)
  deposit_service.py:66-67        seen_total and confirmed_total sum EVERY row
                                  for the swap
  deposit_service.py:87-93        a confirmed_total outside AMOUNT_TOLERANCE_PCT
                                  sets under_review with a failed_reason

A real output at vout=N does not UPDATE the fabricated vout=0 row -- the unique
key differs, so upsert_deposit_event() INSERTS a second row. The swap's total
doubles, lands outside tolerance, and the swap halts in `under_review`.

MEASURED 2026-09-25, by seeding a real SQLite database and running the real
refresh_swap_from_chain() through it (tests/test_deposit_vout_artifact.py::
test_the_double_count_reproduces_through_the_real_refresh):

    before   deposit_events: 1 row  vout=0  amount=1.5
             swaps.status = awaiting_deposit   actual_input_amount = None
    after    deposit_events: 2 rows vout=0 amount=1.5 AND vout=2 amount=1.5
             swaps.status = under_review       actual_input_amount = 3.0
             failed_reason = "Confirmed amount 3.0 outside tolerance for expected 1.5"

It fails SAFE -- a halt, not a wrong payout -- but the swap stops moving and
only an operator can restart it.

================================================================================
THE AMBIGUITY THIS MODULE REFUSES TO RESOLVE
================================================================================

The detection signature is two or more deposit_events rows sharing
(swap_id, asset, txid) with different `vout`. **A genuine deposit can produce
exactly that shape**: one transaction may legitimately pay the same address
twice, and then two real outputs are two real rows and their sum is the real
amount received.

Nothing in the row structure separates the two cases. What separates them is
WHEN the row was written -- a fabricated row predates the deploy that fixed
_extract_matching_vouts(), a real one was written by code that reads real
vouts -- and this module has no way to know when that deploy happened on the
operator's host. `first_seen_at` is reported for exactly that reason and is
never compared against anything here.

So every function below REPORTS. The operator decides. A row is deleted only
when it is named by id on the command line, and only after a dry run has shown
it (migrate_deposit_vouts.py).

================================================================================
WHY THE DETECTION IS SQL AND THE WARNING IS PYTHON, AND WHY THAT IS NOT RULE 8'S DEFECT
================================================================================

There are two implementations of "which rows share one (asset, txid) and
disagree about vout" in this tree, and rule 8 requires that a reader who finds
one is told the other exists:

  AFFECTED_SWAP_ROWS_SQL, below      over the whole deposit_events table, on a
                                     connection, for the migration's report
  multi_vout_groups(), below         over rows ALREADY IN MEMORY, for the
                                     WARNING in deposit_service.
                                     refresh_swap_from_chain()

They genuinely differ, and the difference is the point. The migration scans a
table it has not read; the deposit path has just fetched the swap's rows and
must not pay for a second round trip on every poll to log a diagnostic. Both
live in this file so the two are read side by side, and
tests/test_deposit_vout_artifact.py::test_the_sql_and_the_in_memory_grouping_agree
seeds rows and asserts they return the same groups -- which is the only form of
"these two agree" that survives either one being edited.
"""

# The vout the fabricated branch always invents. It is a constant rather than a
# literal at each site because it is the artifact's whole signature: a group
# with no vout=0 row cannot contain a fabricated row, because the fabricated
# branch could not have written anything else.
SUSPECT_VOUT = 0

# Statuses where the deposit sum no longer decides anything, so rewriting the
# rows under them would be rewriting settled history for no gain. A completed
# swap has already paid out; a failed one has already been recorded as not
# paying out. Neither is re-examined by refresh_swap_from_chain(), whose
# ACTIVE_STATUSES does not contain them.
SETTLED_STATUSES = ("completed", "failed")

# Statuses where the deposit has already been credited and the payout is armed
# or in flight. The sum is not re-evaluated here either, but unlike a settled
# swap this one still has money to move, so it is called out separately rather
# than merged into SETTLED_STATUSES.
PAST_THE_GATE_STATUSES = ("payout_pending", "paying")


# Every deposit_events row belonging to any swap that has at least one
# (asset, txid) group with more than one distinct vout, with the swap's own
# fields joined on.
#
# THE ROWS ARE PER SWAP, NOT PER GROUP, AND THAT IS DELIBERATE.
# deposit_service.refresh_swap_from_chain() sums `WHERE swap_id = ?` -- every
# row for the swap, across every txid. Reporting a total over only the affected
# group would print a number the gate never computes, which is rule 14's defect
# in its worst form: a figure that looks like the one that decided something
# and is not it.
#
# LEFT JOIN, not JOIN. deposit_events has a foreign key to swaps, but
# `PRAGMA foreign_keys` is per-connection and db.connect_db() does not set it
# (only executescript(SCHEMA) does), so an orphaned event row is possible. An
# inner join would silently drop it, and "silently dropped" is indistinguishable
# from "not affected" to whoever reads the output. It is reported instead.
#
# COUNT(DISTINCT vout) rather than COUNT(*): UNIQUE(asset, txid, vout) already
# makes them equal today, and spelling the signature the way the sentence says
# it keeps this query true if that constraint is ever relaxed.
AFFECTED_SWAP_ROWS_SQL = """
SELECT
    e.id                      AS event_id,
    e.swap_id                 AS swap_id,
    e.asset                   AS asset,
    e.txid                    AS txid,
    e.vout                    AS vout,
    e.amount                  AS amount,
    e.confirmations           AS confirmations,
    e.first_seen_at           AS first_seen_at,
    e.last_seen_at            AS last_seen_at,
    e.credited_at             AS credited_at,
    s.status                  AS swap_status,
    s.expected_input_amount   AS expected_input_amount,
    s.actual_input_amount     AS actual_input_amount,
    s.min_confirmations       AS min_confirmations,
    s.failed_reason           AS failed_reason
FROM deposit_events e
LEFT JOIN swaps s ON s.id = e.swap_id
WHERE e.swap_id IN (
    SELECT swap_id
    FROM deposit_events
    GROUP BY swap_id, asset, txid
    HAVING COUNT(DISTINCT vout) > 1
)
ORDER BY e.swap_id, e.txid, e.vout
"""

# Counts for the announce block (rule 14: state the scale before the finding).
SCAN_COUNTS_SQL = """
SELECT
    (SELECT COUNT(*) FROM deposit_events)                          AS deposit_events,
    (SELECT COUNT(*) FROM swaps)                                   AS swaps,
    (SELECT COUNT(*) FROM (
        SELECT swap_id FROM deposit_events
        GROUP BY swap_id, asset, txid
        HAVING COUNT(DISTINCT vout) > 1
    ))                                                             AS affected_groups
"""


def multi_vout_groups(rows) -> dict[tuple[str, str], list[dict]]:
    """Group rows by (asset, txid) and return only the groups with more than one vout.

    Pure, and over rows the caller already has: this is what
    deposit_service.refresh_swap_from_chain() calls to decide whether to warn,
    on rows it has just SELECTed, so that logging a diagnostic costs no extra
    round trip. The table-wide equivalent is AFFECTED_SWAP_ROWS_SQL above and
    the two are asserted to agree in the tests.

    Args:
        rows: any iterable of mappings carrying `asset`, `txid` and `vout`.
            sqlite3 rows under db.dict_factory are mappings, and so are the
            dicts the adapters produce, so this works on both without a shim.

    Returns:
        {(asset, txid): [row, ...]} for every group holding two or more
        DISTINCT vouts, each list ordered by vout. Groups with one vout are
        absent rather than present-and-empty: a caller iterating the result is
        iterating exactly the findings.
    """
    grouped: dict[tuple[str, str], list[dict]] = {}
    for row in rows:
        grouped.setdefault((row["asset"], row["txid"]), []).append(row)
    return {
        key: sorted(group, key=lambda row: int(row["vout"]))
        for key, group in grouped.items()
        if len({int(row["vout"]) for row in group}) > 1
    }


def suspect_rows(rows) -> list[dict]:
    """The rows that COULD be fabricated: vout=0 inside a multi-vout group.

    "Could be", not "are". A vout=0 row that shares its (asset, txid) with
    another vout is the only thing the fabricated branch can have produced --
    but a genuine transaction whose first output pays the deposit address
    produces an identical row. This function narrows the set a human has to
    look at; it does not decide anything about a member of that set.

    A vout=0 row that is ALONE in its (asset, txid) is not returned. It is
    either an ordinary deposit at output 0 or a fabrication that has not yet
    collided with its real counterpart, and in both cases deleting it would
    delete the swap's only record of the deposit.
    """
    found = []
    for group in multi_vout_groups(rows).values():
        found.extend(row for row in group if int(row["vout"]) == SUSPECT_VOUT)
    return sorted(found, key=lambda row: int(row["event_id"]) if "event_id" in row else 0)


def seen_total(rows) -> float:
    """Sum every row's amount -- deposit_service.py:66's `seen_total`, exactly."""
    return sum(float(row["amount"]) for row in rows)


def confirmed_total(rows, min_confirmations: int) -> float:
    """Sum the rows at or past the confirmation threshold -- deposit_service.py:67.

    THIS is the figure the tolerance gate compares, so it is the figure that
    decides whether a swap halts. Reporting only `seen_total` would show the
    operator a number that looks like the one in `failed_reason` and is not
    always it: a row that has not reached min_confirmations is in one and not
    the other.

    Confirmations are counts. They are never converted to microfortnights
    (rule 6): six confirmations is six confirmations.
    """
    return sum(float(row["amount"]) for row in rows if int(row["confirmations"]) >= int(min_confirmations))


def assess_swap(rows: list[dict], tolerance_pct: float) -> dict:
    """Everything the operator needs about one affected swap. Pure; no I/O.

    This is the decision layer (rule 10: the function at the bottom is the
    thing that decides, and it is callable with seeded inputs). It decides what
    to REPORT -- never what to delete.

    Args:
        rows: every deposit_events row for ONE swap, as AFFECTED_SWAP_ROWS_SQL
            returns them, with the swap's fields joined on. Per swap and not
            per group, because the gate sums per swap.
        tolerance_pct: AMOUNT_TOLERANCE_PCT, the fraction either side of
            expected_input_amount that deposit_service.py:87 accepts.

    Returns:
        A dict the caller prints or asserts on. `orphaned` is True when the
        swap row is missing entirely, and then every derived figure is None
        rather than a plausible zero -- a total computed against a missing
        expected amount is a number with nothing behind it, which is the
        failure rule 12 names: the reader cannot tell it from a real answer.
    """
    first = rows[0]
    swap_id = first["swap_id"]
    status = first["swap_status"]
    suspects = suspect_rows(rows)
    suspect_ids = {int(row["event_id"]) for row in suspects}
    kept = [row for row in rows if int(row["event_id"]) not in suspect_ids]

    assessment = {
        "swap_id": swap_id,
        "status": status,
        "orphaned": status is None,
        "settled": status in SETTLED_STATUSES,
        "past_the_gate": status in PAST_THE_GATE_STATUSES,
        "rows": rows,
        "groups": multi_vout_groups(rows),
        "suspects": suspects,
        "suspect_ids": sorted(suspect_ids),
        "expected": None,
        "min_confirmations": None,
        "tolerance_low": None,
        "tolerance_high": None,
        "seen_total": seen_total(rows),
        "confirmed_total": None,
        "confirmed_total_without_suspects": None,
        "seen_total_without_suspects": seen_total(kept),
        "inside_with_suspects": None,
        "inside_without_suspects": None,
        "actual_input_amount": first["actual_input_amount"],
        "failed_reason": first["failed_reason"],
    }

    if assessment["orphaned"]:
        # No swap row, so no expected amount and no threshold. Everything the
        # gate would compute is unknowable, and saying so beats printing zeros.
        assessment["effect"] = suspect_effect(assessment)
        return assessment

    expected = float(first["expected_input_amount"])
    min_confs = int(first["min_confirmations"])
    low = expected * (1 - tolerance_pct)
    high = expected * (1 + tolerance_pct)
    with_suspects = confirmed_total(rows, min_confs)
    without_suspects = confirmed_total(kept, min_confs)

    assessment.update(
        {
            "expected": expected,
            "min_confirmations": min_confs,
            "tolerance_low": low,
            "tolerance_high": high,
            "confirmed_total": with_suspects,
            "confirmed_total_without_suspects": without_suspects,
            # deposit_service.py:88 only applies the tolerance test when
            # confirmed_total > 0, so a zero total is not "outside tolerance",
            # it is "nothing confirmed yet". Reproducing that exactly matters:
            # calling 0.0 out of range would tell an operator a swap is halted
            # when it is merely waiting.
            "inside_with_suspects": with_suspects <= 0 or low <= with_suspects <= high,
            "inside_without_suspects": without_suspects <= 0 or low <= without_suspects <= high,
        }
    )
    # Computed last, and from the assessment rather than from the rows, because
    # it is a comparison of the two totals above and nothing else. Defined
    # below this function; Python resolves the name at call time.
    assessment["effect"] = suspect_effect(assessment)
    return assessment


# What removing the vout=0 row(s) would do to the figure the gate compares.
# These are the four outcomes and they are named rather than spelled as
# booleans at each print site, because the third one is the dangerous one and a
# reader has to be able to grep for it.
EFFECT_CORRECTS = "corrects"  # outside tolerance -> inside. The artifact's signature.
EFFECT_STILL_OUTSIDE = "still-outside"  # outside -> outside. Something else is also wrong.
EFFECT_WOULD_BREAK = "would-break"  # inside -> outside. A legitimate two-output deposit.
EFFECT_NO_CHANGE = "no-change"  # inside -> inside, or nothing confirmed yet.


def suspect_effect(assessment: dict) -> dict:
    """What deleting the vout=0 row(s) would do, and whether to OFFER the deletion.

    Pure, and the single most important decision in this module -- it is what
    keeps a legitimate two-output deposit from being handed to an operator as
    something to delete.

    THE DISCRIMINATOR IS NOT A GUESS, and it is worth being precise about what
    it does and does not establish. It cannot tell a fabricated row from a real
    one; nothing can, from the rows alone. What it CAN measure is the direction
    the deletion would move the swap:

      a fabricated row DOUBLES a total that was correct, so removing it moves
      the confirmed total from OUTSIDE the tolerance window back INSIDE. That
      is the artifact's whole signature and it is what `corrects` means.

      a genuine second output CONTRIBUTES to a total that is already correct,
      so removing it moves the confirmed total from INSIDE to OUTSIDE. A
      deletion there does not fix a halt; it CREATES one, and it under-credits
      a customer who sent the full amount -- which is the failure direction
      that pays somebody less than they sent.

    So `would-break` is never offered. It is printed, loudly, with both
    figures, and an operator who names that row anyway is doing so against a
    sentence that says what it will do. That is the line this module draws:
    it refuses to SUGGEST, it does not refuse to OBEY, because a genuinely
    short real deposit under a fabricated row produces `still-outside` and
    blocking that outright would leave a real artifact unfixable.

    `still-outside` IS offered, with the note that the swap stays halted: the
    fabricated row is still fabricated, and a deposit that is genuinely short
    of its quote is exactly what `under_review` is for.

    Returns {"effect": one of the constants above, "offered": bool,
             "note": a sentence for the operator}.
    """
    if not assessment["suspect_ids"] or assessment["orphaned"]:
        return {"effect": EFFECT_NO_CHANGE, "offered": False, "note": ""}
    if assessment["settled"]:
        # Checked FIRST, before the totals are compared at all. A settled swap
        # can perfectly well show the `corrects` shape -- s_done in the test
        # fixture does -- and offering it on that basis would put a completed
        # swap's row into the copy-pasteable command beside a genuine finding,
        # where deletion_plan() would then refuse it and the operator would be
        # reading two lines of one screen that disagree.
        return {
            "effect": EFFECT_NO_CHANGE,
            "offered": False,
            "note": (
                f"REFUSED. This swap is {assessment['status']}: the sum decides nothing for it any more, and "
                f"rewriting the history of a swap that has already settled is worse than leaving an artifact "
                f"in it. deletion_plan() will not accept these ids at all."
            ),
        }

    before = assessment["confirmed_total"]
    after = assessment["confirmed_total_without_suspects"]
    inside_before = assessment["inside_with_suspects"]
    inside_after = assessment["inside_without_suspects"]

    if not inside_before and inside_after:
        return {
            "effect": EFFECT_CORRECTS,
            "offered": True,
            "note": f"removing it moves the confirmed total from OUTSIDE to INSIDE tolerance ({before:.8f} -> {after:.8f})",
        }
    if not inside_before and not inside_after:
        return {
            "effect": EFFECT_STILL_OUTSIDE,
            "offered": True,
            "note": (
                f"removing it does NOT bring the swap back inside tolerance ({before:.8f} -> {after:.8f}). "
                f"The row may still be fabricated AND the real deposit short of its quote -- which is what "
                f"under_review is for. Check the transaction on chain before deleting."
            ),
        }
    if inside_before and not inside_after:
        return {
            "effect": EFFECT_WOULD_BREAK,
            "offered": False,
            "note": (
                f"NOT OFFERED. The confirmed total is ALREADY INSIDE tolerance and removing this row would take "
                f"it OUTSIDE ({before:.8f} -> {after:.8f}), halting a swap that is currently correct and "
                f"under-crediting a customer who sent the full amount. That is the shape of one transaction "
                f"paying this address twice, not the shape of the artifact."
            ),
        }
    return {
        "effect": EFFECT_NO_CHANGE,
        "offered": False,
        "note": (
            f"NOT OFFERED. Removing it does not change the figure the gate compares ({before:.8f} -> "
            f"{after:.8f}) -- the row is below min_confirmations, or nothing is confirmed yet. There is "
            f"nothing here for a deletion to fix."
        ),
    }


def group_rows_by_swap(rows) -> dict[str, list[dict]]:
    """Split the flat result of AFFECTED_SWAP_ROWS_SQL into one list per swap.

    Assembly, not a decision -- it holds no rule and makes no comparison. It is
    here rather than in the entry point only so that assess_swap()'s caller and
    its tests build their input the same way.
    """
    by_swap: dict[str, list[dict]] = {}
    for row in rows:
        by_swap.setdefault(row["swap_id"], []).append(row)
    return by_swap
