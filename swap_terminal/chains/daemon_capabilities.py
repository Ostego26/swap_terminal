#!/usr/bin/env python3
"""What each of the three Bitcoin-derived daemons CAN do, which Core release it came from, and how we know.

Role: submodule (one table of facts, and pure functions over it)
Reads: nothing. Every input is an argument. No chain, no file, no environment.
Writes: nothing.
Can move funds: no. Nothing here calls a daemon; it answers questions ABOUT daemons.
Mainnet-safe: yes to import and to call.

WHY THIS EXISTS. Operator instruction, 2026-10-09:

    don't forget grc is 2014 bitcoin and bitcoin is way more advanced now

    we probably should've pre-emptively mapped the grc and modern ltc and btc
    capabilities and differences to avoid compatibility and integration
    confusion and cross chain incommunicado.

They are right, and the evidence is that this knowledge ALREADY EXISTED in this
tree -- correct, hard-won, measured on their own daemons, and spelled in prose
across eleven files. Collected 2026-10-09:

    chains/base.py:339                 GRC is pre-0.17, no getaddressinfo, no gettxout
    chains/base.py:583,686             validateaddress carried wallet fields; 0.18 moved them
    chains/payout_quantization.py:211  GRC 5.5.1.0's AmountFromValue ROUNDS HALF UP
    modules/rpc_method_support.py:21   GRC has no gettxout, measured by `help`
    modules/script_chain.py:549        fundrawtransaction is Core 0.12, GRC lacks it
    modules/htlc_fee.py:232            LTC 0.21.4's dust limit is TEN TIMES Bitcoin's
    modules/htlc_rpc.py:377            `addresses` deprecated in 0.20, removed in 22.0
    regtest/funding_steps.py:505       uptime is Core 0.15; `help uptime` -> unknown command
    regtest/daemons.py:853             the same fact, written again one function away
    services/admin_view.py:1496        falls back to getinfo.testnet, "a boolean on the older build"
    services/custody_separation.py:234 getaddressinfo -> Method not found (-32601)
    fee_sweep.py:564                   the same -32601, recorded a third time

Eleven sites, and regtest/daemons.py:658 says out loud that "this one file held
two different" copies of the uptime fact. That is rule 8 exactly: the copies
agree the day they are written and nothing fails when one drifts. Worse for a
capability than for a constant, because the drift is invisible -- a route built
on a method one chain lacks works in testing on the two that have it.

So this file is the one place. It does not replace a single one of those
comments: each explains a local decision and should keep doing so. What it adds
is a place to ASK, so the twelfth site does not have to guess.

WHAT MAKES THIS DIFFERENT FROM modules/rpc_method_support.py, which is the
nearest thing and is NOT this (rule 8 asks for the difference at both sites, and
that file now names this one).

    rpc_method_support  "how loudly do I report this miss?" Its table holds ONLY
                        methods a caller already has a FALLBACK for -- its own
                        comment is explicit that adding one without a fallback
                        "would hide a real failure, which is rule 19's definition
                        of a patch". Its value is a log level.
    this file           "does this daemon have it at all, and what do I do
                        instead?" Its value is a FACT, and a capability with no
                        fallback anywhere belongs here precisely BECAUSE a caller
                        needs to know before building on it.

EVIDENCE IS A FIELD, AND THAT IS RULE 17 MADE STRUCTURAL. Some of these were
measured on the operator's own daemons -- `help gettxout` answering "unknown
command" is a reading. Others are Bitcoin Core release history, which is a
reason to believe and not a reading of THIS deployment. The two must never be
written in the same voice, so each row says which it is, and
`unverified_on_this_deployment()` lists the second kind for anyone who wants to
go and check.

THE DAEMON VERSIONS, as recorded elsewhere in this tree:

    BTC   Bitcoin Core 28.1.0    modules/htlc_rpc.py:37
    LTC   Litecoin Core 0.21.4   modules/htlc_rpc.py:37, htlc_fee.py:232
    GRC   Gridcoin 5.5.1.0       chains/base.py:744

Gridcoin forked from Bitcoin around 2014 and has been maintained since, so it is
not uniformly ancient: its SOURCE LAYOUT is modern (this tree cites
`src/rpc/server.cpp` and `src/wallet/rpcwallet.cpp`, directories that arrived in
Core 0.12 and 0.13), while its RPC SURFACE is pre-0.17. That split is the whole
trap -- "2014 Bitcoin" predicts the missing methods correctly and predicts the
file layout wrongly, so neither a blanket "it is old" nor a blanket "it has been
rebased" is safe to reason from. Only the row matters.
"""

from __future__ import annotations

from dataclasses import dataclass

#: The three Bitcoin-derived chains this file speaks for. XRP, SOL and ICP are
#: not Bitcoin-derived and have no Core release to compare against; asking about
#: them is a bug in the caller rather than a gap here, so it raises.
BITCOIN_FAMILY = ("BTC", "LTC", "GRC")

#: Evidence kinds, and the whole reason the field exists (rule 17: a reason to
#: believe is not the same as having checked, and the two must never be written
#: in the same voice).
MEASURED = "measured on the operator's own daemon"
RELEASE_HISTORY = "Bitcoin Core release history -- NOT checked against this deployment"


@dataclass(frozen=True)
class Capability:
    """One thing a daemon either has or does not, with its provenance.

    `absent_on` is listed rather than derived as "everything not in present_on",
    because the third state matters: a capability nobody has checked on LTC is
    not the same as one measured absent, and collapsing them would invent a
    reading (rule 17). A chain in neither tuple is UNKNOWN for that capability
    and `has()` says so by returning None.
    """

    name: str
    arrived_in: str
    present_on: tuple[str, ...]
    absent_on: tuple[str, ...]
    evidence: str
    instead: str
    recorded_at: str


#: THE MAP. Ordered by what it costs to get wrong rather than alphabetically.
CAPABILITIES: tuple[Capability, ...] = (
    Capability(
        name="multi-wallet HTTP endpoint (/wallet/<name>)",
        arrived_in="Bitcoin Core 0.17",
        present_on=("BTC", "LTC"),
        absent_on=("GRC",),
        evidence=RELEASE_HISTORY,
        instead=(
            "leave the chain's _RPC_WALLET empty so the URL stays bare. A pre-0.17 daemon "
            "serves one wallet at the root path and has no /wallet/<name> route at all, so a "
            "configured wallet name would be appended to every call and answered by nothing"
        ),
        recorded_at=(
            "2026-10-09, found by reading chains/base.py:518 after the operator's warning. "
            "LATENT, NOT LIVE: GRC_RPC_WALLET defaults to \"\" and the operator's own 403 came "
            "from a bare http://host:25779/, so no wallet path is being built today. It is one "
            "environment variable away from breaking every GRC call, and the failure would "
            "look like a transport problem rather than a configuration one"
        ),
    ),
    Capability(
        name="gettxout",
        arrived_in="before 0.17 on Bitcoin; never added to Gridcoin",
        present_on=("BTC", "LTC"),
        absent_on=("GRC",),
        evidence=MEASURED,
        instead=(
            "getrawtransaction, then gettransaction, then decoderawtransaction -- routes 2 to 4 "
            "of modules/htlc_rpc.lookup_contract_output(), which is why a miss costs one round "
            "trip and nothing else"
        ),
        recorded_at="2026-09-27, `help gettxout` -> \"unknown command: gettxout\"",
    ),
    Capability(
        name="getaddressinfo",
        arrived_in="Bitcoin Core 0.18",
        present_on=("BTC", "LTC"),
        absent_on=("GRC",),
        evidence=MEASURED,
        instead=(
            "validateaddress, which on a pre-0.18 daemon still carries the wallet fields "
            "(`ismine`, `account`) that 0.18 moved out of it. chains/base.py tries both and "
            "collects the reason for each, so a caller can tell a capability gap from a "
            "transport failure"
        ),
        recorded_at="getaddressinfo answers Method not found (rpc code -32601)",
    ),
    Capability(
        name="the `account` field",
        arrived_in="removed in Bitcoin Core 0.18 along with the accounts system",
        present_on=("GRC",),
        absent_on=("BTC", "LTC"),
        evidence=MEASURED,
        instead=(
            "nothing -- this is the one capability the OLD daemon has and the new ones do not, "
            "which is why the direction matters. getaddressinfo has no `account` field at all, "
            "so code reading it is GRC-only by construction"
        ),
        recorded_at="services/custody_separation.py:886,933",
    ),
    Capability(
        name="gettransaction with a block-hash third argument",
        arrived_in="Bitcoin Core 0.16",
        present_on=("BTC", "LTC"),
        absent_on=("GRC",),
        evidence=MEASURED,
        instead=(
            "the two-parameter form. chains/base.py:339 chose the hex route for exactly this "
            "reason: it needs no chain context, so it answers for a mempool transaction and "
            "one mined forty blocks ago with the same two calls"
        ),
        recorded_at="docs/atomic_swap_runs_2026_09_27.md, quoted at chains/base.py:335",
    ),
    Capability(
        name="fundrawtransaction",
        arrived_in="Bitcoin Core 0.12",
        present_on=("BTC", "LTC"),
        absent_on=("GRC",),
        evidence=MEASURED,
        instead="select inputs and compute change in this application -- modules/script_chain.py",
        recorded_at="modules/script_chain.py:549",
    ),
    Capability(
        name="uptime",
        arrived_in="Bitcoin Core 0.15",
        present_on=("BTC", "LTC"),
        absent_on=("GRC",),
        evidence=MEASURED,
        instead="getinfo, or treat liveness as answered by any successful call",
        recorded_at=(
            "`gridcoinresearchd -testnet help uptime` -> \"unknown command: uptime\" at block "
            "3295729 (regtest/funding_steps.py:503)"
        ),
    ),
    Capability(
        name="getinfo",
        arrived_in="deprecated in Bitcoin Core 0.16, removed in 0.18",
        present_on=("GRC",),
        absent_on=("BTC", "LTC"),
        evidence=MEASURED,
        instead=(
            "getblockchaininfo on a modern daemon. The direction is the trap: "
            "services/admin_view.py:1496 falls BACK to getinfo.testnet -- a boolean on the "
            "older build -- which is a fallback that only works on the old chain"
        ),
        recorded_at="services/admin_view.py:1496",
    ),
    Capability(
        name="rpcbind",
        arrived_in="Bitcoin Core 0.12",
        present_on=("BTC", "LTC"),
        absent_on=(),
        evidence=RELEASE_HISTORY,
        instead=(
            "nothing is needed: a pre-0.12 daemon binds its RPC port on all interfaces and "
            "`rpcallowip` is the only control. THIS IS WHY THE TWO FAILURE MODES DIFFER -- "
            "measured from the container 2026-10-09, GRC answered 403 Forbidden (it accepted "
            "the connection and declined the caller by IP) while BTC and LTC gave [Errno 111] "
            "Connection refused (nothing was accepting, because modern Core defaults to "
            "loopback only). GRC wants rpcallowip alone; BTC and LTC want rpcbind as well"
        ),
        recorded_at="2026-10-09. Whether gridcoinresearchd ACCEPTS an rpcbind line is UNTESTED here",
    ),
    Capability(
        name="rpcallowip in CIDR form (172.18.0.0/16)",
        arrived_in="Bitcoin Core 0.10; wildcards (172.18.*.*) were REMOVED in 0.12",
        present_on=("BTC", "LTC"),
        absent_on=(),
        evidence=RELEASE_HISTORY,
        instead=(
            "there is no safe form for both eras, which is why this row exists. A modern daemon "
            "REFUSES TO START on a wildcard; a pre-0.10 daemon does not understand CIDR. "
            "Gridcoin's tree carries the modern src/rpc layout, so CIDR is expected to parse -- "
            "expected, not measured. The daemon's own startup log is what says, and "
            "`swap_stack.py chains` is what proves it from where it matters"
        ),
        recorded_at="2026-10-09, while the operator was adding rpcallowip=172.18.0.0/16 for GRC",
    ),
    Capability(
        name="AmountFromValue rounding",
        arrived_in="Bitcoin Core rejects more than 8 decimals; Gridcoin 5.5.1.0 ROUNDS, half up",
        present_on=(),
        absent_on=(),
        evidence=MEASURED,
        instead=(
            "quantize before sending, per chain. This is not a method that is missing -- it is "
            "the SAME method behaving differently, which is the kind of difference a "
            "method-existence probe can never find. chains/payout_quantization.py owns it"
        ),
        recorded_at="chains/payout_quantization.py:211, traced to Gridcoin 5.5.1.0's AmountFromValue()",
    ),
    Capability(
        name="dust relay limit",
        arrived_in="not a version difference -- a per-chain policy constant",
        present_on=(),
        absent_on=(),
        evidence=MEASURED,
        instead=(
            "read it per chain. Litecoin Core 0.21.4's is TEN TIMES Bitcoin's, so a size that "
            "clears dust on BTC can be refused on LTC. modules/htlc_fee.DUST_RELAY_FEE_SAT_PER_KVB "
            "is the table"
        ),
        recorded_at="modules/htlc_fee.py:232, Litecoin Core 0.21.4",
    ),
)


def _capability(name: str) -> Capability:
    """The row called `name`. Raises rather than returning None.

    A caller asking about a capability this file does not record has either
    misspelled it or found a gap, and both want a loud answer: returning None
    would let `if not has(...)` read a typo as "the daemon lacks it", which is
    the shape chains/base.py's `except Exception: return None` cost a whole
    investigation for on 2026-08-08.
    """
    for capability in CAPABILITIES:
        if capability.name == name:
            return capability
    known = "\n  ".join(c.name for c in CAPABILITIES)
    raise KeyError(f"no capability recorded as {name!r}. Recorded:\n  {known}")


def has(asset: str, name: str) -> bool | None:
    """True, False, or None for "nobody has checked this chain for this".

    THREE-VALUED ON PURPOSE. Rule 17 is the reason: "not recorded" is not
    "absent", and a two-valued answer would turn every gap in this table into a
    measurement nobody took. Callers that must not proceed on an unknown should
    test `is True` rather than truthiness.
    """
    asset = asset.upper()
    if asset not in BITCOIN_FAMILY:
        raise KeyError(
            f"{asset} is not Bitcoin-derived; this file compares against Bitcoin Core releases "
            f"and speaks only for {', '.join(BITCOIN_FAMILY)}"
        )
    capability = _capability(name)
    if asset in capability.present_on:
        return True
    if asset in capability.absent_on:
        return False
    return None


def absence_note(asset: str, name: str) -> str:
    """One sentence an operator can act on: what is missing, since when, what instead.

    Rule 14's "state what the number means, next to the number", applied to a
    capability: "GRC has no gettxout" invites somebody to go and install
    something. It was never there, and the sentence has to say so.
    """
    capability = _capability(name)
    verdict = has(asset, name)
    if verdict is True:
        return f"{asset} HAS {capability.name} ({capability.arrived_in})."
    if verdict is None:
        return (
            f"whether {asset} has {capability.name} is NOT RECORDED -- nobody checked. "
            f"It arrived in {capability.arrived_in}. This says nothing either way."
        )
    return (
        f"{asset} does not have {capability.name}, and it was never there: it arrived in "
        f"{capability.arrived_in}. Instead: {capability.instead}. "
        f"Evidence: {capability.evidence} ({capability.recorded_at})."
    )


def serves_wallet_path(asset: str) -> bool | None:
    """Can this daemon be reached at /wallet/<name>? The one live trap, named.

    Its own function rather than a `has()` call at the two URL-building sites,
    because there ARE two -- chains/base.RPCAdapter.url and
    chains/daemon_conf.rpc_url() -- and both append the path for any chain whose
    wallet is configured, with no chain test between them. That is rule 8's two
    copies of one rule, and the rule they are both missing is this one.
    """
    return has(asset, "multi-wallet HTTP endpoint (/wallet/<name>)")


def wallet_path_warning(asset: str, wallet: str) -> str:
    """"" when this configuration is fine, or the sentence saying why it cannot work.

    Returns a STRING RATHER THAN RAISING, and that is rule 16's line. A refusal
    here would be an order-path behavior change: GRC payouts are made through
    this URL, and a session that cannot see the operator's .env must not decide
    that their live configuration should stop working. So this reports, the
    caller logs it at startup, and turning it into a refusal is the operator's
    call -- named in OPEN_FINDINGS rather than shipped.
    """
    if not wallet.strip():
        return ""
    if serves_wallet_path(asset) is False:
        return (
            f"{asset}_RPC_WALLET is set to {wallet.strip()!r}, so every {asset} RPC call will go "
            f"to /wallet/{wallet.strip()} -- and {absence_note(asset, 'multi-wallet HTTP endpoint (/wallet/<name>)')}"
        )
    return ""


def unverified_on_this_deployment() -> tuple[Capability, ...]:
    """Every row whose evidence is release history rather than a reading.

    THE POINT OF THE EVIDENCE FIELD, made callable. These are the rows somebody
    should go and check against the operator's own daemons, and until they do,
    nothing may report them in the voice of a measurement (rule 17).
    """
    return tuple(c for c in CAPABILITIES if c.evidence == RELEASE_HISTORY)


def differences_for(asset: str) -> tuple[str, ...]:
    """Every recorded way `asset` differs from its siblings, as sentences.

    DERIVED FROM THE ONE TABLE, so a capability added above appears here with no
    edit (rule 11's shape). This is what the operator asked for -- the map, read
    per chain -- and it is generated rather than written a second time.
    """
    asset = asset.upper()
    return tuple(
        absence_note(asset, capability.name)
        for capability in CAPABILITIES
        # BOTH tuples non-empty is what makes it a DIFFERENCE rather than a fact
        # about all three: AmountFromValue rounding and the dust limit are real
        # and are not "LTC has it and GRC does not", so they belong in
        # CAPABILITIES and not in a per-chain difference list.
        if capability.present_on
        and capability.absent_on
        and asset.upper() in capability.present_on + capability.absent_on
    )

# =============================================================================
# THE EQUIVALENCE TREE
#
# Operator, 2026-10-09: "yeah a tree of equivalence betwen rpc comamnds for ltc,
# btc, and grc sounds bout right".
#
# CAPABILITIES above answers "does this daemon have it". This answers the
# question a caller actually has: "I need to do T -- what do I call?" Keyed by
# the JOB, because the job is the thing that is the same across chains and the
# method name is the thing that is not.
#
# EVERY ROW NAMES THE FUNCTION THAT ALREADY RESOLVES THE DIVERGENCE, and that is
# the design rather than a convenience. Six of these are already handled
# correctly somewhere in this tree -- chain_network() reads getblockchaininfo
# then getinfo.testnet, lookup_contract_output() walks four routes, base.py
# tries getaddressinfo then validateaddress. A table that re-implemented any of
# them would be rule 8's second spelling of a rule that already works, and the
# second spelling is the one that drifts. So this maps to code: the `resolver`
# field is where the decision lives, and a caller who finds their job here
# should CALL that rather than branch on the asset themselves.
#
# A ROW WITH NO RESOLVER IS THE INTERESTING KIND. It means the divergence is
# real and nothing yet absorbs it, so a caller must handle it or avoid it -- the
# wallet path is exactly that, which is how it was found.
#
# MEASURED FIRST: the three atomic clients call an IDENTICAL method set --
# decodescript, getblockcount, getreceivedbyaddress, listunspent,
# sendrawtransaction, sendtoaddress (counted across atomic_btc_client.py,
# atomic_ltc_client.py and atomic_grc_client.py, 2026-10-09). That is not luck;
# it is the pre-0.17 common surface, chosen deliberately, and it is why the
# swap path works on all three at all. The rows below are the places the code
# could NOT stay on that surface. The tree is small because the common
# denominator was picked well, and that is worth knowing before anybody
# "modernizes" a call.
# =============================================================================


@dataclass(frozen=True)
class Equivalence:
    """One job, and what each chain is called with to do it.

    `calls` maps asset -> the ordered calls that do the job on that daemon. An
    asset absent from the mapping means this job is not done on that chain here,
    which is different from "no equivalent exists" -- the comment says which.
    """

    job: str
    calls: dict[str, tuple[str, ...]]
    resolver: str
    note: str


#: Every job where the three daemons are NOT called the same way.
EQUIVALENTS: tuple[Equivalence, ...] = (
    Equivalence(
        job="which network is this daemon on",
        calls={
            "BTC": ("getblockchaininfo.chain",),
            "LTC": ("getblockchaininfo.chain",),
            "GRC": ("getinfo.testnet",),
        },
        resolver="chains/daemon_network.chain_network()",
        note=(
            "getinfo.testnet is a BOOLEAN on the old build, so False means mainnet -- not "
            "'unknown'. chain_network() reads the modern field first and falls back, and "
            "returns 'unknown' when both fail rather than guessing testnet. Modern Core removed "
            "getinfo in 0.18, so the fallback only ever fires on GRC"
        ),
    ),
    Equivalence(
        job="find the unspent output a contract paid to",
        calls={
            "BTC": ("gettxout",),
            "LTC": ("gettxout",),
            "GRC": ("getrawtransaction", "gettransaction", "decoderawtransaction"),
        },
        resolver="modules/htlc_rpc.lookup_contract_output()",
        note=(
            "four routes, tried in order, and route 1 ALWAYS misses on GRC -- every spend. "
            "That miss is why modules/rpc_method_support.py exists: at ERROR with a stack it "
            "printed ~40 lines in front of a spend that then succeeded"
        ),
    ),
    Equivalence(
        job="is this address valid, and is it ours",
        calls={
            "BTC": ("getaddressinfo", "validateaddress"),
            "LTC": ("getaddressinfo", "validateaddress"),
            "GRC": ("validateaddress",),
        },
        resolver="chains/base.py -- tries both and collects the reason for each",
        note=(
            "0.18 moved the wallet fields OUT of validateaddress into getaddressinfo, so the "
            "same method answers different shapes on the two eras. GRC's validateaddress still "
            "carries `ismine` and `account`; getaddressinfo has no `account` field at all. "
            "Both are attempted on every chain so neither era is special-cased by asset"
        ),
    ),
    Equivalence(
        job="build a transaction with inputs and change selected",
        calls={
            "BTC": ("fundrawtransaction",),
            "LTC": ("fundrawtransaction",),
            "GRC": ("listunspent", "createrawtransaction"),
        },
        resolver="modules/script_chain.py -- selection and change computed here",
        note=(
            "fundrawtransaction arrived in Core 0.12. Doing it in the application is not a "
            "workaround for GRC alone: it is what makes the three paths identical, which is why "
            "the atomic clients share one method set"
        ),
    ),
    Equivalence(
        job="is the daemon alive, and for how long",
        calls={"BTC": ("uptime",), "LTC": ("uptime",), "GRC": ("getinfo",)},
        resolver="regtest/daemons.py -- probes with `help <method>` before calling",
        note=(
            "uptime arrived in Core 0.15. method_exists() asks the daemon BEFORE the call, which "
            "is the opposite direction from rpc_method_support.py's after-the-fact reading; each "
            "names the other and they must not disagree about which absences are expected"
        ),
    ),
    Equivalence(
        job="reach a named wallet",
        calls={
            "BTC": ("POST http://host:port/wallet/<name>",),
            "LTC": ("POST http://host:port/wallet/<name>",),
            "GRC": ("POST http://host:port/  -- one wallet, at the root",),
        },
        resolver="",
        note=(
            "NO RESOLVER, WHICH IS WHY THIS ROW WAS WORTH WRITING. chains/base.RPCAdapter.url "
            "and chains/daemon_conf.rpc_url() BOTH append /wallet/<name> whenever a wallet is "
            "configured, for any chain, with no test between them -- two copies of one rule, and "
            "the rule they are both missing is that the endpoint is Core 0.17+. Latent today "
            "because GRC_RPC_WALLET defaults to empty; wallet_path_warning() reports it and "
            "turning that into a refusal is the operator's call (rule 16)"
        ),
    ),
    Equivalence(
        job="send an exact decimal amount",
        calls={
            "BTC": ("sendtoaddress -- rejects >8 decimals",),
            "LTC": ("sendtoaddress -- rejects >8 decimals",),
            "GRC": ("sendtoaddress -- ROUNDS >8 decimals, half up",),
        },
        resolver="chains/payout_quantization.py",
        note=(
            "THE SAME METHOD NAME, DIFFERENT BEHAVIOR, which is the kind of difference a "
            "method-existence probe can never find and the reason this tree is keyed by job "
            "rather than by method. Quantize before sending, per chain"
        ),
    ),
)


def _equivalence(job: str) -> Equivalence:
    """The row for `job`. Raises, for the reason _capability() does."""
    for row in EQUIVALENTS:
        if row.job == job:
            return row
    known = "\n  ".join(r.job for r in EQUIVALENTS)
    raise KeyError(f"no equivalence recorded for {job!r}. Recorded jobs:\n  {known}")


def calls_for(asset: str, job: str) -> tuple[str, ...]:
    """The ordered calls that do `job` on `asset`, or () if this chain does not.

    () RATHER THAN A RAISE, because "this job is not done on this chain here" is
    a legitimate answer -- and the note on the row says whether that is because
    no equivalent exists or because nothing needed it yet.
    """
    asset = asset.upper()
    if asset not in BITCOIN_FAMILY:
        raise KeyError(
            f"{asset} is not Bitcoin-derived; {', '.join(BITCOIN_FAMILY)} only"
        )
    return _equivalence(job).calls.get(asset, ())


def jobs_that_diverge() -> tuple[str, ...]:
    """Every job where the three are not called identically. DERIVED, not listed.

    A row whose calls are the same for all three would be a row that did not
    need to exist, and this is what says so: it filters on the actual mapping
    rather than trusting that every row earns its place.
    """
    return tuple(
        row.job
        for row in EQUIVALENTS
        if len({tuple(row.calls.get(a, ())) for a in BITCOIN_FAMILY}) > 1
    )


def unresolved_divergences() -> tuple[Equivalence, ...]:
    """Rows nothing in this tree absorbs yet. The ones a caller must handle.

    The interesting half of the table. A job with a resolver is handled; a job
    without one is a trap waiting for the next caller, and the wallet path is
    how this function came to exist.
    """
    return tuple(row for row in EQUIVALENTS if not row.resolver)
