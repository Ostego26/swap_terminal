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

import io
from pathlib import Path

import pytest
from chains.monero_keys import decode_address, public_key_for_share
from conftest import RPC_FIXTURE_AUTH, RPC_FIXTURE_USER
from ecdsa import SECP256k1, VerifyingKey
from ecdsa.util import sigdecode_der
from modules import adaptor_swap_chain as chain
from modules.adaptor_swap_scripts import two_of_two_redeem_script, two_of_two_script_sig
from modules.htlc_spend import SIGHASH_ALL, parse_transaction
from regtest import adaptor_join, adaptor_steps
from regtest.console import FAIL, OK, Console
from regtest.daemons import ChainConfig
from regtest.keys import generate_key


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
    leg = adaptor_join.pre_sign_leg(
        "refund", alice.private_key, refund.digest, side.bob_spend, side.bob_spend_public
    )
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
