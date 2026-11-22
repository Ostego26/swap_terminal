"""`wait_or_mine_to(target)` must wait for THAT target, on a chain whose tip moves.

Role: test (behavioral, no chain)
Reads: swap_terminal/regtest/funding_steps.py
Writes: nothing
Can move funds: no
Mainnet-safe: yes -- the node is a stub and no socket is opened
Live-safe: yes

=========================================================================
THE DEFECT, CONFIRMED 2026-11-22
=========================================================================

`wait_or_mine_to()` read the tip, handed `_mine()` a RELATIVE count, and `_mine()` read the
tip a SECOND time to rebuild the absolute target from it:

    tip = current_height(run)            # first reading
    _mine(run, max(0, target - tip))     # _mine reads again: target = tip2 + count

On BTC/LTC regtest nothing moves between those readings. On Gridcoin testnet a block lands
roughly every GRC_SECONDS_PER_BLOCK seconds, so every block arriving in between pushed the
recomputed target one higher than the caller asked for.

THE PATH IS REACHABLE; THE OVERSHOOT WAS NEVER OBSERVED. Both callers of wait_or_mine_to
are in grc_htlc_verify.py, which builds its Run without ever calling step_3_capabilities --
so `run.mines` is unconditionally False there and the waiting branch is the only branch the
function has ever taken. That much is measured.

THIS FILE FIRST SAID "IT WAS LIVE, NOT LATENT" AND THAT WAS AN OVERSTATEMENT, corrected the
same day after an adversarial re-measurement was asked to refute it rather than confirm it.
The race window is not the 90s block interval it was reasoned from -- it is the microseconds
between two getblockcount calls separated by one run.say() and no sleep. And the single
recorded real Gridcoin run did NOT overshoot: docs/branch_coverage.md has step 8 executing
at tip 3296548 against a script locktime of 3296548, stopping exactly on it.

IT IS STILL WORTH A FIX AND A TEST, for a reason that does not depend on the odds. The
self-contradicting output is unconditional rather than racy -- the second reading is what
printed `(tip 101 -> 121)` one line under `to height 120` -- and two observations of a
moving value where one would do is a defect at any frequency. What changed is the severity
claim, not the diagnosis; inferring severity from a block target was rule 17's error running
in the opposite direction.

The cost is bounded either way: real waiting nobody asked for, and a progress line printing
two different targets one line apart (rule 14, on the path watched the longest). It does not
change what is sent, to whom, or how much.

WHY IT SURVIVED, AND WHY THIS FILE EXISTS. `_mine()` carries a comment stating this exact
shape was already found and fixed -- "THE TIP IS READ ONCE. It used to be read twice" --
and crediting `tests/test_adaptor_regtest_harness.py`. That file was deleted in 802e5b7
with adaptor_regtest_verify.py, so the comment has pointed at a test that could not have
been protecting anything ever since. The claim was true WITHIN _mine(); the identical shape
had moved ACROSS the function boundary, where that comment could not see it. A guard whose
test has been deleted is a guard, and this is its replacement.

HOW THE TARGET IS OBSERVED. `_wait_for_height` is stubbed to record the target it was asked
for and return at once. That separates the thing under test -- which absolute height gets
computed -- from the polling loop, which would otherwise need a sleep. The real
`wait_or_mine_to`, the real `_mine` and the real `_wait_for_blocks_to` all run.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "swap_terminal"))

from regtest import funding_steps


class _Run:
    """A Run whose tip ADVANCES by `drift` on every reading, like a live chain."""

    asset = "GRC"
    console = None

    def __init__(self, start: int, drift: int, mines: bool = False):
        self.height = start
        self.drift = drift
        self.mines = mines
        self.lines: list[str] = []
        self.generated: list[tuple[int, str]] = []
        self.readings: list[int] = []

    # -- the slice of Run these functions touch ------------------------------
    def say(self, *parts: object) -> None:
        self.lines.append(" ".join(str(part) for part in parts))

    def resolve_mining_address(self) -> str:
        return "a-harness-wallet-address"

    def node(self, wallet: bool = True):
        return self

    def call(self, method: str, *params: object) -> object:
        if method == "getblockcount":
            self.readings.append(self.height)
            observed = self.height
            # The chain moves on, exactly as Gridcoin testnet does between two RPCs.
            self.height += self.drift
            return observed
        if method == "generatetoaddress":
            self.generated.append((params[0], params[1]))
            return []
        raise AssertionError(f"unexpected call {method}")


def _capture_target(monkeypatch) -> list[int]:
    """Record every absolute height `_wait_for_height` is asked to reach."""
    asked: list[int] = []
    monkeypatch.setattr(
        funding_steps, "_wait_for_height",
        lambda run, target: asked.append(target) or target)
    return asked


def test_the_target_is_honored_exactly_while_the_tip_moves(monkeypatch):
    """THE DEFECT. With drift, the old code asked for target + drift."""
    asked = _capture_target(monkeypatch)
    run = _Run(start=100, drift=1)

    funding_steps.wait_or_mine_to(run, 150)

    assert asked == [150], f"asked to wait for {asked}, not the requested 150"


def test_the_overshoot_was_one_block_per_block_arriving(monkeypatch):
    """The measurement itself, as a table. Every row must now be zero overshoot.

    0, 1, 2, 3 and 5 blocks arriving between the two readings produced 0, 1, 2, 3 and 5
    blocks of overshoot before the fix. Keeping the drift values in the test is what makes
    a regression say HOW WRONG it went rather than merely that it did.
    """
    for drift in (0, 1, 2, 3, 5):
        asked = _capture_target(monkeypatch)
        run = _Run(start=3296381, drift=drift)

        funding_steps.wait_or_mine_to(run, 3296401)

        overshoot = asked[0] - 3296401
        assert overshoot == 0, f"drift={drift} produced an overshoot of {overshoot}"


def test_the_tip_is_read_ONCE_on_the_waiting_path(monkeypatch):
    """The root cause, pinned directly rather than only through its symptom.

    A second reading is what re-derived the target. Asserting the count means a future
    refactor that reintroduces the second read fails here even if it happens to compute
    the right number on a chain that is momentarily quiet.
    """
    _capture_target(monkeypatch)
    run = _Run(start=100, drift=1)

    funding_steps.wait_or_mine_to(run, 120)

    assert len(run.readings) == 1, f"the tip was read {len(run.readings)} times: {run.readings}"


def test_the_announced_target_matches_the_one_waited_for(monkeypatch):
    """Rule 14: the two progress lines must not disagree about where it is going.

    The old code printed `advancing from tip=100 to height 120` and then, one line later,
    `(tip 101 -> 121)`. Both were true of the step that printed them and the pair was
    incoherent.
    """
    _capture_target(monkeypatch)
    run = _Run(start=100, drift=1)

    funding_steps.wait_or_mine_to(run, 120)

    printed = "\n".join(run.lines)
    assert "to height 120" in printed, printed
    assert "-> 120)" in printed, f"the wait line names a different target:\n{printed}"
    assert "121" not in printed, f"a target nobody asked for appears in the output:\n{printed}"


def test_a_target_already_reached_SAYS_SO_instead_of_returning_silently(monkeypatch):
    """"already there" and "waited for nothing" are different outcomes (rule 14)."""
    asked = _capture_target(monkeypatch)
    run = _Run(start=500, drift=1)

    funding_steps.wait_or_mine_to(run, 400)

    assert asked == [], "it waited for a height already behind the tip"
    assert any("already at or past 400" in line for line in run.lines), run.lines


def test_the_mining_path_still_gets_a_COUNT(monkeypatch):
    """`generatetoaddress` takes a count, so that path must keep converting.

    The fix must not have turned a count into a height for the chains that can mine --
    passing an absolute height to generatetoaddress would mine thousands of blocks.
    """
    _capture_target(monkeypatch)
    run = _Run(start=100, drift=0, mines=True)

    funding_steps.wait_or_mine_to(run, 120)

    assert run.generated == [(20, "a-harness-wallet-address")], run.generated


def test__mine_keeps_its_relative_contract(monkeypatch):
    """_mine(count) is still relative, so its other nine callers are unaffected."""
    asked = _capture_target(monkeypatch)
    run = _Run(start=100, drift=0)

    funding_steps._mine(run, 7)

    assert asked == [107], asked


def test__mine_reads_the_tip_once_too(monkeypatch):
    """The original "THE TIP IS READ ONCE" claim, now actually covered by a test.

    The comment asserting this had credited a deleted file since 802e5b7.
    """
    _capture_target(monkeypatch)
    run = _Run(start=100, drift=1)

    funding_steps._mine(run, 7)

    assert len(run.readings) == 1, f"the tip was read {len(run.readings)} times"


def test__mine_with_a_nonpositive_count_says_so_and_asks_nothing(monkeypatch):
    """A zero-block request must not reach the chain at all."""
    asked = _capture_target(monkeypatch)
    run = _Run(start=100, drift=1)

    funding_steps._mine(run, 0)

    assert asked == [], asked
    assert run.readings == [], "it read the tip for a wait it was never going to do"
    assert any("no blocks needed" in line for line in run.lines), run.lines
