"""Prove and verify cross-curve DLEQ by calling go-dleq as a separate process.

Role: submodule (it spawns a process and speaks a line protocol; the DECISIONS
      it holds -- is this witness in range, do these commitments match the keys
      I already hold -- are pushed down into module-level functions so they can
      be tested with no subprocess at all)
Reads: a helper binary on disk, located per `helper_path()` below. Nothing else:
      no database, no wallet, no RPC endpoint, and the only environment variable
      it reads is SWAP_DLEQ_HELPER, which names that binary.
Writes: nothing to disk. It writes request lines to the child's stdin.
Can move funds: no. It signs nothing and reaches no chain. It is FUND-ADJACENT in
      rule 16's sense and in the strongest way anything in this tree is: the
      witness it passes to `prove` is, in a GRC<->XMR swap, one share of a Monero
      spend key, and `verify` returning True on a proof it should have refused is
      what lets a counterparty commit to different scalars on the two curves,
      take the Gridcoin, and leave the XMR locked to a key nobody holds.
Mainnet-safe: NO, and not for the usual reason. It cannot reach a network. It is
      unsafe in the sense that matters here: the cryptography it invokes is
      UNAUDITED, and the construction has no audited implementation in any
      language. See the next section, which is the most important part of this
      file.
Live-safe: yes to run (no chain, no lock, no state).

UNAUDITED CRYPTOGRAPHY, AND NOTHING CALLS THIS YET

docs/dleq_cross_curve_design.md section 7 is titled "REASONS NOT TO BUILD THIS"
and its first argument is that there is no audited implementation of MRL-0010's
cross-group proof anywhere, in any language -- serai calls its own "solely
experimental, and none are recommended" and its Cypher Stack audit excludes this
feature; sigma_fun calls the module it lives in "highly experimental";
noot/dleq-rs says "not production-ready". go-dleq, the implementation this file
calls, ships no soundness test and its README says MRL-0010 as published "isn't
computationally correct", so it adds proofs the note omits -- unaudited additions
to an unaudited note.

Calling it from a separate process does not fix any of that. What it fixes is a
different and smaller problem, and section 7's own ranking is why this file
exists in this shape rather than as a Python port:

    "Call out to a maintained implementation as a separate process. This is the
    strongest anti-build option and it is not the thing the repo has already
    refused. [...] a small go-dleq helper binary, invoked over stdin/stdout with
    no wallet credentials in its environment, is a different trade entirely --
    no in-process ABI, no C in the interpreter that holds GRC_RPC_PASS, and the
    crypto lives in code other people are running. [...] what is bought is
    shared exposure and other people's bug reports, not a proof."

So: this is the design document's conclusion carried out rather than argued
around. A fourth hand-rolled implementation of the construction is NOT written
here and should not be, and section 7's remaining five arguments -- against
offering GRC<->XMR trustlessly at all until someone commissions a review -- are
untouched by this file and still stand. Nothing in this tree calls it. Name grep
over .py/.js/.sh/.md/.json/.toml/.txt outside node_modules for `dleq_helper`,
`DleqHelper` and `helper_path` hits only this file, its test, and the Go
directory it names.

WHY THE WITNESS GOES OVER stdin AND NOT ON THE COMMAND LINE

argv is world-readable through /proc and `ps`. A Monero spend-key share passed as
an argument is readable by every user on the host for the lifetime of the call,
and by anything sampling the process table. stdin is the only channel to a child
process here that is not exposed that way, which is the entire reason for a line
protocol rather than one invocation per proof. The helper's own docstring carries
the matching rule on its side: no code path formats the witness into an error.

WHAT THIS FILE CHECKS BEFORE AND AFTER THE CHILD, AND WHY BOTH MATTER

BEFORE: every proof is framed by `dleq_proof_format.parse_proof` on the way in.
The child would reject a malformed proof too, but not identically -- go-dleq's
`Deserialize` stops at the last field it needs and IGNORES trailing bytes, so a
proof with a byte appended round-trips through the child as valid. Framing here
first makes that a named refusal instead of a silent acceptance, and it costs a
length comparison against 64 KiB of curve arithmetic.

AFTER: `verify` compares the commitments the child echoes back against the keys
the caller says it expects, and REFUSES when they differ. This is the check whose
absence is easiest to mistake for a working verifier. A DLEQ proof says "the two
points I commit to have the same discrete log"; it says nothing about WHICH
points, so verifying a proof and then using it as evidence about a key you hold
is only sound if you checked that the proof is about that key. The `expect_*`
arguments are therefore not optional conveniences -- `verify` requires them, and
`verify_any` is the separate, explicitly-named call for the case where the
caller genuinely does not know the keys yet.

THE COST THE DESIGN DOCUMENT NAMED, STATED PLAINLY

A Go toolchain in deployment. `helper_path()` does not build anything and does
not fall back to a Python implementation, because there is no Python
implementation to fall back to and a silent fallback would be the worst of the
options. When the binary is absent the error says how to build it, which is two
commands in tools/dleq_helper/README.md.
"""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

from modules.dleq_proof_format import WITNESS_BITS, DleqFormatError, parse_proof

# The witness must be representable as a scalar on BOTH curves, so the bound is
# the smaller group's bit size -- the same 252 the wire format refuses anything
# else for. go-dleq enforces it in checkWitnessSize and would reject a larger
# value; it is checked here as well so the refusal does not require a subprocess
# and so the error names the bound rather than quoting a library.
WITNESS_UPPER_BOUND = 1 << WITNESS_BITS

WITNESS_BYTES = 32
SECP256K1_POINT_HEX_LEN = 66
ED25519_POINT_HEX_LEN = 64

# The directory holding the Go source, relative to the repository root. Used only
# to build the "how to fix this" sentence in HelperNotFound -- nothing imports or
# executes anything from it.
HELPER_SOURCE_DIRECTORY = "tools/dleq_helper"

_HELPER_ENVIRONMENT_VARIABLE = "SWAP_DLEQ_HELPER"


class DleqHelperError(RuntimeError):
    """The helper could not be used, or answered something this file will not accept.

    Distinct from DleqFormatError (the bytes were not a proof) and from a False
    verdict (the proof was well formed and did not verify). Three outcomes, three
    reports: a caller that collapses them cannot tell a missing binary from a
    cheating counterparty.
    """


class HelperNotFound(DleqHelperError):
    """The binary is not where it was looked for, and the message says how to build it."""


@dataclass(frozen=True)
class DleqVerdict:
    """The result of a verify call: the verdict, and the keys the proof is ABOUT.

    `commitment_secp256k1` and `commitment_ed25519` are echoed from the proof the
    child deserialized, not from the request, so a caller comparing them is
    comparing against what was actually proven.
    """

    verified: bool
    commitment_secp256k1: str
    commitment_ed25519: str
    reason: str = ""


@dataclass(frozen=True)
class DleqProofResult:
    """A generated proof and the two public keys it commits to."""

    proof: bytes
    commitment_secp256k1: str
    commitment_ed25519: str


def witness_from_int(value: int) -> bytes:
    """A witness as the 32 little-endian bytes go-dleq expects, or a named refusal.

    Little-endian because that is what go-dleq's [32]byte witness is read as, and
    the endianness is in this function's name for the same reason it is in
    ed25519_group.scalar_from_bytes_le's: a silent disagreement about byte order
    produces a valid-looking proof about a different scalar than the caller meant.
    """
    if value < 0:
        # The value is not quoted back. A witness is a Monero key share, and an
        # exception message reaches logs, tracebacks and bug reports; "it was
        # negative" is the whole of what a caller needs to fix the call.
        raise DleqHelperError("witness must be non-negative")
    if value >= WITNESS_UPPER_BOUND:
        # bit_length() and not the value: a bit count is what a caller needs in
        # order to see that its sampling is wrong, and it is 252 bits short of
        # being the key. Quoting the integer would put a spend-key share in a log.
        raise DleqHelperError(
            f"witness must be below 2^{WITNESS_BITS} and this one is "
            f"{value.bit_length()} bits long -- above that bound it is not a "
            f"scalar on both curves, which is the premise of the proof"
        )
    return value.to_bytes(WITNESS_BYTES, "little")


def helper_path(explicit: str | os.PathLike[str] | None = None) -> Path:
    """Locate the helper binary. A path that was SPECIFIED is used or refused, never
    substituted.

    THE PRECEDENCE, AND WHY A MISSING SPECIFIED PATH IS AN ERROR RATHER THAN A MISS.

    Three sources, most specific first: the `explicit` argument, then
    SWAP_DLEQ_HELPER, then the copy built in the tree. The first one that is
    SPECIFIED decides, and if the binary it names is missing or not executable this
    refuses -- it does NOT try the next one.

    That is not the obvious behavior and it was not the first behavior. Written as a
    search over all three, this function silently ran the in-tree binary when a
    caller passed a path that did not exist -- so a test asserting a refusal for a
    missing path passed only until somebody built the tree copy, and a caller
    pinning a specific reviewed build would have got a different one with no signal.
    Substituting one cryptographic implementation for another because the requested
    one was absent is precisely the failure this repo names in rule 13's "verify the
    artifact, not the deploy": the caller has to be able to check that the pid it
    started is the one it asked for. A search is the right shape for finding a
    default and the wrong shape for honoring an instruction.

    There is no Python implementation of this construction in this repo and there
    should not be one (see the module docstring), so "helper missing" has exactly
    one honest outcome in every branch below.
    """
    if explicit is not None:
        return _require_executable(Path(explicit), "the path passed to helper_path()")

    from_environment = os.environ.get(_HELPER_ENVIRONMENT_VARIABLE)
    if from_environment:
        return _require_executable(
            Path(from_environment), f"${_HELPER_ENVIRONMENT_VARIABLE}"
        )

    repository_root = Path(__file__).resolve().parents[2]
    return _require_executable(
        repository_root / HELPER_SOURCE_DIRECTORY / "dleq_helper",
        "the default location in this repository",
    )


def _require_executable(candidate: Path, source: str) -> Path:
    """The binary at `candidate`, or a refusal naming WHERE that path came from.

    `source` is in the message because the three origins fail for different
    reasons and want different fixes: a caller bug, a stale environment variable,
    or a build that was never run.
    """
    if candidate.is_file() and os.access(candidate, os.X_OK):
        return candidate
    if candidate.exists():
        problem = "exists but is not executable" if candidate.is_file() else "is not a file"
    else:
        problem = "does not exist"
    raise HelperNotFound(
        f"the DLEQ helper at {candidate} ({source}) {problem}. Build it with "
        f"`cd {HELPER_SOURCE_DIRECTORY} && go build -o dleq_helper .` -- a Go toolchain "
        f"is required, and there is deliberately no Python fallback because this repo "
        f"holds no implementation of this construction to fall back to. To use a "
        f"binary elsewhere, set {_HELPER_ENVIRONMENT_VARIABLE} or pass it explicitly"
    )


def _decode_response(line: str) -> dict:
    """One response line, or a named refusal. A blank line means the child died."""
    if not line.strip():
        raise DleqHelperError(
            "the helper closed its output without answering -- it exited, was "
            "killed, or crashed. Its stderr is not captured by this call"
        )
    try:
        payload = json.loads(line)
    except json.JSONDecodeError as error:
        raise DleqHelperError(f"the helper's response is not JSON: {error}") from error
    if not isinstance(payload, dict):
        raise DleqHelperError(f"the helper's response is not an object: {type(payload).__name__}")
    if "ok" not in payload:
        raise DleqHelperError("the helper's response has no `ok` field")
    return payload


def commitments_match(
    verdict: DleqVerdict, expect_secp256k1: str, expect_ed25519: str
) -> tuple[bool, str]:
    """Does this verdict concern the keys the caller expects? Pure; the reason is returned.

    Split out of `verify` so the comparison can be tested without a subprocess,
    and because it is the decision in this file most worth reading on its own: a
    valid proof about the wrong keys is not weaker evidence about the right keys,
    it is no evidence at all.
    """
    mismatches = []
    if verdict.commitment_secp256k1.lower() != expect_secp256k1.lower():
        mismatches.append(
            f"secp256k1 commitment is {verdict.commitment_secp256k1} but the caller "
            f"expected {expect_secp256k1}"
        )
    if verdict.commitment_ed25519.lower() != expect_ed25519.lower():
        mismatches.append(
            f"ed25519 commitment is {verdict.commitment_ed25519} but the caller "
            f"expected {expect_ed25519}"
        )
    if mismatches:
        return False, (
            "the proof verifies, but it is about different keys: "
            + "; and ".join(mismatches)
            + ". A proof about other keys says nothing about these ones"
        )
    return True, ""


class DleqHelper:
    """One long-lived helper process, spoken to over stdin/stdout.

    Use it as a context manager. The process is started on entry and terminated on
    exit, and `close()` is idempotent so an exception inside the block does not
    leave a child behind -- CLAUDE.md rule 13 applied at its smallest scale: the
    spawn and the reap are in one object, and the reap is the `finally` of the
    `with`, not a separate step a caller has to remember.
    """

    def __init__(self, binary: str | os.PathLike[str] | None = None) -> None:
        self._binary = helper_path(binary)
        self._process: subprocess.Popen[str] | None = None

    def __enter__(self) -> DleqHelper:
        self._process = subprocess.Popen(  # noqa: S603  a path this file resolved, no shell
            [str(self._binary)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            # No wallet credentials in the child's environment, which is half the
            # argument for a separate process in the first place. The child reads
            # no environment variable at all, so an empty one loses nothing.
            env={},
        )
        return self

    def __exit__(self, *_exception: object) -> None:
        self.close()

    def close(self) -> None:
        """Terminate the child and prove it is gone. Idempotent.

        Rule 13: "a stop that cannot prove it worked is not a stop." `wait()` is
        the proof -- it returns only once the process has been reaped -- and the
        kill escalation exists because a child blocked writing a 130 KiB response
        into a full pipe will not notice a closed stdin.
        """
        process, self._process = self._process, None
        if process is None:
            return
        try:
            if process.stdin is not None:
                process.stdin.close()
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        finally:
            for stream in (process.stdout, process.stderr):
                if stream is not None:
                    stream.close()

    def _exchange(self, request: dict) -> dict:
        if self._process is None:
            raise DleqHelperError("the helper is not running -- use DleqHelper as a context manager")
        stdin, stdout = self._process.stdin, self._process.stdout
        if stdin is None or stdout is None:
            raise DleqHelperError("the helper has no pipes")
        stdin.write(json.dumps(request) + "\n")
        stdin.flush()
        return _decode_response(stdout.readline())

    def version(self) -> tuple[str, int]:
        """The go-dleq module version the binary was built against, and its bit count.

        Worth recording beside any stored proof: which implementation produced it
        is not recoverable from the bytes.
        """
        payload = self._exchange({"op": "version"})
        if not payload["ok"]:
            raise DleqHelperError(f"version: {payload.get('error', '(no reason given)')}")
        return str(payload.get("go_dleq", "unknown")), int(payload.get("bit_count", 0))

    def prove(self, witness: bytes) -> DleqProofResult:
        """Generate a proof for a 32-byte little-endian witness below 2^252.

        The witness reaches the child over stdin and appears in no argument list,
        no log and no exception message raised here.
        """
        if len(witness) != WITNESS_BYTES:
            raise DleqHelperError(
                f"witness must be {WITNESS_BYTES} bytes, got {len(witness)}"
            )
        if int.from_bytes(witness, "little") >= WITNESS_UPPER_BOUND:
            raise DleqHelperError(
                f"witness is not below 2^{WITNESS_BITS}, so it is not a scalar on both curves"
            )
        payload = self._exchange({"op": "prove", "witness": witness.hex()})
        if not payload["ok"]:
            raise DleqHelperError(f"prove: {payload.get('error', '(no reason given)')}")
        proof = bytes.fromhex(str(payload["proof"]))
        # Frame our own output too. If the helper ever emits something this tree
        # cannot parse, the place to find out is here and not in a counterparty's
        # verifier.
        parse_proof(proof)
        return DleqProofResult(
            proof=proof,
            commitment_secp256k1=str(payload["secp256k1_point"]),
            commitment_ed25519=str(payload["ed25519_point"]),
        )

    def verify_any(self, proof: bytes) -> DleqVerdict:
        """Verify a proof WITHOUT checking which keys it is about.

        Named `verify_any` rather than `verify` on purpose. A true verdict from
        this call means only "some pair of points with equal discrete logs was
        proven"; it is not evidence about any key the caller holds. Use `verify`
        unless the keys are genuinely not known yet, and if they are not, the
        comparison still has to happen before the proof is relied on.
        """
        parse_proof(proof)
        payload = self._exchange({"op": "verify", "proof": proof.hex()})
        if payload["ok"]:
            return DleqVerdict(
                verified=True,
                commitment_secp256k1=str(payload["secp256k1_point"]),
                commitment_ed25519=str(payload["ed25519_point"]),
            )
        return DleqVerdict(
            verified=False,
            commitment_secp256k1="",
            commitment_ed25519="",
            reason=str(payload.get("error", "(no reason given)")),
        )

    def verify(self, proof: bytes, *, expect_secp256k1: str, expect_ed25519: str) -> DleqVerdict:
        """Verify a proof AND that it concerns the two keys the caller names.

        Both expectations are required keyword arguments. See the module docstring:
        a DLEQ proof says the two points it commits to share a discrete log and
        says nothing about which points, so this is the call that makes a verified
        proof mean something about a key in hand.
        """
        verdict = self.verify_any(proof)
        if not verdict.verified:
            return verdict
        matched, reason = commitments_match(verdict, expect_secp256k1, expect_ed25519)
        if matched:
            return verdict
        return DleqVerdict(
            verified=False,
            commitment_secp256k1=verdict.commitment_secp256k1,
            commitment_ed25519=verdict.commitment_ed25519,
            reason=reason,
        )


def helper_is_available(binary: str | os.PathLike[str] | None = None) -> bool:
    """Is the binary built? For a test skip or a readiness report, never for a fallback."""
    try:
        helper_path(binary)
    except HelperNotFound:
        return False
    return True


__all__ = [
    "WITNESS_BITS",
    "WITNESS_UPPER_BOUND",
    "DleqFormatError",
    "DleqHelper",
    "DleqHelperError",
    "DleqProofResult",
    "DleqVerdict",
    "HelperNotFound",
    "commitments_match",
    "helper_is_available",
    "helper_path",
    "witness_from_int",
]
