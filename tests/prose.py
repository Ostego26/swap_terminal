"""Read source as CODE, with the prose removed. The answer to one recurring defect.

Role: test helper (imported by tests; it is not a test itself)
Reads: whatever path a caller hands it
Writes: nothing
Can move funds: no
Mainnet-safe: yes

=============================================================================
WHY THIS MODULE EXISTS, and it is the same bug ten times, in three languages.
=============================================================================

A gate that scans source text for a forbidden pattern matches that pattern in a
COMMENT or a DOCSTRING. So a file gets failed for EXPLAINING the thing it does not
do, and the only ways to satisfy the gate are to stop explaining (making the file
worse to read, to satisfy a grep) or to do the thing the gate forbids.

The occurrences, in order, because the count is the argument:

  1-4  recorded in CLAUDE.md's re-measurement sections, 2026-09/10.
  5    tests/test_daemon_conf.py's SERVICE_SIDE gate failed on testnet_wallets.py,
       which NAMES conf_fallback_settings() in a comment explaining why it
       deliberately does not call it (2026-10-10). code_without_prose() was written
       there, for Python.
  6    a scan counted the Gridcoin passphrase variable DISCUSSED in comments as a
       reader of it.
  7-8  2026-10-11, both in JavaScript and both in gates written the same hour:
       static/script.js was failed for the word "base58" in a sentence about one
       address being substituted for another, and then for `state !== "good"` quoted
       in the comment explaining why the submit gate is a PARAMETER and not that
       comparison. javascript_without_comments() was written for those.
  9    2026-10-11, minutes later: a scan asserting that no customer-facing refusal in
       swap_service.py claims "both chains are reachable" failed on a DOCSTRING
       describing another module's pair states.
  10   2026-11-21, in a THIRD language. A gate asserting that
       docker/harness.Dockerfile verifies a tarball's digest BEFORE extracting it
       compares the character offset of `sha256sum` against the first `tar -x`. The
       comments explaining that ordering name both commands, above the RUN, so the
       raw scan found its own explanation first and would have passed whatever the
       instructions did. dockerfile_without_comments() was written for it.

THE NINTH IS WHY THIS IS A MODULE. code_without_prose() had been copied once, into
tests/test_rpc_connect_timeout.py, with a note saying "If a THIRD copy is ever
wanted, that is the signal to move this into a shared test helper module instead --
two is the most that stays honest." A third was wanted. CLAUDE.md rule 8: two copies
of one rule is a bug with a delay on it, and the delay here was one hour.
"""

from __future__ import annotations

import ast
import pathlib


def code_without_prose(path: pathlib.Path | str) -> str:
    """One Python module's CODE, with every docstring and comment removed.

    An `ast.unparse` of the module with docstrings stripped cannot be satisfied or
    broken by a comment, because comments are not in the tree at all and docstrings
    are removed from every module, class and function body.

    STRING LITERALS IN CODE SURVIVE, and that is the point rather than a limitation: a
    gate asserting what a REFUSAL MESSAGE says must still see the message, which is an
    f-string in a raise. Only the explanatory text goes.

    A body left empty by the strip becomes `pass`, so the unparse stays valid Python.
    """
    tree = ast.parse(pathlib.Path(path).read_text())
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        body = getattr(node, "body", [])
        if (
            body
            and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)
        ):
            node.body = body[1:] or [ast.Pass()]
    return ast.unparse(tree)


def javascript_without_comments(text: str) -> str:
    """`text` with every // and /* */ comment removed, strings left intact.

    A SCANNER AND NOT A PARSE, because there is no ast.unparse for JavaScript here.

    WHAT IT HANDLES AND WHAT IT DOES NOT, stated because a scanner that silently
    mishandles a case is worse than one with a known edge. It tracks single quotes,
    double quotes and template literals, so a `//` or `/*` inside a string survives and
    a quote inside a comment does not open a string. It does NOT track regex literals:
    `/foo/` would have its first slash read as the start of a comment if another slash
    followed. tests/test_live_address_verdict.py pins both -- the behavior claimed here,
    and the absence of any regex literal in the file it scans -- so the limitation
    fails a test rather than sitting in a docstring nobody re-reads.
    """
    out: list[str] = []
    i = 0
    quote = ""
    n = len(text)
    while i < n:
        ch = text[i]
        if quote:
            out.append(ch)
            if ch == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 2
                continue
            if ch == quote:
                quote = ""
            i += 1
            continue
        if ch in "\"'`":
            quote = ch
            out.append(ch)
            i += 1
            continue
        if ch == "/" and i + 1 < n and text[i + 1] == "/":
            while i < n and text[i] != "\n":
                i += 1
            continue
        if ch == "/" and i + 1 < n and text[i + 1] == "*":
            end = text.find("*/", i + 2)
            i = n if end == -1 else end + 2
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def dockerfile_without_comments(text: str) -> str:
    """One Dockerfile's INSTRUCTIONS, with every `#` comment line blanked out.

    THE TENTH OCCURRENCE OF THE DEFECT THIS MODULE'S HEADER COUNTS, 2026-11-21, and
    it arrived as soon as a gate started reasoning about WHERE in a Dockerfile
    something appears rather than merely whether it does.

    tests/test_upstream_daemons_are_pinned_and_verified.py asserts that the
    `sha256sum -c` in docker/harness.Dockerfile comes BEFORE the first `tar -x` of a
    downloaded tarball, by comparing character offsets -- because a verify placed
    after an extraction verifies an archive already unpacked and installed, which
    proves nothing while looking exactly like a check. That Dockerfile's comments
    explain the ordering at length and therefore contain the words "sha256sum" and
    "tar" several times, ABOVE the RUN. Scanned raw, the earliest "sha256sum" is in
    a comment at line 59 and the gate passes no matter where the real one sits. The
    gate would have been measuring its own explanation.

    LINES ARE BLANKED RATHER THAN DELETED, which is the one real design choice here.
    Offsets are what the caller compares, and dropping lines would shift every
    offset after the first comment, so a failure message quoting a position would
    point at the wrong place. Replacing each comment line with an empty line of the
    same count keeps line numbers and leaves offsets monotonic, at the cost of the
    returned text not being byte-identical in length to the original -- which no
    caller needs, because what is compared is the ORDER of two offsets.

    A LINE SCAN AND NOT A PARSE, and the limits are stated rather than discovered.
    Dockerfile comments are whole-line only: `#` is a comment just when it is the
    first non-whitespace character of a line, which is why `install -m 0755` and a
    `${VAR}` containing no comment survive untouched. `#` INSIDE an instruction --
    a shell comment in a RUN, or a fragment in a URL -- is left alone, because
    docker leaves it alone too and hands it to the shell. A continuation line of a
    RUN whose own text begins with `#` is a shell comment, not a docker one, and is
    likewise kept; nothing in this repository has one, and the gates that use this
    would fail safe (seeing more text, not less) if one appeared.
    """
    return "\n".join("" if line.lstrip().startswith("#") else line for line in text.splitlines())
