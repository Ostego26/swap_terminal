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
RUNNABLE: dict[str, tuple[str, list[str]]] = {
    "grc_htlc_verify": (
        "Does OP_CHECKLOCKTIMEVERIFY execute on Gridcoin? ~10 minutes; it waits 6 real blocks.",
        ["python3", "grc_htlc_verify.py"],
    ),
    "adaptor_regtest_verify": (
        "All five protocol transactions on GRC testnet, including the adaptor join.",
        ["python3", "adaptor_regtest_verify.py", "--chain", "grc"],
    ),
    "reclaim_dry_run": (
        "What is recoverable at the funding address. Broadcasts NOTHING -- no --send.",
        ["python3", "reclaim_funding.py", "--to-wallet", "--chain", "grc"],
    ),
}


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
    "GRC": {"accent": "#1f7a3d", "dark": "#6ee7a0", "glyph": "G", "unit": "GRC"},
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
    kind: str          # "operator" | "regtest" | "none"
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
#:   none       no adapter in this panel. XMR, XRP and SOL are swapped by other entry points
#:              with their own clients; saying so beats an empty tab or a missing one.
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
    ChainTab("XMR", "none", False,
             "no adapter in this panel. Monero is reached by monero_regtest.py and "
             "monero_shared_key_verify.py, which speak monero-wallet-rpc rather than a "
             "Bitcoin-style JSON-RPC, so nothing here can probe it without a second client."),
    ChainTab("XRP", "none", False,
             "no adapter in this panel. The XRP Ledger is reached by xrp_chain_check.py and "
             "xrp_htlc_escrow.py through chains/xrp.py."),
    ChainTab("SOL", "none", False,
             "no adapter in this panel, and get_new_address() refuses on Solana by design -- "
             "the custody choice there is the operator's and is not decided in this tree."),
)


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
    if tab.kind == "none":
        return {"asset": tab.asset, "kind": tab.kind, "note": tab.note,
                "reachable": False, "network": "", "error": "", "methods": [], "funding": None}
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
    return rows
