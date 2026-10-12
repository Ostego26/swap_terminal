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
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "swap_terminal"))

import chain_daemons  # noqa: E402  -- same sys.path.insert as every root tool

import configure_env  # noqa: E402  -- write_env() is the one .env writer (rule 8)

#: The process name each chain's daemon runs under, and the asset it serves.
#:
#: MATCHED ON THE EXECUTABLE'S BASENAME, not on a pattern over the whole command
#: line. Rule 13 prefers a pid file to a `pgrep -f` pattern, and no pid file exists
#: for a daemon this tree did not start -- so the narrowest available identity is
#: the program name, and the DATADIR in the captured command is what distinguishes
#: the operator's three Gridcoin wallets from each other.
DAEMON_PROGRAMS = {
    "bitcoind": "BTC",
    "litecoind": "LTC",
    "gridcoinresearchd": "GRC",
}

#: Flags whose VALUE is a secret. Their names are reported; their values never are.
_SECRET_FLAG = re.compile(r"^--?(rpcpassword|rpcauth|rpcuser)=", re.IGNORECASE)

#: A `-rpcport=NNNNN` on the command line, which OVERRIDES any conf file. A daemon
#: started this way against a mainnet port must not have that command stored.
_RPCPORT = re.compile(r"^--?rpcport=(\d+)$")


def running_daemons() -> list[tuple[str, int, list[str]]]:
    """[(asset, pid, argv)] for every chain daemon running as this user.

    /proc AND NOT `ps` or `pgrep`, for the reason swap_stack.py gives for the same
    read: a parse of another tool's output is a second thing that can change under
    us, where /proc is the kernel's own answer.

    A PROCESS THIS USER CANNOT READ IS SKIPPED SILENTLY, which is correct rather
    than lazy: another user's daemon is not this terminal's to restart, and an
    unreadable /proc entry is exactly that case.
    """
    found: list[tuple[str, int, list[str]]] = []
    for entry in sorted(Path("/proc").iterdir()):
        if not entry.name.isdigit():
            continue
        try:
            raw = (entry / "cmdline").read_bytes()
        except OSError:
            continue
        argv = [part for part in raw.decode("utf-8", "replace").split("\0") if part]
        if not argv:
            continue
        asset = DAEMON_PROGRAMS.get(Path(argv[0]).name)
        if asset:
            found.append((asset, int(entry.name), argv))
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


def plan() -> list[tuple[str, int, str, str, list[str]]]:
    """[(asset, pid, command, refusal, flag_names)] for every daemon found."""
    rows = []
    for asset, pid, argv in running_daemons():
        try:
            cwd = str(Path(f"/proc/{pid}/cwd").readlink())
        except OSError:
            cwd = "/"
        rows.append((asset, pid, command_for(argv, cwd), mainnet_refusal(asset, argv), redacted_flags(argv)))
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--apply", action="store_true", help="write the captured commands into .env")
    args = parser.parse_args()

    print("capture daemon commands")
    print(f"  .env            {configure_env.ENV_FILE}")
    print("  prints          FLAG NAMES only. A command line can carry -rpcpassword, so no")
    print("                  value from one is ever echoed -- it is written to .env or not at all.")
    print()

    rows = plan()
    if not rows:
        print("  (none)  <- no bitcoind, litecoind or gridcoinresearchd is running as this user.")
        print("            Start the daemon first; its own command line is what this captures.")
        return 0

    already = configure_env.existing_assignments()
    to_write: dict[str, str] = {}
    for asset, pid, command, refusal, flags in rows:
        name = f"{asset}_DAEMON_COMMAND"
        print(f"  {asset}  pid {pid}")
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
