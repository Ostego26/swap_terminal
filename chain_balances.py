#!/usr/bin/env python3
"""What the Bitcoin, Litecoin and Gridcoin wallets hold. Read-only.

Role: file (operator entry point, at the project root per CLAUDE.md rule 10)
Reads: whichever of the BTC/LTC/GRC daemons the environment configures, over
        JSON-RPC. config.Config for the connection details.
Writes: nothing. No file, no database, no chain.
Can move funds: NO. It calls getblockchaininfo, getblockcount, getbalance,
        getbalances and getwalletinfo, and nothing else. It never unlocks a
        wallet, never builds a transaction and never calls sendtoaddress; the
        adapter it holds HAS a send_to_address() method and
        tests/test_chain_balances.py walks this file's AST to assert it is not
        called, the same way tests/test_xrp_balances.py does for XRP.
Mainnet-safe: it REFUSES a mainnet daemon rather than declining to act on one,
        and the refusal comes BEFORE any wallet call. See the next section --
        on Gridcoin, looking is itself the hazard.

WHY THE NETWORK CHECK COMES FIRST, AND WHY IT IS AN ALLOWLIST

The operator's Gridcoin mainnet wallet is a live staking wallet holding real
coins; the testnet one is port 25715. A balance reader pointed at the wrong port
is not a wrong number on a screen, it is this tree touching a wallet it has no
business touching, which is why swap_readiness.py refuses to poll a mainnet
Gridcoin wallet rather than merely declining to act on the reading.

So each daemon is asked what network it is on before anything asks it about
money, and the answer must be in that chain's own allowlist in
chains/daemon_network.py. "Anything that is not main" would authorize a network
none of these three daemons has ever answered; an unreadable network returns
"unknown" and is refused, never assumed.

WHAT THIS DOES NOT SHOW, said out loud because a balance that silently omits
something is worse than no balance at all: coins sitting in an unspent HTLC. A
funded P2SH is not in the wallet -- the wallet does not watch that address -- so
`getbalance` cannot see it and neither can this. On 2026-09-29 the XRP side of
the same question turned out to have 1 XRP locked in an expired escrow that no
balance line mentioned, and the script-chain equivalent would need the contract
list rather than the wallet. That is real work and it is not done; nothing here
pretends otherwise.
"""

from __future__ import annotations

import argparse
import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "swap_terminal"))

from chains.daemon_conf import CONF_FALLBACK_NETWORK, conf_fallback_settings
from chains.daemon_network import CHAIN_TEST_NETWORKS, chain_network
from chains.registry import build_adapters, why_unconfigured
from chains.wallet_hint import (
    NO_WALLET_LOADED,
    which_wallets_are_on_disk,
)
from chains.wallet_lock import encryption_state
from config import Config
from regtest.daemons import CHAIN_DEFAULTS
from services.custody_separation import wallet_label
from services.wallet_leveling import (
    DEFAULT_TARGET_USD,
    PEG_ASSETS,
    WalletValue,
    even_target,
    moves_to,
    peg_findings,
)
from step_console import Console

# Every chain this can report, in the order it reports them. Derived from the
# allowlist rather than spelled again: a chain that has no test-network
# vocabulary has no business being polled by this, and a hand-written second
# list is rule 8's shape (it is exactly how VALID_HORIZONS drifted in the other
# repo whose rules this tree follows).
CHAINS = tuple(sorted(CHAIN_TEST_NETWORKS))

NOTHING_TO_LOOK_AT = 3


def adapter_from_conf(console: Console, chain: str):
    """An adapter built from this chain's own conf, or None with the reason said.

    THE ENVIRONMENT STILL WINS. This runs only for a chain Config.RPC does not
    configure, so an operator who exported LTC_RPC_PORT gets exactly what they
    exported -- a fallback that overrode an explicit setting would be the worse
    half of rule 8, two sources with the quiet one winning.

    The RESOLUTION moved to chains/daemon_conf.py on 2026-09-29, the same day
    this grew it, because atomic_swap_xrp.py needed the identical answer and did
    not have it: the operator configured Litecoin, this reader found it, and the
    swap driver then said "(none)" about the same daemon.
    """
    settings, line = conf_fallback_settings(chain)
    console.say(f"    {line}")
    if settings is None:
        return None
    # CONSTRUCTED THROUGH build_adapters, not by naming an adapter class here.
    # registry.py owns which class each chain gets and what counts as
    # configured; a second constructor would be a second answer to both, and
    # missing_settings() is the check that stopped a port-with-no-password from
    # building an adapter that 401s on every call (2026-09-26).
    return build_adapters({chain: settings}).get(chain)


# WHAT A ConnectionError MEANS, as opposed to every other unreadable network.
# Nothing is listening on that host and port, which is a daemon that is not
# running -- not a refusal, not a wrong credential, not a mainnet answer. The
# distinction matters because the ACTION differs for each and only this one is
# "start it".
NOTHING_LISTENING = "ConnectionError"


def what_to_do_about_it(chain: str, network: str) -> str:
    """One actionable line for a network this could not read.

    ADDED 2026-09-29 BECAUSE THE OPERATOR HAD TO ASK. The output said "REFUSED to
    ask this daemon about a balance" beside "unknown (getblockchaininfo:
    ConnectionError)". True, complete, and missing the one thing a reader needs:
    the daemon is not running, and here is the command. They ran the same read
    twice and got the same non-answer. That is rule 14's defect -- the screen
    described a state where it could have described a next step.

    THE COMMAND IS PRINTED, NOT RUN. Nothing in this tree starts a daemon on the
    operator's behalf and a read-only balance reader is the last place that
    should change; tests/test_chain_balances.py asserts this file calls no spawn
    path. What is printed is the argv regtest/daemons.py builds, minus its two
    -debug options, which exist for a consensus-refusal message a balance read
    has no use for.
    """
    if NOTHING_LISTENING not in network:
        # A daemon that ANSWERED and named a network outside the allowlist is a
        # different problem, and "start it" would be wrong -- it is running. That
        # covers the mainnet case among others, so this says what is true of all
        # of them rather than guessing which one happened.
        return ("that daemon ANSWERED -- it is running, and what it said is not a network this will "
                "read a wallet on. Check which daemon is on that port before anything else.")
    spec = CHAIN_DEFAULTS.get(chain)
    if chain not in CONF_FALLBACK_NETWORK or spec is None:
        return (f"nothing is listening on that host and port, so no {chain} daemon is running there. "
                f"Start yours, or point {chain}_RPC_PORT at the one that is.")
    datadir = Path(spec["datadir"]).expanduser()
    return (f"nothing is listening on that host and port, so no {chain} daemon is running there. "
            f"Start it and re-run this:  {spec['daemon']} -datadir={datadir} "
            f"-{CONF_FALLBACK_NETWORK[chain]} -daemon    (this script will NOT start it for you)")


def report_chain(console: Console, chain: str, adapters: dict) -> Decimal | None:
    """One chain's holdings, and its spendable amount. None if it could not be read.

    RETURNS THE AMOUNT RATHER THAN A BOOLEAN since 2026-09-29, because the
    leveling step needs the number and re-reading it would be a second call
    against the same daemon for a figure this function already has.
    """
    adapter = adapters.get(chain) or adapter_from_conf(console, chain)
    if adapter is None:
        routes = f"{chain}_RPC_* in the environment"
        if chain in CONF_FALLBACK_NETWORK:
            routes += ", or an rpcuser/rpcpassword/rpcport in this chain's own conf"
        console.check(f"{chain} configured", "no", routes, False)
        console.say(f"    {why_unconfigured(chain, Config.RPC)}")
        return None

    # THE NETWORK, BEFORE THE MONEY. See this module's header: on Gridcoin a
    # mainnet wallet is the operator's live staking wallet and reading it is
    # itself the thing to refuse.
    network = chain_network(adapter)
    safe = CHAIN_TEST_NETWORKS[chain]
    if network not in safe:
        console.check(f"{chain} network", network, f"one of {sorted(safe)}", False)
        console.say("    REFUSED to ask this daemon about a balance. Nothing was read from its "
                    "wallet. An 'unknown (...)' answer above names which RPC would not say.")
        console.say(f"    {what_to_do_about_it(chain, network)}")
        return None
    console.check(f"{chain} network", network, f"one of {sorted(safe)}", True)

    height = "unreadable"
    try:
        height = adapter.call("getblockcount")
    except Exception as error:  # noqa: BLE001 -- checked: the height is context for the balance, not the balance. A daemon that answered getblockchaininfo and refuses getblockcount is odd and worth SEEING, so the reason is printed and the balance is still attempted; nothing downstream reads this value.
        console.say(f"    height    unreadable ({type(error).__name__}: {error})")
    else:
        console.say(f"    height    {height}")

    try:
        spendable = float(adapter.call("getbalance"))
    except Exception as error:  # noqa: BLE001 -- checked: this IS the balance, so a failure here means this chain was not reported, which is what False tells main(). The exception text is printed rather than swallowed.
        console.check(f"{chain} balance", f"{type(error).__name__}: {error}", "getbalance", False)
        if NO_WALLET_LOADED in str(error):
            console.say(f"    {which_wallets_are_on_disk(adapter, chain)}")
        return None

    # BOTH HALVES OF THE BALANCE. `getbalance` reports only what is SPENDABLE, so
    # a wallet whose coins are freshly mined or freshly received reads lower than
    # its total and an operator reads that as a loss. fund_testnets.py prints the
    # same two figures for the same reason -- "balance 0.00076293 after mining 101
    # blocks" reads as a failure until the immature column is beside it.
    immature, untrusted, extra = 0.0, 0.0, ""
    try:
        balances = adapter.call("getbalances") or {}
        mine = balances.get("mine") or {}
        immature = float(mine.get("immature", 0.0))
        untrusted = float(mine.get("untrusted_pending", 0.0))
    except Exception as error:  # noqa: BLE001 -- checked: getbalances is absent on older daemons (Gridcoin among them) and its absence costs only these two reporting lines. NAMED in the output below rather than swallowed, and `spendable` came from a separate call that already succeeded.
        extra = f"not reported ({type(error).__name__}: {error})"

    # WHICH WALLET THE FIGURE CAME OUT OF, added 2026-10-03. This block printed
    # four amounts and never said which wallet they belonged to, and on this tree
    # that is the question: BTC_RPC_WALLET / LTC_RPC_WALLET / GRC_RPC_WALLET are
    # all `_env(..., "")`, so an unset one addresses the daemon with no
    # /wallet/<name> path and the daemon routes to its DEFAULT wallet -- the same
    # one an operator's own CLI reaches. A pasted balance that does not name its
    # wallet cannot be told apart a day later from a balance of a different one,
    # which is rule 14's "echo the parameters that decide the answer".
    #
    # wallet_label() rather than a fourth spelling of "(default wallet)": the
    # phrase lives in services/custody_separation.py and is shared with the worker
    # banner and the admin page's Chains table (rule 8).
    console.say(f"    wallet    {wallet_label(chain, getattr(adapter, 'wallet', '') or '')}")
    console.say(f"    spendable {spendable:.8f} {chain}  <- the WHOLE wallet named above, not desk stock")
    if extra:
        console.say(f"    immature  {extra}")
        console.say(f"    pending   {extra}")
    else:
        console.say(f"    immature  {immature:.8f} {chain}  <- mined, not yet spendable")
        console.say(f"    pending   {untrusted:.8f} {chain}  <- received, not yet trusted")
        total = spendable + immature + untrusted
        console.say(f"    total     {total:.8f} {chain}")

    # WHETHER A PAYOUT WOULD NEED A PASSPHRASE, which is the other thing an
    # operator wants to know before planning a swap and cannot see from a number.
    # encryption_state() never raises and returns its own reason.
    needs_passphrase, why = encryption_state(adapter)
    console.say(f"    wallet    {'ENCRYPTED -- a payout needs a passphrase' if needs_passphrase else 'not encrypted'}"
                f" ({why})")
    console.check(f"{chain} balance", f"{spendable:.8f} spendable", "read", True)
    return Decimal(str(spendable))


def say_what_levels_them(console: Console, held: dict, target: str, *, even: bool) -> None:
    """Price every wallet, check the dollar, and print the moves. Nothing is sent.

    THE VALUES ARE NOTIONAL AND THAT IS SAID FIRST, not in a footnote. A regtest
    wallet holding 13,879 BTC is worth $1.16 billion at a mainnet price and
    nothing at all in fact -- the coins are on a private chain nobody else has.
    The arithmetic is still useful, for exactly one thing: rehearsing the sizing
    of a real book before there is one. A report that prints these figures
    without saying which they are is lying to its reader.

    THE DOLLAR IS CHECKED, NOT ASSUMED. No feed publishes a dollar; it publishes
    what the market pays in USDT or USDC and calls it USD. Both have broken their
    peg -- USDC near $0.88 in 2023, USDT near $0.95 in 2022 -- and when the
    yardstick moves every figure below moves with it. peg_findings() says where
    the two stand, and nothing here REFUSES on a broken peg: that would be a
    posture decision and the operator's (rule 16).
    """
    from services.coinpaprika import (  # noqa: PLC0415 -- checked: a price source imported at module scope makes --help reach for `requests`, the same reason atomic_swap_xrp.py defers its two.
        PaprikaError,
        fetch_quote,
    )

    console.say("EVERY FIGURE BELOW IS NOTIONAL. These are TEST-NETWORK coins priced at MAINNET "
                "rates: they are worth nothing, and the numbers are a rehearsal of sizing a real "
                "book rather than a statement about money you have.")

    prices, unpriced = {}, {}
    for asset in sorted(set(held) | set(PEG_ASSETS)):
        try:
            prices[asset] = Decimal(str(fetch_quote(asset).price_usd))
        except PaprikaError as error:
            unpriced[asset] = str(error)

    suspect, findings = peg_findings(prices)
    console.say(f"THE DOLLAR THIS USES, checked against {' and '.join(PEG_ASSETS)}:")
    for finding in findings:
        console.say(f"    {finding}")
    console.check("the dollar is a usable yardstick", "suspect" if suspect else "yes",
                  "both stablecoins priced and within tolerance", not suspect)

    values = [WalletValue(chain=chain, units=amount, price_usd=prices[chain])
              for chain, amount in sorted(held.items()) if chain in prices]
    for chain, reason in sorted(unpriced.items()):
        if chain in held:
            # Rule 14: a wallet left out of the table must say so IN the table's
            # place, or a reader counts the rows and concludes it has three chains.
            console.say(f"    {chain}: HELD BUT NOT PRICED, so it is absent from the moves below "
                        f"({reason})")
    if not values:
        console.say("    (none) -- no wallet could be both read and priced, so there is nothing to level")
        return

    aim = even_target(values) if even else Decimal(target)
    console.say(f"target {aim:.2f} USD per wallet"
                + ("  <- the AVERAGE of what is already held, reachable by moving value between them"
                   if even else "  <- a fixed figure; reaching it means acquiring, not just moving"))
    for move in moves_to(values, aim):
        console.say(f"    {move.chain:<4} holds {move.have_usd:>14,.2f} USD "
                    f"({move.price_usd} each) -> {move.direction} "
                    f"{abs(move.delta_usd):,.2f} USD = {abs(move.delta_units):,.8f} {move.chain}")
    total = sum((value.value_usd for value in values), Decimal(0))
    console.say(f"    total {total:,.2f} USD across {len(values)} wallet(s); leveling to {aim:.2f} each "
                f"needs {aim * len(values) - total:+,.2f} USD of net change")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--level", action="store_true",
                        help="also price every wallet in USD and say what it would take to bring each "
                             "to --target. Read-only: it prints moves, it does not make them.")
    parser.add_argument("--target", type=str, default=str(DEFAULT_TARGET_USD),
                        help=f"the USD figure each wallet should reach (default {DEFAULT_TARGET_USD}). "
                             f"Ignored without --level.")
    parser.add_argument("--even", action="store_true",
                        help="level to the AVERAGE of what the wallets already hold instead of --target. "
                             "That target is reachable by moving value between them and needs no new "
                             "funding; --target says what to acquire.")
    parser.add_argument("--chain", action="append", default=[],
                        choices=[chain.lower() for chain in CHAINS],
                        help="report only this chain. Repeatable. Default: all of them.")
    args = parser.parse_args()

    console = Console(total_steps=2 if args.level else 1)
    console.banner("CHAIN BALANCES -- read-only. No wallet is unlocked and nothing is signed.")
    wanted = [chain.upper() for chain in args.chain] or list(CHAINS)
    console.say(f"chains={', '.join(wanted)}  (each daemon's network is checked BEFORE its wallet "
                f"is asked anything; a mainnet answer is refused, not reported)")

    console.step(1, f"{len(wanted)} chain(s)")
    # build_adapters() opens no socket, and it constructs a chain ONLY when that
    # chain is configured -- which is what keeps an unset GRC_RPC_PORT from
    # defaulting to the mainnet port. See chains/registry.py's header for the
    # 2026-09-26 incident that made every entry conditional.
    adapters = build_adapters(Config.RPC)
    held: dict[str, Decimal] = {}
    for chain in wanted:
        spendable = report_chain(console, chain, adapters)
        if spendable is not None:
            held[chain] = spendable
    answered = len(held)
    if not answered:
        console.say("(none) -- not one of those chains answered. INCONCLUSIVE rather than a pass: "
                    "nothing here says your wallets are empty, only that none was read.")
        console.summary()
        return NOTHING_TO_LOOK_AT
    console.say(f"{answered} of {len(wanted)} chain(s) reported")
    if args.level:
        console.step(2, "what each wallet is worth, and what levels it")
        say_what_levels_them(console, held, args.target, even=args.even)
    return console.summary()


if __name__ == "__main__":
    raise SystemExit(main())
