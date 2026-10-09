"""Find, start, interrogate and stop the two regtest daemons.

Role: submodule (process and connection lifecycle for the harness)
Reads: the environment variables listed in CHAIN_DEFAULTS below; the daemon
       binaries on PATH; each daemon's pid file under its datadir
Writes: starts and stops processes; with --wipe, deletes the `regtest`
       subdirectory of a datadir
Can move funds: no directly. It opens the connections the rest of the harness
       moves regtest coins over, and it is where the refusal to touch any
       chain but regtest lives.
Mainnet-safe: NO. It starts daemons and it is the file that must refuse to
       proceed against anything but regtest. assert_regtest() is called
       immediately after every connection is established, unconditionally, and
       there is no flag anywhere in this harness that turns it off.

EVERY SPAWN HERE HAS A NAMED REAPER (CLAUDE.md rule 13).

`start_daemon()` spawns `<daemon> -datadir=... -regtest -daemon`, and the
reaper is `stop_daemon()` in this same file, called from the `finally` block in
regtest_htlc_verify.py's main(). A spawn and its reap are one change, and they
are in one file so that neither can be edited without the other in view.

The stop PROVES it worked rather than trusting an exit code. `bitcoin-cli stop`
returns as soon as the request is accepted, not when the process has gone, and
a harness that reports "stopped" while a daemon still holds the datadir lock
is exactly the orphan rule 13 is about: the next run fails to start with
"Cannot obtain a lock on data directory", which reads like a permissions
problem. So stop_daemon() reads the pid from the daemon's own pid file BEFORE
asking it to stop, and then polls `os.kill(pid, 0)` until it raises
ProcessLookupError. The absence is the assertion.

A DAEMON THIS HARNESS DID NOT START IS NEVER STOPPED BY IT. If RPC already
answers on the configured port when the harness begins, it adopts that daemon,
says so on screen, and leaves it running at teardown. Killing a process you did
not spawn is how a harness takes down something an operator was using.

WHAT IS DETECTED AT RUNTIME RATHER THAN ASSUMED FROM A VERSION STRING.

Bitcoin Core 28.1 and Litecoin Core 0.21.4 are four years and several wallet
redesigns apart, and the harness must not branch on a version number somebody
guessed. `probe_capabilities()` asks the daemon itself:

  chain                from getblockchaininfo -- the refusal above depends on
                       this and nothing else.
  build string         from getnetworkinfo, printed so a pasted run says which
                       builds produced it -- and WHICH FIELD it came from, because
                       Core pushes `subversion` and Gridcoin pushes `version`
                       instead. A daemon with neither says so in a sentence; see
                       _build_string(), which exists because `subversion=None`
                       read as a broken probe for the whole of one GRC run.
  descriptor wallet    from getwalletinfo's `descriptors` field. Absent on
                       daemons that predate descriptor wallets, which is itself
                       the answer: a wallet that does not know the word is a
                       legacy wallet.
  which signing RPCs   by asking `help <method>` and reading whether the daemon
                       recognizes the name. `signrawtransaction` (the legacy
                       one the LTC client falls back to) was removed in Bitcoin
                       Core 0.18 and is still present on some Litecoin builds;
                       `signrawtransactionwithwallet` arrived in 0.17. The LTC
                       client tries the modern one and falls back on
                       "Method not found", so which of the two exists decides
                       which route its redeem takes.
  which import RPCs    `importaddress` and `importprivkey` are legacy-wallet
                       RPCs. On a descriptor wallet they exist but refuse, so
                       their presence in `help` is not the answer -- the harness
                       reports the wallet type beside them and lets the real
                       client's failure be the measurement.

None of these changes what the harness asserts. They change what it can TELL
the operator when an assertion fails, which is the difference between a round
trip and a diagnosis.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import requests
from chains.base import RPCAdapter, RPCError
from microfortnights import format_duration
from regtest.console import FAIL, OK, ConsoleLike

# Defaults match the operator's described machine exactly, and every one is
# overridable by environment variable. The variable NAMES are ASCII and say
# _SECONDS where they are seconds (rule 6): a µ in something a shell has to
# export buys nothing and costs a support call.
CHAIN_DEFAULTS = {
    "BTC": {
        "daemon": "bitcoind",
        "cli": "bitcoin-cli",
        "datadir": "~/regtest/btc",
        "port": 18443,
        "conf_name": "bitcoin.conf",
        "pid_name": "bitcoind.pid",
        "env_prefix": "ST_REGTEST_BTC",
    },
    "LTC": {
        "daemon": "litecoind",
        "cli": "litecoin-cli",
        "datadir": "~/regtest/ltc",
        "port": 19443,
        "conf_name": "litecoin.conf",
        "pid_name": "litecoind.pid",
        "env_prefix": "ST_REGTEST_LTC",
    },
}

# How long to wait for a daemon to answer RPC after being asked to start, and
# how long to wait for it to disappear after being asked to stop. SECONDS,
# because both are compared against time.monotonic() and passed to sleep --
# an interface, not a report (rule 6). They are rendered in microfortnights
# wherever they are printed.
RPC_READY_TIMEOUT_SECONDS = float(os.environ.get("ST_REGTEST_RPC_READY_TIMEOUT_SECONDS", "60"))
STOP_TIMEOUT_SECONDS = float(os.environ.get("ST_REGTEST_STOP_TIMEOUT_SECONDS", "60"))
POLL_INTERVAL_SECONDS = 0.5

# Bitcoin's coinbase maturity. A coinbase output is spendable once it has 100
# confirmations, so a chain must be 101 blocks tall before the first one can
# be spent. This is a COUNT OF BLOCKS and is never rendered in microfortnights
# (rule 6): a regtest block takes however long generatetoaddress took.
COINBASE_MATURITY_HEIGHT = 101

# Below this, an HTTP status is a success. Named because RegtestRPC checks it
# AFTER parsing the body rather than before -- see that class for why.
HTTP_ERROR_STATUS = 400


class RegtestSetupError(RuntimeError):
    """A named precondition failed, with the fix in the message.

    Every raise of this carries what was checked, what was found, and the
    command or setting that would put it right. A harness that will first run
    on somebody else's machine has no second chance to ask.
    """


@dataclass
class ChainConfig:
    """Everything needed to reach one chain, resolved from the environment."""

    asset: str
    daemon_path: str
    cli_path: str
    datadir: Path
    host: str
    port: int
    rpc_user: str
    rpc_password: str
    conf_name: str
    pid_name: str
    extra_args: list[str] = field(default_factory=list)

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    @property
    def conf_path(self) -> Path:
        return self.datadir / self.conf_name

    @property
    def regtest_dir(self) -> Path:
        return self.datadir / "regtest"

    @property
    def pid_path(self) -> Path:
        return self.regtest_dir / self.pid_name


def _env(prefix: str, name: str, fallback: str) -> str:
    return os.environ.get(f"{prefix}_{name}", fallback)


def resolve_chain_config(asset: str, extra_args: list[str] | None = None) -> ChainConfig:
    """Build a ChainConfig from the defaults and the environment.

    The binary is NOT resolved here -- see check_binaries(), which is step 1 and
    is where a missing daemon gets its own named failure with the fix in the
    message. Resolving it here would turn a missing binary into a FileNotFound
    raised from somewhere in the middle of step 2.
    """
    spec = CHAIN_DEFAULTS[asset]
    prefix = spec["env_prefix"]
    return ChainConfig(
        asset=asset,
        daemon_path=_env(prefix, "DAEMON", spec["daemon"]),
        cli_path=_env(prefix, "CLI", spec["cli"]),
        datadir=Path(_env(prefix, "DATADIR", spec["datadir"])).expanduser(),
        host=_env(prefix, "RPC_HOST", os.environ.get("ST_REGTEST_RPC_HOST", "127.0.0.1")),
        port=int(_env(prefix, "RPC_PORT", str(spec["port"]))),
        rpc_user=_env(prefix, "RPC_USER", "rt"),
        rpc_password=_env(prefix, "RPC_PASSWORD", "rt"),
        conf_name=spec["conf_name"],
        pid_name=spec["pid_name"],
        extra_args=list(extra_args or []),
    )


def binary_version(path: str) -> str:
    """The daemon's own `-version` first line, or a named failure.

    Uses the resolved absolute path and a fixed argument list -- no shell, no
    interpolation of anything the operator typed into a command string.
    """
    resolved = shutil.which(path)
    if resolved is None:
        raise RegtestSetupError(
            f"binary {path!r} is not on PATH. "
            f"Install it, or point the harness at it: ST_REGTEST_<BTC|LTC>_DAEMON=/full/path/to/{path}"
        )
    completed = subprocess.run(  # noqa: S603 -- checked: `resolved` came from shutil.which and the argument list is a literal. No shell, no operator-supplied string in the argv.
        [resolved, "-version"],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    first_line = (completed.stdout or completed.stderr).strip().splitlines()
    return first_line[0] if first_line else f"(none: {path} -version printed nothing)"


def daemon_help_text(path: str) -> str:
    """The daemon's own `-help -help-debug` output, or an empty string.

    Asking the binary what options it has is the only honest way to settle a
    question like "does this build accept a deployment override". A flag
    remembered from another codebase, passed to a daemon that does not know
    it, makes the daemon refuse to START -- turning a mining failure several
    hundred blocks in into a failure at step 2, which is strictly worse.
    """
    resolved = shutil.which(path)
    if resolved is None:
        return ""
    completed = subprocess.run(  # noqa: S603 -- checked: `resolved` came from shutil.which and the argument list is two literals. No shell, nothing operator-supplied in the argv.
        [resolved, "-help", "-help-debug"],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    return (completed.stdout or "") + (completed.stderr or "")


def mweb_override_args(help_text: str) -> tuple[list[str], str]:
    """Arguments that keep Litecoin's MWEB deployment from activating, if this build has any.

    MEASURED ON THE OPERATOR'S MACHINE 2026-09-25. Mining toward the LTC
    locktime died several hundred blocks in:

        generatetoaddress: code=-1 message=CreateNewBlock: TestBlockValidity
        failed: bad-txns-vin-empty, Transaction check failed (tx hash 58338ec7...)

    A transaction with NO INPUTS, in a block the daemon was assembling for
    itself, is not something an HTLC test can produce. Litecoin 0.21.x carries
    Mimblewimble Extension Blocks, whose integrating "HogEx" transaction is
    exactly a transaction whose input structure a generic `CheckTransaction`
    can read as vin-empty, and MWEB activates by height -- which is why the
    first few hundred blocks mined fine and then one did not.

    THAT IS A HYPOTHESIS AND THE HARNESS TREATS IT AS ONE (rule 17). It cannot
    be tested from the machine this was written on: there is no litecoind here
    and none can be installed. So the harness does three things instead of
    assuming: it prints `getblockchaininfo.softforks` for both chains so the
    daemon states MWEB's status and activation height itself; it asks THIS
    binary which options it accepts rather than passing a remembered flag; and
    if the daemon refuses to start with what it picked, it says so and starts
    again without it.

    `-vbparams=<deployment>:<start>:<timeout>` is the regtest-only versionbits
    override inherited from Bitcoin Core, and `mweb` is the deployment name
    Litecoin gives MWEB. The VALUE matters and the first version of this
    function had it wrong.

    CORRECTED 2026-09-26 by reading Litecoin Core v0.21.4's own source, which
    is the measurement the paragraph above could not take. This used to pass
    `mweb:0:0` on the reasoning that a deployment started at zero and timed out
    at zero reaches FAILED. That reasoning is refuted by
    src/versionbits.cpp::AbstractThresholdConditionChecker::GetStateFor, whose
    FIRST line is

        bool fHeightBased = (nTimeStart == 0 && nTimeTimeout == 0) ? true : false;

    so `0:0` does not mean "time window of zero width", it switches the
    deployment to HEIGHT-based signaling -- and with no 4th and 5th fields
    given, nStartHeight defaults to 0, so DEFINED -> STARTED fires on the very
    first block and MWEB activates on schedule. `mweb:0:0` was a no-op that
    printed as an override, which is rule 14's "did nothing must not look like
    did work" wearing a versionbits flag.

    The value that works is `-2`, because the same function returns early:

        if (nTimeStart == Consensus::BIP9Deployment::NEVER_ACTIVE) {
            return ThresholdState::FAILED;
        }

    and src/consensus/params.h defines `NEVER_ACTIVE = -2`. It is unconditional
    -- no period, no threshold, no signaling -- so MWEB can never activate at
    any height.

    That is also what Litecoin's own functional tests pass, in 20+ files
    including test/functional/feature_cltv.py, which is this harness's exact
    situation: a test that has to mine past BIP65's regtest activation height
    without MWEB turning on partway. They all write `-vbparams=mweb:-2:0`.

    AND THE CAUSE IS NOW CONFIRMED, not merely the leading hypothesis. Read
    src/mweb/mweb_miner.cpp::AddHogExTransaction: it appends the previous
    block's HogAddr as the new HogEx's only input *if* the previous block
    carried a HogEx, and appends peg-in inputs if any exist. On the first block
    after activation on an idle chain NEITHER holds, so the HogEx it builds has
    an empty vin and one vout. src/consensus/tx_check.cpp rejects exactly that:

        if (!tx.IsMWEBOnly()) {
            if (tx.vin.empty())
                return state.Invalid(..., "bad-txns-vin-empty");

    and `IsMWEBOnly()` is `HasMWEBTx() && vin.empty() && vout.empty()`, which
    the HogEx fails on its vout. src/miner.cpp calls AddHogExTransaction
    unconditionally once `IsMWEBEnabled(pindexPrev, ...)`, so this is
    deterministic rather than intermittent: the first block mined after
    activation cannot be built.

    MEASURED, NOT DERIVED, 2026-09-26: this harness was run against Litecoin
    Core v0.21.4 with --ltc-mweb, so MWEB was left to activate, and it died
    `after 288 of 1351 blocks` with `bad-txns-vin-empty, Transaction check
    failed (tx hash 58338ec7c9c4e608...)` -- the SAME transaction hash the
    operator's 2026-09-25 run reported, which is a bit-for-bit reproduction
    rather than a similar-looking failure. Activation is at height 288, not the
    432 a first reading of the window arithmetic suggests: on a 144-block
    window MWEB is STARTED from genesis (its nStartTime is in the past), so
    LOCKED_IN lands at 144 and ACTIVE at 288, and 288 is also where the
    deployment table stops listing it as pending. The run with the override
    mined straight past 288 to 2504 and finished OK=37 FAIL=0.

    Returns (args, explanation). An empty arg list with an explanation is a
    perfectly good answer and says so on screen.
    """
    if not help_text:
        return [], "could not read the daemon's -help output, so no deployment override was attempted"
    if "-vbparams" not in help_text:
        return [], (
            "this build does not advertise -vbparams, so there is no deployment override to apply. "
            "If mining fails with bad-txns-vin-empty, MWEB cannot be switched off from the command line here"
        )
    return (
        ["-vbparams=mweb:-2:0"],
        "this build advertises -vbparams, so MWEB is held at NEVER_ACTIVE (start=-2), which versionbits returns "
        "FAILED for unconditionally -- the value Litecoin's own feature_cltv.py passes. If the daemon refuses to "
        "start with it, the harness retries without it and says so",
    )


def check_binaries(console: ConsoleLike, config: ChainConfig) -> str:
    """Step 1 for one chain: the daemon and the cli exist, and say which build."""
    console.say(f"looking for {config.daemon_path} and {config.cli_path} on PATH")
    # A binary that prints nothing for -version is not a passing check. It is
    # either not the daemon, or not runnable, and reporting OK there would put
    # the real failure three steps later where it is harder to read.
    daemon_version = binary_version(config.daemon_path)
    console.check(f"{config.asset} daemon binary", daemon_version, "a version line",
                  FAIL if daemon_version.startswith("(none") else OK)
    cli_version = binary_version(config.cli_path)
    console.check(f"{config.asset} cli binary", cli_version, "a version line",
                  FAIL if cli_version.startswith("(none") else OK)
    return daemon_version


class RegtestRPC(RPCAdapter):
    """RPCAdapter that reads the JSON body before it reads the HTTP status.

    MEASURED ON THE OPERATOR'S MACHINE, 2026-09-25, first run of this harness.
    Litecoin died at step 3 with nothing but

        FAIL LTC run: got=HTTPError: 500 Server Error for url: http://127.0.0.1:19443/

    while BTC, one step earlier, handled the identical situation and printed
    the daemon's own words:

        BTC: loadwallet said {'code': -18, 'message': 'Wallet file verification
        failed...'}; creating 'regtest_htlc_harness'

    WHY THE SAME CODE BEHAVED TWO DIFFERENT WAYS, which is the part worth
    writing down because it will catch somebody else. chains/base.py's
    RPCAdapter sends `"jsonrpc": "2.0"` and calls `raise_for_status()` BEFORE
    it parses the body. Bitcoin Core 28.1 implements JSON-RPC 2.0 properly, and
    that spec says a well-formed request gets HTTP 200 with the error carried
    in the response object -- so on BTC the body was parsed and the -18 was
    reported. Litecoin Core 0.21.4 predates that support, ignores the
    `jsonrpc` field, and replies to an RPC error with HTTP 500 and a JSON body
    -- so `raise_for_status()` fired first and threw the diagnosis away.

    The evidence for that reading is in the operator's paste rather than in a
    version table: the SAME daemon (BTC 28.1) produced a parsed -18 through
    RPCAdapter, which sends 2.0, and a bare `HTTPError: 500` through
    modules/atomic_btc_client.py, which sends `"jsonrpc": "1.0"`. Two protocol
    versions, one daemon, two behaviors.

    So: parse the body on every path, on every status, and put the daemon's
    own `code` and `message` in the exception. An HTTP status alone is not a
    diagnosis, and a harness that will only ever run on somebody else's machine
    cannot afford to discard the one sentence that says what went wrong.

    WHY THIS IS A SUBCLASS AND NOT A FIX TO chains/base.py. RPCAdapter.call()
    is what services/payout_service.py sends money through. Changing which
    exception type it raises changes how the Flask app's error handling behaves
    on the payout path, which is fund movement and the operator's call (rule
    16). The same defect is there and it is REPORTED, not patched from inside a
    harness: any caller of RPCAdapter against a pre-2.0 daemon loses the error
    code the same way this did.
    """

    def call(self, method: str, *params):
        payload = {"jsonrpc": "2.0", "id": method, "method": method, "params": list(params)}
        response = requests.post(
            self.url,
            auth=(self.user, self.password),
            headers={"Content-Type": "application/json"},
            data=json.dumps(payload),
            timeout=self.timeout,
        )
        try:
            data = response.json()
        except ValueError:
            # No JSON at all. NOW the status is the only thing there is, and it
            # is reported with whatever the daemon did send -- truncated,
            # because an HTML error page in a terminal helps nobody.
            body = (response.text or "").strip()
            raise RPCError(
                f"{method}: HTTP {response.status_code} with a non-JSON body from {self.url}: "
                f"{body[:400] if body else '(none: empty body)'}"
            ) from None
        error = data.get("error")
        if error:
            code = error.get("code") if isinstance(error, dict) else None
            message = error.get("message") if isinstance(error, dict) else error
            raise RPCError(f"{method}: code={code} message={message} (HTTP {response.status_code})")
        if response.status_code >= HTTP_ERROR_STATUS:
            # A non-2xx with a JSON body carrying no error object. Should not
            # happen; if it does, say so rather than returning a result nobody
            # can tell apart from a real one.
            raise RPCError(
                f"{method}: HTTP {response.status_code} but the body carried no error object: {data!r}"
            )
        return data.get("result")


# How far to walk an exception's __cause__ / __context__ chain looking for the
# HTTP response. Two is enough for every wrapper in this tree and stops a
# pathological chain from turning a diagnosis into a loop.
_EXCEPTION_CHAIN_DEPTH = 4
# Body text is truncated before printing: an HTML error page in a terminal
# helps nobody, and the JSON-RPC message is always at the front.
_BODY_EXCERPT = 400


def _response_of(exc: BaseException):
    """Find the requests Response on an exception, or on what it was raised from.

    BOTH HALVES ARE NEEDED, and the second is why the first live run showed
    nothing useful for LTC. `requests.HTTPError` carries `.response` directly,
    which is what modules/atomic_btc_client.py re-raises. But
    modules/atomic_ltc_client.py catches it and raises

        Exception(f"LTC RPC request failed: {e}") from e

    so the object the harness catches has no `.response` at all -- the response
    is on `__cause__`. Walking the chain is the difference between "HTTPError:
    500 Server Error" and the daemon's own code and message.
    """
    seen = exc
    for _ in range(_EXCEPTION_CHAIN_DEPTH):
        if seen is None:
            return None
        response = getattr(seen, "response", None)
        if response is not None:
            return response
        seen = seen.__cause__ or seen.__context__
    return None


def describe_rpc_exception(exc: BaseException) -> str:
    """The exception, plus the JSON-RPC error the daemon put in the body.

    WHY THIS EXISTS. The three real clients call `response.raise_for_status()`
    BEFORE parsing, so on any daemon that answers an RPC error with a non-2xx
    status -- which is every pre-JSON-RPC-2.0 daemon, and Bitcoin Core itself
    for a `"jsonrpc": "1.0"` request -- the body is discarded and the caller
    gets a bare status line. Measured on the operator's machine 2026-09-25:
    both chains reported

        XFAIL REAL redeem_contract(): got=HTTPError: 500 Server Error ...

    (that line is quoted as it was printed on the day; step 7 scores the same
    outcome FAIL since the defect it marked was fixed on 2026-09-25)

    and the harness could say nothing about which defect caused it, because
    the sentence that would have said was thrown away three frames down.

    The response object survives on the exception, so the harness reads it
    there. This changes nothing about the clients -- they are fund-path code
    (rule 16) and the defect is reported, not patched -- it only means the
    harness stops repeating a status code where a diagnosis was available.
    """
    base = f"{type(exc).__name__}: {exc}"
    response = _response_of(exc)
    if response is None:
        return base
    try:
        payload = response.json()
    except ValueError:
        body = (getattr(response, "text", "") or "").strip()
        excerpt = body[:_BODY_EXCERPT] if body else "(none: empty body)"
        return f"{base} -- HTTP {response.status_code}, non-JSON body: {excerpt}"
    error = payload.get("error") if isinstance(payload, dict) else None
    if isinstance(error, dict):
        return (
            f"{base} -- the daemon said code={error.get('code')} message={error.get('message')} "
            f"(HTTP {response.status_code})"
        )
    return f"{base} -- HTTP {response.status_code}, body carried no error object: {payload!r}"


def adapter_for(config: ChainConfig, wallet: str = "") -> RegtestRPC:
    """An RPCAdapter pointed at this chain, optionally at one wallet endpoint.

    chains/base.py's RPCAdapter is reused rather than a sixth JSON-RPC client
    being written here. CLAUDE.md rule 8 counts five Python implementations
    against a Bitcoin-style daemon in this tree already, and its own verdict is
    that this one "is the good shape": one class, a timeout, and an RPCError
    that distinguishes a daemon that answered from one that did not. The real
    atomic clients are still used for the steps that drive THEM -- this is for
    the harness's own mining, funding and broadcasting.
    """
    return RegtestRPC(
        user=config.rpc_user,
        password=config.rpc_password,
        host=config.host,
        port=config.port,
        wallet=wallet,
    )


# Liveness probes, tried in order. Each needs no wallet, so a success means "a daemon is up
# and our credentials work" rather than "a TCP port is open" -- which is the whole point of
# probing with an RPC instead of a socket connect.
#
# `uptime` FIRST AND `getblockcount` SECOND, AND THE SECOND ONE IS NOT DECORATION. This
# function's docstring used to say `uptime` "exists on both daemon families", and that was
# false the moment a third family arrived. Measured 2026-09-28 on the operator's Gridcoin
# testnet daemon: adaptor_regtest_verify.py --chain grc failed at step 1 with "nothing
# answered `uptime`" against a daemon that was demonstrably up -- three atomic swaps had
# completed through it twenty minutes earlier, and `getinfo` answered from the CLI. `uptime`
# arrived in Bitcoin Core 0.15 and Gridcoin forked long before it; it is the same shape as
# Gridcoin having no `gettxout`, which made every GRC spend print a stack trace in front of a
# success.
#
# `getblockcount` is the fallback because it is MEASURED to work on that daemon:
# atomic_swap.py read tip 3295571 from it on 2026-09-27 while funding a real GRC leg. It
# predates every fork in this tree.
LIVENESS_PROBES = ("uptime", "getblockcount")


#: WHERE GRC's TESTNET CREDENTIALS LIVE, in one sentence, in one place.
#:
#: MEASURED ON THE OPERATOR'S HOST 2026-09-29. Their GUI wallet
#: (`gridcoinresearch -testnet`, pid 8897) was serving RPC the whole time and
#: answering HTTP 401, because the credentials had been read out of the MAINNET
#: conf. Two files named gridcoinresearch.conf sit one directory apart under
#: ~/.GridcoinResearch and carry DIFFERENT credentials, and nothing about a 401
#: says which one you used.
#:
#: SHARED BECAUSE IT WAS SPELLED TWICE AND THE OPERATOR SAW THE VAGUER COPY.
#: why_nothing_answered() below carried this sentence; swap_readiness.
#: explain_grc_failure() carried its own, which said only "the conf that wallet
#: actually reads". On 2026-10-02 the operator hit the identical 401 through the
#: readiness path, read that line, and concluded they had mistyped the password --
#: a plausible reading, and not the cause this project had already measured. Rule
#: 8's drift arriving as a worse diagnosis rather than a wrong number: both copies
#: looked correct in their own file, and only one of them had the finding in it.
#:
#: A PATH AND A FILENAME, NEVER A VALUE. This names where to look; nothing in this
#: repository reads that file (see chains/daemon_conf.CONF_FALLBACK_NETWORK for why
#: GRC is deliberately excluded from conf resolution), and no credential of theirs
#: is ever read, logged or echoed.
GRC_CREDENTIALS_ARE_PER_NETWORK = (
    "GRC_RPC_USER and GRC_RPC_PASS have to come from the TESTNET gridcoinresearch.conf -- the one "
    "under the `testnet` subdirectory of ~/.GridcoinResearch -- which carries DIFFERENT credentials "
    "from the mainnet file beside it. Reading them out of the mainnet conf is the cause this project "
    "has actually measured for this 401 (2026-09-29), so check WHICH FILE before retyping anything."
)


def why_nothing_answered(reasons: list[str]) -> str:
    """Which of the two utterly different causes a total liveness miss is. NEVER guessed.

    THE DEFECT THIS EXISTS FOR, on the operator's host 2026-09-29, and its consequence was
    worse than the fault. Every probe missed, and the GRC refusal told them:

        Start your TESTNET daemon yourself -- `gridcoinresearchd -testnet -daemon`

    Their daemon WAS running -- a GUI wallet, `gridcoinresearch -testnet`, pid 8897, serving
    RPC the whole time. The real cause was HTTP 401: the credentials had been read out of the
    MAINNET conf. So the harness proposed starting a SECOND process against a staking wallet's
    datadir, which is a worse action than the problem it was diagnosing, to fix something that
    was not broken.

    THE TWO CAUSES NEED OPPOSITE ACTIONS and share one symptom:

      401 / 403       the daemon is UP and rejecting the credentials. Starting another one
                      changes nothing and risks the datadir lock.
      refused / reset  nothing is listening. Credentials are irrelevant until something is.

    Rule 17, in an error message: the probe knows which of these it got, and printing a guess
    in the voice of a diagnosis is what sent the operator at their own wallet.
    """
    blob = " ".join(reasons).lower()
    if "401" in blob or "unauthorized" in blob or "403" in blob:
        return (
            "the daemon IS answering and REFUSED THE CREDENTIALS (HTTP 401). Do NOT start "
            "another one -- it is already up, and a second process on a staking wallet's "
            f"datadir is a worse problem than this. {GRC_CREDENTIALS_ARE_PER_NETWORK}"
        )
    if "refused" in blob or "reset" in blob or "timed out" in blob or "connection" in blob:
        return (
            "NOTHING IS LISTENING on that port -- this is not a credentials problem, and "
            "checking them will waste the next ten minutes. A GUI wallet serves RPC just as a "
            "daemon does, so if yours is open, check the PORT: 25715 is testnet and 15715 is "
            "MAINNET. If it is closed, start it yourself; this harness will not, because a "
            "Gridcoin daemon is a staking wallet and starting one is a live action"
        )
    return (
        "the reason is not one this harness recognizes, so it is printed verbatim above rather "
        "than diagnosed. It is NOT known to be credentials and NOT known to be a dead port"
    )


def liveness_probe_results(config: ChainConfig) -> tuple[str | None, list[str]]:
    """(the first probe that answered, what each earlier one said). ONE OWNER OF "IS IT UP".

    THIS IS THE FUNCTION EVERY READINESS QUESTION IN THIS FILE GOES THROUGH, and it is
    shaped to return both halves because the two callers need different ones and a caller
    that computed either locally would be the second answer to one question -- which is the
    duplication this repository keeps paying for (CLAUDE.md rule 8).

      the NAME    so a caller can print WHICH probe worked. "uptime missed but getblockcount
                  answered" is a fact about the daemon FAMILY, and a bare True hides it.
      the REASONS verbatim, per probe, so a total miss can be reported with what each one
                  actually said instead of a guess at why.

    WHY THE REASONS ARE PART OF THE RETURN AND NOT LEFT TO THE CALLER. wait_for_rpc() used
    to poll `uptime` ALONE and keep its own `last_error`, three functions below the table
    that already knew `uptime` is absent on Gridcoin -- so this one file held two different
    answers to "is this daemon answering", and only one of them knew about the gap that cost
    the 2026-09-28 GRC run its step 1. Folding the reason in is what let that second
    implementation be deleted rather than merely corrected.
    """
    failures: list[str] = []
    for method in LIVENESS_PROBES:
        reason = _probe_failure(config, method)
        if reason is None:
            return method, failures
        failures.append(f"{method}: {reason}")
    return None, failures


def liveness_probe_that_answers(config: ChainConfig) -> str | None:
    """The name of the first probe this daemon answers, or None if none of them do.

    Returns the NAME rather than a bool so a caller can print which one answered. That
    matters because "uptime failed but getblockcount worked" is a fact about the daemon
    FAMILY, and a reader who sees only True learns nothing about why the first one missed.
    """
    return liveness_probe_results(config)[0]


def _probe_failure(config: ChainConfig, method: str) -> str | None:
    """Why this one method did not answer, or None when it DID. Note which way round.

    None means success, which is the opposite of the usual convention and is deliberate:
    the alternative is returning a bool AND the text, and then every caller has to keep the
    two in step. "There is no failure to report" is exactly what None says.

    The text is the exception's type AND its message, never the type alone. A bare
    `RPCError` cannot be told apart from a wrong rpcpassword, and "nothing answered" sent
    the operator to check credentials that were fine on 2026-09-28 -- the daemon simply had
    no `uptime`. The message carries the daemon's own `code=-32601`, which is the one thing
    that distinguishes the two.
    """
    try:
        adapter_for(config).call(method)
    except Exception as exc:  # noqa: BLE001 -- checked: this function's ONLY question is "did this method answer", and it returns the reason it did not rather than swallowing it. A method absent from an older daemon family, a wallet that is not loaded and a closed port are three different sentences here, all reported verbatim to whoever asked; nothing can return a value a caller would mistake for a connected daemon.
        return f"{type(exc).__name__}: {exc}"
    return None


def rpc_answers(config: ChainConfig) -> bool:
    """True if something on the configured port answers ANY of the liveness probes.

    Kept as a bool for the readiness polling in start_daemon() and stop_daemon(), which only
    ever branch on yes/no. A caller that wants to report WHICH method answered calls
    liveness_probe_that_answers(), and one that wants the reasons for a miss calls
    liveness_probe_results().
    """
    return liveness_probe_that_answers(config) is not None


#: WHY A DAEMON THIS HARNESS STARTS IS ASKED TO LOG WHY IT REFUSES THINGS.
#:
#: Measured 2026-09-29, on the operator's host, on a passing LTC run. Step 8c asks
#: `generateblock` to MINE an early refund and the daemon refused it --
#:
#:     generateblock: code=-25 message=TestBlockValidity failed: block-validation-failed
#:
#: -- which is the chain's own validity check and is the ONLY thing establishing consensus
#: enforcement on Litecoin, because 8b's mempool graded the same refusal
#: `non-mandatory-script-verify-flag` (relay policy) where Bitcoin Core graded it `mandatory`.
#: That message is GENERIC: it says the block was invalid and does not name the locktime.
#:
#: SO THE LOG WAS GREPPED, AND IT HELD NOTHING. `grep -iE "locktime|block-validation|
#: TestBlockValidity" regtest/debug.log` returned four `CTransaction(hash=...)` dumps, every one
#: of them a FUNDING transaction carrying the wallet's own anti-fee-sniping nLockTime, and not a
#: word about the refusal. Bitcoin-derived daemons log script-verification failures under the
#: `validation` and `mempoolrej` categories only, and neither is on by default.
#:
#: NAMED CATEGORIES, NEVER `-debug=all`. `all` on a run that mines 2500 blocks writes a log
#: nobody reads, and rule 14's complaint about silence is not an argument for volume -- the two
#: categories here are the ones that carry the sentence 8c cannot currently produce.
#:
#: ONLY ON A DAEMON THIS HARNESS STARTS. It is in `base_argv`, which is the spawn path; an
#: ADOPTED daemon is untouched, because a startup flag cannot be applied to a running process
#: (the same fact the MWEB override's own report had to stop getting wrong -- see
#: mweb_state_line).
REFUSAL_LOGGING = ("-debug=validation", "-debug=mempoolrej")


def apply_mweb_override(console: ConsoleLike, config: ChainConfig) -> None:
    """Ask THIS litecoind whether it can hold MWEB inactive, and set the flag on `config` if so.

    MOVED HERE FROM regtest/steps.py ON 2026-09-28, BECAUSE A SECOND STARTER APPEARED AND DID
    NOT KNOW ABOUT IT. The operator pressed the panel's new Start daemon button on the LTC tab
    and got

        LTC: starting /usr/local/bin/litecoind -datadir=... -regtest -daemon

    with no -vbparams at all, because the override lived in a step of the nine-step harness and
    the panel does not run steps. `ltc_htlc_verify` then ADOPTED that daemon -- correctly, it
    was answering -- and died at height 288 with bad-txns-vin-empty, which is the failure this
    flag exists to prevent. Rule 8, in its most literal form: one rule about how a daemon must
    be started, and a second way to start one that had never heard of it.

    IT MUST RUN BEFORE THE DAEMON STARTS AND AFTER THE BINARY IS FOUND, because it works by
    reading that binary's own `-help` output. Calling it on a chain that is not LTC, or on a
    build with no -vbparams, is a no-op that says so rather than a silent one.

    See mweb_override_args() for the reproduction and for why the flag is discovered from the
    binary rather than remembered.
    """
    args, explanation = mweb_override_args(daemon_help_text(config.daemon_path))
    console.say(f"{config.asset}: MWEB deployment override: {explanation}")
    if args:
        config.extra_args.extend(args)
        console.say(f"{config.asset}: MWEB deployment override: passing {' '.join(args)} to the daemon")
    else:
        console.say(
            f"{config.asset}: MWEB deployment override: none applied. If mining toward the locktime fails with "
            "bad-txns-vin-empty, that is the failure this would have prevented, and the softfork listing after "
            "the daemon answers says whether MWEB is why."
        )


def start_daemon(console: ConsoleLike, config: ChainConfig) -> bool:
    """Start the daemon if nothing is answering yet. Returns True if we spawned it.

    THE REAPER IS stop_daemon() IN THIS FILE, called from the finally block in
    regtest_htlc_verify.main(). Returning whether we spawned it is what lets
    that reaper leave an adopted daemon alone.
    """
    if rpc_answers(config):
        console.say(
            f"{config.asset}: a daemon is ALREADY answering on {config.base_url} -- adopting it. "
            "This harness will NOT stop a daemon it did not start."
        )
        return False

    if not config.datadir.exists():
        raise RegtestSetupError(
            f"{config.asset} datadir {config.datadir} does not exist. "
            f"Create it and put {config.conf_name} in it, or set "
            f"ST_REGTEST_{config.asset}_DATADIR to where yours lives."
        )
    if not config.conf_path.exists():
        raise RegtestSetupError(
            f"{config.asset} config {config.conf_path} does not exist. "
            f"The harness needs regtest=1, server=1, fallbackfee=0.0002 and a [regtest] section with "
            f"rpcuser/rpcpassword/rpcport={config.port}."
        )

    resolved = shutil.which(config.daemon_path)
    if resolved is None:
        raise RegtestSetupError(f"{config.daemon_path} vanished between step 1 and step 2; not on PATH")

    base_argv = [resolved, f"-datadir={config.datadir}", "-regtest", "-daemon", *REFUSAL_LOGGING]
    completed = _spawn(console, config, base_argv + config.extra_args)
    if completed.returncode != 0 and config.extra_args:
        # A daemon that will not start because of an option the harness ADDED
        # must not become a step-2 failure for the operator. Say exactly what
        # it refused, then start it the plain way -- the run continues, and
        # whatever the option was for is reported as not applied rather than
        # silently assumed.
        console.say(
            f"{config.asset}: refused to start with {' '.join(config.extra_args)} -- "
            f"{completed.stderr.strip() or completed.stdout.strip() or '(none: it said nothing)'}"
        )
        console.say(f"{config.asset}: retrying WITHOUT those options; whatever they were for is NOT in effect")
        config.extra_args.clear()
        completed = _spawn(console, config, base_argv)
    if completed.returncode != 0:
        raise RegtestSetupError(
            f"{config.asset} daemon refused to start (exit {completed.returncode}). "
            f"stdout={completed.stdout.strip() or '(none)'} stderr={completed.stderr.strip() or '(none)'}. "
            "A lock error here usually means a daemon from a previous run is still holding the datadir: "
            "stop it, or rerun with --wipe."
        )
    return True


def _spawn(console: ConsoleLike, config: ChainConfig, argv: list[str]):
    console.say(f"{config.asset}: starting {' '.join(argv)}")
    return subprocess.run(  # noqa: S603 -- checked: argv[0] is from shutil.which; the datadir and the options come from this harness's own defaults and its own -help probe, never from a network source. No shell.
        argv,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def wait_for_rpc(console: ConsoleLike, config: ChainConfig) -> None:
    """Poll until the daemon answers ANY liveness probe, printing progress. Never a fixed sleep.

    A fixed sleep is wrong in both directions: too short and the harness
    reports a connection failure for a daemon that was still reindexing, too
    long and every run pays for the worst case. Polling also lets the wait
    SAY something, which a sleep cannot (rule 14).

    IT POLLED `uptime` ALONE UNTIL 2026-09-28, AND THAT IS THE SAME DEFECT `c2c2041` FIXED
    ONE FUNCTION AWAY IN THIS FILE. `uptime` arrived in Bitcoin Core 0.15 and Gridcoin
    forked long before it -- measured on the operator's testnet daemon, `help uptime`
    answers "unknown command: uptime" while `getblockcount` on the same port with the same
    credentials returns a height. Pointed at that daemon, this loop would have spent the
    whole of RPC_READY_TIMEOUT_SECONDS printing `code=-32601 Method not found` and then
    raised a refusal telling the operator to put a `[regtest]` section and an rpcuser in a
    config file that is already correct, for a daemon that has no regtest mode at all. A
    method this family never had, reported as a credential problem: the exact shape the
    LIVENESS_PROBES table above exists to refuse.

    NOT REACHED ON TODAY'S GRC PATH, and that is not a reason to leave it. funding_steps.
    step_1_reachable() returns before this line for GRC, and fund_testnets.py's GRC branch
    never enters fund_regtest_chain() -- so this was a live break waiting for the first
    caller who pointed a readiness wait at Gridcoin, while `rpc_answers()` forty lines up
    already knew better. Two answers to one question in one file is rule 8 with a delay on
    it; the second one is deleted here rather than corrected, so there is nothing left to
    drift.

    The reasons come back from liveness_probe_results() verbatim -- every probe, not just
    the last one tried -- because "uptime missed AND getblockcount missed" and "uptime
    missed" are different diagnoses and only the first one means the daemon is not up.
    """
    started = time.monotonic()
    deadline = started + RPC_READY_TIMEOUT_SECONDS
    attempts = 0
    last_errors = ["(none: never got far enough to fail)"]
    console.say(
        f"{config.asset}: waiting for RPC on {config.base_url}, trying {list(LIVENESS_PROBES)} each "
        f"attempt; up to {format_duration(RPC_READY_TIMEOUT_SECONDS)}"
    )
    # AT LEAST ONE ATTEMPT, ALWAYS, which is why this is a do-while and not a while.
    # `while time.monotonic() < deadline` alone can make ZERO attempts -- a zero or already
    # elapsed timeout raises the refusal below having asked the daemon nothing, and then the
    # message lists placeholder reasons as though they were the daemon's. A test seeded with
    # RPC_READY_TIMEOUT_SECONDS=0 found exactly that. Rule 14: a check that did not run must
    # not report like one that did, and here it would have reported worse than that -- it
    # would have named the wrong cause.
    while True:
        attempts += 1
        answered, failures = liveness_probe_results(config)
        if answered is not None:
            console.say(
                f"{config.asset}: RPC answered by `{answered}` after "
                f"{format_duration(time.monotonic() - started)} ({attempts} attempt(s))"
                + (f"; earlier probes missed: {'; '.join(failures)} -- which is expected on a daemon "
                   f"family that does not have them" if failures else "")
            )
            return
        last_errors = failures
        console.say(
            f"{config.asset}: waiting for RPC on {config.base_url}, attempt {attempts}, "
            f"{format_duration(time.monotonic() - started)} of {format_duration(RPC_READY_TIMEOUT_SECONDS)} "
            f"-- {'; '.join(failures)}"
        )
        if time.monotonic() >= deadline:
            break
        time.sleep(POLL_INTERVAL_SECONDS)
    raise RegtestSetupError(
        f"{config.asset} daemon did not answer any of {list(LIVENESS_PROBES)} at {config.base_url} within "
        f"{format_duration(RPC_READY_TIMEOUT_SECONDS)} ({attempts} attempts). Last errors, one per probe: "
        f"{'; '.join(last_errors)}. A `code=-32601 Method not found` on ONE probe is that daemon family not "
        f"having it and is not the problem; every probe missing is. "
        f"Check that {config.conf_path} has server=1 and a [regtest] section with rpcuser={config.rpc_user}, "
        f"rpcpassword=<yours> and rpcport={config.port}, and that "
        f"ST_REGTEST_{config.asset}_RPC_USER / _RPC_PASSWORD match it."
    )


def assert_regtest(console: ConsoleLike, config: ChainConfig, *, we_started_it: bool) -> dict:
    """THE UNCONDITIONAL REFUSAL. Nothing in this harness runs before it passes.

    This is the first call made after every connection, on every chain, on
    every run. There is no flag that disables it and no branch that skips it.
    The harness starts daemons, mines blocks and broadcasts transactions; a
    version of it pointed at mainnet would broadcast a real transaction paying
    a key it generated thirty lines earlier.

    `getblockchaininfo.chain` is the one field checked, because it is the
    daemon's own statement about which network it is on. A port number, a
    datadir name or a config file are all things that can be right while the
    daemon is on a different chain.
    """
    info = adapter_for(config).call("getblockchaininfo")
    chain = info.get("chain")
    if chain != "regtest":
        console.check(f"{config.asset} network", chain, "regtest", FAIL)
        raise RegtestSetupError(
            f"REFUSING TO RUN: the {config.asset} daemon at {config.base_url} reports chain={chain!r}, "
            "not 'regtest'. This harness starts daemons, mines and BROADCASTS. It will not touch any other "
            "network, and there is no flag to override this."
        )
    console.check(f"{config.asset} network", chain, "regtest", OK)
    report_softforks(console, config, info, we_started_it=we_started_it)
    return info


def report_softforks(console: ConsoleLike, config: ChainConfig, info: dict, *, we_started_it: bool) -> None:
    """Print each deployment and its status, from the daemon's own mouth.

    This exists for one measured reason. Mining toward the LTC locktime died
    with `bad-txns-vin-empty` 288 blocks in, and Mimblewimble Extension Blocks
    were the leading hypothesis when this was written. They are now the
    confirmed cause -- see mweb_override_args for the reproduction -- and this
    block is still printed before anything mines, because it is what says
    whether the override took effect on THIS daemon.

    MWEB'S ABSENCE FROM THE TABLE IS THE EVIDENCE, which is why it gets a line
    of its own below. A deployment held at NEVER_ACTIVE is not reported by
    getblockchaininfo at all, so the successful run's table simply has no
    `mweb` row while the failing run's reads
    `softfork mweb: type=bip9 active=False height=0`. Without a line saying so,
    those two are a present row versus a missing one, and a missing row reads
    as "this build has no MWEB" exactly as easily as "the override worked" --
    rule 14's did-nothing-must-not-look-like-did-work, at the one place in this
    harness where the difference decides whether 1351 blocks can be mined.

    Never an empty block (rule 14): a daemon with no softforks field says so.
    """
    tables = _deployment_tables(adapter_for(config), info)
    if not tables:
        console.say(
            f"{config.asset}: deployments reported by the daemon: (none: no `softforks` in getblockchaininfo and "
            "no usable `getdeploymentinfo`). The activation heights below cannot be read from this daemon."
        )
        return
    source, softforks = tables[0]
    console.say(f"{config.asset}: deployment table read from {source}")
    for name, detail in sorted(softforks.items()):
        if isinstance(detail, dict):
            kind = detail.get("type")
            height = detail.get("height", detail.get("bip9", {}).get("since"))
            active = detail.get("active")
            console.say(
                f"{config.asset}: softfork {name}: type={kind} active={active} "
                f"height={height if height is not None else '(none)'} (a height, not a duration)"
            )
        else:
            console.say(f"{config.asset}: softfork {name}: {detail}")
    console.say(f"{config.asset}: "
                f"{mweb_state_line(config.asset, softforks, config.extra_args, we_started_it=we_started_it)}")


def mweb_state_line(asset: str, softforks: dict, extra_args: list[str], *, we_started_it: bool) -> str:
    """One sentence on MWEB's state, reading the table the daemon just printed.

    Separated from the printing loop so it can be asserted on without a daemon
    (tests/test_regtest_harness_units.py). It answers the question the table
    cannot answer by itself: a `mweb` row means MWEB is live and this run will
    die at height 288, and no `mweb` row means either the override took or the
    build has none -- which is decided by whether the override was passed, not
    by the table.

    `we_started_it` IS THE WHOLE POINT OF THE 2026-09-28 REVISION, and it is
    keyword-only and required so that no caller can forget to answer it. This
    line said

        MWEB IS LISTED ... Deployment override passed: -vbparams=mweb:-2:0

    on a run that adopted an already-running daemon. `-vbparams` IS A STARTUP
    FLAG. On an adopted daemon the harness computed it, printed it, and never
    handed it to anything -- so the sentence reported an action that did not
    happen, and the step 8 failure text downstream then told the operator "the
    fix is -vbparams=mweb:-2:0" directly underneath a line claiming it had
    already been passed. An operator reading that has been sent in a circle by
    their own tooling.

    Measured on the operator's host, 2026-09-28: LTC adopted a daemon at
    height 2504, the mweb row was present, and mining died at exactly the
    predicted height with the predicted `bad-txns-vin-empty`. The diagnosis was
    right and the remedy was unreachable, because the flag cannot be applied to
    a process that is already running.
    """
    if asset != "LTC":
        return "MWEB is a Litecoin deployment and does not apply to this chain"
    override = [arg for arg in extra_args if "mweb" in arg]
    if "mweb" in softforks and not we_started_it:
        # THE CASE THAT USED TO LIE. Say what could not have happened, and what
        # the operator can actually do about it -- this harness will not stop a
        # daemon it did not start (rule 13's other half: a reaper reaps what it
        # spawned), so the restart is theirs to make.
        return (
            f"MWEB IS LISTED, so it is live on this daemon and mining will fail at height 288. "
            f"THE OVERRIDE WAS NOT APPLIED AND COULD NOT HAVE BEEN: this run ADOPTED a daemon "
            f"that was already running, and -vbparams is a startup flag. The harness computed "
            f"{' '.join(override) or '(none)'} and had nothing to hand it to. To get past 288, "
            f"stop that daemon yourself and re-run so this harness starts one -- it will not "
            f"stop a daemon it did not start."
        )
    if "mweb" in softforks:
        return (
            f"MWEB IS LISTED, so it is live on this daemon and mining will fail at height 288. "
            f"This harness STARTED this daemon and passed: {' '.join(override) or '(none)'} -- "
            f"so either the build ignored the flag or it is not the right one. That is a defect "
            f"in mweb_override_args(), not something a restart fixes."
        )
    if override:
        return (
            f"MWEB is NOT listed, which is what {' '.join(override)} looks like when it works -- a deployment held "
            "at NEVER_ACTIVE is omitted from getblockchaininfo rather than reported inactive"
        )
    return (
        "MWEB is NOT listed and no override was passed, so this build has no MWEB deployment. Nothing should "
        "fail at height 288"
    )


# The deployment that IS CHECKLOCKTIMEVERIFY. Named once; every lookup below
# uses this key, because "bip65" is what both daemon families call it in their
# softfork and deployment tables.
CLTV_DEPLOYMENT = "bip65"


class SupportsCall(Protocol):
    """Anything that can make ONE JSON-RPC call and hand back whatever came out.

    WHY A PROTOCOL AND NOT `RegtestRPC`, which is what the two functions below
    said until 2026-10-09. Between them they reach for exactly one member --
    `node.call("getdeploymentinfo")`, in `_deployment_tables` -- and
    `cltv_activation_height` does not touch `node` at all beyond handing it
    straight down. Annotating them `RegtestRPC` promised a url, credentials, a
    timeout, a wallet path and every send method on the adapter, none of which is
    read, and `RegtestRPC` is a NOMINAL class: a stand-in that implements `call`
    and only `call` is refused on the grounds of its name.

    That refusal is the whole cost, and it lands on the honest stub rather than
    the over-promising signature. tests/test_regtest_harness_units.py's
    `_StubNode` answers RPCs from a dict of canned results and RECORDS which
    methods were asked in which order, because the assertions in that file are
    about the order. It opens no socket and needs no credentials, so there is
    nothing for it to inherit from the real adapter that would not be a lie; the
    alternatives to this Protocol were a live daemon in a unit test or a
    suppression.

    `Any` IS THE RETURN TYPE BECAUSE A JSON-RPC `result` IS ANY JSON VALUE. The
    callers below already treat it as unknown -- `isinstance(deployment_info,
    dict)` is the first thing `_deployment_tables` does with it -- and narrowing
    this to `dict` would be a claim about every daemon's answer that this file
    refuses to make everywhere else.

    THE NEAR-DUPLICATE, named because rule 8 asks for it: `solana_chain_check.py`
    carries `RpcCaller`, structurally this same one-member Protocol. They are not
    merged because they sit in different suites with no shared import path
    between them -- this is `swap_terminal/regtest/`, that is a root-level entry
    point -- and because the methods each names are different vocabularies
    (`getdeploymentinfo` against Solana's). If a shared `swap_terminal/` home for
    transport Protocols is ever created, both belong in it.
    """

    def call(self, method: str, *params: object) -> Any: ...


def _deployment_tables(node: SupportsCall, info: dict) -> list[tuple[str, dict]]:
    """Every place a daemon might keep its deployment table, with where it came from.

    TWO PLACES, because the field moved. Bitcoin Core carried `softforks` in
    `getblockchaininfo` until v25, which moved it to `getdeploymentinfo`.
    Measured on the operator's machine 2026-09-25: Litecoin Core 0.21.4 filled
    `softforks`, and Bitcoin Core 28.1 printed

        BTC: softforks reported by the daemon: (none: no `softforks` field ...)

    which the harness correctly reported and then did nothing with. Asking both
    places is the whole fix; guessing from a version number is what this file
    refuses to do everywhere else.
    """
    tables: list[tuple[str, dict]] = []
    softforks = info.get("softforks")
    if isinstance(softforks, dict) and softforks:
        tables.append(("getblockchaininfo.softforks", softforks))
    try:
        deployment_info = node.call("getdeploymentinfo")
    except RPCError:
        return tables
    deployments = deployment_info.get("deployments") if isinstance(deployment_info, dict) else None
    if isinstance(deployments, dict) and deployments:
        tables.append(("getdeploymentinfo.deployments", deployments))
    return tables


def cltv_activation_height(node: SupportsCall, info: dict) -> tuple[int | None, str]:
    """The height at or above which CHECKLOCKTIMEVERIFY is enforced by CONSENSUS.

    WHY THIS DECIDES WHERE THE REFUND TEST HAS TO RUN, and it is the finding
    that prompted this function. The operator's run of 2026-09-25 printed, from
    the Litecoin daemon itself:

        LTC: softfork bip65: type=buried active=False height=1351

    and then asserted the refund branch at heights 1252 and 1253. BIP65 IS
    CHECKLOCKTIMEVERIFY, so at those heights the opcode was not consensus
    enforced: the refusal that came back was

        LTC 8b: non-mandatory-script-verify-flag (Locktime requirement not satisfied)

    against BTC's `mandatory-script-verify-flag-failed`. `non-mandatory` means
    the transaction passed consensus and was declined by RELAY POLICY. The
    mempool would not carry it; the chain would not have refused it, and a
    miner not applying that policy could have included an early refund. The
    test demonstrated something weaker than it claimed, which is the same
    defect class as a verdict asserting more than its run supports.

    This is a REGTEST ARTIFACT and not a Litecoin mainnet vulnerability --
    BIP65 has been active there since 2015. It is a defect in the instrument.

    Returns (height, how it was learned). A None height is an answer too, and
    the caller must say that the activation height could not be read rather
    than assume the test was run under consensus rules.
    """
    for source, table in _deployment_tables(node, info):
        entry = table.get(CLTV_DEPLOYMENT)
        if not isinstance(entry, dict):
            continue
        height = entry.get("height")
        if height is None:
            height = entry.get("bip9", {}).get("since") if isinstance(entry.get("bip9"), dict) else None
        if height is not None:
            return int(height), f"{source}[{CLTV_DEPLOYMENT}].height, with active={entry.get('active')}"
    return None, (
        "neither getblockchaininfo.softforks nor getdeploymentinfo gave a height for "
        f"{CLTV_DEPLOYMENT}; this daemon does not say when CHECKLOCKTIMEVERIFY becomes consensus-enforced"
    )


def probe_capabilities(console: ConsoleLike, config: ChainConfig, wallet: str = "") -> dict:
    """Ask the daemon what it can do, instead of guessing from a version string.

    Returns a dict the later steps read, and prints every field, because the
    value of this block is mostly in a pasted run: "signrawtransaction:
    present" on LTC and "absent" on BTC explains, by itself, why the two
    clients take different routes through the same failure.
    """
    node = adapter_for(config)
    capabilities: dict[str, object] = {}
    capabilities["subversion"] = _build_string(node)

    for method in ("signrawtransactionwithwallet", "signrawtransactionwithkey", "signrawtransaction",
                   "importaddress", "importprivkey", "importdescriptors", "generatetoaddress",
                   "generateblock", "getdeploymentinfo"):
        capabilities[method] = method_exists(node, method)

    if wallet:
        wallet_node = adapter_for(config, wallet=wallet)
        try:
            wallet_info = wallet_node.call("getwalletinfo")
            # A wallet that does not know the word `descriptors` predates
            # descriptor wallets, which IS the answer: it is a legacy wallet.
            capabilities["descriptor_wallet"] = bool(wallet_info.get("descriptors", False))
            capabilities["wallet_name"] = wallet_info.get("walletname", wallet)
        except RPCError as exc:
            capabilities["descriptor_wallet"] = f"(none: getwalletinfo failed: {exc})"

    for key, val in capabilities.items():
        console.say(f"{config.asset} capability {key}: {val}")
    return capabilities


# WHICH FIELD OF `getnetworkinfo` CARRIES THE BUILD STRING, LONGEST-STANDING FIRST.
#
# `subversion` FIRST because that is the BIP14 field Bitcoin Core and Litecoin Core both
# push ("/Satoshi:28.1.0/"), and `version` second because Gridcoin pushes that instead --
# an integer on Core, but a STRING on Gridcoin (src/rpc/net.cpp: res.pushKV("version",
# FormatFullVersion())). Order matters for exactly the reason rule 11 gives for
# _HORIZON_SUFFIXES: both names exist on some family, so the more specific one is asked
# first rather than whichever happens to be present.
#
# MEASURED 2026-09-28 on the operator's Gridcoin testnet daemon, and it is why this is a
# table rather than one `.get()`. The run printed
#
#     subversion=None
#
# for the whole of step 3. That `None` is NOT a failed call -- the RPCError branch below
# writes a sentence, so a bare None proves getnetworkinfo ANSWERED and simply has no
# `subversion` key. Gridcoin master's getnetworkinfo pushes eleven fields and `subversion`
# is not among them at any version, while the answer the line wanted was in the SAME
# RESPONSE under `version`. That is `getblockchaininfo.chain` again, one method over: the
# right method asked for a key this family does not have, with the value sitting beside it.
#
# A source reading about MASTER is not a measurement of the operator's build (their daemon
# answers False for signrawtransactionwithkey, which master has), which is why this reads
# both names off the response it actually got and PRINTS WHICH ONE ANSWERED rather than
# assuming either.
_BUILD_STRING_FIELDS = ("subversion", "version")


def _build_string(node: RegtestRPC) -> str:
    """The daemon's build string, naming the field it came from -- or why there is none.

    FOUR OUTCOMES, ALL DISTINGUISHABLE IN ONE LINE OF PASTED OUTPUT, because collapsing any
    two of them is what made `subversion=None` unreadable (rule 14: an absent thing must not
    print the same as a broken one):

      "/Satoshi:28.1.0/ (getnetworkinfo.subversion)"   the field was there
      "6.1.0.0-... (getnetworkinfo.version)"           the sibling field was there instead
      "(none: getnetworkinfo answered ... no ... )"     the method works, neither field exists
      "(none: getnetworkinfo failed: ...)"              the call itself did not answer

    Only the LAST of those is something being wrong. The third is a fact about the daemon
    family and reads as one.

    Returns a str always. `node.call()` returning a non-dict is handled rather than allowed
    to raise AttributeError: this is step 3 of a ten-step run, and an unhandled
    AttributeError here is scored `FAIL <asset> run: got=AttributeError` by
    adaptor_regtest_verify.run_chain() -- a version-string probe reported as a crash of the
    whole harness.
    """
    try:
        info = node.call("getnetworkinfo")
    except RPCError as exc:
        return f"(none: getnetworkinfo failed: {exc})"
    if not isinstance(info, dict):
        return f"(none: getnetworkinfo answered {type(info).__name__}, not an object: {info!r})"
    for name in _BUILD_STRING_FIELDS:
        value = info.get(name)
        if value not in (None, ""):
            return f"{value} (getnetworkinfo.{name})"
    return (
        f"(none: getnetworkinfo answered {len(info)} field(s) and none of {list(_BUILD_STRING_FIELDS)} "
        f"is among them -- this daemon family does not report a build string there. "
        f"The fields it did return: {sorted(info)})"
    )


def method_exists(node: RegtestRPC, method: str) -> bool:
    """Whether the daemon recognizes an RPC name, via `help <method>`.

    `help` returns a STRING for an unknown command rather than raising, on all
    three daemon families, so the test is on the text. Verified against
    Gridcoin's own source as well as measured: src/rpc/server.cpp builds
    `"help: unknown command: %s\n"` and strips the newline, and Bitcoin Core
    builds the same sentence -- which is why the prefix, not an exception, is
    what this reads. Calling the method itself to find out would mean calling
    `importprivkey` to learn whether it exists.

    THE SECOND PLACE THIS REPOSITORY ANSWERS "DOES THIS DAEMON HAVE METHOD X",
    AND THE DIFFERENCE IS THE POINT (CLAUDE.md rule 8, which asks for a comment
    at BOTH sites naming the other). The other is
    modules/rpc_method_support.rpc_failure_report(), and they ask at opposite
    ends of a call:

      this function          BEFORE. A proactive probe, one extra round trip,
                             so a step can decide not to attempt something --
                             _mine_early_cancel() SKIPs on no `generateblock`
                             rather than scoring its absence as a refusal.
      rpc_failure_report()   AFTER. It is handed an exception that already
                             happened and decides how loudly to report it, from
                             a table of methods a caller has a FALLBACK for
                             (`gettxout`, which Gridcoin does not have).

    Neither can replace the other: a probe cannot quiet an error that has
    already been raised four routes deep, and an error-code table cannot stop a
    harness attempting something it should skip. They must not disagree about
    which absences are expected, which is why each names the other here.

    A THIRD reader of the same fact is regtest/steps._WALLET_CAPABILITY_MARKERS,
    which folds "method not found" into a redeem-stage classification. That one
    is a per-stage diagnosis rather than a capability question, and it is named
    here so a reader who changes what a -32601 means finds all three.
    """
    try:
        text = node.call("help", method)
    except RPCError:
        return False
    return not str(text).lower().startswith("help: unknown command")


def ensure_wallet(console: ConsoleLike, config: ChainConfig, wallet_name: str) -> str:
    """Load the named wallet, creating it if it is not there. Returns the name.

    Three RPCs in a deliberate order, because the daemons disagree about what
    happens when a wallet is already loaded and neither agrees with the other
    about descriptor defaults:

      listwallets   -- if it is already loaded, do nothing. Loading a loaded
                       wallet is an error on both families, and catching that
                       error would be indistinguishable from a real failure.
      loadwallet    -- for a wallet that exists on disk from a previous run.
      createwallet  -- last, because it is the only one that writes.

    `createwallet` is called with the NAME ONLY. Bitcoin Core 28.1 makes a
    descriptor wallet; Litecoin Core 0.21.4 makes a legacy one. The harness
    does not ask for either, and does not pass descriptors=false to force a
    legacy wallet on Core 28 -- that needs -deprecatedrpc=create_bdb there, and
    branching on a guess about a deprecation token is exactly what this file
    refuses to do. Which kind was created is REPORTED by probe_capabilities(),
    and step 3 says what it means for the real client -- which since 2026-09-25
    reads the same `getwalletinfo.descriptors` field itself and picks
    importdescriptors or importaddress accordingly, rather than calling a
    legacy RPC and raising when it is refused.
    """
    node = adapter_for(config)
    try:
        loaded = node.call("listwallets") or []
    except RPCError as exc:
        # Named rather than left to the generic handler, because this is the
        # first WALLET call of the run and a daemon built without wallet
        # support fails exactly here, with a message nobody would connect to
        # wallets if it arrived as an unhandled exception three frames up.
        raise RegtestSetupError(
            f"{config.asset}: listwallets failed on {config.base_url}: {exc}. "
            "A daemon built with --disable-wallet has no wallet RPCs at all; otherwise check that the [regtest] "
            "credentials in the config match ST_REGTEST_"
            f"{config.asset}_RPC_USER / _RPC_PASSWORD."
        ) from exc
    if wallet_name in loaded:
        console.say(f"{config.asset}: wallet {wallet_name!r} is already loaded")
        return wallet_name
    try:
        node.call("loadwallet", wallet_name)
        console.say(f"{config.asset}: loaded existing wallet {wallet_name!r} from disk")
        return wallet_name
    except RPCError as load_error:
        console.say(f"{config.asset}: loadwallet said {load_error}; creating {wallet_name!r}")
    try:
        node.call("createwallet", wallet_name)
    except RPCError as create_error:
        raise RegtestSetupError(
            f"{config.asset}: could not create wallet {wallet_name!r}: {create_error}. "
            f"If a wallet of that name exists but is broken, remove {config.regtest_dir / 'wallets' / wallet_name} "
            "or rerun with --wipe."
        ) from create_error
    console.say(f"{config.asset}: created wallet {wallet_name!r}")
    return wallet_name


def read_pid(config: ChainConfig) -> int | None:
    """The daemon's pid from its own pid file, or None if there is no file.

    A pid file rather than a `pgrep -f` pattern, which rule 13 asks for
    explicitly: a pattern matches what a command line happens to look like
    today, and two regtest daemons on one machine match each other's.
    """
    try:
        text = config.pid_path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    try:
        return int(text)
    except ValueError:
        return None


def _process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # It exists and belongs to somebody else. Still alive for our purposes,
        # and a stop we cannot perform must not be reported as one that worked.
        return True
    return True


def stop_daemon(console: ConsoleLike, config: ChainConfig, we_started_it: bool) -> None:
    """Ask the daemon to stop and then PROVE it is gone (rule 13).

    The pid is read BEFORE the stop request, because the daemon deletes its pid
    file on the way out and a pid read afterwards is None whether it shut down
    cleanly or is still flushing a 300MB chainstate.
    """
    if not we_started_it:
        # Two different situations share this branch and must not share a line
        # (rule 14: "did nothing" must not look like "did work"). Either a
        # daemon was adopted and is deliberately left alone, or nothing ever
        # came up and there is nothing to stop.
        if rpc_answers(config):
            console.say(
                f"{config.asset}: a daemon is still answering at {config.base_url} and is LEFT RUNNING -- "
                "this harness did not start it, so it will not stop it."
            )
        else:
            console.say(f"{config.asset}: nothing to stop -- no daemon was started and none is answering")
        return

    pid = read_pid(config)
    console.say(f"{config.asset}: stopping daemon pid={pid if pid is not None else '(none: no pid file)'}")
    try:
        adapter_for(config).call("stop")
    except Exception as exc:  # noqa: BLE001 -- checked: a stop request that fails to SEND is not a stop that failed. The absence check below is the actual assertion, and it runs either way; this line only records why the polite route did not work.
        console.say(f"{config.asset}: stop RPC said {type(exc).__name__}: {exc}; checking for the process anyway")

    if pid is None:
        # No pid file to check against, so fall back to the weaker evidence:
        # RPC stops answering. Say that it is weaker rather than printing the
        # same success line as the strong check (rule 14).
        deadline = time.monotonic() + STOP_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            if not rpc_answers(config):
                console.check(
                    f"{config.asset} daemon stopped",
                    "RPC no longer answers (WEAKER evidence: no pid file was found to check)",
                    "the process to be gone",
                    OK,
                )
                return
            time.sleep(POLL_INTERVAL_SECONDS)
        console.check(f"{config.asset} daemon stopped", "RPC still answering", "the process to be gone", FAIL)
        return

    started = time.monotonic()
    deadline = started + STOP_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if not _process_alive(pid):
            console.check(
                f"{config.asset} daemon stopped",
                f"pid {pid} is gone after {format_duration(time.monotonic() - started)}",
                "the process to be gone",
                OK,
            )
            return
        time.sleep(POLL_INTERVAL_SECONDS)
    console.check(
        f"{config.asset} daemon stopped",
        f"pid {pid} is STILL ALIVE after {format_duration(STOP_TIMEOUT_SECONDS)}",
        "the process to be gone",
        FAIL,
    )


def wipe_datadir(console: ConsoleLike, config: ChainConfig) -> None:
    """Delete the `regtest` subdirectory so a rerun starts from an empty chain.

    Only the `regtest` subdirectory, never the datadir itself: the datadir holds
    the operator's bitcoin.conf, and a harness that deletes a config file the
    operator wrote by hand has done something much worse than fail.

    Refuses if the daemon is still answering, because deleting a live chainstate
    produces corruption that looks like a bug in this repository.
    """
    if rpc_answers(config):
        raise RegtestSetupError(
            f"--wipe refused for {config.asset}: a daemon is still answering on {config.base_url}. "
            "Stop it first; deleting a live datadir corrupts it."
        )
    target = config.regtest_dir
    if not target.exists():
        console.say(f"{config.asset}: nothing to wipe -- {target} does not exist")
        return
    shutil.rmtree(target)
    console.say(f"{config.asset}: wiped {target}")
