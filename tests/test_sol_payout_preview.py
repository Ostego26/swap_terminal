#!/usr/bin/env python3
"""The floor decision, and that this entry point cannot send. Seeded, no cluster.

Role: tests (read-only)
Reads: sol_payout_preview.py's own functions and its source tokens. No network,
      no database, no cluster, no key.
Writes: nothing
Can move funds: no
Mainnet-safe: yes

WHY THIS TOOL EXISTS AND THEREFORE WHY THIS TEST DOES. The operator armed the SOL
payout on 2026-10-03 and the next thing they needed was the smallest amount that
would actually be delivered -- and measured that day, NO root entry point could
preview a SOL payout at all (`grep -ln "preview_payout" *.py` returned nothing). So
the only way to see the plan was to let a real swap reach the payout worker, which
is the wrong order for a path whose broadcast has never been exercised against any
cluster.

THE DECISION THIS PINS is clears_the_floor(), which has three cases and they are
not interchangeable:

    destination exists      any amount is deliverable
    new account, >= floor   the runtime creates it
    new account, < floor    REFUSED by the runtime -- accepted by every other
                            check and then not delivered

The third is the whole hazard. The first is a yes that does not survive the
destination being closed and reopened, which is why the sentence says WHICH yes it
is rather than just "YES".
"""

from __future__ import annotations

import tokenize
from pathlib import Path

from chains.solana_units import LAMPORTS_PER_SOL

from sol_payout_preview import clears_the_floor, main

FLOOR = 890_880


def test_a_new_account_below_the_floor_is_NOT_deliverable_and_names_the_smallest_that_is():
    """The case the tool was built for, and the number the operator needs.

    MUTATION: change `>=` to `>` in clears_the_floor(). The exactly-at-the-floor
    test below fails -- and an operator sending exactly the minimum would be told
    it bounces when it does not.
    """
    clears, why = clears_the_floor(0.0001, FLOOR, destination_exists=False)
    assert clears is False, why
    assert "BELOW" in why, why
    # The remedy has to be IN the sentence, in SOL, because that is the figure they
    # will type into --amount next (rule 14: the operator reads the screen).
    assert f"{FLOOR / LAMPORTS_PER_SOL:.9f} SOL" in why, why


def test_exactly_the_floor_IS_deliverable():
    """A boundary that is cheap to get wrong by one lamport and expensive to discover."""
    clears, why = clears_the_floor(FLOOR / LAMPORTS_PER_SOL, FLOOR, destination_exists=False)
    assert clears is True, why
    assert "at or above" in why, why


def test_an_UNASKED_floor_is_never_printed_as_a_figure():
    """"the 0-lamport floor" was on the operator's screen within the hour.

    MEASURED 2026-10-03, from their own run of this tool:

        YES -- the destination account already exists, so any amount is deliverable
        and the 0-lamport floor does not apply to this send.

    preview_payout() prints "rent floor  not asked: the destination account already
    exists" four lines above that, and `rent_minimum_lamports` is 0 BECAUSE NOTHING
    ASKED. So the sentence rendered an unasked value as a measurement -- a reader
    cannot tell that zero from a cluster that answered zero, and the entire point of
    this tool is making a figure's provenance visible.

    MUTATION: put the f-string back. This fails on the digit.
    """
    _clears, why = clears_the_floor(0.001, 0, destination_exists=True)
    assert "0-lamport" not in why, f"an unasked floor is printed as a figure: {why}"
    assert "NOT asked of the cluster" in why, why
    # And when there IS a figure, it is still named -- the fix must not silence a
    # real reading to avoid a fake one.
    # THE FIGURE, NOT THE WORDING AROUND IT. This asserted "890,880-lamport floor"
    # and broke on a grammar fix that changed it to "890,880-lamport rent-exempt
    # floor" -- every fact identical. A test that pins a phrase fails on a rewrite
    # that kept every fact, which is noise that teaches the next person to loosen
    # the assertion rather than read it. What must hold is that a real reading is
    # NAMED with its digits.
    _clears, why = clears_the_floor(0.001, 890_880, destination_exists=True)
    assert "890,880" in why, f"a floor the cluster DID answer is not reported: {why}"
    assert "NOT asked" not in why, f"a real reading is described as unasked: {why}"


def test_an_EXISTING_destination_takes_any_amount_and_the_sentence_says_so_is_conditional():
    """A yes that depends on the account staying open must not read like an absolute.

    MUTATION: return a bare "YES" here. This passes nothing, and an operator reads
    "any amount is fine for this address" as durable when it is a fact about today.
    """
    clears, why = clears_the_floor(0.0000001, FLOOR, destination_exists=True)
    assert clears is True, why
    assert "already exists" in why, why
    assert "IT WOULD APPLY if that account were ever closed" in why, (
        "the yes is unconditional in the prose and conditional in fact, which is the kind of sentence "
        "that gets quoted back months later"
    )


def test_the_tool_REFUSES_and_exits_NON_ZERO_with_no_SOL_adapter(capsys):
    """An unreachable chain is a refusal, not a blank -- and not exit 0.

    Rule 13: a run that did nothing must not report the same way as one that did.
    MEASURED 2026-10-03: exit 2, with why_unconfigured()'s sentence naming the
    variable.
    """
    code = main(["--to", "CUBnQ5QBfYkL71TCqSdecAQ9xjfGmAdu6Hs3fjQeLorp", "--amount", "0.001"])
    body = capsys.readouterr().out
    assert code == 2, f"a missing adapter exited {code}; a reader cannot tell 0 from success"
    assert "REFUSED" in body, body
    assert "SOL_RPC_URL" in body, "the refusal does not name the variable that would fix it"


def test_NOTHING_IN_THIS_FILE_CAN_SIGN_OR_SEND():
    """Asserted over CODE TOKENS, not file text, so the docstrings may explain safety.

    The same technique tests/test_solana_adapter.py uses on chains/solana.py, and for
    the same reason: this file's own prose says the words "sign" and "send" while
    explaining that it does neither, so a substring search over the text would fail
    on the explanation rather than on a defect.

    MUTATION: import signed_transfer_wire or call send_to_address. This fails by
    name.
    """
    source = Path(__file__).resolve().parent.parent / "sol_payout_preview.py"
    with source.open("rb") as handle:
        names = {token.string for token in tokenize.tokenize(handle.readline)
                 if token.type == tokenize.NAME}
    forbidden = {"send_to_address", "signed_transfer_wire", "sign_message", "load_payout_keypair",
                 "CONFIRM_SOL_SEND", "solana_signing", "wire_transaction"}
    present = sorted(forbidden & names)
    assert not present, (
        f"{present} appear as CODE in a tool whose header promises it cannot move funds. The preview "
        f"path is the whole program; a name that can sign does not belong in it"
    )
    assert "preview_payout" in names, (
        "the tool no longer calls preview_payout(), so whatever it does call is unreviewed by this test"
    )
