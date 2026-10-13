"""What this tree has RECORDED about how each chain daemon on this host is started.

Role: submodule (a table of measurements plus the guards over it; holds the decision
      "may this recorded line be PROPOSED for this asset", and nothing else)
Reads: its own table, chain_daemons.MAINNET_RPC_PORTS, and the configured port it is
      handed. No process, no socket, no file, no environment.
Writes: nothing. It PROPOSES text for a person to read; it never runs a command and
      never stores one.
Can move funds: no. A Gridcoin or Litecoin daemon holds a wallet, so a WRONG proposal
      could get a person to start the wrong one -- which is why every entry below is
      a measurement with its evidence, and why propose() refuses more than it offers.
Mainnet-safe: yes, and it refuses rather than declining quietly: a configured mainnet
      port, a recorded command whose datadir is a known mainnet wallet, or a record
      about a different port than the one configured each produce a REFUSAL with its
      reason, not silence.

WHY THIS FILE EXISTS: THE CLI DEAD-ENDED EXACTLY WHERE THE OPERATOR WAS STUCK.

`swap_stack.py on` can start a chain daemon only from `<ASSET>_DAEMON_COMMAND`, and
chain_daemons.start_command() refuses to guess that -- correctly, and its own docstring
says why: "the operator runs three Gridcoin daemons across three datadirs on three
ports, one of them a GUI binary, and this session spent an hour aimed at the wrong
one." The variable is filled by `capture_daemon_commands.py`, which reads
/proc/<pid>/cmdline of whatever is LISTENing on the configured port.

So the only way to teach the CLI to start a daemon was to already have it running. With
both daemons down -- which is the entire reason the operator was running these commands
-- `capture_daemon_commands.py` printed

    GRC  nothing is LISTENing on its configured port 25779
    LTC  nothing is LISTENing on its configured port 19332

and stopped there, and `on`/`off` printed "NOT TOUCHED -- <ASSET>_DAEMON_COMMAND is
unset" and pointed at the capture tool. A closed loop, on 2026-10-12, in the CLI the
operator had asked for two days earlier ("have a cli to control the entire automatic
swap terminal").

WHAT IT DOES AND DOES NOT CHANGE. It does NOT widen what the CLI may run: `on` still
starts only `<ASSET>_DAEMON_COMMAND`, still never guesses, and nothing here is executed
or written to .env by this module. What it removes is the dead end in the INFORMATION:
when nothing is listening, the tool now prints the line this tree has recorded for that
chain, with where it was recorded and what that record does not establish, for a person
to read and run themselves. That is the same posture the chain-safety rules already
prescribe for a transaction -- "build, sign and print a raw transaction for the operator
to inspect; broadcasting is theirs."

WHY A TABLE RATHER THAN READING OPEN_FINDINGS.md. The GRC line below has lived in
OPEN_FINDINGS.md since 2026-10-09 and no code has ever been able to use it. Rule 5 is
explicit that `.md` is a mirror and never an authority a decision is read from, and
rule 15 is explicit that a staging file with no importer "is a leak, and it ages
silently." A command that could start a wallet daemon is exactly the kind of fact that
must be data with a test over it, not prose.

EVERY ENTRY IS A MEASUREMENT AND CARRIES WHAT IT DOES NOT ESTABLISH, which is rule 17's
line between a finding and a hypothesis applied to a table: a launch command recorded
from an observation is a fact about one moment on one host, and the things that were
never checked about it are the things that will be wrong next.
"""

from __future__ import annotations

from typing import NamedTuple

import chain_daemons

#: Datadirs that hold a LIVE MAINNET WALLET on this host, so no command naming one may
#: ever be proposed. Matched as a substring of the recorded command, deliberately
#: loosely: `~/.GridcoinResearch`, `$HOME/.GridcoinResearch` and the expanded absolute
#: path must all be caught, and a loose match here can only ever REFUSE.
#:
#: `~/.GridcoinResearch` IS THE OPERATOR'S STAKING WALLET. OPEN_FINDINGS finding 2 lists
#: the three Gridcoin wallets on this host and names it the mainnet GUI; CLAUDE.md's own
#: chain-safety section and chains/daemon_conf.CONF_FALLBACK_NETWORK both single it out
#: for the same reason -- it is unlocked for staking and nothing in this tree has its
#: passphrase. The desk datadir is `~/.GridcoinResearch-desk`, which does NOT match this
#: string as a path component even though it begins with it, which is why the guard
#: compares a path SEGMENT rather than a prefix.
MAINNET_DATADIR_SEGMENTS = (".GridcoinResearch",)


class RecordedLaunch(NamedTuple):
    """One chain's recorded launch line, with its evidence and its gaps.

    `command` is "" when no launch line has ever been observed for this chain on this
    host. That is a real and useful state -- see LTC below, where what serves the
    configured port is a GUI wallet nobody recorded a command for -- and it is NOT the
    same as the chain being absent from this table, which means nobody has looked.

    `serves_port` is the port the command was OBSERVED serving, never the port it is
    assumed to serve. propose() refuses when it does not match the configured port,
    because a record about a different port is a record about a different daemon, and
    that confusion is the one that cost this session an hour.

    `unestablished` is not optional prose. Rule 17: a reason to believe is not a check,
    and the two must never be written in the same voice.
    """

    command: str
    serves_port: int
    recorded_at: str
    unestablished: str


#: THE RECORDED LINES. One entry per chain anybody has looked at; a chain ABSENT from
#: this table has not been looked at, which propose() reports differently from a chain
#: present with an empty `command`.
RECORDED: dict[str, RecordedLaunch] = {
    "GRC": RecordedLaunch(
        command="gridcoinresearchd -datadir=$HOME/.GridcoinResearch-desk -daemon",
        serves_port=25779,
        recorded_at=(
            "OPEN_FINDINGS finding 2, 2026-10-09, where it is named THE DESK among the "
            "operator's three Gridcoin wallets. Corroborated by what that finding "
            "MEASURED with it running: the web container got HTTP 200 from "
            "host.docker.internal:25779 once rpcallowip=172.18.* was added to "
            "$HOME/.GridcoinResearch-desk/testnet/gridcoinresearch.conf -- so this "
            "line, that datadir and that port are one observed configuration rather "
            "than three facts assembled here"
        ),
        unestablished=(
            "WHERE THE TESTNET SELECTION COMES FROM. There is no -testnet on this line, "
            "and the daemon was observed serving 25779, which network_target.CHAIN_PORTS "
            "lists as a GRC TEST port (mainnet is 15715) -- so it was on testnet and the "
            "selection is in $HOME/.GridcoinResearch-desk/gridcoinresearch.conf, which "
            "is the file read BEFORE the net-specific one. That inference is from the "
            "port, not from reading the conf, and the conf has not been read. CHECK IT "
            "BEFORE RUNNING THIS: `grep -i testnet $HOME/.GridcoinResearch-desk/"
            "gridcoinresearch.conf`. If nothing matches, this line starts a MAINNET "
            "daemon on the desk wallet and -testnet has to be added"
        ),
    ),
    "LTC": RecordedLaunch(
        command="",
        # 19332 AND IT STAYS 19332, because this field's contract is "the port the
        # command was OBSERVED serving, never the port it is assumed to serve" -- and
        # 19332 is what was observed, on 2026-10-12, when stopping the daemon there
        # closed litecoin-qt.
        #
        # I SET THIS TO 19443 ON 2026-10-13 AND BACKED IT OUT WITHIN THE HOUR. The desk's
        # litecoind WAS on 19443 on 2026-10-10 per OPEN_FINDINGS finding 2, and I wrote
        # that into this field -- which asserts that a command was observed serving it,
        # for a record whose `command` is "". A three-day-old listener line is not an
        # observation of now, and this field is the one place in the table that is
        # explicitly not for inference. Operator, the same hour: "quit fucking assuming
        # things."
        #
        # The 19443 fact is real and lives where inference is allowed to live: the prose
        # below, CHAIN_PORTS' hint, and network_target.EXTERNAL_WALLET_PORTS.
        serves_port=19332,
        recorded_at=(
            "NO LAUNCH LINE HAS EVER BEEN RECORDED, and what is recorded instead is why. "
            "THE DESK'S OWN litecoind IS ON 19443 (OPEN_FINDINGS finding 2, 2026-10-10: "
            "`LISTEN 172.17.0.1:19443 litecoind`, network `test`, paying tltc1...), and "
            "nobody has captured the command line that starts it. 19332 -- Litecoin's "
            "DEFAULT testnet RPC port -- is held by the operator's EXTERNAL litecoin-qt, "
            "established the hard way on 2026-10-12 when `swap_stack.py off` stopped the "
            "daemon on the then-configured port and closed that window. The lasting fix "
            "is network_target.EXTERNAL_WALLET_PORTS; _refuse_cannot_start_it_again was "
            "the guard added at the time and covers the symmetry half"
        ),
        unestablished=(
            "THE DATADIR OF THE DESK'S OWN litecoind, and that is the ONLY piece missing. "
            "Everything else is established: OPEN_FINDINGS finding 2 records "
            "`LISTEN 172.17.0.1:19443 litecoind pid 2465635` on TEST, paying tltc1... "
            "addresses, with `swapterm chains` exiting 0 on 2026-10-10 -- a separate "
            "datadir with `port=19444` and `rpcport=19443` under [test], chosen precisely "
            "so it leaves litecoin-qt alone. So the launch line is "
            "`litecoind -datadir=<that datadir> -daemon` and only <that datadir> is unknown "
            "here; `capture_daemon_commands.py --wait --apply` reads it off the running "
            "process."
        ),
        # ===================================================================
        # THIS ENTRY USED TO CONCLUDE "REOPENING THE GUI WALLET is what restores the
        # measured configuration" AND THAT WAS THE DEFECT, not a gap.
        # ===================================================================
        #
        # The paragraph above it had already identified the right answer -- "a separate
        # datadir with its own `port=` and `rpcport=` under [test]" -- and then classified
        # it as "a configuration decision ... so it is the operator's" and recommended the
        # GUI wallet instead, because that was the MEASURED configuration. Preferring a
        # measurement over the correct answer it had in hand.
        #
        # It was also already false when written: that separate datadir EXISTED, on 19443,
        # since 2026-10-10, two days before this entry recorded it as a decision nobody
        # had taken. OPEN_FINDINGS had the listener line.
        #
        # The cost ran for a day. Every `off`, `up` and `chains` told the operator to
        # recover litecoin-qt, and LTC_RPC_PORT stayed 19332 -- pointed at a wallet that
        # is a COUNTERPARTY, not infrastructure. Operator, 2026-10-13: "ltc is not the gui
        # wallet that is a 'external' wallet for the swapper to send or receive ltc" and
        # "a goddamn motherfucker cli is supposed to be a part of the stack."
        #
        # network_target.EXTERNAL_WALLET_PORTS is where that distinction now lives, with
        # chain_daemons._refuse_external_wallet refusing to touch one by name and
        # configure_env.discover_ports() refusing to write one into .env.
    ),
}


def _mainnet_datadir(command: str) -> str:
    """The mainnet datadir segment this command names, or "".

    A PATH SEGMENT AND NOT A PREFIX, which is the whole subtlety: the desk datadir
    `~/.GridcoinResearch-desk` starts with the mainnet one's name, so a `startswith`
    or a bare `in` would refuse the one command this table actually has. Matching
    `/.GridcoinResearch` followed by a path boundary or the end of the token is what
    separates them.
    """
    for token in command.split():
        for segment in MAINNET_DATADIR_SEGMENTS:
            for candidate in token.split("="):
                tail = candidate.rsplit("/", 1)[-1]
                if tail == segment:
                    return segment
    return ""


def _refuse_nothing_recorded(asset, record, port) -> str:
    """Nobody has looked at this chain's launch on this host."""
    del port
    if record is None:
        return (
            f"no launch line has been recorded for {asset} on this host, and nobody has "
            f"looked -- chains/daemon_launch.RECORDED has no entry for it"
        )
    return ""


def _refuse_no_configured_port(asset, record, port) -> str:
    """With no port there is nothing to match the record against."""
    if not port:
        return (
            f"{asset} has no configured RPC port, so a recorded launch line could not be "
            f"shown to be about the daemon you mean. {record.recorded_at}"
        )
    return ""


def _refuse_mainnet_port(asset, record, port) -> str:
    """BY NAME, the way chain_daemons.refuse_to_touch() refuses to STOP one.

    Both verbs are covered rather than one: nothing in this tree may start, stop, or
    now even SUGGEST starting a daemon on 8332, 9332 or 15715.
    """
    del record
    if port in chain_daemons.MAINNET_RPC_PORTS:
        return (
            f"REFUSED: {asset}'s configured port {port} is "
            f"{chain_daemons.MAINNET_RPC_PORTS[port]}. This will not propose a command for a "
            f"mainnet wallet, for the reason chain_daemons.refuse_to_touch() will not stop one"
        )
    return ""


def _refuse_mainnet_datadir(asset, record, port) -> str:
    """The recorded line names a live staking wallet's directory.

    NOTHING IN THE TABLE DOES TODAY, and the guard is what keeps a future entry from
    being added without anyone noticing -- which is the only reason a guard over one's
    own data is worth writing.
    """
    del port
    named = _mainnet_datadir(record.command) if record.command else ""
    if named:
        return (
            f"REFUSED: the recorded {asset} command names {named}, which is a live mainnet "
            f"wallet datadir on this host. No command naming one is ever proposed"
        )
    return ""


def _refuse_port_mismatch(asset, record, port) -> str:
    """The record was observed on a different port, so it is about another daemon.

    THE EXACT CONFUSION THAT COST AN HOUR on 2026-10-09: three Gridcoin wallets, three
    datadirs, three ports, and a remedy applied to the wrong one of them for several
    rounds. A record is about the daemon it was measured on and no other.
    """
    if record.serves_port != port:
        return (
            f"the recorded {asset} line was observed serving port {record.serves_port} and "
            f"{asset}_RPC_PORT is {port}, so the record is about a DIFFERENT daemon and is "
            f"not proposed. {record.recorded_at}"
        )
    return ""


#: THE GUARDS, IN ORDER, CHEAPEST FIRST, AS DATA. Each is callable on its own with
#: seeded inputs, which is rule 12's stated fix for PLR0911 ("extract the decision so it
#: can be called with seeded inputs, not raise the ceiling") and rule 10's shape: the
#: decision is the smallest testable piece at the bottom.
#:
#: SAME ARRANGEMENT AS chain_daemons._REFUSAL_GUARDS, deliberately rather than by
#: coincidence (rule 8): that tuple guards whether a daemon may be STOPPED and this one
#: guards whether a line may be PROPOSED, so a reader who has met one meets no second
#: idea. tests/test_daemon_launch.py calls each one directly, which is what the
#: arrangement buys.
_PROPOSAL_GUARDS = (
    _refuse_nothing_recorded,
    _refuse_no_configured_port,
    _refuse_mainnet_port,
    _refuse_mainnet_datadir,
    _refuse_port_mismatch,
)


def propose(asset: str, configured_port: int | None) -> tuple[str, str]:
    """(command, why) for `asset`: the line to PROPOSE, or "" and the reason there is none.

    EVERY REFUSAL IS IN _PROPOSAL_GUARDS and this function only orders them, which is
    why there are two returns rather than seven. The guards' own docstrings carry what
    each refuses and why; see that tuple for the ordering argument.

    `command` IS NEVER RUN BY ANYTHING. It is printed for a person. The only thing in
    this tree that starts a chain daemon is chain_daemons.start_daemon(), from
    `<ASSET>_DAEMON_COMMAND`, and that is unchanged by this module's existence.

    A RECORD WITH NO COMMAND IS NOT A REFUSAL, which is the distinction the last branch
    makes. LTC's entry below has `command=""` because what serves its port here is a GUI
    wallet nobody recorded a line for -- and the recorded_at plus unestablished text IS
    the useful answer in that case, so it is returned as the `why` of an empty offer
    rather than discarded.
    """
    record = RECORDED.get(asset)
    for guard in _PROPOSAL_GUARDS:
        refusal = guard(asset, record, configured_port)
        if refusal:
            return "", refusal
    if not record.command:
        return "", f"{record.recorded_at}. {record.unestablished}"
    return record.command, f"{record.recorded_at}. NOT ESTABLISHED: {record.unestablished}"
