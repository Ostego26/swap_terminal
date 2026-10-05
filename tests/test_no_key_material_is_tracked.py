"""Role: code hygiene (read-only)
Reads: the files git tracks in this repository
Writes: nothing
Can move funds: no
Mainnet-safe: yes

A CLEAN GATE, not a baseline (CLAUDE.md rule 19): the count this asserts is
zero, so there is nothing to ratchet down and nothing to delete later.

TWO GATES, AND THE SECOND ONE EXISTS BECAUSE THE FIRST REPORTED CLEAN WHILE THE
KEY WAS STILL PUBLISHED. Measured 2026-10-05: `git ls-files` -- what the first
test walks -- does not list the file, so that test passed; and
`git ls-tree -r` over every ref found wgrc.json still in the tree of FIVE
branches on origin:

    origin/claude/deposit-vout-migration
    origin/claude/htlc-and-payout-guards
    origin/claude/htlc-client-fixes
    origin/claude/regtest-harness
    origin/claude/server-auth-and-intents

A green suite next to a private key that anyone can `git show` off GitHub is the
exact defect rule 13 names: "did nothing" rendering identically to "did work".
The index is one ref. An exposure lives on all of them.

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


def refs() -> list[str]:
    """Every local and remote-tracking ref, or an empty list if there are none.

    An empty list is a real answer rather than a swallowed failure: a shallow
    or ref-less checkout genuinely has nothing to walk, and the test below says
    so out loud instead of passing silently (rule 14 -- "(none)" is a result).
    """
    result = subprocess.run(
        ["git", "for-each-ref", "--format=%(refname)", "refs/heads", "refs/remotes"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    return [line for line in result.stdout.splitlines() if line]


def small_json_blobs(ref: str) -> list[tuple[str, str]]:
    """(blob_sha, path) for every .json under `ref` small enough to be a keypair.

    Size-filtered from `ls-tree -l` before any content is fetched, for the same
    reason looks_like_an_ed25519_keypair() caps at 2048 bytes: a keypair is
    ~230 bytes, so a larger blob cannot be one and does not need reading. This
    is the difference between one `cat-file` per candidate and one per JSON file
    in history.
    """
    result = subprocess.run(
        ["git", "ls-tree", "-r", "-l", ref],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    out = []
    for line in result.stdout.splitlines():
        # <mode> blob <sha> <size>\t<path> -- size is "-" for non-blobs.
        meta, _, path = line.partition("\t")
        fields = meta.split()
        if len(fields) != 4 or fields[1] != "blob" or not path.endswith(".json"):
            continue
        if not fields[3].isdigit() or int(fields[3]) > 2048:
            continue
        out.append((fields[2], path))
    return out


def blob_is_an_ed25519_keypair(blob_sha: str) -> bool:
    """Same shape test as the on-disk one, against a blob never written to disk.

    Nothing here prints, logs or returns the bytes -- only the verdict. A test
    that proved a key was exposed by echoing it would be the defect it reports.
    """
    result = subprocess.run(
        ["git", "cat-file", "blob", blob_sha],
        cwd=REPO_ROOT,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        return False
    raw = result.stdout
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


def test_no_ref_has_an_ed25519_keypair_in_its_tree():
    """Zero across EVERY ref, not just the index.

    Scoped to ref TIPS rather than to all of history on purpose, and the
    distinction is the whole remediation. A key in an unreachable old commit is
    purged by the branch being rewritten or deleted; a key in a branch TIP is
    one `git show` away for anyone who can clone, today. Those need different
    actions, so conflating them would make the failure unactionable -- and a
    history-wide walk can never reach zero without a rewrite, which would make
    this a permanent red light rather than a gate.
    """
    all_refs = refs()
    offenders = sorted(
        f"{ref}:{path}"
        for ref in all_refs
        for blob_sha, path in small_json_blobs(ref)
        if blob_is_an_ed25519_keypair(blob_sha)
    )
    assert all_refs, (
        "no refs to walk -- this checkout has neither local branches nor "
        "remote-tracking refs, so this gate checked NOTHING. That is not a pass."
    )
    assert not offenders, (
        f"{len(offenders)} ref/path pairs have an ed25519 keypair at the branch TIP, "
        f"out of {len(all_refs)} refs walked:\n  "
        + "\n  ".join(offenders)
        + "\n\nEvery one is readable by anyone who can clone this repository RIGHT NOW."
        "\nMove whatever the key controls FIRST -- a pushed key is published, and"
        "\ndeleting the branch afterward does not un-publish it. Then delete or rewrite"
        "\nthe branches above. Rewriting shared history is the operator's call"
        "\n(CLAUDE.md rule 4), so this gate reports it rather than doing it."
    )
