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
structural, tests/test_web_surfaces.py asserts it over the real URL map, and the app is
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

# THE CORE-WALLET PANES AND THE READ-ONLY ALLOWLIST LIVE IN chains/ SINCE 2026-10-10,
# AND THEY ARE IMPORTED RATHER THAN RE-EXPORTED. Both were written in this file for
# this panel's four read panes; /admin's chain panel then needed the identical four,
# reached through the application's own adapters rather than through a
# funding_steps.Run, and a second copy is rule 8's bug with a delay on it. The move
# is recorded at length in chains/daemon_wallet.py's header, including why
# `call_read_only()` below did NOT go with them.
#
# NO COMPATIBILITY SHIM WAS LEFT HERE, deliberately (rule 9, rule 19). A block of
# `wallet_pane = daemon_wallet.wallet_pane` aliases would have kept every existing
# `decisions.wallet_pane` reference working, and would be exactly the "quarantine
# instead of delete" rule 2 refuses: a reader finding two paths to one function has
# to establish which one runs before anything else can be asked. The handful of
# callers -- `wallet_pane` here, READ_ONLY_RPCS in the root panel, the pane tests --
# name the new module directly instead.
from chains.daemon_wallet import refuse_unless_read_only, wallet_pane

# AND THE PROTOCOL TRANSLATION WENT THE SAME WAY, IN THE SAME COMMIT, for the same
# second caller: /admin's chain panel asks the four pane questions of XRP and SOL and
# needs exactly the mapping the operator asked for on 2026-09-30. What stayed here is
# console_protocol() and refuse_an_rpc_console(), which take this panel's own ChainTab
# -- a tab is this panel's shape, not a chain fact. chains/rpc_translation.py's header
# records the split from the other side.
from chains.rpc_translation import CONSOLE_PROTOCOL, console_methods
from regtest import daemons, funding_steps
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
#: Bitcoin's orange is the widely published one; Litecoin's blue, XRP's
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
#:   foreign    no Bitcoin-style adapter in this panel. XRP and SOL are swapped by other
#:              entry points with their own clients; saying so beats an empty tab or a missing
#:              one.
#:
#: THE WORD WAS `none` IN THIS COMMENT AND IN ChainTab's TYPE LINE UNTIL 2026-09-28, AND NO TAB
#: HAS EVER CARRIED IT. CHAINS below has said "foreign" since the tabs were written, so both
#: the comment and the annotation described a fourth kind that does not exist -- and two live
#: branches were written against the name: `main()` skipped console construction on
#: `kind == "none"` (so it never skipped, and printed a `KeyError` per foreign tab at startup
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
    "XRP": ("XRP_RPC_URL",),
    "SOL": ("SOL_RPC_URL",),
}

def why_foreign_is_unconfigured(tab: ChainTab) -> str:
    """"" if this foreign chain's endpoint is set, else the sentence saying which is missing.

    ONE SENTENCE, ONE PLACE, THREE READERS. The page assembled this prose in JavaScript from
    `missing_env`, and on 2026-09-30 the XRP console needed the same sentence in Python -- which
    would have been a second copy of it, agreeing on the day it was written (rule 8). So the
    server owns the words, chain_state() carries them, and the page prints what it is given.

    The VARIABLE NAMES are derived from FOREIGN_ENV either way; it is the prose that was about
    to be duplicated, and prose drifts more quietly than a name does because nothing ever
    compares two sentences.
    """
    names = FOREIGN_ENV.get(tab.asset, ())
    missing = [name for name in names if not os.environ.get(name)]
    if not missing:
        return ""
    return (f"{', '.join(missing)} is unset in the environment this panel was started with. "
            f"Nothing reads a .env here, so it has to be exported in the shell that starts the "
            f"panel; a value set only in a file, or only in another shell, does not reach this "
            f"process.")


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
    ChainTab("XRP", "foreign", False,
             "the XRP Ledger has its own JSON-RPC shape, reached through chains/xrp.py. This "
             "panel does not probe it directly; its own read-only check is the button below."),
    # "the custody choice is the operator's and is not decided in this tree" stood here until
    # 2026-09-30 and was stale from 2026-09-29, the day the operator decided it. The panel is
    # the screen somebody reads INSTEAD of the source, so a sentence on it saying a decision is
    # open is the same defect as a wrong number (rule 16).
    ChainTab("SOL", "foreign", False,
             "Solana is reached through chains/solana.py. get_new_address() still refuses there "
             "by design: the operator chose one shared account plus a per-swap Memo instruction "
             "on 2026-09-29, so there is no per-swap address to derive. Its own read-only check "
             "is the button below."),
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

    # THE THREE PASS-THROUGHS ARE SPELLED OUT EVEN THOUGH __getattr__ BELOW ALREADY
    # FORWARDS THEM, and that is not belt-and-braces. Measured 2026-10-09: pyright
    # does NOT credit a `__getattr__` toward structural matching, so with only the
    # magic method this class did not satisfy regtest/console.ConsoleLike and
    # `run.console = SaysEachLineOnce(console)` was refused -- the finding that
    # started this. Nothing was ever broken at runtime; the delegation worked and
    # was simply invisible, to a checker and to `inspect.getmembers` alike, which is
    # what made this wrapper's surface look incomplete the first time anyone read it.
    #
    # Writing them out is the better code regardless of the checker: a reader now
    # sees what a Run's console is actually used for -- say, step, check, elapsed --
    # without having to work out what `__getattr__` would catch.
    #
    # __getattr__ STAYS, because it still covers `banner`, `summary` and the
    # attributes (`results`, `total_steps`) that nothing in the funding walk touches
    # but a future caller might. Removing it would turn "this wrapper forwards
    # everything it does not override" into "this wrapper forwards the four things
    # somebody listed", which is a narrower promise than the class makes today.
    def step(self, number: int, chain: str, title: str) -> None:
        self._console.step(number, chain, title)

    def check(self, label: str, got: object, expected: object, outcome: str) -> str:
        return self._console.check(label, got, expected, outcome)

    def elapsed(self) -> str:
        return self._console.elapsed()

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
        # the one an operator actually needs. "this tab does nothing" was the report on 2026-09-28,
        # and it was accurate: the tab said "no adapter" and stopped, which is a statement about
        # this panel dressed as a statement about the chain. What the operator could act on was
        # sitting one environment variable away.
        #
        # ASKED OF THE ENVIRONMENT, not of a network, so it costs nothing and cannot hang. It
        # says whether the entry point behind the button has somewhere to connect TO -- which
        # is a different question from whether that endpoint answers, and the tab says which
        # question it answered (rule 17).
        missing = [name for name in FOREIGN_ENV.get(tab.asset, ()) if not os.environ.get(name)]
        # `methods` IS NOT EMPTY FOR EVERY FOREIGN TAB ANY MORE. XRP has a command map
        # (chains/xrp_rpc_map.py) and therefore a console, so its dropdown is populated from
        # that map rather than from READ_ONLY_RPCS -- see console_methods() for why the
        # bitcoind allowlist cannot serve it. SOL still has none, and "" is the honest answer.
        protocol = console_protocol(tab)
        # `console_methods`, NOT `methods`. On a bitcoin-family tab `methods` already means
        # something else entirely -- probe_methods()' [{method, present, matters}] telling the
        # operator which RPCs that daemon actually HAS -- and reusing the name put the console's
        # option list and a capability report under one key. The visible result, on the
        # operator's screen 2026-09-30: the XRP dropdown offered all 25 bitcoin names,
        # including the ELEVEN that have no XRP equivalent, and none of the five XRPL-only
        # reads. A console that can only refuse is worse than no console, which is the exact
        # defect the comment at the top of loadChain() in operator_panel.py is about.
        return {"asset": tab.asset, "kind": tab.kind, "note": tab.note, "reachable": False,
                "network": "", "error": "", "funding": None, "methods": [],
                "protocol": protocol,
                "console_methods": console_methods(protocol),
                "configured": not missing, "missing_env": missing,
                "unconfigured_why": why_foreign_is_unconfigured(tab),
                "env": list(FOREIGN_ENV.get(tab.asset, ()))}
    try:
        config = funding_steps.resolve_config(tab.asset)
    except Exception as error:  # noqa: BLE001 -- checked: the failure IS the tab's contents, named below
        return {"asset": tab.asset, "kind": tab.kind, "note": tab.note, "reachable": False,
                "network": "", "error": f"no connection parameters: {type(error).__name__}: {error}",
                "methods": [], "funding": None}
    run = funding_steps.Run(console=console, config=config, wallet="")
    try:
        height = funding_steps.current_height(run)
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
        # THE CORE-WALLET PANES. Costed deliberately: /api/chain/<asset> is fetched on a
        # TAB CLICK and is not on the page's 5-second timer -- "ONLY THE OPEN PANE POLLS"
        # in operator_panel.py, and the timer calls loadSwapper and loadControls, not
        # loadChain. So this is eight loopback reads per click, not per second.
        #
        # EVERY READ INSIDE IS INDEPENDENTLY FAILURE-TOLERANT (see _read), so a daemon
        # missing getpeerinfo still renders a balance and a daemon with no wallet still
        # renders its peers. Nothing here can raise out of this function, which is why it
        # needs no try/except of its own -- and why one that wrapped it would be the blind
        # catch rule 12 is about rather than a safeguard.
        "wallet_pane": wallet_pane(run),
    }


def network_the_daemon_says(run: funding_steps.Run) -> str:
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


def call_read_only(run: funding_steps.HasAChainNode, method: str, args: list) -> dict:
    """Make one allowlisted call and return {ok, result} or {ok: false, error}. NEVER raises.

    A REFUSAL AND A FAILURE ARE DIFFERENT and both are results. "That method is not allowed" is
    the panel's own boundary; "the daemon said -1" is the chain answering. Collapsing them would
    leave an operator unable to tell a policy they can read from a problem they must fix
    (rule 14), so the two carry different text and the daemon's own words are never paraphrased.

    `HasAChainNode` AND NOT `ChainReadingRun`, AND THIS IS THE CALL SITE THAT MEASURED THE
    SPLIT. The body reaches `run.node().call(...)` and nothing else -- no asset, no line
    printed -- and the stub in tests/test_operator_panel.py has neither member. The
    one-Protocol design was tried first and failed here, so declaring the funding walk's
    Protocol would over-promise by two members and `funding_steps.Run` by nine.
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
#:   foreign   XRP, SOL. No daemon lifecycle here at all; this panel cannot even probe them.
DAEMON_CONTROL = {
    "regtest": {"start": True, "stop": True},
    "operator": {"start": False, "stop": True},
    "foreign": {"start": False, "stop": False},
}

#: The environment variable that arms stopping a daemon this panel did not start.
MAY_STOP_VARIABLE = "ST_PANEL_MAY_STOP_DAEMONS"


def effective_control_kind(tab: ChainTab, network: str) -> str:
    """Which DAEMON_CONTROL policy applies, given what the daemon says it is.

    `tab.kind` IS A LITERAL IN THE CHAINS TABLE AND THAT IS THE DEFECT THIS CLOSES.
    BTC and LTC are written `"regtest"` there, and the policy keyed on that word grants
    full start/stop. Read the justification above DAEMON_CONTROL and notice that every
    clause of it is a claim about the CHAIN, not about the tab:

        "Throwaway chains whose daemons regtest_htlc_verify.py already starts and
         stops ... Nothing is at stake: the coins are minted on demand and the datadir
         is disposable. Full control."

    Move that daemon to testnet -- which the operator asked for on 2026-10-10, "our
    grc, ltc, and btc daemons should ... not be regtest anyone and just full testnet
    now" -- and not one of those clauses survives. The coins come from a faucet. The
    datadir is a multi-gigabyte sync that fund_testnets.py measured at 33-54 hours. And
    the swap terminal's own payout path depends on that daemon being up, so stopping it
    from a browser button is stopping a live service.

    WHAT THE UNFIXED VERSION WOULD HAVE DONE, and it is worse than a stale label:
    daemons.start_daemon() builds its argv with `-regtest` as a LITERAL
    (regtest/daemons.py, base_argv) and no environment override. So the panel would have
    shown the tab's network as `test`, still offered START, and pressing it would have
    spawned a SECOND daemon -- a regtest one -- inside the testnet datadir tree, binding
    whatever rpcport the leftover [regtest] section still named. A button that reads as
    "start the thing I am looking at" would have started something else.

    THREE CASES, AND THE MIDDLE ONE IS WHY THIS TAKES THE NETWORK AS AN ARGUMENT RATHER
    THAN ASKING:

      ""            not established -- the daemon did not answer, which is the ORDINARY
                    state for a regtest tab and the exact case START exists for. Keeps
                    the tab's own kind, so starting a stopped daemon still works.
      "regtest"     the daemon confirms what the tab claims. Full control, as before.
      anything else the daemon is on a network this panel did not create and cannot
                    recreate. It is the OPERATOR's daemon now, whatever the table says,
                    so it gets the operator policy: START refused outright, STOP behind
                    MAY_STOP_VARIABLE.

    PURE, so a test can call it with a string instead of standing up a daemon (rule 10).
    The caller passes what network_the_daemon_says() already read for the tab -- no
    second connection, and no second place that decides what a network name means.
    """
    if tab.kind != "regtest" or not network:
        return tab.kind
    return "regtest" if network == "regtest" else "operator"


def _what_is_at_stake(tab: ChainTab, network: str) -> str:
    """Why stopping THIS daemon matters. Per chain, because the reason differs.

    THE GENERAL SENTENCE LOST INFORMATION AND A TEST CAUGHT IT. When this refusal
    started covering BTC and LTC as well as GRC on 2026-10-10, the clause was
    generalized to "that daemon is the one this desk reads" -- and
    test_STOPPING_the_operators_own_daemon_is_armed_OUTSIDE_the_browser failed on the
    missing words STAKING YOUR WALLET. The test was right: for Gridcoin that is not
    colour, it is the specific reason a stop is worse than a restart. Gridcoin stakes
    continuously, so stopping it forfeits research reward the operator cannot get back
    by starting it again, which is not true of either of the others.

    So the clause is derived rather than shared. Two chains, two genuinely different
    stakes, and rule 8's test applies: these are not duplicates to merge, they are a
    difference that belongs at the site.
    """
    if tab.kind == "operator":
        return "That daemon is STAKING YOUR WALLET"
    return (
        f"That daemon is on {network!r} and is the one this desk's payout path reads -- "
        f"stopping it is stopping a live service, and its datadir is a sync measured in "
        f"tens of hours rather than a disposable regtest directory"
    )


def _refuse_start_moved_off_regtest(tab: ChainTab, network: str) -> str:
    """A BTC/LTC tab whose daemon says it is NOT on regtest. Added 2026-10-10.

    This is the one the table existed without. start_daemon() builds its argv with
    `-regtest` as a literal and no override, so on a testnet daemon this button would
    not restart what the operator is looking at -- it would spawn a SECOND, regtest
    daemon inside their testnet datadir.
    """
    return (
        f"this panel will not START {tab.asset}: the daemon says it is on {network!r}, and the "
        f"only daemon this panel knows how to start is a REGTEST one -- `-regtest` is a literal "
        f"in its argv with no override. Pressing this would spawn a SECOND, regtest daemon "
        f"inside your {network} datadir rather than restarting the one you are looking at. "
        f"Start it the way you started it."
    )


def _refuse_start_operators_own(tab: ChainTab, network: str) -> str:
    """GRC. This panel never started it and cannot invent its command line."""
    del network
    return (
        f"this panel will not START {tab.asset}: it never started that daemon, so it has no "
        f"binary, no datadir flags and no idea whether it runs under a service manager. "
        f"Inventing a command line for the process that stakes your wallet is a guess, and 'it "
        f"did not come back up' is the worst time to find that out."
    )


def _refuse_start_foreign(tab: ChainTab, network: str) -> str:
    """XRP, SOL.

    NOT "THERE IS NO LIFECYCLE", WHICH IS A CLAIM ABOUT THE CHAIN AND IS FALSE. Until
    2026-09-28 both buttons on a foreign tab said this panel had no daemon lifecycle for
    that chain and could not even probe it -- and the operator read it beside a tab that
    had just told them exactly which variable was unset. Every foreign chain here HAS a
    daemon: rippled is one, a Solana validator is one. What is true is the same thing
    that is true of GRC -- THIS PANEL DOES NOT KNOW YOUR COMMAND LINE -- and it is true
    here for a stronger reason: nothing in this tree has ever started one.
    regtest/daemons.py owns every Popen here and knows bitcoind and litecoind only.
    """
    del network
    return (
        f"this panel will not START {tab.asset}: nothing in this tree has ever started one, so "
        f"it has no binary, no wallet file, no port and no network flag to start it with. On "
        f"{tab.asset} that command line would choose WHICH WALLET is opened, which is a custody "
        f"decision and is yours -- start it in a shell and this tab will say so."
    )


def _refuse_stop_no_handle(tab: ChainTab, network: str) -> str:
    """A STOP NEEDS A HANDLE, AND THERE IS NONE.

    Rule 13 prefers a pid file to a `pgrep -f` pattern for exactly this case: the only
    way to find a daemon this panel did not spawn is to match its command line, and that
    pattern matches EVERY daemon of that kind on the host -- including one serving a
    different wallet the operator is mid-transfer on. A kill that cannot say which
    process it hit is not a stop, it is a guess with a signal attached.
    """
    del network
    return (
        f"this panel will not STOP {tab.asset}: it did not start that process, so it holds no "
        f"pid for it. The only way to find one is to match a command line, and that pattern "
        f"hits every {tab.asset} daemon on this host -- including one serving a different "
        f"wallet. Rule 13 wants a pid file, and there is not even a pattern worth having here."
    )


#: WHICH SENTENCE A REFUSED SWITCH GETS, keyed by (effective kind, action, was-the-tab-a-
#: regtest-one). A TABLE RATHER THAN AN IF-CHAIN, and this file has already made that
#: argument once about itself: answer_a_get()'s docstring in operator_panel.py says the
#: route dispatch "was an if-chain until the controls route made it seven deep and
#: PLR0911 fired -- which is rule 12's reading of that code: a dispatch that has
#: swallowed a decision per branch." This one fired the same lint for the same reason
#: when the moved-off-regtest case was added, so it takes the same shape.
#:
#: THE THIRD KEY ELEMENT IS WHAT DISTINGUISHES THE TWO "operator" START REFUSALS. Both
#: are "this panel will not start it", and they are refusals for DIFFERENT reasons that
#: an operator needs told apart: GRC was never this panel's to start, while a
#: moved-to-testnet BTC daemon WAS and the thing the panel can still start is no longer
#: the thing they are looking at. Collapsing them would send somebody looking for a flag.
_CONTROL_REFUSALS = {
    ("operator", "start", True): _refuse_start_moved_off_regtest,
    ("operator", "start", False): _refuse_start_operators_own,
    ("foreign", "start", False): _refuse_start_foreign,
    ("foreign", "stop", False): _refuse_stop_no_handle,
}


def refuse_daemon_control(tab: ChainTab, action: str, network: str = "") -> str:
    """"" if this switch may be thrown, else why not. THE decision, apart from the plumbing.

    FOUR REFUSALS AND THEY ARE NOT INTERCHANGEABLE, which is why each carries its own
    sentence rather than a shared "not allowed". An operator refused a START on GRC needs
    to know this panel does not know their command line, because they will otherwise look
    for a flag. One refused a START on a BTC daemon they moved to testnet needs to know
    something different -- that the panel CAN start a daemon and it would be the wrong
    one. One refused a STOP needs to know an environment variable arms it. One on a
    foreign tab needs to know there is no lifecycle here at all.

    `network` IS WHAT THE DAEMON SAID, and passing it in rather than asking keeps this
    pure and keeps the meaning of a network name in one place (see
    effective_control_kind). Empty means not established, which is the ordinary state for
    a stopped daemon and is exactly when START should work.
    """
    if action not in ("start", "stop"):
        return f"{action!r} is not start or stop"
    # THE EFFECTIVE KIND, NOT tab.kind. See effective_control_kind(): a BTC or LTC daemon
    # that says it is on anything but regtest is the operator's own daemon whatever the
    # CHAINS table calls it, because start_daemon() can only ever make a regtest one.
    kind = effective_control_kind(tab, network)
    policy = DAEMON_CONTROL.get(kind, {"start": False, "stop": False})
    if not policy.get(action):
        refusal = _CONTROL_REFUSALS.get((kind, action, tab.kind == "regtest"))
        # A KIND/ACTION PAIR WITH NO SENTENCE IS A GAP, AND IT SAYS SO rather than
        # returning "" -- which would read as PERMITTED and throw the switch. Fail
        # closed, and name the pair so the gap is fixable from the screen.
        if refusal is None:
            return (
                f"this panel will not {action.upper()} {tab.asset}: the policy refuses it and no "
                f"reason is recorded for ({kind!r}, {action!r}). Refusing without a reason is "
                f"still a refusal -- but the missing sentence is a defect, not a policy."
            )
        return refusal(tab, network)
    # ARMED BY THE EFFECTIVE KIND TOO, AND THIS WAS THE SECOND HALF OF THE SAME DEFECT.
    # This read `tab.kind == "operator"` until 2026-10-10, so a BTC or LTC daemon moved to
    # testnet -- whose effective kind is now `operator` and whose DAEMON_CONTROL policy
    # therefore ALLOWS stop -- would have been stoppable from an unauthenticated browser
    # button with no opt-in anywhere. That daemon is the one the swap terminal's payout
    # path reads, so stopping it is stopping a live service, which is precisely what
    # MAY_STOP_VARIABLE exists to make a deliberate act.
    if kind == "operator" and action == "stop" and not os.environ.get(MAY_STOP_VARIABLE):
        return (
            f"stopping {tab.asset} is armed by {MAY_STOP_VARIABLE} in the environment that "
            f"starts this panel, and it is not set. {_what_is_at_stake(tab, network)}, and "
            f"this page is unauthenticated behind a loopback bind -- so the decision to arm a "
            f"browser button that stops it should be made in a shell, deliberately, and not by "
            f"anything that merely reaches this port."
        )
    return ""


#: THE SWAPPER'S OWN PROCESSES, AND WHY THEY GET A BUTTON WHERE A DAEMON DOES NOT.
#:
#: refuse_daemon_control() above refuses to stop anything this panel did not start, and its
#: reason is rule 13's: the only handle on a foreign process is a command-line pattern, and that
#: pattern hits every daemon of its kind on the host. The three supervisor workers are the
#: opposite case in every respect, which is why the same file can allow both buttons without
#: contradicting itself:
#:
#:   a pid file exists      supervisor.pid_file() writes pid AND the command line, and
#:                          pid_is_still_ours() compares /proc's cmdline against the recorded
#:                          one -- so a recycled pid reports `stale-pidfile` and is NOT
#:                          signalled. That is the case where killing would be the damage, and
#:                          it is detected rather than risked.
#:   the stop is PROVEN     stop_worker() polls for ABSENCE after SIGTERM and again after
#:                          SIGKILL, and returns `failed` if the process is still there. The
#:                          assertion is the absence, never the exit code of the kill.
#:   this tree owns them    worker_commands() is the only table of what they are, and
#:                          start_worker() is called with that argv. Nothing is invented.
#:
#: NO ENVIRONMENT VARIABLE ARMS THIS, and that is deliberate rather than an oversight.
#: MAY_STOP_VARIABLE exists because the GRC daemon is STAKING the operator's wallet and a
#: browser button that stops it should be a decision made in a shell. A worker is not that: it
#: holds no wallet lock, it stakes nothing, and stopping one costs exactly what
#: services/admin_view.worker_stopped_consequence() says it costs -- which the page prints
#: beside the button rather than making the operator remember.
#:
#: WHAT IS GENUINELY AT RISK, said plainly because a button should not hide it: payout_worker
#: can be stopped between a broadcast and the row that records it.
#: services/payout_service.process_pending_payouts() names that window in its own comment -- a
#: timeout after the daemon accepted a transaction is marked `failed` and is indistinguishable
#: from a refusal -- and settle_payout.py exists because it happened. A stop lands inside that
#: window no more often than a crash does, and the operator now has one screen where they can
#: see the swap it happened to.
def refuse_worker_control(name: object, action: object, known: dict | None = None) -> str:
    """"" if this worker switch may be thrown, else why not. The decision, not the plumbing.

    THE ALLOWLIST IS DERIVED, never written here: `known` defaults to
    supervisor.worker_commands(), which is the one table of what a worker IS. A hand-kept copy
    in this file would be rule 8's shape at the worst place -- a name that fell out of the table
    would still be startable from a browser, with an argv this file had guessed.

    A name that is not in it is refused by NAME, and nothing from the request reaches subprocess
    either way: start_worker() is called with the table's own argv list.
    """
    from supervisor import (  # noqa: PLC0415 -- checked: imported inside the function because this module is imported by tests that must not touch supervisor's BASE_DIR at import time, and because the table is read fresh per request rather than frozen at import.
        worker_commands,
    )

    table = worker_commands() if known is None else known
    if action not in ("start", "stop"):
        return f"{action!r} is not start or stop"
    if not isinstance(name, str) or name not in table:
        return (
            f"{name!r} is not a worker this panel knows. It knows {', '.join(sorted(table))}, "
            f"which is supervisor.worker_commands() -- the one table of what a worker is. A "
            f"name not in it has no argv, and this panel will not invent one."
        )
    return ""




def console_protocol(tab: ChainTab) -> str:
    """Which console this tab gets: "bitcoin", "xrpl", or "" for none."""
    if tab.asset in CONSOLE_PROTOCOL:
        return CONSOLE_PROTOCOL[tab.asset]
    return "bitcoin" if tab.kind in ("regtest", "operator") else ""




def refuse_an_rpc_console(tab: ChainTab) -> str:
    """"" if this tab gets a console, else why it does not.

    THE CONSOLE USED TO SPEAK ONE PROTOCOL AND REFUSED BOTH FOREIGN TABS. That was honest and
    it was also a dead end: the operator asked on 2026-09-30 for "xrp control commands that are
    congruent to btc/ltc/grc rpc commands since xrp is just different", and the XRP Ledger does
    answer the same QUESTIONS -- under different names, in a different request shape, with a
    handful that genuinely have no answer there. chains/xrp_rpc_map.py is that mapping, so the
    XRP tab now gets a console and this function refuses only where there is nothing to point.

    SOL HAS ONE TOO, since later the same day: chains/solana_rpc_map.py, written after
    solana_chain_check.py passed against api.devnet.solana.com so the shapes in it are measured
    rather than documented. This function now refuses only a chain in CHAINS with no entry in
    CONSOLE_MAPS -- which is none of them today, and the refusal is kept rather than deleted
    because the next chain added will be in exactly that state.

    ASKED HERE RATHER THAN DISCOVERED AT THE SOCKET, which is the whole point of it being a
    function. Before it existed, `main()` tried to build a connection for every tab and the
    foreign ones each printed `no RPC console (KeyError: ...)` at startup -- a Python exception
    class in an operator's terminal, for a condition known before anything is asked of a
    network (rule 14).
    """
    if console_protocol(tab):
        return ""
    return (f"{tab.asset} has no console here yet. It does not speak the Bitcoin-style JSON-RPC "
            f"this one sends, and no command map has been written for it the way "
            f"chains/xrp_rpc_map.py and chains/solana_rpc_map.py were -- which is a thing "
            f"nobody has done, not a thing that cannot be done. That chain has its own client "
            f"in this tree, and its own read-only check is the button under Run.")



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


def probe_methods(run: funding_steps.Run) -> list[Missing]:
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


def scan_depth(previous: int | None, tip: int | None, full: int) -> tuple[int, str]:
    """How many blocks this spend-scan must examine, and the sentence saying why.

    THE DECISION, PULLED OUT OF payment_rows() BECAUSE IT IS ONE (rule 10 and rule 12). Ruff
    put that function at 11 against a ceiling of 10 the moment this logic went inline, and
    the ceiling was right: what had been swallowed is the only interesting judgment on that
    path. It is pure, so it is called with seeded numbers in the tests -- including the
    reorganization case, which is not reachable from a daemon anybody can arrange to have.

    THREE CASES AND ONLY ONE OF THEM IS THE FAST PATH:

      never looked        `previous` is None. The full depth, because nothing below the tip
                          has been examined yet.
      tip unreadable      `tip` is None. The full depth: a depth computed from a height
                          nobody read is a confident number derived from a failure.
      tip went BACKWARD   tip < previous, which means a reorganization or a different
                          daemon. The full depth, because the blocks examined last time may
                          no longer be the blocks that exist.
      otherwise           exactly tip - previous, the blocks that have arrived since. ZERO
                          when the tip has not moved, and that is the correct answer rather
                          than a skipped check: a spend after height `previous` must appear
                          in a block after `previous`, and no such block exists yet.

    The returned sentence is appended to find_the_spender()'s own description, so a reader
    can always tell a full walk from an extended one -- rule 17's "say which you have",
    applied to how much of the chain an answer actually rests on.
    """
    if previous is None or tip is None or tip < previous:
        return full, ""
    depth = tip - previous
    return depth, (
        f" Only the {depth} block(s) since height {previous} were examined; everything below "
        f"that was examined on an earlier look, and a spend cannot appear in a block that "
        f"already exists."
    )


def _tip_or_none(run: funding_steps.HasAChainNode) -> int | None:
    """The chain tip, or None if the daemon will not say. Never raises.

    None RATHER THAN A HEIGHT OF 0, because 0 is a height and would make the incremental
    scan in payment_rows() compute a depth from a tip that was never read -- a confident
    number derived from a failure, which is the shape rule 17 forbids. None routes to the
    full walk instead, which is slower and correct.

    `HasAChainNode` rather than the funding walk's Protocol: this asks one `getblockcount`
    through `funding_steps.current_height`, which wants the same one member.
    """
    try:
        return funding_steps.current_height(run)
    except (RPCError, OSError):
        return None


def payment_rows(run: funding_steps.ChainReadingRun, key, known_spent: dict | None = None,
                 unspent_as_of: dict | None = None) -> list[PaymentRow]:
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

    `run: ChainReadingRun` rather than `Run`, and it is this function that makes the three
    signatures move together: the same object goes straight through to
    `find_operator_funding`, `find_the_spender` and `_tip_or_none`, so any one of them still
    demanding the dataclass would make the Protocol here useless. Fourteen call sites in the
    suite hand this a stub with `asset`, `say`, `node` and `call`, which is exactly what the
    body and its callees touch.
    """
    try:
        entries = run.node().call("listtransactions", "*", funding_steps.FUNDING_SEARCH_DEPTH, 0)
    except (RPCError, OSError) as error:
        raise RegtestSetupError(
            f"could not read the wallet's recent transactions ({error}), so no payment to "
            f"{key.address} can be listed. Nothing else on this page depends on it."
        ) from error

    rows = []
    for payment in funding_steps.payments_to_the_funding_address(entries, key.address):
        try:
            outpoint = funding_steps.find_operator_funding(run, key, payment.txid)
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
        point = (outpoint.txid, outpoint.vout)
        cached = (known_spent or {}).get(point)
        if cached:
            spender, description = cached, "SPENT ALREADY -- remembered from an earlier look, not re-walked"
        else:
            # A NEGATIVE ANSWER IS NOT CACHED. IT IS EXTENDED. Measured on the operator's
            # screen 2026-09-30, and it is why they pressed Ctrl-C: the ONE usable payment was
            # re-walked in full on every page draw -- "scanning blocks 3298076 down to
            # 3296986", 1092 blocks, one getblock each -- and with the funding view and the
            # chain tab both fetching, two of those walks ran at once against the same daemon.
            # Eight candidates, seven long spent and cached, and the eighth cost 1092 RPC reads
            # a second forever.
            #
            # THE CORRECTNESS ARGUMENT IS UNCHANGED AND IS THE REASON THIS IS NOT A CACHE.
            # "Spent by X" is monotone, so remembering it can only turn a slow correct answer
            # into a fast one. "Not spent" is NOT monotone -- the next block can spend it -- so
            # remembering THAT would have the panel offering a payment the harness then
            # refuses. What is monotone is the pair ("unspent", the height it was established
            # at): a spend after height H must appear in a block after H, so re-examining
            # exactly the blocks since H is not a weaker check than re-walking all of them, it
            # is the same check with the already-examined part skipped (rule 3 prefers removing
            # work to doing it faster).
            #
            # The tip is read BEFORE the scan on purpose. If a block lands mid-scan the older
            # height is recorded, so that block is examined next time -- the union of the
            # scanned ranges always covers the full original depth plus every block since.
            # Reading it after would leave a one-block hole, which on a spend is the whole
            # answer.
            previous = (unspent_as_of or {}).get(point)
            tip = _tip_or_none(run)
            full = (payment.confirmations + 1 if isinstance(payment.confirmations, int)
                    and payment.confirmations >= 0 else funding_steps.MAX_SPEND_SCAN_BLOCKS)
            depth, since = scan_depth(previous, tip, full)
            spender, description = funding_steps.find_the_spender(run, outpoint, max_depth=depth)
            description += since
            if spender and known_spent is not None:
                known_spent[point] = spender
                if unspent_as_of is not None:
                    # It is spent now, so the unspent height is not merely stale, it is a
                    # statement that would be WRONG if it outlived this branch (rule 9).
                    unspent_as_of.pop(point, None)
            elif not spender and unspent_as_of is not None and tip is not None:
                unspent_as_of[point] = tip
        # str(), FOR THE REASON WRITTEN OUT AT funding_steps.funding_needed_coins(): PaymentRow
        # declares `value_coins: str` and this handed it a Decimal, and these rows go into
        # operator_panel.funding_payload()'s "rows" list, which is serialized by
        # `json.dumps(handler())` on GET /api/funding. A Decimal there raises TypeError, which
        # `guarded()` renders as a 500 in place of the funding table. Checked at the
        # interpreter rather than reasoned about. The rendered digits are unchanged:
        # str(Decimal) is what the f-string on the candidate line below already produced.
        value = str(funding_steps.satoshis_to_coins(outpoint.value_satoshis))
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


