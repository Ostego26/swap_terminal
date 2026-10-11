"""An XRP payment with no DestinationTag becomes a ROW, not just a print.

Role: test (seeded rippled transport; no socket, nothing broadcast)
Reads: chains/xrp.py, chains/xrp_payments.py, services/deposit_service.py,
      services/unattributable_deposit_service.py, db.py's real SCHEMA
Writes: one throwaway sqlite file per test, under pytest's tmp_path
Can move funds: no. requests.post is replaced by a seeded responder, so the real
      adapter code runs with nothing reachable. Every address here is a literal.
Mainnet-safe: yes -- no test in this file opens a socket.

BEHAVIORAL, NOT A READING. Every assertion here is on a row that is actually in
`unattributable_deposits` after the REAL XRPAdapter scanned a seeded account_tx
response and the REAL services/deposit_service.record_what_nobody_can_claim()
ran against the REAL db.py SCHEMA. Nothing asserts that the source contains a
check; the pattern CLAUDE.md's "Verify by behavior" section asks for is seed,
run, query, assert.

=============================================================================
THE DEFECT THESE TESTS PIN, MEASURED 2026-10-11
=============================================================================

A customer sends XRP to XRP_DEPOSIT_ACCOUNT and omits the DestinationTag.
chains/xrp_payments._classify() refuses to guess which swap it belongs to --
correctly, and that is not the defect -- and returns (None, reason), so it never
becomes a deposit event. From there it reached NO TABLE AT ALL.

Measured by seeding one TAGGED and one UNTAGGED validated Payment and running
the real functions:

    events returned                            1   (the TAGGED one only)
    getattr(adapter, "unattributable_drops")    <<ATTRIBUTE ABSENT>>
    record_what_nobody_can_claim(db, "XRP")     0   rows written
    deposit_events                              0 rows
    unattributable_deposits                     0 rows
    late_deposits                               0 rows
    reconcile_shared_accounts(...)              1 row, for the TAGGED payment
                                                whose tag matched no swap. The
                                                untagged txid present? False

2.5 XRP arrived, was real, and was in swap_terminal.db nowhere. The only trace
was a print in find_deposits_to_address(), filtered through `_reported_deferrals`
so it was emitted once per adapter instance and then never again until a worker
restart -- and nothing on /admin, in show_unattributable.py or in any report can
see it, because all of those read the table.

THE SAME CASE ON SOLANA WAS ALREADY HANDLED, which is why the fix grows the
attribute chains/solana.py already publishes rather than inventing a second
mechanism (rule 8: let the survivor own the concept).

=============================================================================
WHAT EACH TEST FAILS FOR, so a regression says WHICH mechanism stopped holding
=============================================================================

Written one mechanism per test rather than as one end-to-end test of the happy
path, for the reason tests/test_xrp_adapter.py's header gives: a guard with no
test that fails when it is DISABLED is not a guard. Every test below was run
with the fix reverted or its key line broken, and the failure recorded.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "swap_terminal"))

from chains.xrp import XRPAdapter, untagged_payments  # noqa: E402
from db import SCHEMA, connect_db  # noqa: E402

from swap_terminal.services import deposit_service  # noqa: E402

#: The shared deposit account every XRP swap pays into. A classic address, so
#: chains/xrp_address.is_valid_classic_address() accepts it -- a made-up string
#: would be refused by validate_address() and the test would pass for the wrong
#: reason. This one is XRPL's own ACCOUNT_ZERO, which can never hold funds and so
#: can never be mistaken for a real destination somebody should pay.
ACCOUNT = "rrrrrrrrrrrrrrrrrrrrrhoLvTp"

#: Somewhere that is NOT this desk's shared account, for the payment-OUT case.
#: XRPL's ACCOUNT_ONE, the other reserved address, for the same reason: it can
#: never be a real destination anybody should pay.
OTHER_ACCOUNT = "rrrrrrrrrrrrrrrrrrrrBZbvji"

#: 64 hex characters, which is the shape of a real XRP Ledger transaction hash.
#: Distinct first characters so a failure message says which payment it is about.
UNTAGGED_HASH = "A1" + "0" * 62
TAGGED_HASH = "B2" + "0" * 62
FAILED_HASH = "C3" + "0" * 62
OUTGOING_HASH = "D4" + "0" * 62
OFFER_HASH = "E5" + "0" * 62

#: 2,500,000 drops is 2.5 XRP. Chosen because it is the figure
#: tests/xrp_seeded_transport.py records as MEASURED on the live testnet server
#: (`meta.delivered_amount` came back as the STRING "2500000"), so the shape
#: these tests feed the parser is the shape rippled 3.4.1 actually sends.
DELIVERED_DROPS = "2500000"
DELIVERED_XRP = 2.5


def payment(tx_hash, *, tag=None, tx=None, meta=None, validated=True):
    """One account_tx entry, in the `tx`-nested shape account_tx actually returns.

    `tx`-nested and NOT flat, because chains/xrp.py calls ONLY account_tx and
    chains/xrp_payments._unwrap()'s docstring records which nesting each rippled
    method uses -- measured on 3.4.1 from the operator's host. A fixture in the
    `ledger` shape would exercise a code path the production scan never takes.

    `tx` AND `meta` ARE OVERLAYS merged onto a good validated Payment, rather than
    one flat keyword per field. Two reasons, and the second is the one that made me
    rewrite it:

      the lint        a flat form reached nine parameters and ruff's PLR0913 fires
                      at six. tests/* ignores S101, S603, S607 and PLR2004 and
                      NOTHING ELSE, so this is a finding to fix and not one to
                      suppress (rule 19: a noqa is a claim you checked).
      the reading     `meta={"TransactionResult": "tecPATH_DRY"}` says WHERE in the
                      ledger's reply the difference lives, which `result=` did not.
                      Whether a field is in the transaction or in its metadata is
                      the distinction chains/xrp_payments.py's whole header is
                      about -- `Amount` is in the tx and `delivered_amount` is in
                      the meta, and confusing the two IS the partial payment
                      exploit. A fixture that flattens them away makes that
                      invisible at exactly the call sites testing it.
    """
    body = {"hash": tx_hash, "TransactionType": "Payment", "Destination": ACCOUNT, **(tx or {})}
    if tag is not None:
        body["DestinationTag"] = tag
    return {
        "tx": body,
        "meta": {"TransactionResult": "tesSUCCESS", "delivered_amount": DELIVERED_DROPS,
                 **(meta or {})},
        "validated": validated,
    }


class SeededTransport:
    """Answers account_tx with a fixed entry list. Records nothing else.

    A function rather than tests/xrp_seeded_transport.Recorder because the thing
    under test is the DEPOSIT scan, which issues exactly one method, and the
    entries differ per test. The Recorder is keyed by method for the payout tests,
    where the ORDER of server_info and account_info is itself under test.
    """

    def __init__(self, entries):
        self.entries = entries
        self.methods = []

    def __call__(self, url, **kwargs):
        body = json.loads(kwargs["data"])
        self.methods.append(body["method"])
        return _Response({"result": {"status": "success", "transactions": self.entries}})


class _Response:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


@pytest.fixture
def db(tmp_path):
    """A real database on the real SCHEMA, so the table's constraints are the ones under test."""
    conn = connect_db(str(tmp_path / "swap_terminal_test.db"), create=True)
    conn.executescript(SCHEMA)
    return conn


@pytest.fixture
def adapter(monkeypatch):
    """A factory: entries -> a real XRPAdapter whose socket is the seeded responder.

    The adapter is REAL -- its __init__, its validate_address(), its call(), its
    find_deposits_to_address() and chains/xrp_payments.py all run. Only
    requests.post is replaced, which is the narrowest possible substitution and
    the one tests/test_xrp_adapter.py already uses.
    """
    def build(entries, min_confirmations=1):
        monkeypatch.setattr("chains.xrp.requests.post", SeededTransport(entries))
        return XRPAdapter(url="https://seeded.invalid:51234/", min_confirmations=min_confirmations)
    return build


def rows_in(db):
    """Rows as DICTS -- db.connect_db sets row_factory = dict_factory, so row[0] raises."""
    return db.execute("SELECT * FROM unattributable_deposits ORDER BY id").fetchall()


# --- the hole itself -------------------------------------------------------

def test_an_untagged_payment_becomes_a_durable_row(db, adapter):
    """The whole defect, end to end: scan, record, query the table.

    MEASURED BEFORE THE FIX: record_what_nobody_can_claim(db, "XRP", adapter)
    returned 0 and `unattributable_deposits` had 0 rows, because
    hasattr(XRPAdapter, "unattributable_drops") was False and that function reads
    the attribute by name.

    MUTATION: delete the `self.unattributable_drops, unreadable = untagged_payments(...)`
    assignment from find_deposits_to_address -- or the `credits.append(...)` in
    untagged_payments -- and this fails with 0 rows.
    """
    instance = adapter([payment(UNTAGGED_HASH)])

    events = instance.find_deposits_to_address(ACCOUNT)
    written = deposit_service.record_what_nobody_can_claim(db, "XRP", instance)

    assert events == [], "an untagged payment must NOT become a creditable event"
    assert written == 1, "the untagged payment is money that arrived; it has to be recorded"
    rows = rows_in(db)
    assert len(rows) == 1, f"expected one stranded row, got {rows}"
    assert rows[0]["asset"] == "XRP"
    assert rows[0]["txid"] == UNTAGGED_HASH
    assert rows[0]["address"] == ACCOUNT, (
        "the row carries the shared account so it still says where the coins are after "
        "XRP_DEPOSIT_ACCOUNT changes"
    )


def test_the_row_carries_the_amount(db, adapter):
    """A record of money that omits the amount is not a record of money.

    MUTATION: pass credits=1 with amount=0.0, or drop `amount` from the
    UnattributableCredit, and this fails -- which is the defect
    chains/solana.UnattributableCredit's own docstring records against itself,
    caught here before it could be repeated on a second chain.
    """
    instance = adapter([payment(UNTAGGED_HASH)])
    instance.find_deposits_to_address(ACCOUNT)

    deposit_service.record_what_nobody_can_claim(db, "XRP", instance)

    assert rows_in(db)[0]["amount"] == DELIVERED_XRP, (
        f"2,500,000 drops is {DELIVERED_XRP} XRP; the column holds whole units of the asset, "
        f"the same scale as deposit_events.amount"
    )


def test_the_recorded_amount_is_DELIVERED_and_never_the_claimed_Amount(db, adapter):
    """The partial payment exploit, on the RECORDING path.

    An XRP Ledger Payment with tfPartialPayment set succeeds, reports tesSUCCESS,
    and still shows the original, larger `Amount` while delivering less. The
    credit path already refuses to read `Amount` -- that is what
    chains/xrp_payments.py's header is about -- and a SECOND path that reads it
    would write "1,000,000 XRP is stranded in the shared account" into the table
    an operator uses to decide how much money is unaccounted for.

    MUTATION: build the credit from `tx["Amount"]` instead of
    delivered_drops_to()'s first return value and this fails with 1e6 against 2.5.
    """
    instance = adapter([payment(UNTAGGED_HASH, tx={"Amount": "1000000000000"})])
    instance.find_deposits_to_address(ACCOUNT)

    deposit_service.record_what_nobody_can_claim(db, "XRP", instance)

    assert rows_in(db)[0]["amount"] == DELIVERED_XRP, (
        "the sender CLAIMED 1,000,000 XRP and the ledger DELIVERED 2.5. Only the delivered "
        "figure may ever be recorded"
    )


def test_the_discriminator_is_NULL_when_no_tag_was_sent(db, adapter):
    """NULL and an integer are two different support conversations.

    db.py's column comment draws the line: an integer means "you sent a reference
    that matches no open order", NULL means "you sent no reference at all". A 0
    here would be worse than useless -- tag 0 is a LEGAL DestinationTag, and
    chains/xrp_payments.py's own comment says a payment tagged 0 is a real payment
    carrying a real tag -- so an invented 0 would make the two indistinguishable.
    """
    instance = adapter([payment(UNTAGGED_HASH)])
    instance.find_deposits_to_address(ACCOUNT)

    deposit_service.record_what_nobody_can_claim(db, "XRP", instance)

    assert rows_in(db)[0]["discriminator"] is None


def test_one_credit_per_payment(db, adapter):
    """`credits` is a COUNT of pieces, and an XRP Payment has exactly one.

    Not a placeholder: a Solana transaction can carry several credits to one
    account, which is why the column exists at all. A Payment on this ledger has
    exactly one Destination and delivers once.
    """
    instance = adapter([payment(UNTAGGED_HASH)])
    instance.find_deposits_to_address(ACCOUNT)

    deposit_service.record_what_nobody_can_claim(db, "XRP", instance)

    assert rows_in(db)[0]["credits"] == 1


def test_the_why_is_the_adapters_own_reason(db, adapter):
    """The row and the log line have to agree, which is what db.py's column comment asks.

    Derived from the line chains/xrp_payments.py already built rather than written
    a second time, so the two cannot drift (rule 8). The hash is stripped because
    the row already carries it in `txid`.

    MUTATION: write a fresh sentence in chains/xrp.py instead of deriving it and
    this test still passes -- which is why it asserts the SUBSTRING the classifier
    produces rather than a literal of its own. Change _classify's wording and this
    fails, which is the drift being caught.
    """
    instance = adapter([payment(UNTAGGED_HASH)])
    instance.find_deposits_to_address(ACCOUNT)

    deposit_service.record_what_nobody_can_claim(db, "XRP", instance)

    why = rows_in(db)[0]["why"]
    assert "NO DestinationTag" in why, f"the classifier's own reason, not a paraphrase: {why!r}"
    assert not why.startswith(UNTAGGED_HASH), (
        "the hash is in the txid column; show_unattributable.py prints both on adjacent lines"
    )


# --- what must NOT become a row --------------------------------------------

def test_a_failed_payment_with_no_tag_gets_no_row(db, adapter):
    """A tec* code is INCLUDED in a ledger, claims a fee, and transfers NOTHING.

    So there is no money to be stranded. Recording one would put a number in the
    table an operator reads as unaccounted-for funds, for a payment that moved
    none -- and the operator would go looking on chain for coins that are not
    there.

    MUTATION: drop the `meta.TransactionResult == tesSUCCESS` requirement -- in
    practice, stop routing the amount through delivered_drops_to() -- and this
    fails with a row for a payment that delivered nothing.
    """
    instance = adapter([payment(FAILED_HASH, meta={"TransactionResult": "tecPATH_DRY"})])
    instance.find_deposits_to_address(ACCOUNT)

    assert instance.unattributable_drops == [], (
        f"a tecPATH_DRY payment delivered nothing: {instance.unattributable_drops}"
    )
    assert deposit_service.record_what_nobody_can_claim(db, "XRP", instance) == 0
    assert rows_in(db) == []


def test_a_payment_that_DID_carry_a_tag_gets_no_row(db, adapter):
    """It is attributable, so it is a deposit event and this mechanism is not about it.

    A row here would be the mirror of the original defect: a credited deposit
    reported to an operator as money nobody can claim, which is the cry-wolf shape
    services/unattributable_deposit_service.unclaimed_events() records being bitten
    by on 2026-10-01.
    """
    instance = adapter([payment(TAGGED_HASH, tag=77)])

    events = instance.find_deposits_to_address(ACCOUNT)

    assert [event["vout"] for event in events] == [77], "a tagged payment is creditable"
    assert instance.unattributable_drops == []
    assert deposit_service.record_what_nobody_can_claim(db, "XRP", instance) == 0
    assert rows_in(db) == []


def test_a_payment_OUT_of_the_account_gets_no_row(db, adapter):
    """account_tx returns everything TOUCHING the account, including payments out.

    A payout this desk sent is not a deposit anybody can claim, and recording one
    would count the desk's own spending as stranded customer money.
    """
    instance = adapter([payment(OUTGOING_HASH, tx={"Destination": OTHER_ACCOUNT})])
    instance.find_deposits_to_address(ACCOUNT)

    assert instance.unattributable_drops == []
    assert deposit_service.record_what_nobody_can_claim(db, "XRP", instance) == 0


def test_a_non_payment_gets_no_row(db, adapter):
    """An OfferCreate or TrustSet touching the account is not a deposit at all."""
    instance = adapter([payment(OFFER_HASH, tx={"TransactionType": "OfferCreate"})])
    instance.find_deposits_to_address(ACCOUNT)

    assert instance.unattributable_drops == []
    assert deposit_service.record_what_nobody_can_claim(db, "XRP", instance) == 0


# --- the two cases that get a LINE instead of a row ------------------------

def test_an_unvalidated_untagged_payment_gets_no_row_but_IS_announced(db, adapter, capsys):
    """An unvalidated ledger can still change, so it is not yet evidence of an amount.

    NO ROW is the safe direction -- a money record written from a ledger that can
    still be rewritten is a record about possibly nothing -- and it costs only the
    few seconds until the next poll finds the same payment validated.

    BUT SILENCE WOULD BE THE DEFECT (rule 14). "Nothing arrived untagged" and
    "something arrived untagged and no row was written for it" are different facts,
    and an operator reading an empty table has to be able to tell them apart.

    MUTATION: delete the `unreadable` half of untagged_payments()' return value, or
    the second print loop in find_deposits_to_address, and the row assertion still
    passes while this one fails -- which is exactly the half that would otherwise
    be lost quietly.
    """
    instance = adapter([payment(UNTAGGED_HASH, validated=False)], min_confirmations=1)
    instance.find_deposits_to_address(ACCOUNT)

    assert instance.unattributable_drops == [], (
        "an unvalidated ledger is not yet evidence of what was delivered"
    )
    assert deposit_service.record_what_nobody_can_claim(db, "XRP", instance) == 0
    printed = capsys.readouterr().out
    assert "NOT RECORDED" in printed, f"the payment has to be announced. Printed:\n{printed}"
    assert UNTAGGED_HASH in printed


def test_an_untagged_issued_currency_payment_gets_no_row_but_IS_announced(db, adapter, capsys):
    """An IOU is not XRP, and `amount` is whole units of XRP.

    Crediting a stranger's token as XRP at face value is the second way this path
    gets drained; RECORDING one as XRP would be the same number under the same
    wrong label, in the table an operator totals up. So no row -- and a line,
    because the coins (such as they are) did arrive.
    """
    issued = payment(UNTAGGED_HASH)
    issued["meta"]["delivered_amount"] = {"currency": "USD", "issuer": "rIssuerSomebodyElse", "value": "900"}
    instance = adapter([issued])
    instance.find_deposits_to_address(ACCOUNT)

    assert instance.unattributable_drops == []
    assert deposit_service.record_what_nobody_can_claim(db, "XRP", instance) == 0
    printed = capsys.readouterr().out
    assert "NOT RECORDED" in printed, f"Printed:\n{printed}"
    assert "ISSUED CURRENCY" in printed, (
        f"the line has to say WHY no row was written, not just that none was. Printed:\n{printed}"
    )


# --- lifetime of the state the recorder reads ------------------------------

def test_the_list_is_cleared_per_call(adapter, monkeypatch):
    """A watcher holds one adapter for its life and scans once per active swap.

    An accumulating list hands record_what_nobody_can_claim() a payment from an
    hour ago as though it had just been read, and -- worse -- keeps reporting it
    after an operator has resolved it and the money has been moved.

    MUTATION: delete `self.unattributable_drops = []` from the top of
    find_deposits_to_address and this fails with 1 drop on the second scan, where
    the account now holds nothing untagged.
    """
    instance = adapter([payment(UNTAGGED_HASH)])
    instance.find_deposits_to_address(ACCOUNT)
    assert len(instance.unattributable_drops) == 1, "the first scan found it"

    monkeypatch.setattr("chains.xrp.requests.post", SeededTransport([payment(TAGGED_HASH, tag=9)]))
    instance.find_deposits_to_address(ACCOUNT)

    assert instance.unattributable_drops == [], (
        "the list describes THIS scan, and this scan found nothing untagged"
    )


def test_the_attribute_exists_before_any_scan(adapter):
    """record_what_nobody_can_claim() asks by NAME, so the name has to be there.

    It reads getattr(adapter, "unattributable_drops", None) and returns 0 on a
    miss, which means a missing attribute is INDISTINGUISHABLE from an account
    with nothing stranded in it. That ambiguity is the defect this whole mechanism
    exists to remove, and it must not come back through construction order -- an
    adapter built and read before its first scan has to answer "nothing yet", not
    "I do not have that".

    MEASURED BEFORE THE FIX: hasattr(XRPAdapter, "unattributable_drops") was False
    and a constructed instance's vars() were {_reported_deferrals, can_spend,
    min_confirmations, payout_refusal, timeout, url}.
    """
    instance = adapter([])

    assert hasattr(instance, "unattributable_drops")
    assert instance.unattributable_drops == []


# NO SECOND KEY-MATERIAL TEST LIVES HERE, AND THAT IS DELIBERATE (rule 8).
#
# tests/test_xrp_adapter.py already walks vars() on a constructed XRPAdapter and asserts
# that no attribute NAME contains "seed", "secret" or "key" and that no value is
# seed-shaped. Its own comment says anything added to __init__ is visible to it "by
# construction", and `unattributable_drops` is -- that test was re-run with this attribute
# present and still passes, which is the measurement rather than the claim.
#
# A COPY OF IT HERE WAS WRITTEN AND DELETED, and the reason is worth recording because the
# deleted version FAILED for a reason that looks like a finding and is not. It asserted
# over repr(vars(instance)) -- values as well as names -- and tripped on
# `payout_refusal`, which legitimately names the ENVIRONMENT VARIABLE
# XRP_PAYOUT_SECRET_SEED in a sentence an operator reads. The variable's NAME is not its
# value, chains/xrp_payout_seed.py is arranged so only the name is ever rendered, and a
# test that cannot tell those apart would have to be weakened until it asserted nothing.


# --- the pure decision, with no adapter at all -----------------------------

def test_untagged_payments_is_pure_and_separates_the_two_outcomes():
    """One call over a mixed list: what gets a credit, what gets a line, what gets neither.

    Called directly with seeded entries and no adapter, which is the property rule
    10 asks for -- the decision is a function, so it can be asserted on without a
    server, a database, or a scan.

    THE MIXED LIST IS THE POINT. Each case has its own test above; this one pins
    that they do not interfere. A filter that worked on a one-element list and
    dropped everything after the first refusal would pass every test above.
    """
    credits, lines = untagged_payments([
        payment(UNTAGGED_HASH),
        payment(FAILED_HASH, meta={"TransactionResult": "tecUNFUNDED_PAYMENT"}),
        payment(TAGGED_HASH, tag=1),
        payment(OUTGOING_HASH, tx={"Destination": OTHER_ACCOUNT}),
        payment(OFFER_HASH, tx={"TransactionType": "OfferCreate"}),
    ], ACCOUNT, 1)

    assert [credit.signature for credit in credits] == [UNTAGGED_HASH]
    assert credits[0].amount == DELIVERED_XRP
    assert credits[0].address == ACCOUNT
    assert lines == [], "nothing in this list arrived untagged-and-unreadable"


def test_untagged_payments_handles_an_empty_and_a_None_response():
    """`(none)` is a result; a crash is not (rule 14).

    account_tx on a brand-new account returns no transactions, and
    find_deposits_to_address passes `result.get("transactions") or []`, so both
    shapes reach here.
    """
    assert untagged_payments([], ACCOUNT, 1) == ([], [])
    assert untagged_payments(None, ACCOUNT, 1) == ([], [])
