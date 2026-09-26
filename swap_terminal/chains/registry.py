"""The one place that turns Config.RPC into the adapters dict.

Role: submodule (chain registry; holds no decision about swaps, amounts or
      addresses -- it constructs and returns)
Reads: the RPC mapping it is handed. Nothing else: no environment, no file, no
      socket.
Writes: nothing
Can move funds: no -- it CONSTRUCTS adapters that can (RPCAdapter inherits
      send_to_address), and calls nothing on them. No constructor here opens a
      socket, which is a promise workers/common.py's header already makes on
      behalf of three polling workers.
Mainnet-safe: yes

WHY THIS FILE EXISTS: THERE WERE TWO COPIES OF IT (CLAUDE.md rule 8).

Measured 2026-09-25, before this commit, `build_adapters` existed twice:

    app.py:130            def build_adapters(app: Flask) -> dict   reads app.config["RPC"]
    workers/common.py:62  def build_adapters() -> dict             reads Config.RPC

Six lines each, the same three constructors, differing only in where the RPC
mapping came from. That is rule 8's exact shape -- "two copies of one rule is
not redundancy, it is a bug with a delay on it" -- and adding a fourth chain
was the moment it would have bitten: a Solana adapter added to one and not the
other gives an HTTP process that knows about SOL and three workers that do not,
with no error anywhere. The Flask app would hand out a SOL deposit address that
no deposit watcher is looking at.

So the difference is now a PARAMETER (the mapping) rather than a second copy,
and both callers pass their own source.

WHY EVERY CHAIN IS CONDITIONAL, AND WHY THREE OF THEM WERE NOT UNTIL 2026-09-26.

This section used to read "BTC, LTC and GRC are always constructed, because
config.Config always has an entry for them -- defaults and all, pointing at
mainnet ports, which config.py's own header says out loud." Every clause of that
was true, and together they were the bug: the entry existed, the default was the
MAINNET port, and so a checkout with nothing set built three adapters aimed at
real-money daemons. Saying it out loud in a header is not the same as failing
closed. The operator found it from the outside -- "we're still pulling from grc
mainnet wallet and not the testnet wallet" -- which is the shape CLAUDE.md rule
13 warns about: nothing crashed, nothing warned, and the only symptom was
somebody noticing.

All five are now conditional on the one value that cannot be defaulted, and the
test is identical for each: a URL for SOL and XRP, a port for BTC, LTC, GRC and
XMR. config.py defaults those ports to network_target.UNCONFIGURED_PORT.

The reason a missing port must SKIP rather than guess is the one the Solana
paragraph below already gives, plus a second one that only applies to the older
three: guessing wrong here does not merely produce a useless adapter, it
produces a WORKING adapter pointed at mainnet.

SOL is constructed only when SOL_RPC_URL is set, and that is not tidiness. The
adapters dict is walked by services/payout_service.refresh_wallet_inventory(),
which calls get_balance() on EVERY adapter every cycle and logs a WARNING for
each one that fails. An unconfigured Solana adapter would therefore print a
warning per worker per cycle, forever, describing a fault that does not exist
-- and a log that cries wolf is a log nobody reads the day something real
happens. CLAUDE.md rule 14's "make 'did nothing' look different from 'did
work'", applied to a warning that would always be there.

ADDING AN ADAPTER DOES NOT ENABLE A TRADING PAIR, and that separation is
deliberate. `Config.ALLOWED_PAIRS` decides what may be swapped and it is
UNCHANGED: no pair mentions SOL, so services/quote_service.py cannot produce a
SOL quote and services/swap_service.py cannot create a SOL swap. Enabling a
pair is live posture and is the operator's (CLAUDE.md rule 16); this file only
makes the chain reachable for reading.
"""

from __future__ import annotations

from collections.abc import Mapping

from network_target import configuring_variable

from .bitcoin import BitcoinAdapter
from .gridcoin import GridcoinAdapter
from .litecoin import LitecoinAdapter
from .monero import MoneroAdapter
from .solana import SolanaAdapter
from .xrp import XRPAdapter

# The three Bitcoin-derived chains, whose Config.RPC entries all have the same
# six keys and are splatted straight into RPCAdapter's matching signature.
_BITCOIN_DERIVED = {
    "BTC": BitcoinAdapter,
    "LTC": LitecoinAdapter,
    "GRC": GridcoinAdapter,
}


def build_adapters(rpc: Mapping[str, Mapping]) -> dict:
    """Construct every configured chain adapter. Opens no socket.

    `rpc` is Config.RPC, or app.config["RPC"], which is a copy of it.

    THE SOL ENTRY HAS A DIFFERENT SHAPE ON PURPOSE, and the splat is what makes
    that safe. Config.RPC["BTC"] is {user, password, host, port, wallet,
    timeout} -- a Bitcoin JSON-RPC connection. A Solana RPC endpoint has none
    of those: it is one URL, with no HTTP authentication and no wallet path,
    and forcing it into user/password/host/port would mean an adapter whose
    four most prominent parameters are all empty strings, plus a reassembly
    step that could silently produce "http://:0".

    So Config.RPC["SOL"] is {url, commitment, timeout, mint, hot_wallet,
    min_commitment_rank} and SolanaAdapter.__init__ has exactly those names,
    which is the same contract RPCAdapter already has with its own six: the
    dict is built for the signature. Both are `**splatted` here, so a key added
    to one config entry without a matching parameter fails loudly at
    construction rather than being ignored.
    """
    # `and rpc[asset].get("port")` is the fix for 2026-09-26. These three used to
    # be constructed unconditionally, because config.Config always had an entry
    # for them -- and that entry carried a MAINNET port default, so a checkout
    # with nothing set built a Bitcoin adapter aimed at 8332, a Litecoin one at
    # 9332 and a Gridcoin one at 15715. refresh_wallet_inventory() then called
    # get_balance() on each every cycle, which is how the operator's live
    # Gridcoin staking wallet came to be polled on a loop by a terminal that was
    # supposed to be on testnet.
    #
    # The test is the same one SOL, XMR and XRP already use, three paragraphs
    # below: "configured" means the operator supplied the one value that cannot
    # be defaulted. For those it is a URL or a port; for these it is a port. The
    # module header's old claim that these three are "always constructed" was
    # accurate and is now wrong, so it has been rewritten rather than left to
    # mislead the next reader.
    adapters = {
        asset: cls(**rpc[asset])
        for asset, cls in _BITCOIN_DERIVED.items()
        if asset in rpc and rpc[asset].get("port")
    }
    solana = rpc.get("SOL")
    # Configured means "has a URL". See this module's header for why an
    # unconfigured Solana adapter is left out entirely rather than constructed
    # and left to warn on every inventory refresh.
    if solana and solana.get("url"):
        adapters["SOL"] = SolanaAdapter(**solana)

    # XMR is conditional for the same reason SOL is, and on the same test:
    # "configured" means the operator supplied the one value that cannot be
    # defaulted. For Solana that is the endpoint URL; for Monero it is the
    # wallet RPC port, because monero-wallet-rpc has no conventional port the
    # way bitcoind's 8332 does -- it is whatever the operator passed to
    # --rpc-bind-port when they started it, and guessing one would produce an
    # adapter that fails on every cycle against a port nothing is listening on.
    #
    # Config.RPC["XMR"] has its own shape again -- {host, port, user, password,
    # account_index, min_confirmations, can_spend, timeout} -- matching
    # MoneroAdapter.__init__ exactly, for the reason the SOL paragraph above
    # gives: the dict is built for the signature, and the splat makes a
    # mismatch fail loudly here instead of being ignored.
    # XRP is conditional on its URL, exactly as SOL is, and for the same
    # reason: an endpoint is the one value that cannot be defaulted.
    xrp = rpc.get("XRP")
    if xrp and xrp.get("url"):
        adapters["XRP"] = XRPAdapter(**xrp)

    monero = rpc.get("XMR")
    if monero and monero.get("port"):
        adapters["XMR"] = MoneroAdapter(**monero)
    return adapters


def unconfigured_chains(adapters: Mapping[str, object], *assets: str) -> list[str]:
    """Which of these assets have no adapter in this process, in the order given.

    ONE LINE, AND IT EXISTS BECAUSE THE ALTERNATIVE WAS MEASURED ON A PERSON.

    2026-09-26, pasted back from the operator's browser. They had a Gridcoin
    testnet daemon running on 25715, three workers that printed
    `GRC rpc=127.0.0.1:25715` in their startup banners, a quote that priced
    1 XRP -> 56.6 GRC, and this on the page when they pressed Create swap:

        No swap was created: 'GRC'

    That is `str(KeyError("GRC"))`. create_swap() did `adapters[to_asset]`, the
    server process had no GRC_RPC_PORT in its environment, build_adapters()
    therefore skipped Gridcoin, and the subscript raised. routes/swaps.py's HTTP
    boundary caught it and returned `str(exc)`, which for a KeyError is the repr
    of the key and nothing else -- no chain, no cause, no remedy, and
    indistinguishable from a typo in a dictionary somewhere. The operator had no
    way to get from that string to "export GRC_RPC_PORT before starting the
    server", which is the entire content of the failure.

    A membership test rather than a try/except around the subscript: the caller
    needs the answer BEFORE it writes anything, and "which chains are missing" is
    a list, not an exception.
    """
    return [asset for asset in assets if asset not in adapters]


def why_unconfigured(asset: str) -> str:
    """One sentence an operator can act on, for a chain with no adapter.

    Names the variable via network_target.configuring_variable() rather than
    spelling it here, so this cannot drift from the workers' startup banner or
    from config.py (rule 8 -- three copies of a variable name is a typo waiting).

    The .env sentence is the part that actually resolves the 2026-09-26 incident
    and it is not incidental: config.py's own header says nothing in the serving
    path loads a .env "because adding load_dotenv() to a module read at import is
    the import-time side effect rule 12 names as a measured past defect". That is
    a deliberate choice, so the consequence -- the variable must be in the
    PROCESS environment of whatever starts the server, not merely in a file next
    to it -- belongs in the message rather than in a docstring the operator will
    never see.
    """
    return (
        f"{asset} has no adapter in this process: {configuring_variable(asset)} is unset (or 0) in the "
        f"environment this process was started with. Nothing in the serving path reads a .env, so it has "
        f"to be exported in the shell that starts the server -- a value set only in a file, or only in "
        f"another shell, does not reach here."
    )
