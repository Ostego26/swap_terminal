"""Increment 1 holds a key and cannot spend. This is what stops that quietly changing.

Role: test (reads source files; opens no socket, runs no canister, builds nothing)
Reads: icp/threshold_custody/src/lib.rs, icp/threshold_custody/Cargo.toml
Writes: nothing
Can move funds: no
Mainnet-safe: yes

WHY A TEST AND NOT A COMMENT. The canister's safety claim is "there is no code
path here that produces a signature", and that claim is worth exactly as much as
whatever enforces it. A comment enforces nothing: increment 2 adds signing, and
the comment above it still says it cannot sign until somebody notices.

MEASURED ON THE BUILT ARTIFACT TOO, 2026-10-05:

    cargo build --release --target wasm32-unknown-unknown
    -> 376425 bytes
       PRESENT  canister_update public_key
       PRESENT  canister_query config
       PRESENT  ecdsa_public_key
       absent   sign_with_ecdsa

RE-MEASURED 2026-10-10 after increment 1b added ed25519 and BIP-340, and the
drift is left visible rather than overwritten (rule 1):

    -> 473941 bytes          <- 376425 before; +97516 for the second
                                management-canister module and the Curve enum
       EXPORT   canister_init
       EXPORT   canister_post_upgrade
       EXPORT   canister_query config
       EXPORT   canister_update public_key     <- still FOUR, still one update
       PRESENT  ecdsa_public_key
       PRESENT  schnorr_public_key             <- new in 1b
       absent   sign_with_ecdsa
       absent   sign_with_schnorr              <- the one that matters in 1b
       absent   http_request

THE 2026-10-05 NOTE SAID THIS CHECK COULD NOT BE RUN HERE, "because it would
need the wasm32 target and a network fetch". Both are available after
`rustup target add wasm32-unknown-unknown`, so it is now a test rather than a
paragraph -- see `test_the_built_wasm_exports_no_signing_call` at the bottom,
which is SKIPPED BY DEFAULT because a release build costs about 34s and this
suite has over 4,500 tests. Set ST_CHECK_WASM=1 to run it.

So the binary agrees with the source. The rest of this file pins the source,
which is the half that can be checked on every run without a toolchain.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

CANISTER = Path(__file__).resolve().parent.parent / "icp" / "threshold_custody"
SOURCE = CANISTER / "src" / "lib.rs"

#: The management-canister calls that MOVE something or COMMIT to something.
#:
#: `ecdsa_public_key` is deliberately absent from this list -- it is the one call
#: increment 1 exists to make, it returns a public key, and a public key is public.
SIGNING_CALLS = (
    "sign_with_ecdsa",
    "sign_with_schnorr",
    "bitcoin_send_transaction",
    "http_request",          # an outcall is how a canister would reach a chain it cannot see
)


#: The calls that MOVE VALUE, which is the subset every canister in this repository
#: must not contain. Separate from SIGNING_CALLS because `http_request` cannot be on a
#: list applied to operator_admin -- see FORBIDDEN_BY_CANISTER.
VALUE_MOVING_CALLS = (
    "sign_with_ecdsa",
    "sign_with_schnorr",
    "bitcoin_send_transaction",
)

#: What each canister under icp/ must not contain, and the lists DIFFER on purpose.
#:
#: WHY operator_admin IS NOT HELD TO SIGNING_CALLS, measured 2026-10-11 before the
#: entry was written rather than after it failed: operator_admin DEFINES `http_request`
#: as its own query method (operator_admin.did:85, lib.rs:863) because the replica's
#: HTTP gateway routes GETs to a canister method of that name -- it is how the canister
#: serves its operator console. That is a completely different thing from the
#: MANAGEMENT canister's `http_request`, which is an outcall to the internet and is the
#: reason the name is on SIGNING_CALLS at all. Applying the four-call list here would
#: have failed on a legitimate method, and a test that fails for a legitimate reason
#: gets deleted rather than fixed.
#:
#: threshold_custody keeps the full four. It serves no page, so for it the name can
#: only ever be the outcall.
FORBIDDEN_BY_CANISTER = {
    "threshold_custody": SIGNING_CALLS,
    "operator_admin": VALUE_MOVING_CALLS,
}


def non_comment_occurrences(text: str, call: str) -> list[str]:
    """Lines naming `call` that are NOT Rust comments.

    A line that NAMES a call while explaining its absence is not a call, and this
    repository's canister sources are mostly such explanations -- `grep -n sign` over
    both lib.rs files returns 15 hits, every one of them a comment or a rendered-page
    string. The distinction is whether the line starts // or * after stripping.

    EXTRACTED 2026-10-11 so the two canisters share one definition of "a call" rather
    than two that agree today (rule 8). It is also the whole decision this file makes,
    which is rule 10's reason for it being a function: it can be asserted on directly
    with a seeded string instead of only through a canister source.
    """
    return [
        line.strip()
        for line in text.splitlines()
        if call in line and not line.strip().startswith(("//", "*"))
    ]


def test_the_canister_source_contains_no_signing_call():
    """MUTATION: add `sign_with_ecdsa` anywhere in lib.rs.

    Increment 1's whole safety argument is that it cannot produce a signature.
    Everything else about it -- that it holds no addresses, knows no chains, has
    no timers -- follows from being unable to act on any of them.
    """
    assert SOURCE.is_file(), f"{SOURCE} is missing; the canister was moved or deleted"
    text = SOURCE.read_text()

    for call in SIGNING_CALLS:
        occurrences = non_comment_occurrences(text, call)
        assert not occurrences, (
            f"increment 1 must not call {call}; found:\n  " + "\n  ".join(occurrences)
        )


@pytest.mark.parametrize("canister", sorted(FORBIDDEN_BY_CANISTER))
def test_no_canister_in_this_repository_can_move_value(canister):
    """EVERY canister under icp/, not just threshold_custody.

    WHAT THIS CLOSES, measured 2026-10-11: operator_admin is 1458 lines -- three times
    threshold_custody's 457 -- and NOTHING pinned its inability to sign. Four files in
    tests/ mention it and none of them contained `sign_with` or `cannot sign`
    (`grep -rln operator_admin tests/ | xargs grep -ln 'sign_with|cannot sign'` matched
    nothing). So the repository's entire "the canisters cannot move value" argument
    rested on a test scoped by a module-level CANISTER constant pointing at the smaller
    of the two.

    It passes today -- `grep -rn "sign_with" icp/` returns a single hit, prose in
    icp/README.md -- which is the point. The absence was already true and simply
    unasserted, and an unasserted absence is one commit from not being true.

    THE FORBIDDEN LIST IS PER CANISTER AND THE REASON IS AT FORBIDDEN_BY_CANISTER.
    Reading it before adding a canister here is the difference between a gate and a
    test somebody deletes.

    MUTATION: add `sign_with_schnorr` to a non-comment line of either lib.rs.
    """
    source = Path(__file__).resolve().parent.parent / "icp" / canister / "src" / "lib.rs"
    assert source.is_file(), f"{source} is missing; the canister was moved or deleted"
    text = source.read_text()

    for call in FORBIDDEN_BY_CANISTER[canister]:
        occurrences = non_comment_occurrences(text, call)
        assert not occurrences, (
            f"{canister} must not call {call}; found:\n  " + "\n  ".join(occurrences)
        )


def test_every_canister_in_the_tree_is_covered_by_the_table_above():
    """FORBIDDEN_BY_CANISTER must name every rust canister icp/dfx.json declares.

    Without this the parametrize above passes vacuously the day a third canister is
    added: a new crate nobody listed is a crate nobody checks, and it would be the
    newest and least-reviewed code in the repository. Rule 3 -- the denominator is
    what dfx would actually deploy, not what this file remembered.
    """
    dfx = json.loads((Path(__file__).resolve().parent.parent / "icp" / "dfx.json").read_text())
    rust = {
        name for name, body in (dfx.get("canisters") or {}).items()
        if (body or {}).get("type") == "rust"
    }
    assert rust == set(FORBIDDEN_BY_CANISTER), (
        f"icp/dfx.json declares rust canisters {sorted(rust)} and FORBIDDEN_BY_CANISTER "
        f"covers {sorted(FORBIDDEN_BY_CANISTER)}. A canister missing from the table is a "
        f"canister nothing checks for a signing call."
    )


def test_the_canister_declares_only_the_two_methods_it_documents():
    """A third entry point is a third thing to reason about, so it must be deliberate.

    ic_cdk's attribute macros are what create a canister method, so counting them
    counts the surface. If increment 2 adds one, this fails and whoever adds it
    updates the expectation -- which is the moment to ask what it can do.
    """
    text = SOURCE.read_text()
    entry_points = {
        "query": text.count("#[ic_cdk::query]"),
        "update": text.count("#[ic_cdk::update]"),
        "init": text.count("#[ic_cdk::init]"),
        "post_upgrade": text.count("#[ic_cdk::post_upgrade]"),
    }
    assert entry_points == {"query": 1, "update": 1, "init": 1, "post_upgrade": 1}, (
        f"the canister's method surface changed: {entry_points}"
    )


def test_the_key_name_has_no_default():
    """A default key name would report an address for a key nobody chose.

    `dfx_test_key`, `test_key_1` and `key_1` are DIFFERENT master keys: the same
    derivation path under two names yields two public keys and therefore two
    addresses. Defaulting to any of them means a canister that answers
    confidently about the wrong one.
    """
    text = SOURCE.read_text()
    for name in ("dfx_test_key", "test_key_1", "key_1"):
        in_code = [
            line.strip()
            for line in text.splitlines()
            if f'"{name}"' in line and not line.strip().startswith(("//", "*"))
        ]
        assert not in_code, f"{name} is hardcoded in the canister: {in_code}"


@pytest.mark.parametrize("required", ["ic-cdk", "candid", "serde"])
def test_the_canister_declares_the_dependencies_it_imports(required):
    """Rule 12's spirit for a second language: what it uses, it declares.

    serde is the one worth pinning. candid re-exports Deserialize, so
    `use candid::Deserialize` LOOKS sufficient and compiles to an error only at
    the derive site, because the macro expands to `serde::` paths. That cost a
    build on 2026-10-05 and would cost it again the next time someone tidies the
    dependency list.
    """
    manifest = (CANISTER / "Cargo.toml").read_text()
    assert f"\n{required} " in manifest or f"\n{required}=" in manifest, (
        f"{required} is imported but not declared in Cargo.toml"
    )


# ---------------------------------------------------------------------------
# THE ARTIFACT CHECK. Opt-in, because a release build is ~34s against a suite of
# over 4,500 tests -- but real, which the prose version in this file's docstring
# was not. Rule 17: "run the thing that would show it false."


@pytest.mark.skipif(
    os.environ.get("ST_CHECK_WASM", "") not in {"1", "true", "yes"},
    reason="set ST_CHECK_WASM=1 to build the canister and inspect the wasm (~34s)",
)
def test_the_built_wasm_exports_no_signing_call():
    """The BINARY, not the source. Measured 2026-10-10; figures in the docstring.

    WHY THE BINARY IS A DIFFERENT CHECK FROM THE SOURCE. Every other assertion
    here reads lib.rs, so all of them share one blind spot: a signing call
    reaching the artifact some way the source does not spell -- a macro, a
    dependency's re-export, a build script. This looks at what would actually be
    installed.

    REFUSES RATHER THAN SKIPS IF THE BUILD FAILS. A skip on a failed build is a
    green suite reporting on a canister that does not compile.
    """
    icp = Path(__file__).resolve().parent.parent / "icp"
    # Fixed argv, no shell, and no caller input reaches it.
    build = subprocess.run(
        [
            "cargo",
            "build",
            "--release",
            "--target",
            "wasm32-unknown-unknown",
            "-p",
            "threshold_custody",
        ],
        cwd=icp,
        capture_output=True,
        text=True,
        check=False,
    )
    assert build.returncode == 0, f"the canister did not build:\n{build.stderr[-2000:]}"

    wasm = icp / "target" / "wasm32-unknown-unknown" / "release" / "threshold_custody.wasm"
    assert wasm.is_file(), f"{wasm} was not produced"
    blob = wasm.read_bytes()

    # The two calls increment 1b is ALLOWED to make, both returning public keys.
    for allowed in (b"ecdsa_public_key", b"schnorr_public_key"):
        assert allowed in blob, f"{allowed.decode()} is missing; the keyring cannot answer"

    # `sign_with_schnorr` is the one this increment newly risks: the schnorr module
    # it now imports from is the module that contains it.
    for call in SIGNING_CALLS:
        assert call.encode() not in blob, (
            f"{call} reached the built wasm. The source check passed, so it arrived "
            f"through a macro, a re-export or a dependency -- which is exactly the "
            f"blind spot this test exists for."
        )

    # ONE update and ONE query in the artifact, matching the source assertion above.
    assert blob.count(b"canister_update ") == 1, "more than one update method was exported"
    assert blob.count(b"canister_query ") == 1, "more than one query method was exported"
