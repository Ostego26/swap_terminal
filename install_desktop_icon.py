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
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
TEMPLATE = REPO_ROOT / "assets" / "swap-terminal.desktop"
ICON_SOURCE = REPO_ROOT / "assets" / "swap-terminal.svg"
LAUNCHER = REPO_ROOT / "swap_terminal_desktop.py"

DESKTOP_DIR = Path.home() / ".local" / "share" / "applications"
ICON_DIR = Path.home() / ".local" / "share" / "icons" / "hicolor" / "scalable" / "apps"
DESKTOP_TARGET = DESKTOP_DIR / "swap-terminal.desktop"
ICON_TARGET = ICON_DIR / "swap-terminal.svg"


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


def main() -> int:
    parser = argparse.ArgumentParser(description="Install the Swap Terminal desktop icon.")
    parser.add_argument("--apply", action="store_true",
                        help="actually write the files. Without it, this prints what it would do.")
    args = parser.parse_args()

    print("swap terminal desktop icon installer", flush=True)
    print(f"  repo        {REPO_ROOT}", flush=True)
    print(f"  python      {sys.executable}  <- baked into Exec=, because a desktop session's "
          f"python3 is NOT this venv", flush=True)

    for required in (TEMPLATE, ICON_SOURCE, LAUNCHER):
        if not required.exists():
            print(f"\nREFUSED: {required} is missing. Nothing was written.", file=sys.stderr)
            return 1

    safe, note = paths_are_safe_for_exec(sys.executable, LAUNCHER)
    print(f"  exec paths  {note}", flush=True)
    if not safe:
        print("\nREFUSED. Nothing was written.", file=sys.stderr)
        return 1

    entry = rendered_desktop_entry(sys.executable, LAUNCHER, ICON_TARGET)
    print(f"\n  would write {DESKTOP_TARGET}", flush=True)
    print(f"  would write {ICON_TARGET}", flush=True)
    print("\n  the Exec line will be:", flush=True)
    exec_line = next((line for line in entry.splitlines() if line.startswith("Exec=")), "")
    print(f"      {exec_line or '(no Exec= line -- the template is wrong)'}", flush=True)

    if not args.apply:
        print("\nDRY RUN -- nothing was written. Re-run with --apply.", flush=True)
        return 0

    DESKTOP_DIR.mkdir(parents=True, exist_ok=True)
    ICON_DIR.mkdir(parents=True, exist_ok=True)
    DESKTOP_TARGET.write_text(entry)
    DESKTOP_TARGET.chmod(0o755)
    shutil.copy2(ICON_SOURCE, ICON_TARGET)
    print(f"\n  wrote {DESKTOP_TARGET}", flush=True)
    print(f"  wrote {ICON_TARGET}", flush=True)

    updater = shutil.which("update-desktop-database")
    if updater:
        subprocess.run([updater, str(DESKTOP_DIR)], check=False, timeout=30)  # noqa: S603 -- checked: an absolute path resolved from PATH, a fixed argv, no shell, no outside input
        print("  ran update-desktop-database", flush=True)
    else:
        print("  update-desktop-database is not installed; the icon may need a log out and back in "
              "before the desktop lists it", flush=True)

    print("\nINSTALLED. Look for \"Swap Terminal\" in your application menu.", flush=True)
    print("  Closing its window stops the server. Check with:", flush=True)
    print(f"      python3 {LAUNCHER} --status", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
