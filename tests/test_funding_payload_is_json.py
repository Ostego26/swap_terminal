"""GET /api/funding has to survive json.dumps. Both of its amounts are Decimals.

Role: test (pure decisions, seeded; no daemon, no socket, no database)
Reads: swap_terminal/regtest/funding_steps.py, swap_terminal/regtest/operator_panel.py
Writes: nothing
Can move funds: no. Both functions under test only read and format; the RPC calls they
        would make are answered by the stub below.
Mainnet-safe: yes

THE DEFECT, MEASURED 2026-10-09 AND NOT INFERRED.

operator_panel.py's route table maps "/api/funding" to funding_payload(), and the GET
handler is `return json.dumps(handler()).encode(), "application/json", 200`. Two of that
payload's values were Decimals:

    "needed"          funding_steps.funding_needed_coins(run), declared `-> str`,
                      returning satoshis_to_coins(...) which returns a Decimal
    rows[].value      regtest/operator_panel.PaymentRow.value_coins, declared `str`,
                      constructed from funding_steps.satoshis_to_coins(...)

and `json.dumps({"needed": Decimal("1.51010000")})` raises

    TypeError: Object of type Decimal is not JSON serializable

checked at the interpreter. The panel's guarded() turns that into a readable 500, so the
funding region of the page -- the address to pay, the amount to send, and every payment
row with its spent/usable verdict -- rendered as a server error. That region is the only
place the operator is told what to send, and the panel exists because six runs on
2026-09-28 failed for want of exactly that information.

Both declarations said `str` and both were right; the two construction sites were wrong.
The digits are unchanged either way: str(Decimal("1.51010000")) is "1.51010000", which is
what the f-string in no_usable_funding_message() already printed.

WHY THE ASSERTION IS json.dumps AND NOT isinstance(..., str).

isinstance would pass for any string and would also pass for a future value that is a str
but not serializable alongside the rest of the payload. The serializer is the thing that
broke, so the serializer is the assertion -- behavioral verification rather than a check on
the shape of the code (CLAUDE.md's "verify by row-level behavioral outcome").
"""

from __future__ import annotations

import io
import json
from pathlib import Path

from regtest import funding_steps
from regtest import operator_panel as decisions
from regtest.console import Console
from regtest.daemons import ChainConfig

# 1.51 GRC in satoshis -- the value of the seeded funding OUTPOINT, which is the same figure
# tests/test_operator_panel.py's own _Outpoint carries, so a reader comparing the two sees one
# number rather than two. It is deliberately NOT the same thing as funding_needed_coins(),
# which is 0.16 GRC on this asset (LOCK_COIN 0.1 + FUNDING_HEADROOM_COIN 0.05 +
# SPLIT_FEE_ALLOWANCE_COIN 0.01, measured 2026-10-09 by calling it, after a first draft of
# this file asserted 1.51 from memory and failed). The two numbers answer different questions:
# what has been paid, and what one lock costs.
LOCK_VALUE_SATOSHIS = 151_000_000
GRC_NEEDED_COINS = "0.16000000"
TIP_HEIGHT = 3_298_078
FUNDED_TXID = "33" * 32


class _Key:
    """The one thing these functions want from a funding key: its address."""

    address = "ours"


class _Outpoint:
    def __init__(self, txid: str) -> None:
        self.txid, self.vout, self.value_satoshis = txid, 1, LOCK_VALUE_SATOSHIS


class _Node:
    """The daemon, answering the two RPCs payment_rows() makes and refusing everything else.

    A HARD AssertionError on any other method, deliberately: a stub that returned {} for an
    unexpected call would let this test keep passing while the function under test started
    asking the chain something new, which is the stub-drifts-from-reality failure a test
    cannot see from the inside.
    """

    def call(self, method: str, *_params):
        if method == "listtransactions":
            return [{"address": _Key.address, "category": "send", "txid": FUNDED_TXID,
                     "confirmations": 6}]
        if method == "getblockcount":
            return TIP_HEIGHT
        raise AssertionError(method)


def _run() -> funding_steps.Run:
    """A REAL funding_steps.Run with only its daemon replaced.

    Not a duck-typed stand-in with an `asset` attribute: `Run.asset` is a property over
    `config.asset`, `Run.say` prefixes with it, and both of the functions under test read
    through those. Constructing the real dataclass means the only thing stubbed is the one
    thing that must never run in a test -- the socket -- and `node` is the single seam for
    it. A class with `asset = "GRC"` on it would also not be a `Run` to a type checker, and
    writing it that way is what put three findings in the first draft of this file.

    GRC because that is the chain whose funding route the panel serves, and the one
    LOCK_COIN / FUNDING_HEADROOM_COIN are keyed by in the measurement above.

    Port 0 and empty credentials: this config is never used to open anything. The caller
    replaces `node` through monkeypatch before anything that would reach a daemon is called,
    and funding_needed_coins() does not reach one at all -- it reads `run.asset` and three
    module constants. Nothing here is a credential.
    """
    return funding_steps.Run(
        console=Console(total_steps=0, stream=io.StringIO()),
        config=ChainConfig(
            asset="GRC",
            daemon_path="",
            cli_path="",
            datadir=Path("/nonexistent-this-test-opens-nothing"),
            host="127.0.0.1",
            port=0,
            rpc_user="",
            rpc_password="",
            conf_name="",
            pid_name="",
        ),
        wallet="",
    )


def test_the_amount_to_send_survives_the_serializer():
    """funding_needed_coins() is the payload's "needed" value."""
    needed = funding_steps.funding_needed_coins(_run())
    assert json.dumps({"needed": needed}), "a Decimal here is a 500 in place of the funding table"
    # And it still says the number, to eight places, rather than having been emptied to
    # satisfy the serializer. The digits are the assertion because `str()` is the fix: a
    # version that returned "" or "0" would serialize perfectly and tell the operator to send
    # nothing.
    assert needed == GRC_NEEDED_COINS, needed


def test_every_payment_row_survives_the_serializer(monkeypatch):
    """PaymentRow.value_coins is the payload's rows[].value.

    Built through the real payment_rows() rather than by constructing a PaymentRow by hand:
    the declared type was already `str` and a hand-built row would have honored it, so a
    test that did not go through the construction site would have passed before the fix.
    """
    run = _run()
    # THE ONE SEAM, and it is monkeypatch rather than an assignment so that it is undone when
    # the test ends and so that nothing has to be suppressed to assign over a method.
    monkeypatch.setattr(run, "node", lambda wallet=True: _Node())
    monkeypatch.setattr(funding_steps, "find_operator_funding",
                        lambda run, key, txid: _Outpoint(txid))
    monkeypatch.setattr(funding_steps, "find_the_spender",
                        lambda run, outpoint, max_depth=0: (None, "nothing spends this outpoint"))

    rows = decisions.payment_rows(run, _Key())
    assert rows, "the stub seeded one payment; no rows means the test is not exercising this"

    # The shape operator_panel.funding_payload() builds, field for field.
    payload = {
        "address": _Key.address,
        "needed": funding_steps.funding_needed_coins(_run()),
        "asset": "GRC",
        "error": "",
        "rows": [{"txid": r.txid, "confirmations": r.confirmations, "value": r.value_coins,
                  "spender": r.spender, "usable": r.usable, "note": r.note} for r in rows],
    }
    assert json.dumps(payload), "the whole payload, because the route serializes the whole payload"
    assert rows[0].value_coins == "1.51000000", rows[0].value_coins
