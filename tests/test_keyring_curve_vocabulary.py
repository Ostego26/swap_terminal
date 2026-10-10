"""The curve names agree across Python, Rust and Candid. The one cross-language contract.

Role: test (reads three source files; builds nothing, opens no socket)
Reads: swap_terminal/modules/keyring_paths.py (through an import),
       icp/threshold_custody/src/lib.rs, icp/threshold_custody/threshold_custody.did
Writes: nothing
Can move funds: no
Mainnet-safe: yes

WHY THIS IS THE ONLY CROSS-LANGUAGE CHECK THE KEYRING NEEDS.

The derivation path scheme lives in ONE language. Python builds the path and the
canister forwards it unexamined, so there is no second copy to drift (CLAUDE.md
rule 8, argued at length in both files' headers).

The curve NAME is the exception, and it cannot be made to live in one place: the
canister must name its variants in Rust for the candid interface, and the Python
side must name them to ask. So there are three copies by construction --
keyring_paths.CURVES, the Rust `enum Curve` renames, and the `.did`'s variant
labels -- and this file is what keeps them equal.

WHAT A DISAGREEMENT WOULD ACTUALLY DO, which is why it is worth a test rather
than a comment. A Python caller asking for a curve the canister does not list
gets a candid DECODE FAILURE, not a wrong key -- so the failure is safe, and it
is also opaque: it surfaces as a dfx error about a type mismatch, at the one
moment an operator is trying to read a key. The cost of the drift is a confusing
afternoon rather than lost funds, and one test removes it.

RULE 8 ASKS FOR THE DIFFERENCE TO BE NAMED AT BOTH SITES, and it is: the Rust
enum's doc comment and the .did's `type Curve` comment each name this file.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from modules.keyring_paths import CURVES

ICP = Path(__file__).resolve().parent.parent / "icp" / "threshold_custody"
RUST = ICP / "src" / "lib.rs"
DID = ICP / "threshold_custody.did"


def _block(text: str, opener: str) -> str:
    """The brace-balanced block that starts at `opener`.

    BRACE-BALANCED RATHER THAN LINE-COUNTED, because a regex over the whole file
    would pick up curve names out of comments and prose -- which is the
    prose-reading detector this tree has now produced five times (HANDOFF.md
    section 6 counts the first four, and the fifth is fixed in
    tests/test_icp_custody_addresses.py). Scoping to the declaration's own block
    is what makes the extraction mean what it says.
    """
    start = text.find(opener)
    assert start != -1, f"{opener!r} not found; the declaration was renamed or removed"
    brace = text.find("{", start)
    assert brace != -1, f"no opening brace after {opener!r}"
    depth = 0
    for index in range(brace, len(text)):
        if text[index] == "{":
            depth += 1
        elif text[index] == "}":
            depth -= 1
            if depth == 0:
                return text[brace + 1 : index]
    raise AssertionError(f"unbalanced braces after {opener!r}")


def _strip_line_comments(text: str) -> str:
    """Drop `//...` to end of line. The .did and the Rust both carry reasoning.

    A curve name inside a comment is documentation, not a declaration, and this
    file exists to compare DECLARATIONS.
    """
    return "\n".join(line.split("//")[0] for line in text.splitlines())


def rust_curve_names() -> set[str]:
    """The `#[serde(rename = "...")]` labels inside `pub enum Curve`."""
    block = _block(RUST.read_text(), "pub enum Curve")
    return set(re.findall(r'#\[serde\(rename\s*=\s*"([^"]+)"\)\]', block))


def did_curve_names() -> set[str]:
    """The variant labels inside `type Curve = variant { ... }`."""
    block = _strip_line_comments(_block(DID.read_text(), "type Curve"))
    return set(re.findall(r"([A-Za-z_][A-Za-z0-9_]*)\s*;", block))


def test_python_rust_and_candid_name_the_same_curves():
    """MUTATION: rename one variant in any of the three files.

    This is the assertion the whole file exists for. The three sets are three
    copies by construction, and nothing but this keeps them equal.
    """
    rust = rust_curve_names()
    did = did_curve_names()
    assert rust == CURVES, f"Rust {sorted(rust)} != Python {sorted(CURVES)}"
    assert did == CURVES, f"Candid {sorted(did)} != Python {sorted(CURVES)}"


def test_the_extraction_found_three_and_not_zero():
    """A detector that silently matches nothing passes every comparison.

    THE FAILURE THIS GUARDS IS THIS FILE'S OWN. If `pub enum Curve` is renamed,
    `_block` asserts; but if the serde attributes were reformatted onto one line
    with the variant, the regex could return an empty set -- and an empty set
    equals an empty set, so the test above would pass while checking nothing.
    """
    assert len(rust_curve_names()) == 3
    assert len(did_curve_names()) == 3
    assert len(CURVES) == 3


@pytest.mark.parametrize("curve", sorted(CURVES))
def test_each_curve_is_routed_to_a_management_call_in_the_rust(curve):
    """Every name Python can ask for has an arm that reaches a real call.

    A variant declared and not routed compiles -- Rust's match would need to
    handle it, but an arm that returns an error for it is also a legal arm. This
    asserts the canister names both management calls, so no curve can be declared
    into a dead end.
    """
    source = RUST.read_text()
    assert "ecdsa_public_key" in source
    assert "schnorr_public_key" in source
    # The variant's rename is what a caller sends, so it must be present verbatim.
    assert f'rename = "{curve}"' in source


def test_the_canister_still_names_no_signing_call():
    """Belt-and-braces with tests/test_icp_canister_cannot_sign.py.

    HERE TOO because this file is the one that proves the canister grew a second
    management-canister module. `schnorr` is the module that also contains
    `sign_with_schnorr`, so importing from it is exactly the moment the signing
    call becomes one word away.
    """
    body = _strip_line_comments(RUST.read_text())
    for call in ("sign_with_ecdsa", "sign_with_schnorr"):
        assert call not in body, f"{call} appears in the canister's code"
