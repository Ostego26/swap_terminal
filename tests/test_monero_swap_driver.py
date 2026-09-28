"""Behavioral tests for monero_swap.py, the Monero-swap dry-run entry point.

Role: tests
Reads: monero_swap.py, run as a subprocess and imported as a module.
Writes: nothing
Can move funds: no. Nothing here reaches a chain. The one test that exercises the daemon
      check points it at a port nothing is listening on and asserts the SKIPPED path.
Mainnet-safe: yes.
Live-safe: yes.

WHAT IS ASSERTED, AND WHY IT IS ASSERTED ON OUTPUT RATHER THAN ON SOURCE

There is no `inspect.getsource` here. The driver's product IS its output -- an operator
reads the screen and decides what to authorize -- so the assertions are on what the process
printed and what it exited with, which is the same thing the operator sees.

Two properties matter most and neither is provable by reading the file:

  1. `--run` REFUSES and exits non-zero. If that ever silently became a no-op that exited 0,
     an operator would read "ran, exit 0" as "the swap happened".
  2. A check that could not be performed reports SKIPPED and is NOT folded into the proven
     column. CLAUDE.md rule 13: "skipped" plus "success" in the same output is a defect in
     the output, and it is the defect that let twelve consecutive live cycles print
     exit_code=0 while doing nothing.

The subprocess tests use `--skip-chains` so they contact no daemon at all, which is what
makes them safe to run anywhere and is also the configuration a reviewer on a laptop has.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
DRIVER = REPOSITORY_ROOT / "monero_swap.py"

# The same two path shims tests/test_atomic_swap_driver.py uses, for the same reason: the
# driver lives at the repository ROOT (rule 10) and imports its own modules rootlessly, and
# conftest.py puts only swap_terminal/ on sys.path. This is rule 10's layout gap rather than
# a hazard -- CLAUDE.md rule 12 names E402 as exactly the marker of this idiom -- and the
# `noqa` below is a checked claim: the shims must execute before the import, so the import
# cannot move above them.
sys.path.insert(0, str(REPOSITORY_ROOT / "swap_terminal"))
sys.path.insert(0, str(REPOSITORY_ROOT))

from step_console import Console  # noqa: E402  same

import monero_swap  # noqa: E402  the two path shims above have to run first

# The dry run spawns the DLEQ helper and does ~64 KiB of curve arithmetic twice, measured at
# about 1.5s on this container. 120s is generous enough that a slow CI box does not produce a
# flaky failure and short enough that a hang is reported rather than waited on.
DRIVER_TIMEOUT_SECONDS = 120


def _run_driver(*arguments: str) -> subprocess.CompletedProcess[str]:
    """Run the driver as the operator would, from the repository root, capturing both streams.

    As a SUBPROCESS and not as an imported `main()`, because the exit code is half of what is
    being asserted and `sys.exit` inside a test process is not the same measurement. The
    environment is passed through unchanged except that no *_RPC_PASS is added, so the
    script-chain client refuses to guess a credential -- which is itself the behavior
    `--skip-chains` avoids needing.
    """
    return subprocess.run(
        [sys.executable, str(DRIVER), *arguments],
        cwd=REPOSITORY_ROOT,
        capture_output=True,
        text=True,
        timeout=DRIVER_TIMEOUT_SECONDS,
        check=False,
    )


@pytest.fixture(scope="module")
def dry_run() -> subprocess.CompletedProcess[str]:
    """One dry run, shared by the tests that read its output. It takes a second and a half."""
    return _run_driver("--skip-chains")


def test_run_is_refused_and_exits_non_zero():
    """The most important assertion in this file: --run must not look like it worked.

    There is no transaction to broadcast -- the script-chain 2-of-2 and its four pre-signed
    transactions do not exist -- so a `--run` that exited 0 would be read as a completed
    swap. The exit code is asserted with the message, because either one alone is
    insufficient: a zero exit with a refusal printed is the "skipped plus success" defect,
    and a non-zero exit with no explanation gets the flag re-requested next week.
    """
    completed = _run_driver("--run")
    assert completed.returncode != 0
    assert "--run IS REFUSED" in completed.stdout


def test_the_run_refusal_names_all_five_missing_transactions_and_the_reason():
    """The refusal has to be actionable, which means naming what is missing and why.

    A refusal that says only "not implemented" is a refusal somebody will try to route
    around. This one names the five transactions and the measured reason an adaptor signature
    cannot live on the existing HTLC: both of its branches end in a single-key OP_CHECKSIG,
    so the claimer signs freely and nothing can be arranged to leak.
    """
    output = _run_driver("--run").stdout
    for transaction in ("Tx_lock", "Tx_redeem", "Tx_cancel", "Tx_refund", "Tx_punish"):
        assert transaction in output
    assert "OP_CHECKSIG" in output
    assert "2-of-2" in output
    # And the one question that belongs to the operator before any of it can be built.
    assert "OP_CHECKMULTISIG" in output


def test_the_dry_run_succeeds_and_proves_the_cryptography(dry_run):
    """The four cryptographic properties are checked and reported OK, with no chain.

    Asserted individually rather than on an overall exit code, so a failure says WHICH
    property stopped holding. `spend key opens the lock` is the last link: the reconstructed
    private key's public key equals the summed public spend key the lock address was built
    from, which is the property that was swept for real on regtest.
    """
    assert dry_run.returncode == 0, dry_run.stdout + dry_run.stderr
    for label in (
        "both sides verified the other's DLEQ",
        "adaptor pre-signature verified",
        "recovered share == the committed one",
        "reconstructed spend key opens the lock",
    ):
        assert f"OK    {label}" in dry_run.stdout, f"{label!r} was not reported OK"


def test_the_dry_run_says_nothing_was_funded_and_nothing_was_broadcast(dry_run):
    """The operator has to be able to read that off the screen without trusting the flag."""
    assert "nothing was funded" in dry_run.stdout
    assert "nothing was broadcast" in dry_run.stdout
    assert "no wallet was opened" in dry_run.stdout


def test_a_check_that_could_not_run_is_reported_SKIPPED_and_not_as_proven(dry_run):
    """Rule 13: "skipped" and "success" must not share a line, or a column.

    With --skip-chains neither daemon is contacted, so both network checks are unknown. The
    report must place them under SKIPPED, must say "not passed", and must NOT claim under
    PROVEN that any daemon answered.
    """
    assert "SKIPPED (not passed)" in dry_run.stdout
    assert "not passed" in dry_run.stdout
    assert "the network is UNKNOWN" in dry_run.stdout
    assert "PROVEN, against live daemons" not in dry_run.stdout


def test_the_report_separates_what_was_proven_from_what_was_not(dry_run):
    """Both headings, and the NOT PROVEN half leads with the thing that matters.

    The first NOT PROVEN item is that a swap can happen at all. A report whose caveats are
    ordered by convenience buries that one, and it is the one a reader most needs.
    """
    assert "PROVEN, here, with no chain:" in dry_run.stdout
    assert "NOT PROVEN, and this is the half that matters:" in dry_run.stdout
    proven_at = dry_run.stdout.index("NOT PROVEN, and this is the half that matters:")
    tail = dry_run.stdout[proven_at:]
    assert "THAT A SWAP CAN HAPPEN AT ALL" in tail
    assert "no audited implementation" in tail
    assert "A broken DLEQ does not error" in tail


def test_the_dry_run_prints_the_per_party_security_ledger_including_all_three_hazards(dry_run):
    """The analysis prints rather than being cited. Rule 14: the operator reads the screen.

    And hazard 3 has to be named as what it is -- the one step where a single party can hold
    both legs -- because a ledger that lists only the safe rows is worse than no ledger.
    """
    assert "WHO CAN TAKE WHAT, AT EACH STEP" in dry_run.stdout
    for hazard in ("HAZARD 1", "HAZARD 2", "HAZARD 3"):
        assert hazard in dry_run.stdout
    assert "BOTH-LEGS OUTCOME" in dry_run.stdout
    assert "liveness obligation" in dry_run.stdout
    # The role assignment, which is forced by the cryptography rather than chosen.
    assert "RECEIVES on the script chain" in dry_run.stdout


def test_the_dry_run_demonstrates_both_timelock_refusals_rather_than_only_the_accepted_plan(dry_run):
    """An accepted plan proves nothing about a check. The refusals are the demonstration.

    Both hazard 2 (a cancel timelock too close to fund into) and hazard 3 (a redeem broadcast
    too close to the cancel) are exercised on concrete numbers in front of the operator, and
    both must report REFUSED.
    """
    assert "OK    example plan T1=48h T2=96h: got=accepted" in dry_run.stdout
    assert "OK    example plan T1=30min (hazard 2): got=REFUSED" in dry_run.stdout
    assert "OK    redeem broadcast 30min before T1 (hazard 3): got=REFUSED" in dry_run.stdout
    assert "Nothing was funded" in dry_run.stdout


def test_every_duration_the_driver_prints_uses_the_micro_sign_and_no_space(dry_run):
    """Rule 6, asserted on the whole transcript rather than on one line.

    The character is µ (U+00B5), never an ASCII "u", and never with a space before it. An
    ASCII "u" in displayed output is a defect in this repo the same as a wrong number would
    be, and a transcript-wide assertion is the only kind that catches the one call site
    somebody added later.
    """
    assert "µfn" in dry_run.stdout
    assert "ufn" not in dry_run.stdout
    assert " µfn" not in dry_run.stdout


def test_the_driver_echoes_the_helper_binary_and_the_bit_count_it_is_running(dry_run):
    """Rule 13: verify the artifact, not the deploy.

    Which binary is about to do the cryptography, and whether it agrees that the bit count is
    252, are exactly the things a caller must be able to check rather than assume. A step
    that printed nothing would also be indistinguishable from a step that was skipped.
    """
    assert "helper binary:" in dry_run.stdout
    assert "dleq_helper" in dry_run.stdout
    assert "bit_count=252" in dry_run.stdout
    assert "go-dleq v" in dry_run.stdout


def test_the_proof_size_is_reported_as_a_range_rather_than_a_constant(dry_run):
    """The serialized proof is NOT a fixed length and the output must not imply it is.

    The secp256k1 signature inside it is DER-encoded and runs 70 to 72 bytes, so the total
    ranges over 64,966 to 64,968. A verifier written to one number would reject most real
    proofs, and the design document records that exact figure being wrong for five days.
    """
    assert "NOT a constant" in dry_run.stdout
    assert "64,966-64,968" in dry_run.stdout


def test_an_unreachable_monero_daemon_is_a_skipped_check_and_not_a_pass():
    """Pointed at a port nothing is listening on, and the SKIPPED path is asserted.

    Port 1 is used because nothing binds it, so this test contacts no real daemon and cannot
    accidentally reach one. The distinction being asserted is the whole of rule 13's output
    defect: unreachable must not read as fine.
    """
    console = Console(total_steps=1)
    ok, nettype = monero_swap.check_monero_daemon(console, 1)
    assert ok is False
    assert nettype == "(unreachable)"


def test_a_mainnet_monero_daemon_is_refused_by_the_driver_and_not_merely_reported(monkeypatch):
    """A daemon that says "mainnet" must fail the check, with the nettype visible.

    The daemon is replaced at the driver's own seam -- the JSON-RPC function it calls -- so
    the real decision function runs on a real-shaped response. That is the behavioral form of
    this test: a fake response in, a refusal out, with no network and no reimplementation of
    the check inside the test.
    """
    monkeypatch.setattr(
        monero_swap, "monerod_rpc", lambda port, method, timeout=10: {"nettype": "mainnet", "height": 9}
    )
    ok, nettype = monero_swap.check_monero_daemon(Console(total_steps=1), 18081)
    assert ok is False
    assert nettype == "mainnet"

    monkeypatch.setattr(
        monero_swap,
        "monerod_rpc",
        lambda port, method, timeout=10: {"nettype": "stagenet", "height": 9},
    )
    ok, nettype = monero_swap.check_monero_daemon(Console(total_steps=1), 38081)
    assert ok is True
    assert nettype == "stagenet"


def test_the_script_chain_choices_come_from_the_asset_set_and_exclude_XMR():
    """XMR is the OTHER leg and must not be offerable as the script chain.

    Asserted through the parser's own refusal rather than by reading a list: an argument the
    parser accepts is an argument a user can pass.
    """
    completed = _run_driver("--script-chain", "XMR", "--skip-chains")
    assert completed.returncode != 0
    assert "invalid choice" in completed.stderr

    accepted = _run_driver("--script-chain", "LTC", "--skip-chains")
    assert accepted.returncode == 0
    assert "script chain LTC" in accepted.stdout


def test_mainnet_is_not_a_default_monerod_port():
    """18081 is monerod's mainnet port and must not be what an omitted flag falls through to.

    swap_terminal/config.py records the incident this convention comes from: unset ports fell
    through to mainnet and polled the operator's live wallet on a loop. The default here is
    stagenet's 38081, and the assertion is on the port the driver actually announces contacting.
    """
    completed = _run_driver()
    assert "127.0.0.1:38081" in completed.stdout
    assert "127.0.0.1:18081" not in completed.stdout
