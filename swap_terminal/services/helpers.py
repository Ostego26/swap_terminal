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


def new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(8)}"
