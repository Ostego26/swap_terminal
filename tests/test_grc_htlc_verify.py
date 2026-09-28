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

import hashlib
import importlib.util
import io
from pathlib import Path

import pytest
from modules import adaptor_swap_chain as chain
from modules.htlc_timelock import ROLE_INITIATOR, contract_locktime
from regtest.console import FAIL, OK, SKIP, Console
from regtest.keys import generate_key, key_from_seed


class _SilentRun:
    """A Run that answers the three things a step calls and refuses everything else.

    `node()` raises rather than returning a stub: a test that reaches the daemon is a test
    asserting something other than what it says it does, and it should say so loudly.
    """

    asset = "GRC"

    def step(self, *a, **k):
        pass

    def say(self, *a, **k):
        pass

    def check(self, *a, **k):
        pass

    def node(self, wallet=True):
        raise AssertionError("no daemon may be reached here")


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


#: A SEED FOR THE TESTS, and it has to be set because build_contract now derives the refund key
#: from it. That is not test scaffolding working around the code: it is the property under test
#: in test_the_refund_key_is_RECOVERABLE_rather_than_minted_and_thrown_away below, which is
#: there because a fresh key stranded 1.50 GRC on this harness's first real run.
_A_SEED_FOR_TESTS = "a seed that is not the operator's"


def _with_seed(monkeypatch):
    monkeypatch.setenv("ST_ADAPTOR_FUNDING_SEED", _A_SEED_FOR_TESTS)


def test_the_refund_key_is_RECOVERABLE_rather_than_minted_and_thrown_away(monkeypatch):
    """1.50 GRC, STRANDED ON THIS FILE'S FIRST REAL RUN, and not by the thing under test.

    `refund` was `generate_key()`: a keypair living only in this process and written nowhere.
    The run funded it, died at step 4, and python exited -- and e1f8ae8f961d8591:0 became
    unspendable by anyone, forever. docs/branch_coverage.md gap (c) records atomic_swap.py doing
    exactly this and already costing 310.72 GRC testnet. This file reproduced it immediately.

    A seed-derived key has a stable address, so a failed run leaves its coins where the NEXT run
    can spend them and reclaim_funding.py can sweep them in between.

    THE PARTICIPANT KEY STAYS RANDOM and the asymmetry is the point: it is the hashlock side,
    nothing is ever paid to it, and a key that never receives cannot strand anything. Asserting
    they DIFFER pins that too -- deriving both from one role would silently make the hashlock
    and refund branches the same key, which would make the whole contract meaningless.
    """
    entry = _entry()
    _with_seed(monkeypatch)
    built = {}
    monkeypatch.setattr(entry, "build_htlc_redeem_script",
                        lambda **kw: built.update(kw) or b"\x51")

    first = entry.build_contract(_SilentRun(), 100)
    second = entry.build_contract(_SilentRun(), 100)

    assert first["refund"].address == second["refund"].address, (
        "two runs must reach the same refund address, or a failed run strands its funding"
    )
    assert first["refund"].address == key_from_seed(_A_SEED_FOR_TESTS, entry.REFUND_ROLE).address
    assert built["participant_address"] != first["refund"].address, (
        "the hashlock and refund branches must not be the same key"
    )


def test_running_WITHOUT_A_SEED_is_refused_before_anything_is_built(monkeypatch):
    """No seed used to mean "mint a fresh refund key", which is the stranding above by default.

    There is no sensible no-seed mode for this file: the address the operator funds comes from
    the seed too, so a run without one could not be funded anyway. It refuses by name rather
    than failing later with something about a missing payment.
    """
    entry = _entry()
    monkeypatch.delenv("ST_ADAPTOR_FUNDING_SEED", raising=False)
    with pytest.raises(entry.RegtestSetupError) as raised:
        entry.build_contract(_SilentRun(), 100)
    assert "ST_ADAPTOR_FUNDING_SEED is not set" in str(raised.value)
    assert "Nothing was built or broadcast" in str(raised.value)


def test_the_preimage_is_generated_but_never_used_and_never_returned(monkeypatch):
    """The hashlock branch is already established by three live swaps; the REFUND is the gap.
    A preimage in the returned dict is a preimage that can be printed by a caller reporting on
    the contract, and this repository's rule is that it never appears anywhere."""
    _with_seed(monkeypatch)
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


def test_the_contract_locktime_is_the_tip_plus_the_named_constant(monkeypatch):
    _with_seed(monkeypatch)
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


def test_the_contract_is_funded_with_the_key_that_OWNS_the_output(monkeypatch):
    """FOUR RUNS REFUSED BY THE CHAIN, and nothing local had anything to say about it.

    `fund_contract` signed with `operator_funding_key(run)` an output that
    `prepare_operator_funding` had already paid to `contract["refund"]`. The process holds BOTH
    keys, so the transaction built, signed, serialized, and predicted its own txid; every check
    this harness makes passed. Gridcoin answered `-22 TX rejected`, which names nothing, and the
    real answer was in the operator's debug.log all along:

        2026-09-28T17:04:26Z ERROR: ConnectInputs() : 39c099481d VerifySignature failed

    A signature made with the wrong one of two keys you are holding is a perfectly well-formed
    signature. That is why this is asserted on the KEY handed to the signer rather than on the
    bytes: the bytes look right either way, which is the whole defect.
    """
    entry = _entry()
    seen = {}

    def _record(run, key, source, destination_script):
        seen["key"] = key
        return "00", "cd" * 32, 149_000_000

    monkeypatch.setattr(entry.adaptor_steps, "reclaim_p2pkh_to_script", _record)
    monkeypatch.setattr(entry.adaptor_steps, "broadcast_and_report", lambda run, raw, label: ("cd" * 32, "accepted"))
    monkeypatch.setattr(entry.adaptor_steps, "wait_or_mine_to", lambda run, height: None)
    monkeypatch.setattr(entry.adaptor_steps, "current_height", lambda run: 100)
    monkeypatch.setattr(entry.adaptor_steps, "operator_funding_key", lambda run: generate_key())

    refund = generate_key()
    contract = {"redeem_script": b"\x51", "refund": refund, "locktime": 106}
    funding = chain.Outpoint(txid="ab" * 32, vout=0, value_satoshis=150_000_000)

    entry.fund_contract(_SilentRun(), contract, funding)

    assert seen["key"] is refund, (
        "the coin belongs to the REFUND key by the time this runs -- the funding key's output "
        "was consumed one step earlier, when it was split into this one"
    )


def test_THE_WHOLE_CONTRACT_is_reconstructible_from_the_seed_and_the_locktime(monkeypatch):
    """1.49 GRC, STRANDED HOURS AFTER THE REFUND KEY WAS "FIXED", 2026-09-28.

    The earlier fix made the refund KEY seed-derived and stated the rule as "only keys that
    RECEIVE have to be recoverable". The participant key receives nothing, so it stayed random
    -- and that is wrong for P2SH in a way that is easy to miss and expensive to learn.

    A P2SH output is spent by presenting the WHOLE REDEEM SCRIPT, and the participant key's
    hash160 is inside it. Lose that key and the script cannot be rebuilt; lose the script and NO
    branch can be spent -- not the hashlock, and not the refund, however recoverable the refund
    key is. `8cd8531b675b66b3…` holds 1.49 GRC behind a script nobody can reproduce, and it was
    funded by a run whose refund key was perfectly recoverable. Everything looked fixed.

    THE CORRECTED RULE, which this test is the mechanical form of: every input to the redeem
    script must be reconstructible. That is the secret hash, BOTH addresses and the locktime --
    so the contract is a function of (seed, locktime), and the locktime is printed on every run.

    The preimage is asserted too, and its determinism would be WRONG in a real swap, where the
    point is that only one party knows it until the redeem publishes it. It is right here
    because this harness is both parties on a test network, and the alternative is a funded
    contract nobody can spend. It is still never printed.
    """
    entry = _entry()
    _with_seed(monkeypatch)
    built = []
    monkeypatch.setattr(entry, "build_htlc_redeem_script",
                        lambda **kw: built.append(kw) or b"\x51")

    first = entry.build_contract(_SilentRun(), 100)
    second = entry.build_contract(_SilentRun(), 100)

    assert built[0] == built[1], (
        "two runs at the same tip must build the IDENTICAL redeem script, or a crashed run "
        f"leaves coins behind one that cannot be rebuilt. Got {built[0]} then {built[1]}"
    )
    assert first["refund"].address == second["refund"].address
    assert built[0]["participant_address"] == built[1]["participant_address"], (
        "the participant address is INSIDE the script -- random here is what stranded 1.49 GRC"
    )
    assert built[0]["secret_hash"] == built[1]["secret_hash"], (
        "and so is the secret hash, so the preimage behind it has to be derived too"
    )

    # A DIFFERENT LOCKTIME MUST BE A DIFFERENT CONTRACT, or two runs would share a preimage and
    # the second would be spendable by anyone who watched the first one's redeem.
    entry.build_contract(_SilentRun(), 200)
    assert built[2]["secret_hash"] != built[0]["secret_hash"]
    assert built[2]["locktime"] != built[0]["locktime"]

    # AND THE PREIMAGE IS THE ONE THE HASH COMMITS TO -- a derivation that did not round-trip
    # would rebuild an unspendable hashlock branch and look correct doing it.
    preimage = entry.contract_preimage(_A_SEED_FOR_TESTS, 106)
    assert hashlib.sha256(preimage).hexdigest() == built[0]["secret_hash"]
    assert len(preimage) == entry.PREIMAGE_BYTES


def test_the_participant_and_refund_keys_are_DIFFERENT_keys(monkeypatch):
    """Deriving both from one role would make the hashlock and refund branches the same key,
    and the contract would mean nothing -- either branch would be spendable by either party.

    The roles are what keep derivations from a single seed apart, which is the same mechanism
    the funding address already relies on.
    """
    entry = _entry()
    _with_seed(monkeypatch)
    built = []
    monkeypatch.setattr(entry, "build_htlc_redeem_script", lambda **kw: built.append(kw) or b"\x51")
    contract = entry.build_contract(_SilentRun(), 100)
    assert built[0]["participant_address"] != contract["refund"].address
    assert entry.PARTICIPANT_ROLE != entry.REFUND_ROLE != entry.PREIMAGE_ROLE


def test_recover_REFUSES_when_the_rebuilt_contract_does_not_match_the_named_transaction(monkeypatch):
    """A wrong seed or a mistyped locktime rebuilds a DIFFERENT contract, and signing for it
    would produce a transaction the chain refuses with `-22` and no reason at all.

    That is the whole shape of today: four runs whose real cause sat in a daemon log. The P2SH
    is the fingerprint of the seed and the locktime together, so comparing it against the named
    transaction's outputs says which of the two is wrong BEFORE anything is signed.
    """
    entry = _entry()
    _with_seed(monkeypatch)
    console = Console(entry.TOTAL_STEPS, stream=io.StringIO())
    monkeypatch.setattr(entry.adaptor_steps, "_decoded",
                        lambda run, txid: {"vout": [{"n": 0, "value": "1.0",
                                                     "scriptPubKey": {"hex": "deadbeef"}}]})
    monkeypatch.setattr(entry.adaptor_steps, "current_height", lambda run: 999_999)
    monkeypatch.setattr(entry, "step_8_accepted",
                        lambda *a: (_ for _ in ()).throw(AssertionError("must not sign")))

    code = entry.recover(_SilentRun(), console, 3296338, "ab" * 32)

    assert code == 1
    printed = console.text() if hasattr(console, "text") else ""
    assert code == 1, printed


def test_recover_REFUSES_BEFORE_THE_LOCKTIME_rather_than_broadcasting_a_doomed_refund(monkeypatch):
    """CLTV would refuse it, and on this chain that refusal says `-22` and names nothing.

    Saying how many blocks are left, and how long that is, is the difference between "try
    again later" and an operator re-running every few minutes to see whether it took.
    """
    entry = _entry()
    _with_seed(monkeypatch)
    stream = io.StringIO()
    console = Console(entry.TOTAL_STEPS, stream=stream)
    built = {}
    monkeypatch.setattr(entry, "build_htlc_redeem_script", lambda **kw: built.update(kw) or b"\x51")
    wanted = entry.p2sh_script_for(b"\x51").hex()
    monkeypatch.setattr(entry.adaptor_steps, "_decoded",
                        lambda run, txid: {"vout": [{"n": 1, "value": "1.49",
                                                     "scriptPubKey": {"hex": wanted}}]})
    monkeypatch.setattr(entry.adaptor_steps, "current_height", lambda run: 3296300)
    monkeypatch.setattr(entry, "step_8_accepted",
                        lambda *a: (_ for _ in ()).throw(AssertionError("must not sign")))

    code = entry.recover(_SilentRun(), console, 3296338, "ab" * 32)

    assert code == 1
    printed = stream.getvalue()
    assert "TOO EARLY" in printed
    assert "38 more block(s)" in printed, printed
    assert "µfn" in printed, "and how long that is, in this repo's unit (rule 6)"
    assert "Nothing was signed or broadcast" in printed
