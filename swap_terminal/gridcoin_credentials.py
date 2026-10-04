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
    GRC_RPC_PASS            config.py:132, modules/atomic_grc_client.py:539
                            (atomic_swap_gui.py:60 was a fourth reader of this
                            spelling until that file was deleted on
                            2026-09-26; the spelling problem it illustrates is
                            unchanged)

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

TWO ENDPOINTS LIVE HERE AS OF 2026-10-03, and the second one is at the bottom of
the file under its own banner: the operator's OWN Gridcoin daemon, named by
GRC_OPERATOR_RPC_* and resolved by operator_endpoint(). It is in this module
rather than in a new one because this module already owns the question "what is
the Gridcoin RPC credential called, and where is it read from" -- a second
mechanism for the same question is rule 8's bug with a delay on it, and the
delay on the first one had already expired when this file was written. The
measurement that forced a second endpoint (Gridcoin has no multiwallet RPCs, so
there is no `walletname` to ask for) is written out in full down there.
"""

import os
from typing import NamedTuple

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


# ---------------------------------------------------------------------------
# THE SECOND GRIDCOIN ENDPOINT: THE OPERATOR'S OWN DAEMON
# ---------------------------------------------------------------------------
#
# WHY A SECOND ENDPOINT EXISTS AT ALL, MEASURED 2026-10-03 ON THE OPERATOR'S
# GRIDCOIN v5.5.1.0 TESTNET DAEMON.
#
# wallet_custody.py establishes custody separation on BTC and LTC by asking the
# desk's endpoint which wallet it serves -- getwalletinfo().walletname, plus
# listwallets for whether a bare CLI call would reach the same wallet. Six of its
# seven checks answer. The seventh could not, and the reason is now settled rather
# than suspected:
#
#     (this cited a help-text grep; a control run refuted it on 2026-10-04 --
#      `help` writes nothing to stdout on that build, so the grep was empty for
#      listunspent too. See services/custody_separation.GRIDCOIN_NO_WALLETNAME_EVIDENCE)
#       -> (none)
#
# Gridcoin has ONE wallet per datadir. There is no `-rpcwallet`, no
# `/wallet/<name>` endpoint, and no `walletname` field, so
# services/custody_separation.script_chain_verdict() answers
#
#     NOT ESTABLISHED  GRC wallet: getwalletinfo answered with no `walletname`
#                      field, so which wallet this endpoint serves was not
#                      established.
#
# and no amount of configuration can make that question answerable. The BTC/LTC
# approach is not merely unimplemented on GRC; it is unavailable.
#
# SO THE QUESTION IS ASKED FROM THE OTHER DIRECTION. With no name to ask for,
# separation can still be PROVEN by asking the daemon that holds the OPERATOR's
# coins whether the DESK's deposit address is `ismine`. A `false` from that daemon
# is direct behavioral evidence that the desk's wallet is not the operator's -- it
# is an observation about a key, not a reading of a config value. An `ismine: true`
# from both endpoints is proof of the opposite: one wallet, or a wallet.dat that
# was copied, which is the hazard docs/hot_wallet_separation_runbook.md now states
# first and loudest.
#
# FOUR VARIABLES, AND WHY THESE NAMES.
#
#   - The shape is config.py's, exactly: it builds Config.RPC["GRC"] from
#     GRC_RPC_USER / GRC_RPC_PASS / GRC_RPC_HOST / GRC_RPC_PORT (config.py:516-519),
#     a coherent per-chain vocabulary this module's own docstring declines to fold
#     in. These are the same four fields with OPERATOR inserted to name WHOSE
#     daemon it is, so an operator who can read one line of config.py can read
#     these without being told.
#   - `_PASS` and not `_PASSWORD`, matching GRC_RPC_PASS. This module exists
#     because one Gridcoin password already had four spellings; a fifth would be
#     this file's own subject repeated.
#   - ASCII only, and no micro sign anywhere near them (rule 6's boundary): a
#     shell has to export these.
#
# AND THE RULE THAT IS ABSOLUTE: THE PASSWORD IS READ FROM THE ENVIRONMENT AND
# FROM NOWHERE ELSE. Not from gridcoinresearch.conf, not from any .conf on disk,
# not from a .env. A diagnostic that parses an rpcpassword out of a conf file has
# that secret in its process, in its tracebacks, and one careless print away from
# a pasted report -- and this tree has already published a live
# GRIDCOIN_RPC_PASSWORD once, through a .env.bak that reached GitHub (CLAUDE.md
# rule 2). Nothing below ever returns the password in a message; the refusals name
# only variable NAMES.
#
# NO FALLBACK TO THE DESK'S CREDENTIALS, AND THAT IS THE LOAD-BEARING HALF. If an
# unset GRC_OPERATOR_RPC_PORT silently fell back to GRC_RPC_PORT, the tool would
# ask the DESK daemon whether it owns the desk's own address, get the `ismine:
# true` it always gets, and report "NOT SEPARATED, one wallet serves both" --
# about one daemon compared with itself. That is a guaranteed-wrong answer wearing
# the register of a measurement (rule 17), produced by convenience. So an unset
# variable is a REFUSAL that names what was not done.

#: The operator's own daemon, in config.py's spelling with OPERATOR inserted.
#: Named as constants rather than as string literals at the read sites so the
#: refusal sentence, the report header and the runbook cannot drift apart.
OPERATOR_HOST_VARIABLE = "GRC_OPERATOR_RPC_HOST"
OPERATOR_PORT_VARIABLE = "GRC_OPERATOR_RPC_PORT"
OPERATOR_USER_VARIABLE = "GRC_OPERATOR_RPC_USER"
# CREDENTIAL AND NOT PASSWORD IN THE CONSTANT'S OWN NAME, deliberately, and the
# reason is both halves of rule 19. This constant holds the NAME of an environment
# variable; it has never held a secret and cannot. Spelled OPERATOR_PASSWORD_VARIABLE
# it tripped ruff S105 ("possible hardcoded password assigned to ..."), and the
# forbidden move would have been a `noqa` asserting the checker is wrong. The
# checker was reading the name it was given: a constant called ...PASSWORD... that
# is assigned a string literal is exactly the shape of a hardcoded credential. The
# cause was the misleading name, so the name is what changed -- and the VALUE still
# ends in _PASS because that is what config.py:517 calls the field (GRC_RPC_PASS)
# and a fifth spelling of this one password is this module's whole subject.
OPERATOR_CREDENTIAL_VARIABLE = "GRC_OPERATOR_RPC_PASS"

#: The ones with no default, in the order a reader should export them. HOST is
#: absent on purpose: 127.0.0.1 is the only value that has ever been right on this
#: host, config.py defaults GRC_RPC_HOST the same way, and a second daemon on
#: another machine is a different conversation than a second datadir on this one.
OPERATOR_REQUIRED_VARIABLES = (
    OPERATOR_PORT_VARIABLE,
    OPERATOR_USER_VARIABLE,
    OPERATOR_CREDENTIAL_VARIABLE,
)

#: Seconds, and `_SECONDS` in the name on purpose (rule 6: seconds stay where an
#: external API demands them, and requests' `timeout=` is one). 30 matches
#: config.py's GRC_RPC_TIMEOUT default, so the second endpoint waits exactly as
#: long as the first. Not an environment variable: four names are what the runbook
#: has to document and a fifth for a timeout nobody has needed to change would be
#: surface for its own sake.
OPERATOR_RPC_TIMEOUT_SECONDS = 30.0


class OperatorEndpoint(NamedTuple):
    """Where the operator's own Gridcoin daemon is, and what authenticates to it.

    `password` IS A SECRET AND NEVER APPEARS IN `label`. The label is what every
    report, header line and refusal prints; the password is read by
    chains/base.RPCAdapter.call() as an HTTP basic-auth tuple and goes nowhere
    else. Keeping them on one object with one of them excluded from the printable
    form is deliberate -- the alternative is a caller assembling its own display
    string, which is where a credential gets into a pasted block.
    """

    host: str
    port: int
    user: str
    password: str

    @property
    def label(self) -> str:
        """host:port, and NOTHING ELSE. Safe to print, paste and commit."""
        return f"{self.host}:{self.port}"


def operator_endpoint() -> tuple[OperatorEndpoint | None, str]:
    """The operator's own Gridcoin daemon from the environment, or (None, why not).

    (endpoint, refusal) RATHER THAN RAISING, because the caller is a report with
    four other chains to print and a missing second endpoint is one line's worth of
    "not established" rather than the end of the run. It is the same shape
    chains/base.AddressOwnership uses for the same reason: the reason is a return
    value, so no caller can mistake an unconfigured endpoint for an answer about
    custody.

    EVERY REFUSAL NAMES VARIABLE NAMES AND NEVER VALUES. A message that echoed
    what was set would echo GRC_OPERATOR_RPC_PASS on the one path where it is
    wrong, which is exactly when somebody pastes the output.

    AN EMPTY EXPORT COUNTS AS UNSET, for the reason _first_set() already records:
    `export GRC_OPERATOR_RPC_PASS=` is the shape a failed command substitution
    leaves behind -- a rotation script wrote an empty password into a live .env
    that way on 2026-09-25 -- and treating it as configured authenticates with
    nothing and reports a 401 that reads as "the daemon is broken".

    THE PORT IS NOT CLASSIFIED HERE. Whether it is a test chain is
    network_target.may_read_a_wallet()'s decision and the caller takes it BEFORE
    opening a socket; this function only establishes that a port was named and is
    an integer. Two decisions, two places, because the mainnet refusal already has
    exactly one home (rule 8) and a copy of it here would be the second.
    """
    missing = [name for name in OPERATOR_REQUIRED_VARIABLES if not (os.environ.get(name) or "").strip()]
    if missing:
        return None, (
            f"the operator's own Gridcoin daemon was not named, so no cross-daemon ownership question "
            f"was asked and no second socket was opened. Unset: {', '.join(missing)}. Export "
            f"{', '.join(OPERATOR_REQUIRED_VARIABLES)} (and optionally {OPERATOR_HOST_VARIABLE}, "
            f"default 127.0.0.1) in the shell you run this from. This tool reads the password from "
            f"the environment ONLY -- it never opens gridcoinresearch.conf, never reads a .env, and "
            f"never falls back to the desk's own GRC_RPC_* credentials, because asking the DESK "
            f"daemon whether it owns the desk's own address always answers yes and would report that "
            f"as 'one wallet serves both'"
        )
    raw_port = (os.environ.get(OPERATOR_PORT_VARIABLE) or "").strip()
    try:
        port = int(raw_port)
    except ValueError:
        return None, (
            f"{OPERATOR_PORT_VARIABLE} is set to a value that is not an integer, so no cross-daemon "
            f"ownership question was asked and no socket was opened. Set it to the rpcport of the "
            f"operator's own gridcoinresearchd -- 25715 is the one measured on this host, and "
            f"{MAINNET_RPC_PORT} is MAINNET and will be refused before any connection"
        )
    host = (os.environ.get(OPERATOR_HOST_VARIABLE) or "").strip() or "127.0.0.1"
    return OperatorEndpoint(
        host=host,
        port=port,
        user=(os.environ[OPERATOR_USER_VARIABLE] or "").strip(),
        password=os.environ[OPERATOR_CREDENTIAL_VARIABLE],
    ), ""
