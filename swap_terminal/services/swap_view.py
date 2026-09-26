"""What a swap LOOKS like to the person waiting on it. Every decision, one place.

Role: submodule -> function (the display decisions for the end-user surface)
Reads: a swap dict as services/swap_service.get_swap() returns it, plus its
       deposit_events rows. Nothing else -- no database handle, no adapter, no
       clock of its own (the caller passes `now`).
Writes: nothing
Can move funds: no. Nothing here writes a row, and nothing here is read back by
       the deposit watcher or the payout worker. It decides what a HUMAN sees;
       whether a payout is released is decided in services/deposit_service.py
       and services/payout_service.py and is not re-derived here.
Mainnet-safe: yes -- pure functions over dicts.

WHY THIS FILE EXISTS AT ALL, AND WHY NONE OF IT IS IN JAVASCRIPT.

The obvious way to build a status page is to fetch /api/swaps/<id> and branch on
`swap.status` in the browser. That is CLAUDE.md rule 8's defect with a browser
attached: the list of statuses would then exist twice -- once in
services/deposit_service.py, which writes them, and once in a `switch` in
static/script.js, which reads them -- and the copies agree on the day they are
written. The day somebody adds a status, the page renders it as nothing at all,
silently, for every customer. So the mapping lives here, in Python, next to the
modules that produce the statuses, and the browser is handed a rendered answer.

THE STATUSES WERE READ OUT OF THE CODE, NOT GUESSED (rule 17).

Measured 2026-09-26 by grepping every status literal written to `swaps.status`
across swap_terminal/ and tests/. Eight, and each is written at exactly one
place:

    awaiting_deposit   services/swap_service.create_swap()
    deposit_seen       services/deposit_service.refresh_swap_from_chain(), rows
                       exist and max_confirmations <= 0
    confirming         same function, 0 < max_confirmations < min_confirmations
    payout_pending     same function, confirmed_total inside the tolerance band
    under_review       same function, confirmed_total OUTSIDE the band
    paying             services/payout_service.claim_swap_for_payout()
    completed          services/payout_service.process_pending_payouts()
    failed             same function, send_to_address() raised

`expired` is NOT one of them, and that mattered enough to check twice. It
appears four times in the tree and every occurrence is in
swap_intents_schema.py / migrate_swap_intents.py, which describe the Express
bridge's OWN intents table -- a different table, a different state machine.
**Nothing in this tree ever sets swaps.status = 'expired'**, even though every
swap row carries an `expires_at` copied from its quote. So this module reports a
passed `expires_at` as a fact about the QUOTE WINDOW and explicitly says the
swap has not been canceled, because claiming otherwise would tell a customer
their coins will not be credited when in fact a deposit arriving now still
credits normally. That is the difference between reading the code and assuming
what an `expires_at` column must do.

UNKNOWN STATUS RENDERS AS UNKNOWN. A status this module has never heard of is
reported with `known=False` and its raw value shown, never folded into the
nearest familiar bucket. Rule 14: "did nothing" must not look like "did work",
and a status nobody has mapped must not look like a status somebody mapped.

WAITING VERSUS STALLED, AND WHERE THE THRESHOLDS COME FROM.

A swap is a long, mostly-silent wait on a blockchain, so "nothing has happened
for a while" is the NORMAL case and cannot be the alarm. What distinguishes a
healthy wait from a stalled one is WHICH state it is waiting in:

  awaiting_deposit   waiting on a person to send coins. No elapsed time makes
                     this abnormal, so there is no stall threshold at all.
  deposit_seen       waiting on the chain for a first confirmation.
  confirming         waiting on the chain for more confirmations.
  payout_pending     waiting on OUR payout worker, which polls every 10s
                     (workers/payout_worker.DEFAULT_POLL_SECONDS). Minutes here
                     means that worker is not running.
  paying             claimed by a payout worker that has not finished. The
                     documented crash window in
                     services/payout_service.process_pending_payouts() lands a
                     swap here with money possibly on chain and no txid
                     recorded, and nothing retries it automatically.

The two chain-side thresholds are deliberately NOT derived from a block time,
because this code does not know one and inventing one would invent precision
(rule 6's reason for never converting confirmations). They are "somebody should
look" horizons and say so in the text they produce.

tests/test_swap_view.py pins the two worker-side thresholds as STRICTLY GREATER
than the poll intervals they are about, by importing those intervals from the
worker modules rather than restating them here. That keeps one authority for the
poll interval: if somebody slows the payout worker to five minutes, the test
fails instead of the page quietly calling every healthy swap stalled.
"""

from __future__ import annotations

from microfortnights import format_duration

from .helpers import parse_iso

# The happy path, in order. This is the rail the customer watches fill, and it
# is a DISPLAY ordering, not a state machine: the transitions are owned by
# services/deposit_service.py and services/payout_service.py.
#
# `under_review` and `failed` are deliberately absent. They are not later stages
# of this sequence, they are departures from it, and drawing them as the next
# step along would tell a customer their swap is progressing when it has halted.
STAGE_ORDER = (
    "awaiting_deposit",
    "deposit_seen",
    "confirming",
    "payout_pending",
    "paying",
    "completed",
)

# Human labels for the rail. Short, because they are rendered under a five-step
# row on a phone-width screen.
STAGE_LABELS = {
    "awaiting_deposit": "Deposit",
    "deposit_seen": "Seen",
    "confirming": "Confirming",
    "payout_pending": "Credited",
    "paying": "Paying out",
    "completed": "Done",
}

# What each status IS, for the reader. `kind` drives the visual treatment and is
# one of five values; the templates must distinguish them by shape and text as
# well as color, because a red/green-only status is unreadable to roughly 8% of
# men. `headline` is what the customer reads first and `detail` is the sentence
# under it, which always says what is being waited ON rather than merely that
# waiting is happening (rule 14: state what the number means, next to it).
STATUS_MEANINGS = {
    "awaiting_deposit": {
        "kind": "waiting",
        "headline": "Waiting for your deposit",
        "detail": (
            "Nothing has arrived yet. Send the exact amount to the deposit target shown above; this page updates "
            "itself while it is open."
        ),
    },
    "deposit_seen": {
        "kind": "working",
        "headline": "Deposit seen, not yet confirmed",
        "detail": "Your transaction is visible to the network but is in no block yet, so it has 0 confirmations.",
    },
    "confirming": {
        "kind": "working",
        "headline": "Confirming on chain",
        "detail": "Blocks are accumulating on top of your deposit. Nothing is sent until the threshold below is met.",
    },
    "payout_pending": {
        "kind": "working",
        "headline": "Deposit credited, payout queued",
        "detail": "Your deposit is fully confirmed and accepted. A payout worker picks this up on its next poll.",
    },
    "paying": {
        "kind": "working",
        "headline": "Sending your payout",
        "detail": "A payout worker has claimed this swap exclusively and is broadcasting the payment.",
    },
    "completed": {
        "kind": "done",
        "headline": "Done -- payout broadcast",
        "detail": "The payout transaction has been relayed to the network. Its id is below.",
    },
    "under_review": {
        "kind": "halted",
        "headline": "Held for review",
        "detail": (
            "The confirmed amount did not match this swap's expected amount within tolerance, so it was HALTED "
            "rather than paid out. Nothing has been sent and nothing has been lost; a human decides what happens "
            "next."
        ),
    },
    "failed": {
        "kind": "failed",
        "headline": "Payout failed",
        "detail": "The payout could not be broadcast. The recorded reason is below.",
    },
}

# The statuses during which sending MORE coins to the deposit target is still a
# sensible thing for a customer to do.
#
# EVERYTHING ELSE MUST NOT DISPLAY A SEND TARGET, and that was a real defect in
# the first version of this page, caught by looking at a rendered `under_review`
# swap on 2026-09-26: the "What to send" panel showed the deposit address with
# "Send exactly 0.01 BTC to" above it, directly over a card explaining that the
# swap had been HALTED because the amount did not match. The page was inviting a
# second deposit into a swap no worker will advance, and a second deposit would
# land on an address whose swap is waiting for a human -- more money to
# reconcile by hand, sent because this page asked for it.
#
# `payout_pending` and `paying` are excluded for a quieter version of the same
# reason: the deposit is already credited and the amount is fixed, so anything
# sent now is a surprise to the tolerance check.
DEPOSIT_ACCEPTING_STATUSES = frozenset({"awaiting_deposit", "deposit_seen", "confirming"})

# Terminal as far as this page is concerned: no worker advances them and no
# amount of elapsed time says anything about them, so they get no stall clock.
TERMINAL_STATUSES = frozenset({"completed", "under_review", "failed"})

# Seconds in a state before the page says "this has taken longer than expected".
# A status absent from this mapping is never called slow -- see the module
# docstring for why awaiting_deposit is absent on purpose.
#
# The two worker-side figures are pinned by tests/test_swap_view.py against the
# workers' own DEFAULT_POLL_SECONDS, which is where the authority for a poll
# interval lives. The two chain-side figures are horizons, not derivations.
STALL_AFTER_SECONDS = {
    "deposit_seen": 7200.0,
    "confirming": 7200.0,
    "payout_pending": 120.0,
    "paying": 300.0,
}

# What the page says when a stall threshold is passed. Separate from the
# threshold so the reason can be specific: "slow" means something different for
# a chain we are waiting on than for a worker of ours that may not be running.
STALL_EXPLANATIONS = {
    "deposit_seen": (
        "Your deposit has been visible but unconfirmed for a while. That is usually a low fee on a busy chain, "
        "not a problem with this swap. Nothing here can speed it up."
    ),
    "confirming": (
        "Confirmations are arriving more slowly than usual. This is the chain's pace, not a fault in this swap."
    ),
    "payout_pending": (
        "Your deposit was credited a while ago and no payout worker has claimed it. That usually means the payout "
        "worker is not running -- it is being looked at, and your deposit is safe and recorded."
    ),
    "paying": (
        "A payout worker claimed this swap and has not reported finishing. It is deliberately NOT retried "
        "automatically, because a payment that may already have been sent must not be sent twice. A human resolves "
        "this one."
    ),
}

# How deposits are attributed to a swap, per chain, because it is not the same
# question on every chain and a UI that renders one shape for all of them is
# lying about at least one.
#
# MEASURED, not assumed. chains/base.RPCAdapter.get_new_address() calls the
# daemon's `getnewaddress`, so BTC/LTC/GRC get a fresh address per swap and the
# address IS the attribution. chains/xrp.XRPAdapter.get_new_address() REFUSES
# and its message says why: the XRP Ledger attributes by an integer DESTINATION
# TAG on one shared account. chains/solana.SolanaAdapter.get_new_address() also
# refuses, and README.md's Solana section leaves the custody choice with the
# operator, so there is no attribution model to draw yet.
#
# A chain missing from this mapping renders as "unknown" and says so. It does
# not fall back to "address", because an address field drawn for a chain that
# attributes by tag would invite a customer to send money that can never be
# matched to their swap.
ATTRIBUTION_MODELS = {
    "BTC": "address",
    "LTC": "address",
    "GRC": "address",
    "XRP": "destination_tag",
}


def threshold_note(asset: str, threshold) -> str:
    """What a min_confirmations number MEANS on this chain. They are not one unit.

    THREE DIFFERENT THINGS SHARE THE NAME `min_confirmations` in this tree, and
    only one of them is a count of blocks:

        BTC / LTC / GRC   blocks mined on top of the deposit
        SOL               a RUNG on Solana's commitment ladder (3 = finalized),
                          which chains/solana_units.py says explicitly is not a
                          block count
        XRP               1, and it means "in a validated ledger" -- the XRP
                          Ledger does not reorganize, so there is no depth to
                          accumulate at all (chains/xrp_units.py refuses any
                          other value at construction)

    Printing `1` beside `6` beside `3` and calling all three "blocks" is rule
    6's unit laundering arriving as a sentence on a page. It was doing exactly
    that on the customer's confirmation panel until 2026-09-26, where the copy
    read "These are blocks, not time" over an XRP swap.

    ONE FUNCTION, TWO READERS. services/admin_view.chain_rows() calls this too,
    rather than carrying its own wording -- two copies of one explanation drift
    the same way two copies of one gate drift (rule 8), and the drift here is a
    sentence that is quietly false about a chain nobody checked.
    """
    if threshold is None:
        return "no threshold is configured for this chain"
    if asset == "SOL":
        return f"rung {threshold} on Solana's commitment ladder (3 = finalized) -- NOT a count of blocks"
    if asset == "XRP":
        return (
            f"{threshold} validated ledger -- the XRP Ledger does not reorganize, so there is no depth to "
            f"accumulate and no number of blocks to wait for"
        )
    return f"{threshold} blocks must be mined on top of the deposit before a payout is released"


def status_meaning(status: str) -> dict:
    """What a status means, or an explicit 'unknown' record for one that is new.

    Returns a dict with `known`, `kind`, `headline`, `detail`. An unrecognized
    status is reported AS unrecognized rather than mapped to the nearest
    familiar bucket: the raw value goes in the headline so whoever is looking at
    the screen can search for it, and the kind is "unknown" so the template can
    style it as neither progress nor completion.
    """
    meaning = STATUS_MEANINGS.get(status)
    if meaning is None:
        return {
            "known": False,
            "kind": "unknown",
            "headline": f"Unrecognized status: {status or '(empty)'}",
            "detail": (
                "This page does not have a description for that status, which means the code that writes it and "
                "the code that displays it have gone out of step. Nothing is inferred from it here."
            ),
        }
    return {"known": True, **meaning}


def stage_rail(status: str) -> list[dict]:
    """The five-step rail, each step marked done / current / future / skipped.

    A status that is not on the rail (under_review, failed, or an unknown one)
    marks every step `skipped` rather than guessing where along the rail the
    swap stopped. The template then draws the rail grayed out beside the halt
    message, which is the honest picture: the sequence is not running.
    """
    if status not in STAGE_ORDER:
        return [{"key": key, "label": STAGE_LABELS[key], "state": "skipped"} for key in STAGE_ORDER]
    position = STAGE_ORDER.index(status)
    rail = []
    for index, key in enumerate(STAGE_ORDER):
        if index < position:
            state = "done"
        elif index == position:
            state = "current"
        else:
            state = "future"
        rail.append({"key": key, "label": STAGE_LABELS[key], "state": state})
    return rail


def confirmation_progress(swap: dict) -> dict:
    """How far the deposit is toward the threshold that releases a payout.

    COUNTS, NEVER CONVERTED (rule 6). `seen` is the highest confirmation count
    across the swap's deposit_events rows and `threshold` is the swap's own
    `min_confirmations` column -- the same value
    services/deposit_service.refresh_swap_from_chain() compares against, read
    from the row rather than recomputed from config, because the row is what
    the gate actually uses and a config change does not rewrite it.

    `source` says where the threshold came from, so the screen answers "why six?"
    without anybody opening a source file (rule 14: echo the parameters that
    decide the answer).

    Returns `rows=0` with `threshold` still populated when no deposit has been
    seen. A zero here is a real measurement and the template prints it as 0/N,
    not as a blank.
    """
    events = swap.get("deposit_events") or []
    threshold = int(swap.get("min_confirmations") or 0)
    seen = max((int(row["confirmations"]) for row in events), default=0)
    remaining = max(threshold - seen, 0)
    return {
        "rows": len(events),
        "seen": seen,
        "threshold": threshold,
        "remaining": remaining,
        "met": threshold > 0 and seen >= threshold,
        "percent": 0 if threshold <= 0 else min(int(100 * seen / threshold), 100),
        "source": (
            f"swaps.min_confirmations for this swap, set from {swap.get('from_asset', '?')}_MIN_CONFIRMATIONS when "
            f"the swap was created"
        ),
        # Per chain, because the number does not mean the same thing on each of
        # them. See threshold_note() for the three units that share this name.
        "meaning": threshold_note(swap.get("from_asset", ""), threshold),
    }


def deposit_instruction(swap: dict) -> dict:
    """WHAT the customer must send, and HOW it gets attributed to this swap.

    Per-chain, because the chains genuinely differ, and the difference is not
    cosmetic: sending to a shared XRP account without the destination tag
    produces money that arrived and a swap that cannot claim it. See
    ATTRIBUTION_MODELS above for how each entry was established.

    For a tag chain the tag is read from the swap row and is None until an
    allocator issues one. That state is reported as `tag_missing`, NOT papered
    over with the deposit_address or with a zero: `0` is a legal DestinationTag
    (README.md's "Tag 0 is a real tag"), so inventing one is how a real tag gets
    shadowed. No XRP swap can exist today -- Config.ALLOWED_PAIRS contains no
    XRP pair -- so this branch is exercised by seeded rows in
    tests/test_swap_view.py and by nothing else.
    """
    asset = swap.get("from_asset", "")
    model = ATTRIBUTION_MODELS.get(asset, "unknown")
    if model == "address":
        return {
            "model": "address",
            "asset": asset,
            "address": swap.get("deposit_address") or "",
            "tag": None,
            "note": f"This {asset} address belongs to this swap alone. Sending to it is what identifies your deposit.",
            "problem": "" if swap.get("deposit_address") else "No deposit address is recorded for this swap.",
        }
    if model == "destination_tag":
        tag = swap.get("destination_tag")
        return {
            "model": "destination_tag",
            "asset": asset,
            "address": swap.get("deposit_address") or "",
            "tag": tag,
            "note": (
                f"{asset} deposits are attributed by DESTINATION TAG, not by address. The account below is shared by "
                f"every swap, so a payment without the exact tag cannot be matched to yours."
            ),
            "problem": (
                ""
                if tag is not None
                else (
                    "NO DESTINATION TAG HAS BEEN ISSUED for this swap, so there is nothing safe to send yet. Do not "
                    "send to the account without one."
                )
            ),
        }
    return {
        "model": "unknown",
        "asset": asset,
        "address": swap.get("deposit_address") or "",
        "tag": None,
        "note": (
            f"How a {asset or 'this chain'} deposit is attributed to a swap is not described in this application, so "
            f"this page will not tell you where to send anything."
        ),
        "problem": f"No deposit attribution model is recorded for {asset or 'this chain'}.",
    }


def elapsed_seconds(since_iso: str | None, now_iso: str) -> float | None:
    """Seconds between an ISO timestamp and `now`, or None if it cannot be read.

    Returns None rather than 0.0 for a missing or unparseable timestamp, because
    0.0 reads as "this just happened" and would make a broken column look like a
    fresh one -- the same failure README.md records for a staging file that sat
    6000s stale while everything reported fine.
    """
    if not since_iso:
        return None
    try:
        return (parse_iso(now_iso) - parse_iso(since_iso)).total_seconds()
    except ValueError:
        # Checked and deliberately narrow: datetime.fromisoformat raises
        # ValueError and nothing else for a malformed string. A broader catch
        # would also swallow a TypeError from a column that is not a string at
        # all, which is a different defect and should surface.
        return None


def quote_window(swap: dict, now_iso: str) -> dict:
    """Whether the quoted window has passed, and what that does NOT mean.

    MEASURED: nothing in this tree sets swaps.status = 'expired', and no worker
    reads swaps.expires_at at all -- grepped across swap_terminal/ on
    2026-09-26; the only `expires_at` read is
    services/swap_service.get_quote_or_raise(), which guards QUOTE reuse before
    a swap exists. So a passed window means the rate was quoted a while ago, and
    it does NOT mean the swap was canceled or that a deposit arriving now is
    lost. Saying the second would be a plausible reading of a column name
    presented as a measurement (rule 17), and it would tell a customer their
    money is gone when it is not.
    """
    remaining = elapsed_seconds(now_iso, swap.get("expires_at"))
    if remaining is None:
        return {"known": False, "passed": False, "display": "(unknown)", "note": "This swap has no readable quote expiry."}
    if remaining >= 0:
        return {
            "known": True,
            "passed": False,
            "display": format_duration(remaining),
            "note": "Time left in the quoted rate window.",
        }
    return {
        "known": True,
        "passed": True,
        "display": format_duration(-remaining),
        "note": (
            "The quoted rate window passed this long ago. Nothing in this system cancels a swap when that happens: "
            "a deposit arriving now is still detected and still credited, and no status is changed by the clock."
        ),
    }


def attention(swap: dict, now_iso: str) -> dict:
    """Is this swap fine, waiting longer than expected, or stopped?

    THE DECISION THIS MODULE EXISTS FOR, and the one rule 14 cares most about:
    a page that renders a healthy five-minute wait identically to a payout
    worker that died four hours ago has told the reader nothing.

    Returns `level` in (ok, waiting, slow, halted, failed, unknown), with a
    headline and a detail that both name what is being waited on. `level` is
    what the template styles; the text is what makes it legible without color.
    """
    status = swap.get("status", "")
    meaning = status_meaning(status)
    kind = meaning["kind"]
    if kind == "unknown":
        return {"level": "unknown", "headline": meaning["headline"], "detail": meaning["detail"], "waited": None}
    if status in TERMINAL_STATUSES:
        level = {"completed": "ok", "under_review": "halted", "failed": "failed"}[status]
        return {"level": level, "headline": meaning["headline"], "detail": meaning["detail"], "waited": None}

    waited = elapsed_seconds(swap.get("updated_at"), now_iso)
    limit = STALL_AFTER_SECONDS.get(status)
    waited_display = None if waited is None else format_duration(waited)
    if limit is not None and waited is not None and waited > limit:
        return {
            "level": "slow",
            "headline": f"{meaning['headline']} -- longer than expected",
            "detail": (
                f"{STALL_EXPLANATIONS[status]} Nothing has changed on this swap for {waited_display}, and this page "
                f"starts saying so after {format_duration(limit)} in this state."
            ),
            "waited": waited_display,
        }
    # WITHIN THE THRESHOLD, THE LEVEL IS THE STATUS'S OWN KIND, and that
    # distinction was missing until 2026-09-26: every non-terminal status
    # returned "waiting", so a swap waiting on a CUSTOMER to send coins and a
    # swap whose confirmations are actively arriving rendered identically. Both
    # are healthy and neither is an alarm, but only one of them is something
    # happening -- and rule 14's "make 'did nothing' look different from 'did
    # work'" is exactly that difference. `waiting` now means nobody is acting;
    # `working` means the chain, or one of our workers, is.
    return {
        "level": meaning["kind"],
        "headline": meaning["headline"],
        "detail": meaning["detail"],
        "waited": waited_display,
    }


def swap_display(swap: dict, now_iso: str) -> dict:
    """Assemble everything the swap page renders. One call, one pass.

    The route handler calls this and passes the result to a template; it holds
    no branch of its own (rule 10), and neither does the template beyond
    choosing a class from `level` and iterating the rail.
    """
    meaning = status_meaning(swap.get("status", ""))
    return {
        "swap": swap,
        "status": swap.get("status", ""),
        "status_known": meaning["known"],
        "kind": meaning["kind"],
        "attention": attention(swap, now_iso),
        "rail": stage_rail(swap.get("status", "")),
        "confirmations": confirmation_progress(swap),
        "deposit": deposit_instruction(swap),
        "window": quote_window(swap, now_iso),
        "on_rail": swap.get("status", "") in STAGE_ORDER,
        "accepting_deposit": swap.get("status", "") in DEPOSIT_ACCEPTING_STATUSES,
    }
