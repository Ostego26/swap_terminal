"""Start and stop the chain daemons this terminal is configured to use.

Role: submodule -> function (the decisions are refuse_to_touch and
      stop_verdict; swap_stack.py orchestrates)
Reads: Config.RPC (host, port, user, password per chain), and
       <ASSET>_DAEMON_COMMAND from the environment
Writes: nothing of its own. It asks a daemon to stop, which makes that daemon
       flush and close its own wallet.
Can move funds: NO, and the distinction matters enough to state precisely. It
       sends exactly one RPC method, `stop`, and runs one operator-supplied
       command. It never calls sendtoaddress, never signs, never unlocks a
       wallet and never reads a key. What it CAN do is make a chain
       unreachable, which halts payouts -- a halt, never a release.
Mainnet-safe: NO, AND IT REFUSES RATHER THAN TRUSTING THAT. See
       MAINNET_RPC_PORTS: a configured port that is a known mainnet RPC port is
       refused by name, because the operator's Gridcoin mainnet wallet is a
       live staking wallet holding real coins and `swap_stack.py down` must
       never be the thing that stops it.

=============================================================================
WHY THIS EXISTS. Operator, twice:
=============================================================================

    "when i fucking type docker up or docker down this should start/stop every
     subprocess or pid or what the fuckever a"                      (2026-10-11)

    "hey what the fuck why doesn't docker compose down stop that motherfucker?
     how many more times....."                                      (2026-10-12)

THE FIRST ANSWER IS THAT `docker compose` CANNOT. It manages containers, and
bitcoind, litecoind and gridcoinresearchd are host processes -- the whole reason
docker-compose.web.yml needs `extra_hosts: host.docker.internal:host-gateway`
is that the daemons are NOT in the container network. Compose stops what compose
started; nothing will change that.

THE SECOND ANSWER IS THAT swap_stack.py SHOULD, AND SAID IT DID NOT WITHOUT EVER
SAYING WHY. Its `down` banner carried:

    never stopped     chain daemons, and a FOREIGN listener on one of our ports.
                      Both are reported below rather than passed over in silence

Reported, not silent, which is better than nothing -- and an exclusion stated as a
fact with no justification behind it, on the one command whose entire purpose is
"nothing of this stack is running when this returns". The daemons are part of the
stack. They are what the terminal talks to.

=============================================================================
HOW IT STOPS ONE, AND WHY NOT A SIGNAL
=============================================================================

The daemon's own `stop` RPC, through chains/base.RPCAdapter.call() -- the same
adapter and the same credentials the terminal already uses, so this module
introduces no new transport and no new secret (rule 8).

NOT SIGTERM, AND NEVER SIGKILL. A Bitcoin-derived daemon asked to `stop` flushes
its wallet and its block index and closes its databases. Killed mid-write it can
leave wallet.dat or the LevelDB index damaged, and a damaged wallet on a chain
holding testnet funds is a day of recovery at best. CLAUDE.md rule 13 asks for a
reaper and for proven absence; it does not ask for a signal, and here the clean
verb is available and is what an operator would type.

AND stack_authority.py STILL REFUSES TO STOP ONE, which is not a contradiction and
is named at both sites because rule 8 requires it. That file finds a process by the
PORT it holds and would stop it with a SIGNAL, and it refuses on two grounds that
both still stand: a port is "strong enough to report and NOT strong enough to kill"
since something else may legitimately hold one, and a signal can damage wallet.dat.
This module does neither -- it addresses the endpoint the terminal is CONFIGURED to
use and asks the daemon to close itself -- so NEVER_STOPPED is untouched and nothing
here is wired into it.

NOT pkill, NOT pgrep -f. Rule 13 is explicit -- "Prefer a pid file to a `pgrep -f`
pattern. A pattern matches what the command line happens to look like today" -- and
here it is worse than that: a name pattern cannot distinguish the operator's THREE
Gridcoin wallets from each other (OPEN_FINDINGS finding 2 names all three), so a
`pkill gridcoinresearch` aimed at the desk daemon would stop the mainnet staking
wallet in the same breath. Addressing a daemon by the RPC endpoint the terminal is
CONFIGURED to use cannot hit a daemon the terminal does not use.

ABSENCE IS PROVEN BY BIND, not by the stop call returning. `stop` returns as soon
as the daemon accepts the request; shutdown takes seconds afterward. swap_stack.py
already owns that proof for its own ports and this reuses the shape: the port is
bindable and nothing is LISTENing on it.

=============================================================================
HOW IT STARTS ONE, AND WHY IT WILL NOT GUESS
=============================================================================

One environment variable per chain, `<ASSET>_DAEMON_COMMAND`, run by the shell.
Unset means `up` REPORTS that the daemon is down and does nothing.

IT WOULD HAVE BEEN EASY TO DERIVE ONE AND IT WOULD HAVE BEEN WRONG. `litecoind
-datadir=... -daemon` looks obvious, and the operator's actual Gridcoin inventory
is three daemons across three datadirs on three ports, one of them a GUI binary
with `-disableupdatecheck`, discovered only after this session spent an hour
pointed at the wrong one. A launch command is a fact about the host, this file
cannot read it, and rule 17 is the whole reason it does not invent it.
"""

from __future__ import annotations

import os
import shlex
import socket
import subprocess
import time
from pathlib import Path
from typing import NamedTuple

# THE RECORDED REMEDIES, read rather than restated (rule 8). This module asks a
# daemon to stop and reports why it could not; what to DO about a 401 or a refused
# connection is chains/daemon_capabilities.py's, and it already serves the same
# sentences to services/admin_view.probe_chain(). No cycle: that module imports only
# dataclasses and .coin_amounts.
from chains.daemon_capabilities import refusal_remedy
from network_target import is_external_wallet

#: RPC ports that belong to a MAINNET chain. A configured port matching one of
#: these is REFUSED by name rather than acted on.
#:
#: NOT A CONVENIENCE CHECK. config.py's own header records what happened without
#: it: "an unset GRC_RPC_PORT fell through to 15715 and
#: payout_service.refresh_wallet_inventory() ... polled the operator's live
#: staking wallet on a loop." That was a READ. This module would be asking that
#: same wallet to shut down, and CLAUDE.md names it directly -- "the operator's
#: mainnet wallet is a live staking wallet holding real coins".
#:
#: Sourced from each chain's own defaults, and the GRC entry is corroborated by
#: the operator's own ufw rule (`15715/tcp DENY Anywhere`) and by
#: chain_balances.py:21 naming 25715 as the testnet one.
MAINNET_RPC_PORTS = {
    8332: "BTC mainnet",
    9332: "LTC mainnet",
    15715: "GRC mainnet",
}

#: The assets that HAVE a local daemon this module can address, each with its own
#: mainnet RPC port. Keyed by asset so swap_stack can iterate the family without
#: spelling a second list (rule 8).
#:
#: SOL AND XRP ARE ABSENT BECAUSE THEY HAVE NO LOCAL PROCESS -- they are remote
#: endpoints, and "stop" is not a thing one can be asked. ICP is absent because its
#: replica is a CONTAINER, already covered by `docker compose stop`; asking dfx to
#: stop from here would be a second mechanism for a job compose already does, and
#: OPEN_FINDINGS finding 19 is open precisely about what survives that container.
MAINNET_RPC_PORTS_BY_ASSET = {
    "BTC": 8332,
    "LTC": 9332,
    "GRC": 15715,
}

#: Hosts a daemon may be on for this module to address it. Loopback only.
#:
#: WHY THE GUARD IS ON THE HOST AND NOT JUST THE PORT: swap_stack.py runs on the
#: operator's machine, where the daemons are local. A configured RPC host that is
#: NOT loopback means the terminal is pointed at a daemon on another machine, and
#: reaching across the network to shut down somebody else's node is not something
#: a local `down` should ever do -- however the ports happen to line up.
LOCAL_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})

#: How long to wait for a daemon to actually be gone after it accepts `stop`.
#:
#: SECONDS, handed to time.monotonic() arithmetic, so rule 6 keeps them seconds;
#: every line that PRINTS one goes through microfortnights.format_duration().
#:
#: 30 is not a round number: a Bitcoin-derived daemon's shutdown is dominated by
#: flushing the block index, which on a synced testnet node is seconds and on a
#: large one is longer. It is a ceiling on PROVING absence, never on the shutdown
#: itself -- exceeding it reports "asked to stop, still listening" and leaves the
#: daemon alone, because the alternative is a signal and this module does not send
#: one.
STOP_PROOF_SECONDS = 30.0
STOP_POLL_SECONDS = 0.5

#: SECONDS TO WAIT FOR A STARTED DAEMON TO BIND, and it is a DIFFERENT question from
#: proving a stop, which is why it is a different number.
#:
#: IT WAS STOP_PROOF_SECONDS UNTIL 2026-10-13, and that constant's own comment three
#: lines up argues against the reuse: 30 is "a ceiling on PROVING absence". Absence is
#: proven the moment a port is free; PRESENCE has to wait out everything a daemon does
#: before it listens, which on a Bitcoin-derived node is loading the wallet AND the
#: block index. Those are not the same duration and nothing said they were -- the
#: constant was simply the one already in the file.
#:
#: WHAT IT COST, measured on the operator's host 2026-10-13: `swap_stack.py restart`
#: stopped their Gridcoin desk daemon (the stop authenticated on the first try with the
#: credentials that had just been fixed), then started it again, then reported
#:
#:     GRC  NO LISTENER  ran (exit 0) and nothing is LISTENing on 25779 after 30.2s
#:
#: and left the chain down. Whether 30s was simply too short for that node is NOT
#: ESTABLISHED and cannot be from here -- see start_sentence() for the two causes that
#: look identical from outside and for the one file that distinguishes them. 120 is
#: chosen to be longer than a plausible testnet index load rather than from a
#: measurement of one, and that is said rather than dressed up: it is a ceiling on
#: waiting, so being generous costs a slow report and being tight costs a chain.
START_PROOF_SECONDS = 120.0


class DaemonRefusal(NamedTuple):
    """Why a configured chain daemon will not be touched. Empty `why` means it will."""

    asset: str
    why: str

    @property
    def refuses(self) -> bool:
        return bool(self.why)


def _refuse_no_settings(asset: str, settings, _unconfigured_why: str) -> str:
    """Nothing configured, so there is no daemon this terminal uses."""
    if settings:
        return ""
    return f"{asset} is not configured in this process, so there is no daemon to stop"


def _refuse_mainnet_port(asset: str, settings, _unconfigured_why: str) -> str:
    """A configured MAINNET RPC port. CHECKED FIRST OF THE REAL GUARDS.

    config.py's header records what the absence of this already cost once: an unset
    GRC_RPC_PORT fell through to 15715 and the inventory poller READ the operator's
    live staking wallet on a loop. This verb would be asking it to shut down.
    """
    del asset
    port = settings.get("port")
    if port not in MAINNET_RPC_PORTS:
        return ""
    return (
        f"its configured RPC port is {port}, which is {MAINNET_RPC_PORTS[port]}. REFUSED BY "
        f"NAME: this command will not ask a mainnet wallet to shut down. If that port is "
        f"deliberate, stop that daemon yourself"
    )


def _refuse_external_wallet(asset: str, settings, _unconfigured_why: str) -> str:
    """A configured port that belongs to a wallet this stack does not own. HANDS OFF.

    THE GUARD THAT WOULD HAVE SAVED THE WALLET, rather than the one that was added
    afterwards. On 2026-10-12 `off` stopped the daemon on LTC_RPC_PORT and closed the
    operator's litecoin-qt. The guard added then was _refuse_cannot_start_it_again, which
    is about SYMMETRY -- never stop what you cannot start -- and it is correct and keeps
    its place. But it treated a symptom: the reason that window was reachable at all is
    that the terminal was CONFIGURED AGAINST AN EXTERNAL WALLET, and it still was a day
    later because nothing in the tree could say so.

    Operator, 2026-10-13: "ltc is not the gui wallet that is a 'external' wallet for the
    swapper to send or receive ltc", and then the same for Gridcoin's 25715. The desk's
    own node is a headless CLI daemon -- litecoind on 19443, gridcoinresearchd on 25779 --
    and an external GUI is a COUNTERPARTY. Stopping a counterparty's wallet is not a
    worse version of stopping your own daemon; it is a different act.

    SAME SHAPE AS _refuse_mainnet_port AND DELIBERATELY SO. Refused by NAME, with what
    the port is and the evidence behind that, because a refusal an operator cannot check
    is one they route around. network_target.EXTERNAL_WALLET_PORTS carries the evidence
    per port and is the single table (rule 8) -- configure_env's port discovery reads the
    same one so it can never WRITE what this then refuses to touch.

    AFTER the mainnet guard, because a port that is both is the more dangerous fact.
    """
    del asset
    why = is_external_wallet(settings.get("port"))
    if not why:
        return ""
    return (
        f"its configured RPC port is {settings.get('port')}, which is NOT this stack's "
        f"daemon -- it is {why}. REFUSED BY NAME: this command will not start or stop a "
        f"wallet the terminal does not own. Point <ASSET>_RPC_PORT at this desk's own "
        f"headless daemon instead; `python3 swap_stack.py chains` then asks the right one."
    )


def _refuse_unconfigured(_asset: str, _settings, unconfigured_why: str) -> str:
    """The CALLER's sentence for a chain with no adapter, never one spelled here.

    The first version spelled `f"{asset}_RPC_PASSWORD is unset"` and this tree reads
    `<ASSET>_RPC_PASS`, so the message named a variable nothing consumes.
    chains/registry.why_unconfigured() owns those names and records the same mistake
    from 2026-09-26.
    """
    return unconfigured_why


def _refuse_no_port(asset: str, settings, _unconfigured_why: str) -> str:
    if settings.get("port"):
        return ""
    return f"{asset} has no configured RPC port, so this process builds no adapter for it"


def _refuse_remote_host(_asset: str, settings, _unconfigured_why: str) -> str:
    """Not loopback. Shutting down a node on another machine is not `off`'s business."""
    host = str(settings.get("host") or "")
    if host in LOCAL_HOSTS:
        return ""
    return (
        f"its configured RPC host is {host!r}, not loopback. A daemon on another machine is "
        f"not this command's to stop"
    )


def _refuse_cannot_start_it_again(asset: str, _settings, _unconfigured_why: str) -> str:
    """NEVER STOP WHAT THIS CANNOT START. Added 2026-10-12, after it closed a GUI wallet.

    WHAT HAPPENED. `off` stops a daemon by the RPC port the terminal is CONFIGURED to
    use and sends it the `stop` verb. It does not care which program holds that port --
    and on the operator's host the thing serving 19332 was `litecoin-qt`, their
    interactive GUI wallet, not a headless `litecoind`. Asked to stop, it closed. Their
    words: "why'd my external litecoin gui wallet just close".

    The same hour, capture_daemon_commands.py printed "(none)" while scanning for a
    program named `litecoind` -- so the evidence that something else served the port
    was on screen and was read past.

    WHY THE PORT-ADDRESSING ARGUMENT DID NOT COVER IT. This module's header argues
    that addressing by the configured endpoint "cannot hit a daemon this terminal does
    not talk to". True, and not the risk: the terminal DOES talk to that wallet. What
    it cannot do is bring it back -- `on` launches from <ASSET>_DAEMON_COMMAND, and
    nothing relaunches a GUI application with the windows and prompts the operator had
    open in it.

    SO THE INVARIANT IS SYMMETRY, AND THIS TREE HAD ALREADY WRITTEN IT DOWN. From
    regtest/operator_panel.DAEMON_CONTROL:

        "regtest":  {"start": True,  "stop": True},
        "operator": {"start": False, "stop": True},
        "foreign":  {"start": False, "stop": False},

    with ("foreign", "stop", False) mapped to _refuse_stop_no_handle(), whose own
    docstring says "A STOP NEEDS A HANDLE, AND THERE IS NONE." That policy existed and
    this module did not consult it -- the third time in one session the answer was
    already in the repository.

    <ASSET>_DAEMON_COMMAND IS THE HANDLE: the operator's own statement that this is a
    process the terminal may launch, which makes it one the terminal may close.
    capture_daemon_commands.py writes it from the running process, so opting in costs
    one command.

    REFUSING TO STOP IS THE SAFE DIRECTION -- it leaves a chain reachable, which is
    what keeps payouts working.
    """
    if start_command(asset):
        return ""
    # THE SENTENCE NAMES BOTH WAYS OUT, AND IT NAMED ONLY ONE UNTIL 2026-10-12. It
    # pointed at `capture_daemon_commands.py --apply`, which reads the command line of
    # whatever is LISTENing on the configured port -- so it needs the daemon already
    # running, and this refusal is printed precisely when it is NOT. With GRC and LTC
    # both down, that was a closed loop on the operator: `on`/`off` pointed at the
    # capture tool and the capture tool said "nothing is LISTENing".
    #
    # Run WITHOUT --apply the same tool now also prints what chains/daemon_launch.py has
    # RECORDED for the chain, for a person to read and run themselves. Nothing about
    # what this module may START has changed: start_daemon() still runs only
    # <ASSET>_DAEMON_COMMAND, and a recorded line never becomes that value.
    return (
        f"{asset}_DAEMON_COMMAND is unset, so this cannot START it again and will not STOP it. On "
        f"2026-10-12 this closed the operator's Litecoin GUI wallet, because the CONFIGURED PORT "
        f"was 19332 -- that wallet is EXTERNAL and the desk's own litecoind is elsewhere; see "
        f"network_target.EXTERNAL_WALLET_PORTS, which refuses one by name now. It was what served "
        f"the configured port. THREE WAYS OUT, cheapest first: `python3 "
        f"capture_daemon_commands.py --accept-recorded --apply` takes the start command this "
        f"tree has RECORDED for this chain, after which `on` starts it and `off` stops it; "
        f"with the daemon ALREADY RUNNING, `python3 capture_daemon_commands.py --wait --apply` "
        f"stores its real command line instead, which is the stronger source; and a plain "
        f"`python3 capture_daemon_commands.py` prints the record in full with what it does "
        f"NOT establish, to read before you accept it"
    )


#: THE GUARDS, IN ORDER, AND THE ORDER IS THE POLICY. A list rather than a chain of
#: returns because refuse_to_touch() hit ruff PLR0911 at seven of them, and rule 12 says
#: that code means a decision to extract rather than a ceiling to raise. Each entry is
#: now callable with seeded inputs on its own (rule 10), and the sequence is data a
#: reader can check against the docstring instead of tracing branches.
#:
#: MAINNET IS SECOND, AFTER ONLY "is anything configured at all", deliberately: it is the
#: one refusal that must not be reachable past another guard changing.
_REFUSAL_GUARDS = (
    _refuse_no_settings,
    _refuse_mainnet_port,
    # AFTER MAINNET, BEFORE EVERYTHING ELSE. A port that is both a mainnet port and an
    # external wallet -- 15715, the operator's Gridcoin mainnet GUI -- should report the
    # mainnet fact, which is the one that names real coins.
    _refuse_external_wallet,
    _refuse_unconfigured,
    _refuse_no_port,
    _refuse_remote_host,
    _refuse_cannot_start_it_again,
)


def refuse_to_touch(asset: str, settings, unconfigured_why: str = "") -> DaemonRefusal:
    """Whether this module may address `asset`'s daemon at all. THE SAFETY DECISION.

    Pure, so a test can seed a mainnet port and assert the refusal without arranging a
    daemon (rule 10). Each guard in _REFUSAL_GUARDS is separately pure for the same
    reason, and that list is the order.

    AN EMPTY `unconfigured_why` MEANS CONFIGURED, which is why_unconfigured()'s own
    contract. It is passed in rather than derived: see _refuse_unconfigured().
    """
    for guard in _REFUSAL_GUARDS:
        why = guard(asset, settings, unconfigured_why)
        if why:
            return DaemonRefusal(asset, why)
    return DaemonRefusal(asset, "")


def port_is_free(port: int, host: str = "127.0.0.1") -> bool:
    """Nothing is LISTENing on `port`. The proof, not the stop call returning.

    SO_REUSEADDR IS NOT SET, DELIBERATELY. With it, a bind can succeed beside a
    socket in TIME_WAIT and report free when a listener is still there -- which is
    the false all-clear this function exists to not give.
    """
    probe = socket.socket()
    try:
        probe.bind((host, port))
    except OSError:
        return False
    finally:
        probe.close()
    return True


def stop_verdict(accepted: bool, detail: str, freed: bool, waited_seconds: float) -> tuple[str, str]:
    """(state, sentence) for one daemon this module asked to stop. Pure.

    FOUR OUTCOMES, AND "asked but still listening" IS ITS OWN. Collapsing it into
    either success or failure is the defect CLAUDE.md rule 13 names in so many
    words -- "Treat 'skipped' plus 'success' in the same output as a defect in the
    output" -- and here the two readings send an operator to opposite places: a
    daemon that accepted `stop` and is still flushing needs more time, where one
    that never accepted it needs its credentials or its endpoint checked.
    """
    if accepted and freed:
        return "stopped", f"accepted stop and its port is free after {waited_seconds:.1f}s"
    if accepted and not freed:
        return (
            "stopping",
            f"ACCEPTED stop and is STILL LISTENING after {waited_seconds:.1f}s. Not killed: a "
            f"Bitcoin-derived daemon mid-flush can damage wallet.dat. Give it longer, then re-run",
        )
    if not accepted and freed:
        return "already_down", f"did not answer, and its port is free -- it was already not running ({detail})"
    return "unreachable", f"could not be asked to stop and its port is still bound: {detail}"


def stop_daemon(asset: str, adapter, port: int, *, budget_seconds: float = STOP_PROOF_SECONDS):
    """Ask one daemon to stop, then PROVE it is gone. Returns (state, sentence, seconds).

    `adapter` is a chains/base.RPCAdapter -- the same object and credentials the
    terminal uses, so there is no second transport here (rule 8). The only method
    sent is `stop`.

    NEVER RAISES. A daemon that cannot be asked is an outcome, not an exception:
    `down` must report every chain and keep going, exactly as
    refresh_wallet_inventory() does.
    """
    started = time.monotonic()
    accepted, detail = True, ""
    try:
        adapter.call("stop")
    except Exception as exc:  # noqa: BLE001 -- checked: EVERY failure to be asked is one outcome ("not accepted"), and it is returned rather than raised. A daemon that is already down refuses the connection, one with wrong credentials 401s, and one mid-shutdown may drop the socket; all three are "it did not accept a stop" and the reason is carried in the return value, which is what rule 12 requires of a broad catch.
        accepted, detail = False, f"{type(exc).__name__}: {exc}"

    while time.monotonic() - started < budget_seconds:
        if port_is_free(port):
            break
        time.sleep(STOP_POLL_SECONDS)
    waited = time.monotonic() - started
    state, sentence = stop_verdict(accepted, detail, port_is_free(port), waited)
    # THE RECORDED REMEDY, AT THE SECOND SITE THE SAME FAILURE APPEARS. Added
    # 2026-10-12 from the operator's own `restart`: step 3 printed
    #
    #     GRC  UNREACHABLE  could not be asked to stop and its port is still bound:
    #                       HTTPError: 401 Client Error: Authorization Required
    #
    # with no remedy, while step 8 -- the container's probe of the same daemon, for the
    # same reason -- printed the full 401 explanation two screens later. One failure,
    # two surfaces, and only one of them had been taught. chains/daemon_capabilities.
    # refusal_remedy()'s own docstring is about exactly this ("a capability map that no
    # report reads is documentation"), so the fix is to read it here too rather than to
    # write a second sentence.
    #
    # ATTACHED HERE AND NOT IN stop_verdict(), which is pure and takes no asset: the
    # four outcomes are a decision about accepted/freed and nothing else, and threading
    # an asset through it to look up prose would put a lookup inside the one function in
    # this module that a test can exercise with four booleans.
    #
    # ONLY WHEN SOMETHING IS RECORDED. refusal_remedy() returns "" for a failure it
    # cannot classify, and appending an empty string would leave a trailing separator --
    # rule 14's dangling-tail defect, which this tree has already shipped once in
    # cmd_chains()'s exit line.
    remedy = refusal_remedy(asset, detail)
    if remedy:
        sentence = f"{sentence}  REMEDY: {remedy}"
    return state, sentence, waited


def start_command(asset: str) -> str:
    """`<ASSET>_DAEMON_COMMAND` from the environment, or "" if unset.

    THE WHOLE START MECHANISM, and it is one variable because the alternative is
    guessing. See this module's header: the operator runs three Gridcoin daemons
    across three datadirs on three ports, one of them a GUI binary, and this
    session spent an hour aimed at the wrong one. A launch command is a fact about
    the host that this file cannot read.
    """
    return os.getenv(f"{asset}_DAEMON_COMMAND", "").strip()


def refuse_to_run(asset: str, command: str) -> str:
    """Why this command must not be executed, or "". CHECKED BEFORE subprocess.run.

    =========================================================================
    A REAL FAILURE MODE, AND *NOT* THE CAUSE OF THE ONE IT WAS WRITTEN FOR
    =========================================================================

    THE MECHANISM IS MEASURED AND THE GUARD IS SOUND. start_daemon() runs
    <ASSET>_DAEMON_COMMAND through shlex.split with NO shell -- deliberately, so a stray
    `;` in a path cannot become a second command -- and shlex.split expands nothing:

        >>> shlex.split("gridcoinresearchd -datadir=$HOME/.GridcoinResearch-desk -daemon")
        ['gridcoinresearchd', '-datadir=$HOME/.GridcoinResearch-desk', '-daemon']

    A daemon handed a `-datadir` that does not exist dies during startup, and with
    `-daemon` it dies AFTER forking -- so the parent has already printed and exited 0,
    and nothing anywhere reports it. That is worth refusing before it runs.

    capture_daemon_commands.expanded_for_no_shell() was written against this on
    2026-10-12 and its docstring names THIS function's caller as the reason it exists. It
    was wired into the CAPTURE path only, so the runner had no defense. That gap is what
    this closes.

    =========================================================================
    AND IT WAS NOT WHAT BROKE GRC ON 2026-10-13. THREE WRONG DIAGNOSES.
    =========================================================================

    `swap_stack.py restart` left the operator's Gridcoin desk daemon down twice, after
    `off` stopped it cleanly and `up` ran its command and found nothing on 25779 -- first
    at 30s, then at 120s. I offered three causes and all three are refuted or untested:

      a datadir lock    SAID TO BE REFUTED BY THE DAEMON'S LOG, AND IT WAS NOT.
                        The log showed a clean sync with no lock error -- from 07:43:19
                        onward, the HAND start. The failed `up` ran at 07:23:21, twenty
                        minutes earlier, and `tail -40` never reached it. Absence of
                        evidence in a window that does not contain the event. STILL THE
                        LEADING CANDIDATE AND STILL UNTESTED: a Bitcoin-derived daemon
                        closes its RPC port EARLY in shutdown and holds the datadir lock
                        while it flushes, so stop_daemon() proving the port free in 0.5s
                        does not mean the process is gone.
      an unexpanded     REFUTED. `capture_daemon_commands.py --wait --apply` reported
      $HOME             "GRC_DAEMON_COMMAND already matches this process's own argv
                        exactly" -- so the stored value is the absolute, fully expanded
                        path and always was. `on` ran a correct command.
      a MAINNET node    REFUTED, and downstream of the one above anyway. `pgrep -af
      in ./$HOME        gridcoinresearch` found four processes, all accounted for, and
                        `du -sh ./$HOME` found no such directory.

    WHAT THIS GUARD IS FOR, THEN: a failure mode that is real, silent and cheap to
    refuse -- not an explanation of that incident. Writing it as the cause is the thing
    rule 17 exists to stop, and it took three rounds here. The incident itself cannot
    recur through `restart`, which no longer stops chain daemons at all (f788e5a), and
    what would settle it is the debug.log around the FAILING attempt rather than the
    successful one.

    REFUSED RATHER THAN EXPANDED HERE, and that is the same argument that docstring makes:
    expanding at run time would be a second place that decides what a command means. The
    writer owns the expansion; the runner's job is to notice an unexpanded value and stop.
    An expansion here would also have silently papered over every stored command that is
    already wrong, which is the state this was found in.

    THE DATADIR CHECK IS A SECOND, INDEPENDENT NET, and it checks what the daemon itself
    checks -- a directory that is not there is refused, after the fork, where nobody sees
    it. A wrong path need not contain a `$`, so it is checked separately; either guard
    alone would have caught 2026-10-13.
    """
    tokens = shlex.split(command)
    unexpanded = [token for token in tokens if "$" in token or token.startswith("~")
                  or "/~" in token]
    if unexpanded:
        return (
            f"REFUSED: {asset}_DAEMON_COMMAND contains {unexpanded[0]!r}, which is NOT expanded "
            f"before it runs -- this starts the command with shlex.split and no shell, so `$HOME` "
            f"and `~` reach the daemon LITERALLY. A daemon pointed at a directory that does not "
            f"exist DIES DURING STARTUP, and silently: -daemon has already forked and the parent "
            f"exited 0, so nothing reports it and this chain can never start. "
            f"Rewrite the stored value with the path spelled out: `python3 "
            f"capture_daemon_commands.py --accept-recorded --apply` expands it, or "
            f"`--wait --apply` with the daemon running stores the argv the kernel actually got."
        )
    datadir = next((token.split("=", 1)[1] for token in tokens
                    if token.startswith("-datadir=") and "=" in token), "")
    if datadir and not Path(datadir).is_dir():
        return (
            f"REFUSED: {asset}_DAEMON_COMMAND names -datadir={datadir} and no such directory "
            f"exists. A Bitcoin-derived daemon REFUSES a datadir that is not there rather than "
            f"creating one -- measured on this host 2026-10-13, where no such directory was ever "
            f"written -- and with -daemon it refuses AFTER forking, so the parent exits 0 and "
            f"nothing says a word. This will not run a command whose datadir cannot be right."
        )
    return ""


def start_cause(asset: str, command: str) -> str:
    """WHY a command could run and leave nothing listening, for this command. PURE.

    The decision only -- which cause applies to the line that was run. start_daemon()
    composes the measured prefix (exit code, port, elapsed) around it, so the one branch
    in here can be called with two strings and asserted on (rule 10, and PLR0913 when
    the measurements were parameters of it too).

    =========================================================================
    IT USED TO END WITH A CAUSE THAT CANNOT APPLY TO THE COMMAND IT RAN
    =========================================================================

    Unconditionally, this appended:

        "A daemon started without its own -daemon flag dies with this command"

    chains/daemon_launch.RECORDED["GRC"] is
    `gridcoinresearchd -datadir=$HOME/.GridcoinResearch-desk -daemon`. IT HAS THE FLAG.
    So on 2026-10-13 the operator's only explanation for a down chain named the one
    cause that was ruled out by the command printed beside it -- and `exit 0` was
    offered as if it told them something, when a forking daemon's parent exits 0 whether
    the child lives or dies.

    Rule 16: a wrong comment is a bug, and this one is in output somebody acts on while
    a chain is down.

    =========================================================================
    TWO CAUSES THAT LOOK IDENTICAL FROM HERE, AND THE FILE THAT SEPARATES THEM
    =========================================================================

    With `-daemon` present, `exit 0` means the fork succeeded and nothing more. Either
    the daemon is STILL LOADING -- wallet plus block index, which on a testnet node can
    exceed any budget this file picks -- or it EXITED DURING STARTUP. Nothing observable
    from outside the process distinguishes those, so this sentence does not guess
    between them (rule 17). It names the daemon's own debug.log, which says which, and
    the startup failure most likely in this exact sequence.

    THAT FAILURE IS NAMED AS A HYPOTHESIS AND NOT AS THE CAUSE. A Bitcoin-derived daemon
    closes its RPC listener EARLY in shutdown and then flushes the wallet and the block
    index, holding the datadir lock throughout -- so stop_daemon() can prove the PORT is
    free (it proved it in 1.0s) while the old process is still running, and a new one
    started immediately after exits with "Cannot obtain a lock on data directory". That
    is a real race in this module, visible by reading it. Whether it is what happened on
    the operator's host is not established, and the log is what would settle it.
    """
    if "-daemon" not in shlex.split(command):
        return (
            f"{asset}_DAEMON_COMMAND has no -daemon flag, so the daemon was a CHILD of this "
            f"short-lived command and died with it. Add -daemon to the command."
        )
    return (
        "THE EXIT CODE SAYS NOTHING HERE: the command carries -daemon, so its parent forks "
        "and exits 0 whether the daemon then lives or dies. TWO CAUSES LOOK THE SAME FROM "
        "OUTSIDE and only the daemon's own debug.log separates them -- it is in the datadir "
        "that command names, under testnet/ on a testnet run. (a) STILL LOADING: a "
        "Bitcoin-derived node binds its RPC port only after the wallet and block index are "
        "read, which can take longer than any budget this file picks; re-run `swap_stack.py "
        "chains` in a minute and it may simply be up. (b) EXITED DURING STARTUP, and the "
        "likeliest reason right after a stop is `Cannot obtain a lock on data directory` -- "
        "a daemon closes its RPC port EARLY in shutdown and keeps the datadir lock while it "
        "flushes, so a port proven free does not mean the old process is gone. NOT "
        "ESTABLISHED as the cause here; the log says which it was."
    )


def start_daemon(asset: str, command: str, port: int, *, budget_seconds: float = START_PROOF_SECONDS):
    """Run `command`, then wait for `port` to be LISTENing. Returns (state, sentence, seconds).

    shlex.split AND NOT shell=True. The value comes from the operator's own
    environment, so this is not a trust boundary in the usual sense -- but
    `shell=True` would make a stray `;` or `&&` in a datadir path into a second
    command, and the error from a mis-split argument list is readable where the
    error from a half-run shell line is not.

    THE PROCESS IS NOT REAPED HERE AND THAT IS STATED RATHER THAN OVERLOOKED
    (rule 13: name the reaper at the spawn site). A chain daemon launched with its
    own `-daemon` flag forks and detaches, so there is nothing left to wait on; one
    launched WITHOUT it would be a child of this short-lived command and would die
    with it, which the sentence below names so an operator sees why. Its reaper is
    `swap_stack.py down`, which is the other half of this module.
    """
    # BEFORE ANYTHING RUNS. A refusal here is not a failed start -- nothing was
    # launched -- so it returns the "failed" state with the reason, which is what `up`
    # prints and continues past.
    stop = refuse_to_run(asset, command)
    if stop:
        return "failed", stop, 0.0

    started = time.monotonic()
    try:
        completed = subprocess.run(  # noqa: S603 -- checked: the argument list comes from the operator's own environment variable and is shlex.split rather than passed to a shell, so no string is interpreted. This is the mechanism the operator asked for; refusing to run it would be refusing the feature.
            shlex.split(command), capture_output=True, text=True, timeout=budget_seconds, check=False,
        )
    except Exception as exc:  # noqa: BLE001 -- checked: a command that cannot be run at all (not found, not executable, timed out) is one outcome, returned with its reason so `up` reports it and continues to the next chain.
        return "failed", f"{asset}_DAEMON_COMMAND could not be run: {type(exc).__name__}: {exc}", 0.0

    while time.monotonic() - started < budget_seconds:
        if not port_is_free(port):
            waited = time.monotonic() - started
            return "started", f"is LISTENing on {port} after {waited:.1f}s", waited
        time.sleep(STOP_POLL_SECONDS)

    waited = time.monotonic() - started
    # THE EXIT CODE IS REPORTED ONLY WHEN THE PORT DID NOT OPEN, because a daemon
    # with `-daemon` exits 0 immediately and a nonzero code beside a listening port
    # would read as a failure that is not one.
    tail = (completed.stderr or completed.stdout or "").strip().splitlines()
    return (
        "not_listening",
        f"ran (exit {completed.returncode}) and nothing is LISTENing on {port} "
        f"after {waited:.1f}s"
        + (f" -- {tail[-1][:160]}" if tail else "")
        + f". {start_cause(asset, command)}",
        waited,
    )
