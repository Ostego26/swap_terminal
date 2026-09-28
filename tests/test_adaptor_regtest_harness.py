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
import re
from decimal import Decimal
from pathlib import Path

import base58
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
from regtest.console import FAIL, OK, SKIP, XFAIL, Console
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


# ---------------------------------------------------------------------------
# THE NETWORK SIGNALS, AFTER 2026-09-28.
#
# Two tests here used to seed {"getblockchaininfo": {"chain": "test"}} and assert it was
# accepted. THAT RESPONSE SHAPE DOES NOT EXIST ON GRIDCOIN AT ANY VERSION, so they were
# testing a fiction and they passed. Gridcoin's getblockchaininfo pushes exactly eight fields
# -- blocks, in_sync, moneysupply, difficulty{current,target}, testnet, errors -- and `chain`
# is not among them; `grep '"chain"' src/rpc/blockchain.cpp` over Gridcoin master finds
# nothing. The live run proved it from the other end: `FAIL getblockchaininfo.chain:
# got=(none)` with no RPCError attached, which means the call SUCCEEDED and the key was
# simply absent.
#
# Deleted rather than adapted (rule 2: git history is the archive), and replaced by tests of
# what is now true -- the key that exists, the absence that must not be a FAIL, and the
# contradiction that must refuse.
# ---------------------------------------------------------------------------


def test_the_field_gridcoin_actually_carries_is_the_one_probed(console, monkeypatch):
    """getblockchaininfo.testnet -- field 13 of Gridcoin's own getblockchaininfo,
    `res.pushKV("testnet", OnTestnet())`. Probing `.chain` asked the right METHOD for a key
    this family does not have while the same response carried the answer."""
    run, node = _run_with(
        console, monkeypatch, {"getblockchaininfo": {"testnet": True}}, asset="GRC"
    )
    adaptor_steps.assert_test_network(run)
    assert "getblockchaininfo" in node.methods_called()
    assert console.counts[FAIL] == 0, "a daemon on testnet must produce no FAIL here"
    assert "chain" not in [key for _, key, _ in adaptor_steps.TEST_NETWORK_SIGNALS], (
        "`chain` came back into the signal table; Gridcoin's getblockchaininfo has no such key"
    )


def test_a_field_the_daemon_does_not_carry_is_SKIP_and_never_FAIL(console, monkeypatch):
    """THE DEFECT THAT ENDED THE FIRST GRC RUN'S EXIT CODE AT 1 ON A CORRECT DAEMON. The
    method answers, the key is absent, nothing failed and nothing was learned -- that is a
    third outcome, and collapsing it into FAIL puts a non-problem in the operator's
    "unexpected failures" list. Rule 14 in the other direction: a reader who learns to skim
    past FAILs is how twelve `exit_code=0` cycles beside "skipping" survived a deploy."""
    run, _ = _run_with(
        console, monkeypatch,
        # getblockchaininfo answers, WITHOUT the probed key. getinfo carries the positive.
        {"getblockchaininfo": {"blocks": 3295729, "in_sync": True}, "getinfo": {"testnet": True}},
        asset="GRC",
    )
    adaptor_steps.assert_test_network(run)
    assert console.counts[FAIL] == 0, (
        "an absent field is not a failure; this is the getblockchaininfo.chain defect"
    )
    assert console.counts[SKIP] >= 1, "and it must still be REPORTED -- silence is a defect"


def test_a_response_that_is_not_a_dict_is_also_SKIP(console, monkeypatch):
    """Same class, different shape: a daemon answering a scalar where a dict was expected has
    told us nothing, not told us we are on mainnet."""
    run, _ = _run_with(
        console, monkeypatch,
        {"getblockchaininfo": 3295729, "getmininginfo": {"testnet": True}},
        asset="GRC",
    )
    adaptor_steps.assert_test_network(run)
    assert console.counts[FAIL] == 0


def test_a_daemon_that_says_MAINNET_is_refused_even_while_saying_testnet(console, monkeypatch):
    """THE HOLE THIS CLOSES IS A LIVE-MONEY ONE. The aggregate check passes when any signal is
    positive, so before this a daemon whose getblockchaininfo said testnet=False while its
    getinfo said testnet=True would have scored one FAIL, one OK, AND PROCEEDED TO BROADCAST.
    The three signals read three different code paths and a proxied or half-migrated daemon
    can disagree with itself. Gridcoin MAINNET holds the operator's live staking balance, so
    "something said testnet" and "nothing said mainnet" are different claims and only the
    second one makes it safe to send."""
    run, _ = _run_with(
        console, monkeypatch,
        {"getblockchaininfo": {"testnet": False}, "getinfo": {"testnet": True}},
        asset="GRC",
    )
    with pytest.raises(RegtestSetupError, match="POSITIVELY STATED MAINNET"):
        adaptor_steps.assert_test_network(run)


def test_the_mainnet_refusal_names_the_contradiction_not_just_the_mainnet_signal(console, monkeypatch):
    """An operator who sees only "said mainnet" checks the port. One who sees "said mainnet
    while also saying testnet" knows the daemon itself is the problem. Rule 14: the message
    has to carry what the reader needs to act, and these are different actions."""
    run, _ = _run_with(
        console, monkeypatch,
        {"getblockchaininfo": {"testnet": False}, "getinfo": {"testnet": True}},
        asset="GRC",
    )
    with pytest.raises(RegtestSetupError) as caught:
        adaptor_steps.assert_test_network(run)
    message = str(caught.value)
    assert "contradicts itself" in message
    assert "getinfo.testnet=True" in message, "the conflicting positive is named"
    assert "getblockchaininfo.testnet=False" in message, "and so is the mainnet statement"


def test_a_daemon_that_ONLY_says_mainnet_is_refused_without_a_contradiction_claim(console, monkeypatch):
    """And the other half: with no positive to contradict, the message must not claim one."""
    run, _ = _run_with(
        console, monkeypatch, {"getblockchaininfo": {"testnet": False}}, asset="GRC"
    )
    with pytest.raises(RegtestSetupError) as caught:
        adaptor_steps.assert_test_network(run)
    assert "contradicts itself" not in str(caught.value)
    assert "Nothing was built" in str(caught.value)


def test_the_gridcoin_path_never_demands_chain_equals_regtest(console, monkeypatch):
    """Gridcoin has NO regtest mode, so daemons.assert_regtest() could never pass there. The
    second refusal exists because weakening the one that already guards BTC and LTC would be
    the wrong fix. Re-seeded with the field Gridcoin actually carries."""
    run, _ = _run_with(console, monkeypatch, {"getinfo": {"testnet": True}}, asset="GRC")
    adaptor_steps.assert_test_network(run)
    assert console.counts[FAIL] == 0


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
        # THE INVARIANT IS ABOUT THE VALUE, NOT THE CHECK COUNT. This line read `== 1`
        # because the value's SKIP was the only check in this function. Since 2026-09-28 a
        # SECOND check follows it -- probe_wallet_unlock_scope(), which asks BEHAVIORALLY what
        # the docstring above correctly says no state query can report. Against this stub
        # (which has no signrawtransaction answer) the probe returns "undetermined", itself a
        # SKIP, so the count is 2. The invariant the test exists for is untouched and is
        # asserted on the next line: `unlocked_until` is never scored OK, because a non-zero
        # value is exactly as consistent with a staking-only unlock that CANNOT send as with a
        # full one that can.
        assert console.counts[SKIP] >= 1, (
            f"unlocked_until={unlocked_until} must still be REPORTED -- rule 14, a value "
            f"nobody prints is a value nobody can act on"
        )
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


# ---------------------------------------------------------------------------------------
# THE STAKING-ONLY UNLOCK: A DIAGNOSED CONDITION MUST NOT ARRIVE AS AN UNHANDLED EXCEPTION.
#
# Measured on the operator's Gridcoin testnet daemon, 2026-09-28. The harness printed the
# hazard in prose at step 5 --
#
#   "a staking-only unlock CANNOT send (rpc -13) ... the funding send in step 6 is the test"
#
# -- and then, when exactly that occurred, reported it as
#
#   FAIL  GRC run: got=RPCError: sendtoaddress: code=-4 message=Error: Wallet unlocked for
#         staking only, unable to create transaction. (HTTP 500)  expected=no unhandled exception
#
# "No unhandled exception" is the wrong thing to have expected: the harness itself predicted
# this one. Eight decisive 2-of-2 outcomes went to SKIP and the operator's summary named a
# stack-trace class instead of the remedy.
#
# The -13 in that prose was also the wrong site. BOTH codes are real:
#   -4  RPC_WALLET_ERROR, from SendMoney() returning an error string (wallet/wallet.cpp) --
#       this is the one sendtoaddress takes, and the one the daemon actually gave.
#   -13 RPC_WALLET_UNLOCK_NEEDED, thrown by EnsureWalletIsUnlocked() (wallet/rpcwallet.cpp),
#       with DIFFERENT wording: "Wallet is unlocked for staking only."
# ---------------------------------------------------------------------------------------


def _rpc_error(message: str) -> RPCError:
    """An RPCError shaped the way the harness's own client raises them.

    NO `.code` ATTRIBUTE IS SET, and that is the point rather than laziness:
    `chains/base.RPCError` is a bare `class RPCError(Exception)` with no fields, so a real one
    never has one and the code lives inside the message string. A test that attached a `.code`
    would be testing a shape the tree does not produce -- the same defect as the two tests
    above that seeded a `getblockchaininfo.chain` Gridcoin never returns.
    """
    return RPCError(message)


def _raises(exc: BaseException):
    """A StubNode answer that RAISES. The stub returns a value or calls a callable, so an
    exception object handed to it straight would be RETURNED, and the test would pass while
    never exercising the handler it exists for."""
    def answer(*_params):
        raise exc
    return answer


def _grc_setup() -> adaptor_steps.LockSetup:
    """A real LockSetup with distinct keys, built by the real key generator."""
    alice, bob = generate_key(), generate_key()
    script = two_of_two_redeem_script(alice.public_key, bob.public_key)
    return adaptor_steps.LockSetup(
        label="S -- staking-only test", alice=alice, bob=bob,
        lock_script=script, cancel_script=script,
    )


def test_both_rpc_error_spellings_yield_the_same_code():
    """TWO PRODUCERS, TWO SPELLINGS, ONE VALUE. regtest/daemons.py:437 writes `code=-4` and
    chains/base.py:119 writes `(rpc code -4)`. A reader that knew one would silently return
    None for the other, and None means "undetermined" -- so half the surface would have fallen
    through to the message-only branch without anyone noticing."""
    assert adaptor_steps.rpc_code_of(_rpc_error(
        "sendtoaddress: code=-4 message=Error: something (HTTP 500)")) == -4
    assert adaptor_steps.rpc_code_of(_rpc_error("Error: something (rpc code -4)")) == -4
    assert adaptor_steps.rpc_code_of(_rpc_error("Error: something (rpc code -13)")) == -13


def test_an_error_stating_no_code_reads_as_UNDETERMINED_not_zero():
    """rule 17 in a helper: "it does not say" and "it says 0" are different facts and must not
    share a value."""
    assert adaptor_steps.rpc_code_of(_rpc_error("Connection refused")) is None
    assert adaptor_steps.rpc_code_of(_rpc_error("code=0 message=x")) == 0, (
        "and a stated zero really is zero"
    )


def test_the_code_the_daemon_ACTUALLY_returned_is_recognized():
    """-4, verbatim from the operator's daemon on 2026-09-28. A handler that knew only -13
    would have let this through as an unhandled exception, which is precisely what happened."""
    assert adaptor_steps.is_staking_only_refusal(_rpc_error(
        "sendtoaddress: code=-4 message=Error: Wallet unlocked for staking only, "
        "unable to create transaction. (HTTP 500)"))


def test_the_other_site_is_recognized_too():
    """-13 from EnsureWalletIsUnlocked, with its own different wording. Both sites exist and a
    handler that knows one knows half the surface."""
    assert adaptor_steps.is_staking_only_refusal(_rpc_error(
        "signrawtransaction: code=-13 message=Error: Wallet is unlocked for staking only."))


def test_an_unrelated_wallet_error_is_NOT_called_a_staking_only_unlock():
    """WHY THE MESSAGE IS MATCHED AND NOT JUST THE CODE. -4 is RPC_WALLET_ERROR, documented in
    Gridcoin's protocol.h as "Unspecified problem with wallet (key not found etc.)" -- it
    covers far more than this. Keying off -4 alone would tell an operator to unlock a wallet
    that is already unlocked, while the real fault went unnamed."""
    assert not adaptor_steps.is_staking_only_refusal(_rpc_error(
        "sendtoaddress: code=-4 message=Error: Insufficient funds"))
    assert not adaptor_steps.is_staking_only_refusal(_rpc_error(
        "sendtoaddress: code=-4 message=Error: Private key not found"))


def test_a_locked_wallet_is_not_mistaken_for_a_staking_only_one():
    """Different condition, different remedy sentence, and the same -13 code. The message is
    what separates them."""
    assert not adaptor_steps.is_staking_only_refusal(_rpc_error(
        "code=-13 message=Error: Please enter the wallet passphrase with walletpassphrase first."))


def test_the_funding_send_raises_a_NAMED_precondition_not_a_bare_RPCError(console, monkeypatch):
    """THE FIX, ASSERTED BEHAVIORALLY. run_chain() has two handlers: RegtestSetupError is "a
    named precondition failed and the message carries the fix", and a bare Exception becomes
    `FAIL ... expected=no unhandled exception`. A staking-only unlock belongs in the first."""
    run, _ = _run_with(
        console, monkeypatch,
        {"sendtoaddress": _raises(_rpc_error(
            "sendtoaddress: code=-4 message=Error: Wallet unlocked for staking only, "
            "unable to create transaction. (HTTP 500)"))},
        asset="GRC",
    )
    with pytest.raises(RegtestSetupError) as caught:
        adaptor_steps.fund_and_prepare(run, _grc_setup())
    message = str(caught.value)
    assert "STAKING-ONLY" in message
    assert "walletpassphrase" in message, "the remedy names the command"
    assert "third argument" in message, (
        "and the part that actually matters about it. Checked as a SUBSTRING and not as the "
        "exact phrase 'NO third argument' -- that pin went red the moment the sentence was "
        "reworded, which is a test asserting a spelling rather than the fact"
    )
    assert "TESTNET" in message and "mainnet" in message, (
        "the operator has two wallets and only one of them is in scope; the message must say so"
    )
    assert "Nothing was funded" in message, "rule 14: say what did NOT happen, too"


def test_a_real_send_failure_is_still_an_unhandled_exception(console, monkeypatch):
    """THE MUTANT THIS KILLS: catching every RPCError at the send site and calling it
    staking-only. Insufficient funds is a genuine defect in the run's setup and must NOT be
    dressed up as a wallet-lock remedy -- it would send the operator to unlock a wallet that
    is already unlocked while the real fault went unreported."""
    run, _ = _run_with(
        console, monkeypatch,
        {"sendtoaddress": _raises(_rpc_error("sendtoaddress: code=-6 message=Insufficient funds"))},
        asset="GRC",
    )
    with pytest.raises(RPCError, match="Insufficient funds"):
        adaptor_steps.fund_and_prepare(run, _grc_setup())


def test_the_remedy_never_contains_a_passphrase_or_a_command_carrying_one():
    """swap_terminal's standing rule: never move, copy or read back a credential, and never put
    one where argv can be read -- /proc and `ps` are world-readable. The remedy names
    `walletpassphrase` and leaves the secret to the operator to type, with a placeholder."""
    remedy = adaptor_steps.STAKING_ONLY_REMEDY

    # EVERY `walletpassphrase` IN THE REMEDY IS FOLLOWED BY A BRACKETED PLACEHOLDER, checked by
    # pattern rather than by one exact string. This assertion read
    # `assert "<your passphrase>" in remedy` and went red the moment the placeholder was
    # reworded -- which is a test pinning a SPELLING rather than the property it cares about.
    # The property is "no literal ever appears where a secret goes".
    # Anchored to the BACKTICKED command form. An unanchored `walletpassphrase\s+(\S+)` also
    # matched the prose sentence "walletpassphrase refuses an already-unlocked wallet" and
    # reported 'refuses' as a credential -- a false positive, and this file's own rule is that a
    # check nobody trusts is a check somebody deletes.
    occurrences = re.findall(r"`walletpassphrase\s+(\S+)", remedy)
    assert occurrences, "the remedy must name the command the operator has to run, in backticks"
    for argument in occurrences:
        assert argument.startswith("<") and argument.endswith(">"), (
            f"walletpassphrase is followed by {argument!r}, which is not a <placeholder>. A "
            f"literal here would be a credential in source"
        )

    assert "third argument" in remedy, "it says which argument to leave OFF"
    assert "walletlock" in remedy, (
        "and that walletlock comes FIRST -- walletpassphrase refuses an already-unlocked "
        "wallet, so a remedy without it sends the operator into 'Wallet is already unlocked'"
    )
    assert "argv" in remedy, (
        "and it warns that a passphrase on a command line is world-readable through /proc"
    )
    for leak in ("dumpprivkey", "--rpcpassword", "rpcpassword=", "walletpassphrase \""):
        assert leak not in remedy, f"the remedy must not contain {leak!r}"


def test_the_remedy_says_what_RELOCKING_COSTS_on_a_staking_wallet():
    """THE CORRECTION THE OPERATOR HAD TO MAKE FOR ME, 2026-09-28: "GRC is not BTC".

    The first version of this remedy presented `walletlock` + re-unlock as a routine four-step
    sequence. On a Gridcoin STAKING wallet it is not routine, and Gridcoin's own source says so
    in CWallet::ElevateToFull's docstring (wallet/wallet.h:388-405) -- the function that exists
    specifically to REPLACE lock-and-re-unlock:

        "It does NOT lock and re-unlock. That is what this replaces: locking first threw away
         the unlock's deadline, and the re-unlock that followed carried none... It also meant a
         cancelled or mistyped prompt left a staking wallet locked and the node no longer
         staking."

    The operator's daemon reported unlocked_until=1822086129, about a year out. Step 1 of my
    sequence discards that, and a mistyped passphrase at step 2 leaves a staking node not
    staking. Telling someone to do that without naming the cost is a defect in the message, and
    this is the test that keeps it named.
    """
    remedy = adaptor_steps.STAKING_ONLY_REMEDY
    assert "deadline" in remedy.lower(), "it must say relocking discards the unlock's deadline"
    assert "staking" in remedy.lower() and "stops" in remedy.lower(), (
        "and that staking stops while the wallet is locked"
    )
    assert "ElevateToFull" in remedy, (
        "and it must name the primitive that would avoid all of this, so the next reader does "
        "not rediscover it"
    )
    assert "NOTHING CALLS IT" in remedy or "nothing calls it" in remedy.lower(), (
        "AND that it is unreachable -- naming a function the operator cannot invoke, without "
        "saying so, would send them looking for an RPC that does not exist"
    )
    assert "5.5.1.0" in remedy, (
        "and it bounds the claim to the build it was checked against (rule 17): ElevateToFull "
        "was read at master, and whether the operator's v5.5.1.0 carries it is NOT established"
    )


# ---------------------------------------------------------------------------------------
# A DIAGNOSED PRECONDITION IS NOT AN UNEXPECTED FAILURE -- AND MUST STILL EXIT NON-ZERO.
#
# Fixed one level at a time, three times, which is why the exit-code half is tested here
# rather than assumed:
#   3a1af14  a staking-only send stopped arriving as "expected=no unhandled exception"
#   1a0d0e3  the pre-flight probe stopped scoring its own correct diagnosis as FAIL
#   this     the RegtestSetupError handler stopped calling it an unexpected failure
#
# After the first two, the operator's run STILL ended `FAIL=1` with the refusal under
# "unexpected failures", because the handler was the one doing it. And the obvious fix --
# score it XFAIL -- would have made `return 1 if console.counts[FAIL] else 0` return ZERO for
# a run that funded nothing and spent nothing. That is rule 13's twelve cycles printing
# exit_code=0 beside "skipping this cycle", rebuilt by accident while fixing a report.
# ---------------------------------------------------------------------------------------


def test_a_chain_refused_at_a_precondition_has_established_NOTHING():
    """`established()` is what the exit code keys on, so it is asserted directly."""
    refused = adaptor_steps.ChainOutcome(asset="GRC", setup_refusal="staking-only")
    assert not refused.established()

    # Even with every decisive outcome green, a refusal means the run did not happen.
    contradictory = adaptor_steps.ChainOutcome(
        asset="GRC", setup_refusal="staking-only",
        located_by_script_match=OK, spends_in_correct_order=OK,
        refused_when_transposed=OK, refused_without_op0=OK,
    )
    assert not contradictory.established(), (
        "a setup refusal outranks the tallies: those outcomes cannot have been measured"
    )


def test_all_four_decisive_outcomes_OK_is_the_ONLY_thing_that_establishes():
    """MUTATION: drop any one of the four from established() and this goes red on that one."""
    fields = ("located_by_script_match", "spends_in_correct_order",
              "refused_when_transposed", "refused_without_op0")
    green = dict.fromkeys(fields, OK)
    assert adaptor_steps.ChainOutcome(asset="LTC", **green).established()

    for missing in fields:
        one_skipped = {**green, missing: SKIP}
        assert not adaptor_steps.ChainOutcome(asset="LTC", **one_skipped).established(), (
            f"{missing}=SKIP must mean NOT ESTABLISHED -- a SKIP is not a pass, and the "
            f"transposition and missing-OP_0 refusals are the two only a chain can answer"
        )


def test_the_exit_code_is_NON_ZERO_for_a_run_that_was_refused(monkeypatch):
    """THE ONE THAT MAKES THE XFAIL SAFE. console.py's own note says XFAIL must never be used
    to make a run green; this is the assertion that it was not. A refused run has NO FAILs by
    design now, so the old `1 if counts[FAIL] else 0` would return 0 here."""
    entry = _entry_point()
    console, stream = _recording_console()
    refused = adaptor_steps.ChainOutcome(asset="GRC", setup_refusal="unlocked FOR STAKING ONLY")

    code = entry.exit_code_for(console, [refused])

    assert code == 1, "a run refused at a precondition established nothing and must not exit 0"
    assert console.counts[FAIL] == 0, "and it got there with no FAIL tallied -- that is the point"
    printed = stream.getvalue()
    assert "REFUSED AT A PRECONDITION" in printed
    assert "NOT a pass" in printed, "rule 13: 'did nothing' must not read like 'did work'"
    assert "GRC" in printed, "and it names which chain"


def test_the_exit_code_is_NON_ZERO_when_every_decisive_check_merely_SKIPPED(monkeypatch):
    """The other way a run can establish nothing without a single FAIL."""
    entry = _entry_point()
    console, stream = _recording_console()

    code = entry.exit_code_for(console, [adaptor_steps.ChainOutcome(asset="GRC")])

    assert code == 1
    assert "NOT ESTABLISHED" in stream.getvalue()
    assert "SKIP is not a pass" in stream.getvalue()


def test_the_exit_code_is_ZERO_only_when_every_chain_established_its_four(monkeypatch):
    """And the harness can still succeed, or the two tests above would pass with
    `return 1` hard-coded."""
    entry = _entry_point()
    console, stream = _recording_console()
    green = adaptor_steps.ChainOutcome(
        asset="LTC", located_by_script_match=OK, spends_in_correct_order=OK,
        refused_when_transposed=OK, refused_without_op0=OK,
    )

    assert entry.exit_code_for(console, [green]) == 0
    assert "ESTABLISHED on every chain" in stream.getvalue()


def test_one_green_chain_does_not_cover_for_a_refused_one():
    """`--chain both` runs two. A pass on one and a refusal on the other is not a pass."""
    entry = _entry_point()
    console, _ = _recording_console()
    green = adaptor_steps.ChainOutcome(
        asset="LTC", located_by_script_match=OK, spends_in_correct_order=OK,
        refused_when_transposed=OK, refused_without_op0=OK,
    )
    refused = adaptor_steps.ChainOutcome(asset="GRC", setup_refusal="staking-only")

    assert entry.exit_code_for(console, [green, refused]) == 1


def test_a_precondition_refusal_is_NOT_listed_among_unexpected_failures():
    """THE INVARIANT THREE COMMITS WERE ABOUT, AND NOTHING PINNED IT UNTIL NOW.

    tools/mutate.py flipped this outcome from XFAIL back to FAIL on its first real use and the
    whole suite stayed green -- because the exit code is pinned by `setup_refusal` either way,
    and the exit code was all the neighbouring tests checked. What an operator READS was not
    pinned at all, which is the exact half that had to be fixed at three separate levels.

    `console.failures` is what the SUMMARY prints under "unexpected failures, in the order they
    happened", so it is the thing to assert on: a diagnosed precondition must not appear there.
    """
    console, stream = _recording_console()
    outcome = adaptor_steps.ChainOutcome(asset="GRC")

    _entry_point().record_setup_refusal(
        console, "GRC", RegtestSetupError("this wallet is unlocked FOR STAKING ONLY"), outcome,
    )

    assert console.counts[XFAIL] == 1, "the console's own words for XFAIL are 'the harness working'"
    assert console.counts[FAIL] == 0
    assert console.failures == [], (
        "a refusal the harness predicted and explained must not be listed as an UNEXPECTED "
        "failure -- that teaches the reader to distrust the word"
    )
    assert outcome.setup_refusal, "and the chain must still be marked as having established nothing"
    assert not outcome.established()
    assert "STAKING ONLY" in stream.getvalue(), "while still being printed, loudly (rule 14)"


# ---------------------------------------------------------------------------------------
# THE SUPPLIED-KEY BRANCH: is the wallet needed at all?
#
# signrawtransaction picks its keystore on ARGUMENT PRESENCE, before any lock check
# (rawtransaction.cpp:2769-2788). A caller bringing its own keys never reaches
# EnsureWalletIsUnlocked -- so if that branch is reachable on the operator's build, the wallet
# is needed only to move coins to an address the harness holds, and the whole staking-only
# problem shrinks to one manual payment their GUI can make safely.
#
# Probed with a string that CANNOT be a key, because the branch is chosen before the key is
# validated. Nothing is generated and nothing that could hold value crosses the socket.
# ---------------------------------------------------------------------------------------


def test_the_probe_reads_INVALID_PRIVATE_KEY_as_the_route_being_open(console, monkeypatch):
    """The with-keys branch was reached: DecodeSecret rejected our non-key, which it can only
    have done AFTER choosing that branch. EnsureWalletIsUnlocked was never consulted."""
    run, node = _run_with(
        console, monkeypatch,
        {"signrawtransaction": _raises(_rpc_error(
            "signrawtransaction: code=-5 message=Invalid private key"))},
        asset="GRC",
    )
    assert adaptor_steps.probe_supplied_key_signing(run) == "open"
    method, params = node.calls[-1]
    assert method == "signrawtransaction"
    assert params[2] == [adaptor_steps.NOT_A_PRIVATE_KEY], (
        "the third argument is what selects the branch; without it the probe asks nothing"
    )


def test_the_probe_transmits_NOTHING_that_could_ever_be_a_key(console, monkeypatch):
    """THE PROPERTY THAT MAKES THIS PROBE ACCEPTABLE AT ALL. swap_terminal does not move, copy
    or read back keys. A probe that minted a real one to ask a question would be buying its
    answer with the thing that rule protects -- so the string is asserted to be undecodable,
    not merely 'a throwaway'."""
    candidate = adaptor_steps.NOT_A_PRIVATE_KEY
    with pytest.raises(ValueError):
        base58.b58decode_check(candidate)
    assert len(candidate) != 51 and len(candidate) != 52, (
        "and it is not even WIF-shaped, so no reader can mistake it for a near-miss"
    )
    assert "-" in candidate, "it reads as prose, not as an encoding"


def test_the_probe_reads_the_staking_only_refusal_as_the_route_being_closed(console, monkeypatch):
    """If the else branch was taken despite keys being supplied, this build does not have the
    shape read at master and the route is not available."""
    run, _ = _run_with(
        console, monkeypatch,
        {"signrawtransaction": _raises(_rpc_error(
            "signrawtransaction: code=-13 message=Error: Wallet is unlocked for staking only."))},
        asset="GRC",
    )
    assert adaptor_steps.probe_supplied_key_signing(run) == "closed"


@pytest.mark.parametrize("answer", [
    "signrawtransaction: code=-22 message=TX decode failed",
    "signrawtransaction: code=-32601 message=Method not found",
])
def test_any_other_answer_is_UNDETERMINED_and_never_a_verdict(console, monkeypatch, answer):
    """rule 17: their v5.5.1.0 is older than the source this was read from, so a build with a
    different shape must land on 'I could not tell' rather than on either answer."""
    run, _ = _run_with(console, monkeypatch, {"signrawtransaction": _raises(_rpc_error(answer))},
                       asset="GRC")
    assert adaptor_steps.probe_supplied_key_signing(run) == "undetermined"


def test_a_daemon_that_accepts_a_non_key_without_complaining_is_UNDETERMINED(console, monkeypatch):
    """Silence says nothing about which branch ran, and must not be read as success. Without
    this the probe would report 'open' for a stub that answered anything at all."""
    run, _ = _run_with(console, monkeypatch, {"signrawtransaction": {"hex": "00", "complete": False}},
                       asset="GRC")
    assert adaptor_steps.probe_supplied_key_signing(run) == "undetermined"


def test_the_route_text_says_plainly_that_it_is_NOT_BUILT():
    """The worst outcome here is an operator reading a measured possibility as a feature and
    going to look for the flag. Rule 16: this changes how a fund path gets its coins, so it is
    a proposal and has to say so."""
    route = adaptor_steps.SUPPLIED_KEY_ROUTE
    assert "NOT IMPLEMENTED" in route or "is not built" in route.lower()
    assert "proposal" in route.lower()
    assert "yours to decide" in route.lower(), "and whose call it is"
    assert "not available as a flag" in route.lower() or "as a flag today" in route.lower()
