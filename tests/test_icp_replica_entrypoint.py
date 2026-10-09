"""Does the replica's entrypoint find the pid file that stopped dfx from starting?

Role: tests (read-only against a fake tree)
Reads: docker/icp_replica_entrypoint.py, and a directory pytest built
Writes: files under tmp_path, which the deletion test then removes
Can move funds: no. No container, no dfx, no replica, no network.
Mainnet-safe: yes

WHY THESE EXIST. On 2026-10-07 swap_stack.py's `down` stopped running `docker compose
down` -- which removes containers, and the replica's canister state did not survive its
container; the ledger holding 1000 LICP went with it. The fix was right and broke the
restart immediately:

    Running dfx start for version 0.24.3
    Using the default configuration for the local shared network.
    Error: dfx is already running.

Sixty refused probes, against about eleven seconds for a fresh container. Remove loses
state; stop cannot start dfx. The entrypoint deletes the stale pid file so both work.

THE DELETION IS SAFE BY CONSTRUCTION AND THAT IS WHY IT IS NOT TESTED HERE: an
entrypoint runs in a container that has just started and holds no other process, so a
pid file present then cannot name a live dfx. What IS testable, and is the part that
can be wrong, is WHICH PATHS COUNT -- and that is a decision, which rule 10 puts in a
function a test can call against a tree it built.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

_SOURCE = Path(__file__).resolve().parents[1] / "docker" / "icp_replica_entrypoint.py"
_SPEC = importlib.util.spec_from_file_location("icp_replica_entrypoint", _SOURCE)
# BOTH FAILURES OF THE LOAD ARE NAMED, and this one runs at COLLECTION time.
# spec_from_file_location() returns None when the path does not exist or no
# loader claims it, and a spec can carry no loader -- so moving or renaming
# docker/icp_replica_entrypoint.py made every test in this file fail with
# `AttributeError: 'NoneType' object has no attribute 'loader'` during
# collection, naming neither the file nor the reason (pyright
# reportArgumentType + reportOptionalMemberAccess x2, 2026-10-09). The file
# lives in the Docker image's build context, so "it moved" is the realistic
# cause. Same guard, same words, as tests/test_operator_panel.py::_entry and
# tests/test_solana_payout.py's teller_entry().
if _SPEC is None or _SPEC.loader is None:
    raise ImportError(
        f"could not load {_SOURCE} as a module: spec_from_file_location gave spec={_SPEC!r}. "
        f"This file tests the replica container's entrypoint by location, so if that script "
        f"moved this path moves with it."
    )
entrypoint = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(entrypoint)


def test_it_finds_the_pid_file_at_any_depth(tmp_path):
    """dfx nests it under network/local, and the exact depth is not something to claim.

    rglob rather than a fixed path because the layout under .../dfx/network/local has
    changed between dfx versions and this image pins 0.24.3 only until somebody bumps
    it. A fixed path that goes stale fails silently -- the container starts, dfx
    refuses, and the probe loop reports sixty refusals with no cause.
    """
    root = tmp_path
    deep = root / ".local" / "share" / "dfx" / "network" / "local"
    deep.mkdir(parents=True)
    (deep / "pid").write_text("12345")
    (deep / "replica.pid").write_text("12346")
    (root / "pid").write_text("12347")

    found = entrypoint.stale_pid_files(root)
    names = sorted(path.name for path in found)
    assert names == ["pid", "pid", "replica.pid"], found
    assert deep / "pid" in found, "the nested one is the one that blocks dfx"


def test_a_root_that_does_not_exist_is_not_an_error():
    """First run: the volume is empty and /repo/.dfx may not exist at all.

    Raising here would make an ordinary first start fail, which is the opposite of what
    this script is for.
    """
    assert entrypoint.stale_pid_files(Path("/definitely/not/here")) == []


def test_a_DIRECTORY_named_pid_is_not_a_pid_file(tmp_path):
    """is_file(), because deleting a directory named `pid` would fail and say nothing useful.

    Not hypothetical in shape: dfx's own layout has directories beside these files, and
    a finder that returned one would make clear_stale_pid_files() print a COULD NOT
    REMOVE line on every single start -- noise that trains an operator to ignore the
    startup block, which is the thing rule 14 is protecting.
    """
    root = tmp_path
    (root / "pid").mkdir()
    assert entrypoint.stale_pid_files(root) == []


def test_it_DELETES_what_it_found_and_says_so(tmp_path, capsys):
    """And reports (none) when there is nothing, because a silent startup is unreadable."""
    root = tmp_path
    (root / "pid").write_text("999")

    removed = entrypoint.clear_stale_pid_files([str(root)])
    assert removed == 1
    assert not (root / "pid").exists(), "it found the file and left it there"
    assert "removed stale" in capsys.readouterr().out

    removed = entrypoint.clear_stale_pid_files([str(root)])
    assert removed == 0
    assert "(none)" in capsys.readouterr().out, (
        "a startup that prints nothing cannot be told from one that did not run (rule 14)"
    )


def test_the_DOCKERFILE_wires_it_AND_installs_an_interpreter_for_it():
    """An ENTRYPOINT that cannot run is a container that never starts.

    rust:1.90-bookworm carries a rust toolchain and a Debian base; it does NOT promise
    python3. This asserts the three things that have to be true together -- the script
    is copied, it is the ENTRYPOINT, and an interpreter is installed -- because any one
    of them missing turns the replica into a container that exits instantly with
    nothing naming the cause.
    """
    dockerfile = (Path(__file__).resolve().parents[1] / "docker" / "icp-replica.Dockerfile").read_text()

    # DIRECTIVES ARE MATCHED AT THE START OF A LINE, NOT ANYWHERE IN THE FILE, and the
    # first version of this test got that wrong in a way worth recording. It did
    #
    #     dockerfile.split("ENTRYPOINT")[1]
    #
    # and the first "ENTRYPOINT" in this Dockerfile is in a COMMENT -- "an ENTRYPOINT
    # that cannot run is a container that never starts" -- so it searched the prose
    # explaining the directive instead of the directive. Same error as the tile regexes
    # fixed earlier today: matching text where the property is structural. A Dockerfile
    # directive is a line that BEGINS with the keyword, and that is what is asserted.
    # CONTINUATIONS ARE JOINED FIRST, which the second version of this test also got
    # wrong. `python3-minimal` sits on a `\`-continuation of the apt-get RUN, not on
    # the line beginning with RUN, so a per-line check could not see it and reported
    # "no interpreter is installed" about a Dockerfile that installs one. A Dockerfile
    # directive is a LOGICAL line; parsing it as physical lines is the same mistake as
    # matching prose, one level down.
    joined = dockerfile.replace("\\\n", " ")
    directives = [line for line in joined.splitlines() if line and not line.lstrip().startswith("#")]

    assert any(line.startswith("COPY") and "icp_replica_entrypoint.py" in line for line in directives), (
        "the entrypoint script is not COPYed into the image"
    )
    assert any(
        line.startswith("ENTRYPOINT") and "icp_replica_entrypoint.py" in line for line in directives
    ), "the script is copied but is not the ENTRYPOINT, so nothing runs it"
    assert any(line.startswith("RUN") and "python3-minimal" in line for line in directives), (
        "no interpreter is installed, so the ENTRYPOINT cannot run and the container exits instantly"
    )
    assert any(line.startswith("RUN") and "python3 --version" in line for line in directives), (
        "nothing proves the interpreter works at BUILD time, so a missing one becomes a "
        "container that exits instantly instead of a red build"
    )
    assert any(line.startswith("CMD") and '"dfx", "start"' in line for line in directives), (
        "the CMD must stay the dfx invocation -- the entrypoint execs it, so dfx is pid 1 "
        "and `docker compose stop` signals dfx itself"
    )
