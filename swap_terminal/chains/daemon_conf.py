#!/usr/bin/env python3
"""A bitcoin-style daemon conf, read so nobody has to paste a password.

Role: submodule (a decision -- parse a conf, decide what it configures)
Reads: one <datadir>/<name>.conf, given by path. It opens no socket and reads
        no environment variable.
Writes: nothing
Can move funds: no
Mainnet-safe: yes to call. It returns what a conf SAYS; every caller still
        checks the daemon's actual network (chains/daemon_network.py) before
        asking a wallet anything. A conf claiming regtest is not evidence that
        the daemon on that port is on regtest.

WHY THIS EXISTS

The daemon's credentials are already written down, once, in its own conf file.
Config.RPC reads them from LTC_RPC_USER / LTC_RPC_PASS / LTC_RPC_PORT in the
environment instead, so an operator who has a working litecoind must copy three
values out of a file and export them -- and one of the three is a password.

That copy is rule 8's shape: two sources for one fact, and they drift the moment
the conf is edited. It is also the repository's worst hazard by history. On
2026-09-25 a wallet CLI printed a 25-word recovery seed into a terminal whose
whole output was then pasted into a chat, and that wallet had to be treated as
public from then on. Asking someone to `echo` an rpcpassword is that same shape
with a smaller blast radius, and the way to not have it is to never need the
value on screen.

SO THE PASSWORD IS NEVER RETURNED IN THE EXPLANATION, only in the connection
mapping the caller splats into an adapter. describe() names the FILE and which
KEYS matched -- never a value -- which is the same rule chains/xrp_testnet.py's
saved_faucet_accounts() already follows for faucet seeds, and for the same
reason: the key names are what make a parse failure diagnosable, and the values
are what must not be on a screen.

CONFIGURATION IS STILL NOT AUTHORIZATION. Reading a conf makes a chain
REACHABLE. It does not make a pair swappable (Config.ALLOWED_PAIRS), it does not
unlock a wallet, and it does not decide that a daemon is safe to poll -- the
network check does that, after this, every time.
"""

from __future__ import annotations

from pathlib import Path

# CONSTANTS ONLY, and the import is at module scope deliberately. regtest/daemons.py
# has no import-time side effects -- verified by walking its module body, no call
# outside a def -- and CHAIN_DEFAULTS is the one place that owns each chain's datadir
# and conf name. Respelling them here would be two sources for the fact that moves
# whenever the harness is pointed somewhere else (rule 8).
from regtest.daemons import CHAIN_DEFAULTS

#: The three keys a JSON-RPC connection needs out of a conf, and what this
#: module calls them. The names on the right are Config.RPC's, so a mapping
#: built from these splats straight into RPCAdapter like any other entry.
CONF_TO_SETTING = {"rpcuser": "user", "rpcpassword": "password", "rpcport": "port"}

#: Keys whose VALUE may never appear in a message, a log or a return value other
#: than the connection mapping itself. One entry today; named as a set because
#: the rule is about the class of thing, not about this key.
SECRET_CONF_KEYS = frozenset({"rpcpassword"})


class DaemonConfError(Exception):
    """A conf could not be read, or does not configure an RPC connection."""


def parse_daemon_conf(text: str, *, network: str = "") -> dict[str, str]:
    """Every rpc setting in `text`, with a matching [network] section winning.

    BITCOIN'S CONF HAS SECTIONS AND THE PORT IS USUALLY IN ONE. A regtest daemon
    is conventionally configured with rpcuser and rpcpassword at the top level
    and rpcport inside `[regtest]`, because the port differs per network and the
    credentials do not. Reading only the top level finds the credentials and no
    port, which produces "unconfigured" on a daemon that is running and
    answering -- so both levels are read and the section overrides.

    A `#` ONLY STARTS A COMMENT AT THE BEGINNING OF A LINE. Modern Bitcoin Core
    does not strip a trailing comment from a value, and an rpcpassword
    containing `#` is both legal and likely from a generator. Stripping inline
    would silently truncate it, and the failure would arrive as a 401 from the
    daemon with nothing pointing here.
    """
    top: dict[str, str] = {}
    section: dict[str, str] = {}
    current = ""
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            current = line[1:-1].strip()
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if key not in CONF_TO_SETTING:
            continue
        if not current:
            top[key] = value
        elif network and current == network:
            section[key] = value
    return {**top, **section}


def rpc_settings_from_conf(path: Path, *, network: str = "", host: str = "127.0.0.1",
                           timeout: float = 30.0) -> dict:
    """A Config.RPC-shaped entry for the daemon this conf describes.

    Raises DaemonConfError rather than returning a partial mapping. A mapping
    missing the password builds an adapter that 401s on every call, which
    chains/registry.py's missing_settings() was added for on 2026-09-26 -- so
    handing one back would push a known-broken adapter one layer further in.
    """
    try:
        text = Path(path).read_text()
    except OSError as error:
        raise DaemonConfError(f"{path}: {type(error).__name__}: {error}") from error

    found = parse_daemon_conf(text, network=network)
    missing = [key for key in CONF_TO_SETTING if key not in found]
    if missing:
        raise DaemonConfError(
            f"{path} has no {', '.join(missing)}"
            + (f" (looked at the top level and in [{network}])" if network else "")
        )
    try:
        port = int(found["rpcport"])
    except ValueError as error:
        raise DaemonConfError(f"{path}: rpcport={found['rpcport']!r} is not a number") from error

    return {"user": found["rpcuser"], "password": found["rpcpassword"], "host": host,
            "port": port, "wallet": "", "timeout": float(timeout)}


def describe(path: Path, settings: dict, *, network: str = "") -> str:
    """One line saying where a connection came from. NAMES ONLY, NEVER VALUES.

    The password is in `settings` because an adapter needs it. It is not in this
    string, and the test that holds that is the reason this function exists
    rather than each caller formatting its own line -- a second formatter is a
    second chance to interpolate the wrong key (rule 8).
    """
    keys = ", ".join(sorted(CONF_TO_SETTING))
    return (f"read {keys} from {path}"
            + (f" (top level plus [{network}])" if network else "")
            + f"; connecting to {settings['host']}:{settings['port']} as {settings['user']}. "
            + "The password was read and is NOT shown.")


#: WHICH CHAINS MAY BE RESOLVED FROM A CONF, and what network their port section is
#: under. Every entry must also be in regtest.daemons.CHAIN_DEFAULTS, which is where
#: the datadir and conf name come from.
#:
#: GRC IS DELIBERATELY ABSENT. Its conf lives in ~/.GridcoinResearch, shared by
#: mainnet and testnet both, and the operator's mainnet wallet is a live staking
#: wallet holding real coins. Picking a connection out of that file is how a reader
#: -- or worse, a driver that sends -- ends up pointed at it. The network check
#: would still refuse a mainnet answer, but the place to not make that mistake is
#: before the call. Gridcoin stays explicit: GRC_RPC_PORT is set by hand, or GRC is
#: not reached at all.
CONF_FALLBACK_NETWORK = {"BTC": "regtest", "LTC": "regtest"}


def conf_fallback_settings(chain: str) -> tuple[dict | None, str]:
    """This chain's connection from its own conf, and a line about the attempt.

    NEVER RAISES, and always returns a sentence. A caller deciding whether a chain
    is reachable has to be able to say WHY it is not, and an exception at this
    point would arrive three frames up as something about a file rather than
    something about a chain (rule 14).

    SHARED BY EVERY ENTRY POINT, which is the whole reason it is here rather than
    in the script that needed it first. chain_balances.py grew this on 2026-09-29
    and atomic_swap_xrp.py did not, so the operator configured Litecoin, watched
    the balance reader find it, and then watched the SWAP DRIVER say
    "LTC adapter configured: (none)" about the same daemon. Two answers to "is
    this chain reachable" is rule 8's shape, and it cost a round trip within the
    hour of the first one being written.
    """
    network = CONF_FALLBACK_NETWORK.get(chain)
    if network is None:
        return None, (f"{chain} is not resolved from a conf by design; set {chain}_RPC_PORT, "
                      f"{chain}_RPC_USER and {chain}_RPC_PASS to reach it")
    spec = CHAIN_DEFAULTS.get(chain)
    if spec is None:
        return None, (f"{chain} is listed for conf fallback but regtest.daemons.CHAIN_DEFAULTS has "
                      f"no entry for it. One of those two tables is wrong.")
    path = Path(spec["datadir"]).expanduser() / spec["conf_name"]
    try:
        settings = rpc_settings_from_conf(path, network=network)
    except DaemonConfError as error:
        return None, f"no {chain}_RPC_* in the environment and the conf did not supply one: {error}"
    return settings, f"{chain}: {describe(path, settings, network=network)}"


def rpc_url(settings: dict) -> str:
    """http://host:port, with /wallet/<name> when one is configured.

    ASSEMBLED FROM host AND port RATHER THAN READ FROM A SECOND VARIABLE. Some
    callers read {ASSET}_RPC_URL and some have the host and port already because
    an adapter needed them; asking an operator to set a URL as well as a port --
    two settings that must agree -- is a second source for one fact (rule 8).
    This is the one assembly, extracted 2026-09-29 from
    atomic_swap_xrp.build_script_client(), which was the only place that did it
    until atomic_swap.client_for() needed the same thing.
    """
    host = settings.get("host") or "127.0.0.1"
    port = settings.get("port")
    if not port:
        raise DaemonConfError(f"no port in {sorted(settings)}, so no RPC url can be built")
    wallet = (settings.get("wallet") or "").strip()
    return f"http://{host}:{port}" + (f"/wallet/{wallet}" if wallet else "")
