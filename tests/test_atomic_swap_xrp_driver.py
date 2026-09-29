"""What the XRP driver refuses, and what it normalizes, before it contacts anything.

Role: test
Reads: nothing. No adapter, no endpoint, no daemon.
Writes: nothing.
Can send orders: no
Live-safe: yes
"""

from __future__ import annotations

import argparse
import ast
import inspect
import pathlib
import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "swap_terminal"))

from step_console import Console

import atomic_swap_xrp as driver
from atomic_swap_xrp import (
    CAN_FUND_THE_HTLC,
    CHAIN_FIRST,
    LEGACY_CHAIN_FIRST,
    SCRIPT_CHAINS,
    XRP_FIRST,
    normalize_arguments,
    refuse_a_chain_this_driver_cannot_fund,
)


def a_console(capsys):
    capsys.readouterr()
    return Console(total_steps=10)


# ---------------------------------------------------------------------------
# The refusal. It exists because the alternative is a funded XRP escrow.
# ---------------------------------------------------------------------------


def test_a_chain_that_CAN_be_funded_is_not_refused(capsys):
    for chain in sorted(CAN_FUND_THE_HTLC):
        assert refuse_a_chain_this_driver_cannot_fund(a_console(capsys), chain) is False


def test_the_refusal_still_guards_a_chain_the_flag_offers_without_a_funding_path(capsys, monkeypatch):
    """IT REFUSES NOTHING TODAY, AND THAT IS WHY THE PERMISSION IS TAKEN AWAY HERE.

    THIS REPLACED A PARAMETRIZED TEST OVER `set(SCRIPT_CHAINS) - CAN_FUND_THE_HTLC`, which
    became an EMPTY parameter set the moment the two sets converged -- pytest reported it
    as one skip among ten passes, which is how a guard loses its only test without anything
    turning red. A test whose subject can vanish should not be parametrized over the thing
    that makes it vanish.

    On 2026-09-29 this guard refused BTC and LTC, because both runners funded through
    Gridcoin's createhtlc. They now fund through the chain clients, so CAN_FUND_THE_HTLC
    equals SCRIPT_CHAINS and no real chain is refused.

    DELETING THE GUARD WOULD BE THE MISTAKE. What it prevents is a chain arriving in
    SCRIPT_CHAINS -- which derives from SECONDS_PER_BLOCK, so adding an interval is enough
    -- with no funding path behind it. That chain would pass every check up to step 5,
    fund the XRP escrow, and fail at step 6. DOGE stands in for it here because a guard
    exercised only by the state it already permits is a guard nobody would notice breaking.
    """
    # A REAL CHAIN WITH THE PERMISSION TAKEN AWAY, not an invented ticker. The refusal
    # reports the chain's block interval, so a chain absent from SECONDS_PER_BLOCK raises
    # KeyError instead of refusing -- which is what the first version of this test did,
    # and it would have hidden the guard rather than exercised it.
    monkeypatch.setattr(driver, "CAN_FUND_THE_HTLC", frozenset({"GRC"}))
    console = a_console(capsys)
    assert refuse_a_chain_this_driver_cannot_fund(console, "BTC") is True
    printed = capsys.readouterr().out
    assert "EVERYTHING ELSE ABOUT BTC WORKS" in printed
    assert "step 5" in printed and "step 6" in printed, (
        "the refusal must say WHERE the one-sided state would arrive, not merely that it could"
    )


def test_EVERY_CHAIN_THE_FLAG_OFFERS_CAN_ACTUALLY_FUND_ITS_HTLC():
    """THE TWO SETS AGREE AGAIN, and this is what keeps them that way.

    They diverged for one commit on 2026-09-29: --chain offered btc and ltc while only GRC
    could be funded, deliberately, because a flag that hid them would have hidden the gap
    too. Moving both runners onto the chain clients closed it. What this pins now is that
    the sets cannot drift APART again silently -- SCRIPT_CHAINS derives from
    SECONDS_PER_BLOCK, so adding a block interval for a fourth chain is enough to offer it,
    and this fails until that chain can actually fund.
    """
    assert set(SCRIPT_CHAINS) == CAN_FUND_THE_HTLC, (
        "a chain the --chain flag offers has no funding path. It would pass every check up "
        "to step 5, fund the XRP escrow, and fail at step 6 with one leg live on a real "
        "chain -- which is the whole reason refuse_a_chain_this_driver_cannot_fund() exists"
    )


# ---------------------------------------------------------------------------
# Normalization: what was typed, versus what the tables are keyed by.
# ---------------------------------------------------------------------------
def test_a_lowercase_chain_becomes_the_key_every_table_uses():
    args = argparse.Namespace(chain="btc", direction=XRP_FIRST)
    normalize_arguments(args)
    assert args.chain == "BTC"


def test_the_LEGACY_direction_spelling_resolves_rather_than_becoming_a_second_name():
    """grc-first is accepted because it is in every recorded run of this driver and in
    the operator's history. It resolves HERE so nothing downstream ever sees two names
    for one direction -- which is what stops a later `== CHAIN_FIRST` from silently
    missing the legacy runs."""
    args = argparse.Namespace(chain="grc", direction=LEGACY_CHAIN_FIRST)
    normalize_arguments(args)
    assert args.direction == CHAIN_FIRST
    assert args.chain == "GRC"


def test_the_modern_direction_is_left_alone():
    args = argparse.Namespace(chain="grc", direction=XRP_FIRST)
    normalize_arguments(args)
    assert args.direction == XRP_FIRST


# ---------------------------------------------------------------------------
# The rate, which is easy to invert and expensive to get wrong.
# ---------------------------------------------------------------------------
def test_THE_RATE_SENTENCE_STATES_BOTH_DIRECTIONS(capsys):
    """A CORRECT LABEL IS ONLY HALF OF RULE 14, measured 2026-09-29.

    `--rate` is XRP PER UNIT of the script chain and the output always said so. The number
    an operator carries in their head is the other one, because it is what the recorded
    runs print: "1 XRP for 66.10250498 GRC". Passing 66.1 to a flag wanting 0.0151 is
    accepted, arithmetically fine, and off by a factor of about 4,300.

    That is exactly what happened: --rate 66.1 was suggested, passed, and produced a leg of
    0.01512859 GRC against one XRP. The label was right and nobody read it, because reading
    it required doing the division. Printing the inverse does the division.
    """
    amount, why = driver._rated_chain_amount(a_console(capsys), "66.1", "GRC")
    assert "1 XRP buys 0.01512859 GRC" in why
    assert "1 GRC costs 66.1 XRP" in why
    assert "you have passed its inverse" in why, (
        "the sentence must name the mistake, not merely make it visible"
    )
    assert amount == Decimal("0.01512859")


def test_the_rate_that_reproduces_the_recorded_run_is_the_INVERSE_of_the_recorded_figure():
    """docs/atomic_swap_runs_2026_09_27.md records 1 XRP for 66.10250498 GRC. The --rate
    that produces that is ~0.0151, not 66.1 -- which is the trap, stated as a number."""
    amount, _ = driver._rated_chain_amount(_QuietConsole(), "0.01512859", "GRC")
    assert Decimal(66) < amount < Decimal(67), (
        f"--rate 0.01512859 should buy about 66 GRC per XRP, got {amount}"
    )


class _QuietConsole:
    """A console that answers check() and says nothing. The rate path only prints on error."""

    def check(self, *_args, **_kwargs):
        return True

    def say(self, *_args, **_kwargs):
        return None


# ---------------------------------------------------------------------------
# The dry run, which is prose and which no test read until it was wrong.
# ---------------------------------------------------------------------------
def test_THE_DRY_RUN_DESCRIBES_THE_PATH_THAT_ACTUALLY_RUNS(capsys):
    """IT DESCRIBED A DEAD MECHANISM, and a dry run is where that costs the most.

    Until 2026-09-29 this printed "step 6 would fund the GRC leg: createhtlc
    receiver=<wallet address> sender=<wallet address>". Both halves were wrong the moment
    the runners moved onto the chain clients: there is no createhtlc call, and those
    addresses are where the claim and refund LAND rather than the HTLC's branches.

    Caught by an operator running it, not by the suite -- the dry run is prose and nothing
    read it. A stale comment is bad; a dry run describing the wrong mechanism is a stale
    comment at the exact moment it is load-bearing, because a dry run exists to be read
    BEFORE committing money.
    """
    console = a_console(capsys)
    leg = driver.ScriptLeg(chain="BTC", tip_height=100, timeout_height=244)
    driver.describe_the_dry_run(console, argparse.Namespace(direction=XRP_FIRST), "BTC", leg,
                                Decimal("0.5"), 844199449, "rA", "rB", "mClaim")
    printed = capsys.readouterr().out
    assert "createhtlc" not in printed, "the dry run names an RPC this driver no longer calls"
    assert "P2SH HTLC" in printed and "script_leg.py" in printed
    assert "minted" in printed, "the branches pay minted keys and the reader must know that"
    assert "shut before the step that publishes the secret" in printed


def test_the_dry_run_numbers_the_steps_BY_DIRECTION(capsys):
    """In xrp-first the XRP leg is step 6 and the script leg is 7; chain-first reverses it.

    Both printed "step 6" until 2026-09-29 -- which reads as two things happening at once,
    in a sequence whose ORDER is the security property. The whole reason the initiator's
    leg is funded first and takes the longer lock is that the order decides who is exposed.

    THE FIRST VERSION OF THIS TEST WAS WORTHLESS and is recorded rather than quietly
    replaced: it ended `assert ... or True`, which passes whatever the code does. A test
    that cannot fail is worse than no test, because it occupies the place where a real one
    would go and reports green while doing so.
    """
    leg = driver.ScriptLeg(chain="GRC", tip_height=1, timeout_height=2)

    driver.describe_the_dry_run(a_console(capsys), argparse.Namespace(direction=XRP_FIRST),
                                "GRC", leg, Decimal(1), 1, "rA", "rB", "mClaim")
    forward = capsys.readouterr().out
    assert "step 6 would fund the XRP leg" in forward
    assert "step 7 would fund the GRC leg" in forward

    driver.describe_the_dry_run(a_console(capsys), argparse.Namespace(direction=CHAIN_FIRST),
                                "GRC", leg, Decimal(1), 1, "rA", "rB", "mClaim")
    reverse = capsys.readouterr().out
    assert "step 7 would fund the XRP leg" in reverse
    assert "step 6 would fund the GRC leg" in reverse, (
        "chain-first funds the script leg FIRST; numbering it 7 would describe the other "
        "direction's exposure to an operator about to commit to this one"
    )


# ---------------------------------------------------------------------------
# The gap between the dry run and --run, which no test covered until it bit.
# ---------------------------------------------------------------------------
def test_MAIN_PASSES_EVERY_FIELD_SwapContext_REQUIRES():
    """THE DRY RUN CANNOT CATCH THIS, WHICH IS THE WHOLE REASON IT IS A TEST.

    Measured 2026-09-29, on the operator's first --run of this path:

        TypeError: SwapContext.__init__() missing 1 required positional argument: 'chain'

    `chain` was added to SwapContext when the operator-facing lines were made per-chain,
    and never added to the construction in main(). The dry run passed seven checks and
    returned BEFORE the context is built -- so a clean dry run said nothing at all about
    it, and was taken as evidence that --run was safe.

    WHERE IT LANDED WAS LUCK. The construction sits after the passphrase check and the key
    minting but before either leg is funded, so nothing moved. Two lines further down and
    it would have crashed with a live XRP escrow.

    CHECKED STRUCTURALLY RATHER THAN BY CONSTRUCTING ONE. A SwapContext needs a console, a
    submitter, an adapter, a client, four addresses and two secrets; a test that built one
    would be mostly fixture, and the fixture would drift from main()'s real call. Comparing
    the dataclass's fields against the keywords main() actually passes is the exact defect,
    with nothing in between to get stale.
    """
    source = Path(__file__).resolve().parent.parent / "atomic_swap_xrp.py"
    tree = ast.parse(source.read_text())

    context_class = next(n for n in tree.body
                         if isinstance(n, ast.ClassDef) and n.name == "SwapContext")
    required = [n.target.id for n in context_class.body if isinstance(n, ast.AnnAssign)]

    constructions = [n for n in ast.walk(tree)
                     if isinstance(n, ast.Call) and getattr(n.func, "id", "") == "SwapContext"]
    assert len(constructions) == 1, (
        f"expected exactly one SwapContext construction, found {len(constructions)} -- a second "
        f"one is a second place that can fall out of step with the fields"
    )
    passed = {keyword.arg for keyword in constructions[0].keywords}

    assert not [field for field in required if field not in passed], (
        f"SwapContext requires {[f for f in required if f not in passed]} and main() does not "
        f"pass it. This raises TypeError at --run time, AFTER the wallet has been opened and "
        f"keys minted, and the dry run returns before this line so it cannot catch it"
    )
    assert not [name for name in passed if name not in required], (
        f"main() passes {[n for n in passed if n not in required]}, which SwapContext does not "
        f"declare -- also a TypeError, in the other direction"
    )


# ---------------------------------------------------------------------------
# THE TWO-FEED PRICING, added 2026-09-29 after CoinGecko was measured returning
# 403 from the operator's host for every request.
# ---------------------------------------------------------------------------


class _PricingRecorder:
    """A Console that keeps its lines. Same shape as the other recorders here."""

    def __init__(self):
        self.lines = []

    def say(self, text):
        self.lines.append(text)

    def check(self, label, got, expected, ok):
        self.lines.append(f"CHECK {label} got={got} expected={expected} ok={ok}")
        return ok

    def text(self):
        return "\n".join(self.lines)


class _Quote:
    """A stand-in for a services.coinpaprika.PaprikaQuote."""

    def __init__(self, asset, cap, volume, derived=False):
        self.asset = asset
        self.market_cap_usd = cap
        self.volume_24h_usd = volume
        self.market_cap_is_derived = derived

    @property
    def turnover(self):
        if not self.market_cap_usd or not self.volume_24h_usd:
            return None
        return self.volume_24h_usd / self.market_cap_usd


def test_COINGECKO_IS_TRIED_when_coinpaprika_fails_and_the_line_names_which_answered():
    """A silently swapped source is a transcript nobody can read a day later.

    MUTATION: return the amount without the source sentence and this fails on
    "CoinGecko". Verified 2026-09-29.
    """
    recorder = _PricingRecorder()
    calls = []

    def _paprika_dies(chain):
        calls.append("paprika")
        raise RuntimeError("403 from the edge")

    def _gecko_answers(chain):
        calls.append("gecko")
        return driver.Decimal("45.05850281"), "services/pricing.py (CoinGecko): LTC $67.30", None

    original = (driver._paprika_priced, driver._coingecko_priced)
    try:
        driver._paprika_priced, driver._coingecko_priced = _paprika_dies, _gecko_answers
        amount, source = driver._priced_chain_amount(recorder, "LTC")
    finally:
        driver._paprika_priced, driver._coingecko_priced = original

    assert calls == ["paprika", "gecko"], f"the feeds were not tried in order: {calls}"
    assert amount is not None
    assert "CoinGecko" in source, f"the sentence does not name the feed that answered:\n{source}"


def test_BOTH_feeds_failing_REFUSES_and_prints_every_reason():
    """A swap is never priced 1:1 by default, and the refusal has to be diagnosable.

    One reason is not enough: an operator seeing only the second failure would
    conclude CoinGecko is the problem when CoinPaprika failed first for its own
    reason. Rule 14's "state what the number means" applied to a failure.

    MUTATION: keep only the last failure and this fails on the CoinPaprika text.
    Verified 2026-09-29.
    """
    recorder = _PricingRecorder()

    def _dies(message):
        def _attempt(chain):
            raise RuntimeError(message)
        return _attempt

    original = (driver._paprika_priced, driver._coingecko_priced)
    try:
        driver._paprika_priced = _dies("CoinPaprika id not found")
        driver._coingecko_priced = _dies("403 Client Error")
        amount, source = driver._priced_chain_amount(recorder, "LTC")
    finally:
        driver._paprika_priced, driver._coingecko_priced = original

    assert amount is None and source == ""
    out = recorder.text()
    assert "CoinPaprika id not found" in out and "403 Client Error" in out, (
        f"both reasons must survive to the screen:\n{out}"
    )
    assert "NOTHING WAS SUBMITTED" in out and "--rate" in out, out


def test_a_THIN_market_says_so_beside_the_rate():
    """GRC's $299 a day is the whole reason this line exists.

    MUTATION: drop the say_how_thin_this_market_is() call and this fails.
    Verified 2026-09-29.
    """
    recorder = _PricingRecorder()
    # The measured GRC figures: $299.28 of volume against a derived $7,665,794 cap.
    thin = _Quote("GRC", 7665794.0, 299.2754905121976, derived=True)

    original = driver._paprika_priced
    try:
        driver._paprika_priced = lambda chain: (driver.Decimal("0.011171"), "CoinPaprika: GRC", thin)
        driver._priced_chain_amount(recorder, "GRC")
    finally:
        driver._paprika_priced = original

    out = recorder.text()
    assert "THIN" in out, f"a market turning over 0.0039% a day did not read as thin:\n{out}"
    assert "DERIVED from supply" in out, (
        f"the cap was computed from supply and the line does not say so. A derived cap and a "
        f"reported one are the same number and different claims:\n{out}"
    )
    assert "299.28" in out, f"the volume itself is the fact an operator acts on:\n{out}"


def test_an_UNMEASURABLE_depth_says_so_rather_than_printing_nothing():
    """Rule 14: silence here would read as "this market is fine".

    That is the one thing it cannot mean -- no cap and no volume is no evidence,
    not good evidence.
    """
    recorder = _PricingRecorder()
    blind = _Quote("GRC", None, None)

    original = driver._paprika_priced
    try:
        driver._paprika_priced = lambda chain: (driver.Decimal("0.011171"), "CoinPaprika: GRC", blind)
        driver._priced_chain_amount(recorder, "GRC")
    finally:
        driver._paprika_priced = original

    out = recorder.text()
    assert "NOT MEASURABLE" in out, out
    assert "how much money set it is unknown" in out, out


def test_Config_RPC_IS_READ_IN_EXACTLY_ONE_FUNCTION():
    """THIS BUG HAPPENED THREE TIMES IN ONE SESSION, 2026-09-29.

    The conf fallback went into chain_balances.py. Then into this driver's
    adapter, because the driver said "(none)" about a daemon the reader had just
    found. Then into build_script_client, because a Litecoin daemon resolved from
    its conf produced a working adapter and a script client that raised

        ValueError: LTC_RPC_PORT is not set, so no LTC client can be built

    at step 5b -- AFTER the run had priced both legs and computed both timelocks,
    on a --run that was going to move coins.

    Each time, the next consumer was invisible: nothing points from one reader of
    Config.RPC to the others, which is exactly rule 8's complaint and why it says
    to grep for the rule rather than trust you found every copy. A count is what
    makes the next one visible without anybody remembering to look.

    ONE function may read it. Everything else takes the settings as an argument,
    so the adapter and the script client cannot end up on two different daemons.

    MUTATION: put `Config.RPC[chain]` back into prepare_the_script_leg() and this
    fails with two reading functions. Verified 2026-09-29.
    """
    source = pathlib.Path(driver.__file__).read_text()
    tree = ast.parse(source)

    readers = set()
    for function in ast.walk(tree):
        if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for node in ast.walk(function):
            # Config.RPC, however it is then indexed or .get()-ed.
            if (isinstance(node, ast.Attribute) and node.attr == "RPC"
                    and isinstance(node.value, ast.Name) and node.value.id == "Config"):
                readers.add(function.name)
    assert readers == {"resolve_the_script_chain_adapter"}, (
        f"Config.RPC is read in {sorted(readers)}. It may be read in exactly one function, which "
        f"returns the settings it used; every other consumer takes them as an argument. Two readers "
        f"is how a chain resolved from its conf gets an adapter that works and a client that does "
        f"not, and the second failure lands after the run has already priced the legs"
    )


def test_the_resolver_hands_back_the_settings_it_actually_used():
    """The adapter alone is not enough: the script client needs the same mapping.

    Returning only the adapter is what made the third instance possible -- the
    caller had nothing to pass on, so the next consumer went back to the global.
    """
    signature = inspect.signature(driver.prepare_the_script_leg)
    assert "chain_rpc" in signature.parameters, (
        "prepare_the_script_leg() does not take the resolved settings, so it must be finding them "
        "somewhere else -- which is the global this whole test pair exists to remove"
    )
