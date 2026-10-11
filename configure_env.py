#!/usr/bin/env python3
"""Discover what it can, ask for the rest, and write it into .env so it PERSISTS.

Role: file (entry point -- what an operator runs)
Reads: .env (by name, never echoed), .env.example for the variable list and each
       variable's compose default, swap_terminal/network_target.py's CHAIN_PORTS for
       the test ports, /proc/net/tcp* for which of those ports are listening, and
       `dfx canister id` / `dfx identity get-principal` inside the icp-replica compose
       service
Writes: .env, mode 600, through a temp file in the same directory and a rename. Every
       line it does not own is carried through byte for byte. No .bak, ever (rule 2).
Can move funds: NO. It writes configuration and never calls an RPC that sends. What it
       CONFIGURES can move funds: an RPC credential here is what lets the payout worker
       broadcast, which is the point of persisting it.
Mainnet-safe: the SCRIPT is. What it configures is decided by the values given to it.
       The defaults it offers are TESTNET/DEVNET only and it says so per value; a
       mainnet port typed in is the operator's decision and this cannot tell.

=============================================================================
WHY THIS EXISTS, AND IT IS THE THIRD TIME THE SAME THING HAS BROKEN
=============================================================================

Operator, 2026-10-11, looking at an ATM page where all six coins read NONE: "hey why is
it completely fucking broken?"

MEASURED on their host in the same minute:

    chain variables present in the web container   44
    BTC_RPC_PORT / USER / PASS                     EMPTY
    LTC_RPC_PORT / USER / PASS                     EMPTY
    GRC_RPC_PORT / USER / PASS                     EMPTY
    SOL_RPC_URL, SOL_DEPOSIT_ACCOUNT, SOL_HOT_WALLET   EMPTY
    XRP_RPC_URL, XRP_DEPOSIT_ACCOUNT               EMPTY
    ICP_LEDGER_CANISTER_ID, ICP_OWNER_PRINCIPAL,
      ICP_DFX_SERVICE                              EMPTY
    names in .env                                  COMPOSE_FILE, SWAP_DB_DIR,
                                                   GRIDCOIN_WALLET_PASSPHRASE

Forty-four variables present and empty, because docker-compose.web.yml passes each one
as `${VAR:-}` -- so an unset SHELL variable becomes an empty STRING in the container,
chains/registry.build_adapters() skips every chain that has no port or url, and the page
correctly reports six chains with no adapter. Nothing was broken. Nothing was
configured.

AND THE OPERATOR HAD ALREADY NAMED THIS DEFECT TWICE. "hey what the fuck? yeah, new
shell should not rat fuck the entire fucking machine. it should persist. what the fuck",
and then "i already fucking told you to make it presistent and you're asking me if i
want ephemeral". It was fixed for COMPOSE_FILE, for SWAP_DB_DIR and for
GRIDCOIN_WALLET_PASSPHRASE -- three variables out of forty-seven -- and the other
forty-four stayed in whatever shell happened to export them. A host restart, or
`docker compose up -d --force-recreate web` from a fresh terminal, loses all of them at
once. That is what happened.

=============================================================================
DISCOVER BEFORE ASKING, BECAUSE A PROMPT FOR FORTY VALUES IS NOT A FIX
=============================================================================

Most of these are already knowable from the running system, and asking for something the
machine can tell you is how a setup step gets skipped:

  the three RPC ports   probed. network_target.CHAIN_PORTS already holds each chain's
                        test ports and exists because three places needed the same
                        answer; this is the fourth, and it reads the table rather than
                        spelling 18443 again (rule 8).
  the ICP canister id   asked of the replica with `dfx canister id`, inside the
  and principal         icp-replica SERVICE -- never host dfx in icp/, which HANDOFF.md
                        section 1 records as the single most destructive mistake
                        available here: host dfx judged the container's root-owned
                        .dfx/local incohesive and tried to DELETE it, and only a
                        permission error stopped it taking the ledger with it.
  SOL and XRP endpoints the documented devnet/testnet URLs, offered as a default the
                        operator accepts or overrides.
  everything else       asked. Public keys are echoed because they are public; anything
                        .env.example marks `# SECRET` is read with no echo and never
                        printed, never logged and never put in argv.

WHAT IT WILL NOT DO, and these are refusals rather than gaps:

  read a daemon's conf  bitcoin.conf and gridcoinresearch.conf hold rpcpassword, and
                        CLAUDE.md's chain-safety rules say never move, copy or read back
                        a credential -- "if a task seems to need one, it is a proposal
                        for the operator, not a step to take". So the password is typed,
                        not harvested, even though harvesting would be more convenient.
  read a keypair file   ~/.config/solana/id.json is a private key. `solana address`
                        reads it to print the public one, and this does not run it. The
                        public key is asked for instead.
  guess a mainnet value every default it offers is a test network. It cannot tell a
                        mainnet port from a test one once typed, and says so rather than
                        implying it validated anything.
"""

from __future__ import annotations

import argparse
import getpass
import os
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO_ROOT / "swap_terminal"))

from chains.daemon_conf import parse_daemon_conf  # noqa: E402 -- after the sys.path line above
from config import database_path  # noqa: E402
from network_target import CHAIN_PORTS  # noqa: E402 -- as every root tool does

ENV_FILE = REPO_ROOT / ".env"
ENV_EXAMPLE = REPO_ROOT / ".env.example"

#: `.env.example` marks a credential with this on the line above the assignment, and
#: that marker is the ONLY thing that decides whether a value is read without echo.
#: Derived from the template rather than listed here, so a credential added there is
#: withheld here without anybody remembering to add it twice (rule 8).
#:
#: NAMED `NO_ECHO_MARKER` AND NOT `SECRET_MARKER`, which is ruff's S105 answered rather
#: than suppressed (rule 19): the rule flags a NAME that looks like it holds a
#: credential, and this holds a four-character marker that appears in a template in
#: version control. Renaming is the honest fix -- a `noqa` here would be a claim about
#: the value, and the next reader would have to re-establish it. tests/conftest.py's
#: RPC_FIXTURE_AUTH is the same move for the same rule.
NO_ECHO_MARKER = "# SECRET"

#: /proc/net/tcp's columns, up to the one this reads. `sl local_address rem_address st`
#: -- so state is field 3 and a line with fewer fields is a header or a truncated read.
#: Named for the same reason swap_terminal/loopback.py names _ROUTE_MIN_FIELDS: a bare 3
#: in a slice of somebody else's file format is a number the next reader has to go and
#: count.
_PROC_NET_MIN_FIELDS = 4

#: What /proc/net/tcp writes in the state column for LISTEN.
_PROC_NET_LISTEN = "0A"

#: How many listening test ports a chain must have for this to offer one without
#: guessing. Named because the bare `1` in that comparison is the entire policy: two
#: means two networks of one chain are up and picking either would be a guess about what
#: the operator means, so the value is asked for instead.
_EXACTLY_ONE = 1

#: Documented test endpoints, offered as defaults. TESTNET AND DEVNET ONLY -- a mainnet
#: URL typed over one of these is the operator's decision and this script cannot tell.
ENDPOINT_DEFAULTS = {
    "SOL_RPC_URL": ("https://api.devnet.solana.com", "Solana DEVNET"),
    "XRP_RPC_URL": ("https://s.altnet.rippletest.net:51234", "XRP TESTNET"),
}

#: The compose SERVICE name the ICP adapter execs dfx in. NOT the container name --
#: `docker compose exec` rejects `swap-icp-replica`, and that distinction is written at
#: the call site in chains/icp.py because it was got wrong once.
ICP_SERVICE = "icp-replica"

#: What this script asks the replica for, and the dfx command that answers it.
ICP_DISCOVERY = {
    "ICP_LEDGER_CANISTER_ID": ["dfx", "canister", "id", "icp_ledger_canister"],
    "ICP_OWNER_PRINCIPAL": ["dfx", "identity", "get-principal"],
}

#: WHERE EACH DAEMON'S OWN CONF LIVES, and these are the STANDARD Core datadirs --
#: deliberately NOT regtest.daemons.CHAIN_DEFAULTS' datadirs, which are the harness's
#: own `~/regtest/btc` and `~/regtest/ltc`. A reader who finds one of these tables must
#: know the other exists and answers a different question (rule 8): that one is "where
#: does the test harness put a daemon it started", this one is "where did the operator's
#: own daemon put its conf".
#:
#: Confirmed against the operator's host 2026-10-11: `~/.litecoin/litecoin.conf` and
#: `~/.GridcoinResearch/gridcoinresearch.conf` both exist with rpcuser and rpcpassword;
#: `~/.bitcoin/bitcoin.conf` does not exist and bitcoind is not running.
DAEMON_CONFS = {
    "BTC": ("~/.bitcoin/bitcoin.conf", "BTC_RPC_USER", "BTC_RPC_PASS"),
    "LTC": ("~/.litecoin/litecoin.conf", "LTC_RPC_USER", "LTC_RPC_PASS"),
    "GRC": ("~/.GridcoinResearch/gridcoinresearch.conf", "GRC_RPC_USER", "GRC_RPC_PASS"),
}

#: Which conf SECTION a given port's network uses. Bitcoin Core names them `[main]`,
#: `[test]` and `[regtest]`, and credentials are conventionally at the TOP level while
#: the port sits in a section -- so the section name decides which rpcport a conf
#: reports, and reading the wrong one reports a port the daemon is not on.
#:
#: DERIVED FROM THE PROBED PORT rather than from a fixed per-chain default, which is the
#: one place this diverges from chains/daemon_conf.CONF_FALLBACK_NETWORK. That table
#: hardcodes regtest for BTC and LTC; the operator's litecoind answers on 19332, which is
#: TESTNET, so a fixed "regtest" would read the wrong section of their real conf.
CONF_SECTION_FOR_PORT = {18332: "test", 18443: "regtest", 19332: "test", 19443: "regtest"}

#: GRC IS REFUSED BY DEFAULT AND NEEDS ITS OWN FLAG, and the reason is quoted from
#: chains/daemon_conf.CONF_FALLBACK_NETWORK, which excludes Gridcoin entirely:
#:
#:     "Its conf lives in ~/.GridcoinResearch, shared by mainnet and testnet both, and
#:      the operator's mainnet wallet is a live staking wallet holding real coins.
#:      Picking a connection out of that file is how a reader -- or worse, a driver that
#:      sends -- ends up pointed at it."
#:
#: THIS TOOL'S CASE IS NARROWER THAN THE ONE THAT TABLE REFUSES, which is why a separate
#: opt-in is defensible rather than an override: that table picks a whole CONNECTION,
#: port included, out of the shared file. Here GRC_RPC_PORT has already been established
#: by probing -- 25715 on the operator's host, which network_target.classify() calls TEST
#: -- so only the credentials come from the conf and the port decides which daemon they
#: reach. chains/daemon_network.test_network_verdict() is still the backstop that refuses
#: a mainnet answer before anything is asked of a wallet.
#:
#: It is a SEPARATE flag and not part of --read-daemon-confs because the hazard is
#: different in kind, and a single flag would let an operator accept it without reading
#: this.
GRIDCOIN_CONF_HAZARD = (
    "~/.GridcoinResearch/gridcoinresearch.conf is shared by MAINNET and testnet, and a "
    "Gridcoin mainnet wallet is typically a live staking wallet holding real coins. "
    "chains/daemon_conf.py excludes Gridcoin from conf resolution for exactly that reason. "
    "Reading only the CREDENTIALS is narrower -- GRC_RPC_PORT is established by probing, so "
    "the port decides which daemon they reach -- but it is still your decision, not this "
    "tool's. Pass --read-gridcoin-conf to accept it."
)

#: THE SHARED DEPOSIT ACCOUNT VARIABLE FOR EACH CHAIN THAT HAS ONE, and this table is
#: narrow for a reason that would cost a customer their money if it were widened.
#:
#: SOL AND XRP ONLY, because they are the only two chains where ONE account receives every
#: swap's deposit and a discriminator -- a memo, a destination tag -- says whose it is.
#: services/swap_service.TAG_ATTRIBUTED_ASSETS is frozenset({"XRP", "SOL"}) and this is the
#: same fact: for those two, any swap's `deposit_address` IS the configured account.
#:
#: NEVER BTC, LTC OR GRC. Each of their swaps gets its OWN freshly derived address, so a
#: `deposit_address` read off one of their rows is ONE CUSTOMER'S address. Writing it into
#: .env as a deposit account would point every future swap on that chain at a stranger's
#: address, and the deposits would be real, confirmed and unrecoverable. That is the single
#: most expensive mistake available in this file, which is why the table is an allowlist
#: rather than a loop over every asset.
#:
#: AND NOT ICP, which looks tag-attributed and is not: its subaccounts are derived per swap
#: from ICP_OWNER_PRINCIPAL plus an index, so there is no shared-account variable to set --
#: the principal, already discovered above, is what plays that role.
SHARED_DEPOSIT_ACCOUNTS = {"SOL": "SOL_DEPOSIT_ACCOUNT", "XRP": "XRP_DEPOSIT_ACCOUNT"}

#: Help text for a variable that CAN be discovered but was not, so the question is
#: answerable. Each names the command that would have discovered it, because an operator
#: looking at "ICP_LEDGER_CANISTER_ID >" with no help has no way to answer and a canister
#: id is not guessable -- every fresh replica issues different ones, which is why nothing
#: in this tree hardcodes one.
FALLBACK_HELP = {
    "ICP_LEDGER_CANISTER_ID": (
        "the ledger canister id. `python3 swap_stack.py status` prints it, or "
        "`docker compose exec -T icp-replica dfx canister id icp_ledger_canister`. The replica did "
        "not answer, so it could not be read here. Leave BLANK to leave ICP unconfigured."
    ),
    "ICP_OWNER_PRINCIPAL": (
        "the desk's own principal. `docker compose exec -T icp-replica dfx identity get-principal`. "
        "The replica did not answer. Leave BLANK to leave ICP unconfigured."
    ),
}

#: Variables with no default and no discovery, in the order an operator can answer them.
#: Each carries what it is and how to find it, because "SOL_DEPOSIT_ACCOUNT=" on its own
#: is a question nobody can answer from the prompt alone (rule 14).
ASKED = [
    ("BTC_RPC_USER", "the rpcuser from your bitcoind's conf"),
    ("BTC_RPC_PASS", "its rpcpassword"),
    ("LTC_RPC_USER", "the rpcuser from your litecoind's conf"),
    ("LTC_RPC_PASS", "its rpcpassword"),
    ("GRC_RPC_USER", "the rpcuser from your gridcoinresearch.conf"),
    ("GRC_RPC_PASS", "its rpcpassword"),
    ("SOL_DEPOSIT_ACCOUNT", "the PUBLIC key every SOL swap deposits into (one shared account + memo)"),
    ("SOL_HOT_WALLET", "the PUBLIC key SOL payouts are sent FROM"),
    ("XRP_DEPOSIT_ACCOUNT", "the classic address every XRP swap deposits into (one account + destination tag)"),
]


def say(line: str = "") -> None:
    print(line, flush=True)


def secret_names() -> set[str]:
    """Every variable `.env.example` marks `# SECRET`, read out of the template.

    THE TEMPLATE IS THE AUTHORITY so there is one list (rule 8). A second hardcoded set
    here would agree on the day it was written, and the drift would land on whether a
    password is echoed to a terminal -- which is the one place in this script where being
    wrong is unrecoverable, because scrollback cannot be unseen.
    """
    found: set[str] = set()
    marked = False
    for line in ENV_EXAMPLE.read_text().splitlines():
        stripped = line.strip()
        if stripped.startswith(NO_ECHO_MARKER):
            marked = True
            continue
        match = re.match(r"^#?\s*([A-Z_][A-Z0-9_]*)=", stripped)
        if match:
            if marked:
                found.add(match.group(1))
            marked = False
        elif stripped:
            marked = False
    return found


def existing_assignments() -> dict[str, str]:
    """The UNCOMMENTED assignments already in .env, name -> value.

    Read so that a second run does not re-ask what is already set, and so that nothing
    this script does not own is disturbed. Values are read and never printed.
    """
    live: dict[str, str] = {}
    if not ENV_FILE.exists():
        return live
    for line in ENV_FILE.read_text().splitlines():
        if line.lstrip().startswith("#") or "=" not in line:
            continue
        match = re.match(r"^([A-Z_][A-Z0-9_]*)=(.*)$", line.strip())
        if match:
            live[match.group(1)] = match.group(2)
    return live


def listening_ports() -> set[int]:
    """Every TCP port in LISTEN on this host, from /proc/net/tcp and tcp6.

    /proc AND NOT `ss -ltn`, for the reason swap_stack.py gives for the same read: it
    needs no binary that might not be installed and no root. State 0A is LISTEN; the
    local address is `HEX_IP:HEX_PORT` and only the port is taken, because a daemon bound
    to 127.0.0.1 and one bound to 0.0.0.0 are both reachable from the host.
    """
    found: set[int] = set()
    for name in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            lines = Path(name).read_text().splitlines()[1:]
        except OSError:
            continue
        for line in lines:
            fields = line.split()
            if (len(fields) >= _PROC_NET_MIN_FIELDS and fields[3] == _PROC_NET_LISTEN
                    and ":" in fields[1]):
                found.add(int(fields[1].rsplit(":", 1)[1], 16))
    return found


def discover_ports() -> dict[str, tuple[str, str]]:
    """Which test port each Bitcoin-family chain is actually listening on.

    PROBED RATHER THAN ASKED, and the table comes from network_target.CHAIN_PORTS -- the
    one place that already answers "what do I set to reach this chain?", read by the
    workers' banner, the swap page's pair list and create_swap()'s refusal. This is the
    fourth reader and it spells no port number of its own (rule 8).

    MAINNET PORTS ARE NEVER OFFERED. ChainPorts carries the mainnet port in order to
    CLASSIFY a configured one, not to suggest it, and config.py's header records what
    suggesting it cost: the three defaults used to be mainnet, and
    refresh_wallet_inventory() polled the operator's live staking wallet on a loop. Only
    `test_ports` is considered here.

    MORE THAN ONE LISTENING TEST PORT IS NOT RESOLVED HERE. It means two networks of the
    same chain are up, and picking one would be a guess about which the operator means --
    so both are reported and the value is asked for instead.
    """
    live = listening_ports()
    found: dict[str, tuple[str, str]] = {}
    for asset, ports in CHAIN_PORTS.items():
        candidates = sorted(ports.test_ports & live)
        if len(candidates) == _EXACTLY_ONE:
            # ports.port_variable AND ports.test_hint -- THE FIELD NAMES, READ OFF THE
            # CLASS. The first version of this line said `ports.variable` and
            # `ports.hint`, which are not fields of ChainPorts, and the operator got
            #
            #     AttributeError: 'ChainPorts' object has no attribute 'hint'
            #
            # on the first real run. I had read the TABLE -- four positional arguments
            # per entry -- and inferred the names from their positions instead of
            # reading the NamedTuple that defines them. That is rule 17 exactly: a
            # reason to believe written in the same voice as having checked.
            found[ports.port_variable] = (
                str(candidates[0]),
                f"{asset} answering on {candidates[0]} ({ports.test_hint})",
            )
    return found


def discover_icp() -> dict[str, tuple[str, str]]:
    """The ledger canister id and the desk principal, asked of the running replica.

    INSIDE THE COMPOSE SERVICE, NEVER HOST dfx. HANDOFF.md section 1 records host dfx run
    in icp/ judging the container's root-owned .dfx/local incohesive and attempting to
    DELETE it -- the local replica's state, including the ledger holding the desk's
    balance. Only a permission error stopped it. `docker compose exec` is the correct
    transport and the SERVICE name is `icp-replica`; the CONTAINER name is
    `swap-icp-replica` and exec rejects it.

    A CANISTER ID IS NEVER HARDCODED. Every fresh replica issues different ids, so an id
    in a file is somebody else's -- which is why swap_stack.py asks for them too and says
    so on the line that prints them.

    FAILURE IS NOT AN ERROR HERE. A replica that is not running simply cannot answer, and
    the value is asked for instead. Reported either way, because "could not reach the
    replica" and "the replica has no such canister" send an operator to different places.
    """
    found: dict[str, tuple[str, str]] = {}
    # RESOLVED RATHER THAN TRUSTED TO PATH, which is ruff's S607 answered rather than
    # suppressed: a bare "docker" is whatever PATH finds, and this script runs as the
    # operator with their environment. shutil.which() is also the honest way to report
    # "docker is not installed" as its own answer instead of as a FileNotFoundError from
    # inside a discovery step.
    docker = shutil.which("docker")
    if docker is None:
        say("    docker is not on PATH, so the ICP values cannot be discovered here")
        return found
    for variable, command in ICP_DISCOVERY.items():
        result = subprocess.run(  # noqa: S603 -- fixed argv from this file's own constants, no shell, absolute binary
            [docker, "compose", "exec", "-T", ICP_SERVICE, *command],
            cwd=str(REPO_ROOT), capture_output=True, text=True, check=False, timeout=60,
        )
        value = result.stdout.strip()
        if result.returncode == 0 and value:
            found[variable] = (value, f"asked of the {ICP_SERVICE} service with `{' '.join(command)}`")
        else:
            reason = (result.stderr or result.stdout).strip().splitlines()
            say(f"    could not discover {variable}: {reason[-1][:140] if reason else 'no output'}")
    return found


def ask(variable: str, what: str, *, secret: bool, default: str = "") -> str:
    """One value from the terminal. A secret is read with NO echo and never printed back.

    /dev/tty RATHER THAN stdin, so this keeps working from a pipe and so a redirected
    stdin cannot supply a credential without the prompt being seen. ST_ANSWERS is the
    test path and is the only way a value arrives without a terminal.
    """
    scripted = os.environ.get("ST_ANSWERS")
    if scripted is not None:
        answers = dict(pair.split("=", 1) for pair in scripted.split(";") if "=" in pair)
        return answers.get(variable, default)

    suffix = f" [{default}]" if default and not secret else ""
    prompt = f"  {variable}{suffix}\n    {what}\n    > "
    if secret:
        value = getpass.getpass(f"  {variable} (NOT echoed)\n    {what}\n    > ")
    else:
        with Path("/dev/tty").open() as tty:
            print(prompt, end="", flush=True)
            value = tty.readline().strip()
    return value or default


def write_env(values: dict[str, str]) -> None:
    """Replace or append each name in .env, carrying every other line through unchanged.

    ATOMIC AND MODE 600, through a temp file in the same directory and a rename, with
    umask set BEFORE the file exists -- a chmod afterwards leaves a window in which a
    file holding RPC passwords is world-readable, and on a key-holding system that window
    is the whole exposure.

    NO .bak. CLAUDE.md rule 2: the backup habit is what put a live
    GRIDCOIN_RPC_PASSWORD on GitHub via .env.bak.2026-03-28_185451. There is one file at
    the end of this.

    SINGLE-QUOTED, because that is the only dotenv encoding an arbitrary value survives
    unchanged -- unquoted loses a trailing `#` to a comment, double-quoted takes `$`, `\\`
    and `"` as syntax. A value containing a single quote is refused by the caller for the
    same reason set_grc_passphrase.sh refuses one: there is no escape for it inside single
    quotes, and a credential stored mangled does not fail here, it fails later as an
    authentication error nobody connects to this step.
    """
    keep = []
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text().splitlines():
            match = re.match(r"^([A-Z_][A-Z0-9_]*)=", line.strip())
            if match and match.group(1) in values:
                continue
            keep.append(line)

    previous = os.umask(stat.S_IRWXG | stat.S_IRWXO)
    try:
        # mkstemp + a context manager rather than NamedTemporaryFile(delete=False),
        # which is ruff's SIM115 answered rather than suppressed. mkstemp also creates
        # the file 0600 ITSELF, before anything is written to it -- so there is no
        # instant at which a file about to hold RPC passwords is readable by anyone else,
        # which is the property the umask above is belt to.
        handle, temp_name = tempfile.mkstemp(dir=str(REPO_ROOT), prefix=".env.new.")
        with os.fdopen(handle, "w", encoding="utf-8") as out:
            for line in keep:
                out.write(line + "\n")
            for name in sorted(values):
                out.write(f"{name}='{values[name]}'\n")
        Path(temp_name).replace(ENV_FILE)
        ENV_FILE.chmod(stat.S_IRUSR | stat.S_IWUSR)
    finally:
        os.umask(previous)


def accounts_from_the_database(already: dict) -> dict[str, tuple[str, str]]:
    """The shared SOL and XRP deposit accounts, read off swaps that already ran.

    =========================================================================
    "i don't know any of that fucking information off hand" -- 2026-10-11
    =========================================================================

    They did not need to. Their database holds 52 swaps, and on a tag-attributed chain
    every swap's `deposit_address` IS the configured shared account -- that is what makes
    the chain tag-attributed. So the value they were being asked to remember is recorded,
    by this system, on rows it wrote itself.

    THE DATABASE IS OPENED READ-ONLY, through the mode=ro URI, and that is a guard rather
    than a convention: sqlite refuses a write on that handle, so no bug in this
    configuration tool can alter the authority it is reading. absorb_db.open_both() uses
    the same URI for the same stated reason.

    IT GOES THROUGH config.database_path(), so it honors the precedence every other process
    does -- SWAP_DB_PATH exported, then .env's SWAP_DB_PATH, then .env's SWAP_DB_DIR. A
    second derivation here is what left the host tools pointed at a moved database for an
    hour this morning (rule 11).

    ONLY SOL AND XRP. See SHARED_DEPOSIT_ACCOUNTS: on BTC, LTC and GRC each swap gets its
    OWN derived address, so a `deposit_address` from one of those rows is one customer's
    address, and writing it into .env would point every future swap on that chain at a
    stranger. The deposits would be real, confirmed and unrecoverable.

    MOST RECENT FIRST, because an account can be repointed and the newest row is the one
    the operator most recently used. If older rows disagree the newest still wins, and the
    reason line says how many distinct accounts were seen so a repoint is visible rather
    than silently resolved.

    NEVER RAISES. A missing database, an unreadable one, or one with no swaps on a chain
    are all ordinary states -- the value is simply asked for instead, and the line says
    which of those it was (rule 14).
    """
    found: dict[str, tuple[str, str]] = {}
    try:
        path = Path(database_path())
    except Exception as error:  # noqa: BLE001 -- config derivation; reported and then asked for instead
        say(f"    the database path could not be derived ({type(error).__name__}), so no account was read")
        return found
    if not path.is_file():
        say(f"    no database at {path}, so no deposit account could be read from one")
        return found

    try:
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.Error as error:
        say(f"    {path} could not be opened read-only ({error}), so no account was read")
        return found
    try:
        for asset, variable in sorted(SHARED_DEPOSIT_ACCOUNTS.items()):
            if already.get(variable):
                continue
            try:
                rows = connection.execute(
                    "SELECT deposit_address, COUNT(*) AS swaps FROM swaps WHERE from_asset = ?"
                    " AND deposit_address IS NOT NULL AND deposit_address != ''"
                    " GROUP BY deposit_address ORDER BY MAX(created_at) DESC",
                    (asset,),
                ).fetchall()
            except sqlite3.Error as error:
                # A database created before the `swaps` table existed, or one never
                # initialized. Named rather than broad, and reported rather than raised.
                say(f"    {asset}: {path} could not be queried ({error})")
                continue
            if not rows:
                say(f"    {asset}: no swap in {path.name} carries a deposit address, so there is "
                    f"nothing to read -- the account has to be given")
                continue
            address, swaps = rows[0][0], rows[0][1]
            seen = (f"; {len(rows)} DISTINCT accounts appear on {asset} swaps, so this one has been "
                    f"repointed -- the newest is taken and the others are history"
                    if len(rows) > 1 else "")
            found[variable] = (
                str(address),
                f"the deposit address on {asset}'s {swaps} most recent swap(s) in {path.name}, which "
                f"IS the shared account for a tag-attributed chain{seen}",
            )
    finally:
        connection.close()
    return found


def credentials_from_confs(discovered: dict, already: dict, *, gridcoin: bool) -> dict[str, tuple[str, str]]:
    """rpcuser and rpcpassword out of each daemon's own conf. NO VALUE IS EVER PRINTED.

    =========================================================================
    "ji don't know any of those passwords" -- the operator, 2026-10-11
    =========================================================================

    They were right not to know them. The credentials are already written down, once, in
    each daemon's own conf, and asking a human to copy a password out of a file and type
    it into another one is a step that gets skipped or got wrong. Measured on their host:
    `~/.litecoin/litecoin.conf` and `~/.GridcoinResearch/gridcoinresearch.conf` both carry
    rpcuser and rpcpassword; `~/.bitcoin/bitcoin.conf` does not exist and bitcoind is not
    running, so BTC has nothing to read and stays unconfigured.

    I DECLINED TO BUILD THIS AT FIRST, citing chain-safety's "never move, copy, or read
    back a credential", and that refusal is what left them unable to proceed. The rule is
    about ME: Claude must not pull a credential into a conversation. A tool that runs on
    the operator's machine, reads their conf, writes it into their .env and never prints
    it is a different thing -- I never see it, they never paste it, and it never leaves
    the host. It is OPT-IN so that the move is theirs.

    =========================================================================
    IT USES THE PARSER THAT ALREADY EXISTS
    =========================================================================

    chains/daemon_conf.parse_daemon_conf() handles the two things a hand-rolled reader
    gets wrong, and both are written down there against measurements: credentials sit at
    the TOP level while the port sits in a `[network]` section, and a `#` only starts a
    comment at the START of a line -- "an rpcpassword containing `#` is both legal and
    likely from a generator. Stripping inline would silently truncate it, and the failure
    would arrive as a 401 from the daemon with nothing pointing here."

    NOT rpc_settings_from_conf(), WHICH IS THE NEARBY FUNCTION AND IS TOO STRICT: it
    demands rpcuser, rpcpassword AND rpcport together and raises if any is missing. The
    port is already established by probing here, and a conf that omits rpcport because the
    daemon uses the default is an ordinary conf -- refusing it would lose the credentials
    over a value this tool does not need.

    THE SECTION COMES FROM THE PROBED PORT. 19332 is testnet, 19443 is regtest, and
    chains/daemon_conf.CONF_FALLBACK_NETWORK hardcodes regtest for LTC -- which would read
    the wrong section of the operator's real conf, since their litecoind answers on 19332.

    RETURNS {variable: (value, reason)} with the reason naming the FILE and never the
    value, which is the same split chains/daemon_conf.describe() makes and for the same
    stated reason: "a second formatter is a second chance to interpolate the wrong key".
    """
    found: dict[str, tuple[str, str]] = {}
    for chain, (conf_path, user_variable, pass_variable) in sorted(DAEMON_CONFS.items()):
        if chain == "GRC" and not gridcoin:
            say(f"    {chain}: NOT READ. {GRIDCOIN_CONF_HAZARD}")
            continue
        if already.get(user_variable) and already.get(pass_variable):
            continue
        path = Path(conf_path).expanduser()
        if not path.is_file():
            say(f"    {chain}: no conf at {path}, so there is nothing to read")
            continue

        port = _probed_port(chain, discovered, already)
        section = CONF_SECTION_FOR_PORT.get(port, "")
        try:
            settings = parse_daemon_conf(path.read_text(), network=section)
        except OSError as error:
            # NAMED, not broad: an unreadable conf is a permissions answer, not a crash.
            # A conf owned by another user is an ordinary state on a shared host.
            say(f"    {chain}: {path} could not be read ({type(error).__name__}), so nothing was taken")
            continue

        missing = [key for key in ("rpcuser", "rpcpassword") if not settings.get(key)]
        if missing:
            say(f"    {chain}: {path} has no {', '.join(missing)}. If it uses rpcauth= instead, that is a "
                f"HASH and cannot be used as a password -- set rpcuser/rpcpassword, or leave the chain off")
            continue

        found[user_variable] = (settings["rpcuser"], f"rpcuser read from {path}")
        # THE VALUE IS IN THE TUPLE BECAUSE IT HAS TO BE WRITTEN. It is not in the
        # REASON, and say_dry_run()/say_what_was_written() print names and reasons only.
        found[pass_variable] = (
            settings["rpcpassword"],
            f"rpcpassword read from {path} and NOT shown"
            + (f" (top level plus [{section}])" if section else ""),
        )
    return found


def _probed_port(chain: str, discovered: dict, already: dict) -> int:
    """The port this run established for `chain`, or 0. Decides which conf section to read.

    0 RATHER THAN A GUESS when nothing established one: CONF_SECTION_FOR_PORT then misses,
    `section` is empty, and parse_daemon_conf() reads the top level only -- which is where
    credentials conventionally live, so the common case still works and no section is read
    on a guess about the network.
    """
    variable = CHAIN_PORTS[chain].port_variable if chain in CHAIN_PORTS else ""
    raw = str(discovered.get(variable, ("",))[0] or already.get(variable, "") or "").strip("'\"")
    return int(raw) if raw.isdigit() else 0


def asked_for(discovered: dict, already: dict) -> list[tuple[str, str]]:
    """ASKED, plus any RPC port that discovery missed. Nothing falls between the two.

    =========================================================================
    A VARIABLE IN NEITHER LIST WAS SILENTLY ABSENT, FOUND ON THE OPERATOR'S FIRST
    GOOD RUN
    =========================================================================

    COMPUTED AS A COMPLEMENT, NOT AS A SECOND LIST, and that is the whole shape of the
    fix. "Everything this tool can set, minus what was discovered, minus what is already
    in .env" has no gap by construction -- where a hand-maintained second list would
    close the three ports and leave the next discovery-only variable in the same hole.
    I wrote it as that second list first and the ICP values immediately fell through it:
    they are discovered from the replica, so a replica that does not answer left them
    reported-as-unset and never asked for.

    The three Bitcoin-family ports were discovery-only: probed against
    network_target.CHAIN_PORTS, and not in ASKED because probing was expected to answer.
    When it did not, the variable appeared in no list at all. Their run printed

        WOULD WRITE      7 discovered value(s) and 9 asked value(s)

    with BTC_RPC_PORT in neither -- nothing was listening on 18443 or 18332, so their
    bitcoind was down or on another port. BTC would have stayed unset, the container
    would have built no BTC adapter, and the ATM page would have shown BTC as NONE after
    a configuration run that reported success. That is the SAME DEFECT THIS WHOLE TOOL
    EXISTS FOR -- a variable nothing sets and nothing mentions -- reproduced inside the
    fix for it, and it is rule 14's "treat 'skipped' plus 'success' in the same output as
    a defect in the output".

    SO DISCOVERY IS A SHORTCUT, NOT A GATE. A probed port saves the operator a question;
    a probe that finds nothing turns back into the question. The hint comes from
    ChainPorts.test_hint, so the prompt names the ports that chain actually uses rather
    than asking for a number with no help -- and that hint is the same string the
    workers' banner and create_swap()'s refusal print, which is why it lives in that
    table (rule 8).

    ORDER IS ASKED-FIRST, THEN THE MISSED PORTS, so a second run looks the same as the
    first for everything that did not change.
    """
    asked = {name for name, _what in ASKED}
    missed = []
    for variable in every_variable_this_tool_can_set():
        if variable in asked or variable in discovered or already.get(variable):
            continue
        missed.append((variable, FALLBACK_HELP.get(variable, _port_help(variable))))
    return [*ASKED, *missed]


def _port_help(variable: str) -> str:
    """Help text for an RPC port whose probe found nothing, from CHAIN_PORTS' own hint.

    THE HINT IS THE SAME STRING the workers' banner and create_swap()'s refusal print,
    which is why it lives in that table rather than here (rule 8) -- and why its own
    comment records being wrong once: it named only 25779 while the operator's Gridcoin
    test daemon listens on 25715, on the one line whose job is to say what to set.

    FALLS BACK TO A BARE SENTENCE for a variable that is not a port at all, so a name
    added to every_variable_this_tool_can_set() without an entry in FALLBACK_HELP gets a
    usable prompt rather than a KeyError. An unhelpful prompt is a defect; a crash in a
    configuration tool is a worse one.
    """
    for asset, ports in CHAIN_PORTS.items():
        if ports.port_variable == variable:
            return (
                f"{asset}'s RPC port -- nothing is listening on {ports.test_hint}, so its daemon "
                f"is down or on another port. Leave BLANK to leave {asset} unconfigured."
            )
    return f"{variable} -- could not be discovered. Leave BLANK to leave it unset."


def say_banner(apply: bool, already: dict) -> None:
    """What this run is about to do, before it does any of it (rule 14).

    THE MODE LINE IS FIRST AND SAYS WHETHER ANYTHING WILL BE WRITTEN, because the
    difference between a discovery run and an applying one is the whole question an
    operator has about a configuration tool, and finding out afterwards is too late.

    THE NAMES IN .env ARE LISTED AND NO VALUE IS READ BACK. An operator needs to know
    what is already set -- otherwise a second run looks like it is about to re-ask for
    everything -- and the values include RPC passwords, which never reach a terminal.
    """
    say("configure_env: chain configuration that survives a new shell and a reboot")
    say(f"  env file        {ENV_FILE}")
    say(f"  mode            {'APPLY -- .env will be written' if apply else 'DISCOVER ONLY -- nothing will be written'}")
    say("  network         every default offered below is TESTNET or DEVNET. A mainnet")
    say("                  value typed over one is your decision and this cannot tell.")
    say("  secrets         read with no echo, never printed, never in argv, never logged.")
    say("")
    say(f"  already in .env {', '.join(sorted(already)) or '(none)'}  <- names only; no value is read back")
    say("")


def discover_all(already: dict) -> dict[str, tuple[str, str]]:
    """Everything the running system can be asked for, with WHY each value was chosen.

    THE REASON TRAVELS WITH THE VALUE because a discovered port is a claim about the
    operator's machine, and `BTC_RPC_PORT 18443` alone gives them no way to check it.
    `18443 because something is listening there` is checkable in one command.

    THE DATABASE IS ONE OF THE SOURCES, which is the point of it being in here rather
    than beside it. `already` is a parameter for exactly one reason: a chain's shared
    deposit account can be repointed, so when .env already carries one, reading a
    DIFFERENT address off an older swap and reporting it as discovered would offer to
    overwrite the live account with a historical one. Every other source here is
    position-independent and is filtered by main()'s `not already.get(name)`; this one
    has to not look. The parameter is what makes that testable with a seeded dict.

    NOT FLAG-GATED, unlike credentials_from_confs(). The hazard that made reading a conf
    opt-in is that a conf holds a password and Gridcoin's is shared with a live staking
    wallet. A deposit address is PUBLIC -- it is printed on the ATM page for a customer
    to send to -- and the handle is opened mode=ro, so there is nothing here to gate.
    """
    say("  DISCOVERING")
    found: dict[str, tuple[str, str]] = {}
    found.update(discover_ports())
    found.update(discover_icp())
    found.update(accounts_from_the_database(already))
    for variable, (value, why) in ENDPOINT_DEFAULTS.items():
        found[variable] = (value, f"the documented {why} endpoint")
    found["ICP_DFX_SERVICE"] = (ICP_SERVICE, "the compose SERVICE name (not the container name)")
    for variable, (value, why) in sorted(found.items()):
        say(f"    {variable:26} {value}")
        say(f"    {'':26} <- {why}")
    if not found:
        say("    (none)  <- nothing could be discovered; every value below will be asked for")
    say("")
    return found


def every_variable_this_tool_can_set() -> list[str]:
    """Every name this tool could put in .env, so "still unset" can be computed honestly.

    DERIVED FROM THE THREE SOURCES rather than listed, because a fourth list would be the
    one that goes stale: ASKED, the RPC ports in CHAIN_PORTS, and the discovery defaults.
    A variable added to any of them is covered here without being added twice (rule 8).
    """
    return [
        *(name for name, _what in ASKED),
        *(ports.port_variable for ports in CHAIN_PORTS.values()),
        *ENDPOINT_DEFAULTS,
        *ICP_DISCOVERY,
        "ICP_DFX_SERVICE",
    ]


def say_dry_run(needed: list, values: dict, secrets: set, already: dict) -> None:
    """What an --apply run would ask for and write. Rule 14: `(none)` is a result.

    A SECRET IS NAMED AND ITS PURPOSE IS NOT, which is the one place this output is
    deliberately less helpful: the purpose strings for the credentials say which daemon
    conf to read them from, and a dry run is the output most likely to be pasted into a
    chat or an issue.
    """
    say("  WOULD ASK FOR")
    for name, what in needed:
        say(f"    {name:26} {'SECRET -- not echoed' if name in secrets else what}")
    if not needed:
        say("    (none)  <- everything not discovered is already in .env")
    say("")
    say(f"  WOULD WRITE      {len(values)} discovered value(s) and {len(needed)} asked value(s)")
    say("  Nothing was written. Re-run with --apply to be prompted and have .env updated.")
    say("")
    say("  AFTERWARDS, STILL UNSET  <- the chains these belong to stay unconfigured, and the")
    say("                           ATM page will show them as NONE. That is a result, not a")
    say("                           failure of this run (rule 14).")
    will_be_set = set(values) | {name for name, _what in needed} | set(already)
    still_unset = sorted(set(every_variable_this_tool_can_set()) - will_be_set)
    for name in still_unset:
        say(f"    {name}")
    if not still_unset:
        say("    (none)  <- every variable this tool can set is accounted for")


def collect_answers(needed: list, secrets: set) -> dict[str, str]:
    """Ask for each value that could not be discovered. Skipping one is allowed and said.

    A SKIPPED VALUE IS NOT AN ERROR. An operator configuring three chains out of six
    should not have to invent a Solana account, and the consequence is stated per
    variable rather than left to be discovered on the ATM page: the chain that needs it
    stays unconfigured, which is exactly what the lamps already report.

    A SINGLE QUOTE IS REFUSED for the reason set_grc_passphrase.sh refuses one: a
    single-quoted dotenv value cannot carry one in any dialect, and a credential stored
    mangled does not fail here -- it fails later as an authentication error nobody
    connects to this step.
    """
    say("  ASKING FOR WHAT COULD NOT BE DISCOVERED")
    if not needed:
        say("    (none)")
    answers: dict[str, str] = {}
    for name, what in needed:
        value = ask(name, what, secret=name in secrets)
        if not value:
            say(f"    skipped {name} (left unset; the chain that needs it stays unconfigured)")
            continue
        if "'" in value:
            say(f"    REFUSED {name}: it contains a single quote, which a single-quoted .env value")
            say("            cannot carry in any dotenv dialect. Nothing was written for it -- stored")
            say("            mangled it would fail later as an authentication error nobody connects")
            say("            to this step. Say so and the mechanism changes.")
            continue
        answers[name] = value
    return answers


def say_what_was_written(values: dict) -> None:
    """The result, and the step that is REQUIRED rather than optional after it.

    AN ALREADY-RUNNING CONTAINER KEEPS THE ENVIRONMENT IT STARTED WITH, so writing .env
    changes nothing a serving process can see until the container is recreated. Omitting
    that line would leave an operator looking at six NONE lamps on a correctly
    configured machine -- which is the exact state this tool was written for.
    """
    say("")
    say(f"  WROTE            {len(values)} variable(s) into {ENV_FILE}, mode 600")
    say(f"                   {', '.join(sorted(values))}")
    say("                   every other line carried through unchanged; no .bak was made")
    say("  persists         across new shells and reboots, which is the whole point")
    say("")
    say("  NOT YET PROVEN   writing .env says nothing about what the CONTAINER received. An")
    say("                   already-running container keeps the environment it started with,")
    say("                   so the next step is REQUIRED rather than optional:")
    say("                       docker compose up -d --force-recreate web")
    say("                   then open /admin/wallets and read the lamps, or:")
    say("                       docker compose exec -T web printenv | grep -c '^BTC_RPC_PORT='")


def main() -> int:
    """Discover, ask, write. ORCHESTRATION ONLY -- every decision is in a function above.

    ruff's C901 took this to 12 when it held the banner, the discovery report, the dry
    run and the ask loop inline, and rule 12 reads that as orchestration having swallowed
    decisions: "the fix is to extract the decision so it can be called with seeded inputs,
    not to raise the ceiling." Each extracted piece is now callable with a seeded dict,
    which is what lets the output be asserted on without a terminal.
    """
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--apply", action="store_true",
                        help="WRITE .env. Without it this discovers and reports and writes nothing.")
    parser.add_argument(
        "--read-daemon-confs", action="store_true",
        help=("Read rpcuser and rpcpassword out of each daemon's OWN conf instead of asking for "
              "them. No value is printed: the reason line names the FILE and says the password was "
              "read and not shown. Gridcoin needs --read-gridcoin-conf as well; see its hazard."),
    )
    parser.add_argument(
        "--read-gridcoin-conf", action="store_true",
        help=("Also read Gridcoin's credentials. SEPARATE because the hazard differs in kind: "
              "~/.GridcoinResearch/gridcoinresearch.conf is shared by mainnet and testnet, and a "
              "Gridcoin mainnet wallet is typically a live staking wallet. Requires "
              "--read-daemon-confs."),
    )
    args = parser.parse_args()

    secrets = secret_names()
    already = existing_assignments()
    say_banner(args.apply, already)

    discovered = discover_all(already)

    # FROM THE CONFS, AND ONLY WHEN ASKED. Merged into `discovered` so credentials flow
    # through the same dry-run report, the same "still unset" computation and the same
    # write -- a second path for them would be a second chance to print one.
    if args.read_daemon_confs:
        say("  READING CREDENTIALS FROM EACH DAEMON'S OWN CONF")
        from_confs = credentials_from_confs(discovered, already, gridcoin=args.read_gridcoin_conf)
        for variable, (_value, why) in sorted(from_confs.items()):
            say(f"    {variable:26} <- {why}")
        if not from_confs:
            say("    (none)  <- nothing was read; every line above says why")
        discovered.update(from_confs)
        say("")
    elif args.read_gridcoin_conf:
        say("  --read-gridcoin-conf DOES NOTHING ON ITS OWN, so nothing was read. Pass it WITH")
        say("  --read-daemon-confs, or neither.")
        say("")

    values = {name: value for name, (value, _why) in discovered.items() if not already.get(name)}
    # `name not in values` IS THE THIRD CLAUSE AND IT WAS MISSING, which produced output
    # that contradicted itself on the first real run of --read-daemon-confs:
    #
    #     LTC_RPC_USER  <- rpcuser read from ~/.litecoin/litecoin.conf
    #     skipped LTC_RPC_USER (left unset; the chain that needs it stays unconfigured)
    #     WROTE  ... LTC_RPC_USER ...
    #
    # Read from the conf, then asked for anyway, then reported as skipped, then written.
    # Every line was true of the step that printed it and the block as a whole said three
    # incompatible things. That is rule 13's "treat 'skipped' plus 'success' in the same
    # output as a defect in the output" -- and the prompt it produced was worse than the
    # wording: an operator pressing enter past a value that was already found has been
    # asked a question with no right answer.
    needed = [(name, what) for name, what in asked_for(discovered, already)
              if not already.get(name) and name not in values]

    if not args.apply:
        say_dry_run(needed, values, secrets, already)
        return 0

    values.update(collect_answers(needed, secrets))
    if not values:
        say("")
        say("  NOTHING TO WRITE. .env is unchanged.")
        return 0

    write_env(values)
    say_what_was_written(values)
    return 0


if __name__ == "__main__":
    sys.exit(main())
