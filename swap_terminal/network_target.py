"""Which CHAIN an RPC endpoint points at, decided in one place.

Role: submodule (pure functions over host/port; holds the chain vocabulary)
Reads: nothing -- every input is an argument
Writes: nothing
Can move funds: no. It decides nothing about amounts. It does decide what an
      operator is TOLD about which chain a port belongs to, which is how a
      mainnet endpoint gets noticed before it is used.
Mainnet-safe: yes. Opens no socket and reads no environment.

WHY THIS EXISTS, measured 2026-09-26 after the operator reported "we're still
pulling from grc mainnet wallet and not the testnet wallet."

They were right, and the cause was a silent default. config.Config.RPC read:

    "port": int(os.getenv("GRC_RPC_PORT", "15715"))

15715 is Gridcoin MAINNET. Nothing in the serving path loads a .env -- neither
wsgi.py nor gunicorn.conf.py nor config.py itself -- so config.py sees only the
process environment, and an unset GRC_RPC_PORT fell through to that default.
Measured in a clean environment: BTC 8332, LTC 9332, GRC 15715, all three the
mainnet port. And services/payout_service.refresh_wallet_inventory() calls
get_balance() on EVERY adapter every cycle, so the Gridcoin adapter was polling
the operator's live staking wallet on a loop.

THE DEFECT CLASS IS THE SILENT DEFAULT, NOT THE PARTICULAR NUMBER. A port is a
choice of which blockchain real money lives on. Guessing it is not a
convenience, and guessing the mainnet one is the worst available guess. The
correct pattern was ALREADY in that same dict for the newer chains:

    XMR_RPC_PORT = int(os.getenv("XMR_RPC_PORT", "0"))   # 0 means "no wallet here"
    SOL: constructed only when SOL_RPC_URL is set
    XRP: url defaults to empty

So BTC, LTC and GRC are not getting a new convention invented for them; they
are being made to follow the one their own file already uses three times. Rule
11's "one vocabulary, derived in one place", applied to chain selection.

This module is also the single home for port numbers that were spelled twice
before it existed (rule 8: two copies of one rule is a bug with a delay on it).
`MAINNET_RPC_PORT = 15715` appeared in BOTH gridcoin_credentials.py and
transactions.py, and the Gridcoin test-port set appeared in transactions.py as
KNOWN_TESTNET_RPC_PORTS while gridcoin_credentials.py knew only 25779. Those two
disagreed about what a test chain is: a port of 25715 or 9876 was a recognized
test chain to one file and an unknown port to the other. Both now read from
here.

THE PORTS ARE CONVENTIONS, NOT GUARANTEES, and that limit is the reason
classify() has an UNRECOGNIZED answer instead of assuming. Any daemon can be
started on any port with -rpcport, so a port is evidence about which chain is
being addressed and not proof. Where proof is available it beats this: the XRP
path asks the server for its `network_id` and refuses on a mainnet id
(xrp_send_tagged.refuse_mainnet), because a URL can point anywhere. Bitcoin,
Litecoin and Gridcoin have no equivalent single field, so their conventional
ports are the best signal available without a call -- which is why this module
reports and never authorizes.
"""

from __future__ import annotations

from typing import NamedTuple

# Not configured, deliberately distinct from every real port. Matches the
# convention config.py already documents for XMR_RPC_PORT ("unset means no
# Monero wallet here"), so a reader meets one meaning of 0 and not two.
UNCONFIGURED_PORT = 0


class ChainPorts(NamedTuple):
    """One chain's conventional RPC ports.

    `test_ports` is a frozenset rather than one number because these chains have
    several test networks -- Bitcoin separates testnet from regtest, and
    Gridcoin's testnet has been seen on four different ports across the
    operator's own configuration files (25715 twice, 9876, 25779, surveyed
    2026-09-25). A single "the testnet port" field could not express that and
    would have to pick one, which is how transactions.py and
    gridcoin_credentials.py came to disagree.
    """

    mainnet_port: int
    test_ports: frozenset[int]
    port_variable: str
    test_hint: str


# Gridcoin's four test ports are the ones actually present in the operator's
# gridcoin.conf files, surveyed 2026-09-25 -- not a guess at what is
# conventional. 25779 is the one the running testnet daemon uses and is what
# gridcoin_credentials.TESTNET_RPC_URL names.
CHAIN_PORTS: dict[str, ChainPorts] = {
    "BTC": ChainPorts(8332, frozenset({18332, 18443}), "BTC_RPC_PORT", "18443 regtest, 18332 testnet"),
    "LTC": ChainPorts(9332, frozenset({19332, 19443}), "LTC_RPC_PORT", "19443 regtest, 19332 testnet"),
    "GRC": ChainPorts(15715, frozenset({25715, 25779, 9876}), "GRC_RPC_PORT", "25779 testnet"),
}


# THE ONE VALUE PER CHAIN THAT CANNOT BE DEFAULTED, for the three chains that
# are not in CHAIN_PORTS.
#
# CHAIN_PORTS holds the Bitcoin-derived three because they have a conventional
# mainnet port to classify a configured one AGAINST. These three have no such
# table and do not need one:
#
#   SOL  an endpoint URL; there is no port to compare
#   XRP  an endpoint URL, for the same reason
#   XMR  a port, but monero-wallet-rpc has no conventional one -- it is whatever
#        the operator passed to --rpc-bind-port, so there is nothing to classify
#        against and chains/registry.py says so at its construction site
#
# Kept next to CHAIN_PORTS rather than in a second file because the question
# "what do I set to reach this chain?" has exactly one answer per chain and it is
# asked from three places now: the workers' startup banner, the swap page's pair
# list, and create_swap()'s refusal. Three copies of a variable NAME is rule 8's
# shape with a typo waiting in it.
_ENDPOINT_VARIABLES: dict[str, str] = {
    "SOL": "SOL_RPC_URL",
    "XRP": "XRP_RPC_URL",
    "XMR": "XMR_RPC_PORT",
}


def configuring_variable(chain: str) -> str:
    """The environment variable that makes this chain reachable. Never raises.

    Verified against config.py 2026-09-26 rather than recalled: BTC/LTC/GRC read
    <CHAIN>_RPC_PORT (config.py:204 for GRC), XMR reads XMR_RPC_PORT
    (config.py:116), XRP reads XRP_RPC_URL (config.py:134) and SOL reads
    SOL_RPC_URL (config.py:192). chains/registry.build_adapters() skips a chain
    whose value is falsy, which is what makes this the variable that decides
    whether an adapter exists at all.

    The `<CHAIN>_RPC_PORT` fallback for an unknown chain matches what describe()
    and require_configured() already do two functions below, so a chain added to
    ALLOWED_PAIRS before it is added here produces a plausible name rather than a
    KeyError -- which is the failure this whole function exists to stop being
    printed at a person.
    """
    known = CHAIN_PORTS.get(chain)
    if known:
        return known.port_variable
    return _ENDPOINT_VARIABLES.get(chain, f"{chain}_RPC_PORT")


class NetworkTargetUnconfigured(RuntimeError):
    """No RPC port is set for a chain, so this process declines to guess one.

    Its own type, for the reason GridcoinCredentialsMissing gives: an operator
    reading a traceback has to tell "the daemon said no" from "you did not
    configure me", and a connection refused on a guessed port does not
    distinguish them. It reads as a down daemon, which is the wrong
    investigation.
    """


def classify(chain: str, port: int) -> str:
    """Which network a port conventionally belongs to: MAINNET, TEST, UNCONFIGURED, UNRECOGNIZED.

    Four answers, not two, and each one exists because collapsing it loses
    something an operator needs:

      UNCONFIGURED   nothing was set. Distinct from MAINNET precisely because
                     the old code turned this case INTO mainnet.
      MAINNET        real money.
      TEST           a conventional test port for this chain.
      UNRECOGNIZED   a port this module has no convention for. NOT reported as
                     safe: an operator running a mainnet daemon on a custom
                     -rpcport lands here, and telling them "not mainnet" would
                     be a guess dressed as a measurement (rule 17).
    """
    known = CHAIN_PORTS.get(chain)
    if port == UNCONFIGURED_PORT:
        return "UNCONFIGURED"
    if known is None:
        return "UNRECOGNIZED"
    if port == known.mainnet_port:
        return "MAINNET"
    if port in known.test_ports:
        return "TEST"
    return "UNRECOGNIZED"


def describe(chain: str, host: str, port: int) -> str:
    """One line naming the chain a reader is about to act on, for a startup banner.

    Rule 14: echo the parameters that decide the answer, and say what the number
    MEANS next to the number. A bare `port=15715` requires the reader to know
    Gridcoin's port table; `<- MAINNET, REAL MONEY` does not.
    """
    verdict = classify(chain, port)
    known = CHAIN_PORTS.get(chain)
    if verdict == "UNCONFIGURED":
        variable = known.port_variable if known else f"{chain}_RPC_PORT"
        hint = f"; test chain is {known.test_hint}" if known else ""
        return f"{chain:4} NOT CONFIGURED  <- set {variable} to reach this chain{hint}"
    location = f"{host}:{port}"
    if verdict == "MAINNET":
        return f"{chain:4} {location:24} <- *** MAINNET, REAL MONEY ***"
    if verdict == "TEST":
        return f"{chain:4} {location:24} <- test chain (mainnet is {known.mainnet_port})"
    mainnet = f"; mainnet is {known.mainnet_port}" if known else ""
    return (
        f"{chain:4} {location:24} <- UNRECOGNIZED port, so which chain this is was NOT "
        f"established{mainnet}"
    )


def require_configured(chain: str, port: int) -> int:
    """Return the port, or raise rather than let a caller proceed on a guess.

    For a caller that cannot do anything useful without a real endpoint. The
    message names the variable to set, because "connection refused" sends an
    operator to restart a daemon that was never the problem.
    """
    if port == UNCONFIGURED_PORT:
        known = CHAIN_PORTS.get(chain)
        variable = known.port_variable if known else f"{chain}_RPC_PORT"
        hint = f" The test chain is {known.test_hint}." if known else ""
        raise NetworkTargetUnconfigured(
            f"no RPC port is set for {chain}, and this process will not guess one -- a port "
            f"is a choice of which blockchain real money lives on. Set {variable}.{hint}"
        )
    return port


def startup_lines(rpc: dict) -> list[str]:
    """Every chain's target, named, for a startup banner. Pure so it is testable.

    Rule 14 is the whole reason this exists, and specifically its "announce
    before, not only after" and "echo the parameters that decide the answer."
    The mainnet-default defect survived because nothing ever SAID which chain
    each adapter was pointed at. It was written in config.py's header, which is
    not a place an operator looks while a process is starting, and the only
    symptom was the operator eventually noticing money in the wrong wallet.

    A configured chain that is UNCONFIGURED still gets a line. Rule 14's "never
    let an empty result print nothing": a chain silently missing from a banner is
    indistinguishable from a banner that forgot it, and the whole point here is
    that absence must be legible.
    """
    lines = []
    for chain in sorted(CHAIN_PORTS):
        entry = rpc.get(chain) or {}
        lines.append(describe(chain, entry.get("host", "127.0.0.1"), int(entry.get("port") or 0)))

    # The URL-configured chains are reported by presence only. Their endpoint is
    # a URL rather than a port, so CHAIN_PORTS has no convention to classify it
    # against, and inventing one would be the guess this module exists to
    # refuse. XRP is the case where a real answer is available and is obtained
    # properly: xrp_send_tagged.refuse_mainnet() asks the server for its
    # network_id before signing. That is a call, not a lookup, so it does not
    # belong in a pure function -- named here so a reader knows the stronger
    # check exists and where.
    for chain in ("SOL", "XRP", "XMR"):
        entry = rpc.get(chain) or {}
        target = entry.get("url") or (f"port {entry['port']}" if entry.get("port") else "")
        if target:
            suffix = " (network confirmed from the server at send time)" if chain == "XRP" else ""
            lines.append(f"{chain:4} {target:24} <- configured; chain NOT classified here{suffix}")
        else:
            lines.append(f"{chain:4} NOT CONFIGURED")
    return lines


def mainnet_chains(rpc: dict) -> list[str]:
    """Which configured chains point at a mainnet port. For a caller that must react.

    Separate from startup_lines() because a banner is read by a human and this is
    read by code. Returned as a list rather than a bool so the caller can name
    them, since "something is on mainnet" sends an operator hunting and "GRC is
    on mainnet" does not.
    """
    return [
        chain
        for chain in sorted(CHAIN_PORTS)
        if classify(chain, int((rpc.get(chain) or {}).get("port") or 0)) == "MAINNET"
    ]
