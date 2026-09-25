#!/usr/bin/env python3
"""Get test coins for each chain. Test networks only, by construction.

Role: file (operator entry point, at the project root per CLAUDE.md rule 10)
Reads: BTC/LTC regtest daemons, the XRP testnet faucet, a Solana devnet RPC
Writes: BTC/LTC regtest datadirs and wallets; an XRP testnet secret to a 0600
        file OUTSIDE any git repository. Nothing in this repository, and
        nothing in swap_terminal.db.
Can move funds: it MINTS test coins, which are worth nothing by construction.
        It cannot touch mainnet: BTC and LTC run with -regtest (a private chain
        whose coins do not exist anywhere else), XRP uses the testnet faucet
        host, and the Solana step is READ-ONLY.
Mainnet-safe: it never contacts a mainnet endpoint. There is no flag that
        would; the regtest argument is not optional and the hosts are pinned.

WHY EACH CHAIN GETS COINS A DIFFERENT WAY

    BTC, LTC   regtest. `generatetoaddress` mints immediately, unlimited, with
               no faucet and no third party. testnet3 would mean a faucet AND
               a chain download; regtest is instant and the HTLC harness
               already proved these daemons work this way (OK=74 on
               2026-09-25).
    XRP        the testnet faucet CREATES a funded account -- there is no
               regtest equivalent to mint your own, and no sync either, so this
               is one HTTP call.
    SOL        nothing to do. 28.778699200 devnet SOL already sits at
               J5wn3xEMDsr9r8qtF6YTWJodmgW5kG3ZThqDb8Xc37JM from the key
               rotation on 2026-09-25, so this step only CONFIRMS it rather
               than asking for more.
    XMR        NOT HANDLED, and see the --xmr note. A stagenet wallet cannot
               see a deposit until it has synced, and that was MEASURED at
               33-54 hours on this hardware. It needs a decision, not a script.
    GRC        excluded at the operator's request: they already hold testnet
               Gridcoin.

SECRETS ARE NEVER PRINTED. The XRP faucet returns a funded account AND its
secret. That secret goes straight to a 0600 file outside any git repository and
only the address and balance reach the screen. This is not caution in the
abstract: on 2026-09-25 a monero-wallet-cli run printed a 25-word seed into a
terminal whose whole output was then pasted into a chat, and that wallet had to
be treated as public from then on.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "swap_terminal"))

import requests
from regtest.console import Console
from regtest.daemons import (
    RegtestSetupError,
    adapter_for,
    assert_regtest,
    ensure_wallet,
    read_pid,
    resolve_chain_config,
    start_daemon,
    wait_for_rpc,
)

# Pinned test hosts. Neither has a mainnet sibling reachable by changing a flag
# here -- to point this at real money you would have to edit the source, which
# is the point.
XRP_FAUCET = "https://faucet.altnet.rippletest.net/accounts"
SOLANA_DEVNET = "https://api.devnet.solana.com"

# The devnet account the key rotation moved everything to. Read-only here.
SOLANA_DEVNET_ACCOUNT = "J5wn3xEMDsr9r8qtF6YTWJodmgW5kG3ZThqDb8Xc37JM"

# A coinbase output needs 100 confirmations before it can be spent, so 101
# blocks is the smallest number that yields one spendable reward. Mining fewer
# gives a wallet whose balance reads zero while the blocks exist, which looks
# like a broken daemon rather than immature coins.
COINBASE_MATURITY_BLOCKS = 101
WALLET_NAME = "swap_terminal_testnet"


def secret_destination(chain: str) -> Path:
    """A 0600 path for a secret, refusing anywhere git could ever see it.

    Same guard as rotate_solana_key.mjs, and for the same reason: a key written
    inside a working tree is one `git add -A` from being published, which is
    exactly how wgrc.json ended up in this repository's history.
    """
    directory = Path.home() / ".config" / "swap_terminal" / "keys"
    probe = directory
    while probe != probe.parent:
        if (probe / ".git").exists():
            raise RegtestSetupError(
                f"{directory} is inside a git repository ({probe}). A secret must not be written there."
            )
        probe = probe.parent
    directory.mkdir(parents=True, exist_ok=True)
    directory.chmod(0o700)
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    # NEVER RETURN A PATH THAT ALREADY EXISTS. The timestamp is
    # second-resolution, so two calls inside one second would collide -- and
    # the write below uses write_text(), which would silently overwrite the
    # first key with the second. Losing a funded account is cheap on testnet
    # and the habit is not; a suffix is added rather than clobbering, and if
    # even that runs out the function refuses instead of picking one.
    candidate = directory / f"{chain}-testnet-{stamp}.json"
    for suffix in range(1, 100):
        if not candidate.exists():
            return candidate
        candidate = directory / f"{chain}-testnet-{stamp}-{suffix}.json"
    raise RegtestSetupError(
        f"100 secret files already exist for {chain} at {stamp} in {directory}. NOTHING was written -- "
        f"refusing rather than overwriting one of them, since any could hold a funded account."
    )


def fund_regtest_chain(console: Console, asset: str, blocks: int) -> dict:
    """Start a regtest daemon, make a wallet, mine to it. Returns a summary.

    assert_regtest() is called before anything is mined, and that ordering is
    the safety property: it asks the daemon which chain it is on and raises if
    the answer is not "regtest". A mainnet daemon reached by a misconfigured
    port would be refused here rather than asked to generate blocks.
    """
    config = resolve_chain_config(asset)
    console.say(f"{asset}: datadir {config.datadir}")
    we_started = start_daemon(console, config)
    wait_for_rpc(console, config)

    info = assert_regtest(console, config)
    console.say(f"{asset}: chain={info.get('chain')} blocks={info.get('blocks')}")

    ensure_wallet(console, config, WALLET_NAME)
    node = adapter_for(config, wallet=WALLET_NAME)
    address = node.call("getnewaddress", "testnet-coins")
    console.say(f"{asset}: mining {blocks} blocks to {address}")
    node.call("generatetoaddress", blocks, address)

    balance = float(node.call("getbalance"))
    height = int(node.call("getblockcount"))
    console.say(f"{asset}: balance {balance} (spendable), height {height}")
    return {
        "asset": asset,
        "balance": balance,
        "height": height,
        "address": address,
        "datadir": str(config.datadir),
        "pid": read_pid(config),
        "we_started": we_started,
        "wallet": WALLET_NAME,
    }


def fund_xrp_testnet(console: Console) -> dict:
    """Ask the XRP testnet faucet for a funded account. Writes the secret to 0600.

    The faucet both CREATES and FUNDS the account, which is why there is no
    mining step and no address to supply: an XRPL account does not exist until
    something pays its reserve, so "give me an address" and "fund it" are one
    operation on this ledger.
    """
    console.say(f"XRP: POST {XRP_FAUCET}")
    response = requests.post(XRP_FAUCET, timeout=45)
    response.raise_for_status()
    payload = response.json()

    account = payload.get("account") or {}
    address = account.get("address") or account.get("classicAddress")
    if not address:
        raise RegtestSetupError(
            f"the faucet returned no address. Keys present: {sorted(payload)}. NOTHING was written."
        )

    destination = secret_destination("xrp")
    destination.write_text(json.dumps(payload, indent=2))
    destination.chmod(0o600)

    balance = payload.get("balance")
    console.say(f"XRP: address {address}")
    console.say(f"XRP: balance {balance} XRP (testnet, worth nothing)")
    console.say(f"XRP: secret written to {destination} (mode 0600, outside any git repo)")
    console.say("XRP: the secret was NOT printed and is not needed for deposit testing --")
    console.say("     only for paying OUT, which chains/xrp.py refuses to do anyway")
    return {"asset": "XRP", "address": address, "balance": balance, "secret_file": str(destination)}


def check_solana_devnet(console: Console) -> dict:
    """READ-ONLY. Confirms the devnet balance the key rotation left behind."""
    console.say(f"SOL: getBalance {SOLANA_DEVNET_ACCOUNT} on devnet (read-only)")
    response = requests.post(
        SOLANA_DEVNET,
        headers={"Content-Type": "application/json"},
        data=json.dumps({
            "jsonrpc": "2.0", "id": 1, "method": "getBalance",
            "params": [SOLANA_DEVNET_ACCOUNT, {"commitment": "confirmed"}],
        }),
        timeout=30,
    )
    response.raise_for_status()
    lamports = ((response.json().get("result") or {}).get("value"))
    if lamports is None:
        raise RegtestSetupError(f"devnet getBalance returned no value: {response.text[:200]}")
    console.say(f"SOL: {lamports / 1e9:.9f} SOL ({lamports} lamports)")
    console.say("SOL: no airdrop requested -- this is already far more than any test needs")
    return {"asset": "SOL", "address": SOLANA_DEVNET_ACCOUNT, "balance": lamports / 1e9}


def explain_monero() -> None:
    """Monero is not scriptable here, and saying so beats a script that pretends."""
    print("""
[XMR] NOT DONE, and deliberately not attempted by this script.

  A stagenet wallet cannot see a deposit until it has synced, and the sync was
  MEASURED on this hardware at 33-54 hours (2,215,603 blocks, 11-19 blocks/s,
  and that rate was a floor because early blocks are nearly empty).

  Three ways forward, and the choice is yours rather than a default:

    1. Let monerod --stagenet sync in the background for a day or two, then
       faucet into it. Costs wall clock, nothing else.
    2. Point monero-wallet-rpc at a REMOTE stagenet node with --daemon-address,
       which skips the sync entirely. Needs a current public stagenet host, and
       a dead one would present as a wallet bug -- so find one and hand it over
       rather than taking a hostname from me.
    3. Skip it. chains/monero.py's field names are already confirmed against
       the published spec, and the two things the docs do not settle are both
       guarded at runtime to refuse rather than mis-credit.

  There is also a wallet already on disk whose seed was printed into a chat
  transcript on 2026-09-25. Do not reuse it; generate a fresh one when you pick
  an option above.
""")


def selected_chains(args) -> list[tuple[str, str, object]]:
    """The chains to run, in order, as (asset, title, runner).

    A table rather than a chain of `if wanted[...]` blocks. main() was over the
    complexity ceiling three times in this session writing it the other way,
    and rule 12 is explicit that the fix is to extract the decision rather than
    raise the ceiling -- here the decision is "which chains, in what order",
    which is data.
    """
    everything = [
        ("BTC", f"regtest: start daemon, make a wallet, mine {args.blocks} blocks",
         lambda console: fund_regtest_chain(console, "BTC", args.blocks), args.btc),
        ("LTC", f"regtest: start daemon, make a wallet, mine {args.blocks} blocks",
         lambda console: fund_regtest_chain(console, "LTC", args.blocks), args.ltc),
        ("XRP", "testnet faucet: create and fund an account", fund_xrp_testnet, args.xrp),
        ("SOL", "devnet: confirm the balance the key rotation left (read-only)", check_solana_devnet, args.sol),
        ("XMR", "not scripted; explaining why", None, args.xmr),
    ]
    return [(asset, title, run) for asset, title, run, on in everything if on or args.all]


def report(console: Console, results: list[dict], problems: list[str]) -> None:
    """The summary, and the reaper line for anything this run left running.

    Rule 13: a spawn names its reaper. These daemons outlive the script on
    purpose -- the point is to have a funded chain to test against -- so the
    stop command is printed rather than left for the operator to reconstruct,
    and it PROVES the process is gone rather than trusting the kill's exit code.
    """
    console.banner("summary")
    if not results:
        print("  (none) -- no chain produced coins on this run", flush=True)
    for row in results:
        print(f"  {row['asset']:<5} balance {row.get('balance')}  {row.get('address', '')}", flush=True)
    for row in results:
        if row.get("we_started"):
            pid = row.get("pid")
            print(f"  {row['asset']:<5} daemon pid {pid} was STARTED BY THIS RUN and is still running.", flush=True)
            print(f"        REAPER: kill {pid} && sleep 3 && "
                  f"(kill -0 {pid} 2>/dev/null && echo STILL ALIVE || echo confirmed gone)", flush=True)
    if problems:
        print(flush=True)
        print(f"  {len(problems)} chain(s) FAILED:", flush=True)
        for problem in problems:
            print(f"    - {problem}", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="Get test coins. Test networks only.")
    parser.add_argument("--btc", action="store_true", help="mine regtest BTC")
    parser.add_argument("--ltc", action="store_true", help="mine regtest LTC")
    parser.add_argument("--xrp", action="store_true", help="ask the XRP testnet faucet")
    parser.add_argument("--sol", action="store_true", help="confirm the existing devnet balance (read-only)")
    parser.add_argument("--xmr", action="store_true", help="explain why Monero is not scripted here")
    parser.add_argument("--all", action="store_true", help="every chain above")
    parser.add_argument("--blocks", type=int, default=COINBASE_MATURITY_BLOCKS,
                        help=f"regtest blocks to mine (default {COINBASE_MATURITY_BLOCKS}; "
                             f"fewer than 101 leaves the coinbase immature and the balance zero)")
    args = parser.parse_args()

    chains = selected_chains(args)
    if not chains:
        parser.print_help()
        print("\nNothing selected, so nothing was done. Pick a chain, or --all.")
        return 2

    console = Console(total_steps=len(chains))
    console.banner("test coins -- regtest and public testnets only, never mainnet")
    print("  GRC is excluded on purpose: the operator already holds testnet Gridcoin.", flush=True)

    results: list[dict] = []
    problems: list[str] = []
    for number, (asset, title, run) in enumerate(chains, start=1):
        console.step(number, asset, title)
        if run is None:
            explain_monero()
            continue
        try:
            results.append(run(console))
        except Exception as error:  # noqa: BLE001 -- checked: this is an operator tool whose job is to report a per-chain outcome. Every failure is named, printed and counted into the exit code, so none is swallowed and one chain failing does not abandon the others.
            problems.append(f"{asset}: {error}")
            console.say(f"{asset}: FAILED -- {error}")

    report(console, results, problems)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
