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


# EVERY RIPPLED METHOD THIS SCRIPT MAY ASK FOR, and all four are reads.
# account_info is the balance, server_info is the reserve, account_objects is the
# escrow list. Nothing else, and in particular not `submit` or `sign`.
#
# `tx` WAS ADDED 2026-09-30, AND THE GATE CAUGHT IT FIRST, which is what it is for.
# The escrow block now looks up an EscrowCancel's OfferSequence, which an
# account_objects entry does not carry -- measured on the operator's own run, absent
# from all four entries it returned, with PreviousTxnID present in all four. `tx`
# takes a transaction hash and returns that transaction; it signs nothing, submits
# nothing and changes no ledger state, which is why it belongs in this set.
#
# THIS IS AN ALLOWLIST OF READS AND NOT A SUPPRESSION BASELINE (rule 19). The
# difference is whether the entry was READ before it was added: `submit`, `sign`,
# `sign_for` and `submit_multisigned` are the four that must never appear here, and
# a method arriving because a check failed rather than because somebody established
# it is a read is the thing rule 19 forbids. Adding `tx` is a claim that it was
# checked, and this comment is what that claim looks like.
READ_ONLY_METHODS = frozenset({"account_info", "server_info", "account_objects", "tx"})

#: The methods that may never be in the set above, whatever else changes. A separate
#: constant so that widening READ_ONLY_METHODS cannot quietly admit one of them --
#: which is the only way the allowlist above turns into the baseline it says it is not.
NEVER_READ_ONLY = frozenset({"submit", "sign", "sign_for", "submit_multisigned"})


def test_the_read_only_allowlist_can_never_admit_a_submitting_method():
    """The guard on the guard. MUTATION: add "submit" to READ_ONLY_METHODS and this fails.

    Without it, the allowlist is one edit away from permitting exactly what the module header
    promises the script cannot do -- and that edit would look like every other legitimate
    addition to the set, including the `tx` one above.
    """
    overlap = READ_ONLY_METHODS & NEVER_READ_ONLY
    assert not overlap, (
        f"the read-only allowlist admits {sorted(overlap)}, which signs or submits. "
        f"xrp_balances.py's header tells an operator it cannot move funds"
    )


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


# ---------------------------------------------------------------------------
# THE ESCROW DEFECT THE OPERATOR'S FIRST RUN FOUND, 2026-09-29. Seeded with the
# exact rows their ledger returned, so these are not hypothetical shapes: one
# escrow, sender rnjG8n..., destination rBfM7j..., and it appeared under BOTH
# accounts while only the sender reported OwnerCount 1.
# ---------------------------------------------------------------------------

SENDER = "rnjG8n16JinjqkzZj5Jmw6NDMBMzhhNbVv"
DESTINATION = "rBfM7je6e9Ca2cMvuRn7cr9xExFgDa5NGx"

# The escrow as the live ledger returned it, CancelAfter included. 843784768 in
# XRPL's clock is 2026-09-27T00:39:28Z.
LIVE_ESCROW = {"Account": SENDER, "Destination": DESTINATION, "Amount": "1000000",
               "CancelAfter": 843784768, "FinishAfter": None}
LIVE_CANCEL_AFTER_ISO = "2026-09-27T00:39:28"


class _Recorder:
    """A Console that keeps every line instead of printing it."""

    def __init__(self):
        self.lines = []

    def say(self, text):
        self.lines.append(text)

    def check(self, label, got, expected, ok):
        # step_console.Console.check's real signature. A recorder with fewer
        # arguments would silently accept a call the real Console rejects.
        self.lines.append(f"CHECK {label} got={got} expected={expected} ok={ok}")
        return ok

    def text(self):
        return "\n".join(self.lines)


def _report(address: str, owner_count, escrows):
    """report_account() against seeded account_info and account_objects rows."""
    def _rpc(method, params):
        if method == "account_info":
            return {"account_data": {"Balance": "79995090", "OwnerCount": owner_count}}
        if method == "account_objects":
            return {"account_objects": escrows}
        raise AssertionError(f"report_account asked for {method}, which it should not")

    recorder = _Recorder()
    original = xrp_balances.rpc
    try:
        xrp_balances.rpc = _rpc
        xrp_balances.report_account(recorder, address, 1, 0.2)
    finally:
        xrp_balances.rpc = original
    return recorder.text()


def test_an_INCOMING_escrow_is_not_reported_as_costing_this_account_a_reserve():
    """THE DEFECT, pinned with the operator's own rows.

    The destination reported OwnerCount 0 and got the sentence "it raises this
    account's reserve by one increment". It does not: the sender pays that.

    MUTATION: drop the `mine` test and use the OUT wording unconditionally, and
    this fails on "raises". Verified 2026-09-29.
    """
    out = _report(DESTINATION, 0, [LIVE_ESCROW])
    assert "IN " in out, f"an escrow whose Account is not this address must read as incoming:\n{out}"
    assert "costs THIS account no balance and no reserve" in out, out
    assert "raises" not in out, (
        f"an escrow this account did not send must not be described as raising its reserve. That is "
        f"the defect the operator's first run printed:\n{out}"
    )
    assert SENDER in out, f"an incoming escrow has to name whose it is:\n{out}"


def test_an_OUTGOING_escrow_says_its_drops_have_already_left_the_balance():
    """The other side of the same comparison, and the reserve claim IS true here."""
    out = _report(SENDER, 1, [LIVE_ESCROW])
    assert "OUT " in out, out
    assert "already left the balance" in out, out
    assert "holding one reserve increment" in out, out
    assert "MISMATCH" not in out, f"one sent escrow against OwnerCount 1 is consistent:\n{out}"


def test_more_sent_escrows_than_OwnerCount_says_the_spendable_figure_is_UNPROVEN():
    """The cross-check that would have caught the defect at the time.

    Two readings of the same account disagree: OwnerCount says it pays for no
    objects, account_objects says it sent one. The reserve above is computed from
    OwnerCount, so the spendable number below it cannot be trusted -- and saying
    so beats printing a confident figure derived from the losing reading.

    MUTATION: delete the `outgoing > owner_count` block and this fails on
    "MISMATCH". Verified 2026-09-29.
    """
    out = _report(SENDER, 0, [LIVE_ESCROW])
    assert "MISMATCH" in out, out
    assert "unproven" in out, f"the mismatch has to say what it costs the reader:\n{out}"


def test_a_cancel_after_in_the_PAST_says_the_escrow_can_be_canceled_now():
    """Rule 14 on the number that was printed raw.

    843784768 was shown to the operator as-is. It had already passed, so 1 XRP
    was sitting recoverable and the screen did not say so.

    MUTATION: return the raw seconds without the comparison and this fails on
    "PASSED". Verified 2026-09-29.
    """
    line = xrp_balances._when("CancelAfter", LIVE_ESCROW["CancelAfter"])
    assert LIVE_CANCEL_AFTER_ISO in line, (
        f"got {line!r}; 843784768 in XRPL's clock is {LIVE_CANCEL_AFTER_ISO}Z and the raw number is "
        f"what an operator cannot read"
    )
    assert "PASSED" in line and "canceled now" in line, line


def test_a_timestamp_that_is_not_set_says_so_rather_than_printing_None():
    """FinishAfter is optional on an EscrowCreate, and this repo omits it.

    `FinishAfter=None` is what the first version printed. "not set" is the same
    fact in words the reader does not have to be a Python programmer to read.
    """
    assert xrp_balances._when("FinishAfter", None) == "FinishAfter  not set"


def test_the_epoch_offset_has_exactly_one_definition_in_the_tree():
    """Rule 8, on the constant that moved on 2026-09-29.

    It was defined in xrp_htlc_escrow.py, which submits escrows, so a read-only
    balance reader could not use it. chains/xrp_units.py is the home now and
    xrp_htlc_escrow.py IMPORTS it -- one object, two import paths. A second
    assignment anywhere is the drift rule 8 is about, and a wrong epoch offset
    builds an escrow whose timelock expired thirty years ago.
    """
    root = SOURCE.parent
    def _assigns_it(path) -> bool:
        return any(
            isinstance(node, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id == "RIPPLE_EPOCH_OFFSET_SECONDS"
                    for t in node.targets)
            for node in ast.walk(ast.parse(path.read_text()))
        )

    defined = [str(path.relative_to(root)) for path in sorted(root.rglob("*.py"))
               if "__pycache__" not in path.parts and ".venv" not in path.parts
               and _assigns_it(path)]
    assert defined == ["swap_terminal/chains/xrp_units.py"], (
        f"RIPPLE_EPOCH_OFFSET_SECONDS is assigned in {defined}. One definition, imported -- a second "
        f"copy drifts silently and a wrong offset makes an escrow anyone can cancel"
    )


# ---------------------------------------------------------------------------
# THE `tx` CACHE. Measured on the operator's 2026-09-30 run: FOUR reads for TWO
# escrows, because account_objects lists an escrow under BOTH the sender's and
# the destination's owner directory and both faucet accounts are ends of the
# same two escrows. Half the calls were waste, against a public endpoint that
# has already rate-limited this tree once today.
# ---------------------------------------------------------------------------


class _CountingConsole:
    """Collects what would be printed, so the test can assert on the SCREEN."""

    def __init__(self):
        self.lines = []

    def say(self, line):
        self.lines.append(line)


@pytest.fixture(autouse=True)
def _empty_tx_cache():
    """A module-level cache is shared state; each test starts from empty and leaves it empty.

    Without this the second test to run would see the first one's entries and pass for the wrong
    reason -- which is the failure mode of caching in a module rather than in a call, and the
    reason the cache is documented as per-run.
    """
    xrp_balances._TX_ALREADY_READ.clear()
    yield
    xrp_balances._TX_ALREADY_READ.clear()


ESCROW_UNDER_BOTH_ACCOUNTS = {
    "Account": "rSENDER",
    "Amount": "1000000",
    "CancelAfter": 843784768,
    "PreviousTxnID": "F74EFFDB",
}
A_CREATE = {"TransactionType": "EscrowCreate", "Account": "rSENDER", "Sequence": 21051277}


def test_the_same_escrow_seen_twice_is_read_from_the_ledger_ONCE(monkeypatch):
    """THE DEFECT, MEASURED AND FIXED. Two lookups of one hash became one.

    Seeded exactly as the live run produced it: the same escrow entry handed in twice, which is
    what happens when both ends of it are in the account list.
    """
    calls = []
    monkeypatch.setattr(xrp_balances, "rpc",
                        lambda method, params: calls.append((method, params)) or A_CREATE)

    console = _CountingConsole()
    first = xrp_balances._sequence_from_the_creating_tx(
        console, ESCROW_UNDER_BOTH_ACCOUNTS, xrp_balances.cancel_inputs(ESCROW_UNDER_BOTH_ACCOUNTS))
    second = xrp_balances._sequence_from_the_creating_tx(
        console, ESCROW_UNDER_BOTH_ACCOUNTS, xrp_balances.cancel_inputs(ESCROW_UNDER_BOTH_ACCOUNTS))

    assert len(calls) == 1, f"asked the ledger {len(calls)} times for one transaction hash"
    assert calls[0][0] == "tx"
    assert first.offer_sequence == second.offer_sequence == 21051277
    assert first.ready is second.ready is True

    # THE PROVENANCE, AND THIS IS THE ASSERTION THAT WAS MISSING. Reverting this function to
    # merging the sequence into a copy of the escrow dict -- which is how it was first written --
    # survived every other check here: cancel_inputs() would then report "the account_objects
    # entry itself", true of the dict it was handed and false of the world. The provenance has to
    # be asserted where the read HAPPENS, not only on the function that formats it.
    for result in (first, second):
        assert result.source == "the EscrowCreate `tx F74EFFDB`"
        assert "F74EFFDB" in result.how_to_get_it
        assert "account_objects entry" not in result.how_to_get_it, (
            "this sequence came from a second transaction; saying it was on the entry tells the "
            "reader it needs no further checking, when its identity had to be verified first"
        )

    # AND THE CACHE HIT IS ON THE SCREEN (rule 14). A reader comparing the two accounts' sections
    # would otherwise see the read announced under one and not the other, with no way to tell a
    # cache hit from a branch that did not run.
    printed = " ".join(console.lines)
    assert "was already read this run" in printed
    assert printed.count("reading `tx") == 1


def test_a_DIFFERENT_hash_is_still_read(monkeypatch):
    """MUTATION: cache on any key at all -- or return the first response for every hash -- and
    the second escrow inherits the first's OfferSequence. Which is the sequence of a DIFFERENT
    escrow of the same owner, and cancelling by it cancels the wrong one. The operator holds two.
    """
    responses = {"F74EFFDB": A_CREATE,
                 "AD6C8FAF": {**A_CREATE, "Sequence": 21051301}}
    asked = []

    def fake_rpc(method, params):
        asked.append(params["transaction"])
        return responses[params["transaction"]]

    monkeypatch.setattr(xrp_balances, "rpc", fake_rpc)
    console = _CountingConsole()
    other = {**ESCROW_UNDER_BOTH_ACCOUNTS, "PreviousTxnID": "AD6C8FAF", "CancelAfter": 844206951}

    one = xrp_balances._sequence_from_the_creating_tx(
        console, ESCROW_UNDER_BOTH_ACCOUNTS, xrp_balances.cancel_inputs(ESCROW_UNDER_BOTH_ACCOUNTS))
    two = xrp_balances._sequence_from_the_creating_tx(
        console, other, xrp_balances.cancel_inputs(other))

    assert asked == ["F74EFFDB", "AD6C8FAF"], "two distinct hashes, two reads"
    assert one.offer_sequence == 21051277
    assert two.offer_sequence == 21051301
    assert one.offer_sequence != two.offer_sequence
    # Each names ITS OWN creating transaction, so the two cannot be confused on the screen either.
    assert one.source == "the EscrowCreate `tx F74EFFDB`"
    assert two.source == "the EscrowCreate `tx AD6C8FAF`"


def test_a_FAILED_read_is_not_cached_as_a_failure(monkeypatch):
    """A transient network error is not a fact about the ledger.

    MUTATION: cache before checking the call succeeded, or cache the exception, and the second
    account's section reports the field unreadable because the FIRST one's request timed out.
    """
    attempts = []

    def flaky_rpc(_method, params):
        attempts.append(params["transaction"])
        if len(attempts) == 1:
            raise RuntimeError("connection reset")
        return A_CREATE

    monkeypatch.setattr(xrp_balances, "rpc", flaky_rpc)
    console = _CountingConsole()

    failed = xrp_balances._sequence_from_the_creating_tx(
        console, ESCROW_UNDER_BOTH_ACCOUNTS, xrp_balances.cancel_inputs(ESCROW_UNDER_BOTH_ACCOUNTS))
    assert failed.ready is False, "the read did not happen, so nothing is known"
    assert "F74EFFDB" not in xrp_balances._TX_ALREADY_READ

    retried = xrp_balances._sequence_from_the_creating_tx(
        console, ESCROW_UNDER_BOTH_ACCOUNTS, xrp_balances.cancel_inputs(ESCROW_UNDER_BOTH_ACCOUNTS))
    assert len(attempts) == 2, "the second account retries rather than inheriting a failure"
    assert retried.ready is True

    printed = " ".join(console.lines)
    assert "that read FAILED" in printed
    assert "not the same as absent" in printed, (
        "a read that did not happen must not read as a field that is absent"
    )


def test_only_the_tx_read_is_cached_and_it_is_keyed_BY_THE_HASH(monkeypatch):
    """account_info and account_objects are NOT cached, and that is the point of the split.

    A validated transaction's fields never change, so its response cannot go stale within a run.
    A balance and an owner list can, and caching those would make this script report a state the
    ledger has moved past -- rule 15's shape at script scale: a buffer with one reader, and the
    authority still asked directly.

    ASSERTED ON THE CACHE'S CONTENTS after a real call, not by grepping the source. The first
    version of this test did grep -- for `_TX_ALREADY_READ[address]` and two other spellings --
    which is the same "pin a name where you mean a property" mistake four other checks in this
    tree have already made, and it had a syntax error in its own escape sequence besides.
    """
    monkeypatch.setattr(xrp_balances, "rpc", lambda _m, _p: A_CREATE)
    console = _CountingConsole()
    xrp_balances._sequence_from_the_creating_tx(
        console, ESCROW_UNDER_BOTH_ACCOUNTS, xrp_balances.cancel_inputs(ESCROW_UNDER_BOTH_ACCOUNTS))

    # Exactly one entry, keyed by the transaction hash and nothing else -- not the address, not
    # a (method, params) tuple, not the escrow dict.
    assert set(xrp_balances._TX_ALREADY_READ) == {"F74EFFDB"}
    assert all(isinstance(key, str) for key in xrp_balances._TX_ALREADY_READ)
    assert ESCROW_UNDER_BOTH_ACCOUNTS["Account"] not in xrp_balances._TX_ALREADY_READ, (
        "an address is not a transaction hash; caching by account would return one escrow's "
        "creation for a different escrow of the same owner"
    )

    # And the only rippled method this cache stands in front of is `tx`. The read-only allowlist
    # above holds the full set; this holds that the cached one is the immutable one.
    assert "tx" in READ_ONLY_METHODS
    assert not {"account_info", "account_objects", "server_info"} & set(xrp_balances._TX_ALREADY_READ)
