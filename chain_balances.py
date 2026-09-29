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

from chains.daemon_network import CHAIN_TEST_NETWORKS, chain_network
from chains.registry import build_adapters, why_unconfigured
from chains.wallet_lock import encryption_state
from config import Config
from step_console import Console

# Every chain this can report, in the order it reports them. Derived from the
# allowlist rather than spelled again: a chain that has no test-network
# vocabulary has no business being polled by this, and a hand-written second
# list is rule 8's shape (it is exactly how VALID_HORIZONS drifted in the other
# repo whose rules this tree follows).
CHAINS = tuple(sorted(CHAIN_TEST_NETWORKS))

NOTHING_TO_LOOK_AT = 3


def report_chain(console: Console, chain: str, adapters: dict) -> bool:
    """One chain's holdings. True if the daemon answered and was safe to ask."""
    adapter = adapters.get(chain)
    if adapter is None:
        console.check(f"{chain} configured", "no", f"{chain}_RPC_* in the environment", False)
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
