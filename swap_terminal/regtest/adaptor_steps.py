"""The stages that prove -- or refute -- a 2-of-2 P2SH spend on a real chain.

Role: submodule (the stages the root entry point runs; each one asserts and records)
Reads: a local bitcoind / litecoind regtest node, or a Gridcoin TESTNET node, over
      JSON-RPC. Every parameter arrives through daemons.ChainConfig.
Writes: mines blocks and BROADCASTS TRANSACTIONS on a test network. On BTC and LTC it
      may also start a daemon and create a wallet. On GRC it starts nothing, stops
      nothing, and creates no wallet -- see WHY GRIDCOIN IS HANDLED DIFFERENTLY.
Can move funds: YES, on a test network, and structurally nowhere else. The first call
      after every connection is a refusal to proceed unless the daemon ITSELF says it
      is on a test network.
Mainnet-safe: NO, AND IT REFUSES TO BE ASKED. assert_test_network() below is
      unconditional, has no flag, and is never skipped. It asks the daemon; it never
      infers from a port number, a datadir name, or a config file.
Live-safe: no. It broadcasts.

WHAT THIS EXISTS TO SETTLE, AND WHY A SOURCE READING WOULD NOT DO.

`modules/adaptor_swap_scripts.py`'s header says Gridcoin accepts these scripts, and
cites Gridcoin-Research's `src/script.cpp`: OP_CHECKMULTISIG implemented in EvalScript,
TX_MULTISIG a Solver template, the P2SH path recursing into the subscript. That is a
SOURCE READING. It is also the single thing "Verify by behavior, never by reading the
code" exists to refuse, and the operator refused it in those words: a source reading plus
a CLTV measurement is not a spend.

So this harness answers six questions, each as a row-level outcome on a real chain:

  1  a 2-of-2 P2SH funds, and is LOCATED on chain by scriptPubKey match
  2  it SPENDS with both signatures in the correct order
  3  it is REFUSED with the signatures TRANSPOSED
  4  it is REFUSED with the leading OP_0 missing
  5  a spend with nLockTime before T1 is refused and after T1 accepted -- and the
     refusal is sought from CONSENSUS (ask the daemon to mine it) as well as from relay
  6  the same on Gridcoin testnet if it can be driven there, and a stated reason if not

Three and four are the ones only a chain can answer. A transposed pair of signatures
produces a scriptSig of the SAME LENGTH and the SAME SHAPE as a correct one -- no local
assertion can tell them apart, which is why tests/test_adaptor_swap_chain.py says so
rather than trying.

WHY NO BIP65 ACTIVATION HEIGHT IS MINED, WHICH MAKES THIS RUN IN SECONDS WHERE
regtest_htlc_verify.py MINES 2504 BLOCKS.

That harness has to mine past height 1351 on both chains, because CHECKLOCKTIMEVERIFY is
a buried deployment and below its activation height the opcode's script flag is not
applied -- so an early refund is refused only by relay policy and a miner could have
included it.

The 2-of-2 chain does not use CHECKLOCKTIMEVERIFY at all. T1 and T2 are plain nLockTime
fields on transactions that cannot move without both parties, and nLockTime finality has
no deployment: it is checked whenever a transaction is validated for a block. So this
harness mines to coinbase maturity and then only as far as T1 and T2, which it sets a
handful of blocks out. Step 9 is what turns "has no deployment" from a reading into a
measurement, by asking the daemon to MINE an early cancel and recording what it says.

WHY GRIDCOIN IS HANDLED DIFFERENTLY, AND IT IS NOT A SHORTCUT.

Gridcoin has no regtest mode and no local mining: it is proof of stake with research
rewards, and a testnet block arrives about every 90 seconds whether anybody is waiting
or not. Three consequences, all of them named on screen when --chain grc runs:

  - THIS HARNESS NEVER STARTS OR STOPS A GRIDCOIN DAEMON. The operator's testnet daemon
    is a long-running staking wallet; a harness that stopped it would be taking a live
    action nobody asked for, and CLAUDE.md's live-safety rules forbid it outright.
    It must already be answering, and if it is not, the harness says so and stops.
  - `chain == "regtest"` is the wrong question to ask it. assert_test_network() asks
    three different ways and requires at least one POSITIVE statement that the daemon is
    on a test network. An absence of evidence is treated as mainnet.
  - There is nothing to mine with, so every wait is a real wait. T1 is set a couple of
    blocks out and the harness polls with a progress line and an elapsed time, because a
    blinking cursor for three minutes is how an operator ends up pressing Ctrl-C on a
    staking wallet.

And the consensus-versus-relay distinction the BTC and LTC runs draw cannot be drawn on
Gridcoin unless the daemon has `generateblock`. The harness PROBES for it and says which
answer it got rather than assuming either.

NO KEY MATERIAL LEAVES THIS PROCESS. Every key is generated in-process by
regtest/keys.py, controls nothing but test coins the harness itself arranged, and is
never printed, never logged, never written to a file and never put on a command line.
There is no preimage anywhere in this chain -- the adaptor signature replaces the
hashlock, so the most dangerous value in the HTLC path does not exist here at all.
"""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

from chains.base import RPCError
from microfortnights import format_duration
from modules import adaptor_swap_chain as chain
from modules.adaptor_swap_scripts import (
    OP_0,
    two_of_two_p2sh_script,
    two_of_two_redeem_script,
    two_of_two_script_sig,
)
from modules.htlc_rpc import lookup_contract_output
from modules.htlc_spend import SIGHASH_ALL, legacy_sighash, satoshis_to_coins
from modules.htlc_timelock import SECONDS_PER_BLOCK
from regtest import daemons
from regtest.console import FAIL, OK, SKIP, Console
from regtest.daemons import ChainConfig, RegtestSetupError, adapter_for
from regtest.keys import RegtestKey, generate_key
from regtest.txbuild import push_data

TOTAL_STEPS = 10

# How many blocks past the tip T1 and T2 are set. Small on purpose: there is no CLTV
# deployment to clear, so the only reason to put them further out is to leave room for
# the assertions between them. BLOCK COUNTS, never converted to microfortnights -- a
# block is not 1.2096 seconds long, it is however long it took (rule 6).
T1_BLOCKS_AHEAD = 6
T2_BLOCKS_AHEAD = 12

# How much of a test coin to put into each lock. Generous relative to every chain's fee
# floor -- measured 2026-09-28, the GRC minimum for a completable cancel path is
# 0.02 GRC, two fee floors plus dust -- so a failure in this harness is never a failure
# about the amount.
LOCK_COIN = {"BTC": "0.01", "LTC": "0.01", "GRC": "1.0"}

# The extra put into the P2PKH input the harness prepares for itself, over what the lock
# needs, so Tx_lock's own fee comes out of it with room to spare.
FUNDING_HEADROOM_COIN = {"BTC": "0.004", "LTC": "0.004", "GRC": "0.5"}

# How long to wait for a Gridcoin block, and how often to say so. SECONDS, because both
# are compared against time.monotonic() and passed to sleep -- an interface, not a
# report (rule 6). They are printed in microfortnights.
GRC_BLOCK_WAIT_TIMEOUT_SECONDS = float(os.environ.get("ST_ADAPTOR_GRC_BLOCK_TIMEOUT_SECONDS", "1800"))
GRC_POLL_INTERVAL_SECONDS = 10.0
GRC_PROGRESS_INTERVAL_SECONDS = 30.0

# HOW LONG A GRIDCOIN RUN TAKES, DERIVED RATHER THAN RECALLED, AND ANNOUNCED BEFORE IT STARTS.
#
# The number 90 was written as a bare literal in two message strings in this file and nowhere
# else, while `modules/htlc_timelock.SECONDS_PER_BLOCK` has owned the per-chain target interval
# since it was written -- rule 8's shape exactly, and the copies would have drifted the moment
# anyone re-measured Gridcoin's stake interval. It is read off that table now.
#
# expected_grc_blocks() is a FLOOR and says so. The waits a GRC run actually makes, traced
# through this file: lock A costs a block to confirm the funding send, one to confirm Tx_lock
# and one to confirm the redeem; lock B costs the same two, then T2_BLOCKS_AHEAD past its own
# tip to get through the cancel at T1 and the refund after T2, with a block spent confirming
# each of those two spends. Nothing shortens that on a chain that cannot be told to produce a
# block, which is why rule 14 wants it said UP FRONT: an operator who does not know the run is
# half an hour long reads a 30-second gap as a hang, and the thing they would Ctrl-C is a
# staking wallet.
GRC_SECONDS_PER_BLOCK = SECONDS_PER_BLOCK["GRC"]
_LOCK_A_BLOCKS = 3
_LOCK_B_CONFIRMATION_BLOCKS = 4


def expected_grc_blocks() -> int:
    """A FLOOR on the blocks a GRC run waits for -- not a prediction of the run's length.

    A floor because the tip moves while the run works: T1 and T2 are computed from the tip at
    the moment lock B confirms, so every block that arrives during the earlier steps counts
    toward them and the real total is between this and this plus a few.
    """
    return _LOCK_A_BLOCKS + _LOCK_B_CONFIRMATION_BLOCKS + T2_BLOCKS_AHEAD


def expected_grc_seconds() -> float:
    """The floor above in seconds, at Gridcoin's target stake interval."""
    return expected_grc_blocks() * GRC_SECONDS_PER_BLOCK

# The fields, in order, that count as a daemon SAYING it is on a test network. Each is
# (method, key, accepted values). The order is the order they are tried, and every one is
# reported -- a pasted run has to show which answered, because "the daemon said testnet"
# and "nothing contradicted testnet" are different claims and only the first is evidence.
TEST_NETWORK_SIGNALS = (
    ("getblockchaininfo", "testnet", (True,)),
    ("getinfo", "testnet", (True,)),
    ("getmininginfo", "testnet", (True,)),
)

# A POSITIVE STATEMENT OF MAINNET, per signal. Not the complement of the accepted values:
# an ABSENT field says nothing, and `None not in ("test",...)` would read absence as mainnet.
# These are the values that mean the daemon SAID mainnet, and any one of them refuses the run
# even when another signal says testnet -- see step_2's contradiction refusal.
#
# The strings are Gridcoin's own, read off chainparamsbase.cpp:13-15 rather than recalled:
# CBaseChainParams::MAIN is "main", TESTNET is "test", REGTEST is "regtest". `OnTestnet()`
# returns a bool, so False is the mainnet statement for the two `testnet` keys.
MAINNET_STATEMENTS = {
    ("getblockchaininfo", "testnet"): (False,),
    ("getinfo", "testnet"): (False,),
    ("getmininginfo", "testnet"): (False,),
}

# `getblockchaininfo.chain` WAS THE FIRST SIGNAL IN THAT TABLE AND IT COULD NEVER ANSWER.
# It is deleted rather than left to report SKIP forever (rule 2: git history is the archive; a
# probe kept "for reference" is one every reader has to reason about).
#
# Measured 2026-09-28 against the operator's Gridcoin testnet daemon, and then explained by
# reading Gridcoin's own source:
#
#   FAIL  GRC getblockchaininfo.chain: got=(none)  expected=one of ('regtest','test','testnet','signet')
#
# `got=(none)` rather than `got=(none: <RPCError>)` proves the call SUCCEEDED and the response
# simply had no `chain` key. Gridcoin master's getblockchaininfo (src/rpc/blockchain.cpp)
# pushes exactly eight fields -- blocks, in_sync, moneysupply, difficulty{current,target},
# testnet, errors -- and `chain` is not among them at any version. `grep '"chain"'` over that
# whole file finds nothing.
#
# So the harness was asking the right METHOD for a key this family does not have, WHILE THE
# SAME RESPONSE CARRIED THE ANSWER: field 13 of that function is
# `res.pushKV("testnet", OnTestnet())`. Probing `.testnet` instead turns a permanent FAIL into
# a third genuine positive signal, which is strictly better than reporting it as SKIP.
#
# That FAIL was one of the two lines in the run's "unexpected failures" list and part of why it
# exited 1 -- on a daemon that was correctly on testnet and said so twice. A non-problem
# reported as FAIL is rule 14's defect running in the other direction: it trains the reader to
# skim past FAILs, which is exactly how the twelve `exit_code=0` cycles beside "skipping" went
# unnoticed for a deploy.

# Gridcoin's own GUI/daemon environment names, which this tree already reads in
# config.py. Reused rather than a fourth set invented, and there is NO DEFAULT PORT:
# network_target.py records the incident where an unset GRC_RPC_PORT fell through to
# 15715, which is MAINNET and the operator's live staking wallet. An unset port here is a
# named refusal instead.
GRC_ENV = {
    "host": ("ST_ADAPTOR_GRC_RPC_HOST", "GRC_RPC_HOST", "127.0.0.1"),
    "port": ("ST_ADAPTOR_GRC_RPC_PORT", "GRC_RPC_PORT", ""),
    "user": ("ST_ADAPTOR_GRC_RPC_USER", "GRC_RPC_USER", ""),
    "password": ("ST_ADAPTOR_GRC_RPC_PASS", "GRC_RPC_PASS", ""),
}

# Gridcoin MAINNET's rpcport. Named so the refusal below can say "that is the mainnet
# port" rather than printing a number the operator has to recognize.
GRC_MAINNET_PORT = 15715


@dataclass
class ChainOutcome:
    """What this run established on one chain, as the six questions and nothing else.

    Every field starts at SKIP rather than at False, because "not attempted" and "
    attempted and failed" must not render identically (rule 14). A harness that
    initialized these to False would print a clean row of failures for a chain whose
    daemon was never reachable.
    """

    asset: str
    located_by_script_match: str = SKIP
    spends_in_correct_order: str = SKIP
    refused_when_transposed: str = SKIP
    refused_without_op0: str = SKIP
    cancel_refused_before_t1_by_relay: str = SKIP
    cancel_refused_before_t1_by_consensus: str = SKIP
    cancel_accepted_at_t1: str = SKIP
    punish_refused_before_t2: str = SKIP
    refund_accepted_after_cancel: str = SKIP
    predicted_txid_matched: str = SKIP
    notes: list[str] = field(default_factory=list)

    def verdict(self) -> str:
        """The one sentence the operator reads, and it never hedges.

        The four that decide whether a 2-of-2 P2SH is usable on this chain are: it funds
        and is found, it spends in the right order, it is refused in the wrong order, and
        it is refused without the dummy. A green on the first two with a SKIP on either
        refusal is NOT a pass -- it would mean the harness never tested the footgun, and
        the footgun is the thing that produces a well-formed scriptSig verifying nothing.
        """
        decisive = (
            self.located_by_script_match,
            self.spends_in_correct_order,
            self.refused_when_transposed,
            self.refused_without_op0,
        )
        if all(item == OK for item in decisive):
            return (
                "2-of-2 P2SH IS SPENDABLE ON THIS CHAIN: funded, located by scriptPubKey match, spent "
                "with both signatures in key order, and REFUSED both transposed and without the OP_0 "
                "dummy. This is a spend, not a source reading."
            )
        if any(item == FAIL for item in decisive):
            return (
                "2-of-2 P2SH DID NOT WORK ON THIS CHAIN. See the FAIL lines above; nothing here is "
                "softened to make the run green."
            )
        return (
            "NOT ESTABLISHED: one or more of the four decisive checks was never attempted (SKIP). A "
            "SKIP is not a pass -- the transposition and the missing-OP_0 refusals are the two things "
            "only a chain can answer."
        )

    def cancel_verdict(self) -> str:
        """How strong the timelock evidence is, reported separately from the branch verdict.

        Separate because it differs per daemon: a relay refusal says a node will not pass
        the transaction on, and only a refusal to MINE it says a miner could not have
        included it. regtest_htlc_verify.py's step 8c draws the same distinction and it is
        the one that mattered on 2026-09-27.
        """
        if self.cancel_accepted_at_t1 != OK:
            return "nLockTime on the 2-of-2: NOT ESTABLISHED -- the cancel was never accepted at T1"
        if self.cancel_refused_before_t1_by_consensus == OK:
            return (
                "nLockTime on the 2-of-2 is CONSENSUS-enforced: the daemon refused to MINE an early "
                "cancel, and accepted the same transaction at T1"
            )
        if self.cancel_refused_before_t1_by_relay == OK:
            return (
                "nLockTime on the 2-of-2 is RELAY-refused before T1 and accepted at T1. The stronger "
                "claim -- that a miner could not have included it -- was NOT measured; see the "
                "generateblock line above for why"
            )
        return "nLockTime on the 2-of-2: accepted at T1, but the early refusal was never measured"


@dataclass
class Run:
    """One chain's run: where to reach it, what to print to, and what it can do."""

    console: Console
    config: ChainConfig
    wallet: str
    capabilities: dict = field(default_factory=dict)
    spawned: bool = False
    mining_address: str = ""

    @property
    def asset(self) -> str:
        return self.config.asset

    def node(self, wallet: bool = True):
        return adapter_for(self.config, wallet=self.wallet if wallet and self.wallet else "")

    def say(self, text: str) -> None:
        self.console.say(f"{self.asset}: {text}")

    def step(self, number: int, title: str) -> None:
        self.console.step(number, self.asset, title)

    def check(self, label: str, got, expected, outcome: str) -> str:
        return self.console.check(f"{self.asset} {label}", got, expected, outcome)

    def resolve_mining_address(self) -> str:
        """An address in the HARNESS WALLET to mine to, and it must not be a throwaway key.

        MEASURED, first real run of this harness on LTC regtest 2026-09-28: mining 101 blocks
        to `setup_a.alice.address` -- a key generated in this process, which the wallet has
        never heard of -- left `getbalance` at 0.0 and the very next `sendtoaddress` came back
        `code=-6 Insufficient funds`. The chain had 101 blocks of subsidy on it and the wallet
        owned none of them.

        That is why this is a named method rather than an argument at each call site: the two
        address roles in this harness are genuinely different and they read identically. The
        MINING address must be one the wallet can spend, because the wallet is what funds the
        P2PKH input Tx_lock spends. The DESTINATION addresses are throwaway keys this process
        holds, because Tx_lock has to be signed in process.
        """
        if not self.mining_address:
            self.mining_address = self.node().call("getnewaddress")
        return self.mining_address

    @property
    def mines(self) -> bool:
        """Whether this chain can be told to produce a block.

        Read off the capability probe rather than off the asset name, because that is the
        difference that actually matters and a name is a proxy for it. A Gridcoin daemon
        with generateblock would be driven the same way a regtest one is.
        """
        return bool(self.capabilities.get("generatetoaddress"))


def resolve_config(asset: str) -> ChainConfig:
    """The connection parameters for one chain, from the environment.

    BTC and LTC delegate to daemons.resolve_chain_config(), which is the one place the
    ST_REGTEST_* vocabulary lives (rule 8). GRC is built here because that table has no
    Gridcoin row and adding one would tell daemons.py -- whose assert_regtest() demands
    `chain == "regtest"` unconditionally -- about a chain that has no regtest mode. The
    divergence is named at this site; the other site is daemons.CHAIN_DEFAULTS, which a
    reader arrives at from here.

    THERE IS NO DEFAULT GRIDCOIN PORT. network_target.py records why: an unset
    GRC_RPC_PORT once fell through to 15715, which is MAINNET, and the serving path
    polled the operator's live staking wallet on a loop. A silent default is a choice of
    which blockchain real money lives on, and guessing the mainnet one is the worst
    available guess.
    """
    if asset != "GRC":
        return daemons.resolve_chain_config(asset)
    resolved = {}
    for field_name, (override, fallback, default) in GRC_ENV.items():
        value = os.environ.get(override, os.environ.get(fallback, default)).strip()
        if not value:
            raise RegtestSetupError(
                f"GRC: {override} (or {fallback}) is not set, and there is no default. Gridcoin's "
                f"MAINNET rpcport is {GRC_MAINNET_PORT} and its testnet one is conventionally 25715; a "
                f"harness that guessed would be guessing which chain the operator's money is on. Set "
                f"{fallback} for every field: host, port, user and password."
            )
        resolved[field_name] = value
    port = int(resolved["port"])
    if port == GRC_MAINNET_PORT:
        raise RegtestSetupError(
            f"GRC: port {port} is Gridcoin MAINNET's rpcport, which holds the operator's real staking "
            f"balance. REFUSING. This harness broadcasts transactions. Point it at the testnet daemon."
        )
    return ChainConfig(
        asset="GRC",
        daemon_path="gridcoinresearchd",
        cli_path="gridcoinresearch",
        # The datadir is only used by the start, stop and wipe paths, and NONE of them runs
        # for GRC -- step_1 does not call them and there is no --wipe for Gridcoin. It is
        # spelled as the conventional location so a printed config is not full of empty
        # fields, and it is never written to.
        datadir=Path("~/.GridcoinResearch").expanduser(),
        host=resolved["host"],
        port=port,
        rpc_user=resolved["user"],
        rpc_password=resolved["password"],
        conf_name="gridcoinresearch.conf",
        pid_name="gridcoinresearchd.pid",
    )


def step_1_reachable(run: Run) -> None:
    """The daemon answers, and on BTC/LTC start it if it is not up yet.

    GRIDCOIN IS NEVER STARTED HERE. The operator's testnet daemon is a long-running
    staking wallet, and starting or stopping one is a live action nobody asked for
    (CLAUDE.md's live-safety rules: do not stop live services unless the operator asks).
    So on GRC this step only ASKS, and a daemon that is not answering is a named refusal
    with the command in the message rather than something the harness tries to fix.
    """
    run.step(1, "the daemon answers, and we did not start anything we should not have")
    if run.asset == "GRC":
        # WHICH PROBE ANSWERED IS PRINTED, not just that one did.
        #
        # This asked for `uptime` alone until 2026-09-28 and failed against a daemon that was
        # demonstrably up: three atomic swaps had completed through it twenty minutes earlier.
        # Measured -- `gridcoinresearchd -testnet help uptime` answers "unknown command:
        # uptime", while getblockcount on the same port with the same credentials returns
        # 3295729. `uptime` arrived in Bitcoin Core 0.15 and Gridcoin forked long before it.
        #
        # So the refusal message must not say "nothing answered uptime" when uptime is simply
        # a method this family never had -- that sends the operator to check credentials that
        # are fine. daemons.liveness_probe_that_answers() tries each in turn and names the one
        # that worked, because "uptime missed but getblockcount answered" is a fact about the
        # daemon FAMILY and a bare True hides it.
        run.say(
            f"asking {run.config.base_url} for liveness ({', '.join(daemons.LIVENESS_PROBES)}). "
            f"This harness NEVER starts or stops a Gridcoin daemon"
        )
        answered = daemons.liveness_probe_that_answers(run.config)
        if answered is None:
            raise RegtestSetupError(
                f"GRC: none of {list(daemons.LIVENESS_PROBES)} answered at {run.config.base_url}. "
                f"Start your TESTNET daemon yourself -- `gridcoinresearchd -testnet -daemon` -- and "
                f"check that GRC_RPC_USER and GRC_RPC_PASS match the TESTNET "
                f"gridcoinresearch.conf, which on this layout is the one under the `testnet` "
                f"subdirectory and carries DIFFERENT credentials from the mainnet file. This "
                f"harness will not start it for you: a Gridcoin daemon is a staking wallet and "
                f"starting one is a live action."
            )
        run.say(f"GRC: answered by `{answered}`"
                + (" -- this family has no `uptime`, which is expected"
                   if answered != daemons.LIVENESS_PROBES[0] else ""))
        run.check("daemon answers a liveness probe", answered, "any of "
                  f"{list(daemons.LIVENESS_PROBES)}", OK)
        return
    daemons.check_binaries(run.console, run.config)
    run.spawned = daemons.start_daemon(run.console, run.config)
    daemons.wait_for_rpc(run.console, run.config)


def assert_test_network(run: Run) -> dict:
    """THE UNCONDITIONAL REFUSAL. It asks the daemon, and never infers from a port.

    Nothing after this runs unless at least one of TEST_NETWORK_SIGNALS comes back with a
    POSITIVE statement that this daemon is on a test network. The distinction that makes
    this a refusal rather than a check is that an ABSENCE OF EVIDENCE IS TREATED AS
    MAINNET: a daemon that has none of the three fields, or whose calls all fail, is
    refused. "Nothing said mainnet" is not the same claim as "the daemon said testnet",
    and only the second one is evidence.

    daemons.assert_regtest() does the BTC/LTC half of this and is stricter -- it demands
    `chain == "regtest"` exactly -- so it is called for those two rather than reimplemented
    (rule 8). This function exists because Gridcoin has no regtest mode at all, so that
    assertion could never pass there, and the alternative to writing this would be
    weakening the one that already works.
    """
    run.step(2, "WHICH NETWORK -- asked of the daemon, never inferred from a port")
    if run.asset != "GRC":
        run.say("BTC and LTC go through daemons.assert_regtest(), which demands chain=='regtest' exactly")
        return daemons.assert_regtest(run.console, run.config)

    node = run.node(wallet=False)
    run.say(f"port {run.config.port} is not Gridcoin mainnet's {GRC_MAINNET_PORT}, but a PORT IS NOT PROOF")
    run.say(f"asking the daemon itself, {len(TEST_NETWORK_SIGNALS)} ways; at least one must SAY a test network")
    positives: list[str] = []
    mainnet_statements: list[str] = []
    for method, key, accepted in TEST_NETWORK_SIGNALS:
        try:
            answer = node.call(method)
        except RPCError as exc:
            # The method does not exist on this daemon family, or the call failed. SKIP, not
            # FAIL: `uptime` taught this repository the difference on 2026-09-28 and it cost a
            # run against a daemon that was up (see daemons.LIVENESS_PROBES).
            run.check(f"{method}.{key}", f"(none: {exc})", f"one of {accepted}", SKIP)
            continue
        if not isinstance(answer, dict) or key not in answer:
            # THE METHOD ANSWERED AND THE FIELD IS ABSENT. That is a third outcome and it is
            # neither of the other two: nothing failed, and nothing was learned. SKIP says so.
            # Collapsing it into FAIL is what put `getblockchaininfo.chain` in the run's
            # "unexpected failures" list on a correctly-configured testnet daemon.
            shape = "not a dict" if not isinstance(answer, dict) else f"no {key!r} key"
            run.check(f"{method}.{key}", f"(absent: {shape})", f"one of {accepted}", SKIP)
            continue
        value = answer[key]
        if value in MAINNET_STATEMENTS.get((method, key), ()):
            # A POSITIVE STATEMENT OF MAINNET, and it refuses the run below even if another
            # signal says testnet. Recorded as FAIL here so the line is in the summary too.
            run.check(f"{method}.{key}", value, f"one of {accepted}", FAIL)
            mainnet_statements.append(f"{method}.{key}={value!r}")
            continue
        good = value in accepted
        run.check(f"{method}.{key}", value, f"one of {accepted}", OK if good else FAIL)
        if good:
            positives.append(f"{method}.{key}={value!r}")
    if mainnet_statements:
        # A CONTRADICTION REFUSES, AND THIS IS NOT BELT-AND-BRACES. Before this, the aggregate
        # below passed whenever `positives` was non-empty -- so a daemon whose
        # getblockchaininfo said mainnet while its getinfo said testnet=True would have
        # recorded one FAIL, one OK, AND PROCEEDED TO BROADCAST. That is reachable: the three
        # signals read three different code paths, and a half-migrated or proxied daemon can
        # disagree with itself. This harness broadcasts, and Gridcoin mainnet holds the
        # operator's live staking balance, so the two claims "something said testnet" and
        # "nothing said mainnet" are not interchangeable here -- and only the SECOND one makes
        # it safe to send. There is no flag to override this either.
        raise RegtestSetupError(
            f"REFUSING TO RUN: the Gridcoin daemon at {run.config.base_url} POSITIVELY STATED "
            f"MAINNET through {mainnet_statements}"
            + (f", while also stating a test network through {positives}. A daemon that "
               f"contradicts itself about which network it is on is refused rather than "
               f"resolved: this harness broadcasts, and one of the two answers is wrong."
               if positives else
               ". Nothing was built, nothing was funded and nothing was broadcast.")
            + f" Gridcoin MAINNET is port {GRC_MAINNET_PORT} and holds the operator's staking "
              f"balance; this run was pointed at port {run.config.port}."
        )
    if not positives:
        raise RegtestSetupError(
            f"REFUSING TO RUN: the Gridcoin daemon at {run.config.base_url} did not SAY it is on a test "
            f"network through any of {[signal[0] for signal in TEST_NETWORK_SIGNALS]}. An absence of "
            f"evidence is treated as mainnet here, deliberately: this harness broadcasts, and Gridcoin "
            f"mainnet holds the operator's staking balance. There is no flag to override this."
        )
    run.check("test network stated by the daemon", ", ".join(positives), "at least one positive", OK)
    return {}


def step_3_capabilities(run: Run) -> None:
    """Ask the daemon what it can do, and print the answers the funding route depends on.

    THIS IS WHERE THE `fundrawtransaction` QUESTION GETS ANSWERED RATHER THAN ASSUMED.
    `modules/adaptor_swap_chain.select_funding_inputs()` exists because Gridcoin is on the
    pre-0.17 RPC surface -- measured 2026-09-27: no `gettxout`, no
    `signrawtransactionwithkey` -- and nothing in this tree had ever asked whether
    `fundrawtransaction` is there. This asks, and prints the answer whichever way it goes.

    The harness does not USE fundrawtransaction on any chain, because it prepares its own
    P2PKH input and signs Tx_lock itself. The probe is here because the answer is what a
    production funding path needs and the operator asked for it.
    """
    run.step(3, "what this daemon can do -- probed, not assumed")
    if run.asset != "GRC":
        run.wallet = daemons.ensure_wallet(run.console, run.config, run.wallet)
    else:
        run.say("no wallet is created or loaded on Gridcoin; the operator's own wallet is used as it is")
        run.wallet = ""
    run.capabilities = daemons.probe_capabilities(run.console, run.config, run.wallet)
    node = run.node(wallet=False)
    for method in ("fundrawtransaction", "listunspent", "createrawtransaction", "sendrawtransaction",
                   "gettxout", "getrawtransaction", "signrawtransaction", "getblockcount"):
        run.capabilities[method] = daemons.method_exists(node, method)
        run.say(f"capability {method}: {run.capabilities[method]}")
    run.check(
        "listunspent (the route select_funding_inputs() needs)",
        run.capabilities["listunspent"], True,
        OK if run.capabilities["listunspent"] else FAIL,
    )
    run.say(
        f"fundrawtransaction={run.capabilities['fundrawtransaction']} <- the question the brief asked. "
        f"This harness does not use it either way; it builds and signs Tx_lock itself"
    )
    run.say(
        "select_funding_inputs() is exercised in step 5, not here: on a wiped chain nothing is "
        "spendable yet, and a SKIP at that point would say nothing about the selector"
    )


def _exercise_input_selection(run: Run) -> None:
    """Run select_funding_inputs() over the wallet's REAL listunspent rows and say what it chose.

    WHY THIS IS HERE AND NOT ONLY IN A UNIT TEST. `select_funding_inputs()` is the decision
    `fundrawtransaction` would have made, and it exists because Gridcoin may not have that
    RPC. A unit test proves it picks correctly from rows a test wrote; it says nothing about
    whether a real `listunspent` row on a real daemon carries the three fields it reads. Those
    are different claims, and the second is the one that fails on somebody's machine.

    It is DELIBERATELY NOT USED to build anything. Tx_lock is signed in this process, which
    needs an input this process holds the key for, so the harness prepares one with
    `sendtoaddress` instead -- see _send_to_self(). This call is a read: it proves the
    selector can read the daemon's rows, and it prints what it would have chosen.

    Not fatal, and it is called from step 5 AFTER the balance check rather than from step 3.
    It was in step 3 first, and on a wiped chain that is before anything has been mined, so it
    printed `(none: 0 spendable outputs yet)` every run -- a SKIP that says nothing about the
    selector, which is the shape rule 14 calls out: an outcome that cannot distinguish "the
    code is fine" from "the code never ran".
    """
    if not run.capabilities.get("listunspent"):
        run.check("select_funding_inputs() over real listunspent rows", "not attempted: no listunspent",
                  "the selector to read the daemon's own rows", SKIP)
        return
    try:
        rows = run.node().call("listunspent") or []
    except RPCError as exc:
        run.check("select_funding_inputs() over real listunspent rows", f"(none: {exc})",
                  "listunspent to answer", SKIP)
        return
    if not rows:
        run.check(
            "select_funding_inputs() over real listunspent rows", "(none: 0 spendable outputs)",
            "at least one row -- the balance check above just passed, so this is unexpected", SKIP,
        )
        return
    target = 1
    try:
        chosen, total = chain.select_funding_inputs(rows, target)
    except chain.AdaptorChainError as exc:
        run.check("select_funding_inputs() over real listunspent rows", str(exc),
                  "a selection, or a shortfall naming the total", FAIL)
        return
    run.check(
        "select_funding_inputs() over real listunspent rows",
        f"read {len(rows)} row(s), chose {len(chosen)} totaling {total} satoshis for a target of {target}",
        "a selection built from the daemon's own txid/vout/amount fields", OK,
    )


def current_height(run: Run) -> int:
    """The chain tip. Public because the entry point needs it between the two locks.

    A BLOCK HEIGHT, never rendered in microfortnights (rule 6): a block is not 1.2096
    seconds long, it is however long it took.
    """
    return int(run.node(wallet=False).call("getblockcount"))


def _mine(run: Run, count: int) -> None:
    """Produce `count` blocks, or wait for them if this chain cannot be told to.

    Progress every GRC_PROGRESS_INTERVAL_SECONDS with an elapsed time in microfortnights,
    because waiting on Gridcoin blocks is minutes of nothing and a blinking cursor is how
    an operator ends up interrupting a staking wallet (rule 14).
    """
    if count <= 0:
        run.say(f"no blocks needed (count={count})")
        return
    if run.mines:
        address = run.resolve_mining_address()
        run.say(f"mining {count} block(s) to a HARNESS WALLET address (not a throwaway key -- see resolve_mining_address)")
        run.node(wallet=False).call("generatetoaddress", count, address)
        return
    # THE TIP IS READ ONCE. It used to be read twice -- once to compute the target and once
    # inside the message -- and the second reading printed `(tip 51 -> 51)` while the target
    # had been computed from 50. That is rule 14's defect in its purest form: a progress line
    # that contradicts itself, on the one code path an operator watches for half an hour.
    # tests/test_adaptor_regtest_harness.py found it, because a stub that answers a fixed
    # sequence of heights cannot answer a call nobody expected.
    tip = current_height(run)
    target = tip + count
    run.say(
        f"this chain cannot be told to produce a block, so WAITING for {count} more "
        f"(tip {tip} -> {target}). Gridcoin testnet targets {GRC_SECONDS_PER_BLOCK:.0f}s a block "
        f"(modules/htlc_timelock.SECONDS_PER_BLOCK); the timeout is "
        f"{format_duration(GRC_BLOCK_WAIT_TIMEOUT_SECONDS)} and it is set by "
        f"ST_ADAPTOR_GRC_BLOCK_TIMEOUT_SECONDS"
    )
    _wait_for_height(run, target)


def _wait_for_height(run: Run, target: int) -> int:
    started = time.monotonic()
    last_said = 0.0
    while True:
        height = current_height(run)
        if height >= target:
            run.say(f"tip={height} >= {target} after {run.console.elapsed()}")
            return height
        waited = time.monotonic() - started
        if waited >= GRC_BLOCK_WAIT_TIMEOUT_SECONDS:
            raise RegtestSetupError(
                f"{run.asset}: waited {GRC_BLOCK_WAIT_TIMEOUT_SECONDS}s for the tip to reach {target} and it "
                f"is {height}. Nothing was left broadcast that was not already broadcast. Either the chain is "
                f"not advancing or ST_ADAPTOR_GRC_BLOCK_TIMEOUT_SECONDS is too small for a "
                f"{GRC_SECONDS_PER_BLOCK:.0f}s block target."
            )
        if waited - last_said >= GRC_PROGRESS_INTERVAL_SECONDS:
            last_said = waited
            run.say(f"waiting for blocks: tip={height} target={target} elapsed={run.console.elapsed()}")
        time.sleep(GRC_POLL_INTERVAL_SECONDS)


def step_5_spendable_coins(run: Run) -> int:
    """Enough spendable balance to fund two locks, and on BTC/LTC that means mining.

    Only to coinbase maturity plus a little, NOT past a BIP65 activation height -- see the
    module docstring. regtest_htlc_verify.py mines 2504 blocks per chain because CLTV is a
    buried deployment; nLockTime finality is not a deployment at all.
    """
    run.step(5, "spendable balance -- enough for two locks and their fees")
    if run.mines:
        height = current_height(run)
        needed = max(0, daemons.COINBASE_MATURITY_HEIGHT - height)
        run.say(f"tip={height}, coinbase maturity needs {daemons.COINBASE_MATURITY_HEIGHT}; mining {needed}")
        _mine(run, needed)
    balance = run.node().call("getbalance")
    run.check("spendable balance", balance, "enough to fund two locks and their fees",
              OK if float(balance) > 0 else FAIL)
    if run.asset == "GRC":
        _report_gridcoin_lock_state(run)
    _exercise_input_selection(run)
    return current_height(run)


def _report_gridcoin_lock_state(run: Run) -> None:
    """Say what `unlocked_until` is, and say what it CANNOT tell us.

    Measured 2026-09-26 and recorded in README.md: `getwalletinfo` is the only
    wallet-category RPC on Gridcoin that introspects, it returns exactly one lock field,
    and NO RPC reports a staking-only unlock back. So a staking-only unlock -- which
    cannot send -- may be indistinguishable over RPC from a full one that can. The only
    way to learn which is to attempt the send.

    That is why this prints and does not assert. Reporting OK here would be a guess in the
    voice of a measurement about the single condition that decides whether the funding
    send works.
    """
    try:
        info = run.node().call("getwalletinfo")
    except RPCError as exc:
        run.check("GRC wallet lock state", f"(none: {exc})", "getwalletinfo to answer", SKIP)
        return
    unlocked_until = info.get("unlocked_until")
    run.check(
        "GRC wallet unlocked_until", unlocked_until,
        "non-zero, AND a FULL unlock rather than staking-only -- which no Gridcoin RPC reports",
        SKIP,
    )
    run.say(
        "no Gridcoin RPC reports the unlock SCOPE back -- getwalletinfo pushes ten fields and "
        "unlocked_until is the only lock field among them (verified against Gridcoin's source "
        "2026-09-28, src/wallet/rpcwallet.cpp) -- so the value above cannot say whether this "
        "unlock may send. Asking behaviorally instead:"
    )
    scope = probe_wallet_unlock_scope(run)
    run.check(
        "GRC unlock scope, probed by an unsignable transaction that is never broadcast",
        scope, "can-sign (staking-only cannot fund; undetermined means ask again at the send)",
        OK if scope == "can-sign" else SKIP if scope == "undetermined" else FAIL,
    )
    if scope == "staking-only":
        raise RegtestSetupError(
            f"GRC: this wallet is unlocked FOR STAKING ONLY. {STAKING_ONLY_REMEDY}"
        )
    if scope == "locked":
        raise RegtestSetupError(
            f"GRC: this wallet is LOCKED, so it can neither stake nor send. {STAKING_ONLY_REMEDY}"
        )
    if scope == "undetermined":
        run.say(
            "the probe could not tell, which is a real answer and not a pass (rule 17). Carrying "
            "on: the funding send below is the test, and it names the remedy if it refuses"
        )


@dataclass
class LockSetup:
    """One 2-of-2 and the keys that can sign for it. Two of these per run.

    TWO SEPARATE KEY PAIRS PER LOCK, and different pairs between the two locks, for a
    reason that is a diagnostic rather than a protocol requirement: the lock and cancel
    2-of-2s in the real protocol name the same two parties, so their P2SH scriptPubKeys
    are the same bytes, and a scriptPubKey match then cannot say which of the two outputs
    it found. Using distinct keys per lock here makes every match in this harness
    unambiguous, which is what makes outcome 1 mean anything.
    `modules/adaptor_swap_chain.ChainContext`'s docstring carries the full argument.
    """

    label: str
    alice: RegtestKey
    bob: RegtestKey
    lock_script: bytes
    cancel_script: bytes

    @property
    def lock_script_pubkey(self) -> bytes:
        return two_of_two_p2sh_script(self.lock_script)


def step_4_build_scripts(run: Run) -> tuple[LockSetup, LockSetup]:
    """Two independent 2-of-2s: one for the spend and refusal tests, one for the cancel path.

    Two are needed because the happy-path redeem SPENDS the first lock's output, and the
    cancel path has to start from an unspent one. regtest_htlc_verify.py funds two
    contracts for the same reason.
    """
    run.step(4, "two 2-of-2 scripts, with distinct keys so every scriptPubKey match is unambiguous")
    setups = []
    for label in ("A -- spend and refusals", "B -- the cancel path"):
        alice, bob = generate_key(), generate_key()
        lock_script = two_of_two_redeem_script(alice.public_key, bob.public_key)
        # The cancel 2-of-2 uses the SAME two keys, which is what the protocol specifies.
        # It is therefore byte-identical to the lock script, so the cancel output is
        # located by txid and vout rather than by a scriptPubKey match -- said here and on
        # screen because an ambiguous match reported as a found output is worse than none.
        cancel_script = two_of_two_redeem_script(alice.public_key, bob.public_key)
        setup = LockSetup(label=label, alice=alice, bob=bob, lock_script=lock_script, cancel_script=cancel_script)
        run.say(
            f"lock {label}: redeem script {len(lock_script)} bytes, "
            f"P2SH scriptPubKey {setup.lock_script_pubkey.hex()}"
        )
        setups.append(setup)
    same = setups[0].lock_script_pubkey == setups[1].lock_script_pubkey
    run.check("the two locks have different scriptPubKeys", not same, True, FAIL if same else OK)
    run.say(
        "within ONE lock, the cancel 2-of-2 names the same two keys as the lock 2-of-2, exactly as "
        "docs/monero_swap_protocol.md section 2 specifies -- so those two scriptPubKeys ARE identical and "
        "the cancel output is located by txid:vout, never by a script match"
    )
    return setups[0], setups[1]


# THE STAKING-ONLY UNLOCK, WHICH IS THE CONDITION THAT ENDED THE 2026-09-28 GRC RUN, AND THE
# TWO ERROR CODES IT ARRIVES AS. Read off Gridcoin's source, not recalled:
#
#   RPC_WALLET_ERROR         = -4   src/rpc/protocol.h:131
#   RPC_WALLET_UNLOCK_NEEDED = -13  src/rpc/protocol.h:135
#
# BOTH are reachable for this one condition, from two different sites, and a handler that knows
# only one of them misses half the surface:
#
#   -4   src/wallet/wallet.cpp:4325-4330. `SendMoney()` checks IsUnlockedForStakingOnly() and
#        RETURNS AN ERROR STRING -- "Error: Wallet unlocked for staking only, unable to create
#        transaction." -- which rpcwallet.cpp rethrows as RPC_WALLET_ERROR. This is the one
#        `sendtoaddress` takes, and it is what the operator's daemon actually returned:
#        `code=-4 message=Error: Wallet unlocked for staking only, unable to create
#        transaction. (HTTP 500)`. The message string matches wallet.cpp's byte for byte.
#
#        AND `sendtoaddress` NEVER CALLS EnsureWalletIsUnlocked AT ALL, which is why it
#        cannot be the -13 site: its own guard (rpcwallet.cpp:847) tests IsLocked() only,
#        and that is FALSE during a staking-only unlock -- the wallet genuinely is unlocked.
#        It falls through to SendMoneyToDestination and the -4 above.
#   -13  src/wallet/rpcwallet.cpp:102-108. `EnsureWalletIsUnlocked()` THROWS
#        JSONRPCError(RPC_WALLET_UNLOCK_NEEDED, "Error: Wallet is unlocked for staking only.")
#        -- note the different wording, "Wallet IS unlocked" rather than "Wallet unlocked ...
#        unable to create transaction". 36 call sites in .cpp files at master, including
#        signrawtransaction's no-keys branch, fundrawtransaction, and all three of
#        src/rpc/htlc.cpp's (createhtlc's is at :121, BEFORE its own SendMoney at :129, so
#        the HTLC RPCs really do give -13 and not -4).
#
# THE MESSAGE IS THE DISCRIMINATOR, NOT THE CODE, and that is why is_staking_only_refusal()
# below matches on both. The two guards emit DIFFERENT SENTENCES for the same condition, and
# only the wording says which one fired. There is also a second -4 producer for this same
# condition -- CreateTransaction's own staking-only guard at wallet.cpp:3905, which surfaces
# through SendMoney as "Error: Transaction creation failed  " -- so even within -4 the code
# alone does not identify the site. Ordering rules it out here (SendMoney:4325 runs first),
# but the wording rules it out without needing to know that.
#
# BOTH NUMBERS IN THIS BLOCK WERE WRONG WHEN FIRST WRITTEN, 2026-09-28, AND THEY ARE THE
# REASON THE METHOD IS NOW STATED BESIDE THEM. It said "rpcwallet.cpp:102-109" for a function
# that ends at :108, and "43 call sites" -- which was
# `grep -rn EnsureWalletIsUnlocked src --include=*.cpp | wc -l`, i.e. every OCCURRENCE in .cpp
# files including six comments and the definition. A count reported without saying what was
# counted out of what is rule 3's defect, written into a comment whose whole job is to save
# the next reader a grep. The 36 is
# `grep -rn "EnsureWalletIsUnlocked();" src --include=*.cpp` minus the one forward declaration
# at rpcdump.cpp:21, and it distributes as blockchain 10, rpcwallet 10, rpcdump 5, and 3 each
# in voting, rawtransaction, htlc and psgt.
#
# THIS FILE PREVIOUSLY SAID "a staking-only unlock CANNOT send (rpc -13)" AND THE DAEMON GAVE
# -4. The comment was not wrong about -13 existing; it was wrong about which site this path
# hits, which is the kind of near-miss that sends a reader to the wrong function. Both are named
# now, with their sites, and both are matched.
#
# MATCHED ON THE MESSAGE AS WELL AS THE CODE, and deliberately: -4 is
# "Unspecified problem with wallet (key not found etc.)" and covers much more than this. Keying
# the diagnosis off -4 alone would label an unrelated wallet failure a staking-only unlock and
# send the operator to unlock a wallet that is already unlocked.
RPC_WALLET_ERROR = -4
RPC_WALLET_UNLOCK_NEEDED = -13
STAKING_ONLY_RPC_CODES = (RPC_WALLET_ERROR, RPC_WALLET_UNLOCK_NEEDED)
STAKING_ONLY_MESSAGE_MARK = "staking only"

# THE REMEDY, WRITTEN ONCE. Step 5's pre-flight refusal and the funding send's own handler both
# print it, and two copies of an operator instruction is rule 8's bug with a delay on it -- the
# copies agree the day they are written and drift after, and the one that drifts is the one
# somebody follows.
#
# WHAT IS NOT IN HERE, deliberately: any passphrase, and any command that would take one on a
# command line. `walletpassphrase` is named but the operator types it themselves, because argv
# is world-readable through /proc and `ps`, and because this repo never moves, copies or reads
# back a credential.
STAKING_ONLY_REMEDY = (
    "A staking-only unlock cannot CREATE a transaction, and EVERY route around it is closed "
    "too, which is why this refuses rather than trying another way. Read off Gridcoin's source "
    "2026-09-28: `sendtoaddress` goes through SendMoney(), which checks "
    "IsUnlockedForStakingOnly() and returns an error (src/wallet/wallet.cpp:4325, the -4 path); "
    "and `signrawtransaction` with no keys of its own goes through EnsureWalletIsUnlocked(), "
    "which throws (src/wallet/rpcwallet.cpp:102, the -13 path). The third route -- handing "
    "signrawtransaction the wallet's own private key -- would mean reading a key back out, "
    "which this repository does not do for any reason. "
    "\n\n"
    "CHANGING A WALLET'S UNLOCK SCOPE IS THE OPERATOR'S DECISION, NOT THIS HARNESS'S, so here "
    "is the sequence rather than an action. On the TESTNET daemon ONLY -- your mainnet wallet "
    "is a different daemon on a different port and is not involved:\n"
    "  1. `walletlock`                       <- REQUIRED FIRST. walletpassphrase refuses an "
    "already-unlocked wallet: 'Error: Wallet is already unlocked, use walletlock first if need "
    "to change unlock settings.' This STOPS TESTNET STAKING until step 4.\n"
    "  2. `walletpassphrase <passphrase> 5400`   <- NO third argument. The third argument is "
    "`stakingonly` and it defaults to false, which is the full unlock this needs. 5400s covers "
    "a GRC run, which waits about half an hour for real blocks.\n"
    "  3. re-run this harness.\n"
    "  4. `walletlock`, then `walletpassphrase <passphrase> <seconds> true` to put the "
    "staking-only restriction back. Gridcoin's own help: 'The restriction belongs to the "
    "unlock, so locking clears it and the next unlock states its own.' There is no RPC that "
    "narrows a full unlock in place -- RestrictToStakingOnly() exists but is reachable only "
    "through the GUI (src/wallet/interfaces.cpp), so the TESTNET GUI wallet can also do all of "
    "this.\n\n"
    "PREFER THE GUI IF YOU HAVE IT OPEN. A passphrase on a `gridcoinresearch-cli` command line "
    "lands in argv, which is world-readable through /proc and `ps`, and in your shell history. "
    "This harness never handles a passphrase and never will.\n\n"
    "Nothing was funded, signed or broadcast."
)

# The nTime the probe transaction carries on Gridcoin, which serializes nTime in its prefix.
# A FIXED value, not time.time(): a probe built twice must be the same bytes, and this one is
# never broadcast so no node ever judges its timestamp. 1 rather than 0 because 0 reads as
# "unset" to anyone debugging a hex dump.
PROBE_NTIME = 1


# THE RPC CODE IS IN THE MESSAGE STRING, IN TWO DIFFERENT SPELLINGS, AND `RPCError` CARRIES NO
# `.code` ATTRIBUTE AT ALL. Measured by reading both producers:
#
#   regtest/daemons.py:437     f"{method}: code={code} message={message} (HTTP {status})"
#   chains/base.py:119         f"{message} (rpc code {code})"
#
# `chains/base.RPCError` is a bare `class RPCError(Exception)` with no fields, so
# `getattr(exc, "code", None)` is ALWAYS None on a real one -- a first version of this function
# compared it against -13 and that branch could never have run. The operator's 2026-09-28 run
# went through the first spelling: `sendtoaddress: code=-4 message=Error: Wallet unlocked for
# staking only, unable to create transaction. (HTTP 500)`.
#
# Two spellings of one value is rule 8's shape and the right fix is a typed error carrying an
# int -- which is already named work from the burn-proofing pass ("rpc_result() should raise a
# typed error carrying code as int; atomic_ltc_client.rpc_call is a third behavior"). That is a
# change across the client surface and is NOT made from inside this harness. This parser reads
# both spellings, in ONE place, and says so; it is the reader, not a third spelling.
_RPC_CODE_SPELLINGS = (
    re.compile(r"\bcode=(-?\d+)"),
    re.compile(r"\(rpc code (-?\d+)\)"),
)


def rpc_code_of(exc: BaseException) -> int | None:
    """The JSON-RPC code inside an RPCError's message, or None if it does not carry one.

    None means "this error does not state a code", which is a THIRD answer and not a zero
    (rule 17: "I could not tell" and "it is not that" must never be the same value). A caller
    that needs certainty about the code must treat None as undetermined.

    Prefers an explicit `.code` attribute if one ever appears, so this keeps working unchanged
    the day the typed-error work lands.
    """
    typed = getattr(exc, "code", None)
    if isinstance(typed, int):
        return typed
    text = str(exc)
    for pattern in _RPC_CODE_SPELLINGS:
        found = pattern.search(text)
        if found:
            return int(found.group(1))
    return None


def is_staking_only_refusal(exc: RPCError) -> bool:
    """Is this RPCError the wallet refusing because its unlock is staking-only?

    MESSAGE AND CODE, and the message is the load-bearing half. -4 is RPC_WALLET_ERROR,
    documented in Gridcoin's protocol.h as "Unspecified problem with wallet (key not found
    etc.)" -- keying off the code alone would label an insufficient-funds or missing-key
    failure a staking-only unlock and send the operator to unlock a wallet that is already
    unlocked, while the real fault went unnamed.

    An error with NO code stated is judged on its message alone rather than refused: the
    message is the specific half, and a client that omits the code has not said this is a
    different condition.

    Returns a BOOL rather than raising, because the caller knows what it was trying to do and
    therefore what the remedy is -- rule 10: the decision is a function, the message belongs to
    the step that called it.
    """
    if STAKING_ONLY_MESSAGE_MARK not in str(exc).lower():
        return False
    code = rpc_code_of(exc)
    return code is None or code in STAKING_ONLY_RPC_CODES


def probe_wallet_unlock_scope(run: Run) -> str:
    """"staking-only", "can-sign", "locked", or "undetermined" -- asked BEHAVIORALLY.

    WHY THIS EXISTS, AND WHY IT IS NOT A STATE QUERY. Verified against Gridcoin's source
    2026-09-28: `getwalletinfo` pushes ten fields and the only lock field among them is
    `unlocked_until` (src/wallet/rpcwallet.cpp), and NOTHING in that whole file exposes the
    unlock SCOPE -- the single mention of IsUnlockedForStakingOnly() in it is the guard at line
    106 that refuses. So this repository's standing note that "no Gridcoin RPC reports a
    staking-only unlock back" is CORRECT for every state query, and it is now checked rather
    than believed.

    But a state query is not the only way to ask. `signrawtransaction`'s no-keys branch calls
    EnsureWalletIsUnlocked() BEFORE it attempts any signing (src/rpc/rawtransaction.cpp:2785-
    2787), so handing it a decodable transaction the wallet cannot sign separates the three
    states and moves NOTHING:

        staking-only   -13 "Error: Wallet is unlocked for staking only."
        locked         -13 "Error: Please enter the wallet passphrase ... first."
        can sign       returns {hex, complete: false} -- it tried, found no key, said so

    It writes no key, creates no transaction, broadcasts nothing and touches no balance. That
    matters more than the diagnosis: the alternative pre-flight probes all have a cost --
    `getnewaddress` writes a key into the operator's wallet.dat, and the funding send itself is
    the thing we are trying not to have to do blind.

    ADVISORY, NEVER AUTHORITATIVE. "undetermined" is a real answer and the caller must treat it
    as one (rule 17: "I could not tell" and "it is wrong" are not the same value). The operator's
    build is older than the source read above -- it answers False for
    signrawtransactionwithkey, which master has -- so the ORDER of the check inside
    signrawtransaction is a source reading about master, not a measurement of their daemon. The
    funding send in step 6 remains the real test, exactly as it was.
    """
    # A transaction the wallet CANNOT sign, spending an outpoint that does not exist, paying an
    # OP_RETURN. Three properties, each deliberate: it decodes (so signrawtransaction reaches
    # the unlock check rather than throwing a parse error first), the wallet holds no key for
    # its input (so a full unlock returns complete:false instead of a signed transaction), and
    # nothing here is ever broadcast -- only signrawtransaction is called, never
    # sendrawtransaction.
    #
    # OP_RETURN (0x6a) as the destination rather than a P2PKH to a real address: if this
    # transaction ever escaped this function it would be unspendable by anyone, which is the
    # safest thing a probe's output can be. The value is a round 1.0 coin so a reader who finds
    # the hex in a log can tell at a glance it is a fixture and not a payment.
    try:
        probe = chain.build_unsigned(
            asset=run.asset,
            # NOT all zeros: that is the COINBASE outpoint, and chain.Outpoint refuses it --
            # "A transaction claiming to spend it is a coinbase, and no swap transaction is one".
            # Found by that refusal firing on the first version of this probe, which is the
            # guard working. 0xff throughout instead: a txid that cannot plausibly exist and
            # reads as a fixture in a hex dump.
            spends=chain.Outpoint(txid="ff" * 32, vout=0, value_satoshis=100_000_000),
            outputs=[(99_000_000, bytes([0x6A]))],
            locktime=0,
            ntime=PROBE_NTIME if run.asset == "GRC" else None,
        )
        probe_hex = probe.serialize().hex()
    except Exception as exc:  # noqa: BLE001 -- checked: this is a PROBE and a failure to BUILD it means "could not ask", which is the "undetermined" answer this function is documented to return. It never becomes a verdict about the wallet, the funding send remains the real test either way, and it is reported on screen rather than swallowed.
        run.say(f"unlock-scope probe could not be built ({type(exc).__name__}: {exc}); treating as undetermined")
        return "undetermined"
    try:
        run.node().call("signrawtransaction", probe_hex)
    except RPCError as exc:
        if is_staking_only_refusal(exc):
            return "staking-only"
        # "Please enter the wallet passphrase" rather than the code, for the same reason
        # is_staking_only_refusal() matches on the message: -13 is shared between a locked
        # wallet and a staking-only one, and the wording is what separates them.
        if "passphrase" in str(exc).lower() and rpc_code_of(exc) in (None, RPC_WALLET_UNLOCK_NEEDED):
            return "locked"
        run.say(f"unlock-scope probe answered something else ({exc}); treating as undetermined")
        return "undetermined"
    return "can-sign"


def _send_to_self(run: Run, key: RegtestKey, coin: str) -> chain.Outpoint:
    """Put a known amount into a P2PKH output THIS PROCESS holds the key for.

    WHY THE HARNESS DOES THIS INSTEAD OF FUNDING THE 2-of-2 DIRECTLY. Tx_lock has to be
    built, signed and HELD unbroadcast -- that is FINDING 1 in
    modules/adaptor_swap_chain.py and the whole reason `sendtoaddress` cannot build it. To
    sign Tx_lock in-process the harness needs an input it holds the key for, so it makes
    one: `sendtoaddress` to an address generated here, confirm it, and then Tx_lock spends
    that.

    `sendtoaddress` is used for THIS step and only this step, where it is exactly the right
    tool: it is the funder's ordinary wallet spend, it has no counterparty, and nothing
    depends on its txid being known in advance.
    """
    address = key.address
    run.say(f"sendtoaddress {coin} {run.asset} to an address this process holds the key for")
    try:
        txid = run.node().call("sendtoaddress", address, float(coin))
    except RPCError as exc:
        # A DIAGNOSED CONDITION IS NOT AN UNHANDLED EXCEPTION. On 2026-09-28 this raised
        # straight past run_chain()'s broad catch and the operator's summary read
        #
        #   FAIL  GRC run: got=RPCError: sendtoaddress: code=-4 ...  expected=no unhandled exception
        #
        # for a condition the harness had NAMED IN PROSE one screen earlier. "No unhandled
        # exception" is the wrong thing to have expected: this exception was predicted. Raising
        # RegtestSetupError instead routes it to run_chain()'s named-precondition branch, whose
        # own comment already says "the message carries the fix" -- so the remedy reaches the
        # summary instead of a stack-trace class. Rule 14: a skipped run and a broken one must
        # not report the same way.
        if is_staking_only_refusal(exc):
            raise RegtestSetupError(
                f"GRC: the funding send was refused because this wallet's unlock is "
                f"STAKING-ONLY. The daemon's own words: {exc}. {STAKING_ONLY_REMEDY}"
            ) from exc
        raise
    run.check("funding send accepted", txid, "a txid", OK if txid else FAIL)
    _mine(run, 1)
    found = lookup_contract_output(lambda method, params: run.node().call(method, *params), txid, 0)
    # `vout` is NOT assumed: sendtoaddress puts the payment and the change in whichever
    # order it likes, and an assumed 0 would have the harness signing over the change.
    vout = _vout_paying(run, txid, key.p2pkh_script)
    value = _value_of(run, txid, vout)
    run.say(f"found the funding output at {txid}:{vout} worth {satoshis_to_coins(value)} (route: {found.route})")
    return chain.Outpoint(txid=txid, vout=vout, value_satoshis=value)


def _decoded(run: Run, txid: str) -> dict:
    """The daemon's own decoding of a transaction, by whichever route answers.

    The DAEMON decodes it rather than htlc_spend.parse_transaction(), for the reason
    modules/htlc_rpc.lookup_contract_output() gives: a default Bitcoin Core 28.1 or
    Litecoin 0.21.4 wallet holds coins in bech32 P2WPKH, so a wallet funding transaction
    carries a segwit serialization that parser cannot read and says so. The 2-of-2 spends
    this harness builds have no witness and are parsed in process; a WALLET transaction is
    not.
    """
    node = run.node()
    attempts = []
    for params in ([txid, True], [txid]):
        try:
            answer = node.call("getrawtransaction", *params)
        except RPCError as exc:
            attempts.append(f"getrawtransaction{params[1:]}: {exc}")
            continue
        return answer if isinstance(answer, dict) else node.call("decoderawtransaction", answer)
    try:
        record = node.call("gettransaction", txid)
        return node.call("decoderawtransaction", record["hex"])
    except (RPCError, KeyError) as exc:
        attempts.append(f"gettransaction: {exc}")
    # THE BLOCK-HASH ROUTE, added after the first real LTC run, 2026-09-28. Without -txindex,
    # `getrawtransaction` searches only the mempool and `gettransaction` knows only WALLET
    # transactions -- and this harness's spends pay throwaway keys the wallet has never heard
    # of, so a CONFIRMED spend of a 2-of-2 is invisible to both. The daemon's own error text
    # asks for exactly this: "provide a block hash to enable blockchain transaction queries".
    # It is the tip's hash because everything read back here was mined one block ago; a
    # transaction further back would need its own block hash and none is.
    try:
        best = node.call("getbestblockhash")
        return node.call("getrawtransaction", txid, True, best)
    except RPCError as exc:
        attempts.append(f"getrawtransaction with the tip's block hash: {exc}")
    raise RegtestSetupError(
        f"{run.asset}: could not read {txid} back. Tried, in order: {'; '.join(attempts)}. This does NOT "
        f"enable -txindex, because doing so on an existing datadir forces a full reindex"
    )


def _vout_paying(run: Run, txid: str, script_pubkey: bytes) -> int:
    wanted = script_pubkey.hex()
    decoded = _decoded(run, txid)
    for entry in decoded.get("vout", []):
        if entry.get("scriptPubKey", {}).get("hex") == wanted:
            return int(entry["n"])
    raise RegtestSetupError(
        f"{run.asset}: no output of {txid} pays scriptPubKey {wanted}. The outputs present are "
        f"{[entry.get('scriptPubKey', {}).get('hex') for entry in decoded.get('vout', [])] or '(none)'}"
    )


def _corroborate_payout(run: Run, txid: str, script_pubkey: bytes, label: str) -> None:
    """Read a single-output spend back and check who it paid. NEVER FATAL.

    The MEASUREMENT is the broadcast being accepted: `sendrawtransaction` runs the full script
    interpreter, so acceptance already says the 2-of-2 verified. This is corroboration that
    the coins went where the transaction said, and it is scored SKIP rather than FAIL when the
    node cannot answer -- because the thing that would make it unanswerable is a node
    configuration (no -txindex, a wallet that does not know these keys), not a fact about the
    script.

    Scoring a lookup failure as FAIL here would have been the worse mistake, and it is the one
    the first real LTC run made: every decisive outcome was green and the run still exited 1,
    on a read-back of a transaction the daemon had already accepted and mined.
    """
    try:
        found = lookup_contract_output(lambda method, params: run.node().call(method, *params), txid, 0)
    except (LookupError, RegtestSetupError, RPCError) as exc:
        run.check(f"{label} paid the expected key", f"(none: could not read it back: {exc})",
                  script_pubkey.hex(), SKIP)
        return
    run.check(f"{label} paid the expected key", found.script_pubkey_hex, script_pubkey.hex(),
              OK if found.script_pubkey_hex == script_pubkey.hex() else FAIL)


def _value_of(run: Run, txid: str, vout: int) -> int:
    decoded = _decoded(run, txid)
    for entry in decoded.get("vout", []):
        if int(entry["n"]) == vout:
            return int((Decimal(str(entry["value"])) * Decimal(100_000_000)).to_integral_value())
    raise RegtestSetupError(f"{run.asset}: {txid} has no output {vout}")


def _sign_p2pkh(key: RegtestKey, digest: bytes) -> bytes:
    """A DER signature with the SIGHASH byte, from a key this process generated.

    regtest/keys.RegtestKey.sign_digest() is low-S and deterministic. Low-S is not
    cosmetic: Core has enforced it as a standardness rule since 0.11, so a high-S
    signature is refused from the mempool with wording that looks exactly like the script
    being wrong -- on a harness whose entire job is to say whether the script is wrong.
    """
    return push_data(key.sign_digest(digest) + bytes([SIGHASH_ALL])) + push_data(key.public_key)


@dataclass
class BuiltChain:
    """One lock and the four transactions built against it, all still unbroadcast.

    `lock_raw_hex` and `lock_txid` are the point of this object: the txid is computed from
    bytes that have not been sent, and every other transaction here references it. That is
    FINDING 1 in modules/adaptor_swap_chain.py -- step 0 of the protocol exchanges
    signatures on transactions that spend Tx_lock BEFORE Tx_lock is broadcast, and
    `sendtoaddress` cannot produce that state at all.
    """

    setup: LockSetup
    context: chain.ChainContext
    lock_raw_hex: str
    lock_txid: str
    lock_vout: int
    lock_satoshis: int
    lock_fee: int
    redeem: chain.ChainTransaction
    cancel: chain.ChainTransaction
    t1: int
    t2: int


def _signatures_in_key_order(setup: LockSetup, digest: bytes) -> tuple[bytes, bytes]:
    """(Alice's, Bob's) -- the SAME order the keys appear in the redeem script.

    two_of_two_redeem_script(alice, bob) puts Alice first, so Alice's signature goes first.
    This function exists so that order is stated once; the transposition test below calls
    it and swaps the result, which makes the two cases provably the same two signatures in
    two orders rather than two independently built scriptSigs.
    """
    return (
        setup.alice.sign_digest(digest) + bytes([SIGHASH_ALL]),
        setup.bob.sign_digest(digest) + bytes([SIGHASH_ALL]),
    )


def step_6_build_and_hold(run: Run, setup: LockSetup, funding: chain.Outpoint, tip: int) -> BuiltChain:
    """Build Tx_lock and the whole four-transaction chain, and broadcast NOTHING.

    THIS IS THE STEP THAT ANSWERS THE `sendtoaddress` QUESTION AS A MEASUREMENT. Every
    txid below is printed before a single byte reaches the network, and step 7 compares the
    one predicted for Tx_lock against the one the daemon hands back.

    The order inside this step is forced and it is FINDING 2: Tx_cancel's txid depends on
    Tx_cancel's signatures, because a legacy txid covers the scriptSigs. So the cancel is
    SIGNED here, its txid taken, and only then can Tx_refund and Tx_punish be built. The
    design document's step 0 says these are exchanged "in one round"; on a legacy chain
    they cannot be, and that is a correction to the document rather than to this code.
    """
    run.step(6, "build Tx_lock and all four spends -- and broadcast NOTHING")
    lock_coin = LOCK_COIN[run.asset]
    context = chain.ChainContext(
        asset=run.asset,
        lock_redeem_script=setup.lock_script,
        cancel_redeem_script=setup.cancel_script,
        # Both parties are played by this process, so "Alice's destination" is just an
        # address this harness holds. The point of the run is the SCRIPT, not the payee.
        alice_script=setup.alice.p2pkh_script,
        bob_script=setup.bob.p2pkh_script,
        ntime=int(time.time()) if run.asset == "GRC" else None,
    )
    minimum = chain.minimum_lock_value_satoshis(context)
    run.say(
        f"minimum lock value on {run.asset} is {minimum} satoshis ({satoshis_to_coins(minimum)}) "
        f"<- two fee floors plus a non-dust output, because the cancel path is TWO hops"
    )
    unsigned_lock, lock_vout, lock_fee = chain.two_of_two_funding_transaction(
        context, funding, chain.p2pkh_script_sig_upper_bound(setup.alice.public_key), None, None
    )
    lock_script_sig = _sign_p2pkh(setup.alice, _p2pkh_sighash(unsigned_lock, setup.alice))
    lock_raw = unsigned_lock.serialize({0: lock_script_sig})
    lock_txid = chain.predicted_txid(unsigned_lock, {0: lock_script_sig})
    lock_satoshis = unsigned_lock.outputs[lock_vout][0]
    run.check(
        "Tx_lock built, signed and HELD (not broadcast)", lock_txid,
        "a txid computed from bytes that have not been sent", OK,
    )
    run.say(
        f"Tx_lock pays {satoshis_to_coins(lock_satoshis)} to the 2-of-2 P2SH, fee {lock_fee} satoshis, "
        f"{len(lock_raw)} bytes. Asked for {lock_coin}; the whole prepared input went in less the fee"
    )
    run.check("lock value is at or above the minimum", lock_satoshis >= minimum, True,
              OK if lock_satoshis >= minimum else FAIL)

    lock_outpoint = chain.Outpoint(txid=lock_txid, vout=lock_vout, value_satoshis=lock_satoshis)
    t1, t2 = tip + T1_BLOCKS_AHEAD, tip + T2_BLOCKS_AHEAD
    chain.assert_timelocks_ordered(t1, t2)
    redeem = chain.build_redeem(context, lock_outpoint)
    cancel = chain.build_cancel(context, lock_outpoint, t1)
    run.say(f"T1={t1} T2={t2} (BLOCK HEIGHTS, never microfortnights -- tip was {tip})")
    run.say(
        f"Tx_redeem: {redeem.output_satoshis} sat to Alice, fee {redeem.fee_satoshis}, "
        f"size bound {redeem.unsigned_size_bound}, predicted txid {chain.predicted_txid(redeem.parsed)} "
        f"(UNSIGNED -- signing changes it)"
    )
    run.say(
        f"Tx_cancel: {cancel.output_satoshis} sat to the second 2-of-2, fee {cancel.fee_satoshis}, "
        f"nLockTime {cancel.locktime}"
    )
    # FINDING 2 demonstrated on screen: sign the cancel, and its txid is not the one the
    # unsigned bytes predicted. The refund and the punish can only be built after this.
    cancel_sigs = _signatures_in_key_order(setup, cancel.digest)
    _, signed_cancel_txid = chain.assemble(cancel, *cancel_sigs)
    run.check(
        "signing Tx_cancel CHANGES its txid (FINDING 2)",
        f"unsigned={chain.predicted_txid(cancel.parsed)[:16]}.. signed={signed_cancel_txid[:16]}..",
        "two different txids, which is why step 0 needs TWO rounds and not one",
        OK if signed_cancel_txid != chain.predicted_txid(cancel.parsed) else FAIL,
    )
    cancel_output = chain.Outpoint(txid=signed_cancel_txid, vout=0, value_satoshis=cancel.output_satoshis)
    refund = chain.build_refund(context, cancel_output)
    punish = chain.build_punish(context, cancel_output, t2)
    chain.assert_spends(refund.parsed, signed_cancel_txid, 0)
    chain.assert_spends(punish.parsed, signed_cancel_txid, 0)
    run.check(
        "Tx_refund and Tx_punish bind to the SIGNED cancel's outpoint",
        f"{signed_cancel_txid[:16]}..:0", "the assembled cancel, not the unsigned one", OK,
    )
    run.say(
        f"Tx_refund: {refund.output_satoshis} sat to Bob, fee {refund.fee_satoshis}, no locktime. "
        f"Tx_punish: {punish.output_satoshis} sat to Alice, nLockTime {punish.locktime}"
    )
    run.say(
        f"total paid in fees down the cancel path: {cancel.fee_satoshis + refund.fee_satoshis} satoshis "
        f"over two hops, against {redeem.fee_satoshis} for the one-hop redeem"
    )
    return BuiltChain(
        setup=setup, context=context, lock_raw_hex=lock_raw.hex(), lock_txid=lock_txid,
        lock_vout=lock_vout, lock_satoshis=lock_satoshis, lock_fee=lock_fee,
        redeem=redeem, cancel=cancel, t1=t1, t2=t2,
    )


def _p2pkh_sighash(parsed, key: RegtestKey) -> bytes:
    """The digest for a P2PKH input: the script code is the P2PKH scriptPubKey itself.

    NOT a redeem script -- a P2PKH input has none. The 2-of-2 spends sign over their redeem
    script (adaptor_swap_scripts.two_of_two_sighash), and Tx_lock's own input does not,
    which is the one place in this harness where the two differ. Signing over the wrong one
    produces a signature that verifies against nothing and a node that reports a bare
    script failure, so it is a named function rather than an expression at the call site.
    """
    return legacy_sighash(parsed, 0, key.p2pkh_script)


def _broadcast(run: Run, raw_hex: str, label: str) -> tuple[str | None, str]:
    """sendrawtransaction, returning (txid or None, the daemon's own words).

    Both halves matter and this is why it is not a bare call. A refusal is a RESULT here --
    steps 8 and 9 exist to collect them -- so the daemon's message is returned rather than
    raised, and the caller decides whether that particular refusal is the expected one.
    Discarding the message would leave the harness unable to tell a script failure from a
    fee failure from a missing input, which are three different findings that all read as
    "the transaction was refused".
    """
    run.say(f"sendrawtransaction: {label} ({len(bytes.fromhex(raw_hex))} bytes)")
    try:
        txid = run.node().call("sendrawtransaction", raw_hex)
    except RPCError as exc:
        return None, str(exc)
    return txid, "accepted"


def step_7_broadcast_lock(run: Run, built: BuiltChain, outcome: ChainOutcome) -> None:
    """Broadcast Tx_lock, and check the predicted txid against the daemon's.

    OUTCOME 1, in two halves. The txid check is what turns predicted_txid() from an
    expression into a measurement: a local test can only compare that function against
    another expression of the same rule, and this compares it against a chain. Then the
    funded output is LOCATED by scriptPubKey match, through
    modules/htlc_rpc.lookup_contract_output() -- the real product code, four routes -- and
    the bytes it reports are compared against the P2SH script built in step 5.

    The match is on the scriptPubKey HEX and not on a rendered address, for the reason
    atomic_htlc_scripts.p2sh_script_for() records: Core removed `addresses` from decoded
    outputs in 22.0, and Bitcoin and Litecoin do not even agree on the base58 P2SH version
    byte. An address comparison is the wrong key in two independent ways.
    """
    run.step(7, "broadcast Tx_lock, then FIND the 2-of-2 output by scriptPubKey match")
    txid, message = _broadcast(run, built.lock_raw_hex, f"Tx_lock for lock {built.setup.label}")
    if txid is None:
        run.check("Tx_lock accepted", f"REFUSED: {message}", "a txid", FAIL)
        outcome.notes.append(f"Tx_lock refused for lock {built.setup.label}: {message}")
        raise RegtestSetupError(f"{run.asset}: Tx_lock was refused, so nothing downstream can be measured: {message}")
    matched = txid == built.lock_txid
    run.check(
        "the txid PREDICTED before broadcast equals the daemon's",
        f"predicted={built.lock_txid} daemon={txid}", "the same txid", OK if matched else FAIL,
    )
    outcome.predicted_txid_matched = OK if matched else FAIL
    _mine(run, 1)
    found = lookup_contract_output(lambda method, params: run.node().call(method, *params),
                                   built.lock_txid, built.lock_vout)
    expected_hex = built.setup.lock_script_pubkey.hex()
    good = found.script_pubkey_hex == expected_hex
    run.check(
        "the funded output's scriptPubKey is the 2-of-2 P2SH",
        found.script_pubkey_hex, expected_hex, OK if good else FAIL,
    )
    run.say(f"located via route: {found.route}; confirmations={found.confirmations}")
    outcome.located_by_script_match = OK if good else FAIL


def step_8_refusals(run: Run, built: BuiltChain, outcome: ChainOutcome) -> None:
    """OUTCOMES 3 AND 4: the transposed signatures, and the missing OP_0.

    THESE RUN BEFORE THE HAPPY PATH, deliberately. Both spend the same output the happy
    path spends, so if the happy path went first there would be nothing left to be refused
    -- and the refusal would then be indistinguishable from "already spent", which is the
    one answer that proves nothing about the script.

    The two signatures are the SAME two in both orders, produced once by
    _signatures_in_key_order() and swapped, so a pass here cannot be an artifact of two
    independently built scriptSigs. That is the whole point: the transposed scriptSig is
    the same LENGTH and the same SHAPE as the correct one, and only an interpreter can tell
    them apart. tests/test_adaptor_swap_chain.py says so instead of pretending otherwise.
    """
    run.step(8, "the two footguns: signatures TRANSPOSED, and the OP_0 dummy MISSING")
    alice_sig, bob_sig = _signatures_in_key_order(built.setup, built.redeem.digest)

    transposed_hex, _ = chain.assemble(built.redeem, bob_sig, alice_sig)
    correct_hex, _ = chain.assemble(built.redeem, alice_sig, bob_sig)
    run.say(
        f"the transposed scriptSig is {len(bytes.fromhex(transposed_hex))} bytes and the correct one is "
        f"{len(bytes.fromhex(correct_hex))} -- identical, which is why only a chain can tell them apart"
    )
    txid, message = _broadcast(run, transposed_hex, "Tx_redeem with the signatures TRANSPOSED")
    refused = txid is None
    run.check(
        "transposed signatures are REFUSED", message if refused else f"ACCEPTED as {txid}",
        "a refusal from the script interpreter", OK if refused else FAIL,
    )
    outcome.refused_when_transposed = OK if refused else FAIL
    if not refused:
        outcome.notes.append(
            "THE CHAIN ACCEPTED A TRANSPOSED 2-of-2 scriptSig. That contradicts OP_CHECKMULTISIG's "
            "documented lockstep walk and would mean the key order in the redeem script does not "
            "constrain the signature order on this chain. Nothing in this tree should rely on it either "
            "way until that is explained."
        )

    no_dummy_hex = _script_sig_without_op0(built, alice_sig, bob_sig)
    txid, message = _broadcast(run, no_dummy_hex, "Tx_redeem with the leading OP_0 MISSING")
    refused = txid is None
    run.check(
        "a missing OP_0 dummy is REFUSED", message if refused else f"ACCEPTED as {txid}",
        "a refusal: OP_CHECKMULTISIG pops one item more than it uses", OK if refused else FAIL,
    )
    outcome.refused_without_op0 = OK if refused else FAIL
    if not refused:
        outcome.notes.append(
            "THE CHAIN ACCEPTED A 2-of-2 scriptSig WITH NO OP_0 DUMMY. The 2010 off-by-one in "
            "OP_CHECKMULTISIG can never be fixed without invalidating old transactions, so this would be "
            "a genuine consensus divergence on this chain and is worth reporting upstream."
        )


def _script_sig_without_op0(built: BuiltChain, first_signature: bytes, second_signature: bytes) -> str:
    """The same scriptSig as assemble() produces, minus its leading OP_0 byte.

    Built by REMOVING the byte from the real assembler's output rather than by writing a
    second assembler, so the two differ in exactly one byte and nothing else. A
    hand-written variant could differ in a push encoding as well, and the harness would
    then be measuring two changes while reporting one.
    """
    full = two_of_two_script_sig(first_signature, second_signature, built.redeem.redeem_script)
    if not full.startswith(OP_0):
        raise RegtestSetupError(
            "the assembler did not put OP_0 first, so this test would be removing the wrong byte. "
            "adaptor_swap_scripts.two_of_two_script_sig() is the thing to look at."
        )
    return built.redeem.parsed.serialize({0: full[len(OP_0):]}).hex()


def step_9_happy_path(run: Run, built: BuiltChain, outcome: ChainOutcome) -> None:
    """OUTCOME 2: the 2-of-2 spends, with both signatures in key order.

    The assertion is the broadcast being ACCEPTED plus the output appearing on chain, not
    the broadcast returning a txid alone. `sendrawtransaction` accepting a transaction is
    already a full script verification -- the mempool runs the interpreter -- so acceptance
    is the measurement; the confirmation and the read-back are what say the coins moved.
    """
    run.step(9, "the happy path: both signatures, in the redeem script's key order")
    alice_sig, bob_sig = _signatures_in_key_order(built.setup, built.redeem.digest)
    raw_hex, predicted = chain.assemble(built.redeem, alice_sig, bob_sig)
    real_size = len(bytes.fromhex(raw_hex))
    run.say(
        f"size {real_size} bytes against a bound of {built.redeem.unsigned_size_bound} "
        f"(slack {built.redeem.unsigned_size_bound - real_size} -- the bound must never be under)"
    )
    run.check("the real size is at or under the fee's bound", real_size <= built.redeem.unsigned_size_bound,
              True, OK if real_size <= built.redeem.unsigned_size_bound else FAIL)
    txid, message = _broadcast(run, raw_hex, "Tx_redeem with both signatures in key order")
    accepted = txid is not None
    run.check(
        "the 2-of-2 SPENDS", txid if accepted else f"REFUSED: {message}",
        "a txid -- mempool acceptance IS a full script verification", OK if accepted else FAIL,
    )
    outcome.spends_in_correct_order = OK if accepted else FAIL
    if not accepted:
        outcome.notes.append(f"the 2-of-2 could not be spent at all on {run.asset}: {message}")
        return
    run.check("the predicted spend txid equals the daemon's", f"predicted={predicted} daemon={txid}",
              "the same txid", OK if txid == predicted else FAIL)
    _mine(run, 1)
    _corroborate_payout(run, txid, built.setup.alice.p2pkh_script, "the redeem")


def step_10_cancel_path(run: Run, built: BuiltChain, outcome: ChainOutcome) -> None:
    """OUTCOME 5: nLockTime on the 2-of-2, before and after T1, and then both branches of
    the SECOND 2-of-2.

    Four assertions in a deliberate order:

      a  the cancel is refused before T1 by RELAY (sendrawtransaction)
      b  the cancel is refused before T1 by CONSENSUS -- the daemon is asked to MINE it,
         which is the only thing that says a miner could not have included it. That is the
         distinction regtest_htlc_verify.py's step 8c draws and the one that mattered on
         2026-09-27. A chain with no generateblock gets a SKIP with the reason, never a
         pass.
      c  the cancel is ACCEPTED at T1
      d  the punish is refused before T2 and the refund is accepted -- which is the second
         2-of-2 spending, both branches, on the output the cancel created

    Nothing here uses OP_CHECKLOCKTIMEVERIFY. T1 and T2 are plain nLockTime fields, which
    is why no BIP65 activation height has to be mined first.
    """
    run.step(10, "the cancel path: nLockTime before T1, at T1, and then the second 2-of-2's two branches")
    cancel_sigs = _signatures_in_key_order(built.setup, built.cancel.digest)
    cancel_hex, cancel_txid = chain.assemble(built.cancel, *cancel_sigs)
    tip = current_height(run)
    run.say(f"tip={tip}, T1={built.t1}, T2={built.t2} -- the cancel is not final until height {built.t1}")
    run.check("we are genuinely before T1", tip < built.t1, True, OK if tip < built.t1 else FAIL)

    txid, message = _broadcast(run, cancel_hex, f"Tx_cancel EARLY (nLockTime {built.cancel.locktime})")
    refused = txid is None
    run.check(
        "10a RELAY refuses the early cancel", message if refused else f"ACCEPTED as {txid}",
        "a non-final refusal from the mempool", OK if refused else FAIL,
    )
    outcome.cancel_refused_before_t1_by_relay = OK if refused else FAIL

    _mine_early_cancel(run, built, cancel_hex, outcome)

    _wait_or_mine_to(run, built.t1)
    txid, message = _broadcast(run, cancel_hex, f"Tx_cancel AT T1 (height {current_height(run)} >= {built.t1})")
    accepted = txid is not None
    run.check(
        "10c the cancel is ACCEPTED at T1", txid if accepted else f"REFUSED: {message}",
        "the same transaction, now final", OK if accepted else FAIL,
    )
    outcome.cancel_accepted_at_t1 = OK if accepted else FAIL
    if not accepted:
        outcome.notes.append(f"the cancel was refused at T1 too, so nLockTime is not the reason: {message}")
        return
    run.check("the predicted cancel txid equals the daemon's", f"predicted={cancel_txid} daemon={txid}",
              "the same txid", OK if txid == cancel_txid else FAIL)
    _mine(run, 1)
    _spend_the_cancel_output(run, built, cancel_txid, outcome)


def _mine_early_cancel(run: Run, built: BuiltChain, cancel_hex: str, outcome: ChainOutcome) -> None:
    """10b: ask the daemon to MINE the early cancel. The only consensus-strength answer.

    A mempool refusal says a node will not pass the transaction on. `generateblock` runs
    TestBlockValidity over a block containing exactly the transactions it is handed, so a
    refusal from it says the CHAIN would not accept the block -- which is the claim worth
    having, and the one a relay refusal cannot make. regtest_htlc_verify.py's step 8c
    exists for the same reason.

    A daemon with no `generateblock` gets a SKIP naming the absence, never a pass. Gridcoin
    is expected to be in that position and the run says so rather than implying the
    stronger result.
    """
    node = run.node(wallet=False)
    if not daemons.method_exists(node, "generateblock"):
        run.check(
            "10b CONSENSUS refuses the early cancel",
            "not attempted: this daemon has no `generateblock`",
            "a TestBlockValidity refusal -- only this says a MINER could not have included it",
            SKIP,
        )
        outcome.notes.append(
            f"{run.asset} has no generateblock, so the early cancel was refused by RELAY only. The "
            f"stronger claim -- that a miner could not have included it -- is NOT established on this chain."
        )
        return
    # THE ADDRESS IS RESOLVED OUTSIDE THE try, AND THAT IS NOT TIDINESS. Inside it, a
    # `getnewaddress` that failed for any reason at all -- a locked wallet, a daemon with no
    # wallet loaded, a typo in a method name -- would raise RPCError and be scored as
    # "consensus refused the early cancel", which is the strongest claim this harness makes.
    # tests/test_adaptor_regtest_harness.py caught exactly that: a stub with no getnewaddress
    # answer produced `OK 10b CONSENSUS refuses the early cancel: got=getnewaddress: code=-32601
    # Method not found`. A refusal has to come from the thing under test or it is not evidence.
    try:
        mining_address = run.resolve_mining_address()
    except RPCError as exc:
        run.check(
            "10b CONSENSUS refuses the early cancel",
            f"not attempted: no address to mine to ({exc})",
            "a TestBlockValidity refusal -- and a wallet failure is NOT one", SKIP,
        )
        outcome.notes.append(
            f"{run.asset}: could not get an address to mine to, so the consensus-strength refusal was "
            f"never attempted: {exc}"
        )
        return
    try:
        result = node.call("generateblock", mining_address, [cancel_hex])
    except RPCError as exc:
        run.check(
            "10b CONSENSUS refuses the early cancel", str(exc),
            "a TestBlockValidity or non-final refusal", OK,
        )
        outcome.cancel_refused_before_t1_by_consensus = OK
        return
    run.check(
        "10b CONSENSUS refuses the early cancel", f"MINED IT: {result}",
        "a refusal -- nLockTime finality is supposed to be checked when a block is validated", FAIL,
    )
    outcome.cancel_refused_before_t1_by_consensus = FAIL
    outcome.notes.append(
        "THE DAEMON MINED A CANCEL WHOSE nLockTime IS IN THE FUTURE. That would mean nLockTime finality "
        "is relay policy on this chain rather than consensus, and the cancel path's timelock would be "
        "worth nothing against a miner. Nothing in this tree should use nLockTime as a timelock on this "
        "chain until that is explained."
    )


def _wait_or_mine_to(run: Run, target: int) -> None:
    """Advance to `target`: mine the difference, or WAIT for it on a chain that cannot mine."""
    tip = current_height(run)
    run.say(f"advancing from tip={tip} to height {target} so the cancel becomes final")
    _mine(run, max(0, target - tip))


def _spend_the_cancel_output(run: Run, built: BuiltChain, cancel_txid: str, outcome: ChainOutcome) -> None:
    """10d: the SECOND 2-of-2's two branches, on the output the cancel just created.

    The punish is tried first and must be refused (it is not final until T2), then the
    refund is tried and must be accepted. Order matters for the same reason step 8 runs
    before step 9: both spend the same output, and a refusal that arrives because the
    output is already gone proves nothing about the timelock.

    The cancel output is located by TXID AND VOUT, not by a scriptPubKey match, and that is
    not laziness: the cancel 2-of-2 names the same two keys as the lock 2-of-2 -- which is
    what the protocol specifies -- so the two P2SH scriptPubKeys are the same bytes and a
    script match cannot say which of the two outputs it found.
    """
    context = built.context
    value = built.cancel.output_satoshis
    cancel_output = chain.Outpoint(txid=cancel_txid, vout=0, value_satoshis=value)
    found = lookup_contract_output(lambda method, params: run.node().call(method, *params), cancel_txid, 0)
    run.say(
        f"the cancel output is {cancel_txid}:0 worth {satoshis_to_coins(value)}; the daemon reports "
        f"scriptPubKey {found.script_pubkey_hex} (route: {found.route}). Located by txid:vout, NOT by "
        f"script match -- the lock and cancel P2SH scripts are the same bytes by protocol design"
    )
    run.check("the cancel output's value is the one the refund was signed for",
              found.script_pubkey_hex, context.cancel_script_pubkey.hex(),
              OK if found.script_pubkey_hex == context.cancel_script_pubkey.hex() else FAIL)

    punish = chain.build_punish(context, cancel_output, built.t2)
    chain.assert_spends(punish.parsed, cancel_txid, 0)
    punish_hex, _ = chain.assemble(punish, *_signatures_in_key_order(built.setup, punish.digest))
    tip = current_height(run)
    run.check("we are genuinely before T2", tip < built.t2, True, OK if tip < built.t2 else FAIL)
    txid, message = _broadcast(run, punish_hex, f"Tx_punish EARLY (nLockTime {built.t2}, tip {tip})")
    refused = txid is None
    run.check(
        "10d-i the early punish is REFUSED", message if refused else f"ACCEPTED as {txid}",
        "a non-final refusal: T2 has not arrived", OK if refused else FAIL,
    )
    outcome.punish_refused_before_t2 = OK if refused else FAIL

    refund = chain.build_refund(context, cancel_output)
    chain.assert_spends(refund.parsed, cancel_txid, 0)
    refund_hex, refund_txid = chain.assemble(refund, *_signatures_in_key_order(built.setup, refund.digest))
    txid, message = _broadcast(run, refund_hex, "Tx_refund (no locktime -- publishable as soon as the cancel confirms)")
    accepted = txid is not None
    run.check(
        "10d-ii the refund SPENDS the second 2-of-2", txid if accepted else f"REFUSED: {message}",
        "a txid -- this is the second 2-of-2 spending", OK if accepted else FAIL,
    )
    outcome.refund_accepted_after_cancel = OK if accepted else FAIL
    if accepted:
        run.check("the predicted refund txid equals the daemon's", f"predicted={refund_txid} daemon={txid}",
                  "the same txid", OK if txid == refund_txid else FAIL)
        _mine(run, 1)
        _corroborate_payout(run, txid, built.setup.bob.p2pkh_script, "the refund")
    else:
        outcome.notes.append(f"the second 2-of-2 could not be spent: {message}")


def fund_and_prepare(run: Run, setup: LockSetup) -> chain.Outpoint:
    """Prepare a P2PKH input this process holds the key for, sized for the lock plus a fee."""
    coin = str(Decimal(LOCK_COIN[run.asset]) + Decimal(FUNDING_HEADROOM_COIN[run.asset]))
    run.say(f"preparing the input for lock {setup.label}: {coin} {run.asset}")
    return _send_to_self(run, setup.alice, coin)
