#!/usr/bin/env python3
"""xrp_balances.py cannot move money, and its empty cases say they are empty.

Role: tests (read-only)
Reads: xrp_balances.py's source, as text and as an AST. No chain, no network.
Writes: nothing
Can move funds: no
Mainnet-safe: yes

TWO THINGS ARE WORTH PINNING HERE and the rest of the script is orchestration
over a server this suite cannot reach:

  THE SAFETY CLAIM   the module header says "Can move funds: NO", and that
                     sentence is the one an operator reads before running it
                     against an endpoint. It is checked by AST rather than by
                     grepping for a word, because the header itself contains the
                     words "send_to_address" and "submit" while explaining that
                     it does not call them -- a text search would match its own
                     documentation. That exact mistake was made and fixed
                     earlier on 2026-09-29 in a different hygiene test, which
                     searched modules/script_leg.py for "createhtlc" and matched
                     the prose saying why it is not called.
  THE EMPTY CASES    rule 14: an account with no escrows must not render
                     identically to an escrow list that could not be read. Both
                     branches are reachable without a server because
                     escrows_held() returns ([], reason) instead of raising.
"""

from __future__ import annotations

import ast
import pathlib
import sys

import pytest

SOURCE = pathlib.Path(__file__).resolve().parent.parent / "xrp_balances.py"
TREE = ast.parse(SOURCE.read_text())

sys.path.insert(0, str(SOURCE.parent))

# The path insert above has to run first: xrp_balances.py lives at the project
# root, which conftest.py does not put on sys.path (it adds swap_terminal/).
import xrp_balances  # noqa: E402 -- checked: the sys.path.insert above is what makes this importable, and moving it earlier would import the module before its own directory is on the path. Same idiom and same reason as tests/test_xrp_chain_check_units.py:28.

# Every name that would mean this script can move money. `call` is absent on
# purpose: rpc() is this file's only outbound path and it is a read.
FORBIDDEN = ("send_to_address", "submit", "sign", "_sign_and_submit", "preview_payout",
             "submit_and_wait", "dumpprivkey", "sendtoaddress")


def _called_names(tree: ast.AST) -> set[str]:
    """Every name and attribute this module CALLS, ignoring comments and strings."""
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            target = node.func
            if isinstance(target, ast.Name):
                names.add(target.id)
            elif isinstance(target, ast.Attribute):
                names.add(target.attr)
    return names


@pytest.mark.parametrize("forbidden", FORBIDDEN)
def test_the_balance_reader_CALLS_nothing_that_could_move_money(forbidden):
    """The header's "Can move funds: NO" is checked, not trusted.

    MUTATION: add `adapter.send_to_address(...)` and the send_to_address case
    fails. Verified 2026-09-29.

    WHAT THIS DOES NOT CATCH, and the reason the test below it exists. I first
    wrote the mutation here as `rpc("submit", {})` and asserted it would fail.
    It does not: that calls `rpc`, and "submit" is a string ARGUMENT, so an AST
    walk over called NAMES never sees it. The docstring made the claim before
    the mutation was run -- rule 17's register error, in a test whose whole job
    is to check a claim. The method strings are held separately, below.
    """
    assert forbidden not in _called_names(TREE), (
        f"xrp_balances.py calls {forbidden}(). Its module header tells an operator it cannot move "
        f"funds, and that sentence is what gets read before the script is pointed at an endpoint"
    )


# EVERY RIPPLED METHOD THIS SCRIPT MAY ASK FOR, and all three are reads.
# account_info is the balance, server_info is the reserve, account_objects is the
# escrow list. Nothing else, and in particular not `submit` or `sign`.
READ_ONLY_METHODS = frozenset({"account_info", "server_info", "account_objects"})


def test_every_rippled_method_this_script_asks_for_is_a_READ():
    """rpc() is the only outbound path, so what matters is what it is HANDED.

    The name check above cannot see this: the method travels as a string literal
    into a function whose own name is innocent. So the strings are read out of
    the AST directly, which is the form the risk actually takes here.

    MUTATION: add `rpc("submit", {})` anywhere in xrp_balances.py and this fails
    with "submit". That is the mutation the test above claimed and did not catch.
    Verified 2026-09-29, by running it.
    """
    asked = set()
    for node in ast.walk(TREE):
        if not isinstance(node, ast.Call):
            continue
        target = node.func
        if isinstance(target, ast.Name) and target.id == "rpc" and node.args:
            first = node.args[0]
            # A non-literal method name is refused rather than ignored. The
            # point of this test is that the set of methods is knowable by
            # READING the file; an f-string or a variable would make it knowable
            # only by running it, which is the same defect rule 5 names when a
            # gate's logic can only be answered by whoever ran it.
            assert isinstance(first, ast.Constant) and isinstance(first.value, str), (
                f"rpc() is called with a computed method name at line {node.lineno}. Every method this "
                f"script may ask for has to be readable off the page, or this test cannot hold the "
                f"'Can move funds: NO' claim in the module header"
            )
            asked.add(first.value)
    assert asked, "found no rpc() call at all; this test has stopped measuring anything"
    assert asked <= READ_ONLY_METHODS, (
        f"xrp_balances.py asks rippled for {sorted(asked - READ_ONLY_METHODS)}, which is not in the "
        f"read-only set {sorted(READ_ONLY_METHODS)}. Its module header tells an operator it cannot "
        f"move funds"
    )


def test_the_imported_signing_name_is_the_reserve_ARITHMETIC_and_nothing_else():
    """chains/xrp_signing.py holds both; only one of them may come across.

    The header says so in as many words, because "imports no signing path" was
    the sentence that first went in and it was false -- reserve_drops() lives in
    the signing module. What makes the claim true is WHICH name, so that is what
    is asserted rather than the module it came from.
    """
    imported = {alias.name for node in ast.walk(TREE) if isinstance(node, ast.ImportFrom)
                and node.module == "chains.xrp_signing" for alias in node.names}
    assert imported == {"reserve_drops"}, (
        f"xrp_balances.py imports {sorted(imported)} from chains.xrp_signing. Only reserve_drops -- "
        f"arithmetic over two server-reported numbers -- is arithmetic; everything else in that "
        f"module exists to sign or to submit"
    )


def test_an_unreadable_escrow_list_does_not_render_as_an_account_with_no_escrows():
    """Rule 14, on the one pair of outcomes in this file that could be confused.

    A server that refuses account_objects and an account that owns no escrows
    both produce an empty list. If the script printed only the list, those two
    would be one line, and the reader would conclude "no escrows" from a failure
    to look. The reason string is what separates them, so it is what is checked.

    MUTATION: make escrows_held() return ([], "") on the exception path and this
    fails on the emptiness of the reason. Verified 2026-09-29.
    """
    def _explodes(*_args, **_kwargs):
        raise OSError("connection reset")

    original = xrp_balances.rpc
    try:
        xrp_balances.rpc = _explodes
        objects, why = xrp_balances.escrows_held("rNobody")
    finally:
        xrp_balances.rpc = original

    assert objects == [], "a failed lookup must not invent escrows"
    assert why, "an empty escrow list with an empty reason is indistinguishable from 'none held'"
    assert "unavailable" in why and "OSError" in why, (
        f"the reason has to name what failed; an operator reading {why!r} cannot tell whether the "
        f"account holds nothing or the server would not say"
    )


def test_a_server_that_reports_no_escrows_says_none_rather_than_printing_nothing():
    """The other half of the same pair, and the one that is a normal answer."""
    original = xrp_balances.rpc
    try:
        xrp_balances.rpc = lambda *_a, **_k: {"account_objects": []}
        objects, why = xrp_balances.escrows_held("rNobody")
    finally:
        xrp_balances.rpc = original

    assert objects == []
    assert why == "0 outstanding", (
        f"got {why!r}. An account with no escrows is a RESULT and must read as one; this string is "
        f"printed beside '(none)' so the reader knows the list was actually consulted"
    )
