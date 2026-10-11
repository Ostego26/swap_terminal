"""Read source as CODE, with the prose removed. The answer to one recurring defect.

Role: test helper (imported by tests; it is not a test itself)
Reads: whatever path a caller hands it
Writes: nothing
Can move funds: no
Mainnet-safe: yes

=============================================================================
WHY THIS MODULE EXISTS, and it is the same bug nine times.
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
