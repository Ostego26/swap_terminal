"""What a swap LOOKS like to the person waiting on it. Every decision, one place.

Role: submodule -> function (the display decisions for the end-user surface)
Reads: a swap dict as services/swap_service.get_swap() returns it, plus its
       deposit_events rows. Nothing else -- no database handle, no adapter, no
       clock of its own (the caller passes `now`).
Writes: nothing
Can move funds: no. Nothing here writes a row, and nothing here is read back by
       the deposit watcher or the payout worker. It decides what a HUMAN sees;
       whether a payout is released is decided in services/deposit_service.py
       and services/payout_service.py and is not re-derived here. It does decide
       whether a deposit ADDRESS is rendered at all (_address_problem(), added
       2026-09-27), which is a customer-money decision even though no row moves:
       a rendered address is one somebody pays into.
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

import re

import qr_svg
from chains.solana_pay import payment_uri
from microfortnights import format_duration
from modules.address_authority import check_address

from .helpers import parse_iso
from .swap_service import DEPOSIT_TAG_COLUMN, TAG_ATTRIBUTED_ASSETS, TAG_ATTRIBUTION
from .wallet_menu import any_can_sign, wallets_for

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

# The statuses that are terminal because they are WAITING ON A PERSON.
#
# DERIVED from STATUS_MEANINGS rather than spelled a second time. A status is
# halted exactly when its `kind` says so, which is the same field attention()
# returns as `level` and the same one the templates style -- so a status added
# to the vocabulary above with kind "halted" is picked up by every caller
# without anybody remembering to edit a list. A hand-written second copy is
# rule 8's bug with a delay on it, and this set had already begun to spread:
# services/deposit_service.py writes the status, workers/deposit_watcher.py
# counts it for the HALTED_for_review field on its cycle line, and show_swap.py
# at the repository root lists the rows.
#
# `failed` is NOT in here, and that is the point rather than an oversight. A
# failed swap is one whose PAYOUT could not be broadcast, and the operator
# surface for it is services/admin_view.unresolved_payouts(), which reads the
# `payouts` table -- where the evidence about a possibly-relayed transaction
# lives. `under_review` is the deposit-side halt: nothing was sent, the coins
# are in the deposit account, and what happens next is a decision about money.
# Two different halts with two different pieces of evidence, so folding them
# into one list would produce a report that cannot say what to look at.
#
# Non-empty is pinned by tests/test_show_swap.py. An empty tuple here would turn
# the `IN ()` in admin_view.swaps_with_status() into a SQL syntax error rather
# than a quiet lie, and a test failing is a cheaper way to find that out than an
# operator running the tool during a halt.
HALTED_STATUSES = tuple(status for status, meaning in STATUS_MEANINGS.items() if meaning["kind"] == "halted")

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
# refuses -- and on 2026-09-29 the operator chose the strategy that refusal was
# waiting for: one shared account plus a per-swap Memo instruction, the same
# shape as XRP's with a different field carrying the integer. So SOL is a tag
# chain, and it joins by being in TAG_ATTRIBUTED_ASSETS rather than by an entry
# written here.
#
# A CHAIN MISSING FROM THIS MAPPING IS A DEFECT AND NOT A GAP, and that was
# learned from one. Measured 2026-09-27, a chain absent here rendered
# "not decided in this application -- get_new_address() refuses and the custody
# choice is the operator's" on the operator's chain table, next to an
# `attribution` column reading "unknown" -- while that chain's adapter returned a
# real per-swap address and its custody question was not open at all. The page an
# operator consults to learn how deposits are told apart told them nobody had
# decided. Rule 16 counts a wrong comment as a bug; that was the same bug
# rendered as a fact about live state, because admin_view.chain_rows() forces
# every reachable chain into the table whether or not this mapping knows it.
#
# AND IT HAPPENED A SECOND TIME, TO SOL, WHICH IS WHY THIS PARAGRAPH IS A
# CORRECTION RATHER THAN A WARNING. It used to read "SOL IS THE OPPOSITE CASE AND
# THE DEFAULT SENTENCE IS TRUE OF IT", on the grounds that get_new_address()
# really does raise NotImplementedError there and names the three custody options
# README.md left with the operator. Both halves of that were true on 2026-09-27
# and the conclusion was false by 2026-09-30: the operator chose one of those
# three options on 2026-09-29 and the credit path was built to it, so the page
# was telling an operator the question was open while deposit_service credited
# SOL by tag. The adapter's refusal -- the part a reader can check in one grep --
# stayed true the whole time, which is exactly what made the stale sentence look
# verified. Rule 17: a reason to believe something is not having checked it.
#
# The lesson the first occurrence drew was "the fix had to be an entry in this
# mapping rather than a change to the default branch". That was too small. An
# entry is a copy, and a copy drifts on a schedule; the second occurrence took
# twelve days. The tag entries are DERIVED now, so the default branch is
# unreachable for any chain this application can credit.
#
# A MODEL NAME DESCRIBES A BEHAVIOR, NOT A DERIVATION, and the difference has
# teeth. Two chains can derive their per-swap address completely differently --
# an independent key in wallet.dat versus something scanned for with a view key
# -- and still answer the SAME attribution question: given money that arrived,
# which swap claims it? "The one whose deposit address it was sent to" is one
# model however the address was made, and it is different in kind from XRP,
# where one account is shared and an integer decides.
#
# Every consumer branches on exactly that question and on nothing else:
#
#     deposit_instruction() below        address box, or address + tag pair
#     templates/swap.html:59,64          `deposit.model == 'address'` vs
#                                        `== 'tag'`, else a bare note
#     admin_view.chain_rows()            the `attribution` column
#
# So a model name minted for a derivation would name the same behavior in a word
# none of those three readers knows, and the template would route that chain into
# its `{% else %}` fallback -- printing the note and NO send target, so a customer
# gets a page explaining how attribution works that never shows them where to
# send. Derivation differences therefore live in a sentence, in
# ADDRESS_DERIVATIONS below, rather than in a key.
#
# A chain missing from this mapping renders as "unknown" and says so. It does
# not fall back to "address", because an address field drawn for a chain that
# attributes by tag would invite a customer to send money that can never be
# matched to their swap.
#
# THE MODEL IS `tag`, NOT `destination_tag`, AND THE RENAME IS THE POINT OF THE 2026-09-30
# EDIT. `destination_tag` is the XRP LEDGER's field name, and using it as the model name made
# the model unjoinable by any other tag chain: Solana's discriminator is a Memo instruction and
# the Solana blockchain has no destination tag at all. The chain-specific word therefore moved
# out of the key and into services/swap_service.TAG_ATTRIBUTION, which already carried it per
# asset, and the key says only what the three consumers above actually branch on.
#
# THAT IS NOT A STYLE ARGUMENT -- THE IDENTICAL MISTAKE IS ALREADY RECORDED ONE FILE OVER.
# TAG_ATTRIBUTION's own comment says that before it existed, `deposit_address_for()` raised
# every refusal with the word "DestinationTag" in it, "so a SOL swap would have been refused
# for the absence of an XRP variable, in a sentence naming a field the Solana blockchain does
# not have." This table was the second copy of that same mistake, and it had already drifted.
#
# MEASURED 2026-09-30, which is what forced this: TAG_ATTRIBUTED_ASSETS and TAG_ATTRIBUTION
# both held {"SOL", "XRP"} and this table held XRP alone, so `_attribution_note("SOL")` told
# the operator on their own chain page
#
#     "not decided in this application -- get_new_address() refuses and the custody choice is
#      the operator's"
#
# while services/swap_service.deposit_account() would hand a SOL swap a shared account and a
# memo tag, and chains/solana.py._attributable() credits by that tag. The operator CHOSE the
# one-account-plus-memo strategy on 2026-09-29 (commit d22b2a1, and the clarification that this
# terminal takes no custody beyond brief escrow). The chain was decided; only this mapping had
# not heard -- which is word for word the defect admin_view._attribution_note()'s docstring
# already describes happening to a different chain on 2026-09-27. Second occurrence, same
# table, so it is derived now rather than corrected again.
#
# THE TAG ENTRIES ARE DERIVED AND THE ADDRESS ENTRIES ARE NOT, and the asymmetry is deliberate.
# There is no set of "address-attributed assets" to derive from -- an address chain is simply
# one that answers get_new_address(), which is a property of its adapter and not a list. The
# tag chains ARE a named set, so that set is the authority and a third chain joining it cannot
# reach this file's default branch.
ATTRIBUTION_MODELS = {
    "BTC": "address",
    "LTC": "address",
    "GRC": "address",
    **dict.fromkeys(TAG_ATTRIBUTED_ASSETS, "tag"),
}

# WHO derives the per-swap address, for the chains whose model is "address".
# One sentence each, and they differ, which is the whole reason this is a table
# rather than a single clause inside admin_view._attribution_note().
#
# That clause read "derived by the daemon's getnewaddress" for every chain in the
# address model. It was written when the address model meant "a Bitcoin-derived
# daemon", and it held only for as long as that stayed true: the first chain to
# join the model with any other derivation would turn one true sentence about
# three chains into a false sentence about a fourth. Rule 8: the copies agree on
# the day they are written. Keyed per asset so a chain cannot inherit another
# chain's derivation, and read by admin_view._attribution_note() rather than
# restated there.
#
# Each entry was read out of the adapter, 2026-09-27:
#   BTC/LTC/GRC   chains/base.RPCAdapter.get_new_address() -> `getnewaddress`
ADDRESS_DERIVATIONS = {
    "BTC": "the daemon's `getnewaddress` -- an independent address whose key bitcoind stores in wallet.dat",
    "LTC": "the daemon's `getnewaddress` -- an independent address whose key litecoind stores in wallet.dat",
    "GRC": "the daemon's `getnewaddress` -- an independent address whose key the Gridcoin wallet stores in wallet.dat",
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


def _address_problem(asset: str, address: str | None) -> str:
    """"" when this deposit address can receive money, else the sentence saying it cannot.

    THE LAST BOUNDARY, AND DELIBERATELY THE WEAKEST OF THE THREE. Added 2026-09-27.

    services/swap_service.py refuses an unusable deposit address at CREATION, which is where
    the real guard belongs -- nothing has been written and nothing has been paid. This runs
    much later, over a row that already exists, so it cannot refuse anything: the money may
    already be on its way. What it can do is stop the page RENDERING an address a customer
    would then pay into, and say why.

    Why that is not redundant with the creation guard (rule 8 asks; they differ): a row can
    reach this function that never went through create_swap() -- a database written by an
    older version of this code, a row edited by hand, a restored backup. The creation guard
    protects the future; this protects what is already in the table.

    DECODE ONLY. The NETWORK is deliberately not checked here, and that is the one place this
    module disagrees with the creation guard on purpose. A wrong-network address still
    decodes, so a customer may ALREADY HAVE PAID it -- and blanking it off the page at that
    point hides the only string that would let them find their own transaction. An
    undecodable address cannot have received anything, by construction, so hiding it costs
    nothing and showing it invites a paste into a wallet that will refuse it anyway.

    NO_VALIDATOR renders normally. An unvalidatable chain must not have its deposit page
    broken; modules/address_authority.py's header gives the argument at length.
    """
    if not address:
        return "No deposit address is recorded for this swap."
    verdict = check_address(asset, address)
    if not verdict.refuses:
        return ""
    # Rule 14: the reason is what the reader needs, not the word "invalid". It names the
    # address so an operator reading a pasted page a day later can act on it.
    return (
        f"The {asset} deposit address recorded for this swap CANNOT RECEIVE A DEPOSIT: {verdict.why}. "
        f"Do not send anything. Anything paid to it would be unspendable by anybody -- including you. "
        f"This swap has to be reopened with a working deposit address."
    )


def shouted_discriminator(discriminator: str) -> str:
    """"DestinationTag" -> "DESTINATION TAG". The customer-facing shout, from the field name.

    WHY A FUNCTION AND NOT A FOURTH COLUMN IN TAG_ATTRIBUTION. The shouted form is the field
    name and nothing else, so storing it would be storing the same fact twice -- rule 8's "two
    copies of one rule is a bug with a delay on it", at the scale of one word. It is derived.

    WHY NOT PLAIN .upper(), WHICH IS WHAT THIS DID FOR ABOUT A MINUTE ON 2026-09-30 AND WAS
    WRONG ON THE LIVE CHAIN. `"DestinationTag".upper()` is `DESTINATIONTAG`, one word, and the
    customer's page has said "DESTINATION TAG" since it was written -- so uppercasing the field
    name silently degraded the wording on the only tag chain with enabled pairs, in the
    sentence that tells somebody not to send money yet. The camel-case boundary is where the
    space goes, which also leaves "Memo instruction" alone because it has none.
    """
    return re.sub(r"(?<=[a-z])(?=[A-Z])", " ", discriminator).upper()


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
    shadowed.

    THIS BRANCH IS LIVE, AND THIS DOCSTRING SAID THE OPPOSITE UNTIL 2026-09-26.
    It read "No XRP swap can exist today -- Config.ALLOWED_PAIRS contains no XRP
    pair -- so this branch is exercised by seeded rows in tests/test_swap_view.py
    and by nothing else." Measured by reading config.Config.ALLOWED_PAIRS on
    2026-09-26: it holds ("XRP","GRC") and ("GRC","XRP") alongside the four
    Bitcoin-family pairs, XRP->GRC is the pair being tested, and a real halted
    XRP swap was sitting in `under_review` in the operator's database on the day
    this sentence was corrected. A reader who trusted the old wording would have
    treated the tag branch as unreachable code -- which is rule 16's wrong
    comment: it costs exactly as much as a wrong line of code and is one line to
    fix.
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
            "problem": _address_problem(asset, swap.get("deposit_address")),
        }
    if model == "tag":
        # The MODEL is called `tag` and the COLUMN is `deposit_tag` (the schema's generic name,
        # because the same column carries a Solana, Stellar or Cosmos memo). Read from the
        # shared constant rather than either literal: this line read
        # swap["destination_tag"] until 2026-09-26 and so found nothing, which rendered "NO
        # DESTINATION TAG HAS BEEN ISSUED" for a swap that had one. See
        # services/swap_service.DEPOSIT_TAG_COLUMN.
        #
        # THE MODEL WAS CALLED `destination_tag` UNTIL 2026-09-30, and it was renamed for the
        # reason ATTRIBUTION_MODELS above sets out at length: that is the XRP Ledger's field
        # name, so it named the whole model after one member's vocabulary and no other tag
        # chain could join it without the page telling a Solana customer about a field Solana
        # does not have. The chain-specific word is read per asset below instead.
        tag = swap.get(DEPOSIT_TAG_COLUMN)
        # WHAT THIS CHAIN CALLS THE DISCRIMINATOR, from the one table that knows. XRP's is a
        # DestinationTag; SOL's is a Memo instruction. Nothing here spells either -- a
        # tag-attributed asset with no TAG_ATTRIBUTION entry is impossible by the test in
        # tests/test_solana_adapter.py that pins the two tables to the same key set, and
        # falling back to a generic word would be the guess this rename exists to remove.
        _, discriminator, network = TAG_ATTRIBUTION[asset]
        upper = shouted_discriminator(discriminator)
        return {
            "model": "tag",
            "discriminator": discriminator,
            "network": network,
            "asset": asset,
            "address": swap.get("deposit_address") or "",
            "tag": tag,
            "note": (
                f"{asset} deposits are attributed by {upper}, not by address. The account below is shared by "
                f"every swap, so a payment without the exact tag cannot be matched to yours."
            ),
            # TWO WAYS THIS PAGE CAN SEND MONEY NOWHERE, AND IT ONLY CHECKED ONE UNTIL
            # 2026-09-28. The missing tag was checked; THE ACCOUNT ITSELF WAS NOT.
            #
            # `_address_problem()` landed on 2026-09-27 on the `address` branch above and
            # this branch was left rendering `deposit_address` unexamined -- so a swap with
            # a tag and an undecodable shared account printed the account, printed the tag,
            # and printed no problem at all. That is the worse half of the two: an XRP
            # payment to a non-account is rejected by the ledger if the string is malformed,
            # but a WELL-FORMED account on the wrong ledger, or a truncated one that still
            # decodes, takes the money. And unlike the per-swap address branch, this account
            # is SHARED BY EVERY SWAP -- one bad XRP_DEPOSIT_ACCOUNT is every customer, not
            # one.
            #
            # The tag is reported FIRST when both are wrong. A customer with no tag must not
            # send even to a perfect account (the payment cannot be attributed and the money
            # is ours-but-unclaimable), so it is the instruction that has to reach them;
            # burying it under an address complaint would answer the wrong question.
            # services/swap_service.py still refuses a bad account at creation, which is
            # where the real guard belongs -- this covers the row that is already in the
            # table, exactly as _address_problem()'s own docstring argues for its branch.
            "problem": (
                f"NO {upper} HAS BEEN ISSUED for this swap, so there is nothing safe to send yet. Do not "
                "send to the account without one."
                if tag is None
                else _address_problem(asset, swap.get("deposit_address"))
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


def remaining_seconds(until_iso: str | None, now_iso: str) -> float | None:
    """Seconds UNTIL an ISO timestamp, negative once it has passed, or None if unreadable.

    WHY THIS EXISTS RATHER THAN A REVERSED elapsed_seconds() CALL, found 2026-10-01
    by running a new caller instead of reading it.

    elapsed_seconds() guards its FIRST parameter -- `if not since_iso: return None`
    -- and nothing else. So the idiom for "time left", which is

        elapsed_seconds(now_iso, swap.get("expires_at"))

    puts the NULLABLE value in the slot that has no guard, and a missing timestamp
    reaches datetime.fromisoformat(None) and raises TypeError. That catch is
    deliberately narrow (it takes ValueError only, and its comment says a
    TypeError "is a different defect and should surface"), which is right -- and it
    means the reversed call is a crash rather than a None.

    quote_window() in this same file has used that exact idiom since it was
    written, at the line below, and its `remaining is None` branch returns "This
    swap has no readable quote expiry." That branch is UNREACHABLE for a NULL
    expires_at: such a swap raises out of the function before reaching it, taking
    the /swap/<id> page with it. It is reachable for a malformed non-empty string,
    via the ValueError catch, which is why the branch is not dead -- only the case
    its sentence names is.

    swaps.expires_at is NOT NULL in the schema (db.py), so a row from this
    codebase cannot trip it; a hand-edited or migrated row can, and "the column
    says NOT NULL" is a reason to believe rather than a check (rule 17). The
    second caller, pay_test_deposit.quote_age_line(), reads the same column and
    would have had the identical crash -- which is how this was found -- so the
    fix is one guarded function both use rather than two guards (rule 8).
    """
    if not until_iso:
        return None
    # DELEGATED, NOT NEGATED, and the first draft of this line got that wrong in a
    # way only running it caught. elapsed_seconds(a, b) is b - a, so
    # elapsed_seconds(now_iso, until_iso) is ALREADY until - now: the reversed
    # idiom quote_window() used was arithmetically right all along and only its
    # guard was missing. Negating it flipped every verdict -- a swap five minutes
    # into a ten-minute window printed "the quoted window PASSED" -- which would
    # have told an operator to throw away a live quote.
    #
    # Delegated rather than reimplemented so the parse, the narrow ValueError catch
    # and the None-for-unreadable contract stay in one place.
    return elapsed_seconds(now_iso, until_iso)


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

    A SETTLED SWAP IS A THIRD CASE AND UNTIL 2026-10-05 THIS FUNCTION HAD ONLY
    TWO. It branched on the clock alone, so a swap that had already paid out was
    described by whichever side of the expiry it happened to be on. Measured on
    s_b3ff505b6cac5c15, the first BTC -> LTC swap this terminal completed
    (0.001 BTC in, 1.19550983 LTC out, 18.1s from open to `completed`),
    show_swap.py printed over the finished row:

        quote window    485.2µfn (586.9s)  <- Time left in the quoted rate window.

    586.9 seconds of "time left" on a swap whose rate had been spent eleven
    seconds earlier and whose payout was already irreversible on the LTC chain.
    Both halves of that line are wrong in the same direction -- a countdown reads
    as something a reader can still act on, and a settled swap offers nothing to
    act on. The other side of the clock is no better: the same swap read an hour
    later would have said the window "passed this long ago" and that a deposit
    arriving now would "still be detected and still credited", which on a
    completed swap is an invitation to send money to an address whose swap is
    finished. That is the same defect DEPOSIT_ACCEPTING_STATUSES above was added
    for, in the one place that was still deciding by timestamp instead of by
    status.

    So the status is read FIRST, from TERMINAL_STATUSES in this same module
    rather than a fourth copy of that vocabulary, and `spent` is the answer for
    every settled swap whether or not its expiry parses. `passed` keeps its
    arithmetic meaning -- did the timestamp elapse -- because that is what it has
    always meant and tests assert on it; it is `spent` that says the question no
    longer decides anything.
    """
    remaining = remaining_seconds(swap.get("expires_at"), now_iso)
    status = swap.get("status", "")
    if status in TERMINAL_STATUSES:
        return {
            "known": remaining is not None,
            "passed": remaining is not None and remaining < 0,
            "spent": True,
            "display": "(spent)",
            "note": (
                f"This swap is {status}: the quoted rate was already used and the window decides "
                f"nothing now. It is neither a countdown nor an invitation to deposit."
            ),
        }
    if remaining is None:
        return {
            "known": False, "passed": False, "spent": False,
            "display": "(unknown)", "note": "This swap has no readable quote expiry.",
        }
    if remaining >= 0:
        return {
            "known": True,
            "passed": False,
            "spent": False,
            "display": format_duration(remaining),
            "note": "Time left in the quoted rate window.",
        }
    return {
        "known": True,
        "passed": True,
        "spent": False,
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


def payment_options(swap: dict) -> dict:
    """How a customer can pay this deposit: a request URI, a QR, and a wallet menu.

    ONE FUNCTION so the three stay consistent. The URI is what the QR encodes
    AND what a wallet is handed, so two builders would be rule 8's duplicate on
    the string that decides where money goes.

    EVERY FIELD IS EMPTY FOR A CHAIN WITH NO REQUEST FORMAT, rather than absent.
    A template asking `view.pay.uri` for a GRC swap gets "" and renders nothing;
    a missing key would raise in Jinja's default-undefined and take out the page
    for a chain this feature simply does not cover. Rule 14's "(none) is a
    result" applied to a view dict.

    WHY `uri` CAN BE EMPTY EVEN ON SOL. payment_uri() refuses an absent account
    or memo rather than producing half an instruction, and that refusal is
    caught here: a swap in that state already renders deposit.problem on the
    page, and a second exception from the payment panel would replace a specific
    explanation with a 500.
    """
    asset = swap.get("from_asset", "")
    accepting = swap.get("status", "") in DEPOSIT_ACCEPTING_STATUSES
    empty = {"uri": "", "qr": None, "qr_unavailable": "", "wallets": [], "show_wallets": False}
    if not accepting or asset != "SOL":
        # SOL only, and named rather than derived from the tag model: XRP is also
        # tag-attributed but its deposit request format is an X-address, which
        # services/xrp_tag_service.py refuses to allocate against for reasons
        # written out there. Wiring XRP in is a separate piece of work, and a
        # view that quietly produced a Solana URI for it would be worse than one
        # that produces nothing.
        return empty
    try:
        uri = payment_uri(
            str(swap.get("deposit_address") or ""),
            swap.get("expected_input_amount"),
            swap.get("deposit_tag"),
        )
    except ValueError:
        # CHECKED, and the caller can tell: every field stays empty, so the page
        # renders no payment panel and the deposit panel's own `problem` line is
        # what explains the swap. Not a blind catch -- payment_uri() raises
        # ValueError for exactly two states, both of which that line covers.
        return empty
    return {
        "uri": uri,
        "qr": qr_svg.render(uri),
        "qr_unavailable": "" if qr_svg.is_available() else qr_svg.unavailable_reason(),
        "wallets": wallets_for(asset),
        "show_wallets": any_can_sign(asset),
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
        # HOW TO PAY IT, added 2026-10-01 at the operator's request ("a clicking
        # submenu of wallets ... make the QR code too").
        #
        # Built here rather than in the template so that the one artifact a
        # customer's wallet acts on is assembled in Python, where a test asserts
        # on the characters. chains/solana_pay.py's docstring has the full
        # reasoning: a QR and a wallet request both carry the deposit ADDRESS,
        # and third-party JavaScript assembling either one can retarget it with
        # every server-side check still passing.
        #
        # ONLY WHILE THE SWAP IS ACCEPTING A DEPOSIT. A closed swap gets no
        # payment request and no QR -- templates/swap.html already refuses to
        # show a live-looking deposit target for one (found by looking at a
        # rendered `under_review` swap on 2026-09-26), and a scannable code is
        # the most live-looking target there is.
        "pay": payment_options(swap),
        "window": quote_window(swap, now_iso),
        "on_rail": swap.get("status", "") in STAGE_ORDER,
        "accepting_deposit": swap.get("status", "") in DEPOSIT_ACCEPTING_STATUSES,
    }
