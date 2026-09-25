"""The judgments monero_chain_check.py makes, called directly.

Role: test (pure functions; no daemon, no socket, no database)
Reads: monero_chain_check.py
Writes: nothing
Can move funds: no
Mainnet-safe: yes

These exist because of what the script is FOR. It is the one thing that can
confirm the Monero adapter's field names against a real wallet, so the way it
reports is load-bearing: a run that confirmed nothing must not print PASSED,
and a required field that is missing must not be rendered as a note.

The script's own transport cannot be unit tested without a wallet. Its
judgments can, and they are the part that decides what the operator believes.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# The path insert above has to run before this import: monero_chain_check.py
# lives at the project root, which conftest.py does not put on sys.path.
from monero_chain_check import (
    address_findings,
    balance_findings,
    exit_code,
    network_banner,
    transfer_field_report,
    verdict_text,
)


def test_a_good_balance_result_has_no_findings():
    assert balance_findings({"balance": 9_000_000_000_000, "unlocked_balance": 2_000_000_000_000}) == []


def test_a_missing_balance_field_is_reported_by_name():
    """Which field is missing is the diagnosis, so the message has to name it."""
    findings = balance_findings({"balance": 1})
    assert len(findings) == 1
    assert "unlocked_balance" in findings[0]


def test_a_float_balance_is_reported_as_the_scale_error_it_is():
    findings = balance_findings({"balance": 1, "unlocked_balance": 2.0})
    assert len(findings) == 1
    assert "factor of a trillion" in findings[0]


def test_a_bool_is_not_accepted_as_an_integer_balance():
    """bool is a subclass of int in Python, so `isinstance(True, int)` is True.

    A wallet returning `true` for a balance would otherwise pass straight
    through and be read as one atomic unit.
    """
    assert balance_findings({"balance": 1, "unlocked_balance": True}) != []


def test_a_wallet_calling_its_own_address_invalid_is_a_finding():
    """It cannot be right, so it means the method or its result shape is wrong."""
    findings = address_findings({"valid": False, "nettype": "stagenet"})
    assert len(findings) == 1
    assert "OWN primary address" in findings[0]


def test_a_missing_nettype_is_a_finding():
    """A port is not proof of a network."""
    findings = address_findings({"valid": True})
    assert len(findings) == 1
    assert "nettype" in findings[0]


def test_mainnet_is_announced_in_capitals_and_nothing_else_is():
    assert network_banner("mainnet") == "  <- REAL MONEY"
    for other in ("stagenet", "testnet", "(not reported)"):
        assert network_banner(other) == ""


def transfer(**overrides):
    base = {
        "txid": "a" * 64, "amount": 1, "address": "8B", "subaddr_index": {"major": 0, "minor": 1},
        "confirmations": 10, "type": "in", "unlock_time": 0, "locked": False, "double_spend_seen": False,
    }
    base.update(overrides)
    return base


def test_every_field_present_is_no_failures():
    lines, failures = transfer_field_report([transfer()])
    assert failures == []
    assert any("OK txid" in line and "present in all 1" in line for line in lines)


def test_a_missing_required_field_is_a_failure():
    """The four required names are what chains/monero_transfers.py raises without."""
    entry = transfer()
    del entry["subaddr_index"]
    lines, failures = transfer_field_report([entry])
    assert len(failures) == 1
    assert "subaddr_index" in failures[0]
    assert "REQUIRES" in failures[0]
    assert any("MISSING and required" in line for line in lines)


def test_a_missing_optional_field_is_reported_but_is_not_a_failure():
    """The adapter tolerates these, so their absence is information, not a fault."""
    entry = transfer()
    del entry["locked"]
    lines, failures = transfer_field_report([entry])
    assert failures == []
    assert any("locked" in line and "optional" in line for line in lines)


def test_no_transfers_means_unconfirmed_and_never_a_failure():
    """"I could not check" and "it is wrong" are different sentences (rule 17).

    With nothing to look at, every name must read as unconfirmed -- and none of
    them may be counted as a failure, because that would make an empty wallet
    look like a broken adapter.
    """
    lines, failures = transfer_field_report([])
    assert failures == []
    assert all("unconfirmed" in line for line in lines if "ignores" not in line)


def test_fields_the_adapter_does_not_read_are_listed():
    """So a rename shows up as a new name appearing beside a required one going missing."""
    lines, _ = transfer_field_report([transfer(payment_id="00", fee=1000)])
    ignored = next(line for line in lines if "ignores" in line)
    assert "fee" in ignored
    assert "payment_id" in ignored


def test_nothing_ignored_prints_none_rather_than_blank():
    """Rule 14: never let an empty result print nothing."""
    lines, _ = transfer_field_report([transfer()])
    assert "(none)" in next(line for line in lines if "ignores" in line)


def test_a_clean_run_over_real_transfers_passes():
    assert verdict_text([], 3).startswith("PASSED")
    assert "3 real transfer" in verdict_text([], 3)


def test_a_clean_run_over_an_empty_wallet_does_not_say_passed():
    """THE VERDICT THIS FILE EXISTS FOR.

    No failures plus no transfers confirmed NOTHING about the field names.
    Printing PASSED for it would be the instrument reporting more than the run
    established -- the pattern this repository has now been bitten by four
    separate times.
    """
    text = verdict_text([], 0)
    assert "PASSED" not in text
    assert "NOT THE SAME AS A PASS" in text


def test_failures_are_listed_individually_not_counted():
    """A count tells the operator how bad it is; the list tells them what to fix."""
    text = verdict_text(["first thing wrong", "second thing wrong"], 5)
    assert text.startswith("FAILED: 2")
    assert "first thing wrong" in text
    assert "second thing wrong" in text


@pytest.mark.parametrize("count", [0, 1, 99])
def test_failures_beat_an_empty_wallet_in_the_verdict(count):
    """A failing run must never be reported as "nothing was confirmed"."""
    assert verdict_text(["something broke"], count).startswith("FAILED")


def test_a_field_missing_from_only_some_transfers_is_shown_as_a_fraction():
    """The union says the NAME exists; a human reads it as "every row has it".

    Found by running monero_chain_check.py against a stand-in wallet where one
    of two transfers lacked `subaddr_index`: step 3 printed "OK present" and
    step 4 then refused that very row. Both were right and the pair was
    misleading, so the count is now on the line.
    """
    partial = transfer()
    del partial["subaddr_index"]
    lines, failures = transfer_field_report([transfer(), partial])
    assert failures == []
    line = next(line for line in lines if "subaddr_index" in line)
    assert "present in 1/2" in line
    assert "1 transfer(s) lack it" in line


def test_a_field_present_in_every_transfer_says_so_explicitly():
    lines, _ = transfer_field_report([transfer(), transfer()])
    assert any("txid" in line and "present in all 2" in line for line in lines)


def test_an_inconclusive_run_does_not_exit_like_a_pass():
    """Rule 13: "did nothing" must not report the same way as "did work".

    Exit 3, not 0, so a shell `&&`, a CI step or an operator reading $? cannot
    mistake a wallet with no transfers for a confirmed adapter. This was
    shipped as 0 and corrected in the same session.
    """
    assert exit_code([], 0) == 3
    assert exit_code([], 4) == 0
    assert exit_code(["broken"], 4) == 1
    assert exit_code(["broken"], 0) == 1
