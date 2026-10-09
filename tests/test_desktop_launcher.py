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

import inspect
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import install_desktop_icon
import swap_stack
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
    # BOTH MODES, because a new argv shape is exactly where this gets copied in.
    # --window was added 2026-10-02 and returns a different list; checking only the
    # default would have left it unguarded, and a second near-identical test for it
    # is rule 9's dead weight.
    assert "--no-sandbox" not in launcher.browser_command(
        "/usr/bin/chromium", "http://127.0.0.1:5000/", tmp_path / "profile", app_mode=False
    )
    assert any(part.startswith("--user-data-dir=") for part in argv), (
        "a dedicated profile is required: on a SHARED one a second launch returned rc=0 in 0.072s "
        "while the first stayed alive, so the launcher would tear the server down 72ms after "
        "opening the UI"
    )
    assert argv[1].startswith("--app="), "app mode is what gives a window whose close is an exit"


# --- the desktop entry -------------------------------------------------------

# =============================================================================
# `swapterm` ON PATH, AND THE THREE ACTION ICONS
#
# Operator, 2026-10-09: "i need like icons on the desktop and shell commands
# like swaptermi up or down and everything stops safely. all services daemons
# are done database, etc." and "swapterm restart".
#
# The command and the icons both route through the SAME installed wrapper, so
# these pin that routing as well as the rendering -- an icon that started the
# stack a different way from the shell command is rule 8's two spellings, and
# the one that drifts is the one nobody runs.
# =============================================================================


def _directives(entry: str) -> dict[str, str]:
    """A .desktop's key=value pairs, with comments and the group header dropped.

    WRITTEN BECAUSE THREE ASSERTIONS HERE MATCHED PROSE INSTEAD. `"StartupNotify=true"
    not in entry` was true of the template's own explanatory COMMENT, which quotes
    the wrong value to explain why it is wrong -- so the assertion failed on a
    correct file. The same shape as the menu-line test earlier today, and the
    third instance in one session: an assertion true of something ADJACENT to
    what it claims.

    A .desktop is an ini file. Reading it as one costs four lines and makes
    "StartupNotify is false" a different statement from "the string
    StartupNotify=false appears somewhere in this file".
    """
    values = {}
    for line in entry.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith(("#", "[")):
            continue
        key, _, value = stripped.partition("=")
        values[key.strip()] = value.strip()
    return values


def test_no_launcher_asks_for_a_startup_notification_it_cannot_send():
    """THE DEFECT THAT MADE THE ICONS DEAD, measured on the operator's own desktop.

    They were written with StartupNotify=true and clicking them did nothing. The
    one launcher on that desktop which has worked since September --
    ~/Desktop/mammon-restart.desktop -- differs in exactly two ways, and this is
    the first: it says false.

    With StartupNotify=true the desktop waits for the launched application to
    send a startup-notification completion. A terminal emulator opening a shell
    script never sends one, so the launch sits pending and then silently gives
    up: the click registers and nothing appears. Terminal=true and
    StartupNotify=true are close to mutually exclusive for that reason.

    MUTATION: set either template back to true. The icons go dead again and
    nothing else in the suite notices, which is why this is asserted on every
    launcher rather than reviewed.
    """
    entries = [
        install_desktop_icon.rendered_desktop_entry("/p", Path("/l.py"), Path("/i.svg")),
        *(
            install_desktop_icon.rendered_action_entry(a, n, c, Path("/b/swapterm"), Path("/i.svg"))
            for a, n, c in install_desktop_icon.ACTION_ENTRIES
        ),
    ]
    for entry in entries:
        values = _directives(entry)
        name = values.get("Name", "(no Name)")
        assert values.get("StartupNotify") == "false", (
            f"{name} has StartupNotify={values.get('StartupNotify')!r}; a Terminal=true launcher "
            f"running a shell script never sends the completion, and the click silently does "
            f"nothing"
        )
        # And both halves of the measured pattern, not just the one that was
        # easier to spot: Terminal=true is what makes the output visible at all.
        assert values.get("Terminal") == "true", (
            f"{name} would run with no terminal, so its output is lost"
        )


def test_the_action_launchers_run_the_wrapper_through_bash_by_absolute_path():
    """The second difference from the launcher that works on that desktop.

    `Exec=/bin/bash /path/swapterm up` rather than `Exec=/path/swapterm up`
    removes three dependencies that are true today and guaranteed by nothing: the
    wrapper's execute bit surviving a copy, its shebang resolving under the
    desktop's PATH, and the DE choosing to exec the file rather than hand it to
    something else. A .desktop Exec= is not a shell and resolves no PATH of ours,
    so the interpreter is named absolutely.
    """
    for action, name, comment in install_desktop_icon.ACTION_ENTRIES:
        entry = install_desktop_icon.rendered_action_entry(
            action, name, comment, Path("/home/op/.local/bin/swapterm"), Path("/i.svg")
        )
        assert _directives(entry)["Exec"] == (
            f"{install_desktop_icon.BASH} /home/op/.local/bin/swapterm {action}"
        ), f"{action}: {_directives(entry)['Exec']}"
        assert install_desktop_icon.BASH.startswith("/"), (
            "the interpreter must be an absolute path; a .desktop Exec resolves no PATH"
        )


def test_the_trust_step_is_only_printed_on_a_desktop_that_needs_it():
    """Advice for a desktop you are not running is noise, and noise gets skipped.

    The installer printed four GNOME `gio set metadata::trusted` commands at an
    operator running LXQt, where pcmanfm-qt reads the execute bit and no such
    metadata exists. Rule 14's argument is exactly this: a block that does not
    apply trains the reader to skip the block that does.

    THREE CASES, and the third is the one that was missing. Saying NOTHING on an
    unknown desktop would be worse than the noise -- an operator on GNOME whose
    icons do nothing and who was told nothing has no way to find the reason -- so
    the unknown case names the symptom without asserting it applies.
    """
    desktop = Path("/home/op/Desktop")

    def prose(lines: list[str]) -> str:
        """The lines as one string with whitespace collapsed.

        BECAUSE THESE SENTENCES ARE HAND-WRAPPED and a phrase can straddle the
        break: "...this is was not" / "established. IF these icons..." joined
        with a space is "was not   established", so a plain substring match for
        "not established" fails on text that says exactly that. Collapsing
        whitespace makes the assertion about the SENTENCE rather than about where
        the author happened to break the line.
        """
        return " ".join(" ".join(lines).split())

    def runnable_gio(lines: list[str]) -> list[str]:
        """The gio commands, as opposed to prose that mentions gio.

        The LXQt case SAYS "the GNOME `gio set metadata::trusted` dance does not
        apply here" -- so a substring match on "gio set" is true of the message
        that exists to say the step is unnecessary. A command the operator is
        meant to run is an indented line that STARTS with it.
        """
        return [line.strip() for line in lines if line.strip().startswith("gio set")]

    lxqt = install_desktop_icon.trust_note("LXQt", desktop, ("up",))
    assert runnable_gio(lxqt) == [], f"GNOME commands printed on LXQt: {runnable_gio(lxqt)}"
    assert "NO trust step" in prose(lxqt), lxqt

    # Colon-separated and multi-valued, which is how Ubuntu reports it.
    gnome = install_desktop_icon.trust_note("ubuntu:GNOME", desktop, ("up",))
    assert len(runnable_gio(gnome)) == 1, f"the trust step was NOT printed on GNOME: {gnome}"
    said = prose(gnome)
    assert "Allow Launching" in said, said
    assert "not over ssh" in said, f"gio needs the session bus and this does not say so: {said}"

    unknown = install_desktop_icon.trust_note("", desktop, ("up",))
    assert len(runnable_gio(unknown)) == 1, (
        "an unknown desktop is given no command, so a GNOME user whose icons are dead is stuck"
    )
    assert "was not established" in prose(unknown), (
        f"the unknown case must not assert that the step applies (rule 17): {unknown}"
    )


def test_every_action_is_an_icon_or_explicitly_not_one():
    """THE DEFECT THIS PINS WAS SHIPPED AND THE OPERATOR READ IT.

    `rebuild` was added to swap_stack.ACTIONS in a7bc6d2. The installer kept its
    own hand-written list and printed, to the operator, on their own host:

        shell       swapterm up | down | restart | status | chains

    -- advertising a command set that was already wrong, and giving `rebuild` no
    icon. Rule 8 in operator-facing text, found by the operator reading it rather
    than by anything failing.

    So the coverage is asserted rather than the list: every action in ACTIONS is
    either an icon or named in NO_ICON with a reason. A new action can then be
    DELIBERATELY iconless, and cannot be ACCIDENTALLY iconless.

    MUTATION: add an action to swap_stack.ACTIONS and nothing else. This fails and
    names it.
    """
    iconed = {action for action, _name, _comment in install_desktop_icon.ACTION_ENTRIES}
    accounted = iconed | set(install_desktop_icon.NO_ICON)
    actions = set(swap_stack.ACTIONS)

    assert accounted == actions, (
        f"actions with no icon and no stated reason: {sorted(actions - accounted)}; "
        f"icons for actions that do not exist: {sorted(accounted - actions)}"
    )
    assert not (iconed & set(install_desktop_icon.NO_ICON)), (
        f"an action is both iconed and listed as having no icon: "
        f"{sorted(iconed & set(install_desktop_icon.NO_ICON))}"
    )


def test_the_printed_command_list_is_derived_from_the_dispatch_table():
    """Not a string in this file. That string was wrong for one commit.

    Both printed spellings -- the planned-writes label and the closing "shell"
    line -- are built from swap_stack.ACTIONS, so an action added there appears in
    both with no edit here. Asserted on the OUTPUT, because a derivation that is
    not actually printed is not a fix.
    """
    label = dict(install_desktop_icon.planned_writes(desktop_files=False))[
        install_desktop_icon.COMMAND_TARGET
    ]
    # PER LINE, NOT OVER THE BLOB, and that is a correction to this test rather
    # than a style choice. It joined closing_lines() into one string and asserted
    # each action appeared in it -- which the `shell` line alone satisfied, so a
    # THIRD hardcoded spelling on the `menu` line ("plus Up, Down and Restart",
    # still missing `rebuild` after it got an icon) was invisible to it and the
    # operator read it on their own host. A test that passes because the right
    # word appears on the wrong line is a test that passes for a reason unrelated
    # to its claim.
    lines = install_desktop_icon.closing_lines(on_path=True)
    shell_line = next(line for line in lines if line.strip().startswith("shell"))
    menu_line = next(line for line in lines if line.strip().startswith("menu"))

    for action in swap_stack.ACTIONS:
        assert action in label, f"{action} is missing from the installer's command label: {label}"
        assert action in shell_line, f"{action} is missing from the shell line: {shell_line}"

    # AND THE MENU LINE NAMES EXACTLY THE ICONS -- every one, and nothing that has
    # no icon, since telling an operator to look for a "Status" entry that is not
    # there is the same defect pointing the other way.
    for action, _name, _comment in install_desktop_icon.ACTION_ENTRIES:
        assert action.capitalize() in menu_line, (
            f"the {action} icon exists and the menu line does not name it: {menu_line}"
        )
    for action in install_desktop_icon.NO_ICON:
        assert action.capitalize() not in menu_line, (
            f"the menu line names {action}, which deliberately has NO icon: {menu_line}"
        )


def test_the_rebuild_icon_says_why_up_alone_is_not_enough():
    """An icon-only operator would otherwise serve stale code after every pull.

    The app is COPIED INTO the web image (docker/web.Dockerfile:150), so `up`
    alone keeps serving whatever was baked at the last build. Without a rebuild
    icon the launcher set reintroduces exactly the stale-artifact defect that
    `rebuild` exists to fix -- and the Comment is the only documentation a
    one-click user gets.
    """
    comments = {action: comment for action, _name, comment in install_desktop_icon.ACTION_ENTRIES}
    assert "rebuild" in comments, "there is no rebuild icon, so Up is the only option after a pull"
    said = comments["rebuild"]
    assert "git pull" in said, f"the rebuild icon does not say when to use it: {said}"
    assert "baked into the image" in said, f"nor why Up alone is not enough: {said}"
    assert "replica" in said, f"nor that it leaves the ICP replica alone: {said}"


def test_the_command_bakes_both_paths_and_leaves_no_placeholder():
    """A leftover token would be a shell script that execs the literal word.

    MUTATION: drop either .replace() in rendered_command(). The script then
    contains __SWAPTERM_PYTHON__ and fails with "No such file or directory" on a
    path the operator cannot find in any config, which is the worst kind of
    failure to debug from an icon that just closed.
    """
    text = install_desktop_icon.rendered_command("/venv/bin/python3", Path("/repo"))
    assert "__SWAPTERM_PYTHON__" not in text, "the interpreter token survived"
    assert "__SWAPTERM_REPO__" not in text, "the repo token survived"
    assert 'SWAPTERM_PYTHON="/venv/bin/python3"' in text
    assert 'SWAPTERM_REPO="/repo"' in text
    assert "__SWAPTERM" not in text, f"an unreplaced token remains: {text}"


def test_the_rendered_command_is_valid_shell():
    """Rendered, not the template -- the template is deliberately not runnable.

    `bash -n` parses without executing. A quoting mistake in a file that runs
    `docker compose stop` is worth catching here rather than on the operator's
    host, and this is the whole of what a syntax check can establish.
    """
    text = install_desktop_icon.rendered_command("/venv/bin/python3", Path("/repo"))
    done = subprocess.run(
        ["/bin/bash", "-n", "/dev/stdin"],
        input=text, capture_output=True, text=True, timeout=30,
        # check=False DELIBERATELY: a non-zero return IS the finding here, and
        # `check=True` would raise CalledProcessError instead of letting the
        # assertion below print bash's own complaint.
        check=False,
    )
    assert done.returncode == 0, f"bash refused the rendered command: {done.stderr}"


def test_the_action_icons_go_through_the_installed_command_not_their_own_python():
    """ONE WAY TO START THE STACK (rule 8).

    If an icon's Exec were `python3 swap_stack.py up` it would be a second
    spelling of how the stack starts -- with its own interpreter resolution, its
    own working directory, and no reason to stay in step with the shell command.
    Routing the icons through `swapterm` means fixing the wrapper fixes all four
    launchers.
    """
    for action, name, comment in install_desktop_icon.ACTION_ENTRIES:
        text = install_desktop_icon.rendered_action_entry(
            action, name, comment, Path("/home/op/.local/bin/swapterm"), Path("/i.svg")
        )
        assert "__SWAPTERM" not in text, f"{action}: an unreplaced token remains"
        # THROUGH BASH NOW -- see test_the_action_launchers_run_the_wrapper_through_
        # bash_by_absolute_path. The claim here is unchanged: the icon goes to the
        # installed wrapper and not to its own python.
        assert _directives(text)["Exec"].endswith(f"/home/op/.local/bin/swapterm {action}"), text
        assert "swap_stack.py" not in text, (
            f"{action}'s Exec names swap_stack.py directly, which is a second way to start "
            f"the stack and will drift from the wrapper"
        )
        assert "Terminal=true" in text, (
            f"{action} would run with no terminal, so its eight steps and any refusal go "
            f"nowhere and the icon reads as doing nothing (rule 14)"
        )


def test_every_action_icon_says_what_it_does_NOT_stop():
    """An icon carries no documentation, and "Down" reads as taking everything.

    stack_authority.py's header refuses to mark a chain daemon stoppable "ever,
    under any flag" because they hold the wallets and nothing here has their
    passphrase. The Comment is the only place a one-click user learns that, so
    the two destructive-sounding actions must say it.
    """
    comments = {action: comment for action, _name, comment in install_desktop_icon.ACTION_ENTRIES}
    for action in ("down", "restart"):
        assert "chain daemon" in comments[action], (
            f"the {action} icon does not say the chain daemons are left running: {comments[action]}"
        )
    assert "no chain daemon" in comments["up"].lower(), comments["up"]


def test_the_dry_run_names_exactly_what_the_real_run_writes():
    """Rule 8, on the two halves an operator trusts differently.

    The dry run is the ONLY thing read before this writes into a home directory,
    where a mistake is not undone by a git checkout. If planned_writes() and
    write_entries() could disagree, the file that got added to one and not the
    other would be the one nobody expected.

    Asserted against the FUNCTION's targets rather than a hand-written list, so
    adding an action to ACTION_ENTRIES cannot make this stale.
    """
    planned = [target for target, _what in install_desktop_icon.planned_writes(desktop_files=False)]
    assert install_desktop_icon.COMMAND_TARGET in planned
    assert install_desktop_icon.DESKTOP_TARGET in planned
    assert install_desktop_icon.ICON_TARGET in planned
    for action, _name, _comment in install_desktop_icon.ACTION_ENTRIES:
        assert install_desktop_icon.DESKTOP_DIR / f"swap-terminal-{action}.desktop" in planned, action
    # And the desktop-files half adds exactly the three, nothing else.
    with_desktop = [t for t, _w in install_desktop_icon.planned_writes(desktop_files=True)]
    added = [t for t in with_desktop if t not in planned]
    assert len(added) == len(install_desktop_icon.ACTION_ENTRIES), added
    assert all(t.parent == Path.home() / "Desktop" for t in added), added


def test_a_bin_dir_this_installer_just_created_is_reported_as_not_on_PATH():
    """Rule 14, from the other side: "INSTALLED" plus `command not found`.

    Ubuntu's ~/.profile adds ~/.local/bin to PATH only if the directory EXISTED
    AT LOGIN. Installing into a directory this script just made means no shell
    finds `swapterm` until the next login, and an installer that did not say so
    would be reporting success for something that does not work yet.
    """
    on_path, note = install_desktop_icon.path_note(Path("/home/op/.local/bin"), "/usr/bin:/bin")
    assert on_path is False
    assert "NOT on" in note and "log out" in note, note
    on_path, note = install_desktop_icon.path_note(
        Path("/home/op/.local/bin"), "/usr/bin:/home/op/.local/bin:/bin"
    )
    assert on_path is True, note


def test_a_space_in_either_exec_path_refuses_the_whole_install():
    """Two different paths, and both end up in an Exec= line.

    The repo launcher goes into the GUI entry's Exec and the INSTALLED COMMAND
    goes into all three action entries'. A space in either splits that line into
    the wrong argv, and .desktop quoting is its own small grammar -- so this
    refuses rather than quoting and hoping.
    """
    safe, note = install_desktop_icon.paths_are_safe_for_exec(
        "/venv/bin/python3", Path("/home/My Files/swapterm")
    )
    assert safe is False
    assert "space" in note, note


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


# --- the bind: one value, two readers, and they used to disagree --------------

def _gunicorn_bind(environment: dict) -> str:
    """gunicorn.conf.py's OWN `bind`, read by executing that file.

    Not a grep for the variable name and not a copy of its f-string. The
    2026-10-01 defect was two places computing one value, so a test that
    recomputed it here would be a third. The behavioral-verification principle
    applied to configuration: run the real file, assert on what it produces.

    S603 is not raised in tests (pyproject's per-file ignores), so this is a
    plain comment rather than a noqa claiming a check nobody asked for (rule 19):
    the argv is sys.executable plus a literal script, and the only variable is
    the environment dict under test.
    """
    done = subprocess.run(
        [sys.executable, "-c", "import runpy; print(runpy.run_path('gunicorn.conf.py')['bind'])"],
        cwd=str(Path(launcher.__file__).resolve().parent),
        env=environment, capture_output=True, text=True, timeout=60, check=False,
    )
    assert done.returncode == 0, f"could not read gunicorn.conf.py's bind:\n{done.stderr}"
    return done.stdout.strip()


def test_the_port_the_launcher_polls_is_the_port_gunicorn_binds():
    """The defect `--port` had on 2026-10-01, verified by behavior not by text.

    MEASURED on the operator's host. Another project was holding 127.0.0.1:5000,
    so the launcher was run as `--port 5057`. It printed

        waiting up to 24.8µfn (30.0s) for http://127.0.0.1:5057/api/health

    while the gunicorn it had just started printed `bind 127.0.0.1:5000`, failed
    with `[Errno 98] Address already in use` against the stranger's process, and
    died. `--port` reached the readiness poll, the browser URL and the status
    report, and never reached the server -- gunicorn.conf.py reads
    SWAP_TERMINAL_PORT from the ENVIRONMENT and nothing wrote it.

    THE ASSERTION IS gunicorn.conf.py's OWN `bind`; _gunicorn_bind() above says
    why it is read by executing that file rather than by matching its text.
    """
    port = 5057
    environment = launcher.server_environment(DB, "127.0.0.1", port)

    assert environment["SWAP_TERMINAL_PORT"] == "5057", "a string, because an environment holds strings"
    assert environment["SWAP_TERMINAL_HOST"] == "127.0.0.1"

    bind = _gunicorn_bind(environment)
    assert bind == f"127.0.0.1:{port}", (
        f"gunicorn would bind {bind!r} while the launcher polls port {port}; "
        "this is the 2026-10-01 defect"
    )


def test_the_default_port_still_agrees_after_the_fix():
    """The case that hid the defect for the whole life of the launcher.

    `--port` was broken only when it differed from gunicorn.conf.py's own
    default, so every run until somebody needed another port looked correct.
    Asserted explicitly so a future change that wired the port through ONE of
    the two readers and not the other cannot pass by matching the default.
    """
    assert _gunicorn_bind(launcher.server_environment(DB, "127.0.0.1", 5000)) == "127.0.0.1:5000"


def test_run_shim_takes_no_arguments_that_decide_the_bind():
    """Rule 9, and rule 8's reason for the cull rather than the wiring.

    run_shim() took (host, port, db_path) and read none of them; the launcher
    passed all three as argv. Wiring them up would have made run_shim a SECOND
    place deciding the bind, which is the two-copies-drift that produced the
    defect above. So the parameters are gone, and this pins that they stay gone
    -- a future `run_shim(host, port)` would be the same bug with a new spelling.
    """
    assert inspect.signature(launcher.run_shim).parameters == {}


# --- an app-mode window has no way out of it -----------------------------------


def test_window_mode_uses_the_operators_own_profile_so_extensions_exist(tmp_path):
    """MEASURED ON THE OPERATOR'S HOST 2026-10-02: "the brave browser that is opened
    has no taskbar and i can't figure out how to open it either."

    App mode has no toolbar, no address bar and no menu, so there is no way OUT of
    that window -- not to the extensions, not to a new tab, not to the URL. The
    standing advice was "open the same URL in your ordinary browser", which is an
    instruction they can only follow by leaving the window and finding the browser
    themselves. That was the thing they were asking how to do.

    --user-data-dir MUST BE ABSENT here, not merely different. A dedicated profile
    is empty, so an ordinary window pointed at one has a toolbar and still no
    extensions -- the confusing half of the fix, where the window now looks like it
    should work and the wallet buttons stay dead.
    """
    argv = launcher.browser_command(
        "/usr/bin/brave", "http://127.0.0.1:5000/", tmp_path / "profile", app_mode=False
    )

    assert argv == ["/usr/bin/brave", "--new-window", "http://127.0.0.1:5000/"]
    assert not any(part.startswith("--user-data-dir") for part in argv), (
        "a dedicated profile has no extensions, which is the whole reason --window exists"
    )
    assert not any(part.startswith("--app") for part in argv)


def test_app_mode_is_still_the_default_and_unchanged(tmp_path):
    """The 72ms teardown measurement is why, and --window does not relax it.

    A shared profile returned rc=0 in 0.072s while the first window stayed alive,
    so a launcher waiting on it tears the server down 72ms after opening the UI.
    The default keeps --app and the dedicated profile together.
    """
    argv = launcher.browser_command("/usr/bin/brave", "http://127.0.0.1:5000/", tmp_path / "profile")

    assert "--app=http://127.0.0.1:5000/" in argv
    assert f"--user-data-dir={tmp_path / 'profile'}" in argv
    assert "--new-window" not in argv


def test_the_window_flag_reaches_browser_command_as_app_mode_false(monkeypatch):
    """The flag and the parameter are inverses, and getting that backwards would
    silently ship the old behavior behind a new flag."""
    seen = {}

    def fake_launch(host, port, *, app_mode=True):
        seen["app_mode"] = app_mode
        return 0

    monkeypatch.setattr(launcher, "launch", fake_launch)
    assert launcher.main(["--window"]) == 0
    assert seen["app_mode"] is False

    seen.clear()
    assert launcher.main([]) == 0
    assert seen["app_mode"] is True, "app mode must remain the default"


def test_ctrl_c_in_window_mode_is_an_outcome_and_not_a_traceback(monkeypatch, capsys):
    """MEASURED ON THE OPERATOR'S HOST 2026-10-02, on the path --window PRINTS.

    --window tells them "Press Ctrl-C here when you are done". They did, and got

        KeyboardInterrupt
          File ".../swap_terminal_desktop.py", line 932, in _wait_for_either
            time.sleep(WAIT_TICK_SECONDS)

    above a teardown that had worked perfectly -- gunicorn logged its own clean
    shutdown three lines later. A traceback on a documented success path is rule
    14's defect in its most expensive form: it tells a reader something broke, and
    the next thing they do is go looking for what.

    It was always true of this loop and became a DEFECT when an instruction started
    pointing at it.
    """
    class NeverExits:
        returncode = None

        def poll(self):
            return None

    class Interrupts:
        """time.sleep raises, exactly as Ctrl-C makes it."""

        def __init__(self):
            self.calls = 0

        def __call__(self, _seconds):
            self.calls += 1
            raise KeyboardInterrupt

    sleeper = Interrupts()
    monkeypatch.setattr(launcher.time, "sleep", sleeper)
    shim = NeverExits()

    trigger = launcher._wait_for_either(shim, shim)
    out = capsys.readouterr().out

    assert trigger == "ctrl-c", "the interrupt must become a named trigger the teardown can report"
    assert sleeper.calls == 1
    assert "Ctrl-C" in out
    assert "not a failure" in out, (
        "the line has to say so outright: the operator had just been told to press it"
    )


def test_a_closed_window_and_a_dead_server_are_still_distinguished(monkeypatch):
    """The two outcomes that existed before, unchanged by catching the interrupt.

    A version that returned "ctrl-c" for everything would pass the test above.
    """
    class Exited:
        returncode = 0

        def poll(self):
            return 0

    class Alive:
        returncode = None

        def poll(self):
            return None

    monkeypatch.setattr(launcher.time, "sleep", lambda _s: None)
    assert launcher._wait_for_either(Exited(), Alive()) == "window-closed"
    assert launcher._wait_for_either(Alive(), Exited()) == "server-died"
