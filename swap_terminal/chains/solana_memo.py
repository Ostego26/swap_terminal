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
THE PROGRAM IDS ARE NOT VERIFIED FROM THIS MACHINE, AND THAT IS SAID HERE RATHER THAN HIDDEN
=============================================================================

The two constants below are written from knowledge, not measured: nothing in this container can
reach a Solana cluster, so no transaction has been read back to confirm that a real memo carries
this program id. That is precisely the shape of error a chain adapter is built with and that a
live read later catches -- 620 seeded tests passing while a wire format was
a hypothesis -- and writing it down is the only thing that keeps a reader from mistaking one for
the other (rule 17).

`solana_chain_check.py --hunt-memo` is the instrument that settles it, on the same principle as
xrp_chain_check.py's --hunt-tag: somebody else's memo transaction on devnet proves the program
id as well as our own would, read-only, sending nothing. UNTIL THAT RUN, treat a zero-match rate
as "the constant is wrong" before treating it as "nobody uses memos".
"""

from __future__ import annotations

#: The SPL Memo program, v2. The id a memo instruction is expected to name.
MEMO_PROGRAM_V2 = "MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr"

#: The original Memo program. Still accepted by the cluster and still emitted by older wallets,
#: so a reader that knew only v2 would miss a real deposit -- which on this path means a
#: customer's money sitting uncredited.
MEMO_PROGRAM_V1 = "Memo1UhkJRfHyvLMcVucJwxXeuD728EqVDDwQDxFMNo"

MEMO_PROGRAM_IDS = (MEMO_PROGRAM_V2, MEMO_PROGRAM_V1)

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
