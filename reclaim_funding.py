#!/usr/bin/env python3
"""Empty a seed-derived funding address back into your wallet.

Role: file (the entry point; the decisions are adaptor_steps.reclaim_p2pkh() and
      p2pkh_script_for_address(), and it holds none of its own)
Reads: ST_ADAPTOR_FUNDING_SEED, and one daemon over JSON-RPC
Writes: nothing on disk. With --send it BROADCASTS one transaction.
Can move funds: YES -- on a test network, and structurally nowhere else. The keys it derives
      carry the testnet version byte, and the destination's version byte is checked against
      the configured network before anything is built.
Mainnet-safe: NO, AND IT REFUSES TO BE ASKED, the same way adaptor_regtest_verify.py does:
      the daemon must SAY it is on a test network before a byte is built, and an absence of
      evidence is treated as mainnet.
Live-safe: yes in the sense that it starts and stops nothing and never asks the wallet to
      sign. It broadcasts only with --send.

WHY THIS EXISTS. `adaptor_regtest_verify.py` asks the operator to fund an address derived from
ST_ADAPTOR_FUNDING_SEED. Change the seed -- or open a new terminal without the export -- and
that address is no longer derivable from what the shell holds, so the coins at it are invisible
to the harness and to the wallet alike: the wallet does not own the address, `importaddress` is
False on Gridcoin v5.5.1.0, and `gettxout` is False too.

MEASURED ON THE OPERATOR'S HOST, 2026-09-28: two addresses, funded from two different seeds
across one evening, holding 3.499 and 4.60 GRC. Neither was recoverable by any command in this
tree. The seeds were both still in their shell history, which is what makes this possible at
all -- and the harness now says so at the moment it refuses.

    export ST_ADAPTOR_FUNDING_SEED='the seed that derived the address you want to empty'
    python3 reclaim_funding.py --to <a wallet address>          # prints the bytes, sends nothing
    python3 reclaim_funding.py --to <a wallet address> --send   # broadcasts

THE SEED IS NEVER AN ARGUMENT, and that is not an oversight. A value on the command line is
world-readable through /proc and `ps`, and it lands in the shell history of anyone who runs it.
The seed derives a key that controls coins; it goes in the environment or nowhere.

--send IS SEPARATE FROM BUILDING ON PURPOSE. Everything except the broadcast runs by default,
so the operator sees the destination, the amount and the fee BEFORE anything moves, and the
transaction they approve is the one that goes. That is rule 16's shape for a thing that moves
money: the act that moves it is explicit and separate.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# The sys.path line must run before these imports: this repository's modules import each other
# rootlessly (`from config import Config`), which is CLAUDE.md rule 10's layout gap rather than
# a choice made here. E402 is ignored repo-wide for exactly this idiom, so no suppression is
# needed and none is added (rule 19).
sys.path.insert(0, str(Path(__file__).resolve().parent / "swap_terminal"))

from chains.base import RPCError
from modules.htlc_spend import satoshis_to_coins
from regtest import adaptor_steps
from regtest.adaptor_steps import Run
from regtest.console import FAIL, OK, Console
from regtest.daemons import RegtestSetupError

TOTAL_STEPS = 5


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="reclaim_funding.py",
        description=(
            "Empty the funding address derived from ST_ADAPTOR_FUNDING_SEED back into a wallet "
            "address you name. Builds and signs in this process; the wallet is never asked to "
            "sign anything, so a staking-only unlock does not matter. Prints the transaction "
            "and sends NOTHING unless --send is given."
        ),
    )
    parser.add_argument("--to", required=True, help="the P2PKH address to pay -- get one from your wallet")
    parser.add_argument("--chain", choices=("btc", "ltc", "grc"), default="grc", help="which chain")
    parser.add_argument(
        "--send", action="store_true",
        help="BROADCAST it. Without this the transaction is built, signed and printed only.",
    )
    parser.add_argument(
        "--funding-txid", default="",
        help="the payment to spend, when the wallet does not remember making it (it usually does)",
    )
    return parser.parse_args(argv)


def reclaim(console: Console, args: argparse.Namespace) -> int:
    """The five steps, and the only one that moves anything is the last.

    Returns a process exit code. Every refusal is a RegtestSetupError caught in main(), so a
    precondition reads differently from a chain saying no -- the same distinction
    adaptor_regtest_verify.py draws, and for the same reason: they need different things from
    whoever is looking at the screen.
    """
    asset = args.chain.upper()
    console.banner(f"{asset} -- reclaim a seed-derived funding address")
    config = adaptor_steps.resolve_config(asset)
    run = Run(console=console, config=config, wallet="")
    console.say(f"{asset}: endpoint {config.base_url}")

    console.step(1, asset, "the daemon answers, and it SAYS which network it is on")
    adaptor_steps.step_1_reachable(run)
    adaptor_steps.assert_test_network(run)

    console.step(2, asset, "derive the funding address from the seed in the environment")
    key = adaptor_steps.operator_funding_key(run)
    if key is None:
        raise RegtestSetupError(
            f"{adaptor_steps.FUNDING_SEED_VARIABLE} is not set, so there is no address to empty. "
            f"Export the seed that derived the address you want back -- `history | grep "
            f"{adaptor_steps.FUNDING_SEED_VARIABLE}` usually has it -- and run this again."
        )
    console.check(f"{asset} funding address derived from the seed", key.address,
                  "the address you funded", OK)

    console.step(3, asset, "find the payment the wallet made to it")
    txid = args.funding_txid or adaptor_steps.discover_operator_funding_txid(run, key)
    if not txid:
        raise RegtestSetupError(
            f"{asset}: the wallet remembers no payment to {key.address}. Either this is not the "
            f"seed that derived the address you funded -- the address above is its fingerprint, "
            f"so compare it -- or the payment came from somewhere this wallet has no record of, "
            f"in which case pass --funding-txid."
        )
    source = adaptor_steps.find_operator_funding(run, key, txid)

    console.step(4, asset, "build and sign the spend -- IN THIS PROCESS, and broadcast nothing yet")
    raw, predicted, value = adaptor_steps.reclaim_p2pkh(run, key, source, args.to)
    console.check(
        f"{asset} built and signed", f"{satoshis_to_coins(value)} to {args.to}",
        f"the whole output less the fee ({satoshis_to_coins(source.value_satoshis - value)})", OK,
    )
    console.say(f"{asset}: predicted txid {predicted}; {len(raw) // 2} bytes")
    console.say(f"{asset}: spending {source.txid}:{source.vout}")

    console.step(5, asset, "broadcast -- ONLY with --send")
    if not args.send:
        console.say(f"{asset}: NOTHING WAS BROADCAST. Re-run with --send to move it:")
        console.say(f"{asset}:   python3 reclaim_funding.py --to {args.to} --chain {args.chain} --send")
        console.say(f"{asset}: the seed stays in the environment; it is never an argument")
        return 0
    try:
        sent = run.node(wallet=False).call("sendrawtransaction", raw)
    except RPCError as exc:
        console.check(f"{asset} broadcast", str(exc), "a txid", FAIL)
        console.say(f"{asset}: nothing moved. If this says the inputs are missing or spent, the "
                    f"output has already been used -- a completed harness run spends its funding.")
        return 1
    matched = sent == predicted
    console.check(f"{asset} BROADCAST", f"predicted={predicted} daemon={sent}", "the same txid",
                  OK if matched else FAIL)
    console.say(f"{asset}: {satoshis_to_coins(value)} is on its way to {args.to}")
    return 0 if matched else 1


def main(argv: list[str], console: Console | None = None) -> int:
    """`console` is a parameter so a test can read what an operator would see.

    Console's default stream is bound to `sys.stdout` at DEFINITION time, so replacing
    sys.stdout afterwards -- which is what pytest's capsys does -- captures nothing and a test
    asserting on the output silently passes over an empty string. Taking the console instead is
    rule 10's answer: the decision about where output goes belongs to the caller, and a
    function that reaches for a global cannot be driven with seeded inputs.
    """
    console = Console(TOTAL_STEPS) if console is None else console
    try:
        return reclaim(console, parse_args(argv))
    except RegtestSetupError as exc:
        console.banner("REFUSED AT A PRECONDITION")
        console.say(str(exc))
        console.say("Nothing was built, signed or broadcast.")
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
