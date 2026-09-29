"""The decisions behind the operator panel: what may be run, and what the funding looks like.

Role: submodule (every decision the panel makes; operator_panel.py at the root is the server
      and holds none of its own)
Reads: the Gridcoin TESTNET daemon over JSON-RPC, and ST_ADAPTOR_FUNDING_SEED
Writes: nothing on disk. It SPAWNS the repository's own root entry points.
Can move funds: indirectly, and only by running an entry point from RUNNABLE below -- each of
      which is a file already in this tree that an operator can run by hand. Nothing here
      builds, signs or broadcasts a transaction of its own.
Mainnet-safe: NO. The panel refuses to start against a daemon that does not SAY it is on a test
      network, for the same reason grc_htlc_verify.py does.
Live-safe: yes. It starts and stops no daemon and never touches a wallet lock.

WHY A PANEL AT ALL, AND WHY NOT ON /admin.

2026-09-28 cost six runs to reach step 4 of one harness, and not one of the six failed for a
reason anybody could see from the terminal. The state that decided each of them -- which of the
operator's payments to the funding address were still unspent, what the daemon could and could
not be asked, which key owned which coin -- existed, was knowable, and was only ever printed
inside a run that had already committed to a choice. A panel is the place that state can be
LOOKED AT before a run starts.

It is NOT part of the Flask app's `/admin`. That blueprint's header says its GET-only shape is
structural, tests/test_admin_surface.py asserts it over the real URL map, and the app is
unauthenticated behind a loopback bind. Adding buttons that spend money to a page with that
posture would weaken a deliberate invariant that somebody wrote a test to keep. This is a
separate surface with its own, stricter guards, and it shares no route table with that one.

THE THREE GUARDS, and each is refusal rather than a warning:

  loopback only     the server refuses to bind anything but 127.0.0.1. There is no flag to
                    override it. The Flask app makes the same choice a DEFAULT; here it is not
                    a default, because this surface has buttons.
  testnet only      the daemon must SAY it is on a test network before the page renders once.
  an allowlist      the browser names a KEY, never a command. Nothing a request contains
                    reaches a shell, an argv, or a path.

THE SEED IS NEVER ACCEPTED FROM THE BROWSER and never rendered. It comes from the environment
of this process and is passed to children by inheritance. swap_terminal/CLAUDE.md: "Never move,
copy, or read back a key." A text box asking for it would put it in a POST body, a browser's
autofill, and the server's request log.
"""

from __future__ import annotations

import os
from typing import NamedTuple

from chains.base import RPCError
from regtest import adaptor_steps, daemons
from regtest.daemons import RegtestSetupError

#: What the panel is allowed to run, by KEY. The browser posts a key from this table and nothing
#: else -- no path, no argument, no flag. `subprocess` is called with a LIST and never a shell
#: string, so even a key that somehow arrived corrupted cannot become a command: it simply is
#: not in the dict.
#:
#: Every entry is a file an operator can already run by hand from the repository root, which is
#: the point: the panel is a faster way to do what they were doing, not a second implementation
#: of it (rule 8). `reclaim_funding.py` appears WITHOUT `--send` deliberately -- the dry run is
#: the safe one, and the thing that moves money stays a separate, explicit act that has to be
#: typed (rule 16).
RUNNABLE: dict[str, tuple[str, list[str], str]] = {
    "grc_htlc_verify": (
        "Does OP_CHECKLOCKTIMEVERIFY execute on Gridcoin? ~10 minutes; it waits 6 real blocks.",
        ["python3", "grc_htlc_verify.py"], "GRC",
    ),
    "grc_recover_search": (
        "Find and refund a contract a crashed run funded and never spent. Reads the chain, "
        "broadcasts only if it finds an UNSPENT one.",
        ["python3", "grc_htlc_verify.py", "--recover-search"], "GRC",
    ),
    "grc_adaptor_verify": (
        "All five protocol transactions on GRC testnet, including the adaptor join.",
        ["python3", "adaptor_regtest_verify.py", "--chain", "grc"], "GRC",
    ),
    "grc_reclaim_dry_run": (
        "What is recoverable at the funding address. Broadcasts NOTHING -- no --send.",
        ["python3", "reclaim_funding.py", "--to-wallet", "--chain", "grc"], "GRC",
    ),
    "btc_htlc_verify": (
        "The full HTLC suite on BITCOIN regtest -- it starts and stops its own daemon.",
        ["python3", "regtest_htlc_verify.py", "--chain", "btc"], "BTC",
    ),
    "ltc_htlc_verify": (
        "The full HTLC suite on LITECOIN regtest -- it starts and stops its own daemon.",
        ["python3", "regtest_htlc_verify.py", "--chain", "ltc"], "LTC",
    ),
    "xmr_chain_check": (
        "Read-only: what monero-wallet-rpc and monerod say about themselves.",
        ["python3", "monero_chain_check.py"], "XMR",
    ),
    "xrp_chain_check": (
        "Read-only: what the configured XRP Ledger endpoint says about itself.",
        ["python3", "xrp_chain_check.py"], "XRP",
    ),
    "sol_chain_check": (
        "Read-only: what the configured Solana endpoint says about itself.",
        ["python3", "solana_chain_check.py"], "SOL",
    ),
}


def runs_for(asset: str) -> list[dict]:
    """The runs THIS chain offers. The reason every tab used to look the same.

    THE DEFECT, reported by the operator 2026-09-28: "doesn't matter which tab you click, it's
    all GRC controls too. it doesn't switch per chain." Correct, and it was not a rendering
    bug -- RUNNABLE had no idea which chain each entry belonged to, so every tab rendered the
    whole table. A panel with six tabs and one set of controls is a panel with one tab and five
    decorations, and worse than that: the controls it showed under BTC would have spent GRC.

    THE ASSET IS PART OF THE ENTRY now rather than inferred from its name. Inferring it from a
    "grc_" prefix would work until the first entry that did not follow the convention, and the
    failure would be a button appearing under the wrong chain -- which is the thing being fixed.
    """
    return [{"key": key, "what": what}
            for key, (what, _argv, owner) in RUNNABLE.items() if owner == asset]


#: HOW EACH CHAIN'S TAB LOOKS. An accent for light and dark, a one-glyph mark, and the unit.
#:
#: WHY THEME AT ALL, on an operator tool nobody else sees. This panel now shows six chains that
#: are reached in three different ways and refuse in three different vocabularies, and every one
#: of them renders as the same grey table. The failure that costs something here is acting on
#: the wrong tab -- reading GRC's funding while looking at BTC's, or running a harness against a
#: chain you thought was the other one. Color and a mark are the cheapest possible defense
#: against that, and they work at a glance rather than after reading.
#:
#: THE COLORS ARE APPROXIMATED FROM PUBLIC BRAND USAGE and are NOT claimed to be official.
#: Bitcoin's orange and Monero's orange are the widely published ones; Litecoin's blue, XRP's
#: near-black and Solana's purple-to-green pair likewise; Gridcoin's green is the closest match
#: to its logo that is readable on both backgrounds. If any is wrong it is wrong as decoration,
#: and nothing here reads a color to decide anything.
#:
#: THE GLYPH IS THE CURRENCY LETTERFORM, not a logo: a character, set in the page's own font.
#: That keeps the page free of any image, which is what lets it render on a machine with no
#: route to the internet -- the same constraint that made the whole page inline.
#:
#: A DARK VARIANT IS CARRIED SEPARATELY because several brand colors fail against a dark
#: background: XRP's near-black is invisible on it, and Litecoin's blue is close behind. A theme
#: that is unreadable half the time is worse than none, since the reader stops looking at it and
#: the glance-level defense above is what was being bought.
CHAIN_THEME = {
    # GRIDCOIN IS PURPLE, and these two are its OWN values rather than a guess: they are the
    # gradient stops in src/qt/res/images/gridcoin.svg in the Gridcoin wallet's source, which
    # is MIT. It was green here until the operator said otherwise on 2026-09-28 -- a guess
    # presented as a theme, which is rule 17's failure wearing a colour.
    "GRC": {"accent": "#753eef", "dark": "#9d7bf5", "glyph": "G", "unit": "GRC"},
    "BTC": {"accent": "#f7931a", "dark": "#f7931a", "glyph": "\u20bf", "unit": "BTC"},
    "LTC": {"accent": "#345d9d", "dark": "#7aa7e0", "glyph": "\u0141", "unit": "LTC"},
    "XMR": {"accent": "#f26822", "dark": "#ff8a4c", "glyph": "\u0271", "unit": "XMR"},
    "XRP": {"accent": "#23292f", "dark": "#9fb3c8", "glyph": "\u2715", "unit": "XRP"},
    "SOL": {"accent": "#7b3fe4", "dark": "#c4a6ff", "glyph": "\u25ce", "unit": "SOL"},
}


def theme_for(asset: str) -> dict:
    """The tab's colors and glyph, with a readable fallback for a chain nobody has themed.

    A FALLBACK RATHER THAN A KeyError, because a chain added to CHAINS and not to CHAIN_THEME is
    a cosmetic omission and must not blank the page that would have told the operator about it.
    The fallback is deliberately plain: an unthemed chain LOOKS unthemed, which is the honest
    rendering of "nobody has decided what this one looks like" rather than a guess at it.
    """
    return CHAIN_THEME.get(asset, {"accent": "#6b6763", "dark": "#9b96a3", "glyph": "?",
                                   "unit": asset})


class ChainTab(NamedTuple):
    """One chain the panel shows, and what it is honestly able to say about it."""

    asset: str
    kind: str          # "operator" | "regtest" | "foreign"
    reachable_by_this_panel: bool
    note: str


#: EVERY CHAIN GETS A TAB, INCLUDING THE ONES THIS PANEL CANNOT REACH. A tab that is absent
#: reads as "that chain does not exist here"; a tab that says "no adapter in this panel" reads
#: as what it is. Rule 14's "(none) is a result" applied to a navigation bar -- and the question
#: "is XRP on?" is exactly the one routes/admin.py's pair table already refuses to answer by
#: omission.
#:
#: THE `kind` IS THE HONEST PART, because the three are reached in genuinely different ways and
#: pretending otherwise would be the panel's first lie:
#:
#:   operator   a daemon the OPERATOR runs and this panel only ever reads. GRC. The panel starts
#:              and stops nothing, so an unreachable one is reported, never launched.
#:   regtest    a daemon regtest_htlc_verify.py starts and stops for itself (daemons.py owns
#:              that spawn and its reaper, rule 13). The panel probes it if it happens to be up
#:              and says so if it is not -- it must NOT start one, because a process this panel
#:              spawned outside HarnessRunner would have no reaper here.
#:   foreign    no Bitcoin-style adapter in this panel. XMR, XRP and SOL are swapped by other
#:              entry points with their own clients; saying so beats an empty tab or a missing
#:              one.
#:
#: THE WORD WAS `none` IN THIS COMMENT AND IN ChainTab's TYPE LINE UNTIL 2026-09-28, AND NO TAB
#: HAS EVER CARRIED IT. CHAINS below has said "foreign" since the tabs were written, so both
#: the comment and the annotation described a fourth kind that does not exist -- and two live
#: branches were written against the name: `main()` skipped console construction on
#: `kind == "none"` (so it never skipped, and printed `KeyError: 'XMR'` three times at startup
#: on 2026-09-28), and the page set a nav dot on `d.kind === "none"` (so it never set grey, and
#: painted every foreign chain RED for a probe this panel never makes). A wrong comment is a
#: bug, rule 16, and this one was read as documentation by two pieces of code.
#: What each foreign chain's own entry point reads to find its endpoint. Checking these costs
#: nothing and cannot hang, and it answers the question a "no adapter" note left open: whether
#: pressing that chain's button has anywhere to connect to at all.
#:
#: THESE NAMES ARE READ FROM THE SAME PLACE THE CLIENTS READ THEM, swap_terminal/config.py, so
#: a rename there makes this stale rather than wrong -- and the tab prints the names it checked,
#: so a stale one is visible rather than silent.
FOREIGN_ENV = {
    "XMR": ("XMR_RPC_PORT",),
    "XRP": ("XRP_RPC_URL",),
    "SOL": ("SOL_RPC_URL",),
}

CHAINS = (
    ChainTab("GRC", "operator", True,
             "your own testnet daemon. This panel reads it and starts and stops nothing."),
    ChainTab("BTC", "regtest", True,
             "a regtest daemon that regtest_htlc_verify.py starts and stops for itself. This "
             "panel probes it and never launches one -- a process spawned outside "
             "HarnessRunner would have no reaper here (rule 13)."),
    ChainTab("LTC", "regtest", True,
             "a regtest daemon that regtest_htlc_verify.py starts and stops for itself. Same "
             "rule as BTC: probed, never launched."),
    ChainTab("XMR", "foreign", False,
             "monero-wallet-rpc speaks JSON-RPC 2.0 with a different shape from a "
             "Bitcoin-style daemon, so this panel does not probe it directly. Its own "
             "read-only check is the button below."),
    ChainTab("XRP", "foreign", False,
             "the XRP Ledger has its own JSON-RPC shape, reached through chains/xrp.py. This "
             "panel does not probe it directly; its own read-only check is the button below."),
    ChainTab("SOL", "foreign", False,
             "Solana is reached through chains/solana.py, and get_new_address() refuses there "
             "by design -- the custody choice is the operator's and is not decided in this "
             "tree. Its own read-only check is the button below."),
)


class SaysEachLineOnce:
    """A Console wrapper that prints a given line once and then stops repeating it.

    THE NOISE THIS EXISTS FOR, measured from the operator's own terminal 2026-09-28. Serving
    the GRC tab walks the funding payments, and every walk says one line per payment. Six tab
    loads and a few daemon switches later their terminal held FORTY copies of

        GRC: found the operator's funding at 3f2ad97a1f61f629...:1 worth 1.00000000 GRC

    in five-line bursts, with the BTC and LTC harness lines they were actually watching buried
    between them. Nothing was wrong and nothing was slow: the same five facts were restated
    every time a page was drawn.

    THIS IS NOT A SILENCE, WHICH RULE 14 FORBIDS. The first occurrence of every line is printed
    in full, and the first time anything is suppressed the wrapper says so, once, naming why --
    so an operator who notices the lines stopped is told they stopped on purpose and that the
    page, not this terminal, is where the funding table lives. A line that has never been said
    is never suppressed, so a NEW payment, a new scan, or a refusal still arrives immediately.

    WRAPPED RATHER THAN FILTERED INSIDE Console, because this rule is true only here. A harness
    run repeating a line is a harness making progress -- `scanned 100 block(s)` means something
    different each time even when the text matches -- and quieting that would be the defect
    this class is fixing, pointed the other way.
    """

    NOTICE = ("further exact repeats of the lines above are not printed again -- the panel "
              "re-reads them every time a page is drawn, and the page is where they are shown. "
              "Anything NEW still appears here immediately")

    def __init__(self, console) -> None:
        self._console = console
        self._said: set[str] = set()
        self._explained = False

    def should_say(self, line: str) -> bool:
        """THE decision, separated so it is asserted on without a terminal (rule 10)."""
        if line in self._said:
            return False
        self._said.add(line)
        return True

    def say(self, line: str) -> None:
        if self.should_say(line):
            self._console.say(line)
            return
        if not self._explained:
            self._explained = True
            self._console.say(self.NOTICE)

    def __getattr__(self, name):
        # EVERYTHING ELSE IS THE REAL CONSOLE'S. `check` tallies into counts the panel's startup
        # gate already uses, and a wrapper that swallowed one would change what that gate saw.
        return getattr(self._console, name)


def chain_state(tab: ChainTab, console) -> dict:
    """What one tab shows. NEVER RAISES -- an unreachable chain is a RESULT, not an outage.

    A PANEL THAT DIES ON ONE TAB IS WORSE THAN NO PANEL. Five of these six chains are expected
    to be unreachable on an ordinary day: the regtest daemons only exist while a harness is
    running, and three have no adapter at all. If any of that could raise, the page would be
    blank exactly when the operator opened it to find out why something was down.

    WHAT IT WILL NOT DO IS START ANYTHING. `daemons.py` owns every spawn in this tree and its
    reaper sits in the same file so neither can be edited without the other in view (rule 13).
    A panel that launched a bitcoind would be a spawn with no reaper on this side of the wall.

    THE NETWORK IS WHAT THE DAEMON SAYS, never what its port implies -- the same rule
    assert_test_network() enforces, applied to a read-only probe. A chain that will not say is
    reported as unknown rather than assumed to be a test one.
    """
    if tab.kind == "foreign":
        # A CHAIN THIS PANEL CANNOT SPEAK TO STILL HAS A CONFIGURED-OR-NOT ANSWER, and that is
        # the one an operator actually needs. "XMR does nothing" was the report on 2026-09-28,
        # and it was accurate: the tab said "no adapter" and stopped, which is a statement about
        # this panel dressed as a statement about the chain. What the operator could act on was
        # sitting one environment variable away.
        #
        # ASKED OF THE ENVIRONMENT, not of a network, so it costs nothing and cannot hang. It
        # says whether the entry point behind the button has somewhere to connect TO -- which
        # is a different question from whether that endpoint answers, and the tab says which
        # question it answered (rule 17).
        missing = [name for name in FOREIGN_ENV.get(tab.asset, ()) if not os.environ.get(name)]
        return {"asset": tab.asset, "kind": tab.kind, "note": tab.note, "reachable": False,
                "network": "", "error": "", "methods": [], "funding": None,
                "configured": not missing, "missing_env": missing,
                "env": list(FOREIGN_ENV.get(tab.asset, ()))}
    try:
        config = adaptor_steps.resolve_config(tab.asset)
    except Exception as error:  # noqa: BLE001 -- checked: the failure IS the tab's contents, named below
        return {"asset": tab.asset, "kind": tab.kind, "note": tab.note, "reachable": False,
                "network": "", "error": f"no connection parameters: {type(error).__name__}: {error}",
                "methods": [], "funding": None}
    run = adaptor_steps.Run(console=console, config=config, wallet="")
    try:
        height = adaptor_steps.current_height(run)
    except Exception as error:  # noqa: BLE001 -- checked: an unreachable daemon is the ordinary case and is reported as one
        return {"asset": tab.asset, "kind": tab.kind, "note": tab.note, "reachable": False,
                "network": "", "error": f"{type(error).__name__}: {error}",
                "endpoint": config.base_url, "methods": [], "funding": None}
    return {
        "asset": tab.asset, "kind": tab.kind, "note": tab.note, "reachable": True,
        "endpoint": config.base_url, "height": height, "network": network_the_daemon_says(run),
        "error": "", "methods": [{"method": m.method, "present": m.present, "matters": m.matters}
                                 for m in probe_methods(run)],
        "funding": None,
    }


def network_the_daemon_says(run: adaptor_steps.Run) -> str:
    """"testnet", "regtest", "MAINNET" or "unknown" -- from the daemon, never from the port.

    UNKNOWN IS ITS OWN ANSWER and is not folded into mainnet here, because this function only
    REPORTS. The gate that refuses -- assert_test_network() -- treats an absence of evidence as
    mainnet, which is right for a gate and wrong for a label: a tab reading "MAINNET" for a
    daemon that merely did not answer would send an operator looking for a problem they do not
    have, and would make the real thing unremarkable when it appeared.
    """
    node = run.node(wallet=False)
    for method, key in (("getblockchaininfo", "chain"), ("getblockchaininfo", "testnet"),
                        ("getinfo", "testnet")):
        try:
            answer = node.call(method)
        except (RPCError, OSError):
            continue
        if not isinstance(answer, dict) or key not in answer:
            continue
        value = answer[key]
        if key == "chain":
            return str(value) if value != "main" else "MAINNET"
        return "testnet" if value else "MAINNET"
    return "unknown"


#: THE ONLY RPC METHODS THIS PANEL WILL CALL. An allowlist, not a denylist, and the difference
#: is the whole safety argument: a denylist is a list of the ways to lose money that somebody
#: thought of, and it is wrong the first time a daemon adds a method.
#:
#: EVERY ONE OF THESE READS. None creates a transaction, none signs, none touches a lock, none
#: writes to a wallet. `getnewaddress` is NOT here even though it looks harmless -- it writes a
#: key into wallet.dat, which a staking-only wallet may refuse and which changes a file the
#: operator backs up.
#:
#: WHAT IS DELIBERATELY ABSENT AND WHY, because the omissions are the design:
#:
#:   sendtoaddress, sendrawtransaction, signrawtransaction
#:       these move or authorize money. The panel already has buttons that run HARNESSES, which
#:       spend -- but those are three named, reviewed entry points from RUNNABLE, not an
#:       arbitrary transaction an operator can compose in a browser with no confirmation step.
#:   walletpassphrase, walletlock, encryptwallet
#:       walletpassphrase takes the passphrase as an ARGUMENT. Serving a form that collects it
#:       would put it in a POST body, the browser's autofill, and this process's memory --
#:       against swap_terminal/CLAUDE.md's "never move, copy, or read back a key", and against
#:       the rule this session has held all day that a passphrase never appears in anything this
#:       repository emits. Nothing on this page may be able to ask for one.
#:   stop
#:       the operator's Gridcoin daemon is staking their live wallet. Rule 13 and the live-safety
#:       rules: this harness starts and stops NOTHING.
#:   dumpprivkey, dumpwallet, importprivkey
#:       a key read back out is a key that has left the wallet.
#:
#: The page offers these as a dropdown, but the allowlist is enforced HERE, on the server, and
#: the dropdown is only a convenience: a request naming anything else is refused whatever the
#: page sends.
READ_ONLY_RPCS = (
    "getblockchaininfo", "getinfo", "getmininginfo", "getnetworkinfo", "getwalletinfo",
    "getblockcount", "getbestblockhash", "getdifficulty", "getconnectioncount", "getpeerinfo",
    "getbalance", "listunspent", "listtransactions", "listaddressgroupings", "listlockunspent",
    "getrawtransaction", "decoderawtransaction", "decodescript", "validateaddress",
    "getblock", "getblockhash", "getrawmempool", "gettxoutsetinfo", "uptime", "help",
)


def refuse_unless_read_only(method: object) -> str:
    """"" if this method may be called, else the reason it may not. THE decision, on its own.

    A FUNCTION RATHER THAN AN `in` CHECK AT THE ROUTE, because this is the single decision that
    decides whether a browser can make this process move money, and a decision reachable only by
    making an HTTP request is a decision nobody tests (rule 10). This one is called with a
    string, including hostile ones.

    THE REFUSAL NAMES THE RULE rather than just saying no. An operator who is refused
    `sendtoaddress` needs to know it is a deliberate boundary and where the capability lives
    instead, or they will go looking for a flag that does not exist.
    """
    if not isinstance(method, str) or not method:
        return f"{method!r} is not a method name"
    if method in READ_ONLY_RPCS:
        return ""
    return (
        f"{method!r} is not on this panel's read-only allowlist. Every method it will call "
        f"READS -- nothing here creates a transaction, signs, touches a wallet lock, or writes "
        f"a key. Spending goes through the named harnesses in the Run section, or through your "
        f"own shell; a passphrase never goes through this page at all."
    )


def call_read_only(run: adaptor_steps.Run, method: str, args: list) -> dict:
    """Make one allowlisted call and return {ok, result} or {ok: false, error}. NEVER raises.

    A REFUSAL AND A FAILURE ARE DIFFERENT and both are results. "That method is not allowed" is
    the panel's own boundary; "the daemon said -1" is the chain answering. Collapsing them would
    leave an operator unable to tell a policy they can read from a problem they must fix
    (rule 14), so the two carry different text and the daemon's own words are never paraphrased.
    """
    refusal = refuse_unless_read_only(method)
    if refusal:
        return {"ok": False, "refused": True, "error": refusal}
    try:
        return {"ok": True, "result": run.node().call(method, *args)}
    except Exception as error:  # noqa: BLE001 -- checked: the failure IS the return value, named with its type, and a panel that dies on a bad argument is a panel that cannot be used to explore
        return {"ok": False, "refused": False,
                "error": f"{type(error).__name__}: {error}"}


#: Whether this panel may START or STOP each chain's daemon, and the reason either way.
#:
#: THE OPERATOR ASKED FOR AN ON/OFF SWITCH PER TAB on 2026-09-28, which is a posture change and
#: theirs to make (rule 16). It is not the same decision for all six, and giving them one switch
#: with one meaning would be the "six tabs, one set of controls" defect again in a place where
#: it costs more than a wasted click.
#:
#:   regtest   BTC and LTC. Throwaway chains whose daemons regtest_htlc_verify.py already starts
#:             and stops, through daemons.start_daemon/stop_daemon -- a spawn with a named
#:             reaper that PROVES the process is gone (rule 13). Nothing is at stake: the coins
#:             are minted on demand and the datadir is disposable. Full control.
#:
#:   operator  GRC. This is the operator's own daemon, STAKING THEIR WALLET. The whole harness
#:             has said "this harness starts and stops NOTHING" all day for that reason, and a
#:             stop from a browser button is a different thing from a stop they typed: this page
#:             is unauthenticated behind a loopback bind, and a tab they left open is a tab
#:             something else can reach.
#:
#:             STOP IS AVAILABLE AND REQUIRES AN OPT-IN OUTSIDE THE BROWSER --
#:             ST_PANEL_MAY_STOP_DAEMONS in the environment that starts the panel. That is not
#:             a nag: it means the decision to arm this was made in a shell, deliberately, and
#:             cannot be made by anything that merely reaches the port.
#:
#:             START IS REFUSED OUTRIGHT, and not out of caution -- this panel does not KNOW how
#:             that daemon is started. It never started it, so it has no binary, no datadir
#:             flags and no idea whether it runs under a service manager. Inventing a command
#:             line for the process that stakes the operator's wallet is exactly the guess rule
#:             17 forbids, and "it did not come back up" is the worst time to discover a guess.
#:
#:   foreign   XMR, XRP, SOL. No daemon lifecycle here at all; this panel cannot even probe them.
DAEMON_CONTROL = {
    "regtest": {"start": True, "stop": True},
    "operator": {"start": False, "stop": True},
    "foreign": {"start": False, "stop": False},
}

#: The environment variable that arms stopping a daemon this panel did not start.
MAY_STOP_VARIABLE = "ST_PANEL_MAY_STOP_DAEMONS"


def refuse_daemon_control(tab: ChainTab, action: str) -> str:
    """"" if this switch may be thrown, else why not. THE decision, apart from the plumbing.

    THREE REFUSALS AND THEY ARE NOT INTERCHANGEABLE, which is why each carries its own sentence
    rather than a shared "not allowed". An operator refused a START on GRC needs to know this
    panel does not know their command line, because they will otherwise look for a flag. One
    refused a STOP needs to know an environment variable arms it. One on XMR needs to know
    there is no lifecycle here at all.
    """
    if action not in ("start", "stop"):
        return f"{action!r} is not start or stop"
    policy = DAEMON_CONTROL.get(tab.kind, {"start": False, "stop": False})
    if not policy.get(action):
        if tab.kind == "operator" and action == "start":
            return (
                f"this panel will not START {tab.asset}: it never started that daemon, so it "
                f"has no binary, no datadir flags and no idea whether it runs under a service "
                f"manager. Inventing a command line for the process that stakes your wallet is "
                f"a guess, and 'it did not come back up' is the worst time to find that out."
            )
        if action == "start":
            # NOT "THERE IS NO LIFECYCLE", WHICH WAS A CLAIM ABOUT MONERO AND WAS FALSE.
            # Until 2026-09-28 both buttons on a foreign tab said "this panel has no daemon
            # lifecycle for XMR -- it cannot even probe that endpoint, let alone run it", and
            # the operator read it beside a tab that had just told them exactly which variable
            # was unset. Monero has daemons; so does rippled; so does a Solana validator. What
            # is true is the same thing that is true of GRC one branch up -- THIS PANEL DOES
            # NOT KNOW YOUR COMMAND LINE -- and it is true here for a stronger reason: nothing
            # in this tree has ever started one. `grep -rn "monero-wallet-rpc" --include=*.py`
            # over the tree on 2026-09-28 found the string in eleven files and a spawn of it in
            # none; `regtest/daemons.py` owns every Popen here and knows bitcoind and litecoind
            # only.
            return (
                f"this panel will not START {tab.asset}: nothing in this tree has ever started "
                f"one, so it has no binary, no wallet file, no port and no network flag to "
                f"start it with. On {tab.asset} that command line would choose WHICH WALLET is "
                f"opened, which is a custody decision and is yours -- start it in a shell and "
                f"this tab will say so."
            )
        # A STOP NEEDS A HANDLE, AND THERE IS NONE. Rule 13 prefers a pid file to a `pgrep -f`
        # pattern for exactly the case this is: the only way to find a monero-wallet-rpc this
        # panel did not spawn is to match its command line, and that pattern matches EVERY
        # monero-wallet-rpc on the host -- including one serving a different wallet the
        # operator is mid-transfer on. A kill that cannot say which process it hit is not a
        # stop, it is a guess with a signal attached.
        return (
            f"this panel will not STOP {tab.asset}: it did not start that process, so it holds "
            f"no pid for it. The only way to find one is to match a command line, and that "
            f"pattern hits every {tab.asset} daemon on this host -- including one serving a "
            f"different wallet. Rule 13 wants a pid file, and there is not even a pattern worth "
            f"having here."
        )
    if tab.kind == "operator" and action == "stop" and not os.environ.get(MAY_STOP_VARIABLE):
        return (
            f"stopping {tab.asset} is armed by {MAY_STOP_VARIABLE} in the environment that "
            f"starts this panel, and it is not set. That daemon is STAKING YOUR WALLET, and "
            f"this page is unauthenticated behind a loopback bind -- so the decision to arm a "
            f"browser button that stops it should be made in a shell, deliberately, and not by "
            f"anything that merely reaches this port."
        )
    return ""


def refuse_an_rpc_console(tab: ChainTab) -> str:
    """"" if this tab gets the read-only RPC console, else why it does not.

    THE CONSOLE SPEAKS ONE PROTOCOL. Every method in READ_ONLY_RPCS is a Bitcoin-style JSON-RPC
    call, sent as {"method": ..., "params": [...]} to a daemon that answers that shape. Monero,
    XRP and Solana each answer a DIFFERENT shape -- monero-wallet-rpc is JSON-RPC 2.0 with the
    call nested under `params`, the XRP Ledger takes a single-element `params` array of objects,
    Solana takes named parameters -- so `getblockcount` is not a method any of them has, and a
    console pointed at one would not fail usefully, it would fail confusingly.

    ASKED HERE RATHER THAN DISCOVERED AT THE SOCKET, which is the whole point of it being a
    function. Before this existed, `main()` tried to build a connection for every tab and the
    three foreign ones printed `no RPC console (KeyError: 'XMR')` at startup -- a Python
    exception class in an operator's terminal, for a condition that is by design and known
    before anything is asked of a network (rule 14: the panel should say what it is doing in
    words the operator can act on, and an exception name is not one).
    """
    if tab.kind == "foreign":
        return (f"{tab.asset} does not speak the Bitcoin-style JSON-RPC this console sends, so "
                f"there is nothing here to point at it. That chain has its own client in this "
                f"tree, and its own read-only check is the button under Run.")
    return ""


#: What one nav dot may say. NAMED, because the page has a CSS rule per value and a typo in
#: either half is a dot that silently renders as nothing.
DOT_ANSWERED = "yes"      # green: it answered the last time this panel asked
DOT_SILENT = "no"         # red:   it did not answer the last time this panel asked
DOT_CONFIGURED = "cfg"    # amber: this panel cannot ask, but the endpoint IS configured
DOT_UNASKED = ""          # grey:  nobody has asked, which is NOT the same as down


def dot_state(state: dict) -> str:
    """The nav dot for one chain, from that chain's own payload.

    RED MUST MEAN "WE ASKED AND IT DID NOT ANSWER", and until 2026-09-28 it did not. The page
    computed the dot as `d.reachable ? "yes" : "no"` with a grey branch guarded by a kind that
    no tab carries, so the three chains this panel never probes at all were painted red -- the
    exact stale-lie-an-operator-acts-on the dot's own comment forbids. `cfg` had a CSS rule and
    the legend under the nav promised amber, and nothing in the tree ever set it.

    A FOREIGN CHAIN IS NEVER RED HERE, because this panel has no evidence to be red with. It is
    amber when its endpoint is configured -- meaning the button under Run has somewhere to
    connect to -- and grey when it is not, meaning nothing has been asked and nothing could be.
    """
    if state.get("kind") == "foreign":
        return DOT_CONFIGURED if state.get("configured") else DOT_UNASKED
    return DOT_ANSWERED if state.get("reachable") else DOT_SILENT


class Missing(NamedTuple):
    """One RPC the daemon does not have, and what its absence costs this repository."""

    method: str
    present: bool
    matters: str


#: The three absences that shaped every workaround in this tree, so the panel states them as
#: facts rather than leaving each run to rediscover them. Measured on the operator's daemon
#: 2026-09-28: all three absent, and `testmempoolaccept` was the expensive one -- every
#: pre-broadcast check in this repository went through it and none had ever executed.
PROBED_METHODS = (
    ("testmempoolaccept", "would say WHY a transaction is refused; without it, -22 names nothing"),
    ("gettxout", "would say whether an output is unspent; without it, the chain is walked block by block"),
    ("importaddress", "would let the funding address be watched; without it, listtransactions is the only record"),
)


class PaymentRow(NamedTuple):
    """One payment to the funding address, and whether it can still be used."""

    txid: str
    confirmations: object
    value_coins: str
    spender: str
    usable: bool
    note: str


def probe_methods(run: adaptor_steps.Run) -> list[Missing]:
    """Which of the three shaping RPCs this daemon actually has.

    ASKED EVERY TIME RATHER THAN ASSUMED. The operator can rebuild their daemon, and a panel
    that hard-coded "Gridcoin has no testmempoolaccept" would keep saying so after the upgrade
    that fixed it -- which is rule 1's "a measurement in prose ages" in a place that refreshes
    every few seconds and has no excuse.
    """
    node = run.node(wallet=False)
    found = []
    for method, matters in PROBED_METHODS:
        try:
            present = daemons.method_exists(node, method)
        except (RPCError, OSError):
            present = False
        found.append(Missing(method, present, matters))
    return found


def payment_rows(run: adaptor_steps.Run, key, known_spent: dict | None = None) -> list[PaymentRow]:
    """Every payment to the funding address, newest first, each marked usable or spent.

    THIS IS THE PANEL'S WHOLE REASON TO EXIST. Six runs on 2026-09-28 failed or refused because
    this list was only ever computed INSIDE a run, one entry at a time, as a side effect of
    choosing. An operator who could see it would have sent a fresh payment after the first one
    instead of the fifth.

    IT SHARES ITS SOURCE WITH THE PICKER. `payments_to_the_funding_address` and
    `find_the_spender` are the same two functions `discover_operator_funding_txid` uses, so the
    panel cannot disagree with the run about which payment is usable. Two implementations of
    that question would agree on the day they were written (rule 8), and the day they stopped
    agreeing is the day an operator funds an address the harness will not spend from.

    EVERY ROW CARRIES A NOTE, including the ones that are fine. A row that says nothing when it
    is healthy and something when it is not trains the reader to look only at the noisy ones,
    and the quiet failure here -- a payment whose spent-ness could not be established -- looks
    exactly like a healthy one (rule 14).
    """
    try:
        entries = run.node().call("listtransactions", "*", adaptor_steps.FUNDING_SEARCH_DEPTH, 0)
    except (RPCError, OSError) as error:
        raise RegtestSetupError(
            f"could not read the wallet's recent transactions ({error}), so no payment to "
            f"{key.address} can be listed. Nothing else on this page depends on it."
        ) from error

    rows = []
    for payment in adaptor_steps.payments_to_the_funding_address(entries, key.address):
        try:
            outpoint = adaptor_steps.find_operator_funding(run, key, payment.txid)
        except (RegtestSetupError, RPCError, OSError) as error:
            rows.append(PaymentRow(payment.txid, payment.confirmations, "?", "", False,
                                   f"could not be read off the chain: {error}"))
            continue
        # A SPENT OUTPUT IS SPENT FOREVER, so the answer is worth remembering. Measured on the
        # operator's screen 2026-09-28: opening the panel walked 85, 208 and 259 blocks -- three
        # payments, every one of them long spent -- and did it again on every reload, because
        # the tab recomputes what the run recomputes. Rule 3 prefers REMOVING work to doing it
        # faster, and this removes all of it after the first look.
        #
        # ONLY THE POSITIVE ANSWER IS CACHED, and the asymmetry is the whole correctness
        # argument. "Spent by X" is monotone: no reorganization this harness cares about can
        # unspend it, and if one did, the outpoint would be gone rather than usable. "Not
        # spent" is NOT monotone -- the very next block can spend it, and caching that would
        # have the panel cheerfully offering a payment the harness then refuses. The cache may
        # therefore only ever turn a slow correct answer into a fast one.
        cached = (known_spent or {}).get((outpoint.txid, outpoint.vout))
        if cached:
            spender, description = cached, "SPENT ALREADY -- remembered from an earlier look, not re-walked"
        else:
            depth = (payment.confirmations + 1 if isinstance(payment.confirmations, int)
                     and payment.confirmations >= 0 else adaptor_steps.MAX_SPEND_SCAN_BLOCKS)
            spender, description = adaptor_steps.find_the_spender(run, outpoint, max_depth=depth)
            if spender and known_spent is not None:
                known_spent[(outpoint.txid, outpoint.vout)] = spender
        value = adaptor_steps.satoshis_to_coins(outpoint.value_satoshis)
        if spender:
            # THE DESCRIPTION IS CARRIED, not replaced. It is the only thing that says whether
            # this answer was measured just now or remembered from an earlier look, and rule 17
            # is exactly that a reader must be able to tell which they are holding. Dropping it
            # made both read identically -- which the cache's own test caught.
            rows.append(PaymentRow(payment.txid, payment.confirmations, value, spender, False,
                                   f"SPENT -- a completed run consumes its funding by design. "
                                   f"{description}"))
        else:
            rows.append(PaymentRow(payment.txid, payment.confirmations, value, "", True,
                                   description))
        # AND SAY THE VERDICT WHERE IT IS READ, 2026-09-28. Until this line the verdict existed
        # ONLY in the row, which renders in the page's funding table -- and the panel's terminal
        # showed just the candidate line from find_operator_funding(). Five long-spent outputs
        # therefore printed as five found fundings on the operator's screen, and I read that
        # paste and told them 12.12 GRC was available. Every one was spent, at 168 to 479 blocks
        # deep, which the next run of grc_htlc_verify established in eight seconds.
        #
        # The page was never wrong. The terminal was incomplete, and an incomplete line in the
        # place someone is actually looking is rule 14's defect in the output -- "make 'did
        # nothing' look different from 'did work'" applied to a usable output versus a dead one.
        run.say(
            f"{payment.txid[:16]}…:{rows[-1].value_coins} {run.asset} -- "
            f"{'SPENT by ' + rows[-1].spender[:16] + '…' if rows[-1].spender else 'USABLE'}"
        )
    if not rows:
        # RULE 14: "(none) is a result". A blank here is ambiguous between no payment and a
        # listtransactions that came back empty for another reason, and the address is what the
        # operator needs in order to fix either.
        run.say(f"no payment to {key.address} is in the wallet's recent transactions")
    return rows
