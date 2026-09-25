"""The nine stages, run against one chain.

Role: module (the stages the root entry point runs; each stage owns one
      question and holds no decision of its own -- the decisions are in
      regtest.txbuild, regtest.keys, and the REAL modules under test)
Reads: a regtest daemon over JSON-RPC; the real modules
       modules.atomic_htlc_scripts, modules.htlc_timelock, modules.utils,
       modules.atomic_btc_client, modules.atomic_ltc_client
Writes: the regtest chain and the regtest wallet -- mined blocks, funded
       contracts, broadcast spends
Can move funds: YES on regtest, and nowhere else. regtest.daemons.assert_regtest
       has already refused anything but `regtest` before any of this runs.
Mainnet-safe: NO.

WHAT IS UNDER TEST AND WHAT IS THE INSTRUMENT, BECAUSE THE DIFFERENCE IS THE
WHOLE VALUE OF THE OUTPUT.

  under test   modules/atomic_htlc_scripts.build_htlc_redeem_script -- the
               script, both branches, the locktime encoding.
               modules/htlc_timelock.contract_locktime -- the number in it.
               BTCClient.create_contract / redeem_contract and
               LTCClient.create_contract / redeem_contract -- the real fund and
               redeem paths, called with real arguments against a real daemon.

  instrument   regtest.txbuild -- a spender written for this harness, used
               because NO refund implementation exists in the tree to drive.
               Every line it produces on screen is labeled `control`.

THE FAILURE THIS HARNESS EXPECTS, AND WHY IT MUST NOT BE WRITTEN AROUND.

All three clients' `redeem_contract()` accept a `secret: bytes` parameter and
never reference it. That was established by walking each function's AST and is
recorded in all three module headers. What follows from it -- that the
scriptSig those functions build cannot satisfy the hashlock branch -- had
never been confirmed against a chain by anything in this repository. Step 7
is that confirmation. It is expected to FAIL, it is reported as XFAIL rather
than FAIL, and the assertion is not adjusted to let it pass.

Step 7 then runs the control redeem on the SAME contract output. Between the
two, the run answers the question that was previously unanswerable: whether a
funded contract is spendable by its hashlock branch at all, or only by refund.
If the real call fails and the control succeeds, the script is sound and the
client is the defect. If both fail, the script is the defect. Those two
verdicts have completely different fixes, and no amount of reading tells them
apart.

WHY STEP 8 HAS TWO PARTS.

"Refund before expiry is rejected" can be true for two unrelated reasons, and
only one of them says anything about CLTV:

  8a  nLockTime = the script's locktime, tip = locktime - 1. The MEMPOOL
      refuses it as non-final before any script runs. This proves the timelock
      is enforced end to end, and proves nothing at all about the opcode.
  8b  nLockTime = the current tip, which is final, so the script DOES run --
      and CHECKLOCKTIMEVERIFY compares the script's locktime against that
      smaller nLockTime and fails. This is the one that proves the opcode is
      enforcing something, and it is the assertion the refund branch has never
      had.

Both must be rejected. A refund that succeeds at 8b would mean the locktime in
the script is not the one the builder was asked for -- which is exactly the
defect this repository shipped until 2026-09-24, when the locktime was encoded
as a varint and 500,000 was enforced as 128,000,254.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from decimal import Decimal

from chains.base import RPCError
from microfortnights import format_duration
from modules.atomic_htlc_scripts import (
    build_htlc_redeem_script,
    parse_and_reencode_as_testnet_p2pkh,
    script_to_p2sh_address,
)
from modules.htlc_timelock import ROLE_INITIATOR, contract_locktime, describe_locktime
from modules.utils import generate_secret, sha256_hash
from regtest import daemons
from regtest.console import FAIL, OK, SKIP, XFAIL, Console, redact
from regtest.daemons import (
    COINBASE_MATURITY_HEIGHT,
    ChainConfig,
    RegtestSetupError,
    cltv_activation_height,
    daemon_help_text,
    describe_rpc_exception,
    mweb_override_args,
)
from regtest.keys import RegtestKey, generate_key, hash160
from regtest.txbuild import Outpoint, build_branch_spend, coins_to_satoshis, describe_script_sig

TOTAL_STEPS = 9

# How much goes into each contract, and the fee each control spend pays. Both
# are coin amounts as strings so the satoshi conversion is exact.
CONTRACT_AMOUNT = "1.0"
CONTROL_FEE = "0.0001"

# How many contract outputs one run funds: [A] for the redeem attempts, [B] for
# the refund, [C] the unconfirmed copy step 7b needs, [D] the one step 8c tries
# to mine into a block. A COUNT, checked against the wallet balance in step 4.
CONTRACTS_PER_RUN = 4

# generatetoaddress is called in batches so that mining a four-figure number of
# blocks prints progress instead of sitting silent (rule 14). A COUNT OF
# BLOCKS, never a duration.
MINE_BATCH_BLOCKS = 144

# sendrawtransaction's maxfeerate, in coin per kvB. Zero disables the check.
# The control spends pay a flat 0.0001 on a ~250-byte transaction, which is
# roughly 0.4 coin/kvB -- four times the 0.10 default -- so without this every
# control spend is refused as `absurdly-high-fee` and the harness reports a fee
# policy as if it were a script failure. The flat fee is kept (rather than
# reduced) because it is the same constant the real redeem_contract() hardcodes,
# and a harness that quietly pays a different fee is not measuring the same
# thing.
NO_FEE_LIMIT = 0

# The `max_wait` BTCClient.create_contract() passes to wait_for_tx_output().
# SECONDS, because that is the unit the real client uses at its own call site;
# it is rendered in microfortnights wherever the harness prints it (rule 6).
# Spelled here only so the announcement can state the scale up front.
REAL_CLIENT_WAIT_SECONDS = 300


@dataclass
class ChainFacts:
    """What step 2 learned from the daemon that a later step has to act on.

    Mutable and carried on the frozen Run for the same reason SpawnRecord is:
    it must be set the moment it is known, not passed through the return value
    of a function that can raise before it returns.
    """

    cltv_activation_height: int | None = None
    cltv_height_source: str = "(none: not looked up yet)"


@dataclass
class SpawnRecord:
    """Whether THIS run spawned the daemon, recorded the instant it happens.

    THIS EXISTS BECAUSE THE OBVIOUS VERSION LEAKED A PROCESS (rule 13). It was
    first written as `we_started_it = steps.step_2_daemon(run)` in the entry
    point, which assigns only if step 2 RETURNS. Step 2 spawns the daemon and
    then waits up to a minute for RPC, and that wait raises RegtestSetupError
    on timeout -- so a daemon that started but answered slowly was spawned,
    never recorded, and then deliberately left running by the teardown, which
    believed it had adopted something it had actually started. The next run
    fails on the datadir lock, which reads like a permissions problem.

    A mutable record set BEFORE anything that can raise closes that window:
    the spawn and its reap are one change, and the reaper must never depend on
    the spawner returning normally.
    """

    started: bool = False


@dataclass(frozen=True)
class Run:
    """The three things every stage needs: where to print, which chain, which wallet.

    They travel together because they ARE one thing -- a run against one chain
    -- and passing them separately pushed most of this file's functions past
    the argument ceiling. The ceiling was right: a stage that takes
    (console, config, wallet, contract, outpoint, outcome) is orchestration
    carrying its context by hand. CLAUDE.md rule 12 is explicit that the fix
    for a complexity finding is to extract, not to raise the ceiling, and rule
    19 forbids a `noqa` that only quiets the count.
    """

    console: Console
    config: ChainConfig
    wallet: str = ""
    # Mutable on purpose, inside a frozen Run: see SpawnRecord. The teardown in
    # regtest_htlc_verify.main() reads this, and it must be true by the time
    # anything after the spawn can raise.
    spawn: SpawnRecord = field(default_factory=SpawnRecord)
    facts: ChainFacts = field(default_factory=ChainFacts)

    @property
    def asset(self) -> str:
        return self.config.asset

    def node(self, wallet: str | None = None):
        """An RPCAdapter for this chain, at the wallet endpoint unless told otherwise."""
        return daemons.adapter_for(self.config, wallet=self.wallet if wallet is None else wallet)

    def say(self, text: str) -> None:
        """A detail line, already prefixed with which chain is speaking."""
        self.console.say(f"{self.asset}: {text}")

    def check(self, label: str, got: object, expected: object, outcome: str) -> str:
        return self.console.check(f"{self.asset} {label}", got, expected, outcome)

    def step(self, number: int, title: str) -> None:
        self.console.step(number, self.asset, title)


@dataclass
class ChainOutcome:
    """What this chain's run established, for the verdict at the end."""

    asset: str
    real_create_contract: str = SKIP
    real_redeem: str = SKIP
    # WHICH STAGE the real redeem failed at, when it failed. Recorded because
    # the verdict must not say "redeem_contract() cannot spend the hashlock
    # branch" about a call that never reached the signing step -- see
    # classify_redeem_failure().
    real_redeem_stage: str = ""
    # 7a: the same call against a CONFIRMED contract, which is the path a real
    # swap takes. Recorded separately because it fails for a different reason
    # and is a finding in its own right.
    real_redeem_confirmed: str = SKIP
    real_redeem_confirmed_stage: str = ""
    # Whether any attempt got far enough for the node to be asked to build a
    # scriptSig. When this is False the run says nothing about the preimage,
    # and the verdict must not pretend otherwise.
    reached_the_signer: bool = False
    control_redeem: str = SKIP
    refund_before_expiry_rejected: str = SKIP
    # WHICH layer refused the early refund, and whether the chain itself was
    # asked. `policy` is a weaker result than `consensus` and the verdict says
    # so rather than scoring them alike.
    refund_refusal_kind: str = ""
    cltv_consensus_refused: str = SKIP
    cltv_activation_height: int | None = None
    height_at_refund_test: int | None = None
    refund_after_expiry: str = SKIP
    notes: list[str] = field(default_factory=list)

    def verdict(self) -> str:
        """One sentence naming which branch of a funded contract actually works.

        This is the single most valuable line the harness prints, so it is
        assembled from the recorded outcomes rather than from anything a step
        decided in passing -- and it distinguishes THREE states per branch, not
        two:

            OK    the branch was exercised and spent.
            FAIL  the branch was exercised and did not spend.
            SKIP  the branch was NEVER EXERCISED.

        MEASURED 2026-09-25, and this is why the third state had to exist. On
        LTC, mining toward the locktime failed, so steps 8 and 9 never ran --
        and the verdict said "the hashlock branch spends and the REFUND branch
        does not". That asserted a conclusion the run did not reach, about the
        same script builder BTC had just proven correct on the same day. A step
        that did not execute is not evidence about the branch it would have
        exercised, and saying otherwise is worse than saying nothing.
        """
        hashlock_by_real_client = self.real_redeem == OK
        hashlock_by_control = self.control_redeem == OK
        timelock_ran = self.refund_after_expiry in (OK, FAIL)
        timelock_ok = self.refund_after_expiry == OK

        if not timelock_ran:
            return self._verdict_without_a_refund_test(hashlock_by_real_client or hashlock_by_control)

        if hashlock_by_real_client and timelock_ok:
            return "both branches spend: the real redeem_contract() worked and the refund worked"

        if hashlock_by_control and timelock_ok:
            return self._verdict_with_a_working_script()

        if timelock_ok and not hashlock_by_control:
            return (
                "ONLY THE REFUND BRANCH SPENDS. Neither the real client nor a correctly built scriptSig could take "
                "the hashlock branch, which points at the redeem SCRIPT rather than at the client."
            )

        if hashlock_by_control and not timelock_ok:
            return (
                "the hashlock branch spends and the REFUND branch was exercised and did NOT spend -- the timelock "
                "path is broken"
            )

        return "neither branch was shown to spend; read the failures above before concluding anything"

    def cltv_verdict(self) -> str:
        """What this chain established about CHECKLOCKTIMEVERIFY, at the right strength.

        Printed beside the branch verdict rather than folded into it, because
        they answer different questions: the branch verdict says whether the
        coins can move, and this says how strong the evidence is that the
        timelock is what stopped them moving early.

        THE DISTINCTION IS PER CHAIN AND IS NOT COSMETIC. Measured 2026-09-25,
        before this was fixed: BTC refused the early refund with
        `mandatory-script-verify-flag-failed` and LTC with
        `non-mandatory-script-verify-flag`, and the harness reported them as
        the same result. They are not. The second means the transaction passed
        consensus and was declined by relay policy, at heights below the 1351
        at which the Litecoin daemon's own table says BIP65 activates.
        """
        if self.refund_before_expiry_rejected == SKIP:
            return "CHECKLOCKTIMEVERIFY: NOT TESTED on this chain -- step 8 did not run."

        where = (
            f"the test ran at height {self.height_at_refund_test} and the daemon puts BIP65 activation at "
            f"{self.cltv_activation_height}"
            if self.cltv_activation_height is not None
            else f"the test ran at height {self.height_at_refund_test} and this daemon did NOT report a BIP65 "
            "activation height, so consensus enforcement cannot be established from the height alone"
        )

        if self.cltv_consensus_refused == OK:
            return (
                "CHECKLOCKTIMEVERIFY is enforced BY CONSENSUS: the daemon refused to mine the early refund into a "
                f"block at all (step 8c), which is the chain refusing rather than the mempool. Mempool refusal was "
                f"'{self.refund_refusal_kind}'; {where}."
            )
        if self.cltv_consensus_refused == FAIL:
            return (
                "CHECKLOCKTIMEVERIFY IS NOT ENFORCED BY CONSENSUS HERE: the daemon MINED the early refund into a "
                f"block (step 8c). The mempool refusal was '{self.refund_refusal_kind}' and is relay policy only, "
                f"so a miner could include an early refund. {where}."
            )
        if self.refund_refusal_kind == REFUSAL_CONSENSUS:
            return (
                "CHECKLOCKTIMEVERIFY refused the early refund with `mandatory-script-verify-flag-failed`, which is "
                f"the script failing under the flags the chain applies. Step 8c was not run ({where})."
            )
        if self.refund_refusal_kind == REFUSAL_POLICY:
            return (
                "CHECKLOCKTIMEVERIFY evidence here is WEAK: the early refund was refused with "
                "`non-mandatory-script-verify-flag`, meaning it PASSED consensus and was declined by relay policy. "
                f"A miner not applying that policy could have included it. {where}."
            )
        return (
            f"CHECKLOCKTIMEVERIFY: the early refund was refused, but the refusal was '{self.refund_refusal_kind}', "
            f"which does not by itself say whether consensus or policy refused it. {where}."
        )

    def _verdict_with_a_working_script(self) -> str:
        """Both branches spend under a correct scriptSig. What can be said about the CLIENT?"""
        if not self.reached_the_signer:
            # The real call fell over before the node was ever asked to build a
            # scriptSig, so this run cannot say whether it would have pushed the
            # preimage. Say what was measured, and no more (rule 17).
            return (
                "the SCRIPT is sound -- both branches spend when the scriptSig is built correctly. The real "
                f"redeem_contract() was NOT judged on the hashlock branch: it failed earlier (stage "
                f"'{self.real_redeem_stage or self.real_redeem_confirmed_stage}') and never reached signing. "
                "What is measured is that a funded contract is spendable by refund and by a correctly built "
                "hashlock spend; whether redeem_contract() pushes the preimage remains inferred from the source."
            )
        return (
            "the SCRIPT is sound -- both branches spend when the scriptSig is built correctly -- but the real "
            f"redeem_contract() CANNOT spend the hashlock branch (it failed at the '{self.real_redeem_stage}' "
            "stage, with the node asked and refusing). A funded contract is recoverable in practice only by "
            "refund, until redeem_contract() pushes the preimage."
        )

    def _verdict_without_a_refund_test(self, hashlock_spends: bool) -> str:
        """Steps 8 and 9 never executed. Say what was and was not measured, and stop."""
        if hashlock_spends:
            return (
                "the HASHLOCK branch spends. The REFUND branch was NOT TESTED on this chain -- steps 8 and 9 did "
                "not run -- so this run says nothing about it in either direction. Read the failure above for why "
                "they did not run; a step that did not execute is not evidence about the branch it would have "
                "exercised."
            )
        return (
            "NOTHING WAS ESTABLISHED about either branch on this chain. The refund branch was never exercised "
            "(steps 8 and 9 did not run) and the hashlock branch did not spend. Read the failures above."
        )


@dataclass
class Contract:
    """One funded HTLC output and everything needed to spend it."""

    redeem_script: bytes
    p2sh_address: str
    p2sh_script: bytes
    locktime: int
    secret: bytes
    secret_hash: bytes
    participant: RegtestKey
    refund: RegtestKey
    outpoint: Outpoint | None = None
    funded_by: str = "(none: not funded)"


def _p2sh_script_for(redeem_script: bytes) -> bytes:
    """OP_HASH160 <20-byte script hash> OP_EQUAL.

    Built from the hash rather than from a rendered address on purpose. Bitcoin
    and Litecoin disagree about the base58 version byte for P2SH on
    testnet/regtest -- 0xC4 on Bitcoin, and Litecoin prints 0x3A while still
    accepting 0xC4 -- so an assertion written against an address STRING fails on
    one chain for a reason that has nothing to do with the contract. The
    scriptPubKey is the same bytes on both.
    """
    script_hash = hash160(redeem_script)
    return b"\xa9" + bytes([len(script_hash)]) + script_hash + b"\x87"


# --------------------------------------------------------------------------
# steps 1-4: the chain is up, on regtest, with a wallet and spendable coins
# --------------------------------------------------------------------------


def step_1_binaries(run: Run) -> None:
    run.step(1, "binaries present, versions printed")
    daemons.check_binaries(run.console, run.config)


def apply_mweb_override(run: Run) -> None:
    """Ask THIS litecoind whether it can hold MWEB inactive, and set it if so.

    Runs between steps 1 and 2 -- after the binary is confirmed present, before
    it is started -- because it works by reading the binary's own `-help`
    output. See regtest.daemons.mweb_override_args for the measurement that
    made this necessary and for why the flag is discovered rather than
    remembered.

    Everything it decides is printed. An override that could not be applied is
    a result and says so; it is not a silent no-op.
    """
    args, explanation = mweb_override_args(daemon_help_text(run.config.daemon_path))
    run.say(f"MWEB deployment override: {explanation}")
    if args:
        run.config.extra_args.extend(args)
        run.say(f"MWEB deployment override: passing {' '.join(args)} to the daemon")
    else:
        run.say(
            "MWEB deployment override: none applied. If mining toward the locktime fails with bad-txns-vin-empty, "
            "that is the failure this would have prevented, and step 2's softfork listing says whether MWEB is why."
        )


def step_2_daemon(run: Run) -> bool:
    run.step(2, f"start the daemon on regtest and wait for RPC at {run.config.base_url}")
    run.say(
        f"datadir={run.config.datadir}  conf={run.config.conf_path}  "
        f"rpcport={run.config.port}  rpcuser={run.config.rpc_user}"
    )
    we_started_it = daemons.start_daemon(run.console, run.config)
    # Recorded BEFORE the wait below, which can raise. See SpawnRecord.
    run.spawn.started = we_started_it
    daemons.wait_for_rpc(run.console, run.config)
    # THE UNCONDITIONAL REFUSAL, and it is the first thing asserted after the
    # connection exists. Everything below this line mines or broadcasts.
    info = daemons.assert_regtest(run.console, run.config)
    run.say(f"chain={info.get('chain')} blocks={info.get('blocks')} (a height, not a duration)")

    # BIP65 IS CHECKLOCKTIMEVERIFY, and where it activates decides whether the
    # refund test measures consensus or merely relay policy. Read now, acted on
    # in step 4, which mines above it before step 5 derives a locktime from the
    # tip. See daemons.cltv_activation_height for the run that made this
    # necessary.
    height, source = cltv_activation_height(run.node(wallet=""), info)
    run.facts.cltv_activation_height = height
    run.facts.cltv_height_source = source
    if height is None:
        run.say(f"CHECKLOCKTIMEVERIFY activation height: (none) -- {source}")
        run.say(
            "so this run CANNOT state that CLTV was consensus-enforced when the refund is asserted. Step 8b's "
            "refusal kind is then the only evidence, and it is reported as such rather than assumed adequate."
        )
    else:
        run.say(f"CHECKLOCKTIMEVERIFY becomes consensus-enforced at height {height} (a height, not a duration)")
        run.say(f"read from {source}")
    return we_started_it


def step_3_wallet(run: Run) -> dict:
    run.step(3, f"create or load the wallet {run.wallet!r}")
    daemons.ensure_wallet(run.console, run.config, run.wallet)
    capabilities = daemons.probe_capabilities(run.console, run.config, wallet=run.wallet)
    if capabilities.get("descriptor_wallet") is True:
        run.say(
            "this is a DESCRIPTOR wallet. importaddress and importprivkey exist but refuse on one, so the real "
            "create_contract()'s import step is expected to fail here -- that failure is reported, not worked around."
        )
    return capabilities


@dataclass(frozen=True)
class Mined:
    """What a mining call produced: the new tip, and the hashes of the blocks.

    THE HASHES ARE THE POINT, and they were being thrown away. `generatetoaddress`
    already returns them, and a block hash is what lets `getrawtransaction` read
    a CONFIRMED transaction back on a node with no `-txindex`. See _verbose_tx()
    for the failure that taught this.
    """

    height: int
    hashes: list[str]

    @property
    def first_hash(self) -> str | None:
        """The block a transaction broadcast just before this mine landed in.

        None rather than an exception when nothing was mined: the caller passes
        it to _verbose_tx(), which has two further routes, and a missing hash
        should degrade to the next route rather than abort the step.
        """
        return self.hashes[0] if self.hashes else None


def _mine(run: Run, blocks: int) -> Mined:
    """Mine `blocks` blocks in batches, printing progress. Returns tip and hashes."""
    node = run.node()
    address = node.call("getnewaddress", "regtest-harness-mining")
    started = time.monotonic()
    remaining = blocks
    hashes: list[str] = []
    while remaining > 0:
        batch = min(MINE_BATCH_BLOCKS, remaining)
        try:
            hashes.extend(node.call("generatetoaddress", batch, address) or [])
        except RPCError as exc:
            raise _mining_failure(run, exc, blocks - remaining, blocks) from exc
        remaining -= batch
        height = int(node.call("getblockcount"))
        run.say(
            f"mined {blocks - remaining}/{blocks} blocks, height={height} (a height, not a duration), "
            f"{format_duration(time.monotonic() - started)} elapsed"
        )
    return Mined(height=int(node.call("getblockcount")), hashes=hashes)


# The daemon's wording when a block it assembled for itself fails validation
# with a transaction that has no inputs. See _mining_failure().
_VIN_EMPTY_MARKERS = ("bad-txns-vin-empty", "testblockvalidity")


def _mining_failure(run: Run, exc: RPCError, mined_so_far: int, requested: int) -> RegtestSetupError:
    """Turn a generatetoaddress refusal into something an operator can act on.

    MEASURED ON THE OPERATOR'S MACHINE 2026-09-25: mining toward the LTC
    locktime ran 288 blocks and then the daemon refused to assemble the next
    one, reporting a transaction with no inputs. That ended the LTC run before
    steps 8 and 9, and the only thing on screen was the RPC error.

    A block the daemon built ITSELF failing its own validity check is not
    something this harness can cause -- it broadcasts nothing between mining
    batches -- so the diagnosis names the leading hypothesis, points at the
    evidence printed earlier in the run, and says plainly that it is a
    hypothesis. It does NOT shorten the locktime to route around it: 1152
    blocks is what modules/htlc_timelock.py derives for a 48-hour LTC lock, and
    a test that quietly uses a different number is measuring a different
    contract.
    """
    lowered = str(exc).lower()
    detail = (
        f"{run.asset}: mining failed after {mined_so_far} of {requested} blocks "
        f"(a count of blocks, not a duration): {exc}"
    )
    if not any(marker in lowered for marker in _VIN_EMPTY_MARKERS):
        return RegtestSetupError(
            f"{detail}. This harness broadcasts nothing between mining batches, so a block the daemon assembled "
            "for itself failing validation is a daemon-side condition. Read step 2's softfork listing above."
        )
    applied = " ".join(run.config.extra_args) or "(none: no deployment override was applied)"
    return RegtestSetupError(
        f"{detail}. A transaction with NO INPUTS in a block the daemon built for itself points at Litecoin's "
        "Mimblewimble Extension Blocks: MWEB activates BY HEIGHT and adds an integrating HogEx transaction whose "
        "input structure a generic transaction check can read as vin-empty, which is why several hundred blocks "
        "mined first and then one did not. THAT IS A HYPOTHESIS, not a measurement -- step 2 printed this daemon's "
        "own softfork table, including MWEB's status and activation height, and that is the evidence to read. "
        f"Deployment options this harness passed: {applied}. If none were applied, this build did not advertise "
        "-vbparams; rerun with --wipe after adding one by hand if your build takes a different flag. The locktime "
        "is NOT shortened to avoid this: 1152 blocks is what a 48-hour LTC lock derives to."
    )


def _verbose_tx(node, txid: str, block_hash: str | None = None) -> dict:
    """Read a transaction back as a decoded dict, WITHOUT requiring -txindex.

    MEASURED ON THE OPERATOR'S MACHINE, 2026-09-25, first run of this harness.
    The funding transaction was sent and mined, and then:

        RPCError: {'code': -5, 'message': 'No such mempool transaction. Use
        -txindex or provide a block hash to enable blockchain transaction
        queries. Use gettransaction for wallet transactions.'}

    A bare `getrawtransaction <txid> true` only ever searched the MEMPOOL, and
    the transaction had just been mined out of it. This killed BTC before steps
    7, 8 and 9 ran -- so the one question the harness exists to answer went
    unmeasured because of a lookup, not because of anything about HTLCs.

    The daemon's own error names all three fixes and this takes two of them,
    in order, and deliberately not the third:

      1. WITH A BLOCK HASH. `getrawtransaction <txid> true <blockhash>` reads
         the block directly. Every caller here mines the block itself and so
         already knows the hash -- `generatetoaddress` returns it -- which
         makes this the exact route the daemon asked for. It also works for a
         transaction the WALLET DOES NOT OWN, which the redeem and refund
         spends are: their inputs are a P2SH the wallet never imported and
         their outputs pay keys generated inside this harness.

      2. THE WALLET'S OWN RECORD. `gettransaction <txid>` needs no index and
         carries the raw hex plus the confirmation count, which
         `decoderawtransaction` turns into the same shape. It works on both
         Bitcoin Core 28.1 and Litecoin 0.21.4. It only knows transactions the
         wallet was involved in, which covers the funding but not the spends.

      3. NOT `-txindex=1`. Adding it to the daemon's arguments changes the
         datadir's indexes and forces a reindex on an operator who already has
         a regtest chain. A harness must not silently re-shape the machine it
         is measured on.

    The confirmation count is spliced in from whichever route supplied it,
    because `decoderawtransaction` does not carry one and every caller asserts
    on it.
    """
    attempts: list[str] = []
    if block_hash:
        try:
            return node.call("getrawtransaction", txid, True, block_hash)
        except Exception as exc:  # noqa: BLE001 -- checked: a failure of ONE route is not an answer, it is a reason to try the next. Every attempt is collected and re-raised together below, so no route can return a value a caller would mistake for a real transaction.
            attempts.append(f"getrawtransaction with blockhash: {exc}")
    try:
        wallet_record = node.call("gettransaction", txid)
        decoded = node.call("decoderawtransaction", wallet_record["hex"])
        decoded["confirmations"] = int(wallet_record.get("confirmations", 0))
        if wallet_record.get("blockhash"):
            decoded["blockhash"] = wallet_record["blockhash"]
        return decoded
    except Exception as exc:  # noqa: BLE001 -- checked: same. This route only knows wallet transactions, so its failure is expected for the redeem and refund spends and must not end the step.
        attempts.append(f"gettransaction + decoderawtransaction: {exc}")
    try:
        return node.call("getrawtransaction", txid, True)
    except Exception as exc:  # noqa: BLE001 -- checked: the last route. Its failure ends the search, and the RegtestSetupError below carries every attempt so the operator sees which three things were tried rather than only the last.
        attempts.append(f"getrawtransaction without blockhash (needs -txindex): {exc}")
    raise RegtestSetupError(
        f"could not read transaction {txid} back from the node. Tried, in order: "
        + "; ".join(attempts)
        + ". The harness does NOT add -txindex=1, because that would reindex a datadir the operator already has."
    )


def step_4_maturity(run: Run) -> int:
    """Coinbase maturity, AND past CHECKLOCKTIMEVERIFY's activation height.

    THE SECOND HALF IS NOT HOUSEKEEPING, it is what makes step 8 mean anything.

    The locktime step 5 derives is `tip + 1152` on LTC and `tip + 288` on BTC.
    Leaving the tip at 101 put LTC's locktime at 1253 -- BELOW the height 1351
    at which the daemon's own softfork table says BIP65 becomes consensus
    enforced -- so the refund assertion ran in a window where the chain would
    not have refused an early refund and only the mempool's relay policy did.
    Measured 2026-09-25; see daemons.cltv_activation_height.

    Mining past the activation height FIRST moves the whole test above it: a
    tip of 1351 gives a locktime of 2503 on LTC, and step 8 runs at 2502. The
    1152-block lock is NOT shortened to make this cheaper -- that number is
    what modules/htlc_timelock.py derives for a 48-hour LTC lock, and a test
    that uses a convenient number tests a number nobody ships.
    """
    activation = run.facts.cltv_activation_height
    target = max(COINBASE_MATURITY_HEIGHT, activation or 0)
    run.step(4, f"mine to coinbase maturity ({COINBASE_MATURITY_HEIGHT}) and past CLTV activation, to height >= {target}")
    node = run.node()
    height = int(node.call("getblockcount"))
    run.say(f"current height={height}; maturity needs {COINBASE_MATURITY_HEIGHT}")
    if activation is None:
        run.say(
            "CLTV activation height is (none: this daemon did not report one), so the tip is taken to maturity "
            "only. Step 8 will say that consensus enforcement could not be established from the height."
        )
    else:
        run.say(
            f"CLTV is consensus-enforced from height {activation}, so the tip goes past it BEFORE step 5 derives a "
            "locktime from it -- otherwise the refund assertion measures relay policy, not consensus"
        )
    if height < target:
        height = _mine(run, target - height).height
    run.check("chain height", height, f">= {target}", OK if height >= target else FAIL)
    if activation is not None:
        run.check(
            "tip is at or above the CLTV activation height",
            f"{height} vs activation {activation}",
            f">= {activation}",
            OK if height >= activation else FAIL,
        )

    balance = Decimal(str(node.call("getbalance")))
    needed = Decimal(CONTRACT_AMOUNT) * CONTRACTS_PER_RUN + Decimal("0.01")
    run.check(
        "spendable wallet balance",
        f"{balance} ({CONTRACTS_PER_RUN} contracts of {CONTRACT_AMOUNT} plus fees need about {needed})",
        f">= {needed}",
        OK if balance >= needed else FAIL,
    )
    if balance < needed:
        raise RegtestSetupError(
            f"{run.asset}: wallet balance {balance} is below the {needed} this run needs. On a fresh regtest chain, "
            "mine more blocks -- or rerun with --wipe if a previous run left the wallet holding only immature "
            "coinbases."
        )
    return height


# --------------------------------------------------------------------------
# step 5: the contract, built by the REAL builder with a REAL derived locktime
# --------------------------------------------------------------------------


def step_5_build_contract(run: Run, height: int) -> Contract:
    run.step(5, "build an HTLC with the real script builder and a real derived locktime")

    # The preimage comes from the real generator, and the ONLY thing printed
    # about it is its SHA-256 hash. modules/utils.generate_secret() used to log
    # the preimage itself at DEBUG through a handler it installed at import;
    # that is fixed, and this harness must not reintroduce it by another route.
    secret = generate_secret()
    secret_hash = sha256_hash(secret)
    run.say(f"preimage generated -- {redact(secret)}")
    run.say(f"secret_hash={secret_hash.hex()} (public by construction; it goes in the script)")

    participant = generate_key()
    refund = generate_key()
    run.say(f"participant address={participant.address} (the counterparty's: redeems with the preimage)")
    run.say(f"refund address={refund.address} (ours: claims back after the locktime)")

    # If the builder's re-encoder changed either address, the hash160 in the
    # script would not be the one our key hashes to, and every spend would fail
    # at OP_EQUALVERIFY for a reason that looks like a signing bug. Assert the
    # round trip rather than assume it.
    for label, key in (("participant", participant), ("refund", refund)):
        reencoded = parse_and_reencode_as_testnet_p2pkh(key.address)
        run.check(
            f"{label} address survives the builder's re-encoding",
            reencoded,
            key.address,
            OK if reencoded == key.address else FAIL,
        )

    locktime = contract_locktime(run.asset, ROLE_INITIATOR, height)
    run.say(describe_locktime(run.asset, ROLE_INITIATOR, height, locktime))

    redeem_script = build_htlc_redeem_script(
        secret_hash=secret_hash.hex(),
        participant_address=participant.address,
        refund_address=refund.address,
        locktime=locktime,
    )
    run.say(f"redeem script ({len(redeem_script)} bytes) = {redeem_script.hex()}")

    p2sh_address = _report_p2sh_address(run, redeem_script)
    return Contract(
        redeem_script=redeem_script,
        p2sh_address=p2sh_address,
        p2sh_script=_p2sh_script_for(redeem_script),
        locktime=locktime,
        secret=secret,
        secret_hash=secret_hash,
        participant=participant,
        refund=refund,
    )


def _report_p2sh_address(run: Run, redeem_script: bytes) -> str:
    """Compare the builder's P2SH address with the node's, and pick one to fund.

    They can differ without the contract differing. modules/atomic_htlc_scripts
    hardcodes Bitcoin testnet's P2SH version byte 0xC4; Litecoin prints P2SH
    addresses with 0x3A while still decoding 0xC4, so on LTC the two strings
    disagree and the scriptPubKey behind them is identical. The harness funds
    the node's rendering when it has one, and asserts on the scriptPubKey hex
    everywhere afterwards, so nothing downstream depends on which was chosen.
    """
    node = run.node()
    builder_address = script_to_p2sh_address(redeem_script)
    decoded = node.call("decodescript", redeem_script.hex())
    node_address = decoded.get("p2sh")
    run.say(f"builder's P2SH address={builder_address}  node's decodescript p2sh={node_address}")
    if node_address and node_address != builder_address:
        run.say(
            "the two DIFFER. That is a base58 version byte difference, not a different contract -- the "
            "scriptPubKey asserted on below is identical either way."
        )
    validity = node.call("validateaddress", builder_address)
    run.check(
        "node accepts the builder's own P2SH address",
        validity.get("isvalid"),
        True,
        OK if validity.get("isvalid") else FAIL,
    )
    return node_address or builder_address


# --------------------------------------------------------------------------
# step 6: fund it, mine, and confirm the P2SH output is on chain
# --------------------------------------------------------------------------


def _find_vout_by_script(raw_tx: dict, script_hex: str) -> int | None:
    """Locate the output paying a given scriptPubKey.

    Matched on the scriptPubKey HEX, not on `scriptPubKey.addresses`. That is
    the version-independent route and it is also a finding: the real
    `modules/utils.wait_for_tx_output()` and `LTCClient.create_contract()` both
    match on `addresses`, a field Bitcoin Core deprecated in 0.20 and removed
    in 22.0. On a daemon that does not return it, the real code cannot find its
    own contract output no matter how correct the funding was. This helper is
    how the harness gets past that to reach steps 7-9, and step 6 prints which
    fields the node actually returned so the operator can see which case they
    are in.
    """
    for index, vout in enumerate(raw_tx.get("vout", [])):
        if vout.get("scriptPubKey", {}).get("hex") == script_hex:
            return index
    return None


def _report_scriptpubkey_fields(run: Run, raw_tx: dict) -> None:
    vouts = raw_tx.get("vout", [])
    if not vouts:
        run.say("the funding transaction has (none: no vout entries) -- that is itself wrong")
        return
    fields = sorted(vouts[0].get("scriptPubKey", {}).keys())
    run.say(f"this daemon's scriptPubKey fields are {fields}")
    if "addresses" not in fields:
        run.say(
            "`addresses` is ABSENT. modules/utils.wait_for_tx_output() and LTCClient.create_contract() both look "
            "for it, so on this daemon they cannot locate a contract output. That is a real defect in the code "
            "under test, reported here rather than worked around: the harness matches on the scriptPubKey hex "
            "instead so steps 7-9 can still run."
        )


def _fund_directly(run: Run, contract: Contract, label: str) -> Outpoint:
    """Fund the contract with a plain sendtoaddress from the harness's wallet."""
    node = run.node()
    run.say(f"[{label}] sending {CONTRACT_AMOUNT} to {contract.p2sh_address}")
    txid = node.call("sendtoaddress", contract.p2sh_address, float(CONTRACT_AMOUNT))
    mined = _mine(run, 1)
    raw_tx = _verbose_tx(node, txid, mined.first_hash)
    _report_scriptpubkey_fields(run, raw_tx)
    vout = _find_vout_by_script(raw_tx, contract.p2sh_script.hex())
    if vout is None:
        raise RegtestSetupError(
            f"{run.asset}: funded {txid} but no output pays the contract's scriptPubKey "
            f"{contract.p2sh_script.hex()}. The node was asked to pay {contract.p2sh_address}; if those two "
            "disagree, the address encoding and the script hash have diverged."
        )
    return Outpoint(txid=txid, vout=vout, value_satoshis=coins_to_satoshis(CONTRACT_AMOUNT))


def _attempt_real_create_contract(run: Run, client, contract: Contract) -> Outpoint | None:
    """Drive the real create_contract(). Returns its outpoint, or None if it failed.

    The two clients take their arguments in DIFFERENT ORDERS -- BTC takes
    (amount, secret_hash, participant, refund, locktime) and LTC takes
    (amount, participant, refund, locktime, secret_hash=None). Everything is
    passed by KEYWORD here for exactly that reason, which is the same defense
    modules/atomic_swapper.py adopted: passing positionally in the BTC order
    hands LTC the secret hash as its participant address.
    """
    amount_kwarg = {"BTC": "amount_btc", "LTC": "amount_ltc"}[run.asset]
    kwargs = {
        amount_kwarg: Decimal(CONTRACT_AMOUNT),
        "secret_hash": contract.secret_hash.hex(),
        "participant_address": contract.participant.address,
        "refund_address": contract.refund.address,
        "locktime": contract.locktime,
    }
    run.say(f"calling the REAL {type(client).__name__}.create_contract({', '.join(sorted(kwargs))})")
    # Announce the scale BEFORE the call, because this one can sit for minutes
    # with nothing of its own to say (rule 14), and an operator who cannot tell
    # working from hung presses Ctrl-C -- which here can land between a
    # broadcast funding transaction and the line that records it.
    run.say(
        f"this call can block for up to {format_duration(REAL_CLIENT_WAIT_SECONDS)} on BTC: after it broadcasts, "
        "BTCClient.create_contract() polls modules/utils.wait_for_tx_output(), which looks for the contract output "
        "under `scriptPubKey.addresses` -- a field Bitcoin Core removed in 22.0. On a daemon that does not return "
        "it, the poll runs to its deadline and then times out. That is the code under test, not a stall."
    )
    try:
        result = client.create_contract(**kwargs)
    except Exception as exc:  # noqa: BLE001 -- checked: this is the measurement. Any failure of the real fund path is the answer this step exists to record; it is reported verbatim with its type, and the harness then funds the same contract itself so steps 7-9 can still run. Nothing downstream reads a value from the failed call.
        run.check("REAL create_contract()", describe_rpc_exception(exc), "a funded contract", FAIL)
        run.say(
            "the real fund path did not complete. The harness will fund the SAME contract itself so that steps 7-9 "
            "still measure the redeem and refund branches. This is reported, not papered over."
        )
        return None
    run.check(
        "REAL create_contract()",
        f"txid={result.get('txid')} vout={result.get('vout')} p2sh={result.get('p2shAddress')}",
        "a funded contract",
        OK,
    )
    return Outpoint(
        txid=result["txid"],
        vout=int(result["vout"]),
        value_satoshis=coins_to_satoshis(CONTRACT_AMOUNT),
    )


def _assert_output_on_chain(run: Run, contract: Contract, outpoint: Outpoint, label: str) -> None:
    """gettxout: the P2SH output exists, unspent, with the expected script and value."""
    entry = run.node().call("gettxout", outpoint.txid, outpoint.vout)
    if not entry:
        run.check(f"[{label}] contract output on chain", None,
                  f"an unspent output at {outpoint.txid}:{outpoint.vout}", FAIL)
        return
    got_script = entry.get("scriptPubKey", {}).get("hex")
    run.check(
        f"[{label}] contract scriptPubKey",
        got_script,
        contract.p2sh_script.hex(),
        OK if got_script == contract.p2sh_script.hex() else FAIL,
    )
    got_value = coins_to_satoshis(entry.get("value", 0))
    run.check(
        f"[{label}] contract value in satoshis",
        got_value,
        outpoint.value_satoshis,
        OK if got_value == outpoint.value_satoshis else FAIL,
    )
    run.check(
        f"[{label}] confirmations (a count, never a duration)",
        entry.get("confirmations"),
        ">= 1",
        OK if int(entry.get("confirmations", 0)) >= 1 else FAIL,
    )


def step_6_fund(run: Run, client, contract: Contract, outcome: ChainOutcome) -> tuple[Outpoint, Outpoint]:
    run.step(6, "fund the contract, mine a block, and confirm the P2SH output is on chain")
    run.say("two outputs will be funded -- [A] for the redeem attempts, [B] for the refund attempts")

    real_outpoint = _attempt_real_create_contract(run, client, contract)
    outcome.real_create_contract = OK if real_outpoint else FAIL
    if real_outpoint is not None:
        # create_contract() can return before the funding is mined, so mine one
        # regardless; mining over an already-mined transaction costs a block
        # and nothing else.
        _mine(run, 1)
        contract_a = real_outpoint
        contract.funded_by = "the REAL create_contract()"
    else:
        contract_a = _fund_directly(run, contract, "A")
        contract.funded_by = "the harness (the real create_contract() failed above)"
    run.say(f"contract [A] was funded by {contract.funded_by}")
    _assert_output_on_chain(run, contract, contract_a, "A")

    contract_b = _fund_directly(run, contract, "B")
    run.say(
        "contract [B] is funded by the harness in every run -- no code in this tree can create a contract for the "
        "refund path, because no refund implementation exists to call."
    )
    _assert_output_on_chain(run, contract, contract_b, "B")
    contract.outpoint = contract_a
    return contract_a, contract_b


# --------------------------------------------------------------------------
# step 7: redeem with the preimage -- the real client, then the control
# --------------------------------------------------------------------------


def _broadcast(run: Run, raw_hex: str) -> str:
    """sendrawtransaction with the fee ceiling removed. See NO_FEE_LIMIT."""
    return run.node().call("sendrawtransaction", raw_hex, NO_FEE_LIMIT)


# How the real redeem_contract() failed, which decides what the harness may
# claim from it. These are the only three, and the third is deliberately not a
# synonym for the second: a failure nobody classified must not be reported as
# a confirmation of anything (rule 17).
REDEEM_FAILED_ON_LOOKUP = "lookup"
REDEEM_FAILED_ON_SIGNING = "signing"
REDEEM_FAILED_ON_WALLET_CAPABILITY = "wallet-capability"
REDEEM_FAILED_ON_BROADCAST = "broadcast"
REDEEM_FAILED_ON_FEE_POLICY = "fee-policy"
REDEEM_FAILED_UNCLASSIFIED = "unclassified"

# The daemon's own wording, matched on the message because the harness must not
# depend on which of the two clients' exception wrappers the text arrived in --
# and because, since 2026-09-25, the message carries the daemon's `code` and
# `message` read off the discarded response body (daemons.describe_rpc_exception).
#
# ORDER MATTERS and the order below is the order they are checked. Fee policy
# before broadcast, because `absurdly-high-fee` also arrives as code=-26 and is
# a completely different finding from a script that would not verify.
_FEE_POLICY_MARKERS = ("absurdly-high-fee", "max-fee-exceeded", "maxfeerate")
_BROADCAST_MARKERS = ("mandatory-script-verify-flag-failed", "non-mandatory-script-verify-flag", "code=-26")
_LOOKUP_MARKERS = ("no such mempool transaction", "-txindex", "code=-5", "'code': -5")
_SIGNING_MARKERS = ("signing incomplete", "complete': false", "complete\": false", "unable to sign")
_WALLET_CAPABILITY_MARKERS = (
    "only legacy wallets",
    "method not found",
    "-32601",
    "is not available due to",
    "wallet file verification",
)

_FAILURE_MARKERS = (
    (REDEEM_FAILED_ON_FEE_POLICY, _FEE_POLICY_MARKERS),
    (REDEEM_FAILED_ON_BROADCAST, _BROADCAST_MARKERS),
    (REDEEM_FAILED_ON_LOOKUP, _LOOKUP_MARKERS),
    (REDEEM_FAILED_ON_SIGNING, _SIGNING_MARKERS),
    (REDEEM_FAILED_ON_WALLET_CAPABILITY, _WALLET_CAPABILITY_MARKERS),
)


def classify_redeem_failure(message: str) -> str:
    """Which stage of redeem_contract() failed, from the exception's text.

    WHY THIS EXISTS, AND IT IS THE DIFFERENCE BETWEEN A MEASUREMENT AND A GUESS.

    redeem_contract()'s FIRST line is `getrawtransaction(contract_txid, True)`.
    On a node without -txindex that call cannot see a transaction that has been
    mined out of the mempool -- the same defect the harness itself had until
    2026-09-25. So on a freshly mined contract the real client fails at its own
    lookup and NEVER REACHES the signing step where the unused `secret`
    parameter matters.

    If the harness printed its "this is the known preimage defect" block for
    that failure it would be asserting a conclusion the run did not establish:
    the call failed for an unrelated reason and the preimage question was never
    put to the node. That is precisely presenting a hypothesis in the register
    of a measurement, so the two are separated here and reported differently.
    """
    lowered = message.lower()
    for kind, markers in _FAILURE_MARKERS:
        if any(marker in lowered for marker in markers):
            return kind
    return REDEEM_FAILED_UNCLASSIFIED


def _explain_redeem_failure(run: Run, contract: Contract, message: str) -> str:
    """Say what the failure does and does not establish. Returns the classification."""
    kind = classify_redeem_failure(message)

    if kind == REDEEM_FAILED_ON_LOOKUP:
        run.say("THIS IS NOT THE PREIMAGE DEFECT, AND THE HARNESS WILL NOT CLAIM THAT IT IS.")
        run.say(
            "redeem_contract() failed on its FIRST line -- `getrawtransaction(contract_txid, True)` -- which on a "
            "node without -txindex cannot see a transaction that has already been mined out of the mempool. The "
            "call never reached the signing step, so this attempt has NOT put the preimage question to the node."
        )
        run.say(
            "that is a SECOND finding about the real client and it is newly measured: redeem_contract() cannot read "
            "back a confirmed contract on a default node. `gettransaction`, or `getrawtransaction` with the "
            "contract's block hash, both work without an index."
        )
        return kind

    if kind == REDEEM_FAILED_ON_WALLET_CAPABILITY:
        run.say("THIS IS NOT THE PREIMAGE DEFECT. The daemon refused an RPC the client needs before signing.")
        run.say(
            "a legacy-wallet RPC on a descriptor wallet, or a method this daemon vintage does not have. The call "
            "did not reach the signing step, so the preimage question was not put to the node by this attempt."
        )
        return kind

    if kind == REDEEM_FAILED_ON_FEE_POLICY:
        run.say("THIS IS NOT THE PREIMAGE DEFECT -- AND IT MEANS SIGNING SUCCEEDED, WHICH IS ITS OWN FINDING.")
        run.say(
            "the node refused the transaction on FEE policy, which it can only do after accepting the script. "
            "redeem_contract() hardcodes a flat 0.0001 miner fee, and on a ~250-byte spend that is roughly "
            "0.4 coin/kvB -- four times sendrawtransaction's 0.10 default maxfeerate. That is a separate defect in "
            "the client and it is now measured rather than predicted."
        )
        return kind

    if kind == REDEEM_FAILED_ON_BROADCAST:
        run.say("THE CHAIN ITSELF REFUSED THE SPEND, WHICH IS THE STRONGEST FORM OF THIS MEASUREMENT.")
        run.say(
            "signing produced a scriptSig and the node evaluated it and rejected it. For the hashlock branch that "
            "is exactly what an absent preimage looks like: the OP_IF branch cannot be taken without the preimage "
            "and a TRUE flag on the stack, so the script fails verification."
        )
    elif kind == REDEEM_FAILED_ON_SIGNING:
        run.say("THIS IS THE KNOWN PREIMAGE DEFECT, CONFIRMED AGAINST A REAL CHAIN FOR THE FIRST TIME.")
        run.say(
            "redeem_contract() accepts `secret: bytes` and never references it. It builds the spend with "
            "createrawtransaction and hands it to a signrawtransaction* call, which constructs a scriptSig by "
            "recognizing a script PATTERN. An HTLC is OP_IF/OP_ELSE/OP_ENDIF, which matches no pattern, so the "
            "signer has nothing to build and no argument that would let it push a preimage and a TRUE flag."
        )
    else:
        run.say("THIS FAILURE IS NOT ONE THE HARNESS RECOGNIZES, so it claims nothing about which defect caused it.")
        run.say(
            "it matched none of the five known shapes -- lookup, wallet capability, signing, fee policy, broadcast. "
            "Read the daemon's code and message above before concluding anything; the control spend below still "
            "says what the SCRIPT can do."
        )

    run.say("the redeem script's hashlock branch expects, bottom to top:")
    run.say("    <signature> <pubkey> <preimage> OP_1   then the redeem script itself")
    run.say(f"the preimage is {redact(contract.secret)}; its sha256 is {contract.secret_hash.hex()}")
    run.say(f"the redeem script it must satisfy is {contract.redeem_script.hex()}")
    return kind


# The stages at which the real client never got as far as asking the node to
# build a scriptSig. A failure at one of these says nothing about the preimage.
REDEEM_STAGES_BEFORE_SIGNING = (REDEEM_FAILED_ON_LOOKUP, REDEEM_FAILED_ON_WALLET_CAPABILITY)


@dataclass(frozen=True)
class RedeemAttempt:
    """One call to the real redeem_contract(), and what it established."""

    succeeded: bool
    detail: str
    stage: str

    @property
    def reached_the_signer(self) -> bool:
        """Whether the preimage question was actually put to the node."""
        return self.succeeded or self.stage not in REDEEM_STAGES_BEFORE_SIGNING


def _attempt_real_redeem(run: Run, client, contract: Contract, outpoint: Outpoint) -> RedeemAttempt:
    """Call the real redeem_contract() once, and read the daemon's own words out of the failure."""
    # The real BTC client reads its signing key out of the environment. The key
    # handed to it was generated in this process seconds ago, controls nothing
    # but regtest coins this harness mined, and is never printed.
    os.environ["BTC_HTLC_PRIVKEY"] = contract.participant.wif
    try:
        txid = client.redeem_contract(
            outpoint.txid,
            outpoint.vout,
            contract.redeem_script,
            contract.secret,
            contract.participant.wif,
            contract.participant.address,
        )
    except Exception as exc:  # noqa: BLE001 -- checked: the failure IS the measurement, and it is not swallowed: describe_rpc_exception() digs the daemon's own code and message out of the response the client discarded, the text is classified, and both are reported. Nothing downstream reads a value from the failed call.
        detail = describe_rpc_exception(exc)
        return RedeemAttempt(succeeded=False, detail=detail, stage=classify_redeem_failure(detail))
    return RedeemAttempt(succeeded=True, detail=f"txid={txid}", stage="")


def _fund_unconfirmed(run: Run, contract: Contract, label: str) -> Outpoint | None:
    """Fund the contract again and DELIBERATELY leave it in the mempool.

    WHY A SECOND, UNMINED COPY OF THE SAME CONTRACT EXISTS.

    Measured 2026-09-25: the real redeem_contract() fails on its own first line
    -- `getrawtransaction(contract_txid, True)` -- against a CONFIRMED contract
    on a node without -txindex, because that call searches only the mempool. So
    the attempt against contract [A] can never reach the signing step, and the
    question the whole harness exists to answer -- does redeem_contract() push
    the preimage -- goes unasked.

    An output that is still IN the mempool is found by that exact call. So the
    harness funds the same redeem script once more, leaves it unconfirmed, and
    runs the real client against that. It is the same script, the same P2SH and
    the same two branches; the only difference is a confirmation, which is what
    the client's lookup is sensitive to.

    Step 6 is untouched: contract [A] is still funded, mined, and asserted on
    chain exactly as the brief asks. This is an ADDITIONAL output, and every
    line about it says which one it is.
    """
    node = run.node()
    run.say(f"[{label}] funding the SAME contract again and leaving it UNCONFIRMED, so the real client's")
    run.say(f"[{label}] first call -- a getrawtransaction that searches only the mempool -- can find it")
    txid = node.call("sendtoaddress", contract.p2sh_address, float(CONTRACT_AMOUNT))
    raw_tx = _verbose_tx(node, txid)
    vout = _find_vout_by_script(raw_tx, contract.p2sh_script.hex())
    if vout is None:
        run.check(f"[{label}] unconfirmed contract output located", None,
                  f"an output paying {contract.p2sh_script.hex()}", FAIL)
        return None
    run.check(f"[{label}] unconfirmed contract output located", f"{txid}:{vout}", "an output in the mempool", OK)
    return Outpoint(txid=txid, vout=vout, value_satoshis=coins_to_satoshis(CONTRACT_AMOUNT))


def step_7_redeem(run: Run, client, contract: Contract, outpoint: Outpoint, outcome: ChainOutcome) -> None:
    """Three attempts, because they answer three different questions.

      7a  the real client against the CONFIRMED contract [A]. This is the path
          a real swap takes, and on a default node it fails at its own lookup.
      7b  the real client against an UNCONFIRMED copy [C]. Its lookup succeeds,
          so this is the attempt that can actually put the preimage question to
          the node -- and it is what `outcome.real_redeem` records, because it
          is the only one able to judge.
      7c  the harness's control spend on [A], which says what the SCRIPT can do
          regardless of what the client can do.
    """
    run.step(7, "redeem with the preimage -- the REAL redeem_contract(), then the harness control")
    run.say(f"redeeming to the participant address {contract.participant.address}")

    run.say(f"7a: the real client against the CONFIRMED contract [A] {outpoint.txid}:{outpoint.vout}")
    confirmed = _attempt_real_redeem(run, client, contract, outpoint)
    outcome.real_redeem_confirmed = run.check(
        "7a REAL redeem_contract() against a CONFIRMED contract",
        confirmed.detail,
        "a broadcast txid -- but this harness PREDICTS this failure",
        OK if confirmed.succeeded else XFAIL,
    )
    if confirmed.succeeded:
        _finish_successful_real_redeem(run, contract, confirmed, outcome)
        return
    outcome.real_redeem_confirmed_stage = _explain_redeem_failure(run, contract, confirmed.detail)

    run.say("7b: the real client against an UNCONFIRMED copy of the same contract")
    unconfirmed_outpoint = _fund_unconfirmed(run, contract, "C")
    if unconfirmed_outpoint is None:
        outcome.real_redeem = run.check(
            "7b REAL redeem_contract() against an UNCONFIRMED contract",
            "not attempted: the unconfirmed funding could not be located",
            "a broadcast txid",
            FAIL,
        )
    else:
        unconfirmed = _attempt_real_redeem(run, client, contract, unconfirmed_outpoint)
        outcome.real_redeem = run.check(
            "7b REAL redeem_contract() against an UNCONFIRMED contract",
            unconfirmed.detail,
            "a broadcast txid -- but this harness PREDICTS this failure",
            OK if unconfirmed.succeeded else XFAIL,
        )
        if unconfirmed.succeeded:
            _finish_successful_real_redeem(run, contract, unconfirmed, outcome)
            return
        outcome.real_redeem_stage = _explain_redeem_failure(run, contract, unconfirmed.detail)
        outcome.reached_the_signer = unconfirmed.reached_the_signer
        outcome.notes.append(
            f"real redeem_contract() failed at the {outcome.real_redeem_stage} stage against an unconfirmed "
            f"contract, and at the {outcome.real_redeem_confirmed_stage} stage against a confirmed one"
        )

    run.say("7c: the harness control, on the confirmed contract [A]")
    _control_redeem(run, contract, outpoint, outcome)


def _finish_successful_real_redeem(run: Run, contract: Contract, attempt: RedeemAttempt, outcome: ChainOutcome) -> None:
    """The real client spent it. Confirm the spend and skip the control."""
    outcome.real_redeem = OK
    outcome.reached_the_signer = True
    txid = attempt.detail.removeprefix("txid=")
    mined = _mine(run, 1)
    _assert_spend_landed(run, txid, contract.participant, "real redeem", mined.first_hash)
    outcome.control_redeem = run.check(
        "control redeem",
        "not attempted: the real client already spent the output",
        "n/a",
        SKIP,
    )


def _control_redeem(run: Run, contract: Contract, outpoint: Outpoint, outcome: ChainOutcome) -> None:
    """The harness's own hashlock spend, on the same output the real client failed on.

    This is the instrument, not the code under test, and the label on screen
    says so. Its only purpose is to answer whether the SCRIPT can be spent by
    its hashlock branch at all -- which decides whether the defect is in
    redeem_contract() or in build_htlc_redeem_script(), two bugs with entirely
    different fixes.
    """
    run.say("control -- the harness now builds the hashlock scriptSig itself, on the SAME output")
    raw_hex, script_sig = build_branch_spend(
        outpoint=outpoint,
        redeem_script=contract.redeem_script,
        key=contract.participant,
        destination_script=contract.participant.p2pkh_script,
        fee_satoshis=coins_to_satoshis(CONTROL_FEE),
        locktime=0,
        secret=contract.secret,
    )
    run.say(f"control scriptSig = {describe_script_sig(script_sig)}")
    try:
        txid = _broadcast(run, raw_hex)
    except RPCError as exc:
        outcome.control_redeem = run.check("control hashlock spend", f"RPCError: {exc}", "a broadcast txid", FAIL)
        run.say(
            "the control could not spend the hashlock branch either. That points at the redeem SCRIPT rather than "
            "at redeem_contract(), and is a different bug with a different fix."
        )
        return
    outcome.control_redeem = run.check("control hashlock spend", f"txid={txid}", "a broadcast txid", OK)
    mined = _mine(run, 1)
    _assert_spend_landed(run, txid, contract.participant, "control redeem", mined.first_hash)


def _assert_spend_landed(run: Run, txid: str, key: RegtestKey, label: str, block_hash: str | None = None) -> None:
    """The spend confirmed, and its output pays the expected key.

    Asserted on the scriptPubKey hex rather than on a rendered address, for the
    same encoding reason as everywhere else in this file.

    `block_hash` is the block the caller just mined. It is passed rather than
    looked up because these spends are NOT wallet transactions -- they spend a
    P2SH the wallet never imported and pay keys this harness generated -- so
    `gettransaction` cannot see them and a bare `getrawtransaction` searches
    only the mempool they have just left. See _verbose_tx().
    """
    raw_tx = _verbose_tx(run.node(), txid, block_hash)
    confirmations = int(raw_tx.get("confirmations", 0))
    run.check(
        f"{label} confirmations (a count, never a duration)",
        confirmations,
        ">= 1",
        OK if confirmations >= 1 else FAIL,
    )
    vout = _find_vout_by_script(raw_tx, key.p2pkh_script.hex())
    run.check(
        f"{label} pays {key.address}",
        f"vout={vout}" if vout is not None else None,
        f"an output paying scriptPubKey {key.p2pkh_script.hex()}",
        OK if vout is not None else FAIL,
    )


# --------------------------------------------------------------------------
# steps 8 and 9: the refund branch, before and after expiry
# --------------------------------------------------------------------------


def _build_refund(contract: Contract, outpoint: Outpoint, nlocktime: int) -> tuple[str, bytes]:
    return build_branch_spend(
        outpoint=outpoint,
        redeem_script=contract.redeem_script,
        key=contract.refund,
        destination_script=contract.refund.p2pkh_script,
        fee_satoshis=coins_to_satoshis(CONTROL_FEE),
        locktime=nlocktime,
        secret=None,
    )


# Which layer refused a spend. The difference is the whole point of step 8 and
# it is NOT cosmetic: `mandatory-script-verify-flag-failed` means the script
# failed under the flags the chain itself applies, and
# `non-mandatory-script-verify-flag` means it passed consensus and was declined
# by RELAY POLICY -- a miner not applying that policy could have included it.
REFUSAL_CONSENSUS = "consensus"
REFUSAL_POLICY = "policy"
REFUSAL_NON_FINAL = "non-final"
REFUSAL_OTHER = "other"

# Checked in this order. `non-mandatory-script-verify-flag` must be tested
# first: it contains the word "mandatory", and scoring a policy refusal as a
# consensus one is exactly the overclaim this step exists to avoid.
_REFUSAL_MARKERS = (
    (REFUSAL_POLICY, ("non-mandatory-script-verify-flag",)),
    (REFUSAL_CONSENSUS, ("mandatory-script-verify-flag-failed",)),
    (REFUSAL_NON_FINAL, ("non-final", "non-bip68-final")),
)


def classify_refusal(message: str) -> str:
    """Which layer said no: consensus, relay policy, finality, or something else."""
    lowered = message.lower()
    for kind, markers in _REFUSAL_MARKERS:
        if any(marker in lowered for marker in markers):
            return kind
    return REFUSAL_OTHER


def _attempt_refund_expecting_refusal(run: Run, contract: Contract, outpoint: Outpoint, nlocktime: int, label: str) -> tuple[str, str]:
    """Broadcast a refund that must be refused. Returns (outcome, refusal kind)."""
    raw_hex, script_sig = _build_refund(contract, outpoint, nlocktime)
    run.say(f"{label} -- nLockTime={nlocktime}, script locktime={contract.locktime} (both heights)")
    run.say(f"{label} scriptSig = {describe_script_sig(script_sig)}")
    try:
        txid = _broadcast(run, raw_hex)
    except RPCError as exc:
        kind = classify_refusal(str(exc))
        return run.check(f"{label} is REFUSED", f"[{kind}] RPCError: {exc}", "the node to refuse it", OK), kind
    return (
        run.check(f"{label} is REFUSED", f"the node ACCEPTED it: txid={txid}", "the node to refuse it", FAIL),
        REFUSAL_OTHER,
    )


def _attempt_refund_into_a_block(run: Run, contract: Contract, outpoint: Outpoint, nlocktime: int) -> str:
    """Ask the daemon to MINE the early refund into a block. Consensus, not policy.

    WHY THE MEMPOOL'S ANSWER IS NOT ENOUGH, which is the half of the 2026-09-25
    finding the refusal string cannot settle on its own. `mandatory` versus
    `non-mandatory` is decided by a compile-time flag set, so the wording can
    differ between two daemons that would both enforce -- or both not enforce --
    CHECKLOCKTIMEVERIFY at a given height. The question the operator actually
    asked is "could a miner have included it", and the way to answer that is to
    ask a miner.

    `generateblock` runs TestBlockValidity over a block containing the
    transaction, so a refusal here is the CHAIN refusing, not the mempool. It
    is the strongest assertion this harness can make about CLTV, and it needs
    no assumption about which flags a given build calls mandatory.

    This spends a DEDICATED output [D], never the one step 9 refunds. If
    consensus did accept an early refund -- the failure this exists to catch --
    the output it consumed must not be the one the last step depends on.
    """
    node = run.node()
    if not daemons.method_exists(node, "generateblock"):
        return run.check(
            "8c early refund is refused by CONSENSUS (mined into a block)",
            "not attempted: this daemon has no `generateblock`",
            "the daemon to refuse to build the block",
            SKIP,
        )
    raw_hex, _ = _build_refund(contract, outpoint, nlocktime)
    address = node.call("getnewaddress", "regtest-harness-mining")
    run.say(
        "8c: asking the daemon to MINE this same early refund into a block. The mempool's wording says which flag "
        "set refused it; only this says whether the CHAIN would have."
    )
    try:
        result = node.call("generateblock", address, [raw_hex])
    except RPCError as exc:
        kind = classify_refusal(str(exc))
        return run.check(
            "8c early refund is refused by CONSENSUS (mined into a block)",
            f"[{kind}] RPCError: {exc}",
            "the daemon to refuse to build the block",
            OK,
        )
    return run.check(
        "8c early refund is refused by CONSENSUS (mined into a block)",
        f"the daemon MINED IT: {result}",
        "the daemon to refuse to build the block",
        FAIL,
    )


def step_8_refund_before_expiry(run: Run, contract: Contract, outpoint: Outpoint, outcome: ChainOutcome) -> None:
    run.step(8, "refund BEFORE expiry must be REJECTED -- three times, for three different reasons")

    # [D] is funded FIRST, while mining a block for it cannot disturb the
    # height arithmetic below.
    consensus_outpoint = _fund_directly(run, contract, "D")

    height = int(run.node().call("getblockcount"))
    target = contract.locktime - 1
    run.say(
        f"height={height}, mining to {target} = one block short of the script's locktime {contract.locktime} "
        "(heights, never durations)"
    )
    if height < target:
        height = _mine(run, target - height).height
    run.check("height one block short of the locktime", height, target, OK if height == target else FAIL)

    activation = run.facts.cltv_activation_height
    outcome.cltv_activation_height = activation
    outcome.height_at_refund_test = height
    if activation is None:
        run.say(
            "CLTV activation height is (none: this daemon did not report one), so whether the chain itself would "
            "enforce the locktime at this height cannot be stated from the height. 8c below asks the chain directly."
        )
    else:
        run.check(
            "the refund test runs where CLTV is consensus-enforced",
            f"height {height} vs BIP65 activation {activation}",
            f">= {activation}",
            OK if height >= activation else FAIL,
        )

    # 8a: final-ness. The mempool refuses before any script runs.
    first, _ = _attempt_refund_expecting_refusal(
        run, contract, outpoint, contract.locktime,
        "8a refund with nLockTime = the script's locktime (mempool non-final check; proves nothing about CLTV)",
    )
    # 8b: the transaction is final for this block, so the script actually runs
    # and CLTV compares the script's larger locktime against this nLockTime.
    second, refusal_kind = _attempt_refund_expecting_refusal(
        run, contract, outpoint, height,
        "8b refund with nLockTime = the current tip (final, so the script RUNS; this is the CLTV assertion)",
    )
    outcome.refund_refusal_kind = refusal_kind
    _report_refusal_strength(run, refusal_kind)

    # 8c: the chain itself, not the mempool.
    outcome.cltv_consensus_refused = _attempt_refund_into_a_block(run, contract, consensus_outpoint, height)

    outcome.refund_before_expiry_rejected = OK if first == OK and second == OK else FAIL


def _report_refusal_strength(run: Run, kind: str) -> None:
    """Say how strong 8b's refusal actually was. Never score the two the same."""
    if kind == REFUSAL_CONSENSUS:
        run.say(
            "8b was refused with `mandatory-script-verify-flag-failed`: the script failed under the flags the chain "
            "itself applies. This is the result worth having."
        )
        return
    if kind == REFUSAL_POLICY:
        run.say(
            "8b was refused with `non-mandatory-script-verify-flag`, which is WEAKER and must not be read as the "
            "same result. It means the transaction PASSED consensus and was declined by relay policy: this node "
            "would not carry it, but the chain did not refuse it, and a miner not applying that policy could have "
            "included an early refund. 8c is what settles whether the chain would have refused it."
        )
        return
    if kind == REFUSAL_NON_FINAL:
        run.say("8b was refused as non-final, which is the 8a condition rather than the CLTV one -- read the nLockTime above.")
        return
    run.say(f"8b's refusal did not match any known shape ({kind}); read the message above before concluding anything.")


def step_9_refund_after_expiry(run: Run, contract: Contract, outpoint: Outpoint, outcome: ChainOutcome) -> None:
    run.step(9, "refund AFTER expiry must SUCCEED")
    height = _mine(run, 1).height
    run.check(
        "height now reaches the locktime",
        height,
        contract.locktime,
        OK if height >= contract.locktime else FAIL,
    )
    raw_hex, script_sig = _build_refund(contract, outpoint, contract.locktime)
    run.say(f"refund scriptSig = {describe_script_sig(script_sig)}")
    try:
        txid = _broadcast(run, raw_hex)
    except RPCError as exc:
        outcome.refund_after_expiry = run.check("refund after expiry", f"RPCError: {exc}", "a broadcast txid", FAIL)
        return
    outcome.refund_after_expiry = run.check("refund after expiry", f"txid={txid}", "a broadcast txid", OK)
    mined = _mine(run, 1)
    _assert_spend_landed(run, txid, contract.refund, "refund", mined.first_hash)
