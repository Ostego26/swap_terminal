#!/usr/bin/env python3
"""Install the desktop icon. Prints what it would do; --apply actually writes.

Role: file (entry point, repo root per rule 10)
Reads: assets/swap-terminal.desktop, assets/swap-terminal.svg
Writes: with --apply only -- ~/.local/share/applications/swap-terminal.desktop and
      ~/.local/share/icons/hicolor/scalable/apps/swap-terminal.svg
Can send orders: no. It writes two files and runs update-desktop-database.
Mainnet-safe: yes. It starts nothing and reads no chain.

DRY RUN BY DEFAULT, for the same reason every other tool in this tree is: it writes
into the operator's home directory, outside the repo, where a mistake is not undone
by a git checkout. --apply is the opt-in.

WHY THE .desktop IS A TEMPLATE AND THIS REWRITES IT. A .desktop `Exec=` must be an
ABSOLUTE path: the desktop launches it with a minimal environment, no PATH of ours
and no working directory of ours. It also must name the right PYTHON -- the operator
runs inside a .venv, and `python3` from a desktop session is the system one, which
does not have xrpl-py or gunicorn. Both are resolved here, at install time, from
the interpreter running this script.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

# THE ACTION LIST COMES FROM swap_stack, NOT FROM A STRING HERE. This installer
# printed `swapterm up | down | restart | status | chains` to the operator on
# 2026-10-09, one commit after `rebuild` was added -- a hand-written second
# spelling of a list swap_stack.ACTIONS already owns, advertising a command set
# that was already wrong. Rule 8, in operator-facing text, found by the operator
# reading it.
#
# A 0.42s import, measured, and it is a root script importing a root script --
# the same world, the same interpreter, already on sys.path.
import swap_stack

REPO_ROOT = Path(__file__).resolve().parent
TEMPLATE = REPO_ROOT / "assets" / "swap-terminal.desktop"
ACTION_TEMPLATE = REPO_ROOT / "assets" / "swap-terminal-action.desktop"
COMMAND_TEMPLATE = REPO_ROOT / "assets" / "swapterm"
ICON_SOURCE = REPO_ROOT / "assets" / "swap-terminal.svg"
LAUNCHER = REPO_ROOT / "swap_terminal_desktop.py"
STACK = REPO_ROOT / "swap_stack.py"

DESKTOP_DIR = Path.home() / ".local" / "share" / "applications"
ICON_DIR = Path.home() / ".local" / "share" / "icons" / "hicolor" / "scalable" / "apps"
BIN_DIR = Path.home() / ".local" / "bin"
DESKTOP_TARGET = DESKTOP_DIR / "swap-terminal.desktop"
ICON_TARGET = ICON_DIR / "swap-terminal.svg"
COMMAND_TARGET = BIN_DIR / "swapterm"

#: The three actions that get their own icon, and the sentence each one promises.
#:
#: NOT EVERY ACTION IN swap_stack.ACTIONS, and the omission is the decision on
#: this line. `status` and `chains` are read-only reports whose whole value is
#: the text, and a desktop icon that opens a terminal, prints a report and exits
#: gives the operator no time to read it -- the window closes with the process.
#: Those two belong at a shell prompt, which is what `swapterm` is for. `up`,
#: `down` and `restart` are the ones worth one click.
#:
#: Each Comment says what the action DOES NOT do as well as what it does, because
#: an icon carries no other documentation and "Down" could reasonably be read as
#: taking the wallet daemons with it. It does not, ever (stack_authority.py's
#: header refuses to mark them stoppable "under any flag").
#: Actions that deliberately get NO icon, and why. Named rather than left as an
#: absence, so test_every_action_is_an_icon_or_explicitly_not() can assert that
#: ACTION_ENTRIES plus this covers swap_stack.ACTIONS exactly -- which means a new
#: action cannot quietly fail to get an icon the way `rebuild` just did.
#:
#: Both are read-only reports whose whole value is the text, and an icon that
#: opens a terminal, prints a report and exits gives the operator no time to read
#: it: the window closes with the process. They belong at a shell prompt, which is
#: what `swapterm` is for.
NO_ICON: tuple[str, ...] = ("status", "chains")

ACTION_ENTRIES: tuple[tuple[str, str, str], ...] = (
    (
        "up",
        "Swap Terminal: Up",
        "Start the containers and workers, then report what is reachable. Starts no chain daemon.",
    ),
    (
        "down",
        "Swap Terminal: Down",
        "Stop this terminal's workers and containers and PROVE every port is free. "
        "Leaves the chain daemons running -- they hold the wallets.",
    ),
    (
        "restart",
        "Swap Terminal: Restart",
        "Down, prove the stop, then Up. Refuses to start if the stop cannot be proven. "
        "Leaves the chain daemons running.",
    ),
    # REBUILD HAS AN ICON BECAUSE WITHOUT ONE THE ICON SET HAS A TRAP. The
    # application is COPIED INTO the web image (docker/web.Dockerfile:150), so
    # after a `git pull` an operator who only ever clicks Up serves the old code
    # with nothing on screen saying so -- the same stale-artifact defect
    # `rebuild` exists to fix, reintroduced by the launcher set rather than by
    # the code. It takes minutes on a cold layer cache, which Terminal=true makes
    # visible rather than mysterious.
    (
        "rebuild",
        "Swap Terminal: Rebuild",
        "Rebuild the web image from this checkout, then Down, prove, Up. Run this after a "
        "git pull -- the app is baked into the image, so Up alone serves the old code. "
        "Never rebuilds the ICP replica, and leaves the chain daemons running.",
    ),
)


def rendered_desktop_entry(python: str, launcher: Path, icon: Path) -> str:
    """The .desktop text with the placeholders resolved. Pure, so it is testable.

    The Exec line quotes nothing and needs no quoting: both paths are absolute and
    this repo's own, and .desktop Exec parsing has its own quoting rules that are
    easy to get subtly wrong. If either path ever contains a space this must switch
    to the spec's quoting rather than hoping -- which is why the caller checks.
    """
    text = TEMPLATE.read_text()
    return (
        text.replace("__SWAP_TERMINAL_EXEC__", f"{python} {launcher}")
        .replace("__SWAP_TERMINAL_ICON__", str(icon))
    )


def rendered_action_entry(action: str, name: str, comment: str, command: Path, icon: Path) -> str:
    """One action's .desktop text. Pure, so it is testable without writing anything.

    Exec is the INSTALLED `swapterm`, not `python3 swap_stack.py <action>`, and
    that is deliberate: the wrapper already resolves the interpreter and the
    working directory, so routing the icons through it means the icon and the
    shell command cannot diverge about how the stack is started (rule 8). Fix the
    wrapper once and all four launchers follow.
    """
    return (
        ACTION_TEMPLATE.read_text()
        .replace("__SWAPTERM_NAME__", name)
        .replace("__SWAPTERM_COMMENT__", comment)
        .replace("__SWAPTERM_EXEC__", f"{command} {action}")
        .replace("__SWAPTERM_ICON__", str(icon))
        .replace("__SWAPTERM_ACTION__", action)
    )


def rendered_command(python: str, repo: Path) -> str:
    """The `swapterm` script with its two paths baked in. Pure.

    BAKED RATHER THAN LOOKED UP, for the reason the .desktop Exec is: `python3`
    on a desktop session's PATH -- or in a cron environment, or in any other
    directory -- is the SYSTEM python, which has neither xrpl-py nor gunicorn.
    A wrapper that found its own interpreter would find the wrong one exactly
    when it matters and work perfectly when tested from inside the venv.
    """
    return (
        COMMAND_TEMPLATE.read_text()
        .replace("__SWAPTERM_PYTHON__", python)
        .replace("__SWAPTERM_REPO__", str(repo))
    )


def path_note(bin_dir: Path, path_value: str) -> tuple[bool, str]:
    """Is `bin_dir` on PATH? (yes/no, the sentence to print). Pure.

    ASKED AND ANSWERED RATHER THAN ASSUMED. ~/.local/bin is added to PATH by
    Ubuntu's ~/.profile ONLY IF IT EXISTS AT LOGIN -- so installing the command
    into a directory this script just created means the current shell, and every
    shell until the next login, will not find it. An installer that printed
    "INSTALLED" and left `swapterm: command not found` behind would be rule 14's
    did-nothing-looking-like-did-work, from the other side.
    """
    entries = [Path(part) for part in path_value.split(":") if part]
    if bin_dir in entries:
        return True, f"{bin_dir} is on PATH, so `swapterm` works in a new shell immediately"
    return False, (
        f"{bin_dir} is NOT on this shell's PATH. Ubuntu's ~/.profile adds it only if the "
        f"directory existed at login, and this installer may have just created it -- so log "
        f"out and back in, or run `export PATH=\"$HOME/.local/bin:$PATH\"` for this shell"
    )


def paths_are_safe_for_exec(python: str, launcher: Path) -> tuple[bool, str]:
    """A space in either path would silently split the Exec line into wrong argv.

    Checked rather than assumed, and refused rather than quoted-and-hoped: .desktop
    Exec quoting is its own small grammar, and a launcher that starts with the wrong
    argv is a launcher that fails in a way the desktop reports as nothing at all.
    """
    for label, value in (("the python interpreter", python), ("the launcher", str(launcher))):
        if " " in value:
            return False, (
                f"{label} path contains a space ({value!r}). A .desktop Exec= line splits on spaces, "
                f"so this would start the wrong command. Move the repo (or the venv) somewhere "
                f"without spaces, or quote it per the Desktop Entry Specification by hand."
            )
    return True, "both paths are space-free, so the Exec line needs no quoting"


def refusals(python: str) -> list[str]:
    """Every reason this must not write anything, as sentences. [] means go ahead.

    EXTRACTED FROM main() 2026-10-09, when adding the `swapterm` command and the
    three action icons put it at C901 17 > 10, PLR0912 19 > 12 and PLR0915 80 > 50.
    Rule 12's answer to those three complaints is the same one: "A main() past the
    ceiling is orchestration that has swallowed decisions... The fix is to extract
    the decision so it can be called with seeded inputs, not to raise the ceiling."

    These ARE the decisions -- a missing template and a space in a path are the two
    ways this refuses -- and collecting them returns every reason at once rather
    than making the operator re-run to discover the second one.
    """
    reasons = [
        f"{required} is missing"
        for required in (TEMPLATE, ACTION_TEMPLATE, COMMAND_TEMPLATE, ICON_SOURCE, LAUNCHER, STACK)
        if not required.exists()
    ]
    # BOTH PATHS, and they are different paths: the repo's launcher goes into the
    # GUI entry's Exec, and COMMAND_TARGET (under $HOME) goes into all three action
    # entries'. A space in either splits an Exec line into the wrong argv.
    for label, target in (("the repo launcher", LAUNCHER), ("the installed command", COMMAND_TARGET)):
        safe, note = paths_are_safe_for_exec(python, target)
        if not safe:
            reasons.append(f"{label}: {note}")
    return reasons


def planned_writes(desktop_files: bool) -> list[tuple[Path, str]]:
    """(path, what it is) for everything this would write, in write order. Pure.

    SO THE DRY RUN AND THE REAL RUN CANNOT DISAGREE (rule 8). They listed the
    targets separately before this existed, which is the shape where a file gets
    added to one and not the other -- and the dry run is the only thing an
    operator reads before letting it write into their home directory.
    """
    writes = [
        (COMMAND_TARGET, f"`swapterm {'|'.join(swap_stack.ACTIONS)}`"),
        (DESKTOP_TARGET, "menu entry: the window"),
    ]
    writes += [
        (DESKTOP_DIR / f"swap-terminal-{action}.desktop", f"menu entry: {action}")
        for action, _name, _comment in ACTION_ENTRIES
    ]
    writes.append((ICON_TARGET, "the icon"))
    if desktop_files:
        writes += [
            (Path.home() / "Desktop" / f"swap-terminal-{action}.desktop", f"desktop icon: {action}")
            for action, _name, _comment in ACTION_ENTRIES
        ]
    return writes


def write_entries(entry: str, actions: dict[str, str], command: str) -> list[str]:
    """Write the command, the menu entries and the icon. Returns what it wrote."""
    BIN_DIR.mkdir(parents=True, exist_ok=True)
    DESKTOP_DIR.mkdir(parents=True, exist_ok=True)
    ICON_DIR.mkdir(parents=True, exist_ok=True)

    COMMAND_TARGET.write_text(command)
    # 0o755 AND NOT 0o777: it is the operator's own command in their own bin, and
    # group-writable would let anything in their groups rewrite what they run.
    COMMAND_TARGET.chmod(0o755)
    written = [str(COMMAND_TARGET)]

    DESKTOP_TARGET.write_text(entry)
    DESKTOP_TARGET.chmod(0o755)
    written.append(str(DESKTOP_TARGET))

    for action, text in actions.items():
        target = DESKTOP_DIR / f"swap-terminal-{action}.desktop"
        target.write_text(text)
        target.chmod(0o755)
        written.append(str(target))

    shutil.copy2(ICON_SOURCE, ICON_TARGET)
    written.append(str(ICON_TARGET))
    return written


def write_desktop_shortcuts(actions: dict[str, str]) -> list[str]:
    """Drop the action launchers on the desktop itself. Returns lines to print.

    GNOME'S TRUST STEP IS SAID, NOT ATTEMPTED. A .desktop sitting on the desktop
    does nothing until it is marked trusted, and `gio` is the only thing that sets
    that metadata -- but it needs the live session bus, which this script may not
    be inside (a terminal over ssh is not). Running it and swallowing the failure
    would leave an icon that does nothing, with no explanation, which is exactly
    rule 14's "did nothing must not look like did work".
    """
    desktop = Path.home() / "Desktop"
    if not desktop.is_dir():
        return [
            f"  SKIPPED     {desktop} does not exist, so nothing was put on the desktop.",
            "              The menu entries are installed either way.",
        ]
    lines = []
    for action, text in actions.items():
        target = desktop / f"swap-terminal-{action}.desktop"
        target.write_text(text)
        target.chmod(0o755)
        lines.append(f"  wrote {target}")
    lines += [
        "",
        "  GNOME ONLY: a .desktop on the desktop does nothing until it is trusted.",
        '  Either right-click it and choose "Allow Launching", or run this INSIDE your',
        "  desktop session (not over ssh):",
    ]
    lines += [
        f'      gio set "{desktop / f"swap-terminal-{action}.desktop"}" metadata::trusted true'
        for action in actions
    ]
    return lines


def closing_lines(on_path: bool) -> list[str]:
    """What to say once it is installed. Pure, so the wording is testable.

    THE LAST THREE LINES ARE NOT DECORATION. An icon carries no documentation,
    and "Down" could reasonably be read as taking the wallet daemons with it.
    Saying what is NOT stopped, next to the thing that stops, is rule 14's
    "state what the number means, next to the number" applied to a verb.
    """
    lines = [
        "",
        "INSTALLED.",
        '  menu        "Swap Terminal" (the window), plus Up, Down and Restart',
        f"  shell       swapterm {' | '.join(swap_stack.ACTIONS)}",
    ]
    if not on_path:
        lines.append(
            f"  FIRST       {BIN_DIR} is not on this shell's PATH yet -- see the PATH line "
            f"above. `swapterm` will not be found until that is fixed"
        )
    lines += [
        "",
        "  `restart` stops everything this terminal owns, PROVES the stop, and only then",
        "  starts. If the stop cannot be proven it starts NOTHING and says what survived.",
        "  No action here ever stops bitcoind, litecoind or gridcoinresearchd: they hold",
        "  the wallets, nothing here has their passphrase, and a stop this cannot undo is",
        "  not one to put behind an icon.",
    ]
    return lines


def print_plan(entry: str, actions: dict[str, str], *, desktop_files: bool) -> None:
    """The dry run: every target and every Exec line, before anything is written.

    ITS OWN FUNCTION BECAUSE main() WAS STILL OVER C901 AFTER THE FIRST EXTRACTION
    (12 > 10), and rule 12 does not offer a third option. It is also the half an
    operator actually reads -- this writes into their home directory, where a
    mistake is not undone by a git checkout -- so it is worth being able to test
    that the plan names what apply_all() writes.
    """
    print(flush=True)
    for target, what in planned_writes(desktop_files):
        print(f"  would write {target}   <- {what}", flush=True)
    if not desktop_files:
        print(f"  not writing {Path.home() / 'Desktop'}/*.desktop   <- pass --desktop-files for "
              f"icons ON the desktop as well as in the menu", flush=True)
    print("\n  the Exec lines will be:", flush=True)
    for text in (entry, *actions.values()):
        exec_line = next((line for line in text.splitlines() if line.startswith("Exec=")), "")
        print(f"      {exec_line or '(no Exec= line -- a template is wrong)'}", flush=True)


def apply_all(
    entry: str, actions: dict[str, str], command: str, *, desktop_files: bool, on_path: bool
) -> None:
    """Write everything and report it. The other half main() was too long for."""
    print(flush=True)
    for written in write_entries(entry, actions, command):
        print(f"  wrote {written}", flush=True)
    if desktop_files:
        for line in write_desktop_shortcuts(actions):
            print(line, flush=True)

    updater = shutil.which("update-desktop-database")
    if updater:
        subprocess.run([updater, str(DESKTOP_DIR)], check=False, timeout=30)  # noqa: S603 -- checked: an absolute path resolved from PATH, a fixed argv, no shell, no outside input
        print("  ran update-desktop-database", flush=True)
    else:
        print("  update-desktop-database is not installed; the menu may need a log out and back "
              "in before it lists these", flush=True)

    for line in closing_lines(on_path):
        print(line, flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Install the Swap Terminal desktop icons and the `swapterm` command."
    )
    parser.add_argument("--apply", action="store_true",
                        help="actually write the files. Without it, this prints what it would do.")
    parser.add_argument("--desktop-files", action="store_true",
                        help=f"also drop the action launchers in {Path.home() / 'Desktop'} as "
                             f"clickable icons, not only in the application menu")
    args = parser.parse_args()

    print("swap terminal launcher installer", flush=True)
    print(f"  repo        {REPO_ROOT}", flush=True)
    print(f"  python      {sys.executable}  <- baked into every Exec=, because a desktop "
          f"session's python3 is NOT this venv", flush=True)

    refused = refusals(sys.executable)
    for reason in refused:
        print(f"  REFUSED     {reason}", flush=True)
    if refused:
        print("\nNothing was written.", file=sys.stderr)
        return 1

    on_path, where = path_note(BIN_DIR, os.environ.get("PATH", ""))
    print(f"  PATH        {where}", flush=True)

    entry = rendered_desktop_entry(sys.executable, LAUNCHER, ICON_TARGET)
    actions = {
        action: rendered_action_entry(action, name, comment, COMMAND_TARGET, ICON_TARGET)
        for action, name, comment in ACTION_ENTRIES
    }
    command = rendered_command(sys.executable, REPO_ROOT)

    print_plan(entry, actions, desktop_files=args.desktop_files)
    if not args.apply:
        print("\nDRY RUN -- nothing was written. Re-run with --apply.", flush=True)
        return 0
    apply_all(entry, actions, command, desktop_files=args.desktop_files, on_path=on_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
