"""The one place that turns Config.RPC into the adapters dict.

Role: submodule (chain registry; holds no decision about swaps, amounts or
      addresses -- it constructs and returns)
Reads: the RPC mapping it is handed. Nothing else: no environment, no file, no
      socket.
Writes: nothing
Can move funds: no -- it CONSTRUCTS adapters that can (RPCAdapter inherits
      send_to_address), and calls nothing on them. No constructor here opens a
      socket, which is a promise workers/common.py's header already makes on
      behalf of three polling workers.
Mainnet-safe: yes

WHY THIS FILE EXISTS: THERE WERE TWO COPIES OF IT (CLAUDE.md rule 8).

Measured 2026-09-25, before this commit, `build_adapters` existed twice:

    app.py:130            def build_adapters(app: Flask) -> dict   reads app.config["RPC"]
    workers/common.py:62  def build_adapters() -> dict             reads Config.RPC

Six lines each, the same three constructors, differing only in where the RPC
mapping came from. That is rule 8's exact shape -- "two copies of one rule is
not redundancy, it is a bug with a delay on it" -- and adding a fourth chain
was the moment it would have bitten: a Solana adapter added to one and not the
other gives an HTTP process that knows about SOL and three workers that do not,
with no error anywhere. The Flask app would hand out a SOL deposit address that
no deposit watcher is looking at.

So the difference is now a PARAMETER (the mapping) rather than a second copy,
and both callers pass their own source.

WHY EVERY CHAIN IS CONDITIONAL, AND WHY THREE OF THEM WERE NOT UNTIL 2026-09-26.

This section used to read "BTC, LTC and GRC are always constructed, because
config.Config always has an entry for them -- defaults and all, pointing at
mainnet ports, which config.py's own header says out loud." Every clause of that
was true, and together they were the bug: the entry existed, the default was the
MAINNET port, and so a checkout with nothing set built three adapters aimed at
real-money daemons. Saying it out loud in a header is not the same as failing
closed. The operator found it from the outside -- "we're still pulling from grc
mainnet wallet and not the testnet wallet" -- which is the shape CLAUDE.md rule
13 warns about: nothing crashed, nothing warned, and the only symptom was
somebody noticing.

All six are now conditional on the values that cannot be defaulted, and the
test is _REQUIRED_SETTINGS below -- one entry per chain, so there is one table to
read rather than a sentence that has to be kept in step with it.

(It said "all five" until 2026-10-09, counted when there were five. ICP landed
on 2026-10-06 and took the count to six -- BTC, LTC, GRC, SOL, XRP, ICP -- which
is exactly the drift the sentence above warns about in its own second clause: a
number in prose beside a table that grew. _REQUIRED_SETTINGS has six entries and
build_adapters() constructs six shapes; the table is the authority and this
count is the thing that aged.)

THAT SENTENCE USED TO READ "the one value that cannot be defaulted ... a URL for
SOL and XRP, a port for BTC, LTC, GRC", AND IT WAS WRONG BY TWO VARIABLES PER
CHAIN as of the 2026-09-26 credential fix two paragraphs below. Re-measured
2026-10-03 by calling the real function with a config built from an environment
carrying no BTC_RPC_* or LTC_RPC_* at all:

    missing_settings(Config.RPC, "BTC") -> ['BTC_RPC_PORT', 'BTC_RPC_USER', 'BTC_RPC_PASS']
    missing_settings(Config.RPC, "LTC") -> ['LTC_RPC_PORT', 'LTC_RPC_USER', 'LTC_RPC_PASS']

ONE for SOL and XRP (a URL), THREE for BTC, LTC and GRC (a port and both halves
of the HTTP credential). The header kept saying "one value" and naming only the
port while the code three lines down had been testing all three for a week --
which is the precise failure this file's own missing_settings() docstring warns
about on the operator's behalf: a message that names one variable where three
are needed sends a reader to check the setting that was already correct. A header
is read at a glance and trusted at a glance, so a stale one is cheaper to
believe than the code is to read (CLAUDE.md rule 16: a wrong comment is a bug).

config.py defaults the three ports to network_target.UNCONFIGURED_PORT and the
user/password pairs to "", so every key is PRESENT and falsiness is the test --
see missing_settings() below, which says why that is deliberate rather than
incidental.

The reason a missing port must SKIP rather than guess is the one the Solana
paragraph below already gives, plus a second one that only applies to the older
three: guessing wrong here does not merely produce a useless adapter, it
produces a WORKING adapter pointed at mainnet.

SOL is constructed only when SOL_RPC_URL is set, and that is not tidiness. The
adapters dict is walked by services/payout_service.refresh_wallet_inventory(),
which calls get_balance() on EVERY adapter every cycle and logs a WARNING for
each one that fails. An unconfigured Solana adapter would therefore print a
warning per worker per cycle, forever, describing a fault that does not exist
-- and a log that cries wolf is a log nobody reads the day something real
happens. CLAUDE.md rule 14's "make 'did nothing' look different from 'did
work'", applied to a warning that would always be there.

ADDING AN ADAPTER DOES NOT ENABLE A TRADING PAIR, and that separation is
deliberate. `Config.ALLOWED_PAIRS` decides what may be swapped and it is
UNCHANGED: no pair mentions SOL, so services/quote_service.py cannot produce a
SOL quote and services/swap_service.py cannot create a SOL swap. Enabling a
pair is live posture and is the operator's (CLAUDE.md rule 16); this file only
makes the chain reachable for reading.
"""

from __future__ import annotations

from collections.abc import Mapping

from network_target import configuring_variable

from .bitcoin import BitcoinAdapter
from .gridcoin import GridcoinAdapter
from .icp import ICPAdapter, dfx_transport
from .litecoin import LitecoinAdapter
from .solana import SolanaAdapter
from .xrp import XRPAdapter

# The three Bitcoin-derived chains, whose Config.RPC entries all have the same
# six keys and are splatted straight into RPCAdapter's matching signature.
#
# THE SAME THREE ARE DECLARED AS TYPES IN config.RpcSettings, which annotates
# BTC, LTC and GRC as config.BitcoinFamilyRpc and the other three as their own
# shapes (added 2026-10-09). Named here rather than imported from there on
# purpose: this module's header promises it reads "the RPC mapping it is handed.
# Nothing else: no environment, no file", and importing config would both break
# that and make `from config import Config` drag the whole adapter stack --
# requests included -- into every process that reads a setting. So the two are
# deliberately separate and each names the other (rule 8's "the difference
# belongs in a comment at BOTH sites"): config.RpcSettings is the shape, this is
# the constructor per asset, and config.bitcoin_family_rpc() is the accessor
# that gives a VARIABLE-keyed caller the Bitcoin-family six with a refusal
# attached.
_BITCOIN_DERIVED = {
    "BTC": BitcoinAdapter,
    "LTC": LitecoinAdapter,
    "GRC": GridcoinAdapter,
}


def _settings(rpc: Mapping[str, object], asset: str) -> Mapping:
    """One chain's settings out of the table, or {} when the table has no entry.

    WHY THE TABLE IS TYPED `Mapping[str, object]` AND NOT `Mapping[str, Mapping]`,
    which is what these three functions took until 2026-10-09. config.Config.RPC
    is now a TypedDict (config.RpcSettings), so each of its six values has the
    exact shape of the __init__ it is splatted into -- and the typing spec lets a
    TypedDict be read generically as `Mapping[str, object]` and NOTHING ELSE. A
    `Mapping[str, Mapping]` parameter would refuse the very table this module
    exists to consume, and the twenty-odd `build_adapters(Config.RPC)` call sites
    across the tree would each have to launder it on the way in.

    So the outer value type gives up the "each entry is a mapping" claim, and
    this function makes it again as a REAL check rather than an annotation --
    which is more than the old signature did, because `Mapping[str, Mapping]`
    with a bare inner `Mapping` was `Mapping[Any, Any]` and checked nothing about
    the keys or the values anyway.

    THREE OUTCOMES, AND THE MIDDLE ONE IS THE POINT:

      no entry at all     {}. Every caller treats that as unconfigured --
                          missing_settings() names the variables to export and
                          build_adapters() skips the chain -- which is the whole
                          refusal path this module's header is about.
      a mapping           itself, unchanged, for the caller to splat or read.
      anything else       TypeError naming the asset and what was found. Today
                          that case reaches `cls(**entry)` and raises
                          "argument after ** must be a mapping, not str", which
                          names neither the chain nor the table it came from.

    The return type is a bare `Mapping` because `**` needs a mapping whose value
    type is permissive: the four entries have four different shapes and this is
    the one function in the tree whose job is to be generic across them. The
    per-shape checking lives at the literal table in config.py and at the
    `**Config.RPC["SOL"]`-style call sites, which is where a reader can act on it.
    """
    entry = rpc.get(asset)
    if entry is None:
        return {}
    if not isinstance(entry, Mapping):
        raise TypeError(
            f"the RPC table's {asset!r} entry is a {type(entry).__name__}, not a mapping of settings, "
            f"so no {asset} adapter could be built. config.Config.RPC's entries are dicts built for "
            f"one adapter __init__ each -- see config.RpcSettings. Nothing was constructed."
        )
    return entry


def build_adapters(rpc: Mapping[str, object]) -> dict:
    """Construct every configured chain adapter. Opens no socket.

    `rpc` is Config.RPC, or app.config["RPC"], which is a copy of it.

    THE SOL ENTRY HAS A DIFFERENT SHAPE ON PURPOSE, and the splat is what makes
    that safe. Config.RPC["BTC"] is {user, password, host, port, wallet,
    timeout} -- a Bitcoin JSON-RPC connection. A Solana RPC endpoint has none
    of those: it is one URL, with no HTTP authentication and no wallet path,
    and forcing it into user/password/host/port would mean an adapter whose
    four most prominent parameters are all empty strings, plus a reassembly
    step that could silently produce "http://:0".

    So Config.RPC["SOL"] is {url, commitment, timeout, mint, hot_wallet,
    min_commitment_rank} and SolanaAdapter.__init__ has exactly those names,
    which is the same contract RPCAdapter already has with its own six: the
    dict is built for the signature. Both are `**splatted` here, so a key added
    to one config entry without a matching parameter fails loudly at
    construction rather than being ignored.

    AND SINCE 2026-10-09 THAT CONTRACT IS CHECKED RATHER THAN DESCRIBED. The two
    paragraphs above were prose, and "fails loudly at construction" meant at
    RUNTIME, on whichever of this tree's ten splat sites ran first -- a worker's
    startup or an operator tool, never a test. config.py now declares the four
    shapes as TypedDicts (config.BitcoinFamilyRpc, SolanaRpc, XrpRpc, IcpRpc) and
    config.RpcSettings maps each asset to the one it uses, so the literal table
    is checked field by field where it is written. This function is still generic
    and still splats -- see _settings() above for why the parameter is
    `Mapping[str, object]` -- but a renamed key no longer gets as far as here.
    """
    # `and rpc[asset].get("port")` is the fix for 2026-09-26. These three used to
    # be constructed unconditionally, because config.Config always had an entry
    # for them -- and that entry carried a MAINNET port default, so a checkout
    # with nothing set built a Bitcoin adapter aimed at 8332, a Litecoin one at
    # 9332 and a Gridcoin one at 15715. refresh_wallet_inventory() then called
    # get_balance() on each every cycle, which is how the operator's live
    # Gridcoin staking wallet came to be polled on a loop by a terminal that was
    # supposed to be on testnet.
    #
    # The test is the same one SOL and XRP already use, three paragraphs
    # below: "configured" means the operator supplied the one value that cannot
    # be defaulted. For those it is a URL or a port; for these it is a port. The
    # module header's old claim that these three are "always constructed" was
    # accurate and is now wrong, so it has been rewritten rather than left to
    # mislead the next reader.
    # `not missing_settings(...)` rather than `rpc[asset].get("port")`, since
    # 2026-09-26: a port with no password builds an adapter that 401s on every
    # call. See _REQUIRED_SETTINGS BELOW in this file for the run that measured
    # it -- this said "above", which is the one direction a reader cannot find it
    # in, since the table is defined after this function rather than before it.
    # _settings() RATHER THAN rpc[asset], AND IT IS NOT A WRAPPER FOR ITS OWN
    # SAKE: the table is now a TypedDict (config.RpcSettings, 2026-10-09) and the
    # typing spec only lets one be read generically as Mapping[str, object], so
    # the per-entry "this is a mapping of settings" check moved from the
    # parameter annotation into that function, where it is enforced instead of
    # merely written. Its docstring has the three outcomes.
    adapters = {
        asset: cls(**_settings(rpc, asset))
        for asset, cls in _BITCOIN_DERIVED.items()
        if asset in rpc and not missing_settings(rpc, asset)
    }
    # Configured means "has a URL". See this module's header for why an
    # unconfigured Solana adapter is left out entirely rather than constructed
    # and left to warn on every inventory refresh.
    #
    # The `solana and` that used to guard this is gone because _settings()
    # already answers "no entry" with {}, and {}.get("url") is None: one test
    # rather than two for one question.
    solana = _settings(rpc, "SOL")
    if solana.get("url"):
        adapters["SOL"] = SolanaAdapter(**solana)

    # XRP is conditional on its URL, exactly as SOL is, and for the same
    # reason: an endpoint is the one value that cannot be defaulted.
    xrp = _settings(rpc, "XRP")
    if xrp.get("url"):
        adapters["XRP"] = XRPAdapter(**xrp)
    # ICP is gated on missing_settings() rather than on one key, because it needs
    # TWO values and either one alone is useless: a ledger with no owner cannot
    # derive a deposit address, and an owner with no ledger has nothing to ask.
    icp = _settings(rpc, "ICP")
    if icp and not missing_settings(rpc, "ICP"):
        # THE TRANSPORT IS BUILT HERE, NOT INSIDE THE ADAPTER (rule 10). How the
        # ledger is reached is deployment configuration -- which compose service, or
        # which replica url, with what timeout -- and the adapter needs a callable,
        # not those three values. Moved out of ICPAdapter.__init__ on 2026-10-07 when
        # adding network_url took it to six parameters and ruff refused; the lint was
        # pointing at the layering rather than at the count.
        #
        # network_url empty keeps `docker compose exec`, which works from the host.
        # Set (http://icp-replica:4943 for the containerized deployment) it runs dfx
        # in this process, which is the only transport available to the web container:
        # that image has no docker CLI and no daemon socket.
        adapters["ICP"] = ICPAdapter(
            icp["ledger_canister_id"],
            icp["owner_principal"],
            dfx_transport(icp.get("service", "icp-replica"), icp.get("timeout", 60.0), icp.get("network_url", "")),
        )
    return adapters


# WHAT EACH SHAPE OF CHAIN CANNOT DO WITHOUT, as (the key in Config.RPC[asset],
# the environment variable suffix). A None suffix means the name comes from
# network_target.configuring_variable(), which already owns the primary one.
#
# THE CREDENTIALS ARE REQUIRED AND THAT IS NOT A STYLE CHOICE, measured
# 2026-09-26. The operator's shell had GRC_RPC_PORT=25715 and GRC_RPC_USER set,
# and the password under GRC_TESTNET_RPC_PASS -- a name nothing in this tree
# reads. build_adapters() tested the port alone, so a Gridcoin adapter WAS
# constructed, with an empty password. chains/base.RPCAdapter authenticates with
# `auth=(self.user, self.password)` and has no cookie-file path, so every call
# through that adapter is a guaranteed 401 -- and the swap page, which had just
# been taught to badge a pair DISABLED when its chain has no adapter, would have
# badged this one ENABLED and let the operator walk into the 401.
#
# An empty user or password is therefore never usable through this code, which
# makes it exactly the same kind of value as an unset port: something that cannot
# be defaulted. The registry's own principle, stated below for SOL and XRP,
# is that "configured" means the operator supplied those values. The three oldest
# chains were checking one of three.

#: (config key, the environment variable name that sets it). None means "ask
#: network_target.configuring_variable()", which owns the primary name per chain.
#:
#: THE SECOND ELEMENT USED TO BE A SUFFIX appended to f"{asset}_RPC_", which worked
#: for exactly as long as every chain's variables were shaped <ASSET>_RPC_*. ICP's
#: are not -- it reads ICP_LEDGER_CANISTER_ID and ICP_OWNER_PRINCIPAL, with no port,
#: no user and no password -- and the suffix scheme produced
#: `['ICP_RPC_PORT', 'ICP_RPC_PORT']`: one name, repeated, for two settings, neither
#: of which is that and neither of which exists.
#:
#: That is the precise defect this file's own header complains about ("a message
#: that names one variable where three are needed sends a reader to check the
#: setting that was already correct"), so it is fixed by making the table hold the
#: real name rather than by adding a second override mechanism beside it (rule 8).
#: tests/test_offline_reason_names_the_variables.py greps config.py for every name
#: here, so a rename there fails a test instead of printing a variable nothing reads.
_REQUIRED_SETTINGS: dict[str, tuple[tuple[str, str | None], ...]] = {
    "BTC": (("port", None), ("user", "BTC_RPC_USER"), ("password", "BTC_RPC_PASS")),
    "LTC": (("port", None), ("user", "LTC_RPC_USER"), ("password", "LTC_RPC_PASS")),
    "GRC": (("port", None), ("user", "GRC_RPC_USER"), ("password", "GRC_RPC_PASS")),
    "SOL": (("url", None),),
    "XRP": (("url", None),),
    # TWO values, and neither has a safe default -- see config.py's ICP entry for
    # why both are empty strings rather than the mainnet ledger id and somebody's
    # principal. `service` IS defaulted (the compose service name) because guessing
    # it wrong produces an adapter that cannot reach anything, which is a loud
    # failure; guessing a canister id wrong produces an adapter that reaches the
    # WRONG ledger, which is a quiet one.
    #
    # The first is None because network_target.configuring_variable() owns the
    # primary name per chain and now knows ICP; the second is spelled out because
    # there is no suffix convention that would produce it.
    "ICP": (("ledger_canister_id", None), ("owner_principal", "ICP_OWNER_PRINCIPAL")),
}


def missing_settings(rpc: Mapping[str, object], asset: str) -> list[str]:
    """The environment variables this chain needs and does not have. [] is configured.

    Names rather than keys, because the answer goes to a person who has to export
    something. The primary one comes from network_target.configuring_variable() so
    it cannot drift from CHAIN_PORTS or from the workers' startup banner; the
    other names are spelled out in _REQUIRED_SETTINGS rather than derived, which
    config.py uses for all three Bitcoin-derived chains without exception -- grep
    `_env("BTC_RPC_USER"` and its five siblings in Config.RPC rather than trusting
    a line number. This cited "config.py:157-206", and on 2026-10-03 those lines
    were in the middle of the ALLOWED_PAIRS comment block: the real reads are in
    Config.RPC's BTC, LTC and GRC entries, which had moved about 160 lines down
    the file since the citation was written. A line number is a reference that
    rots on every edit ABOVE it, so this names the thing to search for instead.

    THE DERIVATION IS SPELLED HERE AND NOT IN config.py, which is the one piece of
    drift this function is exposed to: configuring_variable() is checked against
    config.py's source by
    tests/test_network_target.test_configuring_variable_matches_what_config_py_actually_reads(),
    but that test only ever covered the PRIMARY name. The two credential suffixes
    are now covered the same way by
    tests/test_offline_reason_names_the_variables.py, so a rename of
    BTC_RPC_PASS in config.py fails a test instead of producing a reason that
    names a variable nothing reads -- the exact shape of the 2026-09-26 incident
    recorded above, where the operator's value sat under GRC_TESTNET_RPC_PASS.

    Falsy rather than absent, on purpose: config.py defaults every one of these to
    "" or to UNCONFIGURED_PORT, so a key is always PRESENT and the question is
    always whether it carries a value.

    An asset this function does not know returns [] -- it has no requirements to
    fail. Callers that need a name for such a chain fall back to
    configuring_variable(), which never raises.
    """
    entry = _settings(rpc, asset)
    names = []
    for key, variable in _REQUIRED_SETTINGS.get(asset, ()):
        if not entry.get(key):
            names.append(configuring_variable(asset) if variable is None else variable)
    return names


def unconfigured_chains(adapters: Mapping[str, object], *assets: str) -> list[str]:
    """Which of these assets have no adapter in this process, in the order given.

    ONE LINE, AND IT EXISTS BECAUSE THE ALTERNATIVE WAS MEASURED ON A PERSON.

    2026-09-26, pasted back from the operator's browser. They had a Gridcoin
    testnet daemon running on 25715, three workers that printed
    `GRC rpc=127.0.0.1:25715` in their startup banners, a quote that priced
    1 XRP -> 56.6 GRC, and this on the page when they pressed Create swap:

        No swap was created: 'GRC'

    That is `str(KeyError("GRC"))`. create_swap() did `adapters[to_asset]`, the
    server process had no GRC_RPC_PORT in its environment, build_adapters()
    therefore skipped Gridcoin, and the subscript raised. routes/swaps.py's HTTP
    boundary caught it and returned `str(exc)`, which for a KeyError is the repr
    of the key and nothing else -- no chain, no cause, no remedy, and
    indistinguishable from a typo in a dictionary somewhere. The operator had no
    way to get from that string to "export GRC_RPC_PORT before starting the
    server", which is the entire content of the failure.

    A membership test rather than a try/except around the subscript: the caller
    needs the answer BEFORE it writes anything, and "which chains are missing" is
    a list, not an exception.
    """
    return [asset for asset in assets if asset not in adapters]


def why_unconfigured(asset: str, rpc: Mapping[str, object] | None = None) -> str:
    """One sentence an operator can act on, for a chain with no adapter.

    PASS `rpc` WHENEVER YOU HAVE IT. Without it this can only name the chain's
    primary setting, and on 2026-09-26 that would have been actively wrong: the
    operator's GRC_RPC_PORT WAS set to 25715, and what was missing was
    GRC_RPC_PASS -- they had the value under GRC_TESTNET_RPC_PASS, a name nothing
    in this tree reads. A message that said "GRC_RPC_PORT is unset" would have
    sent them to check the one variable that was already correct. With `rpc` it
    names exactly what missing_settings() found.

    NAMES EVERY MISSING VARIABLE, NOT ONE. missing_settings() returns a list and
    this joins all of it, because for a Bitcoin-derived chain there are THREE
    things that can be missing and naming only the first would send an operator
    back for a second round after exporting it. Measured 2026-10-03 against a
    config built with no BTC_RPC_* exported at all, which is the state the
    operator reported the page in:

        BTC has no adapter in this process: BTC_RPC_PORT, BTC_RPC_USER and
        BTC_RPC_PASS are unset (or 0) in the environment this process was started
        with. ...

    The primary name comes from network_target.configuring_variable() rather than
    being spelled here, so it cannot drift from the workers' startup banner or
    from config.py (rule 8 -- three copies of a variable name is a typo waiting).
    The two credential names are derived by missing_settings() above; that
    derivation is the one string in this path that is NOT taken from
    network_target, and it is pinned against config.py's source by
    tests/test_offline_reason_names_the_variables.py.

    The .env sentence is the part that actually resolves the 2026-09-26 incident
    and it is not incidental: config.py's own header says nothing in the serving
    path loads a .env "because adding load_dotenv() to a module read at import is
    the import-time side effect rule 12 names as a measured past defect". That is
    a deliberate choice, so the consequence -- the variable must be in the
    PROCESS environment of whatever starts the server, not merely in a file next
    to it -- belongs in the message rather than in a docstring the operator will
    never see.
    """
    missing = missing_settings(rpc, asset) if rpc is not None else []
    if not missing:
        # Either no rpc was passed, or the asset has no requirements registered.
        # configuring_variable() never raises, so this degrades to the primary
        # name rather than to a KeyError on a page.
        missing = [configuring_variable(asset)]
    named = missing[0] if len(missing) == 1 else ", ".join(missing[:-1]) + f" and {missing[-1]}"
    verb = "is" if len(missing) == 1 else "are"
    return (
        # THE SENTENCE WAS "Nothing in the serving path reads a .env" UNTIL 2026-10-10,
        # and that became imprecise the same day. config.read_env_file() now reads TWO
        # keys out of .env -- SWAP_DB_PATH and SWAP_DB_DIR -- because SWAP_DB_DIR and
        # SWAP_DB_PATH were two names for one fact and an operator who moved the database
        # moved the container and not the host tools.
        #
        # NO CREDENTIAL IS IN THAT ALLOWLIST AND THAT IS THE POINT, so the remedy for an
        # RPC variable is unchanged: it has to be exported. Saying "nothing reads a .env"
        # would now be false, and saying ".env is read" would be worse -- an operator
        # would put GRC_RPC_PORT in the file and watch it do nothing. So the line names
        # WHICH keys are read and why the rest are not, which is the only version that
        # leads to the right action.
        f"{asset} has no adapter in this process: {named} {verb} unset (or 0) in the environment this "
        f"process was started with. The serving path reads ONLY the database location out of .env "
        f"(SWAP_DB_PATH and SWAP_DB_DIR) and DELIBERATELY NEVER a credential -- reading those would arm "
        f"every process that imports Config -- so this has to be exported in the shell that starts the "
        f"server, or supplied by compose. A value set only in .env, or only in another shell, does not "
        f"reach here."
    )


def why_cannot_pay_out(adapters: Mapping[str, object], asset: str) -> str:
    """Why this chain cannot be a payout DESTINATION, or "" if it can.

    "CONFIGURED" AND "CAN PAY OUT" ARE DIFFERENT QUESTIONS, and conflating them cost
    an afternoon on 2026-09-26. The swap page had just been taught that a pair whose
    chain has no adapter is DISABLED. GRC -> XRP passed that test -- an XRP adapter
    exists and reaches the testnet -- and was badged ENABLED. But XRPAdapter holds no
    signing key and payout_service calls send_to_address() unarmed, so that payout
    RAISES: a customer would have sent GRC, had it credited, and been left with a
    swap in `failed` and their deposit already taken. Unconfigured is "we cannot
    reach it"; this is "we can reach it and still cannot pay you".

    A review the same day found the second hazard in the same pair: XRPAdapter's
    validate_address() accepts any X-address without verifying its checksum, so a
    typo would have been fixed as that swap's FINAL payout address. A chain that
    cannot be a destination never gets asked for one.

    FAIL-CLOSED, deliberately: getattr(adapter, "can_spend", False) treats an object
    that does not declare the attribute as unable to pay. The alternative -- assume
    it can -- means a new adapter that forgets the declaration is silently offered as
    a destination, which is the expensive direction of the two.

    Returns "" for an asset with NO adapter. That is a different problem and
    unconfigured_chains() already reports it; answering both here would print two
    reasons for one pair and leave the operator to work out which to act on.
    """
    adapter = adapters.get(asset)
    if adapter is None or getattr(adapter, "can_spend", False):
        return ""
    detail = getattr(adapter, "payout_refusal", "") or (
        "cannot pay out, and its adapter does not say why -- see chains/base.RPCAdapter.can_spend"
    )
    return f"{asset} {detail}"
