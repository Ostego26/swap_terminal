"""No command this repository hands an operator runs host `dfx`. HANDOFF.md section 1.

Role: test (reads tracked files as text; runs nothing, opens no socket)
Reads: every .md, .yml, .sh and .py in the tree outside node_modules
Writes: nothing
Can move funds: no
Mainnet-safe: yes

WHY THIS EXISTS, AND IT IS THE WORST THING THIS PROJECT HAS DONE TO ITSELF.

HANDOFF.md section 1, measured on the operator's host 2026-10-07:

    $ dfx canister id icp_ledger_canister     # run in icp/
    Error: failed to ensure cohesive network directory
    Caused by: failed to remove directory .../icp/.dfx/local and its contents
    Caused by: Permission denied (os error 13)

`icp/.dfx/local` is root-owned because the replica runs in Docker. Host dfx saw a
network directory it had not created, judged it incohesive, and MOVED TO DELETE
IT. Only the permission error stopped it. That directory is the local replica's
state, including the ledger holding the desk's LICP balance.

The session that caused it wrote: "I handed the operator a block that used host
dfx in `icp/`. That was wrong and it is the single worst thing I did this
session." The remedy it recorded was a sentence in a handoff document.

A SENTENCE IN A DOCUMENT DID NOT HOLD. Measured 2026-10-10, three days later,
four more bare `dfx canister call` invocations were sitting in commands meant to
be pasted:

    OPEN_FINDINGS.md:342   the finding-19 check, twice
    OPEN_FINDINGS.md:344
    docker-compose.yml:188  the hazard-1 check, twice
    docker-compose.yml:190

All four were written AFTER the incident, by sessions that had the handoff in
front of them. That is rule 19's test applied to a safety rule: the sentence
stopped the symptom being reported and did not stop the cause existing. This
file is the cause-stopper.

THE CORRECT TRANSPORTS, and there are exactly two (chains/icp.py around line 360
holds the same split in code):

    docker compose exec -T icp-replica dfx ...   when the replica is in Docker
    dfx --network <url> ...                      when it is reachable by URL

`icp-replica` is the COMPOSE SERVICE name. The container is `swap-icp-replica`
and `docker compose exec` rejects it -- that distinction is written at the call
site in chains/icp.py because it was got wrong once.

HOW THIS AVOIDS BEING THE SIXTH PROSE-READING DETECTOR.

This tree has produced five detectors that matched their own documentation
instead of code (HANDOFF.md section 6 counts four; the fifth was the Python 3.13
docstring strip in tests/test_icp_custody_addresses.py). The trap is always the
same: a scan for a string finds the string in a sentence ABOUT the string.

So this does not search for `dfx` anywhere. It asks whether a line INVOKES dfx,
by stripping leading whitespace and any comment marker and then requiring the
remainder to START with `dfx `. A mention inside backticks, or mid-sentence, or
after `docker compose exec`, does not start a line and is not an invocation.
`test_the_detector_sees_an_invocation_and_ignores_a_mention` pins both
directions, so a detector that silently matched nothing could not pass.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

#: Extensions that can carry a command an operator is meant to paste.
SCANNED_SUFFIXES = (".md", ".yml", ".yaml", ".sh", ".py")

#: The dfx subcommands that TOUCH replica state, which is what makes host dfx
#: dangerous. `dfx --version` and `dfx help` do not open a network directory.
#:
#: `start` is absent deliberately: docker/icp-replica.Dockerfile's CMD is
#: `dfx start`, and that is dfx running INSIDE the container, which is the whole
#: point. A Dockerfile CMD is not a command handed to an operator's shell.
STATEFUL_SUBCOMMANDS = ("canister", "deploy", "identity", "ledger", "wallet")

#: Leading noise to strip before asking "does this line start with dfx". Comment
#: markers for YAML/shell (`#`), Python's doc-comment idiom (`#:`) and Markdown
#: blockquotes (`>`), plus the `$` of a transcript prompt.
_LEADING = re.compile(r"^[\s>]*(?:#:?|//)?\s*(?:\$\s+)?")


def invokes_host_dfx(line: str) -> bool:
    """True if `line` starts a dfx command with no compose exec and no --network.

    THE DECISION, as a pure function (rule 10), so the test below can call it with
    seeded strings rather than only with whatever happens to be in the tree.
    """
    stripped = _LEADING.sub("", line, count=1)
    if not stripped.startswith("dfx "):
        return False
    # `dfx --network <url> ...` is the second legitimate transport.
    if "--network" in stripped:
        return False
    rest = stripped[len("dfx ") :].lstrip()
    # `dfx --version`, `dfx help`: no subcommand that opens a network directory.
    return rest.split(" ", 1)[0].split("=", 1)[0] in STATEFUL_SUBCOMMANDS


#: A line that is the FIRST line of command output reporting a failure.
#:
#: THE ONE EXCEPTION, AND IT IS THIS NARROW ON PURPOSE. HANDOFF.md section 1
#: quotes the incident verbatim -- the host-dfx command, then dfx's own
#: "Error: failed to ensure cohesive network directory". That command must stay
#: exactly as it was typed or the evidence is falsified: a transcript rewritten
#: to use the safe transport would be a record of something that never happened,
#: and the whole point of section 1 is that THIS command did THAT.
#:
#: It cannot be used to silence a live instruction, because an instruction is not
#: followed by its own error message. Spraying this marker means fabricating a
#: failure transcript, which is a different and much louder thing to do than
#: adding a `noqa` (CLAUDE.md rule 19: never add a suppression to make a check
#: pass -- this is a rule about what the thing IS, not a waiver).
_FAILURE_OUTPUT = re.compile(r"^[\s>]*(?:#:?|//)?\s*(?:Error|error|Caused by):")


def offenders_in(text: str) -> list[tuple[int, str]]:
    """Every (line number, line) in `text` that invokes host dfx.

    Takes the whole text rather than a line because the failure-transcript
    exception needs ONE line of lookahead, and that lookahead is the only reason
    this is not a pure per-line decision.
    """
    lines = text.splitlines()
    found = []
    for index, line in enumerate(lines):
        if not invokes_host_dfx(line):
            continue
        following = lines[index + 1] if index + 1 < len(lines) else ""
        if _FAILURE_OUTPUT.match(following):
            continue
        found.append((index + 1, line))
    return found


def scanned_files() -> list[Path]:
    files = []
    for suffix in SCANNED_SUFFIXES:
        for path in ROOT.rglob(f"*{suffix}"):
            parts = set(path.parts)
            if parts & {"node_modules", ".git", "target", "__pycache__", ".venv"}:
                continue
            files.append(path)
    return sorted(files)


def test_no_documented_command_invokes_host_dfx():
    """MUTATION: put `dfx canister call foo bar` on its own line in any .md or .yml.

    This is the assertion the file exists for. Every dfx command this repository
    prints, documents or pastes must go through `docker compose exec` or carry
    `--network`, because host dfx with icp/dfx.json in scope moves to delete the
    replica's state directory.
    """
    offenders = []
    for path in scanned_files():
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:  # pragma: no cover -- an unreadable file is not a finding
            continue
        for number, line in offenders_in(text):
            offenders.append(f"{path.relative_to(ROOT)}:{number}  {line.strip()[:110]}")

    assert not offenders, (
        "these lines invoke host dfx, which HANDOFF.md section 1 measured moving to "
        "DELETE icp/.dfx/local -- the replica's state, ledger included. Route each "
        "through `docker compose exec -T icp-replica dfx ...` (icp-replica is the "
        "SERVICE name) or give it `--network <url>`:\n  " + "\n  ".join(offenders)
    )


def test_the_scan_actually_reads_the_files_that_carry_these_commands():
    """A detector that silently reads nothing passes every assertion it makes.

    THE FAILURE THIS GUARDS IS THIS FILE'S OWN. If `scanned_files` excluded too
    much -- a suffix dropped, a directory filter widened -- the test above would
    pass by reading an empty list. These two files are the ones that carried the
    2026-10-10 offenders, so both must be in scope.
    """
    scanned = {path.relative_to(ROOT).as_posix() for path in scanned_files()}
    assert "OPEN_FINDINGS.md" in scanned
    assert "docker-compose.yml" in scanned
    assert len(scanned) > 50, f"the scan found only {len(scanned)} files, which is too few"


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        # INVOCATIONS -- the thing that must never ship.
        ("dfx canister call threshold_custody public_key '(record {})'", True),
        ("    dfx canister id icp_ledger_canister", True),
        ("#     dfx canister call threshold_custody config", True),
        ("$ dfx deploy icp_ledger_canister", True),
        ("> dfx identity whoami", True),
        ("dfx ledger balance", True),
        # THE TWO LEGITIMATE TRANSPORTS.
        ("docker compose exec -T icp-replica dfx canister call threshold_custody config", False),
        ("dfx --network http://127.0.0.1:4943 canister call threshold_custody config", False),
        # MENTIONS -- prose about the command, which is documentation, not a command.
        ("#: What `dfx canister call` prints for a nat: digits with underscores", False),
        ("# Never run host `dfx canister call` inside icp/ -- it deletes state", False),
        ("the remedy is to stop using dfx canister call from the host", False),
        # SUBCOMMANDS THAT OPEN NO NETWORK DIRECTORY.
        ("dfx --version", False),
        ("dfx help", False),
        # THE CONTAINER'S OWN CMD, which is dfx running INSIDE the replica.
        ('CMD ["dfx", "start", "--host", "0.0.0.0:4943"]', False),
    ],
)
def test_the_detector_sees_an_invocation_and_ignores_a_mention(line, expected):
    """Both directions pinned, because only one of them is cheap to get right.

    A detector that flags every line containing "dfx" would flag this file, the
    handoff, and every comment explaining the hazard -- and the way that resolves
    is somebody deleting the test. The mention cases below are the ones that make
    it survivable.
    """
    assert invokes_host_dfx(line) is expected


def test_a_failure_transcript_is_evidence_and_is_left_verbatim():
    """HANDOFF.md section 1 must keep the command that caused the incident.

    Rewriting it to the safe transport would make it a record of something that
    never happened, and section 1's entire claim is that THIS command produced
    THAT error.
    """
    transcript = (
        "$ dfx canister id icp_ledger_canister     # run in icp/\n"
        "Error: failed to ensure cohesive network directory\n"
    )
    assert offenders_in(transcript) == []


def test_the_exception_cannot_hide_a_live_instruction():
    """MUTATION: this is the loophole check, and it must stay shut.

    The exception keys on the NEXT line being command output reporting a failure.
    An instruction is not followed by its own error message, so a dfx line
    followed by anything else is still an offense -- including a dfx line
    followed by prose that merely mentions an error.
    """
    instruction = (
        "dfx canister call threshold_custody config\n"
        "Same hex means the key is on the volume.\n"
    )
    assert len(offenders_in(instruction)) == 1

    # "error" in prose rather than as the start of output does not qualify.
    near_miss = (
        "dfx canister call threshold_custody config\n"
        "If this errors, the replica is down.\n"
    )
    assert len(offenders_in(near_miss)) == 1

    # A dfx line as the very last line has no lookahead and is still an offense.
    assert len(offenders_in("dfx canister call threshold_custody config")) == 1
