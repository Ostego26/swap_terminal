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
import pytest  # noqa: E402  -- after the sys.path insert, like every import in this block
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


# --- `--wait`, which the operator needed on their first try --------------------


def test_waiting_is_skipped_when_no_port_is_worth_polling(capsys):
    """Rule 14: `(none)` is a result. An empty wait must not look like a timeout."""
    found = capture.wait_for_listeners({}, 60.0)

    assert found == set()
    assert "(none)" in capsys.readouterr().out


def test_a_mainnet_port_is_never_waited_for():
    """The same exclusion every other verb in this tool makes.

    Waiting for a mainnet port would mean polling until a live staking wallet appeared
    and then capturing the command that starts it. 15715 is the operator's Gridcoin
    mainnet wallet, and it was LISTENing on their host while this ran.
    """
    rpc = {
        "GRC": {"port": 15715},
        "LTC": {"port": 19332},
        "BTC": {"port": 0},
    }

    assert capture.assets_to_wait_for(rpc) == [("LTC", 19332)]


def test_a_listener_that_appears_LATE_is_found_rather_than_missed(monkeypatch, capsys):
    """THE RACE, REPRODUCED. `-daemon` returns before the RPC port is bound.

    The operator ran the recorded start command and a capture in the same command line;
    the capture read /proc before the daemon had bound and reported "nothing is
    LISTENing" on a port that `ss` showed as `LISTEN *:25779` minutes later.

    Scripted rather than slept through: the scan returns nothing twice and then the
    daemon, which is the shape of what happened, and the injected clock means this costs
    no wall time.
    """
    answers = [[], [], [("GRC", 25779, 4242, ["gridcoinresearchd", "-daemon"])]]
    monkeypatch.setattr(capture, "daemons_on_rpc_ports", lambda rpc: answers.pop(0))
    ticks = iter([0.0, 1.0, 2.0, 3.0, 4.0])

    found = capture.wait_for_listeners(
        {"GRC": {"port": 25779}}, 60.0, sleep=lambda _s: None, now=lambda: next(ticks),
    )
    printed = capsys.readouterr().out

    assert found == {"GRC"}
    assert "LISTENING" in printed
    # THE COUNTER, and this assertion replaced one that could not fail. The first draft
    # read `assert "1/1" not in ... or True`, which is the vacuous-test shape this tree
    # warns about in four places -- `or True` makes the whole expression true whatever
    # the left side says, so it asserted nothing and would have passed forever.
    assert "0/1 up" in printed, "rule 14: a counter, so a reader can see it is progressing"
    assert "still want GRC" in printed, "rule 14: say what it is still waiting for"


def test_giving_up_is_not_reported_as_a_verdict_about_the_daemon(monkeypatch, capsys):
    """Rule 17: a budget that ran out is not a measurement that the daemon is down.

    It may still be loading a block index. The sentence says so and points at `ss`,
    rather than printing a conclusion the poll cannot support.
    """
    monkeypatch.setattr(capture, "daemons_on_rpc_ports", lambda rpc: [])
    ticks = iter([0.0, 1.0, 61.0, 61.0])

    found = capture.wait_for_listeners(
        {"GRC": {"port": 25779}}, 60.0, sleep=lambda _s: None, now=lambda: next(ticks),
    )
    printed = capsys.readouterr().out

    assert found == set()
    assert "GAVE UP" in printed
    assert "not a verdict about the daemon" in printed
    assert "ss -ltn" in printed


def test_the_recorded_proposal_tells_you_to_use_wait():
    """The proposal is what the operator runs next, so it carries the flag they need.

    Without it they hit the race the first time, which is exactly what happened: the
    tool printed a start command and then, in the same breath, told them to re-run a
    capture that could not possibly see the daemon yet.
    """
    lines = capture.launch_proposal_lines("GRC", 25779)

    assert lines
    joined = " ".join(lines)
    assert "--wait" in joined
    assert "-daemon returns before the RPC port" in joined


# --- `--accept-recorded`, which is how `on` learns to START a daemon -----------


def test_the_accepted_command_has_no_shell_variable_left_in_it(monkeypatch):
    """THE BUG CAUGHT BEFORE IT WAS WRITTEN, and it would not have crashed.

    chain_daemons.start_daemon() runs <ASSET>_DAEMON_COMMAND through shlex.split and
    subprocess.run with NO shell, so nothing expands `$HOME`. Measured:

        shlex.split('gridcoinresearchd -datadir=$HOME/.GridcoinResearch-desk -daemon')
        -> ['gridcoinresearchd', '-datadir=$HOME/.GridcoinResearch-desk', '-daemon']

    `swap_stack.py on` would have started a Gridcoin daemon against a directory
    literally named `$HOME`, created it, and come up with an empty wallet on a path
    nobody meant -- reporting itself as started throughout. That is the quietest kind of
    wrong, which is why the gate is on the WRITTEN VALUE and not on the function.
    """
    monkeypatch.setenv("HOME", "/home/someone")
    accept, _lines = capture.recorded_to_accept({"GRC": {"port": 25779}}, {})

    assert accept, "GRC has a record at its recorded port, so there is something to accept"
    for name, command in accept.items():
        assert "$" not in command, f"{name} still carries a shell variable: {command}"
        assert "~" not in command, f"{name} still carries a tilde: {command}"
        assert "/home/someone/" in command, (
            f"{name} should have been expanded against the real HOME: {command}"
        )


@pytest.mark.parametrize("token,expected", [
    ("-datadir=$HOME/.x", "/home/someone/.x"),
    ("-datadir=~/.x", "/home/someone/.x"),
    ("$HOME/.x", "/home/someone/.x"),
    ("~/.x", "/home/someone/.x"),
    ("-daemon", "-daemon"),
    ("/usr/local/bin/gridcoinresearchd", "/usr/local/bin/gridcoinresearchd"),
    ("-rpcport=25779", "-rpcport=25779"),
])
def test_one_token_at_a_time(token, expected, monkeypatch):
    """The decision, called with seeded inputs (rule 10).

    `-rpcport=25779` is in the table because the split is on the FIRST `=` only and a
    value with no `$` or `~` must come back untouched -- a naive expansion that rebuilt
    every token would be indistinguishable from this on the paths that matter and would
    quietly mangle the ones that do not.
    """
    monkeypatch.setenv("HOME", "/home/someone")

    assert expected in capture.expanded_token(token)


def test_a_mainnet_port_is_refused_at_acceptance_too():
    """Every guard in daemon_launch.propose() applies, because this calls it.

    15715 is the operator's Gridcoin MAINNET wallet and it was LISTENing on their host
    the whole time. Accepting a command for it would put a line in .env that
    `swap_stack.py on` then runs on a live staking wallet.
    """
    accept, lines = capture.recorded_to_accept({"GRC": {"port": 15715}}, {})

    assert accept == {}
    assert any("REFUSED" in line and "mainnet" in line.lower() for line in lines), lines


def test_a_value_the_operator_already_set_is_never_overwritten():
    """Their own value beats a table, always.

    env_file.load_into_environ() takes the same posture for the shell -- setdefault, so
    the shell wins -- and this is the same rule one layer out.
    """
    accept, lines = capture.recorded_to_accept(
        {"GRC": {"port": 25779}}, {"GRC_DAEMON_COMMAND": "what the operator typed"},
    )

    assert accept == {}
    assert any("already set" in line for line in lines)


def test_accepting_is_a_dry_run_without_apply(monkeypatch, capsys):
    """Two flags because they are two decisions: WHICH source, and WHETHER to write.

    Collapsing them would give the weaker source -- a table rather than a live process
    -- the one path with no dry run.
    """
    def refuse(*args, **kwargs):
        raise AssertionError("nothing may be written without --apply")

    monkeypatch.setattr(capture.configure_env, "write_env", refuse)
    monkeypatch.setattr(capture.configure_env, "existing_assignments", dict)

    assert capture.say_accept_recorded({"GRC": {"port": 25779}}, applying=False) == 0
    printed = capsys.readouterr().out
    assert "DRY RUN" in printed
    assert "GRC_DAEMON_COMMAND" in printed


def test_applying_writes_through_the_one_env_writer(monkeypatch, capsys):
    """configure_env.write_env() is the only thing in this tree that edits .env (rule 8)."""
    written = {}
    monkeypatch.setattr(capture.configure_env, "write_env", written.update)
    monkeypatch.setattr(capture.configure_env, "existing_assignments", dict)

    assert capture.say_accept_recorded({"GRC": {"port": 25779}}, applying=True) == 0
    assert list(written) == ["GRC_DAEMON_COMMAND"]
    assert "$" not in written["GRC_DAEMON_COMMAND"]
    printed = capsys.readouterr().out
    assert "will now START these daemons" in printed
    assert "off" in printed, "the symmetry is the point and the output says so"


# --- a captured argv replaces a stale stored command ----------------------------


def _row(command: str):
    """One plan() row, in the shape captures_to_write() actually unpacks.

    SIX FIELDS, read off the loop rather than guessed: (asset, port, pid, command,
    refusal, flags). A stub narrower than the thing it stands in for is the pattern this
    suite has paid for repeatedly.
    """
    return ("GRC", 25779, 2825590, command, "", ["-datadir", "-daemon"])


EXPANDED = "/usr/local/bin/gridcoinresearchd -datadir=/home/x/.GridcoinResearch-desk -daemon"
STALE = "gridcoinresearchd -datadir=$HOME/.GridcoinResearch-desk -daemon"


def test_a_stored_command_that_DIFFERS_is_replaced_by_the_running_argv(capsys):
    """THE NO-OP OF A FIX I HAD JUST RECOMMENDED, measured 2026-10-13.

    GRC_DAEMON_COMMAND held `gridcoinresearchd -datadir=$HOME/...` with an UNEXPANDED
    $HOME, so `on` ran it with a literal `$HOME` and the daemon died after the fork every
    time. The remedy is this command -- and it printed

        GRC  pid 2825590 on port 25779  (gridcoinresearchd)
            flags       -datadir -daemon
            unchanged   GRC_DAEMON_COMMAND is already set; this does not overwrite it

        nothing to write.

    with the correct expanded argv in hand, on the same screen. "Do not clobber what the
    operator set" is right for a GUESS and wrong for a MEASUREMENT:
    /proc/<pid>/cmdline is the argv the kernel actually got for the process serving that
    port. The second no-op-of-a-recommended-fix in one day -- configure_env.
    credentials_from_confs() was the first, for the identical reason.
    """
    to_write = capture.captures_to_write([_row(EXPANDED)], {"GRC_DAEMON_COMMAND": STALE})
    printed = capsys.readouterr().out

    assert to_write == {"GRC_DAEMON_COMMAND": EXPANDED}, (
        "the stale value survived, so the recommended fix is still a no-op"
    )
    assert "REPLACING" in printed
    assert "unchanged" not in printed
    # AND NEITHER COMMAND IS PRINTED. A command line can carry -rpcpassword, which is
    # this file's whole printing discipline; the flag NAMES are enough to see what moved.
    assert "$HOME" not in printed
    assert "/home/x/.GridcoinResearch-desk" not in printed


def test_a_stored_command_that_MATCHES_exactly_writes_nothing(capsys):
    """Rewriting a value to itself would make every run report a change (rule 13)."""
    to_write = capture.captures_to_write([_row(EXPANDED)], {"GRC_DAEMON_COMMAND": EXPANDED})
    printed = capsys.readouterr().out

    assert to_write == {}
    assert "unchanged" in printed
    assert "already matches this process's own argv exactly" in printed
    assert "REPLACING" not in printed


def test_an_absent_command_is_still_a_plain_would_set(capsys):
    """The first-capture path is unchanged, and reads differently from a replacement."""
    to_write = capture.captures_to_write([_row(EXPANDED)], {})
    printed = capsys.readouterr().out

    assert to_write == {"GRC_DAEMON_COMMAND": EXPANDED}
    assert "would set" in printed
    assert "REPLACING" not in printed
    assert EXPANDED not in printed, "the command was printed; only its length may be"


def test_a_refusal_still_beats_a_replacement(capsys):
    """Order matters: a row this tool refuses must not be written just because it differs.

    A command line carrying a single quote cannot survive .env's encoding, and a mainnet
    datadir is refused upstream. Neither becomes writable by being different from what is
    stored.
    """
    quoted = "gridcoinresearchd -datadir=/home/x/it's-here -daemon"

    to_write = capture.captures_to_write([_row(quoted)], {"GRC_DAEMON_COMMAND": STALE})
    printed = capsys.readouterr().out

    assert to_write == {}, "a refused command was written because the stored one differed"
    assert "REFUSED" in printed
    assert "REPLACING" not in printed
