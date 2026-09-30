"""Reading a per-swap reference out of a Solana transaction's Memo instruction.

Role: function level (the decisions -- which instruction is a memo, and what integer it carries)
Reads: nothing. Every value is passed in; this module makes no network call.
Writes: nothing.
Can move funds: no. It reads. What it returns DECIDES which swap a deposit is credited to,
        which is why its refusals are as carefully drawn as its answers.
Mainnet-safe: yes -- pure parsing.
Live-safe: yes.

WHY THIS EXISTS. The operator chose the one-account-plus-memo deposit strategy on 2026-09-29
(README.md, "Solana deposit addresses"). Under it every SOL swap shares ONE deposit account, so
the address no longer identifies the swap and something else must -- exactly the situation
services/deposit_service.attributable_events() already handles for XRP, where the
DestinationTag does the identifying. On Solana that discriminator is a Memo instruction.

THE FAILURE THIS GUARDS AGAINST IS PAYING THE WRONG PERSON. `attributable_events()` says it
plainly: "an uncredited deposit is a support ticket, a misattributed one is somebody else's
money." So every ambiguity here resolves to None, which credits nothing, rather than to a
best guess. A deposit that arrives with no memo, two memos, a non-numeric memo or a memo out of
range is NOT this swap's and is not anybody's until a human says so.

=============================================================================
BOTH PROGRAM IDS HAVE NOW BEEN READ OFF A REAL CLUSTER
=============================================================================

Both constants below started as knowledge rather than measurement -- nothing in the container
they were written in can reach a Solana cluster -- and the admission sat here for five days
because rule 17 says a reason to believe and a measurement must not share a voice. Both are
discharged now, and each one's evidence is stated rather than summarized, because "confirmed"
without the count is the kind of claim this header exists to avoid:

  MEMO_PROGRAM_V2   MEASURED 2026-09-30 on devnet, by the operator running
                    `solana_chain_check.py --hunt-memo 50`. Ten transactions of that program's
                    own recent traffic were read and memo_strings_in() found a memo in all ten
                    -- "HELM_AUTH|v3|open_trade|will-the", several uuids and several hex ids,
                    none of them a decimal integer, so every one correctly resolved to
                    tag=none. getTransaction answered with `parsed` present, which is the
                    jsonParsed shape this module reads. The id is right and the reader works.

  MEMO_PROGRAM_V1   MEASURED 2026-09-30 as well, on the RE-RUN after the throttling was fixed.
                    Fifty of fifty read, zero throttled, zero unreadable, and a memo found in
                    every one -- including two that name themselves: "V1 Memo no signers" and
                    "V1 Memo with signers". The first attempt at this id established nothing
                    (all twenty-two reads refused with HTTP 429, which is evidence about the
                    endpoint and none about the constant); the retry-and-back-off in
                    solana_chain_check.read_one_transaction is what turned that into an answer.

BOTH IDS ARE NOW MEASURED, so the standing instruction this header used to carry is GONE rather
than narrowed. It read: treat a zero-match rate as "the constant is wrong" before treating it as
"nobody uses memos". That was the right default while the ids were hypotheses and it is the
wrong default now -- it would send somebody to change a constant that a hundred real
transactions have confirmed. A zero-match rate is a finding about something ELSE: the encoding,
the CPI path, a transaction version this reader skipped, or genuinely no memo.

WHAT THE HUNDRED TRANSACTIONS ALSO SETTLED, beyond the two ids. Every refusal below fired on
real traffic, which is better evidence than a seeded test can be:

  not a decimal integer   the overwhelming majority. uuids, hex digests, JSON, "smoke",
                          "Auto-Claim", "ISO20022:pacs.008:UETR:...", and several memos
                          carrying an embedded length prefix before their text.
  outside the range       FOUR memos were integers and outside 0..4294967295 -- 1790804868669741040
                          and three siblings, which are unix timestamps in nanoseconds. Somebody
                          really does put a bare integer in a memo, and TAG_MAXIMUM is what
                          keeps one of them from being read as a swap tag.

Neither refusal had ever been exercised on anything but a seeded dict before this run.
"""

from __future__ import annotations

#: The SPL Memo program, v2. The id a memo instruction is expected to name.
#: MEASURED 2026-09-30 on devnet -- 48 of this program's own transactions read across two runs,
#: a memo parsed out of every one. See the header.
MEMO_PROGRAM_V2 = "MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr"

#: The original Memo program. Still accepted by the cluster and still emitted by older wallets,
#: so a reader that knew only v2 would miss a real deposit -- which on this path means a
#: customer's money sitting uncredited.
#:
#: MEASURED 2026-09-30 on devnet, on the second attempt -- 50 of 50 read, a memo in every one,
#: two of them literally named "V1 Memo with signers". The FIRST attempt established nothing
#: about it: all twenty-two reads were refused with HTTP 429, so it came out of a run that
#: confirmed its sibling with nothing said about this one. Same constant, same endpoint, two
#: runs, opposite outcomes -- which is why a throttle is not allowed to read as a finding.
MEMO_PROGRAM_V1 = "Memo1UhkJRfHyvLMcVucJwxXeuD728EqVDDwQDxFMNo"

MEMO_PROGRAM_IDS = (MEMO_PROGRAM_V2, MEMO_PROGRAM_V1)

#: WHICH OF THEM A REAL CLUSTER HAS CONFIRMED, as data rather than as prose.
#:
#: The header above and the comments beside each constant say this in sentences, for a reader.
#: This is the same fact in a form `solana_chain_check.py` can print, so its banner derives the
#: per-id status instead of carrying a hand-written copy. That is not a hypothetical worry:
#: earlier the same day, the operator's deposit-strategy decision existed in five places and
#: four of them went stale, and the fifth was found only because they read it off a screen.
#: One place knows, everything else asks.
#:
#: A MEMBERSHIP SET AND NOT A BOOLEAN PER ID, so adding a third program id cannot forget to
#: declare itself -- an id absent from here reads as unmeasured, which is the safe direction.
MEASURED_MEMO_PROGRAM_IDS = frozenset({MEMO_PROGRAM_V2, MEMO_PROGRAM_V1})

#: The inclusive range a deposit tag may fall in. THE UPPER BOUND IS XRP'S uint32, deliberately,
#: and not because Solana imposes it -- a Solana memo is arbitrary UTF-8 and could carry
#: anything. It is here so one allocator can serve both chains: services/xrp_tag_service.py
#: already allocates in this range, and a SOL tag outside it could not be issued by the same
#: code. Rule 11's one vocabulary, applied to a number instead of a cadence.
TAG_MINIMUM = 0
TAG_MAXIMUM = 0xFFFFFFFF


def memo_strings_in(transaction: dict) -> list[str]:
    """Every memo string in one `getTransaction` response, in instruction order.

    A LIST AND NOT A VALUE, because "how many" is the question the caller has to answer before
    it may use one. A transaction carrying two memos is not a transaction whose memo is the
    first one; it is ambiguous, and deposit_tag_from() refuses it below.

    READS BOTH `message.instructions` AND `meta.innerInstructions`, because a memo emitted by a
    program invoked through CPI appears only in the inner list. A reader that checked only the
    outer one would miss memos from every wallet that routes through a helper program, and
    "miss" on this path means a deposit nobody can attribute.

    PARSED-JSON SHAPE ONLY. The caller must request `jsonParsed`; a base58-encoded instruction
    is not decoded here, and is reported as absent rather than guessed at. That is a real
    limitation and it is named rather than papered over -- see solana_chain_check.py, which
    prints the encoding it actually received.
    """
    message = (transaction or {}).get("transaction", {}).get("message", {}) or {}
    inner_groups = ((transaction or {}).get("meta", {}) or {}).get("innerInstructions") or []
    groups = [message.get("instructions") or []]
    groups.extend(inner.get("instructions") or [] for inner in inner_groups)

    memos: list[str] = []
    for group in groups:
        for instruction in group:
            if not isinstance(instruction, dict):
                continue
            if instruction.get("programId") not in MEMO_PROGRAM_IDS:
                continue
            parsed = instruction.get("parsed")
            # `parsed` is the memo STRING for the memo program, rather than the dict that other
            # parsed instructions carry. Both shapes are accepted because a cluster that ever
            # wraps it would otherwise silently stop producing memos here.
            if isinstance(parsed, str):
                memos.append(parsed)
            elif isinstance(parsed, dict) and isinstance(parsed.get("memo"), str):
                memos.append(parsed["memo"])
    return memos


def deposit_tag_from(transaction: dict) -> tuple[int | None, str]:
    """(the tag, why) for one transaction. None means CREDIT NOTHING, and `why` says which.

    EVERY BRANCH RETURNS A REASON, including the successful one, because this decides whose
    money a deposit is. A caller logging only the failures teaches its reader to skim, and the
    quiet wrong answer here -- a plausible tag parsed out of a memo that meant something else --
    looks exactly like the right one.

    The refusals, and why each is a refusal rather than a best effort:

      no memo        the sender omitted it or their wallet dropped it. The deposit is real and
                     unattributable; a human matches it. Guessing would pay the wrong person.
      two memos      ambiguous by construction. Taking the first would make a sender able to
                     choose whose swap gets credited by appending a second memo.
      not an integer a memo is arbitrary UTF-8 and most memos on the cluster are prose. This is
                     the ordinary case for somebody else's traffic, not an error.
      out of range   a number that cannot be a tag this terminal issued. Refusing it keeps the
                     allocator's range meaningful rather than treating it as advisory.
    """
    memos = memo_strings_in(transaction)
    if not memos:
        return None, "no memo instruction -- unattributable, and a human has to match it"
    if len(memos) > 1:
        return None, (
            f"{len(memos)} memo instructions -- ambiguous, so nothing is credited. Taking the "
            f"first would let a sender choose whose swap gets credited by appending a second"
        )
    text = memos[0].strip()
    if not text.isdigit():
        return None, f"memo is not a decimal integer ({text[:32]!r}) -- ordinary on this cluster"
    tag = int(text)
    if not TAG_MINIMUM <= tag <= TAG_MAXIMUM:
        return None, f"memo {tag} is outside the allocator's range {TAG_MINIMUM}..{TAG_MAXIMUM}"
    return tag, f"memo carries tag {tag}"
