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
