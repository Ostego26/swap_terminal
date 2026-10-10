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

from .coin_amounts import CHAIN_DECIMALS

#: The Bitcoin-derived chains this file speaks for. XRP, SOL and ICP are not
#: Bitcoin-derived and have no Core release to compare against; asking about them
#: is a bug in the caller rather than a gap here, so it raises.
#:
#: DERIVED FROM coin_amounts.CHAIN_DECIMALS, AND THE FIRST VERSION OF THIS LINE
#: WAS `("BTC", "LTC", "GRC")` -- which made it the SIXTH spelling of that tuple,
#: written in the commit whose entire purpose was consolidating duplicated chain
#: knowledge. config.py:308 and workers/common.py:108 both already say it is
#: spelled in five places, and config.py:1339 records that there is deliberately
#: no tuple in that file "-- rule 8 counts copies". I added one anyway, and found
#: it only by going looking afterwards, which is the whole argument for rule 9's
#: "every time you are in a file, leave less of it behind".
#:
#: CHAIN_DECIMALS is the right authority rather than the nearest one. Its own
#: docstring says XRP and SOL are "DELIBERATELY ABSENT" because both convert to
#: integer base units before sending, so no decimal string ever reaches those
#: RPCs -- which is a property of NOT being a Bitcoin-family daemon, stated at
#: the only place that had to decide it. Its keys are therefore the same set this
#: file needs, for the same underlying reason rather than by coincidence.
#:
#: ORDER IS PRESERVED and matters elsewhere: wallet_custody.py:805 does
#: `enumerate(SCRIPT_CHAINS, start=1)` for a numbered display and
#: icp_custody_addresses.py loops twice. dict insertion order makes
#: tuple(CHAIN_DECIMALS) == ("BTC", "LTC", "GRC"), asserted in
#: tests/test_daemon_capabilities.py rather than assumed.
#:
#: THE OTHER FIVE ARE NOT MERGED INTO THIS, and that is rule 8's harder half
#: rather than laziness: "If they genuinely differ, the difference is the point
#: and belongs in a comment at BOTH sites, naming the other one." They are five
#: DIFFERENT concepts whose membership happens to coincide today --
#:
#:   wallet_custody.SCRIPT_CHAINS            custody is by WALLET (XRP/SOL by account)
#:   modules/atomic_swapper.SUPPORTED_ASSETS pairs the script swapper can do
#:   show_payout_fees.MEASURABLE             payout fee is measurable from here
#:   icp_custody_addresses._P2PKH_CHAINS     has legacy P2PKH addresses
#:   this                                    has a Bitcoin Core release to compare to
#:
#: -- and they can diverge: a bech32-only Bitcoin fork would be in SCRIPT_CHAINS
#: and not in _P2PKH_CHAINS. Collapsing them would be asserting an identity
#: nobody has established. What was missing is that none of them named the
#: others, so the coincidence looked like a copy; the test asserts they agree
#: TODAY, which makes a future divergence deliberate instead of accidental.
BITCOIN_FAMILY: tuple[str, ...] = tuple(CHAIN_DECIMALS)

#: Evidence kinds, and the whole reason the field exists (rule 17: a reason to
#: believe is not the same as having checked, and the two must never be written
#: in the same voice).
MEASURED = "measured on the operator's own daemon"
RELEASE_HISTORY = "Bitcoin Core release history -- NOT checked against this deployment"
#: A THIRD KIND, ADDED 2026-10-09, AND THE REASON IS THE SAME MISTAKE TWICE IN ONE DAY.
#: The rpcallowip row below was settled by cloning gridcoin-community/Gridcoin-Research,
#: diffing master's ClientAllowed() and WildcardMatch() against tag 5.5.1.0 to confirm they
#: are byte-identical, compiling those two functions verbatim, and RUNNING them on the
#: operator's exact config. That is far stronger than release history -- it is the chain's
#: own code executing -- and it is still NOT the operator's binary, which could have been
#: built from anywhere in history.
#:
#: Filing it as MEASURED would have claimed their daemon was tested. Filing it as
#: RELEASE_HISTORY would have left the row reading "NOT checked" when the decisive check had
#: been done. Both are the three-outcomes-collapsed-to-two defect this session already
#: shipped once today, in stack_authority's renderer, and rule 17 is the general form: a
#: reason to believe and a measurement must never be written in the same voice -- which
#: needs a voice for each.
UPSTREAM_SOURCE = (
    "read AND executed from the chain's own source at a named tag -- not this deployment's binary"
)


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
    #: WHAT TO DO ON A CHAIN THAT **HAS** THIS AND IS NOT USING IT. Empty for most rows,
    #: and the distinction from `instead` is not a nicety -- it was a wrong remedy on the
    #: operator's screen during an outage on 2026-10-10.
    #:
    #: `instead` answers "this chain LACKS the capability; what do you do without it",
    #: so the rpcbind row's instead is written from GRC's point of view and opens
    #: "nothing is needed". refusal_remedy() mapped an LTC `Connection refused` to that
    #: row and printed it verbatim, so an operator whose litecoind had just failed to
    #: bind read "nothing is needed" as their remedy. The row was right; the field
    #: answered a different question than the one being asked.
    #:
    #: So the question is now explicit in the field name. A chain in `present_on` that is
    #: nonetheless failing needs to know how to USE the thing it has; a chain in
    #: `absent_on` needs `instead`. One home for each sentence (rule 8), and the mapping
    #: picks by whether the asset is listed as having it.
    when_unused: str = ""


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
        when_unused=(
            "modern Core binds its RPC port to LOOPBACK ONLY until `rpcbind` says otherwise, "
            "so `Connection refused` from the container means nothing was accepting -- not that "
            "the caller was declined. Put BOTH under the running network's section, which is "
            "`[regtest]` on regtest, `[test]` on Litecoin testnet, and `[testnet4]` on Bitcoin "
            "Core 28+ testnet4: rpcbind=127.0.0.1 and rpcbind=172.17.0.1 (the address "
            "host.docker.internal resolves to), plus rpcallowip=127.0.0.1 and "
            "rpcallowip=172.18.0.0/16 (the container's own subnet). Core ERRORS OUT if rpcbind "
            "is given without rpcallowip, so the two go in together. THE SECTION IS THE HALF "
            "THAT GOES WRONG SILENTLY: a header that does not match the running network means "
            "every line under it is skipped, the daemon starts cleanly, and nothing anywhere "
            "says so -- which cost five rounds on GRC on 2026-10-09 for the same reason."
        ),
        recorded_at=(
            "2026-10-09. THE OPEN QUESTION ON THIS ROW IS NOW CLOSED and the answer is "
            "stronger than the row expected: `-rpcbind` does not exist in Gridcoin's release "
            "line AT ALL -- `grep -rn rpcbind src/` on master returns nothing, and "
            "src/init.cpp:602-605 declares only -rpcallowip and -rpcconnect. So a gridcoinresearch.conf "
            "carrying an rpcbind line gets an unknown-argument, not a narrowed bind. The "
            "widening is implicit instead: src/rpc/server.cpp:661-662 computes "
            "`loopback = !IsArgSet(\"-rpcallowip\")` and binds address_v6::any() with "
            "v6_only(false) when ANY rpcallowip is set, which is the `LISTEN *:25779` the "
            "operator sees. The corollary matters: deleting the last rpcallowip line moves the "
            "socket back to loopback, so there is no \"allow nobody\" state that still listens"
        ),
    ),
    Capability(
        name="one config file with [network] sections",
        arrived_in="Bitcoin Core 0.17, which added [main]/[test]/[regtest] sections",
        present_on=("BTC", "LTC"),
        absent_on=("GRC",),
        evidence=UPSTREAM_SOURCE,
        instead=(
            "GRIDCOIN USES A SEPARATE FILE PER NETWORK and a testnet daemon reads "
            "<datadir>/testnet/gridcoinresearch.conf, NOT <datadir>/gridcoinresearch.conf. "
            "There is no section syntax to add and none is needed; put the lines in the "
            "net-specific file. The daemon's own startup log names the directory it chose -- "
            "`Using data directory <datadir>/testnet` -- which is the only reliable way to "
            "know, and it prints it every run"
        ),
        recorded_at=(
            "2026-10-09, AND THIS ROW IS THE MOST EXPENSIVE THING IN THIS FILE SO FAR: it cost "
            "five rounds on the operator's host, every one of them editing the wrong file.\n\n"
            "src/util/system.cpp:811-815 at tag 5.5.1.0:\n\n"
            "    fs::path GetConfigFile(const std::string& confPath)\n"
            "    {\n"
            "        // Unlike in Bitcoin, the net specific flag is TRUE, because we still use\n"
            "        // split config files.\n"
            "        return AbsPathForConfigVal(fs::path(confPath), true);\n"
            "    }\n\n"
            "net_specific=true makes GetDataDirPath append BaseParams().DataDir(), which is "
            "\"testnet\" (src/chainparamsbase.cpp:35). ReadConfigFiles opens that ONE path "
            "(src/util/system.cpp:918-921) with NO fallback to the base datadir, and a missing "
            "file is silently fine -- \"ok to not have a config file\", line 929.\n\n"
            "MEASURED ON THE OPERATOR'S HOST, and the two timestamps are the whole finding:\n\n"
            "    <datadir>/gridcoinresearch.conf          modified 2026-10-09 16:21  <- edited\n"
            "    <datadir>/testnet/gridcoinresearch.conf   modified 2026-10-04 14:22  <- READ\n\n"
            "    base conf     rpcallowip=127.0.0.1, rpcallowip=172.18.*\n"
            "    testnet conf  rpcallowip=127.0.0.1          <- loopback only, five days old\n\n"
            "Adding the subnet to the TESTNET file turned the container's 403 into a 200 on the "
            "first try.\n\n"
            "THE SYMPTOM IS INDISTINGUISHABLE FROM A SYNTAX ERROR, which is why this cost what "
            "it did. Loopback kept working because 127.0.0.0/8 is hardcoded allowed "
            "(src/rpc/server.cpp:521-526), and the stale file's own rpcallowip=127.0.0.1 made "
            "IsArgSet(\"-rpcallowip\") true -- which is what widens the bind at "
            "src/rpc/server.cpp:661 -- so the daemon showed LISTEN *:25779 and a 403 exactly as "
            "a daemon with an unparseable subnet would. Every piece of evidence was consistent "
            "with the wrong hypothesis.\n\n"
            "THE CONVENTION IS INVERTED BETWEEN THESE DAEMONS AND GETTING IT BACKWARDS FAILS "
            "DIFFERENTLY IN EACH DIRECTION: a [regtest] section in a gridcoinresearch.conf is "
            "read as a key nobody declared and does nothing, while GRC's net-specific file "
            "layout applied to bitcoin.conf means the daemon reads a conf with no RPC settings "
            "at all. Pair this row with the rpcallowip CIDR row below -- same two chains, "
            "opposite answers, and a single edit needs BOTH right"
        ),
    ),
    Capability(
        name="rpcallowip in CIDR form (172.18.0.0/16)",
        arrived_in=(
            "Bitcoin Core 0.10, and wildcards were REMOVED in 0.12. On GRIDCOIN it arrived in "
            "commit 924f36eb, 2026-08-23, which is on `development` and tag 5.5.1.7-testnet "
            "ONLY -- not master, and not in any mainnet release"
        ),
        present_on=("BTC", "LTC"),
        absent_on=("GRC",),
        evidence=UPSTREAM_SOURCE,
        instead=(
            "on GRC write a WILDCARD: `rpcallowip=172.18.*`. It is exactly equivalent to "
            "172.18.0.0/16 and does not over-match -- the mask is the seven characters "
            "`172.18.` INCLUDING the trailing dot, so 172.180.1.1 and 172.181.0.3 are both "
            "refused. Gridcoin's own contrib/docker/entrypoint.sh ships 10.*.*.*, 172.*.*.* "
            "and 192.168.*.* for this reason and its README says so in one line: \"Gridcoin "
            "uses wildcard matching for rpcallowip (not CIDR notation)\". "
            "There is NO form that is safe on both eras, which is why this row exists: a "
            "post-0.12 daemon refuses a wildcard and the GRC release line cannot read CIDR"
        ),
        recorded_at=(
            "2026-10-09. THIS ROW USED TO SAY \"CIDR is expected to parse -- expected, not "
            "measured\" AND THE EXPECTATION WAS WRONG, which is the argument for the evidence "
            "field existing at all. It cost the operator most of a day: five rounds of adding "
            "rpcallowip=172.18.0.0/16 to a conf, restarting, and getting the same 403.\n\n"
            "WHAT ACTUALLY HAPPENS, from src/rpc/server.cpp:528-533 on master, diff-verified "
            "byte-identical to tag 5.5.1.0 and then COMPILED AND RUN on the operator's exact "
            "config:\n\n"
            "    const string strAddress = address.to_string();\n"
            "    const vector<string>& vAllow = gArgs.GetArgs(\"-rpcallowip\");\n"
            "    for (auto const& strAllow : vAllow)\n"
            "        if (WildcardMatch(strAddress, strAllow))\n"
            "            return true;\n"
            "    return false;\n\n"
            "The entry is never PARSED. It is glob-matched against the peer's address TEXT by "
            "util.cpp:143's WildcardMatch, where `/` is a literal character -- so "
            "172.18.0.0/16 can only match a peer whose address string is literally "
            "172.18.0.0/16, which no peer's ever is. The upstream commit that fixed it says "
            "the same thing: \"matches no address string, ever... with nothing in the log to "
            "say why\".\n\n"
            "TWO THINGS THAT MADE THIS EXPENSIVE TO DIAGNOSE, both worth knowing before the "
            "next one:\n"
            "  - 127.0.0.0/8 IS HARDCODED ALLOWED at src/rpc/server.cpp:521-526, BEFORE the "
            "    allow list is consulted. So `rpcallowip=127.0.0.1` grants nothing, and "
            "    loopback working carries ZERO information about whether any other line "
            "    parsed -- it works identically with `rpcallowip=garbage` beside it. Half this "
            "    investigation leaned on loopback as evidence and it never was any.\n"
            "  - the release line LOGS NOTHING about rpcallowip, so the config error is "
            "    invisible. That absence is itself the build discriminator: a CIDR-capable "
            "    Gridcoin logs one line per entry at startup, so no such lines means "
            "    wildcard-only. The operator's log had none.\n\n"
            "The v4-mapped-over-v6 hypothesis was WRONG and is recorded so nobody re-chases "
            "it: the daemon does bind dual-stack and the peer does arrive as "
            "::ffff:172.18.0.3, but src/rpc/server.cpp:510-519 lifts bytes 12-15 into an "
            "address_v4 and recurses BEFORE any matching, so to_string() is already "
            "\"172.18.0.3\" at line 528. Measured identical verdicts for both spellings in "
            "every config tried"
        ),
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


#: Evidence kinds that are NOT this deployment's own daemon. UPSTREAM_SOURCE belongs
#: here even though it is the strongest non-local evidence there is: the operator's
#: binary could have been built from any commit, and the rpcallowip row is itself the
#: proof that the boundary matters -- CIDR works on `development` and not on the release
#: line, so "the source says" is only an answer once you know which source.
NOT_THIS_DEPLOYMENT = (RELEASE_HISTORY, UPSTREAM_SOURCE)


def unverified_on_this_deployment() -> tuple[Capability, ...]:
    """Every row not established against the operator's OWN daemon.

    THE POINT OF THE EVIDENCE FIELD, made callable. These are the rows somebody
    should go and check against the operator's own daemons, and until they do,
    nothing may report them in the voice of a measurement (rule 17).

    WIDENED 2026-10-09 FROM `== RELEASE_HISTORY` when UPSTREAM_SOURCE arrived. An
    `== RELEASE_HISTORY` test would have silently dropped every upstream-source row
    out of this list the moment the constant was added -- a row would have moved from
    "go and check this" to invisible by being investigated MORE. That is the shape of
    defect this whole module exists to make impossible, so the test is membership of a
    named tuple and tests assert the tuple covers every kind but MEASURED.
    """
    return tuple(c for c in CAPABILITIES if c.evidence in NOT_THIS_DEPLOYMENT)


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


#: WHICH CAPABILITY ROW EXPLAINS WHICH FAILURE SHAPE, per chain.
#:
#: The REMEDY TEXT IS NOT HERE. This maps a failure to the NAME of a row above, and
#: the sentence comes from that row's `instead` field (rule 8: the remedy had better
#: have one home, and it already has one). A second copy of "write rpcallowip=172.18.*"
#: is the shape that cost this repository three separate weather-city fixes.
#:
#: TWO SHAPES, AND THEY ARE THE TWO THE OPERATOR ACTUALLY HIT ON 2026-10-09:
#:
#:   403         the daemon ACCEPTED the connection and declined the caller by IP.
#:               On GRC this is src/rpc/server.cpp:593, the only HTTP_FORBIDDEN
#:               emission site in that entire tree, so it is uniquely an rpcallowip
#:               rejection -- not a credential failure, which is 401 with a
#:               WWW-Authenticate header and an HTML body.
#:   refused     nothing was accepting at all. On BTC/LTC that is modern Core
#:               defaulting to a loopback-only bind until `rpcbind` says otherwise.
#:
#: Keyed by chain because the SAME shape means different things: a 403 from a modern
#: Core is an rpcallowip rejection too, but the remedy there is CIDR and on GRC it is
#: a wildcard -- which is the entire finding this table exists to deliver.
#: SEVERAL ROWS MAY ANSWER ONE FAILURE AND ALL OF THEM PRINT, in this order. The GRC
#: 403 is the reason: it has TWO causes that produce an identical symptom, and the
#: first version of this table listed only the second one -- which would have handed
#: the operator the syntax fix again, the fix they had already applied correctly four
#: times to a file the daemon does not read. The split-conf row is FIRST because it is
#: the one that was actually blocking on 2026-10-09 and the one no amount of staring
#: at the syntax reveals.
_REFUSAL_REMEDIES: dict[str, tuple[tuple[str, str], ...]] = {
    "GRC": (
        ("403", "one config file with [network] sections"),
        ("403", "rpcallowip in CIDR form (172.18.0.0/16)"),
    ),
    "BTC": (("refused", "rpcbind"),),
    "LTC": (("refused", "rpcbind"),),
}


def _remedy_for(asset: str, name: str) -> str:
    """Which of a row's two remedy fields answers THIS asset's failure.

    A chain listed in `present_on` HAS the capability, so a failure means it is not
    being used and `when_unused` is the sentence. A chain that lacks it needs `instead`.

    THIS DISTINCTION WAS A WRONG REMEDY ON SCREEN. refusal_remedy() printed `instead`
    for every match, so an LTC `Connection refused` rendered the rpcbind row's
    GRC-facing sentence -- which opens "nothing is needed" -- to an operator whose
    litecoind had just failed to come up. The row carried the right knowledge and the
    lookup asked it the wrong question.

    Falls back to `instead` when `when_unused` is empty, because most rows have only
    one remedy and for those the question does not arise: GRC's two 403 rows are both
    about capabilities GRC genuinely lacks.
    """
    capability = _capability(name)
    if asset in capability.present_on and capability.when_unused:
        return capability.when_unused
    return capability.instead


def refusal_shape(detail: str) -> str | None:
    """Classify a probe failure string. None when it is neither shape we know.

    A HEURISTIC OVER AN ERROR MESSAGE, AND IT SAYS SO. The input is built by
    services/admin_view.probe_chain() from whatever exception the RPC call raised,
    so it is our own prose wrapped around a transport library's -- which means the
    tokens matched here can change when `requests` changes its wording.

    That is survivable ONLY because the failure mode is silence: an unrecognized
    string returns None and the report says nothing extra. It never guesses a
    remedy, and it never suppresses the raw detail, which is printed either way.
    This is the lesson from the XRP line shipped and fixed earlier the same day --
    a renderer that cannot tell "no answer" from "nothing to say" invents one.
    """
    lowered = detail.lower()
    if "403" in lowered or "forbidden" in lowered:
        return "403"
    if "connection refused" in lowered or "errno 111" in lowered:
        return "refused"
    return None


def refusal_remedy(asset: str, detail: str) -> str:
    """What to DO about this chain answering this way. "" when nothing is recorded.

    Returns the matching capability row's `instead` sentence, so the advice on an
    operator's screen is the same sentence a reader finds in CAPABILITIES -- never a
    paraphrase that can drift from it.

    WHY THIS IS WORTH A FUNCTION. The GRC 403 went five rounds on 2026-10-09: the
    operator added `rpcallowip=172.18.0.0/16`, restarted, got the same 403, and
    nothing anywhere said that Gridcoin's release line cannot read CIDR -- not the
    daemon, which logs nothing about rpcallowip at all, and not `swap_stack.py
    chains`, which printed the 403 verbatim and stopped. The knowledge was already
    in this file by then, in a row nothing consulted. A capability map that no
    report reads is documentation, and rule 5's test applies to it: if a reader has
    to open a file to learn something, it is in the wrong place.
    """
    shape = refusal_shape(detail)
    if shape is None:
        return ""
    matched = [note for note in (
        _remedy_for(asset, name)
        for recorded_shape, name in _REFUSAL_REMEDIES.get(asset, ())
        if recorded_shape == shape
    ) if note]
    # NUMBERED ONLY WHEN THERE IS MORE THAN ONE, because "1." in front of a lone
    # paragraph implies a second step the reader goes looking for (rule 14: state what
    # the thing means, and do not imply what it does not).
    if len(matched) == 1:
        return matched[0]
    return " ".join(f"({index}) {note}" for index, note in enumerate(matched, start=1))
