"""Everything about the 2-of-2 harness that can be tested with no chain at all.

Role: test (seeded inputs, a stub RPC that records calls, no socket)
Reads: nothing. Every daemon answer here is canned.
Writes: nothing.
Can move funds: no. Nothing is broadcast; the stub records what WOULD have been.
Mainnet-safe: yes.
Live-safe: yes

WHY A HARNESS NEEDS ITS OWN TESTS, WHICH IS NOT AN OBVIOUS THING TO WANT.

`adaptor_regtest_verify.py` exists because a source reading is not evidence. It will first
run on somebody else's computer, and the question that decides whether its output is worth
anything is not "did it pass" but "could it have passed for the wrong reason". Three ways it
could:

  - it reports a PASS when a decisive check was never attempted. A SKIP row in a verdict
    line is easy to skim past, so the verdict itself has to refuse.
  - it reports a PASS when the chain ACCEPTED one of the two footguns. An accepted
    transposition is a finding about the chain, and scoring it green would bury the single
    most important thing this harness could ever discover.
  - it runs against MAINNET. Gridcoin has no regtest mode, so the refusal that protects the
    BTC/LTC runs cannot be the one that protects the GRC run, and a second refusal is a
    second thing that can be wrong.

Those three are what this file pins. None of them can be tested by running the harness for
real, because a real run that passes says nothing about what it would have done if the chain
had answered differently -- which is precisely why they are stubbed here.

NO TEST HERE READS SOURCE. The stub answers RPC calls and the assertions are on the outcome
fields and the recorded calls.
"""

from __future__ import annotations

import importlib.util
import io
from decimal import Decimal
from pathlib import Path

import pytest
from chains.base import RPCError
from conftest import RPC_FIXTURE_AUTH, RPC_FIXTURE_USER
from modules import adaptor_swap_chain as chain
from modules.adaptor_swap_chain import LOCKTIME_THRESHOLD, assert_timelocks_ordered
from modules.adaptor_swap_scripts import OP_0, two_of_two_redeem_script
from modules.htlc_timelock import SECONDS_PER_BLOCK as TIMELOCK_SECONDS_PER_BLOCK
from regtest import (
    adaptor_steps,
    daemons,
)
from regtest.console import FAIL, OK, SKIP, Console
from regtest.daemons import ChainConfig, RegtestSetupError
from regtest.keys import generate_key

# NOT CREDENTIALS, and they come from tests/conftest.py rather than being spelled again
# here. Two reasons, and the second is the real one:
#
#   - ruff's S105/S106 look for names containing `password`, `secret` and the like, and
#     conftest's names deliberately avoid them -- so the finding is answered by the code
#     rather than silenced (rule 19: a suppression is a claim you checked, not a way to quiet
#     a finding nobody read).
#   - a second copy of a fixture value is rule 8's shape whether the value matters or not,
#     and conftest.py's own comment says a third caller would want them. This is the third.
#
# Every ChainConfig in this file points at a StubNode that never opens a socket, so neither
# string is ever sent anywhere; they exist only because ChainConfig requires the fields.
STUB_RPC_USER = RPC_FIXTURE_USER
STUB_RPC_PASSWORD = RPC_FIXTURE_AUTH

GRC_ENV_NAMES = ("GRC_RPC_HOST", "GRC_RPC_PORT", "GRC_RPC_USER", "GRC_RPC_PASS")
ADAPTOR_ENV_NAMES = tuple(f"ST_ADAPTOR_{name}" for name in ("GRC_RPC_HOST", "GRC_RPC_PORT", "GRC_RPC_USER", "GRC_RPC_PASS"))


class StubNode:
    """A daemon that answers from a dict and records every call.

    `answers` maps a method name to either a value or a callable taking the params. A method
    that is NOT in the dict raises RPCError, which is what a real daemon does for a method it
    does not have -- measured on Gridcoin 2026-09-27, `gettxout` comes back as
    `{'code': -32601, 'message': 'Method not found'}`.

    It raises rather than returning None deliberately: a stub answering None for a call
    nobody configured would let a step pass while never having asked the question the test is
    about. And it raises the type the harness catches, rather than AssertionError, because a
    stub whose failures cannot be handled by the code under test would be testing the stub.
    """

    def __init__(self, answers: dict) -> None:
        self.answers = answers
        self.calls: list[tuple[str, tuple]] = []

    def call(self, method: str, *params):
        self.calls.append((method, params))
        if method not in self.answers:
            raise RPCError(f"{method}: code=-32601 message=Method not found (this stub has no answer for it)")
        answer = self.answers[method]
        return answer(*params) if callable(answer) else answer

    def methods_called(self) -> list[str]:
        return [method for method, _ in self.calls]


@pytest.fixture
def console() -> Console:
    return Console(total_steps=adaptor_steps.TOTAL_STEPS)


def _config(asset: str = "LTC") -> ChainConfig:
    return ChainConfig(
        asset=asset, daemon_path="x", cli_path="y", datadir=Path("/nonexistent"),
        host="127.0.0.1", port=1, rpc_user=STUB_RPC_USER, rpc_password=STUB_RPC_PASSWORD, conf_name="c.conf", pid_name="c.pid",
    )


def _run_with(console: Console, monkeypatch, answers: dict, asset: str = "LTC") -> tuple[adaptor_steps.Run, StubNode]:
    node = StubNode(answers)
    run = adaptor_steps.Run(console=console, config=_config(asset), wallet="")
    monkeypatch.setattr(adaptor_steps, "adapter_for", lambda config, wallet="": node)
    return run, node


# ---------------------------------------------------------------------------
# A SKIP IS NOT A PASS. The verdict has to refuse rather than report the rest.
# ---------------------------------------------------------------------------


def test_the_verdict_says_NOT_ESTABLISHED_when_a_decisive_check_was_skipped():
    """THE FAILURE THIS FILE EXISTS FOR. Three of four green and one never attempted is not
    a pass -- it means the harness never tested the footgun, and the footgun is the thing
    only a chain can answer."""
    outcome = adaptor_steps.ChainOutcome(
        asset="GRC",
        located_by_script_match=OK,
        spends_in_correct_order=OK,
        refused_when_transposed=OK,
        refused_without_op0=SKIP,
    )
    verdict = outcome.verdict()
    assert "NOT ESTABLISHED" in verdict
    assert "SPENDABLE" not in verdict


def test_the_verdict_is_a_pass_only_when_all_four_decisive_checks_are_green():
    outcome = adaptor_steps.ChainOutcome(
        asset="GRC",
        located_by_script_match=OK,
        spends_in_correct_order=OK,
        refused_when_transposed=OK,
        refused_without_op0=OK,
    )
    assert "IS SPENDABLE ON THIS CHAIN" in outcome.verdict()
    assert "not a source reading" in outcome.verdict()


def test_a_single_FAIL_dominates_every_other_result():
    outcome = adaptor_steps.ChainOutcome(
        asset="BTC",
        located_by_script_match=OK,
        spends_in_correct_order=OK,
        refused_when_transposed=FAIL,
        refused_without_op0=OK,
    )
    assert "DID NOT WORK" in outcome.verdict()


def test_a_fresh_outcome_is_all_SKIP_and_not_all_False():
    """"Not attempted" and "attempted and failed" must not render identically. An outcome
    initialized to False would print a clean row of failures for a chain whose daemon was
    never reachable."""
    outcome = adaptor_steps.ChainOutcome(asset="BTC")
    assert outcome.located_by_script_match == SKIP
    assert outcome.spends_in_correct_order == SKIP
    assert "NOT ESTABLISHED" in outcome.verdict()
    assert outcome.notes == []


def test_the_cancel_verdict_separates_relay_from_consensus():
    """A relay refusal says a node will not pass it on; only a refusal to MINE says a miner
    could not have included it. Reporting the two as one result is what this line stops."""
    relay_only = adaptor_steps.ChainOutcome(
        asset="GRC", cancel_accepted_at_t1=OK,
        cancel_refused_before_t1_by_relay=OK, cancel_refused_before_t1_by_consensus=SKIP,
    )
    assert "RELAY-refused" in relay_only.cancel_verdict()
    assert "NOT measured" in relay_only.cancel_verdict()
    both = adaptor_steps.ChainOutcome(
        asset="BTC", cancel_accepted_at_t1=OK,
        cancel_refused_before_t1_by_relay=OK, cancel_refused_before_t1_by_consensus=OK,
    )
    assert "CONSENSUS-enforced" in both.cancel_verdict()
    never = adaptor_steps.ChainOutcome(asset="LTC")
    assert "NOT ESTABLISHED" in never.cancel_verdict()


# ---------------------------------------------------------------------------
# The mainnet refusals. Two of them, because Gridcoin cannot use the other one.
# ---------------------------------------------------------------------------


def test_the_gridcoin_mainnet_port_is_refused_by_number(monkeypatch):
    """15715 is Gridcoin MAINNET and it holds the operator's staking balance. This harness
    broadcasts."""
    for name in GRC_ENV_NAMES + ADAPTOR_ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("GRC_RPC_HOST", "127.0.0.1")
    monkeypatch.setenv("GRC_RPC_PORT", "15715")
    monkeypatch.setenv("GRC_RPC_USER", STUB_RPC_USER)
    monkeypatch.setenv("GRC_RPC_PASS", STUB_RPC_PASSWORD)
    with pytest.raises(RegtestSetupError, match="MAINNET"):
        adaptor_steps.resolve_config("GRC")


def test_an_unset_gridcoin_port_is_refused_rather_than_defaulted(monkeypatch):
    """network_target.py's incident: an unset GRC_RPC_PORT once fell through to 15715 and the
    serving path polled the live staking wallet on a loop. A silent default is a choice of
    which blockchain real money lives on."""
    for name in GRC_ENV_NAMES + ADAPTOR_ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("GRC_RPC_USER", STUB_RPC_USER)
    monkeypatch.setenv("GRC_RPC_PASS", STUB_RPC_PASSWORD)
    with pytest.raises(RegtestSetupError, match="no default"):
        adaptor_steps.resolve_config("GRC")


def test_a_testnet_gridcoin_port_resolves(monkeypatch):
    for name in GRC_ENV_NAMES + ADAPTOR_ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("GRC_RPC_HOST", "127.0.0.1")
    monkeypatch.setenv("GRC_RPC_PORT", "25715")
    monkeypatch.setenv("GRC_RPC_USER", STUB_RPC_USER)
    monkeypatch.setenv("GRC_RPC_PASS", STUB_RPC_PASSWORD)
    config = adaptor_steps.resolve_config("GRC")
    assert config.asset == "GRC"
    assert config.port == 25715


def test_a_daemon_that_does_not_SAY_it_is_on_a_test_network_is_refused(console, monkeypatch):
    """AN ABSENCE OF EVIDENCE IS TREATED AS MAINNET. A daemon whose getblockchaininfo says
    `main`, and which has no getinfo and no getmininginfo, is refused -- not accepted
    because nothing contradicted it."""
    run, _ = _run_with(console, monkeypatch, {"getblockchaininfo": {"chain": "main"}}, asset="GRC")
    with pytest.raises(RegtestSetupError, match="did not SAY it is on a test network"):
        adaptor_steps.assert_test_network(run)


def test_a_daemon_whose_every_call_fails_is_refused_rather_than_trusted(console, monkeypatch):
    """The stub answers nothing, so every probe raises. A harness that treated 'no answer' as
    'probably a test network' would broadcast against whatever was actually there."""
    run, _ = _run_with(console, monkeypatch, {}, asset="GRC")
    with pytest.raises(RegtestSetupError, match="did not SAY"):
        adaptor_steps.assert_test_network(run)


def test_one_positive_signal_is_enough_and_it_says_which(console, monkeypatch):
    """getinfo.testnet is true even though getblockchaininfo is absent. That is a daemon
    SAYING it, which is the thing required."""
    run, node = _run_with(console, monkeypatch, {"getinfo": {"testnet": True}}, asset="GRC")
    adaptor_steps.assert_test_network(run)
    assert "getinfo" in node.methods_called()


@pytest.mark.parametrize("chain_value", ["test", "testnet", "regtest", "signet"])
def test_every_accepted_chain_value_is_a_test_network(console, monkeypatch, chain_value):
    run, _ = _run_with(console, monkeypatch, {"getblockchaininfo": {"chain": chain_value}}, asset="GRC")
    adaptor_steps.assert_test_network(run)


def test_the_gridcoin_path_never_demands_chain_equals_regtest(console, monkeypatch):
    """Gridcoin has NO regtest mode, so daemons.assert_regtest() could never pass there. The
    second refusal exists because weakening the one that already guards BTC and LTC would be
    the wrong fix."""
    run, _ = _run_with(console, monkeypatch, {"getblockchaininfo": {"chain": "test"}}, asset="GRC")
    adaptor_steps.assert_test_network(run)


# ---------------------------------------------------------------------------
# An ACCEPTED footgun must score FAIL, and it must leave a note saying why it matters.
# ---------------------------------------------------------------------------


def _built_chain(asset: str = "LTC"):
    """A real BuiltChain, built by the real builders, spending an outpoint that does not exist."""
    alice, bob = generate_key(), generate_key()
    script = two_of_two_redeem_script(alice.public_key, bob.public_key)
    setup = adaptor_steps.LockSetup(
        label="T -- a test", alice=alice, bob=bob, lock_script=script, cancel_script=script
    )
    context = chain.ChainContext(
        asset=asset, lock_redeem_script=script, cancel_redeem_script=script,
        alice_script=alice.p2pkh_script, bob_script=bob.p2pkh_script,
    )
    lock = chain.Outpoint(txid="ab" * 32, vout=0, value_satoshis=1_000_000)
    return adaptor_steps.BuiltChain(
        setup=setup, context=context, lock_raw_hex="00", lock_txid=lock.txid, lock_vout=0,
        lock_satoshis=lock.value_satoshis, lock_fee=10_000,
        redeem=chain.build_redeem(context, lock),
        cancel=chain.build_cancel(context, lock, 1_500), t1=1_500, t2=1_650,
    )


def test_a_chain_that_ACCEPTS_a_transposed_scriptsig_is_scored_FAIL_and_noted(console, monkeypatch):
    """THE SINGLE MOST IMPORTANT THING THIS HARNESS COULD DISCOVER, and scoring it green
    would bury it. A stub that accepts everything must produce FAIL on both refusals."""
    built = _built_chain()
    run, _ = _run_with(console, monkeypatch, {"sendrawtransaction": "de" * 32})
    outcome = adaptor_steps.ChainOutcome(asset="LTC")
    adaptor_steps.step_8_refusals(run, built, outcome)
    assert outcome.refused_when_transposed == FAIL
    assert outcome.refused_without_op0 == FAIL
    assert any("TRANSPOSED" in note for note in outcome.notes)
    assert any("NO OP_0 DUMMY" in note for note in outcome.notes)
    assert "DID NOT WORK" in outcome.verdict()


def test_a_chain_that_REFUSES_both_footguns_is_scored_OK(console, monkeypatch):
    built = _built_chain()

    def refuse(*_params):
        raise RPCError("mandatory-script-verify-flag-failed (Script evaluated without error but finished with a false/empty top stack element)")

    run, _ = _run_with(console, monkeypatch, {"sendrawtransaction": refuse})
    outcome = adaptor_steps.ChainOutcome(asset="LTC")
    adaptor_steps.step_8_refusals(run, built, outcome)
    assert outcome.refused_when_transposed == OK
    assert outcome.refused_without_op0 == OK
    assert outcome.notes == [], "a refusal is the expected outcome and earns no note"


def test_the_transposed_and_correct_scriptsigs_are_the_SAME_LENGTH(console, monkeypatch):
    """This is why only a chain can settle it, and it is asserted rather than claimed.

    Both are built from the SAME two signatures, produced once and swapped, so a difference
    here could only come from the assembler."""
    built = _built_chain()
    alice_sig, bob_sig = adaptor_steps._signatures_in_key_order(built.setup, built.redeem.digest)
    correct, _ = chain.assemble(built.redeem, alice_sig, bob_sig)
    transposed, _ = chain.assemble(built.redeem, bob_sig, alice_sig)
    assert len(correct) == len(transposed)
    assert correct != transposed, "the same bytes in two orders, so the transaction differs"


def test_the_missing_OP_0_variant_differs_by_exactly_one_byte(console, monkeypatch):
    """Built by REMOVING the byte from the real assembler's output rather than by writing a
    second assembler, so the harness measures one change and not two."""
    built = _built_chain()
    alice_sig, bob_sig = adaptor_steps._signatures_in_key_order(built.setup, built.redeem.digest)
    correct, _ = chain.assemble(built.redeem, alice_sig, bob_sig)
    without = adaptor_steps._script_sig_without_op0(built, alice_sig, bob_sig)
    assert len(bytes.fromhex(correct)) - len(bytes.fromhex(without)) == len(OP_0)


def test_the_signature_order_helper_puts_the_FIRST_key_first(console):
    """two_of_two_redeem_script(alice, bob) puts Alice's key first, so Alice's signature goes
    first. Stated once here, and the transposition test swaps this result rather than
    rebuilding it."""
    built = _built_chain()
    alice_sig, bob_sig = adaptor_steps._signatures_in_key_order(built.setup, built.redeem.digest)
    expected_alice = built.setup.alice.sign_digest(built.redeem.digest)
    assert alice_sig.startswith(expected_alice)
    assert bob_sig.startswith(built.setup.bob.sign_digest(built.redeem.digest))
    assert alice_sig != bob_sig


# ---------------------------------------------------------------------------
# The consensus probe: a missing generateblock is a SKIP with a reason, never a pass.
# ---------------------------------------------------------------------------


def test_a_daemon_with_no_generateblock_gets_a_SKIP_and_a_named_note(console, monkeypatch):
    """Gridcoin is expected to be in exactly this position. Reporting a pass would claim the
    stronger consensus result from a relay refusal."""
    built = _built_chain()
    run, _ = _run_with(console, monkeypatch, {"help": "help: unknown command: generateblock"})
    outcome = adaptor_steps.ChainOutcome(asset="GRC")
    adaptor_steps._mine_early_cancel(run, built, "00", outcome)
    assert outcome.cancel_refused_before_t1_by_consensus == SKIP
    assert any("no generateblock" in note for note in outcome.notes)
    assert "RELAY only" in " ".join(outcome.notes)


def test_a_daemon_that_MINES_an_early_cancel_is_scored_FAIL_and_noted(console, monkeypatch):
    """If a daemon mines a transaction whose nLockTime is in the future, the cancel path's
    timelock is worth nothing against a miner. That is a finding, and it must not be green."""
    built = _built_chain()
    run, _ = _run_with(console, monkeypatch, {
        "help": "generateblock output ...",
        "getnewaddress": "mockAddressTheStubReturns",
        "generateblock": {"hash": "ff" * 32},
    })
    outcome = adaptor_steps.ChainOutcome(asset="BTC")
    adaptor_steps._mine_early_cancel(run, built, "00", outcome)
    assert outcome.cancel_refused_before_t1_by_consensus == FAIL
    assert any("MINED A CANCEL" in note for note in outcome.notes)


def test_a_generateblock_that_refuses_is_the_consensus_result(console, monkeypatch):
    built = _built_chain()

    def refuse(*_params):
        raise RPCError("generateblock: code=-1 message=TestBlockValidity failed: bad-txns-nonfinal")

    run, _ = _run_with(console, monkeypatch, {
        "help": "generateblock output ...",
        "getnewaddress": "mockAddressTheStubReturns",
        "generateblock": refuse,
    })
    outcome = adaptor_steps.ChainOutcome(asset="BTC")
    adaptor_steps._mine_early_cancel(run, built, "00", outcome)
    assert outcome.cancel_refused_before_t1_by_consensus == OK
    assert outcome.notes == []


# ---------------------------------------------------------------------------
# The two locks must not share a scriptPubKey, or outcome 1 means nothing.
# ---------------------------------------------------------------------------


def test_the_two_locks_get_different_scriptpubkeys(console, monkeypatch):
    """Outcome 1 is "located by scriptPubKey match". If both locks hashed to the same P2SH,
    a match could not say which output it found and the outcome would be vacuous."""
    run, _ = _run_with(console, monkeypatch, {})
    setup_a, setup_b = adaptor_steps.step_4_build_scripts(run)
    assert setup_a.lock_script_pubkey != setup_b.lock_script_pubkey
    assert setup_a.alice.public_key != setup_b.alice.public_key


def test_within_one_lock_the_cancel_script_matches_the_protocol(console, monkeypatch):
    """docs/monero_swap_protocol.md section 2 says the cancel output is a SECOND 2-of-2 over
    the same {A_pk, B_pk}. So the two scripts ARE identical here, and the harness says so on
    screen and locates the cancel output by txid:vout instead of by a script match."""
    run, _ = _run_with(console, monkeypatch, {})
    setup_a, _ = adaptor_steps.step_4_build_scripts(run)
    assert setup_a.lock_script == setup_a.cancel_script


# ---------------------------------------------------------------------------
# Timelocks are block counts and are never rendered in microfortnights.
# ---------------------------------------------------------------------------


def test_T1_and_T2_are_block_counts_in_the_right_order():
    assert adaptor_steps.T2_BLOCKS_AHEAD > adaptor_steps.T1_BLOCKS_AHEAD > 0
    tip = 200
    t1, t2 = tip + adaptor_steps.T1_BLOCKS_AHEAD, tip + adaptor_steps.T2_BLOCKS_AHEAD
    assert assert_timelocks_ordered(t1, t2) is None
    assert t2 < LOCKTIME_THRESHOLD, "these are HEIGHTS, so they must stay below the timestamp boundary"


def test_every_lock_amount_clears_its_chains_minimum():
    """Measured rather than assumed: the GRC minimum is 0.02, two fee floors plus dust, and a
    harness funded below it would fail in step 6 for a reason that is not about the script."""
    alice, bob = generate_key(), generate_key()
    script = two_of_two_redeem_script(alice.public_key, bob.public_key)
    for asset, coin in adaptor_steps.LOCK_COIN.items():
        context = chain.ChainContext(
            asset=asset, lock_redeem_script=script, cancel_redeem_script=script,
            alice_script=alice.p2pkh_script, bob_script=bob.p2pkh_script,
            ntime=0x6AB99A27 if asset == "GRC" else None,
        )
        satoshis = int(Decimal(coin) * Decimal(100_000_000))
        minimum = chain.minimum_lock_value_satoshis(context)
        assert satoshis > minimum, f"{asset}: LOCK_COIN {coin} is {satoshis} sat, minimum is {minimum}"
        headroom = int(Decimal(adaptor_steps.FUNDING_HEADROOM_COIN[asset]) * Decimal(100_000_000))
        assert headroom > 0


def test_a_wallet_failure_is_NOT_scored_as_a_consensus_refusal(console, monkeypatch):
    """THE DEFECT THIS FILE FOUND, 2026-09-28, and it is the reason a harness needs tests.

    `_mine_early_cancel` used to resolve the mining address INSIDE the try that catches
    RPCError, so a `getnewaddress` failure -- a locked wallet, no wallet loaded, a typo --
    was scored as `OK 10b CONSENSUS refuses the early cancel`. That is the strongest claim
    this harness makes, produced by the wallet being unavailable. It is now SKIP with the
    reason, and a refusal only counts when it comes from the thing under test.
    """
    built = _built_chain()
    run, _ = _run_with(console, monkeypatch, {"help": "generateblock output ..."})
    outcome = adaptor_steps.ChainOutcome(asset="BTC")
    adaptor_steps._mine_early_cancel(run, built, "00", outcome)
    assert outcome.cancel_refused_before_t1_by_consensus == SKIP
    assert any("never attempted" in note for note in outcome.notes)
    # AND THE SCREEN MUST AGREE WITH THE FIELD. The verdict reads the field, but the operator
    # reads the line, and a mutation that changed only the printed token would otherwise
    # survive -- it did, on the first mutation pass, which is why this assertion exists.
    assert console.counts[SKIP] >= 1, "the printed outcome must be SKIP too, not only the field"
    assert console.counts[OK] == 0, "nothing in this step earned an OK; the wallet was unavailable"


# ---------------------------------------------------------------------------
# The GRIDCOIN-only code paths, which no run in this container can reach.
# ---------------------------------------------------------------------------


def test_the_wait_for_blocks_path_returns_when_the_tip_arrives(console, monkeypatch):
    """EXERCISED HERE BECAUSE NO RUN IN THIS CONTAINER CAN REACH IT.

    Gridcoin cannot be told to produce a block, so `_mine` waits instead -- and that whole
    branch would first execute on the operator's machine, where a NameError costs a round trip
    and half an hour of staking-wallet waiting. The stub advances the tip on each call, so the
    loop is driven through its real exit.

    The poll interval is patched to zero. A test that actually slept ten seconds per block
    would be a test somebody deletes.
    """
    heights = iter([100, 101, 103])
    run, _ = _run_with(console, monkeypatch, {"getblockcount": lambda *_: next(heights)})
    monkeypatch.setattr(adaptor_steps, "GRC_POLL_INTERVAL_SECONDS", 0.0)
    monkeypatch.setattr(adaptor_steps, "GRC_PROGRESS_INTERVAL_SECONDS", 0.0)
    assert adaptor_steps._wait_for_height(run, 103) == 103


def test_the_wait_for_blocks_path_gives_up_with_a_named_timeout(console, monkeypatch):
    """A wait that never ends is rule 14's defect with a clock on it. The refusal names the
    variable that changes it, because the operator reads the screen and not the source."""
    run, _ = _run_with(console, monkeypatch, {"getblockcount": 100})
    monkeypatch.setattr(adaptor_steps, "GRC_POLL_INTERVAL_SECONDS", 0.0)
    monkeypatch.setattr(adaptor_steps, "GRC_BLOCK_WAIT_TIMEOUT_SECONDS", 0.0)
    with pytest.raises(RegtestSetupError, match="ST_ADAPTOR_GRC_BLOCK_TIMEOUT_SECONDS"):
        adaptor_steps._wait_for_height(run, 200)


def test_mine_takes_the_WAITING_branch_when_the_daemon_cannot_be_told_to_mine(console, monkeypatch):
    """`run.mines` reads the generatetoaddress capability, not the asset name. A Gridcoin
    daemon that grew generatetoaddress would be driven the same way a regtest one is."""
    heights = iter([50, 51])
    run, node = _run_with(console, monkeypatch, {"getblockcount": lambda *_: next(heights)})
    run.capabilities = {"generatetoaddress": False}
    monkeypatch.setattr(adaptor_steps, "GRC_POLL_INTERVAL_SECONDS", 0.0)
    adaptor_steps._mine(run, 1)
    assert "generatetoaddress" not in node.methods_called()
    assert not run.mines


def test_the_gridcoin_lock_state_is_REPORTED_and_never_asserted(console, monkeypatch):
    """Measured 2026-09-26 and recorded in README.md: `getwalletinfo` returns exactly one lock
    field and NO Gridcoin RPC reports a staking-only unlock back. A staking-only unlock cannot
    send, so reporting OK here would be a guess in the voice of a measurement about the single
    condition that decides whether the funding send works. It must be SKIP either way."""
    for unlocked_until in (0, 1790000000):
        console.counts[SKIP] = 0
        console.counts[OK] = 0
        run, _ = _run_with(console, monkeypatch, {"getwalletinfo": {"unlocked_until": unlocked_until}}, asset="GRC")
        adaptor_steps._report_gridcoin_lock_state(run)
        assert console.counts[SKIP] == 1, f"unlocked_until={unlocked_until} must still be SKIP"
        assert console.counts[OK] == 0, "an unlocked wallet must not be reported as a pass"


def test_the_selector_probe_is_never_fatal(console, monkeypatch):
    """It is a READ over the daemon's own listunspent rows, not a step anything depends on. A
    wallet that cannot answer is a SKIP with the reason -- the harness's job here is to say
    whether select_funding_inputs() can read a real row, not to fund anything with it."""
    run, _ = _run_with(console, monkeypatch, {})
    run.capabilities = {"listunspent": True}
    console.counts[SKIP] = 0
    adaptor_steps._exercise_input_selection(run)
    assert console.counts[SKIP] == 1
    assert console.counts[FAIL] == 0


def test_the_selector_probe_reads_a_real_shaped_listunspent_row(console, monkeypatch):
    """The three fields it reads are `txid`, `vout` and `amount`, and `amount` is in COIN. A
    unit test over rows a test wrote says nothing about a daemon's row shape, which is why this
    one uses the shape both Bitcoin Core and Litecoin Core actually return."""
    row = {"txid": "aa" * 32, "vout": 0, "amount": 50.0, "confirmations": 101, "spendable": True}
    run, _ = _run_with(console, monkeypatch, {"listunspent": [row]})
    run.capabilities = {"listunspent": True}
    console.counts[OK] = 0
    adaptor_steps._exercise_input_selection(run)
    assert console.counts[OK] == 1
    assert console.counts[FAIL] == 0


# ---------------------------------------------------------------------------------------
# THE LIVENESS PROBE, AND THE DAEMON FAMILY IT DID NOT KNOW ABOUT.
#
# Measured 2026-09-28: `adaptor_regtest_verify.py --chain grc` failed at step 1 with "nothing
# answered `uptime`" against a Gridcoin testnet daemon that was demonstrably up -- three atomic
# swaps had completed through it twenty minutes earlier. `gridcoinresearchd -testnet help
# uptime` answers "unknown command: uptime"; getblockcount on the same port with the same
# credentials returns 3295729. `uptime` arrived in Bitcoin Core 0.15; Gridcoin forked long
# before it.
#
# daemons.rpc_answers()'s docstring had claimed `uptime` "exists on both daemon families",
# which was true when there were two and false the moment a third arrived -- the same shape as
# Gridcoin having no `gettxout`, which made every GRC spend print a stack trace in front of a
# success.
# ---------------------------------------------------------------------------------------


class _OnlyAnswers:
    """A daemon that answers exactly one named set of methods and raises for the rest."""

    def __init__(self, *answers: str) -> None:
        self.answers = set(answers)
        self.asked: list[str] = []

    def call(self, method: str, *args):
        self.asked.append(method)
        if method not in self.answers:
            raise RuntimeError(f"unknown command: {method}")
        return 3295729 if method == "getblockcount" else 12345


def _patch_adapter(monkeypatch, daemon):
    monkeypatch.setattr(daemons, "adapter_for", lambda _config: daemon)


def test_a_daemon_with_no_uptime_is_still_found_alive(monkeypatch):
    """THE GRIDCOIN CASE, and the one this repository actually hit. A family that never had
    `uptime` must not be reported as not answering -- that sends the operator to check
    credentials which are fine, which is exactly what happened."""
    daemon = _OnlyAnswers("getblockcount")
    _patch_adapter(monkeypatch, daemon)
    assert daemons.liveness_probe_that_answers(object()) == "getblockcount"
    assert daemon.asked == ["uptime", "getblockcount"], (
        "uptime is tried first and its miss falls through rather than deciding"
    )
    # Cleared before the second call: both functions probe, so asserting on a shared list after
    # both would read four entries. My first version did exactly that and went red -- the
    # accumulation was mine, not the code's.
    daemon.asked.clear()
    assert daemons.rpc_answers(object()) is True


def test_a_daemon_that_answers_uptime_is_not_asked_anything_further(monkeypatch):
    """BTC and LTC answer the first probe, so the fallback costs them no round trip -- this is
    an ADDED route, not a replaced one, and the existing two families are unaffected."""
    daemon = _OnlyAnswers("uptime", "getblockcount")
    _patch_adapter(monkeypatch, daemon)
    assert daemons.liveness_probe_that_answers(object()) == "uptime"
    assert daemon.asked == ["uptime"], "it must stop at the first method that answers"


def test_a_daemon_answering_nothing_is_reported_as_not_answering(monkeypatch):
    """The real failure still fails. A closed port, wrong credentials and a daemon that is
    down all land here, and the caller's message names every method tried rather than one."""
    daemon = _OnlyAnswers()
    _patch_adapter(monkeypatch, daemon)
    assert daemons.liveness_probe_that_answers(object()) is None
    assert daemon.asked == list(daemons.LIVENESS_PROBES), "every probe is tried before giving up"
    daemon.asked.clear()
    assert daemons.rpc_answers(object()) is False


def test_getblockcount_is_in_the_probe_list_because_it_is_measured_to_work():
    """Not chosen for elegance. atomic_swap.py read tip 3295571 from getblockcount on the
    operator's Gridcoin testnet daemon on 2026-09-27 while funding a real GRC leg, and the
    same call returned 3295729 by hand on 2026-09-28. It predates every fork in this tree."""
    assert "getblockcount" in daemons.LIVENESS_PROBES
    assert daemons.LIVENESS_PROBES[0] == "uptime", (
        "uptime stays first: it needs no wallet and the two families that have it answer it"
    )


# ---------------------------------------------------------------------------------------
# THE WALL-CLOCK ANNOUNCEMENT, WHICH IS RULE 14 ON THE ONE RUN THAT NEEDS IT.
#
# A BTC or LTC run of this harness finishes in seconds because `generatetoaddress` makes a
# block on demand -- 6.6µfn (8s) for 40 checks on LTC, 2026-09-28. A GRC run WAITS for real
# testnet blocks and prints progress only every 30s, so the operator faces repeated
# 30-second gaps on a run driven against a staking wallet. CLAUDE.md rule 14 records what
# happens then, in the operator's words: "i cannot stand to wait who knows how the fuck long
# on a blinking cursor. how do i know it's not hung or broken?"
# ---------------------------------------------------------------------------------------


def test_the_grc_block_interval_is_read_off_the_timelock_authority_not_written_again():
    """rule 8. `modules/htlc_timelock.SECONDS_PER_BLOCK` has owned the per-chain target
    interval since it was written, and this file had 90 as a bare literal in two message
    strings. Two copies of one number agree the day they are written and drift after."""
    assert TIMELOCK_SECONDS_PER_BLOCK["GRC"] == adaptor_steps.GRC_SECONDS_PER_BLOCK
    source = Path(adaptor_steps.__file__).read_text()
    assert "90s a block" not in source, "the literal came back; read it off SECONDS_PER_BLOCK"


def test_the_expected_block_floor_moves_with_the_timelock_constant():
    """DERIVED, not a number somebody typed. If T2_BLOCKS_AHEAD changes -- and it is the
    constant that decides how long a GRC run takes -- the announced floor has to change with
    it, or the announcement becomes the stale-measurement defect rule 1 is about."""
    floor = adaptor_steps.expected_grc_blocks()
    assert floor > adaptor_steps.T2_BLOCKS_AHEAD, (
        "the floor must exceed T2 alone: lock A and lock B's own confirmations come first"
    )
    assert adaptor_steps.expected_grc_seconds() == floor * adaptor_steps.GRC_SECONDS_PER_BLOCK


def _entry_point():
    """Import `adaptor_regtest_verify.py` -- the ROOT entry point -- by path.

    By path rather than by name because the repository root is not on sys.path during a test
    run: tests/conftest.py adds `swap_terminal/` (the application imports its own modules
    rootlessly, which is CLAUDE.md rule 10's layout gap) and nothing adds the root. Importing
    it is safe: everything runnable in it is behind `if __name__ == "__main__"`.
    """
    root = Path(__file__).resolve().parent.parent
    spec = importlib.util.spec_from_file_location(
        "adaptor_regtest_verify_under_test", root / "adaptor_regtest_verify.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _recording_console() -> tuple[Console, io.StringIO]:
    """A Console writing to a StringIO, and the StringIO.

    NOT a `.written` list on Console, because Console has no such attribute -- it prints to
    `self.stream` and flushes every line on purpose (rule 14: a buffered progress line is the
    same as no progress line). Earlier in this session I wrote a test that asserted against a
    `console.lines` that does not exist; it compared four values against "" and passed, and
    would have passed with a debug line echoing every private key. `stream=` is the interface
    Console actually offers, read rather than guessed.
    """
    stream = io.StringIO()
    return Console(total_steps=adaptor_steps.TOTAL_STEPS, stream=stream), stream


def test_a_gridcoin_run_is_told_how_long_it_will_take_before_it_starts():
    """The announcement fires for GRC, names a duration, and says the floor is a floor."""
    console, stream = _recording_console()
    _entry_point()._announce_wall_clock(console, ["GRC"])
    printed = stream.getvalue()

    assert "CANNOT BE TOLD TO PRODUCE A BLOCK" in printed
    assert "\u00b5fn" in printed, "rule 6: the duration is reported in microfortnights, with a real \u00b5"
    assert "ufn" not in printed.replace("\u00b5fn", ""), "rule 6: never an ASCII u for the unit"
    assert "floor, not a prediction" in printed, (
        "rule 17: a floor stated as a prediction is a hypothesis in a measurement's voice"
    )
    assert "NEVER starts or stops a Gridcoin daemon" in printed
    assert str(adaptor_steps.expected_grc_blocks()) in printed, "the block count itself is printed"


def test_a_litecoin_run_is_not_given_a_duration_it_does_not_need():
    """Rule 14 asks for output that DISTINGUISHES cases. An eight-second run does not get a
    wall-clock warning, because a warning attached to everything stops being read."""
    console, stream = _recording_console()
    _entry_point()._announce_wall_clock(console, ["LTC"])

    assert stream.getvalue() == "", f"nothing should be printed for LTC, got {stream.getvalue()!r}"


def test_the_announcement_reaches_a_gridcoin_run_in_a_mixed_chain_set():
    """`--chain both` is BTC+LTC today, but CHAIN_SETS is data and a set containing GRC must
    still announce. Membership, not equality -- an `assets == ["GRC"]` check would go quiet
    the day somebody adds a grc+ltc set, which is the silent-regression shape rule 14 is
    about."""
    console, stream = _recording_console()
    _entry_point()._announce_wall_clock(console, ["LTC", "GRC"])

    assert "CANNOT BE TOLD TO PRODUCE A BLOCK" in stream.getvalue()
