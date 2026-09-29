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
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "swap_terminal"))

from chains.daemon_conf import CONF_FALLBACK_NETWORK, conf_fallback_settings
from chains.daemon_network import CHAIN_TEST_NETWORKS, chain_network
from chains.registry import build_adapters, why_unconfigured
from chains.wallet_lock import encryption_state
from config import Config
from regtest.daemons import CHAIN_DEFAULTS
from step_console import Console

# Every chain this can report, in the order it reports them. Derived from the
# allowlist rather than spelled again: a chain that has no test-network
# vocabulary has no business being polled by this, and a hand-written second
# list is rule 8's shape (it is exactly how VALID_HORIZONS drifted in the other
# repo whose rules this tree follows).
CHAINS = tuple(sorted(CHAIN_TEST_NETWORKS))

NOTHING_TO_LOOK_AT = 3

# What to CALL a wallet this suggests creating. The same name the HTLC harness
# uses, so an operator who follows this hint ends up with the wallet
# regtest_htlc_verify.py will then find already loaded rather than a second one
# beside it (rule 8 -- one name for one thing).
DEFAULT_WALLET_NAME = "regtest_htlc_harness"


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


# A FRESHLY STARTED DAEMON HAS NO WALLET LOADED, and since Bitcoin Core 0.21 it
# does not create one either. getbalance then answers rpc code -18 with a message
# naming loadwallet and createwallet, which is most of the answer and not the part
# that says WHICH wallet -- so this asks.
#
# Matched on the message rather than the code because RPCError here is the
# repository's own wrapper and the code is not exposed as an attribute. The
# message is the daemon's and is stable across both families; the match is
# reported as a hint, never branched on for a decision.
NO_WALLET_LOADED = "No wallet is loaded"


def which_wallets_are_on_disk(adapter, chain: str) -> str:
    """The wallets this daemon could load, and the command that loads one.

    listwalletdir IS A READ. loadwallet is not -- it changes what the daemon has
    open -- so this names the command and does not run it, the same line
    what_to_do_about_it() draws around starting a daemon. A read-only balance
    reader that quietly loads a wallet is no longer a read-only balance reader,
    and tests/test_chain_balances.py holds the method list that says so.

    ADDED 2026-09-29, one layer in from the down-daemon hint and for the identical
    reason: the operator started litecoind, re-ran this twice, and got a correct
    message that did not say what to do next.
    """
    cli = CHAIN_DEFAULTS.get(chain, {}).get("cli", "")
    datadir = CHAIN_DEFAULTS.get(chain, {}).get("datadir", "")
    network = CONF_FALLBACK_NETWORK.get(chain, "")
    prefix = (f"{cli} -datadir={Path(datadir).expanduser()} -{network} " if cli and network else "")
    try:
        listing = adapter.call("listwalletdir") or {}
        names = [entry.get("name", "") for entry in (listing.get("wallets") or [])]
    except Exception as error:  # noqa: BLE001 -- checked: listwalletdir is absent on a daemon built without wallet support and on older builds, and its absence costs only the names. The hint still names the two commands, so the reader is not left with nothing; the reason is printed.
        return (f"could not list this daemon's wallets ({type(error).__name__}: {error}), so which one "
                f"to load is unknown. {prefix}createwallet <a name> makes one.")
    if not names:
        # (none) is a RESULT. A daemon with an empty wallet directory needs
        # createwallet, and saying "load one of []" would be nonsense.
        return (f"this daemon has NO wallet on disk -- (none) in its wallet directory. "
                f"{prefix}createwallet {DEFAULT_WALLET_NAME} makes one, and it will be empty until "
                f"something mines or sends to it.")
    unnamed = [name or "(the unnamed default wallet)" for name in names]
    return (f"this daemon has {len(names)} wallet(s) on disk: {', '.join(unnamed)}. "
            f"{prefix}loadwallet {names[0]} loads the first.")


def report_chain(console: Console, chain: str, adapters: dict) -> bool:
    """One chain's holdings. True if the daemon answered and was safe to ask."""
    adapter = adapters.get(chain) or adapter_from_conf(console, chain)
    if adapter is None:
        routes = f"{chain}_RPC_* in the environment"
        if chain in CONF_FALLBACK_NETWORK:
            routes += ", or an rpcuser/rpcpassword/rpcport in this chain's own conf"
        console.check(f"{chain} configured", "no", routes, False)
        console.say(f"    {why_unconfigured(chain, Config.RPC)}")
        return False

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
        return False
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
        return False

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

    console.say(f"    spendable {spendable:.8f} {chain}")
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
    return console.check(f"{chain} balance", f"{spendable:.8f} spendable", "read", True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--chain", action="append", default=[],
                        choices=[chain.lower() for chain in CHAINS],
                        help="report only this chain. Repeatable. Default: all of them.")
    args = parser.parse_args()

    console = Console(total_steps=1)
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
    answered = 0
    for chain in wanted:
        answered += 1 if report_chain(console, chain, adapters) else 0
    if not answered:
        console.say("(none) -- not one of those chains answered. INCONCLUSIVE rather than a pass: "
                    "nothing here says your wallets are empty, only that none was read.")
        console.summary()
        return NOTHING_TO_LOOK_AT
    console.say(f"{answered} of {len(wanted)} chain(s) reported")
    return console.summary()


if __name__ == "__main__":
    raise SystemExit(main())
