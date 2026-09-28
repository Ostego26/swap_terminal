#!/usr/bin/env python3
"""Prove -- or refute -- that a 2-of-2 P2SH actually funds and SPENDS, both branches.

Role: file (the entry point; it runs the stages in swap_terminal/regtest/adaptor_steps.py
      and holds no decision of its own)
Reads: a local bitcoind / litecoind regtest node, or a Gridcoin TESTNET node, over
       JSON-RPC, plus the environment variables listed below.
Writes: on BTC and LTC it may start a daemon, create a wallet, mine blocks and broadcast
       transactions. On GRC it broadcasts and waits, and starts and stops NOTHING. It
       writes nothing into this repository.
Can move funds: YES -- on a test network, and structurally nowhere else.
Mainnet-safe: NO, AND IT REFUSES TO BE ASKED. The first call after every connection is
       adaptor_steps.assert_test_network(), which requires the DAEMON ITSELF to say it is
       on a test network. On BTC and LTC that is `getblockchaininfo.chain == "regtest"`
       exactly, through the same daemons.assert_regtest() regtest_htlc_verify.py uses. On
       GRC it asks three different ways and refuses unless at least one comes back
       positive -- an absence of evidence is treated as mainnet. No flag turns any of
       that off, and the Gridcoin mainnet rpcport is refused by number as well.
Live-safe: no. It broadcasts.

WHY THIS EXISTS, IN THE OPERATOR'S OWN WORDS.

`modules/adaptor_swap_scripts.py` was committed with an 18-test suite and a header saying
Gridcoin accepts these scripts, citing Gridcoin-Research's `src/script.cpp`. The operator
refused that as evidence:

    "Still not established, and I won't let this pass as done: no 2-of-2 P2SH has ever
     been spent on Gridcoin from this repo. A source reading plus a CLTV measurement is
     not a spend. That needs regtest with both branches exercised."

Correct on every count, and one of the two halves of the evidence was worse than
described -- see WHAT THIS CORRECTS below. This harness is what replaces the reading.

WHAT IT SETTLES, EACH AS A ROW-LEVEL OUTCOME ON A REAL CHAIN.

    1  a 2-of-2 P2SH funds, and is LOCATED on chain by scriptPubKey match
    2  it SPENDS with both signatures in the redeem script's key order
    3  it is REFUSED with the signatures TRANSPOSED
    4  it is REFUSED with the leading OP_0 dummy MISSING
    5  a spend with nLockTime before T1 is refused and at T1 accepted -- sought from
       CONSENSUS (the daemon is asked to MINE it) as well as from relay
    6  the same on Gridcoin testnet, or a stated reason why not

Three and four are the ones nothing local can answer. A transposed pair of signatures
produces a scriptSig of the SAME LENGTH and the SAME SHAPE as a correct one; there is no
assertion available off-chain that distinguishes them, which is why
tests/test_adaptor_swap_chain.py says so rather than pretending.

It also settles, as a side effect of doing the above at all:

    -  that the txid predicted before broadcast is the one the daemon reports, for every
       transaction in the chain. That is what makes the whole protocol possible, because
       step 0 exchanges signatures on transactions spending Tx_lock BEFORE Tx_lock is
       broadcast.
    -  that signing Tx_cancel CHANGES its txid, so the refund and punish cannot be built
       in the same round -- see FINDING 2 in modules/adaptor_swap_chain.py.
    -  whether `fundrawtransaction` exists on each daemon, which nothing in this tree had
       ever asked.

WHAT THIS CORRECTS, BEFORE ANYBODY RUNS IT.

Two claims in this repository about Gridcoin and CLTV are weaker than they read, and
both were written by me:

  - `modules/adaptor_swap_scripts.py`'s header says "three swaps on 2026-09-27 proved CLTV
    is CONSENSUS-enforced on Gridcoin, because the daemon refused to MINE an early refund
    (generateblock answered TestBlockValidity failed)". The `generateblock` measurement is
    regtest_htlc_verify.py's step 8c, which runs on BTC and LTC only -- that harness's
    --chain flag offers `btc`, `ltc` and `both`, and Gridcoin has no node in it at all. No
    generateblock call has ever been made against a Gridcoin daemon from this tree.
  - `docs/monero_swap_protocol.md` says "CLTV on GRC is established by three completed
    swaps on 2026-09-27 using OP_CHECKLOCKTIMEVERIFY". Those three swaps took the HASHLOCK
    branch -- docs/atomic_swap_runs_2026_09_27.md records a GRC claim txid for each and no
    GRC refund anywhere. The CLTV opcode sits in the OP_ELSE branch, which a hashlock spend
    never executes. So what they establish is that Gridcoin ACCEPTS a script CONTAINING
    OP_CHECKLOCKTIMEVERIFY in a branch that does not run, which is a real fact and is not
    the fact claimed.

Neither correction weakens this harness, because the 2-of-2 chain does not use
OP_CHECKLOCKTIMEVERIFY at all: T1 and T2 are plain nLockTime fields on transactions that
cannot move without both parties. That is also why this run is cheap. MEASURED 2026-09-28
against Litecoin Core 0.21.4 regtest: a full `--chain ltc --wipe` run took 8 seconds of wall
clock -- 6.6µfn (8s) -- and mined 118 blocks. regtest_htlc_verify.py mines 2504 per
chain, because CLTV is a buried deployment with an activation height at 1351 and nLockTime
finality is not a deployment at all.

WHAT A GREEN LTC RUN LOOKED LIKE, so a first run elsewhere has something to compare against:

    OK=40  FAIL=0  XFAIL=0  SKIP=0
    LTC: 2-of-2 P2SH IS SPENDABLE ON THIS CHAIN: funded, located by scriptPubKey match,
         spent with both signatures in key order, and REFUSED both transposed and without
         the OP_0 dummy. This is a spend, not a source reading.
    LTC: nLockTime on the 2-of-2 is CONSENSUS-enforced: the daemon refused to MINE an early
         cancel, and accepted the same transaction at T1

    the two refusals, in the daemon's own words:
      transposed   code=-26 mandatory-script-verify-flag-failed
                   (Signature must be zero for failed CHECK(MULTI)SIG operation)
      no OP_0      code=-26 mandatory-script-verify-flag-failed
                   (Operation not valid with the current stack size)
      early cancel code=-26 non-final          (relay)
                   code=-25 TestBlockValidity failed: bad-txns-nonfinal  (consensus)

AND WHAT THE HARNESS DOES WHEN THE CODE IS WRONG, because an assertion nobody has seen fail
is an assertion nobody should trust. Two mutations were run against the live LTC chain,
2026-09-28:

    SEQUENCE_NON_FINAL set to 0xFFFFFFFF    the early cancel was ACCEPTED by relay AND MINED
                                            into a block -- 10a, 10b and 10c all FAIL, and
                                            the run exits 1. So the non-final sequence is
                                            what makes T1 mean anything, measured rather
                                            than read.
    OP_0 removed from the real assembler    step 8's own guard fired first -- "the assembler
                                            did not put OP_0 first, so this test would be
                                            removing the wrong byte" -- and the verdict read
                                            NOT ESTABLISHED with two SKIPs rather than a
                                            pass.

HOW TO RUN IT.

    cd <repo>
    source .venv/bin/activate
    python3 adaptor_regtest_verify.py --chain ltc --wipe

    # both bitcoind and litecoind, from an empty chain each
    python3 adaptor_regtest_verify.py --chain both --wipe

    # Gridcoin TESTNET, against a daemon you started yourself
    GRC_RPC_HOST=127.0.0.1 GRC_RPC_PORT=25715 GRC_RPC_USER=... GRC_RPC_PASS=... \
    python3 adaptor_regtest_verify.py --chain grc

IT DOES NOT NEED THE OPERATOR'S MACHINE for BTC or LTC. Every ST_REGTEST_* variable is a
path or a port, so a release tarball unpacked into a scratch directory works -- that is how
LTC was first driven on 2026-09-26, and regtest_htlc_verify.py's docstring carries the
exact curl and conf lines. This harness reads the same variables, so a datadir prepared for
that one works for this one unchanged.

GRIDCOIN IS DIFFERENT AND THE DIFFERENCE IS DELIBERATE.

Gridcoin has no regtest mode and no local mining: it is proof of stake, and a testnet block
arrives about every 90 seconds whether anybody is waiting or not.

  -  THIS HARNESS NEVER STARTS OR STOPS A GRIDCOIN DAEMON, and there is no --wipe for it.
     The operator's testnet daemon is a long-running staking wallet; starting or stopping
     one is a live action nobody asked for, and CLAUDE.md's live-safety rules forbid it.
     It must already be answering.
  -  It needs a SPENDABLE testnet balance, which means a FULL unlock rather than a
     staking-only one. A staking-only unlock cannot send, and THE CODE IS -4, NOT -13 --
     this line said -13 until 2026-09-28, when the operator's daemon answered
     `sendtoaddress: code=-4 message="Error: Wallet unlocked for staking only, unable to
     create transaction."`. Both codes are real for this one condition and they come from
     two different guards: -4 is RPC_WALLET_ERROR, produced as a RETURN STRING inside
     CWallet::SendMoney and rethrown by sendtoaddress, and -13 is
     RPC_WALLET_UNLOCK_NEEDED, THROWN by EnsureWalletIsUnlocked() with the different
     wording "Error: Wallet is unlocked for staking only." sendtoaddress never calls
     EnsureWalletIsUnlocked, which is why the send path is the -4 one.
     regtest/adaptor_steps.py's STAKING_ONLY_RPC_CODES carries both, with the source
     lines; naming only one here is what sent a reader to the wrong function.
     NO Gridcoin RPC reports the unlock SCOPE back either -- measured 2026-09-26:
     `getwalletinfo` returns exactly one lock field, `unlocked_until`, and nothing
     separates the two states. So the harness PROBES the scope behaviorally, and the
     funding send is still the real test.
  -  Every wait is a real wait. T1 is six blocks out and T2 twelve, so a full GRC run is
     roughly eighteen blocks -- call it half an hour, and the harness prints a progress
     line with an elapsed time every thirty seconds rather than leaving a blinking cursor.
  -  The consensus-versus-relay distinction needs `generateblock`. The harness PROBES for
     it and reports a SKIP with the reason if it is absent, never a pass.

FLAGS

    --chain {btc,ltc,grc,both,all}   which chains to run. Default btc+ltc (`both`),
                                     because those two need nothing but a downloaded
                                     daemon. `all` adds GRC.
    --wipe                           delete each regtest datadir's `regtest` subdirectory
                                     first. IGNORED for GRC, loudly.
    --keep-running                   leave the daemons this harness started up afterwards.
    --verbose-clients                leave the shared HTLC modules' loggers at DEBUG.

ENVIRONMENT

    BTC and LTC: exactly the ST_REGTEST_* variables regtest_htlc_verify.py documents --
    ST_REGTEST_{BTC,LTC}_{DATADIR,RPC_PORT,RPC_USER,RPC_PASSWORD,DAEMON,CLI} and
    ST_REGTEST_RPC_HOST. Nothing new.

    GRC: GRC_RPC_HOST, GRC_RPC_PORT, GRC_RPC_USER, GRC_RPC_PASS -- the names config.py
    already reads -- or ST_ADAPTOR_GRC_RPC_* to override any of them. THERE IS NO DEFAULT
    PORT: network_target.py records the incident where an unset GRC_RPC_PORT fell through
    to 15715, which is MAINNET, and the serving path polled the operator's live staking
    wallet on a loop. Port 15715 is refused by number.

    ST_ADAPTOR_GRC_BLOCK_TIMEOUT_SECONDS   how long to wait for the tip to advance
                                           (default 1800). ASCII, and it says _SECONDS
                                           because that is rule 6's boundary: micro in
                                           what a human reads, ASCII in what a shell has
                                           to export.

WHAT IT PRINTS, AND WHY SO MUCH.

This file was written in a container with no bitcoind, no litecoind and no
gridcoinresearchd. It has never been executed. It will first run on somebody else's
computer, where a bare traceback costs a round trip -- so every stage announces its target
before it acts, every assertion prints what it got beside what it wanted, and an empty
result prints `(none)` rather than nothing (rule 14).

Durations are microfortnights with the seconds in parentheses. Block heights, locktimes and
confirmation counts are NEVER converted: a block is not 1.2096 seconds long (rule 6).

A SKIP IS NOT A PASS, and the verdict says so in as many words. Four of the outcomes are
decisive, and if any of them was never attempted the verdict reads NOT ESTABLISHED rather
than reporting the three that did run.

NO KEY MATERIAL IS PRINTED. Every key is generated in-process, controls nothing but test
coins this harness arranged, and is never logged or written anywhere. There is no preimage
in this protocol at all -- the adaptor signature replaces the hashlock -- so the most
dangerous value in the HTLC path does not exist here.

This is NOT a pytest test and is deliberately not under tests/: it needs an external
daemon, it starts processes on two of the three chains, and on Gridcoin it can wait half an
hour. `python3 -m pytest` must stay runnable on a machine with no chain at all. Everything
that CAN be tested without a daemon is in tests/test_adaptor_swap_chain.py.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
APP_ROOT = REPO_ROOT / "swap_terminal"
if str(APP_ROOT) not in sys.path:
    # The application imports its own modules rootlessly (`from config import Config`,
    # `from modules.utils import ...`), so swap_terminal/ has to be importable.
    # tests/conftest.py and regtest_htlc_verify.py both do the same thing for the same
    # reason. That is CLAUDE.md rule 10's layout gap; rewriting every import in the tree
    # to close it would be a large diff with no behavioral gain.
    sys.path.insert(0, str(APP_ROOT))

from microfortnights import format_duration  # noqa: E402 -- the sys.path line above must run first
from regtest import adaptor_steps, daemons  # noqa: E402 -- same
from regtest.adaptor_steps import ChainOutcome, Run  # noqa: E402 -- same
from regtest.console import FAIL, OK, SKIP, Console  # noqa: E402 -- same
from regtest.daemons import RegtestSetupError  # noqa: E402 -- same

WALLET_NAME = "adaptor_2of2_harness"

# The loggers of the modules under test, so --verbose-clients means something. htlc_rpc is
# the one worth having at INFO: lookup_contract_output() says which of its four routes
# answered, and on Gridcoin route 1 FAILS BY DESIGN -- that daemon has no `gettxout` -- so
# a run there prints an error from the path that worked. That is rule 14 pointed backwards
# and it is recorded at the lookup site; this harness names it here so a reader of a pasted
# GRC run is not chasing it.
MODULE_LOGGERS = (
    "modules.adaptor_swap_scripts",
    "modules.adaptor_swap_chain",
    "modules.atomic_htlc_scripts",
    "modules.htlc_rpc",
    "modules.htlc_spend",
    "modules.htlc_fee",
    "modules.utils",
)

CHAIN_SETS = {
    "btc": ["BTC"],
    "ltc": ["LTC"],
    "grc": ["GRC"],
    "both": ["BTC", "LTC"],
    "all": ["BTC", "LTC", "GRC"],
}


def configure_logging(verbose_clients: bool) -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    level = logging.DEBUG if verbose_clients else logging.INFO
    for name in MODULE_LOGGERS:
        logging.getLogger(name).setLevel(level)


def run_chain(console: Console, asset: str, args: argparse.Namespace) -> ChainOutcome:
    """Run all ten steps against one chain. Always tears down what THIS HARNESS started.

    The teardown is in a `finally` and it reads `run.spawned` rather than a local set from
    the step's return value, because step 1 can raise after it has spawned -- and a reaper
    that depends on the spawner returning normally is how an orphan survives a stop
    (CLAUDE.md rule 13). For GRC `run.spawned` is never True, because nothing is ever
    started there, so the teardown correctly does nothing.
    """
    console.banner(f"{asset} -- 2-of-2 P2SH verification, ten steps")
    outcome = ChainOutcome(asset=asset)
    run: Run | None = None
    try:
        config = adaptor_steps.resolve_config(asset)
        run = Run(console=console, config=config, wallet=WALLET_NAME if asset != "GRC" else "")
        console.say(f"{asset}: endpoint {config.base_url}  datadir {config.datadir}")
        if args.wipe and asset == "GRC":
            console.say(
                "GRC: --wipe is IGNORED. There is no regtest datadir to wipe and this harness does not "
                "touch a Gridcoin datadir at all -- that daemon is a live staking wallet."
            )
        adaptor_steps.step_1_reachable(run)
        if args.wipe and asset != "GRC":
            console.say(f"{asset}: --wipe requested")
            daemons.stop_daemon(console, config, run.spawned)
            daemons.wipe_datadir(console, config)
            run.spawned = daemons.start_daemon(console, config)
            daemons.wait_for_rpc(console, config)
        adaptor_steps.assert_test_network(run)
        adaptor_steps.step_3_capabilities(run)

        setup_a, setup_b = adaptor_steps.step_4_build_scripts(run)
        tip = adaptor_steps.step_5_spendable_coins(run)

        # LOCK A: funded, located, then the two refusals, then the happy-path spend. The
        # refusals come BEFORE the spend on purpose -- they spend the same output, and a
        # refusal that arrives because the output is already gone proves nothing.
        funding_a = adaptor_steps.fund_and_prepare(run, setup_a)
        built_a = adaptor_steps.step_6_build_and_hold(run, setup_a, funding_a, tip)
        adaptor_steps.step_7_broadcast_lock(run, built_a, outcome)
        adaptor_steps.step_8_refusals(run, built_a, outcome)
        adaptor_steps.step_9_happy_path(run, built_a, outcome)

        # LOCK B: an unspent 2-of-2 for the cancel path, because lock A's output is gone.
        console.banner(f"{asset} -- lock B, for the cancel path")
        funding_b = adaptor_steps.fund_and_prepare(run, setup_b)
        built_b = adaptor_steps.step_6_build_and_hold(run, setup_b, funding_b, adaptor_steps.current_height(run))
        adaptor_steps.step_7_broadcast_lock(run, built_b, outcome)
        adaptor_steps.step_10_cancel_path(run, built_b, outcome)
    except RegtestSetupError as exc:
        # A named precondition failed and the message carries the fix. Printed as an
        # assertion rather than raised, so the other chains still run and the teardown
        # below still happens.
        console.check(f"{asset} setup", str(exc), "the precondition to hold", FAIL)
        outcome.notes.append(f"setup failed: {exc}")
    except Exception as exc:  # noqa: BLE001 -- checked: an unexpected exception must not skip the teardown below, which is this harness's only reaper for a daemon it spawned (rule 13). It is recorded as a FAIL with its type and message, never swallowed into a pass, and the exit code reflects it.
        console.check(f"{asset} run", f"{type(exc).__name__}: {exc}", "no unhandled exception", FAIL)
        outcome.notes.append(f"unhandled {type(exc).__name__}: {exc}")
    finally:
        console.banner(f"{asset} -- teardown")
        if run is None:
            console.say(f"{asset}: nothing to tear down -- the configuration never resolved")
        elif asset == "GRC":
            console.say(
                "GRC: this harness started nothing and stops nothing. The operator's testnet daemon is "
                "left exactly as it was found, still staking."
            )
        elif args.keep_running:
            console.say(
                f"{asset}: --keep-running, so the daemon at {run.config.base_url} is LEFT UP"
                f"{' (this harness started it)' if run.spawned else ' (this harness did not start it)'}. "
                "Stop it yourself when you are done."
            )
        else:
            daemons.stop_daemon(console, run.config, run.spawned)
    return outcome


def print_verdicts(console: Console, outcomes: list[ChainOutcome]) -> None:
    """The six outcomes per chain, then the one sentence each chain earns.

    Every field is printed even when it is SKIP, because "not attempted" and "attempted and
    failed" are different results and a row that omitted the skips would read as a shorter,
    cleaner pass (rule 14).
    """
    console.banner("DID A 2-of-2 P2SH ACTUALLY FUND AND SPEND, AND WAS THE FOOTGUN REFUSED")
    if not outcomes:
        console.say("(none: no chain was run)")
        return
    for outcome in outcomes:
        console.say(f"{outcome.asset}: {outcome.verdict()}")
        console.say(f"{outcome.asset}: {outcome.cancel_verdict()}")
        console.say(
            f"{outcome.asset}:   THE FOUR DECISIVE OUTCOMES -- "
            f"1 funded and located by scriptPubKey match={outcome.located_by_script_match}  "
            f"2 spends in key order={outcome.spends_in_correct_order}  "
            f"3 REFUSED transposed={outcome.refused_when_transposed}  "
            f"4 REFUSED without OP_0={outcome.refused_without_op0}"
        )
        console.say(
            f"{outcome.asset}:   the nLockTime outcomes -- "
            f"5a cancel refused before T1 by RELAY={outcome.cancel_refused_before_t1_by_relay}  "
            f"5b refused before T1 by CONSENSUS (asked to MINE it)={outcome.cancel_refused_before_t1_by_consensus}  "
            f"5c accepted at T1={outcome.cancel_accepted_at_t1}"
        )
        console.say(
            f"{outcome.asset}:   the SECOND 2-of-2 (the cancel output) -- "
            f"punish refused before T2={outcome.punish_refused_before_t2}  "
            f"refund accepted after the cancel={outcome.refund_accepted_after_cancel}"
        )
        console.say(
            f"{outcome.asset}:   and the property the whole protocol needs: "
            f"the txid PREDICTED before broadcast matched the daemon's={outcome.predicted_txid_matched}  "
            f"<- this is what `sendtoaddress` cannot give you, and step 0 requires it"
        )
        if outcome.notes:
            for note in outcome.notes:
                console.say(f"{outcome.asset}:   note: {note}")
        else:
            console.say(f"{outcome.asset}:   notes: (none)")


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="adaptor_regtest_verify.py",
        description=(
            "Fund and SPEND a 2-of-2 P2SH on a real test chain, both branches, and assert that the "
            "transposed-signature and missing-OP_0 scriptSigs are REFUSED. Refuses to run against any "
            "network the daemon does not itself say is a test network."
        ),
    )
    parser.add_argument("--chain", choices=tuple(CHAIN_SETS), default="both", help="which chains to run")
    parser.add_argument(
        "--wipe", action="store_true",
        help="delete each regtest datadir's `regtest` subdirectory first. Ignored for GRC, loudly",
    )
    parser.add_argument("--keep-running", action="store_true", help="leave daemons this harness started up")
    parser.add_argument("--verbose-clients", action="store_true", help="leave the module loggers at DEBUG")
    return parser.parse_args(argv)


def _announce_wall_clock(console: Console, assets: tuple[str, ...] | list[str]) -> None:
    """Say how long this will take BEFORE it starts, and only where the answer is minutes.

    RULE 14, AND THE SPECIFIC ACCIDENT IT EXISTS TO PREVENT HERE. A BTC or LTC regtest run of
    this harness finishes in single-digit seconds -- the LTC run on 2026-09-28 took 6.6µfn
    (8s) for 40 checks -- because `generatetoaddress` produces a block on demand. A Gridcoin
    run cannot do that: it WAITS for real testnet blocks, and the progress line fires only
    every 30s. So the operator sits in front of a 30-second gap, on a run driven against a
    STAKING WALLET, with nothing on screen saying that is normal. CLAUDE.md rule 14 records
    the operator's own words for what happens next: "i cannot stand to wait who knows how the
    fuck long on a blinking cursor. how do i know it's not hung or broken?"

    Printed only for GRC because a number attached to a run that takes eight seconds is noise,
    and rule 14 asks for output that distinguishes cases rather than output everywhere.

    Membership rather than equality on `assets`: CHAIN_SETS is data, and an `assets == ["GRC"]`
    check would go silent the day a grc+ltc set is added.
    """
    if "GRC" not in assets:
        return
    blocks = adaptor_steps.expected_grc_blocks()
    seconds = adaptor_steps.expected_grc_seconds()
    console.say(
        f"GRC CANNOT BE TOLD TO PRODUCE A BLOCK, so this run WAITS for real testnet blocks: at "
        f"least {blocks} of them at Gridcoin's {adaptor_steps.GRC_SECONDS_PER_BLOCK:.0f}s target "
        f"interval, so EXPECT AT LEAST {format_duration(seconds)} and possibly more -- "
        f"{blocks} is a floor, not a prediction (adaptor_steps.expected_grc_blocks)"
    )
    console.say(
        f"  progress prints every {format_duration(adaptor_steps.GRC_PROGRESS_INTERVAL_SECONDS)} "
        f"while waiting, so a gap LONGER than that is the thing to worry about -- not a gap "
        f"shorter. Per-wait timeout {format_duration(adaptor_steps.GRC_BLOCK_WAIT_TIMEOUT_SECONDS)} "
        f"(ST_ADAPTOR_GRC_BLOCK_TIMEOUT_SECONDS)"
    )
    console.say(
        "  this harness NEVER starts or stops a Gridcoin daemon and sends no `stop`: your staking "
        "wallet is left exactly as it was found"
    )


def main(argv: list[str] | None = None) -> int:
    args = parse_args(list(sys.argv[1:] if argv is None else argv))
    configure_logging(args.verbose_clients)
    console = Console(total_steps=adaptor_steps.TOTAL_STEPS)

    assets = CHAIN_SETS[args.chain]
    console.banner("2-of-2 P2SH verification harness -- the spend, not the source reading")
    console.say(f"chains={assets}  wipe={args.wipe}  keep_running={args.keep_running}")
    console.say(
        "the code under test is modules/adaptor_swap_scripts.py and modules/adaptor_swap_chain.py. "
        "Both were written without a chain to run them against, which is the reason this file exists."
    )
    console.say(
        "every chain must SAY it is on a test network before anything is built; GRC is asked three ways "
        "and an absence of evidence is treated as mainnet"
    )
    console.say(
        "no OP_CHECKLOCKTIMEVERIFY is used anywhere here, so no BIP65 activation height is mined -- T1 "
        "and T2 are plain nLockTime fields on transactions that cannot move without both parties"
    )
    _announce_wall_clock(console, assets)

    outcomes = [run_chain(console, asset, args) for asset in assets]
    print_verdicts(console, outcomes)
    console.summary()

    failed = console.counts[FAIL]
    skipped = console.counts[SKIP]
    console.banner(
        f"exit code {1 if failed else 0} -- "
        + (
            "unexpected failures above"
            if failed
            else f"no unexpected failures ({console.counts[OK]} checks OK, {skipped} SKIP). "
            "Read the per-chain verdict, not this line: a SKIP on any of the four decisive outcomes "
            "means NOT ESTABLISHED, and a zero exit code does not change that."
        )
    )
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
