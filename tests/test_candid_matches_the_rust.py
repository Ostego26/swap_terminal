"""Each hand-written .did must declare exactly the methods its Rust exports, as the same kind.

Role: submodule (tests)
Reads: icp/<canister>/<canister>.did and icp/<canister>/src/lib.rs for every rust
       canister icp/dfx.json declares
Writes: nothing
Can move funds: no. No canister is built, deployed or called; two text files are parsed.
Mainnet-safe: yes, and it needs no dfx, no cargo, no replica and no network

WHY IT EXISTS, and it is a claim a file made about itself that was not true.
icp/threshold_custody/threshold_custody.did opens with:

    HAND-WRITTEN AND KEPT IN STEP BY A TEST, the same arrangement
    operator_admin/operator_admin.did explains: dfx reads this file to build the
    Candid UI and to type-check calls [...] So the file is explicit, and
    tests/test_icp_canister_cannot_sign.py pins the method surface against the
    Rust source.

Measured 2026-10-11: that test never opens the .did. Its method assertion,
test_the_canister_declares_only_the_two_methods_it_documents, COUNTS `#[ic_cdk::query]`
and `#[ic_cdk::update]` occurrences in lib.rs and compares the counts to literals -- a
real check on the Rust, and silent about the candid. `grep -rn threshold_custody.did`
over the whole tree returns tests/test_keyring_curve_vocabulary.py, which extracts the
Curve variant LABELS only, and one docstring mention. So nothing compared the two
method sets, and the file saying otherwise is the wrong-comment bug (rule 16) in the
one place a caller looks first.

WHY A DRIFTED .did IS WORSE THAN A MISSING ONE. dfx reads it to TYPE-CHECK CALLS. A
method in the candid that the Rust does not export type-checks at the caller and fails
at the replica; a method the Rust exports and the candid omits is unreachable through
dfx and through the Candid UI, which is the operator's own console onto these canisters.
Either way the failure arrives at the moment of use, against a deployed canister, which
is the most expensive place for it.

THE KIND IS CHECKED, NOT JUST THE NAME, and that is the half worth having. Candid marks
a query method with a trailing `query`; a method declared `query` that the Rust exports
as an update would be rejected by the replica as a query call. threshold_custody's
public_key is deliberately an UPDATE -- lib.rs:261-263 records why: ecdsa_public_key and
schnorr_public_key are inter-canister calls and a query cannot make one. A .did that
marked it `query` would be a file describing a canister that cannot work, and nothing
would have said so.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
ICP = REPOSITORY_ROOT / "icp"


def rust_canisters() -> list[str]:
    """The rust canisters icp/dfx.json declares, which is what dfx would build.

    DERIVED rather than listed, for rule 3's reason: a third canister added without a
    line here would be the newest and least-reviewed code in the repository and the one
    nothing checked.
    """
    dfx = json.loads((ICP / "dfx.json").read_text())
    return sorted(
        name for name, body in (dfx.get("canisters") or {}).items()
        if (body or {}).get("type") == "rust"
    )


#: A candid method line inside a `service : ... -> { ... }` block. Candid allows the
#: return type to be absent for a oneway method, so the arrow and return are optional.
_DID_METHOD = re.compile(
    r"^\s*([a-z_][a-z0-9_]*)\s*:\s*\([^)]*\)\s*(?:->\s*\([^)]*\)\s*)?(query)?\s*;",
    re.IGNORECASE,
)


def did_methods(path: Path) -> dict[str, str]:
    """{method: "query" | "update"} from the .did's service block.

    PARSED FROM THE SERVICE BLOCK ONLY, not the whole file. A `type` declaration can
    carry a field whose name and parentheses look exactly like a method line -- candid
    records use the same `name : type` shape -- so a file-wide scan would invent
    methods out of record fields. The block is delimited by the `service :` line and
    the first line that is a bare closing brace.
    """
    methods: dict[str, str] = {}
    inside = False
    for line in path.read_text().splitlines():
        stripped = line.strip()
        if stripped.startswith("//"):
            continue
        if not inside:
            if stripped.startswith("service"):
                inside = True
            continue
        if stripped == "}":
            break
        match = _DID_METHOD.match(line)
        if match:
            methods[match.group(1)] = "query" if match.group(2) else "update"
    return methods


def rust_methods(path: Path) -> dict[str, str]:
    """{method: "query" | "update"} from the ic_cdk attribute macros in lib.rs.

    THE MACRO IS WHAT CREATES A CANISTER METHOD, so counting them counts the surface --
    the same reasoning tests/test_icp_canister_cannot_sign.py already uses, and this
    reads the pair rather than the count. `#[ic_cdk::init]` and `#[ic_cdk::post_upgrade]`
    are deliberately NOT included: they are lifecycle hooks, not callable methods, and a
    .did does not declare them.

    A RENAMED EXPORT IS FOLLOWED. `#[ic_cdk::query(name = "http_request")]` exports
    under the candid name, not the Rust function name, so the attribute's own name
    argument wins where it is present -- without this, operator_admin's console method
    would read as a mismatch.
    """
    methods: dict[str, str] = {}
    pending: str | None = None
    pending_name: str | None = None
    for line in path.read_text().splitlines():
        stripped = line.strip()
        attribute = re.match(r"#\[ic_cdk::(query|update)(?:\((.*)\))?\]", stripped)
        if attribute:
            pending = attribute.group(1)
            renamed = re.search(r'name\s*=\s*"([^"]+)"', attribute.group(2) or "")
            pending_name = renamed.group(1) if renamed else None
            continue
        if pending:
            function = re.match(r"(?:pub\s+)?(?:async\s+)?fn\s+([a-z_][a-z0-9_]*)", stripped)
            if function:
                methods[pending_name or function.group(1)] = pending
                pending = None
                pending_name = None
    return methods


@pytest.mark.parametrize("canister", rust_canisters())
def test_the_candid_declares_exactly_the_methods_the_rust_exports(canister):
    """Name-for-name, and the same query/update kind for each.

    MUTATION: delete the `config : () -> (ConfigResult) query;` line from
    threshold_custody.did and this fails naming config as missing from the candid; mark
    `public_key` as `query` and it fails on the kind.
    """
    did = ICP / canister / f"{canister}.did"
    rust = ICP / canister / "src" / "lib.rs"
    assert did.is_file(), f"{did} is missing"
    assert rust.is_file(), f"{rust} is missing"

    declared = did_methods(did)
    exported = rust_methods(rust)

    assert declared, (
        f"no methods parsed out of {did.name}. Either the service block moved or this "
        f"test's parser no longer matches candid's syntax -- an empty expectation here "
        f"would make every assertion below pass vacuously"
    )
    assert exported, f"no ic_cdk query/update macros found in {rust}"

    only_candid = sorted(set(declared) - set(exported))
    only_rust = sorted(set(exported) - set(declared))
    assert only_candid == [], (
        f"{did.name} declares {only_candid}, which {canister}'s Rust does not export. dfx "
        f"type-checks callers against the candid, so a caller of these compiles and then "
        f"fails at the replica."
    )
    assert only_rust == [], (
        f"{canister}'s Rust exports {only_rust}, which {did.name} does not declare. They "
        f"are unreachable through dfx and through the Candid UI -- the operator's own "
        f"console onto this canister."
    )

    wrong_kind = {
        name: (declared[name], exported[name])
        for name in declared
        if declared[name] != exported[name]
    }
    assert wrong_kind == {}, (
        f"{canister}: (candid, rust) kind disagreements {wrong_kind}. A method the candid "
        f"marks `query` and the Rust exports as an update is rejected by the replica when "
        f"called as a query -- and threshold_custody's public_key is an update on purpose, "
        f"because the management-canister calls it makes cannot be made from a query."
    )


def test_the_parsers_disagree_about_nothing_because_they_are_blind_to_the_same_thing():
    """A guard on this file's own method: both parsers must see MORE than one method.

    WHY THIS IS HERE. Both functions above are regex parsers over hand-written files,
    and the failure mode of a regex parser is matching nothing and reporting agreement.
    The assertion above guards against an empty set on each side separately; this one
    guards against the subtler case where both parsers find exactly one method and agree
    trivially while the real surface is larger.

    threshold_custody has two methods and operator_admin has more than two, measured
    from both files, so a floor of two holds for every canister in the tree and would
    fail the day a parser started matching only the first.
    """
    for canister in rust_canisters():
        declared = did_methods(ICP / canister / f"{canister}.did")
        exported = rust_methods(ICP / canister / "src" / "lib.rs")
        assert len(declared) >= 2, f"{canister}.did: parsed only {sorted(declared)}"
        assert len(exported) >= 2, f"{canister}/src/lib.rs: parsed only {sorted(exported)}"
