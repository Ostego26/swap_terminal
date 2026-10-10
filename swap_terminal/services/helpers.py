"""Small shared leaf functions: timestamps and identifiers.

Role: function level (the bottom of rule 10's stack)
Reads: the system clock, os.urandom via `secrets`
Writes: nothing
Can move funds: no
Mainnet-safe: yes

new_id() uses `secrets.token_hex`, not `random`: a swap id is handed to a
client and is the only thing standing between a stranger and
GET /api/swaps/<id>. 8 bytes of CSPRNG output is 64 bits of unguessability.
"""

import secrets
from datetime import UTC, datetime


def utc_now() -> datetime:
    return datetime.now(UTC)


def utc_now_iso() -> str:
    return utc_now().isoformat()


def parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value)


#: Nanoseconds in a second, named because it appears in integer arithmetic below
#: where a bare 1_000_000_000 reads as "some big number".
NANOS_PER_SECOND = 1_000_000_000
NANOS_PER_MICROSECOND = 1_000

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def iso_to_epoch_nanos(value: str) -> int:
    """An ISO timestamp as integer nanoseconds since the Unix epoch.

    WHAT THIS IS FOR, because it is not a general-purpose conversion: the ICP
    ledger deduplicates a `transfer` on
    (from, to, amount, fee, memo, created_at_time) for 24 hours, so
    `created_at_time` IS the idempotency key. chains/icp.send_to_address()
    refuses to send without one, and its docstring says what the value must be:
    "the swap's RECORDED CREATION TIME: real time, captured once when the swap
    row was written, and reused unchanged on every retry." This is the one place
    that turns a `swaps.created_at` string into that number.

    ONE PLACE, AND IT IS NEW (CLAUDE.md rule 8). Measured 2026-10-10: nothing in
    the tree derived this. `fund_desk.py` supplies its own key for a MINT, the
    adapter tests use a fixed constant, and the payout path has never called
    send_to_address() for ICP at all -- chains/icp.py carries `can_spend = False`
    because every call is `--identity anonymous`. So when the payout path is
    armed for ICP it must call THIS, not compute its own: two derivations that
    disagree by any amount produce two different keys for one swap, and a retry
    under the second key is a SECOND TRANSFER rather than a dedup.

    INTEGER ARITHMETIC, NOT `datetime.timestamp()`. The float route is the
    obvious one and it is wrong at this scale:

        datetime.timestamp() returns a float64, ~15-16 significant digits
        epoch nanoseconds today is ~1.79e18, which is 19 digits

    so `round(dt.timestamp() * 1e9)` silently rounds away the low digits, and the
    value it produces depends on the platform's float handling. For a key whose
    whole job is to be byte-identical across retries -- possibly across processes
    and machines -- "almost the same number" is the failure, not an inaccuracy.
    The subtraction below is exact: timedelta holds days, seconds and
    microseconds as ints.

    THE RESULT IS ALWAYS A MULTIPLE OF 1000, and that is worth stating because
    HANDOFF.md section 4 records a key for a live swap -- 1791646582186023936 --
    that is NOT. A `datetime` carries microseconds and no finer, so any key
    derived from `created_at` ends in three zeros. That recorded number therefore
    came from somewhere else (a `time.time_ns()` call in a session, most likely),
    and a retry of that swap through this function would produce a DIFFERENT key
    and a second transfer. For any swap paid through this function from the start,
    retries are idempotent; a swap whose first send used an ad-hoc key has to be
    retried with that same ad-hoc key or not at all.

    NAIVE INPUT IS REFUSED rather than assumed to be UTC. `utc_now()` above is
    `datetime.now(UTC)`, so every timestamp this system writes is offset-aware
    and round-trips through `fromisoformat` aware. A naive value means the string
    came from somewhere else, and guessing its zone would shift the key by up to
    14 hours -- which for a swap more than ten hours old pushes it outside the
    ledger's 24-hour window and gets `TxTooOld`, after the operator has already
    been told the send is idempotent.
    """
    moment = parse_iso(value)
    if moment.tzinfo is None or moment.tzinfo.utcoffset(moment) is None:
        raise ValueError(
            f"{value!r} has no UTC offset. Every timestamp this system writes is "
            f"offset-aware (helpers.utc_now() is datetime.now(UTC)), so a naive value "
            f"came from somewhere else. Guessing its zone would shift the ICP "
            f"idempotency key by up to 14 hours and risk TxTooOld. Nothing was derived."
        )
    delta = moment - _EPOCH
    return (
        (delta.days * 86_400 + delta.seconds) * NANOS_PER_SECOND
        + delta.microseconds * NANOS_PER_MICROSECOND
    )


def new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(8)}"
