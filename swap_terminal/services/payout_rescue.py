"""May a swap whose payout was refused be handed back to the payout worker? The gate.

Role: submodule (one table and two pure decisions; no socket, no wallet, no key)
Reads: nothing of its own. Callers pass it a swap row and its payout rows.
Writes: swap_terminal.db, through apply_rescue() ONLY -- swaps.status,
       swaps.failed_reason, wallet_inventory's reservation and one swap_audit_log row.
       It does not commit; the caller owns the transaction. The two decision functions
       write nothing at all.
Can move funds: NOT DIRECTLY. It signs nothing and broadcasts nothing. It decides
      whether workers/payout_worker.py may be handed a swap, and that worker
      broadcasts -- so this is one step upstream of a send and a wrong verdict here
      is a double payout.
Mainnet-safe: it chooses no network and opens no socket.

=============================================================================
WHY THIS MOVED OUT OF rescue_payout.py ON 2026-10-10
=============================================================================

It lived in the root tool, which is correct by rule 10 for a CLI -- the decision at
the bottom, callable with seeded inputs -- right up until a SECOND caller appeared.

The operator asked, repeatedly, for the panel to actually control something:

    "i asked for just a fucking control panel and i got a dumbass verbose pile of
     shit that doesn't control anything or tell me anything useful really."
    "no controls. no buttons. nothing."
    "why have you been dancing the fuck around on trols."

and the blocker was not safety, it was LAYERING. docker/web.Dockerfile copies
`swap_terminal/`, `wsgi.py`, `gunicorn.conf.py` and the entrypoint -- and nothing
else. So none of the root operator tools exist inside the web container:

    docker compose exec web python3 rescue_payout.py   ->   no such file

A route could not import this gate because the file holding it is not in the image.
The alternatives were to copy the gate into a view (rule 8's duplicate, on the one
decision in this tree where two copies disagreeing means a double send) or to move
it here, where both callers can reach it. It moved.

ONE IMPLEMENTATION, TWO CALLERS, AND THAT IS THE POINT. rescue_payout.py imports
these names and is otherwise unchanged -- same flags, same output, same refusals.
The route calls the same functions with the same arguments. There is no second
verdict to drift.
"""

from __future__ import annotations

from db import PAYOUT_LIVE_STATUSES
from services.helpers import utc_now_iso

#: Fragments of swaps.failed_reason that PROVE the refusal happened before anything
#: was signed. Matched case-insensitively as substrings.
#:
#: EACH ONE IS A REFUSAL RAISED BEFORE THE SIGNING CALL, verified against the
#: raising site rather than inferred from the wording:
#:
#:   "holds no key that could"     the pre-arming SOL adapter's refusal. The exact
#:                                 string the operator's failed swap recorded, from
#:                                 code that imported nothing able to sign.
#:   SolanaSendNotArmed            chains/solana_signing.require_send_confirmation()
#:                                 raises before load_payout_keypair() is reached,
#:                                 so the key file is not even opened.
#:   "cannot pay out"              chains/registry.why_cannot_pay_out()'s verdict,
#:                                 raised by create_swap/payout before any adapter
#:                                 send method is called.
#:   SolanaClusterRefused          require_devnet() refuses after ONE getGenesisHash
#:                                 and before any balance read, blockhash or submit.
#:
#: DELIBERATELY NOT HERE, and each absence is the point:
#:   SolanaWireMismatch            raised AFTER signing. Nothing was submitted on
#:                                 that path either, but the signed bytes exist and
#:                                 a rescue should be a person's decision, not this
#:                                 file's.
#:   a timeout, a transport error, a bare exception
#:                                 the case this whole file is cautious about. Money
#:                                 may be on chain.
#:   "pynacl is not importable"    ADDED 2026-10-03, after the operator's rescue
#:                                 re-drove the swap and it failed on this instead.
#:                                 PyNaCl is an OPTIONAL dependency on purpose, so a
#:                                 host without it refuses rather than crashing a
#:                                 read-only deposit watcher at import.
#:
#:                                 PROVABLY PRE-BROADCAST AT BOTH RAISE SITES,
#:                                 checked rather than inferred from the wording:
#:                                 chains/solana_signing._public_key_bytes():527 is
#:                                 reached from derive_and_check(), which runs BEFORE
#:                                 sign_message() in signed_transfer_wire(); and
#:                                 sign_message():716 is the signing call itself, so
#:                                 if it raises there are no signed bytes to submit.
#:                                 Either way nothing reached a cluster.
#:   "invalid amount (rpc code -3)"
#:                                 ADDED 2026-10-03, after the first BTC -> LTC swap
#:                                 recorded exactly this and the tool refused to
#:                                 re-drive it. The refusal was correct for its own
#:                                 reason -- an unrecognized message is absence of
#:                                 evidence -- and the evidence exists:
#:
#:                                 RPC CODE -3 IS RPC_TYPE_ERROR, raised by Bitcoin
#:                                 Core's AmountFromValue() while CONVERTING the
#:                                 `amount` parameter. It happens before the wallet
#:                                 is touched, before an input is selected, before a
#:                                 transaction exists -- so there are no signed
#:                                 bytes and nothing can have been relayed.
#:
#:                                 MEASURED, NOT REASONED. The operator ran
#:                                 createrawtransaction with the same amount class
#:                                 and got the identical -3 "Invalid amount" while
#:                                 producing NO transaction, and a valid amount on
#:                                 the same command returned a hex string. That is
#:                                 the parser failing in isolation, with the wallet
#:                                 not involved at all.
#:
#:                                 THE CODE IS PART OF THE MARKER on purpose.
#:                                 "invalid amount" alone could plausibly be some
#:                                 other daemon's wording for a post-broadcast
#:                                 condition; "(rpc code -3)" pins it to the
#:                                 parameter-conversion error, which is the thing
#:                                 that proves nothing was signed. chains/base.py
#:                                 includes the code in the message it records,
#:                                 which is why this can be matched at all.
#:
#:                                 AND THE CAUSE IS FIXED, so this marker is for
#:                                 swaps that failed BEFORE c36250f:
#:                                 chains/coin_amounts.fit_to_chain_precision() now
#:                                 fits the amount to the chain's eight decimals
#:                                 before the send, so a payout no longer reaches
#:                                 the parser with seventeen.
#:   "(rpc code -14)"
#:                                 ADDED 2026-10-07, minutes after the entry
#:                                 below, when s_ebb03e8dc1b96e1c failed a SECOND
#:                                 time -- `Error: The wallet passphrase entered
#:                                 was incorrect. (rpc code -14)`. A mangled paste
#:                                 had armed the container with a fragment of a
#:                                 command instead of the passphrase, so the
#:                                 rescue handed the swap back to a worker that
#:                                 could not unlock the wallet.
#:
#:                                 THE PROOF IS STRUCTURAL AND CLEANER THAN THE
#:                                 UNSET CASE. chains/gridcoin_wallet_lock.
#:                                 unlocked_for_payout() is a generator context
#:                                 manager and its first three statements are:
#:
#:                                     lock(adapter)                       # 258
#:                                     unlock_for_sending(adapter, pass..) # 259
#:                                     try:
#:                                         yield                           # 261
#:
#:                                 -14 is raised by line 259, which is BEFORE the
#:                                 `try` is entered and before `yield`. The body
#:                                 of the `with` -- holding broadcast_payout() --
#:                                 is reached only at that yield, so it cannot
#:                                 have executed.
#:
#:                                 AND THE WALLET WAS LOCKED WHEN IT RAISED, by
#:                                 line 258, one statement earlier. A locked
#:                                 Gridcoin wallet cannot send at all: sendtoaddress
#:                                 answers -13. So even a body that had somehow run
#:                                 could not have broadcast anything.
#:
#:                                 -14 IS RPC_WALLET_PASSPHRASE_INCORRECT, which by
#:                                 definition means the wallet did not open. It is
#:                                 not a send that failed; it is a send that was
#:                                 never possible.
#:
#:   "not set in this process's environment"
#:                                 ADDED 2026-10-07, after the first ICP deposit
#:                                 this system ever credited -- 1.00000000 ICP,
#:                                 block index 1, to the subaccount for
#:                                 s_ebb03e8dc1b96e1c -- had its GRC payout refuse
#:                                 and this tool correctly declined to re-drive it.
#:                                 The refusal was right for its own reason: an
#:                                 unrecognized message is absence of evidence. The
#:                                 evidence exists and is recorded here rather than
#:                                 inferred from the wording, which is what this
#:                                 list demands of every entry.
#:
#:                                 THE RAISE PRECEDES THE BODY, STRUCTURALLY.
#:                                 services/payout_service.py:828 is
#:                                 `with payout_unlock_context(destination_asset,
#:                                 adapter):` and broadcast_payout() is at :835,
#:                                 INSIDE it. payout_unlock_context() is a plain
#:                                 function returning a context manager -- not a
#:                                 @contextmanager generator -- so its
#:                                 `if not passphrase: raise` at :1111 fires while
#:                                 the `with` EXPRESSION is being evaluated, before
#:                                 any context manager exists and therefore before
#:                                 the body can be entered at all. There is no path
#:                                 from that raise to a send.
#:
#:                                 THE EXCEPTION TYPE IS CATEGORICALLY SEPARATE.
#:                                 PayoutUnlockUnavailable's own docstring: "no
#:                                 transaction was created, nothing reached any
#:                                 daemon". It exists as its own class precisely so
#:                                 it cannot be confused with a send that failed.
#:
#:                                 AND THE WORKER LOG SHOWS NO SEND BETWEEN THEM.
#:                                 On the operator's host: the amount-quantization
#:                                 line at 20:26:09,280, then `payout FAILED` at
#:                                 20:26:09,285. Five milliseconds and no RPC.
#:
#:                                 Three independent places, which is the standard
#:                                 this file's header sets. MATCHED ON THE
#:                                 ENVIRONMENT CLAUSE rather than on "GRC payouts
#:                                 need the wallet fully unlocked", because
#:                                 payout_service.py builds that sentence from
#:                                 `{asset}` and WALLET_UNLOCK_ASSETS can grow -- a
#:                                 marker naming GRC would silently stop matching
#:                                 the day a second chain joined, which is rule 8's
#:                                 drift in a safety check.
PRE_SIGNING_MARKERS = (
    "holds no key that could",
    "solanasendnotarmed",
    "cannot pay out",
    "solanaclusterrefused",
    "pynacl is not importable",
    "invalid amount (rpc code -3)",
    "not set in this process's environment",
    "(rpc code -14)",
)


def refused_before_signing(reason: str) -> str:
    """The PRE_SIGNING_MARKERS fragment this reason matches, or "" for none.

    EXTRACTED FROM rescue_verdict() ON 2026-10-03, when a marker was added and
    there was nowhere to test the matching with seeded inputs -- the comparison was
    a list comprehension inside a function that also needs a swap row and payout
    rows. CLAUDE.md rule 10 puts the thing that DECIDES at the bottom, callable on
    its own, and this is the decision the whole file turns on: whether a recorded
    failure is provably pre-broadcast.

    CASE-INSENSITIVE, matching the contract PRE_SIGNING_MARKERS documents. The
    reason text comes from a daemon or an adapter and its capitalization is not
    ours to rely on; the markers are written lowercase for that reason.

    "" RATHER THAN False, so the caller can name WHICH marker matched in its own
    sentence. An operator reading "matches 'invalid amount (rpc code -3)'" can go
    and check that claim; "matched: True" gives them nothing to check.
    """
    lowered = (reason or "").lower()
    for marker in PRE_SIGNING_MARKERS:
        if marker in lowered:
            return marker
    return ""


def rescue_verdict(swap, payout_rows) -> tuple[bool, str]:
    """May this swap be handed back to the payout worker? The decision, as a function.

    Returns (allowed, reason). The reason is printed either way, because a refusal
    an operator cannot read is a refusal they will route around.

    ORDER IS CHEAPEST-AND-MOST-FATAL FIRST, the same shape
    chains/solana_signing.signed_transfer_wire() uses: status, then any txid, then
    the recorded reason. The first refusal is the one reported, so an operator acts
    on one fact rather than three.
    """
    if swap["status"] != "failed":
        return False, (
            f"the swap is '{swap['status']}', not 'failed'. This tool exists for a swap whose payout "
            f"was refused before signing; anything else is either already moving or waiting on a person"
        )
    live = [row for row in payout_rows if row["status"] in PAYOUT_LIVE_STATUSES]
    if live:
        statuses = ", ".join(sorted({row["status"] for row in live}))
        return False, (
            f"{len(live)} payout row(s) are LIVE ({statuses}). A live row means a payout was claimed and "
            f"may be ON CHAIN -- a 'created' row with no txid is exactly the crash-between-send-and-record "
            f"case. Re-driving could double-send. Prove the payment's absence or presence on the "
            f"destination chain first; settle_payout.py is the tool when it WAS delivered"
        )
    with_txid = [row for row in payout_rows if row["txid"]]
    if with_txid:
        return False, (
            f"{len(with_txid)} payout row(s) carry a txid ({with_txid[0]['txid']}). A txid is a "
            f"broadcast. Nothing here may re-drive a swap that has one"
        )
    reason = (swap["failed_reason"] or "").lower()
    if not reason:
        return False, (
            "swaps.failed_reason is empty, so there is no evidence about WHEN the refusal happened. "
            "Absence of a recorded reason is not evidence that nothing was broadcast"
        )
    matched = refused_before_signing(reason)
    if not matched:
        return False, (
            f"the recorded reason is not one this tool can prove happened BEFORE signing, so it refuses: "
            f"a send can raise AFTER the node accepted the transaction. Read the reason and decide by "
            f"hand. Recognized markers are {', '.join(PRE_SIGNING_MARKERS)}. The reason recorded was: "
            f"{(swap['failed_reason'] or '')[:200]}"
        )
    return True, (
        f"the recorded reason matches {matched!r}, which is raised BEFORE anything is signed, and no "
        f"payout row is live or carries a txid. This swap was provably never broadcast"
    )


def apply_rescue(db, swap, reason: str, *, actor: str) -> dict:
    """Hand this swap back to the payout worker. THE WRITE. Does not commit.

    THREE STATEMENTS IN ONE TRANSACTION, and the pairing is the point: either all
    three land or none does. A released reservation on a swap still 'failed' loses the
    record of money owed; a 'payout_pending' swap with the reservation still standing is
    the double-reservation this whole path exists to prevent.

      release the reservation   services/payout_service.py commits the claim, the
                                inventory reservation and the payouts row BEFORE the
                                send, and release_inventory_after_send() runs only on
                                the correction path -- NOT on a failed send. Measured
                                2026-10-03: the failed attempt leaves its amount
                                standing in wallet_inventory.hot_reserved. Hand the swap
                                back without releasing it and payout_worker reserves the
                                same amount a second time, which silently halves the hot
                                wallet's apparent availability for every later payout.
      move the status           guarded `AND status = 'failed'`, so two operators
                                clicking at once produce one transition and the second
                                changes nothing. The web button made that a real race
                                rather than a theoretical one.
      write the audit row       with the actor, because there are now two ways in.

    `actor` NAMES WHICH SURFACE DID IT -- "rescue_payout.py" or the panel. Before the
    button existed every rescue came from one place and the audit row did not need to
    say so; now a row that does not name it cannot be traced back to a person at a shell
    versus a click. Rule 14's "echo the parameters that decide the answer", applied to
    the record rather than to a terminal.

    DOES NOT COMMIT, so the caller owns the transaction -- the CLI commits inside
    db_session, and the route commits after rendering has been decided. Same rule the
    rest of this path follows.

    THE PREVIOUS failed_reason IS CARRIED INTO THE NEW ONE rather than overwritten. It is
    the evidence the verdict was reached from, and a rescue that erased it would leave a
    swap nobody could re-judge.
    """
    now = utc_now_iso()
    asset, amount = swap["to_asset"], float(swap["output_amount_estimate"])
    db.execute(
        "UPDATE wallet_inventory SET hot_reserved = MAX(hot_reserved - ?, 0), "
        "hot_available = hot_confirmed - MAX(hot_reserved - ?, 0), updated_at = ? WHERE asset = ?",
        (amount, amount, now, asset),
    )
    moved = db.execute(
        "UPDATE swaps SET status = 'payout_pending', failed_reason = ?, updated_at = ? "
        "WHERE id = ? AND status = 'failed'",
        (f"re-driven by {actor} on {now}; previous refusal: {swap['failed_reason']}", now, swap["id"]),
    ).rowcount
    db.execute(
        "INSERT INTO swap_audit_log (swap_id, old_status, new_status, message, created_at) "
        "VALUES (?, 'failed', 'payout_pending', ?, ?)",
        (swap["id"], f"{actor}: {reason}", now),
    )
    return {
        "swap_id": swap["id"],
        "moved": bool(moved),
        "released": amount,
        "asset": asset,
        "at": now,
        "actor": actor,
    }
