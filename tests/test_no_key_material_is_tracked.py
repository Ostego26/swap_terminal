"""Role: code hygiene (read-only)
Reads: the files git tracks in this repository
Writes: nothing
Can move funds: no
Mainnet-safe: yes

A CLEAN GATE, not a baseline (CLAUDE.md rule 19): the count this asserts is
zero, so there is nothing to ratchet down and nothing to delete later.

TWO GATES, because the index is one ref and a branch you can push is another.
The second walks every LOCAL branch's tree, so a keypair committed on a side
branch cannot be pushed just because it is absent from the branch you happen to
have checked out.

REMOTE-TRACKING REFS ARE DELIBERATELY EXCLUDED, AND THE REASON IS AN ERROR MADE
HERE ON 2026-10-05 THAT THIS COMMENT EXISTS TO PREVENT REPEATING. The first
version of the second gate walked `refs/remotes` as well, found wgrc.json in the
tree of five `origin/claude/*` refs, and reported -- in a commit message, with
the branch names listed -- that a private key was readable off GitHub right then.
It was not. Measured minutes later with `git ls-remote --heads origin`:

    112b54b3  refs/heads/claude/xrp-adapter
    e7f02b29  refs/heads/main

TWO branches, and `git merge-base --is-ancestor` says NEITHER contains b3aa36a,
the commit that added the key. The five branches had been deleted from origin;
`git ls-tree origin/claude/htlc-client-fixes` on the operator's host answers
"fatal: Not a valid object name". docs/key_exposure_runbook.md was right and the
cleanup had already happened.

What the walk actually measured was THIS CLONE'S STALENESS. A remote-tracking ref
is a cached copy of what origin served whenever someone last fetched; it is not a
statement about origin. So the gate was backwards in both directions: it raised a
false alarm on any checkout with stale refs, and a fresh clone would pass it even
if the remote genuinely were exposed. A test that reports on the wrong subject
is worse than no test, because it spends the reader's trust.

The remote is a NETWORK question and does not belong in a unit test. Checking it
is `git ls-remote --heads origin` plus `git merge-base --is-ancestor`, run
deliberately, against the live remote -- which is what should have been run
before the alarm, not after.

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
    """Every LOCAL branch, or an empty list if there are none.

    refs/heads only. See the module docstring for why refs/remotes is excluded:
    a remote-tracking ref describes when this clone last fetched, not what the
    remote serves, and walking it reported a false exposure once already.

    An empty list is a real answer rather than a swallowed failure: a detached
    or ref-less checkout genuinely has nothing to walk, and the test below says
    so out loud instead of passing silently (rule 14 -- "(none)" is a result).
    """
    result = subprocess.run(
        ["git", "for-each-ref", "--format=%(refname)", "refs/heads"],
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


def test_no_local_branch_has_an_ed25519_keypair_in_its_tree():
    """Zero across every local branch, not just the index.

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
        "no local branches to walk, so this gate checked NOTHING. That is not a "
        "pass -- say which it was (rule 17)."
    )
    assert not offenders, (
        f"{len(offenders)} branch/path pairs have an ed25519 keypair at the branch "
        f"TIP, out of {len(all_refs)} local branches walked:\n  "
        + "\n  ".join(offenders)
        + "\n\nThese are LOCAL. Whether any of them is also on the remote is a separate"
        "\nquestion this test does not answer and must not be assumed either way:"
        "\n    git ls-remote --heads origin"
        "\n    git merge-base --is-ancestor <the commit that added it> <each served sha>"
        "\nIf it IS served, move whatever the key controls FIRST -- a pushed key is"
        "\npublished, and deleting the branch afterward does not un-publish it."
        "\nRewriting shared history is the operator's call (CLAUDE.md rule 4)."
    )
