"""The judgments xrp_chain_check.py makes, called directly.

Role: test (pure functions; no socket, no server)
Reads: xrp_chain_check.py
Writes: nothing
Can move funds: no
Mainnet-safe: yes

WHY THESE MATTER MORE THAN USUAL

This script is the only thing that can discover an XRP wire-format error, and
it has already found one: the transaction body arrives flat on the entry, where
chains/xrp_payments.py looked only under `tx`, so payments were skipped
silently. 620 tests passed with that bug in place, because a seeded test cannot
discover a shape it seeded.

So how this script REPORTS is load-bearing. A run that confirmed nothing must
not print PASSED, and an empty scan over a response that did contain inbound
payments must be a failure rather than a quiet zero.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# The path insert above has to run first: xrp_chain_check.py lives at the
# project root, which conftest.py does not put on sys.path.
from xrp_chain_check import (
    collect_payments,
    delivered_amount_findings,
    exit_code,
    network_banner,
    payment_field_report,
    server_findings,
    unwrap_shape,
    verdict_text,
)


def flat_payment(tag=4242, drops="25000000", result="tesSUCCESS", txhash="C" * 64):
    """The shape a real rippled 3.4.1 actually sent, observed 2026-09-25."""
    entry = {
        "TransactionType": "Payment",
        "Destination": "rngeSWu7x9H3QCNfZGufYGCykQJHiaV91p",
        "Amount": drops,
        "hash": txhash,
        "validated": True,
        "metaData": {"TransactionResult": result, "delivered_amount": drops},
    }
    if tag is not None:
        entry["DestinationTag"] = tag
    return entry


# --- naming the network -----------------------------------------------------

def test_mainnet_is_shouted_and_nothing_else_is():
    """A hostname can be proxied or reused; a network id cannot be mistaken."""
    assert "MAINNET, REAL MONEY" in network_banner(0)
    for other in (1, 2, 1024):
        assert "MAINNET" not in network_banner(other)


def test_a_missing_network_id_says_it_cannot_be_confirmed():
    """"Not mainnet" and "I could not tell" are different claims (rule 17)."""
    line = network_banner(None)
    assert "cannot confirm" in line
    assert "not mainnet" not in line


# --- server_info ------------------------------------------------------------

def test_a_complete_server_info_has_no_findings():
    assert server_findings({"validated_ledger": {"reserve_base_xrp": 1, "reserve_inc_xrp": 0.2}}) == []


def test_a_missing_reserve_is_reported_because_the_adapter_refuses_without_it():
    findings = server_findings({"validated_ledger": {"reserve_inc_xrp": 0.2}})
    assert len(findings) == 1
    assert "reserve_base_xrp" in findings[0]


def test_no_validated_ledger_at_all_is_reported():
    assert server_findings({}) != []


# --- the nesting that caused the bug ---------------------------------------

def test_all_three_nestings_are_named():
    assert "FLAT" in unwrap_shape(flat_payment())
    assert "`tx`" in unwrap_shape({"tx": {"TransactionType": "Payment"}, "meta": {}})
    assert "`tx_json`" in unwrap_shape({"tx_json": {"TransactionType": "Payment"}, "meta": {}})


def test_an_unrecognized_nesting_is_called_out_in_capitals():
    """This is the one that must not read as "no payments"."""
    assert "UNRECOGNIZED" in unwrap_shape({"ledger_index": 21050264, "validated": True})


def test_collect_payments_finds_payments_in_the_flat_shape():
    """The shape the probe observed, and the one the old unwrap missed."""
    found = collect_payments([flat_payment(), {"TransactionType": "TrustSet", "hash": "F" * 64}])
    assert len(found) == 1
    body, meta = found[0]
    assert body["DestinationTag"] == 4242
    assert meta["delivered_amount"] == "25000000"


# --- the field report -------------------------------------------------------

def test_every_field_present_is_no_failures():
    lines, failures = payment_field_report(collect_payments([flat_payment()]))
    assert failures == []
    assert any("present in all 1" in line for line in lines)


def test_a_missing_required_field_is_a_failure():
    broken = flat_payment()
    del broken["metaData"]["delivered_amount"]
    lines, failures = payment_field_report(collect_payments([broken]))
    assert len(failures) == 1
    assert "delivered_amount" in failures[0]
    assert any("MISSING and required" in line for line in lines)


def test_a_missing_destination_tag_is_not_a_failure_and_says_why():
    """Absent on ordinary wallet traffic, so its absence proves nothing about us."""
    lines, failures = payment_field_report(collect_payments([flat_payment(tag=None)]))
    assert failures == []
    line = next(line for line in lines if "DestinationTag" in line)
    assert "normal wallet traffic" in line


def test_a_field_present_in_only_some_payments_is_shown_as_a_fraction():
    """Counted per payment, not unioned -- the correction monero_chain_check needed."""
    lines, _ = payment_field_report(collect_payments([flat_payment(), flat_payment(tag=None, txhash="D" * 64)]))
    line = next(line for line in lines if "DestinationTag" in line)
    assert "present in 1/2" in line


def test_no_payments_means_unconfirmed_and_never_a_failure():
    lines, failures = payment_field_report([])
    assert failures == []
    assert "UNCONFIRMED" in lines[0]


# --- the partial payment defense, against real responses -------------------

def test_a_missing_delivered_amount_is_flagged_as_the_one_that_matters():
    broken = flat_payment()
    del broken["metaData"]["delivered_amount"]
    findings = delivered_amount_findings(collect_payments([broken]))
    assert len(findings) == 1
    assert "THIS IS THE ONE THAT MATTERS" in findings[0]
    assert "partial-payment exploit" in findings[0]


def test_an_issued_currency_is_not_a_findings_failure_here():
    """chains/xrp_payments.py refuses it, which is correct behavior, not a defect.

    This script checks the SHAPE the adapter depends on; an IOU arriving is the
    ledger doing something legal that the adapter then declines.
    """
    iou = flat_payment()
    iou["metaData"]["delivered_amount"] = {"currency": "USD", "issuer": "rIssuer", "value": "1"}
    assert delivered_amount_findings(collect_payments([iou])) == []


def test_a_numeric_delivered_amount_is_flagged():
    """Drop counts are strings so clients cannot round them through a double."""
    numeric = flat_payment()
    numeric["metaData"]["delivered_amount"] = 25000000
    findings = delivered_amount_findings(collect_payments([numeric]))
    assert len(findings) == 1
    assert "not a drop string" in findings[0]


# --- the verdict, which has three outcomes ---------------------------------

def test_a_clean_run_over_real_payments_passes():
    assert verdict_text([], 3).startswith("PASSED")
    assert "3 real Payment" in verdict_text([], 3)


def test_a_clean_run_over_nothing_does_not_say_passed():
    """THE VERDICT THIS FILE EXISTS FOR."""
    text = verdict_text([], 0)
    assert "PASSED" not in text
    assert "NOT A PASS" in text


def test_failures_are_listed_individually():
    text = verdict_text(["first thing", "second thing"], 5)
    assert text.startswith("FAILED: 2")
    assert "first thing" in text and "second thing" in text


@pytest.mark.parametrize(("failures", "examined", "expected"), [
    ([], 3, 0),
    ([], 0, 3),
    (["broken"], 3, 1),
    (["broken"], 0, 1),
])
def test_the_exit_code_distinguishes_inconclusive_from_pass(failures, examined, expected):
    """Rule 13: "did nothing" must not report the same way as "did work".

    Exit 3 for inconclusive, so a shell `&&` or a CI step cannot mistake a run
    that examined nothing for a confirmed adapter.
    """
    assert exit_code(failures, examined) == expected
