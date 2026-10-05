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

MEASURED ON THE BUILT ARTIFACT TOO, 2026-10-05, which is the check this file
cannot run because it would need the wasm32 target and a network fetch:

    cargo build --release --target wasm32-unknown-unknown
    -> 376425 bytes
       PRESENT  canister_update public_key
       PRESENT  canister_query config
       PRESENT  ecdsa_public_key
       absent   sign_with_ecdsa

So the binary agrees with the source. This file pins the source, which is the
half that can be checked on every run.
"""

from __future__ import annotations

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


def test_the_canister_source_contains_no_signing_call():
    """MUTATION: add `sign_with_ecdsa` anywhere in lib.rs.

    Increment 1's whole safety argument is that it cannot produce a signature.
    Everything else about it -- that it holds no addresses, knows no chains, has
    no timers -- follows from being unable to act on any of them.
    """
    assert SOURCE.is_file(), f"{SOURCE} is missing; the canister was moved or deleted"
    text = SOURCE.read_text()

    for call in SIGNING_CALLS:
        occurrences = [
            line.strip()
            for line in text.splitlines()
            # A line that NAMES the call while explaining its absence is not a call.
            # The distinction is whether it is inside a comment, which for Rust is
            # a line starting // or //! after stripping.
            if call in line and not line.strip().startswith(("//", "*"))
        ]
        assert not occurrences, (
            f"increment 1 must not call {call}; found:\n  " + "\n  ".join(occurrences)
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
