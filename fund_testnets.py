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
from chains.base import RPCAdapter
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
    wipe_datadir,
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

# regtest halves the block subsidy every 150 blocks, which matters far more than
# it sounds. MEASURED on the operator's machine 2026-09-25, on chains already
# deep from earlier HTLC harness runs:
#
#     BTC at height 390   390//150 = 2 halvings    50/4      = 12.5 per block
#     LTC at height 2504  2504//150 = 16 halvings  50/65536  = 0.00076294
#
# So mining 101 blocks on an old regtest chain yields almost nothing, and the
# reported balance looks like a broken daemon rather than an exhausted subsidy.
# A wiped datadir restarts at height 0 where the subsidy is the full 50.
REGTEST_HALVING_INTERVAL = 150
REGTEST_INITIAL_SUBSIDY = 50.0
# Below this, say so loudly and recommend --wipe rather than leaving the
# operator to wonder why 101 blocks produced a rounding error.
NEGLIGIBLE_SUBSIDY = 1.0


def block_subsidy(height: int) -> float:
    """The regtest coinbase reward at a given height, in whole coins.

    Pure, so the halving arithmetic can be tested without a daemon -- and it is
    the arithmetic that explains an otherwise baffling balance.
    """
    return REGTEST_INITIAL_SUBSIDY / (2 ** (int(height) // REGTEST_HALVING_INTERVAL))


def regtest_yield(height_before: int, blocks: int) -> tuple[float, float]:
    """(spendable, immature) that mining `blocks` from `height_before` produces.

    SUMS the per-block subsidy rather than multiplying by the one at the start,
    and that is not pedantry. The first version multiplied, which is correct
    only while every matured block shares one subsidy -- true for the operator's
    200-block run purely because blocks 1-100 all predate the halving at height
    150, so it printed the right answer for the wrong reason.

    Checked against that run: summing gives 5000.0 spendable and 3725.0
    immature, and the daemon reported exactly those two figures. Checked
    against where multiplying would have LIED: 400 blocks from a fresh chain
    matures 11212.5, where multiplying claims 15000.0 -- an overstatement of
    3787.5 told to an operator deciding how many blocks to mine.

    A coinbase at height h is spendable once the tip reaches h + 100, so after
    mining to `tip` the newly mined heights up to tip-100 are mature.
    """
    first = height_before + 1
    tip = height_before + blocks
    mature_through = tip - COINBASE_MATURITY_BLOCKS + 1
    spendable = sum(block_subsidy(h) for h in range(first, min(mature_through, tip) + 1))
    immature = sum(block_subsidy(h) for h in range(max(first, mature_through + 1), tip + 1))
    return spendable, immature


def describe_regtest_yield(height_before: int, blocks: int) -> str:
    """What mining `blocks` from `height_before` will be worth, and why.

    Rule 14: state what the number means, next to the number. A bare
    "balance 0.00076293" after mining 101 blocks reads as a failure; the same
    figure beside "16 halvings" reads as a chain that needs wiping.
    """
    subsidy = block_subsidy(height_before + 1)
    halvings = (height_before + 1) // REGTEST_HALVING_INTERVAL
    spendable, immature = regtest_yield(height_before, blocks)
    line = (
        f"subsidy {subsidy:.8f}/block after {halvings} halving(s) at height {height_before + 1}; "
        f"{blocks} blocks yields {spendable:.8f} spendable + {immature:.8f} immature"
    )
    if subsidy < NEGLIGIBLE_SUBSIDY:
        line += (
            f"  <- NEGLIGIBLE. This chain is {height_before} blocks deep and the subsidy has halved "
            f"{halvings} times. Rerun with --wipe to restart at height 0, where it is "
            f"{REGTEST_INITIAL_SUBSIDY:.0f}/block."
        )
    return line


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


def fund_regtest_chain(console: Console, asset: str, blocks: int, wipe: bool = False) -> dict:
    """Start a regtest daemon, make a wallet, mine to it. Returns a summary.

    assert_regtest() is called before anything is mined, and that ordering is
    the safety property: it asks the daemon which chain it is on and raises if
    the answer is not "regtest". A mainnet daemon reached by a misconfigured
    port would be refused here rather than asked to generate blocks.
    """
    config = resolve_chain_config(asset)
    console.say(f"{asset}: datadir {config.datadir}")
    if wipe:
        # Wipes BEFORE the daemon starts: wipe_datadir() removes the chain
        # directory, and doing that under a running daemon leaves it writing
        # into deleted files.
        wipe_datadir(console, config)
    we_started = start_daemon(console, config)
    wait_for_rpc(console, config)

    info = assert_regtest(console, config)
    console.say(f"{asset}: chain={info.get('chain')} blocks={info.get('blocks')}")

    ensure_wallet(console, config, WALLET_NAME)
    node = adapter_for(config, wallet=WALLET_NAME)
    height_before = int(node.call("getblockcount"))
    address = node.call("getnewaddress", "testnet-coins")

    # ANNOUNCED BEFORE MINING, not after (rule 14). This is the line that
    # explains a balance the operator would otherwise read as a failure.
    console.say(f"{asset}: {describe_regtest_yield(height_before, blocks)}")
    console.say(f"{asset}: mining {blocks} blocks to {address}")
    node.call("generatetoaddress", blocks, address)

    balance = float(node.call("getbalance"))
    height = int(node.call("getblockcount"))
    # Both halves of the balance. `getbalance` alone reports only what is
    # SPENDABLE, so 100 freshly mined rewards are invisible in it -- and their
    # absence looks like the mining did not work.
    immature = 0.0
    try:
        balances = node.call("getbalances") or {}
        immature = float((balances.get("mine") or {}).get("immature", 0.0))
    except Exception as error:  # noqa: BLE001 -- checked: getbalances is absent on older daemons, and its absence costs only this one reporting line. The exception is NAMED in the output below rather than swallowed, and `balance` above came from a separate call that already succeeded.
        console.say(f"{asset}: getbalances unavailable ({error}); immature total not reported")
    console.say(f"{asset}: spendable {balance}, immature {immature}, height {height}")
    return {
        "asset": asset,
        "balance": balance,
        "immature": immature,
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

    # SEVERAL CANDIDATE KEYS, and the run says which one it found.
    #
    # The first version read payload["balance"] and printed "balance None" on
    # the operator's real run -- a bare None that says nothing about whether
    # the account was funded, which key is right, or where to look. The faucet's
    # response shape is not documented here and could not be checked from the
    # environment this was written in, so the code searches rather than assumes
    # and REPORTS the absence of every candidate instead of one None.
    balance, balance_key = None, None
    for key in ("balance", "amount", "xrp", "drops"):
        for holder, label in ((payload, ""), (account, "account.")):
            if holder.get(key) is not None:
                balance, balance_key = holder[key], f"{label}{key}"
                break
        if balance is not None:
            break

    console.say(f"XRP: address {address}")
    if balance is None:
        console.say("XRP: balance NOT REPORTED under any of balance/amount/xrp/drops.")
        console.say(f"     top-level keys: {sorted(payload)}")
        console.say(f"     account keys:   {sorted(account)}")
        console.say("     the account exists and is funded (the faucet only creates funded")
        console.say("     accounts); this is a reporting gap, not a funding failure. Confirm with:")
        console.say(f"       python3 xrp_chain_check.py --account {address}")
    else:
        console.say(f"XRP: balance {balance} XRP via `{balance_key}` (testnet, worth nothing)")
    console.say(f"XRP: secret written to {destination} (mode 0600, outside any git repo)")
    console.say("XRP: the secret was NOT printed and is not needed for deposit testing --")
    console.say("     only for paying OUT, which chains/xrp.py refuses to do anyway")
    return {"asset": "XRP", "address": address, "balance": balance,
            "balance_key": balance_key, "secret_file": str(destination)}


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


GRIDCOIN_CONF_GLOBS = (
    "~/.GridcoinResearch*/gridcoinresearch.conf",
    "~/.GridcoinResearch/testnet/gridcoinresearch.conf",
    "~/Documents/Python/grctest/*/gridcoinresearch.conf",
)

# Gridcoin mainnet RPC. Named so the report can say MAINNET rather than
# mislabeling real money as test coins -- but the network is decided by the
# DAEMON's own `testnet` field, never by this number.
GRIDCOIN_MAINNET_RPC_PORT = 15715


def gridcoin_conf_candidates() -> list[Path]:
    """Every gridcoinresearch.conf on disk that declares an rpcport.

    Scanned from disk rather than read from the environment, because the
    operator has FIVE of these with FOUR different ports (measured 2026-09-25:
    15715 mainnet, 25715 twice, 9876, 25779) and no single env var names the
    one that is running. Backups are included deliberately -- a conf in a
    directory called `testnet.backup...` may still be the live one, and the
    only way to find out is to ask whether anything answers on its port.
    """
    seen: dict[Path, None] = {}
    for pattern in GRIDCOIN_CONF_GLOBS:
        expanded = Path(pattern).expanduser()
        for path in sorted(expanded.parent.parent.glob("/".join(expanded.parts[-2:]))
                           if "*" in expanded.parent.name else [expanded]):
            if path.is_file():
                seen.setdefault(path, None)
    return list(seen)


def read_gridcoin_conf(path: Path) -> dict:
    """rpcport/rpcuser/rpcpassword from one conf. The password is never returned.

    It is read because an RPC call needs it and immediately handed to the
    adapter; it is not placed in the returned dict, so no caller can print it
    by accident and no summary line can carry it.
    """
    values = {}
    for line in path.read_text(errors="replace").splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip()
    return values


def check_gridcoin_testnet(console: Console, allow_mainnet: bool = False) -> dict:
    """READ-ONLY. Report the Gridcoin balance of whichever daemon answers.

    Mints nothing: the operator already holds testnet Gridcoin and asked only
    to SEE it. So this makes exactly two RPC calls, both reads.

    THE NETWORK IS TAKEN FROM THE DAEMON, not from the port. getinfo reports
    `testnet`, and that is what decides the label -- because a conf in a
    directory named `testnet` is not a testnet (measured 2026-09-25: several of
    the operator's testnet-named confs have no testnet=1 at all, since Gridcoin
    also takes -testnet on the command line). Reporting a mainnet balance as
    test coins would be the worst possible outcome of a script called
    fund_testnets.
    """
    candidates = gridcoin_conf_candidates()
    console.say(f"GRC: {len(candidates)} gridcoinresearch.conf file(s) on disk")
    if not candidates:
        raise RegtestSetupError("no gridcoinresearch.conf found; nothing to ask")

    tried: list[str] = []
    skipped_mainnet = 0
    for path in candidates:
        conf = read_gridcoin_conf(path)
        port = conf.get("rpcport")
        user = conf.get("rpcuser")
        if not port or not user:
            continue

        # THE MAINNET PORT IS NOT ASKED AT ALL, and this is a correction with a
        # date on it. On 2026-09-26 the first version of this function returned
        # the first conf that ANSWERED -- which was the mainnet daemon on 15715,
        # the only one running. It labelled the result correctly and refused to
        # count it, but it had already printed a real 157,797 GRC balance into a
        # terminal whose output goes into a chat transcript.
        #
        # Labelling a mainnet hit is the wrong altitude of fix. A script called
        # fund_testnets should not OPEN A SOCKET to the mainnet wallet, so the
        # port is skipped before any call is made, and reaching it requires
        # --grc-mainnet said out loud. The label stays for the case where a
        # testnet-configured port turns out to be a mainnet daemon, which is a
        # thing only the daemon can tell us.
        if int(port) == GRIDCOIN_MAINNET_RPC_PORT and not allow_mainnet:
            skipped_mainnet += 1
            continue
        adapter = RPCAdapter(user=user, password=conf.get("rpcpassword", ""),
                             host="127.0.0.1", port=int(port), timeout=8.0)
        try:
            info = adapter.call("getinfo") or {}
        except Exception as error:  # noqa: BLE001 -- checked: a conf whose daemon is not running is the COMMON case, not an error, and the loop must continue to the next candidate. Every failure is collected into `tried` and printed below if none answers, so nothing is hidden.
            tried.append(f"port {port}: {str(error).splitlines()[0][:70]}")
            continue

        is_testnet = bool(info.get("testnet"))
        balance = float(info.get("balance", 0.0))
        label = "TESTNET" if is_testnet else "*** MAINNET -- REAL MONEY ***"
        console.say(f"GRC: answered on port {port} from {path}")
        console.say(f"GRC: network {label} (from the daemon's getinfo.testnet, not the port)")
        console.say(f"GRC: balance {balance} GRC, blocks {info.get('blocks')}, version {info.get('version')}")
        if not is_testnet:
            console.say("GRC: NOT counted as test coins. This is the mainnet wallet; nothing was minted")
            console.say("     and nothing was sent, but a script called fund_testnets should not be")
            console.say("     reporting a real balance as though it were play money.")
        return {"asset": "GRC", "balance": balance, "address": f"{'testnet' if is_testnet else 'MAINNET'} "
                f"port {port}", "testnet": is_testnet, "mainnet_warning": not is_testnet}

    detail = "; ".join(tried) if tried else "no conf declared both rpcport and rpcuser"
    skipped = (
        f" Skipped {skipped_mainnet} conf(s) on the mainnet port {GRIDCOIN_MAINNET_RPC_PORT} without "
        f"connecting -- pass --grc-mainnet if you really want the real wallet's balance printed."
        if skipped_mainnet else ""
    )
    raise RegtestSetupError(
        f"no Gridcoin TESTNET daemon answered. Tried: {detail}.{skipped} To start one:\n"
        f"        gridcoinresearchd -testnet -datadir=<your testnet datadir> -daemon\n"
        f"      The coins are already there -- this only means nothing is serving RPC for them."
    )


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
         lambda console: fund_regtest_chain(console, "BTC", args.blocks, args.wipe), args.btc),
        ("LTC", f"regtest: start daemon, make a wallet, mine {args.blocks} blocks",
         lambda console: fund_regtest_chain(console, "LTC", args.blocks, args.wipe), args.ltc),
        ("XRP", "testnet faucet: create and fund an account", fund_xrp_testnet, args.xrp),
        ("SOL", "devnet: confirm the balance the key rotation left (read-only)", check_solana_devnet, args.sol),
        ("GRC", "testnet: report the balance (READ-ONLY; mints nothing)",
         lambda console: check_gridcoin_testnet(console, args.grc_mainnet), args.grc),
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
        extra = f" (+{row['immature']} immature)" if row.get("immature") else ""
        print(f"  {row['asset']:<5} balance {row.get('balance')}{extra}  {row.get('address', '')}", flush=True)
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
    parser.add_argument("--grc", action="store_true",
                        help="report the Gridcoin balance, READ-ONLY -- the operator already holds "
                             "testnet GRC, so this mints nothing and only looks")
    parser.add_argument("--grc-mainnet", action="store_true",
                        help="also ask the MAINNET Gridcoin daemon on port 15715. Off by default: it "
                             "prints a real balance, and this script's output tends to get pasted")
    parser.add_argument("--xmr", action="store_true", help="explain why Monero is not scripted here")
    parser.add_argument("--all", action="store_true", help="every chain above")
    parser.add_argument("--wipe", action="store_true",
                        help="DELETE the regtest chain first and restart at height 0, where the subsidy "
                             "is 50/block. regtest coins are worth nothing, but any wallet in that "
                             "datadir goes with it")
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
