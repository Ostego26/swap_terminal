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

import pytest
import requests

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "swap_terminal"))

import chain_daemons  # noqa: E402

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
        "LTC", {"port": 19332, "host": "10.0.0.5", "password": "x"}
    )
    assert refusal.refuses
    assert "not loopback" in refusal.why


def test_an_unconfigured_chain_is_refused_without_drama():
    assert chain_daemons.refuse_to_touch("BTC", {}).refuses
    assert chain_daemons.refuse_to_touch("BTC", {"host": "127.0.0.1", "password": "x"}).refuses


def test_a_configured_testnet_daemon_on_loopback_is_NOT_refused():
    refusal = chain_daemons.refuse_to_touch(
        "LTC", {"port": 19332, "host": "127.0.0.1", "password": "x"}
    )
    assert not refusal.refuses, refusal.why


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
