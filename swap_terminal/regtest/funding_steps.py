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
assertion can tell them apart, which is why tests/test_script_chain.py says so
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
from typing import NamedTuple

import base58
from chains.base import RPCError
from microfortnights import format_duration
from modules import network_selection
from modules import script_chain as chain
from modules.address_network import BASE58_VERSIONED_HASH160_LEN
from modules.htlc_rpc import lookup_contract_output
from modules.htlc_spend import (
    SIGHASH_ALL,
    coins_to_satoshis,
    legacy_sighash,
    satoshis_to_coins,
)
from modules.htlc_timelock import SECONDS_PER_BLOCK
from regtest import daemons
from regtest.console import FAIL, OK, SKIP, XFAIL, Console
from regtest.daemons import ChainConfig, RegtestSetupError, adapter_for
from regtest.keys import RegtestKey, key_from_seed
from regtest.txbuild import push_data

TOTAL_STEPS = 11

# How many blocks past the tip T1 and T2 are set. Small on purpose: there is no CLTV
# deployment to clear, so the only reason to put them further out is to leave room for
# the assertions between them. BLOCK COUNTS, never converted to microfortnights -- a
# block is not 1.2096 seconds long, it is however long it took (rule 6).
T1_BLOCKS_AHEAD = 6
T2_BLOCKS_AHEAD = 12

# HOW MANY 2-of-2s ONE RUN FUNDS, and why it is three rather than two.
#
# Each lock can be spent exactly once, so each terminal branch of the protocol needs its own:
#
#   A   Tx_redeem      the happy path, and the two OP_CHECKMULTISIG footguns
#   B   Tx_refund      lock -> cancel at T1 -> refund
#   C   Tx_punish      lock -> cancel at T1 -> WAIT TO T2 -> punish
#
# C WAS ADDED 2026-09-28 BECAUSE THE PUNISH HAD NEVER SPENT. The run that established the
# adaptor join -- 48 OK, 0 FAIL, docs/gridcoin_adaptor_join_2026_09_28.md -- broadcast Tx_punish
# exactly once, before T2, and asserted it was REFUSED. That is a real and necessary assertion
# and it is not the branch working: of the five transactions the protocol names, four had moved
# a coin and the punish had moved nothing. Operator, on being shown the run: "i want to test
# each branch first."
#
# It needs its own lock because the refund on lock B SPENDS the cancel output the punish would
# have taken, and a punish refused because the output is already gone proves nothing about T2 --
# the same argument step 8 makes for running before step 9, one level down the chain.
LOCKS_PER_RUN = 3

# How much of a test coin to put into each lock. Generous relative to every chain's fee
# floor -- measured 2026-09-28, the GRC minimum for a completable cancel path is
# 0.02 GRC, two fee floors plus dust -- so a failure in this harness is never a failure
# about the amount.
# GRC WAS "1.0" UNTIL 2026-09-28, AND IT WAS 100x LARGER THAN ANY RULE REQUIRES.
#
# Measured in Gridcoin's own source that day rather than reasoned about: MIN_TX_FEE is 10000
# satoshis (consensus/consensus.h:26) and version-2 transactions multiply it by ten
# (policy/fees.h:41), so the fee floor is 0.001 GRC per kB; and an output below CENT -- 0.01
# GRC -- is what policy/fees.h:54 calls dust. A 0.1 contract is ten times the dust line and a
# hundred times the per-kB fee.
#
# WHY IT MATTERS ENOUGH TO CHANGE. Every run consumes its funding by design, so the size of
# this number is the price of an ATTEMPT -- and on 2026-09-28 the CLTV measurement took seven
# attempts to reach step 5, each costing 1.51 GRC to set up and two of them stranding their
# contract outright. At 0.16 the same day would have cost a tenth as much, and the answer would
# have been identical: what CLTV does with a locktime does not depend on the amount beside it.
#
# BTC and LTC are unchanged. They run on regtest, where coins are minted on demand and the size
# of a lock costs nothing at all.
LOCK_COIN = {"BTC": "0.01", "LTC": "0.01", "GRC": "0.1"}

# The extra put into the P2PKH input the harness prepares for itself, over what the lock
# needs, so Tx_lock's own fee comes out of it with room to spare.
FUNDING_HEADROOM_COIN = {"BTC": "0.004", "LTC": "0.004", "GRC": "0.05"}

# What to ask the operator to add on top, so the split transaction's own fee comes out of their
# payment rather than out of the lock. One flat figure rather than a per-asset table: it is the
# fee for ONE 1-in-N-out transaction on a chain with a 0.01/kB floor, and the amount this asks
# for only has to be ENOUGH -- split_operator_funding computes the real fee from the real bytes
# and refuses by name if what arrived does not cover it.
SPLIT_FEE_ALLOWANCE_COIN = "0.01"

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
# Lock C's own confirmations before its T2 wait begins: the lock, and the cancel it spends.
_LOCK_C_CONFIRMATION_BLOCKS = 2





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
class Run:
    """One chain's run: where to reach it, what to print to, and what it can do."""

    console: Console
    config: ChainConfig
    wallet: str
    capabilities: dict = field(default_factory=dict)
    spawned: bool = False
    mining_address: str = ""
    # FUNDING OUTPOINTS THE OPERATOR ALREADY PAID FOR, one per lock, consumed in order by
    # fund_and_prepare(). Empty means the ordinary wallet route. A list rather than a flag
    # because the two locks need two distinct outputs and popping them is what guarantees the
    # second lock cannot be handed the first one's outpoint.
    operator_funding: list = field(default_factory=list)
    # A funding payment step 5 already located, so prepare_operator_funding() builds the split
    # from the SAME payment that let the run carry on. Two independent lookups could disagree
    # if the operator funded the address again in between, and the run would then be proceeding
    # on one basis and spending another.
    discovered_funding_txid: str = ""
    #: Payments to the funding address that WERE found and were already spent, as
    #: (txid, spender). Kept so the refusal can say "all of these are used, send another" --
    #: without it, a seeded run whose funding is exhausted falls through to the wallet route and
    #: reports a staking-only unlock problem, which on 2026-09-28 was an accurate sentence about
    #: something the operator did not need to fix (rule 14: say what the number means).
    skipped_funding_payments: list = field(default_factory=list)

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
        # THE REASONS, NOT JUST THE MISS. This called liveness_probe_that_answers(), which
        # returns None for a dead port and for a rejected password alike -- so the refusal
        # below guessed, and on 2026-09-29 it guessed wrong in the expensive direction: it told
        # an operator whose GUI wallet was up and serving RPC to start a second daemon, when
        # the answer was HTTP 401 from credentials read out of the MAINNET conf.
        answered, reasons = daemons.liveness_probe_results(run.config)
        if answered is None:
            raise RegtestSetupError(
                f"GRC: none of {list(daemons.LIVENESS_PROBES)} answered at {run.config.base_url}.\n"
                f"          {daemons.why_nothing_answered(reasons)}.\n"
                f"          What each probe actually said: {'; '.join(reasons) or '(nothing)'}"
            )
        # No "GRC:" here -- run.say() already prefixes with the asset, and the first version
        # of this line printed "GRC: GRC: answered by ...". Small, and exactly the class of
        # near-miss this file keeps correcting in other people's messages.
        run.say(f"answered by `{answered}`"
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
        # THIS HARNESS STARTS NO BTC/LTC DAEMON -- it adopts whatever is answering, which is
        # what the MWEB line downstream has to know before it claims a startup flag was applied.
        return daemons.assert_regtest(run.console, run.config, we_started_it=False)

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
    `modules/script_chain.select_funding_inputs()` exists because Gridcoin is on the
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
    except chain.ScriptChainError as exc:
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


def offer_funding_route(run: Run, signing: str) -> RegtestKey | None:
    """Print the address for the operator to fund, and return the key behind it.

    THE DIFFERENCE BETWEEN TELLING SOMEBODY THEIR WALLET IS IN THE WAY AND TELLING THEM WHAT TO
    DO INSTEAD. Extracted from _report_gridcoin_lock_state(), which ruff put one over the
    complexity ceiling -- rule 12 answers that by pulling the decision out, never by raising the
    ceiling or suppressing.

    Returns the key so the caller can tell the three cases apart: a seed is set and the route is
    open (an address was printed), no seed (instructions to set one were printed), or the probe
    did not come back open (nothing printed here, because the route is not available).
    """
    funding_key = operator_funding_key(run)
    if signing != "open":
        return funding_key
    run.say("")
    if funding_key is None:
        run.say(f"TO USE THAT ROUTE, set {FUNDING_SEED_VARIABLE} to something only you know and "
                f"re-run. The harness will print an address for you to fund once.")
        return None
    needed = (Decimal(LOCK_COIN[run.asset]) * LOCKS_PER_RUN
              + Decimal(FUNDING_HEADROOM_COIN[run.asset]) * LOCKS_PER_RUN + Decimal("0.1"))
    run.say(f"SO FUND THIS ADDRESS ONCE, FROM YOUR GUI: {funding_key.address}")
    run.say(
        f"  send at least {needed} {run.asset} to it, then RE-RUN THIS EXACT COMMAND. There is "
        f"nothing to copy and no txid to find: your wallet made the payment, so the harness "
        f"asks it (listtransactions) and picks the payment up by itself. The address is DERIVED "
        f"from {FUNDING_SEED_VARIABLE} -- keep that set and it is the same address every run. "
        f"Your wallet is never asked to create or sign anything: the harness signs this input "
        f"itself and only sendrawtransaction touches the daemon."
    )
    run.say("  THE ADDRESS IS NOT A SECRET. The key behind it never leaves this process and the "
            "seed is never printed.")
    report_recent_payments(run, funding_key)
    return funding_key


# How many recent outgoing payments to show when the funding address has not been paid.
# Small on purpose: this is a "did you mean one of these" prompt, not a wallet statement, and a
# long list is a wall an operator skims past -- which is the failure it exists to prevent.
RECENT_PAYMENTS_SHOWN = 6


def recent_payments(entries: list, exclude: str) -> list[tuple[str, str, str]]:
    """(address, amount, confirmations) for the wallet's recent SENDS, newest first, minus one.

    A pure function over what `listtransactions` returned, so the decision -- which entries count
    as "somewhere you recently sent money" -- can be called with seeded rows (rule 10). The RPC
    call and the printing are both somewhere else.

    `exclude` is the address the harness derived. Leaving it in would be the cruel version of
    this list: the one entry that is not the answer sitting among the ones that might be.
    """
    seen: set[str] = set()
    found: list[tuple[str, str, str]] = []
    for entry in reversed(entries):
        if not isinstance(entry, dict) or entry.get("category") != "send":
            continue
        address = str(entry.get("address") or "")
        if not address or address == exclude or address in seen:
            continue
        seen.add(address)
        found.append((address, str(entry.get("amount", "?")), str(entry.get("confirmations", "?"))))
        if len(found) >= RECENT_PAYMENTS_SHOWN:
            break
    return found


def report_recent_payments(run: Run, funding_key: RegtestKey | None) -> None:
    """"Did you mean one of these?" -- the line that would have saved three runs.

    MEASURED, 2026-09-28, THREE TIMES IN ONE EVENING, and every one of them was the same
    mistake wearing a different hat:

      1. the seed was the literal placeholder `<the same seed as yesterday>`, so the harness
         derived msuGYPvo.. and asked for it to be funded
      2. the seed was changed again, so it derived mxRi6srj..
      3. the operator paid msxA9Raj.. -- the address from the ORIGINAL seed, two seeds ago --
         and the harness went on asking for mxRi6srj.. with no idea the money had arrived
         somewhere it could almost see

    NOTHING ON THE SCREEN WAS WRONG IN ANY OF THEM. That is what makes this class of failure
    expensive: there is no bad number to spot, only an address that differs from one printed in a
    terminal half an hour earlier, and nobody compares addresses between runs. The harness had
    `listtransactions` in hand the whole time -- it is the same call
    `discover_operator_funding_txid` uses to FIND the payment -- and when that call found nothing
    it said nothing about what it HAD found.

    So: when the derived address has not been paid, show where the wallet HAS been sending. If
    one of those is where the operator meant to send, the seed is the thing that changed, and
    that sentence is the whole diagnosis.

    NEVER FATAL AND NEVER A CHECK. A wallet that will not list its transactions still gets the
    funding offer above; a diagnostic that could refuse a run would be a worse defect than the
    one it explains. It prints `(none)` rather than nothing when there are no recent sends,
    because a blank gap is ambiguous between "no payments" and "the query broke" (rule 14).
    """
    try:
        entries = run.node().call("listtransactions", "*", FUNDING_SEARCH_DEPTH, 0)
    except RPCError as exc:
        run.say(f"  (could not list the wallet's recent payments to compare: {exc})")
        return
    if not isinstance(entries, list):
        return
    found = recent_payments(entries, funding_key.address if funding_key else "")
    run.say("")
    if funding_key is None:
        # NO KEY MEANS THE SEED WAS REFUSED, so there is no derived address to compare against
        # and nothing to leave out. The list is then the only thing on screen that can tell the
        # operator which seed they want: the address they recognize is the one whose seed
        # produced it, and that is a recovery route rather than a diagnosis.
        run.say("  WHICH OF THESE DID YOU FUND? No address was derived, because the seed above "
                "was refused -- but the wallet remembers where it has been sending:")
    elif run.skipped_funding_payments:
        # IT SAID "has made no payment to <address>" IN BOTH CASES UNTIL 2026-09-28, and on that
        # day it printed exactly that beneath twenty lines listing THREE payments to that very
        # address, each read off the chain and skipped as spent. The operator then has to decide
        # which half of one screen to believe, which is the defect this harness spends its whole
        # output budget avoiding elsewhere.
        #
        # THE THIRD COPY OF ONE SENTENCE. `prepare_operator_funding` and `reclaim_funding.py`
        # carried the same claim and were fixed earlier the same day; this one was missed twice
        # because each fix was made where the failure was seen rather than by grepping for the
        # claim (rule 8: "when you touch a rule, grep for it"). Measured cost: two more runs.
        run.say(f"  YOU HAVE PAID {funding_key.address} -- {len(run.skipped_funding_payments)} "
                f"time(s) -- AND EVERY ONE IS SPENT. That is the ordinary state after a run, "
                f"not a wrong seed. Send another payment to it. For reference, this is where "
                f"the wallet has been sending recently:")
    else:
        run.say(f"  DID YOU ALREADY PAY A DIFFERENT ADDRESS? The wallet has made no payment to "
                f"{funding_key.address}, and this is where it HAS been sending recently:")
    if not found:
        run.say("    (none: this wallet has no recent outgoing payments at all)")
    for address, amount, confirmations in found:
        run.say(f"    {address}  {amount} {run.asset}  {confirmations} confirmation(s)")
    if found and funding_key is None:
        run.say(
            "    THE SEED THAT DERIVES THE ONE YOU RECOGNIZE IS THE SEED YOU WANT, and your "
            "shell remembers you setting it:"
        )
        run.say(f"      history | grep {FUNDING_SEED_VARIABLE}")
        run.say(
            "    If it is not there, the coins at that address are stranded -- they are TEST "
            "coins, so set any new seed, fund the address this harness then prints, and carry "
            "on. There is no way to recover a seed from an address."
        )
        return
    # `and funding_key is not None` IS THE RETURN ABOVE, SPELLED WHERE IT IS RELIED ON. The
    # `if found and funding_key is None` block three lines up returns, so every path that
    # reaches here with `found` truthy has a key -- and the block below reads
    # `funding_key.address`, which was the one unguarded read of it in this function.
    if found and funding_key is not None:
        run.say(
            f"    IF ONE OF THOSE IS WHERE YOU MEANT TO SEND, THE SEED IS WHAT CHANGED, not the "
            f"payment. {FUNDING_SEED_VARIABLE} derives the address, so a different seed is a "
            f"different address and the harness cannot see money at one it did not derive. Set "
            f"the seed back to the one that produced the address you paid, and re-run -- there is "
            f"nothing to re-send."
        )
        run.say(
            f"    AND IF YOU DO NOT REMEMBER THAT SEED, DO NOT GO LOOKING. Pay "
            f"{funding_key.address} -- the address above, the one THIS shell's seed derives -- "
            f"and carry on. The coins at the other address are stranded, and they are TEST coins: "
            f"the hunt costs more than they do. There is no way to recover a seed from an address "
            f"and there is deliberately no way to read one back out of this repository."
        )
        run.say(
            f"    KEEP THE EXPORT IN THE SHELL YOU RUN THIS FROM. A new terminal has no "
            f"{FUNDING_SEED_VARIABLE}, so it derives a different address and you land here again "
            f"-- which is how you got here. Put it in your shell profile and it stops happening."
        )


def already_funded(run: Run, signing: str) -> bool:
    """True when the operator has ALREADY funded the harness, so the wallet is not needed.

    THE ABSENCE OF THIS CHECK MADE THE WHOLE OPERATOR-FUNDED ROUTE UNREACHABLE. run_chain()
    calls step_5_spendable_coins() BEFORE prepare_operator_funding(), so the staking-only
    refusal fired first -- every time, funded or not. The operator sent 3.50 GRC to the address
    this harness printed, re-ran exactly as instructed, and got the same refusal back with
    their payment sitting on the chain. The evidence was in that run's own output: the balance
    line read 3875.90844485 where the run before it read 3879.40944485, exactly 3.501 GRC
    lighter. The funding existed and nothing looked for it.

    A route that cannot be reached is worse than one that was never built, because the build
    looks like progress. The wallet being unable to CREATE a transaction is only a reason to
    stop if nothing else can fund the run.
    """
    funding_key = operator_funding_key(run)
    if funding_key is None or signing != "open":
        return False
    discovered = discover_operator_funding_txid(run, funding_key)
    if not discovered:
        return False
    run.discovered_funding_txid = discovered
    run.check(
        "GRC the operator has ALREADY funded this harness, so the wallet is not needed",
        f"{discovered[:16]}.. pays {funding_key.address}",
        "a payment this harness can spend without the wallet", OK,
    )
    run.say("carrying on: this run funds itself from that payment and will not ask the wallet "
            "to create or sign anything")
    return True


# WHERE GRIDCOIN ACTUALLY ENFORCES nLockTime, read 2026-09-28 at master after the first GRC
# run reported `10b CONSENSUS` as a SKIP and left it at "NOT ESTABLISHED".
#
# A SKIP IS STILL NOT A PASS AND THIS DOES NOT MAKE IT ONE. Nothing below is a measurement on
# the operator's chain, and it is labeled as a reading every time it is printed. But "we could
# not run the check" and "nobody knows the answer" are different states, and reporting the
# second when the first is true is the same defect as reporting a SKIP as a pass -- it just
# errs in the other direction. The rule is at an exact line and a reader is entitled to it.
#
# THREE INDEPENDENT BARRIERS, one measured and two read:
#
#   MEASURED  the mempool refuses it, and the same bytes are accepted at T1. Finality is
#             checked inside IsStandardTx (src/policy/policy.cpp:57) and reported by
#             AcceptToMemoryPool as `tx-nonstandard` (src/validation.cpp:2176) -- a reason that
#             covers dust and odd scripts too, which is exactly why the same-bytes control at
#             T1 is what carries the argument rather than the reason string.
#   READ      the block ASSEMBLER skips non-final transactions outright
#             (src/miner.cpp:413: `if (tx.IsCoinBase() || tx.IsCoinStake() || !IsFinalTx(tx,
#             nHeight)) continue;`), so an honest Gridcoin staker never puts one in a block.
#   READ      and a block that contained one anyway is REJECTED BY CONSENSUS. AcceptBlock()
#             (src/validation.cpp:1680) walks every transaction and at :1777 does
#             `if (!IsFinalTx(tx, nHeight, block.GetBlockTime())) return state.DoS(10, ... "contains
#             a non-final transaction")`. DoS(10) is a peer-banning score on an INVALID block --
#             the network rejects it, so forcing the transaction in does not help an attacker.
#
# WHAT WOULD TURN THE SECOND AND THIRD INTO MEASUREMENTS: an RPC that runs block validation
# without mining. Bitcoin has two (`generateblock`, and `getblocktemplate` in proposal mode);
# Gridcoin's RPC table has neither -- checked, not assumed, by grepping src/rpc/server.cpp for
# getblocktemplate, submitblock, testblockvalidity and generateblock. `testmempoolaccept` is
# there and is a MEMPOOL check, so it cannot reach this rule either.
GRIDCOIN_FINALITY_SOURCE_READING = (
    "READ IN GRIDCOIN'S SOURCE (not measured here, and not a substitute for measuring): "
    "AcceptBlock() rejects a block containing a non-final transaction -- src/validation.cpp:1777, "
    "`state.DoS(10, \"contains a non-final transaction\")` -- so nLockTime IS a consensus rule on "
    "this chain, not merely relay policy. The block assembler also skips non-final transactions "
    "(src/miner.cpp:413), so an honest staker never includes one. Gridcoin has no RPC that runs "
    "block validation without mining -- no generateblock, no getblocktemplate proposal mode, no "
    "submitblock -- which is why this stays a reading."
)


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
    # XFAIL, NOT FAIL, WHEN THE PROBE DIAGNOSES A WALLET THAT CANNOT FUND. The console's own
    # words for XFAIL are "predicted failures: these are the harness working", and that is
    # exactly what this is: the harness asked a question, got a definite answer, and is about
    # to refuse with the remedy. FAIL means the code under test broke.
    #
    # THIS IS THE DEFECT I FIXED ONE LAYER DOWN AND RECREATED ONE LAYER UP. 3a1af14 stopped a
    # diagnosed staking-only send from arriving as `expected=no unhandled exception`; then the
    # pre-flight probe I added in the same commit scored its own diagnosis as FAIL, so the
    # operator's 2026-09-28 run ended with `FAIL=2` and BOTH lines under "unexpected failures"
    # -- for a run that diagnosed the condition perfectly and said so. Rule 13's "treat
    # 'skipped' plus 'success' in the same output as a defect in the output", inverted: a
    # correct diagnosis filed under "unexpected" teaches the reader to distrust the word.
    #
    # The exit code stays non-zero, because nothing was established. What changes is that the
    # summary no longer claims the harness was surprised.
    run.check(
        "GRC unlock scope, probed by an unsignable transaction that is never broadcast",
        scope, "can-sign (staking-only cannot fund; undetermined means ask again at the send)",
        OK if scope == "can-sign" else SKIP if scope == "undetermined" else XFAIL,
    )
    if scope in ("staking-only", "locked"):
        # BEFORE THE REFUSAL, ASK WHETHER THE WALLET IS NEEDED AT ALL. The remedy below costs
        # the operator their unlock deadline and stops their node staking, so establishing that
        # there may be a route around it is worth one more read-only call -- and this is the
        # only moment we know the wallet is blocked, which is exactly when the answer matters.
        signing = probe_supplied_key_signing(run)
        run.check(
            "GRC signrawtransaction with OUR keys (does it bypass the wallet's unlock?)",
            signing, "open -- then only the funding hop needs the wallet",
            OK if signing == "open" else SKIP if signing == "undetermined" else XFAIL,
        )
        # AND IF THEY HAVE ALREADY PAID, DO NOT REFUSE AT ALL. This must come after the probe,
        # because whether the funded route is usable is exactly what the probe answers -- and
        # before the remedy, because printing a page about unlocking a wallet that is not
        # needed is the defect this whole block has been correcting all day.
        if already_funded(run, signing):
            return
        if signing == "open":
            run.say("")
            for line in SUPPLIED_KEY_ROUTE.splitlines():
                run.say(line) if line.strip() else run.console.say("")
        # THE REMEDY IS PRINTED ONCE, HERE, AND THE EXCEPTION CARRIES A ONE-LINE SUMMARY.
        #
        # Measured on the operator's run, 2026-09-28: raising with the full remedy embedded put
        # that whole block on screen THREE times -- once in this refusal, once in the verdict's
        # `note:`, and once again in the SUMMARY's unexpected-failures list. Rule 14 asks for
        # output that says something; a wall of identical text repeated three times is how a
        # reader learns to skim past the part that matters.
        funding_key = offer_funding_route(run, signing)
        state = ("unlocked FOR STAKING ONLY" if scope == "staking-only"
                 else "LOCKED, so it can neither stake nor send")
        run.console.say("")
        # THE UNLOCK CEREMONY IS THE FALLBACK NOW, NOT THE HEADLINE, and the ordering says so.
        # It used to print in full above the one actionable line, so an operator who could take
        # the --funding-txid route had to read past four paragraphs about discarding their
        # unlock deadline to reach it. Rule 14 is about output that says something; burying the
        # answer under the thing it replaced is the same defect as not printing it.
        if funding_key is not None and signing == "open":
            run.say("The unlock route below is NOT needed if you take the funding route above. "
                    "It is kept for a daemon where the supplied-key probe does not come back "
                    "open, and for anyone who would rather unlock than fund an address:")
        for line in STAKING_ONLY_REMEDY.splitlines():
            run.say(line) if line.strip() else run.console.say("")
        run.console.say("")
        raise RegtestSetupError(
            f"GRC: this wallet is {state}. The full remedy is printed in step 5 above; in "
            f"short, this needs a wallet that may CREATE a transaction and changing that is "
            f"yours to decide. Nothing was funded, signed or broadcast."
        )
    if scope == "undetermined":
        run.say(
            "the probe could not tell, which is a real answer and not a pass (rule 17). Carrying "
            "on: the funding send below is the test, and it names the remedy if it refuses"
        )






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
# WHAT THE SUPPLIED-KEY PROBE MEANS WHEN IT COMES BACK "open", written beside the remedy it
# qualifies so no reader finds one without the other (rule 8).
#
# NOT IMPLEMENTED, AND SAYING SO IS THE POINT. This is a measured statement about the daemon,
# plus a design that follows from it -- not a feature this harness has. Announcing a route as
# though it were available would be worse than the unlock advice it replaces, because the
# operator would go looking for a flag. Rule 16 draws the line and this is on the proposal side:
# it changes how a fund path gets its coins, and it is the operator's call.
SUPPLIED_KEY_ROUTE = (
    "THE WALLET IS NOT NEEDED FOR THIS, and that was MEASURED on this daemon rather than read "
    "from source. `signrawtransaction` picks its keystore on argument presence before it checks "
    "any lock (rawtransaction.cpp:2769-2788), so a caller bringing its own keys never reaches "
    "EnsureWalletIsUnlocked -- and the probe above confirms that branch is reachable HERE, on "
    "v5.5.1.0. This harness already signs its own P2PKH inputs in-process (_sign_p2pkh), and "
    "sendrawtransaction consults no lock.\n\n"

    "SO THE WALLET IS NEEDED FOR EXACTLY ONE THING: putting coins at an address this harness "
    "holds the key for. One payment, from your GUI, which elevates in place and hands the "
    "elevation straight back (walletmodel.cpp:615, :704) -- staking never stops and your unlock "
    "deadline is never discarded. After that the wallet is never asked again.\n\n"

    "THIS IS BUILT AND IT IS THE --funding-txid FLAG. Until 2026-09-28 these paragraphs ended "
    "by denying that the route existed or was reachable by any flag -- correct while it was "
    "only a measured possibility, and false from the commit that built it. The denial went on "
    "printing two lines above the flag itself, so one screen said both things. That is the "
    "defect this harness has spent the day removing from other people's output, and the drift "
    "is recorded here rather than the old wording (rule 16: a wrong comment is a bug).\n\n"

    "THE OLD SENTENCES ARE DESCRIBED AND NEVER REPRODUCED, and the test that holds this scans "
    "for their exact phrases -- so quoting them back would fail it, which is correct and not a "
    "technicality. An operator skims. A screen carrying a denial in quotation marks is still a "
    "screen carrying that denial, and quotation marks are the first thing skimming drops."
)

STAKING_ONLY_REMEDY = (
    "A staking-only unlock cannot CREATE a transaction, and every route around it is closed: "
    "`sendtoaddress` goes through SendMoney() (wallet.cpp:4325, the -4 path), "
    "`signrawtransaction` with no keys of its own goes through EnsureWalletIsUnlocked() "
    "(rpcwallet.cpp:102, the -13 path), and the third route would mean reading a wallet key "
    "back out, which this repository does not do.\n\n"

    "THIS IS A STAKING WALLET AND THE RPC FIX COSTS YOU SOMETHING. Say that first, because an "
    "earlier version of this message did not and it was wrong to present the sequence as "
    "routine. `walletpassphrase` refuses while a wallet is unlocked -- IsLocked() is "
    "`GetUnlockScope() == Locked` and a staking-only unlock is not Locked -- so the only RPC "
    "route is `walletlock` first. GRIDCOIN'S OWN SOURCE NAMES WHAT THAT COSTS, in "
    "CWallet::ElevateToFull's docstring (wallet/wallet.h:388-405): locking first 'threw away "
    "the unlock's deadline', and a 'cancelled or mistyped prompt left a staking wallet locked "
    "and the node no longer staking'. If your unlock has a long deadline, relocking discards "
    "it and the re-unlock carries only the timeout you type.\n\n"

    "AND GRIDCOIN HAS THE RIGHT PRIMITIVE, UNWIRED. ElevateToFull() widens an unlock in place "
    "for one operation -- same passphrase, no relock, deadline preserved, staking never stops. "
    "It is implemented (wallet.cpp:723), declared on the interface (interfaces/wallet.h:318) "
    "and exposed (wallet/interfaces.cpp:145), and NOTHING CALLS IT: grepping the whole of "
    "src/ at master finds no caller in src/rpc/ and none in src/qt/. So it cannot be reached "
    "from a daemon or a GUI today, and this harness has no way to ask for it. Checked at "
    "master; whether v5.5.1.0 carries it at all is NOT established here.\n\n"

    "THE SEQUENCE, ON THE TESTNET DAEMON ONLY -- your mainnet wallet is a different daemon on "
    "a different port and is not involved. Decide whether the deadline is worth it first:\n"
    "  1. `walletlock`                            stops testnet staking, DISCARDS the deadline\n"
    "  2. `walletpassphrase <passphrase> 5400`    no third argument; it defaults to a full unlock\n"
    "  3. re-run this harness\n"
    "  4. `walletlock`, then the same with a trailing `true` to restore staking-only\n\n"

    "THE GUI DOES IT SAFELY AND IT STILL WILL NOT HELP HERE, which is worth stating because an "
    "earlier version of this message said to prefer it. WalletModel::requestUnlock() "
    "(qt/walletmodel.cpp:615) WIDENS a staking-only unlock in place -- deadline kept, staking "
    "never stops -- and its own comment describes the relock above as what it replaced. But the "
    "elevation is an RAII SCOPE: when the GUI operation ends, walletmodel.cpp:704 calls "
    "restrictToStakingOnly() and hands it straight back. So a GUI unlock covers that one GUI "
    "action and leaves the wallet staking-only again for anything arriving over RPC. It cannot "
    "open a window for this harness.\n\n"

    "If you do use the CLI, note the passphrase lands in argv -- world-readable through /proc "
    "and `ps` -- and in your shell history. This harness never handles a passphrase.\n\n"

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


# A STRING THAT CANNOT BE A PRIVATE KEY, used to find out which BRANCH signrawtransaction
# takes without ever creating or transmitting one. See probe_supplied_key_signing().
#
# Deliberately not a corrupted WIF: it has to fail DecodeSecret for a reason no reader could
# mistake for "we nearly sent a key". Nothing on any chain could decode this to a scalar.
NOT_A_PRIVATE_KEY = "this-string-is-not-a-private-key-and-cannot-decode-to-one"


def probe_supplied_key_signing(run: Run) -> str:
    """Can this daemon sign with keys WE supply, bypassing the wallet's unlock entirely?

    "open", "closed", or "undetermined".

    WHY THIS IS THE QUESTION THAT MATTERS. Read at Gridcoin master, src/rpc/rawtransaction.cpp
    2769-2788: signrawtransaction chooses its keystore on ARGUMENT PRESENCE ALONE, before any
    lock check --

        if (params.size() > 2 && !params[2].isNull()) {
            CBasicKeyStore tempKeystore;                               <- our keys
            ... DecodeSecret, AddKey ...
            return SignRawTransactionHelper(..., tempKeystore, ...);   <- NO unlock check
        } else {
            EnsureWalletIsUnlocked();                    <- the -13 the operator keeps hitting
            return SignRawTransactionHelper(..., *pwalletMain, ...);
        }

    So a caller that brings its own keys never reaches the unlock check. If that holds on the
    operator's build, THE WALLET IS ONLY NEEDED FOR ONE THING -- moving coins to an address this
    harness holds the key for -- and everything after that (build, sign, broadcast) runs against
    a staking-only wallet untouched. Their staking never stops and their unlock deadline is
    never discarded, which is what the whole STAKING_ONLY_REMEDY above is apologising for.

    AND IT IS PROBED WITH A STRING THAT IS NOT A KEY. The branch is selected before the key is
    validated, so an obviously-invalid one separates the two paths by which error comes back:

        "Invalid private key"      the with-keys branch was taken -> the route is OPEN
        "...staking only..."       the else branch was taken -> CLOSED on this build

    Nothing is generated, nothing that could ever hold value crosses the socket, and nothing is
    signed or broadcast. That matters more than the convenience: this repository does not move,
    copy or read back keys, and a probe that had to mint a real one to ask a question would be
    buying its answer with the thing the rule protects.

    UNDETERMINED IS A REAL ANSWER (rule 17). Their v5.5.1.0 is older than the source above, so a
    build whose signrawtransaction has a different shape lands here rather than being guessed at.
    """
    try:
        probe = chain.build_unsigned(
            asset=run.asset,
            spends=chain.Outpoint(txid="ff" * 32, vout=0, value_satoshis=100_000_000),
            outputs=[(99_000_000, bytes([0x6A]))],
            locktime=0,
            ntime=PROBE_NTIME if run.asset == "GRC" else None,
        )
        probe_hex = probe.serialize().hex()
    except Exception as exc:  # noqa: BLE001 -- checked: failing to BUILD the probe means "could not ask", which is the "undetermined" this function documents. It never becomes a verdict about the daemon, and it is reported on screen rather than swallowed.
        run.say(f"supplied-key probe could not be built ({type(exc).__name__}: {exc}); undetermined")
        return "undetermined"
    try:
        run.node().call("signrawtransaction", probe_hex, [], [NOT_A_PRIVATE_KEY])
    except RPCError as exc:
        text = str(exc).lower()
        if "invalid private key" in text:
            return "open"
        if is_staking_only_refusal(exc):
            return "closed"
        run.say(f"supplied-key probe answered something else ({exc}); undetermined")
        return "undetermined"
    # No error at all means the daemon accepted a string that cannot be a key, which says
    # nothing about the branch and must not be read as either answer.
    run.say("supplied-key probe: the daemon accepted a non-key without complaint; undetermined")
    return "undetermined"


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
    modules/script_chain.py and the whole reason `sendtoaddress` cannot build it. To
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
            # coins_to_satoshis(), not a fourth copy of the multiply-and-round. This line was
            # one (rule 8), and modules/htlc_spend.py's own comment records that
            # regtest/txbuild.py already held another -- so this file was adding a third to a
            # conversion that had one owner. They agree today; that is what a duplicate always
            # looks like on the day it is written.
            return coins_to_satoshis(str(entry["value"]))
    raise RegtestSetupError(f"{run.asset}: {txid} has no output {vout}")


def _sign_p2pkh(key: RegtestKey, digest: bytes) -> bytes:
    """A DER signature with the SIGHASH byte, from a key this process generated.

    regtest/keys.RegtestKey.sign_digest() is low-S and deterministic. Low-S is not
    cosmetic: Core has enforced it as a standardness rule since 0.11, so a high-S
    signature is refused from the mempool with wording that looks exactly like the script
    being wrong -- on a harness whose entire job is to say whether the script is wrong.
    """
    return push_data(key.sign_digest(digest) + bytes([SIGHASH_ALL])) + push_data(key.public_key)













def _p2pkh_sighash(parsed, key: RegtestKey) -> bytes:
    """The digest for a P2PKH input: the script code is the P2PKH scriptPubKey itself.

    NOT a redeem script -- a P2PKH input has none. The 2-of-2 spends sign over their redeem
    script (adaptor_swap_scripts.two_of_two_sighash), and Tx_lock's own input does not,
    which is the one place in this harness where the two differ. Signing over the wrong one
    produces a signature that verifies against nothing and a node that reports a bare
    script failure, so it is a named function rather than an expression at the call site.
    """
    return legacy_sighash(parsed, 0, key.p2pkh_script)


def broadcast_and_report(run: Run, raw_hex: str, label: str) -> tuple[str | None, str]:
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
        # THE BYTES AND WHERE THE REASON IS. Gridcoin answers `-22 TX rejected` and names
        # nothing, and `testmempoolaccept` -- which would name it -- does not exist on this
        # daemon (measured 2026-09-28, `code=-32601 Method not found`). But every refusal path
        # in AcceptToMemoryPool goes through `error(...)`, and src/logging.h writes that to
        # debug.log as "ERROR: ..." unconditionally, with no debug category to enable. So the
        # answer already exists on the operator's disk and nothing here was telling them to look.
        #
        # The raw hex is printed too. It carries no key -- a signed transaction is public by
        # construction, which is the whole point of broadcasting it -- and it is the only way to
        # decode elsewhere what this daemon refused. A refusal that cannot be reproduced off the
        # host is a refusal that has to be re-provoked to be studied.
        run.say("REFUSED. The daemon logged the reason even though it did not return it. Look with:")
        run.say(f"  grep -a ERROR {run.config.datadir}/testnet/debug.log | tail -20")
        run.say(f"  (or {run.config.datadir}/debug.log if that path does not exist)")
        run.say(f"the refused bytes, which carry no key and can be decoded anywhere: {raw_hex}")
        return None, str(exc)
    return txid, "accepted"


















class MempoolAnswer(NamedTuple):
    """What `testmempoolaccept` said, in BOTH the forms a caller needs.

    TWO FIELDS BECAUSE "NO REASON" IS FOUR DIFFERENT FACTS, and collapsing them to "" is what
    cost a live run on 2026-09-28. `split_operator_funding` asked, got "", proceeded, and
    `sendrawtransaction` then answered `-22 TX rejected` -- so the operator saw a bare Python
    traceback and the one thing that could have named the cause had already been thrown away.
    The daemon had been asked and its answer discarded.

      reason       the reject-reason, or "" when there is no refusal to act on. This is the
                   DECISION: a caller refuses on a non-empty reason and on nothing else.
      description  what happened, ALWAYS non-empty, for a human. Rule 14: an empty result must
                   never print nothing, because a blank is ambiguous between "allowed" and
                   "the question did not get asked".

    The three ways `reason` comes back empty, which `description` tells apart:

      allowed              the daemon ran AcceptToMemoryPool and would accept it.
      call failed          the call raised -- including a daemon with no `testmempoolaccept`,
                           whose `code=-32601 Method not found` says so in the error text. No
                           evidence either way.
      unexpected shape     something answered, in a shape this code does not model.

    Only the first is evidence of anything, and a caller that treats the other two as
    "fine, proceed" is asserting a fact it does not hold (rule 17).
    """

    reason: str
    description: str


def mempool_answer(run: Run, raw_hex: str) -> MempoolAnswer:
    """Ask the daemon whether it would accept this transaction, WITHOUT broadcasting it.

    `sendrawtransaction` answers `code=-22 message=TX rejected` and nothing more, so every
    refusal in this harness looks identical regardless of cause. `testmempoolaccept` runs the
    same AcceptToMemoryPool and returns {allowed, reject-reason} without broadcasting -- so it
    is both more informative and strictly safer than the send it supplements.

    READ OUT OF GRIDCOIN'S OWN SOURCE 2026-09-28 rather than assumed, because the shape of the
    answer is what this function models. `src/rpc/mempool.cpp` calls AcceptToMemoryPool with
    `test_only=true` and fills in `reject-reason` from `state.GetRejectReason()`, falling back
    to `"missing-inputs"` when the input fetch failed and `"rejected"` when neither said
    anything. `src/validation.cpp`'s `test_only` branch skips ONLY the pool insertion and the
    wallet signals -- every validation check above it is the same one the real send runs. So an
    `allowed` answer here and a refusal from the send are not supposed to be possible, and if
    the operator ever sees both, THAT is the finding.

    The raw answer is carried into `description` verbatim for the same reason: a shape this
    code does not model is exactly the case where guessing is worst.
    """
    # NO `method_exists` PROBE FIRST, and that is a decision rather than an omission. It would
    # cost a second RPC round trip on every call to distinguish "no such method" from "the call
    # failed" -- and it does not distinguish them, because the RPCError already carries
    # `code=-32601 Method not found` in its own text. A probe that buys a fact the error message
    # already states is a round trip spent on nothing (rule 3), and it made four existing test
    # stubs answer a `help` call that has nothing to do with what they are testing.
    try:
        results = run.node(wallet=False).call("testmempoolaccept", [raw_hex])
    except RPCError as error:
        return MempoolAnswer("", f"asked, and the call failed (a daemon without the method says so here): {error}")
    # ONE UNMODELED-SHAPE BRANCH RATHER THAN TWO. An answer that is not a non-empty list and an
    # answer whose first element is not an object are the same fact to the reader -- "something
    # replied and it is not what this code models" -- and they print the same sentence, so
    # asking the question once keeps them from drifting into two different sentences (rule 8).
    first = results[0] if isinstance(results, list) and results else results
    # `allowed` MUST BE PRESENT, and its absence is a shape problem rather than a refusal.
    # src/rpc/mempool.cpp pushes it unconditionally before anything else, so an object without
    # it did not come from the code this function models -- and reading a missing key as False
    # would turn a stub, a proxy, or a future Gridcoin into "the daemon refuses your funding",
    # which is the strongest thing this harness says about an operator's money.
    if not isinstance(first, dict) or "allowed" not in first:
        return MempoolAnswer("", f"asked, and the answer had a shape this code does not model: {first!r}")
    if first.get("allowed"):
        return MempoolAnswer("", f"the daemon says it WOULD accept this transaction: {first!r}")
    reason = str(first.get("reject-reason", "") or "")
    if not reason:
        # allowed is false and the daemon named nothing. That is a refusal, and treating it as
        # "no reason, carry on" is how the 2026-09-28 traceback happened. Give it a name.
        return MempoolAnswer("rejected", f"the daemon REFUSES it and names no reason: {first!r}")
    return MempoolAnswer(reason, f"the daemon REFUSES it: reject-reason={reason!r}")


def mempool_reject_reason(run: Run, raw_hex: str) -> str:
    """The daemon's reject-reason for this transaction, or "" if it will not name one.

    ONE IMPLEMENTATION, kept as a wrapper because three call sites want only the decision and
    reading `.reason` at each of them would put the field name in four places (rule 8).
    """
    return mempool_answer(run, raw_hex).reason




def wait_or_mine_to(run: Run, target: int) -> None:
    """Advance to `target`: mine the difference, or WAIT for it on a chain that cannot mine."""
    tip = current_height(run)
    run.say(f"advancing from tip={tip} to height {target} so the cancel becomes final")
    _mine(run, max(0, target - tip))








def wallet_owned_address(run: Run) -> str:
    """An address the WALLET ALREADY OWNS, so a reclaim has somewhere to go without being told.

    THE QUESTION THIS REMOVES, asked by the operator 2026-09-28: "i don't know what the wallet
    address was." They should not have to. A tool that empties an address into a wallet needs a
    destination in that wallet, and the wallet is right there -- being asked for one is the tool
    making somebody look up something it can see, which is the same defect as requiring a txid
    that `listtransactions` already held.

    `listunspent` RATHER THAN `getnewaddress`, and the reason is the whole reason this harness
    exists in its current shape: the operator's wallet is unlocked FOR STAKING ONLY.
    `getnewaddress` writes a new key into the wallet, which a locked or staking-only wallet may
    refuse -- and whether Gridcoin v5.5.1.0 refuses it is NOT established here, so this does not
    find out the hard way. `listunspent` only reads, needs no unlock, and every address it
    returns is one the wallet demonstrably controls because it is holding coins at it.

    Reusing an address the wallet already used is a privacy cost on a real chain and is not one
    here: this is testnet, and the alternative is a tool that cannot run on the wallet it exists
    to serve. An operator who wants a fresh address passes --to and names one.
    """
    try:
        rows = run.node().call("listunspent")
    except RPCError as exc:
        raise RegtestSetupError(
            f"{run.asset}: could not read the wallet's own outputs to find somewhere to send to "
            f"({exc}). Pass --to with an address from your wallet instead"
        ) from exc
    for row in rows if isinstance(rows, list) else []:
        address = isinstance(row, dict) and str(row.get("address") or "")
        if address:
            run.say(f"paying an address the wallet already owns: {address} "
                    f"(from listunspent -- no new key was created and no unlock was needed)")
            return address
    raise RegtestSetupError(
        f"{run.asset}: the wallet reports no unspent outputs, so this could not find an address "
        f"it owns. Pass --to with one from your wallet"
    )


def p2pkh_script_for_address(asset: str, address: str) -> bytes:
    """The scriptPubKey that pays `address`, refusing one encoded for the wrong network.

    THE VERSION BYTE IS CHECKED AGAINST THE CONFIGURED NETWORK rather than assumed, and this is
    the first caller of `modules/network_selection`. It is the right first one: paying an
    address is exactly where a network mismatch costs coins, and the check is one comparison.
    An address carrying a mainnet byte while this engine is on testnet is not a typo to correct
    -- it is somebody about to send test coins to a mainnet address, or the reverse, and both
    are refused by name.

    P2PKH only. A P2SH or bech32 destination is refused rather than half-handled: this exists to
    return a stranded funding output to an ordinary wallet address, and a reclaim tool that
    silently built the wrong script for a fancier destination would strand it again somewhere
    harder to reach.
    """
    expected = network_selection.base58_version(asset, network_selection.P2PKH)
    try:
        decoded = base58.b58decode_check(address)
    except ValueError as exc:
        raise RegtestSetupError(f"{asset}: {address!r} is not valid base58check: {exc}") from exc
    if len(decoded) != BASE58_VERSIONED_HASH160_LEN:
        raise RegtestSetupError(
            f"{asset}: {address} decodes to {len(decoded)} bytes, not "
            f"{BASE58_VERSIONED_HASH160_LEN}. A P2PKH address is a version byte and a hash160"
        )
    if decoded[:1] != expected:
        raise RegtestSetupError(
            f"{asset}: {address} carries version byte 0x{decoded[0]:02x} and this engine is on "
            f"{network_selection.selected_network()}, whose {asset} P2PKH byte is "
            f"0x{expected[0]:02x}. That is not a typo to correct -- paying it would send coins "
            f"to an address on the other network. Nothing was built, signed or broadcast"
        )
    return b"\x76\xa9" + bytes([len(decoded) - 1]) + decoded[1:] + b"\x88\xac"


def reclaim_p2pkh(run: Run, key: RegtestKey, source: chain.Outpoint, destination: str) -> tuple[str, str, int]:
    """`reclaim_p2pkh_to_script` with the destination given as an ADDRESS. See that function.

    Split 2026-09-28 when grc_htlc_verify.py needed to pay a P2SH -- a contract's scriptPubKey,
    which has no address form this repository will encode. The whole body was about building
    and signing a one-input spend and exactly one line of it cared that the destination was an
    address, so the address decode moved up here and the rest became reusable (rule 8: the
    second caller is what shows which half was the decision).
    """
    return reclaim_p2pkh_to_script(run, key, source, p2pkh_script_for_address(run.asset, destination))


def refuse_if_this_key_does_not_own_the_output(
    run: Run, key: RegtestKey, source: chain.Outpoint
) -> None:
    """Does `key` actually own `source`? Ask BEFORE signing, not after the chain refuses.

    THE DEFECT THIS CATCHES, measured 2026-09-28 rather than imagined. grc_htlc_verify.py's
    fund_contract() signed with `operator_funding_key(run)` an output that
    `prepare_operator_funding` had already paid to `contract["refund"]`. The process holds both
    keys, so nothing failed locally: the transaction built, signed, serialized and predicted its
    own txid, and every check the harness makes passed. The chain refused it with

        -22 TX rejected

    which names nothing, four runs in a row, and the real answer sat in the operator's debug.log
    as `39c099481d VerifySignature failed`. A wrong key among several the same process is
    holding is invisible to every local check, because a signature over the wrong key is a
    perfectly well-formed signature.

    ONE getrawtransaction, AND THE ANSWER IS EXACT. The previous output's scriptPubKey is what
    ConnectInputs verifies the scriptSig against, so comparing it to this key's own P2PKH script
    asks the identical question the chain will ask, before anything is signed or sent.

    A DIAGNOSTIC AND NOT A GATE when it cannot be answered, the same shape as every other check
    in this module: a daemon that will not decode the funding transaction leaves this saying so
    and returning, and the signing proceeds exactly as it did before this existed. What it must
    never do is refuse a good run because a lookup failed.
    """
    # OSError IS IN THE LIST BECAUSE A DAEMON THAT IS NOT THERE IS NOT A SIGNING ERROR.
    # requests.RequestException derives from OSError, so an unreachable node arrives here as a
    # ConnectionError rather than an RPCError -- and letting that escape would turn "the check
    # could not run" into a crash, which is the exact inversion this function is written to
    # avoid. Two reclaim tests found it: they exercise the FEE arithmetic with no node stubbed
    # at all, and a guard that raised there would have made a diagnostic into a gate.
    try:
        decoded = _decoded(run, source.txid)
    except (RegtestSetupError, RPCError, OSError) as error:
        run.say(f"could not check which key owns {source.txid}:{source.vout} ({error}); signing anyway")
        return
    outputs = decoded.get("vout", []) if isinstance(decoded, dict) else []
    for output in outputs:
        if int(output.get("n", -1)) != source.vout:
            continue
        owner = (output.get("scriptPubKey") or {}).get("hex", "")
        if owner == key.p2pkh_script.hex():
            return
        raise RegtestSetupError(
            f"{run.asset}: THE SIGNING KEY DOES NOT OWN THE OUTPUT IT IS ABOUT TO SPEND.\n"
            f"  the output:      {source.txid}:{source.vout}, worth "
            f"{satoshis_to_coins(source.value_satoshis)} {run.asset}\n"
            f"  it is paid to:   {owner}\n"
            f"  this key pays:   {key.p2pkh_script.hex()} ({key.address})\n"
            f"  A signature made with the wrong key is a well-formed signature, so nothing local "
            f"catches this -- the chain answers `-22 TX rejected`, and only its debug.log says "
            f"`VerifySignature failed`. Nothing was signed or broadcast."
        )
    run.say(
        f"{source.txid}:{source.vout} has no output at that index in the daemon's decoding, so "
        f"ownership could not be checked; signing anyway"
    )


def reclaim_p2pkh_to_script(
    run: Run, key: RegtestKey, source: chain.Outpoint, destination_script: bytes
) -> tuple[str, str, int]:
    """Build and SIGN a spend of one P2PKH outpoint, entirely to `destination`. Broadcasts NOTHING.

    RETURNS THE BYTES RATHER THAN SENDING THEM, so the caller decides. `reclaim_funding.py`
    defaults to printing them and needs `--send` to broadcast, which is the shape rule 16 asks
    for when something moves money: the thing that moves it is a separate, explicit act.

    One input, one output, no change: the whole output goes to `destination` less the fee. There
    is nothing to keep back -- the point is to empty a stranded address -- and a change output
    returning to the same address would leave a second outpoint there to be stranded again.

    Signed in THIS process with `_sign_p2pkh`, the same call Tx_lock's own input uses, so the
    wallet is never asked to sign and a staking-only unlock is irrelevant here exactly as it is
    everywhere else in this harness.
    """
    refuse_if_this_key_does_not_own_the_output(run, key, source)
    ntime = int(time.time()) if run.asset == "GRC" else None
    sizing = chain.build_unsigned(
        asset=run.asset, spends=source, outputs=[(source.value_satoshis, destination_script)],
        locktime=0, ntime=ntime,
    )
    fee = chain.fee_satoshis_for(run.asset, sizing, chain.p2pkh_script_sig_upper_bound(key.public_key))
    value = source.value_satoshis - fee
    if value <= 0:
        raise RegtestSetupError(
            f"{run.asset}: the output holds {satoshis_to_coins(source.value_satoshis)} and the "
            f"fee for spending it is {satoshis_to_coins(fee)}, so nothing would be left. This "
            f"address cannot be emptied at this fee rate"
        )
    # ONE ntime FOR BOTH BUILDS, as split_operator_funding's comment argues at length: reading
    # the clock twice sizes the fee against different bytes than the ones signed, and makes the
    # predicted txid wrong.
    unsigned = chain.build_unsigned(
        asset=run.asset, spends=source, outputs=[(value, destination_script)],
        locktime=0, ntime=ntime,
    )
    script_sig = _sign_p2pkh(key, _p2pkh_sighash(unsigned, key))
    raw = unsigned.serialize({0: script_sig}).hex()
    return raw, chain.predicted_txid(unsigned, {0: script_sig}), value


FUNDING_SEED_VARIABLE = "ST_ADAPTOR_FUNDING_SEED"
FUNDING_ROLE = "funding"


def operator_funding_key(run: Run) -> RegtestKey | None:
    """The key the operator funds, or None when no seed is configured.

    None rather than a raise, because "no seed set" is the ordinary case on BTC and LTC where
    the wallet funds the harness itself. It only becomes a problem on a chain whose wallet has
    refused, and that is where it is reported.

    A SEED THAT IS PRESENT AND WRONG IS NOT THAT CASE, and it gets a RegtestSetupError instead
    of None. `key_from_seed` refuses two kinds -- an empty one, and one still wrapped in the
    angle brackets of the instruction it was copied from -- and both mean the operator asked for
    the funded route and will not get it. Returning None there would fall through to "no seed
    configured", which is a different sentence with a different remedy, and the one thing this
    harness must not do is answer a specific question with a general message.

    Translated to RegtestSetupError HERE rather than raised as one in regtest/keys.py, because
    that module decides about keys and knows nothing about runs -- a precondition class belongs
    at the seam where preconditions are reported, which is this one (rule 10).
    """
    seed = os.environ.get(FUNDING_SEED_VARIABLE, "")
    if not seed.strip():
        return None
    try:
        return key_from_seed(seed, FUNDING_ROLE)
    except ValueError as exc:
        # AND SHOW WHERE THE MONEY WENT BEFORE REFUSING. A seed this harness will not use is
        # exactly when the operator most needs to know which address they funded -- the
        # refusal is otherwise a dead end that names no way back. It prints the wallet's
        # recent payments with NOTHING excluded, because no address was derived to exclude.
        report_recent_payments(run, None)
        raise RegtestSetupError(
            f"{run.asset}: {FUNDING_SEED_VARIABLE} is set but cannot be used. {exc}"
        ) from exc


# How far back to look for the operator's funding payment. 200 covers a staking wallet's recent
# history comfortably -- a Gridcoin wallet's transaction list fills mostly with its own
# coinstakes -- without asking a daemon to serialize thousands of entries on every run.
FUNDING_SEARCH_DEPTH = 200


class FundingPayment(NamedTuple):
    """One payment the wallet remembers making to the funding address."""

    txid: str
    confirmations: object   # an int from the daemon, or "?" when it did not say


def payments_to_the_funding_address(entries: object, address: str) -> list[FundingPayment]:
    """The wallet's payments to `address`, NEWEST FIRST. A pure function over listtransactions.

    EXTRACTED SO TWO CALLERS SHARE ONE ANSWER (rule 8). `discover_operator_funding_txid` walks
    this to pick a usable payment; the operator panel walks the same list to SHOW every payment
    and which of them are spent. Written twice, those two would agree on the day they were
    written -- the same "bug with a delay on it" this repository keeps paying for.

    NEWEST FIRST because listtransactions returns oldest-first and every caller wants the other
    order. Reversing at each call site is the kind of detail that gets it right in one place and
    wrong in the next.

    NO DAEMON, NO RUN, NO SIDE EFFECT. The decision -- which rows count as a payment to this
    address -- is a function of the rows and the address alone, so a test hands it rows (rule 10).
    The `category` filter keeps both "send" and "receive": the wallet made the payment, so it is
    a send from its point of view, and the receive case covers a wallet that also owns the
    address.
    """
    rows = entries if isinstance(entries, list) else []
    found = []
    for entry in reversed(rows):
        if not isinstance(entry, dict):
            continue
        if entry.get("address") != address:
            continue
        if entry.get("category") not in ("send", "receive"):
            continue
        txid = entry.get("txid")
        if txid:
            found.append(FundingPayment(str(txid), entry.get("confirmations", "?")))
    return found


def discover_operator_funding_txid(run: Run, key: RegtestKey) -> str | None:
    """The txid of the operator's payment to `key.address`, found by ASKING THE WALLET.

    THE OPERATOR SHOULD NOT HAVE TO CARRY A TXID BACK, and requiring one was a failure of
    imagination on my part rather than a constraint. The reasoning that produced --funding-txid
    was: this address is deliberately not the wallet's, `listunspent` returns only the wallet's
    own outputs, `importaddress` is False on v5.5.1.0 so it cannot be watched, and `gettxout` is
    False -- therefore nothing can say what is unspent there, therefore the operator must name
    the transaction.

    Every step of that is true and the conclusion does not follow. THE WALLET MADE THE PAYMENT.
    It does not own the output, but it certainly remembers sending it, and `listtransactions`
    reports exactly that: txid, address and category for each recent entry
    (src/wallet/rpcwallet.cpp:1586-1607). So the wallet can be asked "did you pay this address",
    which is a different question from "what is unspent at it" -- and the one that was needed.

    Measured cost of not seeing that: the operator ran `--funding-txid <that txid>` with my
    placeholder pasted literally, got a bash redirect error, and told me they did not know the
    txid. They were right not to. A tool that makes somebody go and look up an identifier their
    own wallet is already holding is a tool asking them to do its work.

    None means no such payment was found, which is the ordinary state before they have sent one.
    `--funding-txid` remains for the case this cannot cover: a payment made from somewhere other
    than this wallet, which it has no record of.
    """
    try:
        entries = run.node().call("listtransactions", "*", FUNDING_SEARCH_DEPTH, 0)
    except RPCError as exc:
        run.say(f"could not read the wallet's recent transactions ({exc}); "
                f"pass --funding-txid instead if you have already funded the address")
        return None
    if not isinstance(entries, list):
        return None
    # NEWEST FIRST, AND SKIP THE ONES ALREADY SPENT. listtransactions returns oldest-first, and
    # a re-funded address should use the LATEST payment -- but "latest" is not "usable", which is
    # what three failed runs on 2026-09-28 cost the operator. Every run consumes its funding by
    # design, so after a successful one the newest payment IS the spent one, and this function
    # handed it back three times in a row with a cheerful "no --funding-txid needed".
    #
    # The comment this replaces already knew: "an earlier one is most likely already spent by a
    # previous run, and spending it again would fail as a double-spend several steps later with
    # no clue why." It drew the wrong conclusion from it -- that picking the newest avoids the
    # problem -- when the newest is exactly the one a completed run just ate.
    #
    # ASKING IS CHEAP NOW, and it was not when that comment was written: `find_the_spender`
    # walks blocks, and a payment with N confirmations needs exactly N of them looked at,
    # because nothing mined before it can spend its output. The operator's stale funding was
    # found in 111 blocks and 3.3s; a payment made five minutes ago costs three blocks.
    for payment in payments_to_the_funding_address(entries, key.address):
        run.say(
            f"the wallet remembers paying {key.address} in {payment.txid} "
            f"({payment.confirmations} confirmations)"
        )
        spender = _spender_of_the_payment(run, key, payment.txid, payment.confirmations)
        if spender is None:
            run.say(f"using {payment.txid} -- no --funding-txid needed")
            return payment.txid
        run.skipped_funding_payments.append((payment.txid, spender))
        run.say(
            f"SKIPPING {payment.txid}: its output was already spent by {spender}. Looking further "
            f"back -- a completed run consumes its funding, so the newest payment is often the "
            f"used one"
        )
    return None


def _spender_of_the_payment(run: Run, key: RegtestKey, txid: str, confirmations) -> str | None:
    """Whatever spent this payment's output to `key.address`, or None. NEVER RAISES.

    SEPARATE FROM THE LOOP ABOVE so the loop reads as the decision it is -- newest usable
    payment -- rather than as four levels of error handling with a choice buried in it (rule 10).

    NONE ON EVERY UNCERTAINTY, and that is the safe direction here rather than the optimistic
    one. A payment this cannot resolve or cannot scan is treated as usable, which is exactly
    what this function's absence used to do, so a daemon that will not serve `getblock` behaves
    as it did before the scan existed. The gate in `refuse_if_the_funding_is_already_spent`
    still runs before anything is broadcast, so an unspendable output refuses there instead of
    being silently sent -- one uncertainty does not become a strand.
    """
    try:
        outpoint = find_operator_funding(run, key, txid)
    except (RegtestSetupError, RPCError) as error:
        run.say(f"could not read {txid} to check whether it is spent ({error}); treating it as usable")
        return None
    depth = confirmations + 1 if isinstance(confirmations, int) and confirmations >= 0 else MAX_SPEND_SCAN_BLOCKS
    spender, description = find_the_spender(run, outpoint, max_depth=depth)
    run.say(f"is it still there? {description}")
    return spender


def find_operator_funding(run: Run, key: RegtestKey, txid: str) -> chain.Outpoint:
    """The output of `txid` that pays `key.address`, read off the chain.

    WHY THE OPERATOR HAS TO NAME THE TXID. `listunspent` returns the WALLET's outputs, and this
    address is deliberately not the wallet's -- that is the entire point of the route. The
    daemon's measured capability set leaves no way to ask "what is unspent at this address":
    `importaddress` is False on v5.5.1.0, so it cannot be watched, and `gettxout` is False too.
    `getrawtransaction` is True, so one txid is enough and is the smallest thing the operator
    has to carry back from their GUI.

    THE VOUT IS FOUND, NEVER ASSUMED. A GUI send puts the payment and the change in whichever
    order it likes -- _send_to_self() carries the same warning about sendtoaddress for the same
    reason -- and assuming 0 would have the harness signing over the operator's change.
    """
    decoded = _decoded(run, txid)
    wanted = key.p2pkh_script.hex()
    for output in decoded.get("vout", []):
        script = (output.get("scriptPubKey") or {}).get("hex", "")
        if script == wanted:
            index = int(output["n"])
            satoshis = coins_to_satoshis(str(output["value"]))
            # "CANDIDATE", NOT "THE OPERATOR'S FUNDING", and the word is the fix for a
            # misreading this line caused on 2026-09-28 -- mine, not the operator's. It used to
            # read "found the operator's funding at <txid>:<n> worth 1.00000000 GRC", and it is
            # emitted HERE, before anything has asked whether that output is still unspent. In
            # the nine-step harness the verdict follows two lines later, so the sequence reads
            # correctly. In the operator panel it does NOT: payment_rows() puts the verdict in
            # the PAGE's table and the terminal shows only this line, so five long-spent outputs
            # printed as five found fundings. I read that terminal off the operator's paste and
            # told them 12.12 GRC was available; the next run established that every one of the
            # five was spent, at depths of 168 to 479 blocks.
            #
            # Rule 14's "state what the number means, next to the number", and rule 17's line
            # between a finding and a hypothesis, are the same sentence here: this line is a
            # candidate, the spend check has not run, and the words have to say so where they
            # are read rather than where they were written.
            run.say(
                f"candidate funding output {txid}:{index} worth "
                f"{satoshis_to_coins(satoshis)} {run.asset}, paying {key.address} "
                f"-- NOT yet checked for having been spent"
            )
            return chain.Outpoint(txid=txid, vout=index, value_satoshis=satoshis)
    raise RegtestSetupError(
        f"{run.asset}: transaction {txid} has no output paying {key.address}. Its outputs pay "
        f"{[(o.get('scriptPubKey') or {}).get('hex', '?')[:16] for o in decoded.get('vout', [])]}. "
        f"Either the txid is not the funding payment, or the seed in {FUNDING_SEED_VARIABLE} is "
        f"not the one whose address you sent to -- the address is DERIVED from that seed, so a "
        f"changed seed is a changed address. Nothing was funded, signed or broadcast."
    )


# HOW FAR BACK THE SPEND SCAN LOOKS, in blocks. Gridcoin's target is 90s, so 2000 blocks is
# about fifty hours -- comfortably past any funding payment an operator is still waiting on, and
# bounded so a harness run cannot turn into a chain walk. The cap is REPORTED when it bites
# rather than silently truncating the answer: "no spender found in the last 2000 blocks" and "no
# spender exists" are different claims, and only the first one is ever established here.
MAX_SPEND_SCAN_BLOCKS = 2000


def spender_in_block(block: dict, txid: str, vout: int) -> str | None:
    """Does any transaction in this block spend `txid:vout`? Its txid, or None.

    THE DECISION, AT THE BOTTOM, WHERE IT CAN BE CALLED WITH SEEDED INPUTS (rule 10). The
    walking, the progress line and the RPC belong to the caller; this is a pure question about
    one decoded block, so a test can hand it a block and assert on the answer without a daemon.

    `getblock(hash, true)` returns every transaction with its full `vin`, each entry carrying
    `txid` and `vout` (src/rpc/blockchain.cpp blockToJSON -> TxToJSON, read 2026-09-28). A
    coinbase input has no `txid` at all, so `.get` rather than `[]`: an input that names nothing
    cannot be spending anything, and raising a KeyError on every block's first transaction would
    make this function useless.
    """
    for transaction in block.get("tx", []):
        if not isinstance(transaction, dict):
            # txinfo=false was passed, or a daemon answered with bare txid strings. The caller
            # asked for detail; without it this block cannot be judged, and saying "not spent"
            # would be a guess dressed as an answer (rule 17).
            raise RegtestSetupError(
                "the daemon returned a block without transaction detail, so no spend could be "
                "looked for. `getblock(hash, true)` is what this needs"
            )
        for spend in transaction.get("vin", []):
            if spend.get("txid") == txid and int(spend.get("vout", -1)) == vout:
                return str(transaction.get("txid", "an unnamed transaction"))
    return None


def find_the_spender(run: Run, source: chain.Outpoint,
                     max_depth: int = MAX_SPEND_SCAN_BLOCKS) -> tuple[str | None, str]:
    """Walk the chain backward from the tip looking for whatever spent `source`. Reads only.

    THIS EXISTS BECAUSE THE OPERATOR'S DAEMON ANSWERS NONE OF THE EASY QUESTIONS, measured
    2026-09-28 on Gridcoin testnet rather than assumed:

        gettxout            absent
        importaddress       absent -- so the address cannot even be watched
        testmempoolaccept   absent, `code=-32601 Method not found`

    That last one is the finding that forced this function. Every pre-broadcast check in this
    repository went through `testmempoolaccept`, including the one `reclaim_funding.py` added on
    2026-09-28 to stop a dry run reporting a healthy spend of an output that was already gone.
    None of them ever ran on this daemon. They returned "" -- "the daemon will not say" -- and
    every caller read that as permission to continue. A check that cannot execute is not a
    weaker check, it is the absence of one, and it had been reported as passing.

    WHAT IS LEFT IS THE CHAIN ITSELF, and it is enough. `getblock(hash, true)` returns every
    transaction's inputs, so one call per block answers "did anything spend this outpoint" with
    certainty rather than inference. Bounded by MAX_SPEND_SCAN_BLOCKS, newest block first
    because a funding output is almost always consumed within a run or two of being made.

    RETURNS (spender, description) AND NEVER RAISES ON A DAEMON THAT WILL NOT PLAY. A daemon
    that cannot serve `getblock` leaves this returning (None, "could not scan: ...") and the
    caller proceeds exactly as it did before this existed -- a diagnostic must not become a gate
    of its own. The description is always non-empty for the same reason `mempool_answer`'s is:
    "no spender found" and "the scan did not happen" are different facts (rule 14).
    """
    node = run.node(wallet=False)
    try:
        tip = int(node.call("getblockcount"))
    except RPCError as error:
        return None, f"could not scan the chain for a spender: {error}"

    # THE CALLER MAY KNOW A TIGHTER FLOOR, AND IT IS EXACT RATHER THAN A HEURISTIC. Nothing
    # mined BEFORE the funding transaction can spend its output, so a payment with N
    # confirmations needs exactly N blocks looked at -- three for one made five minutes ago,
    # against 2000 for the blind cap. That is what makes this affordable on the happy path,
    # where discover_operator_funding_txid asks the same question of every candidate.
    floor = max(0, tip - max(1, max_depth) + 1)
    scanned = 0
    started = time.monotonic()
    run.say(
        f"scanning blocks {tip} down to {floor} for anything that spends "
        f"{source.txid[:16]}…:{source.vout}. This daemon has no gettxout, no importaddress and "
        f"no testmempoolaccept, so the chain itself is the only thing left that can answer"
    )
    for height in range(tip, floor - 1, -1):
        try:
            block = node.call("getblock", node.call("getblockhash", height), True)
        except RPCError as error:
            return None, (
                f"could not scan the chain for a spender: stopped at block {height} after "
                f"{scanned} block(s), {error}"
            )
        spender = spender_in_block(block, source.txid, source.vout)
        scanned += 1
        if spender:
            elapsed = format_duration(time.monotonic() - started)
            return spender, (
                f"SPENT ALREADY: block {height} contains {spender}, which spends this exact "
                f"outpoint. Found after {scanned} block(s), {elapsed}"
            )
        if scanned % 100 == 0:
            run.say(f"scanned {scanned} block(s), now at height {height}, no spender yet")
    elapsed = format_duration(time.monotonic() - started)
    return None, (
        f"no transaction in the last {scanned} block(s) spends this outpoint ({elapsed}). That "
        f"is NOT the same as 'unspent': the scan stops at {floor} and anything older is unseen"
    )


def refuse_if_the_funding_is_already_spent(
    run: Run, key: RegtestKey, source: chain.Outpoint, raw_hex: str
) -> None:
    """ASK BEFORE SENDING whether this split can be accepted, and name the likely cause if not.

    THE GAP THIS CLOSES WAS ALREADY WRITTEN DOWN ONE FUNCTION UP, in
    `discover_operator_funding_txid`: "an earlier one is most likely already spent by a previous
    run, and spending it again would fail as a double-spend several steps later with no clue
    why." Picking the NEWEST payment makes that unlikely -- and it does not make it impossible,
    because the newest payment IS the spent one on the second run of a harness the operator has
    not re-funded. That is the ordinary case after a successful run, not an exotic one: the first
    Gridcoin run consumed its funding by design.

    What the operator would otherwise see is `code=-22 message=TX rejected` and nothing else,
    which is every Gridcoin refusal (measured 2026-09-28) and says nothing about which of the
    dozen possible causes it was. `testmempoolaccept` runs the same AcceptToMemoryPool WITHOUT
    broadcasting and returns a structured reject-reason, so the question can be asked for free
    before anything moves.

    A `RegtestSetupError` and not a FAIL, deliberately: an unfunded run is a precondition the
    operator can fix in thirty seconds, not the code under test breaking, and the two need
    different things from whoever is reading the screen (rule 14).

    A daemon that will not answer gets "" from `mempool_reject_reason` and this function does
    NOTHING -- the send proceeds exactly as before. An absent diagnostic must never become a
    refusal of its own; it is a diagnostic, not a gate.
    """
    answer = mempool_answer(run, raw_hex)
    # SAY IT EVEN WHEN IT DOES NOT REFUSE. Rule 14: a check that prints nothing on the way past
    # is indistinguishable from a check that did not run, and on 2026-09-28 that difference was
    # the whole investigation -- the gate passed silently and the send then failed, so nobody
    # could tell whether the daemon had said "fine" or had not been asked at all.
    run.say(f"asked the daemon whether the split is acceptable BEFORE broadcasting -- {answer.description}")
    if not answer.reason:
        # THE DAEMON DID NOT REFUSE, WHICH IS NOT THE SAME AS SAYING IT IS FINE. On Gridcoin
        # testnet it cannot say anything at all -- `testmempoolaccept` is absent -- so the line
        # above reads "the call failed" and the check above this one has established nothing.
        # The chain is asked instead, and only when the cheap question came back empty: a
        # daemon that DID answer has already given a better answer than a block walk can.
        spender, description = find_the_spender(run, source)
        run.say(f"asked the chain instead -- {description}")
        if spender is None:
            return
        raise RegtestSetupError(
            f"{run.asset}: the operator's funding output has ALREADY BEEN SPENT.\n"
            f"  the output:   {source.txid}:{source.vout}, worth "
            f"{satoshis_to_coins(source.value_satoshis)} {run.asset}, at {key.address}\n"
            f"  spent by:     {spender}\n"
            f"  found by:     walking the chain, because this daemon has no gettxout, no "
            f"importaddress and no testmempoolaccept to ask directly\n"
            f"  Every run consumes its funding by design, and the harness picks the NEWEST "
            f"payment the wallet remembers -- which after a successful run IS the spent one.\n"
            f"  THE FIX: send another payment to {key.address} from your wallet, wait for one "
            f"confirmation, and run this again. The address is derived from "
            f"{FUNDING_SEED_VARIABLE} and does not change between runs.\n"
            f"  Nothing was funded, signed or broadcast."
        )
    reason = answer.reason
    raise RegtestSetupError(
        f"{run.asset}: the daemon will not accept the split of the operator's funding -- "
        f"reject-reason={reason!r}, asked via testmempoolaccept so NOTHING was broadcast.\n"
        f"  The output being spent is {source.txid}:{source.vout}, worth "
        f"{satoshis_to_coins(source.value_satoshis)} {run.asset}, at {key.address}.\n"
        f"  THE LIKELIEST CAUSE IS THAT A PREVIOUS RUN ALREADY SPENT IT. Each run consumes its "
        f"funding by design, and this daemon has no way to say what is unspent at an address it "
        f"does not own (importaddress and gettxout are both absent on v5.5.1.0), so the harness "
        f"picks the NEWEST payment the wallet remembers and cannot tell a fresh one from a "
        f"spent one.\n"
        f"  THE FIX: send another payment to {key.address} from your wallet, wait for one "
        f"confirmation, and run this again. The address is derived from "
        f"{FUNDING_SEED_VARIABLE} and does not change between runs."
    )


def send_the_split_or_explain_the_refusal(
    run: Run, key: RegtestKey, source: chain.Outpoint, raw_hex: str
) -> str:
    """Broadcast the split, and turn a bare `-22 TX rejected` into something readable.

    WHAT THE OPERATOR SAW ON 2026-09-28, and the reason this function exists: a forty-line
    Python traceback ending in `RPCError: sendrawtransaction: code=-22 message=TX rejected`.
    Every fact needed to diagnose it -- which output was being spent, what it was worth, what
    the daemon had said thirty microseconds earlier when asked the same question without
    broadcasting -- was in scope and none of it reached the screen. Rule 14 is explicit that
    pasted output has to be self-describing a day later, and a traceback is self-describing
    about Python rather than about the chain.

    RAISES RegtestSetupError AND NOT A FAIL, for the same reason the sufficiency check does: a
    refused split is a precondition the operator fixes, not the code under test breaking, and
    the two need different things from whoever is reading (rule 14 again).

    THE CANDIDATE CAUSES ARE NAMED RATHER THAN RANKED. Read out of Gridcoin's own source on
    2026-09-28, `AcceptToMemoryPool` returns false with NO reject reason in two places that
    matter here -- `txdb.ContainsTx(hash)` ("do we already have it?") and a failed
    `FetchInputs` -- and `sendrawtransaction` passes `nullptr` for `pfMissingInputs`, so it
    cannot distinguish them even internally. Guessing which one it was would be rule 17's
    failure exactly: a reason to believe is not a measurement. What this does instead is print
    the daemon's own answer to the same question and let the reader see which they have.
    """
    try:
        return str(run.node(wallet=False).call("sendrawtransaction", raw_hex))
    except RPCError as error:
        answer = mempool_answer(run, raw_hex)
        raise RegtestSetupError(
            f"{run.asset}: the daemon REFUSED the split of the operator's funding.\n"
            f"  the daemon said:            {error}\n"
            f"  asked again without sending: {answer.description}\n"
            f"  the output being spent:     {source.txid}:{source.vout}, worth "
            f"{satoshis_to_coins(source.value_satoshis)} {run.asset}, at {key.address}\n"
            f"  NOTHING ELSE WAS BROADCAST. The candidate causes, none of which this harness "
            f"can tell apart from a -22 alone:\n"
            f"    already spent   a previous run consumed this output. The likeliest, because "
            f"every run consumes its funding by design and this daemon has no `gettxout` or "
            f"`importaddress` to ask what is unspent at an address it does not own. "
            f"`testmempoolaccept` names this one `missing-inputs`.\n"
            f"    already mined   these exact bytes are already in a block, which Gridcoin's "
            f"AcceptToMemoryPool refuses with no reason at all.\n"
            f"    not yet mature  the funding payment is too recent for its output to be spent.\n"
            f"  THE FIX FOR THE FIRST TWO IS THE SAME: send another payment to {key.address} "
            f"from your wallet, wait for one confirmation, and run this again. The address is "
            f"derived from {FUNDING_SEED_VARIABLE} and does not change between runs. To recover "
            f"what is already out there, `python3 reclaim_funding.py --to-wallet` sweeps it."
        ) from error


def split_operator_funding(run: Run, key: RegtestKey, source: chain.Outpoint,
                           destinations: list[RegtestKey]) -> list[chain.Outpoint]:
    """One operator payment -> one P2PKH output per lock, signed HERE and broadcast.

    THIS IS THE WHOLE ROUTE, AND IT ASKS THE WALLET FOR NOTHING. The input is a P2PKH the
    harness holds the key for, so `_sign_p2pkh` signs it in this process exactly as it already
    signs Tx_lock's input -- no `signrawtransaction`, no `sendtoaddress`, no unlock consulted.
    Only `sendrawtransaction` touches the daemon, and it reads no lock state.

    ONE TRANSACTION RATHER THAN ONE PER LOCK, so the operator makes ONE payment. The outputs are
    sized exactly as _send_to_self() sizes its own, so everything downstream is unchanged.

    NO CHANGE OUTPUT, DELIBERATELY. Whatever is left over after the two funding outputs is left
    to the miner as fee rather than returned to the funding address, and that is a choice worth
    naming: a change output returning to the same address would be indistinguishable on chain
    from the operator's next funding payment, so find_operator_funding() could pick up the
    change of a previous run and fund a lock with the wrong outpoint. Overpaying a testnet fee
    costs test coins; funding from the wrong output costs a run and a confusing chase.
    """
    per_lock = coins_to_satoshis(
        str(Decimal(LOCK_COIN[run.asset]) + Decimal(FUNDING_HEADROOM_COIN[run.asset]))
    )
    needed = per_lock * len(destinations)

    # ONE nTime, READ ONCE, USED FOR BOTH BUILDS. Gridcoin serializes nTime between the version
    # and the input count, so a GRC transaction built without it is refused by the builder --
    # which is how a test caught this before it reached a chain, with `ntime=None` here. But the
    # subtler half is that this function builds the transaction TWICE: once to MEASURE the fee
    # and once to SIGN. Reading the clock separately for each would size the fee against
    # different bytes than the ones signed, and would make the predicted txid wrong, because the
    # txid depends on these bytes and the whole point of the check below is that the prediction
    # matches. `build_unsigned` refuses to read a clock itself for exactly this reason.
    ntime = int(time.time()) if run.asset == "GRC" else None
    outputs = [(per_lock, destination.p2pkh_script) for destination in destinations]
    # THE FEE IS SIZED AGAINST THESE OUTPUTS AND THE SURPLUS IS ADDED AFTERWARDS, which is safe
    # only because changing an output's VALUE cannot change the transaction's size: a value is
    # eight bytes whatever it holds. Adding an output would, which is why the surplus rides on
    # an existing one rather than becoming its own.
    fee = chain.fee_satoshis_for(
        run.asset,
        chain.build_unsigned(asset=run.asset, spends=source, outputs=outputs,
                             locktime=0, ntime=ntime),
        chain.p2pkh_script_sig_upper_bound(key.public_key),
    )
    if source.value_satoshis < needed + fee:
        raise RegtestSetupError(
            f"{run.asset}: the operator's funding output holds "
            f"{satoshis_to_coins(source.value_satoshis)} and this run needs "
            f"{satoshis_to_coins(needed)} for {len(destinations)} lock(s) plus "
            f"{satoshis_to_coins(fee)} of fee. Send at least "
            f"{satoshis_to_coins(needed + fee)} {run.asset} to {key.address} and pass that "
            f"txid. Nothing was funded, signed or broadcast."
        )

    # THE FEE THIS PRINTS IS THE FEE IT PAYS, and until 2026-09-28 it was not. The line used to
    # report `fee` -- the SIZE-BASED fee computed above for the sufficiency check -- while the
    # transaction actually pays input minus outputs, because there is no change output. With the
    # adaptor harness's three destinations the two numbers are within a few hundredths and
    # nobody noticed. grc_htlc_verify.py is the first caller with ONE destination, and on the
    # operator's run it printed `fee 0.01000000` for a transaction paying 3.09 GRC to the miner:
    # 4.60 in, 1.50 out, and 3.09 burned. Rule 14 says state what the number means next to the
    # number; a number that is not the quantity it is labeled with is worse than no number.
    #
    # NOT A REFUSAL, deliberately, and the reason is measured rather than assumed. Gridcoin's
    # AcceptToMemoryPool (src/validation.cpp, read 2026-09-28) has NO absurd-high-fee rejection
    # -- Bitcoin Core's `maxfeerate` / "max-fee-exceeded" has no counterpart on this chain -- so
    # an overpaid fee is accepted and burned rather than refused. It is test coin on a test
    # network and refusing here would strand the operator's funding behind a second reclaim for
    # no safety gained. What it needs is to be VISIBLE, which is what the surplus line does.
    surplus = source.value_satoshis - needed - fee
    # THE LAST OUTPUT TAKES THE SURPLUS RATHER THAN THE MINER. There is still no change output
    # -- the reasoning above holds, and change returning to the funding address would be
    # indistinguishable on chain from the operator's next payment -- but an existing output can
    # simply be larger, and that costs nothing and loses nothing.
    #
    # MEASURED 2026-09-28, TWICE, AND THE SECOND TIME IS WHY THIS EXISTS. First a 4.60 payment
    # against a 1.51 requirement burned 3.09. That was reported loudly and left alone, because
    # "a burned test coin costs less than a stranded one" and the surplus was a third of the
    # payment. Then LOCK_COIN["GRC"] dropped to 0.1, the ask became 0.16 -- and the operator's
    # next payment of 1.00 would have burned 0.84, five times the requirement. Lowering the ask
    # made overfunding the NORMAL case, and a note about it is not a fix.
    #
    # WHERE IT GOES AFTERWARDS DEPENDS ON THE CALLER, and both are better than a miner having
    # it. grc_htlc_verify's single destination passes the whole value into the contract, which
    # the refund returns. The adaptor harness's three locks each pay LOCK_COIN to a 2-of-2 and
    # leave the rest as Tx_lock's fee, so there the burn moves one hop rather than disappearing
    # -- no worse than it was, and it stops the split itself from being the thief.
    if surplus > 0 and outputs:
        value, script = outputs[-1]
        outputs[-1] = (value + surplus, script)
    run.say(
        f"splitting the operator's {satoshis_to_coins(source.value_satoshis)} into "
        f"{len(destinations)} funding output(s) of {satoshis_to_coins(per_lock)} each"
        + (f", and the LAST one also takes the {satoshis_to_coins(surplus)} of overfunding "
           f"rather than the miner" if surplus > 0 else "")
        + f". THE MINER TAKES {satoshis_to_coins(fee)}, which is the size-based fee. "
        f"Signed in THIS process -- the wallet is not asked"
    )
    unsigned = chain.build_unsigned(asset=run.asset, spends=source, outputs=outputs,
                                    locktime=0, ntime=ntime)
    script_sig = _sign_p2pkh(key, _p2pkh_sighash(unsigned, key))
    raw = unsigned.serialize({0: script_sig}).hex()
    predicted = chain.predicted_txid(unsigned, {0: script_sig})
    refuse_if_the_funding_is_already_spent(run, key, source, raw)
    txid = send_the_split_or_explain_the_refusal(run, key, source, raw)
    run.check("the split txid PREDICTED before broadcast equals the daemon's",
              f"predicted={predicted} daemon={txid}", "the same txid",
              OK if txid == predicted else FAIL)
    _mine(run, 1)
    return [chain.Outpoint(txid=txid, vout=index, value_satoshis=per_lock)
            for index in range(len(destinations))]


def funding_needed_coins(run: Run) -> str:
    """What one lock costs, fee included, as the operator should send it.

    ONE PLACE FOR THE NUMBER. The same arithmetic already lives in split_operator_funding's
    sufficiency check; a second hand-written copy is rule 8's failure with a delay on it, and
    this one would drift the first time LOCK_COIN changed.
    """
    total = (Decimal(LOCK_COIN[run.asset])
             + Decimal(FUNDING_HEADROOM_COIN[run.asset])
             + Decimal(SPLIT_FEE_ALLOWANCE_COIN))
    # str(), AND IT IS A FIX RATHER THAN AN ANNOTATION CHANGE. satoshis_to_coins() returns a
    # Decimal; this function is declared `-> str`; and the declaration is the one that is
    # right, because one of the two callers is operator_panel.funding_payload(), whose dict
    # goes through `json.dumps(handler())` for GET /api/funding.
    #
    # MEASURED 2026-10-09 at the interpreter: `json.dumps({"needed": Decimal("1.51010000")})`
    # raises `TypeError: Object of type Decimal is not JSON serializable`. The panel's
    # `guarded()` turns that into a readable 500, so the funding region of the page -- the
    # address, the amount to send, and every payment row -- rendered as a server error instead
    # of as the funding picture, on the one screen whose job is to tell the operator what to
    # send. Nothing about the NUMBER changes: `str(Decimal("1.51010000"))` is "1.51010000",
    # byte for byte what the f-string in no_usable_funding_message() already printed.
    #
    # regtest/operator_panel.PaymentRow.value_coins is the second declared-str-given-Decimal on
    # the same payload and is fixed in the same pass; it is the other half of the same route.
    return str(satoshis_to_coins(coins_to_satoshis(str(total))))


def no_usable_funding_message(run: Run, funding_key: RegtestKey) -> str:
    """Why the funded route cannot proceed, and the one thing that fixes it.

    WHAT THIS REPLACES, and the replacement is the point. On 2026-09-28 the picker correctly
    skipped three spent payments, found nothing usable, and fell through to the WALLET route --
    which on this daemon produced a page about staking-only unlocks, `ElevateToFull`, the GUI's
    RAII scope and passphrases in argv. Every sentence of it was true. None of it was the thing
    to fix: the operator needed to send 1.51 GRC to an address that message never mentioned.

    An accurate answer to a question nobody asked is rule 14's "state what the number means"
    failing at the scale of a whole screen, and it is worse than silence, because the reader
    goes and does the thing it describes.

    THE SEED'S PRESENCE IS WHAT MAKES THIS UNAMBIGUOUS. An operator who set the seed asked for
    the funded route; the wallet route is not a fallback for it, and on the daemon this exists
    for it cannot work at all.
    """
    lines = [f"{run.asset}: no USABLE payment to {funding_key.address} was found."]
    if run.skipped_funding_payments:
        lines.append(
            f"  {len(run.skipped_funding_payments)} payment(s) to it were found and every one is "
            f"already spent:"
        )
        lines += [f"    {txid}  spent by {spender}"
                  for txid, spender in run.skipped_funding_payments]
        lines.append(
            "  Each run consumes its funding by design, so this is the ordinary state after a "
            "run rather than anything going wrong."
        )
    else:
        lines.append(
            "  The wallet remembers no payment to it at all, which is the ordinary state before "
            "you have funded it."
        )
    lines += [
        f"  THE FIX: send {funding_needed_coins(run)} {run.asset} to {funding_key.address} and "
        f"wait for ONE confirmation. That address is derived from {FUNDING_SEED_VARIABLE} and "
        f"does not change between runs.",
        "  The wallet was NOT asked to create anything, so nothing here is about your unlock. "
        "Nothing was funded, signed or broadcast.",
    ]
    return "\n".join(lines)


def prepare_operator_funding(run: Run, funding_txid: str, destinations: list[RegtestKey]) -> None:
    """Find the operator's payment and split it into one input per lock, or do nothing.

    EXTRACTED FROM run_chain(), which ruff put at 55 statements against a ceiling of 50. Rule 12
    is explicit that the answer is to pull the decision out rather than raise the ceiling, and
    this is a decision rather than orchestration: whether this run funds from the wallet or from
    a payment the operator already made, and a refusal if the second was asked for and cannot be
    located.

    BOTH LOCKS FROM ONE PAYMENT, which is why this runs before either fund_and_prepare() call
    rather than inside them: the operator should make ONE payment, not one per lock, and a
    failure to find it has to refuse before anything is built or broadcast.
    """
    funding_key = operator_funding_key(run)
    if funding_key is None:
        if not funding_txid:
            return
        raise RegtestSetupError(
            f"{run.asset}: --funding-txid was given but {FUNDING_SEED_VARIABLE} is not set. The "
            f"funding address is DERIVED from that seed, so without it this harness cannot tell "
            f"which output of that transaction is its own. Nothing was funded, signed or "
            f"broadcast."
        )
    if not funding_txid:
        # WHAT STEP 5 ALREADY FOUND. Reusing its answer is not an optimization: step 5 decided
        # to carry on BECAUSE of that payment, so the split must be built from the same one.
        funding_txid = run.discovered_funding_txid
    if not funding_txid:
        # ASK THE WALLET BEFORE ASKING THE OPERATOR. It made the payment, so it knows the txid;
        # see discover_operator_funding_txid(). No payment yet is the ordinary state and falls
        # through to the wallet route, which then refuses and prints the address to fund.
        funding_txid = discover_operator_funding_txid(run, funding_key) or ""
    if not funding_txid:
        # A SEED IS CONFIGURED AND THERE IS NOTHING LEFT TO SPEND. Refuse HERE, naming the
        # address, rather than falling through to the wallet route.
        #
        # WHAT FALLING THROUGH ACTUALLY PRINTED, 2026-09-28, after the picker correctly skipped
        # three spent payments: a page about staking-only unlocks, ElevateToFull, the GUI's RAII
        # scope and passphrases in argv. Every sentence of it true, and none of it the thing to
        # fix -- the operator needed to send 1.51 GRC to an address the message did not even
        # mention. An accurate answer to a question nobody asked is rule 14's "state what the
        # number means" failing at the level of the whole message.
        #
        # The seed's presence is what makes this unambiguous: an operator who set
        # ST_ADAPTOR_FUNDING_SEED asked for the funded route, and the wallet route is not a
        # fallback for it -- on the daemon this exists for, the wallet route cannot work at all.
        raise RegtestSetupError(no_usable_funding_message(run, funding_key))
    run.say(
        f"funding from the operator's payment {funding_txid} to {funding_key.address} -- the "
        f"wallet will NOT be asked to create or sign anything"
    )
    source = find_operator_funding(run, funding_key, funding_txid)
    run.operator_funding = split_operator_funding(run, funding_key, source, destinations)


def fund_and_prepare(run: Run, label: str, key: RegtestKey) -> chain.Outpoint:
    """Prepare a P2PKH input this process holds the key for, sized for the lock plus a fee.

    TWO ROUTES, and the second exists because the first is closed on a staking-only wallet.

      WALLET      `sendtoaddress` -- the ordinary one, used whenever the wallet may create a
                  transaction. BTC and LTC regtest always take it.
      OPERATOR    a payment the operator already made, split here. Taken when
                  `run.operator_funding` has been prepared, which happens only when a funding
                  txid was supplied. It asks the wallet for NOTHING: the input is a P2PKH this
                  process holds the key for, so _sign_p2pkh() signs it here, and only
                  sendrawtransaction touches the daemon.
    """
    if run.operator_funding:
        outpoint = run.operator_funding.pop(0)
        run.say(
            f"lock {label} is funded from the operator's own payment at "
            f"{outpoint.txid[:16]}..:{outpoint.vout} -- the wallet was not asked"
        )
        return outpoint
    coin = str(Decimal(LOCK_COIN[run.asset]) + Decimal(FUNDING_HEADROOM_COIN[run.asset]))
    run.say(f"preparing the input for lock {label}: {coin} {run.asset}")
    return _send_to_self(run, key, coin)
