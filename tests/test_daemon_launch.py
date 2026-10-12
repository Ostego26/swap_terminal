#!/usr/bin/env python3
"""The recorded launch lines, their guards, and the one thing they must never do.

Role: tests (read-only)
Reads: chains/daemon_launch, chain_daemons.MAINNET_RPC_PORTS, and
       capture_daemon_commands through conftest.root_entry_point(). No process, no
       socket, no .env, no wallet.
Writes: nothing
Can move funds: no
Mainnet-safe: yes

WHAT THE MODULE IS FOR, because it decides what is worth failing on.

`swap_stack.py on` starts a chain daemon only from `<ASSET>_DAEMON_COMMAND`, and
chain_daemons.start_command() refuses to guess it. `capture_daemon_commands.py` fills
that variable by reading /proc/<pid>/cmdline of whatever is LISTENing on the configured
port -- so it needs the daemon already running. On 2026-10-12, with GRC and LTC both
down, that closed a loop on the operator: the capture tool said "nothing is LISTENing"
and `on`/`off` said "run the capture tool".

chains/daemon_launch.py is the second source -- what this tree has RECORDED -- and it is
printed for a person, never run and never stored. So the gates here are about the
boundary rather than about the text:

  THE COMMAND NEVER REACHES .env       a line nobody watched serve the configured port
                                       is not a measurement, and --apply must keep
                                       storing only what it read from a live process
  MAINNET IS REFUSED BY NAME, TWICE    by configured port and by datadir. The operator's
                                       ~/.GridcoinResearch is an unlocked staking wallet
                                       and nothing in this tree has its passphrase
  A RECORD IS ABOUT THE DAEMON IT      three Gridcoin wallets, three datadirs, three
  WAS MEASURED ON                      ports, and a remedy aimed at the wrong one of
                                       them for several rounds on 2026-10-09
  EVERY GUARD IS CALLABLE ALONE        rule 12's stated fix for PLR0911, and the only
                                       way the mainnet branches get exercised without a
                                       mainnet wallet to point at
"""

from __future__ import annotations

import chain_daemons
import pytest
from chains import daemon_launch
from chains.daemon_launch import (
    MAINNET_DATADIR_SEGMENTS,
    RECORDED,
    RecordedLaunch,
    _mainnet_datadir,
    _refuse_mainnet_datadir,
    _refuse_mainnet_port,
    _refuse_no_configured_port,
    _refuse_nothing_recorded,
    _refuse_port_mismatch,
    propose,
)
from conftest import root_entry_point


def test_every_recorded_entry_states_what_it_does_not_establish():
    """Rule 17 as a gate: a reason to believe is not a check, in the same voice.

    A launch command recorded from one observation on one host is a fact about that
    moment, and the things nobody checked about it are the things that will be wrong
    next. GRC's entry names the real one -- there is no `-testnet` on the line, and
    where the testnet selection comes from was inferred from the PORT rather than read
    out of the conf.
    """
    assert RECORDED, "an empty table makes every test here vacuous"
    for asset, record in sorted(RECORDED.items()):
        assert isinstance(record, RecordedLaunch)
        assert record.recorded_at.strip(), f"{asset} has no evidence for its record"
        assert record.unestablished.strip(), (
            f"{asset}'s record claims nothing is unestablished about it, which is never "
            f"true of an observation and is how a hypothesis gets written in the register "
            f"of a measurement"
        )
        assert record.serves_port, (
            f"{asset} must name the port its record was OBSERVED on, so propose() can "
            f"refuse when that is not the port configured now"
        )


def test_no_recorded_command_names_a_mainnet_port_or_a_mainnet_datadir():
    """The table itself, before any guard runs. Belt and braces, and cheap.

    The guards below refuse a bad entry at PROPOSE time; this refuses one at COMMIT
    time, which is where a reviewer can still catch it. A command that starts the
    operator's staking wallet has no business existing in this repository at all.
    """
    for asset, record in sorted(RECORDED.items()):
        assert not _mainnet_datadir(record.command), (
            f"{asset}'s recorded command names a mainnet wallet datadir"
        )
        for port in chain_daemons.MAINNET_RPC_PORTS:
            assert f"-rpcport={port}" not in record.command, (
                f"{asset}'s recorded command carries mainnet -rpcport={port}"
            )
        assert record.serves_port not in chain_daemons.MAINNET_RPC_PORTS, (
            f"{asset}'s record was observed on mainnet port {record.serves_port}"
        )


def test_the_desk_datadir_is_not_mistaken_for_the_staking_one():
    """THE SUBTLETY THAT MAKES THE GUARD WORTH A FUNCTION.

    `~/.GridcoinResearch-desk` begins with `~/.GridcoinResearch`, so a `startswith` or a
    bare `in` would refuse the one command this table actually has -- a guard that
    blocks the correct answer and lets nothing through. The segment comparison is what
    separates them, and this is the test that would fail if anyone simplified it.
    """
    assert _mainnet_datadir("gridcoinresearchd -datadir=$HOME/.GridcoinResearch -daemon")
    assert not _mainnet_datadir("gridcoinresearchd -datadir=$HOME/.GridcoinResearch-desk -daemon")
    assert _mainnet_datadir("gridcoinresearch -datadir=/home/someone/.GridcoinResearch -min")
    assert not _mainnet_datadir("litecoind -testnet -daemon")
    assert ".GridcoinResearch" in MAINNET_DATADIR_SEGMENTS


def test_a_mainnet_configured_port_is_refused_by_name():
    """BY NAME, the way chain_daemons.refuse_to_touch() refuses to stop one.

    Both verbs covered rather than one: nothing may start, stop, or now even SUGGEST
    starting a daemon on 8332, 9332 or 15715.
    """
    for port, label in sorted(chain_daemons.MAINNET_RPC_PORTS.items()):
        refusal = _refuse_mainnet_port("GRC", RECORDED["GRC"], port)
        assert "REFUSED" in refusal
        assert label in refusal, f"the refusal must name {label}, not just the number"
        command, why = propose("GRC", port)
        assert command == "", f"no command may be proposed for mainnet port {port}"
        assert "REFUSED" in why


def test_a_record_about_another_port_is_not_proposed():
    """Three Gridcoin wallets on three ports, and an hour lost to the wrong one.

    25715 is the operator's testnet GUI wallet and 25779 is the desk daemon. Both are
    TEST ports, so the mainnet guard says nothing about either -- this is the guard that
    does, and it is the one that was missing from this session's own reasoning for an
    hour on 2026-10-09.
    """
    command, why = propose("GRC", 25715)

    assert command == ""
    assert "25779" in why and "25715" in why, "the refusal must name both ports"
    assert "DIFFERENT daemon" in why
    assert propose("GRC", RECORDED["GRC"].serves_port)[0], (
        "and the matching port must still be proposed, or the guard refuses everything"
    )


def test_an_unlooked_at_chain_reads_differently_from_one_with_no_line():
    """Two absences, two sentences. Rule 14: `(none)` is a result, and so is which one.

    "nobody has looked" and "looked, and what serves that port is a GUI wallet nobody
    recorded a command for" are different states with different next steps, and a single
    blank would be ambiguous between them. LTC is the second; BTC is the first.
    """
    unlooked_command, unlooked_why = propose("BTC", 18332)
    assert unlooked_command == ""
    assert "nobody has looked" in unlooked_why

    ltc_command, ltc_why = propose("LTC", 19332)
    assert ltc_command == ""
    assert "NO LAUNCH LINE HAS EVER BEEN RECORDED" in ltc_why
    assert "litecoin-qt" in ltc_why, "the useful half is WHAT serves that port here"


@pytest.mark.parametrize("guard", [
    _refuse_nothing_recorded,
    _refuse_no_configured_port,
    _refuse_mainnet_port,
    _refuse_mainnet_datadir,
    _refuse_port_mismatch,
])
def test_every_guard_is_callable_on_its_own(guard):
    """Rule 10: the decision is the smallest testable piece, and these are it.

    Also rule 12's stated fix for PLR0911 -- "extract the decision so it can be called
    with seeded inputs, not raise the ceiling" -- which only pays off if the pieces
    really are callable, so that is asserted rather than assumed. Each must return "" on
    the configuration it does not object to.
    """
    assert guard("GRC", RECORDED["GRC"], 25779) == "", (
        f"{guard.__name__} objects to the one configuration this table has recorded as "
        f"working, so it would refuse everything"
    )


def test_the_guards_tuple_holds_every_refusal_this_module_defines():
    """A guard defined and left out of the tuple is a guard that never runs.

    Derived from the module's own namespace rather than from a list written here, which
    is the same shape as services/pair_view.REFUSAL_KEYS and for the same reason: a
    hand-written second list of the things is what went stale four times in this tree
    this week.
    """
    defined = {
        name for name, value in vars(daemon_launch).items()
        if name.startswith("_refuse_") and callable(value)
    }
    wired = {guard.__name__ for guard in daemon_launch._PROPOSAL_GUARDS}

    assert defined == wired, (
        f"defined but never run: {sorted(defined - wired)}; wired but not defined here: "
        f"{sorted(wired - defined)}"
    )


def test_the_recorded_command_is_never_what_apply_would_store():
    """THE BOUNDARY, and it is the only thing in this file that protects funds.

    A recorded line is text from a markdown finding and a reading of a port; a CAPTURED
    line is /proc/<pid>/cmdline of a process observed serving the configured port. Only
    the second is a measurement, and only the second may become
    `<ASSET>_DAEMON_COMMAND` -- because `swap_stack.py on` RUNS that value.

    Asserted by reading what the capture tool hands to configure_env.write_env(): its
    plan comes from daemons_on_rpc_ports(), and launch_proposal_lines() is a printer
    whose return value no writer touches. A refactor that passed a proposal into the
    plan would fail here.
    """
    capture = root_entry_point("capture_daemon_commands.py", "capture_for_launch_gate")

    # The printer returns LINES, never a value a writer could consume.
    lines = capture.launch_proposal_lines("GRC", 25779)
    assert lines, "GRC has a recorded command, so there is something to print"
    assert all(isinstance(line, str) for line in lines)
    # JOINED, NOT PER-LINE, and this gate caught its own brittleness. It asserted
    # `any(... in line for line in lines)` and fired on 2026-10-12 when `--wait` was
    # added to the proposal: the sentence was unchanged in substance and the WRAP moved
    # "text" onto the next line. The claim being gated is about what the output SAYS, so
    # the text is what it reads -- a per-line check was asserting the line breaks too.
    joined = " ".join(lines)
    assert "run it yourself, this tool will not" in joined, (
        "the output must say the tool will not run it"
    )
    assert "never this recorded" in joined, (
        "and must say that --apply stores the captured command, not this one"
    )

    # And the plan -- the only thing --apply writes from -- is built from the port scan.
    # An empty rpc mapping means no listener was found, so no plan and nothing to store.
    assert capture.plan({}) == [], (
        "plan() must be empty with nothing listening; if a recorded command ever leaked "
        "into it, `swap_stack.py on` would run text nobody measured"
    )
