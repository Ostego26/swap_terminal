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
  subversion           from getnetworkinfo, printed so a pasted run says which
                       builds produced it.
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

import requests
from chains.base import RPCAdapter, RPCError
from microfortnights import format_duration
from regtest.console import FAIL, OK, Console

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
    override inherited from Bitcoin Core. A deployment whose start and timeout
    are both zero is STARTED and immediately timed out, so it reaches FAILED
    and never activates -- the conventional way to switch a deployment off on
    regtest. `mweb` is the deployment name Litecoin gives it.

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
        ["-vbparams=mweb:0:0"],
        "this build advertises -vbparams, so MWEB is held at start=0/timeout=0, which reaches FAILED and never "
        "activates. If the daemon refuses to start with it, the harness retries without it and says so",
    )


def check_binaries(console: Console, config: ChainConfig) -> str:
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


def rpc_answers(config: ChainConfig) -> bool:
    """True if something on the configured port answers `uptime`.

    `uptime` is chosen because it needs no wallet and exists on both daemon
    families, so a True here means "a daemon is up and our credentials work",
    not "a TCP port is open".
    """
    try:
        adapter_for(config).call("uptime")
    except Exception:  # noqa: BLE001 -- checked: this is a PROBE whose two answers are "yes" and "not yet". It is called only from readiness polling, and every caller that needs the REASON for a no calls again through wait_for_rpc(), which reports the last error verbatim.
        return False
    return True


def start_daemon(console: Console, config: ChainConfig) -> bool:
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

    base_argv = [resolved, f"-datadir={config.datadir}", "-regtest", "-daemon"]
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


def _spawn(console: Console, config: ChainConfig, argv: list[str]):
    console.say(f"{config.asset}: starting {' '.join(argv)}")
    return subprocess.run(  # noqa: S603 -- checked: argv[0] is from shutil.which; the datadir and the options come from this harness's own defaults and its own -help probe, never from a network source. No shell.
        argv,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def wait_for_rpc(console: Console, config: ChainConfig) -> None:
    """Poll until the daemon answers, printing progress. Never a fixed sleep.

    A fixed sleep is wrong in both directions: too short and the harness
    reports a connection failure for a daemon that was still reindexing, too
    long and every run pays for the worst case. Polling also lets the wait
    SAY something, which a sleep cannot (rule 14).
    """
    started = time.monotonic()
    deadline = started + RPC_READY_TIMEOUT_SECONDS
    attempts = 0
    last_error = "(none: never got far enough to fail)"
    while time.monotonic() < deadline:
        attempts += 1
        try:
            adapter_for(config).call("uptime")
        except Exception as exc:  # noqa: BLE001 -- checked: every failure is kept in `last_error` and reported verbatim if the deadline passes. Nothing here can return a value the caller would mistake for a connected daemon.
            last_error = f"{type(exc).__name__}: {exc}"
            console.say(
                f"{config.asset}: waiting for RPC on {config.base_url}, attempt {attempts}, "
                f"{format_duration(time.monotonic() - started)} of {format_duration(RPC_READY_TIMEOUT_SECONDS)} -- {last_error}"
            )
            time.sleep(POLL_INTERVAL_SECONDS)
            continue
        console.say(
            f"{config.asset}: RPC answered after {format_duration(time.monotonic() - started)} "
            f"({attempts} attempt(s))"
        )
        return
    raise RegtestSetupError(
        f"{config.asset} daemon did not answer RPC at {config.base_url} within "
        f"{format_duration(RPC_READY_TIMEOUT_SECONDS)} ({attempts} attempts). Last error: {last_error}. "
        f"Check that {config.conf_path} has server=1 and a [regtest] section with rpcuser={config.rpc_user}, "
        f"rpcpassword=<yours> and rpcport={config.port}, and that "
        f"ST_REGTEST_{config.asset}_RPC_USER / _RPC_PASSWORD match it."
    )


def assert_regtest(console: Console, config: ChainConfig) -> dict:
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
    report_softforks(console, config, info)
    return info


def report_softforks(console: Console, config: ChainConfig, info: dict) -> None:
    """Print each deployment and its status, from the daemon's own mouth.

    This exists for one measured reason. Mining toward the LTC locktime died
    with `bad-txns-vin-empty` several hundred blocks in, and Mimblewimble
    Extension Blocks -- which activate BY HEIGHT on Litecoin -- are the leading
    hypothesis. A hypothesis about an activation height is settled by asking
    the daemon what its activation heights are, not by reasoning about them, so
    this block is printed before anything mines. If MWEB shows active, or
    active at a height the run will cross, the later failure has its
    explanation attached to it rather than inferred afterwards.

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


# The deployment that IS CHECKLOCKTIMEVERIFY. Named once; every lookup below
# uses this key, because "bip65" is what both daemon families call it in their
# softfork and deployment tables.
CLTV_DEPLOYMENT = "bip65"


def _deployment_tables(node: RegtestRPC, info: dict) -> list[tuple[str, dict]]:
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


def cltv_activation_height(node: RegtestRPC, info: dict) -> tuple[int | None, str]:
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


def probe_capabilities(console: Console, config: ChainConfig, wallet: str = "") -> dict:
    """Ask the daemon what it can do, instead of guessing from a version string.

    Returns a dict the later steps read, and prints every field, because the
    value of this block is mostly in a pasted run: "signrawtransaction:
    present" on LTC and "absent" on BTC explains, by itself, why the two
    clients take different routes through the same failure.
    """
    node = adapter_for(config)
    capabilities: dict[str, object] = {}
    try:
        capabilities["subversion"] = node.call("getnetworkinfo").get("subversion")
    except RPCError as exc:
        capabilities["subversion"] = f"(none: getnetworkinfo failed: {exc})"

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


def method_exists(node: RegtestRPC, method: str) -> bool:
    """Whether the daemon recognizes an RPC name, via `help <method>`.

    `help` returns a STRING for an unknown command rather than raising, on both
    daemon families, so the test is on the text. Calling the method itself to
    find out would mean calling `importprivkey` to learn whether it exists.
    """
    try:
        text = node.call("help", method)
    except RPCError:
        return False
    return not str(text).lower().startswith("help: unknown command")


def ensure_wallet(console: Console, config: ChainConfig, wallet_name: str) -> str:
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
    and what it means for the real client's importaddress/importprivkey calls
    is reported where those fail.
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


def stop_daemon(console: Console, config: ChainConfig, we_started_it: bool) -> None:
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


def wipe_datadir(console: Console, config: ChainConfig) -> None:
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
