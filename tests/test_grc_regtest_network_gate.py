"""The GRC network gate must let REGTEST run and must still refuse MAINNET.

Role: test (behavioral, no chain)
Reads: swap_terminal/regtest/funding_steps.py and chains/gridcoin_genesis.py
Writes: nothing
Can move funds: no
Mainnet-safe: yes -- every node here is a stub and no socket is opened
Live-safe: yes

=========================================================================
THE DEFECT THESE TESTS WERE WRITTEN AGAINST
=========================================================================

Measured 2026-11-22 against the exact Gridcoin source docker/grc-desk.Dockerfile
pins (ref `5.5.1.7-testnet`, commit 30a89f2ab0bb4f7134ca7a9c74e233e76130ebd8):

    src/chainparams.h:129-132   inline bool OnTestnet() {
                                    return Params().NetworkIDString() == CBaseChainParams::TESTNET; }
    src/rpc/blockchain.cpp:4809 res.pushKV("testnet", OnTestnet())

So `testnet` is False on MAINNET and False on REGTEST. funding_steps.MAINNET_STATEMENTS
mapped `False` to a POSITIVE STATEMENT OF MAINNET for all three TEST_NETWORK_SIGNALS, and
assert_test_network() raises on any mainnet statement with no override. A Gridcoin regtest
daemon therefore answered False three times, was classified as mainnet, and the run was
refused -- on the chain whose coins come out of a 1,000,000 GRC genesis premine and are
worth nothing.

THE TEST THAT MATTERS IS test_regtest_genesis_RUNS. It fails without the fix.

=========================================================================
WHAT IS DELIBERATELY STILL REFUSED, which is the other half
=========================================================================

A gate that stopped refusing things would be a worse gate, not a fixed one. These pin that
the fix is NARROW: the `testnet == False` mapping is suppressed only when the genesis hash
has POSITIVELY identified regtest, and never because the genesis was unreachable,
unrecognized, or absent. Four of the seven tests below assert a refusal.

NO VALUE IS SPELLED TWICE. The three genesis hashes come from inverting
gridcoin_genesis.GRIDCOIN_GENESIS rather than being retyped here, so a hash edited in that
table is caught by these tests instead of being shadowed by a stale copy (rule 8). The
inversion is asserted to still have three entries, because a dict inverted into a dict
loses collisions silently and a lost network would make a test vacuous rather than failing.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "swap_terminal"))

from chains import gridcoin_genesis
from chains.base import RPCError
from regtest import funding_steps
from regtest.console import FAIL, OK, SKIP
from regtest.daemons import RegtestSetupError

#: genesis hash per Gridcoin network name, derived from the module's own table.
GENESIS = {network: digest for digest, network in gridcoin_genesis.GRIDCOIN_GENESIS.items()}

#: Every signal in the table answering `testnet=False` -- what a REGTEST node really says,
#: and also what a MAINNET node says. That collision is the whole subject of this file.
ALL_FALSE = {method: {key: False} for method, key, _accepted in funding_steps.TEST_NETWORK_SIGNALS}

#: Every signal answering `testnet=True` -- what a Gridcoin TESTNET node says.
ALL_TRUE = {method: {key: True} for method, key, _accepted in funding_steps.TEST_NETWORK_SIGNALS}


def test_the_inverted_table_did_not_lose_a_network():
    """Guard the fixture itself: three hashes in, three networks out.

    Without this, a table edited down to two entries would make every test below pass
    against a GENESIS dict missing the network it claims to exercise -- a vacuous green,
    which is worse than a red.
    """
    assert len(gridcoin_genesis.GRIDCOIN_GENESIS) == 3, gridcoin_genesis.GRIDCOIN_GENESIS
    assert sorted(GENESIS) == ["main", "regtest", "test"], sorted(GENESIS)


class _Node:
    """A stub daemon that answers `getblockhash 0` and the three boolean signals."""

    def __init__(self, genesis: object, booleans: dict, genesis_error: str = ""):
        self.genesis = genesis
        self.booleans = booleans
        self.genesis_error = genesis_error
        self.calls: list[str] = []

    def call(self, method: str, *params: object) -> object:
        self.calls.append(method)
        if method == "getblockhash":
            assert params == (0,), f"the gate must ask for block 0, asked for {params}"
            if self.genesis_error:
                raise RPCError(self.genesis_error)
            return self.genesis
        if method in self.booleans:
            return self.booleans[method]
        raise RPCError(f"Method not found: {method}")


class _Config:
    port = 35715
    base_url = "http://127.0.0.1:35715"


class _Run:
    """The slice of the Run protocol assert_test_network() touches, recording verdicts."""

    asset = "GRC"
    config = _Config()
    #: daemons.assert_regtest() takes the console positionally for the BTC/LTC branch.
    #: None is enough because that branch is stubbed out wherever this class reaches it.
    console = None

    def __init__(self, node: _Node):
        self._node = node
        self.checks: list[tuple[str, object, str]] = []
        self.lines: list[str] = []

    def say(self, *parts: object) -> None:
        self.lines.append(" ".join(str(part) for part in parts))

    def step(self, *_parts: object) -> None:
        pass

    def check(self, label: str, got: object, _expected: object, verdict: str) -> None:
        self.checks.append((label, got, verdict))

    def node(self, wallet: bool = True) -> _Node:
        return self._node


def _run_gate(genesis: object, booleans: dict, genesis_error: str = "") -> _Run:
    """Drive the REAL assert_test_network() and return the run it reported through."""
    run = _Run(_Node(genesis, booleans, genesis_error))
    funding_steps.assert_test_network(run)
    return run


def _verdict(run: _Run, label: str) -> str:
    matching = [verdict for recorded, _got, verdict in run.checks if recorded == label]
    assert matching, f"nothing reported a check labeled {label!r}; got {run.checks}"
    return matching[0]


def test_regtest_genesis_RUNS():
    """THE DEFECT. A Gridcoin regtest node answers testnet=False three times and must RUN.

    Before the fix this raised RegtestSetupError('... POSITIVELY STATED MAINNET ...'), because
    all three False answers landed in MAINNET_STATEMENTS. The genesis hash is what tells the
    two networks apart, so it is what has to be consulted first.
    """
    run = _run_gate(GENESIS["regtest"], ALL_FALSE)

    assert _verdict(run, "getblockhash(0)") == OK
    assert _verdict(run, "test network stated by the daemon") == OK
    # The booleans contributed NOTHING -- they are consistent with regtest and establish
    # nothing, so they are SKIP rather than OK. If they were OK a reader could count them
    # toward the positive statement the gate requires, which they do not supply.
    for method, key, _accepted in funding_steps.TEST_NETWORK_SIGNALS:
        assert _verdict(run, f"{method}.{key}") == SKIP, run.checks
    # And the one positive is the genesis, named as such in the run's own output.
    stated = [got for label, got, _v in run.checks if label == "test network stated by the daemon"]
    assert "getblockhash(0)" in str(stated[0]), stated


def test_regtest_is_announced_before_the_booleans_are_read():
    """The run must SAY why False stopped meaning mainnet, or the output contradicts itself.

    Rule 14: a pasted run showing three SKIPs against signals whose documented mainnet value
    is False is unreadable unless something on screen says the genesis already settled it.
    """
    run = _run_gate(GENESIS["regtest"], ALL_FALSE)
    assert any("REGTEST" in line and "testnet=False" in line for line in run.lines), run.lines


def test_mainnet_genesis_REFUSES_even_though_the_booleans_look_identical():
    """The byte-for-byte same boolean answers, a different genesis, opposite verdict.

    This is the pair that proves the genesis is doing the work: ALL_FALSE is passed to both
    this test and test_regtest_genesis_RUNS, and only the hash differs.
    """
    with pytest.raises(RegtestSetupError) as caught:
        _run_gate(GENESIS["main"], ALL_FALSE)

    message = str(caught.value)
    assert "POSITIVELY STATED MAINNET" in message, message
    assert "getblockhash(0)" in message, "the refusal must name the genesis as the evidence"
    assert "main" in message, message


def test_mainnet_genesis_REFUSES_even_when_a_boolean_says_testnet():
    """A daemon contradicting itself is refused rather than resolved, and the genesis wins.

    A proxied or half-migrated node can answer testnet=True while serving mainnet's chain.
    The hash cannot be made to agree with a lie, so it must be able to refuse alone.
    """
    with pytest.raises(RegtestSetupError) as caught:
        _run_gate(GENESIS["main"], ALL_TRUE)

    message = str(caught.value)
    assert "POSITIVELY STATED MAINNET" in message, message
    assert "contradicts itself" in message, message


def test_an_UNRECOGNIZED_genesis_suppresses_nothing_and_still_REFUSES():
    """The fix must be narrow: only a POSITIVE regtest identification changes the mapping.

    If an unknown hash suppressed the `testnet == False` mapping, then any daemon this table
    does not recognize -- including a future mainnet whose genesis nobody added here -- would
    sail through. Absence of evidence stays mainnet.
    """
    with pytest.raises(RegtestSetupError) as caught:
        _run_gate("de" * 32, ALL_FALSE)

    message = str(caught.value)
    assert "POSITIVELY STATED MAINNET" in message, message


def test_an_unrecognized_genesis_is_ECHOED_so_the_operator_has_the_value():
    """No Gridcoin daemon has ever answered this call here, so the first one may surprise us.

    Every hash in the table was READ from Gridcoin's source, and Gridcoin spells its block
    hash `GetHash(true)` at the assert sites -- so whether `getblockhash 0` returns that same
    digest is unmeasured. When it does not, the refusal has to carry the value to add rather
    than just the news that there was one (rule 14).
    """
    run = _Run(_Node("ab" * 32, ALL_TRUE))
    funding_steps.assert_test_network(run)

    reported = [got for label, got, _v in run.checks if label == "getblockhash(0)"]
    assert reported, run.checks
    assert "abababababababab" in str(reported[0]), reported
    assert "NOT a Gridcoin network this table knows" in str(reported[0]), reported
    assert _verdict(run, "getblockhash(0)") == SKIP


def test_a_failing_getblockhash_leaves_the_testnet_path_working():
    """An older daemon, or one that refuses the call, must not break what already worked.

    Gridcoin testnet was the only network this gate ever ran against, and it ran through the
    booleans. A genesis probe that turned its own failure into a refusal would take that away.
    """
    run = _run_gate(None, ALL_TRUE, genesis_error="Method not found: getblockhash")

    assert _verdict(run, "getblockhash(0)") == SKIP
    assert _verdict(run, "test network stated by the daemon") == OK
    for method, key, _accepted in funding_steps.TEST_NETWORK_SIGNALS:
        assert _verdict(run, f"{method}.{key}") == OK, run.checks


def test_testnet_genesis_is_itself_a_positive_statement():
    """The testnet hash is evidence in its own right, not merely 'not mainnet'."""
    run = _run_gate(GENESIS["test"], ALL_TRUE)

    assert _verdict(run, "getblockhash(0)") == OK
    assert _verdict(run, "test network stated by the daemon") == OK


def test_nothing_positive_anywhere_REFUSES_and_names_the_genesis_it_got():
    """Absence of evidence is mainnet, and the refusal says what the genesis actually said."""
    with pytest.raises(RegtestSetupError) as caught:
        _run_gate(None, {}, genesis_error="connection refused")

    message = str(caught.value)
    assert "did not SAY it is on a test network" in message, message
    assert "getblockhash(0)" in message, "the enumerated probes must include the genesis"
    assert "connection refused" in message, "and what it answered, so the operator can act"


def test_the_gate_asks_for_the_genesis_BEFORE_the_booleans():
    """Order is load-bearing: the booleans' interpretation depends on the genesis verdict.

    A run that read the booleans first would have to re-interpret them afterwards, and
    `regtest_identified` is passed into grc_boolean_network_signals() precisely so there is
    one place that decides it. This pins the ordering that makes that possible.
    """
    node = _Node(GENESIS["regtest"], ALL_FALSE)
    funding_steps.assert_test_network(_Run(node))

    assert node.calls[0] == "getblockhash", node.calls


def test_the_boolean_interpreter_can_be_called_with_seeded_inputs():
    """grc_boolean_network_signals() is the extracted DECISION and must stand alone.

    Rule 12's C901 remedy is to extract the decision so it can be called with seeded inputs
    rather than to raise the ceiling. This is that call: the same three False answers, read
    twice, with only the regtest flag differing.
    """
    seeded = _Node(GENESIS["regtest"], ALL_FALSE)

    positives, mainnet = funding_steps.grc_boolean_network_signals(
        _Run(seeded), seeded, regtest_identified=True)
    assert (positives, mainnet) == ([], []), "regtest: False establishes nothing either way"

    positives, mainnet = funding_steps.grc_boolean_network_signals(
        _Run(seeded), seeded, regtest_identified=False)
    assert positives == [], positives
    assert len(mainnet) == len(funding_steps.TEST_NETWORK_SIGNALS), mainnet
    assert all("False" in statement for statement in mainnet), mainnet


def test_btc_and_ltc_are_untouched_by_any_of_this(monkeypatch):
    """Only GRC takes this path; BTC and LTC still go through daemons.assert_regtest().

    The genesis table is Gridcoin's. A change here must not reach the two chains whose
    `chain == "regtest"` assertion already works.
    """
    calls: list[str] = []

    class _BtcRun(_Run):
        asset = "BTC"

    run = _BtcRun(_Node(GENESIS["regtest"], ALL_FALSE))
    monkeypatch.setattr(
        funding_steps.daemons, "assert_regtest",
        lambda console, config, we_started_it: calls.append("assert_regtest") or {})
    funding_steps.assert_test_network(run)

    assert calls == ["assert_regtest"], calls
    assert run.checks == [], "the GRC signal table must not have been consulted for BTC"
    assert "getblockhash" not in run.node().calls, run.node().calls


def test_FAIL_is_still_reachable_for_a_boolean_that_says_the_wrong_thing():
    """A signal whose value is neither accepted nor a mainnet statement must still FAIL.

    Pinned because the regtest branch added a third outcome to that chain of conditionals,
    and an over-broad suppression there would quietly turn a wrong answer into a skip.
    """
    method, key, _accepted = funding_steps.TEST_NETWORK_SIGNALS[0]
    booleans = dict(ALL_TRUE)
    booleans[method] = {key: "neither-true-nor-false"}

    run = _run_gate(GENESIS["regtest"], booleans)

    assert _verdict(run, f"{method}.{key}") == FAIL, run.checks
    assert _verdict(run, "test network stated by the daemon") == OK, "the genesis still carries it"
