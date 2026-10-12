"""Capturing a daemon's own command line, without ever printing one.

Role: test
Reads: capture_daemon_commands.py
Writes: nothing. No .env is written and no process is started.
Can move funds: no
Mainnet-safe: yes -- NO CHAIN, NO NETWORK, NO SPAWN. Every command line here is
       a seeded list, and every "secret" in one is a literal marker this file
       then asserts does NOT appear in the output.

=============================================================================
WHY THE TOOL EXISTS, and it is a mistake of mine rather than a gap.
=============================================================================

`swap_stack.py on` starts a chain daemon from <ASSET>_DAEMON_COMMAND and refuses
to GUESS one, for a reason this session earned: an hour pointed at the wrong one
of the operator's three Gridcoin daemons.

So I asked them to set it, and wrote the instruction as

    LTC_DAEMON_COMMAND=<the exact command you start litecoind with>

inside a fenced block. They pasted it. bash answered `syntax error near
unexpected token `newline''. HANDOFF.md's own rule is "Placeholders NEVER inside
a code block", and it was the second time that session I handed over something
that went into the shell wrong -- the first being four litecoin.conf lines pasted
at a prompt, where they became shell variables and did nothing.

The running daemon's /proc/<pid>/cmdline IS the answer, exactly, with whatever
datadir and flags the operator chose. Reading it is a measurement where my
instruction was a request for a fact I could not supply.

=============================================================================
WHAT THESE TESTS ARE FOR
=============================================================================

The process scan was verified by spawning two real binaries named litecoind and
gridcoinresearchd and watching the tool find both (pids 1040 and 1041). That half
needs a process and is not repeated here.

What IS here is the three pure decisions, seeded (rule 10) -- and the one that
matters most is that nothing from a command line reaches the output. A daemon
started with `-rpcpassword=` on its command line exposes that value in /proc to
its owner, so this tool is one careless print away from being a way to read a
password out of the process table.
"""

from __future__ import annotations

import pathlib
import shlex
import sys

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "swap_terminal"))

import chain_daemons  # noqa: E402
from prose import code_without_prose  # noqa: E402

import capture_daemon_commands as capture  # noqa: E402

#: A testnet litecoind with a credential on its command line -- the realistic shape,
#: and the reason redaction is not optional.
LTC_TESTNET = [
    "/usr/local/bin/litecoind",
    "-testnet",
    "-datadir=/home/mpjones26/.litecoin",
    "-rpcport=19332",
    "-rpcpassword=SUPERSECRETVALUE",
    "-daemon",
]

#: The operator's MAINNET Gridcoin GUI wallet. 15715, and a live staking wallet.
GRC_MAINNET = [
    "/usr/local/bin/gridcoinresearchd",
    "-datadir=/home/mpjones26/.GridcoinResearch",
    "-rpcport=15715",
    "-rpcpassword=MAINNETSECRET",
]

#: The desk daemon, on the non-default port it was deliberately given. This one is
#: what the terminal is supposed to use (OPEN_FINDINGS finding 2 and finding 22).
GRC_DESK = [
    "/usr/local/bin/gridcoinresearchd",
    "-datadir=/home/mpjones26/.GridcoinResearch-desk",
    "-rpcport=25779",
    "-daemon",
]


# --- nothing from a command line is printed -------------------------------------


def test_only_flag_NAMES_are_reported_never_values():
    """THE ASSERTION THAT KEEPS THIS TOOL FROM BEING A PASSWORD READER.

    /proc/<pid>/cmdline is readable by the process owner, so a daemon started with
    `-rpcpassword=` on its command line has that value sitting there. CLAUDE.md:
    "Never print a secret", and its chain-safety line forbids `/proc/<pid>/environ`
    outright -- this reads cmdline, which is the neighbouring surface, and still
    refuses to show it.
    """
    flags = capture.redacted_flags(LTC_TESTNET)
    assert flags == ["-testnet", "-datadir", "-rpcport", "-rpcpassword", "-daemon"]
    assert "SUPERSECRETVALUE" not in str(flags)
    assert "/home/mpjones26/.litecoin" not in str(flags), (
        "a datadir is not a secret, but it is a VALUE, and the rule here is names only -- "
        "one exception is how the next one gets argued for"
    )


def test_the_flag_NAME_rpcpassword_is_itself_fine_to_show():
    """Knowing a password was passed is what tells an operator to stop doing that."""
    assert "-rpcpassword" in capture.redacted_flags(LTC_TESTNET)


def test_a_positional_argument_is_reported_as_a_placeholder_not_echoed():
    assert capture.redacted_flags(["/usr/local/bin/litecoind", "somevalue"]) == ["<positional>"]


def test_the_mainnet_refusal_does_not_echo_the_command_either():
    refusal = capture.mainnet_refusal("GRC", GRC_MAINNET)
    assert refusal
    assert "MAINNETSECRET" not in refusal
    assert "15715" in refusal, "the operator needs to know WHICH port earned the refusal"


# --- the mainnet guard, on the other verb ---------------------------------------


def test_a_mainnet_rpcport_on_the_command_line_is_refused():
    """AN EXPLICIT -rpcport BEATS ANY CONF FILE, so this is a mainnet daemon whatever
    its conf says.

    chain_daemons.refuse_to_touch() already refuses to STOP such a daemon. This
    refuses to store the command that would START one, so the two guards cover both
    verbs rather than one -- which matters because a stored start command outlives
    the session that captured it.
    """
    refusal = capture.mainnet_refusal("GRC", GRC_MAINNET)
    assert "REFUSED" in refusal
    assert "mainnet" in refusal.lower()


def test_every_mainnet_port_the_stop_side_knows_is_refused_here_too():
    """ONE TABLE, BOTH VERBS. A port added to MAINNET_RPC_PORTS must guard start too.

    Asserted over chain_daemons' own table rather than a list here, because two
    spellings of "which ports are mainnet" is rule 8's bug with a delay on it, on the
    guard protecting a live staking wallet.
    """
    for port in chain_daemons.MAINNET_RPC_PORTS:
        argv = ["/usr/local/bin/bitcoind", f"-rpcport={port}"]
        assert capture.mainnet_refusal("BTC", argv), f"port {port} is not refused on the start side"


def test_the_desk_daemon_on_its_own_port_is_NOT_refused():
    """25779 is the port the desk daemon was deliberately given. It is what we want."""
    assert capture.mainnet_refusal("GRC", GRC_DESK) == ""


def test_a_command_with_no_rpcport_is_not_refused_here():
    """The port then comes from the conf, which this tool does not read.

    Refusing every daemon whose port it cannot see would refuse all three of the
    operator's. The STOP-side guard reads the CONFIGURED port and is the one that
    holds in that case -- stated so nobody reads this as a hole.
    """
    assert capture.mainnet_refusal("LTC", ["/usr/local/bin/litecoind", "-testnet"]) == ""


# --- the captured command -------------------------------------------------------


def test_the_command_round_trips_as_a_complete_argument_list():
    """`on` runs it with shlex.split and no shell, so it has to be complete."""
    command = capture.command_for(LTC_TESTNET, "/home/mpjones26")
    assert shlex.split(command) == LTC_TESTNET


def test_a_relative_argv0_is_made_absolute_against_the_process_cwd(tmp_path):
    """A .desktop-style launch resolves against whatever directory `on` runs in.

    That is not the directory the daemon was started from, so a relative program
    name would find nothing -- the same reason assets/swap-terminal.desktop's Exec
    must be absolute.
    """
    program = tmp_path / "litecoind"
    program.write_text("#!/bin/sh\n")
    command = capture.command_for(["litecoind", "-testnet"], str(tmp_path))
    assert command.startswith(str(program))


def test_an_argv0_that_cannot_be_resolved_is_left_alone_rather_than_invented():
    """Guessing a path for a program that is not where the cwd says would be worse."""
    command = capture.command_for(["litecoind", "-testnet"], "/nonexistent")
    assert command == "litecoind -testnet"


# --- the tool's own posture -----------------------------------------------------


def test_it_is_a_DRY_RUN_by_default():
    """It writes into .env, outside the repo, where a mistake is not undone by git.

    Read through code_without_prose() -- the ELEVENTH time that defect class has
    come up in this tree -- because the module's docstring discusses --apply at
    length and a raw scan would match the prose rather than the argument.
    """
    # ast.unparse NORMALIZES STRING QUOTES to single quotes, so a scan for a
    # double-quoted literal finds nothing however the source is written. That is the
    # stripper being a parse rather than a text filter, which is the point of it --
    # and it caught my assertion rather than the code.
    source = code_without_prose(REPO_ROOT / "capture_daemon_commands.py")
    assert "--apply" in source
    assert "args.apply" in source


def test_it_reuses_the_one_env_writer_rather_than_writing_its_own():
    """rule 8: configure_env.write_env() is atomic, mode 600, and already argued.

    Its docstring records why: a chmod after creation leaves a window where a file
    holding RPC passwords is world-readable, and single quoting is the only dotenv
    encoding an arbitrary value survives.
    """
    source = code_without_prose(REPO_ROOT / "capture_daemon_commands.py")
    assert "configure_env.write_env" in source
    assert "open(" not in source, "it opens a file directly instead of going through write_env()"


def test_it_does_not_overwrite_a_command_the_operator_already_set():
    """An operator who wrote their own is the authority on their own host."""
    source = code_without_prose(REPO_ROOT / "capture_daemon_commands.py")
    assert "already.get(name)" in source


def test_the_programs_it_looks_for_cover_every_chain_with_a_local_daemon():
    """One table per side, and they have to agree on which chains HAVE a daemon.

    chain_daemons.MAINNET_RPC_PORTS_BY_ASSET is the stop side's answer; this is the
    start side's. A chain in one and not the other is a daemon that can be stopped
    and never started, or captured and never stopped.
    """
    assert set(capture.DAEMON_PROGRAMS.values()) == set(chain_daemons.MAINNET_RPC_PORTS_BY_ASSET)
