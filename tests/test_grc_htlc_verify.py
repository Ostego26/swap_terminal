"""Does CHECKLOCKTIMEVERIFY run on Gridcoin -- and does the harness say so honestly?

Role: tests (offline; no daemon, no network, no chain)
Reads: grc_htlc_verify.py
Writes: nothing
Can move funds: no
Mainnet-safe: yes
Live-safe: yes

The harness itself is the on-chain measurement. What is tested HERE is the part a chain
cannot check: that the verdict matches what happened, that the dangerous outcome is scored
FAIL and says what it costs, and that a SKIP is never rendered as a pass.

THE DANGEROUS OUTCOME IS THE ONE WORTH THE MOST HERE. If Gridcoin ACCEPTED a final refund
whose nLockTime is below the script's locktime, CLTV did not enforce -- and every HTLC this
repository funds on that chain could be refunded by its funder at any time, including while
the counterparty can still claim the hashlock. A harness that reported that quietly, or as a
pass, would be worse than no harness.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

from modules.htlc_timelock import ROLE_INITIATOR, contract_locktime
from regtest.console import FAIL, OK, SKIP


def _entry():
    spec = importlib.util.spec_from_file_location(
        "grc_htlc_verify_under_test", Path(__file__).resolve().parents[1] / "grc_htlc_verify.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# THE CRUX OF THE HARNESS, IN ONE ASSERTION, and a mutant swapping it SURVIVED until this
# existed. Step 6 must build its refund with nLockTime = THE CURRENT TIP. That is what makes
# the transaction FINAL, which is what makes the mempool's finality check pass, which is what
# leaves the script as the only rule that can refuse it.
#
# Build it with the SCRIPT's locktime instead and the transaction is non-final: it is still
# refused, the check still goes OK, the run still reports a pass -- and it has measured
# nothing except that step 5 happens twice. Nothing about the output would say so.
_NLOCKTIME_IS_THE_MEASUREMENT = (
    "step 6 must build its refund with nLockTime = THE TIP. With the script's locktime the "
    "transaction is NOT FINAL, the mempool refuses it before any script runs, and the run "
    "reports CLTV enforcement having measured only that step 5 happens twice"
)


def _outcome(**overrides):
    base = {"non_final_refused": SKIP, "cltv_refused_final": SKIP, "refund_accepted": SKIP, "notes": []}
    base.update(overrides)
    return base


def test_the_pass_verdict_needs_BOTH_the_refusal_and_the_acceptance():
    """Either alone is not the measurement.

    A refusal with no acceptance could be any broken thing about the spend -- a bad signature,
    a dust output, the wrong key. An acceptance with no refusal says the script is spendable
    and nothing about whether the timelock held. The pair is the experiment.
    """
    entry = _entry()
    passed = entry.verdict(_outcome(cltv_refused_final=OK, refund_accepted=OK))
    assert "EXECUTES AND ENFORCES" in passed
    for half in (_outcome(cltv_refused_final=OK), _outcome(refund_accepted=OK)):
        assert "EXECUTES AND ENFORCES" not in entry.verdict(half)


def test_the_pass_verdict_REFUSES_to_claim_the_production_timelock_was_tested():
    """The run uses tip+6, not contract_locktime()'s tip+1920. A verdict that did not say so
    would be read as "the GRC HTLC timelock is tested", which is a different and untrue claim
    -- 48 hours of waiting is exactly why this was never measured before."""
    entry = _entry()
    passed = entry.verdict(_outcome(cltv_refused_final=OK, refund_accepted=OK))
    assert "does NOT test" in passed or "not test contract_locktime" in passed
    assert "1920" in passed, "and it names the production value so the difference is concrete"


def test_a_SKIP_is_never_rendered_as_a_pass():
    entry = _entry()
    assert "NOT ESTABLISHED" in entry.verdict(_outcome())
    assert "NOT ESTABLISHED" in entry.verdict(_outcome(refund_accepted=OK))


def test_a_FAIL_dominates_and_says_nothing_is_softened():
    entry = _entry()
    assert "DID NOT BEHAVE" in entry.verdict(_outcome(cltv_refused_final=FAIL, refund_accepted=OK))
    assert "DID NOT BEHAVE" in entry.verdict(_outcome(cltv_refused_final=OK, refund_accepted=FAIL))


def test_a_chain_that_ACCEPTS_the_final_early_refund_is_FAIL_and_names_what_it_costs(monkeypatch):
    """THE SINGLE MOST IMPORTANT THING THIS HARNESS COULD DISCOVER.

    A final refund accepted below the script's locktime means CLTV did not enforce. The note
    has to say the consequence in as many words -- that the funder could refund at any time,
    while the counterparty can still claim the hashlock -- because "cltv_refused_final=FAIL" on
    its own does not tell an operator to stop funding Gridcoin HTLCs.
    """
    entry = _entry()

    class _AcceptingRun:
        asset = "GRC"

        def step(self, *a, **k):
            pass

        def say(self, *a, **k):
            pass

        def check(self, *a, **k):
            pass

        def node(self, wallet=True):
            raise AssertionError("not reached")

    outcome = _outcome()
    built_with: list[int] = []
    monkeypatch.setattr(entry.adaptor_steps, "current_height", lambda run: 100)
    monkeypatch.setattr(entry, "_refund_bytes",
                        lambda run, contract, outpoint, nlocktime: built_with.append(nlocktime) or "00")
    monkeypatch.setattr(entry.adaptor_steps, "mempool_reject_reason", lambda run, raw: "")
    monkeypatch.setattr(entry.adaptor_steps, "broadcast_and_report",
                        lambda run, raw, label: ("ee" * 32, ""))

    entry.step_6_cltv(_AcceptingRun(), {"locktime": 200}, None, outcome)

    assert built_with == [100], _NLOCKTIME_IS_THE_MEASUREMENT

    assert outcome["cltv_refused_final"] == FAIL
    note = " ".join(outcome["notes"])
    assert "did not enforce" in note
    assert "refunded by its funder AT ANY TIME" in note
    assert "still able to claim the hashlock" in note
    assert "until that is explained" in note, "and it says to stop, not just what happened"


def test_a_chain_that_REFUSES_it_is_OK_and_earns_no_note(monkeypatch):
    """The other direction, or the test above would pass with `= FAIL` hard-coded."""
    entry = _entry()

    class _RefusingRun:
        asset = "GRC"

        def step(self, *a, **k):
            pass

        def say(self, *a, **k):
            pass

        def check(self, *a, **k):
            pass

    outcome = _outcome()
    built_with: list[int] = []
    monkeypatch.setattr(entry.adaptor_steps, "current_height", lambda run: 100)
    monkeypatch.setattr(entry, "_refund_bytes",
                        lambda run, contract, outpoint, nlocktime: built_with.append(nlocktime) or "00")
    monkeypatch.setattr(entry.adaptor_steps, "mempool_reject_reason", lambda run, raw: "")
    monkeypatch.setattr(entry.adaptor_steps, "broadcast_and_report",
                        lambda run, raw, label: (None, "code=-22 TX rejected"))

    entry.step_6_cltv(_RefusingRun(), {"locktime": 200}, None, outcome)

    assert built_with == [100], _NLOCKTIME_IS_THE_MEASUREMENT
    assert outcome["cltv_refused_final"] == OK
    assert outcome["notes"] == []


def test_the_test_locktime_is_short_and_the_production_one_is_not():
    """Pinned against the REAL contract_locktime(), so the docstring's "48 hours" cannot drift
    from what the function returns. If production ever shortens to something a run could wait
    out, this test says so and the harness can stop using a test value."""
    entry = _entry()
    tip = 3_000_000
    production = contract_locktime("GRC", ROLE_INITIATOR, tip) - tip
    assert production > entry.LOCKTIME_BLOCKS_AHEAD
    assert production > 1000, (
        f"production is tip+{production} blocks; if it ever drops to something a run could wait "
        f"out, this harness should use the real value instead of a test one"
    )


def test_the_preimage_is_generated_but_never_used_and_never_returned():
    """The hashlock branch is already established by three live swaps; the REFUND is the gap.
    A preimage in the returned dict is a preimage that can be printed by a caller reporting on
    the contract, and this repository's rule is that it never appears anywhere."""
    entry = _entry()

    class _QuietRun:
        asset = "GRC"

        def step(self, *a, **k):
            pass

        def say(self, *a, **k):
            pass

    contract = entry.build_contract(_QuietRun(), 3_000_000)
    assert "preimage" not in contract and "secret" not in contract
    assert set(contract) == {"participant", "refund", "locktime", "redeem_script", "secret_hash"}


def test_the_contract_locktime_is_the_tip_plus_the_named_constant():
    entry = _entry()

    class _QuietRun:
        asset = "GRC"

        def step(self, *a, **k):
            pass

        def say(self, *a, **k):
            pass

    contract = entry.build_contract(_QuietRun(), 3_000_000)
    assert contract["locktime"] == 3_000_000 + entry.LOCKTIME_BLOCKS_AHEAD


def test_the_exit_code_is_non_zero_unless_both_decisive_outcomes_are_OK():
    """Driven through the harness's own `established()`, not recomputed beside it.

    The first version of this test rebuilt the condition in the test body and compared it to
    itself -- ruff caught it as an unused `entry`, which is the linter noticing a test that
    measures nothing. The decision is a named function now so there is something to drive.
    """
    entry = _entry()
    assert entry.established(_outcome(cltv_refused_final=OK, refund_accepted=OK))
    for short in (
        _outcome(cltv_refused_final=OK),
        _outcome(refund_accepted=OK),
        _outcome(cltv_refused_final=FAIL, refund_accepted=OK),
        _outcome(cltv_refused_final=OK, refund_accepted=FAIL),
        _outcome(),
    ):
        assert not entry.established(short), f"{short} is not a pass"
