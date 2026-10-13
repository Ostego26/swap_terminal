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

# Not configured, deliberately distinct from every real port. One meaning of 0
# across this file and config.py, so a reader meets one and not two: 0 is never
# a port somebody meant, it is the absence of a setting.
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
#: PORTS THAT BELONG TO A WALLET THIS STACK DOES NOT OWN. Hands off, by name.
#:
#: =========================================================================
#: TWO POPULATIONS OF WALLET PER CHAIN, AND NOTHING HERE KNEW IT
#: =========================================================================
#:
#: Operator, 2026-10-13, correcting a claim this tree had been printing on every
#: `off`, `up` and `chains` for a day: "ltc is not the gui wallet that is a
#: 'external' wallet for the swapper to send or receive ltc" and then "we have a gui
#: wallet for grc testnet and it's a fake 'external' wallet too and a goddamn
#: motherfucker cli is supposed to be a part of the stack."
#:
#: So per chain there are TWO kinds of wallet on this host:
#:
#:   THE STACK'S OWN      a headless CLI daemon, part of the deployment. bitcoind on
#:                        18443, litecoind on 19443, gridcoinresearchd on 25779.
#:                        These are what the terminal talks to, starts and stops.
#:   AN EXTERNAL WALLET   a GUI the operator uses BY HAND to send test coins into a
#:                        swap or receive them out of it. It is a counterparty, not
#:                        infrastructure. The stack must never address it, configure
#:                        itself against it, or stop it.
#:
#: NOTHING IN THIS TREE DISTINGUISHED THEM, and the cost is measured twice:
#:
#:   2026-10-12  `swap_stack.py off` stopped "the daemon on the configured port" and
#:               CLOSED the operator's litecoin-qt, because LTC_RPC_PORT was 19332.
#:               The guard added afterwards -- never stop what you cannot start --
#:               treated the symptom. The cause was that the terminal was configured
#:               against an external wallet, and it still was a day later.
#:   2026-10-13  GRC_RPC_PORT had been moved off 25715 onto the desk daemon at 25779
#:               earlier, which is why GRC works. But WHY 25715 was wrong was never
#:               written down -- only that it got changed -- so LTC kept the identical
#:               mistake live in .env, and every surface told the operator to recover
#:               the GUI wallet rather than to point at their own node.
#:
#: ORTHOGONAL TO test_ports AND THAT IS DELIBERATE. 19332 and 25715 ARE testnet ports
#: and classifying a configured one as test is still correct -- the axes are
#: test-vs-mainnet and ours-vs-theirs, and conflating them would make a GUI wallet
#: look like a mainnet hazard, which it is not.
#:
#: WHAT IS NOT LISTED IS NOT A CLAIM OF OWNERSHIP. A port absent from here has not been
#: established either way, which is why callers REFUSE on a match and never assert
#: safety from a miss (rule 17).
EXTERNAL_WALLET_PORTS: dict[int, str] = {
    19332: (
        "the operator's litecoin-qt, an EXTERNAL Litecoin wallet for sending coins into a "
        "swap and receiving them out of it. Measured 2026-10-12, when stopping the daemon "
        "on this port closed that window; and again by OPEN_FINDINGS finding 2, where "
        "litecoind could not bind testnet P2P 19335 'because litecoin-qt held it' and the "
        "desk's own node was given its own datadir with port=19444 and rpcport=19443"
    ),
    25715: (
        "the operator's Gridcoin testnet GUI wallet, EXTERNAL in the same sense. The desk's "
        "own daemon is gridcoinresearchd on 25779 (chains/daemon_launch.RECORDED['GRC']), "
        "and GRC_RPC_PORT was moved off 25715 onto it on 2026-10-13 -- this entry is why, "
        "which went unrecorded at the time and let LTC keep the same defect"
    ),
}


def is_external_wallet(port: object) -> str:
    """Why this port must not be addressed by the stack, or "". One reader's question.

    A FUNCTION RATHER THAN A DICT LOOKUP AT EACH SITE because `port` arrives as a str
    from the environment in some callers and an int in others, and an int-keyed `in`
    check silently misses "19332". config.py coerces, discovery does not, and the one
    that misses is the one writing .env.
    """
    try:
        return EXTERNAL_WALLET_PORTS.get(int(port), "")
    except (TypeError, ValueError):
        return ""


CHAIN_PORTS: dict[str, ChainPorts] = {
    # THE HINTS USED TO READ "18443 regtest, 18332 testnet" AND "19443 regtest, 19332
    # testnet", AND ON THIS HOST BOTH HAD IT BACKWARDS where it mattered. The 2026-10-10
    # migration (OPEN_FINDINGS finding 2) moved the desk's own daemons to TEST networks
    # and KEPT those ports: `LISTEN 172.17.0.1:18443 bitcoind` on testnet4 and
    # `LISTEN 172.17.0.1:19443 litecoind` on test. 19332 and 18332 are the DEFAULT
    # testnet RPC ports, which is why an external wallet holds one of them.
    #
    # So a reader setting LTC_RPC_PORT "for testnet" was told 19332 by the one string
    # whose job is to tell them what to set -- and 19332 is the operator's external
    # litecoin-qt, not the stack's daemon. Rule 16 in a string somebody pastes from.
    "BTC": ChainPorts(8332, frozenset({18332, 18443}), "BTC_RPC_PORT",
                      "18443 is this desk's bitcoind (testnet4); 18332 is the DEFAULT testnet port"),
    "LTC": ChainPorts(9332, frozenset({19332, 19443}), "LTC_RPC_PORT",
                      "19443 is this desk's litecoind (test); 19332 is the DEFAULT testnet port and "
                      "is held by an EXTERNAL wallet here"),
    # THE HINT NAMES BOTH TEST PORTS, and it used to name only 25779 while the
    # operator's own Gridcoin test daemon listens on 25715. So the sentence a
    # reader sees when GRC is unconfigured -- "set GRC_RPC_PORT to the test chain
    # (25779 testnet)" -- named a port they do not run, on the one line whose job
    # is to tell them what to set. Both are in test_ports and always were; only
    # the hint was wrong, which is rule 16's "a wrong comment is a bug" in a
    # string an operator pastes from.
    "GRC": ChainPorts(15715, frozenset({25715, 25779, 9876}), "GRC_RPC_PORT",
                      "25715 or 25779 testnet"),
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
#
# Kept next to CHAIN_PORTS rather than in a second file because the question
# "what do I set to reach this chain?" has exactly one answer per chain and it is
# asked from three places now: the workers' startup banner, the swap page's pair
# list, and create_swap()'s refusal. Three copies of a variable NAME is rule 8's
# shape with a typo waiting in it.
class UrlChain(NamedTuple):
    """One chain with no conventional port to classify a configured one against.

    THREE FIELDS AND NOT ONE, AND THE THIRD IS WHY THIS BECAME A NamedTuple ON
    2026-10-12. It was `_ENDPOINT_VARIABLES: dict[str, str]`, carrying the variable
    name alone, and startup_lines() therefore needed its own list of which chains to
    print -- which it spelled as the literal `("SOL", "XRP")`. ICP was in the dict and
    not in the literal, so the chain on ten of Config.ALLOWED_PAIRS' thirty directions
    got NO LINE AT ALL in the banner whose entire job is to say which network each
    chain points at. Rule 8's two-lists-of-one-thing, and rule 14's "a chain silently
    missing from a banner is indistinguishable from a banner that forgot it" -- which
    that function's own docstring says, three lines above the literal.

    `variable` is the ONE value that decides whether an adapter can exist, which is
    what configuring_variable() returns.
    `decides_key` is where that value sits inside the chain's Config.RPC entry, so a
    reader can be told CONFIGURED or NOT from the same fact the registry uses.
    `endpoint_key` is what the banner PRINTS, and for ICP it is deliberately a
    different key: ICP_LEDGER_CANISTER_ID decides whether the adapter exists, and
    `network_url` is the thing that says mainnet or local -- the fact a network banner
    is for. Collapsing the two would make the banner print a canister id where a
    reader is looking for the word mainnet.
    """

    variable: str
    decides_key: str
    endpoint_key: str
    #: The variable an operator would export to fill `endpoint_key`. Usually the same as
    #: `variable`; for ICP it is not, and that difference is the whole reason this field
    #: exists -- see startup_lines()'s comment on the "(... unset here)" branch.
    endpoint_variable: str


#: The chains with no port convention, keyed by asset. ONE TABLE, read by both
#: configuring_variable() and startup_lines(); see UrlChain for the four characters of
#: duplication that cost ICP its banner line.
URL_CHAINS: dict[str, UrlChain] = {
    "SOL": UrlChain("SOL_RPC_URL", "url", "url", "SOL_RPC_URL"),
    "XRP": UrlChain("XRP_RPC_URL", "url", "url", "XRP_RPC_URL"),
    # ICP has no port and no endpoint URL of the SOL/XRP shape: the adapter reaches
    # the ledger by running dfx in a compose service (chains/icp.py's header has the
    # measurement that rules out every Python agent library). The canister id is what
    # decides whether an adapter can exist at all, so it is the primary name -- and
    # naming it here is what stops configuring_variable()'s <CHAIN>_RPC_PORT fallback
    # from reporting `ICP_RPC_PORT`, a variable nothing reads.
    # The fourth field differs from the first ONLY here, which is what it is for: the
    # canister id decides whether an adapter exists, and ICP_DFX_NETWORK_URL is what
    # names the replica -- supplied to the container by docker-compose.web.yml and
    # legitimately unset on the host.
    "ICP": UrlChain(
        "ICP_LEDGER_CANISTER_ID", "ledger_canister_id", "network_url", "ICP_DFX_NETWORK_URL"
    ),
}


def configuring_variable(chain: str) -> str:
    """The environment variable that makes this chain reachable. Never raises.

    Verified against config.py 2026-09-26 and re-verified 2026-10-03 rather than
    recalled: BTC/LTC/GRC read <CHAIN>_RPC_PORT, XRP reads XRP_RPC_URL and SOL
    reads SOL_RPC_URL, all inside Config.RPC.
    chains/registry.build_adapters() skips a chain whose value is falsy, which is
    what makes this the variable that decides whether an adapter EXISTS at all.

    THE LINE NUMBERS THAT USED TO BE IN THIS PARAGRAPH WERE ALL THREE WRONG, and
    they are gone rather than refreshed. It cited config.py:204 for GRC's port,
    :134 for XRP_RPC_URL and :192 for SOL_RPC_URL; measured 2026-10-03 the reads
    are at 390, 272 and 378, and line 204 is inside the ALLOWED_PAIRS comment
    block. A line number is a citation that rots on every edit above it and says
    nothing when it rots, so the test is the reference instead:
    tests/test_network_target.test_configuring_variable_matches_what_config_py_actually_reads()
    greps config.py's source for each returned name, which cannot go stale
    silently.

    THIS IS THE PRIMARY NAME ONLY, AND IT IS NOT THE WHOLE SET for a
    Bitcoin-derived chain. Since 2026-09-26 chains/registry.build_adapters() also
    requires <CHAIN>_RPC_USER and <CHAIN>_RPC_PASS -- an adapter built from an
    empty credential pair 401s on every call -- so a caller that prints only this
    name for BTC, LTC or GRC names one variable where three are needed.
    chains/registry.missing_settings() returns the complete list and
    chains/registry.why_unconfigured() is the sentence built from it; use those
    wherever an operator has to go and export something. This function exists for
    the ONE value that decides reachability, and the distinction is named here
    because the difference is two variables an operator would otherwise have to
    discover by retrying.

    The `<CHAIN>_RPC_PORT` fallback for an unknown chain matches what describe()
    and require_configured() already do two functions below, so a chain added to
    ALLOWED_PAIRS before it is added here produces a plausible name rather than a
    KeyError -- which is the failure this whole function exists to stop being
    printed at a person.
    """
    known = CHAIN_PORTS.get(chain)
    if known:
        return known.port_variable
    url_chain = URL_CHAINS.get(chain)
    if url_chain:
        return url_chain.variable
    return f"{chain}_RPC_PORT"


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


def may_read_a_wallet(chain: str, port: int) -> tuple[bool, str]:
    """May a tool OPEN A SOCKET to this Bitcoin-derived wallet? (connect?, the sentence).

    LOOKING IS THE HAZARD, which is the whole reason this is a decision taken
    BEFORE a socket opens rather than a label applied after. `getbalance` or
    `getwalletinfo` against Gridcoin port 15715 prints the operator's real staking
    balance into whatever terminal, transcript or pasted block the output lands in.
    That happened on 2026-09-25 -- 157,797 GRC into a chat log -- and the fix then
    was this shape: classify the port first, and refuse. Not "connect and warn".

    MOVED HERE 2026-10-03 FROM swap_readiness.chain_precheck(), WHICH NOW WRAPS IT,
    because a second tool needed the identical refusal and rule 10 forbids a module
    importing a root entry point -- so the only two options were an upward import
    or a second copy of a refusal to read a real wallet. The sentences are moved
    verbatim, which is why tests/test_swap_readiness.py's assertions on them are
    unchanged: the wrapper adds the PASS/FAIL column that is that file's own and
    this layer has no opinion about.

    wallet_custody.py is the second caller. It reads `getwalletinfo` and
    `validateaddress`, so it is subject to exactly the same hazard, and it refuses
    through this function rather than deciding for itself what a safe port is.

    (connect, detail) RATHER THAN A BOOLEAN, because every refusing branch has a
    DIFFERENT remedy and a caller that printed its own sentence for a bare False
    would be a second place in this tree spelling the mainnet refusal.
    """
    known = CHAIN_PORTS.get(chain)
    verdict = classify(chain, port)
    variable = known.port_variable if known else f"{chain}_RPC_PORT"
    hint = known.test_hint if known else f"no {chain} port convention is in CHAIN_PORTS"
    if verdict == "UNCONFIGURED":
        return False, f"(unconfigured) -- set {variable} to the test chain ({hint})"
    if verdict == "MAINNET":
        return False, (
            f"port {port} is MAINNET and this did NOT connect. A preflight will not read a real "
            f"wallet, because reading it means printing the balance. Set {variable} to a "
            f"test chain ({hint})"
        )
    if verdict == "UNRECOGNIZED":
        return False, (
            f"port {port} is not a {chain} port this tree knows, so which chain it is was NOT "
            f"established -- and an unknown port may be a mainnet daemon on a custom -rpcport. "
            f"Refusing to connect rather than guessing"
        )
    # TEST IS NOW THE EXPLICIT BRANCH AND UNRECOGNIZED IS THE FALLTHROUGH, which is the same
    # answer for every reachable input and a readable one for the input that is not. classify()
    # returns UNRECOGNIZED whenever `CHAIN_PORTS.get(chain)` is None (its own `if known is None`
    # branch), so "TEST with no row" cannot happen -- but that argument lived in another
    # function, and as a bare trailing `return` this line read `known.mainnet_port` off a value
    # the three lines above had each guarded with `if known`. If it ever did happen, the
    # sentence below is the true one: which chain this is was not established.
    if verdict == "TEST" and known is not None:
        return True, f"port {port} is a test chain (mainnet is {known.mainnet_port})"
    return False, (
        f"port {port} is not a {chain} port this tree knows, so which chain it is was NOT "
        f"established -- and an unknown port may be a mainnet daemon on a custom -rpcport. "
        f"Refusing to connect rather than guessing"
    )


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
    # `and known is not None` for the reason given in may_read_a_wallet() above: classify()
    # never answers TEST without a CHAIN_PORTS row, and the UNRECOGNIZED line below already
    # spells the no-row case, so this makes the guard visible without inventing a branch.
    if verdict == "TEST" and known is not None:
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
    #
    # ITERATED FROM URL_CHAINS AND NOT FROM A LITERAL. This loop read ("SOL", "XRP")
    # until 2026-10-12 while the table beside it held three chains, so ICP -- ten of
    # thirty allowed directions -- was absent from every startup banner this tree has
    # ever printed. See UrlChain's docstring; the fix is that there is one list.
    for chain, url_chain in sorted(URL_CHAINS.items()):
        entry = rpc.get(chain) or {}
        # CONFIGURED IS DECIDED BY THE VALUE THE REGISTRY DECIDES ON, and the string
        # PRINTED can be a different key -- for ICP the canister id decides and the
        # replica URL is what names the network. A chain configured with no endpoint
        # to print says so rather than reading as unconfigured, because "no adapter"
        # and "an adapter pointed somewhere this banner cannot name" are different
        # problems and a reader acts differently on each.
        if not entry.get(url_chain.decides_key):
            lines.append(f"{chain:4} NOT CONFIGURED  <- set {url_chain.variable} to reach this chain")
            continue
        # A CONFIGURED CHAIN WITH NO ENDPOINT TO PRINT NAMES THE VARIABLE, not the dict
        # key. Measured on the operator's host 2026-10-12, this line read
        #
        #     ICP  (network_url unset)      <- configured; chain NOT classified here
        #
        # which sends a reader to grep for `network_url`: an internal key of
        # Config.RPC["ICP"], not something anyone can export. The variable is
        # ICP_DFX_NETWORK_URL, and on the HOST it is genuinely unset by design --
        # docker-compose.web.yml:387 supplies `${ICP_DFX_NETWORK_URL:-http://icp-replica
        # :4943}` to the container, so the container has a replica and a host-side report
        # has nothing to print. Rule 14's "echo the parameters that decide the answer"
        # means echoing the name the reader can act on, and saying that an absence here
        # is expected rather than leaving them to find out.
        target = entry.get(url_chain.endpoint_key) or f"({url_chain.endpoint_variable} unset here)"
        suffix = " (network confirmed from the server at send time)" if chain == "XRP" else ""
        lines.append(f"{chain:4} {target:24} <- configured; chain NOT classified here{suffix}")
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


# SOLANA IS IDENTIFIED BY ITS GENESIS HASH, NOT BY A PORT, and that is why it has
# its own table in a module otherwise keyed on ports.
#
# Everything above decides a network from a port number, which is a CONVENTION --
# a mainnet daemon on a custom -rpcport lands in UNRECOGNIZED, and classify()'s
# docstring says so rather than claiming safety. Solana has no equivalent
# convention: one URL scheme serves every cluster, and the hostname is a label
# anybody can point anywhere. What a cluster cannot lie about is its genesis hash,
# so the identification is exact here where the port version is conventional.
#
# MOVED HERE 2026-10-01 FROM solana_chain_check.py, where it was a module-level
# dict in a root entry point. That made it unreachable by anything that is not
# that tool without importing a root entry point from another one, which is rule
# 10's layering inverted -- a file must not import a file. swap_readiness.py needed
# the same table, and the alternatives were to import a root tool or to write the
# three hashes out a second time; the second is rule 8's defect exactly, and it
# would have been a quiet one, because two copies of a hash table agree until a
# cluster is added to one of them.
#
# chains/solana_rpc_map.py's comment named "solana_chain_check.GENESIS_HASHES" as
# where to look, and was corrected in the same commit.
GENESIS_HASHES: dict[str, str] = {
    "5eykt4UsFv8P8NJdTREpY1vzqKqZKvdpKuc147dw2N9d": "MAINNET-BETA  <- REAL MONEY",
    "EtWTRABZaYq6iMfeYKouRu166VU2xqa1wcaWoxPkrZBG": "DEVNET",
    "4uhcVJyU9pJkvQyS88uRDiswHXSCkY3zQawwpjk2NsNY": "TESTNET",
}

#: What an unrecognized genesis hash is called. A local validator generates its
#: own genesis, so this is the EXPECTED answer for solana-test-validator and is
#: not a failure -- but it is also what a private fork of mainnet would read as,
#: which is why it is never rendered as "not mainnet" (the same judgment
#: classify() makes for UNRECOGNIZED).
UNRECOGNIZED_CLUSTER = (
    "UNRECOGNIZED -- a local validator has its own genesis, so this is expected for "
    "solana-test-validator"
)


def solana_cluster(genesis: str) -> str:
    """Which Solana cluster a genesis hash identifies. Exact, not inferred.

    One function rather than two `.get(..., default)` call sites, so the default
    sentence cannot drift between them -- it had no second caller until
    swap_readiness.py wanted one, which is the moment a repeated default becomes
    two spellings of one answer.
    """
    return GENESIS_HASHES.get(genesis, UNRECOGNIZED_CLUSTER)
