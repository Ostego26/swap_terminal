#!/usr/bin/env python3
"""Make sure the desk's wallet exists on each TESTNET daemon, and say how to fund it.

Role: file (operator entry point, at the project root per CLAUDE.md rule 10)
Reads: Config.RPC, each chain's own conf as a fallback, and the BTC/LTC daemons
Writes: a wallet on each daemon, via createwallet. Nothing in this repository and
        nothing in swap_terminal.db.
Can move funds: NO, and it has no send path at all. `sendtoaddress`,
        `sendrawtransaction`, `signrawtransactionwithwallet`, `dumpprivkey` and
        `dumpwallet` do not appear in this file, and
        tests/test_testnet_wallets.py asserts that by reading its source.
Mainnet-safe: it refuses any daemon that does not name a network on
        chains/daemon_network.CHAIN_TEST_NETWORKS' allowlist for that chain, and
        refuses one that names no network at all. There is no flag to override it.
Live-safe: yes. createwallet does not stop the daemon, restart it, rescan, or
        touch an existing wallet -- and it does NOT need a synced chain, which is
        the whole reason this can be run during a 33-54 hour initial sync.

WHY THIS EXISTS, AND WHY IT IS NOT IN fund_testnets.py.

On 2026-10-10 the operator moved BTC and LTC off regtest onto full testnet
("our grc, ltc, and btc daemons should have peers and not be regtest anymore and
just full testnet now"). fund_testnets.py cannot help there and says so by
construction: its BTC and LTC path is `generatetoaddress` behind an
`assert_regtest()` that raises on any other chain, because regtest mints
immediately and testnet has no mining you can do. Its own docstring carried the
measurement that this change was waiting on --

    "... see a deposit until it has synced, and that was MEASURED at
    33-54 hours on this hardware. It needs a decision, not a script."

-- as an ORPHANED FRAGMENT, spliced into the Solana entry by an edit, with a note
saying which chain it measured could not be recovered from the text. IT WAS BTC
AND LTC, and the decision it asked for has been made: the operator said "it's
okay if it takes 33-54 hours at this point to do a blockchain sync, bro". That
attribution is now written at the fragment's own site rather than here.

So this is the other half, and it is a DIFFERENT JOB rather than a mode of the
same one:

    fund_testnets.py   mints. regtest only. Refuses testnet, on purpose.
    this file          prepares a wallet and an address, on testnet only, and
                       CANNOT mint -- on testnet there is nothing to mine.

WHAT IT CANNOT DO, said rather than left to be discovered (rule 14). It does not
fetch the coins. What this does is produce the address, prove it is the right
SHAPE for the network the daemon is actually on, and say whether a payment would
even be seen yet.

AND THE FIRST VERSION OF THAT PARAGRAPH WAS WRONG, CORRECTED 2026-10-10 THE SAME
DAY. It said "A tBTC or tLTC faucet is a page with a captcha or a sign-in, and
nothing in this tree can fill one in". The captcha half is false:
cypherfaucet.com serves a KEYLESS, CAPTCHA-FREE JSON API over both legs --
`POST /api/v1/claim` with `{"network":"btc-testnet","address":"tb1q..."}`, read
verbatim from its own README -- which is the same shape fund_testnets.py already
uses for the XRP testnet faucet. So "nothing in this tree can" was a statement
about this tree, dressed up as a statement about faucets.

WHY A CLIENT FOR IT IS NOT IN THIS FILE, which is a reason rather than the same
claim again. Two things are unestablished and only the operator can settle them:
that API is **off by default** in the faucet's own config (`'api_enabled' =>
true`), so whether the live site serves it is not known; and this container cannot
reach any faucet host to find out -- measured 2026-10-10, `curl` through the
session proxy answers `CONNECT tunnel failed, response 403` for cypherfaucet.com,
coinfaucet.eu, faucet.testnet4.dev, tltc.bitaps.com, litecointf.salmen.website and
mempool.space alike. That is a policy denial at the gateway, not six dead domains.
Writing a client against a README I cannot execute is rule 16's "a fix you cannot
test is a proposal"; `GET /api/v1/info` from the operator's own shell settles it in
one request, and the client can be written against a real response after that.

THE WALLET NAME IS NOT THIS FILE'S TO CHOOSE, and that is the defect it was
written to avoid. There are four wallet names in this tree -- `desk_hot` (what
the operator's environment sets), `regtest_htlc_harness`
(regtest_htlc_verify.py:230 and chains/wallet_hint.py:41), `swap_terminal_testnet`
(fund_testnets.py:99) and `LegacyWallet` (modules/atomic_btc_client.py:218's
fallback). Creating one this tool picked would make a fifth, and worse, would
make a wallet the application does not read: config.py reads
`BTC_RPC_WALLET` / `LTC_RPC_WALLET`, defaulting to `""`. So the name comes from
Config.RPC, and an unset variable REFUSES with the variable named -- a funded
wallet the payout path cannot see is worse than no wallet, because it looks done.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "swap_terminal"))

# NO E402 SUPPRESSION ON ANY OF THESE, and that is checked rather than copied. Every
# other root tool in this tree carries one on the imports after its sys.path.insert,
# so adding one was the first draft here too -- and ruff's RUF100 reported all six as UNUSED,
# because E402 is not enabled for this path. tests/test_step_console.py records the
# same discovery from the same mistake. A suppression for a finding that does not fire
# is rule 19's shape exactly: a claim nobody checked.
from chains.daemon_network import (
    NETWORK_IS_TEST,
    NETWORK_NOT_ESTABLISHED,
    PREFIX_KNOWN,
    SYNC_SYNCED,
    bech32_prefix_status,
    chain_network,
    sync_verdict,
    test_network_verdict,
)
from chains.registry import build_adapters, why_unconfigured
from config import Config
from regtest.console import FAIL, OK, Console
from regtest.daemons import RegtestSetupError, WalletSite, ensure_wallet_on

#: The chains this file can prepare. BTC and LTC only, and the omissions are
#: reasons rather than an oversight:
#:
#:   GRC   excluded at the operator's request since fund_testnets.py was written:
#:         they already hold testnet Gridcoin, and Gridcoin's wallet is the
#:         operator's own staking wallet rather than one a tool should create.
#:   XRP   has no wallet to create. An account exists when it is funded, and
#:         fund_testnets.py --xrp already asks the testnet faucet over HTTP.
#:   SOL   same: a keypair is a file, it already exists, and the devnet balance is
#:         confirmed read-only by fund_testnets.py --sol.
CHAINS = ("BTC", "LTC")

#: WHY NONE OF THESE WAS TESTED, which is a harder fact than "I did not try".
#:
#: MEASURED 2026-10-10: this container cannot reach a single faucet host. `curl`
#: through the session proxy answers `CONNECT tunnel failed, response 403` for every
#: one of cypherfaucet.com, coinfaucet.eu, faucet.testnet4.dev, tltc.bitaps.com,
#: litecointf.salmen.website and mempool.space. A 403 at the CONNECT is a policy
#: denial by the environment's network allowlist -- NOT six dead domains, which is
#: what the first read of it looked like (WebFetch reported ENOTFOUND, because its
#: own resolver is a different path; curl through the proxy resolves at the gateway
#: and gives the real answer). So the status below is "unreachable from the machine
#: that wrote this file", and the operator's machine has no such restriction.
#:
#: Rule 17 is the whole reason this is a field rather than a flat list of URLs. A
#: list of confident-looking URLs that are half dead wastes exactly the time it was
#: meant to save, and the searches said so themselves: one result reported "most of
#: them either empty or completely offline", and a repository issue 14 days old
#: reported tltc.bitaps.com stuck at block 4,887,883 while calling CypherFaucet "the
#: only Litecoin testnet faucet I've found that still works".
#:
#: Same shape as chains/daemon_capabilities.py's MEASURED / RELEASE_HISTORY /
#: UPSTREAM_SOURCE: the claim and its evidence travel together, so a reader can tell
#: which they are holding.
SEARCHED_NOT_TESTED = "listed 2026-10-10; UNREACHABLE from this container (403 at the proxy), so untested"


@dataclass(frozen=True)
class Faucet:
    """One faucet, with what it claims to give and whether a link can carry the address.

    `prefill` IS WHY THIS IS A CLASS AND NOT A 2-TUPLE. CypherFaucet documents an
    `?address=` query parameter on every faucet page -- "the claim box arrives
    pre-filled, and they solve the captcha and click" -- so the printed URL can
    carry the address the operator is about to paste. That removes the one step in
    this whole flow where a human copies a 42-character string by hand, which is
    exactly where a wrong-prefix address would slip through unnoticed. Faucets with
    no such parameter get the bare URL, because appending one to a site that
    ignores it would teach the operator it works everywhere.
    """

    url: str
    note: str
    prefill: bool = False

    def link_for(self, address: str) -> str:
        """The URL to print, with the address in it where the faucet supports that."""
        return f"{self.url}?address={address}" if self.prefill else self.url


FAUCETS: dict[str, tuple[Faucet, ...]] = {
    # CypherFaucet IS FIRST BECAUSE IT SERVES BOTH LEGS AND TAKES NO CAPTCHA ON ITS
    # API -- read verbatim from its own README (AGPL-3.0, Tech1k), which documents
    # `POST /api/v1/claim` with {"network":"<slug>","address":"<addr>"}, slugs
    # xmr-stagenet / xmr-testnet / ltc-testnet / btc-testnet, and errors 400 invalid
    # / 409 empty / 429 rate-limited / 503 node-busy. It is OFF BY DEFAULT in the
    # faucet's config, so this file does not call it -- see the module docstring.
    #
    # THE BTC PAGE SLUG IS NOT ESTABLISHED and is deliberately not guessed. The
    # README says "Slugs are the URL slugs" and lists `btc-testnet`; a search result
    # surfaced the page as `/btc-testnet4`. Both are plausible and this container
    # cannot fetch either to find out. `GET /api/v1/info` lists every faucet with its
    # slug and settles it from the source in one request, which is the first thing to
    # run rather than a second URL to try.
    "BTC": (
        Faucet("https://cypherfaucet.com/btc-testnet", "0.01 tBTC/hour, no signup, per-IP limit; "
               "slug per its README -- /btc-testnet4 also seen, ask /api/v1/info", prefill=True),
        Faucet("https://mempool.space/testnet4/faucet", "needs a sign-in"),
        Faucet("https://faucet.testnet4.dev", "1 Mtsat per request, 24h cooldown; may be dry"),
    ),
    "LTC": (
        Faucet("https://cypherfaucet.com/ltc-testnet", "0.01 tLTC/hour; the ?address= form is "
               "quoted verbatim in its README", prefill=True),
        Faucet("https://litecointf.salmen.website", "claims up to 1.5 tLTC, 1 request/hour"),
        Faucet("https://tltc.bitaps.com", "0.01 tLTC per 5 min -- REPORTED STUCK at block 4,887,883"),
    ),
}


def wallet_name_for(asset: str) -> tuple[str, str]:
    """The wallet name the APPLICATION reads for this chain, or ("", why not).

    Config.RPC is the authority and this does not fall back to a name of its own.
    See this module's header: four wallet names already exist in this tree, and a
    funded wallet the payout path cannot see is worse than no wallet because it
    looks finished.
    """
    settings = Config.RPC.get(asset) or {}
    name = str(settings.get("wallet") or "").strip()
    if name:
        return name, ""
    return "", (
        f"{asset}_RPC_WALLET is not set, so this tool does not know which wallet the payout "
        f"path will read -- config.py defaults it to \"\", which makes the adapter talk to the "
        f"daemon's default wallet. Creating a wallet of my own choosing would make a FIFTH "
        f"wallet name in this tree and the application would not read it. Export "
        f"{asset}_RPC_WALLET (the operator's own deployment uses desk_hot) and run this again."
    )


def adapter_for_chain(console: Console, asset: str):
    """An adapter for this chain from Config.RPC ALONE, or None with the reason said.

    NO CONF FALLBACK, AND THAT IS A DECISION THE REGISTER FORCED. The first version
    of this called chains/daemon_conf.conf_fallback_settings() the way
    chain_balances.py does, and
    tests/test_daemon_conf.py::test_EVERY_entry_point_THAT_RESOLVES_A_CHAIN_is_on_one_side_or_the_other
    refused to let the file land without a side. Asking the question changed the
    answer: this is SERVICE SIDE.

    WHY, and it is the same argument the register makes for fund_desk.py. The whole
    purpose of this file is to create THE WALLET THE PAYOUT WORKER WILL SPEND FROM.
    A conf fallback resolving some other daemon would do the quietest wrong thing
    available: create `desk_hot` on a node the service never talks to, print
    "created wallet 'desk_hot'", and leave the brokered terminal with no wallet at
    all. Every screen would say the step was done.

    IT ALSO MAKES THE REFUSALS CONSISTENT, which the first version was not.
    wallet_name_for() already refuses an unset $ASSET_RPC_WALLET on exactly this
    reasoning -- "a funded wallet the payout path cannot see looks finished and is
    not" -- so honoring a conf-resolved HOST while refusing an unexported wallet
    NAME was two answers to one question in one file.

    build_adapters(Config.RPC) is the same construction the service makes, which is
    the property that matters here: the daemon this creates a wallet on is the daemon
    the payout worker will ask for a balance.
    """
    built = build_adapters(Config.RPC).get(asset)
    if built is None:
        console.say(f"    {why_unconfigured(asset, Config.RPC)}")
        console.say("    NO CONF FALLBACK HERE, deliberately: this creates the wallet the payout "
                    "worker spends from, so it must be the daemon the service itself resolves. "
                    "Export the variables above rather than relying on a conf.")
    return built


def address_shape_check(console: Console, asset: str, network: str, address: str) -> bool:
    """Does the address the daemon just handed us look like THIS network's?

    NOT DECORATION, AND THE REASON IS ONE BYTE. Base58 version 0x6F is shared by
    BTC testnet, BTC regtest, BTC signet, LTC testnet, LTC regtest AND GRC testnet
    -- six networks, one byte -- so a base58 address proves almost nothing about
    which chain it belongs to. The bech32 HRP is the only part that distinguishes
    them: `tb` for BTC testnet3/testnet4/signet, `bcrt` for BTC regtest, `tltc`
    for LTC testnet, `rltc` for LTC regtest.

    So this is the check that catches the error that actually happens: a faucet
    payment sent to an address from the WRONG daemon, or from a daemon still on
    the old regtest datadir. A `bcrt1...` address pasted into a testnet4 faucet is
    rejected by the faucet if you are lucky and swallowed if you are not.

    A CHAIN WITH NO BECH32 AT ALL IS NOT A FAILURE. bech32_prefix_status() returns
    four outcomes for exactly this reason, and only PREFIX_KNOWN is a claim about
    what the address should start with -- the renderer that branched on a bare None
    here invented "no bech32 on this chain -- its addresses are base58" for XRP,
    which has no row at all (2026-10-09).
    """
    status, prefix = bech32_prefix_status(asset, network)
    if status != PREFIX_KNOWN or prefix is None:
        console.say(f"    address shape NOT CHECKED: {status} for {asset}/{network}, so there is "
                    f"no expected prefix to compare against")
        return True
    # THE PREFIX ALREADY INCLUDES THE SEPARATOR -- "tb1", "tltc1", "bcrt1" -- which is
    # checked rather than assumed. The first version here wrote
    # `address.startswith(prefix + "1")`, i.e. "tb11", which no address starts with, so
    # every address would have failed its own shape check. PAYABLE_BECH32_PREFIX is the
    # table; it spells the 1 because the 1 is part of what tells the networks apart.
    matched = address.startswith(prefix)
    # AND THE VERDICT IS A STRING, NOT A BOOL, because this is regtest.console.Console.
    # step_console.py's own header predicted this mistake in this exact direction: "a
    # reader moving a line between a `from regtest.console import FAIL, OK, Console`
    # file (11 precedents) and a `from step_console import Console` file". I made it.
    # Passing `matched` directly printed `0` instead of `FAIL`, added a phantom
    # `False: 1` key to counts, left counts[FAIL] at 0 and appended NOTHING to
    # `failures` -- a failed check invisible to the summary and to any exit code read
    # off it, which is C16's incident in the other direction. The test for a
    # wrong-prefix address is what caught it.
    return console.check(
        f"{asset} address prefix", address.split("1", maxsplit=1)[0] + "1", prefix,
        OK if matched else FAIL,
    ) == OK


def prepare_chain(console: Console, asset: str) -> dict:
    """One chain, end to end. Returns a row for the closing block.

    THE ORDER IS THE SAFETY PROPERTY, and it is the same ordering fund_regtest_chain()
    documents for assert_regtest: the NETWORK is established before anything is
    written. createwallet is the only write in this file and it happens after the
    daemon has named a network on the allowlist -- so a mainnet daemon reached
    through a misconfigured port is refused before a wallet is made in it.
    """
    console.say(f"{asset}: resolving an adapter")
    adapter = adapter_for_chain(console, asset)
    if adapter is None:
        console.check(f"{asset} configured", "no", "an adapter", FAIL)
        return {"asset": asset, "ok": False, "why": "not configured"}

    network = chain_network(adapter)
    verdict = test_network_verdict(asset, network)
    if verdict != NETWORK_IS_TEST:
        console.check(f"{asset} network", network, "a test network", FAIL)
        console.say(f"    {_refusal(asset, network, verdict)}")
        return {"asset": asset, "ok": False, "why": verdict}
    console.check(f"{asset} network", network, "a test network", OK)

    name, why_not = wallet_name_for(asset)
    if not name:
        console.check(f"{asset} wallet name", "unset", f"{asset}_RPC_WALLET", FAIL)
        console.say(f"    {why_not}")
        return {"asset": asset, "ok": False, "why": "no wallet name"}

    ensure_wallet_on(
        console, adapter,
        WalletSite(
            asset=asset,
            where=getattr(adapter, "url", "its RPC endpoint"),
            if_broken=(f"A wallet named {name!r} exists on this daemon and will not load. Look at "
                       f"the daemon's own wallets directory; nothing here removes one."),
        ),
        name,
    )

    info = adapter.call("getwalletinfo")
    kind = "descriptor" if info.get("descriptors") else "legacy"
    console.say(f"    wallet {name!r} is a {kind} wallet (REPORTED, not chosen -- Core 28 makes "
                f"descriptor, Litecoin 0.21 makes legacy, and the client reads this field itself)")
    console.say(f"    balance   {info.get('balance')}")

    address = adapter.call("getnewaddress")
    # THE ANSWER IS USED, and it was not. This read
    # `address_shape_check(console, asset, network, address)` with the result thrown
    # away, so a wrong-prefix address printed FAIL and was then listed under "PASTE
    # THESE INTO A FAUCET" anyway -- a correct decision function whose caller discards
    # the verdict, which rule 19 names as the thing that "looks exactly like a working
    # feature until somebody checks whether the page changes". Found by a mutation that
    # SURVIVED: appending the separator to the prefix (my original bug, which would have
    # failed EVERY address) changed no test, because nothing asserted that a correct
    # address passes and nothing acted on the verdict.
    #
    # An address whose shape does not match the network is not an address to hand over:
    # the realistic cause is a daemon still on the old regtest datadir, and a faucet
    # payment to it is thrown away.
    if not address_shape_check(console, asset, network, address):
        console.say("    REFUSING to offer this address for a faucet: its prefix does not match "
                    "the network this daemon reports, so it is not an address on the chain you "
                    "think it is. The wallet WAS created; nothing else is wrong with it.")
        return {"asset": asset, "ok": False, "why": "address prefix does not match the network"}

    # `why` AND THE HEIGHTS, and the key name is `why` -- checked, because the first
    # version of this line read `sync['line']` and raised KeyError on the one path an
    # operator mid-sync would actually take. sync_verdict() returns
    # {"state", "why", "blocks", "headers", "behind", "progress"} and says so in its
    # own docstring; I guessed at a key instead of reading it, and the test for the
    # syncing case is what found it.
    sync = sync_verdict(adapter.call("getblockchaininfo"))
    if sync["state"] != SYNC_SYNCED:
        console.say(f"    STILL SYNCING: {sync['why']}")
        console.say(f"    heights   blocks {sync['blocks']} / headers {sync['headers']}, "
                    f"{sync['behind']} behind")
        console.say("    a faucet payment to the address below is SAFE to make now -- it lands on "
                    "the chain whether or not this node has caught up -- but this daemon will not "
                    "REPORT it until the sync passes that block.")
    return {"asset": asset, "ok": True, "wallet": name, "address": address,
            "network": network, "kind": kind, "synced": sync["state"] == SYNC_SYNCED}


def _refusal(asset: str, network: str, verdict: str) -> str:
    """Why this daemon was refused, in the words the two cases actually need.

    TWO SENTENCES, because test_network_verdict() tells them apart and collapsing
    them is what cost something before: "the daemon would not say" is an RPC or
    credential problem and "the daemon said a chain that is not allowed" is a
    daemon pointed somewhere else. The second deliberately does NOT say "this is a
    mainnet daemon" -- for LTC a refused network may be signet, and a confidently
    wrong claim about a testnet node is the defect CHAIN_TEST_NETWORKS' own comment
    records from 2026-10-10.
    """
    if verdict == NETWORK_NOT_ESTABLISHED:
        return (
            f"the {asset} daemon did not name its network ({network}), so whether it is a test "
            f"chain was NOT established and NOTHING was created in it. An unreadable network is "
            f"refused rather than assumed: a port number is a convention, the network is a "
            f"property of the daemon. Check the RPC credentials first -- this is the failure a "
            f"401 produces."
        )
    return (
        f"the {asset} daemon answered network {network!r}, which is not on this chain's allowlist. "
        f"NOTHING was created in it. This tool will not make a wallet on a chain it was not told "
        f"is disposable, and there is no flag that changes that."
    )


def report(console: Console, rows: list[dict]) -> None:
    """The closing block: what to paste where. Printed even when nothing worked.

    Rule 14: never let a result print nothing, and echo the parameters that decide
    the answer. A run where every chain refused still has to say which chains were
    tried and why each one stopped, because a blank tail is ambiguous between "all
    done" and "it died".
    """
    console.say("")
    console.say("=" * 72)
    ready = [row for row in rows if row.get("ok")]
    if not ready:
        console.say("NO CHAIN IS READY FOR A FAUCET. Nothing was created. Reasons above, one per chain:")
        for row in rows:
            console.say(f"  {row['asset']}  {row.get('why', 'unknown')}")
        return
    console.say("PASTE THESE INTO A FAUCET. The addresses are RECEIVING addresses and are safe to")
    console.say("publish; nothing secret is printed by this tool and it has no send path.")
    for row in ready:
        console.say("")
        console.say(f"  {row['asset']}  ({row['network']}, wallet {row['wallet']!r}, {row['kind']})")
        console.say(f"        {row['address']}")
        if not row["synced"]:
            console.say("        ^ this daemon is STILL SYNCING: pay it now, see it later")
        for faucet in FAUCETS[row["asset"]]:
            console.say(f"        {faucet.link_for(row['address'])}")
            console.say(f"            {faucet.note}  [{SEARCHED_NOT_TESTED}]")
    console.say("")
    console.say("Then, to see the money arrive:  python3 chain_balances.py")
    console.say("Re-running this file is safe and idempotent: an existing wallet is loaded, not")
    console.say("replaced, and each run asks for a FRESH receiving address.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    for asset in CHAINS:
        parser.add_argument(f"--{asset.lower()}", action="store_true",
                            help=f"prepare the {asset} testnet wallet")
    parser.add_argument("--all", action="store_true", help="every chain above")
    args = parser.parse_args(argv)

    chosen = [a for a in CHAINS if args.all or getattr(args, a.lower())]
    if not chosen:
        parser.print_help()
        print("\nNothing selected, so nothing was done. Pick a chain, or --all.")
        return 2

    # ANNOUNCED BEFORE THE WORK, not after it (rule 14). Each chain makes two or
    # three RPCs against a daemon that may be mid-sync and slow to answer, and a
    # blinking cursor is what makes an operator Ctrl-C a healthy run.
    console = Console(total_steps=len(chosen))
    console.say(f"testnet_wallets: preparing {len(chosen)} chain(s): {', '.join(chosen)}")
    console.say("createwallet is the only write this tool makes, and it happens only after the "
                "daemon names a network on the allowlist.")
    console.say("")

    rows: list[dict] = []
    for number, asset in enumerate(chosen, start=1):
        # THREE ARGUMENTS, because this is regtest.console.Console. The two classes
        # named Console differ in THREE methods and I have now written the wrong one
        # twice in this file: step_console's is `step(number, title)` and this one is
        # `step(number, chain, title)`. The first crossing was check()'s verdict type
        # and a test caught it; this one crashed on the operator's host, because every
        # test in tests/test_testnet_wallets.py called prepare_chain() or report()
        # directly and NOTHING ran main(). The gate in test_step_console.py now
        # compares every console call in both populations against the class the file
        # actually imports.
        console.step(number, asset, "testnet wallet")
        try:
            rows.append(prepare_chain(console, asset))
        except (RegtestSetupError, OSError) as error:
            # NAMED AND COUNTED, not raised: one chain that cannot be reached must
            # not stop the other from being prepared, and the operator needs the
            # address for whichever one worked. The same shape fund_testnets.main()
            # uses for its per-chain failures.
            console.say(f"    FAILED: {type(error).__name__}: {error}")
            rows.append({"asset": asset, "ok": False, "why": f"{type(error).__name__}"})
    report(console, rows)
    return 0 if all(row.get("ok") for row in rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
