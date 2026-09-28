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

# SIX, not five, and this is a correction rather than a count. The first run printed two
# `step 1/5` lines and two `step 2/5` lines, because `step_1_reachable` and
# `assert_test_network` print their OWN headers -- they are borrowed whole from
# adaptor_regtest_verify's step 1 and step 2 -- and this file then printed its own on top.
#
# A numbering that repeats itself is worse than no numbering: the operator reads "step 2/5",
# sees another "step 2/5", and now has to work out whether something re-ran. Rule 14 is about
# exactly that -- the operator reads the screen, not the source. So the borrowed steps keep
# their numbers and this file's own start at 3.
TOTAL_STEPS = 6


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
    destination = parser.add_mutually_exclusive_group(required=True)
    destination.add_argument("--to", help="the P2PKH address to pay -- one from your wallet")
    destination.add_argument(
        "--to-wallet", action="store_true",
        help=(
            "pay an address the wallet ALREADY OWNS, found via listunspent. Needs no unlock and "
            "creates no new key. Use this when you do not have an address to hand."
        ),
    )
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

    # Steps 1 and 2 are printed by the two calls themselves, borrowed whole from
    # adaptor_regtest_verify: the daemon answering, and the daemon SAYING which network it is
    # on. Printing a header here as well is what produced the duplicate numbering.
    adaptor_steps.step_1_reachable(run)
    adaptor_steps.assert_test_network(run)

    console.step(3, asset, "derive the funding address from the seed in the environment")
    key = adaptor_steps.operator_funding_key(run)
    if key is None:
        raise RegtestSetupError(
            f"{adaptor_steps.FUNDING_SEED_VARIABLE} is not set, so there is no address to empty. "
            f"Export the seed that derived the address you want back -- `history | grep "
            f"{adaptor_steps.FUNDING_SEED_VARIABLE}` usually has it -- and run this again."
        )
    console.check(f"{asset} funding address derived from the seed", key.address,
                  "the address you funded", OK)

    console.step(4, asset, "find the payment the wallet made to it")
    txid = args.funding_txid or adaptor_steps.discover_operator_funding_txid(run, key)
    if not txid:
        # IT USED TO SAY "the wallet remembers no payment to <address>" IN EVERY CASE, and on
        # 2026-09-28 it said that directly beneath twenty lines listing THREE payments it had
        # just found, read off the chain, and skipped as spent. A refusal that contradicts the
        # output above it is worse than no refusal: the operator has to decide which half of
        # one screen to believe.
        #
        # The cause is that `discover_operator_funding_txid` changed meaning earlier the same
        # day -- None went from "nothing was found" to "nothing USABLE was found" -- and this
        # message did not follow. prepare_operator_funding had the identical sentence and was
        # fixed; this second copy was missed, which is rule 8's failure with a delay on it,
        # measured at about four hours.
        #
        # `no_usable_funding_message` is now the ONE implementation, and it reads
        # run.skipped_funding_payments to tell "all spent" from "none at all". The
        # seed-fingerprint hint stays, because it is the one thing this tool knows that the
        # shared message does not: a reclaim is usually run BECAUSE the operator suspects they
        # used a different seed.
        raise RegtestSetupError(
            adaptor_steps.no_usable_funding_message(run, key)
            + f"\n  If you expected a payment here and there is none, check that this is the "
              f"seed that derived the address you funded -- {key.address} is its fingerprint, so "
              f"compare it -- or pass --funding-txid if the payment came from somewhere this "
              f"wallet has no record of."
        )
    source = adaptor_steps.find_operator_funding(run, key, txid)

    console.step(5, asset, "build and sign the spend -- IN THIS PROCESS, and broadcast nothing yet")
    destination = args.to or adaptor_steps.wallet_owned_address(run)
    raw, predicted, value = adaptor_steps.reclaim_p2pkh(run, key, source, destination)
    console.check(
        f"{asset} built and signed", f"{satoshis_to_coins(value)} to {destination}",
        f"the whole output less the fee ({satoshis_to_coins(source.value_satoshis - value)})", OK,
    )
    console.say(f"{asset}: predicted txid {predicted}; {len(raw) // 2} bytes")
    console.say(f"{asset}: spending {source.txid}:{source.vout}")

    # IS THAT OUTPUT STILL THERE? Ask before claiming the dry run is fine.
    #
    # MEASURED ON THE OPERATOR'S HOST, 2026-09-28. They pointed this at a seed whose funding a
    # completed harness run had already SPLIT AND SPENT, and the dry run reported
    # "built and signed: 4.59000000 to ..." as if nothing were wrong. find_operator_funding()
    # reads the vout out of the FUNDING TRANSACTION, and a transaction's outputs do not stop
    # existing when they are spent -- so it cannot tell. Gridcoin has no `gettxout`, which is
    # the call that would normally answer this.
    #
    # `testmempoolaccept` runs the same AcceptToMemoryPool WITHOUT broadcasting, so the question
    # costs nothing and is asked here rather than discovered by --send. A dry run whose whole
    # purpose is "see it before it moves" must not show a healthy-looking spend of an output
    # that is gone.
    #
    # AND IT NEVER RAN. MEASURED 2026-09-28, one day later, on the same host: Gridcoin answers
    # `testmempoolaccept: code=-32601 message=Method not found`. The check written above to stop
    # a dry run looking healthy over a spent output had itself never executed -- it returned
    # "the daemon will not say" every single time and this code read that as acceptance. The
    # comment above is kept exactly as written because that is the defect: it describes a check
    # that does not run on the only chain this tool is used against.
    #
    # `find_the_spender` walks blocks instead, which needs only `getblockhash` and `getblock`.
    # Same question, answered from the chain rather than from a method the daemon does not have.
    answer = adaptor_steps.mempool_answer(run, raw)
    console.say(f"{asset}: asked the daemon whether it would accept this -- {answer.description}")
    reason = answer.reason
    spender = None
    if not reason:
        spender, description = adaptor_steps.find_the_spender(run, source)
        console.say(f"{asset}: asked the chain instead -- {description}")
    if reason or spender:
        console.check(f"{asset} the output is still there",
                      f"reject-reason={reason!r}" if reason else f"spent by {spender}",
                      "an unspent output", FAIL)
        # DELIBERATELY NOT THE SAME SENTENCE as the --send offer below, which also opens
        # "NOTHING WAS BROADCAST". Rule 13: a run that did nothing because it CANNOT proceed
        # must not read like a run that did nothing because it is waiting to be told to.
        console.say(f"{asset}: NOTHING WAS BROADCAST, AND NOTHING WILL BE -- there is nothing left to spend.")
        console.say(f"{asset}: THIS OUTPUT IS ALREADY SPENT -- a completed "
                    f"adaptor_regtest_verify.py run splits and spends its funding. Nothing is "
                    f"stranded at {key.address} if a run consumed it; it was used.")
        return 1
    console.check(f"{asset} the output is still there", "no spender found",
                  "an unspent output -- asked without broadcasting anything", OK)

    console.step(6, asset, "broadcast -- ONLY with --send")
    if not args.send:
        console.say(f"{asset}: NOTHING WAS BROADCAST. Re-run with --send to move it:")
        console.say(f"{asset}:   python3 reclaim_funding.py --to {destination} --chain {args.chain} --send")
        console.say(f"{asset}:   (--to with the address above, so the SECOND run pays exactly what "
                    f"the first one showed you -- --to-wallet could pick a different one)")
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
    console.say(f"{asset}: {satoshis_to_coins(value)} is on its way to {destination}")
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
