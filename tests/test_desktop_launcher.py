"""The desktop launcher's decisions. No display, no browser, no gunicorn needed.

Role: test (pure functions and a temp /proc tree; spawns nothing)
Reads: swap_terminal_desktop.py, install_desktop_icon.py
Writes: temp files only
Can move funds: no
Mainnet-safe: yes

EVERY TEST HERE CORRESPONDS TO A MEASURED BLOCKER. Three adversarial reviews of the
first design returned 35 findings and the verdict "DO NOT BUILD AS SPECIFIED" from
all three, so the decisions are extracted precisely so the fixes can be pinned on a
machine that cannot run the real thing -- this container has no display, no
chromium on PATH and no gunicorn installed.

What these tests CANNOT establish, stated rather than implied (rule 17): that a real
double-click starts a real gunicorn, that a real chromium window's close is seen, or
that a real teardown leaves no orphan. Those need the operator's desktop.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import install_desktop_icon
import swap_terminal_desktop as launcher

DB = "/home/op/swap_terminal/swap_terminal/swap_terminal.db"


# --- readiness: five answers, because collapsing any two was a defect ---------

def test_a_foreign_server_with_the_right_shape_is_not_ready():
    """THE blocker that would have opened a browser on somebody else's UI.

    A responder on that port returning exactly routes/health.py's body shape --
    {"status": "ok", ...} -- passed the first design's shape-only check. The fix is
    to bind identity: db_path must equal what this launcher put in the child's env.
    """
    verdict, why = launcher.readiness_verdict(200, {"status": "ok", "db_path": "/other/place.db"}, "", DB)

    assert verdict == "wrong-server"
    assert "/other/place.db" in why, "the refusal must name what the impostor claimed"


def test_identity_is_checked_before_health():
    """"ok" from a server that is not ours is the dangerous answer, not the safe one."""
    verdict, _why = launcher.readiness_verdict(200, {"status": "ok", "db_path": "/elsewhere.db"}, "", DB)

    assert verdict == "wrong-server", "a wrong server must not be reported merely as unhealthy"


def test_a_bound_socket_with_no_answer_is_not_ready():
    """gunicorn.conf.py sets preload_app=False, and its own comment says a worker
    that fails its spawn guard dies without taking the master down. The master's
    socket stays bound -- so a TCP connect would report ready for a server that
    cannot serve a single request."""
    verdict, why = launcher.readiness_verdict(None, None, "timed out", DB)

    assert verdict == "bound-but-silent"
    assert "preload_app" in why, "the line should name the setting that causes this"


def test_the_healthy_case_is_reachable():
    """Or every guard above passes against a function that never returns ready."""
    verdict, _why = launcher.readiness_verdict(200, {"status": "ok", "db_path": DB}, "", DB)

    assert verdict == "ready"


@pytest.mark.parametrize(("status", "body", "error", "expected"), [
    (None, None, "Connection refused", "not-listening"),
    (None, None, "timed out", "bound-but-silent"),
    (200, {"status": "ok", "db_path": "/x.db"}, "", "wrong-server"),
    (200, {"status": "degraded", "db_path": DB}, "", "unhealthy"),
    (500, None, "", "unhealthy"),
    (200, {"status": "ok", "db_path": DB}, "", "ready"),
])
def test_the_five_verdicts_are_all_distinguishable(status, body, error, expected):
    assert launcher.readiness_verdict(status, body, error, DB)[0] == expected


# --- teardown: a failed start must not read as a clean stop -------------------

def facts(**overrides):
    base = {"shim_absent": True, "group_left": (), "port_free": True,
            "browser_absent": True, "ever_served": True, "trigger": "window-closed"}
    return launcher.TeardownFacts(**{**base, **overrides})


def test_a_startup_that_never_served_does_not_report_stopped():
    """THE blocker. teardown_report(True, (), True) returned "stopped" for a run
    where readiness timed out, no browser ever opened and nothing was ever
    reachable -- because the verdict came from the post-state alone. "Never served,
    then tore down" and "served, then stopped cleanly" were the same word, and the
    last line on the operator's screen was a success line."""
    word, why = launcher.teardown_report(facts(ever_served=False, trigger="readiness-timed-out"))

    assert word == "NEVER-SERVED"
    assert "NOT a clean run" in why


def test_a_clean_window_close_reports_stopped():
    assert launcher.teardown_report(facts())[0] == "stopped"


def test_a_surviving_process_reports_leaked_however_clean_everything_else_looks():
    """Rule 13: an orphan does not crash anything, it holds a lock while everything
    downstream reports success."""
    word, why = launcher.teardown_report(facts(group_left=(4242,), port_free=False))

    assert word == "LEAKED"
    assert "4242" in why, "the leak must name the pid, or the operator cannot go look"


def test_an_orphaned_browser_is_a_leak_even_when_the_server_stopped():
    """The browser was a TRIGGER and not a SPAWN in the first design, so every
    teardown the window did not itself cause orphaned the whole chromium tree to
    pid 1. Rule 13 is unconditional -- every spawn needs a reaper."""
    assert launcher.teardown_report(facts(browser_absent=False, trigger="SIGHUP"))[0] == "LEAKED"


def test_a_signal_teardown_is_distinguished_from_a_window_close():
    """Both are clean, and they are not the same event: if the operator did not
    close the window, something else ended the run and they should know which."""
    word, _why = launcher.teardown_report(facts(trigger="SIGINT"))

    assert word == "stopped-early"
    assert word != launcher.teardown_report(facts())[0]


# --- the group walk: a zombie is not a survivor -------------------------------

def fake_proc(tmp_path, entries):
    """A /proc tree. entries: {pid: (state, pgid)}."""
    root = tmp_path / "proc"
    for pid, (state, pgid) in entries.items():
        directory = root / str(pid)
        directory.mkdir(parents=True)
        # Real format: `pid (comm) state ppid pgrp ...`, and comm may contain
        # spaces and parens, which is why the parser counts from the last ')'.
        (directory / "stat").write_text(f"{pid} (some (weird) name) {state} 1 {pgid} 0 0\n")
    return root


def test_a_zombie_is_not_counted_as_a_survivor(tmp_path):
    """THE blocker that made a CLEAN teardown report failure.

    The shim, having killed its own group, is itself a zombie in that group until
    its parent reaps it -- so the naive walk always found exactly one "survivor"
    and every successful teardown reported LEAKED.

    A zombie holds no lock, no port and no file. It is not running.
    """
    root = fake_proc(tmp_path, {100: ("Z", 100), 101: ("S", 100)})

    assert launcher.group_members(100, root) == (101,)


def test_a_group_with_only_zombies_is_empty(tmp_path):
    root = fake_proc(tmp_path, {100: ("Z", 100), 102: ("Z", 100)})

    assert launcher.group_members(100, root) == ()


def test_other_groups_are_not_counted(tmp_path):
    root = fake_proc(tmp_path, {100: ("S", 100), 200: ("S", 200)})

    assert launcher.group_members(100, root) == (100,)


def test_a_comm_containing_spaces_and_parens_does_not_break_the_parse(tmp_path):
    """/proc/<pid>/stat's second field is arbitrary text in parentheses. Splitting
    on whitespace from the left puts the state and pgid in the wrong columns, and
    the walk then silently matches nothing -- which reads as a clean teardown."""
    root = fake_proc(tmp_path, {303: ("R", 303)})

    assert launcher.group_members(303, root) == (303,)


# --- identity: positional, never a substring ---------------------------------

def test_the_identity_anchor_is_the_script_name():
    assert launcher.identity_anchor("/usr/bin/python3 /home/op/swap_terminal_desktop.py --shim") == \
        "swap_terminal_desktop.py"


@pytest.mark.parametrize("cmdline", [
    "/bin/grep -r /repo/swap_terminal_desktop.py /var/log",
    "/usr/bin/vim /repo/swap_terminal_desktop.py",
    "/bin/cat /repo/swap_terminal_desktop.py",
])
def test_a_command_that_merely_mentions_our_file_does_not_claim_our_identity(cmdline):
    """This test found a hole in the adversarial review's OWN fix.

    That fix proposed "the first .py token", and written against it this test failed
    at once: in `grep -r /repo/swap_terminal_desktop.py /var/log` the first .py token
    IS our launcher. So grepping, editing or cat-ing this file would claim its
    identity -- and the teardown would then signal somebody's grep.

    Requiring a python interpreter in argv[0] makes the anchor positional in the way
    that matters: the script is what a python RUNS, not a path that merely appears.
    """
    ours = launcher.identity_anchor("/usr/bin/python3 /repo/swap_terminal_desktop.py --shim")

    assert launcher.identity_anchor(cmdline) == ""
    assert launcher.identity_anchor(cmdline) != ours


def test_a_venv_interpreter_still_anchors():
    """The operator runs inside a .venv, so the interpreter is python3.12 under a
    venv path -- the check must match that, not only a bare /usr/bin/python3."""
    assert launcher.identity_anchor(
        "/home/op/.venv/bin/python3.12 /repo/swap_terminal_desktop.py --shim"
    ) == "swap_terminal_desktop.py"


def test_a_command_line_with_no_python_script_anchors_to_nothing():
    assert launcher.identity_anchor("/usr/sbin/sshd -D") == ""


# --- the browser: refuse rather than fall back -------------------------------

def test_no_browser_refuses_and_explains_why_firefox_is_not_a_fallback():
    found, note = launcher.choose_browser(which=lambda _name: None)

    assert found == ""
    assert "--kiosk removes the close button" in note, (
        "the refusal must say why firefox cannot substitute -- it deletes the signal this "
        "launcher is built on"
    )


def test_the_first_available_candidate_wins():
    found, note = launcher.choose_browser(which=lambda name: "/usr/bin/" + name if name == "google-chrome" else None)

    assert found == "/usr/bin/google-chrome"
    assert "google-chrome" in note


def test_the_browser_command_never_carries_no_sandbox(tmp_path):
    """--no-sandbox was needed only because the rehearsal container runs as root.

    The operator's desktop does not, and shipping it would weaken the browser's
    sandbox on a machine holding wallet RPC credentials. This is the kind of
    container artifact that survives into production unless something checks.
    """
    argv = launcher.browser_command("/usr/bin/chromium", "http://127.0.0.1:5000/", tmp_path / "profile")

    assert "--no-sandbox" not in argv
    assert any(part.startswith("--user-data-dir=") for part in argv), (
        "a dedicated profile is required: on a SHARED one a second launch returned rc=0 in 0.072s "
        "while the first stayed alive, so the launcher would tear the server down 72ms after "
        "opening the UI"
    )
    assert argv[1].startswith("--app="), "app mode is what gives a window whose close is an exit"


# --- the desktop entry -------------------------------------------------------

def test_the_rendered_entry_has_no_placeholders_left():
    entry = install_desktop_icon.rendered_desktop_entry(
        "/home/op/.venv/bin/python3", Path("/home/op/swap_terminal/swap_terminal_desktop.py"),
        Path("/home/op/.local/share/icons/swap-terminal.svg"),
    )

    assert "__SWAP_TERMINAL_" not in entry, "an unreplaced placeholder would be a launcher that cannot start"
    assert "Exec=/home/op/.venv/bin/python3 /home/op/swap_terminal/swap_terminal_desktop.py" in entry


def test_the_entry_bakes_in_the_venv_python_not_a_bare_name():
    """A desktop session's `python3` is the SYSTEM one, which has neither xrpl-py
    nor gunicorn. A bare name in Exec= would start an interpreter that cannot import
    the app, and the desktop reports that as nothing at all."""
    entry = install_desktop_icon.rendered_desktop_entry(
        "/home/op/.venv/bin/python3", Path("/x/launcher.py"), Path("/x/icon.svg"))
    exec_line = next(line for line in entry.splitlines() if line.startswith("Exec="))

    assert exec_line.startswith("Exec=/"), "Exec must be an ABSOLUTE path"
    assert "/.venv/" in exec_line


def test_the_entry_keeps_a_terminal_so_refusals_are_visible():
    """Terminal=false would send the readiness wait, the chain report, the mainnet
    warning and every refusal to nowhere -- and a refused double-click would be
    indistinguishable from the icon doing nothing (rule 14)."""
    entry = install_desktop_icon.rendered_desktop_entry("/p", Path("/l.py"), Path("/i.svg"))

    assert "Terminal=true" in entry


def test_a_path_with_a_space_is_refused_rather_than_quoted_and_hoped():
    """.desktop Exec= splits on spaces and has its own quoting grammar. Getting it
    subtly wrong starts the wrong argv, which the desktop reports as nothing."""
    safe, note = install_desktop_icon.paths_are_safe_for_exec(
        "/home/op/my venv/bin/python3", Path("/x/launcher.py"))

    assert safe is False
    assert "space" in note

    ok, _note = install_desktop_icon.paths_are_safe_for_exec("/usr/bin/python3", Path("/x/launcher.py"))
    assert ok is True
