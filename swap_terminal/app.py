"""Flask entry point for the brokered swap terminal.

Role: entry point (HTTP API for quotes, swaps and rates)
Reads: swap_terminal.db (quotes, swaps, deposit_events, payouts,
       address_proof_challenges), BTC/LTC/GRC wallet RPC (getnewaddress,
       validateaddress, verifymessage), CoinGecko simple/price
Writes: swap_terminal.db (quotes, swaps, swap_audit_log,
       address_proof_challenges)
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

from chains.daemon_capabilities import BITCOIN_FAMILY, wallet_path_warning
from chains.registry import build_adapters
from config import Config
from db import close_db, init_db
from engineering_notation import coin_amount_text
from flask import Flask
from log_setup import configure_logging
from microfortnights import format_duration
from network_target import CHAIN_PORTS, mainnet_chains, startup_lines
from routes.admin import bp as admin_bp
from routes.atm import bp as atm_bp
from routes.grc_login import bp as grc_login_bp
from routes.health import bp as health_bp
from routes.kill_switch import bp as kill_switch_bp
from routes.quotes import bp as quotes_bp
from routes.rates import bp as rates_bp
from routes.swaps import bp as swaps_bp
from routes.ui import bp as ui_bp

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
        # NAMED SEPARATELY FROM THE LINE ABOVE, on purpose (added 2026-09-26
        # with routes/admin.py). "Nothing authenticates a caller" is true of
        # every route and reads as a statement about the swap API; /admin is
        # a different exposure in kind, because it lists every swap id, every
        # deposit and payout address, the database path and the hot-wallet
        # balances on one page -- and services/helpers.new_id() records that a
        # swap id is "the only thing standing between a stranger and
        # GET /api/swaps/<id>". An operator who binds an interface has to read
        # that as its own sentence, not infer it from a general one.
        warnings.append(
            f"/admin and /api/admin/* are reachable from {host} with NO authentication: they list every "
            "swap id, deposit address, payout address and hot-wallet balance this database holds. They are "
            "read-only -- there is no POST on that surface -- but they are not private."
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
    # A WALLET PATH A DAEMON CANNOT SERVE, said at startup or not at all.
    #
    # chains/base.RPCAdapter.url appends /wallet/<name> for ANY chain whose
    # wallet is configured, and the multi-wallet HTTP endpoint arrived in Bitcoin
    # Core 0.17 -- which chains/base.py:339 already records Gridcoin as predating.
    # So GRC_RPC_WALLET being set would send every GRC call, payouts included, to
    # a route that does not exist, and it would read as a transport failure
    # rather than as a configuration error.
    #
    # REPORTED, NOT REFUSED (rule 16). This is the order path, and a session that
    # cannot see the operator's .env must not decide their live configuration
    # should stop working. WARNING rather than INFO for the same reason the
    # mainnet line below is: it is visible at the default level, and it is the
    # thing you must have meant to do.
    for chain in BITCOIN_FAMILY:
        trouble = wallet_path_warning(chain, str((app.config["RPC"].get(chain) or {}).get("wallet", "")))
        if trouble:
            logger.warning("chain config  %s", trouble)

    on_mainnet = mainnet_chains(app.config["RPC"])
    if on_mainnet:
        logger.warning(
            "MAINNET RPC configured for %s -- real money. Set %s to a test port if that was "
            "not intended.",
            ", ".join(on_mainnet),
            ", ".join(CHAIN_PORTS[c].port_variable for c in on_mainnet),
        )

    # THE ONE REGISTRATION OF THE AMOUNT FORMAT (engineering_notation.py).
    #
    # A FILTER RATHER THAN A FORMATTED FIELD ON THE DICT, because the two
    # templates that need it render the figure inside a three-branch `{% if %}`
    # whose other branches are sentences -- `estimate.send_text` would have to
    # exist for those branches too, and a sentence named `_text` alongside a
    # number named `_text` is how the next reader prints one where the other
    # belongs. A filter applies at the point of rendering and nowhere else.
    #
    # NOT a float-to-string convenience: engineering_notation() is the house
    # answer to a measured defect (the ATM review printed `8.061e-05 BTC` to a
    # customer about to send it), and registering it here is what stops the
    # fifth template spelling `{{ x }} {{ asset }}` by hand. Measured 2026-10-09:
    # 19 amount renderings in templates/ already use `'%.8f'|format`, which
    # cannot produce an exponent, and exactly 4 interpolated a raw float. Those 4
    # were the whole defect.
    app.jinja_env.filters["coin"] = coin_amount_text

    app.teardown_appcontext(close_db)
    # routes/ui.py owns the per-swap customer pages -- `/swap-lookup`,
    # `/swap/<id>` and `/swap/<id>/fragment`, which is every rule on this
    # blueprint and nothing else -- and routes/admin.py owns the read-only
    # operator surface (`/admin`). They are separate blueprints rather than one
    # "pages" module because the two have different audiences and different
    # exposure: see routes/admin.py's header for the authentication question,
    # which is named rather than solved.
    #
    # THIS SAID ui.py OWNS "(`/`, `/swap/<id>`)" UNTIL 2026-11-21, and `/` has
    # been routes/atm.py's since 9eff792 (2026-10-07). Corrected in the same pass
    # as the comment below it, which was stale from the same commit and in the
    # same direction: both were written while ui.py still had an index() at `/`,
    # and neither was revisited when that view was deleted. Two wrong sentences
    # four lines apart, about the same move, is what rule 16 means by a wrong
    # comment being a bug -- a reader looking for who serves the landing page
    # would have been sent to the wrong file by either one.
    app.register_blueprint(ui_bp)
    # THE ATM FLOW, AND IT IS AT `/` -- the customer's landing page, GET and POST
    # (routes/atm.py::start and ::advance).
    #
    # THIS COMMENT SAID SOMETHING ELSE, AND EVERY CLAUSE OF IT WAS WRONG.
    # Corrected 2026-11-21; kept as a correction rather than overwritten because
    # the drift is the point (rule 1). It read:
    #
    #     THE ATM FLOW, at /atm. It will become / in its own commit (143 tests
    #     across five files pin the current customer page, and one diff that both
    #     adds this and deletes that evidence leaves no way to tell a wizard bug
    #     from a test deletion). Registered after ui_bp so url_for('ui.index')
    #     keeps resolving.
    #
    # `/atm` has been registered nowhere since 9eff792 (2026-10-07), the commit
    # that moved this flow to `/` and deleted routes/ui.index() and
    # templates/index.html with it -- hours after b4eb3e67 (2026-10-07) wrote the
    # sentence above. So the split-commit plan it describes in the future tense is
    # spent, and the half still worth knowing -- why the flow staged at `/atm`
    # first, and which of those 143 tests moved rather than died -- is in
    # routes/atm.py's own header, where one copy of it lives instead of two
    # (rule 8).
    #
    # THE REASON GIVEN FOR THE ORDER IS GONE, AND IT NEVER HELD IN THE FIRST
    # PLACE. Three measurements, taken 2026-11-21 against this tree:
    #
    #   `ui.index` DOES NOT EXIST. routes/ui.py registers exactly three rules --
    #   ui.swap_lookup (/swap-lookup), ui.swap_page (/swap/<swap_id>) and
    #   ui.swap_fragment (/swap/<swap_id>/fragment) -- and url_for('ui.index')
    #   raises werkzeug.routing.BuildError ("Did you mean 'ui.swap_page'
    #   instead?"). Nothing asks for it either: every occurrence of the string
    #   `ui.index` in the tree is prose recording the deletion -- this comment,
    #   routes/atm.py and stack_authority.py -- and not one of them is a
    #   url_for() or an endpoint reference. Checked by grepping the NAME across
    #   the whole tree including templates/, not the import graph (rule 2); the
    #   count is deliberately not stated here, because this block is one of the
    #   hits and would falsify its own number (rule 3).
    #
    #   THE ORDER DECIDES NOTHING TODAY. Registering atm_bp BEFORE ui_bp instead
    #   of after yields a url map identical to the one create_app() builds here:
    #   23 rules, the same path, endpoint and method set on every one of them,
    #   the same url_for() output for each, and the same endpoint for every path
    #   tried (/, /swap-lookup, /swap/<id>, /swap/<id>/fragment,
    #   /swap/<id>/address-proof, /admin, /admin/controls, /api/health,
    #   /static/<file>). That is because no rule string in this application is
    #   shared by two blueprints -- the only three registered twice are the
    #   GET/POST pairs inside ONE blueprint each (`/` is atm.start and
    #   atm.advance, `/swap/<id>/address-proof` is grc_login's two, and
    #   `/admin/controls` is kill_switch's two) -- and because no blueprint here
    #   declares a url_prefix, a template_folder, a static_folder, or any
    #   app-level hook (before_app_request, app_errorhandler,
    #   app_context_processor), which are the other things registration order
    #   could reach.
    #
    #   AND THE MECHANISM WAS NAMED WRONG EVEN ON 2026-10-07. url_for() builds
    #   from the endpoint's OWN rule, so it cannot stop resolving because another
    #   blueprint registered first. Measured in both orders with the rules the
    #   tree actually had that day -- ui at `/` as ui.index, atm at `/atm` -- GET
    #   / reached ui.index and url_for('ui.index') returned '/' either way,
    #   because `/` and `/atm` never collided at all. What registration order can
    #   break is DISPATCH, and only between two rules carrying the same path and
    #   method: with both blueprints forced onto `/`, url_for('ui.index') STILL
    #   returned '/' and it was GET / that changed hands to atm.start.
    #
    # THE LINE THEREFORE STAYS EXACTLY WHERE IT IS. This is a comment fix, not a
    # reordering: there is nothing left to preserve by the order and nothing to
    # win by changing it, and app.py is the application factory, where a reader is
    # entitled to assume the sequence was deliberate until told otherwise. What a
    # future reader needs to know is the conditional: if a second rule for `/` is
    # ever added to another blueprint, THAT is the moment this order starts
    # deciding which view answers the customer's landing page -- and the fix then
    # is one rule for one path (rule 2), not a chosen registration order holding
    # a collision at bay silently.
    app.register_blueprint(atm_bp)
    app.register_blueprint(admin_bp)
    # routes/grc_login.py owns the address-proof panel: a customer proves they
    # hold the key to a GRC address by signing a challenge in their OWN wallet.
    # Its own blueprint rather than part of ui_bp because it is the one customer
    # surface that writes a row on a GET (it issues the challenge) and the one
    # that reaches a chain daemon on a POST -- both named in that file's header.
    # It can move no funds, and nothing on the payout path reads what it records.
    app.register_blueprint(grc_login_bp)
    # routes/kill_switch.py owns /admin/controls, and it is the only blueprint in
    # this application whose POST can SIGNAL OR SPAWN A PROCESS (/api/quotes,
    # /api/swaps and /swap/<id>/address-proof are the other POSTs, and none of them
    # spawns anything). It starts and stops the three
    # supervised workers, one of which broadcasts payouts, so a start is gated on
    # reading the spawn warning and both verbs refuse unless this server is
    # provably bound to loopback. Its own blueprint rather than part of admin_bp
    # because routes/admin.py's header claims every route there is a GET and
    # tests/test_web_surfaces.py asserts that over the real url map -- a claim
    # worth keeping true rather than loosening.
    app.register_blueprint(kill_switch_bp)
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
        f"  allowed pairs   {pairs or '(none)'}  <- Config.ALLOWED_PAIRS; the swap form is rendered from this list",
        f"  operator page   http://{host}:{port}/admin  <- READ-ONLY (no POST on that surface) and UNAUTHENTICATED",
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

    # BEFORE ANYTHING ELSE RUNS. Without it the root logger has no handler and sits
    # at WARNING, so every logger.info() in the request path is discarded and
    # routes/swaps.py's warning -- the one that records WHY a swap was refused, added
    # because a bare 400 told the operator nothing -- reaches logging.lastResort with
    # no timestamp and no logger name. Measured 2026-09-26; see log_setup.py for the
    # reading and for why the level is INFO and not DEBUG.
    #
    # In the __main__ block and not at module scope, because importing app.py must
    # not reconfigure the logging of whatever imported it (rule 12: import-time side
    # effects). Under gunicorn this block does not run and gunicorn installs its own
    # handlers -- UNVERIFIED here, since gunicorn is not installed in this
    # environment, so it is stated as what is expected rather than as measured.
    configure_logging()

    started = time.monotonic()
    run_host = bind_host()
    run_port = bind_port()
    run_debug = debug_enabled()
    print(
        startup_banner(run_host, run_port, run_debug, Config.DB_PATH, time.monotonic() - started),
        flush=True,
    )
    app.run(debug=run_debug, host=run_host, port=run_port)
