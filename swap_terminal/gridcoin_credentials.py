"""One place that knows what the Gridcoin RPC credential is called.

Role: function layer (rule 10 -- resolution is a decision; this is the function
      that makes it, callable with a seeded environment)
Reads: the process environment only. Opens no file, no socket.
Writes: nothing
Can move funds: no. It RESOLVES the credential that authenticates calls which
      can -- so a wrong answer here presents as a broken wallet, never as a
      wrong payment.
Mainnet-safe: yes

WHY THIS EXISTS: FOUR NAMES FOR ONE SECRET

Measured 2026-09-25 by grepping every .py, .js, .mjs and .sh outside
node_modules. One Gridcoin RPC password is read under four different spellings:

    GRIDCOIN_RPC_PASSWORD   grc-sol-swap/abstergo_exchange/server.js:97
                            transactions.py:68
    RPC_PASS                server.js:97, as its own fallback
    GRIDCOIN_RPC_PASS       identity.py, chain_tx.sh
    GRC_RPC_PASS            config.py:132, modules/atomic_grc_client.py:539,
                            atomic_swap_gui.py:60

That is CLAUDE.md rule 8's "two copies of one rule is a bug with a delay on
it", at four copies, and the delay already expired. The operator's live `.env`
spells it GRIDCOIN_RPC_PASSWORD; identity.py and chain_tx.sh read
GRIDCOIN_RPC_PASS. Neither of those scripts could authenticate against the
configuration that actually exists on the host, and the symptom would have been
an HTTP 401 that reads as "the wallet is broken" rather than "you spelled the
variable differently over here".

It was found the way rule 8 says these are always found: somebody went looking.
Nothing failed, because nothing had run those two scripts since the divergence.

WHAT THIS DOES AND DELIBERATELY DOES NOT DO

It resolves, in a documented order, for the PYTHON consumers that ask it to.
It does NOT rename anything. server.js and the operator's .env keep the names
they have, because renaming an environment variable a running deployment reads
is a configuration change on the operator's machine, not a refactor -- the same
line rule 16 draws, and the same trade rule 12 refuses for a tree-wide lint
sweep.

So this is the survivor that owns the CONCEPT while the spellings are migrated
one consumer at a time. When every consumer resolves through here, the extra
names can be retired in one change that the operator schedules.

GRC_RPC_PASS IS NOT IN THE LIST BELOW, AND THAT IS ON PURPOSE. It belongs to
the brokered Flask application, whose config.py builds a whole per-chain RPC
dict around the GRC_/BTC_/LTC_ prefix. That is a coherent vocabulary of its
own, not a stray spelling, and folding it in here would couple the standalone
scripts to the web application's configuration shape. Named so a reader finds
it rather than concluding it was missed.
"""

import os

from network_target import CHAIN_PORTS

# In resolution order, most specific first. The order is the decision: a host
# that has both set is telling you the more explicit one is the one it means.
GRIDCOIN_PASSWORD_VARIABLES = (
    "GRIDCOIN_RPC_PASSWORD",   # what the operator's .env and server.js use
    "GRIDCOIN_RPC_PASS",       # what identity.py and chain_tx.sh used to use
    "RPC_PASS",                # server.js's own fallback, kept for parity
)

GRIDCOIN_USER_VARIABLES = (
    "GRIDCOIN_RPC_USER",
    "RPC_USER",
)

GRIDCOIN_URL_VARIABLES = (
    "GRIDCOIN_RPC_URL",
    "RPC_URL",
)

# Gridcoin's test chain. Mainnet is 15715. The two differ by four characters
# and by whether the coins are real, which is why every message in this module
# that names one names the other.
#
# DERIVED, not respelled (rule 8). `MAINNET_RPC_PORT = 15715` used to be written
# out here AND in transactions.py, and the two files disagreed about what a test
# chain is: transactions.py knew {25715, 25779, 9876} while this file knew only
# 25779, so a wallet on 25715 was a recognized test chain to one and an unknown
# port to the other. network_target.CHAIN_PORTS is now the one table and both
# read from it. The names are kept because callers and tests use them.
TESTNET_RPC_PORT = 25779
MAINNET_RPC_PORT = CHAIN_PORTS["GRC"].mainnet_port
TESTNET_RPC_URL = f"http://127.0.0.1:{TESTNET_RPC_PORT}"

# Asserted at import rather than trusted: 25779 is the port the operator's
# running testnet daemon uses, and this module hands it out as a default URL. If
# the shared table ever stops calling it a test port, that is a contradiction
# worth failing on here rather than discovering by sending to the wrong chain.
assert TESTNET_RPC_PORT in CHAIN_PORTS["GRC"].test_ports, (  # noqa: S101 -- checked: an import-time invariant between two modules, not input validation; the alternative is a silent disagreement about which chain 25779 is
    f"network_target.CHAIN_PORTS no longer lists {TESTNET_RPC_PORT} as a Gridcoin test port"
)


class GridcoinCredentialsMissing(RuntimeError):
    """No Gridcoin RPC password is set under any known name.

    Its own type rather than a bare RuntimeError because this is not the daemon
    refusing -- it is this process declining before it opens a socket. An
    operator reading a traceback needs to tell "the wallet said no" from "you
    did not configure me", and an HTTP 401 does not distinguish them.
    """


def _first_set(names: tuple[str, ...], default: str = "") -> str:
    """The first of `names` that is set to a non-empty value.

    Empty counts as unset on purpose. An environment variable exported as the
    empty string is the shape a failed command substitution produces -- which
    is exactly how a rotation script wrote an empty password into a live .env
    on 2026-09-25 -- and treating that as "configured" would authenticate with
    nothing and report the daemon as broken.
    """
    for name in names:
        value = os.environ.get(name)
        if value:
            return value
    return default


def gridcoin_rpc_password() -> str:
    """The password, or "" if none of the known names carries one."""
    return _first_set(GRIDCOIN_PASSWORD_VARIABLES)


def gridcoin_rpc_user() -> str:
    return _first_set(GRIDCOIN_USER_VARIABLES, "gridcoinrpc")


def gridcoin_rpc_url() -> str:
    return _first_set(GRIDCOIN_URL_VARIABLES, TESTNET_RPC_URL)


def require_gridcoin_rpc_password(what_for: str) -> str:
    """The password, or a refusal naming what was not done and how to fix it.

    `what_for` is what the caller was about to do -- an RPC method name, a
    description -- so the message says what did NOT happen. That matters most
    on the broadcast path: the obvious reaction to an unexplained failure on a
    sendrawtransaction is to retry, and a retry after a broadcast that did go
    out pays twice.
    """
    password = gridcoin_rpc_password()
    if password:
        return password
    url = gridcoin_rpc_url()
    chain = "TEST chain" if str(TESTNET_RPC_PORT) in url else "chain"
    raise GridcoinCredentialsMissing(
        f"no Gridcoin RPC password is set, so {what_for} was NOT attempted and no socket was "
        f"opened. Set one of {', '.join(GRIDCOIN_PASSWORD_VARIABLES)} from your "
        f"gridcoinresearch.conf rpcpassword. This resolves to {url}, which is Gridcoin's {chain} "
        f"by default (port {TESTNET_RPC_PORT}; mainnet is {MAINNET_RPC_PORT}) -- check which one "
        f"you meant before setting it."
    )
