#!/usr/bin/env python3
"""Apply one mutation, run the tests, restore -- and REFUSE if the file is not committed.

Role: file (developer tool; not imported by anything on any path)
Reads: the target source file, and `git status --porcelain` for it
Writes: the target file, twice -- the mutation, then the restore via `git checkout --`
Can move funds: no. It touches no chain, no wallet, no database and no key.
Mainnet-safe: yes -- it contacts nothing.
Live-safe: yes

WHY THIS EXISTS, AND IT IS NOT A CONVENIENCE.

A mutation check is: break the code deliberately, confirm a test goes red, put it back. The
"put it back" is `git checkout -- <file>`, which restores the file to HEAD -- and that is only
a restore if HEAD is what the file looked like before. IF THE FILE HAD UNCOMMITTED WORK, HEAD
IS NOT A RESTORE POINT AND THE CHECKOUT IS A DELETE.

I did that three times in one session, 2026-09-28:

    41043f1  wiped _announce_wall_clock and the wall-clock banner, re-applied by hand
    1dfd6f4  wiped ChainOutcome.established(), setup_refusal, exit_code_for() and the XFAIL
             handler -- two files, in one pass, after mutating both

After the first I wrote the lesson into that commit message ("the idiom was right two commits
ago because the file was committed first") and then made the same mistake twice more. Resolving
to be careful is not a fix; a resolution that has failed three times is evidence about the
method, not about the effort. This is the fix, and its whole contribution is the refusal.

WHAT IT GUARANTEES. If the file is clean at HEAD, the checkout at the end restores exactly what
was there. If it is not, nothing is touched at all and the message says to commit first. There
is no --force: a flag to skip the check is a flag that gets used at 2am, and the check is the
only reason this file exists.

USAGE

    python3 tools/mutate.py <file> <old> <new> [pytest args...]

`old` must appear EXACTLY ONCE -- an anchor matching zero times means a mutation that never
applied and a "killed" verdict that proves nothing, which happened on the same day for the same
reason, and matching twice means mutating somewhere unintended.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

# THE ABSOLUTE PATH TO git, RESOLVED ONCE. ruff's S607 objects to "git" as a bare name and it is
# right to: a partial path is resolved against PATH at call time, so whatever `git` is first on
# PATH runs. Answered by resolving it rather than by a noqa (rule 19: a suppression is not a
# fix), and a missing git is a refusal here rather than a confusing failure later.
GIT = shutil.which("git")

# argv: <file> <old> <new> [pytest args...]
MINIMUM_ARGUMENTS = 3


def uncommitted(path: Path) -> str | None:
    """The porcelain status for `path`, or None when it matches HEAD.

    `git status --porcelain -- <path>` prints nothing for a clean tracked file. An UNTRACKED
    file prints `??`, and that is refused too and deliberately: `git checkout --` cannot restore
    a file git has never seen, so the "restore" would leave the mutation in place -- the same
    accident in the opposite direction, and a far quieter one.
    """
    result = subprocess.run(  # noqa: S603 -- checked: every element is either the resolved absolute path to git or a fixed literal, except `path`, which is passed after `--` so git reads it as a pathspec and never as an option. No shell is involved (shell=False is the default and is what makes the list form safe), so nothing here is interpretable as a command.
        [GIT, "status", "--porcelain", "--", str(path)],
        capture_output=True, text=True, check=True,
    )
    return result.stdout.strip() or None


# pytest's documented exit codes. ONLY 1 MEANS A TEST FAILED.
# https://docs.pytest.org/en/stable/reference/exit-codes.html
PYTEST_ALL_PASSED = 0
PYTEST_TESTS_FAILED = 1
_PYTEST_INCONCLUSIVE = {
    2: "the run was INTERRUPTED (Ctrl-C, or a collection error)",
    3: "pytest hit an INTERNAL ERROR",
    4: "the pytest command line was wrong",
    5: "NO TESTS WERE COLLECTED",
}


def verdict_for(returncode: int) -> str:
    """What a pytest exit code actually says about the mutant. Three answers, not two.

    THIS WAS A REAL FALSE POSITIVE, 2026-09-28, and it is the same defect this tool was built to
    prevent one layer up. The check was `returncode != 0` -> KILLED, so a mutation that broke the
    file's SYNTAX scored KILLED: pytest exited 2 on a collection error, having run no test at
    all. Two mutants were reported killed in a row on that basis, and neither had been executed.

    That is exactly the shape of rule 13's "skipped plus success in the same output" -- a run
    that did nothing rendering identically to a run that did work -- and of the earlier failure
    this tool already refuses, where an anchor matching zero times produced a "killed" verdict
    for a mutation that never applied. A mutation check whose failure mode is a false PASS is
    worse than no mutation check, because the conclusion drawn from it is "this is pinned".

    So only exit code 1 -- a test ran and failed -- counts as killed. Everything else says the
    experiment did not happen, and says which.
    """
    if returncode == PYTEST_TESTS_FAILED:
        return "MUTANT KILLED -- the tests caught it"
    if returncode == PYTEST_ALL_PASSED:
        return "MUTANT SURVIVED -- nothing caught this, so the tests do not pin it"
    reason = _PYTEST_INCONCLUSIVE.get(returncode, f"pytest exited {returncode}")
    return (
        f"INCONCLUSIVE -- {reason}, so NO TEST RAN AGAINST THIS MUTANT. This is not a kill. "
        f"The usual cause is a mutation that broke the file's syntax; fix the replacement text "
        f"so the file still parses, and run it again."
    )


def main(argv: list[str]) -> int:
    if GIT is None:
        print("REFUSING: no `git` on PATH. This tool's only safety property is that it restores "
              "from HEAD, and it cannot do that without git.")
        return 2
    if len(argv) < MINIMUM_ARGUMENTS:
        print(__doc__)
        return 2
    target, old, new = Path(argv[0]), argv[1], argv[2]
    pytest_args = argv[3:] or ["tests/"]

    if not target.is_file():
        print(f"REFUSING: {target} is not a file")
        return 2

    dirty = uncommitted(target)
    if dirty is not None:
        print(f"REFUSING TO MUTATE {target}: it has uncommitted changes.\n")
        print(f"    git status --porcelain -- {target}\n    {dirty}\n")
        print(
            "This tool restores with `git checkout --`, which restores the file to HEAD. HEAD is\n"
            "only a restore point if the file matches it. Mutating an uncommitted file and then\n"
            "'restoring' it DELETES the work -- three times on 2026-09-28, twice after the lesson\n"
            "had been written down.\n\n"
            "Commit the file first. Then the mutation check is safe by construction."
        )
        return 1

    source = target.read_text()
    occurrences = source.count(old)
    if occurrences != 1:
        print(f"REFUSING: the anchor appears {occurrences} times in {target}, not once.")
        print(
            "Zero means the mutation never applied, and every test that then passes 'kills' a\n"
            "mutant that was never there. More than one means mutating somewhere unintended."
        )
        return 1

    print(f"mutating {target}: {old.strip()[:70]!r} -> {new.strip()[:70]!r}")
    target.write_text(source.replace(old, new, 1))
    try:
        completed = subprocess.run(  # noqa: S603 -- checked: sys.executable is this interpreter's own absolute path, the two flags are literals, and `pytest_args` comes from the argv of a developer tool the operator runs themselves -- it is their own command line, not input from a chain, a daemon or a customer. No shell.
            [sys.executable, "-m", "pytest", "-q", *pytest_args], check=False,
        )
    finally:
        # ALWAYS, including on KeyboardInterrupt: a mutant left in the tree is worse than the
        # mutation never having run, because it looks like working code.
        subprocess.run(  # noqa: S603 -- checked: same as the status call above -- resolved git path, fixed literals, and `target` after `--` so it is a pathspec. check=True on purpose: if the RESTORE fails, this must be loud rather than leaving a mutant in the tree.
            [GIT, "checkout", "--", str(target)], check=True,
        )
        still_dirty = uncommitted(target)
        print(f"restored {target}" if still_dirty is None
              else f"WARNING: {target} is still dirty after restore: {still_dirty}")

    print(verdict_for(completed.returncode))
    return 0 if completed.returncode == PYTEST_TESTS_FAILED else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
