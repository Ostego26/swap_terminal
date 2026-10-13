"""`on`/`off` cover the chain daemons, and the old icons get removed.

Role: test
Reads: swap_stack.py, swap_terminal/chain_daemons.py, install_desktop_icon.py
Writes: .desktop files under a synthetic HOME in pytest's tmp_path
Can move funds: no
Mainnet-safe: yes -- NO CHAIN, NO NETWORK, NO DAEMON. Every daemon here is a
       stub in this file, and the one test about mainnet asserts a REFUSAL
       without going near a port.

=============================================================================
WHAT THE OPERATOR ASKED FOR, three times across two days:
=============================================================================

    "when i fucking type docker up or docker down this should start/stop every
     subprocess or pid or what the fuckever a"                      (2026-10-11)
    "hey what the fuck why doesn't docker compose down stop that motherfucker?
     how many more times....."                                      (2026-10-12)
    "then we need to create icons to start stop remove old icons from my
     computer and have a cli to control the entire automatic swap terminal"
    "up.down make it on/off"

THE LITERAL QUESTION HAS A FLAT ANSWER: `docker compose` cannot stop a host
process. bitcoind, litecoind and gridcoinresearchd are not containers -- the
reason docker-compose.web.yml needs `extra_hosts: host.docker.internal:host-gateway`
at all is that they are outside the container network. Compose stops what compose
started.

So the coverage belongs to swap_stack.py, which runs on the host and already had
`up`/`down`. Its own `down` banner said, before this:

    never stopped     chain daemons, and a FOREIGN listener on one of our ports

Reported rather than silent, which is better than nothing, and an exclusion stated
as a fact with no reason behind it -- on the command whose whole purpose is
"nothing of this stack is running when this returns".
"""

from __future__ import annotations

import pathlib
import socket
import subprocess
import sys

import network_target
import pytest
import requests

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "swap_terminal"))

import chain_daemons  # noqa: E402
from stack_authority import PAIR_EXIT_CODES, pair_exit_code  # noqa: E402

import install_desktop_icon as installer_module  # noqa: E402
import swap_stack  # noqa: E402

# --- the verbs ------------------------------------------------------------------


def test_on_and_off_are_the_verbs():
    assert "on" in swap_stack.PRIMARY_ACTIONS
    assert "off" in swap_stack.PRIMARY_ACTIONS
    assert "up" not in swap_stack.PRIMARY_ACTIONS
    assert "down" not in swap_stack.PRIMARY_ACTIONS


def test_up_and_down_STILL_WORK_and_are_the_same_functions():
    """A rename that broke the installed launchers would make them dead on next click.

    Three things name `up`/`down` outside the repo: the .desktop files in
    ~/.local/share/applications, the copies on the Desktop, and the operator's shell
    history. assets/swap-terminal-action.desktop records what a dead launcher looks
    like from 2026-10-09 -- "the click registers and nothing appears" -- and a rename
    with no alias reproduces exactly that.

    SAME FUNCTION OBJECT, not a second copy, so the alias cannot drift from the verb
    (rule 8 is about copies, not about names).
    """
    assert swap_stack.ACTIONS["up"] is swap_stack.ACTIONS["on"]
    assert swap_stack.ACTIONS["down"] is swap_stack.ACTIONS["off"]


def test_help_advertises_the_new_verbs_and_still_accepts_the_old():
    done = subprocess.run(
        [sys.executable, str(REPO_ROOT / "swap_stack.py"), "--help"],
        capture_output=True, text=True, timeout=120, check=False,
    )
    assert "on" in done.stdout and "off" in done.stdout
    assert "up" in done.stdout and "down" in done.stdout, (
        "the aliases must be ACCEPTED; they are listed because argparse prints its own choices"
    )


# --- the safety decision --------------------------------------------------------


@pytest.mark.parametrize("port,chain", sorted(chain_daemons.MAINNET_RPC_PORTS.items()))
def test_a_MAINNET_port_is_refused_by_name(port, chain):
    """THE ASSERTION THAT MATTERS MOST IN THIS FILE.

    CLAUDE.md: "the operator's mainnet wallet is a live staking wallet holding real
    coins." config.py's own header records an unset GRC_RPC_PORT falling through to
    15715 and the inventory poller reading that wallet on a loop -- and that was a
    READ. This module would be asking it to shut down.
    """
    refusal = chain_daemons.refuse_to_touch(
        "GRC", {"port": port, "host": "127.0.0.1", "password": "x"}
    )
    assert refusal.refuses
    assert str(port) in refusal.why
    assert chain in refusal.why
    assert "REFUSED BY NAME" in refusal.why


def test_a_remote_host_is_refused():
    """Reaching across a network to shut down somebody else's node is not `off`'s job."""
    refusal = chain_daemons.refuse_to_touch(
        "LTC", {"port": 19443, "host": "10.0.0.5", "password": "x"}
    )
    assert refusal.refuses
    assert "not loopback" in refusal.why


def test_an_unconfigured_chain_is_refused_without_drama():
    assert chain_daemons.refuse_to_touch("BTC", {}).refuses
    assert chain_daemons.refuse_to_touch("BTC", {"host": "127.0.0.1", "password": "x"}).refuses


def test_a_configured_testnet_daemon_on_loopback_is_NOT_refused(monkeypatch):
    """AND IT NEEDS LTC_DAEMON_COMMAND, since 2026-10-12. See the symmetry test below.

    This test passed without the variable until that day, which is the hole: a daemon
    with no start command was stoppable, and on the operator's host the thing on that
    port was their GUI wallet.
    """
    monkeypatch.setenv("LTC_DAEMON_COMMAND", "/usr/local/bin/litecoind -testnet -daemon")
    refusal = chain_daemons.refuse_to_touch(
        "LTC", {"port": 19443, "host": "127.0.0.1", "password": "x"}
    )
    assert not refusal.refuses, refusal.why


# --- never stop what you cannot start -------------------------------------------
#
# THE REGRESSION THAT CLOSED THE OPERATOR'S WALLET, 2026-10-12. Their words: "hey what
# the fuck did you do? why'd my 'external' litecoin gui wallet just close".
#
# THE FIXTURES IN THIS FILE USE 19443, NOT 19332, SINCE 2026-10-13. Six of them stood
# in for "an ordinary LTC testnet daemon" with 19332 -- Litecoin's DEFAULT testnet RPC
# port, which on this host is the operator's EXTERNAL litecoin-qt. Once
# network_target.EXTERNAL_WALLET_PORTS landed, those six hit the new refusal first and
# three tests about OTHER guards failed on their own fixture. 19443 is the desk's own
# litecoind, which is what every one of them meant. The one place 19332 is still used
# deliberately is the external-wallet test at the bottom, where it is the subject.
#
# `off` stops by the configured RPC port and sends `stop`. The program holding 19332
# was `litecoin-qt`, an interactive GUI wallet, not a headless litecoind -- and
# capture_daemon_commands.py had printed "(none)" scanning for `litecoind` in the same
# hour, which was the evidence, on screen, read past.
#
# regtest/operator_panel.DAEMON_CONTROL already encoded the invariant:
# ("foreign", "stop", False) -> _refuse_stop_no_handle, whose docstring is "A STOP
# NEEDS A HANDLE, AND THERE IS NONE." chain_daemons did not consult it.


def test_a_daemon_with_NO_start_command_is_NOT_stoppable(monkeypatch):
    """THE ASSERTION THE OPERATOR'S CLOSED WALLET EARNED.

    MUTATION: remove _refuse_cannot_start_it_again from _REFUSAL_GUARDS. This fails.
    """
    monkeypatch.delenv("LTC_DAEMON_COMMAND", raising=False)
    refusal = chain_daemons.refuse_to_touch(
        "LTC", {"port": 19443, "host": "127.0.0.1", "password": "x"}
    )
    assert refusal.refuses
    assert "LTC_DAEMON_COMMAND is unset" in refusal.why
    assert "cannot START it again" in refusal.why


def test_the_refusal_says_how_to_opt_in(monkeypatch):
    """A refusal that does not say what to do instead is rule 14's ambiguous silence."""
    monkeypatch.delenv("GRC_DAEMON_COMMAND", raising=False)
    why = chain_daemons.refuse_to_touch(
        "GRC", {"port": 25779, "host": "127.0.0.1", "password": "x"}
    ).why
    assert "capture_daemon_commands.py" in why


def test_the_symmetry_holds_for_every_chain_with_a_local_daemon(monkeypatch):
    """Not just LTC. Every one of them, or the next GUI wallet closes instead."""
    for asset in chain_daemons.MAINNET_RPC_PORTS_BY_ASSET:
        monkeypatch.delenv(f"{asset}_DAEMON_COMMAND", raising=False)
        refusal = chain_daemons.refuse_to_touch(
            asset, {"port": 19443, "host": "127.0.0.1", "password": "x"}
        )
        assert refusal.refuses, f"{asset} is stoppable with no start command"


def test_MAINNET_is_still_refused_even_WITH_a_start_command(monkeypatch):
    """The guards are ordered and mainnet comes first. A start command must not unlock it.

    Otherwise `capture_daemon_commands.py --apply` on a mainnet daemon would hand `off`
    permission to shut down a live staking wallet -- which is why that tool refuses to
    capture a mainnet command in the first place, and why this is belt to that braces.
    """
    monkeypatch.setenv("GRC_DAEMON_COMMAND", "/usr/local/bin/gridcoinresearchd -daemon")
    refusal = chain_daemons.refuse_to_touch(
        "GRC", {"port": 15715, "host": "127.0.0.1", "password": "x"}
    )
    assert refusal.refuses
    assert "REFUSED BY NAME" in refusal.why


def test_every_guard_is_callable_on_its_own(monkeypatch):
    """rule 10: the extraction has to actually make each decision seedable.

    A list of guards that can only be exercised through refuse_to_touch() would be the
    same function with extra steps.
    """
    monkeypatch.delenv("LTC_DAEMON_COMMAND", raising=False)
    settings = {"port": 19443, "host": "127.0.0.1", "password": "x"}
    assert chain_daemons._refuse_no_settings("LTC", {}, "")
    assert chain_daemons._refuse_no_settings("LTC", settings, "") == ""
    assert chain_daemons._refuse_mainnet_port("LTC", {"port": 9332}, "")
    assert chain_daemons._refuse_mainnet_port("LTC", settings, "") == ""
    assert chain_daemons._refuse_unconfigured("LTC", settings, "a reason") == "a reason"
    assert chain_daemons._refuse_no_port("LTC", {"host": "127.0.0.1"}, "")
    assert chain_daemons._refuse_remote_host("LTC", {"port": 1, "host": "10.0.0.5"}, "")
    assert chain_daemons._refuse_cannot_start_it_again("LTC", settings, "")


# --- the stop verdict -----------------------------------------------------------


def test_the_four_stop_outcomes_are_four_and_not_two():
    """Rule 13: "skipped" plus "success" in one output is a defect in the OUTPUT.

    accepted-but-still-listening and never-accepted send an operator to opposite
    places -- more time versus check the endpoint -- so they cannot share a line.
    """
    states = {
        chain_daemons.stop_verdict(True, "", True, 1.0)[0],
        chain_daemons.stop_verdict(True, "", False, 30.0)[0],
        chain_daemons.stop_verdict(False, "refused", True, 0.1)[0],
        chain_daemons.stop_verdict(False, "refused", False, 0.1)[0],
    }
    assert states == {"stopped", "stopping", "already_down", "unreachable"}


def test_a_daemon_still_listening_is_NOT_killed_and_the_sentence_says_so():
    _state, sentence = chain_daemons.stop_verdict(True, "", False, 30.0)
    assert "Not killed" in sentence
    assert "wallet.dat" in sentence, (
        "the reason must be in the sentence: a Bitcoin-derived daemon killed mid-flush can "
        "damage its wallet, which is why this path does not escalate to a signal"
    )


def test_stop_daemon_never_raises_when_the_daemon_cannot_be_asked():
    """`off` must report every chain and keep going, like refresh_wallet_inventory()."""

    class Unreachable:
        def call(self, method):
            raise requests.exceptions.ConnectionError("refused")

    state, sentence, _ = chain_daemons.stop_daemon(
        "LTC", Unreachable(), _free_port(), budget_seconds=1.0
    )
    assert state == "already_down"
    assert "refused" in sentence


def test_stop_daemon_sends_ONLY_the_stop_method():
    """One verb. Nothing here signs, unlocks or spends."""
    sent = []

    class Recorder:
        def call(self, method, *params):
            sent.append((method, params))

    chain_daemons.stop_daemon("LTC", Recorder(), _free_port(), budget_seconds=1.0)
    assert sent == [("stop", ())]


def _free_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def test_port_is_free_does_not_set_SO_REUSEADDR():
    """With it, a bind can succeed beside a TIME_WAIT socket and report a false free."""
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    try:
        assert chain_daemons.port_is_free(port) is False
    finally:
        listener.close()
    assert chain_daemons.port_is_free(port) is True


# --- starting: reported, never guessed ------------------------------------------


def test_an_unset_start_command_is_reported_rather_than_invented(monkeypatch):
    """rule 17. This session spent an hour aimed at the wrong one of three GRC daemons.

    `litecoind -datadir=... -daemon` looks obvious and would have been a guess about
    a host this file cannot read.
    """
    monkeypatch.delenv("LTC_DAEMON_COMMAND", raising=False)
    assert chain_daemons.start_command("LTC") == ""
    monkeypatch.setenv("LTC_DAEMON_COMMAND", "  litecoind -daemon  ")
    assert chain_daemons.start_command("LTC") == "litecoind -daemon"


def test_a_start_command_that_cannot_run_is_an_outcome_not_an_exception():
    state, sentence, _ = chain_daemons.start_daemon(
        "LTC", "/definitely/not/a/program --flag", _free_port(), budget_seconds=1.0
    )
    assert state == "failed"
    assert "LTC_DAEMON_COMMAND" in sentence


def test_a_command_that_exits_without_listening_says_why():
    """A daemon started WITHOUT its own -daemon flag dies with the launching command."""
    state, sentence, _ = chain_daemons.start_daemon(
        "LTC", "/bin/true", _free_port(), budget_seconds=1.0
    )
    assert state == "not_listening"
    assert "-daemon" in sentence


# --- the icons ------------------------------------------------------------------


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    # RELOADED, NOT JUST IMPORTED, and the import is inside the fixture for that
    # reason rather than by oversight. install_desktop_icon computes DESKTOP_DIR,
    # COMMAND_TARGET and the rest from Path.home() AT IMPORT TIME, so a module
    # already imported under the real HOME would keep the real paths and this whole
    # file would be scanning the actual machine. ruff's PLC0415 is suppressed for
    # exactly these two lines and nowhere else.
    import importlib  # noqa: PLC0415 -- see above: the reload must happen after monkeypatch sets HOME

    import install_desktop_icon  # noqa: PLC0415 -- same reason; its module-level paths are computed from HOME at import

    importlib.reload(install_desktop_icon)
    install_desktop_icon.DESKTOP_DIR.mkdir(parents=True, exist_ok=True)
    (tmp_path / "Desktop").mkdir(parents=True, exist_ok=True)
    return install_desktop_icon


def write_launcher(directory: pathlib.Path, name: str, exec_line: str) -> pathlib.Path:
    path = directory / name
    path.write_text("[Desktop Entry]\nName=x\nExec=" + exec_line + "\n")
    return path


def test_the_icon_names_follow_the_verbs(fake_home):
    names = fake_home.current_launcher_names()
    assert "swap-terminal-on.desktop" in names
    assert "swap-terminal-off.desktop" in names
    assert "swap-terminal-up.desktop" not in names


def test_a_renamed_verbs_launcher_is_stale(fake_home):
    """THE PROBLEM THE RENAME ITSELF CREATED, which is why the prune landed with it."""
    write_launcher(
        fake_home.DESKTOP_DIR, "swap-terminal-up.desktop",
        f"/bin/bash {fake_home.COMMAND_TARGET} up",
    )
    stale = {p.name for p, _why in fake_home.stale_launchers()}
    assert "swap-terminal-up.desktop" in stale


def test_a_launcher_pointing_at_a_DELETED_file_is_stale(fake_home):
    """CLAUDE.md rule 2 names .desktop launchers as invisible references by name.

    atomic_swap_gui.py was deleted 2026-09-26 after being proven dead. A launcher
    still pointing at it sits in the menu and clicks to nothing -- rule 14's
    ambiguous silence, on an icon.
    """
    write_launcher(
        fake_home.DESKTOP_DIR, "swap-terminal.desktop",
        "/usr/bin/python3 /home/user/swap_terminal/atomic_swap_gui.py",
    )
    stale = {p.name: why for p, why in fake_home.stale_launchers()}
    assert "swap-terminal.desktop" in stale
    assert "atomic_swap_gui.py" in stale["swap-terminal.desktop"]


def test_a_FOREIGN_launcher_is_never_touched(fake_home):
    """This is not a general desktop cleaner and must never delete what it did not write."""
    write_launcher(fake_home.DESKTOP_DIR, "firefox-pin.desktop", "/usr/bin/firefox")
    write_launcher(fake_home.DESKTOP_DIR, "mammon-restart.desktop", "/does/not/exist")
    assert fake_home.stale_launchers() == []


def test_it_does_NOT_eat_its_own_output_on_a_first_install(fake_home):
    """THE BUG A SYNTHETIC HOME CAUGHT, 2026-10-12.

    main() computes the stale list BEFORE apply_all() writes. On a first install
    ~/.local/bin/swapterm does not exist yet, so a CORRECTLY named launcher pointing
    at it was reported stale -- flag it, write it, delete the file just written. The
    dry run would have printed "would write" and "would remove" for the same path,
    two lines apart.
    """
    write_launcher(
        fake_home.DESKTOP_DIR, "swap-terminal-on.desktop",
        f"/bin/bash {fake_home.COMMAND_TARGET} on",
    )
    assert not fake_home.COMMAND_TARGET.exists(), "the fixture must model a FIRST install"
    stale = {p.name for p, _why in fake_home.stale_launchers()}
    assert "swap-terminal-on.desktop" not in stale


def test_it_scans_the_Desktop_as_well_as_the_menu(fake_home):
    """--desktop-files puts copies on the Desktop, so the prune has to look there too."""
    write_launcher(
        pathlib.Path.home() / "Desktop", "swap-terminal-down.desktop",
        f"/bin/bash {fake_home.COMMAND_TARGET} down",
    )
    paths = [p for p, _why in fake_home.stale_launchers()]
    assert any(p.parent.name == "Desktop" for p in paths)


def test_removal_reports_each_file_and_the_empty_case_is_not_blank(fake_home, capsys):
    """rule 14: "(none)" is a result; a blank gap is ambiguous between zero and broken."""
    fake_home.print_stale([], applying=False)
    assert "(none)" in capsys.readouterr().out


def test_remove_stale_actually_deletes(fake_home):
    path = write_launcher(
        fake_home.DESKTOP_DIR, "swap-terminal-up.desktop",
        f"/bin/bash {fake_home.COMMAND_TARGET} up",
    )
    assert path.exists()
    fake_home.remove_stale(fake_home.stale_launchers())
    assert not path.exists()


def test_no_icon_comment_claims_the_daemons_are_LEFT_RUNNING():
    """Every ACTION_ENTRIES comment said so, and all of them became false on 2026-10-12.

    These sentences are what a one-click user reads INSTEAD of documentation, which
    makes a stale one rule 16's wrong comment in the worst available place.
    """
    for action, name, comment in installer_module.ACTION_ENTRIES:
        lowered = comment.lower()
        assert "leaves the chain daemons running" not in lowered, (
            f"the {action} icon ({name}) still promises the daemons are left running"
        )
        assert "starts no chain daemon" not in lowered, (
            f"the {action} icon ({name}) still promises it starts no chain daemon"
        )


# --- `pairs`, the sixth action -------------------------------------------------


def test_pairs_names_a_file_that_is_actually_there():
    """Nothing imports a root tool, so a rename breaks no import and reports nowhere.

    This is rule 13's `pgrep -f` hazard in a different shape: cmd_pairs() names
    pair_verdicts.py as a STRING, and a pattern matches what the tree happens to look
    like today. The function itself refuses with the path when the file is gone --
    which is the right behavior at runtime and no help at all if the rename shipped.
    """
    assert (REPO_ROOT / swap_stack._PAIR_VERDICTS).is_file(), (
        f"swap_stack._PAIR_VERDICTS names {swap_stack._PAIR_VERDICTS}, which is not in the "
        f"repository root. If it moved, move this name with it."
    )


def test_pairs_is_an_action_and_makes_no_compose_call(monkeypatch):
    """It runs the report on the HOST, so it must not touch docker at all.

    `compose()` is replaced with something that fails the test if called, which is
    stronger than asserting on the output: the claim in cmd_pairs()'s docstring is
    "makes no compose call", and the only honest check of that is to make a compose
    call impossible.
    """
    assert swap_stack.PRIMARY_ACTIONS["pairs"] is swap_stack.cmd_pairs

    def refuse(*args, **kwargs):
        raise AssertionError("cmd_pairs() must not shell out to docker compose")

    monkeypatch.setattr(swap_stack, "compose", refuse)
    recorded = {}

    def fake_run(argv, **kwargs):
        recorded["argv"] = argv
        recorded["cwd"] = kwargs.get("cwd")
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(swap_stack.subprocess, "run", fake_run)

    assert swap_stack.cmd_pairs(("docker-compose.yml",)) == 0
    assert recorded["argv"][0] == sys.executable, (
        "the interpreter running swap_stack must run the report, not whichever python3 "
        "is first on PATH -- they can read different configurations"
    )
    assert recorded["argv"][1].endswith(swap_stack._PAIR_VERDICTS)
    assert recorded["cwd"] == REPO_ROOT


def test_pairs_returns_the_reports_own_verdict_rather_than_a_second_opinion(monkeypatch):
    """The exit code is pair_verdicts.py's. A second opinion about the same rows is
    the defect services/pair_view.py's header records, where /admin and the customer
    page disagreed about one pair in one process.
    """
    monkeypatch.setattr(
        swap_stack.subprocess, "run",
        lambda argv, **kwargs: subprocess.CompletedProcess(argv, 7),
    )

    assert swap_stack.cmd_pairs(()) == 7, (
        "7 is not in PAIR_EXIT_CODES, so cmd_pairs() must pass it through unchanged "
        "rather than translating it into a verdict of its own"
    )


def test_a_missing_report_says_NOTHING_WAS_ASKED_rather_than_passing(monkeypatch, capsys):
    """Rule 13: "skipped" plus "success" in one output is a defect in the output.

    cmd_chains() already takes this posture for a container that is not serving -- "so
    this says NOTHING about the chains" -- and the same sentence is owed here, because
    a report that did not run is not a desk with no problems.
    """
    monkeypatch.setattr(swap_stack, "_PAIR_VERDICTS", "a_file_that_is_not_in_this_tree.py")

    def refuse(*args, **kwargs):
        raise AssertionError("nothing should be run when the report is missing")

    monkeypatch.setattr(swap_stack.subprocess, "run", refuse)

    assert swap_stack.cmd_pairs(()) == 1
    printed = capsys.readouterr().out
    assert "MISSING" in printed
    assert "says NOTHING" in printed


def test_a_crash_is_reported_as_NOTHING_ESTABLISHED_and_not_as_a_verdict(capsys):
    """THE DEFECT WAS ON THE OPERATOR'S SCREEN, in the output of the tool that reports.

    2026-10-12: they ran `pairs` with the system interpreter, pair_verdicts.py died at
    import on a missing `bech32`, CPython exited 1, and cmd_pairs() printed

        exit 1            at least one allowed pair cannot be quoted -- the
                          `blocked by` block above says which condition

    with no block above it. A specific false claim about thirty pairs nothing had
    evaluated, which is worse than rule 13's "skipped plus success" because it is
    actionable in the wrong direction: it sends the operator to look at daemons.

    EVERY CODE OUTSIDE THE VOCABULARY, not just 1, because the point is the set. The
    parametrised sweep below covers the three verdicts; this covers everything else.
    """
    for code in (1, 2, 127, -9):
        swap_stack.say_pair_exit(code)
        printed = capsys.readouterr().out
        assert "THE REPORT DID NOT RUN" in printed, code
        assert "says NOTHING about the pairs" in printed, code
        assert "cannot be quoted" not in printed, (
            f"exit {code} is not a verdict and must not be reported as one"
        )
        assert sys.executable in printed, (
            "the interpreter is the thing to change when a dependency is missing, so it "
            "is echoed (rule 14)"
        )


@pytest.mark.parametrize("status", sorted(PAIR_EXIT_CODES))
def test_each_verdict_code_gets_its_own_sentence(status, capsys):
    """Three verdicts, three distinct sentences, and none of them the crash sentence.

    `no_pairs` is separate from `some_unavailable` on purpose: an empty
    Config.ALLOWED_PAIRS is the one reading where six dark lamps is CORRECT, and
    collapsing it into the verdict would send an operator hunting a down daemon.
    """
    swap_stack.say_pair_exit(pair_exit_code(status))
    printed = capsys.readouterr().out

    assert "THE REPORT DID NOT RUN" not in printed
    expected = {
        "all_quotable": "every allowed pair can be quoted",
        "some_unavailable": "at least one allowed pair cannot be quoted",
        "no_pairs": "ALLOWED_PAIRS is empty",
    }
    assert expected[status] in printed, (
        f"{status} must say what it means next to the number (rule 14); got {printed!r}"
    )


def test_the_unset_command_refusal_names_BOTH_ways_out(monkeypatch):
    """The loop the operator was inside, closed from this end too.

    This refusal pointed only at `capture_daemon_commands.py --apply`, which reads the
    command line of whatever is LISTENing on the configured port. It is printed exactly
    when nothing is -- so with GRC and LTC both down on 2026-10-12, `on`/`off` sent the
    operator to the capture tool and the capture tool said "nothing is LISTENing". A
    closed loop in the CLI they had asked for two days earlier.
    """
    monkeypatch.delenv("LTC_DAEMON_COMMAND", raising=False)
    refusal = chain_daemons._refuse_cannot_start_it_again(
        "LTC", {"port": 19443, "host": "127.0.0.1"}, "",
    )

    assert refusal, "with no DAEMON_COMMAND set, this must refuse"
    assert "--apply" in refusal, "the running-daemon path is still named"
    assert "RECORDED" in refusal, (
        "and so is the nothing-is-running path -- chains/daemon_launch.py -- or the "
        "refusal sends the operator back into the loop it is printed inside"
    )
    # THE THIRD WAY OUT, added 2026-10-12 after the operator asked the question that
    # exposed the gap: "why wasn't the daemon already up from when we hit the stack on or
    # up python command". Because `on` starts only from this variable, and nothing here
    # told them how to fill it WITHOUT first starting the daemon by hand -- which is the
    # thing `on` was supposed to do for them.
    assert "--accept-recorded" in refusal, (
        "the refusal must name the one command that teaches `on` to start this daemon; "
        "without it, `on` tells the operator to go and do `on`'s job by hand"
    )
    assert "--wait" in refusal, (
        "and the capture path must carry --wait, or it races the daemon it just told them "
        "to start (-daemon returns before the RPC port is bound)"
    )


# --- the three wallets per chain, and which one the stack may address ------------


def test_an_external_wallets_port_is_refused_by_name_before_anything_is_asked():
    """THE GUARD THAT WOULD HAVE SAVED THE WALLET, rather than the one added after.

    2026-10-12: `off` stopped the daemon on LTC_RPC_PORT and closed the operator's
    litecoin-qt. The guard added then was _refuse_cannot_start_it_again -- symmetry,
    never stop what you cannot start -- which is correct and keeps its place. It treated
    a symptom: the window was reachable at all because the terminal was CONFIGURED
    AGAINST AN EXTERNAL WALLET, and it still was a day later because nothing in the tree
    could say so.

    Operator, 2026-10-13: "ltc is not the gui wallet that is a 'external' wallet for the
    swapper to send or receive ltc", and the same for Gridcoin's 25715.
    """
    for asset, port in (("LTC", 19332), ("GRC", 25715)):
        refusal = chain_daemons.refuse_to_touch(asset, {"port": port, "host": "127.0.0.1"})

        assert refusal.refuses, f"{asset} on {port} was not refused"
        assert "NOT this stack's daemon" in refusal.why
        assert "REFUSED BY NAME" in refusal.why, (
            "a refusal an operator cannot check is one they route around"
        )


def test_the_stacks_own_ports_are_NOT_refused_by_that_guard():
    """A guard that refused everything would be safe and useless.

    These are the headless CLI daemons the stack owns -- bitcoind 18443, litecoind 19443,
    gridcoinresearchd 25779 -- and the external guard must be silent on all three. Other
    guards may still speak (an unset <ASSET>_DAEMON_COMMAND, for one), so this asserts
    the ABSENCE of this guard's sentence rather than the absence of any refusal.
    """
    for asset, port in (("BTC", 18443), ("LTC", 19443), ("GRC", 25779)):
        why = chain_daemons._refuse_external_wallet(asset, {"port": port, "host": "127.0.0.1"}, "")

        assert why == "", f"{asset}'s own daemon on {port} was called an external wallet: {why}"


def test_three_gridcoin_wallets_and_each_one_has_a_rule():
    """Operator, 2026-10-13, twice over: "there two motherfucking gui wallets for grc
    running on this host. one is a mainnet and don't fucking touch it ever. rule. the
    other is the test net 'external'".

    So three wallets, three rules, and all three are asserted here because the cost of
    confusing them is not symmetric: 15715 holds real coins and is unlocked for staking.

    MAINNET IS CHECKED FIRST, which is asserted rather than assumed: 15715 must report
    the MAINNET fact, not the external one, because that is the sentence naming real
    money.
    """
    mainnet = chain_daemons.refuse_to_touch("GRC", {"port": 15715, "host": "127.0.0.1"})
    assert mainnet.refuses
    assert "GRC mainnet" in mainnet.why, "15715 must be refused as MAINNET, the stronger fact"

    external = chain_daemons.refuse_to_touch("GRC", {"port": 25715, "host": "127.0.0.1"})
    assert external.refuses
    assert "NOT this stack's daemon" in external.why

    ours = chain_daemons._refuse_external_wallet("GRC", {"port": 25779, "host": "127.0.0.1"}, "")
    assert ours == "", "25779 is the desk's own gridcoinresearchd and must be addressable"


def test_every_external_entry_carries_its_evidence():
    """A hands-off list whose entries cannot be checked is a list people route around.

    Rule 17 in table form: each entry says what the port is AND how that was established,
    with a date, so a reader can disagree with the measurement rather than with the rule.
    """
    for port, why in network_target.EXTERNAL_WALLET_PORTS.items():
        assert len(why) > 80, f"{port}'s entry does not say what it is and how that was measured"
        assert "2026-" in why, f"{port}'s entry carries no date for its measurement"
