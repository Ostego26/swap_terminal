#!/usr/bin/env python3
"""Capture each running chain daemon's own command line into <ASSET>_DAEMON_COMMAND.

Role: file (operator entry point at the repository root per CLAUDE.md rule 10)
Reads: /proc/<pid>/cmdline and /proc/<pid>/cwd for processes named like a chain
      daemon; .env for what is already set
Writes: with --apply only -- .env, through configure_env.write_env(), which is
      atomic and mode 600
Can move funds: no. It starts nothing, stops nothing, opens no socket and sends
      no RPC. It reads command lines and writes one variable per chain.
Mainnet-safe: yes to run. The value it captures could later be used by
      `swap_stack.py on` to START a daemon, which is why a daemon whose command
      line names a MAINNET rpcport is refused here as well as there.

DRY RUN BY DEFAULT, like every other tool here that writes outside the repo.

=============================================================================
WHY THIS EXISTS, and it is a mistake of mine rather than a missing feature.
=============================================================================

`swap_stack.py on` starts a chain daemon from <ASSET>_DAEMON_COMMAND and
deliberately refuses to GUESS one -- chain_daemons.start_command() carries the
argument, and this session is the evidence: an hour was spent pointed at the wrong
one of the operator's three Gridcoin daemons.

So I told the operator to set it, and wrote the instruction as

    LTC_DAEMON_COMMAND=<the exact command you start litecoind with>

inside a fenced block. They pasted it. bash said `syntax error near unexpected
token `newline''. HANDOFF.md's own rule is "Placeholders NEVER inside a code
block", and that is the second time in one session I handed over something that
went into the shell wrong -- the first was four litecoin.conf lines, pasted at a
prompt as shell variable assignments that did nothing.

THE RUNNING DAEMON ALREADY KNOWS THE ANSWER. Its /proc/<pid>/cmdline IS the
command that started it, exactly, with every datadir and flag the operator chose.
Reading it is a measurement where my instruction was a request for a fact I could
not supply.

=============================================================================
NOTHING FROM A COMMAND LINE IS EVER PRINTED
=============================================================================

A daemon may have been started with `-rpcpassword=` on its command line, which is
visible in /proc to the user who owns it. So the value is WRITTEN to .env -- mode
600, already the place credentials live -- and never echoed. What this prints is
the FLAG NAMES it saw and a count, which is enough for an operator to recognize
the daemon without the tool becoming a way to read a password out of a process
table.

That is CLAUDE.md's rule 14 ("Never print a secret") and its chain-safety line
("never `/proc/<pid>/environ`") applied to the neighbouring surface: this reads
cmdline, not environ, and still refuses to show it.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import textwrap
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "swap_terminal"))

# .env FIRST, BEFORE config, for the reason swap_stack.py spells out at its own call
# site: config.Config is a class body evaluated once at import, so a load afterwards
# changes nothing while looking like it worked. This tool reads Config.RPC to learn
# WHICH PORTS to look at, so without this it would look at none of them -- the same
# inert shape measured on the operator's host 2026-10-12.
import env_file  # noqa: E402  -- same sys.path.insert as every root tool

env_file.load_into_environ()

import chain_daemons  # noqa: E402  -- same sys.path.insert
from chains import daemon_launch  # noqa: E402  -- same sys.path.insert
from config import Config  # noqa: E402  -- MUST come after load_into_environ() above
from microfortnights import format_duration  # noqa: E402  -- rule 6, same sys.path insert

import configure_env  # noqa: E402  -- write_env() is the one .env writer (rule 8)
from swap_terminal import stack_authority  # noqa: E402  -- port -> pid, already argued there

#: A `-rpcport=NNNNN` on the command line, which OVERRIDES any conf file. A daemon
#: started this way against a mainnet port must not have that command stored.
_RPCPORT = re.compile(r"^--?rpcport=(\d+)$")

#: The process name each chain's daemon runs under. KEPT AS A LABEL ONLY, since
#: 2026-10-12 -- it is no longer how a daemon is FOUND. See daemons_on_rpc_ports().
#:
#: THE NAME SCAN WAS THE FIRST DESIGN AND IT FOUND NOTHING ON THE OPERATOR'S HOST.
#: Something was LISTENing on 19332 and this tool printed "(none)", because it
#: matched `Path(argv[0]).name` against this table and the operator runs GUI wallets
#: -- `litecoin-qt`, not `litecoind`. OPEN_FINDINGS finding 2 lists three Gridcoin
#: wallets and two of them are GUI binaries; assuming the daemon spelling was the
#: same class of guess that put this session an hour behind on which GRC port to use.
DAEMON_PROGRAMS = {
    "bitcoind": "BTC",
    "litecoind": "LTC",
    "gridcoinresearchd": "GRC",
}

#: The assets with a local daemon, from the stop side's own table so the two verbs
#: cannot disagree about which chains have one (rule 8).
LOCAL_DAEMON_ASSETS = tuple(sorted(chain_daemons.MAINNET_RPC_PORTS_BY_ASSET))


def daemons_on_rpc_ports(rpc) -> list[tuple[str, int, int, list[str]]]:
    """[(asset, port, pid, argv)] for whatever is LISTENing on each configured RPC port.

    IDENTIFIED BY THE PORT, NOT BY THE PROGRAM NAME, and stack_authority.py's header
    already argued exactly this: "for a SERVER there is something better than both
    [a pid file and a pgrep pattern]: the port. A process holding 127.0.0.1:5102 is
    the thing answering that URL whatever it calls itself, which is the actual
    question an operator has."

    It is the actual question here too. What this tool needs is the command line of
    whatever serves the RPC the terminal talks to -- `litecoin-qt` serves it exactly
    as `litecoind` does, and a name scan found neither on the operator's host because
    it was looking for the wrong spelling.

    REUSES stack_authority's THREE EXISTING PIECES rather than reading /proc/net/tcp
    again: listening_inodes() for port -> inode, pids_owning_inodes() for inode ->
    pid, and proc_net_tcp_tables() for the text of BOTH tables -- tcp AND tcp6,
    which that function exists because of: an IPv6-only listener was invisible to a
    tcp-only read on 2026-10-07.

    A PORT WITH NO LISTENER IS SIMPLY ABSENT from the result, which is the honest
    answer: there is no command to capture for a daemon that is not running, and
    main() says so per asset rather than printing one "(none)" for all of them --
    which is what hid this bug for a round.
    """
    wanted: dict[int, str] = {}
    for asset in LOCAL_DAEMON_ASSETS:
        settings = rpc.get(asset) or {}
        port = settings.get("port")
        if port and port not in chain_daemons.MAINNET_RPC_PORTS:
            wanted[int(port)] = asset

    if not wanted:
        return []

    text, _missing = stack_authority.proc_net_tcp_tables(Path("/proc/net"))
    inodes = stack_authority.listening_inodes(text, set(wanted))
    pids = stack_authority.pids_owning_inodes(set(inodes.values()), Path("/proc"))

    found: list[tuple[str, int, int, list[str]]] = []
    for port, asset in sorted(wanted.items()):
        inode = inodes.get(port)
        if inode is None:
            continue
        pid = pids.get(inode)
        if pid is None:
            # A LISTENER WHOSE OWNER THIS USER CANNOT SEE. Another user's process, or
            # one in a different namespace. Not ours to restart, and reported by its
            # absence rather than guessed at.
            continue
        try:
            raw = (Path("/proc") / str(pid) / "cmdline").read_bytes()
        except OSError:
            continue
        argv = [part for part in raw.decode("utf-8", "replace").split("\0") if part]
        if argv:
            found.append((asset, port, pid, argv))
    return found


def redacted_flags(argv: list[str]) -> list[str]:
    """The flag NAMES in `argv`, with every value dropped. Safe to print.

    THE WHOLE OUTPUT SURFACE OF THIS TOOL. A flag's name tells an operator which
    daemon they are looking at -- `-datadir`, `-testnet`, `-rpcport` -- and its
    value may be a password.
    """
    names = []
    for part in argv[1:]:
        if not part.startswith("-"):
            names.append("<positional>")
            continue
        names.append(part.split("=", 1)[0])
    return names


def mainnet_refusal(asset: str, argv: list[str]) -> str:
    """Why this command line must not be stored, or "".

    AN EXPLICIT -rpcport ON THE COMMAND LINE BEATS ANY CONF FILE, so a daemon
    started with a mainnet one is a mainnet daemon whatever its conf says.
    chain_daemons.refuse_to_touch() already refuses to STOP such a daemon; this
    refuses to store the command that would START it, so the two guards cover both
    verbs rather than one.

    A COMMAND WITH NO -rpcport IS NOT REFUSED HERE, and that is deliberate: the port
    then comes from the conf, which this tool does not read, and refusing every
    daemon whose port it cannot see would refuse all three of the operator's. The
    stop-side guard reads the CONFIGURED port and is the one that holds there.
    """
    for part in argv[1:]:
        match = _RPCPORT.match(part)
        if not match:
            continue
        port = int(match.group(1))
        if port in chain_daemons.MAINNET_RPC_PORTS:
            return (
                f"its command line carries -rpcport={port}, which is "
                f"{chain_daemons.MAINNET_RPC_PORTS[port]}. REFUSED: this will not store a command "
                f"that starts a mainnet wallet"
            )
    return ""


def command_for(argv: list[str], cwd: str) -> str:
    """The command line as one string, absolute where it can be made so.

    `swap_stack.py on` runs it with shlex.split and no shell, so what is stored has
    to be a complete argument list. A RELATIVE argv[0] would resolve against
    whatever directory `on` happens to run in, which is not the directory the daemon
    was started from -- so it is made absolute against the process's own cwd.

    NO QUOTING IS ADDED. configure_env.write_env() single-quotes the whole value,
    which is the only dotenv encoding an arbitrary string survives unchanged, and
    its own docstring records why the other two lose characters.
    """
    program = argv[0]
    if not program.startswith("/"):
        candidate = Path(cwd) / program
        program = str(candidate) if candidate.exists() else program
    return " ".join([program, *argv[1:]])


def plan(rpc) -> list[tuple[str, int, int, str, str, list[str]]]:
    """[(asset, port, pid, command, refusal, flag_names)] for every daemon found."""
    rows = []
    for asset, port, pid, argv in daemons_on_rpc_ports(rpc):
        try:
            cwd = str(Path(f"/proc/{pid}/cwd").readlink())
        except OSError:
            cwd = "/"
        rows.append(
            (asset, port, pid, command_for(argv, cwd), mainnet_refusal(asset, argv), redacted_flags(argv))
        )
    return rows


def report_assets_without_a_listener(found_assets: set[str], rpc) -> None:
    """Say, per asset, why nothing was captured for it. Extracted; it decides nothing.

    PER ASSET, NOT ONE "(none)" FOR ALL OF THEM -- and that single line is what hid the
    real bug for a round. It read as "no daemons are running" when the truth was "this
    was scanning for a program named litecoind while litecoin-qt served 19332". Rule 14:
    make "did nothing" look different from "did work", and echo the parameter that
    decided.

    main() hit ruff C901 at 12 with this inline. Rule 12 says that code means
    orchestration swallowed something; here what it had swallowed was a report.
    """
    for asset in LOCAL_DAEMON_ASSETS:
        if asset in found_assets:
            continue
        port = (rpc.get(asset) or {}).get("port")
        if not port:
            print(f"  {asset}  no configured RPC port, so there is nothing to look for")
        elif port in chain_daemons.MAINNET_RPC_PORTS:
            print(f"  {asset}  configured port {port} is {chain_daemons.MAINNET_RPC_PORTS[port]} -- not looked at")
        else:
            print(f"  {asset}  nothing is LISTENing on its configured port {port}")
        # AND THEN WHAT THE TREE HAS RECORDED, WHICH IS WHERE THIS TOOL DEAD-ENDED.
        #
        # The three lines above are the whole of what this said until 2026-10-12, and on
        # that day they closed a loop the operator was inside: `swap_stack.py on` can
        # start a daemon only from <ASSET>_DAEMON_COMMAND, this tool is what fills that
        # variable, and it can only read a daemon that is already RUNNING. With GRC and
        # LTC both down -- the reason they were running these commands at all -- the
        # capture tool said "nothing is LISTENing" and `on` said "run the capture tool".
        #
        # chains/daemon_launch.propose() is the other source: what this tree has
        # recorded, with where it was recorded and what that record does not establish.
        # It is PRINTED and never run, and it never reaches .env -- --apply still stores
        # only a command read from a live process, because a command nobody watched
        # serve the configured port is not a measurement. See that module's header.
        for line in launch_proposal_lines(asset, port):
            print(line)
    print()


def launch_proposal_lines(asset: str, port: int | None) -> list[str]:
    """The recorded launch line for `asset`, as report lines. [] when there is nothing.

    Its own function, and not inlined above, for the reason
    report_assets_without_a_listener() itself was extracted: that one hit ruff C901 at 12
    with a report inline, and rule 12 says what that code means. This is the half that
    FORMATS; propose() is the half that decides, and neither re-derives the other.

    THE COMMAND IS LABELED `run it yourself` IN THE OUTPUT, not offered as something the
    tool will do. Rule 14's "echo the parameters that decide the answer" plus the
    chain-safety posture for anything that touches a wallet: print it for inspection, and
    the act is the operator's.
    """
    command, why = daemon_launch.propose(asset, port)
    if not command and not why:
        return []
    lines = []
    if command:
        lines.append("        RECORDED START COMMAND -- run it yourself, this tool will not:")
        lines.append(f"          {command}")
    lines.extend(f"          {chunk}" for chunk in textwrap.wrap(why, width=84))
    if command:
        lines.append(
            "          then re-run as `capture_daemon_commands.py --wait --apply`, which stores"
        )
        lines.append(
            "          the command it reads from the RUNNING process -- never this recorded"
        )
        lines.append(
            "          text. --wait is not optional advice: -daemon returns before the RPC port"
        )
        lines.append(
            "          is bound, so a capture in the same command line finds nothing"
        )
    return lines


#: How long `--wait` polls for, when it is given with no number. Chosen from the one
#: measurement available rather than for roundness: chain_daemons.STOP_PROOF_SECONDS is
#: 30.0 and is what `off` allows a daemon to FLUSH AND CLOSE. Coming up is the slower
#: half -- a Gridcoin or Bitcoin-derived daemon loads a wallet and a block index before
#: it binds -- so this is twice that, and the flag takes a number for a host where it is
#: not enough.
DEFAULT_WAIT_SECONDS = 60.0

#: How often to re-scan while waiting. One second, because the scan is two /proc reads
#: and a listener that appeared is the thing being waited for -- a longer interval only
#: adds latency to the answer.
WAIT_POLL_SECONDS = 1.0


def assets_to_wait_for(rpc) -> list[tuple[str, int]]:
    """[(asset, port)] worth polling: configured, local, and NOT a mainnet port.

    THE MAINNET EXCLUSION IS THE DECISION HERE, and it is the same one every other verb
    in this tool makes: a configured 8332, 9332 or 15715 is not looked at, not waited
    for, and not stored. Waiting for one would mean polling until a live staking wallet
    appeared, and then capturing the command that starts it.
    """
    return [
        (asset, port)
        for asset in LOCAL_DAEMON_ASSETS
        for port in [(rpc.get(asset) or {}).get("port")]
        if port and port not in chain_daemons.MAINNET_RPC_PORTS
    ]


def wait_for_listeners(rpc, budget_seconds: float, *, sleep=time.sleep, now=time.monotonic) -> set[str]:
    """Poll until every waited-for asset has a listener, or the budget runs out.

    Returns the assets that turned up. Announces before, counts while it waits, and says
    which it is still waiting for -- rule 14, and specifically its "emit progress on
    anything that can exceed a couple of seconds, with a counter and elapsed time".

    =========================================================================
    WHY THIS EXISTS: THE OPERATOR HIT THE RACE ON THEIR FIRST TRY
    =========================================================================

    2026-10-12. They ran the recorded command this tool had just printed:

        $ gridcoinresearchd -datadir=$HOME/.GridcoinResearch-desk -daemon
        Gridcoin starting
        $ python3 capture_daemon_commands.py --apply && python3 swap_stack.py pairs
          GRC  nothing is LISTENing on its configured port 25779

    and minutes later, `ss -ltn` showed `LISTEN *:25779`. The daemon was fine. `-daemon`
    FORKS AND RETURNS IMMEDIATELY -- it prints "Gridcoin starting" and then loads a
    wallet and a block index before it binds its RPC port -- so a capture run in the same
    command line reads /proc before there is anything to read. The tool then printed the
    recorded start command again, at somebody who had just run it, and told them nothing
    was listening on a port that was about to be.

    THE SCAN IS daemons_on_rpc_ports() AND NOT A BIND ATTEMPT, which matters here.
    chain_daemons.port_is_free() binds 127.0.0.1 to prove absence, and a daemon bound to
    one non-loopback address would read as free. This reads /proc/net/tcp AND tcp6, so it
    is address-agnostic -- which is the whole point on Gridcoin, whose socket is `::` with
    IPV6_V6ONLY off and appears only in tcp6 (stack_authority.proc_net_tcp_tables has
    that measurement from 2026-10-07).

    `sleep` and `now` ARE INJECTED so a test can exercise the timeout and the
    appeared-late path without spending a minute on either (rule 10: the decision is the
    smallest testable piece).
    """
    expected = assets_to_wait_for(rpc)
    if not expected:
        print("  waiting         (none) -- no configured non-mainnet port to wait for")
        return set()

    names = ", ".join(f"{asset}:{port}" for asset, port in expected)
    print(f"  waiting         up to {format_duration(budget_seconds)} for {names}")
    print("                  a Bitcoin-derived or Gridcoin daemon returns from -daemon")
    print("                  immediately and binds its RPC port seconds later, so a capture")
    print("                  run in the same command line reads /proc too early")
    started = now()
    found: set[str] = set()
    while True:
        found = {asset for asset, _port, _pid, _argv in daemons_on_rpc_ports(rpc)}
        waited = now() - started
        missing = [asset for asset, _ in expected if asset not in found]
        if not missing:
            print(f"  LISTENING       all {len(expected)} after {format_duration(waited)}")
            return found
        if waited >= budget_seconds:
            print(f"  GAVE UP         after {format_duration(waited)}; still nothing on "
                  f"{', '.join(missing)}")
            print("                  that is not a verdict about the daemon -- it may still be")
            print("                  loading. Re-run, or check `ss -ltn` yourself.")
            return found
        print(f"  waiting         {len(found)}/{len(expected)} up, {format_duration(waited)} "
              f"elapsed, still want {', '.join(missing)}", flush=True)
        sleep(WAIT_POLL_SECONDS)


def expanded_token(token: str) -> str:
    """One argv token with `$VAR` and `~` resolved. The decision, callable alone.

    SPLIT ON THE FIRST `=` ONLY, because a daemon flag is `-datadir=<path>` and the path
    may itself contain one. The flag NAME is never expanded -- `-datadir` has no `$` in
    it and a token with no `=` is expanded whole, which covers a bare path argument.
    """
    if "=" in token:
        flag, value = token.split("=", 1)
        return f"{flag}={expanded_token(value)}"
    if not token.startswith(("~", "$")):
        return token
    return str(Path(os.path.expandvars(token)).expanduser())


def expanded_for_no_shell(command: str) -> str:
    """`$HOME` and `~` resolved in every token, because what runs this has NO SHELL.

    A BUG CAUGHT BEFORE IT WAS WRITTEN, 2026-10-12. The recorded GRC line is

        gridcoinresearchd -datadir=$HOME/.GridcoinResearch-desk -daemon

    which is how a person types it and how OPEN_FINDINGS recorded it. But
    chain_daemons.start_daemon() runs `<ASSET>_DAEMON_COMMAND` through shlex.split and
    subprocess.run with NO shell -- deliberately, so a stray `;` in a datadir path cannot
    become a second command -- and shlex.split expands nothing. Measured:

        ['gridcoinresearchd', '-datadir=$HOME/.GridcoinResearch-desk', '-daemon']

    So `on` would have started a Gridcoin daemon against a directory literally named
    `$HOME`, created it, and come up with an empty wallet on a path nobody meant. Nothing
    would have crashed; the daemon would have reported itself starting.

    EXPANDED HERE RATHER THAN AT RUN TIME. start_daemon() expanding its own argument
    would be a second place that decides what a command means, and the expansion belongs
    with the act of writing a value into .env -- where the human is, and where the
    expanded path is printed before anything runs. The CAPTURE path needs none of it:
    /proc/<pid>/cmdline holds the argv the kernel actually got, already expanded by
    whatever shell started it.

    NAIVE ON WHITESPACE, and that is sound for exactly this input: every entry in
    chains/daemon_launch.RECORDED is a flag list with no quoted argument, and a recorded
    command carrying a single quote is refused by the caller before it gets here. A
    general shell-quoting round trip would be more code for a case the table cannot hold.
    """
    return " ".join(expanded_token(token) for token in command.split())


def recorded_to_accept(rpc, already: dict) -> tuple[dict[str, str], list[str]]:
    """({NAME: command}, lines) for every chain whose RECORDED line may be accepted.

    =========================================================================
    WHY `swap_stack.py on` DID NOT START THE DAEMON, WHICH IS THE OPERATOR'S
    QUESTION AND A FAIR ONE
    =========================================================================

    2026-10-12: "why wasn't the daemon already up from when we hit the stack on or up
    python command". Because `on` starts a chain daemon ONLY from
    `<ASSET>_DAEMON_COMMAND` (chain_daemons.start_command(), which refuses to guess),
    both were unset, and step 3 therefore printed NOT TOUCHED and went on to the
    containers. It was honest and it was not "on": the operator ran the command that is
    supposed to bring the terminal up, and then started the daemon by hand anyway.

    THE FIX IS NOT TO MAKE `on` RUN THE RECORD. A recorded line is a measurement about
    ONE host, stored in a shared repository; running it automatically on any checkout is
    the guessing that chain_daemons.start_command()'s docstring refuses, and GRC's own
    entry says in its `unestablished` field that where the testnet selection comes from
    was inferred from a port rather than read out of the conf. Automating past that
    sentence is how a desk daemon starts on mainnet.

    SO THE RECORD IS ACCEPTED ONCE, BY AN EXPLICIT ACT, INTO .env. After that
    `<ASSET>_DAEMON_COMMAND` is set and `on`, `off` and `restart` own the daemon
    completely -- which is the end state the operator asked for two days earlier ("have
    a cli to control the entire automatic swap terminal") and is also what restores the
    SYMMETRY chain_daemons._refuse_cannot_start_it_again is about: a daemon `off` may
    stop is one `on` can start again.

    EVERY GUARD IN chains/daemon_launch.propose() STILL APPLIES, because this calls it
    rather than reading RECORDED itself -- mainnet port, mainnet datadir, port mismatch,
    nothing recorded, no configured port. And a name already set in .env is left alone,
    for the reason the capture path leaves it alone: an operator's own value is not
    something a tool overwrites from a table.
    """
    accept: dict[str, str] = {}
    lines: list[str] = []
    for asset in LOCAL_DAEMON_ASSETS:
        port = (rpc.get(asset) or {}).get("port")
        command, why = daemon_launch.propose(asset, port)
        name = f"{asset}_DAEMON_COMMAND"
        if not command:
            lines.append(f"  {asset}  no record to accept")
            lines.extend(f"          {chunk}" for chunk in textwrap.wrap(why, width=84))
            continue
        if already.get(name):
            lines.append(f"  {asset}  {name} is already set; this does not overwrite it")
            continue
        if "'" in command:
            lines.append(f"  {asset}  REFUSED: the recorded command contains a single quote, "
                         f"which .env cannot carry")
            continue
        command = expanded_for_no_shell(command)
        accept[name] = command
        lines.append(f"  {asset}  would set {name} from the record:")
        lines.append(f"          {command}")
        lines.append("          CHECK THE RECORD'S OWN CAVEAT FIRST -- it is printed in full by a "
                     "plain run of this tool")
    return accept, lines


def say_accept_recorded(rpc, *, applying: bool) -> int:
    """Print the acceptance plan and, with `applying`, write it. Returns an exit code.

    DRY RUN WITHOUT --apply, which is this tool's existing contract and not a second
    one: `--accept-recorded` says WHICH source to take the command from, and `--apply`
    says whether anything is written. Two flags because they are two decisions, and
    collapsing them would make the riskier source the one with no dry run.
    """
    print("  source          chains/daemon_launch.RECORDED -- a measurement about THIS host,")
    print("                  with its evidence and its gaps. Not a guess, and not a capture:")
    print("                  a capture is a command read off a process that was observed")
    print("                  serving the configured port. This is weaker, which is why it")
    print("                  takes an explicit flag.")
    print()
    accept, lines = recorded_to_accept(rpc, configure_env.existing_assignments())
    for line in lines:
        print(line)
    print()
    if not accept:
        print("  nothing to accept.")
        return 0
    if not applying:
        print(f"  DRY RUN -- nothing was written. Re-run with --apply to set "
              f"{', '.join(sorted(accept))}.")
        return 0
    configure_env.write_env(accept)
    print(f"  wrote {', '.join(sorted(accept))} into {configure_env.ENV_FILE}")
    print("  `python3 swap_stack.py on` will now START these daemons, and `off` will stop")
    print("  them -- which is the symmetry: a daemon off may stop is one on can start again.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--apply", action="store_true", help="write the captured commands into .env")
    parser.add_argument(
        "--accept-recorded", action="store_true",
        help="take the command from chains/daemon_launch.RECORDED instead of from a running "
             "process, for a chain that has one. This is how `swap_stack.py on` learns to START "
             "a daemon without you having started it first. Still a dry run unless --apply is "
             "also given, and every guard in daemon_launch.propose() applies -- a mainnet port "
             "or a mainnet datadir is refused by name.",
    )
    parser.add_argument(
        "--wait", nargs="?", type=float, const=DEFAULT_WAIT_SECONDS, default=0.0, metavar="SECONDS",
        help=f"poll for the configured ports to come up before scanning, for this many seconds "
             f"(default when the flag is given with no number: {DEFAULT_WAIT_SECONDS:.0f}). A "
             f"daemon started with -daemon forks and binds its RPC port seconds later, so a "
             f"capture in the same command line reads /proc too early. Mainnet ports are never "
             f"waited for.",
    )
    args = parser.parse_args()

    print("capture daemon commands")
    print(f"  .env            {configure_env.ENV_FILE}")
    print("  prints          FLAG NAMES only. A command line can carry -rpcpassword, so no")
    print("                  value from one is ever echoed -- it is written to .env or not at all.")
    print()

    if args.accept_recorded:
        # A DIFFERENT SOURCE, SO A DIFFERENT PATH, and it returns before the scan: there
        # is nothing to capture and no port to wait for. Mixing the two in one report
        # would make "would set GRC_DAEMON_COMMAND" ambiguous between a command read off
        # a live process and one read out of a table, and those carry different weight.
        return say_accept_recorded(Config.RPC, applying=args.apply)
    if args.wait > 0:
        wait_for_listeners(Config.RPC, args.wait)
        print()
    rows = plan(Config.RPC)
    report_assets_without_a_listener({row[0] for row in rows}, Config.RPC)
    if not rows:
        print("  nothing to capture. Start whatever serves the port named above; its own")
        print("  command line is what this reads -- GUI wallet or headless daemon alike.")
        return 0

    already = configure_env.existing_assignments()
    to_write: dict[str, str] = {}
    for asset, port, pid, command, refusal, flags in rows:
        name = f"{asset}_DAEMON_COMMAND"
        print(f"  {asset}  pid {pid} on port {port}  ({Path(command.split()[0]).name})")
        print(f"      flags       {' '.join(flags) or '(none)'}")
        if refusal:
            print(f"      REFUSED     {refusal}")
            continue
        if "'" in command:
            # write_env() single-quotes the value, and its docstring says a value
            # containing a single quote is the caller's to refuse. This is that caller.
            print("      REFUSED     its command line contains a single quote, which .env cannot carry")
            continue
        if already.get(name):
            print(f"      unchanged   {name} is already set; this does not overwrite it")
            continue
        print(f"      would set   {name}  <- {len(command)} characters, not shown")
        to_write[name] = command

    print()
    if not to_write:
        print("  nothing to write.")
        return 0
    if not args.apply:
        print(f"  DRY RUN -- nothing was written. Re-run with --apply to set {', '.join(sorted(to_write))}.")
        return 0
    configure_env.write_env(to_write)
    print(f"  wrote {', '.join(sorted(to_write))} into {configure_env.ENV_FILE}")
    print("  `python3 swap_stack.py on` will now start these daemons. `off` already stops them.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
