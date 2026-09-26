"""Flask entry point for the brokered swap terminal.

Role: entry point (HTTP API for quotes, swaps and rates)
Reads: swap_terminal.db (quotes, swaps, deposit_events, payouts), BTC/LTC/GRC
       wallet RPC (getnewaddress, validateaddress), CoinGecko simple/price
Writes: swap_terminal.db (quotes, swaps, swap_audit_log)
Can move funds: no -- this process never calls sendtoaddress. It DERIVES a
       deposit address (`getnewaddress`) and records intent; the payout is
       broadcast by workers/payout_worker.py. Note the wallet RPC credentials
       it holds COULD move funds if the process were subverted, which is the
       whole reason the debug console below is off by default.
Mainnet-safe: yes, with the defaults in this file. `SWAP_TERMINAL_DEBUG=1` or a
       non-loopback `SWAP_TERMINAL_HOST` makes it not mainnet-safe, and the
       startup banner says so out loud.

WHY THE RUN DEFAULTS ARE WHAT THEY ARE (CLAUDE.md rule 13, fixed 2026-09-24).

This file used to end with exactly one line:

    app.run(debug=True, host="0.0.0.0", port=5000)

Both keywords were defects, and the pair of them was the highest-severity line
in the repository:

  debug=True   turns on the Werkzeug interactive debugger. On any unhandled
               exception the traceback page offers a Python console, and that
               console executes as the process that holds BTC_RPC_PASS,
               LTC_RPC_PASS and GRC_RPC_PASS. It is remote code execution as
               the wallet owner, reachable by making any endpoint raise. The
               debugger PIN is not a security boundary; Werkzeug's own
               documentation says not to rely on it.
               debug=True ALSO starts the reloader, which forks a second
               process that nothing in this tree ever reaps: rule 13's unreaped
               spawn, arriving as a side effect of a keyword argument.
  host=0.0.0.0 binds every interface, so the above is reachable from the
               network rather than only from the machine itself.

The fix is not "remember not to deploy with debug on". Both now default to the
safe value and both are overridable by environment variable, so the unsafe
setting has to be typed on purpose by someone who can read what they typed.

Rule 14 governs the banner: it names the bind address, the port, the database
and whether the debugger is on, BEFORE serving, because a Flask process that is
listening and one that is wedged look identical from outside.
"""

import logging
import os
from collections.abc import Mapping

from chains.registry import build_adapters
from config import Config
from db import close_db, init_db
from flask import Flask
from microfortnights import format_duration
from network_target import CHAIN_PORTS, mainnet_chains, startup_lines
from routes.health import bp as health_bp
from routes.quotes import bp as quotes_bp
from routes.rates import bp as rates_bp
from routes.swaps import bp as swaps_bp

# The literal strings that turn a boolean environment variable on. Anything
# else -- unset, empty, "0", "no", "False", a typo -- is off. The asymmetry is
# deliberate: a misspelled value must fail CLOSED, because the thing being
# switched on is an interactive Python console on a host holding wallet keys.
_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})

# Loopback only. An operator who wants the API reachable from elsewhere sets
# SWAP_TERMINAL_HOST explicitly and sees it echoed in the startup banner.
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 5000

# Addresses that keep the socket on this machine. Used only to decide whether
# the banner prints an exposure warning.
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


def debug_enabled(env: Mapping[str, str] | None = None) -> bool:
    """Return True only if SWAP_TERMINAL_DEBUG is explicitly set to a true value.

    A decision small enough to call with seeded inputs (rule 10), which is why
    it takes the environment as an argument instead of reading os.environ
    inline. tests/test_app_run_defaults.py calls it with seeded mappings.
    """
    source = os.environ if env is None else env
    return source.get("SWAP_TERMINAL_DEBUG", "").strip().lower() in _TRUE_VALUES


def bind_host(env: Mapping[str, str] | None = None) -> str:
    """Return the interface to bind, defaulting to loopback."""
    source = os.environ if env is None else env
    host = source.get("SWAP_TERMINAL_HOST", "").strip()
    return host or DEFAULT_HOST


def bind_port(env: Mapping[str, str] | None = None) -> int:
    """Return the port to bind, defaulting to 5000.

    A non-numeric value raises rather than silently falling back to 5000:
    falling back would put the API on a different port than the operator
    believes it is on, which is the "did nothing that looks like did work"
    failure rule 14 is about.
    """
    source = os.environ if env is None else env
    raw = source.get("SWAP_TERMINAL_PORT", "").strip()
    return int(raw) if raw else DEFAULT_PORT


def exposure_warnings(host: str, debug: bool) -> list[str]:
    """Return human-readable warnings for an unsafe run configuration.

    An empty list means the configuration is the safe default. Returning a list
    rather than printing keeps the decision testable (rule 10) and lets the
    caller choose where it goes.
    """
    warnings = []
    if debug:
        warnings.append(
            "SWAP_TERMINAL_DEBUG is on: the Werkzeug console can execute Python as this "
            "process, which holds the wallet RPC credentials. Never with a funded wallet."
        )
    if host not in LOOPBACK_HOSTS:
        warnings.append(
            f"binding {host} exposes this API beyond the local machine; the default is "
            "127.0.0.1 and nothing in this app authenticates a caller."
        )
    return warnings


# Named the same way services/deposit_service.py and payout_service.py do, so a
# reader meets one convention rather than two.
logger = logging.getLogger(__name__)


def create_app() -> Flask:
    app = Flask(__name__, static_folder="static", template_folder="templates")
    for key in dir(Config):
        if key.isupper():
            app.config[key] = getattr(Config, key)
    # chains/registry.build_adapters() is shared with workers/common.py. It was
    # two six-line copies until 2026-09-25 (rule 8), and a fourth chain is
    # exactly where that drifts: a SOL adapter added here and not there gives
    # an HTTP process that hands out deposit addresses no watcher is polling.
    app.config["ADAPTERS"] = build_adapters(app.config["RPC"])

    # SAY WHICH CHAIN EACH ADAPTER IS ON, at startup, every time.
    #
    # Added 2026-09-26 after the operator reported "we're still pulling from grc
    # mainnet wallet and not the testnet wallet." They were right, and the reason
    # it went unnoticed for as long as it did is that nothing ever said. The
    # mainnet default was written in config.py's module header, which is not
    # somewhere anyone looks while a process boots, and refresh_wallet_inventory()
    # polling a mainnet wallet every cycle produced no error at all -- it worked.
    # Rule 14: announce before, not only after, and echo the parameters that
    # decide the answer, because pasted output is read a day later.
    #
    # Logged rather than printed so it lands in the gunicorn log where an
    # operator actually looks, and at WARNING for a mainnet chain so it is
    # visible at the default log level. A mainnet endpoint is not an error -- the
    # cost-basis tool needs one -- so it is not logged as one; it is logged as
    # the thing you must have meant to do.
    for line in startup_lines(app.config["RPC"]):
        logger.info("chain target  %s", line)
    on_mainnet = mainnet_chains(app.config["RPC"])
    if on_mainnet:
        logger.warning(
            "MAINNET RPC configured for %s -- real money. Set %s to a test port if that was "
            "not intended.",
            ", ".join(on_mainnet),
            ", ".join(CHAIN_PORTS[c].port_variable for c in on_mainnet),
        )

    app.teardown_appcontext(close_db)
    app.register_blueprint(health_bp)
    app.register_blueprint(quotes_bp)
    app.register_blueprint(rates_bp)
    app.register_blueprint(swaps_bp)

    with app.app_context():
        init_db()

    return app


def startup_banner(host: str, port: int, debug: bool, db_path: str, startup_seconds: float) -> str:
    """Build the block printed before the server starts serving (rule 14).

    Announce BEFORE, not only after: everything that decides the answer -- bind
    address, port, database, debugger state -- appears while the operator is
    still looking at it, and the pasted block is self-describing a day later.
    """
    pairs = ", ".join(sorted(f"{a}->{b}" for a, b in Config.ALLOWED_PAIRS))
    lines = [
        "swap_terminal API starting",
        f"  bind            {host}:{port}  <- 127.0.0.1 is the default; anything else is reachable off-box",
        f"  database        {db_path}",
        f"  debugger        {'ON' if debug else 'off'}  <- off is the default; ON means a remote Python console",
        f"  allowed pairs   {pairs or '(none)'}",
        f"  setup took      {format_duration(startup_seconds)}",
    ]
    warnings = exposure_warnings(host, debug)
    if warnings:
        lines.append("  WARNINGS:")
        lines.extend(f"    ! {warning}" for warning in warnings)
    else:
        lines.append("  warnings        (none)")
    return "\n".join(lines)


app = create_app()

if __name__ == "__main__":
    import time

    started = time.monotonic()
    run_host = bind_host()
    run_port = bind_port()
    run_debug = debug_enabled()
    print(
        startup_banner(run_host, run_port, run_debug, Config.DB_PATH, time.monotonic() - started),
        flush=True,
    )
    app.run(debug=run_debug, host=run_host, port=run_port)
