"""The go-dleq helper process: its pure decisions always, its cryptography when built.

Role: test (read-only; spawns the helper binary in the tests that need it)
Reads: tests/vectors/dleq_cross_curve_go.json, and tools/dleq_helper/dleq_helper
       when it exists
Writes: nothing
Can move funds: no
Mainnet-safe: yes
Live-safe: yes

TWO TIERS, AND WHY THE SPLIT IS DELIBERATE

The decisions in modules/dleq_helper.py -- is this witness in range, do these
commitments match the keys the caller holds, where is the binary -- are pure
functions and are tested with no subprocess at all. They run in every checkout.

The cryptographic round trip needs the Go binary and is skipped without it. That
skip is a real loss of coverage and the reason is written to say so rather than
just naming a missing file: without the binary, nothing here checks that a proof
this repo produces verifies, or that a tampered proof is rejected. The same trap
as the pynacl oracle in test_ed25519_group.py -- a suite reporting `skipped` for
its only end-to-end evidence reads, at a glance, exactly like a suite that
covered it.
"""

from __future__ import annotations

import json
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "swap_terminal"))

from modules.dleq_helper import (
    WITNESS_BITS,
    WITNESS_UPPER_BOUND,
    DleqHelper,
    DleqHelperError,
    DleqVerdict,
    HelperNotFound,
    commitments_match,
    helper_is_available,
    helper_path,
    witness_from_int,
)
from modules.dleq_proof_format import DleqFormatError

VECTOR_PATH = pathlib.Path(__file__).resolve().parents[1] / "tests" / "vectors" / "dleq_cross_curve_go.json"

helper_required = pytest.mark.skipif(
    not helper_is_available(),
    reason=(
        "the go-dleq helper binary is not built, so the ONLY end-to-end evidence in "
        "this file is absent: that a proof round-trips, that the three captured "
        "go-dleq vectors verify, and that a tampered proof is refused. Build it with "
        "`cd tools/dleq_helper && go build -o dleq_helper .` (needs a Go toolchain). "
        "The pure-function tests above this marker still ran"
    ),
)


def _vectors() -> list[dict]:
    with VECTOR_PATH.open() as handle:
        return json.load(handle)


# --- 1. Pure decisions: no subprocess, no binary, every checkout ---------------


def test_witness_bound_is_the_smaller_curves_bit_size():
    assert WITNESS_BITS == 252
    assert WITNESS_UPPER_BOUND == 1 << 252


def test_witness_from_int_encodes_little_endian():
    """Little-endian, because that is how go-dleq reads its [32]byte witness. A
    big-endian encoder produces a perfectly valid proof about a different scalar,
    which is why the endianness is in the function's name and in this assertion."""
    assert witness_from_int(1).hex() == "01" + "00" * 31
    assert witness_from_int(2).hex() == "02" + "00" * 31
    assert witness_from_int(256).hex() == "0001" + "00" * 30
    assert len(witness_from_int(WITNESS_UPPER_BOUND - 1)) == 32


def test_witness_at_or_above_the_bound_is_refused():
    for value in (WITNESS_UPPER_BOUND, WITNESS_UPPER_BOUND + 1, 1 << 255):
        with pytest.raises(DleqHelperError, match="not a scalar on both curves"):
            witness_from_int(value)


def test_a_refused_witness_is_never_quoted_back():
    """The witness is a Monero spend-key share. An exception message reaches logs,
    tracebacks and bug reports, so the refusal reports a BIT COUNT and never the
    integer -- 252 bits short of being the key."""
    secret = WITNESS_UPPER_BOUND + 0xDEADBEEFCAFE
    with pytest.raises(DleqHelperError) as caught:
        witness_from_int(secret)
    message = str(caught.value)
    assert str(secret) not in message
    assert f"{secret:x}" not in message.lower()
    assert "253 bits long" in message

    with pytest.raises(DleqHelperError) as negative:
        witness_from_int(-0xDEADBEEF)
    assert "deadbeef" not in str(negative.value).lower()


def test_commitments_match_is_case_insensitive_and_names_both_sides():
    verdict = DleqVerdict(verified=True, commitment_secp256k1="02AB", commitment_ed25519="CD")
    assert commitments_match(verdict, "02ab", "cd") == (True, "")

    matched, reason = commitments_match(verdict, "0299", "cd")
    assert matched is False
    assert "secp256k1 commitment is 02AB" in reason and "expected 0299" in reason
    assert "ed25519" not in reason, "only the field that differed should be reported"

    matched, reason = commitments_match(verdict, "0299", "ee")
    assert matched is False
    assert "secp256k1" in reason and "ed25519" in reason


def test_helper_path_refuses_rather_than_falling_back(tmp_path, monkeypatch):
    """There is no Python implementation of this construction in the repo and there
    should not be one, so a missing binary has exactly one honest outcome. The
    message has to carry the build command, since that is the whole remedy."""
    monkeypatch.delenv("SWAP_DLEQ_HELPER", raising=False)
    missing = tmp_path / "not_here"
    # Passed explicitly, so the in-tree binary is not a candidate at all -- see
    # test_a_specified_path_that_is_missing_is_never_substituted.
    with pytest.raises(HelperNotFound) as caught:
        helper_path(missing)
    message = str(caught.value)
    assert str(missing) in message
    assert "go build -o dleq_helper" in message
    assert "SWAP_DLEQ_HELPER" in message


def test_helper_path_refuses_a_non_executable_file(tmp_path, monkeypatch):
    """A file that exists and cannot be executed is the shape a half-finished build
    leaves behind, and running it would fail later and less clearly."""
    monkeypatch.delenv("SWAP_DLEQ_HELPER", raising=False)
    present = tmp_path / "dleq_helper"
    present.write_text("not a binary")
    present.chmod(0o644)
    with pytest.raises(HelperNotFound):
        helper_path(present)


def test_helper_path_prefers_the_explicit_argument_over_the_environment(tmp_path, monkeypatch):
    explicit = tmp_path / "explicit"
    explicit.write_text("")
    explicit.chmod(0o755)
    from_environment = tmp_path / "from_env"
    from_environment.write_text("")
    from_environment.chmod(0o755)
    monkeypatch.setenv("SWAP_DLEQ_HELPER", str(from_environment))
    assert helper_path(explicit) == explicit
    assert helper_path() == from_environment


def test_a_specified_path_that_is_missing_is_never_substituted(tmp_path, monkeypatch):
    """This test found the defect it now pins.

    helper_path() was first written as a search over all three candidates, so a
    caller passing a path that did not exist silently got the in-tree binary
    instead. The two refusal tests above passed only while nobody had built that
    copy, and a caller pinning one reviewed build would have run another with no
    signal at all -- substituting one cryptographic implementation for another.
    Both branches are asserted here with the tree binary DELIBERATELY present, so
    this cannot go quiet again the way it did.
    """
    tree_binary = pathlib.Path(__file__).resolve().parents[1] / "tools" / "dleq_helper" / "dleq_helper"
    if not tree_binary.is_file():
        pytest.skip(
            "this test needs the in-tree binary to EXIST -- it is the substitute that "
            "must not be reached for, and with it absent the assertion is vacuous"
        )

    missing = tmp_path / "pinned_build_that_is_not_here"
    with pytest.raises(HelperNotFound, match="does not exist"):
        helper_path(missing)

    monkeypatch.setenv("SWAP_DLEQ_HELPER", str(missing))
    with pytest.raises(HelperNotFound, match=r"\$SWAP_DLEQ_HELPER"):
        helper_path()


def test_using_the_helper_outside_a_context_manager_is_refused():
    """The spawn and the reap are one object (rule 13). A caller that skips `with`
    gets a named refusal rather than a None dereference."""
    if not helper_is_available():
        pytest.skip("needs a located binary to construct the object at all")
    helper = DleqHelper()
    with pytest.raises(DleqHelperError, match="not running"):
        helper.version()


# --- 2. The cryptographic round trip: needs the binary ------------------------


@helper_required
def test_version_reports_the_go_dleq_module_and_the_bit_count():
    """Which implementation produced a proof is not recoverable from its bytes, so
    this is what a stored proof would be attributed with."""
    with DleqHelper() as helper:
        module, bits = helper.version()
    assert bits == WITNESS_BITS
    assert module.startswith("v0.0.0-"), module
    assert "d6fd7c03e213" in module, (
        f"expected the pinned go-dleq commit, got {module} -- a different one is not "
        "wrong, but the captured vectors were made with this one"
    )


@helper_required
@pytest.mark.parametrize("vector", _vectors(), ids=lambda v: v["label"])
def test_the_captured_go_vectors_verify_through_the_helper(vector):
    """The other direction of the vector file: Go produced these bytes months ago,
    and the helper this repo ships verifies them against the keys recorded beside
    them. `verify`, not `verify_any` -- the keys are the point."""
    with DleqHelper() as helper:
        verdict = helper.verify(
            bytes.fromhex(vector["proof_hex"]),
            expect_secp256k1=vector["secp256k1_point"],
            expect_ed25519=vector["ed25519_point"],
        )
    assert verdict.verified, verdict.reason


@helper_required
def test_prove_then_verify_round_trips_and_the_witness_one_gives_the_generators():
    """A witness of 1 must commit to the two curves' own generators, which is a check
    against published constants rather than against the library that produced them."""
    with DleqHelper() as helper:
        result = helper.prove(witness_from_int(1))
        assert result.commitment_secp256k1 == (
            "0279be667ef9dcbbac55a06295ce870b07029bfcdb2dce28d959f2815b16f81798"
        )
        assert result.commitment_ed25519 == "58" + "66" * 31
        verdict = helper.verify(
            result.proof,
            expect_secp256k1=result.commitment_secp256k1,
            expect_ed25519=result.commitment_ed25519,
        )
    assert verdict.verified, verdict.reason


@helper_required
def test_a_verified_proof_about_other_keys_is_reported_as_not_verified():
    """The check whose absence is easiest to mistake for a working verifier. The
    proof below is genuinely valid; it is simply not about the key asked for, and
    `verify` must not return True for it."""
    with DleqHelper() as helper:
        result = helper.prove(witness_from_int(7))
        verdict = helper.verify(
            result.proof,
            expect_secp256k1="02" + "11" * 32,
            expect_ed25519=result.commitment_ed25519,
        )
        assert verdict.verified is False
        assert "about different keys" in verdict.reason
        # ...and the same bytes DO verify when nobody claims which keys they concern,
        # which is what makes this a check on the caller's expectation rather than
        # on the proof.
        assert helper.verify_any(result.proof).verified is True


@helper_required
@pytest.mark.parametrize("offset", [0, 100, 40000, 64800])
def test_a_single_flipped_bit_anywhere_in_a_proof_is_refused(offset):
    """One bit, at four positions: inside the header commitments, inside an early bit
    proof, deep in the middle, and in the last bit proof before the signatures."""
    blob = bytearray(bytes.fromhex(_vectors()[0]["proof_hex"]))
    blob[offset] ^= 0x01
    with DleqHelper() as helper:
        verdict = helper.verify_any(bytes(blob))
    assert verdict.verified is False
    assert verdict.reason


@helper_required
def test_a_malformed_proof_is_refused_by_this_side_before_the_child_sees_it():
    """go-dleq's Deserialize IGNORES trailing bytes, so an appended byte round-trips
    through the child as valid. Framing on this side turns that into a refusal, and
    the refusal is a DleqFormatError -- not a False verdict -- so a caller can tell
    an encoding bug from a cheating counterparty."""
    good = bytes.fromhex(_vectors()[0]["proof_hex"])
    with DleqHelper() as helper:
        with pytest.raises(DleqFormatError, match="trailing byte"):
            helper.verify_any(good + b"\x00")
        with pytest.raises(DleqFormatError):
            helper.verify_any(good[:-1])
        # The process survived both, because a caller's encoding bug must not cost
        # the swap leg the next call is for.
        assert helper.verify_any(good).verified is True


@helper_required
def test_the_child_is_reaped_and_the_absence_is_what_is_asserted():
    """Rule 13: a stop that cannot prove it worked is not a stop. `close()` is
    idempotent and `wait()` is the proof -- it returns only once the process has
    been reaped -- so the assertion is on the exit status existing, not on the
    return value of a kill."""
    helper = DleqHelper()
    with helper:
        helper.version()
        # Reaching into _process on purpose: the test is about the CHILD's lifetime,
        # which no public method exposes and which is exactly what rule 13 asks to be
        # asserted on rather than inferred from a kill's return value.
        process = helper._process
        assert process is not None
        assert process.poll() is None, "the child should be running inside the block"
    assert process.poll() is not None, "the child should be reaped on exit"
    helper.close()  # idempotent: a second close must not raise
