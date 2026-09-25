"""Role: code hygiene (read-only)
Reads: the files git tracks in this repository
Writes: nothing
Can move funds: no
Mainnet-safe: yes

A CLEAN GATE, not a baseline (CLAUDE.md rule 19): the count this asserts is
zero, so there is nothing to ratchet down and nothing to delete later.

Why it matches on SHAPE rather than on filename. On 2026-09-25 `.gitignore`
carried `*keypair*.json`, which is a guess about what somebody names a secret.
`swap_terminal/grc-sol-swap/abstergo_exchange/wgrc.json` was a Solana keypair --
a bare JSON array of 64 integers, 32 bytes of ed25519 secret followed by its
32-byte public half -- and the pattern did not match it, so it was committed.
Its public key is BGdUSPGWiwStabibSXwiLwJCsk6iXDTeyL6fgWbNcAvN, which is the
destinationSolanaAddress on all three intents in the live store, one of them
already paid 5,560,821 lamports. Anyone with a clone could sweep that account.

A filename pattern fails the moment somebody picks a name nobody predicted.
The 64-integer array is the thing itself and cannot be renamed away from.

This does NOT claim to find every kind of secret. It finds the one shape that
has actually reached this repository, and it says so rather than implying
broader cover (rule 3: a count needs its denominator, and the denominator here
is one shape, not all secrets).
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# A Solana keypair as `solana-keygen` writes it: 32 secret bytes then 32 public.
SOLANA_KEYPAIR_LEN = 64
BYTE_MAX = 255


def tracked_files() -> list[str]:
    # Fixed argv, no shell, and nothing here comes from outside this file.
    # `git` is resolved from PATH on purpose: a pinned absolute path is correct
    # on exactly one machine. S603/S607 are already off for tests/ in
    # pyproject.toml, so a `noqa` here would be an unread claim (rule 19) --
    # and RUF100 said so.
    result = subprocess.run(
        ["git", "ls-files"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    return [line for line in result.stdout.splitlines() if line]


def looks_like_an_ed25519_keypair(path: Path) -> bool:
    """True for a bare JSON array of 64 byte-valued integers, and nothing else.

    Read as bytes and size-capped first: a keypair is ~400 bytes, so anything
    larger cannot be one and does not need parsing. The narrow exceptions are
    the point -- a file this cannot parse is not key material of THIS shape,
    which is a real answer and not a swallowed failure (rule 12, BLE001).
    """
    try:
        raw = path.read_bytes()
    except OSError:
        return False
    if len(raw) > 2048 or not raw.lstrip().startswith(b"["):
        return False
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return False
    return (
        isinstance(parsed, list)
        and len(parsed) == SOLANA_KEYPAIR_LEN
        and all(isinstance(n, int) and 0 <= n <= BYTE_MAX for n in parsed)
    )


def test_no_tracked_file_is_an_ed25519_keypair():
    """Zero, and the failure names every offender at once rather than the first.

    The message says what to do in the order that matters: a key that has been
    pushed is published, so moving the funds comes before removing the file.
    Untracking it is cleanup, not remediation, and reporting it as remediation
    is how a known exposure becomes an unknown one.
    """
    offenders = [
        name for name in tracked_files()
        if looks_like_an_ed25519_keypair(REPO_ROOT / name)
    ]
    assert not offenders, (
        "these tracked files are ed25519 keypairs (64-integer JSON arrays):\n  "
        + "\n  ".join(offenders)
        + "\n\nTreat every one as PUBLIC and move whatever it controls FIRST -- git rm"
        "\nand .gitignore do not un-publish a key that has been pushed. Then untrack"
        "\nit, then purge it from history. Do not delete your only copy before the"
        "\nfunds are somewhere else."
    )
