#!/usr/bin/env python3
"""Prove -- or refute -- the HTLC atomic-swap path against real BTC and LTC regtest nodes.

Role: file (the entry point; it runs the stages in regtest/steps.py and holds
      no decision of its own)
Reads: local bitcoind and litecoind daemons over JSON-RPC, and the
       ST_REGTEST_* environment variables documented below
Writes: starts and stops daemons; creates wallets; mines blocks; broadcasts
       transactions. With --wipe it deletes the `regtest` subdirectory of each
       datadir. It writes NOTHING into this repository.
Can move funds: YES -- on a regtest chain, and structurally nowhere else.
Mainnet-safe: NO, AND IT REFUSES TO BE ASKED. The first assertion after every
       connection is that `getblockchaininfo.chain` is exactly `regtest`;
       anything else aborts that chain before a single block is mined or a
       single transaction is built. That refusal is unconditional and no flag
       turns it off. It is in regtest/daemons.py::assert_regtest, called from
       step 2 for every chain on every run.

WHY THIS EXISTS.

Everything this repository believes about its HTLC path is inferred from
reading. The three clients' module headers say, correctly and in detail, that
`redeem_contract()` never references its `secret` parameter -- established by
walking the AST -- and then say plainly that what follows from it "has NOT been
confirmed against a chain here." modules/htlc_timelock.py says the same about
the refund branch: "Nothing in this tree has funded a contract built on one of
these values and then refunded it after expiry, on any chain."

CLAUDE.md's verification rule is what makes that gap the outstanding work:
"the only honest proof that a contract is correct is that both of its branches
were exercised on a test chain: a redeem with the preimage, and a refund after
the timelock expired." This harness is that proof, or that refutation.

WHAT IT DOES NOT DO.

It does not fix anything. `redeem_contract()` is fund-moving code and CLAUDE.md
rule 16 puts that decision with the operator; the harness measures and reports.
It also does not stub, skip, or soften the assertion that is expected to fail
-- a known defect confirmed on a real chain is a successful measurement, and it
is printed as XFAIL so nobody reads it as something to make green.

HOW TO RUN IT.

    cd <repo>
    source .venv/bin/activate
    python3 regtest_htlc_verify.py --wipe

This is NOT a pytest test and is deliberately not under tests/: it needs two
external daemons, it starts processes, and it mines four-figure numbers of
blocks. `python3 -m pytest` must stay runnable on a machine with no chain at
all. Everything in the harness that CAN be tested without a daemon is tested
in tests/test_regtest_harness_units.py.

FLAGS

    --chain {btc,ltc,both}   which chains to run. Default both.
    --wipe                   delete each datadir's `regtest` subdirectory
                             before starting, so the run begins on an empty
                             chain. Refuses if a daemon is still answering.
    --keep-running           skip the teardown and leave the daemons up, for
                             poking at the chain afterwards. The harness still
                             names what it left running.
    --verbose-clients        leave the real clients' loggers at DEBUG. They
                             print every RPC payload, which is useful once and
                             unreadable twice, so the default raises them to
                             INFO.
    --ltc-mweb               do NOT attempt to hold Litecoin's MWEB deployment
                             inactive. By default the harness asks litecoind's
                             own -help whether it accepts a deployment override
                             and passes one if it does, because mining toward
                             the LTC locktime failed with bad-txns-vin-empty on
                             2026-09-25. If the daemon refuses to start with
                             it, the harness says so and starts again without.

ENVIRONMENT (every default matches the operator's described machine)

    ST_REGTEST_BTC_DATADIR       ~/regtest/btc
    ST_REGTEST_BTC_RPC_PORT      18443
    ST_REGTEST_BTC_RPC_USER      rt
    ST_REGTEST_BTC_RPC_PASSWORD  rt
    ST_REGTEST_BTC_DAEMON        bitcoind
    ST_REGTEST_BTC_CLI           bitcoin-cli
    ST_REGTEST_LTC_DATADIR       ~/regtest/ltc
    ST_REGTEST_LTC_RPC_PORT      19443
    ST_REGTEST_LTC_RPC_USER      rt
    ST_REGTEST_LTC_RPC_PASSWORD  rt
    ST_REGTEST_LTC_DAEMON        litecoind
    ST_REGTEST_LTC_CLI           litecoin-cli
    ST_REGTEST_RPC_HOST          127.0.0.1  (both chains, unless overridden)

    Every name is ASCII and every one that is a duration says _SECONDS, which
    is rule 6's boundary: micro in what a human reads, ASCII in what a shell
    has to export.

WHAT IT PRINTS, AND WHY IT PRINTS SO MUCH.

This file was written on a machine with no bitcoind and no litecoind, where it
could not be run even once. It will first execute on somebody else's computer.
A harness that fails there with a bare traceback costs a round trip; one that
says `step 4/9 [BTC] ... height=101 expected >=101 OK` locates its own failure.
So every stage announces its target before it acts, every assertion prints what
it got beside what it wanted, and an empty result prints `(none)` rather than
nothing (CLAUDE.md rule 14).

Durations are microfortnights with the seconds in parentheses. Block heights,
locktimes and confirmation counts are NEVER converted -- a block is not 1.2096
seconds long, it is however long it took (rule 6).

The preimage is never printed. Not once, not at DEBUG, not in an error. The
secret_hash is, because it is in the script and public by construction.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
APP_ROOT = REPO_ROOT / "swap_terminal"
if str(APP_ROOT) not in sys.path:
    # The application imports its own modules rootlessly (`from config import
    # Config`, `from modules.utils import ...`), so swap_terminal/ has to be
    # importable. tests/conftest.py does the same thing for the same reason.
    # That is CLAUDE.md rule 10's layout gap; papering over it by rewriting
    # every import in the tree would be a large diff with no behavioral gain.
    sys.path.insert(0, str(APP_ROOT))

from modules.atomic_btc_client import BTCClient  # noqa: E402 -- same
from modules.atomic_ltc_client import LTCClient  # noqa: E402 -- same
from regtest import daemons, steps  # noqa: E402 -- the sys.path line above must run first
from regtest.console import FAIL, OK, Console  # noqa: E402 -- same
from regtest.daemons import RegtestSetupError, resolve_chain_config  # noqa: E402 -- same
from regtest.keys import generate_key  # noqa: E402 -- same

WALLET_NAME = "regtest_htlc_harness"

# The loggers the real modules install at import. They set themselves to DEBUG
# and attach their own StreamHandler, which prints every RPC payload including
# every raw transaction. Useful exactly once. Raised to INFO by default and
# left at DEBUG by --verbose-clients; propagate is turned off so their own
# handler does not print each line a second time through the root logger.
CLIENT_LOGGERS = (
    "modules.atomic_btc_client",
    "modules.atomic_ltc_client",
    "modules.atomic_htlc_scripts",
    "modules.utils",
)


def configure_logging(verbose_clients: bool) -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    level = logging.DEBUG if verbose_clients else logging.INFO
    for name in CLIENT_LOGGERS:
        logger = logging.getLogger(name)
        logger.setLevel(level)
        logger.propagate = False


def build_real_client(asset: str, config, wallet: str):
    """Construct the REAL client for this chain, pointed at the wallet endpoint.

    The two clients differ in how they reach a wallet, which is one more row in
    the divergence table their own headers carry:

      BTCClient  appends `/wallet/<BTC_RPC_WALLET>` to whatever URL it is given
                 unless the URL already contains `/wallet/`. So the wallet name
                 goes in the environment, not the URL.
      LTCClient  never appends anything, so the wallet endpoint has to be baked
                 into the URL. Against a daemon with exactly one wallet loaded
                 the bare URL would also work, but relying on that would make
                 the harness's behavior depend on how many wallets the operator
                 happens to have.
    """
    if asset == "BTC":
        os.environ["BTC_RPC_WALLET"] = wallet
        return BTCClient(config.base_url, config.rpc_user, config.rpc_password)

    # LTCClient.redeem_contract() pays a 0.25% platform fee to
    # PLATFORM_FEE_LTC_ADDRESS, whose default in that file is a hardcoded
    # `tltc1...` address -- a Litecoin TESTNET bech32 address, which a regtest
    # daemon rejects because regtest's human-readable part is `rltc`. Left
    # unset, the redeem would fail inside createrawtransaction on the address,
    # and the harness would report an address-encoding problem in the place it
    # is trying to measure a preimage problem. A throwaway regtest address is
    # substituted so the failure that arrives is the one under test, and this
    # comment is the record that the default is mainnet-shaped.
    os.environ.setdefault("PLATFORM_FEE_LTC_ADDRESS", generate_key().address)
    return LTCClient(f"{config.base_url}/wallet/{wallet}", config.rpc_user, config.rpc_password)


def run_chain(console: Console, asset: str, args: argparse.Namespace) -> steps.ChainOutcome:
    """Run all nine steps against one chain. Always tears down what it started."""
    console.banner(f"{asset} -- regtest HTLC verification, nine steps")
    config = resolve_chain_config(asset)
    outcome = steps.ChainOutcome(asset=asset)
    run = steps.Run(console=console, config=config, wallet=WALLET_NAME)
    try:
        steps.step_1_binaries(run)
        # Between steps 1 and 2: after the binary is known present, before it
        # is started, because it works by reading that binary's own -help.
        # Measured 2026-09-25: mining toward the LTC locktime died several
        # hundred blocks in with bad-txns-vin-empty, and Litecoin's MWEB is the
        # leading hypothesis. See regtest/daemons.py::mweb_override_args.
        if asset == "LTC" and not args.ltc_mweb:
            steps.apply_mweb_override(run)
        elif asset == "LTC":
            run.say("--ltc-mweb given, so MWEB is left to activate normally")
        if args.wipe:
            console.say(f"{asset}: --wipe requested")
            daemons.wipe_datadir(console, config)
        steps.step_2_daemon(run)
        steps.step_3_wallet(run)
        height = steps.step_4_maturity(run)

        client = build_real_client(asset, config, WALLET_NAME)
        contract = steps.step_5_build_contract(run, height)
        contract_a, contract_b = steps.step_6_fund(run, client, contract, outcome)
        steps.step_7_redeem(run, client, contract, contract_a, outcome)
        steps.step_8_refund_before_expiry(run, contract, contract_b, outcome)
        steps.step_9_refund_after_expiry(run, contract, contract_b, outcome)
    except RegtestSetupError as exc:
        # A named precondition failed and the message carries the fix. It is
        # printed as an assertion rather than raised, so the other chain still
        # runs and the teardown below still happens.
        console.check(f"{asset} setup", str(exc), "the precondition to hold", FAIL)
        outcome.notes.append(f"setup failed: {exc}")
    except Exception as exc:  # noqa: BLE001 -- checked: an unexpected exception must not skip the teardown below, which is this harness's only reaper for the daemons it spawned (rule 13). It is recorded as a FAIL with its type and message, never swallowed into a pass, and the run's exit code reflects it.
        console.check(f"{asset} run", f"{type(exc).__name__}: {exc}", "no unhandled exception", FAIL)
        outcome.notes.append(f"unhandled {type(exc).__name__}: {exc}")
    finally:
        console.banner(f"{asset} -- teardown")
        # run.spawn.started, NOT a local set from step 2's return value: step 2
        # can raise after it has spawned, and a reaper that depends on the
        # spawner returning normally is how an orphan survives a stop (rule 13).
        if args.keep_running:
            console.say(
                f"{asset}: --keep-running, so the daemon at {config.base_url} is LEFT UP"
                f"{' (this harness started it)' if run.spawn.started else ' (this harness did not start it)'}. "
                "Stop it yourself when you are done."
            )
        else:
            daemons.stop_daemon(console, config, run.spawn.started)

    return outcome


def print_verdicts(console: Console, outcomes: list[steps.ChainOutcome]) -> None:
    console.banner("WHICH BRANCH OF A FUNDED CONTRACT ACTUALLY SPENDS")
    if not outcomes:
        console.say("(none: no chain was run)")
        return
    for outcome in outcomes:
        console.say(f"{outcome.asset}: {outcome.verdict()}")
        console.say(
            f"{outcome.asset}:   real create_contract()={outcome.real_create_contract}  "
            f"real redeem_contract()={outcome.real_redeem}  control hashlock spend={outcome.control_redeem}  "
            f"refund refused before expiry={outcome.refund_before_expiry_rejected}  "
            f"refund after expiry={outcome.refund_after_expiry}"
        )
        for note in outcome.notes:
            console.say(f"{outcome.asset}:   note: {note}")


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="regtest_htlc_verify.py",
        description=(
            "Drive the real HTLC modules against local bitcoind and litecoind regtest nodes. "
            "REFUSES to run against any chain but regtest."
        ),
    )
    parser.add_argument("--chain", choices=("btc", "ltc", "both"), default="both", help="which chains to run")
    parser.add_argument("--wipe", action="store_true", help="delete each datadir's regtest subdirectory first")
    parser.add_argument("--keep-running", action="store_true", help="leave the daemons up after the run")
    parser.add_argument("--verbose-clients", action="store_true", help="leave the real clients' loggers at DEBUG")
    parser.add_argument(
        "--ltc-mweb",
        action="store_true",
        help=(
            "do NOT try to hold Litecoin's MWEB deployment inactive. The default attempts it, because mining "
            "toward the locktime failed with bad-txns-vin-empty on 2026-09-25 and MWEB is the leading hypothesis"
        ),
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(list(sys.argv[1:] if argv is None else argv))
    configure_logging(args.verbose_clients)
    console = Console(total_steps=steps.TOTAL_STEPS)

    assets = {"btc": ["BTC"], "ltc": ["LTC"], "both": ["BTC", "LTC"]}[args.chain]
    console.banner("regtest HTLC verification harness")
    console.say(f"chains={assets}  wipe={args.wipe}  keep_running={args.keep_running}")
    console.say("network=regtest for every chain; the harness aborts a chain that reports anything else")
    console.say(
        "the code under test is modules/atomic_htlc_scripts.py, modules/htlc_timelock.py and the real "
        "BTC/LTC clients. Lines labeled `control` are the harness's own spender, used because no refund "
        "implementation exists in this tree to drive."
    )

    outcomes = [run_chain(console, asset, args) for asset in assets]
    print_verdicts(console, outcomes)
    console.summary()

    failed = console.counts[FAIL]
    console.banner(
        f"exit code {1 if failed else 0} -- "
        + ("unexpected failures above" if failed else f"no unexpected failures ({console.counts[OK]} checks OK)")
    )
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
