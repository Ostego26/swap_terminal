"""THE JOIN: adaptor signatures on the script chain, and the Monero scalar they publish.

Role: function level (the decisions -- pre-sign, adapt, encode, parse back, recover)
Reads: nothing. Every value is passed in.
Writes: nothing. No key, share or scalar is ever printed, logged or returned as text.
Can move funds: no directly. It produces signature bytes that regtest.adaptor_steps puts
        into a scriptSig, and those spends move testnet coins.
Mainnet-safe: NO, and not because of anything in this file. It composes
        modules/adaptor_ecdsa.py, whose own header says "unaudited, NOT WIRED IN, NOT FOR
        MAINNET", and that judgment is unchanged by this module existing. What this adds
        is a regtest exercise of it, which is the thing that was missing -- not an audit.

WHAT WAS MISSING, MEASURED RATHER THAN SUSPECTED. On 2026-09-28 a 2-of-2 P2SH funded and
spent on Gridcoin testnet, both branches, both OP_CHECKMULTISIG footguns refused --
`adaptor_regtest_verify.py --chain grc`, 40 OK / 0 FAIL, redeem
e75f257a8620a39b.., refund 91e096f5b821ea09... And grepping `adaptor_ecdsa`, `pre_sign`,
`complete_signature` and `recover` across `adaptor_regtest_verify.py` and
`regtest/adaptor_steps.py` returned ZERO occurrences. That spend used two ORDINARY
signatures. So the single property an adaptor signature exists for -- that the spender
cannot sign alone, and that COMPLETING the signature is what publishes the scalar -- had
never been exercised against a consensus rule anywhere in this tree.

docs/monero_swap_protocol.md section 2 named that gap and called it "narrower and harder:
THE JOIN". This module is it, and the honest scope is one sentence: the arithmetic in
`modules/adaptor_ecdsa.py` was already tested in isolation and is not re-tested here; what
is new is that its output now has to survive a real script interpreter, and that a scalar
is read back out of bytes a daemon accepted rather than out of a variable this process
still holds.

WHICH PARTY PRE-SIGNS WHICH TRANSACTION, AND WHY IT IS NOT SYMMETRIC.
docs/monero_swap_protocol.md section 0 derives it; repeated here because the two call
sites are 400 lines apart in adaptor_steps.py and getting it backwards produces something
that looks symmetric and loses money:

    Tx_redeem   pays ALICE (the XMR holder, who wants S-coin). BOB pre-signs under Y_a =
                s_a*G. Alice completes it with her own Monero spend share s_a, and
                broadcasting it hands s_a to Bob -- who needs exactly that to open the
                Monero lock.
    Tx_refund   pays BOB. ALICE pre-signs under Y_b = s_b*G. Bob completes it with s_b,
                and broadcasting hands s_b to Alice.
    Tx_cancel   plain signatures from both. Nothing leaks; either party may publish it
                once T1 passes.
    Tx_punish   plain signatures from both. Nothing leaks, and `nothing_leaks()` below is
                the assertion that says so rather than the absence of a call saying it.

THE ADAPTOR POINT IS THE MONERO SPEND SHARE'S secp256k1 PUBLIC KEY, and that is the whole
cross-curve trick: the same integer s_a is the discrete log of Y_a to secp256k1's G and of
S_a to ed25519's B. Nothing in this file proves that -- `modules/dleq_helper.py` does, and
`modules/monero_swap_protocol.commit_shares` is where the proof is made and cross-checked.
Here both parties are played by one process, so the shares are simply known, and the
recovery check below therefore compares the recovered scalar against a public share
CAPTURED AT SETUP rather than against the variable it was adapted from. That distinction
is the difference between a measurement and a tautology, and it is why
`RecoveryEvidence.spend_public_at_setup` exists as a field instead of being recomputed.

NEVER ADD THE TWO ADAPTOR POINTS. docs/monero_swap_protocol.md section 1.1: measured,
2000 random share pairs from [1, 2^252), 961 of them (48.0%) sum to at least `l`, so
`Y_a + Y_b` on secp256k1 and `S_a + S_b` on ed25519 commit to DIFFERENT INTEGERS about
half the time, silently. The addition happens on the ed25519 side only, in
`monero_swap_protocol.reconstruct_spend_key`, after both shares are known as integers.
This module never adds a secp256k1 point to another one at all.
"""

from __future__ import annotations

from dataclasses import dataclass

from ecdsa import SECP256k1
from ecdsa.util import sigdecode_der, sigencode_der
from modules import adaptor_ecdsa
from modules.htlc_spend import script_pushes

SECP256K1_ORDER = SECP256k1.order
# BIP62's low-S boundary. `adaptor_ecdsa.adapt` already negates above it; this constant
# exists so `der_from_signature` can ASSERT that rather than silently re-canonizing, for
# the reason that function's docstring gives.
SECP256K1_HALF_ORDER = SECP256K1_ORDER // 2


class AdaptorJoinError(RuntimeError):
    """The join itself is broken -- not the chain, and not the counterparty.

    Kept separate from `regtest.daemons.RegtestSetupError` (a precondition the operator
    can fix) and from a plain FAIL (the chain answered, and the answer was wrong). Every
    raise below is a statement that this repository's own code disagrees with itself: a
    pre-signature that will not verify under the key that just made it, or an `adapt`
    result that is not low-S after `adapt` promised it would be.
    """


def der_from_signature(signature: tuple[int, int]) -> bytes:
    """`(r, s)` from `adaptor_ecdsa.adapt` as the DER a scriptSig carries.

    LOW-S IS ASSERTED, NOT IMPOSED, and that is the only interesting line in this
    function. `ecdsa.util.sigencode_der_canonize` -- which `regtest/keys.RegtestKey.
    sign_digest` uses, and which is the obvious thing to reach for here -- would negate a
    high `s` for us. It must not, and the reason is specific rather than stylistic:
    `adaptor_ecdsa.adapt` ALREADY negated for BIP62, and `recover_adaptor_secret` tests
    both signs of the adaptor point precisely because of that negation. A second,
    invisible negation between `adapt` and the wire would leave a signature the chain
    accepts and the counterparty cannot recover from -- which on a real swap is a funded
    Monero leg that nobody can open, with no error anywhere.

    So a high `s` arriving here means `adapt`'s low-S step is broken, and the correct
    response is to say so loudly at the moment it happens rather than to repair it one
    layer further from the cause (rule 19: stop the cause existing, not the symptom being
    reported).
    """
    r, s = signature
    if not 1 <= r < SECP256K1_ORDER or not 1 <= s < SECP256K1_ORDER:
        raise AdaptorJoinError(f"(r, s) is not a pair of scalars in 1..n-1: r is {r.bit_length()} bits, s is {s.bit_length()}")
    if s > SECP256K1_HALF_ORDER:
        raise AdaptorJoinError(
            "adapt() returned a high-S signature, which it promises never to do. Canonizing it "
            "here would make the chain accept a signature the counterparty cannot recover the "
            "scalar from -- a funded Monero leg nobody can open, with no error anywhere. "
            "modules/adaptor_ecdsa.adapt is the thing to look at"
        )
    return sigencode_der(r, s, SECP256K1_ORDER)


# The shortest possible DER signature is well over this; the check exists only so that
# `item[:-1]` below cannot turn a one-byte push into empty bytes, which `sigdecode_der`
# would then report as a malformed signature rather than as the non-signature it is.
MINIMUM_SIGNATURE_PUSH_BYTES = 2


def _signature_or_none(push: bytes) -> tuple[int, int] | None:
    """`(r, s)` if this push is a DER signature with a trailing SIGHASH byte, else None.

    None rather than an exception because a push that is NOT a signature is the ordinary
    case, not a failure: every scriptSig this harness builds carries the redeem script as a
    push, and it lands here on every call. The caller cannot mistake the answer for a real
    one either -- `recover_published_scalar` reports how many signatures it saw, so a parse
    that found nothing renders differently from a transaction that leaked nothing (rule 14).

    The trailing SIGHASH byte is stripped before decoding. `sigdecode_der` is strict about
    trailing bytes, so leaving it on would make every real signature look like a
    non-signature and this function would answer None for a perfectly good one.
    """
    if len(push) < MINIMUM_SIGNATURE_PUSH_BYTES:
        return None
    try:
        r, s = sigdecode_der(push[:-1], SECP256K1_ORDER)
    except Exception:  # noqa: BLE001 -- checked: "not a DER signature" is the ordinary case for a push, and it is returned as None rather than swallowed
        return None
    return (r, s)


def signatures_in_script_sig(script_sig: bytes) -> list[tuple[int, int]]:
    """Every `(r, s)` a scriptSig's data pushes decode to, in the order they appear.

    READ BACK OFF THE CHAIN, WHICH IS THE POINT. The harness holds the bytes it
    broadcast, and asserting against those would prove nothing about publication -- it
    would prove that a variable in this process still has the value it was given.
    `adaptor_steps` fetches the transaction from the daemon and hands its scriptSig here,
    so the scalar recovered downstream comes out of bytes a consensus rule accepted.

    `htlc_spend.script_pushes` does the walking rather than a second copy of it written
    here (rule 8). Its docstring carries the argument for why a truncated script yields the
    pushes it could read instead of raising, and that argument applies unchanged: this is
    reading somebody else's transaction off a public chain, so the bytes are untrusted.
    """
    return [signature for push in script_pushes(script_sig) if (signature := _signature_or_none(push))]


def point_hex(point) -> str:
    """A secp256k1 point as the 33-byte compressed hex an operator can read off a screen.

    A thin wrapper on `adaptor_ecdsa.point_to_bytes`, and it exists so that a caller printing
    an adaptor point does not have to import the crypto module for one line -- which is how a
    reporting call site ends up next to a signing call site and someone later reaches for the
    other functions in it. An adaptor point is PUBLIC; this is the only value in the join that
    is safe to print, and keeping the printable one in its own named function is what makes
    that visible at the call site.
    """
    return adaptor_ecdsa.point_to_bytes(point).hex()


@dataclass(frozen=True)
class AdaptorLeg:
    """One transaction whose signature is an adaptor pre-signature, before it is completed.

    `pre_signature` is what the pre-signer hands over; `adaptor_point` is the Y it was made
    under. Both are public: a pre-signature reveals nothing about either the signing key or
    the adaptor secret, which is the property that lets it be exchanged at setup.

    The SECRET that completes it is deliberately NOT a field here. It belongs to the party
    who will complete it, and threading it through this object would put both halves of the
    swap in one value -- which is how a value that exists in a process becomes a value the
    next line somebody writes sends over a wire (`monero_swap_protocol` makes the same
    refusal at its own boundary, and for the same reason).
    """

    label: str
    pre_signature: adaptor_ecdsa.PreSignature
    adaptor_point: object
    # The ed25519 public spend share this Y is cross-curve bound to, captured HERE at
    # setup. Recovery is checked against this rather than against a re-derivation from the
    # secret, so the check is a measurement and not a tautology -- see the module header.
    spend_public_at_setup: str


def pre_sign_leg(
    label: str,
    private_key: bytes,
    digest: bytes,
    adaptor_secret: int,
    spend_public_at_setup: str,
) -> AdaptorLeg:
    """Pre-sign `digest` under `Y = adaptor_secret * G`, and VERIFY it before handing it back.

    `adaptor_secret` is taken rather than a point because the harness plays both parties and
    would otherwise derive the same point twice; the point is derived once, here. A real
    implementation receives Y over the wire with a DLEQ proof and never sees the scalar --
    `monero_swap_protocol.verify_share_commitment` is that gate, and it is not this
    function's job.

    THE `pre_verify` CALL IS NOT CEREMONY. A pre-signature that does not verify cannot be
    detected later by anything the chain does: `adapt` will happily produce a well-formed
    (r, s) from it, the daemon will refuse the spend with a generic script failure, and the
    operator reading that refusal has no way to tell a broken pre-signature from a broken
    script -- on a harness whose entire job is to say whether the script is broken. So it is
    checked at the moment it is made, against the same public key the redeem script names,
    and a failure raises rather than scoring FAIL because it means this repository disagrees
    with itself rather than that the chain answered wrongly.
    """
    secret_scalar = int.from_bytes(private_key, "big")
    adaptor_point = adaptor_ecdsa.public_key_point(adaptor_secret)
    pre_signature = adaptor_ecdsa.pre_sign(secret_scalar, digest, adaptor_point)
    public_key = adaptor_ecdsa.public_key_point(secret_scalar)
    if not adaptor_ecdsa.pre_verify(public_key, digest, adaptor_point, pre_signature):
        raise AdaptorJoinError(
            f"the {label} pre-signature does not verify under the key that just made it. That is "
            f"this repository disagreeing with itself, not the chain answering wrongly -- and it "
            f"would otherwise surface as a generic script failure a reader cannot diagnose"
        )
    return AdaptorLeg(
        label=label,
        pre_signature=pre_signature,
        adaptor_point=adaptor_point,
        spend_public_at_setup=spend_public_at_setup,
    )


def complete_leg(leg: AdaptorLeg, adaptor_secret: int, sighash_byte: int) -> bytes:
    """The completed signature, DER plus the SIGHASH byte -- what goes into the scriptSig.

    This is the moment the swap becomes irreversible for the completing party: from here the
    bytes can be broadcast, and broadcasting them publishes the scalar. Nothing in this
    function can be undone by not sending, which is why the ordering in
    `monero_swap_protocol.safe_to_release_redeem_presignature` is a separate gate and not a
    parameter here.
    """
    return der_from_signature(adaptor_ecdsa.adapt(leg.pre_signature, adaptor_secret)) + bytes([sighash_byte])


@dataclass(frozen=True)
class RecoveryEvidence:
    """What reading a published scriptSig established, as facts rather than as a boolean.

    `signatures_seen` is carried so a zero recovery can be told apart from a scriptSig that
    parsed to nothing -- rule 14's "(none) is a result, a blank gap is ambiguous between
    zero rows and a query that broke", applied to a signature count. Without it, "no scalar
    recovered" reads identically for a mangled parse and for a transaction that genuinely
    leaks nothing, and those want opposite responses from a reader.

    `other_signatures_leaked_nothing` is the half that makes the positive result mean
    something. One of the two signatures in a redeem scriptSig is the completed adaptor and
    the other is an ordinary signature; if BOTH yielded the scalar, the recovery would be
    finding it in something other than the adaptor mechanism and the whole measurement would
    be worthless.
    """

    label: str
    signatures_seen: int
    recovered: int | None
    other_signatures_leaked_nothing: bool
    matches_setup_commitment: bool


def recover_published_scalar(leg: AdaptorLeg, script_sig: bytes, ed25519_public_for) -> RecoveryEvidence:
    """Read the scriptSig a daemon accepted and pull the Monero spend share out of it.

    THIS IS THE FUNCTION THE WHOLE SWAP RESTS ON, one layer up from
    `adaptor_ecdsa.recover_adaptor_secret` which carries the same sentence. The difference
    is the input: that one is handed an `(r, s)` a caller already has, and this one is
    handed raw script bytes fetched back from a chain. Everything between those two --
    finding the signature among the pushes, stripping the SIGHASH byte, telling the adaptor
    signature apart from the ordinary one beside it -- is where a working scheme becomes an
    unusable one, and none of it was exercised anywhere in this tree before this module.

    EVERY signature in the scriptSig is tried, not just the one this harness knows it put
    there, and the count of how many yielded the scalar is returned. A counterparty watching
    the chain is in exactly that position: they see a scriptSig, they do not see which push
    was the adaptor. Trying only the one we placed would be testing a fact we supplied
    rather than one the chain published.

    `ed25519_public_for` is passed in rather than imported so this module never depends on
    Monero encoding -- `chains/monero_keys.public_key_for_share` is what the caller passes,
    and the seam is what lets this be tested with a stub (rule 10: the decision is here, the
    curve is not).
    """
    signatures = signatures_in_script_sig(script_sig)
    recovered: int | None = None
    leaked = 0
    for signature in signatures:
        candidate = adaptor_ecdsa.recover_adaptor_secret(leg.pre_signature, signature)
        if candidate is not None:
            recovered, leaked = candidate, leaked + 1
    matches = recovered is not None and ed25519_public_for(recovered).hex() == leg.spend_public_at_setup.lower()
    return RecoveryEvidence(
        label=leg.label,
        signatures_seen=len(signatures),
        recovered=recovered,
        other_signatures_leaked_nothing=leaked == 1,
        matches_setup_commitment=matches,
    )


def nothing_leaks(legs: list[AdaptorLeg], script_sig: bytes) -> bool:
    """True when a scriptSig yields NO scalar for any of `legs` -- the punish branch's property.

    Asserted rather than assumed, because "we did not call adapt here" is an argument about
    this file and not a measurement of the bytes. docs/monero_swap_protocol.md specifies
    Tx_punish and Tx_cancel as plain signatures on both sides, and the consequence -- that
    taking the punish branch tells the counterparty nothing -- is a protocol property that
    ought to be visible on screen next to the ones that do leak. Rule 17: a reason to
    believe something is not the same as having checked it.
    """
    signatures = signatures_in_script_sig(script_sig)
    return not any(
        adaptor_ecdsa.recover_adaptor_secret(leg.pre_signature, signature) is not None
        for leg in legs
        for signature in signatures
    )


__all__ = [
    "SECP256K1_HALF_ORDER",
    "SECP256K1_ORDER",
    "AdaptorJoinError",
    "AdaptorLeg",
    "RecoveryEvidence",
    "complete_leg",
    "der_from_signature",
    "nothing_leaks",
    "point_hex",
    "pre_sign_leg",
    "recover_published_scalar",
    "signatures_in_script_sig",
]
