"""A rate, not a position -- and the cause it names must be measured, not assumed.

Role: behavioral test (read-only)
Reads: chains.daemon_network.sync_bottleneck, chain_sync_rate's functions
Writes: nothing
Can send orders: no
Live-safe: yes. No socket, no daemon, no sleeping -- every test seeds its readings.

WHY THIS EXISTS, and it is a correction to something I said rather than a feature.

A diagnostic handed to the operator on 2026-10-10 printed

    peers now  1   <- want 8+; one peer is why the sync is slow

and "one peer is why" was an inference in the register of a measurement, which is
rule 17's exact failure. They pasted it back twice and acted on it. The numbers,
measured afterwards on LTC testnet at height ~3.1M of 4.9M:

    the conf             no connect=, no maxconnections=, no peer settings at all
    the command line     three flags total, none of them peer-related
    known addresses      8219, from getnodeaddresses
    blocks validated     2153 in 30.0s   (71.7/s)
    bytes received       1,646,379       (0.055 MB/s)
    average block        0.8 kB on the wire

0.055 MB/s is a factor of 36 under the 2 MB/s floor. The ONE peer was feeding that
node faster than it could validate; more peers would have changed little. So the
verdict is computed from a stated threshold now, and these tests drive both sides
of it with seeded numbers rather than trusting the sentence.

NO TEST HERE SLEEPS. sync_bottleneck() takes (elapsed, blocks, headers, bytes)
quadruples and performs no I/O, which is the property the C901 extraction was for:
collect_samples() owns the daemon and the clock, report() owns the rendering, and
only the first needs either.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest
from chains.daemon_network import (
    BOTTLENECK_VERDICTS,
    MINIMUM_SAMPLES,
    MINIMUM_WINDOW_SECONDS,
    NETWORK_BOUND_FLOOR_BYTES_PER_SECOND,
    SYNC_NETWORK_BOUND,
    SYNC_NOT_ADVANCING,
    SYNC_RATE_NOT_ESTABLISHED,
    SYNC_VALIDATION_BOUND,
    rate_refusal,
    sync_bottleneck,
)

import chain_sync_rate as csr

#: THE OPERATOR'S REAL READINGS, verbatim from their terminal 2026-10-10. Using the
#: measured pair rather than a plausible one is the difference between testing this
#: function and testing my idea of a syncing node -- and the figure I would have got
#: wrong is the byte count, which is three orders of magnitude smaller than the
#: "obviously the network is the bottleneck" intuition suggests.
_REAL = [
    (0.0, 3105024, 4912225, 4_169_554_759),
    (30.0, 3107177, 4912225, 4_171_201_138),
]


# ---------------------------------------------------------------------------
# THE VERDICT. Both sides of the threshold, from seeded numbers.
# ---------------------------------------------------------------------------

def test_the_OPERATORS_OWN_READINGS_come_back_VALIDATION_BOUND():
    """The measurement that refuted me, as a test.

    Every figure is asserted, not just the verdict: a function that said
    "validation-bound" while computing the rate wrong would be right by accident.
    """
    out = sync_bottleneck(_REAL)
    assert out["state"] == SYNC_VALIDATION_BOUND
    assert out["blocks_per_second"] == pytest.approx(71.8, abs=0.1)
    assert out["bytes_per_second"] == pytest.approx(54_879.3, rel=0.001)
    assert out["bytes_per_block"] == pytest.approx(764.7, rel=0.001)
    assert out["behind"] == 1_805_048
    assert out["eta_seconds"] == pytest.approx(25_152, rel=0.001)
    assert "NOT the ceiling" in out["why"]
    assert "dbcache" in out["why"], "and it names the lever that would actually move it"


def test_a_FAST_LINK_comes_back_NETWORK_BOUND_so_the_threshold_cuts_both_ways():
    """The other side. A test that only ever saw validation-bound would pass on a
    function that returned that string unconditionally."""
    fast = [(0.0, 0, 100_000, 0), (1.0, 10, 100_000, 9_000_000)]
    out = sync_bottleneck(fast)
    assert out["state"] == SYNC_NETWORK_BOUND
    assert "more peers SHOULD help" in out["why"]


def test_the_THRESHOLD_IS_THE_HINGE_and_a_byte_either_side_flips_it():
    """Asserted AT the boundary, because that is where an off-by-one in a comparison
    lives. `>=` means exactly the floor is network-bound."""
    floor = NETWORK_BOUND_FLOOR_BYTES_PER_SECOND
    at = [(0.0, 0, 9, 0), (1.0, 1, 9, floor)]
    under = [(0.0, 0, 9, 0), (1.0, 1, 9, floor - 1)]
    assert sync_bottleneck(at)["state"] == SYNC_NETWORK_BOUND
    assert sync_bottleneck(under)["state"] == SYNC_VALIDATION_BOUND


def test_the_FLOOR_IS_A_PARAMETER_so_a_caller_can_state_its_own_link():
    """It is an argument rather than a literal precisely so this test exists: driving
    both branches must not require pretending to have a gigabit link."""
    readings = [(0.0, 0, 9, 0), (1.0, 1, 9, 1000)]
    assert sync_bottleneck(readings, floor_bps=100)["state"] == SYNC_NETWORK_BOUND
    assert sync_bottleneck(readings, floor_bps=10_000)["state"] == SYNC_VALIDATION_BOUND


def test_a_STALLED_sync_is_its_own_verdict_and_not_merely_slow():
    """Zero blocks gained is not a very small rate. It is a different problem, and
    the remedy for it is not the remedy for either bound state -- so it must not
    collapse into one of them, and the sentence says more peers will not help."""
    stalled = [(0.0, 500, 9000, 0), (60.0, 500, 9000, 50_000_000)]
    out = sync_bottleneck(stalled)
    assert out["state"] == SYNC_NOT_ADVANCING
    assert "NOT ADVANCING" in out["why"]
    assert out["eta_seconds"] is None, "no rate, so no ETA -- not an infinity and not a zero"
    assert out["bytes_per_block"] is None, "and no per-block figure to divide by zero for"


def test_a_sync_going_BACKWARDS_is_also_not_advancing():
    """A reorg or a reindex can lower `blocks`. A negative rate must not produce a
    negative ETA, which would print as a finish time in the past."""
    out = sync_bottleneck([(0.0, 900, 9000, 0), (10.0, 800, 9000, 10)])
    assert out["state"] == SYNC_NOT_ADVANCING
    assert out["eta_seconds"] is None


# ---------------------------------------------------------------------------
# THE REFUSALS. Rule 14: a figure that was not measured is None, never 0.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("bad", [
    None, "not a sequence", 7, [], [(0.0, 1, 2, 3)],
])
def test_ANYTHING_THAT_IS_NOT_TWO_SAMPLES_refuses_rather_than_guessing(bad):
    out = sync_bottleneck(bad)
    assert out["state"] == SYNC_RATE_NOT_ESTABLISHED
    assert out["why"], "and it says why, rather than returning a bare verdict"


def test_a_MALFORMED_SAMPLE_says_so_instead_of_raising():
    """A caller mid-outage can hand this a short tuple. It is a diagnostic; dying on
    a bad row would lose the readings that DID arrive."""
    out = sync_bottleneck([(0.0, 1), (1.0, 2)])
    assert out["state"] == SYNC_RATE_NOT_ESTABLISHED
    assert "quadruple" in out["why"]


def test_ZERO_ELAPSED_refuses_and_names_the_clock():
    """Two samples at the same instant cannot make a rate, and a wall clock that
    stepped backwards reads exactly like this -- which is why the message says so."""
    out = sync_bottleneck([(5.0, 1, 2, 3), (5.0, 99, 2, 3)])
    assert out["state"] == SYNC_RATE_NOT_ESTABLISHED
    assert "monotonic" in out["why"]


def test_EVERY_REFUSAL_returns_None_for_every_figure_and_never_a_zero():
    """Rule 14, as a sweep. A 0.0 rate means "measured, and it is zero"; None means
    "not measured". Those send a reader to two different places, and a renderer that
    formatted None as 0.0 would erase the distinction -- so the contract is here."""
    for bad in (None, [], [(0.0, 1, 2, 3)], [(1.0, 1), (2.0, 2)]):
        out = sync_bottleneck(bad)
        for key in ("blocks_per_second", "bytes_per_second", "bytes_per_block",
                    "behind", "eta_seconds"):
            assert out[key] is None, f"{key} must be None on a refusal, got {out[key]!r}"


def test_EVERY_verdict_this_can_return_is_in_BOTTLENECK_VERDICTS():
    """The tuple must be total over what the function produces, so a renderer can
    assert it has a sentence for each -- the gap stack_authority's bech32 renderer
    shipped on 2026-10-09, where a missing branch printed an invented sentence."""
    produced = {
        sync_bottleneck(_REAL)["state"],
        sync_bottleneck([(0.0, 0, 9, 0), (1.0, 1, 9, 9_000_000)])["state"],
        sync_bottleneck([(0.0, 5, 9, 0), (1.0, 5, 9, 1)])["state"],
        sync_bottleneck(None)["state"],
    }
    assert produced == set(BOTTLENECK_VERDICTS), (
        f"every verdict must be reachable and all four distinct; produced {sorted(produced)}"
    )
    assert MINIMUM_SAMPLES == 2, "two readings are a rate; one is a position"


# ---------------------------------------------------------------------------
# THE ENTRY POINT. No daemon, no sleeping.
# ---------------------------------------------------------------------------

@dataclass
class _Node:
    """A daemon that answers the two reads, advancing by a fixed step each sample.

    A DATACLASS RATHER THAN AN `__init__`, and not for brevity: the explicit version
    took six keyword arguments and ruff PLR0913 flagged it at 6 > 5. Rule 12's answer
    to that finding is to change the shape, never to `noqa` it -- and the defaults ARE
    the operator's measured readings, so a test that wants the real node writes
    `_Node()` and a test that wants a stalled one names only what differs.
    """

    url: str = "http://127.0.0.1:19443"
    blocks: int = 3_105_024
    headers: int = 4_912_225
    recv: int = 4_169_554_759
    block_step: int = 2153
    byte_step: int = 1_646_379
    fail_on: tuple[int, ...] = ()
    calls: int = 0

    def call(self, method):
        if method == "getnettotals":
            return {"totalbytesrecv": self.recv}
        self.calls += 1
        if self.calls in self.fail_on:
            raise OSError("connection reset")
        if self.calls > 1:
            self.blocks += self.block_step
            self.recv += self.byte_step
        return {"blocks": self.blocks, "headers": self.headers}


def test_the_ENTRY_POINT_runs_END_TO_END_and_names_the_bottleneck(monkeypatch, capsys):
    """main(), with a daemon stub and no gap -- the test that was missing from
    testnet_wallets.py until a TypeError reached the operator's screen."""
    monkeypatch.setattr(csr, "build_adapters", lambda _rpc: {"LTC": _Node()})
    code = csr.main(["--chain", "LTC", "--samples", "2", "--gap-seconds", "1"])
    out = capsys.readouterr().out
    assert code == 0
    assert "VALIDATION-BOUND" in out
    assert "ETA at this rate" in out
    assert "AN EXTRAPOLATION, NOT A FINISH TIME" in out, (
        "the ETA must arrive labeled -- it fell 79.8 -> 71.7 blocks/s in twenty minutes"
    )


def test_an_UNREADABLE_SAMPLE_is_ANNOUNCED_and_the_rest_still_report(monkeypatch, capsys):
    """A sample that vanished silently would shorten the window without saying so."""
    monkeypatch.setattr(csr, "build_adapters",
                        lambda _rpc: {"LTC": _Node(fail_on=(2,))})
    code = csr.main(["--chain", "LTC", "--samples", "3", "--gap-seconds", "1"])
    out = capsys.readouterr().out
    assert code == 0
    assert "UNREADABLE" in out and "connection reset" in out
    assert "OVER" in out, "the two readings that DID arrive still produced a rate"


def test_a_daemon_that_answers_NOTHING_says_so_with_its_denominator(monkeypatch, capsys):
    """Rule 3: "answered none of 3 attempts" beats "no readings" -- the second does
    not say whether anything was asked."""
    monkeypatch.setattr(csr, "build_adapters",
                        lambda _rpc: {"LTC": _Node(fail_on=(1, 2, 3))})
    code = csr.main(["--chain", "LTC", "--samples", "3", "--gap-seconds", "0"])
    out = capsys.readouterr().out
    assert code == 1
    assert "answered none of 3 attempts" in out


def test_an_UNCONFIGURED_chain_REFUSES_and_names_the_variables(monkeypatch, capsys):
    monkeypatch.setattr(csr, "build_adapters", lambda _rpc: {})
    code = csr.main(["--chain", "LTC", "--samples", "2", "--gap-seconds", "0"])
    out = capsys.readouterr().out
    assert code == 1
    assert "REFUSED" in out and "LTC_RPC" in out
    assert "no conf fallback" in out, "and says WHY it will not fall back (SERVICE SIDE)"


def test_a_chain_this_CANNOT_measure_is_refused_rather_than_shown_as_empty(capsys):
    """XRP, SOL and ICP have no getblockchaininfo/getnettotals pair. A row printing
    "(none)" for them would imply the question had been asked and answered."""
    code = csr.main(["--chain", "XRP"])
    out = capsys.readouterr().out
    assert code == 2
    assert "not measurable here" in out
    assert "BTC, LTC" in out, "and it names what it does cover"


def test_an_ALREADY_SYNCED_node_is_NOT_reported_as_a_STALLED_SYNC(monkeypatch, capsys):
    """THE FALSE ALARM THIS TEST FOUND, 2026-10-10.

    A caught-up node does not advance during a short window either -- LTC blocks
    arrive every ~2.5 minutes -- so the first version of this returned "THE SYNC IS
    NOT ADVANCING" for a node with nothing whatever wrong with it. That is the
    confident wrong cause the whole verdict exists to stop, pointed at a healthy
    daemon, and it would have sent the operator looking for a stall that was not
    there.

    `behind` separates the two and the sentence now says which. Asserted in both
    directions, because only the negative half has teeth: a test that merely looked
    for "caught-up" would pass on a function that said it unconditionally.
    """
    monkeypatch.setattr(csr, "build_adapters", lambda _rpc:
                        {"BTC": _Node(blocks=900_000, headers=900_000, block_step=0,
                                      byte_step=5_000)})
    code = csr.main(["--chain", "BTC", "--samples", "2", "--gap-seconds", "1"])
    out = capsys.readouterr().out
    assert code == 0
    assert "caught-up node idling between blocks" in out
    assert "nothing to wait for and nothing to fix" in out
    assert "THE SYNC IS NOT ADVANCING" not in out, (
        "a healthy node must not be reported as a stalled sync -- that is the false alarm"
    )
    assert "ETA at this rate" not in out, "and there is nothing to extrapolate to"


def test_a_GENUINELY_STALLED_sync_still_says_so(monkeypatch, capsys):
    """The other side of that fix. Same zero rate, 400k blocks behind: a real stall."""
    monkeypatch.setattr(csr, "build_adapters", lambda _rpc:
                        {"BTC": _Node(blocks=500_000, headers=900_000, block_step=0,
                                      byte_step=5_000)})
    code = csr.main(["--chain", "BTC", "--samples", "2", "--gap-seconds", "1"])
    out = capsys.readouterr().out
    assert code == 0
    assert "THE SYNC IS NOT ADVANCING" in out
    assert "400000 still to go" in out
    assert "caught-up node idling" not in out


def test_a_WINDOW_TOO_SHORT_refuses_rather_than_naming_a_wrong_cause(monkeypatch, capsys):
    """THE OTHER DEFECT A TEST FOUND, and it is this module's own failure mode.

    With a zero gap the window becomes one RPC round-trip, and 1.6 MB of
    totalbytesrecv over 0.0002s computes as 16 GB/s -- so a node that is firmly
    validation-bound reported NETWORK-BOUND, confidently, with every figure
    arithmetically correct. An operator passing --gap-seconds 0 to "check quickly"
    would get precisely the wrong answer this file exists to correct.

    It refuses now instead of scaling, because the arithmetic SUCCEEDS and that is
    what makes it dangerous.
    """
    monkeypatch.setattr(csr, "build_adapters", lambda _rpc: {"LTC": _Node()})
    code = csr.main(["--chain", "LTC", "--samples", "2", "--gap-seconds", "0"])
    out = capsys.readouterr().out
    assert code == 0
    assert "RATE NOT ESTABLISHED" in out
    assert "one RPC round-trip" in out
    assert "NETWORK-BOUND" not in out, "which is what it used to say here"
    assert "VALIDATION-BOUND" not in out


def test_the_PREAMBLE_states_the_WALL_TIME_before_the_wait_begins(monkeypatch, capsys):
    """Rule 14: announce before, not only after. A minute of silence is what makes an
    operator reach for Ctrl-C, and on this system that can kill a live cycle."""
    monkeypatch.setattr(csr, "build_adapters", lambda _rpc: {"LTC": _Node()})
    csr.main(["--chain", "LTC", "--samples", "5", "--gap-seconds", "15"])
    out = capsys.readouterr().out
    head = out.split("=== LTC")[0]
    assert "wall time" in head and "samples" in head
    assert "49.6µfn (60.0s)" in head, "the real figure, in µfn with seconds (rule 6)"
    assert "every call is a READ" in head


def test_report_NEEDS_NO_DAEMON_which_is_the_point_of_the_extraction(capsys):
    """report() takes readings and nothing else. ruff C901 flagged the combined
    version at 11, and rule 12's answer is to extract the decision rather than raise
    the ceiling -- so this calls it with the operator's real numbers, no stub at all.
    """
    code = csr.report("LTC", _REAL, attempted=2)
    out = capsys.readouterr().out
    assert code == 0
    assert "VALIDATION-BOUND" in out
    assert "0.055 MB/s" in out
    assert "0.8 kB on the wire" in out


def test_rate_refusal_IS_CALLABLE_ALONE_which_is_why_it_was_extracted():
    """ruff PLR0911 flagged the combined function at 7 returns > 6, and rule 12's
    answer is to extract the decision. The win is not the lint code: "a 0.3s window
    is refused" is now one call rather than a dict lookup through a verdict.

    It returns a SENTENCE and not a bool, because the reasons are not
    interchangeable -- a backwards clock and a too-short window send an operator to
    two different places.
    """
    assert rate_refusal(_REAL) == "", "a usable 30s window refuses nothing"
    short = rate_refusal([(0.0, 1, 2, 3), (0.3, 9, 2, 3)])
    assert "under the 1.0s minimum" in short
    assert rate_refusal(None), "and it covers the shapes too, not just the window"
    assert "quadruple" in rate_refusal([(0.0, 1), (1.0, 2)])


def test_the_WINDOW_GUARD_is_a_parameter_so_a_test_need_not_sleep_through_it():
    """Same reason floor_bps is one. A guard that could only be exercised by waiting
    a real second is a guard whose branches go untested."""
    tiny = [(0.0, 0, 9, 0), (0.01, 5, 9, 100)]
    assert sync_bottleneck(tiny)["state"] == SYNC_RATE_NOT_ESTABLISHED
    assert sync_bottleneck(tiny, minimum_window=0.001)["state"] in (
        SYNC_NETWORK_BOUND, SYNC_VALIDATION_BOUND)
    assert MINIMUM_WINDOW_SECONDS == 1.0, (
        "totalbytesrecv moves in bursts as blocks arrive; under a second the byte rate "
        "says more about where the samples fell than about the throughput"
    )


def test_a_CAUGHT_UP_node_and_a_STALLED_one_get_DIFFERENT_sentences():
    """The leaf-level half of the false alarm above. Same zero rate, opposite meaning."""
    caught_up = sync_bottleneck([(0.0, 900_000, 900_000, 0), (10.0, 900_000, 900_000, 5_000)])
    stalled = sync_bottleneck([(0.0, 500_000, 900_000, 0), (10.0, 500_000, 900_000, 5_000)])
    assert caught_up["state"] == stalled["state"] == SYNC_NOT_ADVANCING, (
        "one state, because neither has a rate to extrapolate and a fifth verdict would "
        "make every renderer handle a case that needs no action"
    )
    assert "nothing to fix" in caught_up["why"]
    assert "NOT ADVANCING" in stalled["why"] and "400000 still to go" in stalled["why"]
    assert caught_up["why"] != stalled["why"]
