"""The dfx project and the Cargo workspace must agree, without running either.

Role: code hygiene (read-only)
Reads: dfx.json, Cargo.toml, and the Cargo.toml of every workspace member
Writes: nothing
Can move funds: no
Mainnet-safe: yes

A CLEAN GATE, not a baseline (rule 19): every count asserted here is zero or
exact, so there is nothing to ratchet down and nothing to delete later.

WHY THIS EXISTS. The first `dfx deploy` inside the replica container failed like
this, AFTER creating the canister:

    threshold_custody canister created with canister id: bkyz2-fmaaa-aaaaa-qaaaq-cai
    Building canisters...
    Error: Failed while trying to deploy canisters.
    Caused by: The pre-build all step failed
    Caused by: 'cargo locate-project' failed: error: could not find `Cargo.toml`
              in `/repo` or any parent directory

dfx's Rust builder shells out to `cargo locate-project` from the directory that
holds dfx.json, and `locate-project` walks PARENTS only -- it never descends. The
manifest was two levels down at icp/threshold_custody/Cargo.toml, so there was
nothing to find. Nothing about the repository was wrong in a way any Python test
could see, and the only thing that reported it was an operator pasting a failed
deploy back.

That is the cost this file removes: a dfx/Cargo disagreement is checkable from
the files alone, in milliseconds, with no toolchain and no network.

WHY TOMLLIB AND NOT `cargo metadata`. `cargo metadata` is the authoritative
answer and it needs cargo, a registry index, and sometimes the network -- so on a
machine without a Rust toolchain this module would skip, and a gate that skips on
the machine where the mistake gets made is not a gate (rule 17: a check that
examined nothing is not a pass). The four invariants below are structural, so the
manifests answer them directly.

WHAT IT DOES NOT CHECK: whether the canister COMPILES. That needs cargo and the
wasm32 target and belongs to the replica container, which is where it is actually
run. This file only asserts that dfx and Cargo are describing the same project.
"""

from __future__ import annotations

import json
import subprocess
import tomllib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
DFX_JSON = REPO_ROOT / "dfx.json"


def dfx_project() -> dict:
    """dfx.json, parsed. Not cached: it is one small file read a handful of times."""
    return json.loads(DFX_JSON.read_text())


def workspace_members() -> list[Path]:
    """Member DIRECTORIES named by the root workspace manifest, in declared order.

    Returns [] when there is no root manifest at all rather than raising, so the
    failure a reader sees is test_a_cargo_manifest_sits_beside_dfx_json saying
    what is missing and what dfx will do about it -- not three collateral
    FileNotFoundErrors in helpers (rule 14: the message is the deliverable).
    """
    manifest = REPO_ROOT / "Cargo.toml"
    if not manifest.is_file():
        return []
    root = tomllib.loads(manifest.read_text())
    return [REPO_ROOT / member for member in root.get("workspace", {}).get("members", [])]


def member_manifests() -> list[tuple[str, Path]]:
    """(declared package name, manifest path) for every member that has a manifest.

    Members with no Cargo.toml are not silently dropped -- they are what
    test_every_workspace_member_has_a_manifest fails on.
    """
    found = []
    for directory in workspace_members():
        manifest = directory / "Cargo.toml"
        if manifest.is_file():
            name = tomllib.loads(manifest.read_text()).get("package", {}).get("name")
            found.append((name, manifest))
    return found


def rust_canisters() -> list[tuple[str, dict]]:
    """(canister name, its dfx.json spec) for every canister dfx will build with cargo."""
    return [
        (name, spec)
        for name, spec in (dfx_project().get("canisters") or {}).items()
        if spec.get("type") == "rust"
    ]


def test_there_is_at_least_one_rust_canister_to_check():
    """Rule 17: every parametrized test below would pass by finding nothing."""
    assert DFX_JSON.is_file(), f"no dfx.json at {REPO_ROOT}"
    assert rust_canisters(), (
        "dfx.json declares no canister with type 'rust', so every assertion in "
        "this module checked NOTHING"
    )


def test_a_cargo_manifest_sits_beside_dfx_json():
    """The exact invariant `cargo locate-project` enforces, and the one that failed.

    Same directory, not merely somewhere in the tree: dfx runs locate-project from
    dfx.json's own directory and that command only walks upward.
    """
    beside = DFX_JSON.parent / "Cargo.toml"
    assert beside.is_file(), (
        f"dfx.json is at {DFX_JSON.parent} and there is no Cargo.toml there. "
        f"`dfx deploy` will fail at the pre-build step with 'could not find "
        f"Cargo.toml in {DFX_JSON.parent} or any parent directory' -- after it has "
        f"already created the canister on the replica."
    )


@pytest.mark.parametrize("canister,spec", rust_canisters(), ids=[c[0] for c in rust_canisters()])
def test_each_rust_canister_names_a_workspace_member(canister, spec):
    """dfx builds `--package <package>`, so cargo must be able to see that package."""
    package = spec.get("package")
    assert package, f"canister {canister!r} is type 'rust' with no 'package' key"
    names = [name for name, _ in member_manifests()]
    assert package in names, (
        f"dfx.json builds canister {canister!r} as cargo package {package!r}, which "
        f"is not a member of the root workspace. Members declare: {names or '(none)'}"
    )


@pytest.mark.parametrize("canister,spec", rust_canisters(), ids=[c[0] for c in rust_canisters()])
def test_each_declared_candid_file_exists(canister, spec):
    """dfx reads this file to install the interface; a missing one fails mid-deploy."""
    declared = spec.get("candid")
    assert declared, f"canister {canister!r} declares no 'candid' file"
    path = REPO_ROOT / declared
    assert path.is_file(), f"canister {canister!r} declares candid {declared!r}; no such file"


def test_every_workspace_member_has_a_manifest():
    """A member path with no Cargo.toml makes every cargo command in the tree fail."""
    missing = [
        str(d.relative_to(REPO_ROOT)) for d in workspace_members() if not (d / "Cargo.toml").is_file()
    ]
    assert not missing, f"workspace members with no Cargo.toml: {missing}"


def test_no_workspace_member_declares_a_profile():
    """Cargo IGNORES `[profile.*]` in a member and only WARNS, which is the hazard.

    icp/threshold_custody/Cargo.toml carried opt-level='z', lto, codegen-units=1
    and strip while it was a standalone crate, and they took effect. Creating the
    workspace would have silently reverted the canister to the default release
    profile -- a bigger wasm, paid for in cycles at install time -- with nothing
    failing and one warning in a build log nobody reads.

    The profile belongs to the root manifest. This asserts a member never grows
    one back.
    """
    offenders = []
    for name, manifest in member_manifests():
        if "profile" in tomllib.loads(manifest.read_text()):
            offenders.append(f"{manifest.relative_to(REPO_ROOT)} (package {name})")
    assert not offenders, (
        "these workspace members declare [profile.*], which Cargo ignores with only "
        f"a warning -- move the settings to the root Cargo.toml: {offenders}"
    )


def declared_init_arg_files() -> list[tuple[str, str]]:
    """(canister name, the init_arg_file path it declares) for every canister with one."""
    return [
        (name, spec["init_arg_file"])
        for name, spec in (dfx_project().get("canisters") or {}).items()
        if spec.get("init_arg_file")
    ]


def test_every_init_arg_file_is_git_ignored():
    """An init_arg_file holds environment state and must never be committable.

    THE HAZARD IS FORCED BY dfx AND IS NOT HYPOTHETICAL. DFINITY's ICP ledger
    setup documentation says, verbatim, "`dfx.json` does not support referring to
    values through environment variables. Values must be hardcoded in plain
    text." The values it means are account identifiers read off a running replica
    with `dfx ledger account-id`, and the same identity has a DIFFERENT account on
    every fresh replica.

    So a tracked init_arg_file is a file that claims to know something only a live
    replica can answer, and it goes stale silently the moment the container is
    recreated: the deploy succeeds, the ledger mints its supply to an account
    nobody on this replica controls, and the first symptom arrives at a transfer.

    `git check-ignore` is the instrument rather than a parse of .gitignore,
    because reimplementing git's ignore-pattern matching is a second
    implementation of somebody else's rule (rule 8) -- the same argument
    test_docker_build_context.py makes for not simulating .dockerignore.
    """
    declared = declared_init_arg_files()
    assert declared, (
        "no canister in dfx.json declares an init_arg_file, so this test checked NOTHING "
        "(rule 17). Delete it along with the mechanism if the ledger canister is gone."
    )
    not_ignored = []
    for canister, path in declared:
        # No shell, argv a fixed list, and `path` comes from this repository's own
        # dfx.json rather than from input. `git` is resolved from PATH on purpose:
        # an absolute path would break on every machine whose git is elsewhere, and
        # this is a read-only query. No suppression directives on these two lines --
        # S603 and S607 are not enabled for tests/, so a directive here would
        # suppress nothing and RUF100 said exactly that (rule 19: a suppression is
        # a claim you checked something, not decoration).
        result = subprocess.run(
            ["git", "check-ignore", "-q", path],
            cwd=REPO_ROOT,
            capture_output=True,
            check=False,
        )
        # 0 == ignored, 1 == NOT ignored, anything else == git itself failed and
        # the question was not answered. The third case must not score as a pass:
        # rule 17's "a check that examined nothing is not a pass".
        if result.returncode == 1:
            not_ignored.append(f"{canister} -> {path}")
        elif result.returncode != 0:
            raise AssertionError(
                f"`git check-ignore {path}` exited {result.returncode}, so whether it is "
                f"ignored is UNKNOWN rather than fine: {result.stderr.decode(errors='replace')}"
            )
    assert not not_ignored, (
        "these init_arg_file paths are not git-ignored, so the account identifiers they "
        f"hardcode can be committed: {not_ignored}"
    )


def test_a_canister_mirroring_a_mainnet_one_declares_its_remote_id():
    """A local copy of a canister that EXISTS on mainnet must say where it lives there.

    `remote.id.ic` is what stops dfx from trying to CREATE the canister on the ic
    network -- it declares "this already exists there, at this id". For the ICP
    ledger that id is the real one, holding real ICP, and the consequence of
    omitting the block is that a stray `dfx deploy --network ic` treats a local
    test ledger as something to deploy rather than something that exists.

    Applied to any canister declared with a `wasm` URL under github.com/dfinity,
    which is the signature of running somebody else's released canister locally
    rather than building our own.
    """
    mirrors = [
        (name, spec)
        for name, spec in (dfx_project().get("canisters") or {}).items()
        if "dfinity" in str(spec.get("wasm", ""))
    ]
    assert mirrors, (
        "no canister in dfx.json runs a released DFINITY wasm, so this test checked NOTHING"
    )
    missing = [name for name, spec in mirrors if not (spec.get("remote") or {}).get("id", {}).get("ic")]
    assert not missing, (
        f"these canisters run a released DFINITY wasm with no remote.id.ic declared: {missing}. "
        f"Without it dfx would try to CREATE them on mainnet instead of recognizing that they "
        f"already exist there."
    )


def test_a_pinned_release_tag_is_used_rather_than_a_moving_reference():
    """Every DFINITY artifact URL names a release tag, not `latest` or a branch.

    A replica or ledger version is a consensus implementation; "whatever was
    released this morning" is not a thing to debug a transfer against, which is
    the same argument docker/icp-replica.Dockerfile already makes for pinning
    DFX_VERSION rather than taking latest.
    """
    moving = []
    for name, spec in (dfx_project().get("canisters") or {}).items():
        for key in ("wasm", "candid"):
            value = str(spec.get(key, ""))
            if "dfinity" in value and ("/latest/" in value or "/master/" in value or "/main/" in value):
                moving.append(f"{name}.{key} -> {value}")
    assert not moving, f"these point at a moving reference instead of a release tag: {moving}"
