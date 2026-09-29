"""THE JOIN, tested: does completing an adaptor signature actually publish the Monero share?

Role: tests (offline; no daemon, no network, no Monero node)
Reads: regtest/adaptor_join.py, regtest/adaptor_steps.py, modules/adaptor_ecdsa.py
Writes: nothing
Can move funds: no
Live-safe: yes -- every key here is generated in the test and controls nothing

WHAT THESE TESTS ARE FOR, AND WHAT THEY ARE NOT FOR. `tests/test_adaptor_ecdsa.py` already
covers the arithmetic: pre_sign/adapt/recover over hundreds of random triples, the low-S `-Y`
branch, the DLEQ. None of that is repeated here. What is here is everything BETWEEN that
arithmetic and a chain -- DER encoding, the SIGHASH byte, finding the signature among a
scriptSig's pushes, telling the adaptor signature apart from the ordinary one beside it, and
the cross-curve close -- because on 2026-09-28 a 2-of-2 P2SH funded and spent on Gridcoin with
two ORDINARY signatures and every check went green. The chain cannot tell the difference. That
is the whole reason the measurement has to be the RECOVERY and not the acceptance.

THE MOST IMPORTANT TEST IN THIS FILE is
`test_an_ordinary_2of2_spend_is_NOT_scored_as_having_published_anything`: it reconstructs
exactly the state that passed on 2026-09-28 -- a well-formed 2-of-2 spend the daemon accepts,
with no adaptor anywhere -- and asserts the harness now scores it FAIL and says why.
"""

from __future__ import annotations

import importlib.util
import io
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from chains.base import RPCError
from chains.monero_keys import decode_address, public_key_for_share, shared_private_spend_key
from conftest import RPC_FIXTURE_AUTH, RPC_FIXTURE_USER
from ecdsa import SECP256k1, VerifyingKey
from ecdsa.util import sigdecode_der
from modules import adaptor_swap_chain as chain
from modules.adaptor_swap_scripts import two_of_two_redeem_script, two_of_two_script_sig
from modules.htlc_spend import SIGHASH_ALL, parse_transaction
from modules.monero_shares_file import load_shares, save_shares
from regtest import adaptor_join, adaptor_steps
from regtest.adaptor_join import swap_handoff
from regtest.console import FAIL, OK, SKIP, Console
from regtest.daemons import ChainConfig
from regtest.keys import double_sha256, generate_key, key_from_seed


@pytest.fixture
def side() -> adaptor_steps.MoneroSide:
    """One swap's Monero half -- both parties' shares and the address they would lock to."""
    return adaptor_steps.monero_side()


@pytest.fixture
def parties():
    return generate_key(), generate_key()


DIGEST = bytes(range(32))


def _leg(bob, side, digest: bytes = DIGEST) -> adaptor_join.AdaptorLeg:
    return adaptor_join.pre_sign_leg(
        "redeem", bob.private_key, digest, side.alice_spend, side.alice_spend_public
    )


def _script_sig(alice, bob, first: bytes, second: bytes) -> bytes:
    return two_of_two_script_sig(first, second, two_of_two_redeem_script(alice.public_key, bob.public_key))


# ---------------------------------------------------------------------------
# The completed signature has to be something a chain would accept at all.
# ---------------------------------------------------------------------------


def test_the_completed_adaptor_is_a_valid_ecdsa_signature_under_the_presigners_key(parties, side):
    """THE PRECONDITION FOR EVERYTHING ELSE. If this were false the spend would simply be
    refused, and the recovery tests below would never run on a real chain -- so it is asserted
    with a standard verifier rather than left to the daemon to discover."""
    _alice, bob = parties
    completed = adaptor_join.complete_leg(_leg(bob, side), side.alice_spend, SIGHASH_ALL)
    assert completed[-1] == SIGHASH_ALL, "the SIGHASH byte is what a legacy scriptSig requires"
    verifier = VerifyingKey.from_string(bob.public_key, curve=SECP256k1)
    assert verifier.verify_digest(completed[:-1], DIGEST, sigdecode=sigdecode_der), (
        "the chain sees an ordinary signature under B_pk -- which is exactly why acceptance "
        "proves nothing and recovery is the measurement"
    )


def test_bobs_own_key_never_produced_those_bytes(parties, side):
    """The point of the scheme, stated as a difference between two byte strings.

    Bob signs the digest ordinarily as well, here, and the two signatures are different -- so
    the one that reaches the chain is genuinely the completed pre-signature and not something
    Bob could have produced without Alice.
    """
    _alice, bob = parties
    completed = adaptor_join.complete_leg(_leg(bob, side), side.alice_spend, SIGHASH_ALL)
    assert completed != bob.sign_digest(DIGEST) + bytes([SIGHASH_ALL])


def test_a_high_S_from_adapt_is_REFUSED_rather_than_quietly_canonized(parties, side, monkeypatch):
    """MUTATION TARGET: replace `der_from_signature`'s raise with `s = N - s` and this goes red.

    It is the only place in the join where the obvious convenience is a money bug. Canonizing
    here would produce a signature the chain accepts and the counterparty cannot recover from --
    a funded Monero leg nobody can open, with no error anywhere -- because
    `recover_adaptor_secret` tests both signs of Y against the negation `adapt` already did, not
    against a second one applied afterwards.
    """
    high = adaptor_join.SECP256K1_ORDER - 1
    assert high > adaptor_join.SECP256K1_HALF_ORDER
    with pytest.raises(adaptor_join.AdaptorJoinError) as raised:
        adaptor_join.der_from_signature((12345, high))
    assert "recover" in str(raised.value), "the message has to say what it costs, not just refuse"


def test_a_pre_signature_that_will_not_verify_is_refused_at_the_moment_it_is_made(parties, side, monkeypatch):
    """A broken pre-signature reaches the chain as a GENERIC script failure, on a harness whose
    entire job is to say whether the script is broken. So it is caught where it is made."""
    monkeypatch.setattr(adaptor_join.adaptor_ecdsa, "pre_verify", lambda *a, **k: False)
    _alice, bob = parties
    with pytest.raises(adaptor_join.AdaptorJoinError):
        _leg(bob, side)


# ---------------------------------------------------------------------------
# Reading the scalar back out of a scriptSig.
# ---------------------------------------------------------------------------


def test_the_scalar_comes_out_of_the_scriptsig_and_the_ORDINARY_signature_beside_it_does_not(parties, side):
    """BOTH HALVES, because either alone is worthless.

    That a scalar came out says the mechanism works. That the OTHER signature in the same
    scriptSig yielded nothing says the recovery found it in the adaptor and not in something
    incidental -- if both leaked, the measurement would be about neither.
    """
    alice, bob = parties
    leg = _leg(bob, side)
    script_sig = _script_sig(
        alice, bob,
        alice.sign_digest(DIGEST) + bytes([SIGHASH_ALL]),
        adaptor_join.complete_leg(leg, side.alice_spend, SIGHASH_ALL),
    )
    evidence = adaptor_join.recover_published_scalar(leg, script_sig, public_key_for_share)
    assert evidence.signatures_seen == 2, "the redeem script and the OP_0 dummy are pushes too"
    assert evidence.recovered == side.alice_spend
    assert evidence.other_signatures_leaked_nothing
    assert evidence.matches_setup_commitment


def test_recovery_is_checked_against_the_share_captured_at_SETUP_not_the_one_it_was_adapted_with(parties, side):
    """THE TAUTOLOGY GUARD. `matches_setup_commitment` compares the recovered scalar's ed25519
    public key against a value captured before the swap ran. Point it at a DIFFERENT share and
    the check must go false -- otherwise it is comparing a value against a derivation of itself
    and would pass unconditionally, which is the shape of a green test that measures nothing.
    """
    alice, bob = parties
    honest = _leg(bob, side)
    mislabeled = adaptor_join.AdaptorLeg(
        label=honest.label, pre_signature=honest.pre_signature,
        adaptor_point=honest.adaptor_point,
        spend_public_at_setup=public_key_for_share(side.bob_spend).hex(),
    )
    script_sig = _script_sig(
        alice, bob,
        alice.sign_digest(DIGEST) + bytes([SIGHASH_ALL]),
        adaptor_join.complete_leg(honest, side.alice_spend, SIGHASH_ALL),
    )
    evidence = adaptor_join.recover_published_scalar(mislabeled, script_sig, public_key_for_share)
    assert evidence.recovered == side.alice_spend, "the secp256k1 half still recovers"
    assert not evidence.matches_setup_commitment, "and the ed25519 half refuses it"


def test_a_plain_two_of_two_scriptsig_leaks_NOTHING(parties, side):
    """Tx_cancel and Tx_punish carry plain signatures on both sides, and the consequence --
    publishing one tells the counterparty nothing -- is a protocol property, so it is measured
    against the bytes rather than argued from the absence of an `adapt` call (rule 17)."""
    alice, bob = parties
    leg = _leg(bob, side)
    plain = _script_sig(
        alice, bob,
        alice.sign_digest(DIGEST) + bytes([SIGHASH_ALL]),
        bob.sign_digest(DIGEST) + bytes([SIGHASH_ALL]),
    )
    assert adaptor_join.nothing_leaks([leg], plain)
    adapted = _script_sig(
        alice, bob,
        alice.sign_digest(DIGEST) + bytes([SIGHASH_ALL]),
        adaptor_join.complete_leg(leg, side.alice_spend, SIGHASH_ALL),
    )
    assert not adaptor_join.nothing_leaks([leg], adapted), (
        "and it says False for one that DOES leak, or it would pass on any input"
    )


def test_TWO_signatures_that_both_yield_the_scalar_is_reported_as_a_defect(parties, side):
    """`other_signatures_leaked_nothing` is what makes the positive result mean anything, and a
    mutant pinning it to True survived until this test existed.

    The scriptSig here carries the SAME completed adaptor twice. That is not a transaction any
    chain would accept -- it is the isolating case for the counter, and the counter is what says
    the recovery found the scalar in the adaptor mechanism rather than in something incidental.
    """
    alice, bob = parties
    leg = _leg(bob, side)
    completed = adaptor_join.complete_leg(leg, side.alice_spend, SIGHASH_ALL)
    evidence = adaptor_join.recover_published_scalar(
        leg, _script_sig(alice, bob, completed, completed), public_key_for_share
    )
    assert evidence.signatures_seen == 2
    assert evidence.recovered == side.alice_spend
    assert not evidence.other_signatures_leaked_nothing, (
        "two of two leaked, so the recovery is not isolating the adaptor and must not report "
        "that it is"
    )


def test_a_scriptsig_that_parses_to_nothing_is_reported_as_zero_signatures_not_as_no_leak(parties, side):
    """rule 14: `(none)` is a result and a blank gap is not. A mangled parse and a transaction
    that genuinely leaks nothing must not render identically, because they want opposite
    responses from whoever reads the line."""
    _alice, bob = parties
    evidence = adaptor_join.recover_published_scalar(_leg(bob, side), b"", public_key_for_share)
    assert evidence.signatures_seen == 0
    assert evidence.recovered is None
    assert not evidence.other_signatures_leaked_nothing


# ---------------------------------------------------------------------------
# The cross-curve close: does the reconstructed key open the lock ADDRESS?
# ---------------------------------------------------------------------------


def test_the_recovered_share_plus_the_other_one_opens_the_lock_address(side):
    assert adaptor_steps.reconstruction_opens_lock(side, side.alice_spend)


def test_a_WRONG_share_does_not_open_it(side):
    """Or the check above would pass for any integer, which is the failure mode
    docs/dleq_cross_curve_design.md section 7 calls total, delayed and looking like an
    operational fault."""
    other = adaptor_steps.monero_side()
    assert not adaptor_steps.reconstruction_opens_lock(side, other.alice_spend)


def test_the_close_is_measured_against_the_ADDRESS_and_not_against_its_inputs(side):
    """The address is DECODED back to its public spend key to make the comparison, so what is
    established is that a party holding the two integers can spend what is at the address a
    counterparty was told to pay -- not that addition is associative."""
    decoded = decode_address(side.lock_address)
    assert decoded.network == adaptor_steps.MONERO_REHEARSAL_NETWORK, "never mainnet"
    assert decoded.public_spend_key != public_key_for_share(side.alice_spend), (
        "the address carries the SUM, so it must not equal either share's public key alone"
    )
    assert decoded.public_spend_key != public_key_for_share(side.bob_spend)


def test_the_two_spend_shares_are_never_rendered_anywhere(side):
    """A Monero spend share is half a private key. The lock address and the two PUBLIC shares
    are printable; the secrets are not, and `MoneroSide` is held for a whole run, so this is
    asserted rather than left to call-site discipline."""
    printed = adaptor_steps.MoneroSide.__doc__ or ""
    for secret in (side.alice_spend, side.bob_spend, side.alice_view, side.bob_view):
        assert str(secret) not in printed
        assert str(secret) not in side.lock_address


# ---------------------------------------------------------------------------
# THE ONE THAT WOULD HAVE CAUGHT 2026-09-28.
# ---------------------------------------------------------------------------


def _decoded_with(script_sig: bytes) -> dict:
    return {"vin": [{"scriptSig": {"hex": script_sig.hex()}}]}


class _Node:
    def __init__(self, decoded: dict) -> None:
        self.decoded = decoded

    def call(self, method: str, *params):
        assert method == "getrawtransaction", f"unexpected {method}"
        return self.decoded


def _run(monkeypatch, decoded: dict) -> tuple[adaptor_steps.Run, Console]:
    console = Console(adaptor_steps.TOTAL_STEPS, stream=io.StringIO())
    run = adaptor_steps.Run(
        console=console,
        config=ChainConfig(
            asset="GRC", daemon_path="x", cli_path="y", datadir=Path("/nonexistent"),
            host="127.0.0.1", port=1, rpc_user=RPC_FIXTURE_USER, rpc_password=RPC_FIXTURE_AUTH,
            conf_name="c.conf", pid_name="c.pid",
        ),
        wallet="",
    )
    monkeypatch.setattr(adaptor_steps, "adapter_for", lambda config, wallet="": _Node(decoded))
    return run, console


def test_the_scriptsig_the_harness_ACTUALLY_BROADCASTS_yields_the_scalar(parties, side):
    """MUTATION TARGET, and the one that was missing until it was checked for.

    Every other test in this file builds its own scriptSig and hands it to the recovery. That
    tests the recovery and says nothing about whether the harness puts an adaptor signature in
    the transaction it sends -- and a mutant replacing `_redeem_signatures`' body with
    `_signatures_in_key_order(...)` (two ordinary signatures, exactly the 2026-09-28 run)
    SURVIVED the whole suite when it was tried. Nothing pinned the wiring.

    So this one goes through the real assembler on the real BuiltChain, takes the bytes that
    would leave the process, and asserts the scalar comes back out of them.
    """
    alice, bob = parties
    built = _built(alice, bob, side, lambda digest: _leg(bob, side, digest))
    raw_hex, _txid = chain.assemble(built.redeem, *adaptor_steps._redeem_signatures(built))
    published = _script_sig_of(raw_hex)
    evidence = adaptor_join.recover_published_scalar(built.redeem_leg, published, public_key_for_share)
    assert evidence.recovered == side.alice_spend, (
        "the transaction the harness would BROADCAST does not publish the share -- the recovery "
        "works and the wiring does not, which is the shape of the defect this file exists for"
    )
    assert evidence.other_signatures_leaked_nothing
    assert evidence.matches_setup_commitment


def test_the_refund_the_harness_ACTUALLY_BROADCASTS_yields_the_OTHER_share(parties, side):
    """The mirror, for the same reason, on the branch that pays Bob.

    Alice pre-signs under Y_b and Bob completes with s_b, so the refund publishes BOB's share
    and not Alice's -- and getting that backwards produces something that looks symmetric and
    hands the wrong party a scalar they already had. Asserted both ways.
    """
    alice, bob = parties
    built = _built(alice, bob, side, lambda digest: _leg(bob, side, digest))
    cancel_output = chain.Outpoint(txid="ef" * 32, vout=0, value_satoshis=built.cancel.output_satoshis)
    refund = chain.build_refund(built.context, cancel_output)
    # THE HARNESS'S OWN function, not a rebuild of it here. Rebuilding is what let a mutant
    # swapping Y_b for Y_a survive: a test that constructs the leg itself can only ever agree
    # with itself.
    leg = adaptor_steps.build_refund_leg(built, refund)
    raw_hex, _txid = chain.assemble(refund, *adaptor_steps._refund_signatures(built, refund, leg))
    evidence = adaptor_join.recover_published_scalar(leg, _script_sig_of(raw_hex), public_key_for_share)
    assert evidence.recovered == side.bob_spend
    assert evidence.recovered != side.alice_spend, "the refund publishes BOB's share, not Alice's"
    assert evidence.other_signatures_leaked_nothing
    assert evidence.matches_setup_commitment


def test_the_cancel_the_harness_ACTUALLY_BROADCASTS_publishes_NOTHING(parties, side):
    """Tx_cancel carries plain signatures on both sides, so either party may publish it and it
    tells neither anything. Measured against the bytes the assembler produces, not argued from
    this file not calling `adapt` (rule 17)."""
    alice, bob = parties
    built = _built(alice, bob, side, lambda digest: _leg(bob, side, digest))
    raw_hex, _txid = chain.assemble(
        built.cancel, *adaptor_steps._signatures_in_key_order(built.setup, built.cancel.digest)
    )
    assert adaptor_join.nothing_leaks([built.redeem_leg], _script_sig_of(raw_hex))


def _script_sig_of(raw_hex: str) -> bytes:
    """Input 0's scriptSig, parsed out of a serialized transaction with the repo's own parser.

    `htlc_spend.parse_transaction` rather than a second parser here (rule 8), and it stands in
    for the daemon's `getrawtransaction` decoding in the offline tests: the bytes are the ones
    the harness would have sent, which is the half these tests are about.
    """
    # (outpoint, script_sig, sequence) -- ParsedTransaction.inputs is a tuple of triples, and
    # serialize() above is the definition of that order.
    _outpoint, script_sig, _sequence = parse_transaction(bytes.fromhex(raw_hex)).inputs[0]
    return script_sig


def test_an_ordinary_2of2_spend_is_NOT_scored_as_having_published_anything(parties, side, monkeypatch):
    """EXACTLY THE 2026-09-28 GRIDCOIN RUN, and it must now come back FAIL.

    That run funded a 2-of-2 P2SH, spent it with two ordinary signatures in key order, saw both
    footguns refused, and scored 40 OK / 0 FAIL -- an honest result about a 2-of-2 and a silent
    zero about the adaptor, because nothing asked. Here the daemon accepts the identical thing
    and the harness has to say the share was not published, name it in a note, and refuse to
    call the reconstruction green.
    """
    alice, bob = parties
    built = _built(alice, bob, side, lambda digest: _leg(bob, side, digest))
    digest = built.redeem.digest
    ordinary = _script_sig(
        alice, bob,
        alice.sign_digest(digest) + bytes([SIGHASH_ALL]),
        bob.sign_digest(digest) + bytes([SIGHASH_ALL]),
    )
    run, _console = _run(monkeypatch, _decoded_with(ordinary))
    outcome = adaptor_steps.ChainOutcome(asset="GRC")

    adaptor_steps._recover_from_the_redeem(run, built, "ab" * 32, outcome)

    assert outcome.redeem_publishes_alice_share == FAIL
    assert outcome.reconstructed_key_opens_lock == FAIL
    note = " ".join(outcome.notes)
    assert "accepted" in note and "recoverable" in note, (
        "the note has to hold both halves of the finding: the chain said yes, and nothing was "
        "published. Either alone reads as an ordinary failure"
    )
    assert "nothing would have failed" in note, (
        "and it has to say what it costs -- a counterparty with a funded Monero leg and no way "
        "to open it, and no error anywhere"
    )
    assert not outcome.established()


def test_the_real_adaptor_spend_IS_scored_as_having_published_it(parties, side, monkeypatch):
    """The other direction, or the test above would pass with `= FAIL` hard-coded."""
    alice, bob = parties
    built = _built(alice, bob, side, lambda digest: _leg(bob, side, digest))
    adapted = _script_sig(
        alice, bob,
        alice.sign_digest(built.redeem.digest) + bytes([SIGHASH_ALL]),
        adaptor_join.complete_leg(built.redeem_leg, side.alice_spend, SIGHASH_ALL),
    )
    run, _console = _run(monkeypatch, _decoded_with(adapted))
    outcome = adaptor_steps.ChainOutcome(asset="GRC")

    adaptor_steps._recover_from_the_redeem(run, built, "ab" * 32, outcome)

    assert outcome.redeem_publishes_alice_share == OK
    assert outcome.reconstructed_key_opens_lock == OK
    assert outcome.notes == [], "a working swap earns no note"


def _built(alice, bob, side, make_leg) -> adaptor_steps.BuiltChain:
    """A real BuiltChain on GRC, with the redeem leg pre-signed over ITS OWN redeem digest.

    `make_leg` is a callable rather than a finished leg because the digest only exists once the
    redeem is built, and pre-signing over some other digest would make the object internally
    inconsistent -- the recovery would still work (it reads the scriptSig and the PreSignature,
    not the built redeem), and it would work for a reason the real run does not have. A fixture
    that passes for the wrong reason is worse than one that fails.

    1 GRC and not 0.01: Gridcoin's fee floor is 0.01 GRC per transaction, so a lock of exactly
    the floor leaves nothing to pay out and `build_redeem` refuses it by name. That refusal is
    correct and is `minimum_lock_value_satoshis()` doing its job; the fixture was wrong.
    """
    script = two_of_two_redeem_script(alice.public_key, bob.public_key)
    setup = adaptor_steps.LockSetup(
        label="T", alice=alice, bob=bob, lock_script=script, cancel_script=script
    )
    context = chain.ChainContext(
        asset="GRC", lock_redeem_script=script, cancel_redeem_script=script,
        alice_script=alice.p2pkh_script, bob_script=bob.p2pkh_script, ntime=1,
    )
    lock = chain.Outpoint(txid="cd" * 32, vout=0, value_satoshis=100_000_000)
    redeem = chain.build_redeem(context, lock)
    return adaptor_steps.BuiltChain(
        setup=setup, context=context, lock_raw_hex="00", lock_txid=lock.txid, lock_vout=0,
        lock_satoshis=lock.value_satoshis, lock_fee=1_000_000,
        redeem=redeem,
        cancel=chain.build_cancel(context, lock, 1_500), t1=1_500, t2=1_650,
        monero=side, redeem_leg=make_leg(redeem.digest),
    )


# ---------------------------------------------------------------------------
# A SECOND RUN ON THE SAME FUNDING. The ordinary case after a successful one.
# ---------------------------------------------------------------------------


def _funding_run(monkeypatch, testmempoolaccept, blocks=None):
    console = Console(adaptor_steps.TOTAL_STEPS, stream=io.StringIO())
    run = adaptor_steps.Run(
        console=console,
        config=ChainConfig(
            asset="GRC", daemon_path="x", cli_path="y", datadir=Path("/nonexistent"),
            host="127.0.0.1", port=1, rpc_user=RPC_FIXTURE_USER, rpc_password=RPC_FIXTURE_AUTH,
            conf_name="c.conf", pid_name="c.pid",
        ),
        wallet="",
    )

    # A CHAIN TO SCAN, because `refuse_if_the_funding_is_already_spent` now walks blocks when
    # testmempoolaccept cannot answer -- which on the operator's Gridcoin is ALWAYS, the method
    # being absent. `blocks` maps height -> decoded block; the default is an empty chain, so the
    # scan runs, finds nothing, and the gate proceeds exactly as it did before it could scan.
    chain_blocks = dict(blocks or {})

    class _Node:
        def call(self, method, *params):
            # listtransactions is allowed because a refused seed now prints the wallet's recent
            # payments before raising -- the recovery route. Anything that SENDS is still
            # refused, which is what this stub is for.
            if method == "listtransactions":
                return []
            if method == "getblockcount":
                return max(chain_blocks) if chain_blocks else 0
            if method == "getblockhash":
                return f"hash-of-{params[0]}"
            if method == "getblock":
                height = int(str(params[0]).rsplit("-", 1)[1])
                return chain_blocks.get(height, {"tx": []})
            assert method == "testmempoolaccept", f"nothing may be BROADCAST here, got {method}"
            # AN EXCEPTION IS A LEGITIMATE ANSWER, and on the operator's Gridcoin it is the only
            # one: the method does not exist and the daemon says `code=-32601 Method not found`.
            # A stub that could only RETURN could not express the case that actually happens.
            if isinstance(testmempoolaccept, Exception):
                raise testmempoolaccept
            return testmempoolaccept

    monkeypatch.setattr(adaptor_steps, "adapter_for", lambda config, wallet="": _Node())
    return run


def test_a_funding_output_a_previous_run_already_spent_is_REFUSED_with_the_remedy(monkeypatch):
    """THE ORDINARY SECOND RUN, and until now it failed with `-22 TX rejected` and nothing else.

    Each run consumes its funding by design, and Gridcoin v5.5.1.0 has neither `importaddress`
    nor `gettxout`, so nothing can say what is unspent at an address the wallet does not own --
    the harness picks the newest payment it remembers and cannot tell a fresh one from a spent
    one. `discover_operator_funding_txid` already wrote that gap down in a comment; this is the
    part that turns it into a sentence the operator can act on.

    A RegtestSetupError and not a FAIL: an unfunded run is a precondition, not the code under
    test breaking, and the two need different things from whoever reads the screen.
    """
    key = generate_key()
    source = chain.Outpoint(txid="ab" * 32, vout=1, value_satoshis=350_000_000)
    run = _funding_run(monkeypatch, [{"allowed": False, "reject-reason": "bad-txns-inputs-missingorspent"}])

    with pytest.raises(adaptor_steps.RegtestSetupError) as raised:
        adaptor_steps.refuse_if_the_funding_is_already_spent(run, key, source, "00")

    message = str(raised.value)
    assert "bad-txns-inputs-missingorspent" in message, "the daemon's own words, not a guess"
    assert "NOTHING was broadcast" in message
    assert key.address in message, "and it names the address to pay, or the remedy is unusable"
    assert f"{source.txid}:{source.vout}" in message


def test_a_daemon_that_will_not_say_does_NOT_turn_into_a_refusal(monkeypatch):
    """An absent diagnostic must never become a gate of its own.

    `testmempoolaccept` is missing on some daemons and answers oddly on others, and every one of
    those cases returns "" from `mempool_reject_reason`. A funded run must proceed exactly as it
    did before this check existed -- otherwise a diagnostic added to make one failure legible has
    made a working run impossible.

    `[{"allowed": False}]` LEFT THIS LIST ON 2026-09-28 and the swap is the point of the change:
    a daemon saying it would not accept the split is the one case here that IS evidence, and
    letting it through is half of how the operator got a bare traceback that day. It is replaced
    by an object with no `allowed` key, which is genuinely unmodeled -- src/rpc/mempool.cpp
    pushes that key first and unconditionally, so its absence means the answer did not come from
    the code this harness models, and refusing an operator's funding on it would be a guess.
    """
    key = generate_key()
    source = chain.Outpoint(txid="ab" * 32, vout=0, value_satoshis=350_000_000)
    for answer in ([{"allowed": True}], [], "not a list", [{"vsize": 200}]):
        run = _funding_run(monkeypatch, answer)
        adaptor_steps.refuse_if_the_funding_is_already_spent(run, key, source, "00")


def test_a_daemon_that_says_NO_without_saying_why_still_refuses(monkeypatch):
    """`allowed: false` and no reject-reason is a refusal, and used to be read as silence.

    This is the case the test above gave up, held here so the change is pinned in both
    directions rather than merely removed from one list. Gridcoin's own mempool.cpp substitutes
    `"rejected"` when validation named nothing, so the word this harness prints is the word the
    daemon would have printed.
    """
    key = generate_key()
    source = chain.Outpoint(txid="ab" * 32, vout=0, value_satoshis=350_000_000)
    run = _funding_run(monkeypatch, [{"allowed": False}])
    with pytest.raises(adaptor_steps.RegtestSetupError) as raised:
        adaptor_steps.refuse_if_the_funding_is_already_spent(run, key, source, "00")
    assert "rejected" in str(raised.value)


def test_split_operator_funding_ASKS_BEFORE_IT_SENDS(monkeypatch):
    """PINS THE CALL SITE, not just the gate.

    Removing the `refuse_if_the_funding_is_already_spent(...)` line from
    `split_operator_funding` killed no test when it was tried with tools/mutate.py -- the gate
    was covered and the wiring was not, which is the same shape as the adaptor mutant that
    survived earlier in this file.

    The stub here REFUSES to answer `sendrawtransaction` at all, so the assertion is that the
    broadcast never happens rather than that an exception happened to be raised first. That
    distinction is the whole point of asking testmempoolaccept: a check that runs after the send
    is not a check, it is a report.
    """
    key = generate_key()
    source = chain.Outpoint(txid="ab" * 32, vout=0, value_satoshis=350_000_000)
    sent: list[str] = []

    class _Node:
        def call(self, method, *params):
            if method == "testmempoolaccept":
                return [{"allowed": False, "reject-reason": "bad-txns-inputs-missingorspent"}]
            sent.append(method)
            raise AssertionError(f"{method} must not be reached -- the funding is already spent")

    run = _funding_run(monkeypatch, [])
    monkeypatch.setattr(adaptor_steps, "adapter_for", lambda config, wallet="": _Node())

    with pytest.raises(adaptor_steps.RegtestSetupError):
        adaptor_steps.split_operator_funding(run, key, source, [generate_key(), generate_key()])

    assert sent == [], f"nothing may reach the daemon beyond the question, but {sent} did"


def test_spender_in_block_FINDS_THE_SPEND_and_ignores_a_coinbase(monkeypatch):
    """The decision, called with seeded inputs and no daemon anywhere near it (rule 10).

    THE COINBASE CASE IS NOT DECORATION. Every Gridcoin block's first transaction has an input
    with no `txid` key at all -- src/rpc/rawtransaction.cpp pushes `coinbase` instead -- so a
    lookup written as `spend["txid"]` raises KeyError on the first input of every block and the
    scan dies before it reaches anything. A crash here would be read as "the daemon will not
    scan" and the gate would wave the spend through, which is the failure this whole function
    exists to stop.

    THE VOUT MUST MATCH TOO. A transaction spending output 0 of the funding payment says nothing
    about output 1, and the operator's funding has consistently been vout 1 -- their GUI puts
    the change first. Matching on txid alone would refuse a perfectly good run.
    """
    block = {"tx": [
        {"txid": "coinbase-tx", "vin": [{"coinbase": "0403", "sequence": 0}]},
        {"txid": "unrelated", "vin": [{"txid": "ff" * 32, "vout": 0}]},
        {"txid": "the-spender", "vin": [{"txid": "ab" * 32, "vout": 1}]},
    ]}
    assert adaptor_steps.spender_in_block(block, "ab" * 32, 1) == "the-spender"
    assert adaptor_steps.spender_in_block(block, "ab" * 32, 0) is None, "a different vout is a different outpoint"
    assert adaptor_steps.spender_in_block({"tx": []}, "ab" * 32, 1) is None
    assert adaptor_steps.spender_in_block({}, "ab" * 32, 1) is None, "a block with no tx key is not a crash"

    # txinfo=false gives bare txid strings, which cannot be judged. Refusing beats guessing.
    with pytest.raises(adaptor_steps.RegtestSetupError):
        adaptor_steps.spender_in_block({"tx": ["just-a-txid"]}, "ab" * 32, 1)


def test_an_ALREADY_SPENT_funding_output_is_REFUSED_by_walking_the_chain(monkeypatch):
    """THE MEASURED CASE, 2026-09-28: the operator's daemon answers none of the easy questions.

        gettxout            absent
        importaddress       absent
        testmempoolaccept   absent -- `code=-32601 Method not found`

    That last one is the finding that forced the scan. Every pre-broadcast check in this repo
    went through `testmempoolaccept` and NONE of them had ever run on this daemon; they returned
    "the daemon will not say" and every caller read that as permission to proceed. A check that
    cannot execute is not a weaker check, it is the absence of one, and it was reporting as
    passing.

    So the chain is asked instead. `getblock(hash, true)` carries every transaction's inputs, so
    one call per block settles it with certainty rather than inference (rule 17).
    """
    key = generate_key()
    source = chain.Outpoint(txid="ab" * 32, vout=1, value_satoshis=460_000_000)
    chain_blocks = {
        7: {"tx": [{"txid": "coinbase-7", "vin": [{"coinbase": "00"}]}]},
        6: {"tx": [{"txid": "what-consumed-it", "vin": [{"txid": "ab" * 32, "vout": 1}]}]},
    }
    run = _funding_run(monkeypatch, adaptor_steps.RPCError("testmempoolaccept: code=-32601 message=Method not found"),
                       blocks=chain_blocks)
    stream = io.StringIO()
    run.console = Console(adaptor_steps.TOTAL_STEPS, stream=stream)

    with pytest.raises(adaptor_steps.RegtestSetupError) as raised:
        adaptor_steps.refuse_if_the_funding_is_already_spent(run, key, source, "00")

    message = str(raised.value)
    assert "ALREADY BEEN SPENT" in message
    assert "what-consumed-it" in message, "and it names the transaction, not just the verdict"
    assert f"{source.txid}:{source.vout}" in message
    assert key.address in message, "and where to pay, or the remedy is unusable"
    assert "Method not found" in stream.getvalue(), "the reason the chain had to be walked is said out loud"


def test_a_daemon_that_cannot_be_SCANNED_does_not_become_a_refusal(monkeypatch):
    """A diagnostic must never become a gate of its own -- the same rule the probe above follows.

    `getblock` failing leaves the harness knowing exactly what it knew before the scan existed,
    which is nothing, and a funded run has to proceed. What it must NOT do is stay quiet about
    it: "no spender found" and "the scan did not happen" are different facts and print
    differently (rule 14).
    """
    key = generate_key()
    source = chain.Outpoint(txid="ab" * 32, vout=1, value_satoshis=460_000_000)
    run = _funding_run(monkeypatch, adaptor_steps.RPCError("testmempoolaccept: code=-32601 message=Method not found"))

    class _Node:
        def call(self, method, *params):
            if method == "getblockcount":
                return 9
            raise adaptor_steps.RPCError(f"{method}: code=-32601 message=Method not found")

    monkeypatch.setattr(adaptor_steps, "adapter_for", lambda config, wallet="": _Node())
    stream = io.StringIO()
    run.console = Console(adaptor_steps.TOTAL_STEPS, stream=stream)

    adaptor_steps.refuse_if_the_funding_is_already_spent(run, key, source, "00")

    printed = stream.getvalue()
    assert "could not scan" in printed, printed
    assert "no transaction in the last" not in printed, "an aborted scan must not read as a clean one"


def test_signing_a_key_DOES_NOT_OWN_is_refused_before_anything_is_signed(monkeypatch):
    """THE GENERAL FORM of the defect that cost four Gridcoin runs on 2026-09-28.

    grc_htlc_verify.py's fund_contract signed with the funding key an output already paid to the
    refund key. Both live in the same process, so the transaction built, signed, serialized and
    predicted its own txid, and every local check passed. Gridcoin answered `-22 TX rejected`,
    which names nothing, and the operator's debug.log held the real answer:

        ERROR: ConnectInputs() : 39c099481d VerifySignature failed

    A signature made with the wrong one of several keys you hold is a WELL-FORMED signature.
    Nothing local can tell -- so the check has to be the one the chain makes: does the previous
    output's scriptPubKey match this key's? One getrawtransaction, asked before signing.

    Fixing the call site alone would leave the next caller free to make the same mistake, which
    is why this sits in reclaim_p2pkh_to_script rather than in the entry point (rule 19: remove
    the cause, not the symptom).
    """
    owner, impostor = generate_key(), generate_key()
    source = chain.Outpoint(txid="ab" * 32, vout=1, value_satoshis=150_000_000)

    class _Node:
        def call(self, method, *params):
            if method == "getrawtransaction":
                return {"vout": [
                    {"n": 0, "value": "1.0", "scriptPubKey": {"hex": "00"}},
                    {"n": 1, "value": "1.5", "scriptPubKey": {"hex": owner.p2pkh_script.hex()}},
                ]}
            raise AssertionError(f"{method} must not be reached -- nothing may be signed or sent")

    run = _funding_run(monkeypatch, [])
    monkeypatch.setattr(adaptor_steps, "adapter_for", lambda config, wallet="": _Node())

    with pytest.raises(adaptor_steps.RegtestSetupError) as raised:
        adaptor_steps.reclaim_p2pkh_to_script(run, impostor, source, b"\x51")

    message = str(raised.value)
    assert "DOES NOT OWN" in message
    assert owner.p2pkh_script.hex() in message, "it names who the output actually pays"
    assert impostor.address in message, "and which key was about to sign for it"
    assert "Nothing was signed or broadcast" in message

    # AND THE RIGHT KEY IS NOT OBSTRUCTED. A guard that refuses the good case too is not a
    # guard, it is an outage -- and it would be invisible in a test that only checks the refusal.
    raw, predicted, value = adaptor_steps.reclaim_p2pkh_to_script(run, owner, source, b"\x51")
    assert value < source.value_satoshis, "the fee comes out, and the spend was built normally"
    assert raw and predicted


def test_a_daemon_that_cannot_say_WHO_OWNS_IT_does_not_block_the_signing(monkeypatch):
    """The ownership check is a diagnostic, not a gate -- the same rule every probe here follows.

    A daemon that will not decode the funding transaction leaves this knowing what it knew
    before the check existed, and signing proceeds. Refusing there would turn an unreachable
    node into a failed run, which is the inversion this module keeps having to guard against.

    OSError is in the caught list for a concrete reason: requests.RequestException derives from
    it, so an unreachable daemon arrives as a ConnectionError rather than an RPCError, and two
    reclaim tests that stub no node at all found that the hard way.
    """
    key = generate_key()
    source = chain.Outpoint(txid="ab" * 32, vout=1, value_satoshis=150_000_000)

    class _Node:
        def call(self, method, *params):
            raise ConnectionError("the daemon is not listening")

    run = _funding_run(monkeypatch, [])
    monkeypatch.setattr(adaptor_steps, "adapter_for", lambda config, wallet="": _Node())

    raw, predicted, value = adaptor_steps.reclaim_p2pkh_to_script(run, key, source, b"\x51")
    assert raw and predicted and value > 0


def test_EXHAUSTED_FUNDING_says_send_more_rather_than_lecturing_about_the_wallet_unlock(monkeypatch):
    """THE WRONG ANSWER, DELIVERED CORRECTLY, 2026-09-28.

    The picker skipped three payments it had proved were spent, found nothing usable, returned
    None -- and `prepare_operator_funding` fell straight through to the WALLET route. On this
    operator's daemon that produced a full screen about staking-only unlocks, `ElevateToFull`,
    the GUI's RAII scope, and passphrases landing in argv. Every sentence of it was true.

    None of it was the thing to fix. They needed to send 1.51 GRC to an address that message
    never mentioned. An accurate answer to a question nobody asked is worse than silence,
    because the reader goes and does the thing it describes -- here, relocking a staking wallet.

    THE SEED'S PRESENCE IS WHAT MAKES IT UNAMBIGUOUS: an operator who set it asked for the
    funded route, and the wallet route is not a fallback for it. So the assertion is on BOTH
    halves -- the address and the amount are named, AND no wallet-unlock advice appears.
    """
    key = generate_key()
    run = _funding_run(monkeypatch, [])
    run.skipped_funding_payments = [("aa" * 32, "what-consumed-it"), ("bb" * 32, "and-this-one")]
    monkeypatch.setattr(adaptor_steps, "operator_funding_key", lambda r: key)
    monkeypatch.setattr(adaptor_steps, "discover_operator_funding_txid", lambda r, k: None)

    with pytest.raises(adaptor_steps.RegtestSetupError) as raised:
        adaptor_steps.prepare_operator_funding(run, "", [generate_key()])

    message = str(raised.value)
    assert key.address in message, "it names the address to pay"
    # DERIVED, NOT SPELLED. Writing "1.51000000" here pinned the CONSTANT rather than the
    # behavior: lowering LOCK_COIN["GRC"] on 2026-09-28 -- a change with no effect on what this
    # message is for -- broke three tests that had nothing to do with it, which is the signature
    # of a test asserting the wrong thing (rule 8: the number lives in one place).
    assert str(adaptor_steps.funding_needed_coins(run)) in message, (
        "and how much, so nobody has to work it out"
    )
    assert "aa" * 32 in message and "what-consumed-it" in message, (
        "and it shows the spent payments it already checked, so the operator can see the run "
        "did not simply fail to look"
    )
    for wrong in ("staking", "walletpassphrase", "ElevateToFull", "passphrase"):
        assert wrong not in message, (
            f"{wrong!r} is true of this daemon and IRRELEVANT here -- the wallet was never asked "
            f"to create anything, and sending them to relock a staking wallet is a real cost"
        )


def test_a_FIRST_run_with_a_seed_and_no_payment_yet_gets_the_same_answer(monkeypatch):
    """Nothing skipped is a different sentence, not a different remedy.

    "Every payment is spent" and "you have not funded it yet" are genuinely different facts and
    an operator reads them differently -- the first says a run completed, the second says none
    has started. Both end at the same line: send this much to this address.
    """
    key = generate_key()
    run = _funding_run(monkeypatch, [])
    monkeypatch.setattr(adaptor_steps, "operator_funding_key", lambda r: key)
    monkeypatch.setattr(adaptor_steps, "discover_operator_funding_txid", lambda r, k: None)

    with pytest.raises(adaptor_steps.RegtestSetupError) as raised:
        adaptor_steps.prepare_operator_funding(run, "", [generate_key()])

    message = str(raised.value)
    assert "no payment to it at all" in message
    assert "before you have funded it" in message
    assert key.address in message and str(adaptor_steps.funding_needed_coins(run)) in message


# A CHAIN WITH NOTHING IN IT. `refuse_if_the_funding_is_already_spent` walks blocks whenever
# testmempoolaccept cannot answer, so every stub that reaches that gate has to serve the walk --
# and a stub that serves it with no blocks is the "not spent, carry on" case, which is what the
# tests below are about. Kept as one table rather than three copies (rule 8).
_AN_EMPTY_CHAIN = {
    "getblockcount": lambda *_: 0,
    "getblockhash": lambda height, *_: f"hash-of-{height}",
    "getblock": lambda *_: {"tx": []},
}


def test_a_daemon_that_says_YES_then_REFUSES_the_send_is_EXPLAINED_not_tracebacked(monkeypatch):
    """THE 2026-09-28 TRACEBACK, held so it cannot come back.

    What the operator actually got, after `grc_htlc_verify.py` located their funding:

        chains.base.RPCError: sendrawtransaction: code=-22 message=TX rejected (HTTP 500)

    preceded by forty lines of Python frames and nothing about the chain. The outpoint, its
    value, the address it sits at and the daemon's own answer to the same question asked without
    broadcasting were all in scope, and none of them reached the screen. Rule 14: pasted output
    has to be self-describing a day later, and a traceback is self-describing about Python.

    THE COMBINATION THIS STUB PRODUCES -- testmempoolaccept says allowed, sendrawtransaction
    refuses -- is not supposed to be possible. Gridcoin's src/validation.cpp runs the identical
    checks either way and `test_only` skips only the pool insertion (read 2026-09-28). So this
    test pins the behavior for the case that is the MOST confusing and the LEAST expected, which
    is the one where an unreadable failure costs the most.
    """
    key = generate_key()
    source = chain.Outpoint(txid="cd" * 32, vout=3, value_satoshis=460_000_000)

    class _Node:
        def call(self, method, *params):
            if method == "testmempoolaccept":
                return [{"allowed": True, "vsize": 226}]
            if method == "sendrawtransaction":
                raise adaptor_steps.RPCError("sendrawtransaction: code=-22 message=TX rejected")
            if method in _AN_EMPTY_CHAIN:
                return _AN_EMPTY_CHAIN[method](*params)
            raise AssertionError(f"{method} must not be reached")

    run = _funding_run(monkeypatch, [])
    monkeypatch.setattr(adaptor_steps, "adapter_for", lambda config, wallet="": _Node())

    with pytest.raises(adaptor_steps.RegtestSetupError) as raised:
        adaptor_steps.send_the_split_or_explain_the_refusal(run, key, source, "00")

    message = str(raised.value)
    assert "code=-22" in message, "the daemon's own words survive"
    assert "WOULD accept" in message, "and so does what it said when asked without sending"
    assert f"{source.txid}:{source.vout}" in message, "which output"
    assert "4.60000000" in message, "and what it was worth"
    assert key.address in message, "and where to pay, or the remedy is unusable"
    assert "reclaim_funding.py" in message, "and how to get back what is already out there"


def test_the_gate_SAYS_WHAT_THE_DAEMON_ANSWERED_even_when_it_does_not_refuse(monkeypatch):
    """A check that prints nothing on the way past is indistinguishable from one that did not run.

    That difference was the whole 2026-09-28 investigation. The gate asked testmempoolaccept,
    did not refuse, printed nothing, and the send then failed -- so there was no way to tell,
    from the operator's pasted output, whether the daemon had said "I would accept this" or
    whether the question had never reached it. Both produce the same blank.

    THREE DISTINCT SENTENCES AND NOT ONE, because the three empty cases are three different
    facts and only the first is evidence of anything (rule 17):

      allowed          the daemon ran AcceptToMemoryPool and would accept it.
      call failed      including a daemon with no `testmempoolaccept`, whose own
                       `code=-32601 Method not found` says so in the text.
      unmodeled shape  something answered, in a shape this code does not model.

    Asserting they DIFFER, rather than asserting three fixed strings, is deliberate: the wording
    is free to improve, and what must not happen is two of them collapsing back into one.
    """
    key = generate_key()
    source = chain.Outpoint(txid="ab" * 32, vout=0, value_satoshis=350_000_000)
    printed = []
    for answer in ([{"allowed": True}], "not a list", [{"vsize": 200}]):
        run = _funding_run(monkeypatch, answer)
        stream = io.StringIO()
        run.console = Console(adaptor_steps.TOTAL_STEPS, stream=stream)
        adaptor_steps.refuse_if_the_funding_is_already_spent(run, key, source, "00")
        text = stream.getvalue()
        assert text.strip(), f"{answer!r} printed nothing at all"
        assert "BEFORE broadcasting" in text, text
        printed.append(text)

    assert len(set(printed)) == 3, f"the three cases must not read alike: {printed}"
    assert "WOULD accept" in printed[0], printed[0]


def test_split_operator_funding_EXPLAINS_A_REFUSED_SEND_rather_than_tracebacking(monkeypatch):
    """PINS THE CALL SITE, not just the explainer.

    The same gap as `test_split_operator_funding_ASKS_BEFORE_IT_SENDS` one function up, and it
    was found the same way: replacing the `send_the_split_or_explain_the_refusal(...)` line with
    the bare `run.node(wallet=False).call("sendrawtransaction", raw)` it used to be killed NO
    test, because the explainer was covered and the wiring was not. A wrapper nothing calls is
    the defect it was written to fix, still present, with a test suite saying otherwise.

    The assertion is on the TYPE as much as the text: a RegtestSetupError is the harness saying
    "your funding needs attention", an RPCError escaping to the top is the harness crashing, and
    the operator reads those two very differently off a screen.
    """
    key = generate_key()
    source = chain.Outpoint(txid="ef" * 32, vout=2, value_satoshis=460_000_000)

    class _Node:
        def call(self, method, *params):
            if method == "testmempoolaccept":
                return [{"allowed": True}]
            if method == "sendrawtransaction":
                raise adaptor_steps.RPCError("sendrawtransaction: code=-22 message=TX rejected")
            if method in _AN_EMPTY_CHAIN:
                return _AN_EMPTY_CHAIN[method](*params)
            raise AssertionError(f"{method} must not be reached")

    run = _funding_run(monkeypatch, [])
    monkeypatch.setattr(adaptor_steps, "adapter_for", lambda config, wallet="": _Node())

    with pytest.raises(adaptor_steps.RegtestSetupError) as raised:
        adaptor_steps.split_operator_funding(run, key, source, [generate_key()])

    message = str(raised.value)
    assert "code=-22" in message, "the daemon's own words reach the operator"
    assert f"{source.txid}:{source.vout}" in message, "and so does the outpoint it refused"
    assert "reclaim_funding.py" in message, "and the way back to the money"


def test_the_split_reports_the_fee_it_ACTUALLY_PAYS(monkeypatch):
    """4.60 in, 1.50 out, and the line said `fee 0.01000000`. It paid 3.09.

    `split_operator_funding` writes NO CHANGE OUTPUT on purpose -- change returning to the
    funding address would be indistinguishable on chain from the operator's next funding
    payment, and `find_operator_funding` would pick it up. The consequence is that the fee is
    input minus outputs, not the size-based number computed for the sufficiency check.

    With the adaptor harness's three destinations the two agree to within a few hundredths and
    nobody looked. grc_htlc_verify.py is the first caller with ONE destination, and on the
    operator's 2026-09-28 run the difference was 3.08 GRC -- two thirds of their funding,
    reported as a hundredth. Rule 14 says state what the number means next to the number.

    AND SINCE THE SAME DAY IT IS NOT BURNED AT ALL. Saying it loudly was the first answer, and
    it survived exactly as long as the requirement was large: a 4.60 payment against a 1.51 ask
    wastes a third of itself, which is a note. Then LOCK_COIN["GRC"] dropped to 0.1, the ask
    became 0.16, and the operator's next payment of 1.00 would have burned 0.84 -- five times
    the requirement. Lowering the ask made overfunding the NORMAL case, and a note about the
    normal case is not a fix (rule 19).

    The surplus now rides on the last funding output. There is still no change output -- change
    returning to the funding address would be indistinguishable on chain from the operator's
    next payment -- but an existing output can simply be larger, which costs nothing, loses
    nothing, and cannot change the transaction's size, because a value is eight bytes whatever
    it holds.
    """
    key = generate_key()
    source = chain.Outpoint(txid="ef" * 32, vout=0, value_satoshis=460_000_000)

    class _Node:
        def call(self, method, *params):
            if method == "testmempoolaccept":
                return [{"allowed": True}]
            if method == "sendrawtransaction":
                raise adaptor_steps.RegtestSetupError("stop here -- the reporting is what is under test")
            if method in _AN_EMPTY_CHAIN:
                return _AN_EMPTY_CHAIN[method](*params)
            raise AssertionError(f"{method} must not be reached")

    run = _funding_run(monkeypatch, [])
    monkeypatch.setattr(adaptor_steps, "adapter_for", lambda config, wallet="": _Node())
    stream = io.StringIO()
    run.console = Console(adaptor_steps.TOTAL_STEPS, stream=stream)

    with pytest.raises(adaptor_steps.RegtestSetupError):
        adaptor_steps.split_operator_funding(run, key, source, [generate_key()])

    printed = stream.getvalue()
    # THE MINER TAKES ONLY THE FEE, and the surplus rides on the funding output. 4.60 in
    # against a 0.15 requirement used to burn 4.44; the miner now gets the size-based fee and
    # the rest stays spendable.
    assert "THE MINER TAKES 0.01000000, which is the size-based fee" in printed, printed
    assert "the LAST one also takes the 4.44000000 of overfunding" in printed, printed
    assert "burned" not in printed, "nothing is burned any more, so nothing may say it is"


# ---------------------------------------------------------------------------
# A PLACEHOLDER SEED. Twice in two days, through two different doors.
# ---------------------------------------------------------------------------


def test_the_seed_that_was_actually_pasted_is_REFUSED(monkeypatch):
    """THE EXACT STRING FROM THE OPERATOR'S TERMINAL, 2026-09-28.

        export ST_ADAPTOR_FUNDING_SEED='<the same seed as yesterday>'

    Single-quoted, so bash passed it through verbatim: a perfectly good non-empty seed that
    derived `msuGYPvo3Fyv64Fvzeg35f6jwN9gThSuwi` where the previous day's real seed had derived
    `msxA9RajhxTvJ4EgPwuiza1VJEYJqdsNqw`. The harness then looked for a payment to an address the
    wallet had never made and refused the run -- correct, and a dead end, because the address it
    offered to be funded was the placeholder's.

    Nothing on that screen was wrong, which is what made it undiagnosable: the only symptom was
    an address differing from last run's, and nobody compares addresses between runs.
    """
    monkeypatch.setenv(adaptor_steps.FUNDING_SEED_VARIABLE, "<the same seed as yesterday>")
    run = _funding_run(monkeypatch, [])
    with pytest.raises(adaptor_steps.RegtestSetupError) as raised:
        adaptor_steps.operator_funding_key(run)
    message = str(raised.value)
    assert "PLACEHOLDER" in message
    assert "WRONG address" in message, "it has to say the seed WORKS and is wrong, not that it is invalid"
    assert adaptor_steps.FUNDING_SEED_VARIABLE in message, "and name the variable to change"


def test_a_refused_seed_still_shows_WHERE_THE_MONEY_WENT(monkeypatch):
    """THE DEAD END THE REFUSAL USED TO BE, measured on the operator's host 2026-09-28.

    The placeholder guard fired -- correctly, on a placeholder I had put in their command for
    the fourth time -- and stopped BEFORE the funding offer. So the screen said "set the seed"
    and named no way to find out WHICH seed: the operator had 4.60 GRC at an address derived
    from a seed no longer in that shell, and nothing on screen mentioned it.

    A refusal that names no way back is half a diagnosis. The wallet knows where it has been
    sending, and the address the operator recognizes is the one whose seed they want -- and
    their shell remembers them setting it.
    """
    paid = "mxRi6srjTKAVrfTvVHo1Lp2kjMoESbfQzz"
    stream = io.StringIO()
    console = Console(adaptor_steps.TOTAL_STEPS, stream=stream)
    run = adaptor_steps.Run(
        console=console,
        config=ChainConfig(
            asset="GRC", daemon_path="x", cli_path="y", datadir=Path("/nonexistent"),
            host="127.0.0.1", port=1, rpc_user=RPC_FIXTURE_USER, rpc_password=RPC_FIXTURE_AUTH,
            conf_name="c.conf", pid_name="c.pid",
        ),
        wallet="",
    )

    class _Wallet:
        def call(self, method, *params):
            assert method == "listtransactions"
            return [{"category": "send", "address": paid, "amount": "-4.60", "confirmations": 2}]

    monkeypatch.setattr(adaptor_steps, "adapter_for", lambda config, wallet="": _Wallet())
    monkeypatch.setenv(adaptor_steps.FUNDING_SEED_VARIABLE, "<your real seed>")

    with pytest.raises(adaptor_steps.RegtestSetupError):
        adaptor_steps.operator_funding_key(run)

    printed = stream.getvalue()
    assert paid in printed, "the address they funded has to be on the screen that refuses them"
    assert "WHICH OF THESE DID YOU FUND" in printed
    assert f"history | grep {adaptor_steps.FUNDING_SEED_VARIABLE}" in printed, (
        "and the one place the lost seed actually is: the shell that set it"
    )
    assert "stranded" in printed, "with the fallback when it is not in history"


def test_an_unset_seed_is_still_None_and_not_a_refusal(monkeypatch):
    """No seed at all is the ORDINARY case on BTC and LTC, where the wallet funds the harness.
    A refusal there would break two working chains to diagnose a third."""
    monkeypatch.delenv(adaptor_steps.FUNDING_SEED_VARIABLE, raising=False)
    assert adaptor_steps.operator_funding_key(_funding_run(monkeypatch, [])) is None
    monkeypatch.setenv(adaptor_steps.FUNDING_SEED_VARIABLE, "   ")
    assert adaptor_steps.operator_funding_key(_funding_run(monkeypatch, [])) is None


def test_a_real_seed_is_NOT_refused_and_is_stable_across_calls(monkeypatch):
    """The refusal is narrow on purpose: locking an operator out of their own funding address is
    strictly worse than the failure it prevents. Anything that is not literally `<...>` passes,
    including seeds that merely CONTAIN an angle bracket."""
    for seed in ("hunter2-not-really", "a<b>c", "<open-only", "close-only>", "<", "<>"):
        monkeypatch.setenv(adaptor_steps.FUNDING_SEED_VARIABLE, seed)
        key = adaptor_steps.operator_funding_key(_funding_run(monkeypatch, []))
        assert key is not None, f"{seed!r} is a legitimate seed and must not be refused"
        assert key.address == key_from_seed(seed, adaptor_steps.FUNDING_ROLE).address, (
            "and the same seed must derive the same address every run -- that stability is the "
            "entire reason the funded route exists"
        )


# ---------------------------------------------------------------------------
# UNMEASURED IS NOT REFUTED. A daemon that will not answer must not read as a broken swap.
# ---------------------------------------------------------------------------


class _MuteNode:
    """A daemon that accepts transactions and will not hand any of them back.

    Not hypothetical: Gridcoin v5.5.1.0 without `-txindex` searches the mempool and the WALLET
    for `getrawtransaction`, and every transaction this harness builds pays keys the wallet has
    never heard of. Once one confirms there is nothing left to find it in.
    """

    def call(self, method, *params):
        raise RPCError(f"{method}: code=-5 No information available about transaction")


def test_a_daemon_that_will_not_hand_the_redeem_back_is_SKIP_and_says_the_spend_was_fine(
    parties, side, monkeypatch
):
    """THE FAILURE MODE THAT WOULD HAVE COST HALF AN HOUR AND POINTED AT THE WRONG THING.

    An unreadable transaction and a transaction that published no scalar are different findings
    with different causes -- a node configuration versus the swap being broken -- and scoring the
    first as FAIL would send somebody into the cryptography for a `-txindex` problem, after
    twenty-eight minutes of waiting for Gridcoin blocks.

    So: SKIP on the decisive outcome, which `established()` already refuses to count as a pass,
    plus a note saying in as many words that the spend itself verified.
    """
    alice, bob = parties
    built = _built(alice, bob, side, lambda digest: _leg(bob, side, digest))
    run = _funding_run(monkeypatch, [])
    monkeypatch.setattr(adaptor_steps, "adapter_for", lambda config, wallet="": _MuteNode())
    outcome = adaptor_steps.ChainOutcome(asset="GRC")

    adaptor_steps._recover_from_the_redeem(run, built, "ab" * 32, outcome)

    assert outcome.redeem_publishes_alice_share == SKIP, "unmeasured, and a SKIP is not a FAIL"
    assert outcome.reconstructed_key_opens_lock == SKIP
    assert not outcome.established(), "and a SKIP is not a pass either"
    note = " ".join(outcome.notes)
    assert "UNMEASURED" in note and "not refuted" in note
    assert "sendrawtransaction runs the interpreter" in note, (
        "it has to say the spend itself verified, or the reader chases the cryptography"
    )


def test_a_scriptsig_the_daemon_omits_is_None_rather_than_empty_bytes(parties, side, monkeypatch):
    """Empty bytes would parse to zero signatures and render as 'published nothing', which is
    the wrong answer wearing the right shape. Three ways a daemon can decline, all None."""
    for decoded in ({"vin": []}, {"vin": [{}]}, {"vin": [{"scriptSig": {}}]}):
        run = _funding_run(monkeypatch, [])
        monkeypatch.setattr(adaptor_steps, "adapter_for", lambda config, wallet="", d=decoded: _Node(d))
        assert adaptor_steps.published_script_sig(run, "ab" * 32) is None


def test_a_scriptsig_the_daemon_DOES_return_still_comes_back(parties, side, monkeypatch):
    """Or the three above would pass with `return None` hard-coded."""
    alice, bob = parties
    leg = _leg(bob, side)
    expected = _script_sig(
        alice, bob,
        alice.sign_digest(DIGEST) + bytes([SIGHASH_ALL]),
        adaptor_join.complete_leg(leg, side.alice_spend, SIGHASH_ALL),
    )
    run = _funding_run(monkeypatch, [])
    monkeypatch.setattr(adaptor_steps, "adapter_for", lambda config, wallet="": _Node(_decoded_with(expected)))
    assert adaptor_steps.published_script_sig(run, "ab" * 32) == expected


# ---------------------------------------------------------------------------
# "DID YOU ALREADY PAY A DIFFERENT ADDRESS?" -- three runs lost to this.
# ---------------------------------------------------------------------------


def test_the_wallets_recent_payments_are_offered_newest_first_without_the_derived_one():
    """The decision, over seeded rows. `listtransactions` returns OLDEST first, so a list that
    forgot to reverse would offer the operator their least recent payment as the likely one."""
    entries = [
        {"category": "send", "address": "mOLDEST", "amount": "-1.0", "confirmations": 900},
        {"category": "receive", "address": "mINBOUND", "amount": "5.0", "confirmations": 50},
        {"category": "send", "address": "mDERIVED", "amount": "-3.5", "confirmations": 3},
        {"category": "send", "address": "mNEWEST", "amount": "-3.5", "confirmations": 1},
    ]
    found = adaptor_steps.recent_payments(entries, exclude="mDERIVED")
    assert [address for address, _amount, _confirmations in found] == ["mNEWEST", "mOLDEST"], (
        "newest first, receives dropped, and the address we derived left out -- offering the one "
        "entry that is NOT the answer among the ones that might be is the cruel version"
    )


def test_a_repeatedly_paid_address_is_offered_once():
    """A funding address paid across several runs would otherwise fill the whole list with
    itself and push the actual answer off the bottom."""
    entries = [
        {"category": "send", "address": "mSAME", "amount": "-3.5", "confirmations": n}
        for n in (300, 200, 100)
    ] + [{"category": "send", "address": "mOTHER", "amount": "-1.0", "confirmations": 2}]
    found = adaptor_steps.recent_payments(entries, exclude="")
    assert [address for address, _a, _c in found] == ["mOTHER", "mSAME"]


def test_the_list_is_bounded_so_it_stays_a_prompt_and_not_a_statement():
    entries = [
        {"category": "send", "address": f"m{n}", "amount": "-1.0", "confirmations": n}
        for n in range(50)
    ]
    assert len(adaptor_steps.recent_payments(entries, exclude="")) == adaptor_steps.RECENT_PAYMENTS_SHOWN


def test_the_diagnostic_names_the_seed_variable_as_the_thing_that_changed(monkeypatch):
    """THE THREE RUNS THIS EXISTS FOR, 2026-09-28: the operator paid msxA9Raj.. -- the address
    from a seed two changes ago -- while the harness went on asking for mxRi6srj.. with no idea
    the money had arrived somewhere it could almost see.

    The remedy is the seed, not another payment, and the message has to say so or the operator's
    next move is to send more coins to a third address.
    """
    key = generate_key()
    stream = io.StringIO()
    console = Console(adaptor_steps.TOTAL_STEPS, stream=stream)
    run = adaptor_steps.Run(
        console=console,
        config=ChainConfig(
            asset="GRC", daemon_path="x", cli_path="y", datadir=Path("/nonexistent"),
            host="127.0.0.1", port=1, rpc_user=RPC_FIXTURE_USER, rpc_password=RPC_FIXTURE_AUTH,
            conf_name="c.conf", pid_name="c.pid",
        ),
        wallet="",
    )
    paid = "msxA9RajhxTvJ4EgPwuiza1VJEYJqdsNqw"

    class _Wallet:
        def call(self, method, *params):
            assert method == "listtransactions"
            return [{"category": "send", "address": paid, "amount": "-3.499", "confirmations": 1}]

    monkeypatch.setattr(adaptor_steps, "adapter_for", lambda config, wallet="": _Wallet())
    adaptor_steps.report_recent_payments(run, key)

    printed = stream.getvalue()
    assert paid in printed, "the address they actually paid has to appear, or there is nothing to recognize"
    assert adaptor_steps.FUNDING_SEED_VARIABLE in printed
    assert "THE SEED IS WHAT CHANGED" in printed
    assert "nothing to re-send" in printed, "or the next move is another payment to a third address"
    assert key.address in printed, (
        "AND THE WAY OUT WHEN THE SEED IS NOT REMEMBERED. Telling somebody to set a seed they no "
        "longer have is a dead end: there is no way to recover one from an address, and this "
        "repository deliberately cannot read one back. The escape is to pay the address THIS "
        "shell derives and carry on -- the stranded coins are test coins and the hunt costs more "
        "than they do"
    )
    assert "DO NOT GO LOOKING" in printed
    assert "new terminal" in printed, (
        "and the recurrence has to be named: an export that does not survive a new shell is how "
        "this happens again next week"
    )


def test_a_wallet_with_no_recent_sends_prints_none_rather_than_a_gap(monkeypatch):
    """rule 14: a blank gap is ambiguous between 'no payments' and 'the query broke'."""
    stream = io.StringIO()
    console = Console(adaptor_steps.TOTAL_STEPS, stream=stream)
    run = adaptor_steps.Run(
        console=console,
        config=ChainConfig(
            asset="GRC", daemon_path="x", cli_path="y", datadir=Path("/nonexistent"),
            host="127.0.0.1", port=1, rpc_user=RPC_FIXTURE_USER, rpc_password=RPC_FIXTURE_AUTH,
            conf_name="c.conf", pid_name="c.pid",
        ),
        wallet="",
    )
    class _Empty:
        def call(self, method, *params):
            assert method == "listtransactions"
            return []

    monkeypatch.setattr(adaptor_steps, "adapter_for", lambda config, wallet="": _Empty())
    adaptor_steps.report_recent_payments(run, generate_key())
    assert "(none:" in stream.getvalue()


def test_a_wallet_that_will_not_list_does_NOT_break_the_funding_offer(monkeypatch):
    """A diagnostic that could refuse a run would be a worse defect than the one it explains."""
    stream = io.StringIO()
    console = Console(adaptor_steps.TOTAL_STEPS, stream=stream)
    run = adaptor_steps.Run(
        console=console,
        config=ChainConfig(
            asset="GRC", daemon_path="x", cli_path="y", datadir=Path("/nonexistent"),
            host="127.0.0.1", port=1, rpc_user=RPC_FIXTURE_USER, rpc_password=RPC_FIXTURE_AUTH,
            conf_name="c.conf", pid_name="c.pid",
        ),
        wallet="",
    )
    monkeypatch.setattr(adaptor_steps, "adapter_for", lambda config, wallet="": _MuteNode())
    adaptor_steps.report_recent_payments(run, generate_key())
    assert "could not list" in stream.getvalue()


def test_offer_funding_route_ACTUALLY_CALLS_the_diagnostic(monkeypatch):
    """PINS THE CALL SITE. THIRD TIME.

    tools/mutate.py, replacing `report_recent_payments(run, funding_key)` in
    `offer_funding_route` with `pass`: SURVIVED. Same shape as the two before it in this file --
    the function was covered and the WIRING was not -- and a diagnostic nothing calls is a
    diagnostic that does not exist, which is precisely the state the three lost runs were in.

    The whole offer is driven, so what is asserted is that the address to fund and the
    did-you-already-pay list arrive on the SAME screen. Either alone is what the operator
    already had.
    """
    stream = io.StringIO()
    console = Console(adaptor_steps.TOTAL_STEPS, stream=stream)
    run = adaptor_steps.Run(
        console=console,
        config=ChainConfig(
            asset="GRC", daemon_path="x", cli_path="y", datadir=Path("/nonexistent"),
            host="127.0.0.1", port=1, rpc_user=RPC_FIXTURE_USER, rpc_password=RPC_FIXTURE_AUTH,
            conf_name="c.conf", pid_name="c.pid",
        ),
        wallet="",
    )
    paid = "msxA9RajhxTvJ4EgPwuiza1VJEYJqdsNqw"
    monkeypatch.setenv(adaptor_steps.FUNDING_SEED_VARIABLE, "a-real-seed-for-this-test")

    class _Wallet:
        def call(self, method, *params):
            assert method == "listtransactions"
            return [{"category": "send", "address": paid, "amount": "-3.499", "confirmations": 1}]

    monkeypatch.setattr(adaptor_steps, "adapter_for", lambda config, wallet="": _Wallet())

    key = adaptor_steps.offer_funding_route(run, "open")

    printed = stream.getvalue()
    assert key is not None
    assert key.address in printed, "the address to fund"
    assert paid in printed, "and, on the SAME screen, where the money actually went"
    assert "DID YOU ALREADY PAY" in printed


# ---------------------------------------------------------------------------
# THE FIFTH TRANSACTION. Four of the five had moved a coin; the punish had moved none.
# ---------------------------------------------------------------------------


class _Chain:
    """A daemon that accepts or refuses on a rule the test supplies, and remembers what it saw.

    `accept` decides per raw transaction hex, so a test can say "refuse this one, accept that
    one" -- which is what the punish path is: the SAME BYTES refused at one height and accepted
    at another. `height` is mutable so `wait_or_mine_to` can be satisfied without a chain.
    """

    def __init__(self, accept, height: int, decoded: dict | None = None) -> None:
        self.accept, self.height, self.decoded = accept, height, decoded
        self.broadcast: list[str] = []

    def call(self, method, *params):
        if method == "getblockcount":
            return self.height
        if method == "sendrawtransaction":
            self.broadcast.append(params[0])
            if not self.accept(params[0]):
                raise RPCError("sendrawtransaction: code=-22 message=TX rejected (HTTP 500)")
            return _txid_of(params[0])
        if method == "getrawtransaction":
            if self.decoded is None:
                raise RPCError("getrawtransaction: code=-5 No information available")
            return self.decoded
        raise RPCError(f"{method}: code=-32601 Method not found")


def _txid_of(raw_hex: str) -> str:
    return double_sha256(bytes.fromhex(raw_hex))[::-1].hex()


def _punish_run(monkeypatch, node) -> tuple[adaptor_steps.Run, io.StringIO, list[int]]:
    """A run whose chain advance is RECORDED rather than merely stubbed out.

    The first version replaced `wait_or_mine_to` with a no-op, and a mutant that DELETED the
    wait to T2 then survived: the stub had already made the wait invisible, so the test could
    not tell a step that waits from one that does not. On a real chain that mutant broadcasts
    the punish before T2 and it is refused -- so the defect would surface, thirty minutes into a
    run, as the branch appearing broken.

    Recording the targets makes the wait an assertion instead of an assumption, which is the
    same move `nothing_leaks` makes against published bytes: a stub that swallows the thing
    under test is a test of the stub.
    """
    stream = io.StringIO()
    waited: list[int] = []
    run = adaptor_steps.Run(
        console=Console(adaptor_steps.TOTAL_STEPS, stream=stream),
        config=ChainConfig(
            asset="LTC", daemon_path="x", cli_path="y", datadir=Path("/nonexistent"),
            host="127.0.0.1", port=1, rpc_user=RPC_FIXTURE_USER, rpc_password=RPC_FIXTURE_AUTH,
            conf_name="c.conf", pid_name="c.pid",
        ),
        wallet="",
    )
    monkeypatch.setattr(adaptor_steps, "adapter_for", lambda config, wallet="": node)
    monkeypatch.setattr(adaptor_steps, "_mine", lambda run, count: None)
    monkeypatch.setattr(adaptor_steps, "wait_or_mine_to", lambda run, target: waited.append(target))
    return run, stream, waited


def _punish_built(parties, side) -> adaptor_steps.BuiltChain:
    alice, bob = parties
    return _built(alice, bob, side, lambda digest: _leg(bob, side, digest))


def test_a_chain_where_the_punish_NEVER_SPENDS_is_scored_FAIL_and_says_what_it_costs(
    parties, side, monkeypatch
):
    """THE MEASUREMENT THAT DID NOT EXIST UNTIL 2026-09-28.

    Tx_punish was broadcast exactly once per run, before T2, and asserted REFUSED. That is the
    timelock holding, and it is the opposite of the branch working -- it says the branch is
    closed when it should be closed and nothing about whether it opens. Four of the five
    transactions the protocol names had moved a coin and this one had moved none.

    Here the chain refuses it at both heights. The run must say so, and must say what a broken
    punish costs: without it a counterparty publishes the cancel and sits on it, and the output
    is locked forever -- T2 is the thing that makes the cancel safe to publish at all.
    """
    built = _punish_built(parties, side)
    cancel_hex, _ = chain.assemble(
        built.cancel, *adaptor_steps._signatures_in_key_order(built.setup, built.cancel.digest)
    )
    node = _Chain(accept=lambda raw: raw == cancel_hex, height=built.t2 + 1)
    run, _stream, waited = _punish_run(monkeypatch, node)
    outcome = adaptor_steps.ChainOutcome(asset="LTC")

    adaptor_steps.step_11_punish_path(run, built, outcome)

    assert waited == [built.t1, built.t2], "T1 for the cancel, then T2 for the punish"
    assert outcome.punish_accepted_after_t2 == FAIL
    note = " ".join(outcome.notes)
    assert "PUNISH BRANCH DOES NOT WORK" in note
    assert "locked forever" in note, "the note has to say what it costs, not just that it failed"


def test_the_punish_SPENDING_at_T2_is_scored_OK(parties, side, monkeypatch):
    """The other direction, or the test above would pass with `= FAIL` hard-coded.

    The chain accepts everything here, which also makes the early-punish control FAIL -- and
    that is correct and asserted: a chain that accepts a non-final transaction has a broken
    timelock, and the run must not report the branch as working on the strength of a daemon
    that would have accepted it at any height.
    """
    built = _punish_built(parties, side)
    node = _Chain(accept=lambda raw: True, height=built.t2 + 1)
    run, stream, waited = _punish_run(monkeypatch, node)
    outcome = adaptor_steps.ChainOutcome(asset="LTC")

    adaptor_steps.step_11_punish_path(run, built, outcome)

    assert waited == [built.t1, built.t2], (
        "the step must ADVANCE THE CHAIN to T2 between the two punish broadcasts. A mutant that "
        "deleted the wait survived until this was asserted -- on a real chain it broadcasts "
        "before T2, is refused, and the branch reads as broken thirty minutes into a run"
    )
    assert outcome.punish_accepted_after_t2 == OK
    printed = stream.getvalue()
    assert "11b the early punish is REFUSED" in printed and FAIL in printed, (
        "a daemon that accepts the EARLY punish too has a broken timelock, and the run must "
        "score that rather than being satisfied by the late acceptance"
    )


def test_the_early_and_at_T2_punish_are_asserted_to_be_the_SAME_BYTES(parties, side, monkeypatch):
    """WHAT ISOLATES nLockTime, and it is the only thing that does on this chain family.

    Gridcoin's refusal names nothing -- `-22 TX rejected` -- so a refusal alone proves only
    "refused for some reason", which a bad signature satisfies equally. The pair is a controlled
    experiment only if the transaction is byte-identical across it, so the harness broadcasts
    the same hex twice and this asserts it did.
    """
    built = _punish_built(parties, side)
    cancel_hex, _ = chain.assemble(
        built.cancel, *adaptor_steps._signatures_in_key_order(built.setup, built.cancel.digest)
    )
    node = _Chain(accept=lambda raw: raw != _last_punish_hex(built), height=built.t2 + 1)
    del cancel_hex
    run, _stream, waited = _punish_run(monkeypatch, node)
    adaptor_steps.step_11_punish_path(run, built, adaptor_steps.ChainOutcome(asset="LTC"))
    assert waited == [built.t1, built.t2]

    punishes = [raw for raw in node.broadcast if raw == _last_punish_hex(built)]
    assert len(punishes) == 2, "broadcast twice -- before T2 and at T2"
    assert punishes[0] == punishes[1], (
        "and identical, or the pair is two transactions rather than one controlled experiment"
    )


def _last_punish_hex(built: adaptor_steps.BuiltChain) -> str:
    """The punish the step will build, rebuilt here from the same inputs.

    Rebuilt rather than captured because the step owns its construction; what this test is for
    is that the step broadcasts ONE transaction twice, and comparing its two broadcasts to each
    other is what establishes that regardless of what this helper returns.
    """
    cancel_sigs = adaptor_steps._signatures_in_key_order(built.setup, built.cancel.digest)
    _, cancel_txid = chain.assemble(built.cancel, *cancel_sigs)
    cancel_output = chain.Outpoint(txid=cancel_txid, vout=0, value_satoshis=built.cancel.output_satoshis)
    punish = chain.build_punish(built.context, cancel_output, built.t2)
    punish_hex, _ = chain.assemble(punish, *adaptor_steps._signatures_in_key_order(built.setup, punish.digest))
    return punish_hex


def test_the_punish_publishes_NOTHING(parties, side, monkeypatch):
    """Plain signatures on both sides, so taking the punish branch must tell the counterparty
    nothing. Measured against the PUBLISHED bytes rather than argued from this file not calling
    `adapt` on that path (rule 17)."""
    built = _punish_built(parties, side)
    published = bytes.fromhex(_last_punish_hex(built))
    _outpoint, script_sig, _sequence = parse_transaction(published).inputs[0]
    node = _Chain(accept=lambda raw: True, height=built.t2 + 1,
                  decoded=_decoded_with(script_sig))
    run, _stream, _waited = _punish_run(monkeypatch, node)
    outcome = adaptor_steps.ChainOutcome(asset="LTC")

    adaptor_steps.step_11_punish_path(run, built, outcome)

    assert outcome.punish_leaks_nothing == OK


# ---------------------------------------------------------------------------
# THE DRIVER'S CALL SEQUENCE. 2154 tests passed against a driver that could not run.
# ---------------------------------------------------------------------------


def _entry_module():
    """adaptor_regtest_verify.py, imported as a module so its functions can be driven."""
    spec = importlib.util.spec_from_file_location(
        "adaptor_regtest_verify_under_test", Path(__file__).resolve().parents[1] / "adaptor_regtest_verify.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_driver_drives_EVERY_lock_through_its_own_branch(monkeypatch, side):
    """THE TEST THAT DID NOT EXIST, AND THE FAILURE IT WOULD HAVE CAUGHT.

    On 2026-09-28 step_4_build_scripts started returning THREE setups and the driver still
    unpacked two. The suite passed -- 2154 tests, ruff clean -- and the operator's run died at
    step 4 with `ValueError: too many values to unpack (expected 2)` after they had funded
    4.60 GRC and waited for a confirmation.

    Nothing tested the driver's call sequence. Every step was covered in isolation and the
    ORDER and ARITY that compose them were not, which is the same defect as the three
    call-site gaps already recorded in this file -- the function is pinned and the wiring is
    not. This is that gap at the top level, where it costs a real run rather than a rerun.

    Every step is replaced with a recorder, so what is asserted is the SEQUENCE: each lock
    reaches the branch it exists for, and lock C reaches the punish.
    """
    entry = _entry_module()
    calls: list[str] = []

    funded: list[str] = []

    def record(name, result=None):
        def recorded(*args, **kwargs):
            calls.append(name)
            if name == "fund_and_prepare":
                # WHICH lock, not just how many. A mutant handing lock C the setup for lock B
                # kept every count right and SURVIVED until this line existed -- and on a real
                # chain it means the punish runs against an output the refund already spent,
                # which is the one answer that proves nothing about T2.
                #
                # args[1] is the LABEL since fund_and_prepare stopped taking a whole LockSetup:
                # it used two of the five fields, and grc_htlc_verify.py funds an HTLC with no
                # LockSetup to hand it.
                funded.append(args[1])
            return result
        return recorded

    fake_setups = tuple(
        adaptor_steps.LockSetup(label=f"{n}", alice=generate_key(), bob=generate_key(),
                                lock_script=b"\x01", cancel_script=b"\x01")
        for n in "ABC"
    )
    built = object()
    for name, result in (
        ("fund_and_prepare", None), ("step_6_build_and_hold", built),
        ("step_7_broadcast_lock", None), ("step_8_refusals", None),
        ("step_9_happy_path", None), ("step_10_cancel_path", None),
        ("step_11_punish_path", None), ("current_height", 100),
    ):
        monkeypatch.setattr(entry.adaptor_steps, name, record(name, result))

    class _Run:
        asset = "GRC"
        console = Console(adaptor_steps.TOTAL_STEPS, stream=io.StringIO())

    outcome = adaptor_steps.ChainOutcome(asset="GRC")
    entry.drive_locks(_Run(), fake_setups, side, 100, outcome)

    assert calls.count("fund_and_prepare") == adaptor_steps.LOCKS_PER_RUN, (
        "every lock must be funded, or one branch runs against an output another already spent"
    )
    assert funded == ["A", "B", "C"], (
        f"each branch must be driven on its OWN lock, in order; got {funded}. A lock spends "
        f"once, so two branches sharing one means the second is refused because the output is "
        f"gone -- which proves nothing about the branch"
    )
    assert calls.count("step_6_build_and_hold") == adaptor_steps.LOCKS_PER_RUN
    assert calls.count("step_7_broadcast_lock") == adaptor_steps.LOCKS_PER_RUN
    for terminal in ("step_8_refusals", "step_9_happy_path", "step_10_cancel_path", "step_11_punish_path"):
        assert calls.count(terminal) == 1, f"{terminal} must run exactly once, on its own lock"
    assert calls.index("step_9_happy_path") < calls.index("step_10_cancel_path") < calls.index("step_11_punish_path")
    assert calls.index("step_8_refusals") < calls.index("step_9_happy_path"), (
        "the footgun refusals spend the SAME output as the happy path, so a refusal that "
        "arrived after it would be 'already spent' and would prove nothing about the script"
    )


def test_the_driver_unpacks_exactly_what_step_4_returns(monkeypatch, side):
    """THE ARITY, pinned against the real function rather than a fixture.

    `drive_locks` unpacks the setups tuple. The real `step_4_build_scripts` is what fills it,
    so this drives one into the other -- which is precisely the seam that broke, and the
    seam a test using its own three-element fixture would have stepped straight over.
    """
    entry = _entry_module()
    console = Console(adaptor_steps.TOTAL_STEPS, stream=io.StringIO())
    run = adaptor_steps.Run(
        console=console,
        config=ChainConfig(
            asset="GRC", daemon_path="x", cli_path="y", datadir=Path("/nonexistent"),
            host="127.0.0.1", port=1, rpc_user=RPC_FIXTURE_USER, rpc_password=RPC_FIXTURE_AUTH,
            conf_name="c.conf", pid_name="c.pid",
        ),
        wallet="",
    )
    real_setups = adaptor_steps.step_4_build_scripts(run)

    for name in ("fund_and_prepare", "step_6_build_and_hold", "step_7_broadcast_lock",
                 "step_8_refusals", "step_9_happy_path", "step_10_cancel_path",
                 "step_11_punish_path", "current_height"):
        monkeypatch.setattr(entry.adaptor_steps, name, lambda *a, **k: 100)

    entry.drive_locks(run, real_setups, side, 100, adaptor_steps.ChainOutcome(asset="GRC"))


def test_THE_HANDOFF_CARRIES_THE_RECOVERED_SHARE_NOT_THE_SAMPLED_ONE():
    """GAP (e)'s missing hop, and the one substitution that would fake it.

    The 2026-09-28 Gridcoin run moved a coin through all five transactions, recovered `s_a` out
    of a scriptSig the daemon had accepted, and proved `s_a(recovered) + s_b` reconstructs a key
    whose public key is the Monero lock address's. Then the run ended and the two integers went
    out of scope -- docs/gridcoin_adaptor_join_2026_09_28.md says it in capitals: NO MONERO
    MOVED, the lock address was derived and never funded.

    THE HARNESS HOLDS BOTH SHARES AND THEY ARE EQUAL WHEN EVERYTHING WORKS. So a handoff built
    from `side.alice_spend` would sweep perfectly and prove NOTHING -- the scalar would never
    have touched a chain, and the end-to-end claim would rest on a value this process had all
    along. That is a fake supplying its own answer, and it is the reason the recovered share is
    passed in explicitly rather than read off `side`.

    This test therefore hands it a share DIFFERENT from the one on `side`, and asserts the
    difference survives into the fixture. If someone swaps the argument for the field, the two
    are equal in every real run and only this asserts otherwise.
    """
    side = adaptor_steps.MoneroSide(
        alice_spend=111, alice_view=222, bob_spend=333, bob_view=444,
        lock_address="5Bwhatever", alice_spend_public="aa" * 32, bob_spend_public="bb" * 32,
    )
    recovered = 999  # deliberately NOT side.alice_spend
    payload = swap_handoff(side, recovered)

    assert payload["spend_share_a"] == recovered
    assert payload["spend_share_a"] != side.alice_spend, (
        "the whole point: the scalar in the fixture is the one a CHAIN published"
    )
    assert payload["spend_share_b"] == side.bob_spend
    assert payload["spend_summed"] == shared_private_spend_key(recovered, side.bob_spend), (
        "and the sum is over the RECOVERED share, not over the sampled one -- a sum taken the "
        "other way would load cleanly and open a different address"
    )
    assert payload["view_summed"] == shared_private_spend_key(side.alice_view, side.bob_view)


def test_THE_HANDOFF_IS_READABLE_BY_THE_SWEEPER_THAT_HAS_TO_READ_IT(tmp_path):
    """One format, one implementation -- asserted by round-tripping through the REAL loader.

    modules/monero_shares_file.py holds it precisely because there are now two writers, and two
    writers of one format is rule 8's bug with a delay on it. A test that checked the keys by
    hand would be a third statement of the format and would drift with the other two; this loads
    the file with the function `monero_shared_key_verify.py --sweep` actually calls, so a change
    to either side fails here.
    """
    side = adaptor_steps.MoneroSide(
        alice_spend=111, alice_view=222, bob_spend=333, bob_view=444,
        lock_address="5Bwhatever", alice_spend_public="aa" * 32, bob_spend_public="bb" * 32,
    )
    path = tmp_path / "swap-handoff-shares.json"
    save_shares(path, swap_handoff(side, 999), side.lock_address, "stagenet")

    loaded = load_shares(path)
    assert loaded["spend_share_a"] == 999
    assert loaded["shared_address"] == "5Bwhatever"
    assert path.stat().st_mode & 0o777 == 0o600, "private scalars, even on a test network"


def test_A_HANDOFF_IS_NEVER_WRITTEN_FOR_A_NON_TEST_NETWORK(monkeypatch, tmp_path):
    """PRIVATE SPEND SHARES IN THE CLEAR. Correct on stagenet, a key disclosure anywhere else.

    The network is ASKED of the address -- decoded, not inferred from a flag or a port -- which
    is the same rule every other network gate in this tree follows. And the refusal is a
    sentence rather than an exception: the run's findings do not depend on this file, so a
    refusal to write one must not turn a green run red.
    """
    said: list[str] = []

    class _Run:
        asset = "GRC"

        def say(self, line=""):
            said.append(line)

    monkeypatch.setenv(adaptor_steps.HANDOFF_VARIABLE, str(tmp_path / "must-not-exist.json"))
    monkeypatch.setattr(adaptor_steps, "decode_address",
                        lambda _a: SimpleNamespace(network="mainnet", public_spend_key=b""))
    side = adaptor_steps.MoneroSide(
        alice_spend=111, alice_view=222, bob_spend=333, bob_view=444,
        lock_address="4Mainnetlooking", alice_spend_public="aa" * 32, bob_spend_public="bb" * 32,
    )
    adaptor_steps.write_the_swap_handoff(_Run(), side, 999)

    assert not (tmp_path / "must-not-exist.json").exists(), "nothing written off a test network"
    assert any("NOT writing a swap handoff" in line for line in said)
    assert any("key disclosure" in line for line in said), "and it says WHY, not just that"


def test_THE_HANDOFFS_PUBLIC_KEYS_ARE_THE_SUMS_AND_ARE_JSON_WRITABLE():
    """Both halves were wrong in the first version, and only one of them was the reported bug.

    A code review on 2026-09-29 flagged `"public_spend": side.alice_spend_public` and
    `"public_view": ""` as looking wrong and could not tell whether they mattered. They were
    wrong and they decide NOTHING -- monero_shared_key_verify.address_for() rebuilds the
    address from the four SCALARS. What reads them is print_plan(), which labels them "public
    spend key (sum)" and "public view key (sum)". So the file carried Alice's share alone under
    a label saying sum, and a blank under the other: a wrong value a human reads.

    AND THE FIX FOR IT NEARLY BROKE THE WRITE. shared_public_key returns BYTES; save_shares
    json.dumps() the payload. The first version omitted .hex() and would have thrown TypeError
    on the call that produces the handoff -- turning a wrong display value into no file at all,
    on the path that closes gap (e). Both properties are pinned here because fixing one is what
    introduced the other.
    """
    side = adaptor_steps.monero_side()
    payload = swap_handoff(side, side.alice_spend)

    json.dumps(payload)  # raises TypeError on bytes; that is the assertion
    assert isinstance(payload["public_spend"], str)
    assert payload["public_spend"] == decode_address(side.lock_address).public_spend_key.hex(), (
        "the SUM, checked against the address's own spend key rather than against the inputs "
        "that built it -- the same standard reconstruction_opens_lock() holds itself to"
    )
    assert payload["public_view"] and payload["public_view"] != payload["public_spend"]
    assert payload["public_spend"] != side.alice_spend_public, "the reported bug, gone"
